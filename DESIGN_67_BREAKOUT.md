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

## 5. Results

Final stored run `d6e4e34d5bc8470aa49e6646a66b613f` (git `8fe585b`: the freeze plus the review's correctness and disclosure fixes; no rule
changed). It reproduces the first OOS run `dc45d3a761ab44d18dc0185d45e2fc16` (git `2eb5981`, the freeze commit) exactly - the same 2,005
candidates (0 skipped), candidate set `2a1f66f5c2024258d83fa6db0678cb45`, the same period table, edge and verdict; only the descriptive fields
the review fixed differ (section 6a). Integrity PASS, persistence ELIGIBLE; audit 4,000 / 4,000 trades validated, 0 strategy-specific
violations; control parity 8 / 8; cash accounting re-derived from the trades: 0 mismatches over 321 weeks; control membership: 0 mismatches; 5
boundary legs purged (one boundary week excluded); 52.2 periods a year; causality: one STATIC_FUTURE_INDEX WARN (the next rebalance's decision
sets when a leg ends - made before the exit fill: not look-ahead).

**Absolute performance (per leg, net) - a trade-level description, NOT SELECTION-EDGE EVIDENCE**

| | Legs | Gross | Slippage | Net | Mean R | t (per leg) | Win rate |
|---|---|---|---|---|---|---|---|
| TRAIN | 1,364 | 23,785 | 5,493 | 18,292 | 0.0027 | 2.34 | 54.0 % |
| OOS | 636 | 13,094 | 2,580 | 10,514 | 0.0034 | 1.44 | 53.9 % |
| All | 2,000 | 36,879 | 8,073 | 28,806 | 0.0029 | 2.68 | 54.0 % |

Absolute verdict: PASS (the validation_v2 policy on the legs).

**Control comparison (one observation per WEEK; the strategy on the fixed-slot basis, cash at 0 %)**

| | TRAIN (2020-08 → 2024-11) | OOS (2024-12 → 2026-10) | All |
|---|---|---|---|
| Weekly periods | 223 | 98 | 321 |
| Mean invested fraction | 30.6 % | 32.4 % | 31.2 % |
| Strategy | +18.8 % | +10.9 % | +31.7 % |
| SELECTION CONTROL (equal-weight eligible universe, fully invested) | +142.4 % | +47.8 % | +258.3 % |
| Mean excess per week (net / gross) | −0.34 % / −0.32 % | −0.31 % / −0.28 % | −0.33 % / −0.31 % |
| Excess t-statistic (per week) | −2.47 | **−1.59** | −2.94 |
| Selection part / cash part of the weekly excess | −0.033 % (t −0.79) / −0.31 % | +0.015 % (t 0.14) / −0.32 % | −0.018 % (t −0.43) / −0.31 % |
| Excess win rate | 39.5 % | 40.8 % | 39.9 % |
| Tracking (active) volatility, annualized | 15.0 % | 13.8 % | 14.7 % |
| Worst relative week | 2022-03-14: −8.6 % | 2025-04-21: −5.8 % | 2022-03-14: −8.6 % |
| Max relative drawdown | 51.0 % | 32.3 % | 64.0 % |
| Concentration | n/a (no positive summed excess) | n/a | n/a |
| Volatility strategy / control / SPY (annualized) | 7.2 % / 17.5 % / 15.9 % | 8.1 % / 14.0 % / 13.8 % | 7.5 % / 16.5 % / 15.3 % |
| Sharpe strategy / control / SPY (rf 0) | 0.60 / 1.27 / 1.00 | 0.72 / 1.56 / 1.04 | 0.64 / 1.34 / 1.01 |
| Max drawdown strategy / control / SPY | 9.6 % / 23.5 % / 24.4 % | 12.4 % / 14.8 % / 14.5 % | 12.4 % / 23.5 % / 24.4 % |
| Capital turnover per week strategy / control | 25.2 % / 1.3 % | 27.2 % / 0.05 % | 25.8 % / 0.9 % |
| MARKET BENCHMARK (SPY) | +86.9 % | +28.4 % | +140.0 % |

**Raw selection edge: NOT_ESTABLISHED** (`NO_OOS_EXCESS`: the OOS mean excess over the control is −0.31 % per week, t = −1.59 over 98 weeks -
not positive; the cash guard is not reached). **Universe integrity: UNTRUSTED** (the cap allows at most NOT_ESTABLISHED; not binding here).
**Final selection edge: NOT_ESTABLISHED. Overall verdict: FAIL** (not ready for paper). **Lifecycle: Research.**

Diagnostics (descriptive only):

* Exposure (cash is not selection skill): the invested fraction averaged 31 % (median 25 %, from 0 % to 100 %); weeks with 0 / 1-5 / 6-10 /
  11-19 / 20 holdings: 41 / 139 / 75 / 53 / 13 of 321 (OOS 9 / 41 / 27 / 18 / 3 of 98). Breakout candidates per week: mean 6.6, at most 47;
  more than 20 in 12 weeks. Beta to SPY: strategy 0.21, control 1.04.
* Where the excess comes from: the weekly excess splits exactly into a selection part (the invested fraction x the legs' excess over the
  control) and a cash part. The cash part is −0.31 % a week in every segment and carries almost all of the shortfall (the summed excess of
  −1.07 over 321 weeks: cash −1.01, names −0.06). The selection part is indistinguishable from zero: −0.033 % a week in TRAIN (t −0.79), +0.015 %
  in OOS (t 0.14). The unweighted mean over weeks with a position reads −0.009 % (TRAIN) and +0.31 % (OOS, t 0.76) - that measure counts a
  one-stock week like a twenty-stock week, which is why the guard does not use it (section 7, M1).
* Market weeks: the excess was −1.38 % a week in the 187 weeks SPY rose and +1.12 % in the 134 weeks it fell (OOS −1.17 % in 56 / +0.85 % in
  42): the signature of a low-exposure portfolio, not of selection. 2022 (a falling market) is the only year with a positive mean excess.
* Follow-through (each selected leg's gross return over its week): 41.2 % continued higher (> +1 %), 25.6 % flat, 33.3 % failed (< −1 %);
  relative to the control: 35.9 % above it by more than 1 %, 25.8 % within 1 %, 38.4 % below (mean +0.02 %).
* Failed breakouts: 44.5 % of the legs ended below their entry price; 35.0 % ended below their breakout reference.
* Momentum overlap (241 weeks with a full 12-1 history): 29.8 % of Breakout's holdings were also in the 12-1 Momentum portfolio at the same
  session, against 25.6 % expected by chance from the same eligible set; the weekly excess of the two selections correlates at 0.07. Breakout
  is close to independent of Momentum here.
* Turnover and friction: capital turnover 25.8 % a week (13.4x a year: entries / 20 slots); selection stability 17 % (most breakouts are not
  breaking out again a week later); friction costs 1.3 % a year; the gross weekly excess is already negative.
* Concentration: by SEC SIC major group, software led at 64 of 322 rebalances, semiconductors 62, pharmaceuticals 54 (mean top-group share
  45 %, max 100 % in thin weeks); the most held names were TSLA and NVDA (13 % of weeks each), KLAC, LRCX, MU, IBM. The largest name
  contributions: SNDK +0.038, MRVL +0.034, MU +0.029, TSLA +0.027, UNH +0.020; detractors REGN −0.021, COST −0.020, DHR −0.020, KLAC −0.018,
  NOW −0.016.
* Sensitivity (TRAIN view only; every row on the same 218 weeks): base t −2.38; lookback 10 −2.01; lookback 40 −2.34; 15 holdings −2.22; 25
  holdings −2.44. Every neighbour lags the fully invested control by about as much; nothing was chosen from it.
* Data quality: 30 DATA_INCONSISTENT exclusions - 25 LARGE_REBASING (the spin-off windows of T 2022-04, GE 2023-01 and 2024-04, DHR 2023-10,
  HON 2025-11, SPGI 2026-07) and 5 RATIO_ARTIFACT (HON at the five rebalances from 2026-06-29 to 2026-07-27, whose 20-session windows contain the
  provider artifact); 0 extreme (> +25 %) breakouts; 0 single-print spikes. One inconsistent pair inside a held window: HON's control
  position of the 2026-06-22 OOS week (+0.058 pp on that week's excess, `HOLDING_WINDOW_DATA_ANOMALY`). Five provider-adjusted spin-offs fall
  inside held control windows (GE twice, DHR, HON 2025, SPGI 2026): correct total return, listed apart with the excess at stake had the
  adjustment been wrong.
* Eligibility: 29-80 eligible stocks per rebalance (mean 76.7); exclusions (stock-rebalances): MISSING_LOOKBACK_BAR 553 (the feed gaps),
  NO_PRICE 327, SECOND_LISTING 322, LIQUIDITY_BELOW_MIN 162, DATA_INCONSISTENT 30, LIQUIDITY_HISTORY 4.

## 6. Interpretation and decision

Holding the week's breakouts - about six stocks on an average week, 31 % of the capital, the rest in cash - did not beat simply holding the
eligible universe over the same weeks: it lagged by about 0.3 % a week in both TRAIN (t −2.47) and OOS (t −1.59). That shortfall is
the cash: with a beta of 0.21 the strategy trailed in rising weeks and led in falling ones. The question the selection control asks -
did choosing the breakouts add value on the capital that was invested? - has no positive answer either: the selection part of the excess
is −0.03 % a week in TRAIN and +0.015 % in OOS, statistically zero, and the selected breakouts beat the control in their week about as often as
they fell behind it. Breakout is close to independent of Momentum here, but independence does not help a selection that adds nothing. Even a
positive raw edge would have been capped by the untrusted universe. **Decision: keep Breakout in Research.** Per the policy it is not eligible
for human review for paper trading; nothing is promoted and no rule is changed after this result.

## 6a. Development log (what was seen when)

1. Hypothesis and definition written (sections 1-2) before any code.
2. Implementation and tests on SYNTHETIC fixtures only. They exposed two validation gaps before any real return was seen, both fixed in the
   pre-registered v3 extension: an all-cash boundary week was not excluded on both sides (the parity audit failed it), and an all-cash run
   against a falling control read as an ESTABLISHED raw edge - hence the CASH_DRIVEN_EXCESS guard.
3. Feasibility on the calendar, the adjusted OHLC and the TRAIN VIEW only: eligibility and breakout counts, the feed gaps, and the
   adjusted-vs-raw gap distribution that bounds the large re-basing rule - no strategy or control return was computed on the real data.
4. All 8 CI groups and the frontend tests green; then the freeze: commit `2eb5981` ("freeze Breakout v1 rules before OOS", 09:48:50Z) - every
   rule, the cash basis, the guard, the friction, the sensitivity set and the diagnostic definitions.
5. First real run `dc45d3a7…` (started 09:48:55Z on the clean freeze commit; its record carries `2eb5981`): the result above. The OOS result was
   seen from here on.
6. Independent adversarial review (section 7). Its fixes - committed as `8fe585b` with this disclosure - changed no Breakout rule
   (`breakout/core.py`, `data.py`, `adapter.py` and `__init__.py` are byte-identical to the freeze, `test_G06`): the cash guard now reads the
   exact capital-weighted selection part (it can only lower an edge and does not change this run, whose raw edge stops at NO_OOS_EXCESS), the
   v3 strategy turnover uses the capital basis under fixed slots, and four diagnostics gained a baseline, a relative variant or a corrected
   label. Rerun `d6e4e34d…` (10:07Z, on `8fe585b`) reproduces the first run's candidate set, period table, edge and verdict exactly; it is the
   stored latest run.

## 7. Adversarial review

An independent reviewer read the code (without this document's conclusions), re-derived the stored run, cut the data after each rebalance and
inflated T's own highs ten-fold at nine real rebalances (nothing changed), and re-ran the earlier strategies' stored records under the new
harness code (byte-identical v3 blocks; the same gate results). Clean: look-ahead and self-inclusion, the 5 % large re-basing rule (a gap
between the adjusted and the raw return, never the return itself; dividends at most 2.59 % and spin-offs at least 5.77 % over the whole
dataset; it excludes the stock from both sides), cash accounting, the boundary rule, control fairness, SPY vs the control, survivorship
(declared and capped), friction and continuations, post-OOS tuning (the run was on the freeze commit), audit independence, the gate, broker
isolation and the earlier strategies. Findings:

| # | Severity | Area | Finding | Resolution |
|---|---|---|---|---|
| M1 | MEDIUM | Cash accounting | the CASH_DRIVEN_EXCESS guard read an UNWEIGHTED mean over weeks with a position: a synthetic run losing on its 20-stock up weeks and winning on its 1-stock down weeks passed it with an ESTABLISHED edge built from cash timing | fixed: the guard reads the exact capital-weighted selection part of every week's excess, which must be positive with t >= the existing negligible bar (1.0); only lowers; this run unchanged (NO_OOS_EXCESS); the counter-example is a test (C05) |
| L1 | LOW | Momentum overlap | no chance baseline: 30 % overlap looked like duplication | fixed: 25.6 % expected by chance from the same eligible set |
| L2 | LOW | Data quality | the held-window flag fired on provider-ADJUSTED spin-offs (correct total return) | fixed: listed apart, never flagged; only the HON artifact remains |
| L3 | LOW | Turnover | the v3 strategy turnover was entries / positions under fixed slots (not comparable with the control) | fixed: entries / slots (capital turnover) under fixed slots |
| L4 | LOW | Follow-through | the absolute leg return favours "continued higher" in a rising market | the pre-declared definition kept; a control-relative variant added |
| L5 | LOW | Data | IEX-only highs can sit below consolidated highs, making breakouts easier to trigger | disclosed in the Lab risk notes and section 8 |
| L6 | LOW | Process | the results section was still being written during the review (a documents test red) | completed |

## 8. Limitations

* **Survivor universe** (`FIXED_CURRENT_UNIVERSE`, UNTRUSTED): today's large caps are partly the stocks whose breakouts kept going (SNDK, a 2025
  spin-off, is the largest name contribution); breakouts that collapsed into delisting or a sale are absent. The final edge is capped regardless.
* **Low exposure against a fully invested control**: the strategy is 31 % invested on average, so the period excess is dominated by the cash
  part. The control was not exposure-matched (no matched-count control was pre-registered); the exact selection / cash split and the guard
  are how the cash is kept apart from the selection. A matched-exposure control would be a new methodology version.
* IEX-only bars: an IEX-only high can sit below the consolidated high (an easier breakout); IEX-only volume in the liquidity filter.
* Feed gaps: 553 stock-weeks are excluded by the complete-lookback rule around three provider feed-gap sessions (never filled).
* Unadjusted spin-offs (IBM 2021, MRK 2021, PFE 2020) read as real drops: they can only raise a later reference, never create a breakout.
* One market path of 321 weeks (98 OOS), dominated by a broad advance in which cash lags; weekly observations are treated as independent.
* No risk adjustment in the edge test; the strategy's lower volatility and beta are reported, not netted.
* Continuing holdings are re-sized to 5 % at every weekly leg without a friction charge (the Stage 6.4 convention, on both sides).
