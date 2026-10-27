"""
forward_capture/project.py - ALLOWLIST projections (Stage 6.10A). Everything the capture persists or shows passes through one of these functions:
plain JSON-able dicts built from an explicit list of keys, so a new field upstream can never leak in. In particular:
  - other (read-only, user-managed) accounts: only {source, account_type, state, refreshed_at, age_s} per source and the exposure gate's verdict
    by reason code - never a holding, a quantity, a value, an equity, a cash figure or the gate's free text (DESIGN_69 line 42);
  - the Alpaca PAPER account: equity / cash / buying power / status only - never an account number (masked or not) or a credential.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from forward_capture import GATE_LABELS

ACCOUNT_KEYS = ("equity", "cash", "buying_power", "status", "blocked", "currency", "pattern_day_trader")
ENV_KEYS = ("paper", "label", "simulated", "status", "blocked", "checked_at", "error")   # paper proof without the host string (exports never carry it)
SOURCE_KEYS = ("source", "account_type", "state", "refreshed_at", "age_s")
GATE_KEYS = ("result", "reason_code", "unified_state", "snapshot_at", "stale", "threshold_pct", "policy", "enabled", "planned_notional")
PROPOSAL_KEYS = ("proposal_id", "setup_id", "status", "effective_status", "reference_price", "price_source", "invalidation_price", "stop_rule", "risk_per_share",
                 "risk_budget", "equity", "equity_source", "final_qty", "created_at", "expires_at")
INTENT_DECISION_KEYS = ("execution_id", "intent_type", "symbol", "side", "qty", "client_order_id", "proposal_id", "setup_id", "position_id", "exit_cycle",
                        "exit_reason", "reference_price", "invalidation_price", "risk_budget", "proposal_qty", "created_at", "created_by")
GATE_CONTROL = "UNIFIED_EXTERNAL_EXPOSURE"
_BANNED_SUBSTRINGS = ("account_number", "secret", "token", "password", "api_key", "apikey")
_DROPPED_KEYS = ("account", "host")                       # the masked account id (BROKER_ACCOUNT input) and the paper host string (never in an export)


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _get(obj: Any, key: str):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def pick(obj: Any, keys: Iterable[str]) -> Dict[str, Any]:
    if obj is None:
        return {}
    out = {}
    for k in keys:
        v = _get(obj, k)
        if v is not None and not hasattr(v, "__dict__"):
            out[k] = v if isinstance(v, (str, int, float, bool)) else json.loads(canonical(v))
    return out


def scrub(value: Any, depth: int = 0) -> Any:
    """A JSON-able copy without any key that could identify an account or carry a credential (defence in depth behind the allowlists)."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k): scrub(v, depth + 1) for k, v in value.items() if not any(b in str(k).lower() for b in _BANNED_SUBSTRINGS) and str(k) not in _DROPPED_KEYS}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return json.loads(canonical(value))
    except (TypeError, ValueError):
        return str(value)


def account(d: Optional[dict]) -> Dict[str, Any]:
    return pick(d, ACCOUNT_KEYS)


def environment(d: Optional[dict]) -> Dict[str, Any]:
    return pick(d, ENV_KEYS)


def sources(unified: Optional[dict]) -> List[Dict[str, Any]]:
    return [pick(s, SOURCE_KEYS) for s in ((unified or {}).get("sources") or []) if isinstance(s, dict)]


def external(unified: Optional[dict]) -> Dict[str, Any]:
    """The unified portfolio as evidence context: its state and each source's state / freshness. Never rows, exposure, equity or warnings."""
    u = unified if isinstance(unified, dict) else {}
    return {"unified_state": u.get("state") or "UNAVAILABLE", "sources": sources(u)}


def gate_verdict(ev: Any) -> Dict[str, Any]:
    """The UNIFIED_EXTERNAL_EXPOSURE control as a verdict: result, reason code, a fixed label, the threshold and the data state."""
    inp = _get(ev, "input") or {}
    v = pick(inp, GATE_KEYS)
    v["result"] = _get(ev, "result") or v.get("result")
    v["control_id"] = GATE_CONTROL
    v["label"] = GATE_LABELS.get(v.get("reason_code"), "")
    return v                                       # never the projected share: with the planned notional it would give the other accounts' invested value back


def control(c: Any) -> Dict[str, Any]:
    """One entry control (ControlEvaluation) without anything that identifies an account; the exposure gate reduced to its verdict."""
    cid = _get(c, "control_id")
    if cid == GATE_CONTROL:
        v = gate_verdict(c)
        return {"control_id": cid, "result": v.get("result"), "reason": v.get("label"), "input": {k: v[k] for k in v if k not in ("control_id", "result", "label")}}
    return {"control_id": cid, "result": _get(c, "result"), "reason": str(_get(c, "reason") or "")[:300], "input": scrub(_get(c, "input") or {})}


def controls(cs: Iterable[Any]) -> List[Dict[str, Any]]:
    return [control(c) for c in cs or []]


def proposal(pv: Any) -> Dict[str, Any]:
    out = pick(pv, PROPOSAL_KEYS)
    el = _get(pv, "execution_eligibility") or {}
    if isinstance(el, dict) and el:
        out["execution_eligibility"] = pick(el, ("allowed", "state", "reason_codes"))
    return out


def setup(sv: Any) -> Dict[str, Any]:
    inputs = _get(sv, "inputs")
    if inputs is not None and hasattr(inputs, "model_dump"):
        inputs = inputs.model_dump()
    return {"setup_id": _get(sv, "setup_id"), "state": _get(sv, "state"), "cycle_id": _get(sv, "cycle_id"), "started_at": _get(sv, "started_at"), "since": _get(sv, "since"),
            "evaluated_at": (inputs or {}).get("evaluated_at") if isinstance(inputs, dict) else None, "inputs": scrub(inputs) if isinstance(inputs, dict) else None}


def safety(sv: Any) -> Dict[str, Any]:
    out = pick(sv, ("state", "allowed_new_entry", "evaluated_at"))
    codes = _get(sv, "reason_codes")
    if codes is not None:
        out["reason_codes"] = list(codes)
    return out


def clock(c: Optional[dict]) -> Dict[str, Any]:
    return pick(c, ("is_open", "next_open", "next_close", "timestamp"))


def entry_controls_record(rec: Optional[dict]) -> Dict[str, Any]:
    """The Stage 6.9.1 entry-controls record of an intent (version, contract hash, gate verdict) - already free of holdings; re-picked anyway."""
    rec = rec if isinstance(rec, dict) else {}
    g = rec.get("unified_external_exposure") if isinstance(rec.get("unified_external_exposure"), dict) else {}
    return {"version": rec.get("version"), "contract_hash": rec.get("contract_hash"),
            "unified_external_exposure": pick(g, ("enabled", "enforced", "result", "reason_code", "unified_state", "snapshot_at", "threshold_pct", "policy"))}


def intent_at_decision(it: Any) -> Dict[str, Any]:
    """An intent as it was decided: only the columns that never change after insert (no state, fill, broker id or failure)."""
    out = pick(it, INTENT_DECISION_KEYS)
    snap = _get(it, "proposal_snapshot")
    if isinstance(snap, dict):
        out["proposal_snapshot"] = pick(snap, PROPOSAL_KEYS)
    env = _get(it, "environment")
    if isinstance(env, dict):
        out["environment"] = environment(env)
        out["entry_controls"] = entry_controls_record(env.get("entry_controls"))
    return out
