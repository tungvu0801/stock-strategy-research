"""
account_refresh/scheduler.py - the ONE refresh loop (DESIGN_601 sections 2, 3, 10): one daemon thread per process, the shared app_leases
lease `alpaca_account_refresher`, an injected clock (tests never sleep), 60 s inside the regular session and 300 s outside it, an
immediate STARTUP refresh, and a safety evaluation after every refresh. Never started in a test / harness process or without a
configured paper broker. It only reads.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from account_refresh import LEASE_NAME
from setups.market_hours import describe, is_regular_session

log = logging.getLogger(__name__)
LEASE_TTL_MIN = 2
MAX_SLEEP_S = 300.0
TEST_ENVS = ("test", "browser_test")


def _utc(dt: Optional[datetime]) -> datetime:
    d = dt or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class AccountRefreshScheduler:
    def __init__(self, service_fn: Callable[[], object], *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), interval_s: Optional[float] = None,
                 idle_interval_s: Optional[float] = None, lease_store_fn: Optional[Callable[[], object]] = None, after_refresh: Optional[Callable[[], None]] = None,
                 startup_delay_s: float = 0.0):
        self.service_fn, self.clock = service_fn, clock
        self.interval_s = float(interval_s if interval_s is not None else config.ACCOUNT_REFRESH_SECONDS)
        self.idle_interval_s = float(idle_interval_s if idle_interval_s is not None else config.ACCOUNT_REFRESH_IDLE_SECONDS)
        self.lease_store_fn = lease_store_fn
        self.after_refresh = after_refresh
        self.startup_delay_s = startup_delay_s
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_run_at: Optional[datetime] = None
        self.next_at: Optional[datetime] = None
        self.runs = 0
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    def interval(self, now: datetime) -> float:
        return self.interval_s if is_regular_session(now) else self.idle_interval_s

    def due(self, now: datetime) -> bool:
        return self.next_at is None or now >= self.next_at

    def step(self, now: Optional[datetime] = None) -> Optional[dict]:
        now = _utc(now or self.clock())
        if not self.due(now):
            return None
        if not self._lock.acquire(blocking=False):
            return {"result": "RUN_IN_PROGRESS"}
        lease = None
        try:
            if self.lease_store_fn is not None:
                lease = self.lease_store_fn()
                if not lease.acquire(self.owner, now, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):
                    self.next_at = datetime.fromtimestamp(now.timestamp() + self.interval(now), tz=timezone.utc)
                    return {"result": "LEASE_HELD"}
            trigger = "STARTUP" if self.runs == 0 else "SCHEDULED"
            try:
                out = self.service_fn().refresh(now, trigger)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - never crash the server; report and try again at the next interval
                out = {"result": "ERROR", "error": type(exc).__name__}
                self.last_error = type(exc).__name__
                log.warning("account refresh run failed: %s", type(exc).__name__)
            if self.after_refresh is not None:
                try:
                    self.after_refresh()                                    # the gate sees the new freshness at once
                except Exception as exc:  # noqa: BLE001
                    log.warning("safety evaluation after the account refresh failed (%s)", type(exc).__name__)
            self.runs += 1
            self.last_run_at = now
            self.last_result = out
            self.next_at = datetime.fromtimestamp(now.timestamp() + self.interval(now), tz=timezone.utc)
            return out
        finally:
            if lease is not None:
                try:
                    lease.release(self.owner, name=LEASE_NAME)
                except Exception:  # noqa: BLE001
                    pass
            self._lock.release()

    def wait_seconds(self, now: Optional[datetime] = None) -> float:
        now = _utc(now or self.clock())
        if self.next_at is None:
            return 1.0
        return max(1.0, min(MAX_SLEEP_S, (self.next_at - now).total_seconds()))

    def _loop(self) -> None:
        if self.startup_delay_s and self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                log.warning("account refresh scheduler error: %s", type(exc).__name__)
            self._wake.wait(self.wait_seconds())
            self._wake.clear()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._state_lock:
            if self.running():
                return False
            self._stop.clear()
            self._wake.clear()
            self.next_at = None
            self._thread = threading.Thread(target=self._loop, name="paper-account-refresher", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 15.0) -> bool:
        with self._state_lock:
            t = self._thread
            self._stop.set()
            self._wake.set()
        if t is not None:
            t.join(timeout)
        return not (t is not None and t.is_alive())

    def status(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        return {"running": self.running(), "owner": self.owner, "lease": LEASE_NAME, "interval_s": self.interval_s, "idle_interval_s": self.idle_interval_s,
                "market": describe(now), "runs": self.runs, "last_run_at": self.last_run_at.isoformat(timespec="seconds") if self.last_run_at else None,
                "next_at": self.next_at.isoformat(timespec="seconds") if self.next_at else None, "last_error": self.last_error,
                "last_result": {k: v for k, v in (self.last_result or {}).items() if k in ("result", "trigger", "error_code", "read_attempts")}}


_SCHEDULER: Optional[AccountRefreshScheduler] = None
_GUARD = threading.Lock()


def _default_service():
    from api.routes.trader_view import stream
    return stream().account_refresh


def _evaluate_safety() -> None:
    from api.routes.trader_view import stream
    stream().safety.evaluate("SCHEDULED")


def _lease_store():
    from forward.automation import default_store
    return default_store()


def get_scheduler() -> AccountRefreshScheduler:
    global _SCHEDULER
    with _GUARD:
        if _SCHEDULER is None:
            _SCHEDULER = AccountRefreshScheduler(_default_service, lease_store_fn=_lease_store, after_refresh=_evaluate_safety)
        return _SCHEDULER


def start_if_enabled() -> bool:
    """Start the refresh thread once per process - never in a test / harness / pytest process, never when disabled or unconfigured.
    Its first step is the STARTUP refresh (no delay); until it succeeds the account data reads STARTING and the gate stays paused."""
    if not config.ACCOUNT_REFRESH_ENABLED or os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        if not _default_service().enabled():
            log.info("account refresh: no paper broker configured - loop not started")
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("account refresh: service unavailable (%s)", type(exc).__name__)
        return False
    return get_scheduler().start()


def shutdown() -> None:
    with _GUARD:
        s = _SCHEDULER
    if s is not None:
        s.stop()


def status() -> dict:
    s = _SCHEDULER
    if s is not None:
        return s.status()
    return {"running": False, "enabled": bool(config.ACCOUNT_REFRESH_ENABLED), "lease": LEASE_NAME, "market": describe(datetime.now(timezone.utc))}
