"""strategy_registry/breakout.py — the Strategy Lab detail sections of BREAKOUT (Stage 6.7), documented from the frozen v1.0 rules in
breakout/__init__.py. Results never live here: the Validation block (strategy_validation/service.py) shows the stored run - absolute performance,
the control comparison, the raw and final selection edge, the universe integrity and the research diagnostics (exposure, follow-through, failed
breakouts, turnover, concentration, the Momentum overlap). Research only: the strategy never emits a trading signal and its lifecycle (RESEARCH)
only changes by a human decision."""
from __future__ import annotations

from typing import List

from strategy_registry import NOT_EVALUATED
from strategy_registry.models import Section

LINKS = {"design_docs": "DESIGN_67_BREAKOUT.md"}
SOURCES = ["breakout/__init__.py", "breakout/core.py", "DESIGN_67_BREAKOUT.md"]


def sections() -> List[Section]:
    import breakout as BO
    facts = {"strategy_version": BO.STRATEGY_VERSION, "lookback_sessions": BO.LOOKBACK_SESSIONS, "max_holdings": BO.MAX_HOLDINGS, "slot_pct": BO.SLOT_PCT,
             "cash_return": BO.CASH_RETURN, "rebalance": BO.REBALANCE, "min_price": BO.MIN_PRICE, "min_median_dollar_volume": BO.MIN_MEDIAN_DOLLAR_VOLUME,
             "friction_bps_per_side": BO.FRICTION_BPS_PER_SIDE, "evaluation_mode": "CROSS_SECTIONAL_PORTFOLIO", "selection_control": "Equal-weight eligible universe",
             "market_benchmark": BO.BENCHMARK, "universe_method": "FIXED_CURRENT_UNIVERSE"}
    return [
        Section(key="overview", title="Overview", state="DOCUMENTED", sources=SOURCES, facts=facts, items=[
            "Long-only weekly rebalance of up to 20 stocks whose adjusted close broke above the highest adjusted high of the 20 sessions before, the "
            "strongest breakouts first; each holding gets a fixed slot and the unused slots stay cash.",
            "Lifecycle: Research. A validation verdict never changes it; it never produces a trading signal."]),
        Section(key="hypothesis", title="Hypothesis", state="DOCUMENTED", sources=SOURCES, items=[
            "A stock that closes above its prior multi-week trading range may be experiencing persistent demand and can continue to outperform its "
            "peers over the following weeks (a cross-sectional price breakout).",
            "Breakout only: no value, quality, momentum composite, mean reversion, news, earnings, sentiment, market timing or model. The rules were "
            "fixed before any out-of-sample result existed."]),
        Section(key="rules", title="Rules", state="DOCUMENTED", sources=SOURCES, items=[
            "Breakout: the rebalance session's adjusted close above the highest adjusted high of the 20 completed sessions BEFORE it (the session's "
            "own high is never part of its reference).",
            "Ranking: only breakouts are candidates, the largest breakout strength (close / reference - 1) first, ties by symbol; the score is a "
            "rank, not a probability.",
            "Portfolio: at most 20 breakouts at a fixed allocation each; fewer breakouts leave the rest of the capital in cash (never scaled up); no "
            "breakout means all cash. Long only, no leverage, no stop."]),
        Section(key="data_requirements", title="Data requirements", state="DOCUMENTED", sources=SOURCES, items=[
            "All-adjusted daily bars (the adjusted high and close); raw closes and volumes for the price and liquidity filters; the provider's raw / "
            "split-adjusted ratio for the window check.",
            "Eligibility: a raw close of at least 5 USD, a 20-session median dollar volume of at least 1M USD, a bar on every one of the 20 reference "
            "sessions and the rebalance session (never filled), one listing per issuer, and a consistent breakout window: a provider ratio artifact, an "
            "unabsorbed split, an adjusted loss the raw price did not have or a large re-basing (a spin-off) inside the window excludes the stock "
            "(flagged, never clipped). No fundamentals."]),
        Section(key="entry_logic", title="Entry logic", state="DOCUMENTED", sources=SOURCES, items=[
            "Signal at the close of the first session of each ISO week (a Monday holiday makes it the Tuesday); a selected breakout not already held "
            "is bought at the next session's open."]),
        Section(key="invalidation", title="Invalidation", state="DOCUMENTED", sources=SOURCES, items=[
            "No price stop, no trailing stop, no take-profit, no moving-average exit: a holding is re-examined at every weekly rebalance."]),
        Section(key="exit_logic", title="Exit logic", state="DOCUMENTED", sources=SOURCES, items=[
            "A stock no longer among the selected breakouts is sold at the open after that rebalance; a stock still selected is simply held (no "
            "friction)."]),
        Section(key="holding_horizon", title="Holding horizon", state="DOCUMENTED", sources=SOURCES, items=[
            "Swing horizon: one weekly leg, from the open after a rebalance to the open after the next one, repeated while the stock keeps breaking out."]),
        Section(key="backtest", title="Backtest", state="DOCUMENTED", sources=SOURCES, items=[
            "Historical replay through the validation harness (validation_v3, cross-sectional, fixed-slot cash accounting): next-session-open entries, "
            "explicit friction on real orders only, an independent audit that re-derives the reference window, the breakouts, the selection, the "
            "fixed allocation, the entries and the exits, and the integrity gate.",
            "One week is one observation: the selection edge is the weekly excess over the fully invested equal-weight eligible universe (the "
            "selection control); SPY is a market benchmark only. The invested fraction is reported beside it - sitting in cash is not selection skill."]),
        Section(key="out_of_sample", title="Out-of-sample", state="DOCUMENTED", sources=SOURCES, items=[
            "Chronological 70 / 30 split of the same dataset as the earlier strategies, with the boundary purge; the rules were frozen before the OOS run.",
            "The OOS result is reported on its own in the Validation block above."]),
        Section(key="risk_notes", title="Risk notes", state="DOCUMENTED", sources=SOURCES, items=[
            "SURVIVORSHIP_BIAS_PRESENT: a fixed universe of today's large caps (FIXED_CURRENT_UNIVERSE, universe integrity UNTRUSTED): today's large "
            "caps are partly the stocks whose breakouts kept going - the final selection edge is capped at Not established whatever the raw "
            "statistics say.",
            "Breakouts can fail and reverse; exposure varies with the number of breakouts, so the strategy can hold much cash - a lower-volatility "
            "result from cash is not selection skill; breakouts may cluster in one sector during trends and overlap with momentum.",
            "Volumes are IEX-only; sector labels are today's SEC SIC descriptions (not point in time)."]),
        Section(key="paper_results", title="Paper results", state="NOT_EVALUATED", note=f"{NOT_EVALUATED}: research only - no paper trading."),
        Section(key="decision_log", title="Decision log", state="NOT_EVALUATED",
                note=f"{NOT_EVALUATED}: no human decision is recorded. A validation PASS means eligible for human review, never a promotion."),
    ]
