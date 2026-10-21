"""
Adapter boundary for Trader View. The page talks to four small interfaces; integrating a new source later means implementing one of
them, not touching the UI.

  MarketDataAdapter        quotes, candles, symbol resolution, market state     Stage 5.5: simulator | EXISTING read-only Alpaca data
  PositionAdapter          read-only position per source                        Stage 5.5: Alpaca Paper View, Local Paper, Robinhood RO
  ResearchAdapter          latest STORED evidence                               Stage 5.5: Stage 5.4 packets, read only
  ExecutionPreviewAdapter  order preview                                        Stage 5.5: NULL — NOT_CONNECTED, no route reaches it
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Protocol

import config
from evidence import STRENGTH_DISCLAIMER
from trader_view import DATA_MODES, NOT_CONNECTED
from trader_view.models import Candle, EvidenceSummary, PositionState, Quote, Tradability


class UnknownSymbol(ValueError):
    """The feed answered and has no data for this symbol."""


class MarketUnavailable(RuntimeError):
    """The feed could not be reached; nothing is fabricated."""


class MarketDataAdapter(Protocol):
    mode: str                                   # "simulated" | "real"
    source: str                                 # human label shown in the UI

    def resolve(self, symbol: str) -> str: ...                              # normalised symbol; ValueError | UnknownSymbol
    def quote(self, symbol: str) -> Quote: ...
    def quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]: ...
    def candles(self, symbol: str, limit: int) -> List[Candle]: ...
    def current_candle(self, symbol: str) -> Optional[Candle]: ...
    def market_state(self) -> dict: ...
    def tradability(self, symbol: str) -> Tradability: ...
    def step(self, now_ts: int) -> None: ...                                # simulator advances; real data is polled in quotes()


class PositionAdapter(Protocol):
    def position(self, symbol: str) -> PositionState: ...
    def invalidate(self, symbol: Optional[str] = None) -> None: ...


class ResearchAdapter(Protocol):
    def latest(self, symbol: str) -> EvidenceSummary: ...


class ExecutionPreviewAdapter(Protocol):
    def preview(self, side: str, symbol: str) -> dict: ...


class NullPositionAdapter:
    def position(self, symbol: str) -> PositionState:
        return PositionState(symbol=symbol.upper(), has_position=False, sources=[], note="No position source connected.")

    def invalidate(self, symbol: Optional[str] = None) -> None:
        return None


class NullResearchAdapter:
    def latest(self, symbol: str) -> EvidenceSummary:
        return EvidenceSummary(symbol=symbol.upper(), status="NOT_LOADED", data_status="NOT_LOADED",
                               message="No research source connected.", disclaimer=STRENGTH_DISCLAIMER)


class NullExecutionPreviewAdapter:
    """The only execution-preview implementation in Stage 5.5. No route calls it; it exists so the contract has a home."""

    def preview(self, side: str, symbol: str) -> dict:
        return {"status": "NOT_CONNECTED", "side": str(side).upper(), "symbol": str(symbol).upper(), "message": NOT_CONNECTED}


def resolve_mode(mode: Optional[str] = None, *, credentials: Optional[bool] = None) -> str:
    """auto -> real when read-only market-data credentials are configured, else simulated. Unknown values fall back to auto."""
    m = (mode if mode is not None else config.TRADER_DATA_MODE or "auto").strip().lower()
    if m not in DATA_MODES:
        m = "auto"
    if m == "auto":
        has = config.has_credentials() if credentials is None else bool(credentials)
        return "real" if has else "simulated"
    return m


def build_market_adapter(mode: Optional[str] = None, *, credentials: Optional[bool] = None):
    m = resolve_mode(mode, credentials=credentials)
    if m == "real":
        from trader_view.market_real import RealMarketData
        return RealMarketData()
    from trader_view.simulated import SimulatedMarket
    return SimulatedMarket(seed=config.TRADER_SIM_SEED, backfill_minutes=config.TRADER_HISTORY_MINUTES)
