"""
autorun/policy.py - Stage 6.10A auto-run rules as PURE functions over already-gathered facts (autorun/facts.py): no I/O, no clock read, no broker.

  settings_from(cfg)        the policy settings; an unknown PAPER_AUTO_RUN_POLICY value is MANUAL_ARM; SCHEDULED_ARM is effective only with
                            PAPER_AUTO_SCHEDULED_ARM_ENABLED=true as well
  worker_liveness(...)      the executor loop judged by its OWN contract (next_at / MAX_SLEEP_S + a grace), never by the display classifier
  preflight(facts)          READY | DEGRADED | BLOCKED with every check; WARN / INFO items never gate anything
  disarm_conditions(...)    the deterministic auto-disarm conditions (each one ends new entries; monitoring and exits continue)
  scheduled_arm_check(...)  every condition the opt-in scheduled arm needs - execution-safety facts only (telemetry never gates)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from autorun import (BLOCKED, BROKER_FAILURES_TO_DISARM, DEGRADED, EQUITY_UNAVAILABLE_SECONDS, FAIL, INFO, LIVENESS_GRACE_SECONDS, MANUAL_ARM, PASS,
                     POLICIES, PROOF_DEFINITIVE_ERRORS, PROOF_FAILURES_TO_DISARM, READY, REGULAR, SCHEDULED_ARM, SUPPORTED_CONTROLS_VERSIONS, WARN,
                     WORKER_ERROR_TICKS_TO_DISARM)

MAX_SLEEP_S = 300.0                         # execution/scheduler.py MAX_SLEEP_S (the loop never sleeps longer between decisions)
HUNG_CYCLE_S = 600.0                        # a cycle still running after this long is a stuck loop (every broker read has a short timeout)


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _flag(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def settings_from(cfg: Any) -> Dict[str, Any]:
    raw = str(getattr(cfg, "PAPER_AUTO_RUN_POLICY", MANUAL_ARM) or "").strip().upper()
    policy = raw if raw in POLICIES else MANUAL_ARM
    scheduled_enabled = _flag(getattr(cfg, "PAPER_AUTO_SCHEDULED_ARM_ENABLED", False))
    effective = SCHEDULED_ARM if policy == SCHEDULED_ARM and scheduled_enabled else MANUAL_ARM
    problems = []
    if raw and raw not in POLICIES:
        problems.append(f"PAPER_AUTO_RUN_POLICY={raw!r} is not one of {', '.join(POLICIES)}: MANUAL_ARM applies")
    if policy == SCHEDULED_ARM and not scheduled_enabled:
        problems.append("PAPER_AUTO_RUN_POLICY=SCHEDULED_ARM without PAPER_AUTO_SCHEDULED_ARM_ENABLED=true: MANUAL_ARM applies")
    try:
        delay = max(0, int(getattr(cfg, "PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES", 2)))
    except (TypeError, ValueError):
        delay = 2
        problems.append("PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES is not an integer: 2 applies")
    return {"policy": policy, "policy_raw": raw, "scheduled_arm_enabled": scheduled_enabled, "effective_policy": effective,
            "disarm_at_session_end": _flag(getattr(cfg, "PAPER_AUTO_DISARM_AT_SESSION_END", True)),
            "supervisor_enabled": _flag(getattr(cfg, "AUTORUN_SUPERVISOR_ENABLED", True)), "arm_delay_minutes": delay,
            "near_close_minutes": int(getattr(cfg, "EXECUTION_NEAR_CLOSE_MINUTES", 15) or 15), "problems": problems}


def worker_liveness(sched: Dict[str, Any], now: datetime, grace_s: float = LIVENESS_GRACE_SECONDS) -> Dict[str, Any]:
    """{state: NOT_STARTED | RUNNING | ALIVE | STALE | ERROR, text}. Stale only past the loop's own next decision (next_at) or MAX_SLEEP_S - so a
    healthy loop that slept through the open is never stale (it decides at next_at, at most MAX_SLEEP_S later)."""
    if not sched.get("running"):
        return {"state": "NOT_STARTED" if not sched.get("heartbeat_at") else "STOPPED", "text": "the executor loop is not running in this process"}
    if sched.get("in_cycle"):
        started = _parse(sched.get("cycle_started_at"))
        if started is not None and (now - started).total_seconds() > HUNG_CYCLE_S:
            return {"state": "STALE", "text": f"a cycle has been running since {started.isoformat(timespec='seconds')} (more than {int(HUNG_CYCLE_S)} s)"}
        return {"state": "RUNNING", "text": "a cycle is in progress"}
    hb, nxt, fin = _parse(sched.get("heartbeat_at")), _parse(sched.get("next_at")), _parse(sched.get("last_finished_at"))
    if hb is None:
        return {"state": "NOT_STARTED", "text": "the loop has not made its first decision yet"}
    due = max(d for d in (nxt, fin) if d is not None) if (nxt or fin) else None   # after a cycle longer than the interval the loop decides again on finishing
    late = (due is not None and (now - due).total_seconds() > grace_s) or (now - hb).total_seconds() > MAX_SLEEP_S + grace_s
    if late:
        return {"state": "STALE", "text": f"no loop decision since {hb.isoformat(timespec='seconds')} (next was due {due.isoformat(timespec='seconds') if due else '-'})"}
    res = sched.get("last_result") or {}
    err = sched.get("last_error") or (res.get("result") == "ERROR" and (res.get("error") or "cycle result ERROR"))   # a cycle that failed inside run_cycle too
    if err:
        return {"state": "ERROR", "text": f"the last cycle failed ({str(err)[:120]})"}
    return {"state": "ALIVE", "text": "alive"}


def proof_state(env: Dict[str, Any], failures: int) -> Dict[str, Any]:
    """The paper proof from the executor's own last account read (environment cache, already allowlisted) - never a new broker call."""
    if not env:
        return {"state": "NOT_YET", "definitive": False, "text": "no paper proof in this process yet (it is taken when Paper Auto is armed)"}
    err = str(env.get("error") or "")
    status = str(env.get("status") or "").upper()
    if env.get("paper") and status == "ACTIVE" and not env.get("blocked"):
        return {"state": "PROVEN", "definitive": False, "text": f"paper proof ({env.get('label') or 'paper'}) at {env.get('checked_at') or '-'}"}
    if env.get("paper"):                                                                    # the PAPER account answered, but it cannot trade
        return {"state": "LOST", "definitive": True, "text": f"paper account not active ({status or 'unknown'}{', blocked' if env.get('blocked') else ''})"}
    definitive = err in PROOF_DEFINITIVE_ERRORS or (status and status != "ACTIVE") or bool(env.get("blocked")) or failures >= PROOF_FAILURES_TO_DISARM
    return {"state": "LOST" if definitive else "FAILED", "definitive": definitive, "text": f"paper proof failed ({err or status or 'no proof'})"}


def _check(cid: str, status: str, severity: str, text: str) -> Dict[str, Any]:
    return {"id": cid, "status": status, "severity": severity, "text": text}


def preflight(f: Dict[str, Any]) -> Dict[str, Any]:
    """READY | DEGRADED | BLOCKED from gathered facts. FAIL + severity BLOCKED blocks; FAIL/WARN + DEGRADED degrades; INFO never counts."""
    ch: List[Dict[str, Any]] = []
    s = f.get("settings") or {}
    ch.append(_check("config", FAIL if s.get("problems") and any("not one of" in p for p in s["problems"]) else WARN if s.get("problems") else PASS,
                     BLOCKED if any("not one of" in p for p in s.get("problems") or []) else DEGRADED,
                     "; ".join(s.get("problems") or []) or f"auto-run policy {s.get('effective_policy')}"))
    db = f.get("db") or {}
    ch.append(_check("database", PASS if db.get("readable") else FAIL, BLOCKED, "readable" if db.get("readable") else f"unreadable ({db.get('error')})"))
    lease = f.get("lease") or {}
    if f.get("passive"):
        ch.append(_check("single_instance", FAIL, BLOCKED, f"this process is passive: another instance owns this database ({lease.get('owner')}); no executor runs here"))
    elif lease.get("other_renewing"):
        ch.append(_check("single_instance", FAIL, BLOCKED, f"another live instance holds the instance lease ({lease.get('owner')})"))
    elif f.get("foreign_executor"):
        ch.append(_check("single_instance", FAIL, BLOCKED, f"another executor ran cycles on this database since this process started ({', '.join(f['foreign_executor'])})"))
    elif lease.get("held_by_self"):
        ch.append(_check("single_instance", PASS, BLOCKED, "this process holds the instance lease"))
    elif lease.get("other_static"):
        ch.append(_check("single_instance", WARN, DEGRADED, f"a previous instance's lease is expiring ({lease.get('owner')}, until {lease.get('expires_at')})"))
    else:
        ch.append(_check("single_instance", WARN, DEGRADED, "the instance lease is not held yet (the supervisor takes it on its first tick)"))
    foreign = (f.get("foreign") or {}).get("orders") or []
    ch.append(_check("foreign_orders", FAIL if foreign else PASS, BLOCKED,
                     f"{len(foreign)} open 'stockagent-' order(s) on the paper account unknown to this database" if foreign else "no foreign bot order"))
    proof = f.get("proof") or {}
    if not f.get("broker_configured"):
        ch.append(_check("paper_proof", FAIL, BLOCKED, "no paper broker is configured"))
    elif proof.get("state") == "LOST":
        ch.append(_check("paper_proof", FAIL, BLOCKED, proof.get("text") or "paper proof lost"))
    elif proof.get("state") == "PROVEN":
        ch.append(_check("paper_proof", PASS, BLOCKED, proof.get("text") or "proven"))
    else:
        acct = f.get("account") or {}
        ok = acct.get("source") in ("ALPACA_PAPER", "SIMULATED_PAPER") and acct.get("status") in ("FRESH", "STALE")
        ch.append(_check("paper_proof", PASS if ok else WARN, DEGRADED,
                         f"paper account read by the account refresh ({acct.get('source')})" if ok else (proof.get("text") or "not proven yet")))
    sf = f.get("safety") or {}
    ch.append(_check("emergency_stop", FAIL if sf.get("kill_active") else PASS, BLOCKED, "Emergency Stop is active" if sf.get("kill_active") else "inactive"))
    it, ps = f.get("intents") or {}, f.get("positions") or {}
    amb = int(it.get("reconciliation_required") or 0) + int(it.get("attempted_unresolved") or 0) + int(ps.get("attention") or 0)
    ch.append(_check("reconciliation", FAIL if amb else PASS, BLOCKED,
                     f"{amb} ambiguous order / position state(s) need reconciliation" if amb else
                     ("reconciled clean" if (f.get("recon") or {}).get("reconciled") and (f.get("recon") or {}).get("healthy") else
                      "nothing ambiguous recorded (the first Paper Auto cycle reconciles before any entry)")))
    rr = (f.get("recon") or {})
    if rr.get("reconciled") and not rr.get("healthy") and not amb:
        ch.append(_check("reconciliation_findings", FAIL, BLOCKED, "; ".join(rr.get("reasons") or [])[:200] or "reconciliation not healthy"))
    open_n = int(it.get("open") or 0) - int(it.get("reconciliation_required") or 0)
    ch.append(_check("open_orders", WARN if open_n > 0 else PASS, DEGRADED, f"{open_n} order(s) in progress" if open_n > 0 else "no order in progress"))
    gate = f.get("gate") or {}
    ch.append(_check("controls_version", PASS if gate.get("supported") and not gate.get("problems") else FAIL, BLOCKED,
                     f"{gate.get('version')} supported" if gate.get("supported") and not gate.get("problems") else
                     f"entry controls {gate.get('version')} not supported or invalid ({'; '.join(gate.get('problems') or [])})"))
    ch.append(_check("executor_loop_enabled", PASS if f.get("scheduler_enabled") else FAIL, BLOCKED,
                     "EXECUTION_SCHEDULER_ENABLED" if f.get("scheduler_enabled") else "EXECUTION_SCHEDULER_ENABLED=false: no cycle can run"))
    w = f.get("worker") or {}
    ch.append(_check("worker", PASS if w.get("state") in ("ALIVE", "RUNNING") else WARN, DEGRADED, f"{w.get('state')}: {w.get('text')}"))
    acct = f.get("account") or {}
    ch.append(_check("account_equity", PASS if acct.get("status") == "FRESH" else WARN, DEGRADED, f"account data {acct.get('status')}"))
    obs = f.get("observer") or {}
    ch.append(_check("evidence_observer", PASS if obs.get("running") else WARN, DEGRADED,
                     "forward-validation observer running" if obs.get("running") else "forward-validation observer not running (no evidence capture, no early stop)"))
    # information only: never gates anything
    ch.append(_check("market_clock", INFO, INFO, f"session {(f.get('session') or {}).get('session')} ({(f.get('session') or {}).get('calendar')})"
                                                 + ("; broker clock known" if f.get("clock") else "; broker clock not read (holidays from the static calendar)")))
    crit = (f.get("attention") or {}).get("critical") or []
    ch.append(_check("historical_attention", WARN if crit else PASS, INFO, f"critical forward-validation attention on record: {', '.join(crit)}" if crit else "none"))
    ext = f.get("external") or {}
    ch.append(_check("external_accounts", INFO, INFO, f"unified portfolio {ext.get('state')}; exposure gate {'ON' if gate.get('enabled') else 'OFF'}"))
    cap = f.get("capture") or {}
    ch.append(_check("evidence_capture", WARN if cap.get("faults_today") else PASS, INFO, f"{cap.get('faults_today') or 0} capture fault(s) today"))
    blocked = [c for c in ch if c["status"] == FAIL and c["severity"] == BLOCKED]
    degraded = [c for c in ch if c["status"] in (FAIL, WARN) and c["severity"] == DEGRADED]
    status = BLOCKED if blocked else DEGRADED if degraded else READY
    return {"status": status, "checks": ch, "blocking": [c["id"] for c in blocked], "degrading": [c["id"] for c in degraded]}


def disarm_conditions(f: Dict[str, Any], mem: Dict[str, Any], now: datetime) -> List[Tuple[str, str]]:
    """The auto-disarm conditions that hold now (code, text). The service applies them only while the mode is PAPER_AUTO."""
    out: List[Tuple[str, str]] = []
    sf = f.get("safety") or {}
    if sf.get("kill_active"):
        out.append(("EMERGENCY_STOP", "the Emergency Stop is active"))
    proof = f.get("proof") or {}
    if proof.get("definitive") or not f.get("broker_configured"):
        out.append(("PAPER_PROOF_LOST", proof.get("text") or "no paper broker is configured"))
    it, ps = f.get("intents") or {}, f.get("positions") or {}
    amb = int(it.get("reconciliation_required") or 0) + int(it.get("attempted_unresolved") or 0) + int(ps.get("attention") or 0)
    if amb:
        out.append(("RECONCILIATION_AMBIGUITY", f"{amb} order / position state(s) need reconciliation"))
    acct = f.get("account") or {}
    since = _parse(mem.get("equity_unavailable_since"))
    if (f.get("session") or {}).get("session") == REGULAR and acct.get("status") != "FRESH" and since is not None \
            and (now - since).total_seconds() >= EQUITY_UNAVAILABLE_SECONDS:
        out.append(("ACCOUNT_EQUITY_UNAVAILABLE", f"account data {acct.get('status')} for {int((now - since).total_seconds())} s in the regular session"))
    started = _parse(f.get("process_start"))
    last_attempt = _parse(acct.get("last_attempt_at"))
    seen_here = started is not None and last_attempt is not None and last_attempt >= started
    failures = int(acct.get("consecutive_failures") or 0)
    if failures >= BROKER_FAILURES_TO_DISARM and (seen_here or acct.get("status") == "STALE"):      # failures of a previous process alone never disarm
        out.append(("BROKER_READ_FAILURES", f"{failures} consecutive failed broker reads"))
    w = f.get("worker") or {}
    if w.get("state") in ("STALE", "STOPPED") or (w.get("state") == "ERROR" and int(mem.get("worker_error_ticks") or 0) >= WORKER_ERROR_TICKS_TO_DISARM):
        out.append(("WORKER_FATAL", f"executor loop {w.get('state')}: {w.get('text')}"))
    if not (f.get("db") or {}).get("readable", True) or mem.get("db_failure"):              # sticky: an outage while armed is acted on once reads recover
        out.append(("DATABASE_FAILURE", f"database unreadable ({(f.get('db') or {}).get('error') or (mem.get('db_failure') or {}).get('error')})"))
    gate = f.get("gate") or {}
    if not gate.get("supported") or gate.get("problems"):
        out.append(("UNSUPPORTED_CONTROLS", f"entry controls {gate.get('version')} not in {', '.join(SUPPORTED_CONTROLS_VERSIONS)} or invalid"))
    if (f.get("lease") or {}).get("other_renewing"):
        out.append(("DUPLICATE_INSTANCE", f"another live instance holds the instance lease ({(f.get('lease') or {}).get('owner')})"))
    elif f.get("foreign_executor"):
        out.append(("DUPLICATE_INSTANCE", f"another executor ran cycles on this database ({', '.join(f['foreign_executor'])})"))
    if (f.get("foreign") or {}).get("orders"):
        out.append(("FOREIGN_INSTANCE", f"{len(f['foreign']['orders'])} open 'stockagent-' order(s) unknown to this database"))
    s = f.get("settings") or {}
    m = f.get("mode") or {}
    lc = _parse(f.get("last_close"))
    changed = _parse(m.get("changed_at"))
    if s.get("disarm_at_session_end") and lc is not None and changed is not None and changed < lc:
        out.append(("SESSION_ENDED", f"armed at {m.get('changed_at')}, before the session close of {lc.isoformat(timespec='seconds')}"))
    return out


def scheduled_arm_check(f: Dict[str, Any], day: Optional[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """Every condition of the opt-in scheduled arm. Execution-safety facts only: telemetry, the broker clock and historical attention never gate."""
    s = f.get("settings") or {}
    reasons: List[str] = []
    if s.get("effective_policy") != SCHEDULED_ARM:
        reasons.append("policy is not an enabled SCHEDULED_ARM")
    sess = f.get("session") or {}
    if sess.get("session") != REGULAR:
        reasons.append(f"session {sess.get('session')} (regular session only)")
    else:
        to_close = sess.get("minutes_to_close")
        opened = _parse(f.get("open_at"))
        if opened is None or (now - opened).total_seconds() < 60 * int(s.get("arm_delay_minutes") or 0):
            reasons.append(f"less than {s.get('arm_delay_minutes')} min after the open")
        if to_close is not None and to_close <= int(s.get("near_close_minutes") or 15):
            reasons.append("within the near-close cutoff")
    if (f.get("mode") or {}).get("mode") == "PAPER_AUTO":
        reasons.append("already armed")
    if day and day.get("scheduled_arm_at"):
        reasons.append(f"the scheduled arm already ran today ({day.get('scheduled_arm_result') or 'attempted'})")
    if day and day.get("last_disarmed_at") and day.get("last_disarmed_by") not in (None, "startup"):
        reasons.append(f"disarmed today by {day.get('last_disarmed_by')} ({day.get('last_disarm_code') or '-'}): a person decides")
    elif f.get("disarms_today"):
        reasons.append(f"disarmed today by {f['disarms_today'][-1].get('actor')}: a person decides")
    sf = f.get("safety") or {}
    if sf.get("kill_active"):
        reasons.append("Emergency Stop active")
    if sf.get("state") != "TRADING_ALLOWED" or not sf.get("allowed_new_entry"):
        reasons.append(f"safety gate {sf.get('state')}")
    it, ps = f.get("intents") or {}, f.get("positions") or {}
    if int(it.get("open") or 0) or int(it.get("attempted_unresolved") or 0):
        reasons.append("an order is open or unresolved")
    if int(ps.get("attention") or 0):
        reasons.append("a position needs attention")
    rr = f.get("recon") or {}
    if rr.get("reconciled") and not rr.get("healthy"):
        reasons.append("reconciliation not healthy")
    if (f.get("foreign") or {}).get("orders"):
        reasons.append("foreign bot orders on the paper account")
    if (f.get("account") or {}).get("status") != "FRESH":
        reasons.append(f"account data {(f.get('account') or {}).get('status')}")
    if (f.get("worker") or {}).get("state") not in ("ALIVE", "RUNNING"):
        reasons.append(f"executor loop {(f.get('worker') or {}).get('state')}")
    if not (f.get("lease") or {}).get("held_by_self") or f.get("passive") or f.get("foreign_executor"):
        reasons.append("this process does not hold the instance lease alone")
    if not (f.get("observer") or {}).get("running"):
        reasons.append("the forward-validation observer (early stop) is not running")
    if not f.get("broker_configured") or (f.get("proof") or {}).get("definitive"):
        reasons.append("paper broker not configured or proof lost")
    gate = f.get("gate") or {}
    if not gate.get("supported") or gate.get("problems"):
        reasons.append("entry controls not supported")
    return {"ok": not reasons, "reasons": reasons}
