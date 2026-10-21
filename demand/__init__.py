"""
demand/ — Stage 5.7 MARKET DEMAND / BUYER CONFIRMATION: a deterministic 0 … 100 score of observed buyer participation and price
acceptance, computed from bars only (DESIGN_57_MARKET_DEMAND.md). It sits next to PPE Stretch / PPE Confidence and is never combined
with them. Calculations and rules only — no order, no recommendation, no model call, no research run, no change to PPE.
"""
from typing import Optional

VERSION = "demand_v1"
DISCLAIMER = "Market Demand measures observed buyer participation and confirmation, not the probability that the stock will rise."
NOT_A_SIGNAL = ("Market Demand describes observed buyer participation and price acceptance in the bars. It is a research measurement, "
                "not a trade signal and not a forecast.")
SIMULATED_NOTE = "SIMULATED DATA — synthetic bars from the development simulator. This Market Demand result demonstrates the display path and is not a market reading."

WEIGHTS = {"relative_volume": 0.20, "vwap_acceptance": 0.20, "intraday_structure": 0.20, "range_strength": 0.15,
           "relative_strength": 0.15, "follow_through": 0.10}
COMPONENT_LABELS = {"relative_volume": "Relative volume / participation", "vwap_acceptance": "VWAP / price acceptance",
                    "intraday_structure": "Intraday structure", "range_strength": "Range strength",
                    "relative_strength": "Relative strength vs sector", "follow_through": "Follow-through / persistence"}
MIN_COVERAGE_WEIGHT = 0.60
NEUTRAL = 50.0

MIN_SESSION_BARS = 30            # completed bars of the current session for INTRADAY_MODE (and for B / C)
MIN_FOLLOW_BARS = 20             # completed bars for intraday follow-through
MIN_PRIOR_SESSIONS = 2           # prior sessions behind the expected intraday volume curve
VOLUME_LOOKBACK_SESSIONS = 20    # daily relative volume: median of the prior 20 completed sessions
MIN_DAILY_BARS = 21
MIN_DAILY_FOLLOW = 6
STRUCTURE_WINDOW = 10            # three consecutive windows of 10 completed bars
FOLLOW_WINDOW = 10
STREAK_CAP_INTRADAY = 10
STREAK_CAP_DAILY = 5
RECLAIM_WINDOW = 60              # completed bars searched for the latest VWAP cross
RECLAIM_HOLD_BARS = 3            # closes that must stay above VWAP for a reclaim to count as held
SCALES = {"participation_span": 1.0, "vwap_dist": 0.5, "rel_session_pct": 1.5, "rel_1d_pct": 2.0, "rel_5d_pct": 5.0}
DOWN_MOVE_RANGE_CAP = 60.0       # range strength is capped here when the session / day moved down
KEY_FACTOR_MIN_DEVIATION = 5.0
MAX_KEY_FACTORS = 4

MODES = ("INTRADAY_MODE", "DAILY_MODE", "NONE")
STATUSES = ("AVAILABLE", "SIMULATED_DATA", "INSUFFICIENT_DATA", "NOT_AVAILABLE")
COMPONENT_STATUSES = ("AVAILABLE", "NOT_AVAILABLE")
BANDS = ((0, 24, "VERY_WEAK"), (25, 39, "WEAK"), (40, 59, "MIXED"), (60, 74, "STRENGTHENING"), (75, 89, "STRONG"), (90, 100, "VERY_STRONG"))


def demand_label(score: Optional[float]) -> Optional[str]:
    """Band of a rounded 0 … 100 score; None for no score."""
    if score is None:
        return None
    s = int(round(float(score)))
    s = 0 if s < 0 else 100 if s > 100 else s
    for lo, hi, name in BANDS:
        if lo <= s <= hi:
            return name
    return None
