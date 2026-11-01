"""
market_intel/store.py - Stage 7.1: the market-intelligence store, its OWN SQLite file (data/market_intel.db; DESIGN_71 sections 2-3).

Separate from the trading database; nothing here imports a trading, broker, model or setup module. Created lazily: constructing a store,
importing this module or calling any read creates nothing; only an explicit write creates and migrates the file (migrations.py), and
every open verifies the live schema objects.

Writes (one BEGIN IMMEDIATE transaction each; all or nothing):
* datasets are validated (kind / provider / feed / adjustment / timeframe / symbols / covered date range) and an approved backfill claims
  its single-use approval in the same transaction;
* every observation is checked against its dataset's scope, its chronology (it cannot be observed, completed or published after it was
  retrieved) and, for bars, its payload (OHLCV present and finite, the bar shape codes) - refused, or quarantined as a REJECTED member;
* VALUES go to mi_observations as revisions of a natural key (identical content = DUPLICATE, changed content = a new revision);
  ABSENCES (UNAVAILABLE / INSUFFICIENT_DATA / NOT_APPLICABLE) go to mi_absences and never supersede or hide a value;
* every row a dataset delivered is a member (INSERTED / REVISED / DUPLICATE / ABSENCE / REJECTED): the dataset's manifest hash covers
  what it delivered whatever the store already held.

Point in time - OPERATIONAL knowledge, never a research shortcut:
* a row is KNOWN at T when available_at <= T, ingested_at <= T and its event time observed_at <= T;
* event_end is when the fact was complete (a bar's end; a daily bar's end is its session's late-trading end). Historical research
  availability (event_end + a pinned latency, with a vintage policy) is a separate, later design (Stage 7.6); no API here offers it.
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
from market_intel import validate as VA
from market_intel.migrations import SCHEMA_VERSION, migrate, verify
from market_intel.models import FetchRecord, Observation

STORE_FILE = "market_intel.db"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
BAR_KINDS = {"BAR_1MIN": "1Min", "BAR_1HOUR": "1Hour", "BAR_1DAY": "1Day"}
FEED_KINDS = (*BAR_KINDS, "QUOTE_SNAPSHOT")               # an explicit iex / sip feed is required
SESSION_KINDS = FEED_KINDS                                 # a covered New York session date is required
ABSENCE_STATUSES = (UNAVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE)
BAR_FIELDS = ("open", "high", "low", "close", "volume")
FINAL_STATUSES = ("COMPLETED", "PARTIAL", "FAILED", "EMPTY")
VALUE_DISPOSITIONS = ("INSERTED", "REVISED", "DUPLICATE")
SKEW = timedelta(seconds=VA.CLOCK_SKEW_S)
NEWS_SKEW = timedelta(seconds=VA.NEWS_SKEW_S)
BUSY_TIMEOUT_MS = 10_000


class StoreError(ValueError):
    """An ingestion or a query the store refuses (with the reason)."""


class FeedRequired(StoreError):
    """A bar / quote read or write without an explicit feed, or a feed other than its dataset's."""


class CoverageError(StoreError):
    """A session-bound observation, or a dataset range, on a weekday the calendar does not cover."""


class ScopeError(StoreError):
    """An observation outside its dataset's kind / provider / feed / adjustment / symbols / dates, or an invalid dataset spec."""


class ChronologyError(StoreError):
    """An observation observed, completed or published after it was retrieved (it could not have existed yet)."""


class PayloadError(StoreError):
    """A bar whose payload lacks OHLCV, or carries a non-finite / non-numeric value."""


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
    """When the fact was complete: a minute / hour bar's end; a daily bar's session late-trading end (17:00 ET on a 2025 early close,
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


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    revised: int = 0
    absences: int = 0
    rejected: int = 0
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
    return out


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
        """A read-only connection, or None when the store has not been created (reads never create it). Verifies the schema objects."""
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

    # ------------------------------------------------------------------------------------------------ writes (explicit only)
    def create_dataset(self, spec: DatasetSpec, *, approval=None) -> str:
        """Validate the spec (and the coverage of a bar range), create the dataset and - for an approved backfill - claim the approval in
        the same transaction (an approval is used once)."""
        problems = validate_spec(spec)
        if spec.job_kind == "BACKFILL" and (approval is None or not spec.plan_sha256 or approval.plan_sha256 != spec.plan_sha256
                                            or spec.approval_ref != approval.approval_id):
            problems.append("a BACKFILL dataset needs the operator approval of its own plan (plan_sha256 and approval_ref)")
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
                    c.execute("INSERT INTO mi_approval_uses (approval_id, plan_sha256, approval_file_sha256, dataset_id, used_at) VALUES (?,?,?,?,?)",
                              (approval.approval_id, approval.plan_sha256, approval.file_sha256, did, now))
                except sqlite3.IntegrityError as exc:
                    raise StoreError(f"approval {approval.approval_id} was already used: an approval runs once") from exc
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

    def ingest(self, dataset_id: str, observations: Iterable[Observation], *, fetch_id: Optional[str] = None, on_reject: str = "raise") -> IngestResult:
        """All of `observations` in ONE transaction. on_reject='raise': any invalid observation refuses the whole batch (nothing stored).
        on_reject='quarantine': invalid ones become REJECTED members with their reason (the raw body stays in the archive), valid ones are
        stored. VALUES: identical content = DUPLICATE, changed = a new revision. ABSENCES: kept apart, never a revision of a value."""
        if on_reject not in ("raise", "quarantine"):
            raise StoreError("on_reject is 'raise' or 'quarantine'")
        res = IngestResult()
        obs = list(observations)
        now = ts(self._now())
        with self._rw() as c:
            ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            if ds is None:
                raise StoreError(f"unknown dataset {dataset_id}")
            last = c.execute("SELECT status FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id DESC LIMIT 1", (dataset_id,)).fetchone()
            if last["status"] in FINAL_STATUSES:
                raise StoreError(f"dataset {dataset_id} is already {last['status']}: a sealed dataset takes no rows")
            if fetch_id is not None:
                f = c.execute("SELECT dataset_id, provider, status FROM mi_fetches WHERE fetch_id = ?", (fetch_id,)).fetchone()
                if f is None or f["dataset_id"] != dataset_id or f["provider"] != ds["provider"] or f["status"] not in ("OK", "NOT_MODIFIED"):
                    raise ScopeError(f"fetch {fetch_id} is not a successful fetch of this dataset's provider: rows need their own provenance")
            for o in obs:
                bad = self._scope_reason(o, ds, dataset_id)
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
                    c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, disposition, reason, at) "
                              "VALUES (?,?,?,NULL,'REJECTED',?,?)", (dataset_id, key, content_hash(o) if isinstance(o, Observation) else None, bad[1], now))
                    res.rejected += 1
                    res.rejections.append({"natural_key": key, "reason": bad[1]})
                    continue
                o = self._annotate(o)
                key, h = natural_key(o), content_hash(o)
                mine = c.execute("SELECT content_hash, record_id, disposition FROM mi_dataset_members WHERE dataset_id = ? AND natural_key = ? "
                                 "AND disposition != 'REJECTED' LIMIT 1", (dataset_id, key)).fetchone()
                if mine is not None and mine["content_hash"] != h:
                    if on_reject == "raise":
                        raise StoreError("observation refused: CONFLICT_IN_DATASET (one dataset delivers one fact per natural key)")
                    c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, disposition, reason, at) "
                              "VALUES (?,?,?,NULL,'REJECTED','CONFLICT_IN_DATASET',?)", (dataset_id, key, h, now))
                    res.rejected += 1
                    res.rejections.append({"natural_key": key, "reason": "CONFLICT_IN_DATASET"})
                    continue
                if mine is not None:                                   # the same record twice (a repeated page): one member per delivery
                    again = "ABSENCE" if mine["disposition"] == "ABSENCE" else "DUPLICATE"
                    c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, disposition, at) VALUES (?,?,?,?,?,?)",
                              (dataset_id, key, h, mine["record_id"], again, now))
                    if again == "ABSENCE":
                        res.absences += 1
                    else:
                        res.duplicates += 1
                    continue
                if o.status in ABSENCE_STATUSES:
                    aid = uuid.uuid4().hex
                    rec = _insert(c, "mi_absences", {"absence_id": aid, **_common(o, key, h, dataset_id, fetch_id, now)})
                    c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, record_sha256, disposition, at) "
                              "VALUES (?,?,?,?,?,'ABSENCE',?)", (dataset_id, key, h, aid, rec, now))
                    res.absences += 1
                    continue
                prev = c.execute("SELECT obs_id, revision, content_hash FROM mi_observations WHERE natural_key = ? ORDER BY revision DESC LIMIT 1",
                                 (key,)).fetchone()
                if prev is not None and prev["content_hash"] == h:
                    c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, disposition, at) VALUES (?,?,?,?,'DUPLICATE',?)",
                              (dataset_id, key, h, prev["obs_id"], now))
                    res.duplicates += 1
                    continue
                oid = uuid.uuid4().hex
                rec = _insert(c, "mi_observations", {"obs_id": oid, "revision": (prev["revision"] + 1) if prev else 1,
                                                     "supersedes_obs_id": prev["obs_id"] if prev else None, **_common(o, key, h, dataset_id, fetch_id, now),
                                                     "event_end": ts(event_end(o)), "usable": int(o.status == AVAILABLE and not VA.blocking(o.quality_flags)),
                                                     "freshness": o.freshness, "age_s": None if o.age_s is None else float(o.age_s)})
                c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, record_sha256, disposition, at) "
                          "VALUES (?,?,?,?,?,?,?)", (dataset_id, key, h, oid, rec, "REVISED" if prev else "INSERTED", now))
                res.obs_ids.append(oid)
                if prev:
                    res.revised += 1
                else:
                    res.inserted += 1
        return res

    def finish_dataset(self, dataset_id: str, status: str, *, detail: str = "") -> dict:
        """Seal a dataset ONCE. A failed fetch is RECOVERED only when the identical request - same provider, endpoint and canonical,
        non-empty parameters - later succeeded in this dataset. COMPLETED and EMPTY need every failure recovered and no rejected row;
        EMPTY needs no delivered value. The manifest hash covers every value the dataset delivered (duplicates included)."""
        if status not in FINAL_STATUSES:
            raise StoreError(f"a dataset ends COMPLETED, PARTIAL, FAILED or EMPTY, not {status}")
        with self._rw() as c:
            ev = c.execute("SELECT status FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id DESC LIMIT 1", (dataset_id,)).fetchone()
            if ev is None:
                raise StoreError(f"unknown dataset {dataset_id}")
            if ev["status"] in FINAL_STATUSES:
                raise StoreError(f"dataset {dataset_id} is already sealed {ev['status']}: a seal is final")
            members = c.execute("SELECT natural_key, content_hash, record_sha256, disposition FROM mi_dataset_members WHERE dataset_id = ?",
                                (dataset_id,)).fetchall()
            values = [m for m in members if m["disposition"] in VALUE_DISPOSITIONS]
            counts = {d: sum(1 for m in members if m["disposition"] == d) for d in ("INSERTED", "REVISED", "DUPLICATE", "ABSENCE", "REJECTED")}
            bad = c.execute("""SELECT COUNT(*) FROM mi_fetches f WHERE f.dataset_id = ? AND f.status NOT IN ('OK', 'NOT_MODIFIED') AND NOT EXISTS (
                                 SELECT 1 FROM mi_fetches g WHERE g.dataset_id = f.dataset_id AND g.provider = f.provider AND g.endpoint = f.endpoint
                                 AND g.params_json = f.params_json AND g.params_json != '{}' AND g.status IN ('OK', 'NOT_MODIFIED')
                                 AND g.started_at >= f.started_at AND g.fetch_id != f.fetch_id)""", (dataset_id,)).fetchone()[0]
            if status == "EMPTY" and (values or bad or counts["REJECTED"]):
                raise StoreError("EMPTY needs no delivered value, no unrecovered failed fetch and no rejected row")
            if status == "COMPLETED" and (bad or counts["REJECTED"]):
                raise StoreError("a dataset with an unrecovered failed fetch or a rejected row is PARTIAL or FAILED, never COMPLETED")
            sha = manifest_sha256({f"{m['natural_key']}\t{m['content_hash']}" for m in values})
            records = manifest_sha256(m["record_sha256"] for m in members if m["record_sha256"])
            c.execute("""INSERT INTO mi_dataset_events (dataset_id, status, at, rows_inserted, rows_duplicate, rows_revised, rows_absent, rows_rejected,
                         content_sha256, records_sha256, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (dataset_id, status, ts(self._now()), counts["INSERTED"], counts["DUPLICATE"], counts["REVISED"], counts["ABSENCE"], counts["REJECTED"],
                       sha, records, detail[:500]))
        return {"dataset_id": dataset_id, "status": status, "values": len(values), "content_sha256": sha, "records_sha256": records,
                "unrecovered_failures": bad, **counts}

    def record_problems(self, dataset_id: str, problems: Sequence[dict], *, fetch_id: str) -> int:
        """Rows of a response that never became observations (malformed, out of range, refused by the envelope): REJECTED members."""
        if not problems:
            return 0
        now = ts(self._now())
        with self._rw() as c:
            for i, pr in enumerate(problems):
                c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, content_hash, record_id, disposition, reason, at) "
                          "VALUES (?,?,NULL,NULL,'REJECTED',?,?)", (dataset_id, f"PARSE|{fetch_id}|{pr.get('index', i)}",
                                                                     str(pr.get("problem", "UNKNOWN"))[:60], now))
        return len(problems)

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

    def history(self, kind: str, *, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                adjustment: Optional[str] = None, start: Optional[datetime] = None, end: Optional[datetime] = None, as_of: Optional[datetime] = None,
                usable_only: bool = False) -> List[dict]:
        """VALUES by event time [start, end], each fact's revision KNOWN at `as_of` (default: now); events after `as_of` are never known."""
        at = ts(as_of or self._now())
        where, args = self._scope(kind, symbol, series_id, feed, adjustment)
        where += ["o.observed_at <= ?", "o.available_at <= ?", "o.ingested_at <= ?", self._KNOWN_REV]
        args += [at, at, at, at, at]
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
        where += ["o.observed_at <= ?", "o.available_at <= ?", "o.ingested_at <= ?", self._KNOWN_REV]
        args += [a, a, a, a, a]
        if usable:
            where.append("o.usable = 1")
        r = c.execute(f"SELECT o.* FROM mi_observations o WHERE {' AND '.join(where)} ORDER BY o.observed_at DESC, o.natural_key DESC LIMIT 1", args).fetchone()
        return self._row(r) if r else None

    def latest_usable(self, kind: str, *, at: datetime, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                      adjustment: Optional[str] = None) -> Optional[dict]:
        """The latest USABLE value (by event time) known at `at`, or None (= UNAVAILABLE, never a zero). It may be older than the latest
        record: latest_status() says whether a newer attempt found nothing."""
        with self._ro() as c:
            if c is None:
                return None
            return self._latest_value(c, kind, at, symbol, series_id, feed, adjustment, usable=True)

    def latest_status(self, kind: str, *, at: datetime, symbol: Optional[str] = None, series_id: Optional[str] = None, feed: Optional[str] = None,
                      adjustment: Optional[str] = None) -> dict:
        """The latest KNOWN record (a value of any usability, or an absence) next to the latest usable value - never merged into one."""
        with self._ro() as c:
            if c is None:
                return {"latest_record": None, "latest_usable": None, "usable_is_latest": False, "status": "UNAVAILABLE", "reason": "STORE_NOT_CREATED"}
            value = self._latest_value(c, kind, at, symbol, series_id, feed, adjustment, usable=False)
            usable = self._latest_value(c, kind, at, symbol, series_id, feed, adjustment, usable=True)
            a = ts(at)
            where, args = self._scope(kind, symbol, series_id, feed, adjustment, alias="b")
            where += ["b.observed_at <= ?", "b.available_at <= ?", "b.ingested_at <= ?"]
            args += [a, a, a]
            ab = c.execute(f"SELECT b.* FROM mi_absences b WHERE {' AND '.join(where)} ORDER BY b.observed_at DESC, b.retrieved_at DESC LIMIT 1", args).fetchone()
        absence = self._row(ab) if ab else None
        latest, rtype = value, "VALUE"
        def order(r):
            return (r["observed_at"] or "", r["retrieved_at"])
        if absence is not None and (value is None or order(absence) > order(value)):
            latest, rtype = absence, "ABSENCE"
        usable_is_latest = latest is not None and rtype == "VALUE" and usable is not None and latest["obs_id"] == usable["obs_id"]
        return {"latest_record": None if latest is None else {**latest, "record_type": rtype}, "latest_usable": usable, "usable_is_latest": usable_is_latest,
                "status": "AVAILABLE" if usable_is_latest else ("UNAVAILABLE" if usable is None else "STALE_OR_SUPERSEDED_BY_ABSENCE"),
                "reason": None if usable_is_latest else ("NO_USABLE_VALUE_KNOWN_AT" if usable is None else f"LATEST_RECORD_IS_{rtype}")}

    def snapshot(self, kind: str, symbols: Sequence[str], *, at: datetime, feed: Optional[str] = None, adjustment: Optional[str] = None) -> Dict[str, dict]:
        """Per symbol, latest_status(): the latest known record and the latest usable value, kept apart."""
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
        """The value, every revision of its fact, its absences, the fetch that brought it (with the raw-archive path) and its dataset."""
        with self._ro() as c:
            if c is None:
                return None
            r = c.execute("SELECT * FROM mi_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            if r is None:
                return None
            o = self._row(r)
            fetch = c.execute("SELECT * FROM mi_fetches WHERE fetch_id = ?", (o["fetch_id"],)).fetchone() if o["fetch_id"] else None
            ds = c.execute("SELECT * FROM mi_datasets WHERE dataset_id = ?", (o["dataset_id"],)).fetchone()
            ev = c.execute("SELECT status, at, rows_inserted, rows_duplicate, rows_revised, rows_absent, rows_rejected, content_sha256, detail "
                           "FROM mi_dataset_events WHERE dataset_id = ? ORDER BY event_id", (o["dataset_id"],)).fetchall()
            revs = c.execute("SELECT obs_id, revision, content_hash, available_at, ingested_at FROM mi_observations WHERE natural_key = ? ORDER BY revision",
                             (o["natural_key"],)).fetchall()
            abs_ = c.execute("SELECT absence_id, status, retrieved_at, dataset_id FROM mi_absences WHERE natural_key = ? ORDER BY retrieved_at",
                             (o["natural_key"],)).fetchall()
        return {"observation": o, "revisions": [dict(x) for x in revs], "absences": [dict(x) for x in abs_], "fetch": dict(fetch) if fetch else None,
                "dataset": dict(ds) if ds else None, "dataset_events": [dict(x) for x in ev]}

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
            tables = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("mi_datasets", "mi_fetches", "mi_observations", "mi_absences",
                                                                                         "mi_dataset_members", "mi_approval_uses", "mi_budget_events",
                                                                                         "mi_quality_audits")}
            unsealed = c.execute("""SELECT COUNT(*) FROM mi_datasets d WHERE (SELECT e.status FROM mi_dataset_events e WHERE e.dataset_id = d.dataset_id
                                    ORDER BY e.event_id DESC LIMIT 1) NOT IN ('COMPLETED', 'PARTIAL', 'FAILED', 'EMPTY')""").fetchone()[0]
        return {"store": "OK", "schema_version": self.schema_version(), "tables": tables, "by_kind_feed_status": [list(r) for r in rows],
                "unsealed_datasets": unsealed}

    def verify_integrity(self, *, anchors: Optional[Dict[str, dict]] = None) -> dict:
        """READ-ONLY evidence that stored rows are what was written:
        * every value / absence row's content hash recomputed from its columns (the fact);
        * every member's content hash equal to its row's, and its record hash equal to the FULL row now (times and usability included:
          an earlier available_at - a look-ahead edit - is a mismatch);
        * every sealed dataset's content manifest and records digest recomputed from its members;
        * given the raw archive's external anchors (RawArchive.anchors()), both equal to what was anchored at the seal.
        Limitation: a writer who edits rows AND recomputes every hash, manifest and digest inside the file is detectable only through the
        external anchors; one who also rewrites the anchors file is out of scope (DESIGN_71 section 3)."""
        out = {"rows_checked": 0, "row_hash_mismatches": [], "member_mismatches": [], "record_mismatches": [], "manifest_mismatches": [],
               "anchor_mismatches": []}
        with self._ro() as c:
            if c is None:
                return {**out, "store": "NOT_CREATED", "ok": True}
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
            sealed = c.execute("SELECT dataset_id, content_sha256, records_sha256 FROM mi_dataset_events "
                               "WHERE status IN ('COMPLETED', 'PARTIAL', 'FAILED', 'EMPTY')").fetchall()
            for d in sealed:
                ms = c.execute("SELECT natural_key, content_hash, record_sha256, disposition FROM mi_dataset_members WHERE dataset_id = ?",
                               (d["dataset_id"],)).fetchall()
                lines = {f"{m['natural_key']}\t{m['content_hash']}" for m in ms if m["disposition"] in VALUE_DISPOSITIONS}
                if manifest_sha256(lines) != d["content_sha256"] or manifest_sha256(m["record_sha256"] for m in ms if m["record_sha256"]) != d["records_sha256"]:
                    out["manifest_mismatches"].append(d["dataset_id"])
                a = (anchors or {}).get(d["dataset_id"])
                if a is not None and (a.get("content_sha256") != d["content_sha256"] or a.get("records_sha256") != d["records_sha256"]):
                    out["anchor_mismatches"].append(d["dataset_id"])
        out["ok"] = not any(out[k] for k in ("row_hash_mismatches", "member_mismatches", "record_mismatches", "manifest_mismatches", "anchor_mismatches"))
        return {**out, "store": "OK"}
