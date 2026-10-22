"""
The deterministic transition function (DESIGN_58 §5–§6). Pure: evaluate(prev, inputs) -> Decision. No I/O, no clock, no randomness.
"""
from __future__ import annotations

from typing import List, Optional

from setups import (ALERTED_TRANSITIONS, EVIDENCE_MAX_AGE_DAYS, INVALIDATE_DEMAND, INVALIDATE_DEMAND_EVALS, INVALIDATE_STRETCH, R_AWAITING_RESET,
                    R_BLOCK_EVIDENCE, R_BLOCK_EVIDENCE_STALE, R_BLOCK_NO_EVIDENCE, R_CONFIDENCE_BELOW, R_CONFIRMED, R_DEMAND_BELOW, R_DEMAND_COLLAPSE_PENDING,
                    R_DEMAND_COLLAPSED, R_DEMAND_UNAVAILABLE, R_EPISODE_CLOSED, R_EVIDENCE_CONTRADICTION, R_HOLDS, R_NO_SETUP, R_PAUSED_PPE, R_PAUSED_STALE,
                    R_PPE_COVERAGE_LOW, R_STRETCH_DETECTED, R_STRETCH_FADED, R_STRETCH_NORMALIZED, R_WEAKENED, READY_CONFIDENCE, READY_DEMAND,
                    READY_DEMAND_COVERAGE, READY_PPE_COVERAGE, READY_EXIT_DEMAND, REASON_TEXT, WATCH_ENTER_STRETCH, WATCH_EXIT_STRETCH)
from setups.models import Decision, EpisodeRecord, SetupInputs

COMPANY = "COMPANY_SPECIFIC_DOMINANT"
STRONG_ENOUGH = ("MODERATE", "STRONG")


def evidence_gate(i: SetupInputs) -> Optional[str]:
    """Why READY is blocked by evidence (first applicable code), or None."""
    if not i.evidence_available:
        return R_BLOCK_NO_EVIDENCE
    if i.evidence_age_days is not None and i.evidence_age_days > EVIDENCE_MAX_AGE_DAYS:
        return R_BLOCK_EVIDENCE_STALE
    if i.evidence_cause == COMPANY and (i.evidence_strength or "") in STRONG_ENOUGH:
        return R_BLOCK_EVIDENCE
    if i.guidance_down >= 1 and i.evidence_cause in (COMPANY, "MIXED"):
        return R_BLOCK_EVIDENCE
    return None


def evidence_contradiction(prev: Optional[EpisodeRecord], i: SetupInputs) -> bool:
    """A NEW packet (created after the episode started) with strong company-specific deterioration invalidates the premise."""
    if prev is None or not i.evidence_available or i.evidence_created_at is None:
        return False
    if i.evidence_cause != COMPANY or i.evidence_strength != "STRONG":
        return False
    if i.evidence_packet_id and i.evidence_packet_id == prev.evidence_packet_id_at_start:
        return False
    return i.evidence_created_at > prev.started_at


def demand_available(i: SetupInputs) -> bool:
    return i.demand_status in ("AVAILABLE", "SIMULATED_DATA") and i.demand is not None and i.demand_coverage + 1e-9 >= READY_DEMAND_COVERAGE and i.demand_fresh


def ppe_usable(i: SetupInputs) -> bool:
    return i.ppe_status in ("AVAILABLE", "SIMULATED_DATA") and i.stretch is not None and i.stretch_status == "AVAILABLE"


def _d(decision: str, frm: str, to: str, code: str, *, blockers: Optional[List[str]] = None, streak: int = 0, episode: str = "CONTINUE") -> Decision:
    return Decision(decision=decision, from_state=frm, to_state=to, reason_code=code, reason_text=REASON_TEXT.get(code, code), blockers=blockers or [],
                    demand_low_streak=streak, episode=episode)


def evaluate(prev: Optional[EpisodeRecord], i: SetupInputs) -> Decision:
    state = prev.state if prev is not None else "NONE"
    streak = prev.demand_low_streak if prev is not None else 0
    # pre-check: without a usable, fresh PPE nothing can be decided — pause, never invalidate
    if not ppe_usable(i):
        return _d("PAUSED", state, state, R_PAUSED_PPE, streak=streak, episode="CONTINUE" if prev is not None else "NONE")
    if not i.ppe_fresh:
        return _d("PAUSED", state, state, R_PAUSED_STALE, blockers=list(i.stale_reasons), streak=streak, episode="CONTINUE" if prev is not None else "NONE")
    s = float(i.stretch)
    conf_ok = i.confidence_status == "AVAILABLE" and i.confidence is not None and i.confidence >= READY_CONFIDENCE
    dem_avail = demand_available(i)
    dem_ok = dem_avail and i.demand >= READY_DEMAND
    gate = evidence_gate(i)
    contradiction = evidence_contradiction(prev, i)
    blockers: List[str] = []
    if s > WATCH_ENTER_STRETCH:
        blockers.append(R_NO_SETUP)
    if i.ppe_coverage + 1e-9 < READY_PPE_COVERAGE:
        blockers.append(R_PPE_COVERAGE_LOW)
    if not conf_ok:
        blockers.append(R_CONFIDENCE_BELOW)
    if not dem_avail:
        blockers.append(R_DEMAND_UNAVAILABLE)
    elif not dem_ok:
        blockers.append(R_DEMAND_BELOW)
    if gate:
        blockers.append(gate)
    ready_ok = s <= WATCH_ENTER_STRETCH and i.ppe_coverage + 1e-9 >= READY_PPE_COVERAGE and conf_ok and dem_ok and gate is None

    if state == "NONE":
        if s <= WATCH_ENTER_STRETCH:
            return _d("TRANSITION", "NONE", "WATCH", R_STRETCH_DETECTED, blockers=blockers, streak=0, episode="NEW")
        return _d("HOLD", "NONE", "NONE", R_NO_SETUP, blockers=blockers, streak=0, episode="NONE")

    if state == "WATCH":
        if contradiction:
            return _d("TRANSITION", "WATCH", "INVALIDATED", R_EVIDENCE_CONTRADICTION, blockers=blockers, streak=0)
        if s > WATCH_EXIT_STRETCH:
            return _d("TRANSITION", "WATCH", "NONE", R_STRETCH_FADED, blockers=blockers, streak=0, episode="CLOSE")
        if ready_ok:
            return _d("TRANSITION", "WATCH", "READY", R_CONFIRMED, streak=0)
        return _d("HOLD", "WATCH", "WATCH", blockers[0] if blockers else R_HOLDS, blockers=blockers, streak=0)

    if state == "READY":
        if contradiction:
            return _d("TRANSITION", "READY", "INVALIDATED", R_EVIDENCE_CONTRADICTION, blockers=blockers, streak=streak)
        if s > INVALIDATE_STRETCH:
            return _d("TRANSITION", "READY", "INVALIDATED", R_STRETCH_NORMALIZED, blockers=blockers, streak=streak)
        if dem_avail and i.demand < INVALIDATE_DEMAND:
            streak += 1
            if streak >= INVALIDATE_DEMAND_EVALS:
                return _d("TRANSITION", "READY", "INVALIDATED", R_DEMAND_COLLAPSED, blockers=blockers, streak=streak)
            return _d("HOLD", "READY", "READY", R_DEMAND_COLLAPSE_PENDING, blockers=blockers, streak=streak)
        if not dem_avail:
            return _d("TRANSITION", "READY", "WATCH", R_DEMAND_UNAVAILABLE, blockers=blockers, streak=0)
        if i.demand < READY_EXIT_DEMAND:
            return _d("TRANSITION", "READY", "WATCH", R_WEAKENED, blockers=blockers, streak=0)
        if gate:
            return _d("TRANSITION", "READY", "WATCH", gate, blockers=blockers, streak=0)
        return _d("HOLD", "READY", "READY", R_HOLDS, blockers=blockers, streak=0)

    # INVALIDATED
    if s > WATCH_EXIT_STRETCH:
        return _d("TRANSITION", "INVALIDATED", "NONE", R_EPISODE_CLOSED, blockers=blockers, streak=0, episode="CLOSE")
    return _d("HOLD", "INVALIDATED", "INVALIDATED", R_AWAITING_RESET, blockers=blockers, streak=0)


def alerts_for(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in ALERTED_TRANSITIONS
