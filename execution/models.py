"""
execution/models.py — Stage 6.0 records and views (pydantic, plain data). Money and prices are decimal strings; quantities are whole shares.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from execution import DISCLAIMER, VERSION


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Environment(_M):
    """The paper-environment proof: true only when the paper host answered the account read."""
    paper: bool = False
    host: str = ""
    label: str = ""
    simulated: bool = False
    account_number_masked: Optional[str] = None
    status: Optional[str] = None
    blocked: bool = False
    checked_at: Optional[str] = None
    error: Optional[str] = None


class Intent(_M):
    """One entry or exit intent. Identity columns never change after the insert; only the lifecycle columns do."""
    execution_id: str
    intent_type: str                              # ENTRY | EXIT
    symbol: str
    side: str                                     # buy | sell
    qty: int
    client_order_id: str
    proposal_id: Optional[str] = None
    setup_id: Optional[str] = None
    position_id: Optional[str] = None
    exit_cycle: int = 0
    exit_reason: Optional[str] = None
    reference_price: Optional[str] = None
    invalidation_price: Optional[str] = None
    risk_budget: Optional[str] = None
    proposal_qty: Optional[int] = None
    state: str = "PENDING"
    broker_order_id: Optional[str] = None
    broker_status: Optional[str] = None
    filled_qty: int = 0
    filled_avg_price: Optional[str] = None
    attempt_count: int = 0
    lookup_count: int = 0
    last_lookup_at: Optional[str] = None
    failure_code: Optional[str] = None
    failure_text: Optional[str] = None
    outcome_class: Optional[str] = None
    created_at: str
    submitted_at: Optional[str] = None
    resolved_at: Optional[str] = None
    updated_at: str
    created_by: str = "system"
    proposal_snapshot: Dict[str, Any] = Field(default_factory=dict)
    environment: Dict[str, Any] = Field(default_factory=dict)


class Position(_M):
    """An internal paper position: created only from broker-confirmed fills; quantity is the current open quantity."""
    position_id: str
    execution_id: str
    proposal_id: Optional[str] = None
    setup_id: Optional[str] = None
    symbol: str
    side: str = "LONG"
    quantity: int
    entry_qty: int
    average_entry_price: str
    opened_at: str
    invalidation_price: Optional[str] = None
    risk_budget: Optional[str] = None
    source: str = "BROKER_FILLS"
    broker: str = ""
    state: str = "OPEN"
    demand_low_streak: int = 0
    last_demand_eval_at: Optional[str] = None
    sessions_held: int = 0
    weakening_reason: Optional[str] = None
    exit_reason: Optional[str] = None
    exit_cycle: int = 0
    exit_requested_at: Optional[str] = None
    exit_qty: int = 0
    average_exit_price: Optional[str] = None
    realized_pnl: Optional[str] = None
    last_price: Optional[str] = None
    last_price_at: Optional[str] = None
    attention_reason: Optional[str] = None
    closed_at: Optional[str] = None
    updated_at: str


class ControlEvaluation(_M):
    control_id: str
    result: str                                   # PASS | BLOCK | WARN | UNKNOWN | RUN
    reason: str = ""
    input: Dict[str, Any] = Field(default_factory=dict)


class Finding(_M):
    kind: str
    symbol: Optional[str] = None
    blocking: bool = True
    execution_id: Optional[str] = None
    position_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class SymbolCard(_M):
    """What a beginner sees for one ticker: an icon, two or three words, one plain line. Internal enums live under details."""
    symbol: str
    key: str                                      # NONE | WATCHING | READY | ENTERING | IN_TRADE | EXITING | CLOSED | STOPPED | ATTENTION
    icon: str
    label: str
    line: str = ""
    state: str = "IDLE"                           # the internal execution state
    setup_state: str = "NONE"
    execution_id: Optional[str] = None
    position_id: Optional[str] = None
    shares: Optional[int] = None
    entry: Optional[str] = None
    current: Optional[str] = None
    pnl: Optional[str] = None
    pnl_pct: Optional[str] = None
    reason: Optional[str] = None
    since: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class BotStatus(_M):
    version: str = VERSION
    mode: str = "MONITOR_ONLY"
    mode_label: str = "Monitor only"
    icon: str = "⚪"
    headline: str = "Monitor only"
    attention: bool = False
    attention_reasons: List[str] = Field(default_factory=list)
    ready: bool = False
    environment: Dict[str, Any] = Field(default_factory=dict)
    safety_state: Optional[str] = None
    safety_label: Optional[str] = None
    open_positions: int = 0
    pending_entries: int = 0
    pending_exits: int = 0
    external_positions: int = 0
    external_positions_source: Optional[str] = None                 # BROKER_RECONCILIATION | ACCOUNT_REFRESH (the newer read) | None (not read yet)
    external_positions_as_of: Optional[str] = None
    external_symbols: List[str] = Field(default_factory=list)
    max_open_positions: int = 0
    reconciliation_healthy: bool = True
    last_cycle_at: Optional[str] = None
    last_cycle_result: Optional[str] = None
    scheduler: Dict[str, Any] = Field(default_factory=dict)
    cards: List[SymbolCard] = Field(default_factory=list)
    recent: List[Dict[str, Any]] = Field(default_factory=list)
    mode_changed_at: Optional[str] = None
    mode_changed_by: Optional[str] = None
    account_data: Dict[str, Any] = Field(default_factory=dict)      # Stage 6.0.1: the read-only account refresher's freshness
    disclaimer: str = DISCLAIMER
