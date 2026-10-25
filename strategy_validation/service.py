"""strategy_validation/service.py — read views of the stored validation runs for the Strategy Registry and the Strategy Lab (GET only).

summary()  the registry fields: validation_status (NOT_RUN or the latest verdict), latest_validation_run_id, validation_verdict, train / OOS trade counts,
           mechanical_audit_status, last_validated_at - plus the NOT_RUN reason. Never a lifecycle change.
detail()   the Strategy Lab Validation section: the run's period and split, TRAIN / OOS / ALL metrics of the RULES arm, the arm comparison (candidate
           counts side by side; MODEL "Not evaluated" when no decider ran), friction, execution assumptions, causality, the audit, diagnostics, warnings
           and the decision line. With no run: "Not yet evaluated" and why - nothing is estimated or filled in.
Stage 6.3.1: integrity_status, persistence_eligibility, boundary_purged_count and accounting_status come from the stored record (validation_v2).
A Stage 6.2 record (validation_v1) has none of them: they read None with integrity_recorded False - never back-filled, never assumed."""
from __future__ import annotations

from typing import Dict, Optional

from strategy_validation import DISCLAIMER, MIN_OOS_SESSIONS, MIN_OOS_TRADES, MIN_TRADES, T_STAT_WEAK, VERDICT_LABELS, VERSION
from strategy_validation.adapters import not_run_reason

NOT_EVALUATED = "Not yet evaluated"
SHOWN = ("trade_count", "candidate_count", "accepted_count", "stand_aside_count", "win_rate", "gross_pnl", "slippage_cost", "fees", "friction_total", "net_pnl",
         "average_trade_return", "average_R", "average_R_gross", "sum_R", "expectancy_R", "expectancy_usd", "profit_factor", "profit_factor_note", "max_drawdown",
         "max_drawdown_pct", "sharpe_per_trade", "sharpe_note", "average_holding_bars", "best_trade", "worst_trade", "exit_reason_distribution", "mean_R", "std_R",
         "mean_R_t_stat", "sample_size", "ambiguous_bars_count", "size_status", "configured_risk_total", "achieved_risk_total", "simulation_skipped_count",
         "simulated_trade_count", "boundary_purged_count")
POLICY = {"min_trades": MIN_TRADES, "min_oos_trades": MIN_OOS_TRADES, "min_oos_sessions": MIN_OOS_SESSIONS, "t_stat_weak": T_STAT_WEAK, "validation_version": VERSION}


def _m(metrics: Optional[dict], seg: str) -> Optional[dict]:
    if not metrics or seg not in metrics:
        return None
    return {k: metrics[seg].get(k) for k in SHOWN}


def integrity_fields(run: Optional[dict]) -> dict:
    """The Stage 6.3.1 integrity metadata of a stored run, or None everywhere (no run, or a Stage 6.2 record that never had them)."""
    el = (run or {}).get("eligibility") or {}
    if not el:
        return {"integrity_recorded": False, "integrity_status": None, "persistence_eligibility": None, "boundary_purged_count": None, "accounting_status": None}
    return {"integrity_recorded": True, "integrity_status": el.get("integrity_status"), "persistence_eligibility": el.get("status"),
            "boundary_purged_count": ((run.get("boundary") or {}).get("train_boundary_purged_count")), "accounting_status": (run.get("accounting") or {}).get("status")}


def summary(run: Optional[dict], strategy_id: str, entry=None) -> dict:
    if not run:
        return {"validation_status": "NOT_RUN", "validation_label": NOT_EVALUATED, "not_run_reason": not_run_reason(strategy_id, entry), "latest_validation_run_id": None,
                "validation_verdict": None, "train_trade_count": None, "oos_trade_count": None, "mechanical_audit_status": None, "last_validated_at": None, "validation_synthetic": False,
                **integrity_fields(None)}
    rules = (run.get("arms") or {}).get("RULES") or {}
    m = rules.get("metrics") or {}
    v = (run.get("verdict") or {}).get("verdict")
    return {"validation_status": v, "validation_label": VERDICT_LABELS.get(v, v), "not_run_reason": "", "latest_validation_run_id": run["run_id"], "validation_verdict": v,
            "train_trade_count": (m.get("TRAIN") or {}).get("trade_count"), "oos_trade_count": (m.get("OOS") or {}).get("trade_count"),
            "mechanical_audit_status": (run.get("audit") or {}).get("status"), "last_validated_at": run.get("created_at"), "validation_synthetic": run.get("dataset_kind") == "SYNTHETIC",
            **integrity_fields(run)}


def detail(run: Optional[dict], strategy_id: str, entry=None) -> dict:
    s = summary(run, strategy_id, entry)
    base = {**s, "version": VERSION, "policy": POLICY, "disclaimer": DISCLAIMER, "lifecycle_effect": "none: validation never changes a lifecycle status"}
    if not run:
        return {**base, "run": None}
    arms = {}
    for name in ("RULES", "GATED", "MODEL"):
        a = (run.get("arms") or {}).get(name)
        if a is None:
            arms[name] = {"status": "NOT_RUN", "label": "Not run"}
            continue
        m = a.get("metrics") or {}
        arms[name] = {"status": a.get("status"), "label": "Not evaluated" if a.get("status") == "NOT_EVALUATED" else (a.get("verdict") or {}).get("label"),
                      "note": a.get("note", ""), "gates": a.get("gates") or [], "candidate_count": len(a.get("input_candidate_ids") or []),
                      "TRAIN": _m(m, "TRAIN"), "OOS": _m(m, "OOS"), "ALL": _m(m, "ALL"), "verdict": a.get("verdict")}
    au = run.get("audit") or {}
    el = run.get("eligibility") or {}
    return {**base, "run": {
        "run_id": run["run_id"], "created_at": run["created_at"], "strategy_version": run["strategy_version"], "dataset_id": run["dataset_id"], "dataset_kind": run["dataset_kind"],
        "dataset_start": run["dataset_start"], "dataset_end": run["dataset_end"], "train_start": run["train_start"], "train_end": run["train_end"], "oos_start": run.get("oos_start"),
        "oos_end": run.get("oos_end"), "oos_sessions": run.get("oos_sessions"), "candidate_count": run["candidate_count"], "candidate_set_hash": run.get("candidate_set_hash"),
        "candidates_skipped": len(run.get("candidates_skipped") or []), "skips": (run.get("diagnostics") or {}).get("skips"), "arms_run": run.get("arms_run"),
        "integrity": run.get("integrity"), "friction_model": run.get("friction_model"), "execution_assumptions": run.get("execution_assumptions"),
        "causality": run.get("causality"), "audit": {"status": au.get("status"), "checked": au.get("checked"), "validated": au.get("validated"), "note": au.get("note"),
                                                     "violations": len(au.get("violations") or []), "first_violations": (au.get("violations") or [])[:5]},
        "diagnostics": {k: v for k, v in (run.get("diagnostics") or {}).items() if k != "skips"}, "verdict": run.get("verdict"), "arms": arms,
        "validation_version": run.get("validation_version"), "git_commit_sha": run.get("git_commit_sha"),
        "integrity_gate": ({"recorded": True, "status": el.get("integrity_status"), "eligibility": el.get("status"), "checks": el.get("checks") or [], "reasons": el.get("reasons") or [],
                       "gate_version": el.get("gate_version")} if el else {"recorded": False, "note": "not recorded: a Stage 6.2 run (validation_v1) predates the integrity gate"}),
        "boundary": run.get("boundary") or None, "accounting": run.get("accounting") or None, "provenance": run.get("provenance") or None,
        "replay_parity": run.get("replay_parity") or None}}


def registry_summaries(store, entries) -> Dict[str, dict]:
    out = {}
    for e in entries:
        try:
            run = store.latest(e.strategy_id)
        except Exception:  # noqa: BLE001 - a broken store reads as "not run", never as a result
            run = None
        out[e.strategy_id] = summary(run, e.strategy_id, e)
    return out
