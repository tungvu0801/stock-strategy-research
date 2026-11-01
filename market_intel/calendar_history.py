"""
market_intel/calendar_history.py - Stage 7.1: VERIFIED NYSE history for years before the Stage 6.10A table, owned by market_intel.

autorun/calendar.py (frozen; the executor's session health reads it) covers 2026-2027 only. A historical backfill needs earlier years,
and an uncovered weekday is never assumed to be a session (DESIGN_70 section 5). Each year here is added only after verification against
the NYSE's own publications AND an independent empirical cross-check; a year that cannot be verified stays NOT_COVERED.

2025 (HISTORY_VERSION nyse_history_2025_v1), verified 2026-10-31:
* NYSE Group, "NYSE Group Announces 2025, 2026 and 2027 Holiday and Early Closings Calendar" (ICE investor relations, 2024): holidays
  2025-01-01, 01-20, 02-17, 04-18, 05-26, 06-19, 07-04, 09-01, 11-27, 12-25; early closes at 13:00 ET on 2025-07-03, 11-28, 12-24.
* NYSE / ICE notice (2024-12-30): all NYSE Group equity and options markets closed on Thursday 2025-01-09, the national day of mourning
  for former President Jimmy Carter - an exceptional closure no holiday rule derives.
* Empirical cross-check: archived IEX daily bars of 82 symbols (the Stage 6.4 value + quality bundle): the 2025 weekdays without a
  session are exactly those 11 dates; 250 sessions. (Daily bars cannot show early closes; those rest on the NYSE publication.)

Early-close days end the regular session at 13:00 ET and the late (post-market) trading session at 17:00 ET: the same NYSE Group
release states "NYSE American Equities, NYSE Arca Equities, NYSE Chicago, and NYSE National late trading sessions will close at 5:00 p.m."
on those dates (verified 2026-10-31). The 2025 history uses 17:00. (2026-2027 still follow the frozen Stage 6.10A classifier, which runs
the post-market to 20:00 on every market day: changing that is a Stage 7.0 contract decision - DESIGN_71 section 5.)
"""
from __future__ import annotations

from datetime import date

HISTORY_VERSION = "nyse_history_2025_v1"
HISTORY_YEARS = (2025,)
EARLY_CLOSE_MINUTE = 13 * 60
EARLY_CLOSE_LATE_TRADING_END_MINUTE = 17 * 60                  # the NYSE late trading session ends at 17:00 ET on early-close days
LATE_TRADING_END_MINUTE = 20 * 60
HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
))
EXCEPTIONAL_CLOSURES = frozenset((date(2025, 1, 9),))          # not derivable from the holiday rules
EARLY_CLOSES = frozenset(date.fromisoformat(d) for d in ("2025-07-03", "2025-11-28", "2025-12-24"))
SESSIONS_PER_YEAR = {2025: 250}                               # the empirical cross-check, pinned by tests
SOURCES = (
    "NYSE Group: 2025, 2026 and 2027 Holiday and Early Closings Calendar (ir.theice.com, 2024)",
    "NYSE: markets close on January 9, 2025 for the National Day of Mourning (ir.theice.com, 2024-12-30)",
    "empirical: IEX daily bars of 82 symbols, Stage 6.4 bundle - 250 sessions in 2025, the 11 closures exactly",
)


def covered(d: date) -> bool:
    return d.year in HISTORY_YEARS


def close_minute(d: date):
    """The regular-session close (minutes after midnight New York time) of a covered history day; None on a weekend or a holiday."""
    if not covered(d):
        raise ValueError(f"{d} is not in the market_intel history table")
    if d.weekday() >= 5 or d in HOLIDAYS:
        return None
    return EARLY_CLOSE_MINUTE if d in EARLY_CLOSES else 16 * 60


def post_market_end_minute(d: date):
    """When the late trading session of a covered history day ends (17:00 on an early close, 20:00 otherwise); None on a non-session day."""
    if close_minute(d) is None:
        return None
    return EARLY_CLOSE_LATE_TRADING_END_MINUTE if d in EARLY_CLOSES else LATE_TRADING_END_MINUTE
