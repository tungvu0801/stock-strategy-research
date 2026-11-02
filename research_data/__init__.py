"""
research_data/ — Stage 7.8B-1: an isolated SIP daily-bar research dataset (built OFFLINE from archived raw responses), a static NYSE session
calendar for 2016-2026, and the read-only pre-run data-quality report (DESIGN_78B1_SIP_EVALUATION.md).

The Stage 6.4 IEX builder (value_quality/ingest.py), the global feed setting (config.DATA_FEED) and every live path are untouched. Nothing here
runs on its own: fetching needs an operator approval reference AND the sha256 of a pre-generated request plan; a report on a HISTORICAL dataset
needs an approval reference too. No broker or trading endpoint exists here: market data and SEC public files only. Nothing writes a database.
"""
from value_quality import BENCHMARK, RESEARCH_UNIVERSE  # noqa: F401  - the same 81 stocks + SPY as Stages 6.4-7.8

BUILDER_VERSION = "sip_daily_v1"
FEED = "SIP"                                     # the dataset's declared volume feed (provenance "volume_feed")
API_FEED = "sip"                                 # Alpaca's feed parameter: all US exchanges (verified readable on the account's Basic plan, R1-R4)
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"          # the only market-data endpoint; GET only
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"   # ticker -> CIK (today's mapping: not point in time)
ALLOWED_URLS = (BARS_URL, SEC_TICKERS_URL)
DEFAULT_START = "2016-01-04"                     # the first session of 2016 ("since 2016" historical coverage, verified R1)
ADJUSTMENTS = ("all", "raw", "split")            # all: the simulated prices; raw: price and volume rules; split: raw / split ratio
CHUNK = 20                                       # symbols per request
PAGE_LIMIT = 10_000                              # bars per page (the endpoint's maximum, summed over symbols)
MAX_PAGES_PER_REQUEST = 60                       # a guard: more pages than this stops the fetch (a materially broader request)
MIN_INTERVAL_S = 0.35                            # < 200 requests per minute (the Basic plan's limit)
SEC_MIN_INTERVAL_S = 0.15                        # < 10 requests per second (SEC fair-access policy)
DAILY_BAR_STAMPS = ("T04:00:00Z", "T05:00:00Z")  # a daily bar is stamped midnight New York (EDT / EST); anything else refuses the build
APPROVAL_PREFIX = "OPERATOR-APPROVAL:"           # a real fetch or a report on HISTORICAL data needs "OPERATOR-APPROVAL:<reference>"


def approved(reference) -> bool:
    return isinstance(reference, str) and reference.startswith(APPROVAL_PREFIX) and len(reference) > len(APPROVAL_PREFIX) + 3
