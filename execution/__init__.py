"""
execution/ — Stage 6.0 AUTONOMOUS ALPACA PAPER TRADING (DESIGN_60_AUTONOMOUS_PAPER.md).

The first autonomous loop of Stock Agent, on the Alpaca PAPER account only:
  WATCH -> READY setup (Stage 5.8) -> deterministic proposal (Stage 5.9) -> execution safety gate (Stage 5.9.1) -> ONE paper entry ->
  order monitoring -> fill reconciliation -> internal position -> deterministic exit conditions -> ONE paper exit -> reconciliation -> closed.

Boundaries (tests/test_execution_60.py pins them):
  - PAPER ONLY: the broker client builds every URL from the constant paper host; there is no base-URL setting and no live mode;
  - no real money, no Robinhood call, no Stage 4.6B writer import, no model call anywhere in the path;
  - exactly-once: an intent with a deterministic client_order_id is persisted before any POST; an ambiguous POST is never retried blindly;
  - the safety gate blocks NEW ENTRIES only; deterministic risk-reducing exits continue; an Emergency Stop never liquidates;
  - the user controls the mode (MONITOR_ONLY by default; PAPER_AUTO is an explicit, persisted choice).
"""
VERSION = "execution_v1"
STAGE = "6.0"

MODES = ("MONITOR_ONLY", "PAPER_AUTO")
MODE_LABELS = {"MONITOR_ONLY": "Monitor only", "PAPER_AUTO": "Paper Auto"}
MODE_ICONS = {"MONITOR_ONLY": "⚪", "PAPER_AUTO": "🟢"}
ATTENTION_ICON, ATTENTION_LABEL = "🔴", "Needs attention"

# internal execution states (one per symbol at a time); the beginner view maps them (BEGINNER below)
STATES = ("IDLE", "ENTRY_PENDING", "ENTRY_SUBMITTED", "ENTRY_PARTIAL", "POSITION_OPEN", "EXIT_REQUIRED", "EXIT_PENDING", "EXIT_SUBMITTED", "EXIT_PARTIAL",
          "CLOSED", "FAILED", "RECONCILIATION_REQUIRED")
BEGINNER = {"NONE": ("⚪", "No setup"), "WATCHING": ("🟡", "Watching"), "READY": ("🔵", "Ready"), "ENTERING": ("🟠", "Entering"), "IN_TRADE": ("🟢", "In trade"),
            "EXITING": ("🟠", "Exiting"), "CLOSED": ("⚪", "Closed"), "STOPPED": ("🔴", "Stopped"), "ATTENTION": ("🔴", "Needs attention")}

# intents (one row per entry / exit attempt family; the row is written BEFORE the broker is contacted)
INTENT_TYPES = ("ENTRY", "EXIT")
INTENT_STATES = ("PENDING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "NOT_SENT", "RECONCILIATION_REQUIRED", "FAILED")
INTENT_OPEN = frozenset({"PENDING", "SUBMITTED", "PARTIALLY_FILLED", "RECONCILIATION_REQUIRED"})
INTENT_TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "NOT_SENT", "FAILED"})

# positions (created only from broker-confirmed fills)
POSITION_STATES = ("OPEN", "HEALTHY", "WEAKENING", "EXIT_REQUIRED", "EXIT_SUBMITTED", "CLOSED", "ATTENTION")
POSITION_OPEN = frozenset({"OPEN", "HEALTHY", "WEAKENING", "EXIT_REQUIRED", "EXIT_SUBMITTED", "ATTENTION"})

# exit reasons in PRECEDENCE order (the first true reason wins)
EXIT_REASONS = ("HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED")
EXIT_TEXT = {"HARD_INVALIDATION": "Price reached the invalidation level", "EVIDENCE_INVALIDATION": "New company-specific evidence contradicted the setup",
             "SETUP_INVALIDATED": "The setup was invalidated", "DEMAND_FAILURE": "Demand stayed too weak", "TIME_EXIT": "Maximum holding time reached",
             "DISLOCATION_NORMALIZED": "The dislocation normalized"}

# liveness controls (database rows: last evaluated, last fired, counts, last result, last error)
CONTROLS = ("PAPER_ENVIRONMENT", "EXECUTION_MODE", "SAFETY_GATE", "PROPOSAL_VALIDITY", "SESSION_POLICY", "DUPLICATE_ENTRY", "MAX_OPEN_POSITIONS", "BROKER_ACCOUNT",
            "BROKER_TRADABILITY", "BROKER_POSITION", "BROKER_RECONCILIATION", "ENTRY_SUBMISSION", "POSITION_RECONCILIATION", "EXIT_TRIGGER", "EXIT_SUBMISSION")
RESULTS = ("PASS", "BLOCK", "WARN", "UNKNOWN", "RUN")

# reconciliation finding kinds (blocking ones set RECONCILIATION_REQUIRED for new entries; nothing is ever silently fixed)
FINDINGS = ("ORDER_MISSING_AT_BROKER", "UNKNOWN_INTERNAL_ORDER", "QTY_MISMATCH", "BROKER_FLAT_INTERNAL_OPEN", "INTERNAL_FLAT_BROKER_POSITION", "EXTERNAL_POSITION",
            "MULTIPLE_ORDERS", "UNKNOWN_STATUS", "BROKER_UNAVAILABLE", "ORDER_ADOPTED", "ORDER_NOT_FOUND_FINAL")
INFORMATIONAL_FINDINGS = frozenset({"EXTERNAL_POSITION", "ORDER_ADOPTED", "ORDER_NOT_FOUND_FINAL"})

COID_PREFIX = "stockagent-"
ENTRY_PREFIX = COID_PREFIX + "entry-"
EXIT_PREFIX = COID_PREFIX + "exit-"
SETUP_TYPE = "DOWNSIDE_DISLOCATION_RECOVERY"
SIDE = "LONG"
ORDER_TYPE, TIME_IN_FORCE = "market", "day"

DISCLAIMER = ("Paper execution on the Alpaca PAPER account only: no real money. Deterministic Python rules decide every entry and exit; the safety gate, the proposal "
              "and the broker's own confirmations are the inputs. This is bookkeeping of a paper simulation, not advice.")
BOT_LABELS = {"MONITOR_ONLY": "Monitor only", "PAPER_AUTO": "Paper Auto", "ATTENTION": "Needs attention"}
