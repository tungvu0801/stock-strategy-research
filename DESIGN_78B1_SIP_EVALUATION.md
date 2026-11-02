# Stage 7.8B-1 — SIP research dataset and offline V1 / C1 evaluation (infrastructure only)

Code, documentation and synthetic-only tests. **No SIP history was fetched**, no real-data run was made, no database was written, nothing was
registered or deployed. Base: `main` at `b52e0dc` (PR #48, Stage 7.8 Part B). Packages `research_data/` and `swing_eval/`; tests
`tests/test_sip_eval_78b1.py` with `tests/sip_eval_fixtures.py`.

Source of every value: the operator decision lock `STAGE_7_8B1_DECISION_LOCK.md` (workspace document, R8–R17 and R22, confirmed 2026-11-02),
built on the Stage 7.8C readiness audit and evaluation design and the SIP verification of 2026-11-02. The lock authorizes code, documentation
and synthetic-only tests. It is not approval of a SIP backfill or a real-data evaluation.

## 1. Shape (R15, R16)

| Package | Module | Role |
|---|---|---|
| `research_data/` | `__init__.py` | builder constants, the two allowed URLs, the approval-reference check |
| | `nyse_calendar.py` | the static NYSE closure table 2016–2026 (R8) |
| | `sip_bundle.py` | the SIP daily-bar dataset builder: `plan`, `fetch`, `build` |
| | `quality.py` | the read-only pre-run data-quality report |
| `swing_eval/` | `__init__.py` | the locked values R9–R13 |
| | `manifest.py` | the versioned evaluation manifest (R14) and its checks |
| | `analysis.py` | R9 stability, R10 comparison, R11 eligibility, the blocking validation findings |
| | `evaluate.py` | the offline evaluator and the evidence writer (R16, R17) |

* **No existing module is edited.** The IEX builder (`value_quality/ingest.py`), the global `ALPACA_DATA_FEED` setting, the harness,
  `swing_pullback/`, the registry, config, routes and every live path are byte-identical. The only edited document is
  `DESIGN_78_SWING_PULLBACK.md` (section 9).
* **Isolation.** Neither package imports `config`, the IEX builder, an execution, broker, Robinhood, store, service or market-intelligence
  module, or the harness CLI / store / service. Nothing outside the two packages and the tests imports them. The network library is imported
  only inside `sip_bundle.http_transport`, which allows GET requests to the two allowed URLs only.
* **No persistence.** JSON files only. No SQLite, no harness store; the evaluator refuses every socket and SQLite connection while the runs
  execute.

## 2. The SIP builder (`research_data/sip_bundle.py`)

Three separate steps. Only `fetch` touches the network, and only with an operator approval.

| Step | Network | Gate | Output |
|---|---|---|---|
| `plan` | none | none | the exact request list and its sha256 |
| `fetch` | Alpaca market data (SIP daily bars) and the SEC ticker file, GET only | an approval reference `OPERATOR-APPROVAL:<reference>` **and** the plan's sha256 | every page gzipped under `raw/`, `manifest.json` with each page's sha256 |
| `build` | none | a complete fetch whose plan and every page match their sha256 | `sip_bundle.json` (a harness dataset) |

* **The plan.** One request for `company_tickers.json`, then, for each adjustment (`all`, `raw`, `split`), the universe in chunks of 20
  symbols. The default universe is the 81 stocks of `value_quality.RESEARCH_UNIVERSE` plus SPY: 15 bar requests. Window: from 2016-01-04
  (`DEFAULT_START`) as `[start 00:00Z, the day after the end 00:00Z)`. Parameters: `feed=sip`, `timeframe=1Day`, `limit=10000`,
  `sort=asc`. The default end is yesterday (UTC). A real backfill paginates to about 100 requests.
* **The fetch.** Every request is checked before the first one is sent: an allowed URL, `feed=sip`, `1Day`, the page size, the plan's
  window, the adjustment, symbols inside the universe. A window that does not end before today (UTC) is refused: the current session's bar
  may be incomplete. Credentials (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `SEC_USER_AGENT`) are read by name from the environment or
  an env file. They are never printed or stored: the manifest keeps URLs, parameters and hashes, never headers. Requests are paced (0.35 s;
  SEC 0.15 s). A non-200 response stops the fetch with no retry. More than 60 pages in one request stops it as a materially broader request
  than approved. In both cases the manifest is written as incomplete, and `build` refuses it. An existing non-empty folder is never
  overwritten.
* **The build.** It refuses:
  * a bar not stamped at midnight New York (`T04:00:00Z` in daylight time, `T05:00:00Z` otherwise);
  * a duplicate bar;
  * a symbol the request did not ask for;
  * any page whose bytes differ from the recorded sha256.

  The bars are the all-adjusted series. `aux.series` carries `raw_close` and `raw_volume` (raw) and `split_ratio` (raw / split-adjusted
  close). `aux.static.issuers` maps SEC CIKs to symbols; a symbol without a CIK is listed (and the swing baseline then refuses the dataset).
  A bar with a non-positive or inconsistent OHLC field is removed, leaving a gap, and listed; it is never repaired.
* **Provenance.** `volume_feed: "SIP"`, builder `sip_daily_v1`, the window, the plan and raw-manifest hashes, and the universe statement
  `SURVIVORSHIP_BIAS_PRESENT`.

## 3. The NYSE calendar (R8, `research_data/nyse_calendar.py`)

* **The table.** A static, dated table of the 105 full-day closures 2016–2026, observed holidays included, with the 2018-12-05 and
  2025-01-09 national days of mourning. Early closes are listed for information only: daily bars cannot show them.
* **Verification.**
  * The published session counts per year match (2016: 252 … 2025: 250).
  * The table reproduces the 60 closures and the 1,570 sessions of the IEX bundle's span (2020-07-27 → 2026-10-23).
  * The 2026 rows equal `autorun/calendar.py`.
* **`compare()`.** Returns MATCH, MISMATCH (extra / missing / weekend dates, closures) or NOT_COVERED (outside 2016–2026).

## 4. The pre-run data-quality report (`research_data/quality.py`)

Read-only. It computes no strategy rule, indicator or result. Its only analytical import is the Stage 6.7 data scanner
(`breakout.audit.inconsistent_pairs`).

| Check | BLOCK | FINDING (operator acceptance) | INFO |
|---|---|---|---|
| Q01 identity | | | sha256, kind, symbols, sessions, span |
| Q02 provenance | missing harness fields; no `volume_feed` of SIP or IEX | | complete |
| Q03 coverage | no benchmark | universe symbols without bars | late starters |
| Q04 calendar | a date that is not a benchmark session; the benchmark calendar differs from the NYSE table (extra, missing, weekend, uncovered) | | equal |
| Q05 missing bars | | sessions without a bar inside a symbol's own span | the share of complete 200-session windows |
| Q06 raw series | no `raw_close` (final-review M-4); SIP without `raw_volume` | bar dates without a raw close | IEX without `raw_volume` |
| Q07 issuers | an unmapped symbol; a symbol under two issuers | a kept listing that starts later than another (M-7) | each multi-listing issuer, its listings and the kept one |
| Q08 corporate actions | | each raw / split / adjusted inconsistency | split-ratio changes |
| Q09 bars | | zero-volume bars | bars removed by the builder |

* **Status.** BLOCKED, REVIEW_REQUIRED or CLEAN, with counts.
* **Sealing.** The report carries its own `report_sha256`, and the CLI never overwrites a report.
* **Approval.** A report on a HISTORICAL dataset needs an approval reference.

## 5. The evaluation manifest (R14, `swing_eval/manifest.py`)

* **What it is.** One JSON document (`stage78b_eval_v1`) with its own `manifest_sha256`. It pins what the strategy's pre-registration hash
  does not cover.
* **What it holds.**
  * The V1 / C1 ids and versions, the pre-registration hash `6c20fdb4…3cfa61903`, the code commit and the harness version.
  * The dataset: file sha256, dataset id, kind, `volume_feed`, builder, and the expected symbols / sessions / span.
  * The accepted pre-run report's sha256 and status.
  * Split 0.70 and the boundary policy; arm RULES; the execution assumptions; the sizing.
  * The four runs with their full friction.
  * The R9–R11 thresholds and the R12 / R13 fee records.
  * The evidence location.
* **`verify()`.** Checks the manifest against the strategy code and the locked values. Recomputing the hash after a change does not help:
  every value is compared with the code's.
* **`pre_checks()`** (before any run): the checked-out commit, a clean working tree, the dataset file's sha256 and content, and the accepted,
  unaltered report.
* **`post_checks()`** (after each run; final-review M-2 / M-3): each record's
  * friction (slippage, commission, fee),
  * split, sizing and arms,
  * adapter description (no parameters, `max_hold_bars` 10, entry, time-exit fill, stop policy),
  * execution assumptions,
  * cost label,
  * pre-registration hash, dataset id, volume feed, harness version and strategy id.

  Any mismatch makes that run INVALID; it is never repaired.

A changed value is a new manifest version, and never after a real-data result.

## 6. The offline evaluator (`swing_eval/evaluate.py`)

Each step is fail-closed:
1. The manifest verifies.
2. The output folder is the manifest's evidence location. It must be **outside the repository** and new or empty (R17: for a real run,
   `C:\Data Analyst Py\DA IDX\evidence\stage-78B\<UTC timestamp>\`).
3. A HISTORICAL dataset needs an approval reference **and** the sha256 of the approved manifest. SYNTHETIC datasets are for tests.
4. The pre-checks pass.
5. The four pre-registered runs of `swing_pullback.plan.run()` execute, and the eight sensitivity neighbours are regenerated on the TRAIN
   view. Every socket and SQLite connection is refused meanwhile.
6. The post-checks pass.
7. The blocking validation findings are collected. **M-5:** a non-positive stop (`invalidation_reference` ≤ 0) in any candidate of the
   four runs or of the eight TRAIN-view neighbours. Also a CRITICAL causality report, a failed audit, or a candidate-set mismatch.
8. R9, R10 and R11 are computed.
9. The evidence is written.

**Status.**
* `INVALID`: a post-check failed.
* `BLOCKED_VALIDATION_FINDING`: any blocking finding. No R9, R10 or R11 outcome is accepted. Each finding lists the version,
  configuration, cost run, symbol, signal date, signal close, ATR(14) and stop. The stop calculation, the rules and the candidate set are
  never modified, clipped or filtered, and the run records are kept as produced. Resolution is the operator's decision.
* `COMPLETED`: outcomes accepted. Even then, "eligible for review" is never a promotion.

**Evidence.**
* JSON files: `manifest.json`, `pre_run_report.json`, `dataset.sha256`, `runs/{V1,C1}_{BASE,STRESS_2X}.json`, `validation.json`,
  `stability.json`, `comparison.json`, `eligibility.json`, `environment.json`, `summary.json`.
* A plain-language `RESULTS_SUMMARY.md`.
* `evidence.sha256`, which indexes every file.

## 7. The analysis (`swing_eval/analysis.py`)

* **R9 fold stability.** The gate is V1 BASE; the others are reported. The six folds of `swing_pullback.diagnostics.fold_bounds` are used
  unchanged.
  * A fold is evaluable with ≥ 20 closed trades.
  * ≥ 5 of the 6 folds must be evaluable; otherwise INSUFFICIENT.
  * Net P/L > 0 in ≥ ⌈2/3 × evaluable⌉ folds.
  * Median evaluable-fold mean R > 0.05.
  * No fold holds ≥ 80 % of the total net P/L.
  * Outcomes: STABLE / UNSTABLE.
* **R10 V1 versus C1.**
  * **Population:** the closed RULES trades of each run, R = `r_net`. Boundary-purged candidates and simulation skips are counted and
    reported, not compared. DATA_END exits count as trades and are counted.
  * **Trade counts:** ≥ 20 OOS trades and ≥ 30 in total for each version in each cost run, else INCONCLUSIVE.
  * **C-a:** Δ_R(OOS) ≥ 0.05 in both cost runs.
  * **C-b:** ≥ 4 paired folds (BASE run, both versions ≥ 20 trades), with V1 ahead in ≥ ⌈2/3⌉ of them.
  * **Outcomes:** BEATS_CONTROL (C-a and C-b); NO_ADDED_VALUE (Δ_R ≤ 0 in either cost run); INCONCLUSIVE otherwise.
  * **Reported:** exposure, drawdown, costs, TRAIN versus OOS, and a joint calendar-month bootstrap of Δ_R (2,000 resamples, seed 78,
    5 / 50 / 95 percentiles, descriptive).
* **R10 interpretation.** Written into every `comparison.json` and every summary:
  * The 0.05 R threshold is a materiality threshold, **not a significance test**.
  * Fold consistency uses the whole signal period. It includes **TRAIN observations** and is not an independent OOS confirmation.
  * `NO_ADDED_VALUE` means only that V1 did not show an improvement under the pre-registered mean-R comparison. It is not proof of no
    economic value under every metric.
* **R11 eligibility.** All six items, each with its evidence:
  1. harness PASS on V1 BASE;
  2. V1 OOS expectancy > 0 at STRESS_2X;
  3. R9 STABLE;
  4. R10 BEATS_CONTROL;
  5. TRAIN-view sensitivity ROBUST;
  6. the pre-run report accepted and no blocking finding.

  The edge stays NOT_ESTABLISHED (survivorship).

## 8. Fees (R12, R13)

* **Not modelled.** Regulatory fees are not modelled: `fee_per_share` 0.0 and the flag `REGULATORY_FEES_NOT_MODELLED`.
* **Reference rates.** The SEC Section 31 and FINRA TAF rates, with their sources, are recorded in the manifest and used in no computation.
* **Illustrative estimate.** At an assumed share price of **$50**: about 0.25 bps of sell notional (Section 31 0.206 bps + TAF 0.039 bps).
  * TAF is charged per share, so its part depends on the price: about 0.195 bps at $10, 0.020 at $100, 0.004 at $500.
  * 0.25 bps is not representative of every share price, and it is **not a verified bound** for 2016–2026.
* **Missing history.** The historical rates and the CAT fee schedule are missing. Every summary carries `REGULATORY_FEE_HISTORY_MISSING`.
  The official schedules are to be verified before any real-data run.
* **STRESS_2X** is the pre-registered cost stress. It is not claimed to bound the fees.
* **Commission.** $0 per order is an ASSUMPTION (R13), to be checked against Alpaca's published schedule.

## 9. Final-review M-6 (wording only)

`DESIGN_78_SWING_PULLBACK.md` now says what the harness does:
* The entry is the open of the symbol's next bar.
* The time exit is the close of the 10th bar of the symbol in the trade.
* The TRAIN-view purge uses the neighbour's own `max_hold_bars` (8 / 12).

No rule, constant or code changed. The pre-registration hash is unchanged, and the `swing_pullback/` docstrings are left untouched so that
the package stays byte-identical.

## 10. Tests (synthetic only)

`tests/test_sip_eval_78b1.py`. Every output goes to a temporary folder, and the transport is a fake provider in the Alpaca / SEC response
shapes.

| Group | Covers |
|---|---|
| A | the locked values; the strategy fingerprint |
| C | the calendar |
| B | the builder: the plan, every fetch refusal before any request, pagination, the page guard, no retry, no stored credential, the build and its refusals, the IEX path untouched |
| Q | the report: CLEAN, every BLOCK, every FINDING, the approval gate, no strategy computation |
| M | the manifest: verify, tamper detection even with a recomputed hash, pre-checks, post-checks per field |
| E | the evaluator: end to end with the evidence index verified, every refusal before a run, the offline guard, an INVALID run, M-5 blocking from a base run and from a TRAIN-view neighbour alone |
| N | the analysis on crafted records |
| X | isolation |
| G | this document |
| F | the freeze scope |

## 11. What this stage does not do

* No SIP fetch or backfill, and no real-data backtest: each needs a separate operator approval.
* No database write, no harness store, no strategy registration, no deployment.
* No broker, trading or account endpoint; no Robinhood access.
* No change to a live or paper execution risk limit, to the frozen execution code, or to the global data feed.
* MONITOR_ONLY, Paper Auto OFF, MANUAL_ARM, Robinhood read-only and Evidence Refresh OFF are unchanged.

## 12. A later real run (not authorized by this stage)

For reference only. Each step needs its own operator approval.
1. Verify the official fee schedules and the commission.
2. `python -m research_data.sip_bundle plan`; the operator approves the plan's sha256.
3. `fetch` with the approval reference.
4. `build` (HISTORICAL).
5. `python -m research_data.quality` with an approval reference; the operator accepts or rejects every FINDING.
6. `python -m swing_eval.manifest build`; the operator approves the manifest's sha256.
7. On a clean checkout of the manifest's commit: `python -m swing_eval.evaluate` with the approval reference and the approved manifest
   sha256, writing to the manifest's evidence folder.
