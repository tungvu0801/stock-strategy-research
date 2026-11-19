"""research_gate/bridge.py - the conversion layer from MD-A's records (historical_data) to the shapes md_eval reads (md_eval.contract), the
manifest-only view of a provider, and AdmittedView: the only decision view md_eval's signal sources accept.

Every md_eval record is built BY KEYWORD with every field passed. A Missing becomes md_eval's Missing (md_eval tests missingness by its own
class), MD-A's NO_BAR / EARNINGS_DATA_UNAVAILABLE become md_eval's sentinels, datetimes become ISO-8601 UTC strings ending in Z, an
integrity-only bar refuses the run (never a silent no-fill), an unknown instrument in the execution channel raises (never NO_BAR), and an
unknown universe raises md_eval's ContractError, as the contract double does (O-6 parity). The view's calendar is MD-A's coverage calendar
(declared difference DIFF-3); the dataset's calendar is clamped to the dataset span."""
from __future__ import annotations

import json
import weakref
from datetime import datetime
from types import MappingProxyType

from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data import provider as PV
from historical_data import schema as HSC
from historical_data import status as HS
from historical_data.adapters import ms1_evidence as MS1
from historical_data.adapters import sip_daily_v1 as SIP
from historical_data.adapters import synthetic as SY
from historical_data.provider import _TableProvider
from md_eval import contract as MC
from md_eval.double import ContractDouble
from research_gate.snapshot import BRIDGE_UNMAPPABLE, INTEGRITY_ONLY_BAR, NOT_ADMITTED_VIEW, AdmissionRefused, NotAdmitted

MDA_PROVIDER_TYPES = (SY.SyntheticProvider, MS1.Ms1EvidenceProvider, SIP.SipDailyV1Provider)


# ---- values (M-1, M-2, M-3) -----------------------------------------------------------------------------------------------------------
def missing(x):
    """An MD-A Missing -> md_eval's Missing (same status and reason)."""
    return MC.Missing(x.status, x.reason)


def value(x):
    return missing(x) if HS.is_missing(x) else x


def iso_z(t) -> str:
    return HSC.iso_z(t)


def ts_or_missing(t):
    return missing(t) if HS.is_missing(t) else iso_z(t)


def none_if_missing(x):
    return None if HS.is_missing(x) else x


def scheduled(x):
    """M-3: EARNINGS_DATA_UNAVAILABLE (by value) -> md_eval's sentinel; another Missing -> md_eval's Missing; a list -> md_eval records."""
    if HS.is_missing(x):
        return MC.EARNINGS_DATA_UNAVAILABLE if x == HS.EARNINGS_DATA_UNAVAILABLE else missing(x)
    return [MC.ScheduledEvent(instrument_id=s.instrument_id, event_session=s.event_session, timing=s.timing, known_at=iso_z(s.known_at)) for s in x]


def _json(x):
    return json.loads(CN.dumps(x))


# ---- records (M-6 .. M-16) ----------------------------------------------------------------------------------------------------------------
def manifest(m) -> MC.DatasetManifest:
    """M-6: an MD-A DatasetManifest -> md_eval's, field by field; the licence, source files and data exposure normalised by a JSON round trip."""
    exposure = missing(m.data_exposure) if HS.is_missing(m.data_exposure) else tuple(_json(list(HSC.jv(m.data_exposure))))
    return MC.DatasetManifest(dataset_id=m.dataset_id, contract_version=m.contract_version, contract_sha256=m.contract_sha256, kind=m.kind,
                              provider=m.provider, adapter=m.adapter, adapter_version=m.adapter_version, start_session=value(m.start_session),
                              end_session=value(m.end_session), instrument_count=m.instrument_count, content_sha256=m.content_sha256,
                              price_views_available=tuple(m.price_views_available), volume_feed=m.volume_feed, price_source=m.price_source,
                              membership_basis=m.membership_basis, events_completeness=m.events_completeness, licence=_json(m.licence.to_json()),
                              source_files=tuple(_json([s.to_json() for s in m.source_files])), adjustment_as_of=value(m.adjustment_as_of),
                              retrieved_at=ts_or_missing(m.retrieved_at), data_exposure=exposure)


def instrument(i) -> MC.Instrument:
    """M-11 (with M-9, M-10): every field passed, so no md_eval default (COMMON_STOCK, SECTOR_NOT_PROVIDED) can stand in for a fact."""
    listing = missing(i.listing) if HS.is_missing(i.listing) else tuple(x.to_json() for x in i.listing)
    return MC.Instrument(instrument_id=i.instrument_id, symbol_history=tuple(s.to_json() for s in i.symbol_history),
                         symbol_history_status=i.symbol_history_status, asset_type=value(i.asset_type), listing=listing, sector=value(i.sector),
                         issuer_id=value(i.issuer_id))


def bar(b) -> MC.DailyBar:
    """M-12, M-13: prices and factors as floats or md_eval Missing; known_at ISO-Z; source_ref a lossless JSON string (never empty)."""
    return MC.DailyBar(instrument_id=b.instrument_id, session=b.session, raw_open=value(b.raw_open), raw_high=value(b.raw_high), raw_low=value(b.raw_low),
                       raw_close=b.raw_close, raw_volume=b.raw_volume, split_factor=value(b.split_factor), total_return_factor=value(b.total_return_factor),
                       volume_feed=b.volume_feed, price_source=b.price_source, known_at=iso_z(b.known_at),
                       source_ref=CN.dumps(HSC.jv(b.source_ref)).decode("utf-8"))


def gap(g) -> MC.Gap:
    return MC.Gap(instrument_id=g.instrument_id, session=g.session, reason=g.reason)


_NONE_DEFAULT = ("ratio", "cash_amount", "child_instrument_id", "child_ratio", "acquirer_instrument_id", "stock_ratio", "last_trade_session",
                 "last_raw_price", "delisting_reason")
_MISSING_DEFAULT = ("record_session", "pay_session", "cash_in_lieu_per_share", "cost_basis_allocation", "delisting_return")


def event(e) -> MC.CorporateEvent:
    """M-16: a Missing where md_eval defaults to None becomes None (a Missing object is truthy); where md_eval defaults to Missing, md_eval's."""
    kw = {k: none_if_missing(getattr(e, k)) for k in _NONE_DEFAULT}
    kw.update({k: value(getattr(e, k)) for k in _MISSING_DEFAULT})
    return MC.CorporateEvent(event_id=e.event_id, instrument_id=e.instrument_id, type=e.type, effective_session=e.effective_session,
                             known_at=ts_or_missing(e.known_at), **kw)


def membership_set(ms) -> MC.MembershipSet:
    """M-8: by keyword (the field order differs); known_at only for POINT_IN_TIME; MD-A's flags are dropped (md_eval derives them)."""
    known = MappingProxyType({iid: ts_or_missing(k) for iid, k in ms.change_known_at}) if ms.membership_basis == "POINT_IN_TIME" else None
    return MC.MembershipSet(universe_id=ms.universe_id, session=ms.session, instrument_ids=tuple(ms.instrument_ids), membership_basis=ms.membership_basis,
                            known_at=known)


def view_manifest(d: dict) -> dict:
    """M-23: the view's allow-listed manifest; price_views_available as a tuple."""
    out = dict(d)
    out["price_views_available"] = tuple(out.get("price_views_available") or ())
    return out


def _calendar_error(fn, *a):
    try:
        return fn(*a)
    except (CAL.CalendarNotCovered, CAL.NotASession, ValueError, TypeError) as exc:
        raise MC.CalendarNotCovered(f"{a[0] if a else ''}: CALENDAR_NOT_COVERED ({type(exc).__name__})") from None


def parse_decision_time(t) -> datetime:
    """M-21: strictly an ISO-8601 UTC string ending in Z."""
    if not isinstance(t, str) or not t.endswith("Z"):
        raise ValueError("a decision time is an ISO-8601 UTC string ending in Z")
    dt = datetime.fromisoformat(t[:-1] + "+00:00")
    if dt.tzinfo is None:
        raise ValueError("a decision time must be timezone-aware")
    return dt


# ---- the manifest-only view of a provider (Part A 4.2) ------------------------------------------------------------------------------------
def manifest_view_raw(x):
    """The manifest of anything that is not an AdmittedDataset, reading no data: a contract double through its own manifest() (it records
    "manifest", as the engine always did); an MD-A provider through the class-level read, converted; anything else through its manifest(),
    converted only when it is an MD-A manifest (admission refuses it later, after the pre-data gates)."""
    if type(x) is ContractDouble:
        return x.manifest()
    if type(x) in MDA_PROVIDER_TYPES:
        return manifest(_TableProvider.manifest(x))
    m = x.manifest()
    return manifest(m) if type(m) is HSC.DatasetManifest else m


# ---- the MD-A dataset behind an AdmittedDataset (the provider protocol, converted) -------------------------------------------------------------
def universes(snap) -> frozenset:
    return frozenset(c.universe_id for c in snap._changes)


def mda_instruments(snap) -> list:
    return [instrument(i) for i in _TableProvider.instruments(snap)]


def mda_bars(snap, instrument_id, start_session, end_session) -> list:
    if instrument_id not in snap._inst:
        return []                                                                    # M-5: the double's answer for an unknown instrument
    return [bar(b) for b in _TableProvider.bars(snap, instrument_id, start_session, end_session)]


def mda_gaps(snap, instrument_id) -> list:
    if instrument_id not in snap._inst:
        return []
    return [gap(g) for g in _TableProvider.gaps(snap, instrument_id)]


def mda_events(snap, instrument_id=None, start_session=None, end_session=None) -> list:
    return [event(e) for e in _TableProvider.events(snap, instrument_id, start_session, end_session)]


def mda_membership(snap, universe_id, session) -> MC.MembershipSet:
    if universe_id not in universes(snap):
        raise MC.ContractError(f"unknown universe {universe_id!r}")
    return membership_set(_TableProvider.membership(snap, universe_id, session))


def mda_scheduled(snap, instrument_id, start_session, end_session):
    return scheduled(_TableProvider.scheduled_events(snap, instrument_id, start_session, end_session))


def _span(snap):
    m = _TableProvider.manifest(snap)
    return m.start_session, m.end_session


def mda_session_times(snap, session):
    """M-18: ISO-Z strings; a session outside the dataset span, or one the calendar refuses, is md_eval's CalendarNotCovered."""
    a, b = _span(snap)
    if not isinstance(session, str) or not (a <= session <= b):
        raise MC.CalendarNotCovered(f"{session}: CALENDAR_NOT_COVERED (outside the dataset)")
    o, c = _calendar_error(CAL.session_times, session)
    return iso_z(o), iso_z(c)


def mda_sessions(snap, start_session, end_session) -> list:
    """M-19: the calendar sessions clamped to the dataset span (string comparison, as the double's calendar)."""
    a, b = _span(snap)
    lo, hi = max(start_session, a), min(end_session, b)
    return [s for s in CAL.SESSIONS if lo <= s <= hi]


def mda_next_session(snap, session) -> str:
    """M-20: at or after the dataset's last session (or before its first) md_eval's CalendarNotCovered, as the double at its end."""
    a, b = _span(snap)
    if not isinstance(session, str) or session >= b or session < a:
        raise MC.CalendarNotCovered(f"{session}: CALENDAR_NOT_COVERED (no next session in the dataset)")
    return _calendar_error(CAL.next_session, session)


def mda_execution_open(snap, instrument_id, session, *, decision_session):
    """M-2, M-4, M-5, M-24, M-25: NO_BAR by value -> md_eval's NO_BAR; a float passes; an integrity-only bar refuses the run; an unknown
    instrument raises (never NO_BAR); the timing error and the licence refusal become md_eval's."""
    if instrument_id not in snap._inst:
        raise MC.ContractError("UNKNOWN_INSTRUMENT")
    try:
        x = _TableProvider.execution_open(snap, instrument_id, session, decision_session=decision_session)
    except PV.ExecutionTimingError as exc:
        raise MC.ExecutionChannelViolation(str(exc)) from None
    except PV.IntegrityOnly:
        raise AdmissionRefused(BRIDGE_UNMAPPABLE, (INTEGRITY_ONLY_BAR,)) from None
    except PV.LicenceRefused as exc:
        raise MC.LicenceRefused(str(exc)) from None
    except (CAL.CalendarNotCovered, CAL.NotASession) as exc:
        raise MC.CalendarNotCovered(f"{decision_session}: CALENDAR_NOT_COVERED ({type(exc).__name__})") from None
    if HS.is_missing(x) and x == HS.NO_BAR:
        return MC.NO_BAR
    if type(x) is float:
        return x
    raise AdmissionRefused(BRIDGE_UNMAPPABLE, ("EXECUTION_VALUE_NOT_MAPPABLE",))


def mda_as_of(snap, decision_time_utc, admission_sha256):
    t = parse_decision_time(decision_time_utc)
    try:
        inner = _TableProvider.as_of(snap, t)
    except PV.IntegrityOnly:
        raise AdmissionRefused(BRIDGE_UNMAPPABLE, (INTEGRITY_ONLY_BAR,)) from None
    except PV.LicenceRefused as exc:
        raise MC.LicenceRefused(str(exc)) from None
    return _issue_view(inner, admission_sha256, "MDA", universes(snap))


# ---- AdmittedView (Part A 4.3) ---------------------------------------------------------------------------------------------------------
_VIEW_TOKEN = object()
VIEW_REG: dict = {}


def _register(reg: dict, obj) -> None:
    k = id(obj)

    def _gone(r, k=k, reg=reg):
        if reg.get(k) is r:                                                          # never remove a newer entry that reuses the id
            del reg[k]
    reg[k] = weakref.ref(obj, _gone)


def _issued(reg: dict, x) -> bool:
    r = reg.get(id(x))
    return r is not None and r() is x


class AdmittedView:
    """The decision view of an admitted dataset: MD-B's view protocol only (no execution channel, no proxying of other attributes). It holds
    the inner view object only - MD-A's PointInTimeView or the snapshot double's view - never the dataset, the snapshot or a double."""
    __slots__ = ("_inner", "_admission_sha256", "_flavour", "_universes", "__weakref__")

    def __init__(self, _token, inner, admission_sha256, flavour, universe_ids):
        if _token is not _VIEW_TOKEN:
            raise NotAdmitted(NOT_ADMITTED_VIEW)
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_admission_sha256", admission_sha256)
        object.__setattr__(self, "_flavour", flavour)
        object.__setattr__(self, "_universes", universe_ids)

    def __setattr__(self, name, v):
        raise AttributeError("an AdmittedView is read-only")

    def __delattr__(self, name):
        raise AttributeError("an AdmittedView is read-only")

    @property
    def decision_time_utc(self) -> str:
        if self._flavour == "MDB":
            return self._inner.decision_time_utc
        return iso_z(self._inner.decision_time)

    def manifest(self) -> dict:
        if self._flavour == "MDB":
            return self._inner.manifest()
        return view_manifest(self._inner.manifest())

    def instruments(self) -> list:
        if self._flavour == "MDB":
            return self._inner.instruments()
        return [instrument(i) for i in self._inner.instruments()]

    def bars(self, instrument_id, start_session, end_session) -> list:
        if self._flavour == "MDB":
            return self._inner.bars(instrument_id, start_session, end_session)
        return [bar(b) for b in self._inner.bars(instrument_id, start_session, end_session)]

    def gaps(self, instrument_id) -> list:
        if self._flavour == "MDB":
            return self._inner.gaps(instrument_id)
        return [gap(g) for g in self._inner.gaps(instrument_id)]

    def events(self, instrument_id=None, start_session=None, end_session=None) -> list:
        if self._flavour == "MDB":
            return self._inner.events(instrument_id, start_session, end_session)
        return [event(e) for e in self._inner.events(instrument_id, start_session, end_session)]

    def membership(self, universe_id, session) -> MC.MembershipSet:
        if self._flavour == "MDB":
            return self._inner.membership(universe_id, session)
        if universe_id not in self._universes:
            raise MC.ContractError(f"unknown universe {universe_id!r}")
        return membership_set(self._inner.membership(universe_id, session))

    def scheduled_events(self, instrument_id, start_session, end_session):
        if self._flavour == "MDB":
            return self._inner.scheduled_events(instrument_id, start_session, end_session)
        return scheduled(self._inner.scheduled_events(instrument_id, start_session, end_session))

    def session_times(self, session):
        if self._flavour == "MDB":
            return self._inner.session_times(session)
        o, c = _calendar_error(CAL.session_times, session)
        return iso_z(o), iso_z(c)

    def sessions(self, start_session, end_session) -> list:
        if self._flavour == "MDB":
            return self._inner.sessions(start_session, end_session)
        return [s for s in CAL.SESSIONS if start_session <= s <= end_session]       # DIFF-3: the coverage calendar, no dataset clamp

    def next_session(self, session) -> str:
        if self._flavour == "MDB":
            return self._inner.next_session(session)
        return _calendar_error(CAL.next_session, session)


def _issue_view(inner, admission_sha256, flavour, universe_ids=None) -> AdmittedView:
    v = AdmittedView(_VIEW_TOKEN, inner, admission_sha256, flavour, universe_ids)
    _register(VIEW_REG, v)
    return v


def issued_view(x) -> bool:
    return type(x) is AdmittedView and _issued(VIEW_REG, x)


def require_admitted_view(v) -> None:
    """E7: a signal source accepts only a view this gate issued (exact type first, then the identity registry)."""
    if not issued_view(v):
        raise NotAdmitted(NOT_ADMITTED_VIEW)
