# DESIGN 5.4 — Multi-Source Evidence Research

**Status:** design only. Implementation starts after the base branch is confirmed (see §12).
**Base:** `stage-52` @ `97a191a` (latest completed research stage; no Stage 5.3 exists locally or on origin as of 2026-10-15).
**Kind:** research-evidence layer. It changes nothing in the Stage 4.7 ranking / proposal engine, nothing in the Stage 4.8–5.2
research stack, nothing in the paper or broker modules, and nothing in `benchmark/` (that work lives on `benchmark-efficiency`).

## 0. Question the stage answers

For one ticker that has declined, from sourced evidence only:

1. Did it fall because of the broad market / sector or because of company-specific news? (cause attribution)
2. Is industry evidence improving? Are suppliers / customers confirming demand? Is the company itself confirming improvement? Is price
   action confirming? (recovery ladder, six rungs)
3. What evidence contradicts the thesis, and what important evidence is still missing? (contradictions, gaps, next events)
4. How strong is the evidence: WEAK / MODERATE / STRONG — an evidence-strength grade, never a price probability.

It never says a stock will recover. Every statement in a packet is a stored, hashed evidence item or a rule output over such items.

## 1. Vocabulary

| Term | Meaning |
|---|---|
| Evidence item | One sourced statement: a verbatim snippet (≤ 2,000 chars) or a computed number, with source, timestamps, category, epistemic label, direction. |
| Evidence packet | The immutable result of one build for (ticker, as_of_date): all items, all fetch attempts, cause assessment, ladder, strength, gaps. Hashed. |
| Epistemic label | `FACT` (verbatim from a fetched, hashed source of tier ≤ 3, or computed from official market data), `INFERENCE` (rule-generated statement derived from FACT items, with `derived_from` ids), `UNCONFIRMED` (tier 5, manual text without a fetchable source, or fetched content whose hash could not be verified). The model never assigns or changes a label. |
| Direction | `UP`, `DOWN`, `NEUTRAL`, `NA`: what the item says about the quantity the rung measures, assigned by the deterministic rules in §6. |
| Source tier | 1 official (SEC filing of the ticker, the ticker's own IR domain, official market data) · 2 official material of a declared supplier / customer (their SEC filing or IR domain) · 3 established press (allowlist, reached only through Alpaca headlines, never scraped) · 4 other public web page supplied by the user · 5 social / forum (sentiment only; excluded from the ladder). |
| Rung status | `SUPPORTED`, `PARTIAL`, `NOT_SUPPORTED`, `NOT_AVAILABLE`. |
| Strength | `WEAK`, `MODERATE`, `STRONG`. |

Versions: `ENGINE_VERSION = "5.4.0"`, `RULES_VERSION = "ev_v1"` (tiers, categories, directions, ladder, strength, contradiction), `PROMPT_VERSION = "ev_explain_v1"`.

## 2. Package layout

```
evidence/
  __init__.py          versions, enums (categories, labels, directions, statuses, tiers), CONVENTIONS text
  net.py               the ONLY network path: fetch(url) behind WIRE, allow/deny hosts, timeout, size cap, failure states
  sources/
    base.py            SourceAdapter protocol, FetchResult, SourceItem; every adapter returns items or an explicit failure
    sec_edgar.py       ticker→CIK, submissions index, 8-K / 10-Q / 10-K metadata, EX-99 press-release text, XBRL company facts
    company_ir.py      one configured or supplied IR URL → extracted release (title, date, text)
    alpaca_news.py     wraps data.news.get_recent_news (existing client; no new network path)
    manual_url.py      user-supplied public URLs with a declared role (supplier / customer / company / industry / press)
    internal.py        existing market data (bars via data.market_data), events (services.event_context), sector map
  extract.py           HTML → text (stdlib html.parser), date/meta extraction, snippet selection, keyword matching
  rules.py             tiers, categories, direction keywords, cause attribution, ladder, contradiction, strength, gaps (pure)
  engine.py            build_packet(): orchestrates adapters → items → rules → packet dict; hashes; never raises past FAILED
  explain.py           optional bounded Claude explanation over the stored packet (one request, grounding guard)
  store.py             append-only persistence and readers
database/research_packet_migrations.py     four new tables, no-update / no-delete triggers, user_version untouched
api/routes/evidence.py              prefix /api/evidence (no order / trade / paper / execute / broker / handoff / deploy / activate words)
frontend/evidence.js, evidence.css  Strategy Lab → Evidence (read-only view; build and explain buttons with explicit confirms)
tests/test_evidence_54.py, tests/evidence_fakes.py, tests/fixtures/evidence/*   fixtures only, sockets blocked
```

Nothing under `evidence/` imports `paper/`, `portfolio/`, `rotation/` (write side), `agents.tools`, or the broker modules. It reads
`analysis.indicators.TickerMetrics` and `data.market_data.fetch_daily_bars` for market facts, `services.event_context` for events,
`data.news` for headlines, `data.sector_map` for the sector ETF.

## 3. Source adapters (V1) — exact scope

| Adapter | Fetches | Host(s) | Produces | Tier |
|---|---|---|---|---|
| `sec_edgar` | (a) `https://www.sec.gov/files/company_tickers.json` → CIK for the ticker and for declared related tickers; (b) `https://data.sec.gov/submissions/CIK{10}.json` → recent filings (form, filing date, accession, primary doc); (c) for each 8-K in the window: the filing index and the `EX-99.1` press-release document (HTML → text), items 2.02 / 7.01 / 8.01 only; (d) `https://data.sec.gov/api/xbrl/companyfacts/CIK{10}.json` → `Revenues` / `RevenueFromContractWithCustomerExcludingAssessedTax`, `GrossProfit`, `NetIncomeLoss` quarterly facts | `www.sec.gov`, `data.sec.gov` | filing items (COMPANY_SPECIFIC or INDUSTRY_SUPPLY_CHAIN for related tickers), fundamentals items (computed numbers) | 1 (ticker), 2 (related) |
| `company_ir` | exactly the URL(s) in `config.EVIDENCE_IR_URLS[ticker]` or supplied in the request; no crawling, no link following | the URL's host, which must be on the ticker's declared IR domain list | release items (title, date, first 2,000 chars of body) | 1 |
| `alpaca_news` | `data.news.get_recent_news(symbol, limit)` (existing Alpaca news client) | Alpaca (existing path) | headline items: title, source, published_at, url; snippet = headline + summary | 3 if `source` is on the press allowlist, else 4 |
| `manual_url` | each supplied `{url, role, related_ticker?}`; GET once | the URL's host | page items with the declared role; tier by host rule (§5) | 2 / 4 / 5 |
| `internal` | daily bars for ticker, SPY, sector ETF (lookback + 60 sessions); `TickerMetrics`; `EventsBundle` (FOMC, macro, corporate, earnings availability) | Alpaca market data (existing path), FRED if configured (existing path) | computed MARKET_WIDE / MARKET_CONFIRMATION facts, decline window, next events | 1 |

Not in V1: Reuters / Bloomberg / WSJ / FT scraping, any paywalled or login page, any Robinhood call, any search engine, any crawling.

SEC usage rules implemented in `sec_edgar`: `User-Agent: {config.SEC_USER_AGENT}` (format "Name email", required; adapter refuses to
run without it), at most 1 request per 250 ms, at most 12 requests per packet build, `Accept-Encoding: gzip`.

## 4. Network boundary

`evidence/net.py` is the only module that opens a connection, through one indirection:

```python
WIRE = _real_wire            # tests replace this with a tripwire (same pattern as paper.alpaca_readonly.WIRE)

def fetch(url, *, purpose, allowed_hosts, timeout_s=config.EVIDENCE_FETCH_TIMEOUT_S, max_bytes=config.EVIDENCE_MAX_BYTES) -> FetchResult
```

`FetchResult`: `status` ∈ `OK, TIMEOUT, HTTP_ERROR, ACCESS_DENIED, HOST_NOT_ALLOWED, HOST_DENIED, TOO_LARGE, PARSE_FAILED, EMPTY,
REDIRECT_BLOCKED, DISABLED`, `http_status`, `bytes`, `content_hash` (sha256 of the raw body), `text`, `duration_ms`, `error`.

Rules: scheme must be https; host must be in `allowed_hosts` (adapter-specific) and not in `config.EVIDENCE_DENY_HOSTS`
(default: bloomberg.com, reuters.com, wsj.com, ft.com, barrons.com, seekingalpha.com, x.com, twitter.com, facebook.com, and any host
the user adds); redirects are followed only within the same host (else `REDIRECT_BLOCKED`); 401 / 402 / 403 / 429 → `ACCESS_DENIED`
with no retry; one attempt per URL per build (no retry loop); body cut at `max_bytes` → `TOO_LARGE`; `robots.txt` is not consulted
because no crawling happens — exactly the URLs named are fetched. Every attempt, successful or not, is stored as an
`evidence_source_fetches` row. A failure never falls back to model knowledge: the corresponding rung becomes `NOT_AVAILABLE` and the
failure is listed under "missing evidence".

`config.EVIDENCE_NETWORK_ENABLED` (default `True` in the app) is forced `False` by `tests/conftest.py`; `net.fetch` returns `DISABLED`
when it is off, and the tripwire fixture fails any test that reaches `WIRE` at all.

## 5. Deterministic ingestion rules (`rules.py`, `ev_v1`)

**Tier.** 1 if host ∈ SEC hosts or host matches `EVIDENCE_IR_DOMAINS[ticker]`, or the item is computed from market data · 2 if host ∈
SEC hosts for a declared related ticker, or matches `EVIDENCE_IR_DOMAINS[related]` · 3 if the Alpaca `source` ∈ press allowlist
(`Reuters, Bloomberg, The Wall Street Journal, Financial Times, Associated Press, CNBC, MarketWatch, Barron's, Benzinga`) · 5 if host ∈
social list (`reddit.com, x.com, twitter.com, stocktwits.com, youtube.com`) · 4 otherwise.

**Epistemic label.** `FACT` when the item has a `content_hash` from a successful fetch of tier ≤ 3, or is computed from bars/events ·
`UNCONFIRMED` when tier 5, when the item is manual text without URL, or when the fetch failed (the item is still stored, with the
failure) · `INFERENCE` only for rule outputs (cause attribution, ladder sentences), each with `derived_from` item ids.

**Category.** `MARKET_WIDE`: computed SPY / sector-ETF facts, FOMC and macro events · `COMPANY_SPECIFIC`: the ticker's filings, IR
releases, headlines, corporate events · `INDUSTRY_SUPPLY_CHAIN`: items whose role is supplier / customer / industry, or filings of
declared related tickers · `COMPANY_CONFIRMATION`: the subset of tier-1 COMPANY_SPECIFIC items of kind `EARNINGS` (8-K item 2.02 or
IR release whose title matches `/results|earnings|quarter/i`) or `GUIDANCE` (text matches the guidance patterns below) — stored as a
second row with category COMPANY_CONFIRMATION linked by `derived_from`, never by copying text twice · `MARKET_CONFIRMATION`:
computed price / relative-strength / volume facts for the ticker.

**Direction keywords (applied to tier ≤ 2 text; the matched phrase is stored in `rule_match`).**
UP: `raise[sd]? (its |the )?(full[- ]year |fiscal |annual )?(guidance|outlook)`, `above (the )?(prior|previous|high end)`,
`record (revenue|quarter)`, `(revenue|sales|shipments|orders|bookings|demand|margin[s]?) (grew|increased|improved|expanded|rose)`,
`better[- ]than[- ]expected`, `inventory (normaliz|normalis)`, `pricing (improv|increas)`.
DOWN: `(lower[sd]?|cut[s]?|reduce[sd]?) (its |the )?(full[- ]year |fiscal |annual )?(guidance|outlook)`, `below (the )?(prior|previous|low end)`,
`(revenue|sales|shipments|orders|bookings|demand|margin[s]?) (fell|declined|decreased|contracted|weakened)`, `weaker[- ]than[- ]expected`,
`(impairment|restructuring|layoffs|customer loss|supply (constraint|shortage|disruption))`.
NEUTRAL: `(reaffirm|maintain|reiterat)e?[sd]? (its |the )?(guidance|outlook)`, `in line with`.
Both UP and DOWN present → `NEUTRAL` with `rule_match` listing both and `contradiction = 1`. No match → `NA`.

**Computed facts (`internal`).** Decline window: peak close in the lookback (default 90 sessions) to the latest trough after it;
`stock_ret`, `spy_ret`, `sector_ret` over that window; 20-session returns for all three; `price_vs_ema20`, `price_vs_ema50`,
`rel_strength_20 = stock_20 − sector_20`, `relative_volume`, `trend`; next earnings date if available, next FOMC date.

**Cause attribution (INFERENCE, `cause_json`).** If `stock_ret ≥ −2%` → `NO_MATERIAL_DECLINE` and the ladder is still computed.
Else `explained_share = clamp(sector_ret / stock_ret, 0, 1)` when both negative, else 0. `MARKET_WIDE_DOMINANT` if share ≥ 0.60 and
no tier-1 COMPANY_SPECIFIC item with direction DOWN dated inside the window · `COMPANY_SPECIFIC_DOMINANT` if share < 0.30 or such an
item exists and share < 0.60 · `MIXED` otherwise. The numbers and the item ids are stored with the verdict.

**Ladder (`ladder_json`), each rung from items dated inside the lookback and ≤ as_of:**

| Rung | SUPPORTED | PARTIAL | NOT_SUPPORTED | NOT_AVAILABLE |
|---|---|---|---|---|
| 1 market backdrop | SPY 20-session return > 0 **and** SPY close > its 50-day EMA | exactly one of the two | neither | SPY bars missing |
| 2 industry fundamentals | sector-ETF 20-session return > 0 **and** ≥ 1 INDUSTRY_SUPPLY_CHAIN item (tier ≤ 3) with direction UP and none DOWN | one of the two, or UP and DOWN both present | ETF return ≤ 0 and only DOWN items (or no items) | no ETF bars and no items |
| 3 supplier / customer confirmation | ≥ 1 tier-2 item from a declared supplier or customer with direction UP and none DOWN | UP items only at tier 3–4, or UP and DOWN both present | only DOWN items | no related tickers declared, or no items |
| 4 company fundamentals | XBRL: latest quarter revenue > prior quarter **and** gross margin ≥ prior quarter | exactly one of the two | both worse | company facts missing (no CIK, no fetch, no quarterly rows) |
| 5 company guidance | ≥ 1 tier-1 GUIDANCE item with direction UP and none DOWN | NEUTRAL (reaffirmed) only, or UP and DOWN both present | only DOWN | no GUIDANCE item |
| 6 market / price confirmation | close > EMA20 **and** `rel_strength_20 > 0` | exactly one | neither | metrics missing |

**Strength.** Points: SUPPORTED 2, PARTIAL 1, else 0 (max 12). `STRONG` if points ≥ 9 **and** rung 4 or rung 5 is SUPPORTED ·
`MODERATE` if points ≥ 5 · `WEAK` otherwise. If rungs 1–3 contain no NOT_SUPPORTED and neither rung 4 nor rung 5 is SUPPORTED, the
packet carries the fixed sentence *"Recovery thesis is strengthening, but company-specific confirmation is still missing."* The
strength text always ends with *"This is an evidence-strength grade, not a probability that the stock will rise."*

**Contradictions.** Two items in the same category with opposite direction (UP vs DOWN), both tier ≤ 3, dated within 30 days of each
other → both get `contradiction = 1` and `contradicts_item_id`. A rung whose inputs contain a contradiction cannot be SUPPORTED.

**Gaps and next events (`missing_json`, `next_events_json`).** For each NOT_AVAILABLE rung a fixed sentence names what would fill it
(for example rung 3: "an official report from a declared supplier or customer"); each failed fetch is listed with its status; the next
earnings date (or "earnings calendar unavailable" with the packet's reason) and the next FOMC date are listed as the events that would
strengthen or weaken the thesis. These are templates, not model text.

## 6. Packet build (`engine.build_packet`)

Input (`EvidenceRequest`): `ticker`, `as_of_date` (default today), `lookback_days` (default 90), `related` (`{"suppliers": [...],
"customers": [...]}`; default from `config.EVIDENCE_RELATED`), `ir_urls` (default from config), `manual_urls` (`[{url, role,
related_ticker?}]`), `manual_texts` (`[{text, role, note}]`, always UNCONFIRMED), `sources` enabled flags.

Steps, in order, each bounded: internal facts → SEC (ticker, then related tickers) → IR → Alpaca news → manual URLs → rules →
hashes → one `INSERT` transaction. Any adapter failure is a stored fetch row, never an exception; an engine-level error (no bars at all,
invalid ticker, no network while enabled) produces a `FAILED` packet with `failure_code` ∈ `INVALID_TICKER, NO_MARKET_DATA,
NETWORK_DISABLED, INTERNAL_ERROR`. Items are sorted by `(category, evidence_date, source_url, text)` before hashing so the packet hash is
order-independent. `packet_hash = sha256(canonical JSON of request + items + fetches + cause + ladder + strength + rules_version)`.
Determinism: given the same stored fetch bodies, a rebuild produces the same hash (tested with fixtures).

Limits: ≤ 12 SEC requests, ≤ 6 IR URLs, ≤ 12 manual URLs, ≤ 10 headlines, ≤ 200 items per packet, 15 s per fetch, 120 s per build.

## 7. Persistence (`database/research_packet_migrations.py`, additive, user_version untouched)

`evidence_packets` — `packet_id` (32 hex PK), `packet_hash` (64 hex), `ticker`, `as_of_date`, `created_at`, `lookback_days`,
`engine_version`, `rules_version`, `status` (COMPLETED / FAILED), `failure_code`, `failure_detail`, `request_json` (≤ 16 KB),
`cause_json`, `ladder_json`, `strength` (WEAK / MODERATE / STRONG / NULL), `strength_points`, `strength_text`, `missing_json`,
`next_events_json`, `n_items`, `n_fetches`, `n_failed_fetches`, `sec_requests`, `runtime_s`, `universe_note`.
Unique `(packet_hash)`; no-update / no-delete triggers.

`evidence_items` — `item_id` (32 hex PK), `packet_id` (FK), `position`, `ticker`, `subject_ticker` (the company the text is about),
`source_type` (SEC_FILING / IR_RELEASE / ALPACA_NEWS / MANUAL_URL / MANUAL_TEXT / MARKET_DATA / EVENT), `source_name`, `source_url`,
`source_identifier` (accession number, article id, bar range), `published_at`, `retrieved_at`, `evidence_date`, `category` (5 values),
`kind` (FILING / EARNINGS / GUIDANCE / HEADLINE / PAGE / METRIC / EVENT / TEXT), `text` (≤ 2,000 chars), `value_json` (numbers for
computed items), `epistemic` (FACT / INFERENCE / UNCONFIRMED), `direction` (UP / DOWN / NEUTRAL / NA), `rule_match`, `ladder_rung`
(1–6 or NULL), `ladder_status` (4 values or NULL), `source_tier` (1–5), `contradiction` (0/1), `contradicts_item_id`, `derived_from_json`,
`source_hash` (64 hex or NULL for computed / manual text), `fetch_id` (FK or NULL).

`evidence_source_fetches` — `fetch_id` (32 hex PK), `packet_id`, `adapter`, `purpose`, `url`, `host`, `status` (§4 list),
`http_status`, `bytes`, `content_hash`, `started_at`, `duration_ms`, `error`.

`evidence_explanations` — `explanation_id`, `packet_id`, `packet_hash`, `model`, `prompt_version`, `input_hash`, `status`
(COMPLETED / FAILED / WITHHELD), `text`, `guard_failures_json`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, `created_at`.

Completed packets are immutable: all four tables carry no-update / no-delete triggers, and the packet row is written in the same
transaction as its items and fetches.

## 8. Bounded LLM explanation (`explain.py`) — optional, never automatic

Input: the stored packet only (items with text ≤ 400 chars each, ladder, cause, strength, gaps). One request through the existing
`agents.technical_agent.get_provider()` wrapped by `ai_explain.service.one_request` (no SDK retries), RESEARCH budget via
`agents.gating`. System prompt = the Stage 3.8 `_COMMON` rules plus: "Statuses, labels (FACT / INFERENCE / UNCONFIRMED), directions,
rung results and the strength grade were decided by rules. Report them exactly. Never add an evidence item, never upgrade an
UNCONFIRMED or INFERENCE item to a fact, never give a probability, target, or recommendation, never say the stock will recover."
Output guard (reuse `ai_explain.service.guard_failures`): every number and every ticker in the text must appear in the packet;
forbidden phrases (`will recover`, `will rise`, `buy`, `sell`, `target`, `probability`, `likely to`) → `WITHHELD`, stored with the
failures. The route requires `confirm=true` and returns the request count (1) in the dry run first. **The first real call in this
stage happens only after explicit approval in the conversation.**

## 9. API and UI

`/api/evidence/packets` POST (build; body = EvidenceRequest; returns packet id, status, counts) · GET (list, newest first) ·
`/api/evidence/packets/{id}` GET (packet, items, fetches, explanation if any) · `/api/evidence/packets/{id}/explain` POST (dry run
unless `confirm=true`) · `/api/evidence/config` GET (declared related tickers, IR URLs, deny hosts, network flag). No endpoint
changes anything outside the four tables. Route names contain none of: order, trade, backtest, paper, execute, broker, handoff,
deploy, activate.

UI: Strategy Lab → Evidence: ticker + as_of + manual URL rows → Build (confirm dialog listing the exact hosts that will be contacted)
→ packet view: cause card, six-rung ladder with status and the items behind each rung, contradictions, gaps, next events, fetch log,
strength grade with the fixed disclaimer; Explain button = dry run then confirm. No execution controls anywhere.

## 10. Tests (fixtures only; conftest blocks sockets and forces `EVIDENCE_NETWORK_ENABLED = False`)

Fixtures under `tests/fixtures/evidence/`: `company_tickers.json` (3 tickers), `submissions_SNDK.json` (8-Ks, 10-Q), `8k_ex99_up.html`,
`8k_ex99_down.html`, `8k_ex99_reaffirm.html`, `companyfacts.json` (6 quarters), `ir_release.html`, `supplier_page.html`,
`social_page.html`, `bars_{SNDK,SPY,SMH}.json` (120 sessions each), `alpaca_news.json`, `events_bundle.json`.
`tests/evidence_fakes.py`: `FakeWire` serving fixtures by URL (records every call, raises on unknown URLs), fake bars / news / events.

Focused tests (target ≈ 45): net (allowed / denied / redirect / timeout / size / disabled / tripwire), SEC adapter (CIK map, filings in
window, item filter, EX-99 text, companyfacts quarters, request cap, missing User-Agent refusal), IR adapter (configured URL only,
extraction, failure), manual URL (role → category, tier by host, social → UNCONFIRMED), Alpaca wrapper (tier by source), internal facts
(decline window, returns, EMAs), rules (every tier / label / category / direction branch, both-directions → NEUTRAL + contradiction,
every ladder rung × four statuses, strength thresholds incl. the "company confirmation required for STRONG" rule, the fixed sentences,
cause attribution thresholds), engine (packet determinism golden hash, order independence, FAILED codes, limits), store (immutability
triggers, 32/64-hex checks, migration idempotence), API (routes, confirm gating, dry-run count, forbidden words), explanation (fake
provider, guard withholds forbidden phrases, no call without confirm), isolation (no imports of paper / portfolio / broker modules, no
Robinhood names, no socket). Regression: full suite stays green; Stage 4.6–5.2 frozen-file pins unchanged.

## 11. Real validation (after tests), bounded

One packet for `SNDK`, as_of = build date, lookback 90: SEC (ticker + declared suppliers / customers you name), one official IR URL you
supply, Alpaca headlines, manual public URLs you supply. 0 Claude calls. Report: hosts contacted and request counts, fetch statuses,
item counts per category and tier, cause verdict with numbers, ladder, strength, contradictions, gaps. Explanation only after a
separate, explicit approval stating "1 request".

## 12. Branch and boundaries

- Base: `stage-52` @ `97a191a`. No Stage 5.3 branch, commit or design exists locally or on `origin` (checked 2026-10-15). If a
  Stage 5.3 is created later, this branch (`stage-54`, currently holding only this document) is rebased onto it before implementation.
- Work happens in the separate worktree `C:\Data Analyst Py\DA IDX\stock-agent-stage54`, so `benchmark-efficiency` and the running
  P01 practice files are untouched.
- Unchanged by this stage: Stage 4.7 engine and proposals, Stage 4.8–5.2 semantics, paper / broker modules, Robinhood (no call of
  any kind), `benchmark/`. No order endpoint, no position sizing, no automatic Claude call.
