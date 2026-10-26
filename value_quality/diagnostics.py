"""value_quality/diagnostics.py — Value + Quality RESEARCH diagnostics (never a gate, never a selection rule).

Portfolio view (the harness metrics are per leg): the daily net P/L of the simulated RULES legs (all-adjusted bars: total return; entry and exit at
their friction-inclusive fills) over the fixed hypothetical equity, compounded; SPY over the same sessions (all-adjusted). Purged boundary legs are not
in the portfolio (the TRAIN / OOS split is per segment of sessions). UNIVERSE CONTROL: an equal-weight, monthly re-formed portfolio of every ELIGIBLE
stock at each rebalance (close-to-close, no friction) - it shares the universe's survivorship bias, so strategy minus control isolates what the
Value + Quality SELECTION added; strategy minus SPY does not. The excess over the control is also tested per REBALANCE PERIOD (about one month:
the independent unit - the ~20 legs of one month share the market's move, so the per-leg t-statistic of the harness overstates the evidence). Plus turnover, sector (SEC SIC, today's classification) and single-name
concentration, factor coverage, value vs quality contribution, selection stability and calendar-year returns."""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from value_quality import BENCHMARK, QUALITY_FACTORS, VALUE_FACTORS


def _perf(rets: List[float]) -> Optional[dict]:
    if not rets:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1.0 + r
        peak = max(peak, eq)
        mdd = max(mdd, 1.0 - eq / peak)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    mean = statistics.mean(rets)
    return {"sessions": len(rets), "total_return": round(eq - 1.0, 6), "annualized_volatility": round(sd * math.sqrt(252), 6),
            "sharpe": round(mean / sd * math.sqrt(252), 4) if sd > 0 and len(rets) >= 60 else None, "max_drawdown": round(mdd, 6)}


def diagnostics(record: dict, dataset, log: List[dict], top_n: int) -> dict:
    equity = float(((record.get("config_snapshot") or {}).get("sizing") or {}).get("equity") or 100000.0)
    train_end = record.get("train_end") or ""
    trades = ((record.get("arms") or {}).get("RULES") or {}).get("trades") or []
    close = {s: {b.date: b.close for b in rows} for s, rows in dataset.bars.items()}
    sessions = dataset.sessions
    first = min((r["rebalance"] for r in log), default=None)
    days = [d for d in sessions if first and d > first]
    pnl = defaultdict(float)
    expo = defaultdict(int)
    for t in trades:
        c, q = close.get(t["symbol"]) or {}, float(t["qty"])
        held = [d for d in days if t["entry_date"] <= d <= t["exit_date"] and d in c]
        prev = None
        for d in held:
            start = float(t["entry_fill"]) if d == t["entry_date"] else c[prev]
            end = float(t["exit_fill"]) if d == t["exit_date"] else c[d]
            pnl[d] += q * (end - start)
            if not (d == t["exit_date"] and t["exit_reason"] == "TIME_EXIT"):   # the exit open is a hand-over, not a held session
                expo[d] += 1
            prev = d
    bench = close.get(BENCHMARK) or {}
    rows = []
    for i, d in enumerate(days):
        p = sessions[sessions.index(d) - 1] if d in sessions else None
        b = (bench[d] / bench[p] - 1.0) if (p in bench and d in bench) else None
        rows.append((d, pnl[d] / equity, b, expo[d]))
    ctrl = {}                                                                    # the equal-weight eligible-universe control
    for k, r in enumerate(log):
        start, stop = r["rebalance"], (log[k + 1]["rebalance"] if k + 1 < len(log) else "9999")
        for d in [x for x in days if start < x <= stop]:
            p = sessions[sessions.index(d) - 1]
            rets = [close[s][d] / close[s][p] - 1.0 for s in r.get("eligible_symbols") or [] if d in (close.get(s) or {}) and p in (close.get(s) or {})]
            if rets:
                ctrl[d] = sum(rets) / len(rets)
    seg = {"TRAIN": [r for r in rows if r[0] <= train_end], "OOS": [r for r in rows if r[0] > train_end], "ALL": rows}
    starts = [r["rebalance"] for r in log]

    def monthly_excess(rs):
        per = defaultdict(lambda: [1.0, 1.0])
        for d, r, _, _ in rs:
            if d in ctrl:
                k = max(s for s in starts if s < d)
                per[k][0] *= 1.0 + r
                per[k][1] *= 1.0 + ctrl[d]
        ex = [a - b for a, b in per.values()]
        if len(ex) < 2:
            return {"periods": len(ex), "mean": None, "t_stat": None}
        sd = statistics.stdev(ex)
        return {"periods": len(ex), "mean": round(statistics.mean(ex), 6), "t_stat": round(statistics.mean(ex) / (sd / math.sqrt(len(ex))), 4) if sd > 0 else None,
                "positive_share": round(sum(1 for x in ex if x > 0) / len(ex), 4)}
    portfolio = {}
    for k, rs in seg.items():
        s, b = _perf([r[1] for r in rs]), _perf([r[2] for r in rs if r[2] is not None])
        u = _perf([ctrl[r[0]] for r in rs if r[0] in ctrl])
        portfolio[k] = {"strategy": s, "benchmark": b, "excess_total_return": round(s["total_return"] - b["total_return"], 6) if s and b else None,
                        "universe_equal_weight": u, "excess_vs_universe": round(s["total_return"] - u["total_return"], 6) if s and u else None,
                        "excess_vs_universe_per_period": monthly_excess(rs),
                        "average_positions": round(statistics.mean([r[3] for r in rs]), 2) if rs else None, "from": rs[0][0] if rs else None,
                        "to": rs[-1][0] if rs else None}
    years = defaultdict(lambda: [1.0, 1.0])
    for d, r, b, _ in rows:
        years[d[:4]][0] *= 1.0 + r
        if b is not None:
            years[d[:4]][1] *= 1.0 + b
    issuers = {s: info for info in ((dataset.aux or {}).get("static") or {}).get("issuers", {}).values() for s in info.get("symbols") or []}
    top_share, top_names = [], Counter()
    for r in log:
        sec = Counter((issuers.get(x["symbol"]) or {}).get("sic_description") or "UNKNOWN" for x in r["selected"])
        if sec:
            name, n = sec.most_common(1)[0]
            top_share.append(n / len(r["selected"]))
            top_names[name] += 1
    turnover = [len(r["enter"]) / top_n for r in log[1:]]
    overlap = [len(r["hold"]) / top_n for r in log[1:]]
    elig = [r["eligible"] for r in log]
    cov = {f: round(statistics.mean([r["factor_coverage"][f] / r["eligible"] for r in log if r["eligible"]]), 4) if log else None
           for f in VALUE_FACTORS + QUALITY_FACTORS}
    reasons = Counter()
    for r in log:
        reasons.update(r["ineligible_by_reason"])
    sel_v = [x["value_score"] for r in log for x in r["selected"]]
    sel_q = [x["quality_score"] for r in log for x in r["selected"]]
    from strategy_validation import T_STAT_WEAK
    flags = []                                                                   # disclosures shown with the verdict (never a gate)
    for k in ("TRAIN", "OOS"):
        t = ((portfolio.get(k) or {}).get("excess_vs_universe_per_period") or {}).get("t_stat")
        if t is None or t < T_STAT_WEAK:
            flags.append("SELECTION_EDGE_NOT_ESTABLISHED")
            break
    return {
        "note": "research diagnostics: never a gate, never used to choose a rule",
        "flags": flags,
        "portfolio": portfolio,
        "calendar_years": {y: {"strategy": round(v[0] - 1, 6), "benchmark": round(v[1] - 1, 6)} for y, v in sorted(years.items())},
        "construction": {"rebalances": len(log), "first_rebalance": first, "top_n": top_n,
                         "average_one_way_turnover": round(statistics.mean(turnover), 4) if turnover else None,
                         "selection_stability": round(statistics.mean(overlap), 4) if overlap else None,
                         "max_single_name_weight": round(1.0 / top_n, 4), "eligible_per_rebalance": {"min": min(elig) if elig else None, "max": max(elig) if elig else None,
                                                                                                       "mean": round(statistics.mean(elig), 2) if elig else None}},
        "sector_concentration": {"classification": "SEC SIC description (today's, not point in time)",
                                 "mean_top_sector_share": round(statistics.mean(top_share), 4) if top_share else None,
                                 "max_top_sector_share": round(max(top_share), 4) if top_share else None, "most_frequent_top_sectors": top_names.most_common(5)},
        "factor_coverage": cov, "ineligible_by_reason": dict(sorted(reasons.items())),
        "contribution": {"mean_value_score_selected": round(statistics.mean(sel_v), 4) if sel_v else None,
                         "mean_quality_score_selected": round(statistics.mean(sel_q), 4) if sel_q else None,
                         "mean_value_score_all": round(statistics.mean([r["mean_value_score_all"] for r in log if r["mean_value_score_all"] is not None]), 4) if log else None,
                         "mean_quality_score_all": round(statistics.mean([r["mean_quality_score_all"] for r in log if r["mean_quality_score_all"] is not None]), 4) if log else None},
    }
