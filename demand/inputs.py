"""Engine inputs for Market Demand: plain data, no adapter or network. Daily bars reuse ppe.inputs.DailyBar."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional

from ppe.inputs import DailyBar


@dataclass(frozen=True)
class MinuteBar:
    time: int                                    # epoch seconds, minute aligned
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    final: bool = True


@dataclass
class BenchmarkInputs:
    symbol: str                                  # "SOXX", "PEERS:AMD,MU,…", "SPY"
    kind: str                                    # SECTOR_ETF | PEER_BASKET | MARKET
    label: str                                   # shown to the user, e.g. "SOXX (Semiconductors ETF)" or "SPY (market fallback)"
    daily: List[DailyBar] = field(default_factory=list)      # completed sessions (the engine re-filters by date)
    session_return: Optional[float] = None       # benchmark price / previous close − 1 for the current session, when a quote exists


@dataclass
class DemandInputs:
    symbol: str
    now: int                                     # the evaluation time T (epoch seconds); only bars closed by T are used
    session_date: date                           # the current US session date; daily bars dated on / after it are not completed
    session_bars: List[MinuteBar] = field(default_factory=list)              # current session, ascending (completed and live)
    prior_sessions: Dict[str, List[MinuteBar]] = field(default_factory=dict)  # session key -> bars, all strictly before the session
    daily_bars: List[DailyBar] = field(default_factory=list)
    current_price: Optional[float] = None
    prev_close: Optional[float] = None
    session_open: Optional[float] = None         # the session's official open when the source provides it
    benchmark: Optional[BenchmarkInputs] = None
    mode: str = "real"                           # real | simulated | replay
    market_data: str = ""
    price_source: str = ""
    session_complete: bool = False               # True when evaluated after the close (or for a completed session in replay)


def completed(bars: Iterable[MinuteBar], now: int) -> List[MinuteBar]:
    """Bars whose minute has closed by `now` (final and time + 60 <= now), ascending by time. The live bar is never included."""
    out = [b for b in bars if b.final and b.time + 60 <= now]
    out.sort(key=lambda b: b.time)
    return out


def from_candles(candles) -> List[MinuteBar]:
    """Trader View Candle models (or dicts) -> MinuteBar."""
    out: List[MinuteBar] = []
    for c in candles or []:
        g = c.get if isinstance(c, dict) else (lambda k, d=None, _c=c: getattr(_c, k, d))
        out.append(MinuteBar(time=int(g("time")), open=float(g("open")), high=float(g("high")), low=float(g("low")), close=float(g("close")),
                             volume=float(g("volume") or 0.0), final=bool(g("final", True))))
    return out
