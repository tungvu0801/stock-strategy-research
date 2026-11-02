"""swing_eval/analysis.py — pure functions over the four run records (plain JSON): the confirmed decision lock's R9 fold stability, R10 V1-versus-C1
comparison (with exposure, drawdown and a month-block bootstrap), R11 eligibility, and the blocking validation findings (R22 / M-5 non-positive
stops; harness integrity). Nothing here changes a run, a candidate, a stop or a rule.

  fold_rows()      the six folds of swing_pullback.diagnostics.fold_bounds (unchanged): closed RULES trades by signal date, mean r_net, net P/L
  stability()      R9: evaluable folds (>= 20 trades), >= 5 evaluable, net > 0 in >= ceil(2/3 E), median fold mean R > 0.05, no fold >= 80 % of net
                   (a total net P/L <= 0 is never STABLE: the lock leaves the 80 % rule undefined there, so it fails closed)
  comparison()     R10: OOS mean r_net difference in BOTH cost runs (>= 0.05 R), paired-fold consistency on the BASE run (whole signal period:
                   TRAIN included - a time-consistency check, not OOS confirmation), the trade-count requirement, and the reported measures
  stop_findings()  M-5: every candidate with a stop <= 0 (the four runs and the eight TRAIN-view neighbours): BLOCKING
  integrity_findings()  a run whose harness causality is CRITICAL, whose audit failed or whose arms got different candidates: BLOCKING
  eligibility()    R11: all six items, each with its evidence; REVIEW_REQUIRED findings count as accepted only with a recorded acceptance"""
from __future__ import annotations

import math
import random
import statistics
from typing import Dict, List, Optional, Tuple

from strategy_validation import FRICTION_HEAVY
from swing_eval import R9, R10
from swing_pullback.diagnostics import fold_bounds


def _ceil_share(n: int, share: List[int]) -> int:
    return math.ceil(n * share[0] / share[1])


def trades(rec: dict, segment: Optional[str] = None) -> List[dict]:
    return [t for t in ((rec.get("arms") or {}).get("RULES") or {}).get("trades") or [] if segment is None or t["segment"] == segment]


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def fold_rows(rec: dict, sessions: List[str]) -> List[dict]:
    bounds = fold_bounds(sessions) or []
    rows = []
    for b in bounds:
        inside = [t for t in trades(rec) if b["start"] <= t["signal_date"] <= b["end"]]
        rows.append({**b, "trades": len(inside), "mean_R": _mean([t["r_net"] for t in inside]), "net_pnl": sum(t["net_pnl"] for t in inside)})
    return rows


def stability(rec: dict, sessions: List[str], t: dict = R9) -> dict:
    rows = fold_rows(rec, sessions)
    ev = [r for r in rows if r["trades"] >= t["fold_min_trades"]]
    out = {"folds": rows, "evaluable": len(ev), "thresholds": t}
    if len(rows) < 6 or len(ev) < t["min_evaluable_folds"]:
        return {**out, "outcome": "INSUFFICIENT", "why": f"{len(ev)} evaluable folds (>= {t['fold_min_trades']} trades each); {t['min_evaluable_folds']} needed"}
    pos = sum(1 for r in ev if r["net_pnl"] > 0)
    need = _ceil_share(len(ev), t["positive_share"])
    med = statistics.median(r["mean_R"] for r in ev)
    total = sum(r["net_pnl"] for r in rows)
    dominant = [r["fold"] for r in rows if total > 0 and r["net_pnl"] >= t["max_fold_share_of_net"] * total]
    checks = {"positive_folds": pos, "positive_needed": need, "median_fold_mean_R": med, "total_net_pnl": total, "dominant_folds": dominant,
              "concentration": "CHECKED" if total > 0 else "NOT_SATISFIABLE: total net P/L <= 0"}
    ok = pos >= need and med > t["median_fold_mean_r_min"] and total > 0 and not dominant
    return {**out, **checks, "outcome": "STABLE" if ok else "UNSTABLE"}


def exposure(rec: dict, sessions: List[str], segment: str = "OOS") -> dict:
    tr = trades(rec, segment)
    if segment == "OOS":
        lo, hi, n_sess = rec.get("oos_start"), rec.get("oos_end"), rec.get("oos_sessions") or 0
    else:
        lo, hi = rec.get("train_start"), rec.get("train_end")
        n_sess = sum(1 for d in sessions if lo and hi and lo <= d <= hi)
    window = [d for d in sessions if lo and hi and lo <= d <= hi]
    bars = sum(t["holding_bars"] for t in tr)
    conc = _mean([sum(1 for t in tr if t["entry_date"] <= d <= t["exit_date"]) for d in window]) if window else None
    net = sum(t["net_pnl"] for t in tr)
    return {"trades": len(tr), "position_bars": bars, "trades_per_100_sessions": (100.0 * len(tr) / n_sess) if n_sess else None,
            "mean_concurrent_open_trades": conc, "net_pnl_per_1000_position_bars": (1000.0 * net / bars) if bars else None}


def measures(rec: dict, sessions: List[str]) -> dict:
    """R10.6: reported, never gating. The R10.1 exclusions (boundary-purged candidates, simulation skips) are counted here, never compared."""
    m = ((rec.get("arms") or {}).get("RULES") or {}).get("metrics") or {}
    out = {}
    for seg in ("TRAIN", "OOS", "ALL"):
        tr = trades(rec, None if seg == "ALL" else seg)
        R = [t["r_net"] for t in tr]
        mm = m.get(seg) or {}
        dd = mm.get("max_drawdown")
        net = sum(t["net_pnl"] for t in tr)
        gross, fr = mm.get("gross_pnl"), mm.get("friction_total")
        share = (fr / gross) if fr is not None and gross and gross > 0 else None          # the harness's rule: no share without a gross profit
        by_exit = _count(tr, "exit_reason")
        out[seg] = {"trades": len(tr), "by_exit": by_exit, "data_end_exits": by_exit.get("DATA_END", 0), "by_size": _count(tr, "size_status"),
                    "boundary_purged": mm.get("boundary_purged_count"), "simulation_skipped": mm.get("simulation_skipped_count"),
                    "win_rate": (sum(1 for x in R if x > 0) / len(R)) if R else None, "mean_R": _mean(R), "median_R": statistics.median(R) if R else None,
                    "mean_R_t_stat": mm.get("mean_R_t_stat"), "net_pnl": net, "net_pnl_per_trade": (net / len(tr)) if tr else None,
                    "max_drawdown": dd, "max_drawdown_pct": mm.get("max_drawdown_pct"), "net_over_max_drawdown": (net / dd) if dd else None,
                    "gross_pnl": gross, "friction_total": fr, "friction_share_of_gross": share,
                    "friction_heavy": share is not None and share > FRICTION_HEAVY,
                    "average_holding_bars": mm.get("average_holding_bars"), "exposure": exposure(rec, sessions, seg) if seg != "ALL" else None}
    tvo = ((rec.get("arms") or {}).get("RULES") or {}).get("diagnostics", {}).get("train_vs_oos")
    out["train_vs_oos"] = tvo
    return out


def _count(tr: List[dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for t in tr:
        out[t[key]] = out.get(t[key], 0) + 1
    return dict(sorted(out.items()))


def bootstrap(v1: List[dict], c1: List[dict], cfg: dict = R10["bootstrap"]) -> dict:
    """Δ_R over OOS trades, resampling calendar months of the signal date jointly for both versions. Descriptive only."""
    by = lambda tr: {m: [t["r_net"] for t in tr if t["signal_date"][:7] == m] for m in {t["signal_date"][:7] for t in tr}}  # noqa: E731
    a, b = by(v1), by(c1)
    months = sorted(set(a) | set(b))
    if not months:
        return {"status": "NO_TRADES"}
    rng = random.Random(cfg["seed"])
    deltas, skipped = [], 0
    for _ in range(cfg["resamples"]):
        draw = [rng.choice(months) for _ in months]
        ra = [x for mo in draw for x in a.get(mo, [])]
        rb = [x for mo in draw for x in b.get(mo, [])]
        if not ra or not rb:
            skipped += 1
            continue
        deltas.append(statistics.fmean(ra) - statistics.fmean(rb))
    if not deltas:
        return {"status": "NO_RESAMPLE", "skipped": skipped}
    deltas.sort()
    pct = lambda p: deltas[min(len(deltas) - 1, max(0, math.ceil(p / 100.0 * len(deltas)) - 1))]  # noqa: E731  nearest rank
    return {"status": "REPORTED", "role": cfg["role"], "months": len(months), "resamples_used": len(deltas), "skipped": skipped,
            "percentiles": {str(p): pct(p) for p in cfg["percentiles"]}, "share_delta_le_0": sum(1 for d in deltas if d <= 0) / len(deltas)}


def comparison(recs: Dict[Tuple[str, str], dict], sessions: List[str], t: dict = R10) -> dict:
    """recs: {(version, cost): record}. R10.1-R10.7."""
    counts, short = {}, []
    for (v, c), rec in sorted(recs.items()):
        n_oos, n_all = len(trades(rec, "OOS")), len(trades(rec))
        counts[f"{v} {c}"] = {"oos": n_oos, "all": n_all}
        if n_oos < t["min_oos_trades"] or n_all < t["min_trades"]:
            short.append(f"{v} {c}")
    delta = {}
    for c in ("BASE", "STRESS_2X"):
        a, b = _mean([x["r_net"] for x in trades(recs[("V1", c)], "OOS")]), _mean([x["r_net"] for x in trades(recs[("C1", c)], "OOS")])
        delta[c] = None if a is None or b is None else a - b
    fv, fc = fold_rows(recs[("V1", t["fold_run"])], sessions), fold_rows(recs[("C1", t["fold_run"])], sessions)
    paired = [{"fold": x["fold"], "v1_trades": x["trades"], "c1_trades": y["trades"], "v1_mean_R": x["mean_R"], "c1_mean_R": y["mean_R"],
               "v1_ahead": x["mean_R"] > y["mean_R"]}
              for x, y in zip(fv, fc) if x["trades"] >= t["paired_fold_min_trades"] and y["trades"] >= t["paired_fold_min_trades"]]
    ahead = sum(1 for p in paired if p["v1_ahead"])
    c_a = all(d is not None and d >= t["delta_r_min"] for d in delta.values())
    c_b = len(paired) >= t["min_paired_folds"] and ahead >= _ceil_share(len(paired), t["ahead_share"])
    if short:
        outcome, why = "INCONCLUSIVE", f"insufficient trades: {short}"
    elif any(d is not None and d <= 0 for d in delta.values()):
        outcome, why = "NO_ADDED_VALUE", "Δ_R <= 0 in a cost run (only: V1 did not show an improvement under the pre-registered mean-R comparison)"
    elif c_a and c_b:
        outcome, why = "BEATS_CONTROL", "Δ_R >= 0.05 R in both cost runs and V1 ahead in >= 2/3 of >= 4 paired folds (materiality, not significance)"
    elif len(paired) < t["min_paired_folds"]:
        outcome, why = "INCONCLUSIVE", f"{len(paired)} paired folds (>= {t['min_paired_folds']} needed)"
    else:
        outcome, why = "INCONCLUSIVE", "C-a or C-b not met"
    reported = {f"{v} {c}": measures(rec, sessions) for (v, c), rec in sorted(recs.items())}
    boots = {c: bootstrap(trades(recs[("V1", c)], "OOS"), trades(recs[("C1", c)], "OOS")) for c in ("BASE", "STRESS_2X")}
    folds_stress = {"V1": fold_rows(recs[("V1", "STRESS_2X")], sessions), "C1": fold_rows(recs[("C1", "STRESS_2X")], sessions)}
    return {"outcome": outcome, "why": why, "trade_counts": counts, "delta_R_oos": delta, "C_a": c_a, "paired_folds": paired, "v1_ahead": ahead,
            "C_b": c_b, "reported": reported, "bootstrap": boots, "folds_stress_reported": folds_stress, "interpretation": t["interpretation"],
            "thresholds": t}


def stop_findings(recs: Dict[Tuple[str, str], dict], neighbours: List[Tuple[dict, object]]) -> List[dict]:
    """M-5: a stop (invalidation_reference) <= 0 anywhere is a BLOCKING validation finding. neighbours: [(params, RawCandidate)] (TRAIN view)."""
    out = []
    for (v, c), rec in sorted(recs.items()):
        for cand in rec.get("candidates") or []:
            if cand.get("invalidation_reference") is not None and cand["invalidation_reference"] <= 0:
                f = cand.get("features") or {}
                out.append({"code": "NON_POSITIVE_STOP", "version": v, "configuration": "base", "cost": c, "symbol": cand["symbol"],
                            "signal_date": cand["signal_date"], "signal_close": f.get("close"), "atr14": f.get("atr"), "stop": cand["invalidation_reference"]})
    for params, raw in neighbours:
        if raw.invalidation_reference is not None and raw.invalidation_reference <= 0:
            out.append({"code": "NON_POSITIVE_STOP", "version": "V1", "configuration": params, "cost": "TRAIN view", "symbol": raw.symbol,
                        "signal_date": raw.signal_date, "signal_close": raw.features.get("close"), "atr14": raw.features.get("atr"),
                        "stop": raw.invalidation_reference})
    return out


def integrity_findings(recs: Dict[Tuple[str, str], dict]) -> List[dict]:
    out = []
    for (v, c), rec in sorted(recs.items()):
        if (rec.get("causality") or {}).get("critical"):
            out.append({"code": "CAUSALITY_CRITICAL", "version": v, "cost": c})
        if (rec.get("audit") or {}).get("status") != "PASS":
            out.append({"code": "AUDIT_FAILED", "version": v, "cost": c})
        if not (rec.get("integrity") or {}).get("same_candidate_set"):
            out.append({"code": "CANDIDATE_SET_MISMATCH", "version": v, "cost": c})
    return out


def eligibility(recs: Dict[Tuple[str, str], dict], stab: dict, comp: dict, report: dict, blocking: List[dict], invalid: List[str],
                findings_accepted_by: Optional[str] = None) -> dict:
    v1b, v1s = recs[("V1", "BASE")], recs[("V1", "STRESS_2X")]
    stress_oos = ((((v1s.get("arms") or {}).get("RULES") or {}).get("metrics") or {}).get("OOS") or {}).get("expectancy_R")
    sens = (((v1b.get("diagnostics") or {}).get("strategy") or {}).get("sensitivity") or {}).get("label")
    rs = report.get("status")
    items = [{"item": "harness PASS on V1 BASE", "ok": (v1b.get("verdict") or {}).get("verdict") == "PASS", "evidence": (v1b.get("verdict") or {}).get("verdict")},
             {"item": "V1 OOS expectancy > 0 at STRESS_2X", "ok": stress_oos is not None and stress_oos > 0, "evidence": stress_oos},
             {"item": "R9 STABLE", "ok": stab.get("outcome") == "STABLE", "evidence": stab.get("outcome")},
             {"item": "R10 BEATS_CONTROL", "ok": comp.get("outcome") == "BEATS_CONTROL", "evidence": comp.get("outcome")},
             {"item": "V1 TRAIN-view sensitivity ROBUST", "ok": sens == "ROBUST", "evidence": sens},
             {"item": "pre-run report accepted and no blocking validation finding",
              "ok": (rs == "CLEAN" or (rs == "REVIEW_REQUIRED" and bool(findings_accepted_by))) and not blocking and not invalid,
              "evidence": {"report": rs, "findings_accepted_by": findings_accepted_by if rs == "REVIEW_REQUIRED" else None, "blocking": len(blocking),
                           "invalid_runs": invalid}}]
    return {"eligible_for_review": all(i["ok"] for i in items), "items": items,
            "note": "eligible for review is never a promotion; the edge stays NOT_ESTABLISHED (survivorship); the next step would be the 7.9 shadow"}
