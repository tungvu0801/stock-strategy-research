# Stage 5.7 — Market Demand manual validation (live Trader View, simulator)

Method: the dev server of this worktree (`TRADER_DATA_MODE=simulated`, port 8013) driven by headless Edge over CDP
(`browser_tests/cdp.py`), the same way the Stage 5.6.1 review was done. Six tickers: AAPL, NVDA, SNDK, MSFT, JPM and XOM (non-tech).
No broker call, no model call, no research run; no JS / console errors and no 5xx from `/api/terminal` during the run.

| # | Check | Result |
|---|-------|--------|
| 1 | Each switch paints LOADING DEMAND · <ticker> at once (never the previous ticker's score), then that ticker's result | PASS for all six |
| 2 | Demand differs across symbols (AAPL 61 · NVDA 55 · SNDK 29 · MSFT 73 · JPM 83 · XOM 49 on the simulator run) | PASS |
| 3 | Demand stays separate from PPE: own section (MARKET DEMAND heading), own score, no PPE element inside it and nothing of it inside PPE; PPE Stretch unchanged next to it | PASS |
| 4 | Mode chip INTRADAY · SESSION BARS (the simulator session has 240 completed bars), SIMULATED DATA tag on the card, benchmark chip (vs XLK / SOXX / XLF / XLE) | PASS |
| 5 | Visible stamp "bars through hh:mm UTC · session in progress · n bars · computed hh:mm:ss UTC" plus "Data used" details (source string, benchmark, bars through, session date, computed) | PASS |
| 6 | Exact disclaimer visible under every result | PASS |
| 7 | Rapid AAPL → NVDA → SNDK → MSFT → JPM → XOM: Demand, PPE, position and evidence all XOM; Back / Back → MSFT with MSFT's demand (same score as before) | PASS |
| 8 | No position (NO POSITION state) and missing evidence (EVIDENCE NOT LOADED) do not block Demand | PASS |
| 9 | Prepare Buy / Prepare Sell / Add Alert remain disabled buttons next to Demand; Research enabled (stored evidence only) | PASS |
| 10 | No action language in the Demand section | PASS |
| 11 | 1920x1080: Demand score in view without scrolling the right panel; 1280x720: Demand reachable inside the right panel, no page overflow | PASS |
| 12 | No JS / console errors; no `/api/terminal` 5xx | PASS |

Observed key factors (simulator): "holding above VWAP (10+ bars), reclaim held", "+5.8% vs XLF (session)", "below VWAP for 10+ bars,
reclaim rejected", "price in the bottom of the session so far range (9%) on a down move", "relative volume 1.4x with price above VWAP".
Simulator scores are SIMULATED_DATA and prove the display path only — they are not market readings. The real Alpaca minute-bar path
was not exercised on this machine (no market-data credentials).
