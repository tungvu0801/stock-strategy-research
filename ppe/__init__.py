"""
ppe/ — Stage 5.6 PPE engine (Price Position & Excess): deterministic PPE Stretch (−100 … +100) and PPE Confidence (0 … 100).
Design: DESIGN_56_PPE_ENGINE.md. Calculations and rules only — no order, no recommendation, no model call, no research run.
"""
from typing import Optional

VERSION = "ppe_v1"
DISCLAIMER = "PPE Confidence measures confidence in the overreaction classification, not the probability that the stock will rise."
NOT_A_SIGNAL = ("PPE describes how stretched a move is and how well the stored evidence supports an overreaction reading. It is not a "
                "buy or sell signal, not a probability of recovery and not an expected return.")

# ---- Stretch components (weights are a research hypothesis, not an optimised result) ----------------------------------
WEIGHTS = {"price_dislocation": 0.30, "abnormal_return": 0.20, "relative_sector_move": 0.15, "volume_shock": 0.15,
           "gap_extension": 0.10, "short_term_exhaustion": 0.10}
COMPONENT_LABELS = {"price_dislocation": "Price dislocation", "abnormal_return": "Abnormal return", "relative_sector_move": "Relative sector move",
                    "volume_shock": "Volume shock", "gap_extension": "Gap / extension", "short_term_exhaustion": "Short-term exhaustion"}
MIN_COVERAGE_WEIGHT = 0.60

EMA_SPAN = 20
ATR_PERIOD = 20
RSI_PERIOD = 14
RETURN_LOOKBACK = 250            # trailing closes feeding the return distributions
MIN_RETURN_OBS = 60              # minimum observations for a robust z-score
VOLUME_LOOKBACK = 20
RANGE_LOOKBACK = 10
MIN_BARS_TREND = 25              # EMA20 / ATR20 warm-up
MIN_BARS_RSI = 15
FULL_SCALE = {"dislocation_atr": 4.0, "z": 4.0, "gap_atr": 2.5, "volume_ratio_span": 3.0, "streak_sessions": 6.0, "rsi_span": 30.0}
Z_CAP = 4.0

STRETCH_STATUSES = ("AVAILABLE", "INSUFFICIENT_DATA", "NOT_AVAILABLE")
CONFIDENCE_STATUSES = ("AVAILABLE", "EVIDENCE_NOT_AVAILABLE", "STRETCH_NOT_AVAILABLE", "NOT_APPLICABLE")
PPE_STATUSES = ("AVAILABLE", "SIMULATED_DATA", "INSUFFICIENT_DATA", "NOT_AVAILABLE")
BENCHMARK_KINDS = ("SECTOR_ETF", "PEER_BASKET", "MARKET", "NONE")
COMPONENT_STATUSES = ("AVAILABLE", "NOT_AVAILABLE")

# ---- Confidence constants (documented, not tuned on outcomes) -------------------------------------------------------
CONFIDENCE_BASE = 50
NORMAL_BAND = 40                 # |stretch| <= 40: no stretched move to classify
EXTREME_BAND = 75
FRESH_DAYS, STALE_DAYS, VERY_STALE_DAYS = 3, 10, 30
MIN_EVIDENCE_ITEMS = 5
CAPS = {"COMPANY_SPECIFIC_DOMINANT": 35, "GUIDANCE_CONFIRMS": 30, "COMPANY_CONFIRMS": 40, "WEAK": 50, "MODERATE": 84, "STALE": 60, "VERY_STALE": 45,
        "MISSING_COMPANY_EVIDENCE": 50, "CAUSE_NOT_AVAILABLE": 40}


def stretch_label(score: Optional[float]) -> Optional[str]:
    """Explicit, symmetric band boundaries: ±40 belong to NORMAL, ±75 to the inner stretched bands."""
    if score is None:
        return None
    if score < -EXTREME_BAND:
        return "EXTREME_DOWNSIDE"
    if score < -NORMAL_BAND:
        return "DOWNSIDE_STRETCHED"
    if score <= NORMAL_BAND:
        return "NORMAL"
    if score <= EXTREME_BAND:
        return "UPSIDE_STRETCHED"
    return "EXTREME_UPSIDE"


def confidence_label(score: Optional[int]) -> Optional[str]:
    if score is None:
        return None
    if score <= 24:
        return "VERY_LOW"
    if score <= 49:
        return "LOW"
    if score <= 69:
        return "MODERATE"
    if score <= 84:
        return "HIGH"
    return "VERY_HIGH"
