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
    max_hold_bars: Optional[int] = None         # Stage 6.4: a per-candidate holding limit (a rebalance strategy's leg); None = the adapter's
    entry_trade: bool = True                    # Stage 6.4: False = the leg continues a position already held (no transaction, no friction)
    exit_trade: bool = True                     # Stage 6.4: False = the position continues into the next leg (no transaction, no friction)


class StrategyCandidate(_Frozen):
    candidate_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    signal_date: str
    signal_basis: str = "SESSION_CLOSE"
    entry_reference: float
    invalidation_reference: Optional[float] = None   # None only for an adapter whose stop_policy is NONE (Stage 6.4)
    target_reference: Optional[float] = None
    max_hold_bars: int
    entry_trade: bool = True                    # transaction flags (Stage 6.4): not part of the identity - like the holding limit, an exit-side fact
    exit_trade: bool = True
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
    stop: Optional[float] = None               # None: a stop-less strategy (stop_policy NONE, e.g. a periodic rebalance)
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
    entry_trade: bool = True                    # Stage 6.4: friction is charged only on a real transaction
    exit_trade: bool = True
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
    manual_review_status: str = "UNDECLARED"   # Stage 6.3.1: the adapter's DECLARED review (PENDING | COMPLETED); never inferred from the automated checks
    manual_review_reference: str = ""          # where a COMPLETED review is recorded (required for COMPLETED)


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
    research_flags: List[str] = Field(default_factory=list)       # Stage 6.4: adapter-declared research limitations shown with the result
    # Stage 6.3.1 (validation_v2): absent on Stage 6.2 records, which stay readable and are never back-filled
    provenance: Dict[str, Any] = Field(default_factory=dict)        # the dataset's declared + derived provenance
    boundary: Dict[str, Any] = Field(default_factory=dict)          # the TRAIN / OOS boundary PURGE: policy, train_end, purged candidate ids per arm
    accounting: Dict[str, Any] = Field(default_factory=dict)        # accepted = simulated + boundary_purged + explicit skips, per arm and segment
    replay_parity: Dict[str, Any] = Field(default_factory=dict)     # the adapter's declared parity requirement / status
    eligibility: Dict[str, Any] = Field(default_factory=dict)       # PersistenceEligibility (integrity status, checks, reasons) - recomputed by the store
