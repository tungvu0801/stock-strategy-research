# Stock Monitoring Agent

A Python agent that monitors the overall U.S. stock market and your own
watchlist, and shows descriptive, rule-based signals (momentum, unusual
volume, breakout watch, overbought/oversold, etc.) — both as a terminal
report and as a local web dashboard.

**This is an analysis and research tool only. It never places, modifies, or
cancels trades.** It only ever imports Alpaca's *market data* client, not
the trading client — there is no code path in this project capable of
submitting an order.

## What it does

- **Market scanner** — asks Alpaca's screener endpoints for the day's top
  gainers, top losers, and most-active-by-volume stocks (a bounded list,
  not a download of the entire market), then computes indicators for that
  list and buckets the results into Top Gainers / Top Losers / Unusual
  Volume / Momentum Stocks / Possible Breakouts / Approaching Support /
  Approaching Resistance / Volatility Expansion.
- **Watchlist** — reads tickers from `watchlist.txt` and shows a detailed
  breakdown for each one: price, % change, volume, relative volume, RSI,
  EMA 9/20/50, ATR, support/resistance, multi-day momentum, trend,
  volatility, and a rule-based signal.
- **Signal engine** — transparent, threshold-based rules (defined at the
  top of `analysis/signals.py`) classify each stock as things like `STRONG
  MOMENTUM`, `UNUSUAL VOLUME`, `BREAKOUT WATCH`, `OVERSOLD`, `OVERBOUGHT`,
  `LARGE GAIN`, `LARGE DROP`, `PULLBACK`, or `NORMAL`. Nothing is predicted
  by a model — every signal is a description of data that already happened,
  and it's deliberately not a buy/sell instruction (see Disclaimer below).
- **Scores** — 0-100 descriptive scores for momentum, relative volume,
  trend strength, and volatility (weights configurable in `config.py`),
  plus an overall **Attention Score** that measures how unusual/interesting
  a stock's activity looks right now. The Attention Score is explicitly
  **not** a buy/sell rating or an investment recommendation.
- **AI Technical Agent (optional)** — Claude sits *above* the quant engine,
  not in place of it: Alpaca + Python compute every factual value (price,
  volume, RSI, EMA, ATR, support/resistance, Attention Score, the
  rule-based signal), and the agent only interprets that structured data —
  it's instructed not to substitute its own knowledge for any of it. Output
  is five labeled sections (Technical Structure, Volume, Levels,
  Volatility, Conflicts); it never issues a buy/sell/hold/trim verdict.
  Claude is only called for stocks whose Attention Score clears
  `AI_ANALYSIS_ATTENTION_THRESHOLD`, results are cached
  (`AI_ANALYSIS_CACHE_MINUTES`, re-triggered early by a material price or
  Attention Score move) so an unchanged stock isn't re-analyzed, and hard
  `AI_MAX_CALLS_PER_HOUR`/`AI_MAX_CALLS_PER_DAY` ceilings cap spend
  regardless of caching. Every call's tokens/cost are logged
  (`GET /api/ai/usage`). Works with no `ANTHROPIC_API_KEY` configured too —
  it just says so instead of failing, and the rest of the app is unaffected
  by any Claude outage, rate limit, or malformed response.
- **Web dashboard** — a local FastAPI backend + a dependency-free HTML/JS
  frontend, styled as a dark trading-terminal. See "Web dashboard" below.
- **Alerts** — notable signals surface both in the terminal and via
  `GET /api/alerts`. The alert system (`alerts/alert_manager.py`) is built
  around a small `AlertChannel` interface so Discord/email/desktop
  notifications can be plugged in later without changing the rest of the agent.

## Project structure

```
stock-agent/
├── main.py                  CLI entry point (terminal report / --loop / --sync-robinhood)
├── config.py                All editable settings (intervals, filters, weights, thresholds, versions)
├── analysis/
│   ├── indicators.py        Core math (EMA, RSI, ATR, volatility) + TickerMetrics + compute_metrics
│   ├── levels.py            20-day high/low, support/resistance swing detection
│   ├── signals.py           Signal thresholds + the rule-based signal engine
│   ├── scoring.py           0-100 descriptive scores + the Attention Score
│   ├── sector_context.py    Real sector/market performance vs. peers (live Alpaca bars)
│   ├── risk_flags.py        Deterministic risk-flag detection + the Risk evidence-category score
│   ├── evidence_scoring.py  The bullish/neutral/bearish evidence engine (pure Python, no Claude)
│   ├── research_view.py     Deterministic Research View label from the evidence engine's net score
│   ├── research_setup.py    Entry/invalidation/target/risk-reward arithmetic
│   └── position_sizing.py   Deterministic position-sizing calculator (the "Risk Manager")
├── scanner/
│   ├── market_scanner.py    Whole-market screening (Alpaca screener + batched bars)
│   └── watchlist.py         Load/add/remove watchlist.txt entries + analyze them
├── data/
│   ├── market_data.py       Alpaca market-data client construction + batched bar fetching
│   ├── news.py              Alpaca news feed wrapper (feeds the Catalyst Agent)
│   ├── sector_map.py        Curated sector/peer/ETF mapping (the grouping is static; performance is live)
│   ├── earnings.py          Interface stub (no data source wired up yet)
│   └── events.py            Interface stub (no data source wired up yet)
├── agents/
│   ├── orchestrator.py      Pluggable LLMProvider/LLMResponse interface (not tied to one AI vendor)
│   ├── gating.py            Shared attention-threshold -> cache -> rate-limit -> call pipeline
│   ├── technical_agent.py   Anthropic-backed technical explanation + the tool-use loop implementation
│   ├── catalyst_agent.py    Classifies real Alpaca news by relevance/sentiment
│   ├── risk_agent.py        Explains deterministic risk flags in plain language
│   ├── beginner_agent.py    Combines evidence/catalysts/risks/sector into the 5-section beginner narrative
│   ├── chat_agent.py        Real AI-chat tool-use loop (agents/tools.py)
│   ├── tools.py             The application "tools" Claude can call (real or explicitly stubbed)
│   ├── usage_tracker.py     Token/cost logging + hourly/daily call-limit enforcement
│   └── ai_cache.py          Caches AI results per (symbol, analysis_type)
├── database/
│   ├── database.py          ResearchDatabase repository (all SQL lives here — see Stage 2.5 section)
│   ├── models.py            Plain dataclasses mirroring the SQLite schema
│   ├── migrations.py        CREATE TABLE / index statements (idempotent)
│   └── fingerprint.py       Deterministic snapshot fingerprint for deduplication
├── services/
│   ├── analysis_cache.py    Short-lived cache of already-generated bundles, keyed by analysis_id
│   ├── snapshot_builder.py  Flattens a generated bundle into an immutable database row
│   ├── outcome_tracker.py   Trading-day outcome measurement (never calls Claude)
│   └── analytics.py         Performance-analytics aggregation (never calls Claude)
├── api/
│   ├── server.py            FastAPI app (mounts routes, initializes the database, serves frontend/)
│   ├── cache.py             In-memory TTL cache so the dashboard doesn't over-call Alpaca
│   ├── validation.py        Ticker symbol validation
│   └── routes/              market.py, watchlist.py, stocks.py, research.py, research_history.py, alerts.py, agent.py
├── models/
│   └── schemas.py           Pydantic request/response models for the API
├── frontend/
│   ├── index.html, style.css, app.js   Dashboard UI (plain HTML/JS, no build step)
├── alerts/
│   └── alert_manager.py     Alert evaluation + dispatch (console + API; Discord/email/desktop later)
├── data/stock_agent.db       Local SQLite research history (git-ignored, auto-created)
├── robinhood_sync.py         OPTIONAL: read-only sync of Robinhood holdings into watchlist.txt
├── watchlist.txt              Your editable list of tickers to track
├── .env.example                Template for your Alpaca/Robinhood/Anthropic credentials
├── requirements.txt
├── .gitignore
└── README.md
```

## Installation

1. **Create and activate a virtual environment** (already done for you if
   you're reading this after setup, but for reference):

   Windows (PowerShell):
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   macOS/Linux:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## API setup

1. Create a free Alpaca account at https://alpaca.markets/ and generate an
   API key/secret pair (paper or live — this agent only reads market data,
   it never trades, so either works).
2. Copy the example env file and fill in your keys:
   ```bash
   cp .env.example .env
   ```
3. Edit `.env`:
   ```
   ALPACA_API_KEY=your_key_here
   ALPACA_SECRET_KEY=your_secret_here
   ```

`.env` is listed in `.gitignore` and is never committed. Keys are only
ever read from environment variables via `config.py` — they are not
hard-coded anywhere and are never printed to the terminal or returned by
any API response.

If your Alpaca plan doesn't have access to the SIP data feed, the default
`iex` feed (set in `config.py` via `DATA_FEED`) will work on free/basic
accounts. If Alpaca's screener endpoints aren't available on your plan,
the market scanner automatically falls back to scanning a small, fixed
list of liquid large-cap stocks (`config.FALLBACK_UNIVERSE`) instead.

**Optional — AI Technical Agent:** add `ANTHROPIC_API_KEY=...` to `.env` to
enable plain-English technical explanations on the dashboard. Without it,
those requests just return "AI analysis unavailable — no LLM provider
configured" — every other feature works either way.

## Adding / removing watchlist stocks

Edit `watchlist.txt` directly — one ticker per line. Blank lines and lines
starting with `#` are ignored, so you can leave yourself comments:

```
NVDA
AMD
MU
# SNDK   <- temporarily disabled
AVGO
```

Or use the web dashboard's "Add to Watchlist" box / the × button on a
watchlist card, which call `POST /api/watchlist` / `DELETE
/api/watchlist/{symbol}` and edit the same file. Either way, the file is
re-read on the next scan — no code change needed.

### Syncing from Robinhood (optional)

Robinhood has no official API for market data or account access. If you
want your Robinhood holdings reflected in `watchlist.txt` without typing
them in by hand, `robinhood_sync.py` provides an **optional, read-only**
sync using the unofficial `robin_stocks` library. Before using it, please
read the caveats at the top of that file — in short: it stores your real
Robinhood login in `.env`, it goes outside Robinhood's Terms of Service,
and it can break if Robinhood changes their private endpoints.

To use it:

1. Add your Robinhood login to `.env`:
   ```
   ROBINHOOD_USERNAME=you@example.com
   ROBINHOOD_PASSWORD=your_password
   ```
2. Run the sync (in the foreground — the first run may prompt you to
   approve the login via the Robinhood app or type a verification code):
   ```bash
   python main.py --sync-robinhood
   ```

This writes your current holdings into a clearly marked, auto-managed
block at the bottom of `watchlist.txt`, without touching any tickers or
comments you've added manually elsewhere in the file. Re-run it any time
you want to refresh the list (e.g. after buying/selling something).

This only ever reads your positions — it never places, modifies, or
cancels an order, and it is **not** available in `--loop` mode, and not
wired into the web dashboard, on purpose: a Robinhood login can require
interactive 2FA approval, which must never silently block a background
process. Run `--sync-robinhood` manually whenever you want to refresh the
list, then run the scanner/dashboard separately.

## Running the agent

### Terminal report

Run a single scan (market overview + watchlist) and exit:

```bash
python main.py
```

Run continuously, rescanning on the intervals set in `config.py`
(`MARKET_SCAN_INTERVAL` / `WATCHLIST_SCAN_INTERVAL`) until you press
`Ctrl+C`:

```bash
python main.py --loop
```

### Web dashboard

```bash
uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload
```

Then open **http://127.0.0.1:8000** in a browser. The dashboard shows the
same Market Overview + Watchlist data as the terminal report, plus an "AI
Opportunities" panel sorted by Attention Score, click-through stock detail
(with support/resistance, momentum, and an on-demand AI technical read),
and a "consider also looking at" panel of alternatives when you click a
watchlist stock. It auto-refreshes every 30 seconds by redrawing from a
short-lived server-side cache — it does **not** re-hit Alpaca on every
browser refresh; the cache is keyed to the same `MARKET_SCAN_INTERVAL` /
`WATCHLIST_SCAN_INTERVAL` values the CLI uses.

The frontend is currently plain HTML/CSS/JS (no Node.js/npm required) —
this project's dashboard was originally scoped for a React/TypeScript/Vite
frontend, but Node.js isn't installed on this machine. Everything behind
`/api/*` is a normal JSON API, so swapping in a React frontend later
doesn't require touching the backend.

The terminal (`main.py`) and the web dashboard (`uvicorn api.server:app`)
are independent processes — you can run either or both. Running both at
once roughly doubles how often Alpaca gets called, which is still fine
given the batching, just worth knowing.

## Configuration

Everything tunable lives in `config.py`:

| Setting | Meaning |
|---|---|
| `MARKET_SCAN_INTERVAL` | Seconds between whole-market scans (CLI `--loop`) / cache TTL (dashboard) |
| `WATCHLIST_SCAN_INTERVAL` | Seconds between watchlist scans (CLI `--loop`) / cache TTL (dashboard) |
| `MIN_PRICE` | Minimum price filter for the market scanner (avoids penny stocks) |
| `MIN_AVG_VOLUME` | Minimum average daily volume filter for the market scanner |
| `TOP_RESULTS` | Max rows shown per market-overview category |
| `SCREENER_TOP_N` | How many gainers/losers/actives to request from Alpaca's screener |
| `DATA_FEED` | Alpaca data feed to use (`iex` by default; `sip` if your plan supports it) |
| `RSI_PERIOD`, `EMA_FAST/MEDIUM/SLOW`, `ATR_PERIOD` | Core indicator settings |
| `HIGH_LOW_LOOKBACK_DAYS`, `SUPPORT_RESISTANCE_LOOKBACK_DAYS`, `SWING_WINDOW_DAYS` | Price-level lookback windows |
| `MOMENTUM_5D_DAYS`, `MOMENTUM_10D_DAYS` | Multi-day momentum windows |
| `VOLUME_EXPANSION_SHORT_DAYS/LONG_DAYS`, `VOLATILITY_LOOKBACK_DAYS`, `VOLATILITY_SHORT_LOOKBACK_DAYS` | Expansion-ratio windows |
| `ATTENTION_WEIGHT_MOMENTUM/RVOL/TREND/VOLATILITY` | Attention Score weights |
| `FASTAPI_HOST`, `FASTAPI_PORT`, `WEB_UI_REFRESH_SECONDS` | Dashboard server + browser refresh cadence |
| `ANTHROPIC_MODEL` | Which Claude model the Technical Agent uses (env-overridable) |
| `AI_ANALYSIS_ATTENTION_THRESHOLD` | Minimum Attention Score before a stock is even eligible for AI analysis (default 75) |
| `AI_ANALYSIS_CACHE_MINUTES` | How long a cached AI analysis stays valid before it's eligible for re-analysis |
| `AI_REANALYZE_PRICE_CHANGE_PERCENT`, `AI_REANALYZE_ATTENTION_CHANGE` | Price/Attention-Score moves that force re-analysis even within the cache window |
| `AI_MAX_CALLS_PER_HOUR`, `AI_MAX_CALLS_PER_DAY` | Hard ceilings on Claude calls, independent of caching |
| `AI_MODEL_PRICING` | Per-model $/1M-token pricing used for the cost estimates in `GET /api/ai/usage` |

Signal thresholds (what counts as "overbought", "unusual volume", etc.)
live at the top of `analysis/signals.py` so the rules stay transparent and
easy to audit.

## AI-agent architecture

```
Alpaca -> Python market scanner -> Quant engine -> Attention filter -> Claude reasoning -> Dashboard
```

Alpaca and the Python quant engine (`analysis/`) own every factual number.
Claude never calculates an indicator and is explicitly instructed not to
substitute its own knowledge for any value it's given — it only
interprets the structured JSON `agents/technical_agent.py` hands it (see
`_build_structured_input`), and its system prompt forbids inventing prices,
volume, news, or events.

Claude is called only when useful, gated in this order (see
`agents/technical_agent.get_technical_analysis`):
1. **Attention Score threshold** — below `AI_ANALYSIS_ATTENTION_THRESHOLD`, Claude is never called.
2. **Cache** (`agents/ai_cache.py`) — a fresh result is reused if price/Attention Score haven't moved materially and the cache hasn't expired.
3. **Rate limits** (`agents/usage_tracker.py`) — `AI_MAX_CALLS_PER_HOUR`/`AI_MAX_CALLS_PER_DAY` are hard ceilings checked before every call, independent of caching.

Every call (or skipped/blocked/failed attempt) is logged with tokens and
an estimated cost, viewable at `GET /api/ai/usage`. `agents/tools.py`
defines the application "tools" exposed to Claude — both directly (the
beginner research view) and via the real tool-use loop behind
`POST /api/agent/chat` (`agents/chat_agent.py`) — each tool either wraps a
real data source (market overview, stock metrics, news, sector context,
technical/catalyst/risk analysis) or is explicitly marked unavailable
(`get_earnings`, `get_events` — no real data source exists for those yet);
none ever fabricate data. `agents/catalyst_agent.py` classifies real
Alpaca news by relevance/sentiment; `agents/risk_agent.py` explains
deterministic risk flags computed in `analysis/risk_flags.py` — Claude
never decides which risks apply, only explains ones Python already found.

## Research history, outcome tracking & validation (Stage 2.5)

This is a pure **observation layer** — it never changes any scoring
formula, weight, threshold, or prompt in the research engine above. Its
only job is to answer "was this analysis actually useful?" by freezing
what the agent believed at a moment in time and later measuring, in plain
Python, what really happened.

**Storage.** A local SQLite database at `data/stock_agent.db` (git-ignored,
along with its `-wal`/`-shm` files), accessed only through
`database/database.py`'s `ResearchDatabase` repository class — every
caller elsewhere sees plain Python methods, never raw SQL, so swapping in
PostgreSQL later means rewriting that one file, not the research engine.
Never stores API keys or secrets — verified by grepping the raw database
file for known key substrings.

**Saving a snapshot.** Snapshots are never created automatically or on a
page refresh — only on an explicit action: the "Save Research Snapshot"
button on a stock's research view, the "Save Full Research Snapshot"
button on the Research Setups tab, or asking the AI Agent to save an
analysis. Critically, the dashboard's save button records **exactly** the
analysis you're already looking at — `GET /api/stocks/{symbol}/research`
caches the generated bundle server-side under a short-lived `analysis_id`
(`services/analysis_cache.py`, ~60 min TTL), and saving looks that bundle
up and persists it verbatim, making **zero new Claude calls**. (The AI-chat
save tool and the Setups-tab button are the two exceptions — there's no
prior displayed analysis in those contexts to preserve, so they generate
one fresh and save it in the same step, which is the intended behavior.)
Duplicate saves are prevented by a deterministic fingerprint
(`database/fingerprint.py`) over the substantive fields (symbol, price,
attention score, evidence split, catalysts, risk flags, setup levels) —
not by timestamp, so a double-click or accidental re-save collapses into
the same row instead of spamming the database.

**Outcome tracking.** `POST /api/research/outcomes/update` (no scheduler
yet — run it manually, or automate it later) measures what happened over
1/3/5 **trading days** — real elapsed market sessions counted from actual
daily bars, not calendar days, so weekends/holidays are handled
automatically by simply never producing a bar to count. "N trading days
after" is bar index N−1 in the list of bars strictly after the snapshot's
date. A horizon with too few elapsed sessions stays `PENDING` — it is
never faked. `services/outcome_tracker.py` never calls Claude; every
outcome is deterministic pandas arithmetic over real Alpaca bars.

- **MFE / MAE** (max favorable/adverse excursion) are the best/worst price
  move within the horizon window, always measured relative to the frozen
  snapshot price (not the research setup's entry, which may never trigger).
- **Hit** = an intrabar touch (`low <= level <= high` on any bar). **Break**
  = a *closing* price beyond the level — a wick through support is weaker
  evidence than a session closing below it.
- **Intraday ambiguity**: daily OHLC bars can't prove *within-day* order.
  If a setup's entry and a target/invalidation touch land on the *same*
  daily bar, the outcome records the hit as a fact (it did happen) but
  marks `sequencing_ambiguous: true` and leaves the "after entry" field
  `null` rather than guessing which came first. Order *between* different
  days is always unambiguous (day K+2 strictly follows all of day K)
  regardless of missing intraday granularity.
- A confirmed empty result for a symbol (no bars at all) is stored as
  `ERROR` — a real, likely-recurring data problem. A transient
  network/API failure touches nothing in the database and is only
  reported in that call's response, so a retry can still succeed later.

**Performance analytics** (`GET /api/research/performance`,
`services/analytics.py`) — overall stats, a breakdown by bullish-evidence
bucket (0-39/40-49/.../90-100), by Research View label, and by
catalyst/risk-flag presence, computed from completed outcomes only. Every
group shows its sample size and a warning below 30 observations
(`"Small sample — interpret cautiously."` under 30, `"Very small
sample..."` under 10) — never hidden, never described as strong evidence
regardless of how the numbers look. These are historical observations of
this agent's own saved research, explicitly not a probability of future
returns.

**Viewing an old snapshot** (`GET /api/research/snapshots/{id}`, the
Research History tab's list) shows the frozen "AT THE TIME" data — price,
evidence, catalysts, risks, the beginner narrative, exactly as saved —
and "WHAT HAPPENED AFTERWARD" as a clearly separate section from real
outcome rows. The two are never mixed, and nothing about the original
record changes as new information arrives later.

**Resetting local research history**: stop the server and delete
`data/stock_agent.db` (and any `-wal`/`-shm` files next to it) — the app
recreates an empty database automatically on the next start.

## Event intelligence (Stage 2.6)

Awareness of real upcoming events (earnings, macro releases, FOMC,
corporate actions) that can move a stock independent of its technicals.
Purely additive: event risk feeds the **existing** Risk evidence category
through new deterministic risk-flag codes (`analysis/risk_flags.py`) — no
new evidence category, no change to Technical/Catalyst/Risk/Market/Sector
weights or Research View thresholds.

**Source per event category** (`data/events/`):

| Category | Provider | Real now? |
|---|---|---|
| Corporate actions (splits, dividends, mergers, spin-offs) | Alpaca Corporate Actions API (`data/events/corporate.py`) — same market-data credentials as everything else, never the trading client | Yes |
| FOMC meetings / decisions / minutes | Official Fed calendar page, parsed at runtime (`data/events/fomc.py`) | Yes |
| CPI, PPI, Employment Situation, PCE | FRED release-dates API (`data/events/macro.py`) | Yes, if `FRED_API_KEY` is set — otherwise honestly reports unavailable |
| JOLTS | Same FRED endpoint | Best-effort; unavailable if its release_id doesn't resolve cleanly |
| Earnings (any symbol) | — | **No verified provider exists.** Always reports unavailable with a clear reason — never scraped, inferred from news, or filled from Claude's own knowledge. |

**FOMC minutes honesty rule**: a `FOMC_MINUTES` event is only created when
the Fed's own calendar page states an explicit "(Released ...)" date for
that meeting. It is never derived as "meeting date + 3 weeks" — that's a
well-known convention, but using it to manufacture a date would be a
fabrication. If minutes for a given meeting are still pending, no event is
created for it at all.

**Timezone handling**: every source's raw time is Eastern. Where a source
gives an exact time, it's converted to UTC (`event_datetime_utc`) and the
original zone is preserved separately for display. Where a source gives
only a date (true for every event type wired up in this stage — none of
FOMC/FRED currently state an exact time), the event is stored with
`time_precision=DATE_ONLY` and proximity is computed at UTC-midnight
granularity — never a synthesized time-of-day. Proximity math always takes
an explicit UTC "now" parameter, so results don't depend on the server's
local timezone.

**Event risk formula** (`analysis/event_risk.py`, pure Python, no Claude):
proximity buckets into `NOW` / `WITHIN_24H` / `WITHIN_3D` / `WITHIN_7D` /
`LATER` / `PAST` using `config.EVENT_HIGH_RISK_HOURS` (24) /
`EVENT_MEDIUM_RISK_DAYS` (3) / `EVENT_LOW_RISK_DAYS` (7); severity (`HIGH`/
`MEDIUM`/`LOW`/`NONE`) combines that window with the event's own
`importance` (set by its provider from real source context — FOMC/CPI/jobs
are `HIGH`, a routine dividend is `LOW`). Claude never assigns a window or
severity — it only explains what these functions already decided, and is
explicitly instructed to never call a scheduled macro release bullish or
bearish by default, since its market impact depends on data not yet known.

**Caching**: each provider caches its own fetch in-memory
(`data/events/cache.py`) — 12h for FOMC/macro, 3h for corporate actions —
so a slow-changing calendar isn't re-fetched on every request. A failed
fetch is never cached, so the next request simply retries.

**Data quality levels**: `HIGH` (Alpaca + FRED both configured), `MEDIUM`
(Alpaca configured, no FRED key — the default in this project), `LOW`
(Alpaca itself not configured). Earnings' permanent unavailability is
surfaced as its own explicit `EARNINGS_DATA_UNAVAILABLE` caveat, not folded
into this score.

**Snapshot freezing**: `research_snapshots` gained four additive, nullable
columns (`events_json`, `event_risk_level`, `event_data_quality`,
`event_schema_version`) via a `PRAGMA table_info`-guarded `ALTER TABLE`
(see `database/migrations.py`) — existing rows are never rewritten, and a
pre-Stage-2.6 snapshot reads back `NULL` for all four, forever. A new
snapshot freezes exactly the event context that was displayed at save
time; reading any snapshot back never re-fetches or re-derives events —
old snapshots are never enriched with information discovered later, which
would be lookahead bias.

**Known limitations**: no direct BLS/BEA feed yet (FRED aggregates their
official calendars — a documented future addition, with the
source-priority rule already structural via each event's
`source_provider`); no stale-cache fallback on a mid-window fetch failure
(a failure simply returns no events for that call rather than serving a
possibly-stale cached list); FOMC meeting dates for the *next* calendar
year are marked `confirmed=False` since the Fed itself treats them as
tentative until closer to the year.

## Data notes

- All indicators are computed strictly from historical bars up to the most
  recent one available — nothing is ever computed using future data.
- If there isn't enough price history for an indicator (e.g. a recently
  listed stock with fewer than 50 daily bars for EMA 50, or not enough bars
  to find a swing-based support/resistance level), that field is left blank
  rather than being estimated or fabricated.
- The watchlist tries to show the latest trade price when the market is
  open; if that isn't available it falls back to the most recent daily
  close. Every price is labeled with its source (`latest trade` vs `prior
  daily close`) and a timestamp, so it's always clear whether you're
  looking at real-time or end-of-day data.
- `data/earnings.py` and `data/events.py` are interface stubs — no data
  source is wired up yet, so they always return "no data" rather than
  guessing. `data/news.py` is a real, working wrapper around Alpaca's news
  feed, exposed at `GET /api/stocks/{symbol}/news`.

## Error handling

A single bad ticker, a rate limit, an API outage, or the market being
closed will never crash the whole run — failures are logged and that
symbol (or that scan) is skipped, and the rest of the report/dashboard
still loads. Missing Alpaca credentials produce a clear message (a 503
from the API, or a plain message from the CLI) instead of a stack trace.

## Disclaimer

This tool describes market activity using transparent, rule-based
calculations, optionally explained in plain English by an LLM. It does not
predict future prices, does not recommend buying or selling anything, and
does not execute trades. All scores (including the Attention Score) and
the AI Technical Agent's explanations measure/describe what the data shows
right now — they are not investment advice, and you are responsible for
every trade decision you make.

## Portfolio awareness (Stage 2.7C, read-only, optional)

The **Portfolio** tab shows your Robinhood holdings: value, cash, positions, cost basis, unrealized/realized P&L,
weights, sector exposure (from `data/sector_map.py`; unmapped symbols are shown as `UNCLASSIFIED`), Stage 2.6 event
exposure and quote/data quality, plus arithmetic-only "what if" scenarios and a grounding-checked AI explanation.

- The stock-agent never talks to Robinhood. It reads normalized JSON from the local `rh_gateway` process
  (see `../rh_gateway/README.md`), which alone holds the OAuth credential and full account number.
- Off by default: set `PORTFOLIO_AWARENESS_ENABLED=true` in `.env` and start the gateway. When disabled or when the
  gateway is down, every other feature behaves exactly as before and the tab reports `PORTFOLIO_UNAVAILABLE`.
- Portfolio data never changes evidence scores, Research View, snapshots, outcomes or event scoring, and is never
  written to the database. No order can be placed, prepared or cancelled from this app.
- Portfolio risk thresholds are not defined yet; only raw facts are shown (`PORTFOLIO_RISK_RULES_JSON=[]`).
- Tests: `pip install -r requirements-dev.txt` then `python -m pytest -q tests`.

## Beginner decision support, market context and Trader Review (Stage 2.7E)

- **Dashboard → Beginner** (default): today's market (environment, trend, volatility, risk appetite, difficulty),
  what's driving it (observed data vs sourced news), verified events, watchlist, stocks worth researching, your
  portfolio and what to watch. **Advanced** shows the original dashboard unchanged.
- **Portfolio → Beginner**: your Robinhood account (cost basis ≠ money added; money added is unavailable from the
  verified source), current market, attention flags, positions with saved research and concerns, "What if I add
  money?" (new money by default), Market → Stock → Portfolio, and "When might conditions be better?".
- **Trader Review**: review a hypothetical trade or a held position. It judges the decision PROCESS with the
  information available at the time; outcomes are shown separately as hindsight. No scores, no orders.
- All labels are deterministic (`insights/`), reuse existing Stage 1/2/2.6 outputs, and never change evidence,
  Research View, event scoring, policy arithmetic, snapshots or the database. AI explanations are grounding-checked
  and withheld on invented numbers, order/sizing language, forecasts or unsourced causes.
- Display cutoffs (not trading rules): `BEGINNER_RESEARCH_STALE_HOURS`, `BEGINNER_MOMENTUM_*_SCORE`,
  `BEGINNER_BEARISH_MATERIAL_PCT`, `MARKET_*` (see config.py). Market breadth uses the app's fixed 80-stock list and
  is labelled as such — it is not whole-market breadth.

## Trader View (Stage 5.5, read-only)

A TradingView-style terminal tab (**Trader View**): one active ticker with 1-minute candles, the symbol and price in the top bar, the
existing watchlist with lighter updates, Back / Forward, a read-only position panel per source (Alpaca Paper View, Local Paper
Portfolio, Robinhood Read Only — never combined, with an explicit NO POSITION state), the latest stored Stage 5.4 evidence packet
(EVIDENCE NOT LOADED when none exists), the Stage 5.6 PPE card (Stretch and Confidence as two separate blocks, coverage, key factors,
explicit PPE INSUFFICIENT DATA / PPE EVIDENCE NOT AVAILABLE states, the exact disclaimer — see the next section) and action controls that
are **preview-only**: Prepare Buy / Prepare Sell are disabled buttons with no order path, Research opens the stored evidence
workspace, Add Alert is a placeholder. The data source is always shown in the top bar — `SIMULATED DATA` or `ALPACA MARKET DATA`
(read only) — next to the connection state (CONNECTED / RECONNECTING / DISCONNECTED; shown values are marked STALE while the stream
is down, and a ticker switch shows a loading state for the new ticker instead of the previous ticker's panels). Market data is the
deterministic simulator unless `TRADER_DATA_MODE=auto` finds read-only Alpaca market-data credentials (then it polls at a bounded
cadence only while the tab is open); `TRADER_DATA_MODE=simulated` forces the simulator. The chart library (Lightweight Charts 4.2.3) is
served locally from `frontend/vendor` — no CDN. See `DESIGN_55_TRADER_VIEW.md`; the Stage 5.6.1 UI audit that drove the layout is in
`scratchpad/s561/AUDIT_561.md`, and the pure rendering helpers (`frontend/trader_render.js`) are unit-tested with node.

## PPE engine (Stage 5.6, deterministic, read-only)

Trader View's PPE panel now shows **PPE Stretch** (−100 … +100: how unusually stretched the current move is relative to the stock's
own behaviour and its sector / market context) and **PPE Confidence** (0 … 100: confidence that a stretched move is an overreaction
rather than a fundamental repricing, from the STORED Stage 5.4 evidence only). Six documented components (price dislocation,
abnormal return, relative sector move, volume shock, gap / extension, short-term exhaustion), explicit weights, a 60 % coverage
floor, deterministic confidence rules and caps — see `DESIGN_56_PPE_ENGINE.md`; the point-in-time validation over 81 stored symbols
is in `scratchpad/s56/PPE_VALIDATION_56.md` (`python -m ppe.replay --db data/stock_agent.db --out <dir>` regenerates it).
PPE is not a buy/sell signal and not a probability of recovery: *PPE Confidence measures confidence in the overreaction
classification, not the probability that the stock will rise.* Served at `GET /api/terminal/ppe/{symbol}` and inside the terminal
state; refreshed on subscribe, on refresh and every `TRADER_PPE_REFRESH_SECONDS` (60 s), never per quote tick. In simulated mode the
result is labelled `SIMULATED_DATA`.

Caveats (Stage 5.6; the Stage 5.6.1 UI polish changed none of the formulas, weights, bands or confidence rules):

- The historical validation replayed **PPE Stretch** point-in-time. **PPE Confidence could not be replayed** against real historical
  evidence, because no stored Stage 5.4 packet predates the bar window, so the replay shows `EVIDENCE_NOT_AVAILABLE` throughout. The
  outcome diagnostics in the validation report are descriptive only and were not used to tune PPE.
- The real Alpaca market-data path of Trader View / PPE (`trader_view/market_real.py`, daily bars via `data.market_data.fetch_daily_bars`)
  is covered by fake-client tests but was **not live-tested** on the Stage 5.6 development machine, because market-data credentials were
  not available there.
- **Market Demand / buyer confirmation is not part of PPE.** Stage 5.7 implements it as a separate score with its own section (next
  section); PPE itself is unchanged and the two are never combined.

## Market Demand (Stage 5.7, deterministic, read-only)

A separate **MARKET DEMAND** section under PPE answers *"are buyers actually stepping in now?"* with one deterministic score 0 … 100
(0 = very weak buyer confirmation, 50 = mixed, 100 = very strong) built from bars only — six documented components with fixed
weights: relative volume / participation (0.20; cumulative session volume vs the same minute of prior sessions), VWAP / price
acceptance (0.20; cumulative session VWAP, time above it, hold streak, reclaim held or rejected), intraday structure (0.20; higher /
lower lows and highs over three 10-bar windows, up-close share, failed-breakdown reclaim), range strength (0.15; close location in the
session range, capped on a down move), relative strength vs sector (0.15; session and 5-session returns vs the same SECTOR_ETF →
PEER_BASKET → `SPY (market fallback)` hierarchy PPE uses, fallback always shown) and follow-through (0.10; bars on the current side of
VWAP, higher low kept, close-to-close follow-through). The score is the weighted average over the available components with a 60 %
coverage floor (`INSUFFICIENT_DATA` below it, never a filled-in 50); bands VERY_WEAK 0–24 · WEAK 25–39 · MIXED 40–59 · STRENGTHENING
60–74 · STRONG 75–89 · VERY_STRONG 90–100. `INTRADAY_MODE` needs 30 completed bars of the current session; otherwise `DAILY_MODE` uses
completed daily bars only (VWAP and structure NOT_AVAILABLE, coverage exactly 0.60) and says so — daily mode never implies real-time
confirmation. Served inside the terminal state, at `GET /api/terminal/demand/{symbol}` and as the socket message `demand` every
`TRADER_DEMAND_REFRESH_SECONDS` (30 s), never per quote tick; every result names its market-data source, benchmark, bars-through
time, session (date, completed bars, complete or in progress), calculation time and mode, and simulator results are `SIMULATED_DATA`.
See `DESIGN_57_MARKET_DEMAND.md`; the validation is in `scratchpad/s57/DEMAND_VALIDATION_57.md`
(`python -m demand.replay --db data/stock_agent.db --out <dir>` regenerates it).

What Market Demand is and is not: it is buyer-confirmation research; it is **not a probability** that the stock rises, not an expected
return, not a reversal probability, **not a trade signal**, **not part of PPE** (PPE Stretch, PPE Confidence and Market Demand stay three
separate numbers — e.g. Stretch −82 / Confidence 42 / Demand 18 reads "stretched, buyers not confirming"; Demand 76 later reads
"buyers confirming" — with no recommendation either way, and a high score on an upside-stretched stock is not a short cue), and
**not execution logic** (no order path, Prepare Buy / Prepare Sell stay disabled, no broker, model or research call). Exact disclaimer,
shown with every result: *Market Demand measures observed buyer participation and confirmation, not the probability that the stock will
rise.* Caveats: **bid/ask microstructure is omitted** from V1 (IEX quotes and synthetic simulator spreads are not reliable enough; no
order-book inference); intraday relative strength uses the session horizon only; the project has no intraday bar store, so the
**intraday validation** runs on deterministic synthetic sessions and the daily-fallback validation on the stored daily history; the
real Alpaca minute-bar path is fake-client-tested and was **not live-tested** on the development machine. Weights, scales and bands are
research hypotheses fixed in the design before the replay and were not tuned on future returns.

## Setup state machine + market-hours alerts (Stage 5.8, deterministic, read-only)

A compact **SETUP** section in Trader View and a "Setup alerts" list in the Alerts tab answer *"what is the current research setup
state?"* for one setup, **Downside Dislocation Recovery**, with four states: NONE (no active setup), WATCH (PPE Stretch ≤ −40; a
downside-stretched condition exists, confirmation incomplete), **READY FOR REVIEW** (WATCH plus PPE Confidence ≥ 40, Market Demand ≥ 60
with coverage ≥ 0.60, PPE coverage ≥ 0.60, fresh inputs and no evidence blocker) and INVALIDATED (an explicit rule fired: stretch back
above −20 after confirmation, demand below 35 on two consecutive evaluations, or a new strong company-specific evidence packet).
Hysteresis: WATCH is left only when stretch rises above −30; READY drops back to WATCH when demand falls below 45; an invalidated
episode closes once stretch is above −30, and a later ≤ −40 reading starts a new episode (new `setup_id`). Evidence never adds points —
it can only **block READY** (no packet, packet older than 30 days, company-specific deterioration with MODERATE/STRONG strength, or a
guidance-down item with company-specific / mixed cause) and the blocker is shown. Stale or missing PPE **pauses** evaluation
(`EVALUATION_PAUSED_*`) instead of invalidating; missing Demand keeps WATCH and prevents READY. Inputs are only the already-computed PPE,
Market Demand, stored Stage 5.4 packet, quote freshness and session; nothing is recomputed, no position or P/L is read, no model is
called. Exact rules: `DESIGN_58_SETUP_STATE_ALERTS.md`.

**READY means READY FOR REVIEW, not BUY.** Thresholds were fixed before any outcome analysis and are not tuned; there is no predictive
claim, no auto trade, no live execution, no BUY/SELL field, and Prepare Buy / Prepare Sell stay disabled. Everything is persisted in the
app database (`setup_episodes`, append-only `setup_transitions` / `setup_evaluations` / `setup_alerts`, `setup_controls` liveness
rows): a restart restores active WATCH / READY / INVALIDATED episodes and their history, re-evaluating unchanged inputs is a HOLD with no
new alert, and "when did this transition last fire?" is answered by `GET /api/setups/events` and `GET /api/setups/evaluations`.
Alerts fire only on NONE → WATCH, WATCH → READY, READY → WATCH, WATCH → INVALIDATED and READY → INVALIDATED (an identical
episode / state / reason alert within 15 minutes is suppressed; a different transition, and any new episode, always notifies) and carry symbol, state, reason and
the three source metrics — no trade action. Continuous evaluation runs **only inside the US regular session** (weekdays 09:30–16:00 ET;
no exchange-holiday calendar exists in the project, so holidays are treated as weekdays — a documented limitation), every
`SETUP_EVAL_INTERVAL_SECONDS` (60 s) over the watchlist plus any symbol with an open episode, under a database lease so two processes
never evaluate at once; there is **no 24/7 runtime**: outside the session nothing recomputes except an explicit subscribe / refresh in
Trader View. The evaluator never starts in a test process; `SETUP_EVALUATOR_ENABLED=false` switches it off; manual refresh keeps working
without it. The intraday-validation limits of Stage 5.7 are inherited (Market Demand's intraday components were validated on synthetic
sessions only). Read-only API: `GET /api/setups`, `/api/setups/{symbol}`, `/api/setups/events`, `/api/setups/evaluations`,
`/api/setups/alerts` (+ the existing read-acknowledgement `POST …/read`, `…/read-all`), `/api/setups/controls`.

## Trade proposal / review layer (Stage 5.9, deterministic, review only)

**A READY FOR REVIEW setup is not a trade.** Stage 5.9 adds a *review artifact* for such a setup: a deterministic, risk-bounded
**long review proposal** (`proposals/`, design: `DESIGN_59_TRADE_PROPOSAL_REVIEW.md`). It is not a recommendation, never a
BUY / SELL signal, and nothing is ever submitted — the status line always reads **NOT SUBMITTED** and the card says
"This is a deterministic review proposal, not a recommendation or submitted order." The user remains the only decision maker.

* **Eligibility**: only the `DOWNSIDE_DISLOCATION_RECOVERY` setup while its state is `READY`; the stored setup inputs must be fresh
  (PPE, Market Demand, the setup evaluation), no evidence gate or contradiction, a fresh reference price, no hard risk blocker.
  Anything else returns explicit blockers (`NOT ELIGIBLE` / `BLOCKED`), never a silent nothing.
* **Reference price** (first usable wins, named): quote midpoint (spread ≤ 1 %, ≤ 300 s old) → last trade → latest completed
  1-minute close (≤ 15 min) → last completed daily close, labelled `DAILY_REFERENCE`. An invalid value blocks; nothing is invented.
* **Invalidation level** (fixed rule, never tuned): structural low of the last 5 completed sessions (and the current session's
  completed minute lows) minus 0.25 × ATR(14), capped by reference − 1.5 × ATR(14) (`STRUCTURAL` / `ATR_CAP` / `ATR_FALLBACK`);
  must sit below the reference. Risk per share = reference − invalidation. No target price, no expected return, no probability.
* **Sizing** (Decimal, whole shares): risk budget = equity × `TRADE_PROPOSAL_RISK_FRACTION` (0.5 %, max 1 %); risk-based qty =
  floor(budget / risk per share); capped by the existing `MAX_POSITION_PERCENT` (10 %) position cap (bounded by the Stage 4.6B
  100 000 notional / 10 000 share limits) less the existing position, by buying power with the 4.6B 5 % buffer, and by the hard
  limit. Every cap contribution is returned; an existing position is stated explicitly and never averaged down.
* **Equity source** (named, never faked): real mode — the Stage 4.6A Alpaca Paper snapshot (0 network calls; refreshed only by the
  user) then the Stage 4.5 local paper portfolio; simulated mode — only the explicit `TRADE_PROPOSAL_REVIEW_EQUITY`; otherwise the
  proposal is `BLOCKED` with `EQUITY_NOT_AVAILABLE`.
* **Controls** (DESIGN_59 §10): 16 named controls (freshness, evidence, price, invalidation, risk per share, equity, budget, review
  halt, daily-loss halt, tradability, duplicate pending order, buying power, position cap, quantity). Each generated proposal stores
  one row per control with what it saw and returned (`proposal_controls`) and one upserted liveness row per control
  (`proposal_control_status`), both readable at `GET /api/proposals/controls`. The project had **no kill switch and no daily-loss
  guard**; Stage 5.9 adds the review-only flags `TRADE_PROPOSAL_HALT` and `TRADE_PROPOSAL_DAILY_LOSS_HALT` (default off) and observes
  the broker's account blocks — it does not compute a daily P&L and does not add an execution kill switch.
* **Blockers vs warnings** never mix: hard blockers make the proposal `BLOCKED`; warnings (weak evidence, moderate confidence,
  marginal demand, daily-reference price, existing position, near close / closed, sector fallback, unknown tradability or buying
  power, hypothetical equity, simulated data, quantity reductions) still render.
* **Identity, persistence, expiry**: `proposal_id = sha256(setup_id | created_at | reference_price | invalidation_price |
  risk_budget)[:32]`; `trade_proposals` is append-only (only `review_state` / `reviewed_at` change); intraday proposals expire
  after `TRADE_PROPOSAL_TTL_SECONDS` (300 s), daily-reference ones at the next session open; a proposal reads `STALE` as soon as
  the setup leaves READY or changes episode and `EXPIRED` past its expiry — both derived, never written. Unchanged inputs reuse
  the stored row (no duplicate per refresh); a material change (blockers, invalidation, quantity, equity, evidence packet, a
  reference move above `TRADE_PROPOSAL_PRICE_CHANGE_PCT` = 0.5 %, a setup transition) generates a new row and keeps the old one.
* **Review state**: `UNREVIEWED → VIEWED → DISMISSED` through the only two POSTs (`/api/proposals/{id}/view`, `/dismiss`).
  Read-only API: `GET /api/proposals`, `/history`, `/controls`, `/id/{proposal_id}`, `/{symbol}` (never generates).
* **Trader View**: a **TRADE REVIEW** card under the Setup card (`REVIEWABLE PROPOSAL` / `BLOCKED` / `NOT ELIGIBLE` / `STALE` /
  `EXPIRED`, reference, invalidation, risk per share, risk budget, max quantity, max notional, warnings, blockers, NOT SUBMITTED,
  View Details, Dismiss). Generated after each setup evaluation of the active ticker and after evaluator transitions; the
  `proposal` socket message is symbol-bound. Prepare Buy / Prepare Sell remain disabled (`PREVIEW_NOT_CONNECTED`).
* **Stage 4.6B stays disconnected**: `proposals/` never imports the order writer, the order service, the order reads or any trading
  client (pinned by `tests/test_proposals_59.py`); no proposal, alert or setup transition can place, preview, submit or cancel
  anything; no model is called; no automatic research runs. Auto paper trading is not enabled.

## Execution safety controls (Stage 5.9.1, persisted, no execution yet)

Stage 5.9 found that the project had **no operational kill switch and no daily-loss halt**. Stage 5.9.1 adds a small, deterministic,
**persisted** safety gate (`safety/`, design: `DESIGN_591_EXECUTION_SAFETY.md`) that a future execution layer must consult before any
entry. It still submits nothing: no Alpaca order, no Robinhood write, no auto trading. These controls are prerequisites for Stage 6.0.

* **States** (plain words first): 🟢 Trading allowed (`TRADING_ALLOWED`), 🟡 New trades paused (`NO_NEW_ENTRIES`), 🔴 Trading stopped
  (`HALTED`, or `UNKNOWN` when the gate itself failed). Precedence: manual kill switch > daily-loss halt > no-new-entries > allowed.
  `allowed_new_entry` is true only for `TRADING_ALLOWED` with a verifiable daily-loss control.
* **Kill switch** — now operational and persisted (`execution_safety_state`): **Emergency Stop** activates it, **Clear Emergency Stop**
  (a distinct two-click action) clears it; Resume never does. It survives restarts and day boundaries.
* **No-new-entries** — a separate persisted pause (**Pause New Trades** / **Resume**): monitoring and proposals continue, entries are
  blocked. Also entered automatically while the daily-loss control cannot be verified (no baseline, stale snapshot, invalid equity,
  unadjustable P&L).
* **Daily-loss halt** — now operational: `MAX_DAILY_LOSS_PERCENT` (3 %) is enforced against a **persisted daily equity baseline**
  (`daily_risk_baselines`, created by the evaluator from the first usable fresh equity of the New York trading date and recorded as an
  event — never derived on the fly). `daily_loss_pct = max(0, (starting − current) / starting × 100)` in Decimal; at or above the
  threshold the halt latches for the day (`DAILY_LOSS_LIMIT_REACHED`) and stays latched even if equity recovers. The next trading day
  resets only the daily latch and creates a new baseline; the manual kill switch is untouched.
* **Account data**: the Stage 5.9 read-only snapshot (Alpaca Paper 4.6A view, local paper portfolio, or the explicit hypothetical review
  equity in simulated mode). **Deposits / withdrawals limitation**: the Alpaca 4.6A snapshot carries no transfer activity, so a transfer
  cannot be told from trading P&L; for that source the gate reports `DAILY_PNL_ADJUSTMENT_UNAVAILABLE` (new entries blocked, a raw loss
  over the limit still halts) unless the operator explicitly sets `EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL`. The local paper and hypothetical
  accounts have no cash flows, so their P&L is reliable. A stale Alpaca snapshot (> 15 min) blocks readiness.
* **Failure behaviour**: a persistence or evaluator failure returns `UNKNOWN` (treated as stopped, never allowed) and records an
  `EVALUATOR_ERROR` event and the error on the control rows.
* **Evidence**: append-only `execution_safety_events` (pause / resume / stop / clear, baseline creation, daily-loss trigger, day reset,
  errors), append-only `execution_safety_evaluations` (bounded by a 60 s heartbeat when nothing changed) and `execution_safety_controls`
  liveness rows (`GLOBAL_KILL_SWITCH`, `DAILY_LOSS_HALT`, `NO_NEW_ENTRIES`, `DAILY_BASELINE`: last evaluated, last fired, counts, last
  result, last error). "When was it last evaluated / has it ever blocked / is the baseline populated" are answered from data.
* **API**: `GET /api/execution-safety`, `/events`, `/controls`; `POST /api/execution-safety/pause-new-entries`, `/resume-new-entries`,
  `/emergency-stop`, `/clear-emergency-stop` (safety state only). `safety.service.evaluate_execution_safety(...)` is the gate call for
  Stage 6.0; the `KILL_SWITCH`, `DAILY_LOSS_HALT` and new `NO_NEW_ENTRIES` proposal controls consume it, and every proposal view carries
  `execution_eligibility` from the live gate (a paused or stopped state reads "blocked" even on a stored REVIEWABLE proposal).
* **UI**: a compact **RISK CONTROLS** panel at the top of the Trader View right column (status, one reason line, the four buttons,
  View details). Prepare Buy / Prepare Sell remain disabled. The review-only config flags of Stage 5.9 remain honoured as overrides; the
  persisted gate is the source of truth. Startup policy `EXECUTION_SAFETY_STARTUP_POLICY` (`KEEP` default, `HALT_ON_START`) never clears a stop.

## Autonomous Alpaca PAPER trading (Stage 6.0, deterministic, paper only)

Stage 6.0 is the first autonomous loop of Stock Agent (`execution/`, design: `DESIGN_60_AUTONOMOUS_PAPER.md`), on the **Alpaca PAPER
account only**: READY setup (5.8) → reviewable proposal (5.9) → execution safety gate (5.9.1) → ONE paper entry → order monitoring →
broker-confirmed fills → internal paper position → deterministic exit conditions → ONE paper exit → reconciliation → closed trade.
No real money, no Robinhood call, no model call anywhere in the path. It proves operational execution correctness, not an edge.

* **Modes** — `MONITOR_ONLY` (default; nothing is submitted) and `PAPER_AUTO` (an explicit, persisted user choice: **Enable Paper Auto**
  takes two clicks and is refused unless the PAPER environment is proven by an account read of the constant paper host, the account is
  active, the safety gate is readable and reconciliation is healthy). **Stop New Trades** returns to `MONITOR_ONLY`; monitoring,
  reconciliation and deterministic exits of existing paper positions continue. The mode survives restarts; nothing is submitted before
  the startup reconciliation completes.
* **Paper-only boundary** — `execution/broker.py` builds every URL from the constant `paper-api.alpaca.markets`; there is no base-URL
  setting and no live mode. The transport refuses any other host / scheme / port / path / verb before anything is sent, allows an exact GET
  list within a per-cycle budget, and exactly one `POST /v2/orders` per intent whose body must equal the canonical payload (market, day,
  whole shares, no extended hours, no order class). `execution/simulated.py` is a deterministic in-memory paper broker that sits below the
  same client (tests, the browser harness, `EXECUTION_BROKER=simulated`).
* **Entry** — only a `DOWNSIDE_DISLOCATION_RECOVERY` setup in READY with a `REVIEWABLE` proposal whose `execution_eligibility.allowed`
  is true, LONG only, the proposal quantity exactly (`EXECUTION_MAX_SHARES_PER_ENTRY` is an optional explicit operator hard cap, default off).
  Every precondition is a persisted control: `EXECUTION_MODE`, `PAPER_ENVIRONMENT`, `SAFETY_GATE`, `PROPOSAL_VALIDITY`, `SESSION_POLICY`
  (weekdays 09:30–16:00 ET, not the final 15 minutes, the broker clock must agree — no holiday calendar exists), `BROKER_TRADABILITY`,
  `DUPLICATE_ENTRY`, `BROKER_POSITION`, `BROKER_ACCOUNT` (active, buying power ≥ price × qty × 1.05), `MAX_OPEN_POSITIONS` (now enforced:
  broker positions + pending entries < cap), `BROKER_RECONCILIATION`. At most one new entry per cycle.
* **Exactly once** — `execution_id = sha256("entry|" + proposal_id)[:32]`, `client_order_id = stockagent-entry-<execution_id>`; the intent row
  is written before the broker is contacted and the attempt is recorded before the wire (the database refuses a second attempt). A
  definitive reject ends the intent (no resubmission); an ambiguous result (timeout, reset, 429 / 5xx, unreadable answer) is never retried:
  the order is looked up by client id, adopted when found, `NOT_SENT` after two lookups at least 30 s apart. A crash at any point (after the
  intent, after the attempt, after the POST, after a fill, after an exit intent, after an exit POST) recovers from the database and the
  broker without a duplicate order (tests C01 / C02).
* **Orders and positions** — every broker status change is appended (`execution_order_events`); a position is created only from
  broker-confirmed fills with the filled quantity (partials included); rejected / canceled entries end `FAILED` with no replacement.
* **Exits** (deterministic, precedence order) — `HARD_INVALIDATION` (price ≤ the proposal's invalidation level), `EVIDENCE_INVALIDATION`,
  `SETUP_INVALIDATED`, `DEMAND_FAILURE` (Demand < 35 on two consecutive evaluations), `TIME_EXIT` (5 sessions), `DISLOCATION_NORMALIZED`
  (Stretch > −20). No profit target, no prediction. `client_order_id = stockagent-exit-<position_id>-<cycle>`; a rejected / expired exit opens
  the next bounded cycle (3), then the position needs attention. **The safety gate blocks new entries only**: pause, daily-loss halt and
  Emergency Stop never stop a risk-reducing exit, and a stop never liquidates.
* **Reconciliation** — every cycle and at startup: missing orders, unknown `stockagent-` orders, quantity mismatches, broker-flat /
  internal-open, external positions (informational; they count toward the cap and block their symbol), multiple orders, unknown statuses,
  broker unavailable. Blocking findings set `RECONCILIATION_REQUIRED` for new entries and are never fixed silently.
* **Loop** — `execution/scheduler.py`: one daemon thread per process under the `paper_executor` lease, 30 s in session / 300 s outside,
  exits before entries, never in a test / harness process; an idle `MONITOR_ONLY` process never contacts the broker.
* **Evidence** — `execution_mode`, `execution_runs`, `execution_intents`, `execution_orders`, `execution_order_events`, `paper_positions`,
  `position_events`, `execution_reconciliations`, `execution_controls` (15 liveness rows).
* **API** — `GET /api/execution`, `/status`, `/history`, `/controls`, `/intents`, `/positions`, `/findings`, `/symbol/{symbol}`;
  `POST /api/execution/auto/enable`, `/auto/disable` (mode only).
* **UI** — a **Paper Bot** panel under Risk Controls (⚪ Monitor only · 🟢 Paper Auto · 🔴 Needs attention, Enable Paper Auto / Stop New
  Trades, View details with recent paper trades) and a **Paper Trade** card per ticker (🟡 Watching · 🔵 Ready · 🟠 Entering · 🟢 In trade ·
  🟠 Exiting · ⚪ Closed · 🔴 Needs attention). Internal states live under View Details.
* **Known limitations** — no holiday calendar (the broker clock is the backstop); in real-data mode the safety gate's equity still comes
  from the Stage 4.6A snapshot (≤ 15 min old, `EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL` for an Alpaca source), so a stale snapshot reads
  New trades paused and the bot blocks entries — fail closed; the proposal's equity source in real mode is unchanged from Stage 5.9.

## Running without VS Code (Windows background runner)

The bot is the normal dashboard server: the same app as `uvicorn api.server:app`, with the Stage 5.8 setup evaluator and the Stage 6.0 paper
executor loop. It can run in the background, so **VS Code does not need to stay open**. **The PC must stay on, awake and online.** A shut down,
sleeping or hibernating PC, or a PC without internet, runs nothing. Windows only, local machine only (`127.0.0.1`), Alpaca PAPER only.

Starting the bot never sends an order. The executor stays in **Monitor only** unless Paper Auto was enabled in the app. Even then it
reconciles with the broker before any write, and the risk controls gate every new entry.

**Option A: PowerShell runner** (from the project folder)

```powershell
.\scripts\start_stock_agent.ps1     # start in the background (detached; closing VS Code or the window does not stop it)
.\scripts\check_stock_agent.ps1     # status: RUNNING / NOT RUNNING, mode, safety, reconciliation, executor loop
.\scripts\stop_stock_agent.ps1      # graceful stop of exactly this process (PID file + instance id; no other python.exe is touched)
```

* Python: `.venv\Scripts\python.exe`. In a git worktree the main checkout's `.venv` is used. `-Python <path>` overrides it.
* Environment: the project's `.env`, loaded by path. In a git worktree the main checkout's `.env` is used. `-EnvFile <path>` overrides it.
  No secret is printed, logged or stored in a script.
* Logs: `logs\stock_agent_stdout.log` (startup, scheduler, execution mode, paper broker configuration, shutdown),
  `logs\stock_agent_stderr.log` (errors) and `logs\stock_agent_runner.log` (start / stop / restart decisions). Secret values are redacted.
* Single instance: `runtime\stock_agent.pid` plus an OS file lock (`runtime\stock_agent.lock`). A second start is refused.
  A stale PID file is cleaned without touching any process.
* Options: `-Port 8000`, `-DbPath <file>` (default `data\stock_agent.db`).

**Option B: Windows Task Scheduler** (no admin rights needed; runs as you, only while you are logged on)

```powershell
.\scripts\install_stock_agent_task.ps1 -Trigger Logon -PrintOnly    # show the task definition first
.\scripts\install_stock_agent_task.ps1 -Trigger Logon               # register task "StockAgent": start at logon
.\scripts\install_stock_agent_task.ps1 -Trigger Weekdays -EasternTime 09:20     # or: Monday-Friday before the open
.\scripts\install_stock_agent_task.ps1 -Trigger Once -Date 2026-10-23 -EasternTime 09:35 -TaskName StockAgentSmokeStart   # one start
.\scripts\install_stock_agent_task.ps1 -TaskName StockAgent -Unregister          # remove a task
```

The task runs `start_stock_agent.ps1 -Foreground`. That supervisor restarts the server after a crash, at most 3 times, 60 s apart.
It never restarts after a deliberate stop. A second copy of the task is ignored. Stop the bot with `.\scripts\stop_stock_agent.ps1`, not
with "End" in Task Scheduler. The task only starts the process; it never schedules an order. After any restart the executor runs its normal
startup reconciliation before any write, and an ambiguous earlier order is looked up, never resent.

**Keeping the PC awake.** The runner never changes power settings. Check them yourself before relying on the bot:

```powershell
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE   # "AC Power Setting Index: 0x00000000" = never sleeps when plugged in
powercfg /query SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE # the same for hibernate
powercfg /requests                                     # what currently keeps the PC awake
powercfg /lastwake                                     # what last woke it
```

Settings > System > Power & battery > Screen and sleep: set "When plugged in, put my device to sleep after" to **Never** for the
trading window. Also keep the PC on mains power and connected to the internet.

### Supervised one-share AAPL paper smoke (standalone, Stage 6.0)

`scripts\run_aapl_paper_smoke.ps1` (wrapper) runs `scripts\run_aapl_paper_smoke.py` without VS Code. It proves the Stage 6.0 execution
infrastructure end to end on the Alpaca PAPER account. It is not a strategy signal.

* **Hard scope:** AAPL only, 1 share, BUY then SELL, at most 2 broker order submissions, Alpaca PAPER host only. An independent
  order guard wraps the broker's single submission seam and refuses anything else before the wire. Every order is created by the real
  Stage 6.0 executor: setup, then proposal, then safety gate, then executor, then Alpaca PAPER. No Robinhood, no model call, no live endpoint.
* **Window:** 09:45 to 10:00 New York time on `-RunDate` only. Outside it: NO ORDER, and no catch-up later.
* **Preconditions:** the normal Stock Agent server must answer at `http://127.0.0.1:8000` and be in Monitor only. Fresh pre-flight
  checks then cover:
  * the paper snapshot refresh, the paper proof, an active and unblocked account, equity and buying power;
  * the broker clock open, AAPL tradable, a fresh AAPL quote, Trading allowed, healthy reconciliation;
  * no AAPL order, AAPL position or earlier Stage 6.0 AAPL order at the broker, a share cap of 1, and a fresh scratch database.
  Any failure means NO ORDER.
* **Labels:** `SMOKE_FORCED_SETUP` forces AAPL to READY through the real setup service. `SMOKE_FORCED_EXIT` forces the setup's
  stretch to normalize, and the executor decides and submits the exit. In real mode the process logs
  `UNADJUSTED_PNL_ACCEPTED_FOR_SMOKE_ONLY`: the safety gate accepts the unadjusted Alpaca P&L for this process only. Permanent
  configuration is unchanged, and unattended safety is not solved by it.
* **Ambiguous order:** an ambiguous BUY or SELL is reconciled by `client_order_id` only and never resent. If it stays unresolved,
  the smoke fails.
* **Always:** Paper Auto is switched off in a `finally` block on every outcome.
* **PASS rule:** PASS only when every final condition holds:
  * one entry intent, one BUY and 1 share filled;
  * one exit intent, one SELL and 1 share filled;
  * the broker flat in AAPL and the internal position CLOSED;
  * reconciliation healthy, no duplicate order and Paper Auto off.
  Otherwise the result is FAIL with the reason.
* **Never twice:** an OS lock plus `runtime\aapl_paper_smoke_state.json` stop a second run. After RUNNING, PASS or a FAIL that
  submitted an order, every further run is refused.
* **Files:** `runtime\aapl_paper_smoke.db` (scratch; never the bot or project database), `logs\aapl_paper_smoke.log` and
  `runtime\aapl_paper_smoke_result.json`. Only the four `ALPACA_*` names are read from `.env`, and a secret scan runs on the logs and the result.

```powershell
.\scripts\install_aapl_smoke_task.ps1 -Date 2026-10-23 -PrintOnly    # shows the PC sleep setting and the task definition
.\scripts\install_aapl_smoke_task.ps1 -Date 2026-10-23               # task "StockAgentSmokeRun": once at 09:45 ET, no retry, no catch-up
.\scripts\install_aapl_smoke_task.ps1 -Unregister                    # remove it
.\scripts\run_aapl_paper_smoke.ps1 -RunDate 2026-10-23 -Simulated -ClockEt "2026-10-23 09:46" -ServerUrl http://127.0.0.1:8031   # offline proof
```

## Continuous Alpaca PAPER account refresh (Stage 6.0.1, read only)

The Alpaca PAPER account snapshot the bot relies on is now refreshed automatically (`account_refresh/`, design:
`DESIGN_601_CONTINUOUS_ACCOUNT_REFRESH.md`). It no longer depends on clicking Refresh, so the risk controls no longer go stale after 15
minutes on an unattended bot.

* **One owner.** A single background thread per server holds the `alpaca_account_refresher` lease. It refreshes every **60 s** inside
  the US regular session and every **300 s** outside it. Each refresh is three read-only GETs to the paper host: account, positions and
  open orders. There is no per-ticker, per-request or browser refresher, and API reads never refresh.
* **Atomic.** A complete, validated snapshot replaces the previous one in one database transaction. A failed or invalid read, such as a
  timeout, connection reset, 429, 5xx, broken JSON, missing field or bad number, is recorded and never half-written. Reads retry at most
  3 times (2 s, then 5 s). Order submission is unchanged and is never retried.
* **Fresh vs stale.** The data is Fresh after a successful refresh by this process. It turns Stale after 600 s without a success or after
  3 failed refreshes in a row. After a restart it reads Checking until the first refresh, so a cached snapshot never makes entries
  eligible on startup. The next successful refresh recovers automatically, with no restart and no manual step.
* **Safety.** The Stage 5.9.1 risk controls and the Stage 5.9 proposals read the refreshed snapshot through their existing account
  seams; neither package changed. Stale or failing account data means **New trades paused** and never Trading allowed. Open paper
  positions keep being monitored, and deterministic exits stay allowed.
* **Limitation that remains.** The Alpaca snapshot still cannot separate deposits and withdrawals from trading P&L. With fresh data
  the real-account risk controls therefore read "New trades paused - Deposits and withdrawals cannot be separated from trading P&L"
  (`DAILY_PNL_ADJUSTMENT_UNAVAILABLE`), unless an operator explicitly sets `EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL`. This stage never sets
  that flag, and real-data safety is not production-complete.
* **Evidence:** `account_refresh_snapshot` (one row), `account_refresh_attempts` (append-only) and `account_refresh_liveness` (one row:
  last attempt or success, counts, consecutive failures, last error).
* **API:** `GET /api/account-refresh` and `GET /api/account-refresh/history`, read only and with no credentials. The path avoids the words
  "alpaca", "paper" and "broker", which frozen route pins reserve.
* **UI and health check:** the Paper Bot panel shows one line, for example **Account data: Fresh (42s ago)**, Stale or unavailable, with
  details under View details. `scripts\check_stock_agent.ps1` prints the same line and whether the refresh loop runs.
* **Unchanged:** execution, strategy, PPE, Demand, Evidence, setup rules, proposal sizing, thresholds and order type. No Robinhood, no model
  call, no real money.

## Cash-flow-adjusted daily P&L (Stage 6.0.2, read only)

The daily-loss control now separates money moved in or out of the Alpaca PAPER account from trading P&L (`cash_flow/`, design:
`DESIGN_602_ADJUSTED_DAILY_PNL.md`). This removes the Stage 6.0.1 limitation: with fresh data and no unexplained account activity, the
real-account risk controls read **Trading allowed** while `EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL` stays **off**.

* **Source.** Alpaca's account activity feed, `GET /v2/account/activities?category=non_trade_activity` on the paper host only. It is
  read in the same refresh cycle as the account snapshot: snapshot, then activities, then the risk controls. Paging is bounded at 20
  pages of 100, and reads retry at most 3 times (2 s, then 5 s; a 429 `Retry-After` is honoured up to 30 s).
* **Ledger.** `cash_flow_events` is append-only and deduplicated by the broker's activity id; a changed record becomes a new revision.
  `cash_flow_refresh_attempts` (append-only) and `cash_flow_liveness` (one row) record every read. Account numbers in broker text are
  redacted.
* **Classification.** Deposits, withdrawals and cash journals (`CSD`, `CSW`, `TRANS`, `ACATC`, `JNLC`, `JNL`) adjust the baseline. Fees,
  dividends, interest and corporate actions stay in trading P&L. Unknown or unvalued types (`MISC`, `JNLS`, `ACATS`, anything new) are
  never guessed: they pause new entries with `CASH_FLOW_CLASSIFICATION_UNCERTAIN`.
* **Formula.** `adjusted start = start of day + (deposits + journals in) - (withdrawals + journals out)`; daily loss % =
  `max(0, (adjusted start - equity) / adjusted start x 100)`. The start of day is the Stage 5.9.1 persisted baseline; nothing new is
  stored for it. Example: start 100000, deposit 5000, equity 104000 gives a daily P&L of -1000 (0.95 %), not +4000.
* **Timing.** An event counts from its broker timestamp. Within 60 s of the baseline read it is ambiguous for the day; a brand-new event
  waits one refresh (`CASH_FLOW_PENDING`) until the account snapshot reflects it. A late-posted event is placed by its own time and the
  risk controls re-evaluate at once. A new New York trading date starts a new baseline and a new window.
* **Safety.** Healthy needs a fresh snapshot, today's baseline, a fresh activity read taken after that snapshot, and no pending,
  ambiguous or unclassified activity. Anything else means **New trades paused**; exits continue. A halt fires from the adjusted loss and
  stays latched for the day: a later deposit never clears it, and a withdrawal never fakes a loss.
* **API:** `GET /api/cash-flow-status`, `/events` and `/attempts`, read only, with no broker ids or credentials.
* **UI and health check:** the Paper Bot panel adds one line, for example **Cash-flow data: Fresh (12s ago)**, with today's trading P/L,
  the deposits / withdrawals adjustment, the daily loss and its limit under View details. `scripts\check_stock_agent.ps1` prints the
  same line. Risk Controls still show only Trading allowed, New trades paused or Trading stopped.
* **Unchanged:** execution, order submission, exits, strategy, PPE, Demand, Evidence, setups, proposals and thresholds. No Robinhood, no
  model call, no real money.

## Forward Paper Validation (Stage 6.1)

Stage 6.0 proved that the bot can execute safely; Stage 6.1 records whether it stays **operationally reliable** across many real Alpaca
PAPER sessions and, kept strictly apart, what the **frozen strategy** does forward. It is instrumentation only: no strategy, threshold,
size, exit, mode or order changes (`DESIGN_61_FORWARD_PAPER_VALIDATION.md`).

* **Observer** (`forward_validation/`): one sample a minute inside the regular session (five minutes outside it) of the mode, the safety
  gate, account and cash-flow freshness, equity, reconciliation health, open paper positions (with the executor's price as an MFE / MAE
  mark) and the Stage 6.0 control counts; one record per New York trading session classified HEALTHY / DEGRADED / FAILED / NOT_RUN by
  documented rules; unobserved weekdays back-filled as NOT_RUN (no exchange holiday calendar exists); process restarts recorded with the
  state before and after, the reconciliation result and position continuity. Never runs in a test / harness process.
* **Statistics from persisted events only**: the setup → proposal → entry → fill → position → exit → close funnel with conversion rates,
  order reliability (duplicates, rejects, cancels, expiries, partial fills, ambiguous submissions), reconciliation findings with a resolution
  outcome, safety fires (recovered / persistent), freshness, broker errors, closed paper trades with slippage against the persisted reference
  price, P/L, sampled MFE / MAE, exit reason and setup attribution, drawdown on **adjusted** equity, win rate paired with trade count,
  average win / loss, a descriptive expectancy and a conservative strategy status that reads `INSUFFICIENT_DATA` until 30 closed trades and
  30 healthy sessions exist.
* **The one action**: a critical defect (a duplicate broker order, a non-paper host attempt, an entry recorded while the gate was blocking,
  a reconciliation mismatch unresolved across sessions) stops Paper Auto at once (Monitor only; positions keep their deterministic exits;
  nothing is liquidated) and reads **NEEDS ATTENTION**.
* **Read-only API** `GET /api/forward-validation` (+ `/sessions`, `/closed`, `/closed/{position_id}` audit trace, `/daily`,
  `/report/daily`, `/report/weekly`, `/export/{kind}` CSV / JSON, `/restarts`, `/events`, `/samples`, `/funnel`, `/frozen-controls`);
  the compact **Forward Paper Validation** panel after the Paper Bot panel and the separate page `forward_validation.html`.
* **Operation**: Alpaca PAPER only, Paper Auto deliberately ON during intended validation sessions, regular hours with the existing
  near-close cutoff, the existing detached Windows runner; the PC must stay awake with network. No forced setups, forced exits, test fault
  files or unadjusted-P&L acceptance in normal operation. Bad trades and failed sessions stay in the data set.

## Product UI (Stage 6.1.1, four sections, display only)

The dashboard opens on **TRADE** and has four primary sections: **TRADE · POSITIONS · ACTIVITY · MORE**. Everything the earlier stages built is
still there - the thirteen legacy tabs are hidden, not removed, and MORE reaches each of them - but a beginner sees one chart, the state of the
Agent and the risk status first. The shell is display only: every value is the Paper Bot / Risk Controls payload or a GET route mapped for display,
and its two actions click the existing Risk Controls / Paper Bot buttons, so nothing about execution, risk or Paper Auto changed.

* **Header status bar** (always visible): `PAPER` (or `SIMULATED PAPER`) · `MARKET OPEN / CLOSED` · `SYSTEM HEALTHY / DEGRADED / STOPPED` (a
  display summary of the Stage 5.9.1 gate, the Stage 6.0.1 account freshness, the Stage 6.0.2 cash-flow freshness, reconciliation and the bot's
  attention flag: STOPPED = halted / unknown gate or attention, DEGRADED = anything paused, stale or unavailable, HEALTHY otherwise; click it for
  every reason) · the Paper Auto control (`AUTO ON / OFF` with the enable / stop button), plus the policy line
  "Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled." A missing value shows a dash; a problem (stale account, unknown gate, attention) is never hidden.
* **TRADE**: the Trader View chart and watchlist with three cards above them - **Agent / Setup** (the primary card: `WATCHING · READY · ENTERING ·
  IN TRADE · EXITING · CLOSED · NEEDS ATTENTION`, the Stage 6.0 card keys and the Stage 5.8 setup states renamed for display, a halted or unknown
  risk gate reads NEEDS ATTENTION unless a paper position is live; one setup line in plain words, the engine's own blocker count when it has one,
  and **Why?** for PPE stretch, confidence, demand, evidence, the stored setup state and the proposal status), **Position** (`No position`,
  `Entering · Waiting for broker fill`, or shares / entry / current / P/L / `Day N / max hold`; broker and reconciliation fields stay under POSITIONS)
  and **Risk** (one headline, one primary reason, `[ STOP TRADING ]`, every dependency and attention reason under **Details**). `[ STOP TRADING ]`
  is the Stage 5.9.1 emergency stop and the header's Paper Auto button is the Stage 6.0 two-click one (the first click only arms the confirmation).
  When the feed has no candles the chart shrinks to a compact, centred message (`Market closed` is informational; `Market data unavailable` returns
  automatically when data reconnects; there is no Retry button because no refresh action exists), and the Stage 5.5 status chips collapse into one
  diagnostics line with an expander. **View details** (top right) opens the Stage 5.5-6.1 detail column - PPE, Market Demand, Setup, Trade Review,
  Evidence, Position sources, Risk Controls, Paper Bot, Paper Trade, Forward Paper Validation, the status chips and the disabled Prepare Buy / Sell /
  Add Alert placeholders; the choice is remembered per browser.
* **Trading controls** (Risk card, one block): Paper Auto `ON / OFF` with `Enable Paper Auto` / `Disable Auto`, Trading `ALLOWED / PAUSED /
  STOPPED` with `Pause New Trades` / `Resume New Trades`, Emergency Stop `INACTIVE / ACTIVE` with `Emergency Stop` / `Clear Emergency Stop`. Enable,
  Emergency Stop and Clear show an explanation first and only Confirm clicks the legacy button (the Stage 6.0 / 5.9.1 two-click flows stay intact);
  Disable, Pause and Resume delegate directly. Resume never clears the emergency-stop latch, a cleared stop shows whatever the gate says next, and an
  existing position keeps its monitoring and protective exits whatever the control state - the cards say so in plain words. No Reset Trade exists.
  After Confirm / Disable the row reads `Checking Paper Auto status…` until the authoritative `GET /api/execution/status` has been re-read; `AUTO ON` appears
  only when that status says `PAPER_AUTO`, a refused enable shows `Could not enable Paper Auto. <backend message>` and stays OFF, and a browser
  refresh re-reads the persisted mode.
* **POSITIONS**: one card per open or pending paper position (quantity, entry, current, P/L, holding day); a click shows setup state, demand,
  evidence, exit monitoring, risk state, broker orders and fills and the recent position events. Closed paper trades sit under a fold.
* **ACTIVITY**: a "Today" strip (trades, open, closed, paper P/L, issues) and a timeline built only from persisted rows - setup transitions,
  proposals and their risk-check result, order submissions, fills and rejections, position opened / exit required / closed, attention, the safety-gate
  events and the Forward Paper Validation events - then a compact **Validation** block (sessions, healthy, duplicates, reconciliation issues, strategy
  status) linking to `forward_validation.html`. The Stage 5.8 rule / setup alert lists stay under a fold.
* **MORE**: Research (Strategy Lab and its workspaces, Research Setups, Research History), Tools (AI Agent, Market dashboard, Watchlist editor, Daily
  Review, Trader Review), System (Forward Validation, Account / execution health, Risk Controls, Portfolio - Robinhood, read only) and Settings
  ("Advanced configuration": server-side `config.py` / `.env`; no secret is ever shown).

Implementation: `frontend/product_ui.js` + `frontend/product_ui.css`, loaded by two marked, additive blocks of `frontend/index.html`. The legacy
markup, every legacy script and every backend package are byte-identical to the Stage 6.1 base (`tests/test_product_ui_611.py`,
`tests/frontend/product_ui.test.cjs`, harness flow `product_ui`). The old disclaimer, the two "not available" placeholders and the Portfolio note
that predates paper execution are hidden by the shell and replaced by current wording. Design: `DESIGN_611_PRODUCT_UI.md`.

## Continuous integration (Stage 6.0.3, parallel suites)

`Stock Agent CI` (`.github/workflows/tests.yml`) runs on every push to `main` and every pull request to `main`, on GitHub-hosted
Windows runners, with no secret and no `.env`: the tests block every socket, replace both paper-order wires with tripwires, and the
browser harness drives a simulated world against a guarded local server. The one stable status is **CI Success**, green only when every
job below succeeded.

* **Pytest Core, Pytest Execution, Pytest Account & Cash Flow, Pytest Paper & Portfolio, Pytest Research, Pytest Model Campaign,
  Pytest Forward Validation, Pytest Runner & Smoke.** Eight parallel pytest jobs. Every `tests/test_*.py` file belongs to exactly one job through
  `.github/ci/pytest_groups.json`; `python scripts/ci_pytest_groups.py check` fails when a file is missing, listed twice or absent, and
  runs before every suite (also as `tests/test_ci_groups_603.py`). A new test file that belongs to no group therefore fails CI instead
  of silently never running. The groups are balanced by the runtime measured on the runner, where the SQLite-heavy execution,
  account-refresh and cash-flow suites run many times slower than locally (their databases live in pytest's temporary directory, which
  the jobs therefore point at the runner's work-disk temp folder). The Windows-only runner, smoke and desktop-notification tests stay
  together. The Stage 4.6B manual paper-order tests still run first as their own step, in the job that owns their file.
* **Frontend Tests.** The Node tests (`node --test "tests/frontend/*.test.cjs"`); the same files also run through their pytest wrappers.
* **Browser Harness.** `python -m browser_tests.run` with the runner image's headless Microsoft Edge; its screenshots and summary are
  uploaded only when it fails.

Run a group locally with `python scripts/ci_pytest_groups.py run <group> -- -q`. Jobs do not share a database, port or runtime folder;
each test picks free ports and temporary folders. `fail-fast` is off so one run shows every failing suite.

The workflow is verified structurally (`tests/test_ci_groups_603.py`: job names, triggers, the Stage 4.6B step, timeouts, no secret) and
is no longer byte-pinned by the Stage 5.5 freeze test. That pin, and the freeze tests of Stages 6.0, 6.0.1 and 6.0.2 that pin each
other, each carry one marked, additive Stage 6.0.3 block (the `>>>` / `<<<` comment markers used since Stage 6.0.2); `test_P01` proves
every byte outside those blocks equals the Stage 6.0.3 base and that no other file carries such a block.
