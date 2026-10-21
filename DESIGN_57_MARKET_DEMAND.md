# DESIGN 57 — Market Demand / Buyer Confirmation (deterministic, read only)

Stage 5.7. Base: origin/main `4cd3408` (Stage 5.6.1 merged). Package `demand/`, provider `trader_view/demand_provider.py`, route
`GET /api/terminal/demand/{symbol}`, socket message `demand`, Trader View section **MARKET DEMAND** (separate from PPE).

## 1. Purpose

One deterministic number per ticker, **Market Demand 0 … 100**, answering *"are buyers actually stepping in now?"* — observed buyer
participation and price acceptance, from bars only. 0 = very weak buyer confirmation, 50 = mixed / neutral, 100 = very strong buyer
confirmation. It sits NEXT TO the Stage 5.6 outputs and is never combined with them:

| output | question |
|---|---|
| PPE Stretch | how far is price displaced? |
| PPE Confidence | how credible is the overreaction / dislocation classification? |
| **Market Demand** | are buyers actually stepping in now? |

Exact disclaimer (constant `demand.DISCLAIMER`, shown wherever the number is shown):
**"Market Demand measures observed buyer participation and confirmation, not the probability that the stock will rise."**

## 2. Non-goals (enforced by tests)

Not a probability that price rises, not an expected return, not a reversal probability, not BUY confidence, not a trade
recommendation, not part of PPE (ppe/ is byte-identical to the base commit), not execution logic. Demand never places an order,
never enables Prepare Buy / Sell, never calls a broker, a model or Stage 5.4 research, never inverts into "bearish demand": for an
upside-stretched stock a high score only says buyers are confirming — it is not a short cue. Weights, scales and bands are research
hypotheses and are **not** tuned on future returns in this stage (no grid search, no threshold optimisation, no ML, no LLM).

## 3. Inputs, modes and the session window

Existing data only (no new feed, no new store):
* 1-minute candles with volume from the market adapter (`candles(symbol, limit)`): the simulator's synthetic bars, or the real
  adapter's cached 5-calendar-day IEX minute fetch (one request per symbol per 60 s, already the case in Stage 5.5).
* Completed daily bars (`daily_bars(symbols, lookback_days)`, Stage 5.6 path) — cached 15 min per symbol group.
* The batched quote (`quotes`) for the ticker and its benchmark: price, previous close, session open, today's volume.
* Benchmark hierarchy from `ppe.sectors.resolve_benchmark` (unchanged): SECTOR_ETF → PEER_BASKET (≥ 3 peers, equal weight) →
  MARKET (`SPY`, labelled "SPY (market fallback)") → NONE. The fallback is always shown.
* `analysis.indicators` / `ppe.features` are reused for nothing new here except `clip`; VWAP and the intraday features do not exist
  elsewhere in the project and are implemented once in `demand/session.py`.

New adapter method (both adapters, additive): `session_candles(symbol, sessions=5) -> Dict[str, List[Candle]]` — bars grouped by
session key, newest key = the current session. Real mode: the cached minute bars grouped by America/New_York date, regular hours only
(09:30 ≤ t < 16:00). Simulated mode: the current synthetic session = the simulator's history; prior sessions are deterministic
synthetic 390-bar sessions (U-shaped volume profile) generated from the same seed — labelled SIMULATED_DATA like everything else.

A bar is **completed** when `final` is true and its minute has closed (`time + 60 ≤ now`). The live bar is never used for
persistence metrics; the current price comes from the quote (fallback: the last completed close).

**Mode**
* `INTRADAY_MODE` when the current session has ≥ `MIN_SESSION_BARS = 30` completed bars.
* otherwise `DAILY_MODE`: only completed daily bars (date < the current US session date) are used; components B and C are
  NOT_AVAILABLE (`INTRADAY_ONLY`), so coverage is exactly the 0.60 floor when A, D, E and F are all available. The UI shows the mode;
  daily mode never implies real-time confirmation.

## 4. Components, weights, exact formulas

Each component returns `score ∈ [0, 100]` (50 = neutral), `status` AVAILABLE | NOT_AVAILABLE (+ `code`), `raw` metrics and a
one-line `reason`. `clip(x, lo, hi)` is the usual clamp.

| key | label | weight |
|---|---|---|
| `relative_volume` | Relative volume / participation | 0.20 |
| `vwap_acceptance` | VWAP / price acceptance | 0.20 |
| `intraday_structure` | Intraday structure | 0.20 |
| `range_strength` | Range strength | 0.15 |
| `relative_strength` | Relative strength vs sector | 0.15 |
| `follow_through` | Follow-through / persistence | 0.10 |

### A — relative volume / participation (0.20)
* Intraday: `m` = completed bars in the current session, `cum_today = Σ volume` of those bars. `expected(m)` = **median over prior
  sessions** of the cumulative volume of each prior session's first `m` bars (sessions with fewer than `m` bars are skipped; at least
  `MIN_PRIOR_SESSIONS = 2` valid prior sessions, otherwise NOT_AVAILABLE `NO_VOLUME_PROFILE`). `rv = cum_today / expected(m)`.
  10:00 cumulative volume is therefore compared with 10:00 cumulative volume of prior sessions, never with a full-day average.
* Daily: `rv = V_last / median(V of the 20 completed sessions before it)` (needs 21 completed bars with volume; else
  `INSUFFICIENT_HISTORY` / `NO_VOLUME`).
* `p = clip((rv − 1) / 1.0, −1, +1)` (participation: 0.0x → −1, 1.0x → 0, ≥ 2.0x → +1).
* Direction `d`: intraday +1 if current price ≥ session VWAP (if VWAP unavailable: sign of price − session open), −1 if below, 0 if
  equal / unknown; daily: sign(close − open) of the last completed session.
* `score_A = 50 + 30·p·d` if `p > 0`, else `50 + 20·p`. Range [20, 80]: heavy volume with price above VWAP → up to 80, heavy volume
  with price below VWAP → down to 20 (selling participation is not demand), thin volume → 30 … 50 whatever the direction. **Volume
  alone can never produce extreme demand**: with every other component neutral the total is at most `0.2·80 + 0.8·50 = 56` (MIXED).

### B — VWAP / price acceptance (0.20, intraday only)
* Session VWAP is cumulative and point-in-time: `vwap_i = Σ_{j≤i} tp_j·v_j / Σ_{j≤i} v_j`, `tp = (high + low + close) / 3` over
  completed session bars; `Σ v = 0` → NOT_AVAILABLE `ZERO_VOLUME`.
* `dist = (price − vwap_m) / (session_high − session_low)` (range over completed bars plus the current price; zero range → 0).
* `above_frac` = share of completed session bars whose close ≥ their own `vwap_i`.
* `hold` = consecutive most-recent completed bars with close ≥ `vwap_i` (cap 10); `below` = the same below VWAP (cap 10).
* `reclaim` = +1 if the latest cross from below to above VWAP inside the last 60 completed bars was followed by ≥ 3 completed closes
  that stayed ≥ VWAP; −1 if the latest cross closed back below within 3 bars (rejected); 0 if no cross.
* `score_B = clip(50 + 20·clip(2·dist, −1, 1) + 20·(2·above_frac − 1) + 10·hold/10 − 10·below/10 + 5·reclaim, 0, 100)`.
  One tick above VWAP after a session below it: `≈ 50 + 2 − 16 + 1 + 0 = 37` (moderate, not high). A sustained hold: `≈ 93`.

### C — intraday structure (0.20, intraday only; needs 30 completed session bars)
Last 30 completed bars split into three consecutive windows of 10 (W1 oldest … W3 newest):
* `L_k = min low`, `H_k = max high` of each window; `hl = +1` if `L1 < L2 < L3`, `−1` if `L1 > L2 > L3`, else 0; `hh` likewise on highs.
* `up_frac` = share of the last 10 completed bars whose close > the previous close.
* Breakdown test: `S` = min low of the session bars before W3; if any W3 low `< S`: `reclaim = +1` if the last W3 close is strictly
  `> S` (failed breakdown reclaimed), `−1` if it is `< S` (breakdown held), 0 if it sits exactly at `S`; no break → 0.
* `score_C = clip(50 + 15·hl + 15·hh + 10·(2·up_frac − 1) + 10·reclaim, 0, 100)`. No pattern names, no future bars.

### D — range strength (0.15)
* Intraday: `close_location = (price − session_low) / (session_high − session_low)` with the current price inside the range; the
  session is flagged `complete = false` until the close. Daily: the last completed session's `(close − low) / (high − low)`.
* `move` = price − session open (daily: close − open). Zero range → NOT_AVAILABLE `ZERO_RANGE`.
* `score_D = 100·close_location`, capped at 60 when `move < 0` (closing near the high of a down session is at most mild confirmation).

### E — relative strength vs sector (0.15)
* Benchmark per §3. Horizons: intraday mode `rel_session = (P/prev_close − 1) − (B/B_prev_close − 1)` from the batched quotes
  (basket: mean of member session returns) and `rel_5d` from completed daily bars; daily mode `rel_1d` and `rel_5d` from completed
  daily bars (basket via `ppe.sectors.basket_bars`). Intraday 5-minute / 15-minute benchmark returns are deliberately not in V1 (they
  would need benchmark minute bars, i.e. extra requests per peer).
* `score_E = 50 + 50 · Σ_h 0.5·clip(rel_h / scale_h, −1, 1) / Σ_h 0.5` over the available horizons; scales: session 1.5 %, 1 d 2.0 %,
  5 d 5.0 %. No benchmark → NOT_AVAILABLE `NO_BENCHMARK`; no computable horizon → `BENCHMARK_NOT_ALIGNED`.

### F — follow-through / persistence (0.10)
* Intraday (≥ 20 completed session bars): `streak` = consecutive most-recent completed bars on the current side of VWAP (cap 10; side
  `s = +1` above, `−1` below; without VWAP the session open is the reference); `hl_kept = +1` if the min low of the last 10 bars > the
  min low of the previous 10, `−1` if lower, 0 if equal; `ft_frac` = share of the last 10 bars with close ≥ previous close.
  `score_F = clip(50 + 20·s·streak/10 + 15·hl_kept + 15·(2·ft_frac − 1), 0, 100)`. A one-bar bounce (streak 1) adds 2 points only.
* Daily (≥ 6 completed sessions): `streak` = consecutive sessions with close > previous close (cap 5; or down, `s = −1`), `hl_kept` =
  last session's low vs the min low of the previous 3 sessions, `ft_frac` over the last 5 sessions;
  `score_F = clip(50 + 20·s·streak/5 + 15·hl_kept + 15·(2·ft_frac − 1), 0, 100)`.

## 5. Combination, coverage, labels

`score = round(Σ w_i·s_i / Σ w_i)` over AVAILABLE components; `coverage_weight = Σ w_i` of the available ones. Coverage
`< MIN_COVERAGE_WEIGHT = 0.60` → `status = INSUFFICIENT_DATA`, `score = null`, `label = null`. Missing components are never filled
with 50. Key factors: up to four component reasons ranked by `|s_i − 50|·w_i` (only components with `|s_i − 50| ≥ 5`).

Bands on the rounded integer: 0–24 VERY_WEAK · 25–39 WEAK · 40–59 MIXED · 60–74 STRENGTHENING · 75–89 STRONG · 90–100 VERY_STRONG.

Result status: AVAILABLE | SIMULATED_DATA (simulator; score still shown, always labelled) | INSUFFICIENT_DATA | NOT_AVAILABLE.

## 6. Output contract (pydantic, `extra="forbid"`)

`DemandResult{version "demand_v1", symbol, as_of, status, mode (INTRADAY_MODE | DAILY_MODE | NONE), score, label, coverage_weight,
components{key → DemandComponent{key, label, weight, status, score, raw, reason, code}}, key_factors[], sources{market_data,
price_source, benchmark, benchmark_kind, benchmark_label, bars_through, session{date, complete, bars_completed, open, high, low,
vwap, prior_sessions}, computed_at, mode}, weights, disclaimer, note}`. There is no field named buy, sell, recommendation,
probability, expected_return, target or signal (tested).

## 7. Trader View integration and cadence

* `TerminalState.demand`, `GET /api/terminal/demand/{symbol}` (GET only; the terminal router gains no other verb), socket message
  `demand` (symbol-bound: dropped by the browser unless it carries the active ticker), sent on the demand cadence and on `refresh`.
* Cadence: `TRADER_DEMAND_REFRESH_SECONDS = 30` (result cache per symbol; never per quote tick). Session candles come from the
  adapter's existing bar cache (real mode: no request beyond the Stage 5.5 one per 60 s); daily bars are cached for
  `PPE_BARS_CACHE_SECONDS`; quotes come from the batched 3 s quote cache. Daily mode recomputes on ticker switch / refresh / cadence.
* UI: a separate **MARKET DEMAND** section under PPE: score, band, coverage, mode chip (INTRADAY / DAILY), SIMULATED tag when
  simulated, key factors, the exact disclaimer, "Data used" details (source, benchmark, bars through, session, computed at). It is never
  merged visually with PPE Confidence.

## 8. Validation (bounded, point-in-time, descriptive)

* **DAILY FALLBACK VALIDATION** (`python -m demand.replay --db <main db> --out <dir>`): the Stage 3.2 point-in-time store
  (82 datasets, 2024-06-03 → 2026-09-28, read only), every symbol except SPY, each session evaluated in DAILY_MODE with bars strictly
  before it; checks: score distribution and band frequencies (extremes uncommon), low / high demand vs close location and relative
  volume by band, volume-only bound, relative-strength direction, missing-volume handling, no lookahead (future bars do not change a
  score), outcome diagnostics 1 d / 5 d / 20 d by band (descriptive only; never used to tune).
* **INTRADAY VALIDATION**: the project has **no intraday history store** and the real feed is not exercised on the development
  machine, so intraday behaviour is validated on deterministic synthetic sessions only — constructed scenarios (weak / strong
  acceptance, brief vs held reclaim, structure, one-bar bounce) and simulator sessions across a diversified symbol list at several
  checkpoints — and labelled SYNTHETIC in the report. Daily bars are never presented as validating VWAP or intraday structure.
* Universe: the stored list (semiconductors, mega-cap tech, financials, healthcare, consumer, industrials, energy …), never SNDK/NVDA
  alone.

## 9. Anti-lookahead rules

Every feature at time T uses completed bars with `time + 60 ≤ T` (intraday) or `date < session date` (daily); the expected volume
curve uses prior sessions only; benchmark bars are sliced to the same cut-off; outcomes are computed separately after the score and
never feed back. Tests append future bars (stock and benchmark) and assert the score at T is unchanged.

## 10. Limitations and safety

No bid/ask microstructure in V1 (IEX quotes and synthetic simulator spreads are not reliable enough; no order-book inference);
intraday relative strength limited to the session horizon; session window is clock-based ET (no holiday calendar); the real
Alpaca minute-bar path is covered by fake-client tests only on this machine; simulator sessions are synthetic. Read only: no order
path, no broker call, no model call, no research run, PPE untouched. The disclaimer is part of every result.
