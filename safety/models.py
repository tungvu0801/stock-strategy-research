"""safety/models.py — explicit shapes (extra = forbid). Money travels as canonical Decimal strings; nothing here names a side."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from safety import DISCLAIMER, STATE_ICONS, STATE_LABELS, VERSION


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KillSwitch(_M):
    active: bool = False
    activated_at: Optional[str] = None
    activated_by: Optional[str] = None
    reason: Optional[str] = None


class Pause(_M):
    active: bool = False
    activated_at: Optional[str] = None
    activated_by: Optional[str] = None
    reason: Optional[str] = None


class AccountEquity(_M):
    """What the evaluator saw of the account (from the Stage 5.9 read-only snapshot; never invented)."""
    equity: Optional[str] = None
    source: str = "NOT_AVAILABLE"               # ALPACA_PAPER_VIEW | LOCAL_PAPER | HYPOTHETICAL_REVIEW | NOT_AVAILABLE
    as_of: Optional[str] = None
    age_s: Optional[float] = None
    mode: str = ""
    adjustments: str = "UNAVAILABLE"            # NONE_POSSIBLE | UNAVAILABLE (deposits / withdrawals cannot be separated)
    notes: List[str] = Field(default_factory=list)


class Baseline(_M):
    trading_date: str
    starting_equity: str
    baseline_created_at: str
    source: str
    adjustments: str
    status: str = "ACTIVE"
    equity_as_of: Optional[str] = None
    note: Optional[str] = None


class DailyLoss(_M):
    status: str = "UNAVAILABLE"                 # OK | TRIGGERED | UNAVAILABLE
    code: Optional[str] = None                  # the reason code behind TRIGGERED / UNAVAILABLE
    trading_date: Optional[str] = None
    starting_equity: Optional[str] = None
    current_equity: Optional[str] = None
    daily_pnl: Optional[str] = None
    daily_loss_pct: Optional[str] = None
    threshold_pct: Optional[str] = None
    adjustments: Optional[str] = None
    baseline_created_at: Optional[str] = None
    equity_source: str = "NOT_AVAILABLE"
    equity_as_of: Optional[str] = None
    equity_age_s: Optional[float] = None
    latched: bool = False
    triggered_at: Optional[str] = None
    triggered_loss_pct: Optional[str] = None
    note: Optional[str] = None


class Freshness(_M):
    account_fresh: Optional[bool] = None
    account_age_s: Optional[float] = None
    max_age_s: float = 900.0
    evaluated_within_s: Optional[float] = None  # seconds since the previous persisted evaluation


class SafetyStateRecord(_M):
    kill_active: bool = False
    kill_activated_at: Optional[str] = None
    kill_activated_by: Optional[str] = None
    kill_reason: Optional[str] = None
    pause_active: bool = False
    pause_activated_at: Optional[str] = None
    pause_activated_by: Optional[str] = None
    pause_reason: Optional[str] = None
    daily_halt_active: bool = False
    daily_halt_trading_date: Optional[str] = None
    daily_halt_triggered_at: Optional[str] = None
    daily_halt_loss_pct: Optional[str] = None
    daily_halt_reason: Optional[str] = None
    updated_at: Optional[str] = None


class ControlEvaluation(_M):
    control_id: str
    result: str                                 # PASS | BLOCK | WARN | UNKNOWN
    reason: str = ""
    input: Dict[str, Any] = Field(default_factory=dict)


class SafetyView(_M):
    """The gate's answer (DESIGN_591 §8). `halted` is true for HALTED and UNKNOWN; `allowed_new_entry` only for TRADING_ALLOWED."""
    version: str = VERSION
    state: str = "UNKNOWN"
    state_label: str = STATE_LABELS["UNKNOWN"]
    icon: str = STATE_ICONS["UNKNOWN"]
    allowed_new_entry: bool = False
    halted: bool = True
    reason_codes: List[str] = Field(default_factory=list)
    reason_text: str = ""
    daily_loss: DailyLoss = Field(default_factory=DailyLoss)
    kill_switch: KillSwitch = Field(default_factory=KillSwitch)
    no_new_entries: Pause = Field(default_factory=Pause)
    freshness: Freshness = Field(default_factory=Freshness)
    controls: List[ControlEvaluation] = Field(default_factory=list)
    evaluated_at: str = ""
    trigger: str = ""
    persisted: bool = False
    error: Optional[str] = None
    disclaimer: str = DISCLAIMER
