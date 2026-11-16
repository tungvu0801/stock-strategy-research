"""
historical_data/schema.py - the contract's entities (section 3), the licence block (6.2) and the dataset manifest (6.1), as frozen dataclasses.

Strict types: a price, volume or factor is an int or float that is finite (a bool is refused); a missing value is a P4 Missing object, never
0, False, an empty list or a forward-filled value. Sessions are YYYY-MM-DD strings, timestamps timezone-aware datetimes serialised as ISO-8601
UTC ending in Z. A daily bar that breaks section 3.2 (a non-positive price, negative volume, raw_low above min(open, close) or raw_high below
max(open, close)) cannot be constructed: an adapter removes such a row and lists it as a REMOVED_INVALID gap - it is never repaired.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Tuple, Union

from historical_data import CONTRACT_BASE_SHA256, CONTRACT_SHA256, CONTRACT_VERSION, KINDS
from historical_data.status import Missing, is_missing, unknown

UTC = timezone.utc
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ASSET_TYPES = ("COMMON_STOCK", "ETF", "OTHER")
GAP_REASONS = ("NO_BAR", "HALT", "SUSPENDED", "REMOVED_INVALID", "UNKNOWN")
EVENT_TYPES = ("SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND", "CASH_DIVIDEND", "SPECIAL_DIVIDEND", "SPIN_OFF", "SYMBOL_CHANGE", "MERGER", "DELISTING",
               "TICKER_REUSE")
SHARE_EVENTS = ("SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND")
DISTRIBUTION_EVENTS = ("CASH_DIVIDEND", "SPECIAL_DIVIDEND", "SPIN_OFF")
MEMBERSHIP_BASES = ("POINT_IN_TIME", "FIXED_CURRENT_UNIVERSE")
EVENTS_COMPLETENESS = ("COMPLETE", "PARTIAL", "NONE")
PRICE_VIEWS = ("RAW_CLOSE", "RAW_OHL", "SPLIT_ADJUSTED", "TOTAL_RETURN")
RESEARCH_USE = ("CONFIRMED", "UNKNOWN", "NOT_PERMITTED")
TIMINGS = ("BEFORE_OPEN", "AFTER_CLOSE", "DURING", "UNKNOWN")
SYMBOL_HISTORY_STATUS = ("KNOWN", "UNKNOWN")
VIEW_MANIFEST_FIELDS = ("contract_version", "contract_sha256", "kind", "provider", "adapter", "adapter_version", "start_session",
                        "price_views_available", "volume_feed", "price_source", "membership_basis", "events_completeness", "licence")   # A11 allow-list
FEED_RE = re.compile(r"^(CONSOLIDATED|SINGLE_VENUE:[A-Z0-9_]+)$")


class BarInvalid(ValueError):
    """A row that breaks contract section 3.2: removed by the adapter (REMOVED_INVALID), never repaired."""


# ------------------------------------------------------------------------------------------------ primitives
def number(v, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{name} must be a number, not {type(v).__name__}")
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite")
    return v


def positive(v, name: str) -> float:
    v = number(v, name)
    if v <= 0:
        raise ValueError(f"{name} must be > 0")
    return v


def positive_or_missing(v, name: str):
    return v if is_missing(v) else positive(v, name)


def session_str(s, name: str = "session") -> str:
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"{name} must be YYYY-MM-DD, not {s!r}")
    date.fromisoformat(s)
    return s


def session_or_missing(s, name: str):
    return s if is_missing(s) else session_str(s, name)


def utc_ts(t, name: str) -> datetime:
    if not isinstance(t, datetime) or t.tzinfo is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return t.astimezone(UTC)


def ts_or_missing(t, name: str):
    return t if is_missing(t) else utc_ts(t, name)


def iso_z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def jv(v):
    """JSON value of a field: Missing -> its object, datetime -> ISO Z, tuple -> list (recursively), dataclass with to_json -> its JSON."""
    if is_missing(v):
        return v.to_json()
    if isinstance(v, datetime):
        return iso_z(v)
    if isinstance(v, (tuple, list)):
        return [jv(x) for x in v]
    if hasattr(v, "to_json"):
        return v.to_json()
    return v


def _enum(v, allowed, name):
    if v not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, not {v!r}")
    return v


# ------------------------------------------------------------------------------------------------ section 3.1 instrument
@dataclass(frozen=True)
class SymbolSpan:
    symbol: str
    valid_from_session: str
    valid_to_session: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", self.symbol):
            raise ValueError(f"not a symbol: {self.symbol!r}")
        session_str(self.valid_from_session, "valid_from_session")
        if self.valid_to_session is not None:
            session_str(self.valid_to_session, "valid_to_session")

    def to_json(self):
        return {"symbol": self.symbol, "valid_from_session": self.valid_from_session, "valid_to_session": self.valid_to_session}


@dataclass(frozen=True)
class ListingSpan:
    exchange: str
    from_session: str
    to_session: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.exchange, str) or not self.exchange:
            raise ValueError("exchange must be a non-empty string")
        session_str(self.from_session, "from_session")
        if self.to_session is not None:
            session_str(self.to_session, "to_session")

    def to_json(self):
        return {"exchange": self.exchange, "from": self.from_session, "to": self.to_session}


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    symbol_history: Tuple[SymbolSpan, ...]
    symbol_history_status: str
    asset_type: Union[str, Missing]
    listing: Union[Tuple[ListingSpan, ...], Missing]
    sector: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_IN_SOURCE"))        # A4
    issuer_id: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_IN_SOURCE"))     # A4

    def __post_init__(self):
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ValueError("instrument_id must be a non-empty string")
        if not isinstance(self.symbol_history, tuple) or not self.symbol_history or not all(isinstance(x, SymbolSpan) for x in self.symbol_history):
            raise ValueError("symbol_history is a non-empty tuple of SymbolSpan")
        _enum(self.symbol_history_status, SYMBOL_HISTORY_STATUS, "symbol_history_status")
        if self.symbol_history_status == "UNKNOWN" and len(self.symbol_history) != 1:
            raise ValueError("with symbol_history_status UNKNOWN the history is its single entry (A8)")
        if not is_missing(self.asset_type):
            _enum(self.asset_type, ASSET_TYPES, "asset_type")
        if not is_missing(self.listing) and not (isinstance(self.listing, tuple) and all(isinstance(x, ListingSpan) for x in self.listing)):
            raise ValueError("listing is a tuple of ListingSpan or Missing")
        for name in ("sector", "issuer_id"):
            v = getattr(self, name)
            if not is_missing(v) and (not isinstance(v, str) or not v):
                raise ValueError(f"{name} is a non-empty string or Missing")

    def symbol_at(self, session: str) -> Optional[str]:
        """The symbol at `session`: with an UNKNOWN history its single entry applies to every session (A8)."""
        if self.symbol_history_status == "UNKNOWN":
            return self.symbol_history[0].symbol
        for sp in self.symbol_history:
            if sp.valid_from_session <= session and (sp.valid_to_session is None or session <= sp.valid_to_session):
                return sp.symbol
        return None

    def to_json(self):
        return {"instrument_id": self.instrument_id, "symbol_history": jv(self.symbol_history), "symbol_history_status": self.symbol_history_status,
                "asset_type": jv(self.asset_type), "listing": jv(self.listing), "sector": jv(self.sector), "issuer_id": jv(self.issuer_id)}


# ------------------------------------------------------------------------------------------------ section 3.2 daily bar
@dataclass(frozen=True)
class SourceRef:
    """Section 3.2 source_ref: "the archive file's sha256 plus the index of the row in it". file_sha256 is that value (the sha256 of the file's
    bytes as stored, e.g. the .json.gz); body_sha256 is the sha256 of the decompressed body, which the archives' own manifests record and the
    adapters verify (equal to file_sha256 for an uncompressed file). Both are recorded (MD-A declared addition, L-4)."""
    file_sha256: str
    body_sha256: str
    index: int

    def __post_init__(self):
        for k in ("file_sha256", "body_sha256"):
            v = getattr(self, k)
            if not isinstance(v, str) or not SHA_RE.match(v):
                raise ValueError(f"source_ref.{k} must be 64 lower-case hex characters")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError("source_ref.index must be a non-negative int")

    def to_json(self):
        return {"file_sha256": self.file_sha256, "body_sha256": self.body_sha256, "index": self.index}


def bar_problems(raw_open, raw_high, raw_low, raw_close, raw_volume):
    """Why a row breaks section 3.2 (an empty list: it does not). Missing open / high / low are allowed (P3)."""
    out = []
    vals = {"raw_open": raw_open, "raw_high": raw_high, "raw_low": raw_low, "raw_close": raw_close}
    for k, v in vals.items():
        if is_missing(v) and k != "raw_close":
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or float(v) <= 0:
            out.append(f"{k}_NOT_FINITE_POSITIVE")
    if isinstance(raw_volume, bool) or not isinstance(raw_volume, (int, float)) or not math.isfinite(float(raw_volume)) or float(raw_volume) < 0:
        out.append("RAW_VOLUME_INVALID")
    if out:
        return out
    oc = [float(raw_close)] + ([] if is_missing(raw_open) else [float(raw_open)])
    if not is_missing(raw_low) and float(raw_low) > min(oc):
        out.append("RAW_LOW_ABOVE_MIN_OPEN_CLOSE")
    if not is_missing(raw_high) and float(raw_high) < max(oc):
        out.append("RAW_HIGH_BELOW_MAX_OPEN_CLOSE")
    return out


@dataclass(frozen=True)
class DailyBar:
    instrument_id: str
    session: str
    raw_open: Union[float, Missing]
    raw_high: Union[float, Missing]
    raw_low: Union[float, Missing]
    raw_close: float
    raw_volume: float
    split_factor: Union[float, Missing]
    total_return_factor: Union[float, Missing]
    volume_feed: str
    price_source: str
    known_at: datetime
    source_ref: Union[SourceRef, Missing]

    def __post_init__(self):
        problems = bar_problems(self.raw_open, self.raw_high, self.raw_low, self.raw_close, self.raw_volume)
        if problems:
            raise BarInvalid(f"{self.instrument_id} {self.session}: {', '.join(problems)}")
        session_str(self.session)
        for k in ("raw_open", "raw_high", "raw_low"):
            v = getattr(self, k)
            if not is_missing(v):
                object.__setattr__(self, k, float(v))
        object.__setattr__(self, "raw_close", float(self.raw_close))
        object.__setattr__(self, "raw_volume", float(self.raw_volume))
        for k in ("split_factor", "total_return_factor"):
            object.__setattr__(self, k, positive_or_missing(getattr(self, k), k))
        if self.volume_feed != "UNKNOWN" and not FEED_RE.match(self.volume_feed):
            raise ValueError(f"volume_feed {self.volume_feed!r}")
        if not FEED_RE.match(self.price_source):
            raise ValueError(f"price_source {self.price_source!r}")
        object.__setattr__(self, "known_at", utc_ts(self.known_at, "known_at"))
        if not (is_missing(self.source_ref) or isinstance(self.source_ref, SourceRef)):
            raise TypeError("source_ref is a SourceRef or Missing")

    def to_json(self):
        return {k: jv(getattr(self, k)) for k in ("instrument_id", "session", "raw_open", "raw_high", "raw_low", "raw_close", "raw_volume",
                                                  "split_factor", "total_return_factor", "volume_feed", "price_source", "known_at", "source_ref")}


@dataclass(frozen=True)
class Gap:
    instrument_id: str
    session: str
    reason: str

    def __post_init__(self):
        session_str(self.session)
        _enum(self.reason, GAP_REASONS, "gap reason")

    def to_json(self):
        return {"instrument_id": self.instrument_id, "session": self.session, "reason": self.reason}


# ------------------------------------------------------------------------------------------------ section 3.3 corporate event
_TYPE_FIELDS = ("ratio", "cash_amount", "cash_in_lieu_per_share", "child_instrument_id", "child_ratio", "cost_basis_allocation",
                "acquirer_instrument_id", "stock_ratio", "last_trade_session", "last_raw_price", "delisting_reason", "delisting_return")
_APPLIES = {                                                                                  # section 3.3: the type-specific fields of each type
    **{t: ("ratio", "cash_in_lieu_per_share") for t in SHARE_EVENTS},
    "CASH_DIVIDEND": ("cash_amount",), "SPECIAL_DIVIDEND": ("cash_amount",),
    "SPIN_OFF": ("child_instrument_id", "child_ratio", "cost_basis_allocation", "cash_in_lieu_per_share"),
    "MERGER": ("acquirer_instrument_id", "stock_ratio", "cash_amount", "cash_in_lieu_per_share"),
    "DELISTING": ("last_trade_session", "last_raw_price", "delisting_reason", "delisting_return"),
    "SYMBOL_CHANGE": (), "TICKER_REUSE": (),
}


@dataclass(frozen=True)
class CorporateEvent:
    event_id: str
    instrument_id: str
    type: str
    effective_session: str
    known_at: Union[datetime, Missing]
    record_session: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_IN_SOURCE"))
    pay_session: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_IN_SOURCE"))
    ratio: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    cash_amount: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    cash_in_lieu_per_share: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    child_instrument_id: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    child_ratio: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    cost_basis_allocation: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    acquirer_instrument_id: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    stock_ratio: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    last_trade_session: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    last_raw_price: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    delisting_reason: Union[str, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    delisting_return: Union[float, Missing] = field(default_factory=lambda: unknown("NOT_PROVIDED"))

    def __post_init__(self):
        """P4 (L-6): every type-specific field is a value or a Missing object, never None. A field that does not apply to the type must stay
        Missing; the fields a type needs to be booked (P3a) must be present."""
        if not self.event_id or not self.instrument_id:
            raise ValueError("event_id and instrument_id are required")
        _enum(self.type, EVENT_TYPES, "event type")
        session_str(self.effective_session, "effective_session")
        object.__setattr__(self, "known_at", ts_or_missing(self.known_at, "known_at"))
        session_or_missing(self.record_session, "record_session")
        session_or_missing(self.pay_session, "pay_session")
        for k in _TYPE_FIELDS:
            v = getattr(self, k)
            if v is None:
                raise TypeError(f"{k} is a value or a P4 Missing object, never None")
            if k not in _APPLIES.get(self.type, ()) and not is_missing(v):
                raise ValueError(f"{k} does not apply to a {self.type} event")
        if self.type in SHARE_EVENTS:
            object.__setattr__(self, "ratio", positive(self.ratio, "ratio"))
        if self.type in ("CASH_DIVIDEND", "SPECIAL_DIVIDEND"):
            object.__setattr__(self, "cash_amount", positive(self.cash_amount, "cash_amount"))
        if self.type == "SPIN_OFF":
            if not isinstance(self.child_instrument_id, str) or not self.child_instrument_id:
                raise ValueError("a SPIN_OFF names its child_instrument_id")
            object.__setattr__(self, "child_ratio", positive(self.child_ratio, "child_ratio"))
        if self.type == "MERGER":
            if not is_missing(self.stock_ratio):
                object.__setattr__(self, "stock_ratio", positive(self.stock_ratio, "stock_ratio"))
                if not isinstance(self.acquirer_instrument_id, str) or not self.acquirer_instrument_id:
                    raise ValueError("a MERGER paid in stock names its acquirer_instrument_id")
            if not is_missing(self.cash_amount):
                c = number(self.cash_amount, "cash_amount")
                if c < 0:
                    raise ValueError("a MERGER's cash_amount is >= 0")
                object.__setattr__(self, "cash_amount", c)
            if is_missing(self.stock_ratio) and is_missing(self.cash_amount):
                raise ValueError("a MERGER replaces the position by stock_ratio acquirer shares and / or cash_amount per share (P3a): one is required")
            if not is_missing(self.acquirer_instrument_id) and (not isinstance(self.acquirer_instrument_id, str) or not self.acquirer_instrument_id):
                raise ValueError("acquirer_instrument_id is a non-empty string or Missing")
        if self.type == "DELISTING":
            session_str(self.last_trade_session, "last_trade_session")
            object.__setattr__(self, "last_raw_price", positive(self.last_raw_price, "last_raw_price"))
            if not is_missing(self.delisting_return):
                r = number(self.delisting_return, "delisting_return")
                if r < -1.0:
                    raise ValueError("delisting_return is >= -1 (relative to last_raw_price)")
                object.__setattr__(self, "delisting_return", r)
        if not is_missing(self.cash_in_lieu_per_share):
            v = number(self.cash_in_lieu_per_share, "cash_in_lieu_per_share")
            if v < 0:
                raise ValueError("cash_in_lieu_per_share is >= 0")
            object.__setattr__(self, "cash_in_lieu_per_share", v)
        if not is_missing(self.cost_basis_allocation):
            v = number(self.cost_basis_allocation, "cost_basis_allocation")
            if not 0.0 <= v <= 1.0:
                raise ValueError("cost_basis_allocation is a share of the parent's basis in [0, 1]")
            object.__setattr__(self, "cost_basis_allocation", v)
        for k in ("child_instrument_id", "delisting_reason"):
            v = getattr(self, k)
            if not is_missing(v) and (not isinstance(v, str) or not v):
                raise ValueError(f"{k} is a non-empty string or Missing")

    @property
    def known_at_status(self) -> str:
        return "UNKNOWN" if is_missing(self.known_at) else "KNOWN"

    def to_json(self):
        out = {"event_id": self.event_id, "instrument_id": self.instrument_id, "type": self.type, "effective_session": self.effective_session,
               "known_at": jv(self.known_at), "known_at_status": self.known_at_status, "record_session": jv(self.record_session),
               "pay_session": jv(self.pay_session)}
        for k in _TYPE_FIELDS:
            out[k] = jv(getattr(self, k))
        return out


# ------------------------------------------------------------------------------------------------ section 3.4 membership
@dataclass(frozen=True)
class MembershipChange:
    universe_id: str
    instrument_id: str
    session: str
    action: str
    known_at: Union[datetime, Missing]

    def __post_init__(self):
        session_str(self.session)
        _enum(self.action, ("ADD", "REMOVE"), "membership action")
        object.__setattr__(self, "known_at", ts_or_missing(self.known_at, "known_at"))

    def to_json(self):
        return {"universe_id": self.universe_id, "instrument_id": self.instrument_id, "session": self.session, "action": self.action,
                "known_at": jv(self.known_at)}


@dataclass(frozen=True)
class MembershipSet:
    universe_id: str
    session: str
    membership_basis: str
    instrument_ids: Tuple[str, ...]
    flags: Tuple[str, ...]
    change_known_at: Tuple[Tuple[str, Union[datetime, Missing]], ...]

    def to_json(self):
        return {"universe_id": self.universe_id, "session": self.session, "membership_basis": self.membership_basis,
                "instrument_ids": list(self.instrument_ids), "flags": list(self.flags),
                "change_known_at": [[i, jv(k)] for i, k in self.change_known_at]}


# ------------------------------------------------------------------------------------------------ section 3.5 scheduled events
@dataclass(frozen=True)
class ScheduledEvent:
    event_id: str
    instrument_id: str
    event_session: str
    timing: str
    known_at: datetime
    kind: str = "EARNINGS"

    def __post_init__(self):
        session_str(self.event_session, "event_session")
        _enum(self.timing, TIMINGS, "timing")
        object.__setattr__(self, "known_at", utc_ts(self.known_at, "known_at"))

    def to_json(self):
        return {"event_id": self.event_id, "instrument_id": self.instrument_id, "event_session": self.event_session, "timing": self.timing,
                "known_at": jv(self.known_at), "kind": self.kind}


# ------------------------------------------------------------------------------------------------ section 6.2 licence
@dataclass(frozen=True)
class Licence:
    licence_ref: Union[str, Missing]
    personal_research_use: str
    redistribution: str = "NONE"
    licence_file_sha256: Optional[str] = None
    confirmed_by: Optional[str] = None
    confirmed_on: Optional[str] = None

    def __post_init__(self):
        _enum(self.personal_research_use, RESEARCH_USE, "personal_research_use")
        if self.redistribution != "NONE":
            raise ValueError("redistribution is NONE")
        conf = (self.licence_file_sha256, self.confirmed_by, self.confirmed_on)
        if self.personal_research_use == "CONFIRMED":
            if not (isinstance(self.licence_file_sha256, str) and SHA_RE.match(self.licence_file_sha256) and self.confirmed_by and self.confirmed_on):
                raise ValueError("CONFIRMED needs an owner-authored licence file by its sha256, confirmed_by and confirmed_on")
            session_str(self.confirmed_on, "confirmed_on")
        elif any(x is not None for x in conf):
            raise ValueError("licence_file_sha256 / confirmed_by / confirmed_on belong to CONFIRMED only")

    def to_json(self):
        out = {"licence_ref": jv(self.licence_ref), "personal_research_use": self.personal_research_use, "redistribution": self.redistribution}
        if self.personal_research_use == "CONFIRMED":
            out.update(licence_file_sha256=self.licence_file_sha256, confirmed_by=self.confirmed_by, confirmed_on=self.confirmed_on)
        return out


def licence_unknown() -> Licence:
    """What every adapter emits (section 6.2, A10)."""
    return Licence(licence_ref=unknown("NO_LICENCE_FILE"), personal_research_use="UNKNOWN")


# ------------------------------------------------------------------------------------------------ section 6.1 manifest
@dataclass(frozen=True)
class SourceFile:
    """Section 6.1 source_files "each with its sha256": file_sha256 = the file's bytes as stored; body_sha256 = its decompressed body (the
    value an archive manifest records and the adapter verifies), equal to file_sha256 for an uncompressed file (MD-A declared addition, L-4)."""
    path: str
    file_sha256: str
    body_sha256: str

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path or ".." in self.path.replace("\\", "/").split("/"):
            raise ValueError(f"source file path {self.path!r}")
        if not SHA_RE.match(self.file_sha256) or not SHA_RE.match(self.body_sha256):
            raise ValueError("source file sha256")

    def to_json(self):
        return {"path": self.path, "file_sha256": self.file_sha256, "body_sha256": self.body_sha256}


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    kind: str
    provider: str
    adapter: str
    adapter_version: str
    source_files: Tuple[SourceFile, ...]
    start_session: Union[str, Missing]
    end_session: Union[str, Missing]
    instrument_count: int
    price_views_available: Tuple[str, ...]
    volume_feed: str
    price_source: str
    membership_basis: str
    events_completeness: str
    adjustment_as_of: Union[str, Missing]
    retrieved_at: Union[datetime, Missing]
    licence: Licence
    data_exposure: Union[Tuple[dict, ...], Missing]
    content_sha256: Optional[str] = None
    contract_version: str = CONTRACT_VERSION
    contract_sha256: str = CONTRACT_SHA256
    contract_base_sha256: str = CONTRACT_BASE_SHA256

    def __post_init__(self):
        _enum(self.kind, KINDS, "kind")
        if not all(isinstance(x, SourceFile) for x in self.source_files):
            raise TypeError("source_files are SourceFile")
        session_or_missing(self.start_session, "start_session")
        session_or_missing(self.end_session, "end_session")
        if isinstance(self.instrument_count, bool) or not isinstance(self.instrument_count, int) or self.instrument_count < 0:
            raise ValueError("instrument_count")
        for v in self.price_views_available:
            _enum(v, PRICE_VIEWS, "price view")
        if tuple(sorted(set(self.price_views_available), key=PRICE_VIEWS.index)) != tuple(self.price_views_available):
            raise ValueError("price_views_available is ordered as PRICE_VIEWS, without repeats")
        if self.volume_feed != "UNKNOWN" and not FEED_RE.match(self.volume_feed):
            raise ValueError("volume_feed")
        if not FEED_RE.match(self.price_source):
            raise ValueError("price_source")
        _enum(self.membership_basis, MEMBERSHIP_BASES, "membership_basis")
        _enum(self.events_completeness, EVENTS_COMPLETENESS, "events_completeness")
        session_or_missing(self.adjustment_as_of, "adjustment_as_of")
        object.__setattr__(self, "retrieved_at", ts_or_missing(self.retrieved_at, "retrieved_at"))
        if not isinstance(self.licence, Licence):
            raise TypeError("licence")
        if not (is_missing(self.data_exposure) or isinstance(self.data_exposure, tuple)):
            raise TypeError("data_exposure is a tuple of records or Missing (never an empty list for unknown)")
        if self.content_sha256 is not None and not SHA_RE.match(self.content_sha256):
            raise ValueError("content_sha256")

    def to_json(self, *, include_content_sha256: bool = True) -> dict:
        out = {"dataset_id": self.dataset_id, "contract_version": self.contract_version, "contract_sha256": self.contract_sha256,
               "contract_base_sha256": self.contract_base_sha256, "kind": self.kind, "provider": self.provider, "adapter": self.adapter,
               "adapter_version": self.adapter_version, "source_files": jv(self.source_files), "start_session": jv(self.start_session),
               "end_session": jv(self.end_session), "instrument_count": self.instrument_count, "price_views_available": list(self.price_views_available),
               "volume_feed": self.volume_feed, "price_source": self.price_source, "membership_basis": self.membership_basis,
               "events_completeness": self.events_completeness, "adjustment_as_of": jv(self.adjustment_as_of), "retrieved_at": jv(self.retrieved_at),
               "licence": self.licence.to_json(), "data_exposure": jv(self.data_exposure)}
        if include_content_sha256:
            out["content_sha256"] = self.content_sha256
        return out

    def view_json(self) -> dict:
        """The manifest a PointInTimeView carries: the A11 allow-list, nothing else."""
        full = self.to_json(include_content_sha256=False)
        return {k: full[k] for k in VIEW_MANIFEST_FIELDS}
