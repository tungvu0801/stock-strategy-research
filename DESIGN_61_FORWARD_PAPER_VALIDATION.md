# Stage 6.1 — Forward Paper Validation (DESIGN)

Base: `origin/main` 3904175 (Stage 6.0.3 merged). Branch `stage-61-forward-paper-validation`.

**Instrumentation only. Alpaca PAPER only. Real market setups only. No strategy, threshold, size, exit, mode or order change. No forced signal,
no tuning, no leverage, no short, no live money, no model call, no manual cherry-picking.** Stage 6.0 proved that the bot can execute safely;
Stage 6.1 observes whether it stays **operationally reliable** across many real sessions and — separately — records what the **frozen strategy
actually does** forward. The two questions are never mixed: operational reliability is measured, strategy behaviour is described, and the
strategy numbers stay labelled `INSUFFICIENT FOR STRATEGY CONCLUSION` until the sample is meaningful.

## 1. Objective and the two questions

| Question | Stage 6.1 answers it with | Success bar |
|---|---|---|
| Can the bot run unattended across many real sessions? | one session record per New York trading date, classified HEALTHY / DEGRADED / FAILED / NOT_RUN | 30 sessions observed (operational milestone) |
| Does execution remain operationally correct? | order reliability from the persisted intents / broker orders: duplicates, rejects, cancels, expiries, partials, ambiguous submissions | **duplicate broker orders = 0**, 0 live-host writes |
| Do safety controls fire correctly? | every transition of the persisted gate into a blocking state, classified recovered / persistent; entry-control blocks per session | daily-loss control proven when applicable |
| Are reconciliation and restart paths reliable? | the Stage 6.0 reconciliation findings with a resolution outcome; every detected process restart with state before / after and position continuity | 0 unresolved mismatches, restart recovery proven |
| What does the strategy do forward? | funnel counts and conversion rates, closed paper trades (slippage, P/L, MFE / MAE, exit reason, attribution), drawdown, win / loss, expectancy | descriptive only; no threshold is derived |
| Any early evidence of edge? | a conservative qualitative status: `INSUFFICIENT_DATA` → `EARLY_NEGATIVE` / `MIXED` / `EARLY_POSITIVE` | never `PROFITABLE` / `WINNING`; `INSUFFICIENT_DATA` below 30 closed trades or 30 healthy sessions |

Validation target (section 3 of the stage): 30 regular sessions, preferably 100+ meaningful setup / proposal events (READY transitions +
reviewable proposals). Reaching 30 sessions proves an operational milestone, nothing about profitability; the stage continues when the event
count is too low.

## 2. Frozen strategy controls (no tuning)

`forward_validation.frozen_controls()` reads the Stage 5.8 setup thresholds (WATCH −40 / −30, READY confidence 40, demand 60, coverages 0.60,
exit demand 45, invalidation demand 35 × 2, stretch −20, evidence age 30 d), the Stage 5.9 sizing (risk fraction 0.5 %, position cap 10 %,
TTL 300 s, price-change 0.5 %), the Stage 5.9.1 daily-loss limit, the Stage 6.0 execution rules (10 open positions, 5 sessions, demand exit
35 × 2, stretch exit −20, 15-minute near-close cutoff, 1 entry per cycle, 3 exit cycles, market / day, LONG only, the six exit reasons, regular
session only) and hashes them. `tests/test_forward_validation_61.py::test_Z01` asserts every value and the hash: a change to any control fails
the Stage 6.1 suite. Nothing in this stage reads the P/L to propose a change; any change waits for Stage 6.2+.

## 3. The observer

`forward_validation/service.py::ForwardValidationService.observe()` runs once a minute inside the regular session and every five minutes
outside it (`forward_validation/scheduler.py`, lease `forward_validation_observer`, never in a test / harness / pytest process). One tick:

1. **sample** — mode, executor readiness, the gate's state / allowed / reason codes, reconciliation health and the executor's attention
   reasons, account freshness / age / consecutive failures, equity / cash / buying power of the refreshed snapshot, cumulative external cash
   flow since the validation started, cash-flow freshness / age / failures, open positions (ids, quantities, states, the executor's last
   price), pending entries / exits, the Stage 6.0 control fire counts, the last executor cycle, the executor loop state;
2. **marks** — one price mark per open paper position (the executor's own last price per cycle) → the sampled MFE / MAE;
3. **NOT_RUN back-fill** — weekdays between the last recorded session and today without a row are recorded as `NOT_RUN`
   (`NO_OBSERVATION|HOLIDAYS_NOT_MODELLED`: the project has no exchange holiday calendar, so a holiday reads the same — documented, not hidden);
4. **session record** (section 4); 5. **restart detection** (section 6); 6. **attention + early stop** (sections 9–10).

The observer never raises into its loop: a failure is persisted as `OBSERVER_ERROR` and the control row `observer` carries it.

## 4. Session record and health

`forward_validation_sessions`: `trading_date` (PK), `started_at`, `ended_at`, `bot_start_time` (first observation of the date),
`bot_stop_time` (last observation), `market_open_seen`, `market_close_seen`, `paper_auto_enabled` (ever PAPER_AUTO inside the session),
`account_refresh_healthy`, `cash_flow_healthy`, `reconciliation_healthy`, `session_status`, `notes_code`, `in_session_samples`, `owners`.
The row opens on the first weekday observation, progresses while OPEN and is closed by the first observation after 16:00 ET (or by the next
observation on a later date: `CLOSE_NOT_OBSERVED`). A closed session is never reclassified (trigger).

`metrics.classify_session()` is deterministic (`notes_code` lists every reason):

* **NOT_RUN** — no in-session observation.
* **FAILED** — a duplicate broker order, a non-paper host attempt, an entry recorded while the persisted gate was blocking, the early stop, an
  unresolved RECONCILIATION_REQUIRED intent / ATTENTION position at session end, a FAILED intent, the gate UNKNOWN at session end, or the
  broker unavailable for most in-session observations.
* **DEGRADED** — recoverable issues that recovered (stale account / cash-flow data, reconciliation blocks, UNKNOWN gate readings, executor
  cycle errors, refresh failures, broker errors, a restart during the session) or an incomplete window (open / close not observed, account or
  cash-flow data never fresh).
* **HEALTHY** — the whole window observed, fresh account and cash-flow data throughout, reconciliation healthy, no duplicate, no attention,
  no unresolved execution state, the gate never UNKNOWN.

Session start check (section 31 of the stage): the first in-session sample records the server (the observer runs in it), the mode, the paper
proof (`executor_ready` after the Stage 6.0 startup reconciliation), account / cash-flow freshness, the gate state and reconciliation health;
a session that never meets them reads DEGRADED / FAILED / NOT_RUN by the rules above. Session end check: the post-close observation records
open positions, pending orders, reconciliation, the gate and freshness; nothing is force-closed for reporting.

## 5. Funnel, conversions, order reliability, reconciliation, safety fires, freshness, broker health

Every count comes from a **persisted** row inside the New York day window `[00:00, 24:00) ET` (`ny_window`); nothing is inferred:

| Metric | Source rows |
|---|---|
| universe scanned | distinct symbols in `setup_evaluations` |
| WATCH / READY / INVALIDATED | `setup_transitions.to_state` |
| REVIEWABLE / BLOCKED | `trade_proposals.status` by `created_at` |
| ENTRY_SUBMITTED / EXIT_SUBMITTED | `execution_intents` with `attempt_count ≥ 1` by `submitted_at` |
| ENTRY_FILLED / EXIT_FILLED | `execution_intents.state = FILLED` by `resolved_at` |
| ENTRY_PARTIAL | `execution_order_events.status = partially_filled` (distinct intents) |
| POSITION_OPEN / CLOSED | `paper_positions.opened_at` / `closed_at` |
| EXIT_REQUIRED / ATTENTION | `position_events.kind` |
| rejects / cancels / expired / not sent / failed / ambiguous | `execution_intents` state, `broker_status`, `outcome_class ∈ {O2, O7, O8}` |
| duplicate broker orders | `execution_orders` grouped by `client_order_id` (more than one broker order) + `MULTIPLE_ORDERS` / `UNKNOWN_INTERNAL_ORDER` findings |
| reconciliation | `execution_reconciliations` by kind with a derived **resolution** (RESOLVED when a later `ATTENTION_CLEARED` / `CLOSED` position event or a terminal intent state exists; OPEN otherwise; INFORMATIONAL for `EXTERNAL_POSITION`, `ORDER_ADOPTED`, `ORDER_NOT_FOUND_FINAL`) |
| safety fires | every transition into a non-`TRADING_ALLOWED` state in `execution_safety_evaluations` (RECOVERED / PERSISTENT), `execution_safety_events` by kind (daily-loss halts, emergency stops, pauses, evaluator errors; BASELINE_CREATED / DAILY_RESET / STARTUP are *expected*), entry-control blocks as fire-count deltas of the Stage 6.0 control rows between the first and last sample of the window |
| freshness | max account / cash-flow age of the in-session samples, refresh failures from `account_refresh_attempts` / `cash_flow_refresh_attempts`, max consecutive failures |
| broker health | error codes of the refresh attempts (TIMEOUT, CONNECTION, RATE_LIMITED, HTTP_5xx, UNREADABLE, INVALID_PAYLOAD), executor cycle errors, `BROKER_UNAVAILABLE` findings — broker clock failures and in-cycle read failures are *not separately persisted* by Stage 6.0 and are reported as such, never estimated |

Conversion rates (WATCH → READY, READY → REVIEWABLE, REVIEWABLE → entry, entry → fill, position → normal close, position → attention)
are descriptive percentages with a `None` where the denominator is zero.

## 6. Restart recovery

On its first tick in a process the observer looks for the last sample of another owner. When the executor is ready (or after three ticks) it
records `forward_validation_restarts`: the state before (from that sample: mode, open positions with ids and quantities, pending intents,
attention, reconciliation, gate) and after, whether the broker was queried, the reconciliation result (HEALTHY / BLOCKED reasons /
NOT_QUERIED), the current duplicate count and **position continuity** (`CONTINUOUS` when every position open before is open with the same
quantity or carries a `CLOSED` event; otherwise `CHANGED: …`). Nothing is interrupted on purpose; natural restarts are observed.

## 7. Closed trades: slippage, P/L, MFE / MAE, exit reason, attribution, audit trace

For every `paper_positions` row in `CLOSED`:

* **slippage** — `slippage_absolute = fill − reference` for the buy (entry: the proposal's `reference_price` persisted on the intent) and
  `reference − fill` for the sell (exit: the executor's last price persisted on the exit intent); `slippage_bps = absolute / reference × 10 000`;
  positive = adverse. No expected execution price is invented: a missing reference gives `None`.
* **P/L** — `gross = (exit − entry) × qty`, `return % = (exit − entry) / entry × 100`, `realized_pnl` as the executor stored it. **Fees are not
  attributable per paper order** (Alpaca posts them as account activities; the Stage 6.0.2 ledger has them) → `fees: null`, `net = gross`.
* **MFE / MAE** — from the sampled marks (observer cadence, the executor's own last price), in price, money and %; labelled `sampled marks`.
* **exit reason** — the executor's reason (`HARD_INVALIDATION`, `EVIDENCE_INVALIDATION`, `SETUP_INVALIDATED`, `DEMAND_FAILURE`, `TIME_EXIT`,
  `DISLOCATION_NORMALIZED`) with the beginner text; a distribution is reported.
* **attribution** — setup episode (type, cycle, start, the READY transition's reason and inputs: stretch, confidence, demand, evidence
  strength / cause), the proposal's PPE / Demand / Evidence snapshots, reference price, invalidation level, risk per share, risk budget,
  proposal quantity, equity at proposal, price mode, data mode / source.
* **market context** — session date; `market_trend_proxy` and `volatility_proxy` are `None` with a note: no in-process market trend or
  volatility source exists and none is built (section 19 of the stage).

`trace(position_id)` answers, from persisted rows only: why considered (the NONE → WATCH transition and its inputs), why READY, why this
quantity (equity, risk fraction, risk budget, risk per share, caps, final qty), why the entry was allowed (proposal controls, the persisted
gate evaluation before submission, the paper proof on the intent), what broker order was sent (client / broker ids, side, qty, outcome),
what fill occurred (order events), why the exit was required (the `EXIT_REQUIRED` event with its decision detail), the exit orders, the
position events and the final P/L. Stable ids: trading date, `setup_id`, `proposal_id`, `execution_id`, `position_id`, `client_order_id`.

## 8. Daily summary, cumulative summary, drawdown, expectancy, strategy status

`daily_summary(date)` (persisted at session close, rebuildable on demand) holds the session row, the funnel and rates, order reliability,
reconciliation, safety, freshness, broker health, restarts, the closed trades of the date, realized P/L, unrealized P/L (open positions at the
executor's last price), the day's drawdown and the attention events. `cumulative()` adds sessions completed / healthy / degraded / failed /
not run, setup events vs target, operational totals (duplicates, reconciliation attention and open findings, rejections, partials, ambiguous,
broker errors, refresh failures, safety fires, daily-loss halts, emergency stops, restarts, live-host attempts, early stops) with an
operational status `READY` / `NOT READY`, and the strategy block: win / loss / flat, win rate **always paired with** trade count, average
win, average loss, expectancy and drawdown, gross paper P/L, exit reasons, funnel, conversions.

* **Drawdown** — on *adjusted* equity: each FRESH sample's equity minus the cumulative external cash flow (deposits + journals in −
  withdrawals − journals out, latest revision per broker activity, executed only) since the validation started, so funding never reads as a
  trading gain or loss (the Stage 6.0.2 definitions reused). Peak, trough, current and maximum drawdown in money and %.
* **Expectancy** — `win_rate × average_win − loss_rate × average_loss` per closed trade, labelled "descriptive forward-paper expectancy …
  not a forecast".
* **Strategy status** — `INSUFFICIENT_DATA` below 30 closed trades or 30 healthy sessions (whatever the P/L reads; `2 trades, +8 %` stays
  insufficient); then `EARLY_NEGATIVE` (expectancy < 0), `EARLY_POSITIVE` (expectancy > 0, win rate ≥ 40 %, max drawdown < 10 %) or `MIXED`.
  The words PROFITABLE / WINNING / GUARANTEED / HIGH_CONFIDENCE do not exist in the code.

## 9. Attention

`attention()` lists, with a `critical` flag: `DUPLICATE_BROKER_ORDER`*, `LIVE_HOST_ATTEMPT`* (the Stage 6.0 transport's refusal text "only
the Alpaca PAPER host is allowed" anywhere in the persisted attempts / controls / findings / runs), `SAFETY_GATE_ALLOWED_UNDER_BAD_STATE`*
(an entry attempt recorded while the latest persisted gate evaluation did not allow new entries), `PERSISTENT_RECONCILIATION_MISMATCH`* (an
ATTENTION position unresolved across two closed sessions), `RECONCILIATION_REQUIRED`, `FAILED_EXECUTION`, `UNRESOLVED_QTY_MISMATCH`,
`PERSISTENT_REFRESH_FAILURE`, `PERSISTENT_CASH_FLOW_FAILURE` (* = critical). The UI shows `NEEDS ATTENTION` and never hides it.

## 10. Early stop (the one action)

When a critical condition is observed while the mode is PAPER_AUTO (and `FORWARD_VALIDATION_EARLY_STOP` is on), the observer calls the
Stage 6.0 `disable_auto(actor="forward_validation", reason=…)` **once per condition**: Paper Auto → Monitor only. Existing positions keep
being monitored and their deterministic exits continue (the Stage 6.0 semantics); nothing is liquidated, cancelled or resubmitted. The stop is
an `EARLY_STOP` event, the `early_stop` control fires, the session reads FAILED. Non-critical conditions (transient 429 / timeout / refresh
failures under the existing bounded policies) are recorded and classified, never treated as catastrophic.

## 11. Persistence, immutability, exports

`database/forward_validation_migrations.py` (additive, idempotent, `user_version` untouched): `forward_validation_sessions` (identity
immutable, closed status final, never deleted), `forward_validation_samples`, `forward_validation_marks`, `forward_validation_restarts`,
`forward_validation_events` (append-only: no UPDATE, DELETE or replace), `forward_validation_daily_summary` (derived, rebuildable, never
deleted), `forward_validation_controls` (liveness). Raw evidence is never rewritten; bad trades and failed sessions stay in the data set.
Exports (`/export/{kind}`, CSV or JSON, deterministic order): sessions, trades, daily, control_events, samples, restarts — no credential, no
account identifier, no raw broker description.

## 12. Reports

`reports.daily_report()`: Session health · Signals · Trades · Safety · Execution issues · P/L (descriptive, paper) · Open positions · Notes —
fixed order, plain text, ends with the INSUFFICIENT line and the no-change statement. `reports.weekly_report()` after 5 closed sessions:
health, trade funnel, execution quality, safety, P/L, drawdown, exit reasons, operational status. Both are reproducible from the same rows.

## 13. API, UI, operation

GET only, prefix `/api/forward-validation`: `` (overview), `/sessions`, `/sessions/{date}`, `/closed`, `/closed/{position_id}` (trace),
`/daily`, `/daily/{date}`, `/report/daily[/{date}]`, `/report/weekly`, `/export/{kind}`, `/restarts`, `/events`, `/samples`, `/funnel[/{date}]`,
`/frozen-controls`. **Deviation**: the stage suggested `/trades`; the frozen Stage 2.x isolation scan forbids the word "trade" in any route path
(only `trade-review` exists), so closed paper trades live under `/closed`.

UI: a compact **Forward Paper Validation** panel right after the Paper Bot panel in Trader View (sessions `n / 30`, healthy, closed trades,
duplicates, reconciliation issues, paper P/L, strategy status, the INSUFFICIENT warning, NEEDS ATTENTION; advanced metrics under View details)
and the separate page `forward_validation.html` (operational and strategy blocks kept apart, sessions, closed trades, daily summaries, both
reports, export links). Trader View stays operational; nothing becomes an analytics wall. `index.html` gains two marked, additive Stage 6.1
blocks (stylesheet, script).

Operation (sections 27–30 of the stage): Alpaca PAPER only; Paper Auto deliberately ON during intended validation sessions through the
existing two-click action; regular market hours with the existing near-close cutoff; the existing detached Windows runner (VS Code / Claude
closed, the bot and its refresh / execution / observer loops continue); the PC must stay awake with network — unattended local validation is
not 24/7 and no power setting is changed by this stage. No test fault files, forced clocks, forced setups, forced exits or unadjusted-P&L
acceptance in normal operation; every Stage 6.1 trade originates from real market data, a real READY state, a real proposal and a real
safety evaluation. Paper Auto applies the same deterministic policy to every eligible event — no manual selection of attractive setups.

## 14. Freeze and pins

`tests/test_forward_validation_61.py::test_Z02` pins every file of the earlier stages (packages, routes, migrations, scripts, pinned
frontend files, design documents) to the Stage 6.1 base, allows exactly the additive wiring (`config.py` append-only, `api/server.py` and
`trader_view/stream.py` additive Stage 6.1 lines, `frontend/index.html` outside its two marked blocks) and the marked Stage 6.1 blocks of the
reconciled pin tests (`tests/test_trader_view_561.py`, `tests/test_execution_60.py`, `tests/test_cash_flow_602.py` ×2,
`tests/test_ci_groups_603.py`) plus one widened regex in `tests/test_account_refresh_601.py` (asserted as an exact line replacement). Any
other change to a prior-stage file fails the suite. CI: the new group `validation` → job **Pytest Forward Validation**.

## 15. Handoff to Stage 6.2

Stage 6.1 output (sessions, trades with attribution and slippage, daily summaries, control events, exports) feeds Stage 6.2 Opportunity
Ranking and later portfolio allocation, regime detection and multiple strategies. None of those is built here; no threshold moves on these
numbers.

## 16. Non-goals and limitations

No exchange holiday calendar (holidays read NOT_RUN); MFE / MAE are sampled at the observer cadence, not tick data; per-trade fees are not
attributable; no market trend / volatility proxy exists in-process; broker clock failures and in-cycle read failures are not separately
persisted by Stage 6.0; the observer is a reader — it does not make the bot run (the Stage 6.0 / 6.0.1 loops do).
