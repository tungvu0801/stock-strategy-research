"""breakout/diagnostics.py — Breakout RESEARCH diagnostics: never a gate, never a rule, never used to choose anything. The selection edge itself is
validation_v3's (strategy_validation/cross_sectional.py, on the fixed-slot cash basis), capped by universe_integrity_v1; these describe it. Every
definition below was declared before the OOS run (DESIGN_67_BREAKOUT.md, section 2).

  exposure            invested fraction (positions / N) per week: mean, median, min, max; weeks with 0 / 1-5 / 6-10 / 11-19 / N positions;
                      eligible stocks and breakout candidates per rebalance - a result from sitting in cash is not selection skill
  market_sensitivity  the strategy's and the control's beta to SPY per segment; SPY-up vs SPY-down weeks (mean strategy / control / excess)
  invested_capital    per week with a position: the mean of the selected legs minus the control (the selection effect on the capital actually
                      invested, free of the cash effect) - its mean, t over weeks, win rate; a pre-registered diagnostic, never the v3 edge
  portfolio           absolute performance per segment: strategy, selection control and SPY total return, annualized volatility, Sharpe (rf 0),
                      max drawdown - NOT selection-edge evidence
  friction            gross vs net mean weekly excess; the share of a positive gross excess consumed by friction; strategy friction a year
  construction        rebalances, capital turnover (entries / N a week, annualized), selection stability
  follow_through      the selected legs' gross return over their week: > +1 % continued higher, within +-1 % flat, < -1 % failed
  failed_breakouts    legs ending below their entry price; legs whose exit price ends below their breakout reference
  concentration       sector (today's SEC SIC - not point in time), single names, each name's share of the summed excess ((leg - control) / N)
                      and the CASH share (-control x unused slots / N), the five largest names' share, weekly-excess concentration (v3)
  momentum_overlap    how often a breakout holding is also in the 12-1 Momentum portfolio at the same session (momentum.core's own rules) and the
                      correlation of the two selections' weekly excess - independence evidence only, never combined
  data_quality        DATA_INCONSISTENT exclusions by code, the provider's ratio artifacts, inconsistent data inside a HELD window (strategy or
                      control) with its effect on the week, breakouts above +25 % and single-print spikes (the latter needs the next open:
                      reported, never a filter)
  control_membership  the control snapshot re-checked against the independent audit's re-derived eligible set
  cash_accounting     every included week's strategy return re-derived from the stored trades: sum of the legs / N
  sensitivity         the pre-declared neighbours (breakout.SENSITIVITY) and the base, on the TRAIN VIEW only (dataset.until(train_end)), on the
                      same fixed-slot cash basis, every row on the same weeks; never used to choose"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from breakout import (CONCENTRATION_NAME_SHARE, CONCENTRATION_TOP_NAMES, FLAG_FRICTION, FLAG_HOLDING_ANOMALY, FLAG_NAMES, FOLLOW_THROUGH_BAND,
                      LOOKBACK_SESSIONS, NORMAL_DAY, POSITION_BUCKETS, RESEARCH_UNIVERSE, SENSITIVITY)

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


def _beta(ys: List[float], xs: List[float]) -> Optional[float]:
    if len(xs) < 6:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var > 0 else None


def _corr(a: List[float], b: List[float]) -> Optional[float]:
    if len(a) < 6:
        return None
    try:
        return statistics.correlation(a, b)
    except statistics.StatisticsError:
        return None


def _path(xs: List[float], ppy: float) -> dict:
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
                "mean_excess": _r(statistics.mean(p["excess"] for p in v)), "mean_invested_fraction": _r(statistics.mean(p["strategy"].get("invested_fraction", 0) for p in v), 4)}
            for k, v in sorted(g.items())}


def _period_returns(core, dataset, slip: float) -> List[dict]:
    """TRAIN-view weeks of one configuration on the fixed-slot cash basis: the strategy's breakouts (sum / N, cash 0 %) vs every eligible stock."""
    from breakout.data import PriceData
    from strategy_validation.cross_sectional import _after, _window
    data = PriceData(dataset)
    universe = [s for s in RESEARCH_UNIVERSE if s in dataset.bars]
    decs = [core.evaluate(R, universe, data) for R in core.schedule(dataset.sessions)]
    out = []
    for k in range(len(decs) - 1):                                               # the last period would end at the TRAIN end: not a full period
        R, nxt = decs[k]["rebalance"], decs[k + 1]["rebalance"]
        sel, elig = decs[k]["selected"], decs[k]["eligible"]
        if not elig:
            continue
        prev_s = set(decs[k - 1]["selected"]) if k else set()
        prev_e = set(decs[k - 1]["eligible"]) if k else set()
        next_s, next_e = set(decs[k + 1]["selected"]), set(decs[k + 1]["eligible"])

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
        out.append({"rebalance": R, "strategy": sum(s) / core.max_holdings, "control": statistics.mean(c), "positions": len(s)})
    return out


def sensitivity(dataset, train_end: str, slip: float, base: dict) -> dict:
    from breakout.core import BreakoutStrategyCore
    tv = dataset.until(train_end)
    runs = []
    for params in [{}] + list(SENSITIVITY):
        p = {**base, **params}
        runs.append((params or "base", _period_returns(BreakoutStrategyCore(p["max_holdings"], p["lookback_sessions"]), tv, slip)))
    common = set.intersection(*[{x["rebalance"] for x in per} for _, per in runs]) if runs else set()
    rows = []
    for params, per in runs:
        on = [x for x in per if x["rebalance"] in common]                        # every row on the SAME weekly periods
        ex = [x["strategy"] - x["control"] for x in on]
        rows.append({"params": params, "periods": len(on), "own_periods": len(per), "strategy_total_return": _r(_compound([x["strategy"] for x in on])),
                     "control_total_return": _r(_compound([x["control"] for x in on])), "mean_excess": _r(statistics.mean(ex)) if ex else None,
                     "excess_t_stat": _r(_t(ex), 4), "mean_positions": _r(statistics.mean(x["positions"] for x in on), 2) if on else None})
    return {"scope": f"TRAIN view only (dataset.until({train_end}): no later bar exists); fixed-slot cash basis (each slot 100 / N %); every row on the "
                     f"same {len(common)} weekly periods; diagnostic, never used to choose", "rows": rows}


def holding_window_anomalies(record: dict, dataset, log: List[dict], slots: int, distributions: bool = False) -> List[dict]:
    """Inconsistent day-pairs inside a HELD window - a strategy leg (its simulated entry -> exit) or a control position - with the effect on the
    week's excess: +step / N for a strategy leg (fixed slots), -step / members for a control position. A provider-ADJUSTED large distribution
    (LARGE_REBASING: a spin-off) is correct total return inside a held window - it only makes a LEVEL reference unverifiable - so it is listed
    apart (distributions=True), with the excess at stake if the adjustment were wrong, and never raises the anomaly flag."""
    from breakout.audit import inconsistent_pairs
    from strategy_validation.cross_sectional import _after, _window
    pairs: Dict[str, List[dict]] = defaultdict(list)
    for ev in inconsistent_pairs(dataset, [s for s in RESEARCH_UNIVERSE if s in dataset.bars]):
        if (ev["code"] == "LARGE_REBASING") == distributions:                     # a held window: an adjusted distribution is total return, not an error
            pairs[ev["symbol"]].append(ev)
    if not pairs:
        return []
    period = {p["rebalance"]: p for p in (record.get("cross_sectional") or {}).get("periods") or []}
    out = []
    for t in ((record.get("arms") or {}).get("RULES") or {}).get("trades") or []:
        for ev in pairs.get(t["symbol"], []):
            if t["entry_date"] <= ev["from"] and ev["to"] <= t["exit_date"]:
                p = period.get(t["signal_date"]) or {}
                out.append({"rebalance": t["signal_date"], "symbol": t["symbol"], "side": "strategy", "code": ev["code"], "from": ev["from"], "to": ev["to"],
                            "step": _r(ev["step"]), "effect_on_period_excess": _r(ev["step"] / slots), "period_included": p.get("status") == "INCLUDED"})
    rebs = [r["rebalance"] for r in log]
    for k, r in enumerate(log):
        nxt = rebs[k + 1] if k + 1 < len(rebs) else None
        p = period.get(r["rebalance"]) or {}
        m = ((p.get("control") or {}).get("members")) or len(r.get("eligible_symbols") or []) or 1
        for s in r.get("eligible_symbols") or []:
            for ev in pairs.get(s, []):
                w = _window(_after(dataset, s, r["rebalance"]), nxt)
                if w is not None and w[0].date <= ev["from"] and ev["to"] <= w[1].date:
                    out.append({"rebalance": r["rebalance"], "symbol": s, "side": "control", "code": ev["code"], "from": ev["from"], "to": ev["to"],
                                "step": _r(ev["step"]), "effect_on_period_excess": _r(-ev["step"] / m), "period_included": p.get("status") == "INCLUDED"})
    return sorted(out, key=lambda x: (x["rebalance"], x["side"], x["symbol"]))


def momentum_overlap(record: dict, dataset, log: List[dict], slots: int) -> dict:
    """At each Breakout rebalance with a full 12-1 history: the 12-1 Momentum portfolio AT THAT SESSION (momentum.core, its own rules) - the share of
    Breakout holdings also in it, per week, and the correlation of the two selections' weekly gross excess over the control (the same windows).
    Independence evidence only: nothing is combined."""
    from momentum import LOOKBACK_SESSIONS as MOM_LOOKBACK
    from momentum import RESEARCH_UNIVERSE as MOM_UNIVERSE
    from momentum.core import MomentumStrategyCore
    from momentum.data import PriceData as MomData
    from strategy_validation.cross_sectional import _after, _window
    mc, data = MomentumStrategyCore(), MomData(dataset)
    universe = [s for s in MOM_UNIVERSE if s in dataset.bars]
    pos = {d: i for i, d in enumerate(dataset.sessions)}
    rebs = [r["rebalance"] for r in log]
    c, per_week, bo_ex, mo_ex = Counter(), [], [], []
    for k, r in enumerate(log):
        if not r.get("selected") or pos[r["rebalance"]] < MOM_LOOKBACK:
            continue
        mom = mc.evaluate(r["rebalance"], universe, data)
        top = set(mom["selected"])
        sel = set(r["selected"])
        el = set(r.get("eligible_symbols") or [])
        c["chance"] += len(sel) * (len(top & el) / len(el) if el else 0.0)            # a random pick of as many stocks from the same eligible set
        c["weeks"] += 1
        c["holdings"] += len(sel)
        c["also_in_momentum"] += len(sel & top)
        per_week.append(len(sel & top) / len(sel))
        nxt = rebs[k + 1] if k + 1 < len(rebs) else None

        def gross(sym):
            w = _window(_after(dataset, sym, r["rebalance"]), nxt)
            return None if w is None else w[2] / w[0].open - 1.0
        ctl = [g for g in (gross(s) for s in r.get("eligible_symbols") or []) if g is not None]
        bo = [g for g in (gross(s) for s in sel) if g is not None]
        mo = [g for g in (gross(s) for s in top) if g is not None]
        if ctl and bo and mo:
            cm = statistics.mean(ctl)
            bo_ex.append(statistics.mean(bo) - cm)
            mo_ex.append(statistics.mean(mo) - cm)
    share = lambda a, b: _r(a / b, 4) if b else None                              # noqa: E731
    return {"weeks": c["weeks"], "breakout_holdings": c["holdings"], "share_of_breakout_holdings_in_momentum_portfolio": share(c["also_in_momentum"], c["holdings"]),
            "share_expected_by_chance": share(c["chance"], c["holdings"]),
            "mean_weekly_overlap": _r(statistics.mean(per_week), 4) if per_week else None,
            "weekly_excess_correlation": _r(_corr(bo_ex, mo_ex), 4), "correlation_weeks": len(bo_ex),
            "note": "Momentum's 20-stock 12-1 portfolio evaluated at each weekly Breakout rebalance (momentum.core); the correlation is between the two "
                    "selections' weekly gross excess over the same control, on invested capital - independence evidence only, never combined"}


def diagnostics(record: dict, dataset, log: List[dict], max_holdings: int) -> dict:
    cs = record.get("cross_sectional") or {}
    periods = cs.get("periods") or []
    inc = [p for p in periods if p.get("status") == "INCLUDED"]
    by_seg = {k: [p for p in inc if k == "ALL" or p["segment"] == k] for k in ("TRAIN", "OOS", "ALL")}
    ppy = float(cs.get("periods_per_year") or 52.0)
    slots = max_holdings
    flags: List[str] = []
    # ---- exposure: cash is not selection skill
    exposure = {}
    for k, ps in by_seg.items():
        if not ps:
            continue
        f = [p["strategy"].get("invested_fraction", p["strategy"]["positions"] / slots) for p in ps]
        n = [p["strategy"]["positions"] for p in ps]
        exposure[k] = {"weeks": len(ps), "mean_invested_fraction": _r(statistics.mean(f), 4), "median_invested_fraction": _r(statistics.median(f), 4),
                       "min_invested_fraction": _r(min(f), 4), "max_invested_fraction": _r(max(f), 4), "mean_positions": _r(statistics.mean(n), 2),
                       "weeks_by_positions": {f"{a}" if a == b else f"{a}-{b}": sum(1 for x in n if a <= x <= b) for a, b in POSITION_BUCKETS}}
    elig = [r["eligible"] for r in log]
    cands = [r["breakout_candidates"] for r in log]
    exposure["per_rebalance"] = {"eligible": {"min": min(elig) if elig else None, "max": max(elig) if elig else None, "mean": _r(statistics.mean(elig), 2) if elig else None},
                                 "breakout_candidates": {"min": min(cands) if cands else None, "max": max(cands) if cands else None,
                                                         "mean": _r(statistics.mean(cands), 2) if cands else None,
                                                         "weeks_above_max_holdings": sum(1 for x in cands if x > slots)}}
    # ---- market sensitivity and the invested-capital (cash-free) selection effect
    market, sensitivity_b, invested = {}, {}, {}
    for k, ps in by_seg.items():
        bp = [p for p in ps if (p.get("benchmark") or {}).get("return") is not None]
        up = [p for p in bp if p["benchmark"]["return"] > 0]
        dn = [p for p in bp if p["benchmark"]["return"] <= 0]
        market[k] = {lab: ({"periods": len(xs), "mean_strategy": _r(statistics.mean(p["strategy"]["net"] for p in xs)),
                            "mean_control": _r(statistics.mean(p["control"]["net"] for p in xs)), "mean_excess": _r(statistics.mean(p["excess"] for p in xs)),
                            "mean_benchmark": _r(statistics.mean(p["benchmark"]["return"] for p in xs)),
                            "mean_invested_fraction": _r(statistics.mean(p["strategy"].get("invested_fraction", 0) for p in xs), 4)}
                           if xs else {"periods": 0}) for lab, xs in (("benchmark_up", up), ("benchmark_down", dn))}
        spy = [p["benchmark"]["return"] for p in bp]
        sensitivity_b[k] = {"strategy_beta_to_spy": _r(_beta([p["strategy"]["net"] for p in bp], spy), 4),
                            "control_beta_to_spy": _r(_beta([p["control"]["net"] for p in bp], spy), 4),
                            "strategy_correlation_to_spy": _r(_corr([p["strategy"]["net"] for p in bp], spy), 4)}
        held = [p for p in ps if p["strategy"]["positions"]]
        x = [p["strategy"]["net"] * slots / p["strategy"]["positions"] - p["control"]["net"] for p in held]
        sel = [p["strategy"]["net"] - p["strategy"].get("invested_fraction", 0.0) * p["control"]["net"] for p in ps]
        invested[k] = {"weeks_with_a_position": len(held), "mean_excess_on_invested_capital": _r(statistics.mean(x)) if x else None,
                       "t_stat": _r(_t(x), 4), "win_rate": _r(sum(1 for v in x if v > 0) / len(x), 4) if x else None,
                       "selection_part_mean": _r(statistics.mean(sel)), "selection_part_t": _r(_t(sel), 4),
                       "cash_part_mean": _r(statistics.mean(-(1.0 - p["strategy"].get("invested_fraction", 0.0)) * p["control"]["net"] for p in ps))}
    invested["note"] = ("per week with a position: the mean of the selected legs minus the control (unweighted: a 1-stock week counts like a 20-stock "
                        "week); selection_part / cash_part: the exact split of every week's excess - invested fraction x (legs - control) and "
                        "-(1 - fraction) x control - the selection part is what the v3 cash guard reads")
    friction = {}
    for k, ps in by_seg.items():
        if not ps:
            continue
        g, n = statistics.mean(p["excess_gross"] for p in ps), statistics.mean(p["excess"] for p in ps)
        sf = statistics.mean(p["strategy"]["gross"] - p["strategy"]["net"] for p in ps)
        friction[k] = {"mean_excess_gross": _r(g), "mean_excess_net": _r(n), "share_of_gross_excess_consumed": _r((g - n) / g, 4) if g > 0 else None,
                       "strategy_friction_per_period": _r(sf), "strategy_friction_per_year": _r(sf * ppy), "friction_consumes_gross_excess": bool(g > 0 and n <= 0)}
        if k in ("OOS", "ALL") and g > 0 and n <= 0 and FLAG_FRICTION not in flags:
            flags.append(FLAG_FRICTION)
    portfolio = {k: {"strategy": _path([p["strategy"]["net"] for p in ps], ppy), "selection_control": _path([p["control"]["net"] for p in ps], ppy),
                     "market_benchmark": _path([p["benchmark"]["return"] for p in ps if (p.get("benchmark") or {}).get("return") is not None], ppy)}
                 for k, ps in by_seg.items() if ps}
    invested_r = [r for r in log if r.get("selected")]
    cap_turn = [len(r["enter"]) / slots for r in log[1:]]
    stability = [len(r["hold"]) / len(r["selected"]) for r in log[1:] if r.get("selected")]
    # ---- follow-through and failed breakouts (selected legs)
    trades = ((record.get("arms") or {}).get("RULES") or {}).get("trades") or []
    feats = {c["candidate_id"]: c.get("features") or {} for c in record.get("candidates") or []}
    period_of = {p["rebalance"]: p for p in inc}
    legs = [t for t in trades if t["signal_date"] in period_of]
    follow, failed = {}, {}
    if legs:
        g = [float(t["exit_raw"]) / float(t["entry_raw"]) - 1.0 for t in legs]
        ctl_g = {p["rebalance"]: p["control"]["gross"] for p in inc}
        rel = [x - ctl_g[t["signal_date"]] for x, t in zip(g, legs)]
        follow_rel = {"above_control": _r(sum(1 for x in rel if x > FOLLOW_THROUGH_BAND) / len(rel), 4),
                      "within_band": _r(sum(1 for x in rel if -FOLLOW_THROUGH_BAND <= x <= FOLLOW_THROUGH_BAND) / len(rel), 4),
                      "below_control": _r(sum(1 for x in rel if x < -FOLLOW_THROUGH_BAND) / len(rel), 4), "mean": _r(statistics.mean(rel))}
        follow = {"legs": len(g), "relative_to_control": follow_rel, "continued_higher": _r(sum(1 for x in g if x > FOLLOW_THROUGH_BAND) / len(g), 4),
                  "flat": _r(sum(1 for x in g if -FOLLOW_THROUGH_BAND <= x <= FOLLOW_THROUGH_BAND) / len(g), 4),
                  "failed": _r(sum(1 for x in g if x < -FOLLOW_THROUGH_BAND) / len(g), 4),
                  "mean_gross_leg_return": _r(statistics.mean(g)), "median_gross_leg_return": _r(statistics.median(g)), "band": FOLLOW_THROUGH_BAND}
        below_ref = [float(t["exit_raw"]) < float((feats.get(t["candidate_id"]) or {}).get("breakout_reference") or 0) for t in legs]
        failed = {"legs": len(legs), "share_ending_below_entry_price": _r(sum(1 for x in g if x < 0) / len(g), 4),
                  "share_ending_below_breakout_reference": _r(sum(below_ref) / len(below_ref), 4),
                  "note": "the exit price (the open after the next rebalance, adjusted) against the leg's entry price and against its breakout reference "
                          "(the prior 20-session high, adjusted): descriptive only, no confirmation filter in v1"}
    # ---- concentration: names, sectors, the cash part of the excess
    issuers = {s: info for info in ((dataset.aux or {}).get("static") or {}).get("issuers", {}).values() for s in info.get("symbols") or []}
    major_share, major_names, held_names = [], Counter(), Counter()
    for r in invested_r:
        held_names.update(r["selected"])
        maj = Counter(MAJOR_GROUPS.get(str((issuers.get(x) or {}).get("sic") or "")[:2], "SIC " + (str((issuers.get(x) or {}).get("sic") or "??")[:2]))
                      for x in r["selected"])
        mname, mn = maj.most_common(1)[0]
        major_share.append(mn / len(r["selected"]))
        major_names[mname] += 1
    contrib: Counter = Counter()
    for t in legs:
        p = period_of[t["signal_date"]]
        if t.get("trade_return") is not None:
            contrib[t["symbol"]] += (float(t["trade_return"]) - p["control"]["net"]) / slots
    cash_part = sum(-p["control"]["net"] * (slots - p["strategy"]["positions"]) / slots for p in inc)
    summed = sum(p["excess"] for p in inc)
    top5 = sum(v for _, v in contrib.most_common(CONCENTRATION_TOP_NAMES))
    top5_share = (top5 / summed) if summed > 0 else None
    if top5_share is not None and top5_share >= CONCENTRATION_NAME_SHARE:
        flags.append(FLAG_NAMES)
    # ---- data quality
    excl = [{"rebalance": r["rebalance"], **x} for r in log for x in r.get("data_exclusions") or []]
    from breakout.audit import RawData, split_events
    in_held = holding_window_anomalies(record, dataset, log, slots)
    dist_held = holding_window_anomalies(record, dataset, log, slots, distributions=True)
    if any(x["period_included"] for x in in_held):
        flags.append(FLAG_HOLDING_ANOMALY)
    artifacts = [{k: (_r(v) if isinstance(v, float) else v) for k, v in ev.items()}
                 for ev in split_events(dataset, [s for s in RESEARCH_UNIVERSE if s in dataset.bars]) if ev["kind"] == "RATIO_ARTIFACT"]
    extreme, spikes = [], []
    from bisect import bisect_left
    dates = {s: [b.date for b in rows] for s, rows in dataset.bars.items()}
    for r in invested_r:
        for s in r["selected"]:
            st = ((r.get("candidates") or {}).get(s) or {}).get("strength")
            if st is not None and st > NORMAL_DAY:
                extreme.append({"rebalance": r["rebalance"], "symbol": s, "strength": _r(st)})
            j = bisect_left(dates.get(s) or [], r["rebalance"])
            bar, prev = dataset.bar(s, r["rebalance"]), (dataset.bars.get(s) or [])[j - 1:j] if j > 0 else []
            nxt = dataset.bars_after(s, r["rebalance"])[:1]
            if bar and prev and nxt and bar.close > prev[0].close * (1 + NORMAL_DAY) and nxt[0].open < bar.close * (1 - NORMAL_DAY / 2):
                spikes.append({"rebalance": r["rebalance"], "symbol": s, "previous_close": prev[0].close, "close": bar.close, "next_open": nxt[0].open})
    # ---- the control membership and the cash accounting, re-derived independently
    lookback = int((((record.get("config_snapshot") or {}).get("adapter") or {}).get("params") or {}).get("lookback_sessions", LOOKBACK_SESSIONS))
    raw, mismatch = RawData(dataset), []
    snap = {r["rebalance"]: set(r.get("eligible_symbols") or []) for r in log}
    for p in periods:
        want = set(raw.expected(p["rebalance"], lookback))
        if snap.get(p["rebalance"], set()) != want or (p.get("control") or {}).get("members") != len(want):
            mismatch.append({"rebalance": p["rebalance"], "control_only": sorted(snap.get(p["rebalance"], set()) - want)[:5], "audit_only": sorted(want - snap.get(p["rebalance"], set()))[:5]})
    if mismatch:
        flags.append("CONTROL_MEMBERSHIP_MISMATCH")
    by_r: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        by_r[t["signal_date"]].append(float(t["trade_return"]))
    cash_bad = [p["rebalance"] for p in inc if abs(sum(by_r.get(p["rebalance"], [])) / slots - float(p["strategy"]["net"])) > 1e-9
                or p["strategy"]["positions"] != len(by_r.get(p["rebalance"], []))]
    if cash_bad:
        flags.append("CASH_ACCOUNTING_MISMATCH")
    fr = record.get("friction_model") or {}
    return {
        "note": "research diagnostics: never a gate, never used to choose a rule; the selection edge is validation_v3's (fixed-slot cash basis), capped by "
                "universe_integrity_v1",
        "flags": flags,
        "exposure": exposure,
        "market_weeks": market,
        "market_sensitivity": sensitivity_b,
        "invested_capital": invested,
        "portfolio": portfolio,
        "friction": friction,
        "construction": {"rebalances": len(log), "rebalances_with_a_position": len(invested_r), "max_holdings": slots, "slot_pct": _r(100.0 / slots, 4),
                         "periods_per_year": _r(ppy, 3),
                         "average_one_way_turnover": _r(statistics.mean(cap_turn), 4) if cap_turn else None,
                         "annualized_one_way_turnover": _r(statistics.mean(cap_turn) * ppy, 4) if cap_turn else None,
                         "turnover_basis": "entries / N slots a week: the share of the total capital bought (continuations pay nothing)",
                         "selection_stability": _r(statistics.mean(stability), 4) if stability else None,
                         "ineligible_by_reason": dict(sorted(sum((Counter(r.get("ineligible_by_reason") or {}) for r in log), Counter()).items()))},
        "calendar": {"by_year": _group(inc, lambda p: p["rebalance"][:4])},
        "follow_through": follow,
        "failed_breakouts": failed,
        "concentration": {"sector_classification": "SEC SIC major group (today's, not point in time)",
                          "sic_major_group": {"mean_top_share": _r(statistics.mean(major_share), 4) if major_share else None,
                                              "max_top_share": _r(max(major_share), 4) if major_share else None,
                                              "most_frequent_top_groups": major_names.most_common(5)},
                          "most_held_names": [(s, _r(n / len(log), 4)) for s, n in held_names.most_common(10)] if log else [],
                          "excess_contribution": {"note": "a name's part of the summed weekly excess: (leg - control) / N over its included legs; the CASH "
                                                          "part is -control x unused slots / N",
                                                  "top": [(s, _r(v)) for s, v in contrib.most_common(5)],
                                                  "bottom": [(s, _r(v)) for s, v in sorted(contrib.items(), key=lambda kv: kv[1])[:5]],
                                                  "cash": _r(cash_part), "names": _r(sum(contrib.values())), "summed_period_excess": _r(summed),
                                                  "top_five_share_of_summed_excess": _r(top5_share, 4), "names_contributing": len(contrib)},
                          "period_excess": {k: (v or {}).get("concentration") for k, v in (cs.get("segments") or {}).items()}},
        "momentum_overlap": momentum_overlap(record, dataset, log, slots),
        "data_quality": {"data_inconsistent_exclusions": len(excl), "by_code": dict(sorted(Counter(x["code"] for x in excl).items())),
                         "exclusions": [{k: (_r(v) if isinstance(v, float) else v) for k, v in x.items()} for x in excl[:60]],
                         "ratio_artifacts": artifacts, "holding_window_anomalies": in_held[:50], "holding_window_anomaly_count": len(in_held),
                         "holding_window_effect_on_summed_excess": _r(sum(x["effect_on_period_excess"] for x in in_held if x["period_included"])),
                         "adjusted_distributions_in_held_windows": [{**{k: v for k, v in x.items() if k != "effect_on_period_excess"},
                                                                     "excess_at_stake_if_misadjusted": x["effect_on_period_excess"]} for x in dist_held[:50]],
                         "extreme_breakouts": extreme[:50], "extreme_breakout_count": len(extreme), "single_print_spikes": spikes,
                         "note": "exclusions: a breakout window the corporate-action contract finds inconsistent (never clipped); extreme breakouts (> +25 % "
                                 "above the reference) and single-print spikes (a close > 25 % above the previous close that the next open gives back by more "
                                 "than half) are listed for inspection - the latter needs the next open, so it is never a filter"},
        "control_membership": {"periods_checked": len(periods), "mismatches": len(mismatch), "first_mismatches": mismatch[:5]},
        "cash_accounting": {"periods_checked": len(inc), "mismatches": len(cash_bad), "first_mismatches": cash_bad[:5],
                            "basis": f"strategy week = sum of the legs' net returns / {slots} (cash at 0 %), re-derived from the stored trades"},
        "sensitivity": sensitivity(dataset, record.get("train_end") or "", float(fr.get("slippage_bps_per_side") or 0.0) / 10000.0,
                                   {"max_holdings": max_holdings, "lookback_sessions": lookback}),
    }
