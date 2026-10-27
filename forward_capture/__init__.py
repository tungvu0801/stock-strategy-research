"""
forward_capture/ - Stage 6.10A FORWARD EVIDENCE CAPTURE (DESIGN_610A_FORWARD_PAPER_AUTORUN.md): the Stage 6.1 forward validation, extended with a
point-in-time dataset of every candidate observation and decision, so forward paper evidence can be trusted.

  project.py   ALLOWLIST projections - the only way anything reaches a table, a view or an export (no external-account holding, no REAL
               equity, no account number, no credential, no free text of the exposure gate)
  store.py     the forward_capture_* tables on the shared database (database/forward_capture_migrations.py): append-only rows written with
               'insert if absent'; a second write of the same id with different content is recorded as a CONFLICT fault, never silently dropped
  capture.py   ForwardCapture: the executor hands decision-time markers to an in-memory sink (no I/O, no lock); the forward-validation observer
               thread drains it and indexes setup evaluations, intents and positions - it is the only writer
  cohort.py    the config snapshot (allowlisted, hashed) and the forward paper cohort
  health.py    one operational health record per market day: HEALTHY | DEGRADED | INVALID (MARKET_CLOSED for holidays); no P/L input
  exports.py   read-only exports through /api/forward-validation/export/{kind}
  view.py      the compact read-only views for Bot Activity, the forward-validation overview and the daily summary

Nothing here places, changes or cancels an order, changes a mode, or calls a broker. Recording failures never reach the executor; they are
recorded as faults and degrade the day's evidence instead.
"""
VERSION = "forward_capture_v1"
STAGE = "6.10A"
STRATEGY_ID = "DOWNSIDE_DISLOCATION_RECOVERY"

NOT_READY, READY, SKIPPED, BLOCKED, ENTRY_INTENT, NOT_ENTERED, ENTERED, EXITED, NOT_EVALUATED = (
    "NOT_READY", "READY", "SKIPPED", "BLOCKED", "ENTRY_INTENT", "NOT_ENTERED", "ENTERED", "EXITED", "NOT_EVALUATED")
DECISIONS = (NOT_READY, READY, SKIPPED, BLOCKED, ENTRY_INTENT, NOT_ENTERED, ENTERED, EXITED, NOT_EVALUATED)
SOURCES = ("SETUP_EVALUATION", "EXECUTOR_CYCLE", "INTENT", "POSITION")

HEALTHY, DEGRADED, INVALID, MARKET_CLOSED = "HEALTHY", "DEGRADED", "INVALID", "MARKET_CLOSED"
HEALTH_STATUSES = (HEALTHY, DEGRADED, INVALID, MARKET_CLOSED)

EXPORT_KINDS = ("candidates", "decisions", "controls", "session_health", "entry_snapshots", "outcomes", "config_snapshots", "cohorts", "context", "faults",
                "autorun_events", "autorun_preflights", "autorun_days")
SINK_MAX = 5000                         # bounded executor hand-off; an overflow is recorded as a fault (evidence gap), never a blocked executor
TICK_MAX_EVALUATIONS = 500              # setup evaluations indexed per observer tick
CONTEXT_EVERY_S = 900                   # an unchanged context row is re-written at most this often

# the exposure gate's verdicts as FIXED labels: its free text carries other accounts' quantities and values, which are never persisted
GATE_LABELS = {"EXTERNAL_SYMBOL_ALREADY_HELD": "Another account (read only) already holds the symbol",
               "SYMBOL_CONCENTRATION_ABOVE_LIMIT": "Projected symbol concentration above the limit",
               "EXTERNAL_VALUE_UNREADABLE": "A known external holding has no readable value",
               "PLANNED_NOTIONAL_INVALID": "No valid planned entry amount", "GATE_CONFIG_INVALID": "Gate configuration invalid",
               "EXTERNAL_DATA_UNKNOWN": "External holdings unknown (never treated as zero)", "ALLOWED_ON_STALE_DATA": "Allowed on stale external data",
               "WITHIN_LIMITS": "Within limits", "NO_PLANNED_ENTRY": "No planned entry (pre-check only)"}

DISCLAIMER = ("Forward evidence of the Alpaca PAPER bot: what every candidate looked like when it was decided, and what the bot did. Point-in-time and "
              "append-only; operational health is kept apart from strategy results, which stay inconclusive until the sample is meaningful.")
