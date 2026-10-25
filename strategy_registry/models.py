"""strategy_registry/models.py — explicit shapes (extra = forbid). A registry entry describes a strategy; a detail adds its documented / live /
not-yet-evaluated sections; a StrategySignal is the Strategy Standard v1 normalized object (strategy_registry.standard validates it)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from strategy_registry import (ASSET_CLASSES, DIRECTIONS, NOT_EVALUATED, RISK_CLASSES, SCORE_KINDS, SCORE_MAX, SCORE_MIN, SECTION_STATES, SIGNAL_DIRECTIONS, SOURCES,
                               STANDARD_VERSION, STATUSES, TIME_HORIZONS, UNAVAILABLE)

ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _enum(name: str, allowed: tuple):
    def check(cls, v):
        if v not in allowed:
            raise ValueError(f"{name} must be one of {', '.join(allowed)}, not {v!r}")
        return v
    return check


def _iso(v: str, name: str) -> str:
    try:
        datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an ISO 8601 timestamp, not {v!r}")
    return v


class StrategyEntry(_M):
    """One registry row. `implemented` is the only claim of existing logic; placeholders keep it False and carry no setup_type."""
    strategy_id: str
    display_name: str
    source: str
    asset_class: str
    direction_capability: str
    time_horizon: str
    status: str
    description: str
    version: str
    created_at: str
    updated_at: str
    implemented: bool = False
    setup_type: Optional[str] = None          # the internal setup type the strategy evaluates (implemented strategies only)
    placeholder_note: Optional[str] = None    # placeholders: what does NOT exist yet, in plain words

    _source = field_validator("source")(_enum("source", SOURCES))
    _asset = field_validator("asset_class")(_enum("asset_class", ASSET_CLASSES))
    _direction = field_validator("direction_capability")(_enum("direction_capability", DIRECTIONS))
    _horizon = field_validator("time_horizon")(_enum("time_horizon", TIME_HORIZONS))
    _status = field_validator("status")(_enum("status", STATUSES))

    @field_validator("strategy_id")
    @classmethod
    def _id(cls, v):
        if not ID_RE.match(v or ""):
            raise ValueError("strategy_id must be UPPER_SNAKE_CASE (3-64 characters)")
        return v

    @field_validator("display_name", "description", "version")
    @classmethod
    def _non_empty(cls, v, info):
        if not str(v or "").strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v

    @field_validator("created_at", "updated_at")
    @classmethod
    def _dates(cls, v, info):
        if not DATE_RE.match(str(v or "")):
            raise ValueError(f"{info.field_name} must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if not self.implemented and self.setup_type:
            raise ValueError("a strategy that is not implemented cannot name a setup_type")
        if not self.implemented and self.status not in ("RESEARCH", "REJECTED"):
            raise ValueError("a strategy that is not implemented can only be RESEARCH (or REJECTED)")
        return self


class Section(_M):
    key: str
    title: str
    state: str                                 # DOCUMENTED | LIVE | NOT_EVALUATED
    items: List[str] = Field(default_factory=list)      # plain sentences, each traceable to the listed sources
    facts: Dict[str, Any] = Field(default_factory=dict)  # the raw values behind the sentences (technical details)
    sources: List[str] = Field(default_factory=list)     # files / documents / routes the content comes from
    note: Optional[str] = None

    _state = field_validator("state")(_enum("state", SECTION_STATES))

    @model_validator(mode="after")
    def _honest(self):
        if self.state in ("NOT_EVALUATED", "UNAVAILABLE") and (self.items or self.facts):
            raise ValueError(f"a {self.state} section carries no items or facts")
        if self.state in ("DOCUMENTED", "LIVE") and not self.items:
            raise ValueError(f"a {self.state} section needs at least one item")
        if self.state == "NOT_EVALUATED" and not self.note:
            self.note = NOT_EVALUATED
        if self.state == "UNAVAILABLE" and not self.note:
            self.note = UNAVAILABLE
        return self


class StrategyDetail(_M):
    entry: StrategyEntry
    lifecycle: Dict[str, Any] = Field(default_factory=dict)     # declared status, derived status and the facts behind the derivation
    sections: List[Section] = Field(default_factory=list)
    links: Dict[str, str] = Field(default_factory=dict)         # research / backtest / validation targets in the product UI
    disclaimer: str = ""


class StrategySignal(_M):
    """Strategy Standard v1: the normalized object every strategy must eventually produce. Evidence only — never an order."""
    strategy_id: str
    source: str
    asset_class: str
    direction: str
    symbol: str
    score: float
    time_horizon: str
    entry_condition: str
    invalidation_condition: str
    exit_condition: str
    risk_class: str
    generated_at: str
    version: str = STANDARD_VERSION
    score_kind: str = "RANK"                   # RANK (default): a deterministic ranking value. CALIBRATED_PROBABILITY only with a documented calibration
    calibration: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)      # optional deterministic facts (no execution field)

    _source = field_validator("source")(_enum("source", SOURCES))
    _asset = field_validator("asset_class")(_enum("asset_class", ASSET_CLASSES))
    _direction = field_validator("direction")(_enum("direction", SIGNAL_DIRECTIONS))
    _horizon = field_validator("time_horizon")(_enum("time_horizon", TIME_HORIZONS))
    _risk = field_validator("risk_class")(_enum("risk_class", RISK_CLASSES))
    _kind = field_validator("score_kind")(_enum("score_kind", SCORE_KINDS))

    @field_validator("strategy_id")
    @classmethod
    def _id(cls, v):
        if not ID_RE.match(v or ""):
            raise ValueError("strategy_id must be UPPER_SNAKE_CASE (3-64 characters)")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v):
        if not SYMBOL_RE.match(v or ""):
            raise ValueError("symbol must be an upper-case ticker")
        return v

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, v):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v or not (SCORE_MIN <= float(v) <= SCORE_MAX):   # a JSON number, never a string, never a bool
            raise ValueError(f"score must be a number between {SCORE_MIN:g} and {SCORE_MAX:g}")
        return float(v)

    @field_validator("entry_condition", "invalidation_condition", "exit_condition", "version")
    @classmethod
    def _text(cls, v, info):
        if not str(v or "").strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v

    @field_validator("generated_at")
    @classmethod
    def _generated(cls, v):
        return _iso(v, "generated_at")

    @model_validator(mode="after")
    def _calibrated(self):
        if self.score_kind == "CALIBRATED_PROBABILITY" and not (self.calibration or "").strip():
            raise ValueError("a CALIBRATED_PROBABILITY score needs a documented calibration; otherwise the score is a RANK, not a probability")
        if self.score_kind == "RANK" and self.calibration:
            raise ValueError("a RANK score carries no calibration claim")
        return self
