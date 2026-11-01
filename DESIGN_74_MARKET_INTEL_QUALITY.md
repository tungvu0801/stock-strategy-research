# Stage 7.4 — Market intelligence data quality and latency report (offline, read-only)

Base: `main` at `b9c3621` (PR #45, Stage 7.3.1, merged). New files only: `market_intel/quality.py`, `tests/test_market_intel_quality_74.py` and this
document. No existing module changes; the bookkeeping edits are listed in section 10.

## 1. Scope and numbering

The master design's stage table (E.1) assigns Stage 7.4 to the *data-quality and latency report*: display-only, no extra network,
`market_intel/quality.py`, acceptance "p50 / p95 latency; IEX vs SIP divergence when both exist; staleness histograms; budget usage;
`PLAN_LIMIT_PRESSURE`". The numbering is unchanged (7.5 remains the shared scanner).

One difference from the master design. E.1 also edits "7.3's own router file". That router does not exist: the Stage 7.3 W1 wiring
(daemon, API, startup hook) was deferred, and this stage's instructions forbid daemon and API wiring. Stage 7.4 is therefore a library
function and a CLI that print JSON or a text summary. A read-only route can be added by the deferred wiring stage; it is not built here.

## 2. Inputs, and why the report never opens them in place

The report reads up to three inputs, all named explicitly by the caller (no default path, so no production file is read unless the operator
names it):

| Input | What is read | Interface reused |
|---|---|---|
| the store (`--store`) | a v1 (Stage 7.1) or v2 (Stage 7.3.1) file | `schema_v2.status`, `MarketIntelStore` / `MarketIntelStoreV2` (`latest_status`, `quality_report`, `history`, `verify_integrity`) |
| the collector state (`--state`, optional) | the Stage 7.3 `collector.db` | `CollectorState.runs`, `breaker_events`; `collector.health.freshness`; `collector.plans.PLANS` |
| the raw archive (`--archive-root`, optional) | `anchors.jsonl`, `manifest.jsonl` and the gzipped bodies | `RawArchive.anchors`, `RawArchive.verify` |

**A read-only SQLite open is not read-only on disk.** Probed on this machine: opening a WAL-mode database with `mode=ro` creates `-shm` and
`-wal` beside it and leaves them after closing. Every store and the collector state use WAL, and the frozen 7.1 store's own read path
opens files that way. `immutable=1` writes nothing but silently ignores committed pages still in a `-wal`. Neither is acceptable for a
report that must leave its inputs untouched and must not miss data.

**The snapshot protocol** (`quality._snapshot`), per database:

1. If a `-wal` or `-journal` of non-zero size exists, a writer may be active or a checkpoint is pending: refused
   (`WRITER_MAY_BE_ACTIVE`). Nothing is copied.
2. The main file is copied byte for byte (plain file reads, SHA-256 computed on the way) into a private temporary directory. The copy is
   created exclusively.
3. If the file's size or modification time changed, or a journal appeared, during the copy: refused (`CHANGED_DURING_SNAPSHOT`), and the
   partial copy is removed.
4. Every read — this module's SQL and every 7.1 / 7.3.1 interface — runs on the copy. All sections therefore see one consistent snapshot.
   The temporary directory, and any side files SQLite creates there, are deleted when the report returns.

The raw archive is read with plain file reads; nothing is written there. Cost: temporary disk space equal to the store's size while the
report runs (section 9).

## 3. Point in time

The report is evaluated at an explicit instant `as_of` (T). A naive time is refused; an offset is normalised to UTC. `since` (optional)
bounds the window of the activity sections (latency, provider health, schedule coverage); it must not be after T.

| Data | Counts at T when |
|---|---|
| a value (observation row) | the store's own KNOWN rule: `observed_at <= T`, `event_end <= T`, `available_at <= T`, `ingested_at <= T`; reads that need "the" value use its latest revision known at T (the store's rule) |
| an absence | `available_at <= T` and `ingested_at <= T` (an absence without an event time still says "we looked") |
| a delivery (dataset member) | `at <= T` and `available_at <= T` |
| a dataset | `created_at <= T`; its status is its latest event recorded by T (otherwise `OPEN`) |
| a fetch | `ingested_at <= T` (and, in a window, `started_at >= since`) |
| a budget event | `at <= T` |
| a collector run | `started_at <= T`; its end only when `ended_at <= T` (otherwise it was still open at T) |

Two sections describe the **file**, not the moment, and say so in the report: `integrity` (a property of the stored bytes) and malformed
timestamps (a value that cannot be parsed cannot be placed in time). Test Q08 proves that a report at T is identical, section for section,
before and after later writes (new datasets, revisions, runs, fetches and budget events), and that the later facts do change a later report.

## 4. Sections

### 4.1 Evidence

Every figure is labelled by where its data came from. A dataset's `job_kind` decides its class:

| job_kind | class | can hold production observations |
|---|---|---|
| `FIXTURE` | `FIXTURE` | never |
| `COLLECT` (v2) | `OFFLINE_VERIFICATION` | never: the schema admits only `OFFLINE_VERIFICATION` claims (Stage 7.3.1) |
| `BACKFILL`, `IMPORT` | `OPERATOR` | the only class that can |
| a fetch with no dataset | `UNATTRIBUTED` | — |

The report states a scope (`NONE`, `FIXTURE_ONLY`, `OFFLINE_VERIFICATION_ONLY`, `FIXTURE_AND_OFFLINE_VERIFICATION`, `INCLUDES_OPERATOR_DATA`),
and the text summary prints it on its second line. A stream whose facts were delivered by datasets of more than one class is `MIXED_EVIDENCE`
(a warning: its latest value may come from a fixture). Evidence and coverage count every delivery, including re-deliveries of an unchanged
value (dataset members); latency counts each stored revision once, at its first delivery.

### 4.2 Freshness and staleness histograms

A stream is (kind, symbol, series, feed, adjustment), the scope of the store's own reads. For each stream the report gives:

- every timestamp of its latest known record **kept apart**: `observed_at` (source event time), `event_end`, `published_at`, `retrieved_at`
  (collection time), `available_at`, `ingested_at` and `last_delivered_at`, each with its age at T, and the list of those that are missing.
  A missing time is listed as missing; it is never replaced by another one, and it is never reported as stale;
- the store's own status (`latest_status`: the latest known record next to the latest usable value; an absence or an unusable latest record
  is `UNAVAILABLE`, never a stale value);
- the Stage 7.0 grade, by the policy that applies:

| Kind | Graded by |
|---|---|
| bars | the 7.1 `latest_status` freshness (daily bar: the last completed session's bar; minute: bar end; hour: bar start) |
| quote snapshots | `QUOTE_DAY` and `QUOTE_SWING` from the quote time (both are reported: they serve different consumers) |
| news, halts, earnings, macro | their **feed**: the 7.3 health rule, the plan's 7.0 policy measured from the start of the last successful, verified run (needs the collector state) |
| filings, facts | not graded: `SEC_ACCEPTANCE_TZ_UNVERIFIED` (section 8) |

The histogram counts streams and feeds per policy key by status and freshness class (the classes are the pre-registered FRESH / AGING / STALE
boundaries, not new bins), and `event_age_s` gives the distribution of the latest event's age per kind.
The check warns on `STALE_STREAMS`, `UNAVAILABLE_STREAMS`, `STALE_FEEDS`, `UNAVAILABLE_FEEDS` and `MIXED_EVIDENCE`. It is `INSUFFICIENT_DATA`
when nothing could be graded.

### 4.3 Coverage

Expected versus observed **only where an expectation is explicit**:

| Expectation | Source | Per item |
|---|---|---|
| calendar sessions of a daily-bar dataset's window and symbols | the 7.1 `quality_report` (unchanged) | VALID, INVALID or MISSING sessions, holes |
| the requested symbols of a quote-snapshot dataset | the dataset's `symbols` | `VALUE`, `INVALID` (only unusable values), `ABSENCE` (looked, nothing), `MISSING` |
| a collection plan's schedule | the plan's 7.0 health policy and its windows | minutes current / expected |

Everything else is `INSUFFICIENT_DATA`, with the reason: intraday bars (`NO_PRINTS_MAKE_INTRADAY_COUNTS_UNKNOWN`: a minute without trades
has no bar, so the calendar does not fix a count), event kinds (`NO_EVENT_IS_NOT_MISSING_DATA`: no news, no halt and no filing are not
gaps), and kinds without a per-symbol universe (`NO_EXPECTED_UNIVERSE`).

**Schedule coverage** (with the collector state) samples each plan's timeline every 60 s from its first run (or `since`) to T. At each
minute, `collector.health.freshness` (the 7.3 rule) says whether the feed was current, using only the successes that had ended by then.
Minutes outside the policy's window are not expected; a minute on a day the calendar does not cover is reported apart
(`CALENDAR_NOT_COVERED`). The result is expected minutes, current minutes, the ratio, the not-current intervals, and the gaps the collector
recorded itself. The windows follow New York time across daylight-saving changes (test Q07b). The 60 s step is the resolution, not a
threshold.

Breakdowns: per dataset and symbol, per (provider, kind, evidence), and per collection pair with its time window. The check blocks on a
daily-bar `BLOCKED` result (the pinned 7.1 rule: a hole of two or more sessions, an uncovered calendar day, a blocking code). It warns on
coverage below `min_coverage`, a `FAILED`, `PARTIAL` or still-open dataset, a daily-bar `WARN`, schedule coverage below `min_coverage`, and
recorded collector gaps.

### 4.4 Latency

| Metric | Definition | Rows |
|---|---|---|
| fetch `latency_ms` | as reported by the transport | successful fetches (`OK`, `NOT_MODIFIED`) |
| fetch `duration_ms` | `retrieved_at - started_at` | successful fetches |
| delivery | `retrieved_at -` the provider's time: bar end for bars, `observed_at` otherwise (master design C.7) | live rows only: backfilled rows are history, not latency |
| published → retrieved | `retrieved_at - published_at` | news |
| availability | `available_at - retrieved_at` | live rows |
| ingestion | `ingested_at - retrieved_at` | live rows |

Each group (provider, endpoint or kind and feed, evidence) reports n, p50, p95, min and max by **nearest rank** (every figure is an actual
sample). A delivery up to the 2 s clock skew early is kept and counted (`negative_within_skew`); beyond the skew it contradicts the store's
own rule and is a timestamp violation, never a sample. SEC kinds are excluded and counted.

No latency target or SLA exists, so none is assumed: latency is **measured, not judged** (`INSUFFICIENT_DATA`, `NO_LATENCY_TARGET`) unless
the operator sets `max_fetch_p95_ms` or `max_feed_p95_s`; then a group above its target warns.

### 4.5 Timestamps

- **Malformed** (file level): every time column, found from the schema's own timestamp `CHECK`s, of every table of the store and the
  collector state. A value is well formed only in the store's exact format with a real calendar date and time. Malformed values are
  reported with their column and never used: a row whose time cannot be parsed is left out of every figure (test Q02).
- **Violations** (rows recorded by T): a value available before it was retrieved; a backfilled row available after retrieval; an event end
  before its event; a provider time after retrieval beyond the skew (`OBSERVED_AFTER_RETRIEVAL`, a negative latency); a bar not complete at
  retrieval; a publication after retrieval beyond the news skew; ingestion before retrieval; a negative fetch duration or `latency_ms`; a
  successful fetch without `retrieved_at`; ingestion before the fetch started; a member available before retrieval; a dataset event before
  its dataset; a collector run that ended before it started, or a window that ends before it starts. The store refuses all of these on
  write, so each means a tool wrote around the store (or a bug).
- **Missing** (counted apart, not failures): observations without an event time (non-bar kinds), news without a publication time,
  absences without an event time, and successful fetches without a reported latency.

Any malformed value or violation blocks.

### 4.6 Integrity

The 7.1 `verify_integrity` on a v1 file, the 7.3.1 `MarketIntelStoreV2.verify_integrity` on a v2 file (adding the COLLECT claim and news run
checks), each with the archive's anchors when `--archive-root` is given; SQLite's `integrity_check` and `foreign_key_check` on the copy;
and the raw archive's manifest (`RawArchive.verify`). `CORRUPT`, a schema error, an SQLite or foreign-key failure, an unreadable anchors file
or an archive body that does not match blocks. Without anchors the seals cannot be confirmed: `INSUFFICIENT_DATA` (`NO_ANCHORS`, also when
the archive root does not exist). A pending anchor or a dataset still open warns. The store's rows are never repaired, re-anchored or
re-sealed (`recover_pending` is not called).

### 4.7 Provider health

From the store: per (provider, endpoint, evidence), fetches by status and HTTP status, the distinct errors with counts, and the last success
and last error. **Plan pressure** applies the 7.0 rule to the recorded 429 responses (at least two within 600 s), and any recorded
`PRESSURE` budget event also counts. **Budget use**: per provider, events by type, refusals by reason, `ALLOW`s per New York day (New
York midnight, so 05:00 UTC in winter and 04:00 UTC in summer), and the peak `ALLOW`s within 60 s next to the pre-registered default budget.
The 7.0 token bucket allows a burst plus its refill, so a 60 s peak above the per-minute budget is not a violation. A day above Alpha
Vantage's daily cap is (`DAILY_CAP_EXCEEDED`). Config overrides are not read.

From the collector state: per (source, scope), runs by end status, runs still open at T, runs with clock skew, the distinct errors, the
latest ended run, the breaker state at T (its latest event by T), and whether the plan is blocked. Warnings: `PLAN_LIMIT_PRESSURE`,
`FETCH_ERRORS_RECORDED`, `LATEST_RUN_NOT_COMPLETE`, `CIRCUIT_OPEN` / `CIRCUIT_HALF_OPEN`, `CLOCK_SKEW`, `RUN_ERRORS_RECORDED`,
`DAILY_CAP_EXCEEDED`. A collector state that is refused or does not verify blocks this check.

### 4.8 IEX versus SIP divergence

Usable bars of one symbol, kind, adjustment and time in both feeds, each at its latest revision known at T, are compared by the 7.0 rule
(`validate.feed_divergence`: `|IEX close - SIP close| / SIP close`, 10 bps). The report gives the pairs, the divergent count and the bps
distribution. Divergence warns and is never a correction (the stored bars are unchanged, test Q13). No overlap is `INSUFFICIENT_DATA`
(`NO_FEED_OVERLAP`).

## 5. Thresholds

**Pinned** (from Stage 7.0 / 7.1, pinned by their own tests; changing one is a declared stage), reported in every report under
`thresholds.pinned`:

| Threshold | Value | Defined in |
|---|---|---|
| clock skew | 2 s | `validate.CLOCK_SKEW_S` = `freshness.CLOCK_SKEW_S` |
| news publication skew | 300 s | `validate.NEWS_SKEW_S` |
| freshness policies | per key (e.g. `QUOTE_DAY` 5 s / 15 s, regular session) | `freshness.POLICIES`, `mi_freshness_v2` |
| daily refresh | same New York day / 36 h | `freshness.assess_earnings_calendar` |
| plan pressure | 2 responses with 429 within 600 s | `budget.PRESSURE_429S`, `PRESSURE_WINDOW_S` |
| feed divergence | 10 bps | `validate.FEED_DIVERGENCE_BPS` |
| session hole | 2 sessions | `validate.SESSION_HOLE_DAYS` (inside `store.quality_report`) |
| default budgets | e.g. Alpaca 40/min, Alpha Vantage 6/day | `settings.BUDGETS` |

**Configurable** (`Thresholds`; each value is validated, and an invalid one is refused):

| Threshold | Default | Why |
|---|---|---|
| `min_coverage` | 1.0 | the expectation itself: every expected item present. Any lower number would be invented, so it is the operator's call |
| `max_fetch_p95_ms` | none | no latency target or SLA exists. Without one, latency is measured, not judged |
| `max_feed_p95_s` | none | as above |

## 6. Verdict

Each check is `PASS`, `WARN`, `BLOCK` or `INSUFFICIENT_DATA` with its codes. The verdict is:

1. `BLOCK` if any check blocks; else
2. `INSUFFICIENT_DATA` if a **required** check (`inputs`, `integrity`, `timestamps`) cannot be established; else
3. `WARN` if any check warns; else
4. `PASS`.

Checks are never averaged, weighted or scored, so nothing outweighs a `BLOCK`: test Q06 tampers one row and shows the verdict blocks while
every other check is exactly as good as before. A non-required check that cannot be established (for example latency without a target) is
listed under `not_established`. An input that is missing, empty or delivered nothing by T is `INSUFFICIENT_DATA`, never `PASS`
(`STORE_NOT_CREATED`, `STORE_EMPTY`, `STORE_EMPTY_AT_AS_OF`, `NO_ROWS_AT_AS_OF`); one that is unreadable, of an unknown schema or in use
blocks.

CLI exit codes: 0 `PASS`, 1 `WARN`, 2 `BLOCK`, 3 `INSUFFICIENT_DATA`, 4 a refused invocation (a naive time, an invalid threshold).

## 7. Report format and determinism

`build_report(store, as_of=..., state=..., archive_root=..., since=..., thresholds=...)` returns a JSON-ready dict. `dumps()` serialises it
with sorted keys, no NaN and no wall-clock field other than `as_of`; durations are exact to the microsecond (rounded to 6 decimals), lists
are sorted, and examples are capped at 20. `report_sha256` is the SHA-256 of the canonical JSON of everything else. The same inputs and the
same `as_of` give byte-identical output (test Q10). The text summary prints the verdict, the evidence scope, every check with its codes,
the freshness histogram, the latency groups, coverage and the digest.

    python -m market_intel.quality --store <db> [--state <collector.db>] [--archive-root <raw dir>] [--as-of <ISO time>] [--since <ISO time>]
                                   [--min-coverage 1.0] [--max-fetch-p95-ms N] [--max-feed-p95-s N] [--format text|json]

`--as-of` defaults to the current time (recorded in the report). The CLI writes only to standard output.

## 8. Safety boundaries

- **Read-only.** No input is opened in place; no row is written, repaired, sealed or anchored; no file is written beside an input; no
  migration. The module contains no SQL write statement, no store write method and one file write: the exclusive private copy (test X01).
- **No network, daemon, scheduler, thread, route, startup hook, config key or credential.** The module imports no transport, broker,
  executor, proposal or model code (test X01), and nothing outside `market_intel` imports it.
- **SEC stays blocked.** Filings and facts are neither graded nor measured (`SEC_ACCEPTANCE_TZ_UNVERIFIED`); the SEC plans keep their
  `blocked_reason` (test Q14).
- **Trading untouched.** MONITOR_ONLY, Paper Auto OFF, MANUAL_ARM, Robinhood read-only and Evidence Refresh OFF are unchanged; the frozen
  hashes and settings are pinned (test F02).

## 9. Limitations and remaining risks

- **Temporary disk space** equal to the store's size is needed while the report runs. A store too large for the temporary directory fails
  the copy (an `OSError`, before any figure).
- **A store that is always busy cannot be reported on.** A live writer (the deferred 7.3.2 daemon) keeps a `-wal`: the report refuses it
  rather than read around it. The wiring stage will need either a quiet moment or a snapshot taken under the collector's own lease.
- **The change check uses size and modification time.** A writer that changes a file without changing either during the copy is not
  detected. WAL writers always leave a `-wal`, which is refused first.
- **Schedule coverage is sampled every 60 s**: a feed current for less than a minute between samples can be missed, and the cost grows with
  the window (a `since` bounds it).
- **Integrity and malformed timestamps describe the file, not the moment**, so they are not point-in-time (section 3).
- **Delivery latency for backfilled data is not reported** (it would measure history, not the feed). Live latency exists only for
  collector-written (fixture or, later, real) rows.
- **Evidence is only as honest as `job_kind`.** A fixture written as `IMPORT` would read as operator data; the store's rules (IMPORT rows
  must be backfilled) are the only guard.
- **No production observation has been reported on.** Every test store is a fixture or an offline-verification store.

## 10. Bookkeeping

- `.github/ci/pytest_groups.json`: the new test file in the `research` group.
- `tests/freeze_ledger.py`: `BASE_74 = b9c3621…`, `NEW_74`, `EDITED_74`; `SEMANTIC_74` and `INSTRUMENTATION_74` empty; the change-set
  paragraph and union line; the two re-hashed ledger entries.
- Every earlier F01 union widened by `NEW_74 | EDITED_74`; the 7.1 module registry's `later` set includes `quality.py`; the 7.3.1 F tests
  now read what 7.3.1 left at `BASE_74` (`post()`), as each stage does for its predecessor.

## 11. Decisions for the operator

1. **Latency targets.** None exist; set `max_fetch_p95_ms` / `max_feed_p95_s` only if a target is agreed.
2. **A coverage bar below 100 %**, if partial coverage should pass.
3. **A read-only route** for this report belongs to the deferred wiring stage (proposal only; not authorized here).
4. **Reporting on the production store** is allowed read-only by naming it with `--store`, but it needs a quiet moment (no writer) and free
   temporary space. No production store exists yet.
