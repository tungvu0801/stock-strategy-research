"""
compute_demand(inputs) -> DemandResult. Deterministic; completed bars closed by `inputs.now` only; daily bars strictly before the
session date; mode INTRADAY_MODE when the current session has MIN_SESSION_BARS completed bars, else DAILY_MODE (B and C NOT_AVAILABLE,
coverage at the 0.60 floor). Weighted average over AVAILABLE components, renormalised; below the coverage floor the score is null.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from demand import (DISCLAIMER, KEY_FACTOR_MIN_DEVIATION, MAX_KEY_FACTORS, MIN_COVERAGE_WEIGHT, MIN_SESSION_BARS, SIMULATED_NOTE, WEIGHTS, demand_label)
from demand.components import COMPONENTS, Ctx
from demand.inputs import DemandInputs, completed
from demand.models import DemandComponent, DemandResult, DemandSources, SessionInfo
from demand.session import finite, session_range, vwap_series


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def build_ctx(inp: DemandInputs) -> Ctx:
    bars = completed(inp.session_bars, inp.now)
    daily = sorted((b for b in inp.daily_bars if b.date < inp.session_date), key=lambda b: b.date)
    mode = "INTRADAY_MODE" if len(bars) >= MIN_SESSION_BARS else "DAILY_MODE"
    price = inp.current_price if finite(inp.current_price) else (bars[-1].close if bars else None)
    vwaps = vwap_series(bars) if bars else []
    lo, hi = session_range(bars, price) if bars else (None, None)
    session_open = inp.session_open if finite(inp.session_open) else (bars[0].open if bars else None)
    return Ctx(mode=mode, bars=bars, vwaps=vwaps, price=price, session_open=session_open, low=lo, high=hi, daily=daily)


def compute_demand(inp: DemandInputs, *, weights: Optional[Dict[str, float]] = None, now_fn: Callable[[], float] = time.time) -> DemandResult:
    w = dict(weights or WEIGHTS)
    ctx = build_ctx(inp)
    comps: Dict[str, DemandComponent] = {}
    for fn in COMPONENTS:
        c = fn(inp, ctx)
        if c.key in w:
            c = c.model_copy(update={"weight": w[c.key]})
            comps[c.key] = c
    avail = [c for c in comps.values() if c.status == "AVAILABLE" and c.score is not None]
    coverage = round(sum(c.weight for c in avail), 4)
    score: Optional[int] = None
    if coverage + 1e-9 >= MIN_COVERAGE_WEIGHT and coverage > 0:
        score = int(round(sum(c.weight * c.score for c in avail) / coverage))
        score = 0 if score < 0 else 100 if score > 100 else score
    label = demand_label(score)
    ranked = sorted((c for c in avail if abs(c.score - 50.0) >= KEY_FACTOR_MIN_DEVIATION), key=lambda c: -abs(c.score - 50.0) * c.weight)
    key_factors = [c.reason for c in ranked[:MAX_KEY_FACTORS]]
    simulated = inp.mode == "simulated"
    if score is None:
        status, note = "INSUFFICIENT_DATA", f"only {int(round(coverage * 100))}% of the component weight is available (floor {int(MIN_COVERAGE_WEIGHT * 100)}%)"
        if simulated:
            note = SIMULATED_NOTE + " " + note
    else:
        status, note = ("SIMULATED_DATA", SIMULATED_NOTE) if simulated else ("AVAILABLE", None)
    bars_through = _iso(ctx.bars[-1].time + 60) if ctx.mode == "INTRADAY_MODE" and ctx.bars else (ctx.daily[-1].date.isoformat() if ctx.daily else None)
    session = SessionInfo(date=inp.session_date.isoformat(), complete=bool(inp.session_complete), bars_completed=len(ctx.bars),
                          open=round(ctx.session_open, 4) if finite(ctx.session_open) else None, high=round(ctx.high, 4) if finite(ctx.high) else None,
                          low=round(ctx.low, 4) if finite(ctx.low) else None, vwap=round(ctx.vwaps[-1], 4) if ctx.vwaps and ctx.vwaps[-1] is not None else None,
                          prior_sessions=len(inp.prior_sessions))
    b = inp.benchmark
    sources = DemandSources(mode=inp.mode, market_data=inp.market_data, price_source=inp.price_source, benchmark=b.symbol if b else None,
                            benchmark_kind=b.kind if b else "NONE", benchmark_label=b.label if b else None, bars_through=bars_through, session=session,
                            computed_at=_iso(now_fn()))
    return DemandResult(symbol=inp.symbol, as_of=_iso(inp.now), status=status, mode=ctx.mode, score=score, label=label, coverage_weight=coverage,
                        components=comps, key_factors=key_factors, sources=sources, weights=w, disclaimer=DISCLAIMER, note=note)
