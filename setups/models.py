"""Setup state contracts (pydantic, extra=forbid). No buy / sell, recommendation, probability, target or signal field exists here (tested)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from setups import DISCLAIMER, SETUP_NAME, SETUP_TYPE, STATE_LABELS, VERSION


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupInputs(_M):
    """What the evaluation saw — already-computed research only (DESIGN_58 §3)."""
    symbol: str
    evaluated_at: str                                   # ISO UTC
    stretch: Optional[float] = None
    stretch_status: str = "NOT_AVAILABLE"               # AVAILABLE | INSUFFICIENT_DATA | NOT_AVAILABLE
    ppe_status: str = "NOT_AVAILABLE"                   # AVAILABLE | SIMULATED_DATA | INSUFFICIENT_DATA | NOT_AVAILABLE
    ppe_coverage: float = 0.0
    ppe_computed_at: Optional[str] = None
    confidence: Optional[int] = None
    confidence_status: str = "STRETCH_NOT_AVAILABLE"
    demand: Optional[int] = None
    demand_status: str = "NOT_AVAILABLE"                # AVAILABLE | SIMULATED_DATA | INSUFFICIENT_DATA | NOT_AVAILABLE
    demand_coverage: float = 0.0
    demand_mode: str = "NONE"
    demand_computed_at: Optional[str] = None
    evidence_available: bool = False
    evidence_packet_id: Optional[str] = None
    evidence_created_at: Optional[str] = None
    evidence_age_days: Optional[int] = None
    evidence_cause: Optional[str] = None
    evidence_strength: Optional[str] = None
    company_confirm: Optional[str] = None
    guidance_down: int = 0
    company_down: int = 0
    quote_ts: Optional[int] = None
    quote_status: str = "UNAVAILABLE"
    market_data_source: str = ""
    mode: str = "simulated"                             # simulated | real | test
    session_hint: str = ""
    ppe_fresh: bool = True
    demand_fresh: bool = True
    quote_fresh: bool = True
    stale_reasons: List[str] = Field(default_factory=list)


class EpisodeRecord(_M):
    setup_id: str
    symbol: str
    setup_type: str = SETUP_TYPE
    cycle_id: int = 1
    state: str = "NONE"
    previous_state: Optional[str] = None
    started_at: str
    last_transition_at: str
    last_evaluated_at: str
    closed_at: Optional[str] = None
    invalidation_reason: Optional[str] = None
    transition_reason: str = ""
    demand_low_streak: int = 0
    evidence_packet_id_at_start: Optional[str] = None


class Decision(_M):
    decision: str                                       # TRANSITION | HOLD | PAUSED
    from_state: str
    to_state: str
    reason_code: str
    reason_text: str
    blockers: List[str] = Field(default_factory=list)
    demand_low_streak: int = 0
    episode: str = "CONTINUE"                           # NEW | CONTINUE | CLOSE | NONE


class SetupView(_M):
    """What Trader View / the API show for one ticker."""
    version: str = VERSION
    symbol: str
    setup_type: str = SETUP_TYPE
    setup_name: str = SETUP_NAME
    state: str = "NONE"
    state_label: str = STATE_LABELS["NONE"]
    setup_id: Optional[str] = None
    cycle_id: Optional[int] = None
    previous_state: Optional[str] = None
    since: Optional[str] = None                         # last transition time (ISO)
    started_at: Optional[str] = None
    last_evaluated_at: Optional[str] = None
    decision: Optional[str] = None                      # last decision: TRANSITION | HOLD | PAUSED | ERROR | None (never evaluated)
    reason_code: Optional[str] = None
    reason_text: Optional[str] = None
    blockers: List[str] = Field(default_factory=list)
    invalidation_reason: Optional[str] = None
    paused: bool = False
    inputs: Optional[SetupInputs] = None
    freshness: Dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    mode: str = ""
    disclaimer: str = DISCLAIMER
    note: Optional[str] = None


class SetupAlert(_M):
    alert_id: str
    transition_id: str
    setup_id: str
    symbol: str
    setup_type: str = SETUP_TYPE
    from_state: str
    to_state: str
    to_state_label: str
    reason_code: str
    title: str
    body: str
    created_at: str
    read_at: Optional[str] = None
