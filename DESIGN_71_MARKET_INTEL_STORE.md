# Stage 7.1 — market intelligence store

Stage 7.1 is code-only. It adds the historical, point-in-time storage foundation that the later Quant Metrics, AI Research, scanner and
backtesting stages will read. Nothing in the live path imports it. No collector runs, no backfill runs, no paid API is called and no
trading behaviour changes. The plan lives in `DESIGN_MARKET_INTELLIGENCE_SCANNER.md` (C.1–C.9 and the 7.1 row of E.1, outside the repo).
The contracts it stores are Stage 7.0's (`DESIGN_70_MARKET_INTEL_CONTRACTS.md`).

## 1. Files

| File | Role |
|---|---|
| `market_intel/migrations.py` | the versioned schema; append-only triggers; a DDL hash recorded per migration |
| `market_intel/store.py` | `MarketIntelStore`: lazy creation, idempotent ingestion, revisions, point-in-time reads, quality reports |
| `market_intel/archive.py` | `RawArchive`: gzipped raw responses and a SHA-256 manifest |
| `market_intel/sources/alpaca_bars.py` | Alpaca bars to envelopes, parse-only; there is no network code |
| `market_intel/ingest.py` | the offline planner and CLI, and the gated `run_backfill` for a future approved run |
| `market_intel/calendar_history.py` | the verified 2025 NYSE history |
| `market_intel/calendar.py` | edited to cover 2025 from that table (`SEMANTIC_71`) |

## 2. Database (`data/market_intel.db`, schema version 1)

The store is its own SQLite file, separate from the trading database, so ingestion never contends with the executor or the safety gate.
- **Created lazily.** Importing a module, constructing a store or calling any read creates nothing. Only an explicit write creates and
  migrates the file. The file and its WAL are git-ignored.
- **WAL and durability.** WAL mode gives concurrent readers. `synchronous=FULL` makes committed batches survive a crash or a power loss.
- **Controlled writers.** Each write is one `BEGIN IMMEDIATE` transaction, so writers are serialized and wait up to the busy timeout
  (10 s). A failed batch rolls back whole.
- **Versioning.** `mi_schema_migrations` stores the version, name, DDL SHA-256 and time; `PRAGMA user_version` is the version.
  - A newer schema refuses to open (`SchemaTooNew`).
  - A recorded DDL hash that differs from the code refuses too (`SchemaMismatch`).
  - The v1 hash is pinned by `test_D03`, so changing the schema means writing a new migration.

| Table | Content |
|---|---|
| `mi_datasets` | one explicit backfill, import or fixture batch: kind, provider, **feed**, adjustment, timeframe, symbols, range, request, plan hash, approval reference, code version |
| `mi_dataset_events` | its status history (`RUNNING` → `COMPLETED` / `PARTIAL` / `FAILED` / `EMPTY`) with counts and the content hash, written as rows, never as an UPDATE |
| `mi_fetches` | every provider request (`FetchRecord`): parameters (never a credential), times, status, HTTP status, latency, bytes, content hash, raw-archive path |
| `mi_observations` | the Stage 7.0 `Observation` envelope, one row per **revision** of one fact |
| `mi_budget_events` | provider request-budget history: allow, refuse, response, back-off and pressure events, with the daily count |
| `mi_quality_audits` | recorded data-quality reports |

- **Append-only.** Every table has no-UPDATE and no-DELETE triggers.
- **No instruction column.** No column is named for an order, quantity, side, signal or target; `test_D02` scans the schema.
- **Timestamp format.** CHECK constraints pin one UTC format, `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so text order is time order.
- **Row invariants.** `available_at >= retrieved_at`, and a backfilled row has `available_at = retrieved_at`.

## 3. Point-in-time guarantees

- **Five times per observation:**
  - `observed_at`: event or market time, for a bar its start;
  - `published_at`;
  - `retrieved_at`;
  - `available_at`;
  - `ingested_at`: the store's clock.
- **Known at T** means `available_at <= T` **and** `ingested_at <= T`. Every point-in-time read filters on both:
  - `latest_usable`, `snapshot`, `history(as_of=…)` and `quality_report(as_of=…)`;
  - so a 2026 backfill of 2025 bars is invisible to any evaluation time before it was fetched;
  - and a row ingested late cannot leak backwards, whatever availability it claims (`test_I03`).
- **Revisions.** A fact's natural key is: kind, symbol or series, provider, **feed**, adjustment, `observed_at`, and source identifier.
  - Identical content is a duplicate: skipped and counted.
  - Different content is a new revision that names the one it supersedes. A→B→A gives three revisions.
  - The revision known at T is the highest one known at T (`test_I02`).
  - Content is the fact itself; retrieval times, freshness and the dataset are not content.
- **Missing is never zero:**
  - `UNAVAILABLE` and `INSUFFICIENT_DATA` envelopes carry no payload;
  - a read with nothing usable returns `None` or an explicit `UNAVAILABLE` entry;
  - the quality report lists missing sessions and never fills them.
- **Feeds never mix:**
  - bar and quote datasets, observations and reads require `iex` or `sip`;
  - the feed is part of the natural key;
  - an iex dataset refuses a sip row (`test_I06`).
- **Unknown calendar coverage fails closed:**
  - a session-bound observation on an uncovered weekday is refused at ingestion (`CoverageError`);
  - a daily bar on a holiday or weekend is stored with `NON_SESSION_DAY` and is not usable.
- **Datasets end honestly:**
  - `EMPTY` only with zero rows and every failed fetch recovered;
  - `COMPLETED` only with every failed fetch recovered, meaning the identical request later succeeded;
  - otherwise `PARTIAL` or `FAILED`.

**Query APIs:**
- `latest_usable(kind, at=…, symbol/series, feed)`
- `history(kind, symbol, feed, start, end, as_of)`
- `snapshot(kind, symbols, at, feed)`
- `revisions(natural_key)`
- `provenance(obs_id)`: the observation, its revisions, its fetch with the raw path, and its dataset with events
- `quality_report(BAR_1DAY, symbol, feed, start, end)`: expected and present sessions, missing ones, holes, validation codes, and
  `PASS` / `WARN` / `BLOCKED`
- `status_counts()`
- `budget_used(provider, day)`

All of these are read-only and never create the store.

## 4. Backfill CLI (offline by default)

```
python -m market_intel.ingest plan     --symbols SPY,AAPL --start 2025-01-02 --end 2025-12-31 --timeframe 1Day --feed iex
python -m market_intel.ingest dry-run  ... (the plan and the first request per symbol)
python -m market_intel.ingest calendar --start 2025-01-01 --end 2025-12-31
python -m market_intel.ingest inspect  --db PATH      (an existing store, read-only; never creates one)
python -m market_intel.ingest execute  ...            (always refused in Stage 7.1, exit 3)
```

No CLI command makes a network request or writes a database or a file; `test_C01` checks this with the socket blocked. Exit codes:
0 for a valid plan, 2 for a refused one, 3 when execute is refused, and 1 for a usage error.

The planner validates:
- symbols, using the API's ticker rule, up to 100;
- dates: start before end, only completed sessions, and at most 10 years;
- timeframe (`1Day`, `1Hour`, `1Min`), feed and adjustment, which must be explicit;
- calendar coverage: any uncovered weekday refuses the plan with `CALENDAR_NOT_COVERED`;
- the SIP 15-minute rule: on Basic a SIP window must end at least 16 minutes ago (`SIP_WITHIN_15_MIN`).

It estimates:
- sessions, rows, pages (Alpaca's 10 000-bar page), requests, and minutes at the backfill budget of 60 a minute;
- bytes, using a measured 950 bytes per envelope row.

Every plan lists `REAL_BACKFILL` among the approvals it needs, and a SIP plan also lists `SIP_FEED`.

`run_backfill` is the library path that a future approved run will use. It requires:
- a valid plan;
- a `BackfillApproval` bound to that plan's hash, plus `sip_approved` for SIP;
- the backfill window: outside 07:00–20:00 New York time on market days;
- an injected fetch function.

It archives every response, records every fetch and the budget events, and ingests per page. It retries a 429 after the back-off and
stops on `ACCESS_DENIED` or when the window closes.

**Stage 7.1 ships no fetch function, and the CLI never builds an approval, so no real request can be made by this stage.**

## 5. Calendar coverage

2025 is now covered by `market_intel/calendar_history.py`, a table owned by `market_intel`; the frozen `autorun/calendar.py` is
untouched. It was verified three ways:
- the NYSE Group 2025–2027 holiday and early-closings calendar: 10 holidays, and early closes at 13:00 ET on 07-03, 11-28 and 12-24;
- the NYSE notice that all markets closed on 2025-01-09 for the national day of mourning;
- the archived IEX daily bars of 82 symbols: exactly those 11 weekdays have no session, giving 250 sessions.

The same method reproduces the frozen 2026 table. For history years, sessions are classified with the 6.10A classifier's own thresholds.

2024 and earlier, and 2028 onward, stay `NOT_COVERED` and fail closed. Extending coverage is a separate, verified step.

## 6. Provider and feed limitations

- **IEX is the default feed.** Its volume is a fraction of consolidated volume and it may have no prints for a minute (`NO_PRINTS`).
  SIP history on Basic is delayed by 15 minutes, and access needs a check on the first approved run.
- **Facts still to verify on the first approved run:**
  - the page limit;
  - extended-hours coverage on IEX;
  - the default adjustment;
  - whether delisted symbols are served.
- **Rows are about 10 times larger than the design's estimate.** The generic envelope row measures about 950 bytes, against 70–90
  bytes for C.9's typed per-kind tables. That suits daily and hourly bars and events. 100 symbols of extended-hours minute bars would
  be about 23 GB a year, so a typed `mi_bars` table, derived from these rows, may be needed before a minute-bar backfill of that size.

## 7. Future interfaces

- **Quant Metrics (7.4 and later).** Read `history(...)` and `latest_usable(...)` with an explicit feed and adjustment; filter on
  `usable`; treat a missing value as missing.
- **AI Research.** Read `snapshot(..., at=decision time)` and `provenance(obs_id)`: every value carries its fetch, raw archive and dataset.
- **Scanner (7.5) and backtests (7.6).** Use `history(..., as_of=decision time)` for point-in-time correctness. For research on
  backfilled history, read by `observed_at` with your own availability model; the `backfilled` flag and `available_at` are always present.

## 8. Known limitations and risks

- Stage 7.1 stores bars only. News, filings, macro and halt sources are Stage 7.2. The daemon and API are Stage 7.3.
- Budget counters are persisted as events. The in-memory `ProviderBudget` is not yet rebuilt from them across a restart; Stage 7.3
  will do that, since the daemon is the long-running writer.
- `quality_report` covers daily-bar completeness; other kinds report counts only.
- The single-writer rule is SQLite's own lock with a busy timeout. A long-running daemon (7.3) should also hold a lease.

## 9. Bookkeeping

- `BASE_71` is main `4e2f115`. `NEW_71` covers the new modules, tests and this note. `EDITED_71` covers:
  - `market_intel/calendar.py` (`SEMANTIC_71`), the package docstring, the `DESIGN_70` note and `.gitignore` (the store's files);
  - the CI manifest, plus test bookkeeping.
- `.gitignore` is registered as a new ledger key, following the Stage 5.4.2 `sec_edgar.py` recipe, so the earlier freeze tests read it
  as on `main`. Stage 7.1 F03 pins the three appended lines exactly.
- The earlier F01 sets are widened by `NEW_71 | EDITED_71`.
- The Stage 7.0 F tests read what 7.0 left at `BASE_71`.
- Stage 7.0's behaviour tests use 2024 instead of 2025 as the uncovered history year, with the same fail-closed assertions. 2028 is
  unchanged.
