"""swing_pullback/diagnostics.py — the RESEARCH diagnostics of V1 / C1: never a gate, never a rule, never used to choose anything. The harness
verdict is computed before them and they cannot change it. Every definition was fixed before any data was seen (DESIGN_78_SWING_PULLBACK.md §6).

  preregistration  the hash of every rule value (swing_pullback.preregistration_hash()) and the version
  deviations       D1-D5 from the master design's D.3, and what is deferred (V2, the EMA / support design)
  funnel           per symbol-session, the first failing rule (swing_pullback/core.py REASONS) on the run's dataset
  folds            6 consecutive folds of equal session counts from the dataset's 200th session to its last (the remainder adds one session to
                   each of the earliest folds); the RULES trades by signal date: trades, mean R, net P/L, t of R, positive (net P/L > 0, the
                   rotation_walkforward 'return > 0' convention); positive folds over folds with a trade. The SAME boundaries for V1 and C1. Descriptive:
                   the stability bar and the per-fold minimum are decided before real data
  sensitivity      V1 only: the base and the 8 pre-registered neighbours on the TRAIN VIEW (dataset.until(train_end): no later bar exists), at the
                   run's friction and sizing; a TRAIN candidate whose holding window would reach past the TRAIN end is purged by its dates; labelled by
                   the harness's own rule (strategy_validation/diagnostics.py sensitivity) - never used to choose
  costs            the run's friction, which of the two pre-registered cost runs it is, and the regulatory-fee disclosure"""
from __future__ import annotations

from typing import List, Optional

from swing_pullback import (COSTS, DEFERRED, FOLD_START_INDEX, FOLDS, SENSITIVITY, STRATEGY_ID, preregistration_hash)
from strategy_validation import candidates as CD
from strategy_validation import diagnostics as DG
from strategy_validation import metrics as MX
from strategy_validation import simulator as SIM


def _r(x, n=6):
    return None if x is None else round(float(x), n)


def fold_bounds(sessions: List[str]) -> Optional[List[dict]]:
    span = list(sessions)[FOLD_START_INDEX:]
    if len(span) < FOLDS:
        return None
    q, rem = divmod(len(span), FOLDS)
    out, a = [], 0
    for f in range(FOLDS):
        n = q + (1 if f < rem else 0)
        out.append({"fold": f + 1, "start": span[a], "end": span[a + n - 1], "sessions": n})
        a += n
    return out


def folds(record: dict, dataset) -> dict:
    bounds = fold_bounds(dataset.sessions)
    if bounds is None:
        return {"status": "INSUFFICIENT_SESSIONS", "text": f"fewer than {FOLDS} sessions from the dataset's {FOLD_START_INDEX + 1}th session", "folds": []}
    trades = ((record.get("arms") or {}).get("RULES") or {}).get("trades") or []
    cands = record.get("candidates") or []
    rows = []
    for b in bounds:
        inside = [t for t in trades if b["start"] <= t["signal_date"] <= b["end"]]
        R = [float(t["r_net"]) for t in inside]
        net = sum(float(t["net_pnl"]) for t in inside)
        rows.append({**b, "candidates": sum(1 for c in cands if b["start"] <= c["signal_date"] <= b["end"]), "trades": len(inside),
                     "mean_R": _r(sum(R) / len(R)) if R else None, "net_pnl": _r(net, 2) if inside else None, "mean_R_t_stat": _r(MX.t_stat(R), 4),
                     "positive": (net > 0) if inside else None})
    traded = [r for r in rows if r["trades"]]
    pos = sum(1 for r in traded if r["positive"])
    return {"status": "REPORTED", "boundaries": "6 equal session counts from the dataset's 200th session (the same for V1 and C1)",
            "assignment": "by signal date", "positive_definition": "net P/L > 0", "folds": rows, "folds_with_trades": len(traded), "positive_folds": pos,
            "positive_fold_pct": _r(pos / len(traded), 4) if traded else None, "trades_in_folds": sum(r["trades"] for r in rows),
            "trades_total": len(trades), "note": "descriptive only: no parameter is chosen from it; the stability bar is decided before real data"}


def sensitivity(record: dict, dataset, adapter) -> dict:
    train_end = record["train_end"]
    tv = dataset.until(train_end)
    last = len(tv.sessions) - 1
    pos = {d: i for i, d in enumerate(tv.sessions)}
    friction = dict(record["friction_model"])
    sizing = dict((record.get("config_snapshot") or {}).get("sizing") or {})
    rows = []
    for params in [{}] + [dict(p) for p in SENSITIVITY]:
        a = adapter.with_params(params)
        cands, _ = CD.normalize(a, a.candidates(tv), tv, None)
        trades, purged = [], 0
        for c in cands:
            if pos[c.signal_date] + c.max_hold_bars > last:                           # the holding window reaches past the TRAIN end: dates only
                purged += 1
                continue
            t, _skip = SIM.simulate(c, tv, arm="_", segment="TRAIN", entry_policy=a.entry_policy, friction=friction, sizing=sizing,
                                    time_exit_fill=a.time_exit_fill)
            if t is not None:
                trades.append(t)
        m = MX.compute(trades, candidate_count=len(cands), accepted_count=len(cands), stand_aside_count=0, equity=float(sizing["equity"]),
                       boundary_purged_count=purged)
        rows.append({"params": params or "base", "max_hold_bars": a.max_hold_bars, "candidates": len(cands), "trades": m["trade_count"],
                     "boundary_purged": purged, "expectancy_R": m["expectancy_R"], "net_pnl": m["net_pnl"], "mean_R_t_stat": m["mean_R_t_stat"]})
    base, nb = rows[0], rows[1:]
    label = DG.sensitivity(base["expectancy_R"], [{"label": str(r["params"]), "params": r["params"], "expectancy_R": r["expectancy_R"],
                                                   "trade_count": r["trades"]} for r in nb])
    return {"status": "REPORTED", "scope": f"TRAIN view only (dataset.until({train_end}): no later bar exists); diagnostic, never used to choose",
            "label": label["label"], "label_text": label["text"], "rows": rows,
            "note": "a SENSITIVE or UNSTABLE label makes the result not promotable (design §5); the harness's own neighbour hook is not used: it would "
                    "evaluate the neighbours on every session, OOS included"}


def diagnostics(record: dict, dataset, adapter) -> dict:
    slip = float((record.get("friction_model") or {}).get("slippage_bps_per_side", 0.0))
    cost = next((k for k, v in COSTS.items() if v == slip), None)
    is_v1 = adapter.strategy_id == STRATEGY_ID
    log = adapter.funnel.get(dataset.dataset_id) or {}
    return {"preregistration_hash": preregistration_hash(), "strategy": [adapter.strategy_id, adapter.strategy_version],
            "baseline": "a modified D.3 research baseline, not a faithful implementation of the complete D.3 strategy",
            "deviations": adapter.deviations(), "deferred": dict(DEFERRED), "funnel": log,
            "folds": folds(record, dataset),
            "sensitivity": sensitivity(record, dataset, adapter) if is_v1 else
            {"status": "NOT_RUN", "text": "C1 is the control: the eight pre-registered sensitivity checks are V1's"},
            "costs": {"slippage_bps_per_side": slip, "preregistered_run": cost or "NOT_PREREGISTERED", "commission_per_order":
                      (record.get("friction_model") or {}).get("commission_per_order"), "regulatory_fees": "NOT_MODELLED (rates to verify before real data)"},
            "portfolio_limits": "NOT_SIMULATED (D5): <= 4 open, <= 2 new a day, <= 2 a sector bind in the Stage 7.9 shadow book",
            "flags": adapter.research_flags(dataset)}
