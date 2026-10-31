"""
market_intel/store.py - Stage 7.1: the market-intelligence store, its OWN SQLite file (data/market_intel.db; DESIGN_71 section 2).

Separate from the trading database: the executor and the safety gate never contend with ingestion, and nothing here imports a trading,
broker, model or setup module. Created lazily: constructing a store, importing this module or calling any read creates nothing; only an
explicit write (create_dataset, ingest, ...) creates the file and migrates it (migrations.py).

Point in time (the Stage 7.0 contracts, DESIGN_71 section 3):
* Five times per observation: observed_at (event / market time), published_at, retrieved_at, available_at, ingested_at - all UTC, stored
  in one fixed-width format so text order is time order.
* A row is KNOWN at T when available_at <= T and ingested_at <= T. Every point-in-time read filters on both, so a 2026 backfill of 2025
  bars (backfilled: available_at == retrieved_at) is invisible to any evaluation time before it was fetched - never look-ahead.
* A fact has a natural key (kind, symbol / series, provider, feed, adjustment, observed_at, source identifier). Re-ingesting identical
  content is a duplicate (skipped, counted); different content is a new REVISION that supersedes the previous one; nothing is overwritten.
  The revision known at T is the highest one known at T.
* Feeds never mix: bar and quote reads require an explicit feed; the natural key includes it.
* Missing is never zero: a read with nothing usable returns None or an explicit UNAVAILABLE entry, never a placeholder value.
* Unknown calendar coverage fails closed: a bar or quote on an uncovered weekday is refused at ingestion.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from market_intel import AVAILABLE, VERSION
from market_intel import calendar as CAL
from market_intel import validate as VA
from market_intel.migrations import SCHEMA_VERSION, migrate, verify
from market_intel.models import FetchRecord, Observation

STORE_FILE = "market_intel.db"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
FEED_KINDS = ("BAR_1MIN", "BAR_1HOUR", "BAR_1DAY", "QUOTE_SNAPSHOT")       # an explicit iex / sip feed is required
SESSION_KINDS = FEED_KINDS                                                 # a covered session date is required
CONTENT_FIELDS = ("kind", "symbol", "series_id", "provider", "feed", "adjustment", "observed_at", "published_at", "session", "session_date",
                  "status", "source_url", "source_identifier", "tier", "quality_flags", "payload")
FINAL_STATUSES = ("COMPLETED", "PARTIAL", "FAILED", "EMPTY")
BUSY_TIMEOUT_MS = 10_000


class StoreError(ValueError):
    """An ingestion or a query the store refuses (with the reason)."""


class FeedRequired(StoreError):
    """A bar / quote read or write without an explicit feed."""


class CoverageError(StoreError):
    """A session-bound observation on a weekday the calendar does not cover."""


def default_path() -> Path:
    import config
    return Path(config.PROJECT_ROOT) / "data" / STORE_FILE


def ts(d: Optional[datetime]) -> Optional[str]:
    if d is None:
        return None
    if d.tzinfo is None or d.tzinfo.utcoffset(d) is None:
        raise StoreError("times must be timezone-aware (UTC)")
    return d.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(s: Optional[str]) -> Optional[datetime]:
    return None if s is None else datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def natural_key(o: Observation) -> str:
    return "|".join((o.kind, o.symbol or "", o.series_id or "", o.provider, o.feed, o.adjustment, ts(o.observed_at) or "", o.source_identifier or ""))


def content_hash(o: Observation) -> str:
    """The fact itself - never when or how it was fetched (retrieved / available / ingested times, age, freshness, dataset, backfill)."""
    d = o.model_dump(mode="json")
    body = {k: d[k] for k in CONTENT_FIELDS}
    body["quality_flags"] = sorted(body["quality_flags"])
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatasetSpec:
    job_kind: str                       # BACKFILL | IMPORT | FIXTURE
    kind: str
    provider: str
    feed: str
    adjustment: str
    symbols: Sequence[str]
    timeframe: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    request: Dict = field(default_factory=dict)
    plan_sha256: Optional[str] = None
    approval_ref: Optional[str] = None


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    revised: int = 0
    obs_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class MarketIntelStore:
    def __init__(self, path: Optional[Path] = None, *, clock: Optional[Callable[[], datetime]] = None, busy_timeout_ms: int = BUSY_TIMEOUT_MS):
        self._path = Path(path) if path is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.busy_timeout_ms = int(busy_timeout_ms)

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_path()

    def _now(self) -> datetime:
        n = self.clock()
        if n.tzinfo is None:
            raise StoreError("the store clock must be timezone-aware")
        return n.astimezone(timezone.utc)

    # ------------------------------------------------------------------------------------------------ connections
    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def _ro(self) -> Iterator[Optional[sqlite3.Connection]]:
        """A read-only connection, or None when the store has not been created (reads never create it)."""
        if not self.exists():
            yield None
            return
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            yield None if verify(conn) == 0 else conn
        finally:
            conn.close()

    @contextmanager
    def _rw(self) -> Iterator[sqlite3.Connection]:
        """The single write path: creates and migrates the file when needed, then one BEGIN IMMEDIATE transaction (atomic; rolled back on error)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")            # concurrent readers while one writer writes
            conn.execute("PRAGMA synchronous = FULL")            # a committed batch survives a crash or a power loss
            conn.execute("PRAGMA foreign_keys = ON")
            migrate(conn, self._now())
            conn.execute("BEGIN IMMEDIATE")                      # writers are serialized; a reader never sees a half-written batch
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self._ro() as c:
            return 0 if c is None else int(c.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------------------------------------------------------------------------ writes (explicit only)
    def create_dataset(self, spec: DatasetSpec) -> str:
        if spec.job_kind not in ("BACKFILL", "IMPORT", "FIXTURE"):
            raise StoreError(f"unknown job kind {spec.job_kind}")
        if spec.kind in FEED_KINDS and spec.feed not in ("iex", "sip"):
            raise FeedRequired(f"a {spec.kind} dataset needs an explicit feed (iex or sip), not {spec.feed!r}")
        did = uuid.uuid4().hex
        now = ts(self._now())
        with self._rw() as c:
            c.execute("""INSERT INTO mi_datasets (dataset_id, job_kind, kind, provider, feed, adjustment, timeframe, symbols_json, start_date,
                         end_date, request_json, plan_sha256, approval_ref, created_at, code_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (did, spec.job_kind, spec.kind, spec.provider, spec.feed, spec.adjustment, spec.timeframe, canonical(sorted(spec.symbols)),
                       spec.start_date.isoformat() if spec.start_date else None, spec.end_date.isoformat() if spec.end_date else None,
                       canonical(spec.request), spec.plan_sha256, spec.approval_ref, now, f"{VERSION}/schema{SCHEMA_VERSION}"))
            c.execute("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES (?, 'RUNNING', ?)", (did, now))
        return did

    def record_fetch(self, fetch: FetchRecord, *, dataset_id: Optional[str], params: Optional[dict] = None, raw_path: Optional[str] = None,
                     fetch_id: Optional[str] = None) -> str:
        fid = fetch_id or uuid.uuid4().hex
        with self._rw() as c:
            c.execute("""INSERT INTO mi_fetches (fetch_id, dataset_id, provider, endpoint, params_json, started_at, retrieved_at, status, http_status,
                         latency_ms, bytes, content_hash, retry_after_s, error, raw_path, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (fid, dataset_id, fetch.provider, fetch.endpoint, canonical(params or {}), ts(fetch.started_at), ts(fetch.retrieved_at), fetch.status,
                       fetch.http_status, fetch.latency_ms, fetch.bytes, fetch.content_hash, fetch.retry_after_s, fetch.error, raw_path, ts(self._now())))
        return fid

    def _check(self, o: Observation, job_kind: str, dataset_feed: str) -> None:
        if not isinstance(o, Observation):
            raise StoreError("only market_intel.models.Observation envelopes are stored")
        if o.kind in FEED_KINDS:
            if o.feed not in ("iex", "sip"):
                raise FeedRequired(f"a {o.kind} observation needs an explicit feed (iex or sip)")
            if o.feed != dataset_feed:
                raise FeedRequired(f"a {o.feed} observation cannot join a {dataset_feed} dataset (feeds never mix)")
        if o.kind in SESSION_KINDS:
            if o.session_date is None:
                raise CoverageError(f"a {o.kind} observation needs its New York session date")
            if CAL.market_day_status(o.session_date) == CAL.NOT_COVERED:
                raise CoverageError(f"{o.session_date} is outside the calendar coverage ({CAL.calendar_version(o.session_date)}): refused")
        if job_kind in ("BACKFILL", "IMPORT") and not o.backfilled:
            raise StoreError("a backfill / import observation must be marked backfilled (available_at == retrieved_at)")

    def _annotate(self, o: Observation) -> Observation:
        """Add the calendar's own codes (an annotation, never a repair): a daily bar on a non-session day, an intraday bar outside the sessions."""
        flags = set(o.quality_flags)
        if o.kind == "BAR_1DAY" and CAL.market_day_status(o.session_date) != CAL.MARKET_DAY:
            flags.add("NON_SESSION_DAY")
        if o.kind in ("BAR_1MIN", "BAR_1HOUR") and o.observed_at is not None and CAL.session_of(o.observed_at) == "CLOSED":
            flags.add("OUTSIDE_SESSION")
        return o if flags == set(o.quality_flags) else o.model_copy(update={"quality_flags": tuple(sorted(flags))})

    def ingest(self, dataset_id: str, observations: Iterable[Observation], *, fetch_id: Optional[str] = None) -> IngestResult:
        """All of `observations` in ONE transaction - all stored or none. Identical content: duplicate; changed content: a new revision."""
        res = IngestResult()
        obs = list(observations)
        now = ts(self._now())
        with self._rw() as c:
            ds = c.execute("SELECT job_kind, feed, kind FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            if ds is None:
                raise StoreError(f"unknown dataset {dataset_id}")
            last = c.execute("SELECT status FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id DESC LIMIT 1", (dataset_id,)).fetchone()
            if last["status"] in FINAL_STATUSES:
                raise StoreError(f"dataset {dataset_id} is already {last['status']}: a sealed dataset takes no rows")
            for o in obs:
                self._check(o, ds["job_kind"], ds["feed"])
                o = self._annotate(o)
                key, h = natural_key(o), content_hash(o)
                prev = c.execute("SELECT obs_id, revision, content_hash FROM mi_observations WHERE natural_key = ? ORDER BY revision DESC LIMIT 1",
                                 (key,)).fetchone()
                if prev is not None and prev["content_hash"] == h:
                    res.duplicates += 1
                    continue
                oid = uuid.uuid4().hex
                c.execute("""INSERT INTO mi_observations (obs_id, natural_key, revision, supersedes_obs_id, content_hash, dataset_id, fetch_id, kind,
                             symbol, series_id, provider, feed, adjustment, observed_at, published_at, retrieved_at, available_at, ingested_at, session,
                             session_date, status, freshness, age_s, source_url, source_identifier, tier, quality_flags_json, backfilled, payload_json,
                             envelope_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (oid, key, (prev["revision"] + 1) if prev else 1, prev["obs_id"] if prev else None, h, dataset_id, fetch_id, o.kind,
                           o.symbol, o.series_id, o.provider, o.feed, o.adjustment, ts(o.observed_at), ts(o.published_at), ts(o.retrieved_at),
                           ts(o.available_at), now, o.session, o.session_date.isoformat() if o.session_date else None, o.status, o.freshness, o.age_s,
                           o.source_url, o.source_identifier, o.tier, canonical(sorted(o.quality_flags)), int(o.backfilled),
                           canonical(o.model_dump(mode="json")["payload"]), VERSION))
                res.obs_ids.append(oid)
                if prev:
                    res.revised += 1
                else:
                    res.inserted += 1
        return res

    def finish_dataset(self, dataset_id: str, status: str, *, detail: str = "", duplicates: int = 0) -> dict:
        """Seal a dataset. A failed fetch counts as RECOVERED only when the same request (identical params) later succeeded in this dataset.
        COMPLETED and EMPTY need every failed fetch recovered, and EMPTY needs zero rows: missing data is never presented as no data."""
        if status not in FINAL_STATUSES:
            raise StoreError(f"a dataset ends COMPLETED, PARTIAL, FAILED or EMPTY, not {status}")
        with self._rw() as c:
            rows = c.execute("SELECT content_hash FROM mi_observations WHERE dataset_id = ? ORDER BY content_hash", (dataset_id,)).fetchall()
            bad = c.execute("""SELECT COUNT(*) FROM mi_fetches f WHERE f.dataset_id = ? AND f.status NOT IN ('OK', 'NOT_MODIFIED') AND NOT EXISTS (
                                 SELECT 1 FROM mi_fetches g WHERE g.dataset_id = f.dataset_id AND g.params_json = f.params_json
                                 AND g.status IN ('OK', 'NOT_MODIFIED') AND g.started_at >= f.started_at)""", (dataset_id,)).fetchone()[0]
            if status == "EMPTY" and (rows or bad):
                raise StoreError("EMPTY needs zero rows and no unrecovered failed fetch")
            if status == "COMPLETED" and bad:
                raise StoreError("a dataset with an unrecovered failed fetch is PARTIAL or FAILED, never COMPLETED")
            sha = hashlib.sha256("\n".join(r[0] for r in rows).encode("utf-8")).hexdigest()
            counts = c.execute("SELECT SUM(revision = 1), SUM(revision > 1) FROM mi_observations WHERE dataset_id = ?", (dataset_id,)).fetchone()
            c.execute("""INSERT INTO mi_dataset_events (dataset_id, status, at, rows_inserted, rows_duplicate, rows_revised, content_sha256, detail)
                         VALUES (?,?,?,?,?,?,?,?)""", (dataset_id, status, ts(self._now()), counts[0] or 0, int(duplicates), counts[1] or 0, sha, detail[:500]))
        return {"dataset_id": dataset_id, "status": status, "rows": len(rows), "content_sha256": sha, "failed_fetches": bad}

    def record_budget_event(self, provider: str, event: str, *, reason: str = "", http_status: Optional[int] = None, retry_after_s: Optional[float] = None,
                            per_minute: Optional[float] = None, used_today: Optional[int] = None, dataset_id: Optional[str] = None) -> None:
        with self._rw() as c:
            c.execute("""INSERT INTO mi_budget_events (at, provider, event, reason, http_status, retry_after_s, per_minute, used_today, dataset_id)
                         VALUES (?,?,?,?,?,?,?,?,?)""", (ts(self._now()), provider, event, reason, http_status, retry_after_s, per_minute, used_today, dataset_id))

    def record_quality_audit(self, report: dict) -> None:
        with self._rw() as c:
            c.execute("""INSERT INTO mi_quality_audits (at, dataset_id, scope_json, check_name, result, codes_json, detail_json)
                         VALUES (?,?,?,?,?,?,?)""", (ts(self._now()), report.get("dataset_id"), canonical(report["scope"]), report["check"], report["result"],
                                                     canonical(report["codes"]), canonical({k: v for k, v in report.items() if k not in ("scope", "codes")})))

    # ------------------------------------------------------------------------------------------------ reads (read-only; never create)
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["quality_flags"] = json.loads(d.pop("quality_flags_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        d["usable"] = d["status"] == AVAILABLE and not VA.blocking(d["quality_flags"])
        return d

    def _scope(self, kind: str, symbol: Optional[str], series_id: Optional[str], feed: Optional[str], adjustment: Optional[str]):
        if kind in FEED_KINDS and feed not in ("iex", "sip"):
            raise FeedRequired(f"reading {kind} needs an explicit feed (iex or sip): feeds are never mixed")
        where, args = ["kind = ?"], [kind]
        for col, val in (("symbol", symbol.upper() if symbol else None), ("series_id", series_id), ("feed", feed), ("adjustment", adjustment)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        return where, args

    def _known_rows(self, c, where: List[str], args: list, at: str) -> List[dict]:
        """Per natural key, the highest revision KNOWN at `at` (available_at <= at AND ingested_at <= at)."""
        sql = f"""SELECT o.* FROM mi_observations o JOIN (
                    SELECT natural_key, MAX(revision) AS rev FROM mi_observations
                    WHERE {' AND '.join(where)} AND available_at <= ? AND ingested_at <= ?
                    GROUP BY natural_key) k ON o.natural_key = k.natural_key AND o.revision = k.rev
                  ORDER BY o.observed_at, o.natural_key"""
        return [self._row(r) for r in c.execute(sql, [*args, at, at])]

    def history(self, kind: str, *, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                adjustment: Optional[str] = None, start: Optional[datetime] = None, end: Optional[datetime] = None, as_of: Optional[datetime] = None,
                usable_only: bool = False) -> List[dict]:
        """Observations by event time [start, end], each fact's revision known at `as_of` (default: now). Deterministic order."""
        where, args = self._scope(kind, symbol, series_id, feed, adjustment)
        if start is not None:
            where.append("observed_at >= ?")
            args.append(ts(start))
        if end is not None:
            where.append("observed_at <= ?")
            args.append(ts(end))
        with self._ro() as c:
            if c is None:
                return []
            rows = self._known_rows(c, where, args, ts(as_of or self._now()))
        return [r for r in rows if r["usable"]] if usable_only else rows

    def latest_usable(self, kind: str, *, at: datetime, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                      adjustment: Optional[str] = None) -> Optional[dict]:
        """The latest usable fact (by event time) known at `at`, or None. None means UNAVAILABLE - never a zero."""
        rows = self.history(kind, symbol=symbol, series_id=series_id, feed=feed, adjustment=adjustment, end=at, as_of=at, usable_only=True)
        return rows[-1] if rows else None

    def snapshot(self, kind: str, symbols: Sequence[str], *, at: datetime, feed: Optional[str] = None, adjustment: Optional[str] = None) -> Dict[str, dict]:
        """Per symbol, the latest usable observation known at `at`, or an explicit UNAVAILABLE entry."""
        out = {}
        for s in sorted({x.upper() for x in symbols}):
            r = self.latest_usable(kind, at=at, symbol=s, feed=feed, adjustment=adjustment)
            out[s] = r if r is not None else {"symbol": s, "kind": kind, "feed": feed, "status": "UNAVAILABLE", "usable": False,
                                              "reason": "NO_USABLE_OBSERVATION_KNOWN_AT", "at": ts(at)}
        return out

    def revisions(self, natural_key_: str) -> List[dict]:
        with self._ro() as c:
            if c is None:
                return []
            return [self._row(r) for r in c.execute("SELECT * FROM mi_observations WHERE natural_key = ? ORDER BY revision", (natural_key_,))]

    def provenance(self, obs_id: str) -> Optional[dict]:
        """The observation, every revision of its fact, the fetch that brought it (with the raw-archive path) and its dataset with events."""
        with self._ro() as c:
            if c is None:
                return None
            r = c.execute("SELECT * FROM mi_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            if r is None:
                return None
            o = self._row(r)
            fetch = c.execute("SELECT * FROM mi_fetches WHERE fetch_id = ?", (o["fetch_id"],)).fetchone() if o["fetch_id"] else None
            ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (o["dataset_id"],)).fetchone()
            ev = c.execute("SELECT status, at, rows_inserted, rows_duplicate, rows_revised, content_sha256, detail FROM mi_dataset_events "
                           "WHERE dataset_id = ? ORDER BY event_id", (o["dataset_id"],)).fetchall()
            revs = c.execute("SELECT obs_id, revision, content_hash, available_at, ingested_at FROM mi_observations WHERE natural_key = ? ORDER BY revision",
                             (o["natural_key"],)).fetchall()
        return {"observation": o, "revisions": [dict(x) for x in revs], "fetch": dict(fetch) if fetch else None,
                "dataset": dict(ds) if ds else None, "dataset_events": [dict(x) for x in ev]}

    def dataset(self, dataset_id: str) -> Optional[dict]:
        with self._ro() as c:
            if c is None:
                return None
            ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            if ds is None:
                return None
            ev = [dict(x) for x in c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id", (dataset_id,))]
        return {**dict(ds), "events": ev, "status": ev[-1]["status"] if ev else None}

    def budget_used(self, provider: str, day: date) -> int:
        """ALLOW events of `provider` on New York date `day` (the persisted request count the daily caps read)."""
        with self._ro() as c:
            if c is None:
                return 0
            rows = c.execute("SELECT at FROM mi_budget_events WHERE provider = ? AND event = 'ALLOW'", (provider,)).fetchall()
        return sum(1 for (a,) in rows if CAL.session(parse_ts(a))["session_date"] == day)

    def quality_report(self, kind: str, *, symbol: str, feed: str, start: date, end: date, adjustment: Optional[str] = None,
                       as_of: Optional[datetime] = None) -> dict:
        """Daily bars: the expected sessions (calendar), the present ones, the MISSING sessions listed (never filled), validation codes.
        BLOCKED when coverage is unknown, a session hole or a blocking code exists; WARN on single missing sessions or warnings."""
        if kind != "BAR_1DAY":
            raise StoreError("Stage 7.1 reports daily-bar completeness; other kinds report counts only (status_counts)")
        days, uncovered = [], []
        x = start
        while x <= end:
            st = CAL.market_day_status(x)
            if st == CAL.MARKET_DAY:
                days.append(x)
            elif st == CAL.NOT_COVERED:
                uncovered.append(x.isoformat())
            x = date.fromordinal(x.toordinal() + 1)
        rows = self.history("BAR_1DAY", symbol=symbol, feed=feed, adjustment=adjustment, as_of=as_of)
        rows = [r for r in rows if r["session_date"] and start.isoformat() <= r["session_date"] <= end.isoformat()]
        usable = {r["session_date"]: r for r in rows if r["usable"]}
        missing = [d.isoformat() for d in days if d.isoformat() not in usable]
        series = VA.daily_series_codes([{**r["payload"], "day": date.fromisoformat(r["session_date"])} for _, r in sorted(usable.items())])
        holes, run = [], []                                          # runs of consecutive missing expected sessions (interior or at the edges)
        for d in days + [None]:
            if d is not None and d.isoformat() not in usable:
                run.append(d)
                continue
            if len(run) >= VA.SESSION_HOLE_DAYS:
                holes.append({"first_missing": run[0].isoformat(), "last_missing": run[-1].isoformat(), "market_days": len(run)})
            run = []
        codes = sorted(set(series["codes"]) | ({"CALENDAR_NOT_COVERED"} if uncovered else set()) | ({"SESSION_HOLE"} if holes else set()))
        result = "BLOCKED" if (uncovered or series["blocked"] or holes) else ("WARN" if (missing or codes) else "PASS")
        return {"check": "daily_bar_completeness", "scope": {"kind": kind, "symbol": symbol.upper(), "feed": feed, "adjustment": adjustment,
                                                             "start": start.isoformat(), "end": end.isoformat()},
                "result": result, "codes": codes, "expected_sessions": len(days), "present_sessions": len(usable), "missing_sessions": missing,
                "uncovered_weekdays": uncovered, "holes": holes, "jumps": series["jumps"],
                "unusable_rows": sum(1 for r in rows if not r["usable"]), "as_of": ts(as_of or self._now())}

    def status_counts(self, *, dataset_id: Optional[str] = None) -> dict:
        with self._ro() as c:
            if c is None:
                return {"store": "NOT_CREATED"}
            where, args = ("WHERE dataset_id = ?", (dataset_id,)) if dataset_id else ("", ())
            rows = c.execute(f"SELECT kind, feed, status, COUNT(*) FROM mi_observations {where} GROUP BY kind, feed, status ORDER BY 1, 2, 3", args).fetchall()
            tables = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("mi_datasets", "mi_fetches", "mi_observations", "mi_budget_events",
                                                                                         "mi_quality_audits")}
        return {"store": "OK", "schema_version": self.schema_version(), "tables": tables, "by_kind_feed_status": [list(r) for r in rows]}
