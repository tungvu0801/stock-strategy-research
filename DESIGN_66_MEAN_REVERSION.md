# Stage 6.6 — Strategy #4: Mean Reversion v1 (research only)

> **Backtest results are hypothetical.** PASS means eligible for human review - never a promotion, never a forecast, never a trading signal.
> The lifecycle stays **Research**; nothing in this stage reaches a broker.

## 1. Hypothesis (written before any code)

Short-term relative underperformance may partially reverse over the following short holding period: **cross-sectional short-term reversal**.
Stocks that fell most over the last week, relative to their peers, may rebound relative to those peers over the next week. The literature
(Jegadeesh 1990; Lehmann 1990; Lo & MacKinlay 1990) attributes short-horizon reversal partly to liquidity provision (a temporary price
concession to impatient sellers is paid back), partly to over-reaction to firm-specific news, and partly to microstructure (bid-ask bounce) - the
last of which a daily-close signal with next-open entries does not capture. It is the effect the 12-1 Momentum signal (Stage 6.5) deliberately
skips: one-month and shorter returns tend to reverse.

What this stage tests is reversal ITSELF: no value, quality, momentum, market-timing, news, sentiment, earnings or model filter, and no
discretionary gate. The question is the validation_v3 one: **does buying a fixed universe's recent losers beat simply holding the same eligible
universe over the same weekly periods?** - not whether a long-only portfolio rose with the market, and not whether it beat SPY.

Known failure modes, stated in advance: a recent loser may keep falling (a "falling knife": the drop reflects news, not a temporary concession);
the effect may be beta (losers rebound in up-markets with the market); weekly turnover is high, so friction may consume a gross effect; a bad
tick or an inconsistent price adjustment looks exactly like a large loser, and the strategy selects large losers on purpose.

## 2. Definition (v1, frozen before the OOS run)

* **Signal**: at rebalance session T (index i in the dataset's session calendar),
  `recent_return = adjusted_close[T] / adjusted_close[i - 5] - 1` - the adjusted return over the last 5 sessions, from the close 5 sessions
  before T to T's close. Data through T's close only.
* **Direction**: the LOWER the recent return, the HIGHER the Mean Reversion rank. The most negative recent performer ranks first. (The opposite of
  Momentum: tested explicitly.)
* **Prices**: the all-adjusted daily bars (splits and dividends), so a split or an ex-dividend drop never registers as a loss; the price and
  liquidity filters use RAW closes and volumes.
* **Eligibility** at T: a raw close on T >= 5 USD; a 20-session median raw dollar volume (close x volume, IEX) >= 1M USD; a bar on BOTH endpoint
  sessions (i - 5 and T) and the symbol's history starting on / before the start endpoint (never a shortened lookback, never the nearest other
  bar, never interpolated, never a zero return); positive adjusted endpoint prices; one listing per issuer; and a **consistent signal window**
  (below). Every exclusion is counted by reason.
* **Signal-window data integrity** (the existing corporate-action contract, applied to the 5 session pairs of the window - never a return cap):
  for every pair of consecutive bars of the stock inside [i - 5, T]
  * when the provider's raw / split-adjusted ratio moves (> 1 %), the move is classified exactly as the Stage 6.5 split audit does: a REAL split
    (the raw price jumps > 25 %, the split-adjusted price does not) must be absorbed by the adjusted series (adjusted vs split-adjusted day
    return within 2 %); a RATIO ARTIFACT (the ratio moves, the raw price does not - the HON 2026-06-29 type) or an INCONSISTENT move (both jump)
    makes the window inconsistent;
  * when the ratio does not move, the adjusted day return may not be more than 1 % BELOW the raw day return: a dividend or a spin-off can only
    raise the adjusted return relative to raw (the distribution is added back), so an adjusted loss the raw price did not have has no corporate
    action to explain it. (On the TRAIN view the adjusted return is never more than 0.072 % below raw over 87,684 day-pairs; the tolerance is a
    data-consistency bound, measured before any return of the strategy was computed.)
  * a missing raw close or ratio on a window session makes the window unverifiable.
  An inconsistent window excludes the stock at that rebalance (`DATA_INCONSISTENT`, with the offending pair recorded). Bars with a non-positive
  or inconsistent OHLC field were already removed when the bundle was built (a gap: then the endpoint rule applies). A bad tick present
  identically in the raw AND adjusted series is indistinguishable from a real move: it is not filtered (no arbitrary cap), only reported.
* **Ranking**: the cross-sectional percentile rank of the NEGATED recent return among the eligible stocks (most negative return = 1.0; average
  rank for ties); the score is 100 x the percentile - a RANK, not a probability of a rebound. Ties are broken by symbol (ascending).
* **Portfolio**: the 20 most negative (fixed before OOS), long only, equal weight (5 % of the fixed hypothetical equity each), no leverage, no
  short book, no stop, no take-profit, no trailing stop.
* **Rebalance**: weekly. A week is an ISO week (Monday to Sunday). Signal at the close of the FIRST session of each ISO week that has a session
  (a Monday holiday makes it the Tuesday; a week across a year end - e.g. 2024-12-30 to 2025-01-03 - is one week), from the 21st session of the
  dataset on (the 20-session liquidity history). A newly selected stock is bought at the next session's open; every holding is one weekly LEG that
  ends at the open of the first session after the next rebalance (the Value + Quality / Momentum convention; entry_trade / exit_trade flags: a
  continuation is not an order and pays no friction). The last rebalance's legs run to the data end.
* **Friction**: 5 bps of slippage per side on real orders only; no commission.
* **Universe**: the same fixed 81-stock research universe as Value + Quality and Momentum (today's large caps): `FIXED_CURRENT_UNIVERSE`,
  `survivorship_bias: PRESENT` -> universe integrity **UNTRUSTED** under universe_integrity_v1, so the final selection edge can never be
  ESTABLISHED on this universe; the raw edge is still computed and shown.
* **Evaluation**: validation_v3 `CROSS_SECTIONAL_PORTFOLIO` against the SELECTION CONTROL "equal-weight eligible universe" (the same eligible set,
  weekly sessions, entry and exit timestamps, friction convention and capital basis, without the reversal ranking); SPY is the MARKET BENCHMARK
  only. One week is one observation. The v3 thresholds and the universe_integrity_v1 cap are used unchanged.
* **Sensitivity (pre-declared, diagnostic only, TRAIN only)**: lookback 3 and 10 sessions; top N 15 and 25. Computed on the TRAIN view of the
  dataset (nothing after the TRAIN end) and never used to choose anything.
* **Disclosure thresholds (diagnostic flags, never a gate)**: `EXCESS_CONCENTRATED_IN_FEW_NAMES` when the five largest name contributions carry
  at least half of a positive summed period excess; `FRICTION_CONSUMES_GROSS_EXCESS` when a positive gross mean excess is zero or negative net.

## 3. Feasibility (checked before the freeze: the calendar and the TRAIN view only, no return of the strategy or the control)

**FEASIBLE on the existing data bundle** (no new data, no network): the Stage 6.4 bundle `sha256:e072e67517615fbb8cc42ebb` (Alpaca IEX daily
bars, raw / split-adjusted / all-adjusted, 2020-07-27 → 2026-10-23, 1,570 sessions, the 81 universe stocks + SPY; built offline from
sha256-recorded responses). Mean Reversion uses only its bars, raw series, split ratios and issuer map - no fundamentals.

* Calendar: 322 weekly rebalances, the first on 2020-08-24 (the 21st session: the 20-session liquidity history), the last on 2026-10-19; 224 on
  or before the TRAIN end (2024-12-05, the shared 70 / 30 split of this dataset) and 98 after it - far above the v3 minimums (12 OOS, 24 total).
  33 rebalances fall on a Tuesday (a Monday holiday); every ISO week with a session has exactly one decision.
* Eligibility on the TRAIN view (224 rebalances): mean 77.5 eligible stocks, range 29-79. Exclusions (stock-rebalances): SECOND_LISTING 224
  (GOOGL, a second listing of GOOG), NO_PRICE 317 (SNDK before its 2025 listing, and provider feed gaps), LIQUIDITY_BELOW_MIN 162 (IEX-only
  volume), MISSING_ENDPOINT_BAR 91, DATA_INCONSISTENT 0.
* Feed gaps: on 2021-04-19 only 30 of the 81 universe stocks have a bar, on 2021-10-25 only 37 (and 33 on 2022-03-08, not a rebalance). Both
  are rebalance sessions, so 29 and 36 stocks are eligible there (no bar on T), and again the week after (no bar on the start endpoint). The rule
  excludes and never fills; the selection control holds the same reduced eligible set, so the comparison stays like for like - but those
  weeks' 20 picks come from a much smaller universe.
* Corporate actions: the all-adjusted series absorbs every real split (the Stage 6.5 audit: 20 splits to within 0.01 %); on the TRAIN view the
  adjusted day return is never more than 0.072 % below the raw one (87,684 day-pairs without a ratio move), while spin-offs and special
  dividends lift it above raw (GE 2023 / 2024, T 2022, DHR 2023, COST 2020) - total return, not a loss. The one known provider artifact is in
  the OOS period (HON 2026-06-29, Stage 6.5): the window rule excludes HON from the rebalances whose 5-session window contains it.

## 4. Validation setup

* Harness: validation_v3, `CROSS_SECTIONAL_PORTFOLIO`; TRAIN 2020-07-27 → 2024-12-05, OOS 2024-12-06 → 2026-10-23; the Stage 6.3.1 boundary
  purge (the TRAIN legs whose outcome needs OOS bars are purged; the boundary week is excluded on both sides).
* SELECTION CONTROL: the equal-weight eligible universe at every weekly rebalance (the same snapshot, entry and exit sessions, friction
  convention and capital basis); its membership is pinned in every candidate (`eligible_count`, `eligible_set_sha`). MARKET BENCHMARK: SPY,
  context only. The question asked is whether selecting recent losers beat holding the eligible universe over the same weeks.
* Universe integrity: `FIXED_CURRENT_UNIVERSE` -> UNTRUSTED; the raw v3 edge is computed and kept, the final edge is capped at NOT_ESTABLISHED
  (`UNIVERSE_INTEGRITY_CAP`).
* Arms: RULES; GATED = RULES (no gate invented); MODEL NOT_EVALUATED (no model, no LLM).
* Independent audit (`mean_reversion/audit.py`): from the raw data and the frozen constants it re-derives the weekly calendar, the 5-session
  endpoints and returns, the eligible set (with the window-integrity rule) and its identity, the ranking and the 20 most negative at every
  rebalance, the entry at the next open and the exit at the open after the next rebalance on every simulated trade, the equal weight, the
  transaction flags, the hand-overs and every split of every stock. It never imports the strategy's code.
* Diagnostics (`mean_reversion/diagnostics.py`, never a gate): absolute performance per segment (Sharpe, volatility, drawdown), friction and
  turnover, concentration (sector, names, weeks), the falling-knife distribution, SPY up / down weeks, the overlap with the 12-1 Momentum
  ranking, data quality, the control-membership re-check, and the pre-declared sensitivity on the TRAIN view only.
