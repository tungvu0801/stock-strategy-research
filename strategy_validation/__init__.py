"""
strategy_validation/ — Stage 6.2 STRATEGY VALIDATION HARNESS (research / validation only; STRATEGY_VALIDATION_HARNESS.md, DESIGN_62_STRATEGY_VALIDATION.md).

Strategy -> candidate generation (ONCE) -> immutable candidate set -> RULES / GATED / MODEL arms -> simulation (friction, causal fills, STOP_FIRST)
-> chronological TRAIN / OOS -> metrics -> independent mechanical audit -> deterministic verdict -> human lifecycle review.

The harness exists to DISPROVE weak strategies. A verdict is INSUFFICIENT_DATA, FAIL, MIXED, PASS or INVALID (an integrity failure: no conclusion
is drawn). PASS means "eligible for human review for lifecycle advancement" - never a promotion, never a forecast. Backtest results are
hypothetical. Nothing in this package places, previews or cancels an order, enables Paper Auto, touches a broker, calls a model or the network,
or changes any strategy, risk, safety or execution setting; it imports no execution, broker, paper-order or proposal code.
"""
VERSION = "validation_v3"                       # the methodology version: any policy change below is a new validation_version
                                                # v1 (Stage 6.2): candidates once, three arms, friction, TRAIN / OOS, audit, verdict
                                                # v2 (Stage 6.3.1): the TRAIN / OOS boundary PURGE, the accounting invariant, the persistence integrity gate
                                                # v3 (Stage 6.4.1): cross-sectional portfolio strategies are judged on rebalance-period excess over a
                                                #     declared SELECTION CONTROL (strategy_validation/cross_sectional.py); event-driven strategies unchanged

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
SIZING_MODES = ("RISK", "EQUAL_WEIGHT")         # Stage 6.4: EQUAL_WEIGHT = position_pct of the fixed equity per position (a stop-less portfolio
                                                # strategy); its R is the return on the position's notional, documented as such
STOP_POLICIES = ("INVALIDATION", "NONE")
SKIP_REASONS = ("MISSING_CRITICAL_FEATURE", "STANDARD_V1_INVALID", "UNSUPPORTED_DIRECTION", "DUPLICATE_CANDIDATE", "SIGNAL_BAR_NOT_IN_DATASET",
                "NO_ENTRY_BAR", "INVALID_STOP", "ENTRY_GAP_THROUGH_STOP", "ENTRY_GAP_THROUGH_TARGET", "TRAIN_OOS_BOUNDARY_PURGE",
                "INVALID_HOLD")

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

# ---- Stage 6.4.1 (validation_v3): cross-sectional portfolio strategies - the SELECTION EDGE against a control (strategy_validation/cross_sectional.py) --
# Fixed BEFORE the policy was applied to any strategy result (DESIGN_641_CROSS_SECTIONAL_VALIDATION.md section 6); never tuned to a result.
EVALUATION_MODES = ("EVENT_DRIVEN", "CROSS_SECTIONAL_PORTFOLIO")   # declared by the adapter - never inferred from trade counts
EDGE_STATUSES = ("NOT_APPLICABLE", "INSUFFICIENT_DATA", "NOT_ESTABLISHED", "WEAK", "ESTABLISHED", "INVALID")
EDGE_LABELS = {"NOT_APPLICABLE": "Not applicable", "INSUFFICIENT_DATA": "Insufficient data", "NOT_ESTABLISHED": "Not established", "WEAK": "Weak",
               "ESTABLISHED": "Established", "INVALID": "Invalid (control mismatch)"}
CONTROL_ROLE = "SELECTION_CONTROL"              # the control that omits the selection: the edge is measured against it
BENCHMARK_ROLE = "MARKET_BENCHMARK"             # SPY: a market comparison only, never selection-edge evidence
CAPITAL_CONVENTION = "EQUAL_CAPITAL_BUY_AND_HOLD"   # both sides: equal capital per position at the period's entry, held to the period's exit
CS_MIN_OOS_PERIODS = 12                         # fewer OOS rebalance periods: the selection edge is INSUFFICIENT_DATA
CS_MIN_TOTAL_PERIODS = 24                       # fewer TRAIN + OOS periods: the edge is at most WEAK
CS_MIN_PERIODS_T = 6                            # fewer periods in a segment: no excess t-statistic is reported for it
CS_T_NEGLIGIBLE = 1.0                           # OOS period-excess t below this: NOT_ESTABLISHED (statistically negligible)
CS_T_ESTABLISHED = 2.0                          # OOS period-excess t at / above this: one condition of ESTABLISHED - never the only one
CS_TRAIN_CONTRADICTION_T = -2.0                 # TRAIN mean excess < 0 with t <= this: TRAIN and OOS strongly contradict (no ESTABLISHED)
CS_CONCENTRATION_SHARE = 0.50                   # one period carrying > 50 % of the summed excess, or the excess vanishing without its best
                                                # period: concentrated (a warning; no ESTABLISHED)

# ---- Stage 6.5.1: UNIVERSE INTEGRITY (strategy_validation/universe.py, DESIGN_651_UNIVERSE_INTEGRITY.md) - fixed before it was applied to any
# stored result. It extends validation_v3 (whose statistical thresholds above are unchanged) with a cap on the selection edge; it is versioned on
# its own so that a v3 record WITHOUT a universe assessment is never silently reinterpreted.
UNIVERSE_POLICY_VERSION = "universe_integrity_v1"
UNIVERSE_METHODS = ("POINT_IN_TIME", "HISTORICAL_SNAPSHOT", "FIXED_CURRENT_UNIVERSE", "UNKNOWN")   # declared by the adapter - never inferred
UNIVERSE_INTEGRITY_STATUSES = ("TRUSTED", "LIMITED", "UNTRUSTED", "NOT_APPLICABLE")
UNIVERSE_EDGE_CAP = {"TRUSTED": None, "LIMITED": "WEAK", "UNTRUSTED": "NOT_ESTABLISHED", "NOT_APPLICABLE": None}   # the highest final selection edge
EDGE_LADDER = ("NOT_ESTABLISHED", "WEAK", "ESTABLISHED")       # a cap lowers along it; INSUFFICIENT_DATA / INVALID are never raised or lowered
SNAPSHOT_MAX_STALENESS_DAYS = 31                # HISTORICAL_SNAPSHOT: a rebalance more than 31 days after its latest snapshot is a coverage gap
UNIVERSE_DECLARATION_FIELDS = ("universe_method", "universe_source", "membership_as_of_semantics", "includes_delisted", "includes_acquired",
                               "includes_bankrupt", "historical_additions_supported", "historical_removals_supported", "survivorship_bias",
                               "coverage_start", "coverage_end", "notes")   # unknown values are None - never filled in

DISCLAIMER = ("Backtest results are hypothetical. PASS means eligible for human review for lifecycle advancement; it does not mean profitable in "
              "the future and it never promotes a strategy. A failed validation is a valid research result.")
