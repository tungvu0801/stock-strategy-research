# Stage 6.5 — Strategy #3: Momentum v1 (research only)

> **Backtest results are hypothetical.** PASS means eligible for human review - never a promotion, never a forecast, never a trading signal.
> The lifecycle stays **Research**; nothing in this stage reaches a broker.

## 1. Hypothesis (written before any code)

Stocks with stronger medium-term relative price performance tend to continue to outperform weaker peers over the following month or months:
cross-sectional (relative) momentum, the conventional 12-1 formulation (Jegadeesh & Titman 1993; the "UMD" factor). The most recent month is
skipped because one-month returns tend to REVERSE (short-term reversal, partly bid-ask and liquidity effects) - including it would mix two
opposite effects. Economic explanations offered in the literature: investors under-react to firm-specific news that is incorporated gradually,
and later over-react (herding, positive-feedback trading); momentum is also a compensation for crash risk (momentum portfolios lose sharply
when depressed markets rebound).

What this stage tests is momentum ITSELF: no value, no quality, no news, no model, no market-condition filter. The question is the validation_v3
one: **does ranking a fixed universe by 12-1 momentum select stocks that beat the same universe held equally?** - not whether a long-only
portfolio rose with the market.

## 2. Definition (v1, frozen before the OOS run)

* **Signal**: at rebalance session T (index i in the dataset's session calendar),
  `momentum = adjusted_close[i - 21] / adjusted_close[i - 252] - 1` - about 12 months back to about 1 month back (252 and 21 trading sessions).
  The 21 most recent sessions (the skipped month, and T itself) never enter the signal: 12-1, not 12-0.
* **Prices**: the all-adjusted daily bars (splits AND dividends: a total-return momentum, the conventional CRSP-style definition). A ratio of two
  adjusted closes is unaffected by any later corporate action (a later split or dividend rescales both endpoints by the same factor), so it is
  point in time. Price and liquidity filters use RAW closes and volumes.
* **Eligibility** at T: a raw close on T >= 5 USD; the 20-session median raw dollar volume (close x volume, IEX) >= 1M USD; a bar on BOTH
  lookback endpoint sessions and the symbol's history starting on / before the 252-session endpoint (never a shortened lookback, never padded,
  never a substituted market return); one listing per issuer (GOOG kept, GOOGL a second listing). Every exclusion is counted by reason.
* **Ranking**: the cross-sectional percentile rank of momentum among the eligible stocks (average rank for ties; highest return = 1.0); the
  score is 100 x the percentile - a RANK, not a probability. Ties are broken by symbol (ascending).
* **Portfolio**: the top 20 (fixed before OOS), long only, equal weight (5 % of the fixed hypothetical equity each), no leverage, no stop.
* **Rebalance**: monthly. Signal at the close of the FIRST session of each month (from the 253rd session of the dataset on, so that the full
  lookback exists); a newly selected stock is bought at the next session's open; every holding is one monthly LEG that ends at the open of the
  first session after the next rebalance (the Value + Quality convention - entry_trade / exit_trade flags: only real orders pay friction).
* **Friction**: 5 bps of slippage per side on real orders; no commission.
* **Universe**: the same fixed 81-stock research universe as Value + Quality (today's large caps) -> `SURVIVORSHIP_BIAS_PRESENT`. Unlike Value +
  Quality, financial and real-estate stocks are NOT excluded (momentum needs no accounting comparability).
* **Evaluation**: validation_v3 `CROSS_SECTIONAL_PORTFOLIO` against the SELECTION CONTROL "equal-weight eligible universe" (the same eligible
  set, sessions, friction and capital basis, without the momentum ranking); SPY is the MARKET BENCHMARK only. The selection-edge policy is the
  one pre-registered in Stage 6.4.1 (`17e2cb4`); nothing is changed for Momentum.
* **Sensitivity (pre-declared, diagnostic only, TRAIN only)**: lookback 9-1 (189 sessions) and 15-1 (315 sessions); top N 15 and 25. Computed
  on the TRAIN view of the dataset (nothing after the TRAIN end) and never used to choose anything.
