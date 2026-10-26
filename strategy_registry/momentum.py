"""strategy_registry/momentum.py — the Strategy Lab detail sections of MOMENTUM (Stage 6.5), documented from the frozen v1.0 rules in momentum/__init__.py.
Results never live here: the Validation block (strategy_validation/service.py) shows the stored run - absolute performance, the control comparison
and the selection edge. Research only: the strategy never emits a trading signal and its lifecycle (RESEARCH) only changes by a human decision."""
from __future__ import annotations

from typing import List

from strategy_registry import NOT_EVALUATED
from strategy_registry.models import Section

LINKS = {"design_docs": "DESIGN_65_MOMENTUM.md"}
SOURCES = ["momentum/__init__.py", "momentum/core.py", "DESIGN_65_MOMENTUM.md"]


def sections() -> List[Section]:
    import momentum as M
    facts = {"strategy_version": M.STRATEGY_VERSION, "lookback_sessions": M.LOOKBACK_SESSIONS, "skip_sessions": M.SKIP_SESSIONS, "top_n": M.TOP_N,
             "rebalance": M.REBALANCE, "min_price": M.MIN_PRICE, "min_median_dollar_volume": M.MIN_MEDIAN_DOLLAR_VOLUME, "friction_bps_per_side": M.FRICTION_BPS_PER_SIDE,
             "evaluation_mode": "CROSS_SECTIONAL_PORTFOLIO", "selection_control": "Equal-weight eligible universe", "market_benchmark": M.BENCHMARK}
    return [
        Section(key="overview", title="Overview", state="DOCUMENTED", sources=SOURCES, facts=facts, items=[
            "Long-only monthly rebalance of the 20 stocks with the strongest 12-1 price momentum, equal weight, from adjusted daily bars.",
            "Lifecycle: Research. A validation verdict never changes it; it never produces a trading signal."]),
        Section(key="hypothesis", title="Hypothesis", state="DOCUMENTED", sources=SOURCES, items=[
            "Stocks with stronger medium-term relative price performance may continue to outperform weaker peers over the following holding period "
            "(cross-sectional momentum); the most recent month is skipped because one-month returns tend to reverse.",
            "Momentum only: no value, quality, news, model or market-condition filter. The rules were fixed before any out-of-sample result existed."]),
        Section(key="rules", title="Rules", state="DOCUMENTED", sources=SOURCES, items=[
            "Signal: the adjusted return from the close 252 sessions before the rebalance to the close 21 sessions before it (12-1: the recent "
            "month never enters the signal).",
            "Ranking: the cross-sectional percentile rank of that return among the eligible stocks (ties by symbol); the score is a rank, not a probability.",
            "Portfolio: the top 20, equal weight (5 % each), long only, no leverage, no stop."]),
        Section(key="data_requirements", title="Data requirements", state="DOCUMENTED", sources=SOURCES, items=[
            "All-adjusted daily bars (splits and dividends: a total-return momentum); raw closes and volumes for the price and liquidity filters.",
            "Eligibility: a raw close of at least 5 USD, a 20-session median dollar volume of at least 1M USD, a full 252-session history with "
            "both endpoint bars (never shortened, padded or substituted) and one listing per issuer. No fundamentals."]),
        Section(key="entry_logic", title="Entry logic", state="DOCUMENTED", sources=SOURCES, items=[
            "Signal at the close of the first session of each month; a selected stock not already held is bought at the next session's open."]),
        Section(key="invalidation", title="Invalidation", state="DOCUMENTED", sources=SOURCES, items=[
            "No price stop: a holding is re-examined at every monthly rebalance; a stock that leaves the top 20 or becomes ineligible is sold."]),
        Section(key="exit_logic", title="Exit logic", state="DOCUMENTED", sources=SOURCES, items=[
            "A stock no longer selected at a rebalance is sold at the open after that rebalance; a stock still selected is simply held."]),
        Section(key="holding_horizon", title="Holding horizon", state="DOCUMENTED", sources=SOURCES, items=[
            "Position horizon: re-examined monthly; a stock is held for as long as it stays in the top 20."]),
        Section(key="backtest", title="Backtest", state="DOCUMENTED", sources=SOURCES, items=[
            "Historical replay through the validation harness (validation_v3, cross-sectional): next-session-open entries, explicit friction, an "
            "independent audit that re-derives the eligible set, the momentum and the selection, and the integrity gate.",
            "The selection edge is judged per rebalance period against the equal-weight eligible universe (the selection control); SPY is a market "
            "benchmark only. The stored result (or Not yet evaluated) is the Validation block above; nothing is estimated here."]),
        Section(key="out_of_sample", title="Out-of-sample", state="DOCUMENTED", sources=SOURCES, items=[
            "Chronological 70 / 30 split of the same dataset as Value + Quality, with the boundary purge; the rules were frozen before the OOS run.",
            "The OOS result is reported on its own in the Validation block above."]),
        Section(key="risk_notes", title="Risk notes", state="DOCUMENTED", sources=SOURCES, items=[
            "SURVIVORSHIP_BIAS_PRESENT: a fixed universe of today's large caps (no point-in-time constituents, no delisted stocks). For a price ranking "
            "it is NOT neutral between the strategy and the control: stocks whose momentum persisted (e.g. CLS, SNDK) are in, momentum winners that "
            "later collapsed are out - it likely flatters the selection edge, so even a positive edge here would not be evidence.",
            "Momentum can lose sharply when depressed markets rebound (momentum crashes) and tends to concentrate in recent winners (sectors and "
            "names); its volatility is higher than the control's.",
            "Volumes are IEX-only; sector labels are today's SEC SIC descriptions (not point in time)."]),
        Section(key="paper_results", title="Paper results", state="NOT_EVALUATED", note=f"{NOT_EVALUATED}: research only - no paper trading."),
        Section(key="decision_log", title="Decision log", state="NOT_EVALUATED",
                note=f"{NOT_EVALUATED}: no human decision is recorded. A validation PASS means eligible for human review, never a promotion."),
    ]
