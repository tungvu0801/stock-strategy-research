"""
execution/activity.py - the Bot Activity view of the TRADE dashboard (DESIGN_68_ACCOUNT_POSITIONS_BOT_ACTIVITY.md section 3).

One read-only payload, assembled ONLY from what the executor already records:
  worker      the scheduler thread's heartbeat (every loop decision, due or not), the cycle in progress, the last start / completion, the next run
              -> RUNNING | IDLE | STANDBY | STARTING | STALE | ERROR | STOPPED | DISABLED (the dashboard being connected says nothing about these)
  progress    the step and ticker of the cycle in progress (ExecutionService.progress; None between cycles - never estimated)
  last_cycle  the most recent cycle (this process's, IDLE included; else the newest persisted run) and its plain-language summary
  tickers     per ticker: the persisted entry evaluation (BLOCKED / SKIPPED / ENTRY_INTENT with the controls' own reasons) or the setup's waiting reason
  risk        the execution safety gate now and the executor's per-control liveness rows (last result and reason)
  orders      intents with their BROKER-REPORTED order events (execution_order_events); a fill is shown only when the broker reported it
  positions   bot positions with their latest monitoring / exit decision, and the account's positions marked bot-managed or external (unmanaged)
  history     persisted runs (identical quiet cycles collapsed), mode changes, order events, position events, findings and safety events, newest first

Zero broker calls, no model call, no write. Credentials never reach this payload: no raw broker JSON is passed through.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from execution import MODE_LABELS
from paper import alpaca_order_rules as RU

VERSION = "bot_activity_v1"
CONTROL_LABELS = {"PAPER_ENVIRONMENT": "Paper account proof", "EXECUTION_MODE": "Mode", "SAFETY_GATE": "Risk gate", "PROPOSAL_VALIDITY": "Proposal",
                  "SESSION_POLICY": "Market session", "DUPLICATE_ENTRY": "Duplicate entry", "MAX_OPEN_POSITIONS": "Open-positions limit", "BROKER_ACCOUNT": "Broker account",
                  "BROKER_TRADABILITY": "Tradable at the broker", "BROKER_POSITION": "Already held at the broker", "BROKER_RECONCILIATION": "Reconciliation",
                  "ENTRY_SUBMISSION": "Entry order", "POSITION_RECONCILIATION": "Position reconciliation", "EXIT_TRIGGER": "Exit check", "EXIT_SUBMISSION": "Exit order",
                  "ENTRIES_PER_CYCLE": "Entries per cycle", "UNIFIED_EXTERNAL_EXPOSURE": "Unified exposure"}   # Stage 6.9.1: the entry gate over the other accounts
ORDER_LABELS = {RU.SUBMITTED: "Submitted (received by the broker)", RU.BROKER_ACCEPTED: "Accepted by the broker", RU.PARTIALLY_FILLED: "Partially filled",
                RU.FILLED: "Filled", RU.CANCELED: "Canceled or expired", RU.BROKER_REJECTED: "Rejected by the broker", RU.RR: "Unrecognized broker status · reconciliation required"}
WORKER = {"RUNNING": "Working", "IDLE": "Idle", "STANDBY": "Standby", "STARTING": "Starting", "STALE": "Stale", "ERROR": "Error", "STOPPED": "Not running", "DISABLED": "Off"}
TICKER = {"BLOCKED": "Entry blocked", "SKIPPED": "Skipped this cycle", "ENTRY_INTENT": "Entry order", "WAITING": "Waiting", "READY_MONITOR_ONLY": "Ready · Monitor only",
          "READY_PENDING": "Ready · not evaluated yet"}


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _hm(ts) -> str:
    d = _parse(ts)
    return d.astimezone(timezone.utc).strftime("%H:%M:%S UTC") if d else "—"


def worker(sched: dict, enabled: bool, now: datetime) -> Dict[str, Any]:
    """The bot worker's state from the scheduler's own heartbeat. A connected dashboard is NOT evidence of a working worker."""
    hb = _parse(sched.get("heartbeat_at"))
    age = round((now - hb).total_seconds(), 1) if hb else None
    regular = bool((sched.get("market") or {}).get("regular_session"))
    interval = float(sched.get("interval_s" if regular else "idle_interval_s") or sched.get("interval_s") or 0) or None
    gap = min(interval, float(sched.get("max_sleep_s") or 300)) if interval else None
    stale_after = 2 * gap + 30 if gap else None
    last = sched.get("last_result") or {}
    if not enabled:
        state, text = "DISABLED", "The executor loop is off (EXECUTION_SCHEDULER_ENABLED=false): no cycle runs and nothing is monitored."
    elif not sched.get("running"):
        state, text = "STOPPED", "The bot worker is not running in this server process: the dashboard is connected, but no cycle runs."
    elif sched.get("in_cycle"):
        state, text = "RUNNING", f"A cycle started at {_hm(sched.get('cycle_started_at'))} and is in progress."
    elif hb is None:
        state, text = "STARTING", "The worker thread started; its first scheduler decision is due after the startup delay."
    elif stale_after is not None and age is not None and age > stale_after:
        state, text = "STALE", f"No heartbeat for {int(age)} s (expected about every {int(gap)} s): the worker may be stuck."
    elif last.get("result") == "ERROR" or sched.get("last_error"):
        state, text = "ERROR", f"The last cycle failed ({sched.get('last_error') or last.get('error') or 'error'}); it is retried at the next interval."
    elif last.get("result") == "LEASE_HELD":
        state, text = "STANDBY", "Another Stock Agent process holds the executor lease; this worker waits and does not trade."
    else:
        state, text = "IDLE", f"Alive; waiting for the next scheduled run at {_hm(sched.get('next_at'))}."
    return {"state": state, "label": WORKER[state], "text": text, "heartbeat_at": sched.get("heartbeat_at"), "heartbeat_age_s": age, "expected_every_s": gap,
            "stale_after_s": stale_after, "running": bool(sched.get("running")), "in_cycle": bool(sched.get("in_cycle")), "cycle_started_at": sched.get("cycle_started_at"),
            "last_started_at": sched.get("last_run_at"), "last_finished_at": sched.get("last_finished_at"), "next_at": sched.get("next_at"), "runs": sched.get("runs"),
            "last_result": last.get("result"), "last_error": sched.get("last_error"), "interval_s": interval, "regular_session": regular}


def cycle_text(summary: dict, result: str, mode: str, error: Optional[str] = None) -> str:
    """One plain-language line per cycle, from the cycle's own summary (no estimate, no model)."""
    if result == "IDLE":
        return "Monitor only with nothing open: no broker call, nothing evaluated."
    if result == "ERROR":
        return f"Cycle failed: {error or 'error'}."
    parts = [f"{summary.get('monitored') or 0} open order(s) checked"]
    parts.append(f"reconciliation: {summary['findings']} finding(s)" if summary.get("findings") else "reconciliation clean")
    if summary.get("positions"):
        dec = summary.get("decisions") or []
        holds = sum(d.get("decision") == "HOLD" for d in dec)
        parts.append(f"{summary['positions']} bot position(s) monitored ({holds} hold, {summary.get('exits_submitted', 0)} exit sent, {summary.get('exits_deferred', 0)} deferred)")
    else:
        parts.append("no bot position")
    if summary.get("safety"):
        parts.append(f"risk gate {str(summary['safety']).replace('_', ' ').lower()}")
    ent = summary.get("entries")
    if mode != "PAPER_AUTO":
        parts.append("entries not evaluated (Monitor only)")
    elif not ent or not ent.get("candidates") and not ent.get("evaluations"):
        parts.append("no READY ticker to evaluate")
    else:
        ev = ent.get("evaluations") or []
        parts.append(f"{len(ent.get('candidates') or [])} READY ticker(s) evaluated: {len(ent.get('submitted') or [])} entry order(s), "
                     f"{sum(e.get('result') == 'BLOCKED' for e in ev)} blocked, {sum(e.get('result') == 'SKIPPED' for e in ev)} skipped")
    trades = len((ent or {}).get("submitted") or []) + int(summary.get("exits_submitted") or 0)
    return "; ".join(parts) + ("." if trades else ". No trade.")


def _cycle_row(source: str, at, finished, result, trigger, mode, summary, error) -> Dict[str, Any]:
    return {"source": source, "at": at, "finished_at": finished, "result": result, "trigger": trigger, "mode": mode, "duration_ms": (summary or {}).get("duration_ms"),
            "text": cycle_text(summary or {}, result, mode, error), "error": error}


def last_cycle(service, runs: List[dict]) -> Optional[Dict[str, Any]]:
    lc = service.last_cycle
    if lc:
        return _cycle_row("THIS_PROCESS", lc.get("at"), lc.get("finished_at"), lc.get("result"), lc.get("trigger"), lc.get("mode"), lc.get("summary"), lc.get("error"))
    if runs:
        r = runs[0]
        return _cycle_row("DATABASE", r["started_at"], r.get("finished_at"), r["result"], r["trigger"], r["mode"], r.get("summary"), r.get("error"))
    return None


def tickers(service, mode: str, runs: List[dict], latest_at: Optional[str] = None) -> List[Dict[str, Any]]:
    """Per ticker: the newest persisted entry evaluation, or why it waits. READY tickers in Monitor only are never evaluated, and say so.
    Every row names its origin: scope LATEST_CYCLE / EARLIER_CYCLE (an entry evaluation, `evaluated_at` = that cycle's start) or SETUP (the
    setup state, `checked_at` = the setup's last evaluation) - an older evaluation is never shown as the latest cycle's work."""
    latest: Dict[str, dict] = {}
    sources = ([(service.last_cycle.get("at"), service.last_cycle.get("summary") or {})] if service.last_cycle else []) + [(r["started_at"], r.get("summary") or {}) for r in runs]
    for at, summary in sources:
        for ev in ((summary.get("entries") or {}).get("evaluations") or []):
            latest.setdefault(ev["symbol"], {**ev, "at": at})
    rows: Dict[str, dict] = {}
    try:
        episodes = service.setups.store.open_episodes()
    except Exception:  # noqa: BLE001 - display only
        episodes = []
    for ep in episodes:
        sym = ep.symbol
        try:
            v = service.setups.current(sym)
            why, blockers, label = v.reason_text, list(v.blockers or []), v.state_label
        except Exception:  # noqa: BLE001
            why, blockers, label = None, [], ep.state
        ev = latest.get(sym)
        setup = {"setup_state": ep.state, "scope": "SETUP", "checked_at": ep.last_evaluated_at, "evaluated_at": None, "at": ep.last_evaluated_at}
        if ep.state != "READY":
            rows[sym] = {"symbol": sym, "status": "WAITING", "text": why or f"Setup {label}: waiting for its conditions.",
                         "reasons": [{"control": "SETUP", "label": "Setup", "text": str(b).replace("_", " ").lower()} for b in blockers], **setup}
        elif mode != "PAPER_AUTO":
            rows[sym] = {"symbol": sym, "status": "READY_MONITOR_ONLY", "text": "READY, but the bot is in Monitor only: entries are not evaluated.", "reasons": [], **setup}
        elif ev is None:
            rows[sym] = {"symbol": sym, "status": "READY_PENDING", "text": "READY; evaluated at the next Paper Auto cycle.", "reasons": [], **setup}
    for sym, ev in latest.items():
        if rows.get(sym, {}).get("status") in ("WAITING", "READY_MONITOR_ONLY"):
            continue                                                        # the setup left READY, or the bot is in Monitor only now: the current state wins
        reasons = [{"control": r.get("control"), "label": CONTROL_LABELS.get(r.get("control"), r.get("control")), "text": r.get("text"),
                    **({"code": r.get("code"), "detail": r.get("detail")} if r.get("code") else {})} for r in ev.get("reasons") or []]   # Stage 6.9.1: the gate's record
        text = {"BLOCKED": "Entry blocked: " + "; ".join(r["text"] for r in reasons if r["text"]), "SKIPPED": reasons[0]["text"] if reasons else "Skipped this cycle.",
                "ENTRY_INTENT": f"Entry order: {(ev.get('intent_state') or '').replace('_', ' ').lower() or 'recorded'}"
                                + (f" (broker status {ev['broker_status']})" if ev.get("broker_status") else "")}.get(ev["result"], ev["result"])
        rows[sym] = {"symbol": sym, "status": ev["result"], "setup_state": rows.get(sym, {}).get("setup_state"), "text": text, "reasons": reasons, "at": ev.get("at"),
                     "execution_id": ev.get("execution_id"), "evaluated_at": ev.get("at"), "checked_at": None,
                     "scope": "LATEST_CYCLE" if latest_at and ev.get("at") == latest_at else "EARLIER_CYCLE"}
    order = {"ENTRY_INTENT": 0, "BLOCKED": 1, "SKIPPED": 2, "READY_PENDING": 3, "READY_MONITOR_ONLY": 4, "WAITING": 5}
    return sorted((dict(r, label=TICKER.get(r["status"], r["status"])) for r in rows.values()), key=lambda r: (order.get(r["status"], 9), r["symbol"]))


def mode_view(m: dict, lc: Optional[dict], w: dict) -> Dict[str, Any]:
    """The CURRENT mode (persisted, with when it changed) apart from the mode the last cycle ran in. Paper Auto switched on after the last cycle
    is 'awaiting its first cycle' until the worker runs one - never shown as if that cycle had happened."""
    since, lc_mode = m.get("changed_at"), (lc or {}).get("mode")
    out = {"mode": m["mode"], "label": MODE_LABELS[m["mode"]], "changed_at": since, "changed_by": m.get("changed_by"), "last_cycle_mode": lc_mode,
           "last_cycle_mode_label": MODE_LABELS.get(lc_mode, lc_mode) if lc_mode else None, "last_cycle_at": (lc or {}).get("at"), "awaiting_first_cycle": False, "note": None}
    after_last = bool(since) and (lc is None or str(since) > str(lc.get("at") or ""))
    ran = f" The last cycle ({_hm(lc.get('at'))}) ran in {out['last_cycle_mode_label']}." if lc and lc_mode else ""
    if m["mode"] == "PAPER_AUTO" and after_last and lc_mode != "PAPER_AUTO":
        out["awaiting_first_cycle"] = True
        nxt = f"next scheduled run {_hm(w.get('next_at'))}" if w.get("running") and w.get("next_at") else "the worker is not running, so no cycle is scheduled"
        out["note"] = f"Paper Auto since {_hm(since)}: awaiting its first cycle ({nxt}).{ran}"
    elif lc_mode and lc_mode != m["mode"]:
        out["note"] = f"{out['label']} since {_hm(since)}.{ran}"
    return out


def latest_work(lc_summary: Optional[dict]) -> List[Dict[str, Any]]:
    """What the latest cycle itself evaluated (entry evaluations of that cycle only)."""
    return [{"symbol": e.get("symbol"), "result": e.get("result")} for e in (((lc_summary or {}).get("entries") or {}).get("evaluations") or [])]


def risk(service, now: datetime) -> Dict[str, Any]:
    out: Dict[str, Any] = {"gate": None, "controls": []}
    try:
        sv = service.safety.current(now=now)
        out["gate"] = {"state": sv.state, "label": sv.state_label, "allowed_new_entry": sv.allowed_new_entry, "reason_text": sv.reason_text, "evaluated_at": sv.evaluated_at,
                       "checks": [{"control": c.control_id, "result": c.result, "text": c.reason} for c in sv.controls]}
    except Exception as exc:  # noqa: BLE001 - an unreadable gate is shown as such (the executor treats it as blocked)
        out["gate"] = {"state": "UNKNOWN", "label": "Unavailable", "allowed_new_entry": False, "reason_text": f"risk gate unreadable ({type(exc).__name__})", "checks": []}
    for c in service.store.controls():
        out["controls"].append({"control": c["control_id"], "label": CONTROL_LABELS.get(c["control_id"], c["control_id"]), "result": c["last_result"], "text": c.get("last_reason"),
                                "at": c["last_evaluated_at"], "count": c["evaluation_count"]})
    return out


def orders(store, limit: int = 10) -> List[Dict[str, Any]]:
    """Each intent with its broker-reported order events. 'Sent' is our own record; every later step is the broker's status, verbatim."""
    out = []
    for it in store.intents(limit=limit):
        steps = [{"at": it.created_at, "kind": "LOCAL", "label": "Intent recorded (before any broker call)"}]
        if it.submitted_at:
            steps.append({"at": it.submitted_at, "kind": "LOCAL", "label": "Sent to the broker"})
        for e in (store.order_events(it.broker_order_id) if it.broker_order_id else []):
            st, _ = RU.map_status(e.get("status"))
            qty = f" {e.get('filled_qty') or 0}/{it.qty}" if st in (RU.PARTIALLY_FILLED, RU.FILLED) else ""
            px = f" @ {e['filled_avg_price']}" if st in (RU.PARTIALLY_FILLED, RU.FILLED) and e.get("filled_avg_price") else ""
            steps.append({"at": e["at"], "kind": "BROKER", "label": ORDER_LABELS[st] + qty + px, "broker_status": e.get("status"), "filled_qty": e.get("filled_qty"),
                          "filled_avg_price": e.get("filled_avg_price"), "source": e.get("source")})
        if it.state in ("NOT_SENT", "FAILED", "RECONCILIATION_REQUIRED"):
            steps.append({"at": it.resolved_at or it.updated_at, "kind": "LOCAL", "label": f"{it.state.replace('_', ' ').capitalize()}: {it.failure_text or it.failure_code or ''}".strip()})
        out.append({"execution_id": it.execution_id, "symbol": it.symbol, "type": it.intent_type, "side": it.side, "qty": it.qty, "state": it.state, "broker_status": it.broker_status,
                    "filled_qty": it.filled_qty, "filled_avg_price": it.filled_avg_price, "exit_reason": it.exit_reason, "steps": steps})
    return out


def positions(service, runs: List[dict], account: Optional[dict]) -> Dict[str, Any]:
    decisions: Dict[str, dict] = {}
    sources = ([(service.last_cycle.get("at"), service.last_cycle.get("summary") or {})] if service.last_cycle else []) + [(r["started_at"], r.get("summary") or {}) for r in runs]
    for at, summary in sources:
        for d in summary.get("decisions") or []:
            decisions.setdefault(d["position_id"], {**d, "at": at})
    bot = []
    for p in service.store.positions(open_only=True):
        d = decisions.get(p.position_id)
        bot.append({"symbol": p.symbol, "position_id": p.position_id, "state": p.state, "quantity": p.quantity, "average_entry_price": p.average_entry_price,
                    "last_price": p.last_price, "last_price_at": p.last_price_at, "opened_at": p.opened_at, "exit_reason": p.exit_reason,
                    "decision": d.get("decision") if d else None, "decision_text": d.get("text") if d else "Not monitored by a cycle yet.", "decision_at": d.get("at") if d else None})
    return {"bot": bot, "account": account}


def history(service, store, runs: List[dict], limit: int = 40) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for r in runs:                                                              # newest first; identical consecutive quiet cycles become one row with a count
        text = cycle_text(r.get("summary") or {}, r["result"], r["mode"], r.get("error"))
        row = {"at": r["started_at"], "kind": "CYCLE", "level": "error" if r["result"] == "ERROR" else "info", "text": f"Cycle ({str(r['trigger']).lower()}): {text}", "count": 1}
        if items and items[-1]["kind"] == "CYCLE" and items[-1]["text"] == row["text"]:
            items[-1]["count"] += 1
            items[-1]["first_at"] = r["started_at"]
        else:
            items.append(row)
    for e in store.mode_events(10):
        items.append({"at": e["at"], "kind": "MODE", "level": "info", "text": f"Mode {MODE_LABELS.get(e.get('from_mode'), e.get('from_mode') or 'default')} → "
                                                                             f"{MODE_LABELS.get(e['to_mode'], e['to_mode'])} by {e['actor']}"})
    intents = {i.execution_id: i for i in store.intents(limit=200)}
    for e in store.order_events(limit=30):
        it = intents.get(e.get("execution_id"))
        st, _ = RU.map_status(e.get("status"))
        items.append({"at": e["at"], "kind": "ORDER", "level": "warn" if st in (RU.BROKER_REJECTED, RU.RR) else "info",
                      "text": f"{it.symbol if it else '?'} {it.intent_type.lower() if it else ''} order: {ORDER_LABELS[st]} (broker status {e.get('status')})"})
    syms = {p.position_id: p.symbol for p in store.positions(open_only=False, limit=500)}
    for e in store.position_events(limit=30):
        items.append({"at": e["at"], "kind": "POSITION", "level": "info", "text": f"{syms.get(e['position_id'], '?')}: {str(e['kind']).replace('_', ' ').lower()}"
                                                                                + (f" {e.get('from_state') or ''} → {e['to_state']}" if e.get("to_state") else "") + (f" ({e['reason']})" if e.get("reason") else "")})
    for f in store.findings(30):
        items.append({"at": f["at"], "kind": "FINDING", "level": "warn" if f["blocking"] else "info",
                      "text": f"{f.get('symbol') or ''} {str(f['kind']).replace('_', ' ').lower()} ({'blocks new entries' if f['blocking'] else 'informational'})".strip()})
    try:
        for e in service.safety.store.events(20):
            items.append({"at": e["at"], "kind": "RISK", "level": "info", "text": f"Risk gate {str(e['kind']).replace('_', ' ').lower()}"
                                                                                + (f": {e.get('from_state') or ''} → {e['to_state']}" if e.get("to_state") else "") + (f" ({e['reason']})" if e.get("reason") else "")})
    except Exception:  # noqa: BLE001 - display only
        pass
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items[:limit]


def activity(service, *, scheduler_status: dict, enabled: bool, account: Optional[dict] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    m = service.mode()
    runs = service.store.runs(30)
    p = service.progress                                                    # read once: the cycle thread may clear it at any moment
    prog = dict(p) if p else None
    w = worker(scheduler_status, enabled, now)
    lc = last_cycle(service, runs)
    lc_summary = (service.last_cycle or {}).get("summary") if lc and lc["source"] == "THIS_PROCESS" else (runs[0].get("summary") if runs else None)
    return {"version": VERSION, "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "broker_calls": 0,
            "mode": mode_view(m, lc, w), "worker": w, "progress": prog, "last_cycle": lc, "latest_cycle_evaluations": latest_work(lc_summary),
            "tickers": tickers(service, m["mode"], runs, (lc or {}).get("at")),
            "risk": risk(service, now), "orders": orders(service.store), "positions": positions(service, runs, account), "history": history(service, service.store, runs),
            "note": "Real backend records only: the scheduler heartbeat, the executor's persisted cycles, controls, broker-reported order events, position events and findings. "
                    "Nothing is estimated, no fill is inferred from a submission, and no model narrates."}
