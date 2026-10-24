# Stage 6.0.2 — Cash-flow-adjusted daily P&L (DESIGN)

Base: `stage-601-continuous-account-refresh` @ 5d100d8. Branch `stage-602-adjusted-daily-pnl`. Accounting / risk input only: no strategy
change, no execution change, no Robinhood, no model, no real money.

## 1. Audit: what the Alpaca PAPER interfaces expose (read-only, 2026-10-23)

| Source | Exposes | Used |
|---|---|---|
| Stage 6.0 paper client (`execution/broker.py`) | account, clock, assets, positions, orders; its GET allow-list has **no** activities path | untouched |
| Stage 4.6A reader (`paper/alpaca_readonly.py`) | `GET /v2/account/activities?activity_types=FILL`, one page, for the manual panel | untouched |
| alpaca-py 0.44 `TradingClient` | no activities / transfers method | not used |
| **REST `GET /v2/account/activities`** on `paper-api.alpaca.markets` | every activity type, newest first, `page_size` ≤ 100, `page_token` = last id; filters `activity_types`, `date`, `after` / `until` (on the creation time), `category=non_trade_activity`; an unknown type in `activity_types` → HTTP 422, an unknown `category` is silently ignored | **the cash-flow source** |

Observed on the user's paper account (no identifier printed): 5 activities — 3 `FILL` (trades), 1 `FEE` (CAT fee −0.01, with an
`activity_sub_type`), 1 `JNLC` (cash journal +100000.00 on 2026-09-28: the initial paper funding). Non-trade activities carry `date` (the
business date) **and `created_at`, a precise UTC posting timestamp**, plus `net_amount`, `status` ("executed"), `currency`, `description`.
Their id prefix is the business date at midnight (date precision only); fills carry `transaction_time`. **The `description` can contain the
full account number** ("… by PA…") — it is redacted before anything is stored.

Limitations: no transfer / funding API beyond these activity records; no documented guarantee of when a posted cash activity is reflected
in `equity` (taken to be its `created_at`, with a safety margin, §5); securities journals / ACATS (`JNLS`, `ACATS`) carry no cash value;
corrections / cancellations are visible only through `status`.

## 2. Definitions

* **External cash flow** (adjusts the baseline): `DEPOSIT`, `WITHDRAWAL`, `JOURNAL_IN`, `JOURNAL_OUT` — Alpaca types `CSD`, `CSW`, `TRANS`,
  `ACATC` (deposit / withdrawal by the sign of `net_amount`) and `JNLC`, `JNL` (journal in / out by sign).
* **Account performance** (not external; stays in trading P&L): `FILL` (trades, not stored), `FEE` (`FEE`, `CFEE`, `PTC`, `PTR`), `DIVIDEND`
  (`DIV`, `DIVCGL`, `DIVCGS`, `DIVFEE`, `DIVFT`, `DIVNRA`, `DIVROC`, `DIVTW`, `DIVTXEX`), `INTEREST` (`INT`, `INTNRA`, `INTTW`), `OTHER`
  (corporate actions `MA`, `NC`, `OPASN`, `OPEXP`, `OPXRC`, `REORG`, `SC`, `SSO`, `SSP`).
* **`UNCLASSIFIED`**: any other type, `MISC`, and the non-cash transfers `JNLS` / `ACATS` — never guessed as funding.
* **Trading P&L** = everything that moves equity except external cash flow.

## 3. Formula (Decimal; Stage 5.9.1 baseline reused)

    net_external_cash_flow  = (Σ deposits + Σ journal_in) − (Σ withdrawals + Σ journal_out)   (events inside the window, §5)
    adjusted_starting_equity = starting_equity + net_external_cash_flow
    adjusted_daily_pnl       = current_equity − adjusted_starting_equity
    adjusted_daily_loss_pct  = max(0, (adjusted_starting_equity − current_equity) / adjusted_starting_equity × 100)
    adjusted_starting_equity ≤ 0 → ADJUSTED_BASELINE_INVALID (blocked)

Example A: start 100000, deposit 5000, current 104000 → P&L −1000.00, loss 0.95 %. Example B: start 100000, withdrawal 5000, current
96000 → adjusted start 95000, P&L +1000.00, loss 0 % (never a −4000 "loss").

The starting equity is the Stage 5.9.1 persisted `daily_risk_baselines` row (one per New York trading date, created once by a persisted
evaluation from the first fresh equity, never recomputed). No second baseline system.

## 4. Ingestion (`cash_flow/`, owned by the Stage 6.0.1 refresh loop)

Each refresh cycle (same thread, same `alpaca_account_refresher` lease, 60 s in session / 300 s outside): account snapshot first, then
`CashFlowService.refresh()`, then the safety evaluation. The read: `GET /v2/account/activities?category=non_trade_activity&direction=desc&
page_size=100&after=<now − 7 days>` (30 days on the first read of a process), `page_token` paging, at most **20 pages** — a full last page
means `INCOMPLETE` (never silently truncated; the adjustment is then UNCERTAIN); a page that repeats an id already read aborts the read
(no pagination loop). Own transport: GET only, paper host only, that one path only, a request budget of 20 per read; bounded retries
for timeout / connection / 429 / 5xx / unreadable (3 attempts, 2 s then 5 s; a 429 `Retry-After` is honoured up to 30 s); 4xx and invalid
payloads are not retried. Trade rows that still arrive (if the category filter were ignored) are skipped and counted;
every other row is normalized (type, sub-type, category, direction, signed and absolute amount, currency, status, business date, broker
`created_at` = `effective_at`, New York `trading_date` of `effective_at`, redacted description) and stored **append-only, deduplicated by
the broker activity id**; a changed record is stored as a new revision (raw history never mutated). Attempts and liveness are persisted.
The simulated paper broker gets a deterministic activity feed (`cash_flow/simulated.py`) below the same reader.

## 5. The window (timing, late events, day boundary)

Let `cutoff` = the baseline's `equity_as_of` (the snapshot time its equity came from; else its creation time) and `upper` = the current
snapshot's `refreshed_at`; `margin` = 60 s (read duration + clock skew).

* `effective_at ≤ cutoff − margin` → already inside the starting equity → ignored.
* `cutoff − margin < effective_at ≤ cutoff + margin` (external / unclassified) → **AMBIGUOUS** for the day.
* `cutoff + margin < effective_at ≤ upper − margin` → **counted** (external) / ignored (performance) / **UNCERTAIN** (unclassified) /
  **AMBIGUOUS** (external with a status other than "executed").
* `effective_at > upper − margin` (external) → **PENDING**: not yet attributable to the equity reading; resolves on the next refresh.
* Unclassified after `cutoff − margin` → **UNCERTAIN** (never guessed).
* No `effective_at` (date only) on or after the cutoff date → AMBIGUOUS (UNCERTAIN when unclassified).
* **Coverage:** the activity read must have started at or after the snapshot it adjusts (`read_at ≥ upper`); otherwise an unseen event could
  sit between them → STALE. The loop reads the ledger right after the snapshot, so this only bites when an activity read fails.
* **One cycle, seen atomically:** the loop marks each cycle (snapshot → activity read) as in progress. An evaluation that already sees the
  cycle's new snapshot while its activity read is still in flight waits for it, at most `CASH_FLOW_CYCLE_WAIT_SECONDS` (15 s); past the
  bound it fails closed (STALE). Found in manual validation: without it, a reader in the sub-second gap saw a transient STALE.

A late-discovered event is placed by its own `effective_at`, so it is counted (or ignored) exactly as if it had been seen in time; the
next safety evaluation (run after every refresh) re-computes. A new New York trading date gets a new Stage 5.9.1 baseline → a new cutoff →
yesterday's flows are never carried into today.

## 6. Safety integration (additive to Stage 5.9.1)

The gate's account source (`make_safety_account_fn`) adds `cash_flow_status` and `net_external_cash_flow` when the refreshed source is the
Alpaca paper view. `safety.engine.daily_loss` gains one branch: with `cash_flow_status` present it evaluates the adjusted formula
(`adjustments = CASH_FLOW_ADJUSTED`) instead of the old `DAILY_PNL_ADJUSTMENT_UNAVAILABLE` stop. Healthy = fresh snapshot + baseline +
status `ADJUSTED` (+ valid adjusted equity). Otherwise **New trades paused** with a precise reason: `CASH_FLOW_DATA_STALE`,
`CASH_FLOW_DATA_UNAVAILABLE`, `CASH_FLOW_PENDING`, `CASH_FLOW_TIMING_AMBIGUOUS`, `CASH_FLOW_CLASSIFICATION_UNCERTAIN`,
`ADJUSTED_BASELINE_INVALID`; `UNAVAILABLE` (no successful read by this process yet, or the ledger disabled) keeps the Stage 5.9.1 code
`DAILY_PNL_ADJUSTMENT_UNAVAILABLE` — exactly the 6.0.1 behaviour until the first read. A non-ADJUSTED status never computes a loss, so an
unseen withdrawal can never latch a false halt; entries pause instead, and the halt fires from the measured adjusted loss once the data is
known. Without `cash_flow_status` (hypothetical / local paper accounts, the manual 4.6A path, every older caller) the engine is
byte-for-byte the old logic. The safety change is three marked `# >>> Stage 6.0.2` … `# <<< Stage 6.0.2` blocks (constants, two model field
groups, one engine branch + one function); the Stage 6.0 / 6.0.1 pin tests strip exactly those blocks and still compare every other byte. The latch is untouched: an adjusted loss ≥ the threshold → HALTED for the trading date, and nothing
(a deposit included) clears it before the next date. `EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL` stays OFF; it remains only a documented
legacy / dev fallback (with it ON and cash flow unconfirmed, the old unadjusted evaluation applies).

## 7. Failure behaviour

Activity read failure / page limit / unknown type / ambiguous timing → the last stored events stay, the status degrades, new entries pause;
open positions keep being monitored and deterministic exits stay allowed (the gate blocks entries only). No crash loop; recovery is automatic
on the next good refresh (except AMBIGUOUS / UNCERTAIN events inside the day's window, which keep entries paused until the next trading date —
deliberately fail-closed; there is no reclassification UI).

## 8. Persistence, liveness, API, UI

`database/cash_flow_migrations.py`: `cash_flow_events` (append-only, revisions), `cash_flow_refresh_attempts` (append-only),
`cash_flow_liveness` (one row: attempts / successes / failures, consecutive failures, event count, last result / error, last event time,
last page completeness). `GET /api/cash-flow-status` (+ `/events`): status, last refresh, today's event counts, net external cash flow, adjusted
starting equity, adjusted daily P&L, adjusted daily loss %, limit — no ids, no raw descriptions. Trader View keeps Trading allowed / New trades
paused / Trading stopped; the Paper Bot panel adds "Cash-flow data: Fresh" and, under View details, today's trading P/L, the deposits /
withdrawals adjustment, the daily loss and the limit. The health check adds a "Cash-flow data" line.

## 9. Non-goals

Live accounts / real ACH semantics, valuing securities journals, reclassifying unknown activities by hand, intraday re-baselining, changing
strategy / execution / thresholds / the latch, Robinhood, models.
