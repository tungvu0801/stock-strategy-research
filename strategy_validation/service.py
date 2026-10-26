"""strategy_validation/service.py — read views of the stored validation runs for the Strategy Registry and the Strategy Lab (GET only).

summary()  the registry fields: validation_status (NOT_RUN or the latest verdict), latest_validation_run_id, validation_verdict, train / OOS trade counts,
           mechanical_audit_status, last_validated_at - plus the NOT_RUN reason. Never a lifecycle change.
detail()   the Strategy Lab Validation section: the run's period and split, TRAIN / OOS / ALL metrics of the RULES arm, the arm comparison (candidate
           counts side by side; MODEL "Not evaluated" when no decider ran), friction, execution assumptions, causality, the audit, diagnostics, warnings
           and the decision line. With no run: "Not yet evaluated" and why - nothing is estimated or filled in.
Stage 6.3.1: integrity_status, persistence_eligibility, boundary_purged_count and accounting_status come from the stored record (validation_v2).
A Stage 6.2 record (validation_v1) has none of them: they read None with integrity_recorded False - never back-filled, never assumed.
Stage 6.4.1 (validation_v3): the OVERALL verdict, the absolute PERFORMANCE verdict and the SELECTION EDGE are separate fields. For a
cross-sectional strategy they come from the run itself (a v3 run) or from the latest methodology evaluation that references an older run
(`methodology_evaluation`, attached by the store's read view) - the older run keeps its own verdict, shown as its absolute performance verdict."""
from __future__ import annotations

from typing import Dict, Optional

from strategy_validation import DISCLAIMER, EDGE_LABELS, MIN_OOS_SESSIONS, MIN_OOS_TRADES, MIN_TRADES, T_STAT_WEAK, VERDICT_LABELS, VERSION
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
    flags = list((run or {}).get("research_flags") or [])                       # Stage 6.4: research limitations shown with the verdict
    if not el:
        return {"integrity_recorded": False, "integrity_status": None, "persistence_eligibility": None, "boundary_purged_count": None, "accounting_status": None,
                "research_flags": flags}
    return {"integrity_recorded": True, "integrity_status": el.get("integrity_status"), "persistence_eligibility": el.get("status"),
            "boundary_purged_count": ((run.get("boundary") or {}).get("train_boundary_purged_count")), "accounting_status": (run.get("accounting") or {}).get("status"),
            "research_flags": flags}


def universe_view(run: dict) -> dict:
    """Stage 6.5.1: the universe integrity, the raw / final selection edge and the overall verdict a stored record reads with: the newest universe
    assessment (only when it references the newest methodology evaluation) > the run's own (a 6.5.1 run) > NOT ASSESSED: the universe is treated
    as UNKNOWN (UNTRUSTED), so the final edge is capped - a cross-sectional record is never read uncapped. Nothing is stored."""
    from strategy_validation import cross_sectional as CS
    from strategy_validation import universe as UNI
    ev, ua = run.get("methodology_evaluation"), run.get("universe_assessment")
    src = ev or run
    if src.get("evaluation_mode") != CS.CROSS_SECTIONAL:
        return {"ui": run.get("universe_integrity") or None, "final": src.get("selection_edge"), "raw": (src.get("selection_edge") or {}).get("status"),
                "verdict": src.get("verdict") or run.get("verdict"), "source": "run", "assessment": None}
    if ua and ua.get("evaluation_id") == (ev or {}).get("evaluation_id"):
        return {"ui": ua.get("universe_integrity"), "final": ua.get("selection_edge"), "raw": (ua.get("raw_selection_edge") or {}).get("status"),
                "verdict": ua.get("verdict"), "source": "assessment", "assessment": ua}
    if run.get("universe_integrity") and ev is None:
        return {"ui": run["universe_integrity"], "final": run.get("selection_edge"), "verdict": run.get("verdict"), "source": "run", "assessment": None,
                "raw": ((run.get("cross_sectional") or {}).get("selection_edge") or run.get("selection_edge") or {}).get("status")}
    raw, ui = dict(src.get("selection_edge") or {}), UNI.not_assessed()
    if not raw.get("status"):
        return {"ui": ui, "final": src.get("selection_edge"), "raw": None, "verdict": src.get("verdict") or run.get("verdict"), "source": "not_assessed", "assessment": None}
    final = UNI.cap_edge(raw, ui)
    return {"ui": ui, "final": final, "raw": raw["status"], "verdict": CS.overall_verdict(src.get("performance_verdict") or run.get("verdict") or {}, final["status"]),
            "source": "not_assessed", "assessment": None}


def methodology_fields(run: Optional[dict]) -> dict:
    """Stage 6.4.1: the overall / performance verdicts and the selection edge of a stored run, from the run (validation_v3) or its latest
    methodology evaluation; an older run without an evaluation reads its own verdict and "not assessed" - never back-filled."""
    if not run:
        return {"evaluation_mode": None, "performance_verdict": None, "selection_edge_status": None, "selection_edge_label": None, "methodology_version": None,
                "methodology_evaluation_id": None, "raw_selection_edge_status": None, "universe_integrity_status": None, "universe_method": None,
                "universe_assessment_id": None}
    ev = run.get("methodology_evaluation")
    src = ev or run
    perf = (src.get("performance_verdict") or run.get("verdict") or {}).get("verdict")
    uv = universe_view(run)                                                      # Stage 6.5.1: the FINAL edge (capped by the universe integrity)
    edge, ui, raw = (uv["final"] or {}).get("status"), uv["ui"] or {}, uv["raw"]
    return {"evaluation_mode": src.get("evaluation_mode"), "performance_verdict": perf, "selection_edge_status": edge,
            "selection_edge_label": EDGE_LABELS.get(edge) if edge else "Not assessed", "methodology_version": (ev or {}).get("methodology_version") or run.get("validation_version"),
            "methodology_evaluation_id": (ev or {}).get("evaluation_id"), "raw_selection_edge_status": raw if edge else None,
            "universe_integrity_status": ui.get("status") if ui else None, "universe_method": ui.get("method") if ui else None,
            "universe_assessment_id": (uv["assessment"] or {}).get("assessment_id")}


def summary(run: Optional[dict], strategy_id: str, entry=None) -> dict:
    if not run:
        return {"validation_status": "NOT_RUN", "validation_label": NOT_EVALUATED,
                "not_run_reason": not_run_reason(strategy_id, entry) or "No validation run is stored yet (python -m strategy_validation validate ... --persist).",
                "latest_validation_run_id": None,
                "validation_verdict": None, "train_trade_count": None, "oos_trade_count": None, "mechanical_audit_status": None, "last_validated_at": None, "validation_synthetic": False,
                **integrity_fields(None), **methodology_fields(None)}
    rules = (run.get("arms") or {}).get("RULES") or {}
    m = rules.get("metrics") or {}
    v = (universe_view(run)["verdict"] or {}).get("verdict")                    # the overall verdict (universe assessment > v3 evaluation > run; capped)
    return {"validation_status": v, "validation_label": VERDICT_LABELS.get(v, v), "not_run_reason": "", "latest_validation_run_id": run["run_id"], "validation_verdict": v,
            "train_trade_count": (m.get("TRAIN") or {}).get("trade_count"), "oos_trade_count": (m.get("OOS") or {}).get("trade_count"),
            "mechanical_audit_status": (run.get("audit") or {}).get("status"), "last_validated_at": run.get("created_at"), "validation_synthetic": run.get("dataset_kind") == "SYNTHETIC",
            **integrity_fields(run), **methodology_fields(run)}


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
        "replay_parity": run.get("replay_parity") or None},
        **_methodology_detail(run)}


def _methodology_detail(run: dict) -> dict:
    """Stage 6.4.1: the overall verdict, the absolute performance verdict, the selection edge and the cross-sectional control comparison (without the
    per-position detail), labelled by where they come from."""
    ev = run.get("methodology_evaluation")
    src = ev or run
    cs = src.get("cross_sectional") or {}
    uv = universe_view(run)
    ui, ua = uv["ui"] or {}, uv["assessment"]
    return {"overall_verdict": uv["verdict"], "absolute_verdict": src.get("performance_verdict") or run.get("verdict"),
            "selection_edge": uv["final"] or None, "cross_sectional": cs or None,
            "universe_integrity": ({**{k: ui.get(k) for k in ("policy_version", "method", "status", "cap", "point_in_time", "survivorship_bias", "includes_delisted",
                                                               "reasons", "consequence", "declaration")},
                                    "raw_edge_status": uv["raw"], "final_edge_status": (uv["final"] or {}).get("status"), "source": uv["source"],
                                    "assessment": ({"assessment_id": ua["assessment_id"], "created_at": ua["created_at"], "policy_version": ua["policy_version"],
                                                    "note": ua.get("note")} if ua else None)} if ui else None),
            "methodology_evaluation": ({"evaluation_id": ev["evaluation_id"], "created_at": ev["created_at"], "methodology_version": ev["methodology_version"],
                                        "run_id": ev["run_id"], "evaluated_run_validation_version": ev.get("evaluated_run_validation_version"),
                                        "candidate_set_reproduced": ev.get("candidate_set_reproduced"), "git_commit_sha": ev.get("git_commit_sha"), "note": ev.get("note")}
                                       if ev else None)}


def registry_summaries(store, entries) -> Dict[str, dict]:
    out = {}
    for e in entries:
        try:
            run = store.latest(e.strategy_id)
        except Exception:  # noqa: BLE001 - a broken store reads as "not run", never as a result
            run = None
        out[e.strategy_id] = summary(run, e.strategy_id, e)
    return out
