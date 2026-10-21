"""PPE output contract (pydantic). No BUY/SELL, no expected return, no probability-of-rise field exists here (tested)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ppe import DISCLAIMER, VERSION


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComponentResult(_M):
    key: str
    label: str
    weight: float
    status: str                                   # AVAILABLE | NOT_AVAILABLE
    score: Optional[float] = None                 # [-100, 100]
    raw: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    code: Optional[str] = None                    # why NOT_AVAILABLE: INSUFFICIENT_HISTORY | ZERO_ATR | ZERO_VARIANCE | NO_BENCHMARK | NO_VOLUME | …


class StretchResult(_M):
    status: str                                   # AVAILABLE | INSUFFICIENT_DATA | NOT_AVAILABLE
    score: Optional[float] = None                 # display score, 1 decimal
    raw_score: Optional[float] = None
    label: Optional[str] = None
    coverage_weight: float = 0.0
    direction: Optional[str] = None               # DOWN | UP | FLAT
    components: Dict[str, ComponentResult] = Field(default_factory=dict)
    key_factors: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class EvidenceSnapshot(_M):
    packet_id: str
    packet_status: str
    created_at: str
    as_of_date: Optional[str] = None
    age_days: int
    strength: Optional[str] = None
    cause: Optional[str] = None
    explained_share: Optional[float] = None
    rungs: Dict[str, str] = Field(default_factory=dict)        # "1".."6" -> SUPPORTED | PARTIAL | NOT_SUPPORTED | NOT_AVAILABLE
    company_down: int = 0
    company_up: int = 0
    guidance_down: int = 0
    guidance_up: int = 0
    supply_down: int = 0
    supply_up: int = 0
    contradictions: int = 0
    n_items: int = 0


class ConfidenceResult(_M):
    status: str                                   # AVAILABLE | EVIDENCE_NOT_AVAILABLE | STRETCH_NOT_AVAILABLE | NOT_APPLICABLE
    score: Optional[int] = None
    label: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)
    contributions: Dict[str, int] = Field(default_factory=dict)
    caps: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: Optional[EvidenceSnapshot] = None


class PPESources(_M):
    mode: str                                     # real | simulated | replay
    market_data: str
    price_source: str
    bars_through: Optional[str] = None
    sessions_used: int = 0
    benchmark: Optional[str] = None
    benchmark_kind: str = "NONE"
    benchmark_label: Optional[str] = None
    evidence_packet_at: Optional[str] = None
    computed_at: str


class PPEResult(_M):
    version: str = VERSION
    symbol: str
    as_of: str
    status: str                                   # AVAILABLE | SIMULATED_DATA | INSUFFICIENT_DATA | NOT_AVAILABLE
    stretch: StretchResult
    confidence: ConfidenceResult
    sources: PPESources
    weights: Dict[str, float]
    disclaimer: str = DISCLAIMER
    note: Optional[str] = None


def not_available(symbol: str, as_of: str, *, mode: str, market_data: str, computed_at: str, note: str, weights: Dict[str, float]) -> PPEResult:
    return PPEResult(symbol=symbol, as_of=as_of, status="SIMULATED_DATA" if mode == "simulated" else "NOT_AVAILABLE",
                     stretch=StretchResult(status="NOT_AVAILABLE", note=note),
                     confidence=ConfidenceResult(status="STRETCH_NOT_AVAILABLE", reasons=["PPE Stretch is not available, so there is no move to classify."]),
                     sources=PPESources(mode=mode, market_data=market_data, price_source="", computed_at=computed_at), weights=dict(weights), note=note)
