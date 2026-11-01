# Stage 7.5 — Shared market scanner v1 (offline core, read-only, research only)

Base: `main` at `1d81793` (PR #46, Stage 7.4, merged). Master design: `DESIGN_MARKET_INTELLIGENCE_SCANNER.md` (D.1, C.5, C.11, C.12,
C.13, E.1 row 7.5). Package: `market_scan/` (`__init__`, `features`, `flags`, `filters`, `run`), exactly the files E.1 names.

A scan result is a **research candidate**. It is not a buy / sell recommendation, a strategy signal or an order: no order, quantity,
side, broker or account field exists anywhere in a scan document (the Strategy Standard's forbidden keys are checked over all of it), no
score is produced, and nothing is ranked across horizons.

## 1. Mapping to the master design

| Master design (Stage 7.5 / D.1) | Stage 7.5 | Where |
|---|---|---|
| `market_scan/{__init__,features,filters,flags,run}.py` | built | section 4–8 |
| Tier A universe: core ∪ watchlist ∪ 12 sector ETFs ∪ SPY / QQQ / IWM; dynamic universe stays OFF | built (read only) | 3 |
| Features: `gap_pct`, `premarket_volume` / `_range`, `relative_volume_tod`, `intraday_move`, `unusual_volatility`, `sector_strength`, `dollar_volume_20d`, `spread_bps` ("pre-register exact formulas in 7.5") | built, formulas pre-registered here | 4 |
| Flags: `halt_flag`, `catalyst_flag`, `earnings_proximity`, `regime` | built; SEC parts BLOCKED; VIX percentile not pre-registered | 5 |
| Eligibility: price ≥ $5, halt false, every required input AVAILABLE and FRESH / AGING; every failure recorded | built | 6 |
| Fail-closed on STALE; deterministic tie-break; reproducible scan hash | built | 6, 8 |
| `dynamic_universe.plan()` still reports active set == watchlist; no live path imports `market_scan` | tested (S15, X01) | 9 |
| Output: one `mi_scan_runs` row + one `mi_scan_results` row per symbol | the same content as one JSON document; tables `BLOCKED_BY_DEFERRED_7_3_WIRING` | 8 |
| Output contract: a Strategy Standard v1 signal per candidate | scanner records (operator decision); v1 signals are 7.7 / 7.8's | 2 |
| Mode `MARKET_SCAN_MODE` (declared in 7.3); cadence PRE / REG / POST; edits to 7.3's daemon / router files | `BLOCKED_BY_DEFERRED_7_3_WIRING` (the files and settings do not exist) | 11 |
| Network: none extra | none at all | 9 |

## 2. Operator decisions taken at the start of Stage 7.5

The master design left four points that conflicted with this stage's constraints or were undefined. The operator chose:

1. **Output contract: scanner records.** A Strategy Standard v1 signal needs a registered, implemented, active `strategy_id` and entry /
   exit / invalidation conditions; the Day / Swing strategies arrive in 7.7 / 7.8. The scanner emits per-symbol records (features,
   flags, eligibility with reasons, provenance, quality, horizon tags); the strategies emit the v1 signals later. The records follow
   the Standard's constraints that apply: no execution field (checked with `strategy_registry.standard.forbidden_keys`), no score,
   `time_horizon` INTRADAY / SWING.
2. **Persistence: a JSON document only.** Nothing is written. The document holds exactly what `mi_scan_runs` (inputs hash, universe
   hash, data states) and `mi_scan_results` (one row per symbol) would hold.
3. **Ranking: the gap list only.** INTRADAY: eligible symbols with a usable `gap_pct`, by `gap_pct` descending, ties by symbol; rank =
   position. SWING: symbol order, unranked. No 0–100 score, no composite.
4. **Stage 7.4 WARN proceeds.** BLOCK blocks the whole scan, INSUFFICIENT_DATA leaves nothing eligible, WARN proceeds with its codes
   attached (every symbol still passes its own input checks), PASS proceeds.

## 2.1 Read-only inputs (the Stage 7.4 protocol, reused unchanged)

`run_scan(store, as_of=T, state=None, archive_root=None, feed="iex")` takes explicit paths and an explicit, timezone-aware T (the CLI
requires `--as-of`; there is no "now").

* The store (and a collector state, when named) is copied byte for byte into a private temporary directory by `quality._snapshot`,
  and refused while a writer may be active (`STORE_WRITER_MAY_BE_ACTIVE`, `STORE_CHANGED_DURING_SNAPSHOT`). **Quiet or offline stores
  only:** the copy is not an atomic snapshot of a store being written (`DESIGN_74` section 2.1).
* The Stage 7.4 report runs on that copy at the same T (`quality.build_report`); its store hash must equal the scan's.
* Every read — the 7.1 / 7.3.1 store interfaces (`latest_status`, `latest_usable`, `history`, `StoreEarningsProvider`) and this
  package's own SELECTs (fetch records, news-mention runs) — runs on the copy through `quality._open_store` / `quality._ro`. The copy is
  deleted when the scan returns. The raw archive is read with plain file reads (its anchors and manifest are hashed into the inputs).
* A missing store is `INSUFFICIENT_DATA` (`STORE_NOT_CREATED`) and is never created; a named collector state that does not exist is
  `STATE_NOT_CREATED`.

## 3. The universe (Tier A, D.1)

`dynamic_universe.core.load_core()` (read only; the 79-name core or `DYNAMIC_UNIVERSE_CORE_FILE`) ∪ the live watchlist ∪ the 12
`data/sector_map.py` ETFs ∪ SPY / QQQ / IWM, de-duplicated, each symbol with its roles (CORE, WATCHLIST, SECTOR_ETF, INDEX_PROXY).

* The watchlist is read exactly as `scanner.watchlist.load_watchlist` reads it (tested equal), without importing that module (it imports
  the market-data client). A line that is not a valid ticker is dropped and reported.
* Sector tags are the core's; `Unmapped` (CLS, SHOP) has none — a classification is never invented.
* The universe hash covers every symbol, its roles and its sector. The dynamic universe stays OFF: nothing selects, activates or writes,
  and `dynamic_universe/signals.py` (the 6.10C declarations) is untouched (`implemented()` is still empty).

## 4. Pre-registered features (`market_scan_features_v1`)

Every price / volume feature reads ONE feed (`--feed`, default `iex`) and raw bars (`adjustment = raw`: raw and adjusted series are never
mixed in one ratio, C.6). Values are rounded to 6 decimals for output. A feature that cannot be computed exactly as written is
`INSUFFICIENT_DATA` (or `NOT_APPLICABLE`) with its codes, and its value is `None` — never an estimate, never a zero. A feature computed
from graded inputs carries the worst of their states (`AVAILABLE < NOT_APPLICABLE < STALE < UNAVAILABLE < INSUFFICIENT_DATA`) and is
`usable` only when AVAILABLE: a STALE value is shown, never used.

Today `D` = the New York session date of T (a market day); `P` = the previous market day; `L` = the last completed session at T
(`calendar.last_completed_session`: today from 18:00, else the previous market day). "The snapshot" is the symbol's latest QUOTE_SNAPSHOT
known at T, used only when it is the latest record and usable (a newer absence or an unusable quote leaves every snapshot feature empty).

| Feature | Formula | Grading |
|---|---|---|
| `gap_pct` | PRE: the snapshot's last trade (traded on D, in PRE) vs its `prev_daily_bar` close; REG / POST: the snapshot's `daily_bar` open (dated D) vs its `prev_daily_bar` close (dated P). 100 × (price / previous close − 1). Both sides from one snapshot, so one feed. Both embedded bars are checked like stored ones (positive prices, high ≥ max(open, close), low ≤ min(open, close)): else `EMBEDDED_BAR_INVALID` | the quote's QUOTE_DAY state |
| `price_intraday` | the snapshot's last trade, traded on D in PRE or REG | QUOTE_DAY |
| `premarket_volume`, `premarket_range` | today's DELIVERED PRE minute bars: Σ volume (`SUM_OF_DELIVERED_BARS`); 100 × (max high / min low − 1); the coverage stated (`minutes_covered` of `minutes_elapsed`), never filled; `THIN_ON_IEX` on IEX | — |
| `relative_volume_tod` | REG only. t = the end of today's latest delivered REG minute bar; m = the minutes from 09:30 to t (capped by the session). Σ volume of today's bars of [09:30, t) ÷ the median over the 10 previous market days of the same sum over their first m minutes (an early close: up to its close). **Every** minute must have its bar on both sides: a gap today → `MINUTES_MISSING`; a prior session with a gap is MISSING → `PRIOR_SESSIONS_MISSING`. A missing minute — on IEX possibly a minute without prints — is never read as zero volume (C.13) | BAR_1MIN freshness of t |
| `atr_14` | the plain mean of the 14 true ranges over the 15 market days ending at L, TR = max(high, previous close) − min(low, previous close); a day jump beyond 25 % (`validate.DAY_JUMP`) → `DAY_JUMP_IN_WINDOW` | all 15 sessions present |
| `intraday_move` | the snapshot's last trade (traded on D in REG) vs its (checked) `daily_bar` open: 100 × (last / open − 1), and `atr_units` = (last − open) / ATR(14) | QUOTE_DAY |
| `unusual_volatility` | (the snapshot's checked `daily_bar` high − low) / ATR(14); a zero ATR is never a divisor | QUOTE_DAY |
| `day_change` | 100 × (the last trade on D / the previous close − 1), from one snapshot: sector strength's "move" | QUOTE_DAY |
| `sector_strength` | the sector ETF's `day_change` − SPY's `day_change`, in percentage points; no sector → `NOT_APPLICABLE` | worst of both |
| `dollar_volume_20d` | the median of close × volume over the 20 market days ending at L: SIP when all 20 exist, else IEX labelled `IEX_ONLY` | all 20 present |
| `spread_bps` | the median over the last 5 usable REG snapshots of D of 10 000 × (ask − bid) / mid; `NON_REPRESENTATIVE_IEX` on IEX | QUOTE_DAY |
| `price_swing` | the close of L's daily bar | present |

Medians of an even count are the mean of the two middle values. Every number above is the master design's (10 sessions, ATR 14, 20
sessions, 5 snapshots, $5, 10 min, 18 h, SMA200) or the 7.0 policy's; none is tuned, and none was chosen after seeing data.

## 5. Flags

| Flag | Values | Rule |
|---|---|---|
| `halt` | TRUE / FALSE / UNKNOWN | TRUE: any halt known at T — whenever it began: an unresolved halt from an earlier session is open today — that is still open (no resumption, one after T, or one that cannot be read: `HALT_RESUMPTION_UNREADABLE`) or resumed less than 10 min before T. The halt time is the row's store-validated `observed_at`, never the payload's. FALSE: none, and the halt feed is current under the 7.0 HALT_FEED policy (the latest OK fetch of `NASDAQ_TRADER` retrieved and ingested by T, whose time parses: ≤ 120 s FRESH, ≤ 180 s AGING, regular session only). UNKNOWN otherwise (`HALT_FEED_<reason>`) |
| `catalyst` | TRUE / NOT_ESTABLISHED / UNAVAILABLE | TRUE: a catalyst-eligible mention (the 7.2 rule: PRIMARY, confidence ≥ 0.8, tier ≤ 3) of the symbol in an article published in the 18 h before T, from the MENTIONS run (the pinned algorithm; `as_of` and `computed_at` ≤ T) that read the article's **latest revision known at T** — an article whose latest known revision no run has read yet is withheld, so a superseded run never speaks for a correction. UNAVAILABLE when nothing is found and the news poll is not current (C.5: never "no news"); NOT_ESTABLISHED otherwise. Never FALSE: the 8-K / earnings-release parts read SEC acceptance times, which stay BLOCKED. A v1 store has no mention tables: `NEWS_MENTIONS_NOT_IN_SCHEMA_V1`. A flag, not an eligibility filter |
| `earnings_proximity` | KNOWN (sessions) / UNKNOWN | the market days after D up to the next expected report date in the **latest** calendar snapshot known at T (one calendar call lists every symbol, so a symbol the latest snapshot no longer lists is `NOT_IN_LATEST_CALENDAR`, whatever an older snapshot said), when the 7.0 EARNINGS_CALENDAR policy says the calendar is current (the latest OK `ALPHA_VANTAGE` fetch); else `EARNINGS_UNKNOWN`. Expected dates only, `DATE_ONLY`; the snapshot date, the row and its evidence class are recorded |
| `regime` | spy_trend / spy_vol | `rotation_walkforward.regimes.labels` unchanged (SPY close vs SMA200; 20-session realised volatility vs 0.20) at L, over the contiguous run of SPY sessions ending at L; UNKNOWN before 200 / 21 sessions or when SPY's daily bar is not current. Macro flags: curve inversion (T10Y2Y < 0) is display only — the 7.0 MACRO policy needs a release schedule no source records, so it is never usable; the VIX percentile is `NOT_PRE_REGISTERED` (no window is defined) |

## 6. The gate, eligibility and the sequence

1. **Gate** (run level, from the Stage 7.4 verdict): BLOCK → `QUALITY_BLOCK` on every symbol, in both horizons, whatever its features
   (test S05 shows a +3 % gap with every input fresh, blocked); INSUFFICIENT_DATA → `QUALITY_INSUFFICIENT_DATA`; WARN → `QUALITY_WARN`
   on the run, proceeds; PASS → proceeds. A report that cannot run is never a pass.
2. **INTRADAY** (applies on a market day in PRE or REG; else `NOT_APPLICABLE` with `OUTSIDE_DAY_WINDOW_<session>` /
   `NOT_A_MARKET_DAY` / `CALENDAR_NOT_COVERED`, plus the gate's codes): the quote's latest record is a usable value, and QUOTE_DAY says AVAILABLE
   (`QUOTE_<reason>` otherwise: `AGE_LIMIT`, `OUTSIDE_WINDOW`, `LATEST_RECORD_IS_ABSENCE`, ...); `price_intraday` ≥ $5
   (`PRICE_UNAVAILABLE` / `PRICE_BELOW_MIN`); halt FALSE (`HALTED`, or `HALT_STATUS_UNKNOWN` — the halt feed fails closed for every
   day candidate).
3. **SWING** (applies always): L's daily bar is the latest record and present (`DAILY_BAR_<reason>`, e.g. `SESSION_MISSING`,
   `LATEST_RECORD_IS_ABSENCE`); `price_swing` ≥ $5; halt not TRUE (an UNKNOWN halt feed leaves swing unaffected, C.5 / C.13). From 18:00
   to the bar's completion the horizon carries `DAILY_BAR_DUE_BEFORE_STORABLE` (section 12).
4. **Sequence**: INTRADAY — the gap list (section 2, decision 3), then every other symbol in symbol order, unranked; SWING — symbol
   order, unranked. `filters.sequence` refuses any other horizon. (The document key is `sequence`, not `order`: "order" is a Strategy
   Standard forbidden key.)

Every symbol of the universe gets one result in both horizons, with every reason; nothing is dropped — also when the store was not read
(`INPUT_NOT_READ`), or a run-level read failed (the run is then `BLOCKED`, `SCAN_INPUT_UNREADABLE`).

## 7. Point in time

Every value is the store's KNOWN revision at T (observed, complete, available and ingested by T); fetch records count when retrieved
and ingested by T; a mention run counts when its `as_of` and `computed_at` are ≤ T; SQL comparisons with T also require the store's own
timestamp format (the 7.4 `_le` guard), so a malformed time is never compared as text. Test S06 writes, after T: a snapshot quoted
after T, one quoted before T but retrieved after it, a daily-bar revision, a halt, a late minute bar and (S12) a mention run computed
after T — the scan at T is unchanged in every result, and a later scan sees them.

## 8. The document

`run` (D.1's `mi_scan_runs`): status COMPLETED / BLOCKED / INSUFFICIENT_DATA with codes, `inputs_sha256` (scan and pre-registration
versions, T, feed, universe hash, the store's and the state's byte hashes, the archive's anchors / manifest hashes), `universe_sha256`,
`data_states`, the Stage 7.4 summary (verdict, evidence scope, every check, the store hash, `not_proof_of_live_readiness`) and the gate.
`results` (D.1's `mi_scan_results`): one per symbol — features, flags, the quote and daily-bar assessments, inputs (the quote's
provenance in full; bar, snapshot and minute inputs as digests: count, obs-id hash, datasets, evidence classes, first / last time),
evidence classes, per-horizon eligibility. Then `sequence`, `horizons` (counts), `context` (regime, feed assessments),
`pre_registered`, `deferred`, `not_pre_registered`, `blocked`, `limitations`, `disclaimer`.

`scan_sha256` covers everything except itself and `invocation` (the paths): the same store bytes, T, feed, universe and
pre-registration give the same document and hash wherever the files lie (test S04). JSON is strict (`allow_nan=False`).

CLI: `python -m market_scan.run --store <db> --as-of <ISO time> [--state] [--archive-root] [--feed iex|sip] [--watchlist <file>]
[--core-file <file>] [--format text|json]`; exit 0 COMPLETED with a 7.4 PASS, 1 COMPLETED with a WARN, 2 BLOCKED, 3
INSUFFICIENT_DATA, 4 a refused invocation.

## 9. Safety boundaries and isolation

* MONITOR_ONLY, Paper Auto OFF, MANUAL_ARM, Robinhood read-only, Evidence Refresh OFF unchanged; SEC acceptance-time uncertainty stays
  BLOCKED; no provider request, migration, deployment, configuration, route, thread or job.
* The package imports **directly** market-intel, `dynamic_universe.core`, `data.sector_map`, `api.validation`,
  `rotation_walkforward.regimes` / `backtest.replay` (the regime) and `config` (the watchlist path) only — no network, broker, execution,
  proposal, setup, safety or model module (test X01); it holds no SQL write and no file write other than the CLI's output stream;
  nothing outside `market_scan/` imports it. **Transitively**, through the frozen data layer, it loads `evidence.net` / `evidence.rules`
  (the 7.2 news attribution's tier rules; nothing calls a fetch), `setups.market_hours` and `autorun` (the calendar) and `data.events`
  (the earnings model) — and no Alpaca, broker, execution, proposal, paper, Robinhood or model module, and no thread (X01 pins the list).
* The earlier stages' isolation guards ("nothing outside `market_intel/` mentions `market_intel`", 7.0–7.4; "only these directories
  import `dynamic_universe`", 6.10C) now name `market_scan/` as the one declared reader. X01 proves nothing outside `market_scan/`
  imports it, so no live path reaches the data layer through it.

## 10. Tests and verification

`tests/test_market_scan_75.py`: S01–S17 (the feature maths on fixtures; a whole scan; fail-closed on STALE; determinism and the
tie-break; the 7.4 gate; point in time; missing, stale, malformed and contradictory inputs; duplicates and revisions; provenance and
evidence; Day / Swing separation; halts; catalysts; earnings; the regime; the universe and the inactive dynamic universe; read-only
behaviour; the CLI), V01 (the figures recomputed from raw SQL), A01–A02 (tampered rows and unreadable symbols fail closed), R01–R10
(the independent review's findings, section 14, each failing on the reviewed commit), X01 (isolation), F01–F03 (scope). Fake clocks,
fixtures and temporary paths only.

**Mutation check.** Each guarantee of `market_scan/` was undone in turn (a harness that restores every file and stops if a test writes
into the repository's `data/`). The first run caught 70 of 81: six gaps were cases no test reached (a trade from another day, a negative
minute volume, a zero ATR, yesterday's embedded bar for the intraday move, SIP daily bars, three post-close quotes), three were in-place
reads the fixtures' own empty `-wal` / `-shm` hid (S16 now scans a store with nothing beside it and checks that the 7.4 report reads the
scan's copy), and two were redundant branches, removed. After the review's fixes, 95 of 95 mutations are caught. Equivalent mutations are
listed in the harness, not counted (the mention-run algorithm filter: one algorithm exists; the 7.4 report's store-hash cross-check: a
private copy of a private copy; the minute bars' REG filter: the window already lies in the regular session except after an early close).

## 11. Deferred dependencies (`BLOCKED_BY_DEFERRED_7_3_WIRING`)

* `MARKET_SCAN_MODE` (OFF | SHADOW): declared by Stage 7.3's `config.py` marked block, which does not exist.
* The cadence (PRE every 5 min; REG every 60 s with shortlist minute bars; POST at 16:20 and 18:00 ET): a scheduler is the daemon's.
* `mi_scan_runs` / `mi_scan_results` tables: no schema; this core returns the same content and writes nothing.
* The edits to 7.3's own daemon / router files: they do not exist.

## 12. Limitations and remaining risks

* **No production observation has been scanned.** Every test store is a fixture or an offline-verification store.
* **Quiet or offline stores only** (section 2.1): a busy store is refused, and the copy is not an atomic snapshot.
* **No premarket quote policy exists** (7.0 QUOTE_DAY is regular-session only): a PRE gap list shows its values, but nothing is eligible
  before 09:30; the halt feed policy is regular-session only too.
* **A missing minute bar cannot be told from a minute without prints** from the store alone: such a session is MISSING, never zero.
* **Raw bars from one feed.** Dollar volume prefers SIP; every other feature reads the chosen feed. IEX volumes and spreads are labelled.
* **Survivorship.** The universe is today's Tier A; a scan of a past T is survivorship-biased.
* **The 7.4 gate is store-wide.** One malformed row anywhere blocks every symbol (fail closed by design).
* **Regime macro flags are display only; the VIX percentile is not pre-registered.**
* **Earnings proximity reads expected dates only**; SEC confirmations stay blocked.
* **No swing candidate between 18:00 and the daily bar's completion** (20:00; 17:00 on an early close): the 7.0 calendar expects
  today's bar from 18:00, the 7.1 store holds it only once complete. The scan says so (`DAILY_BAR_DUE_BEFORE_STORABLE`).
* **ATR is withheld after any close-to-close move beyond 25 %** (`validate.DAY_JUMP`), for 14 sessions, with ATR units and unusual
  volatility: on raw bars a split cannot be told from a real move without a corporate-action source. This is stricter than the master
  design's plain ATR(14), and it hits exactly the most volatile names.
* **Premarket volume is the sum of the delivered bars**, with its coverage stated; premarket prints are sparse, so it is not judged.
* **Unverified offline:** whether the provider rolls a snapshot's daily and previous-day bars before the open; if not, a premarket gap
  reads `PREV_CLOSE_NOT_PREVIOUS_SESSION` (fail closed).
* **Private helpers of `market_intel.quality` are reused** (`_snapshot`, `_open_store`, `_ro`, `_le`, `_aware`, `_iso`): one
  implementation of the read-only rule. They are frozen with Stage 7.4; a change there is a declared change for both stages.

## 13. Outstanding operator decisions

1. A premarket quote freshness policy (a 7.0 policy change), if PRE gap-list candidates should ever be eligible.
2. Persisting scan runs (tables) and the cadence: with the Stage 7.3 wiring, when authorized.
3. A VIX-percentile window, if the regime should carry it; a macro release schedule, if macro flags should become usable.
4. The SIP decision for spreads and the day strategy's real-time phase (master design E.2 #9).
5. Aligning the 7.0 daily-bar cutoff (18:00) with the 7.1 completion time (20:00), a 7.0 policy change, if post-close swing scans should
   run at 18:00.
6. A corporate-action source (or adjusted bars) for ATR across splits, if the 25 % rule should not withhold ATR after real moves.
7. The merge of this stage.

## 14. Independent review

A reviewer working from the requirements and the code alone (not from this document's conclusions) built adversarial stores against
`9abc03e` (30 probes, each asserting what it found). Every finding was reproduced by a regression test that fails on `9abc03e`, and fixed:

| # | Severity | Finding | Resolution |
|---|---|---|---|
| B1 | BLOCKING | The halt flag read only halts that began on D: a stock halted at yesterday's close, never resumed, was FALSE and ranked first | every halt known at T counts; an unresolved one is open today (R01) |
| B2 | BLOCKING | A snapshot's embedded bars were never checked: an open above the day's high became the top gap | both embedded bars checked like stored ones: `EMBEDDED_BAR_INVALID` (R02) |
| S1 | SHOULD_FIX | Relative volume summed whatever minute bars existed: a missing minute read as zero volume (0.2x; or 29x with sparse prior sessions) | t ends at today's last delivered bar; every minute covered on both sides, else MISSING (R03) |
| S2 | SHOULD_FIX | A superseded mention run (before a correction re-tagged the article) still made the catalyst TRUE | only the run that read the article's latest known revision speaks for it (R04) |
| S3 | SHOULD_FIX | A fetch time the schema accepts but no clock can read (second 61) crashed the scan | parsed by 7.4's reader, skipped; run-level reads fail closed into a BLOCKED document (R05) |
| S4 | SHOULD_FIX | From 18:00 to 20:00 no swing candidate can be eligible (7.0 cutoff vs 7.1 completion), silently | recorded on the horizon and in the limitations; the alignment is an operator decision (R06) |
| S5 | SHOULD_FIX | Earnings proximity read an older snapshot's date after the latest snapshot dropped the symbol | the latest snapshot only (R07) |
| S6 | SHOULD_FIX | A halt row whose payload time could not be read was FALSE | the validated `observed_at`; an unreadable resumption keeps it open (R08) |
| M1 | MINOR | Swing read past a newer absence of the daily bar | the store's `latest_status` (R09) |
| M2 | MINOR | The scan hash changed with the watchlist's order | the watchlist is recorded as a set (R10) |
| M3 | MINOR | Section 9 described direct imports as if transitive | both described; X01 pins the transitive list and no thread |
| M4 | MINOR | `day_change` formula missing "− 1" here | fixed (the code was right) |
| M5 | MINOR | No results when the store was not read, against section 6 | a result per symbol, `INPUT_NOT_READ` (S16) |
| M6 | MINOR | Misleading codes: NOT_A_MARKET_DAY for an uncovered year; no UNAVAILABLE catalyst on a stale news poll; a closed day window hid the gate; no earnings / regime provenance | `CALENDAR_NOT_COVERED`; UNAVAILABLE (S12); gate codes kept (R10); snapshot / row / evidence and a SPY-bar digest recorded |
| M7 | MINOR | ATR withheld after any > 25 % day goes beyond the master's ATR(14) | kept (fail closed; a split is indistinguishable on raw bars); limitation and operator decision 6 |

The reviewer found sound: point in time (later rows, late arrivals, mention runs computed after T); the 7.4 gate; the feature maths;
no feed mixing; ranking and horizon separation; no execution field or score; the regime's reuse; the catalyst never FALSE; hash
determinism; read-only behaviour (inputs byte-identical, temp root empty even on a crash, a CLI subprocess creates nothing); calendar
edges; isolation.
