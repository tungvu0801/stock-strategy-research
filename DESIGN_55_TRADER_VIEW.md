# Stage 5.5 — Trader View (read-only integration, V1)

**Goal.** Port the Trader View proof-of-concept (TradingView-style terminal) into Stock Agent as an additive dashboard tab, backed by
EXISTING read-only data where it exists, with the deterministic simulator as the explicit fallback. **No execution:** every action is
preview-only, no order endpoint is reachable from the page, nothing is auto-traded, no model is called, no evidence is built.

## 1. Architecture

```
frontend/index.html            + "Trader View" tab (additive), vendored chart library, trader_core.js, trader_view.js, trader_view.css
frontend/trader_core.js        pure logic shared with node tests: NavigationHistory · acceptsMessage · SubscriptionManager · nextBackoff
frontend/trader_view.js        the tab: WebSocket client, chart, watchlist, Back/Forward, position / PPE / evidence panels, actions
frontend/vendor/lightweight-charts/   TradingView Lightweight Charts 4.2.3 (Apache-2.0), pinned by SHA-256 — no CDN at runtime

api/routes/trader_view.py      GET /config · /symbols · /state/{symbol} · /candles/{symbol}   +   WS /api/terminal/stream   (no POST)
trader_view/stream.py          TerminalStream: one subscription per connection, bounded polling loop, normalised messages
trader_view/hub.py             Hub (subscribe replaces), accepts_message (stale guard), NavigationHistory (Python mirror)
trader_view/adapters.py        MarketDataAdapter · PositionAdapter · ResearchAdapter · ExecutionPreviewAdapter (+ null adapters, mode)
trader_view/simulated.py       SimulatedMarket — seeded per symbol, any valid ticker, 1-minute candles, 240-minute backfill
trader_view/market_real.py     RealMarketData — EXISTING Alpaca market-data client (data.market_data), snapshots + minute bars, cached
trader_view/positions.py       StockAgentPositionAdapter — Alpaca Paper View · Local Paper Portfolio · Robinhood Read Only (per source)
trader_view/research.py        EvidenceResearchAdapter — latest STORED Stage 5.4 packet (read only)
trader_view/models.py          explicit response models (TerminalState …) — the browser never sees a broker payload
config.py                      TRADER_DATA_MODE (auto | real | simulated), cadences, history length, demo symbols
```

Reused as-is: `api.validation.validate_symbol`, `scanner.watchlist.load_watchlist` (watchlist.txt), `data.market_data.get_data_client`,
`paper.alpaca_view.status/view` (0 broker calls), `paper.portfolio.view`, `api.routes.portfolio.load_view` + `portfolio.provider`
(only when `PORTFOLIO_AWARENESS_ENABLED`), `evidence.store.EvidenceStore`, the Strategy Lab workspace `evidence54` for the Research
button, the existing StaticFiles mount and tab mechanics.

## 2. Terminal state (explicit contract)

`GET /api/terminal/state/NVDA` → `{label, symbol, quote, candles, position{sources[]}, evidence, ppe, tradability, data_status, actions, market_state}`.
The same object is pushed as the `state` message right after `subscribed`. `ppe` is `{status: NOT_AVAILABLE, stretch: null, confidence: null, contract, note}` —
nothing is fabricated; Stage 5.6 fills the contract. `tradability` is `UNKNOWN` (real) or `SIMULATED`: no read-only asset-metadata source
exists and none is inferred. Position sources are shown separately and never summed.

## 3. Market data

`TRADER_DATA_MODE=auto` (default) → real when Alpaca market-data credentials are configured, else simulated; `simulated` and `real` force
a mode. Stock Agent's market data is polling, not streaming, so the stream polls at a safe cadence only while a browser holds a
subscription: one batched snapshot (price, bid/ask, current minute bar, previous close, day volume) every 3 s for all active symbols
and the watchlist every 10 s; minute bars on subscribe (cached 60 s); positions every 30 s (cached); evidence on subscribe / refresh only.
A feed error is reported as UNAVAILABLE / FEED_ERROR, a symbol without data as NO_DATA → 404 UNKNOWN_SYMBOL. Nothing is polled when no
tab is open; the page unsubscribes when the user leaves the tab.

## 4. Ticker switching

validate → normalise → `unsubscribe(old)` + `subscribe(new)` (server: Hub.subscribe REPLACES, so one stream per connection) → cached
history paints immediately → `state` (quote, position, evidence, PPE, tradability) + `candles` arrive → live `quote` / `candle` updates.
The page drops any symbol-bound message not carrying the active ticker; the server never delivers a loaded state for a symbol the
connection no longer holds. Back / Forward replay the history without recording. Stage 5.4 research is never run on a switch.

## 5. Safety boundary (tests/test_trader_view_55.py)

No POST/PUT/PATCH/DELETE under `/api/terminal`; no route word order/execute/broker/place/cancel/submit; the only client commands are
subscribe / unsubscribe / ping / refresh; the page never names an order endpoint; `trader_view/` imports no order writer, trading client,
Robinhood client, agent, model or evidence engine; a model-client tripwire stays silent through a full flow; the chart asset is local
and hash-pinned; prior-stage modules are byte-identical to the base commit; the full suite stays green.
