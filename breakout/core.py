"""breakout/core.py — BreakoutStrategyCore: the ONE implementation of the Breakout v1 rules (weekly schedule, eligibility, the breakout-window data
integrity, the prior-range reference, breakout detection, strength, ranking, selection, continuation). The historical adapter calls it; a future
paper / live evaluator must call it too - the rules are never re-implemented (the independent audit, breakout/audit.py, re-derives them
separately on purpose).

Pure and deterministic: no clock, no randomness, no network, no model. Data comes through breakout/data.py's interface."""
from __future__ import annotations

import statistics
from datetime import date
from typing import Dict, List, Optional, Set

from breakout import (ADJUSTED_LOSS_TOL, DOLLAR_VOLUME_SESSIONS, LARGE_REBASING_TOL, LOOKBACK_SESSIONS, MAX_HOLDINGS, MIN_MEDIAN_DOLLAR_VOLUME, MIN_PRICE,
                      NORMAL_DAY, SPLIT_DAY_TOL, SPLIT_SESSION)

INELIGIBLE = ("SECOND_LISTING", "NO_PRICE", "PRICE_BELOW_MIN", "LIQUIDITY_HISTORY", "LIQUIDITY_BELOW_MIN", "INSUFFICIENT_HISTORY", "MISSING_LOOKBACK_BAR",
              "INVALID_PRICE", "DATA_INCONSISTENT")
WINDOW_ANOMALIES = ("RATIO_ARTIFACT", "INCONSISTENT_SPLIT", "SPLIT_NOT_ABSORBED", "ADJUSTED_LOSS_WITHOUT_RAW_MOVE", "LARGE_REBASING", "UNVERIFIABLE")


def pct_ranks(pairs: List[tuple]) -> Dict[str, float]:
    """Percentile ranks in (0, 1] (the highest value = 1.0), average rank for ties; deterministic."""
    srt = sorted(pairs, key=lambda x: (x[1], x[0]))
    n, out, i = len(srt), {}, 0
    while i < n:
        j = i
        while j + 1 < n and srt[j + 1][1] == srt[i][1]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[srt[k][0]] = avg / n
        i = j + 1
    return out


def iso_week(d: str) -> tuple:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return (y, w)


class BreakoutStrategyCore:
    def __init__(self, max_holdings: int = MAX_HOLDINGS, lookback_sessions: int = LOOKBACK_SESSIONS):
        self.max_holdings, self.lookback = int(max_holdings), int(lookback_sessions)
        if self.max_holdings < 1 or self.lookback < 1:
            raise ValueError("max_holdings >= 1 and lookback_sessions >= 1")

    # ---------------------------------------------------------------- the schedule: first session of each ISO week, once the histories exist
    def schedule(self, sessions: List[str]) -> List[str]:
        start = max(self.lookback, DOLLAR_VOLUME_SESSIONS)
        return [s for i, s in enumerate(sessions) if i >= start and iso_week(s) != iso_week(sessions[i - 1])]

    # ---------------------------------------------------------------- the universe: one listing per issuer
    def listing(self, symbols: List[str], data) -> Dict[str, Optional[str]]:
        out, seen = {}, set()
        for s in sorted(symbols):
            iss = data.issuer(s)
            out[s] = "SECOND_LISTING" if iss in seen else None
            seen.add(iss)
        return out

    # ---------------------------------------------------------------- the breakout window must be internally consistent (never a return cap)
    def window_anomaly(self, symbol: str, start_d: str, T: str, data) -> Optional[dict]:
        """The first inconsistent pair of consecutive bars of the stock inside [start_d, T], or None: a ratio move that is not a REAL split absorbed
        by the adjusted series; an adjusted loss more than ADJUSTED_LOSS_TOL below the raw move; an adjusted gain more than LARGE_REBASING_TOL
        above it (a large distribution re-basing the history the level reference is taken from)."""
        ds = data.bar_dates(symbol, start_d, T)
        for d0, d1 in zip(ds, ds[1:]):
            r0, r1, q0, q1 = data.raw_close(symbol, d0), data.raw_close(symbol, d1), data.split_ratio(symbol, d0), data.split_ratio(symbol, d1)
            a0, a1 = data.adj_close(symbol, d0), data.adj_close(symbol, d1)
            if None in (r0, r1, q0, q1) or min(r0, r1, q0, q1) <= 0:
                return {"code": "UNVERIFIABLE", "from": d0, "to": d1}
            raw_ret, adj_ret = r1 / r0 - 1.0, a1 / a0 - 1.0
            if abs(q1 / q0 - 1.0) > SPLIT_SESSION:
                spl_ret = (r1 / q1) / (r0 / q0) - 1.0
                raw_jump, spl_jump = abs(raw_ret) > NORMAL_DAY, abs(spl_ret) > NORMAL_DAY
                if spl_jump and not raw_jump:
                    return {"code": "RATIO_ARTIFACT", "from": d0, "to": d1, "adjusted_return": adj_ret, "raw_return": raw_ret}
                if not (raw_jump and not spl_jump):
                    return {"code": "INCONSISTENT_SPLIT", "from": d0, "to": d1, "adjusted_return": adj_ret, "raw_return": raw_ret}
                if abs(adj_ret - spl_ret) > SPLIT_DAY_TOL:
                    return {"code": "SPLIT_NOT_ABSORBED", "from": d0, "to": d1, "adjusted_return": adj_ret, "split_adjusted_return": spl_ret}
            elif adj_ret < raw_ret - ADJUSTED_LOSS_TOL:
                return {"code": "ADJUSTED_LOSS_WITHOUT_RAW_MOVE", "from": d0, "to": d1, "adjusted_return": adj_ret, "raw_return": raw_ret}
            elif adj_ret > raw_ret + LARGE_REBASING_TOL:
                return {"code": "LARGE_REBASING", "from": d0, "to": d1, "adjusted_return": adj_ret, "raw_return": raw_ret}
        return None

    # ---------------------------------------------------------------- one stock at one rebalance
    def snapshot(self, symbol: str, T: str, data, listing_reason: Optional[str] = None) -> dict:
        snap = {"symbol": symbol, "eligible": False, "reason": listing_reason, "breakout": False, "strength": None, "inputs": {}, "anomaly": None}
        if listing_reason:
            return snap
        i = data.position(T)
        ref_sessions = data.sessions[i - self.lookback:i]                         # the completed sessions BEFORE T: T is never in its own reference
        px, close = data.raw_close(symbol, T), data.adj_close(symbol, T)
        if px is None or close is None:
            return {**snap, "reason": "NO_PRICE"}
        if px < MIN_PRICE:
            return {**snap, "reason": "PRICE_BELOW_MIN"}
        dv = data.dollar_volumes(symbol, T, DOLLAR_VOLUME_SESSIONS)
        if len(dv) < DOLLAR_VOLUME_SESSIONS:
            return {**snap, "reason": "LIQUIDITY_HISTORY"}
        mdv = statistics.median(dv)
        if mdv < MIN_MEDIAN_DOLLAR_VOLUME:
            return {**snap, "reason": "LIQUIDITY_BELOW_MIN"}
        first = data.first_date(symbol)
        if first is None or len(ref_sessions) < self.lookback or first > ref_sessions[0]:
            return {**snap, "reason": "INSUFFICIENT_HISTORY"}                   # never a shortened lookback
        highs = [data.adj_high(symbol, d) for d in ref_sessions]
        if any(h is None for h in highs):
            return {**snap, "reason": "MISSING_LOOKBACK_BAR"}                  # a complete lookback: never the nearest other bar, never filled
        if close <= 0 or min(highs) <= 0:
            return {**snap, "reason": "INVALID_PRICE"}
        an = self.window_anomaly(symbol, ref_sessions[0], T, data)
        if an is not None:
            return {**snap, "reason": "DATA_INCONSISTENT", "anomaly": an}      # flagged and excluded - never clipped into a usable breakout
        ref = max(highs)
        k = highs.index(ref)
        broke = close > ref
        inputs = {"raw_close": px, "median_dollar_volume": mdv, "reference_start_date": ref_sessions[0], "reference_end_date": ref_sessions[-1],
                  "reference_date": ref_sessions[k], "breakout_reference": ref, "close": close}
        return {**snap, "eligible": True, "reason": None, "breakout": broke, "strength": (close / ref - 1.0) if broke else None, "inputs": inputs}

    # ---------------------------------------------------------------- the breakout candidates, the strongest first
    def rank(self, snaps: List[dict]) -> List[dict]:
        cands = [s for s in snaps if s["eligible"] and s["breakout"]]
        pr = pct_ranks([(s["symbol"], s["strength"]) for s in cands])
        out = [{"symbol": s["symbol"], "strength": s["strength"], "percentile": pr[s["symbol"]], "snapshot": s} for s in cands]
        out.sort(key=lambda x: (-x["strength"], x["symbol"]))
        for i, x in enumerate(out, start=1):
            x["rank"] = i
        return out

    # ---------------------------------------------------------------- one rebalance
    def evaluate(self, T: str, symbols: List[str], data, prev: Optional[Set[str]] = None) -> dict:
        prev = set(prev or ())
        lst = self.listing(symbols, data)
        snaps = [self.snapshot(s, T, data, lst.get(s)) for s in sorted(symbols)]
        ranked = self.rank(snaps)
        selected = [x["symbol"] for x in ranked[:self.max_holdings]]              # at most N; fewer when fewer break out - never filled
        sel = set(selected)
        eligible = [s["symbol"] for s in snaps if s["eligible"]]
        reasons: Dict[str, int] = {}
        for s in snaps:
            if not s["eligible"]:
                reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        return {"rebalance": T, "eligible": eligible, "ranked": ranked, "selected": selected, "enter": sorted(sel - prev), "hold": sorted(sel & prev),
                "exit": sorted(prev - sel), "data_exclusions": [{"symbol": s["symbol"], **s["anomaly"]} for s in snaps if s["anomaly"]],
                "summary": {"universe": len(symbols), "eligible": len(eligible), "breakout_candidates": len(ranked), "selected": len(selected),
                            "invested_fraction": len(selected) / self.max_holdings, "ineligible_by_reason": dict(sorted(reasons.items()))}}
