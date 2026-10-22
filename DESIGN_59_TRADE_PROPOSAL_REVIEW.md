# Stage 5.9 — Deterministic Trade Proposal / Review Layer (DESIGN)

Base: `origin/main` e714dc7 (Stage 5.8 merged). Branch `stage-59-trade-proposal-review`.

**A READY setup is not a trade.** Stage 5.9 answers one question for a `READY FOR REVIEW` setup: *if the user wants to review a
possible position, what would a deterministic, risk-bounded proposal look like?* The answer is a **review artifact**: it is not a
recommendation, not an order, not a BUY signal, and it is never submitted anywhere. The user remains the only decision maker.
Visible wording everywhere: "This is a deterministic review proposal, not a recommendation or submitted order."

## 1. Audit of what exists (reused, not duplicated)

| Need | Existing code (frozen, read only) | Stage 5.9 use |
|---|---|---|
| Deterministic order rules | `paper/alpaca_order_rules.py` (4.6B): `MAX_QTY` 10 000, `MAX_NOTIONAL` 100 000, `BUY_BUFFER` 1.05 (`guard_amount`), `SYMBOL_RE`, `UNRESOLVED` / `LIVE` intent states | hard quantity / notional caps, the 5 % buying-power buffer, the symbol regex, the pending-intent states |
| Unresolved / live paper intents | `paper/alpaca_order_store.py::OrderStore.exists() / in_states()` (read only; pattern of `rotation/engine.py::default_conflicts`) | DUPLICATE_ORDER control (4.6B intents in the symbol) |
| Local paper pending orders | `paper/store.py::PaperStore.orders(acct, status="PENDING")`, `paper/portfolio.py::view()` (equity, cash, positions) | DUPLICATE_ORDER control, LOCAL_PAPER equity / buying power / existing position |
| Alpaca paper account (0 network calls) | `paper/alpaca_view.py::view()` — the in-memory 4.6A snapshot (`account.equity / buying_power / cash`, `positions`, `orders`, `refreshed_at`, `trading_blocked` / `account_blocked`); refreshed only by the user | ALPACA_PAPER_VIEW equity / buying power / existing position / open orders / broker blocks |
| Existing position per source | `TerminalState.position` (`trader_view/positions.py`, per source, never summed) | the existing-position figure for the account source in use |
| ATR | `ppe/features.py::atr_last(bars, period)` (Wilder, via `analysis/indicators.atr`) on `market.daily_bars()` completed sessions | ATR(14) for the stop rule (`config.ATR_PERIOD` is 14 — the project's ATR convention) |
| Sizing rule | `analysis/position_sizing.py::compute_position_sizing` (risk-based shares = floor(equity × risk% / risk per share); allocation shares = floor(equity × position% / price); the smaller wins) and `config.MAX_POSITION_PERCENT` = 10 | the same two formulas, in `Decimal`, plus the 4.6B hard caps and the buying-power cap; `MAX_POSITION_PERCENT` is the position cap (`cap_source = config.MAX_POSITION_PERCENT`) |
| Reference price | `trader_view` quote (`price`, `bid`, `ask`, `ts`), minute candles (`final`), `market.daily_bars()` | §4 hierarchy |
| Market hours | `setups/market_hours.py` | near-close / closed warnings, daily-reference expiry |
| Setup state | `setups/service.py::SetupService.current()`; `setups.engine.evidence_gate / evidence_contradiction` on the stored inputs | eligibility, evidence blockers |
| Execution | `paper/alpaca_orders.py` (preview / confirm) and `paper/alpaca_order_writer.py` | **not called, not imported** (§13) |

**Gaps found (not invented away):**
* There is **no kill switch** in the project. `config.MAX_DAILY_LOSS_PERCENT` and `MAX_OPEN_POSITIONS` are documented as
  informational and are enforced nowhere; the 4.6B `enabled` flag only gates the manual order form. Stage 5.9 therefore adds two
  explicit, review-only halt flags (`TRADE_PROPOSAL_HALT`, `TRADE_PROPOSAL_DAILY_LOSS_HALT`, both default off) and observes the
  broker's own account blocks (`trading_blocked` / `account_blocked` in the 4.6A snapshot). It does **not** compute a daily P&L and
  does not add an execution kill switch; the gap is reported, not hidden.
* There is **no zero-call tradability source** (the 4.6B asset check is a network GET made at preview time by the frozen service;
  Trader View reports UNKNOWN / SIMULATED). Tradability is therefore a control that blocks only when a source says *not tradable*
  and otherwise records UNKNOWN as a warning.
* Alpaca `buying_power` exists only in the 4.6A snapshot; the local simulator has `cash`; both are used as "buying power" with the
  source named.

## 2. Purpose, lifecycle, statuses

* One proposal type in V1: a **long review proposal** (`side_context = LONG_REVIEW`, label "Long review proposal") for the
  `DOWNSIDE_DISLOCATION_RECOVERY` setup while its state is `READY` (UI label READY FOR REVIEW). No short proposals.
* Lifecycle: `NOT_ELIGIBLE` (setup not READY — computed, never stored) → `BLOCKED` or `REVIEWABLE` (stored snapshot) →
  `STALE` (the setup left READY or changed episode) / `EXPIRED` (past `expires_at`) — both derived at read time from the stored
  row, never written back. A new generation event creates a new row; old rows stay as history.
* Review states (the only mutable fields): `UNREVIEWED` → `VIEWED` → `DISMISSED` (`POST /api/proposals/{id}/view`,
  `POST /api/proposals/{id}/dismiss`). `USER_ACCEPTED_FOR_PREVIEW` is **not** implemented in V1.
* Forbidden words as statuses or labels: APPROVED, BUY, EXECUTE, GO. The visible status line is always `NOT SUBMITTED`.

## 3. Eligibility (Phase 2) — exact

A proposal is generated only when ALL hold; otherwise the response carries explicit blockers:

1. an open setup episode exists for the symbol, type `DOWNSIDE_DISLOCATION_RECOVERY` (`SETUP_NOT_FOUND`, `SETUP_TYPE_UNSUPPORTED`);
2. its state is `READY` (`SETUP_NOT_READY`); `INVALIDATED` → `SETUP_INVALIDATED`;
3. the setup evaluation is fresh: `last_evaluated_at` within `max(3 × TRADER_SETUP_REFRESH_SECONDS, 300)` s (`SETUP_STALE`);
4. PPE in the stored setup inputs is AVAILABLE / SIMULATED_DATA and fresh (`PPE_NOT_AVAILABLE`, `PPE_STALE`);
5. Demand likewise (`DEMAND_NOT_AVAILABLE`, `DEMAND_STALE`);
6. no evidence gate on the stored inputs (`EVIDENCE_BLOCKED:<gate code>`) and no contradiction (`EVIDENCE_CONTRADICTION`);
7. a reference price exists and is fresh (§4);
8. no hard risk blocker (§9);
9. tradability: blocks only when known false.

Items 1–2 make the result `NOT_ELIGIBLE` (not stored). Items 3–9 are controls (§10): a failure makes the result `BLOCKED`
(stored, with its controls). Simulated / test modes count every input as fresh (the Stage 5.8 convention).

## 4. Reference price (Phase 4) — hierarchy

In order; the first usable candidate wins and is named in `price_source`; a candidate that is present but not a positive finite
number blocks with `PRICE_INVALID` (nothing falls through silently):

1. `QUOTE_MID` — bid > 0, ask ≥ bid, (ask − bid) / mid ≤ 1 %, quote status OK and age ≤ 300 s → mid, `price_mode = INTRADAY`;
2. `LAST_TRADE` — quote `price` > 0, status OK, age ≤ 300 s → `INTRADAY`;
3. `MINUTE_CLOSE` — the latest completed (`final`) 1-minute candle not older than 15 min → `INTRADAY`;
4. `DAILY_CLOSE` — the latest completed daily bar (dated before the current New York session) → `price_mode = DAILY_REFERENCE`
   (warning `DAILY_REFERENCE_PRICE`; expiry §12).

None usable → `PRICE_NOT_AVAILABLE`; stale quote with no fallback → `PRICE_STALE`. Simulated / test modes: quote age is not checked.
Recorded: `reference_price` (4 dp), `price_source`, `price_timestamp` (ISO), `price_mode`.

## 5. Invalidation level (Phase 5) — exact rule, fixed before any outcome analysis

Inputs: completed daily bars (before the current session), the current session's completed minute candles, ATR.

* `ATR` = Wilder ATR over `ATR_PERIOD = 14` completed sessions (`ppe.features.atr_last`); needs ≥ 15 bars, else `ATR_NOT_AVAILABLE`.
* `structural_low` = min(low of the last `STRUCTURAL_LOOKBACK_SESSIONS = 5` completed sessions, low of the current session's
  completed minute candles when ≥ 30 of them exist). Reliable when ≥ 5 completed sessions exist and `structural_low < reference`.
* `structural_stop = structural_low − 0.25 × ATR` (buffer `STRUCTURAL_BUFFER_ATR = 0.25`).
* `atr_stop = reference − 1.5 × ATR` (`FALLBACK_STOP_ATR = 1.5`).
* `invalidation_price = max(structural_stop, atr_stop)` when the structural low is reliable, else `atr_stop`.
  `stop_rule` records which level won: `STRUCTURAL` (structural level, within 1.5 ATR), `ATR_CAP` (the structural level was more
  than 1.5 ATR below the reference: the ATR level caps the risk per share), `ATR_FALLBACK` (no reliable structural low).
  Rationale (documented, not tuned): the ATR level is a ceiling on risk per share; the dislocation low tightens it when nearer.
* Prices are quantized to cents. `invalidation_price ≥ reference_price` → `INVALID_STOP`. No fixed-percentage stop exists.
* V1 does not use a setup-derived price threshold (the setup's own invalidation is a stretch score, not a price).

## 6. Risk per share (Phase 6)

`risk_per_share = reference_price − invalidation_price` (4 dp) — must be > 0, else `RISK_PER_SHARE_INVALID`.
`risk_pct = risk_per_share / reference_price × 100` (2 dp). No target price, no reward, no expected return (Phases 23–24).

## 7. Account equity and risk budget (Phase 7)

Equity source hierarchy (the first available wins, named in `equity_source`; nothing is faked):

| mode | order |
|---|---|
| real | `ALPACA_PAPER_VIEW` (4.6A snapshot; `as_of = refreshed_at`; warning `ACCOUNT_SNAPSHOT_STALE` when older than 15 min) → `LOCAL_PAPER` (Stage 4.5 `summary.equity`, unusable while a holding is unmarked) → `EQUITY_NOT_AVAILABLE` |
| simulated | `HYPOTHETICAL_REVIEW` = `config.TRADE_PROPOSAL_REVIEW_EQUITY` when set (> 0) — explicit, labelled, warning `EQUITY_HYPOTHETICAL`; otherwise `EQUITY_NOT_AVAILABLE` |

`risk_fraction = config.TRADE_PROPOSAL_RISK_FRACTION` (default **0.005** = 0.5 % of equity; valid range 0 < f ≤ 0.01, otherwise
`RISK_FRACTION_INVALID`). `risk_budget = equity × risk_fraction` (cents); ≤ 0 → `RISK_BUDGET_ZERO`. Never optimized.

## 8. Quantity (Phases 8–10)

* `base_qty = floor(risk_budget / risk_per_share)`.
* Position cap: `max_position_pct = config.MAX_POSITION_PERCENT` (10 %; existing rule), `max_position_notional =
  min(equity × max_position_pct / 100, 4.6B MAX_NOTIONAL)`; `cap_source` names the binding bound.
  `available_notional = max_position_notional − existing_position_notional`; `position_cap_qty = floor(available_notional / reference)`,
  also ≤ `MAX_QTY − existing_qty`. Existing notional ≥ cap → `POSITION_CAP_EXCEEDED` (no additional proposal).
* Buying power: `buying_power_qty = floor(buying_power / (reference × 1.05))` (the 4.6B cash-guard buffer). Known and < 1 →
  `BUYING_POWER_INSUFFICIENT`; unknown → warning `BUYING_POWER_UNKNOWN`, no cap.
* `final_qty = min(base_qty, position_cap_qty, buying_power_qty if known, MAX_QTY)`; `< 1` → `QUANTITY_BELOW_ONE_SHARE` with the
  binding cap named. Every cap contribution is returned (`caps`: risk_based, position_cap, buying_power, hard_limit, binding).
* Existing position (same account source): reported explicitly (`existing_position_qty / notional / avg_cost`, warning
  `EXISTING_POSITION`); never averaged down automatically; the cap applies to existing + proposed.
* `proposed_notional = final_qty × reference_price`. All arithmetic is `Decimal`; whole shares only; never negative; never NaN.

## 9. Hard blockers vs warnings (Phase 13)

Hard blockers (status `BLOCKED`): `SETUP_STALE`, `PPE_STALE`, `PPE_NOT_AVAILABLE`, `DEMAND_STALE`, `DEMAND_NOT_AVAILABLE`,
`EVIDENCE_BLOCKED:*`, `EVIDENCE_CONTRADICTION`, `PRICE_NOT_AVAILABLE`, `PRICE_STALE`, `PRICE_INVALID`, `ATR_NOT_AVAILABLE`,
`INVALID_STOP`, `RISK_PER_SHARE_INVALID`, `EQUITY_NOT_AVAILABLE`, `RISK_FRACTION_INVALID`, `RISK_BUDGET_ZERO`, `KILL_SWITCH_ACTIVE`,
`DAILY_LOSS_HALT_ACTIVE`, `SYMBOL_NOT_TRADABLE`, `DUPLICATE_PENDING_ORDER`, `BUYING_POWER_INSUFFICIENT`, `POSITION_CAP_EXCEEDED`,
`QUANTITY_BELOW_ONE_SHARE`. (`SETUP_NOT_FOUND`, `SETUP_TYPE_UNSUPPORTED`, `SETUP_NOT_READY`, `SETUP_INVALIDATED` make `NOT_ELIGIBLE`.)

Warnings (proposal still renders): `EVIDENCE_WEAK`, `CONFIDENCE_MODERATE` (< 60), `DEMAND_MARGINAL` (< 70),
`DAILY_REFERENCE_PRICE`, `EXISTING_POSITION`, `MARKET_NEAR_CLOSE` (≤ 15 min to 16:00 ET), `MARKET_CLOSED`, `SECTOR_FALLBACK`
(PPE benchmark fell back to the market proxy), `TRADABILITY_UNKNOWN`, `BUYING_POWER_UNKNOWN`, `EQUITY_HYPOTHETICAL`,
`ACCOUNT_SNAPSHOT_STALE`, `SIMULATED_DATA`, `QTY_REDUCED_BY_POSITION_CAP`, `QTY_REDUCED_BY_BUYING_POWER`, `QTY_REDUCED_BY_HARD_LIMIT`.
The two lists never mix.

## 10. Controls and liveness (Phases 11–12)

Every hard blocker is produced by a named control. Each eligible evaluation records one `ControlEvaluation` per control:
`control_id`, `result` (`PASS` | `BLOCK` | `WARN` | `UNKNOWN`), `reason`, `input` (the exact values the control saw).

| control_id | input it sees | BLOCK code |
|---|---|---|
| `SETUP_FRESHNESS` | setup `last_evaluated_at`, window | `SETUP_STALE` |
| `PPE_FRESHNESS` | ppe status / `ppe_fresh` | `PPE_NOT_AVAILABLE` / `PPE_STALE` |
| `DEMAND_FRESHNESS` | demand status / `demand_fresh` | `DEMAND_NOT_AVAILABLE` / `DEMAND_STALE` |
| `EVIDENCE_GATE` | gate code, contradiction, strength | `EVIDENCE_BLOCKED:*`, `EVIDENCE_CONTRADICTION` |
| `PRICE_REFERENCE` | candidates tried, chosen source, age | `PRICE_NOT_AVAILABLE` / `PRICE_STALE` / `PRICE_INVALID` |
| `INVALIDATION_LEVEL` | ATR, structural low, both stops, rule | `ATR_NOT_AVAILABLE` / `INVALID_STOP` |
| `RISK_PER_SHARE` | reference, invalidation | `RISK_PER_SHARE_INVALID` |
| `ACCOUNT_EQUITY` | equity, source, as_of | `EQUITY_NOT_AVAILABLE` |
| `RISK_BUDGET` | fraction, budget | `RISK_FRACTION_INVALID` / `RISK_BUDGET_ZERO` |
| `KILL_SWITCH` | `TRADE_PROPOSAL_HALT`, broker account blocks | `KILL_SWITCH_ACTIVE` |
| `DAILY_LOSS_HALT` | `TRADE_PROPOSAL_DAILY_LOSS_HALT` (explicit; no automatic P&L exists) | `DAILY_LOSS_HALT_ACTIVE` |
| `TRADABILITY` | tradable flag + source | `SYMBOL_NOT_TRADABLE` (UNKNOWN → warning) |
| `DUPLICATE_ORDER` | unresolved + live 4.6B intents, local pending orders, snapshot open orders (per symbol) | `DUPLICATE_PENDING_ORDER` |
| `BUYING_POWER` | buying power, source, buffered qty | `BUYING_POWER_INSUFFICIENT` (unknown → warning) |
| `POSITION_CAP` | cap notional, existing notional, cap qty | `POSITION_CAP_EXCEEDED` |
| `QUANTITY` | every cap contribution | `QUANTITY_BELOW_ONE_SHARE` |

Persistence: `proposal_controls` (append-only, one row per control per generated proposal) and `proposal_control_status`
(one upserted row per control: `last_evaluated_at`, `last_result`, `last_reason`, `last_symbol`, `last_proposal_id`,
`evaluation_count`, `block_count`, `last_blocked_at`, `last_input_json`). Both are read through `GET /api/proposals/controls`.
"Was it evaluated, what did it see, what did it return, when, has it ever blocked" are all answered from these rows.

## 11. Identity, persistence (Phases 16–17)

* `proposal_id = sha256(setup_id | created_at | reference_price | invalidation_price | risk_budget)[:32]`.
* Table `trade_proposals` (append-only; only `review_state` / `reviewed_at` may change; no delete, no replace):
  `proposal_id, setup_id, symbol, setup_type, side_context, status, created_at, expires_at, price_mode, reference_price,
  price_source, price_timestamp, invalidation_price, stop_rule, structural_low, atr, atr_period, risk_per_share, risk_pct, equity,
  equity_source, risk_fraction, risk_budget, base_qty, position_cap_qty, buying_power_qty, final_qty, max_notional,
  max_position_pct, cap_source, existing_position_qty, existing_position_notional, existing_avg_cost, proposed_notional,
  blockers_json, warnings_json, inputs_json, controls_json, ppe_snapshot_json, demand_snapshot_json, evidence_snapshot_json,
  setup_snapshot_json, review_state, reviewed_at, trigger, owner, source, mode, fingerprint`.
* The proposal row, its control rows and the status upserts are written in ONE transaction (`BEGIN IMMEDIATE`); a failed write
  is reported as `ERROR` and nothing is pretended.
* Reads tolerate missing tables (a database without Stage 5.9 tables answers "no proposals").

## 12. Freshness, expiry, regeneration (Phase 15)

* `expires_at`: `INTRADAY` → `created_at + TRADE_PROPOSAL_TTL_SECONDS` (300 s); `DAILY_REFERENCE` → the earlier of the next
  09:30 ET session open and `created_at + 24 h`.
* At read time: `EXPIRED` when `now ≥ expires_at`; `STALE` when the current stored setup is not the same episode in `READY`
  (different `setup_id`, state or `since`). Both are derived, never written.
* Regeneration (a new row) happens when any material input changed versus the latest stored row of the same `setup_id`: status,
  the set of blocker codes, `invalidation_price`, `final_qty`, `equity_source`, equity (cents), `evidence_packet_id`, or
  `|reference − previous reference| / previous reference > TRADE_PROPOSAL_PRICE_CHANGE_PCT` (0.5 %), or the previous row expired.
  Otherwise the stored row is **reused** (no new row on every refresh); the control status rows are still touched so liveness is
  current. Stale rows are never silently reused: a STALE / EXPIRED row always leads to a fresh generation on the next evaluation.

## 13. Review-only boundaries and the relationship to Stage 4.6B

* `proposals/` never imports `paper.alpaca_orders`, `paper.alpaca_order_writer`, `paper.alpaca_order_reads`, `paper.execution`,
  any trading client, Robinhood module, model client or research builder (pinned by tests). It reads: the 4.6A snapshot
  (0 network calls), the local paper view, the frozen 4.6B rules constants and intent store (read only), market data already
  held by Trader View, and the stored setup state.
* No route under `/api/proposals` buys, sells, previews, submits, places or cancels; the only POSTs change `review_state`.
* Nothing is triggered by an alert or a setup transition except the generation of a review artifact. No LLM is involved.
* Prepare Buy / Prepare Sell stay disabled (`PREVIEW_NOT_CONNECTED`). The 4.6B manual order form stays separate; the future
  hand-off (user accepts → execution preview → user confirms → 4.6B) is **not wired**: Stage 5.9 stops before any preview
  submission or broker POST.
* No target price, no expected return, no probability of profit, no win rate, no expected value (Phases 23–24).

## 14. Evaluation points and UI (Phases 20–22)

* Generation runs after every setup evaluation (subscribe, refresh, in-session cadence, scheduler, forced test evaluations):
  cheap when the setup is not READY. Socket message `proposal` (symbol-bound). `GET /api/proposals/{symbol}` never generates.
* Trader View section **TRADE REVIEW** under the Setup card: status chip (`REVIEWABLE PROPOSAL` / `BLOCKED` / `NOT ELIGIBLE` /
  `STALE` / `EXPIRED`), "Long review proposal", Reference, Invalidation, Risk / share, Risk budget, Max quantity, Max notional,
  Warnings, Blockers, `Status NOT SUBMITTED`, buttons **View Details** and **Dismiss** (review state only; the POSTs live in
  `frontend/trade_review.js`). The detail view shows SETUP CONTEXT, PRICE / INVALIDATION, ACCOUNT RISK, SIZE, CONTROLS, WARNINGS,
  STATUS exactly as Phase 22 lists them. No "Prepare Preview" button in V1.

## 15. Configuration (append-only, `config.py`)

`TRADE_PROPOSAL_RISK_FRACTION` 0.005 · `TRADE_PROPOSAL_REVIEW_EQUITY` unset (0) · `TRADE_PROPOSAL_TTL_SECONDS` 300 ·
`TRADE_PROPOSAL_PRICE_CHANGE_PCT` 0.5 · `TRADE_PROPOSAL_HALT` false · `TRADE_PROPOSAL_DAILY_LOSS_HALT` false.
Reused as-is: `MAX_POSITION_PERCENT` (10), `ATR_PERIOD` (14), 4.6B `MAX_QTY` / `MAX_NOTIONAL` / `BUY_BUFFER`.

## 16. Non-goals

Execution, order preview submission, automatic paper trading, short proposals, target prices, expected returns, probabilities,
sizing optimisation against historical P&L, a kill-switch subsystem, a daily P&L engine, a holiday calendar, an LLM anywhere.
