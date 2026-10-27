"""
autorun/ - Stage 6.10A FORWARD PAPER AUTO-RUN (DESIGN_610A_FORWARD_PAPER_AUTORUN.md): WHEN the Alpaca PAPER executor may enter, never HOW.

  session.py    the market-session class of a moment: PREMARKET | REGULAR | AFTER_HOURS | CLOSED (pure)
  policy.py     the auto-run settings, the startup preflight (READY | DEGRADED | BLOCKED), the auto-disarm conditions and the scheduled-arm
                conditions - pure functions over already-gathered facts
  facts.py      the read-only fact gatherer: databases, in-memory executor state, the account refresh snapshot, the cached Robinhood view
                (refresh=False); never a broker call, never a Robinhood read, never the executor lock
  service.py    AutoRunService: the startup step (before the executor thread exists), the supervisor tick and the Bot Activity view. Its only
                actions are the existing ExecutionService.disable_auto (new entries stop; monitoring and exits continue) and, under an explicitly
                enabled SCHEDULED_ARM, the existing ExecutionService.enable_auto with all of its own refusals
  scheduler.py  the supervisor thread (never in a test / harness process) and the long-held single-instance lease
  store.py      autorun events, preflights and one record per trading day (database/autorun_migrations.py)

Order decisions (entry controls, exits, sizing, risk) are not touched by anything here. PAPER only: nothing here talks to a broker.
"""
import os
import uuid

VERSION = "autorun_v1"
STAGE = "6.10A"

# the auto-run policies: MANUAL_ARM is the default; SCHEDULED_ARM needs PAPER_AUTO_SCHEDULED_ARM_ENABLED=true as well
MANUAL_ARM, SCHEDULED_ARM = "MANUAL_ARM", "SCHEDULED_ARM"
POLICIES = (MANUAL_ARM, SCHEDULED_ARM)
POLICY_LABELS = {MANUAL_ARM: "Manual arm · starts in Monitor only", SCHEDULED_ARM: "Scheduled arm · regular session only"}

# market-session classes (entries stay regular-session only through the executor's unchanged SESSION_POLICY control)
PREMARKET, REGULAR, AFTER_HOURS, CLOSED = "PREMARKET", "REGULAR", "AFTER_HOURS", "CLOSED"
SESSIONS = (PREMARKET, REGULAR, AFTER_HOURS, CLOSED)
PREMARKET_MINUTE, AFTER_HOURS_END_MINUTE = 4 * 60, 20 * 60          # 04:00 and 20:00 America/New_York

# preflight
READY, DEGRADED, BLOCKED = "READY", "DEGRADED", "BLOCKED"
PREFLIGHT_STATUSES = (READY, DEGRADED, BLOCKED)
PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

# the actors written into execution_mode_events (distinct from the Stage 6.1 early stop's 'forward_validation' and the user's 'user')
ACTOR_POLICY, ACTOR_SCHEDULED_ARM = "autorun_policy", "autorun_scheduled_arm"

# auto-disarm codes: Paper Auto -> Monitor only (new entries stop; monitoring and exits of existing bot positions continue)
DISARM_CODES = ("EMERGENCY_STOP", "PAPER_PROOF_LOST", "RECONCILIATION_AMBIGUITY", "ACCOUNT_EQUITY_UNAVAILABLE", "BROKER_READ_FAILURES", "WORKER_FATAL",
                "DATABASE_FAILURE", "UNSUPPORTED_CONTROLS", "DUPLICATE_INSTANCE", "FOREIGN_INSTANCE", "SESSION_ENDED")
STARTUP_DISARM = "STARTUP_MANUAL_ARM"
STARTUP_REASON = "MANUAL_ARM: process start"   # the startup demotion's mode-event reason prefix (never an operator's disarm)

# the entry-controls versions this auto-run policy was written and tested against (execution/exposure_gate.py VERSION)
SUPPORTED_CONTROLS_VERSIONS = ("entry_controls_v2",)

# deterministic thresholds (versioned with the module, recorded in the config snapshot)
EQUITY_UNAVAILABLE_SECONDS = 300            # account data not FRESH this long inside the regular session -> disarm
BROKER_FAILURES_TO_DISARM = 3               # consecutive failed account refreshes seen by THIS process -> disarm
WORKER_ERROR_TICKS_TO_DISARM = 3            # supervisor ticks in a row that saw an executor loop error -> disarm
PROOF_FAILURES_TO_DISARM = 3                # consecutive failed paper proofs (transient codes) -> disarm
LIVENESS_GRACE_SECONDS = 60                 # the executor loop is stale only past its own next_at (or MAX_SLEEP_S) by this much
INSTANCE_LEASE = "stock_agent_instance"     # the long-held single-instance lease (taken at startup, renewed first in every tick, released on stop)
SUPERVISOR_REGULAR_S, SUPERVISOR_IDLE_S = 30.0, 60.0   # supervisor cadence in / outside the regular session (database reads only: no broker call)
INSTANCE_LEASE_TTL_MIN = 3                  # >= 3x the longest supervisor tick: a live owner always renews it; a crashed one's expires within 3 min
LOCK_WAIT_S = 20.0                          # the supervisor never waits longer for the executor's lock (a long cycle): it retries at the next tick
STARTUP_DEMOTION_ATTEMPTS, STARTUP_DEMOTION_BACKOFF_S = 3, 0.5
PROOF_DEFINITIVE_ERRORS = ("NOT_CONFIGURED", "HTTP_401", "HTTP_403")   # a paper proof that cannot succeed by waiting

DISCLAIMER = ("Auto-run decides only WHEN Paper Auto is armed: it never places, changes or cancels an order. Alpaca PAPER only; Robinhood is read "
              "only and never traded. Disarming stops new entries; monitoring and exits of existing bot positions continue.")

_INSTANCE = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


def instance_id() -> str:
    """This process's identity in the single-instance lease and in every auto-run / capture record (pid:random; no secret)."""
    return _INSTANCE
