"""
market_intel/calendar.py - New York sessions for the data layer: PRE / REG / POST / CLOSED.

One source of truth: the Stage 6.10A classifier (autorun/session.py) and its static, versioned NYSE table for 2026-2027
(autorun/calendar.py), both read only and pure. PRE is 04:00-09:30, REG 09:30 to the close (16:00, or 13:00 on an early-close day),
POST the close to 20:00, CLOSED otherwise, on weekends and on holidays. Daylight-saving changes are handled by zoneinfo.

A WEEKDAY outside the table is NOT_COVERED (a weekend is a weekend in every year). The 6.10A classifier would treat it as a normal
session; this layer never does: market_day_status() says NOT_COVERED, is_market_day() raises CalendarNotCovered, the walkers
(previous / next market day, last completed session) return None instead of guessing, validate.py flags CALENDAR_NOT_COVERED (a blocking
code) and freshness.py fails closed. So an uncovered historical year (a 2025 backfill) or a future one (2028) is never silently treated as
verified sessions.

Coverage strategy (DESIGN_70 section 5): years are added to a versioned table from the NYSE published schedule, each pinned by a test
that cross-checks the holiday rules and lists the exceptional closures rules cannot derive (e.g. 2025-01-09, the national day of
mourning). History (2025 and earlier) goes into a market_intel-owned table, never into autorun/calendar.py (frozen, used by the
executor's session health); a live year (2028) is a separate, declared change to autorun/calendar.py. `rule_holidays` only cross-checks.

Stage 7.1: 2025 is covered by the market_intel-owned, verified history table (calendar_history.py: the NYSE publications plus an
empirical cross-check). For a history year the session is classified here with the 6.10A classifier's own thresholds (PRE from 04:00,
REG from 09:30 to the close, POST to 20:00) and the history table's closes; 2026-2027 still come from the 6.10A classifier itself. Every
other year (2024 and earlier, 2028 and later) stays NOT_COVERED.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from autorun import AFTER_HOURS, AFTER_HOURS_END_MINUTE, CLOSED as A_CLOSED, PREMARKET, PREMARKET_MINUTE, REGULAR
from autorun import calendar as CAL
from autorun import session as SES
from market_intel import CLOSED, POST, PRE, REG
from market_intel import calendar_history as HIST
from setups.market_hours import OPEN_MINUTE, to_et

_MAP = {PREMARKET: PRE, REGULAR: REG, AFTER_HOURS: POST, A_CLOSED: CLOSED}
MARKET_DAY, HOLIDAY, WEEKEND, NOT_COVERED = "MARKET_DAY", "HOLIDAY", "WEEKEND", "NOT_COVERED"
WALK_LIMIT_DAYS = 15


class CalendarNotCovered(ValueError):
    """A weekday outside the static table: whether it was a session is unknown."""
CALENDAR_VERSION = CAL.CALENDAR_VERSION
DAILY_BAR_CUTOFF_MINUTE = 18 * 60          # a market day's daily bar is expected from 18:00 New York time (design C.5)


def _utc(now: datetime) -> datetime:
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def covered(d: date) -> bool:
    return CAL.covered(d) or HIST.covered(d)


def _close_minute(d: date) -> Optional[int]:
    """The regular close of a covered date (None on a weekend or a holiday): the history table for its years, else the 6.10A table."""
    return HIST.close_minute(d) if HIST.covered(d) else CAL.close_minute(d)


def calendar_version(d: Optional[date]) -> str:
    if d is None:
        return "UNAVAILABLE"
    if HIST.covered(d):
        return HIST.HISTORY_VERSION
    return CAL.CALENDAR_VERSION if CAL.covered(d) else "NOT_COVERED"


def post_market_end_minute(d: Optional[date]) -> Optional[int]:
    """When the late (post-market) session of a market day ends, minutes after midnight New York time; None on a non-session or uncovered
    day. History years: 17:00 on an early close (the NYSE release), else 20:00. 2026-2027: the 6.10A classifier's 20:00 on every market day
    (its early-close correction is a pending Stage 7.0 contract decision - DESIGN_71 section 5)."""
    if d is None or market_day_status(d) != MARKET_DAY:
        return None
    return HIST.post_market_end_minute(d) if HIST.covered(d) else AFTER_HOURS_END_MINUTE


def _classify_history(now: datetime) -> Dict[str, Any]:
    """A history-year moment, with the 6.10A classifier's own thresholds, the history table's close and its late-trading end (17:00 on
    an early-close day: the NYSE release)."""
    et = to_et(_utc(now))
    d = et.date()
    m = et.hour * 60 + et.minute + et.second / 60.0
    close = HIST.close_minute(d)
    late_end = HIST.post_market_end_minute(d) if close is not None else None
    if close is None or m < PREMARKET_MINUTE or m >= late_end:
        cls = A_CLOSED
    elif m < OPEN_MINUTE:
        cls = PREMARKET
    elif m < close:
        cls = REGULAR
    else:
        cls = AFTER_HOURS
    return {"session": cls, "trading_date": d.isoformat(), "et_time": et.strftime("%H:%M:%S"), "close_minute": close,
            "calendar": HIST.HISTORY_VERSION}


def market_day_status(d: Optional[date]) -> str:
    """MARKET_DAY | HOLIDAY | WEEKEND | NOT_COVERED (a weekday outside the table, or no date at all)."""
    if d is None:
        return NOT_COVERED
    if d.weekday() >= 5:
        return WEEKEND
    if not covered(d):
        return NOT_COVERED
    return MARKET_DAY if _close_minute(d) is not None else HOLIDAY


def is_market_day(d: date) -> bool:
    """True / False for a covered date or any weekend; CalendarNotCovered for an uncovered weekday (never a guess)."""
    st = market_day_status(d)
    if st == NOT_COVERED:
        raise CalendarNotCovered(f"{d} is outside the calendar table ({CALENDAR_VERSION})")
    return st == MARKET_DAY


def session(now: datetime) -> Dict[str, Any]:
    """{session, session_date, et_minute, market_day, close_minute, covered, calendar} for `now` (no broker clock: this layer reads none)."""
    c = SES.classify(_utc(now))
    if c.get("trading_date") and HIST.covered(date.fromisoformat(c["trading_date"])):
        c = _classify_history(now)                         # 2025: the verified history table, never the weekday assumption
    d = date.fromisoformat(c["trading_date"]) if c.get("trading_date") else None
    et = c.get("et_time")
    minute = int(et[:2]) * 60 + int(et[3:5]) if et else None
    status = market_day_status(d)
    return {"session": _MAP[c["session"]], "session_date": d, "et_minute": minute, "day_status": status,
            "market_day": None if status == NOT_COVERED else status == MARKET_DAY, "close_minute": c.get("close_minute"),
            "covered": bool(d and covered(d)), "calendar": c.get("calendar")}


def session_of(now: datetime) -> str:
    return session(now)["session"]


def _walk(d: date, step: int) -> Optional[date]:
    x = d + timedelta(days=step)
    for _ in range(WALK_LIMIT_DAYS):
        st = market_day_status(x)
        if st == MARKET_DAY:
            return x
        if st == NOT_COVERED:
            return None                                   # the answer lies outside the table: unknown, never guessed
        x += timedelta(days=step)
    return None


def previous_market_day(d: date) -> Optional[date]:
    """The previous market day, or None when the walk reaches an uncovered weekday first."""
    return _walk(d, -1)


def next_market_day(d: date) -> Optional[date]:
    """The next market day, or None when the walk reaches an uncovered weekday first."""
    return _walk(d, 1)


def last_completed_session(now: datetime) -> Optional[date]:
    """The latest market day whose daily bar is expected by `now`: today from 18:00 New York time on a market day, else the previous one."""
    s = session(now)
    d = s["session_date"]
    if d is None or s["day_status"] == NOT_COVERED:
        return None
    if s["day_status"] == MARKET_DAY and s["et_minute"] is not None and s["et_minute"] >= DAILY_BAR_CUTOFF_MINUTE:
        return d
    return previous_market_day(d)


def regular_minutes(d: date) -> Optional[int]:
    """Minutes in the regular session of `d` (390, 210 on an early close, 0 on a weekend or holiday; None when not covered)."""
    if market_day_status(d) == NOT_COVERED:
        return None
    close = _close_minute(d)
    return 0 if close is None else close - (9 * 60 + 30)


def rule_holidays(year: int) -> set:
    """NYSE holiday RULES (New Year, MLK, Presidents, Good Friday, Memorial, Juneteenth, Independence, Labor, Thanksgiving, Christmas;
    Saturday -> Friday, Sunday -> Monday; a Saturday New Year is not moved back). Used only to cross-check the static table: an
    exceptional closure (a national day of mourning) is never derivable from rules, which is why the table, not this, is the calendar."""
    def nth(month, weekday, n):
        x = date(year, month, 1)
        while x.weekday() != weekday:
            x += timedelta(days=1)
        return x + timedelta(days=7 * (n - 1))

    def last(month, weekday):
        x = date(year, month + 1, 1) - timedelta(days=1)
        while x.weekday() != weekday:
            x -= timedelta(days=1)
        return x

    def observed(x, move_saturday=True):
        if x.weekday() == 5:
            return x - timedelta(days=1) if move_saturday else None
        if x.weekday() == 6:
            return x + timedelta(days=1)
        return x

    a, b, c = year % 19, year // 100, year % 100                    # anonymous Gregorian Easter
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    good_friday = date(year, month, day) - timedelta(days=2)
    out = {observed(date(year, 1, 1), move_saturday=False), nth(1, 0, 3), nth(2, 0, 3), good_friday, last(5, 0), observed(date(year, 6, 19)),
           observed(date(year, 7, 4)), nth(9, 0, 1), nth(11, 3, 4), observed(date(year, 12, 25))}
    return {x for x in out if x is not None}
