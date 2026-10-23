# Stage 6.0 — Autonomous Alpaca PAPER Trading (DESIGN)

Base: `origin/main` a58297a (Stage 5.9.1 merged). Branch `stage-60-autonomous-paper`.

**ALPACA PAPER ONLY. No real money. No Robinhood write. No LLM in the execution path.** Stage 6.0 is the first autonomous loop:
WATCH → READY setup → deterministic proposal → execution safety gate → ONE paper entry → order monitoring → fill reconciliation →
internal position → deterministic exit conditions → ONE paper exit → reconciliation → closed trade → keep monitoring.
Market / broker data are state, Python rules decide, the LLM explains only, Stage 5.9.1 safety is a hard constraint, the user controls
the mode. Stage 6.0 proves **operational execution correctness**, not trading edge (Stage 6.1 measures that forward).

## 0. Audit outcome (what is reused, what stays isolated, what is new)

| Component | Finding | Stage 6.0 |
|---|---|---|
| 4.6A read-only view (`paper/alpaca_view`, `alpaca_readonly`) | in-memory snapshot, manual refresh by contract, `TradingClient` only there | not called by the executor; the Stage 5.9 / 5.9.1 account adapters can read it in MONITOR_ONLY |
| 4.6B writer (`paper/alpaca_order_writer`) | frozen; only `paper/alpaca_orders.py` may import it; accepts only the `sa46b-` manual payload inside its own scope | **cannot be reused** → Stage 6.0 has its own paper-host-pinned client (`execution/broker_alpaca.py`), same requests-transport pattern, single POST, outcome classification |
| 4.6B pure rules (`paper/alpaca_order_rules`) | pure, importable | reused: `parse_order`, `consistent`-style checks, `classify_post`, `map_status` / `STATUS_MAP`, `DUPLICATE_MARKERS`, limits |
| 4.6B reads / store (`alpaca_order_reads`, `alpaca_order_store`) | GET allow-list pinned to the `sa46b-` prefix, no list reads, no average price | not used for execution; the 4.6B intent store is read for the duplicate-order control (as Stage 5.9 does) |
| Stage 5.8 setups, 5.9 proposals, 5.9.1 safety | pinned packages / services with injectable seams | consumed read-only: `setups.current()`, `proposals.current()`, `safety.evaluate()`; entry uses the proposal quantity exactly |
| Scheduler / lease | `setups/scheduler.py` pattern, `forward.automation` lease (`app_leases`) | new `execution/scheduler.py`, lease name `paper_executor`, one loop per app / database |
| `MAX_OPEN_POSITIONS` | informational (10) | **enforced** as the open-position cap |
| Trader View | `TerminalState` key set, socket burst / refresh order, listener lists and `index.html` lines are pinned | a `bot` + per-symbol `execution` message fanned out by the executor's own listener; `frontend/paper_bot.js` polls the read-only API; the `index.html` pin gains one script line (additive, reported) |
| Route words | `paper`, `execute`, `trade`, `order` (non-GET) banned by frozen scans | `/api/execution/...` with `auto/enable` and `auto/disable` instead of the suggested `paper-auto/...` (reported deviation) |
| Test lifespans | pytest runs the real lifespan with fake paper credentials and asserts zero broker requests | the executor performs **no broker call at startup unless Stage 6.0 state exists or PAPER_AUTO is persisted**, and never starts its thread in a test / harness process |

Missing pieces built here: a paper broker client + a deterministic simulated broker, execution intents / orders / positions /
reconciliations / controls persistence, the exit engine, startup reconciliation, the executor loop, the API, the beginner UI.

## 1. Execution modes (Step 4)

`MONITOR_ONLY` (default) and `PAPER_AUTO`, persisted in `execution_mode` (single row: mode, changed_at, changed_by, reason,
environment_proof_json). `LIVE_CONFIRM` / `AUTO_LIVE` do not exist. Enabling PAPER_AUTO validates: paper environment proven (§2),
broker account readable, reconciliation healthy, safety evaluator healthy (state not UNKNOWN). Disabling stops new entries only;
monitoring, reconciliation and exits of existing paper positions continue. After a restart the persisted mode is restored, but nothing is
submitted before startup reconciliation completes (§9).

## 2. PAPER-only hard boundary (Step 6, 42, 45)

`execution/broker_alpaca.py::AlpacaPaperBroker` builds every URL from the constant host `paper-api.alpaca.markets` and refuses any
other scheme / host / port at the transport (the Stage 4.6B pattern); credentials are the `ALPACA_PAPER_*` pair; there is no base-URL
setting anywhere. `environment()` performs `GET /v2/account` and returns `{paper: True only when host == PAPER_HOST and the account read
succeeded, host, account_number_masked, status, blocked flags}`. PAPER_AUTO cannot be enabled unless `paper is True`; missing
credentials, a failed account read or an unknown host fail closed. The simulated broker reports `environment.paper = True,
host = "simulated"`, and is selected only by `EXECUTION_BROKER=simulated` (never by default in a real-data process). Tests prove the live
host string is never built and that enable refuses when the proof is absent.

## 3. Execution state machine (Step 7) and beginner mapping

Internal execution states: `IDLE, ENTRY_PENDING, ENTRY_SUBMITTED, ENTRY_PARTIAL, POSITION_OPEN, EXIT_REQUIRED, EXIT_PENDING,
EXIT_SUBMITTED, EXIT_PARTIAL, CLOSED, FAILED, RECONCILIATION_REQUIRED`.

| internal | beginner headline |
|---|---|
| no execution, setup WATCH / NONE | 🟡 Watching |
| no execution, setup READY with a reviewable proposal | 🔵 Ready |
| ENTRY_PENDING / ENTRY_SUBMITTED / ENTRY_PARTIAL | 🟠 Entering — "Waiting for paper order fill" |
| POSITION_OPEN | 🟢 In trade — shares, entry, current, P/L, reason |
| EXIT_REQUIRED / EXIT_PENDING / EXIT_SUBMITTED / EXIT_PARTIAL | 🟠 Exiting — reason |
| CLOSED | ⚪ Closed — "Trade completed" |
| safety HALTED / UNKNOWN with no position | 🔴 Stopped |
| FAILED / RECONCILIATION_REQUIRED | 🔴 Needs attention |

The BOT headline: ⚪ Monitor only · 🟢 Paper Auto · 🔴 Needs attention. Internal enums, ids, payloads and reconciliation detail live
under **View Details** only.

## 4. Entry (Steps 5, 8, 9, 10, 11, 12)

**Trigger**: `DOWNSIDE_DISLOCATION_RECOVERY` setup in READY + a Stage 5.9 proposal with `effective_status == REVIEWABLE` and
`execution_eligibility.allowed == true` + the Stage 5.9.1 gate `TRADING_ALLOWED`. The proposal is the single source of symbol, side
(LONG only), quantity (`final_qty`, used exactly; never recomputed), reference price, invalidation price, risk budget, setup_id,
proposal_id.

**Preconditions** (each a persisted control, all must PASS): `EXECUTION_MODE` (PAPER_AUTO), `PAPER_ENVIRONMENT`, `SAFETY_GATE`
(TRADING_ALLOWED), `PROPOSAL_VALIDITY` (REVIEWABLE, eligible, not stale, not expired, setup READY, quantity ≥ 1, side LONG,
reference fresh), `SESSION_POLICY` (regular session by the clock helper AND the broker clock when available, not within the final 15
minutes, no `MARKET_NEAR_CLOSE` warning), `BROKER_TRADABILITY` (asset tradable, active, us_equity), `DUPLICATE_ENTRY` (no
non-terminal intent for the symbol, no prior entry for this setup episode, no open internal position, no live broker order for the
symbol, no 4.6B unresolved intent), `BROKER_POSITION` (no existing broker position in the symbol), `BROKER_ACCOUNT` (readable, ACTIVE,
not blocked, buying power ≥ quantity × reference × 1.05), `MAX_OPEN_POSITIONS` (broker positions + pending entries <
`config.MAX_OPEN_POSITIONS`), `BROKER_RECONCILIATION` (healthy, nothing RECONCILIATION_REQUIRED). Any failure → no submission, the
control row says why.

**Exactly-once intent**: `execution_id = sha256("entry|" + proposal_id)[:32]` (one entry per proposal, PK); the row
(`execution_intents`) is persisted with state `ENTRY_PENDING` **before** any broker call; `client_order_id =
"stockagent-entry-<execution_id>"`. A crash after the intent and before the POST leaves `ENTRY_PENDING`: on restart the executor looks
the client_order_id up at the broker before deciding (§9).

**Submission** (one function, `submit_entry`): re-check safety, proposal, account, duplicates and the paper proof against the persisted
intent → **exactly one POST** (`market`, `day`, whole shares, `client_order_id`) → persist the broker order id immediately
(`ENTRY_SUBMITTED`) → first status mapped from the response. Outcomes: a parsed, consistent 2xx → SUBMITTED; a definitive reject
(4xx with a definitive category) → `FAILED` (no replacement order); a duplicate-id answer or an **ambiguous transport result** (timeout,
connection drop after sending, unreadable 2xx) → `RECONCILIATION_REQUIRED`, never an automatic re-POST. The next cycles query the
order by client_order_id; found → adopt it; not found after two lookups at least 30 s apart → `FAILED (NOT_SENT)`; the proposal id is
burnt (a later proposal / setup cycle may create a new execution). Order type: market, day, regular session only; no brackets,
trailing stops, OCO / OTO, extended hours.

## 5. Order lifecycle, partial fills, rejects (Steps 13–15)

Broker statuses NEW / ACCEPTED / PARTIALLY_FILLED / FILLED / CANCELED / EXPIRED / REJECTED (plus held, pending_*, replaced) map through
the Stage 4.6B `map_status`. Every status change (status, filled_qty, filled_avg_price, raw snapshot) is appended to
`execution_order_events`; the current view is upserted in `execution_orders`. Partial fills: the internal position is created from the
first broker-confirmed fill with `quantity = filled_qty` and `average_entry_price = filled_avg_price`; later fills update it from
broker evidence; `requested_qty`, `filled_qty`, `remaining_qty` are kept on the intent. A rejected entry → `FAILED`
(`BROKER_REJECTED`); a canceled / expired entry with zero fill → `FAILED` (`CANCELED`); with a partial fill → the position stays with the
filled quantity and the entry intent ends `PARTIALLY_FILLED_EXPIRED`. No automatic replacement or resubmission.

## 6. Position lifecycle (Steps 16, 17)

A position (`paper_positions`) exists only from broker-confirmed fills: `position_id, execution_id, proposal_id, setup_id, symbol,
side, quantity, average_entry_price, opened_at, invalidation_price, risk_budget, source (broker fills), broker,
broker_position_reference, state, demand_low_streak, sessions_held, exit_reason, exit_cycle, closed_at, realized_pnl`.
States: `OPEN → HEALTHY | WEAKENING → EXIT_REQUIRED → EXIT_SUBMITTED → CLOSED` (+ `ATTENTION`). WEAKENING is informational
(demand < 45 or stretch > −30). Every change is appended to `position_events`.

## 7. Exit engine (Steps 18–22) — deterministic, no LLM

Inputs per cycle: the latest price (quote `price`, else last completed candle close), the Stage 5.8 setup view and its stored inputs,
the open episode, sessions held. Reasons and **precedence**:

1. `HARD_INVALIDATION` — price ≤ proposal `invalidation_price`
2. `EVIDENCE_INVALIDATION` — the setup is INVALIDATED with `EVIDENCE_CONTRADICTION`, or `setups.engine.evidence_contradiction`
   holds for the episode and the current inputs (a new STRONG company-specific packet after the episode started)
3. `SETUP_INVALIDATED` — the setup is INVALIDATED for any other reason (or the episode closed to NONE)
4. `DEMAND_FAILURE` — Demand < 35 on two consecutive eligible evaluations (the Stage 5.8 `INVALIDATE_DEMAND` / `_EVALS` convention)
5. `TIME_EXIT` — held for ≥ `EXECUTION_MAX_HOLD_SESSIONS` (5) regular sessions
6. `DISLOCATION_NORMALIZED` — PPE Stretch > −20 (the Stage 5.8 `INVALIDATE_STRETCH` convention)

No profit target, no predicted price. Thresholds are engineering V1 defaults in config, never tuned from returns; they coincide with the
documented Stage 5.8 conventions (35 × 2, −20).

**Safety semantics**: NO_NEW_ENTRIES, the daily-loss halt and the Emergency Stop **block new entries only**. Existing paper positions
keep being monitored and deterministic risk-reducing exits stay permitted. Emergency Stop never liquidates. Exits are submitted in the
regular session only (market orders); outside it the exit stays `EXIT_REQUIRED` and is submitted at the next open.

**Exactly-once exit**: a separate exit intent (`intent_type = EXIT`, `exit_cycle` per position, id `sha256("exit|position_id|cycle")`,
`client_order_id = "stockagent-exit-<position_id>-<cycle>"`) persisted before the POST; at most one non-terminal exit intent per
position; repeated ticks re-use it. A definitively rejected / canceled exit returns the position to `EXIT_REQUIRED` and the next
cycle opens the next `exit_cycle`, bounded by `EXECUTION_MAX_EXIT_CYCLES` (3); beyond that the position is `ATTENTION`. An ambiguous
exit POST → `RECONCILIATION_REQUIRED` and the client_order_id lookup rule above.

## 8. Reconciliation (Step 23) and startup (Step 24)

Each cycle (and at startup when Stage 6.0 state exists or PAPER_AUTO is persisted) compares internal intents / positions with broker
orders (listed by status + by client_order_id) and broker positions. Detected: internal order exists / broker order missing; broker
order with a `stockagent-` id but no internal intent; internal position qty ≠ broker qty; internal flat / broker position exists (for a
symbol Stage 6.0 traded); internal open / broker flat; multiple unexpected broker orders in a symbol; unknown broker status. Each finding
is appended to `execution_reconciliations`; blocking findings set `RECONCILIATION_REQUIRED` (new entries blocked, the control row says
why) and are **never silently fixed**: the only automatic adoptions are (a) a broker order found by our own client_order_id for an
intent without a broker id, and (b) broker-confirmed fill quantities. Pre-existing broker positions in symbols Stage 6.0 never traded
are listed as `EXTERNAL_POSITION` (informational; they block entries in that symbol and count toward the open-position cap).
Startup order: load mode → load open intents / positions → query the broker → reconcile orders → reconcile positions → evaluate
safety → only then mark the loop ready. No broker write before this completes.

## 9. Scheduler (Steps 26, 27)

`execution/scheduler.py`: one daemon thread per process under the `paper_executor` lease (`forward.automation` app_leases, TTL 2 min),
cadence `EXECUTION_CYCLE_SECONDS` (30 s in session, 300 s outside). Each cycle: lease → reconcile pending intents → refresh account /
broker state → evaluate open positions first → submit required exits → evaluate safety → if PAPER_AUTO and allowed, scan READY
setups with reviewable proposals → submit at most `EXECUTION_MAX_ENTRIES_PER_CYCLE` (1) new entry → persist the run and liveness.
Started from the app lifespan only when `EXECUTION_SCHEDULER_ENABLED`, not in `test` / `browser_test` processes, not under pytest, and
only when a broker is configured. `MAX_OPEN_POSITIONS` (10) is enforced: broker positions + pending entries must stay below it.

## 10. Persistence (Step 35) and liveness (Step 36)

Tables (`database/execution_migrations.py`, additive, idempotent): `execution_mode` (single row), `execution_runs` (append),
`execution_intents` (PK execution_id; identity immutable, only state / broker_order_id / filled fields / attempt_count / updated_at
change; no delete), `execution_orders` (current broker view per order, upsert), `execution_order_events` (append), `paper_positions`
(identity immutable; lifecycle fields change; no delete), `position_events` (append), `execution_reconciliations` (append),
`execution_controls` (upsert liveness). Controls: `PAPER_ENVIRONMENT, EXECUTION_MODE, SAFETY_GATE, PROPOSAL_VALIDITY, SESSION_POLICY,
DUPLICATE_ENTRY, MAX_OPEN_POSITIONS, BROKER_ACCOUNT, BROKER_TRADABILITY, BROKER_POSITION, BROKER_RECONCILIATION, ENTRY_SUBMISSION,
POSITION_RECONCILIATION, EXIT_TRIGGER, EXIT_SUBMISSION` with `last_evaluated_at, last_fired_at, evaluation_count, fire_count,
last_result, last_reason, last_input, last_error`.

## 11. API (Step 30) and UI (Steps 31–34)

`GET /api/execution` (overview: mode, environment, bot headline, open positions, pending intents, reconciliation, controls),
`GET /api/execution/status` (compact bot + per-symbol cards), `GET /api/execution/history`, `GET /api/execution/intents`,
`GET /api/execution/positions`, `GET /api/execution/controls`, `GET /api/execution/{symbol}`; `POST /api/execution/auto/enable`,
`POST /api/execution/auto/disable` (mode only). UI: a compact **BOT** panel under Risk Controls (headline, Enable Paper Auto / Stop New
Trades, View details with recent paper trades), and a **Paper trade** card under Trade Review per symbol (headline, shares, entry,
current, P/L, reason, View Details). Emergency Stop stays in Risk Controls. No autonomous-trading dashboard.

## 12. Failure modes and tests (Steps 25, 37–42, 46)

Crash points injectable in tests (`SimulatedCrash`): after intent / before POST; after POST / before broker id persisted; after a
partial fill; after a full fill / before the position write; after an exit intent / before the exit POST; after the exit POST / before
status persisted. Each restart must query the broker, reconcile and continue with no duplicate order. Broker failure script: timeout
before POST, ambiguous timeout after POST, 429, 500, rejected, canceled, partial fill, late fill, position mismatch, order present after a
local crash. Forced safety: Emergency Stop, daily-loss HALTED, NO_NEW_ENTRIES, safety UNKNOWN, reconciliation unhealthy → no entry POST;
a halt while a position is open still allows the deterministic exit. Live-endpoint prohibition, no-Robinhood, no-LLM proofs.

## 13. Stage 6.1 forward validation (Step 51)

Stage 6.1 will inspect, descriptively: duplicate-order rate, reconciliation findings, entry rejects, partial fills, slippage (fill vs
reference), control fires, unexpected state transitions, restart recoveries, setup → proposal → execution conversion, exit reasons,
paper P/L. Nothing in Stage 6.0 is tuned on paper P/L.

## 14. Non-goals

Live trading, Robinhood execution, automatic liquidation on Emergency Stop, order cancellation / replacement policy, bracket / stop
orders, extended hours, a holiday calendar (the broker clock is used when available; otherwise weekdays 09:30–16:00 ET), position
sizing of its own, an LLM anywhere in the path, profitability claims.

## 15. Deviations from the stage brief, decisions taken, limitations

* **Route names**: the brief suggested `/api/execution/paper-auto/enable|disable`; frozen Stage 5.5 / 5.9.1 route scans ban the path words
  `paper`, `alpaca`, `broker`, `execute`, `trade`, `place`, `cancel`, `submit`, `order`, so the mode actions are `/api/execution/auto/enable`
  and `/api/execution/auto/disable`. All other routes are GET.
* **Quantity**: the proposal quantity is used exactly. `EXECUTION_MAX_SHARES_PER_ENTRY` (default 0 = off) is an explicit operator hard cap
  (recorded on the intent as `proposal_qty` vs `qty`) so a first live-paper smoke can be run with one share; it never computes a size.
* **A crash before the attempt was recorded** leaves a `PENDING` intent with `attempt_count 0`; on restart it is looked up by client id and,
  when the broker never saw it, marked `NOT_SENT` (`STALE_PENDING`). It is NOT resubmitted blindly: the proposal is burnt until the proposal
  layer regenerates it (price move > 0.5 %, a new setup transition or the 5-minute TTL). Bounded delay, no duplicate.
* **Exits in the final 15 minutes** follow the same session policy as entries (market orders only in the regular session, not the final
  minutes); a required exit stays `EXIT_REQUIRED` and is submitted at the next open. Exits are never submitted while a quantity mismatch
  stands (the position reads Needs attention); a sell of a quantity the broker does not hold would be a blind action.
* **Startup**: the executor thread performs the startup reconciliation as its first step (the app lifespan never blocks on the broker); an
  idle `MONITOR_ONLY` process with nothing open never contacts the broker and writes nothing.
* **Run rows** are written for every active cycle (something monitored, found, evaluated or submitted) and at most every 5 minutes otherwise.
* **Safety gate equity in real mode**: unchanged from Stage 5.9.1 — the gate reads the Stage 4.6A snapshot; the executor does not feed it
  the broker's account read. A stale snapshot (> 15 min) or an unadjustable P&L reads New trades paused and blocks entries (fail closed).
  This is the main operational gap before unattended real-paper runs and the first candidate for Stage 6.0.1.
* **Holiday calendar**: none; the broker clock is consulted and a closed broker clock blocks market orders.
* **Manual paper smoke**: the US regular session was closed when the stage was built (22:00 UTC); no live paper order was placed. The
  deterministic simulated-broker smoke ran in-process and through the browser harness (`paper_bot` flow) on the real executor path.
