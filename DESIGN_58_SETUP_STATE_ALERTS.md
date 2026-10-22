# DESIGN 58 — Setup State Machine + Continuous Market-Hours Alerts

Stage 5.8. Base: origin/main `8194409` (Stage 5.7 merged). Package `setups/`, tables in `database/setup_state_migrations.py`, routes
`/api/setups/*` (read only + the existing alert read-acknowledgement pattern), Trader View **SETUP** section, Alerts-tab list.

Question answered: **"What is the current research setup state?"** — next to PPE Stretch (how far is price displaced?), PPE Confidence
(how credible is the dislocation classification?) and Market Demand (are buyers stepping in?). This is not a trading engine: no order,
no entry signal, no BUY/SELL, no probability. READY means **READY FOR REVIEW** — a deterministic confirmation threshold was met and the
setup deserves the user's attention — and is never shown or named as "ready to buy".

## 1. Audit of what exists (reused, not duplicated)

| need | existing | decision |
|---|---|---|
| persisted in-app alerts | Stage 4.1 `saved_scan_alert_events`: append-only rows, immutable content, only `read_at` changes, `GET` list / `POST …/read` / `POST …/read-all` | same pattern, own tables (`setup_alerts`), same read-acknowledgement routes under `/api/setups/alerts` |
| scheduler | Stage 3.7 `forward.automation.Scheduler`: one daemon thread per process, injected clock, `app_leases` row so two processes never run a check at once, start/stop from the app lifespan | same structure in `setups/scheduler.py`; reuses `AutomationStore.acquire/release` with lease name `setup_evaluator`; the after-close scheduler itself is unchanged (a 60-second market-hours cadence is a different job) |
| market hours | `trader_view.market_real._session_hint` (clock-based ET regular session; no holiday calendar; no calendar dependency in requirements) | `setups/market_hours.py`: weekdays, 09:30 ≤ ET < 16:00 via zoneinfo; **holidays are not modelled** (documented limitation; nothing is invented) |
| inputs | Trader View stream providers: `ppe_result`, `demand_result`, `research.latest` (stored Stage 5.4 packet), `market.quote` / `market_state` | the service reads these only; nothing is recomputed differently; position adapters are never consulted |
| persistence | per-stage `*_migrations.py` with `CREATE … IF NOT EXISTS`, no-update / no-delete triggers; stores open their own sqlite connections (`EvidenceStore`, `AutomationStore`) | `database/setup_state_migrations.py` + `setups/store.py` (migrate on first write; read-only reads tolerate a database without the tables) |
| live UI | Trader View socket (symbol-bound messages, stale guard, toasts), Alerts tab `#tab-alerts` | symbol-bound `setup` message + non-symbol-bound `setup_alert` broadcast; a self-polling `setup_alerts.js` list in the Alerts tab |
| observability | Stage 2.7G.1 ops status (read only) | a small `setup_controls` table (per control: last run / evaluation / transition, counts, last result, last error) + `GET /api/setups/controls` |
| test hooks | `TerminalStream.allow_manual_tick` (test-only `tick` command, refused in production) | the same flag gates a test-only `setup_eval` command; `build_stream()` enables it only when `STOCK_AGENT_ENV=browser_test` |

## 2. Setup type and states (V1)

One setup type: `DOWNSIDE_DISLOCATION_RECOVERY` ("Downside Dislocation Recovery"): price has become materially downside-stretched and
may be showing buyer confirmation. No upside / short setup in V1.

| state | meaning | UI label |
|---|---|---|
| NONE | no active downside-dislocation setup | NO ACTIVE SETUP |
| WATCH | a downside-stretched condition exists; confirmation incomplete | WATCH |
| READY | deterministic confirmation threshold met; deserves attention | **READY FOR REVIEW** |
| INVALIDATED | a previously active WATCH / READY setup failed an explicit invalidation rule | INVALIDATED |

An **episode** (`setup_id`) starts at NONE → WATCH and ends when the state returns to NONE. INVALIDATED keeps its `setup_id` until the
displacement is gone (then → NONE, episode closed); a new ≤ −40 reading afterwards starts a new episode (`cycle_id + 1`).

## 3. Inputs (`setups/models.py::SetupInputs`) — already-computed research only

From the Trader View stream at evaluation time: PPE (`stretch` score / status / coverage / `computed_at`, `confidence` score / status,
the evidence snapshot's `guidance_down` / `company_down`), Market Demand (score / status / coverage / mode / `computed_at`), the stored
Stage 5.4 packet summary (`created_at`, age in days, cause verdict, strength, company-confirmation rung, packet id), the quote timestamp,
market-data source, mode (simulated / real) and session hint. Nothing else: no position, no P/L, no future return, no model.

**Freshness** (service-computed, recorded with every evaluation): PPE fresh when `computed_at` is within `max(3·TRADER_PPE_REFRESH_SECONDS,
300) s` of the evaluation time; Demand fresh within `max(3·TRADER_DEMAND_REFRESH_SECONDS, 300) s`; quote fresh within 300 s (real mode).
Simulated mode is always fresh (deterministic clock). "Severe staleness" = PPE not fresh or PPE NOT_AVAILABLE.

## 4. Thresholds (fixed before any outcome analysis; `setups/__init__.py`)

| constant | value | role |
|---|---|---|
| WATCH_ENTER_STRETCH | −40 | NONE → WATCH when stretch ≤ −40 |
| WATCH_EXIT_STRETCH | −30 | WATCH → NONE when stretch > −30 (hysteresis); INVALIDATED → NONE when stretch > −30 (episode reset) |
| READY_CONFIDENCE | 40 | READY needs PPE Confidence ≥ 40 (status AVAILABLE) |
| READY_DEMAND | 60 | READY needs Market Demand ≥ 60 |
| READY_DEMAND_COVERAGE | 0.60 | Demand coverage ≥ 0.60 |
| READY_PPE_COVERAGE | 0.60 | PPE stretch coverage ≥ 0.60 (status AVAILABLE) |
| READY_EXIT_DEMAND | 45 | READY → WATCH when demand < 45 (hysteresis) |
| INVALIDATE_DEMAND / INVALIDATE_DEMAND_EVALS | 35 / 2 | READY → INVALIDATED after 2 consecutive evaluations with demand < 35 |
| INVALIDATE_STRETCH | −20 | READY → INVALIDATED when stretch > −20 (displacement gone after confirmation) |
| EVIDENCE_MAX_AGE_DAYS | 30 | a packet older than this cannot support READY |
| ALERT_COOLDOWN_MINUTES | 15 | identical episode / state / reason alert suppressed within 15 min (a new episode always alerts) |
| EVAL_INTERVAL_SECONDS | 60 | scheduler cadence during the regular session |

## 5. Evidence gate (blocks READY; WATCH may remain) and evidence contradiction (invalidates)

Gate codes, checked in this order, the first applies:
1. `READY_BLOCKED_NO_EVIDENCE` — no COMPLETED Stage 5.4 packet for the ticker (conservative: READY needs evidence).
2. `READY_BLOCKED_EVIDENCE_STALE` — the packet is older than 30 days.
3. `READY_BLOCKED_BY_EVIDENCE` — cause = COMPANY_SPECIFIC_DOMINANT with strength MODERATE or STRONG ("company-specific deterioration
   strongly confirmed"), **or** `guidance_down ≥ 1` with cause COMPANY_SPECIFIC_DOMINANT or MIXED ("guidance materially confirms
   deterioration").
The blocker is returned explicitly in the evaluation / transition reason and shown in the UI. Evidence never adds points.

**Contradiction** (hard invalidation from WATCH or READY): a packet **created after the episode started** with cause
COMPANY_SPECIFIC_DOMINANT and strength STRONG → INVALIDATED (`EVIDENCE_CONTRADICTION`). A packet that predates the episode only blocks READY.

## 6. Transition function (`setups/engine.py::evaluate(prev, inputs)`) — exact

Pre-check: PPE NOT_AVAILABLE or not fresh → **PAUSED** (`EVALUATION_PAUSED_PPE_UNAVAILABLE` / `EVALUATION_PAUSED_STALE_DATA`): state and
counters unchanged, nothing invalidated, the evaluation is still recorded.

Let `s` = stretch; `conf_ok` = Confidence AVAILABLE and ≥ 40; `demand_available` = Demand status AVAILABLE or SIMULATED_DATA, score present,
coverage ≥ 0.60, fresh; `demand_ok` = `demand_available` and demand ≥ 60; `gate` = §5 gate code or none; `contradiction` = §5;
`ready_ok` = `s ≤ −40` and PPE coverage ≥ 0.60 and `conf_ok` and `demand_ok` and no gate.

```
NONE:         s <= -40                        -> WATCH         DOWNSIDE_STRETCH_DETECTED (new episode)
              else                            -> hold NONE
WATCH:        contradiction                   -> INVALIDATED   EVIDENCE_CONTRADICTION
              s > -30                         -> NONE          STRETCH_FADED (episode closed, no alert)
              ready_ok                        -> READY         CONFIRMATION_STRENGTHENED
              else                            -> hold WATCH    first blocker: CONFIDENCE_BELOW_40 | DEMAND_UNAVAILABLE | DEMAND_BELOW_60 | PPE_COVERAGE_LOW | <gate>
READY:        contradiction                   -> INVALIDATED   EVIDENCE_CONTRADICTION
              s > -20                         -> INVALIDATED   STRETCH_NORMALIZED
              demand_available and d < 35:    streak += 1; streak >= 2 -> INVALIDATED DEMAND_COLLAPSED
                                              else hold READY DEMAND_COLLAPSE_PENDING (one more evaluation below 35 invalidates)
              not demand_available            -> WATCH         DEMAND_UNAVAILABLE  (cannot stay confirmed without demand)
              d < 45                          -> WATCH         CONFIRMATION_WEAKENED
              gate                            -> WATCH         <gate>  (a blocker that appeared after confirmation)
              else                            -> hold READY    CONFIRMATION_HOLDS (streak reset to 0)
INVALIDATED:  s > -30                         -> NONE          EPISODE_CLOSED (no alert)
              else                            -> hold INVALIDATED  INVALIDATED_AWAITING_RESET
```
No other transition exists. Boundaries: −40 enters (≤), −30 is still WATCH (exit needs >), 40 / 60 / 0.60 are inclusive, 45 stays
READY (exit needs <), 35 does not count (needs <), −20 is still READY (exit needs >). Same inputs → same decision (pure function).

## 7. Alerts and cooldown

Alert on NONE → WATCH, WATCH → READY, READY → WATCH, WATCH → INVALIDATED, READY → INVALIDATED. WATCH → NONE and INVALIDATED → NONE are
recorded as transitions without an alert. An unchanged state never alerts (HOLD rows carry no alert). Cooldown: an alert with the same
`setup_id / to_state / reason_code` as one created within the last 15 minutes is **suppressed** (the transition is still persisted, the
suppression is recorded on the transition row as `COOLDOWN:<alert_id>`); a different transition always notifies (WATCH at 10:00, READY at
10:04 → both alert). The key is scoped to the episode on purpose: the cooldown throttles re-entries of the same state inside one episode
(WATCH → READY → WATCH → READY within 15 min notifies READY once), while a **new episode** (new `setup_id`, e.g. WATCH → INVALIDATED → NONE
→ WATCH inside 15 min) is a new state transition and always notifies — found during manual validation, where a symbol-scoped key hid the
first alert of a new episode.

Content (no BUY / SELL words anywhere):
```
SNDK — WATCH                       NVDA — READY FOR REVIEW                AAPL — INVALIDATED
Downside dislocation detected.     Buyer confirmation strengthened.       Recovery setup invalidated.
PPE Stretch: -67 · Confidence: 34  PPE Stretch: -54 · Confidence: 58      Reason: demand collapsed after confirmation.
· Demand: 41                       · Demand: 72
```

## 8. Persistence (`database/setup_state_migrations.py`; additive, no delete anywhere)

* `setup_episodes` — one row per episode: `setup_id` (32 hex = sha256(symbol|type|started_at)[:32]), `symbol`, `setup_type`, `cycle_id`,
  `state`, `previous_state`, `started_at`, `last_transition_at`, `last_evaluated_at`, `closed_at`, `invalidation_reason`,
  `transition_reason`, `demand_low_streak`, `last_inputs_json`, `last_freshness_json`, `source`, `mode`, `updated_at`. Only the state
  fields change (no delete; at most one open episode per symbol + type — partial unique index).
* `setup_transitions` — append-only (immutable): `transition_id`, `setup_id`, `symbol`, `setup_type`, `from_state`, `to_state`,
  `reason_code`, `reason_text`, `evaluated_at`, `inputs_json`, `freshness_json`, `source`, `mode`, `trigger`, `alert_id`,
  `alert_suppressed_reason`, `created_at`.
* `setup_evaluations` — append-only (immutable), one row per evaluation incl. HOLD / PAUSED / ERROR: `evaluation_id`, `evaluated_at`,
  `symbol`, `setup_type`, `setup_id`, `prior_state`, `resulting_state`, `decision` (TRANSITION | HOLD | PAUSED | ERROR), `reason_code`,
  `reason_text`, `inputs_json`, `freshness_json`, `source`, `mode`, `trigger`, `owner`.
* `setup_alerts` — append-only except `read_at`: `alert_id`, `transition_id` (unique), `setup_id`, `symbol`, `setup_type`, `from_state`,
  `to_state`, `reason_code`, `title`, `body`, `cooldown_key`, `created_at`, `read_at`.
* `setup_controls` — status rows (upserted, never deleted): `name`, `last_run_at`, `last_evaluated_at`, `last_transition_at`,
  `run_count`, `evaluation_count`, `transition_count`, `error_count`, `last_result`, `last_error`, `updated_at`. Controls:
  `setup_state_evaluator`, `alert_persistor`, `market_hours_scheduler`.
A transition, its evaluation row, the episode update and the alert are written in ONE transaction; if the write fails nothing is
reported as persisted (the evaluation returns `ERROR`, the control's `last_error` is set, the UI is not told a transition happened).

## 9. Continuous evaluation, market hours, restart

* Scope: the current watchlist ∪ symbols with an open episode (a symbol that leaves the watchlist keeps its WATCH / READY until resolved).
* Scheduler (`setups/scheduler.py`): one daemon thread per process; while the regular session is open (weekday, 09:30 ≤ ET < 16:00) it
  runs `evaluate_all` every 60 s under the `setup_evaluator` lease; outside the session it sleeps until the next open (re-reading the
  clock at least every 15 minutes — covers machine sleep). No overnight loop, no weekend loop, no after-close evaluation in V1.
  Started from the app lifespan when `SETUP_EVALUATOR_ENABLED` (default on) and the process is not a test (`STOCK_AGENT_ENV` not
  `test` / `browser_test`); stopped at shutdown; `start()` on a running scheduler is a no-op (one loop per process; the lease covers a
  second process).
* Explicit paths that work without the scheduler: subscribing a ticker in Trader View and the socket `refresh` command evaluate that
  ticker (user actions); the stream's own 60-second cadence evaluates the active ticker only inside the session; `GET /api/setups/{symbol}`
  reads the stored state (never evaluates).
* Restart: the store is the state. On startup the service only records a `STARTUP` control row; open episodes, transition history,
  alerts and their read flags are read back from the tables. Re-evaluating unchanged inputs after a restart is a HOLD: no transition, no
  alert, same `setup_id`.
* Failure behaviour: PPE failure → PAUSED; Demand failure → WATCH may remain, READY impossible / READY → WATCH; evidence unavailable →
  READY blocked (`READY_BLOCKED_NO_EVIDENCE`); database write failure → reported, not pretended; one symbol's exception → `ERROR` row for
  that symbol, the others continue; scheduler failure → manual / subscribe / refresh evaluation still works.

## 10. API (read only + the existing acknowledgement pattern)

`GET /api/setups` (open episodes + latest evaluation per symbol), `GET /api/setups/events` (transitions, newest first, `limit`),
`GET /api/setups/alerts` (`unread_only`, `limit`), `POST /api/setups/alerts/read-all`, `POST /api/setups/alerts/{alert_id}/read`
(acknowledgement only — the same shape as `/api/strategy-alerts`), `GET /api/setups/controls`, `GET /api/setups/{symbol}`. No trade,
order, buy, sell, execute or preview path exists.

## 11. Trader View and Alerts tab

Trader View: a compact **SETUP** section after MARKET DEMAND: state label (NO ACTIVE SETUP / WATCH / READY FOR REVIEW / INVALIDATED),
"Downside Dislocation Recovery", since time, the three linked metrics (PPE Stretch, Confidence, Demand — small, never a second card),
reason / blocker text, a PAUSED notice when inputs are stale, and the evaluation stamp (evaluated at · source · mode). Styling: NONE
neutral, WATCH muted amber, READY muted blue, INVALIDATED muted red-grey; READY is a label, never a button. Messages: `setup`
(symbol-bound, inside the state, on subscribe / refresh / 60 s session cadence) and `setup_alert` (every connection, on alert creation;
the page de-duplicates by `alert_id` and shows a toast with symbol, state, reason and time). Alerts tab: a "Setup alerts" list of the
persisted alerts (newest first, unread count, mark read / read all, "Open in Trader View"). No Buy / Sell control anywhere.

## 12. Safety boundaries (tests pin them)

No order writer, trading client, broker or Robinhood import in `setups/`; no model call; no Stage 5.4 research run; no field or wording
BUY / SELL / recommendation / probability in results, alerts or UI; READY wording is READY FOR REVIEW; Prepare Buy / Sell stay disabled;
alerts and transitions never call anything but the store and the socket broadcast; PPE, Demand and evidence code unchanged.
