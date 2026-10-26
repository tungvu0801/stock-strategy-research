# Stage 6.4 — Strategy #2: Value + Quality (research only)

> **Backtest results are hypothetical.** PASS means eligible for human review - never a promotion, never a forecast, never a trading signal.
> The lifecycle stays **Research**; nothing in this stage reaches a broker.

**Hypothesis.** Companies that are inexpensive versus their fundamentals AND financially strong / profitable may outperform weaker or more
expensive peers over medium-to-long horizons. No momentum, no market timing, no model.

Code: `value_quality/` (`__init__.py` rules, `pit.py` point in time, `core.py` the one rule implementation, `data.py` the data interface,
`adapter.py` the historical replay adapter, `audit.py` independent checks, `diagnostics.py` research diagnostics, `ingest.py` the separated
network step), `strategy_registry/value_quality.py` (Strategy Lab sections). Harness extensions: `STRATEGY_VALIDATION_HARNESS.md` §13.

## 1. Feasibility

**Conclusion: FEASIBLE with documented limitations** (survivorship, today's sector classification, IEX-only volume). Sources: SEC EDGAR
`companyfacts` / `submissions` / `company_tickers` (free, with filing dates and accession numbers) and Alpaca IEX daily bars (raw,
split-adjusted, all-adjusted). Data retrieved 2026-10-26T00:05:46Z for 2020-07-27 → 2026-10-23 (1,570 sessions; 164 responses, each
sha256-recorded in the raw manifest; the bundle is rebuilt offline from them).

| FACTOR | SOURCE | FIELD | POINT-IN-TIME? | AVAILABLE DATE | COVERAGE | REVISIONS? | USABLE? | NOTES |
|---|---|---|---|---|---|---|---|---|
| Earnings yield (TTM NI / market cap) | SEC companyfacts | `NetIncomeLoss` → `NetIncomeLossAvailableToCommonStockholdersBasic` → `ProfitLoss` | yes (`filed`) | session after `filed` | 80 / 80 issuers | yes (comparatives, amendments) | yes | only when NI > 0; ProfitLoss includes non-controlling interests |
| Book-to-market (equity / market cap) | SEC companyfacts | `StockholdersEquity` | yes | session after `filed` | 76 / 80 | yes | yes (optional factor) | only when equity > 0; no total-equity substitute |
| Cash-flow yield (TTM OCF / market cap) | SEC companyfacts | `NetCashProvidedByUsedInOperatingActivities` → `…ContinuingOperations` | yes | session after both YTD filings | 80 / 80 | yes | yes | reported year-to-date: quarters are YTD differences |
| Return on assets (TTM NI / assets) | SEC companyfacts | NI as above, `Assets` | yes | session after `filed` | 80 / 80 | yes | yes | negative values rank low |
| Cash flow on assets (TTM OCF / assets) | SEC companyfacts | OCF as above, `Assets` | yes | session after `filed` | 80 / 80 | yes | yes | |
| Equity ratio (equity / assets) | SEC companyfacts | `StockholdersEquity`, `Assets` | yes | session after `filed` | 76 / 80 | yes | yes (optional) | low leverage = high |
| Market cap | Alpaca raw close x SEC diluted shares | `WeightedAverageNumberOfDilutedSharesOutstanding` (→ basic-and-diluted → basic) | yes | session after `filed` | 79 / 80 | yes | yes | weighted average of the latest quarter, re-based across splits; <1M rows rejected (XBRL scale errors) |
| Price / returns | Alpaca IEX | raw, split-adjusted, all-adjusted bars | yes (completed sessions) | the session itself | 81 / 81 symbols (SNDK from 2025-02) | n/a | yes | one invalid bar removed (T 2021-06-11, low 0.0) |
| Liquidity | Alpaca IEX | raw close x volume | yes | the session itself | 81 / 81 | n/a | yes | IEX volume is a fraction of consolidated volume (conservative filter) |
| Sector | SEC submissions | SIC code / description | **no** (today's) | n/a | 80 / 80 | n/a | exclusion + reporting only | not point in time |
| Gross profit, operating income, revenue, liabilities, capex | SEC companyfacts | various | yes | | 43–69 / 80 | yes | **not used** by v1.0 | coverage too uneven for a 2-of-3 rule |
| Historical universe membership | none available | | **no** | | | | **no** | fixed universe → `SURVIVORSHIP_BIAS_PRESENT` |
| Delisted stocks | none in the universe | | | | 0 | | n/a | a survivor universe by construction |
| Filing time / acceptance timestamp | SEC (not in companyfacts) | | partially | | | | not used | the 1-session lag after the filing DATE is conservative |

Corporate actions in the window: 21 split-ratio changes (Alpaca's split adjustment) across 18 symbols (AAPL, AMZN, AVGO, BKNG, GE, GOOG/GOOGL, HON, ISRG, KLAC, LRCX, NEE, NFLX,
NOW, NVDA x2, PANW x2, TSLA x2, WMT); spin-offs (e.g. GE) are in the all-adjusted prices; no delisting.

## 2. Point in time

* **available_at** = the first session strictly AFTER the SEC filing date (`AVAILABILITY_LAG_SESSIONS = 1`; the filing time is not used).
  A rebalance at T reads only facts with `available_at <= T` (`value_quality/pit.py`).
* **Restatements**: per fiscal period, the highest-priority concept visible at T and, within it, the LATEST filing visible at T. A restated
  value is invisible until the restating filing is available, after which it replaces the original; the original is never "corrected"
  retroactively (test P02: value A before the restatement, B after).
* **TTM**: the sum of the latest four CONTIGUOUS fiscal quarters (80–120 days each - COST / PEP close 16-week quarters) spanning one fiscal year
  (345–385 days); a quarter is a direct duration or the difference of two year-to-date durations, available only when both filings are. A gap
  makes the TTM missing - never padded.
* **The Aug 10 test** (P01): a Q2 filed on Aug 10 is invisible to the Aug 2 rebalance (Q1 is used, and modifying Q2 changes nothing); the
  Sep 1 rebalance uses Q2.
* The TRAIN view of the harness (`Dataset.until(train_end)`) also cuts the auxiliary data: facts filed after the TRAIN end are removed.
* Staleness: the latest quarter must end within 200 days of T.

## 3. Universe and survivorship

The universe is the 81-stock Stage 3.2 research store (today's large caps) - **no point-in-time constituent history exists** in any source
available here. This is a survivor universe: stocks that failed, shrank or were delisted since 2020 are absent. Every run therefore carries
**`SURVIVORSHIP_BIAS_PRESENT`** (stored with the run, shown as a badge with the verdict, never suppressed). Rules applied at every rebalance:

* one listing per issuer (GOOG kept, GOOGL `SECOND_LISTING`); SEC SIC 6000–6799 excluded (banks, brokers, insurers incl. UNH, REITs: book value,
  leverage and cash flow are not comparable) - 11 issuers;
* raw close >= 5 USD, 20-session median dollar volume >= 1M USD (IEX volume), market cap >= 2B USD;
* every exclusion is counted by reason (`ineligible_by_reason`), never silent.

Because the universe itself is biased upward, the meaningful comparison is the **equal-weight universe control** (section 10), not SPY.

## 4. Corporate actions

* **Splits**: share counts are re-based by the ratio raw close / split-adjusted close between the filing date (the basis the filing used)
  and T; a share fact filed before the split-ratio history begins cannot be re-based and is unusable (`NO_SHARE_COUNT`). Market cap = raw close
  x re-based shares is continuous across a split (test C01); the audit re-derives it (MARKET_CAP_SPLIT).
* **Returns** use the all-adjusted bars (splits and dividends).
* **XBRL scale errors** (MCD reported 751.8 "shares" = millions) are rejected below 1M shares, never rescaled.
* Spin-offs, mergers and ticker changes are not modelled beyond the adjusted prices (limitation).

## 5. XBRL concepts and the pre-OOS data decisions

Issuers switch XBRL concepts over time (BKNG / CAT: `NetIncomeLoss` → `…AvailableToCommonStockholdersBasic`; MA / AVGO → `ProfitLoss`; DIS / RTX /
MRK / LIN / T between operating cash flow and its continuing-operations variant; PANW reported basic-and-diluted shares). Each field has an
ordered concept list (`value_quality.CONCEPTS`); per period the highest-priority concept visible at T wins and the concept used is recorded with
every value. These decisions, the 16-week quarter span and the share-count scale rule were made on **TRAIN data coverage only** (why a stock was
ineligible), before any performance figure and before the pre-registration commit.

## 6. Factors

| Group | Factor | Definition | Missing when |
|---|---|---|---|
| Value | earnings_yield | TTM net income / market cap | NI <= 0 (a negative P/E is not cheap) |
| Value | book_to_market | stockholders' equity / market cap | equity missing or <= 0 |
| Value | cash_flow_yield | TTM operating cash flow / market cap | OCF <= 0 |
| Quality | return_on_assets | TTM net income / assets | never (negative ranks low) |
| Quality | cash_flow_on_assets | TTM operating cash flow / assets | never |
| Quality | equity_ratio | equity / assets | equity missing |

A stock needs at least 2 of 3 value AND 2 of 3 quality factors, else it is not ranked (`TOO_FEW_VALUE_FACTORS` / `TOO_FEW_QUALITY_FACTORS`).

## 7. Preprocessing and composite

Each factor is a cross-sectional **percentile rank** in (0, 1] (average rank for ties) over the eligible stocks of that rebalance - bounded, so an
outlier cannot dominate and no winsorization is needed. value_score = mean of the value ranks present, quality_score likewise,
**composite = 0.5 x value_score + 0.5 x quality_score**; ties broken by symbol. The harness score is 100 x composite (a RANK score; Strategy
Standard v1 compliant, no calibration claimed).

## 8. Shared core and historical adapter

* `ValueQualityStrategyCore` (`core.py`) is the ONE implementation of eligibility, factors, ranking, selection and the rebalance decision
  {enter, hold, exit}; pure and deterministic (no clock, no randomness, no network). A future paper evaluator must call it, never re-implement it.
* `ValueQualityAdapter` (`adapter.py`): `historical_replay_supported`, `replay_parity_requirement = NOT_APPLICABLE` (no separate live
  implementation exists), provenance requires `corporate_actions`, `universe`, `point_in_time`; the replay reads only the offline bundle.
* Registry: `VALUE_QUALITY`, INTERNAL, US_EQUITY, LONG, `POSITION` horizon (the registry's medium-to-long horizon), status RESEARCH,
  version 1.0. A live signal is refused (`STRATEGY_INACTIVE`); the Strategy Lab shows the rules and the stored validation.

## 9. Portfolio and rebalance

* Rebalance: the **first session of each month** (known without future data), from the 21st session of the dataset (the liquidity window).
  Signal at that session's close; a newly selected stock is bought at the next session's open.
* Top N = 20, long only, equal weight (5 % of the fixed hypothetical equity each), no leverage, no short book, no price stop.
* A candidate is one **monthly leg**: held from the open after rebalance R to the open of the first session after the next rebalance (a
  TIME_EXIT at the open, `time_exit_fill = OPEN`), where the next leg begins. `entry_trade` = not selected at the previous rebalance (ENTER);
  `exit_trade` = not selected at the next one (EXIT). A continuation (HOLD) is not an order and pays no friction. The last rebalance's legs run
  to the data end.
* Why legs and not holding spells: with ~9 % monthly turnover a holding lasts about a year, so the Stage 6.3.1 boundary purge would remove every
  position open at the TRAIN end and the OOS window would start almost empty. With legs only the boundary month is purged (20 legs).

## 10. Friction, benchmark and universe control

* Friction: 5 bps slippage per side on real orders only; no commission (gross, slippage, fees and net all reported).
* Benchmark: SPY (all-adjusted) over the same sessions - a comparison, never an entry rule.
* **Portfolio view**: each rebalance period is a buy-and-hold of its legs from the equity reset at the period start (legs sized on the fixed
  equity, the uninvested remainder as cash), marked daily; periods chain on the hand-over open and compound.
* **Universe control**: an equal-weight portfolio of every ELIGIBLE stock, bought at the same open as the legs and held (weights drift, no
  friction) to the same exit open - built exactly like the strategy portfolio (test D01: a strategy that holds the whole universe matches it
  within integer shares and friction). It shares the universe's survivorship bias, so strategy minus control isolates what the Value + Quality
  SELECTION added.
* **Boundary window**: the purged boundary legs have no simulated outcome; their holding window (2024-12-03 → 2025-01-03, 22 sessions) is
  excluded from the strategy, SPY and the control alike. The excess over the control is
  tested **per rebalance period** (the independent unit: the ~20 legs of one month share the market's move, so the harness's per-leg t-statistic
  overstates the evidence). If either TRAIN or OOS has a per-period t below 2, the run carries **`SELECTION_EDGE_NOT_ESTABLISHED`**.
* Sector (SEC SIC description, today's) and single-name concentration are reported, not neutralized.

## 11. Validation

* Harness: Stage 6.2 / 6.3.1 (`validation_v2`): chronological TRAIN 70 % / OOS 30 % by sessions, TRAIN legs whose window crosses the TRAIN
  end are purged (`TRAIN_OOS_BOUNDARY_PURGE`), next-bar-open entries, friction, independent mechanical audit, integrity gate, persistence only
  when ELIGIBLE.
* Arms: RULES; GATED = RULES (no gate is invented: `NO_ADDITIONAL_GATES`); MODEL `NOT_EVALUATED` (no model, no LLM, no external call).
* Sensitivity (pre-declared, diagnostic only, never chosen): top N 15 / 25, value weight 0.4 / 0.6. (Winsorization levels are not applicable:
  ranks are bounded.)
* Strategy-specific audit (`value_quality/audit.py`, imports neither the core nor `pit.py`, only the frozen rule constants):
  REBALANCE_TIMESTAMP, FACTOR_AVAILABILITY, RESTATEMENT (balance-sheet values = latest filing visible), RANK_SELECTION (score formula; ranks
  exactly 1..k in falling score order), SHARE_COUNT_SOURCE (the share count is a raw XBRL row and the latest period visible),
  FUNDAMENTAL_SOURCE (every accession behind a fundamental exists in the issuer's raw rows, filed before the signal), MARKET_CAP_SPLIT,
  PORTFOLIO_SIZE (exactly min(top N, eligible) legs), TRANSACTION_FLAGS, EXIT_AT_REBALANCE, NO_OVERLAP - any violation fails the audit and
  blocks persistence. Not re-derived by the audit (unit-tested instead): the percentile ranks of the non-selected stocks and the TTM arithmetic.
* CLI: `python -m strategy_validation validate --strategy VALUE_QUALITY --dataset runtime/vq/vq_bundle.json --persist`.

## 12. Development log (what was seen when)

1. Feasibility and ingestion (no performance computed).
2. Development on the **TRAIN view only** (`dataset.until(train_end)`): eligibility counts by reason drove the concept lists, the 16-week
   quarter span, the share-count scale rule and the invalid-bar rule (section 5). No return was computed.
3. **pre-registration**: commit `4358a3a` froze every rule (factors, weights, top N, filters, concepts, legs, sensitivity set) before the OOS
   run. `tests/test_value_quality_64.py::test_Z01` compares the current rules with that commit.
4. First full run: **0 trades** - the harness applied the Strategy Standard's live-emission rule to research candidates (every candidate
  rejected as `STRATEGY_INACTIVE`), so no performance was produced. Fixed in the harness (research registry check) + the CANDIDATE_CONTRACT gate
  (commit `a681114`); no strategy rule changed.
5. Second run: the result below. The harness PASS reflected absolute leg returns, which include the market and the survivor universe; the
  universe control, the per-period test and the `SELECTION_EDGE_NOT_ESTABLISHED` flag were added as **disclosure** (commit `1b0feeb`). They
  can only make the reading stricter; the verdict rules were not changed after the OOS was seen.
6. Final reconciliation: Strategy Lab sections, text corrections of the rebalance / exit wording (the implemented timing was always the
  open; no rule changed), tests, documents.
7. Independent adversarial review (a separate reviewer reading the code, section 15): no look-ahead found. Fixed: the diagnostics counted the
  purged boundary month as cash for the strategy only and the control was close-to-close rather than built like the legs (together they
  moved the OOS excess over the universe from +2.0 to +9.0 pp), and the audit gained source checks. A first
  attempt to handle data ending on a rebalance session (never rebalancing on the last session) was itself flagged by the harness's
  truncation-invariance check as look-ahead and reverted; the audit handles that case instead. Candidates, legs, trades and the verdict are
  unchanged (identical candidate set hash); the final run is reproduced at the PR head.

## 13. Results

Run (reproduced at the PR head; first produced at git `1b0feeb`): dataset `sha256:e072e67517615fbb8cc42ebb`, 1,480 candidates (0 skipped), candidate set
`b7e1572aeddd4bd9104d9a808f4aa7e9`. TRAIN 2020-07-27 → 2024-12-05, OOS 2024-12-06 → 2026-10-23 (471 sessions); 74 rebalances from 2020-09-01.

**Harness (RULES, per leg, net of friction)**

| | Legs | Gross | Slippage | Net | Mean R | t (per leg) | Win rate | Max DD |
|---|---|---|---|---|---|---|---|---|
| TRAIN | 1,020 | 85,857 | 448 | 85,409 | 0.0170 | 6.30 | 56.0 % | 18,923 |
| OOS | 440 | 45,817 | 279 | 45,538 | 0.0210 | 3.69 | 56.6 % | 9,880 |
| All | 1,460 | 131,674 | 727 | 130,948 | 0.0182 | 7.14 | 56.2 % | 18,923 |

20 TRAIN legs purged at the boundary; accounting PASS; audit 2,920 / 2,920 trades validated, 0 strategy-specific violations; integrity PASS,
persistence ELIGIBLE; sensitivity ROBUST (mean R 0.0172–0.0182 across the four neighbours); causality: one STATIC_FUTURE_INDEX WARN (section 15).

**Portfolio (equal weight, per-period buy and hold, compounded; the boundary window excluded from all three)**

| | Strategy | SPY | Universe control | Excess vs SPY | Excess vs universe | Per-period excess t (periods, positive) | Sharpe strat / SPY / univ. | Max DD strat / SPY / univ. |
|---|---|---|---|---|---|---|---|---|
| TRAIN (2020-09-02 → 2024-12-02) | +120.7 % | +82.2 % | +115.8 % | +38.5 pp | +4.9 pp | 0.19 (51, 43 %) | 1.15 / 0.93 / 1.13 | 21.7 / 24.5 / 24.5 % |
| OOS (2025-01-06 → 2026-10-23) | +53.2 % | +30.9 % | +51.2 % | +22.3 pp | +2.0 pp | 0.15 (22, 55 %) | 1.48 / 1.01 / 1.51 | 17.7 / 18.7 / 18.7 % |
| All | +238.1 % | +138.4 % | +226.3 % | +99.6 pp | +11.8 pp | 0.24 (73, 47 %) | 1.25 / 0.96 / 1.24 | 21.7 / 24.5 / 24.5 % |

Calendar years (strategy / SPY; 2024 and 2025 without the boundary window): 2020 (Sep–Dec) +8.0 / +6.9 %, 2021 +34.4 / +28.7 %, 2022 −9.3 /
−18.2 %, 2023 +35.6 / +26.2 %, 2024 +23.7 / +28.2 %, 2025 +33.9 / +16.5 %, 2026 (to Oct 23) +14.4 / +12.3 %.

Construction: one-way turnover 8.6 % / month, selection stability 91.4 %, eligible stocks per rebalance 43–67 (mean 63.1), max single-name weight
5 %. Sector: the most frequent top sector holds on average 20.5 % of the portfolio (max 30 %; Pharmaceutical Preparations most often, then
Semiconductors, then Prepackaged Software). Factor coverage among eligible stocks: E/P 96 %, B/P 81 %, CF/P 100 %, ROA 100 %, CFOA 100 %,
equity ratio 89 %. Contribution: the selected stocks' mean value score 0.726 vs 0.517 for all eligible, quality 0.651 vs 0.506 (both
dimensions drive the selection). Exclusions (stock-rebalances): EXCLUDED_SIC 814, NO_SHARE_COUNT 257 (mostly the first months: share filings
before the split-ratio history begins), SECOND_LISTING 74, TOO_FEW_VALUE_FACTORS 54, NO_PRICE 54 (SNDK before its listing), LIQUIDITY_BELOW_MIN
39, NO_TTM_EARNINGS 31, NO_TTM_CASH_FLOW 3, LIQUIDITY_HISTORY 1.

## 14. Verdict and interpretation

**Harness verdict: PASS - eligible for human review.** Flags: **`SELECTION_EDGE_NOT_ESTABLISHED`**, **`SURVIVORSHIP_BIAS_PRESENT`**.

The pre-registered harness rules are met (adequate samples, positive OOS after friction, consistent with TRAIN, |t| >= 2 per leg, audit
passed). But the portfolio's outperformance of SPY is almost entirely shared by the equal-weight survivor universe: the selection added
+4.9 pp over four TRAIN years (t 0.19 over 51 months) and +2.0 pp in OOS (t 0.15 over 22 months) - indistinguishable from zero. The honest reading:
**this backtest does not establish that Value + Quality selection adds return beyond the survivor universe it ranks.** PASS means eligible for
human review only; the lifecycle stays Research. A paper candidate decision would need, at minimum, a point-in-time universe (or a forward
paper record) and a period-level test of the selection edge.

## 15. Adversarial review

| # | Area | Finding | Severity | Resolution |
|---|---|---|---|---|
| 1 | Future fundamentals | facts read only from the session after filing; TRAIN view cuts aux data; audit FACTOR_AVAILABILITY re-derives the lag | — | tests P01, P03, U02 |
| 2 | Filing-date leakage | an earnings release (8-K) can precede the 10-Q: the information is used LATER than the market had it (conservative); filing times unused | LOW | documented |
| 3 | Restatement leakage | latest filing visible at T per period; audit re-derives balance-sheet values; TTM flow items are covered by unit tests, not by the audit | LOW | test P02; documented |
| 4 | Survivorship | fixed universe of today's large caps | HIGH (inherent) | cannot be fixed with available data: `SURVIVORSHIP_BIAS_PRESENT` displayed + universe control + `SELECTION_EDGE_NOT_ESTABLISHED` |
| 5 | Corporate actions | splits re-based and audited; spin-offs / ticker changes only through adjusted prices | LOW | test C01; documented |
| 6 | Negative P/E | a negative E/P is missing, not cheap; negative ROA ranks low | — | test A04 |
| 7 | Outlier domination | percentile ranks are bounded | — | test A07 |
| 8 | OOS tuning | rules frozen in `4358a3a` before OOS; the OOS was seen in every run after the first (0-trade) one; post-OOS changes are a harness bug fix, disclosure diagnostics and their corrections, audit checks and text - no rule; the candidate set hash never changed | MEDIUM (process) | test Z01 compares the rules with the frozen commit |
| 9 | Hidden missing-data exclusions | every exclusion counted by reason; NO_SHARE_COUNT concentrates in the first months; IEX volume excludes 39 large-cap stock-rebalances | LOW | reported in diagnostics |
| 10 | Rebalance timing | signal at R's close, entry next open, exit at the open after the next rebalance; STATIC_FUTURE_INDEX WARN = `decisions[k + 1]` in the adapter: it sets only WHEN a leg ends (the next rebalance's decision, made at that rebalance's close - before the exit fill) and whether that exit is an order; manual review: not a lookahead | LOW | audit REBALANCE_TIMESTAMP / EXIT_AT_REBALANCE / TRANSACTION_FLAGS |
| 11 | Turnover / friction | continuation legs are re-sized to 5 % of the fixed equity each month without paying for the resize trades: a large share (~30–40 %) of the true traded volume, but at 5 bps about 0.35 bp of equity a month (~4 bp a year); the ENTER / EXIT orders of the purged boundary month carry no friction either | LOW | documented |
| 12 | Sector concentration | reported (max 30 % top sector), not neutralized | LOW | diagnostics |
| 13 | Candidate equality | one candidate set for every arm; GATED = RULES | — | integrity CANDIDATE_SET |
| 14 | Audit independence | the audit did not catch ranking, TTM or share-count errors (a reviewer inverted every factor rank and the audit passed) | MEDIUM | partly fixed: SHARE_COUNT_SOURCE, FUNDAMENTAL_SOURCE, exact PORTFOLIO_SIZE, rank order (tests U02, U04); residual LOW: the ranks of non-selected stocks and the TTM arithmetic are unit-tested, not audited; the audit shares the raw bundle, so a wrong reported XBRL value is undetectable |
| 15 | Persistence gate | the 0-trade run (every candidate rejected) was ELIGIBLE before CANDIDATE_CONTRACT existed (stored only in a local preview database) | MEDIUM | fixed: CANDIDATE_CONTRACT blocks it (test G02) |
| 16 | Broker isolation | no broker / execution import; ingestion uses the market-data client only; a replay opens no socket | — | tests X01, X02 |
| 17 | Research lifecycle in the harness | the live-emission rule rejected every RESEARCH candidate (no strategy could be validated before promotion) | HIGH | fixed (research registry check; live signals still refused) |
| 18 | Spell design vs purge | whole holding spells would have been purged at the TRAIN end (the OOS would start empty) | HIGH | fixed before pre-registration: monthly legs |
| 19 | Interpretation | the harness PASS rests on absolute leg returns and a per-leg t that overstates the evidence (legs of one month are correlated) | HIGH | disclosed with the verdict (`SELECTION_EDGE_NOT_ESTABLISHED`, per-period t); a period-level verdict rule for cross-sectional strategies is recommended as a NEW validation version - not applied retroactively, which would itself be post-hoc |
| 20 | Strategy Lab detail | two DOCUMENTED sections had no items: the detail view raised | MEDIUM | fixed (test S02) |
| 21 | Rule text | the REBALANCE / EXIT_FILL / exit_condition wording said "last session" / "close" - also in the pre-registration commit; the implementation (and the frozen code) always used the first session / the open | LOW | text corrected; no rule changed |
| 22 | Diagnostics: boundary month | the purged boundary legs had no P/L, so the strategy was "in cash" for a month while SPY and the control earned - with row 23 it moved the OOS excess over the universe from +2.0 to +9.0 pp | MEDIUM | fixed: the window is excluded from all three series (test D01) |
| 23 | Diagnostics: control construction | the control was a daily-rebalanced close-to-close average starting at R's close (it earned the overnight the legs cannot, and the strategy returns used the fixed equity as base) | LOW | fixed: both are per-period buy-and-holds from the same open (test D01) |
| 24 | Data ending on a rebalance | the run became VALIDATION INVALID (NO_OVERLAP: no hand-over bar exists) | LOW | fixed in the audit (test U05); the legs of that rebalance are explicit NO_ENTRY_BAR skips |
| 25 | TTM vintages | a TTM can sum quarters from different filing vintages (a restated quarter beside unrestated year-to-date values) - point in time, but not one consistent set | LOW | documented |
| 26 | Share-count staleness | the share count has no age limit (only the fundamentals do) | LOW | a rule change: deferred to a future version |
| 27 | An invalid bar on a rebalance date | the ingestion removes an inconsistent all-adjusted bar; on a selected stock's rebalance date it would skip that leg (not in the real run: 0 skips) | LOW | documented |

Deferred LOW items are listed in section 16.

## 16. Limitations

* Survivor universe (no point-in-time constituents, no delisted stocks); SIC classification is today's.
* Market cap uses the latest quarter's weighted-average diluted shares, not the shares outstanding at T.
* IEX-only volume understates liquidity; the dollar-volume filter is conservative.
* `ProfitLoss` (a fallback concept) includes non-controlling interests; two issuers report basic shares only.
* Filing times and acceptance timestamps are not used (a 1-session lag after the filing date instead).
* Continuation legs are re-sized monthly without resize friction; legs within one month are not independent.
* A TTM may combine quarters from different filing vintages; the share count has no staleness limit (v1.0 rules, unchanged after the OOS).
* Six years of data (2020-09 → 2026-10), one market path; 22 OOS months outside the boundary window.
* No paper trading, no forward evaluation: research only.

## 17. Reproduce

```
python -m value_quality.ingest fetch --out runtime/vq --env-file .env --end 2026-10-23     # network: SEC + Alpaca market data
python -m value_quality.ingest build --out runtime/vq                                     # offline, verifies every response hash
python -m strategy_validation validate --strategy VALUE_QUALITY --dataset runtime/vq/vq_bundle.json --neighbors runtime/vq/neighbours.json --persist
```

## 18. Recommended next step

Keep Value + Quality in **Research**. Before any paper consideration: a point-in-time universe (or a forward paper record) and a period-level
selection test as a new validation version. Momentum, Mean Reversion and Breakout are not started.
