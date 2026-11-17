"""md_eval/costs.py - the cost model: slippage and half-spread by liquidity bucket (ASSUMPTION defaults, decision D4), at 1x or 2x; dated,
sourced sell-side regulatory fees (SEC Section 31, FINRA TAF with its cap); the CAT fee (UNKNOWN, never invented); commission as charged ($0,
unverified); the first-order break-even cost per side.

The fee rows are copied, with their sources, from the Stage 7.8B-1 decision lock (STAGE_7_8B1_DECISION_LOCK.md, R12 and R13, recorded
2026-11-02). They are NOT verified here (no network):
  - FINRA TAF: 0.000195 USD per share sold, capped at 9.79 USD per trade, with a fee holiday at 0 USD from 2026-10-01 to 2026-12-31. The
    holiday is consistent with Alpaca's 2026-10-12 schedule listing 0 USD per share; FINRA's rulebook page still shows 0.000166 / 8.30. Every
    TAF charge therefore carries TAF_SOURCE_DISCREPANCY_UNVERIFIED. The 2026 rate's start date (2026-01-01) is an assumption of this table.
  - a sell date with no recorded rate uses the MAXIMUM recorded rate (conservative) and is flagged REGULATORY_FEE_HISTORY_MISSING.
Cost bucket values are ASSUMPTION defaults: the final values are fixed in a pre-registration, never tuned."""
from __future__ import annotations

import math
import statistics
from types import MappingProxyType
from typing import Optional, Sequence, Tuple

from md_eval import (CAT_FEE_NOT_MODELLED, COMMISSION_ASSUMED_UNVERIFIED, COST_BUCKETS_ASSUMPTION, LIQUIDITY_BUCKET_CONSERVATIVE,
                     REGULATORY_FEE_HISTORY_MISSING, TAF_SOURCE_DISCREPANCY_UNVERIFIED)
from md_eval.contract import unknown

FEE_SOURCE = "STAGE_7_8B1_DECISION_LOCK.md R12 / R13 (recorded 2026-11-02; not re-verified)"
SEC_SECTION_31 = (
    MappingProxyType({"from": "2026-04-04", "to": None, "per_million_usd": 20.60, "source": "SEC fee rate advisory 2026-2"}),
    MappingProxyType({"from": "2025-05-14", "to": "2026-04-03", "per_million_usd": 0.0, "source": "SEC Release 34-104909"}),
)
FINRA_TAF = (
    MappingProxyType({"from": "2026-10-01", "to": "2026-12-31", "per_share_usd": 0.0, "max_per_trade_usd": 0.0,
                      "source": "SR-FINRA-2026-021 (fee holiday); Alpaca schedule 2026-10-12 lists 0 USD"}),
    MappingProxyType({"from": "2026-01-01", "to": "2026-09-30", "per_share_usd": 0.000195, "max_per_trade_usd": 9.79,
                      "source": "SR-FINRA-2026-021; FINRA billing FAQ (start date assumed)"}),
)
CAT_FEE = unknown("CAT_FEE_SCHEDULE_NOT_RECORDED")
COMMISSION_PER_ORDER = 0.0                                   # R13: as charged; an ASSUMPTION to verify before any real run

COST_LABEL = "ASSUMPTION"
DEFAULT_BUCKETS = (                                          # min 20-session median raw dollar volume (USD) -> bps per side
    MappingProxyType({"min_median_dollar_volume": 100_000_000.0, "slippage_bps": 5.0, "half_spread_bps": 1.0}),
    MappingProxyType({"min_median_dollar_volume": 20_000_000.0, "slippage_bps": 5.0, "half_spread_bps": 2.0}),
    MappingProxyType({"min_median_dollar_volume": 0.0, "slippage_bps": 10.0, "half_spread_bps": 3.0}),
)
MULTIPLIERS = (1.0, 2.0)
LIQUIDITY_SESSIONS = 20


def _rate(table, session: str, key: str) -> Tuple[float, Optional[dict], bool]:
    """(rate, the row or None, history missing?)."""
    for row in table:
        if row["from"] <= session and (row["to"] is None or session <= row["to"]):
            return float(row[key]), dict(row), False
    return max(float(r[key]) for r in table), None, True


def sec_rate(session: str):
    return _rate(SEC_SECTION_31, session, "per_million_usd")


def taf(session: str) -> Tuple[float, float, Optional[dict], bool]:
    for row in FINRA_TAF:
        if row["from"] <= session and (row["to"] is None or session <= row["to"]):
            return float(row["per_share_usd"]), float(row["max_per_trade_usd"]), dict(row), False
    worst = max(FINRA_TAF, key=lambda r: r["per_share_usd"])
    return float(worst["per_share_usd"]), float(worst["max_per_trade_usd"]), None, True


class CostModel:
    def __init__(self, buckets: Sequence = DEFAULT_BUCKETS, multiplier: float = 1.0, label: str = COST_LABEL, fees: bool = True):
        """fees=False (with ZERO_COST_BUCKETS) is the zero-cost run of the break-even figure and of the reconciliation fixture only."""
        self.fees = bool(fees)
        if float(multiplier) not in MULTIPLIERS:
            raise ValueError(f"the cost multiplier is one of the pre-registered levels {MULTIPLIERS}; got {multiplier!r}")
        b = sorted((dict(x) for x in buckets), key=lambda x: -x["min_median_dollar_volume"])
        if not b or b[-1]["min_median_dollar_volume"] != 0.0:
            raise ValueError("the buckets must end with a 0 USD floor (every instrument falls in one)")
        self.buckets = tuple(MappingProxyType(x) for x in b)
        self.multiplier = float(multiplier)
        self.label = label

    def identity(self) -> dict:
        return {"buckets": [dict(x) for x in self.buckets], "multiplier": self.multiplier, "label": self.label, "fees": self.fees, "fee_source": FEE_SOURCE,
                "sec_section_31": [dict(r) for r in SEC_SECTION_31], "finra_taf": [dict(r) for r in FINRA_TAF], "cat_fee": CAT_FEE.canonical(),
                "commission_per_order": COMMISSION_PER_ORDER}

    def bucket(self, median_dollar_volume: Optional[float]) -> Tuple[dict, list]:
        """The instrument's bucket; an unmeasurable liquidity (fewer than 20 sessions, non-consolidated volume) -> the most conservative."""
        flags = [COST_BUCKETS_ASSUMPTION] if self.label == COST_LABEL else []
        if median_dollar_volume is None or not math.isfinite(median_dollar_volume):
            return dict(self.buckets[-1]), flags + [LIQUIDITY_BUCKET_CONSERVATIVE]
        for x in self.buckets:
            if median_dollar_volume >= x["min_median_dollar_volume"]:
                return dict(x), flags
        return dict(self.buckets[-1]), flags + [LIQUIDITY_BUCKET_CONSERVATIVE]   # pragma: no cover - the 0 floor always matches

    def friction_rate(self, bucket: dict) -> float:
        return (bucket["slippage_bps"] + bucket["half_spread_bps"]) * self.multiplier / 1e4

    def cost(self, side: str, shares: int, price: float, bucket: dict, session: str) -> Tuple[float, dict, list]:
        """(total cost in USD, the breakdown, flags). Slippage and spread scale with the multiplier; fees are charged as dated."""
        notional = shares * price
        out = {"friction": notional * self.friction_rate(bucket), "commission": COMMISSION_PER_ORDER, "sec_section_31": 0.0, "finra_taf": 0.0,
               "cat": 0.0}
        flags = [COMMISSION_ASSUMED_UNVERIFIED]
        if side not in ("BUY", "SELL"):
            raise ValueError(side)
        if side == "SELL" and self.fees:
            rate, _, miss = sec_rate(session)
            out["sec_section_31"] = notional * rate / 1e6
            per_share, cap, _, miss_t = taf(session)
            out["finra_taf"] = min(shares * per_share, cap)
            flags += [TAF_SOURCE_DISCREPANCY_UNVERIFIED, CAT_FEE_NOT_MODELLED]
            if miss or miss_t:
                flags.append(REGULATORY_FEE_HISTORY_MISSING)
        return sum(out.values()), out, flags


def median_dollar_volume(bars) -> Optional[float]:
    """The median raw close x raw volume of the last 20 bars, when all 20 are present, numeric and CONSOLIDATED volume; else None."""
    tail = list(bars)[-LIQUIDITY_SESSIONS:]
    if len(tail) < LIQUIDITY_SESSIONS:
        return None
    vals = []
    for b in tail:
        if b.volume_feed != "CONSOLIDATED" or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (b.raw_close, b.raw_volume)):
            return None
        vals.append(b.raw_close * b.raw_volume)
    return statistics.median(vals)


def break_even_bps_per_side(pnl_at_zero_cost: float, traded_notional_both_sides: float) -> dict:
    """FIRST ORDER (no re-sizing): the per-side cost, in bps of traded notional, that would make the zero-cost P/L zero."""
    if not traded_notional_both_sides:
        return {"bps_per_side": None, "label": "FIRST_ORDER", "reason": "NO_TRADES"}
    return {"bps_per_side": pnl_at_zero_cost * 1e4 / traded_notional_both_sides, "label": "FIRST_ORDER"}


ZERO_COST_BUCKETS = (MappingProxyType({"min_median_dollar_volume": 0.0, "slippage_bps": 0.0, "half_spread_bps": 0.0}),)
