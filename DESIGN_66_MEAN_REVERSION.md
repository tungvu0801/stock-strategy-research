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
  An inconsistent window excludes the stock at that rebalance (`DATA_INCONSISTENT`, with the offending pair recorded) - flagged and
  excluded, never clipped into a usable return. Bars with a non-positive
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
  `survivorship_bias: PRESENT` (research flag `SURVIVORSHIP_BIAS_PRESENT`) -> universe integrity **UNTRUSTED** under universe_integrity_v1, so
  the final selection edge can never be ESTABLISHED on this universe; the raw edge is still computed and shown.
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
  adjusted day return is never more than 0.072 % below the raw one (87,684 day-pairs without a ratio move), while the distributions the
  provider adjusted lift it above raw (spin-offs GE 2023 / 2024, T 2022, DHR 2023; the COST 2020 special dividend) - total return, not a loss.
  The one known provider artifact is in the OOS period (HON 2026-06-29, Stage 6.5): the window rule excludes HON from the rebalances whose
  5-session window contains it. (Corrected after the adversarial review: NOT every spin-off is adjusted - IBM / Kyndryl 2021-11-04, MRK /
  Organon 2021-06-03 and PFE / Viatris 2020-11 are identical in the raw and adjusted series, so they read as real drops; section 7, M1.)

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

## 5. Results

Run `04488324ae244dbfbbdb8c2b7ef2efba` (git `626fb11`, the freeze commit, clean tree; created 2026-10-26T07:44:43Z): 6,440 candidates (0
skipped), candidate set `fd0dd012db34d12a308e64dddcf54e6d`; integrity PASS, persistence ELIGIBLE; audit 12,840 / 12,840 trades validated, 0
strategy-specific violations; control parity 8 / 8; 20 boundary legs purged (one boundary week excluded); 52.2 periods a year; causality: one
STATIC_FUTURE_INDEX WARN (`decisions[k + 1]` sets when a leg ends - the next rebalance's decision, made before the exit fill: not look-ahead).

**Absolute performance (per leg, net) - a trade-level description, NOT SELECTION-EDGE EVIDENCE**

| | Legs | Gross | Slippage | Net | Mean R | t (per leg) | Win rate |
|---|---|---|---|---|---|---|---|
| TRAIN | 4,460 | 79,507 | 15,645 | 63,863 | 0.0029 | 4.19 | 53.3 % |
| OOS | 1,960 | 33,432 | 7,013 | 26,420 | 0.0028 | 2.22 | 52.2 % |
| All | 6,420 | 112,940 | 22,657 | 90,283 | 0.0029 | 4.66 | 53.0 % |

Absolute verdict: PASS (the validation_v2 policy on the legs): a long-only portfolio rose with the market.

**Control comparison (one observation per WEEK)** - did selecting recent losers beat simply holding the eligible universe over the same weeks?

| | TRAIN (2020-08 → 2024-11) | OOS (2024-12 → 2026-10) | All |
|---|---|---|---|
| Weekly periods | 223 | 98 | 321 |
| Strategy | +73.9 % | +26.2 % | +119.4 % |
| SELECTION CONTROL (equal-weight eligible universe) | +134.2 % | +47.8 % | +246.0 % |
| Excess over the control (cumulative) | −60.3 pp | −21.6 pp | −126.6 pp |
| Mean excess per week (net / gross) | −0.121 % / −0.050 % | −0.137 % / −0.064 % | −0.126 % / −0.054 % |
| Excess t-statistic (per week) | −1.47 | **−0.64** | −1.45 |
| Excess win rate | 46.6 % | 42.9 % | 45.5 % |
| Tracking (active) volatility, annualized | 8.9 % | 15.3 % | 11.2 % |
| Worst relative week | 2023-05-22: −3.6 % | 2026-07-20: −6.5 % | 2026-07-20: −6.5 % |
| Max relative drawdown | 27.8 % | 17.4 % | 37.3 % |
| Concentration | n/a (no positive summed excess) | n/a | n/a |
| Volatility strategy / control / SPY (annualized) | 20.4 % / 16.8 % / 15.9 % | 21.5 % / 14.0 % / 13.8 % | 20.7 % / 16.0 % / 15.3 % |
| Sharpe strategy / control / SPY (rf 0) | 0.74 / 1.27 / 1.00 | 0.68 / 1.56 / 1.04 | 0.72 / 1.35 / 1.01 |
| Max drawdown strategy / control / SPY | 36.9 % / 24.1 % / 24.4 % | 17.6 % / 14.8 % / 14.5 % | 36.9 % / 24.1 % / 24.4 % |
| Turnover per week strategy / control | 70.9 % / 1.0 % | 72.8 % / 0.0 % | 71.5 % / 0.7 % |
| MARKET BENCHMARK (SPY) | +86.9 % | +28.4 % | +140.0 % |

**Raw selection edge: NOT_ESTABLISHED** (`NO_OOS_EXCESS`: the OOS mean excess over the control is −0.14 % per week, t = −0.64 over 98
weeks - not positive). **Universe integrity: UNTRUSTED** (FIXED_CURRENT_UNIVERSE): the cap allows at most NOT_ESTABLISHED, so the raw edge was
not lowered (`capped: false`). **Final selection edge: NOT_ESTABLISHED. Overall verdict: FAIL** (not ready for paper). **Lifecycle: Research.**

Diagnostics (descriptive only):

* Turnover and friction: one-way turnover 71 % a week (37x a year), selection stability 29 %; friction costs the strategy 0.072 % a week
  (3.8 % a year). The GROSS weekly excess is already negative (−0.05 % TRAIN, −0.06 % OOS): friction deepens the shortfall but does not create
  it (`FRICTION_CONSUMES_GROSS_EXCESS` not raised: there is no positive gross excess to consume).
* Market weeks (is it reversal or beta?): mean excess +0.05 % in the 187 weeks SPY rose and −0.38 % in the 134 weeks it fell (OOS: +0.05 % in
  56 / −0.39 % in 42). The selected losers fall further than the universe when the market falls: the portfolio behaves like a higher-volatility
  (21.5 % vs 14.0 % OOS), down-market-sensitive basket, not like a reversal edge.
* Falling knife: 53.0 % of the 6,420 selected legs rose over the next week and 47.0 % fell further, but only 47.4 % beat the control; median
  next-week return +0.24 % (p10 −5.0 %, p90 +5.5 %, worst −37.4 %). By depth of the drop: the deepest third (mean prior return −8.1 %) beat
  the control by +0.04 % a week, the middle third (−3.3 %) lagged by −0.09 %, the shallowest third (−0.7 %) by −0.32 %. No filter is derived
  from this in v1.
* Momentum overlap (12-1 ranking evaluated at 273 weekly rebalances with a full history): 21.3 % of the selected losers sit in Momentum's
  bottom quintile and 21.3 % in its top quintile (random expectation 19.2 %); 27.3 % of Momentum's 20-stock portfolio is also selected. The two
  rankings are close to unrelated at this horizon.
* Concentration: the summed weekly excess is negative (−0.40), so the five-name concentration flag does not apply. The largest positive
  contributors were CLS +0.046, SNDK +0.040, PANW +0.037, KLAC +0.033, NOW +0.026; the largest detractors TXN −0.035, CVX −0.032, PFE −0.031,
  UNH −0.031, MCD −0.028 (80 names contributed). By SEC SIC major group, chemicals & pharmaceuticals led the portfolio at 106 of 322 rebalances,
  software 98, semiconductors 91 (mean top-group share 28 %, max 55 %); the most held names were TSLA (38 % of weeks), AMD (36 %), PFE (34 %),
  MRVL (33 %), REGN / NOW / UBER (32 %); max single-name weight 5 %.
* Calendar (mean excess per week): 2020 (Aug–Dec) −0.23 %, 2021 +0.01 %, 2022 −0.35 %, 2023 −0.07 %, 2024 −0.08 %, 2025 +0.00 %, 2026 −0.26 %.
* Sensitivity (TRAIN view only; every row on the same 223 weeks): base t −1.47; lookback 3 +0.07; lookback 10 −0.03; top 15 −1.13; top 25
  −2.18. No neighbour shows an edge on TRAIN; nothing was chosen from it.
* Data quality: 2 DATA_INCONSISTENT exclusions, both HON (`RATIO_ARTIFACT` between 2026-06-26 and 2026-06-29: adjusted −6.1 % vs raw −1.6 %)
  at the 2026-06-29 and 2026-07-06 rebalances, whose windows contain the artifact - without the rule the provider's −4.6 % step would have made
  HON look like a larger loser than it was. 12 selected drops beyond −25 % over 5 sessions (TSLA 2020-09, NFLX 2022-01 and 2022-04, META
  2022-02 and 2022-10, ADBE 2022-09, TSLA 2022-12, SCHW 2023-03, SNDK 2025-04, UNH 2025-04, ACN 2026-06, IBM 2026-07): each identical in the
  raw (split-adjusted) and the adjusted series; the ten up to 2025 coincide with widely reported earnings or market events, the two 2026 drops
  could not be cross-checked outside the bundle - listed, not filtered; 0 single-print V shapes. The control membership re-derived by the
  independent audit matches all 322 periods.
* Data quality found by the adversarial review (section 7; computed on this stored run with the frozen code, which is deterministic - the run
  was not repeated): the HON artifact also sits inside one HELD window, HON's control position in the 2026-06-22 OOS week (a −4.6 % step the
  raw price did not take), which overstates that week's excess by +0.058 pp (+0.0006 % on the OOS mean excess); and three spin-offs the provider
  did not adjust read as real drops - IBM / Kyndryl made IBM a selected loser at 2021-11-08 (rank 12 at −1.3 %; about +3.8 % without the
  distribution, so it would not have been selected), MRK / Organon put a −2.6 % step into MRK's held leg of the 2021-06-01 week (about −0.16 pp on
  that week's excess) and lowered its 2021-06-07 return (still among the 20 most negative without it), and PFE's 2020-11-16 / 17 moves (the
  Viatris distribution) were in its 2020-11-16 selection and its 2020-11-09 control position. All but HON fall in TRAIN; none changes a status.
* Eligibility: 29-80 eligible stocks per rebalance (mean 78.2); exclusions (stock-rebalances): SECOND_LISTING 322 (GOOGL), NO_PRICE 327 (SNDK
  before its listing, feed gaps), LIQUIDITY_BELOW_MIN 162, MISSING_ENDPOINT_BAR 91, LIQUIDITY_HISTORY 4 (SNDK's first weeks), DATA_INCONSISTENT 2.

## 6. Interpretation and decision

Buying the week's 20 worst performers of this universe and holding them for a week did NOT beat holding the whole eligible universe over the
same weeks - it lagged it in TRAIN (−0.12 % a week, t −1.47) and in OOS (−0.14 % a week, t −0.64), before AND after friction, with higher
volatility and deeper drawdowns. The shortfall comes from the weeks the market fell: the recent losers kept falling more than the universe then.
The long-only portfolio still rose with the market (absolute PASS on the legs), which is exactly why the absolute figure is not selection
evidence. Even had the raw edge been positive, the fixed current universe (UNTRUSTED) would have capped it at NOT_ESTABLISHED - here the cap did
not bind. **Decision: keep Mean Reversion in Research.** Per the policy it is not eligible for human review for paper trading; nothing is
promoted, and no rule is changed after this result.

## 6a. Development log (what was seen when)

1. Hypothesis and definition written (sections 1-2) before any code.
2. Implementation and tests on SYNTHETIC fixtures only (a designed-reversal fixture exercises the cap: raw ESTABLISHED -> final NOT_ESTABLISHED).
3. Feasibility on the calendar and the TRAIN VIEW only (`dataset.until(train_end)`): eligibility counts, exclusions, feed gaps, and the
   adjusted-vs-raw day-return noise that bounds the window rule (section 2) - no strategy or control return was computed on the real data.
4. All 8 CI groups and the frontend tests green; then the freeze: commit `626fb11` ("freeze Mean Reversion v1 rules before OOS", 07:44:22Z) -
   every rule, the universe, the window-integrity rule, the friction and the sensitivity set.
5. First and only real run (`04488324…`, started 07:44:33Z on the clean freeze commit; its record carries `git_commit_sha` `626fb11`): the result
   above, stored. Nothing was re-run to change a figure.
6. Independent adversarial review (section 7) after the OOS result was seen: no rule changed (core, data and adapter are byte-identical to the
   freeze commit, `test_G06`). Its findings changed the documentation (the spin-off claim of section 3 was wrong), added a disclosure to the
   diagnostics for future runs (inconsistent data inside a HELD window, `HOLDING_WINDOW_DATA_ANOMALY`) and corrected an audit message; the
   effects on this run were computed from the stored record with the frozen code and are reported in section 5. The stored run is the run of
   the freeze commit.

## 7. Adversarial review

An independent reviewer read the code (without this document's conclusions), re-derived the run from the bundle and the stored record, and
probed it with synthetic data. Clean: look-ahead (truncation invariance at six cuts; the sensitivity sees no OOS bar), the ranking direction
everywhere (core, audit, score, sensitivity, diagnostics - never momentum), the weekly calendar (one decision in each of the 322 ISO weeks, 33
Tuesday rebalances, the year boundaries, hand-overs at the same open), the 1 % window tolerance (an asymmetric consistency bound, not a return cap:
noise at most 0.072 % TRAIN and 0.048 % OOS, so no OOS case was hidden), bad ticks (the 12 extreme drops are identical in both series),
survivorship disclosure and the cap, control fairness, one observation per week, friction on real orders only (0.072 % a week = 2 x 0.71 x 5
bps), turnover, concentration, post-OOS tuning (none: the rule code is unchanged since the freeze; the run is the only one and follows it),
storage (the candidate hash recomputes; gate ELIGIBLE) and broker isolation. Every figure of sections 5-6 matches the stored run. Findings:

| # | Severity | Area | Finding | Resolution |
|---|---|---|---|---|
| M1 | MEDIUM | Corporate actions | three spin-offs are NOT adjusted by the provider (adjusted = raw on the ex-date: IBM / Kyndryl 2021-11-04, MRK / Organon 2021-06-03, PFE / Viatris 2020-11): they read as real drops the window rule cannot see; section 3 claimed spin-offs are absorbed | the documentation is corrected and the effect on this run quantified (section 5: all in TRAIN, no status changes); detecting an unadjusted distribution needs a corporate-action list the bundle does not carry - a rule using one would be a new strategy version (v1.1), not a post-OOS fix |
| M2 | MEDIUM | Data integrity | the window rule guards the SIGNAL window only; an artifact inside a HELD window (a strategy leg or a control position) changes an outcome silently - HON 2026-06-29 sits in HON's control position of the 2026-06-22 OOS week | disclosed, not re-weighted: the diagnostics now list every inconsistent day-pair inside a held window with its effect on the week's excess and raise `HOLDING_WINDOW_DATA_ANOMALY` (test V09); for this run +0.058 pp on one OOS week (section 5). Excluding such weeks would change the OOS result after it was seen, so it is left to a future validation version |
| L1 | LOW | Split classification | a ratio move that is not a clean split (a 5:4 split or a stock dividend where neither series jumps; a 2:1 split on a −30 % day) excludes the stock and, through the audit's split check, blocks the run; the audit message said "both jump" | none occurs in this bundle (the run passed); the message is corrected; classifying by absorption instead of jump size is a rule change for a v1.1 |
| L2 | LOW | Missing bars | a selected stock with no bar during the whole following week gets a one-bar leg that exits at the entry bar's close | fails closed (the audit and the control parity block the run); none occurs here |
| L3 | LOW | Storage | the shared candidate identity and set hash do not include `max_hold_bars` or the transaction flags | the audit re-derives both on every leg (EXIT_AT_REBALANCE, TRANSACTION_FLAGS); a harness-wide hash change would alter every earlier strategy's stored hash - documented |
| L4 | LOW | Control fairness | the control's membership identity is checked by the diagnostics (0 mismatches over 322 periods) and pinned per candidate (`eligible_set_sha`, re-derived by the audit), while the shared gate compares sizes | documented |
| L5 | LOW | Audit independence | the audit is independent code but transliterates the same rule definitions (it shares M1 and L1 by design); `core.evaluate` indexes `sessions[i - lookback]`, which would wrap for a session before the lookback - unreachable through `schedule()` | documented: a future paper / live evaluator must call the core on scheduled sessions only; a guard is a v1.1 change (the rule code stays byte-identical to the freeze) |
| L6 | LOW | Calendar | the 2022-03-08 feed gap moved 7 entries (2022-03-07 rebalance) and 15 exits (2022-02-28 rebalance) to 2022-03-09 | symmetric with the control (the same `_window` rule); disclosed |

## 8. Limitations

* **Survivor universe** (`FIXED_CURRENT_UNIVERSE`, UNTRUSTED): every recent loser in today's large caps is one that survived - losers that later
  failed, were delisted or were bought cheaply are absent. For a loser ranking that bias favours a rebound, so it most likely FLATTERS the
  strategy; the selection edge is negative even with it. The final edge is capped at NOT_ESTABLISHED by universe_integrity_v1 regardless.
* **Large caps only**: 81 of today's largest US stocks. Short-term reversal is usually reported strongest in smaller, less liquid stocks (the
  liquidity-provision explanation); this test says nothing about them.
* One market path of 321 weeks (98 OOS); the weekly observations are treated as independent in the t-statistic (overlapping information across
  consecutive weeks is not modelled).
* No risk adjustment in the edge test: the weekly excess is a raw return difference; the portfolio's higher volatility and down-market
  sensitivity are reported, not netted.
* Friction: 5 bps per side on real orders; with 71 % one-way turnover a week, real spreads and market impact would cost more - but the gross
  excess is already negative, so friction is not the reason for the result. Continuing legs are not charged for re-sizing (the Stage 6.4
  convention).
* Data: IEX-only volume; today's SEC SIC labels (not point in time); two provider feed-gap sessions leave 29 and 36 eligible stocks at four
  rebalances (excluded, never filled); a bad tick present identically in the raw and adjusted series cannot be told from a real move (the 12
  extreme selected drops are identical in both series and were not filtered); the window rule's 1 % tolerance was set from the TRAIN view's
  adjusted-vs-raw noise.
* Timing: a Monday-close signal and a next-open entry (the overnight gap belongs to the leg); other weekdays and intraday entries are untested.
* Total-return prices (dividends and most spin-offs in the adjusted series), so an ex-dividend drop never registers as a loss - except three
  spin-offs the provider did not adjust (IBM 2021, MRK 2021, PFE 2020; section 7, M1), which read as real drops.
* The window rule protects the selection only; a provider artifact inside a held window changes an outcome. It is reported
  (`HOLDING_WINDOW_DATA_ANOMALY`), never re-weighted (section 7, M2).
* A ratio move that is not a clean split excludes the stock and blocks the run (section 7, L1); none occurs in this bundle.
