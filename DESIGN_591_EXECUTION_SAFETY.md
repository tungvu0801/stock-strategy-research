# Stage 5.9.1 — Real Execution Safety Controls (DESIGN)

Base: `origin/main` bd57e45 (Stage 5.9 merged). Branch `stage-591-execution-safety`.

Stage 5.9 found that the project had **no operational kill switch and no daily-loss halt** (`MAX_DAILY_LOSS_PERCENT` and
`MAX_OPEN_POSITIONS` were informational; the Stage 5.9 proposal controls read review-only config flags). Stage 5.9.1 implements a
small, deterministic, **persisted** execution-safety layer and proves it with forced-fire, restart and day-boundary tests. It still
submits nothing: no Alpaca order, no Robinhood write, no auto trading. It is the prerequisite gate for Stage 6.0.

## 1. Safety-state model

| state | meaning | beginner wording |
|---|---|---|
| `TRADING_ALLOWED` | proposal generation allowed; a future execution layer may proceed if every other control passes | 🟢 Trading allowed |
| `NO_NEW_ENTRIES` | monitoring continues (PPE / Demand / Setup / proposals stay visible); no new entry may proceed; exits stay possible in Stage 6.0 | 🟡 New trades paused |
| `HALTED` | no new entry; the future execution layer must treat this as a hard stop; cleared only by the documented policy | 🔴 Trading stopped |
| `UNKNOWN` | the evaluator or its persistence failed (§10); treated exactly like HALTED by any caller | 🔴 Trading stopped (safety status unavailable) |

No state is named after a side. `allowed_new_entry` is a separate boolean: it is true only when the state is `TRADING_ALLOWED` **and**
the daily-loss control is actually verifiable (§5, §8). A proposal may be REVIEWABLE while `allowed_new_entry` is false.

## 2. Precedence (exact)

```
MANUAL KILL SWITCH active                      -> HALTED            reason KILL_SWITCH_ACTIVE
else DAILY LOSS latch set for today            -> HALTED            reason DAILY_LOSS_LIMIT_REACHED
else NO NEW ENTRIES active                     -> NO_NEW_ENTRIES    reason NO_NEW_ENTRIES_ACTIVE
else                                           -> TRADING_ALLOWED
evaluator / persistence failure                -> UNKNOWN           reason SAFETY_EVALUATOR_ERROR | PERSISTENCE_FAILED
```
Reason codes are cumulative (a kill switch plus a daily halt lists both); the state follows the highest-precedence active control.

## 3. Global kill switch (manual)

Persisted in `execution_safety_state` (`kill_active`, `kill_activated_at`, `kill_activated_by`, `kill_reason`, `updated_at`).
Actions: **ACTIVATE** (`POST /api/execution-safety/emergency-stop`, optional reason) and **CLEAR** (`POST
/api/execution-safety/clear-emergency-stop`): they change safety state only — no order, no liquidation, no broker call. Activation and
clearing survive restarts (the store is the state). Config never activates it; `EXECUTION_SAFETY_STARTUP_POLICY` (default `KEEP`) only
says what happens at startup: `KEEP` leaves the persisted state untouched, `HALT_ON_START` activates the kill switch once at startup
with reason `STARTUP_POLICY` (never clears one). No environment variable is a source of truth at runtime.

## 4. No-new-entries mode (manual pause)

Persisted (`pause_active`, `pause_activated_at`, `pause_activated_by`, `pause_reason`). **Pause New Trades** sets it;
**Resume** clears it and nothing else (a Resume never clears a kill switch; clearing an Emergency Stop is its own explicit action).
While active, monitoring and proposal generation continue; `allowed_new_entry` is false; the proposal control `NO_NEW_ENTRIES`
records WARN (the proposal stays visible) and the proposal's `execution_eligibility` says blocked.

## 5. Daily loss (a real control, not a flag)

* **Account data** (§7): the Stage 5.9 read-only `AccountAdapter` snapshot — Alpaca Paper 4.6A view (zero network calls, refreshed by
  the user), the Stage 4.5 local paper portfolio, or the explicit hypothetical review equity in simulated mode. Nothing is invented;
  no live broker call is added.
* **Trading date** = the America/New_York calendar date of the evaluation clock.
* **Baseline** (`daily_risk_baselines`, one row per trading date): created by the evaluator the first time a usable, fresh equity is
  observed on that date (`starting_equity`, `baseline_created_at`, `source`, `adjustments`, `status`, `equity_as_of`), recorded as the
  event `BASELINE_CREATED`. It is never derived on the fly: without a persisted baseline the control is `DAILY_BASELINE_MISSING`
  (status UNAVAILABLE) and `allowed_new_entry` is false. `starting_equity = current_equity` as a silent fallback does not exist (§13).
* **Formula** (Decimal, cents): `daily_pnl = adjusted_current_equity − starting_equity`;
  `daily_loss_pct = max(0, (starting_equity − adjusted_current_equity) / starting_equity × 100)` (2 dp).
  `starting_equity ≤ 0`, a non-finite value, or a missing current equity → status UNAVAILABLE (`INVALID_EQUITY` / `EQUITY_NOT_AVAILABLE`).
* **Threshold**: `config.MAX_DAILY_LOSS_PERCENT` (3.0) becomes enforced. It must be > 0 (`THRESHOLD_INVALID` otherwise → UNAVAILABLE).
  `daily_loss_pct ≥ threshold` latches the daily halt (`daily_halt_active`, `daily_halt_trading_date`, `daily_halt_triggered_at`,
  `daily_halt_loss_pct`, reason `DAILY_LOSS_LIMIT_REACHED`; event `DAILY_HALT_TRIGGERED`). Never tuned from historical P&L.
* **Adjustments (deposits / withdrawals)**: the local paper portfolio and the hypothetical review account have no cash-flow model, so
  deposits and withdrawals are structurally impossible there (`adjustments = NONE_POSSIBLE`, reliable). The Alpaca Paper 4.6A snapshot
  carries no transfer activity, so a transfer cannot be told from trading P&L: `adjustments = UNAVAILABLE`,
  status `DAILY_PNL_ADJUSTMENT_UNAVAILABLE`. In that case the raw loss is still compared with the threshold (a withdrawal that looks
  like a loss halts — the conservative error) but a non-halting day is **not** reported healthy: `allowed_new_entry` stays false.
  The explicit, default-off `EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL` lets an operator accept the raw equity difference for a paper
  account with no transfers; the choice is recorded in every evaluation input. No cash-flow reconciliation is implemented (§15).
* **Stale snapshot**: an Alpaca snapshot older than `EXECUTION_SAFETY_ACCOUNT_MAX_AGE_SECONDS` (900) → `ACCOUNT_SNAPSHOT_STALE`
  (UNAVAILABLE, blocked readiness; the latch is untouched). Local paper equity is marked at the latest completed close (fresh by
  definition); hypothetical equity is a constant (fresh by definition, labelled).

## 6. Latch and reset

A triggered daily halt stays latched for its trading date even when equity recovers. Reset happens only when the trading date
changes: the evaluator clears the daily latch (event `DAILY_RESET`, reason `NEW_TRADING_DAY`) and creates the new day's baseline when a
usable equity is available. The manual kill switch and the pause are **never** cleared by a day change. No user reset of the daily
latch exists in V1 (policy: a loss day stays stopped).

## 7. Control evaluation path

`ExecutionSafetyService.evaluate(trigger, now=None, persist=True, overrides=None) -> SafetyView` is the one function Stage 6.0 calls
(`safety.service.evaluate_execution_safety(...)` is the module-level entry). Steps: load state → day rollover → account snapshot →
baseline (create if missing and usable) → daily-loss computation → latch → precedence → persist (state upsert, events for changes
only, an evaluation row, control liveness) → listeners. Triggers: `API` (reads), `ACTION` (user buttons), `PROPOSAL` (every proposal
evaluation consumes it), `SCHEDULED` (the Trader View in-session cadence), `STARTUP`, `TEST`. Writes are bounded: unchanged results
re-persist an evaluation row at most every `EXECUTION_SAFETY_EVALUATION_HEARTBEAT_SECONDS` (60); control liveness updates every time.

## 8. Safety gate contract

```
{ state, allowed_new_entry, halted, reason_codes[], daily_loss{status, trading_date, starting_equity, current_equity, daily_pnl,
  daily_loss_pct, threshold_pct, adjustments, baseline_created_at, equity_source, equity_as_of, latched, triggered_at},
  kill_switch{active, activated_at, activated_by, reason}, no_new_entries{active, activated_at, activated_by, reason},
  freshness{account_fresh, account_age_s, evaluated_within_s}, evaluated_at, trigger, persisted, error }
```
`halted` is true for HALTED and UNKNOWN. `allowed_new_entry` is true only for TRADING_ALLOWED with daily-loss status OK.

## 9. Persistence

* `execution_safety_state` — single row (`id = 1`, upserted): the kill switch, pause and daily-latch fields above + `updated_at`.
* `daily_risk_baselines` — one row per `trading_date` (append-only, no delete, no replace, identity fixed).
* `execution_safety_events` — append-only: `event_id, at, kind, actor, reason, trading_date, from_state, to_state, input_json`.
  Kinds: `PAUSE_ON, PAUSE_OFF, KILL_ON, KILL_OFF, BASELINE_CREATED, DAILY_HALT_TRIGGERED, DAILY_RESET, EVALUATOR_ERROR, STARTUP`.
  An unchanged evaluation writes no event (no duplicates on hold or on restart).
* `execution_safety_evaluations` — append-only: `evaluation_id, evaluated_at, trigger, state, allowed_new_entry, reason_codes_json,
  daily_loss_json, kill_json, pause_json, freshness_json, input_json, error`.
* `execution_safety_controls` — liveness rows (upserted, no delete): `control_id, last_evaluated_at, last_fired_at, evaluation_count,
  fire_count, last_result, last_reason, last_input_json, last_error, updated_at` for `GLOBAL_KILL_SWITCH`, `DAILY_LOSS_HALT`,
  `NO_NEW_ENTRIES`, `DAILY_BASELINE`. A control "fires" in an evaluation when its result is BLOCK (`fire_count` counts those
  evaluations; `last_fired_at` is the latest). "When was it last evaluated / has it ever blocked / is the baseline populated" are
  answered from these rows and the events, never from logs.
* State upsert, events, the evaluation row and the liveness rows are written in ONE transaction.

## 10. Failure behaviour

* Persistence failure → the evaluation returns `UNKNOWN` with `PERSISTENCE_FAILED`, `allowed_new_entry = false`, `halted = true`;
  the error text is in `error` and in the kill-switch / daily-loss liveness `last_error` when they can still be written.
* Evaluator exception → `UNKNOWN` with `SAFETY_EVALUATOR_ERROR` (same flags); the error is recorded as an `EVALUATOR_ERROR` event
  when the store is reachable.
* Stale account snapshot, missing baseline, invalid equity, invalid threshold, unadjustable P&L → daily-loss status UNAVAILABLE with the
  specific code; the state follows the other controls, `allowed_new_entry` is false.

## 11. API (safety state only; no order path)

`GET /api/execution-safety` (evaluates with trigger `API`), `GET /api/execution-safety/events`, `GET /api/execution-safety/controls`,
`POST /api/execution-safety/pause-new-entries`, `POST /api/execution-safety/resume-new-entries`,
`POST /api/execution-safety/emergency-stop`, `POST /api/execution-safety/clear-emergency-stop` (optional `{reason}`; actor `user`).

## 12. Stage 5.9 integration

The proposal controls `KILL_SWITCH` and `DAILY_LOSS_HALT` consume the safety gate (the persisted state), no longer the review-only
booleans (`TRADE_PROPOSAL_HALT` / `TRADE_PROPOSAL_DAILY_LOSS_HALT` remain honoured as additional review-only overrides, documented as
such). A new proposal control `NO_NEW_ENTRIES` records WARN while paused (the proposal stays REVIEWABLE). Every proposal view carries
`execution_eligibility {allowed, state, reason_codes, evaluated_at}` derived at read time from the live gate, so a stored proposal
reads "blocked" the moment a pause or halt is active. Proposal history is never deleted. The `AccountSnapshot` keeps its `halt` /
`daily_loss_halt` fields (filled from the gate) so the Stage 5.9 test overrides keep working.

## 13. Forced-fire and dead-control test plan

A. activate the kill switch → HALTED → proposal control blocks → `KILL_ON` event → restart → still HALTED.
B. baseline 10 000, equity 9 400, threshold 5 % → loss 6 % → HALTED → `DAILY_HALT_TRIGGERED` event → `fire_count` increments.
C. pause → NO_NEW_ENTRIES → proposal REVIEWABLE and visible → `execution_eligibility.allowed` false.
D. normal → TRADING_ALLOWED, `allowed_new_entry` true (simulated hypothetical account: adjustments NONE_POSSIBLE).
Dead control: with no baseline and a baseline write that fails, the evaluation reports `DAILY_BASELINE_MISSING`, never a zero loss; a
baseline from earlier in the day is used as persisted, never re-derived from the current equity.
Restart (Phase 21) and day boundary (Phase 22) run on a fake clock against the same database file.

## 14. UI wording (beginner first)

Trader View, right column, first panel **RISK CONTROLS**: 🟢 Trading allowed · 🟡 New trades paused · 🔴 Trading stopped; one
reason line ("Daily loss limit reached", "Emergency stop active", "New trades paused by you", "Safety status unavailable"); buttons
**Pause New Trades**, **Resume**, **Emergency Stop**, and — only while an emergency stop is active — **Clear Emergency Stop**, which
needs a second click to confirm. **View details** reveals: daily loss, threshold, starting equity, current equity, kill switch state,
last evaluated, control history. Internal enums are never the headline. No BUY / SELL wording.

## 15. Relationship to Stage 6.0 and non-goals

Stage 6.0 must call `evaluate_execution_safety` immediately before any entry and treat `allowed_new_entry = false` as a hard stop;
how HALTED interacts with exits of open positions is a Stage 6.0 decision. Non-goals here: order submission, liquidation, broker
calls, Alpaca transfer / activity reads, cash-flow reconciliation, Robinhood execution, an LLM anywhere, a holiday calendar.
