# Stage 6.7 — Strategy #5: Breakout v1 (research only)

> **Backtest results are hypothetical.** PASS means eligible for human review - never a promotion, never a forecast, never a trading signal.
> The lifecycle stays **Research**; nothing in this stage reaches a broker.

## 1. Hypothesis (written before any code)

A stock that closes above its prior multi-week trading range may be experiencing persistent demand and can continue to outperform its peers over
the following weeks: a **cross-sectional price breakout**. The usual explanations are under-reaction to new information that reaches the price
gradually, and a breakout drawing in further buyers (trend followers, short covering); the counter-explanation is that most breakouts fail and
mean-revert, and that a breakout is simply a noisy recent-return signal.

What this stage tests is the breakout ITSELF: no value, quality, momentum composite, mean reversion, news, earnings, sentiment, regime timing or
discretionary confirmation, and no model. The validation_v3 question: **does selecting the stocks that just broke out beat holding the same
eligible universe over the same weeks?** Unlike the earlier strategies, Breakout only holds stocks that actually break out: on a quiet week it may
hold few or none, and the rest of its capital is cash. A result driven by sitting in cash (lower market exposure) is not selection skill, so
exposure is reported beside the edge.

## 2. Definition (v1, frozen before the OOS run)

* **Prices**: the all-adjusted daily bars (splits and dividends). The adjusted OHLC bars are internally consistent in the bundle (0 of 87,779
  TRAIN bars violate low <= open, close <= high), so the preferred convention is used: the prior adjusted HIGH against the current adjusted
  CLOSE. Raw closes and volumes feed only the price and liquidity filters and the data-integrity check; raw and adjusted fields are never mixed in
  the signal.
* **Reference** at rebalance session T (index i in the dataset's session calendar): `breakout_reference = max(adjusted_high[i-20 .. i-1])` -
  the 20 completed sessions BEFORE T. T itself is never in its own reference (its high cannot raise its own threshold).
* **Breakout**: `adjusted_close[T] > breakout_reference` (strictly). **Strength**: `adjusted_close[T] / breakout_reference - 1` (> 0).
* **Eligibility** at T: a raw close on T >= 5 USD; a 20-session median raw dollar volume (close x volume, IEX) >= 1M USD; a bar on EVERY one of
  the 20 reference sessions and on T (a complete lookback: never shortened, never the nearest other bar, never filled); positive adjusted
  prices; one listing per issuer; and a **consistent breakout window** (below). Every exclusion is counted by reason.
* **Breakout-window data integrity** (the Stage 6.6 corporate-action contract, applied to the 20 consecutive bar pairs from session i-20 to T,
  plus one Breakout-specific rule - never a return cap):
  * a move of the provider's raw / split-adjusted ratio (> 1 %) must be a REAL split absorbed by the adjusted series (the raw price jumps > 25 %,
    the split-adjusted price does not, adjusted vs split-adjusted day return within 2 %); a RATIO ARTIFACT, an INCONSISTENT move or an UNABSORBED
    split makes the window inconsistent;
  * without a ratio move, the adjusted day return may not be more than 1 % BELOW the raw one (no corporate action explains an adjusted loss the
    raw price did not have);
  * **large re-basing**: without a ratio move, an adjusted day return more than 5 % ABOVE the raw one is a large distribution (a spin-off) the
    provider has re-based the history for. A breakout is a LEVEL comparison, so a reference that straddles such a re-basing depends entirely on
    an adjustment factor the bundle cannot verify: the window is excluded. (On the TRAIN view regular and special dividends never exceed 2.6 %;
    only four spin-offs exceed 5 % - T 2022, GE 2023 and 2024, DHR 2023 - a data-separation bound measured before any return was computed.)
  * a missing raw close or ratio on a window session makes the window unverifiable.
  An inconsistent window excludes the stock at that rebalance (`DATA_INCONSISTENT`, with the offending pair recorded) - flagged and excluded,
  never clipped into a usable breakout.
* **Candidates and ranking**: only stocks that break out are candidates (`breakout_candidate_count`); they are ranked by strength (largest
  first; ties by symbol ascending); the score is the percentile of the strength among the candidates - a RANK, not a probability.
* **Portfolio**: at most 20 holdings (the 20 strongest breakouts; all of them when fewer qualify). **Fixed allocation: 5 % of the fixed
  hypothetical equity per selected stock; the unused slots stay CASH at 0 % return** - 7 breakouts mean 35 % invested and 65 % cash, never 7
  positions scaled to 14.3 %; no breakout means 100 % cash. No leverage, no short book, no stop, no trailing stop, no take-profit.
* **Rebalance**: weekly. A week is an ISO week; signal at the close of the FIRST session of each ISO week that has one (a Monday holiday makes it
  the Tuesday; a week across a year end is one week), from the 21st session of the dataset on. A newly selected stock is bought at the next
  session's open; every holding is one weekly LEG that ends at the open of the first session after the next rebalance. A stock still selected at
  the next rebalance is simply held (a continuation: no order, no friction); one no longer selected is sold at the open after that rebalance.
  The last rebalance's legs run to the data end.
* **Friction**: 5 bps of slippage per side on real orders only; no commission.
* **Universe**: the same fixed 81-stock research universe as the earlier strategies (today's large caps): `FIXED_CURRENT_UNIVERSE`,
  `survivorship_bias: PRESENT` (`SURVIVORSHIP_BIAS_PRESENT`) -> universe integrity **UNTRUSTED** under universe_integrity_v1: the final selection
  edge can never be ESTABLISHED on this universe; the raw edge is still computed and shown.
* **Evaluation**: validation_v3 `CROSS_SECTIONAL_PORTFOLIO` against the SELECTION CONTROL "equal-weight eligible universe" (every eligible stock
  at the rebalance, fully invested, the same weekly sessions, timestamps, friction convention and total-capital basis, without the breakout
  selection); SPY is the MARKET BENCHMARK only. One week is one observation. **Cash accounting** (a pre-registered, opt-in extension of the v3
  period table, declared by the adapter as `strategy_capital = FIXED_SLOTS_CASH, 20 slots of 5 %`): a week's strategy return is the sum of its
  legs' returns divided by 20 (the cash earns 0); a week with no breakout is INCLUDED as 100 % cash (never dropped); the gate re-derives every
  week's strategy side from the stored trades on that basis and requires the recorded basis to be the adapter's own. Strategies that do not
  declare it are computed exactly as before. The v3 thresholds and the universe_integrity_v1 cap are used unchanged.
* **Sensitivity (pre-declared, diagnostic only, TRAIN only)**: lookback 10 and 40 sessions; maximum holdings 15 and 25 (each slot 100 / N % of
  the equity, so 25 slots never lever). Computed on the TRAIN view (nothing after the TRAIN end), every row on the same weeks; never used to choose.
* **Pre-declared diagnostics (never a gate)**: exposure (invested fraction; weeks with 0 / 1-5 / 6-10 / 11-19 / 20 positions); market
  sensitivity (the strategy's and the control's beta to SPY; SPY-up vs SPY-down weeks); the **invested-capital excess** (per week with a
  position: the mean of the selected legs minus the control - the selection effect on the capital actually invested, which removes the cash
  effect); follow-through (a selected leg's gross return above +1 % = continued higher, within +-1 % = flat, below -1 % = failed); failed
  breakouts (the leg ends below its entry price; the exit price ends below the breakout reference); turnover (entries / 20 slots a week, and
  annualized); concentration (sector, names, the five largest name contributions, `EXCESS_CONCENTRATED_IN_FEW_NAMES` when they carry at least
  half of a positive summed excess); the overlap with the 12-1 Momentum ranking at the same session; data quality.

## 3. Feasibility (checked before the freeze: the calendar and the TRAIN view only, no return of the strategy or the control)

**FEASIBLE on the existing data bundle** (no new data, no network): the Stage 6.4 bundle `sha256:e072e67517615fbb8cc42ebb` (Alpaca IEX daily
bars, raw / split-adjusted / all-adjusted OHLC, 2020-07-27 → 2026-10-23, 1,570 sessions, the 81 universe stocks + SPY). Breakout uses only its
bars, raw series, split ratios and issuer map - no fundamentals.

* Calendar: the same 322 weekly rebalances as Mean Reversion (the first on 2020-08-24, the 21st session; the last on 2026-10-19): 224 on or
  before the TRAIN end (2024-12-05) and 98 after it; 33 fall on a Tuesday (a Monday holiday); one decision per ISO week.
* Adjusted OHLC: 0 of 87,779 TRAIN bars violate low <= open, close <= high (the bundle already removed the inconsistent bars); the median high
  is 0.76 % above the close, the 99th percentile 5.2 % - the adjusted high is usable as the reference field.
* Eligibility on the TRAIN view (224 rebalances): 29-79 eligible stocks (mean 75.3). Exclusions (stock-rebalances): SECOND_LISTING 224 (GOOGL),
  NO_PRICE 317 (SNDK before its listing; feed gaps), LIQUIDITY_BELOW_MIN 162 (IEX-only volume), **MISSING_LOOKBACK_BAR 553** (the complete
  20-session lookback: the three provider feed-gap sessions - 2021-04-19, 2021-10-25, 2022-03-08, when 30-37 of the 81 stocks have a bar -
  exclude the missing stocks for the four weeks their reference window contains the gap; never filled), DATA_INCONSISTENT 17 (all
  LARGE_REBASING: the spin-off windows of T 2022-04, GE 2023-01 and 2024-04, DHR 2023-10).
* Breakout candidates on the TRAIN view: median 4 a week (mean 6.4, max 32); 0 in 32 of the 224 weeks; more than 20 in 9. The portfolio would
  hold 6.1 stocks on average - about 31 % invested, 69 % cash (weeks with 0 / 1-5 / 6-10 / 11-19 / 20 holdings: 32 / 99 / 48 / 35 / 10). Against
  a fully invested control, cash will dominate the period excess: the exposure and the invested-capital diagnostics are therefore part of the
  reading, and the cash-driven-excess guard keeps a cash result from reading as selection.
* Corporate actions: the all-adjusted series absorbs every real split (the Stage 6.5 audit); the known OOS provider artifact (HON 2026-06-29) is
  excluded by the window rule wherever it falls inside a breakout window; three spin-offs the provider did NOT adjust (IBM 2021, MRK 2021, PFE
  2020, Stage 6.6 review) read as real price drops - they can only lower a later reference, never create a breakout above the pre-drop range.

## 4. Validation setup

* Harness: validation_v3, `CROSS_SECTIONAL_PORTFOLIO`, with the pre-registered `FIXED_SLOTS_CASH` capital basis (20 slots of 5 %, cash at 0 %);
  TRAIN 2020-07-27 → 2024-12-05, OOS 2024-12-06 → 2026-10-23; the Stage 6.3.1 boundary purge, and under fixed slots an all-cash week whose
  control crosses the TRAIN end is excluded on both sides too.
* SELECTION CONTROL: the equal-weight eligible universe, fully invested, at every weekly rebalance (the same snapshot, sessions, friction
  convention and total capital); its membership is pinned in every candidate (`eligible_count`, `eligible_set_sha`). MARKET BENCHMARK: SPY,
  context only. The control is not adjusted to the strategy's exposure (no matched-count control is used); the invested fraction and the
  invested-capital excess are reported beside the edge.
* **Cash-driven-excess guard** (pre-registered, fixed-slot runs only, can only lower the edge): a positive OOS period excess counts as a
  selection edge only if the selection's excess on the capital it actually invested (per week with a position: the mean of its legs minus the
  control) is positive too; otherwise the edge is NOT_ESTABLISHED (`CASH_DRIVEN_EXCESS`). The gate re-derives it from the recorded period table.
* Universe integrity: `FIXED_CURRENT_UNIVERSE` -> UNTRUSTED; the raw v3 edge is kept, the final edge is capped at NOT_ESTABLISHED.
* Arms: RULES; GATED = RULES (no confirmation gate invented); MODEL NOT_EVALUATED (no model, no LLM).
* Independent audit (`breakout/audit.py`): from the raw data and the frozen constants it re-derives the weekly calendar, every reference window
  (ending BEFORE T), the breakout events and strengths, the eligible set (with the window-integrity rules) and its identity, the candidate
  counts, the 20 strongest at every rebalance, the fixed 5 % allocation, the next-open entries, the exits, the transaction flags, the hand-overs
  and every split; it never imports the strategy's code. The gate re-derives every week's strategy return from the stored trades on the fixed-slot
  basis and requires the recorded basis to be the adapter's own.
* Diagnostics (`breakout/diagnostics.py`, never a gate): exposure, market sensitivity, the invested-capital excess, absolute performance,
  friction and turnover, follow-through, failed breakouts, concentration (with the cash part of the excess), the Momentum overlap, data quality,
  the control-membership and cash-accounting re-checks, and the pre-declared sensitivity on the TRAIN view only.
