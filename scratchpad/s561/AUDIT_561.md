# Stage 5.6.1 — Trader View UI audit (before any change)

Base: origin/main `24ae7d5` (Stage 5.6 merged). Method: the dev server of this worktree (`TRADER_DATA_MODE=simulated`, port 8012) driven by
headless Edge over CDP (`browser_tests/cdp.py`); AAPL, NVDA, SNDK, MSFT, JPM at 1920x1080, then MSFT at 1440x900, 1366x768, 1280x720.
Screenshots and the raw text dump live in the session scratchpad (`s561_audit/audit_before.json`), not in the repo. No JS or console errors;
the two 503s seen are the Dashboard tab's `/api/market/overview` and `/api/watchlist` on an empty development database (not Trader View).

## Top bar
* Symbol is 20 px and sits between the search form and the mode pill; the price lives in the centre panel, so the two most important
  facts (symbol, price + change) are not read together.
* `♥ 16:09:03` heartbeat pill and `Connected` pill are 11 px and far right; "Disconnected — reconnecting" is the only reconnect wording.
* `SIMULATED DATA` pill is present but small (11 px, amber outline); the real-data label would read `LIVE · POLLED (read only)` — it does
  not name the source.

## Left panel
* Watchlist is compact and readable (ticker / price / %); the active row is outlined, but the heading
  `WATCHLIST (WATCHLIST.TXT · SIMULATED QUOTES)` wraps onto two lines and reads like a debug label.
* When the active ticker is in both lists, two rows highlight (watchlist + recent); when it is not in the watchlist only "Recent" shows it.

## Centre
* Price 26 px, change 15 px, then Bid / Ask / 1-minute OHLC / "as of" in one dim line — fine, but the OHLC string is dense.
* Chart is a fixed 380 px: at 1366x768 and 1280x720 the PPE section of the right panel is below the fold and the whole page scrolls.
* Status line `Mode SIMULATED · Market SIMULATED · Position OK · Evidence NOT_LOADED · Tradability SIMULATED · PPE SIMULATED_DATA · …`
  is raw enum metadata with underscores.

## Right panel
* POSITION comes first and spends ~230 px on three "not configured / not set up / disabled" sources with full sentences; there is no
  explicit NO POSITION state.
* PPE: `NOT AVAILABLE` in 18 px amber wraps to two lines and the band label `EVIDENCE NOT AVAILABLE` wraps to three; Stretch is coloured
  green (> +40) / red (< −40) exactly like a buy / sell cue; Stretch and Confidence share one visual weight; the evidence line duplicates
  the Evidence panel; the source line (10 px) is debug metadata; the explainer for Confidence does not exist; the disclaimer is present
  (11 px italic) and visible.
* On a ticker switch the PPE panel keeps the PREVIOUS ticker's result until the new state arrives (position / evidence show "Loading…",
  PPE does not) — the old ticker's PPE is briefly shown under the new symbol.
* EVIDENCE: `NOT LOADED` + two sentences; LOADED shows a 6-row table (fine).
* ACTIONS: 2x2 buttons; Prepare Buy / Prepare Sell are dimmed + dashed with `aria-disabled` but are real clickable buttons (click = toast);
  the 10 px status text wraps.

## States
* No loading skeleton for PPE; quote shows "…" while pending; FEED UNAVAILABLE / INVALID SYMBOL only reach the user as a toast + "UNAVAILABLE".
* Disconnected: values stay fully bright (no stale treatment).
* Sizes: no horizontal overflow at any of the four sizes; chart width 1318 / 838 / 764 / 678 px; nothing clipped; smallest fonts 10 px.

## Verdict (what Stage 5.6.1 changes — UI only)
Hierarchy (symbol + price in the top bar, chart next, source + connection unmistakable), a PPE card with Stretch and Confidence as two
separate, neutral-coloured blocks, explicit empty / loading / error states, stale treatment while not connected, compact position /
evidence / actions, semantic `disabled` on Prepare Buy / Sell, responsive heights, focus styles, ≥ 11 px text, and the README fixes.
No backend, PPE, evidence, market-data or execution semantics change.
