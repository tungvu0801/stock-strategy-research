# Stage 6.10C — Agent Decision Flow, Decision History and an Inactive Dynamic Universe

Base: main `fed5b3cd3d602eccd71e473046ce58ad2f7819f4` (Stage 6.10A merged, PR #34). Alpaca PAPER only. Robinhood stays READ ONLY / USER_MANAGED / REAL.

**Purpose.** Stage 6.10C makes the agent's per-cycle decisions explainable, and prepares a larger stock universe without activating it. It is
observability only. It changes no trading, risk, strategy, Paper Auto, startup or execution behaviour.

```
existing persisted rows (setup_evaluations, execution_runs, execution_mode_events, execution_safety_evaluations,
autorun_preflights, forward_validation_samples, forward_capture_decisions, execution_intents/orders)
    → decision_flow (read-only projection; mode=ro; never writes, never calls a broker)
    → GET /api/decision-flow[/history|/cycle|/explain|/universe]
    → TRADE › Decision Flow (latest cycle + Closest to READY)  ·  ACTIVITY › Decision history (cycles, day counts, "why not at T")

Core universe (79, inactive) → cheap scanner (interfaces only, 0 signals implemented) → active universe (== live watchlist, unchanged)
    → existing setup evaluator → READY / WATCHING / BLOCKED → existing risk engine → existing execution engine
```

## 1. Declaration: what changes execution (nothing) and what only reads it

| Area | Change | Evidence it is unchanged |
|---|---|---|
| execution/* | none (byte-identical to the base) | 6.10C F02 |
| setups/*, proposals/*, safety/*, trader_view/*, forward_capture/*, autorun/* | none | 6.10C F01 directory bans |
| Strategy thresholds / frozen controls | none | 6.10C F03 (clean process) |
| Startup / Paper Auto | MONITOR_ONLY at every start, MANUAL_ARM, scheduled arm off | 6.10C F03 clean-process policy line |
| Live ticker coverage | unchanged: `watchlist.txt` (10 symbols) | 6.10C U-tests; no live module imports `dynamic_universe` |

`SEMANTIC_610C = {}` and `INSTRUMENTATION_610C = {}`: Stage 6.10C has no execution semantic change and no executor instrumentation.

| Hash | Value (unchanged) |
|---|---|
| Frozen strategy controls (FROZEN_V1) | `b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530` |
| Entry contract, gate off | `2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa` |
| Entry contract, gate on | `d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4` |

One display bug of the Stage 6.1.1 shell is fixed on the way, because the Decision history keeps a user on ACTIVITY longer: `renderActivity()` looked
up the legacy alerts after rebuilding the Activity panel, so a second render detached them and `app.js` then failed every 30 s. The node is now held
before the rebuild.

## 2. Settings (all appended to config.py; inert by default)

| Setting | Default | Meaning |
|---|---|---|
| `DYNAMIC_UNIVERSE_MODE` | `OFF` | `OFF` or `SHADOW`. SHADOW only computes the read-only universe plan on demand (no thread, no request, no write). Any other value, `ACTIVE` included, is treated as OFF and reported as a problem: activation is a later stage. |
| `DYNAMIC_UNIVERSE_CORE_FILE` | empty | Optional JSON file `[{"symbol": "...", "sector": "..."}]` replacing the built-in core list. Invalid entries are dropped and reported; an unreadable file falls back to the built-in core. |
| `DYNAMIC_UNIVERSE_ACTIVE_MAX` | `25` | Upper bound the future active set would respect. Used by no live path. |

None of these are cohort keys or forward-capture FLAGS, so neither the cohort id nor the config hash changes.

## 3. Definitions

**Evaluation cycle.** One batch of the Stage 5.8 setup scheduler: the `setup_evaluations` rows with trigger `SCHEDULED` that share one
`evaluated_at` and contain at least 2 distinct symbols. The latest cycle's timestamp is confirmed by `setup_controls['setup_state_evaluator'].last_run_at`.
Single-symbol `SCHEDULED` rows come from the Trader View stream (the active ticker of a connected browser) and `SUBSCRIBE` rows from opening a
ticker: both are *off-cycle evaluations*, shown in a ticker's history but never counted as cycles. When the same symbol appears twice in one
stamp (a stream row landing in the batch second), the lower rowid is kept, and explain still names that symbol's row the cycle's ticker (the
values were identical in every such case on 2026-10-27). Known limit: two Trader View connections on different tickers writing in the same
second form a 2-symbol stamp, which reads as a cycle; not seen on 2026-10-27 (244 one-symbol stamps, 458 ten-symbol stamps, none with 2-9). A batch is written one symbol at a time, so the executor can read a
partially written batch (up to ~14 s on 2026-10-27); the per-ticker executor result is therefore attached by time window, not by id.

`cycle_id = "SE-" + evaluated_at` in compact UTC form (`SE-20261027T195903Z`). It is derived, not persisted: the engine stores no cycle id.

**Executor cycles of a decision cycle.** The `execution_runs` rows with `started_at` in `[cycle_at, next_cycle_at)`. Monitor-only IDLE cycles
are never persisted; their only trace is `forward_validation_samples.last_cycle_at/last_cycle_result`.

**Final decision vocabulary** (one map, used by the API and the UI):

| Decision | When |
|---|---|
| `READY` | setup READY and Paper Auto ON: awaiting the executor, or an order intent was created (sub-status `ORDER_INTENT`) |
| `SKIPPED` | setup READY but not sent: Paper Auto OFF (`PAPER_AUTO_OFF`), or the per-cycle entry limit (`ENTRIES_PER_CYCLE`) |
| `BLOCKED` | setup READY and an entry control blocked it (control ids), or a duplicate entry |
| `WATCHING` | setup state WATCH: a downside dislocation was detected, confirmation incomplete |
| `WAIT` | no active setup (NONE), INVALIDATED awaiting reset, or the evaluation was PAUSED / ERROR |

For a READY setup the executor's own result in the cycle window comes first: it is what happened. That includes a PAUSED or ERROR evaluation
that kept its stored READY state, because the executor still reads that open READY episode; without an executor result such a row is `WAIT`.

The legacy Agent card shows "WATCHING" for every non-READY setup; the Decision Flow legend states that its WATCHING means setup state WATCH only.

## 4. READY checks, score and distance (decision_flow/conditions.py)

The setup engine computes **no composite score**: READY is the conjunction of six checks. The Decision Flow recomputes each check from the
stored `inputs_json` with the engine's own read-only predicates (`setups.engine.ppe_usable`, `demand_available`, `evidence_gate`) and the
constants in `setups/__init__.py`, then cross-checks the failing list against the stored `freshness_json.blockers` (`consistent`). A mismatch is
reported, and the stored blockers stay authoritative.

| Check id | Label | Passes when | Blocker code |
|---|---|---|---|
| `STRETCH` | Downside stretch (PPE) | stretch ≤ −40 | `NO_DOWNSIDE_STRETCH` |
| `PPE_COVERAGE` | PPE coverage | coverage ≥ 0.60 | `PPE_COVERAGE_LOW` |
| `CONFIDENCE` | PPE confidence | status AVAILABLE and confidence ≥ 40 | `CONFIDENCE_BELOW_40` |
| `DEMAND_DATA` | Market Demand data | available, coverage ≥ 0.60, fresh | `DEMAND_UNAVAILABLE` |
| `DEMAND` | Market Demand | demand ≥ 60 (evaluated only when the data check passes) | `DEMAND_BELOW_60` |
| `EVIDENCE` | Stage 5.4 evidence | a COMPLETED packet ≤ 30 days old with no company-specific deterioration | `READY_BLOCKED_*` |

Rules that keep the display honest:
* Confidence that is not AVAILABLE is shown as **Not available** with its cause (`EVIDENCE_NOT_AVAILABLE` → "no stored Stage 5.4 evidence
  packet"; `NOT_APPLICABLE` → "|stretch| ≤ 40"; `STRETCH_NOT_AVAILABLE`), never as "below 40". On 2026-10-27 this was every row.
* When confidence is unavailable for lack of evidence *and* the evidence check fails for the same reason, both checks carry
  `root_cause: NO_EVIDENCE_PACKET`; `distance.unmet_root_causes` counts that cause once.
* PAUSED / ERROR evaluations show every check as `null` (not evaluated) with the pause or error text.
* **READY held:** once READY, the engine keeps the state until its exit thresholds (stretch above −20, Market Demand below 45, below 35 for two
  evaluations, demand data unavailable, an evidence block); the six checks are the WATCH → READY entry checks. A READY row with failed checks
  therefore reads "READY held: N of 6 entry checks (WATCH to READY) not met now", followed by those exit thresholds, never "READY checks unmet".
* A setup reason the Decision Flow rewords (unavailable confidence, an error) carries `reason_reworded: true`, and the dashboard then omits the
  engine's code (`CONFIDENCE_BELOW_40` would say "below 40").
* **Score:** `Not available — the setup engine computes no composite score`. The PPE confidence is never relabelled as a setup score.
* **Distance from READY:** the number of unmet checks (and of distinct root causes) plus the numeric gaps where the values exist:
  `stretch_gap = stretch − (−40)` (points above the threshold), `demand_gap = 60 − demand`, `confidence_gap = 40 − confidence`. No scalar
  composite distance is computed.
* **Price:** history rows show `Not available (the evaluator does not record prices)`. The latest cycle may show the last *cached* quote from
  the shared market-data cache (≤ 60 s old, status OK), read without any request; otherwise `Not available`.

**Closest to READY** (a display ordering of persisted values, not a score): setup state (READY, WATCH, INVALIDATED, NONE), then fewest unmet
checks, then smallest stretch gap, then smallest demand gap, then symbol.

## 5. Risk, Paper Auto and market state at a past time

| Item | Source (read-only) |
|---|---|
| Market state | `autorun.session.classify(at)` (holiday-aware static NYSE calendar) |
| Paper Auto | `execution_mode_events`: the `to_mode` of the last event ≤ t (default MONITOR_ONLY); `environment_json` is never read |
| Risk gate | the last `execution_safety_evaluations` row ≤ t: state, allowed_new_entry, reason codes |
| Preflight | the last `autorun_preflights` row ≤ t: status and failing check ids |
| Reconciliation | the last `forward_validation_samples` row ≤ t: reconciliation_healthy, attention |
| Entry controls of a ticker | `forward_capture_decisions` EXECUTOR_CYCLE rows of the cycle's runs (`controls_json`: every control's PASS/BLOCK); else `execution_runs.summary_json.entries.blocked/evaluations` (blocking controls only); else **Not evaluated** (entry controls run only for a READY setup during Paper Auto) |

## 6. API (GET only; api/routes/decision_flow.py)

| Route | Returns |
|---|---|
| `GET /api/decision-flow` | the latest cycle: summary, per-ticker rows, Closest to READY, universe status |
| `GET /api/decision-flow/history?date=YYYY-MM-DD&limit=N` | one New York trading date: day counts and the cycle list (newest first) |
| `GET /api/decision-flow/cycle?at=ISO` | one cycle reconstructed: the batch at or before `at` |
| `GET /api/decision-flow/explain?symbol=MU&date=YYYY-MM-DD&time=HH:MM&tz=ET` | why a ticker did (not) trade at a time (tz ET, PT or UTC) |
| `GET /api/decision-flow/universe` | the inactive dynamic-universe plan |

Dates are bounded to the years 2000-2100 (year 1 or 9999 would overflow the time arithmetic): outside them, and for any malformed input,
the answer is 400, never 500. Times print as "ET" with UTC alongside; in the repeated 01:00-02:00 hour on the November DST change an explain
time in ET or PT resolves to its first occurrence.

Every read opens SQLite with `mode=ro`; a missing table reads as empty; nothing is migrated, created or written (the database path is the open
singleton's or the configured one, never `get_db()`, which would create and migrate a file); `safety.current()` (which can
write) and live quote requests are never used. Payloads carry `broker_calls: 0`.

### 6.1 Per-ticker row
```
{symbol, evaluated_at, evaluated_at_et, trigger, origin: SCHEDULER_CYCLE|TRADER_VIEW|SUBSCRIBE,
 price: {value|null, as_of|null, text},
 setup: {type, name, note: "only setup type; no cross-setup ranking", state, prior_state, state_label, evaluation, reason_code, reason_text, reason_reworded, setup_id},
 score: {value: null, text},
 values: {stretch, ppe_coverage, confidence, confidence_status, demand, demand_status, demand_coverage},
 checks: [{id, label, value, value_text, threshold_text, passed: true|false|null, gap|null, detail, root_cause|null}],
 passed: [labels], failed: [labels],
 distance: {unmet, unmet_root_causes, checks: 6, stretch_gap|null, demand_gap|null, confidence_gap|null, text},
 risk: {evaluated: bool, text, controls: [{id, label, result, reason}], blocked_by: [ids]},
 decision: READY|SKIPPED|BLOCKED|WATCHING|WAIT, decision_detail, decision_label,
 no_trade_reason, consistent, consistency_note|null}
```

### 6.2 Cycle summary
```
{cycle_id, at, at_et, next_at|null, stale: bool,
 market: {session, label, calendar},
 tickers_scanned, candidate_setups,
 counts: {READY, SKIPPED, BLOCKED, WATCHING, WAIT},
 risk: {state, allowed_new_entry, reason_codes, evaluated_at, preflight: {status, at, failing}, reconciliation: {healthy, attention}},
 paper_auto: {mode, on: bool, label, policy, policy_label, since, since_et},          (policy: today's setting; the policy at a past time is not recorded)
 executor: {runs, results: {RUN: n, ERROR: n}, first_at, last_at, error_types, idle_note},
 order_intents, orders_submitted,
 outcome: {code: NO_TRADE|ORDER_INTENT|NO_CYCLE, text},
 closest: {symbol, decision, unmet, text}|null}
```
With no batch at or before the requested time, `/cycle` returns `at: null`, outcome `NO_CYCLE` and the context (risk gate, Paper Auto, market) at
the requested time, never the current one.

### 6.3 History day block
```
{date, timezone: "America/New_York", latest_date,
 cycles, evaluations, off_cycle_evaluations, tickers: [...],
 ready_transitions, ready_evaluations, watch_transitions,
 skipped_paper_auto_off: {count, basis}, skipped_entry_limit,
 blocked: {count, by_control: {id: n}, labels: {id: label}, basis},          (one per ticker per cycle, the cycle table's decision; skipped_entry_limit too)
 closest: {symbol, unmet, stretch, at, at_et, text}|null,
 paper_auto: {spans: [{from, to, open}], minutes},          (open: still armed at the read time; `to` is then the read time)
 executor: {runs, with_candidates},
 order_intents, orders, fills,
 risk_states: {state: rows}}
```

## 7. Dashboard

* **TRADE › Decision Flow** (`section#pui-decision`): directly after Bot Activity in the default layout and hidden in View details, so the
  pinned View-details layout is untouched. A fold, closed by default, whose summary line reads the cycle outcome. Inside: the ten cycle items,
  the per-ticker table (expand a row for its checks, distance and risk checks) and the Closest to READY table. Polled with a 15 s self-throttle
  while TRADE is open.
* **ACTIVITY › Decision history** (`section#pui-dhist`, a sibling of the Activity panel so its 30 s rebuild never wipes it): the day counts,
  the recent cycles (click one to reconstruct it), and an "Explain a ticker at a time" form (ticker, date, HH:MM, ET / PT / UTC).
* Times show New York time with UTC alongside. Missing values read **Not available**; nothing is estimated.

## 8. Dynamic universe (inactive)

`dynamic_universe/` prepares the two-layer universe. Nothing in the live path imports it.

* **Core universe v1** (`dynamic_universe/core.py`): 79 liquid US stocks, a frozen list built from the repo's curated `data/sector_map.py`
  peers (GOOG dropped as a duplicate share class of GOOGL) plus the three live symbols it lacks (CLS, SHOP, SNDK). Sector tags come from
  that map; symbols it does not map read `Unmapped`. The list is pinned by sha256, so editing `SECTOR_MAP` cannot change it silently.
* **Cheap scanner** (`dynamic_universe/signals.py`): six declared signal specs (relative volume, gap %, intraday move, sector strength,
  earnings/catalyst flag, unusual volatility) with inputs and expected request cost, all `implemented: False`. No signal is computed and no
  catalyst or news input exists.
* **Active universe** (`dynamic_universe/plan.py`): `select_active()` is the pure, tested future selection rule (live watchlist ∪ open
  episodes ∪ held symbols ∪ scanner candidates, bounded by `DYNAMIC_UNIVERSE_ACTIVE_MAX`). It is called by no live path. `plan()` reports the
  active set as exactly the live watchlist and asserts it.
* Any later activation must keep open setup episodes and open paper-position symbols in scope: two exits (DEMAND_FAILURE,
  DISLOCATION_NORMALIZED) need fresh setup evaluations of held symbols.

## 9. Scalability (measured; tools/evaluator_capacity.py)

`python -m tools.evaluator_capacity --symbols 10,50,100,200 --latency-ms 0,130 --cycles 3` runs the REAL evaluator path (SetupService.evaluate_all ->
PPE / Demand providers -> RealMarketData caches -> engine -> SetupStore -> the proposal listener) against a fake market-data client on a temporary
database, with sockets blocked and api.server never imported. 130 ms per request reproduces the live 10-symbol cycle (live 2026-10-27: median 4 s,
p95 8 s, max 19 s over 390 cycles; executor cycles median 594 ms).

| Symbols | Warm cycle, 0 ms network | Warm cycle, 130 ms / request | Cold cycle (130 ms) | Requests / cycle | Requests/min (evaluator) |
|---|---|---|---|---|---|
| 10 | 0.33 s | 4.1 s | 7.3 s | 29 | 29 |
| 50 | 1.8 s | 19.9 s | 26.2 s | 138 | 138 |
| 100 | 3.2 s | 39.3 s | 57.1 s | 271 | 271 |
| 200 | 6.5 s | 95.0 s (over the 60 s interval) | 133 s | 638 | 399 (cycles run back to back) |

Per symbol: about 33 ms of CPU and SQLite, about 0.4 s at the calibrated latency, and about 3 market-data requests per cycle (1 own snapshot,
about 0.4 benchmark snapshot, about 0.5 minute bars, 1 uncached 60-day daily-bar read by the proposal listener). The network, not the CPU, is the
bottleneck; the proposal listener's uncached daily-bar read is the largest single cost. A Trader View tab adds about 26-30 requests/min.

Recommendation: an initial active universe of about 25 symbols (the `DYNAMIC_UNIVERSE_ACTIVE_MAX` default): about 70 requests/min and a ~10 s cycle,
well inside the 60 s interval and below a 200 requests/min data-plan limit (Alpaca Basic; verify the account's plan). 50 is the practical ceiling
without changes; 100 needs the proposal read cached first, 200 needs that plus batching or parallel evaluation.

## 10. Tests

`tests/test_decision_flow_610c.py` (decision flow, history, explain, read-only proof, GET-only routes, dynamic universe, safety invariants,
freeze F01–F06) and `tests/frontend/decision_flow.test.cjs` (rendering, Not available, READY/WATCHING/BLOCKED/SKIPPED texts, ranking,
escaping, wording bans). The browser harness `product_ui` flow checks both panels at 1920×1080, 1440×900, 1366×768 and 1280×720.

## 11. Freeze bookkeeping

| Item | Value |
|---|---|
| `BASE_610C` | `fed5b3cd3d602eccd71e473046ce58ad2f7819f4` |
| New files | `NEW_610C` |
| Edited files | `EDITED_610C` |
| Semantic / instrumentation | `SEMANTIC_610C = {}`, `INSTRUMENTATION_610C = {}` |
