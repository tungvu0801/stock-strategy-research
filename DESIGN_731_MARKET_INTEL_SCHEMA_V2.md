# Stage 7.3.1 — Market intelligence schema v2 and data lifecycle (offline)

**Designation (operator decision, 2026-11-01).**
- Stage 7.3.1 is an explicitly authorized intermediate stage between 7.3 (the offline collector) and 7.4.
- The master design's numbering is unchanged: 7.4 is still the data-quality and latency report, 7.5 the shared scanner.
- The deferred wiring (daemon, startup hook, API routes, config block) may later be proposed as 7.3.2. That designation authorizes
  nothing.

Base: main `ad60a02` (PR #44 merged).

**What this stage is.** It is an offline storage foundation, made only of new files:
- schema v2, with a distinct `COLLECT` dataset kind;
- an explicit, copy-only v1 → v2 migration;
- point-in-time news cluster and ticker-mention persistence;
- a retention and archival proposal, with non-destructive primitives.

**What it is not.**
- No live collection: schema support for `COLLECT` authorizes no live ingestion.
- No network, transport, daemon, scheduler activation, route, startup hook, config key or credential.
- No migration of any production database, and no deletion or compaction of any record.

## 1. Migration strategy (v1 → v2)

**The Stage 7.1 store stays exactly as it is.**
- `market_intel/store.py` and `market_intel/migrations.py` are not edited.
- `MarketIntelStore` still creates and reads only v1 files, and still refuses a newer file (`SchemaTooNew`). A v1 reader can never
  mistake v2 data for v1.
- The Stage 7.3 collector keeps using it.

**Schema v2 lives in new modules.**
- `market_intel/schema_v2.py` holds the migration-2 statements, the v2 schema verification, the explicit copy migration and its CLI.
- `market_intel/store_v2.py` holds `MarketIntelStoreV2`, a subclass of the 7.1 store, plus:
  - `COLLECT` datasets;
  - news persistence;
  - v2 integrity checks.

**Explicit only.** No code path upgrades a file implicitly. `MarketIntelStoreV2`:
- creates a fresh file directly at v2;
- reads and writes v2 files;
- refuses a v1 file with `SchemaUpgradeRequired`, naming the migration command;
- has **no default path**: every v2 file is named by its caller, so nothing is ever created at `data/market_intel.db`.

**The migration** (`schema_v2.migrate_copy(source, target, anchors=...)`) works on a disposable copy:

1. **Refusals before anything is written:**
   - `SOURCE_IS_PRODUCTION` or `TARGET_IS_PRODUCTION`: the store's default path, or anything under the project's `data/`;
   - `TARGET_EXISTS`, `REPORT_EXISTS` (a `<target>.migration.json` already there) or `SAME_FILE`;
   - `INTERRUPTED_MIGRATION_PRESENT`: a `<target>.partial` or `<target>.migration.json.partial` left by an earlier, interrupted run;
   - `NO_ANCHORS`: the archive anchors are required.
2. **Snapshot.** The SQLite backup API copies the source, opened read-only, into `<target>.partial`. The source is never opened for
   writing, and a concurrent writer cannot make the copy inconsistent. The `.partial` file is claimed **exclusively**: a second
   migration to the same target is refused rather than sharing the copy.
3. **Pre-verification of the copy, as v1.** All of these must hold, else the migration is refused:
   - the schema objects match v1 exactly;
   - `PRAGMA integrity_check` is `ok` and `PRAGMA foreign_key_check` is empty;
   - the 7.1 `verify_integrity(anchors)` is **VERIFIED**: every value and absence hash, every member and record hash, every seal's
     manifest, records and metadata digest, every count, every anchor matched both ways, nothing unsealed or unanchored. A store with a
     run in flight is not quiescent, and is refused;
   - a digest of every row of every table, per table, is recorded.
4. **Migration, in one transaction.** Foreign keys are off for the rebuild only.
   - `mi_datasets` is rebuilt so that `job_kind` also allows `COLLECT`. The rows, with their rowids, are carried through a temporary
     table, the table is recreated from the code's own v2 DDL, and the rows are copied back. Column names, types and values are
     identical, so every record hash, and every seal's metadata digest, is unchanged.
   - Its append-only triggers are recreated, and the v2 tables, triggers and indexes are added.
   - Migration 2 is recorded with its DDL hash, and `user_version` is set to 2.
   - An interruption rolls the whole transaction back.
   The copy is switched to a rollback journal first: the backup carries the source's WAL mode, and a promoted file must hold every
   committed page itself, with no `-wal`.
5. **Post-verification.** All of these must hold before the file is promoted:
   - the v2 schema objects match exactly;
   - the integrity and foreign-key checks pass;
   - **every v1 table's row digest equals its pre-migration digest** (`mi_schema_migrations` adds exactly one row);
   - the v2 store's `verify_integrity(anchors)` is VERIFIED.

   Only then is the copy promoted (step 6).
6. **Promotion: a new name or nothing.** Checking that the target is absent and then renaming over it is not atomic: a file
   created between the check and an `os.replace` would be overwritten (found by the operator's independent review of `75b3051`).
   Promotion therefore never uses a call that can replace a file:
   - The copy is flushed to disk (`fsync`), then **hard-linked** to the target name (`os.link`). The call creates the name or fails
     when the name exists. Neither POSIX `link(2)` nor Windows `CreateHardLinkW` has a replacing mode, so no file system that
     accepts the call can overwrite. A file that appeared at any moment before the call is left byte for byte: `TARGET_EXISTS`.
   - **Fail closed.** A file system without hard links (FAT, exFAT, some network shares) fails the call: `ATOMIC_PROMOTION_UNSUPPORTED`.
     Nothing else is ever tried: no rename, replace or copy.
   - After the link, the target must be **this very file** (`samefile`) holding **the verified bytes** (SHA-256), else
     `PROMOTION_NOT_VERIFIED`. On POSIX the directory is flushed, so the new name is durable before the next step; Windows offers no
     directory flush through `os`, and NTFS records the name in its metadata log. Then the second name, `.partial`, is removed.
   - **The report is published the same way, last.** It is written to `<target>.migration.json.partial` (created exclusively and
     flushed), then hard-linked to `<target>.migration.json` (`REPORT_EXISTS` if that name appeared meanwhile, which is never
     overwritten). It records `target_sha256`, the bytes read back through the promoted name, and the promotion method.
   - **A migration is complete exactly when its report exists and its `target_sha256` equals the target's SHA-256.** A target
     without its report is not a migration.
7. **Failure handling.**
   - A refusal **before** promotion discards the tool's own `.partial` copy (never the source).
   - A refusal **during** promotion, a crash or a `KeyboardInterrupt` leaves every file in place. The next run refuses before
     writing anything, until an operator inspects the files and removes them.
   - The states a crash can leave, each tested by killing a real process at that step (test V05):

     | Crash point | Files left | Next run | Recovery |
     |---|---|---|---|
     | before the link | `.partial` | `INTERRUPTED_MIGRATION_PRESENT` | remove it, run again |
     | after the link | target and `.partial`: one file, two names, no report | `INTERRUPTED_MIGRATION_PRESENT` | remove both, run again |
     | after `.partial` is removed | the target, no report | `TARGET_EXISTS` | remove it, run again |
     | before the report's link | the target and `.migration.json.partial` | `INTERRUPTED_MIGRATION_PRESENT` | remove both, run again |
     | after the report's link | target, report, `.migration.json.partial` | `INTERRUPTED_MIGRATION_PRESENT` | complete: remove the `.migration.json.partial` only |

     The source is never touched, so running again is always safe and carries the same v1 rows, hashes and seals (test M01).
   - Concurrent migrations to one target (threads, test V02; separate processes, test V04): exactly one completes. A loser is refused
     before copying, or, if it copied after the winner's promotion, its link fails and it keeps its own copy as `.partial`.

**Unknown versions fail closed.** `user_version` above 2 is `SchemaTooNew`. A populated file at version 0, migration rows that disagree
with `user_version`, or a missing, extra or altered object is `SchemaMismatch`.

## 2. The COLLECT dataset kind and its provenance

- **Distinct.** `COLLECT` is its own `job_kind` in v2. FIXTURE, IMPORT and BACKFILL keep their exact 7.1 semantics and code paths.
- **Not live because the schema allows it.** A COLLECT dataset exists only with a **collect authorization** that the store re-reads and
  checks itself:
  - the file is `mi_collect_authorization_v1`, in the approvals directory, and no code writes one;
  - its fields name the purpose, source, provider, kind, feed, symbols, scope, caps, grantor and a validity window of at most 24 h;
  - the file is re-read and must hash the same;
  - the dataset spec must equal what the file names, and the authorization is **used once** (`mi_collect_authorization_uses`).
- **Only one purpose is accepted: `OFFLINE_VERIFICATION`.** This is the fake-transport and fixture path.
  - `LIVE` is refused (`LIVE_COLLECT_NOT_AUTHORIZED`) by the loader, by the store and **by the schema itself**: the claim table's
    `CHECK (purpose IN ('OFFLINE_VERIFICATION'))` means even raw SQL cannot record a live claim.
  - Live collection needs a later, approved stage with a schema change.
- **Other authority paths do not create COLLECT datasets.**
  - The 7.1 store refuses `COLLECT` (its validation and its v1 `CHECK`).
  - The v2 store refuses COLLECT with a backfill approval or with none, refuses a collect authorization on any other kind, and refuses
    a blocked plan (both SEC plans: `SEC_ACCEPTANCE_TZ_UNVERIFIED`).
  - A schema trigger refuses a COLLECT dataset row without its own claim, and a claim on a non-COLLECT dataset.
- **Provenance, enforced by the schema as well as the store.** Every COLLECT value, absence and member:
  - names a successful fetch of its own dataset;
  - is not backfilled;
  - has `available_at = retrieved_at`, since live knowledge starts when we saw it.

  The 7.1 chronology rules (2 s skew; nothing observed, completed or published after retrieval) and the revision rules apply
  unchanged.
- **Time-bounded.** A COLLECT dataset takes no fetch, value, absence or member at or after its authorization's `expires_at`. Both the
  store and schema triggers enforce this. The dataset can still be sealed, and the 7.1 recovery seals and anchors an abandoned one.
- **Caps and seals.**
  - The claim's caps bound requests and delivered rows (`min(max_rows, MAX_DATASET_ROWS)`).
  - A COLLECT seal's metadata digest also covers its claim. A non-COLLECT digest is exactly the 7.1 digest, so migrated seals still
    verify.
- **The collector does not change.** It still accepts only the fake transport and still refuses every sink but FIXTURE, whatever store
  it is given.

## 3. News clusters and ticker mentions (point in time)

**Inputs are the stored NEWS facts known at `as_of`:**
- `available_at ≤ as_of`, `ingested_at ≤ as_of` and `usable`;
- for each article, the latest revision known at `as_of`;
- first seen = the earliest `available_at` of its known revisions;
- the article's `observed_at` falls in the run's window.

**Computation.**
- Clusters reuse the 7.2 `news.dedup.cluster`, with its pinned thresholds.
- Mentions reuse the 7.2 `news.entities.mentions`, with an **entity map** (SEC names, universe, aliases) stored by its SHA-256 and
  required to be recorded at or before `as_of` (reference data is point in time too).
- No model, no sentiment and no LLM are involved.

**Persistence (v2 tables, append-only).**
- `mi_news_cluster_runs` with `mi_news_clusters` and `mi_news_cluster_members`.
- `mi_news_mention_runs` with `mi_news_mentions`.
- `mi_news_entity_maps`.

**Traceability.** Every member and mention names:
- the observation (`obs_id`, a foreign key);
- its natural key and revision;
- the full-row record hash of that observation when the run was computed.

**Closed runs and point-in-time guards, enforced by the schema.**
- A run's member and mention rows are written before its run row, in the same transaction (deferred foreign keys), and a trigger
  refuses any member or mention for a run that already exists.
- The run row is refused when:
  - its counts disagree with its rows;
  - any input was not known at `as_of` (`LOOK_AHEAD`);
  - `as_of` is after `computed_at`;
  - (mentions) the map was recorded after `as_of`.

**Idempotent, with late corrections.**
- Runs are unique on (algorithm, window, input digest), plus the map digest for mentions. Recording the same inputs again returns the
  existing run.
- A late correction is a new revision, so the input digest changes, so a new run is written. The old run stays (history).
- `news_clusters_as_of(T, window)` and `news_mentions_as_of(T, window)` return the latest run with `as_of ≤ T` **and**
  `computed_at ≤ T`, never one computed later (no look-ahead).

**Verification.** `verify_integrity` recomputes each run's input and output digests and its clusters or mentions from the stored
observations and map. A tampered association, for example a wrong ticker, a missing record, or a member not known at `as_of`, is
CORRUPT.

## 4. Retention and archival policy (proposal; nothing is deleted)

No retention period is invented. Every period below is an **operator decision**: the primitives take it as a required argument, with
no default.

| Class | Where | Policy proposed | Why |
|---|---|---|---|
| **Audit evidence** | datasets, events (seals), members, fetches, approval and collect-authorization uses, budget events, migrations; `anchors.jsonl`; the raw `manifest.jsonl` | **never deleted, never moved out of the store** | it is the proof of what was fetched, delivered and sealed, and the integrity check reads it |
| **Facts** | `mi_observations`, `mi_absences` | **never deleted** | point-in-time history: a value superseded later is still what was known then |
| **Raw payloads** | `runtime/market_intel/raw/YYYY-MM-DD/*.json.gz` | **archivable**: copied to cold storage once its dataset is sealed, anchored and verified and its body re-hashes; removal from hot storage is a later decision | the largest class; re-derivable evidence |
| **Derived intelligence** | news cluster and mention runs, entity maps | **kept** (small; recomputable from facts plus a map); a thinning rule (for example only the latest run per window and algorithm) is a later decision | runs are history: a thinned run can be recomputed, but its `computed_at` cannot |
| **Collector state** | `runtime/market_intel/collector.db` | **kept** (append-only); a snapshot-and-archive compaction is a later decision | needed for watermarks, leases and the breaker |

**Tradeoffs to decide.**
1. **Hot raw-payload age** (for example 30, 90 or 365 days). Shorter frees disk sooner. Longer keeps fast re-parsing (adapter fixes)
   and dispute evidence local.
2. **Cold archive medium and verification cadence.**
3. **Derived-run thinning.** Fewer rows, against losing the operational record of when a computation existed.
4. **Collector-state compaction.**

**Primitives (`market_intel/lifecycle.py`, read-only or copy-only):**
- `inventory(...)`: rows and bytes per class.
- `archive_candidates(..., older_than_days=N, now=...)`: the bodies that would be eligible, each with the reason the others are not
  (`UNSEALED`, `UNANCHORED`, `UNVERIFIED`, `BODY_MISMATCH`, `TOO_RECENT`). It is a dry run.
- `export_raw(candidates, destination)`: byte-identical copies, re-verified, with a manifest. It refuses a destination inside the hot
  archive or the project's `data/`, never deletes or edits a source, and re-running it is idempotent.
- `disk_status(path, min_free_bytes=...)`: OK or LOW, with no default threshold.

## 5. Integrity verification and crash recovery

- **v2 `verify_integrity`** is the 7.1 verdict (rows, members, seals, anchors in both directions) plus:
  - each COLLECT dataset has exactly one claim, and each claim's dataset is COLLECT;
  - news runs recompute: digests, counts, clusters, mentions, point-in-time inputs, record hashes.
- **Crash during a write.** Every write is one `BEGIN IMMEDIATE` transaction, so a crash leaves no half row. The 7.1
  `recover_pending` (which the 7.3 collector's `recover()` calls) seals abandoned datasets and anchors sealed ones; it works unchanged
  on v2.
- **Crash during a migration.** The rollback leaves `.partial` at v1 or absent, and the next run refuses until an operator removes it.
  A crash during promotion leaves one of the five states in section 1, step 7, each refused by the next run. The source is never
  touched.
- **Concurrent first creation.** Writers racing to create a brand-new file can fail the switch to WAL with `database is locked`,
  because SQLite does not wait for that lock. The v2 store switches only when the file is not in WAL yet, and retries within the busy
  timeout. The Stage 7.1 store sets WAL on every open and keeps this race; it is frozen, so it is recorded here (section 11), not
  changed.
- **Disk full.** SQLite rolls the transaction back. The 7.3 run turns a seal that cannot be written into `PENDING_FINALIZATION`, and
  recovery seals it later. `disk_status` lets a future wiring stage refuse new runs before that point (a 7.3.2 decision).

## 6. Compatibility with Stages 7.0–7.3

- **No existing file changes.**
  - 7.0 contracts, the 7.1 store, migrations, approvals and backfill, the 7.2 adapters and news logic, and the 7.3 collector are all
    unchanged.
  - The new modules import them read-only.
  - The frozen trading code, `config.py`, `api/server.py` and `requirements.txt` are untouched; F01 pins them.
- **Unchanged guarantees:**
  - the fake-only transport and the default refusal of real requests;
  - `SEC_ACCEPTANCE_TZ_UNVERIFIED`, with both SEC plans blocked (the collect authorization refuses them too);
  - provider budgets, watermark protections and chronology validation;
  - the collector refusing live sinks.
- **Reading across versions.** A v1 file opens only with the 7.1 store. A v2 file opens only with the v2 store, and FIXTURE, IMPORT and
  BACKFILL behave in it exactly as in v1.

## 7. Authorization boundaries for future live ingestion

Before any live COLLECT dataset can exist, every one of these is needed. None of them is done here.
1. A **real-transport stage**, separately approved, and the **7.3.2 wiring decision**.
2. A **schema v3** (or v2.1) migration that admits a `LIVE` purpose in the claim table. A code change alone cannot do it, because the
   `CHECK` refuses it.
3. **Live collect authorizations** with operator signatures (DESIGN_71 section 8: a key kept off this machine), per source and scope,
   time-bounded, with caps.
4. **Per-provider approvals** (DESIGN_73 section 7): keys, terms, the SIP feed, the SEC acceptance timezone, and FRED licensing.
5. **The production migration itself**: copy, verify, promote, with a rollback plan, approved separately.

## 8. Storage growth and disk pressure

These are estimates, to be measured on the first approved run. Structural bounds are tested.

**Per collector run.** Store rows: one dataset, two events, one or more fetch rows, two or three budget events per request, and one
member per delivered row. Collector state: about five rows. One raw body per page.

**At the 7.3 cadences with one scope per source** (news 480, snapshots 420, halts 960, plus 2 daily runs a day):

| Item | Per day |
|---|---|
| store bookkeeping | about 1,860 runs × 8 rows (about 15k rows, a few MB) |
| facts | grow with what changes, for example snapshots: 420 × the number of symbols |
| raw bodies | mostly halts (960 × a few KB gzipped) and news pages |

The expected order of magnitude is tens of MB a day, so a few GB to tens of GB a year, dominated by raw payloads. That is why raw
payloads are the archivable class.

**News runs.** A run stores one input row per article in its window and one row per cluster or mention. A new run is written only
when the inputs change (a new article, or a new revision); unchanged inputs return the existing run. In a busy news hour that can be one
run per poll, about 2 × the articles in the window, in rows each. That is why derived-run thinning is listed as a decision.

**Bounds that hold by construction:** `daily_row_bound(plan)` (7.3), the per-run page, byte and row caps, and the per-dataset row cap.

**Disk pressure.** Writes fail atomically. A full disk turns a run into `PENDING_FINALIZATION`, never into a false COMPLETED, and a
future wiring stage can refuse runs below a free-space threshold (`disk_status`).

## 9. Bookkeeping

- **New only.** `BASE_731` is `ad60a02`. `NEW_731` is the new modules, the test file and this note. `SEMANTIC_731` and
  `INSTRUMENTATION_731` are empty.
- **`EDITED_731` is bookkeeping only:**
  - the CI manifest;
  - the earlier F01 unions;
  - the 7.0, 7.1 and 7.2 `later` sets, and the 7.1 module registry;
  - the 7.3 F tests, which now read what 7.3 left at `BASE_731`;
  - `tests/freeze_ledger.py`.
- `ROADMAP.md` and `PROJECT_STATE.md` live outside the repository and are updated with the operator's designation decision.

## 10. Decisions requiring operator approval

1. **Retention periods** (section 4): the hot raw-payload age, the cold medium, derived-run thinning and collector-state compaction.
2. **The production migration**, if v2 is adopted (copy, verify, promote, rollback), as its own approval.
3. **The entity-map source** for mentions: the SEC company-ticker map, how often it is refreshed, and the aliases.
4. **7.3.2 wiring**: a proposal only.
5. **Live COLLECT**: the section 7 list, unchanged.

## 11. Adversarial self-review (Phase 4)

| Area | Finding | Resolution |
|---|---|---|
| Migration safety | Two migrations to one target could share one `.partial` (a check-then-write race) | Fixed: the `.partial` is claimed exclusively (test V02) |
| Migration safety (operator review of `75b3051`) | Promotion re-checked the target and then called `os.replace`: a file created between the check and the call was overwritten. The report was written with an overwriting call too | Fixed: promotion and the report use a hard link, which creates a name or fails and has no replacing mode, with no fallback (`ATOMIC_PROMOTION_UNSUPPORTED`). The linked name is verified to be the same file with the verified bytes, and the report records `target_sha256`. Tests V03 (another process creating the target, the report or the report's copy immediately before or after the call; no hard links; a swapped or changed file), V04 (concurrent processes), V05 (a crash at every step, and recovery). The earlier V02 check ran before the window, so it never reached the race |
| Migration safety | The backup carries the source's WAL mode into `.partial`, so promotion relied on an implicit checkpoint and left orphan `-wal`/`-shm` files | Fixed: the copy is switched to a rollback journal before any work, and promotion refuses any side file (`POST_SIDE_FILES`). The migrated target is one file (test M01) |
| Test harness | With a production guard mutated away, the production-path tests could write into the repository's `data/` (seen once, in this worktree only; removed) | Fixed: those tests use stand-in production paths under `tmp_path`, and the mutation harness stops if any file appears in `data/` |
| Authorization | A COLLECT dataset could keep recording fetches and rows after its authorization expired | Fixed: refused by the store and by schema triggers; sealing and recovery still work (test V01) |
| Provenance and point in time | Stated inputs and outputs of a news run were trusted until verification | Fixed during implementation: `record_news_run` re-derives the known input set and recomputes the outputs inside its transaction (`NEWS_INPUTS_NOT_KNOWN_SET`, `NEWS_OUTPUT_MISMATCH`; tests N02, N03) |
| Concurrency | Racing writers on a brand-new file failed the switch to WAL (`database is locked`) | Fixed in the v2 store (test M05). **Pre-existing in the frozen 7.1 store**, not changed: recorded for the operator |
| Storage growth | News runs grow with articles × input changes | Documented (section 8); thinning is an operator decision |
| Backward compatibility | none | A non-COLLECT seal is exactly the 7.1 seal; the 7.1 recovery and reads behave identically on v2 (tests M01, V01) |
