"""
autorun/session.py - the market-session class of a moment (pure, deterministic; Stage 6.10A).

  PREMARKET     market days 04:00 <= New York time < 09:30
  REGULAR       market days 09:30 <= New York time < the close (16:00; 13:00 on an early-close day)
  AFTER_HOURS   market days the close <= New York time < 20:00
  CLOSED        any other time, weekends and holidays (autorun/calendar.py), or a moment the broker clock reports closed in regular hours

Recording and arming policy only: new entries stay regular-session only through the executor's unchanged SESSION_POLICY control; nothing here
allows or blocks an order. A broker clock, when one is known, overrides the static calendar for the regular session.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from autorun import AFTER_HOURS, AFTER_HOURS_END_MINUTE, CLOSED, PREMARKET, PREMARKET_MINUTE, REGULAR
from autorun import calendar as CAL
from setups.market_hours import OPEN_MINUTE, to_et


def _utc(now: datetime) -> datetime:
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def classify(now: datetime, broker_clock: Optional[dict] = None) -> Dict[str, Any]:
    """{session, trading_date, et_time, market_day, close_minute, minutes_to_open, minutes_to_close, calendar} for `now`."""
    et = to_et(_utc(now))
    if et is None:
        return {"session": CLOSED, "trading_date": None, "et_time": None, "market_day": None, "close_minute": None, "minutes_to_open": None,
                "minutes_to_close": None, "calendar": "UNAVAILABLE", "note": "no time-zone database: the session is unknown and treated as CLOSED"}
    d = et.date()
    m = et.hour * 60 + et.minute + et.second / 60.0
    close = CAL.close_minute(d)
    if close is None or m < PREMARKET_MINUTE or m >= AFTER_HOURS_END_MINUTE:
        cls = CLOSED
    elif m < OPEN_MINUTE:
        cls = PREMARKET
    elif m < close:
        cls = REGULAR
    else:
        cls = AFTER_HOURS
    out: Dict[str, Any] = {"session": cls, "trading_date": d.isoformat(), "et_time": et.strftime("%H:%M:%S"), "market_day": close is not None,
                           "close_minute": close, "minutes_to_open": round(OPEN_MINUTE - m, 1) if close is not None and m < OPEN_MINUTE else None,
                           "minutes_to_close": round(close - m, 1) if close is not None and OPEN_MINUTE <= m < close else None,
                           "calendar": CAL.CALENDAR_VERSION if CAL.covered(d) else "NOT_COVERED"}
    if broker_clock and cls == REGULAR and broker_clock.get("is_open") is False:
        out.update(session=CLOSED, note="the broker clock reports the market closed during regular hours (holiday or early close)")
    return out


def session_of(now: datetime, broker_clock: Optional[dict] = None) -> str:
    return classify(now, broker_clock)["session"]


def _at(d: date, minute: int, tz) -> datetime:
    return datetime(d.year, d.month, d.day, minute // 60, minute % 60, tzinfo=tz).astimezone(timezone.utc)


def open_of(now: datetime) -> Optional[datetime]:
    """Today's 09:30 New York open as UTC (None when today is not a market day)."""
    et = to_et(_utc(now))
    if et is None or CAL.close_minute(et.date()) is None:
        return None
    return _at(et.date(), OPEN_MINUTE, et.tzinfo)


def close_of(now: datetime) -> Optional[datetime]:
    """Today's close as UTC (16:00, or 13:00 on an early-close day; None when today is not a market day)."""
    et = to_et(_utc(now))
    if et is None:
        return None
    close = CAL.close_minute(et.date())
    return None if close is None else _at(et.date(), close, et.tzinfo)


def last_close(now: datetime) -> Optional[datetime]:
    """The most recent market-day close at or before `now` (walks back over weekends and holidays; at most 10 days)."""
    et = to_et(_utc(now))
    if et is None:
        return None
    for back in range(0, 11):
        d = (et - timedelta(days=back)).date()
        close = CAL.close_minute(d)
        if close is None:
            continue
        at = _at(d, close, et.tzinfo)
        if at <= _utc(now):
            return at
    return None
