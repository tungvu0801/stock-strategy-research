"""
setups/ — Stage 5.8 SETUP STATE MACHINE + market-hours alerts (DESIGN_58_SETUP_STATE_ALERTS.md).

One deterministic research setup (DOWNSIDE_DISLOCATION_RECOVERY) with four states — NONE, WATCH, READY, INVALIDATED — evaluated from
already-computed research inputs only (PPE Stretch / Confidence, Market Demand, the stored Stage 5.4 packet, freshness, session).
READY means READY FOR REVIEW: a deterministic confirmation threshold was met. It is not an entry signal, not a trade recommendation and
not a probability. No order, no broker, no model call, no research run.
"""
VERSION = "setups_v1"
SETUP_TYPE = "DOWNSIDE_DISLOCATION_RECOVERY"
SETUP_NAME = "Downside Dislocation Recovery"
STATES = ("NONE", "WATCH", "READY", "INVALIDATED")
STATE_LABELS = {"NONE": "NO ACTIVE SETUP", "WATCH": "WATCH", "READY": "READY FOR REVIEW", "INVALIDATED": "INVALIDATED"}
DECISIONS = ("TRANSITION", "HOLD", "PAUSED", "ERROR")
TRIGGERS = ("SCHEDULED", "SUBSCRIBE", "REFRESH", "MANUAL", "STARTUP", "TEST")
DISCLAIMER = ("Setup state is deterministic research bookkeeping: READY means the setup met its review threshold, not that a trade should "
              "be made and not a probability that the stock will rise.")

# thresholds (fixed before any outcome analysis; research hypotheses, not tuned)
WATCH_ENTER_STRETCH = -40.0
WATCH_EXIT_STRETCH = -30.0
READY_CONFIDENCE = 40
READY_DEMAND = 60
READY_DEMAND_COVERAGE = 0.60
READY_PPE_COVERAGE = 0.60
READY_EXIT_DEMAND = 45
INVALIDATE_DEMAND = 35
INVALIDATE_DEMAND_EVALS = 2
INVALIDATE_STRETCH = -20.0
EVIDENCE_MAX_AGE_DAYS = 30
ALERT_COOLDOWN_MINUTES = 15
EVAL_INTERVAL_SECONDS = 60
QUOTE_MAX_AGE_SECONDS = 300
MIN_FRESH_WINDOW_SECONDS = 300

# reason codes
R_STRETCH_DETECTED = "DOWNSIDE_STRETCH_DETECTED"
R_STRETCH_FADED = "STRETCH_FADED"
R_CONFIRMED = "CONFIRMATION_STRENGTHENED"
R_WEAKENED = "CONFIRMATION_WEAKENED"
R_HOLDS = "CONFIRMATION_HOLDS"
R_DEMAND_UNAVAILABLE = "DEMAND_UNAVAILABLE"
R_DEMAND_BELOW = "DEMAND_BELOW_60"
R_CONFIDENCE_BELOW = "CONFIDENCE_BELOW_40"
R_PPE_COVERAGE_LOW = "PPE_COVERAGE_LOW"
R_DEMAND_COLLAPSE_PENDING = "DEMAND_COLLAPSE_PENDING"
R_DEMAND_COLLAPSED = "DEMAND_COLLAPSED"
R_STRETCH_NORMALIZED = "STRETCH_NORMALIZED"
R_EVIDENCE_CONTRADICTION = "EVIDENCE_CONTRADICTION"
R_EPISODE_CLOSED = "EPISODE_CLOSED"
R_AWAITING_RESET = "INVALIDATED_AWAITING_RESET"
R_NO_SETUP = "NO_DOWNSIDE_STRETCH"
R_BLOCK_NO_EVIDENCE = "READY_BLOCKED_NO_EVIDENCE"
R_BLOCK_EVIDENCE_STALE = "READY_BLOCKED_EVIDENCE_STALE"
R_BLOCK_EVIDENCE = "READY_BLOCKED_BY_EVIDENCE"
R_PAUSED_STALE = "EVALUATION_PAUSED_STALE_DATA"
R_PAUSED_PPE = "EVALUATION_PAUSED_PPE_UNAVAILABLE"
ALERTED_TRANSITIONS = (("NONE", "WATCH"), ("WATCH", "READY"), ("READY", "WATCH"), ("WATCH", "INVALIDATED"), ("READY", "INVALIDATED"))
REASON_TEXT = {
    R_STRETCH_DETECTED: "Downside dislocation detected.",
    R_STRETCH_FADED: "Displacement faded before confirmation; episode closed.",
    R_CONFIRMED: "Buyer confirmation strengthened.",
    R_WEAKENED: "Buyer confirmation weakened.",
    R_HOLDS: "Confirmation holds.",
    R_DEMAND_UNAVAILABLE: "Market Demand unavailable or stale.",
    R_DEMAND_BELOW: "Market Demand below 60.",
    R_CONFIDENCE_BELOW: "PPE Confidence below 40.",
    R_PPE_COVERAGE_LOW: "PPE coverage below the 60% floor.",
    R_DEMAND_COLLAPSE_PENDING: "Market Demand below 35; one more evaluation below 35 invalidates.",
    R_DEMAND_COLLAPSED: "Demand collapsed after confirmation.",
    R_STRETCH_NORMALIZED: "Displacement normalised after confirmation.",
    R_EVIDENCE_CONTRADICTION: "New evidence shows strong company-specific deterioration.",
    R_EPISODE_CLOSED: "Displacement gone; invalidated episode closed.",
    R_AWAITING_RESET: "Invalidated; waiting for the displacement to clear before a new cycle.",
    R_NO_SETUP: "No downside stretch.",
    R_BLOCK_NO_EVIDENCE: "No stored Stage 5.4 evidence packet; READY needs evidence.",
    R_BLOCK_EVIDENCE_STALE: "Stored evidence packet older than 30 days; READY blocked.",
    R_BLOCK_EVIDENCE: "Evidence confirms company-specific deterioration; READY blocked.",
    R_PAUSED_STALE: "Evaluation paused: critical inputs are stale.",
    R_PAUSED_PPE: "Evaluation paused: PPE unavailable.",
}
CONTROLS = ("setup_state_evaluator", "alert_persistor", "market_hours_scheduler")
