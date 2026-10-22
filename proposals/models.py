"""proposals/models.py — explicit shapes (extra = forbid). Money and prices travel as canonical Decimal strings."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from proposals import DISCLAIMER, SETUP_TYPE, SIDE_CONTEXT, SIDE_LABEL, STATUS_LABELS, SUBMISSION_STATUS, VERSION


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceCandidate(_M):
    source: str                                 # QUOTE_MID | LAST_TRADE | MINUTE_CLOSE | DAILY_CLOSE
    value: Optional[float] = None
    ts: Optional[float] = None                  # epoch seconds of the price
    age_s: Optional[float] = None
    mode: str = "INTRADAY"                      # INTRADAY | DAILY_REFERENCE
    status: str = "OK"                          # OK | UNAVAILABLE
    note: Optional[str] = None


class AccountSnapshot(_M):
    equity: Optional[str] = None
    equity_source: str = "NOT_AVAILABLE"        # ALPACA_PAPER_VIEW | LOCAL_PAPER | HYPOTHETICAL_REVIEW | NOT_AVAILABLE
    equity_as_of: Optional[str] = None
    equity_age_s: Optional[float] = None
    buying_power: Optional[str] = None
    buying_power_source: str = ""
    cash: Optional[str] = None
    halt: bool = False                          # review halt (config) or broker account block
    halt_source: str = ""
    daily_loss_halt: bool = False
    daily_loss_source: str = ""
    no_new_entries: bool = False                # Stage 5.9.1: the persisted pause (from the execution safety gate)
    no_new_entries_source: str = ""
    safety_state: Optional[str] = None          # Stage 5.9.1: TRADING_ALLOWED | NO_NEW_ENTRIES | HALTED | UNKNOWN as the gate reported it
    safety_reason_codes: List[str] = Field(default_factory=list)
    tradable: Optional[bool] = None
    tradability_source: str = ""
    pending_orders: int = 0
    pending_source: str = ""
    pending_detail: List[str] = Field(default_factory=list)
    existing_qty: Optional[str] = None
    existing_avg_cost: Optional[str] = None
    existing_notional: Optional[str] = None
    position_source: str = ""
    notes: List[str] = Field(default_factory=list)


class ProposalInputs(_M):
    symbol: str
    now: str                                    # ISO UTC
    now_ts: float
    mode: str = "simulated"                     # simulated | real | test
    market_data_source: str = ""
    setup_view: Dict[str, Any] = Field(default_factory=dict)     # setups.models.SetupView dump
    episode: Optional[Dict[str, Any]] = None                     # setups.models.EpisodeRecord dump (open episode)
    price_candidates: List[PriceCandidate] = Field(default_factory=list)
    daily_bars: List[Dict[str, Any]] = Field(default_factory=list)   # completed sessions, oldest -> newest: {date, open, high, low, close}
    intraday_lows: List[float] = Field(default_factory=list)         # lows of the current session's completed minute candles
    account: AccountSnapshot = Field(default_factory=AccountSnapshot)
    session: Dict[str, Any] = Field(default_factory=dict)            # {regular_session, seconds_to_close, next_open, session_hint}
    risk_fraction: str = "0.005"
    max_position_pct: str = "10"
    ttl_seconds: int = 300
    setup_fresh_window_s: float = 300.0
    sector_fallback: bool = False
    ppe_snapshot: Dict[str, Any] = Field(default_factory=dict)
    demand_snapshot: Dict[str, Any] = Field(default_factory=dict)
    evidence_snapshot: Dict[str, Any] = Field(default_factory=dict)


class ControlEvaluation(_M):
    control_id: str
    result: str                                 # PASS | BLOCK | WARN | UNKNOWN
    reason: str = ""
    input: Dict[str, Any] = Field(default_factory=dict)


class Draft(_M):
    """What the pure engine returns for one evaluation (not yet persisted)."""
    symbol: str
    setup_id: Optional[str] = None
    setup_type: str = SETUP_TYPE
    status: str                                 # NOT_ELIGIBLE | BLOCKED | REVIEWABLE
    blockers: List[Dict[str, str]] = Field(default_factory=list)
    warnings: List[Dict[str, str]] = Field(default_factory=list)
    controls: List[ControlEvaluation] = Field(default_factory=list)
    price_mode: Optional[str] = None
    reference_price: Optional[str] = None
    price_source: Optional[str] = None
    price_timestamp: Optional[str] = None
    invalidation_price: Optional[str] = None
    stop_rule: Optional[str] = None
    structural_low: Optional[str] = None
    atr: Optional[str] = None
    atr_period: int = 0
    risk_per_share: Optional[str] = None
    risk_pct: Optional[str] = None
    equity: Optional[str] = None
    equity_source: str = "NOT_AVAILABLE"
    risk_fraction: Optional[str] = None
    risk_budget: Optional[str] = None
    base_qty: Optional[int] = None
    position_cap_qty: Optional[int] = None
    buying_power_qty: Optional[int] = None
    final_qty: Optional[int] = None
    max_notional: Optional[str] = None
    max_position_pct: Optional[str] = None
    cap_source: Optional[str] = None
    existing_position_qty: Optional[str] = None
    existing_position_notional: Optional[str] = None
    existing_avg_cost: Optional[str] = None
    proposed_notional: Optional[str] = None
    caps: Dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[str] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    ppe_snapshot: Dict[str, Any] = Field(default_factory=dict)
    demand_snapshot: Dict[str, Any] = Field(default_factory=dict)
    evidence_snapshot: Dict[str, Any] = Field(default_factory=dict)
    setup_snapshot: Dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""
    mode: str = ""
    source: str = ""


class ProposalView(_M):
    """The stored (or transient NOT_ELIGIBLE) proposal as the API and the socket deliver it. Never an order; never a recommendation."""
    version: str = VERSION
    symbol: str
    setup_id: Optional[str] = None
    setup_type: str = SETUP_TYPE
    side_context: str = SIDE_CONTEXT
    side_label: str = SIDE_LABEL
    proposal_id: Optional[str] = None
    status: str = "NOT_ELIGIBLE"                # stored generation status
    effective_status: str = "NOT_ELIGIBLE"      # status | STALE | EXPIRED (derived at read time)
    status_label: str = STATUS_LABELS["NOT_ELIGIBLE"]
    stale_reason: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    price_mode: Optional[str] = None
    reference_price: Optional[str] = None
    price_source: Optional[str] = None
    price_timestamp: Optional[str] = None
    invalidation_price: Optional[str] = None
    stop_rule: Optional[str] = None
    structural_low: Optional[str] = None
    atr: Optional[str] = None
    atr_period: int = 0
    risk_per_share: Optional[str] = None
    risk_pct: Optional[str] = None
    equity: Optional[str] = None
    equity_source: str = "NOT_AVAILABLE"
    risk_fraction: Optional[str] = None
    risk_budget: Optional[str] = None
    base_qty: Optional[int] = None
    position_cap_qty: Optional[int] = None
    buying_power_qty: Optional[int] = None
    final_qty: Optional[int] = None
    max_notional: Optional[str] = None
    max_position_pct: Optional[str] = None
    cap_source: Optional[str] = None
    existing_position_qty: Optional[str] = None
    existing_position_notional: Optional[str] = None
    existing_avg_cost: Optional[str] = None
    proposed_notional: Optional[str] = None
    caps: Dict[str, Any] = Field(default_factory=dict)
    blockers: List[Dict[str, str]] = Field(default_factory=list)
    warnings: List[Dict[str, str]] = Field(default_factory=list)
    controls: List[Dict[str, Any]] = Field(default_factory=list)
    inputs: Dict[str, Any] = Field(default_factory=dict)
    ppe_snapshot: Dict[str, Any] = Field(default_factory=dict)
    demand_snapshot: Dict[str, Any] = Field(default_factory=dict)
    evidence_snapshot: Dict[str, Any] = Field(default_factory=dict)
    setup_snapshot: Dict[str, Any] = Field(default_factory=dict)
    execution_eligibility: Dict[str, Any] = Field(default_factory=dict)   # Stage 5.9.1: {allowed, state, reason_codes, ...} from the LIVE safety gate, derived at read time
    review_state: str = "UNREVIEWED"
    reviewed_at: Optional[str] = None
    submission_status: str = SUBMISSION_STATUS
    trigger: Optional[str] = None
    source: str = ""
    mode: str = ""
    disclaimer: str = DISCLAIMER
    note: Optional[str] = None
