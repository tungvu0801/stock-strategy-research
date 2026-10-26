# Stage 6.9 — Robinhood read-only portfolio + unified exposure

Robinhood becomes a **read-only** account source. A unified portfolio view shows it next to the Alpaca PAPER account. Robinhood is **not** an
execution broker: nothing in this stage can place, change, cancel or close a Robinhood order or position, and Robinhood holdings never become
bot-managed. Alpaca PAPER stays the only execution broker; its semantics, the executor, the risk controls and every strategy are unchanged.

## 1. Access mechanism (existing, reused)

`stock-agent → portfolio.provider.RobinhoodGatewayPortfolioProvider → http://127.0.0.1:8787 (rh_gateway) → Robinhood Trading MCP`

- **The gateway** (`../rh_gateway`) is the only component that talks to Robinhood. It holds the OAuth token and the full account number.
  - It calls only 8 read tools. Mutation tools (`place_*`, `cancel_*`, `exercise_*`, …) are deny-listed before any session exists.
  - It serves fixed GET routes on loopback only, behind a shared secret.
- **This app** holds only the gateway's local secret (DPAPI file). It sends that secret on the loopback header and never stores or logs it.
- **This stage's adapter** calls exactly two of the provider's GET methods (`/portfolio`, `/positions`).
- **Legacy path:** `robinhood_sync.py` (unofficial `robin_stocks`, username and password) is not used and not imported.

## 2. Read-only contract (`unified_portfolio/robinhood.py`)

`RobinhoodPortfolioAdapter` has one operation, `read()`, which calls `get_portfolio()` and `get_positions()`.
- It has no buy, sell, cancel, replace, submit or close member.
- `__slots__` prevents attaching one.
- It never touches any other provider method.

`RobinhoodSource` holds the normalized snapshot (alias plus masked id only; values from `portfolio.analytics.build_view`, sectors from the
curated map) and its state:

| State | Meaning | `positions` |
|---|---|---|
| NOT_CONFIGURED | `ROBINHOOD_PORTFOLIO_ENABLED=false`, the gateway is not set up on this machine, or the configuration is invalid | `None` |
| AUTH_REQUIRED | the gateway needs a new Robinhood login (`REAUTH_REQUIRED`), or it rejected this app | `None` |
| NOT_LOADED | enabled, never read | `None` |
| LOADED | read and fresh | rows |
| STALE | older than `FRESHNESS_PORTFOLIO_STALE_SECONDS`, marked STALE by the gateway, or **the last good snapshot kept after a failed read** | rows |
| ERROR | the read failed and no snapshot exists | `None` |

`positions` is `None` in every state without data. Robinhood being unavailable is **never** zero holdings.

**When the gateway is called:**
- The unified routes read at most once per `ROBINHOOD_PORTFOLIO_TTL_SECONDS` (default 120 s).
- Trader View and the TRADE card read the cached snapshot only; they never call the gateway.
- There is no background loop, and nothing is written to a database.

## 3. Ownership

| Ownership | Where | Meaning |
|---|---|---|
| BOT_MANAGED | Alpaca PAPER | opened from a broker-confirmed bot fill, or a bot entry in progress |
| EXTERNAL | Alpaca PAPER | in the paper account, not the bot's (Stage 6.8) |
| NEEDS_REVIEW | Alpaca PAPER | the bot's trade in the symbol is closed, yet shares remain (Stage 6.8) |
| USER_MANAGED | Robinhood (always) | visible, counted in exposure, flagged in risk analytics; the bot never trades, exits or resizes it |

Ownership comes only from the account and the executor's own records. A Robinhood holding never becomes BOT_MANAGED: not because its ticker
matches a strategy, a READY setup, or Paper Auto being on (`test_O01`, `test_X01`).

## 4. Unified portfolio state (`unified_portfolio/state.py`, pure)

- **Rows:** one per account position, never merged. NVDA in Alpaca (bot) and in Robinhood (user) are two rows.
- **Exposure:** computed over the known sources only.
  - By symbol: quantity and value summed across accounts, each account kept.
  - By source, by ownership, PAPER vs REAL, and by sector (curated map only; unclassified value reported, never guessed).
  - Largest position and top-5 concentration.
  - Rows without a usable price are listed and excluded, never guessed.
  - Example: $500 bot NVDA (Alpaca) plus $4,000 user NVDA (Robinhood) is $4,500 exposure, shown as BOT_MANAGED $500 and USER_MANAGED $4,000
    (`test_U02`).
- **Equity:** PAPER and REAL are kept apart and **never added together**. Paper trades are never sized from Robinhood cash.
- **State:**
  - COMPLETE: every source is loaded and fresh.
  - STALE_SOURCE: a stale source is included and disclosed.
  - PARTIAL: a source has no data; exposure covers the others, disclosed as "unknown, not zero".
  - UNAVAILABLE: no source has data.
- **Warnings** (informational only): missing or stale source, duplicate symbol, concentration (`UNIFIED_CONCENTRATION_WARN_PCT`, default 20%),
  external exposure in a READY ticker, unvalued rows.

## 5. Duplicate-entry policy (advisory; executor unchanged)

**Current production policy (documented, unchanged):** the executor blocks a new bot entry only when the **Alpaca PAPER** account already
holds the symbol (control `BROKER_POSITION`), and counts every Alpaca position toward the open-positions limit. Robinhood holdings are not an
executor input.

**Stage 6.9 adds `BLOCK_NEW_BOT_ENTRY_IF_EXTERNAL_EXPOSURE` in ADVISORY mode** (`ROBINHOOD_DUPLICATE_ENTRY_POLICY=ADVISORY|OFF`):
- For each READY ticker it computes `would_block` (true, false, or `None` when Robinhood is not loaded) and shows a warning.
- It is **not enforced** (`enforced: false`). `test_P01` proves the bot still enters a symbol Robinhood holds.

Enforcing it would add an executor entry control, which changes frozen execution semantics. That is proposed as a separate stage (6.9.1): a
new entry control fed by a fresh Robinhood read, failing closed when Robinhood is not loaded, with its own pins.

## 6. API (GET only) and UI

- `GET /api/unified-portfolio`: the unified state.
- `GET /api/unified-portfolio/robinhood`: the source. There is no POST, PUT, PATCH or DELETE route (`test_W04`, `test_R01`).
- The new routes are deliberately not under `/api/portfolio`. The product shell still never calls that legacy route, which has POST endpoints,
  including an LLM explain.

**POSITIONS:**
- **PAPER — ALPACA** (the Stage 6.8 card: bot / external).
- **REAL — ROBINHOOD · user managed · read only** (state, freshness, rows).
- **Unified exposure** (state, disclosure, totals, PAPER vs REAL, ownership, largest, top 5, equity PAPER and REAL apart, by-symbol table with
  per-account breakdown, warnings, the current entry policy).

**TRADE Position card:**
- Without a bot position: "No bot position · Alpaca PAPER account: … · Robinhood · REAL: N shares · User managed · Unified exposure: …".
- With a bot position: the other accounts are named under it.
- Robinhood is "unknown (…)" when not loaded, never "none".

**Trader View:**
- The Robinhood source reads the cached snapshot, labelled "Robinhood · REAL · user managed". NOT_LOADED, AUTH_REQUIRED and ERROR never read
  as No position.
- Not configured: the earlier behaviour, unchanged.

**Bot Activity:** unchanged. No Robinhood read appears as a bot trade.

## 7. Configuration

- `ROBINHOOD_PORTFOLIO_ENABLED` (default false).
- `ROBINHOOD_PORTFOLIO_SOURCE` (`gateway` | `fixture`, synthetic previews only).
- `ROBINHOOD_PORTFOLIO_FIXTURE`.
- `ROBINHOOD_PORTFOLIO_TTL_SECONDS`.
- `ROBINHOOD_DUPLICATE_ENTRY_POLICY` (ADVISORY | OFF).
- `UNIFIED_CONCENTRATION_WARN_PCT`.

There is **no** Robinhood execution setting; `test_W04` fails if one appears.

## 8. Isolation proofs (`tests/test_robinhood_portfolio_69.py`)

- `test_W01`: the adapter's members are exactly `read` / `READS` / `_provider`; nothing can be attached; only the two reads reach the provider.
- `test_W02`: the read paths are exercised against a spying gateway that records any write verb, with a fake `robin_stocks` whose order
  functions record calls. Paths: unified and Robinhood routes, Trader View, activity, account positions, status, a direct refresh and the
  unified build. Result: **0 writes**, only `GET /portfolio` and `GET /positions`, 0 broker POSTs.
- `test_W03`: `execution/`, `safety/`, `proposals/` and `setups/` import no Robinhood or portfolio module; the new package imports no
  execution, safety, proposal, paper, LLM or `robin_stocks` module; its code has no write-shaped call.
- `test_X02` / `test_X03`: the bot's exit sells only its own Alpaca PAPER shares, and the executor never reaches Robinhood. With no bot
  position, nothing is exited whatever a Robinhood holding does.
- `test_F01`: the exact Stage 6.9 change set. No execution, safety, proposal, setup, strategy, validation, account-refresh, cash-flow, paper or
  legacy portfolio file changed. The frozen-controls hash is checked in a clean process.

## 9. Freeze ledger

Stage 6.9 extends `tests/freeze_ledger.py`:
- Edits of already-frozen files are registered as exact SHA-256 hashes. The view base stays main@6.7, where every older freeze test passed.
- New files are `NEW_69`.
- The GET-route allow-lists of the Stage 6.1.1 / 6.1.2 / 6.2 wiring tests gain `/api/unified-portfolio`; every other assertion is unchanged.
