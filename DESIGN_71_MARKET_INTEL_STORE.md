# Stage 7.1 — market intelligence store

Stage 7.1 is code-only. It adds the historical, point-in-time storage foundation that the later Quant Metrics, AI Research, scanner and
backtesting stages will read. Nothing in the live path imports it. No collector runs, no backfill runs, no paid API is called and no
trading behaviour changes. The plan lives in `DESIGN_MARKET_INTELLIGENCE_SCANNER.md` (C.1–C.9 and the 7.1 row of E.1, outside the repo).
The contracts it stores are Stage 7.0's (`DESIGN_70_MARKET_INTEL_CONTRACTS.md`).

Two review rounds hardened it before merge:
- **Round 1 (B1–B12):** the PR #42 adversarial review.
- **Round 2 (F1–F12):** the independent Codex review.

Neither round changes a Stage 7.0 contract. The one exception is the declared calendar exception of section 5, which the operator
approved.

## 1. Files

| File | Role |
|---|---|
| `market_intel/migrations.py` | the versioned schema; append-only triggers; sealed-dataset guards; a DDL hash per migration; live schema-object verification |
| `market_intel/store.py` | `MarketIntelStore`. It handles: lazy creation, the operator-authority check, scope, derived-label, fetch, chronology and payload checks, revisions, absences, dataset membership, row caps, point-in-time reads, freshness-graded status, quality reports and integrity verification |
| `market_intel/archive.py` | `RawArchive`: gzipped raw responses, a SHA-256 manifest, and the external anchors of sealed datasets |
| `market_intel/approvals.py` | the fail-closed, operator-controlled backfill approval (a file; no code writes one) |
| `market_intel/sources/alpaca_bars.py` | Alpaca bars to envelopes, parse-only and strict about the response shape; there is no network code |
| `market_intel/ingest.py` | the offline planner and CLI; the gated `run_backfill` for a future approved run; `recover_pending` |
| `market_intel/calendar_history.py` | the verified 2025 NYSE history, plus the 2026–2027 late-trading override |
| `market_intel/calendar.py` | edited to cover 2025 and to end late trading at 17:00 on early closes (`SEMANTIC_71`) |

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
  - **A populated file whose `user_version` reads 0 is refused** (`SchemaMismatch`). It is never read as an empty store.
  - **Every open also compares the live schema objects** (each table, index and trigger in `sqlite_master`) with a reference built
    from the migrations in an in-memory database. A missing, extra or altered object refuses reads and writes alike.
  - The v1 hash is pinned by `test_D03`. v1 was re-pinned by the two review rounds, while unmerged and before any data existed; from
    the merge on, a schema change is a new migration.

| Table | Content |
|---|---|
| `mi_datasets` | one explicit backfill, import or fixture batch: kind, provider, **feed**, adjustment, timeframe, symbols, date range, request, plan hash, approval reference, code version |
| `mi_dataset_events` | its status history (`RUNNING`, then exactly one of `COMPLETED` / `PARTIAL` / `FAILED` / `EMPTY`), with counts (excluded rows included), the content manifest, the records digest and the metadata digest |
| `mi_dataset_members` | every row a dataset delivered, as `INSERTED`, `REVISED`, `DUPLICATE`, `ABSENCE`, `REJECTED` (with the reason) or `EXCLUDED` (outside the approved sessions). Each carries its content hash, the full-row record hash, **the fetch that delivered it and its delivery times**, a duplicate included |
| `mi_approval_uses` | each operator approval, claimed once with its caps (`max_requests`, `max_rows`), in the transaction that creates its dataset |
| `mi_fetches` | every provider attempt (`FetchRecord`), in **durable attempt order** (`seq`): parameters (never a credential), times, status, HTTP status, latency, bytes, content hash, raw-archive path |
| `mi_observations` | VALUES: the Stage 7.0 envelope plus `event_end` and `usable`, one row per **revision** of one fact |
| `mi_absences` | ABSENCES: `UNAVAILABLE` / `INSUFFICIENT_DATA` / `NOT_APPLICABLE` envelopes, "we looked and found nothing" |
| `mi_budget_events` | provider request-budget history: allow, refuse, response, back-off and pressure events, with the daily count |
| `mi_quality_audits` | recorded data-quality reports (an audit of a sealed dataset is allowed: it evaluates the dataset, it is not part of it) |

- **Append-only.** Every table has no-UPDATE and no-DELETE triggers.
- **Sealed is final, in the schema itself.** Seven tables carry a `BEFORE INSERT` guard: members, events, approval uses, fetches,
  observations, absences and budget events. Once a dataset's latest event is final, no row for it is accepted, whether it comes through
  a store method or raw SQL. Every public write method also refuses first, with `SealedError`.
- **No instruction column.** No column is named for an order, quantity, side, signal or target; `test_D02` scans the schema.
- **Timestamp format.** CHECK constraints pin one UTC format, `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so text order is time order.
- **Row invariants (CHECK).**
  - `available_at >= retrieved_at`, and a backfilled row has `available_at = retrieved_at`;
  - `event_end >= observed_at`;
  - a value row never carries an absence status;
  - `usable = 1` only for `AVAILABLE`.

## 3. Point-in-time guarantees

### 3.1 Times, and what `event_end` means

- **Six times per value:**
  - `observed_at`: the event or market time; for a bar, its start;
  - `event_end`: when the fact was **complete**;
  - `published_at`;
  - `retrieved_at`;
  - `available_at`;
  - `ingested_at`: the store's clock.
- **`event_end`:**
  - a minute bar ends 1 minute after its start, an hour bar 1 hour after;
  - a daily bar ends at its session's **late-trading end**, the latest moment a print can enter the daily aggregate: 20:00 New York
    time, or 17:00 on an NYSE early close in 2025–2027 (section 5);
  - any other kind ends at its observed time, or else its published or retrieved time.
- **Chronology at the store boundary.** A row cannot have been observed, completed or published after it was retrieved.
  - **The admission skews:** 2 s, or 5 minutes for `published_at`.
  - **What is refused:** `OBSERVED_AFTER_RETRIEVAL`, `BAR_NOT_COMPLETE_AT_RETRIEVAL` (a forming bar) and `PUBLISHED_AFTER_RETRIEVAL`,
    either outright or as quarantined `REJECTED` members.
  - **The skew admits a row; it never makes one readable early** (section 3.2).

### 3.2 Operational as-of — the only as-of Stage 7.1 offers

- **Known at T** means all of `available_at <= T`, `ingested_at <= T`, `observed_at <= T` **and `event_end <= T`**.
  - A bar still forming at T is never a completed fact at T, even when the skew admitted it a second early (`test_G09`, `test_X05`).
  - Every point-in-time read filters on all four: `latest_usable`, `latest_status`, `snapshot`, `history(as_of=…)` and
    `quality_report(as_of=…)`.
  - A 2026 backfill of 2025 bars is invisible to any evaluation time before it was fetched.
- **Research as-of is a separate, later design (Stage 7.6):** `event_end` plus a pinned publication latency, with a vintage policy. No
  Stage 7.1 API offers it (`test_N10`).
  - Do not use `history(as_of=<2025 time>)` on backfilled data to simulate 2025.
  - Do not read backfilled rows by `observed_at` alone.

### 3.3 Values, revisions, absences and current status

- **Revisions.** A fact's natural key is: kind, symbol or series, provider, **feed**, adjustment, `observed_at`, and source identifier.
  - Identical content is a duplicate: not stored again, but recorded as a `DUPLICATE` member with **its own fetch and delivery times**.
  - Different content from a later dataset is a new revision that names the one it supersedes. Within one dataset a key has one fact
    (`CONFLICT_IN_DATASET`).
  - Content is the fact itself, in store-canonical form. A content hash supplied by a source must equal the store's own.
- **Absences** go to `mi_absences`. They never supersede, revise or hide a value, and never carry a zero.
- **The latest known record** is the newest by event time, then by its **latest delivery** (`test_G07`). So a value, then an absence,
  then the same value re-delivered makes the value the latest record again.
- **Current status follows the Stage 7.0 freshness policy (F7).** `freshness.py` itself is unchanged.
  - `latest_status(...)` and `snapshot(...)` return:
    - the newest record;
    - the latest usable value;
    - `usable_is_latest`;
    - the Stage 7.0 assessment (`freshness`) and its `status`.
  - **How each kind is assessed:**
    - BAR_1DAY by its session date;
    - BAR_1MIN by the end of the last completed minute;
    - BAR_1HOUR by the start of the last completed hour.
  - **The status:**
    - `AVAILABLE` only when the newest record is that usable value **and** the policy says it is current;
    - an old value is `STALE` (`SESSION_MISSING` / `AGE_LIMIT`);
    - a value superseded by a newer absence or an unusable record is `UNAVAILABLE`;
    - a kind without a 7.1 policy mapping is `UNAVAILABLE` (`FRESHNESS_NOT_ASSESSED`).
  - **Never current by default.** A value is never "current" only because its row says `AVAILABLE` (`test_X08`).
- **Usable.** A value is usable when its status is `AVAILABLE` and it carries no blocking code. An `AVAILABLE` bar needs finite
  numeric OHLCV.

### 3.4 Dataset scope, provenance, caps and sealing

- **Specs are validated.**
  - Kind, provider, feed, adjustment and job kind are checked.
  - For bars: the timeframe of the kind, an explicit `iex` or `sip` feed, upper-case unique symbols, and a start and end date.
  - Every weekday of the range must be covered by the calendar.
- **Labels are derived, never trusted (F2).**
  - A row's session date and session must equal the ones its `observed_at` gives in New York (`SESSION_DATE_MISMATCH`,
    `SESSION_MISMATCH`).
  - A daily bar must start at New York midnight, a minute bar on the minute, an hour bar on the hour (`TIMEFRAME_MISALIGNED`).
  - A payload's own `timeframe` must be the dataset's (`TIMEFRAME_MISMATCH`).
- **Fetch binding (F2).** A row that names a fetch must match what that fetch asked for:
  - the endpoint (`/v2/stocks/{symbol}/bars`), symbol, feed, adjustment and timeframe;
  - the time window `[start, end)`;
  - that fetch's retrieval time.
  - **A BACKFILL row must name its fetch.** A QQQ fetch never carries a SPY row, and a SIP fetch never an IEX row.
- **Session scope (F3).** An intraday bar belongs to the approved sessions when its interval `[start, start + timeframe)` overlaps them.
  - **A regular-hours dataset:** the regular session, 09:30 to the close.
  - **An extended-hours dataset** (`request.extended_hours`): 04:00 to the late-trading end.
  - **Consequences:** the 09:00 hour bar (09:00–10:00) is a regular-hours bar, while the 16:00 bar, and the 13:00 bar on an early
    close, are not.
  - **EXCLUDED rows.** Bars outside the scope, such as the pre- and post-market bars a provider sends with a regular-hours request,
    become `EXCLUDED` members. They are never stored as values, and they do not make a run `PARTIAL`.
- **Caps at every entry point (F3).**
  - **Delivered rows.** A dataset never holds more delivered rows (all members) than `min(MAX_DATASET_ROWS = 2,500,000, the approval's
    max_rows)`. `ingest` and `record_problems` refuse a whole batch that would pass it (`RowCapError`).
  - **Requests.** An approved dataset never records more fetch attempts than the approval's `max_requests`.
- **Membership.** Every delivered row is a member.
  - The content manifest is the SHA-256 of the dataset's distinct (natural key, content hash) values, so a re-download hashes the same
    and never as the empty hash.
  - The records digest covers the full rows the dataset wrote.
  - The **metadata digest** covers: the dataset row (spec, request, plan hash, approval reference), its approval use (with caps), its
    opening event, every fetch attempt in order, and the sealed status and counts.
- **Sealing.** A dataset is sealed once.
  - `COMPLETED` needs every failed fetch recovered and no rejected row.
  - `EMPTY` additionally needs no delivered value.
  - A failure is recovered only by the identical request (same provider, endpoint and canonical, non-empty parameters) in a **later
    attempt**. Attempt order is the durable `seq`, never a clock (`test_G10`).

### 3.5 Integrity: VERIFIED, UNVERIFIED, CORRUPT (F4)

`verify_integrity(anchors=RawArchive.anchors())` is read-only. It reconciles the database with the external anchors **in both
directions**:
- **CORRUPT** when any of these holds:
  - a schema problem, including a reset `user_version`;
  - a value or absence whose content hash does not recompute;
  - a member whose content or full-row record hash differs from its row;
  - a sealed dataset whose manifest, records digest, metadata digest or counts do not recompute, or that has an event after its seal;
  - a row whose dataset no longer exists (an orphan);
  - **an anchored dataset missing from the store** (deleted rows, or the whole database gone);
  - an anchor that disagrees with its seal or conflicts with another anchor.
- **UNVERIFIED** when nothing contradicts but something is unconfirmed: no anchors were given; a sealed dataset has no anchor yet
  (pending anchor); or a dataset is unsealed (pending finalization).
- **VERIFIED** only when every sealed dataset recomputes and matches its anchor, every anchor matches a sealed dataset, and nothing is
  pending. `ok` is true only for VERIFIED.
- **Limits (unchanged, documented):**
  - Schema verification cannot prove that no row was changed while a trigger was dropped and restored exactly; the row hashes and
    anchors are what catch that (`test_N04`).
  - A writer who can rewrite **both** the database and the anchors file consistently is out of scope. An operator signature kept off
    the machine is the next step, if this store ever becomes evidence for an automated decision.

**Query APIs** (all read-only; none creates the store):
- `latest_usable(...)`, `latest_status(...)` and `snapshot(...)`
- `history(kind, symbol, feed, start, end, as_of, usable_only)`
- `revisions(natural_key)`, `absences(natural_key)` and `members(dataset_id)`
- `provenance(obs_id)`: revisions, absences, **every delivery**, the first fetch with its raw path, and the dataset with its events
- `dataset(dataset_id)` and `dataset_states()`
- `quality_report(BAR_1DAY, …)`
- `status_counts()`, `budget_used(provider, day)` and `verify_integrity(anchors)`

## 4. Backfill CLI, runner, approval and finalization

```
python -m market_intel.ingest plan     --symbols SPY,AAPL --start 2025-01-02 --end 2025-12-31 --timeframe 1Day --feed iex
python -m market_intel.ingest dry-run  ... (the plan and the first request per symbol)
python -m market_intel.ingest calendar --start 2025-01-01 --end 2025-12-31
python -m market_intel.ingest inspect  --db PATH      (an existing store, read-only; never creates one)
python -m market_intel.ingest execute  ...            (always refused in Stage 7.1, exit 3)
```

No CLI command makes a network request or writes a database or a file (`test_C01`, with the socket blocked). Exit codes: 0 for a
valid plan, 2 for a refused one, 3 when execute is refused, and 1 for a usage error.

**The planner validates:**
- symbols: the ticker rule, upper case, unique, at most 100;
- dates: only completed sessions, at most 10 years;
- timeframe, feed and adjustment, which must be explicit;
- calendar coverage;
- the SIP 15-minute rule;
- the generic-store cap: a plan whose provider may deliver more than 2,500,000 rows is refused (`ROWS_EXCEED_GENERIC_STORE_CAP`).

**It estimates two row counts:**
- `rows_total`: the bars the approved sessions keep;
- `rows_delivered_max`: what the provider may send, meaning every bar from 04:00 to the late-trading end for intraday timeframes, even
  when the request is regular-hours only.

Pages, requests and the approval's row cap follow `rows_delivered_max`. The plan hash binds the request, its coverage (calendar versions
included), the estimate and the run policy (budget, retries, window, cap, endpoint, page limit).

### 4.1 The approval (round 1 B1; round 2 F1)

A real backfill, in a future stage (Stage 7.1 ships no transport), needs an operator-written approval file in
`runtime/market_intel/approvals/` (git-ignored; `MarketIntelStore(approvals_root=…)` names it). No code writes one.

```json
{"version": "mi_backfill_approval_v1", "approval_id": "OPS-2026-11-07-01", "plan_sha256": "<the plan's hash>",
 "request": {"symbols": ["SPY"], "start": "2025-01-02", "end": "2025-12-31", "timeframe": "1Day", "feed": "iex",
             "adjustment": "raw", "extended_hours": false},
 "sip_approved": false, "max_requests": 4, "max_rows": 1000, "granted_by": "operator",
 "granted_at": "2026-11-07T23:00:00Z", "expires_at": "2026-11-08T05:00:00Z"}
```

- **Load.** A regular `.json` file inside the approvals directory, not a link. It has exactly the schema above, an id of 6–64
  `[A-Za-z0-9_-]`, positive integer caps, and a validity of at most 24 hours that contains now.
- **Verified by the store itself, at every entry point (F1).** `MarketIntelStore.create_dataset` refuses a BACKFILL dataset unless
  `_authorize` passes, whoever calls:
  - the approval is a `BackfillApproval` whose file still exists in the store's approvals directory, hashes exactly as when loaded, and
    is valid now;
  - the plan **recomputed now from the dataset's own request** is valid and hashes to `plan_sha256`;
  - the request equals the approved one, SIP has `sip_approved`, and the caps cover `requests` and `rows_delivered_max`;
  - the dataset spec equals, field by field, the one spec that plan allows (`ingest.dataset_spec`).
  - So a fabricated object, a missing or edited file, an expired approval, a file outside the directory or a widened scope (another
    symbol, feed, date, adjustment, timeframe, hash or approval id) is refused before anything is written (`test_G01`, `test_X01`).
- **The runner checks it too.** `run_backfill` takes the `BackfillRequest` itself (never a plan dict), recomputes the plan and checks the
  approval before it sends anything. It also requires a budget no larger than the plan's.
- **Once.** The approval is claimed, with its caps, in the same transaction that creates the dataset. A second use in the same database
  is refused before any request is sent.
- **During the run.** `max_requests` and `max_rows` stop the runner (`APPROVAL_REQUEST_CAP`, `APPROVAL_ROW_CAP`), and the store enforces
  the same caps underneath.
- **Not a security boundary** against a local process that can write the approvals directory. It is an integrity control against
  mistakes, scope drift, stale or reused approvals and mutated plans. A transport stage should add an off-machine operator signature.

### 4.2 Failure handling and finalization (round 1 B3; round 2 F5)

- **Every response is archived first.** It is archived and recorded as a fetch row, and only then parsed and ingested.
- **Transport failures:**
  - exceptions become `TIMEOUT` / `ERROR` fetch rows (the class name only) and are retried up to 2 times;
  - a transport contract violation is `INVALID_RESPONSE`;
  - Retry-After is accepted as a non-negative finite number or an HTTP date, and otherwise ignored.
- **Response validation (F11).** An HTTP 200 body is a page only when it has the exact Alpaca shape:
  - a JSON object with exactly `bars` / `symbol` / `next_page_token` (`currency` allowed);
  - `bars` a list or null, of at most 10 000;
  - `symbol` the requested one;
  - the token null or a plain string of up to 512 characters.
  - An error envelope (`message` / `code`) is `ERROR_ENVELOPE`; any other deviation is `UNEXPECTED_SHAPE`, `SYMBOL_MISMATCH`,
    `BAD_PAGE_TOKEN` or `PAGE_TOO_LARGE`.
  - Any of those makes the fetch `INVALID_RESPONSE`, so a malformed 200 can never seal a dataset `EMPTY` (`test_G11`, `test_X06`).
  - A bar must be a JSON object of the known keys, with a string time and numeric (never boolean, never string) prices and volume.
    Anything else is `MALFORMED_ROW`.
- **The dataset is sealed** `COMPLETED`, `PARTIAL`, `FAILED` or `EMPTY`. An unexpected exception becomes the stop reason. A
  `KeyboardInterrupt` is re-raised after the seal.
- **If the seal itself cannot be written** (a disk failure), `run_backfill` raises `FinalizationError(dataset_id,
  "PENDING_FINALIZATION")`. The dataset stays `RUNNING`, visible in `dataset_states()` and as UNVERIFIED in the integrity check, and its
  approval stays used. A persistent disk failure is never claimed to be sealed.
- **If the anchor cannot be written**, the result says `finalization: PENDING_ANCHOR`. The seal is durable, and integrity reads
  UNVERIFIED until the anchor exists.
- **`recover_pending(store, archive, clock=…)`** completes both, deterministically (`test_G05`):
  - it seals a dataset whose last activity is older than `stale_after` (1 hour) as `PARTIAL` if it delivered a value, else `FAILED`
    (`RECOVERED_ABANDONED_RUN`);
  - it leaves recent ones alone, since they may belong to a live run;
  - it anchors sealed-but-unanchored datasets with the seal the database holds, **only if the store verifies without a CORRUPT
    finding**. A corrupt store is never re-anchored.

**Stage 7.1 ships no fetch function, and the CLI never builds an approval, so no real request can be made by this stage.**

## 5. Calendar coverage

2025 is covered by `market_intel/calendar_history.py`, a table owned by `market_intel`; the frozen `autorun/calendar.py` is untouched.
It was verified three ways:
- the NYSE Group 2025–2027 holiday and early-closings calendar: 10 holidays, and early closes at 13:00 ET on 07-03, 11-28 and 12-24;
- the NYSE notice that all markets closed on 2025-01-09 for the national day of mourning;
- the archived IEX daily bars of 82 symbols: exactly those 11 weekdays have no session, giving 250 sessions.

2024 and earlier, and 2028 onward, stay `NOT_COVERED` and fail closed.

**Early-close late trading: the one declared exception to the Stage 7.0 single-source rule.** The operator approved it on 2026-10-31.
The NYSE release footnotes every 2025–2027 early-close date: the late trading sessions "will close at 5:00 p.m." This was re-verified
on 2026-10-31 for the 2026-11-27, 2026-12-24 and 2027-11-26 dates.
- **2025:** the history table ends POST at 17:00 on its three early closes.
- **2026–2027:** a market_intel-owned, versioned override (`LATE_TRADING_EARLY_CLOSES`, `nyse_late_trading_2026_2027_v1`) turns the
  6.10A classifier's `AFTER_HOURS` into `CLOSED` from 17:00 on exactly those three dates. `CAL.session()` marks it with
  `late_trading_override`. A test pins the override dates equal to the frozen table's `EARLY_CLOSES` (`test_G12`).
- **What it changes:** `post_market_end_minute`, daily-bar `event_end`, the planner's extended estimates, the session scope, and the
  7.0 `EXTENDED` freshness window on those evenings.
- **What it does not change:**
  - `autorun/calendar.py`, the 6.10A classifier, the executor's session health;
  - every holiday and every 13:00 close;
  - all other days.
- **The 12:59 / 13:00 / 16:59 / 17:00 boundaries** read REG / POST / POST / CLOSED.
- **The Stage 7.0 test that compares the two classifiers minute by minute** now excepts exactly that window on its early-close day,
  through the REPLAY bookkeeping.

## 6. Provider and feed limitations

- **IEX is the default feed.** Its volume is a fraction of consolidated volume and it may have no prints for a minute. SIP history on
  Basic is delayed by 15 minutes, and access needs a check on the first approved run.
- **Facts still to verify on the first approved run:**
  - the page limit;
  - the exact response shape: the strict parser fails closed on any deviation, as intended;
  - whether bars without extended hours requested still include pre- and post-market bars (they would be `EXCLUDED`, and the
    estimate already allows for them);
  - IEX extended-hours coverage;
  - the default adjustment;
  - delisted symbols;
  - whether Alpaca's daily bar aggregates extended hours, which decides whether a daily bar's `event_end` could move to the regular
    close.

## 7. Measured limits and the generic-store cap

Measured on this machine with synthetic minute bars (round 2 schema):

| Rows | Bytes per stored row (value + member + indexes) | Ingest | `latest_usable` at now | as-of before ingestion |
|---|---|---|---|---|
| 780 | 1,638 | 4.3k rows/s | 1.1 ms | 1.7 ms |
| 7,800 | 1,638 | 3.8k rows/s | 1.1 ms | 6.9 ms |
| 23,400 | 1,640 | 3.8k rows/s | 1.2 ms | 17.4 ms |

An `EXCLUDED` member (a delivered bar outside the approved sessions) costs about 500 bytes, and a daily-bar row about 1,630.

- **Bounded latest reads.** A latest read walks the `(symbol, kind, feed, observed_at, natural_key)` index backwards and stops at the
  first row known, complete and usable at T. Its cost does not grow with the rows (`test_N12` counts SQLite VM steps).
- **The remaining limit.** When nothing in a symbol's partition was known at T (an as-of before the backfill was ingested), the walk
  visits that symbol's rows up to T. That is bounded by one symbol, kind and feed.
- **Disk.** A generic row with its membership row is about 1,650 bytes, against 70-90 bytes for C.9's typed per-kind tables. 100 symbols of extended-hours minute bars for a year (24 M rows) would be about 40 GB, and the 2.5 M-row cap is about 4.1 GB. Ingest runs at about 4k rows a second, so a full capped dataset takes about 10 minutes.
- **The cap.** `MAX_DATASET_ROWS = 2,500,000` delivered rows per dataset, enforced by the store at every entry point. A plan whose
  `rows_delivered_max` exceeds it is refused. A minute-bar history of that size needs the typed `mi_bars` table first.

## 8. Future interfaces

- **Quant Metrics (7.4 and later).** Read `history(...)` and `latest_usable(...)` with an explicit feed and adjustment; filter on
  `usable`; treat a missing value as missing; use `latest_status(...)` when currency matters, since it applies the 7.0 freshness policy.
- **AI Research.** Read `snapshot(..., at=decision time)` and `provenance(obs_id)`. Every value carries its fetches, deliveries, raw
  archive and dataset.
- **Scanner (7.5).** Use `history(..., as_of=decision time)` for operational point-in-time correctness.
- **Backtests (7.6).** Need the research as-of of section 3.2. Until then, no backtest may treat backfilled rows as known before they
  were ingested.

## 9. Known limitations and risks

- **Scope of 7.1.** Stage 7.1 stores bars only. News, filings, macro and halt sources are Stage 7.2. The daemon and API are Stage 7.3.
- **Stage 7.3: daily-bar `event_end` against freshness.** The 7.0 freshness policy expects the daily bar from 18:00 New York time, but a
  daily bar retrieved before its late-trading end is refused as not yet complete. The daemon must fetch daily bars after 20:00 (17:00 on
  an early close), unless the first approved run shows that Alpaca's daily bar ends at the regular close.
- **Recovery's live-run test is a heuristic.** `stale_after` defaults to 1 hour of no activity. A long-running daemon (7.3) should hold
  a lease instead, and seal its own abandoned datasets at start-up.
- **Budget counters** are persisted as events but not yet rebuilt into the in-memory budget across a restart (Stage 7.3).
- **Quality reports** cover daily-bar completeness only; other kinds report counts.
- **The approval and integrity limits** are those of sections 4.1 and 3.5.

## 10. Bookkeeping

- `BASE_71` is main `4e2f115`. `NEW_71` covers the new modules (including `approvals.py`), the tests and this note.
- `EDITED_71` covers:
  - `market_intel/calendar.py` (`SEMANTIC_71`: 2025 coverage, and the 17:00 early-close late-trading end for 2025 and the 2026–2027
    override);
  - the package docstring, the `DESIGN_70` notes (the 2025 step and the declared exception) and `.gitignore`;
  - the CI manifest, plus test bookkeeping. One of those test edits is the Stage 7.0 calendar test's narrowed minute-by-minute
    comparison (a REPLAY pair).
- `.gitignore` is a new ledger key, following the Stage 5.4.2 recipe, and F03 pins its three lines.
- The earlier F01 sets are widened by `NEW_71 | EDITED_71`.
- `test_S01` keeps a closed registry of every `market_intel/**/*.py` file, so a module nobody declared fails the build.
