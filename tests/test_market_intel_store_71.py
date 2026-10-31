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
  S01     isolation: no network / broker / model / execution import in the new modules; nothing outside market_intel imports them.
  F01-F03 scope: exactly the declared files; frozen hashes; every bookkeeping edit pinned exactly.
"""
from __future__ import annotations

import ast
import json
import os
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
from market_intel import calendar as CAL
from market_intel import calendar_history as HIST
from market_intel import ingest as IG
from market_intel import migrations as MG
from market_intel.archive import RawArchive
from market_intel.models import FetchRecord, Observation, forbidden_key
from market_intel.sources import alpaca_bars as AB
from market_intel.store import (CoverageError, DatasetSpec, FeedRequired, MarketIntelStore, StoreError, content_hash, natural_key, parse_ts,
                                ts)

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
    return MarketIntelStore(tmp_path / name, clock=clock or Clock(SAT))


def spec(job="BACKFILL", feed="iex", kind="BAR_1DAY"):
    return DatasetSpec(job_kind=job, kind=kind, provider="ALPACA", feed=feed, adjustment="raw", symbols=["SPY"], timeframe="1Day")


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
    s.ingest(did, [daily("SPY", date(2025, 12, 1), 100.0)])
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
        hashes.append(s.finish_dataset(did, "COMPLETED", duplicates=again.duplicates)["content_sha256"])
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
    did = s.create_dataset(spec(job="FIXTURE"))
    v1 = s.ingest(did, [daily("SPY", d, 100.0)]).obs_ids[0]
    t1 = clk()
    clk.tick(86400)
    corr = s.ingest(did, [daily("SPY", d, 101.0, retrieved=clk())])                             # the provider corrected the close
    t2 = clk()
    clk.tick(86400)
    back = s.ingest(did, [daily("SPY", d, 100.0, retrieved=clk())])                             # ... and reverted it: A -> B -> A
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
                       retrieved_at=ny(2025, 12, 2, 16, 5), available_at=ny(2025, 12, 2, 16, 5), session="REG", session_date=date(2025, 12, 2),
                       status="AVAILABLE", payload={"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10})   # ... claiming an earlier availability
    s.ingest(fx, [late])
    assert s.latest_usable("BAR_1DAY", at=ny(2025, 12, 3, 9, 0), symbol="QQQ", feed="iex") is None   # not in the store yet at that time
    assert s.latest_usable("BAR_1DAY", at=clk(), symbol="QQQ", feed="iex") is not None
    snap = s.snapshot("BAR_1DAY", ["spy", "QQQ", "IWM"], at=SAT, feed="iex")
    assert (snap["SPY"]["usable"], snap["QQQ"]["status"], snap["IWM"]["status"]) == (True, "UNAVAILABLE", "UNAVAILABLE")
    assert "close" not in snap["IWM"] and snap["IWM"]["reason"] == "NO_USABLE_OBSERVATION_KNOWN_AT"        # missing is never a zero


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
    assert s.latest_usable("BAR_1DAY", at=SAT, symbol="SPY", feed="iex") is None and s.history("BAR_1DAY", symbol="SPY", feed="iex")[0]["payload"] == {}
    body = json.dumps({"symbol": "SPY", "bars": [{"t": "2025-12-01T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "v": 9}, {"t": "not-a-time", "o": 1, "h": 2,
                       "l": 0.5, "c": 1, "v": 9}, {"t": "2025-12-02T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]}).encode()
    obs, token, problems = AB.parse_page(body, symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT)
    assert [o.session_date for o in obs] == [date(2025, 12, 2)] and [p["problem"] for p in problems] == ["MALFORMED_ROW", "BAD_TIME"] and token is None
    assert AB.parse_page(b"<html>", symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw", retrieved_at=SAT)[2] == [{"problem": "INVALID_JSON"}]
    assert AB.parse_page(json.dumps({"symbol": "QQQ", "bars": []}).encode(), symbol="SPY", timeframe="1Day", feed="iex", adjustment="raw",
                         retrieved_at=SAT)[2][0]["problem"] == "SYMBOL_MISMATCH"
    nan = json.dumps({"bars": [{"t": "2025-12-03T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": float("nan"), "v": 9}]}).encode()
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
    days = sessions_2025(date(2025, 12, 1), 10)
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
    p = IG.plan(IG.BackfillRequest(symbols=("SPY",), start=date(2025, 12, 1), end=date(2025, 12, 5), timeframe="1Day", feed="iex"), clk())
    res = IG.run_backfill(p, fetch_fn=Wire(clk), store=s, archive=arc, approval=IG.BackfillApproval("OP-1", p["plan_sha256"]), clock=clk,
                          sleep_fn=lambda x: clk.tick(x))
    row = s.history("BAR_1DAY", symbol="SPY", feed="iex")[0]
    prov = s.provenance(row["obs_id"])
    assert prov["fetch"]["status"] == "OK" and prov["fetch"]["raw_path"].endswith(".json.gz") and prov["dataset"]["approval_ref"] == "OP-1"
    assert prov["dataset"]["plan_sha256"] == p["plan_sha256"] and [e["status"] for e in prov["dataset_events"]] == ["RUNNING", "COMPLETED"]
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


def _plan(symbols=("SPY",), start=date(2025, 12, 1), end=date(2025, 12, 5), feed="iex", now=SAT, timeframe="1Day"):
    return IG.plan(IG.BackfillRequest(symbols=tuple(symbols), start=start, end=end, timeframe=timeframe, feed=feed), now)


def test_R01_a_real_run_needs_a_valid_plan_an_approval_bound_to_it_sip_approval_and_the_window(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    p = _plan()
    w = Wire(clk)
    run = lambda plan_, appr, c=clk: IG.run_backfill(plan_, fetch_fn=w, store=s, archive=arc, approval=appr, clock=c, sleep_fn=lambda x: None)   # noqa: E731
    for bad in (None, "OK", IG.BackfillApproval("", p["plan_sha256"]), IG.BackfillApproval("OP", "0" * 64)):
        with pytest.raises(PermissionError):
            run(p, bad)
    with pytest.raises(PermissionError):
        run(_plan(start=date(2024, 12, 30), end=date(2025, 1, 3)), IG.BackfillApproval("OP", "x"))          # a refused plan
    sip = _plan(feed="sip")
    with pytest.raises(PermissionError):
        run(sip, IG.BackfillApproval("OP", sip["plan_sha256"]))                                            # SIP needs its own approval
    weekday = Clock(ny(2025, 12, 8, 10, 0))                                                                # Monday 10:00 New York
    with pytest.raises(PermissionError):
        run(_plan(now=weekday()), IG.BackfillApproval("OP", _plan(now=weekday())["plan_sha256"]), weekday)
    assert w.calls == [] and not s.exists() and not arc.root.exists()                                    # nothing fetched, nothing written
    src = (ROOT / "market_intel" / "ingest.py").read_text(encoding="utf-8")
    assert src.count("BackfillApproval(") == 0 and "class BackfillApproval" in src                         # the CLI never builds an approval


def test_R02_paging_archive_retries_and_honest_final_states(tmp_path):
    clk = Clock(SAT)
    s, arc = new_store(tmp_path, clk), RawArchive(tmp_path / "raw")
    p = _plan(symbols=("SPY", "QQQ"), start=date(2025, 11, 3), end=date(2025, 11, 28))
    w = Wire(clk, per_page=7, script={1: (429, {"retry-after": "5"}, b"slow down")})
    sleeps = []
    res = IG.run_backfill(p, fetch_fn=w, store=s, archive=arc, approval=IG.BackfillApproval("OP", p["plan_sha256"]), clock=clk,
                          sleep_fn=lambda x: (sleeps.append(x), clk.tick(x)))
    n = p["coverage"]["sessions"]
    assert n == 19 and res["status"] == "COMPLETED" and res["inserted"] == 2 * n and res["failed_fetches"] == 1 and res["unrecovered"] == 0   # a retried 429 is recovered
    assert [c.get("page_token") for c in w.calls if c["symbol"] == "SPY"] == [None, "7", "7", "14"] and sleeps and sleeps[0] == pytest.approx(5.0, abs=1.1)
    assert arc.verify()["ok"] and arc.verify()["entries"] == len(w.calls)
    for sym in ("SPY", "QQQ"):
        assert len(s.history("BAR_1DAY", symbol=sym, feed="iex")) == n
    ok = Clock(SAT)
    s2 = new_store(tmp_path, ok, "ok.db")
    p2 = _plan(symbols=("SPY", "QQQ"), start=date(2025, 11, 3), end=date(2025, 11, 28))
    r2 = IG.run_backfill(p2, fetch_fn=Wire(ok, per_page=7), store=s2, archive=RawArchive(tmp_path / "raw2"), approval=IG.BackfillApproval("OP", p2["plan_sha256"]),
                         clock=ok, sleep_fn=lambda x: ok.tick(x))
    assert r2["status"] == "COMPLETED"
    ok3 = Clock(SAT)
    s3 = new_store(tmp_path, ok3, "ok3.db")
    r3 = IG.run_backfill(p2, fetch_fn=Wire(ok3, per_page=7), store=s3, archive=RawArchive(tmp_path / "raw3"), approval=IG.BackfillApproval("OP", p2["plan_sha256"]),
                         clock=ok3, sleep_fn=lambda x: ok3.tick(x))
    assert r3["content_sha256"] == r2["content_sha256"]                                              # the same data, the same dataset hash
    stuck = {i: (429, {"retry-after": "5"}, b"slow") for i in range(1, 4)}                          # page 2 of SPY never recovers
    cases = {"rate_limited": (Wire(Clock(SAT), per_page=7, script=stuck), "PARTIAL", "BUDGET_BLOCKED_BACKOFF"),   # still backing off: stop
             "denied": (Wire(Clock(SAT), symbol_status={"SPY": 403}), "FAILED", "ACCESS_DENIED"),
             "one_symbol_down": (Wire(Clock(SAT), symbol_status={"QQQ": 500}), "PARTIAL", None),
             "empty": (Wire(Clock(SAT), empty=True), "EMPTY", None)}
    for name, (wire, want, stop) in cases.items():
        st = new_store(tmp_path, wire.clock, f"{name}.db")
        out = IG.run_backfill(p2, fetch_fn=wire, store=st, archive=RawArchive(tmp_path / f"raw_{name}"), approval=IG.BackfillApproval("OP", p2["plan_sha256"]),
                              clock=wire.clock, sleep_fn=lambda x, c=wire.clock: c.tick(x))
        assert (out["status"], out["stop"]) == (want, stop), name
    closes = Clock(ny(2025, 12, 8, 6, 59) + timedelta(seconds=57))                                  # Monday 06:59:57: the window closes at 07:00
    s4 = new_store(tmp_path, closes, "closes.db")
    p4 = _plan(symbols=("SPY", "QQQ"), start=date(2025, 11, 3), end=date(2025, 11, 28), now=closes())
    out4 = IG.run_backfill(p4, fetch_fn=Wire(closes, per_page=7), store=s4, archive=RawArchive(tmp_path / "raw4"), approval=IG.BackfillApproval("OP", p4["plan_sha256"]),
                           clock=closes, sleep_fn=lambda x: closes.tick(x))
    assert (out4["status"], out4["stop"]) == ("PARTIAL", "WINDOW_CLOSED")


def test_R03_the_budget_paces_requests_and_its_history_is_persisted(tmp_path):
    from market_intel.budget import ProviderBudget
    clk = Clock(SAT)
    s = new_store(tmp_path, clk)
    p = _plan(symbols=("SPY", "QQQ", "IWM"), start=date(2025, 11, 3), end=date(2025, 11, 28))
    slow = ProviderBudget("ALPACA_BACKFILL", 2, clk)                                                 # 2 requests a minute
    sleeps = []
    res = IG.run_backfill(p, fetch_fn=Wire(clk), store=s, archive=RawArchive(tmp_path / "raw"), approval=IG.BackfillApproval("OP", p["plan_sha256"]),
                          clock=clk, sleep_fn=lambda x: (sleeps.append(x), clk.tick(x)), budget=slow)
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
    assert (ext["rows_per_symbol"], ext["pages_per_symbol"]) == (250 * 960, 24)                    # the design's "about 24 pages per symbol-year"
    p = IG.plan(IG.BackfillRequest(tuple(f"S{i}" for i in range(100)), date(2025, 1, 1), date(2025, 12, 31), "1Min", "iex", extended_hours=True), now)
    assert p["estimate"]["requests"] == 2400 and p["estimate"]["minutes_at_budget"] == 40.0          # at 60 requests a minute


def test_C05_the_bytes_per_row_estimate_is_measured(tmp_path):
    s = new_store(tmp_path)
    did = s.create_dataset(spec())
    days = sessions_2025(date(2025, 1, 2), 250)
    syms = [f"S{chr(65 + i)}" for i in range(8)]
    s.ingest(did, [daily(sym, d, 100.0 + i * 0.01) for sym in syms for i, d in enumerate(days)])
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
    cases = [(ny(2025, 7, 3, 12, 59), "REG"), (ny(2025, 7, 3, 13, 0), "POST"), (ny(2025, 7, 3, 19, 59), "POST"), (ny(2025, 7, 3, 20, 0), "CLOSED"),
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
    assert len(HIST.SOURCES) == 3 and HIST.HISTORY_YEARS == (2025,)


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


# ============================================================================================================ isolation (S)
NEW_MODULES = ("calendar_history.py", "migrations.py", "store.py", "archive.py", "ingest.py", "sources/__init__.py", "sources/alpaca_bars.py")
ALLOWED_71 = {"__future__", "argparse", "gzip", "hashlib", "json", "math", "sqlite3", "sys", "uuid", "contextlib", "dataclasses", "datetime", "pathlib",
              "typing", "zoneinfo", "config", "api.validation", "market_intel", "market_intel.calendar", "market_intel.validate", "market_intel.models",
              "market_intel.migrations", "market_intel.budget", "market_intel.settings", "market_intel.sources", "market_intel.store",
              "market_intel.archive", "market_intel.sources.alpaca_bars"}


def test_S01_the_new_modules_import_nothing_that_can_reach_a_network_a_broker_or_a_model():
    for rel in NEW_MODULES:
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


PINNED_V1_DDL = "c15451ca248520bcc26d7195a8a630caf15c36142ecdac213c8d9bcb42612f17"   # the v1 schema; a change is a new migration


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
        "archive.py", "calendar_history.py", "ingest.py", "migrations.py", "sources/__init__.py", "sources/alpaca_bars.py", "store.py")}
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
    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key
    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_71 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_71 = {
    '.gitignore': [
        ('runtime/\n',
         'runtime/\ndata/market_intel.db\ndata/market_intel.db-wal\ndata/market_intel.db-shm\n'),
    ],
    'DESIGN_70_MARKET_INTEL_CONTRACTS.md': [
        ('\nOne fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.\n',
         '\n**Stage 7.1 implemented the 2025 step** (`market_intel/calendar_history.py`, `DESIGN_71` section 5). It was verified against the NYSE\n2025 calendar release, the NYSE notice of the 2025-01-09 closure, and the archived IEX daily bars of 82 symbols (250 sessions, the\n11 closures exactly). 2024 and earlier, and 2028 onward, remain `NOT_COVERED`.\n\nOne fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.\n'),
    ],
    'market_intel/__init__.py': [
        ('proposal, safety or setup service (tests/test_market_intel_70.py).\n"""\n',
         'proposal, safety or setup service (tests/test_market_intel_70.py).\n\nStage 7.1 (DESIGN_71_MARKET_INTEL_STORE.md) adds, still with nothing in the live path importing them:\n\n  calendar_history.py  the verified 2025 NYSE history (holidays, the 2025-01-09 closure, early closes); 2024 / 2028 stay NOT_COVERED\n  migrations.py        the versioned schema of the store\'s own SQLite file (data/market_intel.db), append-only triggers on every table\n  store.py             the point-in-time store: lazy creation, idempotent ingestion, revisions, feed separation, PIT queries, quality reports\n  archive.py           the raw-response archive (gzip + SHA-256 manifest under runtime/market_intel/raw)\n  sources/alpaca_bars.py  Alpaca bars -> envelopes, parse-only (no network code)\n  ingest.py            the offline backfill planner and CLI; the gated runner a future approved backfill will use\n"""\n'),
    ],
    'market_intel/calendar.py': [
        ('executor\'s session health); a live year (2028) is a separate, declared change to autorun/calendar.py. `rule_holidays` only cross-checks.\n"""\n',
         'executor\'s session health); a live year (2028) is a separate, declared change to autorun/calendar.py. `rule_holidays` only cross-checks.\n\nStage 7.1: 2025 is covered by the market_intel-owned, verified history table (calendar_history.py: the NYSE publications plus an\nempirical cross-check). For a history year the session is classified here with the 6.10A classifier\'s own thresholds (PRE from 04:00,\nREG from 09:30 to the close, POST to 20:00) and the history table\'s closes; 2026-2027 still come from the 6.10A classifier itself. Every\nother year (2024 and earlier, 2028 and later) stays NOT_COVERED.\n"""\n'),
        ('\nfrom autorun import AFTER_HOURS, CLOSED as A_CLOSED, PREMARKET, REGULAR\nfrom autorun import calendar as CAL\n',
         '\nfrom autorun import AFTER_HOURS, AFTER_HOURS_END_MINUTE, CLOSED as A_CLOSED, PREMARKET, PREMARKET_MINUTE, REGULAR\nfrom autorun import calendar as CAL\n'),
        ('from market_intel import CLOSED, POST, PRE, REG\n\n',
         'from market_intel import CLOSED, POST, PRE, REG\nfrom market_intel import calendar_history as HIST\nfrom setups.market_hours import OPEN_MINUTE, to_et\n\n'),
        ('def covered(d: date) -> bool:\n    return CAL.covered(d)\n\n',
         'def covered(d: date) -> bool:\n    return CAL.covered(d) or HIST.covered(d)\n\n\ndef _close_minute(d: date) -> Optional[int]:\n    """The regular close of a covered date (None on a weekend or a holiday): the history table for its years, else the 6.10A table."""\n    return HIST.close_minute(d) if HIST.covered(d) else CAL.close_minute(d)\n\n\ndef calendar_version(d: Optional[date]) -> str:\n    if d is None:\n        return "UNAVAILABLE"\n    if HIST.covered(d):\n        return HIST.HISTORY_VERSION\n    return CAL.CALENDAR_VERSION if CAL.covered(d) else "NOT_COVERED"\n\n\ndef _classify_history(now: datetime) -> Dict[str, Any]:\n    """A history-year moment, with the 6.10A classifier\'s own thresholds and the history table\'s close."""\n    et = to_et(_utc(now))\n    d = et.date()\n    m = et.hour * 60 + et.minute + et.second / 60.0\n    close = HIST.close_minute(d)\n    if close is None or m < PREMARKET_MINUTE or m >= AFTER_HOURS_END_MINUTE:\n        cls = A_CLOSED\n    elif m < OPEN_MINUTE:\n        cls = PREMARKET\n    elif m < close:\n        cls = REGULAR\n    else:\n        cls = AFTER_HOURS\n    return {"session": cls, "trading_date": d.isoformat(), "et_time": et.strftime("%H:%M:%S"), "close_minute": close,\n            "calendar": HIST.HISTORY_VERSION}\n\n'),
        ('        return NOT_COVERED\n    return MARKET_DAY if CAL.close_minute(d) is not None else HOLIDAY\n\n',
         '        return NOT_COVERED\n    return MARKET_DAY if _close_minute(d) is not None else HOLIDAY\n\n'),
        ('    c = SES.classify(_utc(now))\n    d = date.fromisoformat(c["trading_date"]) if c.get("trading_date") else None\n',
         '    c = SES.classify(_utc(now))\n    if c.get("trading_date") and HIST.covered(date.fromisoformat(c["trading_date"])):\n        c = _classify_history(now)                         # 2025: the verified history table, never the weekday assumption\n    d = date.fromisoformat(c["trading_date"]) if c.get("trading_date") else None\n'),
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
