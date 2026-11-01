# Stage 7.2 — market intelligence event and reference adapters

Stage 7.2 is offline. It adds parse-only provider adapters that normalise external event and reference data into the Stage 7.0
observation envelope, so it can be stored, unchanged, in the Stage 7.1 store.

What it does not do:
- **No transport ships.** Nothing in this stage can fetch, and the CLI's `fetch` command is always refused.
- **No new key, no config, no route and no thread.**
- **Nothing in the live path imports it.** The earnings provider is implemented but **not wired**.

The plan is the 7.2 row of `DESIGN_MARKET_INTELLIGENCE_SCANNER.md` E.1 (outside the repo): "pure parsers + CLI; network via
`evidence.net.fetch` only, when run (approval for new keys / terms); edits to existing files: none".

## 1. Scope and boundaries

| In this stage (offline, tested) | Not in this stage |
|---|---|
| seven adapters: request builders and parsers | any transport, so any real request (each needs the approvals of section 6) |
| deterministic news attribution, dedup and entities | persisting news clusters and mentions: schema v1 has no tables for them; a v2 migration later |
| `StoreEarningsProvider` (reads the store) | wiring it into `get_earnings_provider()`, which changes Stage 2.6, 5.4 and DDR context (design E.2 #7) |
| an offline CLI: `describe`, `request`, `parse`; `fetch` refused | a collector or daemon (7.3), feed health and latency (7.4) |
| ingestion through the **unchanged** 7.1 store (FIXTURE / IMPORT datasets) | an approval path for non-bar downloads: the 7.1 BACKFILL authority is built on bar plans |

**Reused, never duplicated:**
- **Stage 7.0 contracts:** the envelope and the validation codes `quote_codes`, `news_codes`, `earnings_codes`, `halt_codes`, `time_codes`.
- **Stage 7.1 store:** revisions, absences, point-in-time reads and integrity.
- **Calendar:** `market_intel.calendar` sessions.
- **Evidence layer:** `evidence.rules.tier_for` for tiers; `evidence.net.host_matches`; and the URL builders in
  `evidence.sources.sec_edgar`.
- **Fundamentals:** `value_quality.ALL_CONCEPTS` and the `value_quality.pit` visibility rule.
- **Earnings:** the `data.events.earnings` interface and the `data.events.models` types.

## 2. The shared contract (`sources/common.py`)

- **Parse-only, and never raises.** Each parser returns `(observations, next page token, problems)`. Any exception becomes the page
  problem `PARSER_ERROR:<type>` (`test_X01`).
- **Page problems** make the whole page unusable, and a future transport records the fetch as `INVALID_RESPONSE`:
  `INVALID_JSON`, `INVALID_XML`, `INVALID_CSV`, `UNSAFE_XML`, `UNEXPECTED_SHAPE`, `ERROR_ENVELOPE`, `SYMBOL_MISMATCH`,
  `BAD_PAGE_TOKEN`, `PAGE_TOO_LARGE`, `PARSER_ERROR`.
- **Row problems:** `MALFORMED_ROW`, `BAD_TIME`, `OUT_OF_RANGE`, `OUT_OF_SCOPE`, `REFUSED_BY_ENVELOPE`. They are reported; the row is
  never stored as a zero.
- **Explicit time.** A provider time must carry an offset or a Z; a naive time is `BAD_TIME`. New York wall-clock times (halts, dates)
  are converted with zoneinfo. A date-only fact sits at New York midnight and says `time_precision: DATE_ONLY`.
- **Availability is ours.** `available_at = retrieved_at`, the first time we saw the row, never a provider time (`test_X02`). The store
  enforces it again.
- **Requests carry no credential.** A request names the credential its transport must add (for example `ALPHAVANTAGE_API_KEY`) but
  never contains it. A credential-looking parameter is refused outright (`test_X03`). Without the credential the request is
  `NOT_CONFIGURED`.

## 3. Adapters

| Adapter | Kind | Event time (`observed_at`) | Natural key adds | Absence | Notes |
|---|---|---|---|---|---|
| 3.1 `alpaca_news` (`/v1beta1/news`) | NEWS | article `created_at` (also `published_at`) | article id | — | windowed paging, `sort=asc`, ≤ 50 per page; an edit is a **revision**; `content` is never stored; tier and deny-host via attribution |
| 3.2 `alpaca_snapshots` (`/v2/stocks/snapshots`) | QUOTE_SNAPSHOT | the latest quote's time; session derived | symbol + **feed** | a requested symbol omitted or null, as UNAVAILABLE at our retrieval time | iex and sip are different facts; embedded bars stay in the payload; quote codes |
| 3.3 `sec_filings` (submissions) | FILING | acceptance time (see section 6) | accession number | — | metadata only (documents stay with the evidence adapter); 8-K item 2.02 sets `earnings_release`; tier 1 |
| 3.4 `sec_facts` (companyfacts) | FACT | period end (DATE_ONLY) | concept / unit / accession / period | — | declared concepts only; a restatement is its own fact; `visible_from` uses `value_quality.pit` (filed + 1 session), else `CALENDAR_NOT_COVERED` |
| 3.5 `fred` (series observations, ALFRED) | MACRO | observation date (DATE_ONLY) | `vintage:<realtime_start>` | FRED's "." | every vintage kept; the offset is the page token; the API key is never recorded |
| 3.6 `halts` (Nasdaq Trader RSS) | HALT | halt time (New York) | symbol + halt date and time | — | XML with no DTD or entity (`UNSAFE_XML`); a resumption is a **revision**; HALTED_OPEN while open; reason codes kept verbatim |
| 3.7 `earnings_av` (Alpha Vantage CSV) | EARNINGS | the **snapshot day** (DATE_ONLY) | `expected:<fiscal period end>` | — | NOT_CONFIGURED without a key; each daily snapshot kept, so a moved date is history; UNCONFIRMED_PAST |

**Why earnings use the snapshot day.** The expected report date is in the future, so it can never be an `observed_at`: the 7.1 store
refuses facts observed after their retrieval.

**Strict response shapes, verified on the first approved run:**
- **Alpaca news:** exactly `news` and `next_page_token`.
- **Snapshots:** a map with requested symbols only.
- **SEC:** the requested CIK, and equal-length `filings.recent` columns.
- **FRED:** integer `count` / `offset` / `limit`; `error_code` / `error_message` is an error envelope.
  - **Consistent paging metadata, or `UNEXPECTED_SHAPE` (detail `pagination`) — never a legitimate empty page:**
    - `count >= 0`, `offset >= 0`, and `1 <= limit <= 100000`;
    - `offset <= count` and `offset + rows <= count`;
    - a page short of `limit` must end the series;
    - an empty page is valid only at `offset == count`;
    - the response offset must equal the offset the caller asked for (`expected_offset`).
- **Alpha Vantage estimates:** an estimate must be finite or empty. NaN, ±Infinity and overflows (`1e999`) are `MALFORMED_ROW`.
- **Halts:** RSS `channel` / `item` with `ndaq:` fields.
- **Alpha Vantage:** the exact CSV header. Its HTTP 200 JSON notices (`Information`, `Note`, `Error Message`) are `ERROR_ENVELOPE`.

## 4. News (`market_intel/news/`)

- **Attribution.** Each article keeps its provider, source, author, canonical URL, host, `published_at`, `first_seen_at` and tier.
  - The tier is `evidence.rules.tier_for(..., source_type="ALPACA_NEWS")`, never a second table: 3 on the press allowlist, 4 otherwise,
    5 for a social host.
  - A deny-listed host is metadata only (`deny_host`, `DENY_HOST_LINK`).
  - Only the headline and a ≤ 300-character snippet are kept.
- **Dedup.** Rules, in order: `PROVIDER_ID` (revisions), `CANONICAL_URL`, `HEADLINE_HASH` (normalised headline + source + New York
  date), and `NEAR_DUPLICATE`.
  - **Near-duplicates:** word-set Jaccard ≥ **0.8**, within **36 h**, sharing a symbol. These thresholds are pre-registered and pinned.
  - **Representative:** the item first seen, then the lower tier, then the key. Cluster ids are deterministic and independent of input
    order.
  - **Fixtures** cover: the same story from different outlets; revisions; near-duplicates; and different stories written from the same
    template ("Apple shares rise 2%…" against "Tesla shares rise 2%…").
- **Entities.** Methods: `EXCHANGE_TAG` 0.95, `PROVIDER_TAG` 0.9, `CASHTAG` 0.8, `NAME_MATCH` 0.7, `UNVERIFIED` 0.3.
  - A bare word is never a ticker.
  - A common-word ticker (ON, ALL, IT, …), or a tag unknown to both the SEC map and the universe, stays `UNVERIFIED` on a provider tag
    alone.
  - Roles are `PRIMARY` (named in the headline), `MENTION`, and `ROUNDUP` (more than 5 tags).
  - Catalyst eligibility: `PRIMARY`, confidence ≥ 0.8, tier ≤ 3.
- **Storage.** Clusters and mentions depend on other articles and on external maps. They are computed by these pure functions, and are
  not part of an article's stored content.
- **Tier and revisions.** The tier *is* part of the envelope, so a change of the press allowlist re-attributes an article as a new
  revision.

## 5. The CLI (`python -m market_intel.sources.cli`)

| Command | What it does |
|---|---|
| `describe` | lists each adapter with its kind, host, credential name and the approvals a real request needs |
| `request` | prints what a transport would send, with no key, and `NOT_CONFIGURED` when the credential is missing; Alpha Vantage is never READY here, because config declares no key |
| `parse` | normalises a saved response file, read-only |
| `fetch` | always refused (exit 3) |

No command opens a socket, hits the evidence tripwire or writes a file (`test_C01`, `test_X04`). Exit codes: 0 done, 1 usage, 2 an
unusable body, 3 fetch refused.

## 6. Unresolved provider facts, contract notes and approvals

**Provider facts to verify on the first approved run.** The parsers fail closed on any deviation, so a wrong assumption surfaces as a
page problem, never as data.
- **SEC `acceptanceDateTime`.** It carries a "Z" but is read as **New York wall-clock time**. The EDGAR filing index shows the same
  digits as Eastern time, and the later reading is the conservative one for availability. Each filing records `acceptance_raw`
  (verbatim), `acceptance_tz` and a filing-date consistency check. **Still unresolved.** The assumption is explicit and tested
  (`test_H04`):
  - **It decides the session.** A raw `16:30:12Z` is POST, at 16:30 New York; read literally as UTC it would be REG, at 12:30 New York.
  - **It decides the chronology.** An acceptance that, read as New York time, falls after our retrieval is `FUTURE_OBSERVED`, and the
    store refuses it (`OBSERVED_AFTER_RETRIEVAL`). It is never silently re-read as UTC.
- **Alpaca news:** the page limit (50), history depth, and whether `next_page_token` is stable.
- **Alpaca snapshots:** the response shape (a top-level symbol map) and the symbol cap per request; 100 is a self-imposed cap.
- **SEC companyfacts:** size per CIK; `MAX_FACTS` = 50 000 rows selected.
- **FRED:** the rate limit (120 a minute, third-party sources only) and the licensing of each series, such as VIXCLS.
- **Alpha Vantage:** the terms of service, the exact CSV columns (`timeOfTheDay`), and its error and limit bodies.
- **Nasdaq halt RSS:** the namespace and field names, the reason codes (`KNOWN_REASON_CODES` is unverified and only sets
  `reason_code_known`), and the terms.

**Contract notes.** Each of these is a decision for a later stage, not a conflict with what this stage stores:
1. **Clock skew in live polling.** The 7.1 store refuses a row observed more than 2 s after our retrieval. Live news, quotes or halts
   from a provider whose clock leads ours by more than that would be refused: fail-closed, never stored wrong. Historical data is
   unaffected. A 7.3 decision.
2. **No approval plan for non-bar downloads.** The 7.1 BACKFILL authority is built on bar plans, so non-bar historical downloads need
   their own approval plan type (7.3 or later). Here they enter as FIXTURE or IMPORT data only.
3. **Freshness grading.** The 7.1 store grades only bars against the 7.0 freshness policy. Quote snapshots (day or swing policy), news,
   halt and SEC poll health are feed-health questions for 7.3 and 7.4; `latest_status` reports them `FRESHNESS_NOT_ASSESSED`.
4. **No storage for clusters and mentions.** Schema v1 has no news cluster or mention tables (section 4).

**Approvals before any real request:**
- the transport stage itself;
- `ALPHAVANTAGE_API_KEY`, a new key, and its terms;
- confirmation of `FRED_API_KEY` and `SEC_USER_AGENT`;
- acceptance of the Nasdaq RSS terms and of FRED's per-series licensing;
- for SIP snapshots, `SIP_FEED`.

## 7. Bookkeeping

- `BASE_72` is main `05358cb` (PR #42 merged). `NEW_72` covers the adapters, the news package, the CLI, the tests and this note.
  `SEMANTIC_72` and `INSTRUMENTATION_72` are empty.
- `EDITED_72` is bookkeeping only:
  - the CI manifest, with the new test file in the research group;
  - the earlier stages' F01 unions, widened by `NEW_72 | EDITED_72`;
  - the Stage 7.0 F01 `later` set;
  - the Stage 7.1 closed module registry, which accepts the modules `NEW_72` declares (7.2's own S01 scans them with its own import
    list);
  - the Stage 7.1 F tests, which now read what 7.1 left at `BASE_72`, the way 7.1 converted 7.0's;
  - `tests/freeze_ledger.py`.
- No application file outside `market_intel/sources/` and `market_intel/news/` changes.
