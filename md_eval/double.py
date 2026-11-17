"""md_eval/double.py - ContractDouble: a minimal, in-memory, SYNTHETIC implementation of exactly the contract v2 provider interface (base §4
plus A1, A2, A6, A11), returning contract-shaped records. It is MD-B's own test double: it imports nothing from MD-A's package (integration
on MD-A's real adapters is a later, separately reviewed step).

Synthetic calendar times: open 14:30Z, close 21:00Z for every session (no time zone data is needed: the real calendar is MD-A's).
The as_of view is built by COPYING the filtered records: it holds no reference to the provider and has no execution_open (A2).

Test knobs (each returns a NEW double; the original is unchanged): perturb_after(t) (every value with known_at > t is changed),
truncate(session) (everything knowable after the session's close removed), scale_factors(instrument_id, c), and two deliberate defects used
only as negative controls: leak_known_at (the view ignores known_at) and view_holds_provider (the view keeps a provider reference)."""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from types import MappingProxyType
from typing import Dict, Iterable, List, Optional

from md_eval.contract import (EARNINGS_DATA_UNAVAILABLE, NO_BAR, VIEW_MANIFEST_FIELDS, CalendarNotCovered, ContractError, CorporateEvent,
                              DailyBar, DatasetManifest, ExecutionChannelViolation, Gap, Instrument, MembershipSet, is_missing, unavailable)

OPEN_TIME, CLOSE_TIME = "T14:30:00Z", "T21:00:00Z"


def weekday_sessions(start: str, n: int) -> List[str]:
    """n consecutive weekdays from `start` (inclusive when it is a weekday): a synthetic calendar."""
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class ContractDouble:
    def __init__(self, *, sessions: Iterable[str], instruments: Iterable[Instrument], bars: Iterable[DailyBar], manifest: DatasetManifest,
                 events: Iterable[CorporateEvent] = (), membership: Optional[dict] = None, scheduled=EARNINGS_DATA_UNAVAILABLE,
                 gaps: Iterable[Gap] = (), leak_known_at: bool = False, view_holds_provider: bool = False):
        self._sessions = tuple(sorted(set(sessions)))
        self._pos = {s: i for i, s in enumerate(self._sessions)}
        self._instruments = {i.instrument_id: i for i in instruments}
        per: Dict[str, List[DailyBar]] = {}
        for b in bars:
            if b.session not in self._pos:
                raise ContractError(f"{b.instrument_id} {b.session}: not a calendar session")
            if not b.known_at:
                b = dataclasses.replace(b, known_at=self.session_times(b.session)[1])
            per.setdefault(b.instrument_id, []).append(b)
        self._bars = {k: tuple(sorted(v, key=lambda x: x.session)) for k, v in per.items()}
        self._events = tuple(sorted(events, key=lambda e: (e.effective_session, e.event_id)))
        self._membership = dict(membership or {})            # universe_id -> {"basis": ..., "members": (...)} or {"basis": POINT_IN_TIME, "changes": (...)}
        self._scheduled = scheduled
        self._gaps = tuple(gaps)
        self._manifest = manifest
        self.leak_known_at = leak_known_at
        self.view_holds_provider = view_holds_provider
        self.calls: List[str] = []
        self.exec_calls: List[tuple] = []

    def _clone(self, **over) -> "ContractDouble":
        kw = dict(sessions=self._sessions, instruments=tuple(self._instruments.values()), bars=[b for v in self._bars.values() for b in v],
                  manifest=self._manifest, events=self._events, membership=self._membership, scheduled=self._scheduled, gaps=self._gaps,
                  leak_known_at=self.leak_known_at, view_holds_provider=self.view_holds_provider)
        kw.update(over)
        return ContractDouble(**kw)

    # ---- the calendar (§2, A1) -------------------------------------------------------------------------------------------------------
    def session_times(self, session: str):
        if session not in self._pos:
            raise CalendarNotCovered(f"{session}: CALENDAR_NOT_COVERED (not a session of the synthetic calendar)")
        return session + OPEN_TIME, session + CLOSE_TIME

    def sessions(self, start_session: str, end_session: str) -> List[str]:
        return [s for s in self._sessions if start_session <= s <= end_session]

    def next_session(self, session: str) -> str:
        i = self._pos.get(session)
        if i is None or i + 1 >= len(self._sessions):
            raise CalendarNotCovered(f"{session}: CALENDAR_NOT_COVERED (no next session)")
        return self._sessions[i + 1]

    # ---- full history (bookings, valuation, reporting only) -----------------------------------------------------------------------------
    def manifest(self) -> DatasetManifest:
        self.calls.append("manifest")
        return self._manifest

    def instruments(self) -> list:
        self.calls.append("instruments")
        return list(self._instruments.values())

    def bars(self, instrument_id: str, start_session: str, end_session: str) -> list:
        self.calls.append("bars")
        return [b for b in self._bars.get(instrument_id, ()) if start_session <= b.session <= end_session]

    def gaps(self, instrument_id: str) -> list:
        self.calls.append("gaps")
        return _gaps(self._bars.get(instrument_id, ()), self._sessions, self._gaps, instrument_id)

    def events(self, instrument_id=None, start_session=None, end_session=None) -> list:
        self.calls.append("events")
        return [e for e in self._events if (instrument_id is None or e.instrument_id == instrument_id)
                and (start_session is None or e.effective_session >= start_session) and (end_session is None or e.effective_session <= end_session)]

    def membership(self, universe_id: str, session: str) -> MembershipSet:
        self.calls.append("membership")
        return _membership(self._membership, universe_id, session, self._bars, self._events, None)

    def scheduled_events(self, instrument_id: str, start_session: str, end_session: str):
        self.calls.append("scheduled_events")
        if self._scheduled is EARNINGS_DATA_UNAVAILABLE:
            return EARNINGS_DATA_UNAVAILABLE
        return [x for x in self._scheduled if x.instrument_id == instrument_id and start_session <= x.event_session <= end_session]

    # ---- the execution channel (§4, A2) ---------------------------------------------------------------------------------------------------
    def execution_open(self, instrument_id: str, session: str, *, decision_session: str):
        self.calls.append("execution_open")
        self.exec_calls.append((instrument_id, session, decision_session))
        if session != self.next_session(decision_session):
            raise ExecutionChannelViolation(f"execution_open({instrument_id}, {session}) for decision session {decision_session}: only the next "
                                            "calendar session may fill")
        for b in self._bars.get(instrument_id, ()):
            if b.session == session:
                return b.raw_open if _num(b.raw_open) else NO_BAR
        return NO_BAR

    # ---- decisions -----------------------------------------------------------------------------------------------------------------------
    def as_of(self, decision_time_utc: str) -> "PointInTimeViewDouble":
        self.calls.append("as_of")
        return PointInTimeViewDouble(self, decision_time_utc)

    # ---- test knobs -------------------------------------------------------------------------------------------------------------------------
    def perturb_after(self, t_utc: str, price=1.37, volume=2.5, factor=1.11, cash=3.0) -> "ContractDouble":
        """Every value with known_at > t changes: bars (prices, volumes, both factors), events (amounts, ratios, returns), membership changes
        (dropped), scheduled events (moved). Values knowable at or before t are untouched."""
        bars = []
        for v in self._bars.values():
            for b in v:
                if b.known_at > t_utc:
                    f = lambda x, m: x * m if _num(x) else x  # noqa: E731
                    b = dataclasses.replace(b, raw_open=f(b.raw_open, price), raw_high=f(b.raw_high, price), raw_low=f(b.raw_low, price),
                                            raw_close=f(b.raw_close, price), raw_volume=f(b.raw_volume, volume),
                                            split_factor=f(b.split_factor, factor), total_return_factor=f(b.total_return_factor, factor))
                bars.append(b)
        events = []
        for e in self._events:
            if _event_known_after(e, t_utc, self):
                e = dataclasses.replace(e, cash_amount=e.cash_amount * cash if _num(e.cash_amount) else e.cash_amount,
                                        ratio=e.ratio * 1.5 if _num(e.ratio) else e.ratio)
            events.append(e)
        mem = {}
        for u, spec in self._membership.items():
            spec = dict(spec)
            if "changes" in spec:
                spec["changes"] = tuple(c for c in spec["changes"] if not c[3] > t_utc)
            mem[u] = spec
        return self._clone(bars=bars, events=events, membership=mem)

    def truncate(self, session: str) -> "ContractDouble":
        """The dataset cut at the close of `session`: every value knowable after it removed; the manifest's end_session moves to it."""
        t = self.session_times(session)[1]
        bars = [b for v in self._bars.values() for b in v if b.known_at <= t]
        events = [e for e in self._events if not _event_known_after(e, t, self)]
        mem = {}
        for u, spec in self._membership.items():
            spec = dict(spec)
            if "changes" in spec:
                spec["changes"] = tuple(c for c in spec["changes"] if c[3] <= t)
            mem[u] = spec
        return self._clone(bars=bars, events=events, membership=mem, manifest=dataclasses.replace(self._manifest, end_session=session))

    def scale_factors(self, instrument_id: str, c: float) -> "ContractDouble":
        bars = [dataclasses.replace(b, split_factor=b.split_factor * c if _num(b.split_factor) else b.split_factor,
                                    total_return_factor=b.total_return_factor * c if _num(b.total_return_factor) else b.total_return_factor)
                if b.instrument_id == instrument_id else b for v in self._bars.values() for b in v]
        return self._clone(bars=bars)

    def with_defect(self, **defects) -> "ContractDouble":
        return self._clone(**defects)


def _event_visible_time(e: CorporateEvent, prov) -> str:
    """When decision logic may see the event: its known_at, or (known_at UNKNOWN) the open of effective_session (§3.3)."""
    return prov.session_times(e.effective_session)[0] if is_missing(e.known_at) else e.known_at


def _event_known_after(e: CorporateEvent, t_utc: str, prov) -> bool:
    return _event_visible_time(e, prov) > t_utc


def _gaps(bars, sessions, declared, instrument_id) -> list:
    if not bars:
        return []
    have = {b.session for b in bars}
    given = {g.session: g for g in declared if g.instrument_id == instrument_id}
    return [given.get(s, Gap(instrument_id, s, "NO_BAR")) for s in sessions if bars[0].session <= s <= bars[-1].session and s not in have]


def _membership(spec_by_u, universe_id, session, bars, events, t_utc) -> MembershipSet:
    spec = spec_by_u.get(universe_id)
    if spec is None:
        raise ContractError(f"unknown universe {universe_id!r}")
    delisted = {e.instrument_id for e in events if e.type == "DELISTING" and e.effective_session <= session}
    alive = lambda iid: bool(bars.get(iid)) and bars[iid][0].session <= session and iid not in delisted  # noqa: E731
    if spec["basis"] == "FIXED_CURRENT_UNIVERSE":
        return MembershipSet(universe_id, session, tuple(sorted(i for i in spec["members"] if alive(i))), "FIXED_CURRENT_UNIVERSE", None)
    members, known = set(), {}
    for iid, eff, action, k in sorted(spec["changes"], key=lambda c: (c[1], c[0])):
        if eff <= session and (t_utc is None or k <= t_utc):
            (members.add if action == "ADD" else members.discard)(iid)
            known[iid] = k
    return MembershipSet(universe_id, session, tuple(sorted(i for i in members if alive(i))), "POINT_IN_TIME", MappingProxyType(known))


def _rebase(bars: List[DailyBar]) -> List[DailyBar]:
    """A6: each factor divided by the factor on the LAST visible bar (so it is exactly 1.0); UNAVAILABLE there -> UNAVAILABLE throughout."""
    if not bars:
        return []
    last = bars[-1]
    if last.split_factor == 1.0 and last.total_return_factor == 1.0 and _num(last.split_factor) and _num(last.total_return_factor) and \
            all(_num(b.split_factor) and _num(b.total_return_factor) for b in bars):
        return list(bars)                                                      # x / 1.0 == x exactly: the rebased view is the same
    out = []
    for b in bars:
        sf = b.split_factor / last.split_factor if _num(b.split_factor) and _num(last.split_factor) else unavailable("LAST_VISIBLE_SPLIT_FACTOR_UNAVAILABLE"
                                                                                                                     if not _num(last.split_factor) else "SPLIT_FACTOR_UNAVAILABLE")
        tr = (b.total_return_factor / last.total_return_factor if _num(b.total_return_factor) and _num(last.total_return_factor)
              else unavailable("LAST_VISIBLE_TOTAL_RETURN_FACTOR_UNAVAILABLE" if not _num(last.total_return_factor) else "TOTAL_RETURN_FACTOR_UNAVAILABLE"))
        out.append(dataclasses.replace(b, split_factor=sf, total_return_factor=tr))
    return out


class PointInTimeViewDouble:
    """as_of(t): copies of the records knowable at t. No provider reference (unless the view_holds_provider control is on), no execution_open."""

    def __init__(self, prov: ContractDouble, t_utc: str):
        self.decision_time_utc = t_utc
        leak = prov.leak_known_at
        ok = (lambda k: True) if leak else (lambda k: k <= t_utc)  # noqa: E731
        m = prov._manifest
        self._manifest = MappingProxyType({k: getattr(m, k) for k in VIEW_MANIFEST_FIELDS})            # A11: the allow-list only
        self._instruments = tuple(prov._instruments.values())
        self._bars = {iid: tuple(_rebase([b for b in v if ok(b.known_at)])) for iid, v in prov._bars.items()}
        self._events = tuple(e for e in prov._events if ok(_event_visible_time(e, prov)))
        self._membership = {u: dict(s, changes=tuple(c for c in s["changes"] if ok(c[3]))) if "changes" in s else dict(s)
                            for u, s in prov._membership.items()}
        self._scheduled = prov._scheduled if prov._scheduled is EARNINGS_DATA_UNAVAILABLE else tuple(x for x in prov._scheduled if ok(x.known_at))
        self._sessions = prov._sessions
        self._pos = dict(prov._pos)
        self._declared_gaps = prov._gaps
        if prov.view_holds_provider:                                                                    # the NEGATIVE CONTROL only
            self._provider = prov

    def manifest(self) -> dict:
        return dict(self._manifest)

    def instruments(self) -> list:
        return list(self._instruments)

    def bars(self, instrument_id: str, start_session: str, end_session: str) -> list:
        return [b for b in self._bars.get(instrument_id, ()) if start_session <= b.session <= end_session]

    def gaps(self, instrument_id: str) -> list:
        return _gaps(self._bars.get(instrument_id, ()), self._sessions, self._declared_gaps, instrument_id)

    def events(self, instrument_id=None, start_session=None, end_session=None) -> list:
        return [e for e in self._events if (instrument_id is None or e.instrument_id == instrument_id)
                and (start_session is None or e.effective_session >= start_session) and (end_session is None or e.effective_session <= end_session)]

    def membership(self, universe_id: str, session: str) -> MembershipSet:
        return _membership(self._membership, universe_id, session, self._bars, self._events, None)

    def scheduled_events(self, instrument_id: str, start_session: str, end_session: str):
        if self._scheduled is EARNINGS_DATA_UNAVAILABLE:
            return EARNINGS_DATA_UNAVAILABLE
        return [x for x in self._scheduled if x.instrument_id == instrument_id and start_session <= x.event_session <= end_session]

    def session_times(self, session: str):
        if session not in self._pos:
            raise CalendarNotCovered(f"{session}: CALENDAR_NOT_COVERED")
        return session + OPEN_TIME, session + CLOSE_TIME

    def sessions(self, start_session: str, end_session: str) -> list:
        return [s for s in self._sessions if start_session <= s <= end_session]

    def next_session(self, session: str) -> str:
        i = self._pos.get(session)
        if i is None or i + 1 >= len(self._sessions):
            raise CalendarNotCovered(f"{session}: CALENDAR_NOT_COVERED (no next session)")
        return self._sessions[i + 1]
