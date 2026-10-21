"""
The six Market Demand components (DESIGN_57 §4). Each returns a DemandComponent with score in [0, 100] (50 = neutral) or
NOT_AVAILABLE with a code. Inputs are completed bars only (the engine builds the Ctx); nothing here looks at a future bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from demand import (COMPONENT_LABELS, DOWN_MOVE_RANGE_CAP, FOLLOW_WINDOW, MIN_DAILY_BARS, MIN_DAILY_FOLLOW, MIN_FOLLOW_BARS, MIN_SESSION_BARS,
                    RECLAIM_HOLD_BARS, RECLAIM_WINDOW, SCALES, STREAK_CAP_DAILY, STREAK_CAP_INTRADAY, STRUCTURE_WINDOW, VOLUME_LOOKBACK_SESSIONS, WEIGHTS)
from demand.inputs import BenchmarkInputs, DemandInputs, MinuteBar
from demand.models import DemandComponent
from demand.session import clip, expected_cumulative_volume, finite, median, monotone, streak, window_extremes
from ppe.inputs import DailyBar


@dataclass
class Ctx:
    mode: str                                    # INTRADAY_MODE | DAILY_MODE
    bars: List[MinuteBar] = field(default_factory=list)          # completed session bars
    vwaps: List[Optional[float]] = field(default_factory=list)    # cumulative VWAP per completed bar
    price: Optional[float] = None
    session_open: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    daily: List[DailyBar] = field(default_factory=list)          # completed sessions strictly before the session date


def _na(key: str, code: str, reason: str) -> DemandComponent:
    return DemandComponent(key=key, label=COMPONENT_LABELS[key], weight=WEIGHTS[key], status="NOT_AVAILABLE", code=code, reason=reason)


def _ok(key: str, score: float, raw: dict, reason: str) -> DemandComponent:
    return DemandComponent(key=key, label=COMPONENT_LABELS[key], weight=WEIGHTS[key], status="AVAILABLE", score=round(clip(score, 0.0, 100.0), 1),
                           raw=raw, reason=reason)


def _sign(x: Optional[float]) -> int:
    return 0 if not finite(x) or x == 0 else (1 if x > 0 else -1)


def _vwap_now(ctx: Ctx) -> Optional[float]:
    return ctx.vwaps[-1] if ctx.vwaps and ctx.vwaps[-1] is not None else None


# ---------------------------------------------------------------- A relative volume / participation -------------------------

def relative_volume(inp: DemandInputs, ctx: Ctx) -> DemandComponent:
    key = "relative_volume"
    if ctx.mode == "INTRADAY_MODE":
        m = len(ctx.bars)
        cum = sum(b.volume for b in ctx.bars)
        expected, used = expected_cumulative_volume(inp.prior_sessions, m)
        if expected is None:
            return _na(key, "NO_VOLUME_PROFILE", f"no intraday volume profile: {used} prior session(s) with {m} bars (need {2})")
        if expected <= 0 or cum <= 0:
            return _na(key, "ZERO_VOLUME", "no volume in the session bars")
        rv = cum / expected
        vwap = _vwap_now(ctx)
        ref, ref_name = (vwap, "VWAP") if vwap is not None else (ctx.session_open, "the open")
        d = _sign((ctx.price - ref) if finite(ctx.price) and finite(ref) else None)
        basis = f"{m} bars vs median of {used} prior sessions"
        raw = {"relative_volume": round(rv, 3), "cumulative_volume": cum, "expected_cumulative_volume": round(expected, 1), "bars": m, "prior_sessions": used,
               "direction": d, "reference": ref_name, "basis": "intraday cumulative volume at the same minute of prior sessions"}
    else:
        daily = ctx.daily
        if len(daily) < MIN_DAILY_BARS:
            return _na(key, "INSUFFICIENT_HISTORY", f"{len(daily)} completed sessions (need {MIN_DAILY_BARS})")
        last = daily[-1]
        prior = [b.volume for b in daily[-(VOLUME_LOOKBACK_SESSIONS + 1):-1] if finite(b.volume) and b.volume > 0]
        if not finite(last.volume) or last.volume <= 0 or len(prior) < VOLUME_LOOKBACK_SESSIONS * 3 // 4:
            return _na(key, "NO_VOLUME", "completed-day volume missing")
        rv = last.volume / median(prior)
        d = _sign(last.close - last.open)
        basis = "completed-day volume vs median of 20 prior sessions"
        ref_name = "the day's open"
        raw = {"relative_volume": round(rv, 3), "volume": last.volume, "median_prior_volume": round(median(prior), 1), "sessions": VOLUME_LOOKBACK_SESSIONS,
               "direction": d, "reference": ref_name, "basis": "completed-day volume"}
    p = clip((rv - 1.0) / SCALES["participation_span"], -1.0, 1.0)
    score = 50.0 + (30.0 * p * d if p > 0 else 20.0 * p)
    raw["participation"] = round(p, 3)
    where = "above" if d > 0 else "below" if d < 0 else "at"
    if p > 0:
        reason = f"relative volume {rv:.1f}x with price {where} {ref_name}" + ("" if ctx.mode == "INTRADAY_MODE" else " (completed day)")
    else:
        reason = f"thin participation: relative volume {rv:.1f}x" + ("" if ctx.mode == "INTRADAY_MODE" else " (completed day)")
    raw["basis_detail"] = basis
    return _ok(key, score, raw, reason)


# ---------------------------------------------------------------- B VWAP / price acceptance -----------------------------------

def vwap_acceptance(inp: DemandInputs, ctx: Ctx) -> DemandComponent:
    key = "vwap_acceptance"
    if ctx.mode != "INTRADAY_MODE":
        return _na(key, "INTRADAY_ONLY", "session VWAP needs intraday bars")
    bars, vw = ctx.bars, ctx.vwaps
    if len(bars) < MIN_SESSION_BARS:
        return _na(key, "INSUFFICIENT_BARS", f"{len(bars)} completed bars (need {MIN_SESSION_BARS})")
    if vw[-1] is None:
        return _na(key, "ZERO_VOLUME", "no volume in the session bars")
    if not finite(ctx.price):
        return _na(key, "NO_PRICE", "no current price")
    rng = (ctx.high - ctx.low) if finite(ctx.high) and finite(ctx.low) else 0.0
    dist = (ctx.price - vw[-1]) / rng if rng > 0 else 0.0
    above = [b.close >= v for b, v in zip(bars, vw) if v is not None]
    above_frac = sum(above) / len(above) if above else 0.5
    hold = streak(reversed(above), STREAK_CAP_INTRADAY)
    below = streak([not a for a in reversed(above)], STREAK_CAP_INTRADAY)
    reclaim = 0
    start = max(1, len(above) - RECLAIM_WINDOW)
    crosses = [i for i in range(start, len(above)) if (not above[i - 1]) and above[i]]
    if crosses:
        i = crosses[-1]
        after = above[i:i + RECLAIM_HOLD_BARS]
        if len(after) >= RECLAIM_HOLD_BARS and all(after):
            reclaim = 1
        elif not all(after):
            reclaim = -1
    score = 50.0 + 20.0 * clip(dist / SCALES["vwap_dist"], -1.0, 1.0) + 20.0 * (2.0 * above_frac - 1.0) + 10.0 * hold / STREAK_CAP_INTRADAY \
        - 10.0 * below / STREAK_CAP_INTRADAY + 5.0 * reclaim
    raw = {"vwap": round(vw[-1], 4), "distance_of_range": round(dist, 4), "above_fraction": round(above_frac, 3), "hold_bars": hold, "below_bars": below,
           "reclaim": reclaim, "bars": len(bars)}
    if hold >= RECLAIM_HOLD_BARS:
        reason = f"holding above VWAP ({hold}{'+' if hold >= STREAK_CAP_INTRADAY else ''} bars)" + (", reclaim held" if reclaim > 0 else "")
    elif below >= RECLAIM_HOLD_BARS:
        reason = f"below VWAP for {below}{'+' if below >= STREAK_CAP_INTRADAY else ''} bars" + (", reclaim rejected" if reclaim < 0 else "")
    elif hold > 0:
        reason = f"just crossed above VWAP ({hold} bar{'s' if hold != 1 else ''}, not yet held)"
    else:
        reason = "just slipped below VWAP"
    return _ok(key, score, raw, reason)


# ---------------------------------------------------------------- C intraday structure ---------------------------------------

def intraday_structure(inp: DemandInputs, ctx: Ctx) -> DemandComponent:
    key = "intraday_structure"
    if ctx.mode != "INTRADAY_MODE":
        return _na(key, "INTRADAY_ONLY", "intraday structure needs intraday bars")
    bars = ctx.bars
    need = 3 * STRUCTURE_WINDOW
    if len(bars) < need:
        return _na(key, "INSUFFICIENT_BARS", f"{len(bars)} completed bars (need {need})")
    w1, w2, w3 = bars[-need:-2 * STRUCTURE_WINDOW], bars[-2 * STRUCTURE_WINDOW:-STRUCTURE_WINDOW], bars[-STRUCTURE_WINDOW:]
    lows = [window_extremes(w)[0] for w in (w1, w2, w3)]
    highs = [window_extremes(w)[1] for w in (w1, w2, w3)]
    hl, hh = monotone(lows), monotone(highs)
    last = bars[-FOLLOW_WINDOW:]
    prev = bars[-FOLLOW_WINDOW - 1:-1]
    up_frac = sum(1 for a, b in zip(prev, last) if b.close > a.close) / float(FOLLOW_WINDOW)
    before = bars[:-STRUCTURE_WINDOW]
    support = min(b.low for b in before)
    reclaim = 0
    if any(b.low < support for b in w3):                                     # a break below the session's earlier support …
        reclaim = 1 if w3[-1].close > support else (-1 if w3[-1].close < support else 0)   # … reclaimed only by a close strictly back above it
    score = 50.0 + 15.0 * hl + 15.0 * hh + 10.0 * (2.0 * up_frac - 1.0) + 10.0 * reclaim
    raw = {"window_lows": [round(x, 4) for x in lows], "window_highs": [round(x, 4) for x in highs], "higher_lows": hl, "higher_highs": hh,
           "up_close_fraction": round(up_frac, 2), "session_support": round(support, 4), "breakdown_reclaim": reclaim, "bars": len(bars)}
    parts = []
    if hl > 0 and hh > 0:
        parts.append("higher lows and higher highs")
    elif hl > 0:
        parts.append("higher lows")
    elif hh > 0:
        parts.append("higher highs")
    if hl < 0 and hh < 0:
        parts.append("lower lows and lower highs")
    elif hl < 0:
        parts.append("lower lows")
    elif hh < 0:
        parts.append("lower highs")
    if reclaim > 0:
        parts.append("failed breakdown reclaimed")
    elif reclaim < 0:
        parts.append("breakdown held")
    parts.append(f"{int(round(up_frac * FOLLOW_WINDOW))} of last {FOLLOW_WINDOW} closes up")
    return _ok(key, score, raw, ", ".join(parts))


# ---------------------------------------------------------------- D range strength -------------------------------------------

def range_strength(inp: DemandInputs, ctx: Ctx) -> DemandComponent:
    key = "range_strength"
    if ctx.mode == "INTRADAY_MODE":
        if not finite(ctx.price) or not finite(ctx.low) or not finite(ctx.high):
            return _na(key, "NO_PRICE", "no current price or session range")
        lo, hi, px = ctx.low, ctx.high, ctx.price
        open_ = ctx.session_open if finite(ctx.session_open) else (ctx.bars[0].open if ctx.bars else None)
        complete = inp.session_complete
        basis = "session so far" if not complete else "completed session"
    else:
        if not ctx.daily:
            return _na(key, "INSUFFICIENT_HISTORY", "no completed session")
        d = ctx.daily[-1]
        lo, hi, px, open_, complete, basis = d.low, d.high, d.close, d.open, True, "last completed session"
    if hi - lo <= 0:
        return _na(key, "ZERO_RANGE", "zero range")
    loc = clip((px - lo) / (hi - lo), 0.0, 1.0)
    move = (px - open_) if finite(open_) else 0.0
    score = 100.0 * loc
    capped = False
    if move < 0 and score > DOWN_MOVE_RANGE_CAP:
        score, capped = DOWN_MOVE_RANGE_CAP, True
    raw = {"close_location": round(loc, 3), "low": round(lo, 4), "high": round(hi, 4), "price": round(px, 4), "move": round(move, 4), "session_complete": complete,
           "down_move_cap_applied": capped, "basis": basis}
    pct = int(round(loc * 100))
    where = "top" if loc >= 0.7 else "bottom" if loc <= 0.3 else "middle"
    reason = f"price in the {where} of the {basis} range ({pct}%)" + (" on a down move" if move < 0 else "")
    return _ok(key, score, raw, reason)


# ---------------------------------------------------------------- E relative strength vs sector -------------------------------

def _ret(bars: List[DailyBar], k: int) -> Optional[float]:
    if len(bars) <= k or bars[-1 - k].close <= 0:
        return None
    return bars[-1].close / bars[-1 - k].close - 1.0


def relative_strength(inp: DemandInputs, ctx: Ctx) -> DemandComponent:
    key = "relative_strength"
    b: Optional[BenchmarkInputs] = inp.benchmark
    if b is None:
        return _na(key, "NO_BENCHMARK", "no sector, peer or market benchmark available")
    bench_daily = [x for x in b.daily if x.date < inp.session_date]
    stock_daily = ctx.daily
    by_date = {x.date: x for x in bench_daily}
    aligned = [(s, by_date[s.date]) for s in stock_daily if s.date in by_date]
    horizons = []
    if ctx.mode == "INTRADAY_MODE":
        if finite(inp.current_price) and finite(inp.prev_close) and inp.prev_close > 0 and finite(b.session_return):
            horizons.append(("session", (inp.current_price / inp.prev_close - 1.0) - b.session_return, SCALES["rel_session_pct"]))
    else:
        if len(aligned) >= 2:
            s1, s0 = aligned[-1][0], aligned[-2][0]
            b1, b0 = aligned[-1][1], aligned[-2][1]
            if s0.close > 0 and b0.close > 0:
                horizons.append(("1d", (s1.close / s0.close - 1.0) - (b1.close / b0.close - 1.0), SCALES["rel_1d_pct"]))
    if len(aligned) >= 6:
        s5, b5 = aligned[-6][0], aligned[-6][1]
        if s5.close > 0 and b5.close > 0:
            horizons.append(("5d", (aligned[-1][0].close / s5.close - 1.0) - (aligned[-1][1].close / b5.close - 1.0), SCALES["rel_5d_pct"]))
    if not horizons:
        return _na(key, "BENCHMARK_NOT_ALIGNED", f"no aligned horizon with {b.label}")
    contrib = [clip(100.0 * rel / scale, -1.0, 1.0) for _, rel, scale in horizons]
    score = 50.0 + 50.0 * sum(0.5 * c for c in contrib) / (0.5 * len(contrib))
    raw = {"benchmark": b.symbol, "benchmark_kind": b.kind, "horizons": {h: round(100.0 * rel, 3) for h, rel, _ in horizons}}
    h, rel, _ = horizons[0]
    name = b.label.split(" (")[0]
    reason = (f"{'+' if rel >= 0 else ''}{100.0 * rel:.1f}% vs {name} ({h})") if abs(rel) * 100 >= 0.05 else f"in line with {name} ({h})"
    if rel < 0:
        reason = f"lagging {name} by {abs(100.0 * rel):.1f}% ({h})"
    return _ok(key, score, raw, reason)


# ---------------------------------------------------------------- F follow-through / persistence -------------------------------

def follow_through(inp: DemandInputs, ctx: Ctx) -> DemandComponent:
    key = "follow_through"
    if ctx.mode == "INTRADAY_MODE":
        bars, vw = ctx.bars, ctx.vwaps
        if len(bars) < MIN_FOLLOW_BARS:
            return _na(key, "INSUFFICIENT_BARS", f"{len(bars)} completed bars (need {MIN_FOLLOW_BARS})")
        refs = [v if v is not None else ctx.session_open for v in vw]
        side_flags = [b.close >= r if finite(r) else True for b, r in zip(bars, refs)]
        s = 1 if side_flags[-1] else -1
        run = streak(reversed(side_flags) if s > 0 else [not f for f in reversed(side_flags)], STREAK_CAP_INTRADAY)
        last, prev = bars[-FOLLOW_WINDOW:], bars[-2 * FOLLOW_WINDOW:-FOLLOW_WINDOW]
        lo_last, lo_prev = min(b.low for b in last), min(b.low for b in prev)
        hl_kept = 1 if lo_last > lo_prev else -1 if lo_last < lo_prev else 0
        pairs = list(zip(bars[-FOLLOW_WINDOW - 1:-1], last))
        ft_frac = sum(1 for a, b in pairs if b.close >= a.close) / float(len(pairs))
        cap, ref_name, unit = STREAK_CAP_INTRADAY, "VWAP" if vw[-1] is not None else "the open", "bar"
    else:
        daily = ctx.daily
        if len(daily) < MIN_DAILY_FOLLOW:
            return _na(key, "INSUFFICIENT_HISTORY", f"{len(daily)} completed sessions (need {MIN_DAILY_FOLLOW})")
        ups = [daily[i].close > daily[i - 1].close for i in range(1, len(daily))]
        s = 1 if ups[-1] else -1
        run = streak(reversed(ups) if s > 0 else [not u for u in reversed(ups)], STREAK_CAP_DAILY)
        lo_last, lo_prev = daily[-1].low, min(b.low for b in daily[-4:-1])
        hl_kept = 1 if lo_last > lo_prev else -1 if lo_last < lo_prev else 0
        last5 = [daily[i].close >= daily[i - 1].close for i in range(len(daily) - 5, len(daily))]
        ft_frac = sum(last5) / 5.0
        cap, ref_name, unit = STREAK_CAP_DAILY, "the previous close", "session"
    score = 50.0 + 20.0 * s * run / cap + 15.0 * hl_kept + 15.0 * (2.0 * ft_frac - 1.0)
    raw = {"side": s, "streak": run, "streak_cap": cap, "higher_low_kept": hl_kept, "follow_through_fraction": round(ft_frac, 2), "reference": ref_name}
    if run >= 3 and s > 0:
        reason = f"{run}{'+' if run >= cap else ''} consecutive {unit}s above {ref_name}" if ctx.mode == "INTRADAY_MODE" else f"{run}{'+' if run >= cap else ''} consecutive up sessions"
    elif run >= 3:
        reason = f"{run}{'+' if run >= cap else ''} consecutive {unit}s below {ref_name}" if ctx.mode == "INTRADAY_MODE" else f"{run}{'+' if run >= cap else ''} consecutive down sessions"
    elif s > 0:
        reason = f"one-{unit} bounce, not yet followed through" if run <= 1 else f"{run} {unit}s above {ref_name}"
    else:
        reason = f"{run} {unit}{'s' if run != 1 else ''} below {ref_name}"
    if hl_kept > 0:
        reason += ", higher low maintained"
    elif hl_kept < 0:
        reason += ", lower low"
    return _ok(key, score, raw, reason)


COMPONENTS = (relative_volume, vwap_acceptance, intraday_structure, range_strength, relative_strength, follow_through)
