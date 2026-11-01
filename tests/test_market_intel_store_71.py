"""
Stage 7.1 - the market-intelligence store, code only (DESIGN_71_MARKET_INTEL_STORE.md). Synthetic fixtures and temporary databases only;
no network (the conftest socket block plus explicit guards), no real backfill, nothing written outside tmp_path.

  D01-D03 the database: lazy creation (imports and reads create nothing), WAL, append-only triggers on every table, no instruction
          column, versioned migrations with a pinned DDL hash, SchemaTooNew / SchemaMismatch.
  I01-I10 ingestion and point-in-time reads: idempotent duplicates and a stable dataset hash, revisions (A -> B -> A included), look-ahead
          prevention (available_at AND ingested_at), UTC handling, missing / malformed data never zero, sealed-dataset rules, feed
          separation, calendar coverage fail-closed, all-or-nothing batches, crash recovery (a killed writer), concurrent readers and
          serialized writers, provenance, quality audits, budget history.
  R01-R03 the gated backfill runner with an injected fake transport: approvals bound to the plan, SIP approval, the backfill window,
          paging, raw archive + manifest verification, retries, ACCESS_DENIED, PARTIAL / FAILED / EMPTY, budgets.
  C01-C05 the CLI: offline by default (no socket, no file), refusals (coverage, SIP 15-minute rule, feed, dates), dry-run preview, execute
          always refused, inspect never creates, estimates (2025: 250 sessions; 24 pages per symbol-year of extended minute bars).
  K01-K02 the verified 2025 history calendar (NYSE publications + the empirical cross-check), 2024 / 2028 still NOT_COVERED.
  N01-N12 the PR #42 review findings, each with its regression and negative tests: B1 approvals (a file for the exact request and the plan
          recomputed at run time, used once), B2 dataset scope, B3 failures always sealed, B4 schema objects + row hashes + external
          anchors, B5 chronology, B6 membership manifests, B7 exact recovery keys, B8 no reseal, B9 payloads / absences / latest status,
          item 10 event_end semantics, B11 the 2025 early-close post-market, B10 bounded latest reads and the generic-store plan cap.
  E01     an approved synthetic run end to end: quarantine, PARTIAL, integrity and anchors verified.
  G01-G12 the second independent review (Codex F1-F12): the store's own authority check, derived labels and fetch binding, session scope
          and the delivered-row cap at every entry point, VERIFIED / UNVERIFIED / CORRUPT integrity in both directions, durable pending
          finalization and recovery, sealed immutability (schema guards included), delivery provenance and 7.0 freshness, quality reports
          with invalid sessions, event_end <= T, durable attempt order, strict Alpaca responses, the 2026-2027 late-trading override.
  X01-X08 cross-entry-point invariants (sweeps over every public method, raw SQL where it matters): no BACKFILL without verified
          authority, no observation outside its scope, no change after a seal, no missing anchored dataset VERIFIED, no incomplete bar in
          a point-in-time read, no malformed response EMPTY, no delivered rows above the cap, no stale value labelled current.
  S01     isolation: no network / broker / model / execution import in ANY market_intel module (a closed registry: a new module must be
          declared); nothing outside market_intel imports them.
  F01-F03 scope: exactly the declared files; frozen hashes; every bookkeeping edit pinned exactly.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import shutil
from types import SimpleNamespace
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import freeze_ledger as FL
from market_intel import approvals as APP
from market_intel import freshness as FR
from market_intel import calendar as CAL
from market_intel import calendar_history as HIST
from market_intel import ingest as IG
from market_intel import migrations as MG
from market_intel.archive import RawArchive
from market_intel.models import FetchRecord, Observation, forbidden_key
from market_intel.sources import alpaca_bars as AB
from market_intel import store as ST
from market_intel.store import (ChronologyError, CoverageError, DatasetSpec, FeedRequired, MarketIntelStore, PayloadError, RowCapError, ScopeError,
                                SealedError, StoreError, content_hash, natural_key, parse_ts, ts)

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
SAT = datetime(2025, 12, 6, 15, 0, tzinfo=UTC)                 # a Saturday: the backfill window is open


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=1.0):
        self.t += timedelta(seconds=s)


def ny(y, m, d, hh, mm):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def daily(sym, d: date, close, *, feed="iex", retrieved=SAT, backfilled=True, adjustment="raw", status="AVAILABLE", flags=()):
    t = ny(d.year, d.month, d.day, 0, 0)
    payload = {} if status != "AVAILABLE" else {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000}
    return Observation(kind="BAR_1DAY", symbol=sym, provider="ALPACA", feed=feed, adjustment=adjustment, observed_at=t, retrieved_at=retrieved,
                       available_at=retrieved, session="REG", session_date=d, status=status, freshness=None, backfilled=backfilled,
                       quality_flags=tuple(flags), payload=payload)


def sessions_2025(start: date, n: int):
    out, x = [], start
    while len(out) < n:
        if CAL.market_day_status(x) == CAL.MARKET_DAY:
            out.append(x)
        x += timedelta(days=1)
    return out


def new_store(tmp_path, clock=None, name="mi.db"):
    return MarketIntelStore(tmp_path / name, clock=clock or Clock(SAT), approvals_root=tmp_path / "approvals")


SYMS = ("SPY", "QQQ", "IWM", "AAPL")
TF = {"BAR_1DAY": "1Day", "BAR_1HOUR": "1Hour", "BAR_1MIN": "1Min"}


def spec(job="IMPORT", feed="iex", kind="BAR_1DAY", symbols=SYMS, start=date(2025, 1, 1), end=date(2025, 12, 31)):
    """An IMPORT (backfilled rows, no approval needed) or FIXTURE dataset; a BACKFILL dataset needs an approval (approve())."""
    return DatasetSpec(job_kind=job, kind=kind, provider="ALPACA", feed=feed, adjustment="raw", symbols=list(symbols), timeframe=TF.get(kind),
                       start_date=start, end_date=end)


def req(symbols=("SPY",), start=date(2025, 12, 1), end=date(2025, 12, 5), feed="iex", timeframe="1Day"):
    return IG.BackfillRequest(symbols=tuple(symbols), start=start, end=end, timeframe=timeframe, feed=feed)


def approve(tmp_path, rq, *, now=SAT, aid="OPS-TEST-0001", sip=False, max_requests=10_000, max_rows=10_000_000, hours=6, **over):
    """What an operator does: write an approval file for the exact request and its plan hash into the approvals directory."""
    root = tmp_path / "approvals"
    root.mkdir(parents=True, exist_ok=True)
    doc = {"version": APP.APPROVAL_VERSION, "approval_id": aid, "plan_sha256": IG.plan(rq, now)["plan_sha256"], "request": rq.as_dict(),
           "sip_approved": sip, "max_requests": max_requests, "max_rows": max_rows, "granted_by": "test operator",
           "granted_at": (now - timedelta(minutes=5)).isoformat(), "expires_at": (now + timedelta(hours=hours)).isoformat()}
    doc.update(over)
    path = root / f"{aid}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return APP.load(path, now=now, root=root), root


def run(tmp_path, rq, clk, store, *, wire=None, archive=None, budget=None, aid="OPS-TEST-0001", sleep=None):
    a, root = approve(tmp_path, rq, now=clk(), aid=aid)
    return IG.run_backfill(rq, approval=a, fetch_fn=wire or Wire(clk), store=store, archive=archive or RawArchive(tmp_path / "raw"),
                           clock=clk, sleep_fn=sleep or (lambda x: clk.tick(x)), budget=budget)


# ============================================================================================================ the database (D)
def test_D01_the_store_is_created_lazily_and_reads_never_create_it(tmp_path):
    import importlib
    for m in ("market_intel.store", "market_intel.migrations", "market_intel.ingest", "market_intel.archive", "market_intel.sources.alpaca_bars"):
        importlib.import_module(m)
    assert not (ROOT / "data" / "market_intel.db").exists() and not (ROOT / "runtime" / "market_intel").exists()   # imports create nothing
    from market_intel.store import default_path
    from market_intel.archive import default_root
    assert default_path() == Path(config.PROJECT_ROOT) / "data" / "market_intel.db" and default_root() == Path(config.PROJECT_ROOT) / "runtime" / "market_intel" / "raw"
    for rel in ("data/market_intel.db", "data/market_intel.db-wal", "data/market_intel.db-shm", "runtime/market_intel/raw/x.json.gz"):
        assert subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT).returncode == 0, rel                  # never committed
    s = new_store(tmp_path)
    at = SAT
    reads = [s.history("BAR_1DAY", symbol="SPY", feed="iex"), s.latest_usable("BAR_1DAY", at=at, symbol="SPY", feed="iex"),
             s.snapshot("BAR_1DAY", ["SPY"], at=at, feed="iex")["SPY"]["status"], s.provenance("x"), s.dataset("x"), s.revisions("k"),
             s.status_counts(), s.budget_used("ALPACA", date(2025, 12, 6)), s.schema_version(),
             s.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=date(2025, 12, 1), end=date(2025, 12, 5))["result"]]
    assert reads == [[], None, "UNAVAILABLE", None, None, [], {"store": "NOT_CREATED"}, 0, 0, "BLOCKED"] and not s.exists()   # nothing known: blocked
    s.create_dataset(spec())                                                                                     # the first explicit write
    assert s.exists() and s.schema_version() == MG.SCHEMA_VERSION == 1
    c = sqlite3.connect(s.path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(MG.APPEND_ONLY_TABLES) <= tables
    c.close()


def test_D02_every_table_is_append_only_and_no_column_names_an_instruction(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 100.0), daily("SPY", date(2025, 12, 2), 0.0, status="UNAVAILABLE")])   # a value + an absence
    a, _ = approve(tmp_path, req())
    s.create_dataset(IG.dataset_spec(IG.plan(req(), SAT), a), approval=a)                         # an approval use (the approved plan's spec)
    s.record_budget_event("ALPACA", "ALLOW")
    s.record_quality_audit({"scope": {"x": 1}, "check": "c", "result": "PASS", "codes": []})
    s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=did)
    c = sqlite3.connect(s.path)
    for t in MG.APPEND_ONLY_TABLES:
        col = [r[1] for r in c.execute(f"PRAGMA table_info({t})")][1]
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"UPDATE {t} SET {col} = {col}")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"DELETE FROM {t}")
        assert not [r[1] for r in c.execute(f"PRAGMA table_info({t})") if forbidden_key(r[1])], t
    c.close()


def test_D03_versioned_migrations_with_a_pinned_ddl_hash_refuse_newer_or_altered_schemas(tmp_path):
    assert MG.ddl_sha256(MG.MIGRATIONS[0][2]) == MG.ddl_sha256(MG.V1) == PINNED_V1_DDL            # a schema edit is a new migration, never a quiet change
    s = new_store(tmp_path)
    s.create_dataset(spec())
    s.create_dataset(spec())                                                                     # re-open: no second migration row
    c = sqlite3.connect(s.path)
    assert c.execute("SELECT version, ddl_sha256 FROM mi_schema_migrations").fetchall() == [(1, PINNED_V1_DDL)]
    c.execute("PRAGMA user_version = 2")
    c.commit()
    c.close()
    with pytest.raises(MG.SchemaTooNew):
        s.history("BAR_1DAY", symbol="SPY", feed="iex")
    with pytest.raises(MG.SchemaTooNew):
        s.create_dataset(spec())
    s2 = new_store(tmp_path, name="m2.db")
    s2.create_dataset(spec())
    c = sqlite3.connect(s2.path)
    c.execute("DROP TRIGGER mi_schema_migrations_no_update")
    c.execute("UPDATE mi_schema_migrations SET ddl_sha256 = 'x'")
    c.commit()
    c.close()
    with pytest.raises(MG.SchemaMismatch):
        s2.create_dataset(spec())


# ============================================================================================================ ingestion + point in time (I)
def test_I01_reingesting_identical_content_is_a_duplicate_and_the_dataset_hash_is_stable(tmp_path):
    days = sessions_2025(date(2025, 12, 1), 5)
    hashes = []
    for name in ("a.db", "b.db"):
        clk = Clock(SAT)
        s = new_store(tmp_path, clk, name)
        did = s.create_dataset(spec())
        first = s.ingest(did, [daily("SPY", d, 100.0 + i) for i, d in enumerate(days)])
        clk.tick(3600)
        again = s.ingest(did, [daily("SPY", d, 100.0 + i, retrieved=SAT + timedelta(hours=1)) for i, d in enumerate(days)])   # fetched again later
        assert (first.inserted, first.duplicates, again.inserted, again.duplicates, again.revised) == (5, 0, 0, 5, 0)
        hashes.append(s.finish_dataset(did, "COMPLETED")["content_sha256"])
        assert s.dataset(did)["events"][-1]["rows_duplicate"] == 5
        with pytest.raises(StoreError):
            s.ingest(did, [daily("SPY", days[0], 1.0)])                                         # a sealed dataset takes no rows
    assert hashes[0] == hashes[1]                                                                # same content, same dataset hash, run to run
    o = daily("SPY", days[0], 100.0)
    assert natural_key(o) == natural_key(daily("SPY", days[0], 999.0)) and content_hash(o) != content_hash(daily("SPY", days[0], 999.0))
    assert content_hash(o) == content_hash(daily("SPY", days[0], 100.0, retrieved=SAT + timedelta(days=3)))     # retrieval time is not content


def test_I02_corrections_are_new_revisions_and_every_version_is_kept(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    d = date(2025, 12, 1)
    v1 = s.ingest(s.create_dataset(spec(job="FIXTURE")), [daily("SPY", d, 100.0)]).obs_ids[0]
    t1 = clk()
    clk.tick(86400)
    corr = s.ingest(s.create_dataset(spec(job="FIXTURE")), [daily("SPY", d, 101.0, retrieved=clk())])   # a later dataset: the provider corrected it
    t2 = clk()
    clk.tick(86400)
    back = s.ingest(s.create_dataset(spec(job="FIXTURE")), [daily("SPY", d, 100.0, retrieved=clk())])   # ... and reverted it: A -> B -> A
    assert (corr.revised, back.revised, back.duplicates) == (1, 1, 0)
    revs = s.revisions(natural_key(daily("SPY", d, 100.0)))
    assert [(r["revision"], r["payload"]["close"], r["supersedes_obs_id"] is None) for r in revs] == [(1, 100.0, True), (2, 101.0, False), (3, 100.0, False)]
    assert revs[1]["supersedes_obs_id"] == v1 and revs[2]["supersedes_obs_id"] == revs[1]["obs_id"]
    at = lambda t: s.history("BAR_1DAY", symbol="SPY", feed="iex", as_of=t)[0]["payload"]["close"]     # noqa: E731
    assert (at(t1), at(t2), at(clk())) == (100.0, 101.0, 100.0)                                 # each time sees what was known then
    prov = s.provenance(revs[1]["obs_id"])
    assert [r["revision"] for r in prov["revisions"]] == [1, 2, 3] and prov["dataset"]["job_kind"] == "FIXTURE"


def test_I03_point_in_time_reads_exclude_what_was_not_yet_available_or_not_yet_ingested(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    d = date(2025, 12, 1)
    did = s.create_dataset(spec())
    with pytest.raises(StoreError):
        s.ingest(did, [daily("SPY", d, 100.0, backfilled=False)])                                # a backfill can never pose as live data
    s.ingest(did, [daily("SPY", d, 100.0)])                                                      # backfilled on SAT (available_at == retrieved_at)
    market_time = ny(2025, 12, 1, 16, 0)
    assert s.latest_usable("BAR_1DAY", at=market_time, symbol="SPY", feed="iex") is None        # a backfill never claims to be known in the past
    assert s.latest_usable("BAR_1DAY", at=SAT, symbol="SPY", feed="iex")["payload"]["close"] == 100.0
    clk.tick(7 * 86400)                                                                          # ingested a week later ...
    fx = s.create_dataset(spec(job="FIXTURE"))
    late = Observation(kind="BAR_1DAY", symbol="QQQ", provider="ALPACA", feed="iex", adjustment="raw", observed_at=ny(2025, 12, 2, 0, 0),
                       retrieved_at=ny(2025, 12, 2, 20, 5), available_at=ny(2025, 12, 2, 20, 5), session="REG", session_date=date(2025, 12, 2),
                       status="AVAILABLE", payload={"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10})   # ... claiming an earlier availability
    s.ingest(fx, [late])
    assert s.latest_usable("BAR_1DAY", at=ny(2025, 12, 3, 9, 0), symbol="QQQ", feed="iex") is None   # not in the store yet at that time
    assert s.latest_usable("BAR_1DAY", at=clk(), symbol="QQQ", feed="iex") is not None
    snap = s.snapshot("BAR_1DAY", ["spy", "QQQ", "IWM"], at=SAT, feed="iex")
    assert (snap["SPY"]["status"], snap["QQQ"]["status"], snap["IWM"]["status"]) == ("STALE", "UNAVAILABLE", "UNAVAILABLE")
    assert snap["SPY"]["latest_usable"]["payload"]["close"] == 100.0 and snap["SPY"]["usable_is_latest"] is True and snap["SPY"]["reason"] == "SESSION_MISSING"
    assert snap["IWM"]["latest_usable"] is None and snap["IWM"]["reason"] == "NO_USABLE_VALUE_KNOWN_AT"          # missing is never a zero


def test_I04_every_time_is_utc_aware_and_stored_in_one_sortable_format(tmp_path):
    with pytest.raises(StoreError):
        ts(datetime(2025, 1, 1, 12, 0))
    est = datetime(2025, 12, 1, 11, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert ts(est) == "2025-12-01T16:00:00.000000Z" and parse_ts(ts(est)) == est
    with pytest.raises(StoreError):
        MarketIntelStore(tmp_path / "z.db", clock=lambda: datetime(2025, 1, 1)).create_dataset(spec())
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 100.0, retrieved=datetime(2025, 12, 6, 10, 0, tzinfo=timezone(timedelta(hours=-5))))])
    r = s.history("BAR_1DAY", symbol="SPY", feed="iex")[0]
    assert r["retrieved_at"] == "2025-12-06T15:00:00.000000Z" and r["observed_at"] == "2025-12-01T05:00:00.000000Z"
    c = sqlite3.connect(s.path)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO mi_budget_events (at, provider, event) VALUES ('2025-12-06 15:00:00', 'X', 'ALLOW')")   # the CHECK refuses other formats
    c.close()


def test_I05_missing_and_malformed_data_never_becomes_a_value(tmp_path):
    with pytest.raises(ValueError):
        Observation(**{**daily("SPY", date(2025, 12, 1), 100.0).model_dump(), "status": "UNAVAILABLE"})          # an UNAVAILABLE row with a payload
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 0.0, status="UNAVAILABLE")])
    assert s.latest_usable("BAR_1DAY", at=SAT, symbol="SPY", feed="iex") is None and s.history("BAR_1DAY", symbol="SPY", feed="iex") == []
    st = s.latest_status("BAR_1DAY", at=SAT, symbol="SPY", feed="iex")
    assert (st["latest_record"]["record_type"], st["latest_record"]["status"], st["latest_record"]["payload"], st["status"]) == ("ABSENCE", "UNAVAILABLE", {}, "UNAVAILABLE")
    body = json.dumps({"symbol": "SPY", "bars": [{"t": "2025-12-01T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "v": 9}, {"t": "not-a-time", "o": 1, "h": 2,
                       "l": 0.5, "c": 1, "v": 9}, {"t": "2025-12-02T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}], "next_page_token": None}).encode()
    obs, token, problems = AB.parse_page(body, symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT)
    assert [o.session_date for o in obs] == [date(2025, 12, 2)] and [p["problem"] for p in problems] == ["MALFORMED_ROW", "BAD_TIME"] and token is None
    assert AB.parse_page(b"<html>", symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT)[2] == [{"problem": "INVALID_JSON"}]
    assert AB.parse_page(json.dumps({"symbol": "QQQ", "bars": [], "next_page_token": None}).encode(), symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw",
                         retrieved_at=SAT)[2][0]["problem"] == "SYMBOL_MISMATCH"
    nan = json.dumps({"symbol": "SPY", "bars": [{"t": "2025-12-03T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": float("nan"), "v": 9}], "next_page_token": None}).encode()
    assert AB.parse_page(nan, symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT)[2][0]["problem"] == "REFUSED_BY_ENVELOPE"
    s2 = new_store(tmp_path, name="seal.db")
    d2 = s2.create_dataset(spec())
    s2.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="HTTP_ERROR", http_status=500),
                    dataset_id=d2)
    for final in ("EMPTY", "COMPLETED"):
        with pytest.raises(StoreError):
            s2.finish_dataset(d2, final)                                                         # a failed fetch is never "no data"
    assert s2.finish_dataset(d2, "FAILED")["status"] == "FAILED"


def test_I06_iex_and_sip_never_mix(tmp_path):
    s = new_store(tmp_path)
    iex, sip = s.create_dataset(spec(feed="iex")), s.create_dataset(spec(feed="sip"))
    d = date(2025, 12, 1)
    with pytest.raises(FeedRequired):
        s.ingest(iex, [daily("SPY", d, 100.0, feed="sip")])
    with pytest.raises(FeedRequired):
        s.ingest(iex, [daily("SPY", d, 100.0, feed="none")])
    with pytest.raises(FeedRequired):
        s.create_dataset(spec(feed="none"))
    s.ingest(iex, [daily("SPY", d, 100.0)])
    s.ingest(sip, [daily("SPY", d, 100.07, feed="sip")])
    assert natural_key(daily("SPY", d, 1.0)) != natural_key(daily("SPY", d, 1.0, feed="sip"))
    assert [r["payload"]["close"] for r in s.history("BAR_1DAY", symbol="SPY", feed="iex")] == [100.0]
    assert [r["payload"]["close"] for r in s.history("BAR_1DAY", symbol="SPY", feed="sip")] == [100.07]
    for call in (lambda: s.history("BAR_1DAY", symbol="SPY"), lambda: s.latest_usable("BAR_1DAY", at=SAT, symbol="SPY"),
                 lambda: s.snapshot("BAR_1DAY", ["SPY"], at=SAT)):
        with pytest.raises(FeedRequired):
            call()


def test_I07_unknown_calendar_coverage_fails_closed_and_non_sessions_are_flagged(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    with pytest.raises(CoverageError):
        s.ingest(did, [daily("SPY", date(2024, 12, 31), 100.0)])                                  # 2024: not in any verified calendar
    with pytest.raises(CoverageError):
        s.ingest(did, [daily("SPY", date(2025, 12, 1), 100.0).model_copy(update={"session_date": None})])
    s.ingest(did, [daily("SPY", date(2025, 7, 4), 100.0)])                                        # Independence Day: stored, flagged, unusable
    r = s.history("BAR_1DAY", symbol="SPY", feed="iex")[0]
    assert r["quality_flags"] == ["NON_SESSION_DAY"] and r["usable"] is False
    days = sessions_2025(date(2025, 11, 10), 10)                                                    # sessions before the retrieval (B5)
    s.ingest(did, [daily("SPY", x, 100.0) for i, x in enumerate(days) if i not in (3,)])            # one session missing
    q = s.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=days[0], end=days[-1])
    assert (q["result"], q["missing_sessions"], q["expected_sessions"], q["present_sessions"]) == ("WARN", [days[3].isoformat()], 10, 9)
    s.ingest(did, [daily("QQQ", x, 50.0) for i, x in enumerate(days) if i not in (3, 4)])          # two in a row: a session hole
    q2 = s.quality_report("BAR_1DAY", symbol="QQQ", feed="iex", start=days[0], end=days[-1])
    assert q2["result"] == "BLOCKED" and "SESSION_HOLE" in q2["codes"] and q2["holes"][0]["market_days"] == 2
    q3 = s.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=date(2024, 12, 30), end=date(2025, 1, 3))
    assert q3["result"] == "BLOCKED" and q3["uncovered_weekdays"] == ["2024-12-30", "2024-12-31"] and "CALENDAR_NOT_COVERED" in q3["codes"]
    s.record_quality_audit(q2)
    assert s.status_counts()["tables"]["mi_quality_audits"] == 1


def test_I08_a_batch_is_all_or_nothing_and_a_killed_writer_leaves_no_partial_rows(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 100.0)])
    good = [daily("SPY", x, 1.0) for x in sessions_2025(date(2025, 12, 2), 3)]
    with pytest.raises(CoverageError):
        s.ingest(did, good + [daily("SPY", date(2024, 6, 3), 1.0)])                               # the last row is refused ...
    assert len(s.history("BAR_1DAY", symbol="SPY", feed="iex")) == 1                              # ... so none of the batch was stored
    script = tmp_path / "killer.py"
    script.write_text(f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(ROOT / 'tests')!r})
from datetime import date
from market_intel.store import MarketIntelStore
from test_market_intel_store_71 import daily, sessions_2025, Clock, SAT
s = MarketIntelStore({str(s.path)!r}, clock=Clock(SAT))
real, seen = s._annotate, []
def dying(o):
    seen.append(o)
    if len(seen) == 4:
        os._exit(9)                                                       # the process dies INSIDE the write transaction (3 rows inserted)
    return real(o)
s._annotate = dying
s.ingest({did!r}, [daily("SPY", d, 2.0) for d in sessions_2025(date(2025, 12, 2), 5)])
""", encoding="utf-8")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "STOCK_AGENT_ENV": "test"}
    r = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 9, r.stderr[-800:]
    c = sqlite3.connect(s.path)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    assert [x["payload"]["close"] for x in s.history("BAR_1DAY", symbol="SPY", feed="iex")] == [100.0]   # recovered: only committed rows
    assert s.ingest(did, good).inserted == 3                                                        # and the store keeps working


def test_I09_readers_never_see_an_open_batch_and_writers_are_serialized(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    real, seen = s._annotate, []

    def slow(o):                                                                                   # pauses INSIDE the write transaction
        seen.append(o)
        if len(seen) == 3:
            entered.set()
            assert release.wait(20)
        return real(o)

    s._annotate = slow
    rows = [daily("SPY", d, 100.0 + i) for i, d in enumerate(sessions_2025(date(2025, 12, 1), 4))]
    th = threading.Thread(target=lambda: (s.ingest(did, rows), done.set()))
    th.start()
    assert entered.wait(20)
    assert s.history("BAR_1DAY", symbol="SPY", feed="iex") == []                                 # a reader sees only committed data (WAL)
    blocked = MarketIntelStore(s.path, clock=Clock(SAT), busy_timeout_ms=200)
    t0 = time.time()
    with pytest.raises(sqlite3.OperationalError):
        blocked.record_budget_event("ALPACA", "ALLOW")                                           # a second writer waits, then times out
    assert time.time() - t0 >= 0.15
    release.set()
    th.join(20)
    assert done.is_set() and len(s.history("BAR_1DAY", symbol="SPY", feed="iex")) == 4
    blocked.record_budget_event("ALPACA", "ALLOW")                                               # once the first commits, it writes
    assert s.budget_used("ALPACA", date(2025, 12, 6)) == 1


def test_I10_provenance_names_the_fetch_the_raw_archive_and_the_dataset(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    arc = RawArchive(tmp_path / "raw")
    res = run(tmp_path, req(), clk, s, archive=arc, aid="OPS-PROV-01")
    row = s.history("BAR_1DAY", symbol="SPY", feed="iex")[0]
    prov = s.provenance(row["obs_id"])
    assert prov["fetch"]["status"] == "OK" and prov["fetch"]["raw_path"].endswith(".json.gz") and prov["dataset"]["approval_ref"] == "OPS-PROV-01"
    assert prov["dataset"]["plan_sha256"] == res["plan_sha256"] and [e["status"] for e in prov["dataset_events"]] == ["RUNNING", "COMPLETED"]
    assert s.dataset(res["dataset_id"])["approval_use"]["approval_id"] == "OPS-PROV-01"
    seal = s.dataset(res["dataset_id"])["events"][-1]
    assert arc.anchors() == {res["dataset_id"]: {k: seal[k] for k in ("content_sha256", "records_sha256", "metadata_sha256", "status")}}
    assert json.loads(arc.read(prov["fetch"]["raw_path"]))["symbol"] == "SPY" and arc.verify() == {"entries": 1, "ok": True, "problems": []}
    assert "apikey" not in prov["fetch"]["params_json"].lower() and json.loads(prov["fetch"]["params_json"])["feed"] == "iex"
    (tmp_path / "raw" / prov["fetch"]["raw_path"]).write_bytes(b"tampered")
    assert arc.verify()["ok"] is False and res["status"] == "COMPLETED"


# ============================================================================================================ the gated runner (R)
class Wire:
    """A fake Alpaca bars transport: pages of `per_page` bars per symbol over the requested dates; scripted failures by call index."""

    def __init__(self, clock, *, per_page=1000, script=None, symbol_status=None, empty=False):
        self.clock, self.per_page, self.script, self.symbol_status, self.empty = clock, per_page, dict(script or {}), dict(symbol_status or {}), empty
        self.calls = []

    def __call__(self, params):
        self.calls.append(params)
        self.clock.tick(1)
        i = len(self.calls) - 1
        if i in self.script:
            return self.script[i]
        if params["symbol"] in self.symbol_status:
            return self.symbol_status[params["symbol"]], {}, b"error"
        start = datetime.fromisoformat(params["start"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=-5))).date()
        end = datetime.fromisoformat(params["end"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=-5))).date()
        days, x = [], start
        while x < end:
            if CAL.market_day_status(x) == CAL.MARKET_DAY:
                days.append(x)
            x += timedelta(days=1)
        off = int(params.get("page_token") or 0)
        page = [] if self.empty else days[off:off + self.per_page]
        bars = [{"t": f"{d.isoformat()}T05:00:00Z", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100, "n": 5, "vw": 10.2} for d in page]
        nxt = str(off + self.per_page) if (not self.empty and off + self.per_page < len(days)) else None
        return 200, {}, json.dumps({"symbol": params["symbol"], "bars": bars, "next_page_token": nxt}).encode()


def test_R01_a_real_run_needs_the_request_an_approval_file_for_its_recomputed_plan_sip_approval_and_the_window(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    w = Wire(clk)
    rq = req()
    good, root = approve(tmp_path, rq)
    go = lambda r_, appr, c=clk: IG.run_backfill(r_, approval=appr, fetch_fn=w, store=s, archive=arc, clock=c,   # noqa: E731
                                                 sleep_fn=lambda x: None)
    for bad in (None, "OK", {"approval_id": "OPS-TEST-0001", "plan_sha256": good.plan_sha256}):
        with pytest.raises(PermissionError):
            go(rq, bad)                                                                                  # only an approval loaded from a file
    with pytest.raises(PermissionError):
        go(IG.plan(rq, SAT), good)                                                                       # a plan dict is never accepted: recomputed
    refused = req(start=date(2024, 12, 30), end=date(2025, 1, 3))
    with pytest.raises(PermissionError):
        go(refused, approve(tmp_path, refused, aid="OPS-REFUSED-1")[0])                                  # a refused plan, even if "approved"
    sip = req(feed="sip")
    with pytest.raises(PermissionError, match="SIP"):
        go(sip, approve(tmp_path, sip, aid="OPS-SIP-0001")[0])                                           # SIP needs its own approval
    weekday = Clock(ny(2025, 12, 8, 10, 0))                                                                # Monday 10:00 New York
    with pytest.raises(PermissionError, match="window"):
        go(rq, approve(tmp_path, rq, now=weekday(), aid="OPS-WKDY-0001")[0], weekday)
    assert w.calls == [] and not s.exists() and not arc.root.exists()                                    # nothing fetched, nothing written
    src = (ROOT / "market_intel" / "ingest.py").read_text(encoding="utf-8")
    assert "APP.check(approval, fresh" in src and "APP.load(" not in src and "BackfillApproval" not in src  # the CLI never builds an approval
    tree = ast.parse((ROOT / "market_intel" / "approvals.py").read_text(encoding="utf-8"))
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "") for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"write_text", "write_bytes", "open", "mkdir", "dump", "touch", "rename", "unlink"}   # no code writes an approval


def test_R02_paging_archive_retries_and_honest_final_states(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    rq = req(symbols=("SPY", "QQQ"), start=date(2025, 11, 3), end=date(2025, 11, 28))
    w = Wire(clk, per_page=7, script={1: (429, {"retry-after": "5"}, b"slow down")})
    sleeps = []
    res = run(tmp_path, rq, clk, s, wire=w, archive=arc, sleep=lambda x: (sleeps.append(x), clk.tick(x)))
    n = IG.plan(rq, SAT)["coverage"]["sessions"]
    assert n == 19 and res["status"] == "COMPLETED" and res["inserted"] == 2 * n and res["failed_fetches"] == 1 and res["unrecovered"] == 0   # a retried 429 is recovered
    assert [c.get("page_token") for c in w.calls if c["symbol"] == "SPY"] == [None, "7", "7", "14"] and sleeps and sleeps[0] == pytest.approx(5.0, abs=1.1)
    assert arc.verify()["ok"] and arc.verify()["entries"] == len(w.calls)
    for sym in ("SPY", "QQQ"):
        assert len(s.history("BAR_1DAY", symbol=sym, feed="iex")) == n
    ok = Clock(SAT)
    s2 = new_store(tmp_path, ok, "ok.db")
    r2 = run(tmp_path, rq, ok, s2, wire=Wire(ok, per_page=7), archive=RawArchive(tmp_path / "raw2"))
    assert r2["status"] == "COMPLETED"
    ok3 = Clock(SAT)
    s3 = new_store(tmp_path, ok3, "ok3.db")
    r3 = run(tmp_path, rq, ok3, s3, wire=Wire(ok3, per_page=7), archive=RawArchive(tmp_path / "raw3"))
    assert r3["content_sha256"] == r2["content_sha256"]                                              # the same data, the same dataset hash
    stuck = {i: (429, {"retry-after": "5"}, b"slow") for i in range(1, 4)}                          # page 2 of SPY never recovers
    cases = {"rate_limited": (Wire(Clock(SAT), per_page=7, script=stuck), "PARTIAL", "BUDGET_BLOCKED_BACKOFF"),   # still backing off: stop
             "denied": (Wire(Clock(SAT), symbol_status={"SPY": 403}), "FAILED", "ACCESS_DENIED"),
             "one_symbol_down": (Wire(Clock(SAT), symbol_status={"QQQ": 500}), "PARTIAL", None),
             "empty": (Wire(Clock(SAT), empty=True), "EMPTY", None)}
    for name, (wire, want, stop) in cases.items():
        st = new_store(tmp_path, wire.clock, f"{name}.db")
        out = run(tmp_path, rq, wire.clock, st, wire=wire, archive=RawArchive(tmp_path / f"raw_{name}"))
        assert (out["status"], out["stop"]) == (want, stop), name
    closes = Clock(ny(2025, 12, 8, 6, 59) + timedelta(seconds=57))                                  # Monday 06:59:57: the window closes at 07:00
    s4 = new_store(tmp_path, closes, "closes.db")
    out4 = run(tmp_path, rq, closes, s4, wire=Wire(closes, per_page=7), archive=RawArchive(tmp_path / "raw4"))
    assert (out4["status"], out4["stop"]) == ("PARTIAL", "WINDOW_CLOSED")


def test_R03_the_budget_paces_requests_and_its_history_is_persisted(tmp_path):
    from market_intel.budget import ProviderBudget
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    rq = req(symbols=("SPY", "QQQ", "IWM"), start=date(2025, 11, 3), end=date(2025, 11, 28))
    slow = ProviderBudget("ALPACA_BACKFILL", 2, clk)                                                 # 2 requests a minute (never more than the plan's)
    sleeps = []
    res = run(tmp_path, rq, clk, s, budget=slow, sleep=lambda x: (sleeps.append(x), clk.tick(x)))
    assert res["status"] == "COMPLETED" and len(sleeps) == 1 and sleeps[0] == pytest.approx(28.0, abs=0.5)   # the third request waited for its token
    assert s.budget_used("ALPACA_BACKFILL", date(2025, 12, 6)) == 3
    c = sqlite3.connect(s.path)
    assert [r[0] for r in c.execute("SELECT event FROM mi_budget_events ORDER BY event_id")].count("REFUSE") == 1
    c.close()


# ============================================================================================================ the CLI (C)
@pytest.fixture
def no_network(monkeypatch):
    import socket
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        raise AssertionError("network access in an offline command")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return calls


def cli(args, tmp_path, monkeypatch):
    import io as _io
    monkeypatch.chdir(tmp_path)
    buf = _io.StringIO()
    code = IG.main(args, out=buf)
    text = buf.getvalue()
    return code, (json.loads(text) if text.strip().startswith("{") else text)


BASE_ARGS = ["--symbols", "SPY,AAPL", "--start", "2025-01-02", "--end", "2025-12-31", "--timeframe", "1Day", "--feed", "iex", "--now", "2026-10-31T20:00:00Z"]


def test_C01_every_cli_command_is_offline_and_writes_nothing(tmp_path, monkeypatch, no_network):
    before = sorted(p.name for p in tmp_path.iterdir())
    code, doc = cli(["plan", *BASE_ARGS], tmp_path, monkeypatch)
    assert code == 0 and doc["valid"] and (doc["network"], doc["writes"], doc["paid"]) == (False, False, False)
    assert doc["requires_approval"] == ["REAL_BACKFILL"] and doc["coverage"]["calendars"] == ["nyse_history_2025_v1"]
    for args in (["dry-run", *BASE_ARGS], ["execute", *BASE_ARGS], ["calendar", "--start", "2025-01-01", "--end", "2025-12-31"],
                 ["inspect", "--db", str(tmp_path / "nope.db")]):
        cli(args, tmp_path, monkeypatch)
    assert no_network == [] and sorted(p.name for p in tmp_path.iterdir()) == before                 # no socket, no file
    assert not (ROOT / "data" / "market_intel.db").exists()


def test_C02_the_planner_refuses_uncovered_dates_recent_sip_windows_missing_feeds_and_bad_input(tmp_path, monkeypatch, no_network):
    def codes(extra, **over):
        args = dict(zip(BASE_ARGS[::2], BASE_ARGS[1::2]))
        args.update(over)
        flat = [x for kv in args.items() for x in kv]
        code, doc = cli(["plan", *flat, *extra], tmp_path, monkeypatch)
        return code, [r["code"] for r in doc["refusals"]] if isinstance(doc, dict) and "refusals" in doc else doc
    assert codes([], **{"--start": "2024-06-03", "--end": "2025-01-31"}) == (2, ["CALENDAR_NOT_COVERED"])
    assert codes([], **{"--start": "2027-01-04", "--end": "2028-01-31", "--now": "2028-03-01T20:00:00Z"})[1] == ["CALENDAR_NOT_COVERED"]
    assert codes([], **{"--end": "2026-10-31"}) == (2, ["END_NOT_COMPLETED"])
    assert codes([], **{"--feed": "sip", "--start": "2025-12-01", "--end": "2025-12-05", "--now": "2025-12-06T05:10:00Z"}) == (2, ["SIP_WITHIN_15_MIN"])
    ok_sip = cli(["plan", *BASE_ARGS[:-6], "--feed", "sip", "--timeframe", "1Day", "--now", "2026-10-31T20:00:00Z"], tmp_path, monkeypatch)[1]
    assert ok_sip["valid"] and ok_sip["requires_approval"] == ["REAL_BACKFILL", "SIP_FEED"]
    assert codes([], **{"--feed": "none"}) == (2, ["FEED_REQUIRED"])
    assert codes([], **{"--symbols": "SPY,BAD SYMBOL"})[1] == ["INVALID_SYMBOL"]
    assert codes([], **{"--start": "2025-12-31", "--end": "2025-01-02"})[1] == ["START_AFTER_END"]
    assert codes([], **{"--symbols": ",".join(f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(101))})[1] == ["TOO_MANY_SYMBOLS"]
    assert cli(["plan", "--symbols", "SPY", "--start", "2025-01-02", "--end", "2025-12-31", "--timeframe", "1Day"], tmp_path, monkeypatch)[0] == 1   # no feed
    assert cli(["plan", *BASE_ARGS[:-2], "--now", "2026-10-31T20:00:00"], tmp_path, monkeypatch)[0] == 1   # a naive --now: a usage error


def test_C03_dry_run_previews_requests_execute_is_always_refused_and_inspect_never_creates(tmp_path, monkeypatch, no_network):
    code, doc = cli(["dry-run", *BASE_ARGS], tmp_path, monkeypatch)
    assert code == 0 and [x["params"]["symbol"] for x in doc["preview"]] == ["SPY", "AAPL"] and doc["would_write"]["rows"] == 500
    for x in doc["preview"]:
        assert set(x["params"]) == {"symbol", "timeframe", "start", "end", "feed", "adjustment", "limit", "sort"}   # never a key or a secret
    code, doc = cli(["execute", *BASE_ARGS], tmp_path, monkeypatch)
    assert (code, doc["executed"], doc["reason"]) == (3, False, "EXECUTE_NOT_ENABLED")
    code, doc = cli(["inspect", "--db", str(tmp_path / "missing.db")], tmp_path, monkeypatch)
    assert (code, doc["store"], doc["created"]) == (2, "NOT_FOUND", False) and not (tmp_path / "missing.db").exists()
    s = new_store(tmp_path, name="real.db")
    s.ingest(s.create_dataset(spec()), [daily("SPY", date(2025, 12, 1), 1.0)])
    code, doc = cli(["inspect", "--db", str(tmp_path / "real.db")], tmp_path, monkeypatch)
    assert code == 0 and doc["tables"]["mi_observations"] == 1 and doc["schema_version"] == 1
    code, doc = cli(["calendar", "--start", "2024-12-30", "--end", "2025-01-10"], tmp_path, monkeypatch)
    assert doc["status"] == "NOT_COVERED" and doc["uncovered_first"] == ["2024-12-30", "2024-12-31"] and doc["sessions"] == 6


def test_C04_estimates_follow_the_verified_calendar():
    now = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    est = lambda tf, ext=False: IG.plan(IG.BackfillRequest(("SPY",), date(2025, 1, 1), date(2025, 12, 31), tf, "iex", extended_hours=ext), now)["estimate"]  # noqa: E731
    assert est("1Day")["rows_per_symbol"] == 250 == HIST.SESSIONS_PER_YEAR[2025]
    assert est("1Min")["rows_per_symbol"] == 247 * 390 + 3 * 210 and est("1Hour")["rows_per_symbol"] == 247 * 7 + 3 * 4
    ext = est("1Min", True)
    assert (ext["rows_per_symbol"], ext["pages_per_symbol"]) == (247 * 960 + 3 * 780, 24)          # the design's "about 24 pages per symbol-year"
    assert est("1Min")["rows_delivered_max_per_symbol"] == ext["rows_per_symbol"] and est("1Min")["pages_per_symbol"] == 24   # extended bars still arrive
    p = IG.plan(IG.BackfillRequest(tuple(f"S{i}" for i in range(100)), date(2025, 1, 1), date(2025, 12, 31), "1Min", "iex", extended_hours=True), now)
    assert p["estimate"]["requests"] == 2400 and p["estimate"]["minutes_at_budget"] == 40.0          # at 60 requests a minute


def test_C05_the_bytes_per_row_estimate_is_measured(tmp_path):
    s = new_store(tmp_path)
    syms = [f"S{chr(65 + i)}" for i in range(8)]
    did = s.create_dataset(spec(symbols=syms))
    days = sessions_2025(date(2025, 1, 2), 250)
    jan3 = datetime(2026, 1, 3, 15, 0, tzinfo=UTC)
    s.ingest(did, [daily(sym, d, 100.0 + i * 0.01, retrieved=jan3) for sym in syms for i, d in enumerate(days)])
    c = sqlite3.connect(s.path)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    per_row = s.path.stat().st_size / (len(syms) * len(days))
    assert 0.75 * IG.BYTES_PER_ROW_ESTIMATE <= per_row <= 1.25 * IG.BYTES_PER_ROW_ESTIMATE, per_row               # the planner's disk estimate is real


# ============================================================================================================ calendar (K)
def test_K01_2025_is_the_verified_nyse_history_and_classified_like_the_610a_classifier():
    days = [date(2025, 1, 1) + timedelta(i) for i in range(365)]
    assert sum(CAL.market_day_status(d) == "MARKET_DAY" for d in days) == 250 == HIST.SESSIONS_PER_YEAR[2025]
    assert HIST.HOLIDAYS == CAL.rule_holidays(2025) | HIST.EXCEPTIONAL_CLOSURES and HIST.EXCEPTIONAL_CLOSURES == {date(2025, 1, 9)}
    assert HIST.HOLIDAYS - CAL.rule_holidays(2025) == {date(2025, 1, 9)}                             # the national day of mourning
    assert {d for d in days if CAL.regular_minutes(d) == 210} == HIST.EARLY_CLOSES == {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}
    cases = [(ny(2025, 7, 3, 12, 59), "REG"), (ny(2025, 7, 3, 13, 0), "POST"), (ny(2025, 7, 3, 16, 59), "POST"), (ny(2025, 7, 3, 17, 0), "CLOSED"),
             (ny(2025, 7, 2, 19, 59), "POST"), (ny(2025, 7, 2, 20, 0), "CLOSED"),                                   # an early close ends late trading at 17:00
             (ny(2025, 1, 9, 10, 0), "CLOSED"), (ny(2025, 1, 10, 9, 30), "REG"), (ny(2025, 1, 10, 4, 0), "PRE"), (ny(2025, 1, 10, 3, 59), "CLOSED"),
             (datetime(2025, 3, 10, 13, 30, tzinfo=UTC), "REG"), (datetime(2025, 3, 10, 13, 29, tzinfo=UTC), "PRE"),      # the Monday after DST starts
             (datetime(2025, 11, 3, 14, 30, tzinfo=UTC), "REG"), (datetime(2025, 11, 3, 14, 29, tzinfo=UTC), "PRE"),      # the Monday after DST ends
             (ny(2025, 7, 4, 11, 0), "CLOSED"), (ny(2025, 7, 5, 11, 0), "CLOSED")]
    for t, want in cases:
        assert CAL.session_of(t) == want, t
    s = CAL.session(ny(2025, 11, 28, 10, 0))
    assert (s["covered"], s["calendar"], s["close_minute"], s["day_status"]) == (True, "nyse_history_2025_v1", 13 * 60, "MARKET_DAY")
    assert CAL.calendar_version(date(2025, 6, 2)) == HIST.HISTORY_VERSION and CAL.calendar_version(date(2026, 6, 2)) == "nyse_2026_2027_v1"
    assert CAL.previous_market_day(date(2026, 1, 2)) == date(2025, 12, 31) and CAL.next_market_day(date(2025, 12, 31)) == date(2026, 1, 2)
    assert len(HIST.SOURCES) == 4 and HIST.HISTORY_YEARS == (2025,)


def test_K02_2024_and_2028_stay_not_covered_and_fail_closed():
    for d in (date(2024, 12, 31), date(2024, 7, 3), date(2028, 1, 3)):
        assert CAL.market_day_status(d) == "NOT_COVERED" and CAL.calendar_version(d) == "NOT_COVERED"
        with pytest.raises(CAL.CalendarNotCovered):
            CAL.is_market_day(d)
    assert CAL.previous_market_day(date(2025, 1, 2)) is None                                           # 2025-01-01 a holiday, then 2024: unknown
    assert IG.coverage(date(2024, 12, 30), date(2025, 1, 3))["uncovered_first"] == ["2024-12-30", "2024-12-31"]
    assert IG.backfill_window_open(ny(2024, 7, 3, 22, 0)) is False                                       # an uncovered weekday never opens the window
    import autorun.calendar as AC
    assert AC.COVERED_YEARS == (2026, 2027) and not [d for d in AC.HOLIDAYS if d.year == 2025]           # the frozen 6.10A table is untouched

# ============================================================================================================ review findings (N, E)
def test_N01_B1_an_approval_is_a_file_for_the_exact_request_and_the_plan_recomputed_at_run_time_used_once(tmp_path):
    from market_intel.budget import ProviderBudget
    clk = Clock(SAT)
    s, arc, w = new_store(tmp_path, clk), RawArchive(tmp_path / "raw"), Wire(clk)
    rq = req(symbols=("SPY", "QQQ"))
    a, root = approve(tmp_path, rq)
    go = lambda r_, appr, **k: IG.run_backfill(r_, approval=appr, fetch_fn=w, store=s, archive=arc, clock=clk,   # noqa: E731
                                               sleep_fn=lambda x: clk.tick(x), **k)
    for other in (req(symbols=("SPY", "QQQ", "IWM")), req(symbols=("SPY", "QQQ"), end=date(2025, 12, 4)), dataclasses.replace(rq, adjustment="all"),
                  dataclasses.replace(rq, symbols=("QQQ", "SPY"))):
        with pytest.raises(APP.ApprovalError, match="not the approved request"):
            go(other, a)                                                                               # a mutated request is not the approved one
    b, _ = approve(tmp_path, rq, aid="OPS-HASH-0001", plan_sha256="0" * 64)
    with pytest.raises(APP.ApprovalError, match="does not hash"):
        go(rq, b)                                                                                      # the plan recomputed now must hash to it
    c_, _ = approve(tmp_path, rq, aid="OPS-EDIT-0001")
    doc = json.loads(c_.path.read_text(encoding="utf-8"))
    doc["max_rows"] = 99_999_999
    c_.path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(APP.ApprovalError, match="changed"):
        go(rq, c_)                                                                                     # the file edited after it was loaded
    with pytest.raises(APP.ApprovalError, match="changed"):
        go(rq, dataclasses.replace(a, max_rows=10 ** 9))                                               # an approval object built in code ...
    with pytest.raises(APP.ApprovalError):
        go(rq, dataclasses.replace(a, path=root / "ghost.json"))                                       # ... or naming no file: the file is the authority
    with pytest.raises(APP.ApprovalError, match="caps"):
        go(rq, approve(tmp_path, rq, aid="OPS-CAPS-0001", max_requests=1)[0])                          # caps below the plan's own estimate
    with pytest.raises(PermissionError, match="budget"):
        go(rq, a, budget=ProviderBudget("ALPACA_BACKFILL", 1000, clk))                                 # a budget above the plan's
    late = Clock(SAT + timedelta(hours=7))
    with pytest.raises(APP.ApprovalError, match="not valid now"):
        IG.run_backfill(rq, approval=a, fetch_fn=w, store=s, archive=arc, clock=late, sleep_fn=lambda x: None)   # expired
    good_doc = json.loads(a.path.read_text(encoding="utf-8"))
    broken = {"extra field": {**good_doc, "note": "x"}, "missing field": {k: v for k, v in good_doc.items() if k != "granted_by"},
              "version": {**good_doc, "version": "v0"}, "short id": {**good_doc, "approval_id": "x"}, "hash": {**good_doc, "plan_sha256": "ABC"},
              "zero cap": {**good_doc, "max_rows": 0}, "bool cap": {**good_doc, "max_requests": True}, "grantor": {**good_doc, "granted_by": " "},
              "naive time": {**good_doc, "granted_at": "2025-12-06T14:55:00"}, "over 24 h": {**good_doc, "expires_at": (SAT + timedelta(hours=25)).isoformat()},
              "not yet valid": {**good_doc, "granted_at": (SAT + timedelta(minutes=1)).isoformat()}, "sip flag": {**good_doc, "sip_approved": "yes"}}
    for name, d in broken.items():
        path = root / f"broken_{name.replace(' ', '_')}.json"
        path.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(APP.ApprovalError):
            APP.load(path, now=SAT, root=root)
    (root / "garbage.json").write_bytes(b"\xff not json")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.json").write_text(json.dumps(good_doc), encoding="utf-8")
    (root / "a.txt").write_text(json.dumps(good_doc), encoding="utf-8")
    for path in (root / "garbage.json", outside / "a.json", root / "a.txt", root / "missing.json"):
        with pytest.raises(APP.ApprovalError):
            APP.load(path, now=SAT, root=root)
    assert w.calls == [] and not s.exists()                                                            # every refusal: nothing sent, nothing created
    out = go(rq, a)
    assert out["status"] == "COMPLETED" and s.dataset(out["dataset_id"])["approval_use"]["approval_file_sha256"] == a.file_sha256
    n = len(w.calls)
    with pytest.raises(StoreError, match="already used"):
        go(rq, a)                                                                                      # an approval runs once ...
    assert len(w.calls) == n and s.status_counts()["tables"]["mi_datasets"] == 1                       # ... and the reuse sent and created nothing
    with pytest.raises(PermissionError, match="BACKFILL"):
        s.create_dataset(DatasetSpec(job_kind="BACKFILL", kind="BAR_1DAY", provider="ALPACA", feed="iex", adjustment="raw", symbols=["SPY"],
                                     timeframe="1Day", start_date=date(2025, 12, 1), end_date=date(2025, 12, 5), plan_sha256="0" * 64,
                                     approval_ref="OPS-NONE-01"))                                   # the store refuses an unapproved backfill too


def test_N02_B2_every_row_is_checked_against_its_dataset_scope_and_dataset_specs_are_validated(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec(symbols=("SPY",), start=date(2025, 12, 1), end=date(2025, 12, 5)))
    d = date(2025, 12, 1)
    base = daily("SPY", d, 100.0)
    foreign = {"SCOPE_KIND": (base.model_copy(update={"kind": "BAR_1HOUR"}), ScopeError),
               "SCOPE_PROVIDER": (base.model_copy(update={"provider": "SEC"}), ScopeError),
               "SCOPE_FEED": (daily("SPY", d, 100.0, feed="sip"), FeedRequired),
               "SCOPE_ADJUSTMENT": (daily("SPY", d, 100.0, adjustment="all"), ScopeError),
               "SCOPE_SYMBOL": (daily("QQQ", d, 100.0), ScopeError),
               "SCOPE_DATE": (daily("SPY", date(2025, 12, 8), 100.0), ScopeError),
               "SCOPE_DATASET_ID": (base.model_copy(update={"dataset_id": "another"}), ScopeError),
               "CONTENT_HASH_MISMATCH": (base.model_copy(update={"content_hash": "0" * 64}), StoreError)}
    for why, (o, err) in foreign.items():
        with pytest.raises(err, match=why):
            s.ingest(did, [base, o])                                                                   # refused: the whole batch, nothing stored
    assert s.history("BAR_1DAY", symbol="SPY", feed="iex") == []
    res = s.ingest(did, [base] + [o for o, _ in foreign.values()], on_reject="quarantine")
    assert (res.inserted, res.rejected) == (1, len(foreign)) and {r["reason"].split(":")[0] for r in res.rejections} == set(foreign)
    assert sorted(m["disposition"] for m in s.members(did)) == ["INSERTED"] + ["REJECTED"] * len(foreign)
    with pytest.raises(StoreError, match="rejected"):
        s.finish_dataset(did, "COMPLETED")                                                             # a quarantined row: never COMPLETED
    assert s.finish_dataset(did, "PARTIAL")["REJECTED"] == len(foreign)
    n0 = s.status_counts()["tables"]["mi_datasets"]
    bad_specs = [dataclasses.replace(spec(), kind="BAR_5MIN"), dataclasses.replace(spec(), provider="ACME"), dataclasses.replace(spec(), timeframe="1Hour"),
                 spec(symbols=("spy",)), spec(symbols=("SPY", "SPY")), spec(symbols=()), spec(start=date(2025, 12, 5), end=date(2025, 12, 1)),
                 spec(start=None), dataclasses.replace(spec(), job_kind="LIVE"), dataclasses.replace(spec(), adjustment="weird")]
    for bad in bad_specs:
        with pytest.raises(ScopeError):
            s.create_dataset(bad)
    with pytest.raises(CoverageError):
        s.create_dataset(spec(start=date(2024, 12, 30), end=date(2025, 1, 3)))                         # a range must be covered by the calendar
    assert s.status_counts()["tables"]["mi_datasets"] == n0                                            # no invalid dataset was created
    other = s.create_dataset(spec())
    fid_other = s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=other)
    mine = s.create_dataset(spec())
    fid_failed = s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, status="TIMEOUT"), dataset_id=mine)
    for fid in (fid_other, fid_failed, "no-such-fetch"):
        with pytest.raises(ScopeError, match="provenance"):
            s.ingest(mine, [base], fetch_id=fid)                                                       # rows carry their own fetch's provenance
    body = json.dumps({"symbol": "SPY", "bars": [{"t": f"2025-{md}T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9} for md in ("11-28", "12-01", "12-08")],
                       "next_page_token": None}).encode()
    obs, _, problems = AB.parse_page(body, symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT,
                                     start_date=date(2025, 12, 1), end_date=date(2025, 12, 5))
    assert [o.session_date for o in obs] == [date(2025, 12, 1)] and problems == [{"problem": "OUT_OF_RANGE", "index": 0}, {"problem": "OUT_OF_RANGE", "index": 2}]


class Scripted(Wire):
    """Wire with per-call behaviour: an exception to raise or a response to return (by call index)."""

    def __init__(self, clock, actions, **k):
        super().__init__(clock, **k)
        self.actions = actions

    def __call__(self, params):
        i = len(self.calls)
        act = self.actions.get(i)
        if isinstance(act, BaseException):
            self.calls.append(params)
            self.clock.tick(1)
            raise act
        if act is not None:
            self.calls.append(params)
            self.clock.tick(1)
            return act
        return super().__call__(params)


def _parser_bug(*a, **k):
    raise RuntimeError("a parser bug")


def test_N03_B3_transport_parser_and_store_failures_are_recorded_and_the_dataset_is_always_sealed(tmp_path, monkeypatch):
    rq = req(symbols=("SPY", "QQQ"))

    def scenario(name, actions=None, patch=None):
        clk = Clock(SAT)
        st = new_store(tmp_path, clk, f"{name}.db")
        arc = RawArchive(tmp_path / f"raw_{name}")
        if patch:
            patch(st)
        out = run(tmp_path, rq, clk, st, wire=Scripted(clk, actions or {}), archive=arc, aid=f"OPS-{name}")
        ds = st.dataset(out["dataset_id"])
        assert ds["status"] == out["status"] and st.status_counts()["unsealed_datasets"] == 0 and out["dataset_id"] in arc.anchors(), name
        c = sqlite3.connect(st.path)
        fetches = [r[0] for r in c.execute("SELECT status FROM mi_fetches ORDER BY started_at, rowid")]
        c.close()
        return out, fetches, st

    out, fetches, _ = scenario("timeout", {0: TimeoutError("read timed out")})
    assert (out["status"], out["failed_fetches"], out["unrecovered"], fetches) == ("COMPLETED", 1, 0, ["TIMEOUT", "OK", "OK"])   # retried, recovered
    out, fetches, _ = scenario("conn", {0: ConnectionError("x"), 1: ConnectionError("x"), 2: ConnectionError("x")})
    assert (out["status"], out["unrecovered"], fetches) == ("PARTIAL", 1, ["ERROR", "ERROR", "ERROR", "OK"])                  # never recovered: PARTIAL
    for bad in ("soon", "-5", "nan", "inf", "", "1e999"):
        assert IG.retry_after_s({"Retry-After": bad}, SAT) is None, bad                                 # garbage never crashes the run
    assert IG.retry_after_s({"retry-after": "Sat, 06 Dec 2025 15:00:30 GMT"}, SAT) == 30.0 and IG.retry_after_s("nope", SAT) is None
    out, fetches, _ = scenario("retryafter", {0: (429, {"Retry-After": "soon"}, b"slow")})
    assert (out["status"], fetches) == ("COMPLETED", ["RATE_LIMITED", "OK", "OK"])
    out, fetches, st = scenario("parser", patch=lambda _st: monkeypatch.setattr(AB, "parse_page", _parser_bug))
    monkeypatch.undo()
    assert (out["status"], set(fetches)) == ("FAILED", {"INVALID_RESPONSE"})                           # no page parsed: FAILED, never RUNNING
    assert {m["reason"] for m in st.members(out["dataset_id"])} == {"PARSER_ERROR:RuntimeError"}
    out, fetches, _ = scenario("contract", {0: ("200", {}, b"{}"), 1: (200, {}, "not bytes")})
    assert (out["status"], fetches) == ("FAILED", ["INVALID_RESPONSE", "INVALID_RESPONSE"])
    out, fetches, _ = scenario("badjson", {0: (200, {}, b"<html>oops</html>")})
    assert (out["status"], fetches[0]) == ("PARTIAL", "INVALID_RESPONSE")

    def broken_ingest(st):
        def boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        st.ingest = boom
    out, fetches, _ = scenario("ingest", patch=broken_ingest)
    assert (out["status"], out["stop"]) == ("PARTIAL", "ERROR:OperationalError")                       # a store error mid-run: sealed PARTIAL
    clk = Clock(SAT)
    st = new_store(tmp_path, clk, "interrupt.db")
    a, root = approve(tmp_path, rq, aid="OPS-interrupt")
    with pytest.raises(KeyboardInterrupt):
        IG.run_backfill(rq, approval=a, fetch_fn=Scripted(clk, {1: KeyboardInterrupt()}), store=st,
                        archive=RawArchive(tmp_path / "raw_int"), clock=clk, sleep_fn=lambda x: None)
    c = sqlite3.connect(st.path)
    assert c.execute("SELECT status, detail FROM mi_dataset_events ORDER BY event_id DESC LIMIT 1").fetchone()[0] == "PARTIAL"   # sealed, then re-raised
    c.close()


def _copy_db(src: Path, dst: Path) -> Path:
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    a.backup(b)
    a.close()
    b.close()
    return dst


def test_N04_B4_schema_objects_are_verified_and_row_tampering_is_detected_by_hashes_and_external_anchors(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    out = run(tmp_path, req(), clk, s, archive=arc)
    assert s.verify_integrity(anchors=arc.anchors())["ok"] and s.verify_integrity()["rows_checked"] == 5
    tamper = {"dropped trigger": ["DROP TRIGGER mi_observations_no_update"],
              "extra trigger": ["CREATE TRIGGER mi_x AFTER INSERT ON mi_fetches BEGIN SELECT 1; END"],
              "extra index": ["CREATE INDEX mi_x ON mi_fetches(status)"],
              "dropped index": ["DROP INDEX mi_obs_symbol_kind_time"],
              "extra table": ["CREATE TABLE mi_x (a)"],
              "altered table": ["ALTER TABLE mi_fetches ADD COLUMN note TEXT"]}
    for name, sqls in tamper.items():
        db = _copy_db(s.path, tmp_path / f"t_{name.replace(' ', '_')}.db")
        c = sqlite3.connect(db)
        for q in sqls:
            c.execute(q)
        c.commit()
        c.close()
        t = MarketIntelStore(db, clock=clk)
        with pytest.raises(MG.SchemaMismatch):
            t.history("BAR_1DAY", symbol="SPY", feed="iex")                                            # reads refuse a tampered schema ...
        with pytest.raises(MG.SchemaMismatch):
            t.record_budget_event("ALPACA", "ALLOW")                                                   # ... and so do writes
    db = _copy_db(s.path, tmp_path / "edited.db")
    c = sqlite3.connect(db)
    trig = {n: q for n, q in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    c.execute("DROP TRIGGER mi_observations_no_update")
    c.execute("UPDATE mi_observations SET payload_json = replace(payload_json, '\"close\":10.5', '\"close\":99.5') WHERE session_date = '2025-12-03'")
    c.execute(trig["mi_observations_no_update"])
    c.commit()
    c.close()
    t = MarketIntelStore(db, clock=clk)
    assert t.history("BAR_1DAY", symbol="SPY", feed="iex", as_of=SAT + timedelta(days=1))[2]["payload"]["close"] == 99.5   # the documented limit:
    rep_ = t.verify_integrity()                                                                        # a restored trigger passes the schema check,
    assert rep_["ok"] is False and len(rep_["row_hash_mismatches"]) == 1                               # but the row hash catches the edit
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM mi_observations WHERE session_date = '2025-12-03'").fetchone()
    h = ST._row_hash(r)                                                                                 # a thorough forger re-hashes everything inside the file
    for n in ("mi_observations_no_update", "mi_dataset_members_no_update", "mi_dataset_events_no_update"):
        c.execute(f"DROP TRIGGER {n}")
    c.execute("UPDATE mi_observations SET content_hash = ? WHERE obs_id = ?", (h, r["obs_id"]))
    c.execute("UPDATE mi_dataset_members SET content_hash = ? WHERE record_id = ?", (h, r["obs_id"]))
    lines = {f"{m['natural_key']}\t{m['content_hash']}" for m in c.execute("SELECT natural_key, content_hash FROM mi_dataset_members WHERE dataset_id = ? "
                                                                            "AND disposition IN ('INSERTED', 'REVISED', 'DUPLICATE')", (out["dataset_id"],))}
    c.execute("UPDATE mi_dataset_events SET content_sha256 = ? WHERE dataset_id = ? AND status = 'COMPLETED'", (ST.manifest_sha256(lines), out["dataset_id"]))
    for n in ("mi_observations_no_update", "mi_dataset_members_no_update", "mi_dataset_events_no_update"):
        c.execute(trig[n])
    c.commit()
    c.close()
    rep_ = t.verify_integrity()
    assert rep_["row_hash_mismatches"] == rep_["member_mismatches"] == rep_["manifest_mismatches"] == [] and len(rep_["record_mismatches"]) == 1
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    for n in ("mi_dataset_members_no_update", "mi_dataset_events_no_update"):
        c.execute(f"DROP TRIGGER {n}")
    full = ST.record_sha256(c.execute("SELECT * FROM mi_observations WHERE obs_id = ?", (r["obs_id"],)).fetchone())
    c.execute("UPDATE mi_dataset_members SET record_sha256 = ? WHERE record_id = ?", (full, r["obs_id"]))
    recs = [m[0] for m in c.execute("SELECT record_sha256 FROM mi_dataset_members WHERE dataset_id = ? AND record_sha256 IS NOT NULL", (out["dataset_id"],))]
    c.execute("UPDATE mi_dataset_events SET records_sha256 = ? WHERE dataset_id = ? AND status = 'COMPLETED'", (ST.manifest_sha256(recs), out["dataset_id"]))
    e = c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? AND status = 'COMPLETED'", (out["dataset_id"],)).fetchone()
    stated = {k: e[col] for k, col in MarketIntelStore._COUNT_COLUMNS.items()}                          # ... and the seal's metadata digest (members)
    c.execute("UPDATE mi_dataset_events SET metadata_sha256 = ? WHERE event_id = ?",
              (MarketIntelStore._metadata_sha256(c, out["dataset_id"], "COMPLETED", stated), e["event_id"]))
    for n in ("mi_dataset_members_no_update", "mi_dataset_events_no_update"):
        c.execute(trig[n])
    c.commit()
    c.close()
    assert t.verify_integrity()["state"] == "UNVERIFIED"                                               # consistent inside the file ...
    v = t.verify_integrity(anchors=arc.anchors())
    assert v["anchor_mismatches"] == [out["dataset_id"]] and v["state"] == "CORRUPT"                     # ... the external anchor still disagrees
    db2 = _copy_db(s.path, tmp_path / "lookahead.db")                                                  # a look-ahead forgery: only a time moves
    c = sqlite3.connect(db2)
    c.execute("DROP TRIGGER mi_observations_no_update")
    c.execute("UPDATE mi_observations SET available_at = '2025-12-01T21:00:00.000000Z', retrieved_at = '2025-12-01T21:00:00.000000Z' "
              "WHERE session_date = '2025-12-01'")
    c.execute(trig["mi_observations_no_update"])
    c.commit()
    c.close()
    look = MarketIntelStore(db2, clock=clk).verify_integrity(anchors=arc.anchors())
    assert look["row_hash_mismatches"] == [] and len(look["record_mismatches"]) == 1 and look["ok"] is False   # the content is unchanged; the row is not


def test_N05_B5_nothing_is_stored_or_read_before_it_could_have_existed(tmp_path):
    s = new_store(tmp_path)
    fx = s.create_dataset(spec(job="FIXTURE"))
    tonight = ny(2025, 12, 2, 21, 0)
    tomorrow = daily("SPY", date(2025, 12, 3), 1.0, retrieved=tonight, backfilled=False)               # a bar of a session not yet started
    with pytest.raises(ChronologyError, match="OBSERVED_AFTER_RETRIEVAL"):
        s.ingest(fx, [tomorrow])
    forming = daily("SPY", date(2025, 12, 2), 1.0, retrieved=ny(2025, 12, 2, 17, 0), backfilled=False)  # late trading still open: not final
    with pytest.raises(ChronologyError, match="BAR_NOT_COMPLETE_AT_RETRIEVAL"):
        s.ingest(fx, [forming])
    mfx = s.create_dataset(spec(job="FIXTURE", kind="BAR_1MIN"))
    t0 = ny(2025, 12, 2, 10, 0)
    minute = Observation(kind="BAR_1MIN", symbol="SPY", provider="ALPACA", feed="iex", adjustment="raw", observed_at=t0, retrieved_at=t0 + timedelta(seconds=30),
                         available_at=t0 + timedelta(seconds=30), session="REG", session_date=date(2025, 12, 2), status="AVAILABLE",
                         payload={"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    with pytest.raises(ChronologyError, match="BAR_NOT_COMPLETE_AT_RETRIEVAL"):
        s.ingest(mfx, [minute])                                                                       # a minute bar retrieved mid-minute
    published = daily("SPY", date(2025, 12, 1), 1.0, retrieved=tonight, backfilled=False).model_copy(update={"published_at": tonight + timedelta(hours=1)})
    with pytest.raises(ChronologyError, match="PUBLISHED_AFTER_RETRIEVAL"):
        s.ingest(fx, [published])
    final = daily("SPY", date(2025, 12, 2), 7.0, retrieved=ny(2025, 12, 2, 20, 5), backfilled=False)
    res = s.ingest(fx, [tomorrow, forming, final], on_reject="quarantine")
    assert (res.inserted, res.rejected) == (1, 2)
    for t in (ny(2025, 12, 2, 20, 4), ny(2025, 12, 2, 16, 0)):
        assert s.history("BAR_1DAY", symbol="SPY", feed="iex", as_of=t) == [] and s.latest_usable("BAR_1DAY", at=t, symbol="SPY", feed="iex") is None
    later = SAT + timedelta(days=30)
    assert [r["payload"]["close"] for r in s.history("BAR_1DAY", symbol="SPY", feed="iex", as_of=later)] == [7.0]     # the rejected rows never appear
    c = sqlite3.connect(s.path)
    with pytest.raises(sqlite3.IntegrityError):                                                        # and the schema refuses an end before the start
        c.execute("INSERT INTO mi_observations SELECT 'x', natural_key || 'x', 1, NULL, content_hash, dataset_id, fetch_id, kind, symbol, series_id, provider, "
                  "feed, adjustment, observed_at, '2000-01-01T00:00:00.000000Z', published_at, retrieved_at, available_at, ingested_at, session, session_date, "
                  "status, usable, freshness, age_s, source_url, source_identifier, tier, quality_flags_json, backfilled, payload_json, envelope_version "
                  "FROM mi_observations LIMIT 1")
    c.close()


def test_N06_B6_every_delivered_row_is_a_member_and_a_dataset_hash_covers_what_it_delivered(tmp_path):
    s = new_store(tmp_path)
    days = sessions_2025(date(2025, 12, 1), 5)
    d1 = s.create_dataset(spec())
    s.ingest(d1, [daily("SPY", d, 100.0 + i) for i, d in enumerate(days)])
    h1 = s.finish_dataset(d1, "COMPLETED")
    d2 = s.create_dataset(spec())                                                                     # the same download again, into the same store
    r2 = s.ingest(d2, [daily("SPY", d, 100.0 + i, retrieved=SAT + timedelta(hours=1)) for i, d in enumerate(days)])
    h2 = s.finish_dataset(d2, "COMPLETED")
    assert (r2.inserted, r2.duplicates) == (0, 5) and h2["content_sha256"] == h1["content_sha256"] != ST.manifest_sha256([])   # never the empty hash
    assert [m["disposition"] for m in s.members(d2)] == ["DUPLICATE"] * 5
    assert {m["record_id"] for m in s.members(d2)} == {m["record_id"] for m in s.members(d1)}         # each duplicate names the stored value
    d3 = s.create_dataset(spec())
    s.ingest(d3, [daily("SPY", d, 100.0 + i) for i, d in enumerate(days)][:4])
    s.ingest(d3, [daily("SPY", d, 100.0 + i) for i, d in enumerate(days)][:4])                        # a repeated page: distinct facts count once
    assert s.finish_dataset(d3, "PARTIAL")["content_sha256"] == ST.manifest_sha256(
        {f"{natural_key(daily('SPY', d, 1.0))}\t{content_hash(daily('SPY', d, 100.0 + i))}" for i, d in enumerate(days[:4])})
    fresh = new_store(tmp_path, name="fresh.db")
    f1 = fresh.create_dataset(spec())
    fresh.ingest(f1, [daily("SPY", d, 100.0 + i) for i, d in enumerate(days)])
    assert fresh.finish_dataset(f1, "COMPLETED")["content_sha256"] == h1["content_sha256"]           # reproducible across stores
    rep_ = s.verify_integrity()
    assert rep_["state"] == "UNVERIFIED" and rep_["manifest_mismatches"] == rep_["metadata_mismatches"] == rep_["count_mismatches"] == []


def test_N07_B7_a_failed_fetch_is_recovered_only_by_the_identical_request(tmp_path):
    s = new_store(tmp_path)
    ep = "/v2/stocks/SPY/bars"
    P = lambda sym: AB.request_params(sym, "1Day", ny(2025, 12, 1, 0, 0), ny(2025, 12, 6, 0, 0), "iex", "raw")   # noqa: E731
    cases = {"other params": ((ep, P("SPY")), ("ALPACA", ep, P("QQQ")), False),
             "other endpoint": ((ep, P("SPY")), ("ALPACA", "/v2/stocks/QQQ/bars", P("SPY")), False),
             "other provider": ((ep, P("SPY")), ("SEC", ep, P("SPY")), False),
             "empty params": ((ep, {}), ("ALPACA", ep, {}), False),
             "identical": ((ep, P("SPY")), ("ALPACA", ep, P("SPY")), True)}
    for name, ((f_ep, f_params), (ok_provider, ok_ep, ok_params), recovered) in cases.items():
        did = s.create_dataset(spec())
        s.record_fetch(FetchRecord(provider="ALPACA", endpoint=f_ep, started_at=SAT, status="HTTP_ERROR", http_status=503), dataset_id=did, params=f_params)
        s.record_fetch(FetchRecord(provider=ok_provider, endpoint=ok_ep, started_at=SAT + timedelta(seconds=5), retrieved_at=SAT + timedelta(seconds=6),
                                   status="OK"), dataset_id=did, params=ok_params)
        if recovered:
            assert s.finish_dataset(did, "COMPLETED")["unrecovered_failures"] == 0, name
        else:
            with pytest.raises(StoreError, match="unrecovered"):
                s.finish_dataset(did, "COMPLETED")
            assert s.finish_dataset(did, "PARTIAL")["unrecovered_failures"] == 1, name


def test_N08_B8_a_sealed_dataset_is_never_resealed(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 1.0)])
    s.finish_dataset(did, "COMPLETED")
    for again in ("FAILED", "PARTIAL", "COMPLETED", "EMPTY"):
        with pytest.raises(StoreError, match="sealed"):
            s.finish_dataset(did, again)
    assert [e["status"] for e in s.dataset(did)["events"]] == ["RUNNING", "COMPLETED"] and s.dataset(did)["status"] == "COMPLETED"


def test_N09_B9_bar_payloads_are_validated_and_absences_never_hide_or_pose_as_current_values(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    did = s.create_dataset(spec())
    good = daily("SPY", date(2025, 12, 1), 100.0)
    for payload, why in (({}, "PAYLOAD_OPEN"), ({**good.payload, "close": "100"}, "PAYLOAD_CLOSE"), ({**good.payload, "volume": True}, "PAYLOAD_VOLUME"),
                         ({k: v for k, v in good.payload.items() if k != "low"}, "PAYLOAD_LOW")):
        with pytest.raises(PayloadError, match=why):
            s.ingest(did, [good.model_copy(update={"payload": payload})])                              # an AVAILABLE bar needs real OHLCV
    bad_shape = daily("SPY", date(2025, 12, 2), 100.0).model_copy(update={"payload": {"open": 10, "high": 9, "low": 8, "close": 10, "volume": 5}})
    s.ingest(did, [good, bad_shape])
    rows = {r["session_date"]: r for r in s.history("BAR_1DAY", symbol="SPY", feed="iex")}
    assert rows["2025-12-02"]["usable"] is False and "HIGH_BELOW_BODY" in rows["2025-12-02"]["quality_flags"] and rows["2025-12-01"]["usable"] is True
    stale = Observation(**{**daily("SPY", date(2025, 12, 3), 101.0).model_dump(), "status": "STALE", "freshness": "STALE"})
    s.ingest(did, [stale])
    assert s.latest_usable("BAR_1DAY", at=SAT, symbol="SPY", feed="iex")["session_date"] == "2025-12-01"   # a STALE or blocked value is not usable
    clk.tick(3600)
    later = s.create_dataset(spec())
    s.ingest(later, [daily("SPY", date(2025, 12, 1), 0.0, status="UNAVAILABLE", retrieved=clk()),
                     daily("SPY", date(2025, 12, 4), 0.0, status="UNAVAILABLE", retrieved=clk())])
    key = natural_key(good)
    assert len(s.revisions(key)) == 1 and len(s.absences(key)) == 1                                   # an absence is never a revision of a value
    st = s.latest_status("BAR_1DAY", at=clk(), symbol="SPY", feed="iex")
    assert st["latest_usable"]["payload"]["close"] == 100.0                                           # the last usable value is still shown ...
    assert (st["latest_record"]["record_type"], st["latest_record"]["session_date"], st["usable_is_latest"], st["status"], st["reason"]) == (
        "ABSENCE", "2025-12-04", False, "UNAVAILABLE", "LATEST_RECORD_IS_ABSENCE")                    # ... never as the current state
    before = s.latest_status("BAR_1DAY", at=SAT, symbol="SPY", feed="iex")
    assert before["latest_record"]["record_type"] == "VALUE" and before["latest_record"]["session_date"] == "2025-12-03" and before["status"] != "AVAILABLE"


def test_N10_item10_event_end_is_stored_and_operational_as_of_never_shortcuts_to_research_availability(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 1.0), daily("SPY", date(2025, 7, 3), 1.0), daily("SPY", date(2025, 11, 28), 1.0)])
    ends = {r["session_date"]: r["event_end"] for r in s.history("BAR_1DAY", symbol="SPY", feed="iex")}
    assert ends == {"2025-07-03": "2025-07-03T21:00:00.000000Z", "2025-11-28": "2025-11-28T22:00:00.000000Z",      # 17:00 New York on an early close
                    "2025-12-01": "2025-12-02T01:00:00.000000Z"}                                                    # 20:00 New York otherwise
    t0 = ny(2025, 12, 2, 10, 0)
    for kind, tf, step in (("BAR_1MIN", "1Min", timedelta(minutes=1)), ("BAR_1HOUR", "1Hour", timedelta(hours=1))):
        o = Observation(kind=kind, symbol="SPY", provider="ALPACA", feed="iex", adjustment="raw", observed_at=t0, retrieved_at=SAT, available_at=SAT,
                        session="REG", session_date=date(2025, 12, 2), status="AVAILABLE", backfilled=True,
                        payload={"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timeframe": tf})
        assert ST.event_end(o) == t0 + step
    assert s.latest_usable("BAR_1DAY", at=ny(2025, 12, 1, 20, 30), symbol="SPY", feed="iex") is None   # complete then, but not KNOWN then (backfilled)
    for name in ("history", "latest_usable", "latest_status", "snapshot", "quality_report"):
        params = inspect.signature(getattr(MarketIntelStore, name)).parameters
        assert not [q for q in params if any(w in q for w in ("event", "research", "vintage", "latency"))], name
    assert not [n for n in dir(MarketIntelStore) if any(w in n.lower() for w in ("research", "vintage", "backtest"))]   # no research shortcut (Stage 7.6)


def test_N11_B11_the_2025_early_close_post_market_ends_at_17_00_and_2026_2027_use_the_versioned_override():
    assert HIST.EARLY_CLOSES == {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}
    for d in sorted(HIST.EARLY_CLOSES):
        y, m, dd = d.year, d.month, d.day
        assert [CAL.session_of(ny(y, m, dd, hh, mm)) for hh, mm in ((12, 59), (13, 0), (16, 59), (17, 0), (19, 59))] == ["REG", "POST", "POST", "CLOSED", "CLOSED"]
        assert CAL.post_market_end_minute(d) == HIST.EARLY_CLOSE_LATE_TRADING_END_MINUTE == 17 * 60
    assert CAL.session_of(ny(2025, 12, 1, 19, 59)) == "POST" and CAL.post_market_end_minute(date(2025, 12, 1)) == 20 * 60
    assert CAL.post_market_end_minute(date(2025, 7, 4)) is None and CAL.post_market_end_minute(date(2024, 7, 3)) is None
    assert CAL.session_of(ny(2026, 11, 27, 18, 0)) == "CLOSED" and CAL.post_market_end_minute(date(2026, 11, 27)) == 17 * 60   # the override (G12)
    assert CAL.regular_minutes(date(2026, 11, 27)) == 210                                                                    # the 13:00 close is unchanged


def _minute_rows(n_days, start=date(2025, 11, 3)):
    out = []
    for d in sessions_2025(start, n_days):
        t = ny(d.year, d.month, d.day, 9, 30)
        for i in range(390 if CAL.regular_minutes(d) == 390 else 210):
            x = t + timedelta(minutes=i)
            out.append(Observation(kind="BAR_1MIN", symbol="SPY", provider="ALPACA", feed="iex", adjustment="raw", observed_at=x, retrieved_at=SAT,
                                   available_at=SAT, session="REG", session_date=d, status="AVAILABLE", backfilled=True,
                                   payload={"open": 1.0, "high": 1.0 + i / 1000, "low": 1.0, "close": 1.0 + i / 1000, "volume": 1}))
    return out


def test_N12_B10_latest_reads_are_bounded_and_plans_above_the_generic_store_cap_are_refused(tmp_path):
    steps = {}
    for n_days in (2, 20):
        s = new_store(tmp_path, name=f"m{n_days}.db")
        did = s.create_dataset(spec(kind="BAR_1MIN", symbols=("SPY",), start=date(2025, 11, 1), end=date(2025, 12, 31)))
        rows = _minute_rows(n_days)
        s.ingest(did, rows)
        c = sqlite3.connect(f"file:{s.path.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        count = [0]

        def tick():
            count[0] += 1
            return 0

        c.set_progress_handler(tick, 100)
        latest = s._latest_value(c, "BAR_1MIN", SAT, "SPY", None, "iex", None, usable=True)
        c.set_progress_handler(None, 0)
        assert latest["observed_at"] == ts(rows[-1].observed_at)
        a = ts(SAT)
        where, args = s._scope("BAR_1MIN", "SPY", None, "iex", None)
        plan_ = " ".join(r[3] for r in c.execute(f"EXPLAIN QUERY PLAN SELECT o.* FROM mi_observations o WHERE {' AND '.join(where)} AND o.observed_at <= ? AND "
                                                 f"o.available_at <= ? AND o.ingested_at <= ? AND {s._KNOWN_REV} AND o.usable = 1 ORDER BY o.observed_at DESC, "
                                                 f"o.natural_key DESC LIMIT 1", args + [a] * 5))
        c.close()
        assert "mi_obs_symbol_kind_time" in plan_ and "TEMP B-TREE" not in plan_, plan_                 # an index walk, no sort of the partition
        steps[n_days] = count[0]
    assert steps[20] <= 2 * steps[2] + 5, steps                                                         # 10x the rows, the same work: bounded
    now = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    big = IG.plan(IG.BackfillRequest(tuple(f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(100)), date(2025, 1, 1), date(2025, 12, 31), "1Min", "iex",
                                     extended_hours=True), now)
    assert big["valid"] is False and [r["code"] for r in big["refusals"]] == ["ROWS_EXCEED_GENERIC_STORE_CAP"]
    assert big["estimate"]["rows_total"] == big["estimate"]["rows_delivered_max"] == 100 * (247 * 960 + 3 * 780)
    ok = IG.plan(IG.BackfillRequest(tuple(f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(100)), date(2025, 1, 1), date(2025, 12, 31), "1Day", "iex"), now)
    assert ok["valid"] and ok["estimate"]["rows_total"] == 25_000 <= IG.MAX_PLAN_ROWS


class Dirty(Wire):
    """Wire whose QQQ pages carry an out-of-range bar, a malformed row, a NaN close and a string close (the parser's type check)."""

    def __call__(self, params):
        status, headers, body = super().__call__(params)
        if params["symbol"] != "QQQ" or status != 200:
            return status, headers, body
        doc = json.loads(body)
        doc["bars"][1]["c"] = "10.5"                                                                    # a string price: a malformed row
        doc["bars"] += [{"t": "2025-12-08T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1, "v": 1},       # outside the requested range
                        {"t": "2025-12-02T05:00:00Z", "o": 1},                                          # malformed
                        {"t": "2025-12-03T06:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": float("nan"), "v": 1}]   # refused by the envelope
        return status, headers, json.dumps(doc).encode()


def test_E01_an_approved_synthetic_run_end_to_end_quarantines_bad_rows_and_verifies(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    out = run(tmp_path, req(symbols=("SPY", "QQQ")), clk, s, wire=Dirty(clk), archive=arc)
    assert (out["status"], out["inserted"], out["problems"], out["rejected"]) == ("PARTIAL", 9, 4, 0)
    reasons = sorted(m["reason"].split(":")[0] for m in s.members(out["dataset_id"]) if m["disposition"] == "REJECTED")
    assert reasons == ["MALFORMED_ROW", "MALFORMED_ROW", "OUT_OF_RANGE", "REFUSED_BY_ENVELOPE"]
    assert len(s.history("BAR_1DAY", symbol="SPY", feed="iex")) == 5 and len(s.history("BAR_1DAY", symbol="QQQ", feed="iex")) == 4
    assert s.verify_integrity(anchors=arc.anchors())["ok"] and arc.verify()["ok"] and arc.anchors()[out["dataset_id"]]["content_sha256"] == out["content_sha256"]
    assert s.status_counts()["unsealed_datasets"] == 0 and s.dataset(out["dataset_id"])["approval_use"]["approval_id"] == "OPS-TEST-0001"

# ============================================================================================================ second review (G, X)
def bar(kind, t, close=1.0, *, retrieved=SAT, backfilled=True, symbol="SPY", **kw):
    """A bar whose session date and session are DERIVED from its time (as the store derives them)."""
    s_ = CAL.session(t)
    d = dict(kind=kind, symbol=symbol, provider="ALPACA", feed="iex", adjustment="raw", observed_at=t, retrieved_at=retrieved, available_at=retrieved,
             session="REG" if kind == "BAR_1DAY" else s_["session"], session_date=s_["session_date"], status="AVAILABLE", backfilled=backfilled,
             payload={"open": close, "high": close, "low": close, "close": close, "volume": 1})
    d.update(kw)
    return Observation(**d)


def bars_wire(clock, bars, symbol="SPY"):
    def fn(params):
        clock.tick(1)
        return 200, {}, json.dumps({"symbol": symbol, "bars": bars, "next_page_token": None}).encode()
    return fn


def jbar(t: datetime, c=10.0):
    return {"t": t.astimezone(UTC).isoformat().replace("+00:00", "Z"), "o": c, "h": c, "l": c, "c": c, "v": 100, "n": 1, "vw": c}


def daily_params(sym="SPY", start=date(2025, 12, 1), end=date(2025, 12, 5)):
    return AB.request_params(sym, "1Day", ny(start.year, start.month, start.day, 0, 0), ny(end.year, end.month, end.day, 0, 0) + timedelta(days=1), "iex", "raw")


def test_G01_F1_the_store_itself_verifies_operator_authority_for_every_backfill_dataset(tmp_path):
    s = new_store(tmp_path)
    rq = req()
    a, root = approve(tmp_path, rq)
    good = IG.dataset_spec(IG.plan(rq, SAT), a)
    for forged in (None, SimpleNamespace(plan_sha256=a.plan_sha256, approval_id=a.approval_id, file_sha256=a.file_sha256, max_requests=9, max_rows=9),
                   dataclasses.replace(a, path=root / "ghost.json"), dataclasses.replace(a, max_rows=10 ** 9)):
        with pytest.raises(PermissionError):
            s.create_dataset(good, approval=forged)                                                    # fabricated, missing or edited: refused
    late = MarketIntelStore(s.path, clock=Clock(SAT + timedelta(hours=7)), approvals_root=root)
    with pytest.raises(PermissionError, match="not valid now"):
        late.create_dataset(good, approval=a)                                                          # expired - checked by the store itself
    elsewhere = MarketIntelStore(s.path, clock=Clock(SAT), approvals_root=tmp_path / "elsewhere")
    with pytest.raises(PermissionError, match="inside the approvals directory"):
        elsewhere.create_dataset(good, approval=a)                                                     # a file outside the store's approvals directory
    for bad in (dataclasses.replace(good, symbols=["QQQ"]), dataclasses.replace(good, feed="sip"), dataclasses.replace(good, end_date=date(2025, 12, 4)),
                dataclasses.replace(good, adjustment="all"), dataclasses.replace(good, kind="BAR_1HOUR", timeframe="1Hour"),
                dataclasses.replace(good, plan_sha256="0" * 64), dataclasses.replace(good, approval_ref="OPS-OTHER-01"), dataclasses.replace(good, request={}),
                dataclasses.replace(good, request={**good.request, "symbols": ["SPY", "QQQ"]})):
        with pytest.raises((PermissionError, ScopeError)):
            s.create_dataset(bad, approval=a)                                                          # every scope field is bound to the approval
    assert not s.exists()
    did = s.create_dataset(good, approval=a)                                                           # the one spec the approved plan allows
    with pytest.raises(StoreError, match="already used"):
        s.create_dataset(good, approval=a)                                                             # same-database replay: one transaction, refused
    assert s.status_counts()["tables"]["mi_datasets"] == 1 and s.dataset(did)["approval_use"]["max_rows"] == a.max_rows
    with pytest.raises(ScopeError, match="only a BACKFILL"):
        s.create_dataset(spec(), approval=a)
    assert s.create_dataset(spec(job="FIXTURE"))                                                      # synthetic fixtures need no approval


def test_G02_F2_labels_are_derived_from_timestamps_and_rows_are_bound_to_their_fetch(tmp_path):
    s = new_store(tmp_path)
    m = s.create_dataset(spec(kind="BAR_1MIN"))
    t0 = ny(2025, 12, 2, 10, 0)
    good = bar("BAR_1MIN", t0)
    cases = {"SESSION_DATE_MISMATCH": good.model_copy(update={"session_date": date(2025, 12, 3)}),
             "SESSION_MISMATCH": good.model_copy(update={"session": "POST"}),
             "TIMEFRAME_MISALIGNED": bar("BAR_1MIN", t0 + timedelta(seconds=30)),
             "TIMEFRAME_MISMATCH": good.model_copy(update={"payload": {**good.payload, "timeframe": "1Hour"}})}
    for why, o in cases.items():
        with pytest.raises(ScopeError, match=why):
            s.ingest(m, [o])
    d = s.create_dataset(spec())
    for why, o in (("TIMEFRAME_MISALIGNED", daily("SPY", date(2025, 12, 1), 1.0).model_copy(update={"observed_at": datetime(2025, 12, 1, 5, 30, tzinfo=UTC)})),
                   ("SESSION_MISMATCH", daily("SPY", date(2025, 12, 1), 1.0).model_copy(update={"session": "POST"}))):
        with pytest.raises(ScopeError, match=why):
            s.ingest(d, [o])
    t26 = datetime(2026, 12, 6, 15, 0, tzinfo=UTC)                                                     # the review's reproduction: a 2026 bar labelled 2025
    s26 = new_store(tmp_path, Clock(t26), "y26.db")
    d26 = s26.create_dataset(spec(kind="BAR_1MIN"))
    with pytest.raises(ScopeError, match="SESSION_DATE_MISMATCH"):
        s26.ingest(d26, [good.model_copy(update={"observed_at": datetime(2026, 12, 1, 15, 0, tzinfo=UTC), "retrieved_at": t26, "available_at": t26})])
    P = daily_params()

    def fetch(endpoint="/v2/stocks/SPY/bars", retrieved=SAT, **over):
        return s.record_fetch(FetchRecord(provider="ALPACA", endpoint=endpoint, started_at=retrieved, retrieved_at=retrieved, status="OK"), dataset_id=d,
                              params={**P, **over})
    base = daily("SPY", date(2025, 12, 2), 100.0)
    bad = {"FETCH_SYMBOL": fetch(symbol="QQQ"), "FETCH_FEED": fetch(feed="sip"), "FETCH_ADJUSTMENT": fetch(adjustment="all"), "FETCH_TIMEFRAME": fetch(timeframe="1Min"),
           "FETCH_WINDOW": fetch(start="2025-12-03T05:00:00Z"), "FETCH_ENDPOINT": fetch(endpoint="/v2/stocks/QQQ/bars"),
           "FETCH_RETRIEVED_AT": fetch(retrieved=SAT - timedelta(hours=1)), "FETCH_PARAMS_INCOMPLETE": s.record_fetch(
               FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=d, params={"symbol": "SPY"})}
    for why, fid in bad.items():
        with pytest.raises(ScopeError, match=why):
            s.ingest(d, [base], fetch_id=fid)                                                          # QQQ / SIP / another window: never accepted
    assert s.ingest(d, [base], fetch_id=fetch()).inserted == 1
    a, _ = approve(tmp_path, req())
    bd = s.create_dataset(IG.dataset_spec(IG.plan(req(), SAT), a), approval=a)
    with pytest.raises(ScopeError, match="needs the fetch"):
        s.ingest(bd, [daily("SPY", date(2025, 12, 3), 1.0)])                                          # a BACKFILL row always names its fetch
    fid = s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=bd, params=P)
    assert s.ingest(bd, [daily("SPY", date(2025, 12, 3), 1.0)], fetch_id=fid).inserted == 1


def test_G03_F3_session_scope_hour_boundaries_and_the_delivered_row_cap_hold_at_every_entry_point(tmp_path, monkeypatch):
    d1 = date(2025, 12, 1)
    three = [jbar(ny(2025, 12, 1, 8, 0)), jbar(ny(2025, 12, 1, 10, 0)), jbar(ny(2025, 12, 1, 16, 30))]   # PRE, REG, POST
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    out = run(tmp_path, req(timeframe="1Min", start=d1, end=d1), clk, s, wire=bars_wire(clk, three))
    assert (out["status"], out["inserted"], out["excluded"]) == ("COMPLETED", 1, 2)                     # regular-hours approval: PRE / POST excluded
    assert [r["session"] for r in s.history("BAR_1MIN", symbol="SPY", feed="iex")] == ["REG"]
    assert sorted(m["reason"] for m in s.members(out["dataset_id"]) if m["disposition"] == "EXCLUDED") == ["OUTSIDE_APPROVED_SESSIONS:REGULAR"] * 2
    clk2 = Clock(SAT)
    s2 = new_store(tmp_path, clk2, "ext.db")
    ext = IG.BackfillRequest(("SPY",), d1, d1, "1Min", "iex", extended_hours=True)
    out2 = run(tmp_path, ext, clk2, s2, wire=bars_wire(clk2, three), aid="OPS-EXTENDED-1")
    assert (out2["status"], out2["inserted"], out2["excluded"]) == ("COMPLETED", 3, 0)                 # an extended approval keeps them
    h = s.create_dataset(spec(kind="BAR_1HOUR"))
    hours = [bar("BAR_1HOUR", ny(2025, 12, 1, hh, 0)) for hh in (9, 15, 16)] + [bar("BAR_1HOUR", ny(2025, 11, 28, hh, 0)) for hh in (12, 13)]
    r = s.ingest(h, hours)
    assert (r.inserted, r.excluded) == (3, 2)                                  # 09:00-10:00 overlaps 09:30; 16:00 and the early-close 13:00 do not
    assert [x["observed_at"] for x in s.history("BAR_1HOUR", symbol="SPY", feed="iex")] == [ts(ny(2025, 11, 28, 12, 0)), ts(ny(2025, 12, 1, 9, 0)),
                                                                                          ts(ny(2025, 12, 1, 15, 0))]
    he = s.create_dataset(dataclasses.replace(spec(kind="BAR_1HOUR"), request={"extended_hours": True}))
    r = s.ingest(he, [bar("BAR_1HOUR", ny(2025, 12, 2, hh, 0)) for hh in (3, 4, 16, 19)] + [bar("BAR_1HOUR", ny(2025, 11, 26, 16, 0))] +
                 [bar("BAR_1HOUR", ny(2025, 11, 28, hh, 0)) for hh in (16, 17)])
    assert (r.inserted, r.excluded) == (5, 2)                                  # 03:00 before 04:00; 17:00 after the early-close late session
    s3 = new_store(tmp_path, name="caps.db")
    a, _ = approve(tmp_path, req(), aid="OPS-CAPS-0002", max_rows=5, max_requests=1)               # exactly the plan's own estimate
    bd = s3.create_dataset(IG.dataset_spec(IG.plan(req(), SAT), a), approval=a)
    fid = s3.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=bd,
                          params=daily_params())
    with pytest.raises(RowCapError, match="APPROVAL_REQUEST_CAP"):
        s3.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=bd,
                        params=daily_params())                                                     # the approved request count, at the store
    assert s3.ingest(bd, [daily("SPY", d, 1.0) for d in sessions_2025(date(2025, 12, 1), 5)], fetch_id=fid).inserted == 5
    with pytest.raises(RowCapError, match="DELIVERED_ROW_CAP"):
        s3.record_problems(bd, [{"problem": "MALFORMED_ROW", "index": 0}], fetch_id=fid)             # the approved row count, at every entry point
    big, _ = approve(tmp_path, req(), aid="OPS-CAPS-BIG1", max_rows=10 ** 9)
    bd2 = new_store(tmp_path, name="capsbig.db")
    d2 = bd2.create_dataset(IG.dataset_spec(IG.plan(req(), SAT), big), approval=big)
    fid2 = bd2.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=d2,
                            params=daily_params())
    monkeypatch.setattr(ST, "MAX_DATASET_ROWS", 4)
    with pytest.raises(RowCapError):
        bd2.ingest(d2, [daily("SPY", d, 1.0) for d in sessions_2025(date(2025, 12, 1), 5)], fetch_id=fid2)   # never above MAX_DATASET_ROWS
    imp = new_store(tmp_path, name="imp.db")
    di = imp.create_dataset(spec())
    imp.ingest(di, [daily("SPY", d, 1.0) for d in sessions_2025(date(2025, 12, 1), 3)])
    with pytest.raises(RowCapError):
        imp.ingest(di, [daily("SPY", d, 1.0) for d in sessions_2025(date(2025, 11, 24), 2)])           # the whole batch refused ...
    assert len(imp.history("BAR_1DAY", symbol="SPY", feed="iex")) == 3                                 # ... nothing of it stored
    imp.record_problems(di, [{"problem": "X"}], fetch_id="f")
    with pytest.raises(RowCapError):
        imp.record_problems(di, [{"problem": "X"}], fetch_id="f")


def _drop_and_restore(db: Path, edits):
    c = sqlite3.connect(db)
    trig = {n: q for n, q in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    for n in trig:
        c.execute(f"DROP TRIGGER {n}")
    for q, args in edits:
        c.execute(q, args)
    for q in trig.values():
        c.execute(q)
    c.commit()
    c.close()


def test_G04_F4_integrity_is_reconciled_with_the_anchors_in_both_directions(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    did = run(tmp_path, req(), clk, s, archive=arc)["dataset_id"]
    v = s.verify_integrity(anchors=arc.anchors())
    assert (v["state"], v["ok"]) == ("VERIFIED", True)
    assert s.verify_integrity()["state"] == "UNVERIFIED"                                               # nothing external was checked
    v = s.verify_integrity(anchors={})
    assert (v["state"], v["unanchored_datasets"]) == ("UNVERIFIED", [did])                             # sealed but not anchored: pending anchor
    tamper = {
        "dataset deleted": [(f"DELETE FROM {t} WHERE dataset_id = ?", (did,)) for t in ("mi_dataset_members", "mi_observations", "mi_budget_events",
                                                                                       "mi_fetches", "mi_approval_uses", "mi_dataset_events", "mi_datasets")],
        "dataset row deleted": [("DELETE FROM mi_datasets WHERE dataset_id = ?", (did,))],
        "member deleted": [("DELETE FROM mi_dataset_members WHERE member_id = (SELECT MIN(member_id) FROM mi_dataset_members WHERE dataset_id = ?)", (did,))],
        "membership altered": [("UPDATE mi_dataset_members SET disposition = 'DUPLICATE' WHERE member_id = (SELECT MIN(member_id) FROM mi_dataset_members "
                                "WHERE dataset_id = ? AND disposition = 'INSERTED')", (did,))],
        "fetch altered": [("UPDATE mi_fetches SET params_json = '{}' WHERE dataset_id = ?", (did,))],
        "member delivery time altered": [("UPDATE mi_dataset_members SET retrieved_at = '2025-12-06T14:00:00.000000Z', available_at = "
                                          "'2025-12-06T14:00:00.000000Z' WHERE member_id = (SELECT MIN(member_id) FROM mi_dataset_members WHERE dataset_id = ?)", (did,))],
        "member reason altered": [("UPDATE mi_dataset_members SET reason = 'edited' WHERE member_id = (SELECT MAX(member_id) FROM mi_dataset_members "
                                   "WHERE dataset_id = ?)", (did,))],
        "budget event deleted": [("DELETE FROM mi_budget_events WHERE event_id = (SELECT MIN(event_id) FROM mi_budget_events WHERE dataset_id = ?)", (did,))],
        "event after the seal": [("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES (?, 'RUNNING', '2025-12-06T16:00:00.000000Z')", (did,))],
        "rows of a dataset that never existed": [("INSERT INTO mi_observations SELECT 'orphan', natural_key || '|orphan', revision, NULL, content_hash, "
                                                  "'ghost-dataset', NULL, kind, symbol, series_id, provider, feed, adjustment, observed_at, event_end, published_at, "
                                                  "retrieved_at, available_at, ingested_at, session, session_date, status, usable, freshness, age_s, source_url, "
                                                  "source_identifier, tier, quality_flags_json, backfilled, payload_json, envelope_version FROM mi_observations "
                                                  "WHERE dataset_id = ? LIMIT 1", (did,))],
    }
    for name, edits in tamper.items():
        db = _copy_db(s.path, tmp_path / f"g04_{name.replace(' ', '_')}.db")
        _drop_and_restore(db, edits)
        v = MarketIntelStore(db, clock=clk).verify_integrity(anchors=arc.anchors())
        assert v["state"] == "CORRUPT" and v["ok"] is False, (name, {k: v[k] for k in MarketIntelStore._CORRUPT_KEYS if v[k]})
    v = MarketIntelStore(tmp_path / "g04_dataset_deleted.db", clock=clk).verify_integrity(anchors=arc.anchors())
    assert v["missing_datasets"] == [did]                                                              # an anchored dataset that is gone
    v = MarketIntelStore(tmp_path / "g04_rows_of_a_dataset_that_never_existed.db", clock=clk).verify_integrity(anchors=arc.anchors())
    assert v["orphans"] == [{"table": "mi_observations", "rows": 1}] and v["missing_datasets"] == v["anchor_mismatches"] == []   # only the orphan check sees it
    forged = _copy_db(s.path, tmp_path / "g04_counts_forged.db")                                       # sealed counts edited AND the digest recomputed:
    c = sqlite3.connect(forged)
    c.row_factory = sqlite3.Row
    trig = {n: q for n, q in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    c.execute("DROP TRIGGER mi_dataset_events_no_update")
    e = c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? AND status = 'COMPLETED'", (did,)).fetchone()
    stated = {k: e[col] for k, col in MarketIntelStore._COUNT_COLUMNS.items()}
    stated["INSERTED"] += 1
    c.execute("UPDATE mi_dataset_events SET rows_inserted = ?, metadata_sha256 = ? WHERE event_id = ?",
              (stated["INSERTED"], MarketIntelStore._metadata_sha256(c, did, "COMPLETED", stated), e["event_id"]))
    c.execute(trig["mi_dataset_events_no_update"])
    c.commit()
    c.close()
    v = MarketIntelStore(forged, clock=clk).verify_integrity()
    assert (v["count_mismatches"], v["metadata_mismatches"], v["state"]) == ([did], [], "CORRUPT")       # only the recount from the members sees it
    v = MarketIntelStore(tmp_path / "never.db", clock=clk).verify_integrity(anchors=arc.anchors())
    assert (v["store"], v["state"], v["missing_datasets"]) == ("NOT_CREATED", "CORRUPT", [did])       # the whole database gone
    v0 = _copy_db(s.path, tmp_path / "g04_v0.db")
    c = sqlite3.connect(v0)
    c.execute("PRAGMA user_version = 0")
    c.close()
    with pytest.raises(MG.SchemaMismatch, match="user_version 0"):
        MarketIntelStore(v0, clock=clk).history("BAR_1DAY", symbol="SPY", feed="iex")                  # never read as an empty store
    v = MarketIntelStore(v0, clock=clk).verify_integrity(anchors=arc.anchors())
    assert (v["store"], v["state"]) == ("SCHEMA_ERROR", "CORRUPT")
    arc2 = RawArchive(tmp_path / "raw_conflict")
    shutil.copytree(arc.root, arc2.root)
    with (arc2.root / "anchors.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"dataset_id": did, "content_sha256": "0" * 64, "records_sha256": "0" * 64, "metadata_sha256": "0" * 64, "status": "COMPLETED"}) + "\n")
    v = s.verify_integrity(anchors=arc2.anchors())
    assert arc2.anchors()[did]["conflicting"] is True and v["anchor_mismatches"] == [did] and v["state"] == "CORRUPT"


def test_G05_F5_a_failed_seal_or_anchor_is_a_durable_pending_state_that_recovery_completes(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")

    def disk_failure(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    s.finish_dataset = disk_failure
    with pytest.raises(IG.FinalizationError) as e:
        run(tmp_path, req(), clk, s, archive=arc)
    did = e.value.dataset_id
    assert e.value.state == "PENDING_FINALIZATION" and s.dataset(did)["status"] == "RUNNING" and arc.anchors() == {}   # never claimed sealed
    v = s.verify_integrity(anchors=arc.anchors())
    assert (v["state"], v["unsealed_datasets"]) == ("UNVERIFIED", [did])
    with pytest.raises(StoreError, match="already used"):
        run(tmp_path, req(), clk, s, archive=arc)                                                     # replay protection survives the failure
    del s.finish_dataset                                                                               # the disk is back
    assert IG.recover_pending(s, arc, clock=clk) == {"sealed": [], "anchored": [], "left_running": [did], "refused": None}   # maybe live: untouched
    clk.tick(2 * 3600)
    rep_ = IG.recover_pending(s, arc, clock=clk)
    assert rep_["sealed"] == [{"dataset_id": did, "status": "PARTIAL"}] and rep_["anchored"] == [did]
    assert "RECOVERED_ABANDONED_RUN" in s.dataset(did)["events"][-1]["detail"] and s.verify_integrity(anchors=arc.anchors())["state"] == "VERIFIED"
    clk2 = Clock(SAT)
    s2, arc2 = new_store(tmp_path, clk2, "anchor.db"), RawArchive(tmp_path / "raw2")

    def anchor_failure(*a, **k):
        raise OSError("disk full")
    arc2.anchor = anchor_failure
    out = run(tmp_path, req(), clk2, s2, archive=arc2, aid="OPS-ANCHOR-01")
    assert (out["status"], out["finalization"], out["anchor_error"], out["anchor"]) == ("COMPLETED", "PENDING_ANCHOR", "OSError", None)
    v = s2.verify_integrity(anchors=arc2.anchors())
    assert (v["state"], v["unanchored_datasets"]) == ("UNVERIFIED", [out["dataset_id"]])
    del arc2.anchor
    assert IG.recover_pending(s2, arc2, clock=clk2)["anchored"] == [out["dataset_id"]]
    assert s2.verify_integrity(anchors=arc2.anchors())["state"] == "VERIFIED"
    clk3 = Clock(SAT)
    s3, arc3 = new_store(tmp_path, clk3, "corrupt.db"), RawArchive(tmp_path / "raw3")
    arc3.anchor = anchor_failure
    run(tmp_path, req(), clk3, s3, archive=arc3, aid="OPS-CORRUPT-1")
    del arc3.anchor
    _drop_and_restore(s3.path, [("UPDATE mi_observations SET available_at = '2025-12-01T21:00:00.000000Z', retrieved_at = '2025-12-01T21:00:00.000000Z' "
                                 "WHERE session_date = '2025-12-01'", ())])
    assert IG.recover_pending(s3, arc3, clock=clk3) == {"sealed": [], "anchored": [], "left_running": [], "refused": "CORRUPT"}
    assert arc3.anchors() == {}                                                                        # a corrupt store is never re-anchored


def test_G06_F6_a_sealed_dataset_takes_nothing_more_through_any_entry_point(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 1.0), daily("SPY", date(2025, 12, 2), 0.0, status="UNAVAILABLE")])
    seal = s.finish_dataset(did, "COMPLETED")
    anchors = {did: {k: seal[k] for k in ("content_sha256", "records_sha256", "metadata_sha256", "status")}}
    attempts = {"ingest": lambda: s.ingest(did, [daily("SPY", date(2025, 12, 3), 1.0)]),
                "ingest quarantine": lambda: s.ingest(did, [daily("QQQ", date(2025, 12, 3), 1.0)], on_reject="quarantine"),
                "ingest absence": lambda: s.ingest(did, [daily("SPY", date(2025, 12, 4), 0.0, status="UNAVAILABLE")]),
                "record_problems": lambda: s.record_problems(did, [{"problem": "LATE"}], fetch_id="x"),
                "record_fetch": lambda: s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/x", started_at=SAT, status="ERROR"), dataset_id=did),
                "record_budget_event": lambda: s.record_budget_event("ALPACA_BACKFILL", "ALLOW", dataset_id=did),
                "finish again": lambda: s.finish_dataset(did, "FAILED")}
    for name, f in attempts.items():
        with pytest.raises(SealedError):
            f()
    now = ts(SAT)
    raw = {"mi_dataset_members": ("INSERT INTO mi_dataset_members (dataset_id, natural_key, disposition, at) VALUES (?, 'k', 'REJECTED', ?)", (did, now)),
           "mi_dataset_events": ("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES (?, 'RUNNING', ?)", (did, now)),
           "mi_fetches": ("INSERT INTO mi_fetches (fetch_id, dataset_id, provider, endpoint, params_json, started_at, status, ingested_at) "
                          "VALUES ('late', ?, 'ALPACA', '/x', '{}', ?, 'OK', ?)", (did, now, now)),
           "mi_budget_events": ("INSERT INTO mi_budget_events (at, provider, event, dataset_id) VALUES (?, 'ALPACA', 'ALLOW', ?)", (now, did)),
           "mi_approval_uses": ("INSERT INTO mi_approval_uses (approval_id, plan_sha256, approval_file_sha256, dataset_id, used_at, max_requests, max_rows) "
                                "VALUES ('OPS-LATE-01', 'h', 'f', ?, ?, 1, 1)", (did, now)),
           "mi_observations": ("INSERT INTO mi_observations SELECT 'late', natural_key || 'x', revision, supersedes_obs_id, content_hash, dataset_id, fetch_id, "
                               "kind, symbol, series_id, provider, feed, adjustment, observed_at, event_end, published_at, retrieved_at, available_at, ingested_at, "
                               "session, session_date, status, usable, freshness, age_s, source_url, source_identifier, tier, quality_flags_json, backfilled, "
                               "payload_json, envelope_version FROM mi_observations WHERE dataset_id = ?", (did,)),
           "mi_absences": ("INSERT INTO mi_absences SELECT 'late', natural_key || 'x', content_hash, dataset_id, fetch_id, kind, symbol, series_id, provider, feed, "
                           "adjustment, observed_at, published_at, retrieved_at, available_at, ingested_at, session, session_date, status, source_url, "
                           "source_identifier, tier, quality_flags_json, backfilled, payload_json, envelope_version FROM mi_absences WHERE dataset_id = ?", (did,))}
    c = sqlite3.connect(s.path)
    for table, (q, args) in raw.items():
        with pytest.raises(sqlite3.DatabaseError, match="sealed"):
            c.execute(q, args)                                                                         # the schema refuses raw SQL too
    c.close()
    s.record_quality_audit({"dataset_id": did, "scope": {"dataset": did}, "check": "later_review", "result": "PASS", "codes": []})   # an evaluation, allowed
    assert s.verify_integrity(anchors=anchors)["state"] == "VERIFIED" and [e["status"] for e in s.dataset(did)["events"]] == ["RUNNING", "COMPLETED"]


def test_G07_F7_re_delivery_keeps_its_provenance_and_status_follows_the_70_freshness_policy(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    good = daily("SPY", date(2025, 12, 5), 100.0)                                                       # Friday: the last completed session at SAT
    s.ingest(s.create_dataset(spec()), [good])
    clk.tick(3600)
    s.ingest(s.create_dataset(spec()), [daily("SPY", date(2025, 12, 5), 0.0, status="UNAVAILABLE", retrieved=clk())])
    st = s.latest_status("BAR_1DAY", at=clk(), symbol="SPY", feed="iex")
    assert (st["latest_record"]["record_type"], st["status"], st["reason"]) == ("ABSENCE", "UNAVAILABLE", "LATEST_RECORD_IS_ABSENCE")
    clk.tick(3600)
    d3 = s.create_dataset(spec())
    r = s.ingest(d3, [daily("SPY", date(2025, 12, 5), 100.0, retrieved=clk())])                       # the identical value, delivered again
    m = s.members(d3)[0]
    assert r.duplicates == 1 and (m["disposition"], m["retrieved_at"], m["available_at"]) == ("DUPLICATE", ts(clk()), ts(clk()))   # its own delivery
    st = s.latest_status("BAR_1DAY", at=clk(), symbol="SPY", feed="iex")
    assert (st["latest_record"]["record_type"], st["latest_record"]["last_delivered_at"], st["status"], st["freshness"]["freshness"]) == (
        "VALUE", ts(clk()), "AVAILABLE", "FRESH")                                                       # value -> absence -> value: current again
    prov = s.provenance(st["latest_usable"]["obs_id"])
    assert [x["disposition"] for x in prov["deliveries"]] == ["INSERTED", "DUPLICATE"]
    tue = ny(2025, 12, 9, 19, 0)
    st = s.latest_status("BAR_1DAY", at=tue, symbol="SPY", feed="iex")
    assert (st["status"], st["reason"], st["latest_usable"]["session_date"]) == ("STALE", "SESSION_MISSING", "2025-12-05")   # history, not current
    assert st["freshness"] == FR.assess_daily_bar(date(2025, 12, 5), tue)                               # exactly the Stage 7.0 policy
    mclk = Clock(ny(2025, 12, 2, 10, 1))
    ms = new_store(tmp_path, mclk, "minute.db")
    ms.ingest(ms.create_dataset(spec(job="FIXTURE", kind="BAR_1MIN")), [bar("BAR_1MIN", ny(2025, 12, 2, 10, 0), retrieved=mclk(), backfilled=False)])
    grade = lambda t: (lambda x: (x["status"], x["freshness"]["freshness"]))(ms.latest_status("BAR_1MIN", at=t, symbol="SPY", feed="iex"))   # noqa: E731
    assert [grade(ny(2025, 12, 2, 10, 1) + timedelta(seconds=sec)) for sec in (90, 180, 360)] == [("AVAILABLE", "FRESH"), ("AVAILABLE", "AGING"), ("STALE", "STALE")]


def test_G08_F8_quality_reports_keep_valid_invalid_and_missing_sessions_apart(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    days = sessions_2025(date(2025, 11, 3), 5)
    rows = [daily("SPY", days[0], 100.0),
            daily("SPY", days[1], 100.0).model_copy(update={"payload": {"open": 100, "high": 90, "low": 89, "close": 95, "volume": 1}}),   # HIGH_BELOW_BODY
            daily("SPY", days[2], 100.0).model_copy(update={"payload": {"open": 100, "high": 101, "low": -1, "close": 100, "volume": 1}}),  # NON_POSITIVE_PRICE
            daily("SPY", days[4], 100.0)]                                                                                                 # days[3]: missing
    s.ingest(did, rows)
    q = s.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=days[0], end=days[-1])
    assert q["result"] == "BLOCKED" and {"HIGH_BELOW_BODY", "NON_POSITIVE_PRICE", "SESSION_HOLE"} <= set(q["codes"])
    assert q["present_sessions"] == 2 and q["missing_sessions"] == [days[3].isoformat()] and q["unusable_rows"] == 2
    assert q["invalid_sessions"] == [{"session_date": days[1].isoformat(), "codes": ["HIGH_BELOW_BODY"]},
                                     {"session_date": days[2].isoformat(), "codes": ["NON_POSITIVE_PRICE"]}]
    assert q["holes"] == [{"first_missing": days[1].isoformat(), "last_missing": days[3].isoformat(), "market_days": 3}]   # no valid bar there
    one = new_store(tmp_path, name="one.db")                                                          # one invalid session, no hole:
    one.ingest(one.create_dataset(spec()), [rows[0], rows[1], daily("SPY", days[2], 100.0), daily("SPY", days[3], 100.0), rows[3]])
    q = one.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=days[0], end=days[-1])
    assert (q["result"], q["holes"], q["missing_sessions"], q["codes"]) == ("BLOCKED", [], [], ["HIGH_BELOW_BODY"])   # the blocking code alone blocks
    clean = new_store(tmp_path, name="clean.db")
    clean.ingest(clean.create_dataset(spec()), [daily("SPY", d, 100.0 + i * 0.01, flags=("BACKFILLED",)) for i, d in enumerate(days)])
    q = clean.quality_report("BAR_1DAY", symbol="SPY", feed="iex", start=days[0], end=days[-1])
    assert (q["result"], q["codes"], q["invalid_sessions"], q["missing_sessions"]) == ("PASS", [], [], [])   # BACKFILLED is provenance, not a warning


def test_G09_F9_a_bar_is_never_a_completed_fact_before_its_event_end(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    for kind, t0 in (("BAR_1MIN", ny(2025, 12, 2, 10, 0)), ("BAR_1HOUR", ny(2025, 12, 2, 11, 0)), ("BAR_1DAY", ny(2025, 12, 2, 0, 0))):
        end = ST.event_end(bar(kind, t0))
        did = s.create_dataset(spec(job="FIXTURE", kind=kind))
        clk.t = end - timedelta(seconds=3)
        with pytest.raises(ChronologyError, match="BAR_NOT_COMPLETE_AT_RETRIEVAL"):
            s.ingest(did, [bar(kind, t0, retrieved=clk(), backfilled=False)])                            # 3 s early: beyond the skew, refused
        clk.t = end - timedelta(seconds=1)
        s.ingest(did, [bar(kind, t0, retrieved=clk(), backfilled=False)])                                # 1 s early: admitted within the skew ...
        for at, seen in ((end - timedelta(microseconds=1), False), (end, True)):                         # ... but never read before it ended
            got = [s.history(kind, symbol="SPY", feed="iex", as_of=at) != [], s.latest_usable(kind, at=at, symbol="SPY", feed="iex") is not None,
                   s.latest_status(kind, at=at, symbol="SPY", feed="iex")["latest_record"] is not None,
                   s.snapshot(kind, ["SPY"], at=at, feed="iex")["SPY"]["latest_usable"] is not None]
            if kind == "BAR_1DAY":
                got.append(s.quality_report(kind, symbol="SPY", feed="iex", start=date(2025, 12, 2), end=date(2025, 12, 2), as_of=at)["present_sessions"] == 1)
            assert got == [seen] * len(got), (kind, at)


def test_G10_F10_only_a_later_attempt_recovers_a_failure(tmp_path):
    s = new_store(tmp_path)
    P = daily_params()
    for seq, want in ((["OK", "ERROR"], 1), (["ERROR", "OK"], 0), (["ERROR", "ERROR", "OK"], 0), (["OK", "ERROR", "ERROR"], 2), (["ERROR", "OK", "ERROR"], 1)):
        did = s.create_dataset(spec())
        for status in seq:                                                                             # identical timestamps: only the order counts
            s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT if status == "OK" else None,
                                       status=status), dataset_id=did, params=P)
        assert s.finish_dataset(did, "PARTIAL")["unrecovered_failures"] == want, seq
    did = s.create_dataset(spec())
    s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT + timedelta(seconds=10), retrieved_at=SAT + timedelta(seconds=10),
                               status="OK"), dataset_id=did, params=P)                                  # recorded first, with a LATER (skewed) clock
    s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, status="TIMEOUT"), dataset_id=did, params=P)
    assert s.finish_dataset(did, "PARTIAL")["unrecovered_failures"] == 1                              # the durable attempt order wins over clocks


def test_G11_F11_malformed_http_200_bodies_are_invalid_responses_never_empty(tmp_path):
    ok = {"symbol": "SPY", "bars": [], "next_page_token": None}
    good_bar = jbar(ny(2025, 12, 1, 0, 0))
    pages = {"ERROR_ENVELOPE": [{"message": "upstream failed"}, {"code": 40010001, "message": "invalid"}, {**ok, "message": "partial"}],
             "UNEXPECTED_SHAPE": [[], "text", {"bars": []}, {"symbol": "SPY", "bars": []}, {**ok, "extra": 1}, {**ok, "bars": "x"}, {**ok, "bars": {}}],
             "SYMBOL_MISMATCH": [{**ok, "symbol": None}, {**ok, "symbol": "QQQ"}],
             "BAD_PAGE_TOKEN": [{**ok, "next_page_token": 5}, {**ok, "next_page_token": ""}, {**ok, "next_page_token": "a b"}, {**ok, "next_page_token": "x" * 513}],
             "PAGE_TOO_LARGE": [{**ok, "bars": [good_bar] * (AB.PAGE_LIMIT + 1)}]}
    parse = lambda doc: AB.parse_page(json.dumps(doc).encode(), symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT)   # noqa: E731
    for code, docs in pages.items():
        for doc in docs:
            obs, token, problems = parse(doc)
            assert (obs, token, problems[0]["problem"]) == ([], None, code), (code, str(doc)[:80])
    for doc in (ok, {**ok, "bars": None}, {**ok, "currency": "USD"}):
        assert parse(doc) == ([], None, [])                                                             # a valid empty page
    rows = [{**good_bar, "v": True}, {**good_bar, "c": "10.0"}, {**good_bar, "x": 1}, {k: v for k, v in good_bar.items() if k != "t"}, {**good_bar, "t": 123},
            {**good_bar, "n": "5"}]
    assert [p["problem"] for p in parse({**ok, "bars": rows})[2]] == ["MALFORMED_ROW"] * len(rows)
    for i, doc in enumerate([d for docs in pages.values() for d in docs]):
        clk = Clock(SAT)
        out = run(tmp_path, req(start=date(2025, 12, 1), end=date(2025, 12, 1)), clk, new_store(tmp_path, clk, f"g11_{i}.db"),
                  wire=lambda p, d=doc, c=clk: (c.tick(1), (200, {}, json.dumps(d).encode()))[1], archive=RawArchive(tmp_path / f"raw_g11_{i}"), aid=f"OPS-G11-{i:03d}")
        assert out["status"] == "FAILED" and out["failed_fetches"] == 1, (i, str(doc)[:80])               # never a legitimate EMPTY
    clk = Clock(SAT)
    out = run(tmp_path, req(start=date(2025, 12, 1), end=date(2025, 12, 1)), clk, new_store(tmp_path, clk, "g11_ok.db"), wire=bars_wire(clk, []), aid="OPS-G11-OK1")
    assert out["status"] == "EMPTY"


def test_G12_F12_the_versioned_2026_2027_late_trading_override():
    import autorun.calendar as AC
    from autorun import session as SES
    assert HIST.LATE_TRADING_EARLY_CLOSES == AC.EARLY_CLOSES and HIST.LATE_TRADING_OVERRIDE_VERSION == "nyse_late_trading_2026_2027_v1"
    for d in sorted(AC.EARLY_CLOSES):
        at = lambda hh, mm, d=d: ny(d.year, d.month, d.day, hh, mm)                                     # noqa: E731
        assert [CAL.session_of(at(hh, mm)) for hh, mm in ((12, 59), (13, 0), (16, 59), (17, 0), (19, 59))] == ["REG", "POST", "POST", "CLOSED", "CLOSED"]
        assert SES.session_of(at(17, 30)) == "AFTER_HOURS"                                              # the frozen 6.10A classifier is untouched
        assert CAL.session(at(17, 30))["late_trading_override"] == HIST.LATE_TRADING_OVERRIDE_VERSION and "late_trading_override" not in CAL.session(at(16, 30))
        assert (CAL.post_market_end_minute(d), CAL.regular_minutes(d)) == (17 * 60, 210)
        assert FR.window_open("EXTENDED", at(16, 30)) is True and FR.window_open("EXTENDED", at(17, 30)) is False
    assert CAL.session_of(ny(2026, 11, 30, 19, 59)) == "POST" and CAL.post_market_end_minute(date(2026, 11, 30)) == 20 * 60
    o = Observation(kind="BAR_1DAY", symbol="SPY", provider="ALPACA", feed="iex", adjustment="raw", observed_at=ny(2026, 11, 27, 0, 0),
                    retrieved_at=ny(2026, 11, 30, 10, 0), available_at=ny(2026, 11, 30, 10, 0), session="REG", session_date=date(2026, 11, 27), status="AVAILABLE",
                    payload={"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    assert ST.event_end(o) == datetime(2026, 11, 27, 22, 0, tzinfo=UTC)                                 # 17:00 New York: event_end follows the calendar
    now = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    est = lambda tf: IG.plan(IG.BackfillRequest(("SPY",), date(2026, 11, 27), date(2026, 11, 27), tf, "iex", extended_hours=True), now)["estimate"]   # noqa: E731
    assert (est("1Min")["rows_per_symbol"], est("1Hour")["rows_per_symbol"]) == (13 * 60, 13)       # 04:00-17:00


# ------------------------------------------------------------------------------------------------------------ invariants (X)
def test_X01_no_backfill_dataset_exists_without_verified_operator_authority(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    rq = req()
    a, _ = approve(tmp_path, rq)
    good = IG.dataset_spec(IG.plan(rq, SAT), a)
    elsewhere = MarketIntelStore(s.path, clock=clk, approvals_root=tmp_path / "elsewhere")
    attempts = [lambda: s.create_dataset(good), lambda: s.create_dataset(good, approval=SimpleNamespace(**{f.name: getattr(a, f.name) for f in dataclasses.fields(a)})),
                lambda: s.create_dataset(dataclasses.replace(good, request={}), approval=a), lambda: elsewhere.create_dataset(good, approval=a),
                lambda: s.create_dataset(dataclasses.replace(good, symbols=["SPY", "QQQ"]), approval=a),
                lambda: IG.run_backfill(rq, approval=dataclasses.replace(a, approval_id="OPS-OTHER-01"), fetch_fn=Wire(clk), store=s,
                                        archive=RawArchive(tmp_path / "raw"), clock=clk, sleep_fn=lambda x: None),
                lambda: IG.run_backfill(rq, approval=a, fetch_fn=Wire(clk), store=elsewhere, archive=RawArchive(tmp_path / "raw"), clock=clk, sleep_fn=lambda x: None)]
    for f in attempts:
        with pytest.raises((PermissionError, StoreError)):
            f()
    assert not s.exists()
    s.create_dataset(good, approval=a)
    c = sqlite3.connect(s.path)
    assert c.execute("SELECT COUNT(*) FROM mi_datasets WHERE job_kind = 'BACKFILL'").fetchone()[0] == c.execute("SELECT COUNT(*) FROM mi_approval_uses").fetchone()[0] == 1
    c.close()


def test_X02_no_stored_observation_lies_outside_its_dataset_fetch_or_session_scope(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec(kind="BAR_1MIN", symbols=("SPY",), start=date(2025, 12, 1), end=date(2025, 12, 5)))
    P = AB.request_params("SPY", "1Min", ny(2025, 12, 1, 0, 0), ny(2025, 12, 6, 0, 0), "iex", "raw")
    fid = s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, retrieved_at=SAT, status="OK"), dataset_id=did, params=P)
    t = ny(2025, 12, 2, 10, 0)
    good = bar("BAR_1MIN", t)
    variants = [good.model_copy(update=u) for u in (
        {"symbol": "QQQ"}, {"feed": "sip"}, {"adjustment": "all"}, {"provider": "SEC"}, {"kind": "BAR_1HOUR"}, {"dataset_id": "x"}, {"backfilled": False},
        {"session_date": date(2025, 12, 3)}, {"session": "POST"}, {"observed_at": t + timedelta(seconds=7)}, {"observed_at": ny(2025, 12, 8, 10, 0)},
        {"observed_at": ny(2025, 11, 28, 10, 0), "session_date": date(2025, 11, 28)}, {"retrieved_at": SAT - timedelta(minutes=1), "available_at": SAT - timedelta(minutes=1)},
        {"observed_at": ny(2025, 12, 2, 8, 0), "session": "PRE"}, {"observed_at": ny(2025, 12, 2, 17, 0), "session": "POST"},
        {"observed_at": ny(2025, 12, 6, 10, 0), "session": "CLOSED", "session_date": date(2025, 12, 6)}, {"payload": {}},
        {"payload": {**good.payload, "close": True}}, {"payload": {**good.payload, "timeframe": "1Day"}}, {"content_hash": "0" * 64})]
    for v in variants:
        try:
            s.ingest(did, [v], fetch_id=fid)
        except StoreError:
            pass
        s.ingest(did, [v], fetch_id=fid, on_reject="quarantine")
    s.ingest(did, [good], fetch_id=fid)
    c = sqlite3.connect(s.path)
    c.row_factory = sqlite3.Row
    stored = c.execute("SELECT * FROM mi_observations").fetchall()
    fetch = c.execute("SELECT * FROM mi_fetches WHERE fetch_id = ?", (fid,)).fetchone()
    c.close()
    assert len(stored) == 1                                                                            # every variant was refused, quarantined or excluded
    for r in stored:
        sess = CAL.session(parse_ts(r["observed_at"]))
        assert (r["symbol"], r["feed"], r["adjustment"], r["kind"], r["fetch_id"], r["retrieved_at"]) == ("SPY", "iex", "raw", "BAR_1MIN", fid, fetch["retrieved_at"])
        assert (r["session"], r["session_date"]) == (sess["session"], sess["session_date"].isoformat()) and sess["session"] == "REG"
        assert "2025-12-01" <= r["session_date"] <= "2025-12-05" and parse_ts(r["observed_at"]).second == 0


def test_X03_no_completed_dataset_changes_after_its_seal(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    did = run(tmp_path, req(), clk, s, archive=arc)["dataset_id"]
    tables = ("mi_dataset_members", "mi_dataset_events", "mi_fetches", "mi_budget_events", "mi_approval_uses", "mi_observations", "mi_absences")

    def counts():
        c = sqlite3.connect(s.path)
        try:
            return {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE dataset_id = ?", (did,)).fetchone()[0] for t in tables}
        finally:
            c.close()
    before = counts()
    fid = s.dataset(did) and s.members(did)[0]["fetch_id"]
    writes = [lambda: s.ingest(did, [daily("SPY", date(2025, 12, 1), 9.0)], fetch_id=fid), lambda: s.ingest(did, [], fetch_id=fid),
              lambda: s.ingest(did, [daily("SPY", date(2025, 12, 1), 9.0)], fetch_id=fid, on_reject="quarantine"),
              lambda: s.record_problems(did, [{"problem": "LATE"}], fetch_id=fid), lambda: s.record_budget_event("ALPACA_BACKFILL", "RESPONSE", dataset_id=did),
              lambda: s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v2/stocks/SPY/bars", started_at=SAT, status="OK", retrieved_at=SAT), dataset_id=did),
              lambda: s.finish_dataset(did, "PARTIAL")]
    for w in writes:
        with pytest.raises(SealedError):
            w()
    assert counts() == before and s.verify_integrity(anchors=arc.anchors())["state"] == "VERIFIED"


def test_X04_no_missing_or_altered_anchored_dataset_is_ever_verified(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    did = run(tmp_path, req(), clk, s, archive=arc)["dataset_id"]
    sweeps = {"no anchors given": lambda: s.verify_integrity(), "anchors file emptied": lambda: s.verify_integrity(anchors={}),
              "database gone": lambda: MarketIntelStore(tmp_path / "gone.db", clock=clk).verify_integrity(anchors=arc.anchors())}
    for t in ("mi_dataset_members", "mi_observations", "mi_fetches", "mi_dataset_events", "mi_datasets", "mi_approval_uses"):
        db = _copy_db(s.path, tmp_path / f"x04_{t}.db")
        _drop_and_restore(db, [(f"DELETE FROM {t} WHERE rowid = (SELECT MIN(rowid) FROM {t} WHERE dataset_id = ?)", (did,))])
        sweeps[f"one row of {t} deleted"] = lambda db=db: MarketIntelStore(db, clock=clk).verify_integrity(anchors=arc.anchors())
    v0 = _copy_db(s.path, tmp_path / "x04_v0.db")
    c = sqlite3.connect(v0)
    c.execute("PRAGMA user_version = 0")
    c.close()
    sweeps["user_version reset"] = lambda: MarketIntelStore(v0, clock=clk).verify_integrity(anchors=arc.anchors())
    for name, f in sweeps.items():
        v = f()
        assert v["state"] != "VERIFIED" and v["ok"] is False, name
        if name not in ("no anchors given", "anchors file emptied"):
            assert v["state"] == "CORRUPT", name


def test_X05_no_incomplete_bar_is_exposed_by_any_point_in_time_read(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    for kind, t0 in (("BAR_1MIN", ny(2025, 12, 3, 15, 59)), ("BAR_1HOUR", ny(2025, 12, 3, 15, 0)), ("BAR_1DAY", ny(2025, 12, 3, 0, 0))):
        end = ST.event_end(bar(kind, t0))
        clk.t = end - timedelta(seconds=2)
        s.ingest(s.create_dataset(spec(job="FIXTURE", kind=kind)), [bar(kind, t0, retrieved=clk(), backfilled=False)])
        for dt in (-2.0, -1.0, -0.000001, 0.0, 0.5):
            at = end + timedelta(seconds=dt)
            visible = {s.history(kind, symbol="SPY", feed="iex", as_of=at) != [], s.latest_usable(kind, at=at, symbol="SPY", feed="iex") is not None,
                       s.latest_status(kind, at=at, symbol="SPY", feed="iex")["latest_usable"] is not None,
                       s.snapshot(kind, ["SPY"], at=at, feed="iex")["SPY"]["latest_record"] is not None}
            assert visible == {at >= end}, (kind, dt)


def test_X06_no_malformed_response_is_ever_classified_empty(tmp_path):
    ok = {"symbol": "SPY", "bars": [], "next_page_token": None}
    bodies = [b"", b"null", b"[]", b"{}", b'{"message": "x"}', json.dumps({**ok, "symbol": "QQQ"}).encode(), json.dumps({**ok, "next_page_token": 1}).encode(),
              json.dumps({"bars": [], "next_page_token": None}).encode(), json.dumps({**ok, "bars": "[]"}).encode(), b"\xff\xfe", json.dumps({**ok, "x": 0}).encode()]
    for i, body in enumerate(bodies):
        clk = Clock(SAT)
        out = run(tmp_path, req(start=date(2025, 12, 1), end=date(2025, 12, 1)), clk, new_store(tmp_path, clk, f"x06_{i}.db"),
                  wire=lambda p, b=body, c=clk: (c.tick(1), (200, {}, b))[1], archive=RawArchive(tmp_path / f"raw_x06_{i}"), aid=f"OPS-X06-{i:03d}")
        assert out["status"] != "EMPTY" and out["status"] == "FAILED", (i, body[:40])


def test_X07_no_dataset_ever_holds_more_delivered_rows_than_its_cap(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    rq = req()                                                                                         # 5 sessions: rows_delivered_max = 5
    a, _ = approve(tmp_path, rq, aid="OPS-X07-0001", max_rows=5)
    flood = [jbar(ny(d.year, d.month, d.day, 0, 0)) for d in sessions_2025(date(2025, 12, 1), 5)] + [jbar(ny(2025, 12, 8, 0, 0))] * 3   # + extra bars
    out = IG.run_backfill(rq, approval=a, fetch_fn=bars_wire(clk, flood), store=s, archive=RawArchive(tmp_path / "raw"), clock=clk, sleep_fn=lambda x: None)
    assert (out["status"], out["stop"]) == ("PARTIAL", "APPROVAL_ROW_CAP") and len(s.members(out["dataset_id"])) <= 5
    clk2 = Clock(SAT)
    s2 = new_store(tmp_path, clk2, "pages.db")
    a2, _ = approve(tmp_path, rq, aid="OPS-X07-0002", max_requests=1)

    def endless(params):                                                                               # a provider that pages forever
        clk2.tick(1)
        n = int(params.get("page_token") or "0")
        return 200, {}, json.dumps({"symbol": "SPY", "bars": [], "next_page_token": f"p{n + 1}"}).encode()
    out = IG.run_backfill(rq, approval=a2, fetch_fn=lambda p: endless({**p, "page_token": (p.get("page_token") or "p0")[1:]}), store=s2,
                          archive=RawArchive(tmp_path / "raw2"), clock=clk2, sleep_fn=lambda x: None)
    assert out["stop"] == "APPROVAL_REQUEST_CAP" and s2.status_counts()["tables"]["mi_fetches"] == 1
    for st in (s, s2):
        c = sqlite3.connect(st.path)
        for did, cap in c.execute("SELECT dataset_id, max_rows FROM mi_approval_uses").fetchall():
            assert c.execute("SELECT COUNT(*) FROM mi_dataset_members WHERE dataset_id = ?", (did,)).fetchone()[0] <= min(cap, ST.MAX_DATASET_ROWS)
        c.close()


def test_X08_no_stale_value_is_ever_labelled_current(tmp_path):
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    s.ingest(s.create_dataset(spec()), [daily("SPY", date(2025, 12, 5), 100.0)])
    t = SAT
    while t < ny(2025, 12, 11, 23, 0):
        st = s.latest_status("BAR_1DAY", at=t, symbol="SPY", feed="iex")
        policy = FR.assess_daily_bar(date(2025, 12, 5), t)
        assert st["status"] == policy["status"] and (st["status"] == "AVAILABLE") == (policy["usable"] is True), t
        t += timedelta(hours=3)
    assert s.latest_status("BAR_1DAY", at=ny(2025, 12, 8, 18, 0), symbol="SPY", feed="iex")["status"] == "STALE"   # Monday 18:00: Monday's bar expected


# ============================================================================================================ isolation (S)
PURE_70 = ("__init__.py", "budget.py", "calendar.py", "freshness.py", "models.py", "settings.py", "validate.py")   # their own test: 7.0 I01
NEW_MODULES = ("calendar_history.py", "migrations.py", "store.py", "archive.py", "ingest.py", "approvals.py", "sources/__init__.py", "sources/alpaca_bars.py")
ALLOWED_71 = {"__future__", "argparse", "gzip", "hashlib", "json", "math", "re", "sqlite3", "sys", "uuid", "contextlib", "dataclasses", "datetime", "pathlib",
              "typing", "zoneinfo", "collections", "collections.abc", "email.utils", "urllib.parse", "pydantic", "config", "api.validation",
              "autorun", "autorun.calendar", "autorun.session", "setups.market_hours", "market_intel", "market_intel.calendar", "market_intel.calendar_history",
              "market_intel.validate", "market_intel.models", "market_intel.migrations", "market_intel.budget", "market_intel.settings",
              "market_intel.sources", "market_intel.store", "market_intel.archive", "market_intel.approvals", "market_intel.sources.alpaca_bars"}


def test_S01_the_new_modules_import_nothing_that_can_reach_a_network_a_broker_or_a_model():
    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}
    assert present == set(PURE_70) | set(NEW_MODULES), sorted(present ^ (set(PURE_70) | set(NEW_MODULES)))   # a new module must be declared here
    for rel in sorted(present):
        tree = ast.parse((ROOT / "market_intel" / rel).read_text(encoding="utf-8"))
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                                    for a in n.names}
        assert names <= ALLOWED_71, (rel, sorted(names - ALLOWED_71))
        calls = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, (ast.Name, ast.Attribute))}
        assert not calls & {"urlopen", "create_connection", "get_data_client", "Request", "post", "submit_order", "Popen", "system"}, rel
        src = (ROOT / "market_intel" / rel).read_text(encoding="utf-8")
        for word in ("http.client", "urllib.request", "alpaca.data", "alpaca.trading", "anthropic", "os.environ"):
            assert word not in src, (rel, word)
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",
             "scratchpad/")) and ("market_intel" in p.read_text(encoding="utf-8", errors="replace"))]
    assert users == []                                                                                   # no live path reads the store
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_"))]                         # MARKET_INTEL_MODE stays an OFF default


PINNED_V1_DDL = "f9c32726d642465c8fa579175051a412b43b44b0cf81addac9b1e99b034d72b2"   # the v1 schema (re-pinned by the PR #42 remediation, unmerged, no data); a change is a new migration


# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_71}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_71} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


APP_EDITS_71 = {".github/ci/pytest_groups.json", ".gitignore", "DESIGN_70_MARKET_INTEL_CONTRACTS.md", "market_intel/__init__.py", "market_intel/calendar.py"}
FROZEN_DIRS_71 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                  "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                  "database")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed():
    assert FL.BASE_71 == "4e2f115dd9826647c38474506984f89546e3d9e0" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_71}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_71).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_71).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_71 and {p for p in changed if p in in_base} == FL.EDITED_71
    assert FL.NEW_71 == {"DESIGN_71_MARKET_INTEL_STORE.md", "tests/test_market_intel_store_71.py"} | {f"market_intel/{n}" for n in (
        "approvals.py", "archive.py", "calendar_history.py", "ingest.py", "migrations.py", "sources/__init__.py", "sources/alpaca_bars.py", "store.py")}
    assert {p for p in FL.EDITED_71 if not p.startswith("tests/")} == APP_EDITS_71
    assert set(FL.SEMANTIC_71) == {"market_intel/calendar.py"} and FL.INSTRUMENTATION_71 == {}
    for d in FROZEN_DIRS_71:
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert now_text(rel) == at_base(rel), rel                               # no trading, safety, startup, API, DB, UI or harness file changed
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py"):
        assert now_text(rel) == at_base(rel), rel
    for rel in sorted(FL.NEW_71 | FL.EDITED_71):
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
    from market_intel.settings import settings as mi_settings
    assert mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_"))]
    import setups as SU
    assert (SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.EVIDENCE_MAX_AGE_DAYS) == (40, 60, 30)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_71.items():
        text = now_text(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_71) == FL.EDITED_71 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.1 (market intelligence store")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_542 + NEW_70 + NEW_71 plus this ledger.", " + NEW_542 + NEW_70 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_71 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_71: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(FL.LEDGER) - set(old) == {".gitignore"}                                     # one new key: the store's ignore lines (read as on main earlier)
    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_71 & set(old)
    gi_base, gi_now = at_base(".gitignore"), now_text(".gitignore")
    assert gi_now.startswith(gi_base) and gi_now[len(gi_base):] == "data/market_intel.db\ndata/market_intel.db-wal\ndata/market_intel.db-shm\n"
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_71 = {
    'DESIGN_70_MARKET_INTEL_CONTRACTS.md': [
        ('\nOne fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.\n',
         '\n**Stage 7.1 implemented the 2025 step** (`market_intel/calendar_history.py`, `DESIGN_71` section 5). It was verified against the NYSE\n2025 calendar release, the NYSE notice of the 2025-01-09 closure, and the archived IEX daily bars of 82 symbols (250 sessions, the\n11 closures exactly). 2024 and earlier, and 2028 onward, remain `NOT_COVERED`.\n\nOne fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.\n'),
        ('One fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.\n\n',
         'One fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.\n\n**Stage 7.1 resolved that fact, as the one declared exception to the single-source rule** (operator-approved 2026-10-31; `DESIGN_71`\nsection 5). The NYSE release footnotes every 2025-2027 early-close date: the late trading sessions "will close at 5:00 p.m." So\n`market_intel` ends POST at 17:00 New York time on those dates. For 2025 the history table does this. For 2026-2027 a versioned,\n`market_intel`-owned override (`nyse_late_trading_2026_2027_v1`) turns the 6.10A classifier\'s AFTER_HOURS into CLOSED from 17:00 on\nexactly 2026-11-27, 2026-12-24 and 2027-11-26. `autorun/calendar.py`, the 6.10A classifier and the executor are unchanged, and every\nholiday and 13:00 close still comes from them.\n\n'),
    ],
    'market_intel/__init__.py': [
        ('proposal, safety or setup service (tests/test_market_intel_70.py).\n"""\n',
         'proposal, safety or setup service (tests/test_market_intel_70.py).\n\nStage 7.1 (DESIGN_71_MARKET_INTEL_STORE.md) adds, still with nothing in the live path importing them:\n\n  calendar_history.py  the verified 2025 NYSE history (holidays, the 2025-01-09 closure, early closes); 2024 / 2028 stay NOT_COVERED\n  migrations.py        the versioned schema of the store\'s own SQLite file (data/market_intel.db), append-only triggers on every table\n  store.py             the point-in-time store: lazy creation, idempotent ingestion, revisions, feed separation, PIT queries, quality reports\n  archive.py           the raw-response archive (gzip + SHA-256 manifest under runtime/market_intel/raw)\n  sources/alpaca_bars.py  Alpaca bars -> envelopes, parse-only (no network code)\n  ingest.py            the offline backfill planner and CLI; the gated runner a future approved backfill will use\n  approvals.py         the fail-closed operator approval that runner needs (a file in runtime/; no code writes one)\n"""\n'),
    ],
    'market_intel/calendar.py': [
        ('executor\'s session health); a live year (2028) is a separate, declared change to autorun/calendar.py. `rule_holidays` only cross-checks.\n"""\n',
         'executor\'s session health); a live year (2028) is a separate, declared change to autorun/calendar.py. `rule_holidays` only cross-checks.\n\nStage 7.1: 2025 is covered by the market_intel-owned, verified history table (calendar_history.py: the NYSE publications plus an\nempirical cross-check). For a history year the session is classified here with the 6.10A classifier\'s own thresholds (PRE from 04:00,\nREG from 09:30 to the close, POST to the late-trading end) and the history table\'s closes; 2026-2027 still come from the 6.10A classifier\nitself. Every other year (2024 and earlier, 2028 and later) stays NOT_COVERED.\n\nTHE ONE DECLARED EXCEPTION to "one source of truth" (Stage 7.1, operator-approved): on an NYSE early-close day the late trading session\nends at 17:00 New York time, not 20:00 (the NYSE release). For 2025 the history table says so; for 2026-2027 a market_intel-owned,\nversioned override (calendar_history.LATE_TRADING_EARLY_CLOSES, nyse_late_trading_2026_2027_v1) turns the 6.10A classifier\'s AFTER_HOURS\ninto CLOSED from 17:00 on those three dates only. autorun/calendar.py and the executor\'s session health are untouched.\n"""\n'),
        ('\nfrom autorun import AFTER_HOURS, CLOSED as A_CLOSED, PREMARKET, REGULAR\nfrom autorun import calendar as CAL\n',
         '\nfrom autorun import AFTER_HOURS, AFTER_HOURS_END_MINUTE, CLOSED as A_CLOSED, PREMARKET, PREMARKET_MINUTE, REGULAR\nfrom autorun import calendar as CAL\n'),
        ('from market_intel import CLOSED, POST, PRE, REG\n\n',
         'from market_intel import CLOSED, POST, PRE, REG\nfrom market_intel import calendar_history as HIST\nfrom setups.market_hours import OPEN_MINUTE, to_et\n\n'),
        ('def covered(d: date) -> bool:\n    return CAL.covered(d)\n\n',
         'def covered(d: date) -> bool:\n    return CAL.covered(d) or HIST.covered(d)\n\n\ndef _close_minute(d: date) -> Optional[int]:\n    """The regular close of a covered date (None on a weekend or a holiday): the history table for its years, else the 6.10A table."""\n    return HIST.close_minute(d) if HIST.covered(d) else CAL.close_minute(d)\n\n\ndef calendar_version(d: Optional[date]) -> str:\n    if d is None:\n        return "UNAVAILABLE"\n    if HIST.covered(d):\n        return HIST.HISTORY_VERSION\n    return CAL.CALENDAR_VERSION if CAL.covered(d) else "NOT_COVERED"\n\n\ndef post_market_end_minute(d: Optional[date]) -> Optional[int]:\n    """When the late (post-market) session of a market day ends, minutes after midnight New York time; None on a non-session or uncovered\n    day: 17:00 on an NYSE early close (the 2025 history table; the 2026-2027 late-trading override), else 20:00."""\n    if d is None or market_day_status(d) != MARKET_DAY:\n        return None\n    if HIST.covered(d):\n        return HIST.post_market_end_minute(d)\n    return HIST.EARLY_CLOSE_LATE_TRADING_END_MINUTE if d in HIST.LATE_TRADING_EARLY_CLOSES else AFTER_HOURS_END_MINUTE\n\n\ndef _classify_history(now: datetime) -> Dict[str, Any]:\n    """A history-year moment, with the 6.10A classifier\'s own thresholds, the history table\'s close and its late-trading end (17:00 on\n    an early-close day: the NYSE release)."""\n    et = to_et(_utc(now))\n    d = et.date()\n    m = et.hour * 60 + et.minute + et.second / 60.0\n    close = HIST.close_minute(d)\n    late_end = HIST.post_market_end_minute(d) if close is not None else None\n    if close is None or m < PREMARKET_MINUTE or m >= late_end:\n        cls = A_CLOSED\n    elif m < OPEN_MINUTE:\n        cls = PREMARKET\n    elif m < close:\n        cls = REGULAR\n    else:\n        cls = AFTER_HOURS\n    return {"session": cls, "trading_date": d.isoformat(), "et_time": et.strftime("%H:%M:%S"), "close_minute": close,\n            "calendar": HIST.HISTORY_VERSION}\n\n'),
        ('        return NOT_COVERED\n    return MARKET_DAY if CAL.close_minute(d) is not None else HOLIDAY\n\n',
         '        return NOT_COVERED\n    return MARKET_DAY if _close_minute(d) is not None else HOLIDAY\n\n'),
        ('    c = SES.classify(_utc(now))\n    d = date.fromisoformat(c["trading_date"]) if c.get("trading_date") else None\n',
         '    c = SES.classify(_utc(now))\n    if c.get("trading_date") and HIST.covered(date.fromisoformat(c["trading_date"])):\n        c = _classify_history(now)                         # 2025: the verified history table, never the weekday assumption\n    d = date.fromisoformat(c["trading_date"]) if c.get("trading_date") else None\n'),
        ('    minute = int(et[:2]) * 60 + int(et[3:5]) if et else None\n    status = market_day_status(d)\n',
         '    minute = int(et[:2]) * 60 + int(et[3:5]) if et else None\n    if c["session"] == AFTER_HOURS and d in HIST.LATE_TRADING_EARLY_CLOSES and minute is not None and minute >= HIST.EARLY_CLOSE_LATE_TRADING_END_MINUTE:\n        c = {**c, "session": A_CLOSED, "late_trading_override": HIST.LATE_TRADING_OVERRIDE_VERSION}   # 2026-2027 early close: late trading ended 17:00\n    status = market_day_status(d)\n'),
        ('            "market_day": None if status == NOT_COVERED else status == MARKET_DAY, "close_minute": c.get("close_minute"),\n            "covered": bool(d and covered(d)), "calendar": c.get("calendar")}\n\n',
         '            "market_day": None if status == NOT_COVERED else status == MARKET_DAY, "close_minute": c.get("close_minute"),\n            "covered": bool(d and covered(d)), "calendar": c.get("calendar"),\n            **({"late_trading_override": c["late_trading_override"]} if "late_trading_override" in c else {})}\n\n'),
        ('        return None\n    close = CAL.close_minute(d)\n    return 0 if close is None else close - (9 * 60 + 30)\n',
         '        return None\n    close = _close_minute(d)\n    return 0 if close is None else close - (9 * 60 + 30)\n'),
    ],
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('  V01-V05 validation codes on fixtures (bars, series, quotes, feeds, news, events); every code is declared and accepted by the envelope;\n          uncovered 2025 / 2028 dates and bars on known holidays are flagged and blocked, never treated as verified sessions.\n  K01-K03 the calendar: the Stage 6.10A classifier is the one source of truth; holidays, early closes, PRE / REG / POST, DST change days,\n',
         '  V01-V05 validation codes on fixtures (bars, series, quotes, feeds, news, events); every code is declared and accepted by the envelope;\n          uncovered 2024 / 2028 dates and bars on known holidays are flagged and blocked, never treated as verified sessions.\n  K01-K03 the calendar: the Stage 6.10A classifier is the one source of truth; holidays, early closes, PRE / REG / POST, DST change days,\n'),
        ('\ndef test_V05_uncovered_2025_and_2028_dates_and_bars_on_known_holidays_are_flagged_and_blocked():\n    good = {"open": 10, "high": 11, "low": 9.5, "close": 10.5, "volume": 1}\n',
         '\ndef test_V05_uncovered_2024_and_2028_dates_and_bars_on_known_holidays_are_flagged_and_blocked():\n    good = {"open": 10, "high": 11, "low": 9.5, "close": 10.5, "volume": 1}\n'),
        ('    series = lambda days: VA.daily_series_codes([{**good, "day": d} for d in days])                     # noqa: E731\n    y25 = series([date(2025, 6, 30), date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 3), date(2025, 7, 7)])   # over 2025-07-04\n    assert y25["codes"] == ["CALENDAR_NOT_COVERED"] and y25["blocked"] is True and y25["holes"] == []          # never "verified complete"\n    assert "2025-07-03..2025-07-07" in y25["uncovered"] and "2025-06-30" in y25["uncovered"]\n    edge = series([date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2)])                                # into the covered table\n    assert edge["codes"] == ["CALENDAR_NOT_COVERED"] and edge["blocked"] is True\n',
         '    series = lambda days: VA.daily_series_codes([{**good, "day": d} for d in days])                     # noqa: E731\n    y24 = series([date(2024, 6, 28), date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 5)])   # over 2024-07-04\n    assert y24["codes"] == ["CALENDAR_NOT_COVERED"] and y24["blocked"] is True and y24["holes"] == []          # never "verified complete"\n    assert "2024-07-03..2024-07-05" in y24["uncovered"] and "2024-06-28" in y24["uncovered"]\n    edge = series([date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2)])                                # into the covered history\n    assert edge["codes"] == ["CALENDAR_NOT_COVERED"] and edge["blocked"] is True\n'),
        ('    assert series([date(2026, 11, 24), date(2026, 11, 27)])["codes"] == []                                   # one missing day: below the threshold\n    for t in (ny(2025, 7, 3, 10, 0), ny(2028, 3, 1, 10, 0)):                                                  # the 6.10A classifier would say REG\n        m = VA.minute_series_codes([{**good, "ts": t}], regular_only=False)\n',
         '    assert series([date(2026, 11, 24), date(2026, 11, 27)])["codes"] == []                                   # one missing day: below the threshold\n    for t in (ny(2024, 7, 3, 10, 0), ny(2028, 3, 1, 10, 0)):                                                  # the 6.10A classifier would say REG\n        m = VA.minute_series_codes([{**good, "ts": t}], regular_only=False)\n'),
        ('    assert (q["status"], q["reason"], q["usable"]) == ("UNAVAILABLE", "CALENDAR_NOT_COVERED", False)\n    assert FR.assess("NEWS_POLL", None, ny(2025, 7, 3, 7, 0))["reason"] == "CALENDAR_NOT_COVERED"\n    assert FR.assess("QUOTE_DAY", ny(2028, 3, 4, 10, 0), ny(2028, 3, 4, 10, 0))["status"] == "NOT_APPLICABLE"   # a 2028 Saturday is still a weekend\n',
         '    assert (q["status"], q["reason"], q["usable"]) == ("UNAVAILABLE", "CALENDAR_NOT_COVERED", False)\n    assert FR.assess("NEWS_POLL", None, ny(2024, 7, 3, 7, 0))["reason"] == "CALENDAR_NOT_COVERED"\n    assert FR.assess("QUOTE_DAY", ny(2028, 3, 4, 10, 0), ny(2028, 3, 4, 10, 0))["status"] == "NOT_APPLICABLE"   # a 2028 Saturday is still a weekend\n'),
        ('    assert FR.assess("QUOTE_DAY", ny(2028, 3, 4, 10, 0), ny(2028, 3, 4, 10, 0))["status"] == "NOT_APPLICABLE"   # a 2028 Saturday is still a weekend\n    assert FR.assess_daily_bar(date(2025, 7, 3), ny(2025, 7, 7, 10, 0))["reason"] == "CALENDAR_NOT_COVERED"\n    assert FR.assess_daily_bar(date(2025, 12, 31), ny(2026, 1, 2, 10, 0))["reason"] == "CALENDAR_NOT_COVERED"   # the expected session is in 2025\n\n',
         '    assert FR.assess("QUOTE_DAY", ny(2028, 3, 4, 10, 0), ny(2028, 3, 4, 10, 0))["status"] == "NOT_APPLICABLE"   # a 2028 Saturday is still a weekend\n    assert FR.assess_daily_bar(date(2024, 7, 3), ny(2024, 7, 8, 10, 0))["reason"] == "CALENDAR_NOT_COVERED"\n    assert FR.assess_daily_bar(date(2024, 12, 31), ny(2025, 1, 2, 10, 0))["reason"] == "CALENDAR_NOT_COVERED"   # the expected session is in 2024\n\n'),
        ('    t = ny(2026, 11, 27, 0, 0)\n    for _ in range(0, 24 * 60, 7):                                                                   # one source of truth, minute by minute\n        assert CAL.session_of(t) == mapping[SES.session_of(t)]\n        t += timedelta(minutes=7)\n',
         '    t = ny(2026, 11, 27, 0, 0)\n    for _ in range(0, 24 * 60, 7):                                                                   # one source of truth, minute by minute -\n        want = mapping[SES.session_of(t)]                                                            # except the ONE declared Stage 7.1 exception:\n        if want == "POST" and t >= ny(2026, 11, 27, 17, 0):                                          # late trading ends at 17:00 on an NYSE early close\n            want = "CLOSED"                                                                          # (DESIGN_71 section 5; 6.10A itself is unchanged)\n        assert CAL.session_of(t) == want\n        t += timedelta(minutes=7)\n'),
        ('    st = CAL.market_day_status\n    assert [st(d) for d in (date(2025, 7, 4), date(2025, 1, 9), date(2028, 1, 3), date(2025, 7, 5), date(2028, 1, 1))] == [\n        "NOT_COVERED", "NOT_COVERED", "NOT_COVERED", "WEEKEND", "WEEKEND"]                       # 2025-01-09 (a closure no rule derives) stays unknown\n    assert [st(d) for d in (date(2026, 11, 26), date(2026, 4, 3), date(2027, 6, 18), date(2026, 7, 3), date(2027, 12, 24))] == ["HOLIDAY"] * 5\n',
         '    st = CAL.market_day_status\n    assert [st(d) for d in (date(2024, 7, 4), date(2024, 1, 9), date(2028, 1, 3), date(2024, 7, 6), date(2028, 1, 1))] == [\n        "NOT_COVERED", "NOT_COVERED", "NOT_COVERED", "WEEKEND", "WEEKEND"]                       # 2024: outside every verified table\n    assert [st(d) for d in (date(2026, 11, 26), date(2026, 4, 3), date(2027, 6, 18), date(2026, 7, 3), date(2027, 12, 24))] == ["HOLIDAY"] * 5\n'),
        ('    with pytest.raises(CAL.CalendarNotCovered):\n        CAL.is_market_day(date(2025, 7, 3))\n    with pytest.raises(CAL.CalendarNotCovered):\n',
         '    with pytest.raises(CAL.CalendarNotCovered):\n        CAL.is_market_day(date(2024, 7, 3))\n    with pytest.raises(CAL.CalendarNotCovered):\n'),
        ('    assert CAL.is_market_day(date(2028, 1, 1)) is False and CAL.is_market_day(date(2026, 11, 26)) is False\n    assert CAL.previous_market_day(date(2026, 1, 2)) is None                                       # 2026-01-01 holiday, then 2025: unknown\n    assert CAL.next_market_day(date(2027, 12, 31)) is None and CAL.next_market_day(date(2027, 12, 23)) == date(2027, 12, 27)\n',
         '    assert CAL.is_market_day(date(2028, 1, 1)) is False and CAL.is_market_day(date(2026, 11, 26)) is False\n    assert CAL.previous_market_day(date(2025, 1, 2)) is None                                       # 2025-01-01 holiday, then 2024: unknown\n    assert CAL.next_market_day(date(2027, 12, 31)) is None and CAL.next_market_day(date(2027, 12, 23)) == date(2027, 12, 27)\n'),
        ('    assert CAL.next_market_day(date(2027, 12, 31)) is None and CAL.next_market_day(date(2027, 12, 23)) == date(2027, 12, 27)\n    assert CAL.regular_minutes(date(2025, 7, 3)) is None and CAL.regular_minutes(date(2025, 7, 5)) == 0\n    assert CAL.last_completed_session(ny(2026, 1, 2, 10, 0)) is None and CAL.last_completed_session(ny(2028, 3, 1, 19, 0)) is None\n    s25 = CAL.session(ny(2025, 7, 3, 10, 0))\n    assert (s25["session"], s25["day_status"], s25["market_day"], s25["covered"]) == ("REG", "NOT_COVERED", None, False)   # never claimed\n\n',
         '    assert CAL.next_market_day(date(2027, 12, 31)) is None and CAL.next_market_day(date(2027, 12, 23)) == date(2027, 12, 27)\n    assert CAL.regular_minutes(date(2024, 7, 3)) is None and CAL.regular_minutes(date(2024, 7, 6)) == 0\n    assert CAL.last_completed_session(ny(2025, 1, 2, 10, 0)) is None and CAL.last_completed_session(ny(2028, 3, 1, 19, 0)) is None\n    s24 = CAL.session(ny(2024, 7, 3, 10, 0))\n    assert (s24["session"], s24["day_status"], s24["market_day"], s24["covered"]) == ("REG", "NOT_COVERED", None, False)   # never claimed\n\n'),
        ('                   "market_intel.calendar", "market_intel.settings", "market_intel.validate", "autorun", "autorun.calendar", "autorun.session",\n                   "api.validation", "config", "zoneinfo"}\n\n',
         '                   "market_intel.calendar", "market_intel.settings", "market_intel.validate", "autorun", "autorun.calendar", "autorun.session",\n                   "api.validation", "config", "zoneinfo", "market_intel.calendar_history", "setups.market_hours"}\n\n'),
        ('def test_I01_pure_contracts_only_pure_imports_no_io_and_nothing_imports_them():\n    files = sorted((ROOT / "market_intel").glob("*.py"))\n    assert [f.name for f in files] == ["__init__.py", "budget.py", "calendar.py", "freshness.py", "models.py", "settings.py", "validate.py"]\n    for f in files:\n',
         'def test_I01_pure_contracts_only_pure_imports_no_io_and_nothing_imports_them():\n    files = [ROOT / "market_intel" / n for n in ("__init__.py", "budget.py", "calendar.py", "freshness.py", "models.py", "settings.py", "validate.py",\n                                                 "calendar_history.py")]            # the pure contracts; the Stage 7.1 store: its own tests\n    assert all(f.is_file() for f in files)\n    for f in files:\n'),
        ('\ndef test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited():\n',
         '\ndef post(rel):\n    """main after PR #41 (Stage 7.0 merged; the Stage 7.1 base): what Stage 7.0 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_71}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_71} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\ndef test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited():\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_70).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_70, FL.BASE_71).split())                # Stage 7.0 itself, between two fixed commits\n    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71                                                         # Stage 7.1, pinned by its own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n                                                         "missing": sorted((FL.NEW_70 | FL.EDITED_70 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_70).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_70).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_70 and {p for p in changed if p in in_base} == FL.EDITED_70\n    assert FL.NEW_70 == {"DESIGN_70_MARKET_INTEL_CONTRACTS.md", "tests/test_market_intel_70.py"} | {f"market_intel/{n}" for n in (\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_70).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_70 and {p for p in stage if p in in_base} == FL.EDITED_70\n    assert FL.NEW_70 == {"DESIGN_70_MARKET_INTEL_CONTRACTS.md", "tests/test_market_intel_70.py"} | {f"market_intel/{n}" for n in (\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_70.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_70.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_70) == FL.EDITED_70 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.0 (market intelligence contracts")\n',
         '    assert set(REPLAY_70) == FL.EDITED_70 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.0 (market intelligence contracts")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_70 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.0 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_70 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
