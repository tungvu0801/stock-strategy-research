"""
autorun/scheduler.py - the Stage 6.10A supervisor thread 'paper-autorun' (one per process; never in a test / harness / pytest process; off with
AUTORUN_SUPERVISOR_ENABLED=false). Cadence: every 30 s in the regular session, every 60 s otherwise - database reads only, no broker call, so
the closed-market cadence costs no broker or API traffic. Each tick renews the single-instance lease first (TTL 3 min: a live owner always
renews it, a crashed one's lapses within 3 min). stop() ends the thread; release_lease() frees the lease at the end of the shutdown. There is no process restart loop here: a dead
worker is reported (and disarms Paper Auto), never restarted blindly.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from autorun import INSTANCE_LEASE, SUPERVISOR_IDLE_S, SUPERVISOR_REGULAR_S, instance_id
from autorun.policy import settings_from
from setups.market_hours import is_regular_session

log = logging.getLogger(__name__)
UTC = timezone.utc
STARTUP_DELAY_S = 3.0


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


class AutoRunScheduler:
    def __init__(self, service_fn: Callable[[], object], *, clock: Callable[[], datetime] = lambda: datetime.now(UTC), startup_delay_s: float = STARTUP_DELAY_S):
        self.service_fn, self.clock = service_fn, clock
        self.startup_delay_s = startup_delay_s
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self.runs = 0
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    def interval(self, now: datetime) -> float:
        return SUPERVISOR_REGULAR_S if is_regular_session(now) else SUPERVISOR_IDLE_S

    def step(self, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or self.clock()
        try:
            out = self.service_fn().tick(now)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - the supervisor never crashes the server; the next tick tries again
            out = {"result": "ERROR", "error": type(exc).__name__}
            self.last_error = type(exc).__name__
            log.warning("auto-run supervisor tick failed: %s", type(exc).__name__)
        self.runs += 1
        self.last_run_at, self.last_result = now, out
        return out

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.interval(self.clock()))

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._state_lock:
            if self.running():
                return False                                   # exactly one supervisor per process
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="paper-autorun", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 15.0) -> bool:
        with self._state_lock:
            t = self._thread
            self._stop.set()
        if t is not None:
            t.join(timeout)
        return not (t is not None and t.is_alive())          # the lease is released last, by release_lease(), after the executor stopped

    def status(self) -> dict:
        return {"running": self.running(), "instance": instance_id(), "lease": INSTANCE_LEASE, "interval_regular_s": SUPERVISOR_REGULAR_S, "interval_idle_s": SUPERVISOR_IDLE_S,
                "runs": self.runs, "last_run_at": _iso(self.last_run_at), "last_error": self.last_error,
                "last_result": {k: v for k, v in (self.last_result or {}).items() if k in ("result", "lease", "preflight", "at")}}


_SERVICE = {"instance": None}
_SCHEDULER: Optional[AutoRunScheduler] = None
_GUARD = threading.Lock()


def service():
    from api.routes.trader_view import stream
    from autorun.service import AutoRunService
    with _GUARD:
        if _SERVICE["instance"] is None:
            _SERVICE["instance"] = AutoRunService(stream_fn=stream)
        return _SERVICE["instance"]


def get_scheduler() -> AutoRunScheduler:
    global _SCHEDULER
    with _GUARD:
        if _SCHEDULER is None:
            _SCHEDULER = AutoRunScheduler(service)
        return _SCHEDULER


def start_if_enabled() -> bool:
    from autorun.service import in_test_process
    if in_test_process():
        return False
    if not settings_from(config)["supervisor_enabled"]:
        from autorun.service import STARTUP
        if STARTUP.get("pending"):
            log.error("auto-run: the startup step was refused (%s) and AUTORUN_SUPERVISOR_ENABLED=false: the executor stays stopped", STARTUP.get("code"))
        return False
    return get_scheduler().start()


def shutdown() -> None:
    s = _SCHEDULER
    if s is not None:
        s.stop()


def release_lease() -> None:
    """The very end of the shutdown, after the executor and the observer stopped: release the instance lease this process holds, so a successor
    takes over at once and never sees this process's last cycle as a foreign executor."""
    from autorun.service import STARTUP
    if not STARTUP.get("lease_taken"):
        return
    try:
        from forward.automation import default_store
        default_store().release(instance_id(), name=INSTANCE_LEASE)
        STARTUP["lease_taken"] = False
    except Exception as exc:  # noqa: BLE001 - the lease expires by itself
        log.warning("auto-run: lease not released at shutdown (%s)", type(exc).__name__)


def status() -> dict:
    s = _SCHEDULER
    if s is not None:
        return s.status()
    return {"running": False, "instance": instance_id(), "lease": INSTANCE_LEASE, "enabled": settings_from(config)["supervisor_enabled"]}


def current_service():
    """The supervisor's service when it was created in this process (display only); None otherwise."""
    return _SERVICE["instance"]
