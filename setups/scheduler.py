"""
setups/scheduler.py — the market-hours evaluator loop (DESIGN_58 §9), modelled on forward.automation.Scheduler: one daemon thread per
process, an injected clock (tests never sleep), the shared app_leases table so two processes never evaluate at once, and no loop
outside the regular session (it sleeps until the next open, re-reading the clock at least every 15 minutes). The state engine does not
depend on it: subscribe / refresh / manual evaluation work when the thread is not running.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from setups import EVAL_INTERVAL_SECONDS
from setups.market_hours import describe, is_regular_session, seconds_until_open

log = logging.getLogger(__name__)
LEASE_NAME = "setup_evaluator"
LEASE_TTL_MIN = 2
MAX_SLEEP_S = 900.0
STARTUP_DELAY_S = 5.0


def _utc(dt: Optional[datetime]) -> datetime:
    d = dt or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class SetupScheduler:
    def __init__(self, service_fn: Callable[[], object], *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 interval_s: Optional[float] = None, market_hours_only: Optional[bool] = None, lease_store_fn: Optional[Callable[[], object]] = None,
                 startup_delay_s: float = STARTUP_DELAY_S):
        self.service_fn, self.clock = service_fn, clock
        self.interval_s = float(interval_s if interval_s is not None else config.SETUP_EVAL_INTERVAL_SECONDS or EVAL_INTERVAL_SECONDS)
        self.market_hours_only = bool(config.SETUP_MARKET_HOURS_ONLY if market_hours_only is None else market_hours_only)
        self.lease_store_fn = lease_store_fn
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
        self.skipped_outside_hours = 0
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    # ---- decision (pure given the clock; the thread just calls step) ----------------------------------------------------
    def due(self, now: datetime) -> bool:
        return self.next_at is None or now >= self.next_at

    def step(self, now: Optional[datetime] = None) -> Optional[dict]:
        """One scheduler decision: evaluate everything in scope if the session is open and the interval has elapsed."""
        now = _utc(now or self.clock())
        if self.market_hours_only and not is_regular_session(now):
            self.skipped_outside_hours += 1
            self.next_at = None
            return None
        if not self.due(now):
            return None
        if not self._lock.acquire(blocking=False):
            return {"result": "RUN_IN_PROGRESS"}
        lease = None
        try:
            if self.lease_store_fn is not None:
                lease = self.lease_store_fn()
                if not lease.acquire(self.owner, now, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):
                    self.next_at = now.__class__.fromtimestamp(now.timestamp() + self.interval_s, tz=timezone.utc)
                    return {"result": "LEASE_HELD"}
            try:
                out = self.service_fn().evaluate_all(now, "SCHEDULED")
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - never crash the server; report and try again at the next interval
                out = {"result": "ERROR", "error": type(exc).__name__}
                self.last_error = type(exc).__name__
                log.warning("setup evaluator run failed: %s", type(exc).__name__)
            self.runs += 1
            self.last_run_at = now
            self.last_result = out
            self.next_at = datetime.fromtimestamp(now.timestamp() + self.interval_s, tz=timezone.utc)
            try:
                self.service_fn().store.touch_control("market_hours_scheduler", now, run=True, result="RUN" if "error" not in out else "ERROR", error=out.get("error"))
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler control status not saved (%s)", type(exc).__name__)
            return out
        finally:
            if lease is not None:
                try:
                    lease.release(self.owner, name=LEASE_NAME)
                except Exception:  # noqa: BLE001 - the lease expires by itself
                    pass
            self._lock.release()

    def wait_seconds(self, now: Optional[datetime] = None) -> float:
        now = _utc(now or self.clock())
        if self.market_hours_only and not is_regular_session(now):
            return seconds_until_open(now, MAX_SLEEP_S)
        if self.next_at is None:
            return 1.0
        return max(1.0, min(MAX_SLEEP_S, (self.next_at - now).total_seconds()))

    # ---- thread ---------------------------------------------------------------------------------------------------------
    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                log.warning("setup scheduler error: %s", type(exc).__name__)
            self._wake.wait(self.wait_seconds())
            self._wake.clear()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._state_lock:
            if self.running():
                return False                      # exactly one evaluator thread per process
            self._stop.clear()
            self._wake.clear()
            self.next_at = None
            self._thread = threading.Thread(target=self._loop, name="setup-state-evaluator", daemon=True)
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
        return {"running": self.running(), "owner": self.owner, "interval_s": self.interval_s, "market_hours_only": self.market_hours_only, "market": describe(now),
                "last_run_at": self.last_run_at.isoformat(timespec="seconds") if self.last_run_at else None,
                "next_at": self.next_at.isoformat(timespec="seconds") if self.next_at else None, "runs": self.runs,
                "skipped_outside_hours": self.skipped_outside_hours, "last_error": self.last_error, "last_result": self.last_result}


# ---------------------------------------------------------------- process singleton (started from the app lifespan) ----------
_SCHEDULER: Optional[SetupScheduler] = None
_GUARD = threading.Lock()
TEST_ENVS = ("test", "browser_test")


def _default_service():
    from api.routes.trader_view import stream
    return stream().setups


def _lease_store():
    from forward.automation import default_store
    return default_store()


def get_scheduler() -> SetupScheduler:
    global _SCHEDULER
    with _GUARD:
        if _SCHEDULER is None:
            _SCHEDULER = SetupScheduler(_default_service, lease_store_fn=_lease_store)
        return _SCHEDULER


def start_if_enabled() -> bool:
    """Start the market-hours evaluator thread once per process — never in a test / harness process, never when disabled."""
    if not config.SETUP_EVALUATOR_ENABLED or os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS:
        return False
    try:
        _default_service().startup()
    except Exception as exc:  # noqa: BLE001 - the restore summary is informational
        log.warning("setup startup record failed (%s)", type(exc).__name__)
    return get_scheduler().start()


def shutdown() -> None:
    global _SCHEDULER
    with _GUARD:
        s = _SCHEDULER
    if s is not None:
        s.stop()


def status() -> dict:
    s = _SCHEDULER
    return s.status() if s is not None else {"running": False, "enabled": bool(config.SETUP_EVALUATOR_ENABLED), "market": describe(datetime.now(timezone.utc))}
