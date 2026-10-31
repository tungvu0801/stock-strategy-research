"""
market_intel - Stage 7.0: the CONTRACTS of the shared, read-only Market Intelligence Data Layer
(DESIGN_70_MARKET_INTEL_CONTRACTS.md; the full plan is DESIGN_MARKET_INTELLIGENCE_SCANNER.md, sections C.1-C.8 and E.1).

Stage 7.0 is pure: no network, no thread, no file, no database, no route and no config change. Before any data exists it fixes the
vocabulary and the policies every later stage (7.1 store and backfill, 7.2 sources, 7.3 daemon and read-only API, 7.5 scanner) follows:

  models.py     the observation envelope and the fetch record (pydantic, extra=forbid). Every value carries observed / published /
                retrieved / available times; a value is usable at T only when available_at <= T; no order, quantity or side field exists
  settings.py   the MARKET_INTEL_* settings, normalised: OFF unless SHADOW; ACTIVE does not exist (reads as OFF and is reported)
  freshness.py  the pre-registered freshness and state policy per kind (FRESH / AGING / STALE, AVAILABLE / STALE / UNAVAILABLE / ...)
  validate.py   deterministic validation codes for bars, quotes, news and events: a code is recorded, a value is never repaired
  calendar.py   New York sessions PRE / REG / POST / CLOSED: the Stage 6.10A classifier and its static 2026-2027 table, read only
  budget.py     per-provider request budgets: token buckets on an injected clock, daily caps, back-off and plan pressure

Display-only and read-only: nothing in the live path imports this package, and it imports no model provider, broker, executor,
proposal, safety or setup service (tests/test_market_intel_70.py).

Stage 7.1 (DESIGN_71_MARKET_INTEL_STORE.md) adds, still with nothing in the live path importing them:

  calendar_history.py  the verified 2025 NYSE history (holidays, the 2025-01-09 closure, early closes); 2024 / 2028 stay NOT_COVERED
  migrations.py        the versioned schema of the store's own SQLite file (data/market_intel.db), append-only triggers on every table
  store.py             the point-in-time store: lazy creation, idempotent ingestion, revisions, feed separation, PIT queries, quality reports
  archive.py           the raw-response archive (gzip + SHA-256 manifest under runtime/market_intel/raw)
  sources/alpaca_bars.py  Alpaca bars -> envelopes, parse-only (no network code)
  ingest.py            the offline backfill planner and CLI; the gated runner a future approved backfill will use
"""
VERSION = "market_intel_v0"
STAGE = "7.0"

# modes (the Stage 6.10C pattern): ACTIVE is deliberately absent
OFF, SHADOW = "OFF", "SHADOW"
MODES = (OFF, SHADOW)

# envelope status: the PPE / Demand vocabulary. A missing value is never zero, never "low risk" and never "no news".
AVAILABLE, STALE, UNAVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE, SIMULATED_DATA = (
    "AVAILABLE", "STALE", "UNAVAILABLE", "INSUFFICIENT_DATA", "NOT_APPLICABLE", "SIMULATED_DATA")
STATUSES = (AVAILABLE, STALE, UNAVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE, SIMULATED_DATA)

# freshness classes (STALE is the same word as the status)
FRESH, AGING = "FRESH", "AGING"
FRESHNESS = (FRESH, AGING, STALE)

# observation kinds (design C.4)
KINDS = ("BAR_1MIN", "BAR_1HOUR", "BAR_1DAY", "QUOTE_SNAPSHOT", "NEWS", "FILING", "EARNINGS", "HALT", "MACRO", "FACT", "UNIVERSE", "SCAN")
SYMBOL_KINDS = ("BAR_1MIN", "BAR_1HOUR", "BAR_1DAY", "QUOTE_SNAPSHOT", "FILING", "EARNINGS", "HALT", "FACT")   # need a symbol
SERIES_KINDS = ("MACRO",)                                                                                   # need a series id

# New York sessions
PRE, REG, POST, CLOSED = "PRE", "REG", "POST", "CLOSED"
SESSIONS = (PRE, REG, POST, CLOSED)

PROVIDERS = ("ALPACA", "SEC", "FRED", "NASDAQ_TRADER", "ALPHA_VANTAGE", "INTERNAL")
FEEDS = ("iex", "sip", "none")
ADJUSTMENTS = ("raw", "split", "dividend", "all", "none")

NOTE = ("Stage 7.0 contracts only: no data is fetched, stored or shown, and nothing in the trading path reads this package. "
        "Every later market-intelligence stage is read-only and OFF by default.")
