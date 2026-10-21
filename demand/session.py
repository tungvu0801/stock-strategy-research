"""Session-level helpers for Market Demand: cumulative (point-in-time) VWAP, session range, the expected intraday volume curve from
PRIOR sessions only, window extremes and streaks. Pure functions over completed bars."""
from __future__ import annotations

import math
import statistics
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from demand import MIN_PRIOR_SESSIONS
from demand.inputs import MinuteBar


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))


def median(xs: Sequence[float]) -> Optional[float]:
    v = [float(x) for x in xs if finite(x)]
    return statistics.median(v) if v else None


def vwap_series(bars: Sequence[MinuteBar]) -> List[Optional[float]]:
    """vwap_i over bars[0..i] with typical price (h + l + c) / 3; None while the cumulative volume is zero."""
    out: List[Optional[float]] = []
    pv = vol = 0.0
    for b in bars:
        pv += (b.high + b.low + b.close) / 3.0 * b.volume
        vol += b.volume
        out.append(pv / vol if vol > 0 else None)
    return out


def session_range(bars: Sequence[MinuteBar], price: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    lows = [b.low for b in bars] + ([price] if finite(price) else [])
    highs = [b.high for b in bars] + ([price] if finite(price) else [])
    return (min(lows), max(highs)) if lows else (None, None)


def expected_cumulative_volume(prior: Dict[str, List[MinuteBar]], m: int) -> Tuple[Optional[float], int]:
    """Median over prior sessions of the cumulative volume of their first m bars (sessions shorter than m bars are skipped).
    Needs MIN_PRIOR_SESSIONS valid sessions. Prior sessions only: never the current one."""
    if m <= 0:
        return None, 0
    sums = []
    for bars in prior.values():
        rows = sorted(bars, key=lambda b: b.time)
        if len(rows) >= m:
            sums.append(sum(b.volume for b in rows[:m]))
    if len(sums) < MIN_PRIOR_SESSIONS:
        return None, len(sums)
    return median(sums), len(sums)


def window_extremes(bars: Sequence[MinuteBar]) -> Tuple[float, float]:
    return min(b.low for b in bars), max(b.high for b in bars)


def streak(flags_newest_first: Iterable[bool], cap: int) -> int:
    n = 0
    for f in flags_newest_first:
        if not f:
            break
        n += 1
        if n >= cap:
            break
    return n


def monotone(values: Sequence[float]) -> int:
    """+1 strictly rising, -1 strictly falling, 0 otherwise."""
    if len(values) < 2:
        return 0
    if all(values[i] < values[i + 1] for i in range(len(values) - 1)):
        return 1
    if all(values[i] > values[i + 1] for i in range(len(values) - 1)):
        return -1
    return 0
