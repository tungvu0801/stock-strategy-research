"""
forward_validation/attention_signature.py - Wave 2 PR A (X2; WAVE2_REMEDIATION_DESIGN.md section 3.7.1): the incident signature of each CRITICAL
attention code, a set of evidence elements read from the same persisted rows that ForwardValidationService.attention() reads.

  signature(store, code)   frozenset of tuples (strings and integers only; no wall-clock value except a stored row's own column)

A stop is consumed per element: an element not yet acknowledged always stops Paper Auto, an element that disappears (a cancelled order, an
overwritten control error) never does. Read only: every row comes through ForwardValidationStore.query (a read-only connection). Nothing here
calls the executor, the broker or any safety action; the early stop's one action lives in ForwardValidationService._early_stop.
"""
from __future__ import annotations

from typing import FrozenSet, Tuple

from forward_validation import PERSISTENT_MISMATCH_SESSIONS

HUMAN_ACTORS = frozenset({"user"})                            # the only actor whose PAPER_AUTO arm acknowledges a stopped incident (design 3.7.2, F-20): an allow-list
LIVE_HOST_TABLES = (("account_refresh_attempts", "error_text"), ("cash_flow_refresh_attempts", "error_text"), ("execution_reconciliations", "detail_json"),
                    ("execution_runs", "error"))              # the append-only rows of ForwardValidationService._live_host_attempts (execution_controls apart: mutable)

Element = Tuple


def _duplicates(store) -> FrozenSet[Element]:
    rows = store.query("SELECT client_order_id, broker_order_id FROM execution_orders WHERE client_order_id IN "
                       "(SELECT client_order_id FROM execution_orders GROUP BY client_order_id HAVING count(*) > 1)")
    out = {("COID_DUP", str(r["client_order_id"]), str(r["broker_order_id"])) for r in rows}
    for r in store.query("SELECT rowid AS rid FROM execution_reconciliations WHERE kind IN ('MULTIPLE_ORDERS', 'UNKNOWN_INTERNAL_ORDER')"):
        out.add(("REC", "execution_reconciliations", int(r["rid"])))
    return frozenset(out)


def _live_host(store) -> FrozenSet[Element]:
    from forward_validation.service import LIVE_HOST_MARK                       # the transport's refusal text (one definition)
    like = f"%{LIVE_HOST_MARK}%"
    out = set()
    for table, col in LIVE_HOST_TABLES:
        for r in store.query(f"SELECT rowid AS rid FROM {table} WHERE {col} LIKE ?", (like,)):
            out.add(("ROW", table, int(r["rid"])))
    for r in store.query("SELECT control_id, evaluation_count FROM execution_controls WHERE last_error LIKE ?", (like,)):
        out.add(("CTRL", str(r["control_id"]), int(r["evaluation_count"] or 0)))   # G-02: a new refusal touch is a new element; a benign touch clears the mark
    return frozenset(out)


def _gate(store) -> FrozenSet[Element]:
    out = set()
    for it in store.query("SELECT execution_id, submitted_at FROM execution_intents WHERE intent_type = 'ENTRY' AND attempt_count >= 1 AND submitted_at IS NOT NULL"):
        ev = store.query("SELECT allowed_new_entry FROM execution_safety_evaluations WHERE evaluated_at <= ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1", (it["submitted_at"],))
        if ev and not int(ev[0]["allowed_new_entry"] or 0):
            out.add(("GATE", str(it["execution_id"])))
    return frozenset(out)


def _persistent_mismatch(store) -> FrozenSet[Element]:
    """G-06: only the ATTENTION positions that INDIVIDUALLY meet attention()'s persistence rule, each keyed on its latest entry into ATTENTION."""
    closed = [s for s in store.sessions(limit=PERSISTENT_MISMATCH_SESSIONS + 1) if s["session_status"] != "OPEN"][:PERSISTENT_MISMATCH_SESSIONS]
    if len(closed) < PERSISTENT_MISMATCH_SESSIONS:
        return frozenset()
    out = set()
    for p in store.query("SELECT position_id, updated_at FROM paper_positions WHERE state = 'ATTENTION'"):
        ev = store.query("SELECT MIN(at) AS a, MAX(CASE WHEN IFNULL(from_state, '') != 'ATTENTION' THEN rowid END) AS r FROM position_events "
                         "WHERE position_id = ? AND to_state = 'ATTENTION'", (p["position_id"],))   # keyed on the latest ENTRY into ATTENTION (K-10: a fill correction is not one)
        oldest = min([x for x in (p["updated_at"], ev[0]["a"] if ev else None) if x])
        if all((s.get("started_at") or s["trading_date"]) > oldest for s in closed):
            out.add(("PRM", str(p["position_id"]), int((ev[0]["r"] if ev else None) or 0)))
    return frozenset(out)


READERS = {"DUPLICATE_BROKER_ORDER": _duplicates, "LIVE_HOST_ATTEMPT": _live_host, "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE": _gate,
           "PERSISTENT_RECONCILIATION_MISMATCH": _persistent_mismatch}


def signature(store, code: str) -> FrozenSet[Element]:
    """The evidence elements behind one critical code, now. An unknown code has none (the caller then uses ("UNSIGNED", code): it stops)."""
    reader = READERS.get(code)
    return reader(store) if reader is not None else frozenset()
