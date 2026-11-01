"""
market_intel/collector/plans.py - Stage 7.3: the registered collection plans (DESIGN_73 section 2). Pure.

A plan fixes, per source: its polling cadence (design C.8), its Stage 7.0 budget, its caps (pages, bytes, rows per run), its live
lookback (how far back a windowed run may reach without a historical approval), its re-read overlap (late revisions and late arrivals),
its feed-health policy (a Stage 7.0 freshness policy: no new number), its credential (by NAME) and the approvals a real request needs.
Plans are frozen and registered in a read-only mapping: a runner accepts only the exact registered object (an edited copy is
PLAN_NOT_REGISTERED).

Only news is windowed (a watermark window [watermark - overlap, now], at most 36 h back). FRED reads a fixed observation window - the
last `observation_days` observation dates with every vintage - because a monthly or quarterly series' observation date lies weeks
before its release (DESIGN_73 section 6). Snapshots, halts and the earnings calendar are current-state polls without a window.

Windows (New York time): EXTENDED = PRE + REG + POST of a market day (freshness W_EXTENDED); REG; PRE_FROM_0700 = the pre-market from
07:00; SEC_06_22 = weekdays 06:00-22:00 (freshness W_SEC); MARKET_0400_2000 = 04:00-20:00 on a market day; DAILY = once a New York day
after `at_minute`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

from market_intel import PRE
from market_intel import calendar as CAL
from market_intel import freshness as FR
from market_intel.settings import BUDGETS
from market_intel.store import MAX_DATASET_ROWS

EXTENDED, REG, PRE_FROM_0700, SEC_06_22, MARKET_0400_2000, DAILY = "EXTENDED", "REG", "PRE_FROM_0700", "SEC_06_22", "MARKET_0400_2000", "DAILY"
WINDOW_SECONDS = {EXTENDED: 16 * 3600, REG: int(6.5 * 3600), PRE_FROM_0700: int(2.5 * 3600), SEC_06_22: 16 * 3600, MARKET_0400_2000: 16 * 3600,
                  DAILY: 86400}
DAILY_REFRESH = "DAILY_REFRESH"          # the 7.0 daily-refresh rule (assess_earnings_calendar): same New York day / 36 h
SEC_TZ_BLOCK = "SEC_ACCEPTANCE_TZ_UNVERIFIED: SEC acceptanceDateTime's timezone is unverified - no SEC ingestion until verified and approved"


@dataclass(frozen=True)
class Cadence:
    window: str
    interval_s: int
    at_minute: Optional[int] = None      # DAILY: minutes after New York midnight


@dataclass(frozen=True)
class Scope:
    """What one collection covers: symbols (news, snapshots, earnings filter), a feed (snapshots), a series (FRED)."""
    symbols: Tuple[str, ...] = ()
    feed: str = "none"
    series: Optional[str] = None

    def key(self) -> str:
        return f"symbols={','.join(sorted(s.upper() for s in self.symbols))}|feed={self.feed}|series={self.series or ''}"


@dataclass(frozen=True)
class CollectionPlan:
    source: str
    kind: str
    provider: str
    budget_name: str                     # market_intel.budget.default_budgets key
    budget_setting: str                  # market_intel.settings.BUDGETS key
    cadences: Tuple[Cadence, ...]
    health_policy: str                   # a freshness.POLICIES key, or DAILY_REFRESH
    max_pages: int
    max_bytes: int
    max_rows_per_run: int
    lookback_max_s: int
    overlap_s: int
    credential: Optional[str]
    approvals: Tuple[str, ...]
    blocked_reason: Optional[str] = None
    concurrency: int = 1
    windowed: bool = False               # a watermark window [start, end] is part of the request (news)
    observation_days: Optional[int] = None   # FRED: the observation dates read (today - n .. today), every vintage
    notes: Tuple[str, ...] = field(default_factory=tuple)


MB = 1024 * 1024
PLANS: Mapping[str, CollectionPlan] = MappingProxyType({p.source: p for p in (
    CollectionPlan("alpaca_news", "NEWS", "ALPACA", "ALPACA", "MI_ALPACA_MAX_PER_MIN", (Cadence(EXTENDED, 120),), "NEWS_POLL", 20, 2 * MB, 1_000,
                   36 * 3600, 600, "ALPACA_API_KEY", ("REAL_FETCH", "ALPACA_DATA_LAYER_USE"), windowed=True),
    CollectionPlan("alpaca_snapshots", "QUOTE_SNAPSHOT", "ALPACA", "ALPACA", "MI_ALPACA_MAX_PER_MIN", (Cadence(REG, 60), Cadence(PRE_FROM_0700, 300)),
                   "QUOTE_SWING", 1, 2 * MB, 100, 0, 0, "ALPACA_API_KEY", ("REAL_FETCH", "ALPACA_DATA_LAYER_USE", "SIP_FEED for sip")),
    CollectionPlan("halts", "HALT", "NASDAQ_TRADER", "NASDAQ_TRADER", "MI_HALTS_MAX_PER_MIN", (Cadence(MARKET_0400_2000, 60),), "HALT_FEED", 1, 2 * MB,
                   500, 0, 0, None, ("REAL_FETCH", "NASDAQ_RSS_TERMS")),
    CollectionPlan("sec_filings", "FILING", "SEC", "SEC", "MI_SEC_MAX_PER_MIN", (Cadence(SEC_06_22, 600),), "SEC_POLL", 1, 5 * MB, 1_000, 0, 0,
                   "SEC_USER_AGENT", ("REAL_FETCH", "SEC_USER_AGENT_CONFIRMED", "SEC_ACCEPTANCE_TZ_VERIFIED"), blocked_reason=SEC_TZ_BLOCK),
    CollectionPlan("sec_facts", "FACT", "SEC", "SEC", "MI_SEC_MAX_PER_MIN", (Cadence(DAILY, 86400, 3 * 60 + 30),), DAILY_REFRESH, 1, 20 * MB, 50_000,
                   0, 0, "SEC_USER_AGENT", ("REAL_FETCH", "SEC_USER_AGENT_CONFIRMED", "SEC_ACCEPTANCE_TZ_VERIFIED"), blocked_reason=SEC_TZ_BLOCK),
    CollectionPlan("fred", "MACRO", "FRED", "FRED", "MI_FRED_MAX_PER_MIN", (Cadence(DAILY, 86400, 7 * 60),), DAILY_REFRESH, 5, 5 * MB, 50_000,
                   0, 0, "FRED_API_KEY", ("REAL_FETCH", "FRED_KEY_CONFIRMED", "FRED_SERIES_TERMS"), observation_days=400),
    CollectionPlan("earnings_av", "EARNINGS", "ALPHA_VANTAGE", "ALPHA_VANTAGE", "MI_ALPHA_VANTAGE_MAX_PER_DAY", (Cadence(DAILY, 86400, 6 * 60 + 30),),
                   "EARNINGS_CALENDAR", 1, 5 * MB, 20_000, 0, 0, "ALPHAVANTAGE_API_KEY", ("REAL_FETCH", "ALPHAVANTAGE_KEY_AND_TERMS")),
)})


def registered(plan) -> bool:
    return isinstance(plan, CollectionPlan) and PLANS.get(plan.source) is plan


def _open(window: str, now: datetime) -> bool:
    s = CAL.session(now)
    if window in (EXTENDED, REG, SEC_06_22):
        return FR.window_open({EXTENDED: FR.W_EXTENDED, REG: FR.W_REG, SEC_06_22: FR.W_SEC}[window], now)
    if window == PRE_FROM_0700:
        return s["session"] == PRE and s["et_minute"] is not None and s["et_minute"] >= 7 * 60
    if window == MARKET_0400_2000:
        return s["day_status"] == CAL.MARKET_DAY and s["et_minute"] is not None and 4 * 60 <= s["et_minute"] < 20 * 60
    if window == DAILY:
        return True
    raise ValueError(f"unknown window {window}")


def active_cadence(plan: CollectionPlan, now: datetime) -> Optional[Cadence]:
    """The cadence that applies at `now` (the first whose window is open), or None: nothing is due outside the windows."""
    return next((c for c in plan.cadences if _open(c.window, now)), None)


def runs_per_day_bound(plan: CollectionPlan) -> int:
    return sum(1 if c.window == DAILY else -(-WINDOW_SECONDS[c.window] // c.interval_s) for c in plan.cadences)


def daily_row_bound(plan: CollectionPlan) -> int:
    """The most rows a plan's cadence can write in one New York day (storage growth is bounded by construction)."""
    return runs_per_day_bound(plan) * plan.max_rows_per_run


def check_plans() -> dict:
    """Every plan's problems (empty lists = consistent): budgets declared in Stage 7.0, caps within the store's, a health policy that exists."""
    out = {}
    for name, p in PLANS.items():
        bad = []
        if p.budget_setting not in BUDGETS:
            bad.append("budget setting")
        if p.health_policy != DAILY_REFRESH and p.health_policy not in FR.POLICIES:
            bad.append("health policy")
        if not 0 < p.max_rows_per_run <= MAX_DATASET_ROWS or p.max_pages < 1 or p.max_bytes < 1 or p.concurrency < 1:
            bad.append("caps")
        out[name] = bad
    return out


def scope_problem(plan: CollectionPlan, scope: Scope) -> Optional[str]:
    """Why `scope` is not a valid scope of `plan` (None: valid). Symbols are upper-case tickers (the API's own rule), never repeated."""
    from api.validation import validate_symbol
    if not isinstance(scope, Scope):
        return "NOT_A_SCOPE"
    syms = list(scope.symbols)
    try:
        if any(validate_symbol(str(s)) != str(s) for s in syms) or len(set(syms)) != len(syms):
            return "SYMBOLS"
    except ValueError:
        return "SYMBOLS"
    s = plan.source
    if s == "alpaca_news":
        ok = 1 <= len(syms) <= 50 and scope.feed == "none" and scope.series is None
    elif s == "alpaca_snapshots":
        ok = 1 <= len(syms) <= 100 and scope.feed in ("iex", "sip") and scope.series is None
    elif s == "halts":
        ok = not syms and scope.feed == "none" and scope.series is None
    elif s == "fred":
        from market_intel.sources.fred import SERIES
        ok = not syms and scope.feed == "none" and scope.series in SERIES
    elif s == "earnings_av":
        ok = len(syms) <= 500 and scope.feed == "none" and scope.series is None
    else:
        ok = False                                                         # the SEC plans are blocked: no scope is collected
    return None if ok else "SCOPE_FIELDS"
