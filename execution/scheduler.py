"""
execution/scheduler.py — the executor loop (DESIGN_60 §9), modelled on setups/scheduler.py: one daemon thread per process, an injected
clock (tests never sleep), the shared app_leases table (lease `paper_executor`) so two processes never execute at once, a short cadence
inside the regular session and a long one outside it. The cycle itself (execution/service.py) is idle — no broker call, no write — while
the mode is MONITOR_ONLY and nothing is open. Never started in a test / harness process.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from setups.market_hours import describe, is_regular_session

log = logging.getLogger(__name__)
LEASE_NAME = "paper_executor"
LEASE_TTL_MIN = 2
MAX_SLEEP_S = 300.0
STARTUP_DELAY_S = 5.0
TEST_ENVS = ("test", "browser_test")


def _utc(dt: Optional[datetime]) -> datetime:
    d = dt or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class ExecutionScheduler:
    def __init__(self, service_fn: Callable[[], object], *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), interval_s: Optional[float] = None,
                 idle_interval_s: Optional[float] = None, lease_store_fn: Optional[Callable[[], object]] = None, startup_delay_s: float = STARTUP_DELAY_S):
        self.service_fn, self.clock = service_fn, clock
        self.interval_s = float(interval_s if interval_s is not None else config.EXECUTION_CYCLE_SECONDS)
        self.idle_interval_s = float(idle_interval_s if idle_interval_s is not None else config.EXECUTION_IDLE_CYCLE_SECONDS)
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
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    def due(self, now: datetime) -> bool:
        return self.next_at is None or now >= self.next_at

    def _interval(self, now: datetime) -> float:
        return self.interval_s if is_regular_session(now) else self.idle_interval_s

    def step(self, now: Optional[datetime] = None) -> Optional[dict]:
        """One scheduler decision: run a cycle when the interval has elapsed and the lease is ours."""
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
                    self.next_at = datetime.fromtimestamp(now.timestamp() + self._interval(now), tz=timezone.utc)
                    return {"result": "LEASE_HELD"}
            try:
                out = self.service_fn().run_cycle(now, "SCHEDULED")
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - never crash the server; report and try again at the next interval
                out = {"result": "ERROR", "error": type(exc).__name__}
                self.last_error = type(exc).__name__
                log.warning("paper executor cycle failed: %s", type(exc).__name__)
            self.runs += 1
            self.last_run_at = now
            self.last_result = out
            self.next_at = datetime.fromtimestamp(now.timestamp() + self._interval(now), tz=timezone.utc)
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
        if self.next_at is None:
            return 1.0
        return max(1.0, min(MAX_SLEEP_S, (self.next_at - now).total_seconds()))

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                log.warning("paper executor scheduler error: %s", type(exc).__name__)
            self._wake.wait(self.wait_seconds())
            self._wake.clear()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._state_lock:
            if self.running():
                return False                      # exactly one executor thread per process
            self._stop.clear()
            self._wake.clear()
            self.next_at = None
            self._thread = threading.Thread(target=self._loop, name="paper-executor", daemon=True)
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
        return {"running": self.running(), "owner": self.owner, "interval_s": self.interval_s, "idle_interval_s": self.idle_interval_s, "lease": LEASE_NAME, "market": describe(now),
                "last_run_at": self.last_run_at.isoformat(timespec="seconds") if self.last_run_at else None,
                "next_at": self.next_at.isoformat(timespec="seconds") if self.next_at else None, "runs": self.runs, "last_error": self.last_error,
                "last_result": {k: v for k, v in (self.last_result or {}).items() if k in ("result", "at", "trigger", "error")}}


_SCHEDULER: Optional[ExecutionScheduler] = None
_GUARD = threading.Lock()


def _default_service():
    from api.routes.trader_view import stream
    return stream().execution


def _lease_store():
    from forward.automation import default_store
    return default_store()


def get_scheduler() -> ExecutionScheduler:
    global _SCHEDULER
    with _GUARD:
        if _SCHEDULER is None:
            _SCHEDULER = ExecutionScheduler(_default_service, lease_store_fn=_lease_store)
        return _SCHEDULER


def start_if_enabled() -> bool:
    """Start the executor thread once per process — never in a test / harness process, never when disabled. The first cycle performs the
    startup reconciliation (no write before it); an idle MONITOR_ONLY process never contacts the broker."""
    if not config.EXECUTION_SCHEDULER_ENABLED or os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS:
        return False
    return get_scheduler().start()


def shutdown() -> None:
    global _SCHEDULER
    with _GUARD:
        s = _SCHEDULER
    if s is not None:
        s.stop()


def status() -> dict:
    s = _SCHEDULER
    if s is not None:
        return s.status()
    return {"running": False, "enabled": bool(config.EXECUTION_SCHEDULER_ENABLED), "lease": LEASE_NAME, "market": describe(datetime.now(timezone.utc))}
