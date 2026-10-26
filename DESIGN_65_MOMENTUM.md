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
  Quality, financial and real-estate stocks are NOT excluded (momentum needs no accounting comparability). (The adversarial review added: for a
  price ranking this bias is not neutral between the strategy and the control - see section 8.)
* **Evaluation**: validation_v3 `CROSS_SECTIONAL_PORTFOLIO` against the SELECTION CONTROL "equal-weight eligible universe" (the same eligible
  set, sessions, friction and capital basis, without the momentum ranking); SPY is the MARKET BENCHMARK only. The selection-edge policy is the
  one pre-registered in Stage 6.4.1 (`17e2cb4`); nothing is changed for Momentum.
* **Sensitivity (pre-declared, diagnostic only, TRAIN only)**: lookback 9-1 (189 sessions) and 15-1 (315 sessions); top N 15 and 25. Computed
  on the TRAIN view of the dataset (nothing after the TRAIN end) and never used to choose anything.

## 3. Feasibility

**FEASIBLE on the existing data bundle** (no new data, no network): the Stage 6.4 bundle `sha256:e072e67517615fbb8cc42ebb` (Alpaca IEX daily
bars, raw / split-adjusted / all-adjusted, 2020-07-27 → 2026-10-23, 1,570 sessions; built offline from sha256-recorded responses). Momentum
uses only its bars, raw series and issuer map - no fundamentals.

* Lookback: 252 sessions exist from 2021-07-27, so the first rebalance is **2021-08-02**; the shared 70 / 30 split (the same dataset as Value +
  Quality, so the same TRAIN end 2024-12-05) leaves 41 TRAIN rebalances (40 periods + the boundary) and 22 OOS periods - above the v3 minimums
  (12 OOS, 24 total).
* Coverage (checked on the TRAIN view before the freeze, no return computed): 78-79 eligible stocks per rebalance; SNDK has no bar before its
  2025-02 listing (NO_PRICE, then INSUFFICIENT_HISTORY for its first 252 sessions); GOOGL is a second listing of GOOG; 28 stock-rebalances fall
  below the IEX liquidity floor (all CLS); no lookback endpoint bar was missing. 50 symbols miss 1-4 sessions over the whole bundle (data-feed
  gaps): an endpoint on such a session would exclude the stock for that rebalance (MISSING_ENDPOINT_BAR), never fill it.
* Corporate actions: 21 split-ratio changes across 18 symbols are absorbed by the adjusted series (a ratio of two adjusted closes is unaffected
  by any later split or dividend); spin-offs (AT&T / WarnerMedia 2022, GE / GE HealthCare 2023, GE Vernova 2024) appear in the all-adjusted series
  as total return. QQQ is not in the bundle and is not reported (no new data was fetched).

## 4. Validation setup

* Harness: validation_v3, `CROSS_SECTIONAL_PORTFOLIO`; TRAIN 2020-07-27 → 2024-12-05, OOS 2024-12-06 → 2026-10-23 (471 sessions); boundary purge.
* SELECTION CONTROL: the equal-weight eligible universe (the same snapshot of 78-80 stocks per rebalance, the same sessions, the same friction
  convention, equal capital). MARKET BENCHMARK: SPY, context only.
* Arms: RULES; GATED = RULES (no gate invented); MODEL NOT_EVALUATED.
* Independent audit (`momentum/audit.py`): it re-derives, from the raw data and the frozen constants, the whole eligible set, the momentum of every
  stock and the top 20 at every rebalance - plus the endpoints (12-1), the absence of future data, split continuity, the transaction flags, the
  exits and the hand-overs. It never imports the core.
* Diagnostics (`momentum/diagnostics.py`, never a gate): calendar, market months, friction, turnover, concentration, and the pre-declared
  sensitivity computed on the TRAIN view only.

## 5. Results

Run `52922e3f66234c489d6816f3243bd139` (git `26891c5`; the same candidate set and period results as run `baaa5cff…` at `3b8ead2`, which
predates the review fixes to the audit and the diagnostics): 1,260 candidates (0 skipped), candidate set `1d4b0d0e9ac8a70c2a661120605450b8`;
integrity PASS, persistence ELIGIBLE; audit 2,480 / 2,480 trades validated, 0 strategy-specific violations; control parity 8 / 8; 20 boundary
legs purged; causality: one STATIC_FUTURE_INDEX WARN (`decisions[k + 1]` sets when a leg ends - the next rebalance's decision, made before the
exit fill: not look-ahead).

**Absolute performance (per leg, net) - a trade-level description, NOT SELECTION-EDGE EVIDENCE**

| | Legs | Gross | Slippage | Net | Mean R | t (per leg) |
|---|---|---|---|---|---|---|
| TRAIN | 800 | 65,717 | 910 | 64,807 | 0.0164 | 4.88 |
| OOS | 440 | 64,379 | 430 | 63,949 | 0.0298 | 4.25 |
| All | 1,240 | 130,096 | 1,340 | 128,756 | 0.0212 | 6.40 |

Absolute verdict: PASS (the validation_v2 policy on the legs).

**Control comparison (one observation per rebalance period)**

| | TRAIN (2021-08 → 2024-11) | OOS (2025-01 → 2026-10) | All |
|---|---|---|---|
| Periods | 40 | 22 | 62 |
| Strategy | +80.3 % | +79.0 % | +222.7 % |
| SELECTION CONTROL (equal-weight eligible universe) | +68.1 % | +48.5 % | +149.8 % |
| Excess over the control | +12.1 pp | +30.5 pp | +72.9 pp |
| Mean excess per period (net / gross) | +0.22 % / +0.24 % | +1.11 % / +1.13 % | +0.54 % / +0.56 % |
| Excess t-statistic (per period) | 0.39 | **0.87** | 0.93 |
| Excess win rate | 60 % | 55 % | 58 % |
| Active volatility (annualized) | 12.4 % | 20.9 % | 15.8 % |
| Worst relative period | 2023-01: −11.1 % | 2026-10: −8.4 % | 2023-01: −11.1 % |
| Max relative drawdown | 13.0 % | 12.5 % | 13.0 % |
| Concentration | one period 87 % of the summed excess | one period 64 % | 47 % (not concentrated) |
| Volatility strategy / control (annualized) | 19.8 % / 16.9 % | 28.7 % / 12.0 % | 23.2 % / 15.3 % |
| Max drawdown strategy / control | 22.6 % / 21.3 % | 14.1 % / 6.2 % | 22.6 % / 21.3 % |
| Turnover per period strategy / control | 23.9 % / 2.6 % | 18.4 % / 0.1 % | 21.9 % / 1.7 % |
| MARKET BENCHMARK (SPY) | +44.3 % | +31.9 % | +90.3 % |

**Selection edge: NOT_ESTABLISHED** (`EXCESS_NEGLIGIBLE`: OOS mean excess +1.11 % per period, t = 0.87 over 22 periods, below the
pre-registered 1.0). **Overall verdict: FAIL** (not ready for paper). **Lifecycle: Research.**

Diagnostics (descriptive only):

* Turnover and friction: one-way turnover 20.9 % per month (about 2.5x a year), selection stability 79 %; friction costs the strategy about
  0.28 % a year and consumes 9 % (TRAIN) / 2 % (OOS) of the gross excess - friction does not explain the verdict.
* Market months: the excess is positive when SPY rose and negative when it fell - All: +1.24 % per period in 41 up months, −0.84 % in 21 down
  months; OOS: +2.40 % (16) vs −2.32 % (6). Together with 2.4x the control's volatility in OOS, the excess looks largely like a higher-beta tilt
  in a rising market rather than a clean selection edge.
* Calendar (mean excess per period): 2021 (Aug–Dec) +0.48 %, 2022 −0.19 %, 2023 −0.12 %, 2024 +0.91 %, 2025 +0.09 %, 2026 +2.34 %.
* Concentration: by SEC SIC major group, electronic equipment & semiconductors (SIC 36) led the portfolio at 42 of 63 rebalances (mean top-group
  share 29 %, max 45 %; by the narrow SIC description, semiconductors led at 35); the most held names were AVGO (70 % of rebalances), LLY (68 %),
  NVDA (67 %), GE (65 %) and KLAC (62 %); max single-name weight 5 %.
* Who carried the excess (each name's part of the summed period excess, +0.332 over all 62 periods): CLS +0.114 (34 %), AMD +0.070, MU +0.066,
  NVDA +0.059, SNDK +0.054 - the top five exceed the total; the largest detractors were ORCL −0.027, TSLA −0.021, BRK.B −0.017, MRVL −0.016,
  BKNG −0.016. CLS and SNDK are exactly the names the survivor universe favours (section 8).
* Sensitivity (TRAIN view only; every row on the same 37 periods that all configurations can form): base t 0.22; 9-1 0.04; 15-1 −0.27; top 15
  0.20; top 25 0.19. No neighbour shows an edge on TRAIN; nothing was chosen from it.
* Data quality: one provider artifact - HON 2026-06-29: the raw / split-adjusted ratio moves 0.5 -> 1.0 with no raw price move (no real split);
  the all-adjusted series Momentum uses shows an unexplained −4.6 % step against raw there. Momentum never held HON in 2026; the step lowers
  HON's momentum for the Aug-Oct 2026 rebalances (it was not near the top 20) and the control's June 2026 period return by about 0.06 pp. All 20
  real splits in the bundle are absorbed by the adjusted series (to within 0.01 %).
* Eligibility: 78-80 eligible stocks per rebalance (mean 78.7); exclusions (stock-rebalances): SECOND_LISTING 63 (GOOGL), NO_PRICE 43 (SNDK before
  its listing), LIQUIDITY_BELOW_MIN 28 (CLS, IEX volume), INSUFFICIENT_HISTORY 11 and LIQUIDITY_HISTORY 1 (SNDK's first year).

## 6. Interpretation and decision

The 12-1 ranking selected a portfolio that beat both the market benchmark and its own eligible universe on paper - by a wide margin in OOS
(+30.5 pp over 22 months). The validation_v3 question is whether that is evidence of a selection edge, and the answer is no: one month carries
64 % of the OOS excess, the excess swings with the market's direction, the portfolio ran at 2.4x the control's volatility, and the period-excess
t-statistic is 0.87 - below the pre-registered threshold for "not negligible" (1.0), let alone "established" (2.0). TRAIN is weaker still (t 0.39,
the excess carried by one period) and no TRAIN-only neighbour shows an edge. A third of the excess comes from one name (CLS) and much of the rest
from semiconductors and a 2025 listing (SNDK) - names a survivor universe selects for - so even this paper excess overstates what momentum
selection would have earned on a point-in-time universe. **Decision: keep Momentum in Research.** Per the policy it is not
eligible for human review for paper trading; nothing is promoted.

## 6a. Development log (what was seen when)

1. Hypothesis and definition written (sections 1-2) before any code.
2. Feasibility and candidate generation checked on the TRAIN VIEW only (`dataset.until(train_end)`): eligibility counts, endpoints - no return.
3. Freeze: commit `ad3e71c` ("freeze Momentum v1 rules before OOS") - every rule, the universe, the friction and the sensitivity set.
4. First run: **VALIDATION INVALID** (not stored) - the independent audit's split check flagged 28 legs of T and GE: it compared the all-adjusted
   momentum with a raw / split-ratio momentum and assumed they differ by dividends only, but the all-adjusted series also carries spin-off
   distributions (total return). That run printed its OOS figures (they were seen). The AUDIT was corrected (commit `3b8ead2`: split continuity on
   real split sessions); no strategy rule changed - the candidate set is identical, and `test_G06` checks the rule code is byte-identical to the
   freeze commit.
5. Second run (`baaa5cff…`, git `3b8ead2`): the result above (stored).
6. Timeline evidence: the freeze commit is 03:56:35Z; the first OOS run started at 03:56:48Z and its record carries `git_commit_sha` `ad3e71c` (the
   tree was clean - the frozen code); the audit fix was committed at 03:57:58Z and the second run started at 03:58:04Z on `3b8ead2`.
7. Independent adversarial review (section 7). Its fixes changed the audit's coverage (stricter: every split of every stock), the diagnostics and
   the survivorship wording - no strategy rule. The stricter audit found the HON ratio artifact; the classification of ratio moves (a real split
   moves the raw price) was added so that a provider artifact in a series the strategy does not use is reported rather than failing the run.
   Final run `52922e3f…` at `26891c5`: identical candidate set and period results.

## 7. Adversarial review

An independent reviewer read the code (without this document's conclusions) and probed it with synthetic data. Look-ahead, 12-1 endpoints,
correctly adjusted splits, history handling, timing, friction, the control, sample size, arms and isolation were clean; the engine reorder leaves
Value + Quality's diagnostics, flags and verdict unchanged. Findings:

| # | Area | Finding | Severity | Resolution |
|---|---|---|---|---|
| 1 | Survivorship | for a price ranking the survivor universe is not neutral between strategy and control (CLS, SNDK in; collapsed winners out); the Lab text claimed the control shares it | HIGH | cannot be fixed without point-in-time constituents: the disclosure is corrected everywhere (Lab, README, here) and the attribution shows the bias at work (CLS 34 % of the excess, SNDK in the top five); the v3 policy is not changed after the OOS - a survivorship gate is recommended for a future validation version |
| 2 | Split audit coverage | the split check only looked at the SELECTED legs' lookback windows: an unadjusted split in a non-selected stock (or one with a hole before it) went undetected | MEDIUM | fixed: every split of every universe stock over the whole dataset, across holes, independent of the selection (test U04) |
| 3 | Post-OOS audit change | the first run's audit change was disclosed in the commit only; git alone could not show the freeze preceded the OOS run | MEDIUM | disclosed in section 6a with the run records' timestamps and code versions (the first OOS run carries the freeze commit) |
| 4 | Listing order | one listing per issuer is applied before eligibility: if GOOG were ineligible, GOOGL would still be dropped | LOW | a rule: deferred to a v1.1 (GOOG was never ineligible in the data, so v1 is unaffected) |
| 5 | A month-long data gap | with no bar between two rebalances a leg would exit at its entry bar's close while the control exits at the open: parity fails and the run is INVALID (fails closed) | LOW | documented (no such gap in the data) |
| 6 | Sensitivity periods | the neighbours were measured on different period sets | LOW | fixed: every row on the same 37 TRAIN periods (test V06) |
| 7 | Concentration | the narrow SIC description understated the sector concentration; there was no per-name attribution | LOW | fixed: SIC major group and per-name excess contribution (test V06) |
| 8 | Final period | the last (DATA_END) period counts as a full OOS observation (validation_v3 behaviour) | LOW | documented |
| 9 | Control membership | checked by size against the candidate set; the momentum audit (which re-derives the eligible set) runs before the v3 block | LOW | documented; the size check is pinned by the candidate set hash |
| 10 | Provenance text | the shared bundle's universe text describes Value + Quality's SIC exclusion | LOW | documented: Momentum's universe rule is in section 2 |
| 11 | Data quality (found by fix 2) | HON 2026-06-29 ratio artifact | — | reported in the run's diagnostics (section 5); it also affects **Value + Quality**, whose share counts are re-based by that ratio: HON's market cap read $66.9B (about half) and HON entered Value + Quality's top 20 (rank 20) at the 2026-09-01 OOS rebalance - a finding for the Value + Quality record, which this stage does not modify |

## 8. Limitations

* **Survivor universe, and it is not neutral for momentum** (adversarial review, HIGH): today's large caps are in the universe partly BECAUSE their
  momentum persisted (CLS; SNDK, listed in 2025 and eligible from 2026-02, inside OOS), while momentum winners that later collapsed are absent. A
  survivor universe therefore favours exactly what the strategy ranks on, and the selection control does not cancel that: the bias most likely
  flatters the selection edge. The v3 policy does not gate on it (its thresholds were fixed before Momentum and are not changed after its OOS);
  here the edge is NOT_ESTABLISHED even with this tailwind, but a positive edge on this universe would not have been evidence. A point-in-time
  universe (or a survivorship gate in a future validation version) is needed before any momentum conclusion.
* Short history: 62 monthly periods (22 OOS) in one market path dominated by a semiconductor / AI-led advance; momentum's typical failure mode
  (sharp losses when depressed markets rebound) is barely represented.
* No risk adjustment in the edge test: the period excess is a raw return difference; the strategy's higher volatility is reported, not netted.
* IEX-only volume (a conservative liquidity filter: it excludes CLS at 28 rebalances); sector labels are today's SEC SIC (not point in time); 1-4
  missing sessions per symbol in the
  bundle (an endpoint on one excludes the stock for that rebalance; 50 symbols have such gaps).
* The provider's split-adjusted series carries one artifact (HON 2026-06-29); a split missed in BOTH adjusted series would be undetectable.
* Monthly re-sizing of continuing legs is not charged friction (the Stage 6.4 convention; about 0.35 bp of equity a month at 5 bps).
* Total-return momentum (dividends and spin-offs included); a price-only variant would differ slightly for high-yield and spin-off names.

## 9. Reproduce

```
python -m strategy_validation validate --strategy MOMENTUM --dataset runtime/vq/vq_bundle.json --persist
```
(the bundle is built by `python -m value_quality.ingest fetch` / `build`, see DESIGN_64_VALUE_QUALITY.md; it is git-ignored.)
