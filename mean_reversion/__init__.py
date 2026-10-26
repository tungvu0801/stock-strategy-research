"""
mean_reversion/ — Stage 6.6 STRATEGY #4: MEAN REVERSION v1 (DESIGN_66_MEAN_REVERSION.md). Research only; long only; deterministic; no model.

Hypothesis: short-term relative underperformance may partially reverse over the following short holding period (cross-sectional short-term
reversal). Reversal ONLY: no value, quality, momentum, market-timing, news, sentiment, earnings or model filter, no discretionary gate.

Every rule below was FIXED before any performance figure was seen (the freeze commit). Changing any of them is a new STRATEGY_VERSION and a new
untouched evaluation plan - never a tweak after the OOS result.
"""
STRATEGY_ID = "MEAN_REVERSION"
STRATEGY_VERSION = "1.0"
CORE_VERSION = "mr_core_v1"

# ---- universe: the same fixed research universe as Value + Quality and Momentum (FIXED_CURRENT_UNIVERSE -> SURVIVORSHIP_BIAS_PRESENT) ------------
RESEARCH_UNIVERSE = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BAC", "BKNG", "BLK", "BRK.B", "C", "CAT",
                     "CLS", "COST", "CRM", "CSCO", "CVX", "DHR", "DIS", "GE", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTU", "ISRG", "JNJ", "JPM", "KLAC",
                     "KO", "LIN", "LLY", "LOW", "LRCX", "MA", "MCD", "META", "MRK", "MRVL", "MS", "MSFT", "MU", "NEE", "NFLX", "NOW", "NVDA", "ORCL", "PANW",
                     "PEP", "PFE", "PG", "PLD", "PM", "QCOM", "REGN", "RTX", "SBUX", "SCHW", "SNDK", "SPGI", "SYK", "T", "TMO", "TSLA", "TXN", "UBER", "UNH",
                     "UNP", "V", "VRTX", "VZ", "WMT", "XOM")     # the 81 stocks of value_quality.RESEARCH_UNIVERSE (tested equal), today's large caps
BENCHMARK = "SPY"                                # the MARKET BENCHMARK (never a selection input, never the selection control)
MIN_PRICE = 5.0                                  # raw close at the rebalance
MIN_MEDIAN_DOLLAR_VOLUME = 1_000_000.0           # USD, 20-session median of raw close x volume (IEX-only volume: conservative)
DOLLAR_VOLUME_SESSIONS = 20

# ---- the signal: the 5-session adjusted return; the LOWEST return ranks FIRST ---------------------------------------------------------------------
LOOKBACK_SESSIONS = 5                            # adjusted_close[T] / adjusted_close[i - 5] - 1, data through T's close only

# ---- signal-window data integrity: the existing corporate-action contract (the Stage 6.5 split audit's classification), applied to the window ----
SPLIT_SESSION = 0.01                             # a move of the raw / split-adjusted ratio above 1 % is a corporate-action event to classify
NORMAL_DAY = 0.25                                # a day move beyond 25 % is a jump (a split implies >= ~33 %)
SPLIT_DAY_TOL = 0.02                             # a REAL split must be absorbed by the adjusted series: adjusted vs split-adjusted day return within 2 %
ADJUSTED_LOSS_TOL = 0.01                         # no ratio move: the adjusted day return may not be > 1 % BELOW the raw one (a distribution only raises it)

# ---- portfolio ---------------------------------------------------------------------------------------------------------------------------------
TOP_N = 20                                       # the 20 MOST NEGATIVE recent returns; long-only, equal weight, no leverage, no short book, no stop
POSITION_PCT = 100.0 / TOP_N                     # 5 % of the fixed hypothetical equity per position (the harness sizing)
REBALANCE = "WEEKLY"                             # signal at the close of the FIRST session of each ISO week (from session index DOLLAR_VOLUME_SESSIONS
                                                 # on); entry at the next session's open; a leg ends at the open of the first session after the next rebalance
FRICTION_BPS_PER_SIDE = 5.0                      # the harness default friction model on real orders (ENTER / EXIT), no commission

# ---- the explicit, pre-declared sensitivity neighbourhood (TRAIN view only; diagnostic; never chosen) ----------------------------------------------
SENSITIVITY = ({"lookback_sessions": 3}, {"lookback_sessions": 10}, {"top_n": 15}, {"top_n": 25})

# ---- disclosure thresholds (research flags in the diagnostics: never a gate, never a rule) -------------------------------------------------------
CONCENTRATION_TOP_NAMES = 5                      # EXCESS_CONCENTRATED_IN_FEW_NAMES: the 5 largest name contributions carry >= half of a positive
CONCENTRATION_NAME_SHARE = 0.5                   # summed period excess
FLAG_SURVIVORSHIP = "SURVIVORSHIP_BIAS_PRESENT"
FLAG_NAMES = "EXCESS_CONCENTRATED_IN_FEW_NAMES"
FLAG_FRICTION = "FRICTION_CONSUMES_GROSS_EXCESS"
DISCLAIMER = ("Research only. Mean Reversion ranks a fixed universe of today's large caps (survivorship bias present); backtest results are "
              "hypothetical; PASS means eligible for human review, never a promotion and never a forecast.")
