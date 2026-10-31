"""
market_intel/models.py - the observation envelope and the fetch record (design C.4; pydantic, extra=forbid, frozen).

Invariants enforced here (each is a test):
* every time is timezone-aware and normalised to UTC; a naive time is rejected;
* available_at >= retrieved_at: a value can never be used before we had it. A backfilled row has available_at == retrieved_at, so
  history is never presented as having been known live;
* a symbol kind has a validated, upper-cased symbol (the API's own ticker rule); a series kind has a series id;
* an UNAVAILABLE or INSUFFICIENT_DATA observation carries no payload: a missing value is never a zero;
* status and freshness agree: a STALE status has STALE freshness, and a STALE value is never presented as AVAILABLE;
* quality flags are only the declared validation codes (validate.QUALITY_CODES);
* no field or payload key names an order, a quantity, a side or a price instruction (tested over the whole schema).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from market_intel import (ADJUSTMENTS, FEEDS, FRESHNESS, INSUFFICIENT_DATA, KINDS, NOT_APPLICABLE, PROVIDERS, SERIES_KINDS, SESSIONS, STALE,
                          STATUSES, SYMBOL_KINDS, UNAVAILABLE)

_HASH = re.compile("^[0-9a-f]{64}$")
_SERIES = re.compile("^[A-Z0-9_.:-]{1,40}$")
# a field or payload key with one of these words is an instruction, not an observation: refused (and tested over the schema).
# Words are matched as whole snake_case / camelCase tokens, so "inside" or "bid_size" are fine and "order_qty" or "buySide" are not.
FORBIDDEN_WORDS = ("order", "orders", "qty", "quantity", "side", "buy", "sell", "short", "target", "signal", "recommend", "recommendation",
                   "notional", "allocation")
FORBIDDEN_PHRASES = ("limit_price", "stop_price", "position_size", "take_profit", "stop_loss")
FETCH_STATUSES = ("OK", "NOT_MODIFIED", "HTTP_ERROR", "TIMEOUT", "RATE_LIMITED", "ACCESS_DENIED", "TOO_LARGE", "NOT_CONFIGURED",
                  "BUDGET_EXHAUSTED", "INVALID_RESPONSE", "ERROR")


def _utc(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError("times must be timezone-aware (UTC)")
    return v.astimezone(timezone.utc)


def forbidden_key(key: str) -> bool:
    snake = re.sub("([a-z0-9])([A-Z])", lambda m: m.group(1) + "_" + m.group(2), str(key)).lower()
    tokens = [t for t in re.split("[^a-z0-9]+", snake) if t]
    return any(t in FORBIDDEN_WORDS for t in tokens) or any(p in snake for p in FORBIDDEN_PHRASES)


def _one_of(v: str, allowed: tuple, name: str) -> str:
    if v not in allowed:
        raise ValueError(f"{name} must be one of {allowed}")
    return v


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Observation(_M):
    """One stored value of the data layer, whatever its source (a bar, a quote snapshot, an article, a filing, a macro print...)."""
    kind: str
    symbol: Optional[str] = None
    series_id: Optional[str] = None
    provider: str
    feed: str = "none"
    adjustment: str = "none"
    observed_at: Optional[datetime] = None          # the market / provider timestamp (bar start, quote time, article created_at)
    published_at: Optional[datetime] = None         # the source's own publication time, when it states one
    retrieved_at: datetime                          # our clock, when the response arrived
    available_at: datetime                          # the earliest moment a decision could have used it
    session: str
    session_date: Optional[date] = None             # the New York trading date
    status: str
    freshness: Optional[str] = None
    age_s: Optional[float] = Field(default=None, ge=0)
    dataset_id: Optional[str] = Field(default=None, max_length=80)
    content_hash: Optional[str] = None
    source_url: Optional[str] = Field(default=None, max_length=2000)
    source_identifier: Optional[str] = Field(default=None, max_length=200)   # accession number, article id, RSS guid
    tier: Optional[int] = Field(default=None, ge=1, le=6)                   # evidence.rules source tier
    quality_flags: Tuple[str, ...] = ()
    backfilled: bool = False
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", "published_at", "retrieved_at", "available_at")
    @classmethod
    def _times(cls, v):
        return _utc(v)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        return _one_of(v, KINDS, "kind")

    @field_validator("provider")
    @classmethod
    def _provider(cls, v):
        return _one_of(v, PROVIDERS, "provider")

    @field_validator("feed")
    @classmethod
    def _feed(cls, v):
        return _one_of(v, FEEDS, "feed")

    @field_validator("adjustment")
    @classmethod
    def _adjustment(cls, v):
        return _one_of(v, ADJUSTMENTS, "adjustment")

    @field_validator("session")
    @classmethod
    def _session(cls, v):
        return _one_of(v, SESSIONS, "session")

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        return _one_of(v, STATUSES, "status")

    @field_validator("freshness")
    @classmethod
    def _freshness(cls, v):
        return v if v is None else _one_of(v, FRESHNESS, "freshness")

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, v):
        if v is not None and not _HASH.match(v):
            raise ValueError("content_hash must be a lowercase sha256 hex digest")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v):
        if v is None:
            return v
        from api.validation import validate_symbol           # the API's own ticker rule; nothing is looked up
        return validate_symbol(v)

    @field_validator("series_id")
    @classmethod
    def _series(cls, v):
        if v is not None and not _SERIES.match(v):
            raise ValueError("series_id must be 1-40 characters of A-Z 0-9 _ . : -")
        return v

    @field_validator("quality_flags")
    @classmethod
    def _flags(cls, v):
        from market_intel.validate import QUALITY_CODES
        bad = [f for f in v if f not in QUALITY_CODES]
        if bad:
            raise ValueError(f"unknown quality flags {bad}")
        return tuple(v)

    @field_validator("payload")
    @classmethod
    def _payload(cls, v):
        bad = [k for k in v if forbidden_key(str(k))]
        if bad:
            raise ValueError(f"payload keys {bad} name an instruction, not an observation")
        for k, x in v.items():
            if isinstance(x, float) and not math.isfinite(x):
                raise ValueError(f"payload value {k} is not finite")
        return v

    @model_validator(mode="after")
    def _invariants(self):
        if self.available_at < self.retrieved_at:
            raise ValueError("available_at may not be earlier than retrieved_at (a value is never usable before we had it)")
        if self.backfilled and self.available_at != self.retrieved_at:
            raise ValueError("a backfilled row has available_at == retrieved_at (history is never presented as known live)")
        if self.kind in SYMBOL_KINDS and not self.symbol:
            raise ValueError(f"{self.kind} needs a symbol")
        if self.kind in SERIES_KINDS and not self.series_id:
            raise ValueError(f"{self.kind} needs a series_id")
        if self.status in (UNAVAILABLE, INSUFFICIENT_DATA) and self.payload:
            raise ValueError(f"a {self.status} observation carries no payload (a missing value is never a zero)")
        if self.status == STALE and self.freshness != STALE:
            raise ValueError("a STALE status has STALE freshness")
        if self.freshness == STALE and self.status not in (STALE, NOT_APPLICABLE):
            raise ValueError("a STALE value is never presented as usable")
        return self

    def usable_at(self, t: datetime) -> bool:
        """The point-in-time rule every consumer applies: a value exists for a decision at t only when available_at <= t."""
        return self.available_at <= _utc(t)


class FetchRecord(_M):
    """One request of the data layer to one provider (Stage 7.1+ store these beside the observations; Stage 7.0 fixes the shape)."""
    provider: str
    endpoint: str = Field(max_length=200)            # a path or a named endpoint, never a key or a secret
    started_at: datetime
    retrieved_at: Optional[datetime] = None
    status: str
    http_status: Optional[int] = Field(default=None, ge=100, le=599)
    latency_ms: Optional[int] = Field(default=None, ge=0)
    bytes: int = Field(default=0, ge=0)
    content_hash: Optional[str] = None
    retry_after_s: Optional[float] = Field(default=None, ge=0)
    error: str = Field(default="", max_length=300)

    @field_validator("started_at", "retrieved_at")
    @classmethod
    def _times(cls, v):
        return _utc(v)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v):
        return _one_of(v, PROVIDERS, "provider")

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        return _one_of(v, FETCH_STATUSES, "status")

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, v):
        if v is not None and not _HASH.match(v):
            raise ValueError("content_hash must be a lowercase sha256 hex digest")
        return v

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v):
        low = v.lower()
        if any(s in low for s in ("apikey", "api_key", "token=", "secret", "key=")):
            raise ValueError("an endpoint never carries a credential")
        return v

    @model_validator(mode="after")
    def _invariants(self):
        if self.retrieved_at is not None and self.retrieved_at < self.started_at:
            raise ValueError("retrieved_at may not be earlier than started_at")
        if self.status in ("OK", "NOT_MODIFIED") and self.retrieved_at is None:
            raise ValueError(f"a {self.status} fetch has a retrieved_at")
        return self
