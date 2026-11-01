# Stage 7.1 — market intelligence store

Stage 7.1 is code-only. It adds the historical, point-in-time storage foundation that the later Quant Metrics, AI Research, scanner and
backtesting stages will read. Nothing in the live path imports it. No collector runs, no backfill runs, no paid API is called and no
trading behaviour changes. The plan lives in `DESIGN_MARKET_INTELLIGENCE_SCANNER.md` (C.1–C.9 and the 7.1 row of E.1, outside the repo).
The contracts it stores are Stage 7.0's (`DESIGN_70_MARKET_INTEL_CONTRACTS.md`); the remediation of the PR #42 review changes none of them.

## 1. Files

| File | Role |
|---|---|
| `market_intel/migrations.py` | the versioned schema; append-only triggers; a DDL hash per migration; live schema-object verification |
| `market_intel/store.py` | `MarketIntelStore`: lazy creation, scope / chronology / payload checks, revisions, absences, dataset membership, point-in-time reads, quality reports, integrity verification |
| `market_intel/archive.py` | `RawArchive`: gzipped raw responses, a SHA-256 manifest, and the external anchors of sealed datasets |
| `market_intel/approvals.py` | the fail-closed, operator-controlled backfill approval (a file, never written by code) |
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
  - **Every open also compares the live schema objects** (each table, index and trigger in `sqlite_master`) with a reference built
    from the migrations in an in-memory database. A missing, extra or altered object (a dropped append-only trigger, an added
    column, an extra trigger or index) refuses reads and writes alike (`SchemaMismatch`, `test_N04`).
  - The v1 hash is pinned by `test_D03`. v1 was re-pinned once, by the review remediation, while unmerged and before any data existed;
    from the merge on, a schema change is a new migration.

| Table | Content |
|---|---|
| `mi_datasets` | one explicit backfill, import or fixture batch: kind, provider, **feed**, adjustment, timeframe, symbols, date range, request, plan hash, approval reference, code version |
| `mi_dataset_events` | its status history (`RUNNING`, then exactly one of `COMPLETED` / `PARTIAL` / `FAILED` / `EMPTY`) with counts, the content manifest and the records digest |
| `mi_dataset_members` | every row a dataset delivered: `INSERTED`, `REVISED`, `DUPLICATE`, `ABSENCE` or `REJECTED` (with the reason), its content hash and the full-row record hash |
| `mi_approval_uses` | each operator approval, claimed once, in the transaction that creates its dataset |
| `mi_fetches` | every provider request (`FetchRecord`): parameters (never a credential), times, status, HTTP status, latency, bytes, content hash, raw-archive path |
| `mi_observations` | VALUES: the Stage 7.0 envelope plus `event_end` and `usable`, one row per **revision** of one fact |
| `mi_absences` | ABSENCES: `UNAVAILABLE` / `INSUFFICIENT_DATA` / `NOT_APPLICABLE` envelopes, "we looked and found nothing" |
| `mi_budget_events` | provider request-budget history: allow, refuse, response, back-off and pressure events, with the daily count |
| `mi_quality_audits` | recorded data-quality reports |

- **Append-only.** Every table has no-UPDATE and no-DELETE triggers.
- **No instruction column.** No column is named for an order, quantity, side, signal or target; `test_D02` scans the schema.
- **Timestamp format.** CHECK constraints pin one UTC format, `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so text order is time order.
- **Row invariants (CHECK).** `available_at >= retrieved_at`; a backfilled row has `available_at = retrieved_at`; `event_end >= observed_at`;
  a value row is never an absence status; `usable = 1` only for `AVAILABLE`.

## 3. Point-in-time guarantees

### 3.1 Times, and what `event_end` means

- **Six times per value:**
  - `observed_at`: the event or market time; for a bar, its start;
  - `event_end`: when the fact was **complete**;
  - `published_at`;
  - `retrieved_at`;
  - `available_at`;
  - `ingested_at`: the store's clock.
- **`event_end` (decided before v1 is pinned):**
  - a minute bar ends 1 minute after its start, an hour bar 1 hour after;
  - a daily bar ends at its session's **late-trading end**, the latest moment a print can enter the daily aggregate: 20:00 New York
    time, or 17:00 on a 2025 early-close day (section 5);
  - any other kind ends at its observed time, or else its published or retrieved time.
- **Chronology at the store boundary (B5).** A row cannot have been observed, completed or published after it was retrieved, beyond the
  pinned skews (2 s; 5 minutes for `published_at`). `OBSERVED_AFTER_RETRIEVAL`, `BAR_NOT_COMPLETE_AT_RETRIEVAL` (a forming bar is not
  yet a fact) and `PUBLISHED_AFTER_RETRIEVAL` are refused, or quarantined as `REJECTED` members (`test_N05`).

### 3.2 Operational as-of — the only as-of Stage 7.1 offers

- **Known at T** means `available_at <= T` **and** `ingested_at <= T` **and** `observed_at <= T`. Every point-in-time read filters on
  all three: `latest_usable`, `latest_status`, `snapshot`, `history(as_of=…)` and `quality_report(as_of=…)`.
  - A 2026 backfill of 2025 bars is invisible to any evaluation time before it was fetched.
  - A row ingested late cannot leak backwards, whatever availability it claims (`test_I03`).
- **Research as-of is a separate, later design (Stage 7.6).** "When could a 2025 bar have been known in 2025" is `event_end` plus a
  pinned publication latency, with a vintage policy for revisions. No Stage 7.1 API offers it: no query takes an `event_end`,
  latency, vintage or research parameter (`test_N10`).
  - Do not use `history(as_of=<2025 time>)` on backfilled data to simulate 2025. It correctly returns nothing.
  - Do not read backfilled rows by `observed_at` alone and call that point-in-time. That is the shortcut this split forbids.

### 3.3 Values, revisions and absences

- **Revisions.** A fact's natural key is: kind, symbol or series, provider, **feed**, adjustment, `observed_at`, and source identifier.
  - Identical content is a duplicate: not stored again, but recorded as a `DUPLICATE` member of the dataset that delivered it.
  - Different content from a later dataset is a new revision that names the one it supersedes. A→B→A gives three revisions.
  - Within one dataset a key has one fact. A conflicting second record is refused (`CONFLICT_IN_DATASET`).
  - The revision known at T is the highest one known at T (`test_I02`).
  - Content is the fact itself, in store-canonical form; retrieval times, freshness and the dataset are not content. A content hash
    supplied by a source must equal the store's own, or the row is refused.
- **Absences (B9).** `UNAVAILABLE`, `INSUFFICIENT_DATA` and `NOT_APPLICABLE` rows go to `mi_absences`. They never supersede, revise or
  hide a value, and they never carry a zero.
- **Latest usable versus latest known (B9).** These are separate questions:
  - `latest_usable(...)` is the newest usable value known at T. It may be older than the newest record.
  - `latest_status(...)` and `snapshot(...)` return the newest known record (a value of any usability, or an absence) **beside** that
    value, plus `usable_is_latest`.
  - The status is `AVAILABLE` only when the newest record is that usable value. Otherwise it is `STALE_OR_SUPERSEDED_BY_ABSENCE` or
    `UNAVAILABLE`. A stale price is never presented as current (`test_N09`).
- **Usable.** A value is usable when its status is `AVAILABLE` and it carries no blocking code. An `AVAILABLE` bar must have finite,
  numeric open, high, low, close and volume (not booleans, not strings), or it is refused (`PAYLOAD_*`). Bar shape codes are annotated
  at ingest; a blocking code makes the row unusable, never repaired.

### 3.4 Dataset scope, membership and sealing

- **Specs are validated (B2).** Before a dataset exists the store checks:
  - kind, provider, feed and adjustment against the Stage 7.0 vocabularies, and the job kind;
  - for bars: the timeframe of the kind, an explicit `iex` or `sip` feed, upper-case unique symbols, and a start and end date;
  - every weekday of the range is covered by the calendar.
- **A BACKFILL dataset** also needs the operator approval of its own plan (section 4).
- **Every row is checked against its dataset (B2).** Kind, provider, feed, adjustment, symbol, session date within the range, the
  dataset id, the backfilled flag, calendar coverage, and the fetch it names (a successful fetch of this dataset and provider).
  - `on_reject="raise"` refuses the whole batch.
  - `on_reject="quarantine"`, which the runner uses, records each invalid row as a `REJECTED` member with its reason and stores the
    rest. The raw body stays in the archive.
- **Membership (B6).** Every delivered row is a member, whatever the store already held.
  - The **content manifest** is the SHA-256 of the dataset's distinct (natural key, content hash) values, duplicates included.
  - So a re-download into the same store hashes the same as the first one, never the empty hash (`test_N06`).
  - The **records digest** covers the full rows the dataset wrote (section 3.5).
- **Sealing (B7, B8).** A dataset is sealed once; a second seal is refused (`test_N08`).
  - `COMPLETED` needs every failed fetch recovered and no rejected row.
  - `EMPTY` additionally needs no delivered value.
  - A failed fetch counts as recovered only when the identical request (same provider, endpoint and canonical, non-empty parameters)
    later succeeded in the same dataset (`test_N07`).
  - Anything else is `PARTIAL` or `FAILED`.

### 3.5 Integrity: what is verified, and its limits (B4)

- **Schema verification proves the schema, not the rows.** It runs on every open (section 2). It cannot prove that no row was changed
  while a trigger was dropped and later restored exactly; `test_N04` demonstrates that limit.
- **`verify_integrity(anchors=…)` is the row-level evidence.** It is read-only and recomputes:
  - every value and absence row's content hash from its columns, which catches an edited fact;
  - every member's full-row record hash, which catches an edited time or flag, for example an earlier `available_at` (a look-ahead
    forgery that leaves the content unchanged);
  - every sealed dataset's content manifest and records digest, from its members.
- **External anchors.** At each seal, the runner appends the dataset's content manifest and records digest to
  `runtime/market_intel/raw/anchors.jsonl`, outside the database file. Passing `RawArchive.anchors()` detects a writer who edited rows
  **and** recomputed every hash inside the database.
- **Out of scope.** A writer able to rewrite both the database and the anchors file. An operator signature kept off the machine is the
  next step, if this store ever becomes evidence for an automated decision.

**Query APIs** (all read-only; none creates the store):
- `latest_usable(kind, at=…, symbol/series, feed)` and `latest_status(...)`
- `history(kind, symbol, feed, start, end, as_of, usable_only)`
- `snapshot(kind, symbols, at, feed)`
- `revisions(natural_key)`, `absences(natural_key)` and `members(dataset_id)`
- `provenance(obs_id)`: the value, its revisions and absences, its fetch with the raw path, and its dataset with events
- `dataset(dataset_id)`, including its approval use
- `quality_report(BAR_1DAY, symbol, feed, start, end)`: expected and present sessions, missing ones, holes, validation codes, and
  `PASS` / `WARN` / `BLOCKED`
- `status_counts()` (including `unsealed_datasets`), `budget_used(provider, day)` and `verify_integrity(anchors)`

## 4. Backfill CLI, runner and approval

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
- symbols: the API's ticker rule, upper case, unique, at most 100;
- dates: start before end, only completed sessions, and at most 10 years;
- timeframe (`1Day`, `1Hour`, `1Min`), feed and adjustment, which must be explicit;
- calendar coverage: any uncovered weekday refuses the plan with `CALENDAR_NOT_COVERED`;
- the SIP 15-minute rule: on Basic a SIP window must end at least 16 minutes ago (`SIP_WITHIN_15_MIN`);
- the generic-store cap: more than 2,500,000 rows is refused with `ROWS_EXCEED_GENERIC_STORE_CAP` (section 7).

It estimates sessions, rows, pages (Alpaca's 10 000-bar page), requests, minutes at the backfill budget of 60 a minute, and bytes at a
measured 1,500 bytes per row. The plan hash binds the request, its coverage (calendar versions included), the estimate and the run
policy: budget, retries, window, cap, endpoint and page limit. Every plan lists `REAL_BACKFILL` among the approvals it needs, and a SIP
plan also lists `SIP_FEED`.

### 4.1 The approval (B1)

A real backfill, in a future stage (Stage 7.1 ships no transport), needs an operator-written approval file in
`runtime/market_intel/approvals/` (git-ignored). No code writes one, and the CLI never builds one.

```json
{"version": "mi_backfill_approval_v1", "approval_id": "OPS-2026-11-07-01", "plan_sha256": "<the plan's hash>",
 "request": {"symbols": ["SPY"], "start": "2025-01-02", "end": "2025-12-31", "timeframe": "1Day", "feed": "iex",
             "adjustment": "raw", "extended_hours": false},
 "sip_approved": false, "max_requests": 4, "max_rows": 1000, "granted_by": "operator",
 "granted_at": "2026-11-07T23:00:00Z", "expires_at": "2026-11-08T05:00:00Z"}
```

The approval fails closed at every step:
- **Load.** The file must be a regular `.json` inside the approvals directory, not a link. It must have exactly these fields: an id of
  6–64 `[A-Za-z0-9_-]`, a 64-hex plan hash, the full request, a boolean `sip_approved`, positive integer caps, a grantor, and
  timezone-aware times with a validity of at most 24 hours that contains now.
- **Run.** `run_backfill` takes the `BackfillRequest` itself, never a plan dict, and **recomputes the plan at run time**. It then:
  - re-reads the approval file, which must hash exactly as when it was loaded;
  - requires the recomputed plan to be valid and to hash to `plan_sha256`;
  - requires the request to equal the approved one;
  - requires `sip_approved` for a SIP request;
  - requires the caps to cover the plan's own estimate;
  - requires a budget no larger than the plan's.
- **Once.** The approval is claimed in `mi_approval_uses`, in the transaction that creates the dataset. A second run with it is refused
  before any request is sent. The store itself also refuses a BACKFILL dataset that lacks the approval of its own plan.
- **During the run.** `max_requests` and `max_rows` stop it (`APPROVAL_REQUEST_CAP`, `APPROVAL_ROW_CAP`).

What it is **not**: a security boundary against a local process that can write the approvals directory. It is an integrity control
against mistakes, scope drift, stale or reused approvals and mutated plans. A transport stage should add an operator signature with a
key kept off the machine.

### 4.2 Failure handling (B3)

Every response is archived and recorded as a fetch row, and only then parsed and ingested.
- **Transport exceptions** become `TIMEOUT` / `ERROR` fetch rows (the class name only, never a message that could carry a URL). They
  are retried up to 2 times, then the symbol is unrecovered.
- **A transport contract violation** (a non-integer status, a body that is not bytes) becomes `INVALID_RESPONSE`.
- **Retry-After** is accepted as a non-negative finite number or an HTTP date. Anything else is ignored, and the budget's own back-off
  applies.
- **Page problems** (invalid JSON, wrong shape, symbol mismatch, bad page token, a parser crash) make the fetch `INVALID_RESPONSE`.
- **Row problems** (malformed, `OUT_OF_RANGE`, refused by the envelope, or refused by the store) become `REJECTED` members.
- **Unexpected exceptions.** An unexpected exception (a store or disk error) becomes the stop reason. A `KeyboardInterrupt` is
  re-raised after the seal.
- **The dataset is always sealed**: `COMPLETED`, `PARTIAL`, `FAILED` or `EMPTY`, never left `RUNNING`. Its anchor is then written.
  `status_counts()["unsealed_datasets"]` exposes any dataset a crashed process left open.

**Stage 7.1 ships no fetch function, and the CLI never builds an approval, so no real request can be made by this stage.**

## 5. Calendar coverage

2025 is now covered by `market_intel/calendar_history.py`, a table owned by `market_intel`; the frozen `autorun/calendar.py` is
untouched. It was verified three ways:
- the NYSE Group 2025–2027 holiday and early-closings calendar: 10 holidays, and early closes at 13:00 ET on 07-03, 11-28 and 12-24;
- the NYSE notice that all markets closed on 2025-01-09 for the national day of mourning;
- the archived IEX daily bars of 82 symbols: exactly those 11 weekdays have no session, giving 250 sessions.

The same method reproduces the frozen 2026 table. 2024 and earlier, and 2028 onward, stay `NOT_COVERED` and fail closed. Extending
coverage is a separate, verified step.

**Early-close post-market (B11).** The same NYSE release states that on the early-close days "NYSE American Equities, NYSE Arca
Equities, NYSE Chicago, and NYSE National late trading sessions will close at 5:00 p.m." The 2025 history therefore runs POST from
13:00 to 17:00 and CLOSED after (`post_market_end_minute`, `test_N11`).

**Decision pending — 2026–2027.** The frozen Stage 6.10A classifier, which DESIGN_70 section 5 makes the single source for 2026–2027,
runs POST to 20:00 on every market day, including the 2026 and 2027 early closes. Correcting it would change a Stage 7.0 contract and
the frozen executor's session table. So this remediation leaves 2026–2027 unchanged; `test_N11` pins the current behaviour so that any
change is deliberate. The options are:
- (a) a market_intel-owned override for 2026–2027 early closes, which changes the 7.0 single-source rule;
- (b) a declared change to `autorun/calendar.py` and the 6.10A classifier, which touches the frozen executor path;
- (c) keep 20:00 and document it, the status quo.

## 6. Provider and feed limitations

- **IEX is the default feed.** Its volume is a fraction of consolidated volume and it may have no prints for a minute. SIP history on
  Basic is delayed by 15 minutes, and access needs a check on the first approved run.
- **Facts still to verify on the first approved run:**
  - the page limit;
  - extended-hours coverage on IEX;
  - the default adjustment;
  - whether delisted symbols are served;
  - whether Alpaca's daily bar aggregates extended hours. This decides whether a daily bar's `event_end` could move to the regular
    close; see the Stage 7.3 note in section 9.

## 7. Measured limits and the generic-store cap (B10)

Measured on this machine with synthetic minute bars:

| Rows | Bytes per row (value + member + indexes) | Ingest | `latest_usable` at now | as-of before ingestion |
|---|---|---|---|---|
| 780 | 1,675 | 5.9k rows/s | 1.3 ms | 2.2 ms |
| 7,800 | 1,524 | 5.0k rows/s | 1.5 ms | 11.9 ms |
| 23,400 | 1,513 | 4.8k rows/s | 1.4 ms | 23.3 ms |

- **Bounded latest reads.** A latest read walks the `(symbol, kind, feed, observed_at, natural_key)` index backwards and stops at the
  first row known and usable at T. There is no sort of the partition, and its cost does not grow with the rows (`test_N12`, which
  counts SQLite VM steps at 780 and 7,800 rows).
- **The remaining limit.** When nothing in a symbol's partition was known at T (an as-of before the backfill was ingested), the walk
  visits that symbol's rows up to T. That is about 23 ms per 23,400 rows, or about 0.2 s for a symbol-year of extended-hours minute
  bars. It is bounded by one symbol, kind and feed, not by the store.
- **Disk.** A generic row with its membership row is about 1,500 bytes, against 70–90 bytes for C.9's typed per-kind tables. 100
  symbols of extended-hours minute bars for a year (24 M rows) would be about 36 GB.
- **The cap.** A plan above **2,500,000 rows** (about 3.75 GB) is refused (`ROWS_EXCEED_GENERIC_STORE_CAP`). A minute-bar history of
  that size needs the typed `mi_bars` table first.

## 8. Future interfaces

- **Quant Metrics (7.4 and later).** Read `history(...)` and `latest_usable(...)` with an explicit feed and adjustment; filter on
  `usable`; treat a missing value as missing; show `latest_status(...)` when staleness matters.
- **AI Research.** Read `snapshot(..., at=decision time)` and `provenance(obs_id)`. Every value carries its fetch, its raw archive and
  its dataset.
- **Scanner (7.5).** Use `history(..., as_of=decision time)` for operational point-in-time correctness.
- **Backtests (7.6).** Need the research as-of of section 3.2. Until then, no backtest may treat backfilled rows as known before they
  were ingested.

## 9. Known limitations and risks

- Stage 7.1 stores bars only. News, filings, macro and halt sources are Stage 7.2. The daemon and API are Stage 7.3.
- **Stage 7.3 note (`event_end` and freshness).** The 7.0 freshness policy expects the daily bar from 18:00 New York time. Under
  section 3.1 a daily bar retrieved before 20:00 (17:00 on an early close) is refused as not yet complete. The daemon must fetch daily
  bars after the late-trading end, or the first approved run must show that Alpaca's daily bar ends at the regular close. This is a
  Stage 7.3 decision; Stage 7.1 fetches nothing live.
- Budget counters are persisted as events. The in-memory `ProviderBudget` is not yet rebuilt from them across a restart; Stage 7.3
  will do that, since the daemon is the long-running writer.
- `quality_report` covers daily-bar completeness; other kinds report counts only.
- **Single writer.** The single-writer rule is SQLite's own lock with a busy timeout. A long-running daemon (7.3) should also hold a
  lease.
- **A crash during a run.** A process killed mid-run (power loss) leaves its dataset `RUNNING`. It is visible in `unsealed_datasets`,
  and its rows are those of committed batches only. Stage 7.3 should seal such datasets `FAILED` at start-up.

## 10. Bookkeeping

- `BASE_71` is main `4e2f115`. `NEW_71` covers the new modules (including `approvals.py`), the tests and this note.
- `EDITED_71` covers:
  - `market_intel/calendar.py` (`SEMANTIC_71`: 2025 coverage and the 2025 early-close post-market end);
  - the package docstring, the `DESIGN_70` note and `.gitignore` (the store's files);
  - the CI manifest, plus test bookkeeping.
- `.gitignore` is registered as a new ledger key, following the Stage 5.4.2 `sec_edgar.py` recipe, so the earlier freeze tests read it
  as on `main`. Stage 7.1 F03 pins the three appended lines exactly.
- The earlier F01 sets are widened by `NEW_71 | EDITED_71`, and the Stage 7.0 F tests read what 7.0 left at `BASE_71`.
- Stage 7.0's behaviour tests use 2024 instead of 2025 as the uncovered history year, with the same fail-closed assertions. 2028 is
  unchanged.
- `test_S01` keeps a closed registry of every `market_intel/**/*.py` file, so a module nobody declared fails the build.
