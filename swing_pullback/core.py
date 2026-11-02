"""swing_pullback/core.py — SwingPullbackCore: the ONE implementation of the V1 / C1 rules. The adapters call it; the tests re-derive the rules
separately on purpose.

For every universe symbol (one listing per issuer) and every session t of that symbol, in order, the FIRST failing rule names the outcome:

  SECOND_LISTING           another universe symbol of the same issuer sorts first (GOOG is kept, GOOGL is not)
  WARMUP                   t is before the dataset's 200th session
  MISSING_WINDOW_BAR       the symbol lacks a bar on one of the 200 dataset sessions ending at t (never filled, never the nearest other bar)
  REGIME_WINDOW_INCOMPLETE SPY lacks a bar on one of them
  NO_RAW_PRICE             no positive raw close at t
  PRICE_BELOW_MIN          raw close < $5
  LIQUIDITY_HISTORY        (SIP volume only) a raw close or volume missing on one of the 20 sessions ending at t
  LIQUIDITY_BELOW_MIN      (SIP volume only) median raw close x raw volume of those 20 sessions < $20 M
  REGIME_OFF               SPY close <= SMA200(SPY)
  BELOW_SLOW_SMA           close <= SMA200
  FAST_SMA_NOT_RISING      SMA50 <= SMA50 of 20 sessions earlier
  NO_PULLBACK_TRIGGER      (V1) Wilder RSI(2) > the threshold and not three successive lower closes
  BELOW_DEPTH_GUARD        (V1) close < 0.97 x SMA50
  COOLDOWN                 the symbol had a candidate within the previous 9 dataset sessions
  CANDIDATE                every rule holds: stop = close - 2 x ATR14, target = close + 3 x ATR14, both fixed here

Everything at t reads bars <= t (the indicators are causal; the windows end at t); the cooldown reads the signal dates of earlier candidates only.
The three strict comparisons with a mean (close > SMA200, SPY close > SMA200, SMA50 > SMA50 20 sessions earlier) are decided EXACTLY: the sign
of math.fsum (correctly rounded, so its sign is the exact sign) of the window sums' difference - a tie is never a pass.
Pure and deterministic: no clock, no randomness, no network, no model."""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional

from swing_pullback import (ATR_PERIOD, COOLDOWN_SESSIONS, DEPTH_GUARD, DOLLAR_VOLUME_SESSIONS, FAST_SMA, LOWER_CLOSES, MIN_DOLLAR_VOLUME, MIN_PRICE,
                            REGIME_SYMBOL, RESEARCH_UNIVERSE, RSI_MAX, RSI_PERIOD, SLOPE_LOOKBACK, SLOW_SMA, STOP_ATR, TARGET_ATR, WARMUP_SESSIONS)
from swing_pullback.data import SwingData
from swing_pullback.indicators import sma, wilder_atr, wilder_rsi

REASONS = ("SECOND_LISTING", "WARMUP", "MISSING_WINDOW_BAR", "REGIME_WINDOW_INCOMPLETE", "NO_RAW_PRICE", "PRICE_BELOW_MIN", "LIQUIDITY_HISTORY",
           "LIQUIDITY_BELOW_MIN", "REGIME_OFF", "BELOW_SLOW_SMA", "FAST_SMA_NOT_RISING", "NO_PULLBACK_TRIGGER", "BELOW_DEPTH_GUARD", "COOLDOWN", "CANDIDATE")


def _positive(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


TIE_BAND = 1e-9                                                                     # relative: far wider than the prefix sums' rounding error


def _exceeds(approx: float, scale: float, xs, ys) -> bool:
    """sum(xs) > sum(ys), exactly. `approx` (a prefix-sum estimate of the difference) decides when it is clearly away from 0; near a tie the
    sign of math.fsum over both windows decides (correctly rounded, so its sign is the exact sign)."""
    if abs(approx) > TIE_BAND * scale:
        return approx > 0
    return math.fsum([*xs, *(-y for y in ys)]) > 0


def _volume(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


class _Series:
    """One symbol's adjusted bars and their indicators, by the symbol's own bar index."""

    def __init__(self, bars):
        self.dates = [b.date for b in bars]
        self.index = {d: k for k, d in enumerate(self.dates)}
        self.close = [b.close for b in bars]
        self.prefix = [0.0]
        for c in self.close:
            self.prefix.append(self.prefix[-1] + c)
        self.sma_slow = sma(self.close, SLOW_SMA)
        self.sma_fast = sma(self.close, FAST_SMA)
        self.rsi = wilder_rsi(self.close, RSI_PERIOD)
        self.atr = wilder_atr([b.high for b in bars], [b.low for b in bars], self.close, ATR_PERIOD)

    def above_slow(self, k: int) -> bool:
        """close[k] > SMA200 at k: 200 x close > the window sum."""
        p, a, end = self.prefix, k - SLOW_SMA + 1, k + 1                                  # prefix[end] sums close[0 .. end - 1]
        total = p[end] - p[a]
        return _exceeds(SLOW_SMA * self.close[k] - total, total, [self.close[k]] * SLOW_SMA, self.close[a:end])

    def fast_rising(self, k: int) -> bool:
        """SMA50 at k > SMA50 SLOPE_LOOKBACK bars earlier: one window sum > the other."""
        p, end, end_b = self.prefix, k + 1, k - SLOPE_LOOKBACK + 1                        # the windows end at k and at k - 20
        now, prior = p[end] - p[end - FAST_SMA], p[end_b] - p[end_b - FAST_SMA]
        return _exceeds(now - prior, now + prior, self.close[end - FAST_SMA:end], self.close[end_b - FAST_SMA:end_b])

    def complete(self, start: str, end: str) -> bool:
        """A bar on every one of the WARMUP_SESSIONS dataset sessions [start .. end] (the symbol's dates are a subset of the sessions)."""
        a, b = self.index.get(start), self.index.get(end)
        return a is not None and b is not None and b - a == WARMUP_SESSIONS - 1


class SwingPullbackCore:
    def __init__(self, use_pullback_rule: bool = True, rsi_max: float = RSI_MAX, stop_atr: float = STOP_ATR, target_atr: float = TARGET_ATR):
        self.use_pullback_rule = bool(use_pullback_rule)
        self.rsi_max, self.stop_atr, self.target_atr = float(rsi_max), float(stop_atr), float(target_atr)
        if not (self.stop_atr > 0 and self.target_atr > 0):
            raise ValueError("stop_atr and target_atr must be positive")

    @staticmethod
    def listing(symbols: List[str], data: SwingData) -> Dict[str, Optional[str]]:
        out, seen = {}, set()
        for s in sorted(symbols):
            iss = data.issuer(s)
            out[s] = "SECOND_LISTING" if iss in seen else None
            seen.add(iss)
        return out

    def trigger(self, s: _Series, k: int) -> bool:
        """Wilder RSI(2) <= the threshold, or three successive lower closes, at the symbol's bar k (k >= LOWER_CLOSES)."""
        c = s.close
        return s.rsi[k] <= self.rsi_max or all(c[k - j] < c[k - j - 1] for j in range(LOWER_CLOSES))

    @staticmethod
    def guard(s: _Series, k: int) -> bool:
        return s.close[k] >= DEPTH_GUARD * s.sma_fast[k]

    def evaluate(self, dataset, trace: bool = False) -> dict:
        data = SwingData(dataset)
        sessions = data.sessions
        symbols = sorted(s for s in RESEARCH_UNIVERSE if s in dataset.bars)
        lst = self.listing(symbols, data)
        spy = _Series(data.bars(REGIME_SYMBOL))
        funnel = {r: 0 for r in REASONS}
        signals: List[dict] = []
        rows: List[dict] = []
        for sym in symbols:
            bars = data.bars(sym)
            if lst[sym]:
                funnel["SECOND_LISTING"] += len(bars)
                if trace:
                    rows += [{"symbol": sym, "date": b.date, "reason": "SECOND_LISTING", "pre_cooldown": False, "pullback": None} for b in bars]
                continue
            s = _Series(bars)
            last: Optional[int] = None                                                      # the dataset index of the symbol's previous candidate
            for k, d in enumerate(s.dates):
                i = data.position(d)
                reason, pull = self._rules(sym, s, k, i, sessions, spy, data)
                pre = reason is None
                if pre and last is not None and i - last <= COOLDOWN_SESSIONS:
                    reason = "COOLDOWN"
                if reason is None:
                    reason = "CANDIDATE"
                    last = i
                    signals.append(self._signal(sym, s, k, i, sessions, spy, data))
                funnel[reason] += 1
                if trace:
                    rows.append({"symbol": sym, "date": d, "reason": reason, "pre_cooldown": pre, "pullback": pull})
        out = {"signals": signals, "funnel": funnel, "volume_feed": data.volume_feed, "symbols": symbols,
               "second_listings": sorted(s for s, r in lst.items() if r)}
        if trace:
            out["trace"] = rows
        return out

    def _rules(self, sym, s: _Series, k: int, i: int, sessions, spy: _Series, data: SwingData):
        """(the first failing rule or None when every rule holds, the pullback rule's value where it is computable)."""
        if i < WARMUP_SESSIONS - 1:
            return "WARMUP", None
        start, d = sessions[i - WARMUP_SESSIONS + 1], sessions[i]
        if not s.complete(start, d):
            return "MISSING_WINDOW_BAR", None
        if not spy.complete(start, d):
            return "REGIME_WINDOW_INCOMPLETE", None
        pull = self.trigger(s, k) and self.guard(s, k)                                     # the pullback rule, recorded for both versions
        rc = data.raw_close(sym, d)
        if not _positive(rc):
            return "NO_RAW_PRICE", pull
        if rc < MIN_PRICE:
            return "PRICE_BELOW_MIN", pull
        if data.volume_feed == "SIP":
            window = sessions[i - DOLLAR_VOLUME_SESSIONS + 1:i + 1]
            pairs = [(data.raw_close(sym, x), data.raw_volume(sym, x)) for x in window]
            if any(not _positive(c) or not _volume(v) for c, v in pairs):
                return "LIQUIDITY_HISTORY", pull
            if statistics.median(c * v for c, v in pairs) < MIN_DOLLAR_VOLUME:
                return "LIQUIDITY_BELOW_MIN", pull
        q = spy.index[d]
        if not spy.above_slow(q):
            return "REGIME_OFF", pull
        if not s.above_slow(k):
            return "BELOW_SLOW_SMA", pull
        if not s.fast_rising(k):
            return "FAST_SMA_NOT_RISING", pull
        if self.use_pullback_rule:                                                          # V1 only: C1 omits trigger AND guard
            if not self.trigger(s, k):
                return "NO_PULLBACK_TRIGGER", pull
            if not self.guard(s, k):
                return "BELOW_DEPTH_GUARD", pull
        return None, pull

    def _signal(self, sym, s: _Series, k: int, i: int, sessions, spy: _Series, data: SwingData) -> dict:
        d, c = sessions[i], s.close
        q = spy.index[d]
        window = sessions[i - DOLLAR_VOLUME_SESSIONS + 1:i + 1]
        mdv = statistics.median(data.raw_close(sym, x) * data.raw_volume(sym, x) for x in window) if data.volume_feed == "SIP" else None
        lower = all(c[k - j] < c[k - j - 1] for j in range(LOWER_CLOSES))
        return {"symbol": sym, "signal_date": d, "close": c[k], "stop": c[k] - self.stop_atr * s.atr[k], "target": c[k] + self.target_atr * s.atr[k],
                "atr": s.atr[k], "sma_slow": s.sma_slow[k], "sma_fast": s.sma_fast[k], "sma_fast_prior": s.sma_fast[k - SLOPE_LOOKBACK],
                "sma_fast_prior_date": s.dates[k - SLOPE_LOOKBACK], "rsi": s.rsi[k], "lower_closes": lower,
                "depth_ratio": c[k] / s.sma_fast[k], "regime_close": spy.close[q], "regime_sma_slow": spy.sma_slow[q],
                "raw_close": data.raw_close(sym, d), "median_dollar_volume": mdv, "window_start": sessions[i - WARMUP_SESSIONS + 1]}
