"""
forward_validation/reports.py - deterministic plain-text reports and exports of Stage 6.1 (DESIGN_61 sections 12-13). No marketing language,
no claim: the same persisted rows always produce the same text and the same CSV / JSON.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Sequence, Tuple

from forward_validation import EXPORTS, INSUFFICIENT_TEXT, VERSION

SESSION_COLS = ("trading_date", "session_status", "notes_code", "started_at", "ended_at", "bot_start_time", "bot_stop_time", "market_open_seen", "market_close_seen", "paper_auto_enabled",
                "account_refresh_healthy", "cash_flow_healthy", "reconciliation_healthy", "in_session_samples", "created_at", "updated_at")
TRADE_COLS = ("position_id", "execution_id", "proposal_id", "setup_id", "symbol", "side", "trading_date", "opened_at", "closed_at", "qty", "entry_price", "exit_price", "realized_pnl",
              "gross_pnl", "return_pct", "holding_sessions", "exit_reason", "entry_reference_price", "entry_slippage_absolute", "entry_slippage_bps", "exit_reference_price",
              "exit_slippage_absolute", "exit_slippage_bps", "mfe_pnl", "mae_pnl", "mfe_pct", "mae_pct", "marks", "stretch", "confidence", "demand", "evidence_strength", "risk_per_share",
              "risk_budget", "proposal_qty", "invalidation_price", "entry_broker_order_id")
DAILY_COLS = ("trading_date", "session_status", "notes_code", "universe_scanned", "WATCH", "READY", "REVIEWABLE", "BLOCKED", "ENTRY_SUBMITTED", "ENTRY_FILLED", "ENTRY_PARTIAL", "POSITION_OPEN",
              "EXIT_REQUIRED", "EXIT_SUBMITTED", "EXIT_FILLED", "CLOSED", "ATTENTION", "rejections", "partials", "duplicates", "reconciliation_issues", "safety_fires", "realized_pnl",
              "max_drawdown", "max_drawdown_pct", "account_max_age_s", "cash_flow_max_age_s", "refresh_failures", "broker_errors", "restarts", "computed_at")
EVENT_COLS = ("at", "trading_date", "kind", "code", "detail", "owner")
SAMPLE_COLS = ("at", "trading_date", "in_session", "owner", "mode", "executor_ready", "safety_state", "allowed_new_entry", "reconciliation_healthy", "attention_count", "account_status",
               "account_age_s", "equity", "net_external_cum", "cash_flow_status", "cash_flow_age_s", "open_positions", "pending_entries", "pending_exits", "last_cycle_at", "last_cycle_result",
               "loop_running")
RESTART_COLS = ("at", "trading_date", "previous_owner", "previous_sample_at", "owner", "broker_queried", "reconciliation_result", "duplicate_orders", "position_continuity")


def _flat_trade(t: dict) -> dict:
    a, es, xs, ex = t.get("attribution") or {}, t.get("entry_slippage") or {}, t.get("exit_slippage") or {}, t.get("excursions") or {}
    return {**{k: t.get(k) for k in TRADE_COLS if k in t}, "entry_reference_price": es.get("reference_price"), "entry_slippage_absolute": es.get("slippage_absolute"),
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
            "restarts": d.get("restarts"), "computed_at": d.get("computed_at")}


def export_rows(service, kind: str) -> Tuple[Sequence[str], List[dict]]:
    """(columns, rows) of one export. Deterministic order: by trading date / time. No secret, no credential, no raw broker description."""
    if kind not in EXPORTS:
        raise ValueError(f"unknown export {kind!r}; one of {', '.join(EXPORTS)}")
    st = service.store
    if kind == "sessions":
        return SESSION_COLS, [{k: s.get(k) for k in SESSION_COLS} for s in st.sessions(limit=100000, newest_first=False)]
    if kind == "trades":
        rows = sorted(service.closed_trades(limit=100000), key=lambda t: (t.get("closed_at") or "", t["position_id"]))
        return TRADE_COLS, [_flat_trade(t) for t in rows]
    if kind == "daily":
        rows = st.dailies(limit=100000, newest_first=False)
        return DAILY_COLS, [_flat_daily(d) for d in rows]
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
        out.append(f"    {t['symbol']}: {t['qty']} sh, entry {t['entry_price']} -> exit {t['exit_price']}, P/L {_v(t.get('realized_pnl'))}, {t.get('exit_reason')}, {t.get('holding_sessions')} session(s) held")
    out += ["", "SAFETY", _line("Safety fires (into a blocking state)", saf.get("fire_count")), _line("  recovered / persistent", f"{saf.get('recovered')} / {saf.get('persistent')}"),
            _line("Daily-loss halts / emergency stops / pauses", f"{saf.get('daily_loss_halts')} / {saf.get('emergency_stops')} / {saf.get('pauses')}"),
            _line("Entry-control blocks", json.dumps(saf.get("entry_control_blocks") or {}, sort_keys=True) if saf.get("entry_control_blocks") else "none"), "",
            "EXECUTION ISSUES", _line("Duplicate broker orders", rel.get("duplicate_broker_orders")), _line("Rejects / cancels / expired", f"{rel.get('rejects')} / {rel.get('cancels')} / {rel.get('expired_orders')}"),
            _line("Ambiguous submissions", rel.get("ambiguous_submissions")), _line("Not sent / failed", f"{rel.get('not_sent')} / {rel.get('failed')}"),
            _line("Reconciliation findings (blocking / open)", f"{rec.get('findings')} ({rec.get('blocking')} / {rec.get('open')})"), _line("Broker errors", (s.get("broker_health") or {}).get("total_errors")), "",
            "P/L (descriptive, paper)", _line("Realized paper P/L", s.get("realized_pnl")), _line("Closed trades (win / loss / flat)", f"{wl.get('closed_trades')} ({wl.get('win_count')} / {wl.get('loss_count')} / {wl.get('flat_count')})"),
            _line("Unrealized paper P/L", s.get("unrealized_pnl")), _line("Max drawdown (adjusted equity)", f"{_v(dd.get('max_drawdown'))} ({_v(dd.get('max_drawdown_pct'), ' %')})"), "",
            "OPEN POSITIONS", _line("Open at end of observation", s.get("open_positions_at_end")), "", "NOTES"]
    for a in s.get("attention") or []:
        out.append(f"  attention: {a.get('code')} at {a.get('at')}")
    out.append(f"  {INSUFFICIENT_TEXT}: one session is never a strategy conclusion.")
    out.append("  No threshold, rule, size or exit was changed. Alpaca PAPER only; no real money.")
    return "\n".join(out) + "\n"


def weekly_report(cumulative: Dict[str, Any], dailies: List[Dict[str, Any]], sessions_needed: int = 5) -> str:
    """After at least `sessions_needed` closed sessions: health, trade funnel, execution quality, safety, P/L, drawdown, exit reasons."""
    c = cumulative
    closed = [d for d in dailies if d.get("session_status") in ("HEALTHY", "DEGRADED", "FAILED", "NOT_RUN")]
    out = [f"FORWARD PAPER VALIDATION - WEEKLY REPORT - as of {c.get('as_of')}", f"(descriptive; Alpaca PAPER only; {VERSION})", ""]
    if len(closed) < sessions_needed:
        out += [f"Not yet available: {len(closed)} closed session(s) of the {sessions_needed} needed for a weekly aggregate.", ""]
        return "\n".join(out) + "\n"
    week = closed[-sessions_needed:]
    st = {k: sum(1 for d in week if d.get("session_status") == k) for k in ("HEALTHY", "DEGRADED", "FAILED", "NOT_RUN")}
    f_sum = {k: sum(int((d.get("funnel") or {}).get(k) or 0) for d in week) for k in ("universe_scanned", "WATCH", "READY", "REVIEWABLE", "BLOCKED", "ENTRY_SUBMITTED", "ENTRY_FILLED", "POSITION_OPEN", "CLOSED", "ATTENTION")}
    pnl = sum((float(d.get("realized_pnl")) for d in week if d.get("realized_pnl") not in (None, "")), 0.0)
    s, o = c.get("strategy") or {}, c.get("operational") or {}
    out += ["HEALTH (last %d sessions: %s .. %s)" % (sessions_needed, week[0].get("trading_date"), week[-1].get("trading_date")),
            _line("Healthy / degraded / failed / not run", f"{st['HEALTHY']} / {st['DEGRADED']} / {st['FAILED']} / {st['NOT_RUN']}"),
            _line("Sessions completed overall", (c.get("sessions") or {}).get("label")), "",
            "TRADE FUNNEL (sum of the week)", *[_line(k, v) for k, v in f_sum.items()], "",
            "EXECUTION QUALITY (week)", _line("Duplicate broker orders", sum(int(d.get("duplicates") or 0) for d in week)), _line("Rejections", sum(int(d.get("rejections") or 0) for d in week)),
            _line("Partial fills", sum(int(d.get("partials") or 0) for d in week)), _line("Reconciliation issues", sum(int(d.get("reconciliation_issues") or 0) for d in week)),
            _line("Restarts", sum(int(d.get("restarts") or 0) for d in week)), "",
            "SAFETY (week)", _line("Safety fires", sum(int(d.get("safety_fires") or 0) for d in week)), "",
            "P/L (descriptive, paper)", _line("Realized paper P/L (week)", f"{pnl:.2f}"), _line("Closed trades overall", s.get("closed_trades")), _line("Win rate overall (%)", s.get("win_rate")),
            _line("Average win / loss", f"{_v(s.get('average_win'))} / {_v(s.get('average_loss'))}"), _line("Expectancy per trade", s.get("expectancy")),
            _line("Strategy status", f"{s.get('edge_label')}" + (f" - {INSUFFICIENT_TEXT}" if s.get("insufficient") else "")), "",
            "DRAWDOWN (adjusted equity, overall)", _line("Max drawdown", f"{_v((s.get('drawdown') or {}).get('max_drawdown'))} ({_v((s.get('drawdown') or {}).get('max_drawdown_pct'), ' %')})"), "",
            "EXIT REASONS (overall)", *[_line(k, v) for k, v in sorted((s.get("exit_reasons") or {}).items())], "",
            "OPERATIONAL STATUS", _line("Status", o.get("status")), _line("Live-host attempts / early stops", f"{o.get('live_host_attempts')} / {o.get('early_stops')}"), "",
            "No threshold, rule, size or exit was changed. These numbers describe a small paper sample; they are not a forecast."]
    return "\n".join(out) + "\n"
