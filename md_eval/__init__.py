"""
md_eval/ - MD-B: the offline portfolio evaluation engine (a cash ledger and the frozen book), dated fees, benchmarks, statistics, the
pre-registration template, the trial registry, the holdout ledger and the verdict. Research only; long only; deterministic; offline.

It consumes ONLY the shared historical data contract v2 = HISTORICAL_DATA_CONTRACT_v1.md revision 2 (the base, pinned by its sha256) plus
HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md (the contract_sha256 of v2):
  - decisions read only provider.as_of(t) (a PointInTimeView);
  - fills read only provider.execution_open(instrument_id, session, *, decision_session) (A2), and only in the engine's fill step;
  - full history (bars, events) is read only for ledger bookings, valuation marks and reporting.

The book is a FROZEN evaluation assumption (owner), never an optimised parameter: at most 4 positions, at most 2 new a day, at most 2 a
sector, 0.5 % risk on current equity, a 10 % position cap, no margin, cash never below 0. No run may override it.
"""
from types import MappingProxyType

ENGINE_VERSION = "md_eval_v1"

CONTRACT_V1 = "historical_data_contract_v1"
CONTRACT_V1_SHA256 = "453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7"     # the base, revision 2 final
CONTRACT_V2 = "historical_data_contract_v2"
CONTRACT_V2_SHA256 = "7850580ba22195e92ee03f71e75d2d7eb3719176343e90f485ce47c3ac876ed8"     # the v2 amendment file (A12)
SUPPORTED_CONTRACTS = MappingProxyType({CONTRACT_V2: CONTRACT_V2_SHA256})                   # (version -> sha256); anything else is refused (§8)
CONTRACT_BASE_OF = MappingProxyType({CONTRACT_V2: (CONTRACT_V1, CONTRACT_V1_SHA256)})      # v2 pins its base by sha (amendment line 3)

BOOK = MappingProxyType({                                                                   # FROZEN (owner): an evaluation assumption
    "max_positions": 4,
    "max_new_per_session": 2,
    "max_per_sector": 2,
    "risk_fraction_of_current_equity": 0.005,
    "max_position_pct_of_current_equity": 10.0,
    "margin": False,
})
BOOK_ROLES = ("STRATEGY", "BENCHMARK")      # STRATEGY: the frozen book; BENCHMARK: buy-and-hold weights, no slot limits, same ledger and costs

# ---- flags (each is reported in the result; the starred ones cap the verdict at NOT_ESTABLISHED) -------------------------------------------
FIXED_CURRENT_UNIVERSE = "FIXED_CURRENT_UNIVERSE"                   # * (contract §3.4, §7)
SURVIVORSHIP_BIAS_PRESENT = "SURVIVORSHIP_BIAS_PRESENT"
SPLIT_UNADJUSTED = "SPLIT_UNADJUSTED"                               # * (contract §7)
CORPORATE_ACTION_UNACCOUNTED = "CORPORATE_ACTION_UNACCOUNTED"       # * (amendment A7)
STATIC_CLASSIFICATION_MAP = "STATIC_CLASSIFICATION_MAP"             # * (amendment A4, P1 exception iii)
SYMBOL_HISTORY_UNKNOWN = "SYMBOL_HISTORY_UNKNOWN"                   # (amendment A8, P1 exception iv)
EXITS_NEXT_OPEN = "EXITS_NEXT_OPEN"                                 # (amendment A3)
SIGNAL_BASIS_SPLIT_ADJUSTED = "SIGNAL_BASIS_SPLIT_ADJUSTED"         # (decision D2)
NO_FILL_GAP = "NO_FILL_GAP"
STALE_MARK = "STALE_MARK"                                           # (amendment A5)
SIZING_FROM_STALE_EQUITY = "SIZING_FROM_STALE_EQUITY"               # (amendment A5)
POSSIBLE_DELISTING = "POSSIBLE_DELISTING"                           # (amendment A5)
FRACTION_UNPAID = "FRACTION_UNPAID"
PAY_DATE_UNKNOWN = "PAY_DATE_UNKNOWN"
DELISTING_RETURN_UNKNOWN = "DELISTING_RETURN_UNKNOWN"
DELISTING_LAST_PRICE_SENSITIVITY = "DELISTING_LAST_PRICE_SENSITIVITY"
NO_SIGNALS_SPLIT_VIEW_UNAVAILABLE = "NO_SIGNALS_SPLIT_VIEW_UNAVAILABLE"
LIQUIDITY_BUCKET_CONSERVATIVE = "LIQUIDITY_BUCKET_CONSERVATIVE"
COST_BUCKETS_ASSUMPTION = "COST_BUCKETS_ASSUMPTION"                 # (decision D4: ASSUMPTION defaults, fixed only in a pre-registration)
REGULATORY_FEE_HISTORY_MISSING = "REGULATORY_FEE_HISTORY_MISSING"
TAF_SOURCE_DISCREPANCY_UNVERIFIED = "TAF_SOURCE_DISCREPANCY_UNVERIFIED"
CAT_FEE_NOT_MODELLED = "CAT_FEE_NOT_MODELLED"
COMMISSION_ASSUMED_UNVERIFIED = "COMMISSION_ASSUMED_UNVERIFIED"
CASH_RATE_HYPOTHETICAL = "CASH_RATE_HYPOTHETICAL"
VERDICT_CAPS = (FIXED_CURRENT_UNIVERSE, SPLIT_UNADJUSTED, CORPORATE_ACTION_UNACCOUNTED, STATIC_CLASSIFICATION_MAP)

NOT_TAKEN_REASONS = ("ALREADY_HELD", "EMBARGO", "PURGE_BOUNDARY", "MAX_NEW_PER_SESSION", "MAX_POSITIONS", "MAX_PER_SECTOR", "INVALID_STOP",
                     "NO_DECISION_PRICE", "SIZE_BELOW_ONE_SHARE", "CASH", "NO_FILL_GAP", "POSITION_GONE", "SPLIT_RATIO_UNKNOWN_LEVELS_UNCONVERTED")

DISCLAIMER = ("Research only. Backtest and benchmark results are hypothetical; a constant cash rate other than zero is HYPOTHETICAL; cost "
              "buckets are ASSUMPTION defaults. A verdict of ESTABLISHED is eligible for human review, never a promotion or a forecast.")
