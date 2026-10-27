# Stage 6.10A — Forward Paper Auto-Run + Data Capture

Base: main `27926f627c5cb83a6e1b3b6a9b646ce455cfbe52` (Stage 6.9.1 merged, PR #33). Alpaca PAPER only. Robinhood stays READ ONLY / USER_MANAGED / REAL and is never traded.

This stage does not make the strategy more profitable. It makes forward paper evidence **trustworthy**:

```
startup → preflight → reconcile → arm Paper Auto only under explicit policy → deterministic cycles
        → record every candidate / decision / block / order / outcome → survive and recover → forward-validation evidence
```

Two packages are new: `autorun/` decides **when** Paper Auto may be armed, and `forward_capture/` records the point-in-time forward dataset.
How an entry or exit is decided (entry controls, exits, sizing, risk) is unchanged.

## 1. Declaration: what changes execution, and what only records it (the STOP-and-explain item)

The stage specification asks for arming and restart rules (§4, §5, §7, §8). These are an **intentional execution-semantics change** of *when* the
executor may enter. They are declared openly in `tests/freeze_ledger.py` and are not disguised as instrumentation:

* `SEMANTIC_610A` covers `api/server.py`, `autorun/service.py`, `autorun/policy.py`, `autorun/scheduler.py` and `config.py`. These files contain:
  * the startup demotion;
  * the automatic disarms;
  * the opt-in scheduled arm;
  * the policy settings.
* `INSTRUMENTATION_610A` covers two files, which only record what happens:
  * `execution/service.py`: six `self._record(lambda: self._mark(...))` markers plus `_mark`;
  * `forward_validation/service.py`: the capture tick and read-only views.

The restart rule supersedes DESIGN_60 §1 ("the persisted mode is restored after a restart") for MANUAL_ARM and SCHEDULED_ARM. DESIGN_60 itself is
pinned and is not edited.

**Order-decision logic is unchanged.** `tests/test_forward_paper_610a.py` F02 and F03 prove it:

* `execution/engine.py` is byte-identical to the base.
* Every added executor line is a `_record` call or inside `_mark`. `_record` already swallows every failure, so a recorder fault can never
  change or interrupt order handling.
* The hashes are the Stage 6.9.1 values:

| Hash | Value |
|---|---|
| Stage 6.1 frozen strategy controls | `b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530` |
| Entry contract, gate off | `2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa` |
| Entry contract, gate on | `d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4` |

The modes are still `MONITOR_ONLY` and `PAPER_AUTO`.

## 2. Auto-run policy

| Setting | Default | Meaning |
|---|---|---|
| `PAPER_AUTO_RUN_POLICY` | `MANUAL_ARM` | MANUAL_ARM: every start or restart comes up in Monitor only, and a person arms Paper Auto. SCHEDULED_ARM: see §5. An unknown value is MANUAL_ARM, and the preflight blocks on it. |
| `PAPER_AUTO_SCHEDULED_ARM_ENABLED` | `false` | SCHEDULED_ARM is effective only when this is also true. **PAPER_AUTO_SCHEDULED_ARM_ENABLED=false** by default, and it is never enabled during development. |
| `PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES` | `2` | Minutes after the 09:30 ET open before a scheduled arm. |
| `PAPER_AUTO_DISARM_AT_SESSION_END` | `true` | Paper Auto never carries over a session close. |
| `AUTORUN_SUPERVISOR_ENABLED` | `true` | Lease renewal, preflight and automatic disarms. Turning it off is not recommended: the startup demotion still runs, but nothing disarms automatically. |

## 3. Scheduled startup (06:15 PT) and the startup step

A weekday task can start the app at **06:15 PT** (09:15 ET), 15 minutes before the 06:30 PT open. The startup runs in this order:

1. **Single-instance guard.** The lifespan takes the `stock_agent_instance` lease (TTL 3 min). The supervisor renews it at the start of every tick, at most 60 s apart.
2. **Paper broker.** The PAPER host is the only host the transport allows. The account refresh proves the paper account read-only. `enable_auto` forces its own proof GET before any arm.
3. **Database.**
4. **Worker.** The executor and observer threads start only after steps 1 and 9.
5. **Reconcile.** The executor reconciles before any write whenever anything is open. A Paper Auto cycle always reconciles before entries.
6. **Account refresh.** Its first action is a STARTUP refresh.
7. **Robinhood.** One read-only refresh in the first supervisor tick, if `ROBINHOOD_PORTFOLIO_ENABLED`. It is context only.
8. **Safety state.** The safety startup evaluation, then the preflight.
9. **MONITOR_ONLY.** A persisted PAPER_AUTO is demoted through `ExecutionService.disable_auto`, with actor `autorun_policy` and a reason starting with `MANUAL_ARM: process start`.

**No orders during startup.**

The step fails closed:

* If another instance owns the lease, the process stays **passive**: no executor, no observer, no disarm, and the other instance's mode is left alone.
* If the demotion cannot be written after 3 attempts, the lifespan starts neither the executor nor the observer.
* In both cases the supervisor retries every tick. It starts the threads only after it owns the lease and the demotion is persisted.

A crashed predecessor's lease lapses within 3 minutes and is then taken over. A live owner is recognised because its `expires_at` advances
between two reads. A static row is only an expiring lease.

More lease rules:

* The lifespan marks the threads it started (`mark_started`), so a normal start is never recorded as a takeover.
* The lease is released as the **last** step of a shutdown, after the executor has stopped.
* With `AUTORUN_SUPERVISOR_ENABLED=false` no long-held lease is taken, because nothing would renew it. The demotion still runs.

## 4. Preflight: READY | DEGRADED | BLOCKED

`autorun/policy.py preflight()` is pure and is computed from read-only facts (`autorun/facts.py`). It makes **never a broker call** and never takes the executor lock.

| Severity | Checks |
|---|---|
| BLOCKED | config invalid; database unreadable; single instance (another live owner, passive, or a foreign executor); a foreign `stockagent-` open order unknown to this database; paper proof lost or no paper broker; Emergency Stop; ambiguous order / position state; reconciliation findings; unsupported entry-controls version; executor loop disabled |
| DEGRADED | orders in progress; executor loop not alive; account data not FRESH; forward-validation observer not running |
| INFO (never gates) | market clock and calendar; historical forward-validation attention; external (Robinhood) state; capture faults |

The preflight is persisted (`autorun_preflights`) when its status or failing checks change, and at least hourly.

## 5. Automatic arming (SCHEDULED_ARM, opt-in)

A scheduled arm requires every one of these:

* preflight READY;
* paper broker configured and proof not lost;
* a market day in the REGULAR session, at least the delay after the open and before the near-close cutoff;
* reconciliation clean;
* no Emergency Stop;
* safety TRADING_ALLOWED;
* no open or ambiguous intent and no ATTENTION position;
* account FRESH;
* executor loop alive;
* this process holds the lease alone;
* the observer (which hosts the Stage 6.1 early stop) running;
* the entry controls supported;
* no disarm today by a person or by a rule (the startup demotion excepted);
* no earlier attempt today.

The attempt is written ahead in `autorun_days.scheduled_arm_at`, so there is exactly one attempt per trading date. A refusal is recorded and not
retried. Under the executor lock every condition is checked again (an Emergency Stop or an order that appeared during the wait), and the latest
mode event is re-checked. `enable_auto` then applies all of its own refusals.

## 6. Automatic disarm (Paper Auto → Monitor only)

Disarming means **new entries stop**. Monitoring, reconciliation and deterministic exits continue, because `needs_broker()` keeps cycles running while
anything is open. Exits are never stranded (tested: an Emergency Stop disarm followed by a completed exit).

| Code | Condition |
|---|---|
| EMERGENCY_STOP | the kill switch is active |
| PAPER_PROOF_LOST | a definitive proof failure (NOT_CONFIGURED / HTTP_401 / HTTP_403, account not ACTIVE or blocked), or 3 consecutive failed proofs |
| RECONCILIATION_AMBIGUITY | a RECONCILIATION_REQUIRED intent, an ATTENTION position, or an attempted intent without a broker id for 120 s |
| ACCOUNT_EQUITY_UNAVAILABLE | account data not FRESH for 300 s in the regular session |
| BROKER_READ_FAILURES | 3 consecutive failed refreshes seen by this process (a previous process's failures alone never disarm) |
| WORKER_FATAL | the executor loop is stale (past its own next decision, or one cycle running for more than 10 min) or stopped, or 3 cycles in a row failed |
| DATABASE_FAILURE | the database is unreadable. Sticky: an outage while armed disarms as soon as the mode can be written again |
| UNSUPPORTED_CONTROLS | the entry-controls version is not supported, or the enabled gate's configuration is invalid |
| DUPLICATE_INSTANCE | another owner is renewing the lease, or another executor ran cycles on this database since this start |
| FOREIGN_INSTANCE | an open `stockagent-` order on the paper account is unknown to this database |
| SESSION_ENDED | armed before the latest session close (`PAPER_AUTO_DISARM_AT_SESSION_END`) |

Every disarm writes an `AUTO_DISARM` event and the day record. The supervisor never waits more than 20 s for the executor lock; it retries at the
next tick, and the executor re-reads the mode before every POST anyway.

Under the lock the conditions are gathered again, and one that cleared while a cycle ran never disarms. Liveness is judged from the loop's own
next decision: a slow cycle that ends late is not a stale loop. An attempted order counts as ambiguous only while no cycle is running, because
an in-flight POST gets its answer within its cycle. A cached broker clock older than 10 minutes, or from another day, is ignored.

## 7. Market sessions

| Session | Window (New York time) |
|---|---|
| PREMARKET | 04:00–09:30 |
| REGULAR | 09:30 to the close (16:00, or 13:00 on an early close) |
| AFTER_HOURS | the close to 20:00 |
| CLOSED | everything else: weekends, holidays, and a regular-session moment the broker clock reports closed |

Holidays come from a static, versioned table, `nyse_2026_2027_v1`. A known broker clock overrides it.

New autonomous entries stay **regular session only**, through the executor's unchanged SESSION_POLICY control and near-close cutoff. No extended
hours were introduced.

## 8. The forward dataset (`forward_capture/`)

**Capture path.** Executor markers (SCAN, EVALUATED, SKIPPED, DUPLICATE, INTENT, CYCLE_END) are built as allowlisted plain dicts at decision time
and handed to a bounded in-memory sink: no I/O and no executor lock. The forward-validation observer thread is the **only writer**. It drains the
sink and also indexes, in short transactions with a 1 s busy timeout:

* the setup evaluations;
* the entry intents;
* the positions.

**Tables.** Rows are append-only with no-update / no-delete / no-replace triggers. Inserts are "insert if absent"; the same id stored again with a
different content hash is recorded as a CONFLICT fault and never overwrites.

| Table | Holds |
|---|---|
| `forward_capture_candidates` | one row per setup episode: `candidate_id` = sha256('candidate\|' + setup_id)[:32], symbol, setup type, cycle, strategy id and version, first seen, cohort, `legacy` |
| `forward_capture_decisions` | one row per observation or decision (see below) |
| `forward_capture_entry_snapshots` | immutable, one per entry intent |
| `forward_capture_outcomes` | immutable, one per closed position |
| `forward_capture_context` | other accounts' states and freshness only |
| `forward_capture_faults` | recording faults |
| `forward_capture_config_snapshots` | the config snapshot (§9) |
| `forward_capture_cohorts` | the cohort (§9) |
| `forward_capture_session_health` | the health record (§10) |
| `forward_capture_state` | the watermark and capture epoch |

**Decision rows** carry `decision_id`, `candidate_id`, decided / observed time, trading date, market session, mode, source, decision, reason codes,
setup state, features (the setup inputs at that time), score (PPE confidence), proposal, PAPER account, exposure context, controls version and
contract hash, the per-control results, `execution_id`, `position_id`, `run_id`, `recovered` and `legacy`.

**Decisions**

| Decision | Meaning |
|---|---|
| NOT_READY | WATCH, INVALIDATED or closing evaluations |
| READY | READY under Paper Auto, awaiting the executor |
| SKIPPED | READY in Monitor only (`PAPER_AUTO_OFF` plus the blockers knowable without a broker call: session, safety gate, proposal validity), or `ENTRIES_PER_CYCLE` |
| BLOCKED | the blocking control ids (safety, session, max positions, broker position, unified external exposure ...) or `DUPLICATE_ENTRY` |
| ENTRY_INTENT | the entry intent was created |
| NOT_ENTERED | the intent resolved without a fill (rejected, not sent, canceled) |
| ENTERED / EXITED | from the persisted positions |
| NOT_EVALUATED | an evaluation error or pause, or a cycle that ended early (INCOMPLETE) |

TEST-trigger evaluations are never evidence; MANUAL ones are flagged.

**Ids and lineage**

| Id | Derived from |
|---|---|
| `decision_id` EVAL | sha256('decision\|EVAL\|' + evaluation_id) |
| `decision_id` EXEC | run_id + setup_id |
| `decision_id` ENTRY_INTENT / NOT_ENTERED | `execution_id` |
| `decision_id` ENTERED / EXITED | `position_id` |

The chain candidate → decision → intent (`execution_id`) → order (`client_order_id` / broker order events) → fill → position (`position_id`) → exit →
outcome is reconstructable from ids alone. The FV trades export carries `candidate_id`, `entry_decision_id` and `cohort_id`.

**Entry snapshot** (immutable, at intent creation):

* symbol, strategy version, signal time;
* planned qty, reference and invalidation price, risk per share and risk budget (the strategy defines no fixed reward level: exits are rule-based);
* controls version, contract hash and all entry-control results;
* exposure gate verdict and external context;
* market session and regime fields (stretch, confidence, demand, evidence);
* PAPER equity, cash and buying power;
* mode and safety.

**Outcome snapshot** (immutable, at close):

* entry / exit time, holding seconds and sessions;
* average entry / exit, realized P/L, return %, exit reason, slippage;
* controls and strategy version;
* MFE / MAE from the observer's **sampled marks**, labelled so. With no mark they are `NOT_AVAILABLE`, never invented.

**No outcome leakage.** Features are frozen when recorded. Point-in-time lookups use only rows with `at <= decided_at`: the mode, the FV account
sample, the safety evaluation, the proposal and the context.

**Kept markers.** Markers drained by a tick that fails before recording them go back to the front of the sink, so the next tick records them.
Any that do not fit are an EVIDENCE_LOST fault, which makes the day INVALID.

**Recovered rows.** An entry intent whose decision-time marker was lost (a crash, a previous process, or a failing recorder seen for 120 s) is rebuilt
only from the persisted intent and the last sample before it. It is flagged `recovered=1`, its controls are `UNAVAILABLE`, and an
`ENTRY_RECOVERED` fault is written. It is never presented as decision-time evidence.

## 9. Cohort and config snapshot

`forward_capture/cohort.py` builds the **config snapshot** from an allowlist of code constants and config values only. It includes no
environment dump, credential, host or account number. The snapshot contains:

* strategies and lifecycle;
* package versions;
* the frozen controls and their hash;
* the entry-controls contract;
* executor limits, cadences and flags;
* broker kind and market-data mode;
* session policy and auto-run policy;
* the universe and its sha;
* calendar version and app identity.

`config_hash` is the canonical SHA-256 of the snapshot. Snapshots are written at process start (STARTUP), on the first tick of a trading date (DAY)
and on any change (CHANGE).

The **cohort key** hashes only the decision-relevant subset: executing strategy versions, frozen hash, entry contract hash, executor limits, broker
kind and market-data mode. Then `cohort_id = 'FP-' + key[:12]`, and the label is the year's sequence. The first is **FP_2026_01**.

A decision-relevant change starts a new cohort. Mixed cohorts on one day make the day DEGRADED. An outcome counts in its entry's cohort, and a
trade held across a cohort change is flagged.

## 10. Session health: HEALTHY | DEGRADED | INVALID

There is one record per market day; holidays are `MARKET_CLOSED`, and market days the process never saw are recorded, not skipped. The record is
operational only: **no P/L, return or win rate is an input**. A losing day can be HEALTHY (tested), and a profitable one with missing evidence can
be INVALID.

Coverage is measured over the expected regular minutes. A minute counts when an observer sample shows the condition within one observer cadence.

* **INVALID:** any of
  * Stage 6.1 session FAILED or NOT_RUN;
  * executor loop coverage < 75 %;
  * PAPER account FRESH coverage < 75 %;
  * reconciliation unresolved at the close;
  * candidate evidence lost (sink overflow, buffer full);
  * a duplicate or foreign instance (any disarm code, a failed foreign-order or single-instance preflight check, or a lost lease; Monitor only included);
  * inconsistent timestamps.
* **DEGRADED:** any of
  * the Stage 6.1 session record DEGRADED;
  * a restart in the session;
  * recovered capture;
  * capture faults;
  * cycle errors;
  * broker read failures;
  * data-source failures;
  * mixed cohorts;
  * the gate ON with partial external context;
  * an automatic disarm;
  * setup-evaluation gaps;
  * a READY candidate under Paper Auto without an executor decision within two cycles.
* **HEALTHY:** none of the above.

The record is upserted while open and frozen (`final`) 10 minutes after the close, once the Stage 6.1 session record is closed. No premarket
record is written.

Further rules:

* Setup coverage counts only evaluations that ran; PAUSED and ERROR rows are gaps, not coverage.
* A day's cohorts are the configurations in force: the snapshot at the open plus any change during the session. They are never inferred from decisions.
* A candidate counts as blocked only for a block **before** its entry.
* The stored Stage 6.1 daily summary is rebuilt once the record is final.

## 11. Forward validation integration

This extends Stage 6.1 rather than competing with it.

* `GET /api/forward-validation` gains `forward_evidence`, with per cohort:
  * healthy capture sessions (target 30);
  * READY candidate events (target 100);
  * blocked READY candidates;
  * closed trades (in their entry's cohort, cross-cohort holds excluded);
  * the existing Stage 6.1 status rule: INSUFFICIENT_DATA, EARLY_NEGATIVE, MIXED or EARLY_POSITIVE.
* Only FINAL day records count. Today's open record is reported apart as `in_progress`.
* An unknown drawdown is treated conservatively.
* No status approves a strategy. Nothing is tuned.
* The daily summary and report gain the day's FORWARD EVIDENCE block.

## 12. Exports (GET only)

The existing `GET /api/forward-validation/export/{kind}` serves CSV by default and JSON with `?format=json`. There is no new route and no delete or
edit endpoint. The 6.10A kinds are:

* `candidates`, `decisions`, `controls`;
* `session_health`, `entry_snapshots`, `outcomes`;
* `config_snapshots`, `cohorts`, `context`, `faults`;
* `autorun_events`, `autorun_preflights`, `autorun_days`.

The Stage 6.1 kinds `sessions` and `trades` complete the §22 set. Ordering is deterministic and unique.

## 13. Daily summary (reporting only)

```
Market session: HEALTHY
Candidates observed: 37
READY: 3
Blocked: 2
Entered: 1
Open: 1
Closed: 0
Paper Auto: 06:31-13:00 PT
Incidents: none
```

## 14. Bot Activity

`/api/execution/activity` gains two read-only keys:

* `auto_run`: the policy, the latest preflight, the startup outcome, the lease and today's arm / disarm record;
* `forward_session`: cohort, health, Paper Auto spans, cycles, candidates, READY, blocked, orders, positions and incidents.

TRADE › Bot Activity shows **Current mode**, **Last cycle ran in** and **Auto-run policy** as separate rows. A **Forward session** fold, collapsed by
default, is the first fold. No new fetch path was added. The layout was checked at 1920×1080, 1440×900, 1366×768 and 1280×720.

## 15. Robinhood and the gate

Robinhood remains READ ONLY / USER_MANAGED / REAL. There is no Robinhood order path. The unified state and each source's state and freshness are
recorded as candidate context:

* STALE_SOURCE;
* AUTH_REQUIRED (login expired);
* UNAVAILABLE (gateway down).

No holding, quantity, value, equity or label is recorded, and no zero exposure is ever fabricated. The Stage 6.9.1 gate stays **default OFF**.
When it is ON, its verdict is recorded as result, reason code, a fixed label, the threshold and the data state. Never recorded: its free text and
the projected symbol share (with the planned notional, the share would let the other accounts' invested value be recomputed).

## 16. Strategies

There is no tuning and no Strategy #5. DDR is the only setup type that reaches the executor. The research strategies (Value + Quality, Momentum,
Mean Reversion, Breakout) are recorded in the snapshot with `executes: false` and never trade because forward collection exists.

## 17. Robustness

| Fault | Behaviour |
|---|---|
| Crash at any entry point (after intent, after attempt recorded, after POST, before fill persisted) and during exit | Existing exactly-once / reconciliation behaviour holds. The capture never adds a trade or a duplicate decision. Recovered rows say so. |
| Recorder throws | Order handling is identical (tested against a clean run), and the gap shows as recovered rows. |
| DB locked | Evidence is delayed (kept and written next tick, WRITE_FAILED fault, DEGRADED day), never an order. |
| Required evidence lost | The day is INVALID. |
| Broker outage (timeouts, rejected, partial, stale) | No blind order retry; reconciliation stays the authority. |

## 18. Single instance

The per-cycle `paper_executor` lease, the runner's PID-file process guard and the database's exactly-once intent ids are preserved. On top of
them, the long-held `stock_agent_instance` lease makes a second process on the same database passive: it runs no executor, no observer and no
capture writes.

A foreign executor writing runs to this database, or a foreign `stockagent-` order on the paper account (another checkout on the same account), is
detected, disarms Paper Auto and BLOCKS the preflight. There is exactly one supervisor thread per process.

**A process that loses the lease** to another live instance disarms Paper Auto at once (DUPLICATE_INSTANCE). It stops its own executor and
observer only when the new owner demonstrably runs an executor (its cycles on this database). Until then exits keep running there: a lost lease
never strands exits.

**A process whose executor cannot run** (`EXECUTION_SCHEDULER_ENABLED=false`) never takes a database over from another owner.

The foreign-executor window starts when this process took the lease, so a predecessor's last cycles are never counted against its successor.

## 19. Scheduler cadence

| Component | Regular session | Outside it |
|---|---|---|
| Supervisor | 30 s | 60 s |
| Executor | 30 s | 300 s |
| Observer | 60 s | 300 s |

Closed-market supervisor ticks are database reads only and make no broker or API traffic. The executor is IDLE (no broker call, no write) in
Monitor only with nothing open.

## 20. Startup task (inspection, no install)

Inspected on 2026-10-26:

* `\StockAgentSmokeStart` and `\StockAgentSmokeRun` exist. Both are one-time triggers (2026-10-23) for the Stage 6.0 smoke worktree.
* No task named `StockAgent` exists.
* No task points at `stock-agent-live`.

Nothing was created or changed.

`scripts/install_stock_agent_task.ps1` is idempotent: registering the same `-TaskName` replaces it, and `MultipleInstances IgnoreNew`. It gains
`-OverridesFile`, so a scheduled start uses exactly the settings of a manual start (the live instance uses `runtime/live_paper.env`). Example:

```
.\scripts\install_stock_agent_task.ps1 -Trigger Weekdays -EasternTime 09:15 -OverridesFile runtime\live_paper.env -PrintOnly
```

It must not be installed on live during development.

## 21. Auto-restart

No restart loop was added. `start_stock_agent.ps1 -Foreground` keeps its existing bounded supervision: at most 3 restarts, 60 s apart, never after
a stop request. A dead or stuck executor loop is reported and disarms Paper Auto (WORKER_FATAL); it is never restarted blindly. A restarted
process comes up in Monitor only.

## 22. Read paths

Every read is `mode=ro`:

* activity;
* the FV overview, cumulative and daily views;
* exports;
* autorun views.

A read never migrates, never runs DDL and never creates a cohort, snapshot or health record. A missing table is an empty result. All writes
happen only in the observer tick, the supervisor tick or the lifespan startup step. Tested by comparing the schema and every capture row around
the read paths.

## 23. Security

Never recorded:

* credentials, broker or Robinhood tokens, the gateway secret;
* account numbers (masked or not);
* the paper host string;
* other accounts' holdings, values or labels;
* the gate's free text and its projected symbol share.

Allowlist projections (`forward_capture/project.py`) are the only way data reaches a table, export or view. A sentinel test scans every table,
export and view.

## 24. Proposed live rollout (not deployed in this stage)

* **Phase A**: deploy in MONITOR_ONLY (MANUAL_ARM, scheduled arm disabled). Verify the instance lease, the preflight and the Bot Activity rows.
* **Phase B**: observe one complete market session in Monitor only. The day's health must be HEALTHY, with candidates and READY decisions recorded as SKIPPED / PAPER_AUTO_OFF.
* **Phase C**: arm Paper Auto manually in the regular session and watch the first cycles, entries and disarms.
* **Phase D**: collect 3–5 healthy paper sessions. Review session health, faults and the exports; there must be no INVALID day without an explanation.
* **Phase E**: only then consider SCHEDULED_ARM, `PAPER_AUTO_SCHEDULED_ARM_ENABLED=true`, as a separate, explicit decision. There is no immediate unattended scheduled arm.

## 25. First paper limits (candidate settings: the final values require a user decision)

These are not changed automatically by this stage:

* at most 1–2 new positions per session (the existing cap is `EXECUTION_MAX_ENTRIES_PER_CYCLE=1` per cycle, with no daily cap);
* the existing max-open-positions cap (`MAX_OPEN_POSITIONS`);
* an optional `EXECUTION_MAX_SHARES_PER_ENTRY` hard cap;
* regular session only; no extended hours;
* no Robinhood writes, no options, no shorts, no leverage.

## 26. Freeze bookkeeping

| Item | Value |
|---|---|
| `BASE_610A` | `27926f627c5cb83a6e1b3b6a9b646ce455cfbe52` |
| New files | `NEW_610A` |
| Edited files | `EDITED_610A` |
| Ledger | re-hashed entries for registered edits; new entries for `api/routes/forward_validation.py`, `tests/test_windows_runner_60.py`, `scripts/install_stock_agent_task.ps1` |

Edits to files main did not have at BASE_68 (`execution/activity.py` and the three earlier stage tests) are pinned by exact replay lists in
`tests/test_forward_paper_610a.py` F04. The 6.8, 6.9 and 6.9.1 freeze tests now read their own stage's result at BASE_610A, plus a keep-lines guard
on the working tree.
