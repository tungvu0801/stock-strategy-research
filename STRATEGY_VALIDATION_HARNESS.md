# Strategy Validation Harness (Stage 6.2)

The harness exists to **disprove weak strategies** before they reach paper trading. It is research and validation only: it never places,
previews or cancels an order, never enables Paper Auto, never touches a broker, never calls a model or the network, and never changes a
strategy, a risk / safety setting or a lifecycle status.

> **Backtest results are hypothetical.**
> **PASS does not mean profitable in the future.**
> **PASS does not auto-promote a strategy.** PASS means *eligible for human review for lifecycle advancement*; the lifecycle stays an
> explicit human decision.
> **A failed validation is a valid research result.**

Package: `strategy_validation/` (design: `DESIGN_62_STRATEGY_VALIDATION.md`). Tests: `tests/test_strategy_validation_62.py`,
`tests/frontend/strategy_validation.test.cjs`, browser flows `strategy_validation*`.

## 1. Purpose

Answer, honestly, for one registered strategy on one dataset: after friction and with causal fills, does the edge survive an untouched
out-of-sample slice, with enough trades to mean anything, without depending on look-ahead, a lucky period, ambiguous bars or one parameter
choice - and does every simulated trade check out mechanically? The answer is one of `INSUFFICIENT DATA`, `FAIL`, `MIXED`, `PASS` or
`VALIDATION INVALID`.

## 2. Candidate contract

A strategy is replayed through an **adapter** (`strategy_validation/adapters.py`) that declares its identity (strategy id + version), its
execution assumptions (entry policy, holding limit, signal basis), its critical features and the Strategy Standard v1 condition texts, and turns
a dataset into raw candidates. Candidate generation happens **once** (`candidates.normalize`):

* every candidate becomes an immutable `StrategyCandidate` with a **content identity**: `candidate_id = sha256(strategy_id, strategy_version,
  symbol, direction, signal date, entry reference, invalidation reference, feature_snapshot_hash)`; a changed version or feature is a new candidate;
* every candidate must form a valid **Strategy Standard v1** signal (no execution field anywhere, `EXECUTION_FIELD_FORBIDDEN` otherwise);
* a missing / NaN / infinite critical feature is **never turned into zero**: the signal is skipped and counted (`candidate_skipped_missing_data`),
  as are duplicates, unsupported directions (Stage 6.2 simulates LONG candidates only; no shorting is added), a stop not below the entry and a
  signal session absent from the dataset; every skip carries its reason.

A strategy **without an honest historical replay** declares `historical_replay_supported = False` with the reason, and the harness reports
`NOT_RUN` - it never manufactures a backtest. **Downside Dislocation Recovery** is such a strategy: READY needs a point-in-time Stage 5.4
evidence packet that does not exist for past sessions (the Stage 5.6 PPE replay returned `EVIDENCE_NOT_AVAILABLE` for every observation), and
intraday Market Demand was validated on synthetic sessions only. Its Validation section reads **Not yet evaluated** with that reason; its
forward evidence is the Stage 6.1 forward paper validation. The four registry placeholders (Value + Quality, Momentum, Mean Reversion,
Breakout) have no rules and also read `NOT_RUN`.

## 3. Three-arm methodology

| Arm | What it does | Default |
|---|---|---|
| **RULES** | takes every candidate: the strategy as defined (the reference arm; the run's verdict is RULES') | always run |
| **GATED** | deterministic hand-written gates may only VETO a candidate (the control arm) | no gate defined: `NO_ADDITIONAL_GATES`, GATED equals RULES |
| **MODEL** | an optional decider may only answer `TAKE` or `STAND_ASIDE` for a candidate (a filter, never a source of edge) | no decider configured: `NOT_EVALUATED` (never zero) |

**Same candidate set guarantee**: RULES candidate ids == GATED ids before veto == MODEL ids before decision, in the same order, and every
decision belongs to exactly one input candidate (`arms.same_candidates`). If the sets differ the run is **VALIDATION INVALID**, nothing is
simulated and no comparative conclusion is drawn. A gate or decider receives a deep copy of the candidate and cannot create a trade or change
a symbol, direction, stop, target or timing; a decider answering anything else makes its arm `INVALID` (and the run INVALID). An accepted
candidate is simulated once and is identical in every arm that accepted it.

## 4. Out-of-sample (OOS)

* The split is **chronological** (`dataset.split`): the earlier `train_ratio` share of the dataset's sessions is TRAIN (development), the rest
  is OOS. Default 70 / 30, configurable within 0.5 - 0.9. **There is no random split.** A candidate belongs to the segment of its signal date.
* OOS is reported **on its own** (TRAIN, OOS and ALL metrics side by side), never folded into one number.
* An OOS slice shorter than `MIN_OOS_SESSIONS` (60) or with fewer than `MIN_OOS_TRADES` (20) trades is inadequate: `INSUFFICIENT DATA`.
* **No OOS contamination**: the harness never fits, selects or tunes a parameter. Changing a strategy's rules or parameters after looking at an
  OOS result makes it a **new `strategy_version`** (a new candidate identity), and its earlier OOS result no longer applies to it. A run records
  the strategy version, the full configuration snapshot, the dataset content hash, the split and the git commit, so a result can always be
  traced to the exact code and data that produced it. The store **enforces** it: a run of a `strategy_version` already validated with another
  adapter configuration (parameters, entry policy, holding limit, critical features) is refused.
* **Boundary PURGE** (Stage 6.3.1, `validation_v2`): a TRAIN candidate whose holding window (the next `max_hold_bars` sessions after its
  signal) reaches past the TRAIN end is purged (`TRAIN_OOS_BOUNDARY_PURGE`) whatever its outcome - it stays a TRAIN candidate and counts in
  no TRAIN or OOS metric; every other TRAIN candidate is simulated on the bars up to the TRAIN end only. OOS prices cannot change a TRAIN
  metric (DESIGN_631_VALIDATION_HARDENING.md).

## 5. Friction and execution assumptions

* **Causal fills**: a session-close signal is known at the close, so the default entry is the **next bar's open** (`NEXT_BAR_OPEN`). An entry at
  the signal close requires the strategy's explicit market-on-close declaration (`SIGNAL_CLOSE_MOC` + `moc_declared`); otherwise the causality
  check is CRITICAL and the run INVALID.
* **Exits**, per bar: a gap through the stop fills at the (worse) open (`STOP_GAP`); a gap through the target fills at the target (conservative,
  never the better open); stop and target inside one bar with unknown ordering → **STOP_FIRST** (an *ambiguous bar*, counted as
  `ambiguous_bars_count`; the target-first alternative is recorded for the dependence diagnostic only); the stop (`STOP`); the target (`TARGET`);
  the close of the last allowed bar (`TIME_EXIT`, `max_hold_bars`); the dataset end (`DATA_END`). An entry bar opening at or below the stop is
  skipped explicitly (`ENTRY_GAP_THROUGH_STOP`), and so is one opening at or above the target (`ENTRY_GAP_THROUGH_TARGET`: the planned move is
  gone; never booked as a target exit below the fill).
* **Friction** (the Stage 3.2 convention): buys fill at price × (1 + slippage_bps_per_side / 10 000), sells at price × (1 − …),
  `commission_per_order` per order plus `fee_per_share` per share and order. Default: 5 bps per side, no commission, no fee. **Gross P/L,
  slippage, fees and net P/L are always shown**; every conclusion uses net.
* **Risk-based sizing** (research only): quantity = floor(equity × risk_fraction / risk per share), capped by max_position_pct of a fixed
  hypothetical equity (default 100 000, 0.5 %, 10 %); a quantity below one share is simulated as one share (`MIN_ONE_UNIT`) and a capped one is
  `CAPPED` - configured and achieved risk are both reported, nothing is dropped. Overlapping trades are allowed, nothing compounds.

## 6. Metrics and the t-statistic

Per arm and per segment (TRAIN / OOS / ALL), from the simulated trades only: trade / candidate / accepted / stand-aside counts, win rate, gross
P/L, slippage, fees, net P/L, average trade return, average / summed R (net, and gross), expectancy (R and $), profit factor (none without a
losing trade), max drawdown ($ and %, net P/L in exit order on the fixed equity), per-trade Sharpe (not reported below 30 trades), average
holding bars, best / worst trade, exit-reason distribution, mean R, std R, **mean_R_t_stat**, sample size, ambiguous bars, size status,
configured vs achieved risk, simulation skips. R = net P/L / (quantity × (entry fill − stop)).

**t = mean(R) / (std(R) / √n)** with the sample standard deviation. It is **evidence, never an oracle**: |t| < 2 is weak evidence (`MIXED`),
and an OOS-only |t| < 2 is a warning.

## 7. Sample size

| Constant | Value | Effect |
|---|---|---|
| `MIN_TRADES` | 30 | fewer RULES trades (TRAIN + OOS) → INSUFFICIENT DATA |
| `MIN_OOS_TRADES` | 20 | fewer OOS trades → INSUFFICIENT DATA |
| `MIN_OOS_SESSIONS` | 60 | a shorter OOS window → INSUFFICIENT DATA |
| `MIN_SHARPE_TRADES` | 30 | below it the per-trade Sharpe is not reported |
| `T_STAT_WEAK` | 2.0 | below it: weak statistical evidence |

The constants are explicit (`strategy_validation/__init__.py`) and are never tuned to make a strategy pass; changing one is a new
`validation_version`.

## 8. Mechanical audit

`strategy_validation/audit.py` is an **independent** pass over the generated records after simulation: it imports nothing from the simulator or
the engine and re-reads the candidates, the trades and the raw bars as plain data (exactly what the store persists). For every trade it checks:
the candidate exists and its id re-hashes from its identity fields; no candidate is executed twice in one arm; symbol, direction, strategy id and
version match; **the arm accepted (TAKE) the candidate**, and **every accepted candidate has a trade or a recorded simulation skip** (nothing is
silently dropped - a missing losing trade is a violation); the entry is the documented fill (next-bar open, or the declared market-on-close) and
never before the signal; the risk-based size re-derives; dates are ordered; the stop equals the invalidation and lies below the entry; the exit reason is allowed and consistent with its bar; **the exit is the
first bar the documented rules exit on** (re-derived bar by bar, stop first inside an ambiguous bar); the holding period is within the limit; the
TRAIN / OOS label follows the signal date and the split; gross, friction, net and R arithmetic add up. Result: **N / N trades validated**, plus
every violation. Any violation makes the run **VALIDATION INVALID**.

## 9. Verdict policy (`strategy_validation/verdict.py`, per arm; the run's verdict is RULES')

1. **VALIDATION INVALID** - an integrity failure: the arms did not receive the same candidate set, a CRITICAL causality finding, a mechanical
   audit violation, or an arm broke its contract. No conclusion is drawn.
2. **INSUFFICIENT DATA** - the sample-size rules above. Shown with the real counts; no performance is invented.
3. **FAIL** - friction-killed (gross positive, net not positive), OOS expectancy not positive after friction, or total net not positive. Shown
   plainly: *"This strategy should not progress to paper trading."*
4. **MIXED** - OOS positive, but: TRAIN not positive, OOS degraded below half of TRAIN, |t| < 2, severe period concentration (one year or quarter
   ≥ 80 % of net P/L), ambiguous-bar dependence (target-first would move net P/L by > 25 %), friction above half of the gross profit, or a
   SENSITIVE / UNSTABLE parameter neighbourhood.
5. **PASS** - none of the above: *Eligible for review*. Never a promotion, never a forecast.

Causality is checked automatically (static scan of the adapter source for negative shifts / centred windows / future indexing; runtime checks
that every dated feature is at or before its signal close, that features carry an as-of date, that signals are close-based, that an entry at
the signal close is declared; and a **point-in-time truncation check**: the dataset is cut at the signal date of sampled candidates - no bar after
it exists - and the candidates are generated again; every candidate signalled on or before the cut must come back identical, which also catches a
*selection* on future bars that no dated feature reveals) **and** `MANUAL_CAUSALITY_REVIEW_REQUIRED` is always set: automated checks catch known
patterns only.

## 10. Parameter sensitivity and period concentration

* **Sensitivity** runs only *explicitly supplied* neighbouring parameter sets (`--neighbors params.json`) with the same split and friction and
  labels the neighbourhood **ROBUST** (every neighbour within 50 % of the base expectancy, same sign), **SENSITIVE** (further) or **UNSTABLE** (a
  sign flip or a neighbour without trades). It is a diagnostic only: the base configuration is never replaced, nothing is optimized.
* **Period concentration** reports net P/L by year, quarter and (from 60 trades) month, with the top period's share. No market-condition labels
  are produced: no deterministic data feeds such a label in this stage.

## 11. Storage, API, CLI and the Strategy Lab

* **Store** (`strategy_validation/store.py`, `database/strategy_validation_migrations.py`): three append-only tables on the app database
  (runs, candidate sets, simulated trades; UPDATE / DELETE abort). A run is written once, complete, in one transaction. **SYNTHETIC** (fixture)
  runs are refused outside a test process (`STOCK_AGENT_ENV` test / browser_test) and invisible to every read outside one: a normal app or
  preview never shows a fixture result. **Persistence integrity gate** (Stage 6.3.1): the store re-evaluates `PersistenceEligibility`
  (`strategy_validation/integrity.py`: provenance, strategy version, candidate set, causality incl. the declared manual review, replay parity,
  mechanical audit, accounting, boundary) and writes only an ELIGIBLE run. Integrity is separate from the performance verdict: integrity PASS
  with a FAIL or INSUFFICIENT DATA verdict is persisted; integrity FAIL never is.
* **API** (GET only; `api/routes/strategy_validation.py`, under the registry prefix): `/api/strategy-registry/{strategy_id}/validation`,
  `/api/strategy-registry/validation/runs/{run_id}`, `…/simulations[?arm=]`, `…/audit`. No endpoint launches a backtest.
* **CLI** (never a broker writer): `python -m strategy_validation validate --strategy ID --dataset FILE [--train-ratio 0.7] [--arms
  rules,gated,model] [--output run.json] [--persist [--db PATH]] [--neighbors params.json] [--provenance prov.json]` → exit 0 (a completed run,
  any verdict; with `--persist` stored), 2 (NOT_RUN), 1 (input error), 3 (`--persist` given but BLOCKED by the integrity gate: not stored);
  `python -m strategy_validation runs [--strategy ID]`. A dataset is a CSV (`symbol,date,open,high,low,close[,volume]`) or JSON
  file of completed daily sessions; its content hash is the `dataset_id`.
* **Registry**: every row carries `validation_status` (NOT_RUN or the latest verdict), `latest_validation_run_id`, `validation_verdict`,
  `train_trade_count`, `oos_trade_count`, `mechanical_audit_status`, `last_validated_at`. **No lifecycle mutation**: a verdict is displayed next to
  the lifecycle, never written into it.
* **Strategy Lab** (MORE › Research › Strategy Lab): a Validation column in the registry and a Validation block in the strategy detail - the
  verdict and decision line, the TRAIN / OOS / All metrics of the RULES arm after friction, the arm comparison (one candidate count for every
  arm; MODEL "Not evaluated"), friction, the audit line, the causality line, the train-vs-OOS and sensitivity diagnostics, the disclaimer. With no
  run: **Not yet evaluated** and why.

## 12. Limitations

* Daily bars only; intrabar ordering is unknown (hence STOP_FIRST and the dependence diagnostic). No partial fills, no borrow, no shorting.
* No market impact model beyond the per-side slippage; no liquidity filter is implied by the harness itself.
* Fixed hypothetical equity, overlapping positions allowed, no portfolio-level constraint (the Stage 5.9 proposal / 6.0 execution limits are not
  simulated) - a validation result is about the signal, not the portfolio.
* Survivorship and data quality are the dataset's responsibility; the harness validates OHLC consistency only. A missing session in a symbol's
  bars makes the next available bar the "next bar". The dataset kind (HISTORICAL / SYNTHETIC) of a JSON file is self-declared; the run records the
  content hash and the source file name.
* The boundary purge removes the TRAIN candidates nearest the TRAIN end from the TRAIN sample (reported, never hidden); there is no separate
  embargo before the OOS window.
* Parameter-sensitivity neighbours are evaluated on the whole dataset (TRAIN + OOS): a diagnostic label only, never a selection.
* The causality checks are pattern-based; a manual review is always required.
* No real historical dataset ships with the repository (data bundles are built under the git-ignored `runtime/`). Downside Dislocation Recovery
  reads `NOT_RUN` (no honest replay, Stage 6.3); Value + Quality (Stage 6.4, section 13) is replayed from point-in-time SEC fundamentals; Momentum,
  Mean Reversion and Breakout are not implemented.
* The replay foundation (`strategy_validation/replay.py`) defines the research-vs-paper candidate-state comparison; no paper-side source is
  implemented in Stage 6.2.

## 13. Cross-sectional, stop-less strategies (Stage 6.4)

Added for Value + Quality (`value_quality/`, `DESIGN_64_VALUE_QUALITY.md`) and available to any adapter; a per-trade strategy is unaffected
(every default is the Stage 6.2 behaviour).

* **Auxiliary point-in-time data**: a dataset may carry `aux` = `series` ({name: {symbol: {date: value}}}), `events` (rows with a `filed` date)
  and `static` (classification). `aux` is part of the content hash (`dataset_id`), and the TRAIN view (`Dataset.until(train_end)`) cuts it:
  series after the TRAIN end and events filed after it are removed, so a TRAIN outcome can read no later filing either.
* **`stop_policy = "NONE"`**: a candidate without an invalidation level (a periodic rebalance) - it must use **`EQUAL_WEIGHT`** sizing (an
  adapter-declared `sizing` = {mode, position_pct}: notional = position_pct x the fixed equity; risk sizing needs a stop, so the engine refuses
  the combination). The R of such a trade is its return on the position notional.
* **Per-candidate `max_hold_bars`** and **`time_exit_fill = "OPEN"`**: a time exit fills at the open of its last bar (the hand-over to the next
  monthly leg), instead of the default close.
* **Transaction flags** `entry_trade` / `exit_trade`: a leg that continues a held position on one side is not an order there and pays no
  slippage, commission or fee on that side; the audit re-derives the flags (TRANSACTION_FLAGS) and the fills.
* **Adapter hooks**: `audit_extra(record, dataset)` (strategy-specific mechanical checks merged into the one audit; any violation fails it),
  `strategy_diagnostics(record, dataset)` (research diagnostics, never a gate) and `research_flags(dataset)` (limitations such as
  `SURVIVORSHIP_BIAS_PRESENT`, stored with the run and displayed with the verdict - never suppressed).
* **Research registry check**: a historical replay checks the Strategy Standard v1 schema plus the registry rules that apply to research
  (registered, implemented, source, direction). The live-emission rule (only PAPER_CANDIDATE / PAPER_ACTIVE / APPROVED strategies emit LIVE
  signals) is not applied to a replay: a strategy is validated BEFORE any promotion. A live signal of a RESEARCH strategy is still refused
  (`STRATEGY_INACTIVE`).
* **`CANDIDATE_CONTRACT`** (integrity gate): a run in which any generated candidate failed the Strategy Standard v1 contract is BLOCKED - a
  replay whose candidates were all rejected can no longer be persisted as an "eligible" empty result.
