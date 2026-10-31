"""
market_intel/freshness.py - the PRE-REGISTERED freshness and state policy per kind (design C.5). Pure: (what we last saw, now) -> a state.

Display and fail-closed only: an assessment never fetches, never repairs and never allows anything. It says how old a value is (FRESH /
AGING / STALE), whether it may be used (AVAILABLE / STALE / UNAVAILABLE / INSUFFICIENT_DATA / NOT_APPLICABLE), why, and - when it may
not be used - what a consumer does instead (the `action`, written down now, before any consumer exists).

Every assessment carries `usable`: True only when the status is AVAILABLE. A consumer that needs the value requires usable; nothing
else (an AGING freshness, a reported age, a previous vintage) makes a value usable.

Rules shared by every kind:
* nothing ever seen -> UNAVAILABLE (NEVER_OBSERVED): a missing value is never zero, never "low risk", never "no news";
* a reference time more than CLOCK_SKEW_S in the future -> UNAVAILABLE (FUTURE_TIMESTAMP), fail closed;
* outside the kind's window (e.g. a quote outside the regular session) -> NOT_APPLICABLE (OUTSIDE_WINDOW): the age is reported, no
  consumer acts on it;
* a date the calendar does not cover, for a check that needs "is this a market day?" (the REG / EXTENDED windows, the daily bar)
  -> UNAVAILABLE (CALENDAR_NOT_COVERED): an uncovered weekday is never assumed to be a session.

MACRO is stricter than the C.5 draft (second review): once a scheduled release time has passed, the series is current only when that
release has been observed. Until then the previous vintage is history, not the current value: UNAVAILABLE (RELEASE_PENDING) from the
release time, escalating to STALE (RELEASE_MISSING) one day later. There is no usable AGING state for MACRO.

The numbers below are the design's C.5 table. They are pinned by tests/test_market_intel_70.py; changing one is a new, declared stage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from market_intel import AGING, AVAILABLE, FRESH, INSUFFICIENT_DATA, NOT_APPLICABLE, POST, PRE, REG, STALE, UNAVAILABLE
from market_intel import calendar as CAL

CLOCK_SKEW_S = 2.0                      # design C.7: a skew above 2 s marks CLOCK_SKEW
POLICY_VERSION = "mi_freshness_v2"                 # v2: explicit `usable`; MACRO pending is unusable; uncovered calendar days fail closed

# windows: when a kind is expected to be current
W_ALWAYS, W_REG, W_EXTENDED, W_SEC = "ALWAYS", "REG", "EXTENDED", "SEC_06_22"   # EXTENDED = PRE + REG + POST of a market day


@dataclass(frozen=True)
class Policy:
    key: str
    reference: str                  # which timestamp the age is measured from
    fresh_s: float
    aging_s: float
    window: str
    action: str                     # what a consumer does when the value is STALE or UNAVAILABLE
    fail_closed_scope: str = "SYMBOL"   # SYMBOL: blocks that symbol's use; ALL: blocks every new candidate of the consumer

    def as_dict(self) -> dict:
        return asdict(self)


POLICIES: Dict[str, Policy] = {p.key: p for p in (
    Policy("QUOTE_DAY", "quote time", 5, 15, W_REG, "no new day candidate for that symbol"),
    Policy("QUOTE_SWING", "quote time", 60, 300, W_REG, "swing intraday confirmation skipped; close-based signals unaffected"),
    Policy("BAR_1MIN", "end of the last completed minute bar", 120, 300, W_REG,
           "Demand-style features read INSUFFICIENT_DATA; on IEX a minute without prints is NO_PRINTS, not STALE"),
    Policy("BAR_1HOUR", "start of the last completed hour bar", 70 * 60, 130 * 60, W_REG, "hourly confirmation skipped"),
    Policy("NEWS_POLL", "last successful news poll", 180, 600, W_EXTENDED,
           "the catalyst flag reads UNAVAILABLE, never 'no news'; day entries that require a catalyst are blocked"),
    Policy("HALT_FEED", "last successful halt-feed poll", 120, 180, W_REG, "all new day candidates are blocked", fail_closed_scope="ALL"),
    Policy("SEC_POLL", "last successful SEC filings poll", 900, 1800, W_SEC, "the filing flag reads UNAVAILABLE"),
    Policy("EARNINGS_CALENDAR", "last successful calendar refresh", 0, 36 * 3600, W_ALWAYS,
           "swing candidates without a known earnings date are blocked (EARNINGS_UNKNOWN)"),
)}
# the three date-based kinds have their own functions below
DAILY_BAR_ACTION = "no swing signal for that symbol"
MACRO_ACTION = "the regime label reads UNKNOWN; the pre-registered default applies (no new swing entries)"
FUNDAMENTALS_ACTION = "the fundamental filter reads INSUFFICIENT_DATA"
FUNDAMENTALS_MAX_AGE_DAYS = 200          # value_quality.MAX_FUNDAMENTAL_AGE_DAYS (pinned equal by a test)


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _out(key, status, freshness, age_s, reason, action, **kw) -> dict:
    return {"key": key, "status": status, "usable": status == AVAILABLE, "freshness": freshness, "age_s": None if age_s is None else round(age_s, 3),
            "reason": reason, "action": action if status in (STALE, UNAVAILABLE, INSUFFICIENT_DATA) else None, "policy_version": POLICY_VERSION, **kw}


def _classify(age_s: float, fresh_s: float, aging_s: float) -> str:
    return FRESH if age_s <= fresh_s else (AGING if age_s <= aging_s else STALE)


def window_open(window: str, now: datetime) -> bool:
    s = CAL.session(now)
    if window == W_ALWAYS:
        return True
    if window == W_REG:
        return s["session"] == REG
    if window == W_EXTENDED:
        return s["session"] in (PRE, REG, POST)
    if window == W_SEC:                                  # weekdays 06:00-22:00 New York time (EDGAR accepts filings on business days)
        d = s["session_date"]
        return d is not None and d.weekday() < 5 and s["et_minute"] is not None and 6 * 60 <= s["et_minute"] < 22 * 60
    raise ValueError(f"unknown window {window}")


def assess(key: str, last_at: Optional[datetime], now: datetime, *, prints_expected: Optional[bool] = None) -> dict:
    """The state of one age-based kind. `prints_expected=False` (BAR_1MIN only): the feed shows no trade in the gap, so a missing
    minute is NO_PRINTS (INSUFFICIENT_DATA), never a STALE feed."""
    p = POLICIES[key]
    now = _utc(now)
    if key == "EARNINGS_CALENDAR":
        return assess_earnings_calendar(last_at, now)
    age = None if last_at is None else (now - _utc(last_at)).total_seconds()
    if age is not None and age < -CLOCK_SKEW_S:
        return _out(key, UNAVAILABLE, None, None, "FUTURE_TIMESTAMP", p.action, scope=p.fail_closed_scope)
    age = None if age is None else max(0.0, age)
    if p.window in (W_REG, W_EXTENDED) and CAL.market_day_status(CAL.session(now)["session_date"]) == CAL.NOT_COVERED:
        return _out(key, UNAVAILABLE, None, age, "CALENDAR_NOT_COVERED", p.action, scope=p.fail_closed_scope)
    if not window_open(p.window, now):
        return _out(key, NOT_APPLICABLE, None if age is None else _classify(age, p.fresh_s, p.aging_s), age, "OUTSIDE_WINDOW", p.action,
                    scope=p.fail_closed_scope)
    if age is None:
        return _out(key, UNAVAILABLE, None, None, "NEVER_OBSERVED", p.action, scope=p.fail_closed_scope)
    f = _classify(age, p.fresh_s, p.aging_s)
    if f == STALE and key == "BAR_1MIN" and prints_expected is False:
        return _out(key, INSUFFICIENT_DATA, None, age, "NO_PRINTS", p.action, scope=p.fail_closed_scope)
    return _out(key, STALE if f == STALE else AVAILABLE, f, age, "AGE_LIMIT" if f == STALE else "OK", p.action, scope=p.fail_closed_scope)


def assess_earnings_calendar(refreshed_at: Optional[datetime], now: datetime) -> dict:
    """FRESH when refreshed on today's New York date, AGING within 36 h, STALE after."""
    p = POLICIES["EARNINGS_CALENDAR"]
    now = _utc(now)
    if refreshed_at is None:
        return _out(p.key, UNAVAILABLE, None, None, "NEVER_OBSERVED", p.action, scope=p.fail_closed_scope)
    age = (now - _utc(refreshed_at)).total_seconds()
    if age < -CLOCK_SKEW_S:
        return _out(p.key, UNAVAILABLE, None, None, "FUTURE_TIMESTAMP", p.action, scope=p.fail_closed_scope)
    age = max(0.0, age)
    same_day = CAL.session(now)["session_date"] == CAL.session(_utc(refreshed_at))["session_date"]
    f = FRESH if same_day else (AGING if age <= p.aging_s else STALE)
    return _out(p.key, STALE if f == STALE else AVAILABLE, f, age, "AGE_LIMIT" if f == STALE else "OK", p.action, scope=p.fail_closed_scope)


def assess_daily_bar(last_session: Optional[date], now: datetime) -> dict:
    """FRESH when the last completed session's bar is present (expected from 18:00 New York time on a market day); STALE when missing."""
    now = _utc(now)
    s = CAL.session(now)
    expected = CAL.last_completed_session(now)
    extra = {"expected_session": expected.isoformat() if expected else None, "last_session": last_session.isoformat() if last_session else None}
    if not s["covered"] or expected is None:                # today, or the session the bar must come from, is outside the table
        return _out("BAR_1DAY", UNAVAILABLE, None, None, "CALENDAR_NOT_COVERED", DAILY_BAR_ACTION, **extra)
    if last_session is None:
        return _out("BAR_1DAY", UNAVAILABLE, None, None, "NEVER_OBSERVED", DAILY_BAR_ACTION, **extra)
    if last_session > s["session_date"]:
        return _out("BAR_1DAY", UNAVAILABLE, None, None, "FUTURE_TIMESTAMP", DAILY_BAR_ACTION, **extra)
    ok = expected is not None and last_session >= expected
    return _out("BAR_1DAY", AVAILABLE if ok else STALE, FRESH if ok else STALE, None, "OK" if ok else "SESSION_MISSING", DAILY_BAR_ACTION, **extra)


MACRO_PENDING_ESCALATION_S = 86400.0      # RELEASE_PENDING becomes RELEASE_MISSING (STALE) one day after the scheduled release


def assess_macro(expected_release_at: Optional[datetime], observed_release_at: Optional[datetime], now: datetime) -> dict:
    """`expected_release_at`: the latest scheduled release at or before now. `observed_release_at`: the release time of the newest vintage
    held. FRESH (usable) only when that release has been observed. From the release time until it is observed the current value does not
    exist for any consumer: UNAVAILABLE (RELEASE_PENDING); one day later STALE (RELEASE_MISSING). Never AVAILABLE on a previous vintage."""
    now = _utc(now)
    if expected_release_at is None:
        return _out("MACRO", UNAVAILABLE, None, None, "NO_RELEASE_SCHEDULE", MACRO_ACTION)
    exp = _utc(expected_release_at)
    if exp > now + timedelta(seconds=CLOCK_SKEW_S):
        return _out("MACRO", UNAVAILABLE, None, None, "FUTURE_TIMESTAMP", MACRO_ACTION)
    if observed_release_at is not None and _utc(observed_release_at) >= exp:
        return _out("MACRO", AVAILABLE, FRESH, None, "OK", MACRO_ACTION)
    late = max(0.0, (now - exp).total_seconds())
    if late <= MACRO_PENDING_ESCALATION_S:
        return _out("MACRO", UNAVAILABLE, None, late, "RELEASE_PENDING", MACRO_ACTION)
    return _out("MACRO", STALE, STALE, late, "RELEASE_MISSING", MACRO_ACTION)


def assess_fundamentals(quarter_end: Optional[date], now: datetime) -> dict:
    """FRESH when the latest fiscal quarter used ended within 200 days (the Value + Quality rule); STALE (INSUFFICIENT for the filter) after."""
    now = _utc(now)
    if quarter_end is None:
        return _out("FUNDAMENTALS", UNAVAILABLE, None, None, "NEVER_OBSERVED", FUNDAMENTALS_ACTION)
    today = CAL.session(now)["session_date"] or now.date()
    days = (today - quarter_end).days
    if days < 0:
        return _out("FUNDAMENTALS", UNAVAILABLE, None, None, "FUTURE_TIMESTAMP", FUNDAMENTALS_ACTION)
    ok = days <= FUNDAMENTALS_MAX_AGE_DAYS
    return _out("FUNDAMENTALS", AVAILABLE if ok else STALE, FRESH if ok else STALE, days * 86400.0, "OK" if ok else "AGE_LIMIT", FUNDAMENTALS_ACTION,
                age_days=days)
