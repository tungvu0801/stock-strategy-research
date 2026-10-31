"""
market_intel/calendar.py - New York sessions for the data layer: PRE / REG / POST / CLOSED.

One source of truth: the Stage 6.10A classifier (autorun/session.py) and its static, versioned NYSE table for 2026-2027
(autorun/calendar.py), both read only and pure. PRE is 04:00-09:30, REG 09:30 to the close (16:00, or 13:00 on an early-close day),
POST the close to 20:00, CLOSED otherwise, on weekends and on holidays. Daylight-saving changes are handled by zoneinfo.

A date outside the table is NOT_COVERED. The classifier then treats a weekday as a normal session, and the data layer says so
(`covered: False`): freshness checks that depend on "was today a market day?" fail closed on an uncovered date (freshness.py). The table
must be extended (from the NYSE published schedule) before 2028; `rule_holidays` exists only to cross-check the static table.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from autorun import AFTER_HOURS, CLOSED as A_CLOSED, PREMARKET, REGULAR
from autorun import calendar as CAL
from autorun import session as SES
from market_intel import CLOSED, POST, PRE, REG

_MAP = {PREMARKET: PRE, REGULAR: REG, AFTER_HOURS: POST, A_CLOSED: CLOSED}
CALENDAR_VERSION = CAL.CALENDAR_VERSION
DAILY_BAR_CUTOFF_MINUTE = 18 * 60          # a market day's daily bar is expected from 18:00 New York time (design C.5)


def _utc(now: datetime) -> datetime:
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def covered(d: date) -> bool:
    return CAL.covered(d)


def is_market_day(d: date) -> bool:
    """A weekday that is not a table holiday (an uncovered year: every weekday, reported by covered())."""
    return CAL.close_minute(d) is not None


def session(now: datetime) -> Dict[str, Any]:
    """{session, session_date, et_minute, market_day, close_minute, covered, calendar} for `now` (no broker clock: this layer reads none)."""
    c = SES.classify(_utc(now))
    d = date.fromisoformat(c["trading_date"]) if c.get("trading_date") else None
    et = c.get("et_time")
    minute = int(et[:2]) * 60 + int(et[3:5]) if et else None
    return {"session": _MAP[c["session"]], "session_date": d, "et_minute": minute, "market_day": c.get("market_day"),
            "close_minute": c.get("close_minute"), "covered": bool(d and covered(d)), "calendar": c.get("calendar")}


def session_of(now: datetime) -> str:
    return session(now)["session"]


def previous_market_day(d: date) -> date:
    x = d - timedelta(days=1)
    for _ in range(15):
        if is_market_day(x):
            return x
        x -= timedelta(days=1)
    raise AssertionError("no market day in 15 days")


def next_market_day(d: date) -> date:
    x = d + timedelta(days=1)
    for _ in range(15):
        if is_market_day(x):
            return x
        x += timedelta(days=1)
    raise AssertionError("no market day in 15 days")


def last_completed_session(now: datetime) -> Optional[date]:
    """The latest market day whose daily bar is expected by `now`: today from 18:00 New York time on a market day, else the previous one."""
    s = session(now)
    d = s["session_date"]
    if d is None:
        return None
    if is_market_day(d) and s["et_minute"] is not None and s["et_minute"] >= DAILY_BAR_CUTOFF_MINUTE:
        return d
    return previous_market_day(d)


def regular_minutes(d: date) -> int:
    """Minutes in the regular session of `d` (390, 210 on an early close, 0 on a weekend or holiday)."""
    close = CAL.close_minute(d)
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
