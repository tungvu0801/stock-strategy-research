"""strategy_validation/models.py — explicit shapes (extra = forbid). A StrategyCandidate is immutable (frozen) and carries its identity hash;
arm decisions can only TAKE / ALLOW or STAND_ASIDE / VETO a candidate by id - they have no field that could change a symbol, a direction, a stop,
a target or a timing."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bar(_Frozen):
    date: str                                   # YYYY-MM-DD (a completed daily session)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class RawCandidate(_M):
    """What a strategy adapter emits for one signal (before identity, missing-data and Standard v1 checks)."""
    symbol: str
    direction: str = "LONG"
    signal_date: str                            # the session whose CLOSE completed the signal (the signal is known at that close)
    entry_reference: Optional[float] = None     # the signal-bar close the strategy saw
    invalidation_reference: Optional[float] = None
    target_reference: Optional[float] = None
    score: Optional[float] = None               # a deterministic rank 0-100 (Strategy Standard v1); never a probability
    features: Dict[str, Any] = Field(default_factory=dict)
    feature_as_of: Dict[str, str] = Field(default_factory=dict)   # the latest date each feature used (must be <= signal_date)


class StrategyCandidate(_Frozen):
    candidate_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    signal_date: str
    signal_basis: str = "SESSION_CLOSE"
    entry_reference: float
    invalidation_reference: float
    target_reference: Optional[float] = None
    max_hold_bars: int
    score: Optional[float] = None
    feature_snapshot_hash: str
    features: Dict[str, Any] = Field(default_factory=dict)
    feature_as_of: Dict[str, str] = Field(default_factory=dict)


class SkippedCandidate(_M):
    symbol: str
    signal_date: str
    reason: str
    detail: str = ""
    candidate_id: Optional[str] = None


class ArmDecision(_M):
    """RULES: TAKE always. GATED: TAKE (ALLOW) or STAND_ASIDE (VETO by a named gate). MODEL: TAKE or STAND_ASIDE. Nothing else is expressible."""
    candidate_id: str
    decision: str                               # TAKE | STAND_ASIDE
    source: str                                 # RULES | gate name | model name
    reason: str = ""
    score: Optional[float] = None               # model metadata only; never changes the candidate


class SimulatedTrade(_M):
    trade_id: str
    arm: str
    candidate_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    segment: str                                # TRAIN | OOS (by signal date)
    signal_date: str
    entry_date: str
    entry_policy: str
    entry_raw: float
    entry_fill: float
    stop: float
    target: Optional[float] = None
    exit_date: str
    exit_raw: float
    exit_fill: float
    exit_reason: str
    holding_bars: int
    max_hold_bars: int
    qty: int
    size_status: str                            # RISK_SIZED | CAPPED | MIN_ONE_UNIT
    configured_risk: float
    achieved_risk: float
    gross_pnl: float
    slippage_cost: float
    fees: float
    net_pnl: float
    r_gross: float
    r_net: float
    trade_return: float                         # net P/L / entry notional
    ambiguous_bar: bool = False
    alt_target_first_net: Optional[float] = None   # what the ambiguous bar would have given if the target had come first (diagnostic only)


class ArmResult(_M):
    arm: str
    status: str                                 # EVALUATED | NOT_EVALUATED | INVALID
    input_candidate_ids: List[str] = Field(default_factory=list)
    decisions: List[ArmDecision] = Field(default_factory=list)
    gates: List[str] = Field(default_factory=list)
    note: str = ""
    simulation_skips: List[SkippedCandidate] = Field(default_factory=list)
    trades: List[SimulatedTrade] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)   # TRAIN / OOS / ALL
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    verdict: Optional[Dict[str, Any]] = None


class CausalityReport(_M):
    automated_checks: List[Dict[str, Any]] = Field(default_factory=list)   # {check, result PASS|WARN|CRITICAL, detail, count}
    critical: bool = False
    manual_review_required: bool = True        # static and runtime checks cannot prove causality; a human review is always required
    manual_review_note: str = "MANUAL_CAUSALITY_REVIEW_REQUIRED: automated checks catch known patterns only; a reviewer must confirm every feature uses information available at the signal close."


class AuditResult(_M):
    status: str                                 # PASS | FAIL
    checked: int = 0
    validated: int = 0
    violations: List[Dict[str, Any]] = Field(default_factory=list)
    candidates_checked: int = 0
    note: str = ""


class ValidationRun(_M):
    run_id: str
    strategy_id: str
    strategy_version: str
    dataset_id: str
    dataset_kind: str                           # HISTORICAL | SYNTHETIC (synthetic runs are test fixtures only)
    dataset_start: str
    dataset_end: str
    train_start: str
    train_end: str
    oos_start: Optional[str] = None
    oos_end: Optional[str] = None
    oos_sessions: int = 0
    created_at: str
    validation_version: str
    git_commit_sha: Optional[str] = None
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    friction_model: Dict[str, Any] = Field(default_factory=dict)
    execution_assumptions: Dict[str, Any] = Field(default_factory=dict)
    candidate_count: int = 0
    candidate_set_hash: str = ""
    candidates_skipped: List[SkippedCandidate] = Field(default_factory=list)
    arms_run: List[str] = Field(default_factory=list)
    integrity: Dict[str, Any] = Field(default_factory=dict)
    causality: CausalityReport = Field(default_factory=CausalityReport)
    arms: Dict[str, ArmResult] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    audit: AuditResult = Field(default_factory=lambda: AuditResult(status="FAIL"))
    verdict: Dict[str, Any] = Field(default_factory=dict)
    candidates: List[StrategyCandidate] = Field(default_factory=list)
    disclaimer: str = ""
