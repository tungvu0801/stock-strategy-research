"""
strategy_registry/standard.py — STRATEGY STANDARD V1 (STRATEGY_STANDARD_V1.md): validation of the normalized strategy object.

validate_signal(obj, registry=None) -> StrategySignal, or StrategyStandardError(code, message, field):
  MISSING_FIELD · UNKNOWN_FIELD · EXECUTION_FIELD_FORBIDDEN (an order / quantity / broker / account key anywhere in the object) · INVALID_VALUE
  (enum, range, timestamp, empty condition) · SCORE_NOT_CALIBRATED (a probability claim without calibration) · STRATEGY_UNKNOWN /
  STRATEGY_NOT_IMPLEMENTED / STRATEGY_INACTIVE (with a registry: only an implemented strategy in an active lifecycle status may emit a signal, so a
  placeholder can never appear as a live signal) · SOURCE_MISMATCH (the signal's source differs from the registry entry's) · DIRECTION_NOT_SUPPORTED
  (a LONG-only strategy emitting SHORT, or the reverse).
A valid signal is evidence. Nothing here routes it anywhere: the adapter, the opportunity ranking, the proposal, the gate and the executor stay the
only path to a paper order, and this module imports none of them.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from pydantic import ValidationError

from strategy_registry import (ACTIVE_STATUSES, ASSET_CLASSES, FORBIDDEN_SIGNAL_KEYS, RISK_CLASSES, SCORE_KINDS, SCORE_MAX, SCORE_MIN, SIGNAL_DIRECTIONS, SOURCES,
                               STANDARD_VERSION, TIME_HORIZONS)
from strategy_registry.models import StrategyEntry, StrategySignal

REQUIRED_FIELDS = ("strategy_id", "source", "asset_class", "direction", "symbol", "score", "time_horizon", "entry_condition", "invalidation_condition",
                   "exit_condition", "risk_class", "generated_at", "version")
OPTIONAL_FIELDS = ("score_kind", "calibration", "evidence")
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

SCHEMA: Dict[str, Any] = {
    "version": STANDARD_VERSION,
    "required": list(REQUIRED_FIELDS),
    "optional": list(OPTIONAL_FIELDS),
    "forbidden_keys": sorted(FORBIDDEN_SIGNAL_KEYS),
    "enums": {"source": list(SOURCES), "asset_class": list(ASSET_CLASSES), "direction": list(SIGNAL_DIRECTIONS), "time_horizon": list(TIME_HORIZONS),
              "risk_class": list(RISK_CLASSES), "score_kind": list(SCORE_KINDS)},
    "score": {"min": SCORE_MIN, "max": SCORE_MAX, "default_kind": "RANK", "note": "score is NOT a probability unless score_kind is CALIBRATED_PROBABILITY with a documented calibration"},
    "rules": ["A signal is evidence: it names a symbol, a direction, conditions and a score; it never names an order, a quantity, a broker or an account.",
              "Only an implemented strategy in an active lifecycle status (PAPER_CANDIDATE, PAPER_ACTIVE, APPROVED) may emit a signal; a placeholder never does.",
              "No strategy places a broker order. The signal enters the Stock Agent Strategy Adapter, then Opportunity Ranking, Portfolio, Risk / Safety, Execution, Broker (Alpaca PAPER).",
              "generated_at is ISO 8601; version names the standard the object follows."],
}


class StrategyStandardError(ValueError):
    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _walk_keys(obj: Any, path: str = "") -> Iterable[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            yield here
            yield from _walk_keys(v, here)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


def forbidden_keys(obj: Any) -> list:
    """Every execution-shaped key in the object, at any depth (the leaf name is what counts)."""
    return [p for p in _walk_keys(obj) if p.rsplit(".", 1)[-1].split("[")[0].lower() in FORBIDDEN_SIGNAL_KEYS]


def validate_signal(obj: Any, registry: Optional[Dict[str, StrategyEntry]] = None) -> StrategySignal:
    if not isinstance(obj, dict):
        raise StrategyStandardError("INVALID_VALUE", "a strategy signal is a JSON object")
    bad = forbidden_keys(obj)
    if bad:
        raise StrategyStandardError("EXECUTION_FIELD_FORBIDDEN", f"a strategy signal never carries an execution field: {', '.join(bad)}", bad[0])
    missing = [f for f in REQUIRED_FIELDS if f not in obj]
    if missing:
        raise StrategyStandardError("MISSING_FIELD", f"missing required field(s): {', '.join(missing)}", missing[0])
    unknown = [k for k in obj if k not in ALL_FIELDS]
    if unknown:
        raise StrategyStandardError("UNKNOWN_FIELD", f"unknown field(s): {', '.join(unknown)} (optional facts belong under `evidence`)", unknown[0])
    try:
        sig = StrategySignal(**obj)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(x) for x in err.get("loc", ())) or None
        msg = str(err.get("msg", "invalid value")).replace("Value error, ", "")
        code = "SCORE_NOT_CALIBRATED" if "calibrat" in msg.lower() else "INVALID_VALUE"
        raise StrategyStandardError(code, msg, loc) from None
    if registry is not None:
        entry = registry.get(sig.strategy_id)
        if entry is None:
            raise StrategyStandardError("STRATEGY_UNKNOWN", f"{sig.strategy_id} is not in the strategy registry", "strategy_id")
        if not entry.implemented:
            raise StrategyStandardError("STRATEGY_NOT_IMPLEMENTED", f"{sig.strategy_id} is a placeholder ({entry.status}); it has no rules and emits no signal", "strategy_id")
        if entry.status not in ACTIVE_STATUSES:
            raise StrategyStandardError("STRATEGY_INACTIVE", f"{sig.strategy_id} is {entry.status}; only {', '.join(ACTIVE_STATUSES)} strategies emit signals", "strategy_id")
        if entry.source != sig.source:
            raise StrategyStandardError("SOURCE_MISMATCH", f"{sig.strategy_id} is registered as {entry.source}, the signal says {sig.source}", "source")
        if entry.direction_capability != "LONG_SHORT" and sig.direction != entry.direction_capability:
            raise StrategyStandardError("DIRECTION_NOT_SUPPORTED", f"{sig.strategy_id} is {entry.direction_capability} only, the signal says {sig.direction}", "direction")
    return sig


def check_signal(obj: Any, registry: Optional[Dict[str, StrategyEntry]] = None) -> Dict[str, Any]:
    """The same validation as a report: {valid, error, signal}. Never raises."""
    try:
        sig = validate_signal(obj, registry)
    except StrategyStandardError as exc:
        return {"valid": False, "error": exc.as_dict(), "signal": None}
    return {"valid": True, "error": None, "signal": sig.model_dump()}
