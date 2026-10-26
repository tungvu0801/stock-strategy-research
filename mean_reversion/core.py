"""mean_reversion/core.py — MeanReversionStrategyCore: the ONE implementation of the Mean Reversion v1 rules (weekly schedule, eligibility, the
signal-window data integrity, the 5-session return, ranking, selection, rebalance decision). The historical adapter calls it; a future paper /
live evaluator must call it too - the rules are never re-implemented (the independent audit, mean_reversion/audit.py, re-derives them separately
on purpose).

Pure and deterministic: no clock, no randomness, no network, no model. Data comes through mean_reversion/data.py's interface."""
from __future__ import annotations

import statistics
from datetime import date
from typing import Dict, List, Optional, Set

from mean_reversion import (ADJUSTED_LOSS_TOL, DOLLAR_VOLUME_SESSIONS, LOOKBACK_SESSIONS, MIN_MEDIAN_DOLLAR_VOLUME, MIN_PRICE, NORMAL_DAY, SPLIT_DAY_TOL,
                            SPLIT_SESSION, TOP_N)

INELIGIBLE = ("SECOND_LISTING", "NO_PRICE", "PRICE_BELOW_MIN", "LIQUIDITY_HISTORY", "LIQUIDITY_BELOW_MIN", "INSUFFICIENT_HISTORY", "MISSING_ENDPOINT_BAR",
              "INVALID_PRICE", "DATA_INCONSISTENT")
WINDOW_ANOMALIES = ("RATIO_ARTIFACT", "INCONSISTENT_SPLIT", "SPLIT_NOT_ABSORBED", "ADJUSTED_LOSS_WITHOUT_RAW_MOVE", "UNVERIFIABLE")


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


class MeanReversionStrategyCore:
    def __init__(self, top_n: int = TOP_N, lookback_sessions: int = LOOKBACK_SESSIONS):
        self.top_n, self.lookback = int(top_n), int(lookback_sessions)
        if self.top_n < 1 or self.lookback < 1:
            raise ValueError("top_n >= 1 and lookback_sessions >= 1")

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

    # ---------------------------------------------------------------- the signal window must be internally consistent (never a return cap)
    def window_anomaly(self, symbol: str, start_d: str, T: str, data) -> Optional[dict]:
        """The first inconsistent pair of consecutive bars of the stock inside [start_d, T], or None. A ratio move is classified like the Stage
        6.5 split audit (a REAL split must be absorbed by the adjusted series; a RATIO_ARTIFACT or an INCONSISTENT move is not usable); without a
        ratio move, the adjusted day return may not fall more than ADJUSTED_LOSS_TOL below the raw one (a distribution only raises it)."""
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
        return None

    # ---------------------------------------------------------------- one stock at one rebalance
    def snapshot(self, symbol: str, T: str, data, listing_reason: Optional[str] = None) -> dict:
        snap = {"symbol": symbol, "eligible": False, "reason": listing_reason, "recent_return": None, "inputs": {}, "anomaly": None}
        if listing_reason:
            return snap
        i = data.position(T)
        start_d = data.sessions[i - self.lookback]
        px = data.raw_close(symbol, T)
        b = data.adj_close(symbol, T)
        if px is None or b is None:
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
        a = data.adj_close(symbol, start_d)
        if a is None:
            return {**snap, "reason": "MISSING_ENDPOINT_BAR"}               # never the nearest other bar, never interpolated, never a zero return
        if a <= 0 or b <= 0:
            return {**snap, "reason": "INVALID_PRICE"}
        an = self.window_anomaly(symbol, start_d, T, data)
        if an is not None:
            return {**snap, "reason": "DATA_INCONSISTENT", "anomaly": an}    # flagged and excluded - never clipped into a usable return
        inputs = {"raw_close": px, "median_dollar_volume": mdv, "lookback_start_date": start_d, "lookback_end_date": T, "lookback_start_close": a,
                  "lookback_end_close": b}
        return {**snap, "eligible": True, "reason": None, "recent_return": b / a - 1.0, "inputs": inputs}

    # ---------------------------------------------------------------- cross-sectional ranking: the MOST NEGATIVE recent return first
    def rank(self, snaps: List[dict]) -> List[dict]:
        elig = [s for s in snaps if s["eligible"]]
        pr = pct_ranks([(s["symbol"], -s["recent_return"]) for s in elig])          # most negative return -> percentile 1.0
        out = [{"symbol": s["symbol"], "recent_return": s["recent_return"], "percentile": pr[s["symbol"]], "snapshot": s} for s in elig]
        out.sort(key=lambda x: (x["recent_return"], x["symbol"]))
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
                "data_exclusions": [{"symbol": s["symbol"], **s["anomaly"]} for s in snaps if s["anomaly"]],
                "summary": {"universe": len(symbols), "eligible": len(ranked), "ineligible_by_reason": dict(sorted(reasons.items()))}}
