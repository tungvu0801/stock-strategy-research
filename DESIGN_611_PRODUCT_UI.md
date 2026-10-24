# Stage 6.1.1 — Product UI simplification (DESIGN)

Base: `origin/main` ec87dc6 (Stage 6.1 merged). Branch `stage-611-product-ui-simplification`.

**Display only.** Stage 6.1.1 changes what a person sees first, not what the system does. Strategy logic, setup rules, proposal logic, risk logic,
execution logic, broker integration, the database schema, the scheduler, the Alpaca behaviour, the Paper Auto semantics, the safety behaviour, the
Forward Validation calculations and every API contract are byte-identical to the Stage 6.1 base (`tests/test_product_ui_611.py` F01/F02 prove it
against the base commit, file by file and through `forward_validation.frozen_controls()`). No real order is placed by anything in this stage.

## 1. Objective

The dashboard had thirteen top-level tabs and the Trader View had nine detail panels in one column. A beginner could not tell within seconds what the
Agent was doing, whether trading was allowed, whether a paper position was open, or whether anything needed attention. Stage 6.1.1 gives the product
four primary sections and one state vocabulary, keeps every earlier workspace reachable, and never hides a safety problem.

| Section | Default content | Advanced content |
|---|---|---|
| **TRADE** (default) | chart + watchlist (Trader View), header status bar, Agent card, Market condition card, Risk card with `[ STOP TRADING ]`, Current position card, Paper Auto card | **View details** opens the Stage 5.5–6.1 detail column unchanged: PPE, Market Demand, Setup, Trade Review, Evidence, Position sources, Risk Controls, Paper Bot, Paper Trade, Forward Paper Validation, Actions (the disabled Prepare Buy / Sell / Add Alert placeholders) |
| **POSITIONS** | one card per open or pending paper position; "No open paper position" otherwise | click a card: setup state, demand, evidence, exit monitoring, risk state, position state, invalidation level, broker orders / fills, recent events; closed paper trades under a fold |
| **ACTIVITY** | "Today" strip (trades, open, closed, paper P/L, issues) + timeline of persisted events + compact Validation block | the Stage 5.8 rule / setup alert lists under a fold; `forward_validation.html` for the full Forward Paper Validation report |
| **MORE** | Research / Tools / System / Settings groups | every legacy workspace: Strategy Lab (+ its workspaces), Research Setups, Research History, AI Agent, Market dashboard, Watchlist editor, Daily Review, Trader Review, Forward Validation, Account / execution health, Risk Controls, Portfolio (Robinhood, read only), Advanced configuration |

## 2. The user-facing state model (mapping, not a new state machine)

The backend states are untouched; the shell renames them for display. `agentModel(card, setupState, safetyState, exec)` in `frontend/product_ui.js`:

| Shown | From | Second line |
|---|---|---|
| **WATCHING** | Stage 6.0 card key `WATCHING`, or key `NONE` with the Stage 5.8 setup in `NONE` / `WATCH` / `INVALIDATED` | Waiting for setup conditions |
| **READY** | card key `READY`, or key `NONE` with the setup `READY` | Eligible for entry |
| **ENTERING** | card key `ENTERING` | Order submitted / awaiting fill (or the card's own line) |
| **IN TRADE** | card key `IN_TRADE` | Monitoring position (or the card's own line) |
| **EXITING** | card key `EXITING` | Exit required / order submitted (or the card's own line) |
| **CLOSED** | card key `CLOSED` | Trade completed |
| **NEEDS ATTENTION** | card key `ATTENTION` or `STOPPED`; the bot's `attention` flag (first reason shown); the safety gate `HALTED` / `UNKNOWN` while no trade is live | Manual review required / Trading stopped by risk controls / the reason |

A live trade (`ENTERING` / `IN_TRADE` / `EXITING`) keeps its state under a halted gate: the position is still monitored and exits still run, exactly as
Stage 6.0 defines. Nothing in the browser decides a state; it only maps the persisted one.

**Header status bar** (`statusModel`): `PAPER` or `SIMULATED PAPER` (bot environment) · `MARKET OPEN / CLOSED` (scheduler market) · `ACCOUNT FRESH /
STALE / CHECKING / UNAVAILABLE` (Stage 6.0.1 freshness) · `TRADING ALLOWED / NEW TRADES PAUSED / TRADING STOPPED / RISK STATUS UNKNOWN` (Stage 5.9.1 gate) ·
`AUTO ON / OFF` (persisted mode), plus the policy line **"Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled."**
A missing value is a dash. A problem is always shown.

**Risk card** (`riskModel`): the gate's state in words and its reason, account freshness, cash-flow freshness (Stage 6.0.2), reconciliation health,
every attention reason; level `ok / warn / bad`. **Current position** (`positionModel`): shares, entry, current, P/L (and %), `Day N / max hold` (the
`max_hold_sessions` limit is read from `GET /api/execution`, never hard-coded), status, exit / attention reason. **Market condition** (`marketModel`):
PPE stretch, demand score, stored evidence strength, setup state and the review result, read from the already-rendered Stage 5.6–5.9 panels.

## 3. Actions: delegation, not re-implementation

* `[ STOP TRADING ]` clicks the Risk Controls button `[data-safety="stop"]`: the Stage 5.9.1 emergency stop, its POST, its persistence and its
  two-click clear (under View details) are unchanged. The card's button mirrors the legacy button's availability and reads `TRADING STOPPED` when halted.
* The **Paper Auto** card's button clicks the Paper Bot button `[data-bot="enable"]` / `[data-bot="disable"]`. The first click arms the Stage 6.0
  confirmation (the legacy label becomes "Confirm: enable Paper Auto" and the card mirrors it); only the second click posts. Nothing is posted by the
  shell itself (`'method: "POST"'` does not occur in `product_ui.js`; the node and pytest tests assert it).
* Prepare Buy / Prepare Sell / Add Alert are not promoted: they stay the disabled placeholders of the Actions panel under View details.

## 4. Data flow

The shell adds **no poll of its own**. Every two seconds it re-reads `window.PaperBot.state.data` (the Stage 6.0 `/api/execution/status` payload the
Paper Bot panel already polls), `window.RiskControls.state.data` (the Stage 5.9.1 safety view) and `window.TraderView.state` (active ticker), plus the
`data-state` hooks of the rendered panels. POSITIONS and ACTIVITY fetch only while open: `GET /api/execution/positions`, `/api/execution/intents`,
`/api/execution/findings`, `/api/setups/events`, `/api/proposals/history`, `/api/execution-safety/events`, `/api/forward-validation`,
`/api/forward-validation/events`; MORE › health reads `GET /api/account-refresh`; a position's details read `GET /api/terminal/state/{symbol}`. All GET,
all existing routes. The ACTIVITY timeline is built only from persisted rows (setup transitions, proposals and their risk-check result, order
submissions / fills / rejections, position opened / exit required / closed / attention, safety-gate events, blocking reconciliation findings, Forward
Validation events); rows without a timestamp produce nothing, informational rows (`FILL_UPDATED`, `SAMPLE`, `ORDER_ADOPTED`) are not shown. The
Validation block reuses `ForwardValidation.summaryModel` (Stage 6.1) so the numbers are the panel's and the page's.

## 5. Layering over the pinned page

`frontend/index.html` gains two marked, additive blocks (a stylesheet after the Stage 6.1 one, a script after the Stage 6.1 one, last on the page);
its markup is otherwise byte-identical to the base, so the Stage 5.6.1 / 6.0 / 6.0.2 / 6.1 line rules keep holding once the blocks are stripped. The
block markers deliberately do not begin with the Stage 6.1 marker text (a `6.1` word-boundary strip would otherwise swallow the page between a 6.1.1
opener and the next 6.1 closer).

`frontend/product_ui.css` + `frontend/product_ui.js` (`window.ProductUI`, also a CommonJS module for the node tests):

* `body.pui` hides the legacy tab bar, the stale disclaimer ("This tool never places a trade."), the two "Not available in this version" placeholders
  and the Portfolio note that predates paper execution; the status bar and the four-button nav are inserted around the hidden tab bar; the TRADE cards
  are inserted above the Trader View grid; POSITIONS / ACTIVITY / MORE render into the legacy `paper` / `alerts` / `settings` panels (the alert lists
  move into a fold), so the legacy one-panel-at-a-time logic in `app.js` keeps working and every legacy tab button still works when clicked.
* `pui-collapsed` (default; remembered per browser in `localStorage`) hides the detail column and widens the chart; **View details** restores the
  Stage 5.5 three-column layout, with the cards shrinking to a one-line strip so PPE Stretch and the Actions stay in view at 1280×720.
* A legacy workspace opened from MORE keeps MORE highlighted and shows a crumb with "Back to MORE".
* A deep link wins over the TRADE default: the Daily Brief notification opens `/#daily-brief`, which `brief_delivery.js` consumes by switching to
  Strategy Lab › Daily Brief before the shell mounts; when a hash is present or a tab other than the dashboard is already active, the shell only
  syncs its nav (MORE + crumb) instead of opening TRADE (harness flow `notification_click`).
* The Portfolio tab shows a current note: Robinhood holdings are read only; paper orders go to the Alpaca PAPER account only. Robinhood is reachable
  only through MORE › System.

## 6. Tests and proofs

* `tests/frontend/product_ui.test.cjs` (13): the four sections and their hosts, MORE covers every non-host legacy tab, the state mapping table, the
  status bar for fresh / stale / halted / missing data, the risk and position summaries, the cards and the delegation labels, the timeline from
  persisted rows only, the Today summary, the ACTIVITY and POSITIONS views, the wording, the read-only wiring.
* `tests/test_product_ui_611.py`: W wiring and wording; N the node tests; H the harness flow and the detail-column helper; C the CI manifest; F the
  freeze — every tracked base file identical except the eight wiring / reconciliation files, `frozen_controls()` hash and explicit values, the
  Forward Validation calculations, the four reconciled prior-stage tests (one marked block each in 561 / 60 / 602; six exact in-place edits in the
  Stage 6.1 test) and the marker carrier scans; D this document and the README.
* Browser harness flow `product_ui` (after `forward_validation`, before the clock-moving flows): opens on TRADE with the four-button nav and the
  hidden legacy bar; status bar vs. the live payloads; ticker select; Agent / Risk / Paper Auto cards vs. the backend state; the first Paper Auto click
  arms the confirmation without a request; POSITIONS vs. the live cards; ACTIVITY timeline kinds and the Validation block vs. `GET /api/forward-validation`;
  MORE opens Strategy Lab and the AI Agent; no stale wording on any screen; 1280×720 collapsed and expanded (PPE Stretch in view, placeholders still
  disabled, ≥ 11 px text, no overflow); read-only proof (no intent, no broker request, no mode change, no JS error). The nine earlier Trader View
  detail flows call `_tv_details(c)` right after installing their helpers: they open the detail column the way a user does and keep every one of
  their assertions.

## 7. Reconciliation of the pinned tests

| File | Change | Pinned by |
|---|---|---|
| `tests/test_trader_view_561.py` | one marked Stage 6.1.1 block: strip the Product UI blocks of `index.html` before the Stage 5.6.1 line rule | F03 (base + block) |
| `tests/test_execution_60.py` | one marked block: same, before the Stage 6.0 line rule | F03 |
| `tests/test_cash_flow_602.py` | one marked block: `now()` strips the Product UI blocks from `index.html` before the Stage 6.1 / 6.0.2 comparisons | F03 |
| `tests/test_forward_validation_61.py` | six exact in-place edits: the Stage 6.1.1 strip / new-file constants, the two `index.html` comparisons strip the Product UI blocks first, the new files and this reconciled file are allowed, the reconciled-test counts strip Stage 6.1.1 blocks first | F03 (base + the six replacements) |

The Stage 6.1 strips (`Stage 6.1` with a word boundary) match the Stage 6.1.1 Python blocks transparently, so the Stage 6.0.2 / 6.0.3 / 6.1 pins of
those files keep passing; the Stage 6.1 test counts its own blocks after stripping the Stage 6.1.1 ones.

## 8. Out of scope / unchanged

No route, schema, scheduler, broker, strategy, risk or execution change. No new poll. No model call. No Robinhood call. No real order. The Stage 6.0 /
6.1 runner (`scripts/start_stock_agent.ps1`) is unchanged; the preview of this stage runs the same runner from the stage worktree on a separate port with
every background loop off.
