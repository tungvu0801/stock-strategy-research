"""swing_eval/manifest.py — the versioned evaluation manifest (decision lock R14): one frozen JSON document, with its own sha256, that pins
everything the strategy's pre-registration hash does not cover. The strategy hash is referenced, never changed.

    python -m swing_eval.manifest build --dataset FILE --report REPORT.json --out MANIFEST.json [--evidence-location DIR] [--provenance OVERRIDE.json]
                                        [--created-at ISO]

verify()      the manifest agrees with the strategy code (pre-registration hash, the four runs and their friction) and with the locked values
pre_checks()  before any run: the code's hash and commit, a clean tree, the dataset file's sha256 and content, the accepted report
post_checks() after each run: the run record's friction, commission, fee, split, sizing, arms, adapter, execution assumptions, cost label,
              pre-registration hash, dataset and harness version equal the manifest's (final-review M-2 / M-3). Any mismatch: the run is INVALID."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import List, Optional

import swing_pullback as SP
import strategy_validation as SV
from swing_eval import ARMS, EVALUATOR_VERSION, EVIDENCE_ROOT, EXECUTION, LOCK, MANIFEST_VERSION, R9, R10, R11, R12, R13, SIZING, TRAIN_RATIO
from swing_pullback import plan as PL

VERSIONS = {"V1": (SP.STRATEGY_ID, SP.STRATEGY_VERSION), "C1": (SP.CONTROL_ID, SP.CONTROL_VERSION)}


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_sha256(m: dict) -> str:
    return hashlib.sha256(_canon({k: v for k, v in m.items() if k != "manifest_sha256"})).hexdigest()


def file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runs() -> List[dict]:
    return [{"version": v, "strategy": list(VERSIONS[v]), "cost": c, "friction": PL.friction(c)} for v, c in PL.RUNS]


def build(*, dataset_path: str, report: dict, code_commit: str, created_at: str, evidence_location: str, provenance: Optional[dict] = None) -> dict:
    from strategy_validation.dataset import load
    ds = load(str(dataset_path), provenance=provenance)
    m = {"manifest_version": MANIFEST_VERSION, "evaluator": EVALUATOR_VERSION, "lock": LOCK, "created_at": created_at,
         "strategy": {"v1": list(VERSIONS["V1"]), "c1": list(VERSIONS["C1"]), "preregistration_hash": SP.preregistration_hash(), "code_commit": code_commit,
                      "harness_validation_version": SV.VERSION},
         "dataset": {"file_sha256": file_sha256(dataset_path), "kind": ds.kind, "dataset_id": ds.dataset_id, "volume_feed": ds.declared.get("volume_feed"),
                     "provenance_override": dict(provenance or {}), "builder": ds.declared.get("builder"),
                     "expected": {"symbols": len(ds.bars), "sessions": len(ds.sessions), "start": ds.start, "end": ds.end}},
         "pre_run_report": {"report_sha256": report.get("report_sha256"), "status": report.get("status"),
                            "dataset_sha256": (report.get("dataset") or {}).get("sha256")},
         "experiment": {"train_ratio": TRAIN_RATIO, "boundary_policy": SV.BOUNDARY_POLICY, "arms": list(ARMS), "execution": dict(EXECUTION),
                        "sizing": dict(SIZING), "runs": runs()},
         "thresholds": {"R9": R9, "R10": R10, "R11": list(R11)},
         "fees": {"R12": R12, "R13": R13},
         "evidence": {"location": str(evidence_location)}}
    m["manifest_sha256"] = manifest_sha256(m)
    return m


def verify(m: dict) -> List[str]:
    """Structure, own hash, and agreement with the strategy code and the locked values."""
    p: List[str] = []
    if m.get("manifest_version") != MANIFEST_VERSION:
        p.append(f"manifest_version {m.get('manifest_version')!r} is not {MANIFEST_VERSION}")
    if m.get("manifest_sha256") != manifest_sha256(m):
        p.append("manifest_sha256 does not match the manifest's content")
    st = m.get("strategy") or {}
    if st.get("preregistration_hash") != SP.preregistration_hash():
        p.append("the strategy's pre-registration hash differs from the code's")
    if [st.get("v1"), st.get("c1")] != [list(VERSIONS["V1"]), list(VERSIONS["C1"])]:
        p.append("the strategy ids / versions differ")
    if st.get("harness_validation_version") != SV.VERSION:
        p.append("the harness validation version differs")
    ex = m.get("experiment") or {}
    if ex.get("runs") != runs():
        p.append("the runs are not exactly the four pre-registered runs with their friction")
    if (ex.get("train_ratio"), ex.get("arms"), ex.get("execution"), ex.get("sizing"), ex.get("boundary_policy")) != (TRAIN_RATIO, ARMS, EXECUTION, SIZING,
                                                                                                                      SV.BOUNDARY_POLICY):
        p.append("the split, arms, execution assumptions, sizing or boundary policy differ from the lock")
    if m.get("thresholds") != {"R9": R9, "R10": R10, "R11": list(R11)}:
        p.append("the R9-R11 thresholds differ from the lock")
    if m.get("fees") != {"R12": R12, "R13": R13}:
        p.append("the R12 / R13 fee records differ from the lock")
    d = m.get("dataset") or {}
    if d.get("volume_feed") not in ("SIP", "IEX") or not d.get("file_sha256") or not d.get("dataset_id"):
        p.append("the dataset identity or its volume feed is missing")
    r = m.get("pre_run_report") or {}
    if not r.get("report_sha256") or r.get("dataset_sha256") != d.get("file_sha256"):
        p.append("the pre-run report is missing or was made on another dataset file")
    if r.get("status") not in ("CLEAN", "REVIEW_REQUIRED"):
        p.append(f"the pre-run report's status {r.get('status')!r} does not allow a run")
    if not (m.get("evidence") or {}).get("location"):
        p.append("no evidence location")
    return p


def pre_checks(m: dict, *, dataset_path: str, dataset, report: dict, code_commit: Optional[str], tree_clean: bool) -> List[str]:
    p = verify(m)
    st, d = m.get("strategy") or {}, m.get("dataset") or {}
    if not code_commit or code_commit != st.get("code_commit"):
        p.append("the checked-out commit differs from the manifest's code_commit")
    if not tree_clean:
        p.append("the working tree is not clean")
    if file_sha256(dataset_path) != d.get("file_sha256"):
        p.append("the dataset file's sha256 differs from the manifest's")
    exp = d.get("expected") or {}
    got = {"symbols": len(dataset.bars), "sessions": len(dataset.sessions), "start": dataset.start, "end": dataset.end}
    if got != exp:
        p.append(f"the dataset's content differs from the manifest's expectation: {got} vs {exp}")
    if dataset.dataset_id != d.get("dataset_id") or dataset.kind != d.get("kind") or dataset.declared.get("volume_feed") != d.get("volume_feed"):
        p.append("the dataset id, kind or volume feed differs from the manifest's")
    from research_data.quality import report_sha256
    if report.get("report_sha256") != report_sha256(report) or report.get("report_sha256") != (m.get("pre_run_report") or {}).get("report_sha256"):
        p.append("the pre-run report is not the accepted one (its sha256 differs)")
    if report.get("status") == "BLOCKED":
        p.append("the pre-run report is BLOCKED")
    return p


def post_checks(m: dict, rec: dict, version: str, cost: str) -> List[str]:
    """The run record against the manifest. Any problem makes the run INVALID (never repaired)."""
    p: List[str] = []
    run = next((r for r in m["experiment"]["runs"] if r["version"] == version and r["cost"] == cost), None)
    if run is None:
        return [f"{version} {cost} is not a manifest run"]
    sid, sv = run["strategy"]
    if (rec.get("strategy_id"), rec.get("strategy_version")) != (sid, sv):
        p.append("strategy id / version")
    if rec.get("validation_version") != m["strategy"]["harness_validation_version"]:
        p.append("harness validation version")
    if rec.get("dataset_id") != m["dataset"]["dataset_id"]:
        p.append("dataset id")
    if (rec.get("provenance") or {}).get("volume_feed") != m["dataset"]["volume_feed"]:
        p.append("volume feed")
    if rec.get("friction_model") != run["friction"]:
        p.append(f"friction {rec.get('friction_model')} != {run['friction']} (commission / fee / slippage)")
    cs = rec.get("config_snapshot") or {}
    split = cs.get("split") or {}
    if (split.get("method"), split.get("train_ratio")) != ("CHRONOLOGICAL", m["experiment"]["train_ratio"]):
        p.append("split")
    if cs.get("sizing") != m["experiment"]["sizing"]:
        p.append(f"sizing {cs.get('sizing')}")
    if rec.get("arms_run") != m["experiment"]["arms"]:
        p.append("arms")
    a = cs.get("adapter") or {}
    ex = m["experiment"]["execution"]
    if (a.get("strategy_id"), a.get("strategy_version"), a.get("params"), a.get("max_hold_bars"), a.get("entry_policy"), a.get("time_exit_fill"),
            a.get("stop_policy")) != (sid, sv, {}, SP.MAX_HOLD_BARS, ex["entry_policy"], ex["time_exit_fill"], ex["stop_policy"]):
        p.append("adapter description (params, holding limit, entry, time-exit fill, stop policy)")
    ea = rec.get("execution_assumptions") or {}
    if (ea.get("entry_policy"), ea.get("ambiguous_bar_policy")) != (ex["entry_policy"], ex["ambiguous_bar_policy"]):
        p.append("execution assumptions")
    sd = ((rec.get("diagnostics") or {}).get("strategy") or {})
    if sd.get("preregistration_hash") != m["strategy"]["preregistration_hash"]:
        p.append("pre-registration hash in the run")
    if (sd.get("costs") or {}).get("preregistered_run") != cost:
        p.append(f"cost label {(sd.get('costs') or {}).get('preregistered_run')!r} != {cost}")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m swing_eval.manifest", description="build an evaluation manifest (offline; for the operator's approval)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dataset", required=True)
    b.add_argument("--report", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--evidence-location", default=None, help="default: EVIDENCE_ROOT/<UTC timestamp> (R17)")
    b.add_argument("--provenance", default=None)
    b.add_argument("--created-at", default=None)
    a = ap.parse_args(argv)
    from datetime import datetime, timezone
    from strategy_validation.engine import git_commit_sha
    rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
    prov = json.loads(Path(a.provenance).read_text(encoding="utf-8")) if a.provenance else None
    created = a.created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    location = a.evidence_location or str(Path(EVIDENCE_ROOT) / created.replace(":", "").replace("+0000", "Z"))
    m = build(dataset_path=a.dataset, report=rep, code_commit=git_commit_sha() or "", provenance=prov, evidence_location=location, created_at=created)
    out = Path(a.out)
    if out.exists():
        print(f"REFUSED: {out} exists (a manifest is never overwritten)")
        return 2
    out.write_text(json.dumps(m, indent=1, sort_keys=True), encoding="utf-8")
    problems = verify(m)
    print(f"manifest sha256 {m['manifest_sha256']}" + (f" - PROBLEMS: {problems}" if problems else " - verified"))
    return 0 if not problems else 3


if __name__ == "__main__":
    raise SystemExit(main())
