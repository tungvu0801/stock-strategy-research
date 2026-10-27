"""
autorun/facts.py - the READ-ONLY fact gatherer of the Stage 6.10A auto-run policy (autorun/policy.py). Sources: the shared database (read-only
connections), the executor's in-memory state (plain attribute reads - never its lock), the account refresh's persisted snapshot and freshness,
the cached unified portfolio (refresh=False: never a Robinhood read) and the loop threads' status. No broker call, no write, no evaluation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import config
from autorun import STARTUP_REASON, SUPPORTED_CONTROLS_VERSIONS
from autorun import policy as POL
from autorun import session as SES

UTC = timezone.utc
AMBIGUOUS_AFTER_S = 120                 # an attempted entry / exit without a broker order id this long after its POST is ambiguous


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - an unreadable source is a fact too (reported as such by the caller)
        return default


def fresh_clock(clock: Any, now: datetime, max_age_s: float = 600) -> Optional[dict]:
    """The executor's cached broker clock only while it describes this moment (read <= 10 min ago, same New York date); else None and the static
    calendar decides - a stale 'closed' reading never relabels a later regular session."""
    if not isinstance(clock, dict):
        return None
    ts = POL._parse(clock.get("timestamp"))
    if ts is None or abs((now - ts).total_seconds()) > max_age_s:
        return None
    a, b = SES.classify(ts).get("trading_date"), SES.classify(now).get("trading_date")
    return clock if a == b else None


def gather(x, st, store, mem: Dict[str, Any], now: datetime, *, lease: Dict[str, Any], process_start: Optional[datetime],
           owned_since: Optional[datetime] = None) -> Dict[str, Any]:
    """`x` the ExecutionService, `st` the Trader View stream, `store` the AutoRunStore, `mem` the supervisor's memory (updated here).
    `owned_since`: when this process took the instance lease (another executor's cycles are foreign only after it)."""
    from execution import COID_PREFIX
    from execution import exposure_gate as G
    f: Dict[str, Any] = {"settings": POL.settings_from(config), "lease": lease, "process_start": _iso(process_start)}
    try:
        store.query("SELECT 1 AS ok")
        m = x.store.mode()
        f["db"] = {"readable": True, "error": None}
        mem["last_mode"] = m.get("mode")
        if m.get("mode") != "PAPER_AUTO":
            mem.pop("db_failure", None)                                    # nothing armed: nothing left to disarm
    except Exception as exc:  # noqa: BLE001
        m = {"mode": "UNKNOWN", "changed_at": None, "persisted": False}
        f["db"] = {"readable": False, "error": type(exc).__name__}
        if mem.get("last_mode") == "PAPER_AUTO":                           # an outage while armed: disarmed as soon as the mode can be written
            mem.setdefault("db_failure", {"since": _iso(now), "error": type(exc).__name__})
    ev = _safe(lambda: x.store.mode_events(1), []) or []
    f["mode"] = {"mode": m.get("mode"), "changed_at": m.get("changed_at"), "changed_by": m.get("changed_by"), "event_id": ev[0].get("event_id") if ev else None}
    f["broker_configured"] = bool(_safe(lambda: (getattr(x, "_broker", None) or x.broker_fn()).configured(), False))   # never constructs the executor's own client
    from forward_capture import project as FP
    env = FP.environment(getattr(x, "environment_cache", None) or {})        # the allowlisted proof fields (never the host string)
    checked = env.get("checked_at")
    if checked and checked != mem.get("proof_checked_at"):               # a NEW proof attempt since the last tick
        mem["proof_checked_at"] = checked
        mem["proof_failures"] = 0 if env.get("paper") else int(mem.get("proof_failures") or 0) + 1
    f["proof"] = POL.proof_state(env, int(mem.get("proof_failures") or 0))
    refresher = getattr(st, "account_refresh", None)
    fr = _safe(lambda: refresher.freshness(now), {}) or {}
    snap = _safe(lambda: refresher.store.snapshot(), None)
    f["account"] = {"source": fr.get("source") or (snap or {}).get("source"), "status": fr.get("status") or "UNAVAILABLE", "consecutive_failures": fr.get("consecutive_failures"),
                    "last_attempt_at": fr.get("last_attempt_at"), "age_s": fr.get("age_seconds")}
    foreign = []
    for o in ((snap or {}).get("open_orders") or []):
        coid = str(o.get("client_order_id") or "")
        if coid.startswith(COID_PREFIX) and _safe(lambda c=coid: x.store.intent_by_coid(c), "ERR") is None:
            foreign.append({"symbol": o.get("symbol"), "client_order_id": coid, "status": o.get("status")})
    f["foreign"] = {"orders": foreign, "as_of": (snap or {}).get("refreshed_at")}
    sstore = getattr(getattr(st, "safety", None), "store", None)
    state = _safe(lambda: sstore.state(), None)
    last = _safe(lambda: sstore.last_evaluation(), None) or {}
    f["safety"] = {"kill_active": bool(getattr(state, "kill_active", False)), "pause_active": bool(getattr(state, "pause_active", False)), "state": last.get("state"),
                   "allowed_new_entry": bool(last.get("allowed_new_entry")), "evaluated_at": last.get("evaluated_at"), "readable": state is not None}
    try:
        from execution.scheduler import status as exec_status
        sched = exec_status()
    except Exception:  # noqa: BLE001
        sched = {"running": False}
    opens = _safe(lambda: x.store.open_intents(), []) or []
    cutoff = _iso(now - timedelta(seconds=AMBIGUOUS_AFTER_S))
    in_cycle = bool(sched.get("in_cycle")) or getattr(x, "progress", None) is not None   # a POST in flight is not an ambiguity: its answer comes in the cycle
    f["intents"] = {"open": len(opens), "reconciliation_required": sum(1 for i in opens if i.state == "RECONCILIATION_REQUIRED"),
                    "attempted_unresolved": 0 if in_cycle else sum(1 for i in opens if i.state != "RECONCILIATION_REQUIRED" and int(i.attempt_count or 0) >= 1
                                                                   and not i.broker_order_id and str(i.submitted_at or "") < cutoff)}
    pos = _safe(lambda: x.store.positions(open_only=True), []) or []
    f["positions"] = {"open": len(pos), "attention": sum(1 for p in pos if p.state == "ATTENTION")}
    f["recon"] = {"reconciled": bool(getattr(x, "_broker_positions_at", None)), "healthy": bool(getattr(x, "recon_healthy", True)),
                  "reasons": list(getattr(x, "recon_reasons", []) or [])[:10], "at": getattr(x, "_broker_positions_at", None)}
    gate = G.from_config(config)
    f["gate"] = {"version": G.VERSION, "supported": G.VERSION in SUPPORTED_CONTROLS_VERSIONS, "problems": gate.problems() if gate.enabled else [], "enabled": gate.enabled}
    f["scheduler_enabled"] = bool(getattr(config, "EXECUTION_SCHEDULER_ENABLED", True))
    f["worker"] = POL.worker_liveness(sched, now)
    if f["worker"]["state"] == "ERROR":                                     # counted per failed CYCLE (a new run), not per supervisor tick
        run = sched.get("last_run_at") or (sched.get("last_result") or {}).get("at")
        if run != mem.get("worker_error_run"):
            mem["worker_error_run"] = run
            mem["worker_error_ticks"] = int(mem.get("worker_error_ticks") or 0) + 1
    else:
        mem["worker_error_ticks"], mem["worker_error_run"] = 0, None
    try:
        from forward_validation.scheduler import status as fv_status
        f["observer"] = {"running": bool(fv_status().get("running"))}
    except Exception:  # noqa: BLE001
        f["observer"] = {"running": False}
    f["clock"] = fresh_clock(getattr(x, "_clock_cache", None), now)
    f["session"] = SES.classify(now, f["clock"])
    if f["session"]["session"] == "REGULAR" and f["account"]["status"] != "FRESH":
        mem.setdefault("equity_unavailable_since", _iso(now))
    else:
        mem.pop("equity_unavailable_since", None)
    from forward_validation import CRITICAL_CODES
    crit = _safe(lambda: store.query("SELECT DISTINCT code FROM forward_validation_events WHERE kind = 'ATTENTION_RAISED'"), []) or []
    f["attention"] = {"critical": sorted(r["code"] for r in crit if r["code"] in CRITICAL_CODES)}
    ufn = getattr(st, "unified_portfolio", None)
    u = _safe(lambda: ufn(refresh=False), None) if callable(ufn) else None
    f["external"] = {"state": (u or {}).get("state") or "UNAVAILABLE", "sources": [{"source": s.get("source"), "state": s.get("state")} for s in ((u or {}).get("sources") or [])]}
    from forward_capture.capture import trading_date
    td = trading_date(_iso(now))
    n = _safe(lambda: store.query("SELECT count(*) AS n FROM forward_capture_faults WHERE trading_date = ?", (td,)), []) or []
    f["capture"] = {"faults_today": int(n[0]["n"]) if n else 0}
    f["trading_date"] = td
    since = _iso(max(d for d in (process_start, owned_since) if d is not None)) if (process_start or owned_since) else None
    runs = _safe(lambda: store.query("SELECT DISTINCT owner FROM execution_runs WHERE started_at >= ? AND owner IS NOT NULL", (since,)), []) if since else []
    f["foreign_executor"] = sorted(r["owner"] for r in (runs or []) if r["owner"] != getattr(x, "owner", None))   # cycles another executor ran since this owned it
    from forward_validation.service import ny_window
    lo, hi = ny_window(td) if td else (None, None)
    rows = _safe(lambda: store.query("SELECT at, actor, reason FROM execution_mode_events WHERE at >= ? AND at < ? AND from_mode = 'PAPER_AUTO' AND to_mode = 'MONITOR_ONLY' "
                                     "ORDER BY at, rowid", (lo, hi)), []) if lo else []
    f["disarms_today"] = [{"at": r["at"], "actor": r["actor"]} for r in (rows or []) if not str(r.get("reason") or "").startswith(STARTUP_REASON)]
    lc, op = SES.last_close(now), SES.open_of(now)
    f["last_close"], f["open_at"] = _iso(lc), _iso(op)
    return f
