"""strategy_registry/mean_reversion.py — the Strategy Lab detail sections of MEAN_REVERSION (Stage 6.6), documented from the frozen v1.0 rules in
mean_reversion/__init__.py. Results never live here: the Validation block (strategy_validation/service.py) shows the stored run - absolute
performance, the control comparison, the raw and final selection edge, the universe integrity and the research diagnostics. Research only: the
strategy never emits a trading signal and its lifecycle (RESEARCH) only changes by a human decision."""
from __future__ import annotations

from typing import List

from strategy_registry import NOT_EVALUATED
from strategy_registry.models import Section

LINKS = {"design_docs": "DESIGN_66_MEAN_REVERSION.md"}
SOURCES = ["mean_reversion/__init__.py", "mean_reversion/core.py", "DESIGN_66_MEAN_REVERSION.md"]


def sections() -> List[Section]:
    import mean_reversion as MR
    facts = {"strategy_version": MR.STRATEGY_VERSION, "lookback_sessions": MR.LOOKBACK_SESSIONS, "top_n": MR.TOP_N, "rebalance": MR.REBALANCE,
             "min_price": MR.MIN_PRICE, "min_median_dollar_volume": MR.MIN_MEDIAN_DOLLAR_VOLUME, "friction_bps_per_side": MR.FRICTION_BPS_PER_SIDE,
             "evaluation_mode": "CROSS_SECTIONAL_PORTFOLIO", "selection_control": "Equal-weight eligible universe", "market_benchmark": MR.BENCHMARK,
             "universe_method": "FIXED_CURRENT_UNIVERSE"}
    return [
        Section(key="overview", title="Overview", state="DOCUMENTED", sources=SOURCES, facts=facts, items=[
            "Long-only weekly rebalance of the 20 stocks with the most negative 5-session adjusted return (short-term reversal), equal weight, "
            "from adjusted daily bars.",
            "Lifecycle: Research. A validation verdict never changes it; it never produces a trading signal."]),
        Section(key="hypothesis", title="Hypothesis", state="DOCUMENTED", sources=SOURCES, items=[
            "Short-term relative underperformance may partially reverse over the following short holding period (cross-sectional short-term "
            "reversal): the stocks that fell most over the last week, relative to their peers, may rebound relative to them over the next week.",
            "Reversal only: no value, quality, momentum, market-timing, news, sentiment, earnings or model filter. The rules were fixed before any "
            "out-of-sample result existed."]),
        Section(key="rules", title="Rules", state="DOCUMENTED", sources=SOURCES, items=[
            "Signal: the adjusted return from the close 5 sessions before the rebalance to the rebalance session's close (data through that close only).",
            "Ranking: the lower the return, the higher the rank - the most negative recent performer ranks first (ties by symbol); the score is a "
            "rank, not a probability of a rebound.",
            "Portfolio: the 20 most negative, equal weight (5 % each), long only, no leverage, no short book, no stop."]),
        Section(key="data_requirements", title="Data requirements", state="DOCUMENTED", sources=SOURCES, items=[
            "All-adjusted daily bars (splits and dividends), so a split or an ex-dividend drop never registers as a loss; raw closes and volumes for "
            "the price and liquidity filters; the provider's raw / split-adjusted ratio for the window check.",
            "Eligibility: a raw close of at least 5 USD, a 20-session median dollar volume of at least 1M USD, both endpoint bars and a full 5-session "
            "history (never shortened, substituted, interpolated or set to zero), one listing per issuer, and a consistent signal window: a "
            "provider ratio artifact, an unabsorbed split or an adjusted loss the raw price did not have excludes the stock (flagged, never "
            "clipped). No fundamentals."]),
        Section(key="entry_logic", title="Entry logic", state="DOCUMENTED", sources=SOURCES, items=[
            "Signal at the close of the first session of each ISO week (a Monday holiday makes it the Tuesday); a selected stock not already held is "
            "bought at the next session's open."]),
        Section(key="invalidation", title="Invalidation", state="DOCUMENTED", sources=SOURCES, items=[
            "No price stop, no take-profit, no trailing stop: a holding is re-examined at every weekly rebalance; a stock that leaves the 20 most "
            "negative or becomes ineligible is sold."]),
        Section(key="exit_logic", title="Exit logic", state="DOCUMENTED", sources=SOURCES, items=[
            "A stock no longer selected at a rebalance is sold at the open after that rebalance; a stock still selected is simply held (no friction)."]),
        Section(key="holding_horizon", title="Holding horizon", state="DOCUMENTED", sources=SOURCES, items=[
            "Swing horizon: one weekly leg, from the open after a rebalance to the open after the next one."]),
        Section(key="backtest", title="Backtest", state="DOCUMENTED", sources=SOURCES, items=[
            "Historical replay through the validation harness (validation_v3, cross-sectional): next-session-open entries, explicit friction on real "
            "orders only, an independent audit that re-derives the weekly schedule, the eligible set, the returns, the selection, the entries, the "
            "exits and the weights, and the integrity gate.",
            "One week is one observation: the selection edge is the weekly excess over the equal-weight eligible universe (the selection control); "
            "SPY is a market benchmark only. The stored result (or Not yet evaluated) is the Validation block above; nothing is estimated here."]),
        Section(key="out_of_sample", title="Out-of-sample", state="DOCUMENTED", sources=SOURCES, items=[
            "Chronological 70 / 30 split of the same dataset as Value + Quality and Momentum, with the boundary purge; the rules were frozen before "
            "the OOS run.",
            "The OOS result is reported on its own in the Validation block above."]),
        Section(key="risk_notes", title="Risk notes", state="DOCUMENTED", sources=SOURCES, items=[
            "SURVIVORSHIP_BIAS_PRESENT: a fixed universe of today's large caps (FIXED_CURRENT_UNIVERSE, universe integrity UNTRUSTED). For a loser "
            "ranking it is not neutral: losers that later failed, were delisted or were bought cheaply are absent, so every loser here is one that "
            "survived - the final selection edge is capped at Not established whatever the raw statistics say.",
            "A recent loser can keep falling (a falling knife); the effect can be beta (losers rebound with the market); weekly turnover is high, so "
            "friction can consume a gross effect; a bad tick present in both price series looks like a real loser and is reported, not filtered.",
            "Volumes are IEX-only; sector labels are today's SEC SIC descriptions (not point in time)."]),
        Section(key="paper_results", title="Paper results", state="NOT_EVALUATED", note=f"{NOT_EVALUATED}: research only - no paper trading."),
        Section(key="decision_log", title="Decision log", state="NOT_EVALUATED",
                note=f"{NOT_EVALUATED}: no human decision is recorded. A validation PASS means eligible for human review, never a promotion."),
    ]
