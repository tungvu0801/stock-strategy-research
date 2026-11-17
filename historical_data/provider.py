"""
historical_data/provider.py - the provider interface of contract section 4 (+ v2 A1, A2, A6, A11) and the shared in-memory table provider.

HistoricalDataProvider (abstract): manifest, instruments, bars, gaps, events, membership, scheduled_events, session_times, sessions,
next_session, as_of, execution_open. Full-history access (bars, events) is for ledger bookings and reporting only, never for decisions.

as_of(t) returns a PointInTimeView: decision logic sees only what was knowable at t (P1).
  * a bar when known_at <= t (known_at = close_utc(session)); an event when its known_at <= t, or - known_at UNKNOWN - from the OPEN of its
    effective_session; a POINT_IN_TIME membership change when its known_at <= t; a scheduled event when its known_at <= t;
  * an instrument once its first bar is visible; its symbol history / listing clipped to the last session closed by t;
  * gaps between the instrument's first and last VISIBLE bar;
  * split_factor and total_return_factor REBASED (A6): divided by the factor of the instrument's last visible bar (that bar reads exactly
    1.0), so an absolute factor level - which encodes later events - never reaches decision logic; if that last factor is UNAVAILABLE,
    the view's factors for the instrument are UNAVAILABLE;
  * the manifest as the A11 allow-list only.
The view holds plain immutable copies and no reference to the provider; it has no execution channel.

execution_open(instrument_id, session, *, decision_session) (A2) is the execution channel, on the provider only: it raises unless
session == next_session(decision_session), and returns the session's raw_open or NO_BAR (a missing or removed-invalid bar -> NO_FILL_GAP).
A2 interpretation (fail closed, I-3): NO_BAR means "no bar at that session"; a bar that exists but whose raw_open is UNAVAILABLE, and a
dataset without the raw open / high / low view at all, raise IntegrityOnly instead - a fill price is never invented and an integrity-only
dataset is never mistaken for a market without a bar. Duplicate bars at the session raise (the dataset is BLOCKED by DUPLICATE_KEY).

Licence (section 6.2, A10): for kind HISTORICAL, as_of and execution_open raise LicenceRefused unless personal_research_use is CONFIRMED
(adapters always emit UNKNOWN). kind SYNTHETIC is exempt. Without raw open / high / low the dataset is integrity-only (P3, section 4.1):
as_of and execution_open raise IntegrityOnly.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from historical_data import SYNTHETIC
from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data.schema import (CorporateEvent, DailyBar, DatasetManifest, Gap, Instrument, ListingSpan, MembershipChange, MembershipSet,
                                    ScheduledEvent, SymbolSpan, utc_ts)
from historical_data.status import EARNINGS_DATA_UNAVAILABLE, NO_BAR, Missing, is_missing, unavailable


class LicenceRefused(PermissionError):
    """Section 6.2: no returns, signals, ledger runs or statistics on a HISTORICAL dataset whose licence is not CONFIRMED."""


class ExecutionTimingError(ValueError):
    """A2: execution_open only for the calendar session immediately after the decision session."""


class IntegrityOnly(RuntimeError):
    """P3 / section 4.1: without raw open / high / low the dataset serves integrity checks only."""


class UnknownInstrument(KeyError):
    pass


# ------------------------------------------------------------------------------------------------ the abstract interface
class HistoricalDataProvider(ABC):
    @abstractmethod
    def manifest(self) -> DatasetManifest: ...

    @abstractmethod
    def instruments(self) -> List[Instrument]: ...

    @abstractmethod
    def bars(self, instrument_id: str, start_session: Optional[str] = None, end_session: Optional[str] = None) -> List[DailyBar]: ...

    @abstractmethod
    def gaps(self, instrument_id: str) -> List[Gap]: ...

    @abstractmethod
    def events(self, instrument_id: Optional[str] = None, start_session: Optional[str] = None, end_session: Optional[str] = None) -> List[CorporateEvent]: ...

    @abstractmethod
    def membership(self, universe_id: str, session: str) -> MembershipSet: ...

    @abstractmethod
    def scheduled_events(self, instrument_id: str, start_session: Optional[str] = None, end_session: Optional[str] = None): ...

    @abstractmethod
    def as_of(self, decision_time_utc: datetime) -> "PointInTimeView": ...

    @abstractmethod
    def execution_open(self, instrument_id: str, session: str, *, decision_session: str): ...

    def session_times(self, session: str):
        return CAL.session_times(session)

    def sessions(self, start_session: str, end_session: str) -> List[str]:
        return CAL.sessions(start_session, end_session)

    def next_session(self, session: str) -> str:
        return CAL.next_session(session)


# ------------------------------------------------------------------------------------------------ helpers shared by the provider and the view
def _in_range(s: str, start: Optional[str], end: Optional[str]) -> bool:
    return (start is None or s >= start) and (end is None or s <= end)


def _closed_by(session: str, t: datetime) -> bool:
    try:
        return CAL.close_utc(session) <= t
    except (CAL.CalendarNotCovered, CAL.NotASession, ValueError):
        return False                                                          # a session the calendar refuses is never visible


def _event_visible_at(e: CorporateEvent, t: datetime) -> bool:
    if not is_missing(e.known_at):
        return e.known_at <= t
    try:
        return CAL.open_utc(e.effective_session) <= t                         # known_at UNKNOWN: from the effective session's open (3.3)
    except (CAL.CalendarNotCovered, CAL.NotASession, ValueError):
        return False


def _membership_set(changes: Iterable[MembershipChange], universe_id: str, session: str, first_bar: Dict[str, str], delisted: Dict[str, str],
                    basis: str) -> MembershipSet:
    state: Dict[str, Tuple[str, object]] = {}
    for c in sorted((c for c in changes if c.universe_id == universe_id and c.session <= session), key=lambda c: (c.session, c.instrument_id, c.action)):
        state[c.instrument_id] = (c.action, c.known_at)
    ids = tuple(sorted(i for i, (a, _k) in state.items() if a == "ADD" and i in first_bar and first_bar[i] <= session
                       and not (i in delisted and delisted[i] <= session)))
    flags = ("SURVIVORSHIP_BIAS_PRESENT",) if basis == "FIXED_CURRENT_UNIVERSE" else ()
    known = tuple((i, state[i][1]) for i in ids) if basis == "POINT_IN_TIME" else ()
    return MembershipSet(universe_id=universe_id, session=session, membership_basis=basis, instrument_ids=ids, flags=flags, change_known_at=known)


def _delisted(events: Iterable[CorporateEvent]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for e in events:
        if e.type == "DELISTING":
            out[e.instrument_id] = min(out.get(e.instrument_id, e.effective_session), e.effective_session)
    return out


# ------------------------------------------------------------------------------------------------ the shared table provider
class _TableProvider(HistoricalDataProvider):
    """Every v1 adapter loads its rows into this. Rows are kept as given (duplicates included) so validation can see them."""

    def __init__(self, *, manifest_fields: dict, instruments: Iterable[Instrument], bars: Iterable[DailyBar], events: Iterable[CorporateEvent] = (),
                 membership: Iterable[MembershipChange] = (), scheduled_events=EARNINGS_DATA_UNAVAILABLE, removed: Iterable[Tuple[str, str]] = (),
                 notes: Iterable[dict] = (), dropped_symbols: Iterable[str] = ()):
        self._dropped = tuple(sorted(set(dropped_symbols)))
        self._instruments: Tuple[Instrument, ...] = tuple(sorted(instruments, key=lambda i: i.instrument_id))
        ids = [i.instrument_id for i in self._instruments]
        if len(ids) != len(set(ids)):
            raise ValueError("an instrument_id appears twice")
        self._inst = {i.instrument_id: i for i in self._instruments}
        self._bars: Tuple[DailyBar, ...] = tuple(bars)
        orphans = sorted({b.instrument_id for b in self._bars} - set(self._inst))
        if orphans:
            raise ValueError(f"bars for unknown instruments: {orphans[:5]}")
        self._events: Tuple[CorporateEvent, ...] = tuple(events)
        if any(e.instrument_id not in self._inst for e in self._events):
            raise ValueError("an event names an unknown instrument")
        self._changes: Tuple[MembershipChange, ...] = tuple(membership)
        if scheduled_events is not EARNINGS_DATA_UNAVAILABLE and not is_missing(scheduled_events):
            scheduled_events = tuple(scheduled_events)
        self._scheduled = scheduled_events
        self._removed = frozenset(removed)
        self._notes = tuple(json.loads(json.dumps(n, sort_keys=True)) for n in notes)
        self._by_inst: Dict[str, List[DailyBar]] = {i: [] for i in self._inst}
        for b in sorted(self._bars, key=lambda b: b.session):
            self._by_inst[b.instrument_id].append(b)
        self._at: Dict[Tuple[str, str], List[DailyBar]] = {}
        for b in self._bars:
            self._at.setdefault((b.instrument_id, b.session), []).append(b)
        self._gaps: Tuple[Gap, ...] = self._compute_gaps()
        sessions = sorted({b.session for b in self._bars})
        m = DatasetManifest(**manifest_fields, start_session=sessions[0] if sessions else unavailable("NO_BARS"),
                            end_session=sessions[-1] if sessions else unavailable("NO_BARS"), instrument_count=len(self._instruments))
        self._manifest = m
        self._manifest = replace(m, content_sha256=CN.content_sha256(self.canonical_document()))

    # -------------------------------------------------------------------------------- gaps (3.2)
    def _compute_gaps(self) -> Tuple[Gap, ...]:
        out = []
        for iid, rows in self._by_inst.items():
            have = set()
            for b in rows:
                try:
                    if CAL.is_session(b.session):
                        have.add(b.session)
                except (CAL.CalendarNotCovered, ValueError):
                    continue                                                  # validation reports it; no gap is derived from it
            if not have:
                continue
            for s in CAL.sessions(min(have), max(have)):
                if s not in have:
                    out.append(Gap(iid, s, "REMOVED_INVALID" if (iid, s) in self._removed else "NO_BAR"))
        return tuple(out)

    # -------------------------------------------------------------------------------- the canonical form (6.3)
    def canonical_document(self) -> dict:
        return CN.document(self._manifest, self._instruments, self._bars, self._gaps, self._events, self._changes, self._scheduled)

    def adapter_notes(self) -> Tuple[dict, ...]:
        """What the adapter recorded about its sources (skipped error envelopes, removed rows); data, never a verdict."""
        return tuple(json.loads(json.dumps(n)) for n in self._notes)

    def removed_rows(self) -> List[Tuple[str, str]]:
        return sorted(self._removed)

    def dropped_symbols(self) -> List[str]:
        """Symbols present in the source that yield no instrument (only error envelopes, or no valid bar): validation warns (L-5)."""
        return list(self._dropped)

    def all_scheduled_events(self):
        """Every scheduled event (or EARNINGS_DATA_UNAVAILABLE), for validation and reporting only."""
        return self._scheduled if is_missing(self._scheduled) else list(self._scheduled)

    # -------------------------------------------------------------------------------- section 4: full history
    def manifest(self) -> DatasetManifest:
        return self._manifest

    def instruments(self) -> List[Instrument]:
        return list(self._instruments)

    def bars(self, instrument_id, start_session=None, end_session=None) -> List[DailyBar]:
        if instrument_id not in self._inst:
            raise UnknownInstrument(instrument_id)
        return [b for b in self._by_inst[instrument_id] if _in_range(b.session, start_session, end_session)]

    def gaps(self, instrument_id) -> List[Gap]:
        if instrument_id not in self._inst:
            raise UnknownInstrument(instrument_id)
        return [g for g in self._gaps if g.instrument_id == instrument_id]

    def events(self, instrument_id=None, start_session=None, end_session=None) -> List[CorporateEvent]:
        return sorted((e for e in self._events if (instrument_id is None or e.instrument_id == instrument_id)
                       and _in_range(e.effective_session, start_session, end_session)), key=lambda e: (e.effective_session, e.event_id))

    def membership(self, universe_id, session) -> MembershipSet:
        first = {i: rows[0].session for i, rows in self._by_inst.items() if rows}
        return _membership_set(self._changes, universe_id, session, first, _delisted(self._events), self._manifest.membership_basis)

    def membership_changes(self) -> List[MembershipChange]:
        return list(self._changes)

    def scheduled_events(self, instrument_id, start_session=None, end_session=None):
        if is_missing(self._scheduled):
            return self._scheduled
        return sorted((s for s in self._scheduled if s.instrument_id == instrument_id and _in_range(s.event_session, start_session, end_session)),
                      key=lambda s: (s.event_session, s.event_id))

    # -------------------------------------------------------------------------------- the gates
    def _require_licence(self) -> None:
        m = self._manifest
        if m.kind == SYNTHETIC:
            return                                                              # section 6.2: SYNTHETIC is exempt
        if m.licence.personal_research_use != "CONFIRMED":
            raise LicenceRefused(f"{m.dataset_id}: kind HISTORICAL with personal_research_use {m.licence.personal_research_use} - only schema, "
                                 "integrity and validation checks are allowed (section 6.2)")

    def _require_raw_ohl(self) -> None:
        if "RAW_OHL" not in self._manifest.price_views_available:
            raise IntegrityOnly(f"{self._manifest.dataset_id}: no raw open / high / low - integrity checks only (P3, section 4.1)")

    # -------------------------------------------------------------------------------- the decision view (P1, A6, A11)
    def as_of(self, decision_time_utc: datetime) -> "PointInTimeView":
        t = utc_ts(decision_time_utc, "decision_time_utc")
        self._require_licence()
        self._require_raw_ohl()
        tsess = CAL.last_session_closed_by(t)
        vis: Dict[str, Tuple[DailyBar, ...]] = {}
        for iid, rows in self._by_inst.items():
            seen = [b for b in rows if b.known_at <= t and _closed_by(b.session, t)]
            if seen:
                vis[iid] = _rebased(seen)
        insts = tuple(_clipped(self._inst[i], tsess) for i in sorted(vis))
        last = {i: rows[-1].session for i, rows in vis.items()}
        gaps = tuple(g for g in self._gaps if g.instrument_id in vis and g.session <= last[g.instrument_id])
        events = tuple(sorted((e for e in self._events if e.instrument_id in vis and _event_visible_at(e, t)),
                              key=lambda e: (e.effective_session, e.event_id)))
        basis = self._manifest.membership_basis
        changes = tuple(c for c in self._changes if c.instrument_id in vis and (
            (not is_missing(c.known_at) and c.known_at <= t) if basis == "POINT_IN_TIME" else True))
        if is_missing(self._scheduled):
            sched = self._scheduled
        else:
            sched = tuple(s for s in self._scheduled if s.instrument_id in vis and s.known_at <= t)
        return PointInTimeView(t=t, last_session=tsess, manifest_json=json.dumps(self._manifest.view_json(), sort_keys=True), instruments=insts,
                               bars=vis, gaps=gaps, events=events, changes=changes, scheduled=sched, basis=basis)

    # -------------------------------------------------------------------------------- the execution channel (A2)
    def execution_open(self, instrument_id, session, *, decision_session):
        self._require_licence()
        self._require_raw_ohl()
        if instrument_id not in self._inst:
            raise UnknownInstrument(instrument_id)
        nxt = CAL.next_session(decision_session)                               # NotASession / CalendarNotCovered propagate
        if session != nxt:
            raise ExecutionTimingError(f"execution_open({instrument_id}, {session}): only the session after the decision session {decision_session} "
                                       f"({nxt}) can be filled - never the signal bar, never later (P2, A2)")
        rows = self._at.get((instrument_id, session), [])
        if not rows:
            return NO_BAR
        if len(rows) > 1:
            raise ValueError(f"{instrument_id} {session}: duplicate bars (DUPLICATE_KEY) - the dataset is BLOCKED")
        if is_missing(rows[0].raw_open):
            raise IntegrityOnly(f"{instrument_id} {session}: raw_open UNAVAILABLE")
        return rows[0].raw_open


def _rebased(rows: List[DailyBar]) -> Tuple[DailyBar, ...]:
    """A6: each factor divided by the last visible bar's factor (that bar reads exactly 1.0); UNAVAILABLE when the last one is."""
    anchor = rows[-1]
    out = []
    for b in rows:
        upd = {}
        for f in ("split_factor", "total_return_factor"):
            a, v = getattr(anchor, f), getattr(b, f)
            if is_missing(a):
                upd[f] = unavailable("LAST_VISIBLE_FACTOR_UNAVAILABLE")
            elif is_missing(v):
                upd[f] = v
            else:
                upd[f] = v / a
        out.append(replace(b, **upd))
    return tuple(out)


def _clipped(inst: Instrument, tsess: Optional[str]) -> Instrument:
    """The instrument as known at the last closed session: spans starting later are dropped, ends after it read as open (None)."""
    def clip_to(x):
        return x if (x is not None and tsess is not None and x <= tsess) else None
    spans = tuple(SymbolSpan(sp.symbol, sp.valid_from_session, clip_to(sp.valid_to_session)) for sp in inst.symbol_history
                  if tsess is not None and sp.valid_from_session <= tsess)
    if not spans:
        first = inst.symbol_history[0]
        spans = (SymbolSpan(first.symbol, first.valid_from_session, None),)
    listing = inst.listing
    if not is_missing(listing):
        listing = tuple(ListingSpan(x.exchange, x.from_session, clip_to(x.to_session)) for x in listing if tsess is not None and x.from_session <= tsess)
    return replace(inst, symbol_history=spans if inst.symbol_history_status == "KNOWN" else spans[:1], listing=listing)


# ------------------------------------------------------------------------------------------------ the point-in-time view
class PointInTimeView:
    """What decision logic may see at `decision_time` (P1). Plain immutable data; no provider reference; no execution channel (A2)."""
    __slots__ = ("_t", "_last_session", "_manifest_json", "_instruments", "_bars", "_gaps", "_events", "_changes", "_scheduled", "_basis")

    def __init__(self, *, t, last_session, manifest_json, instruments, bars, gaps, events, changes, scheduled, basis):
        self._t, self._last_session, self._manifest_json = t, last_session, manifest_json
        self._instruments, self._bars, self._gaps, self._events = instruments, dict(bars), gaps, events
        self._changes, self._scheduled, self._basis = changes, scheduled, basis

    @property
    def decision_time(self) -> datetime:
        return self._t

    @property
    def last_closed_session(self) -> Optional[str]:
        return self._last_session

    def manifest(self) -> dict:
        return json.loads(self._manifest_json)

    def instruments(self) -> List[Instrument]:
        return list(self._instruments)

    def bars(self, instrument_id, start_session=None, end_session=None) -> List[DailyBar]:
        return [b for b in self._bars.get(instrument_id, ()) if _in_range(b.session, start_session, end_session)]

    def gaps(self, instrument_id) -> List[Gap]:
        return [g for g in self._gaps if g.instrument_id == instrument_id]

    def events(self, instrument_id=None, start_session=None, end_session=None) -> List[CorporateEvent]:
        return [e for e in self._events if (instrument_id is None or e.instrument_id == instrument_id) and _in_range(e.effective_session, start_session, end_session)]

    def membership(self, universe_id, session) -> MembershipSet:
        first = {i: rows[0].session for i, rows in self._bars.items()}
        return _membership_set(self._changes, universe_id, session, first, _delisted(self._events), self._basis)

    def scheduled_events(self, instrument_id, start_session=None, end_session=None):
        if is_missing(self._scheduled):
            return self._scheduled
        return sorted((s for s in self._scheduled if s.instrument_id == instrument_id and _in_range(s.event_session, start_session, end_session)),
                      key=lambda s: (s.event_session, s.event_id))

    def session_times(self, session):
        return CAL.session_times(session)

    def sessions(self, start_session, end_session):
        return CAL.sessions(start_session, end_session)

    def next_session(self, session):
        return CAL.next_session(session)
