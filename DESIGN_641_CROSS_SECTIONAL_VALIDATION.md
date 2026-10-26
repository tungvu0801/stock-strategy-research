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

(Computed after this policy was committed - section 11.)
