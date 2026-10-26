"""strategy_validation/cli.py — the research CLI (python -m strategy_validation ...). It never imports a broker, an order writer or the executor, and it
cannot launch anything from the browser: research runs are made here, on purpose, by a person.

  validate --strategy ID --dataset FILE [--train-ratio 0.7] [--arms rules,gated,model] [--output run.json] [--persist [--db PATH]] [--neighbors params.json]
           [--provenance provenance.json]
      exit 0  a run completed (any verdict: a FAIL is a valid result); with --persist it was ELIGIBLE and is stored
      exit 1  an input error (dataset, split, arms, provenance file)          exit 2  NOT_RUN: no honest historical replay / not implemented
      exit 3  --persist was given but the run was NOT stored: the integrity gate BLOCKED it (or the store refused it); the reasons are printed
  runs [--strategy ID] [--db PATH]     the stored runs, newest first
  evaluate --run RUN_ID --dataset FILE [--db PATH] [--persist] [--output evaluation.json]
      Stage 6.4.1: a validation_v3 METHODOLOGY EVALUATION of a stored cross-sectional run - the selection edge against the declared control, per
      rebalance period. The run is never rewritten; the snapshots are re-derived from the same dataset and must reproduce its candidate set.
      exit 0 evaluated (with --persist: stored) · 1 input error / cannot be evaluated honestly · 3 --persist refused by the store

A run is persisted only with --persist, and only when the Stage 6.3.1 integrity gate finds it ELIGIBLE (strategy_validation/integrity.py) - a
blocked --persist is never silently downgraded to an ephemeral run. Without --persist a run is ephemeral (research / debug) and its integrity
result is printed. --provenance adds the declared dataset provenance (source, retrieved_at, bar_frequency, adjustment, missing_data_policy, ...)
for a file format that cannot carry it (CSV). MODEL is NOT_EVALUATED: no model decider is configured (no provider, no external call)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from strategy_validation import ARMS, DEFAULT_TRAIN_RATIO


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m strategy_validation", description="Stage 6.2 strategy validation harness (research only; never orders)")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate one registered strategy on a dataset file")
    v.add_argument("--strategy", required=True)
    v.add_argument("--dataset", required=True, help="CSV (symbol,date,open,high,low,close[,volume]) or JSON {kind, bars} of completed daily sessions")
    v.add_argument("--train-ratio", type=float, default=DEFAULT_TRAIN_RATIO, help="chronological development share (the rest is the untouched OOS slice)")
    v.add_argument("--arms", default="rules,gated,model")
    v.add_argument("--output", default="", help="write the full run record as JSON")
    v.add_argument("--persist", action="store_true", help="store the run (append-only) so the Strategy Lab shows it")
    v.add_argument("--db", default="", help="database file (default: the app database)")
    v.add_argument("--neighbors", default="", help="JSON list of explicitly supplied neighbouring parameter sets (diagnostic only; never chosen)")
    v.add_argument("--provenance", default="", help="JSON object of declared dataset provenance (required for persistence when the dataset file has none)")
    r = sub.add_parser("runs", help="list stored validation runs")
    r.add_argument("--strategy", default="")
    r.add_argument("--db", default="")
    e = sub.add_parser("evaluate", help="validation_v3 methodology evaluation of a stored cross-sectional run (the run is never rewritten)")
    e.add_argument("--run", required=True, help="the stored run_id")
    e.add_argument("--dataset", required=True, help="the run's dataset file (its content hash must equal the run's dataset_id)")
    e.add_argument("--db", default="")
    e.add_argument("--persist", action="store_true", help="store the evaluation (append-only) so the Strategy Lab shows it")
    e.add_argument("--output", default="", help="write the evaluation record as JSON")
    return p


def _evaluate(args, out) -> int:
    from strategy_registry import registry as R
    from strategy_validation import cross_sectional as CS
    from strategy_validation import engine as E
    from strategy_validation.adapters import adapter_for
    from strategy_validation.dataset import DatasetError, load
    from strategy_validation.store import StoreError, StrategyValidationStore
    store = StrategyValidationStore(Path(args.db) if args.db else None)
    run = store.run(args.run)
    if run is None:
        print(f"ERROR no visible stored run {args.run}", file=out)
        return 1
    adapter, reason = adapter_for(run["strategy_id"], R.get(run["strategy_id"]))
    if adapter is None:
        print(f"ERROR {run['strategy_id']}: {reason}", file=out)
        return 1
    try:
        dataset = load(args.dataset)
        ev = CS.assess_stored_run(run, store.candidates(args.run), store.trades(args.run, "RULES"), dataset, adapter, registry=R.by_id(), git_sha=E.git_commit_sha())
    except (DatasetError, CS.EvaluationError, ValueError, OSError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=out)
        return 1
    cs, edge, v = ev["cross_sectional"], ev["selection_edge"], ev["verdict"]
    print(f"{ev['strategy_id']} v{ev['strategy_version']} run {ev['run_id']} ({ev['evaluated_run_validation_version']}) -> {ev['methodology_version']}: "
          f"selection edge {edge['status']}; overall {v['label']} (absolute performance {ev['performance_verdict'].get('label')})", file=out)
    for seg in ("TRAIN", "OOS", "ALL"):
        s = cs["segments"].get(seg) or {}
        print(f"  {seg}: {s.get('period_count')} periods, strategy {s.get('strategy_total_return')}, control {s.get('control_total_return')}, "
              f"market benchmark {s.get('benchmark_total_return')}, mean excess {s.get('mean_excess_period_return')}, t {s.get('excess_t_stat')}", file=out)
    print(f"  {cs['parity']['note']}; reasons: " + "; ".join(r["text"] for r in edge["reasons"]), file=out)
    if args.output:
        Path(args.output).write_text(json.dumps(ev, indent=1), encoding="utf-8")
    if args.persist:
        try:
            store.insert_evaluation(ev)
        except StoreError as exc:
            print(f"  NOT STORED - {exc}", file=out)
            return 3
        print(f"  stored evaluation {ev['evaluation_id']} (run {ev['run_id']} unchanged)", file=out)
    return 0


def main(argv: Optional[List[str]] = None, adapters: Optional[Dict[str, object]] = None, out=None) -> int:
    """`adapters` is a test seam only (fixture adapters never reach the registry); from the command line it is always None."""
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    from strategy_validation.store import PersistenceBlocked, StoreError, StrategyValidationStore
    if args.cmd == "evaluate":
        return _evaluate(args, out)
    if args.cmd == "runs":
        store = StrategyValidationStore(Path(args.db) if args.db else None)
        for row in store.runs(args.strategy.upper() or None):
            print(f"{row['created_at']}  {row['run_id']}  {row['strategy_id']} v{row['strategy_version']}  {row['verdict']}  {row['dataset_kind']}  {row['candidate_count']} candidates", file=out)
        return 0
    from strategy_registry import registry as R
    from strategy_validation import engine as E
    from strategy_validation.adapters import adapter_for
    from strategy_validation.dataset import DatasetError, load
    sid = args.strategy.upper()
    adapter = (adapters or {}).get(sid)
    registry = None if adapter is not None else R.by_id()                       # a test-seam fixture is not a registered strategy
    if adapter is None:
        entry = R.get(sid)
        adapter, reason = adapter_for(sid, entry)
        if adapter is None:
            print(f"NOT_RUN {sid}: {reason or 'unknown strategy'}", file=out)
            if args.output:
                Path(args.output).write_text(json.dumps({"strategy_id": sid, "status": "NOT_RUN", "reason": reason}, indent=1), encoding="utf-8")
            return 2
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    if any(a not in ARMS for a in arms):
        print(f"ERROR arms must be among {', '.join(ARMS)}", file=out)
        return 1
    try:
        prov = json.loads(Path(args.provenance).read_text(encoding="utf-8")) if args.provenance else None
        if prov is not None and not isinstance(prov, dict):
            raise ValueError("--provenance must be a JSON object")
        dataset = load(args.dataset, provenance=prov)
        neighbours = json.loads(Path(args.neighbors).read_text(encoding="utf-8")) if args.neighbors else None
        run = E.run_validation(adapter, dataset, train_ratio=args.train_ratio, arms=arms, gates=(), model=None, neighbours=neighbours,
                               adapter_factory=getattr(adapter, "with_params", None), registry=registry)
    except (DatasetError, E.ValidationError, ValueError, OSError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=out)
        return 1
    v = run.verdict
    m = run.arms["RULES"].metrics or {}
    print(f"{run.strategy_id} v{run.strategy_version}: {v['label']} - {'; '.join(r['text'] for r in v['reasons'])}", file=out)
    for seg in ("TRAIN", "OOS"):
        s = m.get(seg) or {}
        print(f"  {seg}: {s.get('trade_count')} trades, net {s.get('net_pnl')} (gross {s.get('gross_pnl')}), mean R {s.get('expectancy_R')}, t {s.get('mean_R_t_stat')}, "
              f"max drawdown {s.get('max_drawdown')}", file=out)
    print(f"  audit: {run.audit.note}; decision: {v['decision']}", file=out)
    if run.evaluation_mode == "CROSS_SECTIONAL_PORTFOLIO":                      # Stage 6.4.1: the selection edge is a separate result
        o = (run.cross_sectional.get("segments") or {}).get("OOS") or {}
        print(f"  selection edge (vs {run.cross_sectional.get('control', {}).get('label')}): {run.selection_edge.get('status')} - OOS {o.get('period_count')} periods, "
              f"mean excess {o.get('mean_excess_period_return')}, t {o.get('excess_t_stat')}; absolute performance {run.performance_verdict.get('label')}", file=out)
    el = run.eligibility
    print(f"  integrity: {el['integrity_status']} - persistence {el['status']}; accounting {run.accounting.get('status')}; "
          f"{run.boundary.get('train_boundary_purged_count', 0)} TRAIN outcome(s) purged at the TRAIN / OOS boundary", file=out)
    for r in el["reasons"]:
        print(f"    blocked by {r['code']}: {r['text']}", file=out)
    if args.output:
        Path(args.output).write_text(run.model_dump_json(indent=1), encoding="utf-8")
    if args.persist:
        try:
            StrategyValidationStore(Path(args.db) if args.db else None).insert(run)
        except PersistenceBlocked as exc:
            print(f"  NOT STORED - persistence BLOCKED by the integrity gate ({', '.join(r['code'] for r in exc.eligibility.reasons)})", file=out)
            return 3
        except StoreError as exc:
            print(f"  NOT STORED - {exc}", file=out)
            return 3
        print(f"  stored run {run.run_id}", file=out)
    else:
        print("  ephemeral run (not stored; use --persist to store an ELIGIBLE run)", file=out)
    return 0
