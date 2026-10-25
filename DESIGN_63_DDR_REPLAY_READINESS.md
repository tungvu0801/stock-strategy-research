# DESIGN 6.3 — Downside Dislocation Recovery: historical replay readiness

**Conclusion: FAITHFUL REPLAY NOT ESTABLISHED.** The validation harness (Stage 6.2) cannot evaluate Downside Dislocation Recovery on
genuine history today: the inputs that decide READY - the entry trigger - have no point-in-time historical source. The strategy stays
**NOT_RUN** ("Not yet evaluated"). No replay adapter, no backtest and no validation run is produced; nothing is simplified, substituted
or invented. This document records the parity matrix, the provenance gaps, the missing inputs, the forward-data collection plan and the
requirements a future replay must meet.

Assessed on 2026-10-25 against `main` dca719af (Stage 6.2 merged). No `AGENTS.md` exists in the repository; the specification read was
`setups/` (state machine, DESIGN_58), `ppe/` (DESIGN_56), `demand/` (DESIGN_57), `evidence/` (DESIGN_54), `proposals/` (DESIGN_59),
`execution/` (DESIGN_60), the frozen Stage 6.1 controls (DESIGN_61) and the Stage 6.2 harness (STRATEGY_VALIDATION_HARNESS.md).

## 1. The live strategy, as it decides

| Step | Live rule (unchanged, frozen Stage 6.1 controls) | Code |
|---|---|---|
| WATCH | PPE Stretch ≤ −40 (PPE usable and fresh) | `setups/engine.py` `evaluate` |
| READY | still Stretch ≤ −40, PPE coverage ≥ 0.60, PPE Confidence ≥ 40, Market Demand ≥ 60 (status AVAILABLE, coverage ≥ 0.60, fresh), evidence gate passes (a stored Stage 5.4 packet, ≤ 30 days old, not COMPANY_SPECIFIC_DOMINANT with MODERATE / STRONG strength, no guidance-down with a company / mixed cause) | `evaluate`, `evidence_gate`, `demand_available` |
| READY → WATCH / INVALIDATED | Demand < 45 → WATCH; Demand < 35 on 2 consecutive evaluations → INVALIDATED; Stretch > −20 → INVALIDATED; a new STRONG company-specific packet → INVALIDATED; WATCH → NONE when Stretch > −30 | `evaluate`, `evidence_contradiction` |
| Cadence | one evaluation per watchlist symbol every 60 s in the regular session; READY can happen at any minute | `setups/scheduler.py` |
| Proposal | market order at the live reference price; invalidation = 5-session structural low (incl. the session low) − buffer × ATR(14), capped at an ATR stop; risk 0.5 % of equity, ≤ 10 % position; TTL 300 s | `proposals/engine.py` |
| Gates | safety gate (kill switch, no-new-entries, 3 % daily loss), ≤ 10 open positions, 1 entry per cycle, no entry in the last 15 min | `safety/`, `execution/` |
| Exits | precedence HARD_INVALIDATION (price ≤ invalidation) > EVIDENCE_INVALIDATION > SETUP_INVALIDATED > DEMAND_FAILURE (Demand < 35 twice) > TIME_EXIT (5 sessions) > DISLOCATION_NORMALIZED (Stretch > −20) | `execution/engine.py` `exit_decision` |

## 2. Data that genuinely exists (read-only inventory, 2026-10-25)

| Store | Content |
|---|---|
| Stage 3.2 daily bars (`historical_bar_datasets`, `historical_daily_bars`, main database) | 82 symbols, 2024-06-03 → 2026-09-28 (SNDK from 2025-02-13, its listing), `1Day`, source `alpaca-market-data`, feed **IEX**, adjustment **all** (splits + dividends, applied as of the fetch 2026-09-29 04:52–04:59 UTC), one SHA-256 content hash per symbol dataset. Symbols include SPY but **no sector ETF**. No one-day close ratio outside 0.6–1.4 (no unadjusted split artefact). |
| Intraday bars | **none** (no minute / intraday table; DESIGN_57 §8: "the project has no intraday history store") |
| Stage 5.4 evidence packets | 3 COMPLETED packets, all **SNDK**, created 2026-10-16 00:25–04:09 UTC: **0 created inside the bar history** |
| Recorded live setup evaluations (`setup_evaluations.inputs_json`) | none in the main database (the forward paper validation has not started: 0 sessions) |

The inventory was read with SQLite `mode=ro`; the database content was not modified (the git-ignored WAL index `-shm` is touched by any
reader).

## 3. Strategy parity matrix

`EXACT` = the historical value is what the live strategy would have computed at that moment. `APPROXIMATE` never counts as parity.
`UNAVAILABLE` = no genuine point-in-time source.

| Live condition | Historical replay source | Replayable? | Parity | Notes |
|---|---|---|---|---|
| WATCH: Stretch ≤ −40 | Stage 3.2 daily bars, `ppe/stretch.py` replay mode | at the daily close only | **APPROXIMATE** | Live evaluates every minute at the live price; the stored benchmark set has no sector ETF (live uses the sector ETF where mapped, so the sector component differs); dividend-adjusted levels; IEX-only volume. |
| PPE coverage ≥ 0.60 | same | at the close | **APPROXIMATE** | Depends on which components are available, incl. the benchmark. |
| PPE Confidence ≥ 40 | a Stage 5.4 packet created at or before T | no | **UNAVAILABLE** | 0 packets inside the bar history; packets exist only after an explicit research request. |
| Evidence gate (packet exists, ≤ 30 days, cause / strength, guidance-down) | same | no | **UNAVAILABLE** | READY is impossible without a packet: the gate blocks (`READY_BLOCKED_NO_EVIDENCE`). |
| Evidence contradiction (new STRONG company-specific packet) | packet timeline | no | **UNAVAILABLE** | Needs the sequence of packet creation times. |
| Market Demand ≥ 60 (INTRADAY_MODE, coverage ≥ 0.60, fresh) | an intraday minute-bar store | no | **UNAVAILABLE** | No intraday history. DAILY_MODE is the off-session fallback, not the live input during the session. |
| READY → WATCH (Demand < 45), INVALIDATED (Demand < 35 × 2) | intraday store | no | **UNAVAILABLE** | Needs the evaluation-by-evaluation demand path. |
| INVALIDATED / WATCH → NONE on Stretch (> −20 / > −30) | daily bars | at the close only | **APPROXIMATE** | Live crosses intraday. |
| 60-second evaluation path | recorded evaluations / intraday data | no | **UNAVAILABLE** | One point per session from daily bars. |
| Proposal reference price, session low | intraday prices | no | **UNAVAILABLE** | The entry is an intraday market order, not the next day's open. |
| Invalidation level (ATR(14), 5-session structural low) | daily bars | partly | **APPROXIMATE** | ATR and the prior-session lows are exact on unadjusted bars; the session low at proposal time is not; adjusted levels differ from the live raw prices before a split / dividend. |
| Sizing (0.5 % risk, ≤ 10 %) | hypothetical equity | yes (as an assumption) | **APPROXIMATE** | The live account equity and buying power are not historical inputs. |
| Gates: open positions, entries per cycle, near-close cutoff, daily loss, kill switch | a portfolio-level intraday simulation | no | **UNAVAILABLE** | Path-dependent; the harness simulates one candidate at a time on daily bars. |
| Exit HARD_INVALIDATION | intraday price vs invalidation | at the daily low only | **APPROXIMATE** | Live checks each cycle; fill price and order not reproducible from daily bars. |
| Exits EVIDENCE_INVALIDATION, SETUP_INVALIDATED, DEMAND_FAILURE | packets, episode, intraday demand | no | **UNAVAILABLE** | |
| Exit TIME_EXIT (5 sessions) | session calendar | yes | **EXACT** | |
| Exit DISLOCATION_NORMALIZED (Stretch > −20) | daily bars | at the close only | **APPROXIMATE** | |
| Universe | the 82 stored symbols | — | **APPROXIMATE** | Today's list; membership over time not recorded (§8). |

No required entry condition is EXACT: WATCH and PPE coverage are APPROXIMATE, and three READY conditions (Confidence, the evidence gate,
Demand) are UNAVAILABLE. No documented reason makes them irrelevant to entry eligibility - they ARE the entry eligibility. Default
conclusion: **FAITHFUL REPLAY NOT ESTABLISHED**.

## 4. Missing inputs (concrete)

1. **Point-in-time Stage 5.4 evidence packets** for every symbol-session a READY could have occurred, with their creation time. None
   exist inside the bar history, and they cannot be rebuilt honestly:
   * the live research cadence is an operator decision (packets are built only on an explicit request), so when a packet would have
     existed is not recorded;
   * `company_ir` reads today's investor-relations page (no archive; a page dated after the as-of date is kept with the retrieval date);
   * `alpaca_news` requests the latest N headlines and drops those after the as-of date, so a past date receives (almost) none;
   * `sec_edgar` reads EDGAR's `filings.recent` list only (older filings live in paged files the adapter does not read);
   * manual URLs / texts and the related-ticker / IR-URL configuration are operator inputs of their own time.
2. **Intraday 1-minute bars** of each symbol and its benchmark for the session (≥ 30 completed bars) and the prior sessions (the
   expected-volume curve), for Market Demand at every 60-second evaluation, and the live price / session low for the proposal and the
   exits.
3. **The 60-second evaluation path** (or the inputs to rebuild it) for READY timing and the 2-evaluation demand collapse.
4. **Sector ETF daily bars** (the live PPE benchmark) - absent from the store.
5. **Raw (unadjusted) daily bars + a corporate-action table**, so thresholds and stops compare against the prices the strategy saw.
6. **Point-in-time universe membership** (the watchlist per day).
7. **Portfolio state** (open positions, entries per cycle, daily P/L) - requires a portfolio-level simulator, not available in the harness.

## 5. Point-in-time provenance (required for every historical field)

Every field a future replay reads must carry: **source**, **available_to_strategy_at** (when the live strategy could first read it),
**observed_at** (the market time the value describes), **published_at** where applicable, the **transformation** applied and whether a
**revision** could exist. Macro / event / fundamental data separates **event_date** (the period or event) from **data_available_at**
(the first release). A revised value is never used where the live strategy would only have seen the original release.

| Field | Source | available_to_strategy_at | observed_at | published_at | Transformation | Revision possible? | Gap today |
|---|---|---|---|---|---|---|---|
| Daily OHLCV | Alpaca market data, IEX | after the session close (the bar is complete) | session date | — | none (raw) / split + dividend adjustment | **yes**: adjustments are restated after every later split / dividend; IEX bars can be corrected | the store keeps adjusted bars only, adjusted as of the fetch (2026-09-29); no raw bars, no corporate-action table |
| Minute bars | Alpaca market data, IEX | minute close + 60 s (`completed()`) | the minute | — | none | late corrections possible | no store |
| PPE Stretch components | derived from daily bars + benchmark | evaluation time | evaluated session | — | `ppe/stretch.py` (robust z, ATR, RSI, EMA, range, volume, sector-relative) | inherits the bar revisions | benchmark differs (no sector ETF bars) |
| Market Demand components | derived from minute + daily bars | evaluation time T (bars closed by T) | session up to T | — | `demand/components.py` | inherits the bar revisions | no minute bars |
| Packet `created_at` | the research run | `created_at` | — | — | — | no (packets are immutable) | 0 packets inside the history |
| Evidence cause, explained share | internal adapter: stock vs sector / SPY returns over the decline window | packet `created_at` | decline window (peak → trough) | — | `evidence/sources/internal.py` | inherits the bar revisions | depends on adjusted bars |
| SEC filings (8-K items, 10-Q / 10-K) | EDGAR submissions | the acceptance time (filing date used today) | the report date | filing date | rule labelling (`evidence/rules.py`) | amendments are separate filings (no restatement in place) | `recent` list only |
| XBRL fundamentals (revenue, gross margin) | EDGAR companyfacts | `filed` (data_available_at) | the fiscal period end (event_date) | `filed` | quarter selection (80–100 days), latest filed ≤ as-of per period | **yes**: restated values arrive with later `filed` dates - the filter `filed ≤ as_of` keeps only versions already released | none for the filter; network fetch at build time |
| Headlines | Alpaca news (Benzinga) | `created_at` of the article | — | `created_at` | rule labelling | headlines / summaries can be updated (`updated_at`) | adapter has no historical window |
| IR releases | the company IR page | the page's own date (when extractable) | — | extracted date | HTML parse + rule labelling | pages are edited in place | no archive (today's page only) |
| Event calendar (earnings dates) | Stage 2.6 event store | the time the event was stored | event_date | — | — | scheduled dates move | stored dates are not point-in-time |
| Strength, rungs, contradictions, guidance | packet rules over the items above | packet `created_at` | — | — | `evidence/rules.py` (deterministic) | inherits item revisions | as above |

## 6. Dataset versioning (required identity)

| Required identity field | Stage 3.2 store today | Gap |
|---|---|---|
| source | `alpaca-market-data` + `feed` (iex) | — |
| retrieval / build date | `fetched_at` per symbol dataset | no single build id for the 82-symbol bundle |
| covered symbols | one dataset per symbol | no bundle manifest |
| date range | `requested_start/end`, `first/last_session` | — |
| bar frequency | `timeframe` (1Day) | — |
| adjustment policy | `adjustment` (all) | — |
| corporate-action handling | implicit in `adjustment=all` | no corporate-action table, no raw series |
| missing-data policy | not recorded | must be stated (e.g. a missing session is a gap, never forward-filled) |
| checksum | `content_hash` per symbol dataset | no bundle hash |

The Stage 6.2 harness identifies a run's dataset by the content hash of the bars it was given (`dataset_id`) and records the source file
name; a DDR replay must additionally carry the bundle manifest above in its configuration snapshot.

## 7. Corporate actions

* **Splits / reverse splits**: the window contains forward splits (e.g. NVDA 10:1 2024-06-10, AVGO 10:1 2024-07-15, LRCX 10:1
  2024-10-03); the adjusted store shows no artefact. Scale-free Stretch components are unaffected; price-unit quantities (ATR stops,
  structural lows, invalidation prices) must be computed on the series **as the strategy saw it on that day** - raw bars with only the
  splits up to that day applied - never on today's back-adjusted levels.
* **Dividends**: `adjustment=all` scales pre-dividend prices down; returns are close to total-return, levels are not what was traded.
  Use split-only adjustment as of each day for levels; dividends only where a component is defined on total return (none is).
* **Symbol changes**: the store is keyed by today's symbol; a replay needs a dated symbol map (none recorded; no change is known for the
  82 symbols inside the window, SNDK is a 2025 listing).
* A live threshold (Stretch −40 / −30 / −20, Demand 60 / 45 / 35, Confidence 40) must never be compared against a value computed from
  adjusted data without this being documented in the run.

## 8. Universe bias

The 82 stored symbols are today's list (large caps that exist today). Point-in-time constituents are not recorded, so any replay over them
carries **survivorship bias** and validates a **fixed, known symbol set** only - it must be reported as such and never as "the market".

## 9. TRAIN / OOS boundary policy (required before any run)

Chosen policy: **PURGE**. A TRAIN trade whose holding window can reach past the TRAIN end (signal date + `max_hold` sessions > TRAIN end)
is removed from TRAIN and is not counted in OOS; the purge is reported (count and candidate ids). Labelling such trades TRAIN while
their outcome uses OOS prices is not allowed. The Stage 6.2 harness only **counted** them (`diagnostics.boundary`); **implemented in Stage
6.3.1** (DESIGN_631_VALIDATION_HARDENING.md).

## 10. Persistence gate (required before any run)

A replay run may be **persisted** only when: provenance (§5, §6) is complete; every causality check passes (incl. the point-in-time
truncation check); replay parity is acceptable (every entry condition EXACT, §3); the mechanical audit passes. Anything else is an
ephemeral research / debug run and is never written. The Stage 6.2 store persisted any complete run with a verdict; **the gate is implemented
in Stage 6.3.1** (`strategy_validation/integrity.py`, DESIGN_631_VALIDATION_HARDENING.md). Downside Dislocation Recovery still declares replay
parity NOT_ESTABLISHED and stays NOT_RUN.

## 11. Candidate parity (how a future adapter must be built)

The adapter must not re-implement the rules. It builds `SetupInputs` from point-in-time stored data and calls the live
`setups.engine.evaluate` (and `evidence_gate`, `evidence_contradiction`, `demand_available`) and the live exit / invalidation functions,
so the live evaluator and the replay share one implementation. Parity tests: for fixtures constructed as equivalent live states, the live
evaluator and the replay adapter must produce the same state (WATCH / READY / INVALIDATED), direction (LONG), blocker / invalidation
reason code and evidence values (packet id, cause, strength, age).

## 12. Forward-data collection plan

Faithful history can only be **collected forward**. Proposed (nothing here is implemented in Stage 6.3; none of it changes a trading
control):

| # | Record | Covers | Status |
|---|---|---|---|
| F1 | an append-only intraday store of the completed 1-minute bars (symbol + benchmark) the live Demand engine reads, raw, with feed and fetch time | Demand, evaluation path, reference price, session low, HARD_INVALIDATION | to build |
| F2 | every setup evaluation's full `SetupInputs` (`setup_evaluations.inputs_json`, append-only) for every watchlist symbol in every session, plus the PPE / Demand component values | evaluation path, Stretch, Demand, packet state | exists (inputs_json); components to add |
| F3 | every research request and every packet (immutable, `created_at`) | evidence packets | packets exist; requests to log |
| F4 | intents, orders, fills and exit decisions (the paper execution store) - keep the Stage 6.1 forward paper validation running | proposal, gates, exits | exists |
| F5 | daily watchlist snapshot, raw daily bars, a corporate-action table, sector ETF daily bars, a dated symbol map | universe, Stretch benchmark, corporate actions | to build |

With F1–F5 a **forward replay** over the recorded period can feed the recorded inputs to the live functions (§11) and reproduce each
decision exactly; it becomes a validation dataset once the Stage 6.2 sample rules are met (≥ 30 trades, ≥ 20 OOS trades, ≥ 60 OOS
sessions). An automatic packet policy (a packet built at every WATCH close) would change the live strategy: it needs an explicit decision
and a new `strategy_version`, and is not proposed here as a default.

## 13. Rejected shortcuts

Not done, because each would replace the strategy with a different one: building packets now for past dates; treating a missing packet as
neutral; dropping the evidence gate; using DAILY_MODE Demand for the intraday threshold; using the 3 SNDK packets for earlier dates;
deciding READY at the daily close and entering at the next open; validating on today's adjusted levels against live thresholds. A
separately named, separately versioned research variant (for example a daily-close variant without the evidence gate) could be
backtested on the existing store, but only as a new strategy with its own registry entry and an explicit decision - it would say nothing
about Downside Dislocation Recovery.

## 14. Definition of ready (future replay)

1. F1–F5 recorded for the replay window (or genuine point-in-time equivalents with provenance per §5).
2. Every entry condition EXACT in the parity matrix (§3), or a documented reason it cannot affect entry eligibility.
3. Dataset identity per §6; corporate actions per §7; the universe stated per §8.
4. The PURGE boundary policy (§9) and the persistence gate (§10) implemented in the harness.
5. The adapter built on the shared live functions with parity tests (§11).
6. Then RULES and GATED first; MODEL stays NOT_EVALUATED unless a historical model evaluation is explicitly implemented; any parameter or
   sensitivity work on TRAIN only.

Until then: **Downside Dislocation Recovery is NOT_RUN** and its forward evidence is the Stage 6.1 forward paper validation.
