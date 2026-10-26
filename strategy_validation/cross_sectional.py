"""strategy_validation/cross_sectional.py — Stage 6.4.1 (validation_v3): the SELECTION EDGE of a CROSS_SECTIONAL_PORTFOLIO strategy, judged per
rebalance period against its declared SELECTION CONTROL (DESIGN_641_CROSS_SECTIONAL_VALIDATION.md).

The ~20 legs of one rebalance share the same month: they are ONE observation, not twenty. Evidence of selection is the strategy's return over
the control's - a portfolio that holds the same eligible universe with the same timing, friction and capital basis but without the selection -
measured once per period. SPY is a MARKET BENCHMARK only (context, never selection evidence).

  period_table(...)      one row per rebalance: INCLUDED (TRAIN / OOS) or EXCLUDED with its reason, the strategy's legs and the control's
                         positions with their net and gross returns, SPY, the excess
  parity_audit(...)      the comparison is fair: same periods, universe snapshot, timestamps, capital convention and friction; no missing or
                         one-sided period; the boundary period excluded on both sides; the legs reconcile
  segment_stats(rows)    the period-level metrics of a segment
  selection_edge(...)    NOT_APPLICABLE | INVALID | INSUFFICIENT_DATA | NOT_ESTABLISHED | WEAK | ESTABLISHED, with its reasons
  overall_verdict(...)   a cross-sectional strategy reaches PASS only with an ESTABLISHED edge (the absolute verdict is kept beside it)
  evaluate(...)          the `cross_sectional` block of a validation_v3 record
  assess_stored_run(...) a METHODOLOGY EVALUATION of an older stored run: it never rewrites the run

Pure: the record, the trades, the snapshots and the dataset; no clock (except the evaluation timestamp), no network, no broker, no model."""
from __future__ import annotations

import math
import statistics
import uuid
from bisect import bisect_right
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from strategy_validation import (BENCHMARK_ROLE, CAPITAL_CONVENTION, CONTROL_ROLE, CS_CONCENTRATION_SHARE, CS_MIN_OOS_PERIODS, CS_MIN_PERIODS_T,
                                 CS_MIN_TOTAL_PERIODS, CS_T_ESTABLISHED, CS_T_NEGLIGIBLE, CS_TRAIN_CONTRADICTION_T, EDGE_LABELS, PURGE_REASON, VERSION)
from strategy_validation import verdict as V

CROSS_SECTIONAL = "CROSS_SECTIONAL_PORTFOLIO"
EVENT_DRIVEN = "EVENT_DRIVEN"
POLICY = {"min_oos_periods": CS_MIN_OOS_PERIODS, "min_total_periods": CS_MIN_TOTAL_PERIODS, "min_periods_for_t": CS_MIN_PERIODS_T,
          "t_negligible": CS_T_NEGLIGIBLE, "t_established": CS_T_ESTABLISHED, "train_contradiction_t": CS_TRAIN_CONTRADICTION_T,
          "concentration_share": CS_CONCENTRATION_SHARE, "primary_statistic": "OOS mean period excess over the selection control, t over periods"}
PARITY_CHECKS = ("SAME_PERIODS", "SAME_UNIVERSE_SNAPSHOT", "SAME_PRICE_TIMESTAMPS", "SAME_CAPITAL_CONVENTION", "SAME_FRICTION", "NO_MISSING_PERIOD",
                 "BOUNDARY_BOTH_SIDES", "PERIOD_ACCOUNTING")
LEG_T_NOTE = "per-leg t-statistic: a trade-level description - NOT SELECTION-EDGE EVIDENCE (the legs of one rebalance are correlated)"


class EvaluationError(ValueError):
    """A methodology evaluation cannot be made honestly (wrong dataset, a candidate set that does not reproduce, an event-driven run...)."""


def _r(x: Optional[float], n: int = 6) -> Optional[float]:
    return None if x is None else round(float(x), n)


def _after(dataset, symbol: str, d: str) -> list:
    """Every bar of the symbol strictly after `d` (whether or not the symbol has a bar on `d`)."""
    rows = dataset.bars.get(symbol) or []
    i = bisect_right([b.date for b in rows], d)
    return rows[i:]


def _window(after: list, next_r: Optional[str]):
    """(entry bar, exit bar, exit price, exit kind): enter at the first bar after the rebalance's open; exit at the open of the first bar after
    the next rebalance, or at the last close when there is none (DATA_END) - the strategy legs' rule."""
    if not after:
        return None
    if next_r is not None:
        x = next((b for b in after if b.date > next_r), None)
        if x is not None:
            return after[0], x, x.open, "TIME_EXIT"
    return after[0], after[-1], after[-1].close, "DATA_END"


# ---------------------------------------------------------------- the period table ------------------------------------------------------------
def period_table(rec: dict, trades: List[dict], candidates: List[dict], dataset, snapshots: List[dict], benchmark: Optional[str] = "SPY") -> List[dict]:
    fr = rec.get("friction_model") or {}
    slip = float(fr.get("slippage_bps_per_side") or 0.0) / 10000.0
    commission, fee = float(fr.get("commission_per_order") or 0.0), float(fr.get("fee_per_share") or 0.0)
    sizing = (rec.get("config_snapshot") or {}).get("sizing") or {}
    notional = float(sizing.get("equity") or 100000.0) * float(sizing.get("position_pct") or 0.0) / 100.0
    train_end = rec.get("train_end") or ""
    rules = (rec.get("arms") or {}).get("RULES") or {}
    purged = set(((rec.get("boundary") or {}).get("purged") or {}).get("RULES") or [])
    skips = {s["candidate_id"]: s.get("reason") for s in rules.get("simulation_skips") or [] if s.get("reason") != PURGE_REASON}
    traded = {t["candidate_id"]: t for t in trades if t.get("arm", "RULES") == "RULES"}
    legs_at: Dict[str, List[dict]] = {}
    for c in candidates:
        legs_at.setdefault(c["signal_date"], []).append(c)
    rows = []
    for k, snap in enumerate(snapshots):
        R = snap["rebalance"]
        nxt = snapshots[k + 1]["rebalance"] if k + 1 < len(snapshots) else None
        prev_m = set(snapshots[k - 1]["eligible"]) if k > 0 else set()
        next_m = set(snapshots[k + 1]["eligible"]) if nxt else set()
        members = sorted(set(snap.get("eligible") or []))
        # ---- the control: every eligible stock, equal capital, the legs' timing and friction convention
        ctrl, missing = [], []
        for m in members:
            w = _window(_after(dataset, m, R), nxt)
            if w is None:
                missing.append(m)
                continue
            eb, xb, xpx, kind = w
            e_tx, x_tx = m not in prev_m, (nxt is None or m not in next_m)
            ef = eb.open * (1 + slip) if e_tx else eb.open
            xf = xpx * (1 - slip) if x_tx else xpx
            n_tx = int(e_tx) + int(x_tx)
            cost = (n_tx * commission / notional if notional else 0.0) + n_tx * fee / ef
            ctrl.append({"symbol": m, "entry_date": eb.date, "exit_date": xb.date, "entry_raw": eb.open, "exit_raw": xpx, "exit_kind": kind, "entry_trade": e_tx,
                         "exit_trade": x_tx, "net": xf / ef - 1.0 - cost, "gross": xpx / eb.open - 1.0})
        # ---- the strategy: the period's legs as simulated (stored trades), purged or skipped
        legs = []
        for c in sorted(legs_at.get(R, []), key=lambda c: c["symbol"]):
            cid, t = c["candidate_id"], traded.get(c["candidate_id"])
            if t is not None:
                gross = float(t["exit_raw"]) / float(t["entry_raw"]) - 1.0
                net = float(t["trade_return"]) if t.get("trade_return") is not None else (
                    (float(t["exit_fill"]) - float(t["entry_fill"])) * float(t["qty"]) - float(t.get("fees") or 0.0)) / (float(t["entry_fill"]) * float(t["qty"]))
                legs.append({"candidate_id": cid, "symbol": c["symbol"], "status": "SIMULATED", "entry_date": t["entry_date"], "exit_date": t["exit_date"],
                             "entry_raw": float(t["entry_raw"]), "exit_raw": float(t["exit_raw"]), "entry_fill": float(t["entry_fill"]), "exit_fill": float(t["exit_fill"]),
                             "entry_trade": bool(c.get("entry_trade", True)), "exit_trade": bool(c.get("exit_trade", True)), "net": net, "gross": gross})
            else:
                legs.append({"candidate_id": cid, "symbol": c["symbol"], "status": "PURGED" if cid in purged else "SKIPPED", "reason": PURGE_REASON if cid in purged
                             else (skips.get(cid) or "NOT_SIMULATED")})
        sim = [x for x in legs if x["status"] == "SIMULATED"]
        exit_session = max([x["exit_date"] for x in ctrl] + [x["exit_date"] for x in sim] or [""]) or None
        if not members and not legs:
            status, reason = "EXCLUDED", "NO_ELIGIBLE_STOCK"
        elif any(x["status"] == "PURGED" for x in legs):
            status, reason = "EXCLUDED", "BOUNDARY"
        elif any(x["status"] == "SKIPPED" for x in legs):
            status, reason = "EXCLUDED", "INCOMPLETE_STRATEGY_PERIOD"
        elif not legs:
            status, reason = "EXCLUDED", "NO_STRATEGY_LEG"
        elif not ctrl:
            status, reason = "EXCLUDED", "NO_CONTROL_POSITION"
        elif missing:                                                            # symmetric with a skipped strategy leg: never a silent drop
            status, reason = "EXCLUDED", "CONTROL_MEMBER_NO_ENTRY_BAR"
        else:
            status, reason = "INCLUDED", ""
        bench = None
        if benchmark:
            w = _window(_after(dataset, benchmark, R), nxt)
            bench = None if w is None else w[2] / w[0].open - 1.0
        s_net = statistics.mean(x["net"] for x in sim) if sim else None
        c_net = statistics.mean(x["net"] for x in ctrl) if ctrl else None
        s_gross = statistics.mean(x["gross"] for x in sim) if sim else None
        c_gross = statistics.mean(x["gross"] for x in ctrl) if ctrl else None
        rows.append({"period": k, "rebalance": R, "next_rebalance": nxt, "entry_session": (_after_session(dataset, R)), "exit_session": exit_session,
                     "status": status, "reason": reason, "segment": ("TRAIN" if R <= train_end else "OOS") if status == "INCLUDED" else None,
                     "strategy": {"positions": len(sim), "legs": len(legs), "net": s_net, "gross": s_gross, "entries": sum(1 for x in sim if x["entry_trade"]),
                                  "exits": sum(1 for x in sim if x["exit_trade"]), "legs_detail": legs},
                     "control": {"positions": len(ctrl), "members": len(members), "net": c_net, "gross": c_gross, "entries": sum(1 for x in ctrl if x["entry_trade"]),
                                 "exits": sum(1 for x in ctrl if x["exit_trade"]), "missing_entry": missing, "positions_detail": ctrl},
                     "benchmark": {"symbol": benchmark, "return": bench},
                     "excess": (s_net - c_net) if (status == "INCLUDED") else None, "excess_gross": (s_gross - c_gross) if status == "INCLUDED" else None})
    return rows


def _after_session(dataset, d: str) -> Optional[str]:
    i = bisect_right(dataset.sessions, d)
    return dataset.sessions[i] if i < len(dataset.sessions) else None


# ---------------------------------------------------------------- parity audit (independent of the strategy) -----------------------------------
def parity_audit(rec: dict, rows: List[dict], snapshots: List[dict], candidates: List[dict], trades: List[dict], control: Optional[dict]) -> dict:
    problems: Dict[str, List[str]] = {c: [] for c in PARITY_CHECKS}
    p = problems
    fr = rec.get("friction_model") or {}
    slip = float(fr.get("slippage_bps_per_side") or 0.0) / 10000.0
    train_end = rec.get("train_end") or ""
    reb = [s["rebalance"] for s in snapshots]
    if reb != sorted(set(reb)) or len(rows) != len(snapshots) or [r["rebalance"] for r in rows] != reb:
        p["SAME_PERIODS"].append("the periods are not one per rebalance snapshot, in order")
    stray = sorted({c["signal_date"] for c in candidates} - set(reb))
    if stray:
        p["SAME_PERIODS"].append(f"{len(stray)} leg signal date(s) are not snapshot rebalances (first {stray[0]})")
    snap = {s["rebalance"]: s for s in snapshots}
    feat = (control or {}).get("membership_size_feature")                        # a candidate feature (inside the candidate set hash)
    if not feat:
        p["SAME_UNIVERSE_SNAPSHOT"].append("the control declares no membership_size_feature: its membership cannot be checked against the candidate set")
    cands_at: Dict[str, List[dict]] = {}
    for c in candidates:
        cands_at.setdefault(c["signal_date"], []).append(c)
    for r in rows:
        s = snap.get(r["rebalance"]) or {}
        sel, elig = set(s.get("selected") or []), set(s.get("eligible") or [])
        legs = {x["symbol"] for x in r["strategy"]["legs_detail"]}
        if legs != sel or not sel <= elig:
            p["SAME_UNIVERSE_SNAPSHOT"].append(f"{r['rebalance']}: legs {len(legs)} vs selection {len(sel)}; selection within the eligible set: {sel <= elig}")
        sizes = {(c.get("features") or {}).get(feat) for c in cands_at.get(r["rebalance"], [])} if feat else set()
        if feat and sizes and sizes != {len(elig)}:
            p["SAME_UNIVERSE_SNAPSHOT"].append(f"{r['rebalance']}: the control has {len(elig)} members, the candidate set records {sorted(sizes, key=str)} eligible")
        if r["status"] == "INCLUDED" and r["control"]["positions"] != len(elig):
            p["NO_MISSING_PERIOD"].append(f"{r['rebalance']}: INCLUDED with {len(elig) - r['control']['positions']} control member(s) dropped")
        cpos = {x["symbol"]: x for x in r["control"]["positions_detail"]}
        for x in r["strategy"]["legs_detail"]:
            if x["status"] != "SIMULATED":
                continue
            c = cpos.get(x["symbol"])
            if c is None or (c["entry_date"], c["exit_date"]) != (x["entry_date"], x["exit_date"]) or abs(c["entry_raw"] - x["entry_raw"]) > 1e-9 \
                    or abs(c["exit_raw"] - x["exit_raw"]) > 1e-9:
                p["SAME_PRICE_TIMESTAMPS"].append(f"{r['rebalance']} {x['symbol']}: the leg ({x['entry_date']} -> {x['exit_date']}) and the control position "
                                                  f"({None if c is None else (c['entry_date'], c['exit_date'])}) differ")
            want_e = x["entry_raw"] * (1 + slip) if x["entry_trade"] else x["entry_raw"]
            want_x = x["exit_raw"] * (1 - slip) if x["exit_trade"] else x["exit_raw"]
            if abs(x["entry_fill"] - want_e) > 1e-6 * max(1.0, want_e) or abs(x["exit_fill"] - want_x) > 1e-6 * max(1.0, want_x):
                p["SAME_FRICTION"].append(f"{r['rebalance']} {x['symbol']}: fills do not follow the friction model and the ENTER / EXIT flags")
        if r["status"] == "INCLUDED" and (r["strategy"]["net"] is None or r["control"]["net"] is None):
            p["NO_MISSING_PERIOD"].append(f"{r['rebalance']}: INCLUDED with one side missing")
        if r["status"] == "INCLUDED" and r["segment"] == "TRAIN" and (r["exit_session"] or "") > train_end:
            p["BOUNDARY_BOTH_SIDES"].append(f"{r['rebalance']}: a TRAIN period ending {r['exit_session']} crosses the TRAIN end {train_end}")
        crosses = r["rebalance"] <= train_end and any((x.get("exit_date") or "") > train_end for x in r["control"]["positions_detail"])
        if crosses and r["reason"] != "BOUNDARY" and r["status"] == "INCLUDED":
            p["BOUNDARY_BOTH_SIDES"].append(f"{r['rebalance']}: the control crosses the TRAIN end but the period is counted")
    sizing = (rec.get("config_snapshot") or {}).get("sizing") or {}
    if (control or {}).get("capital") != CAPITAL_CONVENTION or sizing.get("mode") != "EQUAL_WEIGHT":
        p["SAME_CAPITAL_CONVENTION"].append(f"control capital {(control or {}).get('capital')} / strategy sizing {sizing.get('mode')}: both must be {CAPITAL_CONVENTION} "
                                            "with equal-weight legs")
    if (control or {}).get("friction") != "RUN_FRICTION_MODEL":
        p["SAME_FRICTION"].append("the control does not declare the run's friction model")
    purged = set(((rec.get("boundary") or {}).get("purged") or {}).get("RULES") or [])
    in_boundary = {x["candidate_id"] for r in rows if r["reason"] == "BOUNDARY" for x in r["strategy"]["legs_detail"]}
    if not purged <= in_boundary or any(x["status"] == "PURGED" for r in rows if r["reason"] != "BOUNDARY" for x in r["strategy"]["legs_detail"]):
        p["BOUNDARY_BOTH_SIDES"].append(f"{len(purged - in_boundary)} purged leg(s) outside a boundary period, or a boundary period counted")
    seen: Dict[str, int] = {}
    for r in rows:
        for x in r["strategy"]["legs_detail"]:
            seen[x["candidate_id"]] = seen.get(x["candidate_id"], 0) + 1
    ids = [c["candidate_id"] for c in candidates]
    dup = [i for i, n in seen.items() if n != 1]
    rules_trades = {t["candidate_id"] for t in trades if t.get("arm", "RULES") == "RULES"}
    if set(seen) != set(ids) or dup or not rules_trades <= set(seen):
        p["PERIOD_ACCOUNTING"].append(f"{len(set(ids) - set(seen))} leg(s) in no period, {len(dup)} in several, {len(rules_trades - set(seen))} trade(s) unassigned")
    excl = [r for r in rows if r["status"] == "EXCLUDED" and not r["reason"]]
    if excl:
        p["NO_MISSING_PERIOD"].append(f"{len(excl)} period(s) excluded without a reason")
    checks = [{"check": c, "status": "FAIL" if p[c] else "PASS", "detail": "; ".join(p[c][:3]) + (f" (+{len(p[c]) - 3} more)" if len(p[c]) > 3 else "")} for c in PARITY_CHECKS]
    failed = [c["check"] for c in checks if c["status"] == "FAIL"]
    return {"status": "FAIL" if failed else "PASS", "checks": checks, "failed": failed,
            "note": f"control parity: {len(PARITY_CHECKS) - len(failed)} / {len(PARITY_CHECKS)} checks pass" + (f" (failed: {', '.join(failed)})" if failed else "")}


# ---------------------------------------------------------------- period statistics -----------------------------------------------------------
def _path(rets: List[float]) -> dict:
    w, peak, mdd = 1.0, 1.0, 0.0
    for x in rets:
        w *= 1.0 + x
        peak = max(peak, w)
        mdd = max(mdd, 1.0 - w / peak)
    return {"total_return": w - 1.0, "max_drawdown": mdd}


def concentration(ex: List[float]) -> dict:
    total = sum(ex)
    if len(ex) < 2 or total <= 0:
        return {"applicable": False, "concentrated": False, "text": "no positive summed excess: concentration does not apply"}
    best = max(ex)
    share = best / total
    loo = (total - best) / (len(ex) - 1)
    conc = share > CS_CONCENTRATION_SHARE or loo <= 0
    return {"applicable": True, "concentrated": conc, "top_period_share": share, "mean_excess_without_best_period": loo,
            "text": ("the best period carries more than the whole summed excess: without it the excess is not positive" if share > 1 else
                     f"one period carries {round(share * 100)} % of the summed excess" + (" and the excess vanishes without it" if loo <= 0 else "")) if conc
            else f"the best period carries {round(share * 100)} % of the summed excess"}


def periods_per_year(rows: List[dict]) -> float:
    ds = [date.fromisoformat(r["rebalance"]) for r in rows]
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    return 365.25 / statistics.median(gaps) if gaps else 12.0


def segment_stats(rows: List[dict], ppy: float) -> dict:
    n = len(rows)
    if not n:
        return {"period_count": 0}
    s = [r["strategy"]["net"] for r in rows]
    c = [r["control"]["net"] for r in rows]
    ex = [r["excess"] for r in rows]
    exg = [r["excess_gross"] for r in rows]
    b = [r["benchmark"]["return"] for r in rows if r["benchmark"]["return"] is not None]
    sp, cp = _path(s), _path(c)
    rel, peak, rdd = 1.0, 1.0, 0.0
    for a, z in zip(s, c):
        rel *= (1.0 + a) / (1.0 + z)
        peak = max(peak, rel)
        rdd = max(rdd, 1.0 - rel / peak)
    sd = statistics.stdev(ex) if n >= 2 else None
    t = (statistics.mean(ex) / (sd / math.sqrt(n))) if (n >= CS_MIN_PERIODS_T and sd and sd > 0) else None
    worst = min(rows, key=lambda r: r["excess"])
    turn = lambda side: statistics.mean(r[side]["entries"] / r[side]["positions"] for r in rows if r[side]["positions"])   # noqa: E731
    vol = lambda xs: _r(statistics.stdev(xs) * math.sqrt(ppy), 6) if len(xs) >= 2 else None   # noqa: E731
    return {"period_count": n, "from": rows[0]["rebalance"], "to": rows[-1]["rebalance"],
            "strategy_total_return": _r(sp["total_return"]), "control_total_return": _r(cp["total_return"]),
            "benchmark_total_return": _r(_path(b)["total_return"]) if len(b) == n else None,
            "mean_strategy_period_return": _r(statistics.mean(s)), "mean_control_period_return": _r(statistics.mean(c)),
            "mean_benchmark_period_return": _r(statistics.mean(b)) if b else None,
            "mean_excess_period_return": statistics.mean(ex), "mean_excess_period_return_gross": statistics.mean(exg),   # full precision: they decide
            "excess_stdev": _r(sd), "excess_t_stat": t, "excess_t_note": None if t is not None else (
                f"fewer than {CS_MIN_PERIODS_T} periods: no t-statistic" if n < CS_MIN_PERIODS_T else "no dispersion: no t-statistic"),
            "excess_win_rate": _r(sum(1 for x in ex if x > 0) / n, 4), "active_volatility": _r(sd * math.sqrt(ppy)) if sd is not None else None,
            "worst_relative_period": {"rebalance": worst["rebalance"], "excess": _r(worst["excess"])},
            "max_relative_drawdown": _r(rdd), "cumulative_excess_return": _r(sp["total_return"] - cp["total_return"]),
            "relative_return": _r((1.0 + sp["total_return"]) / (1.0 + cp["total_return"]) - 1.0),
            "strategy_volatility": vol(s), "control_volatility": vol(c), "strategy_max_drawdown": _r(sp["max_drawdown"]), "control_max_drawdown": _r(cp["max_drawdown"]),
            "strategy_turnover": _r(turn("strategy"), 4), "control_turnover": _r(turn("control"), 4),
            "turnover_difference": _r(turn("strategy") - turn("control"), 4), "concentration": concentration(ex)}


# ---------------------------------------------------------------- the selection edge and the overall verdict --------------------------------
def _edge(status: str, reasons: List[dict], **extra) -> dict:
    return {"status": status, "label": EDGE_LABELS[status], "reasons": reasons, "policy": POLICY, **extra}


def not_applicable() -> dict:
    return _edge("NOT_APPLICABLE", [{"code": "EVENT_DRIVEN", "text": "An event-driven strategy: judged per trade (validation_v2 policy); no selection control applies."}])


def selection_edge(segments: Dict[str, dict], parity: dict) -> dict:
    tr, oo, al = segments.get("TRAIN") or {}, segments.get("OOS") or {}, segments.get("ALL") or {}
    stat = {"oos_periods": oo.get("period_count", 0), "total_periods": al.get("period_count", 0), "oos_mean_excess": oo.get("mean_excess_period_return"),
            "oos_excess_t": oo.get("excess_t_stat"), "train_mean_excess": tr.get("mean_excess_period_return"), "train_excess_t": tr.get("excess_t_stat")}
    if parity.get("status") != "PASS":
        return _edge("INVALID", [{"code": "SELECTION_CONTROL_INVALID", "text": "The control-parity audit failed (" + ", ".join(parity.get("failed") or ["?"]) +
                                  "): no conclusion about the selection is drawn; the absolute research result is not affected."}], **stat)
    n_oos = oo.get("period_count", 0)
    if n_oos < CS_MIN_OOS_PERIODS:
        return _edge("INSUFFICIENT_DATA", [{"code": "TOO_FEW_OOS_PERIODS", "text": f"{n_oos} OOS rebalance periods; at least {CS_MIN_OOS_PERIODS} are required."}], **stat)
    m, mg, t = oo.get("mean_excess_period_return"), oo.get("mean_excess_period_return_gross"), oo.get("excess_t_stat")
    if m is None or m <= 0:
        code = "FRICTION_ERASES_EXCESS" if (mg or 0) > 0 else "NO_OOS_EXCESS"
        return _edge("NOT_ESTABLISHED", [{"code": code, "text": ("Friction turns the OOS excess over the control negative." if code == "FRICTION_ERASES_EXCESS" else
                                                                  f"The OOS mean excess over the control is {_pct(m)} per period: not positive.")}], **stat)
    if t is None or t < CS_T_NEGLIGIBLE:
        return _edge("NOT_ESTABLISHED", [{"code": "EXCESS_NEGLIGIBLE", "text": f"OOS mean excess {_pct(m)} per period with t = {_t(t)} over {n_oos} periods "
                                                                               f"(< {CS_T_NEGLIGIBLE}): statistically indistinguishable from zero."}], **stat)
    reasons = []
    if t < CS_T_ESTABLISHED:
        reasons.append({"code": "EXCESS_WEAK", "text": f"OOS period-excess t = {_t(t)} (< {CS_T_ESTABLISHED}): weak evidence."})
    if al.get("period_count", 0) < CS_MIN_TOTAL_PERIODS:
        reasons.append({"code": "TOO_FEW_PERIODS", "text": f"{al.get('period_count', 0)} periods in total; at least {CS_MIN_TOTAL_PERIODS} are required to establish an edge."})
    if (oo.get("concentration") or {}).get("concentrated"):
        reasons.append({"code": "EXCESS_CONCENTRATED", "text": "The OOS excess is concentrated: " + oo["concentration"]["text"] + "."})
    tm, tt = tr.get("mean_excess_period_return"), tr.get("excess_t_stat")
    if tm is not None and tm < 0 and tt is not None and tt <= CS_TRAIN_CONTRADICTION_T:
        reasons.append({"code": "TRAIN_CONTRADICTS", "text": f"TRAIN mean excess {_pct(tm)} with t = {_t(tt)}: TRAIN and OOS strongly contradict."})
    if reasons:
        return _edge("WEAK", reasons, **stat)
    return _edge("ESTABLISHED", [{"code": "EDGE_ESTABLISHED", "text": f"OOS mean excess {_pct(m)} per period, t = {_t(t)} over {n_oos} periods; not concentrated; "
                                                                      "consistent with TRAIN."}], **stat)


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:+.2f} %"


def _t(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.4f}"


OVERALL = {"WEAK": ("MIXED", "SELECTION_EDGE_WEAK", "The selection edge over the control is weak."),
           "NOT_ESTABLISHED": ("FAIL", "SELECTION_EDGE_NOT_ESTABLISHED", "No evidence that the selection beats its control."),
           "INSUFFICIENT_DATA": ("INSUFFICIENT_DATA", "SELECTION_EDGE_INSUFFICIENT_DATA", "Too few rebalance periods to assess the selection edge."),
           "INVALID": ("MIXED", "SELECTION_CONTROL_INVALID", "The selection edge could not be assessed: the control comparison is not fair.")}


def overall_verdict(performance: dict, edge_status: str) -> dict:
    """The overall verdict of a run: event-driven = the performance verdict; cross-sectional = PASS only with an ESTABLISHED selection edge."""
    pv = (performance or {}).get("verdict")
    if edge_status in ("NOT_APPLICABLE", "ESTABLISHED") or pv in (None, "INVALID", "INSUFFICIENT_DATA", "FAIL"):
        return {**performance, "basis": "PERFORMANCE" if edge_status == "NOT_APPLICABLE" else "PERFORMANCE_AND_SELECTION_EDGE"}
    verdict, code, text = OVERALL[edge_status]
    reasons = [{"code": code, "text": f"{text} Absolute performance: {performance.get('label', pv)} - not selection evidence on its own."}]
    out = V._out(verdict, reasons, list(performance.get("warnings") or []))
    return {**out, "basis": "PERFORMANCE_AND_SELECTION_EDGE"}


# ---------------------------------------------------------------- re-derivation (the gate and the store never trust a recorded statistic) -------
STRATEGY_SIDE_TOL = 1e-9


def verify(cs: dict, edge: dict, performance: dict, overall: dict, trades: Optional[List[dict]] = None, candidates: Optional[List[dict]] = None) -> List[str]:
    """Problems with a recorded cross-sectional block: the parity status must follow its own checks; the segment statistics, the selection edge and
    the overall verdict are recomputed from the recorded PERIOD TABLE; with the run's trades / candidates, every included period's strategy side is
    recomputed from the immutable trades and every control's membership size from the hash-pinned candidate feature. [] = consistent."""
    problems: List[str] = []
    par, rows, ctl = cs.get("parity") or {}, cs.get("periods") or [], cs.get("control") or {}
    checks = {c.get("check"): c.get("status") for c in par.get("checks") or []}
    if set(checks) != set(PARITY_CHECKS) or any(v not in ("PASS", "FAIL") for v in checks.values()):
        problems.append("the parity audit does not carry its eight checks")
    if par.get("status") != ("FAIL" if "FAIL" in checks.values() else "PASS") or sorted(par.get("failed") or []) != sorted(k for k, v in checks.items() if v == "FAIL"):
        problems.append("the parity status does not follow its own checks")
    try:
        inc = [r for r in rows if r["status"] == "INCLUDED"]
        for r in inc:
            if r["segment"] not in ("TRAIN", "OOS") or abs(float(r["excess"]) - (float(r["strategy"]["net"]) - float(r["control"]["net"]))) > 1e-12:
                problems.append(f"{r['rebalance']}: the period excess is not strategy - control")
                break
        ppy = periods_per_year(rows) if len(rows) > 1 else 12.0
        segs = {"TRAIN": segment_stats([r for r in inc if r["segment"] == "TRAIN"], ppy), "OOS": segment_stats([r for r in inc if r["segment"] == "OOS"], ppy),
                "ALL": segment_stats(inc, ppy)}
    except (KeyError, TypeError, ValueError, statistics.StatisticsError) as exc:
        return problems + [f"the period table cannot be re-derived ({type(exc).__name__})"]
    rec_segs = cs.get("segments") or {}
    for k, s in segs.items():
        r = rec_segs.get(k) or {}
        for f in ("period_count", "mean_excess_period_return", "mean_excess_period_return_gross", "excess_t_stat"):
            a, b = s.get(f), r.get(f)
            if (a is None) != (b is None) or (a is not None and abs(float(a) - float(b)) > 1e-9):
                problems.append(f"{k} {f}: recorded {b}, re-derived from the period table {a}")
        if bool((s.get("concentration") or {}).get("concentrated")) != bool((r.get("concentration") or {}).get("concentrated")):
            problems.append(f"{k}: the recorded concentration does not follow the period table")
    want = selection_edge(segs, {"status": "FAIL" if "FAIL" in checks.values() else "PASS", "failed": [k for k, v in checks.items() if v == "FAIL"]})
    want_overall = overall_verdict(performance or {}, want["status"]).get("verdict")
    if (edge or {}).get("status") != want["status"] or (overall or {}).get("verdict") != want_overall:
        problems.append(f"the recorded selection edge {(edge or {}).get('status')} / verdict {(overall or {}).get('verdict')} do not follow the policy "
                        f"({want['status']} / {want_overall})")
    if trades is not None:
        by: Dict[str, List[float]] = {}
        for t in trades:
            if t.get("arm", "RULES") == "RULES":
                by.setdefault(t["signal_date"], []).append(float(t["trade_return"]))
        for r in inc:
            got = by.get(r["rebalance"]) or []
            if len(got) != r["strategy"]["positions"] or abs(statistics.mean(got) - float(r["strategy"]["net"])) > STRATEGY_SIDE_TOL:
                problems.append(f"{r['rebalance']}: the strategy side does not match the run's stored trades")
                break
    if candidates is not None:
        feat = ctl.get("membership_size_feature")
        if not feat:
            problems.append("the control declares no membership_size_feature")
        else:
            sizes: Dict[str, set] = {}
            for c in candidates:
                sizes.setdefault(c["signal_date"], set()).add((c.get("features") or {}).get(feat))
            for r in rows:
                if r["rebalance"] in sizes and sizes[r["rebalance"]] != {r["control"]["members"]}:
                    problems.append(f"{r['rebalance']}: the control has {r['control']['members']} members; the candidate set records {sorted(sizes[r['rebalance']], key=str)}")
                    break
    return problems


# ---------------------------------------------------------------- the v3 block -----------------------------------------------------------------
def evaluate(rec: dict, trades: List[dict], candidates: List[dict], dataset, snapshots: List[dict], control: Optional[dict],
             benchmark: Optional[str] = "SPY") -> dict:
    rows = period_table(rec, trades, candidates, dataset, snapshots, benchmark)
    parity = parity_audit(rec, rows, snapshots, candidates, trades, control)
    inc = [r for r in rows if r["status"] == "INCLUDED"]
    ppy = periods_per_year(rows) if len(rows) > 1 else 12.0
    segs = {"TRAIN": segment_stats([r for r in inc if r["segment"] == "TRAIN"], ppy), "OOS": segment_stats([r for r in inc if r["segment"] == "OOS"], ppy),
            "ALL": segment_stats(inc, ppy)}
    edge = selection_edge(segs, parity)
    excluded: Dict[str, int] = {}
    for r in rows:
        if r["status"] == "EXCLUDED":
            excluded[r["reason"]] = excluded.get(r["reason"], 0) + 1
    table = [{k: v for k, v in r.items() if k not in ("strategy", "control")} | {
        "strategy": {k: v for k, v in r["strategy"].items() if k != "legs_detail"}, "control": {k: v for k, v in r["control"].items() if k != "positions_detail"}}
        for r in rows]
    return {"methodology_version": VERSION, "evaluation_mode": CROSS_SECTIONAL, "control": {**(control or {}), "role": CONTROL_ROLE},
            "market_benchmark": {"symbol": benchmark, "role": BENCHMARK_ROLE, "note": "a market comparison only - never selection-edge evidence"},
            "capital_convention": CAPITAL_CONVENTION, "periods_per_year": _r(ppy, 3), "periods": table, "excluded_periods": excluded,
            "segments": segs, "parity": parity, "selection_edge": edge, "leg_t_note": LEG_T_NOTE,
            "unit": "one observation per rebalance period (the legs of one rebalance are not independent)"}


def assess_stored_run(run: dict, candidates: List[dict], trades: List[dict], dataset, adapter, registry=None, created_at: Optional[str] = None,
                      git_sha: Optional[str] = None) -> dict:
    """A validation_v3 METHODOLOGY EVALUATION of a stored run (never a rewrite of it): the snapshots are re-derived from the same dataset and must
    reproduce the run's candidate set exactly; the period assessment uses the run's stored RULES trades."""
    from strategy_validation import candidates as CD
    if getattr(adapter, "evaluation_mode", EVENT_DRIVEN) != CROSS_SECTIONAL:
        raise EvaluationError(f"{run.get('strategy_id')} is not a cross-sectional portfolio strategy: there is no selection edge to assess")
    if run.get("validation_version") == VERSION:
        raise EvaluationError(f"run {run.get('run_id')} is a {VERSION} run: it carries its own selection-edge assessment")
    if dataset.dataset_id != run.get("dataset_id"):
        raise EvaluationError(f"the dataset {dataset.dataset_id} is not the run's dataset {run.get('dataset_id')}")
    if (adapter.strategy_id, adapter.strategy_version) != (run.get("strategy_id"), run.get("strategy_version")):
        raise EvaluationError("the adapter is not the run's strategy version")
    cands, _ = CD.normalize(adapter, adapter.candidates(dataset), dataset, registry)
    h = CD.candidate_set_hash([c.candidate_id for c in cands])
    if h != run.get("candidate_set_hash") or sorted(c.candidate_id for c in cands) != sorted(c["candidate_id"] for c in candidates):
        raise EvaluationError("the re-derived snapshots do not reproduce the run's candidate set: no honest methodology evaluation is possible")
    cs = evaluate(run, trades, candidates, dataset, adapter.selection_snapshots(dataset), adapter.control_definition, getattr(adapter, "market_benchmark", "SPY"))
    perf = dict(run.get("performance_verdict") or run.get("verdict") or {})       # a v3 run's `verdict` is already its overall verdict
    return {"evaluation_id": uuid.uuid4().hex, "run_id": run["run_id"], "strategy_id": run["strategy_id"], "strategy_version": run["strategy_version"],
            "methodology_version": VERSION, "evaluated_run_validation_version": run.get("validation_version"),
            "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_commit_sha": git_sha, "dataset_id": run["dataset_id"],
            "dataset_kind": run.get("dataset_kind"), "candidate_set_hash": h, "candidate_set_reproduced": True, "evaluation_mode": CROSS_SECTIONAL,
            "cross_sectional": cs, "selection_edge": cs["selection_edge"], "performance_verdict": perf,
            "verdict": overall_verdict(perf, cs["selection_edge"]["status"]),
            "note": (f"A {VERSION} methodology evaluation of run {run['run_id']} (recorded under {run.get('validation_version')}). The run record is unchanged; "
                     "its verdict is the absolute performance verdict of its own methodology.")}
