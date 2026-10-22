"""Explicit Trader View response models. The browser only ever sees these shapes — never a broker- or vendor-specific payload."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from demand.models import DemandResult
from ppe.models import PPEResult
from proposals.models import ProposalView
from setups.models import SetupView
from trader_view import NOT_CONNECTED


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Quote(_M):
    symbol: str
    status: str = "OK"                      # OK | UNAVAILABLE
    code: Optional[str] = None              # None | NO_DATA (symbol unknown to the feed) | FEED_ERROR (feed unreachable)
    price: Optional[float] = None
    open: Optional[float] = None            # the current session's open when the source provides it (Stage 5.6 PPE)
    bid: Optional[float] = None
    ask: Optional[float] = None
    prev_close: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    ts: Optional[int] = None                # epoch seconds of the price
    source: str = ""
    message: Optional[str] = None


class Candle(_M):
    time: int                               # epoch seconds, minute aligned
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    final: bool = False


class PositionSource(_M):
    source: str                             # ALPACA_PAPER_VIEW | LOCAL_PAPER | ROBINHOOD_READ_ONLY
    label: str                              # "Alpaca Paper View" | "Local Paper Portfolio" | "Robinhood Read Only"
    status: str                             # POSITION | NO_POSITION | NOT_CONFIGURED | NOT_REFRESHED | NOT_SET_UP | DISABLED | UNAVAILABLE | ERROR
    message: Optional[str] = None
    quantity: Optional[str] = None          # exact strings as the source reports them (never recomputed across sources)
    average_cost: Optional[str] = None
    market_value: Optional[str] = None
    unrealized_pnl: Optional[str] = None
    unrealized_pnl_pct: Optional[str] = None
    as_of: Optional[str] = None


class PositionState(_M):
    symbol: str
    has_position: bool
    sources: List[PositionSource]
    note: str


class EvidenceSummary(_M):
    symbol: str
    status: str                             # LOADED | NOT_LOADED | FAILED
    packet_id: Optional[str] = None
    packet_status: Optional[str] = None     # COMPLETED | FAILED (the stored packet's own status)
    created_at: Optional[str] = None
    as_of_date: Optional[str] = None
    strength: Optional[str] = None          # WEAK | MODERATE | STRONG
    strength_text: Optional[str] = None
    cause: Optional[str] = None             # cause verdict
    cause_reason: Optional[str] = None
    company_confirm: Optional[str] = None   # ladder rung 5 status
    market_confirm: Optional[str] = None    # ladder rung 6 status
    rungs: List[Dict[str, Any]] = Field(default_factory=list)
    n_items: Optional[int] = None
    n_failed_fetches: Optional[int] = None
    data_status: str = "NOT_LOADED"         # STORED | NOT_LOADED | FAILED
    message: Optional[str] = None
    disclaimer: str


class Tradability(_M):
    status: str                             # UNKNOWN | SIMULATED
    message: str


class DataStatus(_M):
    mode: str                               # simulated | real
    market: str                             # OK | UNAVAILABLE | SIMULATED
    market_source: str
    position: str                           # OK | PARTIAL | UNAVAILABLE
    evidence: str                           # LOADED | NOT_LOADED | FAILED
    server_time: str


class Action(_M):
    id: str
    label: str
    enabled: bool = False
    status: str
    message: str


def default_actions() -> List[Action]:
    return [
        Action(id="prepare_buy", label="Prepare Buy", enabled=False, status="PREVIEW_NOT_CONNECTED", message=NOT_CONNECTED),
        Action(id="prepare_sell", label="Prepare Sell", enabled=False, status="PREVIEW_NOT_CONNECTED", message=NOT_CONNECTED),
        Action(id="research", label="Research", enabled=True, status="NAVIGATE_ONLY",
               message="Opens the stored evidence workspace (Strategy Lab › Evidence). No research is run and no model is called."),
        Action(id="add_alert", label="Add Alert", enabled=False, status="PLACEHOLDER",
               message="Alert creation is not connected in Trader View. The Alerts tab shows the rule-based alerts."),
    ]


class TerminalState(_M):
    label: str
    symbol: str
    quote: Quote
    position: PositionState
    evidence: EvidenceSummary
    ppe: PPEResult
    demand: DemandResult                    # Stage 5.7 Market Demand — separate from PPE, never combined with it
    setup: SetupView                        # Stage 5.8 setup state (NONE / WATCH / READY FOR REVIEW / INVALIDATED) — stored view, never an entry signal
    proposal: ProposalView                  # Stage 5.9 review proposal for a READY setup — a review artifact, never a recommendation or an order
    tradability: Tradability
    data_status: DataStatus
    actions: List[Action]
    market_state: Dict[str, Any]
    candles: List[Candle] = Field(default_factory=list)
