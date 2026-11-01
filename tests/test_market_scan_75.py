"""
Stage 7.5 - the SHARED MARKET SCANNER v1, offline core (market_scan/; DESIGN_75_MARKET_SCAN.md). Temporary files only: every store,
archive and scan lives under tmp_path; the production paths are never read, written or created.

  S01-S17 the scan: the feature maths on fixtures; a whole scan; fail-closed on STALE; determinism and the tie-break; the Stage 7.4
          BLOCK / INSUFFICIENT_DATA / WARN gate; point in time and look-ahead; missing, stale, malformed and contradictory data; duplicates
          and revisions; evidence provenance; Day / Swing separation; halts; catalysts; earnings; the regime; the universe and the inactive
          dynamic universe; read-only behaviour; the CLI
  V01     independent recomputation of the figures from raw SQL
  A01-A02 adversarial inputs: tampered rows and unreadable symbols fail closed, never crash
  X01     isolation: no network, broker, execution, proposal or model; no write path; nothing outside market_scan imports it; no
          execution field anywhere in a scan document
  F01-F03 scope (freeze bookkeeping)
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tokenize
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import freeze_ledger as FL
import market_scan as MS
from dynamic_universe import core as DUC
from dynamic_universe import plan as DUP
from dynamic_universe import signals as DUS
from market_intel import calendar as CAL
from market_intel import ingest as IG
from market_intel import store as ST
from market_intel import validate as VA
from market_intel.archive import RawArchive
from market_intel.models import FetchRecord, Observation
from market_intel.news import persist as NP
from market_intel.sources import alpaca_news as AN
from market_intel.sources import earnings_av as EA
from market_intel.sources import halts as HA
from market_intel.store import DatasetSpec, MarketIntelStore
from market_intel.store_v2 import MarketIntelStoreV2
from market_scan import features as F
from market_scan import filters as FI
from market_scan import flags as FG
from market_scan import run as R
from test_market_intel_collector_73 import TUE, Clock, art, news

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
S = timedelta(seconds=1)
T = TUE                                                        # Tuesday 2025-12-02 10:00 New York: the regular session
TODAY = date(2025, 12, 2)
PREV = CAL.previous_market_day(TODAY)                          # Monday 2025-12-01, the last completed session at T
BASE = {"NVDA": 180.0, "AMD": 210.0, "SOXX": 300.0, "SPY": 680.0}
OPEN_LAST = {"NVDA": (185.4, 186.0), "AMD": (212.1, 211.0), "SOXX": (303.0, 304.5), "SPY": (681.0, 683.4)}
TEST_CORE = DUC.Core("core_file", "test core", (("NVDA", "Semiconductors"), ("AMD", "Semiconductors"), ("SHOP", "Unmapped")), "test")
WATCH = ("NVDA", "CLS")


# ============================================================================================================ fixtures
def ny(d: date, hh=0, mm=0, ss=0) -> datetime:
    return R.ny(d, hh, mm) + timedelta(seconds=ss)


def back(last: date, n: int) -> list:
    out, x = [], last
    while len(out) < n:
        out.append(x)
        x = CAL.previous_market_day(x)
    return list(reversed(out))


def seal(w, did, status="COMPLETED"):
    w.store.finish_dataset(did, status)
    IG.recover_pending(w.store, w.archive, clock=w.clk)                     # anchors the seal, as the 7.1 recovery does


def at_least(w, t):
    w.clk.t = max(w.clk.t, t)


def dbar(sym, d, close, *, feed="iex", retrieved, job="IMPORT", high=None, low=None, open_=None, volume=1_000_000):
    return Observation(kind="BAR_1DAY", symbol=sym, provider="ALPACA", feed=feed, adjustment="raw", observed_at=ny(d), retrieved_at=retrieved,
                       available_at=retrieved, session="REG", session_date=d, status="AVAILABLE", backfilled=job != "FIXTURE",
                       payload={"open": open_ if open_ is not None else close, "high": high if high is not None else close * 1.01,
                                "low": low if low is not None else close * 0.99, "close": close, "volume": volume})


def add_daily(w, sym, closes, *, feed="iex", job="IMPORT", **kw):
    ds = sorted(closes)
    did = w.store.create_dataset(DatasetSpec(job_kind=job, kind="BAR_1DAY", provider="ALPACA", feed=feed, adjustment="raw", symbols=[sym], timeframe="1Day",
                                             start_date=ds[0], end_date=ds[-1]))
    w.store.ingest(did, [dbar(sym, d, c, feed=feed, retrieved=w.clk(), job=job, **kw) for d, c in sorted(closes.items())])
    seal(w, did)
    return did


def add_minutes(w, sym, bars, *, extended=False, job="IMPORT"):
    """bars: {start: volume}; every bar complete before the clock."""
    ts = sorted(bars)
    at_least(w, ts[-1] + timedelta(minutes=1, seconds=5))
    did = w.store.create_dataset(DatasetSpec(job_kind=job, kind="BAR_1MIN", provider="ALPACA", feed="iex", adjustment="raw", symbols=[sym], timeframe="1Min",
                                             start_date=CAL.session(ts[0])["session_date"], end_date=CAL.session(ts[-1])["session_date"],
                                             request={"extended_hours": extended}))
    r = w.clk()
    w.store.ingest(did, [Observation(kind="BAR_1MIN", symbol=sym, provider="ALPACA", feed="iex", adjustment="raw", observed_at=t, retrieved_at=r, available_at=r,
                                     session=CAL.session(t)["session"], session_date=CAL.session(t)["session_date"], status="AVAILABLE", backfilled=job != "FIXTURE",
                                     payload={"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": v}) for t, v in sorted(bars.items())])
    seal(w, did)
    return did


def base_minutes(today_volume=300, prior_volume=100, prior=None):
    out = {}
    for d in (prior if prior is not None else back(PREV, 10)):
        for k in range(30):
            out[ny(d, 9, 30) + timedelta(minutes=k)] = prior_volume
    for k in range(29):                                                          # 09:30 - 09:58: the bars complete at 10:00
        out[ny(TODAY, 9, 30) + timedelta(minutes=k)] = today_volume
    return out


def snap(*, quote_at, prev_close, open_, last, today=TODAY, prev=PREV, trade_at=None, bid=None, ask=None, daily_day=None, prev_day=None):
    return {"bid": last - 0.01 if bid is None else bid, "ask": last + 0.01 if ask is None else ask, "bid_size": 1, "ask_size": 1, "quote_at": quote_at.isoformat(),
            "last_price": last, "last_size": 10, "last_trade_at": (trade_at or quote_at - S).isoformat(), "minute_bar": None,
            "daily_bar": {"start": ny(daily_day or today).isoformat(), "open": open_, "high": max(open_, last) + 1, "low": min(open_, last) - 1, "close": last,
                          "volume": 5, "trade_count": 1, "vwap": last},
            "prev_daily_bar": {"start": ny(prev_day or prev).isoformat(), "open": prev_close, "high": prev_close + 1, "low": prev_close - 1, "close": prev_close,
                               "volume": 5, "trade_count": 1, "vwap": prev_close}}


def add_snaps(w, rows, *, feed="iex", absent=(), retrieved_after=0.5):
    """rows: [(symbol, quote_at, payload)]; absent: [(symbol, at)] - a looked-for symbol the provider omitted."""
    syms = sorted({s for s, _, _ in rows} | {s for s, _ in absent})
    late = max([qa for _, qa, _ in rows] + [a for _, a in absent]) + timedelta(seconds=retrieved_after)
    at_least(w, late + timedelta(seconds=0.1))
    did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="QUOTE_SNAPSHOT", provider="ALPACA", feed=feed, adjustment="none", symbols=syms))
    out = []
    for sym, qa, p in rows:
        r = qa + timedelta(seconds=retrieved_after)
        flags = VA.quote_codes({"bid": p["bid"], "ask": p["ask"], "quote_at": qa, "last_trade_at": F.at(p["last_trade_at"])})
        out.append(Observation(kind="QUOTE_SNAPSHOT", symbol=sym, provider="ALPACA", feed=feed, adjustment="none", observed_at=qa, retrieved_at=r, available_at=r,
                               session=CAL.session(qa)["session"], session_date=CAL.session(qa)["session_date"], status="AVAILABLE",
                               quality_flags=tuple(sorted(set(flags))), payload=p))
    for sym, a in absent:
        r = a + timedelta(seconds=retrieved_after)
        out.append(Observation(kind="QUOTE_SNAPSHOT", symbol=sym, provider="ALPACA", feed=feed, adjustment="none", observed_at=r, retrieved_at=r, available_at=r,
                               session=CAL.session(r)["session"], session_date=CAL.session(r)["session_date"], status="UNAVAILABLE", payload={}))
    w.store.ingest(did, out)
    seal(w, did)
    return did


def base_snaps(syms=tuple(BASE), *, end=T - 2 * S, n=5):
    rows = []
    for k in range(n):
        qa = end - timedelta(seconds=30 * (n - 1 - k))
        rows += [(s, qa, snap(quote_at=qa, prev_close=BASE[s], open_=OPEN_LAST[s][0], last=OPEN_LAST[s][1])) for s in syms]
    return rows


def halt_obs(sym, halted, resumed=None, *, at):
    p = {"halted_at": halted.isoformat(), "reason_code": "LUDP", "reason_code_known": True, "market": "NASDAQ", "issue_name": f"{sym} Corp",
         "pause_threshold_price": None, "resumption_date": None, "resumption_quote_at": None, "resumed_at": resumed.isoformat() if resumed else None}
    return Observation(kind="HALT", symbol=sym, provider="NASDAQ_TRADER", feed="none", adjustment="none", observed_at=halted, retrieved_at=at, available_at=at,
                       session=CAL.session(halted)["session"], session_date=CAL.session(halted)["session_date"], status="AVAILABLE",
                       source_identifier=f"{sym}|{halted.isoformat()}", quality_flags=tuple(VA.halt_codes({"halted_at": halted, "resumed_at": resumed})),
                       payload=p)


def halt_poll(w, at, halts=(), *, status="OK", ingest_at=None):
    """One halt-feed poll: a fetch record (the feed was seen working at `at`, recorded at `ingest_at`) and the halts it listed."""
    at_least(w, ingest_at or at)
    did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="HALT", provider="NASDAQ_TRADER", feed="none", adjustment="none", symbols=[]))
    w.store.record_fetch(FetchRecord(provider="NASDAQ_TRADER", endpoint=HA.ENDPOINT, started_at=at - 0.2 * S, retrieved_at=at, status=status, http_status=200),
                         dataset_id=did)
    if halts:
        w.store.ingest(did, list(halts))
    seal(w, did, "COMPLETED" if halts else ("EMPTY" if status == "OK" else "FAILED"))
    return did


def world(tmp, *, v2=False, daily=None, minutes=True, halt_at=T - 30 * S, snaps=None, job="IMPORT"):
    tmp.mkdir(parents=True, exist_ok=True)
    clk = Clock(T - timedelta(hours=2))
    cls = MarketIntelStoreV2 if v2 else MarketIntelStore
    w = SimpleNamespace(store=cls(tmp / "mi.db", clock=clk, approvals_root=tmp / "approvals"), archive=RawArchive(tmp / "raw"), clk=clk, tmp=tmp)
    for sym, close in (BASE if daily is None else daily).items():
        add_daily(w, sym, {d: close for d in back(PREV, 25)}, job=job)
    if minutes:
        add_minutes(w, "NVDA", base_minutes(), job=job)
    if halt_at is not None:
        halt_poll(w, halt_at)
    rows = base_snaps() if snaps is None else snaps
    if rows:
        add_snaps(w, rows)
    return w


def scan(w, at=T, **kw):
    kw.setdefault("archive_root", w.archive.root)
    kw.setdefault("core", TEST_CORE)
    kw.setdefault("watchlist", WATCH)
    kw.setdefault("tmp_root", None)
    return R.run_scan(w.store.path, as_of=at, **kw)


def res(doc, sym):
    return next(r for r in doc["results"] if r["symbol"] == sym)


def hz(doc, sym, h=MS.INTRADAY):
    return res(doc, sym)["horizons"][h]


def fv(doc, sym, name):
    f = res(doc, sym)["features"][name]
    return f["value"], f["status"]


def tree(*dirs) -> dict:
    return {f"{Path(d).name}/{p.relative_to(d).as_posix()}": hashlib.sha256(p.read_bytes()).hexdigest() for d in dirs for p in sorted(Path(d).rglob("*"))
            if p.is_file()}


def raw_insert(path, table, row_changes, *, where_symbol, where_kind, unchecked=True):
    """A copy of a stored row written around the store (raw SQL, around the CHECKs) into an OPEN dataset: a tool, or a bug."""
    c = sqlite3.connect(str(path))
    try:
        if unchecked:
            c.execute("PRAGMA ignore_check_constraints = ON")
        cols = [x[1] for x in c.execute(f"PRAGMA table_info({table})")]
        row = dict(zip(cols, c.execute(f"SELECT * FROM {table} WHERE symbol = ? AND kind = ? ORDER BY rowid LIMIT 1", (where_symbol, where_kind)).fetchone()))
        row.update(row_changes)
        c.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [row[k] for k in cols])
        c.commit()
    finally:
        c.close()


# ============================================================================================================ the scan (S)
def test_S01_the_feature_maths_follow_the_pre_registered_formulas_on_fixtures():
    s = snap(quote_at=T - 2 * S, prev_close=180.0, open_=185.4, last=186.0)
    g = F.gap_pct(s, session="REG", today=TODAY, previous=PREV)
    assert (g["value"], g["status"], g["basis"]) == (3.0, "AVAILABLE", "TODAY_OPEN")                     # 100 x (185.4 / 180 - 1)
    pre_t = ny(TODAY, 8, 0)
    p = F.gap_pct(snap(quote_at=pre_t, prev_close=180.0, open_=185.4, last=181.8, trade_at=pre_t - S), session="PRE", today=TODAY, previous=PREV)
    assert (p["value"], p["basis"]) == (1.0, "PRE_LAST_TRADE")                                         # the last premarket trade
    assert F.gap_pct(snap(quote_at=T, prev_close=180.0, open_=185.4, last=186.0, daily_day=PREV), session="REG", today=TODAY, previous=PREV)["codes"] == ["DAILY_BAR_NOT_TODAY"]
    assert "PREV_CLOSE_NOT_PREVIOUS_SESSION" in F.gap_pct(snap(quote_at=T, prev_close=180.0, open_=185.4, last=186.0, prev_day=back(PREV, 2)[0]),
                                                          session="REG", today=TODAY, previous=PREV)["codes"]
    assert F.gap_pct(s, session="CLOSED", today=TODAY, previous=PREV)["status"] == "NOT_APPLICABLE"
    # relative volume: the median of 10 prior sessions (even: the mean of the 5th and 6th); a zero or missing baseline is never a value
    rv = F.relative_volume_tod(900.0, [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0], minutes=30)
    assert (rv["value"], rv["baseline_volume"]) == (round(900.0 / 550.0, 6), 550.0)
    assert F.relative_volume_tod(900.0, [0.0] * 10, minutes=30)["codes"] == ["ZERO_BASELINE"]
    assert F.relative_volume_tod(900.0, [100.0] * 9 + [None], minutes=30, missing=["2025-11-17"])["missing_sessions"] == ["2025-11-17"]
    assert F.relative_volume_tod(900.0, [100.0] * 10, minutes=0)["codes"] == ["NO_COMPLETED_MINUTE"]
    open_at = ny(TODAY, 9, 30)
    bars = [{"start": open_at + timedelta(minutes=k), "volume": 10} for k in range(5)]
    assert F.cumulative_volume(bars, open_at=open_at, minutes=3) == 30 and F.cumulative_volume([], open_at=open_at, minutes=3) is None
    assert F.cumulative_volume(bars + [{"start": open_at, "volume": float("nan")}], open_at=open_at, minutes=3) is None
    assert F.cumulative_volume(bars + [{"start": open_at, "volume": -5}], open_at=open_at, minutes=3) is None           # never a negative volume
    assert F.cumulative_volume([b for b in bars if b["start"] != open_at + timedelta(minutes=1)], open_at=open_at, minutes=3) is None   # a gap: never 0
    # ATR(14): the plain mean of 14 true ranges; a gap counts through the previous close; a day jump beyond 25 % leaves it undefined
    days = back(PREV, 15)
    b = [{"session_date": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1} for d in days]
    b[-1] = {**b[-1], "open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0}                          # TR = 106 - 100 (through the close)
    a = F.atr(b)
    assert a["value"] == round((13 * 2.0 + 6.0) / 14, 6) and a["status"] == "AVAILABLE"
    jump = [dict(x) for x in b]
    jump[7] = {**jump[7], "close": 140.0, "high": 141.0}
    assert F.atr(jump)["codes"] == ["DAY_JUMP_IN_WINDOW"] and F.atr(b[1:])["codes"] == ["SESSIONS_MISSING"]
    assert F.atr([{**x, "high": 98.0} for x in b])["codes"] == ["INVALID_BAR"]
    last = F.last_trade(s, today=TODAY, sessions=("REG",))
    im = F.intraday_move(last, s, a, today=TODAY)
    assert (im["value"], im["atr_units"]) == (round(100 * (186.0 / 185.4 - 1), 6), round(0.6 / a["value"], 6))
    uv = F.unusual_volatility(s, a, today=TODAY)
    assert uv["value"] == round((187.0 - 184.4) / a["value"], 6)
    flat = F.atr([{**x, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0} for x in b])          # a zero ATR is never a divisor
    assert flat["value"] == 0.0 and F.unusual_volatility(s, flat, today=TODAY)["codes"] == ["ATR_UNAVAILABLE"]
    assert F.intraday_move(last, s, flat, today=TODAY)["atr_units"] is None
    old = snap(quote_at=T - 2 * S, prev_close=180.0, open_=185.4, last=186.0, trade_at=ny(PREV, 15, 59))
    assert F.last_trade(old, today=TODAY)["codes"] == ["LAST_TRADE_NOT_TODAY"]                             # yesterday's trade is no price today
    pre_trade = snap(quote_at=T - 2 * S, prev_close=180.0, open_=185.4, last=186.0, trade_at=ny(TODAY, 9, 29))
    assert F.last_trade(pre_trade, today=TODAY, sessions=("REG",))["codes"] == ["LAST_TRADE_IN_PRE"]
    assert F.intraday_move(F.last_trade(pre_trade, today=TODAY, sessions=("REG",)), pre_trade, a, today=TODAY)["codes"] == ["LAST_TRADE_IN_PRE"]
    etf, spy = F.feature(1.5, "AVAILABLE"), F.feature(0.5, "AVAILABLE")
    assert F.sector_strength("Semiconductors", "SOXX", etf, spy)["value"] == 1.0
    stale = F.sector_strength("Semiconductors", "SOXX", F.feature(1.5, "STALE"), spy)
    assert (stale["value"], stale["status"], stale["usable"]) == (1.0, "STALE", False)                     # shown, never used
    assert F.sector_strength(None, None, etf, spy)["status"] == "NOT_APPLICABLE"
    dv = F.dollar_volume([{"session_date": d, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": v}
                          for d, v in zip(back(PREV, 20), list(range(1, 20)) + [1000])], label=["IEX_ONLY"])
    assert (dv["value"], dv["codes"]) == (105.0, ["IEX_ONLY"])                                             # the median, not the mean (595)
    sp = F.spread_bps([{"bid": 99.95, "ask": 100.05}] * 4 + [{"bid": 99.0, "ask": 101.0}], feed="iex")
    assert (sp["value"], sp["codes"]) == (10.0, ["NON_REPRESENTATIVE_IEX"])
    assert F.spread_bps([{"bid": 100.0, "ask": 99.0}] * 5, feed="sip")["codes"] == ["INVALID_QUOTE"]
    assert F.spread_bps([{"bid": 99.95, "ask": 100.05}] * 4, feed="sip")["codes"] == ["FEWER_THAN_5_SNAPSHOTS"]
    pre0 = ny(TODAY, 8)
    pm = F.premarket([{"start": pre0, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 5},
                      {"start": pre0 + timedelta(minutes=7), "open": 11.0, "high": 11.5, "low": 10.0, "close": 11.0, "volume": 7}], feed="iex", minutes_elapsed=240)
    assert (pm["premarket_volume"]["value"], pm["premarket_range"]["value"], pm["premarket_range"]["codes"]) == (12.0, round(100 * (12 / 9 - 1), 6), ["THIN_ON_IEX"])
    assert (pm["premarket_volume"]["codes"], pm["premarket_volume"]["minutes_covered"], pm["premarket_volume"]["minutes_elapsed"]) == (
        ["SUM_OF_DELIVERED_BARS", "THIN_ON_IEX"], 2, 240)                                              # the coverage is stated, never filled
    for bad in (float("nan"), float("inf"), True, "1", None):
        assert F.num(bad) is None
    assert F.worst(["AVAILABLE", "NOT_APPLICABLE", "STALE"]) == "STALE" and F.worst(["UNAVAILABLE", "INSUFFICIENT_DATA"]) == "INSUFFICIENT_DATA"
    assert F.feature(None, "AVAILABLE")["status"] == "INSUFFICIENT_DATA"                                    # nothing to show is never AVAILABLE


def test_S02_a_whole_scan_computes_every_feature_and_ranks_the_gap_list(tmp_path):
    w = world(tmp_path / "w")
    doc = scan(w)
    assert (doc["run"]["status"], doc["run"]["codes"], doc["run"]["quality"]["verdict"]) == ("COMPLETED", [], "PASS")
    n = res(doc, "NVDA")["features"]
    got = {k: n[k]["value"] for k in ("gap_pct", "price_intraday", "atr_14", "intraday_move", "unusual_volatility", "day_change", "relative_volume_tod",
                                      "dollar_volume_20d", "spread_bps", "price_swing", "sector_strength")}
    assert got == {"gap_pct": 3.0, "price_intraday": 186.0, "atr_14": 3.6, "intraday_move": round(100 * (186 / 185.4 - 1), 6), "unusual_volatility": round(2.6 / 3.6, 6),
                   "day_change": round(100 * (186 / 180 - 1), 6), "relative_volume_tod": 3.0, "dollar_volume_20d": 180_000_000.0,
                   "spread_bps": round(10_000 * 0.02 / 186.0, 6), "price_swing": 180.0, "sector_strength": 1.0}
    assert all(n[k]["status"] == "AVAILABLE" for k in got) and n["intraday_move"]["atr_units"] == round(0.6 / 3.6, 6)
    assert (n["relative_volume_tod"]["minutes"], n["relative_volume_tod"]["today_volume"], n["relative_volume_tod"]["baseline_volume"]) == (29, 8700, 2900)   # to t = 09:59
    assert n["dollar_volume_20d"]["codes"] == ["IEX_ONLY"] and n["spread_bps"]["codes"] == ["NON_REPRESENTATIVE_IEX"]
    assert n["premarket_volume"]["codes"] == ["NO_PREMARKET_BARS"] and n["premarket_volume"]["value"] is None
    assert doc["sequence"][MS.INTRADAY][:4] == ["NVDA", "AMD", "SOXX", "SPY"] and [hz(doc, s)["rank"] for s in ("NVDA", "AMD", "SOXX", "SPY")] == [1, 2, 3, 4]
    assert fv(doc, "AMD", "gap_pct")[0] == fv(doc, "SOXX", "gap_pct")[0] == 1.0                            # a tie: broken by symbol
    assert doc["horizons"] == {MS.INTRADAY: {"applies": True, "reason": None, "eligible": 4, "ranked": 4, "symbols": 19, "codes": []},
                               MS.SWING: {"applies": True, "reason": None, "eligible": 4, "ranked": 0, "symbols": 19, "codes": []}}
    assert all(hz(doc, s, MS.SWING)["rank"] is None for s in doc["sequence"][MS.SWING]) and doc["sequence"][MS.SWING] == sorted(doc["sequence"][MS.SWING])
    shop = res(doc, "SHOP")
    assert set(hz(doc, "SHOP")["reasons"]) == {"QUOTE_NEVER_OBSERVED", "PRICE_UNAVAILABLE"} and hz(doc, "SHOP")["rank"] is None
    assert set(hz(doc, "SHOP", MS.SWING)["reasons"]) == {"DAILY_BAR_NEVER_OBSERVED", "PRICE_UNAVAILABLE"}
    assert shop["sector"] is None and shop["features"]["sector_strength"]["status"] == "NOT_APPLICABLE"     # Unmapped: no sector
    assert res(doc, "NVDA")["flags"]["halt"]["value"] == "FALSE" and doc["context"]["feeds"]["halt"]["status"] == "AVAILABLE"
    assert R.scan_sha256(doc) == doc["scan_sha256"] and "invocation" in doc


def test_S03_stale_inputs_fail_closed_per_horizon_and_aging_ones_do_not(tmp_path):
    w = world(tmp_path / "w")
    aging = scan(w, T + 10 * S)                                                                  # the last quote is 12 s old: AGING, usable
    assert hz(aging, "NVDA")["eligible"] and res(aging, "NVDA")["quote"]["day"]["freshness"] == "AGING"
    stale = scan(w, T + 20 * S)                                                                  # 22 s: STALE under QUOTE_DAY (> 15 s)
    assert hz(stale, "NVDA")["reasons"] == ["QUOTE_AGE_LIMIT"] and hz(stale, "NVDA")["rank"] is None and stale["horizons"][MS.INTRADAY]["ranked"] == 0
    assert fv(stale, "NVDA", "gap_pct") == (3.0, "STALE") and not res(stale, "NVDA")["features"]["gap_pct"]["usable"]   # shown, never used
    assert hz(stale, "NVDA", MS.SWING)["eligible"]                                               # close-based signals are unaffected
    blind = scan(w, T + 200 * S)                                                                 # the halt feed was last seen 230 s ago: STALE
    assert res(blind, "NVDA")["flags"]["halt"]["value"] == "UNKNOWN" and res(blind, "NVDA")["flags"]["halt"]["codes"] == ["HALT_FEED_AGE_LIMIT"]
    assert "HALT_STATUS_UNKNOWN" in hz(blind, "NVDA")["reasons"] and hz(blind, "NVDA", MS.SWING)["eligible"]   # fails closed for day only
    late = scan(w, ny(TODAY, 18, 30))                                                            # after 18:00 today's bar is expected
    assert hz(late, "NVDA", MS.SWING)["reasons"] == ["DAILY_BAR_SESSION_MISSING", "PRICE_UNAVAILABLE"]
    assert res(late, "NVDA")["daily"]["expected_session"] == TODAY.isoformat()


def test_S04_a_scan_is_deterministic_reproducible_from_its_inputs_and_independent_of_where_the_files_lie(tmp_path):
    w = world(tmp_path / "w")
    a, b = scan(w), scan(w)
    assert R.dumps(a) == R.dumps(b) and json.loads(R.dumps(a)) == a
    moved = tmp_path / "elsewhere"
    shutil.copytree(w.tmp, moved)
    c = R.run_scan(moved / "mi.db", as_of=T, archive_root=moved / "raw", core=TEST_CORE, watchlist=WATCH)
    assert c["scan_sha256"] == a["scan_sha256"] and c["invocation"] != a["invocation"]          # paths are outside the hash
    assert {k: v for k, v in c.items() if k != "invocation"} == {k: v for k, v in a.items() if k != "invocation"}
    assert scan(w, watchlist=("CLS", "NVDA", "cls"))["universe"]["sha256"] == a["universe"]["sha256"]          # the universe is a set
    other = scan(w, at=T + S)
    assert other["run"]["inputs_sha256"] != a["run"]["inputs_sha256"] and other["scan_sha256"] != a["scan_sha256"]
    tie = {s: {"horizons": {MS.INTRADAY: {"eligible": True, "rank": None}}, "features": {"gap_pct": {"usable": True, "value": 1.0}}} for s in ("ZZZ", "AAA", "MMM")}
    assert FI.sequence(tie, MS.INTRADAY) == ["AAA", "MMM", "ZZZ"] and [tie[s]["horizons"][MS.INTRADAY]["rank"] for s in ("AAA", "MMM", "ZZZ")] == [1, 2, 3]
    with pytest.raises(ValueError):
        FI.sequence(tie, "ACROSS")                                                               # nothing is ranked across horizons


def test_S05_the_74_gate_blocks_on_block_withholds_on_insufficient_data_and_warns_on_warn(tmp_path):
    w = world(tmp_path / "w")
    no_archive = scan(w, archive_root=None)                                                      # seals cannot be confirmed: INSUFFICIENT_DATA
    assert (no_archive["run"]["status"], no_archive["run"]["codes"]) == ("INSUFFICIENT_DATA", ["QUALITY_INSUFFICIENT_DATA"])
    assert no_archive["horizons"][MS.INTRADAY]["eligible"] == no_archive["horizons"][MS.SWING]["eligible"] == no_archive["horizons"][MS.INTRADAY]["ranked"] == 0
    assert fv(no_archive, "NVDA", "gap_pct") == (3.0, "AVAILABLE") and hz(no_archive, "NVDA")["reasons"] == ["QUALITY_INSUFFICIENT_DATA"]   # never an opportunity
    out = io.StringIO()
    assert R.main(["--store", str(w.store.path), "--as-of", ST.ts(T)], out=out) == 3
    w.clk.t = T - 0.5 * S
    open_did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="QUOTE_SNAPSHOT", provider="ALPACA", feed="iex", adjustment="none", symbols=["ZZZ"]))
    warn = scan(w)                                                                               # an unsealed dataset: PENDING_FINALIZATION
    assert (warn["run"]["status"], warn["run"]["quality"]["verdict"], warn["run"]["codes"]) == ("COMPLETED", "WARN", ["QUALITY_WARN"])
    assert {x["check"] for x in warn["run"]["gate"]["warnings"]} >= {"integrity"} and hz(warn, "NVDA")["eligible"] and hz(warn, "NVDA")["rank"] == 1
    assert R.main(["--store", str(w.store.path), "--archive-root", str(w.archive.root), "--as-of", ST.ts(T)], out=io.StringIO()) == 1
    raw_insert(w.store.path, "mi_observations", {"obs_id": "bad-time", "natural_key": "QUOTE_SNAPSHOT|bad", "observed_at": "2025-12-01 05:00:00",
                                                  "symbol": "ZZZ", "dataset_id": open_did}, where_symbol="AMD", where_kind="QUOTE_SNAPSHOT")   # a malformed time
    blocked = scan(w)
    assert blocked["run"]["quality"]["verdict"] == "BLOCK" and (blocked["run"]["status"], blocked["run"]["codes"]) == ("BLOCKED", ["QUALITY_BLOCK"])
    assert fv(blocked, "NVDA", "gap_pct") == (3.0, "AVAILABLE") and hz(blocked, "NVDA")["reasons"] == ["QUALITY_BLOCK"]   # the gap never outweighs it
    assert all(not r["horizons"][h]["eligible"] and r["horizons"][h]["rank"] is None for r in blocked["results"] for h in MS.HORIZONS)
    assert R.main(["--store", str(w.store.path), "--archive-root", str(w.archive.root), "--as-of", ST.ts(T)], out=io.StringIO()) == 2
    assert FI.quality_gate(None)["actionable"] is False and FI.quality_gate("SOMETHING")["codes"] == ["QUALITY_NOT_ESTABLISHED"]


def test_S06_a_scan_as_of_t_never_sees_anything_recorded_after_t(tmp_path):
    w = world(tmp_path / "w")
    first = scan(w)
    w.clk.t = T + 5 * S
    add_snaps(w, [("AMD", T + 4 * S, snap(quote_at=T + 4 * S, prev_close=210.0, open_=252.0, last=250.0))])            # a big gap, quoted after T
    add_snaps(w, [("SPY", T - 1 * S, snap(quote_at=T - 1 * S, prev_close=680.0, open_=700.0, last=700.0))], retrieved_after=40)   # quoted before T, arrived after
    w.clk.t = T + 50 * S
    add_daily(w, "NVDA", {PREV: 150.0})                                                          # a revision of the last session, after T
    halt_poll(w, T + 55 * S, [halt_obs("NVDA", T - 60 * S, at=T + 55 * S)])                      # a halt that began before T, reported after
    w.clk.t = T + 70 * S
    add_minutes(w, "NVDA", {ny(TODAY, 9, 59): 99_999})                                           # the 09:59 bar, complete at 10:00, ingested late
    halt_poll(w, T - 1 * S, ingest_at=T + 80 * S)                                                # a poll retrieved before T, recorded after it
    again = scan(w)
    assert again["results"] == first["results"] and again["sequence"] == first["sequence"] and again["context"] == first["context"]
    later = scan(w, T + 60 * S)
    assert fv(later, "AMD", "gap_pct")[0] == 20.0 and res(later, "NVDA")["flags"]["halt"]["value"] == "TRUE"          # and a later scan sees them
    assert fv(later, "NVDA", "price_swing")[0] == 150.0 and "HALTED" in hz(later, "NVDA")["reasons"]
    assert fv(scan(w, T + 75 * S), "NVDA", "relative_volume_tod")[0] > fv(first, "NVDA", "relative_volume_tod")[0]


def test_S07_missing_stale_malformed_and_contradictory_inputs_are_never_estimated(tmp_path):
    core = DUC.Core("core_file", "t", tuple((s, "Semiconductors") for s in ("NVDA", "AMD", "MU", "AVGO", "MRVL", "QCOM", "TXN", "INTC")), "t")
    w = world(tmp_path / "w", daily={**BASE, "MU": 4.0, "AVGO": 100.0, "MRVL": 50.0}, snaps=[])
    d15 = back(PREV, 15)
    add_daily(w, "QCOM", {d: (100.0 if i != 7 else 140.0) for i, d in enumerate(d15)})          # a 40 % jump: a split or an extreme move
    add_daily(w, "TXN", {d: 100.0 for d in back(PREV, 12)})                                      # 12 of the 20 sessions dollar volume needs
    add_daily(w, "AMD", {d: 210.0 for d in back(PREV, 20)}, feed="sip", volume=2_000_000)        # 20 SIP sessions: dollar volume prefers them
    rows = base_snaps()
    q = T - 2 * S
    rows += [("MU", q, snap(quote_at=q, prev_close=4.0, open_=4.1, last=4.2)),                    # under $5
             ("AVGO", q, snap(quote_at=q, prev_close=100.0, open_=101.0, last=101.0, daily_day=PREV)),                # the provider did not roll the day
             ("MRVL", q, snap(quote_at=q, prev_close=50.0, open_=51.0, last=51.0, bid=51.1, ask=50.9)),               # crossed: never usable
             ("QCOM", q, snap(quote_at=q, prev_close=100.0, open_=102.0, last=103.0)),
             ("TXN", q, snap(quote_at=q, prev_close=100.0, open_=100.0, last=100.0, prev_day=back(PREV, 3)[0])),        # not the previous session
             ("INTC", q - 30 * S, snap(quote_at=q - 30 * S, prev_close=20.0, open_=21.0, last=21.0))]
    add_snaps(w, rows, absent=[("INTC", q)])                                                     # INTC: a newer absence after an old value
    doc = scan(w, core=core, watchlist=())
    assert set(hz(doc, "MU")["reasons"]) == {"PRICE_BELOW_MIN"} and "PRICE_BELOW_MIN" in hz(doc, "MU", MS.SWING)["reasons"]
    assert res(doc, "AVGO")["features"]["gap_pct"]["codes"] == ["DAILY_BAR_NOT_TODAY"] and hz(doc, "AVGO")["eligible"] and hz(doc, "AVGO")["rank"] is None
    assert res(doc, "AVGO")["features"]["intraday_move"]["codes"] == ["DAILY_BAR_NOT_TODAY"] and fv(doc, "AVGO", "unusual_volatility")[0] is None
    assert (fv(doc, "AMD", "dollar_volume_20d"), res(doc, "AMD")["features"]["dollar_volume_20d"]["codes"]) == ((420_000_000.0, "AVAILABLE"), ["SIP"])
    assert res(doc, "NVDA")["features"]["dollar_volume_20d"]["codes"] == ["IEX_ONLY"]                       # no SIP history: IEX, labelled
    assert hz(doc, "MRVL")["reasons"] == ["PRICE_UNAVAILABLE", "QUOTE_NO_USABLE_VALUE_KNOWN_AT"] and fv(doc, "MRVL", "gap_pct")[0] is None
    assert res(doc, "MRVL")["inputs"]["quote"]["quality_flags"] == ["CROSSED"]
    assert res(doc, "QCOM")["features"]["atr_14"]["codes"] == ["DAY_JUMP_IN_WINDOW"] and res(doc, "QCOM")["features"]["intraday_move"]["atr_units"] is None
    assert res(doc, "QCOM")["features"]["unusual_volatility"]["codes"] == ["ATR_UNAVAILABLE"]
    dv = res(doc, "TXN")["features"]["dollar_volume_20d"]
    assert dv["value"] is None and "SESSIONS_MISSING" in dv["codes"] and len(dv["missing_sessions"]) == 8
    assert "PREV_CLOSE_NOT_PREVIOUS_SESSION" in res(doc, "TXN")["features"]["gap_pct"]["codes"]
    assert hz(doc, "INTC")["reasons"] == ["PRICE_UNAVAILABLE", "QUOTE_LATEST_RECORD_IS_ABSENCE"] and fv(doc, "INTC", "gap_pct")[0] is None   # never the old quote
    rv = res(doc, "AMD")["features"]["relative_volume_tod"]
    assert rv["codes"] == ["NO_BARS_TODAY"] and rv["value"] is None                              # no minute bars: never a zero
    w2 = world(tmp_path / "w2", minutes=False)
    add_minutes(w2, "NVDA", base_minutes(prior=back(PREV, 10)[1:]))                              # the oldest prior session is missing
    rv2 = res(scan(w2), "NVDA")["features"]["relative_volume_tod"]
    assert rv2["codes"] == ["PRIOR_SESSIONS_MISSING"] and rv2["missing_sessions"] == [back(PREV, 10)[0].isoformat()]


def test_S08_duplicate_deliveries_count_once_and_the_revision_known_at_t_is_used(tmp_path):
    w = world(tmp_path / "w")
    add_minutes(w, "NVDA", base_minutes())                                                       # the same minute bars delivered again
    assert fv(scan(w, T + 2 * S), "NVDA", "relative_volume_tod")[0] == 3.0
    w.clk.t = T - 0.2 * S
    add_daily(w, "NVDA", {PREV: 170.0}, high=172.0, low=168.0)                                   # a revision of the last session, known before T
    doc = scan(w, T + 2 * S)
    assert fv(doc, "NVDA", "price_swing")[0] == 170.0
    tr_last = max(172.0, 180.0) - min(168.0, 180.0)
    assert fv(doc, "NVDA", "atr_14")[0] == round((13 * 3.6 + tr_last) / 14, 6) and res(doc, "NVDA")["inputs"]["daily_bars"]["count"] == 15
    key = next(r["natural_key"] for r in w.store.history("BAR_1DAY", symbol="NVDA", feed="iex", adjustment="raw", as_of=T)
               if r["session_date"] == PREV.isoformat())
    assert [r["payload"]["close"] for r in w.store.revisions(key)] == [180.0, 170.0]              # two revisions; the scan used the one known


def test_S09_every_figure_carries_its_provenance_and_evidence_class(tmp_path):
    w = world(tmp_path / "w")
    doc = scan(w)
    n = res(doc, "NVDA")
    q = n["inputs"]["quote"]
    assert q["evidence"] == "FIXTURE" and q["record_type"] == "VALUE" and q["provider"] == "ALPACA" and q["feed"] == "iex"
    assert all(q[k] <= ST.ts(T) for k in ("observed_at", "available_at", "ingested_at", "retrieved_at"))
    assert n["inputs"]["daily_bars"]["count"] == 15 and n["inputs"]["daily_bars"]["evidence"] == ["OPERATOR"]
    assert n["inputs"]["minute_bars"]["count"] == 329 and n["inputs"]["spread_snapshots"]["count"] == 5 and n["evidence"] == ["FIXTURE", "OPERATOR"]
    assert doc["run"]["quality"]["evidence_scope"] == "INCLUDES_OPERATOR_DATA" and doc["run"]["quality"]["not_proof_of_live_readiness"] is True
    fx = world(tmp_path / "fx", job="FIXTURE")
    f = scan(fx)
    assert f["run"]["quality"]["evidence_scope"] == "FIXTURE_ONLY" and res(f, "NVDA")["evidence"] == ["FIXTURE"]
    assert "not production observations" in f["disclaimer"] and "not proof of live-data readiness" in f["disclaimer"]
    assert "FIXTURE_ONLY - fixture data only - NOT production observations" in R.render_summary(f)
    assert doc["deferred"] == {MS.DEFERRED: list(MS.BLOCKED_BY_DEFERRED_7_3_WIRING)} and len(doc["deferred"][MS.DEFERRED]) == 4
    assert doc["blocked"].keys() == {"SEC_ACCEPTANCE_TZ_UNVERIFIED"} and doc["not_pre_registered"] == list(MS.NOT_PRE_REGISTERED)


def test_S10_the_day_and_swing_horizons_are_kept_apart(tmp_path):
    w = world(tmp_path / "w")
    add_snaps(w, [("NVDA", q, snap(quote_at=q, prev_close=180.0, open_=185.4, last=186.0, bid=180.0, ask=190.0))
                  for q in (ny(TODAY, 16, 5), ny(TODAY, 16, 10), ny(TODAY, 16, 15))])                  # three wide POST quotes
    post = scan(w, ny(TODAY, 16, 30))
    assert fv(post, "NVDA", "spread_bps")[0] == round(10_000 * 0.02 / 186.0, 6)                # the spread reads regular-session quotes only
    assert post["horizons"][MS.INTRADAY] == {"applies": False, "reason": "OUTSIDE_DAY_WINDOW_POST", "eligible": 0, "ranked": 0, "symbols": 19, "codes": []}
    assert hz(post, "NVDA") == {"state": "NOT_APPLICABLE", "eligible": False, "reasons": ["OUTSIDE_DAY_WINDOW_POST"], "rank": None}
    assert hz(post, "NVDA", MS.SWING)["eligible"]                                                # swing is evaluated after the close
    sat = scan(w, ny(date(2025, 12, 6), 12))
    assert sat["horizons"][MS.INTRADAY]["reason"] == "NOT_A_MARKET_DAY" and hz(sat, "NVDA")["state"] == "NOT_APPLICABLE"
    assert hz(sat, "NVDA", MS.SWING)["reasons"] == ["DAILY_BAR_SESSION_MISSING", "PRICE_UNAVAILABLE"]   # Friday's bar is due: swing is judged
    pre = world(tmp_path / "pre", snaps=[], minutes=False, halt_at=ny(TODAY, 8, 59, 30))
    qa = ny(TODAY, 8, 59, 58)
    add_snaps(pre, [(s, qa, snap(quote_at=qa, prev_close=BASE[s], open_=BASE[s], last=BASE[s] * 1.02, trade_at=qa - S)) for s in BASE])
    p = scan(pre, ny(TODAY, 9, 0))
    assert p["horizons"][MS.INTRADAY]["applies"] and p["horizons"][MS.INTRADAY]["eligible"] == 0             # no premarket quote policy exists
    assert fv(p, "NVDA", "gap_pct") == (2.0, "NOT_APPLICABLE") and res(p, "NVDA")["features"]["gap_pct"]["basis"] == "PRE_LAST_TRADE"
    assert "QUOTE_OUTSIDE_WINDOW" in hz(p, "NVDA")["reasons"] and "HALT_STATUS_UNKNOWN" in hz(p, "NVDA")["reasons"]   # 7.0: both REG-only
    halted = world(tmp_path / "h", halt_at=None)
    halt_poll(halted, T - 0.6 * S, [halt_obs("AMD", T - 300 * S, at=T - 0.6 * S)])
    h = scan(halted)
    assert "HALTED" in hz(h, "AMD")["reasons"] and "HALTED" in hz(h, "AMD", MS.SWING)["reasons"]          # a halt blocks both horizons
    assert all(r["horizons"][MS.SWING]["rank"] is None for r in h["results"])


def test_S11_the_halt_flag_is_true_false_or_unknown_never_assumed(tmp_path):
    fresh = {"status": "AVAILABLE", "usable": True, "reason": "OK", "age_s": 5.0}
    stale = {"status": "STALE", "usable": False, "reason": "AGE_LIMIT", "age_s": 400.0}
    row = lambda h, r=None: {"obs_id": "x", "observed_at": ST.ts(h), "payload": {"resumed_at": r if r is None or isinstance(r, str) else r.isoformat()}}  # noqa: E731
    assert FG.halt_flag([row(T - 60 * S)], stale, now=T)["value"] == "TRUE"                               # a known open halt is a fact
    assert FG.halt_flag([row(T - 900 * S, T - 540 * S)], fresh, now=T)["codes"] == ["RESUMED_WITHIN_10_MIN"]
    assert FG.halt_flag([row(T - 900 * S, T - 660 * S)], fresh, now=T)["value"] == "FALSE"
    assert FG.halt_flag([row(T - 900 * S, T + 60 * S)], fresh, now=T)["codes"] == ["HALT_OPEN"]           # a resumption still to come
    assert FG.halt_flag([row(ny(PREV, 15, 50))], fresh, now=T)["codes"] == ["HALT_OPEN"]                  # halted yesterday, never resumed: open today
    assert FG.halt_flag([row(ny(PREV, 11), ny(PREV, 11, 30))], fresh, now=T)["value"] == "FALSE"          # yesterday's halt, resumed yesterday
    assert FG.halt_flag([row(T - 900 * S, "soon")], fresh, now=T)["codes"] == ["HALT_RESUMPTION_UNREADABLE"]   # never read as resumed
    assert FG.halt_flag([], stale, now=T) == {"value": "UNKNOWN", "codes": ["HALT_FEED_AGE_LIMIT"], "feed": {"status": "STALE", "reason": "AGE_LIMIT",
                                                                                                             "age_s": 400.0}}
    w = world(tmp_path / "w", halt_at=None)
    assert res(scan(w), "NVDA")["flags"]["halt"]["codes"] == ["HALT_FEED_NEVER_OBSERVED"]
    halt_poll(w, T - 0.7 * S, status="HTTP_ERROR")
    assert res(scan(w), "NVDA")["flags"]["halt"]["value"] == "UNKNOWN"                                    # a failed poll is not the feed working
    halt_poll(w, T - 0.6 * S, [halt_obs("NVDA", T - 900 * S, T - 300 * S, at=T - 0.6 * S)])
    d = scan(w)
    assert res(d, "NVDA")["flags"]["halt"]["codes"] == ["RESUMED_WITHIN_10_MIN"] and res(d, "AMD")["flags"]["halt"]["value"] == "FALSE"


def _news_world(tmp, *, published, computed_at=None, headline="Nvidia (NASDAQ: NVDA) beats estimates"):
    """A v2 world, then one Reuters article (tier 3) polled just before T, an entity map and a MENTIONS run as of T - 1.1 s, computed at
    `computed_at` (default: then). The clock only moves forward."""
    w = world(tmp, v2=True)
    w.clk.t = T - 1.3 * S
    did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="NEWS", provider="ALPACA", feed="none", adjustment="none", symbols=[]))
    body = news([dict(art(1, published, headline=headline, symbols=("NVDA",)), source="Reuters", url="https://www.reuters.com/markets/nvda-beats"),
                 dict(art(2, published, headline="Advanced Micro Devices (NASDAQ: AMD) rallies", symbols=("AMD",)), source="Some Blog",
                      url="https://someblog.example/amd")]).body                                 # AMD: PRIMARY, but tier 4 - never a catalyst
    w.store.record_fetch(FetchRecord(provider="ALPACA", endpoint=AN.ENDPOINT, started_at=w.clk(), retrieved_at=w.clk(), status="OK"), dataset_id=did)
    w.store.ingest(did, AN.parse_page(body, symbols=[], retrieved_at=w.clk(), dataset_id=did)[0])
    seal(w, did)
    w.clk.t = T - 1.2 * S
    msha = w.store.record_entity_map({"sec_names": {"NVDA": "NVIDIA CORP", "AMD": "ADVANCED MICRO DEVICES INC"}, "universe": ["NVDA", "AMD"], "aliases": {}})
    run_at = T - 1.1 * S
    w.clk.t = computed_at or run_at
    NP.mention_run(w.store, as_of=run_at, window_start=published - timedelta(hours=1), window_end=run_at, map_sha256=msha)
    return w


def test_S12_the_catalyst_flag_is_true_or_not_established_never_false(tmp_path):
    w = _news_world(tmp_path / "w", published=T - timedelta(hours=2))
    doc = scan(w, T + 2 * S)
    c = res(doc, "NVDA")["flags"]["catalyst"]
    assert c["value"] == "TRUE" and c["codes"] == ["PRIMARY_NEWS_MENTION"] and c["articles"][0]["method"] == "EXCHANGE_TAG"
    assert res(doc, "AMD")["flags"]["catalyst"]["codes"] == ["NO_CATALYST_MENTION_KNOWN", "SEC_ACCEPTANCE_TZ_UNVERIFIED"]
    assert doc["context"]["feeds"]["news"]["status"] == "AVAILABLE"
    old = _news_world(tmp_path / "old", published=T - timedelta(hours=19))                      # outside the 18 h window
    assert res(scan(old, T + 2 * S), "NVDA")["flags"]["catalyst"]["value"] == "NOT_ESTABLISHED"
    late = _news_world(tmp_path / "late", published=T - timedelta(hours=2), computed_at=T + 60 * S)   # as of before T, computed after T
    lc = res(scan(late, T + 2 * S), "NVDA")["flags"]["catalyst"]
    assert lc["value"] == "NOT_ESTABLISHED" and "NO_MENTION_RUN_KNOWN" in lc["codes"]
    assert res(scan(late, T + 90 * S), "NVDA")["flags"]["catalyst"]["value"] == "TRUE"
    v1 = res(scan(world(tmp_path / "v1")), "NVDA")["flags"]["catalyst"]                       # no mention tables, and no news poll seen
    assert (v1["value"], v1["codes"]) == ("UNAVAILABLE", ["NEWS_MENTIONS_NOT_IN_SCHEMA_V1", "NEWS_POLL_NEVER_OBSERVED", "SEC_ACCEPTANCE_TZ_UNVERIFIED"])
    quiet = res(scan(w, T + 700 * S), "AMD")["flags"]["catalyst"]                               # the news poll is stale: never "no news"
    assert (quiet["value"], "NEWS_POLL_AGE_LIMIT" in quiet["codes"]) == ("UNAVAILABLE", True)
    assert res(scan(w, T + 700 * S), "NVDA")["flags"]["catalyst"]["value"] == "TRUE"             # a known article is still a fact
    for d in (doc, scan(old, T + 2 * S), scan(late, T + 2 * S)):
        assert {r["flags"]["catalyst"]["value"] for r in d["results"]} <= {"TRUE", "NOT_ESTABLISHED", "UNAVAILABLE"}   # never FALSE: SEC is blocked
    assert hz(doc, "NVDA") == hz(scan(world(tmp_path / "plain", v2=True), T + 2 * S), "NVDA")    # a flag, never an eligibility filter


AV_CSV = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\nNVDA,NVIDIA,2025-12-09,2025-10-31,1.10,USD\r\nAMD,AMD,2025-11-20,2025-09-30,,USD\r\n"
          "SHOP,Shopify,2025-12-06,2025-09-30,,USD\r\n")                                          # SHOP: a Saturday date counts the market days only


def _earnings(w, at, csv=AV_CSV):
    at_least(w, at)
    did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="EARNINGS", provider="ALPHA_VANTAGE", feed="none", adjustment="none", symbols=[]))
    w.store.record_fetch(FetchRecord(provider="ALPHA_VANTAGE", endpoint=EA.ENDPOINT, started_at=at, retrieved_at=at, status="OK"), dataset_id=did)
    w.store.ingest(did, EA.parse(csv.encode(), retrieved_at=at, dataset_id=did)[0])
    seal(w, did)


def test_S13_earnings_proximity_needs_a_current_calendar_and_reads_expected_dates_only(tmp_path):
    w = world(tmp_path / "w")
    _earnings(w, T - 0.8 * S)
    doc = scan(w)
    e = res(doc, "NVDA")["flags"]["earnings_proximity"]
    assert (e["value"], e["sessions"], e["report_date"], e["time_precision"]) == ("KNOWN", 5, "2025-12-09", "DATE_ONLY")      # Wed..Tue next week
    assert e["codes"] == ["EXPECTED_NOT_CONFIRMED", "SEC_ACCEPTANCE_TZ_UNVERIFIED"]
    assert res(doc, "AMD")["flags"]["earnings_proximity"]["codes"] == ["EARNINGS_UNKNOWN", "NO_UPCOMING_DATE_IN_CALENDAR"]   # only a past date
    assert res(doc, "SPY")["flags"]["earnings_proximity"]["codes"] == ["EARNINGS_UNKNOWN", "NOT_IN_LATEST_CALENDAR"]
    assert (e["calendar"]["snapshot_date"], e["evidence"]) == (TODAY.isoformat(), "FIXTURE") and e["obs_id"]
    assert res(doc, "SHOP")["flags"]["earnings_proximity"]["sessions"] == 3                    # Wed, Thu, Fri before Saturday 2025-12-06
    stale = res(scan(w, T + timedelta(hours=40)), "NVDA")["flags"]["earnings_proximity"]
    assert stale["value"] == "UNKNOWN" and stale["codes"] == ["EARNINGS_CALENDAR_AGE_LIMIT", "EARNINGS_UNKNOWN"]
    assert res(scan(world(tmp_path / "none")), "NVDA")["flags"]["earnings_proximity"]["codes"] == ["EARNINGS_CALENDAR_NEVER_OBSERVED", "EARNINGS_UNKNOWN"]


def test_S14_the_regime_reuses_rotation_walkforward_unchanged_and_the_macro_flags_stay_display_only(tmp_path):
    from backtest.replay import BarSeries
    from rotation_walkforward import regimes as RG
    days = back(PREV, 210)
    closes = {d: 400.0 + i for i, d in enumerate(days)}
    w = world(tmp_path / "w", daily={k: v for k, v in BASE.items() if k != "SPY"})
    add_daily(w, "SPY", closes)
    did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="MACRO", provider="FRED", feed="none", adjustment="none", symbols=[]))
    w.store.ingest(did, [Observation(kind="MACRO", series_id="T10Y2Y", provider="FRED", feed="none", adjustment="none", observed_at=ny(PREV), retrieved_at=w.clk(),
                                     available_at=w.clk(), session=CAL.session(ny(PREV))["session"], session_date=PREV, status="AVAILABLE",
                                     source_identifier="vintage:2025-12-01",
                                     payload={"value": -0.3, "date": PREV.isoformat(), "realtime_start": PREV.isoformat(), "realtime_end": "9999-12-31"})])
    seal(w, did)
    g = scan(w)["context"]["regime"]
    want = RG.labels(BarSeries("SPY", [(d.isoformat(), ny(d).isoformat(), c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in sorted(closes.items())]))[PREV.isoformat()]
    assert (g["spy_trend"], g["spy_vol"]) == (want["trend"], want["vol"]) == ("TREND_UP", "LOW_VOL") and g["sessions"] == 200 and g["codes"] == []
    c = g["macro"]["curve_inversion"]
    assert (c["value"], c["inverted"], c["status"], c["reason"], c["display_only"]) == (-0.3, True, "UNAVAILABLE", "NO_RELEASE_SCHEDULE", True)
    assert g["macro"]["vix_percentile"] == {"status": "NOT_APPLICABLE", "reason": "NOT_PRE_REGISTERED"}
    short = scan(world(tmp_path / "short"))["context"]["regime"]                                 # 25 sessions: no SMA200
    assert (short["spy_trend"], short["codes"]) == ("UNKNOWN", ["FEWER_THAN_200_SESSIONS"]) and short["macro"]["curve_inversion"]["value"] is None
    stale = scan(world(tmp_path / "stale"), ny(TODAY, 18, 30))["context"]["regime"]              # today's SPY bar is due and missing
    assert (stale["spy_trend"], stale["spy_vol"], stale["codes"]) == ("UNKNOWN", "UNKNOWN", ["SPY_DAILY_SESSION_MISSING"])
    assert (RG.SMA_SESSIONS, MS.REGIME_TREND_SESSIONS, RG.VOL_SESSIONS + 1, MS.REGIME_VOL_SESSIONS) == (200, 200, 21, 21)


def test_S15_the_universe_is_tier_a_and_the_dynamic_universe_stays_off_with_its_declarations_untouched(tmp_path):
    from scanner.watchlist import load_watchlist
    import config
    u = R.universe()
    core = DUC.load_core()
    live = load_watchlist()
    etfs = sorted({i["etf"] for i in R.SECTOR_MAP.values()})
    assert len(etfs) == 12 and {r["symbol"] for r in u["symbols"]} == set(core.symbols) | set(live) | set(etfs) | {"SPY", "QQQ", "IWM"}
    by = {r["symbol"]: r for r in u["symbols"]}
    assert by["NVDA"]["roles"] == ["CORE", "WATCHLIST"] and by["NVDA"]["sector_etf"] == "SOXX" and by["SHOP"]["sector"] is None
    assert by["SPY"]["roles"] == ["INDEX_PROXY"] and by["XLK"]["roles"] == ["SECTOR_ETF"] and u["count"] == len(by) == 94
    for text in ("NVDA\n# comment\n\namd\nNVDA\n  mu  \n", "", "#only\n"):
        p = tmp_path / "wl.txt"
        p.write_text(text, encoding="utf-8")
        assert R.read_watchlist(p) == (load_watchlist(p), [])                                    # exactly the live loader's rules
    assert R.read_watchlist(tmp_path / "absent.txt")[0] == [] and load_watchlist(tmp_path / "absent.txt") == []
    p.write_text("NVDA\nNOT A TICKER\n", encoding="utf-8")
    bad = R.universe(core=TEST_CORE, watchlist_file=p)
    assert "NOT A TICKER" not in {r["symbol"] for r in bad["symbols"]} and any("NOT A TICKER" in x for x in bad["problems"])
    assert R.read_watchlist()[0] == load_watchlist(config.WATCHLIST_FILE)
    plan = DUP.plan(live)
    assert plan["active"]["equals_live"] is True and plan["active"]["symbols"] == live and next(s for s in plan["stages"] if s["id"] == "active")["state"] == "LIVE_WATCHLIST"
    assert DUS.implemented() == () and len(DUS.SIGNALS) == 6 and plan["mode"] == "OFF"            # the 6.10C declarations stay untouched
    with pytest.raises(NotImplementedError):
        DUS.compute()


def test_S16_the_scan_reads_copies_only_and_leaves_every_input_byte_for_byte_as_it_was(tmp_path, monkeypatch):
    w = world(tmp_path / "w")
    quiet = tmp_path / "quiet"                                                                   # the store alone: no -wal / -shm beside it yet
    quiet.mkdir()
    shutil.copyfile(w.store.path, quiet / "mi.db")
    shutil.copytree(w.archive.root, quiet / "raw")
    control = tmp_path / "control"
    control.mkdir()
    shutil.copyfile(w.store.path, control / "mi.db")
    MarketIntelStore(control / "mi.db").status_counts()                                          # the 7.1 read path, in place, on a WAL file ...
    assert sorted(p.name for p in control.iterdir()) == ["mi.db", "mi.db-shm", "mi.db-wal"]     # ... leaves files beside its input
    before = tree(quiet)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    seen = []
    real = R.Q.build_report

    def spy(store, **kw):
        seen.append(Path(store))
        return real(store, **kw)
    monkeypatch.setattr(R.Q, "build_report", spy)
    doc = R.run_scan(quiet / "mi.db", as_of=T, archive_root=quiet / "raw", core=TEST_CORE, watchlist=WATCH, tmp_root=scratch)
    assert doc["run"]["status"] == "COMPLETED" and tree(quiet) == before and sorted(p.name for p in quiet.iterdir()) == ["mi.db", "raw"]
    assert list(scratch.iterdir()) == [] and len(seen) == 1 and seen[0].parent.parent == scratch   # the 7.4 report read the scan's own copy
    monkeypatch.setattr(R.Q, "build_report", real)
    assert {k: v for k, v in doc.items() if k != "invocation"} == {k: v for k, v in scan(w).items() if k != "invocation"}
    missing = R.run_scan(tmp_path / "nope.db", as_of=T, core=TEST_CORE, watchlist=WATCH)
    assert (missing["run"]["status"], missing["run"]["codes"]) == ("INSUFFICIENT_DATA", ["STORE_NOT_CREATED"]) and not (tmp_path / "nope.db").exists()
    assert len(missing["results"]) == 19 and missing["horizons"][MS.INTRADAY]["eligible"] == 0             # every symbol has its result, not read
    assert all(r["features"]["gap_pct"]["codes"] == ["INPUT_NOT_READ"] and "STORE_NOT_CREATED" in r["horizons"][h]["reasons"]
               for r in missing["results"] for h in MS.HORIZONS)
    st = scan(w, state=tmp_path / "no-state.db")
    assert (st["run"]["status"], st["run"]["codes"]) == ("INSUFFICIENT_DATA", ["STATE_NOT_CREATED"]) and not (tmp_path / "no-state.db").exists()
    c = sqlite3.connect(str(w.store.path))
    try:
        c.execute("PRAGMA wal_autocheckpoint = 0")
        c.execute("INSERT INTO mi_budget_events (at, provider, event) VALUES (?, 'ALPACA', 'ALLOW')", (ST.ts(T - S),))
        c.commit()                                                                               # a writer holding committed pages in -wal
        busy = scan(w, tmp_root=scratch)
        assert (busy["run"]["status"], busy["run"]["codes"]) == ("BLOCKED", ["STORE_WRITER_MAY_BE_ACTIVE"]) and len(busy["results"]) == 19
        assert all("STORE_WRITER_MAY_BE_ACTIVE" in r["horizons"][h]["reasons"] for r in busy["results"] for h in MS.HORIZONS)
        assert busy["horizons"][MS.INTRADAY]["eligible"] == busy["horizons"][MS.SWING]["eligible"] == 0
        assert list(scratch.iterdir()) == []
    finally:
        c.close()


def test_S17_the_cli_prints_json_or_a_summary_and_exits_by_the_run_status(tmp_path):
    w = world(tmp_path / "w")
    args = ["--store", str(w.store.path), "--archive-root", str(w.archive.root), "--as-of", ST.ts(T), "--watchlist", str(tmp_path / "wl.txt")]
    (tmp_path / "wl.txt").write_text("NVDA\nCLS\n", encoding="utf-8")
    core = tmp_path / "core.json"
    core.write_text(json.dumps([{"symbol": "NVDA", "sector": "Semiconductors"}, {"symbol": "AMD", "sector": "Semiconductors"}, {"symbol": "SHOP"}]),
                    encoding="utf-8")
    out = io.StringIO()
    assert R.main(args + ["--core-file", str(core), "--format", "json"], out=out) == 0
    doc = json.loads(out.getvalue())
    assert doc["run"]["status"] == "COMPLETED" and doc["scan_sha256"] == R.scan_sha256(doc) and doc["universe"]["count"] == 19
    lib = scan(w)                                                                                # the same universe through the library
    assert (doc["results"], doc["sequence"], doc["universe"]["sha256"]) == (lib["results"], lib["sequence"], lib["universe"]["sha256"])
    assert doc["universe"]["core"]["source"] == "file core.json"                                 # the core file is its own, recorded source
    out = io.StringIO()
    R.main(args + ["--core-file", str(core)], out=out)
    text = out.getvalue()
    assert "RUN: COMPLETED" in text and "#1   NVDA   gap +3.00 %" in text and "Research candidates only" in text and doc["scan_sha256"] in text
    for bad in (["--store", "x"], ["--store", "x", "--as-of", "2025-12-02T15:00:00"], ["--store", "x", "--as-of", ST.ts(T), "--feed", "both"]):
        o = io.StringIO()
        assert R.main(bad, out=o) == R.USAGE_ERROR, bad
    o = io.StringIO()
    assert R.main(["--store", "x", "--as-of", "2025-12-02T15:00:00"], out=o) == 4 and o.getvalue().startswith("refused:")
    run = subprocess.run([sys.executable, "-m", "market_scan.run", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0 and "Quiet or offline stores only" in run.stdout


# ============================================================================================================ verification (V)
def test_V01_the_figures_recompute_from_raw_sql(tmp_path):
    w = world(tmp_path / "w")
    doc = scan(w)
    shutil.copy(w.store.path, tmp_path / "copy.db")                                              # read a copy: never the input in place
    c = sqlite3.connect(str(tmp_path / "copy.db"))
    try:
        snaps = [json.loads(p) for (p,) in c.execute("SELECT payload_json FROM mi_observations WHERE kind = 'QUOTE_SNAPSHOT' AND symbol = 'NVDA' AND usable = 1 "
                                                      "ORDER BY observed_at")]
        daily = [json.loads(p) for (p,) in c.execute("SELECT payload_json FROM mi_observations WHERE kind = 'BAR_1DAY' AND symbol = 'NVDA' AND feed = 'iex' "
                                                      "ORDER BY observed_at")]
        minutes = c.execute("SELECT observed_at, json_extract(payload_json, '$.volume'), session_date FROM mi_observations WHERE kind = 'BAR_1MIN' "
                            "AND symbol = 'NVDA' AND session = 'REG'").fetchall()
    finally:
        c.close()
    last = snaps[-1]
    assert fv(doc, "NVDA", "gap_pct")[0] == round(100 * (last["daily_bar"]["open"] / last["prev_daily_bar"]["close"] - 1), 6)
    w15 = daily[-15:]
    trs = [max(b["high"], a["close"]) - min(b["low"], a["close"]) for a, b in zip(w15, w15[1:])]
    assert fv(doc, "NVDA", "atr_14")[0] == round(sum(trs) / 14, 6)
    dv = sorted(b["close"] * b["volume"] for b in daily[-20:])
    assert fv(doc, "NVDA", "dollar_volume_20d")[0] == round((dv[9] + dv[10]) / 2, 6)
    sp = sorted(1e4 * (s["ask"] - s["bid"]) / ((s["ask"] + s["bid"]) / 2) for s in snaps[-5:])
    assert fv(doc, "NVDA", "spread_bps")[0] == round(sp[2], 6)
    last_today = max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t, _, d in minutes if d == TODAY.isoformat())
    m = int((last_today + timedelta(minutes=1) - ny(TODAY, 9, 30)).total_seconds() // 60)        # to the end of today's last delivered bar
    by = {}
    for t, v, d in minutes:
        if t < ST.ts(ny(date.fromisoformat(d), 9, 30) + timedelta(minutes=m)):
            by[d] = by.get(d, 0) + v
    today = by.pop(TODAY.isoformat())
    prior = sorted(by.values())
    assert m == 29 and len(prior) == 10 and fv(doc, "NVDA", "relative_volume_tod")[0] == round(today / ((prior[4] + prior[5]) / 2), 6)


# ============================================================================================================ adversarial (A)
def test_A01_tampered_values_written_around_the_store_never_crash_the_scan_they_block_it(tmp_path):
    w = world(tmp_path / "w")
    w.clk.t = T - 0.5 * S
    open_did = w.store.create_dataset(DatasetSpec(job_kind="IMPORT", kind="BAR_1DAY", provider="ALPACA", feed="iex", adjustment="raw", symbols=["NVDA"],
                                                  timeframe="1Day", start_date=PREV, end_date=PREV))
    raw_insert(w.store.path, "mi_observations", {"obs_id": "inf", "natural_key": "BAR_1DAY|NVDA|inf", "observed_at": ST.ts(ny(PREV)), "dataset_id": open_did,
                                                  "payload_json": '{"open": 1, "high": 1, "low": 1, "close": Infinity, "volume": "lots"}'},
               where_symbol="NVDA", where_kind="BAR_1DAY")
    doc = scan(w)
    assert doc["run"]["status"] == "BLOCKED" and doc["run"]["quality"]["verdict"] == "BLOCK"
    assert all(not r["horizons"][h]["eligible"] for r in doc["results"] for h in MS.HORIZONS)
    for f in res(doc, "NVDA")["features"].values():
        assert f["value"] is None or math.isfinite(f["value"])                                  # never a non-finite figure
    json.loads(R.dumps(doc))                                                                     # strict JSON: allow_nan=False held


def test_A02_a_symbol_whose_inputs_cannot_be_read_fails_closed_and_the_run_says_so(tmp_path, monkeypatch):
    w = world(tmp_path / "w")
    real = R._Reader.quote

    def broken(self, sym):
        if sym == "AMD":
            raise ValueError("a row that does not read as written")
        return real(self, sym)
    monkeypatch.setattr(R._Reader, "quote", broken)
    doc = scan(w)
    assert "SYMBOL_INPUT_UNREADABLE" in doc["run"]["codes"] and res(doc, "AMD")["problems"] == ["SYMBOL_INPUT_UNREADABLE"]
    assert not hz(doc, "AMD")["eligible"] and not hz(doc, "AMD", MS.SWING)["eligible"] and hz(doc, "NVDA")["rank"] == 1
    assert res(doc, "AMD")["inputs"] == {"quote": None, "error": "ValueError"}
    assert res(doc, "AMD")["features"]["sector_strength"]["codes"] == ["SYMBOL_INPUT_UNREADABLE"]   # never completed from its sector's ETF

    def boom(*a, **k):
        raise TypeError("SPY bars that do not read as written")
    monkeypatch.setattr(R.FG, "regime", boom)
    g = scan(w)["context"]["regime"]
    assert (g["spy_trend"], g["spy_vol"], g["codes"], g["status"]) == ("UNKNOWN", "UNKNOWN", ["REGIME_INPUT_UNREADABLE"], "INSUFFICIENT_DATA")
    monkeypatch.setattr(R._Reader, "last_ok_fetch", lambda self, *a, **k: boom())                # a run-level read that does not read as written
    d = scan(w)
    assert (d["run"]["status"], d["run"]["codes"]) == ("BLOCKED", ["SCAN_INPUT_UNREADABLE"]) and len(d["results"]) == 19
    assert all(not r["horizons"][h]["eligible"] and "SCAN_INPUT_UNREADABLE" in r["horizons"][h]["reasons"] for r in d["results"] for h in MS.HORIZONS)


# ============================================================================================================ the independent review (R)
def test_R01_a_halt_that_began_before_today_and_is_unresolved_is_open_today(tmp_path):
    w = world(tmp_path / "w", halt_at=None)
    halt_poll(w, T - 0.6 * S, [halt_obs("NVDA", ny(PREV, 15, 50), at=T - 0.6 * S), halt_obs("AMD", ny(PREV, 15, 55), T - 300 * S, at=T - 0.6 * S),
                               halt_obs("SOXX", ny(PREV, 11), ny(PREV, 11, 30), at=T - 0.6 * S)])
    doc = scan(w)
    assert res(doc, "NVDA")["flags"]["halt"]["codes"] == ["HALT_OPEN"] and "HALTED" in hz(doc, "NVDA")["reasons"] and "HALTED" in hz(doc, "NVDA", MS.SWING)["reasons"]
    assert res(doc, "AMD")["flags"]["halt"]["codes"] == ["RESUMED_WITHIN_10_MIN"] and hz(doc, "AMD")["rank"] is None     # halted at the close, resumed now
    assert res(doc, "SOXX")["flags"]["halt"]["value"] == "FALSE" and hz(doc, "SOXX")["rank"] == 1                          # resolved yesterday


def test_R02_a_self_contradictory_embedded_bar_is_never_a_value(tmp_path):
    q = T - 2 * S
    bad_today = snap(quote_at=q, prev_close=210.0, open_=212.1, last=211.0)
    bad_today["daily_bar"] = {**bad_today["daily_bar"], "open": 500.0, "high": 212.0, "low": 209.0}
    bad_prev = snap(quote_at=q, prev_close=300.0, open_=303.0, last=304.5)
    bad_prev["prev_daily_bar"] = {**bad_prev["prev_daily_bar"], "high": 1.0}
    w = world(tmp_path / "w", snaps=[r for r in base_snaps() if r[0] not in ("AMD", "SOXX")] + [("AMD", q, bad_today), ("SOXX", q, bad_prev)])
    doc = scan(w)
    assert doc["run"]["quality"]["verdict"] == "PASS"                                            # the 7.4 report does not read embedded bars
    for name in ("gap_pct", "intraday_move", "unusual_volatility"):
        assert (fv(doc, "AMD", name)[0], res(doc, "AMD")["features"][name]["codes"]) == (None, ["EMBEDDED_BAR_INVALID"]), name
    assert res(doc, "SOXX")["features"]["gap_pct"]["codes"] == ["EMBEDDED_BAR_INVALID"] and fv(doc, "SOXX", "day_change")[0] is None
    assert hz(doc, "AMD")["rank"] is None and hz(doc, "SOXX")["rank"] is None and doc["sequence"][MS.INTRADAY][:2] == ["NVDA", "SPY"]


def test_R03_relative_volume_never_reads_a_missing_minute_as_zero(tmp_path):
    w = world(tmp_path / "w", minutes=False)
    prior = back(PREV, 10)
    bars = {ny(d, 9, 30) + timedelta(minutes=k): 100 for d in prior for k in range(30)}
    bars[ny(TODAY, 9, 30)] = 100                                                                 # only 09:30 has arrived
    add_minutes(w, "NVDA", bars)
    one = res(scan(w, T + 2 * S), "NVDA")["features"]["relative_volume_tod"]
    assert (one["value"], one["minutes"], one["status"]) == (1.0, 1, "STALE")                   # to t = 09:31, one minute each; that bar is old
    add_minutes(w, "NVDA", {ny(TODAY, 9, 32): 100})                                              # 09:31 never arrives
    gap = res(scan(w, T + 2 * S), "NVDA")["features"]["relative_volume_tod"]
    assert (gap["value"], gap["codes"]) == (None, ["MINUTES_MISSING"])
    sparse = world(tmp_path / "sparse", minutes=False)
    b2 = {ny(d, 9, 30) + timedelta(minutes=k): 100 for d in prior for k in range(30 if d not in prior[:6] else 1)}
    b2.update({ny(TODAY, 9, 30) + timedelta(minutes=k): 100 for k in range(29)})
    add_minutes(sparse, "NVDA", b2)                                                              # six prior sessions hold only their first bar
    f = res(scan(sparse), "NVDA")["features"]["relative_volume_tod"]
    assert f["codes"] == ["PRIOR_SESSIONS_MISSING"] and f["missing_sessions"] == [d.isoformat() for d in prior[:6]] and f["value"] is None


def test_R04_a_catalyst_is_read_from_the_run_that_read_the_articles_latest_revision(tmp_path):
    w = _news_world(tmp_path / "w", published=T - timedelta(hours=2))
    assert res(scan(w, T + 2 * S), "NVDA")["flags"]["catalyst"]["value"] == "TRUE"
    w.clk.t = T - 1.0 * S
    did = w.store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind="NEWS", provider="ALPACA", feed="none", adjustment="none", symbols=[]))
    fixed = news([dict(art(1, T - timedelta(hours=2), headline="Advanced Micro Devices (NASDAQ: AMD) beats estimates", symbols=("AMD",),
                           updated=T - 2 * S), source="Reuters", url="https://www.reuters.com/markets/nvda-beats")]).body   # the correction re-tags it
    w.store.record_fetch(FetchRecord(provider="ALPACA", endpoint=AN.ENDPOINT, started_at=w.clk(), retrieved_at=w.clk(), status="OK"), dataset_id=did)
    w.store.ingest(did, AN.parse_page(fixed, symbols=[], retrieved_at=w.clk(), dataset_id=did)[0])
    seal(w, did)
    behind = res(scan(w, T + 2 * S), "NVDA")["flags"]["catalyst"]                                # revision 2 is known, no run has read it yet
    assert behind["value"] == "NOT_ESTABLISHED" and "articles" not in behind
    msha = w.store.record_entity_map({"sec_names": {"NVDA": "NVIDIA CORP", "AMD": "ADVANCED MICRO DEVICES INC"}, "universe": ["NVDA", "AMD"], "aliases": {}})
    w.clk.t = T - 0.8 * S
    NP.mention_run(w.store, as_of=T - 0.8 * S, window_start=T - timedelta(hours=3), window_end=T - 0.8 * S, map_sha256=msha)
    d = scan(w, T + 2 * S)
    assert res(d, "NVDA")["flags"]["catalyst"]["value"] == "NOT_ESTABLISHED"                     # the superseded run never speaks for it
    a = res(d, "AMD")["flags"]["catalyst"]
    assert a["value"] == "TRUE" and a["articles"][0]["revision"] == 2


def test_R05_a_fetch_time_the_schema_accepts_but_no_clock_can_read_never_crashes_the_scan(tmp_path):
    w = world(tmp_path / "w")
    c = sqlite3.connect(str(w.store.path))
    try:
        c.execute("INSERT INTO mi_fetches (fetch_id, provider, endpoint, params_json, started_at, retrieved_at, status, ingested_at) "
                  "VALUES ('odd', 'NASDAQ_TRADER', ?, '{}', ?, ?, 'OK', ?)", (HA.ENDPOINT, "2025-12-02T14:59:50.000000Z", "2025-12-02T14:59:61.000000Z",
                                                                             "2025-12-02T14:59:59.000000Z"))
        c.commit()
    finally:
        c.close()
    doc = scan(w)                                                                                # 61 seconds: GLOB-valid, never a time
    assert doc["run"]["status"] == "BLOCKED" and doc["run"]["quality"]["verdict"] == "BLOCK"
    assert doc["context"]["feeds"]["halt"]["age_s"] == 30.0                                      # the readable poll before it, never the odd one


def test_R06_from_18_00_to_the_daily_bars_completion_no_swing_candidate_can_be_eligible_and_the_scan_says_why(tmp_path):
    w = world(tmp_path / "w")
    at6 = scan(w, ny(TODAY, 18, 30))
    assert at6["horizons"][MS.SWING]["codes"] == ["DAILY_BAR_DUE_BEFORE_STORABLE"] and at6["horizons"][MS.SWING]["eligible"] == 0
    at_least(w, ny(TODAY, 20, 5))
    for s in BASE:
        add_daily(w, s, {TODAY: BASE[s]})                                                        # complete at 20:00, stored from then
    at8 = scan(w, ny(TODAY, 20, 30))
    assert at8["horizons"][MS.SWING]["codes"] == [] and at8["horizons"][MS.SWING]["eligible"] == 4


def test_R07_earnings_proximity_reads_the_latest_calendar_snapshot_only(tmp_path):
    w = world(tmp_path / "w")
    _earnings(w, ny(PREV, 12), csv=AV_CSV)                                                       # yesterday: NVDA on 2025-12-09
    _earnings(w, T - 0.8 * S, csv="symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\nAMD,AMD,2026-02-03,2025-12-31,,USD\r\n")
    doc = scan(w)
    e = res(doc, "NVDA")["flags"]["earnings_proximity"]
    assert (e["value"], e["codes"], e["calendar"]["snapshot_date"]) == ("UNKNOWN", ["EARNINGS_UNKNOWN", "NOT_IN_LATEST_CALENDAR"], TODAY.isoformat())
    assert res(doc, "AMD")["flags"]["earnings_proximity"]["report_date"] == "2026-02-03"


def test_R08_a_halt_reads_its_validated_time_and_an_unreadable_resumption_keeps_it_open(tmp_path):
    w = world(tmp_path / "w", halt_at=None)
    odd = halt_obs("NVDA", T - 900 * S, at=T - 0.6 * S)
    odd = odd.model_copy(update={"payload": {**dict(odd.payload), "halted_at": "yesterday-ish", "resumed_at": "later"}})
    halt_poll(w, T - 0.6 * S, [odd])
    h = res(scan(w), "NVDA")["flags"]["halt"]
    assert (h["value"], h["codes"], h["halts"][0]["halted_at"]) == ("TRUE", ["HALT_RESUMPTION_UNREADABLE"], ST.ts(T - 900 * S))


def test_R09_a_newer_absence_of_the_daily_bar_is_never_read_past(tmp_path):
    w = world(tmp_path / "w")
    did = w.store.create_dataset(DatasetSpec(job_kind="IMPORT", kind="BAR_1DAY", provider="ALPACA", feed="iex", adjustment="raw", symbols=["AMD"], timeframe="1Day",
                                             start_date=PREV, end_date=PREV))
    w.store.ingest(did, [Observation(kind="BAR_1DAY", symbol="AMD", provider="ALPACA", feed="iex", adjustment="raw", observed_at=ny(PREV), retrieved_at=w.clk(),
                                     available_at=w.clk(), session="REG", session_date=PREV, status="UNAVAILABLE", backfilled=True, payload={})])
    seal(w, did, "PARTIAL")
    doc = scan(w)
    assert res(doc, "AMD")["daily"]["reason"] == "LATEST_RECORD_IS_ABSENCE" and "DAILY_BAR_LATEST_RECORD_IS_ABSENCE" in hz(doc, "AMD", MS.SWING)["reasons"]
    assert hz(doc, "NVDA", MS.SWING)["eligible"]


def test_R10_the_hash_ignores_input_order_codes_say_what_happened_and_a_closed_window_never_hides_the_gate(tmp_path):
    w = world(tmp_path / "w")
    assert scan(w, watchlist=("CLS", "NVDA"))["scan_sha256"] == scan(w, watchlist=("NVDA", "CLS", "nvda"))["scan_sha256"]
    far = scan(w, ny(date(2029, 1, 3), 10))                                                      # a year outside the calendar
    assert far["horizons"][MS.INTRADAY]["reason"] == "CALENDAR_NOT_COVERED" and res(far, "NVDA")["features"]["relative_volume_tod"]["codes"] == ["CALENDAR_NOT_COVERED"]
    post = scan(w, ny(TODAY, 16, 30), archive_root=None)                                         # INSUFFICIENT_DATA after the close
    assert hz(post, "NVDA")["reasons"] == ["OUTSIDE_DAY_WINDOW_POST", "QUALITY_INSUFFICIENT_DATA"]


# ============================================================================================================ isolation (X)
ALLOWED_75 = {"__future__", "argparse", "hashlib", "json", "math", "sys", "tempfile", "datetime", "pathlib", "typing", "zoneinfo",
              "api.validation", "data.sector_map", "dynamic_universe", "dynamic_universe.core", "market_intel", "market_intel.calendar",
              "market_intel.freshness", "market_intel.quality", "market_intel.store", "market_intel.validate", "market_intel.news",
              "market_intel.sources", "market_scan", "market_scan.features", "backtest.replay", "rotation_walkforward", "config"}
NEW_MODULES_75 = ("__init__.py", "features.py", "filters.py", "flags.py", "run.py")


def test_X01_the_scanner_reaches_no_network_broker_execution_or_model_writes_nothing_and_nothing_outside_imports_it(tmp_path):
    files = sorted((ROOT / "market_scan").glob("*.py"))
    assert [p.name for p in files] == list(NEW_MODULES_75)
    for p in files:
        src = p.read_text(encoding="utf-8")
        t = ast.parse(src)
        names = {n.module for n in ast.walk(t) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names}
        assert names <= ALLOWED_75, (p.name, sorted(names - ALLOWED_75))
        toks = [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = re.sub(r"\s*([.(])\s*", r"\1", " ".join(toks))                                   # the code itself, not its prose
        for word in ("urlopen", "urllib", "http.client", "requests.", "socket.", "alpaca.", "net.fetch", "broker", "execution", "proposals", "setups.",
                     "safety.", "anthropic", "openai", "agents", "ai_explain", "threading", "Thread(", "subprocess", "environ", "getenv", "submit_order",
                     "create_dataset", "finish_dataset", ".ingest(", "record_fetch", "record_news_run", "mention_run(", "cluster_run(", "write_text",
                     "write_bytes", "rmtree", "os.remove", "shutil", "unlink", "_rw(", "datetime.now", "time.time", "robinhood", "signals.compute",
                     "select_active", "sqlite3.connect", "connect("):
            assert word not in code, (p.name, word)
        for word in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER ", "REPLACE ", "VACUUM", "journal_mode", "mode=rw"):
            assert word not in src, (p.name, word)                                               # no SQL write anywhere, strings included
        assert not re.findall(r"\bopen\(", src), p.name
        writes = re.findall(r"[\w.]+\.write\(", src)
        assert writes == (["out.write("] * 3 if p.name == "run.py" else []), (p.name, writes)        # only the CLI's output stream
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_scan/", "tests/", ".venv/",
             "scratchpad/")) and re.search(r"^\s*(from|import)\s+market_scan\b", p.read_text(encoding="utf-8", errors="replace"), re.M)]
    assert users == []                                                                           # no live path imports the scanner
    from strategy_registry.standard import forbidden_keys
    doc = scan(world(tmp_path / "w"))
    def keys(x):
        if isinstance(x, dict):
            return set(x) | {k for v in x.values() for k in keys(v)}
        return {k for v in x for k in keys(v)} if isinstance(x, list) else set()
    assert forbidden_keys(doc) == [] and not {"score", "score_kind", "probability", "signal"} & keys(doc)   # no execution field and no score, anywhere
    probe = ("import sys, threading; n = threading.active_count(); import market_scan.run; "
             "print(sorted(m for m in sys.modules if m.split('.')[0] in {'api', 'autorun', 'config', 'data', 'dynamic_universe', 'evidence', 'setups', "
             "'alpaca', 'anthropic', 'openai', 'agents', 'ai_explain', 'execution', 'proposals', 'paper', 'safety', 'scanner', 'trader_view', 'rh_gateway', "
             "'robinhood_sync', 'strategy', 'backtest', 'rotation_walkforward'})); print(threading.active_count() - n)")
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True)
    loaded, threads = out.stdout.splitlines()
    assert ast.literal_eval(loaded) == ["api", "api.validation", "autorun", "autorun.calendar", "autorun.session", "config", "data", "data.events",
                                        "data.events.earnings", "data.events.models", "data.sector_map", "dynamic_universe", "dynamic_universe.core", "evidence",
                                        "evidence.net", "evidence.rules", "setups", "setups.market_hours"], out.stderr[-600:]
    assert threads == "0"                                    # transitively: the data layer's own imports (DESIGN_75 section 9); no broker, no model
    import config
    assert (MS.MIN_PRICE, config.MIN_PRICE) == (5.0, 5) and MS.MIN_PRICE >= config.MIN_PRICE     # never looser than the project's minimum
    from market_intel.news import entities as EN
    assert (EN.CATALYST_MIN_CONFIDENCE, EN.CATALYST_MAX_TIER) == (0.8, 3) and VA.DAY_JUMP == 0.25
    assert not ST.default_path().exists()



# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_75}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_75} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


APP_EDITS_75 = {".github/ci/pytest_groups.json"}
FROZEN_DIRS_75 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                  "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                  "database", "data", "value_quality", "market_intel", "scanner", "strategy_registry", "rotation_walkforward", "backtest", "paper")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_75 == "1d817939af64f38e95745e456595225ebf809c10" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_75}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_75).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_75 | FL.EDITED_75, {"unexpected": sorted(changed - FL.NEW_75 - FL.EDITED_75), "missing": sorted((FL.NEW_75 | FL.EDITED_75) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_75).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_75 and {p for p in changed if p in in_base} == FL.EDITED_75
    assert FL.NEW_75 == {"DESIGN_75_MARKET_SCAN.md", "tests/test_market_scan_75.py"} | {f"market_scan/{m}" for m in NEW_MODULES_75}
    assert {p for p in FL.EDITED_75 if not p.startswith("tests/")} == APP_EDITS_75
    assert FL.SEMANTIC_75 == {} and FL.INSTRUMENTATION_75 == {}
    for d in FROZEN_DIRS_75:                                                                   # every existing module: the 7.0-7.4 data layer included
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert now_text(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "watchlist.txt",
                "STRATEGY_STANDARD_V1.md", "main.py"):
        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook, dependency or universe edit
    for rel in sorted(FL.NEW_75 | FL.EDITED_75):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_market_intel_or_a_scan():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import config
    from market_intel.settings import settings as mi_settings
    assert mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_", "ALPHAVANTAGE", "MARKET_SCAN_MODE", "STRATEGY_SHADOW"))]   # no new key
    assert DUP.settings()["mode"] == "OFF"                                                     # the dynamic universe stays OFF
    import setups as SU
    assert (SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.EVIDENCE_MAX_AGE_DAYS) == (40, 60, 30)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_75.items():
        text = now_text(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_75) == FL.EDITED_75 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.5 (the shared market scanner")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_731 + NEW_74 + NEW_75 plus this ledger.", " + NEW_731 + NEW_74 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_75 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_75: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key
    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_75 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_75 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('def test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base():\n    allowed = ("dynamic_universe/", "tools/", "tests/", "decision_flow/", "api/routes/decision_flow.py", "scratchpad/")\n    offenders = []\n',
         'def test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base():\n    allowed = ("dynamic_universe/", "tools/", "tests/", "decision_flow/", "api/routes/decision_flow.py", "scratchpad/",\n               "market_scan/")                                       # Stage 7.5: reads core.py, never activates\n    offenders = []\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('        rel = p.relative_to(ROOT).as_posix()\n        if rel.startswith(("market_intel/", "tests/", ".venv/", "scratchpad/")) or "/site-packages/" in rel:\n            continue\n',
         '        rel = p.relative_to(ROOT).as_posix()\n        if rel.startswith(("market_intel/", "market_scan/", "tests/", ".venv/", "scratchpad/")) or "/site-packages/" in rel:\n            continue\n'),
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    assert present == set(NEW_MODULES_73) and {f"market_intel/{m}" for m in NEW_MODULES_73} <= FL.NEW_73\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n',
         '    assert present == set(NEW_MODULES_73) and {f"market_intel/{m}" for m in NEW_MODULES_73} <= FL.NEW_73\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "market_scan/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n'),
        ('    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n',
         '    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    assert src.count("sqlite3.connect(") == 1 and "?mode=ro" in src                         # and it opens only copies, read-only\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n',
         '    assert src.count("sqlite3.connect(") == 1 and "?mode=ro" in src                         # and it opens only copies, read-only\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "market_scan/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n'),
        ('\nAPP_EDITS_74 = {".github/ci/pytest_groups.json"}\n',
         '\ndef post(rel):\n    """main after PR #46 (Stage 7.4 merged; the Stage 7.5 base): what Stage 7.4 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_75}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_75} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_74 = {".github/ci/pytest_groups.json"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_74).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_74, FL.BASE_75).split())                 # Stage 7.4 itself, between two fixed commits\n    assert stage == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(stage - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - stage)}\n    later = FL.NEW_75 | FL.EDITED_75                                                         # Stage 7.5, pinned by its own F tests\n    assert changed == FL.NEW_74 | FL.EDITED_74 | later, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74 - later),\n                                                         "missing": sorted((FL.NEW_74 | FL.EDITED_74 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_74).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_74).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_74 and {p for p in changed if p in in_base} == FL.EDITED_74\n    assert FL.NEW_74 == {"DESIGN_74_MARKET_INTEL_QUALITY.md", "tests/test_market_intel_quality_74.py"} | {f"market_intel/{m}" for m in NEW_MODULES_74}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_74).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_74 and {p for p in stage if p in in_base} == FL.EDITED_74\n    assert FL.NEW_74 == {"DESIGN_74_MARKET_INTEL_QUALITY.md", "tests/test_market_intel_quality_74.py"} | {f"market_intel/{m}" for m in NEW_MODULES_74}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert now_text(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert post(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n'),
        ('    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook or dependency\n    for rel in sorted(FL.NEW_74 | FL.EDITED_74):\n',
         '    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook or dependency\n    for rel in sorted(FL.NEW_74 | FL.EDITED_74):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_74.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_74.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_74) == FL.EDITED_74 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.4 (market intelligence data quality")\n',
         '    assert set(REPLAY_74) == FL.EDITED_74 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.4 (market intelligence data quality")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_74 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.4 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_74 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    assert (sv.count(".unlink()"), sv.count("partial.unlink()"), sv.count("tmp.unlink()")) == (3, 2, 1)                  # its own .partial; a second name\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n',
         '    assert (sv.count(".unlink()"), sv.count("partial.unlink()"), sv.count("tmp.unlink()")) == (3, 2, 1)                  # its own .partial; a second name\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "market_scan/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n'),
        ('    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n',
         '    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    assert set(NEW_MODULES_72) <= present and {f"market_intel/{m}" for m in NEW_MODULES_72} <= FL.NEW_72                # declared, one by one\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n',
         '    assert set(NEW_MODULES_72) <= present and {f"market_intel/{m}" for m in NEW_MODULES_72} <= FL.NEW_72                # declared, one by one\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "market_scan/", "tests/", ".venv/",\n             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]\n'),
        ('    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n',
         '    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('            assert word not in src, (rel, word)\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",\n             "scratchpad/")) and ("market_intel" in p.read_text(encoding="utf-8", errors="replace"))]\n',
         '            assert word not in src, (rel, word)\n    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "market_scan/", "tests/", ".venv/",\n             "scratchpad/")) and ("market_intel" in p.read_text(encoding="utf-8", errors="replace"))]\n'),
        ('    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n',
         '    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
