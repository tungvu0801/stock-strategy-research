# Wave 1 — execution safety and credential hygiene (remediation of the independent validation audit)

Code and synthetic tests only. Base: `main` at `4c01519` (PR #49, Stage 7.8B-1), the audited commit. Findings addressed (workspace documents
`VALIDATION_GAPS_AND_RISKS.md`, `VALIDATION_REMEDIATION_PLAN.md`, Wave 1): **EX-08 / EX-07** (W1-1), **EX-08b** (W1-2), **EX-14** (W1-3) and
**EP-13** (W1-4). The changes after independent review round 1 (findings H1, M1, M2, L1, L2, L6, L7, L8) are included. Nothing was fetched,
backtested, deployed or ordered; the running bot is unchanged. No strategy rule, threshold, frozen control (`b5d4a56e...`), market-data reader,
broker call or Stage 7.8B-2B tool changes.

## 1. The safety state can no longer lose a latch (W1-1, W1-2; `safety/store.py`, `safety/service.py`)

**Finding.** An evaluation read the state row, read the account (slow), then wrote the whole row back. A user action computed its new row from a
read taken before the evaluation started. Any write in between was overwritten:
- an Emergency Stop set during a background evaluation was cleared by it;
- a Resume racing a Stop cleared the Stop;
- a Clear racing a Pause dropped the Pause;
- the day's loss halt was lost to an evaluation that had read the state before the halt.

An unreadable state row read as the default state, "no kill switch". `current()` served a cached view that could hide a stop written by
another process.

**Mechanism (optimistic concurrency).**
- `SafetyStore.commit(..., expect=record)` checks, inside its existing `BEGIN IMMEDIATE` transaction (the write lock), that:
  - the persisted row still equals the row the caller read (every state column except `updated_at`);
  - when the commit would create today's baseline, no baseline row exists yet.

  Otherwise it rolls back and raises `SafetyStateChanged` (a `SafetyStoreError`); nothing is written.
- `ExecutionSafetyService._evaluate` reads the account once. Then `_decide` reads the state, applies the user action (if any) **to the state as
  read**, decides and commits with `expect=`. On `SafetyStateChanged` it decides again on the new state, at most `MAX_STATE_ATTEMPTS` = 3 times.
  A state that keeps changing is a persistence failure, which the existing handler reports as UNKNOWN (entries blocked).
- `_action` no longer reads the state itself; it passes the requested change into the evaluation. "Already in that state" (no event) is decided
  on the state as read inside the loop.
- `SafetyStore.state()` reads strictly. No database file or no table yet is the default state (a first run). Any other read failure raises
  `SafetyStoreError`, so the evaluation reports UNKNOWN, never "no kill switch".
- **A stop or pause is saved even when its evaluation fails (review M1).** If the evaluation of an Emergency Stop or a Pause raises for any
  reason, `_latch_fallback` saves that latch on its own through `SafetyStore.latch`:
  - the causes covered are an unreadable row, a state that kept changing, or a failed commit;
  - `latch` is one transaction that sets only that latch's own columns, only while it is off, and records its event;
  - it can never clear or overwrite another latch;
  - the evaluation still reports UNKNOWN;
  - a Resume or a Clear is never applied this way.

  `startup()` with HALT_ON_START goes through the same path.
- `current()` serves its cached view only while the persisted kill / pause / daily-halt latches still match it.

**Deviation from the plan's wording.** W1-1 proposed that evaluations never write the latch fields, with a conditional UPDATE and a process
lock. The compare-and-swap above gives the same guarantee (no write can overwrite a newer latch) for threads and processes alike without a
second lock, and keeps the single-transaction write of DESIGN_591 section 9. Sequential behaviour (events, baselines, persistence of no-op
reads) is unchanged and pinned by test S09 and the Stage 5.9.1 / 6.0.x suites.

## 2. The stop-loss check fails closed (W1-3; `execution/service.py`, additive lines only)

**Finding.** The exit check mishandled bad prices in five ways:
- with no price, it reported PASS "no exit condition" and HOLD;
- a days-old price (the audit reproduced a 2023 trade) was read as current;
- a price of 0 or -5 sold the position as a stop breach;
- a NaN from the price function crashed the cycle;
- new entries continued while a held position's stop could not be checked, and a missing price erased the last known price.

**Mechanism.** `stop_price_problem(price, source, price_at, now, simulated)` classifies the exit context's price:

| Problem | Meaning | Breach possible? |
|---|---|---|
| `PRICE_MISSING` | no price | no: the context price is set to None |
| `PRICE_INVALID` | not a positive, finite number | no: the context price is set to None |
| `PRICE_UNDATED` | a real value without a time | yes |
| `PRICE_STALE` | older than the entry side's own limit: `proposals.PRICE_MAX_AGE_SECONDS` (300 s), `MINUTE_BAR_MAX_AGE_SECONDS` (900 s) for a minute close | yes |
| `PRICE_FROM_FUTURE` | stamped more than `STOP_PRICE_FUTURE_TOLERANCE_S` (60 s) after the cycle's clock | yes |

A simulated feed is not aged, exactly as on the entry side (`proposals/engine.py`, `SIMULATED_MODES`). An unreadable stream counts as not
simulated. `exit_decision` (execution/engine.py, unchanged) then runs as before: a stale but real price below the stop still exits
(HARD_INVALIDATION). When it finds no exit and the price has a problem, the position is **not** confirmed safe:
- **Market open, or the broker clock unreadable** (the clock is read at most once per cycle):
  - `EXIT_TRIGGER` is `UNKNOWN` with the reason (re-asserted after the loop, so a later healthy position's PASS never hides it);
  - the decision is `ATTENTION`, and the bot status shows attention;
  - **every new entry is blocked** by one extra `EXIT_TRIGGER` BLOCK control; all other entry controls still evaluate and report;
  - `_recheck_entry` refuses a pending entry for the same reason (defence in depth: the scan already blocks under the same lock);
  - when a fresh, valid price arrives, the next cycle clears it by itself.
- **Market closed** (a readable clock): `EXIT_TRIGGER` is `WARN`, the decision `HOLD`; no escalation.

The position's state is not changed (it is neither HEALTHY nor an exit), and a missing price never overwrites the last known price.

**Deviation from the plan's wording (operator decision).** W1-3 proposed "UNKNOWN, ATTENTION **and a disarm**". Wave 1 blocks entries at the
executor instead of disarming Paper Auto:
- Autorun disarms on a *position* in the ATTENTION state. That state also suspends every automatic exit for the position until a manual review,
  which would turn a short feed outage into a permanently unchecked stop.
- A disarm on a sustained blind stop check would need an autorun policy change, outside this wave's files. It is listed as decision D-W1-a in
  the approval package.

Wave 1 never sells because a price is missing (no automatic flatten).

## 3. No real credential reaches a test (W1-4; `config.py`, `tests/conftest.py`)

**Finding.** `load_dotenv()` with no path searches every parent folder, so a checkout or worktree nested inside `stock-agent\` loaded the real
`.env`, credentials included, into tests and scripts. Two tests (`test_automation_37`, `test_history_budget_39`) asserted against the raw
credential values, which pytest prints on failure.

**Mechanism.**
- `config.py` loads `PROJECT_ROOT / ".env"`: the checkout's own file only. For the production checkout, which has its own `.env`, this is the
  same file as before. It skips the load when `STOCK_AGENT_SKIP_DOTENV=1`, which nothing in production sets.
- `tests/conftest.py`, before any project import:
  - sets `STOCK_AGENT_SKIP_DOTENV=1`, so no test process, and no process that inherits its environment, loads its checkout's `.env`. A
    process started through WMI inherits nothing: the Stage 6.0 runner rig's background server does this, as on main. Tests therefore belong
    outside the production checkout (approval package, remaining risks);
  - removes every credential-shaped variable from the environment.

  python-dotenv itself stays on (review M2). A script given an explicit synthetic env file, such as the Stage 6.0 runner rig's
  `scripts/run_stock_agent.py -EnvFile`, still loads that file.
- The two tests use fake values (set on `config`, or by the shared explanation fixture) and report leaked setting *names* only.
- E03 scans every test file so that no assert (test or message), `fail(...)` / `pytest.fail(...)` or `raise ...Error(...)` reads a credential
  value again.
  - It covers the config settings and the paper broker's variable names (`ALPACA_PAPER_*`, `APCA_*`, `KEY_ENV` / `SECRET_ENV`).
  - It catches direct reads, `os.environ[...]`, `.get` / `os.getenv`, `getattr`, loops, and assigned, annotated, unpacked or copied variables.
  - Taint is tracked per function.
  - E04 is the scan's self-test.
  - Static limits remain (approval package, R11).

## 4. Tests (`tests/test_wave1_safety_remediation.py`, synthetic only)

The before / after logs in the approval package list exactly which tests fail on main `4c01519`. S09, P03, P05 and E04 are regression guards
that pass on main.

- **S01–S21, the safety state:**
  - the account-read window (S01–S05, including 15 real-thread trials);
  - an unreadable row (S06, S17 at the store);
  - the cached view, for a stop and for a pause (S07);
  - contention that never settles (S08);
  - sequential behaviour (S09);
  - the read-to-commit window only the compare-and-swap protects: a stop, Resume vs Stop, Clear vs Pause, the daily halt, a baseline, another
    OS process, and 60 real-thread trials (S10–S16; on main 46 of the 60 trials lost the stop);
  - a stop or pause saved when its evaluation fails, changing nothing else, never rewriting an active latch, and never applying a blind Resume
    or Clear; HALT_ON_START; endless contention (S18–S21).
- **P01–P18, the stop check,** through the real `_price` path (a non-simulated fake feed; `price_fn` only where noted):
  - no price; a stale quote above the stop; a stale bar below the stop still exits;
  - 0 / -5 / NaN / inf, and non-finite through `price_fn`;
  - a fresh price as before;
  - market closed; an unreadable clock; an unreadable stream;
  - entries blocked while blind, and the block clearing by itself;
  - a future stamp; an undated price; the 300 s / 900 s limits;
  - the last price kept; the clock read once per cycle;
  - the control row; the classifier rule by rule.
- **E01–E06, credentials:**
  - no `.env` and no credential in the test process;
  - main's `config.py` loads an outer synthetic `.env` from a nested checkout, this one does not;
  - no assertion reads a credential, and the scan's self-test;
  - a test child skips its checkout's `.env` but loads an explicit synthetic file;
  - conftest's strip block removes every credential variable.
- **F01–F03:** the change set, the semantics and the bookkeeping (section 5).

## 5. Freeze bookkeeping

As every earlier stage: `tests/freeze_ledger.py` declares `BASE_W1` / `NEW_W1` / `EDITED_W1` / `SEMANTIC_W1` / `INSTRUMENTATION_W1`, and every
earlier stage's change-set union admits Wave 1's files. Beyond that:
- main's files that Wave 1 edits are ledger entries: re-hashed, or newly registered (both safety files, `tests/conftest.py`,
  `test_automation_37`, `test_history_budget_39`, each unchanged since BASE_68);
- older freeze tests that read `config.py` or `tests/conftest.py` raw read them through `undo_w1`. That undoes Wave 1's exact replacements, and
  F02 proves the result is main byte for byte;
- Stage 6.0 / 6.0.1 read the safety files through the ledger;
- Stage 5.4.1, 6.10A, 6.10C (F02 and the dynamic-universe I01) and 6.10D admit `SEMANTIC_W1` where they pinned execution / safety files raw, as
  Stage 6.10C already admits Stage 5.4.1's;
- Stage 7.8B-1's and 7.1's F tests read the merged state (`post()`), as 7.2–7.8 already do;
- F03 pins every edit of a file no ledger entry can hold (`REPLAY_W1`).

## 6. What this wave does not do

No threshold, strategy, setup, proposal, entry-control, exposure or sizing change; no autorun policy change; no route, UI, schema or migration
change; no log redaction (audit EP-16, Wave 4); no change to `autorun/facts.py`'s own fallback on an unreadable state; no change to the
operations scripts' env-file resolution; no deployment. Arming Paper Auto still needs Wave 3, a deployment of both waves and explicit approval.
