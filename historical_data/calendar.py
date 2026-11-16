"""
historical_data/calendar.py - the AUTHORITATIVE session calendar of contract section 2: session_times(session) -> (open_utc, close_utc).

* Coverage 2016-01-01..2026-12-31. The trading days, the full closures and the early-close dates come from research_data/nyse_calendar.py
  (read only, unchanged). A date outside the coverage raises CalendarNotCovered (CALENDAR_NOT_COVERED): the dataset fails closed.
* Regular sessions are 09:30-16:00 America/New_York; an early close ends at 13:00. Conversion to UTC uses zoneinfo with the tzdata package
  (pinned in requirements.txt). Without the tzdata package the times are refused (TzdataUnavailable; validation reports TZDATA_UNAVAILABLE).
* crosscheck_market_intel() compares every date of 2025-2026 (the overlap) with market_intel/calendar.py: the session days and the regular
  close. Any disagreement is a BLOCK (CALENDAR_CROSSCHECK_DISAGREEMENT) in the acceptance record. market_intel is imported only there.
* "Next session" is the next calendar session (contract section 2, A1), whether or not an instrument traded on it.
"""
from __future__ import annotations

import bisect
import functools
import importlib.metadata
import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

from research_data import nyse_calendar as NYSE

COVERAGE = ("2016-01-01", "2026-12-31")
CROSSCHECK_RANGE = ("2025-01-01", "2026-12-31")
CALENDAR_VERSION = "historical_data_calendar_v1 (research_data.nyse_calendar 2016-2026, 09:30-16:00 / 13:00 America/New_York)"
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)
ZONE = "America/New_York"
UTC = timezone.utc

SESSIONS: Tuple[str, ...] = tuple(NYSE.sessions(*COVERAGE))
_INDEX = {s: i for i, s in enumerate(SESSIONS)}
EARLY_CLOSES = frozenset(NYSE.EARLY_CLOSES)


class CalendarNotCovered(ValueError):
    reason = "CALENDAR_NOT_COVERED"


class NotASession(ValueError):
    reason = "NON_CALENDAR_SESSION"


class TzdataUnavailable(RuntimeError):
    reason = "TZDATA_UNAVAILABLE"


def _find_tzdata():
    return importlib.util.find_spec("tzdata")


@functools.lru_cache(maxsize=None)
def tzdata_status() -> Tuple[bool, Optional[str], str]:
    """(available, tzdata version, detail). The tzdata PACKAGE is required (contract section 2), not only a system time-zone database."""
    if _find_tzdata() is None:
        return False, None, "the tzdata package is not installed"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(ZONE)
    except Exception as exc:                                               # noqa: BLE001 - any failure to load the zone is an absence
        return False, None, f"{ZONE} does not load: {type(exc).__name__}"
    try:
        version = importlib.metadata.version("tzdata")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return True, version, "ok"


@functools.lru_cache(maxsize=None)
def _zone():
    if not tzdata_status()[0]:
        raise TzdataUnavailable(tzdata_status()[2])
    from zoneinfo import ZoneInfo
    return ZoneInfo(ZONE)


def reset_caches() -> None:
    """Tests only: forget the cached tzdata answer, zone, times and cross-check."""
    for f in (tzdata_status, _zone, _times, crosscheck_cached):
        f.cache_clear()


def _parse(s) -> date:
    if not isinstance(s, str) or len(s) != 10:
        raise ValueError(f"a session is a YYYY-MM-DD string, not {s!r}")
    return date.fromisoformat(s)


def covered(s: str) -> bool:
    _parse(s)
    return COVERAGE[0] <= s <= COVERAGE[1]


def check_session(s: str) -> str:
    """`s` if it is a calendar session; CalendarNotCovered outside the coverage; NotASession for a weekend or a full-day closure."""
    if not covered(s):
        raise CalendarNotCovered(f"{s} is outside the calendar coverage {COVERAGE[0]}..{COVERAGE[1]}")
    if s not in _INDEX:
        raise NotASession(f"{s} is not an NYSE session (a weekend or a full-day closure)")
    return s


def is_session(s: str) -> bool:
    """True / False inside the coverage; CalendarNotCovered outside it (never a guess)."""
    try:
        check_session(s)
    except NotASession:
        return False
    return True


@functools.lru_cache(maxsize=None)
def _times(s: str) -> Tuple[datetime, datetime]:
    check_session(s)
    ny = _zone()
    d = date.fromisoformat(s)
    o = datetime.combine(d, OPEN, tzinfo=ny).astimezone(UTC)
    c = datetime.combine(d, EARLY_CLOSE if s in EARLY_CLOSES else CLOSE, tzinfo=ny).astimezone(UTC)
    return o, c


def session_times(s: str) -> Tuple[datetime, datetime]:
    """(open_utc, close_utc) of a calendar session."""
    return _times(s)


def open_utc(s: str) -> datetime:
    return _times(s)[0]


def close_utc(s: str) -> datetime:
    return _times(s)[1]


def sessions(start_session: str, end_session: str) -> List[str]:
    """Every calendar session in [start_session, end_session], ascending (A1). Both bounds must lie inside the coverage."""
    if not covered(start_session) or not covered(end_session):
        raise CalendarNotCovered(f"{start_session}..{end_session} is not inside the calendar coverage {COVERAGE[0]}..{COVERAGE[1]}")
    if start_session > end_session:
        raise ValueError("start_session must not be after end_session")
    lo, hi = bisect.bisect_left(SESSIONS, start_session), bisect.bisect_right(SESSIONS, end_session)
    return list(SESSIONS[lo:hi])


def next_session(s: str) -> str:
    """The next calendar session (A1); CALENDAR_NOT_COVERED past 2026-12-31."""
    check_session(s)
    i = _INDEX[s] + 1
    if i >= len(SESSIONS):
        raise CalendarNotCovered(f"the session after {s} is outside the calendar coverage")
    return SESSIONS[i]


def previous_session(s: str) -> str:
    check_session(s)
    i = _INDEX[s] - 1
    if i < 0:
        raise CalendarNotCovered(f"the session before {s} is outside the calendar coverage")
    return SESSIONS[i]


def session_index(s: str) -> int:
    return _INDEX[check_session(s)]


def last_session_closed_by(t: datetime) -> Optional[str]:
    """The latest calendar session whose close is at or before `t` (None before the first one)."""
    if t.tzinfo is None:
        raise TypeError("a decision time must be timezone-aware (UTC)")
    lo, hi = 0, len(SESSIONS)
    while lo < hi:                                                           # close_utc is increasing in the session order
        mid = (lo + hi) // 2
        if close_utc(SESSIONS[mid]) <= t:
            lo = mid + 1
        else:
            hi = mid
    return SESSIONS[lo - 1] if lo else None


def session_of_date(d: date) -> Optional[str]:
    """The latest calendar session at or before date `d` (None before the coverage; the last session after it)."""
    s = d.isoformat()
    i = bisect.bisect_right(SESSIONS, s)
    return SESSIONS[i - 1] if i else None


def crosscheck_market_intel(mic=None) -> dict:
    """Every date of 2025-2026 against market_intel/calendar.py (read only): the same session days and the same regular close.
    {status: AGREE | DISAGREE, checked, disagreements[:20], disagreement_count}. `mic` is injectable for tests only."""
    if mic is None:
        from market_intel import calendar as mic                           # the one declared market_intel read (section 2 cross-check)
    out, n = [], 0
    d, end = date.fromisoformat(CROSSCHECK_RANGE[0]), date.fromisoformat(CROSSCHECK_RANGE[1])
    while d <= end:
        s = d.isoformat()
        n += 1
        mine = s in _INDEX
        status = mic.market_day_status(d)
        if status == mic.NOT_COVERED:
            out.append({"date": s, "problem": "MARKET_INTEL_NOT_COVERED"})
        elif mine != (status == mic.MARKET_DAY):
            out.append({"date": s, "problem": "SESSION_DAY", "historical_data": mine, "market_intel": status})
        elif mine:
            my_minutes = (13 * 60 if s in EARLY_CLOSES else 16 * 60) - (9 * 60 + 30)
            theirs = mic.regular_minutes(d)
            if theirs != my_minutes:
                out.append({"date": s, "problem": "REGULAR_MINUTES", "historical_data": my_minutes, "market_intel": theirs})
        d += timedelta(days=1)
    return {"status": "DISAGREE" if out else "AGREE", "range": list(CROSSCHECK_RANGE), "checked": n, "disagreement_count": len(out),
            "disagreements": out[:20]}


@functools.lru_cache(maxsize=None)
def crosscheck_cached() -> str:
    """The real cross-check's result, cached per process (as canonical JSON text, so the cache never hands out a mutable dict)."""
    import json
    return json.dumps(crosscheck_market_intel(), sort_keys=True)
