"""Feature helpers for the PPE engine: the project's existing causal EMA / ATR / RSI (analysis.indicators), simple returns, robust
z-scores and the bounded mapping. Pure functions; every input is a prefix of history, so nothing here can look ahead."""
from __future__ import annotations

import math
import statistics
from typing import List, Optional, Sequence, Tuple

import pandas as pd

from analysis.indicators import atr as _atr
from analysis.indicators import ema as _ema
from analysis.indicators import rsi as _rsi
from ppe.inputs import DailyBar


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def bounded(x: float, full_scale: float) -> float:
    """Deterministic monotonic mapping to [-100, 100]: linear up to ±full_scale, saturated beyond."""
    return clip(x / full_scale, -1.0, 1.0) * 100.0


def ema_last(closes: Sequence[float], span: int) -> Optional[float]:
    if len(closes) < span:
        return None
    v = _ema(pd.Series(list(closes), dtype="float64"), span).iloc[-1]
    return float(v) if finite(float(v)) else None


def atr_last(bars: Sequence[DailyBar], period: int) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    df = pd.DataFrame({"high": [b.high for b in bars], "low": [b.low for b in bars], "close": [b.close for b in bars]}, dtype="float64")
    v = _atr(df, period).iloc[-1]
    return float(v) if finite(float(v)) else None


def rsi_last(closes: Sequence[float], period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    v = _rsi(pd.Series(list(closes), dtype="float64"), period).iloc[-1]
    return float(v) if finite(float(v)) else None


def simple_returns(closes: Sequence[float], k: int = 1) -> List[float]:
    return [closes[i] / closes[i - k] - 1.0 for i in range(k, len(closes)) if closes[i - k] > 0]


def robust_z(x: float, history: Sequence[float]) -> Tuple[Optional[float], str]:
    """(x - median) / (1.4826 * MAD); population std when MAD is 0; (None, ZERO_VARIANCE) when both are 0."""
    if len(history) < 2:
        return None, "INSUFFICIENT_HISTORY"
    med = statistics.median(history)
    mad = statistics.median([abs(h - med) for h in history])
    scale, method = 1.4826 * mad, "MAD"
    if scale <= 0:
        sd = statistics.pstdev(history)
        if sd <= 0:
            return None, "ZERO_VARIANCE"
        scale, method = sd, "STD"
    return (x - med) / scale, method
