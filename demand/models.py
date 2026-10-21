"""Market Demand output contract (pydantic). No buy / sell, recommendation, probability, expected-return, target or signal field exists here (tested)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from demand import DISCLAIMER, VERSION


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemandComponent(_M):
    key: str
    label: str
    weight: float
    status: str                                   # AVAILABLE | NOT_AVAILABLE
    score: Optional[float] = None                 # [0, 100], 50 = neutral
    raw: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    code: Optional[str] = None                    # why NOT_AVAILABLE: INTRADAY_ONLY | INSUFFICIENT_BARS | NO_VOLUME_PROFILE | ZERO_VOLUME | NO_BENCHMARK | …


class SessionInfo(_M):
    date: Optional[str] = None
    complete: bool = False                        # False while the session is still open (intraday values are provisional)
    bars_completed: int = 0
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    vwap: Optional[float] = None
    prior_sessions: int = 0                       # prior sessions behind the expected volume curve


class DemandSources(_M):
    mode: str                                     # real | simulated | replay
    market_data: str
    price_source: str = ""
    benchmark: Optional[str] = None
    benchmark_kind: str = "NONE"
    benchmark_label: Optional[str] = None
    bars_through: Optional[str] = None            # last completed bar used (ISO)
    session: SessionInfo = Field(default_factory=SessionInfo)
    computed_at: str


class DemandResult(_M):
    version: str = VERSION
    symbol: str
    as_of: str
    status: str                                   # AVAILABLE | SIMULATED_DATA | INSUFFICIENT_DATA | NOT_AVAILABLE
    mode: str                                     # INTRADAY_MODE | DAILY_MODE | NONE
    score: Optional[int] = None
    label: Optional[str] = None
    coverage_weight: float = 0.0
    components: Dict[str, DemandComponent] = Field(default_factory=dict)
    key_factors: List[str] = Field(default_factory=list)
    sources: DemandSources
    weights: Dict[str, float]
    disclaimer: str = DISCLAIMER
    note: Optional[str] = None


def not_available(symbol: str, as_of: str, *, mode: str, market_data: str, computed_at: str, note: str, weights: Dict[str, float]) -> DemandResult:
    return DemandResult(symbol=symbol, as_of=as_of, status="NOT_AVAILABLE", mode="NONE", weights=dict(weights), note=note,
                        sources=DemandSources(mode=mode, market_data=market_data, computed_at=computed_at))
