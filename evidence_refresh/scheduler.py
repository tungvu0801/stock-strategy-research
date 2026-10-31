"""
evidence_refresh/scheduler.py - the ONE Stage 5.4.2 refresh thread per process: default OFF (config.EVIDENCE_REFRESH_MODE), never
started in a test / harness / pytest process, a startup delay so the start-up work (account refresh, preflight) comes first, then one
run_once every EVIDENCE_REFRESH_CHECK_SECONDS. Stopping it interrupts the spacing wait between two builds; a build already running
finishes by its own time limit (evidence LIMITS build_s).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

import config
from evidence_refresh import LEASE_NAME, VERSION
from evidence_refresh.service import EvidenceRefresher, mode

log = logging.getLogger(__name__)
TEST_ENVS = ("test", "browser_test")
_GUARD = threading.Lock()
_SCHEDULER: Optional["EvidenceRefreshScheduler"] = None


class EvidenceRefreshScheduler:
    def __init__(self, refresher: EvidenceRefresher, *, interval_s: Optional[float] = None, startup_delay_s: Optional[float] = None):
        self.refresher = refresher
        self.interval_s = float(interval_s if interval_s is not None else config.EVIDENCE_REFRESH_CHECK_SECONDS)
        self.startup_delay_s = float(startup_delay_s if startup_delay_s is not None else config.EVIDENCE_REFRESH_STARTUP_DELAY_SECONDS)
        self._thread: Optional[threading.Thread] = None
        self._stop = refresher._stop                     # one event: stopping the loop also interrupts the refresher's spacing wait
        self.next_at: Optional[str] = None

    def _loop(self) -> None:
        if self.startup_delay_s and self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                out = self.refresher.run_once()
                if out.get("result") not in ("NOTHING_DUE", "SKIPPED_REGULAR_SESSION"):
                    log.info("evidence refresh run: %s", {k: v for k, v in out.items() if k != "attempts"})
            except Exception as exc:  # noqa: BLE001
                self.refresher.last_error = type(exc).__name__
                log.warning("evidence refresh scheduler error: %s", type(exc).__name__)
            self.next_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + self.interval_s, tz=timezone.utc).isoformat(timespec="seconds")
            self._stop.wait(self.interval_s)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="evidence-refresher", daemon=True)
        self._thread.start()
        log.info("evidence refresh: loop running (mode %s, lease %s, every %.0f s, outside the regular session only; zero model calls)",
                 mode(), LEASE_NAME, self.interval_s)
        return True

    def stop(self, timeout: float = 15.0) -> bool:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
        return not self.running()

    def status(self) -> dict:
        return {"running": self.running(), "interval_s": self.interval_s, "startup_delay_s": self.startup_delay_s, "next_at": self.next_at,
                **self.refresher.status()}


def get_scheduler() -> EvidenceRefreshScheduler:
    global _SCHEDULER
    with _GUARD:
        if _SCHEDULER is None:
            _SCHEDULER = EvidenceRefreshScheduler(EvidenceRefresher())
        return _SCHEDULER


def start_if_enabled() -> bool:
    """Start the refresh thread once per process - only when EVIDENCE_REFRESH_MODE is DRY_RUN or ON, never in a test / harness process."""
    if mode() == "OFF" or os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS or os.environ.get("PYTEST_CURRENT_TEST"):
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
    return {"running": False, "version": VERSION, "mode": mode(), "lease": LEASE_NAME}
