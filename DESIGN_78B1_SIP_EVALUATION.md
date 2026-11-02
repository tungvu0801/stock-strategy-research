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
| `research_data/` | `__init__.py` | builder constants, the two allowed URLs, the approval-reference check, the output guard |
| | `nyse_calendar.py` | the static NYSE closure table 2016–2026 (R8) |
| | `sip_bundle.py` | the SIP daily-bar dataset builder: `plan`, `fetch`, `build` |
| | `quality.py` | the read-only pre-run data-quality report |
| `swing_eval/` | `__init__.py` | the locked values R9–R13 |
| | `manifest.py` | the versioned evaluation manifest (R14) and its checks |
| | `analysis.py` | R9 stability, R10 comparison, R11 eligibility, the blocking validation findings |
| | `evaluate.py` | the offline evaluator and the evidence writer (R16, R17) |

* **No existing module is edited.** The IEX builder (`value_quality/ingest.py`), the global `ALPACA_DATA_FEED` setting, the harness,
  `swing_pullback/`, the registry, config, routes and every live path are byte-identical. The only edited document is
  `DESIGN_78_SWING_PULLBACK.md` (section 9: M-6 and M-1, wording only).
* **Isolation.** Neither package imports `config`, the IEX builder, an execution, broker, Robinhood, store, service or market-intelligence
  module, or the harness CLI / store / service. Nothing outside the two packages and the tests imports them. The network library is imported
  only inside `sip_bundle.http_transport`, which allows GET requests to the two allowed URLs only.
* **No persistence, nothing in the repository (R16, R17).** JSON files only. No SQLite, no harness store. Every writer (plan, fetch, report,
  manifest, evaluator) refuses a database file name and any place inside the repository; the repository test compares files
  (`os.path.samefile` on every existing ancestor), so a `\\?\` or `\\localhost\C$` spelling of a repository path is refused too. The
  evaluator refuses every socket and SQLite connection while the runs execute.

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
* **The fetch.** Every request is checked before the first one is sent:
  * an allowed URL; the SEC request takes no parameter;
  * a bar request has exactly the eight parameters above, `feed=sip`, `1Day`, `sort=asc`, a page size within the limit, the plan's window,
    an allowed adjustment equal to the request's, and the request's own symbol list, inside the universe;
  * a window that does not end before today (UTC) is refused: the current session's bar may be incomplete.

  Credentials (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `SEC_USER_AGENT`) are read by name from an explicit env file, else the environment.
  They are never printed or stored: the manifest keeps URLs, parameters and hashes, never headers or exception texts. The real transport
  never follows a redirect (a 3xx is a non-200), so the Alpaca headers cannot follow one to another host. Requests are paced (0.35 s; SEC
  0.15 s). A non-200 response or a transport error stops the fetch with no retry. More than 60 pages in one request stops it as a
  materially broader request than approved. In each case the manifest is written as incomplete, and `build` refuses it. An existing
  non-empty folder is never overwritten. The manifest records whether the network transport was used.
* **The build.** It refuses:
  * a bar not stamped at midnight New York (`T04:00:00Z` in daylight time, `T05:00:00Z` otherwise);
  * a duplicate bar;
  * a symbol the request did not ask for;
  * any page whose bytes differ from the recorded sha256;
  * SYNTHETIC for an archive fetched over the network (it is HISTORICAL; the CLI always builds HISTORICAL);
  * an existing `sip_bundle.json` (never overwritten).

  The bars are the all-adjusted series. `aux.series` carries `raw_close` and `raw_volume` (raw) and `split_ratio` (raw / split-adjusted
  close). `aux.static.issuers` maps SEC CIKs to symbols; a symbol without a CIK is listed (and the swing baseline then refuses the dataset).
  A bar with a non-finite, non-positive or inconsistent OHLC field is removed, leaving a gap, and listed; it is never repaired.
* **Provenance.** `volume_feed: "SIP"`, builder `sip_daily_v1`, the window, the plan and raw-manifest hashes, the fetch's approval
  reference, and the universe statement `SURVIVORSHIP_BIAS_PRESENT`.
* **Limit.** A dataset's kind is declared by the build. Someone able to edit the files could relabel one; the manifest's dataset sha256, which
  the operator approves, is the control for that.

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
| Q04 calendar | a date that is not a benchmark session; the benchmark calendar differs from the NYSE table (extra, missing, weekend, uncovered); the benchmark does not hold exactly the sessions of the builder's requested window (a session missing at an edge) | | equal |
| Q05 missing bars | | sessions without a bar inside a symbol's own span | the share of complete 200-session windows |
| Q06 raw series | no `raw_close` (final-review M-4); SIP without `raw_volume` | bar dates without a raw close; on SIP, bar dates without a raw volume | IEX without `raw_volume` |
| Q07 issuers | an unmapped symbol; a symbol under two issuers | a kept listing that starts later than another (M-7) | each multi-listing issuer, its listings and the kept one |
| Q08 corporate actions | | each raw / split / adjusted inconsistency | split-ratio changes |
| Q09 bars | | zero-volume bars | bars removed by the builder |

* **Status.** BLOCKED, REVIEW_REQUIRED or CLEAN, with counts.
* **Sealing.** The report carries its own `report_sha256`, the provenance override and the approval reference it was made under, and the
  CLI never overwrites a report.
* **Approval.** A report on a HISTORICAL dataset needs an approval reference.

## 5. The evaluation manifest (R14, `swing_eval/manifest.py`)

* **What it is.** One JSON document (`stage78b_eval_v1`) with its own `manifest_sha256`. It pins what the strategy's pre-registration hash
  does not cover.
* **What it holds.**
  * The V1 / C1 ids and versions, the pre-registration hash `6c20fdb4…3cfa61903`, the code commit and the harness version.
  * The dataset: file sha256, dataset id, kind, `volume_feed`, builder, the provenance override, and the expected symbols / sessions / span.
  * The accepted pre-run report's sha256 and status, and the provenance it was made under (override, dataset id, volume feed, kind).
  * Split 0.70 and the boundary policy; arm RULES; the execution assumptions; the sizing.
  * The four runs with their full friction.
  * The R9–R11 thresholds and the R12 / R13 fee records.
  * The evidence location.
* **`verify()`.** Checks the manifest against the strategy code and the locked values. Recomputing the hash after a change does not help:
  every value is compared with the code's. It also requires:
  * a `sip_daily_v1` dataset with `volume_feed` SIP (R14: "feed SIP"); an IEX bundle is refused (allowing one would need a lock amendment);
  * a provenance override that sets none of `volume_feed`, `builder`, `source`, `window`, `plan_sha256`, `raw_manifest_sha256`;
  * a pre-run report made under the same provenance as the one evaluated, so a report run under another override cannot clear a BLOCK.
* **`pre_checks()`** (before any run): the checked-out commit, a clean working tree, the dataset file's sha256 and content, and the accepted,
  unaltered report file, made under the evaluated provenance.
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
2. The output folder is the manifest's evidence location. It must be **outside the repository**, not a database path, and new or empty.
3. A HISTORICAL dataset needs an approval reference **and** the sha256 of the approved manifest, and its evidence goes to a new folder
   directly under `C:\Data Analyst Py\DA IDX\evidence\stage-78B\` (R17; the manifest CLI's default is `<that folder>\<UTC timestamp>`).
   SYNTHETIC datasets are for tests.
4. The pre-checks pass. The checked-out commit and the clean working tree are read from the `.git` files, because product code never
   starts a process (the Stage 3.9 rule). The tree check compares every tracked file with its git index blob (a line-end-only difference is
   not a change), and finds deleted and unmerged files and untracked `.py` files beside tracked ones. **Limit:** a change staged but not
   committed is not seen without git's object store, so the operator builds the manifest on a checkout whose `git status` is empty.
5. The four pre-registered runs of `swing_pullback.plan.run()` execute, and the eight sensitivity neighbours are regenerated on the TRAIN
   view. Every socket and SQLite connection is refused meanwhile (`socket.socket`, `create_connection`, `getaddrinfo`, `_socket.socket`,
   `sqlite3.connect`, `sqlite3.dbapi2.connect`, `_sqlite3.connect`). Each attempt is recorded, so the evaluation is refused even if the run
   path caught the error; the refusal is not an `OSError`.
6. The post-checks pass.
7. The blocking validation findings are collected. **M-5:** a non-positive stop (`invalidation_reference` ≤ 0) in any candidate of the
   four runs or of the eight TRAIN-view neighbours. Also a CRITICAL causality report, a failed audit, or a candidate-set mismatch.
8. R9, R10 and R11 are computed.
9. The evidence is written.

**Status.**
* `INVALID`: a post-check failed.
* `BLOCKED_VALIDATION_FINDING`: any blocking finding. No R9, R10 or R11 outcome is accepted. When both apply, the status is
  `INVALID + BLOCKED_VALIDATION_FINDING`; neither hides the other. Each finding lists the version,
  configuration, cost run, symbol, signal date, signal close, ATR(14) and stop. The stop calculation, the rules and the candidate set are
  never modified, clipped or filtered, and the run records are kept as produced. Resolution is the operator's decision.
* `COMPLETED`: outcomes accepted. Even then, "eligible for review" is never a promotion.
* **Not accepted means marked.** Otherwise `stability.json`, `comparison.json`, `eligibility.json`, `summary.json` and the summary memo carry
  `accepted: false` and "NOT ACCEPTED (diagnostic only)": R10.1 compares no INVALID or BLOCKED run.

**Evidence.**
* JSON files: `manifest.json`, `pre_run_report.json`, `dataset.sha256`, `runs/{V1,C1}_{BASE,STRESS_2X}.json`, `validation.json`,
  `stability.json`, `comparison.json`, `eligibility.json`, `environment.json`, `summary.json`.
* A plain-language `RESULTS_SUMMARY.md`.
* `evidence.sha256`, which indexes every file.
* The approval reference, the approved manifest sha256 and the evaluation time are recorded (`summary.json`, `environment.json`, the memo).

## 7. The analysis (`swing_eval/analysis.py`)

* **R9 fold stability.** The gate is V1 BASE; the others are reported. The six folds of `swing_pullback.diagnostics.fold_bounds` are used
  unchanged.
  * A fold is evaluable with ≥ 20 closed trades.
  * ≥ 5 of the 6 folds must be evaluable; otherwise INSUFFICIENT.
  * Net P/L > 0 in ≥ ⌈2/3 × evaluable⌉ folds.
  * Median evaluable-fold mean R > 0.05.
  * No fold holds ≥ 80 % of the total net P/L.
  * Outcomes: STABLE / UNSTABLE.
  * **Interpretation (for the operator's confirmation):** the lock does not define the 80 % rule when the total net P/L is ≤ 0. The code
    fails closed: such a run is never STABLE (`concentration: NOT_SATISFIABLE`), which is also what a literal reading of the rule gives.
* **R10 V1 versus C1.**
  * **Population:** the closed RULES trades of each run, R = `r_net`. Boundary-purged candidates and simulation skips are counted and
    reported, not compared. DATA_END exits count as trades and are counted.
  * **Trade counts:** ≥ 20 OOS trades and ≥ 30 in total for each version in each cost run, else INCONCLUSIVE.
  * **C-a:** Δ_R(OOS) ≥ 0.05 in both cost runs.
  * **C-b:** ≥ 4 paired folds (BASE run, both versions ≥ 20 trades), with V1 ahead in ≥ ⌈2/3⌉ of them.
  * **Outcomes:** BEATS_CONTROL (C-a and C-b); NO_ADDED_VALUE (Δ_R ≤ 0 in either cost run); INCONCLUSIVE otherwise.
  * **Reported:** exposure, drawdown, costs, TRAIN versus OOS, and a joint calendar-month bootstrap of Δ_R: the calendar months of the
    signal date that hold an OOS trade of either version are resampled with replacement, the same months for both versions (2,000
    resamples, seed 78, nearest-rank 5 / 50 / 95 percentiles, descriptive; the lock's "moving-block" is this month-block scheme).
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
  6. the pre-run report accepted and no blocking finding: CLEAN, or REVIEW_REQUIRED with a recorded acceptance. For a HISTORICAL
     dataset the acceptance is the operator's approval of the manifest, which pins the report's sha256 and its findings; it is written into
     the evidence.

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

## 9. Final-review M-6 and M-1 (wording only)

`DESIGN_78_SWING_PULLBACK.md` now says what the harness and the code do:
* M-6: the entry is the open of the symbol's next bar; the time exit is the close of the 10th bar of the symbol in the trade; the TRAIN-view
  purge uses the neighbour's own `max_hold_bars` (8 / 12).
* M-1: the strict comparisons with a mean are exact on the stored doubles, not on decimal prices; a near-tie on a series spanning about
  10⁶ in adjusted price can be decided wrongly.

No rule, constant or code changed. The pre-registration hash is unchanged, and the `swing_pullback/` docstrings are left untouched so that
the package stays byte-identical.

## 10. Tests (synthetic only)

`tests/test_sip_eval_78b1.py`. Every output goes to a temporary folder, and the transport is a fake provider in the Alpaca / SEC response
shapes.

| Group | Covers |
|---|---|
| A | the locked values; the strategy fingerprint |
| C | the calendar |
| B | the builder: the plan, every fetch refusal before any request (scope, window, output place), pagination, the page guard, no retry, transport errors, no stored or printed credential, the real transport (no redirect, fake library), the build and its refusals, the IEX path untouched, the CLIs |
| Q | the report: CLEAN, every BLOCK, every FINDING, the requested window, raw volume gaps, the approval gate and record, no strategy computation |
| M | the manifest: verify (SIP builder only, protected overrides, the report's provenance), tamper detection even with a recomputed hash, pre-checks, post-checks per field, the CLI |
| E | the evaluator: end to end with the evidence index verified, every refusal before a run, the offline guard (every entry point, a swallowed attempt), INVALID and BLOCKED marked NOT ACCEPTED, M-5 blocking from a base run and from a TRAIN-view neighbour alone, the real tree check |
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
6. On a checkout whose `git status` is empty: `python -m swing_eval.manifest build`; the operator approves the manifest's sha256.
7. On a clean checkout of the manifest's commit: `python -m swing_eval.evaluate` with the approval reference and the approved manifest
   sha256, writing to the manifest's evidence folder.
