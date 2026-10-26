"""value_quality/core.py — ValueQualityStrategyCore: the ONE implementation of the Value + Quality rules (eligibility, factors, ranking, selection,
rebalance decision). The historical adapter calls it; a future paper / live evaluator must call it too - the rules are never re-implemented.

    fundamental snapshot (value_quality/pit.py, point in time) + price / liquidity snapshot (raw closes, split ratios, volumes)
        -> ValueQualityStrategyCore.evaluate(T, ...) -> ranked candidates + {enter, hold, exit}

Pure and deterministic: no clock, no randomness, no network, no model. Data comes through the small `data` interface (see value_quality/data.py)."""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Set

from value_quality import (DOLLAR_VOLUME_SESSIONS, EXCLUDED_SIC, MAX_FUNDAMENTAL_AGE_DAYS, MIN_FACTORS_PER_GROUP, MIN_MARKET_CAP, MIN_MEDIAN_DOLLAR_VOLUME,
                           MIN_PRICE, QUALITY_FACTORS, TOP_N, US_GAAP_TAGS, VALUE_FACTORS, VALUE_WEIGHT)
from value_quality import pit

INELIGIBLE = ("NO_CIK", "EXCLUDED_SIC", "SECOND_LISTING", "NO_PRICE", "PRICE_BELOW_MIN", "LIQUIDITY_HISTORY", "LIQUIDITY_BELOW_MIN", "NO_SHARE_COUNT",
              "MARKET_CAP_BELOW_MIN", "NO_ASSETS", "STALE_FUNDAMENTALS", "NO_TTM_EARNINGS", "NO_TTM_CASH_FLOW", "TOO_FEW_VALUE_FACTORS", "TOO_FEW_QUALITY_FACTORS")


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


def _r(x: Optional[float], n: int = 10) -> Optional[float]:
    return None if x is None else round(float(x), n)


class ValueQualityStrategyCore:
    def __init__(self, top_n: int = TOP_N, value_weight: float = VALUE_WEIGHT):
        self.top_n, self.value_weight = int(top_n), float(value_weight)
        self.quality_weight = 1.0 - self.value_weight                         # v1.0: VALUE_WEIGHT / QUALITY_WEIGHT = 0.5 / 0.5

    # ---------------------------------------------------------------- the universe: one listing per issuer, no financial / real-estate SIC
    def listing(self, symbols: List[str], data) -> Dict[str, Optional[str]]:
        out, first = {}, {}
        for s in sorted(symbols):
            iss = data.issuer(s)
            if not iss or not iss.get("cik"):
                out[s] = "NO_CIK"
            elif iss.get("sic") is not None and EXCLUDED_SIC[0] <= int(iss["sic"]) <= EXCLUDED_SIC[1]:
                out[s] = "EXCLUDED_SIC"
            elif iss["cik"] in first:
                out[s] = "SECOND_LISTING"                                           # e.g. GOOGL after GOOG: one issuer is ranked once
            else:
                first[iss["cik"]] = s
                out[s] = None
        return out

    # ---------------------------------------------------------------- one stock at one rebalance
    def snapshot(self, symbol: str, T: str, data, cal: pit.Calendar, listing_reason: Optional[str] = None) -> dict:
        snap = {"symbol": symbol, "eligible": False, "reason": listing_reason, "factors": {}, "inputs": {}, "as_of": {}}
        if listing_reason:
            return snap
        px = data.raw_close(symbol, T)
        if px is None:
            return {**snap, "reason": "NO_PRICE"}
        if px < MIN_PRICE:
            return {**snap, "reason": "PRICE_BELOW_MIN"}
        dv = data.dollar_volumes(symbol, T, DOLLAR_VOLUME_SESSIONS)
        if len(dv) < DOLLAR_VOLUME_SESSIONS:
            return {**snap, "reason": "LIQUIDITY_HISTORY"}
        mdv = statistics.median(dv)
        if mdv < MIN_MEDIAN_DOLLAR_VOLUME:
            return {**snap, "reason": "LIQUIDITY_BELOW_MIN"}
        sh = pit.shares(data.facts(symbol, US_GAAP_TAGS["diluted_shares"]), T, cal, data.split_ratio(symbol))
        if sh is None:
            return {**snap, "reason": "NO_SHARE_COUNT"}
        mcap = px * sh["value"]
        if mcap < MIN_MARKET_CAP:
            return {**snap, "reason": "MARKET_CAP_BELOW_MIN"}
        assets = pit.instant(data.facts(symbol, US_GAAP_TAGS["assets"]), T, cal)
        if assets is None or assets["value"] <= 0:
            return {**snap, "reason": "NO_ASSETS"}
        if pit.days(assets["period_end"], T) > MAX_FUNDAMENTAL_AGE_DAYS:
            return {**snap, "reason": "STALE_FUNDAMENTALS"}
        ni = pit.ttm(data.facts(symbol, US_GAAP_TAGS["net_income"]), T, cal)
        if ni is None or pit.days(ni["period_end"], T) > MAX_FUNDAMENTAL_AGE_DAYS:
            return {**snap, "reason": "NO_TTM_EARNINGS"}
        ocf = pit.ttm(data.facts(symbol, US_GAAP_TAGS["operating_cash_flow"]), T, cal)
        if ocf is None or pit.days(ocf["period_end"], T) > MAX_FUNDAMENTAL_AGE_DAYS:
            return {**snap, "reason": "NO_TTM_CASH_FLOW"}
        eq = pit.instant(data.facts(symbol, US_GAAP_TAGS["equity"]), T, cal)
        if eq is not None and pit.days(eq["period_end"], T) > MAX_FUNDAMENTAL_AGE_DAYS:
            eq = None                                                               # stale equity: its two factors are missing, never filled
        a, n, o, e = assets["value"], ni["value"], ocf["value"], (eq["value"] if eq else None)
        factors = {
            "earnings_yield": n / mcap if n > 0 else None,                          # negative earnings are NOT cheap: the factor is missing
            "book_to_market": e / mcap if e is not None and e > 0 else None,        # negative / missing equity: missing
            "cash_flow_yield": o / mcap if o > 0 else None,
            "return_on_assets": n / a,                                              # negative profitability ranks low (correct direction)
            "cash_flow_on_assets": o / a,
            "equity_ratio": e / a if e is not None else None,                       # negative equity ranks lowest
        }
        nv = sum(1 for f in VALUE_FACTORS if factors[f] is not None)
        nq = sum(1 for f in QUALITY_FACTORS if factors[f] is not None)
        inputs = {"raw_close": px, "median_dollar_volume": mdv, "diluted_share_count": sh, "market_cap": mcap, "assets": assets, "net_income_ttm": ni,
                  "operating_cash_flow_ttm": ocf, "equity": eq}
        snap = {**snap, "factors": factors, "inputs": inputs,
                "as_of": {"raw_close": T, "market_cap": T, "assets": assets["available_at"], "net_income_ttm": ni["available_at"],
                          "operating_cash_flow_ttm": ocf["available_at"], "diluted_share_count": sh["available_at"], **({"equity": eq["available_at"]} if eq else {})}}
        if nv < MIN_FACTORS_PER_GROUP:
            return {**snap, "reason": "TOO_FEW_VALUE_FACTORS"}
        if nq < MIN_FACTORS_PER_GROUP:
            return {**snap, "reason": "TOO_FEW_QUALITY_FACTORS"}
        return {**snap, "eligible": True, "reason": None}

    # ---------------------------------------------------------------- cross-sectional ranking
    def rank(self, snaps: List[dict]) -> List[dict]:
        elig = [s for s in snaps if s["eligible"]]
        franks: Dict[str, Dict[str, float]] = {}
        for f in VALUE_FACTORS + QUALITY_FACTORS:
            franks[f] = pct_ranks([(s["symbol"], s["factors"][f]) for s in elig if s["factors"][f] is not None])
        out = []
        for s in elig:
            v = [franks[f][s["symbol"]] for f in VALUE_FACTORS if s["symbol"] in franks[f]]
            q = [franks[f][s["symbol"]] for f in QUALITY_FACTORS if s["symbol"] in franks[f]]
            vs, qs = sum(v) / len(v), sum(q) / len(q)
            out.append({"symbol": s["symbol"], "value_score": vs, "quality_score": qs, "composite": self.value_weight * vs + self.quality_weight * qs,
                        "factor_ranks": {f: franks[f].get(s["symbol"]) for f in VALUE_FACTORS + QUALITY_FACTORS}, "snapshot": s})
        out.sort(key=lambda x: (-x["composite"], x["symbol"]))
        for i, x in enumerate(out, start=1):
            x["rank"] = i
        return out

    # ---------------------------------------------------------------- one rebalance
    def evaluate(self, T: str, symbols: List[str], data, cal: pit.Calendar, prev: Optional[Set[str]] = None) -> dict:
        prev = set(prev or ())
        lst = self.listing(symbols, data)
        snaps = [self.snapshot(s, T, data, cal, lst.get(s)) for s in sorted(symbols)]
        ranked = self.rank(snaps)
        selected = [x["symbol"] for x in ranked[:self.top_n]]
        sel = set(selected)
        reasons: Dict[str, int] = {}
        for s in snaps:
            if not s["eligible"]:
                reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        return {"rebalance": T, "ranked": ranked, "selected": selected, "enter": sorted(sel - prev), "hold": sorted(sel & prev), "exit": sorted(prev - sel),
                "summary": {"universe": len(symbols), "eligible": len(ranked), "ineligible_by_reason": dict(sorted(reasons.items())),
                            "factor_coverage": {f: sum(1 for x in ranked if x["snapshot"]["factors"][f] is not None) for f in VALUE_FACTORS + QUALITY_FACTORS},
                            "selected": [{"symbol": x["symbol"], "rank": x["rank"], "value_score": _r(x["value_score"], 6), "quality_score": _r(x["quality_score"], 6)}
                                         for x in ranked[:self.top_n]],
                            "mean_value_score_all": _r(statistics.mean([x["value_score"] for x in ranked]), 6) if ranked else None,
                            "mean_quality_score_all": _r(statistics.mean([x["quality_score"] for x in ranked]), 6) if ranked else None}}
