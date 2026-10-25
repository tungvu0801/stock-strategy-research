# Stage 6.1.2 — Unified Product Flow + Strategy Lab Foundation (DESIGN)

Base: `origin/main` 1825958 (Stage 6.1 Forward Validation, Stage 6.1.1 Product UI, the Paper Auto state-sync fix). Branch `stage-612-unified-product-flow`.

**Navigation, presentation, information architecture and a strategy-management foundation.** Strategy logic, risk / safety semantics, the Alpaca
execution semantics, the exactly-once order behaviour, reconciliation and the fail-closed rules are unchanged: every production package of the
earlier stages is byte-identical to the base except `api/server.py`, which gains two additive lines for one GET-only router
(`tests/test_product_flow_612.py` F-tests; `forward_validation.frozen_controls()` hash unchanged). Paper trading only. No real order.

## 1. Objective

After Stage 6.1.1 the app still exposed thirteen legacy modules under MORE and read as a newer shell over an older collection of tools. Stage 6.1.2
makes ONE coherent product flow and ONE research flow, without deleting backend functionality:

```
primary   START / RUNNING -> TRADE -> Opportunity -> READY -> Proposal / Auto Entry -> IN TRADE -> POSITIONS -> EXIT -> ACTIVITY
research  MORE -> Strategy Lab -> Research -> Backtest -> Review -> Paper Candidate -> Paper Active -> Approved / Rejected
```

Complex inside, simple outside. Default layer: WATCHING · READY · IN TRADE · NEEDS ATTENTION. Second layer: Why? · strategy · risk · blocker. Third layer:
Technical details · raw evidence · timestamps · diagnostics.

## 2. Navigation before / after

| Before (13 peer tabs) | After (4 sections) |
|---|---|
| Dashboard, Daily Review, Watchlist, AI Agent, Alerts, Research Setups, Research History, Portfolio, Trader Review, Strategy Lab, Paper Trades, Trader View, Settings | **TRADE · POSITIONS · ACTIVITY · MORE** (exactly four; the legacy bar stays in the markup, hidden by the shell) |

Legacy modules mapped (nothing deleted; every legacy tab stays reachable):

| Legacy | Now |
|---|---|
| Watchlist, Trader View, Dashboard | TRADE (the watchlist and the chart; the Market dashboard and the Watchlist editor under MORE › System › Diagnostics) |
| Portfolio (Robinhood, read only) | POSITIONS hosts the paper positions; the Robinhood view stays under MORE › System › Diagnostics |
| Paper Trades | ACTIVITY › Trades filter (and POSITIONS for the live ones) |
| Alerts | ACTIVITY (setup transitions in the timeline; the legacy lists under a fold) |
| Daily Review / Trader Review | ACTIVITY › Daily Review strip (trading); the research Daily Review and Trader Review under MORE › System › Diagnostics |
| Forward Validation | ACTIVITY (compact summary) · MORE › System › Forward Validation (detailed diagnostics page) |
| Research Setups, Research History, Strategy Lab | MORE › Research (Strategy Lab registry, Backtests / rule builder, Research History, Research Setups) |
| AI Agent | a tool under MORE › System › Diagnostics, never a primary tab |
| Settings | MORE › Settings (Risk, Automation, Notifications, Advanced configuration) |

Legacy deep links (`frontend/product_ui.js` `LEGACY_LINKS` / `routeFor`): `#portfolio` -> POSITIONS, `#paper` / `#paper-trades` -> ACTIVITY · Trades,
`#trader` / `#trader-view` / `#watchlist` / `#overview` -> TRADE, `#alerts` / `#daily` / `#review` -> ACTIVITY, `#strategy` / `#strategy-lab` / `#setups` ->
MORE › Research › Strategy Lab, `#history` -> MORE › Research, `#agent` -> MORE › System › Diagnostics, `#settings` -> MORE › Settings,
`#forward-validation` -> MORE › System. The four product hashes map to themselves and the hash follows the nav (`history.replaceState`). A hash the
map does not know (`#daily-brief`, consumed by `brief_delivery.js`) is never rewritten or routed.

## 3. MORE

Three groups only, each a collapsed fold (progressive disclosure; nothing renders fifteen options at once):

```
Research   Strategy Lab · Backtests (rule builder, legacy lab) · Research History · Research Setups
System     Broker Connection · Data Sources · Forward Validation · Diagnostics (Market dashboard, AI Agent, Watchlist editor, Daily Review (research), Trader Review, Portfolio - Robinhood, read only) · Logs
Settings   Risk · Automation · Notifications · Advanced configuration
```

The panels are read only: Broker Connection (`GET /api/account-refresh` + the executor's environment proof), Data Sources (the Trader View feed and the
diagnostics chips), Logs (the persisted executor cycle records of `GET /api/execution`), Risk (the frozen controls of
`GET /api/forward-validation/frozen-controls` and the gate's state), Automation (Paper Auto, the loops; the controls stay on TRADE), Notifications
(setup alerts, the Daily Brief delivery workspace). The Strategy Lab's own workspaces (Builder, Strategy Fit, Evidence, Scanner, Daily Brief, Rotation
Backtest, …) are listed inside the registry panel, not in MORE.

## 4. TRADE

* **Focus strip** (presentation-only priority, `priorityModel`): the first item of `NEEDS ATTENTION / reconciliation / critical system problem ->
  EXIT REQUIRED / EXITING -> open position needing attention -> SYSTEM DEGRADED -> IN TRADE -> REVIEWABLE proposal -> READY -> WATCHING -> market
  summary`. A degraded or stopped system is therefore the first thing on TRADE, not the WATCHING stock. It is a strip above the cards, never a card and
  never a decision.
* **Strategy display** (`strategyModel`): `Best setup · Downside Dislocation Recovery` (WATCHING) or `Best strategy · …` (READY) from the registry
  (`GET /api/strategy-registry`, read once). Supporting strategies: "No other strategy evaluated." Opportunity score: "No opportunity score: one
  strategy, no cross-strategy ranking yet." Placeholders are never evaluated, supporting or scored. When the registry has no active strategy the line
  reads "No strategy result available"; while it loads, a dash.
* **READY is setup eligibility.** The READY card adds an execution line from the one blocker surface (§5): `Automatic entry off · Monitor only: no order
  is sent …`, `Cannot enter · <reason>` or `Entry allowed when every check passes`. `[ Why? ]` opens the WHY panel; `[ View Proposal ]` (only when a
  proposal is REVIEWABLE or BLOCKED) opens the Stage 5.9 review panel (NOT SUBMITTED). Nothing on TRADE implies an order will be sent.
* **WHY panel** (`whyModel`, reusable): the question for the state (Why WATCHING? Why READY? Why is the position still held? Why did it exit? …) and
  seven sections - Signal · Strategy · Supporting evidence · Against / risks · Portfolio · Safety · Decision - from deterministic backend state only:
  the Stage 6.0 card, the stored Stage 5.8 setup (`GET /api/terminal/state/{symbol}`, read on demand while Why? is open, 10 s cache), the Stage 5.9
  proposal (warnings, blockers, execution eligibility), the Stage 5.9.1 gate, the account data and the limits. Thresholds come from the frozen controls
  ("Demand 48 (needs >= 60)"). Raw values sit under **Technical details**. No model is consulted; the model is a pure function (same input, same output).
* **Trading session line** on the Risk card (`sessionModel`): `AUTO ON · TRADING PAUSED`, `AUTO ON · TRADING STOPPED`, `AUTO OFF · TRADING ALLOWED`, …
  Paper Auto ON is never trading allowed. The real controls stay the three rows (Paper Auto, Pause / Resume New Trades, Emergency Stop / Clear) with
  their 6.1.1 confirmations and legacy delegation; a unified START TRADING SESSION control is planned, no backend endpoint is added and no fake button
  exists.

## 5. One blocker surface

`blockerModel(exec, safety, dom, terminal, limits)` returns the first deterministic reason in precedence - the safety gate (stopped / emergency stop /
paused with the gate's own sentence), reconciliation (`Reconciliation required.`), account data (`Account equity unavailable.`), the position cap
(`Maximum open positions reached.`), the session (`Market closed …`, `Near-close entry cutoff reached.`), the proposal's own first blocker - else
"Automatic entry off" (Monitor only) or "Entry allowed when every check passes". The same object feeds the READY execution line and the WHY Decision
section; the focus strip reads the system state itself (`systemModel`, the same gate / account / reconciliation fields). Wording is one title
(`Cannot enter`) plus one sentence.

## 6. POSITIONS

The full lifecycle surface from the persisted position, its card and its intents (`positionLifecycle`): `ENTERING · IN TRADE · EXIT REQUIRED · EXITING ·
CLOSED · NEEDS ATTENTION`. Each card: quantity, entry, current, paper P/L, holding day, **Strategy**, **Entry reason** (READY setup with a reviewable
proposal · the fill), **Risk / invalidation** (invalidation price, risk budget), holding since, **Why still holding?** (the exit rules the persisted
position carries, in the engine's precedence, with the frozen thresholds once loaded: invalidation vs current, the demand streak vs the DEMAND_FAILURE
count, holding day vs max, weakening; the stretch rule needs the live PPE panel and is shown by TRADE › Why? for the active ticker), the exit reason and
the broker order status while exiting, the attention reason. Closed trades: realized paper P/L, exit reason, entry -> exit, held, closed at. The exit
logic itself is untouched; this is display of persisted rows.

## 7. ACTIVITY and the Daily Review

One timeline (persisted rows only, de-duplicated by timestamp + text), grouped for the filters `All · Trades · Safety · System · Research`
(`GROUP_OF`): setups -> research; proposals, orders, fills, positions, exits -> trades; safety-gate events and attention -> safety; reconciliation and
validation -> system. The **Today** strip is the Daily Review: System (health), Market (OPEN / CLOSED from the scheduler), Setup changes, READY setups,
Entries, Exits, Blocked (proposals), Safety blocks, Paper P/L, Attention - only counts the persisted rows support; no market regime (none is implemented
in the execution path) and no estimate. The compact Validation block stays; the detailed diagnostics live on `forward_validation.html` (MORE › System).

## 8. Strategy Lab foundation

`strategy_registry/` + `GET /api/strategy-registry[/standard|/{id}]` (read only) and the MORE › Research › Strategy Lab panel: the registry (one
implemented strategy, four placeholders, QuantConnect as a supported source with nothing registered), the lifecycle enum, the pipeline statement and
the detail view with thirteen sections (DOCUMENTED / LIVE / Not yet evaluated / Unavailable). See `DESIGN_612_STRATEGY_REGISTRY.md`,
`STRATEGY_STANDARD_V1.md` and `DESIGN_612_QUANTCONNECT_ADAPTER.md`. Strategies produce evidence and signals; they never execute:
`Strategy -> Opportunity / Proposal -> Portfolio -> Risk / Safety -> Execution -> Broker`.

## 9. No fake data

No opportunity, strategy score, trade, performance, paper result, QuantConnect result or market regime is invented. Empty states are explicit
("No strategy result available", "No other strategy evaluated.", "No opportunity score …", "Not yet evaluated", "No closed paper trade yet.").
The Downside Dislocation Recovery detail is built only from the frozen controls, the module constants and the cited design documents; its backtest,
out-of-sample and decision-log sections say that nothing exists.

## 10. Backend production changes

* New package `strategy_registry/` (models, standard, registry, service, downside_recovery) and `api/routes/strategy_registry.py` (three GET routes).
* `api/server.py`: two additive lines (import + `include_router`). Nothing else in `setups/`, `proposals/`, `safety/`, `execution/`, `account_refresh/`,
  `cash_flow/`, `forward_validation/`, `trader_view/`, `database/`, `scripts/`, `config.py` or any earlier route changes.
* No new poll: the shell reads the registry and the frozen controls once per page (a failed read is read again at most once a minute) and the terminal
  state on demand (Why? open, 10 s cache).

## 11. Tests, harness, freeze

* `tests/frontend/product_flow.test.cjs` (13): four sections, legacy links, MORE groups, strategy display, blocker surface, priority order (degraded
  outranks WATCHING), READY as eligibility, deterministic WHY, session combinations, position lifecycle, activity dedupe / filters / Daily Review, the
  registry rendering (placeholders never active), the read-only wiring.
* `tests/test_product_flow_612.py`: W wiring and wording, A the GET-only API on the Stage 6.0 fake stack, R the registry model and the Strategy
  Standard v1 validation (INTERNAL and QUANTCONNECT sources, lifecycle statuses, execution fields rejected, placeholders never emit), D the Downside
  Recovery sections from real constants, N the node tests, H the harness flows, C the CI manifest, F the freeze (every tracked base file identical
  except the listed wiring / reconciliation files; `frozen_controls()` hash unchanged; the exact in-place edits of the reconciled tests), Z the docs.
* Browser harness flows after `product_ui`: `product_flow` (A), `product_flow_degraded` (B), `product_flow_lab` (C), `product_flow_links` (D),
  `product_flow_ready` (E), `product_flow_session` (F). No JS error, no order intent, no broker request.
* Reconciled in place (pinned by exact edits): `tests/test_product_ui_611.py` (new files, `api/server.py` additive rule, the GET allowlist, the FLOWS
  string and helper count, the Stage 6.1 test replay), `tests/test_forward_validation_61.py` (the new files), `tests/frontend/product_ui.test.cjs`
  (MORE in three groups, the Daily Review strip, the GET allowlist, the session line's `TRADING STOPPED`), the `product_ui` harness flow (MORE groups,
  the Today strip), `browser_tests/flows.py` POLLING (the shell's two one-off reads).

## 12. Out of scope

Opportunity Ranking across strategies, a START TRADING SESSION endpoint, QuantConnect / LEAN integration, new strategies' logic, a decision table,
market regime. Nothing about Paper Auto, the gate, the executor, reconciliation or the broker changed.
