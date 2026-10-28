"""
decision_flow/conditions.py - one stored setup evaluation explained: its six READY checks, its distance from READY and its final decision
(pure; DESIGN_610C_DECISION_FLOW.md sections 3-4).

The setup engine computes no composite score: READY is the conjunction of six checks. Each check is recomputed from the stored inputs_json with
the engine's own read-only predicates (setups.engine.ppe_usable / demand_available / evidence_gate) and the setups/ constants, and the failing
codes are compared with the stored freshness_json.blockers ('consistent'); a mismatch is reported and the stored blockers stay authoritative.
SetupInputs forbids unknown keys and fills absent ones with defaults, so only the model's own keys are passed in, and a key the row did not
record makes the checks that read it 'not recorded' (passed None): a default is never shown as a recorded value. No I/O, no clock.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import ValidationError

from decision_flow import (AWAITING_EXECUTOR, BLOCKED, CHECK_IDS, CONFIDENCE_STATUS_TEXT, DECISION_LABELS, DETAIL_LABELS, DUPLICATE_ENTRY, ENTRIES_PER_CYCLE,
                           ENTRY_CONTROLS, EVALUATION_ERROR, INVALIDATED, NO_EVIDENCE_PACKET, NO_SETUP, NOT_AVAILABLE, ORDER_INTENT, PAPER_AUTO_OFF, PAUSED, READY,
                           READY_CHECKS, SCORE_TEXT, SETUP_WATCH, SKIPPED, STATE_ORDER, WAIT, WATCHING, text_for)
from setups import (INVALIDATE_DEMAND, INVALIDATE_DEMAND_EVALS, INVALIDATE_STRETCH, R_BLOCK_NO_EVIDENCE, R_CONFIDENCE_BELOW, R_DEMAND_BELOW, R_DEMAND_UNAVAILABLE,
                    R_NO_SETUP, R_PAUSED_STALE, R_PPE_COVERAGE_LOW, READY_CONFIDENCE, READY_DEMAND, READY_DEMAND_COVERAGE, READY_EXIT_DEMAND, READY_PPE_COVERAGE,
                    REASON_TEXT, WATCH_ENTER_STRETCH)
from setups.engine import demand_available, evidence_gate, ppe_usable
from setups.models import SetupInputs

EPS = 1e-9                                       # the engine's coverage tolerance (setups/engine.py)
FIELDS = tuple(SetupInputs.model_fields)
SPEC = {c["id"]: c for c in READY_CHECKS}
# the stored input keys each check reads
NEEDS = {"STRETCH": ("stretch", "stretch_status", "ppe_status"), "PPE_COVERAGE": ("ppe_coverage",), "CONFIDENCE": ("confidence", "confidence_status"),
         "DEMAND_DATA": ("demand", "demand_status", "demand_coverage", "demand_fresh"), "DEMAND": ("demand",),
         "EVIDENCE": ("evidence_available", "evidence_age_days", "evidence_cause", "evidence_strength", "guidance_down")}
VALUE_KEYS = ("stretch", "ppe_coverage", "confidence", "confidence_status", "demand", "demand_status", "demand_coverage")
STATE_RANK = {s: n for n, s in enumerate(STATE_ORDER)}
NOT_EVALUATED, NOT_RECORDED = "Not evaluated", "Not recorded"
# setups/engine.py, state READY: the episode stays READY until one of these (the entry checks above are not re-required)
HOLD_TEXT = (f"The engine keeps READY until stretch rises above {INVALIDATE_STRETCH:g}, Market Demand falls below {READY_EXIT_DEMAND} (below "
             f"{INVALIDATE_DEMAND} for {INVALIDATE_DEMAND_EVALS} evaluations invalidates), the demand data becomes unavailable or the evidence gate fails.")
_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")


def loads(value: Any, default: Any) -> Any:
    """A stored JSON column as Python (an already-parsed value passes through; unreadable text is the default)."""
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def inputs_from(stored: Any, symbol: str = "", evaluated_at: str = "") -> Tuple[Optional[SetupInputs], Set[str]]:
    """SetupInputs from a stored inputs_json - only the model's keys (it forbids others) - and the model keys the row did not record.
    The row's own symbol / evaluated_at columns stand in for those two keys; None when nothing readable was stored (an ERROR row stores {})."""
    d = loads(stored, {})
    if not isinstance(d, dict) or not d:
        return None, set(FIELDS)
    known = {k: v for k, v in d.items() if k in FIELDS}
    known.setdefault("symbol", symbol)
    known.setdefault("evaluated_at", evaluated_at)
    try:
        return SetupInputs(**known), {k for k in FIELDS if k not in known}
    except ValidationError:
        return None, set(FIELDS)


def error_type(reason_text: Any) -> str:
    """The exception class of an ERROR evaluation (stored as '<Type>: message'). Only the type is shown: the message can carry a host name."""
    head = str(reason_text or "").split(":", 1)[0].strip()
    return head if _ERROR_TYPE.match(head) else "error"


def _num(v: Any, digits: int) -> str:
    return f"{float(v):.{digits}f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else NOT_AVAILABLE


def _check(cid: str, value: Any = None, value_text: str = NOT_EVALUATED, passed: Optional[bool] = None, gap: Any = None, detail: str = "",
           code: Optional[str] = None, root_cause: Optional[str] = None) -> Dict[str, Any]:
    s = SPEC[cid]
    return {"id": cid, "label": s["label"], "value": value, "value_text": value_text, "threshold_text": s["threshold_text"], "passed": passed, "gap": gap,
            "detail": detail, "code": code if passed is False else None, "root_cause": root_cause}


def _unrecorded(cid: str, keys: List[str]) -> Dict[str, Any]:
    return _check(cid, None, NOT_RECORDED, None, None, f"Not recorded in this evaluation's stored inputs ({', '.join(keys)}).")


def checks(i: SetupInputs, missing: Set[str] = frozenset()) -> Tuple[List[Dict[str, Any]], Optional[List[str]]]:
    """The six checks in the engine's order (setups/engine.py evaluate) and the blocker codes they produce, one per failed check
    (None when a key a check needs was not recorded, so the list cannot be compared)."""
    out: Dict[str, Dict[str, Any]] = {}
    codes: List[str] = []
    complete = True

    def gone(cid: str) -> List[str]:
        return sorted(k for k in NEEDS[cid] if k in missing)

    # 1. downside stretch (PPE): stretch <= -40, inclusive
    if gone("STRETCH"):
        out["STRETCH"], complete = _unrecorded("STRETCH", gone("STRETCH")), False
    elif not ppe_usable(i):
        out["STRETCH"], complete = _check("STRETCH", i.stretch, _num(i.stretch, 1), None, None,
                                          "Not evaluated: PPE unavailable (the engine pauses such an evaluation)."), False
    else:
        s = float(i.stretch)
        ok = s <= WATCH_ENTER_STRETCH
        out["STRETCH"] = _check("STRETCH", s, f"{s:.1f}", ok, round(max(0.0, s - WATCH_ENTER_STRETCH), 1), "Met" if ok else REASON_TEXT[R_NO_SETUP], R_NO_SETUP)
        codes += [] if ok else [R_NO_SETUP]
    # 2. PPE coverage >= 0.60
    if gone("PPE_COVERAGE"):
        out["PPE_COVERAGE"], complete = _unrecorded("PPE_COVERAGE", gone("PPE_COVERAGE")), False
    else:
        cov = float(i.ppe_coverage)
        ok = cov + EPS >= READY_PPE_COVERAGE
        out["PPE_COVERAGE"] = _check("PPE_COVERAGE", cov, f"{cov:.2f}", ok, 0.0 if ok else round(READY_PPE_COVERAGE - cov, 2),
                                     "Met" if ok else REASON_TEXT[R_PPE_COVERAGE_LOW], R_PPE_COVERAGE_LOW)
        codes += [] if ok else [R_PPE_COVERAGE_LOW]
    # 3. PPE confidence: status AVAILABLE and >= 40 - anything else is Not available with its cause, never 'below 40'
    if gone("CONFIDENCE"):
        out["CONFIDENCE"], complete = _unrecorded("CONFIDENCE", gone("CONFIDENCE")), False
    elif i.confidence_status == "AVAILABLE" and i.confidence is not None:
        c = int(i.confidence)
        ok = c >= READY_CONFIDENCE
        out["CONFIDENCE"] = _check("CONFIDENCE", c, str(c), ok, max(0, READY_CONFIDENCE - c), "Met" if ok else REASON_TEXT[R_CONFIDENCE_BELOW], R_CONFIDENCE_BELOW)
        codes += [] if ok else [R_CONFIDENCE_BELOW]
    else:
        cause = CONFIDENCE_STATUS_TEXT.get(i.confidence_status) or ("no value recorded" if i.confidence_status == "AVAILABLE" else f"status {i.confidence_status}")
        out["CONFIDENCE"] = _check("CONFIDENCE", None, f"Not available ({cause})", False, None, f"Not available: {cause}.", R_CONFIDENCE_BELOW)
        codes.append(R_CONFIDENCE_BELOW)
    # 4. Market Demand data: available, coverage >= 0.60, fresh;  5. Market Demand >= 60, only when the data check passed
    if gone("DEMAND_DATA"):
        out["DEMAND_DATA"], complete = _unrecorded("DEMAND_DATA", gone("DEMAND_DATA")), False
        out["DEMAND"] = _unrecorded("DEMAND", gone("DEMAND") or gone("DEMAND_DATA"))
    else:
        avail = demand_available(i)
        why = ([f"status {i.demand_status}"] if i.demand_status not in ("AVAILABLE", "SIMULATED_DATA") else []) + (["no score"] if i.demand is None else []) \
            + ([f"coverage {float(i.demand_coverage):.2f} below {READY_DEMAND_COVERAGE:.2f}"] if float(i.demand_coverage) + EPS < READY_DEMAND_COVERAGE else []) \
            + ([] if i.demand_fresh else ["stale"])
        text = f"{i.demand_status}, coverage {float(i.demand_coverage):.2f}, {'fresh' if i.demand_fresh else 'stale'}"
        out["DEMAND_DATA"] = _check("DEMAND_DATA", i.demand_status, text, avail, None,
                                    "Met" if avail else f"{REASON_TEXT[R_DEMAND_UNAVAILABLE].rstrip('.')} ({'; '.join(why)}).", R_DEMAND_UNAVAILABLE)
        if not avail:
            codes.append(R_DEMAND_UNAVAILABLE)
            out["DEMAND"] = _check("DEMAND", i.demand, str(i.demand) if i.demand is not None else NOT_AVAILABLE, None, None,
                                   "Not evaluated: the Market Demand data check did not pass.")
        else:
            d = int(i.demand)
            ok = d >= READY_DEMAND
            out["DEMAND"] = _check("DEMAND", d, str(d), ok, max(0, READY_DEMAND - d), "Met" if ok else REASON_TEXT[R_DEMAND_BELOW], R_DEMAND_BELOW)
            codes += [] if ok else [R_DEMAND_BELOW]
    # 6. Stage 5.4 evidence: the engine's evidence gate (its first applicable code)
    if gone("EVIDENCE"):
        out["EVIDENCE"], complete = _unrecorded("EVIDENCE", gone("EVIDENCE")), False
    else:
        gate = evidence_gate(i)
        if not i.evidence_available:
            text = "No stored COMPLETED packet"
        else:
            age = f"{i.evidence_age_days} day(s) old" if i.evidence_age_days is not None else "age not recorded"
            text = f"COMPLETED packet, {age}, cause {i.evidence_cause or 'n/a'}, strength {i.evidence_strength or 'n/a'}"
        out["EVIDENCE"] = _check("EVIDENCE", bool(i.evidence_available), text, gate is None, None, "Met" if gate is None else REASON_TEXT.get(gate, gate), gate)
        codes += [gate] if gate else []
    # one root cause behind two checks: confidence unavailable for lack of evidence AND the evidence gate failing for the same reason
    conf, ev = out["CONFIDENCE"], out["EVIDENCE"]
    if conf["passed"] is False and ev["passed"] is False and i.confidence_status == "EVIDENCE_NOT_AVAILABLE" and ev["code"] == R_BLOCK_NO_EVIDENCE:
        conf["root_cause"] = ev["root_cause"] = NO_EVIDENCE_PACKET
    return [out[c] for c in CHECK_IDS], (codes if complete else None)


def distance(cs: List[Dict[str, Any]], not_evaluated: Optional[str] = None, unrecorded: int = 0, held: bool = False) -> Dict[str, Any]:
    """Distance from READY: unmet checks, distinct root causes, and the numeric gaps where the values exist. No scalar composite. held: the stored
    state is READY, so the six checks are the WATCH -> READY entry checks and the engine's own exit thresholds decide whether READY holds."""
    if not_evaluated is not None:
        return {"unmet": None, "unmet_root_causes": None, "checks": len(cs), "stretch_gap": None, "demand_gap": None, "confidence_gap": None, "text": not_evaluated}
    by = {c["id"]: c for c in cs}
    failed = [c for c in cs if c["passed"] is False]
    roots = {c["root_cause"] or c["id"] for c in failed}
    sg, dg, cg = by["STRETCH"]["gap"], by["DEMAND"]["gap"], by["CONFIDENCE"]["gap"]
    if not failed and not unrecorded:
        text = f"All {len(cs)} READY checks met."
    else:
        parts = ([f"stretch {sg:.1f} points above {WATCH_ENTER_STRETCH:g}"] if sg else []) + ([f"confidence {cg} below {READY_CONFIDENCE}"] if cg else []) \
            + ([f"demand {dg} below {READY_DEMAND}"] if dg else [])
        head = f"READY held: {len(failed)} of {len(cs)} entry checks (WATCH to READY) not met now" if held else f"{len(failed)} of {len(cs)} READY checks unmet"
        text = head + (f" ({len(roots)} distinct causes)" if len(roots) != len(failed) else "") \
            + (f"; {unrecorded} not recorded" if unrecorded else "") + (": " + "; ".join(parts) if parts else "") + "." + (f" {HOLD_TEXT}" if held else "")
    return {"unmet": len(failed), "unmet_root_causes": len(roots), "checks": len(cs), "stretch_gap": sg, "demand_gap": dg, "confidence_gap": cg, "text": text}


def not_evaluated_text(decision: Optional[str], reason_code: Optional[str], reason_text: Any, freshness: Dict[str, Any], inputs: Dict[str, Any]) -> str:
    """The pause / error text shown on every check of a PAUSED or ERROR evaluation."""
    if decision == "ERROR":
        return f"{text_for(EVALUATION_ERROR).rstrip('.')} ({error_type(reason_text)})."
    if reason_code == R_PAUSED_STALE:
        stale = [str(s) for s in (freshness.get("blockers") or inputs.get("stale_reasons") or [])]
        extra = "; ".join(text_for(s).rstrip(".") for s in stale)
        return REASON_TEXT[R_PAUSED_STALE].rstrip(".") + (f" ({extra})." if extra else ".")
    return REASON_TEXT.get(reason_code or "") or text_for(PAUSED)


def setup_reason_text(decision: Optional[str], reason_code: Optional[str], reason_text: Any, inputs: Dict[str, Any]) -> str:
    """The evaluation's reason as the engine words it (REASON_TEXT, verbatim) - except where that wording would be false: confidence that was
    not available is never 'below 40', and an error shows only its exception type."""
    if decision == "ERROR":
        return f"{text_for(EVALUATION_ERROR).rstrip('.')} ({error_type(reason_text)})."
    if reason_code == R_CONFIDENCE_BELOW and (inputs.get("confidence_status") != "AVAILABLE" or inputs.get("confidence") is None):
        st = inputs.get("confidence_status")
        cause = CONFIDENCE_STATUS_TEXT.get(st) or ("not recorded" if st is None else "no value recorded" if st == "AVAILABLE" else f"status {st}")
        return f"PPE confidence not available ({cause})."
    return REASON_TEXT.get(reason_code or "") or text_for(reason_code)


def explain(row: Dict[str, Any]) -> Dict[str, Any]:
    """One setup_evaluations row (its stored columns; the JSON columns as text or parsed) as values, checks, distance, score and the cross-check."""
    raw = loads(row.get("inputs_json", row.get("inputs")), {})
    raw = raw if isinstance(raw, dict) else {}
    fresh = loads(row.get("freshness_json", row.get("freshness")), {})
    fresh = fresh if isinstance(fresh, dict) else {}
    stored = [str(b) for b in fresh.get("blockers")] if isinstance(fresh.get("blockers"), list) else []
    values = {k: raw.get(k) for k in VALUE_KEYS}
    decision = row.get("decision")
    i, missing = inputs_from(raw, str(row.get("symbol") or ""), str(row.get("evaluated_at") or ""))
    if decision in ("PAUSED", "ERROR") or i is None:
        text = not_evaluated_text(decision, row.get("reason_code"), row.get("reason_text"), fresh, raw) if decision in ("PAUSED", "ERROR") \
            else "Not evaluated: the stored inputs of this evaluation could not be read."
        cs = [_check(c, None, NOT_EVALUATED, None, None, text) for c in CHECK_IDS]
        return {"evaluated": False, "values": values, "checks": cs, "passed": [], "failed": [], "distance": distance(cs, text), "score": score(),
                "blockers": stored, "recomputed": None, "consistent": None, "note": text}
    cs, codes = checks(i, missing)
    consistent: Optional[bool] = None if codes is None else codes == stored
    note = None
    if {"ppe_status", "stretch", "stretch_status", "ppe_fresh"} <= set(raw) and not (ppe_usable(i) and i.ppe_fresh):
        consistent, note = False, "The stored inputs would have paused this evaluation; the stored decision and blockers stay authoritative."
    elif consistent is False:
        note = (f"The recomputed checks ({', '.join(codes or []) or 'none failing'}) differ from the stored blockers ({', '.join(stored) or 'none'}); "
                "the stored blockers stay authoritative.")
    unrecorded = sum(1 for c in cs if c["value_text"] == NOT_RECORDED and c["passed"] is None)
    return {"evaluated": True, "values": values, "checks": cs, "passed": [c["label"] for c in cs if c["passed"] is True],
            "failed": [c["label"] for c in cs if c["passed"] is False], "distance": distance(cs, unrecorded=unrecorded, held=row.get("resulting_state") == "READY"),
            "score": score(), "blockers": stored,
            "recomputed": codes, "consistent": consistent, "note": note}


def score() -> Dict[str, Any]:
    return {"value": None, "text": SCORE_TEXT}


def stored_distance(blockers: Any, confidence_status: Optional[str], stretch: Any, demand: Any) -> Dict[str, Any]:
    """The distance from the STORED blockers (one code per failed check; what history reads in bulk), with the same root-cause rule and
    the numeric gaps where the values exist."""
    b = [str(x) for x in blockers] if isinstance(blockers, list) else []
    pair = R_CONFIDENCE_BELOW in b and R_BLOCK_NO_EVIDENCE in b and confidence_status == "EVIDENCE_NOT_AVAILABLE"
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    sg = round(max(0.0, float(stretch) - WATCH_ENTER_STRETCH), 1) if num(stretch) else None
    dg = max(0, READY_DEMAND - int(demand)) if num(demand) and R_DEMAND_UNAVAILABLE not in b else None
    return {"unmet": len(b), "unmet_root_causes": len(b) - 1 if pair else len(b), "stretch_gap": sg, "demand_gap": dg}


def closest_key(state: Optional[str], unmet: Optional[int], stretch_gap: Any, demand_gap: Any, symbol: Optional[str]) -> tuple:
    """Closest to READY (a display ordering of persisted values, not a score): state, fewest unmet, smallest stretch gap, smallest demand gap, symbol."""
    inf = float("inf")
    return (STATE_RANK.get(state or "NONE", len(STATE_ORDER)), unmet if unmet is not None else 99, stretch_gap if stretch_gap is not None else inf,
            demand_gap if demand_gap is not None else inf, symbol or "")


def decide(evaluation: Optional[str], state: Optional[str], paper_auto: bool, executor: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """The final decision of one evaluation (DESIGN_610C section 3). For a READY setup the executor's own result in the cycle's window wins over
    the mode at the evaluation: it is what happened."""
    res = (executor or {}).get("result")
    if state == "READY" and res == "ENTRY_INTENT":                    # the executor's result first: a paused or failed evaluation keeps its stored
        d, x = READY, ORDER_INTENT                                    # READY episode, and the executor still acted on it
    elif state == "READY" and res == "BLOCKED":
        d, x = BLOCKED, DUPLICATE_ENTRY if list((executor or {}).get("blocked_by") or []) == ["DUPLICATE_ENTRY"] else ENTRY_CONTROLS
    elif state == "READY" and res == "SKIPPED":
        d, x = SKIPPED, ENTRIES_PER_CYCLE
    elif evaluation == "ERROR":
        d, x = WAIT, EVALUATION_ERROR
    elif evaluation == "PAUSED":
        d, x = WAIT, PAUSED
    elif state == "READY":
        if not paper_auto:
            d, x = SKIPPED, PAPER_AUTO_OFF
        else:
            d, x = READY, AWAITING_EXECUTOR
    elif state == "WATCH":
        d, x = WATCHING, SETUP_WATCH
    elif state == "INVALIDATED":
        d, x = WAIT, INVALIDATED
    else:
        d, x = WAIT, NO_SETUP
    return {"decision": d, "decision_detail": x, "decision_label": f"{DECISION_LABELS[d]} · {DETAIL_LABELS[x]}"}
