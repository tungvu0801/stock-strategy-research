"""
market_scan/filters.py - Stage 7.5: the quality gate, the shared eligibility filters and the gap-list order, PURE (DESIGN_75 section 6;
master design D.1 "Eligibility filters").

The gate (operator decision at Stage 7.5): the Stage 7.4 report runs on the same store snapshot at the same T.
* BLOCK             -> nothing is eligible (QUALITY_BLOCK). A favourable feature never outweighs it: the gate is checked first, per symbol;
* INSUFFICIENT_DATA -> nothing is eligible (QUALITY_INSUFFICIENT_DATA): an unestablished store is never an opportunity;
* WARN              -> the scan proceeds with the warning codes attached, and every symbol still passes its own input checks;
* PASS              -> the scan proceeds.

Eligibility (D.1, never looser than the project's MIN_PRICE 5): price >= $5; the halt flag; every REQUIRED input AVAILABLE (FRESH / AGING)
under the Stage 7.0 policy. Required inputs per horizon:
* INTRADAY: the latest quote snapshot (QUOTE_DAY: <= 5 s FRESH, <= 15 s AGING, regular session only), and a halt flag of FALSE - a TRUE
  flag or an UNKNOWN one (the halt feed is not current) blocks: HALT_FEED fails closed for every day candidate;
* SWING: the last completed session's daily bar (the 7.0 daily-bar rule), its close as the price, and a halt flag that is not TRUE (an
  UNKNOWN halt feed leaves swing unaffected: C.5 / C.13).
A symbol that fails a filter is recorded with every reason, never dropped.

Sequence (operator decision at Stage 7.5): INTRADAY - the gap list: eligible symbols with a usable gap_pct by gap_pct descending, ties by
symbol, rank = position; SWING - symbol order, no rank. No score, no composite, nothing ranked across horizons.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from market_scan import ELIGIBLE, FALSE, INELIGIBLE, INTRADAY, MIN_PRICE, NOT_APPLICABLE, SWING, TRUE

PASS, WARN, BLOCK, INSUFFICIENT = "PASS", "WARN", "BLOCK", "INSUFFICIENT_DATA"     # the Stage 7.4 verdicts


def quality_gate(verdict: Optional[str], *, warnings: Sequence[dict] = ()) -> Dict[str, Any]:
    """The run-level gate from the Stage 7.4 verdict (None: the report could not run - never a pass)."""
    if verdict == BLOCK:
        return {"verdict": verdict, "actionable": False, "codes": ["QUALITY_BLOCK"]}
    if verdict in (PASS, WARN):
        return {"verdict": verdict, "actionable": True, "codes": ["QUALITY_WARN"] if verdict == WARN else [],
                "warnings": [{"check": w["check"], "codes": list(w["codes"])} for w in warnings]}
    return {"verdict": verdict, "actionable": False, "codes": ["QUALITY_INSUFFICIENT_DATA" if verdict == INSUFFICIENT else "QUALITY_NOT_ESTABLISHED"]}


def _price(reasons: List[str], price: Dict[str, Any]) -> None:
    if price["value"] is None:
        reasons.append("PRICE_UNAVAILABLE")
    elif price["value"] < MIN_PRICE:
        reasons.append("PRICE_BELOW_MIN")


def intraday(sym: Dict[str, Any], gate: Dict[str, Any], *, window: Tuple[bool, str]) -> Dict[str, Any]:
    """`sym`: the symbol's inputs and features (run.py); `window`: (the day horizon applies at T, why not)."""
    applies, why = window
    if not applies:
        return {"state": NOT_APPLICABLE, "eligible": False, "reasons": [why], "rank": None}
    reasons: List[str] = [] if gate["actionable"] else list(gate["codes"])
    q = sym["quote"]
    if not q["present"]:
        reasons.append("QUOTE_NEVER_OBSERVED")
    elif not q["usable_is_latest"]:
        reasons.append(f"QUOTE_{q['reason']}")
    elif not q["day"]["usable"]:
        reasons.append(f"QUOTE_{q['day']['reason']}")
    _price(reasons, sym["features"]["price_intraday"])
    h = sym["flags"]["halt"]["value"]
    if h == TRUE:
        reasons.append("HALTED")
    elif h != FALSE:
        reasons.append("HALT_STATUS_UNKNOWN")
    return {"state": INELIGIBLE if reasons else ELIGIBLE, "eligible": not reasons, "reasons": sorted(set(reasons)), "rank": None}


def swing(sym: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = [] if gate["actionable"] else list(gate["codes"])
    d = sym["daily"]
    if not d["usable"]:
        reasons.append(f"DAILY_BAR_{d['reason']}")
    _price(reasons, sym["features"]["price_swing"])
    if sym["flags"]["halt"]["value"] == TRUE:
        reasons.append("HALTED")
    return {"state": INELIGIBLE if reasons else ELIGIBLE, "eligible": not reasons, "reasons": sorted(set(reasons)), "rank": None}


def sequence(results: Dict[str, Dict[str, Any]], horizon: str) -> List[str]:
    """Rank `horizon` in place and return its symbols in order. INTRADAY: the gap list (eligible, usable gap_pct, descending; ties by
    symbol); everything else after it in symbol order, unranked. SWING: symbol order, unranked."""
    if horizon == SWING:
        return sorted(results)
    if horizon != INTRADAY:                                       # explicit, not `assert`: it must hold under python -O as well
        raise ValueError(f"unknown horizon {horizon!r}")
    ranked = sorted((s for s, r in results.items() if r["horizons"][INTRADAY]["eligible"] and r["features"]["gap_pct"]["usable"]),
                    key=lambda s: (-results[s]["features"]["gap_pct"]["value"], s))
    for i, s in enumerate(ranked, 1):
        results[s]["horizons"][INTRADAY]["rank"] = i
    return ranked + sorted(s for s in results if s not in ranked)
