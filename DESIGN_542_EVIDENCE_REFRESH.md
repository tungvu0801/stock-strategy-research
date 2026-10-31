# Stage 5.4.2 — automatic evidence refresh

Status: implemented behind `EVIDENCE_REFRESH_MODE` (default `OFF`). Zero model calls, no broker call, no order, no READY-rule or
threshold change, no schema change.

## 1. Problem

The setup's READY check needs a COMPLETED Stage 5.4 evidence packet that is at most `setups.EVIDENCE_MAX_AGE_DAYS` (30) whole days
old; older evidence reads `READY_BLOCKED_EVIDENCE_STALE`. A packet was only ever built when a person pressed Build
(`POST /api/evidence/packets`). The ten watchlist packets built on 2026-10-31 06:08–06:10 UTC therefore all go stale on
2026-12-01 (age 31 days) unless someone rebuilds them. Separately, the SEC `companyfacts` JSON of most large caps exceeds the
2 MB `EVIDENCE_MAX_BYTES` cap, so 6 of those 10 packets lost their fundamentals rung (a `TOO_LARGE` fetch; the packet still completed).

## 2. What it does

One daemon thread per process (`evidence_refresh/scheduler.py`), started from the server lifespan only when
`EVIDENCE_REFRESH_MODE` is `DRY_RUN` or `ON`, never in a test / harness / pytest process. After a start-up delay (120 s) it runs
`EvidenceRefresher.run_once` every `EVIDENCE_REFRESH_CHECK_SECONDS` (900 s). A run:

1. **Mode** `OFF` → nothing. `DRY_RUN` → plan and log "would build", build nothing. `ON` → build.
2. **One run at a time** (in-process lock → `RUN_IN_PROGRESS`).
3. **Outside the regular session only** (`setups.market_hours`, the evaluator's own calendar); evidence never changes under an open
   session. The rule governs **both ends of a build**:
   - *Start.* No run and no build starts during the regular session or less than `PRE_OPEN_MARGIN_S` (180 s: the engine's
     `build_s` limit plus a minute) before the open (`default_session_blocked`). Otherwise the result is `SKIPPED_REGULAR_SESSION`;
     a run that reaches the margin stops (`SESSION_OPENED`).
   - *Persistence.* The insert re-checks the regular session inside the store's write transaction (`persist_unless_superseded`). A
     build that finishes at or after the open is discarded (`DISCARDED_SESSION_OPEN`): nothing is stored, it is not a failure (no
     back-off, no journal entry, no budget spent) and the run stops. The symbol is rebuilt after the close.
   - *In-flight fetches.* A build that overruns the margin keeps fetching until the engine returns (the engine is frozen and cannot be
     interrupted). Its result is then discarded. At most one such build per open, because the run stops.
4. **Evidence network enabled** (`EVIDENCE_NETWORK_ENABLED`) → otherwise `SKIPPED_NETWORK_OFF`.
5. **The shared lease** `evidence_refresher` (`app_leases`, 15 min, renewed before every build) → one refresher per database
   whatever the number of processes; `LEASE_HELD` otherwise, `LEASE_LOST` mid-run stops it.
6. **Plan** (`evidence_refresh/policy.py`, pure) over the watchlist (`scanner.watchlist.load_watchlist`, the file the setup
   evaluator watches), then **sequential builds** with `EVIDENCE_REFRESH_SPACING_SECONDS` (20 s) between them.
7. **Circuit closed** (section 4): after a run stopped on consecutive errors or a refusing source, no build until it closes
   (`CIRCUIT_OPEN`).
8. **Before each build the symbol is re-checked** against the store and the attempt journal; if a packet appeared meanwhile (a
   manual Build, another process) it is skipped (`SKIPPED_ALREADY_FRESH`).
9. **The build** is the Build button's engine call: `evidence.engine.build_packet(EvidenceRequest(ticker=sym))` with the default
   adapters — no new pipeline, no optional explanation (`evidence/explain.py` is never imported). The one difference: it is built in
   memory (`persist=False`) and stored by `persist_unless_superseded`, which decides **inside the store's own write transaction**
   (`EvidenceStore.write()`, `BEGIN IMMEDIATE`) whether to insert. If a FRESH packet was stored while this build was fetching, or the
   identical packet (same `packet_hash`) is already stored, nothing is written (`SKIPPED_SUPERSEDED`). Otherwise it INSERTs exactly the
   rows `EvidenceStore.insert_packet` writes. Every other writer waits for that transaction, so nothing can land between the check and
   the insert.

## 3. Per-symbol policy

The packet a symbol is judged by is the packet the READY check reads: `trader_view.research.pick` (Stage 5.4.1 — the newest
COMPLETED packet created at or before now; a newer FAILED one never hides it). Age is the setup service's own formula
(`max(0, (now - created_at).days)`).

| State | When | Decision |
|---|---|---|
| FRESH | age < `EVIDENCE_REFRESH_AGE_DAYS` (7) | WAIT until `due_at` |
| DUE | 7 ≤ age ≤ 30 | BUILD |
| STALE | age > 30 (READY already blocked) | BUILD, first |
| MISSING | no COMPLETED packet | BUILD, second |

Back-off overrides BUILD. A failed attempt is a FAILED packet **or** a journal error (an attempt that stored no packet, section 4);
both count the same. The last failed attempt newer than the valid packet waits `EVIDENCE_REFRESH_RETRY_HOURS` (6 h, `RETRY_WAIT`),
and a symbol with `EVIDENCE_REFRESH_MAX_FAILURES_PER_DAY` (2) failed attempts in the trailing 24 h waits for the window to clear
(`FAILURE_CAP`). A symbol whose packets cannot be read waits (`READ_ERROR`); it is never read as MISSING. Order: STALE, MISSING, then
DUE oldest first. Budget per run `EVIDENCE_REFRESH_MAX_BUILDS_PER_RUN` (3) and per trailing 24 h `EVIDENCE_REFRESH_MAX_BUILDS_PER_DAY`
(12, counting every packet of the in-scope symbols, manual ones included, plus every journal error). All state is derived from the
append-only `evidence_packets` rows and the durable journal, so it survives restarts; the in-memory history is for display only.

With the defaults a ten-symbol watchlist refreshes each symbol about weekly and has 23 days of slack before READY would block.

## 4. Failure handling

* A FAILED build is an ordinary append-only FAILED packet (the engine's own behaviour). The gate keeps reading the previous
  COMPLETED packet; the symbol backs off.
* **A build that raises stores no packet** — the adapter factory, the engine's post-processing after the fetches, or a locked or full
  database during the insert. It is recorded (`ERROR`) in the **attempt journal** (`evidence_refresh/journal.py`): a small JSON file
  `runtime/evidence_refresh_journal.json` (git-ignored, beside `account_refresh.fault`), written atomically by the lease holder only,
  pruned after 48 h. A journal error counts exactly like a FAILED packet (retry wait, daily cap, daily budget), so the next run — 15 minutes
  later, or after a restart — does not retry that symbol. Worst case with the defaults: 2 failed attempts per symbol per 24 h, 6 h
  apart, and at most 12 attempts of any kind per 24 h across the watchlist (`test_J01`: 4 attempts in 48 h of 15-minute runs with a
  restart before every run, against 192 without the journal).
* Two errors in a row stop the run (`CONSECUTIVE_ERRORS`). A source that refuses us (`ACCESS_DENIED` — 401/402/403/429 in
  `evidence/net.py`) stops the run (`SOURCE_REFUSED`). Either **opens the circuit** for `EVIDENCE_REFRESH_RETRY_HOURS`, recorded in
  the journal: every run until then is `CIRCUIT_OPEN` and builds nothing. No retry loop anywhere; every URL is still one attempt.
* The journal is operational state, never evidence: the evidence tables are untouched and there is no schema change. Reading a
  missing file creates nothing. An unreadable file reads as empty and is reported, and the next write replaces it. If the file cannot
  be written, the in-memory copy still bounds retries for the life of the process.
* A store read error makes that symbol wait in the plan (`READ_ERROR`), and "do not build this symbol now" in the re-check.
* Shutdown interrupts the spacing wait; a build already running finishes inside the engine's own `build_s` limit (120 s).

### Concurrent manual Build

| Order | Result |
|---|---|
| The manual packet is stored while the refresh is fetching (before its insert) | The refresh stores nothing (`SKIPPED_SUPERSEDED`, `FRESH_PACKET_STORED`); the manual packet is the gate packet (`test_C01`, the real POST route) |
| The refresh holds its write transaction when the manual insert arrives | The manual insert waits, then is stored; both packets kept, the newest is the gate packet (`test_C02`) |
| The identical packet (same `packet_hash`) is already stored | Nothing inserted, no `IntegrityError`; bounded like an error (`test_C03`) |

No case modifies or deletes a stored row (append-only triggers; `test_C04` also proves the guarded insert writes exactly the store's
own rows). A person's Build is never refused or delayed beyond one transaction. Not prevented: both builds may fetch at the same time
(one wasted set of SEC requests), because the manual route is frozen and takes no lock.

## 5. Oversized SEC files

`SecAdapter.fundamentals` passes `EVIDENCE_SEC_FACTS_MAX_BYTES` (25,000,000) for the `companyfacts` request only; every other fetch
keeps `EVIDENCE_MAX_BYTES` (2 MB). It is still one bounded request (same timeout, same request budget, same host allow-list). This
is the one intentional evidence-content change of the stage: large caps regain their XBRL fundamentals rung, which can change a
packet's strength and the `company_down` / guidance inputs the READY evidence check already reads. The READY rule itself is unchanged.

## 6. Visibility

`GET /api/evidence-refresh` (read-only): mode, thread status (runs, last result, totals, the last 20 attempts, the journal), and the
plan computed now (each symbol's state, packet, age, `due_at`, `stale_at`, back-off, errors, the circuit, and what would be built
next). `GET /api/evidence-refresh?at=<ISO time>` evaluates the same plan at a later instant (up to 90 days ahead): a preview of the
decision path on the stored packets. Reading it builds nothing, fetches nothing and writes nothing.
The runner log gets one line per build and per non-trivial run. A display of this in the TRADE ENGINE fold is a follow-up.

## 7. Boundaries (pinned by tests/test_evidence_refresh_542.py)

No model call (the refresh path never imports the explanation module; tests make every provider raise). No broker call, no
order, no Robinhood call (tests make the broker raise). No change to MONITOR_ONLY, Paper Auto, MANUAL_ARM, READY thresholds, PPE
scoring or the frozen controls hash. No schema change, no deletion, no update of an existing packet (the tables' triggers forbid it).

## 8. Rollout

Deploying this code changes nothing while `EVIDENCE_REFRESH_MODE=OFF`. Suggested: `DRY_RUN` for one evening (check
`GET /api/evidence-refresh` and the log), then `ON` in `runtime\live_paper.env`. Each step is an operator decision and a restart.
While every live packet is still FRESH, a DRY_RUN evening only shows `NOTHING_DUE`. The decision path itself is proven by
`test_D01` / `test_D02`: real engine packets that are DUE, STALE, MISSING, FRESH and backing off, run through the scheduler thread
with the production defaults. On the live store, `GET /api/evidence-refresh?at=2026-11-08T03:00:00Z` shows what a run on that date
would build.
