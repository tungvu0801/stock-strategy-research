"""strategy_validation/diagnostics.py — TRAIN vs OOS diagnosis, period concentration (year / quarter / month; no market-regime labels: no deterministic
regime data feeds this stage), ambiguous-bar dependence, friction weight and the parameter-sensitivity label. Diagnostics describe; they never change a
strategy, a parameter, a trade or the candidate set."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from strategy_validation import (AMBIGUOUS_DEPENDENCE, AMBIGUOUS_SHARE_WARN, CONCENTRATION_SEVERE, CONCENTRATION_WARN, DEGRADATION_RATIO, FRICTION_HEAVY, NEAR_ZERO_R,
                                 SENSITIVITY_SPREAD)
from strategy_validation.models import SimulatedTrade


def train_vs_oos(train: dict, oos: dict, nz: float = NEAR_ZERO_R) -> dict:
    """`nz`: the |mean R| band read as "near zero" - in RISK units (NEAR_ZERO_R) for stop-sized trades; 0 for equal-weight legs, whose R is
    the return on the position notional (Stage 6.4: a 0.05 band would call any monthly return below 5 % "near zero")."""
    t, o = train.get("expectancy_R"), oos.get("expectancy_R")
    if not train.get("trade_count") or not oos.get("trade_count") or t is None or o is None:
        return {"code": "NO_COMPARISON", "text": "TRAIN or OOS has no trade: no comparison is possible.", "train_R": t, "oos_R": o}
    if t > nz and o > nz:
        if o >= DEGRADATION_RATIO * t:
            return {"code": "CONSISTENT", "text": "TRAIN positive, OOS positive: consistent.", "train_R": t, "oos_R": o}
        return {"code": "DEGRADATION", "text": f"TRAIN positive, OOS positive but below {int(DEGRADATION_RATIO * 100)} % of TRAIN: degradation.", "train_R": t, "oos_R": o}
    if t > nz and abs(o) <= nz:
        return {"code": "DEGRADATION", "text": "TRAIN positive, OOS near zero: degradation.", "train_R": t, "oos_R": o}
    if t > nz and o < -nz:
        return {"code": "FAILURE_INSTABILITY", "text": "TRAIN positive, OOS negative: likely failure / instability.", "train_R": t, "oos_R": o}
    if t <= nz and o > nz:
        return {"code": "TRAIN_WEAK_OOS_POSITIVE", "text": "TRAIN not positive, OOS positive: not automatically good; the result may depend on the period.", "train_R": t, "oos_R": o}
    return {"code": "CONSISTENTLY_WEAK", "text": "TRAIN and OOS not clearly positive: no edge demonstrated.", "train_R": t, "oos_R": o}


def _period(date: str, kind: str) -> str:
    y, m = date[:4], int(date[5:7])
    return y if kind == "year" else f"{y}-Q{(m - 1) // 3 + 1}" if kind == "quarter" else date[:7]


def concentration(trades: List[SimulatedTrade]) -> dict:
    total = sum(t.net_pnl for t in trades)
    out: Dict[str, object] = {"total_net_pnl": round(total, 6), "levels": {}, "severe": False, "warning": False, "text": ""}
    kinds = ["year", "quarter"] + (["month"] if len(trades) >= 60 else [])
    for kind in kinds:
        per: Dict[str, float] = defaultdict(float)
        for t in trades:
            per[_period(t.exit_date, kind)] += t.net_pnl
        top = max(per.items(), key=lambda kv: kv[1]) if per else (None, 0.0)
        share = (top[1] / total) if total > 0 and len(per) >= 2 else None
        out["levels"][kind] = {"periods": {k: round(v, 6) for k, v in sorted(per.items())}, "top_period": top[0], "top_share": None if share is None else round(share, 4), "count": len(per)}
        if share is not None and share >= CONCENTRATION_SEVERE and kind in ("year", "quarter"):
            out["severe"] = True
            out["text"] = f"{round(share * 100)} % of total net P/L comes from {top[0]}: severe period concentration."
        elif share is not None and share >= CONCENTRATION_WARN and not out["text"]:
            out["warning"] = True
            out["text"] = f"{round(share * 100)} % of total net P/L comes from {top[0]}."
    if total <= 0:
        out["text"] = "Total net P/L is not positive: concentration is not applicable."
    return out


def ambiguous(trades: List[SimulatedTrade]) -> dict:
    amb = [t for t in trades if t.ambiguous_bar]
    total = sum(t.net_pnl for t in trades)
    alt_total = total + sum((t.alt_target_first_net or 0.0) - t.net_pnl for t in amb)
    share = len(amb) / len(trades) if trades else 0.0
    move = abs(alt_total - total) / abs(total) if total else (1.0 if amb else 0.0)
    dependent = bool(amb) and (move > AMBIGUOUS_DEPENDENCE or (total <= 0 < alt_total))
    return {"policy": "STOP_FIRST", "ambiguous_bars_count": len(amb), "share": round(share, 4), "net_pnl_stop_first": round(total, 6), "net_pnl_if_target_first": round(alt_total, 6),
            "warning": share > AMBIGUOUS_SHARE_WARN, "dependent": dependent,
            "text": ("The result depends materially on ambiguous bars (target-first would move net P/L by "
                     f"{round(move * 100)} %)." if dependent else f"{len(amb)} ambiguous bar(s) resolved stop-first.")}


def friction(m: dict) -> dict:
    gross, fr = m.get("gross_pnl") or 0.0, m.get("friction_total") or 0.0
    share = (fr / gross) if gross > 0 else None
    killed = gross > 0 and (m.get("net_pnl") or 0.0) <= 0
    return {"gross_pnl": m.get("gross_pnl"), "net_pnl": m.get("net_pnl"), "friction_total": m.get("friction_total"), "friction_share_of_gross": None if share is None else round(share, 4),
            "heavy": share is not None and share > FRICTION_HEAVY, "killed": killed}


def sensitivity(base_R: Optional[float], neighbours: List[dict]) -> dict:
    """neighbours: [{"label", "params", "expectancy_R", "trade_count"}] - explicitly supplied configurations, diagnostic only (the base is never replaced)."""
    if not neighbours:
        return {"label": "NOT_RUN", "text": "No neighbouring configuration was supplied.", "neighbours": []}
    vals = [n.get("expectancy_R") for n in neighbours]
    if base_R is None or any(v is None for v in vals):
        return {"label": "UNSTABLE", "text": "A configuration produced no trade: the result is unstable.", "neighbours": neighbours}
    if any((v > 0) != (base_R > 0) for v in vals):
        return {"label": "UNSTABLE", "text": "A neighbouring configuration flips the sign of the expectancy.", "neighbours": neighbours}
    spread = max(abs(v - base_R) for v in vals) / abs(base_R) if base_R else 1.0
    if spread > SENSITIVITY_SPREAD:
        return {"label": "SENSITIVE", "text": f"Neighbouring configurations move the expectancy by up to {round(spread * 100)} %.", "neighbours": neighbours}
    return {"label": "ROBUST", "text": f"Neighbouring configurations stay within {round(spread * 100)} % of the base expectancy.", "neighbours": neighbours}
