"""
autorun/calendar.py - a STATIC, versioned US equity holiday / early-close table (Stage 6.10A).

The project has no exchange calendar dependency (setups/market_hours.py treats every weekday as a session). Stage 6.10A needs one only to keep
session-health honest: a holiday must not read as a missed (INVALID) session, and an early close shortens the expected regular-session minutes.
It is a plain table for 2026-2027 (the NYSE published schedule), recorded as CALENDAR_VERSION in every config snapshot. It never allows or blocks
an order: entries stay governed by the executor's unchanged SESSION_POLICY control, which also consults the broker clock when it is read.
A date outside the table is a normal weekday session (the old behaviour) and is reported as `calendar: NOT_COVERED`.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

CALENDAR_VERSION = "nyse_2026_2027_v1"
COVERED_YEARS = (2026, 2027)
HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
))
EARLY_CLOSE_MINUTE = 13 * 60                                     # 13:00 New York time
EARLY_CLOSES = frozenset(date.fromisoformat(d) for d in ("2026-11-27", "2026-12-24", "2027-11-26"))


def covered(d: date) -> bool:
    return d.year in COVERED_YEARS


def is_holiday(d: date) -> bool:
    return d in HOLIDAYS


def close_minute(d: date) -> Optional[int]:
    """The regular-session close (minutes after midnight New York time) of a market day; None on a weekend or a holiday."""
    if d.weekday() >= 5 or d in HOLIDAYS:
        return None
    return EARLY_CLOSE_MINUTE if d in EARLY_CLOSES else 16 * 60


def is_market_day(d: date) -> bool:
    return close_minute(d) is not None
