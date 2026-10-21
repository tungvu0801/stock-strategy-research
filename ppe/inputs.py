"""Plain input types for the PPE engine (data-source agnostic). Bars are completed daily sessions, ascending by date."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


@dataclass
class Benchmark:
    symbol: str                                   # "SOXX", "SPY" or "PEERS:NVDA,AMD,…"
    kind: str                                     # SECTOR_ETF | PEER_BASKET | MARKET
    bars: List[DailyBar]
    current_price: Optional[float] = None         # live mode only: the benchmark's latest price
    label: str = ""


@dataclass
class MarketInputs:
    symbol: str
    bars: List[DailyBar]
    as_of: date                                   # the session being evaluated (replay: its bar is the last one <= as_of)
    current_price: Optional[float] = None         # live mode: the latest price; None = evaluate the close of the as_of bar
    current_open: Optional[float] = None          # live mode: today's open when known
    price_time: Optional[str] = None              # ISO time of current_price
    benchmark: Optional[Benchmark] = None
    mode: str = "real"                            # real | simulated | replay
    market_data: str = ""                         # human label of the bar source
    price_source: str = ""                        # human label of the price source
    extra: dict = field(default_factory=dict)


def point_in_time(bars: List[DailyBar], as_of: date) -> List[DailyBar]:
    """Only sessions dated at or before as_of, ascending — the engine's own lookahead guard."""
    return sorted((b for b in bars if b.date <= as_of), key=lambda b: b.date)
