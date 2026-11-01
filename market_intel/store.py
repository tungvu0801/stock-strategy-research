"""
market_intel/store.py - Stage 7.1: the market-intelligence store, its OWN SQLite file (data/market_intel.db; DESIGN_71 sections 2-3).

Separate from the trading database; nothing here imports a trading, broker, model or setup module. Created lazily: constructing a store,
importing this module or calling any read creates nothing; only an explicit write creates and migrates the file (migrations.py), and
every open verifies the live schema objects.

Writes (one BEGIN IMMEDIATE transaction each; all or nothing):
* datasets are validated (kind / provider / feed / adjustment / timeframe / symbols / covered date range). A BACKFILL dataset exists only
  with VERIFIED operator authority: the store itself re-reads the approval file, recomputes the plan from the dataset's request, checks
  them (approvals.check) and requires the dataset spec to be exactly that plan's; the approval is claimed once, with its caps, in the same
  transaction;
* every observation is checked against its dataset's scope; its session date and session label are DERIVED from its timestamp (never
  trusted as labels); a bar must be aligned to its timeframe; a row that names a fetch must match that fetch (endpoint, symbol, feed,
  adjustment, timeframe, time window, retrieval time) and a BACKFILL row must name one; chronology (nothing observed, completed or published
  after it was retrieved) and, for bars, the OHLCV payload are checked - refused, or quarantined as a REJECTED member;
* rows outside the approved sessions (a regular-hours dataset receiving pre- or post-market bars) are EXCLUDED members: recorded, never
  stored as values;
* VALUES go to mi_observations as revisions of a natural key (identical content = DUPLICATE, changed content = a new revision);
  ABSENCES (UNAVAILABLE / INSUFFICIENT_DATA / NOT_APPLICABLE) go to mi_absences and never supersede or hide a value;
* every row a dataset delivered is a member, with its own fetch and delivery times (a duplicate value included);
* a dataset never holds more delivered rows than its cap: min(MAX_DATASET_ROWS, the approval's max_rows) - at every entry point;
* a sealed dataset takes nothing more, through any method (and the schema's own guards refuse raw SQL too).

Point in time - OPERATIONAL knowledge, never a research shortcut:
* a value is KNOWN at T when available_at <= T, ingested_at <= T, observed_at <= T and its event_end <= T (a bar still forming at T is
  never a completed fact at T);
* latest_status() grades the latest usable value with the Stage 7.0 freshness policy: an old value is never "current" because its row
  says AVAILABLE;
* research availability (event_end + a pinned latency, with a vintage policy) is a separate, later design (Stage 7.6); no API offers it.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from market_intel import ADJUSTMENTS, AVAILABLE, FEEDS, INSUFFICIENT_DATA, KINDS, NOT_APPLICABLE, PROVIDERS, UNAVAILABLE, VERSION
from market_intel import calendar as CAL
from market_intel import freshness as FR
from market_intel import validate as VA
from market_intel.migrations import SCHEMA_VERSION, SchemaMismatch, SchemaTooNew, migrate, verify
from market_intel.models import FetchRecord, Observation
from market_intel.sources import alpaca_bars as AB

STORE_FILE = "market_intel.db"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
BAR_KINDS = {"BAR_1MIN": "1Min", "BAR_1HOUR": "1Hour", "BAR_1DAY": "1Day"}
FEED_KINDS = (*BAR_KINDS, "QUOTE_SNAPSHOT")               # an explicit iex / sip feed is required
SESSION_KINDS = FEED_KINDS                                 # a covered New York session date is required
ABSENCE_STATUSES = (UNAVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE)
BAR_FIELDS = ("open", "high", "low", "close", "volume")
FINAL_STATUSES = ("COMPLETED", "PARTIAL", "FAILED", "EMPTY")
VALUE_DISPOSITIONS = ("INSERTED", "REVISED", "DUPLICATE")
DISPOSITIONS = ("INSERTED", "REVISED", "DUPLICATE", "ABSENCE", "REJECTED", "EXCLUDED")
OK_FETCH = ("OK", "NOT_MODIFIED")
FETCH_ENDPOINTS = {"ALPACA": AB.ENDPOINT}                  # the bars endpoint a fetch must name for a row of that symbol
MAX_DATASET_ROWS = 2_500_000                               # every dataset, every entry point (DESIGN_71 section 7)
INFORMATIONAL_CODES = frozenset(("BACKFILLED",))           # provenance, not quality: never a warning in a quality report
SKEW = timedelta(seconds=VA.CLOCK_SKEW_S)
NEWS_SKEW = timedelta(seconds=VA.NEWS_SKEW_S)
BUSY_TIMEOUT_MS = 10_000
VERIFIED, UNVERIFIED, CORRUPT = "VERIFIED", "UNVERIFIED", "CORRUPT"


class StoreError(ValueError):
    """An ingestion or a query the store refuses (with the reason)."""


class FeedRequired(StoreError):
    """A bar / quote read or write without an explicit feed, or a feed other than its dataset's."""


class CoverageError(StoreError):
    """A session-bound observation, or a dataset range, on a weekday the calendar does not cover."""


class ScopeError(StoreError):
    """An observation outside its dataset's / fetch's scope, a label its timestamp contradicts, or an invalid dataset spec."""


class ChronologyError(StoreError):
    """An observation observed, completed or published after it was retrieved (it could not have existed yet)."""


class PayloadError(StoreError):
    """A bar whose payload lacks OHLCV, or carries a non-finite / non-numeric value."""


class SealedError(StoreError):
    """A write that names a sealed dataset: a seal is final."""


class RowCapError(StoreError):
    """A batch that would take a dataset past its delivered-row cap, or a fetch past its approved request cap."""


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


def _iso(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("naive time")
    return d.astimezone(timezone.utc)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def natural_key(o: Observation) -> str:
    return "|".join((o.kind, o.symbol or "", o.series_id or "", o.provider, o.feed, o.adjustment, ts(o.observed_at) or "", o.source_identifier or ""))


def _content(kind, symbol, series_id, provider, feed, adjustment, observed_at, published_at, session, session_date, status, source_url,
             source_identifier, tier, flags, payload_json) -> str:
    """The fact in store-canonical form: recomputable from a stored row exactly (verify_integrity)."""
    return canonical({"kind": kind, "symbol": symbol, "series_id": series_id, "provider": provider, "feed": feed, "adjustment": adjustment,
                      "observed_at": observed_at, "published_at": published_at, "session": session, "session_date": session_date, "status": status,
                      "source_url": source_url, "source_identifier": source_identifier, "tier": tier, "quality_flags": sorted(flags),
                      "payload": json.loads(payload_json)})


def _payload_json(o: Observation) -> str:
    return canonical(o.model_dump(mode="json")["payload"])


def content_hash(o: Observation) -> str:
    """The fact itself - never when or how it was fetched (retrieved / available / ingested times, age, freshness, dataset, backfill)."""
    body = _content(o.kind, o.symbol, o.series_id, o.provider, o.feed, o.adjustment, ts(o.observed_at), ts(o.published_at), o.session,
                    o.session_date.isoformat() if o.session_date else None, o.status, o.source_url, o.source_identifier, o.tier, list(o.quality_flags),
                    _payload_json(o))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _row_hash(r: sqlite3.Row) -> str:
    """content_hash recomputed from a stored value or absence row (both tables hold every content column)."""
    body = _content(r["kind"], r["symbol"], r["series_id"], r["provider"], r["feed"], r["adjustment"], r["observed_at"], r["published_at"], r["session"],
                    r["session_date"], r["status"], r["source_url"], r["source_identifier"], r["tier"], json.loads(r["quality_flags_json"]), r["payload_json"])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def record_sha256(r) -> str:
    """The FULL stored row (every column, times and usability included): a sqlite3.Row read back, or the dict it was inserted from."""
    return hashlib.sha256(canonical(r if isinstance(r, dict) else {k: r[k] for k in r.keys()}).encode("utf-8")).hexdigest()


def _insert(c: sqlite3.Connection, table: str, row: dict) -> str:
    """INSERT one row from a dict and return its record hash. Every value is already the type its column stores (TEXT str, INTEGER int,
    REAL float), so the row read back later hashes the same (verify_integrity)."""
    c.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    return record_sha256(row)


def _common(o: Observation, key: str, h: str, dataset_id: str, fetch_id: Optional[str], now: str) -> dict:
    """The columns a value row and an absence row share."""
    return {"natural_key": key, "content_hash": h, "dataset_id": dataset_id, "fetch_id": fetch_id, "kind": o.kind, "symbol": o.symbol,
            "series_id": o.series_id, "provider": o.provider, "feed": o.feed, "adjustment": o.adjustment, "observed_at": ts(o.observed_at),
            "published_at": ts(o.published_at), "retrieved_at": ts(o.retrieved_at), "available_at": ts(o.available_at), "ingested_at": now,
            "session": o.session, "session_date": o.session_date.isoformat() if o.session_date else None, "status": o.status, "source_url": o.source_url,
            "source_identifier": o.source_identifier, "tier": None if o.tier is None else int(o.tier), "quality_flags_json": canonical(sorted(o.quality_flags)),
            "backfilled": int(o.backfilled), "payload_json": _payload_json(o), "envelope_version": VERSION}


def manifest_sha256(lines: Iterable[str]) -> str:
    """The DISTINCT facts a dataset delivered (natural key + content hash), independent of order and repeats: the same delivery hashes the
    same whatever the store already held."""
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def event_end(o: Observation) -> datetime:
    """When the fact was complete: a minute / hour bar's end; a daily bar's session late-trading end (17:00 ET on an NYSE early close,
    else 20:00 ET: the latest moment a print can enter the daily aggregate); any other kind: its observed (or published / retrieved) time."""
    if o.kind == "BAR_1MIN":
        return o.observed_at + timedelta(minutes=1)
    if o.kind == "BAR_1HOUR":
        return o.observed_at + timedelta(hours=1)
    if o.kind == "BAR_1DAY":
        end = CAL.post_market_end_minute(o.session_date) or (20 * 60)
        from zoneinfo import ZoneInfo
        return datetime(o.session_date.year, o.session_date.month, o.session_date.day, end // 60, end % 60,
                        tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    return o.observed_at or o.published_at or o.retrieved_at


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


def spec_key(spec: DatasetSpec) -> str:
    """A spec in canonical form (symbols as a list, dates ISO): two specs are the same dataset scope exactly when these are equal."""
    return canonical({"job_kind": spec.job_kind, "kind": spec.kind, "provider": spec.provider, "feed": spec.feed, "adjustment": spec.adjustment,
                      "symbols": list(spec.symbols), "timeframe": spec.timeframe, "start_date": spec.start_date.isoformat() if spec.start_date else None,
                      "end_date": spec.end_date.isoformat() if spec.end_date else None, "request": spec.request, "plan_sha256": spec.plan_sha256,
                      "approval_ref": spec.approval_ref})


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    revised: int = 0
    absences: int = 0
    rejected: int = 0
    excluded: int = 0
    obs_ids: List[str] = field(default_factory=list)
    rejections: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def validate_spec(spec: DatasetSpec) -> List[str]:
    """Every problem of a dataset spec (empty = valid)."""
    from api.validation import validate_symbol
    out = []
    if spec.job_kind not in ("BACKFILL", "IMPORT", "FIXTURE"):
        out.append(f"job_kind {spec.job_kind!r}")
    if spec.kind not in KINDS:
        out.append(f"kind {spec.kind!r}")
    if spec.provider not in PROVIDERS:
        out.append(f"provider {spec.provider!r}")
    if spec.feed not in FEEDS:
        out.append(f"feed {spec.feed!r}")
    if spec.adjustment not in ADJUSTMENTS:
        out.append(f"adjustment {spec.adjustment!r}")
    syms = list(spec.symbols or [])
    for s in syms:
        try:
            if validate_symbol(str(s)) != str(s):
                out.append(f"symbol {s!r} is not upper-case")
        except ValueError:
            out.append(f"symbol {s!r}")
    if len(set(syms)) != len(syms):
        out.append("duplicate symbols")
    if spec.kind in FEED_KINDS and spec.feed not in ("iex", "sip"):
        out.append(f"a {spec.kind} dataset needs feed iex or sip")
    if spec.kind in BAR_KINDS:
        if spec.timeframe != BAR_KINDS[spec.kind]:
            out.append(f"a {spec.kind} dataset has timeframe {BAR_KINDS[spec.kind]}, not {spec.timeframe!r}")
        if not syms:
            out.append("a bar dataset names its symbols")
        if spec.start_date is None or spec.end_date is None:
            out.append("a bar dataset has a start and an end date")
    if spec.start_date and spec.end_date and spec.start_date > spec.end_date:
        out.append("start_date after end_date")
    if not isinstance(spec.request, dict) or not isinstance(spec.request.get("extended_hours", False), bool):
        out.append("request is a dict whose extended_hours, when given, is a boolean")
    return out


class MarketIntelStore:
    def __init__(self, path: Optional[Path] = None, *, clock: Optional[Callable[[], datetime]] = None, busy_timeout_ms: int = BUSY_TIMEOUT_MS,
                 approvals_root: Optional[Path] = None):
        self._path = Path(path) if path is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._approvals_root = Path(approvals_root) if approvals_root is not None else None

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_path()

    @property
    def approvals_root(self) -> Path:
        """Where operator approval files live (the store re-reads them itself before a BACKFILL dataset exists)."""
        if self._approvals_root is not None:
            return self._approvals_root
        from market_intel import approvals as APP
        return APP.default_root()

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
        """A read-only connection, or None when the store has not been created (reads never create it). Verifies the schema objects; a
        populated file with user_version 0 is refused (SchemaMismatch), never read as an empty store."""
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
            migrate(conn, self._now())                           # verifies the live schema objects first
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

    @staticmethod
    def _open_dataset(c: sqlite3.Connection, dataset_id: str) -> sqlite3.Row:
        """The dataset a write names: it must exist and must not be sealed (checked inside the write's own transaction)."""
        ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if ds is None:
            raise StoreError(f"unknown dataset {dataset_id}")
        last = c.execute("SELECT status FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id DESC LIMIT 1", (dataset_id,)).fetchone()
        if last is not None and last["status"] in FINAL_STATUSES:
            raise SealedError(f"dataset {dataset_id} is already sealed {last['status']}: a sealed dataset takes no new row")
        return ds

    @staticmethod
    def _caps(c: sqlite3.Connection, dataset_id: str) -> Tuple[Optional[int], int]:
        """(approved request cap or None, delivered-row cap) of a dataset; the row cap never exceeds MAX_DATASET_ROWS."""
        use = c.execute("SELECT max_requests, max_rows FROM mi_approval_uses WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if use is None:
            return None, MAX_DATASET_ROWS
        return int(use["max_requests"]), min(int(use["max_rows"]), MAX_DATASET_ROWS)

    def _check_row_cap(self, c: sqlite3.Connection, dataset_id: str, adding: int) -> None:
        _, cap = self._caps(c, dataset_id)
        have = c.execute("SELECT COUNT(*) FROM mi_dataset_members WHERE dataset_id = ?", (dataset_id,)).fetchone()[0]
        if have + adding > cap:
            raise RowCapError(f"DELIVERED_ROW_CAP: {have} delivered + {adding} > the dataset's cap of {cap} rows (nothing of this batch was stored)")

    # ------------------------------------------------------------------------------------------------ writes (explicit only)
    def _authorize(self, spec: DatasetSpec, approval) -> object:
        """VERIFIED operator authority for a BACKFILL dataset - checked here, by the store, whoever calls: the approval must have been
        loaded from an approval file that still exists, hashes the same and is valid now; the plan RECOMPUTED now from the dataset's own
        request must be valid and hash to the approved plan; and the dataset spec must be exactly the one that plan allows."""
        from market_intel import approvals as APP
        from market_intel import ingest as IG
        if not isinstance(approval, APP.BackfillApproval):
            raise APP.ApprovalError("a BACKFILL dataset needs an operator approval loaded from an approval file")
        now = self._now()
        try:
            req = IG.request_from_dict(spec.request)
        except (KeyError, TypeError, ValueError) as exc:
            raise ScopeError("a BACKFILL dataset carries its exact BackfillRequest") from exc
        fresh = IG.plan(req, now)
        APP.check(approval, fresh, now=now, root=self.approvals_root)
        if spec_key(spec) != spec_key(IG.dataset_spec(fresh, approval)):
            raise ScopeError("the BACKFILL dataset spec is not the approved plan's own (kind, feed, adjustment, symbols, dates, request, hash, approval)")
        return approval

    def create_dataset(self, spec: DatasetSpec, *, approval=None) -> str:
        """Validate the spec (and the coverage of its range), verify a BACKFILL's operator authority (_authorize), create the dataset and -
        for an approved backfill - claim the approval, with its caps, in the same transaction (an approval is used once)."""
        problems = validate_spec(spec)
        if problems:
            if any("feed" in p for p in problems) and spec.kind in FEED_KINDS:
                raise FeedRequired("; ".join(problems))
            raise ScopeError("invalid dataset spec: " + "; ".join(problems))
        if spec.kind in SESSION_KINDS and spec.start_date and spec.end_date:
            x = spec.start_date
            while x <= spec.end_date:
                if CAL.market_day_status(x) == CAL.NOT_COVERED:
                    raise CoverageError(f"{x} is outside the calendar coverage: a dataset range must be covered")
                x += timedelta(days=1)
        if spec.job_kind == "BACKFILL":
            approval = self._authorize(spec, approval)
        elif approval is not None:
            raise ScopeError("only a BACKFILL dataset carries an operator approval")
        did = uuid.uuid4().hex
        now = ts(self._now())
        with self._rw() as c:
            c.execute("""INSERT INTO mi_datasets (dataset_id, job_kind, kind, provider, feed, adjustment, timeframe, symbols_json, start_date,
                         end_date, request_json, plan_sha256, approval_ref, created_at, code_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (did, spec.job_kind, spec.kind, spec.provider, spec.feed, spec.adjustment, spec.timeframe, canonical(sorted(spec.symbols)),
                       spec.start_date.isoformat() if spec.start_date else None, spec.end_date.isoformat() if spec.end_date else None,
                       canonical(spec.request), spec.plan_sha256, spec.approval_ref, now, f"{VERSION}/schema{SCHEMA_VERSION}"))
            if approval is not None:
                try:
                    c.execute("INSERT INTO mi_approval_uses (approval_id, plan_sha256, approval_file_sha256, dataset_id, used_at, max_requests, max_rows) "
                              "VALUES (?,?,?,?,?,?,?)", (approval.approval_id, approval.plan_sha256, approval.file_sha256, did, now,
                                                         int(approval.max_requests), int(approval.max_rows)))
                except sqlite3.IntegrityError as exc:
                    raise StoreError(f"approval {approval.approval_id} was already used: an approval runs once") from exc
            c.execute("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES (?, 'RUNNING', ?)", (did, now))
        return did

    def record_fetch(self, fetch: FetchRecord, *, dataset_id: Optional[str], params: Optional[dict] = None, raw_path: Optional[str] = None,
                     fetch_id: Optional[str] = None) -> str:
        """One provider attempt. Its dataset must be open; an approved dataset never records more attempts than its approval's max_requests."""
        fid = fetch_id or uuid.uuid4().hex
        with self._rw() as c:
            if dataset_id is not None:
                self._open_dataset(c, dataset_id)
                cap, _ = self._caps(c, dataset_id)
                n = c.execute("SELECT COUNT(*) FROM mi_fetches WHERE dataset_id = ?", (dataset_id,)).fetchone()[0]
                if cap is not None and n >= cap:
                    raise RowCapError(f"APPROVAL_REQUEST_CAP: dataset {dataset_id} already recorded {n} of its {cap} approved requests")
            c.execute("""INSERT INTO mi_fetches (fetch_id, dataset_id, provider, endpoint, params_json, started_at, retrieved_at, status, http_status,
                         latency_ms, bytes, content_hash, retry_after_s, error, raw_path, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (fid, dataset_id, fetch.provider, fetch.endpoint, canonical(params or {}), ts(fetch.started_at), ts(fetch.retrieved_at), fetch.status,
                       fetch.http_status, fetch.latency_ms, fetch.bytes, fetch.content_hash, fetch.retry_after_s, fetch.error, raw_path, ts(self._now())))
        return fid

    @staticmethod
    def _scope_reason(o: Observation, ds: sqlite3.Row, dataset_id: str) -> Optional[Tuple[type, str]]:
        """Why `o` does not belong to its dataset, or None. Every field of the dataset scope is checked."""
        if not isinstance(o, Observation):
            return ScopeError, "NOT_AN_OBSERVATION"
        if o.kind != ds["kind"]:
            return ScopeError, f"SCOPE_KIND:{o.kind}"
        if o.provider != ds["provider"]:
            return ScopeError, f"SCOPE_PROVIDER:{o.provider}"
        if o.feed != ds["feed"]:
            return FeedRequired, f"SCOPE_FEED:{o.feed}"
        if o.adjustment != ds["adjustment"]:
            return ScopeError, f"SCOPE_ADJUSTMENT:{o.adjustment}"
        syms = json.loads(ds["symbols_json"])
        if syms and o.symbol not in syms:
            return ScopeError, f"SCOPE_SYMBOL:{o.symbol}"
        if o.dataset_id not in (None, dataset_id):
            return ScopeError, "SCOPE_DATASET_ID"
        if o.content_hash is not None and o.content_hash != content_hash(o):
            return StoreError, "CONTENT_HASH_MISMATCH"
        if o.kind in SESSION_KINDS:
            if o.session_date is None:
                return CoverageError, "NO_SESSION_DATE"
            if CAL.market_day_status(o.session_date) == CAL.NOT_COVERED:
                return CoverageError, f"CALENDAR_NOT_COVERED:{o.session_date}"
        if ds["start_date"] and o.session_date and not ds["start_date"] <= o.session_date.isoformat() <= ds["end_date"]:
            return ScopeError, f"SCOPE_DATE:{o.session_date}"
        if ds["job_kind"] in ("BACKFILL", "IMPORT") and not o.backfilled:
            return StoreError, "BACKFILL_FLAG"
        return None

    @staticmethod
    def _derived_reason(o: Observation, ds: sqlite3.Row) -> Optional[Tuple[type, str]]:
        """Labels are checked against the timestamp, never trusted: the session date and session of `observed_at` (New York), the bar's
        alignment to its timeframe, and the payload's own timeframe."""
        if o.kind not in SESSION_KINDS or o.observed_at is None:
            return None
        s = CAL.session(o.observed_at)
        if s["session_date"] != o.session_date:
            return ScopeError, f"SESSION_DATE_MISMATCH:{o.session_date}"
        if o.kind == "BAR_1DAY":
            from zoneinfo import ZoneInfo
            et = o.observed_at.astimezone(ZoneInfo("America/New_York"))
            if (et.hour, et.minute, et.second, et.microsecond) != (0, 0, 0, 0):
                return ScopeError, "TIMEFRAME_MISALIGNED"
            if o.session != "REG":
                return ScopeError, f"SESSION_MISMATCH:{o.session}"
        else:
            if o.session != s["session"]:
                return ScopeError, f"SESSION_MISMATCH:{o.session}"
            u = o.observed_at.astimezone(timezone.utc)
            if o.kind == "BAR_1MIN" and (u.second or u.microsecond):
                return ScopeError, "TIMEFRAME_MISALIGNED"
            if o.kind == "BAR_1HOUR" and (u.minute or u.second or u.microsecond):
                return ScopeError, "TIMEFRAME_MISALIGNED"
        if o.kind in BAR_KINDS and "timeframe" in o.payload and o.payload["timeframe"] != ds["timeframe"]:
            return ScopeError, f"TIMEFRAME_MISMATCH:{o.payload['timeframe']}"
        return None

    @staticmethod
    def _fetch_reason(o: Observation, fetch: Optional[sqlite3.Row], params: dict, ds: sqlite3.Row) -> Optional[Tuple[type, str]]:
        """A row that names a fetch must be what that fetch asked for: endpoint, symbol, feed, adjustment, timeframe, time window - and it
        must carry that fetch's retrieval time."""
        if fetch is None:
            return None
        if o.kind in BAR_KINDS:
            missing = [k for k in ("symbol", "feed", "adjustment", "timeframe", "start", "end") if k not in params]
            if missing:
                return ScopeError, "FETCH_PARAMS_INCOMPLETE"
            for k, want in (("symbol", o.symbol), ("feed", o.feed), ("adjustment", o.adjustment), ("timeframe", ds["timeframe"])):
                if params[k] != want:
                    return ScopeError, f"FETCH_{k.upper()}:{params[k]}"
            try:
                lo, hi = _iso(params["start"]), _iso(params["end"])
            except (TypeError, ValueError):
                return ScopeError, "FETCH_WINDOW"
            if not lo <= o.observed_at < hi:
                return ScopeError, "FETCH_WINDOW"
            tmpl = FETCH_ENDPOINTS.get(ds["provider"])
            if tmpl and fetch["endpoint"] != tmpl.format(symbol=o.symbol):
                return ScopeError, "FETCH_ENDPOINT"
        if fetch["retrieved_at"] != ts(o.retrieved_at):
            return ScopeError, "FETCH_RETRIEVED_AT"
        return None

    @staticmethod
    def _chronology_reason(o: Observation) -> Optional[str]:
        """An observation cannot be observed, completed or published after we retrieved it (beyond the clock skew)."""
        if o.kind in BAR_KINDS and o.observed_at is None:
            return "NO_OBSERVED_AT"
        if o.observed_at is not None and o.observed_at > o.retrieved_at + SKEW:
            return "OBSERVED_AFTER_RETRIEVAL"
        if o.status not in ABSENCE_STATUSES and o.kind in BAR_KINDS and event_end(o) > o.retrieved_at + SKEW:
            return "BAR_NOT_COMPLETE_AT_RETRIEVAL"
        if o.published_at is not None and o.published_at > o.retrieved_at + NEWS_SKEW:
            return "PUBLISHED_AFTER_RETRIEVAL"
        return None

    @staticmethod
    def _payload_reason(o: Observation) -> Optional[str]:
        if o.kind not in BAR_KINDS or o.status in ABSENCE_STATUSES:
            return None
        for k in BAR_FIELDS:
            v = o.payload.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                return f"PAYLOAD_{k.upper()}"
        return None

    @staticmethod
    def _session_scope_reason(o: Observation, extended: bool) -> Optional[str]:
        """An intraday bar belongs to the approved sessions when its interval [start, start + timeframe) overlaps them: the regular session
        (09:30 to the close) or, for an extended-hours dataset, 04:00 to the late-trading end. So the 09:00 hour bar (09:00-10:00) is a
        regular-hours bar and the 16:00 one is not; a day without a session has none."""
        if o.kind not in ("BAR_1MIN", "BAR_1HOUR") or o.status in ABSENCE_STATUSES:
            return None
        s = CAL.session(o.observed_at)
        d = s["session_date"]
        if CAL.market_day_status(d) != CAL.MARKET_DAY or s["close_minute"] is None or s["et_minute"] is None:
            return "OUTSIDE_APPROVED_SESSIONS:NO_SESSION"
        m, length = s["et_minute"], (1 if o.kind == "BAR_1MIN" else 60)
        lo, hi = (4 * 60, CAL.post_market_end_minute(d)) if extended else (9 * 60 + 30, s["close_minute"])
        if not (m < hi and m + length > lo):
            return f"OUTSIDE_APPROVED_SESSIONS:{'EXTENDED' if extended else 'REGULAR'}"
        return None

    @staticmethod
    def _annotate(o: Observation) -> Observation:
        """Add the store's own codes (an annotation, never a repair): bar shape, a daily bar on a non-session day, an intraday bar outside the
        sessions. (backfilled is its own column; BACKFILLED stays the source's code.)"""
        flags = set(o.quality_flags)
        if o.kind in BAR_KINDS and o.status not in ABSENCE_STATUSES:
            flags.update(VA.bar_codes({k: o.payload.get(k) for k in BAR_FIELDS}))
        if o.kind == "BAR_1DAY" and CAL.market_day_status(o.session_date) != CAL.MARKET_DAY:
            flags.add("NON_SESSION_DAY")
        if o.kind in ("BAR_1MIN", "BAR_1HOUR") and o.observed_at is not None and CAL.session_of(o.observed_at) == "CLOSED":
            flags.add("OUTSIDE_SESSION")
        return o if flags == set(o.quality_flags) else o.model_copy(update={"quality_flags": tuple(sorted(flags))})

    @staticmethod
    def _member(c: sqlite3.Connection, dataset_id: str, key: str, h: Optional[str], record_id: Optional[str], rec: Optional[str], disposition: str,
                now: str, *, o: Optional[Observation] = None, fetch_id: Optional[str] = None, reason: str = "") -> None:
        """One delivery: which row, what became of it, the fetch that brought it and its delivery times (kept even for a duplicate)."""
        c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, record_sha256, fetch_id, retrieved_at, available_at, "
                  "disposition, reason, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (dataset_id, key, h, record_id, rec, fetch_id, ts(o.retrieved_at) if isinstance(o, Observation) else None,
                   ts(o.available_at) if isinstance(o, Observation) else None, disposition, reason[:120], now))

    def ingest(self, dataset_id: str, observations: Iterable[Observation], *, fetch_id: Optional[str] = None, on_reject: str = "raise") -> IngestResult:
        """All of `observations` in ONE transaction. on_reject='raise': any invalid observation refuses the whole batch (nothing stored).
        on_reject='quarantine': invalid ones become REJECTED members with their reason (the raw body stays in the archive), valid ones are
        stored. Rows outside the approved sessions are EXCLUDED members in both modes. VALUES: identical content = DUPLICATE, changed = a
        new revision. ABSENCES: kept apart, never a revision of a value. A batch that would pass the dataset's row cap is refused whole."""
        if on_reject not in ("raise", "quarantine"):
            raise StoreError("on_reject is 'raise' or 'quarantine'")
        res = IngestResult()
        obs = list(observations)
        now = ts(self._now())
        with self._rw() as c:
            ds = self._open_dataset(c, dataset_id)
            extended = bool(json.loads(ds["request_json"]).get("extended_hours"))
            fetch, params = None, {}
            if fetch_id is not None:
                fetch = c.execute("SELECT * FROM mi_fetches WHERE fetch_id = ?", (fetch_id,)).fetchone()
                if fetch is None or fetch["dataset_id"] != dataset_id or fetch["provider"] != ds["provider"] or fetch["status"] not in OK_FETCH:
                    raise ScopeError(f"fetch {fetch_id} is not a successful fetch of this dataset's provider: rows need their own provenance")
                params = json.loads(fetch["params_json"])
            elif ds["job_kind"] == "BACKFILL":
                raise ScopeError("a BACKFILL row needs the fetch that delivered it (provenance): ingest(..., fetch_id=...)")
            self._check_row_cap(c, dataset_id, len(obs))
            for o in obs:
                bad = self._scope_reason(o, ds, dataset_id) or self._derived_reason(o, ds) or self._fetch_reason(o, fetch, params, ds)
                if bad is None:
                    why = self._chronology_reason(o)
                    bad = (ChronologyError, why) if why else None
                if bad is None:
                    why = self._payload_reason(o)
                    bad = (PayloadError, why) if why else None
                if bad is not None:
                    if on_reject == "raise":
                        raise bad[0](f"observation refused: {bad[1]}")
                    key = natural_key(o) if isinstance(o, Observation) else "?"
                    self._member(c, dataset_id, key, content_hash(o) if isinstance(o, Observation) else None, None, None, "REJECTED", now, o=o,
                                 fetch_id=fetch_id, reason=bad[1])
                    res.rejected += 1
                    res.rejections.append({"natural_key": key, "reason": bad[1]})
                    continue
                out_of_scope = self._session_scope_reason(o, extended)
                if out_of_scope:
                    self._member(c, dataset_id, natural_key(o), content_hash(o), None, None, "EXCLUDED", now, o=o, fetch_id=fetch_id, reason=out_of_scope)
                    res.excluded += 1
                    continue
                o = self._annotate(o)
                key, h = natural_key(o), content_hash(o)
                mine = c.execute("SELECT content_hash, record_id, disposition FROM mi_dataset_members WHERE dataset_id = ? AND natural_key = ? "
                                 "AND disposition NOT IN ('REJECTED', 'EXCLUDED') LIMIT 1", (dataset_id, key)).fetchone()
                if mine is not None and mine["content_hash"] != h:
                    if on_reject == "raise":
                        raise StoreError("observation refused: CONFLICT_IN_DATASET (one dataset delivers one fact per natural key)")
                    self._member(c, dataset_id, key, h, None, None, "REJECTED", now, o=o, fetch_id=fetch_id, reason="CONFLICT_IN_DATASET")
                    res.rejected += 1
                    res.rejections.append({"natural_key": key, "reason": "CONFLICT_IN_DATASET"})
                    continue
                if mine is not None:                                   # the same record twice (a repeated page): one member per delivery
                    again = "ABSENCE" if mine["disposition"] == "ABSENCE" else "DUPLICATE"
                    self._member(c, dataset_id, key, h, mine["record_id"], None, again, now, o=o, fetch_id=fetch_id)
                    if again == "ABSENCE":
                        res.absences += 1
                    else:
                        res.duplicates += 1
                    continue
                if o.status in ABSENCE_STATUSES:
                    aid = uuid.uuid4().hex
                    rec = _insert(c, "mi_absences", {"absence_id": aid, **_common(o, key, h, dataset_id, fetch_id, now)})
                    self._member(c, dataset_id, key, h, aid, rec, "ABSENCE", now, o=o, fetch_id=fetch_id)
                    res.absences += 1
                    continue
                prev = c.execute("SELECT obs_id, revision, content_hash FROM mi_observations WHERE natural_key = ? ORDER BY revision DESC LIMIT 1",
                                 (key,)).fetchone()
                if prev is not None and prev["content_hash"] == h:
                    self._member(c, dataset_id, key, h, prev["obs_id"], None, "DUPLICATE", now, o=o, fetch_id=fetch_id)   # a re-delivery: its own times
                    res.duplicates += 1
                    continue
                oid = uuid.uuid4().hex
                rec = _insert(c, "mi_observations", {"obs_id": oid, "revision": (prev["revision"] + 1) if prev else 1,
                                                     "supersedes_obs_id": prev["obs_id"] if prev else None, **_common(o, key, h, dataset_id, fetch_id, now),
                                                     "event_end": ts(event_end(o)), "usable": int(o.status == AVAILABLE and not VA.blocking(o.quality_flags)),
                                                     "freshness": o.freshness, "age_s": None if o.age_s is None else float(o.age_s)})
                self._member(c, dataset_id, key, h, oid, rec, "REVISED" if prev else "INSERTED", now, o=o, fetch_id=fetch_id)
                res.obs_ids.append(oid)
                if prev:
                    res.revised += 1
                else:
                    res.inserted += 1
        return res

    @staticmethod
    def _metadata_sha256(c: sqlite3.Connection, dataset_id: str, status: str, counts: Dict[str, int]) -> str:
        """What substantiates a seal beyond the delivered values: the dataset row (spec, request, plan hash, approval reference), its approval
        use (with caps), its opening event, every fetch attempt in order, EVERY MEMBER ROW (each delivery's disposition, reason, fetch and
        times - a duplicate, a rejection and an exclusion included), every budget event of the run, and the sealed status and counts."""
        ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        use = c.execute("SELECT * FROM mi_approval_uses WHERE dataset_id = ?", (dataset_id,)).fetchone()
        opened = c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id LIMIT 1", (dataset_id,)).fetchone()
        fetches = [record_sha256(r) for r in c.execute("SELECT * FROM mi_fetches WHERE dataset_id = ? ORDER BY seq", (dataset_id,))]
        members = [record_sha256(r) for r in c.execute("SELECT * FROM mi_dataset_members WHERE dataset_id = ? ORDER BY member_id", (dataset_id,))]
        budget = [record_sha256(r) for r in c.execute("SELECT * FROM mi_budget_events WHERE dataset_id = ? ORDER BY event_id", (dataset_id,))]
        body = {"dataset": record_sha256(ds) if ds else None, "approval_use": record_sha256(use) if use else None,
                "opened": record_sha256(opened) if opened else None, "fetches": fetches, "members": members, "budget_events": budget,
                "status": status, "counts": counts}
        return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()

    _COUNT_COLUMNS = {"INSERTED": "rows_inserted", "DUPLICATE": "rows_duplicate", "REVISED": "rows_revised", "ABSENCE": "rows_absent",
                      "REJECTED": "rows_rejected", "EXCLUDED": "rows_excluded"}

    def finish_dataset(self, dataset_id: str, status: str, *, detail: str = "") -> dict:
        """Seal a dataset ONCE. A failed fetch is RECOVERED only when the identical request - same provider, endpoint and canonical,
        non-empty parameters - succeeded in a LATER attempt (durable attempt order, never a timestamp) of this dataset. COMPLETED and EMPTY
        need every failure recovered and no rejected row; EMPTY needs no delivered value. EXCLUDED rows never block COMPLETED."""
        if status not in FINAL_STATUSES:
            raise StoreError(f"a dataset ends COMPLETED, PARTIAL, FAILED or EMPTY, not {status}")
        with self._rw() as c:
            ev = c.execute("SELECT status FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id DESC LIMIT 1", (dataset_id,)).fetchone()
            if ev is None:
                raise StoreError(f"unknown dataset {dataset_id}")
            if ev["status"] in FINAL_STATUSES:
                raise SealedError(f"dataset {dataset_id} is already sealed {ev['status']}: a seal is final")
            members = c.execute("SELECT natural_key, content_hash, record_sha256, disposition FROM mi_dataset_members WHERE dataset_id = ?",
                                (dataset_id,)).fetchall()
            values = [m for m in members if m["disposition"] in VALUE_DISPOSITIONS]
            counts = {d: sum(1 for m in members if m["disposition"] == d) for d in DISPOSITIONS}
            bad = c.execute("""SELECT COUNT(*) FROM mi_fetches f WHERE f.dataset_id = ? AND f.status NOT IN ('OK', 'NOT_MODIFIED') AND NOT EXISTS (
                                 SELECT 1 FROM mi_fetches g WHERE g.dataset_id = f.dataset_id AND g.provider = f.provider AND g.endpoint = f.endpoint
                                 AND g.params_json = f.params_json AND g.params_json != '{}' AND g.status IN ('OK', 'NOT_MODIFIED') AND g.seq > f.seq)""",
                            (dataset_id,)).fetchone()[0]
            if status == "EMPTY" and (values or bad or counts["REJECTED"]):
                raise StoreError("EMPTY needs no delivered value, no unrecovered failed fetch and no rejected row")
            if status == "COMPLETED" and (bad or counts["REJECTED"]):
                raise StoreError("a dataset with an unrecovered failed fetch or a rejected row is PARTIAL or FAILED, never COMPLETED")
            sha = manifest_sha256({f"{m['natural_key']}\t{m['content_hash']}" for m in values})
            records = manifest_sha256(m["record_sha256"] for m in members if m["record_sha256"])
            meta = self._metadata_sha256(c, dataset_id, status, counts)
            c.execute("""INSERT INTO mi_dataset_events (dataset_id, status, at, rows_inserted, rows_duplicate, rows_revised, rows_absent, rows_rejected,
                         rows_excluded, content_sha256, records_sha256, metadata_sha256, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (dataset_id, status, ts(self._now()), counts["INSERTED"], counts["DUPLICATE"], counts["REVISED"], counts["ABSENCE"], counts["REJECTED"],
                       counts["EXCLUDED"], sha, records, meta, detail[:500]))
        return {"dataset_id": dataset_id, "status": status, "values": len(values), "content_sha256": sha, "records_sha256": records,
                "metadata_sha256": meta, "unrecovered_failures": bad, **counts}

    def record_problems(self, dataset_id: str, problems: Sequence[dict], *, fetch_id: str) -> int:
        """Rows of a response that never became observations (malformed, out of range, refused by the envelope): REJECTED members of an
        open dataset, within its row cap."""
        if not problems:
            return 0
        now = ts(self._now())
        with self._rw() as c:
            self._open_dataset(c, dataset_id)
            self._check_row_cap(c, dataset_id, len(problems))
            for i, pr in enumerate(problems):
                self._member(c, dataset_id, f"PARSE|{fetch_id}|{pr.get('index', i)}", None, None, None, "REJECTED", now, fetch_id=fetch_id,
                             reason=str(pr.get("problem", "UNKNOWN"))[:60])
        return len(problems)

    def record_budget_event(self, provider: str, event: str, *, reason: str = "", http_status: Optional[int] = None, retry_after_s: Optional[float] = None,
                            per_minute: Optional[float] = None, used_today: Optional[int] = None, dataset_id: Optional[str] = None) -> None:
        with self._rw() as c:
            if dataset_id is not None:
                self._open_dataset(c, dataset_id)
            c.execute("""INSERT INTO mi_budget_events (at, provider, event, reason, http_status, retry_after_s, per_minute, used_today, dataset_id)
                         VALUES (?,?,?,?,?,?,?,?,?)""", (ts(self._now()), provider, event, reason, http_status, retry_after_s, per_minute, used_today, dataset_id))

    def record_quality_audit(self, report: dict) -> None:
        """An audit is a later evaluation OF a dataset, not part of it: allowed for sealed datasets, outside their manifests."""
        with self._rw() as c:
            c.execute("""INSERT INTO mi_quality_audits (at, dataset_id, scope_json, check_name, result, codes_json, detail_json)
                         VALUES (?,?,?,?,?,?,?)""", (ts(self._now()), report.get("dataset_id"), canonical(report["scope"]), report["check"], report["result"],
                                                     canonical(report["codes"]), canonical({k: v for k, v in report.items() if k not in ("scope", "codes")})))

    # ------------------------------------------------------------------------------------------------ reads (read-only; never create)
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["quality_flags"] = json.loads(d.pop("quality_flags_json"))
        d["payload"] = json.loads(d.pop("payload_json")) if "payload_json" in d else {}
        d["usable"] = bool(d.get("usable", 0))
        return d

    def _scope(self, kind: str, symbol: Optional[str], series_id: Optional[str], feed: Optional[str], adjustment: Optional[str], alias: str = "o"):
        if kind in FEED_KINDS and feed not in ("iex", "sip"):
            raise FeedRequired(f"reading {kind} needs an explicit feed (iex or sip): feeds are never mixed")
        where, args = [f"{alias}.kind = ?"], [kind]
        for col, val in (("symbol", symbol.upper() if symbol else None), ("series_id", series_id), ("feed", feed), ("adjustment", adjustment)):
            if val is not None:
                where.append(f"{alias}.{col} = ?")
                args.append(val)
        return where, args

    _KNOWN_REV = ("o.revision = (SELECT MAX(r.revision) FROM mi_observations r WHERE r.natural_key = o.natural_key "
                  "AND r.available_at <= ? AND r.ingested_at <= ?)")
    _KNOWN = ["o.observed_at <= ?", "o.event_end <= ?", "o.available_at <= ?", "o.ingested_at <= ?", _KNOWN_REV]

    def history(self, kind: str, *, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                adjustment: Optional[str] = None, start: Optional[datetime] = None, end: Optional[datetime] = None, as_of: Optional[datetime] = None,
                usable_only: bool = False) -> List[dict]:
        """VALUES by event time [start, end], each fact's revision KNOWN at `as_of` (default: now): complete (event_end <= as_of), available
        and ingested by then; events after `as_of` are never known."""
        at = ts(as_of or self._now())
        where, args = self._scope(kind, symbol, series_id, feed, adjustment)
        where += self._KNOWN
        args += [at] * 6
        if start is not None:
            where.append("o.observed_at >= ?")
            args.append(ts(start))
        if end is not None:
            where.append("o.observed_at <= ?")
            args.append(ts(end))
        if usable_only:
            where.append("o.usable = 1")
        with self._ro() as c:
            if c is None:
                return []
            return [self._row(r) for r in c.execute(f"SELECT o.* FROM mi_observations o WHERE {' AND '.join(where)} ORDER BY o.observed_at, o.natural_key", args)]

    def _latest_value(self, c, kind, at, symbol, series_id, feed, adjustment, usable: bool) -> Optional[dict]:
        """Bounded: walks the (symbol, kind, feed, observed_at) index backwards and stops at the first fact whose KNOWN revision qualifies."""
        a = ts(at)
        where, args = self._scope(kind, symbol, series_id, feed, adjustment)
        where += self._KNOWN
        args += [a] * 6
        if usable:
            where.append("o.usable = 1")
        r = c.execute(f"SELECT o.* FROM mi_observations o WHERE {' AND '.join(where)} ORDER BY o.observed_at DESC, o.natural_key DESC LIMIT 1", args).fetchone()
        return self._row(r) if r else None

    def latest_usable(self, kind: str, *, at: datetime, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                      adjustment: Optional[str] = None) -> Optional[dict]:
        """The latest USABLE, COMPLETE value (by event time) known at `at`, or None (= UNAVAILABLE, never a zero). It may be old: use
        latest_status() for whether it is current under the Stage 7.0 freshness policy."""
        with self._ro() as c:
            if c is None:
                return None
            return self._latest_value(c, kind, at, symbol, series_id, feed, adjustment, usable=True)

    @staticmethod
    def _last_delivery(c, natural_key_: str, at: str) -> Optional[str]:
        """The latest delivery of a fact's value known at `at` - a duplicate re-delivery counts: it is new evidence that the value stands."""
        r = c.execute("SELECT MAX(retrieved_at) FROM mi_dataset_members WHERE natural_key = ? AND disposition IN ('INSERTED', 'REVISED', 'DUPLICATE') "
                      "AND available_at <= ? AND at <= ?", (natural_key_, at, at)).fetchone()
        return r[0] if r else None

    @staticmethod
    def freshness_of(kind: str, usable: Optional[dict], at: datetime) -> Optional[dict]:
        """The Stage 7.0 freshness assessment (freshness.py, unchanged) of the latest usable value, or None for a kind this stage stores none
        of: BAR_1DAY by its session date, BAR_1MIN by its end (the last completed minute), BAR_1HOUR by its start (the last completed hour)."""
        last = None if usable is None else usable
        if kind == "BAR_1DAY":
            return FR.assess_daily_bar(date.fromisoformat(last["session_date"]) if last else None, at)
        if kind == "BAR_1MIN":
            return FR.assess("BAR_1MIN", parse_ts(last["event_end"]) if last else None, at)
        if kind == "BAR_1HOUR":
            return FR.assess("BAR_1HOUR", parse_ts(last["observed_at"]) if last else None, at)
        return None

    def latest_status(self, kind: str, *, at: datetime, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                      adjustment: Optional[str] = None) -> dict:
        """The latest KNOWN record (a value of any usability, or an absence - by event time, then by its latest DELIVERY) next to the latest
        usable value, never merged into one. `status` uses the Stage 7.0 vocabulary: AVAILABLE only when the latest record is that usable
        value AND the 7.0 freshness policy says it is current; an old value is STALE (or as the policy says), a superseded one UNAVAILABLE."""
        a = ts(at)
        with self._ro() as c:
            if c is None:
                return {"latest_record": None, "latest_usable": None, "usable_is_latest": False, "freshness": None, "status": UNAVAILABLE,
                        "reason": "STORE_NOT_CREATED"}
            value = self._latest_value(c, kind, at, symbol, series_id, feed, adjustment, usable=False)
            usable = self._latest_value(c, kind, at, symbol, series_id, feed, adjustment, usable=True)
            where, args = self._scope(kind, symbol, series_id, feed, adjustment, alias="b")
            where += ["b.observed_at <= ?", "b.available_at <= ?", "b.ingested_at <= ?"]
            args += [a, a, a]
            ab = c.execute(f"SELECT b.* FROM mi_absences b WHERE {' AND '.join(where)} ORDER BY b.observed_at DESC, b.retrieved_at DESC LIMIT 1", args).fetchone()
            delivered = None if value is None else (self._last_delivery(c, value["natural_key"], a) or value["retrieved_at"])
        absence = self._row(ab) if ab else None
        latest, rtype = (None if value is None else {**value, "last_delivered_at": delivered}), "VALUE"
        if absence is not None and (value is None or (absence["observed_at"] or "", absence["retrieved_at"]) > (value["observed_at"] or "", delivered)):
            latest, rtype = {**absence, "last_delivered_at": absence["retrieved_at"]}, "ABSENCE"
        usable_is_latest = latest is not None and rtype == "VALUE" and usable is not None and latest["obs_id"] == usable["obs_id"]
        fresh = self.freshness_of(kind, usable, at)
        if usable is None:
            status, reason = UNAVAILABLE, "NO_USABLE_VALUE_KNOWN_AT"
        elif not usable_is_latest:
            status, reason = UNAVAILABLE, ("LATEST_RECORD_IS_ABSENCE" if rtype == "ABSENCE" else "LATEST_RECORD_IS_UNUSABLE")
        elif fresh is None:
            status, reason = UNAVAILABLE, "FRESHNESS_NOT_ASSESSED"
        else:
            status, reason = fresh["status"], fresh["reason"]
        return {"latest_record": None if latest is None else {**latest, "record_type": rtype}, "latest_usable": usable, "usable_is_latest": usable_is_latest,
                "freshness": fresh, "status": status, "reason": reason}

    def snapshot(self, kind: str, symbols: Sequence[str], *, at: datetime, feed: Optional[str] = None, adjustment: Optional[str] = None) -> Dict[str, dict]:
        """Per symbol, latest_status(): the latest known record and the latest usable value, kept apart, graded by the 7.0 freshness policy."""
        return {s: {"symbol": s, "kind": kind, "feed": feed, "at": ts(at), **self.latest_status(kind, at=at, symbol=s, feed=feed, adjustment=adjustment)}
                for s in sorted({x.upper() for x in symbols})}

    def revisions(self, natural_key_: str) -> List[dict]:
        with self._ro() as c:
            if c is None:
                return []
            return [self._row(r) for r in c.execute("SELECT * FROM mi_observations WHERE natural_key = ? ORDER BY revision", (natural_key_,))]

    def absences(self, natural_key_: str) -> List[dict]:
        with self._ro() as c:
            if c is None:
                return []
            return [self._row(r) for r in c.execute("SELECT * FROM mi_absences WHERE natural_key = ? ORDER BY retrieved_at, absence_id", (natural_key_,))]

    def members(self, dataset_id: str) -> List[dict]:
        with self._ro() as c:
            if c is None:
                return []
            return [dict(r) for r in c.execute("SELECT * FROM mi_dataset_members WHERE dataset_id = ? ORDER BY member_id", (dataset_id,))]

    def provenance(self, obs_id: str) -> Optional[dict]:
        """The value, every revision of its fact, its absences, every delivery of it, the fetch that first brought it (with the raw-archive
        path) and its dataset."""
        with self._ro() as c:
            if c is None:
                return None
            r = c.execute("SELECT * FROM mi_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            if r is None:
                return None
            o = self._row(r)
            fetch = c.execute("SELECT * FROM mi_fetches WHERE fetch_id = ?", (o["fetch_id"],)).fetchone() if o["fetch_id"] else None
            ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (o["dataset_id"],)).fetchone()
            ev = c.execute("SELECT status, at, rows_inserted, rows_duplicate, rows_revised, rows_absent, rows_rejected, rows_excluded, content_sha256, detail "
                           "FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id", (o["dataset_id"],)).fetchall()
            revs = c.execute("SELECT obs_id, revision, content_hash, available_at, ingested_at FROM mi_observations WHERE natural_key = ? ORDER BY revision",
                             (o["natural_key"],)).fetchall()
            abs_ = c.execute("SELECT absence_id, status, retrieved_at, dataset_id FROM mi_absences WHERE natural_key = ? ORDER BY retrieved_at",
                             (o["natural_key"],)).fetchall()
            dels = c.execute("SELECT dataset_id, disposition, fetch_id, retrieved_at, available_at, at FROM mi_dataset_members WHERE natural_key = ? "
                             "AND disposition IN ('INSERTED', 'REVISED', 'DUPLICATE') ORDER BY member_id", (o["natural_key"],)).fetchall()
        return {"observation": o, "revisions": [dict(x) for x in revs], "absences": [dict(x) for x in abs_], "deliveries": [dict(x) for x in dels],
                "fetch": dict(fetch) if fetch else None, "dataset": dict(ds) if ds else None, "dataset_events": [dict(x) for x in ev]}

    def dataset(self, dataset_id: str) -> Optional[dict]:
        with self._ro() as c:
            if c is None:
                return None
            ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            if ds is None:
                return None
            ev = [dict(x) for x in c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id", (dataset_id,))]
            use = c.execute("SELECT * FROM mi_approval_uses WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return {**dict(ds), "events": ev, "status": ev[-1]["status"] if ev else None, "approval_use": dict(use) if use else None}

    def dataset_states(self) -> dict:
        """Every dataset by finalization state: `unsealed` (with the time of its last recorded activity and whether it delivered a value) and
        `sealed` (with its seal hashes) - what recovery (ingest.recover_pending) works from."""
        with self._ro() as c:
            if c is None:
                return {"unsealed": [], "sealed": []}
            unsealed, sealed = [], []
            for d in c.execute("SELECT dataset_id, created_at FROM mi_datasets ORDER BY created_at, dataset_id").fetchall():
                did = d["dataset_id"]
                ev = c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id DESC LIMIT 1", (did,)).fetchone()
                if ev is not None and ev["status"] in FINAL_STATUSES:
                    sealed.append({"dataset_id": did, "status": ev["status"], "at": ev["at"], "content_sha256": ev["content_sha256"],
                                   "records_sha256": ev["records_sha256"], "metadata_sha256": ev["metadata_sha256"]})
                    continue
                last = max(x for x in (d["created_at"],
                                       c.execute("SELECT MAX(ingested_at) FROM mi_fetches WHERE dataset_id = ?", (did,)).fetchone()[0],
                                       c.execute("SELECT MAX(at) FROM mi_dataset_members WHERE dataset_id = ?", (did,)).fetchone()[0],
                                       c.execute("SELECT MAX(at) FROM mi_budget_events WHERE dataset_id = ?", (did,)).fetchone()[0]) if x)
                values = c.execute("SELECT COUNT(*) FROM mi_dataset_members WHERE dataset_id = ? AND disposition IN ('INSERTED', 'REVISED', 'DUPLICATE')",
                                   (did,)).fetchone()[0]
                unsealed.append({"dataset_id": did, "last_activity": last, "values": values})
        return {"unsealed": unsealed, "sealed": sealed}

    def budget_used(self, provider: str, day: date) -> int:
        """ALLOW events of `provider` on New York date `day` (the persisted request count the daily caps read)."""
        with self._ro() as c:
            if c is None:
                return 0
            rows = c.execute("SELECT at FROM mi_budget_events WHERE provider = ? AND event = 'ALLOW'", (provider,)).fetchall()
        return sum(1 for (a,) in rows if CAL.session(parse_ts(a))["session_date"] == day)

    def quality_report(self, kind: str, *, symbol: str, feed: str, start: date, end: date, adjustment: Optional[str] = None,
                       as_of: Optional[datetime] = None) -> dict:
        """Daily bars, three kinds of session kept apart: VALID (a usable bar), INVALID (only unusable bars - their blocking codes are
        reported) and MISSING (no bar at all); the missing ones are listed, never filled. BLOCKED when coverage is unknown, a run of sessions
        without a valid bar forms a hole, or a blocking code is present on any bar in range; WARN on single missing / invalid sessions or
        warnings."""
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
        present = {r["session_date"] for r in rows}
        invalid = {}
        for r in rows:
            if r["session_date"] not in usable:
                invalid.setdefault(r["session_date"], set()).update(set(r["quality_flags"]) - INFORMATIONAL_CODES)
        missing = [d.isoformat() for d in days if d.isoformat() not in present]
        row_codes = {f for r in rows for f in r["quality_flags"]} - INFORMATIONAL_CODES
        series = VA.daily_series_codes([{**r["payload"], "day": date.fromisoformat(r["session_date"])} for _, r in sorted(usable.items())])
        holes, run = [], []                                          # runs of consecutive expected sessions without a valid bar
        for d in days + [None]:
            if d is not None and d.isoformat() not in usable:
                run.append(d)
                continue
            if len(run) >= VA.SESSION_HOLE_DAYS:
                holes.append({"first_missing": run[0].isoformat(), "last_missing": run[-1].isoformat(), "market_days": len(run)})
            run = []
        codes = sorted(set(series["codes"]) | row_codes | ({"CALENDAR_NOT_COVERED"} if uncovered else set()) | ({"SESSION_HOLE"} if holes else set()))
        blocked = uncovered or series["blocked"] or holes or VA.blocking(row_codes)
        result = "BLOCKED" if blocked else ("WARN" if (missing or invalid or codes) else "PASS")
        return {"check": "daily_bar_completeness", "scope": {"kind": kind, "symbol": symbol.upper(), "feed": feed, "adjustment": adjustment,
                                                             "start": start.isoformat(), "end": end.isoformat()},
                "result": result, "codes": codes, "expected_sessions": len(days), "present_sessions": len(usable),
                "invalid_sessions": [{"session_date": k, "codes": sorted(v)} for k, v in sorted(invalid.items())], "missing_sessions": missing,
                "uncovered_weekdays": uncovered, "holes": holes, "jumps": series["jumps"],
                "unusable_rows": sum(1 for r in rows if not r["usable"]), "as_of": ts(as_of or self._now())}

    def status_counts(self, *, dataset_id: Optional[str] = None) -> dict:
        with self._ro() as c:
            if c is None:
                return {"store": "NOT_CREATED"}
            where, args = ("WHERE dataset_id = ?", (dataset_id,)) if dataset_id else ("", ())
            rows = c.execute(f"SELECT kind, feed, status, COUNT(*) FROM mi_observations {where} GROUP BY kind, feed, status ORDER BY 1, 2, 3", args).fetchall()
            tables = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("mi_datasets", "mi_fetches", "mi_observations", "mi_absences",
                                                                                         "mi_dataset_members", "mi_approval_uses", "mi_budget_events",
                                                                                         "mi_quality_audits")}
            unsealed = c.execute("""SELECT COUNT(*) FROM mi_datasets d WHERE (SELECT e.status FROM mi_dataset_events e WHERE e.dataset_id = d.dataset_id
                                    ORDER BY e.event_id DESC LIMIT 1) NOT IN ('COMPLETED', 'PARTIAL', 'FAILED', 'EMPTY')""").fetchone()[0]
        return {"store": "OK", "schema_version": self.schema_version(), "tables": tables, "by_kind_feed_status": [list(r) for r in rows],
                "unsealed_datasets": unsealed}

    _CORRUPT_KEYS = ("row_hash_mismatches", "member_mismatches", "record_mismatches", "manifest_mismatches", "metadata_mismatches", "count_mismatches",
                     "orphans", "missing_datasets", "anchor_mismatches")

    def verify_integrity(self, *, anchors: Optional[Dict[str, dict]] = None) -> dict:
        """READ-ONLY evidence that stored rows are what was written, reconciled in BOTH directions with the external anchors.
        * CORRUPT: a schema problem (incl. a reset user_version), a value / absence whose content hash does not recompute, a member whose
          content or full-row record hash differs from its row, a sealed dataset whose content manifest, records digest, metadata digest
          (dataset row, approval use, opening event, fetches, status, counts) or counts do not recompute, a row of a dataset that no longer
          exists, an anchored dataset missing from the store, or an anchor that disagrees (or conflicts with another anchor);
        * UNVERIFIED: nothing contradicts, but something cannot be confirmed - no anchors were given, a sealed dataset has no anchor yet
          (pending anchor), or a dataset is unsealed (pending finalization);
        * VERIFIED: every sealed dataset recomputes and matches its anchor, every anchor matches a sealed dataset, nothing is pending.
        `ok` is True only for VERIFIED. Limitation: a writer who rewrites BOTH the database and the anchors file consistently is out of
        scope (DESIGN_71 section 3)."""
        out = {"rows_checked": 0, **{k: [] for k in self._CORRUPT_KEYS}, "unanchored_datasets": [], "unsealed_datasets": [], "anchors_checked": anchors is not None}
        try:
            with self._ro() as c:
                if c is None:
                    out["missing_datasets"] = sorted(anchors or {})
                    return self._verdict(out, store="NOT_CREATED")
                self._verify_rows(c, out)
                sealed = self._verify_seals(c, out)
        except (SchemaMismatch, SchemaTooNew, sqlite3.DatabaseError) as exc:
            return {**out, "store": "SCHEMA_ERROR", "schema_error": f"{type(exc).__name__}: {str(exc)[:200]}", "state": CORRUPT, "ok": False}
        if anchors is not None:
            for did, a in sorted(anchors.items()):
                if did not in sealed:
                    out["missing_datasets"].append(did)
                elif a.get("conflicting") or any(a.get(k) != sealed[did][k] for k in ("content_sha256", "records_sha256", "metadata_sha256", "status")):
                    out["anchor_mismatches"].append(did)
            out["unanchored_datasets"] = sorted(set(sealed) - set(anchors))
        return self._verdict(out, store="OK")

    @staticmethod
    def _verdict(out: dict, *, store: str) -> dict:
        state = CORRUPT if any(out[k] for k in MarketIntelStore._CORRUPT_KEYS) else (
            UNVERIFIED if (not out["anchors_checked"] or out["unanchored_datasets"] or out["unsealed_datasets"]) else VERIFIED)
        return {**out, "store": store, "state": state, "ok": state == VERIFIED}

    @staticmethod
    def _verify_rows(c: sqlite3.Connection, out: dict) -> None:
        hashes, records = {}, {}
        for table, idcol in (("mi_observations", "obs_id"), ("mi_absences", "absence_id")):
            for r in c.execute(f"SELECT * FROM {table}"):
                out["rows_checked"] += 1
                hashes[r[idcol]], records[r[idcol]] = r["content_hash"], record_sha256(r)
                if _row_hash(r) != r["content_hash"]:
                    out["row_hash_mismatches"].append(r[idcol])
        for m in c.execute("SELECT member_id, record_id, content_hash, record_sha256 FROM mi_dataset_members WHERE record_id IS NOT NULL"):
            if hashes.get(m["record_id"]) != m["content_hash"]:
                out["member_mismatches"].append(m["member_id"])
            if m["record_sha256"] is not None and records.get(m["record_id"]) != m["record_sha256"]:
                out["record_mismatches"].append(m["member_id"])
        for table in ("mi_observations", "mi_absences", "mi_dataset_members", "mi_dataset_events", "mi_approval_uses", "mi_fetches", "mi_budget_events"):
            n = c.execute(f"SELECT COUNT(*) FROM {table} t WHERE t.dataset_id IS NOT NULL AND NOT EXISTS "
                          f"(SELECT 1 FROM mi_datasets d WHERE d.dataset_id = t.dataset_id)").fetchone()[0]
            if n:
                out["orphans"].append({"table": table, "rows": n})
        n = c.execute("SELECT COUNT(*) FROM mi_datasets d WHERE NOT EXISTS (SELECT 1 FROM mi_dataset_events e WHERE e.dataset_id = d.dataset_id)").fetchone()[0]
        if n:
            out["orphans"].append({"table": "mi_datasets", "rows": n, "problem": "no event"})

    def _verify_seals(self, c: sqlite3.Connection, out: dict) -> Dict[str, dict]:
        sealed = {}
        for d in c.execute("SELECT dataset_id FROM mi_datasets").fetchall():
            did = d["dataset_id"]
            evs = c.execute("SELECT * FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id", (did,)).fetchall()
            finals = [e for e in evs if e["status"] in FINAL_STATUSES]
            if not finals:
                out["unsealed_datasets"].append(did)
                continue
            e = finals[-1]
            if len(finals) > 1 or evs[-1]["event_id"] != e["event_id"]:
                out["metadata_mismatches"].append(did)                     # a second seal, or an event after the seal
            ms = c.execute("SELECT natural_key, content_hash, record_sha256, disposition FROM mi_dataset_members WHERE dataset_id = ?", (did,)).fetchall()
            lines = {f"{m['natural_key']}\t{m['content_hash']}" for m in ms if m["disposition"] in VALUE_DISPOSITIONS}
            if manifest_sha256(lines) != e["content_sha256"] or manifest_sha256(m["record_sha256"] for m in ms if m["record_sha256"]) != e["records_sha256"]:
                out["manifest_mismatches"].append(did)
            counts = {k: sum(1 for m in ms if m["disposition"] == k) for k in DISPOSITIONS}
            if any(counts[k] != e[col] for k, col in self._COUNT_COLUMNS.items()):
                out["count_mismatches"].append(did)
            stated = {k: e[col] for k, col in self._COUNT_COLUMNS.items()}
            if self._metadata_sha256(c, did, e["status"], stated) != e["metadata_sha256"]:
                out["metadata_mismatches"].append(did)
            sealed[did] = {"content_sha256": e["content_sha256"], "records_sha256": e["records_sha256"], "metadata_sha256": e["metadata_sha256"],
                           "status": e["status"], "at": e["at"]}
        return sealed
