"""
Deterministic US regular-session helper for Stage 5.8: weekdays, 09:30 <= America/New_York < 16:00 (zoneinfo handles DST).
LIMITATION (documented, not hidden): the project has no exchange holiday calendar and no calendar dependency, so exchange holidays
and early closes are treated like ordinary weekdays. Nothing here contacts an exchange or a feed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

OPEN_MINUTE = 9 * 60 + 30
CLOSE_MINUTE = 16 * 60
HOLIDAY_CALENDAR = "NONE (weekdays only; exchange holidays and early closes are not modelled)"


def _et():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - no tz database: say so (UTC fallback would be wrong by hours)
        return None


def to_et(now: datetime) -> Optional[datetime]:
    tz = _et()
    if tz is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz)


def session_date(now: datetime) -> Optional[date]:
    et = to_et(now)
    return et.date() if et else None


def is_regular_session(now: datetime) -> bool:
    et = to_et(now)
    if et is None:
        return False
    if et.weekday() >= 5:
        return False
    m = et.hour * 60 + et.minute
    return OPEN_MINUTE <= m < CLOSE_MINUTE


def next_session_open(now: datetime) -> Optional[datetime]:
    """The next 09:30 ET strictly after `now` on a weekday (UTC-aware)."""
    et = to_et(now)
    if et is None:
        return None
    candidate = et.replace(hour=9, minute=30, second=0, microsecond=0)
    if candidate <= et or candidate.weekday() >= 5:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=9, minute=30, second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def seconds_until_open(now: datetime, cap: float = 900.0) -> float:
    nxt = next_session_open(now)
    if nxt is None:
        return cap
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(1.0, min(cap, (nxt - now).total_seconds()))


def describe(now: datetime) -> dict:
    et = to_et(now)
    return {"regular_session": is_regular_session(now), "session_date": session_date(now).isoformat() if session_date(now) else None,
            "et_time": et.strftime("%H:%M") if et else None, "weekday": et.weekday() if et else None, "holiday_calendar": HOLIDAY_CALENDAR}
