"""strategy_validation/arms.py — the three arms, all fed the SAME immutable candidate tuple.

  RULES  every candidate is taken (the raw strategy)
  GATED  deterministic hand-written gates may VETO a candidate (the control arm); with no real gate the arm equals RULES: NO_ADDITIONAL_GATES
  MODEL  an optional decider may only TAKE or STAND_ASIDE a candidate; with no decider configured the arm is NOT_EVALUATED (never zero)

A gate or a decider receives a deep copy of the candidate and can return nothing but a decision for that candidate id: it cannot create a trade,
change a symbol, a direction, a stop, a target or the timing. The model is a filter, not a source of edge."""
from __future__ import annotations

from typing import Iterable, List, Optional, Protocol, Sequence, Tuple

from strategy_validation.models import ArmDecision, ArmResult, StrategyCandidate


class Gate(Protocol):
    name: str

    def decide(self, candidate: StrategyCandidate) -> Tuple[str, str]:          # ("ALLOW" | "VETO", reason)
        ...


class ModelDecider(Protocol):
    name: str

    def decide(self, candidate: StrategyCandidate) -> dict:                     # {"decision": "TAKE" | "STAND_ASIDE", "reason": str, "score": float | None}
        ...


MODEL_KEYS = {"decision", "reason", "score"}


class ArmError(ValueError):
    pass


def rules_arm(cands: Sequence[StrategyCandidate]) -> ArmResult:
    return ArmResult(arm="RULES", status="EVALUATED", input_candidate_ids=[c.candidate_id for c in cands],
                     decisions=[ArmDecision(candidate_id=c.candidate_id, decision="TAKE", source="RULES", reason="strategy rule") for c in cands])


def gated_arm(cands: Sequence[StrategyCandidate], gates: Iterable[Gate] = ()) -> ArmResult:
    gates = list(gates or [])
    ids = [c.candidate_id for c in cands]
    if not gates:
        return ArmResult(arm="GATED", status="EVALUATED", input_candidate_ids=ids, gates=[], note="NO_ADDITIONAL_GATES: no real gate is defined, so GATED equals RULES",
                         decisions=[ArmDecision(candidate_id=c.candidate_id, decision="TAKE", source="NO_ADDITIONAL_GATES") for c in cands])
    out: List[ArmDecision] = []
    for c in cands:
        dec = ArmDecision(candidate_id=c.candidate_id, decision="TAKE", source="ALL_GATES", reason="every gate allowed")
        for g in gates:
            verdict, reason = g.decide(c.model_copy(deep=True))
            if verdict not in ("ALLOW", "VETO"):
                raise ArmError(f"gate {g.name} returned {verdict!r}; a gate answers ALLOW or VETO only")
            if verdict == "VETO":
                dec = ArmDecision(candidate_id=c.candidate_id, decision="STAND_ASIDE", source=g.name, reason=str(reason or "veto"))
                break
        out.append(dec)
    return ArmResult(arm="GATED", status="EVALUATED", input_candidate_ids=ids, gates=[g.name for g in gates], decisions=out)


def model_arm(cands: Sequence[StrategyCandidate], model: Optional[ModelDecider] = None) -> ArmResult:
    ids = [c.candidate_id for c in cands]
    if model is None:
        return ArmResult(arm="MODEL", status="NOT_EVALUATED", input_candidate_ids=ids, note="NOT_EVALUATED: no model decider is configured (no provider, no external call)")
    out: List[ArmDecision] = []
    try:
        for c in cands:
            ans = model.decide(c.model_copy(deep=True))
            if not isinstance(ans, dict) or not set(ans) <= MODEL_KEYS or ans.get("decision") not in ("TAKE", "STAND_ASIDE"):
                raise ArmError(f"model {getattr(model, 'name', '?')} returned {ans!r}: a model may only answer TAKE or STAND_ASIDE (+ reason, score) for the candidate")
            score = ans.get("score")
            out.append(ArmDecision(candidate_id=c.candidate_id, decision=ans["decision"], source=str(getattr(model, "name", "model")), reason=str(ans.get("reason") or ""),
                                   score=None if score is None else float(score)))
    except ArmError as exc:
        return ArmResult(arm="MODEL", status="INVALID", input_candidate_ids=ids, note=str(exc))
    return ArmResult(arm="MODEL", status="EVALUATED", input_candidate_ids=ids, decisions=out, note="the model filters the candidate set; it is not a source of edge")


def same_candidates(candidate_ids: List[str], arms: Iterable[ArmResult]) -> dict:
    """The same-candidate-set guarantee: every arm received exactly the generated candidate ids (same order), and every decision belongs to one."""
    problems = []
    for a in arms:
        if a.input_candidate_ids != candidate_ids:
            problems.append(f"{a.arm}: input candidates differ from the generated set ({len(a.input_candidate_ids)} vs {len(candidate_ids)})")
        if a.status == "EVALUATED" and [d.candidate_id for d in a.decisions] != a.input_candidate_ids:
            problems.append(f"{a.arm}: decisions do not cover exactly the input candidates")
    return {"same_candidate_set": not problems, "problems": problems}
