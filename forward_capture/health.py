"""
forward_capture/health.py - one OPERATIONAL health record per market day (Stage 6.10A). No P/L, return or win rate is an input: a losing session
can be HEALTHY and a profitable one with missing evidence INVALID. Strategy results live in the Stage 6.1 cumulative view, apart from this.

  MARKET_CLOSED  an exchange holiday (autorun/calendar.py); weekends get no record
  INVALID        the evidence of the day cannot be trusted: the Stage 6.1 session FAILED / NOT_RUN, the executor loop ran for < 75 % of the
                 regular session, the PAPER account data was FRESH for < 75 % of it, reconciliation unresolved at the end, candidate capture lost
                 (sink overflow / evidence lost), a duplicate instance, or inconsistent timestamps
  DEGRADED       usable with a caveat: the Stage 6.1 session record DEGRADED, a restart in the session, recovered capture, capture faults, cycle errors, broker read failures, data-source
                 failures, mixed cohorts, partial external context while the exposure gate is ON, an automatic disarm, setup-evaluation gaps, or a
                 READY candidate under Paper Auto without an executor decision within two cycles
  HEALTHY        everything above clean
Coverage is measured over the expected regular-session minutes (390; fewer on an early close): a minute counts when an observer sample shows
the condition within one observer cadence of it. The record stays open (upserted) until the Stage 6.1 session is closed, then it is FINAL.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from forward_capture import DEGRADED, HEALTHY, INVALID, MARKET_CLOSED, VERSION

UTC = timezone.utc
MIN_COVERAGE = 0.75
SETUP_COVERAGE = 0.90
FINALIZE_AFTER_S = 600                    # after the close (and the Stage 6.1 session record closed)
INVALID_FAULTS = ("SINK_OVERFLOW", "EVIDENCE_LOST", "DUPLICATE_INSTANCE")
REASON_TEXT = {"FV_SESSION_FAILED": "the Stage 6.1 session record is FAILED", "FV_SESSION_NOT_RUN": "the Stage 6.1 observer did not run in the session",
               "WORKER_COVERAGE_LOW": "the executor loop ran for less than 75 % of the regular session",
               "ACCOUNT_DATA_COVERAGE_LOW": "the PAPER account data was fresh for less than 75 % of the regular session",
               "UNRESOLVED_RECONCILIATION": "reconciliation was unresolved at the end of the session", "CAPTURE_LOST": "candidate evidence was lost (not recorded)",
               "DUPLICATE_INSTANCE": "another instance used the same database", "INCONSISTENT_TIMESTAMPS": "observation timestamps are inconsistent",
               "FV_SESSION_DEGRADED": "the Stage 6.1 session record is DEGRADED (see its notes)", "RESTART_IN_SESSION": "the process restarted during the session", "RECOVERED_CAPTURE": "an entry was recovered from persisted rows (no decision-time record)",
               "CAPTURE_FAULTS": "recording faults occurred", "CYCLE_ERRORS": "executor cycles failed", "BROKER_READ_FAILURES": "broker reads failed",
               "DATA_SOURCE_FAILURES": "a data source was unavailable", "MIXED_COHORTS": "decisions of more than one cohort (configuration changed)",
               "PARTIAL_EXTERNAL_CONTEXT": "the exposure gate was ON while the other accounts' data was partial or unavailable",
               "AUTO_DISARM": "Paper Auto was disarmed automatically", "SETUP_EVALUATION_GAPS": "setup evaluations missed part of the session",
               "READY_WITHOUT_EXECUTOR_DECISION": "a READY candidate under Paper Auto got no executor decision within two cycles"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def window(td: str) -> Optional[Tuple[datetime, datetime, int]]:
    """(open, close, minutes) of the trading date's regular session in UTC; None on a weekend or holiday."""
    from autorun import calendar as CAL
    from setups.market_hours import OPEN_MINUTE, to_et
    d = date.fromisoformat(td)
    close = CAL.close_minute(d)
    if close is None:
        return None
    et = to_et(datetime(d.year, d.month, d.day, 12, tzinfo=UTC))
    tz = et.tzinfo if et is not None else UTC
    o = datetime(d.year, d.month, d.day, OPEN_MINUTE // 60, OPEN_MINUTE % 60, tzinfo=tz).astimezone(UTC)
    c = datetime(d.year, d.month, d.day, close // 60, close % 60, tzinfo=tz).astimezone(UTC)
    return o, c, close - OPEN_MINUTE


def coverage(times: List[datetime], start: datetime, end: datetime, half_width_s: float) -> float:
    """Share of [start, end) within half_width_s of a sample time (merged intervals; 0 when the window is empty)."""
    total = (end - start).total_seconds()
    if total <= 0:
        return 0.0
    spans = sorted((max(start, t - timedelta(seconds=half_width_s)), min(end, t + timedelta(seconds=half_width_s))) for t in times)
    covered, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                covered += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        covered += (cur_e - cur_s).total_seconds()
    return round(min(1.0, covered / total), 4)


def _pt(ts: Optional[datetime]) -> str:
    if ts is None:
        return "-"
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")
    except Exception:  # noqa: BLE001 - no time-zone database: UTC, labelled
        return ts.astimezone(UTC).strftime("%H:%M UTC")


def paper_auto_spans(q, start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
    """The PAPER_AUTO intervals inside [start, end) from the persisted mode events (append-only)."""
    rows = q("SELECT at, to_mode FROM execution_mode_events WHERE at < ? ORDER BY at, rowid", (_iso(end),))
    spans, on_since, mode = [], None, "MONITOR_ONLY"
    for r in rows:
        at = _parse(r["at"])
        if at is None:
            continue
        if at <= start:
            mode = r["to_mode"]
            continue
        if mode != "PAPER_AUTO" and r["to_mode"] == "PAPER_AUTO":
            on_since = at
        elif mode == "PAPER_AUTO" and r["to_mode"] != "PAPER_AUTO":
            spans.append((on_since or start, at))
            on_since = None
        mode = r["to_mode"]
    if mode == "PAPER_AUTO":
        spans.append((on_since or start, end))
    return [(s, e) for s, e in spans if e > s]


def compute(cap, td: str, now: datetime) -> Optional[Dict[str, Any]]:
    """The health record of trading date `td` (None for a weekend). Read only."""
    from autorun import calendar as CAL
    q = cap.store.query
    w = window(td)
    if w is None:
        d = date.fromisoformat(td)
        if d.weekday() >= 5:
            return None
        return {"trading_date": td, "status": MARKET_CLOSED, "final": True, "reasons": ["EXCHANGE_HOLIDAY"], "metrics": {"calendar": CAL.CALENDAR_VERSION},
                "summary_text": f"Market session: MARKET CLOSED (exchange holiday, calendar {CAL.CALENDAR_VERSION})", "cohort_ids": [], "source_session_status": None}
    o, c, minutes = w
    if now < o:
        return None                                                       # before the open: nothing of the session exists yet (never a premarket 'HEALTHY')
    end = min(c, now)
    start_s, end_s = _iso(o), _iso(c)
    from forward_validation.service import ny_window
    day_lo, day_hi = ny_window(td)
    fv_row = cap.fv.store.session(td)
    fv_status = fv_row["session_status"] if fv_row else "NOT_RUN"
    samples = q("SELECT at, loop_running, account_status, reconciliation_healthy, attention_json, open_positions, mode FROM forward_validation_samples "
                "WHERE at >= ? AND at < ? ORDER BY at, rowid", (start_s, end_s))
    cadence = float(getattr(config, "FORWARD_VALIDATION_INTERVAL_SECONDS", 60) or 60)
    times_worker = [t for t in (_parse(s["at"]) for s in samples if s.get("loop_running")) if t]
    times_fresh = [t for t in (_parse(s["at"]) for s in samples if s.get("account_status") == "FRESH") if t]
    worker_cov = coverage(times_worker, o, end, cadence) if end > o else None
    fresh_cov = coverage(times_fresh, o, end, cadence) if end > o else None
    ev_times = [t for t in (_parse(r["evaluated_at"]) for r in q("SELECT evaluated_at FROM setup_evaluations WHERE evaluated_at >= ? AND evaluated_at < ? AND trigger != 'TEST' "
                                                                  "AND decision NOT IN ('ERROR', 'PAUSED')", (start_s, end_s))) if t]
    setup_cov = coverage(ev_times, o, end, float(getattr(config, "SETUP_EVAL_INTERVAL_SECONDS", 60) or 60)) if end > o else None
    dec = q("SELECT decision, source, cohort_id, candidate_id, recovered, decided_at, observed_at, reason_codes_json, mode, market_session FROM forward_capture_decisions "
            "WHERE trading_date = ?", (td,))
    faults = q("SELECT code, component FROM forward_capture_faults WHERE trading_date = ?", (td,))
    fault_codes: Dict[str, int] = {}
    for f in faults:
        fault_codes[f["code"]] = fault_codes.get(f["code"], 0) + 1
    runs = q("SELECT result FROM execution_runs WHERE started_at >= ? AND started_at < ?", (day_lo, day_hi))
    cycle_errors = sum(1 for r in runs if r.get("result") == "ERROR")
    acct_fail = q("SELECT count(*) AS n FROM account_refresh_attempts WHERE started_at >= ? AND started_at < ? AND result != 'SUCCESS'", (start_s, end_s))
    broker_fail = int(acct_fail[0]["n"]) if acct_fail else 0
    recon = q("SELECT count(*) AS n FROM execution_reconciliations WHERE at >= ? AND at < ?", (day_lo, day_hi))
    restarts = cap.fv.store.restarts(trading_date=td)
    in_session_restart = [r for r in restarts if start_s <= str(r.get("at") or "") < end_s]
    ctx_rows = q("SELECT unified_state, context_json FROM forward_capture_context WHERE at >= ? AND at < ?", (start_s, end_s))
    partial_gate_on = sum(1 for r in ctx_rows if '"enabled":true' in (r.get("context_json") or "") and r.get("unified_state") not in ("COMPLETE",))
    ext_unavailable = sum(1 for r in ctx_rows if r.get("unified_state") in ("UNAVAILABLE",))
    disarms = q("SELECT code, at, detail_json FROM autorun_events WHERE trading_date = ? AND kind = 'AUTO_DISARM'", (td,))
    disarm_codes = {c for e in disarms for c in ([e["code"]] + list((json.loads(e.get("detail_json") or "{}") or {}).get("codes") or []))}
    lease_lost = q("SELECT count(*) AS n FROM autorun_events WHERE trading_date = ? AND kind = 'LEASE_LOST'", (td,))
    other_instance = bool(disarm_codes & {"DUPLICATE_INSTANCE", "FOREIGN_INSTANCE"}) or bool(lease_lost and int(lease_lost[0]["n"] or 0))
    for pf in q("SELECT checks_json FROM autorun_preflights WHERE at >= ? AND at < ?", (start_s, end_s)):
        for ch in json.loads(pf.get("checks_json") or "[]"):
            if ch.get("status") == "FAIL" and (ch.get("id") == "foreign_orders" or (ch.get("id") == "single_instance" and "this process is passive" not in str(ch.get("text")))):
                other_instance = True                                     # another bot on the account / another instance on the database (seen in Monitor only too)
    arms = paper_auto_spans(q, o, c)
    before_open = q("SELECT cohort_id FROM forward_capture_config_snapshots WHERE at <= ? ORDER BY at DESC, rowid DESC LIMIT 1", (start_s,))
    during = q("SELECT cohort_id FROM forward_capture_config_snapshots WHERE at > ? AND at < ?", (start_s, end_s))
    cohorts = sorted({r["cohort_id"] for r in before_open + during if r.get("cohort_id")})
    inconsistent = sum(1 for d in dec if (_parse(d["observed_at"]) and _parse(d["decided_at"]) and (_parse(d["decided_at"]) - _parse(d["observed_at"])).total_seconds() > 120))
    exec_times: Dict[str, List[datetime]] = {}
    for d in dec:
        if d["source"] in ("EXECUTOR_CYCLE", "INTENT") and d.get("candidate_id"):
            t = _parse(d["decided_at"])
            if t:
                exec_times.setdefault(d["candidate_id"], []).append(t)
    cyc = float(getattr(config, "EXECUTION_CYCLE_SECONDS", 30) or 30)
    near = int(getattr(config, "EXECUTION_NEAR_CLOSE_MINUTES", 15) or 15)
    ready_gap = 0
    for d in dec:
        t = _parse(d["decided_at"])
        if d["decision"] != "READY" or d.get("mode") != "PAPER_AUTO" or t is None or d.get("market_session") != "REGULAR":
            continue
        if t > c - timedelta(minutes=near) or t > now - timedelta(seconds=2 * cyc + 60):
            continue                                                       # inside the near-close cutoff, or too recent to judge
        if not any(t - timedelta(seconds=cyc) <= x <= t + timedelta(seconds=2 * cyc + 30) for x in exec_times.get(d["candidate_id"], [])):
            ready_gap += 1
    last = samples[-1] if samples else None
    closed = now >= c
    unresolved_end = bool(closed and last is not None and (last.get("reconciliation_healthy") == 0 or (last.get("attention_json") or "[]") not in ("[]", "")))
    recovered = sum(1 for d in dec if d.get("recovered"))
    inv, deg = [], []
    if fv_status in ("FAILED", "NOT_RUN") and (closed or fv_status == "FAILED"):
        inv.append("FV_SESSION_FAILED" if fv_status == "FAILED" else "FV_SESSION_NOT_RUN")
    if worker_cov is not None and (end - o).total_seconds() >= 1800 and worker_cov < MIN_COVERAGE:
        inv.append("WORKER_COVERAGE_LOW")
    if fresh_cov is not None and (end - o).total_seconds() >= 1800 and fresh_cov < MIN_COVERAGE:
        inv.append("ACCOUNT_DATA_COVERAGE_LOW")
    if unresolved_end:
        inv.append("UNRESOLVED_RECONCILIATION")
    if fault_codes.get("SINK_OVERFLOW") or fault_codes.get("EVIDENCE_LOST"):
        inv.append("CAPTURE_LOST")
    if fault_codes.get("DUPLICATE_INSTANCE") or other_instance:
        inv.append("DUPLICATE_INSTANCE")
    if inconsistent:
        inv.append("INCONSISTENT_TIMESTAMPS")
    if fv_status == "DEGRADED":
        deg.append("FV_SESSION_DEGRADED")                                  # the Stage 6.1 session record says so (its notes say why): never HEALTHY over it
    if in_session_restart:
        deg.append("RESTART_IN_SESSION")
    if recovered or fault_codes.get("ENTRY_RECOVERED"):
        deg.append("RECOVERED_CAPTURE")
    if any(k not in INVALID_FAULTS + ("ENTRY_RECOVERED",) for k in fault_codes):
        deg.append("CAPTURE_FAULTS")
    if cycle_errors or any("CYCLE_ERROR" in (d.get("reason_codes_json") or "") for d in dec):
        deg.append("CYCLE_ERRORS")
    if broker_fail:
        deg.append("BROKER_READ_FAILURES")
    if ext_unavailable:
        deg.append("DATA_SOURCE_FAILURES")
    if len(cohorts) > 1:
        deg.append("MIXED_COHORTS")
    if partial_gate_on:
        deg.append("PARTIAL_EXTERNAL_CONTEXT")
    if disarms:
        deg.append("AUTO_DISARM")
    if setup_cov is not None and (end - o).total_seconds() >= 1800 and setup_cov < SETUP_COVERAGE:
        deg.append("SETUP_EVALUATION_GAPS")
    if ready_gap:
        deg.append("READY_WITHOUT_EXECUTOR_DECISION")
    status = INVALID if inv else DEGRADED if deg else HEALTHY
    entered_at: Dict[str, str] = {}
    for d in dec:
        if d["decision"] == "ENTRY_INTENT" and d.get("candidate_id"):
            entered_at[d["candidate_id"]] = min(entered_at.get(d["candidate_id"], d["decided_at"]), d["decided_at"])
    counts: Dict[str, set] = {}
    for d in dec:
        if d["decision"] == "BLOCKED" and d.get("candidate_id") in entered_at and d["decided_at"] >= entered_at[d["candidate_id"]]:
            continue
        counts.setdefault(d["decision"], set()).add(d.get("candidate_id"))
    ready_set = set()
    for d in dec:
        if d["decision"] in ("READY", "ENTRY_INTENT") or (d["decision"] == "SKIPPED") or (d["decision"] == "BLOCKED"):
            ready_set.add(d.get("candidate_id"))
    intents = q("SELECT intent_type, state, attempt_count FROM execution_intents WHERE created_at >= ? AND created_at < ?", (day_lo, day_hi))
    metrics = {"window": {"open": start_s, "close": end_s, "expected_minutes": minutes, "measured_until": _iso(end)}, "fv_session_status": fv_status,
               "startup_at": (cap.store.query("SELECT MIN(at) AS a FROM forward_capture_config_snapshots WHERE trading_date = ? AND trigger IN ('STARTUP', 'DAY')", (td,)) or [{}])[0].get("a"),
               "worker_coverage": worker_cov, "account_fresh_coverage": fresh_cov, "setup_evaluation_coverage": setup_cov, "observer_samples": len(samples),
               "cycles_expected": int(minutes * 60 / cyc), "cycle_runs_recorded": len(runs), "cycle_errors": cycle_errors, "broker_read_failures": broker_fail,
               "reconciliation_findings": int(recon[0]["n"]) if recon else 0, "data_source_failures": ext_unavailable,
               "paper_auto": [{"from": _iso(s), "to": _iso(e)} for s, e in arms], "paper_auto_minutes": round(sum((e - s).total_seconds() for s, e in arms) / 60, 1),
               "auto_disarms": [{"code": e["code"], "at": e["at"]} for e in disarms], "restarts_in_session": len(in_session_restart), "other_instance": other_instance,
               "candidates_observed": len({d.get("candidate_id") for d in dec if d.get("candidate_id")}), "ready_candidates": len(ready_set - {None}),
               "decisions": {k: len(v - {None}) for k, v in counts.items()}, "decision_rows": len(dec),
               "entries_attempted": sum(1 for i in intents if i["intent_type"] == "ENTRY" and int(i["attempt_count"] or 0) >= 1),
               "entries_blocked": len(counts.get("BLOCKED", set()) - {None}), "orders": len(intents),
               "fills": sum(1 for i in intents if i["state"] == "FILLED"), "exits": len(counts.get("EXITED", set()) - {None}),
               "open_positions_at_end": last.get("open_positions") if last else None, "recovered_decisions": recovered, "faults": fault_codes,
               "ready_without_executor_decision": ready_gap, "inconsistent_timestamps": inconsistent, "cohorts": cohorts}
    incidents = [REASON_TEXT.get(r, r) for r in inv + deg]
    summary = "\n".join([f"Market session: {status}", f"Candidates observed: {metrics['candidates_observed']}", f"READY: {metrics['ready_candidates']}",
                         f"Blocked: {metrics['entries_blocked']}", f"Entered: {len(counts.get('ENTERED', set()) - {None})}",
                         f"Open: {metrics['open_positions_at_end'] if metrics['open_positions_at_end'] is not None else '-'}",
                         f"Closed: {metrics['exits']}",
                         "Paper Auto: " + (", ".join(f"{_pt(s)}-{_pt(e)} PT" for s, e in arms) if arms else "off"),
                         "Incidents: " + ("; ".join(incidents) if incidents else "none")])
    final = bool(fv_status not in ("OPEN",) and fv_row is not None and now >= c + timedelta(seconds=FINALIZE_AFTER_S)) or (fv_row is None and now >= c + timedelta(days=1))
    return {"trading_date": td, "status": status, "final": final, "reasons": inv + deg, "metrics": metrics, "summary_text": summary, "cohort_ids": cohorts,
            "source_session_status": fv_status}


def _refresh_daily(cap, d: str, now: datetime) -> None:
    """The Stage 6.1 daily summary of `d` was stored at the close, before this record was final: rebuild it now (derived, upserted)."""
    try:
        if cap.fv.store.daily(d) is not None:
            from forward_validation import VERSION as FV_VERSION
            cap.fv.store.put_daily(d, now, FV_VERSION, cap.fv.daily_summary(d, now))
    except Exception:  # noqa: BLE001 - reporting only; the routes still read the final record
        pass


def update(cap, now: datetime) -> Optional[Dict[str, Any]]:
    """Upsert today's record and finalize earlier open ones (write: observer thread only). Returns today's status."""
    from autorun import calendar as CAL
    from forward_capture.capture import trading_date
    td = trading_date(_iso(now))
    days = [r["trading_date"] for r in cap.store.query("SELECT trading_date FROM forward_capture_session_health WHERE final = 0 AND trading_date < ? ORDER BY trading_date", (td,))]
    last = cap.store.query("SELECT MAX(trading_date) AS d FROM forward_capture_session_health")
    if last and last[0]["d"] and last[0]["d"] < td:                       # market days with no record at all (the process was not running): recorded, never skipped
        d = date.fromisoformat(last[0]["d"]) + timedelta(days=1)
        while d.isoformat() < td and len(days) < 60:
            if CAL.is_market_day(d) and d.isoformat() not in days:
                days.append(d.isoformat())
            d += timedelta(days=1)
    out = None
    for d in days + [td]:
        rec = compute(cap, d, now)
        if rec is None:
            continue
        was = cap.store.health(d)
        cap.store.put_health({**rec, "reasons_json": rec["reasons"], "metrics_json": rec["metrics"], "cohort_ids_json": rec["cohort_ids"], "computed_at": _iso(now), "version": VERSION})
        if rec["final"] and not (was or {}).get("final"):
            _refresh_daily(cap, d, now)
        if d == td:
            out = {"trading_date": td, "status": rec["status"], "final": rec["final"]}
    return out
