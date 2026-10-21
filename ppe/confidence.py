"""
PPE Confidence (DESIGN_56 §6): deterministic rules over the STORED Stage 5.4 evidence snapshot. Start at 50, add documented
contributions, apply explicit caps, clip to [0, 100]. "Confirming" evidence is evidence in the same direction as the move.
High Stretch alone cannot raise confidence beyond +5; WEAK evidence never raises it. No model, no research run.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ppe import (CAPS, CONFIDENCE_BASE, EXTREME_BAND, FRESH_DAYS, MIN_EVIDENCE_ITEMS, NORMAL_BAND, STALE_DAYS, VERY_STALE_DAYS,
                 confidence_label)
from ppe.features import clip
from ppe.models import ConfidenceResult, EvidenceSnapshot, StretchResult

TEXT = {
    "cause_market_wide": "cause MARKET_WIDE_DOMINANT: the sector / market explains the move",
    "cause_mixed": "cause MIXED: part of the move is sector-driven",
    "cause_not_separable": "cause NOT_SEPARABLE: no sector benchmark in the packet (neutral)",
    "cause_no_material_decline": "cause NO_MATERIAL_DECLINE: the packet found no material decline to explain",
    "cause_not_available": "cause NOT_AVAILABLE in the packet",
    "cause_company_specific": "cause COMPANY_SPECIFIC_DOMINANT: the move is explained by company-specific evidence",
    "sector_explained_share": "sector-explained share of the decline",
    "company_evidence_confirms_move": "supported company-specific evidence points the same way as the move",
    "guidance_confirms_move": "supported guidance points the same way as the move",
    "supply_chain_confirms_move": "supported supplier / customer evidence points the same way as the move",
    "company_fundamentals_intact": "ladder rung 4 (company fundamentals improving)",
    "guidance_intact": "ladder rung 5 (guidance confirming recovery)",
    "market_confirmation": "ladder rung 6 (market / price confirmation)",
    "strength": "evidence strength",
    "freshness": "evidence age",
    "missing_company_evidence": "no company evidence in the packet (rungs 4 and 5 unavailable)",
    "low_coverage": "fewer than %d evidence items" % MIN_EVIDENCE_ITEMS,
    "contradictions": "contradictory evidence items",
    "stretch_extreme": "stretch magnitude is extreme",
}


def compute_confidence(stretch: StretchResult, ev: Optional[EvidenceSnapshot], as_of=None) -> ConfidenceResult:
    if stretch.status != "AVAILABLE" or stretch.score is None:
        return ConfidenceResult(status="STRETCH_NOT_AVAILABLE", reasons=["PPE Stretch is not available, so there is no move to classify."], evidence=ev)
    if ev is None:
        return ConfidenceResult(status="EVIDENCE_NOT_AVAILABLE",
                                reasons=["No stored Stage 5.4 evidence packet exists for this ticker as of this time. Research runs only on explicit request."])
    mag = abs(stretch.score)
    if mag <= NORMAL_BAND:
        return ConfidenceResult(status="NOT_APPLICABLE", evidence=ev,
                                reasons=[f"Stretch is within the normal band (|score| <= {NORMAL_BAND}): there is no stretched move to classify as an overreaction."])
    down = stretch.score < 0
    sign = 1 if down else -1
    contrib: Dict[str, int] = {}
    caps: List[dict] = []

    def cap(value: int, why: str) -> None:
        caps.append({"cap": value, "reason": why})

    cause, share = ev.cause, ev.explained_share
    if cause == "MARKET_WIDE_DOMINANT":
        contrib["cause_market_wide"] = 20
        if share is not None:
            contrib["sector_explained_share"] = int(round(15 * clip(share, 0.0, 1.0)))
    elif cause == "MIXED":
        contrib["cause_mixed"] = 5
        if share is not None:
            contrib["sector_explained_share"] = int(round(15 * clip(share, 0.0, 1.0)))
    elif cause == "COMPANY_SPECIFIC_DOMINANT":
        contrib["cause_company_specific"] = -25
        cap(CAPS["COMPANY_SPECIFIC_DOMINANT"], "cause is COMPANY_SPECIFIC_DOMINANT")
    elif cause == "NOT_SEPARABLE":
        contrib["cause_not_separable"] = 0
    elif cause == "NO_MATERIAL_DECLINE":
        contrib["cause_no_material_decline"] = -10 if down else 0
    else:
        contrib["cause_not_available"] = -10
        cap(CAPS["CAUSE_NOT_AVAILABLE"], "cause is not available in the packet")

    company_conf = ev.company_down if down else ev.company_up
    guidance_conf = ev.guidance_down if down else ev.guidance_up
    supply_conf = ev.supply_down if down else ev.supply_up
    if company_conf:
        contrib["company_evidence_confirms_move"] = -15 * min(company_conf, 2)
        cap(CAPS["COMPANY_CONFIRMS"], f"{company_conf} supported company item(s) confirm the move")
    if guidance_conf:
        contrib["guidance_confirms_move"] = -20
        cap(CAPS["GUIDANCE_CONFIRMS"], "supported guidance confirms the move")
    if supply_conf:
        contrib["supply_chain_confirms_move"] = -10

    for rung, key, pts in (("4", "company_fundamentals_intact", (10, 5)), ("5", "guidance_intact", (10, 5))):
        st = ev.rungs.get(rung)
        if st == "SUPPORTED":
            contrib[key] = pts[0] * sign
        elif st == "PARTIAL":
            contrib[key] = pts[1] * sign
    if ev.rungs.get("6") == "SUPPORTED":
        contrib["market_confirmation"] = 5 * sign

    if ev.strength == "STRONG":
        contrib["strength"] = 10
    elif ev.strength == "MODERATE":
        contrib["strength"] = 5
        cap(CAPS["MODERATE"], "evidence strength is MODERATE (VERY_HIGH needs STRONG evidence)")
    else:
        contrib["strength"] = 0
        cap(CAPS["WEAK"], f"evidence strength is {ev.strength or 'unknown'}")

    age = ev.age_days
    if age <= FRESH_DAYS:
        contrib["freshness"] = 5
    elif age <= STALE_DAYS:
        contrib["freshness"] = 0
    elif age <= VERY_STALE_DAYS:
        contrib["freshness"] = -10
        cap(CAPS["STALE"], f"evidence is {age} days old")
    else:
        contrib["freshness"] = -20
        cap(CAPS["VERY_STALE"], f"evidence is {age} days old")

    if ev.rungs.get("4", "NOT_AVAILABLE") == "NOT_AVAILABLE" and ev.rungs.get("5", "NOT_AVAILABLE") == "NOT_AVAILABLE":
        contrib["missing_company_evidence"] = -15
        cap(CAPS["MISSING_COMPANY_EVIDENCE"], "no company evidence in the packet")
    if ev.n_items < MIN_EVIDENCE_ITEMS:
        contrib["low_coverage"] = -10
    if ev.contradictions:
        contrib["contradictions"] = -min(20, 5 * ev.contradictions)
    if mag >= EXTREME_BAND:
        contrib["stretch_extreme"] = 5

    raw = CONFIDENCE_BASE + sum(contrib.values())
    capped = min([raw] + [c["cap"] for c in caps])
    score = int(round(clip(capped, 0, 100)))
    reasons = [f"{'+' if v > 0 else ''}{v} {TEXT[k]}" for k, v in sorted(contrib.items(), key=lambda kv: -abs(kv[1])) if v != 0]
    reasons += [f"capped at {c['cap']}: {c['reason']}" for c in caps]
    if raw < capped:
        pass
    return ConfidenceResult(status="AVAILABLE", score=score, label=confidence_label(score), reasons=reasons, contributions=contrib, caps=caps, evidence=ev)
