"""
historical_data/ - MD-A: the provider-independent, point-in-time historical daily data contract (contract v2 = HISTORICAL_DATA_CONTRACT_v1.md
revision 2 final + HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md; both sha256 pinned below).

  status.py           P4 status objects (UNKNOWN / UNAVAILABLE + a reason code), NO_BAR, EARNINGS_DATA_UNAVAILABLE
  schema.py           the entities (section 3), the licence block and the dataset manifest (section 6), strict types: NaN, infinity and booleans
                      are refused as numbers
  calendar.py         the authoritative session_times(session) for 2016-01-01..2026-12-31 (section 2), built from the Stage 7.8B-1 NYSE table,
                      zoneinfo + the tzdata package, cross-checked against the Stage 7.1 data-layer calendar for 2025-2026 (calendar.py is the
                      package's only reader of either; the isolation guards name it)
  canonical.py        the canonical form and content_sha256 (section 6.3, P7)
  provider.py         the abstract HistoricalDataProvider (section 4, A1, A2), the shared table provider and the PointInTimeView (P1, A6, A11)
  expected_events.py  the coded expected-events table (reference data only, A9); the five open contradictions are UNRESOLVED
  validate.py         the section 5 checks -> a machine-readable acceptance record (CLEAN / WARN / BLOCK with every reason)
  acceptance.py       the dataset acceptance criteria as code (ACCEPTANCE_CRITERIA.md is the same rules as a document)
  adapters/           SyntheticProvider, Ms1EvidenceProvider (the MS-1 raw archive only), SipDailyV1Provider (the sip_daily_v1 bundle)

Offline and read-only (P6): no network, no database (not even read-only), no write. Licence (section 6.2, A10): adapters always emit
personal_research_use UNKNOWN; a HISTORICAL dataset under UNKNOWN serves schema, integrity and validation checks only - the decision view
(as_of) and the execution channel (execution_open) refuse it. Nothing in the live or trading path imports this package.
"""
VERSION = "historical_data_v1"
CONTRACT_VERSION = "historical_data_contract_v2"
CONTRACT_SHA256 = "7850580ba22195e92ee03f71e75d2d7eb3719176343e90f485ce47c3ac876ed8"          # v2 amendment (A12: it pins the base by its sha)
CONTRACT_BASE_SHA256 = "453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7"     # v1 revision 2 final
SUPPORTED_CONTRACT_VERSIONS = (CONTRACT_VERSION,)

KINDS = ("HISTORICAL", "SYNTHETIC")
HISTORICAL, SYNTHETIC = KINDS

BLOCK, WARN, CLEAN = "BLOCK", "WARN", "CLEAN"

# section 5 reason codes (BLOCK)
DUPLICATE_KEY = "DUPLICATE_KEY"
NON_CALENDAR_SESSION = "NON_CALENDAR_SESSION"
CALENDAR_NOT_COVERED = "CALENDAR_NOT_COVERED"
KNOWN_AT_MISMATCH = "KNOWN_AT_MISMATCH"
FACTOR_NOT_FINITE_OR_NONPOSITIVE = "FACTOR_NOT_FINITE_OR_NONPOSITIVE"
FACTOR_CHANGE_WITHOUT_EVENT = "FACTOR_CHANGE_WITHOUT_EVENT"
TICKER_REUSE_COLLISION = "TICKER_REUSE_COLLISION"
SPLIT_SERIES_MISSING = "SPLIT_SERIES_MISSING"
MANIFEST_HASH_MISMATCH = "MANIFEST_HASH_MISMATCH"
CALENDAR_CROSSCHECK_DISAGREEMENT = "CALENDAR_CROSSCHECK_DISAGREEMENT"
TZDATA_UNAVAILABLE = "TZDATA_UNAVAILABLE"
BLOCK_CODES = (DUPLICATE_KEY, NON_CALENDAR_SESSION, CALENDAR_NOT_COVERED, KNOWN_AT_MISMATCH, FACTOR_NOT_FINITE_OR_NONPOSITIVE,
               FACTOR_CHANGE_WITHOUT_EVENT, TICKER_REUSE_COLLISION, SPLIT_SERIES_MISSING, MANIFEST_HASH_MISMATCH, CALENDAR_CROSSCHECK_DISAGREEMENT,
               TZDATA_UNAVAILABLE)

# section 5 reason codes (WARN)
DAY_JUMP_UNCLASSIFIED = "DAY_JUMP_UNCLASSIFIED"
GAP_2_OR_MORE_SESSIONS = "GAP_2_OR_MORE_SESSIONS"
POSSIBLE_TICKER_REUSE = "POSSIBLE_TICKER_REUSE"
SPIN_OFF_NOT_ADJUSTED = "SPIN_OFF_NOT_ADJUSTED"
VOLUME_FEED_NOT_CONSOLIDATED = "VOLUME_FEED_NOT_CONSOLIDATED"
PRICE_SOURCE_NOT_CONSOLIDATED = "PRICE_SOURCE_NOT_CONSOLIDATED"
INFERRED_SPLIT = "INFERRED_SPLIT"
SYMBOL_DROPPED_BY_ADAPTER = "SYMBOL_DROPPED_BY_ADAPTER"   # MD-A addition (QA L-5): a source symbol that yields no instrument is never silent
WARN_CODES = (DAY_JUMP_UNCLASSIFIED, GAP_2_OR_MORE_SESSIONS, POSSIBLE_TICKER_REUSE, SPIN_OFF_NOT_ADJUSTED, VOLUME_FEED_NOT_CONSOLIDATED,
              PRICE_SOURCE_NOT_CONSOLIDATED, INFERRED_SPLIT, SYMBOL_DROPPED_BY_ADAPTER)

DAY_JUMP_THRESHOLD = 0.25          # a raw close-to-close move strictly over 25 % (the project threshold)
FACTOR_REL_TOLERANCE = 1e-5        # a factor change of more than 1e-5 relative between consecutive bars
GAP_WARN_SESSIONS = 2              # a gap of 2 or more sessions
TICKER_REUSE_GAP_SESSIONS = 20     # a symbol gap OVER 20 sessions
