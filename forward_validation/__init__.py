"""
forward_validation/ - Stage 6.1 FORWARD PAPER VALIDATION (DESIGN_61_FORWARD_PAPER_VALIDATION.md).

Stage 6.0 proved that the bot can execute safely; Stage 6.1 observes whether it stays operationally reliable across many real Alpaca PAPER
sessions, and - separately - what the FROZEN strategy actually does forward. It is instrumentation only:

  - it reads the persisted system events of Stages 5.8 - 6.0.2 (setup transitions, proposals, intents, broker orders, fills, positions,
    reconciliation findings, safety evaluations / events, account and cash-flow refresh attempts) and its own observer samples;
  - it never infers a missing state, never changes a threshold, a rule, a quantity, an exit, the mode or an order (its ONE action is the
    Stage 6.1 early stop: Paper Auto -> Monitor only when a critical safety defect is observed - never a liquidation);
  - every operational number (duplicates, reconciliation, safety fires, freshness) is kept apart from every strategy number (funnel, P/L,
    win rate, expectancy, drawdown), and the strategy numbers stay labelled INSUFFICIENT FOR STRATEGY CONCLUSION until the sample is meaningful.
Alpaca PAPER only. Real market setups only. No forced signal, no tuning, no leverage, no short, no live money, no model call.
"""
import hashlib
import json

VERSION = "forward_validation_v1"
STAGE = "6.1"
LEASE_NAME = "forward_validation_observer"

SESSION_STATUSES = ("HEALTHY", "DEGRADED", "FAILED", "NOT_RUN")
SESSION_LABELS = {"OPEN": "In progress", "HEALTHY": "Healthy", "DEGRADED": "Degraded", "FAILED": "Failed", "NOT_RUN": "Not run"}
EDGE_STATUSES = ("INSUFFICIENT_DATA", "EARLY_NEGATIVE", "MIXED", "EARLY_POSITIVE")
EDGE_LABELS = {"INSUFFICIENT_DATA": "Insufficient data", "EARLY_NEGATIVE": "Early negative", "MIXED": "Mixed", "EARLY_POSITIVE": "Early positive"}
INSUFFICIENT_TEXT = "INSUFFICIENT FOR STRATEGY CONCLUSION"

# the validation target: an OPERATIONAL milestone, never a profitability claim
TARGET_SESSIONS = 30
TARGET_EVENTS = 100                       # meaningful setup / proposal events (READY transitions + reviewable proposals)
MIN_TRADES_FOR_EDGE = 30                  # below it the strategy status is INSUFFICIENT_DATA whatever the P/L reads
WEEKLY_SESSIONS = 5

FUNNEL = ("universe_scanned", "WATCH", "READY", "REVIEWABLE", "BLOCKED", "ENTRY_SUBMITTED", "ENTRY_FILLED", "ENTRY_PARTIAL", "POSITION_OPEN", "EXIT_REQUIRED",
          "EXIT_SUBMITTED", "EXIT_FILLED", "CLOSED", "ATTENTION")
CONVERSIONS = (("watch_to_ready", "WATCH", "READY"), ("ready_to_reviewable", "READY", "REVIEWABLE"), ("reviewable_to_entry", "REVIEWABLE", "ENTRY_SUBMITTED"),
               ("entry_to_fill", "ENTRY_SUBMITTED", "ENTRY_FILLED"), ("position_to_normal_close", "POSITION_OPEN", "CLOSED"), ("position_to_attention", "POSITION_OPEN", "ATTENTION"))

# attention conditions (DESIGN_61 section 9); CRITICAL ones stop Paper Auto at once (the early stop, section 10)
ATTENTION_CODES = ("DUPLICATE_BROKER_ORDER", "LIVE_HOST_ATTEMPT", "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", "PERSISTENT_RECONCILIATION_MISMATCH", "RECONCILIATION_REQUIRED",
                   "FAILED_EXECUTION", "UNRESOLVED_QTY_MISMATCH", "PERSISTENT_REFRESH_FAILURE", "PERSISTENT_CASH_FLOW_FAILURE")
CRITICAL_CODES = frozenset({"DUPLICATE_BROKER_ORDER", "LIVE_HOST_ATTEMPT", "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", "PERSISTENT_RECONCILIATION_MISMATCH"})
ATTENTION_TEXT = {"DUPLICATE_BROKER_ORDER": "a broker order was duplicated", "LIVE_HOST_ATTEMPT": "a request to a non-paper host was attempted",
                  "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE": "an entry was submitted while the safety gate did not allow new entries",
                  "PERSISTENT_RECONCILIATION_MISMATCH": "a reconciliation mismatch stayed unresolved across sessions", "RECONCILIATION_REQUIRED": "an order needs reconciliation",
                  "FAILED_EXECUTION": "an execution intent failed", "UNRESOLVED_QTY_MISMATCH": "an internal / broker quantity mismatch is unresolved",
                  "PERSISTENT_REFRESH_FAILURE": "the account refresh keeps failing", "PERSISTENT_CASH_FLOW_FAILURE": "the cash-flow activity read keeps failing"}
PERSISTENT_MISMATCH_SESSIONS = 2          # an ATTENTION position still unresolved after this many closed sessions is a critical defect

EVENT_KINDS = ("SESSION_OPENED", "SESSION_CLOSED", "SESSION_NOT_RUN", "RESTART_DETECTED", "ATTENTION_RAISED", "ATTENTION_CLEARED", "EARLY_STOP", "SUMMARY_REBUILT",
               "OBSERVER_ERROR")
CONTROLS = ("observer", "session_recorder", "summary_builder", "attention_monitor", "early_stop")
EXPORTS = ("sessions", "trades", "daily", "control_events", "samples", "restarts")

DISCLAIMER = ("Descriptive forward-paper statistics of the Alpaca PAPER account only: operational reliability of the bot and what the frozen strategy did. "
              "Not a forecast, not a performance claim, not advice; the strategy numbers stay inconclusive until the validation sample is meaningful.")

# ---------------------------------------------------------------- the frozen strategy controls (section 2 of the stage) -----------------
# Stage 6.1 observes the existing strategy; it never tunes it. The values below are read from the frozen Stage 5.8 / 5.9 / 5.9.1 / 6.0
# modules and config at call time, so tests/test_forward_validation_61.py can prove that none of them moved (hash + explicit values).


def frozen_controls() -> dict:
    import config
    import setups as S
    import execution as X
    return {
        "setup_type": S.SETUP_TYPE, "watch_enter_stretch": S.WATCH_ENTER_STRETCH, "watch_exit_stretch": S.WATCH_EXIT_STRETCH, "ready_confidence": S.READY_CONFIDENCE,
        "ready_demand": S.READY_DEMAND, "ready_demand_coverage": S.READY_DEMAND_COVERAGE, "ready_ppe_coverage": S.READY_PPE_COVERAGE, "ready_exit_demand": S.READY_EXIT_DEMAND,
        "invalidate_demand": S.INVALIDATE_DEMAND, "invalidate_demand_evals": S.INVALIDATE_DEMAND_EVALS, "invalidate_stretch": S.INVALIDATE_STRETCH,
        "evidence_max_age_days": S.EVIDENCE_MAX_AGE_DAYS, "risk_fraction": float(config.TRADE_PROPOSAL_RISK_FRACTION), "max_position_pct": float(config.MAX_POSITION_PERCENT),
        "proposal_ttl_s": int(config.TRADE_PROPOSAL_TTL_SECONDS), "proposal_price_change_pct": float(config.TRADE_PROPOSAL_PRICE_CHANGE_PCT),
        "max_daily_loss_pct": float(config.MAX_DAILY_LOSS_PERCENT), "max_open_positions": int(config.MAX_OPEN_POSITIONS), "max_hold_sessions": int(config.EXECUTION_MAX_HOLD_SESSIONS),
        "demand_exit": int(config.EXECUTION_DEMAND_EXIT), "demand_exit_evals": int(config.EXECUTION_DEMAND_EXIT_EVALS), "stretch_exit": float(config.EXECUTION_STRETCH_EXIT),
        "near_close_minutes": int(config.EXECUTION_NEAR_CLOSE_MINUTES), "max_entries_per_cycle": int(config.EXECUTION_MAX_ENTRIES_PER_CYCLE),
        "max_exit_cycles": int(config.EXECUTION_MAX_EXIT_CYCLES), "order_type": X.ORDER_TYPE, "time_in_force": X.TIME_IN_FORCE, "side": X.SIDE,
        "exit_reasons": list(X.EXIT_REASONS), "session_rule": "regular session only (weekdays 09:30-16:00 ET), no extended hours", "leverage": "none", "shorts": "none",
    }


def frozen_hash() -> str:
    return hashlib.sha256(json.dumps(frozen_controls(), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
