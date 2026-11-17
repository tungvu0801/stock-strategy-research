# Historical data contract, v1 (shared by MD-A and MD-B): revision 2, 2026-11-16

**Status: APPROVED, frozen as revision 2 final (2026-11-16).**
- **Review history:**
  - revision 1 NOT APPROVED (20 text changes);
  - revision 2 delta review APPROVE WITH CHANGES (7 minor edits);
  - all applied verbatim, plus the optional cap: `SPLIT_UNADJUSTED` caps the verdict.
- The owner's cash-ledger clarification (P3a) is included.
- The approved sha256 is recorded in `project-planning\md\CONTRACT_SHA256.txt`.
- When approved, this file's sha256 is recorded, and MD-A and MD-B implement against exactly that version.
- A change after approval is a new version (v2) with its own review.

**Purpose.** One provider-independent, point-in-time description of historical daily equity data. A provider adapter (MD-A) produces it, and the evaluation engine (MD-B) consumes only it. CRSP, Alpaca or a licensed commercial source can then be added later as an adapter without changing the engine.

**Scope of v1:** US equities and ETFs, daily sessions, long-only research.
- **Out of scope:** intraday, options, short selling, fundamentals, news text.
- **No WRDS or CRSP account or library dependency.** A future CRSP adapter reads exported files offline (review item 20).

## 1. Principles (binding)
- **P1. Point-in-time.** Every value carries the session it describes and `known_at` (UTC, when it became knowable). Decision logic at time *t* uses only values with `known_at ≤ t`.
  - **Declared exceptions, and only these:**
    - (i) adjusted factors (§3.2) may be used **only as ratios within a window that ends at or before t**, so their absolute level, which encodes later events, never matters (test (i), §7);
    - (ii) `FIXED_CURRENT_UNIVERSE` membership (§3.4), which is flagged and verdict-capping.
- **P2. Decision and fill timing.**
  - A signal is computed after the close of session *d*, using only data with `known_at ≤ close(d)`.
  - Its order fills at the **raw open of the next calendar session *d+1*** through the execution channel (§4), never on the bar that produced it.
  - If *d+1* has no bar for that instrument, the order is **not filled** (`NO_FILL_GAP`), and the reason is recorded.
- **P3. Three price views, each with one job.** They are never mixed, and a view a provider cannot supply is `UNAVAILABLE`, never derived silently.
  - **Raw (executable)** prices are used for price filters and thresholds (for example a $5 minimum) and for **every cash-ledger amount**: fills, position marks and cash.
  - **Split-adjusted** prices are used for signal computation only.
  - **Total-return** series are used **only for benchmark returns (e.g. SPY buy-and-hold) and for reconciliation checks.** They never drive the cash ledger.
  - **When a view is missing:** without `split_factor` a provider gives no signals unless the run is explicitly flagged `SPLIT_UNADJUSTED`. Without raw open/high/low the provider is integrity-only (§4.1).
- **P3a. Cash-flow accounting (owner clarification, 2026-11-16).** The ledger books actual cash flows from raw prices and explicit events.
  - **Bookings are not decisions.** A ledger booking happens on the event's `effective_session` or `pay_session`, whatever its `known_at`; `known_at` governs only decision visibility.
  - **Order of operations within a session:** first the corporate-action step, then fills at the open, then marks at the close.
    - The corporate-action step comes first: splits and stock dividends adjust the share count; cash-dividend credits due on their `pay_session`, mergers and delistings are booked.
    - A delisting is booked on its `effective_session`, or on the session after `last_trade_session` when that is earlier.
  - **Fills:** shares = floor(order notional ÷ raw_open), **whole shares only**. Cash is debited or credited at raw_open × shares, plus costs. An exit order closes the whole position. Cost basis includes costs.
  - **Split, reverse split or stock dividend:** shares × `ratio` (§3.3); the **total** cost basis is preserved (the per-share basis is divided by `ratio`); cash is unchanged.
    - A fractional share is paid at `cash_in_lieu_per_share` when the event carries it. Otherwise the fraction is valued at 0 and flagged `FRACTION_UNPAID`.
  - **Cash dividend entitlement:** the shares held at the **close of the session before the ex-date**. A buy filled at the ex-date open is not entitled; a sell filled at the ex-date open is.
    - Cash is credited on `pay_session`. If `pay_session` is UNKNOWN it is credited on the ex-date, flagged `PAY_DATE_UNKNOWN` (an optimistic approximation, recorded as such).
  - **Spin-off:** child shares = parent shares × `child_ratio`, plus cash in lieu. The cost basis splits per `cost_basis_allocation`.
    - When that is UNKNOWN, it splits by the raw closing values on `effective_session`, or by the child's first raw close at or after `effective_session` (rule recorded).
    - If the child has no such close, the event is flagged `CORPORATE_ACTION_UNACCOUNTED`.
  - **Merger:** the position is replaced by `stock_ratio` acquirer shares and/or `cash_amount` per share.
  - **Delisting:** the position closes at `last_raw_price × (1 + delisting_return)`. **When `delisting_return` is UNKNOWN it is −100 % in primary results,** and closing at `last_raw_price` is a labelled sensitivity.
  - **No double counting:** P&L comes only from raw prices plus booked events. Total-return factors are never applied to a ledger position.
  - **Incomplete events:** when the manifest's `events_completeness ≠ COMPLETE`, **every held instrument** is flagged `CORPORATE_ACTION_UNACCOUNTED` and the run reports it. Inferred events (§4.1) are never booked.
- **P4. Unknown is explicit.** A missing value is a `{"status": "UNKNOWN" | "UNAVAILABLE", "reason": "<CODE>"}` object. It is never `0`, `False`, an empty list or a forward-filled value.
- **P5. Provenance and licence travel with the data** (§6). Real-data evaluation is refused unless the licence permits it (§6.2).
- **P6. Adapters are read-only and offline.**
  - No network connection; no write into the source; **no database opened, not even read-only.**
  - `Ms1EvidenceProvider` reads **only the raw archive** (`manifest.jsonl` + `.json.gz` bodies, each sha256-verified), never the SQLite store.
  - Fetching is a separate, separately approved tool.
- **P7. Determinism and canonical form.** The same input files and the same adapter version give byte-identical canonical output (§6.3) and the same `content_sha256`.

## 2. Time and calendar
- **Authoritative calendar: a single function pinned by MD-A,** `session_times(session) → (open_utc, close_utc)`, covering **2016-01-01..2026-12-31**.
  - It is built from `research_data/nyse_calendar.py`: the trading days, full closures and the listed early-close dates.
  - Regular sessions are 09:30–16:00 America/New_York; early closes end at 13:00. Conversion to UTC uses `zoneinfo` with the **`tzdata` package as a pinned dependency** (DST-correct). Its absence is BLOCK.
  - It is cross-checked against `market_intel/calendar.py` for the overlapping years (2025–2026); any disagreement is BLOCK.
  - **A date outside the coverage is `CALENDAR_NOT_COVERED`, and the dataset fails closed.**
- **`known_at` of a daily bar is `close_utc(session)`,** computed from the calendar. It is **never** taken from a provider's `available_at` or `retrieved_at`.
  - This is **simulated knowability for research only**; it never feeds any live usability decision.
  - `retrieved_at` is provenance only.
- **"Next session" means the next calendar session** (`session_times` order), whether or not the instrument traded on it.

## 3. Entities

### 3.1 Instrument
| Field | Rule |
|---|---|
| `instrument_id` | Stable over the instrument's life and **never reused**. It is provider-mapped: a CRSP PERMNO as `CRSP:<permno>`, or, for symbol-only providers, `<PROVIDER>:<symbol>:<first_session>` (e.g. `ALPACA:SNDK:<its first bar's session>`). **For symbol-only providers, `instrument_id` is dataset-scoped:** joins across datasets (e.g. `data_exposure`) use symbol plus session range. `POSSIBLE_TICKER_REUSE` is a WARN and does not split the instrument. |
| `symbol_history` | list of {symbol, valid_from_session, valid_to_session or null}. A provider without the history sets `symbol_history_status = UNKNOWN`. |
| `asset_type` | `COMMON_STOCK` \| `ETF` \| `OTHER` |
| `listing` | list of {exchange, from, to or null}, or UNKNOWN |

### 3.2 Daily bar (one per instrument per session the instrument traded)
| Field | Rule |
|---|---|
| `instrument_id`, `session` | the key, unique |
| `raw_open`, `raw_high`, `raw_low`, `raw_close`, `raw_volume` | Unadjusted. Prices are finite and > 0; volume ≥ 0; `raw_low ≤ min(open, close)`, `raw_high ≥ max(open, close)`. A failing row is removed and becomes a `REMOVED_INVALID` gap, never repaired. Any of open/high/low may be UNAVAILABLE (P3). |
| `split_factor`, `total_return_factor` | **Cumulative, backwards from `adjustment_as_of`:** factor(d) = the product of the `ratio`-equivalents of the qualifying events with effective session in (d, `adjustment_as_of`]. It is 1.0 on and after the last such event. Split-adjusted price = raw ÷ `split_factor`. Total-return price = raw ÷ `total_return_factor`, where `total_return_factor` **includes** `split_factor` (it covers splits plus distributions). **Ratio-equivalents:** a split, reverse split or stock dividend uses its `ratio`. A cash distribution D with ex-date *e* uses `raw_close(e−1) ÷ (raw_close(e−1) − D)`. A spin-off uses D = `child_ratio` × the child's raw close on *e*. Either factor may be UNAVAILABLE. |
| `volume_feed` | `CONSOLIDATED` \| `SINGLE_VENUE:<name>` \| `UNKNOWN` |
| `price_source` | `CONSOLIDATED` \| `SINGLE_VENUE:<name>`. IEX opens are venue prints, not the official open. |
| `known_at` | `close_utc(session)` (§2) |
| `source_ref` | the archive file's sha256 plus the index of the row in it |

- **`gaps(instrument_id)`** lists {session, reason: `NO_BAR` \| `HALT` \| `SUSPENDED` \| `REMOVED_INVALID` \| `UNKNOWN`} between the instrument's first and last bar.
- **A gap is never forward-filled.**
- **Adjusted factors are snapshots,** so they are used only per P1 exception (i). `adjustment_as_of` is recorded in the manifest.

### 3.3 Corporate-action and listing event
| Field | Rule |
|---|---|
| `event_id`, `instrument_id`, `type` | `type` ∈ `SPLIT`, `REVERSE_SPLIT`, `STOCK_DIVIDEND`, `CASH_DIVIDEND`, `SPECIAL_DIVIDEND`, `SPIN_OFF`, `SYMBOL_CHANGE`, `MERGER`, `DELISTING`, `TICKER_REUSE` |
| `effective_session` | the ex-date or effective session |
| `record_session`, `pay_session` | UNKNOWN allowed |
| `known_at` | When the event became public. If the provider lacks it, `known_at_status = UNKNOWN`, and decision logic treats the event as known only at the **open of `effective_session`**, never earlier. Bookings are unaffected (P3a). |
| `ratio` | **New shares per old share** (2-for-1 = 2.0; 1-for-10 = 0.1; a 5 % stock dividend = 1.05). For splits, `split_factor(eff − 1) = split_factor(eff) × ratio`. |
| `cash_amount` | per old share (dividends, merger cash) |
| `cash_in_lieu_per_share` | price per fractional share, or UNKNOWN |
| spin-off fields | `child_instrument_id`, `child_ratio`, `cost_basis_allocation` (UNKNOWN allowed) |
| merger fields | `acquirer_instrument_id`, `stock_ratio`, `cash_amount` |
| delisting fields | `last_trade_session`, `last_raw_price`, `delisting_reason`, `delisting_return` (relative to `last_raw_price`; UNKNOWN allowed) |

### 3.4 Universe membership (point-in-time)
- `membership(universe_id, session)` returns the eligible `instrument_id`s, plus `membership_basis` (`POINT_IN_TIME` | `FIXED_CURRENT_UNIVERSE`) and, for `POINT_IN_TIME`, the `known_at` of each change.
- An instrument is never eligible before its first listed session (**or its first bar, when no listing data exists**) or after its delisting.
- **With `FIXED_CURRENT_UNIVERSE`,** every result carries `SURVIVORSHIP_BIAS_PRESENT`, and **every edge or alpha claim is capped at NOT_ESTABLISHED.**

### 3.5 Scheduled events (earnings and similar)
- Each carries `event_session`, `timing` (`BEFORE_OPEN` | `AFTER_CLOSE` | `DURING` | `UNKNOWN`) and `known_at`.
- **No point-in-time source:** the provider returns `EARNINGS_DATA_UNAVAILABLE`. Strategies bound by D.3 then produce **no candidates**, and that is reported, never bypassed.
- **Using the information:** `AFTER_CLOSE` or `UNKNOWN` timing becomes tradable at the **next session's open** at the earliest.
- **Exclusion windows (avoiding the event):** `UNKNOWN` timing is treated as `BEFORE_OPEN` of `event_session`.

## 4. Provider interface (MD-A implements; MD-B consumes)
```
class HistoricalDataProvider (abstract):
    manifest() -> DatasetManifest                       # §6; available before any data is read
    instruments() -> list[Instrument]
    bars(instrument_id, start_session, end_session) -> list[DailyBar]   # ascending; gaps are absent rows
    gaps(instrument_id) -> list[Gap]
    events(instrument_id=None, start_session, end_session) -> list[CorporateEvent]
    membership(universe_id, session) -> MembershipSet
    scheduled_events(instrument_id, start_session, end_session) -> list[ScheduledEvent] | EARNINGS_DATA_UNAVAILABLE
    session_times(session) -> (open_utc, close_utc)
    as_of(decision_time_utc) -> PointInTimeView         # decision logic: every query filters known_at <= decision_time_utc
    execution_open(instrument_id, session) -> float | NO_BAR
```
- **`execution_open`** is the execution channel. It may be called only by the fill step, and only for the calendar session immediately after an order's decision session. It returns that session's `raw_open`, or `NO_BAR` (→ `NO_FILL_GAP`). Signal logic never calls it; the engine enforces this (test (d)).
- **Full-history access** (`bars`, `events`) is allowed only for ledger bookings (P3a) and for reporting after a run, never for decisions.
- **MD-A's v1 adapters (all offline):**
  - `SyntheticProvider`, for fixtures;
  - `Ms1EvidenceProvider`, over the raw archive (P6);
  - `SipDailyV1Provider`, over the `sip_daily_v1` bundle plus its archived raw responses (fixture-tested only; no such dataset exists).
- **Not in v1:** CRSP, Norgate and Alpaca-SIP fetch adapters (they come at the data-source gate).

### 4.1 Mapping the existing formats (informative, code-verified by the revision-1 review)
| Contract | `sip_daily_v1` | MS-1 evidence (IEX) |
|---|---|---|
| raw OHLCV | `raw_close`/`raw_volume` from `aux.series` (`sip_bundle.py:276-280`). Raw open/high/low are **not** in the bundle file; they come from the archived raw responses (`raw/*.json.gz`, sha-verified, `:201-211`). If those are absent, the dataset is UNAVAILABLE and integrity-only. | raw bars (`ADJUSTMENT = "raw"`, `ms1_driver/pins.py:26-34`) |
| `split_factor` | `aux.series.split_ratio` (= raw ÷ split-adjusted close, rounded to 6 decimals; only on dates that have both) | UNAVAILABLE, so no signals unless the run is flagged `SPLIT_UNADJUSTED` |
| `total_return_factor` | raw_close ÷ all-adjusted close (includes splits) on dates present in both series; the all-adjusted rows are filtered (`:271`) and the raw rows are not | UNAVAILABLE |
| `volume_feed` / `price_source` | `CONSOLIDATED` / `CONSOLIDATED` | `SINGLE_VENUE:IEX` / `SINGLE_VENUE:IEX` |
| `known_at` | `close_utc(session)` from §2 | `close_utc(session)` from §2, **never** the archive's `available_at` (= retrieval time) |
| `instrument_id` | `ALPACA:<symbol>:<first_session>`, `symbol_history_status = UNKNOWN` | the same |
| `membership_basis` | `FIXED_CURRENT_UNIVERSE` | `FIXED_CURRENT_UNIVERSE` |
| events | none recorded (`events_completeness = NONE`). A split inferred from a `split_ratio` change is a WARN only, never booked. | none (`NONE`) |
| earnings | `EARNINGS_DATA_UNAVAILABLE` | `EARNINGS_DATA_UNAVAILABLE` |

The existing `strategy_validation/dataset.py` keys by symbol, raises on a bad bar, and cuts point-in-time by date string (`:45`, `:67-75`). **It is not contract-conformant**, so MD-B does not reuse it unchanged.

## 5. Validation (MD-A implements; any BLOCK makes the dataset unusable)
- **BLOCK:**
  - a key duplicate;
  - a non-calendar session, or one outside the calendar's coverage;
  - `known_at ≠ close_utc(session)` for a bar;
  - a non-finite factor, or one ≤ 0;
  - with `events_completeness = COMPLETE`, a factor change of more than **1e-5 relative** between consecutive sessions without a matching event;
  - a ticker-reuse collision (two instruments sharing a symbol at one session);
  - a declared split series that is missing;
  - a manifest whose content hash mismatches;
  - a calendar cross-check disagreement (§2).
- **WARN:**
  - a raw close-to-close move over 25 % without a matching event (`DAY_JUMP_UNCLASSIFIED`);
  - a gap of 2 or more sessions;
  - **a symbol gap over 20 sessions (`POSSIBLE_TICKER_REUSE`)**;
  - a spin-off indicated but not adjusted;
  - `volume_feed` or `price_source` not `CONSOLIDATED`;
  - an inferred split.
- Each dataset gets `CLEAN` | `WARN` | `BLOCK` with every reason, and a machine-readable acceptance record (so MD-7 is mechanical).

## 6. Dataset manifest
### 6.1 Fields
- `dataset_id`; `contract_version = "historical_data_contract_v1"`; `contract_sha256`; `kind = HISTORICAL | SYNTHETIC`.
- `provider`, `adapter`, `adapter_version`; `source_files` (each with its sha256).
- `start_session`, `end_session`, `instrument_count`, `content_sha256` (§6.3).
- `price_views_available`, `volume_feed`, `price_source`, `membership_basis`, `events_completeness = COMPLETE | PARTIAL | NONE`, `adjustment_as_of`, `retrieved_at`.
- `licence` (§6.2).
- `data_exposure`: the prior studies that saw which ranges, or UNKNOWN. MD-B's holdout ledger consumes it.

### 6.2 Licence block (owner-approved rule)
- **Fields:** `licence_ref`, `personal_research_use = CONFIRMED | UNKNOWN | NOT_PERMITTED`, `redistribution = NONE`.
- **Adapters always emit `UNKNOWN`.** `CONFIRMED` comes only from an **owner-authored licence file** referenced by its sha256 (`licence_file_sha256`, `confirmed_by`, `confirmed_on`).
- **Under `UNKNOWN`:** only schema, integrity and validation checks are allowed. **No returns, signals, ledger runs or performance statistics are computed.**
- **`NOT_PERMITTED`:** no use except deletion.
- **`kind = SYNTHETIC` is exempt.**
- **The MS-1 IEX evidence is `UNKNOWN`** until you verify it.

### 6.3 Canonical form (for `content_sha256`)
- UTF-8 JSON, `sort_keys=True`, separators `(",", ":")`.
- Instruments are sorted by `instrument_id`; bars and gaps by (`instrument_id`, `session`); events by (`effective_session`, `event_id`).
- Floats as Python `repr`; NaN or Infinity is never valid.
- Missing values as `{"status": …, "reason": …}`.
- Timestamps as ISO-8601 UTC ending in `Z`; sessions as `YYYY-MM-DD`.
- **The canonical document is exactly** `{"manifest": <manifest minus content_sha256>, "instruments": [...], "bars": [...], "gaps": [...], "events": [...], "membership": [...], "scheduled_events": [...] | {"status": "UNAVAILABLE", "reason": "EARNINGS_DATA_UNAVAILABLE"}}`. The hash covers this document.

## 7. Consumer obligations (MD-B), with tests required
- **Decision logic uses only `as_of(t)`; fills use only `execution_open`.**
- **Look-ahead tests:**
  - (a) **future perturbation:** changing any value with `known_at > t` (other than the declared snapshot factors) changes no decision at or before *t*;
  - (b) **truncation:** the decisions up to *t* are identical on data cut at *t*;
  - (c) **event timing:** corporate actions, membership changes and scheduled events are invisible to decisions before their `known_at`, or before the `effective_session` open when `known_at` is UNKNOWN;
  - (d) **fill timing:** no fill on the signal bar; fills only at the next calendar session's open; a missing bar gives `NO_FILL_GAP`; signal code cannot reach `execution_open`;
  - (i) **snapshot invariance:** multiplying an instrument's `split_factor` (or `total_return_factor`) by any positive constant changes no decision.
- **Ledger tests (P3a):**
  - (e) **split and stock dividend:** shares × ratio, total cost basis preserved, cash unchanged; cash in lieu or `FRACTION_UNPAID`; the split is applied before that session's fills;
  - (f) **cash dividend:** credited once; entitled per the ex-date rule (buy at the ex-date open: not entitled; sell at the ex-date open: entitled); not also present in the price return; `PAY_DATE_UNKNOWN` flagged;
  - (g) **spin-off, merger, delisting:** booked per their events; UNKNOWN `delisting_return` is −100 % in primary results, with last price as a labelled sensitivity; `CORPORATE_ACTION_UNACCOUNTED` when `events_completeness ≠ COMPLETE`;
  - (h) **reconciliation:** run on a **pinned exact fixture**:
    - zero costs;
    - order notional an exact multiple of `raw_open`;
    - integer split ratios (no fractions);
    - `pay_session = effective_session`;
    - ex-date close = prior close − D, with the price flat from the ex-date to the window end.

    On it, the ledger's raw-price-plus-cash-flow return equals the total-return series' return within **1e-9 relative**, and a mismatch fails. **Outside this fixture, reconciliation is reported as a difference, not checked against 1e-9.**
- **Verdict caps:** `FIXED_CURRENT_UNIVERSE` caps verdicts at NOT_ESTABLISHED, and so does a run flagged `SPLIT_UNADJUSTED`. `EARNINGS_DATA_UNAVAILABLE` is reported as the reason for zero candidates.
- **Licence check:** the licence rule (§6.2) is checked before any HISTORICAL computation of returns or signals; the refusal is a hard error.

## 8. Versioning
- `contract_version` and `contract_sha256` are recorded in every manifest and every result identity.
- An adapter declares the versions it supports.
- The engine refuses any version it does not support.
