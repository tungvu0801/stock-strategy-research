"""strategy_validation/integrity.py — Stage 6.3.1 VALIDATION INTEGRITY: the accounting invariant and the persistence gate.

Integrity and performance are different questions. The performance verdict (INSUFFICIENT_DATA / FAIL / MIXED / PASS) says what the strategy
did; integrity says whether the run can be trusted at all. Integrity PASS + verdict FAIL (or INSUFFICIENT_DATA) is a valid, persistable research
result. Integrity FAIL is never persisted, however good the numbers look - its verdict is VALIDATION INVALID and it may only be used as an
ephemeral research / debug run.

  reconcile(record)  per evaluated arm, as disjoint candidate-id sets: accepted = simulated + boundary_purged + explicit simulation skips, and
                     input candidates = accepted + stand_aside; the same split by TRAIN / OOS. Anything else: accounting FAIL (VALIDATION INVALID).
  evaluate(record)   PersistenceEligibility: every check PASS / FAIL / NOT_APPLICABLE with its detail; ELIGIBLE only when none FAILS.

Both read the run RECORD (plain data, exactly as stored) and never the engine's objects, so the store re-evaluates the gate itself before it
writes: neither the CLI nor any caller can persist a run the gate blocks."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field

from strategy_validation import (ARM_STATUSES, BAR_FREQUENCY, BOUNDARY_POLICY, PARITY_REQUIREMENTS, PARITY_STATUSES, PROVENANCE_CONDITIONAL, PROVENANCE_REQUIRED,
                                 PURGE_REASON, REVIEW_STATUSES, VERDICTS)

GATE_VERSION = "integrity_gate_v1"
PERFORMANCE_VERDICTS = tuple(v for v in VERDICTS if v != "INVALID")


class GateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check: str
    status: str                                 # PASS | FAIL | NOT_APPLICABLE
    detail: str = ""


class PersistenceEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str                                 # ELIGIBLE | BLOCKED
    integrity_status: str                       # PASS | FAIL
    checks: List[GateCheck] = Field(default_factory=list)
    reasons: List[Dict[str, str]] = Field(default_factory=list)   # the failing checks: {code, text}
    gate_version: str = GATE_VERSION

    @property
    def eligible(self) -> bool:
        return self.status == "ELIGIBLE"


def segment(signal_date: str, train_end: str) -> str:
    return "TRAIN" if signal_date <= train_end else "OOS"


# ---------------------------------------------------------------- accounting ----------------------------------------------------------------
def reconcile_arm(arm: dict, seg_of: Dict[str, str]) -> dict:
    accepted = [d["candidate_id"] for d in arm.get("decisions") or [] if d.get("decision") == "TAKE"]
    stand = [d["candidate_id"] for d in arm.get("decisions") or [] if d.get("decision") == "STAND_ASIDE"]
    traded = [t["candidate_id"] for t in arm.get("trades") or []]
    skips = arm.get("simulation_skips") or []
    purged = [s.get("candidate_id") for s in skips if s.get("reason") == PURGE_REASON]
    other = [s for s in skips if s.get("reason") != PURGE_REASON]
    other_ids = [s.get("candidate_id") for s in other]
    A, T, P, O = set(accepted), set(traded), set(purged), set(other_ids)
    problems = []
    if len(T) != len(traded):
        problems.append("a candidate has more than one simulated trade")
    if len(P) != len(purged) or len(O) != len(other_ids):
        problems.append("a candidate is purged or skipped more than once")
    if None in P or None in O:
        problems.append("a purge or skip has no candidate id")
    if T & P or T & O or P & O:
        problems.append("a candidate is in more than one terminal category (traded / purged / skipped)")
    if (T | P | O) != A:
        problems.append(f"{len(A - (T | P | O))} accepted candidate(s) have no terminal category; {len((T | P | O) - A)} outcome(s) belong to no accepted candidate")
    if len(accepted) + len(stand) != len(arm.get("input_candidate_ids") or []):
        problems.append("TAKE + STAND_ASIDE decisions do not cover the arm's input candidates")
    if any(seg_of.get(c) == "OOS" for c in P):
        problems.append("an OOS candidate was boundary-purged (only TRAIN outcomes can cross the boundary)")
    by_reason: Dict[str, int] = {}
    for s in other:
        by_reason[s.get("reason") or "?"] = by_reason.get(s.get("reason") or "?", 0) + 1
    segments = {}
    for seg in ("TRAIN", "OOS"):
        segments[seg] = {"accepted": sum(1 for c in accepted if seg_of.get(c) == seg), "simulated": sum(1 for c in traded if seg_of.get(c) == seg),
                         "boundary_purged": sum(1 for c in purged if seg_of.get(c) == seg), "skipped": sum(1 for c in other_ids if seg_of.get(c) == seg)}
        s = segments[seg]
        if s["accepted"] != s["simulated"] + s["boundary_purged"] + s["skipped"]:
            problems.append(f"{seg}: accepted {s['accepted']} != simulated {s['simulated']} + purged {s['boundary_purged']} + skipped {s['skipped']}")
    return {"status": "FAIL" if problems else "PASS", "problems": problems, "segments": segments,
            "counts": {"candidates": len(arm.get("input_candidate_ids") or []), "accepted": len(accepted), "stand_aside": len(stand), "simulated": len(traded),
                       "boundary_purged": len(purged), "skipped": len(other), "skipped_by_reason": dict(sorted(by_reason.items()))},
            "invariant": "accepted = simulated + boundary_purged + skipped; candidates = accepted + stand_aside"}


def reconcile(rec: dict) -> dict:
    train_end = rec.get("train_end") or ""
    seg_of = {c["candidate_id"]: segment(c["signal_date"], train_end) for c in rec.get("candidates") or []}
    skipped = rec.get("candidates_skipped") or []
    reasons: Dict[str, int] = {}
    for s in skipped:
        reasons[s.get("reason") or "?"] = reasons.get(s.get("reason") or "?", 0) + 1
    generated = {"candidates": len(rec.get("candidates") or []), "skipped_before_arms": len(skipped),
                 "missing_data_skipped": reasons.get("MISSING_CRITICAL_FEATURE", 0), "skipped_by_reason": dict(sorted(reasons.items()))}
    if not (rec.get("integrity") or {}).get("same_candidate_set", False):
        return {"status": "NOT_APPLICABLE", "arms": {}, "generated": generated, "note": "not simulated: the arms did not receive the same candidate set"}
    arms = {}
    for name, a in (rec.get("arms") or {}).items():
        if a.get("status") != "EVALUATED":
            arms[name] = {"status": "NOT_APPLICABLE", "problems": [], "note": f"arm {a.get('status')}: nothing simulated"}
            continue
        arms[name] = reconcile_arm(a, seg_of)
    return {"status": "FAIL" if any(v["status"] == "FAIL" for v in arms.values()) else "PASS", "arms": arms, "generated": generated}


# ---------------------------------------------------------------- the gate --------------------------------------------------------------------
def _structure(rec: dict) -> List[str]:
    miss = [k for k in ("run_id", "strategy_id", "strategy_version", "dataset_id", "created_at", "validation_version", "train_end") if not rec.get(k)]
    if rec.get("run_id") and not re.fullmatch(r"[0-9a-f]{32}", str(rec["run_id"])):
        miss.append("run_id format")
    if (rec.get("verdict") or {}).get("verdict") not in VERDICTS:
        miss.append("verdict")
    rules = (rec.get("arms") or {}).get("RULES") or {}
    if rules.get("status") != "EVALUATED" or not all(k in (rules.get("metrics") or {}) for k in ("TRAIN", "OOS", "ALL")):
        miss.append("RULES arm evaluated with TRAIN / OOS / ALL metrics")
    snap = rec.get("config_snapshot") or {}
    if not snap.get("adapter") or not snap.get("split"):
        miss.append("config snapshot (adapter, split)")
    if not (rec.get("audit") or {}).get("status"):
        miss.append("audit")
    if not rec.get("candidates") and rec.get("candidate_count"):
        miss.append("candidate set")
    return miss


def _iso(value) -> bool:
    for parse in (datetime.fromisoformat, date.fromisoformat):
        try:
            parse(str(value))
            return True
        except ValueError:
            continue
    return False


def _provenance(rec: dict) -> GateCheck:
    p = rec.get("provenance") or {}
    decl = ((rec.get("config_snapshot") or {}).get("adapter") or {}).get("declarations") or {}
    bad = [f"{k} missing" for k in PROVENANCE_REQUIRED if p.get(k) in (None, "", [])]
    if p.get("retrieved_at") not in (None, "", []) and not _iso(p.get("retrieved_at")):
        bad.append(f"retrieved_at {p.get('retrieved_at')!r} is not an ISO date / datetime")
    if p.get("bar_frequency") not in (None, "") and p.get("bar_frequency") != BAR_FREQUENCY:
        bad.append(f"bar_frequency {p.get('bar_frequency')} (the simulator needs {BAR_FREQUENCY})")
    for k, want in (("dataset_id", rec.get("dataset_id")), ("start", rec.get("dataset_start")), ("end", rec.get("dataset_end"))):
        if not p.get(k) or p.get(k) != want:
            bad.append(f"derived {k} does not match the run")
    if not p.get("symbols") or not p.get("sessions"):
        bad.append("derived symbols / sessions missing")
    req = decl.get("provenance_requires")
    if req is None:
        bad.append("the adapter did not declare which conditional provenance (corporate_actions / universe / point_in_time) it requires")
        cond = "undeclared"
    else:
        unknown = [k for k in req if k not in PROVENANCE_CONDITIONAL]
        bad += [f"unknown conditional provenance {k}" for k in unknown]
        bad += [f"{k} missing (required by the adapter)" for k in req if k in PROVENANCE_CONDITIONAL and p.get(k) in (None, "", [])]
        not_req = [k for k in PROVENANCE_CONDITIONAL if k not in req]
        cond = f"required: {', '.join(req) or 'none'}; not required by the adapter: {', '.join(not_req) or 'none'}"
    return GateCheck(check="PROVENANCE", status="FAIL" if bad else "PASS", detail="; ".join(bad) if bad else f"complete ({cond})")


def _parity(rec: dict) -> GateCheck:
    rp = rec.get("replay_parity") or {}
    req, st, live, reg_live, ref = rp.get("requirement"), rp.get("status"), rp.get("live_implementation"), bool(rp.get("registry_live")), rp.get("reference") or ""
    if req is None:
        return GateCheck(check="REPLAY_PARITY", status="FAIL", detail="the adapter did not declare its replay-parity requirement")
    if req not in PARITY_REQUIREMENTS or (st is not None and st not in PARITY_STATUSES):
        return GateCheck(check="REPLAY_PARITY", status="FAIL", detail=f"unknown declaration {req} / {st}")
    if (live or reg_live) and req != "EXACT_REQUIRED":
        return GateCheck(check="REPLAY_PARITY", status="FAIL", detail="a separate live implementation exists, so exact replay parity is required")
    if req == "EXACT_REQUIRED":
        if st != "ESTABLISHED":
            return GateCheck(check="REPLAY_PARITY", status="FAIL", detail=f"exact replay parity with {live or 'the live implementation'} is {st or 'undeclared'}")
        if not ref:
            return GateCheck(check="REPLAY_PARITY", status="FAIL", detail="parity declared ESTABLISHED without a reference to the parity tests that establish it")
        return GateCheck(check="REPLAY_PARITY", status="PASS", detail=f"exact replay parity with {live or 'the live implementation'} established ({ref})")
    return GateCheck(check="REPLAY_PARITY", status="NOT_APPLICABLE", detail="research-only strategy: no separate live implementation to reproduce")


def evaluate(rec: dict) -> PersistenceEligibility:
    checks: List[GateCheck] = []
    miss = _structure(rec)
    checks.append(GateCheck(check="STRUCTURE", status="FAIL" if miss else "PASS", detail=("incomplete: " + ", ".join(miss)) if miss else "complete run record"))
    adapter = (rec.get("config_snapshot") or {}).get("adapter") or {}
    cands = rec.get("candidates") or []
    sv_bad = (not rec.get("strategy_version") or adapter.get("strategy_version") != rec.get("strategy_version")
              or any(c.get("strategy_version") != rec.get("strategy_version") for c in cands))
    checks.append(GateCheck(check="STRATEGY_VERSION", status="FAIL" if sv_bad else "PASS",
                            detail="strategy_version missing or inconsistent across the run, the adapter and the candidates" if sv_bad else f"v{rec.get('strategy_version')}"))
    checks.append(_provenance(rec))
    integ = rec.get("integrity") or {}
    ids = [c["candidate_id"] for c in cands]
    set_hash = hashlib.sha256("|".join(sorted(ids)).encode("utf-8")).hexdigest()[:32]
    arms = rec.get("arms") or {}
    same = bool(integ.get("same_candidate_set")) and rec.get("candidate_set_hash") == set_hash and all(a.get("input_candidate_ids") == ids for a in arms.values())
    checks.append(GateCheck(check="CANDIDATE_SET", status="PASS" if same else "FAIL",
                            detail=f"one candidate set for every arm ({len(ids)} candidates)" if same else "the arms did not receive the generated candidate set (or its hash does not match)"))
    bad_arms = [n for n, a in arms.items() if a.get("status") not in ARM_STATUSES or a.get("status") == "INVALID"]
    checks.append(GateCheck(check="ARM_CONTRACT", status="FAIL" if bad_arms else "PASS", detail=("invalid arm(s): " + ", ".join(bad_arms)) if bad_arms else "every arm kept its contract"))
    ca = rec.get("causality") or {}
    crit = [c.get("check") for c in ca.get("automated_checks") or [] if c.get("result") == "CRITICAL"]
    auto_bad = bool(crit) or bool(ca.get("critical")) or not ca.get("automated_checks")
    checks.append(GateCheck(check="CAUSALITY_AUTOMATED", status="FAIL" if auto_bad else "PASS",
                            detail=("CRITICAL: " + ", ".join(crit)) if crit else ("no automated causality check recorded" if auto_bad else f"{len(ca['automated_checks'])} automated checks, no CRITICAL finding")))
    rs, ref = ca.get("manual_review_status"), ca.get("manual_review_reference") or ""
    review_bad = rs not in REVIEW_STATUSES or (rs == "COMPLETED" and not ref)
    checks.append(GateCheck(check="CAUSALITY_MANUAL_REVIEW", status="FAIL" if review_bad else "PASS",
                            detail=("manual causality review status undeclared" if rs not in REVIEW_STATUSES else "COMPLETED without a review reference") if review_bad
                            else (f"COMPLETED ({ref})" if rs == "COMPLETED" else "PENDING: declared, persisted as pending - never counted as reviewed")))
    checks.append(_parity(rec))
    au = rec.get("audit") or {}
    n_trades = sum(len(a.get("trades") or []) for a in arms.values())
    audit_bad = au.get("status") != "PASS" or au.get("violations") or au.get("checked") != n_trades or au.get("candidates_checked") != len(cands)
    checks.append(GateCheck(check="MECHANICAL_AUDIT", status="FAIL" if audit_bad else "PASS",
                            detail=(f"audit {au.get('status') or 'missing'}: {len(au.get('violations') or [])} violation(s), {au.get('checked')} of {n_trades} trades, "
                                    f"{au.get('candidates_checked')} of {len(cands)} candidates") if audit_bad else au.get("note", "PASS")))
    acc = reconcile(rec)
    acc_bad = acc["status"] == "FAIL" or (acc["status"] == "NOT_APPLICABLE" and same)
    checks.append(GateCheck(check="ACCOUNTING", status="FAIL" if acc_bad else ("PASS" if acc["status"] == "PASS" else "NOT_APPLICABLE"),
                            detail="; ".join(p for a in acc["arms"].values() for p in a.get("problems", [])) if acc_bad else
                            "accepted = simulated + boundary_purged + skipped in every arm and segment"))
    bnd = rec.get("boundary") or {}
    leaks = [t["candidate_id"] for a in arms.values() for t in a.get("trades") or [] if t.get("segment") == "TRAIN" and t.get("exit_date", "") > rec.get("train_end", "")]
    bnd_bad = bnd.get("policy") != BOUNDARY_POLICY or bool(leaks)
    checks.append(GateCheck(check="BOUNDARY", status="FAIL" if bnd_bad else "PASS",
                            detail=(f"policy {bnd.get('policy') or 'missing'}; {len(leaks)} TRAIN trade(s) use bars after the TRAIN end") if bnd_bad
                            else f"PURGE: {bnd.get('train_boundary_purged_count', 0)} TRAIN outcome(s) purged; no TRAIN trade uses an OOS bar"))
    verdict = (rec.get("verdict") or {}).get("verdict")
    checks.append(GateCheck(check="PERFORMANCE_VERDICT", status="PASS" if verdict in PERFORMANCE_VERDICTS else "FAIL",
                            detail=f"{verdict} (a performance result; integrity is judged separately)" if verdict in PERFORMANCE_VERDICTS
                            else "VALIDATION INVALID: the engine found an integrity failure - no performance conclusion exists"))
    failed = [c for c in checks if c.status == "FAIL"]
    return PersistenceEligibility(status="BLOCKED" if failed else "ELIGIBLE", integrity_status="FAIL" if failed else "PASS", checks=checks,
                                  reasons=[{"code": c.check, "text": c.detail} for c in failed])
