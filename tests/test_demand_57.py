"""
Stage 5.7 — MARKET DEMAND / BUYER CONFIRMATION (deterministic; bars only; separate from PPE). Focused tests, numbered after the stage checklist:
  E01–E24 engine · L1–L5 no lookahead · P1–P7 adapters / provider / stream / route · S1–S9 safety · U1–U6 UI · B1–B2 base pins
"""
import ast
import inspect
import json
import random
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import demand  # noqa: E402
from demand import BANDS, DISCLAIMER, MIN_COVERAGE_WEIGHT, MIN_SESSION_BARS, NOT_A_SIGNAL, WEIGHTS, demand_label  # noqa: E402
from demand.components import Ctx, relative_volume  # noqa: E402
from demand.engine import build_ctx, compute_demand  # noqa: E402
from demand.inputs import BenchmarkInputs, DemandInputs, MinuteBar, completed, from_candles  # noqa: E402
from demand.models import DemandResult  # noqa: E402
from demand.replay import _session, replay_daily, summarize_daily, synthetic_scenarios  # noqa: E402
from demand.session import expected_cumulative_volume, vwap_series  # noqa: E402
from ppe.inputs import DailyBar  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from trader_view import MESSAGE_TYPES, SYMBOL_BOUND_TYPES  # noqa: E402
from trader_view.adapters import NullPositionAdapter, NullResearchAdapter  # noqa: E402
from trader_view.demand_provider import DemandProvider  # noqa: E402
from trader_view.hub import accepts_message  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from test_trader_view_55 import FORBIDDEN_IMPORTS, T0, WL, FakePositions, FakeResearch, burst, hello_of, marker  # noqa: E402

BASE = "4cd3408"                                                                        # origin/main the stage was branched from (Stage 5.6.1 merged)
NOW = 1_800_000_000
T = date(2027, 1, 15)
JS = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
RENDER = (ROOT / "frontend" / "trader_render.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend" / "trader_core.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "trader_view.css").read_text(encoding="utf-8")
DEMAND_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "demand").glob("*.py")) + ["trader_view/demand_provider.py"]


def demand_source() -> str:
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in DEMAND_FILES)


# ---------------------------------------------------------------- builders ------------------------------------------------------

def prior_sessions(n=3, bars=390, vol=1000.0):
    return {f"p{k}": _session(NOW - (k + 1) * 86400 - 400 * 60, [100.0] * bars, [vol] * bars) for k in range(n)}


def session(closes, vols=None, spread=0.2):
    return _session(NOW - (len(closes) + 1) * 60, list(closes), list(vols or [1000.0] * len(closes)), spread)


def bdays(n, end=T - timedelta(days=1)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def daily(n=60, step=0.0, vol=1e6, last=None, seed=1):
    rng = random.Random(seed)
    bars, px = [], 100.0
    for d in bdays(n):
        o = px
        px = round(px * (1 + step + rng.gauss(0, 0.002)), 4)
        h, lo = max(o, px) * 1.003, min(o, px) * 0.997
        bars.append(DailyBar(date=d, open=o, high=h, low=lo, close=px, volume=vol))
    if last:
        bars[-1] = DailyBar(date=bars[-1].date, **last)
    return bars


def bench(daily_bars=None, session_return=None, kind="SECTOR_ETF", label="SOXX (Semiconductors ETF)"):
    return BenchmarkInputs(symbol="SOXX", kind=kind, label=label, daily=daily_bars if daily_bars is not None else daily(60, seed=2), session_return=session_return)


def inputs(closes, vols=None, *, price=None, prev_close=100.0, benchmark=None, daily_bars=None, prior=None, mode="real", **kw):
    bars = session(closes, vols)
    return DemandInputs(symbol="SYN", now=NOW, session_date=T, session_bars=bars, prior_sessions=prior if prior is not None else prior_sessions(),
                        daily_bars=daily_bars if daily_bars is not None else daily(), current_price=price if price is not None else closes[-1], prev_close=prev_close,
                        session_open=closes[0], benchmark=benchmark, mode=mode, market_data="synthetic", price_source="synthetic", **kw)


def daily_inputs(daily_bars, benchmark=None, **kw):
    """DAILY_MODE inputs: no session bars at all (the live fallback when the current session has fewer than MIN_SESSION_BARS completed bars)."""
    return DemandInputs(symbol="SYN", now=NOW, session_date=T, session_bars=[], prior_sessions={}, daily_bars=daily_bars, current_price=None, prev_close=None,
                        benchmark=benchmark, mode="real", market_data="synthetic", price_source="synthetic", **kw)


def run(inp):
    return compute_demand(inp, now_fn=lambda: NOW)


RISING = [100.0 + 0.02 * i for i in range(90)]
FALLING = [100.0 - 0.02 * i for i in range(90)]
FLAT = [100.0 + (0.01 if i % 2 else -0.01) for i in range(90)]


# ---------------------------------------------------------------- E engine ------------------------------------------------------

def test_E01_neutral_participation_scores_near_neutral():
    r = run(inputs(FLAT, benchmark=bench(session_return=0.0)))
    assert r.mode == "INTRADAY_MODE" and r.status == "AVAILABLE" and 42 <= r.score <= 58 and r.label == "MIXED" and r.coverage_weight == 1.0
    assert all(40 <= c.score <= 60 for c in r.components.values()), {k: c.score for k, c in r.components.items()}


def test_E02_weak_participation_below_vwap_is_low_demand():
    r = run(inputs(FALLING, [500.0] * 90))
    c = r.components
    assert r.score < 35 and c["relative_volume"].score < 50 and c["vwap_acceptance"].score < 30 and c["relative_volume"].raw["relative_volume"] < 0.6
    assert "below VWAP" in c["vwap_acceptance"].reason


def test_E03_high_participation_with_sustained_vwap_hold_is_high_demand():
    r = run(inputs(RISING, [1800.0] * 90, benchmark=bench(session_return=0.0)))
    c = r.components
    assert r.score >= 75 and r.label in ("STRONG", "VERY_STRONG") and c["vwap_acceptance"].score >= 85 and c["vwap_acceptance"].raw["hold_bars"] == 10
    assert c["relative_volume"].score >= 70 and "holding above VWAP" in c["vwap_acceptance"].reason


def test_E04_volume_spike_without_price_acceptance_cannot_produce_extreme_demand():
    r = run(inputs(FALLING, [3000.0] * 90))
    assert r.score < 50 and r.components["relative_volume"].score <= 50 and r.label in ("VERY_WEAK", "WEAK", "MIXED")
    # the participation component alone is bounded to [20, 80]; with every other component neutral the total cannot leave MIXED
    ctx = build_ctx(inputs(FLAT))
    scores = []
    for rv in (0.0, 0.3, 0.6, 1.0, 1.5, 2.0, 5.0):
        for d in (-1, 0, 1):
            ctx.price = ctx.vwaps[-1] + d * 0.5
            pr = {k: [MinuteBar(b.time, b.open, b.high, b.low, b.close, 1000.0 / rv if rv else 1e9, True) for b in v] for k, v in prior_sessions().items()}
            inp = inputs(FLAT, prior=pr)
            scores.append(relative_volume(inp, ctx).score)
    assert all(20.0 <= s <= 80.0 for s in scores if s is not None)
    assert WEIGHTS["relative_volume"] * 80 + (1 - WEIGHTS["relative_volume"]) * 50 == 56.0 < 60


def test_E05_brief_vwap_cross_without_persistence_stays_moderate():
    scen = synthetic_scenarios()
    brief, held = run(scen["brief_vwap_cross"]), run(scen["reclaim_held"])
    b = brief.components["vwap_acceptance"]
    assert b.raw["hold_bars"] <= 2 and b.score < 60 and brief.score < 60
    assert held.components["vwap_acceptance"].raw["reclaim"] == 1 and held.score >= brief.score + 15
    # the design example: one tick above VWAP after a session below it is moderate, never high
    one_tick = inputs([100.0 - 0.01 * i for i in range(89)] + [99.5], price=99.5)
    assert run(one_tick).components["vwap_acceptance"].score < 55


def test_E06_higher_low_higher_high_structure_raises_the_structure_component():
    c = run(inputs(RISING)).components["intraday_structure"]
    assert c.score >= 75 and c.raw["higher_lows"] == 1 and c.raw["higher_highs"] == 1 and "higher lows and higher highs" in c.reason


def test_E07_lower_low_structure_lowers_the_structure_component():
    c = run(inputs(FALLING)).components["intraday_structure"]
    assert c.score <= 25 and c.raw["higher_lows"] == -1 and c.raw["higher_highs"] == -1 and "lower lows" in c.reason


def test_E08_strong_close_near_range_high_raises_range_strength():
    c = run(inputs(RISING)).components["range_strength"]
    assert c.score >= 85 and c.raw["close_location"] >= 0.85 and not c.raw["session_complete"] and "top" in c.reason
    d = run(daily_inputs(daily(60, last=dict(open=100, high=104, low=99, close=103.8, volume=1e6)))).components["range_strength"]
    assert d.score >= 90 and d.raw["session_complete"] and d.raw["basis"] == "last completed session"      # DAILY_MODE: the last completed session


def test_E09_close_near_range_low_lowers_range_strength():
    c = run(inputs(FALLING)).components["range_strength"]
    assert c.score <= 15 and c.raw["close_location"] <= 0.15 and "bottom" in c.reason
    down_near_high = run(inputs([100.0 - 0.02 * i for i in range(80)] + [98.6 + 0.1 * j for j in range(10)], price=99.55)).components["range_strength"]
    assert down_near_high.raw["down_move_cap_applied"] and down_near_high.score <= 60               # a down session closing near its high is at most mild


def test_E10_stock_outperforming_sector_raises_relative_strength():
    c = run(inputs(RISING, benchmark=bench(session_return=-0.01))).components["relative_strength"]
    assert c.score > 60 and c.raw["horizons"]["session"] > 0 and "vs SOXX" in c.reason
    stock = daily(60, step=0.004, seed=3)
    d = run(daily_inputs(stock, benchmark=bench(daily(60, step=-0.002, seed=4)))).components["relative_strength"]
    assert d.score > 60 and set(d.raw["horizons"]) == {"1d", "5d"}                                   # DAILY_MODE horizons


def test_E11_stock_lagging_sector_lowers_relative_strength():
    c = run(inputs(FALLING, benchmark=bench(session_return=0.01))).components["relative_strength"]
    assert c.score < 40 and c.raw["horizons"]["session"] < 0 and "lagging SOXX" in c.reason
    fallback = run(inputs(FLAT, benchmark=bench(session_return=0.0, kind="MARKET", label="SPY (market fallback)")))
    assert fallback.sources.benchmark_kind == "MARKET" and fallback.sources.benchmark_label == "SPY (market fallback)"   # the fallback is never hidden


def test_E12_follow_through_increases_the_score():
    c = run(inputs(RISING)).components["follow_through"]
    assert c.score >= 85 and c.raw["streak"] == 10 and c.raw["higher_low_kept"] == 1 and c.raw["follow_through_fraction"] == 1.0
    d = run(daily_inputs(daily(60, step=0.01, seed=5))).components["follow_through"]
    assert d.score >= 80 and d.raw["streak"] >= 3 and "consecutive up sessions" in d.reason


def test_E13_one_bar_bounce_does_not_over_score():
    r = run(synthetic_scenarios()["one_bar_bounce"])
    c = r.components["follow_through"]
    assert c.raw["side"] == -1 and c.score <= 40 and r.score < 50 and r.components["vwap_acceptance"].score < 30     # the bounce did not even reach VWAP
    first_bar_above = inputs([100.0 + (0.01 if i % 2 else -0.01) for i in range(89)] + [100.25], price=100.25)       # flat session, one close just above VWAP
    f = run(first_bar_above).components["follow_through"]
    assert f.raw["side"] == 1 and f.raw["streak"] == 1 and f.score <= 60 and "one-bar bounce" in f.reason


def test_E14_missing_vwap_is_handled():
    r = run(inputs(RISING, [0.0] * 90, benchmark=bench(session_return=0.0)))
    c = r.components
    assert c["vwap_acceptance"].status == "NOT_AVAILABLE" and c["vwap_acceptance"].code == "ZERO_VOLUME"
    assert c["relative_volume"].status == "NOT_AVAILABLE" and c["relative_volume"].code == "ZERO_VOLUME"
    assert r.coverage_weight == 0.6 and r.score is not None and c["follow_through"].raw["reference"] == "the open"   # follow-through falls back to the session open
    nop = run(inputs(RISING, prior={}))
    assert nop.components["relative_volume"].code == "NO_VOLUME_PROFILE" and nop.coverage_weight == 0.65


def test_E15_missing_volume_is_handled():
    bars = daily(60)
    bars = [DailyBar(date=b.date, open=b.open, high=b.high, low=b.low, close=b.close, volume=None) for b in bars]
    r = run(daily_inputs(bars, benchmark=bench(daily(60, seed=2))))
    c = r.components["relative_volume"]
    assert r.mode == "DAILY_MODE" and c.status == "NOT_AVAILABLE" and c.code == "NO_VOLUME"
    assert r.coverage_weight == 0.4 and r.status == "INSUFFICIENT_DATA" and r.score is None and r.label is None


def test_E16_missing_sector_is_handled():
    r = run(inputs(RISING))
    c = r.components["relative_strength"]
    assert c.status == "NOT_AVAILABLE" and c.code == "NO_BENCHMARK" and r.coverage_weight == 0.85 and r.sources.benchmark_kind == "NONE"
    r2 = run(inputs(RISING, benchmark=bench(daily_bars=[], session_return=None)))
    assert r2.components["relative_strength"].code == "BENCHMARK_NOT_ALIGNED"


def test_E17_partial_coverage_renormalizes():
    r = run(inputs(RISING))                                                                 # E missing: 0.85 of the weight
    avail = [c for c in r.components.values() if c.status == "AVAILABLE"]
    expect = round(sum(c.weight * c.score for c in avail) / sum(c.weight for c in avail))
    assert r.score == expect and abs(sum(c.weight for c in avail) - r.coverage_weight) < 1e-9
    assert r.score != round(sum(c.weight * c.score for c in avail))                            # not the un-renormalised sum


def test_E18_below_minimum_coverage_gives_null_score():
    r = run(DemandInputs(symbol="SYN", now=NOW, session_date=T, session_bars=[], prior_sessions={}, daily_bars=daily(10), current_price=None, mode="real"))
    assert r.mode == "DAILY_MODE" and r.status == "INSUFFICIENT_DATA" and r.score is None and r.label is None and r.coverage_weight < MIN_COVERAGE_WEIGHT
    assert "floor" in r.note and demand_label(None) is None


def test_E19_bounds_always_within_0_100():
    rng = random.Random(7)
    for trial in range(40):
        n = rng.choice([35, 60, 120, 200])
        closes, px = [], 100.0
        for _ in range(n):
            px = max(0.5, px * (1 + rng.gauss(0, 0.004)))
            closes.append(round(px, 3))
        vols = [rng.choice([0.0, 50.0, 1000.0, 5000.0]) for _ in range(n)]
        r = run(inputs(closes, vols, benchmark=bench(session_return=rng.gauss(0, 0.01)) if trial % 2 else None, daily_bars=daily(rng.choice([5, 25, 60]), step=rng.gauss(0, 0.003), seed=trial)))
        assert r.score is None or 0 <= r.score <= 100
        for c in r.components.values():
            assert c.score is None or 0.0 <= c.score <= 100.0, (c.key, c.score)
        assert r.label == demand_label(r.score)


def test_E20_deterministic_repeatability():
    a, b = run(inputs(RISING, benchmark=bench(session_return=0.002))), run(inputs(RISING, benchmark=bench(session_return=0.002)))
    assert a.model_dump() == b.model_dump() and a.model_dump_json() == b.model_dump_json()
    for name, inp in synthetic_scenarios().items():
        assert run(inp).model_dump() == run(inp).model_dump(), name


def test_E21_no_nan_or_infinity():
    rng = random.Random(3)
    cases = [inputs([100.0] * 90, [0.0] * 90), inputs([100.0] * 90), inputs([1e-3 + 1e-6 * i for i in range(90)]), inputs(RISING, [1e12] * 90),
             inputs(FLAT, daily_bars=[DailyBar(date=d, open=100, high=100, low=100, close=100, volume=0.0) for d in bdays(60)], prior={}, price=None)]
    for _ in range(10):
        cases.append(inputs([round(100 * (1 + rng.gauss(0, 0.05)), 2) for _ in range(90)], [abs(rng.gauss(0, 1e4)) for _ in range(90)]))
    for inp in cases:
        r = run(inp)
        json.dumps(r.model_dump(), allow_nan=False)                                             # raises on NaN / Infinity
        assert r.score is None or isinstance(r.score, int)


def test_E22_stable_serialization():
    r = run(inputs(RISING, benchmark=bench(session_return=0.001)))
    d = json.loads(r.model_dump_json())
    assert set(d) == {"version", "symbol", "as_of", "status", "mode", "score", "label", "coverage_weight", "components", "key_factors", "sources", "weights", "disclaimer", "note"}
    assert set(d["sources"]) == {"mode", "market_data", "price_source", "benchmark", "benchmark_kind", "benchmark_label", "bars_through", "session", "computed_at"}
    assert set(d["sources"]["session"]) == {"date", "complete", "bars_completed", "open", "high", "low", "vwap", "prior_sessions"}
    assert set(d["components"]) == set(WEIGHTS) and all(set(c) == {"key", "label", "weight", "status", "score", "raw", "reason", "code"} for c in d["components"].values())
    assert DemandResult.model_validate_json(r.model_dump_json()) == r and d["version"] == "demand_v1" and d["disclaimer"] == DISCLAIMER
    with pytest.raises(Exception):
        DemandResult.model_validate({**d, "probability_of_rise": 0.5})                            # extra = forbid


def test_E23_incomplete_session_is_labelled():
    live = run(inputs(RISING))
    assert live.sources.session.complete is False and live.components["range_strength"].raw["session_complete"] is False and "so far" in live.components["range_strength"].reason
    assert live.sources.session.bars_completed == 90 and live.sources.session.vwap is not None and live.sources.bars_through.endswith("+00:00")
    done = run(inputs(RISING, session_complete=True))
    assert done.sources.session.complete is True and done.components["range_strength"].raw["basis"] == "completed session"
    live_bar = MinuteBar(time=NOW - 30, open=101.8, high=102.5, low=101.7, close=102.4, volume=500.0, final=False)
    with_live = inputs(RISING)
    with_live.session_bars = with_live.session_bars + [live_bar]
    assert run(with_live).sources.session.bars_completed == 90                                     # the live bar is never counted


def test_E24_simulator_results_are_labelled():
    m = SimulatedMarket(seed=11, start_time=T0)
    prov = DemandProvider(m, now_fn=lambda: float(T0))
    r = prov.result("NVDA")
    assert r.status == "SIMULATED_DATA" and r.sources.mode == "simulated" and r.note.startswith("SIMULATED DATA") and "simulator" in r.sources.market_data
    assert r.mode == "INTRADAY_MODE" and r.score is not None and r.sources.session.prior_sessions == config.DEMAND_PRIOR_SESSIONS


# ---------------------------------------------------------------- L no lookahead ---------------------------------------------

def test_L1_score_at_T_only_uses_bars_closed_by_T():
    base = inputs(RISING)
    future = inputs(RISING)
    future.session_bars = future.session_bars + [MinuteBar(time=NOW + 60 * k, open=90.0, high=91.0, low=80.0, close=80.0, volume=9e6, final=True) for k in range(1, 40)]
    assert run(base).model_dump() == run(future).model_dump()
    assert len(completed(future.session_bars, NOW)) == 90


def test_L2_future_daily_bars_do_not_change_the_score():
    stock = daily(60)
    base = daily_inputs(stock, benchmark=bench(daily(60, seed=2)))
    fut = daily_inputs(stock + [DailyBar(date=T + timedelta(days=k), open=10, high=200, low=1, close=150, volume=9e9) for k in range(0, 10)], benchmark=bench(daily(60, seed=2)))
    assert run(base).model_dump() == run(fut).model_dump() and run(base).mode == "DAILY_MODE"
    intraday_base, intraday_fut = inputs(RISING, daily_bars=stock, benchmark=bench(daily(60, seed=2), session_return=0.0)), inputs(RISING, daily_bars=fut.daily_bars, benchmark=bench(daily(60, seed=2), session_return=0.0))
    assert run(intraday_base).model_dump() == run(intraday_fut).model_dump()                           # and in INTRADAY_MODE (the 5-session horizon)


def test_L3_future_sector_bars_do_not_change_the_score():
    b = daily(60, seed=2)
    base = inputs(RISING, benchmark=bench(b, session_return=0.001))
    fut = inputs(RISING, benchmark=bench(b + [DailyBar(date=T + timedelta(days=k), open=1, high=1, low=1, close=1, volume=1) for k in range(0, 5)], session_return=0.001))
    assert run(base).model_dump() == run(fut).model_dump()


def test_L4_future_outcomes_do_not_affect_the_score():
    stock = daily(80, seed=9)
    rows_a = replay_daily("SYN", stock, None)
    shocked = stock[:50] + [DailyBar(date=b.date, open=b.open * 3, high=b.high * 3, low=b.low * 3, close=b.close * 3, volume=b.volume * 7) for b in stock[50:]]
    rows_b = replay_daily("SYN", shocked, None)
    for a, b in zip(rows_a, rows_b):
        if a.date < stock[50].date.isoformat():
            assert (a.score, a.coverage, a.a, a.d, a.f) == (b.score, b.coverage, b.a, b.d, b.f), a.date
    assert any(a.fwd != b.fwd for a, b in zip(rows_a, rows_b) if a.date < stock[50].date.isoformat())   # outcomes changed, scores did not


def test_L5_expected_intraday_volume_curve_uses_prior_sessions_only():
    pr = prior_sessions(3, vol=1000.0)
    e30, used = expected_cumulative_volume(pr, 30)
    assert e30 == 30_000.0 and used == 3
    assert expected_cumulative_volume(pr, 400) == (None, 0)                                         # sessions shorter than m are skipped
    assert expected_cumulative_volume({"p0": pr["p0"]}, 30) == (None, 1)                           # fewer than MIN_PRIOR_SESSIONS
    heavy = inputs(RISING, [5000.0] * 90, prior=pr)
    heavier_today = inputs(RISING, [5000.0] * 89 + [9e9], prior=pr)                                 # the last CURRENT-session bar is still closed by NOW … included
    assert run(heavy).components["relative_volume"].raw["expected_cumulative_volume"] == run(heavier_today).components["relative_volume"].raw["expected_cumulative_volume"]
    m = SimulatedMarket(seed=11, start_time=T0)
    keys = list(m.session_candles("NVDA", 4))
    assert keys == sorted(keys) and all(k < keys[-1] for k in keys[:-1])                           # the current session is the newest key; the rest precede it


# ---------------------------------------------------------------- P adapters / provider / stream / route ------------------------

def test_P1_simulator_sessions_are_deterministic_and_synthetic():
    a, b = SimulatedMarket(seed=11, start_time=T0), SimulatedMarket(seed=11, start_time=T0)
    sa, sb = a.session_candles("MSFT", 5), b.session_candles("MSFT", 5)
    assert list(sa) == list(sb) and len(sa) == 5 and all(len(v) == 390 for k, v in list(sa.items())[:-1]) and len(list(sa.values())[-1]) == 240   # 240 backfilled bars before the first step
    assert [c.model_dump() for c in list(sa.values())[0]] == [c.model_dump() for c in list(sb.values())[0]]
    assert [c.model_dump() for c in list(sa.values())[-1]] == [c.model_dump() for c in a.candles("MSFT", 0)]
    first = list(sa.values())[0]
    assert all(first[i].time + 60 == first[i + 1].time for i in range(389)) and all(c.final and c.volume > 0 for c in first)
    assert SimulatedMarket(seed=12, start_time=T0).session_candles("MSFT", 2) != sa


class FakeBarsClient:
    """Minute bars across two ET sessions incl. pre-market rows; never contacted twice within the cache window."""
    def __init__(self):
        self.calls = 0

    def get_stock_snapshot(self, req):
        raise AssertionError("not used")

    def get_stock_bars(self, req):
        self.calls += 1
        s = req.symbol_or_symbols[0]
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        rows = []
        for day in (datetime(2026, 10, 20, tzinfo=et), datetime(2026, 10, 21, tzinfo=et)):
            for minute in list(range(9 * 60, 9 * 60 + 30)) + list(range(9 * 60 + 30, 16 * 60)) + list(range(16 * 60, 16 * 60 + 20)):
                t = day.replace(hour=minute // 60, minute=minute % 60)
                rows.append(SimpleNamespace(timestamp=t, open=100.0, high=100.5, low=99.5, close=100.2, volume=10.0))
        return SimpleNamespace(data={s: rows})


def test_P2_real_adapter_groups_cached_minute_bars_by_et_session_without_extra_requests():
    from trader_view.market_real import RealMarketData
    client = FakeBarsClient()
    r = RealMarketData(client_factory=lambda: client, feed="iex", quote_cache_s=3, bars_cache_s=60, history_minutes=240, now_fn=lambda: 0.0)
    sess = r.session_candles("NVDA", 5)
    assert list(sess) == ["2026-10-20", "2026-10-21"] and all(len(v) == 390 for v in sess.values()) and client.calls == 1
    assert sess["2026-10-21"][0].time == int(datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc).timestamp())      # 09:30 ET (EDT) = 13:30 UTC
    r.session_candles("NVDA", 1)
    assert client.calls == 1 and list(r.session_candles("NVDA", 1)) == ["2026-10-21"]                                # served from the same cache


def test_P3_provider_caches_results_and_invalidates():
    clock = [float(T0)]
    prov = DemandProvider(SimulatedMarket(seed=11, start_time=T0), result_cache_s=30, now_fn=lambda: clock[0])
    a = prov.result("NVDA")
    assert prov.computations == 1 and prov.result("nvda") == a and prov.computations == 1
    clock[0] += 31
    prov.result("NVDA")
    assert prov.computations == 2
    prov.invalidate("NVDA")
    prov.result("NVDA")
    assert prov.computations == 3
    prov.invalidate()
    assert prov._results == {}


def make_stream(**kw):
    market = kw.pop("market", None) or SimulatedMarket(seed=11, start_time=T0)
    return TerminalStream(market, kw.pop("positions", None) or FakePositions(), kw.pop("research", None) or FakeResearch(), watchlist_fn=lambda: list(WL),
                          tick_seconds=1.0, allow_manual_tick=True, run_loop=False, now_fn=lambda: T0, **kw)


@pytest.fixture
def ts():
    return make_stream()


@pytest.fixture
def api(ts, monkeypatch):
    monkeypatch.setitem(RT.STREAM, "instance", ts)
    from api.server import app
    return TestClient(app)


def test_P4_stream_state_cadence_refresh_and_symbol_binding(api, ts):
    assert "demand" in MESSAGE_TYPES and "demand" in SYMBOL_BOUND_TYPES
    assert not accepts_message({"type": "demand", "symbol": "SNDK", "score": 80}, "NVDA") and accepts_message({"type": "demand", "symbol": "NVDA"}, "NVDA")
    assert not accepts_message({"type": "demand"}, "NVDA")
    s = make_stream(demand_poll_seconds=5)
    assert s.demand_every == 5 and s.cadence()["demand_s"] == 5.0 and s.config_payload()["demand"]["separate_from_ppe"] is True
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        b = burst(ws, "NVDA")
        assert [m["type"] for m in b] == ["subscribed", "state", "candles", "quote", "candle", "position", "evidence"]    # the Stage 5.5 burst is unchanged
        assert b[1]["demand"]["symbol"] == "NVDA" and b[1]["demand"]["status"] == "SIMULATED_DATA" and b[1]["demand"]["disclaimer"] == DISCLAIMER
        ws.send_json({"type": "refresh"})
        got = marker(ws)
        assert [m["type"] for m in got] == ["position", "evidence", "ppe", "demand", "setup", "proposal"] and got[3]["symbol"] == "NVDA" and got[3]["version"] == "demand_v1"   # "setup": Stage 5.8; "proposal": Stage 5.9
        n0 = ts.demand.computations
        for i in range(1, 31):
            ws.send_json({"type": "tick", "now": T0 + i})
        got = marker(ws)
        types = [m["type"] for m in got]
        assert types.count("quote") == 30 and types.count("demand") == 1 and types.count("ppe") == 0                      # demand at tick 30 only (30 s cadence), PPE at 60
        assert ts.demand.computations == n0 and all(m["symbol"] == "NVDA" for m in got if m["type"] == "demand")          # the frozen test clock keeps the 30 s result cache warm: no recompute
        clock = [float(T0)]
        prov = DemandProvider(ts.market, result_cache_s=5, now_fn=lambda: clock[0])
        s5 = make_stream(market=ts.market, demand_provider=prov, demand_poll_seconds=5)
        assert s5.demand_every == 5 and s5.demand is prov
        prov.result("NVDA")
        clock[0] += 6
        prov.result("NVDA")
        assert prov.computations == 2                                                                                     # an advancing clock recomputes after the cache window
        burst(ws, "SNDK")
        for i in range(31, 61):
            ws.send_json({"type": "tick", "now": T0 + i})
        got = marker(ws)
        assert [m["symbol"] for m in got if m["type"] == "demand"] == ["SNDK"]                                           # never the previous ticker


def test_P5_route_is_get_only_and_returns_the_contract(api):
    r = api.get("/api/terminal/demand/nvda")
    assert r.status_code == 200
    j = r.json()
    assert j["symbol"] == "NVDA" and j["version"] == "demand_v1" and j["disclaimer"] == DISCLAIMER and set(j["weights"]) == set(WEIGHTS) and j["mode"] in ("INTRADAY_MODE", "DAILY_MODE")
    assert api.get("/api/terminal/demand/12345").status_code == 400 and api.get("/api/terminal/demand/" + "A" * 11).status_code == 400
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert api.request(method, "/api/terminal/demand/NVDA", json={"side": "buy", "qty": 1}).status_code == 405
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/terminal")}
    assert mine["/api/terminal/demand/{symbol}"] == {"get"} and all(o == {"get"} for o in mine.values())
    for p in mine:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|alert|alpaca|robinhood|trade", p), p
    cfg = api.get("/api/terminal/config").json()["demand"]
    assert cfg["version"] == "demand_v1" and cfg["refresh_s"] == config.TRADER_DEMAND_REFRESH_SECONDS and "never per quote tick" in cfg["cadence"]


def test_P6_a_demand_failure_never_hides_the_state_or_touches_ppe(api, monkeypatch):
    class Boom:
        computations = 0

        def result(self, symbol):
            raise RuntimeError("demand down")

        def invalidate(self, symbol=None):
            pass
    broken = make_stream(demand_provider=Boom())
    monkeypatch.setitem(RT.STREAM, "instance", broken)
    st = broken.state("NVDA", with_candles=False)
    assert st.demand.status == "NOT_AVAILABLE" and st.demand.score is None and "RuntimeError" in st.demand.note and st.ppe.status == "SIMULATED_DATA"
    normal = make_stream().state("NVDA", with_candles=False)
    assert normal.ppe.model_dump(exclude={"sources"}) == st.ppe.model_dump(exclude={"sources"})            # PPE is the same with or without demand
    assert api.get("/api/terminal/demand/NVDA").json()["status"] == "NOT_AVAILABLE"


def test_P7_daily_fallback_in_the_provider_when_the_session_is_short():
    m = SimulatedMarket(seed=11, start_time=T0, backfill_minutes=10)
    r = DemandProvider(m, now_fn=lambda: float(T0)).result("MSFT")
    assert r.mode == "DAILY_MODE" and r.coverage_weight == 0.6 and r.score is not None
    assert {k for k, c in r.components.items() if c.status == "NOT_AVAILABLE"} == {"vwap_acceptance", "intraday_structure"}
    assert all(c.code == "INTRADAY_ONLY" for c in r.components.values() if c.status == "NOT_AVAILABLE")


# ---------------------------------------------------------------- S safety ------------------------------------------------------

def test_S1_no_execution_broker_model_or_ppe_engine_import():
    forbidden = FORBIDDEN_IMPORTS + ("ppe.engine", "ppe.stretch", "ppe.confidence", "ppe.evidence_context", "trader_view.ppe_provider.PPEProvider")
    for rel in DEMAND_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == p or name.startswith(p + ".") for p in forbidden), (rel, name)
        assert not re.search(r"TradingClient|TradingStream|messages\.create|get_provider|one_request|/v2/orders|alpaca_order_writer|\.submit\(|place_order|cancel_order|"
                             r"requests\.(get|post)|urlopen|\beval\(|\bexec\(|__import__|os\.system|subprocess|build_packet|get_data_client|compute_ppe", text), rel


def test_S2_ppe_evidence_and_execution_files_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    pinned = [p.relative_to(ROOT).as_posix() for p in (ROOT / "ppe").glob("*.py")] + ["trader_view/ppe_provider.py", "trader_view/adapters.py", "trader_view/hub.py",
                                                                                      "trader_view/positions.py", "trader_view/research.py", "evidence/engine.py", "evidence/rules.py",
                                                                                      "evidence/store.py", "paper/alpaca_order_writer.py", "paper/alpaca_orders.py", "paper/alpaca_readonly.py",
                                                                                      "paper/execution.py", "portfolio/provider.py", "database/migrations.py",
                                                                                      "DESIGN_56_PPE_ENGINE.md", "tests/test_evidence_54.py"]   # api/server.py + index.html: extended by Stage 5.8 (pinned in tests/test_setups_58.py)
    for rel in pinned:
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob(rel), f"{rel} differs from {BASE}"
    base_cfg = blob("config.py").decode("utf-8")
    now_cfg = (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert now_cfg.startswith(base_cfg.rstrip("\n")) and "TRADER_DEMAND_REFRESH_SECONDS" in now_cfg                   # config is append-only


def strip_sanctioned(text: str) -> str:
    """Source without comments / docstrings and without the two sanctioned sentences (DISCLAIMER, NOT_A_SIGNAL)."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
            for ln in range(node.body[0].lineno - 1, node.body[0].end_lineno):
                lines[ln] = "\n"
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id in ("DISCLAIMER", "NOT_A_SIGNAL", "SIMULATED_NOTE") for x in node.targets):
            for ln in range(node.lineno - 1, node.end_lineno):
                lines[ln] = "\n"
    return re.sub(r"#.*", "", "".join(lines)).replace(DISCLAIMER, "").replace(NOT_A_SIGNAL, "")


def test_S3_no_recommendation_or_probability_language_in_demand_sources():
    src = "".join(strip_sanctioned((ROOT / f).read_text(encoding="utf-8")) for f in DEMAND_FILES)       # per file: every module docstring is stripped
    assert not re.search(r"(?i)\b(buy|sell|recommend\w*|short it|go long)\b", src), "trading verbs in demand sources"
    assert not re.search(r"(?i)expected return|reversal probability|probability|will rise|price target|win rate|bearish demand", src)
    assert demand_source().count("probability that the stock will rise") == 1                      # only inside the disclaimer constant
    assert "buyer" in demand_source().lower()                                                        # the thing it measures


def walk_keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k).lower())
            walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_keys(v, out)
    return out


def test_S4_no_recommendation_probability_or_target_field_in_the_result():
    keys = walk_keys(run(inputs(RISING, benchmark=bench(session_return=0.001))).model_dump(), set())
    for bad in ("buy", "sell", "recommendation", "action", "probability", "expected_return", "target", "stop", "signal", "prob"):
        assert not any(bad == k or k.startswith(bad + "_") or k.endswith("_" + bad) for k in keys), bad
    assert "score" in keys and "disclaimer" in keys


def test_S5_actions_stay_disabled_and_the_ui_never_derives_actions_from_demand(ts):
    acts = {a.id: a for a in ts.state("NVDA", with_candles=False).actions}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False and acts["prepare_buy"].status == "PREVIEW_NOT_CONNECTED"
    assert "demand" not in JS.split("function renderActions")[1].split("function renderStatusLine")[0]
    assert '{ id: "prepare_buy", label: "Prepare Buy", enabled: false' in JS and 'aria-disabled="${a.enabled ? "false" : "true"}"${a.enabled ? "" : " disabled"}' in JS


def test_S6_no_real_model_call_and_no_network_primitive(api, monkeypatch):
    import anthropic

    def boom(self, *a, **k):
        raise AssertionError("a model client was constructed by Market Demand")
    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    assert api.get("/api/terminal/demand/NVDA").status_code == 200 and api.get("/api/terminal/state/NVDA").status_code == 200
    assert not re.search(r"anthropic|get_provider|\.analyze\(|messages\.create|run_tool_loop|LLMProvider|technical_agent|socket\.|http", demand_source())


def test_S7_no_automatic_research_and_no_packet_write(ts):
    n = len(ts.research.calls)
    for _ in range(3):
        ts.demand_result("NVDA")
    assert ts.research.calls == ts.research.calls[:n]                                                  # demand never touches research
    assert not re.search(r"build_packet|research_flow|run_research|EvidenceStore\(", demand_source())


def test_S8_no_order_path_through_the_socket_or_routes(api):
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        for cmd in ({"type": "demand", "symbol": "NVDA"}, {"type": "order", "demand": 90}, {"type": "prepare_buy", "symbol": "NVDA"}, {"type": "execute"}):
            ws.send_json(cmd)
            e = ws.receive_json()
            assert e["type"] == "error" and e["code"] == "BAD_MESSAGE", cmd
    routes = (ROOT / "api" / "routes" / "trader_view.py").read_text(encoding="utf-8")
    assert "@router.post" not in routes and "@router.put" not in routes and "@router.delete" not in routes and "preview(" not in routes


def test_S9_demand_does_not_alter_ppe_and_is_not_combined_with_it(ts):
    st = ts.state("NVDA", with_candles=False)
    assert st.ppe.symbol == "NVDA" and st.demand.symbol == "NVDA" and st.ppe.version == "ppe_v1" and st.demand.version == "demand_v1"
    assert "demand" not in json.dumps(st.ppe.model_dump()).lower() and "stretch" not in json.dumps(st.demand.model_dump()).lower()
    assert not re.search(r"(?i)(stretch|confidence|ppe)[\w.\[\]\"']*\s*[*+/]\s*(demand|dem)\b|\b(demand|dem)[\w.\[\]\"']*\s*[*+/]\s*(stretch|confidence|ppe)", demand_source() + JS + RENDER)   # no arithmetic between them
    assert "composite" not in (demand_source() + RENDER).lower() and "master score" not in (demand_source() + RENDER).lower()


# ---------------------------------------------------------------- U ui -----------------------------------------------------------

def test_U1_node_tests_of_the_demand_markup():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the same markup rules are exercised by the browser harness flow")
    cmd = [node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_demand.test.cjs")]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0 and "# fail 0" in r.stdout, r.stdout + r.stderr
    assert int(re.search(r"# pass (\d+)", r.stdout).group(1)) >= 11
    for f in ("trader_core.test.cjs", "trader_render.test.cjs"):                                       # the Stage 5.5 / 5.6.1 node tests still pass
        rr = subprocess.run(cmd[:-1] + [str(ROOT / "tests" / "frontend" / f)], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
        assert rr.returncode == 0 and "# fail 0" in rr.stdout, f


def test_U2_demand_section_is_separate_from_ppe_in_the_page():
    assert '"demand"' in CORE.split("SYMBOL_BOUND = new Set(")[1].split(")")[0] and 'case "demand": renderDemand(m); break;' in JS
    assert '<section class="tv-panel tv-panel-demand"><h3>Market Demand</h3><div id="tv-demand" class="tv-panel-body"></div></section>' in JS
    assert JS.index('<h3>PPE</h3>') < JS.index('<h3>Market Demand</h3>') < JS.index('<h3>Evidence</h3>')
    assert '["tv-demand", "DEMAND"]' in JS and "renderDemand(d.demand)" in JS and "${esc(d.disclaimer || \"\")}" in JS and "renderDemand(null)" in JS
    assert "demandHtml" in RENDER and "demandModel" in RENDER and 'data-demand="score"' in RENDER and 'data-demand-mode=' in RENDER
    assert not re.search(r"(?i)(stretch|confidence|demand)\s*[:=]\s*[0-9.]+", demand_source() + JS)    # no fabricated number anywhere


def test_U3_no_buy_sell_text_in_the_demand_ui():
    section = RENDER.split("Market Demand (Stage 5.7")[1].split("status strip")[0]
    assert not re.search(r"(?i)\b(buy|sell|recommend\w*|should)\b", section) and "buyer" in section
    assert not re.search(r"(?i)probability|expected return|reversal|win rate", section)
    assert DISCLAIMER not in RENDER and DISCLAIMER not in JS                                             # always the server's sentence


def test_U4_demand_colours_are_not_the_price_green_red_and_text_is_readable():
    weak = re.search(r"\.tv-dem-val\.weak \{([^}]*)\}", CSS).group(1)
    strong = re.search(r"\.tv-dem-val\.strong \{([^}]*)\}", CSS).group(1)
    assert "var(--tv-down)" in weak and "var(--tv-up)" in strong and "--green" not in strong and "--red" not in weak
    assert ".tv-panel-demand" in CSS and ".tv-dem-mode" in CSS
    assert min(int(x) for x in re.findall(r"font-size:\s*(\d+)px", CSS)) >= 11


def test_U5_harness_flow_registered_after_trader_view():
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert "market_demand" in names and names.index("market_demand") == names.index("trader_view") + 1 and names.index("market_demand") < names.index("automation")
    assert names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]
    src = inspect.getsource(F.market_demand)
    assert "tv-demand" in src and "SIMULATED DATA" in src and "disclaimer" in src and 'c.shot("market_demand"' in src


def test_U6_readme_and_design_document_the_caveats():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sec = readme[readme.index("## Market Demand (Stage 5.7"):]
    flat = " ".join(sec.replace("*", "").split())
    for s in ("not a probability", "not a trade signal", "not part of PPE", "not execution logic", "bid/ask", "intraday validation", "not live-tested", DISCLAIMER):
        assert s in flat, s
    design = (ROOT / "DESIGN_57_MARKET_DEMAND.md").read_text(encoding="utf-8")
    for s in ("## 2. Non-goals", "## 4. Components, weights, exact formulas", "## 9. Anti-lookahead rules", DISCLAIMER, "0.60", "VERY_STRONG"):
        assert s in design, s
    assert WEIGHTS == {"relative_volume": 0.20, "vwap_acceptance": 0.20, "intraday_structure": 0.20, "range_strength": 0.15, "relative_strength": 0.15, "follow_through": 0.10}
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9 and BANDS[0][2] == "VERY_WEAK" and BANDS[-1] == (90, 100, "VERY_STRONG")
    assert [demand_label(x) for x in (0, 24, 25, 39, 40, 59, 60, 74, 75, 89, 90, 100)] == ["VERY_WEAK", "VERY_WEAK", "WEAK", "WEAK", "MIXED", "MIXED", "STRENGTHENING",
                                                                                           "STRENGTHENING", "STRONG", "STRONG", "VERY_STRONG", "VERY_STRONG"]


# ---------------------------------------------------------------- B replay sanity --------------------------------------------------

def test_B1_daily_replay_rows_are_point_in_time_and_summarised():
    stock = daily(90, step=0.001, seed=21)
    rows = replay_daily("SYN", stock, None)
    assert rows and all(r.date > stock[0].date.isoformat() for r in rows) and all(r.coverage in (0.45, 0.6) for r in rows)   # no benchmark: E missing
    s = summarize_daily(rows)
    assert s["observations"] == len(rows) and set(s["bands"]) == {b[2] for b in BANDS} and "DAILY_MODE" in s["mode"]


def test_B2_synthetic_scenarios_behave_as_documented():
    from demand.replay import summarize_synthetic
    scen = {}
    for name, inp in synthetic_scenarios().items():
        r = run(inp)
        scen[name] = {"score": r.score, "label": r.label, "mode": r.mode, "coverage": r.coverage_weight, "components": {k: c.score for k, c in r.components.items()}, "key_factors": r.key_factors}
    checks = summarize_synthetic(scen, [])["scenario_checks"]
    assert all(checks.values()), checks
