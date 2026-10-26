"""mean_reversion/diagnostics.py — Mean Reversion RESEARCH diagnostics: never a gate, never a rule, never used to choose anything. The selection
edge itself is validation_v3's (strategy_validation/cross_sectional.py), capped by universe_integrity_v1; these describe it.

  calendar            per year and quarter: strategy, selection control and excess (the v3 weekly period table)
  market_weeks        weeks in which the MARKET BENCHMARK (SPY) rose vs fell: mean strategy / control / excess - is it reversal or beta?
  portfolio           absolute performance per segment: strategy, selection control and SPY total return, annualized volatility, Sharpe (rf 0) and
                      max drawdown of the compounded weekly path - NOT selection-edge evidence
  friction            gross vs net mean weekly excess; the share of the gross excess consumed by friction; strategy friction per week and year
  construction        rebalances, eligible stocks, one-way turnover per week and annualized, selection stability
  concentration       sector (today's SEC SIC - not point in time), single-name holding frequency, per-name excess contribution and the top-five
                      share, weekly-excess concentration (v3)
  falling_knife       the next-week return distribution of the selected losers: rebound vs continued fall, vs the control, by depth of the drop
  momentum_overlap    how often the selected losers are names the 12-1 Momentum ranking (Stage 6.5) would avoid or hold - diversification only,
                      never combined
  data_quality        the DATA_INCONSISTENT exclusions (by code), the provider's ratio artifacts, extreme selected drops and single-print V shapes
                      (the latter uses the next open: reported, never a filter)
  control_membership  the period table's control members re-checked against the independent audit's re-derived eligible set
  sensitivity         the pre-declared neighbours (mean_reversion.SENSITIVITY) and the base configuration, computed on the TRAIN VIEW of the dataset
                      (dataset.until(train_end): no bar after the TRAIN end exists for them), every row on the same weekly periods; never chosen"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from mean_reversion import (CONCENTRATION_NAME_SHARE, CONCENTRATION_TOP_NAMES, FLAG_FRICTION, FLAG_NAMES, LOOKBACK_SESSIONS, NORMAL_DAY, RESEARCH_UNIVERSE,
                            SENSITIVITY)

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


def _q(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def _path(xs: List[float], ppy: float) -> dict:
    """Total return, annualized volatility, Sharpe (rf 0) and max drawdown of a compounded per-period return path."""
    if not xs:
        return {"periods": 0}
    w, peak, mdd = 1.0, 1.0, 0.0
    for x in xs:
        w *= 1.0 + x
        peak = max(peak, w)
        mdd = max(mdd, 1.0 - w / peak)
    sd = statistics.stdev(xs) if len(xs) > 1 else None
    return {"periods": len(xs), "total_return": _r(w - 1.0), "volatility_annualized": _r(sd * math.sqrt(ppy)) if sd else None,
            "sharpe_annualized": _r(statistics.mean(xs) / sd * math.sqrt(ppy), 4) if sd else None, "max_drawdown": _r(mdd)}


def _group(periods: List[dict], key) -> Dict[str, dict]:
    g: Dict[str, List[dict]] = defaultdict(list)
    for p in periods:
        g[key(p)].append(p)
    return {k: {"periods": len(v), "strategy": _r(_compound([p["strategy"]["net"] for p in v])), "control": _r(_compound([p["control"]["net"] for p in v])),
                "mean_excess": _r(statistics.mean(p["excess"] for p in v))} for k, v in sorted(g.items())}


def _period_returns(core, dataset, slip: float) -> List[dict]:
    """TRAIN-view periods of one configuration: the strategy's selection vs every eligible stock, the legs' timing and friction convention."""
    from mean_reversion.data import PriceData
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
    from mean_reversion.core import MeanReversionStrategyCore
    tv = dataset.until(train_end)
    runs = []
    for params in [{}] + list(SENSITIVITY):
        p = {**base, **params}
        runs.append((params or "base", _period_returns(MeanReversionStrategyCore(p["top_n"], p["lookback_sessions"]), tv, slip)))
    common = set.intersection(*[{x["rebalance"] for x in per} for _, per in runs]) if runs else set()
    rows = []
    for params, per in runs:
        on = [x for x in per if x["rebalance"] in common]                        # every row on the SAME weekly periods
        ex = [x["strategy"] - x["control"] for x in on]
        rows.append({"params": params, "periods": len(on), "own_periods": len(per), "strategy_total_return": _r(_compound([x["strategy"] for x in on])),
                     "control_total_return": _r(_compound([x["control"] for x in on])), "mean_excess": _r(statistics.mean(ex)) if ex else None,
                     "excess_t_stat": _r(_t(ex), 4)})
    return {"scope": f"TRAIN view only (dataset.until({train_end}): no later bar exists); every row on the same {len(common)} weekly periods (those all "
                     "configurations can form); diagnostic, never used to choose", "rows": rows}


def momentum_overlap(dataset, log: List[dict]) -> dict:
    """At each Mean Reversion rebalance with a full 12-1 history: where the selected losers sit in the 12-1 Momentum ranking AT THAT SESSION
    (momentum.core, its own rules). Diversification evidence only: nothing is combined."""
    from momentum import LOOKBACK_SESSIONS as MOM_LOOKBACK
    from momentum import RESEARCH_UNIVERSE as MOM_UNIVERSE
    from momentum.core import MomentumStrategyCore
    from momentum.data import PriceData as MomData
    mc, data = MomentumStrategyCore(), MomData(dataset)
    universe = [s for s in MOM_UNIVERSE if s in dataset.bars]
    pos = {d: i for i, d in enumerate(dataset.sessions)}
    c = Counter()
    for r in log:
        if not r.get("selected") or pos[r["rebalance"]] < MOM_LOOKBACK:
            continue
        mom = mc.evaluate(r["rebalance"], universe, data)
        order = [x["symbol"] for x in mom["ranked"]]                             # strongest 12-1 first
        if len(order) < 5:
            continue
        c["rebalances"] += 1
        q = len(order) // 5
        top_q, bottom_q, held = set(order[:q]), set(order[-q:]), set(mom["selected"])
        sel = set(r["selected"])
        c["selections"] += len(sel)
        c["in_bottom_quintile"] += len(sel & bottom_q)
        c["in_top_quintile"] += len(sel & top_q)
        c["in_momentum_portfolio"] += len(sel & held)
        c["not_momentum_eligible"] += len(sel - set(order))
        c["momentum_portfolio"] += len(held)
        c["momentum_portfolio_also_selected"] += len(held & sel)
        c["quintile_slots"] += q
        c["eligible"] += len(order)
    share = lambda a, b: _r(a / b, 4) if b else None                              # noqa: E731
    return {"rebalances": c["rebalances"], "selections": c["selections"],
            "share_of_selected_in_momentum_bottom_quintile": share(c["in_bottom_quintile"], c["selections"]),
            "share_of_selected_in_momentum_top_quintile": share(c["in_top_quintile"], c["selections"]),
            "share_of_selected_in_momentum_portfolio": share(c["in_momentum_portfolio"], c["selections"]),
            "share_of_selected_not_momentum_eligible": share(c["not_momentum_eligible"], c["selections"]),
            "share_of_momentum_portfolio_also_selected": share(c["momentum_portfolio_also_selected"], c["momentum_portfolio"]),
            "quintile_random_expectation": share(c["quintile_slots"], c["eligible"]),
            "note": "the 12-1 ranking (Momentum's own rules, momentum.core) evaluated at each weekly rebalance with a full 12-1 history: how many of the "
                    "selected losers Momentum would strongly avoid (its bottom quintile) or hold (its top quintile / its 20-stock portfolio); a share "
                    "near the random expectation means the two rankings are close to unrelated - descriptive only, never combined"}


def diagnostics(record: dict, dataset, log: List[dict], top_n: int) -> dict:
    cs = record.get("cross_sectional") or {}
    inc = [p for p in cs.get("periods") or [] if p.get("status") == "INCLUDED"]
    by_seg = {k: [p for p in inc if k == "ALL" or p["segment"] == k] for k in ("TRAIN", "OOS", "ALL")}
    ppy = float(cs.get("periods_per_year") or 52.0)
    flags: List[str] = []
    friction = {}
    for k, ps in by_seg.items():
        if not ps:
            continue
        g, n = statistics.mean(p["excess_gross"] for p in ps), statistics.mean(p["excess"] for p in ps)
        sf = statistics.mean(p["strategy"]["gross"] - p["strategy"]["net"] for p in ps)
        friction[k] = {"mean_excess_gross": _r(g), "mean_excess_net": _r(n), "share_of_gross_excess_consumed": _r((g - n) / g, 4) if g > 0 else None,
                       "strategy_friction_per_period": _r(sf), "strategy_friction_per_year": _r(sf * ppy),
                       "friction_consumes_gross_excess": bool(g > 0 and n <= 0)}
        if k in ("OOS", "ALL") and g > 0 and n <= 0 and FLAG_FRICTION not in flags:
            flags.append(FLAG_FRICTION)
    market = {}
    for k, ps in by_seg.items():
        up = [p for p in ps if (p.get("benchmark") or {}).get("return") is not None and p["benchmark"]["return"] > 0]
        dn = [p for p in ps if (p.get("benchmark") or {}).get("return") is not None and p["benchmark"]["return"] <= 0]
        market[k] = {lab: ({"periods": len(xs), "mean_strategy": _r(statistics.mean(p["strategy"]["net"] for p in xs)),
                            "mean_control": _r(statistics.mean(p["control"]["net"] for p in xs)), "mean_excess": _r(statistics.mean(p["excess"] for p in xs)),
                            "mean_benchmark": _r(statistics.mean(p["benchmark"]["return"] for p in xs))}
                           if xs else {"periods": 0}) for lab, xs in (("benchmark_up", up), ("benchmark_down", dn))}
    portfolio = {k: {"strategy": _path([p["strategy"]["net"] for p in ps], ppy), "selection_control": _path([p["control"]["net"] for p in ps], ppy),
                     "market_benchmark": _path([p["benchmark"]["return"] for p in ps if (p.get("benchmark") or {}).get("return") is not None], ppy)}
                 for k, ps in by_seg.items() if ps}
    invested = [r for r in log if r.get("selected")]
    turnover = [len(r["enter"]) / top_n for r in invested[1:]]
    stability = [len(r["hold"]) / top_n for r in invested[1:]]
    issuers = {s: info for info in ((dataset.aux or {}).get("static") or {}).get("issuers", {}).values() for s in info.get("symbols") or []}
    major_share, major_names, held = [], Counter(), Counter()
    for r in invested:
        held.update(r["selected"])
        maj = Counter(MAJOR_GROUPS.get(str((issuers.get(x) or {}).get("sic") or "")[:2], "SIC " + (str((issuers.get(x) or {}).get("sic") or "??")[:2]))
                      for x in r["selected"])
        mname, mn = maj.most_common(1)[0]
        major_share.append(mn / len(r["selected"]))
        major_names[mname] += 1
    period_of = {p["rebalance"]: p for p in inc}
    trades = ((record.get("arms") or {}).get("RULES") or {}).get("trades") or []
    feats = {c["candidate_id"]: c.get("features") or {} for c in record.get("candidates") or []}
    contrib: Counter = Counter()
    legs = []
    for t in trades:                                                              # name i's part of a period's excess: (leg - control) / positions
        p = period_of.get(t["signal_date"])
        if p is None or t.get("trade_return") is None:
            continue
        lr = float(t["trade_return"])
        contrib[t["symbol"]] += (lr - p["control"]["net"]) / p["strategy"]["positions"]
        legs.append({"ret": lr, "vs_control": lr - p["control"]["net"], "recent": (feats.get(t["candidate_id"]) or {}).get("recent_return")})
    summed = sum(p["excess"] for p in inc)
    top5 = sum(v for _, v in contrib.most_common(CONCENTRATION_TOP_NAMES))
    top5_share = (top5 / summed) if summed > 0 else None
    if top5_share is not None and top5_share >= CONCENTRATION_NAME_SHARE:
        flags.append(FLAG_NAMES)
    knife = {}
    if legs:
        rets, vs = [x["ret"] for x in legs], [x["vs_control"] for x in legs]
        deep = sorted((x for x in legs if x["recent"] is not None), key=lambda x: x["recent"])
        third = max(1, len(deep) // 3)
        terc = {"deepest_third": deep[:third], "middle_third": deep[third:2 * third], "shallowest_third": deep[2 * third:]}
        knife = {"legs": len(legs), "share_rebounding": _r(sum(1 for x in rets if x > 0) / len(rets), 4),
                 "share_falling_further": _r(sum(1 for x in rets if x < 0) / len(rets), 4),
                 "share_beating_control": _r(sum(1 for x in vs if x > 0) / len(vs), 4),
                 "next_week_return": {"mean": _r(statistics.mean(rets)), "median": _r(statistics.median(rets)), "p10": _r(_q(rets, 0.1)), "p90": _r(_q(rets, 0.9)),
                                      "worst": _r(min(rets)), "best": _r(max(rets))},
                 "by_depth_of_the_drop": {k: ({"legs": len(v), "mean_recent_return": _r(statistics.mean(x["recent"] for x in v)),
                                               "mean_next_week_return": _r(statistics.mean(x["ret"] for x in v)),
                                               "mean_vs_control": _r(statistics.mean(x["vs_control"] for x in v))} if v else {"legs": 0})
                                          for k, v in terc.items()},
                 "note": "the selected losers' net leg returns over the following week (included periods): rebound (> 0) vs continued fall (< 0); "
                         "descriptive only - no filter is derived from it in v1"}
    # data quality: what the window-integrity rule excluded, the provider's ratio artifacts, extreme selected drops and single-print V shapes
    excl = [{"rebalance": r["rebalance"], **x} for r in log for x in r.get("data_exclusions") or []]
    from mean_reversion.audit import RawData, split_events
    artifacts = [{k: (_r(v) if isinstance(v, float) else v) for k, v in ev.items()}
                 for ev in split_events(dataset, [s for s in RESEARCH_UNIVERSE if s in dataset.bars]) if ev["kind"] == "RATIO_ARTIFACT"]
    extreme, vshape = [], []
    from bisect import bisect_left
    dates = {s: [b.date for b in rows] for s, rows in dataset.bars.items()}
    for r in invested:
        for s in r["selected"]:
            rr = (r.get("recent_returns") or {}).get(s)
            if rr is not None and rr < -NORMAL_DAY:
                extreme.append({"rebalance": r["rebalance"], "symbol": s, "recent_return": _r(rr)})
            j = bisect_left(dates.get(s) or [], r["rebalance"])
            bar, prev = dataset.bar(s, r["rebalance"]), (dataset.bars.get(s) or [])[j - 1:j] if j > 0 else []
            nxt = dataset.bars_after(s, r["rebalance"])[:1]
            if bar and prev and nxt and bar.close < prev[0].close * (1 - NORMAL_DAY) and nxt[0].open > bar.close * (1 + NORMAL_DAY):
                vshape.append({"rebalance": r["rebalance"], "symbol": s, "previous_close": prev[0].close, "close": bar.close, "next_open": nxt[0].open})
    # the control's membership against the independent audit's re-derived eligible set (after the v3 block: the audit itself runs before it)
    mismatch, raw = [], RawData(dataset)
    lookback = int((((record.get("config_snapshot") or {}).get("adapter") or {}).get("params") or {}).get("lookback_sessions", LOOKBACK_SESSIONS))
    snap = {r["rebalance"]: set(r.get("eligible_symbols") or []) for r in log}   # the snapshot the control was built from (selection_snapshots)
    for p in cs.get("periods") or []:
        members = snap.get(p["rebalance"], set())
        want = set(raw.expected(p["rebalance"], lookback))
        if members != want or (p.get("control") or {}).get("members") != len(want):
            mismatch.append({"rebalance": p["rebalance"], "control_only": sorted(members - want)[:5], "audit_only": sorted(want - members)[:5],
                             "control_members": (p.get("control") or {}).get("members"), "audit_members": len(want)})
    if mismatch:
        flags.append("CONTROL_MEMBERSHIP_MISMATCH")
    elig = [r["eligible"] for r in log if r.get("eligible") is not None]
    reasons = Counter()
    for r in log:
        reasons.update(r.get("ineligible_by_reason") or {})
    fr = record.get("friction_model") or {}
    base = {"top_n": top_n, "lookback_sessions": lookback}
    return {
        "note": "research diagnostics: never a gate, never used to choose a rule; the selection edge is validation_v3's, capped by universe_integrity_v1",
        "flags": flags,
        "calendar": {"by_year": _group(inc, lambda p: p["rebalance"][:4]),
                     "by_quarter": _group(inc, lambda p: f"{p['rebalance'][:4]}-Q{(int(p['rebalance'][5:7]) - 1) // 3 + 1}")},
        "market_weeks": market,
        "portfolio": portfolio,
        "friction": friction,
        "construction": {"rebalances": len(log), "invested_rebalances": len(invested), "first_invested_rebalance": invested[0]["rebalance"] if invested else None,
                         "top_n": top_n, "periods_per_year": _r(ppy, 3), "average_one_way_turnover": _r(statistics.mean(turnover), 4) if turnover else None,
                         "annualized_one_way_turnover": _r(statistics.mean(turnover) * ppy, 4) if turnover else None,
                         "selection_stability": _r(statistics.mean(stability), 4) if stability else None, "max_single_name_weight": _r(1.0 / top_n, 4),
                         "eligible_per_rebalance": {"min": min(elig) if elig else None, "max": max(elig) if elig else None,
                                                    "mean": _r(statistics.mean(elig), 2) if elig else None},
                         "ineligible_by_reason": dict(sorted(reasons.items()))},
        "concentration": {"sector_classification": "SEC SIC major group (today's, not point in time)",
                          "sic_major_group": {"mean_top_share": _r(statistics.mean(major_share), 4) if major_share else None,
                                              "max_top_share": _r(max(major_share), 4) if major_share else None,
                                              "most_frequent_top_groups": major_names.most_common(5)},
                          "most_held_names": [(s, _r(n / len(invested), 4)) for s, n in held.most_common(10)] if invested else [],
                          "excess_contribution": {"note": "sum over a name's included legs of (leg return - control return) / positions: its share of "
                                                          "the summed weekly excess (ALL periods)",
                                                  "top": [(s, _r(v)) for s, v in contrib.most_common(5)],
                                                  "bottom": [(s, _r(v)) for s, v in sorted(contrib.items(), key=lambda kv: kv[1])[:5]],
                                                  "summed_period_excess": _r(summed),
                                                  "top_five_share_of_summed_excess": _r(top5_share, 4),
                                                  "names_contributing": len(contrib)},
                          "period_excess": {k: (v or {}).get("concentration") for k, v in (cs.get("segments") or {}).items()}},
        "falling_knife": knife,
        "momentum_overlap": momentum_overlap(dataset, log),
        "data_quality": {"data_inconsistent_exclusions": len(excl), "by_code": dict(sorted(Counter(x["code"] for x in excl).items())),
                         "exclusions": [{k: (_r(v) if isinstance(v, float) else v) for k, v in x.items()} for x in excl[:50]],
                         "ratio_artifacts": artifacts, "extreme_selected_drops": extreme[:50], "extreme_selected_drop_count": len(extreme),
                         "single_print_v_shapes": vshape,
                         "note": "exclusions: a signal window the corporate-action contract finds inconsistent (never clipped); extreme drops (< -25 % over "
                                 "the lookback) and single-print V shapes (a close > 25 % below both the previous close and the next open) are listed "
                                 "for inspection - the latter needs the next open, so it is never a filter"},
        "control_membership": {"periods_checked": len(cs.get("periods") or []), "mismatches": len(mismatch), "first_mismatches": mismatch[:5]},
        "sensitivity": sensitivity(dataset, record.get("train_end") or "", float(fr.get("slippage_bps_per_side") or 0.0) / 10000.0, base),
    }
