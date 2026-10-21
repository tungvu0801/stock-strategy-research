"""
PPE Stretch (DESIGN_56 §4): six deterministic components on completed daily bars, each mapped to [-100, 100], combined as a
weighted average over the AVAILABLE components with renormalisation and a minimum-coverage rule.

Baseline convention: every baseline uses the sessions strictly before the evaluated session. Replay (current_price is None):
the evaluated session is the last bar <= as_of. Live (current_price given): all bars are completed sessions and the evaluated
session is the one in progress.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional

from ppe import (ATR_PERIOD, COMPONENT_LABELS, EMA_SPAN, FULL_SCALE, MIN_BARS_RSI, MIN_BARS_TREND, MIN_COVERAGE_WEIGHT, MIN_RETURN_OBS,
                 RANGE_LOOKBACK, RETURN_LOOKBACK, RSI_PERIOD, VOLUME_LOOKBACK, WEIGHTS, Z_CAP, stretch_label)
from ppe.features import atr_last, bounded, clip, ema_last, finite, robust_z, rsi_last, simple_returns
from ppe.inputs import DailyBar, MarketInputs, point_in_time
from ppe.models import ComponentResult, StretchResult


@dataclass
class Frame:
    symbol: str
    prior: List[DailyBar]                 # completed sessions strictly before the evaluated session
    price: float                          # the evaluated price (close of bar T, or the live price)
    open: Optional[float]                 # the evaluated session's open when known
    session: date
    live: bool
    volume_session: Optional[DailyBar]    # the completed session whose volume is compared
    volume_window: List[DailyBar]         # the 20 completed sessions before volume_session
    bench_prior: Optional[List[DailyBar]]
    bench_price: Optional[float]
    bench_kind: str
    bench_label: str


def build_frame(inputs: MarketInputs) -> Optional[Frame]:
    bars = point_in_time(inputs.bars, inputs.as_of)
    if not bars:
        return None
    if inputs.current_price is None:
        if len(bars) < 2:
            return None
        cur, prior = bars[-1], bars[:-1]
        price, open_, session, live = cur.close, cur.open, cur.date, False
        vol_session, vol_window = cur, prior[-VOLUME_LOOKBACK:]
    else:
        prior = bars
        price, open_, session, live = float(inputs.current_price), inputs.current_open, inputs.as_of, True
        vol_session, vol_window = prior[-1], prior[-(VOLUME_LOOKBACK + 1):-1]
    bench_prior = bench_price = None
    kind, label = "NONE", ""
    b = inputs.benchmark
    if b is not None and b.bars:
        bb = point_in_time(b.bars, inputs.as_of)
        kind, label = b.kind, b.label or b.symbol
        if not live:
            by = {x.date: x for x in bb}
            if session in by:
                bench_price, bench_prior = by[session].close, [x for x in bb if x.date < session]
        else:
            bench_prior, bench_price = bb, b.current_price
    return Frame(inputs.symbol, prior, price, open_, session, live, vol_session, vol_window, bench_prior, bench_price, kind, label)


def _na(key: str, reason: str, code: str) -> ComponentResult:
    return ComponentResult(key=key, label=COMPONENT_LABELS[key], weight=WEIGHTS[key], status="NOT_AVAILABLE", reason=reason, code=code)


def _ok(key: str, score: float, raw: dict, reason: str) -> ComponentResult:
    return ComponentResult(key=key, label=COMPONENT_LABELS[key], weight=WEIGHTS[key], status="AVAILABLE", score=round(clip(score, -100.0, 100.0), 3),
                           raw=raw, reason=reason)


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return round(float(x), n) if finite(x) else None


# ---------------------------------------------------------------- A ------------------------------------------------------------
def price_dislocation(f: Frame) -> ComponentResult:
    key = "price_dislocation"
    if len(f.prior) < MIN_BARS_TREND:
        return _na(key, f"needs {MIN_BARS_TREND} completed sessions, have {len(f.prior)}", "INSUFFICIENT_HISTORY")
    e = ema_last([b.close for b in f.prior], EMA_SPAN)
    a = atr_last(f.prior, ATR_PERIOD)
    if not finite(e) or not finite(a) or a <= 0:
        return _na(key, "ATR20 is zero or undefined", "ZERO_ATR")
    raw = (f.price - e) / a
    return _ok(key, bounded(raw, FULL_SCALE["dislocation_atr"]), {"price": _r(f.price), "ema20": _r(e), "atr20": _r(a), "atr_distance": _r(raw, 3)},
               f"{abs(raw):.1f} ATR {'below' if raw < 0 else 'above'} EMA20")


# ---------------------------------------------------------------- B ------------------------------------------------------------
def abnormal_return(f: Frame) -> ComponentResult:
    key = "abnormal_return"
    closes = [b.close for b in f.prior]
    if len(closes) < MIN_RETURN_OBS + 5:
        return _na(key, f"needs {MIN_RETURN_OBS + 5} completed sessions, have {len(closes)}", "INSUFFICIENT_HISTORY")
    hist = closes[-(RETURN_LOOKBACK + 5):]
    r1h, r5h = simple_returns(hist, 1), simple_returns(hist, 5)
    if closes[-1] <= 0 or closes[-5] <= 0:
        return _na(key, "non-positive close in history", "BAD_PRICE")
    r1, r5 = f.price / closes[-1] - 1.0, f.price / closes[-5] - 1.0
    z1, m1 = robust_z(r1, r1h)
    z5, m5 = robust_z(r5, r5h)
    if z1 is None or z5 is None:
        return _na(key, "the stock's return history has no dispersion", "ZERO_VARIANCE")
    z1c, z5c = clip(z1, -Z_CAP, Z_CAP), clip(z5, -Z_CAP, Z_CAP)
    combined = 0.5 * z1c + 0.5 * z5c
    horizon, z = ("5d", z5c) if abs(z5c) >= abs(z1c) else ("1d", z1c)
    reason = (f"{horizon} return unusually {'negative' if z < 0 else 'positive'} (robust z {z:+.1f})" if abs(combined) >= 1.0
              else f"recent returns within the normal range (robust z {combined:+.1f})")
    return _ok(key, bounded(combined, FULL_SCALE["z"]), {"r1": _r(r1, 5), "r5": _r(r5, 5), "z1": _r(z1, 3), "z5": _r(z5, 3), "combined_z": _r(combined, 3),
                                                        "observations_1d": len(r1h), "observations_5d": len(r5h), "method": m1 if m1 == m5 else f"{m1}/{m5}",
                                                        "lookback_sessions": RETURN_LOOKBACK}, reason)


# ---------------------------------------------------------------- C ------------------------------------------------------------
def relative_sector_move(f: Frame) -> ComponentResult:
    key = "relative_sector_move"
    if f.bench_kind == "NONE" or f.bench_prior is None or f.bench_price is None:
        return _na(key, "no deterministic sector or market benchmark available for this session", "NO_BENCHMARK")
    sb = {b.date: b.close for b in f.prior}
    bb = {b.date: b.close for b in f.bench_prior}
    common = sorted(set(sb) & set(bb))
    if len(common) < MIN_RETURN_OBS + 5:
        return _na(key, f"needs {MIN_RETURN_OBS + 5} aligned sessions with {f.bench_label}, have {len(common)}", "INSUFFICIENT_HISTORY")
    if not f.prior or common[-1] != f.prior[-1].date:
        return _na(key, f"{f.bench_label} does not cover the latest completed session", "BENCHMARK_NOT_ALIGNED")
    common = common[-(RETURN_LOOKBACK + 5):]
    s, b = [sb[d] for d in common], [bb[d] for d in common]
    if min(s[-5], s[-1], b[-5], b[-1]) <= 0:
        return _na(key, "non-positive close in history", "BAD_PRICE")
    rel1h = [x - y for x, y in zip(simple_returns(s, 1), simple_returns(b, 1))]
    rel5h = [x - y for x, y in zip(simple_returns(s, 5), simple_returns(b, 5))]
    rel1 = (f.price / s[-1] - 1.0) - (f.bench_price / b[-1] - 1.0)
    rel5 = (f.price / s[-5] - 1.0) - (f.bench_price / b[-5] - 1.0)
    z1, m1 = robust_z(rel1, rel1h)
    z5, m5 = robust_z(rel5, rel5h)
    if z1 is None or z5 is None:
        return _na(key, "relative-return history has no dispersion", "ZERO_VARIANCE")
    z1c, z5c = clip(z1, -Z_CAP, Z_CAP), clip(z5, -Z_CAP, Z_CAP)
    combined = 0.5 * z1c + 0.5 * z5c
    short = f.bench_label.split(" (")[0] if f.bench_kind != "PEER_BASKET" else "peer basket"
    reason = f"{rel5 * 100:+.1f}% vs {short} over 5d (robust z {z5c:+.1f})" + (" — market fallback" if f.bench_kind == "MARKET" else "")
    return _ok(key, bounded(combined, FULL_SCALE["z"]), {"rel1": _r(rel1, 5), "rel5": _r(rel5, 5), "z1": _r(z1, 3), "z5": _r(z5, 3), "combined_z": _r(combined, 3),
                                                        "observations": len(rel1h), "benchmark": f.bench_label, "benchmark_kind": f.bench_kind,
                                                        "method": m1 if m1 == m5 else f"{m1}/{m5}"}, reason)


# ---------------------------------------------------------------- D ------------------------------------------------------------
def volume_shock(f: Frame) -> ComponentResult:
    key = "volume_shock"
    vs, win = f.volume_session, f.volume_window
    if vs is None or vs.volume is None:
        return _na(key, "no volume for the evaluated session", "NO_VOLUME")
    if len(win) < VOLUME_LOOKBACK or any(b.volume is None for b in win):
        return _na(key, f"needs volume for the {VOLUME_LOOKBACK} sessions before the evaluated one", "INSUFFICIENT_HISTORY")
    normal = statistics.median([b.volume for b in win])
    if normal <= 0:
        return _na(key, "the 20-session median volume is zero", "ZERO_VOLUME")
    ratio = vs.volume / normal
    prev_close = f.prior[-1].close if not f.live else (f.prior[-2].close if len(f.prior) >= 2 else None)
    if prev_close is None or prev_close <= 0:
        return _na(key, "no prior close for the volume session", "INSUFFICIENT_HISTORY")
    r = vs.close / prev_close - 1.0
    direction = 0 if abs(r) < 0.001 else (1 if r > 0 else -1)
    m = clip((ratio - 1.0) / FULL_SCALE["volume_ratio_span"], 0.0, 1.0)
    kind = "an up" if direction > 0 else "a down" if direction < 0 else "a flat"
    reason = f"volume {ratio:.1f}× its 20-session median on {kind} session" + (" (last completed session)" if f.live else "")
    return _ok(key, direction * m * 100.0, {"session": vs.date.isoformat(), "volume": _r(vs.volume, 0), "normal_20": _r(normal, 0), "ratio": _r(ratio, 3),
                                           "session_return": _r(r, 5), "direction": direction, "live_partial_day_excluded": f.live}, reason)


# ---------------------------------------------------------------- E ------------------------------------------------------------
def gap_extension(f: Frame) -> ComponentResult:
    key = "gap_extension"
    if len(f.prior) < MIN_BARS_TREND:
        return _na(key, f"needs {MIN_BARS_TREND} completed sessions, have {len(f.prior)}", "INSUFFICIENT_HISTORY")
    a = atr_last(f.prior, ATR_PERIOD)
    if not finite(a) or a <= 0:
        return _na(key, "ATR20 is zero or undefined", "ZERO_ATR")
    prev_close = f.prior[-1].close
    ext = (f.price - prev_close) / a
    gap = (f.open - prev_close) / a if finite(f.open) else None
    raw = 0.5 * gap + 0.5 * ext if gap is not None else ext
    reason = f"{ext:+.1f} ATR from the prior close" + (f", gap {gap:+.1f} ATR" if gap is not None else " (open unavailable: extension only)")
    return _ok(key, bounded(raw, FULL_SCALE["gap_atr"]), {"extension_atr": _r(ext, 3), "gap_atr": _r(gap, 3), "atr20": _r(a), "prior_close": _r(prev_close),
                                                         "open": _r(f.open)}, reason)


# ---------------------------------------------------------------- F ------------------------------------------------------------
def short_term_exhaustion(f: Frame) -> ComponentResult:
    key = "short_term_exhaustion"
    closes = [b.close for b in f.prior]
    if len(closes) < MIN_BARS_RSI:
        return _na(key, f"needs {MIN_BARS_RSI} completed sessions, have {len(closes)}", "INSUFFICIENT_HISTORY")
    series = closes + [f.price]
    rsi = rsi_last(series, RSI_PERIOD)
    if not finite(rsi):
        return _na(key, "RSI14 undefined", "INSUFFICIENT_HISTORY")
    rsi_part = clip((rsi - 50.0) / FULL_SCALE["rsi_span"], -1.0, 1.0)
    n, sign = 0, 0
    for d in reversed([series[i] - series[i - 1] for i in range(1, len(series))]):
        s_ = 1 if d > 0 else -1 if d < 0 else 0
        if n == 0:
            sign = s_
        if s_ == 0 or s_ != sign:
            break
        n += 1
    streak_part = sign * clip(n / FULL_SCALE["streak_sessions"], 0.0, 1.0)
    window = f.prior[-RANGE_LOOKBACK:]
    hi, lo = max(max(b.high for b in window), f.price), min(min(b.low for b in window), f.price)
    range_part = ((f.price - lo) / (hi - lo) - 0.5) * 2.0 if hi > lo else 0.0
    raw = 0.5 * rsi_part + 0.25 * streak_part + 0.25 * range_part
    reason = f"RSI14 {rsi:.0f}" + (f"; {n} consecutive {'up' if sign > 0 else 'down'} sessions" if n >= 3 else "")
    return _ok(key, clip(raw, -1.0, 1.0) * 100.0, {"rsi14": _r(rsi, 2), "streak": n * sign, "range_position": _r((f.price - lo) / (hi - lo) if hi > lo else 0.5, 3),
                                                  "rsi_part": _r(rsi_part, 3), "streak_part": _r(streak_part, 3), "range_part": _r(range_part, 3)}, reason)


COMPONENTS: Dict[str, Callable[[Frame], ComponentResult]] = {
    "price_dislocation": price_dislocation, "abnormal_return": abnormal_return, "relative_sector_move": relative_sector_move,
    "volume_shock": volume_shock, "gap_extension": gap_extension, "short_term_exhaustion": short_term_exhaustion}


def compute_stretch(inputs: MarketInputs, weights: Optional[Dict[str, float]] = None) -> StretchResult:
    weights = dict(weights or WEIGHTS)
    f = build_frame(inputs)
    if f is None:
        return StretchResult(status="NOT_AVAILABLE", note="no completed daily bars at or before as_of")
    comps = {k: fn(f) for k, fn in COMPONENTS.items()}
    avail = {k: c for k, c in comps.items() if c.status == "AVAILABLE" and c.score is not None}
    coverage = round(sum(weights[k] for k in avail), 4)
    if coverage < MIN_COVERAGE_WEIGHT:
        return StretchResult(status="INSUFFICIENT_DATA", coverage_weight=coverage, components=comps,
                             note=f"only {coverage:.0%} of the configured weight is available (minimum {MIN_COVERAGE_WEIGHT:.0%})")
    raw = clip(sum(weights[k] * c.score for k, c in avail.items()) / coverage, -100.0, 100.0)
    score = round(raw, 1)
    ranked = sorted(((abs(weights[k] * c.score) / coverage, k) for k, c in avail.items()), reverse=True)
    key_factors = [comps[k].reason for _, k in ranked[:3] if abs(comps[k].score) >= 10.0]
    direction = "DOWN" if raw < -5.0 else "UP" if raw > 5.0 else "FLAT"
    return StretchResult(status="AVAILABLE", score=score, raw_score=round(raw, 6), label=stretch_label(score), coverage_weight=coverage,
                         direction=direction, components=comps, key_factors=key_factors)
