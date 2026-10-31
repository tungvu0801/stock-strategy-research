"""
market_intel/budget.py - per-provider request budgets (design C.8). Pure and in memory, on an injected clock; Stage 7.1 persists the
counters. Nothing here makes a request: a caller asks `allow()` BEFORE a request and reports the response with `record()` after it.

* A token bucket per provider: capacity = the per-minute budget (a burst never exceeds one minute's budget), refilled continuously.
* An optional daily cap that resets at New York midnight (Alpha Vantage: 6 a day).
* Back-off: a 429 or 403 stops the provider for Retry-After (capped at 30 s, as the Stage 6.0.2 cash-flow reader caps it) or 60 s when the
  provider gave none. ACCESS_DENIED (evidence.net) is never retried within the cycle: blocked until `new_cycle()`.
* Plan pressure: more than one 429 within 10 minutes halves the provider's rate and raises PLAN_LIMIT_PRESSURE (shown on the status
  page from Stage 7.4). Only an explicit reset (`clear_pressure()`, an operator action in a later stage) restores it.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, Optional, Tuple

from market_intel import calendar as CAL
from market_intel.settings import settings as _settings

RETRY_AFTER_CAP_S = 30.0
DEFAULT_BACKOFF_S = 60.0
PRESSURE_WINDOW_S = 600.0
PRESSURE_429S = 2                         # "more than one 429 in 10 minutes"
OK, BLOCKED_BACKOFF, BLOCKED_CYCLE, RATE_LIMITED, DAILY_CAP, DISABLED = (
    "OK", "BLOCKED_BACKOFF", "BLOCKED_CYCLE", "RATE_LIMITED", "DAILY_CAP", "DISABLED")
PLAN_LIMIT_PRESSURE = "PLAN_LIMIT_PRESSURE"


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class TokenBucket:
    def __init__(self, per_minute: float, clock: Callable[[], datetime]):
        if per_minute < 0:
            raise ValueError("per_minute must be >= 0")
        self.clock = clock
        self.per_minute = float(per_minute)
        self.capacity = float(per_minute)
        self.tokens = float(per_minute)
        self._at = _utc(clock())

    def _refill(self) -> None:
        now = _utc(self.clock())
        dt = max(0.0, (now - self._at).total_seconds())          # a clock that steps back never mints tokens
        self.tokens = min(self.capacity, self.tokens + dt * self.per_minute / 60.0)
        self._at = max(self._at, now)

    def try_take(self, n: float = 1.0) -> bool:
        self._refill()
        if n <= self.tokens + 1e-9:
            self.tokens -= n
            return True
        return False

    def wait_s(self, n: float = 1.0) -> Optional[float]:
        """Seconds until `n` tokens exist (None: never, the rate is zero or n exceeds the capacity)."""
        self._refill()
        if n <= self.tokens + 1e-9:
            return 0.0
        if self.per_minute <= 0 or n > self.capacity:
            return None
        return (n - self.tokens) * 60.0 / self.per_minute

    def scale(self, factor: float) -> None:
        self._refill()
        self.per_minute *= factor
        self.capacity *= factor
        self.tokens = min(self.tokens, self.capacity)


class ProviderBudget:
    def __init__(self, name: str, per_minute: float, clock: Callable[[], datetime], *, daily_cap: Optional[int] = None):
        self.name, self.clock, self.daily_cap = name, clock, daily_cap
        self.bucket = TokenBucket(per_minute, clock)
        self.base_per_minute = float(per_minute)
        self.blocked_until: Optional[datetime] = None
        self.blocked_cycle = False
        self.day = None
        self.day_count = 0
        self.recent_429: Deque[datetime] = deque()
        self.pressure = False
        self.requests = 0
        self.refused: Dict[str, int] = {}

    def _now(self) -> datetime:
        return _utc(self.clock())

    def _roll_day(self, now: datetime) -> None:
        d = CAL.session(now)["session_date"]
        if d != self.day:
            self.day, self.day_count = d, 0

    def _refuse(self, reason: str) -> Tuple[bool, str]:
        self.refused[reason] = self.refused.get(reason, 0) + 1
        return False, reason

    def allow(self, n: int = 1) -> Tuple[bool, str]:
        """(may the caller make `n` requests now, why). A True answer spends the budget."""
        now = self._now()
        self._roll_day(now)
        if self.bucket.per_minute <= 0 or self.daily_cap == 0:
            return self._refuse(DISABLED)
        if self.blocked_cycle:
            return self._refuse(BLOCKED_CYCLE)
        if self.blocked_until is not None and now < self.blocked_until:
            return self._refuse(BLOCKED_BACKOFF)
        if self.daily_cap is not None and self.day_count + n > self.daily_cap:
            return self._refuse(DAILY_CAP)
        if not self.bucket.try_take(n):
            return self._refuse(RATE_LIMITED)
        self.day_count += n
        self.requests += n
        return True, OK

    def record(self, http_status: Optional[int] = None, *, retry_after_s: Optional[float] = None, access_denied: bool = False) -> None:
        """Report one response. 429 / 403: back off. ACCESS_DENIED: blocked for the rest of the cycle. Repeated 429s: plan pressure."""
        now = self._now()
        if access_denied:
            self.blocked_cycle = True
        if http_status in (429, 403):
            wait = DEFAULT_BACKOFF_S if retry_after_s is None else min(max(0.0, float(retry_after_s)), RETRY_AFTER_CAP_S)
            until = now + timedelta(seconds=wait)
            self.blocked_until = until if self.blocked_until is None or until > self.blocked_until else self.blocked_until
        if http_status == 429:
            self.recent_429.append(now)
            while self.recent_429 and (now - self.recent_429[0]).total_seconds() > PRESSURE_WINDOW_S:
                self.recent_429.popleft()
            if len(self.recent_429) >= PRESSURE_429S and not self.pressure:
                self.pressure = True
                self.bucket.scale(0.5)

    def new_cycle(self) -> None:
        self.blocked_cycle = False

    def clear_pressure(self) -> None:
        if self.pressure:
            self.pressure = False
            self.bucket.scale(self.base_per_minute / self.bucket.per_minute if self.bucket.per_minute else 1.0)
        self.recent_429.clear()

    def status(self) -> dict:
        now = self._now()
        self._roll_day(now)
        return {"provider": self.name, "per_minute": round(self.bucket.per_minute, 3), "base_per_minute": self.base_per_minute,
                "tokens": round(self.bucket.tokens, 3), "daily_cap": self.daily_cap, "used_today": self.day_count, "requests": self.requests,
                "refused": dict(self.refused), "blocked_until": self.blocked_until.isoformat(timespec="seconds") if self.blocked_until else None,
                "blocked_cycle": self.blocked_cycle, "flags": [PLAN_LIMIT_PRESSURE] if self.pressure else []}


def default_budgets(clock: Callable[[], datetime], cfg=None) -> Dict[str, ProviderBudget]:
    """One budget per provider from the normalised settings (today: every default, config declares none of them yet)."""
    b = _settings(cfg)["budgets"]
    return {
        "ALPACA": ProviderBudget("ALPACA", b["MI_ALPACA_MAX_PER_MIN"], clock),
        "ALPACA_BACKFILL": ProviderBudget("ALPACA_BACKFILL", b["MI_ALPACA_BACKFILL_MAX_PER_MIN"], clock),
        "SEC": ProviderBudget("SEC", b["MI_SEC_MAX_PER_MIN"], clock),
        "FRED": ProviderBudget("FRED", b["MI_FRED_MAX_PER_MIN"], clock),
        "NASDAQ_TRADER": ProviderBudget("NASDAQ_TRADER", b["MI_HALTS_MAX_PER_MIN"], clock),
        "ALPHA_VANTAGE": ProviderBudget("ALPHA_VANTAGE", 1, clock, daily_cap=b["MI_ALPHA_VANTAGE_MAX_PER_DAY"]),
    }
