"""
Stage 7.4 - the market-intelligence data quality and latency report: OFFLINE, READ-ONLY and display-only (DESIGN_74_MARKET_INTEL_QUALITY.md).
Temporary files only: every store, collector state, archive and report lives under tmp_path; the production paths are never read, written or
created.

  Q01-Q15 the report: fresh, aging and stale per the 7.0 policy; missing and malformed timestamps; coverage gaps per provider and schedule;
          empty datasets and stores; negative latency; integrity failures, never outweighed; clock boundaries and time zones; point-in-time
          leakage; read-only behaviour; determinism and the CLI; fixture versus production evidence; provider health; IEX vs SIP divergence;
          SEC still blocked; thresholds
  V01     independent verification: the figures recomputed from raw SQL and from the 7.1 store's own reads
  X01     isolation: no network, broker, trading or model; no write path; nothing outside market_intel imports the report
  R01-R08 the review findings (DESIGN_74 sections 12 and 13), each reproduced first
  F01-F03 scope (freeze bookkeeping)
"""
from __future__ import annotations

import ast
import dataclasses
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import freeze_ledger as FL
from market_intel import calendar as CAL
from market_intel import ingest as IG
from market_intel import quality as Q
from market_intel import schema_v2 as SV
from market_intel import store as ST
from market_intel.collector import plans as PL
from market_intel.collector import transport as TR
from market_intel.collector.transport import Response
from market_intel.models import Observation
from market_intel.store import DatasetSpec, MarketIntelStore
from test_market_intel_collector_73 import AAPL_IEX, HALTS, NEWS, NONE, NV, SNAP, TUE, Clock, art, env, news, rss, snaps
from test_market_intel_schema_v2_731 import AAPL_MSFT, auth_file, collect_spec, load, news_obs, ok_fetch, tamper, v1_world
from test_market_intel_schema_v2_731 import v2 as v2_store

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NYZ = ZoneInfo("America/New_York")
S = timedelta(seconds=1)


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree(*dirs) -> dict:
    """Every file under the directories with the hash of its bytes: what 'nothing changed' means."""
    return {f"{Path(d).name}/{p.relative_to(d).as_posix()}": sha(p) for d in dirs for p in sorted(Path(d).rglob("*")) if p.is_file()}


def rep(e, at, **kw):
    kw.setdefault("state", e.state.path)
    kw.setdefault("archive_root", e.archive.root)
    return Q.build_report(e.store.path, as_of=at, **kw)


def chk(r, name):
    return next(c for c in r["checks"] if c["check"] == name)


def results(r, *skip):
    return {c["check"]: (c["result"], c["codes"]) for c in r["checks"] if c["check"] not in skip}


def stream(r, kind, symbol=None, feed=None):
    return next(s for s in r["freshness"]["streams"] if s["kind"] == kind and s["symbol"] == symbol and (feed is None or s["feed"] == feed))


def sessions(start: date, end: date) -> list:
    return [date.fromordinal(n) for n in range(start.toordinal(), end.toordinal() + 1) if CAL.market_day_status(date.fromordinal(n)) == CAL.MARKET_DAY]


def ny(d: date, hh=0, mm=0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NYZ).astimezone(UTC)


def dbar(sym, d, close, *, feed, retrieved, backfilled=True, flags=()):
    return Observation(kind="BAR_1DAY", symbol=sym, provider="ALPACA", feed=feed, adjustment="raw", observed_at=ny(d), retrieved_at=retrieved,
                       available_at=retrieved, session="REG", session_date=d, status="AVAILABLE", backfilled=backfilled, quality_flags=tuple(flags),
                       payload={"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000})


def clone_obs(path, symbol, dataset_id, unchecked=False, **changes):
    """A copy of a stored observation row written around the store (raw SQL): a tool, or a bug. `unchecked`: around the CHECKs too."""
    c = sqlite3.connect(str(path))
    try:
        if unchecked:
            c.execute("PRAGMA ignore_check_constraints = ON")
        cols = [x[1] for x in c.execute("PRAGMA table_info(mi_observations)")]
        row = dict(zip(cols, c.execute("SELECT * FROM mi_observations WHERE symbol = ? ORDER BY rowid LIMIT 1", (symbol,)).fetchone()))
        row.update(dataset_id=dataset_id, fetch_id=None, **changes)
        c.execute(f"INSERT INTO mi_observations ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [row[k] for k in cols])
        c.commit()
    finally:
        c.close()


def seal(e, did, status="COMPLETED"):
    e.store.finish_dataset(did, status)
    IG.recover_pending(e.store, e.archive, clock=e.clk)                       # anchors the seal, as the 7.1 recovery does


def add_daily(e, sym, days, *, feed="iex", job="IMPORT", closes=None, start=None, end=None, flags=()):
    """One sealed, anchored BAR_1DAY dataset of `days`; its window is [start, end] (by default the days' own span)."""
    did = e.store.create_dataset(DatasetSpec(job_kind=job, kind="BAR_1DAY", provider="ALPACA", feed=feed, adjustment="raw", symbols=[sym], timeframe="1Day",
                                             start_date=start or days[0], end_date=end or days[-1]))
    e.store.ingest(did, [dbar(sym, d, (closes or {}).get(d, 100.0), feed=feed, retrieved=e.clk(), backfilled=job != "FIXTURE", flags=flags) for d in days])
    seal(e, did)
    return did


def add_minutes(e, sym, times):
    did = e.store.create_dataset(DatasetSpec(job_kind="IMPORT", kind="BAR_1MIN", provider="ALPACA", feed="iex", adjustment="raw", symbols=[sym], timeframe="1Min",
                                             start_date=CAL.session(times[0])["session_date"], end_date=CAL.session(times[-1])["session_date"]))
    e.store.ingest(did, [Observation(kind="BAR_1MIN", symbol=sym, provider="ALPACA", feed="iex", adjustment="raw", observed_at=t, retrieved_at=e.clk(),
                                     available_at=e.clk(), session=CAL.session(t)["session"], session_date=CAL.session(t)["session_date"], status="AVAILABLE",
                                     backfilled=True, payload={"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}) for t in times])
    seal(e, did)
    return did


def fixture(kind, provider="ALPACA", feed="none", symbols=()):
    return DatasetSpec(job_kind="FIXTURE", kind=kind, provider=provider, feed=feed, adjustment="none", symbols=list(symbols))


def raw_fetch(path, fetch_id, *, started, retrieved, ingested, status="OK", http=200, latency_ms=None, endpoint="/x", unchecked=False):
    """A fetch row written around the store (a tool, or a bug): only the schema's own checks apply - or none, with `unchecked`."""
    c = sqlite3.connect(str(path))
    try:
        if unchecked:
            c.execute("PRAGMA ignore_check_constraints = ON")
        c.execute("INSERT INTO mi_fetches (fetch_id, provider, endpoint, params_json, started_at, retrieved_at, status, http_status, latency_ms, ingested_at) "
                  "VALUES (?, 'ALPACA', ?, '{}', ?, ?, ?, ?, ?, ?)", (fetch_id, endpoint, started, retrieved, status, http, latency_ms, ingested))
        c.commit()
    finally:
        c.close()


def world_paths(d: Path):
    return d / "mi.db", d / "collector.db", d / "raw"


# ============================================================================================================ the report (Q)
def test_Q01_fresh_aging_and_stale_follow_the_70_policy_per_stream_and_feed_and_no_timestamp_stands_in_for_another(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S), news([art(1, TUE - timedelta(minutes=5))])])
    assert e.col.collect(SNAP, AAPL_MSFT)["status"] == "COMPLETED" and e.col.collect(NEWS, NV)["status"] == "COMPLETED"
    add_daily(e, "SPY", sessions(date(2025, 11, 24), date(2025, 12, 1)))
    add_daily(e, "QQQ", sessions(date(2025, 11, 24), date(2025, 11, 28)))
    r = rep(e, TUE + 4 * S)
    aapl = stream(r, "QUOTE_SNAPSHOT", "AAPL")
    assert {k: (g["status"], g["freshness"], g["age_s"]) for k, g in aapl["grades"].items()} == {"QUOTE_DAY": ("AVAILABLE", "FRESH", 5.0),
                                                                                               "QUOTE_SWING": ("AVAILABLE", "FRESH", 5.0)}
    ages = aapl["latest_record"]["ages_s"]
    assert (ages["observed_at"], ages["retrieved_at"], ages["available_at"], ages["ingested_at"]) == (5.0, 4.0, 4.0, 4.0)   # each time its own
    assert aapl["latest_record"]["missing"] == ["published_at"]                                                           # absent, never borrowed
    assert stream(r, "QUOTE_SNAPSHOT", "MSFT")["grades"]["QUOTE_DAY"]["status"] == "UNAVAILABLE"                            # an absence: never a value
    assert stream(r, "BAR_1DAY", "SPY", "iex")["grades"]["BAR_1DAY"]["freshness"] == "FRESH"
    qqq = stream(r, "BAR_1DAY", "QQQ", "iex")["grades"]["BAR_1DAY"]
    assert (qqq["status"], qqq["reason"]) == ("STALE", "SESSION_MISSING")                                                   # Monday's bar is due
    n = stream(r, "NEWS")
    assert (n["assessed_by"], n["feed_policy"], n["grades"]) == ("FEED", "NEWS_POLL", {})                                   # graded by its poll
    feeds = {f["source"]: f for f in r["freshness"]["feeds"]}
    assert (feeds["alpaca_news"]["status"], feeds["alpaca_news"]["freshness"]) == ("AVAILABLE", "FRESH")
    assert chk(r, "freshness")["codes"] == ["STALE_STREAMS", "UNAVAILABLE_STREAMS"] and chk(r, "freshness")["result"] == "WARN"
    edge = rep(e, TUE + 4 * S + timedelta(microseconds=1))
    assert stream(edge, "QUOTE_SNAPSHOT", "AAPL")["grades"]["QUOTE_DAY"]["freshness"] == "AGING"                            # 5 s FRESH, 5 s + 1 us not
    late = rep(e, TUE + 700 * S)
    assert {f["source"]: f["status"] for f in late["freshness"]["feeds"]}["alpaca_news"] == "STALE" and "STALE_FEEDS" in chk(late, "freshness")["codes"]
    assert late["freshness"]["histogram"]["QUOTE_DAY"]["status"] == {"STALE": 1, "UNAVAILABLE": 1}
    e.clk.t = TUE + 20 * S
    e.ft.push(snaps(MSFT=TUE + 19 * S))                                                    # AAPL absent now: its old value is not current
    e.col.collect(SNAP, AAPL_MSFT)
    gone = stream(rep(e, TUE + 21 * S), "QUOTE_SNAPSHOT", "AAPL")
    assert {k: (g["status"], g["reason"]) for k, g in gone["grades"].items()} == {k: ("UNAVAILABLE", "LATEST_RECORD_IS_ABSENCE")
                                                                                for k in ("QUOTE_DAY", "QUOTE_SWING")}


def test_Q02_missing_timestamps_are_counted_apart_and_malformed_ones_block_without_breaking_the_report(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S)])
    e.col.collect(SNAP, AAPL_IEX)
    did = e.store.create_dataset(fixture("NEWS"))
    t = TUE - timedelta(minutes=3)
    e.store.ingest(did, [Observation(kind="NEWS", provider="ALPACA", observed_at=t, retrieved_at=TUE, available_at=TUE, session=CAL.session(t)["session"],
                                     session_date=CAL.session(t)["session_date"], status="AVAILABLE", source_identifier="n-1", payload={"headline": "untimed"})])
    seal(e, did)
    at = TUE + 10 * S
    clean = rep(e, at)
    assert chk(clean, "timestamps")["result"] == "PASS" and clean["timestamps"]["missing"] == {"news_without_published_at": 1}
    raw_fetch(e.store.path, "bad", started="2025-02-30T10:00:00.000000Z", retrieved="2025-12-02 15:00:00", ingested="2025-12-02T25:00:00.000000Z",
              unchecked=True)                                                                  # a writer around the schema's CHECKs
    c = sqlite3.connect(str(e.state.path))
    c.execute("PRAGMA ignore_check_constraints = ON")
    c.execute("INSERT INTO cs_breaker_events (source, at, state, failures, cooldown_s) VALUES ('halts', '2025-12-02T15:00:00Z', 'CLOSED', 0, 0)")
    c.commit()
    c.close()
    r = rep(e, at)
    m = r["timestamps"]["malformed"]
    assert m["counts"] == {"MALFORMED_TIMESTAMP": 4}
    assert sorted(x["column"] for x in m["examples"]) == ["state:cs_breaker_events.at", "store:mi_fetches.ingested_at", "store:mi_fetches.retrieved_at",
                                                         "store:mi_fetches.started_at"]
    assert (chk(r, "timestamps")["result"], r["verdict"]["result"]) == ("BLOCK", "BLOCK")
    assert results(r, "timestamps") == results(clean, "timestamps")                            # a malformed time never reaches a figure
    good = "2025-12-02T15:00:00.000000Z"
    assert Q._t(good) == datetime(2025, 12, 2, 15, tzinfo=UTC)
    for bad in ("2025-02-30T10:00:00.000000Z", "2025-12-02T25:00:00.000000Z", "2025-12-02T15:00:60.000000Z", "2025-12-02 15:00:00", "2025-12-02T15:00:00Z",
                "2025-1-02T15:00:00.000000Z", "2025-12-02T15:00:00.00000Z", "2025-12-02T15:00:00.000000+00:00", "", None, 1733151600):
        assert Q._t(bad) is None, bad


def test_Q03_coverage_compares_expected_and_observed_only_where_an_expectation_is_explicit_per_provider_kind_and_symbol(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S), news([art(1, TUE - timedelta(minutes=5))]), rss()])
    assert [e.col.collect(p, s)["status"] for p, s in ((SNAP, AAPL_MSFT), (NEWS, NV), (HALTS, NONE))] == ["COMPLETED", "COMPLETED", "EMPTY"]
    days = sessions(date(2025, 11, 17), date(2025, 12, 1))
    hole = add_daily(e, "SPY", [d for d in days if d not in (date(2025, 11, 24), date(2025, 11, 25))], start=days[0], end=days[-1])
    whole = add_daily(e, "QQQ", days, feed="sip")
    minute = add_minutes(e, "SPY", [ny(date(2025, 12, 1), 10, m) for m in (0, 1, 2)])
    r = rep(e, TUE + 60 * S)
    cov = {d["dataset_id"]: d for d in r["coverage"]["datasets"]}
    spy = cov[hole]["per_symbol"]["SPY"]
    assert (spy["result"], spy["missing_sessions"], cov[hole]["codes"]) == ("BLOCKED", ["2025-11-24", "2025-11-25"], ["BELOW_MIN_COVERAGE",
                                                                                                         "DAILY_BAR_BLOCKED"])   # the 7.1 hole rule
    assert (cov[whole]["expected"], cov[whole]["observed"], cov[whole]["codes"]) == (len(days), len(days), [])
    snap = next(d for d in cov.values() if d["kind"] == "QUOTE_SNAPSHOT")
    assert (snap["per_symbol"], snap["ratio"], snap["codes"]) == ({"AAPL": "VALUE", "MSFT": "ABSENCE"}, 0.5, ["BELOW_MIN_COVERAGE"])
    assert {(d["kind"], d["reason"]) for d in cov.values() if d["expectation"] == "INSUFFICIENT_DATA"} == {
        ("NEWS", "NO_EVENT_IS_NOT_MISSING_DATA"), ("HALT", "NO_EVENT_IS_NOT_MISSING_DATA"), ("BAR_1MIN", "NO_PRINTS_MAKE_INTRADAY_COUNTS_UNKNOWN")}
    assert cov[minute]["observed"] == 3 and cov[minute]["expected"] is None                                                   # counted, never expected
    by = {(a["provider"], a["kind"], a["evidence"]): (a["expected"], a["observed"]) for a in r["coverage"]["by_provider_kind_evidence"]}
    assert by == {("ALPACA", "BAR_1DAY", "OPERATOR"): (2 * len(days), 2 * len(days) - 2), ("ALPACA", "QUOTE_SNAPSHOT", "FIXTURE"): (2, 1)}
    assert chk(r, "coverage")["result"] == "BLOCK"
    lax = rep(e, TUE + 60 * S, thresholds=Q.Thresholds(min_coverage=0.5))
    assert "BELOW_MIN_COVERAGE" not in chk(lax, "coverage")["codes"] and chk(lax, "coverage")["result"] == "BLOCK"            # the hole rule is pinned


def test_Q03b_schedule_coverage_is_the_share_of_a_plans_open_minutes_its_70_policy_calls_current_and_recorded_gaps_warn(tmp_path):
    e = env(tmp_path, outcomes=[rss(), news([])])
    e.col.collect(HALTS, NONE)
    e.col.collect(NEWS, NV)
    r = rep(e, TUE + timedelta(minutes=10))
    sch = {x["source"]: x for x in r["coverage"]["schedules"]}
    h = sch["halts"]                                                                       # HALT_FEED: FRESH 120 s, AGING 180 s, regular session
    assert (h["expected_minutes"], h["current_minutes"], h["not_current_intervals"]) == (11, 4, [[ST.ts(TUE + timedelta(minutes=4)),
                                                                                                   ST.ts(TUE + timedelta(minutes=11))]])
    assert (sch["alpaca_news"]["expected_minutes"], sch["alpaca_news"]["current_minutes"]) == (11, 11)   # NEWS_POLL: AGING up to 600 s
    assert "SCHEDULE_BELOW_MIN_COVERAGE" in chk(r, "coverage")["codes"]
    e.clk.t = TUE + timedelta(hours=48)                                                    # Thursday: 48 h without a run (as 7.3 W04)
    e.ft.push(news([]))
    gap = e.col.collect(NEWS, NV)["gap"]
    r = rep(e, e.clk() + 60 * S, since=TUE - timedelta(hours=1))
    n = next(x for x in r["coverage"]["schedules"] if x["source"] == "alpaca_news")
    assert n["gaps_recorded"] == [gap] and {"COLLECTOR_GAPS", "SCHEDULE_BELOW_MIN_COVERAGE"} <= set(chk(r, "coverage")["codes"])
    assert n["current_minutes"] == 13 and 0 < n["ratio"] < 1                               # 11 after the first run, 2 after the second: a
    assert n["not_current_interval_count"] >= 1                                            # success counts only once it has ended


def test_Q03c_a_dataset_that_re_delivers_an_unchanged_value_covered_it(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S), snaps(AAPL=TUE - S)])
    assert e.col.collect(SNAP, AAPL_IEX)["status"] == "COMPLETED"
    e.clk.tick(60)
    assert e.col.collect(SNAP, AAPL_IEX)["status"] == "COMPLETED"                         # the same quote again: a DUPLICATE, no new row
    r = rep(e, e.clk() + S)
    assert [d["per_symbol"] for d in r["coverage"]["datasets"] if d["kind"] == "QUOTE_SNAPSHOT"] == [{"AAPL": "VALUE"}] * 2
    assert chk(r, "coverage")["result"] == "PASS" and stream(r, "QUOTE_SNAPSHOT", "AAPL")["values"] == 1


def test_Q04_a_missing_empty_or_unreadable_store_is_never_created_and_never_passes(tmp_path):
    none = tmp_path / "none" / "mi.db"
    r = Q.build_report(none, as_of=TUE)
    assert (r["verdict"]["result"], chk(r, "inputs")["result"], chk(r, "inputs")["codes"]) == ("INSUFFICIENT_DATA", "INSUFFICIENT_DATA", ["STORE_NOT_CREATED"])
    assert not none.parent.exists()
    assert all(r[s] is None for s in Q.SECTIONS) and {c["check"] for c in r["checks"] if c["required"]} == {"inputs", "integrity", "timestamps", "freshness", "coverage"}
    zero = tmp_path / "zero.db"
    zero.write_bytes(b"")
    z = chk(Q.build_report(zero, as_of=TUE), "inputs")
    assert (z["result"], z["codes"], zero.read_bytes()) == ("INSUFFICIENT_DATA", ["STORE_EMPTY"], b"")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database " * 100)
    r = Q.build_report(junk, as_of=TUE)
    assert (chk(r, "inputs")["codes"], r["verdict"]["result"]) == (["STORE_NOT_A_DATABASE"], "BLOCK")
    e = env(tmp_path / "e", outcomes=[rss()])
    assert e.col.collect(HALTS, NONE)["status"] == "EMPTY"                                 # looked, found nothing
    r = rep(e, TUE + 30 * S)
    assert (chk(r, "inputs")["codes"], r["verdict"]["result"]) == (["NO_ROWS_AT_AS_OF"], "INSUFFICIENT_DATA")
    assert chk(rep(e, TUE - timedelta(microseconds=1)), "inputs")["codes"] == ["STORE_EMPTY_AT_AS_OF"]   # the dataset did not exist yet
    extra = tmp_path / "extra.db"
    shutil.copyfile(e.store.path, extra)
    c = sqlite3.connect(str(extra))
    c.execute("CREATE TABLE intruder (x)")
    c.commit()
    c.close()
    r = Q.build_report(extra, as_of=TUE + 30 * S)
    assert (chk(r, "inputs")["codes"], r["verdict"]["result"]) == (["STORE_SCHEMA_MISMATCH"], "BLOCK")


def test_Q05_negative_latency_within_the_skew_is_measured_and_beyond_it_or_in_a_fetch_it_blocks(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE + 1.5 * S)])                               # the quote's clock 1.5 s ahead of ours: within the skew
    assert e.col.collect(SNAP, AAPL_IEX)["status"] == "COMPLETED"
    r = rep(e, TUE + 10 * S)
    g = next(x for x in r["latency"]["feed"] if x["kind"] == "QUOTE_SNAPSHOT")
    assert (g["negative_within_skew"], g["delivery_s"]["min"], g["delivery_s"]["n"]) == (1, -1.5, 1) and chk(r, "timestamps")["result"] == "PASS"
    raw_fetch(e.store.path, "neg", started=ST.ts(TUE + 10 * S), retrieved=ST.ts(TUE + 5 * S), ingested=ST.ts(TUE + 10 * S), latency_ms=-5,
              endpoint="/v2/stocks/snapshots")
    r = rep(e, TUE + 20 * S)
    assert (chk(r, "timestamps")["codes"], r["verdict"]["result"]) == (["NEGATIVE_FETCH_DURATION", "NEGATIVE_LATENCY_MS"], "BLOCK")
    f = next(x for x in r["latency"]["fetch"] if x["evidence"] == "UNATTRIBUTED")
    assert (f["successful_fetches"], f["latency_ms"]["n"], f["duration_ms"]["n"]) == (1, 0, 0)                 # a contradiction is never a figure
    did = e.store.create_dataset(fixture("QUOTE_SNAPSHOT", feed="iex", symbols=["AAPL"]))
    late = ST.ts(TUE + 12 * S)
    clone_obs(e.store.path, "AAPL", did, obs_id="ahead", natural_key="QUOTE_SNAPSHOT|AAPL|ahead", observed_at=late, event_end=late)
    r = rep(e, TUE + 30 * S)
    assert "OBSERVED_AFTER_RETRIEVAL" in chk(r, "timestamps")["codes"]                     # 12 s ahead: beyond the skew
    g = next(x for x in r["latency"]["feed"] if x["kind"] == "QUOTE_SNAPSHOT")
    assert g["delivery_s"]["min"] == -1.5 and g["delivery_s"]["n"] == 1                       # and never a latency sample


def test_Q06_integrity_reuses_the_71_and_731_verification_and_a_critical_failure_is_never_outweighed(tmp_path, monkeypatch):
    e = v1_world(tmp_path / "w")
    at = TUE + timedelta(minutes=5)
    clean = rep(e, at)
    assert (chk(clean, "integrity")["result"], clean["integrity"]["state"], clean["integrity"]["sqlite_integrity_check"]) == ("PASS", "VERIFIED", ["ok"])
    blind = rep(e, at, archive_root=None)
    assert (chk(blind, "integrity")["codes"], blind["verdict"]["result"]) == (["NO_ANCHORS"], "INSUFFICIENT_DATA")     # seals cannot be confirmed
    gone = rep(e, at, archive_root=tmp_path / "nowhere")
    assert (chk(gone, "integrity")["codes"], gone["verdict"]["result"]) == (["ARCHIVE_NOT_FOUND", "NO_ANCHORS"], "INSUFFICIENT_DATA")

    def copy(name):
        shutil.copytree(tmp_path / "w", tmp_path / name)
        return world_paths(tmp_path / name)
    db, state, archive = copy("forged")
    tamper(db, "mi_observations", "UPDATE mi_observations SET payload_json = '{\"headline\":\"forged\"}' WHERE rowid = 1")
    r = Q.build_report(db, as_of=at, state=state, archive_root=archive)
    assert (chk(r, "integrity")["result"], r["verdict"]["result"]) == ("BLOCK", "BLOCK") and "INTEGRITY_CORRUPT" in chk(r, "integrity")["codes"]
    assert "row_hash_mismatches" in r["integrity"]["findings"]
    assert results(r, "integrity") == results(clean, "integrity")                          # everything else as good as before: still BLOCK
    db, state, archive = copy("body")
    body = sorted(archive.rglob("*.gz"))[0]
    body.write_bytes(gzip.compress(b"another body"))
    r = Q.build_report(db, as_of=at, state=state, archive_root=archive)
    assert (chk(r, "integrity")["result"], chk(r, "integrity")["codes"], r["integrity"]["archive"]["manifest_problems"][0]["problem"]) == (
        "BLOCK", ["RAW_ARCHIVE_MISMATCH"], "SHA256_MISMATCH")                                  # a body that is not what was fetched blocks
    db, state, archive = copy("anchors")
    with open(archive / "anchors.jsonl", "a", encoding="utf-8") as f:
        f.write("not an anchor\n")
    assert chk(Q.build_report(db, as_of=at, state=state, archive_root=archive), "integrity")["codes"] == ["ANCHORS_UNREADABLE"]
    v2p = tmp_path / "m" / "v2.db"
    SV.migrate_copy(e.store.path, v2p, anchors=e.archive.anchors(), clock=e.clk)
    from market_intel.store_v2 import MarketIntelStoreV2
    real, seen = MarketIntelStoreV2.verify_integrity, []

    def spy(self, **kw):
        seen.append(kw)
        return real(self, **kw)
    monkeypatch.setattr(MarketIntelStoreV2, "verify_integrity", spy)
    r = Q.build_report(v2p, as_of=at, archive_root=e.archive.root)
    monkeypatch.undo()
    assert (r["inputs"]["store"]["schema"], chk(r, "integrity")["result"]) == ({"state": "OK", "schema_version": 2}, "PASS")
    assert seen == [{"anchors": e.archive.anchors()}]                                       # the 7.3.1 verification itself, with the anchors
    tamper(v2p, "mi_observations", "UPDATE mi_observations SET payload_json = '{\"headline\":\"forged\"}' WHERE rowid = 1")
    assert chk(Q.build_report(v2p, as_of=at, archive_root=e.archive.root), "integrity")["result"] == "BLOCK"
    e.clk.tick(60)
    e.store.create_dataset(fixture("NEWS"))                                               # a run in flight: nothing contradicts, something pends
    r = rep(e, e.clk() + S)
    assert (chk(r, "integrity")["result"], chk(r, "integrity")["codes"]) == ("WARN", ["PENDING_FINALIZATION"])


def test_Q07_evaluation_times_are_explicit_instants_and_every_boundary_follows_the_stores_own_rule(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S)])
    e.col.collect(SNAP, AAPL_IEX)
    with pytest.raises(ValueError, match="as_of"):
        Q.build_report(e.store.path, as_of=datetime(2025, 12, 2, 15, 0))                    # naive: refused, never guessed
    with pytest.raises(ValueError, match="since"):
        Q.build_report(e.store.path, as_of=TUE, since=TUE + S)
    with pytest.raises(ValueError, match="since"):
        Q.build_report(e.store.path, as_of=TUE, since=datetime(2025, 12, 2))
    est = (TUE + 4 * S).astimezone(timezone(timedelta(hours=-5)))
    assert Q.dumps(rep(e, est)) == Q.dumps(rep(e, TUE + 4 * S))                              # one instant, whatever its offset
    assert stream(rep(e, TUE), "QUOTE_SNAPSHOT", "AAPL")["values"] == 1                       # ingested and available exactly at as_of: known
    assert chk(rep(e, TUE - timedelta(microseconds=1)), "inputs")["codes"] == ["STORE_EMPTY_AT_AS_OF"]
    did = e.store.create_dataset(fixture("NEWS"))                                         # ingested now, usable only from TUE + 10 min
    e.store.ingest(did, [Observation(kind="NEWS", provider="ALPACA", observed_at=TUE - 60 * S, published_at=TUE - 60 * S, retrieved_at=TUE,
                                     available_at=TUE + timedelta(minutes=10), session=CAL.session(TUE)["session"], session_date=CAL.session(TUE)["session_date"],
                                     status="AVAILABLE", source_identifier="embargo", payload={"headline": "embargoed"})])
    seal(e, did)
    assert [sum(s["values"] for s in rep(e, TUE + d)["freshness"]["streams"] if s["kind"] == "NEWS")
            for d in (timedelta(minutes=10) - timedelta(microseconds=1), timedelta(minutes=10))] == [0, 1]   # known only once available
    for t in (datetime(2025, 12, 3, 4, 59, 59, 999999, UTC), datetime(2025, 12, 3, 5, 0, tzinfo=UTC),     # New York midnight: 05:00 UTC in winter
              datetime(2025, 7, 1, 3, 59, 59, 999999, UTC), datetime(2025, 7, 1, 4, 0, tzinfo=UTC)):        # and 04:00 UTC in summer
        e.clk.t = t
        e.store.record_budget_event("ALPHA_VANTAGE", "ALLOW")
    r = rep(e, datetime(2025, 12, 4, tzinfo=UTC))
    assert r["provider_health"]["budget"]["ALPHA_VANTAGE"]["allows_by_new_york_day"] == {"2025-06-30": 1, "2025-07-01": 1, "2025-12-02": 1, "2025-12-03": 1}


def test_Q07b_schedule_windows_follow_new_york_time_across_the_daylight_saving_change(tmp_path):
    for day, open_, close in ((date(2026, 3, 6), "14:30", "21:00"), (date(2026, 3, 9), "13:30", "20:00")):   # EST, then EDT from 8 March 2026
        t0 = datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)
        e = env(tmp_path / day.isoformat(), t=t0, outcomes=[snaps(AAPL=t0 - S)])
        assert e.col.collect(SNAP, AAPL_IEX)["status"] == "COMPLETED"
        s = next(x for x in rep(e, t0 + timedelta(hours=12))["coverage"]["schedules"] if x["source"] == "alpaca_snapshots")
        assert (s["expected_minutes"], s["current_minutes"]) == (390, 0)                    # QUOTE_SWING: the 390 regular-session minutes
        assert s["not_current_intervals"] == [[f"{day}T{open_}:00.000000Z", f"{day}T{close}:00.000000Z"]]


def test_Q08_a_report_as_of_t_never_sees_what_was_recorded_after_t(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S), news([art(1, TUE - timedelta(minutes=5))])])
    e.col.collect(SNAP, AAPL_MSFT)
    e.col.collect(NEWS, NV)
    days = sessions(date(2025, 11, 24), date(2025, 11, 28))
    add_daily(e, "SPY", days)
    add_daily(e, "SPY", days, feed="sip", closes={days[0]: 100.2})
    pending = e.store.create_dataset(fixture("NEWS"))                                     # open at t1, sealed after it
    t1 = TUE + 60 * S
    first = rep(e, t1)
    mine = next(d for d in first["coverage"]["datasets"] if d["dataset_id"] == pending)
    assert (mine["status_at_as_of"], mine["codes"]) == ("RUNNING", ["DATASET_NOT_FINAL"])
    e.clk.t = TUE + 120 * S                                                               # everything below is recorded after t1
    seen = TUE + 30 * S                                                                   # retrieved and available before t1, ingested after it
    e.store.ingest(pending, [Observation(kind="NEWS", provider="ALPACA", observed_at=seen - 60 * S, published_at=seen - 60 * S, retrieved_at=seen,
                                         available_at=seen, session=CAL.session(seen)["session"], session_date=CAL.session(seen)["session_date"],
                                         status="AVAILABLE", source_identifier="late-ingest", payload={"headline": "ingested late"})])
    seal(e, pending)
    raw_fetch(e.store.path, "slow", started=ST.ts(TUE + 50 * S), retrieved=ST.ts(TUE + 55 * S), ingested=ST.ts(TUE + 120 * S), latency_ms=5000)
    raw_fetch(e.store.path, "neg-late", started=ST.ts(TUE + 130 * S), retrieved=ST.ts(TUE + 125 * S), ingested=ST.ts(TUE + 130 * S))
    e.ft.push(snaps(MSFT=e.clk() - S), news([art(1, TUE - timedelta(minutes=5), headline="revised", updated=e.clk()),
                                             art(2, e.clk() - 30 * S)]), *[TR.TransportConnectionError("x")] * 3)
    e.col.collect(SNAP, AAPL_MSFT)                                                        # AAPL absent this time
    e.col.collect(NEWS, NV)
    assert e.col.collect(HALTS, NONE)["status"] == "FAILED"
    e.ft.push(*[TR.TransportConnectionError("x")] * 9)
    for _ in range(3):
        e.clk.tick(120)
        assert e.col.collect(NEWS, NV)["status"] == "FAILED"                              # the news breaker opens after t1
    ahead = e.store.create_dataset(fixture("QUOTE_SNAPSHOT", feed="iex", symbols=["AAPL"]))
    clone_obs(e.store.path, "AAPL", ahead, obs_id="ahead", natural_key="QUOTE_SNAPSHOT|AAPL|ahead", observed_at=ST.ts(TUE + 12 * S),
              event_end=ST.ts(TUE + 12 * S), ingested_at=ST.ts(TUE + 140 * S))                # last: a violation, recorded after t1
    add_daily(e, "SPY", [date(2025, 12, 1)])
    add_daily(e, "SPY", days, feed="sip", closes={d: 101.0 for d in days})                   # a revision of every SIP bar
    e.store.record_budget_event("ALPACA", "ALLOW")
    raw_fetch(e.store.path, "after", started=ST.ts(e.clk()), retrieved=ST.ts(e.clk()), ingested=ST.ts(e.clk()), latency_ms=9)
    again = rep(e, t1)
    for s in ("evidence", "freshness", "coverage", "latency", "timestamps", "provider_health", "feed_divergence"):
        assert again[s] == first[s], s
    assert results(again, "integrity") == results(first, "integrity")
    assert (first["integrity"]["state"], again["integrity"]["state"]) == ("UNVERIFIED", "CORRUPT")  # integrity describes the file (section 3)
    later = rep(e, e.clk() + 60 * S)                                                       # after every later write (the failed run's back-off moved the clock)
    for s in ("freshness", "coverage", "latency", "timestamps", "provider_health", "feed_divergence"):
        assert later[s] != first[s], s                                                    # the later facts exist: the test is not vacuous
    for s in first["freshness"]["streams"]:                                               # and t1 reads as the 7.1 store reads at t1
        ls = e.store.latest_status(s["kind"], at=t1, symbol=s["symbol"], series_id=s["series_id"], feed=s["feed"], adjustment=s["adjustment"])
        assert (s["store_status"], s["store_reason"], s["latest_usable_observed_at"]) == (ls["status"], ls["reason"], (ls["latest_usable"] or {}).get("observed_at"))


def test_Q08b_runs_started_after_t_ends_after_t_and_unverified_successes_never_count():
    T = TUE
    runs = [{"run_id": "a", "started_at": ST.ts(T - 60 * S), "ended_at": ST.ts(T - 30 * S), "status": "COMPLETED", "verified": 1},
            {"run_id": "b", "started_at": ST.ts(T - 20 * S), "ended_at": ST.ts(T + 10 * S), "status": "COMPLETED", "verified": 1},   # ends after T
            {"run_id": "c", "started_at": ST.ts(T + 5 * S), "ended_at": ST.ts(T + 6 * S), "status": "COMPLETED", "verified": 1},     # starts after T
            {"run_id": "d", "started_at": ST.ts(T - 10 * S), "ended_at": ST.ts(T - 5 * S), "status": "COMPLETED", "verified": 0},    # unverified
            {"run_id": "e", "started_at": ST.ts(T - 8 * S), "ended_at": ST.ts(T - 4 * S), "status": "FAILED", "verified": 0}]
    at = Q._runs_at(runs, T)
    assert [(r["run_id"], r["status"]) for r in at] == [("a", "COMPLETED"), ("b", None), ("d", "COMPLETED"), ("e", "FAILED")]
    assert Q._successes(at) == [(T - 30 * S, T - 60 * S)]                                  # (ended, started) of verified, ended successes
    assert Q._dist(list(range(1, 21))) == {"n": 20, "p50": 10, "p95": 19, "min": 1, "max": 20}   # nearest rank, no interpolation
    assert Q._dist([]) == {"n": 0}


def test_Q09_the_report_reads_copies_only_and_leaves_every_input_byte_for_byte_as_it_was(tmp_path, monkeypatch):
    e = v1_world(tmp_path / "w")
    src = tmp_path / "quiet"
    src.mkdir()
    for name in ("mi.db", "collector.db"):
        shutil.copyfile(tmp_path / "w" / name, src / name)                                 # quiescent files: no -wal, no -shm beside them
    shutil.copytree(tmp_path / "w" / "raw", src / "raw")
    db, state, archive = world_paths(src)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    at = TUE + timedelta(minutes=5)
    before = tree(src)
    r = Q.build_report(db, as_of=at, state=state, archive_root=archive, tmp_root=scratch)
    assert chk(r, "integrity")["result"] == "PASS" and tree(src) == before and list(scratch.iterdir()) == []   # nothing beside the inputs; copies gone
    files = [p for p in src.rglob("*") if p.is_file()]
    for p in files:
        os.chmod(p, stat.S_IREAD)                                                          # read-only files: nothing needs to write them
    try:
        assert Q.dumps(Q.build_report(db, as_of=at, state=state, archive_root=archive, tmp_root=scratch)) == Q.dumps(r) and tree(src) == before
    finally:
        for p in files:
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    control = tmp_path / "control"
    control.mkdir()
    shutil.copyfile(db, control / "mi.db")
    MarketIntelStore(control / "mi.db").status_counts()                                   # the 7.1 read path, in place, on a WAL file
    assert sorted(p.name for p in control.iterdir()) == ["mi.db", "mi.db-shm", "mi.db-wal"]  # what reading in place would leave beside an input
    w = sqlite3.connect(str(db))
    w.execute("PRAGMA wal_autocheckpoint = 0")
    w.execute("INSERT INTO mi_budget_events (at, provider, event) VALUES (?, 'ALPACA', 'ALLOW')", (ST.ts(TUE),))
    w.commit()                                                                             # a writer holding un-checkpointed pages in -wal
    try:
        busy = Q.build_report(db, as_of=at, state=state, archive_root=archive, tmp_root=scratch)
        assert (chk(busy, "inputs")["codes"], busy["verdict"]["result"]) == (["STORE_WRITER_MAY_BE_ACTIVE"], "BLOCK") and list(scratch.iterdir()) == []
    finally:
        w.close()
    real, calls = Q._side_sizes, []

    def a_writer_starts(p):
        calls.append(p)
        return real(p) if len(calls) == 1 else {"-wal": 4096}
    monkeypatch.setattr(Q, "_side_sizes", a_writer_starts)
    moved = Q.build_report(db, as_of=at, tmp_root=scratch)
    assert (chk(moved, "inputs")["codes"], list(scratch.iterdir())) == (["STORE_CHANGED_DURING_SNAPSHOT"], [])
    monkeypatch.undo()
    odd = tmp_path / "odd.db"
    shutil.copyfile(state, odd)
    c = sqlite3.connect(str(odd))
    c.execute("CREATE TABLE intruder (x)")
    c.commit()
    c.close()
    r = Q.build_report(db, as_of=at, state=odd, archive_root=archive)
    assert (chk(r, "provider_health")["result"], chk(r, "provider_health")["codes"]) == ("BLOCK", ["STATE_SCHEMA_MISMATCH"])
    assert r["inputs"]["collector_state"]["schema"]["state"] == "SCHEMA_MISMATCH" and r["verdict"]["result"] == "BLOCK"
    assert not ST.default_path().exists()


def test_Q10_the_report_is_deterministic_json_with_a_digest_and_the_cli_prints_it_or_a_summary(tmp_path):
    e = v1_world(tmp_path / "w")
    at = TUE + timedelta(minutes=5)
    a, b = rep(e, at), rep(e, at)
    assert Q.dumps(a) == Q.dumps(b) and a["report_sha256"] == hashlib.sha256(Q._canonical({k: v for k, v in a.items() if k != "report_sha256"})).hexdigest()
    assert json.loads(Q.dumps(a)) == a
    args = ["--store", str(e.store.path), "--state", str(e.state.path), "--archive-root", str(e.archive.root), "--as-of", ST.ts(at)]
    out = io.StringIO()
    code = Q.main(args + ["--format", "json"], out=out)
    assert json.loads(out.getvalue()) == a and code == Q.EXIT_CODES[a["verdict"]["result"]] == 1                           # WARN
    out = io.StringIO()
    Q.main(args, out=out)
    text = out.getvalue()
    assert f"VERDICT: {a['verdict']['result']}" in text and "EVIDENCE: FIXTURE_ONLY - fixture data only - NOT production observations" in text
    assert a["report_sha256"] in text and "integrity        PASS" in text
    for bad in (["--as-of", "2025-12-02T15:00:00"], ["--as-of", ST.ts(at), "--min-coverage", "0"], ["--as-of", ST.ts(at), "--min-coverage", "1.5"],
                ["--as-of", ST.ts(at), "--max-fetch-p95-ms", "-1"], ["--as-of", ST.ts(at), "--since", "2025-12-03T00:00:00Z"]):
        out = io.StringIO()
        assert Q.main(args[:6] + bad, out=out) == Q.USAGE_ERROR and out.getvalue().startswith("refused:"), bad
    assert Q.main([], out=io.StringIO()) == Q.USAGE_ERROR


def test_Q11_every_figure_says_whether_it_comes_from_fixtures_offline_verification_or_operator_data(tmp_path):
    e = env(tmp_path / "e", outcomes=[snaps(AAPL=TUE - S)])
    e.col.collect(SNAP, AAPL_IEX)
    fx = rep(e, TUE + 10 * S)
    assert (fx["evidence"]["scope"], fx["evidence"]["production_observations_possible"], fx["verdict"]["evidence_scope"]) == ("FIXTURE_ONLY", False, "FIXTURE_ONLY")
    days = sessions(date(2025, 11, 24), date(2025, 12, 1))
    add_daily(e, "SPY", days)                                                             # IMPORT: operator data
    add_daily(e, "SPY", days[:2], job="FIXTURE", closes={days[0]: 101.0})                # a fixture in the same stream: one revision, one re-delivery
    add_daily(e, "QQQ", days)
    add_daily(e, "QQQ", days[:1], job="FIXTURE")                                         # a fixture that only re-delivered: still in that stream
    r = rep(e, TUE + 10 * S)
    assert (r["evidence"]["scope"], r["evidence"]["datasets_by_class"]) == ("INCLUDES_OPERATOR_DATA", {"FIXTURE": 3, "OPERATOR": 2})
    for sym in ("SPY", "QQQ"):
        s = stream(r, "BAR_1DAY", sym, "iex")
        assert (s["evidence"], s["mixed_evidence"]) == (["FIXTURE", "OPERATOR"], True), sym
    assert "MIXED_EVIDENCE" in chk(r, "freshness")["codes"]
    assert {(a["kind"], a["evidence"]) for a in r["coverage"]["by_provider_kind_evidence"]} == {("BAR_1DAY", "FIXTURE"), ("BAR_1DAY", "OPERATOR"),
                                                                                               ("QUOTE_SNAPSHOT", "FIXTURE")}
    assert {(g["kind"], g["evidence"]): g["delivery_s"]["n"] for g in r["latency"]["feed"]} == {("QUOTE_SNAPSHOT", "FIXTURE"): 1, ("BAR_1DAY", "FIXTURE"): 1}
    bar = next(g for g in r["latency"]["feed"] if g["kind"] == "BAR_1DAY")
    assert (bar["reference"], bar["delivery_s"]["p50"]) == ("event_end", (TUE - datetime(2025, 11, 25, 1, tzinfo=UTC)).total_seconds())  # 24 Nov ends 20:00 ET
    # IMPORT rows are history, not latency; the fixture's re-delivery of an unchanged bar is no new sample, its revision is one
    assert r["latency"]["excluded"]["backfilled_rows"] == 2 * len(days)
    assert {(f["endpoint"], f["evidence"]) for f in r["provider_health"]["fetches"]} == {("/v2/stocks/snapshots", "FIXTURE")}
    s, clk = v2_store(tmp_path / "v2")
    did = s.create_dataset(collect_spec(), approval=load(tmp_path / "v2", auth_file(tmp_path / "v2"), clk))
    s.ingest(did, news_obs([art(1, TUE - timedelta(minutes=1))], clk(), did), fetch_id=ok_fetch(s, did, clk))
    s.finish_dataset(did, "COMPLETED")
    r = Q.build_report(s.path, as_of=TUE + 10 * S)
    assert (r["evidence"]["scope"], r["evidence"]["datasets_by_class"]) == ("OFFLINE_VERIFICATION_ONLY", {"OFFLINE_VERIFICATION": 1})
    assert [g["evidence"] for g in r["latency"]["feed"]] == ["OFFLINE_VERIFICATION"] and "NOT production observations" in Q.render_summary(r)


def test_Q12_provider_health_summarizes_fetch_outcomes_errors_plan_pressure_budgets_runs_and_breakers(tmp_path):
    e = env(tmp_path, outcomes=[Response(429, {"Retry-After": "7"}), news([])])
    assert e.col.collect(NEWS, NV)["status"] == "EMPTY"
    e.clk.tick(120)
    e.ft.push(Response(429, {"Retry-After": "1"}), news([]))
    assert e.col.collect(NEWS, NV)["status"] == "EMPTY"
    e.ft.push(*[TR.TransportConnectionError("x")] * 9)
    for _ in range(3):
        e.clk.tick(120)
        assert e.col.collect(HALTS, NONE)["status"] == "FAILED"
    e.clk.tick(120)
    e.ft.push(news([], **{"Date": format_datetime(e.clk() + 5 * S, usegmt=True)}))
    assert e.col.collect(NEWS, NV)["clock_skew"] is True
    for _ in range(7):
        e.store.record_budget_event("ALPHA_VANTAGE", "ALLOW")
    r = rep(e, e.clk() + S)
    h = r["provider_health"]
    c = sqlite3.connect(f"file:{Path(e.store.path).as_posix()}?mode=ro&immutable=1", uri=True)
    starts = [x[0] for x in c.execute("SELECT started_at FROM mi_fetches WHERE http_status = 429 ORDER BY seq")]
    c.close()
    assert len(starts) == 2 and h["plan_pressure"] == {"ALPACA": [{"from": starts[0], "to": starts[1], "evidence": ["FIXTURE"]}]}   # two 429s, 10 min
    nf = next(f for f in h["fetches"] if f["endpoint"] == "/v1beta1/news")
    assert nf["by_http_status"]["429"] == 2 and nf["evidence"] == "FIXTURE"
    halts = next(x for x in h["collector"] if x["source"] == "halts")
    assert (halts["by_status"], halts["breaker"], halts["latest"]["status"], halts["errors"][0]["count"]) == ({"FAILED": 3}, "OPEN", "FAILED", 3)
    assert h["budget"]["ALPHA_VANTAGE"]["days_over_daily_cap"] == ["2025-12-02"]
    assert h["budget"]["ALPHA_VANTAGE"]["events_by_evidence"] == {"UNATTRIBUTED": 7} and set(h["budget"]["ALPACA"]["events_by_evidence"]) <= {"FIXTURE",
                                                                                                                                       "UNATTRIBUTED"}
    assert set(chk(r, "provider_health")["codes"]) == {"PLAN_LIMIT_PRESSURE", "CIRCUIT_OPEN", "LATEST_RUN_NOT_COMPLETE", "RUN_ERRORS_RECORDED",
                                                         "FETCH_ERRORS_RECORDED", "CLOCK_SKEW", "DAILY_CAP_EXCEEDED"}
    assert chk(r, "provider_health")["result"] == "WARN" and "DATASET_FAILED" in chk(r, "coverage")["codes"]


def test_Q13_iex_and_sip_bars_at_one_time_are_compared_by_the_70_divergence_rule_and_never_corrected(tmp_path):
    e = env(tmp_path)
    days = sessions(date(2025, 11, 24), date(2025, 11, 26))
    add_daily(e, "SPY", days)
    r = rep(e, TUE + 10 * S)
    assert (chk(r, "feed_divergence")["result"], chk(r, "feed_divergence")["codes"]) == ("INSUFFICIENT_DATA", ["NO_FEED_OVERLAP"])
    sip = dict(zip(days, (100.05, 100.2, 100.0)))
    add_daily(e, "SPY", days, feed="sip", closes=sip)
    fri = date(2025, 11, 28)
    add_daily(e, "SPY", [fri])
    add_daily(e, "SPY", [fri], feed="sip", closes={fri: 130.0}, flags=("NON_POSITIVE_PRICE",))   # unusable: never compared
    r = rep(e, TUE + 10 * S)
    bps = sorted(round(abs(100.0 - c) / c * 10_000, 6) for c in sip.values())
    assert r["feed_divergence"]["pairs"] == [{"kind": "BAR_1DAY", "symbol": "SPY", "adjustment": "raw", "pairs": 3, "divergent": 1, "not_computable": 0,
                                              "bps": {"n": 3, "p50": bps[1], "p95": bps[2], "min": bps[0], "max": bps[2]}, "evidence": ["OPERATOR"]}]
    assert (chk(r, "feed_divergence")["result"], chk(r, "feed_divergence")["codes"]) == ("WARN", ["FEED_DIVERGENCE"])
    assert [x["payload"]["close"] for x in e.store.history("BAR_1DAY", symbol="SPY", feed="sip", as_of=TUE + 10 * S) if x["usable"]] == [100.05, 100.2, 100.0]


def test_Q14_sec_times_stay_blocked_they_are_neither_graded_nor_measured(tmp_path):
    e = env(tmp_path)
    did = e.store.create_dataset(fixture("FILING", provider="SEC", symbols=["AAPL"]))
    t = TUE - timedelta(minutes=30)
    e.store.ingest(did, [Observation(kind="FILING", symbol="AAPL", provider="SEC", observed_at=t, retrieved_at=TUE, available_at=TUE,
                                     session=CAL.session(t)["session"], session_date=CAL.session(t)["session_date"], status="AVAILABLE",
                                     source_identifier="0000320193-25-000001", payload={"form": "8-K"})])
    seal(e, did)
    r = rep(e, TUE + 10 * S)
    f = stream(r, "FILING", "AAPL")
    assert (f["assessed_by"], f["reason"], f["grades"]) == ("NOT_ASSESSED", "SEC_ACCEPTANCE_TZ_UNVERIFIED", {})
    assert (r["latency"]["excluded"]["SEC_ACCEPTANCE_TZ_UNVERIFIED"], r["latency"]["feed"]) == (1, [])
    assert (chk(r, "freshness")["result"], chk(r, "freshness")["codes"]) == ("INSUFFICIENT_DATA", ["SEC_ACCEPTANCE_TZ_UNVERIFIED"])
    assert "FILING" not in r["freshness"]["event_age_s"] and chk(r, "coverage")["result"] == "INSUFFICIENT_DATA"
    assert (f["latest_record"]["times"], f["latest_record"]["ages_s"], f["latest_record"]["withheld"], f["latest_usable_observed_at"]) == (
        None, None, "SEC_ACCEPTANCE_TZ_UNVERIFIED", None)                                  # not one SEC time is measured
    assert r["verdict"]["result"] == "INSUFFICIENT_DATA"
    assert [PL.PLANS[s].blocked_reason for s in ("sec_filings", "sec_facts")] == [PL.SEC_TZ_BLOCK] * 2          # still blocked, unchanged


def test_Q15_thresholds_are_pinned_or_explicit_and_a_check_without_one_is_measured_not_judged(tmp_path):
    t = Q.Thresholds()
    assert (t.min_coverage, t.max_fetch_p95_ms, t.max_feed_p95_s) == (1.0, None, None)
    for bad in (dict(min_coverage=0), dict(min_coverage=1.5), dict(min_coverage=True), dict(min_coverage="1"), dict(max_fetch_p95_ms=0),
                dict(max_fetch_p95_ms=float("nan")), dict(max_fetch_p95_ms=float("inf")), dict(max_feed_p95_s=-1), dict(max_feed_p95_s=True)):
        with pytest.raises(ValueError):
            Q.Thresholds(**bad)
    p = Q.pinned()
    assert (p["clock_skew_s"], p["news_skew_s"], p["feed_divergence_bps"], p["session_hole_days"], p["plan_pressure"]) == (2.0, 300.0, 10.0, 2,
                                                                                                                      {"window_s": 600.0, "responses_429": 2})
    assert p["freshness_policies"]["QUOTE_DAY"] == {"reference": "quote time", "fresh_s": 5, "aging_s": 15, "window": "REG"}
    assert (p["default_budgets"]["MI_ALPACA_MAX_PER_MIN"], p["default_budgets"]["MI_ALPHA_VANTAGE_MAX_PER_DAY"], p["freshness_policy_version"]) == (40, 6,
                                                                                                                                           "mi_freshness_v2")
    e = env(tmp_path, outcomes=[news([art(1, TUE - timedelta(minutes=5))])])
    e.col.collect(NEWS, NV)
    at = TUE + 10 * S
    assert chk(rep(e, at), "latency")["codes"] == ["NO_LATENCY_TARGET"]                       # measured, never judged against an invented SLA
    ok = rep(e, at, thresholds=Q.Thresholds(max_fetch_p95_ms=1000, max_feed_p95_s=3600))
    assert (chk(ok, "latency")["result"], chk(ok, "latency")["detail"]) == ("PASS", "judged against: fetch, delivery")
    slow = rep(e, at, thresholds=Q.Thresholds(max_feed_p95_s=60))                            # the article arrived 300 s after it was written
    assert (chk(slow, "latency")["result"], chk(slow, "latency")["codes"]) == ("WARN", ["DELIVERY_P95_ABOVE_TARGET"])
    assert ok["thresholds"] == {"configurable": {"min_coverage": 1.0, "max_fetch_p95_ms": 1000, "max_feed_p95_s": 3600}, "pinned": p}
    raw_fetch(e.store.path, "slow", started=ST.ts(TUE + S), retrieved=ST.ts(TUE + 6 * S), ingested=ST.ts(TUE + 6 * S), latency_ms=5000)
    late = rep(e, at, thresholds=Q.Thresholds(max_fetch_p95_ms=1000))
    assert (chk(late, "latency")["result"], chk(late, "latency")["codes"]) == ("WARN", ["FETCH_P95_ABOVE_TARGET"])


# ============================================================================================================ verification (V)
def _rank(xs, q):
    s = sorted(xs)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def test_V01_the_figures_recompute_from_raw_sql_and_the_71_store_reads(tmp_path):
    e = v1_world(tmp_path / "w")
    add_daily(e, "SPY", sessions(date(2025, 11, 24), date(2025, 12, 1)))
    at = TUE + timedelta(minutes=5)
    r = rep(e, at)
    a = ST.ts(at)
    c = sqlite3.connect(f"file:{Path(e.store.path).as_posix()}?mode=ro&immutable=1", uri=True)   # immutable: the test writes nothing either
    try:
        lat = {}
        for p, k, f, d in c.execute("SELECT provider, kind, feed, (julianday(retrieved_at) - julianday(CASE WHEN kind LIKE 'BAR_%' THEN event_end ELSE "
                                    "observed_at END)) * 86400.0 FROM mi_observations WHERE backfilled = 0 AND observed_at <= ? AND event_end <= ? AND "
                                    "available_at <= ? AND ingested_at <= ?", (a,) * 4):
            lat.setdefault((p, k, f), []).append(d)
        got = {(g["provider"], g["kind"], g["feed"]): g["delivery_s"] for g in r["latency"]["feed"]}
        assert set(got) == set(lat)
        for key, xs in lat.items():
            assert got[key]["n"] == len(xs) and all(math.isclose(got[key][q], v, abs_tol=1e-3) for q, v in (("p50", _rank(xs, .5)), ("p95", _rank(xs, .95)),
                                                                                                        ("min", min(xs)), ("max", max(xs)))), key
        fx = {}
        for p, ep, ms in c.execute("SELECT provider, endpoint, latency_ms FROM mi_fetches WHERE status IN ('OK', 'NOT_MODIFIED') AND latency_ms IS NOT NULL "
                                   "AND ingested_at <= ?", (a,)):
            fx.setdefault((p, ep), []).append(float(ms))
        assert {(f["provider"], f["endpoint"]): (f["latency_ms"]["n"], f["latency_ms"].get("p95")) for f in r["latency"]["fetch"]} == {
            k: (len(v), _rank(v, .95)) for k, v in fx.items()}
        n_obs = c.execute("SELECT COUNT(*) FROM mi_observations WHERE observed_at <= ? AND event_end <= ? AND available_at <= ? AND ingested_at <= ?",
                          (a,) * 4).fetchone()[0]
    finally:
        c.close()
    assert sum(s["values"] for s in r["freshness"]["streams"]) == n_obs
    for s in r["freshness"]["streams"]:
        assert s["facts"] == len(e.store.history(s["kind"], symbol=s["symbol"], series_id=s["series_id"], feed=s["feed"], adjustment=s["adjustment"], as_of=at))
    spy = next(d for d in r["coverage"]["datasets"] if d["kind"] == "BAR_1DAY")
    q = e.store.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=date(2025, 11, 24), end=date(2025, 12, 1), adjustment="raw", as_of=at)
    assert (spy["per_symbol"]["SPY"]["result"], spy["expected"], spy["observed"]) == (q["result"], q["expected_sessions"], q["present_sessions"])
    v = e.store.verify_integrity(anchors=e.archive.anchors())
    assert (r["integrity"]["state"], r["integrity"]["rows_checked"]) == (v["state"], v["rows_checked"])


# ============================================================================================================ isolation (X)
ALLOWED_74 = {"__future__", "argparse", "collections", "dataclasses", "datetime", "hashlib", "json", "math", "pathlib", "re", "sqlite3", "sys", "tempfile",
              "typing", "zoneinfo", "market_intel", "market_intel.archive", "market_intel.budget", "market_intel.collector", "market_intel.collector.state",
              "market_intel.migrations", "market_intel.settings", "market_intel.store_v2"}
NEW_MODULES_74 = ("quality.py",)


def test_X01_the_report_reaches_no_network_broker_trading_or_model_has_no_write_path_and_nothing_outside_imports_it():
    src = (ROOT / "market_intel" / "quality.py").read_text(encoding="utf-8")
    t = ast.parse(src)
    names = {n.module for n in ast.walk(t) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names}
    assert names <= ALLOWED_74, sorted(names - ALLOWED_74)
    for word in ("urlopen", "urllib", "http.client", "requests", "socket", "alpaca.data", "alpaca.trading", "net.fetch", "broker", "execution", "proposals",
                 "anthropic", "openai", "threading", "subprocess", "os.environ", "getenv", "submit_order", "import config", "INSERT ", "UPDATE ", "DELETE ",
                 "CREATE ", "DROP ", "ALTER ", "REPLACE ", "VACUUM", "journal_mode", "writable_schema", "migrate_copy", "create_dataset", "finish_dataset",
                 "ingest(", "record_fetch", "record_budget_event", "record_quality_audit", "record_news_run", "record_problems", "recover_pending",
                 "write_text", "write_bytes", "rmtree", "os.remove", "shutil", "mode=rw", "_rw("):
        assert word not in src, word
    assert re.findall(r"\bopen\([^)]*\)", src) == ['open(src, "rb")', 'open(dst, "xb")']    # the only file write: the private copy, exclusively
    assert src.count(".unlink(") == 1 and "dst.unlink()" in src                             # a refused copy, in the private directory
    assert src.count("sqlite3.connect(") == 1 and "?mode=ro" in src                         # and it opens only copies, read-only
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",
             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]
    assert users == []
    assert not ST.default_path().exists() and not (ROOT / "runtime" / "market_intel" / "collector.db").exists()



# ============================================================================================================ the independent review (R)
def test_R01_tampered_or_odd_inputs_never_crash_the_report_they_block_it(tmp_path, monkeypatch):
    e = v1_world(tmp_path / "w")
    add_daily(e, "SPY", sessions(date(2025, 11, 24), date(2025, 12, 1)))
    add_daily(e, "SPY", [date(2025, 11, 24)], feed="sip")                                 # one IEX / SIP pair
    at = TUE + timedelta(minutes=5)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def copy(name):
        shutil.copytree(tmp_path / "w", tmp_path / name)
        return world_paths(tmp_path / name)

    def append(path, text):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    cases = {}
    db, state, archive = cases.setdefault("payload", copy("payload"))
    tamper(db, "mi_observations", "UPDATE mi_observations SET payload_json = 'not json' WHERE rowid = 1")
    db, state, archive = cases.setdefault("flags", copy("flags"))
    tamper(db, "mi_observations", "UPDATE mi_observations SET quality_flags_json = '{' WHERE rowid = 1")
    db, state, archive = cases.setdefault("session_date", copy("session_date"))
    tamper(db, "mi_observations", "UPDATE mi_observations SET session_date = 'garbage' WHERE rowid = (SELECT MAX(rowid) FROM mi_observations "
                                  "WHERE kind = 'BAR_1DAY')")
    db, state, archive = cases.setdefault("event_end", copy("event_end"))
    did = MarketIntelStore(db, clock=Clock(TUE)).create_dataset(DatasetSpec(job_kind="IMPORT", kind="BAR_1MIN", provider="ALPACA", feed="iex",
                                                                             adjustment="raw", symbols=["SPY"], timeframe="1Min",
                                                                             start_date=date(2025, 12, 1), end_date=date(2025, 12, 1)))
    clone_obs(db, "SPY", did, obs_id="odd-end", kind="BAR_1MIN", natural_key="BAR_1MIN|SPY|odd", event_end="2025-12-02T14:52:00Z", unchecked=True)
    for name, ms in (("inf_latency", float("inf")), ("text_latency", "slow")):
        db, state, archive = cases.setdefault(name, copy(name))
        raw_fetch(db, name, started=ST.ts(TUE), retrieved=ST.ts(TUE + S), ingested=ST.ts(TUE + S), latency_ms=ms)
    db, state, archive = cases.setdefault("anchor_null", copy("anchor_null"))
    append(archive / "anchors.jsonl", "null\n")
    db, state, archive = cases.setdefault("manifest_json", copy("manifest_json"))
    append(archive / "manifest.jsonl", "{bad\n")
    db, state, archive = cases.setdefault("manifest_key", copy("manifest_key"))
    append(archive / "manifest.jsonl", '{"fetch_id": "x"}\n')
    db, state, archive = cases.setdefault("inf_close", copy("inf_close"))
    tamper(db, "mi_observations", "UPDATE mi_observations SET payload_json = '{\"open\": 1, \"high\": 1, \"low\": 1, \"close\": Infinity, \"volume\": 1}' "
                                  "WHERE feed = 'sip'")
    db, state, archive = cases.setdefault("state_json", copy("state_json"))
    c = sqlite3.connect(str(state))
    c.execute("INSERT INTO cs_runs (run_id, source, scope_key, scope_json, worker, lease_id, plan_sha256, started_at, dataset_id, trial) "
              "VALUES ('odd', 'halts', 'x', 'not json', 'w', 'none', 'x', ?, 'none', 0)", (ST.ts(TUE),))
    c.commit()
    c.close()
    want = {"payload": "INTEGRITY_UNREADABLE", "flags": "INTEGRITY_UNREADABLE", "session_date": "INTEGRITY_CORRUPT", "event_end": "MALFORMED_TIMESTAMP",
            "inf_latency": "INVALID_LATENCY_MS", "text_latency": "INVALID_LATENCY_MS", "anchor_null": "ANCHORS_UNREADABLE",
            "manifest_json": "RAW_ARCHIVE_UNREADABLE", "manifest_key": "RAW_ARCHIVE_UNREADABLE", "state_json": "STATE_UNREADABLE",
            "inf_close": "DIVERGENCE_NOT_COMPUTABLE"}
    for name, (db, state, archive) in cases.items():
        r = Q.build_report(db, as_of=at, state=state, archive_root=archive, tmp_root=scratch)          # never raises
        codes = {x for c in r["checks"] for x in c["codes"]}
        assert r["verdict"]["result"] == "BLOCK" and want[name] in codes, (name, r["verdict"])
        assert json.loads(Q.dumps(r)) == r and str(scratch) not in Q.dumps(r) and list(scratch.iterdir()) == [], name   # valid, no private path
        out = io.StringIO()
        assert Q.main(["--store", str(db), "--state", str(state), "--archive-root", str(archive), "--as-of", ST.ts(at), "--format", "json"], out=out) == 2, name
        assert json.loads(out.getvalue())["verdict"]["result"] == "BLOCK", name
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(Q, "build_report", boom)
    out = io.StringIO()
    assert Q.main(["--store", str(db), "--as-of", ST.ts(at)], out=out) == 2 and out.getvalue().startswith("error:")       # never vouched for


def test_R02_a_pass_needs_freshness_and_coverage_established_and_is_reachable(tmp_path):
    e = env(tmp_path / "news")
    did = e.store.create_dataset(fixture("NEWS"))
    t = TUE - timedelta(hours=1)
    e.store.ingest(did, [Observation(kind="NEWS", provider="ALPACA", observed_at=t, published_at=t, retrieved_at=TUE, available_at=TUE, backfilled=True,
                                     session=CAL.session(t)["session"], session_date=CAL.session(t)["session_date"], status="AVAILABLE",
                                     source_identifier="hist", payload={"headline": "history"})])
    seal(e, did)
    r = Q.build_report(e.store.path, as_of=TUE + 10 * S, archive_root=e.archive.root)
    assert {c["check"]: c["result"] for c in r["checks"] if c["check"] in ("integrity", "timestamps", "freshness", "coverage")} == {
        "integrity": "PASS", "timestamps": "PASS", "freshness": "INSUFFICIENT_DATA", "coverage": "INSUFFICIENT_DATA"}
    assert r["verdict"]["result"] == "INSUFFICIENT_DATA" and r["verdict"]["required"] == ["inputs", "integrity", "timestamps", "freshness", "coverage"]   # never PASS on nothing established
    b = env(tmp_path / "bars")
    add_daily(b, "SPY", sessions(date(2025, 11, 24), date(2025, 12, 1)))
    r = Q.build_report(b.store.path, as_of=TUE + 10 * S, archive_root=b.archive.root)
    assert r["verdict"]["result"] == "PASS" and all(c["result"] == "PASS" for c in r["checks"] if c["required"])
    assert {c["check"] for c in r["verdict"]["not_established"]} == {"latency", "provider_health", "feed_divergence"}
    assert Q.main(["--store", str(b.store.path), "--archive-root", str(b.archive.root), "--as-of", ST.ts(TUE + 10 * S)], out=io.StringIO()) == 0


def test_R03_a_latency_target_with_nothing_to_measure_is_never_judged_met(tmp_path):
    e = env(tmp_path / "live")
    did = e.store.create_dataset(fixture("NEWS"))
    t = TUE - timedelta(minutes=5)
    e.store.ingest(did, [Observation(kind="NEWS", provider="ALPACA", observed_at=t, published_at=t, retrieved_at=TUE, available_at=TUE,
                                     session=CAL.session(t)["session"], session_date=CAL.session(t)["session_date"], status="AVAILABLE",
                                     source_identifier="live", payload={"headline": "live"})])
    seal(e, did)                                                                          # a live row, no fetch at all
    r = rep(e, TUE + 10 * S, state=None, thresholds=Q.Thresholds(max_fetch_p95_ms=1))
    assert (chk(r, "latency")["result"], chk(r, "latency")["codes"]) == ("INSUFFICIENT_DATA", ["NO_FETCH_SAMPLES"])
    r = rep(e, TUE + 10 * S, state=None, thresholds=Q.Thresholds(max_fetch_p95_ms=1, max_feed_p95_s=3600))
    assert (chk(r, "latency")["result"], chk(r, "latency")["codes"]) == ("INSUFFICIENT_DATA", ["NO_FETCH_SAMPLES"])   # one target unknown: not met
    h = env(tmp_path / "history")
    add_daily(h, "SPY", sessions(date(2025, 11, 24), date(2025, 12, 1)))                  # history only, and one fetch
    raw_fetch(h.store.path, "one", started=ST.ts(TUE), retrieved=ST.ts(TUE + S), ingested=ST.ts(TUE + S), latency_ms=5)
    r = rep(h, TUE + 10 * S, state=None, thresholds=Q.Thresholds(max_feed_p95_s=0.001))
    assert (chk(r, "latency")["result"], chk(r, "latency")["codes"]) == ("INSUFFICIENT_DATA", ["NO_DELIVERY_SAMPLES"])


def test_R04_daily_bar_coverage_expects_only_the_sessions_due_by_as_of(tmp_path):
    e = env(tmp_path)
    days = sessions(date(2025, 11, 24), date(2025, 12, 1))
    did = add_daily(e, "SPY", days, end=date(2025, 12, 5))                               # a window ending after T: 2-5 December are not due yet
    future = e.store.create_dataset(DatasetSpec(job_kind="IMPORT", kind="BAR_1DAY", provider="ALPACA", feed="iex", adjustment="raw", symbols=["QQQ"],
                                                timeframe="1Day", start_date=date(2025, 12, 3), end_date=date(2025, 12, 5)))
    seal(e, future, "EMPTY")
    r = Q.build_report(e.store.path, as_of=TUE + 10 * S, archive_root=e.archive.root)
    cov = {d["dataset_id"]: d for d in r["coverage"]["datasets"]}
    assert (cov[did]["not_yet_due_sessions"], cov[did]["per_symbol"]["SPY"]["result"], cov[did]["per_symbol"]["SPY"]["missing_sessions"]) == (4, "PASS", [])
    assert (cov[future]["expectation"], cov[future]["reason"], cov[future]["not_yet_due_sessions"]) == ("INSUFFICIENT_DATA", "NO_SESSION_DUE_AT_AS_OF", 3)
    assert chk(r, "coverage")["result"] == "PASS"                                         # never a hole for sessions still to come
    later = Q.build_report(e.store.path, as_of=datetime(2025, 12, 6, 15, tzinfo=UTC), archive_root=e.archive.root)
    assert {d["dataset_id"]: d["per_symbol"]["SPY"]["missing_sessions"] for d in later["coverage"]["datasets"] if d["dataset_id"] == did} == {
        did: ["2025-12-02", "2025-12-03", "2025-12-04", "2025-12-05"]}                     # once due, they are missing


def test_R05_an_archive_without_anchors_never_vouches_and_a_named_state_file_must_exist(tmp_path):
    e = v1_world(tmp_path / "w")
    at = TUE + timedelta(minutes=5)
    empty = tmp_path / "empty"
    empty.mkdir()
    r = rep(e, at, archive_root=empty)
    assert (chk(r, "integrity")["result"], chk(r, "integrity")["codes"], r["verdict"]["result"]) == ("INSUFFICIENT_DATA", ["ANCHORS_FILE_NOT_FOUND", "NO_ANCHORS"],
                                                                                                   "INSUFFICIENT_DATA")
    bare = tmp_path / "bare"
    shutil.copytree(e.archive.root, bare)
    (bare / "anchors.jsonl").write_bytes(b"")
    assert (chk(rep(e, at, archive_root=bare), "integrity")["codes"]) == ["NO_ANCHORS"]   # no anchor for any sealed dataset
    r = rep(e, at, state=tmp_path / "typo" / "collector.db")
    assert (chk(r, "inputs")["result"], chk(r, "inputs")["codes"], r["verdict"]["result"]) == ("INSUFFICIENT_DATA", ["STATE_NOT_CREATED"], "INSUFFICIENT_DATA")
    assert r["inputs"]["collector_state"]["present"] is False and not (tmp_path / "typo").exists()


def test_R06_rows_breaking_the_stores_rules_never_become_figures(tmp_path):
    e = env(tmp_path, outcomes=[snaps(AAPL=TUE - S)])
    e.col.collect(SNAP, AAPL_IEX)
    did = e.store.create_dataset(fixture("QUOTE_SNAPSHOT", feed="iex", symbols=["AAPL"]))
    clone_obs(e.store.path, "AAPL", did, obs_id="early", natural_key="QUOTE_SNAPSHOT|AAPL|early", ingested_at=ST.ts(TUE - 30 * S))
    clone_obs(e.store.path, "AAPL", did, obs_id="sloppy", natural_key="QUOTE_SNAPSHOT|AAPL|sloppy", observed_at="2025-12-02T14:59:59Z", unchecked=True)
    r = rep(e, TUE + 10 * S)
    assert {"INGESTED_BEFORE_RETRIEVAL", "MALFORMED_TIMESTAMP"} <= set(chk(r, "timestamps")["codes"]) and r["verdict"]["result"] == "BLOCK"
    g = next(x for x in r["latency"]["feed"] if x["kind"] == "QUOTE_SNAPSHOT")
    assert g["ingestion_s"]["min"] >= 0                                                   # the row ingested 30 s early is no -30 s sample
    assert stream(r, "QUOTE_SNAPSHOT", "AAPL")["values"] == 2                               # the malformed time is never compared as text


def test_R07_the_schedule_window_is_bounded_and_since_brings_it_back(tmp_path):
    e = env(tmp_path, outcomes=[rss()])
    e.col.collect(HALTS, NONE)
    far = TUE + timedelta(days=Q.SCHEDULE_MAX_DAYS + 1)
    s = next(x for x in rep(e, far)["coverage"]["schedules"] if x["source"] == "halts")
    assert (s["status"], s["from"]) == ("WINDOW_TOO_LONG", ST.ts(TUE))
    assert "SCHEDULE_WINDOW_TOO_LONG" in chk(rep(e, far), "coverage")["codes"]
    s = next(x for x in rep(e, far, since=far - timedelta(days=1))["coverage"]["schedules"] if x["source"] == "halts")
    assert s["status"] == "EVALUATED"


def test_R08_a_non_finite_threshold_is_refused_by_name_in_python_and_on_the_command_line(tmp_path, monkeypatch):
    for v in (float("nan"), float("inf"), float("-inf"), float("1e999")):                   # the operator's review of 0ebfd3c
        with pytest.raises(ValueError, match="^min_coverage must be finite, not "):
            Q.Thresholds(min_coverage=v)
        with pytest.raises(ValueError, match="^min_coverage must be finite, not "):
            dataclasses.replace(Q.Thresholds(), min_coverage=v)                                # a changed copy is validated as well
        for name in ("max_fetch_p95_ms", "max_feed_p95_s"):
            with pytest.raises(ValueError, match=f"^{name} must be a positive finite number"):
                Q.Thresholds(**{name: v})
    huge = 10 ** 400                                                                           # finite, but no float holds it: never an OverflowError
    with pytest.raises(ValueError, match=r"^min_coverage must be a number in \(0, 1\]$"):
        Q.Thresholds(min_coverage=huge)
    assert (Q.Thresholds(max_fetch_p95_ms=huge).max_fetch_p95_ms, Q.Thresholds(max_feed_p95_s=huge).max_feed_p95_s) == (huge, huge)
    assert [Q.Thresholds(min_coverage=x).min_coverage for x in (1, 0.5, 1e-9)] == [1, 0.5, 1e-9]
    e = v1_world(tmp_path / "w")
    monkeypatch.setattr(Q, "build_report", lambda *a, **k: pytest.fail("a refused invocation must not read anything"))
    base = ["--store", str(e.store.path), "--as-of", ST.ts(TUE)]
    for flag, msg in (("--min-coverage", "min_coverage must be finite, not "), ("--max-fetch-p95-ms", "max_fetch_p95_ms must be a positive finite"),
                      ("--max-feed-p95-s", "max_feed_p95_s must be a positive finite")):
        for text in ("nan", "NaN", "inf", "Infinity", "-inf", "1e999"):
            out = io.StringIO()
            assert Q.main(base + [f"{flag}={text}"], out=out) == Q.USAGE_ERROR and out.getvalue().startswith("refused: " + msg), (flag, text)
    assert Q.main(base + ["--min-coverage", "-inf"], out=io.StringIO()) == Q.USAGE_ERROR                     # read as an option by argparse
    run = subprocess.run([sys.executable, "-m", "market_intel.quality", *base, "--min-coverage", "nan"], cwd=ROOT, capture_output=True, text=True)
    assert (run.returncode, run.stdout) == (Q.USAGE_ERROR, "refused: min_coverage must be finite, not nan\n"), run.stderr[-600:]


# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_74}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_74} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


APP_EDITS_74 = {".github/ci/pytest_groups.json"}
FROZEN_DIRS_74 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                  "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                  "database", "data", "value_quality", "market_intel")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_74 == "b9c36212e0104831d665d22f8a2eaa3d4c4885c9" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_74}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_74).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_74).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_74 and {p for p in changed if p in in_base} == FL.EDITED_74
    assert FL.NEW_74 == {"DESIGN_74_MARKET_INTEL_QUALITY.md", "tests/test_market_intel_quality_74.py"} | {f"market_intel/{m}" for m in NEW_MODULES_74}
    assert {p for p in FL.EDITED_74 if not p.startswith("tests/")} == APP_EDITS_74
    assert FL.SEMANTIC_74 == {} and FL.INSTRUMENTATION_74 == {}
    for d in FROZEN_DIRS_74:                                                                   # every existing module: the 7.0-7.3.1 data layer included
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert now_text(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):
        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook or dependency
    for rel in sorted(FL.NEW_74 | FL.EDITED_74):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_market_intel():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import config
    from market_intel.settings import settings as mi_settings
    assert mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_", "ALPHAVANTAGE"))]          # no new key (config.py itself: F01)
    import setups as SU
    assert (SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.EVIDENCE_MAX_AGE_DAYS) == (40, 60, 30)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_74.items():
        text = now_text(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_74) == FL.EDITED_74 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.4 (market intelligence data quality")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_73 + NEW_731 + NEW_74 plus this ledger.", " + NEW_73 + NEW_731 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_74 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_74: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key
    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_74 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_74 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731                                                       # Stage 7.3.1, pinned by its own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n',
         '    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('\nAPP_EDITS_731 = {".github/ci/pytest_groups.json"}\n',
         '\ndef post(rel):\n    """main after PR #45 (Stage 7.3.1 merged; the Stage 7.4 base): what Stage 7.3.1 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_74}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_74} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_731 = {".github/ci/pytest_groups.json"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_731).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_731, FL.BASE_74).split())               # Stage 7.3.1 itself, between two fixed commits\n    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n                                                           "missing": sorted((FL.NEW_731 | FL.EDITED_731 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_731).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_731).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_731 and {p for p in changed if p in in_base} == FL.EDITED_731\n    assert FL.NEW_731 == {"DESIGN_731_MARKET_INTEL_SCHEMA_V2.md", "tests/test_market_intel_schema_v2_731.py"} | {f"market_intel/{m}" for m in NEW_MODULES_731}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_731).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_731 and {p for p in stage if p in in_base} == FL.EDITED_731\n    assert FL.NEW_731 == {"DESIGN_731_MARKET_INTEL_SCHEMA_V2.md", "tests/test_market_intel_schema_v2_731.py"} | {f"market_intel/{m}" for m in NEW_MODULES_731}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert now_text(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert post(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n'),
        ('    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook or dependency\n    for rel in sorted(FL.NEW_731 | FL.EDITED_731):\n',
         '    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook or dependency\n    for rel in sorted(FL.NEW_731 | FL.EDITED_731):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_731.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_731.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_731) == FL.EDITED_731 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.3.1 (market intelligence schema v2")\n',
         '    assert set(REPLAY_731) == FL.EDITED_731 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.3.1 (market intelligence schema v2")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_731 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.3.1 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_731 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n',
         '    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 | FL.NEW_73 | FL.NEW_731 if p.startswith("market_intel/") and p.endswith(".py")}   # 7.2, 7.3, 7.3.1: own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n',
         '    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 if p.startswith("market_intel/") and p.endswith(".py")}   # 7.2-7.4: own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n'),
        ('    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n',
         '    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
