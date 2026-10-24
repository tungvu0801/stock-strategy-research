# Stage 6.0.1 — Continuous Alpaca PAPER account refresh (DESIGN)

Base: `stage-60-autonomous-paper` @ 1738956 (Stage 6.0 + Windows runner + standalone AAPL smoke; the real one-share smoke PASSED on
2026-10-23). Branch `stage-601-continuous-account-refresh`. **Read-only. No new order logic, no strategy change, no model, no Robinhood.**

## 1. The stale-snapshot problem

In real-data mode the Stage 5.9 proposal account adapter and the Stage 5.9.1 safety gate read the Stage 4.6A Alpaca Paper snapshot
(`paper.alpaca_view`), an in-memory snapshot refreshed only when a user clicks Refresh. After 15 minutes
(`EXECUTION_SAFETY_ACCOUNT_MAX_AGE_SECONDS` = 900) the gate correctly fails closed (`ACCOUNT_SNAPSHOT_STALE` → New trades paused), so an
unattended bot stalls. The snapshot is also not persisted: a restart loses it.

## 2. Refresh ownership

Exactly one owner: `account_refresh.service.AccountRefreshService`, created once per Trader View stream (the app's process-wide service
container, like the setup, proposal, safety and execution services), driven by one daemon thread (`account_refresh.scheduler`) under the
shared `app_leases` lease **`alpaca_account_refresher`**, so two processes on one database never refresh at once. No per-ticker,
per-request or frontend refresher; API reads never refresh. An in-process non-blocking lock refuses an overlapping refresh
(`SKIPPED_RUNNING`).

## 3. Cadence

60 s inside the US regular session, 300 s outside it (`ACCOUNT_REFRESH_SECONDS`, `ACCOUNT_REFRESH_IDLE_SECONDS`). The account data is
**stale** after 600 s without a success (`ACCOUNT_REFRESH_STALE_SECONDS`, comfortably below the gate's 900 s and above the 300 s idle
cadence) or after 3 consecutive failed refreshes (`ACCOUNT_REFRESH_MAX_FAILURES`).

## 4. Snapshot lifecycle

read (account, positions, open orders) → validate the complete candidate → **one transaction**: replace the single current snapshot row,
append an attempt row, update the liveness row. A failed read or a failed validation writes only the attempt row and the liveness
(failure counts, last error): the last good snapshot stays for observability but is never marked newly refreshed.
Freshness (`FRESH | STARTING | STALE | UNAVAILABLE | DISABLED`) is derived at read time from the snapshot's own `refreshed_at`, the
consecutive-failure count and whether **this process** has completed a successful refresh — never from the application start time.

## 5. Broker read boundary

The Stage 6.0 paper client (`execution.broker.AlpacaPaperBroker`): constant host `paper-api.alpaca.markets`, exact GET allow-list, a
per-refresh read budget, `ALPACA_PAPER_*` credentials only. Three GETs per refresh: `/v2/account`, `/v2/positions`,
`/v2/orders?status=open`. No POST, no cancel, no position change, no live host, no Robinhood. Bounded read retries only (3 attempts,
2 s then 5 s) for timeout / connection / 429 / 5xx / unreadable answers; Stage 6.0 order POST semantics are untouched. The deterministic
simulated paper broker sits below the same client for tests and the browser harness.

## 6. Fields

Account: status, trading_blocked, account_blocked, transfers_blocked, trade_suspended_by_user, pattern_day_trader, currency, equity,
last_equity, cash, buying_power, portfolio_value, masked account number, account created_at. Positions: symbol, signed qty, side,
avg_entry_price, cost_basis, market_value, current_price, unrealized P/L. Open orders: short id, client order id, symbol, side, qty,
type, time in force, status, submitted/filled times, filled qty / price. Plus `refreshed_at`, source, host. The **4.6A view shape is
reused** (`view_payload()` returns the same `account` / `positions` / `orders` / `refreshed_at` / `connection` keys the proposal account
adapter already reads), so no second account model reaches the proposal or safety code. No credential, header or account UUID is stored.

## 7. Safety-gate integration (no change to `safety/` or `proposals/`)

The stream injects the refresher through the existing seams: `ProposalService(account=AccountAdapter(alpaca_view=…))` and
`ExecutionSafetyService(account_fn=…)`. When the refresher is **enabled** (paper credentials present, or the simulated paper broker):

* FRESH → the real-mode gate reads the refreshed Alpaca equity (source `ALPACA_PAPER_VIEW`, true age); simulated mode keeps its
  hypothetical review equity.
* not FRESH → real mode: the snapshot age is withheld, so the existing rule returns `ACCOUNT_SNAPSHOT_STALE`; simulated mode: equity is
  withheld (`EQUITY_NOT_AVAILABLE`). Both = **New trades paused**. Never Trading allowed on a failed refresh.

When the refresher is disabled (no credentials, e.g. every pytest default stream) the injected sources behave exactly like the old
defaults.

## 8. The unadjusted-P&L limitation remains

Continuous refresh solves **stale account state only**. The Alpaca snapshot still cannot separate deposits / withdrawals from trading
P&L, so the real-mode gate keeps `DAILY_PNL_ADJUSTMENT_UNAVAILABLE` (New trades paused) unless an operator explicitly sets
`EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL`. This stage does not set it and does not claim that real-data safety is production-complete.

## 9. Execution integration

Unchanged Stage 6.0. The executor already reads the broker live at decision time (account status / blocked flags / buying power in
`BROKER_ACCOUNT`, positions and open orders in reconciliation and `DUPLICATE_ENTRY`); its `SAFETY_GATE` control now consumes the
refreshed snapshot through the gate. Stale or failing refresh → no new entry; open positions keep being monitored and deterministic
risk-reducing exits stay allowed (the gate blocks entries only). Proposal sizing formula, thresholds, order type and exactly-once logic
are untouched; in real mode the proposals now size against the refreshed Alpaca equity instead of a manual snapshot.

## 10. Startup

The lifespan starts the refresher thread before the executor thread; its first action is an immediate `STARTUP` refresh, then a safety
evaluation. Until that refresh succeeds the data reads `STARTING` and the gate stays paused, so a cached snapshot from before the restart
can never make entries eligible. The executor's own startup reconciliation is unchanged. Never in a test / harness process
(`STOCK_AGENT_ENV`, pytest) and never without a configured paper broker.

## 11. Failure behaviour

Each failure: attempt row (result, error code, attempts), liveness (`consecutive_failures`, `last_error`), last snapshot kept, no crash
loop (the thread logs and waits for the next interval). Recovery is automatic on the next successful refresh; no restart, no manual action.

## 12. Persistence / liveness

`database/account_refresh_migrations.py`: `account_refresh_snapshot` (one row, replaced atomically), `account_refresh_attempts`
(append-only), `account_refresh_liveness` (one row: last attempt / success, attempt / success / failure counts, consecutive failures,
last result / error, last snapshot age, owner, lease). Triggers keep the history append-only.

## 13. API, UI, health check

`GET /api/account-refresh` (status, fresh, age, last success / attempt, last error, counts, loop state, a snapshot summary) and
`GET /api/account-refresh/history`. The route deliberately avoids the words "alpaca" / "paper" / "broker" (frozen Stage 4.5 / 4.6A route
pins). Trader View: one line in the Paper Bot panel — **Account data: Fresh (42 s ago)** / **Stale** / **unavailable** — with the last
refresh, age, last error and attempts under View details; no raw payload. `scripts\check_stock_agent.ps1` adds the same line.

## 14. Stage 6.1 dependency

6.0.1 makes multi-hour, multi-session unattended paper forward validation operationally possible (fresh account state, liveness rows,
attempt history for uptime / freshness metrics). It claims nothing about profitability. Stage 6.1 measures uptime, snapshot freshness,
control fires, duplicates, reconciliation failures, rejects, slippage, conversion, exit reasons and descriptive paper P/L.

## 15. Non-goals

Cash-flow-adjusted daily P&L, any order / cancel / liquidation path, changing the Stage 4.6A manual Alpaca Paper panel (it stays a manual
comparison view), a refresh button, websockets / streaming account updates, Robinhood, live trading, strategy changes.
