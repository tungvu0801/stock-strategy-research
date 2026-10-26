"""momentum/diagnostics.py — Momentum RESEARCH diagnostics: never a gate, never a rule, never used to choose anything. The selection edge itself is
validation_v3's (strategy_validation/cross_sectional.py); these describe it.

  calendar            per year and quarter: strategy, selection control and excess (the v3 period table)
  market_months       periods in which the MARKET BENCHMARK (SPY) rose vs fell: mean strategy / control / excess - descriptive only
  friction            gross vs net mean period excess; the share of the gross excess consumed by friction; strategy friction per period
  construction        rebalances, eligible stocks, one-way turnover per month and annualized, selection stability
  concentration       sector (today's SEC SIC description - not point in time), single-name holding frequency, period-excess concentration
  sensitivity         the pre-declared neighbours (momentum.SENSITIVITY) and the base configuration, computed on the TRAIN VIEW of the dataset
                      (dataset.until(train_end): no bar after the TRAIN end exists for them) - one excess per period over the same eligible-universe
                      control; never used to choose a rule"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from momentum import RESEARCH_UNIVERSE, SENSITIVITY

MAJOR_GROUPS = {"28": "Chemicals & pharmaceuticals (SIC 28)", "35": "Industrial & computer equipment (SIC 35)", "36": "Electronic equipment & semiconductors (SIC 36)",
                "37": "Transportation equipment (SIC 37)", "38": "Instruments & medical devices (SIC 38)", "48": "Communications (SIC 48)", "49": "Utilities (SIC 49)",
                "60": "Banks (SIC 60)", "62": "Brokers & exchanges (SIC 62)", "73": "Software & business services (SIC 73)", "80": "Health services (SIC 80)"}


def _compound(xs: List[float]) -> float:
    w = 1.0
    for x in xs:
        w *= 1.0 + x
    return w - 1.0


def _t(ex: List[float]) -> Optional[float]:
    if len(ex) < 6:
        return None
    sd = statistics.stdev(ex)
    return statistics.mean(ex) / (sd / math.sqrt(len(ex))) if sd > 0 else None


def _r(x, n=6):
    return None if x is None else round(float(x), n)


def _group(periods: List[dict], key) -> Dict[str, dict]:
    g: Dict[str, List[dict]] = defaultdict(list)
    for p in periods:
        g[key(p)].append(p)
    return {k: {"periods": len(v), "strategy": _r(_compound([p["strategy"]["net"] for p in v])), "control": _r(_compound([p["control"]["net"] for p in v])),
                "mean_excess": _r(statistics.mean(p["excess"] for p in v))} for k, v in sorted(g.items())}


def _period_returns(core, dataset, slip: float) -> List[dict]:
    """TRAIN-view periods of one configuration: the strategy's top N vs every eligible stock, the legs' timing and friction convention."""
    from momentum.data import PriceData
    from strategy_validation.cross_sectional import _after, _window
    data = PriceData(dataset)
    universe = [s for s in RESEARCH_UNIVERSE if s in dataset.bars]
    decs = [core.evaluate(R, universe, data) for R in core.schedule(dataset.sessions)]
    out = []
    for k in range(len(decs) - 1):                                               # the last period would end at the TRAIN end: not a full period
        R, nxt = decs[k]["rebalance"], decs[k + 1]["rebalance"]
        sel, elig = decs[k]["selected"], [x["symbol"] for x in decs[k]["ranked"]]
        if not sel:
            continue
        prev_s = set(decs[k - 1]["selected"]) if k else set()
        prev_e = {x["symbol"] for x in decs[k - 1]["ranked"]} if k else set()
        next_s, next_e = set(decs[k + 1]["selected"]), {x["symbol"] for x in decs[k + 1]["ranked"]}

        def leg(sym, prev, nxt_set):
            w = _window(_after(dataset, sym, R), nxt)
            if w is None or w[3] != "TIME_EXIT":
                return None
            e = w[0].open * (1 + slip) if sym not in prev else w[0].open
            x = w[2] * (1 - slip) if sym not in nxt_set else w[2]
            return x / e - 1.0
        s = [leg(x, prev_s, next_s) for x in sel]
        c = [leg(x, prev_e, next_e) for x in elig]
        if any(v is None for v in s) or any(v is None for v in c):
            continue                                                             # an incomplete period is excluded on both sides
        out.append({"rebalance": R, "strategy": statistics.mean(s), "control": statistics.mean(c)})
    return out


def sensitivity(dataset, train_end: str, slip: float, base: dict) -> dict:
    from momentum.core import MomentumStrategyCore
    tv = dataset.until(train_end)
    runs = []
    for params in [{}] + list(SENSITIVITY):
        p = {**base, **params}
        runs.append((params or "base", _period_returns(MomentumStrategyCore(p["top_n"], p["lookback_sessions"], p["skip_sessions"]), tv, slip)))
    common = set.intersection(*[{x["rebalance"] for x in per} for _, per in runs]) if runs else set()
    rows = []
    for params, per in runs:
        on = [x for x in per if x["rebalance"] in common]                        # every row on the SAME rebalance periods
        ex = [x["strategy"] - x["control"] for x in on]
        rows.append({"params": params, "periods": len(on), "own_periods": len(per), "strategy_total_return": _r(_compound([x["strategy"] for x in on])),
                     "control_total_return": _r(_compound([x["control"] for x in on])), "mean_excess": _r(statistics.mean(ex)) if ex else None,
                     "excess_t_stat": _r(_t(ex), 4)})
    return {"scope": f"TRAIN view only (dataset.until({train_end}): no later bar exists); every row on the same {len(common)} periods (those all "
                     "configurations can form); diagnostic, never used to choose", "rows": rows}


def diagnostics(record: dict, dataset, log: List[dict], top_n: int) -> dict:
    cs = record.get("cross_sectional") or {}
    inc = [p for p in cs.get("periods") or [] if p.get("status") == "INCLUDED"]
    by_seg = {k: [p for p in inc if k == "ALL" or p["segment"] == k] for k in ("TRAIN", "OOS", "ALL")}
    ppy = float(cs.get("periods_per_year") or 12.0)
    friction = {}
    for k, ps in by_seg.items():
        if not ps:
            continue
        g, n = statistics.mean(p["excess_gross"] for p in ps), statistics.mean(p["excess"] for p in ps)
        sf = statistics.mean(p["strategy"]["gross"] - p["strategy"]["net"] for p in ps)
        friction[k] = {"mean_excess_gross": _r(g), "mean_excess_net": _r(n), "share_of_gross_excess_consumed": _r((g - n) / g, 4) if g > 0 else None,
                       "strategy_friction_per_period": _r(sf), "strategy_friction_per_year": _r(sf * ppy)}
    market = {}
    for k, ps in by_seg.items():
        up = [p for p in ps if (p.get("benchmark") or {}).get("return") is not None and p["benchmark"]["return"] > 0]
        dn = [p for p in ps if (p.get("benchmark") or {}).get("return") is not None and p["benchmark"]["return"] <= 0]
        market[k] = {lab: ({"periods": len(xs), "mean_strategy": _r(statistics.mean(p["strategy"]["net"] for p in xs)),
                            "mean_control": _r(statistics.mean(p["control"]["net"] for p in xs)), "mean_excess": _r(statistics.mean(p["excess"] for p in xs))}
                           if xs else {"periods": 0}) for lab, xs in (("benchmark_up", up), ("benchmark_down", dn))}
    invested = [r for r in log if r.get("selected")]
    turnover = [len(r["enter"]) / top_n for r in invested[1:]]
    stability = [len(r["hold"]) / top_n for r in invested[1:]]
    issuers = {s: info for info in ((dataset.aux or {}).get("static") or {}).get("issuers", {}).values() for s in info.get("symbols") or []}
    top_share, top_names, held, major_share, major_names = [], Counter(), Counter(), [], Counter()
    for r in invested:
        sec = Counter((issuers.get(x) or {}).get("sic_description") or "UNKNOWN" for x in r["selected"])
        name, n = sec.most_common(1)[0]
        top_share.append(n / len(r["selected"]))
        top_names[name] += 1
        held.update(r["selected"])
        maj = Counter(MAJOR_GROUPS.get(str((issuers.get(x) or {}).get("sic") or "")[:2], "SIC " + (str((issuers.get(x) or {}).get("sic") or "??")[:2]))
                      for x in r["selected"])
        mname, mn = maj.most_common(1)[0]
        major_share.append(mn / len(r["selected"]))
        major_names[mname] += 1
    control_of = {p["rebalance"]: p["control"]["net"] for p in inc}
    contrib: Counter = Counter()
    for t in ((record.get("arms") or {}).get("RULES") or {}).get("trades") or []:   # name i's part of a period's excess: (leg - control) / positions
        p = next((q for q in inc if q["rebalance"] == t["signal_date"]), None)
        if p is not None and t.get("trade_return") is not None:
            contrib[t["symbol"]] += (float(t["trade_return"]) - control_of[t["signal_date"]]) / p["strategy"]["positions"]
    elig = [r["eligible"] for r in log if r.get("eligible") is not None]
    reasons = Counter()
    for r in log:
        reasons.update(r.get("ineligible_by_reason") or {})
    fr = record.get("friction_model") or {}
    base = {"top_n": top_n, "lookback_sessions": int(((record.get("config_snapshot") or {}).get("adapter") or {}).get("params", {}).get("lookback_sessions", 252)),
            "skip_sessions": int(((record.get("config_snapshot") or {}).get("adapter") or {}).get("params", {}).get("skip_sessions", 21))}
    from momentum.audit import split_events
    artifacts = [{**{k: (_r(v) if isinstance(v, float) else v) for k, v in ev.items()},
                  "adjusted_vs_raw_step": _r((1 + ev["adjusted_return"]) / (1 + ev["raw_return"]) - 1) if ev["adjusted_return"] is not None else None}
                 for ev in split_events(dataset, [s for s in RESEARCH_UNIVERSE if s in dataset.bars]) if ev["kind"] == "RATIO_ARTIFACT"]
    return {
        "note": "research diagnostics: never a gate, never used to choose a rule; the selection edge is validation_v3's",
        "data_quality": {"ratio_artifacts": artifacts,
                         "note": "moves of the provider's raw / split-adjusted ratio with no raw price move (no real split): the split-adjusted series is "
                                 "wrong there and the strategy does not use it; adjusted_vs_raw_step is the unexplained step of the all-adjusted series "
                                 "it does use"},
        "flags": [],
        "calendar": {"by_year": _group(inc, lambda p: p["rebalance"][:4]),
                     "by_quarter": _group(inc, lambda p: f"{p['rebalance'][:4]}-Q{(int(p['rebalance'][5:7]) - 1) // 3 + 1}")},
        "market_months": market,
        "friction": friction,
        "construction": {"rebalances": len(log), "invested_rebalances": len(invested), "first_invested_rebalance": invested[0]["rebalance"] if invested else None,
                         "top_n": top_n, "average_one_way_turnover": _r(statistics.mean(turnover), 4) if turnover else None,
                         "annualized_one_way_turnover": _r(statistics.mean(turnover) * ppy, 4) if turnover else None,
                         "selection_stability": _r(statistics.mean(stability), 4) if stability else None, "max_single_name_weight": _r(1.0 / top_n, 4),
                         "eligible_per_rebalance": {"min": min(elig) if elig else None, "max": max(elig) if elig else None,
                                                    "mean": _r(statistics.mean(elig), 2) if elig else None},
                         "ineligible_by_reason": dict(sorted(reasons.items()))},
        "concentration": {"sector_classification": "SEC SIC description (today's, not point in time)",
                          "mean_top_sector_share": _r(statistics.mean(top_share), 4) if top_share else None,
                          "max_top_sector_share": _r(max(top_share), 4) if top_share else None, "most_frequent_top_sectors": top_names.most_common(5),
                          "most_held_names": [(s, _r(n / len(invested), 4)) for s, n in held.most_common(10)] if invested else [],
                          "sic_major_group": {"mean_top_share": _r(statistics.mean(major_share), 4) if major_share else None,
                                              "max_top_share": _r(max(major_share), 4) if major_share else None,
                                              "most_frequent_top_groups": major_names.most_common(5)},
                          "excess_contribution": {"note": "sum over a name's included legs of (leg return - control return) / positions: its share of "
                                                          "the summed period excess (ALL periods)",
                                                  "top": [(s, _r(v)) for s, v in contrib.most_common(5)],
                                                  "bottom": [(s, _r(v)) for s, v in sorted(contrib.items(), key=lambda kv: kv[1])[:5]],
                                                  "summed_period_excess": _r(sum(p["excess"] for p in inc))},
                          "period_excess": {k: (v or {}).get("concentration") for k, v in (cs.get("segments") or {}).items()}},
        "sensitivity": sensitivity(dataset, record.get("train_end") or "", float(fr.get("slippage_bps_per_side") or 0.0) / 10000.0, base),
    }
