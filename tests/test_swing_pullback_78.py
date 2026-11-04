"""
Stage 7.8 Part B - the SWING TREND PULLBACK research baseline (swing_pullback/; DESIGN_78_SWING_PULLBACK.md): V1, a modified D.3 research
baseline, and its control C1, on SYNTHETIC fixtures only (tests/swing_pullback_fixtures.py). No real data, no network, no production database.

  A01-A04 the pre-registration: every value, its hash, the universe, the experiments, the declared deviations
  I01-I03 the indicators: known answers, the analysis/indicators.py formulas, causality
  R01-R10 the rules, one at a time: regime, trend, pullback trigger (RSI / lower closes), depth guard, price, liquidity, the volume feed, one
          listing, ties
  R11-R17 the trend windows against exact rational sums; every symbol-session against an independent exact reference (random walks, a flat
          SPY, IEX); strict lower closes; the $5 rule on IEX; a regime tie; IEX ignores raw volume; a one-ulp slope
  W01-W03 the warm-up: 200 complete dataset sessions for the symbol and SPY, never filled
  K01-K03 the 9-session cooldown: signal dates only, suppressed sessions never restart it, 9 in every neighbour
  L01-L06 look-ahead and leakage: truncation at every cut, rewritten futures, a fixed holding limit, a leaking variant is caught, the static
          scan, an independent re-derivation of every candidate
  C01-C04 the control: C1 is V1 without the pullback rule (trigger and guard) and nothing else
  H01-H04 the harness: exits, the time exit, the store accepts the 2x-cost run as the same version and refuses a changed rule, no network
  D01-D08 the diagnostics: fold boundaries, folds by signal date for both versions, the TRAIN-only sensitivity, nothing changes the verdict,
          the purge, fold positivity, every sensitivity row equal to a full harness run of its configuration at the run's cost
  P01     the two pre-registered cost runs per version
  X01-X02 isolation: imports, nothing imports the package, no registry entry, no database, no socket
  G01     the design document
  F01-F03 scope (freeze bookkeeping)
"""
from __future__ import annotations

import ast
import copy
import functools
import json
import math
import socket
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest

import freeze_ledger as FL
import swing_pullback as SP
import swing_pullback_fixtures as F
from swing_pullback import diagnostics as SPD
from swing_pullback import indicators as IND
from swing_pullback import plan as PL
from swing_pullback.adapter import PARAMS, SwingTrendPullbackAdapter, SwingTrendPullbackControlAdapter
from swing_pullback.core import REASONS, SwingPullbackCore
from swing_pullback.data import SwingDataError
from strategy_validation import candidates as CD
from strategy_validation import causality as CA
from strategy_validation import engine as E
from strategy_validation.dataset import Dataset
from strategy_validation.models import Bar

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-11-01T00:00:00+00:00"
N = len(F.SESSIONS)
PREREG_HASH = "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
EXITS = {"STOP", "STOP_GAP", "TARGET", "TIME_EXIT", "DATA_END"}


# ============================================================================================================ helpers
def closes_from(rets, price=50.0):
    out, c = [], price
    for i, r in enumerate(rets):
        c = price if i == 0 else c * (1.0 + r)
        out.append(c)
    return out


def rets(drift=0.001, n=N, **at):
    """Constant daily returns with overrides {index: return}."""
    r = [drift] * n
    for k, v in at.items():
        r[int(k[1:])] = v
    return r


def dip(T, kind, drift=0.001, n=N):
    r = [drift] * n
    if kind == "rsi":
        r[T] = -0.03                                           # one 3 % drop: RSI(2) ~3, one decline only
    elif kind == "lower3":
        r[T - 2] = r[T - 1] = r[T] = -0.0001                   # three tiny declines: RSI(2) ~59, three successive lower closes
    elif kind == "lower2":
        r[T - 1] = r[T] = -0.0001                              # two tiny declines: no trigger
    elif kind == "guard":
        r[T] = -0.06                                           # a 6 % drop: RSI(2) ~2, close < 0.97 x SMA50
    return closes_from(r)


def one(closes, sym="AAPL", **kw):
    return F.stock(sym, closes, **kw)


def ds_of(specs, **kw):
    return F.dataset(specs if isinstance(specs, list) else [specs], **kw)


def trace(ds, control=False, **core_kw):
    ev = SwingPullbackCore(not control, **core_kw).evaluate(ds, trace=True)
    return ev, {(r["symbol"], r["date"]): r for r in ev["trace"]}


def reason_at(ds, T, sym="AAPL", control=False):
    return trace(ds, control)[1][(sym, F.SESSIONS[T])]["reason"]


def signal_days(ev, sym="AAPL"):
    pos = {d: i for i, d in enumerate(F.SESSIONS)}
    return [pos[s["signal_date"]] for s in ev["signals"] if s["symbol"] == sym]


def dump(raws):
    return [json.loads(r.model_dump_json()) for r in raws]


@functools.lru_cache(maxsize=None)
def uni():
    return F.dataset(F.universe(6))


@functools.lru_cache(maxsize=None)
def uni_regime():
    """The same stocks under an SPY that crosses its SMA200 again and again: the regime decides candidates."""
    return F.dataset(F.universe(6), spy=F.wave(N, price=300.0, drift=0.0002, amp=0.06, period=90.0))


@functools.lru_cache(maxsize=None)
def walks_sip():
    """Cent-rounded random walks: the trend, slope, regime, liquidity and pullback rules bind both ways; a second listing; a bar gap; raw
    gaps (no raw price, then no liquidity history)."""
    specs = [F.stock(sym, F.walk(seed=11 + i, price=14.0 + 6 * i, sigma=0.011 + 0.002 * (i % 3))) for i, sym in enumerate(F.SYMBOLS[:5])]
    specs += [F.stock("GOOG", F.walk(seed=21, price=40.0), cik="ALPHABET"), F.stock("GOOGL", F.walk(seed=22, price=41.0), cik="ALPHABET"),
              F.stock("AMGN", F.walk(seed=23, price=30.0), gaps=[300], raw_gaps=range(600, 630)),
              F.stock("AMZN", F.walk(seed=24, price=35.0), raw_gaps=[700])]
    return F.dataset(specs, spy=F.walk(seed=99, price=300.0, sigma=0.008, drift=0.0004))


@functools.lru_cache(maxsize=None)
def walks_iex():
    """IEX volume: raw closes crossing $5, a stock without raw volume, raw gaps, an SPY that turns exactly flat (regime ties) from session 400
    and misses session 700."""
    spy = F.walk(seed=98, price=300.0, sigma=0.008, drift=0.0006)
    spy = spy[:400] + [spy[399]] * (N - 400)
    specs = [F.stock(sym, F.walk(seed=31 + i, price=20.0 + 5 * i)) for i, sym in enumerate(F.SYMBOLS[:3])]
    specs += [F.stock("ADBE", F.walk(seed=41, price=30.0), raw_mult=0.125), F.stock("ADI", F.walk(seed=42, price=30.0), volume=None),
              F.stock("AMD", F.walk(seed=43, price=25.0), raw_gaps=range(500, 530))]
    return F.dataset(specs, spy=spy, spy_gaps=[700], feed="IEX")


@functools.lru_cache(maxsize=None)
def sizing_mix():
    """V1 trades of every sizing branch and both kinds of exit: a calm riser (CAPPED, time exits), a fast wave (RISK_SIZED, time exits),
    a $30,000 share (MIN_ONE_UNIT, stops)."""
    calm = [0.0004] * N
    for T in range(230, N, 31):
        calm[T - 2] = calm[T - 1] = calm[T] = -0.00005
    return F.dataset([F.stock("AAPL", closes_from(calm, 40.0)), F.stock("ABBV", F.wave(N, price=40.0, drift=0.002, amp=0.06, period=5.0)),
                      F.stock("ABT", F.wave(N, price=30000.0, drift=0.001, amp=0.10, period=13.0))])


@functools.lru_cache(maxsize=None)
def runs():
    ds = uni()
    return ds, {(v, c): PL.run(v, c, ds, created_at=AT) for v, c in PL.RUNS}


def rec(run):
    return json.loads(run.model_dump_json())


def _cum(xs):
    """Exact prefix sums (rational arithmetic) of a series of doubles: the window [a, b] sums to cum[b + 1] - cum[a] with no rounding."""
    out, t = [Fraction(0)], Fraction(0)
    for x in xs:
        t += Fraction(x)
        out.append(t)
    return out


def reference(ds, control=False, rsi_max=10.0, stop_atr=2.0, target_atr=3.0):
    """An independent re-derivation: exact rational window sums for the strict comparisons, the analysis/indicators.py RSI / ATR, plain loops.
    Returns [(symbol, signal_date, stop, target)]."""
    import pandas as pd
    from analysis import indicators as AI
    sessions = ds.sessions
    pos = {d: i for i, d in enumerate(sessions)}
    issuers = {s: iid for iid, info in ds.aux["static"]["issuers"].items() for s in info["symbols"]}
    syms, seen = [], set()
    for s in sorted(x for x in SP.RESEARCH_UNIVERSE if x in ds.bars):
        if issuers[s] not in seen:
            syms.append(s)
        seen.add(issuers[s])
    spy = {b.date: b.close for b in ds.bars["SPY"]}
    spy_dates = [b.date for b in ds.bars["SPY"]]
    spy_k, spy_cum = {d: q for q, d in enumerate(spy_dates)}, _cum([spy[d] for d in spy_dates])
    raw_c, raw_v = ds.aux["series"]["raw_close"], ds.aux["series"]["raw_volume"]
    feed = ds.declared["volume_feed"]
    out = []
    for s in syms:
        bars = ds.bars[s]
        df = pd.DataFrame({"high": [b.high for b in bars], "low": [b.low for b in bars], "close": [b.close for b in bars]})
        rsi, atr = list(AI.rsi(df["close"], 2)), list(AI.atr(df, 14))
        c = [b.close for b in bars]
        cum = _cum(c)
        own = {b.date: k for k, b in enumerate(bars)}
        last = None
        for k, b in enumerate(bars):
            i = pos[b.date]
            if i < 199:
                continue
            win = sessions[i - 199:i + 1]
            if any(d not in own for d in win) or any(d not in spy for d in win):
                continue
            rc = raw_c.get(s, {}).get(b.date)
            if rc is None or rc < 5.0:
                continue
            if feed == "SIP":
                w = sessions[i - 19:i + 1]
                vals = [(raw_c.get(s, {}).get(d), raw_v.get(s, {}).get(d)) for d in w]
                if any(x is None or y is None for x, y in vals) or statistics.median(x * y for x, y in vals) < 20_000_000:
                    continue
            q = spy_k[b.date]
            if not 200 * Fraction(spy[b.date]) > spy_cum[q + 1] - spy_cum[q - 199]:          # the strict comparisons, exactly
                continue
            sma50 = statistics.fmean(c[k - 49:k + 1])
            if not (200 * Fraction(c[k]) > cum[k + 1] - cum[k - 199] and cum[k + 1] - cum[k - 49] > cum[k - 19] - cum[k - 69]):
                continue
            if not control:
                trig = rsi[k] <= rsi_max or (c[k] < c[k - 1] < c[k - 2] < c[k - 3])
                if not (trig and c[k] >= 0.97 * sma50):
                    continue
            if last is not None and i - last <= 9:
                continue
            last = i
            out.append((s, b.date, c[k] - stop_atr * atr[k], c[k] + target_atr * atr[k]))
    return out


def ref_reasons(ds, control=False):
    """An independent re-derivation of the FIRST failing rule at every symbol-session (the documented order): pandas RSI, exact rational
    window sums, plain loops. Returns {(symbol, date): reason}."""
    import pandas as pd
    from analysis import indicators as AI
    sessions = ds.sessions
    pos = {d: i for i, d in enumerate(sessions)}
    issuers = {s: iid for iid, info in ds.aux["static"]["issuers"].items() for s in info["symbols"]}
    present = sorted(x for x in SP.RESEARCH_UNIVERSE if x in ds.bars)
    kept = {}
    for s in present:
        kept.setdefault(issuers[s], s)
    spy = {b.date: b.close for b in ds.bars.get("SPY", [])}
    spy_dates = [b.date for b in ds.bars.get("SPY", [])]
    spy_k, spy_cum = {d: q for q, d in enumerate(spy_dates)}, _cum([spy[d] for d in spy_dates])
    raw_c, raw_v = ds.aux["series"].get("raw_close", {}), ds.aux["series"].get("raw_volume", {})
    sip = ds.declared["volume_feed"] == "SIP"
    out = {}
    for s in present:
        bars = ds.bars[s]
        if kept[issuers[s]] != s:
            out.update({(s, b.date): "SECOND_LISTING" for b in bars})
            continue
        c = [b.close for b in bars]
        cum, own = _cum(c), {b.date: k for k, b in enumerate(bars)}
        rsi = list(AI.rsi(pd.Series(c), 2))
        rc_s, rv_s = raw_c.get(s, {}), raw_v.get(s, {})
        last = None
        for k, b in enumerate(bars):
            d, i = b.date, pos[b.date]
            rc = rc_s.get(d)
            w20 = sessions[i - 19:i + 1]
            if i < 199:
                why = "WARMUP"
            elif any(x not in own for x in sessions[i - 199:i + 1]):
                why = "MISSING_WINDOW_BAR"
            elif any(x not in spy for x in sessions[i - 199:i + 1]):
                why = "REGIME_WINDOW_INCOMPLETE"
            elif rc is None or not rc > 0:
                why = "NO_RAW_PRICE"
            elif rc < 5.0:
                why = "PRICE_BELOW_MIN"
            elif sip and any(rc_s.get(x) is None or rv_s.get(x) is None for x in w20):
                why = "LIQUIDITY_HISTORY"
            elif sip and statistics.median(rc_s[x] * rv_s[x] for x in w20) < 20_000_000:
                why = "LIQUIDITY_BELOW_MIN"
            elif not 200 * Fraction(spy[d]) > spy_cum[spy_k[d] + 1] - spy_cum[spy_k[d] - 199]:
                why = "REGIME_OFF"
            elif not 200 * Fraction(c[k]) > cum[k + 1] - cum[k - 199]:
                why = "BELOW_SLOW_SMA"
            elif not cum[k + 1] - cum[k - 49] > cum[k - 19] - cum[k - 69]:
                why = "FAST_SMA_NOT_RISING"
            elif not control and not (rsi[k] <= 10.0 or c[k] < c[k - 1] < c[k - 2] < c[k - 3]):
                why = "NO_PULLBACK_TRIGGER"
            elif not control and not c[k] >= 0.97 * statistics.fmean(c[k - 49:k + 1]):
                why = "BELOW_DEPTH_GUARD"
            elif last is not None and i - last <= 9:
                why = "COOLDOWN"
            else:
                why, last = "CANDIDATE", i
            out[(s, d)] = why
    return out


# ============================================================================================================ pre-registration (A)
def test_A01_every_rule_value_is_the_signed_off_one():
    assert (SP.STRATEGY_ID, SP.STRATEGY_VERSION, SP.CONTROL_ID, SP.CONTROL_VERSION) == ("SWING_TREND_PULLBACK", "1.0", "SWING_TREND_PULLBACK_CONTROL", "1.0")
    assert (SP.MIN_PRICE, SP.MIN_DOLLAR_VOLUME, SP.DOLLAR_VOLUME_SESSIONS, SP.VOLUME_FEEDS) == (5.0, 20_000_000.0, 20, ("SIP", "IEX"))
    assert (SP.SLOW_SMA, SP.FAST_SMA, SP.SLOPE_LOOKBACK, SP.RSI_PERIOD, SP.RSI_MAX, SP.LOWER_CLOSES, SP.DEPTH_GUARD) == (200, 50, 20, 2, 10.0, 3, 0.97)
    assert (SP.ATR_PERIOD, SP.STOP_ATR, SP.TARGET_ATR, SP.MAX_HOLD_BARS, SP.COOLDOWN_SESSIONS, SP.WARMUP_SESSIONS) == (14, 2.0, 3.0, 10, 9, 200)
    assert SP.REGIME_SYMBOL == "SPY" and SP.SCORE == 50.0
    assert (SP.FOLDS, SP.FOLD_START_INDEX) == (6, 199)
    assert SP.COSTS == {"BASE": 5.0, "STRESS_2X": 10.0} and SP.COSTS["STRESS_2X"] == 2 * SP.COSTS["BASE"]
    assert list(SP.SENSITIVITY) == [{"rsi_max": 7.5}, {"rsi_max": 12.5}, {"stop_atr": 1.5}, {"stop_atr": 2.5}, {"target_atr": 2.25}, {"target_atr": 3.75},
                                    {"max_hold_bars": 8}, {"max_hold_bars": 12}]
    assert all(len(p) == 1 for p in SP.SENSITIVITY) and {k for p in SP.SENSITIVITY for k in p} == set(PARAMS)   # one at a time, the only parameters
    base = {"rsi_max": SP.RSI_MAX, "stop_atr": SP.STOP_ATR, "target_atr": SP.TARGET_ATR, "max_hold_bars": SP.MAX_HOLD_BARS}
    for p in SP.SENSITIVITY:
        (k, v), = p.items()
        assert v in (base[k] * 0.75, base[k] * 1.25) or (k == "max_hold_bars" and v in (8, 12)), p   # +-25 %; the holding period rounded to 8 / 12


def test_A02_the_preregistration_hash_pins_every_value():
    assert SP.preregistration_hash() == PREREG_HASH
    pr = SP.preregistration()
    assert pr["cooldown_sessions"] == 9 and pr["max_hold_bars"] == 10 and pr["folds"] == 6 and set(pr["deviations"]) == {"D1", "D2", "D3", "D4", "D5"}
    assert "V2" in pr["deferred"] and "EMA_SUPPORT" in pr["deferred"]


def test_A03_the_universe_is_the_research_universe_and_one_listing_per_issuer_is_declared():
    import value_quality as VQ
    assert SP.RESEARCH_UNIVERSE == VQ.RESEARCH_UNIVERSE and len(SP.RESEARCH_UNIVERSE) == 81 and "SPY" not in SP.RESEARCH_UNIVERSE
    assert {"GOOG", "GOOGL"} <= set(SP.RESEARCH_UNIVERSE)


def test_A04_v1_says_it_is_a_modified_d3_baseline_and_declares_every_deviation():
    assert "MODIFIED D.3 RESEARCH BASELINE" in SP.__doc__ and "not a faithful implementation" in SP.__doc__
    a, c = SwingTrendPullbackAdapter(), SwingTrendPullbackControlAdapter()
    flags = a.research_flags(uni())
    assert set(flags) == {"MODIFIED_D3_BASELINE", "SURVIVORSHIP_BIAS_PRESENT", "CLOSE_BASED_EXIT_NOT_MODELLED", "EARNINGS_FILTER_NOT_APPLIED",
                          "STOP_TARGET_FROM_SIGNAL_CLOSE", "PORTFOLIO_LIMITS_NOT_SIMULATED", "REGULATORY_FEES_NOT_MODELLED"}
    assert set(c.research_flags(uni())) == set(flags) | {"CONTROL_ARM_NOT_A_STRATEGY"}
    assert "LIQUIDITY_RULE_NOT_APPLIED_IEX_VOLUME" in a.research_flags(F.dataset(F.universe(2), feed="IEX"))
    assert "close-based exit" in SP.DEVIATIONS["D1"] and "UNRESOLVED" in SP.DEVIATIONS["D1"] and "EARNINGS_FILTER_NOT_APPLIED" in SP.DEVIATIONS["D2"]
    assert "not modelled" in a.exit_condition and "deviation D1" in a.exit_condition


def test_A05_every_neighbour_applies_its_own_value_and_only_it():
    ds = uni()
    base = {(x["symbol"], x["signal_date"]): x for x in SwingPullbackCore().evaluate(ds)["signals"]}
    for p in SP.SENSITIVITY:
        a = SwingTrendPullbackAdapter().with_params(p)
        (k, v), = p.items()
        assert (a.core.rsi_max, a.core.stop_atr, a.core.target_atr, a.max_hold_bars) == (
            p.get("rsi_max", 10.0), p.get("stop_atr", 2.0), p.get("target_atr", 3.0), p.get("max_hold_bars", 10))
        sigs = a.core.evaluate(ds)["signals"]
        assert sigs
        for x in sigs:
            assert x["stop"] == pytest.approx(x["close"] - a.core.stop_atr * x["atr"], rel=1e-12)
            assert x["target"] == pytest.approx(x["close"] + a.core.target_atr * x["atr"], rel=1e-12)
            assert x["rsi"] <= a.core.rsi_max or x["lower_closes"]
        if k in ("stop_atr", "target_atr"):                                                   # same candidates, other levels
            assert {(x["symbol"], x["signal_date"]) for x in sigs} == set(base)
            assert all(x[k[:-4]] != base[(x["symbol"], x["signal_date"])][k[:-4]] for x in sigs)


# ============================================================================================================ indicators (I)
def test_I01_known_answers():
    assert IND.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    # RSI(2), Wilder: deltas +1, -2, +1 -> gains 1, 0, 1 / losses 0, 2, 0; ewm alpha 1/2 seeded with the first delta
    r = IND.wilder_rsi([10, 11, 9, 10], 2)
    ag2, al2 = 0.5 * 1 + 0.5 * 0, 0.5 * 0 + 0.5 * 2                # index 2: two observations
    ag3, al3 = 0.5 * ag2 + 0.5 * 1, 0.5 * al2 + 0.5 * 0
    assert r[:2] == [None, None] and r[2] == pytest.approx(100 - 100 / (1 + ag2 / al2)) and r[3] == pytest.approx(100 - 100 / (1 + ag3 / al3))
    assert IND.wilder_rsi([1, 2, 3, 4], 2)[2:] == [100.0, 100.0]  # no loss: 100
    # ATR(2): TR0 = H0 - L0; TR1 = max(H-L, |H-C0|, |L-C0|)
    a = IND.wilder_atr([11, 12, 10], [9, 10, 8], [10, 11, 9], 2)
    tr = [2.0, max(2.0, 2.0, 0.0), max(2.0, 1.0, 3.0)]
    assert a[0] is None and a[1] == pytest.approx(0.5 * tr[0] + 0.5 * tr[1]) and a[2] == pytest.approx(0.5 * a[1] + 0.5 * tr[2])


def test_I02_rsi_and_atr_are_the_analysis_indicators_formulas_and_the_sma_is_the_rolling_mean():
    import pandas as pd
    from analysis import indicators as AI
    closes = F.wave(300, price=40.0, amp=0.05, period=13.0)
    bars = F.bars_of(closes, F.SESSIONS[:300])
    df = pd.DataFrame({"high": [b["high"] for b in bars], "low": [b["low"] for b in bars], "close": [b["close"] for b in bars]})
    for n in (2, 14):
        mine, theirs = IND.wilder_rsi(df["close"].tolist(), n), AI.rsi(df["close"], n).tolist()
        for x, y in zip(mine, theirs):
            assert (x is None and math.isnan(y)) or x == pytest.approx(y, rel=1e-12, abs=1e-9)
        mine, theirs = IND.wilder_atr(df["high"].tolist(), df["low"].tolist(), df["close"].tolist(), n), AI.atr(df, n).tolist()
        for x, y in zip(mine, theirs):
            assert (x is None and math.isnan(y)) or x == pytest.approx(y, rel=1e-12, abs=1e-12)
    for n in (50, 200):
        for x, y in zip(IND.sma(df["close"].tolist(), n), df["close"].rolling(n).mean().tolist()):
            assert (x is None and math.isnan(y)) or x == pytest.approx(y, rel=1e-12)


def test_I03_every_indicator_is_causal_a_prefix_gives_the_same_values():
    closes = F.wave(260, amp=0.06, period=11.0)
    bars = F.bars_of(closes, F.SESSIONS[:260])
    h, lo, c = [b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars]
    full = (IND.sma(c, 50), IND.wilder_rsi(c, 2), IND.wilder_atr(h, lo, c, 14))
    for cut in (1, 2, 13, 14, 15, 49, 50, 120, 259):
        part = (IND.sma(c[:cut], 50), IND.wilder_rsi(c[:cut], 2), IND.wilder_atr(h[:cut], lo[:cut], c[:cut], 14))
        for f, p in zip(full, part):
            assert f[:cut] == p, cut


# ============================================================================================================ rules (R)
def test_R01_the_rsi_trigger_and_the_lower_close_trigger_each_make_a_v1_candidate():
    ev, tr = trace(ds_of(one(dip(400, "rsi"))))
    row = tr[("AAPL", F.SESSIONS[400])]
    sig = next(s for s in ev["signals"] if s["signal_date"] == F.SESSIONS[400])
    assert row["reason"] == "CANDIDATE" and sig["rsi"] <= 10 and not sig["lower_closes"]
    ev, tr = trace(ds_of(one(dip(400, "lower3"))))
    sig = next(s for s in ev["signals"] if s["signal_date"] == F.SESSIONS[400])
    assert sig["rsi"] > 10 and sig["lower_closes"]                          # the lower-close path alone
    assert reason_at(ds_of(one(dip(400, "lower2"))), 400) == "NO_PULLBACK_TRIGGER"   # two declines are not three
    assert reason_at(ds_of(one(closes_from(rets()))), 400) == "NO_PULLBACK_TRIGGER"  # a steady rise: RSI 100, no decline


def test_R02_the_rsi_threshold_is_inclusive_and_the_depth_guard_too():
    ds = ds_of(one(dip(400, "rsi")))
    rsi = next(s for s in SwingPullbackCore().evaluate(ds)["signals"] if s["signal_date"] == F.SESSIONS[400])["rsi"]
    assert trace(ds, rsi_max=rsi)[1][("AAPL", F.SESSIONS[400])]["reason"] == "CANDIDATE"
    assert trace(ds, rsi_max=math.nextafter(rsi, 0.0))[1][("AAPL", F.SESSIONS[400])]["reason"] == "NO_PULLBACK_TRIGGER"
    core = SwingPullbackCore()
    s = type("S", (), {})()
    s.close, s.sma_fast = [0.97 * 100.0], [100.0]
    below = type("S", (), {"close": [math.nextafter(0.97 * 100.0, 0.0)], "sma_fast": [100.0]})()
    assert core.guard(s, 0) and not core.guard(below, 0)


def test_R03_a_drop_below_the_depth_guard_is_no_v1_candidate():
    assert reason_at(ds_of(one(dip(400, "guard"))), 400) == "BELOW_DEPTH_GUARD"


def test_R04_the_regime_and_the_trend_rules():
    up = one(dip(400, "rsi"))
    assert reason_at(ds_of(up, spy=closes_from(rets(-0.001), 300.0)), 400) == "REGIME_OFF"
    assert reason_at(ds_of(one(closes_from(rets(-0.001, a400=-0.03)))), 400) == "BELOW_SLOW_SMA"
    r = [0.002 if i < 330 else -0.0005 for i in range(N)]
    r[400] = -0.03
    assert reason_at(ds_of(one(closes_from(r))), 400) == "FAST_SMA_NOT_RISING"          # above SMA200, SMA50 falling
    assert reason_at(ds_of(one(closes_from(r))), 400, control=True) == "FAST_SMA_NOT_RISING"


def test_R05_the_price_rule_reads_the_raw_close_inclusive():
    c = dip(400, "rsi")
    assert reason_at(ds_of(one(c, raw_mult=0.05)), 400) == "PRICE_BELOW_MIN"          # adjusted ~74, raw ~3.7: the raw close decides
    assert reason_at(ds_of(one([x * 0.05 for x in c], raw_mult=20.0)), 400) == "CANDIDATE"   # adjusted ~3.7, raw ~74
    assert reason_at(ds_of(one(c, raw_override={400: 5.0})), 400) == "CANDIDATE"
    assert reason_at(ds_of(one(c, raw_override={400: math.nextafter(5.0, 0.0)})), 400) == "PRICE_BELOW_MIN"
    assert reason_at(ds_of(one(c, raw_gaps=[400])), 400) == "NO_RAW_PRICE"


def test_R06_the_liquidity_rule_is_the_20_session_median_of_raw_dollar_volume_inclusive():
    c = dip(400, "rsi")
    assert reason_at(ds_of(one(c, volume=100_000.0)), 400) == "LIQUIDITY_BELOW_MIN"
    assert reason_at(ds_of(one(c, raw_gaps=[390])), 400) == "LIQUIDITY_HISTORY"
    assert reason_at(ds_of(one(c, raw_gaps=[381])), 400) == "LIQUIDITY_HISTORY"                  # the 20th session back is in the window
    assert reason_at(ds_of(one(c, raw_gaps=[380])), 400) == "CANDIDATE"                          # the 21st is not
    exact = {i: 50.0 for i in range(381, 401)}
    assert reason_at(ds_of(one(c, volume=400_000.0, raw_override=exact)), 400) == "CANDIDATE"     # 50 x 400 000 = 20 M exactly
    assert reason_at(ds_of(one(c, volume=399_999.0, raw_override=exact)), 400) == "LIQUIDITY_BELOW_MIN"
    low11 = {i: 1.0 for i in range(381, 392)}                                                      # 11 of 20 tiny: median 1 M (the mean would be 25.3 M)
    assert reason_at(ds_of(one(c, volume=1_000_000.0, raw_override={**exact, **low11})), 400) == "LIQUIDITY_BELOW_MIN"
    low9 = {i: 1.0 for i in range(381, 390)}                                                       # 9 of 20: median 50 M
    assert reason_at(ds_of(one(c, volume=1_000_000.0, raw_override={**exact, **low9})), 400) == "CANDIDATE"
    lows = {i: 1.0 for i in range(380, 391)}                                                       # 380-390 tiny: [381, 400] holds 10 of them, [380, 399] 11
    assert reason_at(ds_of(one(c, volume=1_000_000.0, raw_override={**exact, **lows})), 400) == "CANDIDATE"   # median (1 M + 50 M) / 2


def test_R07_on_iex_volume_the_liquidity_rule_is_not_applied_and_said_so():
    ds = ds_of(one(dip(400, "rsi"), volume=100_000.0), feed="IEX")
    assert reason_at(ds, 400) == "CANDIDATE"
    raws = list(SwingTrendPullbackAdapter().candidates(ds))
    assert all(r.features["liquidity_rule"] == "NOT_APPLIED_IEX_VOLUME" and r.features["median_dollar_volume"] is None for r in raws)
    sip = list(SwingTrendPullbackAdapter().candidates(ds_of(one(dip(400, "rsi")))))
    assert all(r.features["liquidity_rule"] == "APPLIED" and r.features["median_dollar_volume"] > 20e6 for r in sip)


def test_R08_an_undeclared_or_unknown_volume_feed_or_an_incomplete_issuer_map_refuses_the_run():
    for feed in (None, "", "SIP_OR_IEX", "sip"):
        with pytest.raises(SwingDataError):
            SwingPullbackCore().evaluate(ds_of(one(dip(400, "rsi")), feed=feed))
    with pytest.raises(SwingDataError):
        SwingPullbackCore().evaluate(ds_of([one(dip(400, "rsi")), one(dip(400, "rsi"), sym="ABBV")], issuers={"X": {"symbols": ["AAPL"]}}))
    with pytest.raises(SwingDataError, match="two issuers"):
        SwingPullbackCore().evaluate(ds_of([one(dip(400, "rsi"), sym="GOOG"), one(dip(400, "rsi"), sym="GOOGL")],
                                           issuers={"A": {"symbols": ["GOOG", "GOOGL"]}, "B": {"symbols": ["GOOGL"]}}))


def test_R09_one_listing_per_issuer_goog_is_kept_googl_is_not():
    c = dip(400, "rsi")
    ev, tr = trace(ds_of([one(c, sym="GOOG", cik="ALPHABET"), one(c, sym="GOOGL", cik="ALPHABET")]))
    assert {s["symbol"] for s in ev["signals"]} == {"GOOG"} and ev["second_listings"] == ["GOOGL"]
    assert all(r["reason"] == "SECOND_LISTING" for (s, _), r in tr.items() if s == "GOOGL")
    ev, _ = trace(ds_of([one(c, sym="GOOG"), one(c, sym="GOOGL")]))                     # two issuers: both
    assert {s["symbol"] for s in ev["signals"]} == {"GOOG", "GOOGL"}
    assert "SPY" not in {s["symbol"] for s in trace(uni())[0]["signals"]}


def test_R10_a_tie_is_never_a_pass_flat_windows_give_no_trend():
    for flat in (37.13, 50.07, 99.99):
        r = [0.001] * 400
        c = closes_from(r, price=flat / 1.001 ** 399)[:400] + [flat] * (N - 400)              # a rise, then exactly flat closes
        ev, tr = trace(ds_of(one(c)), control=True)
        late = [i for i in signal_days(ev) if i >= 470]                                       # from 470 both SMA50 windows are flat: equal sums
        assert not late, (flat, late[:5])
        assert tr[("AAPL", F.SESSIONS[600])]["reason"] in ("BELOW_SLOW_SMA", "FAST_SMA_NOT_RISING"), flat   # 600: the 200-session window is flat too
        assert {r["reason"] for (s, d), r in tr.items() if d >= F.SESSIONS[599]} == {"BELOW_SLOW_SMA"}, flat


def test_R11_the_trend_windows_are_exactly_200_50_and_20_bars_in_exact_arithmetic():
    from swing_pullback.core import _Series
    outcomes = Counter()
    for seed in (1, 2, 3, 4):
        c = F.walk(seed=seed, sigma=0.012)
        s = _Series([Bar(**b) for b in F.bars_of(c, F.SESSIONS)])
        cum = _cum(s.close)
        for k in range(199, len(c)):
            slow = 200 * Fraction(s.close[k]) > cum[k + 1] - cum[k - 199]
            fast = cum[k + 1] - cum[k - 49] > cum[k - 19] - cum[k - 69]
            assert (s.above_slow(k), s.fast_rising(k)) == (slow, fast), (seed, k)
            outcomes[("slow", slow)] += 1
            outcomes[("fast", fast)] += 1
    assert min(outcomes.values()) > 100 and len(outcomes) == 4                              # both outcomes of both rules, often


def test_R12_every_symbol_session_has_the_independent_references_first_failing_rule():
    seen = Counter()
    for ds in (uni(), uni_regime(), walks_sip(), walks_iex()):
        for control in (False, True):
            _, tr = trace(ds, control=control)
            ref = ref_reasons(ds, control=control)
            got = {k: v["reason"] for k, v in tr.items()}
            assert got.keys() == ref.keys()
            bad = sorted((k, got[k], ref[k]) for k in got if got[k] != ref[k])
            assert not bad, (len(bad), bad[:5])
            seen.update(ref.values())
    assert set(seen) == set(REASONS), set(REASONS) - set(seen)                                # every rule is exercised, both ways
    assert min(seen.values()) >= 20, seen


def test_R13_three_lower_closes_are_strictly_lower_an_equal_close_is_no_trigger():
    T = 400
    for zero in (T - 1, T):                                                                  # an unchanged close at T - 1 or at T
        r = [0.001] * N
        r[T - 2] = r[T - 1] = r[T] = -0.0001
        r[zero] = 0.0
        c = closes_from(r)
        assert c[zero] == c[zero - 1] and IND.wilder_rsi(c, 2)[T] > 10
        assert reason_at(ds_of(one(c)), T) == "NO_PULLBACK_TRIGGER", zero


def test_R14_the_5_dollar_raw_price_rule_applies_on_iex_volume_too():
    c = dip(400, "rsi")
    assert reason_at(ds_of(one(c, raw_mult=0.05), feed="IEX"), 400) == "PRICE_BELOW_MIN"
    assert reason_at(ds_of(one(c, raw_override={400: math.nextafter(5.0, 0.0)}), feed="IEX"), 400) == "PRICE_BELOW_MIN"
    assert reason_at(ds_of(one(c, raw_override={400: 5.0}), feed="IEX"), 400) == "CANDIDATE"
    assert reason_at(ds_of(one(c, raw_gaps=[400]), feed="IEX"), 400) == "NO_RAW_PRICE"


def test_R15_an_spy_close_equal_to_its_sma200_is_regime_off():
    ds = ds_of(one(closes_from(rets())), spy=[300.0] * N)                                     # a flat SPY: close == SMA200 exactly
    for control in (False, True):
        _, tr = trace(ds, control=control)
        assert {r["reason"] for (s, d), r in tr.items() if d >= F.SESSIONS[199]} == {"REGIME_OFF"}, control
    assert reason_at(ds_of(one(dip(400, "rsi")), spy=[300.0] * N), 400) == "REGIME_OFF"


def test_R16_on_iex_volume_the_raw_volume_is_never_read():
    c = dip(400, "rsi")
    assert reason_at(ds_of(one(c, volume=None), feed="IEX"), 400) == "CANDIDATE"
    assert reason_at(ds_of(one(c, raw_gaps=range(381, 400)), feed="IEX"), 400) == "CANDIDATE"   # no raw history before t either
    assert reason_at(ds_of(one(c, volume=None)), 400) == "LIQUIDITY_HISTORY"                   # on SIP the same stock has no liquidity history


def test_R17_a_sma50_rise_of_one_ulp_is_a_rise_although_the_float_means_are_equal():
    base = [50.0 + (j % 50) * 0.1 for j in range(N)]                                         # period 50: every SMA50 window sums to the same value
    k = 449                                                                                  # the period's top (k % 50 == 49): close far above SMA200
    c = list(base)
    c[k] = math.nextafter(base[k], math.inf)
    a, b = c[k - 49:k + 1], c[k - 69:k - 19]
    assert math.fsum(a) == math.fsum(b) and sum(map(Fraction, a)) > sum(map(Fraction, b))     # equal in floats, larger by one ulp exactly
    ev, tr = trace(ds_of(one(c, exact=True)), control=True)
    assert tr[("AAPL", F.SESSIONS[k])]["reason"] == "CANDIDATE" and signal_days(ev)[0] == k
    before = {r["reason"] for (s, d), r in tr.items() if F.SESSIONS[199] <= d < F.SESSIONS[k]}
    assert before == {"BELOW_SLOW_SMA", "FAST_SMA_NOT_RISING"}                                 # no candidate before the bump: low phases fail
    assert tr[("AAPL", F.SESSIONS[k - 50])]["reason"] == "FAST_SMA_NOT_RISING"                 # SMA200, the top phases fail the exact tie


# ============================================================================================================ warm-up (W)
def test_W01_no_candidate_before_the_200th_dataset_session():
    assert reason_at(ds_of(one(dip(198, "rsi"))), 198) == "WARMUP"
    assert reason_at(ds_of(one(dip(199, "rsi"))), 199) == "CANDIDATE"
    ev, tr = trace(ds_of(one(closes_from(rets()))), control=True)
    assert min(signal_days(ev)) == 199 and all(tr[("AAPL", F.SESSIONS[i])]["reason"] == "WARMUP" for i in range(199))


def test_W02_a_missing_bar_anywhere_in_the_200_sessions_blocks_and_is_never_filled():
    for T, want in ((300, "MISSING_WINDOW_BAR"), (349, "MISSING_WINDOW_BAR"), (350, "CANDIDATE")):
        assert reason_at(ds_of(one(dip(T, "rsi"), gaps=[150])), T) == want, T
    for T, want in ((248, "MISSING_WINDOW_BAR"), (249, "CANDIDATE")):                     # a symbol that starts at session 50
        assert reason_at(ds_of(one(dip(T, "rsi"), start=50)), T) == want, T


def test_W03_spy_needs_the_same_200_complete_sessions():
    assert reason_at(ds_of(one(dip(205, "rsi")), spy_start=10), 205) == "REGIME_WINDOW_INCOMPLETE"
    assert reason_at(ds_of(one(dip(209, "rsi")), spy_start=10), 209) == "CANDIDATE"
    assert reason_at(ds_of(one(dip(300, "rsi")), spy_gaps=[250]), 300) == "REGIME_WINDOW_INCOMPLETE"


# ============================================================================================================ cooldown (K)
def test_K01_a_candidate_blocks_the_next_9_sessions_and_a_blocked_session_never_restarts_it():
    ev, tr = trace(ds_of(one(closes_from(rets()))), control=True)                       # C1 on a steady rise: every session passes every rule
    days = signal_days(ev)
    assert days[:5] == [199, 209, 219, 229, 239] and all(b - a == 10 for a, b in zip(days, days[1:]))
    for i in range(200, 209):
        row = tr[("AAPL", F.SESSIONS[i])]
        assert row["reason"] == "COOLDOWN" and row["pre_cooldown"], i                    # suppressed signals: 209 is still a candidate


def test_K02_v1_candidates_in_a_long_pullback_are_10_sessions_apart():
    r = [0.002] * N
    for i in range(300, 340):
        r[i] = -0.0001                                                                  # forty tiny declines inside an uptrend
    ev, _ = trace(ds_of(one(closes_from(r))))
    assert signal_days(ev) == [302, 312, 322, 332]


def test_K03_the_cooldown_stays_9_sessions_in_every_sensitivity_neighbour():
    r = [0.002] * N
    for i in range(300, 340):
        r[i] = -0.0001
    ds = ds_of(one(closes_from(r)))
    pos = {d: i for i, d in enumerate(F.SESSIONS)}
    for p in SP.SENSITIVITY:
        a = SwingTrendPullbackAdapter().with_params(p)
        assert [pos[x.signal_date] for x in a.candidates(ds)] == [302, 312, 322, 332], p
        assert a.max_hold_bars == p.get("max_hold_bars", 10)


# ============================================================================================================ look-ahead / leakage (L)
def divergences(cls, ds, cuts):
    """The cuts at which the candidates generated on dataset.until(cut) differ (any field) from the full run's candidates signalled by then."""
    full = dump(cls().candidates(ds))
    return [cut for cut in cuts if dump(cls().candidates(ds.until(cut))) != [c for c in full if c["signal_date"] <= cut]]


def test_L01_every_point_in_time_cut_reproduces_every_earlier_candidate_and_rule_outcome_exactly():
    ev = SwingPullbackCore().evaluate(uni_regime())
    assert ev["funnel"]["REGIME_OFF"] > 500 and ev["signals"]                                     # the regime fixture really switches
    for ds, cls in [(d, c) for d in (uni(), uni_regime()) for c in (SwingTrendPullbackAdapter, SwingTrendPullbackControlAdapter)]:
        full = dump(cls().candidates(ds))
        _, ftr = trace(ds, control=cls is SwingTrendPullbackControlAdapter)
        cuts = sorted(set(ds.sessions[199::9]) | {c["signal_date"] for c in full[::7]})
        assert divergences(cls, ds, cuts) == [], cls.__name__
        for cut in cuts[::5]:
            _, ptr = trace(ds.until(cut), control=cls is SwingTrendPullbackControlAdapter)
            assert ptr == {k: v for k, v in ftr.items() if k[1] <= cut}, cut


def test_L02_rewriting_every_bar_and_raw_value_after_t_changes_nothing_at_or_before_t():
    ds = uni()
    T = ds.sessions[500]
    bars = {s: [b.model_dump() if b.date <= T else {**b.model_dump(), "open": b.open * 1.3, "high": b.high * 1.4, "low": b.low * 0.7, "close": b.close * 0.75}
                for b in rows] for s, rows in ds.bars.items()}
    for rows in bars.values():                                                          # keep OHLC consistent after the rewrite
        for b in rows:
            b["high"], b["low"] = max(b["high"], b["open"], b["close"]), min(b["low"], b["open"], b["close"])
    aux = copy.deepcopy(ds.aux)
    for name in ("raw_close", "raw_volume"):
        for s, m in aux["series"][name].items():
            for d in m:
                if d > T:
                    m[d] *= 0.01
    alt = Dataset(bars, kind=ds.kind, source=ds.source, provenance=ds.declared, aux=aux)
    for cls in (SwingTrendPullbackAdapter, SwingTrendPullbackControlAdapter):
        a, b = dump(cls().candidates(ds)), dump(cls().candidates(alt))
        assert [c for c in a if c["signal_date"] <= T] == [c for c in b if c["signal_date"] <= T] and a != b


def test_L03_the_holding_limit_is_the_configurations_never_a_candidates():
    ds = uni()
    for cls in (SwingTrendPullbackAdapter, SwingTrendPullbackControlAdapter):
        assert all(r.max_hold_bars is None for r in cls().candidates(ds))
        cands, _ = CD.normalize(cls(), cls().candidates(ds), ds, None)
        assert cands and all(c.max_hold_bars == 10 for c in cands)
        assert cls().describe()["max_hold_bars"] == 10
    for p in SP.SENSITIVITY:
        a = SwingTrendPullbackAdapter().with_params(p)
        cands, _ = CD.normalize(a, a.candidates(ds), ds, None)
        assert {c.max_hold_bars for c in cands} == {p.get("max_hold_bars", 10)}
    _, rr = runs()
    for (v, c), run in rr.items():
        assert {t["max_hold_bars"] for t in rec(run)["arms"]["RULES"]["trades"]} == {10}
    for f in sorted((ROOT / "swing_pullback").glob("*.py")):                            # no code path hands a candidate its own holding limit
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "RawCandidate":
                assert "max_hold_bars" not in {k.arg for k in n.keywords}, f.name
    with pytest.raises(ValueError):
        SwingTrendPullbackAdapter({"cooldown_sessions": 5})                             # nothing else is a parameter
    with pytest.raises(ValueError):
        SwingTrendPullbackAdapter({"max_hold_bars": 0})


def test_L04_a_variant_that_peeks_at_the_next_bar_is_caught_by_the_point_in_time_sweep():
    """The harness samples at most five truncation cuts at kept candidates' own dates, and a next-bar peek can pass them (it did on this
    fixture); the sweep of L01 cuts at every 9th session and at candidate dates, and catches it at once."""
    class Peek(SwingTrendPullbackAdapter):
        def candidates(self, dataset):
            for r in super().candidates(dataset):
                nxt = dataset.next_bar(r.symbol, r.signal_date)
                if nxt is None or nxt.close >= r.entry_reference:                     # drops the losers it can see coming
                    yield r
    ds = uni()
    full = dump(SwingTrendPullbackAdapter().candidates(ds))
    cuts = sorted(set(ds.sessions[199::9]) | {c["signal_date"] for c in full[::7]})
    assert divergences(Peek, ds, cuts) and divergences(SwingTrendPullbackAdapter, ds, cuts) == []


def test_L05_the_static_causality_scan_finds_nothing_in_any_package_file():
    for f in sorted((ROOT / "swing_pullback").glob("*.py")):
        assert {r["check"]: r["result"] for r in CA.static_scan(f.read_text(encoding="utf-8"))} == {
            "STATIC_FUTURE_SHIFT": "PASS", "STATIC_CENTERED_WINDOW": "PASS", "STATIC_FUTURE_INDEX": "PASS"}, f.name


def test_L06_an_independent_re_derivation_gives_every_candidate_and_level():
    sets = [uni(), uni_regime(), walks_sip(), walks_iex(), F.dataset(F.universe(4), feed="IEX"), F.dataset([dict(x, raw_mult=0.5, volume=2_000_000.0) for x in F.universe(4)]), ds_of(one(closes_from([0.002 if i < 300 or i >= 340 else -0.0001 for i in range(N)]))),
            ds_of([one(dip(400, "rsi"), sym="GOOG", cik="A"), one(dip(410, "rsi"), sym="GOOGL", cik="A"), one(dip(420, "guard"), sym="ABBV", gaps=[300])])]
    for ds in sets:
        for control in (False, True):
            mine = [(s["symbol"], s["signal_date"], s["stop"], s["target"]) for s in SwingPullbackCore(not control).evaluate(ds)["signals"]]
            ref = reference(ds, control=control)
            assert [m[:2] for m in mine] == [r[:2] for r in ref] and mine
            for m, r in zip(mine, ref):
                assert m[2] == pytest.approx(r[2], rel=1e-9) and m[3] == pytest.approx(r[3], rel=1e-9)
    for c in SwingTrendPullbackAdapter().candidates(uni()):
        assert all(d <= c.signal_date for d in c.feature_as_of.values()) and c.feature_as_of["sma_fast_prior"] < c.signal_date


# ============================================================================================================ the control (C)
def test_C01_c1_describes_exactly_v1_except_its_identity_and_the_pullback_feature():
    a, c = SwingTrendPullbackAdapter().describe(), SwingTrendPullbackControlAdapter().describe()
    diff = {k for k in set(a) | set(c) if a.get(k) != c.get(k)}
    assert diff == {"strategy_id", "critical_features"} and a["strategy_version"] == c["strategy_version"] == "1.0"
    assert set(a["critical_features"]) - set(c["critical_features"]) == {"rsi"} and set(c["critical_features"]) < set(a["critical_features"])
    assert SwingTrendPullbackControlAdapter.__mro__[1] is SwingTrendPullbackAdapter


def test_C02_at_every_symbol_session_v1_passes_exactly_when_c1_passes_and_the_pullback_rule_holds():
    for ds in (uni(), ds_of(one(dip(399, "guard"))), ds_of(one(dip(400, "lower3")))):
        _, t1 = trace(ds)
        _, t0 = trace(ds, control=True)
        assert t1.keys() == t0.keys()
        for key, r1 in t1.items():
            r0 = t0[key]
            assert r1["pullback"] == r0["pullback"]
            assert r1["pre_cooldown"] == (r0["pre_cooldown"] and bool(r0["pullback"])), key
            if not r0["pre_cooldown"]:
                assert r1["reason"] == r0["reason"]                                     # any other failing rule: the same rule for both


def test_C03_the_control_drops_the_depth_guard_too_and_shares_every_level():
    ds = ds_of(one(dip(399, "guard")))                                                   # 399 = 199 + 200: on C1's cooldown grid
    assert reason_at(ds, 399) == "BELOW_DEPTH_GUARD" and reason_at(ds, 399, control=True) == "CANDIDATE"
    ds = ds_of(one(dip(399, "rsi")))
    v1 = {r.signal_date: r for r in SwingTrendPullbackAdapter().candidates(ds)}
    c1 = {r.signal_date: r for r in SwingTrendPullbackControlAdapter().candidates(ds)}
    d = F.SESSIONS[399]
    assert (v1[d].entry_reference, v1[d].invalidation_reference, v1[d].target_reference) == (c1[d].entry_reference, c1[d].invalidation_reference,
                                                                                           c1[d].target_reference)
    assert {k for k in v1[d].features if v1[d].features[k] != c1[d].features[k]} == {"pullback_rule"}


def test_C04_the_control_runs_no_sensitivity_and_v1_runs_the_eight():
    _, rr = runs()
    assert rec(rr[("C1", "BASE")])["diagnostics"]["strategy"]["sensitivity"]["status"] == "NOT_RUN"
    rows = rec(rr[("V1", "BASE")])["diagnostics"]["strategy"]["sensitivity"]["rows"]
    assert [r["params"] for r in rows] == ["base"] + [dict(p) for p in SP.SENSITIVITY]


# ============================================================================================================ the harness (H)
def test_H01_every_trade_enters_at_the_next_open_and_exits_by_stop_target_time_or_data_end():
    ds, rr = runs()
    pos = {d: i for i, d in enumerate(ds.sessions)}
    for (v, c), run in rr.items():
        r = rec(run)
        assert r["audit"]["status"] == "PASS" and not r["causality"]["critical"] and r["eligibility"]["status"] == "ELIGIBLE", (v, c)
        trades = r["arms"]["RULES"]["trades"]
        assert trades and {t["exit_reason"] for t in trades} <= EXITS
        for t in trades:
            assert pos[t["entry_date"]] == pos[t["signal_date"]] + 1 and t["holding_bars"] <= 10
            cand = next(x for x in r["candidates"] if x["candidate_id"] == t["candidate_id"])
            assert t["stop"] == pytest.approx(cand["invalidation_reference"]) and t["target"] == pytest.approx(cand["target_reference"])


def test_H02_the_time_exit_is_the_close_of_the_10th_session_in_the_trade():
    ds = ds_of(one(closes_from(rets(0.0004))))                                          # tiny steady moves: no stop, no target within 10 sessions
    r = rec(PL.run("C1", "BASE", ds, created_at=AT))
    t = r["arms"]["RULES"]["trades"][0]
    i = ds.sessions.index(t["signal_date"])
    assert t["exit_reason"] == "TIME_EXIT" and t["holding_bars"] == 10 and t["exit_date"] == ds.sessions[i + 10]
    assert t["exit_raw"] == pytest.approx(ds.bar("AAPL", ds.sessions[i + 10]).close)


def test_H03_the_store_accepts_the_2x_cost_run_as_the_same_version_and_refuses_a_changed_rule(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, rr = runs()
    store = StrategyValidationStore(tmp_path / "v.db")
    for key in PL.RUNS:
        store.insert(rr[key])                                                           # two cost runs per version: one configuration each
    changed = E.run_validation(SwingTrendPullbackAdapter({"stop_atr": 2.5}), ds, arms=("RULES",), friction=PL.friction("BASE"), created_at=AT)
    with pytest.raises(StoreError, match="new strategy_version"):
        store.insert(changed)


def test_H04_a_run_opens_no_socket_and_no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network or database access during a swing run")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)
    assert PL.run("V1", "STRESS_2X", F.dataset(F.universe(3)), created_at=AT).candidate_count > 0


# ============================================================================================================ diagnostics (D)
def test_D01_the_fold_boundaries_are_six_equal_session_counts_from_the_200th_session():
    for n, sizes in ((205, [1] * 6), (211, [2] * 6), (212, [3, 2, 2, 2, 2, 2]), (216, [3, 3, 3, 3, 3, 2]), (N, None)):
        s = F.SESSIONS[:n]
        b = SPD.fold_bounds(s)
        span = s[199:]
        assert [x["sessions"] for x in b] == (sizes or [x["sessions"] for x in b]) and sum(x["sessions"] for x in b) == len(span)
        assert b[0]["start"] == s[199] and b[-1]["end"] == s[-1] and max(x["sessions"] for x in b) - min(x["sessions"] for x in b) <= 1
        for x, y in zip(b, b[1:]):
            assert s.index(y["start"]) == s.index(x["end"]) + 1
    assert SPD.fold_bounds(F.SESSIONS[:204]) is None


def test_D02_folds_assign_every_trade_by_signal_date_with_the_same_boundaries_for_v1_and_c1():
    ds, rr = runs()
    f1, f0 = (rec(rr[(v, "BASE")])["diagnostics"]["strategy"]["folds"] for v in ("V1", "C1"))
    strip = lambda f: [(x["fold"], x["start"], x["end"], x["sessions"]) for x in f["folds"]]  # noqa: E731
    assert strip(f1) == strip(f0) == [(b["fold"], b["start"], b["end"], b["sessions"]) for b in SPD.fold_bounds(ds.sessions)]
    for v, f in (("V1", f1), ("C1", f0)):
        trades = rec(rr[(v, "BASE")])["arms"]["RULES"]["trades"]
        assert f["trades_in_folds"] == f["trades_total"] == len(trades)
        for x in f["folds"]:
            inside = [t for t in trades if x["start"] <= t["signal_date"] <= x["end"]]
            assert x["trades"] == len(inside) and (x["positive"] is None if not inside else x["positive"] == (sum(t["net_pnl"] for t in inside) > 0))
        assert f["positive_fold_pct"] == pytest.approx(f["positive_folds"] / f["folds_with_trades"], abs=1e-4)


def test_D03_the_sensitivity_reads_the_train_view_only_and_never_changes_the_configuration():
    ds, rr = runs()
    r = rec(rr[("V1", "BASE")])
    T = r["train_end"]
    bars = {s: [b.model_dump() if b.date <= T else {**b.model_dump(), "close": b.close * 0.9, "low": min(b.low, b.close * 0.9)} for b in rows]
            for s, rows in ds.bars.items()}
    alt = Dataset(bars, kind=ds.kind, source=ds.source, provenance=ds.declared, aux=ds.aux)
    a = SwingTrendPullbackAdapter()
    again = SPD.sensitivity(r, alt, a)
    assert again["rows"] == r["diagnostics"]["strategy"]["sensitivity"]["rows"] and again["label"] in ("ROBUST", "SENSITIVE", "UNSTABLE")
    assert a.params == {} and a.max_hold_bars == 10 and r["config_snapshot"]["adapter"]["params"] == {}
    assert [x["max_hold_bars"] for x in again["rows"]] == [10, 10, 10, 10, 10, 10, 10, 8, 12]


def test_D04_the_diagnostics_never_change_the_verdict_or_any_metric(monkeypatch):
    ds = uni()
    with_d = rec(PL.run("V1", "BASE", ds, created_at=AT))
    monkeypatch.setattr(SwingTrendPullbackAdapter, "strategy_diagnostics", None)
    without = rec(PL.run("V1", "BASE", ds, created_at=AT))
    for k in ("verdict", "performance_verdict", "candidate_set_hash"):
        assert with_d[k] == without[k], k
    assert with_d["arms"]["RULES"]["metrics"] == without["arms"]["RULES"]["metrics"] and "strategy" not in without["diagnostics"]


def test_D05_the_sensitivity_purges_exactly_the_windows_past_the_train_end_and_runs_at_the_runs_own_friction():
    ds, rr = runs()
    base, stress = (rec(rr[("V1", c)]) for c in ("BASE", "STRESS_2X"))
    T = base["train_end"]
    tv = ds.until(T)
    for row in base["diagnostics"]["strategy"]["sensitivity"]["rows"]:
        a = SwingTrendPullbackAdapter().with_params({} if row["params"] == "base" else row["params"])
        cands, _ = CD.normalize(a, a.candidates(tv), tv, None)
        past = [c for c in cands if any(b.date > T for b in ds.bars_after(c.symbol, c.signal_date)[:a.max_hold_bars])]   # the symbol's own bars, dates only
        assert row["candidates"] == len(cands) and row["boundary_purged"] == len(past) > 0, row["params"]
        assert row["trades"] <= len(cands) - len(past)
    m = base["arms"]["RULES"]["metrics"]["TRAIN"]                                                   # the base row IS the main run's TRAIN result
    b0 = base["diagnostics"]["strategy"]["sensitivity"]["rows"][0]
    assert (b0["trades"], b0["boundary_purged"], b0["expectancy_R"], b0["net_pnl"]) == (m["trade_count"], m["boundary_purged_count"], m["expectancy_R"], m["net_pnl"])
    pairs = zip(base["diagnostics"]["strategy"]["sensitivity"]["rows"], stress["diagnostics"]["strategy"]["sensitivity"]["rows"])
    for b, s in pairs:                                                                              # the 2x run's sensitivity pays 2x friction
        assert b["params"] == s["params"] and b["trades"] == s["trades"] and s["net_pnl"] < b["net_pnl"], b["params"]


def test_D06_a_fold_is_positive_by_its_net_pnl_not_by_its_mean_r():
    ds = F.dataset([one(closes_from(rets()))])
    d = ds.sessions[199]
    record = {"arms": {"RULES": {"trades": [{"signal_date": d, "r_net": 1.0, "net_pnl": 10.0}, {"signal_date": d, "r_net": -0.5, "net_pnl": -100.0}]}},
              "candidates": []}
    f = SPD.folds(record, ds)
    assert f["folds"][0]["trades"] == 2 and f["folds"][0]["mean_R"] == 0.25 and f["folds"][0]["positive"] is False
    assert f["folds_with_trades"] == 1 and f["positive_folds"] == 0 and f["positive_fold_pct"] == 0.0
    assert all(x["positive"] is None and x["trades"] == 0 for x in f["folds"][1:])


def test_D07_a_gap_after_a_train_signal_is_purged_exactly_as_the_harness_purges_it():
    ds = ds_of(one(dip(530, "rsi"), gaps=range(531, 541)))                                       # the next 10 bars of 530 end after the TRAIN end (547)
    r = rec(PL.run("V1", "BASE", ds, created_at=AT))
    T = r["train_end"]
    assert 530 + 10 < F.SESSIONS.index(T)                                                      # a session count would have kept it
    assert any(b.date > T for b in ds.bars_after("AAPL", F.SESSIONS[530])[:10])                # its own next 10 bars pass the TRAIN end
    m = r["arms"]["RULES"]["metrics"]["TRAIN"]
    b0 = r["diagnostics"]["strategy"]["sensitivity"]["rows"][0]
    assert (b0["trades"], b0["boundary_purged"], b0["expectancy_R"]) == (m["trade_count"], m["boundary_purged_count"], m["expectancy_R"])
    sig = next(c for c in r["candidates"] if c["signal_date"] == F.SESSIONS[530])
    assert sig["candidate_id"] in r["boundary"]["purged"]["RULES"]                               # the harness purged it ...
    assert b0["boundary_purged"] >= 1                                                           # ... and so did the sensitivity


def test_D08_every_sensitivity_row_equals_a_full_harness_run_of_its_configuration_at_the_runs_cost(monkeypatch):
    ds = sizing_mix()
    runs_ = {c: rec(PL.run("V1", c, ds, created_at=AT)) for c in SP.COSTS}
    monkeypatch.setattr(SwingTrendPullbackAdapter, "strategy_diagnostics", None)            # the comparison runs need no diagnostics of their own
    for cost, r in runs_.items():
        rows = r["diagnostics"]["strategy"]["sensitivity"]["rows"]
        assert [x["params"] for x in rows] == ["base"] + [dict(p) for p in SP.SENSITIVITY]
        for row in rows:
            params = {} if row["params"] == "base" else row["params"]
            full = rec(E.run_validation(SwingTrendPullbackAdapter(params), ds, arms=("RULES",), friction=PL.friction(cost), created_at=AT))
            m = full["arms"]["RULES"]["metrics"]["TRAIN"]
            got = (row["candidates"], row["trades"], row["boundary_purged"], row["expectancy_R"], row["net_pnl"], row["mean_R_t_stat"])
            want = (m["candidate_count"], m["trade_count"], m["boundary_purged_count"], m["expectancy_R"], m["net_pnl"], m["mean_R_t_stat"])
            assert got == want, (cost, row["params"], got, want)
            assert full["friction_model"] == PL.friction(cost) and full["config_snapshot"]["sizing"] == r["config_snapshot"]["sizing"]
            tr = [t for t in full["arms"]["RULES"]["trades"] if t["signal_date"] <= full["train_end"]]
            assert {t["size_status"] for t in tr} == {"CAPPED", "RISK_SIZED", "MIN_ONE_UNIT"}, (cost, row["params"])   # every sizing branch counts
            assert {"TIME_EXIT", "STOP"} <= {t["exit_reason"] for t in tr}, (cost, row["params"])                        # the time-exit fill counts


# ============================================================================================================ the experiments (P)
def test_P01_exactly_two_cost_runs_per_version_and_nothing_else():
    assert PL.RUNS == (("V1", "BASE"), ("V1", "STRESS_2X"), ("C1", "BASE"), ("C1", "STRESS_2X"))
    assert PL.friction("BASE") == {"slippage_bps_per_side": 5.0, "commission_per_order": 0.0, "fee_per_share": 0.0}
    assert PL.friction("STRESS_2X")["slippage_bps_per_side"] == 10.0
    for bad in ("STRESS_3X", "base", "", None):
        with pytest.raises(ValueError):
            PL.friction(bad)
    with pytest.raises(ValueError):
        PL.adapter("V2")                                                                 # V2 is deferred, not implemented
    _, rr = runs()
    for (v, c), run in rr.items():
        r = rec(run)
        assert r["friction_model"] == PL.friction(c) and r["arms_run"] == ["RULES"]
        assert r["diagnostics"]["strategy"]["costs"]["preregistered_run"] == c and r["config_snapshot"]["split"]["train_ratio"] == 0.7


# ============================================================================================================ isolation (X)
ALLOWED_IMPORTS = ("__future__", "hashlib", "json", "math", "statistics", "typing", "swing_pullback", "strategy_validation")


def test_X01_the_package_imports_nothing_but_the_harness_and_the_standard_library():
    for f in sorted((ROOT / "swing_pullback").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                         for a in n.names}
        assert not {m for m in mods if not any(m == q or m.startswith(q + ".") for q in ALLOWED_IMPORTS)}, (f.name, mods)
        assert not {m for m in mods if m.startswith("strategy_validation.") and m.split(".")[1] in ("store", "service", "cli")}, f.name   # never persists


def test_X02_nothing_imports_the_package_it_is_not_registered_and_no_setting_names_it():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("swing_pullback/", "swing_eval/", "tests/", ".venv/", "scratchpad/", ".claude/")) or "/site-packages/" in rel:
            continue                                                                     # swing_eval/: the Stage 7.8B-1 offline evaluator
        assert "swing_pullback" not in p.read_text(encoding="utf-8", errors="replace"), rel
    for rel in ("strategy_validation/adapters.py", "strategy_registry/registry.py", "config.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "SWING_TREND_PULLBACK" not in text and "swing_pullback" not in text, rel


# ============================================================================================================ the document (G)
def test_G01_the_design_document_states_the_baseline_its_deviations_and_the_limits():
    doc = (ROOT / "DESIGN_78_SWING_PULLBACK.md").read_text(encoding="utf-8")
    for s in ("modified D.3 research baseline", "not a faithful implementation", "D1", "D2", "D3", "D4", "D5", "EARNINGS_FILTER_NOT_APPLIED",
              "V2", "deferred", "EMA / support", "9 sessions", "every sensitivity neighbour", "trigger and the depth guard", "raw close", "one listing per issuer",
              "200th session", "two cost runs", "TRAIN view", "Synthetic fixtures only", SP.preregistration_hash()):
        assert s in doc, s



# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_78}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_78} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #48 (Stage 7.8 merged; the Stage 7.8B-1 base): what Stage 7.8 left, whatever a later stage changed since."""
    run = subprocess.run(["git", "show", f"{FL.BASE_78B1}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_78B1} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


NEW_MODULES_78 = ("__init__.py", "adapter.py", "core.py", "data.py", "diagnostics.py", "indicators.py", "plan.py")
APP_EDITS_78 = {".github/ci/pytest_groups.json"}
FROZEN_DIRS_78 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                  "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                  "database", "data", "value_quality", "market_intel", "market_scan", "scanner", "strategy_registry", "strategy_validation", "analysis",
                  "momentum", "mean_reversion", "breakout", "rotation_walkforward", "backtest", "paper")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_78 == "d1de7514d551f9eb261f60117fea58586af89a9b" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_78}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_78).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    stage = set(_git("diff", "--name-only", FL.BASE_78, FL.BASE_78B1).split())               # Stage 7.8 itself, between two fixed commits
    assert stage == FL.NEW_78 | FL.EDITED_78, {"unexpected": sorted(stage - FL.NEW_78 - FL.EDITED_78), "missing": sorted((FL.NEW_78 | FL.EDITED_78) - stage)}
    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                     # Stage 7.8B-1, pinned by its own F tests
    assert changed == FL.NEW_78 | FL.EDITED_78 | later, {"unexpected": sorted(changed - FL.NEW_78 - FL.EDITED_78 - later),
                                                         "missing": sorted((FL.NEW_78 | FL.EDITED_78 | later) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_78).split())
    assert {p for p in stage if p not in in_base} == FL.NEW_78 and {p for p in stage if p in in_base} == FL.EDITED_78
    assert FL.NEW_78 == ({"DESIGN_78_SWING_PULLBACK.md", "tests/test_swing_pullback_78.py", "tests/swing_pullback_fixtures.py"}
                         | {f"swing_pullback/{m}" for m in NEW_MODULES_78})
    assert {p for p in FL.EDITED_78 if not p.startswith("tests/")} == APP_EDITS_78
    assert FL.SEMANTIC_78 == {} and FL.INSTRUMENTATION_78 == {}
    for d in FROZEN_DIRS_78:                                                                   # every existing module: the harness and the scanner included
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert post(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "watchlist.txt",
                "STRATEGY_STANDARD_V1.md", "STRATEGY_VALIDATION_HARNESS.md", "main.py", ".github/workflows/tests.yml"):
        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook, dependency or workflow edit
    for rel in sorted(FL.NEW_78 | FL.EDITED_78):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in post(rel) and m54 not in post(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_a_strategy():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import config
    import execution as X
    from dynamic_universe import plan as DUP
    from market_intel.settings import settings as mi_settings
    from strategy_registry import registry as RG
    from strategy_validation import adapters as AD
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO") and mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert DUP.settings()["mode"] == "OFF"
    assert not [n for n in dir(config) if "SWING_PULLBACK" in n or "SWING_TREND" in n]                       # no new key
    assert set(AD.ADAPTERS) == {"DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"}   # nothing registered
    assert not [s for s in RG.by_id() if "SWING" in s]


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_78.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_78) == FL.EDITED_78 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.8 Part B (the swing trend pullback")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_74 + NEW_75 + NEW_78 plus this ledger.", " + NEW_74 + NEW_75 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_78 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_78: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.8 left
    assert set(new_led) == set(old)                                                         # no new ledger key
    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_78 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_78 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n',
         '    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    assert stage == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(stage - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - stage)}\n    later = FL.NEW_75 | FL.EDITED_75                                                         # Stage 7.5, pinned by its own F tests\n    assert changed == FL.NEW_74 | FL.EDITED_74 | later, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74 - later),\n',
         '    assert stage == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(stage - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - stage)}\n    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.5, pinned by its own F tests\n    assert changed == FL.NEW_74 | FL.EDITED_74 | later, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74 - later),\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n',
         '    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n',
         '    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n',
         '    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('\nAPP_EDITS_75 = {".github/ci/pytest_groups.json"}\n',
         '\ndef post(rel):\n    """main after PR #47 (Stage 7.5 merged; the Stage 7.8 base): what Stage 7.5 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_78}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_78} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_75 = {".github/ci/pytest_groups.json"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_75 | FL.EDITED_75, {"unexpected": sorted(changed - FL.NEW_75 - FL.EDITED_75), "missing": sorted((FL.NEW_75 | FL.EDITED_75) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_75).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_75, FL.BASE_78).split())                 # Stage 7.5 itself, between two fixed commits\n    assert stage == FL.NEW_75 | FL.EDITED_75, {"unexpected": sorted(stage - FL.NEW_75 - FL.EDITED_75), "missing": sorted((FL.NEW_75 | FL.EDITED_75) - stage)}\n    later = FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.8, pinned by its own F tests\n    assert changed == FL.NEW_75 | FL.EDITED_75 | later, {"unexpected": sorted(changed - FL.NEW_75 - FL.EDITED_75 - later),\n                                                         "missing": sorted((FL.NEW_75 | FL.EDITED_75 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_75).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_75).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_75 and {p for p in changed if p in in_base} == FL.EDITED_75\n    assert FL.NEW_75 == {"DESIGN_75_MARKET_SCAN.md", "tests/test_market_scan_75.py"} | {f"market_scan/{m}" for m in NEW_MODULES_75}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_75).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_75 and {p for p in stage if p in in_base} == FL.EDITED_75\n    assert FL.NEW_75 == {"DESIGN_75_MARKET_SCAN.md", "tests/test_market_scan_75.py"} | {f"market_scan/{m}" for m in NEW_MODULES_75}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert now_text(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "watchlist.txt",\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert post(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "watchlist.txt",\n'),
        ('                "STRATEGY_STANDARD_V1.md", "main.py"):\n        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook, dependency or universe edit\n    for rel in sorted(FL.NEW_75 | FL.EDITED_75):\n',
         '                "STRATEGY_STANDARD_V1.md", "main.py"):\n        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook, dependency or universe edit\n    for rel in sorted(FL.NEW_75 | FL.EDITED_75):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_75.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_75.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_75) == FL.EDITED_75 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.5 (the shared market scanner")\n',
         '    assert set(REPLAY_75) == FL.EDITED_75 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.5 (the shared market scanner")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_75 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.5 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_75 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
