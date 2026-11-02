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
| `research_data/` | `__init__.py` | builder constants, the two allowed URLs, the approval-reference form check, the output guard |
| | `nyse_calendar.py` | the static NYSE closure table 2016–2026 (R8) |
| | `sip_bundle.py` | the SIP daily-bar dataset builder: `plan`, `fetch`, `build` |
| | `quality.py` | the read-only pre-run data-quality report |
| `swing_eval/` | `__init__.py` | the locked values R9–R13 |
| | `manifest.py` | the versioned evaluation manifest (R14) and its checks |
| | `codetree.py` | the checked-out code read from the `.git` files: the commit, the index's computed tree, the working tree against the index |
| | `preflight.ps1` | the operator-side clean-tree preflight (git commands; PowerShell, outside the Python product code) |
| | `analysis.py` | R9 stability, R10 comparison, R11 eligibility, the blocking validation findings |
| | `evaluate.py` | the offline evaluator and the evidence writer (R16, R17) |

* **No existing module is edited.** The IEX builder (`value_quality/ingest.py`), the global `ALPACA_DATA_FEED` setting, the harness,
  `swing_pullback/`, the registry, config, routes and every live path are byte-identical. The only edited document is
  `DESIGN_78_SWING_PULLBACK.md` (section 9: M-6 and M-1, wording only).
* **Isolation.** Neither package imports `config`, the IEX builder, an execution, broker, Robinhood, store, service or market-intelligence
  module, or the harness CLI / store / service. Nothing outside the two packages and the tests imports them. The network library is imported
  only inside `sip_bundle.http_transport`, which allows GET requests to the two allowed URLs only.
* **No persistence, nothing in the repository (R16, R17).** JSON files only. No SQLite, no harness store. Every writer (plan, fetch, build,
  report, manifest, evaluator) refuses a database file name and any place inside the repository; the repository test compares files
  (`os.path.samefile` on every existing ancestor), so a `\\?\` or `\\localhost\C$` spelling of a repository path is refused too. The
  evaluator refuses every socket and SQLite connection while the runs execute.
* **Approval references are procedural records, not authorization.**
  * **What is checked.** Every gate that takes `OPERATOR-APPROVAL:<reference>` checks only the reference's form: the prefix and at least four
    more characters (`research_data.approved`). The reference is not looked up, signed or verified against any approval record, and any
    well-formed text passes.
  * **What is verified.** The sha256 supplied with the reference: a fetch runs only the plan whose own sha256 equals it, and a HISTORICAL
    evaluation only the manifest whose own sha256 equals it.
  * **What that achieves.** These controls stop a different plan or manifest from running by mistake, and they leave an audit record.
  * **What it does not.** They are **not cryptographic authorization**. Anyone who can run the commands can type a reference and compute a
    sha256, so the real control is the operator's procedure: approve a sha256 in writing, then supply it.
  * **Where it is recorded.** Every output that records a reference also records `approval_check`, which states all this
    (`research_data.APPROVAL_CHECK`): the fetch manifest, the built dataset's provenance (`fetch_approval_check`), the pre-run report,
    `summary.json`, `environment.json` and the summary memo.

## 2. The SIP builder (`research_data/sip_bundle.py`)

Three separate steps. Only `fetch` touches the network, and only with an operator approval.

| Step | Network | Gate | Output |
|---|---|---|---|
| `plan` | none | none | the exact request list and its sha256 |
| `fetch` | Alpaca market data (SIP daily bars) and the SEC ticker file, GET only | an approval reference `OPERATOR-APPROVAL:<reference>` (its form is checked) **and** the plan's sha256 (compared with the plan's own) | every page gzipped under `raw/`, `manifest.json` with each page's sha256 |
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
* **Approval.** A report on a HISTORICAL dataset needs an approval reference; it is checked for its form only (section 1).

## 5. The evaluation manifest (R14, `swing_eval/manifest.py`)

* **What it is.** One JSON document (`stage78b_eval_v1`) with its own `manifest_sha256`. It pins what the strategy's pre-registration hash
  does not cover.
* **What it holds.**
  * The V1 / C1 ids and versions, the pre-registration hash `6c20fdb4…3cfa61903`, the code commit and its tree (`code_commit`, `code_tree`),
    and the harness version.
  * The CLEAN record of the operator-side clean-tree preflight (`code_preflight`).
  * The dataset: file sha256, dataset id, kind, `volume_feed`, builder, the provenance override, and the expected symbols / sessions / span.
  * The accepted pre-run report's sha256 and status, and the provenance it was made under (override, dataset id, volume feed, kind).
  * Split 0.70 and the boundary policy; arm RULES; the execution assumptions; the sizing.
  * The four runs with their full friction.
  * The R9–R11 thresholds and the R12 / R13 fee records.
  * The evidence location.
* **Preparation: an enforced clean-tree preflight.**
  * **The script.** `python -m swing_eval.manifest build` requires `--preflight FILE`, the record written by
    `swing_eval/preflight.ps1 -Out FILE`. The script runs git and refuses, writing nothing, unless all eight checks find nothing:
    * staged changes (`git diff --cached HEAD`), unstaged changes (`git diff`), untracked files of any type, unmerged paths;
    * index entries hidden from `git status` by a flag (assume-unchanged, skip-worktree);
    * ignored code files outside `.venv/` and `__pycache__/`: `.py`, `.pyw`, `.pyc`, `.pyd`, `.so` (ignored code can still be imported;
      a sourceless `.pyc` too);
    * replace refs (`refs/replace/*`);
    * a non-empty `git status --porcelain --untracked-files=all`.

    The record holds the commit (`git rev-parse HEAD`), its tree (`git rev-parse HEAD:`) and every check as NONE.
  * **How git is run.**
    * Only reading commands are used: `rev-parse`, `diff`, `ls-files`, `for-each-ref`, `status`, `branch`. Nothing is staged, committed,
      checked out or configured. `git diff` may refresh the index's cached file timestamps; it never changes an entry.
    * Every call runs with `--no-replace-objects` (and `GIT_NO_REPLACE_OBJECTS=1`), because a replace ref can make `HEAD:` show another
      commit's tree while `git status` stays empty.
    * The variables that would redirect git (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
      `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`) are removed for the
      script's process.
  * **Where the record goes.** It must be written to a new file on a local drive, outside the repository, with no database name:
    * the path is normalised as Windows does (trailing dots and spaces dropped);
    * 8.3 short names are expanded before the comparison;
    * `\\?\` and UNC paths are refused.

    A junction is not resolved. A record placed inside the repository through one would still be an untracked file, so the next preflight
    (`-Manifest`, before approval) refuses.
  * **Why PowerShell, and why here.** The Stage 3.9 rule forbids `subprocess` in any non-test Python module, so the git checks are PowerShell.
    The script lives in the stage's own package because `scripts/` is pinned by the Stage 6.0–6.10A freeze tests.
  * **The re-check without git.** The manifest CLI checks the record against this checkout through `swing_eval/codetree.py`:
    * the record is CLEAN, every check NONE, and its repository is this one;
    * its commit is the checked-out commit;
    * its tree is the tree computed from the git index;
    * the working tree equals the index.

    A change made after the preflight is therefore refused too: a new commit, a staged change, an unstaged change, or an untracked or ignored
    importable file anywhere in the repository. That last case covers a new package directory, which would shadow a tracked module of the
    same name, and a sourceless `.pyc`. Only `.git`, `.venv` and `__pycache__` are not scanned.
  * **What the manifest pins.** Only then is the manifest built. It pins `code_commit` and `code_tree` and carries the record, so the manifest
    sha256 that the operator approves names the exact committed tree.
  * **Limits (procedural).**
    * The record is not signed.
    * Product code cannot confirm by itself that the record's tree is its commit's tree: that needs git's object store, so the link is git's
      answer in the preflight.
    * A hand-written record, or a direct call to `manifest.build()`, skips the script. Even then, the record must match the checkout's
      commit and index tree.
    * Code outside the repository is not checked: `.venv`, site-packages and `PYTHONPATH`. `environment.json` records the Python version
      only.
    * A deliberately crafted `__pycache__` file is not checked either. Python loads a cached `.pyc` whose header matches its tracked
      source's size and time.

    As an independent check before approving, `swing_eval/preflight.ps1 -Manifest FILE` repeats the git checks. It confirms that
    `code_commit` is the checked-out commit and that `git rev-parse <code_commit>^{tree}` equals `code_tree`. It writes nothing.
* **`verify()`.** Checks the manifest against the strategy code and the locked values. Recomputing the hash after a change does not help:
  every value is compared with the code's. It also requires:
  * a `sip_daily_v1` dataset with `volume_feed` SIP (R14: "feed SIP"); an IEX bundle is refused (allowing one would need a lock amendment);
  * a provenance override that sets none of `volume_feed`, `builder`, `source`, `window`, `plan_sha256`, `raw_manifest_sha256`;
  * a pre-run report made under the same provenance as the one evaluated, so a report run under another override cannot clear a BLOCK;
  * a CLEAN preflight record (`code_preflight`) whose commit and tree are the manifest's `code_commit` and `code_tree`.
* **`pre_checks()`** (before any run):
  * the checked-out commit, and the index's computed tree against `code_tree`, so that nothing is staged;
  * a clean working tree;
  * the dataset file's sha256 and content;
  * the accepted report file, made under the evaluated provenance.

   The report is **re-derived** from the dataset (with the recorded override and approval)
  and must be equal to the file, apart from its file name: the report's own sha256 has no key, so an edited report (a BLOCK dropped, its
  hash recomputed) is caught here. A HISTORICAL report without a valid approval cannot be re-derived and is refused.
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
   directly under `C:\Data Analyst Py\DA IDX\evidence\stage-78B\`, named by its UTC timestamp `YYYY-MM-DDTHHMMSSZ` (R17; the manifest
   CLI's default).
   SYNTHETIC datasets are for tests.
4. The pre-checks pass. The code is identified from the `.git` files (`swing_eval/codetree.py`), because product code never starts a process
   (the Stage 3.9 rule):
   * the checked-out commit equals the manifest's `code_commit`;
   * the tree computed from the git index (the tree `git write-tree` would write; nothing is written) equals `code_tree`, so nothing is staged;
   * the working tree equals the index:
     * every tracked file matches its index blob (a line-end-only difference is not a change);
     * no file is deleted or unmerged;
     * there is no untracked or ignored importable file (`.py`, `.pyw`, `.pyc`, `.pyd`, `.so`) anywhere outside `.git`, `.venv` and
       `__pycache__`;
     * no entry is hidden by an index flag (assume-unchanged, skip-worktree, intent-to-add);
     * there is no symlink or submodule.

   A split index, a sparse index and an index whose checksum does not match are refused. `environment.json` records the commit, the tree and
   this scope (`tree_check`). The runbook also runs `preflight.ps1 -Manifest` just before the evaluation; product code cannot require it.
5. The four pre-registered runs of `swing_pullback.plan.run()` execute, and the eight sensitivity neighbours are regenerated on the TRAIN
   view. Every socket and SQLite connection is refused meanwhile (`socket.socket`, `SocketType`, `create_connection`, `getaddrinfo`,
   `_socket.socket`, and `connect` / `Connection` of `sqlite3`, `sqlite3.dbapi2` and `_sqlite3`). Each attempt is recorded, so the evaluation is refused even if the run
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
* The approval reference with its `approval_check` statement, the approved manifest sha256 and the evaluation time are recorded
  (`summary.json`, `environment.json`, the memo).

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
     dataset the acceptance is recorded as the approval reference supplied with the approved manifest's sha256, a procedural record
     (section 1); the manifest pins the report's sha256 and its findings, and the record is written into the evidence. A SYNTHETIC dataset has
     no operator acceptance, so item 6 holds for it only on a CLEAN report.

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
| A | the locked values; the strategy fingerprint; the approval reference checked for its form only, and `approval_check` in every output |
| C | the calendar |
| B | the builder: the plan, every fetch refusal before any request (scope, window, output place), pagination, the page guard, no retry, transport errors, no stored or printed credential, the real transport (no redirect, fake library), the build and its refusals, the IEX path untouched, the CLIs |
| Q | the report: CLEAN, every BLOCK, every FINDING, the requested window, raw volume gaps, the approval gate and record, no strategy computation |
| M | the manifest: verify (SIP builder only, protected overrides, the report's provenance), tamper detection even with a recomputed hash, pre-checks, post-checks per field, the CLI |
| E | the evaluator: end to end with the evidence index verified, every refusal before a run, the offline guard (every entry point, a swallowed attempt), INVALID and BLOCKED marked NOT ACCEPTED, M-5 blocking from a base run and from a TRAIN-view neighbour alone, the real tree check |
| T | the code identity: the index's tree computed as git writes it (name order, modes, a staged change), entries hidden from `git status`, the preflight record against the checkout, the operator preflight on temporary repositories (every unclean state refused, replace refs included, nothing written), the manifest built only from it, `-Manifest` catching a manifest that names another tree, the evaluator's own reading of a checkout, untracked importable code anywhere |
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
6. On the checkout to be evaluated, prepare and check the manifest:
   * `swing_eval\preflight.ps1 -Out <file outside the repository>`;
   * `python -m swing_eval.manifest build --preflight <that file> ...`;
   * `swing_eval\preflight.ps1 -Manifest <the manifest>`.

   The operator then approves the manifest's sha256 in writing.
7. On that checkout, still clean: `swing_eval\preflight.ps1 -Manifest <the manifest>` again, then `python -m swing_eval.evaluate` with the
   approval reference and the approved manifest sha256, writing to the manifest's evidence folder.
