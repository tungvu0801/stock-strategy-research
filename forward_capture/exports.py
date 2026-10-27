"""
forward_capture/exports.py - the Stage 6.10A forward dataset as deterministic, read-only exports, served by the EXISTING route
GET /api/forward-validation/export/{kind} (CSV by default, JSON with ?format=json). Nothing here writes, deletes or edits a row.

  candidates          one row per candidate (setup episode)          decisions        one row per decision (time order, unique id)
  controls            one row per entry-control result of an executor decision (allowlisted; the exposure gate as its verdict)
  session_health      one row per market day (operational only)       entry_snapshots / outcomes   the immutable entry and outcome records
  config_snapshots, cohorts, context, faults                         autorun_events, autorun_preflights, autorun_days   the auto-run records
Rows come from read-only connections; the same database always gives the same text. No credential, account number or other account's holding.
"""
from __future__ import annotations

import json
from typing import List, Sequence, Tuple

from forward_capture import EXPORT_KINDS
from forward_capture.store import ForwardCaptureStore, decode

CANDIDATE_COLS = ("candidate_id", "setup_id", "symbol", "setup_type", "cycle_id", "strategy_id", "strategy_version", "started_at", "first_seen_at", "first_state", "cohort_id", "legacy")
DECISION_COLS = ("decision_id", "candidate_id", "setup_id", "symbol", "cohort_id", "decided_at", "observed_at", "trading_date", "market_session", "mode", "source", "source_ref",
                 "decision", "reason_codes", "setup_state", "evaluation_id", "score", "controls_version", "contract_hash", "execution_id", "position_id", "run_id", "recovered",
                 "legacy", "context_at", "features", "proposal", "account", "exposure", "detail", "content_sha256")
CONTROL_COLS = ("decision_id", "candidate_id", "symbol", "decided_at", "trading_date", "decision", "cohort_id", "controls_version", "contract_hash", "control_id", "result",
                "reason", "input")
HEALTH_COLS = ("trading_date", "status", "final", "reasons", "source_session_status", "cohort_ids", "computed_at", "version", "summary_text", "metrics")
SNAPSHOT_COLS = ("execution_id", "candidate_id", "decision_id", "cohort_id", "captured_at", "recovered", "snapshot_sha256", "snapshot")
OUTCOME_COLS = ("position_id", "execution_id", "candidate_id", "cohort_id", "closed_at", "cross_cohort", "outcome_sha256", "outcome")
CONFIG_COLS = ("snapshot_id", "at", "trigger", "trading_date", "config_hash", "cohort_id", "owner", "config")
COHORT_COLS = ("cohort_id", "label", "cohort_key", "started_at", "definition")
CONTEXT_COLS = ("context_id", "at", "unified_state", "content_sha256", "context")
FAULT_COLS = ("fault_id", "at", "trading_date", "component", "code", "detail")
EVENT_COLS = ("event_id", "at", "trading_date", "kind", "code", "actor", "owner", "detail")
PREFLIGHT_COLS = ("preflight_id", "at", "trigger", "status", "policy", "owner", "checks")
DAY_COLS = ("trading_date", "first_armed_at", "first_armed_by", "last_disarmed_at", "last_disarmed_by", "last_disarm_code", "scheduled_arm_at", "scheduled_arm_result", "updated_at")


def _rows(store: ForwardCaptureStore, sql: str) -> List[dict]:
    return [decode(r) for r in store.query(sql)]


def export_rows(store: ForwardCaptureStore, kind: str) -> Tuple[Sequence[str], List[dict]]:
    if kind not in EXPORT_KINDS:
        raise ValueError(f"unknown export {kind!r}; one of {', '.join(EXPORT_KINDS)}")
    if kind == "candidates":
        return CANDIDATE_COLS, store.query("SELECT * FROM forward_capture_candidates ORDER BY first_seen_at, candidate_id")
    if kind == "decisions":
        return DECISION_COLS, _rows(store, "SELECT * FROM forward_capture_decisions ORDER BY decided_at, decision_id")
    if kind == "controls":
        out = []
        for d in _rows(store, "SELECT * FROM forward_capture_decisions WHERE controls_json IS NOT NULL ORDER BY decided_at, decision_id"):
            for c in d.get("controls") or []:
                out.append({**{k: d.get(k) for k in ("decision_id", "candidate_id", "symbol", "decided_at", "trading_date", "decision", "cohort_id", "controls_version", "contract_hash")},
                            "control_id": c.get("control_id"), "result": c.get("result"), "reason": c.get("reason"), "input": c.get("input")})
        return CONTROL_COLS, out
    if kind == "session_health":
        return HEALTH_COLS, _rows(store, "SELECT * FROM forward_capture_session_health ORDER BY trading_date")
    if kind == "entry_snapshots":
        return SNAPSHOT_COLS, _rows(store, "SELECT * FROM forward_capture_entry_snapshots ORDER BY captured_at, execution_id")
    if kind == "outcomes":
        return OUTCOME_COLS, _rows(store, "SELECT * FROM forward_capture_outcomes ORDER BY closed_at, position_id")
    if kind == "config_snapshots":
        return CONFIG_COLS, _rows(store, "SELECT * FROM forward_capture_config_snapshots ORDER BY at, snapshot_id")
    if kind == "cohorts":
        return COHORT_COLS, _rows(store, "SELECT * FROM forward_capture_cohorts ORDER BY started_at, cohort_id")
    if kind == "context":
        return CONTEXT_COLS, _rows(store, "SELECT * FROM forward_capture_context ORDER BY at, context_id")
    if kind == "faults":
        return FAULT_COLS, _rows(store, "SELECT * FROM forward_capture_faults ORDER BY at, fault_id")
    if kind == "autorun_events":
        return EVENT_COLS, _rows(store, "SELECT * FROM autorun_events ORDER BY at, event_id")
    if kind == "autorun_preflights":
        return PREFLIGHT_COLS, [{**r, "checks": json.loads(r.pop("checks_json") or "[]")} for r in store.query("SELECT * FROM autorun_preflights ORDER BY at, preflight_id")]
    return DAY_COLS, store.query("SELECT * FROM autorun_days ORDER BY trading_date")
