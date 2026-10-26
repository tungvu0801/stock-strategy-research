"""strategy_registry/value_quality.py — the Strategy Lab detail sections of VALUE_QUALITY (Stage 6.4), documented from the frozen v1.0 rules in
value_quality/__init__.py. Results never live here: the Validation block (strategy_validation/service.py) shows the stored run, if any.
Research only: the strategy never emits a trading signal and its lifecycle (RESEARCH) only changes by an explicit human decision."""
from __future__ import annotations

from typing import List

from strategy_registry import NOT_EVALUATED
from strategy_registry.models import Section

LINKS = {"design_docs": "DESIGN_64_VALUE_QUALITY.md"}
SOURCES = ["value_quality/__init__.py", "value_quality/core.py", "DESIGN_64_VALUE_QUALITY.md"]


def sections() -> List[Section]:
    import value_quality as VQ
    facts = {"strategy_version": VQ.STRATEGY_VERSION, "top_n": VQ.TOP_N, "value_weight": VQ.VALUE_WEIGHT, "quality_weight": VQ.QUALITY_WEIGHT,
             "value_factors": list(VQ.VALUE_FACTORS), "quality_factors": list(VQ.QUALITY_FACTORS), "rebalance": VQ.REBALANCE, "min_price": VQ.MIN_PRICE,
             "min_market_cap": VQ.MIN_MARKET_CAP, "min_median_dollar_volume": VQ.MIN_MEDIAN_DOLLAR_VOLUME, "excluded_sic": list(VQ.EXCLUDED_SIC)}
    return [
        Section(key="overview", title="Overview", state="DOCUMENTED", sources=SOURCES, facts=facts, items=[
            "Long-only monthly rebalance of the 20 highest-ranked stocks by an equal-weight composite of value and quality, from point-in-time SEC "
            "fundamentals and daily bars.",
            "Lifecycle: Research. A validation verdict never changes it; it never produces a trading signal."]),
        Section(key="hypothesis", title="Hypothesis", state="DOCUMENTED", sources=SOURCES, items=[
            "Companies that are inexpensive versus their fundamentals AND financially strong / profitable may outperform weaker or more expensive peers "
            "over medium-to-long horizons.",
            "No momentum, no market timing. The rules were fixed before any out-of-sample data was inspected (pre-registration)."]),
        Section(key="rules", title="Rules", state="DOCUMENTED", sources=SOURCES, items=[
            "Value: earnings yield, book-to-market, operating cash-flow yield - each only when its numerator is positive (a negative P/E is not cheap).",
            "Quality: return on assets, cash flow on assets, equity ratio (low leverage).",
            "Each factor is a cross-sectional percentile rank; value score = mean of the value ranks (>= 2 of 3 required), quality score likewise; "
            "composite = 0.5 x value + 0.5 x quality; the top 20 are held, equal weight."]),
        Section(key="data_requirements", title="Data requirements", state="DOCUMENTED", sources=SOURCES, items=[
            "SEC EDGAR XBRL companyfacts (net income, operating cash flow, assets, stockholders' equity, weighted-average share count) with filing dates; "
            "Alpaca IEX daily bars (raw, split-adjusted, all-adjusted).",
            "A fact is usable from the session after its filing date; per period the latest filing visible at the rebalance wins (restatements appear "
            "only once filed)."]),
        Section(key="entry_logic", title="Entry logic", state="DOCUMENTED", sources=SOURCES, items=[
            "Signal at the close of the first session of each month; a selected stock not already held is bought at the next session's open."]),
        Section(key="invalidation", title="Invalidation", state="DOCUMENTED", sources=SOURCES, items=[
            "No price stop: a holding is re-examined at every monthly rebalance; a stock that becomes ineligible or leaves the top 20 is sold."]),
        Section(key="exit_logic", title="Exit logic", state="DOCUMENTED", sources=SOURCES, items=[
            "A stock no longer selected at a rebalance is sold at the open after that rebalance; a stock still selected is simply held."]),
        Section(key="holding_horizon", title="Holding horizon", state="DOCUMENTED", sources=SOURCES, items=[
            "Position horizon: months to years (monthly re-examination; low turnover)."]),
        Section(key="backtest", title="Backtest", state="DOCUMENTED", sources=SOURCES, items=[
            "Historical replay through the Stage 6.2 / 6.3.1 harness: point-in-time SEC fundamentals, next-session-open entries, explicit friction, "
            "an independent mechanical audit and the integrity gate.",
            "The stored result (or Not yet evaluated) is the Validation block above; nothing is estimated here."]),
        Section(key="out_of_sample", title="Out-of-sample", state="DOCUMENTED", sources=SOURCES, items=[
            "Chronological 70 / 30 split with the Stage 6.3.1 boundary purge; the rules were frozen before the OOS slice was inspected.",
            "The OOS result is reported on its own in the Validation block above."]),
        Section(key="risk_notes", title="Risk notes", state="DOCUMENTED", sources=SOURCES, items=[
            "SURVIVORSHIP_BIAS_PRESENT: a fixed universe of today's large caps (no point-in-time constituents, no delisted stocks).",
            "Compare with the equal-weight universe control, not only with SPY: the universe itself carries the survivorship bias.",
            "Sector classification is today's SEC SIC (not point in time); volumes are IEX-only; some issuers report net income or cash flow under "
            "alternative XBRL concepts (recorded per value; ProfitLoss includes non-controlling interests)."]),
        Section(key="paper_results", title="Paper results", state="NOT_EVALUATED", note=f"{NOT_EVALUATED}: research only - no paper trading."),
        Section(key="decision_log", title="Decision log", state="NOT_EVALUATED",
                note=f"{NOT_EVALUATED}: no human decision is recorded. A validation PASS means eligible for human review, never a promotion."),
    ]
