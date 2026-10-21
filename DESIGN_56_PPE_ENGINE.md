# Stage 5.6 — PPE Engine (Price Position & Excess), deterministic V1

**Disclaimer (exact, shown with every result):**
"PPE Confidence measures confidence in the overreaction classification, not the probability that the stock will rise."

## 1. Purpose

Two separate, deterministic outputs for one ticker at one point in time:

| Output | Range | Question it answers |
|---|---|---|
| **PPE Stretch** | −100 … +100 | How unusually stretched is the current price move relative to this stock's own normal behaviour and its market / sector context? |
| **PPE Confidence** | 0 … 100 | Given the STORED Stage 5.4 evidence, how confident are we that this stretched move is better described as an overreaction / dislocation than as a fundamental repricing? |

Philosophy: official / market / broker reads = data; Python = calculations and rules; LLM = explanation only (none in this stage);
risk and safety = deterministic constraints; the user decides.

## 2. Non-goals (enforced by tests)

PPE never places an order, never enables Prepare Buy / Sell, never recommends BUY or SELL, never produces a probability that
price rises, an expected return or a reversal probability, never claims a bottom or a top, never runs Stage 5.4 research, never
calls a model, never calls a broker. Weights and thresholds are a research hypothesis; nothing is tuned on future returns.

## 3. Inputs and point-in-time convention

* Completed **daily** bars (date, open, high, low, close, volume), ascending. Source: the existing read-only Alpaca market-data
  client (`data.market_data`, adjusted, IEX) in live mode; the Stage 3.2 point-in-time bar store (`historical_daily_bars`) in
  replay; the deterministic simulator's synthetic daily series in simulated mode (always labelled `SIMULATED_DATA`).
* `as_of` (session date) and, in live mode, the latest price plus today's open when known.
* **Baseline convention:** every indicator baseline is computed from the sessions strictly before the session being evaluated.
  Replay at session T: the evaluated session is bar T (close, open, volume); baselines use bars ≤ T−1. Live mode: the evaluated
  session is the one in progress (price = latest quote, open = today's open if available); baselines use all completed bars.
* The engine itself discards any bar dated after `as_of` (`point_in_time`), so a caller that passes future bars cannot change a
  historical score (tested).
* Evidence is the latest COMPLETED Stage 5.4 packet with `created_at ≤ as_of`; later packets are invisible at T (tested).

## 4. PPE Stretch

### 4.1 Components, weights (hypothesis, not optimised)

| Key | Weight | Raw metric |
|---|---|---|
| A `price_dislocation` | 0.30 | (P − EMA20) / ATR20 |
| B `abnormal_return` | 0.20 | robust z of 1-day and 5-day return vs the stock's own trailing distribution |
| C `relative_sector_move` | 0.15 | robust z of (stock − benchmark) 1-day and 5-day returns |
| D `volume_shock` | 0.15 | completed-session volume / median of the previous 20 sessions, signed by that session's move |
| E `gap_extension` | 0.10 | (open − prior close)/ATR20 and (P − prior close)/ATR20 |
| F `short_term_exhaustion` | 0.10 | RSI14, consecutive sessions, close position in the 10-session range |

EMA and ATR use the project's existing causal implementations (`analysis.indicators.ema/atr/rsi`, Wilder smoothing, `adjust=False`,
`min_periods` = period). EMA span 20; ATR period 20 (the dashboard's 14 is not reused here); RSI period 14.

### 4.2 Normalisation (deterministic, monotonic, bounded)

`bounded(x, F) = clip(x / F, −1, +1) × 100`.

* **A:** `raw = (P − EMA20) / ATR20`, score = `bounded(raw, 4.0)` → −4 ATR = −100, −2 ATR = −50, 0 = 0, +4 ATR = +100.
* **B:** `r1 = P / c[T−1] − 1`, `r5 = P / c[T−5] − 1`; history = 1-day and (overlapping) 5-day returns of the trailing 250 closes
  before the evaluated session (minimum 60 observations). Robust z = (r − median) / (1.4826 × MAD); if MAD = 0 the population
  standard deviation is used; if that is 0 too the component is NOT_AVAILABLE (`ZERO_VARIANCE`). Each z is capped at ±4;
  `combined = 0.5·z1 + 0.5·z5`; score = `bounded(combined, 4.0)`.
* **C:** benchmark closes aligned by date; `rel1 = r1_stock − r1_bench`, `rel5 = r5_stock − r5_bench`; the same robust z
  construction on the trailing 250 aligned sessions (minimum 60); z capped at ±4, `combined = 0.5·z1 + 0.5·z5`, score =
  `bounded(combined, 4.0)`. Requires the benchmark to have the latest prior session (no misaligned comparison).
* **D:** `ratio = volume_session / median(volume of the 20 sessions before it)`; `m = clip((ratio − 1) / 3, 0, 1)` (ratio ≤ 1 → 0,
  ratio ≥ 4 → 1); `direction = sign(session return)` with |return| < 0.1% → 0; score = `direction × m × 100`. Elevated volume
  only ever adds magnitude in the direction of that session's move. In live mode the session used is the **last completed
  session** (partial-day volume is never compared with full-day medians; this is labelled in the output).
* **E:** `ext = (P − prior close) / ATR20`; `gap = (open − prior close) / ATR20` when the open is known; `raw = 0.5·gap + 0.5·ext`
  (or `ext` alone, flagged, when the open is unavailable); score = `bounded(raw, 2.5)`. A and E share ATR20 but measure different
  things (distance from trend vs size of today's move); the 10 % weight keeps the overlap small.
* **F:** `rsi_part = clip((RSI14 − 50) / 30, −1, 1)` on closes + P; `streak_part = sign × clip(n / 6, 0, 1)` where n = consecutive
  same-direction closes ending with P; `range_part = 2 × ((P − low10) / (high10 − low10)) − 1` over the previous 10 sessions
  plus P (0 when the range is flat); `raw = 0.5·rsi + 0.25·streak + 0.25·range`; score = `clip(raw, −1, 1) × 100`. RSI 20
  contributes downside stretch; it is never read as a buy signal.

### 4.3 Minimum data

| Component | Requirement | Otherwise |
|---|---|---|
| A, E | ≥ 25 completed sessions and ATR20 > 0 | NOT_AVAILABLE (`INSUFFICIENT_HISTORY` / `ZERO_ATR`) |
| B | ≥ 65 completed sessions (60 return observations), non-zero dispersion | NOT_AVAILABLE |
| C | deterministic benchmark + ≥ 65 aligned sessions, benchmark covers the latest prior session | NOT_AVAILABLE (`NO_BENCHMARK` / `INSUFFICIENT_HISTORY`) |
| D | volume on the session and the 20 before it, median > 0 | NOT_AVAILABLE (`NO_VOLUME`) |
| F | ≥ 15 completed sessions | NOT_AVAILABLE |

### 4.4 Combination and coverage

Weighted average over AVAILABLE components only, renormalised: `score = Σ wᵢ·sᵢ / Σ wᵢ (available)`, clipped to [−100, 100].
`coverage_weight = Σ wᵢ (available)`. If coverage < **0.60** → status `INSUFFICIENT_DATA`, score `null`. Display score is rounded
to 1 decimal; `raw_score` keeps 6 decimals. `key_factors` = the up to three largest |wᵢ·sᵢ| contributions with |sᵢ| ≥ 10, as
human text (e.g. "2.6 ATR below EMA20", "5d return unusually negative (robust z −2.4)", "−6.2% vs SOXX over 5d").

### 4.5 Stretch bands (boundaries explicit and symmetric)

`score < −75` EXTREME_DOWNSIDE · `−75 ≤ score < −40` DOWNSIDE_STRETCHED · `−40 ≤ score ≤ +40` NORMAL ·
`+40 < score ≤ +75` UPSIDE_STRETCHED · `score > +75` EXTREME_UPSIDE.

## 5. Sector mapping policy (deterministic, never chosen by a model)

1. Sector = `data.sector_map.get_sector_for_symbol`, else `config.EVIDENCE_SECTOR_EXTENSIONS` (SNDK → Semiconductors), else
   `config.PPE_SECTOR_EXTENSIONS` (AAPL → Software, whose ETF XLK holds AAPL). Explicit tables only; nothing is guessed.
2. Benchmark hierarchy: **SECTOR_ETF** (the curated sector ETF, when its bars are available) → **PEER_BASKET** (equal-weight
   daily-return index of the curated peers with available bars, excluding the symbol, at least 3 peers) → **MARKET**
   (`config.MARKET_PROXY_SYMBOL`, SPY, explicitly labelled "market fallback") → **NONE** (component C NOT_AVAILABLE).
3. The benchmark symbol and kind are always surfaced in `sources`; SPY is never used silently as a sector proxy.

## 6. PPE Confidence (deterministic; stored evidence only)

Statuses: `STRETCH_NOT_AVAILABLE` (no stretch), `EVIDENCE_NOT_AVAILABLE` (no COMPLETED packet created at or before as_of),
`NOT_APPLICABLE` (|stretch| ≤ 40: there is no stretched move to classify), `AVAILABLE`.

Evidence snapshot read from the packet and its items (read only): cause verdict and sector `explained_share`, strength, ladder
rung statuses 1–6, counts of *supported* items (epistemic FACT or INFERENCE, tier ≤ 3, not computed METRIC/EVENT kinds) by
direction for company categories, GUIDANCE kind and supply-chain category/roles, contradictions, item count, `age_days`
= as_of − created_at.

"Confirming" evidence = evidence in the **same direction as the move** (DOWN items for a downside stretch, UP items for an
upside stretch). Start at 50 and add contributions:

| Rule | Contribution |
|---|---|
| cause MARKET_WIDE_DOMINANT | +20, plus round(15 × explained_share) |
| cause MIXED | +5, plus round(15 × explained_share) |
| cause NOT_SEPARABLE | 0 |
| cause NO_MATERIAL_DECLINE | −10 for a downside stretch, 0 for upside |
| cause NOT_AVAILABLE | −10 (cap 40) |
| cause COMPANY_SPECIFIC_DOMINANT | −25 (cap 35) |
| confirming company evidence (n items) | −15 × min(n, 2) (cap 40) |
| confirming guidance | −20 (cap 30) |
| confirming supplier / customer evidence | −10 |
| rung 4 company fundamentals improving SUPPORTED / PARTIAL | +10 / +5 for a downside stretch (sign reversed for upside) |
| rung 5 guidance confirming recovery SUPPORTED / PARTIAL | +10 / +5 (sign reversed for upside) |
| rung 6 market confirmation SUPPORTED | +5 (sign reversed for upside) |
| strength STRONG / MODERATE / WEAK | +10 / +5 (caps at 84: VERY_HIGH needs STRONG) / 0 (caps at 50) |
| freshness: age ≤ 3 d / ≤ 10 d / ≤ 30 d / > 30 d | +5 / 0 / −10 (cap 60) / −20 (cap 45) |
| rungs 4 and 5 both NOT_AVAILABLE (missing company evidence) | −15 (cap 50) |
| fewer than 5 items (low coverage) | −10 |
| contradictions | −5 each, at most −20 |
| |stretch| ≥ 75 | +5 |

`score = clip(min(50 + Σ contributions, all applicable caps), 0, 100)`, rounded to an integer. High Stretch alone cannot
produce high Confidence (its only effect is +5), and WEAK evidence never raises it. Every applied contribution and cap is
returned as a reason.

Bands: 0–24 VERY_LOW · 25–49 LOW · 50–69 MODERATE · 70–84 HIGH · 85–100 VERY_HIGH. HIGH means confidence in the
overreaction classification, not probability of recovery.

## 7. Output contract

`PPEResult {version, symbol, as_of, status, stretch {status, score, raw_score, label, coverage_weight, direction, components{…},
key_factors}, confidence {status, score, label, reasons, contributions, caps, evidence}, sources {mode, market_data,
price_source, bars_through, sessions_used, benchmark, benchmark_kind, evidence_packet_at, computed_at}, weights, disclaimer,
note}`. Top-level status: `SIMULATED_DATA` whenever the inputs are simulated (never looks real), else `AVAILABLE`,
`INSUFFICIENT_DATA` or `NOT_AVAILABLE`. There is no BUY/SELL field and no expected-return or probability field (tested).

## 8. Historical validation methodology (Phase 4)

* Replay tool `ppe/replay.py`: strictly point-in-time per session (bars ≤ T, benchmark ≤ T, packets created ≤ T end of day).
* Data: the Stage 3.2 point-in-time store in the main database, read only (82 symbols, 2024-06-03 → 2026-09-28, Alpaca IEX
  adjusted); benchmark = sector ETF if stored, else PEER_BASKET from stored peers, else SPY labelled MARKET.
* Universe: every stored symbol except SPY (semiconductors, mega-cap tech, financials, healthcare, consumer, industrials,
  energy, communication, utilities, materials, real estate). Evaluation starts once 65 completed sessions exist.
* Checks: distribution centred near 0; extremes (|score| > 75) uncommon; downside extremes coincide with objectively unusual
  negative 5-day returns and upside extremes with unusual positive ones; the sector component behaves as designed; missing
  components renormalise; no lookahead (perturbation tests).
* **Outcome diagnostics** (1d / 5d / 20d forward return, excess vs benchmark, win rate, MFE / MAE over 20 sessions) are
  computed separately, labelled OUTCOME DIAGNOSTICS, never used to compute or tune PPE, and never read as predictive power.

## 9. Anti-lookahead and anti-overfit rules

Baselines exclude the evaluated session; the engine slices by `as_of`; evidence is filtered by `created_at`; forward returns
live only in the diagnostics table. No weight search, no threshold selection by future returns, no per-ticker thresholds, no
model training. Confidence thresholds are documented constants.

## 10. Trader View integration (Phase 5)

`TerminalState.ppe` carries the PPEResult; `GET /api/terminal/ppe/{symbol}` returns it; the socket pushes a symbol-bound `ppe`
message on subscribe, on refresh and every `TRADER_PPE_REFRESH_SECONDS` (60 s) for the active ticker — never per quote tick.
Daily bars are cached per symbol for 15 minutes in real mode; the simulator's daily series is deterministic. The panel shows
Stretch score and band, Confidence score and band, coverage, up to three key factors, the evidence summary, the sources
(market data, benchmark, evidence packet time, computed time) and the disclaimer; simulated mode is labelled SIMULATED DATA
in the panel and in `status`. Prepare Buy / Prepare Sell stay disabled.
