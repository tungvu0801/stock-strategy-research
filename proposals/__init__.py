"""
proposals/ — Stage 5.9 DETERMINISTIC TRADE PROPOSAL / REVIEW LAYER (DESIGN_59_TRADE_PROPOSAL_REVIEW.md).

A READY FOR REVIEW setup is not a trade. For such a setup this package builds a REVIEW ARTIFACT: a deterministic, risk-bounded
long review proposal (reference price, invalidation level, risk per share, account risk budget, capped whole-share quantity,
blockers, warnings, control evidence, expiry). It is never a recommendation, never an order and never submitted anywhere:
no broker write, no order writer import, no model call, no target price, no expected return, no probability of profit.
"""
VERSION = "proposals_v1"
SETUP_TYPE = "DOWNSIDE_DISLOCATION_RECOVERY"
SIDE_CONTEXT = "LONG_REVIEW"
SIDE_LABEL = "Long review proposal"
STATUSES = ("NOT_ELIGIBLE", "BLOCKED", "REVIEWABLE", "STALE", "EXPIRED")
STATUS_LABELS = {"NOT_ELIGIBLE": "NOT ELIGIBLE", "BLOCKED": "BLOCKED", "REVIEWABLE": "REVIEWABLE PROPOSAL", "STALE": "STALE", "EXPIRED": "EXPIRED"}
REVIEW_STATES = ("UNREVIEWED", "VIEWED", "DISMISSED")
SUBMISSION_STATUS = "NOT SUBMITTED"
DISCLAIMER = "This is a deterministic review proposal, not a recommendation or submitted order."
PRICE_MODES = ("INTRADAY", "DAILY_REFERENCE")
PRICE_SOURCES = ("QUOTE_MID", "LAST_TRADE", "MINUTE_CLOSE", "DAILY_CLOSE")
STOP_RULES = ("STRUCTURAL", "ATR_CAP", "ATR_FALLBACK")
EQUITY_SOURCES = ("ALPACA_PAPER_VIEW", "LOCAL_PAPER", "HYPOTHETICAL_REVIEW", "NOT_AVAILABLE")
TRIGGERS = ("SCHEDULED", "SUBSCRIBE", "REFRESH", "MANUAL", "SETUP_EVENT", "TEST")

# ---- fixed V1 rules (DESIGN_59 §4–§8; written before any outcome analysis, never tuned) -----------------------------------------
ATR_PERIOD = 14                               # the project's ATR convention (config.ATR_PERIOD default)
STRUCTURAL_LOOKBACK_SESSIONS = 5
STRUCTURAL_BUFFER_ATR = "0.25"
FALLBACK_STOP_ATR = "1.5"
INTRADAY_MIN_BARS = 30                        # completed minute candles before the session low counts (the Stage 5.7 intraday threshold)
MAX_SPREAD_PCT = "1.0"                        # quote midpoint usable only when (ask - bid) / mid <= 1 %
PRICE_MAX_AGE_SECONDS = 300
MINUTE_BAR_MAX_AGE_SECONDS = 900
SETUP_MIN_FRESH_WINDOW_SECONDS = 300
ACCOUNT_SNAPSHOT_WARN_AGE_SECONDS = 900
RISK_FRACTION_MAX = "0.01"                    # never more than 1 % of equity per review proposal
CONFIDENCE_MODERATE_BELOW = 60
DEMAND_MARGINAL_BELOW = 70
NEAR_CLOSE_SECONDS = 15 * 60
DAILY_REFERENCE_MAX_TTL_SECONDS = 24 * 3600

CONTROLS = ("SETUP_FRESHNESS", "PPE_FRESHNESS", "DEMAND_FRESHNESS", "EVIDENCE_GATE", "PRICE_REFERENCE", "INVALIDATION_LEVEL", "RISK_PER_SHARE",
            "ACCOUNT_EQUITY", "RISK_BUDGET", "KILL_SWITCH", "DAILY_LOSS_HALT", "TRADABILITY", "DUPLICATE_ORDER", "BUYING_POWER", "POSITION_CAP", "QUANTITY",
            "NO_NEW_ENTRIES")   # Stage 5.9.1: the persisted pause (WARN: the proposal stays visible, entries are blocked)
CONTROL_RESULTS = ("PASS", "BLOCK", "WARN", "UNKNOWN")

# ---- not eligible (computed, never stored) --------------------------------------------------------------------------------------
NE_NOT_FOUND = "SETUP_NOT_FOUND"
NE_TYPE = "SETUP_TYPE_UNSUPPORTED"
NE_NOT_READY = "SETUP_NOT_READY"
NE_INVALIDATED = "SETUP_INVALIDATED"
NOT_ELIGIBLE_CODES = (NE_NOT_FOUND, NE_TYPE, NE_NOT_READY, NE_INVALIDATED)

# ---- hard blockers (status BLOCKED) ----------------------------------------------------------------------------------------------
B_SETUP_STALE = "SETUP_STALE"
B_PPE_STALE = "PPE_STALE"
B_PPE_NA = "PPE_NOT_AVAILABLE"
B_DEMAND_STALE = "DEMAND_STALE"
B_DEMAND_NA = "DEMAND_NOT_AVAILABLE"
B_EVIDENCE_BLOCKED = "EVIDENCE_BLOCKED"       # suffixed with the gate code: EVIDENCE_BLOCKED:READY_BLOCKED_NO_EVIDENCE
B_EVIDENCE_CONTRA = "EVIDENCE_CONTRADICTION"
B_PRICE_NA = "PRICE_NOT_AVAILABLE"
B_PRICE_STALE = "PRICE_STALE"
B_PRICE_INVALID = "PRICE_INVALID"
B_ATR_NA = "ATR_NOT_AVAILABLE"
B_INVALID_STOP = "INVALID_STOP"
B_RPS_INVALID = "RISK_PER_SHARE_INVALID"
B_EQUITY_NA = "EQUITY_NOT_AVAILABLE"
B_FRACTION = "RISK_FRACTION_INVALID"
B_BUDGET_ZERO = "RISK_BUDGET_ZERO"
B_KILL = "KILL_SWITCH_ACTIVE"
B_DAILY_LOSS = "DAILY_LOSS_HALT_ACTIVE"
B_NOT_TRADABLE = "SYMBOL_NOT_TRADABLE"
B_DUPLICATE = "DUPLICATE_PENDING_ORDER"
B_BUYING_POWER = "BUYING_POWER_INSUFFICIENT"
B_CAP_EXCEEDED = "POSITION_CAP_EXCEEDED"
B_QTY_ZERO = "QUANTITY_BELOW_ONE_SHARE"
BLOCKER_CODES = (B_SETUP_STALE, B_PPE_STALE, B_PPE_NA, B_DEMAND_STALE, B_DEMAND_NA, B_EVIDENCE_BLOCKED, B_EVIDENCE_CONTRA, B_PRICE_NA, B_PRICE_STALE,
                 B_PRICE_INVALID, B_ATR_NA, B_INVALID_STOP, B_RPS_INVALID, B_EQUITY_NA, B_FRACTION, B_BUDGET_ZERO, B_KILL, B_DAILY_LOSS, B_NOT_TRADABLE,
                 B_DUPLICATE, B_BUYING_POWER, B_CAP_EXCEEDED, B_QTY_ZERO)

# ---- warnings (the proposal still renders) ------------------------------------------------------------------------------------------
W_EVIDENCE_WEAK = "EVIDENCE_WEAK"
W_CONFIDENCE = "CONFIDENCE_MODERATE"
W_DEMAND = "DEMAND_MARGINAL"
W_DAILY_REF = "DAILY_REFERENCE_PRICE"
W_EXISTING = "EXISTING_POSITION"
W_NEAR_CLOSE = "MARKET_NEAR_CLOSE"
W_CLOSED = "MARKET_CLOSED"
W_SECTOR_FALLBACK = "SECTOR_FALLBACK"
W_TRADABILITY = "TRADABILITY_UNKNOWN"
W_BP_UNKNOWN = "BUYING_POWER_UNKNOWN"
W_HYPOTHETICAL = "EQUITY_HYPOTHETICAL"
W_SNAPSHOT_STALE = "ACCOUNT_SNAPSHOT_STALE"
W_SIMULATED = "SIMULATED_DATA"
W_CAP_REDUCED = "QTY_REDUCED_BY_POSITION_CAP"
W_BP_REDUCED = "QTY_REDUCED_BY_BUYING_POWER"
W_HARD_REDUCED = "QTY_REDUCED_BY_HARD_LIMIT"
W_NO_NEW_ENTRIES = "NO_NEW_ENTRIES_ACTIVE"     # Stage 5.9.1
WARNING_CODES = (W_EVIDENCE_WEAK, W_CONFIDENCE, W_DEMAND, W_DAILY_REF, W_EXISTING, W_NEAR_CLOSE, W_CLOSED, W_SECTOR_FALLBACK, W_TRADABILITY, W_BP_UNKNOWN,
                 W_HYPOTHETICAL, W_SNAPSHOT_STALE, W_SIMULATED, W_CAP_REDUCED, W_BP_REDUCED, W_HARD_REDUCED, W_NO_NEW_ENTRIES)

TEXT = {
    NE_NOT_FOUND: "No open setup episode for this ticker.",
    NE_TYPE: "Only the Downside Dislocation Recovery setup is supported.",
    NE_NOT_READY: "The setup is not READY FOR REVIEW.",
    NE_INVALIDATED: "The setup is INVALIDATED.",
    B_SETUP_STALE: "The setup evaluation is older than the freshness window.",
    B_PPE_STALE: "PPE is stale.",
    B_PPE_NA: "PPE is not available.",
    B_DEMAND_STALE: "Market Demand is stale.",
    B_DEMAND_NA: "Market Demand is not available.",
    B_EVIDENCE_BLOCKED: "Evidence blocks the review.",
    B_EVIDENCE_CONTRA: "New strong company-specific evidence contradicts the setup.",
    B_PRICE_NA: "No reference price is available.",
    B_PRICE_STALE: "The reference price is stale.",
    B_PRICE_INVALID: "The price source returned an invalid value.",
    B_ATR_NA: "ATR cannot be computed (fewer than 15 completed sessions).",
    B_INVALID_STOP: "The invalidation level is not below the reference price.",
    B_RPS_INVALID: "Risk per share is not positive.",
    B_EQUITY_NA: "Account equity is not available (refresh the Alpaca Paper view, set up the local paper portfolio, or set the review equity).",
    B_FRACTION: "The configured risk fraction is outside 0 < f <= 0.01.",
    B_BUDGET_ZERO: "The risk budget is zero.",
    B_KILL: "The review halt is active.",
    B_DAILY_LOSS: "The daily-loss halt is active.",
    B_NOT_TRADABLE: "The broker reports the symbol as not tradable.",
    B_DUPLICATE: "A pending paper order or unresolved intent exists for this symbol.",
    B_BUYING_POWER: "Buying power does not cover one share with the 5 % buffer.",
    B_CAP_EXCEEDED: "The existing position already reaches the position cap.",
    B_QTY_ZERO: "The capped quantity is below one whole share.",
    W_EVIDENCE_WEAK: "Evidence strength is WEAK.",
    W_CONFIDENCE: "PPE Confidence is only moderate.",
    W_DEMAND: "Market Demand is only slightly above the review threshold.",
    W_DAILY_REF: "The reference price is the last completed daily close (DAILY_REFERENCE).",
    W_EXISTING: "A position already exists in this account source.",
    W_NEAR_CLOSE: "The regular session closes within 15 minutes.",
    W_CLOSED: "The regular session is closed.",
    W_SECTOR_FALLBACK: "PPE used the market proxy instead of a sector benchmark.",
    W_TRADABILITY: "Tradability is unknown (no broker asset metadata without a network call).",
    W_BP_UNKNOWN: "Buying power is unknown for this account source.",
    W_HYPOTHETICAL: "Equity is the configured hypothetical review equity (simulated mode).",
    W_SNAPSHOT_STALE: "The Alpaca Paper snapshot is older than 15 minutes.",
    W_SIMULATED: "Simulated market data.",
    W_CAP_REDUCED: "Quantity reduced by the position cap.",
    W_BP_REDUCED: "Quantity reduced by buying power.",
    W_HARD_REDUCED: "Quantity reduced by the 4.6B hard limit.",
    W_NO_NEW_ENTRIES: "New entries are paused (Risk Controls); the proposal is for review only.",
}
