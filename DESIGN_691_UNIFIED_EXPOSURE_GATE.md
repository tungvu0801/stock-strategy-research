# Stage 6.9.1 — Unified external exposure gate

Stage 6.9 made Robinhood a read-only account source. Its duplicate-entry policy was **advisory**: it was computed and shown, but the executor never used it.
Stage 6.9.1 turns that information into **one deterministic entry control**, `UNIFIED_EXTERNAL_EXPOSURE`. When the gate is enabled, the control can
block a **new Alpaca PAPER entry**.

What the control never does:
- It never places, changes, cancels, closes or resizes anything in any account.
- It never touches exits.
- It never treats a Robinhood holding as the bot's.

Robinhood is a **risk input only**. Alpaca PAPER stays the only execution broker. The gate ships with `UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=false`,
so production behaviour is unchanged until someone reviews it and turns it on.

## 1. Flow

```
setup READY → proposal REVIEWABLE → the existing entry controls (unchanged, same order)
            → UNIFIED_EXTERNAL_EXPOSURE (only when the gate is on)
            → blocking() → ALLOW or BLOCK → intent → the single Alpaca PAPER POST (unchanged)
```

The control is appended to `execution.engine.entry_controls()`. `blocking()` and the entry decision treat it like every other control, so there is no
parallel decision path. Possible results:
- `PASS`: allow.
- `WARN`: allow, but the data has a caveat that the reason names.
- `BLOCK`: block.

## 2. The control (`execution/exposure_gate.py`, pure)

**Inputs:**
- **The Stage 6.9 `UnifiedPortfolioState`**, as a plain dict:
  - The executor reads it **once per Paper Auto scan**, and only when the gate is on, through `TerminalStream.unified_portfolio()`.
  - Robinhood is read at most once per `ROBINHOOD_PORTFOLIO_TTL_SECONDS`.
- **The planned entry** that the other controls already use: `entry_quantity(final_qty, cap) × proposal reference price`. No model is involved.

The module imports only the standard library and `execution.models`. It names no account in code; each account's name comes from the snapshot's own
source label.

| Rule | Condition | Result |
|---|---|---|
| A · duplicate symbol | an account that is **not the execution broker** (Robinhood) holds the symbol: quantity ≠ 0 (fractional kept, a short counts), ownership `USER_MANAGED` / `EXTERNAL` / `NEEDS_REVIEW` | `BLOCK` `EXTERNAL_SYMBOL_ALREADY_HELD` |
| B · concentration | `projected_pct > threshold` (strictly greater; equal is allowed) | `BLOCK` `SYMBOL_CONCENTRATION_ABOVE_LIMIT` |
| C · sector | not evaluated: the curated sector map leaves names UNCLASSIFIED, so nothing blocks on a sector | — |

**Rule A in detail:**
- It never looks at Alpaca rows. An Alpaca holding is still blocked by the unchanged `BROKER_POSITION` control, so the two stay distinguishable in logs
  and the UI.
- Human text: `Robinhood already holds NVDA as a user-managed real position. Blocked: 5.283683 NVDA shares already held in Robinhood REAL (user managed), $1,142.97.`

**Rule B, formula and denominator:**

```
projected_pct = (known symbol value + planned notional) / (known invested value + planned notional) × 100
```

- **Known invested value** is the gross market value (absolute values) of every position in every KNOWN account: PAPER and REAL, each position
  counted once.
- It is an exposure share, never an account-equity ratio. PAPER equity and REAL equity are never added together.
- **Example:** known $12,000; SNDK $5,000; a planned SNDK $500 gives $5,500 / $12,500 = 44 % → BLOCK.

**Arithmetic:**
- All money is `Decimal`.
- The limit is Decimal text (`UNIFIED_MAX_SYMBOL_EXPOSURE_PCT=20`).
- The comparison is exact, so no binary-float threshold flip is possible. For example, 0.3 / 1.0 is exactly 30 % and is allowed at a 30 % limit.

**No dynamic sizing.** The control never computes a smaller allowed size. Every result is ALLOW or BLOCK, never a resize.

**Fail-safe edges (all deterministic):**

| Case | Behaviour |
|---|---|
| A planned notional that is missing, zero or negative | `BLOCK PLANNED_NOTIONAL_INVALID` (the display pre-check shows "no planned entry" instead) |
| A known position in the candidate symbol without a readable value | `BLOCK EXTERNAL_VALUE_UNREADABLE` when rule A is off (it cannot be bounded) |
| Another symbol without a readable value | excluded and noted; the known total is then a lower bound, so the result is conservative |
| The same account row twice with identical values | counted once |
| The same account row twice with conflicting values | resolved conservatively: the candidate symbol at its largest value, any other symbol at its smallest |
| Two genuinely separate accounts, e.g. Alpaca EXTERNAL NVDA + Robinhood NVDA | summed in the numerator |
| A row without a readable symbol | counted as unreadable, never dropped silently |
| An invalid setting | `BLOCK GATE_CONFIG_INVALID` (fail closed) |

## 3. Data states and the unknown-data policy

| Unified state | Rule A | Rule B |
|---|---|---|
| COMPLETE | evaluated | evaluated |
| STALE_SOURCE | evaluated on the **last known** holdings (a stale snapshot that says NVDA is held blocks) | evaluated on the last known values, marked `basis: STALE`; an allow is `WARN ALLOWED_ON_STALE_DATA` |
| PARTIAL | a **known** holding still blocks; a missing Robinhood is **never** zero, so the check is UNKNOWN | not evaluated: incomplete totals are never turned into a falsely precise share |
| UNAVAILABLE (also a failed or unreadable unified read) | UNKNOWN | not evaluated |

`UNIFIED_STALE_SOURCE_POLICY` decides what UNKNOWN means:
- **`BLOCK_DUPLICATE_ONLY_IF_KNOWN`** (default) allows the entry with `WARN EXTERNAL_DATA_UNKNOWN`, and the reason names what is unknown.
  - Only a known holding blocks.
  - So a Robinhood outage or an expired login does not stop all paper trading.
- **`BLOCK_IF_UNKNOWN`** is an explicit fail-closed opt-in.
  - It blocks every new entry while the external data is unknown.
  - That can last as long as Robinhood is unavailable.

## 4. Ownership

- **Non-paper rows (Robinhood):**
  - They are `USER_MANAGED` by construction and count for rule A.
  - A non-paper row labelled `BOT_MANAGED` (a contract violation) is treated as external, never as the bot's.
- **Alpaca `BOT_MANAGED` and `EXTERNAL` positions:**
  - They stay with the existing executor controls.
  - They count in the concentration total.
- `NEEDS_REVIEW` is counted conservatively, as held.
- Nothing changes ownership. Robinhood rows stay USER_MANAGED, read only, and are never exited.

## 5. Integration and unchanged invariants

`execution/service.py`:
- `_scan_entries()` reads the snapshot once per scan, only when the gate is on, and only when there are READY candidates.
- It sets `ctx.exposure_gate` and `ctx.unified_portfolio` on the existing `EntryContext`.
- It persists the gate's record with the cycle (a blocked evaluation carries `code` and `detail`).
- It stamps every new entry intent with `environment.entry_controls`, which holds the version, contract hash and gate result.

With the gate off, the entry controls are exactly the `entry_controls_v1` set and the executor does not read Robinhood at all.

Unchanged:
- intent-before-submit
- attempt-before-wire
- the deterministic `client_order_id`
- ambiguous-POST handling
- exactly-once
- reconciliation
- exits (the gate is evaluated for entries only)
- Paper Auto and Monitor only (Monitor only never evaluates entries)
- Emergency Stop
- Pause
- the open-positions limit
- the paper-host restriction

The pre-wire recheck still catches a mode switch during evaluation.

## 6. Configuration (append-only `config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED` | `false` | the control runs and can block (`UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=false` keeps Stage 6.9's advisory behaviour) |
| `UNIFIED_BLOCK_DUPLICATE_SYMBOL` | `true` | rule A on |
| `UNIFIED_MAX_SYMBOL_EXPOSURE_PCT` | `20` | Decimal text in (0, 100]; `100` never blocks, which turns rule B off |
| `UNIFIED_STALE_SOURCE_POLICY` | `BLOCK_DUPLICATE_ONLY_IF_KNOWN` | or `BLOCK_IF_UNKNOWN` |

There is still no Robinhood execution setting.

`ROBINHOOD_DUPLICATE_ENTRY_POLICY` stays `ADVISORY | OFF` for the display. When the gate is on, the policy reads `mode: ENFORCED, enforced: true`.

## 7. API, UI, Bot Activity

All routes are GET only, and no payload carries an account label or number.

**API:**
- **`GET /api/unified-portfolio`** gains `entry_gate`:
  - the settings, version and contract hash;
  - per READY ticker, a **display pre-check**: the same pure rule over the same snapshot and the current proposal's planned entry.
  - In Monitor only, this pre-check is the only place the decision is visible.
  - `duplicate_entry_policy.*.enforced` follows the gate.
- **`GET /api/execution`** gains `entry_controls`.

**UI:**
- **TRADE:**
  - The Position card shows `Entry gate · Unified exposure: BLOCKED · EXTERNAL_SYMBOL_ALREADY_HELD · …`, or `WOULD BLOCK · advisory` when the gate is off.
  - The execution line reads `Cannot enter · Unified exposure: …` only when the gate is enforced.
- **POSITIONS:** the unified section shows the gate (ON · enforced / OFF · advisory only) and the READY pre-checks.
- **Bot Activity:** a blocked ticker lists `Unified exposure: <reason code> · <reason>`. The risk list shows the control's last result. The current mode
  and the last cycle's mode are unchanged.

## 8. Forward validation metadata

Future paper trades are decided under a different entry policy. The forward validation must never pool them silently with older trades:
- **Every entry intent from Stage 6.9.1 on records `environment.entry_controls`:**
  - `version` (`entry_controls_v2`)
  - `contract_hash`
  - `unified_external_exposure.enabled`
  - `result`, `reason_code`, `unified_state`, `snapshot_at`
- **Every closed trade carries `entry_controls`:**
  - An intent created before Stage 6.9.1 has no record and is labelled `entry_controls_v1`, `recorded: false`. It is never rewritten.
- **The trades CSV export** adds `entry_controls_version`, `unified_exposure_gate_enabled` and `unified_exposure_result`.
- **The cumulative summary** adds `entry_controls` with `closed_trades_by_entry_controls`: v1, v2 with the gate off, and v2 with the gate on, counted apart.

## 9. Freeze, versions and hashes

This stage changes entry semantics on purpose. It does not pretend otherwise.

**Old frozen hash:**
- The Stage 6.1 frozen strategy controls (`forward_validation.frozen_controls()`) keep `b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530`.
- None of their values moved. Every `FROZEN_HASH` pin in the old tests is kept as historical evidence and is not rewritten.

**New version:**
- The entry controls are now versioned separately (`execution/exposure_gate.py`):

| Version | Entry controls |
|---|---|
| `entry_controls_v1` | `EXECUTION_MODE`, `PAPER_ENVIRONMENT`, `SAFETY_GATE`, `PROPOSAL_VALIDITY`, `SESSION_POLICY`, `BROKER_TRADABILITY`, `DUPLICATE_ENTRY`, `BROKER_POSITION`, `BROKER_ACCOUNT`, `MAX_OPEN_POSITIONS`, `BROKER_RECONCILIATION` |
| `entry_controls_v2` | v1 + `UNIFIED_EXTERNAL_EXPOSURE` (the exact semantic delta: one control, appended) |

- `contract_hash` (SHA-256 of the contract JSON) identifies each regime:

| Regime | `contract_hash` |
|---|---|
| gate off (code default) | `2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa` |
| gate on with the default settings | `d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4` |

**The freeze ledger:**
- `tests/freeze_ledger.py` pins every edited file byte for byte, as in Stages 6.8 and 6.9, so the older freeze tests still check what their own stage froze.
- It also gains `SEMANTIC_691`, which names each file whose change alters what the executor does: `execution/exposure_gate.py`, `execution/engine.py`,
  `execution/service.py`, `trader_view/stream.py` and `config.py`.
- A registration is not a claim of equivalence.

**`tests/test_unified_exposure_gate_691.py` F01–F04 pin the delta explicitly against main `98c59e2`:**
- The execution files are **not** byte-equivalent to pre-6.9.1 main.
- The only removed line is one Stage 6.8 display line in `service.py`, now extended with the gate's record.
- `engine.py` gains exactly its seven listed lines.
- Every added `service.py` line is a marked Stage 6.9.1 line or sits inside the two new read-only methods.
- `config.py` is append-only and gains exactly the four settings.
- The v1 hash and both v2 hashes are pinned.

## 10. Rollout, and what to decide before enabling it

**Rollout steps:**
1. Merge.
2. Deploy with the gate **off**. Nothing changes; the advisory view now shows the pre-check.
3. Decide the settings.
4. Set `UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true` in the live overrides file and restart from Monitor only.

**Known properties of rule B, as specified:**
- **The denominator is the KNOWN invested value, not equity.**
  - With the live numbers at deploy time (about $12,435 known invested: Robinhood about $12,424 plus Alpaca F $11.58), a new symbol passes 20 % only if
    its planned notional is ≤ 0.25 × $12,435 ≈ $3,109.
  - A PAPER proposal sized from $100,000 of paper equity (up to 10 % = $10,000) projects to about 45 % and would be **blocked**.
  - Choose the limit with that in mind, or use `100` to enforce rule A only.
- **An empty known portfolio makes any first entry 100 %** of the known invested value, so it blocks at any limit below 100.

## 11. Tests

**`tests/test_unified_exposure_gate_691.py`:**
- **G — the pure rule:**
  - duplicate symbol, the 44 % example, the strict boundary, Decimal exactness, fractional quantities;
  - stale, partial and unavailable data, never treated as zero;
  - ownership; same symbol across accounts; duplicated and conflicting rows;
  - malformed, zero and negative values; a missing notional; an invalid configuration; the contract.
- **E — the real executor on the simulated broker:**
  - block with 0 Robinhood writes and 0 Alpaca orders; an allowed entry on the unchanged exactly-once path; gate off = Stage 6.9;
  - exits continue while entries are blocked; a Robinhood holding never exits; Monitor only never enters or reads;
  - a mode switch during evaluation; Pause and Emergency Stop; reconciliation identical with the gate on or off; crash points;
  - a gateway failure during the cycle; a unified-read failure; one snapshot per scan.
- **W:** write isolation.
- **A:** the API, Bot Activity and forward-validation metadata.
- **F:** the freeze, as in section 9.

**`tests/frontend/exposure_gate.test.cjs`:** the TRADE, POSITIONS and Bot Activity rendering.
