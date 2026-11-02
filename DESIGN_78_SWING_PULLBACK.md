# Stage 7.8 Part B — Swing Trend Pullback research baseline (V1) and its control (C1)

Research only. Synthetic fixtures only in this stage: no real-data run, no fetch, no production database, no registry entry, no live path.
Package `swing_pullback/`; tests `tests/test_swing_pullback_78.py` with `tests/swing_pullback_fixtures.py`. Base: `main` at `d1de751`.

Sources: the master design's D.3 template `SWING_TREND_PULLBACK_V1` and the Stage 7.8 Part A workspace documents
(`STAGE_7_8_OPERATOR_DECISION_PACKAGE.md`; M1–M9 approved as defaults). Four clarifications were made by the operator on 2026-11-01, before any
code was written:
1. C1 omits the whole pullback rule: the trigger and the depth guard.
2. The cooldown stays 9 sessions in every sensitivity neighbour.
3. The price and liquidity rules read the raw series, and the universe keeps one listing per issuer.
4. The folds are 6 equal session counts from the dataset's 200th session.

**V1 is a modified D.3 research baseline, not a faithful implementation of the complete D.3 strategy** (deviations D1–D5 below). No value was
chosen from any result. A changed value is a new `strategy_version`.

## 1. V1 — `SWING_TREND_PULLBACK` 1.0

Each rule is evaluated at the close of session t, from completed daily bars at or before t. The prices are the dataset's all-adjusted bars;
the price and liquidity rules read the raw series in `aux`.

| # | Rule | Value |
|---|---|---|
| 1 | Universe | the 81-stock research universe of Stages 6.4–6.7 (`value_quality.RESEARCH_UNIVERSE`), today's large caps (FIXED_CURRENT_UNIVERSE, `SURVIVORSHIP_BIAS_PRESENT`); one listing per issuer (aux static issuer map: the alphabetically first listing the dataset holds is kept, so GOOG and not GOOGL; a symbol mapped to two issuers refuses the run); SPY for the regime only, never a candidate |
| 2 | Warm-up | no candidate until all of the 200 dataset sessions ending at t carry a bar of the symbol and of SPY; a missing bar is never filled |
| 3 | Price | raw close ≥ $5 |
| 4 | Liquidity | median of raw close × raw volume over the 20 dataset sessions ending at t ≥ $20 M; a missing raw value in the window: no candidate. On IEX volume the rule is NOT_APPLIED (D4). The dataset must declare `volume_feed` SIP or IEX, else the run is refused |
| 5 | Regime | SPY close > SMA200(SPY) |
| 6 | Trend | close > SMA200 and SMA50 > SMA50 of 20 sessions earlier |
| 7 | Pullback rule | (Wilder RSI(2) ≤ 10, or close[t] < close[t−1] < close[t−2] < close[t−3]) and close ≥ 0.97 × SMA50 |
| 8 | Cooldown | after a candidate at session t′, no candidate for the symbol at t′+1 … t′+9 (dataset sessions; signal dates only; a blocked session never restarts it); 9 sessions in every sensitivity neighbour |
| 9 | Entry | the open of t+1 (`NEXT_BAR_OPEN`); an entry opening at or below the stop, or at or above the target, is skipped (the harness) |
| 10 | Stop / target | signal close − 2 × ATR(14) / signal close + 3 × ATR(14), Wilder ATR, fixed at the signal |
| 11 | Time exit | the close of the 10th session in the trade (the entry session is the first): `max_hold_bars` = 10, the adapter's, never a candidate's |
| 12 | Exits | the harness only: gap through the stop at the open, stop, target, stop-first on an ambiguous bar, time, data end |
| 13 | Sizing | the harness research sizing: 0.5 % of 100 000 per trade, capped at 10 %, not compounded; overlapping trades of different symbols allowed |
| 14 | Score | the Strategy Standard needs a 0–100 score; V1 does not rank, so every candidate carries 50 (never read by the RULES arm) |

The indicators are written in plain Python (`swing_pullback/indicators.py`). They reproduce the `analysis/indicators.py` formulas: the RSI
and ATR use the pandas `ewm(alpha = 1/n, adjust = False, min_periods = n)` recursion seeded with the first value, and RSI is 100 when the
average loss is 0. They are causal by construction: a prefix gives the same values. `analysis/` itself is not imported. The SMA is the correctly rounded mean
of each window (`math.fsum` / n). The three strict comparisons with a mean are decided exactly, so a tie never passes: close > SMA200,
SPY close > SMA200, and SMA50 rising.

## 2. C1 — `SWING_TREND_PULLBACK_CONTROL` 1.0

C1 is V1 without rule 7: it omits the trigger and the depth guard. Every other rule, value, exit, the sizing and the cooldown are V1's (the
adapter is a subclass that only switches the pullback rule off). It tests whether the pullback adds anything to trend-following entries. It
is a control, not a strategy: it runs no sensitivity.

## 3. Declared deviations from D.3 (written into every run as research flags)

| # | Deviation | Flag |
|---|---|---|
| D1 | No close-based exit. D.3's "a close above the prior 5-session high" cannot be expressed by the harness. V2 (V1 + that exit) is **deferred**, not implemented and not pre-registered: it needs a separately approved harness exit stage, and its fill timing is UNRESOLVED | `CLOSE_BASED_EXIT_NOT_MODELLED` |
| D2 | No earnings filter: there is no point-in-time earnings history, and no approximation is used; earnings-window trades are included | `EARNINGS_FILTER_NOT_APPLIED` |
| D3 | The stop and the target are measured from the signal close, not from the fill | `STOP_TARGET_FROM_SIGNAL_CLOSE` |
| D4 | The liquidity rule is not applied on IEX volume | `LIQUIDITY_RULE_NOT_APPLIED_IEX_VOLUME` |
| D5 | The portfolio limits (≤ 4 open, ≤ 2 new a day, ≤ 2 a sector) are not simulated; they bind in the Stage 7.9 shadow book | `PORTFOLIO_LIMITS_NOT_SIMULATED` |

Also flagged: `MODIFIED_D3_BASELINE`, `SURVIVORSHIP_BIAS_PRESENT`, `REGULATORY_FEES_NOT_MODELLED`, and `CONTROL_ARM_NOT_A_STRATEGY` for C1. The
harness applies its universe cap to cross-sectional runs only. For these event-driven runs the survivorship flag carries the limit, and the
edge is to be read as NOT_ESTABLISHED.

The EMA / support design is deferred as a separate future strategy.

## 4. The pre-registered experiments (`swing_pullback/plan.py`)

There are exactly **two cost runs** per version: BASE at 5 bps per side and STRESS_2X at 10 bps per side, with commission 0 per order
(modelled explicitly) and no fee per share. Both are configurations of the same version. The store's version identity excludes friction
(tested: it accepts both runs, and refuses a run whose parameters or holding limit differ under the same version). The store cannot see a
changed code constant: the pre-registration hash below guards those. It is recorded in every run's diagnostics and pinned by the tests.
`plan.run()` accepts no other cost and no parameters. It returns the run and never stores it.

The verdict is the harness's: the chronological 70 / 30 split with its boundary purge, ≥ 30 trades, ≥ 20 OOS trades, ≥ 60 OOS sessions,
|t| ≥ 2, and FRICTION_HEAVY 0.50. A PASS also needs positive OOS expectancy in the STRESS_2X run; that comparison happens at review, outside the
code.

## 5. Diagnostics (`swing_pullback/diagnostics.py`; descriptive, never a gate, never used to choose)

* **Folds.** Six consecutive folds of equal session counts, from the dataset's 200th session to its last; the remainder adds one session to
  each of the earliest folds. The boundaries are the same for V1 and C1 and depend on no trade. RULES trades are assigned by signal date.
  Each fold reports trades, mean R, net P/L, the t of R, and whether it is positive (net P/L > 0, the `rotation_walkforward` "return > 0"
  convention), plus the share of positive folds among folds with a trade. The stability bar and the per-fold minimum are decided before
  real data.
* **Sensitivity (V1 only).** The base and the eight pre-registered neighbours, one at a time, ±25 %:
  * RSI threshold 7.5 / 12.5;
  * stop 1.5 / 2.5 ATR;
  * target 2.25 / 3.75 ATR;
  * time exit 8 / 12, with the cooldown still 9.

  They are computed on the **TRAIN view** only (`dataset.until(train_end)`: no later bar exists), at the run's friction and sizing. A
  candidate is purged by the harness's own boundary rule (its symbol's next 10 bars reach past the TRAIN end: dates only, never a price), so
  the base row equals the run's own TRAIN result. The neighbourhood is labelled by the harness's own rule. A SENSITIVE or UNSTABLE label makes the result not promotable. The harness's own neighbour hook is not used, because it would evaluate
  the neighbours on every session, OOS included.
* **Funnel.** The first failing rule per symbol-session.

## 6. What this stage does not do

* No real-data run.
* No data collection or backfill.
* No persistence outside a test's temporary store.
* No registration in `strategy_validation/adapters.py` or `strategy_registry/registry.py`.
* No config key, route, scheduler, UI or execution path.
* No change to the harness, the scanner or any frozen module.
* No Strategy Standard signal to any consumer.
* MONITOR_ONLY, Paper Auto OFF, MANUAL_ARM, Robinhood read-only and Evidence Refresh OFF are unchanged.

## 7. Open before real-data validation (Stage 7.8B)

* **Data.** The dataset must be approved: universe, feed, adjustment, and an existing bundle or a backfill. It must declare `volume_feed` and
  carry raw close / raw volume and a complete issuer map.
* **Stability bar.** The fold-stability bar and the per-fold minimum.
* **Control comparison.** The comparison rule.
* **Fees.** The verified regulatory-fee rates.
* **Registration.** How a real run is invoked and persisted, and whether the registry gets an entry.
* **V2.** V2's rules, if wanted, fixed before any real data is seen.
* **Data integrity.** A corporate-action consistency check of the real bundle (the Stage 6.6 / 6.7 window checks are not part of V1).
* **Calendar.** The real calendar must be checked: one stray date on any symbol blocks 200 sessions of candidates for every other symbol
  (rule 2).

Pre-registration hash (`swing_pullback.preregistration_hash()`): `6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903`.
