"""
value_quality/ — Stage 6.4 STRATEGY #2: VALUE + QUALITY (DESIGN_64_VALUE_QUALITY.md). Research only; long only; deterministic; no model.

Hypothesis: companies that are inexpensive versus their fundamentals AND financially strong / profitable may outperform weaker or more
expensive peers over medium-to-long horizons. Momentum and market timing are deliberately absent.

Every rule below was FIXED before any performance figure was seen (pre-registration). Changing any of them is a new STRATEGY_VERSION and a new
untouched evaluation plan - never a tweak after the OOS result.
"""
STRATEGY_ID = "VALUE_QUALITY"
STRATEGY_VERSION = "1.0"
CORE_VERSION = "vq_core_v1"

# ---- universe (a fixed research universe: point-in-time constituents are not available -> SURVIVORSHIP_BIAS_PRESENT) -------------------------
RESEARCH_UNIVERSE = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BAC", "BKNG", "BLK", "BRK.B", "C", "CAT",
                     "CLS", "COST", "CRM", "CSCO", "CVX", "DHR", "DIS", "GE", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTU", "ISRG", "JNJ", "JPM", "KLAC",
                     "KO", "LIN", "LLY", "LOW", "LRCX", "MA", "MCD", "META", "MRK", "MRVL", "MS", "MSFT", "MU", "NEE", "NFLX", "NOW", "NVDA", "ORCL", "PANW",
                     "PEP", "PFE", "PG", "PLD", "PM", "QCOM", "REGN", "RTX", "SBUX", "SCHW", "SNDK", "SPGI", "SYK", "T", "TMO", "TSLA", "TXN", "UBER", "UNH",
                     "UNP", "V", "VRTX", "VZ", "WMT", "XOM")     # the 81 stocks of the Stage 3.2 research store (today's large caps)
BENCHMARK = "SPY"
EXCLUDED_SIC = (6000, 6799)                      # finance, insurance, real estate (banks, brokers, insurers, REITs): book value, leverage and cash
                                                 # flow are not comparable with industrial companies - excluded by SEC SIC code
MIN_PRICE = 5.0                                  # raw close at the rebalance
MIN_MARKET_CAP = 2_000_000_000.0                 # USD, raw close x diluted shares (split-aligned)
MIN_MEDIAN_DOLLAR_VOLUME = 1_000_000.0           # USD, 20-session median of raw close x volume (IEX-only volume: a fraction of consolidated volume)
DOLLAR_VOLUME_SESSIONS = 20
MAX_FUNDAMENTAL_AGE_DAYS = 200                   # the latest fiscal quarter used must end within 200 days of the rebalance
AVAILABILITY_LAG_SESSIONS = 1                    # a fact is usable from the session AFTER its SEC filing date (filing times are not used)

# ---- factors (higher = more attractive after the sign below) ---------------------------------------------------------------------------------
VALUE_FACTORS = ("earnings_yield", "book_to_market", "cash_flow_yield")      # E/P, B/P, OCF/P - each only when its numerator is > 0
QUALITY_FACTORS = ("return_on_assets", "cash_flow_on_assets", "equity_ratio")  # NI/A, OCF/A, Equity/A (low leverage) - negatives rank low
MIN_FACTORS_PER_GROUP = 2                        # a stock needs >= 2 of 3 value AND >= 2 of 3 quality factors, else it is not ranked
VALUE_WEIGHT = 0.5
QUALITY_WEIGHT = 0.5
PREPROCESSING = "cross-sectional percentile rank per factor (average rank for ties); ranks are bounded, so no winsorization is needed"

# ---- portfolio ---------------------------------------------------------------------------------------------------------------------------------
TOP_N = 20                                       # long-only, equal weight, no leverage, no short book
POSITION_PCT = 100.0 / TOP_N                     # 5 % of a fixed hypothetical equity per position (the harness sizing)
REBALANCE = "MONTHLY"                            # signal at the close of the FIRST session of each month; entry at the next session's open
EXIT_FILL = "TIME_EXIT at the open of the first session after the exit rebalance (strictly after the decision; where the next leg would begin)"

# ---- the explicit, pre-declared sensitivity neighbourhood (diagnostic only; never chosen) -------------------------------------------------------
SENSITIVITY = ({"top_n": 15}, {"top_n": 25}, {"value_weight": 0.4}, {"value_weight": 0.6})

# ---- XBRL concepts per field, in priority order (decided on TRAIN data COVERAGE only - no performance was looked at; DESIGN_64 §5). Issuers switch
# concepts over time (BKNG / CAT: NetIncomeLoss -> ...AvailableToCommonStockholdersBasic; MA / AVGO -> ProfitLoss; DIS / RTX / MRK / LIN / T between
# operating cash flow and its continuing-operations variant; PANW reported basic-and-diluted shares). Per period, the highest-priority concept
# VISIBLE at the rebalance wins (point in time); the concept used is recorded with every value. ProfitLoss includes non-controlling interests.
CONCEPTS = {
    "net_income": ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "assets": ("Assets",),
    "equity": ("StockholdersEquity",),                                          # strict: no total-equity substitute (the factor is optional)
    "share_count": ("WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
                    "WeightedAverageNumberOfSharesOutstandingBasic"),
}
US_GAAP_TAGS = {"net_income": "net_income", "operating_cash_flow": "operating_cash_flow", "assets": "assets", "equity": "equity",
                "diluted_shares": "share_count"}                                   # field names the core asks the data interface for
MIN_SHARE_COUNT = 1_000_000                      # a weighted share count below 1M is an XBRL scale error (e.g. MCD reported 751.8 "shares" = millions): rejected
QUARTER_DAYS = (80, 120)                         # a fiscal quarter: 12-17 weeks (COST / PEP close a 16-week fourth quarter)
TTM_SPAN_DAYS = (345, 385)                       # four quarters must span one fiscal year
FEASIBILITY_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "OperatingIncomeLoss", "GrossProfit", "Liabilities", "Revenues",
                    "RevenueFromContractWithCustomerExcludingAssessedTax")   # inspected for the feasibility matrix only; not used by v1.0
ALL_CONCEPTS = tuple(dict.fromkeys(c for cs in CONCEPTS.values() for c in cs))
FLAG_SURVIVORSHIP = "SURVIVORSHIP_BIAS_PRESENT"
DISCLAIMER = ("Research only. Value + Quality ranks a fixed universe of today's large caps (survivorship bias present); backtest results are "
              "hypothetical; PASS means eligible for human review, never a promotion and never a forecast.")
