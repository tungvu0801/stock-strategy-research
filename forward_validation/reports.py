"""
forward_validation/reports.py - deterministic plain-text reports and exports of Stage 6.1 (DESIGN_61 sections 12-13). No marketing language,
no claim: the same persisted rows always produce the same text and the same CSV / JSON.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Sequence, Tuple

from forward_capture import EXPORT_KINDS as CAPTURE_EXPORTS   # Stage 6.10A: the forward dataset's exports share the Stage 6.1 export route
from forward_validation import EXPORTS, INSUFFICIENT_TEXT, VERSION

SESSION_COLS = ("trading_date", "session_status", "notes_code", "started_at", "ended_at", "bot_start_time", "bot_stop_time", "market_open_seen", "market_close_seen", "paper_auto_enabled",
                "account_refresh_healthy", "cash_flow_healthy", "reconciliation_healthy", "in_session_samples", "created_at", "updated_at")
TRADE_COLS = ("position_id", "execution_id", "proposal_id", "setup_id", "symbol", "side", "trading_date", "opened_at", "closed_at", "qty", "entry_price", "exit_price", "realized_pnl",
              "gross_pnl", "return_pct", "holding_sessions", "exit_reason", "entry_reference_price", "entry_slippage_absolute", "entry_slippage_bps", "exit_reference_price",
              "exit_slippage_absolute", "exit_slippage_bps", "mfe_pnl", "mae_pnl", "mfe_pct", "mae_pct", "marks", "stretch", "confidence", "demand", "evidence_strength", "risk_per_share",
              "risk_budget", "proposal_qty", "invalidation_price", "entry_broker_order_id",
              "entry_controls_version", "unified_exposure_gate_enabled", "unified_exposure_result",   # Stage 6.9.1: the entry-control regime of each trade
              "candidate_id", "entry_decision_id", "cohort_id",   # Stage 6.10A: the trade's forward lineage (candidate -> decision -> intent -> position) and cohort
              "verified")                                          # Wave 2 PR A: an unverified trade's P/L cells are empty (never 0); appended, so the order above is unchanged
DAILY_COLS = ("trading_date", "session_status", "notes_code", "universe_scanned", "WATCH", "READY", "REVIEWABLE", "BLOCKED", "ENTRY_SUBMITTED", "ENTRY_FILLED", "ENTRY_PARTIAL", "POSITION_OPEN",
              "EXIT_REQUIRED", "EXIT_SUBMITTED", "EXIT_FILLED", "CLOSED", "ATTENTION", "rejections", "partials", "duplicates", "reconciliation_issues", "safety_fires", "realized_pnl",
              "max_drawdown", "max_drawdown_pct", "account_max_age_s", "cash_flow_max_age_s", "refresh_failures", "broker_errors", "restarts", "computed_at",
              "pnl_status", "unverified")                          # Wave 2 PR A: the day's P/L status and its unverified trades (appended)
EVENT_COLS = ("at", "trading_date", "kind", "code", "detail", "owner")
SAMPLE_COLS = ("at", "trading_date", "in_session", "owner", "mode", "executor_ready", "safety_state", "allowed_new_entry", "reconciliation_healthy", "attention_count", "account_status",
               "account_age_s", "equity", "net_external_cum", "cash_flow_status", "cash_flow_age_s", "open_positions", "pending_entries", "pending_exits", "last_cycle_at", "last_cycle_result",
               "loop_running")
RESTART_COLS = ("at", "trading_date", "previous_owner", "previous_sample_at", "owner", "broker_queried", "reconciliation_result", "duplicate_orders", "position_continuity")


def _flat_trade(t: dict) -> dict:
    a, es, xs, ex = t.get("attribution") or {}, t.get("entry_slippage") or {}, t.get("exit_slippage") or {}, t.get("excursions") or {}
    ec = t.get("entry_controls") or {}
    g = ec.get("unified_external_exposure") or {}
    return {**{k: t.get(k) for k in TRADE_COLS if k in t}, "entry_controls_version": ec.get("version"), "unified_exposure_gate_enabled": g.get("enabled"),
            "unified_exposure_result": g.get("result"), "entry_reference_price": es.get("reference_price"), "entry_slippage_absolute": es.get("slippage_absolute"),
            "entry_slippage_bps": es.get("slippage_bps"), "exit_reference_price": xs.get("reference_price"), "exit_slippage_absolute": xs.get("slippage_absolute"),
            "exit_slippage_bps": xs.get("slippage_bps"), "mfe_pnl": ex.get("mfe_pnl"), "mae_pnl": ex.get("mae_pnl"), "mfe_pct": ex.get("mfe_pct"), "mae_pct": ex.get("mae_pct"),
            "marks": ex.get("marks"), "stretch": a.get("stretch"), "confidence": a.get("confidence"), "demand": a.get("demand"), "evidence_strength": a.get("evidence_strength"),
            "risk_per_share": a.get("risk_per_share"), "risk_budget": t.get("attribution", {}).get("risk_budget"), "proposal_qty": a.get("proposal_qty"),
            "invalidation_price": a.get("invalidation_price")}


def _flat_daily(d: dict) -> dict:
    f, dd, fr, bh = d.get("funnel") or {}, d.get("drawdown") or {}, d.get("freshness") or {}, d.get("broker_health") or {}
    return {"trading_date": d.get("trading_date"), "session_status": d.get("session_status"), "notes_code": (d.get("session") or {}).get("notes_code"), **{k: f.get(k) for k in f},
            "rejections": d.get("rejections"), "partials": d.get("partials"), "duplicates": d.get("duplicates"), "reconciliation_issues": d.get("reconciliation_issues"),
            "safety_fires": d.get("safety_fires"), "realized_pnl": d.get("realized_pnl"), "max_drawdown": dd.get("max_drawdown"), "max_drawdown_pct": dd.get("max_drawdown_pct"),
            "account_max_age_s": (fr.get("account") or {}).get("max_age_s"), "cash_flow_max_age_s": (fr.get("cash_flow") or {}).get("max_age_s"),
            "refresh_failures": ((fr.get("account") or {}).get("refresh_failures") or 0) + ((fr.get("cash_flow") or {}).get("refresh_failures") or 0), "broker_errors": bh.get("total_errors"),
            "restarts": d.get("restarts"), "computed_at": d.get("computed_at"), "pnl_status": d.get("pnl_status"), "unverified": d.get("unverified")}


def _lineage(service, t: dict) -> dict:
    """Stage 6.10A: the forward ids of a closed trade - its candidate, the entry decision recorded at intent creation and the entry's cohort
    (LEGACY when the entry predates the capture: no decision-time record exists and none is invented)."""
    from forward_capture.store import fid
    snap = service.capture.store.entry_snapshot(t["execution_id"]) if t.get("execution_id") else None
    return {"candidate_id": fid("candidate", t["setup_id"]) if t.get("setup_id") else None, "entry_decision_id": (snap or {}).get("decision_id"),
            "cohort_id": (snap or {}).get("cohort_id") or "LEGACY"}


def export_rows(service, kind: str) -> Tuple[Sequence[str], List[dict]]:
    """(columns, rows) of one export. Deterministic order: by trading date / time. No secret, no credential, no raw broker description."""
    if kind in CAPTURE_EXPORTS:                                                        # Stage 6.10A: read-only rows of the forward dataset
        from forward_capture.exports import export_rows as capture_rows
        cols, rows = capture_rows(service.capture.store, kind)
        if kind != "outcomes":
            return cols, rows
        xs = service.execution.store                                                   # Wave 2 PR C (C-84c): verified and a verified-only pnl, joined by position_id;
        out = []                                                                       # the raw `outcome` and its `outcome_sha256` are never altered
        for r in rows:
            try:
                p = xs.position(r.get("position_id")) if r.get("position_id") else None
            except Exception:  # noqa: BLE001 - an unreadable position is unverified with no pnl, never a 500; the raw columns are kept
                p = None
            ok = bool(p is not None and service.verification(p).get("verified"))
            out.append({**r, "verified": ok, "pnl": p.realized_pnl if ok else None})
        return tuple(cols) + ("verified", "pnl"), out
    if kind not in EXPORTS:
        raise ValueError(f"unknown export {kind!r}; one of {', '.join(EXPORTS)}")
    st = service.store
    run = service._run_block() if kind in ("trades", "daily") else None              # Wave 2 PR C: the run columns (run_id, member, exclusion_reason, time_source)
    rid = (run or {}).get("run_id")
    if kind == "sessions":
        return SESSION_COLS, [{k: s.get(k) for k in SESSION_COLS} for s in st.sessions(limit=100000, newest_first=False)]
    if kind == "trades":
        rows = sorted(service.closed_trades(limit=100000), key=lambda t: (t.get("closed_at") or "", t["position_id"]))
        tr = {r["position_id"]: r for r in ((run or {}).get("trades") or {}).get("rows") or []}
        return TRADE_COLS + ("run_id", "member", "exclusion_reason", "time_source"), [
            {**_flat_trade(t), **_lineage(service, t), "run_id": rid if t["position_id"] in tr else None, "member": (tr[t["position_id"]]["status"] == "member") if t["position_id"] in tr else None,
             "exclusion_reason": (tr.get(t["position_id"]) or {}).get("exclusion"), "time_source": (tr.get(t["position_id"]) or {}).get("time_source")} for t in rows]
    if kind == "daily":
        rows = service.fresh_dailies(limit=100000, newest_first=False)              # Wave 2 PR A: each stored day current, else computed fresh (never written here)
        ss = (run or {}).get("sessions") or {}
        members, nonmember, pend = set(ss.get("member_dates") or []), ss.get("nonmember") or {}, set(ss.get("pending_dates") or [])
        overlap = set(ss.get("overlap_dates") or [])
        return DAILY_COLS + ("run_id", "member", "exclusion_reason", "time_source"), [
            {**_flat_daily(d), "run_id": rid if d.get("trading_date") in overlap else None, "member": (d.get("trading_date") in members) if d.get("trading_date") in overlap else None,
             "exclusion_reason": nonmember.get(d.get("trading_date")) or ("pending" if d.get("trading_date") in pend else None), "time_source": None} for d in rows]
    if kind == "control_events":
        return EVENT_COLS, [{**{k: e.get(k) for k in EVENT_COLS if k != "detail"}, "detail": json.dumps(e.get("detail") or {}, sort_keys=True)} for e in st.events(limit=100000, newest_first=False)]
    if kind == "samples":
        return SAMPLE_COLS, [{k: s.get(k) for k in SAMPLE_COLS} for s in st.samples(limit=1000000)]
    return RESTART_COLS, [{k: r.get(k) for k in RESTART_COLS} for r in sorted(st.restarts(limit=100000), key=lambda r: r["at"])]


def to_csv(columns: Sequence[str], rows: List[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else (json.dumps(r[k], sort_keys=True) if isinstance(r[k], (dict, list)) else r[k])) for k in columns})
    return buf.getvalue()


def to_json(kind: str, columns: Sequence[str], rows: List[dict]) -> str:
    return json.dumps({"version": VERSION, "export": kind, "columns": list(columns), "rows": rows}, sort_keys=True, default=str, indent=1)


# ---------------------------------------------------------------- the daily report -----------------------------------------------------
def _v(x, unit: str = "") -> str:
    return "not available" if x is None else f"{x}{unit}"


def _line(label: str, value: Any) -> str:
    return f"  {label:<34} {_v(value)}"


def _pnl_text(s: Dict[str, Any]) -> str:
    """Wave 2 PR A (design 3.12.3): a summary's realized P/L as text. Plain only when every closed trade is verified; labelled partial when some
    are not; never a number for an UNSTABLE or status-less summary, and never 0.00 for a day without a verified trade."""
    status, n = s.get("pnl_status"), int(s.get("unverified") or 0)
    if status in ("OK", "NO_TRADES"):
        return _v(s.get("realized_pnl"))
    if status == "UNVERIFIED_PRESENT":
        if s.get("realized_pnl") is None:
            return f"not available (no verified trades; {n} unverified)"
        return f"{s.get('realized_pnl')} (partial: verified trades only; {n} unverified)"
    if status == "UNSTABLE":
        return "not available (accounting changing or incomplete during the read)"
    return "not available (no P/L status)"


def daily_report(summary: Dict[str, Any]) -> str:
    """Session health, Signals, Trades, Safety, Execution issues, P/L, Open positions, Notes - plain text, fixed order, no adjectives."""
    s = summary
    row = s.get("session") or {}
    f, rel, rec, saf, fr, dd, wl = s.get("funnel") or {}, s.get("order_reliability") or {}, s.get("reconciliation") or {}, s.get("safety") or {}, s.get("freshness") or {}, s.get("drawdown") or {}, s.get("win_loss") or {}
    acc, cfr = fr.get("account") or {}, fr.get("cash_flow") or {}
    out = [f"FORWARD PAPER VALIDATION - DAILY REPORT - {s.get('trading_date')}", f"(descriptive; Alpaca PAPER only; generated {s.get('computed_at')}; {VERSION})", "",
           "SESSION HEALTH", _line("Status", s.get("session_status")), _line("Notes", row.get("notes_code") or "none"),
           _line("Market open / close observed", f"{'yes' if row.get('market_open_seen') else 'no'} / {'yes' if row.get('market_close_seen') else 'no'}"),
           _line("Paper Auto enabled in session", "yes" if row.get("paper_auto_enabled") else "no"), _line("Bot first / last seen", f"{_v(row.get('bot_start_time'))} / {_v(row.get('bot_stop_time'))}"),
           _line("In-session observations", row.get("in_session_samples")), _line("Account data max age (s)", acc.get("max_age_s")), _line("Cash-flow data max age (s)", cfr.get("max_age_s")),
           _line("Refresh failures (account / cash)", f"{acc.get('refresh_failures', 0)} / {cfr.get('refresh_failures', 0)}"), _line("Restarts", s.get("restarts")), "",
           "SIGNALS", _line("Universe scanned (symbols)", f.get("universe_scanned")), _line("WATCH transitions", f.get("WATCH")), _line("READY transitions", f.get("READY")),
           _line("Reviewable / blocked proposals", f"{f.get('REVIEWABLE')} / {f.get('BLOCKED')}"), "",
           "TRADES", _line("Entries submitted / filled / partial", f"{f.get('ENTRY_SUBMITTED')} / {f.get('ENTRY_FILLED')} / {f.get('ENTRY_PARTIAL')}"),
           _line("Positions opened", f.get("POSITION_OPEN")), _line("Exits required / submitted / filled", f"{f.get('EXIT_REQUIRED')} / {f.get('EXIT_SUBMITTED')} / {f.get('EXIT_FILLED')}"),
           _line("Positions closed", f.get("CLOSED"))]
    for t in s.get("closed_trades") or []:
        out.append(f"    {t['symbol']}: {t['qty']} sh, entry {t['entry_price']} -> exit {t['exit_price']}, P/L {'unverified' if t.get('verified') is False else _v(t.get('realized_pnl'))}, {t.get('exit_reason')}, {t.get('holding_sessions')} session(s) held")
    out += ["", "SAFETY", _line("Safety fires (into a blocking state)", saf.get("fire_count")), _line("  recovered / persistent", f"{saf.get('recovered')} / {saf.get('persistent')}"),
            _line("Daily-loss halts / emergency stops / pauses", f"{saf.get('daily_loss_halts')} / {saf.get('emergency_stops')} / {saf.get('pauses')}"),
            _line("Entry-control blocks", json.dumps(saf.get("entry_control_blocks") or {}, sort_keys=True) if saf.get("entry_control_blocks") else "none"), "",
            "EXECUTION ISSUES", _line("Duplicate broker orders", rel.get("duplicate_broker_orders")), _line("Rejects / cancels / expired", f"{rel.get('rejects')} / {rel.get('cancels')} / {rel.get('expired_orders')}"),
            _line("Ambiguous submissions", rel.get("ambiguous_submissions")), _line("Not sent / failed", f"{rel.get('not_sent')} / {rel.get('failed')}"),
            _line("Reconciliation findings (blocking / open)", f"{rec.get('findings')} ({rec.get('blocking')} / {rec.get('open')})"), _line("Broker errors", (s.get("broker_health") or {}).get("total_errors")), "",
            "P/L (descriptive, paper)", _line("P/L status", s.get("pnl_status") or "not recorded"), _line("Realized paper P/L", _pnl_text(s)),
            _line("Closed trades (win / loss / flat)", (f"{wl.get('closed_trades')} ({wl.get('win_count')} / {wl.get('loss_count')} / {wl.get('flat_count')})"
                                                        + (" (verified trades only)" if s.get("pnl_status") == "UNVERIFIED_PRESENT" else ""))
                  if s.get("pnl_status") in ("OK", "NO_TRADES", "UNVERIFIED_PRESENT") else "not available"),
            _line("Unrealized paper P/L (priced positions only; informational)", s.get("unrealized_pnl")),   # C-83: never a partial total unlabelled
            _line("Max drawdown (account curve; informational)", f"{_v(dd.get('max_drawdown'))} ({_v(dd.get('max_drawdown_pct'), ' %')})"), "",   # Wave 2 PR C (C-57): never an edge input
            "OPEN POSITIONS", _line("Open at end of observation", s.get("open_positions_at_end")), ""]
    fc = s.get("forward_capture") or {}
    if fc.get("summary_text"):                                                         # Stage 6.10A: the day's evidence health (operational only)
        out += ["FORWARD EVIDENCE (operational health, not a strategy result)"] + ["  " + ln for ln in str(fc["summary_text"]).splitlines()]             + [_line("Record", "final" if fc.get("final") else "in progress"), ""]
    out.append("NOTES")
    for a in s.get("attention") or []:
        out.append(f"  attention: {a.get('code')} at {a.get('at')}")
    out.append(f"  {INSUFFICIENT_TEXT}: one session is never a strategy conclusion.")
    out.append("  No threshold, rule, size or exit was changed. Alpaca PAPER only; no real money.")
    return "\n".join(out) + "\n"


def weekly_report(cumulative: Dict[str, Any], dailies: List[Dict[str, Any]], sessions_needed: int = 5) -> str:
    """After at least `sessions_needed` closed sessions: health, trade funnel, execution quality, safety, P/L, drawdown, exit reasons."""
    c = cumulative
    run = c.get("run") or {}                                                            # Wave 2 PR C (C-88, C-35): the gate counts the run's closed member sessions that
    members = set((run.get("sessions") or {}).get("member_dates") or [])               # are not unreadable; every edge label and drawdown shown comes from the run block

    def unreadable(d):
        return d.get("session_status") == "DEGRADED" and "EVIDENCE_UNREADABLE" in ((d.get("session") or {}).get("notes") or [])
    closed = [d for d in dailies if d.get("session_status") in ("HEALTHY", "DEGRADED", "FAILED", "NOT_RUN") and d.get("trading_date") in members and not unreadable(d)]
    out = [f"FORWARD PAPER VALIDATION - WEEKLY REPORT - as of {c.get('as_of')}", f"(descriptive; Alpaca PAPER only; {VERSION})", ""]
    if len(closed) < sessions_needed:
        out += [f"Not yet available: {len(closed)} closed member session(s) of the forward run of the {sessions_needed} needed for a weekly aggregate"
                + ("" if run.get("defined") else " (no forward run defined)") + ".", ""]
        return "\n".join(out) + "\n"
    week = closed[-sessions_needed:]
    st = {k: sum(1 for d in week if d.get("session_status") == k) for k in ("HEALTHY", "DEGRADED", "FAILED", "NOT_RUN")}
    span = [d for d in dailies if week[0].get("trading_date") <= str(d.get("trading_date")) <= week[-1].get("trading_date")]
    own = {"unreadable": sum(1 for d in span if unreadable(d)), "failed": sum(1 for d in span if d.get("session_status") == "FAILED")}   # P-09: their own columns
    f_sum = {k: sum(int((d.get("funnel") or {}).get(k) or 0) for d in week) for k in ("universe_scanned", "WATCH", "READY", "REVIEWABLE", "BLOCKED", "ENTRY_SUBMITTED", "ENTRY_FILLED", "POSITION_OPEN", "CLOSED", "ATTENTION")}
    pnl = sum((float(d.get("realized_pnl")) for d in week if d.get("realized_pnl") not in (None, "")), 0.0)
    bad = [str(d.get("trading_date")) for d in week if d.get("pnl_status") not in ("OK", "NO_TRADES", "UNVERIFIED_PRESENT")]   # Wave 2 PR A: never summed silently
    unv, verified_n = sum(int(d.get("unverified") or 0) for d in week), sum(int((d.get("win_loss") or {}).get("closed_trades") or 0) for d in week)
    pnl_text = (f"not available (P/L unstable or unknown on {', '.join(bad)})" if bad else f"not available (no verified trades; {unv} unverified)" if unv and not verified_n
                else f"{pnl:.2f} (partial: verified trades only; {unv} unverified)" if unv else f"{pnl:.2f}")
    s, o = c.get("strategy") or {}, c.get("operational") or {}
    lab = f" (verified trades only; {s.get('unverified')} unverified)" if s.get("unverified") else ""
    out += ["HEALTH (last %d sessions: %s .. %s)" % (sessions_needed, week[0].get("trading_date"), week[-1].get("trading_date")),
            _line("Healthy / degraded / failed / not run", f"{st['HEALTHY']} / {st['DEGRADED']} / {st['FAILED']} / {st['NOT_RUN']}"),
            _line("Unreadable / failed days in the span", f"{own['unreadable']} / {own['failed']}"),
            _line("Sessions completed overall", (c.get("sessions") or {}).get("label")), _line("Forward run", (run.get("sessions") or {}).get("label")),
            _line("Forward run readiness", (run.get("readiness") or {}).get("text") or "No forward run defined"),   # C-37: both blocks wherever readiness shows
            _line("All-time record", (c.get("all_time") or {}).get("text") or "unknown"), "",
            "TRADE FUNNEL (sum of the week)", *[_line(k, v) for k, v in f_sum.items()], "",
            "EXECUTION QUALITY (week)", _line("Duplicate broker orders", sum(int(d.get("duplicates") or 0) for d in week)), _line("Rejections", sum(int(d.get("rejections") or 0) for d in week)),
            _line("Partial fills", sum(int(d.get("partials") or 0) for d in week)), _line("Reconciliation issues", sum(int(d.get("reconciliation_issues") or 0) for d in week)),
            _line("Restarts", sum(int(d.get("restarts") or 0) for d in week)), "",
            "SAFETY (week)", _line("Safety fires", sum(int(d.get("safety_fires") or 0) for d in week)), "",
            "P/L (descriptive, paper)", _line("Realized paper P/L (week)", pnl_text), _line("Closed trades overall", _v(s.get("closed_trades")) + lab),
            _line("Win rate overall (%)", _v(s.get("win_rate")) + lab), _line("Average win / loss", f"{_v(s.get('average_win'))} / {_v(s.get('average_loss'))}" + lab),
            _line("Expectancy per trade", _v(s.get("expectancy")) + lab),
            _line("Strategy status", f"{(run.get('strategy') or {}).get('edge_label')}"
                  + (f" - {INSUFFICIENT_TEXT}" if (run.get("strategy") or {}).get("edge_status") == "INSUFFICIENT_DATA" else "")), "",
            "DRAWDOWN (forward run, bot-only)", _line("Max drawdown", (run.get("drawdown") or {}).get("text") or "not available"),
            _line("Account curve (informational)", f"{_v((s.get('drawdown') or {}).get('max_drawdown'))} ({_v((s.get('drawdown') or {}).get('max_drawdown_pct'), ' %')})"), "",
            "EXIT REASONS (overall)", *[_line(k, v) for k, v in sorted((s.get("exit_reasons") or {}).items())], "",
            "OPERATIONAL STATUS", _line("Status", o.get("status")), _line("Live-host attempts / early stops", f"{o.get('live_host_attempts')} / {o.get('early_stops')}"), "",
            "No threshold, rule, size or exit was changed. These numbers describe a small paper sample; they are not a forecast."]
    return "\n".join(out) + "\n"
