"""
forward_capture/view.py - the compact READ-ONLY views of the forward dataset (Stage 6.10A). Read-only connections only: a view never runs a
migration, never creates a cohort, a snapshot or a health record, and never calls a broker.

  forward_session(...)  Bot Activity: today's cohort, operational health (live until final), Paper Auto armed / disarmed times, cycles,
                        candidates, READY, blocked, orders, positions, incidents
  evidence(...)         the Stage 6.1 overview: healthy capture sessions, candidate events and closed trades PER COHORT with the existing
                        Stage 6.1 evidence status (INSUFFICIENT_DATA | EARLY_NEGATIVE | MIXED | EARLY_POSITIVE) - descriptive, never an approval
  daily(...)            one trading date's health record and its plain-text summary
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from forward_capture import DISCLAIMER, HEALTHY, VERSION
from forward_capture.health import REASON_TEXT
from forward_capture.store import ForwardCaptureStore

UTC = timezone.utc


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _cohort(store: ForwardCaptureStore, cohort_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cohort_id:
        return None
    c = store.cohort(cohort_id)
    return {"cohort_id": c["cohort_id"], "label": c["label"], "started_at": c["started_at"]} if c else {"cohort_id": cohort_id, "label": cohort_id, "started_at": None}


def current_cohort(store: ForwardCaptureStore) -> Optional[Dict[str, Any]]:
    snaps = store.config_snapshots(limit=1)
    return _cohort(store, snaps[0]["cohort_id"]) if snaps else None


def daily(store: ForwardCaptureStore, trading_date: str) -> Optional[Dict[str, Any]]:
    h = store.health(trading_date)
    if h is None:
        return None
    return {"trading_date": trading_date, "status": h["status"], "final": bool(h["final"]), "reasons": h.get("reasons") or [],
            "reason_text": [REASON_TEXT.get(r, r) for r in h.get("reasons") or []], "summary_text": h.get("summary_text"), "metrics": h.get("metrics") or {},
            "cohort_ids": h.get("cohort_ids") or [], "computed_at": h.get("computed_at"), "source_session_status": h.get("source_session_status")}


def forward_session(store: ForwardCaptureStore, trading_date: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Bot Activity's forward-session block. 'recorded' False until the observer writes the first record of the day."""
    now = now or datetime.now(UTC)
    d = daily(store, trading_date)
    cohort = current_cohort(store)
    day = (store.query("SELECT * FROM autorun_days WHERE trading_date = ?", (trading_date,)) or [None])[0]
    faults = store.faults(trading_date, limit=50)
    m = (d or {}).get("metrics") or {}
    dec = m.get("decisions") or {}
    out = {"version": VERSION, "trading_date": trading_date, "recorded": d is not None, "cohort": cohort,
           "health": {"status": d["status"], "final": d["final"], "reasons": d["reasons"], "reason_text": d["reason_text"], "computed_at": d["computed_at"]} if d else
           {"status": None, "final": False, "reasons": [], "reason_text": [], "computed_at": None,
            "text": "No forward-session record yet today (written by the forward-validation observer during the market day)."},
           "paper_auto": {"spans": m.get("paper_auto") or [], "minutes": m.get("paper_auto_minutes"), "first_armed_at": (day or {}).get("first_armed_at"),
                          "first_armed_by": (day or {}).get("first_armed_by"), "last_disarmed_at": (day or {}).get("last_disarmed_at"),
                          "last_disarmed_by": (day or {}).get("last_disarmed_by"), "last_disarm_code": (day or {}).get("last_disarm_code")},
           "cycles": {"expected": m.get("cycles_expected"), "recorded": m.get("cycle_runs_recorded"), "errors": m.get("cycle_errors"), "worker_coverage": m.get("worker_coverage")},
           "candidates": m.get("candidates_observed"), "ready": m.get("ready_candidates"), "blocked": m.get("entries_blocked"), "skipped": dec.get("SKIPPED"),
           "orders": m.get("orders"), "entries_attempted": m.get("entries_attempted"), "fills": m.get("fills"), "entered": dec.get("ENTERED"), "exited": dec.get("EXITED"),
           "open_positions": m.get("open_positions_at_end"), "account_fresh_coverage": m.get("account_fresh_coverage"),
           "incidents": (d or {}).get("reason_text") or [], "faults": [{"at": f["at"], "component": f["component"], "code": f["code"]} for f in faults[:10]],
           "summary_text": (d or {}).get("summary_text"), "note": "Operational health of the day's evidence only - not a strategy result."}
    return out


def evidence(store: ForwardCaptureStore, fv=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Forward evidence per cohort for the Stage 6.1 overview (read only). The Stage 6.1 edge rule decides the label; the minimums (healthy
    sessions, closed trades) come from the Stage 6.1 configuration - nothing here approves a strategy."""
    from forward_validation import EDGE_LABELS, INSUFFICIENT_TEXT, MIN_TRADES_FOR_EDGE, TARGET_EVENTS, TARGET_SESSIONS
    from forward_validation import metrics as M
    now = now or datetime.now(UTC)
    target_sessions = int(getattr(fv, "target_sessions", TARGET_SESSIONS) or TARGET_SESSIONS)
    min_trades = int(getattr(fv, "min_trades", MIN_TRADES_FOR_EDGE) or MIN_TRADES_FOR_EDGE)
    target_events = int(getattr(fv, "target_events", TARGET_EVENTS) or TARGET_EVENTS)
    healths = store.healths(limit=10000)
    cohorts = store.cohorts()
    dd = None
    try:
        dd = M.drawdown(fv.equity_series()).get("max_drawdown_pct") if fv is not None else None
    except Exception:  # noqa: BLE001 - unreadable drawdown stays unknown (the rule treats it conservatively below)
        dd = None
    rows: List[Dict[str, Any]] = []
    for c in cohorts:
        cid = c["cohort_id"]
        mine = [h for h in healths if cid in (h.get("cohort_ids") or [])]
        days = [h for h in mine if h.get("final")]                        # only FINAL records count (today's open record is reported apart)
        healthy = [h for h in days if h["status"] == HEALTHY]
        ready = store.query("SELECT count(DISTINCT candidate_id) AS n FROM forward_capture_decisions WHERE cohort_id = ? AND decision IN ('READY', 'SKIPPED', 'BLOCKED', 'ENTRY_INTENT') "
                            "AND recovered = 0 AND legacy = 0", (cid,))
        blocked = store.query("SELECT count(DISTINCT d.candidate_id) AS n FROM forward_capture_decisions d WHERE d.cohort_id = ? AND d.decision = 'BLOCKED' "
                              "AND NOT EXISTS (SELECT 1 FROM forward_capture_decisions e WHERE e.candidate_id = d.candidate_id AND e.decision = 'ENTRY_INTENT' "
                              "AND e.decided_at <= d.decided_at)", (cid,))       # a block before (or without) an entry, never its own entry's re-evaluation
        outs = store.query("SELECT outcome_json FROM forward_capture_outcomes WHERE cohort_id = ? AND cross_cohort = 0 ORDER BY closed_at, position_id", (cid,))
        pnls = [(json.loads(o["outcome_json"]) or {}).get("realized_pnl") for o in outs]
        wl = M.win_loss(pnls)
        edge = M.edge_status(wl["closed_trades"], len(healthy), wl["expectancy"], wl["win_rate"], dd if dd is not None else 100, min_trades=min_trades, min_sessions=target_sessions)
        n_ready = int(ready[0]["n"]) if ready else 0
        rows.append({"cohort_id": cid, "label": c["label"], "started_at": c["started_at"], "sessions": {s: sum(1 for h in days if h["status"] == s) for s in ("HEALTHY", "DEGRADED", "INVALID")},
                     "in_progress": sum(1 for h in mine if not h.get("final")),
                     "healthy_sessions": len(healthy), "healthy_target": target_sessions, "ready_candidates": n_ready, "events_target": target_events,
                     "blocked_ready_candidates": int(blocked[0]["n"]) if blocked else 0, "closed_trades": wl["closed_trades"], "min_trades": min_trades,
                     "expectancy": wl["expectancy"], "win_rate": wl["win_rate"], "status": edge["status"], "label_text": EDGE_LABELS[edge["status"]], "reasons": edge["reasons"],
                     "insufficient_text": INSUFFICIENT_TEXT if edge["status"] == "INSUFFICIENT_DATA" else None})
    return {"version": VERSION, "as_of": _iso(now), "cohorts": rows, "current_cohort": current_cohort(store),
            "sessions": {**{s: sum(1 for h in healths if h["status"] == s and h.get("final")) for s in ("HEALTHY", "DEGRADED", "INVALID", "MARKET_CLOSED")},
                         "in_progress": sum(1 for h in healths if not h.get("final"))},
            "counts": {k.replace("forward_capture_", ""): v for k, v in store.counts().items()},
            "note": ("Evidence per cohort: only HEALTHY sessions count toward the minimum, outcomes are counted in the cohort of their entry, and "
                     "trades held across a cohort change are excluded. Descriptive only: no status approves a strategy and nothing is tuned from it."),
            "disclaimer": DISCLAIMER}
