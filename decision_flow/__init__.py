"""
decision_flow/ - Stage 6.10C AGENT DECISION FLOW (DESIGN_610C_DECISION_FLOW.md): what the agent decided for every ticker in every setup
evaluation cycle, and why, rebuilt read only from the rows the existing services already persist.

  conditions.py  the six READY checks of one stored evaluation recomputed with the setup engine's own predicates and constants, the distance
                 from READY, the cross-check against the stored blockers and the final decision of the evaluation (pure)
  store.py       read-only sqlite connections (mode=ro); rowid bisection over the append-only tables; a missing table is an empty result
  view.py        the payloads: the latest cycle, one cycle at a time, one New York trading date, why a ticker did (not) trade at a time, and the
                 inactive dynamic-universe status

Observability only: nothing here evaluates a setup, changes a mode, writes a row, calls safety.current(), requests market data or calls a
broker (every payload carries broker_calls 0). The texts reuse the setup engine's REASON_TEXT / STATE_LABELS / SETUP_NAME and the executor's
CONTROL_LABELS verbatim; this module only adds texts for the codes that have none anywhere.
"""
from typing import Optional

# reused verbatim, read only (re-exported for conditions.py / view.py): never reworded here
from execution import MODE_LABELS  # noqa: F401
from execution.activity import CONTROL_LABELS
from safety import REASON_TEXT as SAFETY_REASON_TEXT
from safety import STATE_LABELS as SAFETY_STATE_LABELS  # noqa: F401
from setups import (EVIDENCE_MAX_AGE_DAYS, R_CONFIDENCE_BELOW, R_DEMAND_BELOW, R_DEMAND_UNAVAILABLE, R_NO_SETUP, R_PPE_COVERAGE_LOW, READY_CONFIDENCE,
                    READY_DEMAND, READY_DEMAND_COVERAGE, READY_PPE_COVERAGE, SETUP_NAME, SETUP_TYPE, STATE_LABELS, WATCH_ENTER_STRETCH)
from setups import REASON_TEXT as SETUP_REASON_TEXT

VERSION = "decision_flow_v1"
STAGE = "6.10C"

# ---------------------------------------------------------------- the final decision of one evaluation (one map for the API and the UI) ----
READY, SKIPPED, BLOCKED, WATCHING, WAIT = "READY", "SKIPPED", "BLOCKED", "WATCHING", "WAIT"
DECISIONS = (READY, SKIPPED, BLOCKED, WATCHING, WAIT)
DECISION_LABELS = {READY: "READY", SKIPPED: "SKIPPED", BLOCKED: "BLOCKED", WATCHING: "WATCHING", WAIT: "WAIT"}
DECISION_BADGES = {READY: "ok", SKIPPED: "info", BLOCKED: "bad", WATCHING: "warn", WAIT: "dim"}       # the .pui-badge classes of the dashboard
DECISION_TEXT = {READY: "Setup READY and Paper Auto on: awaiting the executor, or an order intent was created.",
                 SKIPPED: "Setup READY but not sent: Paper Auto was off, or the per-cycle entry limit was used.",
                 BLOCKED: "Setup READY and an entry control blocked it, or an entry for it already existed.",
                 WATCHING: "Setup state WATCH: a downside dislocation was detected; confirmation is incomplete.",
                 WAIT: "No active setup, an invalidated setup awaiting reset, or the evaluation was paused or failed."}
WATCHING_NOTE = "WATCHING here means setup state WATCH only. The Agent card shows WATCHING for every setup that is not READY."

# the decision detail (sub-status) of each decision
AWAITING_EXECUTOR, ORDER_INTENT = "AWAITING_EXECUTOR", "ORDER_INTENT"                       # READY
PAPER_AUTO_OFF, ENTRIES_PER_CYCLE = "PAPER_AUTO_OFF", "ENTRIES_PER_CYCLE"                   # SKIPPED
ENTRY_CONTROLS, DUPLICATE_ENTRY = "ENTRY_CONTROLS", "DUPLICATE_ENTRY"                       # BLOCKED
SETUP_WATCH = "SETUP_WATCH"                                                                 # WATCHING
NO_SETUP, INVALIDATED, PAUSED, EVALUATION_ERROR = "NO_SETUP", "INVALIDATED", "PAUSED", "EVALUATION_ERROR"   # WAIT
DETAIL_LABELS = {AWAITING_EXECUTOR: "awaiting the executor", ORDER_INTENT: "order intent created", PAPER_AUTO_OFF: "Paper Auto off",
                 ENTRIES_PER_CYCLE: "entry limit of the cycle used", ENTRY_CONTROLS: "entry control", DUPLICATE_ENTRY: "duplicate entry",
                 SETUP_WATCH: "confirmation incomplete", NO_SETUP: "no active setup", INVALIDATED: "invalidated, awaiting reset", PAUSED: "evaluation paused",
                 EVALUATION_ERROR: "evaluation error"}

# display texts for the codes that have no text anywhere else (the setup engine, the risk gate and the executor keep their own)
CODE_TEXT = {PAPER_AUTO_OFF: "Paper Auto was off (Monitor only): a READY setup is not sent to the executor.",
             ENTRIES_PER_CYCLE: "The per-cycle entry limit was used by another ticker; this ticker is evaluated again next cycle.",
             AWAITING_EXECUTOR: "Paper Auto on: the executor evaluates the entry controls in its next cycle.",
             ORDER_INTENT: "Every entry control passed: the executor recorded a paper entry intent.",
             ENTRY_CONTROLS: "An entry control blocked the entry.",
             DUPLICATE_ENTRY: "An entry for this setup's proposal already exists.",
             SETUP_WATCH: "A downside dislocation was detected; confirmation is incomplete.",
             NO_SETUP: "No active setup.",
             INVALIDATED: "The setup was invalidated; it waits for the displacement to clear.",
             PAUSED: "The evaluation was paused: nothing was decided for this ticker.",
             EVALUATION_ERROR: "The evaluation failed with an error: nothing was decided for this ticker.",
             "PPE_STALE": "PPE result older than its freshness window.",
             "DEMAND_STALE": "Market Demand result older than its freshness window.",
             "QUOTE_STALE": "Quote older than its freshness window (recorded only; it does not change the setup decision)."}
ENTRY_CONTROLS_NOT_EVALUATED = "Not evaluated: entry controls run only for a READY setup during Paper Auto."
NO_EXECUTOR_RESULT = "No executor result was recorded for this ticker in this cycle's window."

# PPE confidence that is not AVAILABLE is shown as Not available with its cause - never as 'below 40'
CONFIDENCE_STATUS_TEXT = {"EVIDENCE_NOT_AVAILABLE": "no stored Stage 5.4 evidence packet",
                          "NOT_APPLICABLE": "|stretch| <= 40: no stretched move to classify",
                          "STRETCH_NOT_AVAILABLE": "PPE stretch not available"}
NO_EVIDENCE_PACKET = "NO_EVIDENCE_PACKET"                   # the one root cause behind the confidence and evidence checks when no packet is stored
ROOT_CAUSE_TEXT = {NO_EVIDENCE_PACKET: "No stored Stage 5.4 evidence packet (one cause behind two checks)"}

# ---------------------------------------------------------------- the six READY checks (DESIGN_610C section 4) -----------------------------
READY_CHECKS = (
    {"id": "STRETCH", "label": "Downside stretch (PPE)", "blocker": R_NO_SETUP, "threshold_text": f"<= {WATCH_ENTER_STRETCH:g}"},
    {"id": "PPE_COVERAGE", "label": "PPE coverage", "blocker": R_PPE_COVERAGE_LOW, "threshold_text": f">= {READY_PPE_COVERAGE:.2f}"},
    {"id": "CONFIDENCE", "label": "PPE confidence", "blocker": R_CONFIDENCE_BELOW, "threshold_text": f"status AVAILABLE and >= {READY_CONFIDENCE}"},
    {"id": "DEMAND_DATA", "label": "Market Demand data", "blocker": R_DEMAND_UNAVAILABLE, "threshold_text": f"available, coverage >= {READY_DEMAND_COVERAGE:.2f}, fresh"},
    {"id": "DEMAND", "label": "Market Demand", "blocker": R_DEMAND_BELOW, "threshold_text": f">= {READY_DEMAND} (evaluated only when the data check passes)"},
    {"id": "EVIDENCE", "label": "Stage 5.4 evidence", "blocker": "READY_BLOCKED_*",
     "threshold_text": f"a COMPLETED packet <= {EVIDENCE_MAX_AGE_DAYS} days old with no company-specific deterioration"},
)
CHECK_IDS = tuple(c["id"] for c in READY_CHECKS)

NOT_AVAILABLE = "Not available"
SCORE_TEXT = "Not available - the setup engine computes no composite score."
PRICE_HISTORY_TEXT = "Not available (the evaluator does not record prices)"
SETUP_NOTE = "only setup type; no cross-setup ranking"
RANKING_RULE = ("Closest to READY is a display ordering of persisted values, not a score: setup state (READY, WATCH, INVALIDATED, NONE), then the "
                "fewest unmet checks, then the smallest stretch gap, then the smallest demand gap, then the symbol.")
STATE_ORDER = ("READY", "WATCH", "INVALIDATED", "NONE")
SESSION_LABELS = {"PREMARKET": "Pre-market", "REGULAR": "Regular session", "AFTER_HOURS": "After hours", "CLOSED": "Market closed"}
ORIGINS = ("SCHEDULER_CYCLE", "TRADER_VIEW", "SUBSCRIBE")    # a scheduler batch row; a Trader View stream / refresh row; opening a ticker
UNIVERSE_NOT_AVAILABLE = "Dynamic universe status not available"
DISCLAIMER = ("Decision Flow explains, read only, what the agent's existing deterministic rules decided at each setup evaluation, rebuilt from rows "
              "the agent already stored. It changes nothing and contacts no broker. READY means a setup met its review threshold; it is not advice "
              "and not a probability that a stock rises.")


def text_for(code: Optional[str]) -> str:
    """The display sentence of a code: this module's texts first, then the setup engine's, the risk gate's and the executor's control labels."""
    if not code:
        return ""
    return CODE_TEXT.get(code) or SETUP_REASON_TEXT.get(code) or SAFETY_REASON_TEXT.get(code) or CONTROL_LABELS.get(code) or code


def legend() -> dict:
    """The decision vocabulary and the check list as the dashboard shows them (one map, used by the API and the UI)."""
    return {"decisions": [{"code": d, "label": DECISION_LABELS[d], "badge": DECISION_BADGES[d], "text": DECISION_TEXT[d]} for d in DECISIONS],
            "watching_note": WATCHING_NOTE, "checks": [dict(c) for c in READY_CHECKS], "ranking_rule": RANKING_RULE, "score_text": SCORE_TEXT,
            "setup": {"type": SETUP_TYPE, "name": SETUP_NAME, "note": SETUP_NOTE, "state_labels": dict(STATE_LABELS)},
            "entry_controls_not_evaluated": ENTRY_CONTROLS_NOT_EVALUATED}
