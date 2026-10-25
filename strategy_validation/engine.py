"""strategy_validation/engine.py — one validation run.

  1. candidates are generated ONCE (adapter -> normalize: identity, missing-data skips, Strategy Standard v1) -> an immutable tuple
  2. causality report (static + runtime) on that tuple
  3. the chronological split (TRAIN = signals on or before train_end, OOS = after); OOS is reported on its own, never folded into one number
  4. RULES / GATED / MODEL each receive the SAME tuple; the same-candidate-set guarantee is checked (a mismatch makes the run INVALID)
  5. each accepted candidate is simulated once (identical across arms), then attributed to the arms that accepted it. BOUNDARY-SAFE (Stage 6.3.1):
     a TRAIN candidate whose holding window (the entry bar + up to max_hold_bars sessions) reaches past the TRAIN end is PURGED - whatever its
     outcome, so the purge never selects on a result (TRAIN_OOS_BOUNDARY_PURGE: excluded from TRAIN and OOS metrics, never re-labelled, never
     truncated at a price); every other TRAIN candidate is simulated on the dataset cut at the TRAIN end, so its outcome cannot read an OOS bar
  6. metrics per arm x segment, diagnostics, the accounting invariant, the independent mechanical audit, the verdict per arm (the run's verdict = RULES)
  7. the persistence integrity gate (strategy_validation/integrity.py) - recorded on the run; the store re-evaluates it before any write
The run never mutates a strategy, its parameters or its lifecycle. Parameter sensitivity runs the explicitly supplied neighbours with the same split,
the same boundary policy and friction, and only labels the neighbourhood."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from strategy_validation import ARMS, BOUNDARY_POLICY, DEFAULT_FRICTION, DEFAULT_SIZING, DEFAULT_TRAIN_RATIO, DISCLAIMER, PURGE_REASON, VERSION
from strategy_validation import arms as A
from strategy_validation import integrity as IG
from strategy_validation import audit as AU
from strategy_validation import candidates as CD
from strategy_validation import causality as CA
from strategy_validation import diagnostics as DG
from strategy_validation import metrics as MX
from strategy_validation import simulator as SIM
from strategy_validation import verdict as V
from strategy_validation.dataset import Dataset, segment_of, split
from strategy_validation.models import ArmResult, AuditResult, CausalityReport, SimulatedTrade, SkippedCandidate, ValidationRun


class ValidationError(ValueError):
    pass


def git_commit_sha(root: Optional[Path] = None) -> Optional[str]:
    """The checked-out commit, read from the .git files (no subprocess); None when it cannot be read."""
    try:
        root = Path(root or Path(__file__).resolve().parents[1])
        g = root / ".git"
        gitdir = Path(g.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()) if g.is_file() else g
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:40]
        ref = head.split(" ", 1)[1].strip()
        common = Path((gitdir / "commondir").read_text(encoding="utf-8").strip()) if (gitdir / "commondir").is_file() else gitdir
        common = common if common.is_absolute() else (gitdir / common).resolve()
        for base in (gitdir, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()[:40]
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:40]
    except (OSError, IndexError, ValueError):
        return None
    return None


def outcome(c, dataset: Dataset, train_view: Dataset, split_info: dict, entry_policy: str, friction: dict, sizing: dict):
    """(trade, skip) of one candidate under the Stage 6.3 PURGE policy (DESIGN_63 §9). A TRAIN candidate whose holding window - the bars its
    trade could use: the next max_hold_bars sessions of its symbol after the signal - reaches past the TRAIN end is purged before any simulation,
    whatever its outcome (the decision reads dates only, never a price). Every other TRAIN candidate is simulated on `train_view` (bars <= TRAIN end),
    so its outcome cannot read an OOS price. OOS candidates see everything."""
    seg = segment_of(c.signal_date, split_info)
    if seg == "OOS":
        return SIM.simulate(c, dataset, arm="_", segment=seg, entry_policy=entry_policy, friction=friction, sizing=sizing)
    end = split_info["train_end"]
    window = dataset.bars_after(c.symbol, c.signal_date)[:c.max_hold_bars]
    beyond = [b.date for b in window if b.date > end]
    if beyond:
        return None, SkippedCandidate(symbol=c.symbol, signal_date=c.signal_date, candidate_id=c.candidate_id, reason=PURGE_REASON,
                                      detail=f"TRAIN candidate (signal {c.signal_date}): its holding window (up to {c.max_hold_bars} sessions) reaches past the TRAIN "
                                             f"end {end} (from {beyond[0]}); excluded from TRAIN and OOS metrics whatever its outcome")
    return SIM.simulate(c, train_view, arm="_", segment=seg, entry_policy=entry_policy, friction=friction, sizing=sizing)


def _simulate_arm(arm: ArmResult, cands, dataset, train_view, split_info, adapter, friction, sizing, cache) -> None:
    by_id = {c.candidate_id: c for c in cands}
    accepted = [d.candidate_id for d in arm.decisions if d.decision == "TAKE"]
    trades: List[SimulatedTrade] = []
    for cid in accepted:
        if cid not in cache:
            cache[cid] = outcome(by_id[cid], dataset, train_view, split_info, adapter.entry_policy, friction, sizing)
        t, skip = cache[cid]
        if skip is not None:
            arm.simulation_skips.append(skip)
            continue
        tid = hashlib.sha256(f"{arm.arm}|{cid}".encode("utf-8")).hexdigest()[:24]
        trades.append(t.model_copy(update={"arm": arm.arm, "trade_id": tid}))
    arm.trades = trades


def _segment_metrics(arm: ArmResult, seg_of: dict, equity: float) -> dict:
    out = {}
    for seg in ("TRAIN", "OOS", "ALL"):
        ids = {i for i in arm.input_candidate_ids if seg == "ALL" or seg_of.get(i) == seg}
        decisions = [d for d in arm.decisions if d.candidate_id in ids]
        trades = [t for t in arm.trades if t.candidate_id in ids]
        purged = sum(1 for s in arm.simulation_skips if s.candidate_id in ids and s.reason == PURGE_REASON)
        skipped = sum(1 for s in arm.simulation_skips if s.candidate_id in ids and s.reason != PURGE_REASON)
        out[seg] = MX.compute(trades, candidate_count=len(ids), accepted_count=sum(1 for d in decisions if d.decision == "TAKE"),
                              stand_aside_count=sum(1 for d in decisions if d.decision == "STAND_ASIDE"), equity=equity, boundary_purged_count=purged, skipped_count=skipped)
    return out


def _arm_diagnostics(r: ArmResult) -> dict:
    if not r.metrics:
        return {}
    return {"train_vs_oos": DG.train_vs_oos(r.metrics["TRAIN"], r.metrics["OOS"]), "concentration": DG.concentration(r.trades), "ambiguous": DG.ambiguous(r.trades),
            "friction": DG.friction(r.metrics["ALL"])}


def run_validation(adapter, dataset: Dataset, *, train_ratio: float = DEFAULT_TRAIN_RATIO, arms: Sequence[str] = ARMS, gates: Iterable = (), model=None,
                   friction: Optional[dict] = None, sizing: Optional[dict] = None, neighbours: Optional[List[dict]] = None,
                   adapter_factory: Optional[Callable[[dict], object]] = None, created_at: Optional[str] = None, registry=None,
                   arm_hooks: Optional[dict] = None) -> ValidationRun:
    if not getattr(adapter, "historical_replay_supported", False):
        raise ValidationError(getattr(adapter, "not_supported_reason", "") or "this strategy has no historical replay")
    arms = [a for a in ARMS if a in set(x.upper() for x in arms)] or ["RULES"]
    if "RULES" not in arms:
        arms = ["RULES"] + arms                                                  # the raw strategy is always the reference arm
    friction = {**DEFAULT_FRICTION, **(friction or {})}
    sizing = {**DEFAULT_SIZING, **(sizing or {})}
    split_info = split(dataset, train_ratio)
    train_view = dataset.until(split_info["train_end"])                          # what a TRAIN outcome may read: nothing after the TRAIN end
    cands, skipped = CD.normalize(adapter, adapter.candidates(dataset), dataset, registry)      # ONCE
    ids = [c.candidate_id for c in cands]
    causality: CausalityReport = CA.report(adapter, cands, dataset, lambda ds: CD.normalize(adapter, adapter.candidates(ds), ds, registry)[0],
                                           extra_cuts=[split_info["train_end"]])
    review = getattr(adapter, "causality_review", None) or {}
    causality.manual_review_status = review.get("status") or "UNDECLARED"           # the adapter's declaration - never inferred from the automated checks
    causality.manual_review_reference = str(review.get("reference") or "")
    results = {}
    hooks = arm_hooks or {}                                                      # tests only: wrap an arm to prove a mismatch is caught
    for name in arms:
        if name == "RULES":
            r = A.rules_arm(cands)
        elif name == "GATED":
            r = A.gated_arm(cands, gates)
        else:
            r = A.model_arm(cands, model)
        if name in hooks:
            r = hooks[name](r)
        results[name] = r
    integrity = A.same_candidates(ids, results.values())
    integrity["candidate_set_hash"] = CD.candidate_set_hash(ids)
    invalid_arms = [a.arm for a in results.values() if a.status == "INVALID"]
    integrity["invalid_arms"] = invalid_arms
    seg_of = {c.candidate_id: segment_of(c.signal_date, split_info) for c in cands}
    cache: dict = {}
    for r in results.values():
        if r.status == "EVALUATED" and integrity["same_candidate_set"]:          # a mismatched set is never simulated: no comparative conclusion
            _simulate_arm(r, cands, dataset, train_view, split_info, adapter, friction, sizing, cache)
            r.metrics = _segment_metrics(r, seg_of, float(sizing["equity"]))
            r.diagnostics = _arm_diagnostics(r)
    now = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    run = ValidationRun(run_id=uuid.uuid4().hex, strategy_id=adapter.strategy_id, strategy_version=adapter.strategy_version, dataset_id=dataset.dataset_id, dataset_kind=dataset.kind,
                        dataset_start=dataset.start, dataset_end=dataset.end, train_start=split_info["train_start"], train_end=split_info["train_end"], oos_start=split_info["oos_start"],
                        oos_end=split_info["oos_end"], oos_sessions=split_info["oos_sessions"], created_at=now, validation_version=VERSION, git_commit_sha=git_commit_sha(),
                        config_snapshot={"adapter": adapter.describe(), "split": split_info, "arms": arms, "gates": [getattr(g, "name", "?") for g in gates or []],
                                         "model": getattr(model, "name", None), "sizing": sizing, "dataset_source": dataset.source},
                        friction_model=friction, execution_assumptions={"entry_policy": adapter.entry_policy, "moc_declared": adapter.moc_declared, "signal_basis": adapter.signal_basis,
                                                                        "ambiguous_bar_policy": "STOP_FIRST", "target_gap_fill": "at the target (conservative)",
                                                                        "stop_gap_fill": "at the open (worse)", "time_exit": "close of the last allowed bar",
                                                                        "sizing": "risk-based on a fixed hypothetical equity; overlapping trades allowed; no compounding"},
                        candidate_count=len(cands), candidate_set_hash=integrity["candidate_set_hash"], candidates_skipped=skipped, arms_run=arms, integrity=integrity,
                        causality=causality, arms=results, candidates=list(cands), disclaimer=DISCLAIMER, provenance=dataset.provenance_record())
    entry = (registry or {}).get(adapter.strategy_id) if isinstance(registry, dict) else None
    run.replay_parity = {"requirement": getattr(adapter, "replay_parity_requirement", None), "status": getattr(adapter, "replay_parity_status", None),
                         "reference": getattr(adapter, "replay_parity_reference", None),
                         "live_implementation": getattr(adapter, "live_implementation", None),
                         "registry_live": bool(entry is not None and getattr(entry, "implemented", False) and getattr(entry, "setup_type", None))}
    purged = {name: sorted(s.candidate_id for s in r.simulation_skips if s.reason == PURGE_REASON) for name, r in results.items()}
    run.boundary = {"policy": BOUNDARY_POLICY, "reason": PURGE_REASON, "train_end": split_info["train_end"], "purged": purged,
                    "train_boundary_purged_count": len(purged.get("RULES", [])),
                    "text": f"{len(purged.get('RULES', []))} TRAIN outcome(s) purged at the TRAIN / OOS boundary (their outcome would need OOS bars); "
                            "they count as TRAIN candidates and in no TRAIN or OOS metric."}
    # the independent audit reads the generated records as plain data (exactly what the store persists)
    rec = json.loads(run.model_dump_json())
    run.accounting = IG.reconcile(rec)
    trades = [t for a in rec["arms"].values() for t in a["trades"]]
    bars = {s: [b.model_dump() for b in rows] for s, rows in dataset.bars.items()}
    run.audit = AuditResult(**AU.audit(rec, rec["candidates"], trades, bars))
    rules = results["RULES"]
    diag = dict(rules.diagnostics)
    sens = {"label": "NOT_RUN", "text": "No neighbouring configuration was supplied.", "neighbours": []}
    if neighbours and adapter_factory is not None and rules.metrics:
        sens = DG.sensitivity(rules.metrics["ALL"]["expectancy_R"], [_neighbour(adapter_factory, p, dataset, train_ratio, friction, sizing, registry) for p in neighbours])
    diag["sensitivity"] = sens
    diag["boundary"] = {"policy": BOUNDARY_POLICY, "train_boundary_purged_count": run.boundary["train_boundary_purged_count"], "text": run.boundary["text"]}
    diag["skips"] = {"candidate_skipped_missing_data": sum(1 for s in skipped if s.reason == "MISSING_CRITICAL_FEATURE"),
                     "by_reason": {r: sum(1 for s in skipped if s.reason == r) for r in sorted({s.reason for s in skipped})}}
    run.diagnostics = diag
    integrity_ok = integrity["same_candidate_set"] and not invalid_arms
    accounting_problems = [f"{n}: {p}" for n, a in run.accounting.get("arms", {}).items() for p in a.get("problems", [])]
    for name, r in results.items():
        if r.status == "NOT_EVALUATED":
            r.verdict = {"verdict": None, "label": "Not evaluated", "reasons": [{"code": "NOT_EVALUATED", "text": r.note}], "warnings": [], "decision": "", "eligible_for_review": False}
            continue
        r.verdict = V.decide(r.metrics, oos_sessions=split_info["oos_sessions"], integrity_ok=integrity_ok and r.status != "INVALID",
                             integrity_problems=integrity["problems"] + ([r.note] if r.status == "INVALID" else []), causality_critical=causality.critical,
                             audit_ok=run.audit.status == "PASS", diagnostics=diag if name == "RULES" else r.diagnostics,
                             sensitivity_label=sens["label"] if name == "RULES" else None, accounting_problems=accounting_problems)
    run.verdict = results["RULES"].verdict
    run.eligibility = IG.evaluate(json.loads(run.model_dump_json())).model_dump()   # informational here; the store re-evaluates before any write
    return run


def _neighbour(factory, params: dict, dataset, train_ratio, friction, sizing, registry) -> dict:
    a = factory(params)
    split_info = split(dataset, train_ratio)
    train_view = dataset.until(split_info["train_end"])
    cands, _ = CD.normalize(a, a.candidates(dataset), dataset, registry)
    trades = []
    for c in cands:
        t, skip = outcome(c, dataset, train_view, split_info, a.entry_policy, friction, sizing)   # the same boundary policy as the base run
        if t is not None:
            trades.append(t)
    m = MX.compute(trades, candidate_count=len(cands), accepted_count=len(cands), stand_aside_count=0, equity=float(sizing["equity"]))
    return {"label": json.dumps(params, sort_keys=True), "params": dict(params), "expectancy_R": m["expectancy_R"], "trade_count": m["trade_count"]}
