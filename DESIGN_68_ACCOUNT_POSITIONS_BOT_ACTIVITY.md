# Stage 6.8 — Account position visibility + Bot Activity

Two read-only additions to the product UI:
- the Alpaca PAPER account's positions, as the automatic account refresh already fetched them, each marked as bot-managed or external;
- a Bot Activity panel on TRADE that shows whether the bot worker is actually working.

Nothing here places, changes or cancels an order. Nothing changes the execution mode, a strategy rule, a risk control or the frozen
Stage 6.1 controls (`frozen_hash()` is unchanged).

## 1. The problem

- In MONITOR_ONLY with nothing open, the executor never queries the broker (`needs_broker()` is false). Its "external positions" count
  therefore stayed 0 while the account held F.
- Trader View's Alpaca source was the manual "Refresh Alpaca Paper" view, with an outdated instruction ("Paper Trades › Alpaca Paper ›
  Refresh"). Before any refresh, Trader View showed "NO POSITION", which reads as "the account has none".
- Nothing showed whether the executor loop was alive, what it was doing, or why a ticker was not entered.

## 2. Account positions (`execution/account_positions.py`)

- `classify(broker_positions, store)` marks each position by the same rule the executor's reconciliation (`_reconcile`) uses:
  - **BOT**: an open internal position in the symbol;
  - **BOT_ENTRY**: an open entry intent in the symbol;
  - **NEEDS_REVIEW**: the bot's position in the symbol is CLOSED, yet the account holds shares. This matches the finding
    INTERNAL_FLAT_BROKER_POSITION;
  - **EXTERNAL**: anything else.
- `tests/test_bot_activity_68.py::test_A01` proves the parity against real reconciliation findings.
- `view(refresher, store)` returns the snapshot, classified, in one of three states:
  - **LOADED**: the rows, possibly none;
  - **NOT_LOADED**: no snapshot yet. `positions` is `None`, never `[]`, so "not loaded" is never shown as "no position";
  - **DISABLED**: no paper broker is configured.
- The view carries its source (`ALPACA_PAPER`, `paper-api.alpaca.markets`) and the refresh's freshness. A stale snapshot says so, with its
  read time.
- Zero broker calls: the view reads the account-refresh snapshot and the executor's own tables only.

Where it appears:
- `GET /api/account-refresh/positions` (GET only).
- POSITIONS: an "Alpaca PAPER account" card below the bot's lifecycle cards, with a badge per position. "External · not managed by the bot"
  explains that the bot never trades or exits it, that it counts toward the open-positions limit, and that it blocks a bot entry in the
  same symbol.
- TRADE › Position card: when the bot holds nothing in the active symbol but the account does, the card says "No bot position" and
  "Account holds 1 F · External · not managed by the bot", instead of a bare "No position". When the account positions are not loaded
  yet, it says so.
- Trader View: a new source, `ALPACA_PAPER_ACCOUNT`, replaces the manual view as the Alpaca source whenever the account view is wired
  (`build_stream`) and a paper broker is configured.
  - Its states are POSITION (with management and freshness), NO_POSITION (only when loaded) and NOT_LOADED.
  - The panel says NOT LOADED, not NO POSITION, until some source has actually read the account.
  - Without a paper broker, the manual view remains. Its instruction now names the real path: MORE › Research › Backtests (rule builder,
    legacy lab) › Paper Portfolio › Alpaca Paper — Read Only › Refresh Alpaca Paper.
- Executor status (display only):
  - `external_positions` now comes from whichever read is newer: the executor's own broker reconciliation (`BROKER_RECONCILIATION`) or
    the account refresh (`ACCOUNT_REFRESH`);
  - new fields `external_positions_source`, `external_positions_as_of` and `external_symbols`;
  - the entry controls do not read these. They keep using the broker positions the executor reads itself in each Paper Auto cycle.

## 3. Bot Activity (`execution/activity.py`, `GET /api/execution/activity`, TRADE)

The payload is assembled only from records the executor already writes, plus a small amount of display-only bookkeeping.

**Worker**
- Source: the scheduler's own heartbeat (`heartbeat_at`, stamped at every scheduler decision of the loop thread, due or not), `in_cycle`,
  `cycle_started_at`, `last_finished_at`, `next_at` and the lease result.
- States:
  - RUNNING: a cycle is in progress;
  - IDLE: alive, waiting for the next run;
  - STANDBY: another process holds the lease;
  - STARTING: the thread started; its first decision is due after the startup delay;
  - STALE: no heartbeat within 2 × the expected wake gap + 30 s. The gap is the cycle interval, capped by the 300 s maximum sleep;
  - ERROR: the last cycle failed;
  - STOPPED: no worker thread in this process;
  - DISABLED: `EXECUTION_SCHEDULER_ENABLED=false`.
- The page shows its own connection (the last successful read) as a separate chip. A connected dashboard is never evidence of a working
  worker.

**Mode**
- The current persisted mode, and when it changed, is shown apart from the mode the last cycle ran in.
- When Paper Auto was switched on after the last cycle, the panel says "Paper Auto since HH:MM: awaiting its first cycle (next scheduled
  run HH:MM)". If the worker is not running, it says "no cycle is scheduled". A cycle that has not happened is never shown as run.

**Progress**
- `ExecutionService.progress`, in memory only:
  - the step (orders → reconcile → positions → risk checks → entries);
  - the ticker being evaluated, with its index and total.
- It is `None` between cycles and is never estimated.

**Last cycle**
- This process's last cycle (IDLE included), else the newest persisted run (marked as recorded before the server started).
- `finished_at` is the measured completion (start + elapsed).
- A plain-language summary is built from the cycle's own summary. Cycles without a trade say "No trade.".

**Tickers**
- The newest persisted entry evaluation per ticker:
  - BLOCKED, with each blocking control's own reason;
  - SKIPPED, when the per-cycle entry limit was used;
  - ENTRY_INTENT, with the intent's state, which is set only by the broker's answers.
- Otherwise the setup's own waiting reason.
- A READY ticker in Monitor only says it is not evaluated.
- Every row names its origin:
  - LATEST_CYCLE: an evaluation by the latest cycle, with `evaluated_at` = that cycle's start;
  - EARLIER_CYCLE: an older evaluation, not re-evaluated since, with its own `evaluated_at`;
  - SETUP: the setup state, with `checked_at` = the setup's last evaluation.
- `latest_cycle_evaluations` lists only what the latest cycle itself evaluated.

**Risk checks**
- The safety gate now, and the executor's per-control liveness rows (last result and reason).

**Orders**
- Each intent with its broker-reported order events: Submitted (received), Accepted, Partially filled x/y @ price, Filled, Rejected,
  Canceled or expired.
- These go through the frozen Stage 4.6B status table.
- "Sent to the broker" is our own record and is labelled as such. A fill is shown only when the broker reported it.

**Positions**
- Bot positions, with their latest monitoring or exit decision: HOLD, EXIT_SENT, EXIT_DEFERRED (with the real deferral reason),
  EXIT_IN_PROGRESS or ATTENTION.
- The account's positions, external ones marked unmanaged.

**History**
- Persisted runs, mode changes, order events, position events, findings and safety events, newest first.
- Identical consecutive quiet cycles collapse into one row with a count.
- Quiet cycles still write at most one run row per 300 s (`RUN_ROW_HEARTBEAT_S`, unchanged). Idle Monitor-only cycles write nothing.

**Polling and safety**
- The UI polls every 10 s, or every 2 s while the worker reports a cycle in progress.
- No broker call, no model call, no write.
- No raw broker JSON is passed through, so no credential can reach the payload (`test_B07`).

## 4. Paper Auto ordering (verified, unchanged)

In each Paper Auto cycle, `run_cycle` runs these steps in this order:
1. monitor intents;
2. reconcile with the broker;
3. evaluate positions;
4. evaluate the safety gate;
5. scan entries.

Tests:
- `test_P01`: the broker positions and open orders are read before any entry read (account, clock, asset) or POST, in the first Paper Auto
  cycle. An external F held before enabling is recorded as a non-blocking EXTERNAL_POSITION in that same cycle.
- `test_P02`: an external position in the READY symbol blocks the entry (BROKER_POSITION), and an external position counts toward the
  open-positions cap (MAX_OPEN_POSITIONS).
- `test_P03`: an unreadable broker positions read blocks every entry (BROKER_RECONCILIATION) and nothing is posted.

`enable_auto` reconciles before persisting PAPER_AUTO only when the bot already manages something. With nothing managed, the first Paper Auto
cycle does that reconciliation, before any entry is evaluated, as shown above. This stage does not change that behaviour.

## 5. What changed in frozen files

Executor (`execution/service.py`, `execution/models.py`, `execution/scheduler.py`). Additive, display-only bookkeeping:
- `_broker_positions_at`, `progress`, `_last_touch`;
- the per-ticker `evaluations`, including SKIPPED, and the per-position `decisions` in the persisted run summary;
- `duration_ms` and a measured `finished_at`;
- the three status fields above;
- the scheduler's `heartbeat_at`, `in_cycle`, `cycle_started_at` and `last_finished_at`.

No decision reads any of these. The lease, the scheduler lock, the exactly-once intent, the entry controls and the exit rules are untouched.

Every recording goes through one guard, `ExecutionService._record()`, which logs and swallows any failure:
- the progress step and ticker;
- the per-ticker evaluations, including the read-back of a submitted intent;
- the per-position decisions;
- the last control text.

`test_B09` runs an entry → partial fill → fill → hold → exit → close scenario three times:
- clean;
- with a failure injected at every recording call;
- with real faults inside the recording code (a failing store for control texts, failing step labels).

All three produce identical intents, orders, order events, POST counts, positions, run results and mode.
`tests/test_bot_activity_68.py::test_F02` proves it line by line: every line of main's executor and scheduler is still present, except an
explicit list of edited lines.

Elsewhere:
- Trader View: `trader_view/positions.py` (the new source and the corrected path), `trader_view/stream.py` (wiring) and
  `frontend/trader_view.js` (NOT LOADED).
- Routes: one GET route each in `api/routes/account_refresh.py` and `api/routes/execution.py`.
- `frontend/product_ui.js` and `product_ui.css`: the POSITIONS card and the TRADE panel.

## 6. The freeze ledger (`tests/freeze_ledger.py`)

The earlier freeze tests pin these files to their own base commits. Stage 6.8 does not loosen them:
- Every changed file is registered with the SHA-256 of its exact Stage 6.8 text.
- The freeze tests read a registered file through `view()`, which returns main's version (b2a20ea, where they all passed) after proving
  the hash.
- `test_F01` proves that the change set since main is exactly the new files plus the ledger.
- Any other change, or a later edit without a new hash, still fails.
- The two GET-route allow-lists (Stage 6.1.1 W02, Stage 6.1.2 W01) gained the two new GET routes, so they keep checking the live file.
