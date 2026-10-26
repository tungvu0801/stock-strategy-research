"""momentum/core.py — MomentumStrategyCore: the ONE implementation of the Momentum v1 rules (schedule, eligibility, 12-1 return, ranking, selection,
rebalance decision). The historical adapter calls it; a future paper / live evaluator must call it too - the rules are never re-implemented
(the independent audit, momentum/audit.py, re-derives them separately on purpose).

Pure and deterministic: no clock, no randomness, no network, no model. Data comes through momentum/data.py's interface."""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Set

from momentum import DOLLAR_VOLUME_SESSIONS, LOOKBACK_SESSIONS, MIN_MEDIAN_DOLLAR_VOLUME, MIN_PRICE, SKIP_SESSIONS, TOP_N

INELIGIBLE = ("SECOND_LISTING", "NO_PRICE", "PRICE_BELOW_MIN", "LIQUIDITY_HISTORY", "LIQUIDITY_BELOW_MIN", "INSUFFICIENT_HISTORY", "MISSING_ENDPOINT_BAR",
              "INVALID_PRICE")


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


class MomentumStrategyCore:
    def __init__(self, top_n: int = TOP_N, lookback_sessions: int = LOOKBACK_SESSIONS, skip_sessions: int = SKIP_SESSIONS):
        self.top_n, self.lookback, self.skip = int(top_n), int(lookback_sessions), int(skip_sessions)
        if not 0 < self.skip < self.lookback:
            raise ValueError("0 < skip_sessions < lookback_sessions")

    # ---------------------------------------------------------------- the schedule: first session of each month, once the lookback exists
    def schedule(self, sessions: List[str]) -> List[str]:
        start = max(self.lookback, DOLLAR_VOLUME_SESSIONS)
        return [s for i, s in enumerate(sessions) if i >= start and s[:7] != sessions[i - 1][:7]]

    # ---------------------------------------------------------------- the universe: one listing per issuer
    def listing(self, symbols: List[str], data) -> Dict[str, Optional[str]]:
        out, seen = {}, set()
        for s in sorted(symbols):
            iss = data.issuer(s)
            out[s] = "SECOND_LISTING" if iss in seen else None
            seen.add(iss)
        return out

    # ---------------------------------------------------------------- one stock at one rebalance
    def snapshot(self, symbol: str, T: str, data, listing_reason: Optional[str] = None) -> dict:
        snap = {"symbol": symbol, "eligible": False, "reason": listing_reason, "momentum": None, "inputs": {}}
        if listing_reason:
            return snap
        S = data.sessions
        i = data.position(T)
        start_d, end_d = S[i - self.lookback], S[i - self.skip]
        px = data.raw_close(symbol, T)
        if px is None or data.adj_close(symbol, T) is None:
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
        if first is None or first > start_d:
            return {**snap, "reason": "INSUFFICIENT_HISTORY"}               # never a shortened lookback
        a, b = data.adj_close(symbol, start_d), data.adj_close(symbol, end_d)
        if a is None or b is None:
            return {**snap, "reason": "MISSING_ENDPOINT_BAR"}               # never filled from a neighbouring session
        if a <= 0 or b <= 0:
            return {**snap, "reason": "INVALID_PRICE"}
        inputs = {"raw_close": px, "median_dollar_volume": mdv, "lookback_start_date": start_d, "lookback_end_date": end_d,
                  "lookback_start_close": a, "lookback_end_close": b}
        return {**snap, "eligible": True, "reason": None, "momentum": b / a - 1.0, "inputs": inputs}

    # ---------------------------------------------------------------- cross-sectional ranking
    def rank(self, snaps: List[dict]) -> List[dict]:
        elig = [s for s in snaps if s["eligible"]]
        pr = pct_ranks([(s["symbol"], s["momentum"]) for s in elig])
        out = [{"symbol": s["symbol"], "momentum": s["momentum"], "percentile": pr[s["symbol"]], "snapshot": s} for s in elig]
        out.sort(key=lambda x: (-x["momentum"], x["symbol"]))
        for i, x in enumerate(out, start=1):
            x["rank"] = i
        return out

    # ---------------------------------------------------------------- one rebalance
    def evaluate(self, T: str, symbols: List[str], data, prev: Optional[Set[str]] = None) -> dict:
        prev = set(prev or ())
        lst = self.listing(symbols, data)
        snaps = [self.snapshot(s, T, data, lst.get(s)) for s in sorted(symbols)]
        ranked = self.rank(snaps)
        selected = [x["symbol"] for x in ranked[:self.top_n]]
        sel = set(selected)
        reasons: Dict[str, int] = {}
        for s in snaps:
            if not s["eligible"]:
                reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        return {"rebalance": T, "ranked": ranked, "selected": selected, "enter": sorted(sel - prev), "hold": sorted(sel & prev), "exit": sorted(prev - sel),
                "summary": {"universe": len(symbols), "eligible": len(ranked), "ineligible_by_reason": dict(sorted(reasons.items()))}}
