# Stage 6.4.1 — Cross-sectional validation (validation_v3)

> **Backtest results are hypothetical.** PASS means eligible for human review - never a promotion, never a forecast. Nothing in this stage
> places an order, enables Paper Auto, or changes execution, risk or safety. Value + Quality's rules, data and split are unchanged.

## 1. The problem

The Stage 6.2 harness judges a strategy on its trades: mean R per trade, a per-trade t-statistic, OOS expectancy after friction. That is the
right unit for an **event-driven** strategy, whose trades are separate events. A **cross-sectional portfolio** strategy is different: each
rebalance selects ~20 stocks at once, and those 20 legs share the same month - the same market move, macro news, sector shocks, liquidity and
rebalance timing. Twenty legs in January are not twenty independent January experiments; they are closer to one. A per-leg t-statistic
therefore overstates the evidence roughly by the square root of the portfolio size (Value + Quality: TRAIN t 6.30 per leg, while the
portfolio's excess over its own universe had a t of 0.19 per month).

Worse, a positive absolute return says nothing about selection. A long-only portfolio in a rising market earns money whatever it selects;
beating SPY can come from the universe (Value + Quality ranks a survivor universe of today's large caps, which beat SPY on its own). The
question a selection strategy must answer is: **did the selection beat the same universe held without the selection?**

## 2. Evaluation modes

The adapter DECLARES its mode (`evaluation_mode`); the harness never infers it from trade counts.

| Mode | Unit of evidence | Primary statistic | Used by |
|---|---|---|---|
| `EVENT_DRIVEN` (default) | one trade | per-trade mean R and t (validation_v1/v2 policy, unchanged) | Downside Dislocation Recovery, the fixtures |
| `CROSS_SECTIONAL_PORTFOLIO` | one rebalance period | period excess return over the SELECTION CONTROL | Value + Quality (future: Momentum) |

For a cross-sectional strategy the per-leg metrics are still shown, as a trade-level description, labelled **NOT SELECTION-EDGE EVIDENCE**.

## 3. The control contract

A cross-sectional adapter declares a `control_definition` (role `SELECTION_CONTROL`) and exposes its `selection_snapshots(dataset)`: for every
rebalance, the eligible universe and the selection. The control shares everything with the strategy except the selection:

| Shared | How |
|---|---|
| investable universe and eligibility filters | the control holds exactly the stocks eligible at that rebalance (the strategy's own snapshot) |
| rebalance dates | the strategy's rebalance sessions |
| execution timing | each position enters at the open of its first session after the rebalance and exits at the open of its first session after the next rebalance (the last period: the last close) - exactly the strategy legs' rule |
| friction | the run's friction model, charged on real orders only: a position entering the control's membership pays entry friction, a position leaving it pays exit friction; a continuing position pays none (the strategy's ENTER / HOLD / EXIT convention) |
| capital basis | `EQUAL_CAPITAL_BUY_AND_HOLD` on both sides: equal capital per position at the period's entry, held to the period's exit; the period return is the mean of the position returns (the simulator's integer-share rounding is not part of the comparison) |

What the control omits is the ranking: it holds every eligible stock. For Value + Quality the control is the existing **equal-weight
eligible universe**. **SPY is a MARKET BENCHMARK only** - shown for context, never used as selection-edge evidence.

## 4. Period aggregation

One observation per rebalance period: `strategy_period_return` (mean net return of the period's legs), `control_period_return` (mean net
return of the control's positions), `benchmark_period_return` (SPY, open to open) and `excess_period_return = strategy - control`. Gross
(friction-free) returns are kept on both sides to show whether friction erases the excess. The 20 legs of a month are ONE observation.

Periods are assigned to TRAIN (rebalance on / before the TRAIN end and exit on / before it) or OOS (rebalance after the TRAIN end). A period
straddling the TRAIN end holds the purged boundary legs: it is excluded on BOTH sides, as is a period with no eligible stock or with a
missing leg. Every period carries its status and reason; nothing is dropped silently.

## 5. Metrics (TRAIN / OOS / ALL)

`period_count`, `strategy_total_return`, `control_total_return`, `benchmark_total_return` (all compounded over the periods), the mean period
returns, `mean_excess_period_return`, `excess_t_stat` (one excess per period, sample standard deviation:
`mean / (stdev / sqrt(n))`; not reported below `CS_MIN_PERIODS_T` = 6 periods), `excess_win_rate`, `active_volatility` (stdev of the period
excess, annualized by the rebalance frequency), `worst_relative_period`, `max_relative_drawdown` (of strategy / control wealth),
`cumulative_excess_return` (strategy total - control total) and `relative_return` (strategy wealth / control wealth - 1), gross vs net mean
excess, turnover on both sides, and the ordinary portfolio metrics per side (annualized volatility and maximum drawdown of the period path).

## 6. Selection-edge policy (fixed before it was applied to any strategy)

`selection_edge_status` is separate from the integrity status and from the absolute performance verdict.

| Status | Rule (evaluated in this order; OOS decides, TRAIN and ALL qualify) |
|---|---|
| `NOT_APPLICABLE` | an EVENT_DRIVEN strategy |
| `INVALID` | the control-parity audit (section 8) failed: no selection-edge conclusion (the absolute research result stands) |
| `INSUFFICIENT_DATA` | fewer than **12** OOS periods |
| `NOT_ESTABLISHED` | OOS mean excess <= 0 (incl. a gross excess erased by friction: `FRICTION_ERASES_EXCESS`), or OOS excess t < **1.0** (statistically negligible), or no OOS t |
| `WEAK` | OOS mean excess > 0 with 1.0 <= t < 2.0; or one of the ESTABLISHED conditions below fails |
| `ESTABLISHED` | OOS t >= **2.0** AND at least **24** periods in total AND the OOS excess not concentrated (section 7) AND TRAIN not strongly contradictory (TRAIN mean excess < 0 with t <= **-2.0**) |

t >= 2 is never the only rule; a t of 0.15 can never be "established". The thresholds are conventional and were written into
`strategy_validation/__init__.py` (`CS_*`) and this document in a commit preceding the first evaluation of any result under them.

**Overall verdict of a cross-sectional strategy** (the absolute verdict is the unchanged validation_v2 policy on the legs):

| Absolute performance | Selection edge | Overall verdict |
|---|---|---|
| INVALID / INSUFFICIENT_DATA / FAIL | any | the absolute verdict |
| PASS / MIXED | ESTABLISHED | the absolute verdict |
| PASS / MIXED | WEAK | MIXED (`SELECTION_EDGE_WEAK`) |
| PASS / MIXED | NOT_ESTABLISHED | FAIL (`SELECTION_EDGE_NOT_ESTABLISHED`: no evidence the selection beats its control) |
| PASS / MIXED | INSUFFICIENT_DATA | INSUFFICIENT_DATA (`SELECTION_EDGE_INSUFFICIENT_DATA`) |
| PASS / MIXED | INVALID | MIXED (`SELECTION_CONTROL_INVALID`) |

So a cross-sectional strategy can only reach PASS (eligible for human review) with an ESTABLISHED selection edge: never on positive absolute
returns, a per-leg t-statistic or beating SPY alone.

## 7. Concentration

On the OOS period excesses (and reported for TRAIN / ALL): if the summed excess is positive and one period carries more than **50 %** of it,
or the mean excess is no longer positive once the best period is removed, the excess is **concentrated** - a warning, and no ESTABLISHED.

## 8. Control-parity audit (independent of the strategy)

Re-derived from the run record, the stored trades, the snapshots and the raw bars:

* `SAME_PERIODS` - one period per rebalance snapshot; every leg's signal is a snapshot rebalance.
* `SAME_UNIVERSE_SNAPSHOT` - every period's legs are exactly the snapshot's selection, a subset of the control's members (the eligible set).
* `SAME_PRICE_TIMESTAMPS` - every leg's entry / exit session and raw entry / exit price equal the control's for that stock and period.
* `SAME_CAPITAL_CONVENTION` - both sides `EQUAL_CAPITAL_BUY_AND_HOLD`; the strategy uses equal-weight sizing.
* `SAME_FRICTION` - the control's friction is the run's friction model; the legs' fills match their ENTER / EXIT flags.
* `NO_MISSING_PERIOD` - every period is INCLUDED on both sides or EXCLUDED on both with a reason; no one-sided period.
* `BOUNDARY_BOTH_SIDES` - the boundary period (purged legs) is excluded on both sides; no included period crosses the TRAIN end; the purged
  legs are exactly the boundary period's legs. (This is the Stage 6.4 bug: the boundary month counted as cash for the strategy only.)
* `PERIOD_ACCOUNTING` - every RULES leg (simulated, purged or skipped) belongs to exactly one period; the counts reconcile.

Any failure makes the selection-edge status `INVALID` (no conclusion about selection); the absolute research run is not invalidated.

## 9. Integrity, storage and versions

* `validation_v1` (Stage 6.2): candidates once, three arms, friction, TRAIN / OOS, audit, verdict.
* `validation_v2` (Stage 6.3.1): boundary purge, accounting invariant, persistence integrity gate.
* `validation_v3` (Stage 6.4.1): evaluation modes; for cross-sectional strategies the control comparison, the selection edge and the overall
  verdict policy above. Event-driven runs are unchanged apart from the recorded mode (`NOT_APPLICABLE`).
* The integrity gate gains `SELECTION_CONTROL`: a cross-sectional v3 record must carry its control definition, period table and parity audit;
  a selection-edge status other than INVALID with a failed parity audit, or an overall verdict that does not follow the policy, blocks
  persistence.
* Stored runs are never rewritten or reinterpreted silently. An older run is re-assessed by a separate, append-only **methodology
  evaluation** record (`strategy_validation_evaluations`) that references the run, re-derives the snapshots from the same dataset, proves
  that they reproduce the run's candidate set (identical candidate set hash) and computes the v3 assessment from the run's stored trades.
  The run keeps its validation_v2 verdict; the Strategy Lab shows both, labelled.

## 10. Value + Quality under validation_v3

Computed after the policy commit (`17e2cb4`), by a methodology evaluation of the final Stage 6.4 run `1660bd5091ad4799a6908b9e8ddc6f2f`
(validation_v2, git `f89e358`). Nothing about the strategy changed: same rules, data (`sha256:e072e67517615fbb8cc42ebb`), split and trades; the
re-derived snapshots reproduce the run's candidate set exactly (`b7e1572aeddd4bd9104d9a808f4aa7e9`). The run row is byte-identical before and
after the evaluation.

| Per rebalance period | TRAIN (2020-09 → 2024-11) | OOS (2025-01 → 2026-10) | All |
|---|---|---|---|
| Periods | 51 | 22 | 73 |
| Strategy | +122.7 % | +54.8 % | +244.7 % |
| SELECTION CONTROL (equal-weight eligible universe) | +115.3 % | +52.2 % | +227.7 % |
| Excess over the control | +7.3 pp | +2.6 pp | +17.0 pp |
| Mean excess per period (net / gross) | +0.073 % / +0.080 % | +0.109 % / +0.120 % | +0.084 % / +0.092 % |
| Excess t-statistic (per period) | 0.32 | **0.18** | 0.35 |
| Excess win rate | 49 % | 55 % | 51 % |
| Active volatility (annualized) | 5.7 % | 9.9 % | 7.1 % |
| Worst relative period | 2024-01: −2.2 % | 2026-06: −7.4 % | 2026-06: −7.4 % |
| Max relative drawdown | 5.2 % | 16.2 % | 16.2 % |
| Concentration | the excess vanishes without its best period | the best period carries more than the whole excess | one period carries 87 % |
| Turnover per period (strategy / control) | 9.7 % / 2.8 % | 10.5 % / 0.3 % | 9.9 % / 2.0 % |
| MARKET BENCHMARK (SPY) | +81.0 % | +31.9 % | +138.7 % |

One period excluded on both sides: the boundary period (rebalance 2024-12-02, exit 2025-01-03: its legs were purged). Control parity 8 / 8.

**Selection edge: NOT_ESTABLISHED** (`EXCESS_NEGLIGIBLE`: OOS mean excess +0.11 % per period, t = 0.18 over 22 periods). **Overall: FAIL** (not
ready for paper; no evidence that the selection beats its control). **Absolute performance: PASS** (the validation_v2 verdict of the run, kept as
recorded). **Lifecycle: Research.** The OOS excess also depends on a single month - without it the OOS excess is negative - so even a larger
t-statistic would not have made it ESTABLISHED.

**Regression against Stage 6.4.** The stored validation_v2 diagnostics read OOS +53.2 % (strategy) vs +51.2 % (universe control); validation_v3
reads +54.8 % vs +52.2 %. The underlying trades are the same; the differences are the methodology, each measured:

| Difference | TRAIN | OOS |
|---|---|---|
| The sessions at the edges of the boundary window (v2 dropped the whole window, including the hand-over sessions 2024-12-03 and 2025-01-03; v3 keeps every session of the included periods, on both sides) | ≈ −0.3 pp on both sides | ≈ +0.9 pp strategy, ≈ +1.0 pp control |
| Equal capital per position on both sides (v2's strategy side carried 1.7 % / 2.8 % uninvested cash from integer shares; its control had none) | +2.3 pp strategy | +0.7 pp strategy |
| The control now pays friction on its membership changes (as the strategy does on its orders) | −0.2 pp control | −0.1 pp control |

The interpretation does not change: v2's per-period t was 0.15 (OOS), v3's is 0.18.

## 11. Implementation notes

* `strategy_validation/cross_sectional.py`: `period_table`, `parity_audit`, `segment_stats`, `concentration`, `selection_edge`, `overall_verdict`,
  `evaluate` (the v3 block of a run) and `assess_stored_run` (a methodology evaluation; raises `EvaluationError` when it cannot be done honestly:
  another dataset, a candidate set that does not reproduce, an event-driven strategy).
* `strategy_validation/engine.py`: the declared mode is checked up front (a cross-sectional adapter must declare its control and snapshots); after
  the absolute verdict, a cross-sectional run gets its `cross_sectional` block and `selection_edge`, and its overall `verdict` follows section 6.
* `strategy_validation/integrity.py`: `SELECTION_CONTROL` (section 9). `strategy_validation/store.py`: `insert_evaluation` (re-checked: the run
  exists and is visible; strategy, version, dataset and candidate set equal the run's; the performance verdict is the run's; the edge and the
  overall verdict follow the policy), `evaluations`, `latest_evaluation`; `latest()` is a read view that attaches the latest evaluation and never
  modifies the stored run JSON. `database/strategy_validation_migrations.py`: `strategy_validation_evaluations` (append-only triggers).
* `strategy_validation/service.py`: `validation_status` is the overall verdict; `performance_verdict`, `selection_edge_status`, `evaluation_mode`,
  `methodology_version` and `methodology_evaluation_id` are separate fields; an older run without an evaluation reads "Not assessed".
* `value_quality/adapter.py`: declarations only (`evaluation_mode`, `control_definition`, `market_benchmark`, `selection_snapshots` from the core's
  own rebalance log). The candidate set hash is unchanged.
* Strategy Lab (`frontend/product_ui.js`, inside the Stage 6.2 block): three sections - Absolute performance (the per-leg table, its t-statistic
  labelled NOT SELECTION-EDGE EVIDENCE), Control comparison (SELECTION CONTROL and MARKET BENCHMARK labelled apart) and Selection edge (status,
  reasons, the primary statistic, the policy, the methodology-evaluation note); the registry badge carries the edge. The Stage 6.4 portfolio
  table is relabelled (market benchmark / selection control) and shown only for an older run without a v3 assessment.
* CLI: `python -m strategy_validation evaluate --run RUN_ID --dataset FILE [--db PATH] [--persist] [--output FILE]`.

## 12. Adversarial review

An independent reviewer read the v3 code (no access to this document's conclusions) and probed it with synthetic data. No look-ahead, no
correlated-observation counting in the edge decision and no use of SPY in it were found; the pre-registered constants are byte-identical to
`17e2cb4`. Findings and resolutions:

| # | Area | Finding | Severity | Resolution |
|---|---|---|---|---|
| 1 | Storage / gate trust | the integrity gate and the evaluation store re-applied the policy to the RECORDED statistics and parity status: forged segments, a parity status contradicting its own checks, or an invented period table could be stored as ESTABLISHED / PASS | HIGH | fixed: `cross_sectional.verify` re-derives everything - the parity status from its checks, the statistics / edge / overall verdict from the period table, the strategy side of every included period from the run's immutable trades, the control's size from the candidate set (tests O01, O02, K01) |
| 2 | Evaluation shadowing | evaluations were ordered by the caller's `created_at`: a forged future timestamp shadowed an honest later evaluation | HIGH (with 1) | fixed: insertion order (`rowid`); a future `created_at` is refused (test K03) |
| 3 | Unfair control | the control's membership came only from the adapter's snapshots and a count check compared the snapshot with itself; a narrowed control (selection + the weakest third) passed parity and turned NOT_ESTABLISHED into ESTABLISHED | HIGH | fixed: the control declares `membership_size_feature` (Value + Quality: `eligible_count`, a candidate feature inside the candidate-set hash); the parity audit and the store compare every period's control size with it (test H02). The tautological check is gone |
| 4 | Missing periods / bars | a control member without an entry bar was dropped while the period stayed INCLUDED (a strategy leg in that situation excludes the period) | MEDIUM | fixed: the period is EXCLUDED on both sides (`CONTROL_MEMBER_NO_ENTRY_BAR`); an INCLUDED period must hold every member (test H03). Value + Quality has no such period |
| 5 | Mode switch | the same strategy version could be re-stored as EVENT_DRIVEN (no selection test) and become the latest run | MEDIUM | fixed: once a version is stored as cross-sectional its mode cannot change; the gate requires the record's mode to equal its adapter's (test M02) |
| 6 | Rounding before thresholds | t was rounded to 4 decimals before the 1.0 / 2.0 / -2.0 comparisons (a raw 1.99996 became ESTABLISHED) | LOW | fixed: the decision statistics keep full precision (test P03) |
| 7 | Parity checks that cannot fail | `NO_MISSING_PERIOD` could not fail on the engine's own table | LOW | fixed with 4 (it now checks the members); the commission conversion differs slightly (notional vs fill x quantity) - immaterial while commissions are 0, documented |
| 8 | Re-assessing a v3 run | an evaluation of a v3 run was accepted and replaced the run's own verdict in the summary; the UI note ("its own verdict is the absolute performance verdict") was false for it | LOW | fixed: evaluations are only for runs of an older methodology (`assess_stored_run` and the store refuse a v3 run; test K03) |

Earlier issues found while building the stage: the boundary month counted as cash on one side only (the Stage 6.4 bug) is structurally
impossible here (one period table for both sides; `BOUNDARY_BOTH_SIDES`, test I01); the Strategy Lab exports were left untouched so the Stage
6.2 pins hold; a concentration text over 100 % was reworded.

Area summary (the 12 areas of the brief): correlated observations - one excess per period (A01, D01); sample inflation - only INCLUDED periods,
each leg in one period (PERIOD_ACCOUNTING); unfair control - 3, 4; boundary - I01; friction - the same model and ENTER / HOLD / EXIT convention
on both sides (C01, SAME_FRICTION); capital - equal capital on both sides (SAME_CAPITAL_CONVENTION); missing periods - 4, every exclusion
reasoned and two-sided; benchmark / control - labelled roles, SPY never in the decision (G01); post-hoc thresholds - committed in `17e2cb4`
before any result (Z01); Value + Quality - declarations only, identical candidate set hash (K01, K02); storage - 1, 2, 8, append-only triggers
(K01); broker isolation - no broker / execution / network import (N01).

## 13. Limitations

* The selection control shares the universe's survivorship bias; it isolates the selection, not the universe. A point-in-time universe is still
  needed for any statement about the universe itself.
* The control is equal-weight; a strategy whose selection is tied to a different natural weighting (e.g. capitalization) would need its own
  declared control. Value + Quality's legs are equal-weight, so the comparison is like for like.
* The store cannot re-derive the control's RETURNS without the dataset; it re-derives the strategy side from the trades and the control's
  membership size from the candidate set. The full control is re-derived only by `assess_stored_run` / the engine from the dataset itself.
* 22 OOS months are a short sample: even a genuine edge of the observed size would need many more periods to reach t >= 2.
