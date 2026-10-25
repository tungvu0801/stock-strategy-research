"""
strategy_validation/ — Stage 6.2 STRATEGY VALIDATION HARNESS (research / validation only; STRATEGY_VALIDATION_HARNESS.md, DESIGN_62_STRATEGY_VALIDATION.md).

Strategy -> candidate generation (ONCE) -> immutable candidate set -> RULES / GATED / MODEL arms -> simulation (friction, causal fills, STOP_FIRST)
-> chronological TRAIN / OOS -> metrics -> independent mechanical audit -> deterministic verdict -> human lifecycle review.

The harness exists to DISPROVE weak strategies. A verdict is INSUFFICIENT_DATA, FAIL, MIXED, PASS or INVALID (an integrity failure: no conclusion
is drawn). PASS means "eligible for human review for lifecycle advancement" - never a promotion, never a forecast. Backtest results are
hypothetical. Nothing in this package places, previews or cancels an order, enables Paper Auto, touches a broker, calls a model or the network,
or changes any strategy, risk, safety or execution setting; it imports no execution, broker, paper-order or proposal code.
"""
VERSION = "validation_v2"                       # the methodology version: any policy change below is a new validation_version
                                                # v2 (Stage 6.3.1): the TRAIN / OOS boundary PURGE, the accounting invariant, the persistence integrity gate

VERDICTS = ("INSUFFICIENT_DATA", "FAIL", "MIXED", "PASS", "INVALID")
VERDICT_LABELS = {"INSUFFICIENT_DATA": "INSUFFICIENT DATA", "FAIL": "FAIL", "MIXED": "MIXED", "PASS": "PASS", "INVALID": "VALIDATION INVALID"}
STATUSES = ("NOT_RUN", "RUNNING") + VERDICTS     # RUNNING is never persisted: a run is written once, when it is complete
ARMS = ("RULES", "GATED", "MODEL")
ARM_STATUSES = ("EVALUATED", "NOT_EVALUATED", "INVALID")
SEGMENTS = ("TRAIN", "OOS", "ALL")
DIRECTIONS = ("LONG",)                          # Stage 6.2 simulates long candidates only (no shorting is added anywhere)

# ---- sample-size policy (explicit constants; never tuned to make a strategy pass) ------------------------------------------------------
MIN_TRADES = 30                                 # RULES arm, TRAIN + OOS together
MIN_OOS_TRADES = 20                             # RULES arm, OOS alone
MIN_OOS_SESSIONS = 60                           # an OOS window shorter than this (dataset sessions) is inadequate
MIN_SHARPE_TRADES = 30                          # below it the per-trade Sharpe is not reported
T_STAT_WEAK = 2.0                               # |t| below this = weak statistical evidence (evidence, never an oracle)
DEFAULT_TRAIN_RATIO = 0.70                      # chronological development share; the remainder is the untouched OOS slice
TRAIN_RATIO_BOUNDS = (0.50, 0.90)

# ---- diagnostics policy -----------------------------------------------------------------------------------------------------------
DEGRADATION_RATIO = 0.50                        # OOS mean R below half of TRAIN mean R = degradation
NEAR_ZERO_R = 0.05                              # |mean R| below this = near zero
CONCENTRATION_SEVERE = 0.80                     # one period carrying >= 80 % of total net P/L = severe concentration (blocks PASS)
CONCENTRATION_WARN = 0.50
FRICTION_HEAVY = 0.50                           # friction above half of gross profit = friction-heavy (blocks PASS)
AMBIGUOUS_SHARE_WARN = 0.10                     # more than 10 % of trades decided inside an ambiguous bar
AMBIGUOUS_DEPENDENCE = 0.25                     # resolving the ambiguous bars target-first would move total net P/L by > 25 %
SENSITIVITY_SPREAD = 0.50                       # neighbour mean R further than 50 % from the base = SENSITIVE

# ---- execution / friction assumptions (the Stage 3.2 backtest convention, backtest/engine.py) -------------------------------------------
ENTRY_POLICIES = ("NEXT_BAR_OPEN", "SIGNAL_CLOSE_MOC")   # a close-based signal is known at the close: the default entry is the next bar's open
AMBIGUOUS_POLICIES = ("STOP_FIRST",)            # stop and target inside one bar with unknown ordering: the stop is assumed first
EXIT_REASONS = ("STOP", "STOP_GAP", "TARGET", "TIME_EXIT", "DATA_END")
DEFAULT_FRICTION = {"slippage_bps_per_side": 5.0, "commission_per_order": 0.0, "fee_per_share": 0.0}
DEFAULT_SIZING = {"equity": 100000.0, "risk_fraction": 0.005, "max_position_pct": 10.0}   # the Stage 5.9 conventions (research simulation only)
SKIP_REASONS = ("MISSING_CRITICAL_FEATURE", "STANDARD_V1_INVALID", "UNSUPPORTED_DIRECTION", "DUPLICATE_CANDIDATE", "SIGNAL_BAR_NOT_IN_DATASET",
                "NO_ENTRY_BAR", "INVALID_STOP", "ENTRY_GAP_THROUGH_STOP", "ENTRY_GAP_THROUGH_TARGET", "TRAIN_OOS_BOUNDARY_PURGE")

# ---- Stage 6.3.1: the TRAIN / OOS boundary and the persistence integrity gate (strategy_validation/integrity.py) ---------------------------
BOUNDARY_POLICY = "PURGE"                       # a TRAIN candidate whose outcome needs a bar after the TRAIN end is excluded from TRAIN and OOS
PURGE_REASON = "TRAIN_OOS_BOUNDARY_PURGE"
INTEGRITY_STATUSES = ("PASS", "FAIL")          # validation integrity - a different question from the performance verdict
ELIGIBILITY_STATUSES = ("ELIGIBLE", "BLOCKED")  # may this run be persisted?
PARITY_REQUIREMENTS = ("EXACT_REQUIRED", "NOT_APPLICABLE")   # EXACT_REQUIRED: the adapter claims to reproduce a separate live implementation
PARITY_STATUSES = ("ESTABLISHED", "NOT_ESTABLISHED")
REVIEW_STATUSES = ("PENDING", "COMPLETED")      # the adapter's declared MANUAL causality review (never inferred from the automated checks)
PROVENANCE_REQUIRED = ("source", "retrieved_at", "bar_frequency", "adjustment", "missing_data_policy")   # declared by the dataset
PROVENANCE_DERIVED = ("dataset_id", "start", "end", "symbols", "sessions")                               # computed from the bars
PROVENANCE_CONDITIONAL = ("corporate_actions", "universe", "point_in_time")                              # required when the adapter declares them
BAR_FREQUENCY = "1Day"                          # the simulator works on completed daily sessions only

DISCLAIMER = ("Backtest results are hypothetical. PASS means eligible for human review for lifecycle advancement; it does not mean profitable in "
              "the future and it never promotes a strategy. A failed validation is a valid research result.")
