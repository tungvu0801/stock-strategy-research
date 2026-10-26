"""
breakout/ — Stage 6.7 STRATEGY #5: BREAKOUT v1 (DESIGN_67_BREAKOUT.md). Research only; long only; deterministic; no model.

Hypothesis: a stock that closes above its prior multi-week trading range may be experiencing persistent demand and can continue to outperform
its peers over the following weeks. Breakout ONLY: no value, quality, momentum composite, mean reversion, news, earnings, sentiment, regime
timing or discretionary confirmation.

Every rule below was FIXED before any performance figure was seen (the freeze commit). Changing any of them is a new STRATEGY_VERSION and a new
untouched evaluation plan - never a tweak after the OOS result.
"""
STRATEGY_ID = "BREAKOUT"
STRATEGY_VERSION = "1.0"
CORE_VERSION = "bo_core_v1"

# ---- universe: the same fixed research universe as the earlier strategies (FIXED_CURRENT_UNIVERSE -> SURVIVORSHIP_BIAS_PRESENT) -----------------
RESEARCH_UNIVERSE = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BAC", "BKNG", "BLK", "BRK.B", "C", "CAT",
                     "CLS", "COST", "CRM", "CSCO", "CVX", "DHR", "DIS", "GE", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTU", "ISRG", "JNJ", "JPM", "KLAC",
                     "KO", "LIN", "LLY", "LOW", "LRCX", "MA", "MCD", "META", "MRK", "MRVL", "MS", "MSFT", "MU", "NEE", "NFLX", "NOW", "NVDA", "ORCL", "PANW",
                     "PEP", "PFE", "PG", "PLD", "PM", "QCOM", "REGN", "RTX", "SBUX", "SCHW", "SNDK", "SPGI", "SYK", "T", "TMO", "TSLA", "TXN", "UBER", "UNH",
                     "UNP", "V", "VRTX", "VZ", "WMT", "XOM")     # the 81 stocks of value_quality.RESEARCH_UNIVERSE (tested equal), today's large caps
BENCHMARK = "SPY"                                # the MARKET BENCHMARK (never a selection input, never the selection control)
MIN_PRICE = 5.0                                  # raw close at the rebalance
MIN_MEDIAN_DOLLAR_VOLUME = 1_000_000.0           # USD, 20-session median of raw close x volume (IEX-only volume: conservative)
DOLLAR_VOLUME_SESSIONS = 20

# ---- the breakout: T's adjusted close above the highest adjusted HIGH of the 20 completed sessions BEFORE T (T never in its own reference) -------
LOOKBACK_SESSIONS = 20

# ---- breakout-window data integrity: the Stage 6.6 corporate-action contract + a Breakout-specific large re-basing rule (never a return cap) ------
SPLIT_SESSION = 0.01                             # a move of the raw / split-adjusted ratio above 1 % is a corporate-action event to classify
NORMAL_DAY = 0.25                                # a day move beyond 25 % is a jump (a split implies >= ~33 %)
SPLIT_DAY_TOL = 0.02                             # a REAL split must be absorbed by the adjusted series: adjusted vs split-adjusted day return within 2 %
ADJUSTED_LOSS_TOL = 0.01                         # no ratio move: the adjusted day return may not be > 1 % BELOW the raw one
LARGE_REBASING_TOL = 0.05                        # no ratio move: an adjusted day return > 5 % ABOVE the raw one is a large distribution (a spin-off)
                                                 # re-basing the history: a level reference straddling it cannot be verified -> excluded

# ---- portfolio: at most 20 breakouts, a FIXED 5 % each, the unused slots cash (never renormalized) -------------------------------------------------
MAX_HOLDINGS = 20
SLOT_PCT = 100.0 / MAX_HOLDINGS                  # 5 % of the fixed hypothetical equity per selected stock
CASH_RETURN = 0.0                                # an unused slot earns nothing
REBALANCE = "WEEKLY"                             # signal at the close of the FIRST session of each ISO week (from session index LOOKBACK_SESSIONS on);
                                                 # entry at the next session's open; a leg ends at the open of the first session after the next rebalance
FRICTION_BPS_PER_SIDE = 5.0                      # the harness default friction model on real orders (ENTER / EXIT), no commission

# ---- the explicit, pre-declared sensitivity neighbourhood (TRAIN view only; diagnostic; never chosen) ----------------------------------------------
SENSITIVITY = ({"lookback_sessions": 10}, {"lookback_sessions": 40}, {"max_holdings": 15}, {"max_holdings": 25})

# ---- pre-declared diagnostic definitions (never a gate, never a rule) ------------------------------------------------------------------------------
FOLLOW_THROUGH_BAND = 0.01                       # a selected leg's gross return: > +1 % continued higher, within +-1 % flat, < -1 % failed
POSITION_BUCKETS = ((0, 0), (1, 5), (6, 10), (11, 19), (20, 20))
CONCENTRATION_TOP_NAMES = 5                      # EXCESS_CONCENTRATED_IN_FEW_NAMES: the 5 largest name contributions carry >= half of a positive
CONCENTRATION_NAME_SHARE = 0.5                   # summed period excess
FLAG_SURVIVORSHIP = "SURVIVORSHIP_BIAS_PRESENT"
FLAG_NAMES = "EXCESS_CONCENTRATED_IN_FEW_NAMES"
FLAG_FRICTION = "FRICTION_CONSUMES_GROSS_EXCESS"
FLAG_HOLDING_ANOMALY = "HOLDING_WINDOW_DATA_ANOMALY"
DISCLAIMER = ("Research only. Breakout ranks a fixed universe of today's large caps (survivorship bias present) and holds cash when few stocks "
              "break out; backtest results are hypothetical; PASS means eligible for human review, never a promotion and never a forecast.")
