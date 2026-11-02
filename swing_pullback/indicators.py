"""swing_pullback/indicators.py — the point-in-time indicators of the swing baseline, in plain Python (no pandas). Every function maps a series to a
series of the same length whose value at k reads inputs[0 .. k] only (causal by construction); None = not enough history.

  sma(values, n)               the simple mean of the n values ending at k (a prefix-sum difference)
  wilder_rsi(closes, n)        the analysis/indicators.py formula: deltas, gains / losses smoothed by the pandas ewm(alpha = 1 / n, adjust = False,
                               min_periods = n) recursion seeded with the first delta; 100 where the average loss is 0
  wilder_atr(h, l, c, n)       the analysis/indicators.py formula: true range (high - low on the first bar), the same recursion

They equal analysis/indicators.py wherever that module defines a value on a series of at least n + 1 bars (tested). That module also returns no
value at all for a series shorter than n + 1, so its ATR at index n - 1 depends on whether a later bar exists; here it never does."""
from __future__ import annotations

from typing import List, Optional, Sequence


def sma(values: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + float(v))
    for end in range(n, len(values) + 1):                                         # prefix[end] sums values[0 .. end - 1]
        out[end - 1] = (prefix[end] - prefix[end - n]) / n
    return out


def _ewm(xs: Sequence[Optional[float]], alpha: float, min_periods: int) -> List[Optional[float]]:
    """pandas ewm(alpha, adjust=False, min_periods).mean() for a series whose only missing values lead it."""
    out: List[Optional[float]] = [None] * len(xs)
    y: Optional[float] = None
    count = 0
    for k, x in enumerate(xs):
        if x is None:
            if y is not None:
                raise ValueError("a missing value after the first observation")      # never happens: deltas / ranges have one leading gap at most
            continue
        y = float(x) if y is None else (1.0 - alpha) * y + alpha * float(x)
        count += 1
        if count >= min_periods:
            out[k] = y
    return out


def wilder_rsi(closes: Sequence[float], n: int) -> List[Optional[float]]:
    deltas: List[Optional[float]] = [None] + [float(closes[k]) - float(closes[k - 1]) for k in range(1, len(closes))]
    gains = [None if d is None else max(d, 0.0) for d in deltas]
    losses = [None if d is None else -min(d, 0.0) for d in deltas]
    ag, al = _ewm(gains, 1.0 / n, n), _ewm(losses, 1.0 / n, n)
    out: List[Optional[float]] = [None] * len(closes)
    for k in range(len(closes)):
        if ag[k] is None or al[k] is None:
            continue
        out[k] = 100.0 if al[k] == 0 else 100.0 - 100.0 / (1.0 + ag[k] / al[k])
    return out


def wilder_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int) -> List[Optional[float]]:
    tr: List[Optional[float]] = []
    for k in range(len(closes)):
        h, lo = float(highs[k]), float(lows[k])
        if k == 0:
            tr.append(h - lo)
        else:
            pc = float(closes[k - 1])
            tr.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return _ewm(tr, 1.0 / n, n)
