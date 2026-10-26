"""strategy_validation/candidates.py — candidate generation happens ONCE. Every raw signal becomes an immutable StrategyCandidate with a content
identity (candidate_id = sha256 of strategy, version, symbol, direction, signal date, entry / invalidation reference and the feature snapshot hash),
or an explicit SkippedCandidate with its reason. A missing or non-finite critical feature is never turned into zero: the signal is skipped and
counted (candidate_skipped_missing_data). Every candidate must also form a valid Strategy Standard v1 signal (no execution field anywhere)."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Tuple

from strategy_validation import DIRECTIONS
from strategy_validation.models import RawCandidate, SkippedCandidate, StrategyCandidate


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not math.isnan(float(x)) and not math.isinf(float(x))


def is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return True
    return False


def feature_hash(features: Dict[str, Any], feature_as_of: Dict[str, str]) -> str:
    blob = json.dumps({"features": features, "as_of": feature_as_of}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def candidate_identity(strategy_id: str, strategy_version: str, symbol: str, direction: str, signal_date: str, entry_reference: float,
                       invalidation_reference, feature_snapshot_hash: str) -> str:
    inv = "NONE" if invalidation_reference is None else round(float(invalidation_reference), 8)    # NONE: a stop-less strategy (Stage 6.4)
    blob = json.dumps([strategy_id, strategy_version, symbol, direction, signal_date, round(float(entry_reference), 8), inv,
                       feature_snapshot_hash], separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def candidate_set_hash(ids: Iterable[str]) -> str:
    return hashlib.sha256("|".join(sorted(ids)).encode("utf-8")).hexdigest()[:32]


def standard_signal(adapter, raw: RawCandidate) -> dict:
    """The Strategy Standard v1 object this candidate corresponds to (validated, never routed anywhere)."""
    return {"strategy_id": adapter.strategy_id, "source": getattr(adapter, "source", "INTERNAL"), "asset_class": "US_EQUITY", "direction": raw.direction, "symbol": raw.symbol,
            "score": raw.score, "time_horizon": adapter.time_horizon, "entry_condition": adapter.entry_condition, "invalidation_condition": adapter.invalidation_condition,
            "exit_condition": adapter.exit_condition, "risk_class": adapter.risk_class, "generated_at": f"{raw.signal_date}T16:00:00-04:00", "version": "strategy_standard_v1",
            "evidence": {"features": raw.features, "feature_as_of": raw.feature_as_of}}


def research_registry_error(adapter, raw: RawCandidate, registry) -> str:
    """The registry checks of the Strategy Standard that apply to a RESEARCH replay: the strategy is registered and implemented, the source and the
    direction match. The live-emission rule (only PAPER_CANDIDATE / PAPER_ACTIVE / APPROVED strategies emit LIVE signals) is deliberately not applied:
    a strategy is validated historically BEFORE any promotion (Stage 6.4 found the harness applying it, which made every RESEARCH strategy
    unvalidatable)."""
    if registry is None:
        return ""
    entry = registry.get(adapter.strategy_id)
    if entry is None:
        return f"STRATEGY_UNKNOWN: {adapter.strategy_id} is not in the strategy registry"
    if not entry.implemented:
        return f"STRATEGY_NOT_IMPLEMENTED: {adapter.strategy_id} is a placeholder"
    if entry.source != getattr(adapter, "source", "INTERNAL"):
        return f"SOURCE_MISMATCH: registered as {entry.source}"
    if entry.direction_capability != "LONG_SHORT" and raw.direction != entry.direction_capability:
        return f"DIRECTION_NOT_SUPPORTED: {adapter.strategy_id} is {entry.direction_capability} only"
    return ""


def normalize(adapter, raws: Iterable[RawCandidate], dataset, registry=None) -> Tuple[Tuple[StrategyCandidate, ...], List[SkippedCandidate]]:
    from strategy_registry.standard import check_signal
    out: List[StrategyCandidate] = []
    skipped: List[SkippedCandidate] = []
    seen = set()
    for raw in raws:
        sym = str(raw.symbol).upper()
        base = {"symbol": sym, "signal_date": raw.signal_date}
        if raw.direction not in DIRECTIONS:
            skipped.append(SkippedCandidate(**base, reason="UNSUPPORTED_DIRECTION", detail=f"{raw.direction}: Stage 6.2 simulates long candidates only"))
            continue
        missing = [f for f in adapter.critical_features if is_missing(raw.features.get(f))]
        stopless = getattr(adapter, "stop_policy", "INVALIDATION") == "NONE"          # Stage 6.4: a periodic rebalance strategy has no stop
        if raw.entry_reference is None or not _num(raw.entry_reference):
            missing.append("entry_reference")
        if not stopless and (raw.invalidation_reference is None or not _num(raw.invalidation_reference)):
            missing.append("invalidation_reference")
        if missing:
            skipped.append(SkippedCandidate(**base, reason="MISSING_CRITICAL_FEATURE", detail=", ".join(missing)))
            continue
        if dataset.bar(sym, raw.signal_date) is None:
            skipped.append(SkippedCandidate(**base, reason="SIGNAL_BAR_NOT_IN_DATASET", detail="the signal session is not in the dataset for this symbol"))
            continue
        if stopless and raw.invalidation_reference is not None:
            skipped.append(SkippedCandidate(**base, reason="INVALID_STOP", detail="the strategy declares no stop (stop_policy NONE) but the candidate carries one"))
            continue
        if not stopless and float(raw.invalidation_reference) >= float(raw.entry_reference):
            skipped.append(SkippedCandidate(**base, reason="INVALID_STOP", detail="the invalidation reference is not below the entry reference (long)"))
            continue
        hold = raw.max_hold_bars if raw.max_hold_bars is not None else adapter.max_hold_bars
        if not isinstance(hold, int) or isinstance(hold, bool) or hold < 1:
            skipped.append(SkippedCandidate(**base, reason="INVALID_HOLD", detail=f"max_hold_bars {hold!r} is not a positive integer"))
            continue
        chk = check_signal(standard_signal(adapter, raw), None)                  # the schema: no execution field anywhere, valid enums, a rank score
        reg = research_registry_error(adapter, raw, registry) if chk["valid"] else ""
        if not chk["valid"] or reg:
            skipped.append(SkippedCandidate(**base, reason="STANDARD_V1_INVALID", detail=reg or f"{chk['error']['code']}: {chk['error']['message']}"))
            continue
        fh = feature_hash(raw.features, raw.feature_as_of)
        cid = candidate_identity(adapter.strategy_id, adapter.strategy_version, sym, raw.direction, raw.signal_date, raw.entry_reference, raw.invalidation_reference, fh)
        if cid in seen:
            skipped.append(SkippedCandidate(**base, reason="DUPLICATE_CANDIDATE", detail="an identical candidate was already generated", candidate_id=cid))
            continue
        seen.add(cid)
        out.append(StrategyCandidate(candidate_id=cid, strategy_id=adapter.strategy_id, strategy_version=adapter.strategy_version, symbol=sym, direction=raw.direction,
                                     signal_date=raw.signal_date, signal_basis=adapter.signal_basis, entry_reference=float(raw.entry_reference),
                                     invalidation_reference=None if raw.invalidation_reference is None else float(raw.invalidation_reference),
                                     target_reference=None if raw.target_reference is None else float(raw.target_reference),
                                     max_hold_bars=int(hold), entry_trade=bool(raw.entry_trade), exit_trade=bool(raw.exit_trade), score=raw.score, feature_snapshot_hash=fh, features=dict(raw.features), feature_as_of=dict(raw.feature_as_of)))
    out.sort(key=lambda c: (c.signal_date, c.symbol, c.candidate_id))
    return tuple(out), skipped
