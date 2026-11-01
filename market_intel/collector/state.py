"""
market_intel/collector/state.py - Stage 7.3: the collector's own state file (DESIGN_73 section 4).

runtime/market_intel/collector.db (git-ignored), a SQLite file of its own: the Stage 7.1 store verifies its schema objects exactly and
refuses any it does not own, so collector state never lives there. Created lazily (constructing, importing or reading creates nothing);
WAL; every write is ONE BEGIN IMMEDIATE transaction; every table is append-only (UPDATE / DELETE triggers); every open verifies the live
schema objects against this code (an extra, missing or altered object is refused, as is a reset user_version).

Tables: cs_runs (one row per run start: source, scope, window, gap, worker, lease, dataset), cs_run_ends (one end per run), cs_watermarks
(the log; the current watermark is the latest row per source and scope), cs_leases + cs_lease_releases, cs_breaker_events.

The schema itself enforces the watermark rules - whoever writes, raw SQL included:
* a watermark names a run of ITS source and scope that ended COMPLETED or EMPTY (every page fetched, validated and ingested) AND VERIFIED
  (its seal anchored and read back from the store - the runner records `verified`);
* it is never later than that run's end (nothing is "covered" in the future) and never earlier than the current one (it never moves back);
* one watermark per run.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from market_intel.collector import policy as PO
from market_intel.store import parse_ts, ts

STATE_FILE = "collector.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 10_000
RUN_END_STATUSES = ("COMPLETED", "EMPTY", "PARTIAL", "FAILED", "INTERRUPTED", "PENDING_FINALIZATION")
COMPLETE = ("COMPLETED", "EMPTY")
TABLES = ("cs_runs", "cs_run_ends", "cs_watermarks", "cs_leases", "cs_lease_releases", "cs_breaker_events")
_TS = "({c} GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-6][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')"


class StateError(RuntimeError):
    pass


class StateMismatch(StateError):
    pass


def _ts_check(*cols: str, nullable: Tuple[str, ...] = ()) -> str:
    return ", ".join(f"CHECK ({c} IS NULL OR {_TS.format(c=c)})" if c in nullable else f"CHECK ({_TS.format(c=c)})" for c in cols)


DDL: Tuple[str, ...] = (
    f"""CREATE TABLE cs_runs (run_id TEXT PRIMARY KEY, source TEXT NOT NULL, scope_key TEXT NOT NULL, scope_json TEXT NOT NULL, worker TEXT NOT NULL,
        lease_id TEXT NOT NULL REFERENCES cs_leases(lease_id), plan_sha256 TEXT NOT NULL, started_at TEXT NOT NULL, window_start TEXT, window_end TEXT,
        gap_start TEXT, gap_end TEXT, dataset_id TEXT NOT NULL, trial INTEGER NOT NULL CHECK (trial IN (0, 1)),
        {_ts_check('started_at', 'window_start', 'window_end', 'gap_start', 'gap_end', nullable=('window_start', 'window_end', 'gap_start', 'gap_end'))})""",
    "CREATE INDEX cs_runs_pair ON cs_runs (source, scope_key, started_at)",
    f"""CREATE TABLE cs_run_ends (run_id TEXT PRIMARY KEY REFERENCES cs_runs(run_id), status TEXT NOT NULL CHECK (status IN
        ('COMPLETED', 'EMPTY', 'PARTIAL', 'FAILED', 'INTERRUPTED', 'PENDING_FINALIZATION')), ended_at TEXT NOT NULL, stop TEXT, failed INTEGER NOT NULL
        CHECK (failed IN (0, 1)), clock_skew INTEGER NOT NULL CHECK (clock_skew IN (0, 1)), verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
        last_error TEXT NOT NULL, content_sha256 TEXT,
        counts_json TEXT NOT NULL, {_ts_check('ended_at')})""",
    f"""CREATE TABLE cs_watermarks (seq INTEGER PRIMARY KEY, source TEXT NOT NULL, scope_key TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE
        REFERENCES cs_runs(run_id), watermark TEXT NOT NULL, at TEXT NOT NULL, {_ts_check('watermark', 'at')})""",
    "CREATE INDEX cs_watermarks_pair ON cs_watermarks (source, scope_key, seq)",
    """CREATE TRIGGER cs_watermarks_need_complete_run BEFORE INSERT ON cs_watermarks WHEN NOT EXISTS (SELECT 1 FROM cs_run_ends e JOIN cs_runs r
        ON r.run_id = e.run_id WHERE e.run_id = NEW.run_id AND e.status IN ('COMPLETED', 'EMPTY') AND e.verified = 1 AND r.source = NEW.source
        AND r.scope_key = NEW.scope_key)
        BEGIN SELECT RAISE(ABORT, 'WATERMARK_NEEDS_COMPLETE_RUN: a watermark names a verified COMPLETED or EMPTY run of its own source and scope'); END""",
    """CREATE TRIGGER cs_watermarks_not_after_run BEFORE INSERT ON cs_watermarks WHEN NEW.watermark > (SELECT e.ended_at FROM cs_run_ends e
        WHERE e.run_id = NEW.run_id) BEGIN SELECT RAISE(ABORT, 'WATERMARK_IN_FUTURE: a watermark is never later than its run end'); END""",
    """CREATE TRIGGER cs_watermarks_monotonic BEFORE INSERT ON cs_watermarks WHEN EXISTS (SELECT 1 FROM cs_watermarks w WHERE w.source = NEW.source
        AND w.scope_key = NEW.scope_key AND w.watermark > NEW.watermark) BEGIN SELECT RAISE(ABORT, 'WATERMARK_BACKWARDS: a watermark never moves back'); END""",
    f"""CREATE TABLE cs_leases (lease_id TEXT PRIMARY KEY, source TEXT NOT NULL, scope_key TEXT NOT NULL, provider TEXT NOT NULL, worker TEXT NOT NULL,
        acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, CHECK (expires_at > acquired_at), {_ts_check('acquired_at', 'expires_at')})""",
    "CREATE INDEX cs_leases_expiry ON cs_leases (expires_at)",
    f"""CREATE TABLE cs_lease_releases (lease_id TEXT PRIMARY KEY REFERENCES cs_leases(lease_id), released_at TEXT NOT NULL, {_ts_check('released_at')})""",
    f"""CREATE TABLE cs_breaker_events (seq INTEGER PRIMARY KEY, source TEXT NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN
        ('CLOSED', 'OPEN', 'HALF_OPEN')), failures INTEGER NOT NULL CHECK (failures >= 0), opened_at TEXT, cooldown_s REAL NOT NULL CHECK (cooldown_s >= 0),
        trial_at TEXT, run_id TEXT, {_ts_check('at', 'opened_at', 'trial_at', nullable=('opened_at', 'trial_at'))})""",
    "CREATE INDEX cs_breaker_source ON cs_breaker_events (source, seq)",
    *(f"CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, '{t} rows are immutable'); END" for t in TABLES),
    *(f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} rows are never deleted'); END" for t in TABLES),
)
_EXPECTED: Dict[Tuple[str, str], str] = {}


def _objects(conn: sqlite3.Connection) -> Dict[Tuple[str, str], str]:
    return {(t, n): " ".join((s or "").split()) for t, n, s in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%'")}


def expected_objects() -> Dict[Tuple[str, str], str]:
    if not _EXPECTED:
        mem = sqlite3.connect(":memory:")
        try:
            for st in DDL:
                mem.execute(st)
            _EXPECTED.update(_objects(mem))
        finally:
            mem.close()
    return _EXPECTED


def verify(conn: sqlite3.Connection) -> int:
    v = int(conn.execute("PRAGMA user_version").fetchone()[0])
    live = _objects(conn)
    if v == 0:
        if live:
            raise StateMismatch("collector.db has schema objects but user_version 0: refused, never read as empty")
        return 0
    if v != SCHEMA_VERSION:
        raise StateMismatch(f"collector.db is schema version {v}; this code knows {SCHEMA_VERSION}")
    want = expected_objects()
    if live != want:
        raise StateMismatch(f"collector.db schema objects differ from this code: missing {sorted(set(want) - set(live))[:4]}, "
                            f"extra {sorted(set(live) - set(want))[:4]}, altered {sorted(k for k in want if k in live and live[k] != want[k])[:4]}")
    return v


def default_path() -> Path:
    import config
    return Path(config.PROJECT_ROOT) / "runtime" / "market_intel" / STATE_FILE


def _breaker_from(r) -> PO.Breaker:
    return PO.Breaker(r["state"], int(r["failures"]), parse_ts(r["opened_at"]), float(r["cooldown_s"]), parse_ts(r["trial_at"]))


class CollectorState:
    def __init__(self, path: Optional[Path] = None, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS):
        self._path = Path(path) if path is not None else None
        self.busy_timeout_ms = int(busy_timeout_ms)

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_path()

    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def _ro(self) -> Iterator[Optional[sqlite3.Connection]]:
        if not self.exists():
            yield None
            return
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            yield None if verify(conn) == 0 else conn
        finally:
            conn.close()

    @contextmanager
    def _rw(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if verify(conn) == 0:
                    for st in DDL:
                        conn.execute(st)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    verify(conn)
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # ------------------------------------------------------------------------------------------------ leases
    def begin(self, *, source: str, scope_key: str, provider: str, worker: str, now: datetime, ttl_s: float,
              concurrency: int) -> Tuple[Optional[str], str, bool]:
        """(lease id, "OK", trial?) or (None, refusal, False) - in ONE transaction: the breaker gate (CIRCUIT_OPEN / CIRCUIT_TRIAL_RUNNING),
        one live lease per (source, scope) (LEASE_HELD), at most `concurrency` per provider (PROVIDER_CONCURRENCY), the lease, and - when the
        breaker lets one trial through - its HALF_OPEN claim. A lease lives until released or until it expires (a crashed worker never
        blocks forever)."""
        lid = uuid.uuid4().hex
        with self._rw() as c:
            b = self._last_breaker(c, source)
            gate = PO.gate(b, now)
            if gate in (PO.CIRCUIT_OPEN, PO.TRIAL_RUNNING):
                return None, gate, False
            active = c.execute("SELECT * FROM cs_leases l WHERE l.expires_at > ? AND NOT EXISTS (SELECT 1 FROM cs_lease_releases r "
                               "WHERE r.lease_id = l.lease_id)", (ts(now),)).fetchall()
            if any(a["source"] == source and a["scope_key"] == scope_key for a in active):
                return None, "LEASE_HELD", False
            if sum(1 for a in active if a["provider"] == provider) >= concurrency:
                return None, "PROVIDER_CONCURRENCY", False
            c.execute("INSERT INTO cs_leases (lease_id, source, scope_key, provider, worker, acquired_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                      (lid, source, scope_key, provider, worker, ts(now), ts(now + timedelta(seconds=ttl_s))))
            if gate == PO.TRIAL:
                self._breaker_event(c, source, PO.start_trial(b, now), now, None)
        return lid, "OK", gate == PO.TRIAL

    def abandon(self, lease_id: str, source: str, now: datetime, *, trial: bool) -> None:
        """A run that could not start: its lease is released and a trial claim is handed back (the breaker is OPEN again, cool-down passed)."""
        with self._rw() as c:
            if c.execute("SELECT 1 FROM cs_lease_releases WHERE lease_id = ?", (lease_id,)).fetchone() is None:
                c.execute("INSERT INTO cs_lease_releases (lease_id, released_at) VALUES (?, ?)", (lease_id, ts(now)))
            if trial:
                self._hand_back(c, source, now)

    def hand_back_trial(self, source: str, now: datetime) -> None:
        """A trial run that made no request: the breaker is OPEN again (cool-down passed), never closed without evidence."""
        with self._rw() as c:
            self._hand_back(c, source, now)

    def _hand_back(self, c: sqlite3.Connection, source: str, now: datetime) -> None:
        b = self._last_breaker(c, source)
        if b.state == PO.HALF_OPEN:
            self._breaker_event(c, source, PO.Breaker(PO.OPEN, b.failures, b.opened_at, b.cooldown_s, None), now, None)

    def release_lease(self, lease_id: str, now: datetime) -> None:
        with self._rw() as c:
            if c.execute("SELECT 1 FROM cs_lease_releases WHERE lease_id = ?", (lease_id,)).fetchone() is None:
                c.execute("INSERT INTO cs_lease_releases (lease_id, released_at) VALUES (?, ?)", (lease_id, ts(now)))

    def lease_active(self, lease_id: str, now: datetime) -> bool:
        with self._ro() as c:
            if c is None:
                return False
            r = c.execute("SELECT expires_at FROM cs_leases l WHERE lease_id = ? AND NOT EXISTS (SELECT 1 FROM cs_lease_releases r "
                          "WHERE r.lease_id = l.lease_id)", (lease_id,)).fetchone()
        return r is not None and r["expires_at"] > ts(now)

    # ------------------------------------------------------------------------------------------------ runs
    def start_run(self, *, run_id: str, source: str, scope_key: str, scope: dict, worker: str, lease_id: str, plan_sha256: str, started_at: datetime,
                  window: Optional[Tuple[datetime, datetime]], gap: Optional[Tuple[datetime, datetime]], dataset_id: str, trial: bool) -> None:
        with self._rw() as c:
            c.execute("""INSERT INTO cs_runs (run_id, source, scope_key, scope_json, worker, lease_id, plan_sha256, started_at, window_start, window_end,
                         gap_start, gap_end, dataset_id, trial) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (run_id, source, scope_key, json.dumps(scope, sort_keys=True), worker, lease_id, plan_sha256, ts(started_at),
                       ts(window[0]) if window else None, ts(window[1]) if window else None, ts(gap[0]) if gap else None, ts(gap[1]) if gap else None,
                       dataset_id, int(trial)))

    def end_run(self, run_id: str, *, status: str, now: datetime, stop: Optional[str], failed: bool, clock_skew: bool, last_error: str,
                content_sha256: Optional[str], counts: dict, watermark: Optional[datetime] = None, verified: bool = False,
                breaker_failed: Optional[bool] = None) -> None:
        """The run end, its watermark (the schema refuses one for an incomplete run) and the breaker transition (read and written here, so
        two runs ending at once never lose one) - all or nothing."""
        with self._rw() as c:
            run = c.execute("SELECT source, scope_key FROM cs_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise StateError(f"unknown run {run_id}")
            c.execute("""INSERT INTO cs_run_ends (run_id, status, ended_at, stop, failed, clock_skew, verified, last_error, content_sha256, counts_json)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""", (run_id, status, ts(now), stop, int(failed), int(clock_skew), int(bool(verified)), last_error[:300],
                                                           content_sha256, json.dumps(counts, sort_keys=True)))
            if watermark is not None:
                c.execute("INSERT INTO cs_watermarks (source, scope_key, run_id, watermark, at) VALUES (?,?,?,?,?)",
                          (run["source"], run["scope_key"], run_id, ts(watermark), ts(now)))
            if breaker_failed is not None:
                self._breaker_event(c, run["source"], PO.after_run(self._last_breaker(c, run["source"]), breaker_failed, now), now, run_id)

    @staticmethod
    def _last_breaker(c: sqlite3.Connection, source: str) -> PO.Breaker:
        r = c.execute("SELECT * FROM cs_breaker_events WHERE source = ? ORDER BY seq DESC LIMIT 1", (source,)).fetchone()
        return PO.Breaker() if r is None else _breaker_from(r)

    @staticmethod
    def _breaker_event(c: sqlite3.Connection, source: str, b: PO.Breaker, now: datetime, run_id: Optional[str]) -> None:
        if CollectorState._last_breaker(c, source) == b:
            return                                                       # only transitions are kept (bounded growth)
        c.execute("INSERT INTO cs_breaker_events (source, at, state, failures, opened_at, cooldown_s, trial_at, run_id) VALUES (?,?,?,?,?,?,?,?)",
                  (source, ts(now), b.state, b.failures, ts(b.opened_at), float(b.cooldown_s), ts(b.trial_at), run_id))

    # ------------------------------------------------------------------------------------------------ reads (never create the file)
    def breaker(self, source: str) -> PO.Breaker:
        with self._ro() as c:
            r = None if c is None else c.execute("SELECT * FROM cs_breaker_events WHERE source = ? ORDER BY seq DESC LIMIT 1", (source,)).fetchone()
        return PO.Breaker() if r is None else _breaker_from(r)

    def breaker_events(self, source: str) -> List[dict]:
        with self._ro() as c:
            return [] if c is None else [dict(r) for r in c.execute("SELECT * FROM cs_breaker_events WHERE source = ? ORDER BY seq", (source,))]

    def watermark(self, source: str, scope_key: str) -> Optional[datetime]:
        with self._ro() as c:
            r = None if c is None else c.execute("SELECT watermark FROM cs_watermarks WHERE source = ? AND scope_key = ? ORDER BY seq DESC LIMIT 1",
                                                 (source, scope_key)).fetchone()
        return None if r is None else parse_ts(r["watermark"])

    def watermarks(self, source: str, scope_key: str) -> List[dict]:
        with self._ro() as c:
            return [] if c is None else [dict(r) for r in c.execute("SELECT * FROM cs_watermarks WHERE source = ? AND scope_key = ? ORDER BY seq",
                                                                    (source, scope_key))]

    _RUNS = ("SELECT r.*, e.status, e.ended_at, e.stop, e.failed, e.clock_skew, e.verified, e.last_error, e.content_sha256, e.counts_json FROM cs_runs r "
             "LEFT JOIN cs_run_ends e ON e.run_id = r.run_id")

    @staticmethod
    def _run(r) -> dict:
        d = dict(r)
        d["scope"] = json.loads(d.pop("scope_json"))
        d["counts"] = json.loads(d.pop("counts_json")) if d.get("counts_json") else {}
        d.pop("counts_json", None)
        return d

    def runs(self, source: Optional[str] = None, scope_key: Optional[str] = None) -> List[dict]:
        q, args = self._RUNS, []
        if source is not None:
            q, args = q + " WHERE r.source = ?", [source]
            if scope_key is not None:
                q, args = q + " AND r.scope_key = ?", args + [scope_key]
        with self._ro() as c:
            return [] if c is None else [self._run(r) for r in c.execute(q + " ORDER BY r.started_at, r.rowid", args)]

    def last_run(self, source: str, scope_key: str) -> Optional[dict]:
        with self._ro() as c:
            r = None if c is None else c.execute(self._RUNS + " WHERE r.source = ? AND r.scope_key = ? ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1",
                                                 (source, scope_key)).fetchone()
        return None if r is None else self._run(r)

    def last_success(self, source: str, scope_key: str) -> Optional[dict]:
        with self._ro() as c:
            r = None if c is None else c.execute(self._RUNS + " WHERE r.source = ? AND r.scope_key = ? AND e.status IN ('COMPLETED', 'EMPTY') "
                                                 "AND e.verified = 1 ORDER BY e.ended_at DESC, r.rowid DESC LIMIT 1", (source, scope_key)).fetchone()
        return None if r is None else self._run(r)

    def last_ended_run(self, source: str, scope_key: str) -> Optional[dict]:
        with self._ro() as c:
            r = None if c is None else c.execute(self._RUNS + " WHERE r.source = ? AND r.scope_key = ? AND e.status IS NOT NULL "
                                                 "ORDER BY e.ended_at DESC, r.rowid DESC LIMIT 1", (source, scope_key)).fetchone()
        return None if r is None else self._run(r)

    def gaps(self, source: str, scope_key: str) -> List[dict]:
        """Every window a run could not cover without a historical approval (recorded at the run's start, never guessed or filled)."""
        with self._ro() as c:
            return [] if c is None else [dict(r) for r in c.execute("SELECT DISTINCT gap_start, gap_end FROM cs_runs WHERE source = ? AND scope_key = ? "
                                                                    "AND gap_start IS NOT NULL ORDER BY gap_start", (source, scope_key))]

    def open_runs(self) -> List[dict]:
        with self._ro() as c:
            return [] if c is None else [self._run(r) for r in c.execute(self._RUNS + " WHERE e.run_id IS NULL ORDER BY r.started_at, r.rowid")]
