"""
evidence_refresh - Stage 5.4.2: automatic, zero-model-call refresh of the stored Stage 5.4 evidence packets
(DESIGN_542_EVIDENCE_REFRESH.md).

The setup's READY check (setups.EVIDENCE_MAX_AGE_DAYS) needs a COMPLETED Stage 5.4 packet that is at most 30 days old, and the only
way a packet was ever built was a person pressing Build (POST /api/evidence/packets). This package rebuilds a watchlist symbol's
packet through the SAME engine call that route makes, before it goes stale:

* policy.py   - pure: which symbols are FRESH / DUE / STALE / MISSING, which are in a retry back-off, which to build now.
* service.py  - one refresh run: mode, session, network and lease checks, then sequential builds with a re-check before each one.
* scheduler.py - the one daemon thread per process (never in a test / harness process), default OFF.

Boundaries: no model call (the optional explanation endpoint is never touched), no broker call, no order, no Robinhood call, no
threshold or READY-rule change, no schema change, no deletion. Packets stay append-only: a refresh only adds a packet; the gate keeps
reading the newest COMPLETED one (Stage 5.4.1), so a FAILED refresh never hides the packet in use.
"""
from __future__ import annotations

VERSION = "evidence_refresh_v1"
LEASE_NAME = "evidence_refresher"            # the shared app_leases row: one refresher per database, whatever the process
MODES = ("OFF", "DRY_RUN", "ON")             # OFF: no thread. DRY_RUN: the thread plans and logs, builds nothing. ON: builds.

# per-symbol states (policy.symbol_state)
FRESH, DUE, STALE, MISSING = "FRESH", "DUE", "STALE", "MISSING"
# per-symbol decisions
WAIT, BUILD, BACKOFF = "WAIT", "BUILD", "BACKOFF"
# back-off reasons
RETRY_WAIT, FAILURE_CAP = "RETRY_WAIT", "FAILURE_CAP"

# run results (service.EvidenceRefresher.run_once)
R_OFF = "OFF"
R_RUN_IN_PROGRESS = "RUN_IN_PROGRESS"
R_SKIPPED_SESSION = "SKIPPED_REGULAR_SESSION"
R_SKIPPED_NETWORK = "SKIPPED_NETWORK_OFF"
R_LEASE_HELD = "LEASE_HELD"
R_NOTHING_DUE = "NOTHING_DUE"
R_DRY_RUN = "DRY_RUN"
R_COMPLETED = "COMPLETED"
R_STOPPED = "STOPPED"
R_ERROR = "ERROR"

# per-attempt outcomes
A_BUILT, A_FAILED, A_ERROR, A_SKIPPED_FRESH, A_WOULD_BUILD = "BUILT", "FAILED", "ERROR", "SKIPPED_ALREADY_FRESH", "WOULD_BUILD"

# a run stops (circuit open) on these: the source refused us, so asking again now is impolite and useless
CIRCUIT_FETCH_STATUSES = ("ACCESS_DENIED",)
MAX_CONSECUTIVE_ERRORS = 2

DISCLAIMER = ("Evidence refresh rebuilds the stored Stage 5.4 research packets on a schedule, outside the regular session, through the "
              "same engine as the Build button. It makes no model call, no broker call and no order, and changes no READY rule or threshold.")
