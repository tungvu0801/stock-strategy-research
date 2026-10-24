"""
safety/ — Stage 5.9.1 REAL EXECUTION SAFETY CONTROLS (DESIGN_591_EXECUTION_SAFETY.md).

A persisted, deterministic safety gate: a manual GLOBAL KILL SWITCH, a real DAILY LOSS HALT computed from a persisted daily equity
baseline, and a NO NEW ENTRIES pause. One function (`safety.service.evaluate_execution_safety`) answers "may a new entry proceed?"
for the future Stage 6.0 execution layer; every evaluation leaves control evidence in the database. Nothing here submits, previews,
liquidates or calls a broker.
"""
VERSION = "safety_v1"
STATES = ("TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN")
STATE_LABELS = {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New trades paused", "HALTED": "Trading stopped", "UNKNOWN": "Trading stopped"}
STATE_ICONS = {"TRADING_ALLOWED": "🟢", "NO_NEW_ENTRIES": "🟡", "HALTED": "🔴", "UNKNOWN": "🔴"}
CONTROLS = ("GLOBAL_KILL_SWITCH", "DAILY_LOSS_HALT", "NO_NEW_ENTRIES", "DAILY_BASELINE")
CONTROL_RESULTS = ("PASS", "BLOCK", "WARN", "UNKNOWN")
TRIGGERS = ("API", "ACTION", "PROPOSAL", "SCHEDULED", "STARTUP", "TEST")
ACTORS = ("user", "system", "test")
EVENT_KINDS = ("PAUSE_ON", "PAUSE_OFF", "KILL_ON", "KILL_OFF", "BASELINE_CREATED", "DAILY_HALT_TRIGGERED", "DAILY_RESET", "EVALUATOR_ERROR", "STARTUP")
DAILY_STATUSES = ("OK", "TRIGGERED", "UNAVAILABLE")
ADJUSTMENTS = ("NONE_POSSIBLE", "UNAVAILABLE", "ACCEPTED_UNADJUSTED")

# ---- reason codes --------------------------------------------------------------------------------------------------------------
R_KILL = "KILL_SWITCH_ACTIVE"
R_DAILY = "DAILY_LOSS_LIMIT_REACHED"
R_PAUSE = "NO_NEW_ENTRIES_ACTIVE"
R_BASELINE_MISSING = "DAILY_BASELINE_MISSING"
R_EQUITY_NA = "EQUITY_NOT_AVAILABLE"
R_EQUITY_INVALID = "INVALID_EQUITY"
R_SNAPSHOT_STALE = "ACCOUNT_SNAPSHOT_STALE"
R_THRESHOLD_INVALID = "THRESHOLD_INVALID"
R_ADJUSTMENT_NA = "DAILY_PNL_ADJUSTMENT_UNAVAILABLE"
R_DAILY_NA = "DAILY_LOSS_UNAVAILABLE"
R_PERSISTENCE = "PERSISTENCE_FAILED"
R_EVALUATOR = "SAFETY_EVALUATOR_ERROR"
R_NEW_DAY = "NEW_TRADING_DAY"
R_STARTUP_POLICY = "STARTUP_POLICY"
REASON_TEXT = {
    R_KILL: "Emergency stop active",
    R_DAILY: "Daily loss limit reached",
    R_PAUSE: "New trades paused by you",
    R_BASELINE_MISSING: "No daily equity baseline yet",
    R_EQUITY_NA: "Account equity not available",
    R_EQUITY_INVALID: "Account equity invalid",
    R_SNAPSHOT_STALE: "Account snapshot is stale",
    R_THRESHOLD_INVALID: "Daily loss threshold is not configured",
    R_ADJUSTMENT_NA: "Deposits and withdrawals cannot be separated from trading P&L for this account",
    R_DAILY_NA: "Daily loss cannot be verified",
    R_PERSISTENCE: "Safety status could not be saved",
    R_EVALUATOR: "Safety status unavailable",
    R_NEW_DAY: "New trading day",
    R_STARTUP_POLICY: "Startup policy",
}
STARTUP_POLICIES = ("KEEP", "HALT_ON_START")
DISCLAIMER = "Risk controls gate future entries only. Nothing here places, previews or cancels an order."
# >>> Stage 6.0.2: cash-flow-adjusted daily P&L (DESIGN_602_ADJUSTED_DAILY_PNL.md section 6). Additive: nothing above changes meaning.
ADJUSTMENTS = ADJUSTMENTS + ("CASH_FLOW_ADJUSTED",)
R_CASH_FLOW_STALE = "CASH_FLOW_DATA_STALE"
R_CASH_FLOW_PENDING = "CASH_FLOW_PENDING"
R_CASH_FLOW_AMBIGUOUS = "CASH_FLOW_TIMING_AMBIGUOUS"
R_CASH_FLOW_UNCERTAIN = "CASH_FLOW_CLASSIFICATION_UNCERTAIN"
R_ADJUSTED_BASELINE_INVALID = "ADJUSTED_BASELINE_INVALID"
# today's cash-flow adjustment status -> the reason a new entry pauses (UNAVAILABLE keeps its Stage 5.9.1 meaning and code)
CASH_FLOW_REASONS = {"STALE": R_CASH_FLOW_STALE, "PENDING": R_CASH_FLOW_PENDING, "AMBIGUOUS": R_CASH_FLOW_AMBIGUOUS, "UNCERTAIN": R_CASH_FLOW_UNCERTAIN,
                     "UNAVAILABLE": R_ADJUSTMENT_NA}
REASON_TEXT.update({
    R_CASH_FLOW_STALE: "Deposit and withdrawal data is stale",
    R_CASH_FLOW_PENDING: "A new deposit or withdrawal is not yet reflected in the account snapshot",
    R_CASH_FLOW_AMBIGUOUS: "A deposit or withdrawal is too close to the daily baseline to place",
    R_CASH_FLOW_UNCERTAIN: "An account activity could not be classified as trading or funding",
    R_ADJUSTED_BASELINE_INVALID: "Starting equity after deposits and withdrawals is not positive",
})
# <<< Stage 6.0.2
