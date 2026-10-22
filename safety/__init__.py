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
