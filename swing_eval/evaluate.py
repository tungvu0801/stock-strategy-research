"""swing_eval/evaluate.py — the OFFLINE evaluator of V1 / C1 (decision lock R14-R17, R22).

    python -m swing_eval.evaluate --manifest MANIFEST.json --dataset FILE --report REPORT.json --out DIR
                                  [--approval "OPERATOR-APPROVAL:<ref>" --approved-manifest-sha256 HASH]

Steps, each fail-closed:
  1. the manifest verifies (its own hash; the strategy code's pre-registration hash, runs and friction; the locked values)
  2. the output folder is the manifest's evidence location, outside the repository and new (or empty); nothing is overwritten
  3. a HISTORICAL dataset needs an operator approval reference and the approved manifest's sha256 (SYNTHETIC datasets: tests)
  4. pre-checks: the checked-out commit, a clean working tree, the dataset file's sha256 and content, the accepted pre-run report
  5. the four pre-registered runs of swing_pullback.plan.run(), with every socket and SQLite connection refused while they execute
  6. post-checks of every run record against the manifest: a mismatch makes that run INVALID (never repaired)
  7. blocking validation findings: a stop <= 0 in any candidate of the four runs or of the eight TRAIN-view neighbours (M-5), a CRITICAL
     causality report, a failed audit, a candidate-set mismatch. Any of them: status BLOCKED_VALIDATION_FINDING, and no outcome is accepted
  8. R9 stability, R10 comparison, R11 eligibility (reported as ACCEPTED only when the status is COMPLETED)
  9. the evidence: JSON files + evidence.sha256 + a plain-language summary; never a database, never a run record changed"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import platform
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import swing_pullback as SP
from research_data import approved
from swing_eval import EVALUATOR_VERSION, FEE_FLAG_MISSING_HISTORY, R12
from swing_eval import analysis as AN
from swing_eval import manifest as MF
from swing_pullback import plan as PL
from swing_pullback.adapter import SwingTrendPullbackAdapter

ROOT = Path(__file__).resolve().parents[1]


class EvaluationRefused(PermissionError):
    pass


def tree_clean(root: Path = ROOT) -> bool:
    """git status --porcelain is empty (no modified, staged or untracked file)."""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    return r.returncode == 0 and not r.stdout.strip()


@contextlib.contextmanager
def offline():
    """Every socket and SQLite connection is refused while the runs execute (restored afterwards)."""
    def refuse(*a, **k):
        raise EvaluationRefused("network or database access during an offline evaluation")
    saved = (socket.socket, socket.create_connection, socket.getaddrinfo, sqlite3.connect)
    socket.socket = socket.create_connection = socket.getaddrinfo = sqlite3.connect = refuse      # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket, socket.create_connection, socket.getaddrinfo, sqlite3.connect = saved


def _out_dir(out: Path) -> Path:
    out = Path(out).resolve()
    try:
        out.relative_to(ROOT)
        raise EvaluationRefused(f"the evidence folder {out} is inside the repository (R17)")
    except ValueError:
        pass
    if out.exists() and any(out.iterdir()):
        raise EvaluationRefused(f"{out} is not empty: evidence is never overwritten")
    return out


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str), encoding="utf-8")


def evaluate(manifest_path: str, dataset_path: str, report_path: str, out: str, *, approval: Optional[str] = None,
             approved_manifest_sha256: Optional[str] = None, commit_fn: Optional[Callable[[], Optional[str]]] = None,
             tree_clean_fn: Optional[Callable[[], bool]] = None) -> dict:
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
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    commit = (commit_fn or git_commit_sha)()
    clean = (tree_clean_fn or tree_clean)()
    problems = MF.pre_checks(m, dataset_path=dataset_path, dataset=ds, report=report, code_commit=commit, tree_clean=clean)
    if problems:
        raise EvaluationRefused(f"pre-checks failed: {problems}")
    created = m["created_at"]
    recs: Dict[Tuple[str, str], dict] = {}
    with offline():
        for r in m["experiment"]["runs"]:
            recs[(r["version"], r["cost"])] = json.loads(PL.run(r["version"], r["cost"], ds, created_at=created).model_dump_json())
        train_end = recs[("V1", "BASE")]["train_end"]
        tv = ds.until(train_end)
        neighbours = [(dict(p), raw) for p in SP.SENSITIVITY for raw in SwingTrendPullbackAdapter(dict(p)).candidates(tv)]
    invalid = {f"{v} {c}": MF.post_checks(m, rec, v, c) for (v, c), rec in recs.items()}
    invalid = {k: v for k, v in invalid.items() if v}
    blocking = AN.stop_findings(recs, neighbours) + AN.integrity_findings(recs)
    status = "INVALID" if invalid else ("BLOCKED_VALIDATION_FINDING" if blocking else "COMPLETED")
    stab = {"V1 BASE": AN.stability(recs[("V1", "BASE")], ds.sessions), "C1 BASE": AN.stability(recs[("C1", "BASE")], ds.sessions),
            "V1 STRESS_2X": AN.stability(recs[("V1", "STRESS_2X")], ds.sessions), "C1 STRESS_2X": AN.stability(recs[("C1", "STRESS_2X")], ds.sessions)}
    comp = AN.comparison(recs, ds.sessions)
    elig = AN.eligibility(recs, stab["V1 BASE"], comp, report, blocking, sorted(invalid))
    accepted = status == "COMPLETED"
    summary = {"status": status, "outcomes_accepted": accepted, "dataset_kind": ds.kind, "manifest_sha256": m["manifest_sha256"],
               "stability_V1": stab["V1 BASE"]["outcome"], "comparison": comp["outcome"], "eligible_for_review": elig["eligible_for_review"] if accepted else None,
               "invalid_runs": invalid, "blocking_findings": len(blocking),
               "flags": sorted({*recs[("V1", "BASE")].get("research_flags", []), R12["flag"], FEE_FLAG_MISSING_HISTORY}),
               "note": "hypothetical research results; never a promotion; the edge stays NOT_ESTABLISHED (survivorship)"}
    _dump(out_dir / "manifest.json", m)
    _dump(out_dir / "pre_run_report.json", report)
    (out_dir / "dataset.sha256").write_text(f"{MF.file_sha256(dataset_path)}  {Path(dataset_path).name}\n", encoding="utf-8")
    for (v, c), rec in recs.items():
        _dump(out_dir / "runs" / f"{v}_{c}.json", rec)
    _dump(out_dir / "validation.json", {"status": status, "invalid_runs": invalid, "blocking_findings": blocking,
                                        "rule": "M-5: a stop <= 0 blocks acceptance; the stop calculation and the rules are never modified"})
    _dump(out_dir / "stability.json", stab)
    _dump(out_dir / "comparison.json", comp)
    _dump(out_dir / "eligibility.json", {**elig, "accepted": accepted})
    _dump(out_dir / "environment.json", {"python": sys.version.split()[0], "platform": platform.platform(), "evaluator": EVALUATOR_VERSION,
                                          "code_commit": commit, "tree_clean": clean, "fees": R12})
    _dump(out_dir / "summary.json", summary)
    (out_dir / "RESULTS_SUMMARY.md").write_text(_memo(summary, stab, comp, elig), encoding="utf-8")
    lines = []
    for p in sorted(x for x in out_dir.rglob("*") if x.is_file() and x.name != "evidence.sha256"):
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out_dir).as_posix()}")
    (out_dir / "evidence.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def _memo(summary: dict, stab: dict, comp: dict, elig: dict) -> str:
    accepted = summary["outcomes_accepted"]
    head = "Outcomes ACCEPTED (status COMPLETED)." if accepted else f"Outcomes NOT accepted: status {summary['status']}."
    return "\n".join([
        "# Swing trend pullback V1 / C1 - evaluation summary", "",
        f"Dataset kind: {summary['dataset_kind']}. Manifest: `{summary['manifest_sha256']}`. {head}", "",
        f"* R9 fold stability (V1 BASE): {stab['V1 BASE']['outcome']}",
        f"* R10 V1 versus C1: {comp['outcome']} - {comp['why']}",
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
