"""
momentum/ — Stage 6.5 STRATEGY #3: MOMENTUM v1 (DESIGN_65_MOMENTUM.md). Research only; long only; deterministic; no model.

Hypothesis: stocks with stronger medium-term relative price performance (12-1 momentum) tend to continue to outperform weaker peers over the
following holding period. Momentum ONLY: no value, quality, news, model or market-condition filter.

Every rule below was FIXED before any performance figure was seen (the freeze commit). Changing any of them is a new STRATEGY_VERSION and a new
untouched evaluation plan - never a tweak after the OOS result.
"""
STRATEGY_ID = "MOMENTUM"
STRATEGY_VERSION = "1.0"
CORE_VERSION = "mom_core_v1"

# ---- universe: the same fixed research universe as Value + Quality (point-in-time constituents are not available -> SURVIVORSHIP_BIAS_PRESENT) --
RESEARCH_UNIVERSE = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BAC", "BKNG", "BLK", "BRK.B", "C", "CAT",
                     "CLS", "COST", "CRM", "CSCO", "CVX", "DHR", "DIS", "GE", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTU", "ISRG", "JNJ", "JPM", "KLAC",
                     "KO", "LIN", "LLY", "LOW", "LRCX", "MA", "MCD", "META", "MRK", "MRVL", "MS", "MSFT", "MU", "NEE", "NFLX", "NOW", "NVDA", "ORCL", "PANW",
                     "PEP", "PFE", "PG", "PLD", "PM", "QCOM", "REGN", "RTX", "SBUX", "SCHW", "SNDK", "SPGI", "SYK", "T", "TMO", "TSLA", "TXN", "UBER", "UNH",
                     "UNP", "V", "VRTX", "VZ", "WMT", "XOM")     # the 81 stocks of value_quality.RESEARCH_UNIVERSE (tested equal), today's large caps
BENCHMARK = "SPY"                                # the MARKET BENCHMARK (never a selection input, never the selection control)
MIN_PRICE = 5.0                                  # raw close at the rebalance
MIN_MEDIAN_DOLLAR_VOLUME = 1_000_000.0           # USD, 20-session median of raw close x volume (IEX-only volume: conservative)
DOLLAR_VOLUME_SESSIONS = 20

# ---- the signal: 12-1 momentum on all-adjusted closes (splits + dividends: total return) ------------------------------------------------------
LOOKBACK_SESSIONS = 252                          # about 12 months: the start endpoint is the close of session i - 252
SKIP_SESSIONS = 21                               # about 1 month: the end endpoint is the close of session i - 21 (the recent month is skipped)

# ---- portfolio ---------------------------------------------------------------------------------------------------------------------------------
TOP_N = 20                                       # long-only, equal weight, no leverage, no short book, no stop
POSITION_PCT = 100.0 / TOP_N                     # 5 % of the fixed hypothetical equity per position (the harness sizing)
REBALANCE = "MONTHLY"                            # signal at the close of the FIRST session of each month (from session index LOOKBACK_SESSIONS on);
                                                 # entry at the next session's open; a leg ends at the open of the first session after the next rebalance
FRICTION_BPS_PER_SIDE = 5.0                      # the harness default friction model on real orders (ENTER / EXIT), no commission

# ---- the explicit, pre-declared sensitivity neighbourhood (TRAIN view only; diagnostic; never chosen) ----------------------------------------------
SENSITIVITY = ({"lookback_sessions": 189}, {"lookback_sessions": 315}, {"top_n": 15}, {"top_n": 25})

FLAG_SURVIVORSHIP = "SURVIVORSHIP_BIAS_PRESENT"
DISCLAIMER = ("Research only. Momentum ranks a fixed universe of today's large caps (survivorship bias present); backtest results are hypothetical; "
              "PASS means eligible for human review, never a promotion and never a forecast.")
