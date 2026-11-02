"""
swing_pullback/ — Stage 7.8 Part B: the SWING TREND PULLBACK research baseline (DESIGN_78_SWING_PULLBACK.md). Research only; long only;
deterministic; daily bars; offline; no model. It emits validation-harness candidates, never a Strategy Standard signal for a live path.

V1 (SWING_TREND_PULLBACK 1.0) is a MODIFIED D.3 RESEARCH BASELINE, not a faithful implementation of the complete master-design D.3 strategy
(SWING_TREND_PULLBACK_V1): D.3's close-based exit and its earnings filter are not modelled, and three more deviations are declared (DEVIATIONS).
C1 (SWING_TREND_PULLBACK_CONTROL 1.0) is V1 without the pullback rule (the RSI(2) / lower-close trigger AND the 0.97 x SMA50 depth guard).

Every value below is the operator's Stage 7.8 Part A sign-off (STAGE_7_8_OPERATOR_DECISION_PACKAGE.md, M1-M9, and the four clarifications of
2026-11-01): C1 drops trigger and guard; the cooldown stays 9 sessions in every sensitivity neighbour; the price and liquidity rules read the
raw series, one listing per issuer; the folds are 6 equal session counts from the dataset's 200th session. Nothing was chosen from a result.
Changing any value is a new STRATEGY_VERSION and a new, untouched evaluation plan - never a tweak after an OOS look.
"""
import hashlib
import json

STRATEGY_ID = "SWING_TREND_PULLBACK"
STRATEGY_VERSION = "1.0"
CONTROL_ID = "SWING_TREND_PULLBACK_CONTROL"
CONTROL_VERSION = "1.0"
CORE_VERSION = "swing_pullback_core_v1"

# ---- universe: the fixed research universe of Stages 6.4-6.7 (FIXED_CURRENT_UNIVERSE -> SURVIVORSHIP_BIAS_PRESENT), one listing per issuer ------
RESEARCH_UNIVERSE = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BAC", "BKNG", "BLK", "BRK.B", "C", "CAT",
                     "CLS", "COST", "CRM", "CSCO", "CVX", "DHR", "DIS", "GE", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTU", "ISRG", "JNJ", "JPM", "KLAC",
                     "KO", "LIN", "LLY", "LOW", "LRCX", "MA", "MCD", "META", "MRK", "MRVL", "MS", "MSFT", "MU", "NEE", "NFLX", "NOW", "NVDA", "ORCL", "PANW",
                     "PEP", "PFE", "PG", "PLD", "PM", "QCOM", "REGN", "RTX", "SBUX", "SCHW", "SNDK", "SPGI", "SYK", "T", "TMO", "TSLA", "TXN", "UBER", "UNH",
                     "UNP", "V", "VRTX", "VZ", "WMT", "XOM")     # the 81 stocks of value_quality.RESEARCH_UNIVERSE (tested equal), today's large caps
REGIME_SYMBOL = "SPY"                            # read for the regime only, never a candidate

# ---- eligibility (raw series: aux raw_close / raw_volume, the 6.6 / 6.7 convention) -------------------------------------------------------------
MIN_PRICE = 5.0                                  # raw close at the signal session
MIN_DOLLAR_VOLUME = 20_000_000.0                 # USD: median of raw close x raw volume over the 20 sessions ending at the signal session
DOLLAR_VOLUME_SESSIONS = 20
VOLUME_FEEDS = ("SIP", "IEX")                    # the dataset DECLARES its volume feed (provenance "volume_feed"); IEX -> the liquidity rule is
                                                 # NOT_APPLIED (one venue's volume is not the $20 M quantity); anything else refuses the run

# ---- the trend, the regime and the pullback (adjusted closes) ----------------------------------------------------------------------------------
SLOW_SMA = 200                                   # close > SMA200 (stock); SPY close > SMA200(SPY)
FAST_SMA = 50                                    # SMA50 at the signal session > SMA50 SLOPE_LOOKBACK sessions earlier
SLOPE_LOOKBACK = 20
RSI_PERIOD = 2                                   # Wilder RSI (the analysis/indicators.py formula), <= RSI_MAX inclusive
RSI_MAX = 10.0
LOWER_CLOSES = 3                                 # three successive declines: close[t] < close[t-1] < close[t-2] < close[t-3]
DEPTH_GUARD = 0.97                               # close >= 0.97 x SMA50; with the trigger, the PULLBACK RULE (C1 omits both)
WARMUP_SESSIONS = 200                            # no candidate until the 200 dataset sessions ending at t are all present (symbol and SPY)

# ---- the trade (fixed at the signal close; the harness exits only) --------------------------------------------------------------------------------
ATR_PERIOD = 14                                  # Wilder ATR (the analysis/indicators.py formula)
STOP_ATR = 2.0                                   # stop = signal close - 2 x ATR14
TARGET_ATR = 3.0                                 # target = signal close + 3 x ATR14
MAX_HOLD_BARS = 10                               # time exit: the close of the 10th session in the trade (the entry session is the first); never
                                                 # set per candidate
COOLDOWN_SESSIONS = 9                            # after a candidate at session t', none for the symbol at t'+1 ... t'+9 (dataset sessions; signal dates
                                                 # only); 9 in every sensitivity neighbour (operator, 2026-11-01)
SCORE = 50.0                                     # the Strategy Standard requires a 0-100 score; V1 does not rank, so every candidate carries the same
                                                 # placeholder (never read by the RULES arm)

# ---- the pre-registered experiments ------------------------------------------------------------------------------------------------------------
COSTS = {"BASE": 5.0, "STRESS_2X": 10.0}         # slippage bps per side; commission 0 (modelled explicitly), regulatory fees NOT_MODELLED: exactly
                                                 # two cost runs per version, never a third
SENSITIVITY = ({"rsi_max": 7.5}, {"rsi_max": 12.5}, {"stop_atr": 1.5}, {"stop_atr": 2.5}, {"target_atr": 2.25}, {"target_atr": 3.75},
               {"max_hold_bars": 8}, {"max_hold_bars": 12})   # V1 only; one at a time, +-25 %; TRAIN view only; diagnostic, never chosen
FOLDS = 6                                        # consecutive, equal session counts from the dataset's 200th session to its last; the remainder
FOLD_START_INDEX = WARMUP_SESSIONS - 1           # adds one session to each of the earliest folds; trades by signal date; descriptive only

# ---- declared deviations from D.3 and disclosures (research flags, never a gate) ---------------------------------------------------------------
DEVIATIONS = {
    "D1": "no close-based exit: D.3's 'a close above the prior 5-session high' cannot be expressed by the harness (stop / target / time / data "
          "end only); V2 (V1 + that exit) is deferred, not implemented, and its fill timing is UNRESOLVED",
    "D2": "no earnings filter: there is no point-in-time earnings history (EARNINGS_FILTER_NOT_APPLIED); earnings-window trades are included",
    "D3": "the stop and the target are measured from the signal close, not from the fill (the harness fixes both at the signal)",
    "D4": "the liquidity rule is NOT_APPLIED when the dataset's volume is IEX (one venue's volume)",
    "D5": "the portfolio limits (<= 4 open, <= 2 new a day, <= 2 a sector) are not simulated: overlapping trades of different symbols are allowed",
}
FLAG_MODIFIED = "MODIFIED_D3_BASELINE"
FLAG_SURVIVORSHIP = "SURVIVORSHIP_BIAS_PRESENT"
FLAG_NO_CLOSE_EXIT = "CLOSE_BASED_EXIT_NOT_MODELLED"
FLAG_EARNINGS = "EARNINGS_FILTER_NOT_APPLIED"
FLAG_LEVELS = "STOP_TARGET_FROM_SIGNAL_CLOSE"
FLAG_IEX = "LIQUIDITY_RULE_NOT_APPLIED_IEX_VOLUME"
FLAG_PORTFOLIO = "PORTFOLIO_LIMITS_NOT_SIMULATED"
FLAG_FEES = "REGULATORY_FEES_NOT_MODELLED"
FLAG_CONTROL = "CONTROL_ARM_NOT_A_STRATEGY"
DEFERRED = {"V2": "V1 + D.3's close-based exit: needs a separately approved harness exit stage; fill timing UNRESOLVED; not pre-registered here",
            "EMA_SUPPORT": "the EMA / support design: a separate future strategy"}
DISCLAIMER = ("Research only. A modified D.3 baseline on a fixed universe of today's large caps (survivorship bias present); backtest results are "
              "hypothetical; PASS means eligible for human review, never a promotion and never a forecast.")


def preregistration() -> dict:
    """Every rule value of V1 and C1, the experiments and the reports: what the PREREGISTRATION_HASH covers."""
    return {"core": CORE_VERSION, "v1": [STRATEGY_ID, STRATEGY_VERSION], "c1": [CONTROL_ID, CONTROL_VERSION], "universe": list(RESEARCH_UNIVERSE),
            "regime_symbol": REGIME_SYMBOL, "min_price": MIN_PRICE, "min_dollar_volume": MIN_DOLLAR_VOLUME, "dollar_volume_sessions": DOLLAR_VOLUME_SESSIONS,
            "volume_feeds": list(VOLUME_FEEDS), "slow_sma": SLOW_SMA, "fast_sma": FAST_SMA, "slope_lookback": SLOPE_LOOKBACK, "rsi_period": RSI_PERIOD,
            "rsi_max": RSI_MAX, "lower_closes": LOWER_CLOSES, "depth_guard": DEPTH_GUARD, "warmup_sessions": WARMUP_SESSIONS, "atr_period": ATR_PERIOD,
            "stop_atr": STOP_ATR, "target_atr": TARGET_ATR, "max_hold_bars": MAX_HOLD_BARS, "cooldown_sessions": COOLDOWN_SESSIONS, "score": SCORE,
            "costs": dict(COSTS), "sensitivity": [dict(p) for p in SENSITIVITY], "folds": FOLDS, "fold_start_index": FOLD_START_INDEX,
            "deviations": dict(DEVIATIONS), "deferred": dict(DEFERRED)}


def preregistration_hash() -> str:
    return hashlib.sha256(json.dumps(preregistration(), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
