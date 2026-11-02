"""swing_eval/evaluate.py — the OFFLINE evaluator of V1 / C1 (decision lock R14-R17, R22).

    python -m swing_eval.evaluate --manifest MANIFEST.json --dataset FILE --report REPORT.json --out DIR
                                  [--approval "OPERATOR-APPROVAL:<ref>" --approved-manifest-sha256 HASH]

Steps, each fail-closed:
  1. the manifest verifies (its own hash; the strategy code's pre-registration hash, runs and friction; a sip_daily_v1 SIP dataset; a report
     made under the evaluated provenance; the locked values)
  2. the output folder is the manifest's evidence location: outside the repository, not a database path, new (or empty); nothing is overwritten
  3. a HISTORICAL dataset needs an operator approval reference, the approved manifest's sha256, and a new folder directly under EVIDENCE_ROOT
     (R17); SYNTHETIC datasets are for tests
  4. pre-checks: the checked-out commit, the index's computed tree against the manifest's code_tree (a staged change), a clean working tree
     (all read from the .git files, no subprocess: swing_eval/codetree.py), the dataset file's sha256 and content, the accepted pre-run report
  5. the four pre-registered runs of swing_pullback.plan.run() and the eight TRAIN-view neighbours, with every socket and SQLite connection
     refused while they execute; an attempt refuses the evaluation even if the run path caught the error
  6. post-checks of every run record against the manifest: a mismatch makes that run INVALID (never repaired)
  7. blocking validation findings: a stop <= 0 in any candidate of the four runs or of the eight TRAIN-view neighbours (M-5), a CRITICAL
     causality report, a failed audit, a candidate-set mismatch. Any of them: status BLOCKED_VALIDATION_FINDING
  8. R9 stability, R10 comparison, R11 eligibility. Accepted only when the status is COMPLETED; otherwise every outcome is written marked NOT
     ACCEPTED, for diagnosis only (R10.1: an INVALID or BLOCKED run is not compared)
  9. the evidence: JSON files + evidence.sha256 + a plain-language summary, with the approval reference, the approved manifest sha256 and the
     evaluation time; never a database, never a run record changed

The approval reference is checked for its form only and the approved manifest sha256 for equality with the manifest's (research_data.
APPROVAL_CHECK): a procedural record, not an authorization check."""
from __future__ import annotations

import _socket
import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import swing_pullback as SP
from research_data import APPROVAL_CHECK, approved, output_problem
from swing_eval import EVALUATOR_VERSION, EVIDENCE_ROOT, FEE_FLAG_MISSING_HISTORY, R12
from swing_eval import analysis as AN
from swing_eval import codetree as CT
from swing_eval import manifest as MF
from swing_pullback import plan as PL
from swing_pullback.adapter import SwingTrendPullbackAdapter

try:
    import _sqlite3
except ImportError:                                                              # pragma: no cover - CPython ships it
    _sqlite3 = None

NOT_ACCEPTED = "NOT ACCEPTED: diagnostic only (R10.1: an INVALID or BLOCKED run is not compared; M-5.2: no outcome is accepted)"


class EvaluationRefused(RuntimeError):                                          # not an OSError: an `except OSError` never swallows it
    pass


@contextlib.contextmanager
def offline():
    """Every socket and SQLite connection is refused while the runs execute (restored afterwards). Each attempt is recorded, so the evaluation
    is refused even if the run path caught the error."""
    attempts: List[str] = []

    def refuse(*a, **k):
        attempts.append("network or database access")
        raise EvaluationRefused("network or database access during an offline evaluation")
    targets = [(socket, "socket"), (socket, "SocketType"), (socket, "create_connection"), (socket, "getaddrinfo"), (_socket, "socket"),
               (sqlite3, "connect"), (sqlite3, "Connection"), (sqlite3.dbapi2, "connect"), (sqlite3.dbapi2, "Connection")] + \
        ([(_sqlite3, "connect"), (_sqlite3, "Connection")] if _sqlite3 is not None else [])
    saved = [(mod, name, getattr(mod, name)) for mod, name in targets]
    for mod, name, _ in saved:
        setattr(mod, name, refuse)
    try:
        yield attempts
    finally:
        for mod, name, orig in saved:
            setattr(mod, name, orig)


def _out_dir(out: Path) -> Path:
    problem = output_problem(out)
    if problem:
        raise EvaluationRefused(problem)
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise EvaluationRefused(f"{out} is not empty: evidence is never overwritten")
    return out


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str), encoding="utf-8")


def evaluate(manifest_path: str, dataset_path: str, report_path: str, out: str, *, approval: Optional[str] = None,
             approved_manifest_sha256: Optional[str] = None, commit_fn: Optional[Callable[[], Optional[str]]] = None,
             tree_clean_fn: Optional[Callable[[], bool]] = None, tree_fn: Optional[Callable[[], Optional[str]]] = None) -> dict:
    from strategy_validation.dataset import load
    from strategy_validation.engine import git_commit_sha
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    problems = MF.verify(m)
    if problems:
        raise EvaluationRefused(f"the manifest does not verify: {problems}")
    out_dir = _out_dir(Path(out))
    if Path(m["evidence"]["location"]).resolve() != out_dir:
        raise EvaluationRefused(f"{out_dir} is not the manifest's evidence location {m['evidence']['location']}")
    ds = load(str(dataset_path), provenance=(m["dataset"].get("provenance_override") or None))
    if ds.kind == "HISTORICAL" and not (approved(approval) and approved_manifest_sha256 == m["manifest_sha256"]):
        raise EvaluationRefused("a HISTORICAL dataset needs an operator approval reference and the approved manifest's sha256")
    if ds.kind == "HISTORICAL" and os.path.normcase(str(out_dir.parent)) != os.path.normcase(str(Path(EVIDENCE_ROOT).resolve())):
        raise EvaluationRefused(f"a HISTORICAL evaluation writes its evidence in a new folder directly under {EVIDENCE_ROOT} (R17)")
    if ds.kind == "HISTORICAL" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}Z", out_dir.name):
        raise EvaluationRefused(f"a HISTORICAL evidence folder is named by its UTC timestamp (YYYY-MM-DDTHHMMSSZ, R17), not {out_dir.name!r}")
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    commit = (commit_fn or git_commit_sha)()
    tree = (tree_fn or CT.index_tree_or_none)()
    tree_issues = CT.tree_problems() if tree_clean_fn is None else ([] if tree_clean_fn() else ["the supplied tree check says not clean"])
    clean = not tree_issues
    problems = MF.pre_checks(m, dataset_path=dataset_path, dataset=ds, report=report, code_commit=commit, code_tree=tree, tree_clean=clean)
    if problems:
        raise EvaluationRefused(f"pre-checks failed: {problems}" + (f"; working tree: {tree_issues[:20]}" if tree_issues else ""))
    evaluated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created = m["created_at"]
    recs: Dict[Tuple[str, str], dict] = {}
    with offline() as attempts:
        for r in m["experiment"]["runs"]:
            recs[(r["version"], r["cost"])] = json.loads(PL.run(r["version"], r["cost"], ds, created_at=created).model_dump_json())
        train_end = recs[("V1", "BASE")]["train_end"]
        tv = ds.until(train_end)
        neighbours = [(dict(p), raw) for p in SP.SENSITIVITY for raw in SwingTrendPullbackAdapter(dict(p)).candidates(tv)]
    if attempts:
        raise EvaluationRefused(f"{len(attempts)} network or database access attempt(s) during the offline evaluation (caught by the run path)")
    invalid = {f"{v} {c}": MF.post_checks(m, rec, v, c) for (v, c), rec in recs.items()}
    invalid = {k: v for k, v in invalid.items() if v}
    blocking = AN.stop_findings(recs, neighbours) + AN.integrity_findings(recs)
    statuses = [s for s, on in (("INVALID", invalid), ("BLOCKED_VALIDATION_FINDING", blocking)) if on] or ["COMPLETED"]
    status = " + ".join(statuses)
    accepted = statuses == ["COMPLETED"]
    stab = {"V1 BASE": AN.stability(recs[("V1", "BASE")], ds.sessions), "C1 BASE": AN.stability(recs[("C1", "BASE")], ds.sessions),
            "V1 STRESS_2X": AN.stability(recs[("V1", "STRESS_2X")], ds.sessions), "C1 STRESS_2X": AN.stability(recs[("C1", "STRESS_2X")], ds.sessions)}
    comp = AN.comparison(recs, ds.sessions)
    accepted_by = (f"approval reference {approval} supplied with the approved manifest sha256 {m['manifest_sha256']} (a procedural record: the "
                   f"reference is checked for its form only)") if ds.kind == "HISTORICAL" else None                              # SYNTHETIC: none
    elig = AN.eligibility(recs, stab["V1 BASE"], comp, report, blocking, sorted(invalid), findings_accepted_by=accepted_by)
    mark = {"accepted": accepted, "status": status, "note": None if accepted else NOT_ACCEPTED}
    shown = (lambda o: o) if accepted else (lambda o: f"NOT ACCEPTED (diagnostic: {o})")  # noqa: E731
    summary = {"status": status, "statuses": statuses, "outcomes_accepted": accepted, "dataset_kind": ds.kind, "manifest_sha256": m["manifest_sha256"],
               "approval_reference": approval, "approved_manifest_sha256": approved_manifest_sha256, "approval_check": APPROVAL_CHECK,
               "evaluated_at": evaluated_at, "stability_V1": shown(stab["V1 BASE"]["outcome"]), "comparison": shown(comp["outcome"]),
               "eligible_for_review": elig["eligible_for_review"] if accepted else None, "invalid_runs": invalid, "blocking_findings": len(blocking),
               "flags": sorted({*recs[("V1", "BASE")].get("research_flags", []), R12["flag"], FEE_FLAG_MISSING_HISTORY}),
               "note": "hypothetical research results; never a promotion; the edge stays NOT_ESTABLISHED (survivorship)"}
    _dump(out_dir / "manifest.json", m)
    _dump(out_dir / "pre_run_report.json", report)
    (out_dir / "dataset.sha256").write_text(f"{MF.file_sha256(dataset_path)}  {Path(dataset_path).name}\n", encoding="utf-8")
    for (v, c), rec in recs.items():
        _dump(out_dir / "runs" / f"{v}_{c}.json", rec)
    _dump(out_dir / "validation.json", {"status": status, "invalid_runs": invalid, "blocking_findings": blocking,
                                        "rule": "M-5: a stop <= 0 blocks acceptance; the stop calculation and the rules are never modified"})
    _dump(out_dir / "stability.json", {**mark, "runs": stab})
    _dump(out_dir / "comparison.json", {**comp, **mark})
    _dump(out_dir / "eligibility.json", {**elig, "accepted": accepted, "status": status, "acceptance_note": mark["note"]})
    _dump(out_dir / "environment.json", {"python": sys.version.split()[0], "platform": platform.platform(), "evaluator": EVALUATOR_VERSION,
                                          "code_commit": commit, "code_tree": tree, "tree_clean": clean, "tree_check": CT.TREE_CHECK,
                                          "evaluated_at": evaluated_at, "approval_reference": approval, "approval_check": APPROVAL_CHECK,
                                          "approved_manifest_sha256": approved_manifest_sha256, "fees": R12})
    _dump(out_dir / "summary.json", summary)
    (out_dir / "RESULTS_SUMMARY.md").write_text(_memo(summary, stab, comp), encoding="utf-8")
    lines = []
    for p in sorted(x for x in out_dir.rglob("*") if x.is_file() and x.name != "evidence.sha256"):
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out_dir).as_posix()}")
    (out_dir / "evidence.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def _memo(summary: dict, stab: dict, comp: dict) -> str:
    accepted = summary["outcomes_accepted"]
    head = "Outcomes ACCEPTED (status COMPLETED)." if accepted else f"Outcomes NOT ACCEPTED: status {summary['status']}. {NOT_ACCEPTED}."
    tag = "" if accepted else "NOT ACCEPTED (diagnostic only): "
    return "\n".join([
        "# Swing trend pullback V1 / C1 - evaluation summary", "",
        f"Dataset kind: {summary['dataset_kind']}. Manifest: `{summary['manifest_sha256']}`. {head}", "",
        f"Approval: {summary['approval_reference'] or 'none (SYNTHETIC)'}; approved manifest: {summary['approved_manifest_sha256'] or 'none'}; "
        f"evaluated at {summary['evaluated_at']}. The approval is a procedural record ({APPROVAL_CHECK})", "",
        f"* R9 fold stability (V1 BASE): {tag}{stab['V1 BASE']['outcome']}",
        f"* R10 V1 versus C1: {tag}{comp['outcome']} - {comp['why']}",
        f"* R11 eligible for review: {summary['eligible_for_review']}",
        f"* Flags: {', '.join(summary['flags'])}", "",
        "Hypothetical research results on a fixed universe of today's large caps (survivorship bias present): the edge is NOT_ESTABLISHED. "
        "The 0.05 R threshold is a materiality threshold, not a significance test; fold consistency includes TRAIN observations; NO_ADDED_VALUE "
        "means only that V1 did not show an improvement under the pre-registered mean-R comparison. Eligible for review is never a promotion.", ""])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m swing_eval.evaluate", description="the offline V1 / C1 evaluator")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--approval", default=None)
    ap.add_argument("--approved-manifest-sha256", default=None)
    a = ap.parse_args(argv)
    try:
        s = evaluate(a.manifest, a.dataset, a.report, a.out, approval=a.approval, approved_manifest_sha256=a.approved_manifest_sha256)
    except EvaluationRefused as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(json.dumps({k: s[k] for k in ("status", "outcomes_accepted", "stability_V1", "comparison", "eligible_for_review")}))
    return 0 if s["status"] == "COMPLETED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
