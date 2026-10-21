# Stage 5.6.1 — Trader View manual visual review (after the polish)

Method: the dev server of this worktree (`TRADER_DATA_MODE=simulated`, port 8012) driven by headless Edge over CDP, the same way the
audit was taken (`AUDIT_561.md`). Every step below was executed against the real page; screenshots live in the session scratchpad.
Two PPE states that the simulator does not produce today were shown by rendering a display fixture through `TraderRender.ppeHtml`
into the panel — marked FIXTURE — and the live state was restored afterwards. No JS error, no console error, no 5xx from
`/api/terminal` during the review.

| # | Step | Result |
|---|------|--------|
| 1 | Boot: first watchlist ticker (NVDA) shown, pill CONNECTED, badge SIMULATED DATA in the top bar | PASS |
| 2 | Ticker search: `/` focuses the search; `aapl` + Enter opens AAPL (symbol + price + change in the top bar) | PASS |
| 3 | Normal PPE (AAPL, +16.9 NORMAL): Stretch block + band, Confidence `—` + EVIDENCE NOT AVAILABLE, explicit PPE EVIDENCE NOT AVAILABLE line, coverage 100 % | PASS |
| 4 | No position: explicit NO POSITION, the three sources with their status tags, explanations under "Why each source shows this" | PASS |
| 5 | Missing evidence: EVIDENCE NOT LOADED + the stored-evidence hint | PASS |
| 6 | Watchlist click (SNDK): all three panels show LOADING · SNDK first, then SNDK data; SNDK row highlighted in watchlist and recent | PASS |
| 7 | Back → AAPL, Forward → SNDK; panels and highlighted rows follow | PASS |
| 8 | Rapid AAPL → NVDA → SNDK → MSFT → JPM: subscription, PPE, position and evidence all JPM | PASS |
| 9 | Stretched PPE (NVDA, +58.7 UPSIDE STRETCHED): band shown, value in muted blue (`rgb(158, 203, 255)`), not the price green | PASS |
| 10 | Reconnect (socket dropped): pill RECONNECTING, values dimmed, STALE tag on the quote; then CONNECTED with exactly the NVDA subscription, attempt counter 0 | PASS |
| 11 | Switching to the ticker already shown: no loading flash, no re-subscription | PASS |
| 12 | FIXTURE extreme downside: −82.0 EXTREME DOWNSIDE (muted orange, tinted block), Confidence 38 LOW (neutral), drivers, evidence WEAK / MIXED | PASS |
| 13 | FIXTURE insufficient data: PPE INSUFFICIENT DATA, both values `—`, coverage 40 %, 60 % floor explained | PASS |
| 14 | Live state restored after the fixtures | PASS |
| 15 | 1440x900: no overflow, chart 874x568, PPE Stretch and the actions in view without page scroll | PASS |
| 16 | 1366x768: no overflow, chart 800x436, PPE Stretch and the actions in view | PASS |
| 17 | 1280x720: no overflow, chart 714x378, PPE Stretch and the actions in view | PASS |
| 18 | No JS / console errors; no `/api/terminal` 5xx | PASS |

Before → after (from the audit):

* Top bar: symbol 20 px among pills → **symbol 24 px + price 24 px + change pill** together; `♥ hh:mm:ss` pill → heartbeat time in the
  quote row; `Connected` 11 px → `● CONNECTED` / `RECONNECTING · try n` / `DISCONNECTED` 12 px bold; `SIMULATED DATA` outline pill →
  filled amber badge (real mode: `ALPACA MARKET DATA`, blue).
* Centre: fixed 380 px chart → chart fills the column (748 px at 1080p, 378 px at 720p); raw enum status line → plain-word chips.
* Right panel: position first with full sentences → NO POSITION + three one-line sources (details on demand); PPE `NOT AVAILABLE` in
  18 px amber → two separate blocks with `—` and a band label, explicit state lines, visible explainer, disclaimer, "Data used" details;
  evidence NOT LOADED → EVIDENCE NOT LOADED; actions → compact sticky footer, Prepare Buy / Prepare Sell / Add Alert are `disabled`
  buttons that say "disabled" (the reason is the tooltip).
* Switching: the old ticker's PPE no longer shows under the new symbol — every panel shows LOADING · <new ticker> until its own data arrives.
* Browser harness: 385 → 438 checks (new `trader_view` flow, 53 checks, inserted before the clock-moving flows; pinned order unchanged).
