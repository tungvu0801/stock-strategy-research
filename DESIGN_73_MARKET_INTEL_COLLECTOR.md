# Stage 7.3 — market intelligence collector and scheduling (offline)

Stage 7.3 adds the collection and scheduling layer that will one day connect the Stage 7.2 adapters to the Stage 7.1 store. In this
stage it is **code only**, and nothing runs it:
- no transport ships, no daemon thread starts, and no scheduled job, config key, route or credential is added;
- every request a test sends goes to a fake transport, and the default transport refuses the network;
- nothing in the live path imports it.

Base: main `7376568` (PR #43 merged).

## 1. Scope against the original design

The master design (`DESIGN_MARKET_INTELLIGENCE_SCANNER.md` E.1) defines 7.3 as **"W1: daemon + read-only API"**. That row includes:
- a marked settings block in the frozen `config.py`;
- a router include and a lifespan start in `api/server.py`;
- a daemon thread under a lease;
- GET routes under `/api/market-intel`.

The operator's 7.3 instruction approves only the offline collector and scheduler, and forbids daemon activation and config changes.
So:

| In this stage (new files only, offline, tested) | Deferred: each needs an operator decision |
|---|---|
| per-source collection plans and polling cadences | **W1 wiring**: the `config.py` settings block, the `api/server.py` include and lifespan start, the daemon thread, the API routes |
| an injectable transport (fake in tests; the default refuses the network) | a real transport (on `evidence.net.fetch` or the Alpaca client) and every real request (section 7) |
| budgets (Stage 7.0 `ProviderBudget`, rebuilt from the 7.1 store's persisted events), concurrency leases, retry with exponential backoff and deterministic jitter, a circuit breaker | **a live-collection dataset kind** in the 7.1 store, a schema v2 migration (section 5) |
| persistent watermarks and run records in the collector's own state file | **non-bar historical downloads**, with their own approval (section 6) |
| pagination with loop and duplicate-page detection and page caps; idempotent ingestion through the unchanged 7.1 store | **news cluster and mention persistence**, a schema v2 (section 8) |
| fetch-attempt provenance (7.1 `mi_fetches`, the raw archive) and error classes | |
| feed health, availability, degraded and stale states | |
| deterministic scheduling on a fake clock; shutdown; recovery after an interrupted run; bounded sizes | |

**Reused, never duplicated:**
- **Stage 7.0:** the budgets (`settings.BUDGETS`, `budget.ProviderBudget`), the freshness policies (`freshness.assess`,
  `assess_earnings_calendar`), the validation codes (`validate.clock_skew`) and the calendar sessions.
- **Stage 7.1:** the store (datasets, fetches, members, seals, integrity), `RawArchive` and `ingest.recover_pending`.
- **Stage 7.2:** every adapter's `request` / `parse` and the CLI's adapter registry.

## 2. Collection plans (`collector/plans.py`)

One registered, frozen plan per source. A runner refuses any plan that is not exactly the registered one (`PLAN_NOT_REGISTERED`), so a
caller cannot loosen a cadence, cap or block by passing an edited copy.

| Source | Cadence (New York time) | Budget (7.0) | Feed-health policy | Pages / bytes per run | Blocked? |
|---|---|---|---|---|---|
| `alpaca_news` | every 120 s in PRE / REG / POST of a market day | `MI_ALPACA_MAX_PER_MIN` 40 | 7.0 `NEWS_POLL` (FRESH ≤ 3 min, AGING ≤ 10 min) | 20 / 2 MB | — |
| `alpaca_snapshots` | every 60 s in REG; every 300 s in PRE from 07:00 | `MI_ALPACA_MAX_PER_MIN` 40 | 7.0 `QUOTE_SWING` numbers (60 / 300 s), on the last successful poll | 1 / 2 MB | — |
| `halts` | every 60 s, 04:00–20:00 on market days | `MI_HALTS_MAX_PER_MIN` 1 | 7.0 `HALT_FEED` (2 / 3 min) | 1 / 2 MB | — |
| `sec_filings` | every 600 s, 06:00–22:00 on weekdays | `MI_SEC_MAX_PER_MIN` 60 | 7.0 `SEC_POLL` (15 / 30 min) | 1 / 5 MB | **yes**: `SEC_ACCEPTANCE_TZ_UNVERIFIED` |
| `sec_facts` | daily at 03:30 | `MI_SEC_MAX_PER_MIN` 60 | 7.0 daily-refresh rule (`assess_earnings_calendar`: same New York day, ≤ 36 h) | 1 / 20 MB | **yes**: SEC ingestion awaits the same verification |
| `fred` | daily at 07:00 | `MI_FRED_MAX_PER_MIN` 30 | 7.0 daily-refresh rule | 5 / 5 MB | — |
| `earnings_av` | daily at 06:30 | `MI_ALPHA_VANTAGE_MAX_PER_DAY` 6 a day | 7.0 `EARNINGS_CALENDAR` | 1 / 5 MB | — (NOT_CONFIGURED: no key) |

The cadences are design C.8's. Every freshness number is an existing Stage 7.0 policy (decision D); none is new.

**Snapshot poll health.** The collector grades its own snapshot polling against the `QUOTE_SWING` numbers, measured from the last
successful poll. Grading a consumer's quote against a quote time stays the 7.0 policy's job.

**Daily jobs.** These are FRED, SEC facts and the earnings calendar. They reuse the 7.0 daily-refresh rule:
- FRESH when refreshed on the current New York date;
- AGING within 36 h;
- STALE after that.

**FRED's read (a proposal to confirm).** FRED is not watermark-windowed. A monthly or quarterly series' observation date lies weeks
before its release, so a short observation window would never see it. The daily refresh therefore reads:
- the last **400 observation dates**, with every vintage (ALFRED realtime 1776-07-04 to 9999-12-31);
- at most 10,000 rows a page and 5 pages a run.

An identical fact is a DUPLICATE, a new vintage is a new fact, and a vintage that was closed (its `realtime_end` set) is a revision.

**News** is the only windowed source (section 6). Snapshots, halts and the earnings calendar are current-state polls.

Each plan also bounds its storage. Rows per run are capped (`max_rows_per_run`, never above the store's `MAX_DATASET_ROWS`), and
`daily_row_bound(plan)` gives the most rows the cadence can write in a day; it is tested.

## 3. A run (`collector/runner.py`)

`collect(plan, scope, ...)` performs one bounded collection.

**Refusals before anything is sent.** No request goes out, no dataset is created and no lease is claimed when any of these holds.
They are checked in this order, and the result is `REFUSED` with the code:
- the plan is not the registered object (`PLAN_NOT_REGISTERED`): an edited copy, a look-alike or a bare name is refused;
- the plan is blocked (`BLOCKED_PENDING_APPROVAL`): both SEC plans;
- the scope is not one of the plan's (`SCOPE_INVALID`): wrong symbols, feed or series, or a window for a source that has none;
- its credential is missing (`NOT_CONFIGURED`), checked by presence only. An injected predicate must answer exactly `True`, and a value
  is never read;
- the transport is anything but the fake (`REAL_NETWORK_NOT_APPROVED`);
- the sink would be anything but a FIXTURE dataset (`LIVE_INGESTION_NOT_APPROVED`, section 5);
- a stop was requested (`SHUTDOWN`);
- our clock is behind the pair's watermark (`CLOCK_BEHIND_WATERMARK`), or the window starts before the plan's live lookback
  (`HISTORICAL_WINDOW_NEEDS_APPROVAL`, section 6);
- the circuit is open, or its one trial run is in flight (`CIRCUIT_OPEN`, `CIRCUIT_TRIAL_RUNNING`);
- the budget refuses (`BUDGET_EXHAUSTED`, `RATE_LIMITED`, or `ACCESS_DENIED_THIS_CYCLE` after a 401 or 403);
- another worker holds the pair's lease, or the provider's (`LEASE_HELD`, `PROVIDER_CONCURRENCY`).

**The run, in order:**
1. **Lease.** One lease per (source, scope), and at most `plan.concurrency` per provider. A lease expires after `LEASE_TTL_S`
   (1800 s), so a crashed worker never blocks forever.
   - The breaker gate is re-checked, and a half-open trial claimed, in the same state transaction.
   - A run stops at the next page boundary after `MAX_RUN_S` (900 s), well inside its lease.
2. **Dataset.** A 7.1 store dataset (FIXTURE) and a run record in the state file.
3. **Pages**, up to `max_pages`. For each page:
   - **Request.** The adapter's request goes to the transport, wrapped in the retry policy.
   - **Record.** Every attempt is recorded as a 7.1 fetch row with its error class, and its budget spend is persisted
     (`mi_budget_events`). Every response body is archived (the 7.1 `RawArchive`), except a body above the cap, which is never kept.
   - **Parse.** The adapter parses the page (it never raises).
   - **Problems.** Row problems become REJECTED members; a page problem makes the fetch `INVALID_RESPONSE` and ends the run.
   - **Ingest.** Rows are ingested in quarantine mode.
   - **Stop conditions:** the page loop also stops on a repeated token (`PAGINATION_LOOP`), a page identical to the previous one
     (`DUPLICATE_PAGE`), the page cap with a token left (`PAGE_LIMIT`), the row cap (`ROW_LIMIT`), or a shutdown request (`SHUTDOWN`).
4. **Seal.** The dataset is sealed `COMPLETED` only with no failure, rejection or stop, `EMPTY` when nothing came, and `PARTIAL` or
   `FAILED` otherwise. An exception becomes the stop reason and the dataset is still sealed. A `KeyboardInterrupt` is re-raised after
   the seal.
5. **Watermark.** It is advanced **only after a COMPLETED or EMPTY seal**, that is, after every page was fetched, validated and
   ingested (EMPTY means the provider had nothing).
   - It is written in the same state transaction as the run end.
   - For news it is the window end; for a current-state poll it is the run start.
   - Schema triggers refuse a watermark (section 4): one for a run that did not end COMPLETED or EMPTY, one later than its run's end,
     one earlier than the current watermark, and a second one for the same run.
6. **Breaker.** The circuit breaker is updated, the lease released, and the run ended.

**Error classes:**

| Outcome | Fetch status | Retried? | Effect on the breaker |
|---|---|---|---|
| timeout | `TIMEOUT` | yes | counts as a failure |
| connection or OS error | `ERROR` | yes | failure |
| HTTP 429 | `RATE_LIMITED` | yes, after `max(backoff, Retry-After ≤ 30 s)` | failure; budget back-off and plan pressure (7.0) |
| HTTP 5xx | `HTTP_ERROR` | yes | failure |
| HTTP 401 / 403 | `ACCESS_DENIED` | **no** | blocked for the cycle (7.0) |
| other 4xx | `HTTP_ERROR` | no | failure |
| body above the cap | `TOO_LARGE` | no | failure |
| HTTP 200 with a page problem (error envelope, bad shape) | `INVALID_RESPONSE` | no | failure |
| HTTP 200 and parsed | `OK` | — | success |

**Retry.** Exponential backoff, `base 2 s × 2^(attempt−1)`, capped at 60 s, with ±20 % jitter, over 3 attempts. The jitter is
**deterministic**, a hash of the request and the attempt, so a fake-clock run is exactly reproducible.

**Circuit breaker.**
- **A run fails** when one of its requests failed and was not recovered by a later identical attempt. A recovered timeout does not
  count, and neither does a quarantined row, a stop or an ingestion error: none of them is a provider failure.
- One breaker covers each source, all its scopes included (fail closed).
- A run that made no request proves nothing: it neither fails nor closes the breaker, and a trial without a request is handed back
  (OPEN again).
- It opens after 3 consecutive failed runs, with a 300 s cool-down that doubles up to 3600 s.
- After the cool-down it goes half-open, allowing one trial run: a success closes it, a failure opens it again.
- Every transition is persisted, so a restart keeps the state.

**Budgets.** A run's budget is the 7.0 `ProviderBudget`, its daily count **rebuilt from the 7.1 store's persisted `mi_budget_events`**.
That closes the 7.1 note "not rebuilt across a restart".
- A daily cap's count is re-read from the store before every run, and again under the provider lease. So neither a restart nor a
  second collector sharing the files ever spends past it.
- A run waits at most 60 s in all for its own budget (a token, or a Retry-After back-off); after that it stops (`BUDGET_<reason>`).
- An ACCESS_DENIED provider stays blocked until the next collection cycle, and the scheduler starts a new cycle each New York day.

**Clock skew (decision A).** The Stage 7.1 chronology guarantee is kept exactly:
- the 2 s tolerance is never raised, and no provider timestamp is ever rewritten;
- a row observed after our retrieval is REJECTED by the store, so the run is PARTIAL and the watermark stays where it was;
- the HTTP `Date` header is compared with our clock (`validate.clock_skew`), and either symptom marks the source `CLOCK_SKEW` in health;
- because the watermark did not advance, the next poll covers that window again, and the row is stored once our retrieval time has
  passed it.

## 4. The collector state file (`collector/state.py`)

The state file is `runtime/market_intel/collector.db`, outside git (`runtime/` is ignored).
- **Separate file.** It is its own SQLite file because the 7.1 store's schema verification refuses any object it does not own.
- **Writes.** It is created lazily, uses WAL, runs every write in one `BEGIN IMMEDIATE` transaction, keeps every table append-only
  through triggers, and verifies its schema objects on open.

| Table | Content |
|---|---|
| `cs_runs` | one row per run start: source, scope, window, gap, worker, lease, plan hash, dataset id, trial flag |
| `cs_run_ends` | the end of a run: status, stop reason, pages, rows, at |
| `cs_watermarks` | the watermark log. The current watermark is the latest row per (source, scope). **Triggers refuse a row whose run did not end COMPLETED or EMPTY, one later than its run's end, one earlier than the current watermark, and a second row for a run.** |
| `cs_leases` and `cs_lease_releases` | lease claims with expiry, and their releases |
| `cs_breaker_events` | circuit-breaker transitions (only changes are stored) |

**Restart-safe.**
- The state file is consistent on its own, since every write is atomic.
- The store and the state file are written in a fixed order: ingest, then seal, then watermark. A crash between the seal and the
  watermark therefore only re-collects that window, and that is harmless because ingestion is idempotent. A re-delivered fact is a
  DUPLICATE member, and a changed one is a revision.
- `recover()` marks runs with no end `INTERRUPTED` and seals the store datasets they left open, through 7.1 `recover_pending`, which
  never touches a recent run.

## 5. Live ingestion needs a schema decision

The 7.1 store has three job kinds:
- **BACKFILL:** bars only, approval-bound;
- **IMPORT:** backfilled rows only;
- **FIXTURE:** tests.

None of them describes live collection. Using IMPORT would mislabel live rows as backfilled, and FIXTURE is for tests. Real
collection therefore needs a **schema v2 migration** with a `COLLECT` job kind (and its scope rules) in the 7.1 store. That is a
contract change to a merged stage, so this stage does not make it. **The runner refuses any sink job kind but FIXTURE.**

## 6. Non-bar historical downloads (decision C): a separate approval, proposed

The 7.1 BACKFILL authority (`mi_backfill_approval_v1`) is built on bar plans and **is never accepted for any other source**: the
collector has no parameter that takes it.
- **News**, the only windowed source, may start a live run's window no earlier than 36 h ago (`lookback_max_s`). An earlier window is
  refused as `HISTORICAL_WINDOW_NEEDS_APPROVAL`.
- **An outage longer than that** leaves a recorded **gap**, `[watermark, now − 36 h]`, shown by health. Only a historical approval
  could fill it.
- **FRED's fixed 400-day observation window** (section 2) is the one current-state read that reaches back. It is listed for
  confirmation.

**Proposed for a later stage: `mi_source_backfill_approval_v1`.**
- **The file.** It is a separate approval file per source and scope, naming the source, scope, window, `max_requests`, `max_rows` and
  the credential and terms it relies on (`terms_accepted`).
- **Validity.** It is valid for at most 24 h and used once.
- **Checks.** The store checks it the same way it checks bar backfills (file re-read, plan recomputed, scope equal).

Implementing it requires the schema v2 of section 5.

## 7. Approvals and credentials before the first real request (decision E)

| Provider | Needed before any real request |
|---|---|
| all | **the transport stage itself**; the W1 wiring decision; the schema v2 `COLLECT` decision |
| Alpaca (news, snapshots) | approval to use the existing `ALPACA_API_KEY` for the data layer within the 40 requests a minute self-cap; for SIP snapshots, `SIP_FEED` |
| SEC (filings, facts) | `SEC_USER_AGENT` configured ("Name email") and confirmed; **verification of `acceptanceDateTime`'s timezone**, then approval (decision B). Until then both SEC plans are blocked |
| FRED | `FRED_API_KEY` confirmed; per-series licensing (VIXCLS and others) accepted |
| Nasdaq Trader | the halt RSS terms accepted (at most once a minute) |
| Alpha Vantage | a **new** `ALPHAVANTAGE_API_KEY` and its terms of use; config declares no such key, so the source stays NOT_CONFIGURED |

No credential is added, read or activated here. Plans name credentials, and a run checks only their presence, through an injected
function that defaults to "nothing configured".

## 8. News clusters and ticker mentions: future persistence (decision F)

Stage 7.2 computes clusters (`news/dedup.py`) and mentions (`news/entities.py`) on demand. Persisting them needs append-only tables in
the 7.1 store through a **schema v2 migration**. Nothing here changes the v1 schema. The proposed tables:
- **`mi_news_clusters`:** cluster id, representative, rule, member keys, the dedup thresholds' version and the computation time;
- **`mi_news_mentions`:** observation id, symbol, method, confidence, role, the SEC map version and the alias version.

Both would be recomputed whenever a new article, the SEC map or the press allowlist changes. Each recomputation is a new row, never an
overwrite.

## 9. Health (`collector/health.py`)

`source_health(plan, scope, ...)` reports, per source and scope:
- **`status`**, one of:
  - `AVAILABLE`;
  The first status that applies wins, in this order:
  - `BLOCKED_PENDING_APPROVAL`;
  - `NOT_CONFIGURED`;
  - `CIRCUIT_OPEN` (open, or its trial in flight);
  - `BUDGET_EXHAUSTED`;
  - `UNAVAILABLE`: never succeeded, freshness UNAVAILABLE, or access denied this cycle;
  - `STALE`: freshness STALE under the 7.0 policy;
  - `DEGRADED`: the latest ended run was not COMPLETED or EMPTY, or it saw clock skew, while freshness is still within policy;
  - `AVAILABLE`;
- **the 7.0 freshness assessment**, measured from the start of the last successful (COMPLETED or EMPTY) run;
- **gaps**: windows no complete run covered;
- **`last_success_at`, `last_attempt_at`, `consecutive_failures` and the breaker state;**
- **`clock_skew` and the last error.**

A stale source is reported as stale. Values already stored stay history, and their own point-in-time status comes from the 7.1
store's `latest_status`.

## 10. Scheduling (`collector/scheduler.py`)

- **`due(now)`** lists the (plan, scope) pairs whose cadence window is open at `now`, and whose last run started at least one interval
  ago. A daily job is due once per New York day, after its time. A failed daily run is not retried the same day, and health shows it
  as DEGRADED.
- **Each New York day** starts a new collection cycle.
- **`tick()`** runs the due pairs **sequentially**, in a fixed order (plan, then scope), each under its lease. A repeated tick in the
  same interval runs nothing.
- **Two workers on one state file** never run the same pair twice: the second finds the lease held, and later finds the run already
  recorded.
- **`request_stop()`** ends the current run at the next page boundary (`SHUTDOWN`, sealed PARTIAL, watermark unchanged); later pairs do
  not start.
- **No thread.** A scheduler is a plain object driven by a caller's clock. The fake clock in tests makes every tick reproducible.

## 11. Bounded resources

| Limit | Value |
|---|---|
| response bytes per plan | 2 MB–20 MB (`TOO_LARGE`) |
| pages per run | ≤ 20 |
| rows per run | `max_rows_per_run`, never above `MAX_DATASET_ROWS` |
| requests | the 7.0 budgets (per minute, per day) |
| rows per day | `daily_row_bound(plan)` |

Responses are processed one page at a time, so nothing accumulates across pages beyond the run's counters.

## 12. Bookkeeping

- **New only.** `BASE_73` is main `7376568`. `NEW_73` covers `market_intel/collector/*`, the tests and this note. `SEMANTIC_73` and
  `INSTRUMENTATION_73` are empty.
- **`EDITED_73` is bookkeeping only:**
  - the CI manifest;
  - the earlier F01 unions;
  - the Stage 7.0 and 7.1 F01 `later` sets;
  - the 7.1 module registry, which accepts the modules `NEW_73` declares;
  - the Stage 7.2 F tests, which now read what 7.2 left at `BASE_73`;
  - `tests/freeze_ledger.py`.

## 13. Known limits of this offline stage

- **Late news edits.** An edit made after the 10-minute overlap is not re-read, because the 7.2 parser's range rule is on
  `created_at`. Catching later edits needs an `updated_at`-based read, which is not designed yet.
- **A bad row holds the watermark.** A permanently malformed or conflicting news row keeps the watermark where it is (fail closed)
  until it ages out of the 36 h lookback. Meanwhile the source is DEGRADED, and the uncovered window is then recorded as a gap.
- **Busy windows.** More than 20 pages in one window is `PAGE_LIMIT`: the watermark stays and the window is read again. Raising the
  cap is a declared change.
- **State growth.** The state file is append-only and grows by about `runs_per_day_bound × 5` rows a day. Retention is a later,
  declared change.
- **The per-minute bucket is in memory, per process.** After a restart, a provider may receive one minute's budget at once. Two
  processes each keep their own bucket: the provider lease serializes their runs but not their combined rate, so one collector process is
  assumed (the W1 decision). The daily caps are persisted and exact.
- **Budgets come from config.** The default budgets read config's settings, which are absent today, so every budget is its 7.0 default.
