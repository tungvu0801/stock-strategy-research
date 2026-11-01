# Stage 7.0 — market intelligence contracts

Stage 7.0 is the first stage of the shared, read-only Market Intelligence Data Layer. The full plan lives in
`DESIGN_MARKET_INTELLIGENCE_SCANNER.md` in the workspace, outside this repository: sections C.1–C.8 and stage table E.1.

**Display-only and pure.** No network, thread, file, database, route or config change. Nothing in the live path imports the package.
It fixes the vocabulary and the policies before any data exists, so later stages cannot quietly choose them after seeing data.

## 1. What it adds

| File | Contents |
|---|---|
| `market_intel/__init__.py` | version, stage, modes (`OFF`, `SHADOW`; no `ACTIVE`), statuses, freshness classes, kinds, sessions, providers |
| `market_intel/models.py` | `Observation` (the envelope) and `FetchRecord`, pydantic `extra=forbid`, frozen |
| `market_intel/settings.py` | `MARKET_INTEL_MODE` and the request budgets, normalised; read with defaults (config declares none yet) |
| `market_intel/freshness.py` | the pre-registered freshness and state policy per kind |
| `market_intel/validate.py` | deterministic quality codes for bars, quotes, feeds, news and events |
| `market_intel/calendar.py` | `PRE` / `REG` / `POST` / `CLOSED`, a thin adapter over the Stage 6.10A classifier and its static 2026–2027 table |
| `market_intel/budget.py` | per-provider token buckets on an injected clock, daily caps, back-off, plan pressure |

## 2. The envelope invariants

* Every time is timezone-aware and stored in UTC. A naive time is rejected.
* `available_at >= retrieved_at`: a value is never usable before we had it. Consumers read by `available_at <= T`
  (`Observation.usable_at`).
* A backfilled row has `available_at == retrieved_at`, so history is never presented as having been known live.
* An `UNAVAILABLE` or `INSUFFICIENT_DATA` observation carries no payload. A missing value is never a zero.
* A `STALE` status has `STALE` freshness, and a `STALE` value is never `AVAILABLE`.
* Quality flags are the closed set `validate.QUALITY_CODES`.
* No field or payload key names an order, quantity, side, signal, target or price instruction, **at any depth**: keys inside nested
  objects and lists are checked too. Words are matched as whole snake_case or camelCase tokens.
* Payload values are JSON-like only: strings, integers, finite floats, booleans, null, objects and lists, at most 6 levels deep.
* The stored payload is read-only. Objects become `ReadOnlyDict` and lists become tuples, so `o.payload["order_qty"] = 5` raises after
  validation. A frozen model alone would not prevent it. The caller's own dict is copied, not kept.

## 3. Freshness policy (pinned by `test_Q02`)

| Key | Fresh | Aging | Window | When stale or unavailable |
|---|---|---|---|---|
| `QUOTE_DAY` | ≤ 5 s | ≤ 15 s | REG | no new day candidate for that symbol |
| `QUOTE_SWING` | ≤ 60 s | ≤ 300 s (= `setups.QUOTE_MAX_AGE_SECONDS`) | REG | swing intraday confirmation skipped |
| `BAR_1MIN` | bar end ≤ 120 s | ≤ 300 s | REG | features `INSUFFICIENT_DATA`; no IEX prints is `NO_PRINTS`, not STALE |
| `BAR_1HOUR` | hour start ≤ 70 min | ≤ 130 min | REG | hourly confirmation skipped |
| `NEWS_POLL` | ≤ 3 min | ≤ 10 min | PRE + REG + POST | catalyst flag `UNAVAILABLE`, never "no news" |
| `HALT_FEED` | ≤ 2 min | ≤ 3 min | REG | **all** new day candidates blocked |
| `SEC_POLL` | ≤ 15 min | ≤ 30 min | weekdays 06:00–22:00 ET | filing flag `UNAVAILABLE` |
| `EARNINGS_CALENDAR` | refreshed today (NY date) | ≤ 36 h | always | swing candidates without a known date blocked |
| `BAR_1DAY` | the last completed session is present | — | from 18:00 ET on a market day | no swing signal for that symbol |
| `MACRO` | the latest scheduled release observed | none: no usable aging state | — | regime `UNKNOWN`, the pre-registered default |
| `FUNDAMENTALS` | quarter end ≤ 200 days (= `value_quality.MAX_FUNDAMENTAL_AGE_DAYS`) | — | — | fundamental filter `INSUFFICIENT_DATA` |

Every assessment carries `usable`, which is True only when the status is `AVAILABLE`. A consumer that needs the value requires
`usable`. An aging freshness, a reported age or a previous vintage never makes a value usable.

**MACRO** is stricter than the C.5 draft (second review). From the scheduled release time until that release is observed, the series has
no current value:
* the status is `UNAVAILABLE` (`RELEASE_PENDING`), not usable;
* one day later it escalates to `STALE` (`RELEASE_MISSING`);
* the previous vintage stays history and is never presented as current.

The following rules apply to every key:
* Nothing seen gives `UNAVAILABLE` (`NEVER_OBSERVED`).
* A reference time more than 2 s in the future gives `UNAVAILABLE` (`FUTURE_TIMESTAMP`).
* Outside the window gives `NOT_APPLICABLE`, with the age reported.
* A weekday outside the calendar table gives `UNAVAILABLE` (`CALENDAR_NOT_COVERED`) for the REG and extended-hours windows and for
  the daily bar, including when the session the bar must come from is uncovered.

## 4. Validation codes

The codes are pre-registered, with thresholds pinned by `test_Q02`:
* **Bar shape:** `HIGH_BELOW_BODY`, `LOW_ABOVE_BODY`, `NEGATIVE_VOLUME`, `NON_FINITE`, `NON_POSITIVE_PRICE`.
* **Bar series:** `DUPLICATE_TS`, `NOT_INCREASING`, `OUTSIDE_SESSION`, `GAP_MINUTES`.
* **Daily series:**
  - `SESSION_HOLE`: a hole of 2 or more market days blocks the dataset.
  - `DAY_JUMP`: above 25 %, equal to `mean_reversion.NORMAL_DAY`. A jump is classified later; it is never fixed here.
  - `NON_SESSION_DAY`: a bar dated on a weekend or a table holiday. It is blocking.
  - `CALENDAR_NOT_COVERED`: a bar, or a gap between two bars, on a weekday outside the table. The series cannot be verified, so it is
    blocked; it is never assumed complete. Minute bars on an uncovered weekday get the same code.
* **Quotes:**
  - `CROSSED` and `LOCKED`.
  - `WIDE_SPREAD`: wider than 50 bps of the mid.
  - `STALE_PRINT`: the last trade is more than 60 s older than the quote.
  - `NON_POSITIVE_QUOTE`.
* **Feeds and clocks:**
  - `FEED_DIVERGENCE`: IEX and SIP closes differ by more than 10 bps. It is a report, never a correction.
  - `CLOCK_SKEW`: above 2 s.
  - `FUTURE_OBSERVED` and `BACKFILLED`.
* **News:**
  - `EMPTY_HEADLINE` and `MISSING_PUBLISHED`.
  - `FUTURE_DATED`: more than 5 min ahead of retrieval.
  - `DENY_HOST_LINK`: the configured evidence deny list, subdomains included; such a link is kept as metadata only.
* **Events:** `UNCONFIRMED_PAST` (no 8-K item 2.02) and `HALTED_OPEN`.

`BLOCKING_CODES` lists the codes after which a value is never usable as it is.

## 5. Calendar

`calendar.py` adds no clock of its own. It maps `autorun.session.classify` as follows: PREMARKET becomes PRE, REGULAR becomes REG,
AFTER_HOURS becomes POST and CLOSED stays CLOSED. `test_K01` sweeps a whole early-close day minute by minute to prove the two agree.

`rule_holidays(year)` encodes the NYSE holiday rules. It exists only to cross-check the static 2026–2027 table (`test_K02`).

**Coverage.** A weekend is a weekend in every year. A weekday outside the table is `NOT_COVERED` and is never guessed:
* `market_day_status` says so;
* `is_market_day` raises `CalendarNotCovered`;
* the previous- and next-market-day walkers return None;
* validation flags `CALENDAR_NOT_COVERED`, a blocking code;
* freshness fails closed.

A 2025 backfill or a 2028 date can therefore never be read as verified sessions (`test_K03`, `test_V05`).

**Coverage strategy (proposed; extending coverage is its own approved change):**
1. Years are added to a versioned table from the NYSE published schedule, never derived from rules.
2. Each added year is pinned by a test. The test cross-checks `rule_holidays` and lists the exceptional closures that rules cannot
   derive. For 2025 that is the 2025-01-09 national day of mourning, plus the early closes of 2025-07-03, 2025-11-28 and 2025-12-24.
3. History (2025 and earlier) goes into a `market_intel`-owned table. It never goes into `autorun/calendar.py`, which is frozen and used
   by the executor's session health.
4. A live year (2028) is a separate, declared change to `autorun/calendar.py`, made before 2028.
5. Stage 7.1 accepts a backfill only for covered years. Any uncovered dataset stays blocked.

**Stage 7.1 implemented the 2025 step** (`market_intel/calendar_history.py`, `DESIGN_71` section 5). It was verified against the NYSE
2025 calendar release, the NYSE notice of the 2025-01-09 closure, and the archived IEX daily bars of 82 symbols (250 sessions, the
11 closures exactly). 2024 and earlier, and 2028 onward, remain `NOT_COVERED`.

One fact to verify: post-market end on early-close days is taken from the 6.10A classifier as 20:00 ET.

**Stage 7.1 resolved that fact, as the one declared exception to the single-source rule** (operator-approved 2026-10-31; `DESIGN_71`
section 5). The NYSE release footnotes every 2025-2027 early-close date: the late trading sessions "will close at 5:00 p.m." So
`market_intel` ends POST at 17:00 New York time on those dates. For 2025 the history table does this. For 2026-2027 a versioned,
`market_intel`-owned override (`nyse_late_trading_2026_2027_v1`) turns the 6.10A classifier's AFTER_HOURS into CLOSED from 17:00 on
exactly 2026-11-27, 2026-12-24 and 2027-11-26. `autorun/calendar.py`, the 6.10A classifier and the executor are unchanged, and every
holiday and 13:00 close still comes from them.

## 6. Budgets (design C.8)

These are defaults; a value outside its range reads as the default and is reported.

| Provider | Default | Hard range |
|---|---|---|
| Alpaca live | 40/min | 1–40 (at most 20 % of Basic's 200/min, which the evaluator and Trader View share) |
| Alpaca backfill | 60/min | 1–60 |
| SEC | 60/min | 1–60 (at most 1 request/s) |
| FRED | 30/min | 1–30 |
| Nasdaq halts | 1/min | 1 |
| Alpha Vantage | 6/day | 0–6 |

Back-off follows these rules:
* **429 or 403:** wait for Retry-After, capped at 30 s; 60 s when no header is given.
* **ACCESS_DENIED:** blocked for the rest of the cycle.
* **Plan pressure:** two 429s within 10 minutes halve the rate and set `PLAN_LIMIT_PRESSURE`. Only an explicit reset restores the rate.

Counters are in memory; Stage 7.1 persists them.

## 7. Scope and bookkeeping

* `BASE_70` is PR #40's head: this stage is stacked on Stage 5.4.2. Merge PR #40 first.
* `NEW_70` covers the package, its test file and this note.
* `EDITED_70` covers the CI manifest and the freeze bookkeeping. Two changes are involved:
  - The earlier stages' F01 sets are widened by `NEW_70 | EDITED_70`.
  - The Stage 5.4.2 F tests now read what 5.4.2 left at `BASE_70`, as 5.4.2 did for 6.10D.
* `SEMANTIC_70 = {}` and `INSTRUMENTATION_70 = {}`.
* The frozen-controls hash `b5d4a56e…` and both exposure-gate contract hashes are unchanged (`test_F02`).

## 8. Next (each a separate, approved stage)

* **7.1:** its own `data/market_intel.db`, created lazily and append-only, plus the backfill CLI. The first run and any `feed=sip`
  request need approval.
* **7.2:** source parsers.
* **7.3:** the daemon and read-only API, `OFF` by default. This is the first wiring stage.
