"""
market_intel/collector/policy.py - Stage 7.3: retry, backoff with deterministic jitter, and the circuit breaker (DESIGN_73 section 3). Pure.

Retry: at most MAX_ATTEMPTS attempts of the same request; before attempt n+1 the runner waits backoff_s(n) = BACKOFF_BASE_S * 2^(n-1),
capped at BACKOFF_CAP_S, times a jitter factor in [1 - JITTER, 1 + JITTER] taken from a hash of the request key and the attempt - so a
run on a fake clock is exactly reproducible, and two collectors retrying different requests still spread out. After a 429 the wait is
at least the budget's own back-off (Retry-After, capped by Stage 7.0 at 30 s; 60 s when the provider gave none).

Breaker (one per source, every scope included - fail closed): a run FAILS when one of its requests failed and was not recovered by a
later attempt. BREAKER_FAILURES consecutive failed runs open it for BREAKER_COOLDOWN_S; when the cool-down has passed one trial run is
allowed (HALF_OPEN); a trial that fails opens it again for twice the cool-down (at most BREAKER_COOLDOWN_MAX_S), a run that succeeds
closes it. A trial whose run never ended (a crash) is stale after TRIAL_STALE_S and a new trial is allowed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0
JITTER = 0.2

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 300.0
BREAKER_COOLDOWN_MAX_S = 3600.0
TRIAL_STALE_S = 1800.0                     # = the runner's lease TTL: a trial older than this died with its worker
ALLOW, TRIAL, CIRCUIT_OPEN, TRIAL_RUNNING = "ALLOW", "TRIAL", "CIRCUIT_OPEN", "CIRCUIT_TRIAL_RUNNING"


def jitter_factor(key: str, attempt: int) -> float:
    u = int(hashlib.sha256(f"{key}|{attempt}".encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return 1.0 - JITTER + 2.0 * JITTER * u


def backoff_s(attempt: int, key: str) -> float:
    """The wait after failed attempt `attempt` (1-based) of the request `key`."""
    if attempt < 1:
        raise ValueError("attempts are counted from 1")
    return round(min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1)) * jitter_factor(key, attempt), 6)


@dataclass(frozen=True)
class Breaker:
    state: str = CLOSED
    failures: int = 0                      # consecutive failed runs
    opened_at: Optional[datetime] = None
    cooldown_s: float = 0.0
    trial_at: Optional[datetime] = None


def gate(b: Breaker, now: datetime) -> str:
    """ALLOW, TRIAL (the one run a half-open breaker lets through), CIRCUIT_OPEN or CIRCUIT_TRIAL_RUNNING."""
    if b.state == CLOSED:
        return ALLOW
    if b.state == OPEN:
        return TRIAL if now >= b.opened_at + timedelta(seconds=b.cooldown_s) else CIRCUIT_OPEN
    return TRIAL if b.trial_at is None or now >= b.trial_at + timedelta(seconds=TRIAL_STALE_S) else TRIAL_RUNNING


def start_trial(b: Breaker, now: datetime) -> Breaker:
    return replace(b, state=HALF_OPEN, trial_at=now)


def after_run(b: Breaker, failed: bool, now: datetime) -> Breaker:
    """The breaker after a run ended."""
    if not failed:
        return Breaker()
    n = b.failures + 1
    if b.state == HALF_OPEN:
        return Breaker(OPEN, n, now, min(BREAKER_COOLDOWN_MAX_S, max(BREAKER_COOLDOWN_S, b.cooldown_s * 2)), None)
    if n >= BREAKER_FAILURES:
        return Breaker(OPEN, n, now, BREAKER_COOLDOWN_S, None)
    return Breaker(CLOSED, n, None, 0.0, None)
