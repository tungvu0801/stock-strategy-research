"""
evidence_refresh/service.py - one Stage 5.4.2 refresh run, with every dependency injected (tests never touch the network or sleep).

A run: mode -> one run at a time (in-process lock) -> outside the regular session -> evidence network enabled -> the shared
`evidence_refresher` lease -> plan -> for each symbol to build, sequentially: renew the lease, re-read that symbol's packets and build
only if it is STILL due (a manual Build or another process may have just added one), then the same engine call as
POST /api/evidence/packets. A refused source (ACCESS_DENIED) or two errors in a row stop the run; the next run is the next interval.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import config
from evidence_refresh import (A_BUILT, A_ERROR, A_FAILED, A_SKIPPED_FRESH, A_WOULD_BUILD, BUILD, CIRCUIT_FETCH_STATUSES, DISCLAIMER, LEASE_NAME,
                              MAX_CONSECUTIVE_ERRORS, MODES, R_COMPLETED, R_DRY_RUN, R_ERROR, R_LEASE_HELD, R_NOTHING_DUE, R_OFF,
                              R_RUN_IN_PROGRESS, R_SKIPPED_NETWORK, R_SKIPPED_SESSION, R_STOPPED, VERSION)
from evidence_refresh import policy as P

log = logging.getLogger(__name__)
LEASE_TTL_MIN = 15
PACKETS_PER_SYMBOL = 100                     # trader_view.research.PACKETS_READ: the window the gate itself reads
MAX_SYMBOLS = 50
_SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,9}$")   # the Build route's own ticker pattern (upper-cased)


def _utc(d: Optional[datetime] = None) -> datetime:
    d = d or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def mode() -> str:
    m = str(getattr(config, "EVIDENCE_REFRESH_MODE", "OFF") or "OFF").strip().upper()
    return m if m in MODES else "OFF"


def settings() -> P.RefreshSettings:
    from setups import EVIDENCE_MAX_AGE_DAYS                     # the READY rule's own limit, never a copy
    # refresh age >= 1 day: a packet built today is never rebuilt today. The store keeps one packet per packet_hash (UNIQUE; the hash covers
    # the request incl. its as_of_date, not the ids or created_at), so a same-day rebuild of unchanged evidence could only collide.
    return P.RefreshSettings(refresh_age_days=max(1, int(config.EVIDENCE_REFRESH_AGE_DAYS)), max_age_days=int(EVIDENCE_MAX_AGE_DAYS),
                             retry_hours=max(0.0, float(config.EVIDENCE_REFRESH_RETRY_HOURS)),
                             max_failures_per_day=max(1, int(config.EVIDENCE_REFRESH_MAX_FAILURES_PER_DAY)),
                             max_builds_per_run=max(0, int(config.EVIDENCE_REFRESH_MAX_BUILDS_PER_RUN)),
                             max_builds_per_day=max(0, int(config.EVIDENCE_REFRESH_MAX_BUILDS_PER_DAY)))


def default_symbols() -> List[str]:
    """The watchlist the setup evaluator watches (scanner.watchlist, the same file Trader View reads), validated, in order."""
    from scanner.watchlist import load_watchlist
    out: List[str] = []
    for s in load_watchlist():
        u = str(s).strip().upper()
        if _SYMBOL.match(u) and u not in out:
            out.append(u)
    return out[:MAX_SYMBOLS]


def default_rows(symbol: str) -> List[dict]:
    from evidence.store import EvidenceStore
    st = EvidenceStore()
    if not st.exists():
        return []
    return st.packets(limit=PACKETS_PER_SYMBOL, ticker=symbol)


def default_build(symbol: str, now: datetime) -> dict:
    """Exactly the engine call of POST /api/evidence/packets with only a ticker: default sources, lookback, adapters and store."""
    from evidence import engine as EN
    from evidence.store import EvidenceStore
    out = EN.build_packet(EN.EvidenceRequest(ticker=symbol), now_fn=lambda: datetime.now(timezone.utc), store=EvidenceStore())
    p = out["packet"]
    refused = sorted({f.get("host") for f in out.get("fetches", []) if f.get("status") in CIRCUIT_FETCH_STATUSES and f.get("host")})
    return {"status": p["status"], "packet_id": p["packet_id"], "failure_code": p.get("failure_code"), "strength": p.get("strength"),
            "n_items": p.get("n_items"), "n_failed_fetches": p.get("n_failed_fetches"), "refused_hosts": refused, "model_requests": 0}


def default_regular_session(now: datetime) -> bool:
    from setups.market_hours import is_regular_session
    return bool(is_regular_session(now))


def _lease_store():
    from forward.automation import default_store
    return default_store()


class EvidenceRefresher:
    def __init__(self, *, mode_fn: Callable[[], str] = mode, settings_fn: Callable[[], P.RefreshSettings] = settings,
                 symbols_fn: Callable[[], List[str]] = default_symbols, rows_fn: Callable[[str], List[dict]] = default_rows,
                 build_fn: Callable[[str, datetime], dict] = default_build, regular_session_fn: Callable[[datetime], bool] = default_regular_session,
                 network_fn: Callable[[], bool] = lambda: bool(config.EVIDENCE_NETWORK_ENABLED), lease_store_fn: Optional[Callable[[], object]] = _lease_store,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), sleep_fn: Optional[Callable[[float], bool]] = None,
                 spacing_s: Optional[float] = None, owner: Optional[str] = None):
        self.mode_fn, self.settings_fn, self.symbols_fn, self.rows_fn, self.build_fn = mode_fn, settings_fn, symbols_fn, rows_fn, build_fn
        self.regular_session_fn, self.network_fn, self.lease_store_fn, self.clock = regular_session_fn, network_fn, lease_store_fn, clock
        self._stop = threading.Event()
        self.sleep_fn = sleep_fn or (lambda s: self._stop.wait(s))     # True = stop requested while waiting
        self.spacing_s = float(spacing_s if spacing_s is not None else config.EVIDENCE_REFRESH_SPACING_SECONDS)
        self.owner = owner or f"evidence-refresh-{uuid.uuid4().hex[:12]}"
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=60)
        self.runs = 0
        self.last_run_at: Optional[str] = None
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None
        self.totals = {A_BUILT: 0, A_FAILED: 0, A_ERROR: 0, A_SKIPPED_FRESH: 0, A_WOULD_BUILD: 0}

    # ------------------------------------------------------------------------------------------------ read-only
    def plan(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        symbols = list(self.symbols_fn())
        rows: Dict[str, List[dict]] = {}
        errors: List[str] = []
        for s in symbols:
            try:
                rows[s] = list(self.rows_fn(s))
            except Exception as exc:  # noqa: BLE001 - an unreadable store is "no packets known", never a crash
                rows[s] = []
                errors.append(f"{s}: {type(exc).__name__}")
        out = P.plan(symbols, rows, now, self.settings_fn())
        out["read_errors"] = errors
        return out

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------------------------------------ one run
    def _record(self, at: datetime, symbol: str, outcome: str, **kw) -> dict:
        row = {"at": _utc(at).isoformat(timespec="seconds"), "symbol": symbol, "outcome": outcome, **kw}
        self.history.appendleft(row)
        self.totals[outcome] = self.totals.get(outcome, 0) + 1
        return row

    def _finish(self, now: datetime, result: dict) -> dict:
        self.runs += 1
        self.last_run_at = _utc(now).isoformat(timespec="seconds")
        self.last_result = result
        return result

    def run_once(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        m = self.mode_fn()
        if m == "OFF":
            return {"result": R_OFF}
        if not self._lock.acquire(blocking=False):
            return {"result": R_RUN_IN_PROGRESS}
        lease = None
        try:
            if self.regular_session_fn(now):
                return self._finish(now, {"result": R_SKIPPED_SESSION, "mode": m})
            if not self.network_fn():
                return self._finish(now, {"result": R_SKIPPED_NETWORK, "mode": m})
            if self.lease_store_fn is not None:
                lease = self.lease_store_fn()
                if not lease.acquire(self.owner, now, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):
                    lease = None
                    return self._finish(now, {"result": R_LEASE_HELD, "mode": m})
            p = self.plan(now)
            if not p["to_build"]:
                return self._finish(now, {"result": R_NOTHING_DUE, "mode": m, "wanted": p["wanted"], "budget": p["budget"]})
            if m == "DRY_RUN":
                for s in p["to_build"]:
                    self._record(now, s, A_WOULD_BUILD)
                log.info("evidence refresh (dry run): would build %s", ", ".join(p["to_build"]))
                return self._finish(now, {"result": R_DRY_RUN, "mode": m, "would_build": list(p["to_build"])})
            return self._finish(now, self._build_all(p["to_build"], lease, m))
        except Exception as exc:  # noqa: BLE001 - a refresh run never crashes the server; the next run tries again
            self.last_error = type(exc).__name__
            log.warning("evidence refresh run failed: %s", type(exc).__name__)
            return self._finish(now, {"result": R_ERROR, "error": type(exc).__name__})
        finally:
            if lease is not None:
                try:
                    lease.release(self.owner, name=LEASE_NAME)
                except Exception:  # noqa: BLE001 - the lease expires by itself
                    pass
            self._lock.release()

    def _build_all(self, symbols: List[str], lease, m: str) -> dict:
        attempts: List[dict] = []
        stop_reason = None
        errors_in_row = 0
        s = self.settings_fn()
        for i, sym in enumerate(symbols):
            if i and self.spacing_s > 0 and self.sleep_fn(self.spacing_s):
                stop_reason = "SHUTDOWN"
                break
            if self._stop.is_set():
                stop_reason = "SHUTDOWN"
                break
            t = _utc(self.clock())
            if self.regular_session_fn(t):
                stop_reason = "SESSION_OPENED"
                break
            if lease is not None and not lease.acquire(self.owner, t, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):
                stop_reason = "LEASE_LOST"
                break
            try:
                again = P.symbol_state(sym, self.rows_fn(sym), t, s)  # duplicate prevention: is it STILL due right now?
            except Exception as exc:  # noqa: BLE001 - cannot tell whether it is still due: do not build it this run
                attempts.append(self._record(t, sym, A_ERROR, error=f"recheck {type(exc).__name__}"))
                continue
            if again["decision"] != BUILD:
                attempts.append(self._record(t, sym, A_SKIPPED_FRESH, state=again["state"], packet_id=again["packet_id"]))
                continue
            try:
                out = self.build_fn(sym, t)
            except Exception as exc:  # noqa: BLE001 - one symbol's error never stops the others (until two in a row)
                errors_in_row += 1
                self.last_error = type(exc).__name__
                attempts.append(self._record(t, sym, A_ERROR, error=type(exc).__name__))
                log.warning("evidence refresh: %s build raised %s", sym, type(exc).__name__)
                if errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                    stop_reason = "CONSECUTIVE_ERRORS"
                    break
                continue
            errors_in_row = 0
            ok = out.get("status") == "COMPLETED"
            attempts.append(self._record(t, sym, A_BUILT if ok else A_FAILED, packet_id=out.get("packet_id"), failure_code=out.get("failure_code"),
                                         strength=out.get("strength"), refused_hosts=out.get("refused_hosts") or []))
            log.info("evidence refresh: %s %s packet %s%s", sym, "built" if ok else "FAILED", out.get("packet_id"),
                     f" ({out.get('failure_code')})" if out.get("failure_code") else "")
            if out.get("refused_hosts"):
                stop_reason = "SOURCE_REFUSED"
                log.warning("evidence refresh: %s refused the requests; stopping this run", ", ".join(out["refused_hosts"]))
                break
        return {"result": R_STOPPED if stop_reason else R_COMPLETED, "mode": m, "stop_reason": stop_reason, "attempts": attempts}

    def status(self) -> dict:
        return {"version": VERSION, "mode": self.mode_fn(), "owner": self.owner, "lease": LEASE_NAME, "runs": self.runs,
                "last_run_at": self.last_run_at, "last_result": self.last_result, "last_error": self.last_error, "totals": dict(self.totals),
                "history": list(self.history)[:20], "spacing_s": self.spacing_s, "disclaimer": DISCLAIMER}
