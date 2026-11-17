"""md_eval/contract.py - the CONSUMER-side view of the historical data contract v2 (base §3-§4, §6, amendment A1-A11): the record shapes MD-B
reads, the explicit missing value (P4), the two sentinels, the provider and view protocols (structural: a provider fits without importing
anything from here), and the two checks made before any data is read: the version (§8) and the licence (§6.2).

The protocols mirror §4 as amended:
  provider: manifest, instruments, bars, gaps, events, membership, scheduled_events, session_times, sessions (A1), next_session (A1),
            as_of, execution_open(instrument_id, session, *, decision_session) (A2: raises unless session == next_session(decision_session))
  view:     the same queries filtered to known_at <= t, factors rebased so the last visible bar's factor is exactly 1.0 (A6), the manifest
            reduced to the A11 allow-list, and NO execution_open and no reference to the provider (A2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Tuple

from md_eval import CONTRACT_BASE_OF, SUPPORTED_CONTRACTS


@dataclass(frozen=True)
class Missing:
    """P4: a missing value is {"status": UNKNOWN | UNAVAILABLE, "reason": CODE}; never 0, False, an empty list or a forward-filled value."""
    status: str
    reason: str

    def __post_init__(self):
        if self.status not in ("UNKNOWN", "UNAVAILABLE"):
            raise ValueError(f"a missing value's status is UNKNOWN or UNAVAILABLE; got {self.status!r}")

    def canonical(self) -> dict:
        return {"status": self.status, "reason": self.reason}


def unknown(reason: str) -> Missing:
    return Missing("UNKNOWN", reason)


def unavailable(reason: str) -> Missing:
    return Missing("UNAVAILABLE", reason)


def is_missing(x) -> bool:
    return isinstance(x, Missing)


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


NO_BAR = _Sentinel("NO_BAR")                                            # execution_open: no bar (or a removed one) -> NO_FILL_GAP
EARNINGS_DATA_UNAVAILABLE = _Sentinel("EARNINGS_DATA_UNAVAILABLE")      # scheduled_events with no point-in-time source (§3.5)


class ContractError(Exception):
    pass


class UnsupportedContract(ContractError):
    pass


class LicenceRefused(ContractError):
    pass


class ExecutionChannelViolation(ContractError):
    pass


class CalendarNotCovered(ContractError):
    pass


# ---- records (§3, A4) ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    symbol_history: Tuple[dict, ...]                        # ({symbol, valid_from_session, valid_to_session or None}, ...)
    symbol_history_status: str = "KNOWN"                    # KNOWN | UNKNOWN
    asset_type: str = "COMMON_STOCK"                        # COMMON_STOCK | ETF | OTHER
    listing: Any = field(default_factory=lambda: unknown("NO_LISTING_DATA"))
    sector: Any = field(default_factory=lambda: unknown("SECTOR_NOT_PROVIDED"))         # A4: a string or UNKNOWN
    issuer_id: Any = field(default_factory=lambda: unknown("ISSUER_NOT_PROVIDED"))     # A4: e.g. SEC:<cik10>, or UNKNOWN


def symbol_at(instrument: Instrument, session: str) -> Tuple[Optional[str], bool]:
    """(the instrument's symbol at the session or None, whether A8 applied). A8: with symbol_history_status UNKNOWN the single entry applies
    to every session (flag SYMBOL_HISTORY_UNKNOWN); more than one entry under UNKNOWN is ambiguous and gives None."""
    hist = tuple(instrument.symbol_history or ())
    if instrument.symbol_history_status == "UNKNOWN":
        return (hist[0]["symbol"], True) if len(hist) == 1 else (None, True)
    for h in hist:
        lo, hi = h.get("valid_from_session"), h.get("valid_to_session")
        if (lo is None or lo <= session) and (hi is None or session <= hi):
            return h["symbol"], False
    return None, False


@dataclass(frozen=True)
class DailyBar:
    instrument_id: str
    session: str
    raw_open: Any
    raw_high: Any
    raw_low: Any
    raw_close: Any
    raw_volume: Any
    split_factor: Any = 1.0
    total_return_factor: Any = 1.0
    volume_feed: str = "CONSOLIDATED"
    price_source: str = "CONSOLIDATED"
    known_at: str = ""                                      # close_utc(session) (§2)
    source_ref: str = ""


@dataclass(frozen=True)
class Gap:
    instrument_id: str
    session: str
    reason: str                                             # NO_BAR | HALT | SUSPENDED | REMOVED_INVALID | UNKNOWN


@dataclass(frozen=True)
class CorporateEvent:
    event_id: str
    instrument_id: str
    type: str                                               # SPLIT ... TICKER_REUSE (§3.3)
    effective_session: str
    known_at: Any                                           # an ISO-8601 UTC string, or UNKNOWN (then visible from the open of effective_session)
    record_session: Any = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    pay_session: Any = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    ratio: Any = None
    cash_amount: Any = None
    cash_in_lieu_per_share: Any = field(default_factory=lambda: unknown("NOT_PROVIDED"))
    child_instrument_id: Any = None
    child_ratio: Any = None
    cost_basis_allocation: Any = field(default_factory=lambda: unknown("NOT_PROVIDED"))   # the child's share of the basis (0..1), or UNKNOWN
    acquirer_instrument_id: Any = None
    stock_ratio: Any = None
    last_trade_session: Any = None
    last_raw_price: Any = None
    delisting_reason: Any = None
    delisting_return: Any = field(default_factory=lambda: unknown("NOT_PROVIDED"))

    @property
    def known_at_status(self) -> str:
        return "UNKNOWN" if is_missing(self.known_at) else "KNOWN"


@dataclass(frozen=True)
class MembershipSet:
    universe_id: str
    session: str
    instrument_ids: Tuple[str, ...]
    membership_basis: str                                   # POINT_IN_TIME | FIXED_CURRENT_UNIVERSE
    known_at: Any = None                                    # POINT_IN_TIME: {instrument_id: known_at of its latest change}


@dataclass(frozen=True)
class ScheduledEvent:
    instrument_id: str
    event_session: str
    timing: str                                             # BEFORE_OPEN | AFTER_CLOSE | DURING | UNKNOWN
    known_at: Any


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    contract_version: str
    contract_sha256: str
    kind: str                                               # HISTORICAL | SYNTHETIC
    provider: str
    adapter: str
    adapter_version: str
    start_session: str
    end_session: str
    instrument_count: int
    content_sha256: str
    price_views_available: Tuple[str, ...]                  # e.g. ("RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN")
    volume_feed: str
    price_source: str
    membership_basis: str
    events_completeness: str                                # COMPLETE | PARTIAL | NONE
    licence: dict
    source_files: Tuple[dict, ...] = ()
    adjustment_as_of: str = ""
    retrieved_at: str = ""
    data_exposure: Any = field(default_factory=lambda: unknown("NOT_RECORDED"))


VIEW_MANIFEST_FIELDS = ("contract_version", "contract_sha256", "kind", "provider", "adapter", "adapter_version", "start_session",
                        "price_views_available", "volume_feed", "price_source", "membership_basis", "events_completeness", "licence")   # A11


class PointInTimeView(Protocol):
    decision_time_utc: str

    def manifest(self) -> dict: ...
    def instruments(self) -> list: ...
    def bars(self, instrument_id: str, start_session: str, end_session: str) -> list: ...
    def gaps(self, instrument_id: str) -> list: ...
    def events(self, instrument_id=None, start_session=None, end_session=None) -> list: ...
    def membership(self, universe_id: str, session: str) -> MembershipSet: ...
    def scheduled_events(self, instrument_id: str, start_session: str, end_session: str): ...
    def session_times(self, session: str) -> Tuple[str, str]: ...
    def sessions(self, start_session: str, end_session: str) -> list: ...
    def next_session(self, session: str) -> str: ...


class HistoricalDataProvider(Protocol):
    def manifest(self) -> DatasetManifest: ...
    def instruments(self) -> list: ...
    def bars(self, instrument_id: str, start_session: str, end_session: str) -> list: ...
    def gaps(self, instrument_id: str) -> list: ...
    def events(self, instrument_id=None, start_session=None, end_session=None) -> list: ...
    def membership(self, universe_id: str, session: str) -> MembershipSet: ...
    def scheduled_events(self, instrument_id: str, start_session: str, end_session: str): ...
    def session_times(self, session: str) -> Tuple[str, str]: ...
    def sessions(self, start_session: str, end_session: str) -> list: ...
    def next_session(self, session: str) -> str: ...
    def as_of(self, decision_time_utc: str) -> PointInTimeView: ...
    def execution_open(self, instrument_id: str, session: str, *, decision_session: str): ...


# ---- the checks made before any data is read -------------------------------------------------------------------------------------------
def check_version(m: DatasetManifest) -> None:
    """§8: the engine refuses any contract version (or sha) it does not support."""
    if SUPPORTED_CONTRACTS.get(m.contract_version) != m.contract_sha256:
        raise UnsupportedContract(f"unsupported contract {m.contract_version!r} / {m.contract_sha256!r}; supported: {dict(SUPPORTED_CONTRACTS)} "
                                  f"(base {dict(CONTRACT_BASE_OF)})")


def licence_gate(m) -> str:
    """§6.2 / §7 / A10: before any HISTORICAL computation of returns or signals. SYNTHETIC is exempt. HISTORICAL needs
    personal_research_use = CONFIRMED with the owner-authored licence file's sha256, confirmed_by and confirmed_on; UNKNOWN allows only
    integrity checks (none of which this engine runs); NOT_PERMITTED allows no use except deletion. A refusal is a hard error.
    `m` is the full DatasetManifest or the view's allow-listed manifest (a mapping with kind and licence, A11): the same rule for both, so the
    engine, the benchmarks and the signal bridge share one gate."""
    kind = m.get("kind") if hasattr(m, "get") else getattr(m, "kind", None)
    licence = m.get("licence") if hasattr(m, "get") else getattr(m, "licence", None)
    if kind == "SYNTHETIC":
        return "SYNTHETIC_EXEMPT"
    if kind != "HISTORICAL":
        raise LicenceRefused(f"dataset kind must be HISTORICAL or SYNTHETIC; got {kind!r}")
    lic = dict(licence or {})
    use = lic.get("personal_research_use")
    if use == "NOT_PERMITTED":
        raise LicenceRefused("personal_research_use = NOT_PERMITTED: no use except deletion")
    if use != "CONFIRMED":
        raise LicenceRefused(f"personal_research_use = {use!r}: no returns, signals, ledger runs or statistics on HISTORICAL data (§6.2)")
    absent = [k for k in ("licence_file_sha256", "confirmed_by", "confirmed_on") if not lic.get(k)]
    if absent:
        raise LicenceRefused(f"CONFIRMED without {', '.join(absent)}: CONFIRMED comes only from an owner-authored licence file referenced by sha256")
    return "CONFIRMED"
