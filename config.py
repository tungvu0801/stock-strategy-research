"""
config.py — All user-editable settings for the stock monitoring agent.

Nothing in this file is secret. API credentials are NEVER stored here —
they are loaded from environment variables (via a local .env file) so
they never end up in source control or terminal output.
"""
from pathlib import Path
from typing import List

from dotenv import load_dotenv
import json
import os

# Load variables from a local .env file (if present) into the environment.
load_dotenv()

PROJECT_ROOT: Path = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Alpaca API credentials — set these in a .env file next to this project
# (copy .env.example to .env and fill in your keys). Never hard-code keys.
# --------------------------------------------------------------------------
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")


def has_credentials() -> bool:
    """Whether Alpaca API credentials have been supplied via the environment."""
    return bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


# --------------------------------------------------------------------------
# Robinhood credentials (OPTIONAL) — only needed if you use
# `python main.py --sync-robinhood` to pull your current Robinhood holdings
# into watchlist.txt. See robinhood_sync.py for important caveats: this
# uses an unofficial, reverse-engineered client library, not an official
# Robinhood API. Leave these blank if you don't want to use that feature.
# --------------------------------------------------------------------------
ROBINHOOD_USERNAME: str = os.getenv("ROBINHOOD_USERNAME", "")
ROBINHOOD_PASSWORD: str = os.getenv("ROBINHOOD_PASSWORD", "")

# Where the Robinhood login session is cached on disk so you don't have to
# re-approve 2FA on every sync. Kept inside the project but git-ignored.
ROBINHOOD_SESSION_DIR: Path = PROJECT_ROOT / ".robinhood_session"


def has_robinhood_credentials() -> bool:
    """Whether Robinhood credentials have been supplied via the environment."""
    return bool(ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD)


# --------------------------------------------------------------------------
# LLM provider (OPTIONAL) — used by agents/technical_agent.py to explain a
# stock's technical picture in plain English. If unset, AI-dependent
# endpoints return a clear "unavailable" message instead of failing; every
# other feature (scanner, watchlist, dashboard) works with no LLM key at all.
# --------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# Only needed if your key is an organization-level key that isn't scoped to
# a specific workspace (Anthropic will return a 400 asking for this header
# in that case). Leave blank if your key works without it.
ANTHROPIC_WORKSPACE_ID: str = os.getenv("ANTHROPIC_WORKSPACE_ID", "")


def has_llm_credentials() -> bool:
    """Whether an LLM provider key has been supplied via the environment."""
    return bool(ANTHROPIC_API_KEY)


# --------------------------------------------------------------------------
# AI-agent trigger/cost controls. The quant engine (Alpaca + Python) always
# runs on every scan; Claude is only ever called on top of it, for a bounded
# number of already-interesting stocks — never for the whole market, and
# never more than these limits allow. All of these are read from the
# environment so they're changeable without touching code.
# --------------------------------------------------------------------------
# Only stocks whose Attention Score crosses this threshold are eligible for
# AI analysis at all — below it, get_technical_analysis() never calls Claude.
AI_ANALYSIS_ATTENTION_THRESHOLD: int = int(os.getenv("AI_ANALYSIS_ATTENTION_THRESHOLD", "75"))

# How long a cached AI analysis is considered fresh before it's eligible for
# re-analysis (see AI_REANALYZE_* below for the other staleness triggers).
AI_ANALYSIS_CACHE_MINUTES: int = int(os.getenv("AI_ANALYSIS_CACHE_MINUTES", "30"))

# Hard ceilings on Claude calls, independent of caching/threshold — a safety
# net so a bug or a chaotic market day can't run up an unbounded AI bill.
# Two independent budgets (Stage 3.9) — one never reduces the other:
#   RESEARCH     every AI feature that existed before Stage 3.8 (Research Analyze / batch, chat, portfolio and insight
#                explanations). Env AI_RESEARCH_HOURLY_LIMIT / AI_RESEARCH_DAILY_LIMIT; the older names
#                AI_MAX_CALLS_PER_HOUR / AI_MAX_CALLS_PER_DAY still work. The code reads AI_MAX_CALLS_PER_*.
#   EXPLANATION  Stage 3.8 "Explain this setup" / "Explain this evidence".
AI_MAX_CALLS_PER_HOUR: int = int(os.getenv("AI_RESEARCH_HOURLY_LIMIT", os.getenv("AI_MAX_CALLS_PER_HOUR", "10")))
AI_MAX_CALLS_PER_DAY: int = int(os.getenv("AI_RESEARCH_DAILY_LIMIT", os.getenv("AI_MAX_CALLS_PER_DAY", "30")))
AI_EXPLANATION_HOURLY_LIMIT: int = int(os.getenv("AI_EXPLANATION_HOURLY_LIMIT", "10"))
AI_EXPLANATION_DAILY_LIMIT: int = int(os.getenv("AI_EXPLANATION_DAILY_LIMIT", "30"))

# A cached analysis is still invalidated early if price or Attention Score
# have moved this much, even if the cache TTL hasn't expired yet.
AI_REANALYZE_PRICE_CHANGE_PERCENT: float = float(os.getenv("AI_REANALYZE_PRICE_CHANGE_PERCENT", "2.0"))
AI_REANALYZE_ATTENTION_CHANGE: int = int(os.getenv("AI_REANALYZE_ATTENTION_CHANGE", "10"))

# --------------------------------------------------------------------------
# Claude model pricing, USD per 1,000,000 tokens. Isolated here so a price
# change never requires touching agents/usage_tracker.py or any calling
# code — just update these numbers. Cache write/read rates follow
# Anthropic's standard multipliers (1.25x / 0.1x of the input rate) where
# not separately published. Source: Anthropic pricing, retrieved 2026-06-24.
# A model not listed here simply won't get a cost estimate (never guessed).
# --------------------------------------------------------------------------
AI_MODEL_PRICING = {
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
    "claude-opus-5":     {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-8":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-sonnet-5":   {"input": 2.00,  "output": 10.00, "cache_write": 2.50,  "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00,  "cache_write": 1.25,  "cache_read": 0.10},
}


# --------------------------------------------------------------------------
# Web dashboard (FastAPI + static frontend)
# --------------------------------------------------------------------------
FASTAPI_HOST: str = os.getenv("FASTAPI_HOST", "127.0.0.1")
FASTAPI_PORT: int = int(os.getenv("FASTAPI_PORT", "8000"))
WEB_UI_REFRESH_SECONDS: int = 30  # how often the browser redraws from cached data


# --------------------------------------------------------------------------
# Scan intervals (in seconds). Only used when main.py is run with --loop.
# --------------------------------------------------------------------------
MARKET_SCAN_INTERVAL: int = 900   # how often to refresh the whole-market overview
WATCHLIST_SCAN_INTERVAL: int = 60  # how often to refresh the watchlist detail view

# --------------------------------------------------------------------------
# Liquidity filters for the market-wide scanner. These keep illiquid /
# penny stocks out of the "notable stocks" results.
# --------------------------------------------------------------------------
MIN_PRICE: float = 5.0
MIN_AVG_VOLUME: int = 500_000

# --------------------------------------------------------------------------
# Output sizing
# --------------------------------------------------------------------------
TOP_RESULTS: int = 20

# --------------------------------------------------------------------------
# Indicator / lookback settings
# --------------------------------------------------------------------------
DAILY_BAR_LOOKBACK_DAYS: int = 120    # calendar days of daily bars to pull per symbol
RSI_PERIOD: int = 14
EMA_FAST: int = 9
EMA_MEDIUM: int = 20
EMA_SLOW: int = 50
HIGH_LOW_LOOKBACK_DAYS: int = 20      # window for the "20-day high/low" feature
VOLUME_AVG_LOOKBACK_DAYS: int = 20    # window used to compute average volume
VOLATILITY_LOOKBACK_DAYS: int = 20    # window used to compute historical volatility
VOLATILITY_SHORT_LOOKBACK_DAYS: int = 5  # shorter window, used only for the volatility-expansion ratio

ATR_PERIOD: int = 14                  # Average True Range smoothing period

SUPPORT_RESISTANCE_LOOKBACK_DAYS: int = 60  # bars considered when looking for swing highs/lows
SWING_WINDOW_DAYS: int = 3                  # a bar is a swing point if it's the extreme within +/- this many bars
APPROACHING_LEVEL_PCT: float = 2.0          # within this % of support/resistance counts as "approaching"

MOMENTUM_5D_DAYS: int = 5             # multi-day momentum windows
MOMENTUM_10D_DAYS: int = 10

VOLUME_EXPANSION_SHORT_DAYS: int = 5   # short/long average-volume ratio windows
VOLUME_EXPANSION_LONG_DAYS: int = 20
VOLATILITY_EXPANSION_RATIO_THRESHOLD: float = 1.3  # short/long volatility ratio considered "expanding"

# --------------------------------------------------------------------------
# Attention Score weights (must describe "how unusual/interesting," never a
# buy/sell weighting). Kept configurable per the original design goal.
# --------------------------------------------------------------------------
ATTENTION_WEIGHT_MOMENTUM: float = 0.35
ATTENTION_WEIGHT_RVOL: float = 0.30
ATTENTION_WEIGHT_TREND: float = 0.20
ATTENTION_WEIGHT_VOLATILITY: float = 0.15

# --------------------------------------------------------------------------
# Alpaca screener settings (whole-market scan). Alpaca's screener endpoints
# do the market-wide ranking on their side, so we only ever request a
# bounded list of "already interesting" symbols back — this keeps the
# scanner efficient and avoids pulling data for the entire market.
# --------------------------------------------------------------------------
SCREENER_TOP_N: int = 25              # symbols requested per screener call (gainers/losers/actives)

# Which Alpaca data feed to use. "iex" works on free/basic accounts.
# Accounts with a market data subscription can switch this to "sip".
DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex")

# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
WATCHLIST_FILE: Path = PROJECT_ROOT / "watchlist.txt"

# --------------------------------------------------------------------------
# Fallback universe — used ONLY if Alpaca's screener endpoints are
# unavailable (e.g. older data plan, temporary outage). This is a small,
# fixed list of liquid large/mid-cap U.S. stocks so the market overview
# still produces useful output. It is intentionally bounded in size so a
# fallback scan stays fast, cheap, and within API rate limits.
# --------------------------------------------------------------------------
FALLBACK_UNIVERSE: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA", "BRK.B",
    "LLY", "JPM", "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "JNJ",
    "NFLX", "BAC", "ABBV", "CRM", "ORCL", "MRK", "CVX", "KO", "AMD", "PEP",
    "ADBE", "WMT", "TMO", "LIN", "MCD", "CSCO", "ACN", "ABT", "DHR", "TXN",
    "GE", "PM", "IBM", "CAT", "VZ", "INTU", "QCOM", "NOW", "AMGN", "ISRG",
    "SPGI", "BKNG", "NEE", "AMAT", "PFE", "UNP", "RTX", "LOW", "HON", "SYK",
    "MU", "MRVL", "SNDK", "PANW", "LRCX", "KLAC", "ADI", "REGN", "VRTX", "PLD",
    "SCHW", "GS", "MS", "BLK", "C", "AXP", "T", "DIS", "UBER", "SBUX",
]

# ==========================================================================
# STAGE 2 — Beginner research view, evidence engine, risk/position sizing.
# Everything above this line is unchanged from Milestone 1.
# ==========================================================================

# --------------------------------------------------------------------------
# Explanation mode. The UI defaults to plain-language Beginner mode; a
# future toggle can request ADVANCED per-call. Advanced technical fields
# are always computed and returned either way — this only controls which
# framing is primary in the UI/beginner narrative.
# --------------------------------------------------------------------------
EXPLANATION_MODE: str = os.getenv("EXPLANATION_MODE", "BEGINNER")  # BEGINNER | ADVANCED

# --------------------------------------------------------------------------
# Evidence-scoring weights (analysis/evidence_scoring.py). A category with
# no data is excluded and its weight redistributed among the rest — it is
# never defaulted to 0, which would silently count "no data" as evidence.
# --------------------------------------------------------------------------
EVIDENCE_WEIGHT_TECHNICAL: float = float(os.getenv("EVIDENCE_WEIGHT_TECHNICAL", "0.35"))
EVIDENCE_WEIGHT_CATALYST: float = float(os.getenv("EVIDENCE_WEIGHT_CATALYST", "0.25"))
EVIDENCE_WEIGHT_RISK: float = float(os.getenv("EVIDENCE_WEIGHT_RISK", "0.20"))
EVIDENCE_WEIGHT_MARKET: float = float(os.getenv("EVIDENCE_WEIGHT_MARKET", "0.10"))
EVIDENCE_WEIGHT_SECTOR: float = float(os.getenv("EVIDENCE_WEIGHT_SECTOR", "0.10"))

# Research View label thresholds, applied to net = (bullish_pct - bearish_pct) / 100.
RESEARCH_VIEW_STRONG_THRESHOLD: float = float(os.getenv("RESEARCH_VIEW_STRONG_THRESHOLD", "0.50"))
RESEARCH_VIEW_LEAN_THRESHOLD: float = float(os.getenv("RESEARCH_VIEW_LEAN_THRESHOLD", "0.20"))

# --------------------------------------------------------------------------
# Market/Sector context normalization. A proxy's daily % change is divided
# by these values and clamped to [-1, 1] to become a -1..+1 evidence score
# (i.e. a move of this size or larger counts as "maximally" bullish/bearish
# for that category).
# --------------------------------------------------------------------------
MARKET_NORM_PCT: float = float(os.getenv("MARKET_NORM_PCT", "1.5"))
SECTOR_NORM_PCT: float = float(os.getenv("SECTOR_NORM_PCT", "2.0"))
MARKET_PROXY_SYMBOL: str = os.getenv("MARKET_PROXY_SYMBOL", "SPY")

# --------------------------------------------------------------------------
# Risk Agent flag thresholds (analysis/risk_flags.py) and severities used to
# turn triggered flags into the Risk evidence-category score.
# --------------------------------------------------------------------------
RISK_FLAG_SEVERITY = {"HIGH": 0.45, "MEDIUM": 0.25, "LOW": 0.10}
RISK_ATR_PCT_THRESHOLD: float = float(os.getenv("RISK_ATR_PCT_THRESHOLD", "4.0"))       # ATR as % of price
RISK_VOLATILITY_THRESHOLD: float = float(os.getenv("RISK_VOLATILITY_THRESHOLD", "60.0"))  # annualized %
RISK_LARGE_GAP_PCT: float = float(os.getenv("RISK_LARGE_GAP_PCT", "4.0"))
RISK_EXTENDED_MOMENTUM_PCT: float = float(os.getenv("RISK_EXTENDED_MOMENTUM_PCT", "10.0"))  # 5-day momentum
RISK_LOW_RVOL_THRESHOLD: float = float(os.getenv("RISK_LOW_RVOL_THRESHOLD", "1.0"))
RISK_SECTOR_MARKET_WEAKNESS_PCT: float = float(os.getenv("RISK_SECTOR_MARKET_WEAKNESS_PCT", "-1.0"))

# --------------------------------------------------------------------------
# Research Setup calculator (analysis/research_setup.py) — all arithmetic,
# no AI involved. `buffer` shrinks the invalidation level below support.
# --------------------------------------------------------------------------
RESEARCH_SETUP_INVALIDATION_BUFFER_PCT: float = float(os.getenv("RESEARCH_SETUP_INVALIDATION_BUFFER_PCT", "1.5"))
RESEARCH_SETUP_DEFAULT_HORIZON: str = os.getenv("RESEARCH_SETUP_DEFAULT_HORIZON", "2-5 days")

# --------------------------------------------------------------------------
# Deterministic Risk Manager / position sizing (analysis/position_sizing.py).
# Claude never picks a position size — this is pure Python arithmetic.
# Portfolio value has no real data source in this app yet, so it must be
# supplied explicitly per request; these are only the configurable ceilings.
# --------------------------------------------------------------------------
MAX_POSITION_PERCENT: float = float(os.getenv("MAX_POSITION_PERCENT", "10.0"))
MAX_RISK_PER_TRADE_PERCENT: float = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "1.0"))
MAX_DAILY_LOSS_PERCENT: float = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3.0"))  # informational only — not enforced (no position tracking exists)
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))               # informational only — not enforced (no position tracking exists)

# --------------------------------------------------------------------------
# AI chat (agents/chat_agent.py) — shares the same usage_tracker budget as
# the other agents; this only bounds the tool-use loop itself.
# --------------------------------------------------------------------------
CHAT_MAX_TOOL_ITERATIONS: int = int(os.getenv("CHAT_MAX_TOOL_ITERATIONS", "5"))


# ==========================================================================
# STAGE 2.5 — Research history, outcome tracking, and validation.
# This is an OBSERVATION layer only: it stores immutable snapshots of what
# the research engine produced and later measures real price outcomes
# against them. Nothing here changes any scoring formula, weight,
# threshold, or prompt above this line.
# ==========================================================================

# --------------------------------------------------------------------------
# Local SQLite database (database/database.py). Never stores API keys or
# secrets — only research snapshots and their later-measured outcomes.
# --------------------------------------------------------------------------
RESEARCH_DB_PATH: Path = PROJECT_ROOT / "data" / "stock_agent.db"

# --------------------------------------------------------------------------
# Versioning stamped onto every saved snapshot/outcome row. Bump these by
# hand whenever the corresponding logic changes — historical rows keep
# their original stamp forever; nothing here is ever rewritten in place.
# --------------------------------------------------------------------------
ENGINE_VERSION: str = "1.0.0"            # overall research-engine version (analysis/ + agents/)
EVIDENCE_VERSION: str = "1.0.0"          # analysis/evidence_scoring.py formula/weights version
PROMPT_VERSION: str = "1.0.0"            # agent system-prompt version
RESEARCH_SCHEMA_VERSION: str = "1.0.0"   # research_snapshots table shape
OUTCOME_SCHEMA_VERSION: str = "1.0.0"    # research_outcomes table shape

# --------------------------------------------------------------------------
# Outcome tracking (services/outcome_tracker.py)
# --------------------------------------------------------------------------
TRADING_DAY_HORIZONS: List[int] = [1, 3, 5]
OUTCOME_FETCH_BUFFER_DAYS: int = int(os.getenv("OUTCOME_FETCH_BUFFER_DAYS", "5"))  # extra calendar days fetched past the snapshot date

# --------------------------------------------------------------------------
# Snapshot deduplication (database/fingerprint.py)
# --------------------------------------------------------------------------
FINGERPRINT_PRICE_DECIMALS: int = int(os.getenv("FINGERPRINT_PRICE_DECIMALS", "2"))

# --------------------------------------------------------------------------
# How long a generated (but not yet saved) research bundle stays available
# to be saved verbatim via POST /api/research/snapshots (services/analysis_cache.py).
# --------------------------------------------------------------------------
ANALYSIS_CACHE_TTL_MINUTES: int = int(os.getenv("ANALYSIS_CACHE_TTL_MINUTES", "60"))

# --------------------------------------------------------------------------
# Performance analytics small-sample warnings (services/analytics.py)
# --------------------------------------------------------------------------
ANALYTICS_MIN_SAMPLE_SIZE_LOW: int = int(os.getenv("ANALYTICS_MIN_SAMPLE_SIZE_LOW", "10"))       # below this: "very small sample"
ANALYTICS_MIN_SAMPLE_SIZE_CAUTION: int = int(os.getenv("ANALYTICS_MIN_SAMPLE_SIZE_CAUTION", "30"))  # below this: "small sample"

# --------------------------------------------------------------------------
# Historical-validation badge (api/routes/research_history.py, presentation
# only). A snapshot whose market_timestamp predates its created_at by at
# least this many calendar days is flagged HISTORICAL VALIDATION in the UI --
# large enough to never fire on a normal weekend/holiday gap between the
# last trading session and an ordinary same-week save.
# --------------------------------------------------------------------------
HISTORICAL_VALIDATION_GAP_DAYS: int = int(os.getenv("HISTORICAL_VALIDATION_GAP_DAYS", "5"))

# ==========================================================================
# STAGE 2.6 — Real event intelligence (earnings/macro calendar/corporate
# actions). Observation layer only: event risk feeds the EXISTING Risk
# evidence category through new deterministic risk-flag codes (see
# analysis/risk_flags.py) -- no new evidence category, no weight/threshold
# change above this line. Every date/time here comes from a real, named
# source (see data/events/*.py module docstrings) or is honestly reported
# unavailable -- nothing is ever fabricated or inferred.
# ==========================================================================
EVENT_SCHEMA_VERSION: str = "1.0.0"

# How far back/ahead of "now" the event layer looks when assembling a
# symbol's event context.
EVENT_LOOKAHEAD_DAYS: int = int(os.getenv("EVENT_LOOKAHEAD_DAYS", "14"))
EVENT_LOOKBACK_DAYS: int = int(os.getenv("EVENT_LOOKBACK_DAYS", "7"))

# Deterministic event-risk severity thresholds (analysis/event_risk.py).
# Claude never assigns severity -- these are the sole authority.
EVENT_HIGH_RISK_HOURS: float = float(os.getenv("EVENT_HIGH_RISK_HOURS", "24"))
EVENT_MEDIUM_RISK_DAYS: float = float(os.getenv("EVENT_MEDIUM_RISK_DAYS", "3"))
EVENT_LOW_RISK_DAYS: float = float(os.getenv("EVENT_LOW_RISK_DAYS", "7"))

# Event-provider cache TTLs (data/events/cache.py) -- these calendars change
# slowly, so a request never needs to re-fetch them from scratch.
MACRO_CACHE_TTL_MINUTES: int = int(os.getenv("MACRO_CACHE_TTL_MINUTES", "720"))            # 12h
CORPORATE_ACTION_CACHE_TTL_MINUTES: int = int(os.getenv("CORPORATE_ACTION_CACHE_TTL_MINUTES", "180"))  # 3h
FOMC_CACHE_TTL_MINUTES: int = int(os.getenv("FOMC_CACHE_TTL_MINUTES", "720"))               # 12h
EARNINGS_CACHE_TTL_MINUTES: int = int(os.getenv("EARNINGS_CACHE_TTL_MINUTES", "60"))        # unused while unavailable

# FRED (St. Louis Fed) release-dates API — OPTIONAL. Without a key, the
# macro provider (data/events/macro.py) honestly reports UNAVAILABLE rather
# than guessing. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")


def has_fred_credentials() -> bool:
    """Whether a FRED API key has been supplied via the environment."""
    return bool(FRED_API_KEY)


# FRED release_id per macro series -- confirmed against FRED's own
# /fred/releases catalog, not guessed. JOLTS has no stable, cleanly
# resolvable release_id in this catalog as of this writing, so it is
# intentionally omitted (data/events/macro.py reports it UNAVAILABLE
# without blocking the other series).
FRED_RELEASE_IDS = {
    "CPI": 10,                     # Consumer Price Index
    "PPI": 46,                     # Producer Price Index
    "EMPLOYMENT_SITUATION": 50,    # Employment Situation (the "jobs report")
    "PCE": 21,                     # Personal Income and Outlays (includes the PCE price index)
}


# --------------------------------------------------------------------------
# Stage 2.7C — read-only portfolio awareness (OPTIONAL, OFF by default).
# The stock-agent never talks to Robinhood: it reads normalized JSON from the
# separate local rh_gateway process (../rh_gateway), which alone holds the
# Robinhood OAuth credential and full account numbers. When disabled, or when
# the gateway is unreachable, every Stage 1–2.6 feature behaves exactly as
# before and portfolio endpoints report PORTFOLIO_UNAVAILABLE.
# Portfolio data never changes evidence scores, Research View, snapshots,
# outcomes or event scoring, and is never written to the database.
# --------------------------------------------------------------------------
PORTFOLIO_AWARENESS_ENABLED: bool = os.getenv("PORTFOLIO_AWARENESS_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on")
RH_GATEWAY_URL: str = os.getenv("RH_GATEWAY_URL", "http://127.0.0.1:8787")  # must be loopback
RH_GATEWAY_TIMEOUT_SECONDS: float = float(os.getenv("RH_GATEWAY_TIMEOUT_SECONDS", "30"))
PORTFOLIO_ACCOUNT_ALIAS: str = os.getenv("PORTFOLIO_ACCOUNT_ALIAS", "holdings")  # alias only, never a number

# A quote older than this is labelled STALE (still shown, never silently treated as live).
PORTFOLIO_QUOTE_STALE_SECONDS: int = int(os.getenv("PORTFOLIO_QUOTE_STALE_SECONDS", "900"))
# Display rule only: above this |gap| the UI shows Robinhood-reported equity value and the
# calculated position value side by side with an explicit warning.
PORTFOLIO_VALUATION_GAP_MATERIAL_PCT: float = float(os.getenv("PORTFOLIO_VALUATION_GAP_MATERIAL_PCT", "0.5"))
PORTFOLIO_REALIZED_PNL_SPAN: str = os.getenv("PORTFOLIO_REALIZED_PNL_SPAN", "3month")

# Portfolio risk RULES are configuration-only and intentionally EMPTY until thresholds are reviewed.
# Format (JSON list): [{"id": "...", "scope": "position|sector|portfolio", "fact": "...",
#                       "op": ">=|>|<=|<|==", "value": 0.0, "label": "..."}]
PORTFOLIO_RISK_RULES_JSON: str = os.getenv("PORTFOLIO_RISK_RULES_JSON", "[]")


# --------------------------------------------------------------------------
# Stage 2.7D — portfolio ATTENTION policy (deterministic; not trading advice).
# Bands are "INFO,MEDIUM,HIGH" thresholds in PERCENT of the calculated
# portfolio value (leave a band empty, e.g. "10,,30", to disable it).
# A triggered rule means "this deserves attention", never "buy" or "sell".
# Policy results never change research evidence, Research View, snapshots,
# outcomes or event scoring, and are never written to the database.
# Effective only when PORTFOLIO_AWARENESS_ENABLED is also true.
# --------------------------------------------------------------------------
PORTFOLIO_POLICY_ENABLED: bool = os.getenv("PORTFOLIO_POLICY_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
PORTFOLIO_POLICY_POSITION_WEIGHT_PCT: str = os.getenv("PORTFOLIO_POLICY_POSITION_WEIGHT_PCT", "10,20,30")        # >=
PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT: str = os.getenv("PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT", "25,40,60")            # >=
PORTFOLIO_POLICY_LOW_CASH_PCT: str = os.getenv("PORTFOLIO_POLICY_LOW_CASH_PCT", "10,5,2")                        # <
PORTFOLIO_POLICY_HIGH_EVENT_EXPOSURE_PCT: str = os.getenv("PORTFOLIO_POLICY_HIGH_EVENT_EXPOSURE_PCT", "10,25,50")  # >=
PORTFOLIO_POLICY_VALUATION_GAP_PCT: str = os.getenv("PORTFOLIO_POLICY_VALUATION_GAP_PCT", "0.25,0.50,1.00")      # |gap| >=
# Quote / basis data-quality: INFO = any affected position (count >= 1); MEDIUM,HIGH = share of portfolio value.
PORTFOLIO_POLICY_UNRELIABLE_QUOTE_VALUE_PCT: str = os.getenv("PORTFOLIO_POLICY_UNRELIABLE_QUOTE_VALUE_PCT", ",10,25")
PORTFOLIO_POLICY_MISSING_BASIS_VALUE_PCT: str = os.getenv("PORTFOLIO_POLICY_MISSING_BASIS_VALUE_PCT", ",10,25")
# Comma-separated rule ids to switch off (see GET /api/portfolio/policy for the ids).
PORTFOLIO_POLICY_DISABLED_RULES: str = os.getenv("PORTFOLIO_POLICY_DISABLED_RULES", "")


# --------------------------------------------------------------------------
# Stage 2.7E — beginner decision SUPPORT and market context (display labels only).
# These cutoffs only choose plain-language LABELS (e.g. "Strong"/"Weak") from
# facts the app already computes. They never change evidence scores, Research
# View, event scoring or policy arithmetic, and nothing here is an instruction
# to trade. Existing constants are reused wherever one exists (see insights/labels.py).
# --------------------------------------------------------------------------
BEGINNER_RESEARCH_STALE_HOURS: float = float(os.getenv("BEGINNER_RESEARCH_STALE_HOURS", "24"))
BEGINNER_MOMENTUM_STRONG_SCORE: int = int(os.getenv("BEGINNER_MOMENTUM_STRONG_SCORE", "60"))  # momentum_score 0-100, 50 = neutral
BEGINNER_MOMENTUM_WEAK_SCORE: int = int(os.getenv("BEGINNER_MOMENTUM_WEAK_SCORE", "40"))
BEGINNER_BEARISH_MATERIAL_PCT: int = int(os.getenv("BEGINNER_BEARISH_MATERIAL_PCT", "30"))  # bearish evidence share
MARKET_TECH_PROXY_SYMBOL: str = os.getenv("MARKET_TECH_PROXY_SYMBOL", "QQQ")
MARKET_VOL_LOW_PCT: float = float(os.getenv("MARKET_VOL_LOW_PCT", "12"))        # SPY annualized 20-day volatility
MARKET_VOL_ELEVATED_PCT: float = float(os.getenv("MARKET_VOL_ELEVATED_PCT", "25"))
MARKET_BREADTH_STRONG_PCT: float = float(os.getenv("MARKET_BREADTH_STRONG_PCT", "60"))
MARKET_BREADTH_WEAK_PCT: float = float(os.getenv("MARKET_BREADTH_WEAK_PCT", "40"))
MARKET_LARGE_MOVER_SHARE_PCT: float = float(os.getenv("MARKET_LARGE_MOVER_SHARE_PCT", "10"))
MARKET_FEEDBACK_STALE_MINUTES: float = float(os.getenv("MARKET_FEEDBACK_STALE_MINUTES", "30"))
PORTFOLIO_RECONCILIATION_REALIZED_SPAN: str = os.getenv("PORTFOLIO_RECONCILIATION_REALIZED_SPAN", "all")


# --------------------------------------------------------------------------
# Stage 2.7F — Beginner command center: data FRESHNESS labels (FRESH / AGING / STALE).
# Display only. Each "stale" boundary reuses the existing stale setting for that data.
# --------------------------------------------------------------------------
FRESHNESS_MARKET_FRESH_MINUTES: float = float(os.getenv("FRESHNESS_MARKET_FRESH_MINUTES", "15"))   # stale: MARKET_FEEDBACK_STALE_MINUTES
FRESHNESS_PORTFOLIO_FRESH_SECONDS: float = float(os.getenv("FRESHNESS_PORTFOLIO_FRESH_SECONDS", "120"))
FRESHNESS_PORTFOLIO_STALE_SECONDS: float = float(os.getenv("FRESHNESS_PORTFOLIO_STALE_SECONDS", "900"))
FRESHNESS_RESEARCH_FRESH_HOURS: float = float(os.getenv("FRESHNESS_RESEARCH_FRESH_HOURS", "6"))    # stale: BEGINNER_RESEARCH_STALE_HOURS
FRESHNESS_QUOTE_FRESH_SECONDS: float = float(os.getenv("FRESHNESS_QUOTE_FRESH_SECONDS", "300"))    # stale: PORTFOLIO_QUOTE_STALE_SECONDS
AI_CALLS_PER_RESEARCH_RUN: int = 3   # catalyst + risk + narrative (agents/beginner_agent.py), used for the cost warning

# ------------------------------------------------------------------------------------------------------------------
# Stage 2.7G.1 — operational polish (no financial logic). Warm the Stage 2.6 event calendars (FRED macro releases,
# FOMC) in the background when the server starts, so the first dashboard load does not wait on a cold FRED fetch.
# Startup never waits for it, failures never block startup, and it makes 0 Claude calls.
EVENT_WARMUP_ON_STARTUP: bool = os.getenv("EVENT_WARMUP_ON_STARTUP", "true").strip().lower() in ("1", "true", "yes", "on")
EVENT_WARMUP_TIMEOUT_SECONDS: float = float(os.getenv("EVENT_WARMUP_TIMEOUT_SECONDS", "90"))   # still running after this -> reported unavailable (slow)
EVENT_WARMUP_RETRY_SECONDS: float = float(os.getenv("EVENT_WARMUP_RETRY_SECONDS", "300"))      # minimum gap between warm-up attempts

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.4 — MULTI-SOURCE EVIDENCE RESEARCH (evidence/). Research-evidence layer only: no broker, no Robinhood call,
# no automatic Claude call. Every live read goes through evidence.net.fetch (https only, allowed hosts per adapter,
# deny list, one attempt per URL, timeout, size cap). Tests force EVIDENCE_NETWORK_ENABLED off and tripwire the wire.
# ------------------------------------------------------------------------------------------------------------------
def _env_json(name: str, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


SEC_USER_AGENT: str = os.getenv("SEC_USER_AGENT", "").strip()            # SEC requires "Name email"; the adapter refuses to run without it
EVIDENCE_NETWORK_ENABLED: bool = os.getenv("EVIDENCE_NETWORK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
EVIDENCE_FETCH_TIMEOUT_S: float = float(os.getenv("EVIDENCE_FETCH_TIMEOUT_S", "15"))
EVIDENCE_MAX_BYTES: int = int(os.getenv("EVIDENCE_MAX_BYTES", "2000000"))
EVIDENCE_LOOKBACK_DAYS: int = int(os.getenv("EVIDENCE_LOOKBACK_DAYS", "90"))
EVIDENCE_SEC_MAX_REQUESTS: int = int(os.getenv("EVIDENCE_SEC_MAX_REQUESTS", "12"))
EVIDENCE_SEC_MIN_INTERVAL_S: float = float(os.getenv("EVIDENCE_SEC_MIN_INTERVAL_S", "0.25"))
EVIDENCE_DENY_HOSTS: tuple = tuple(h.strip().lower() for h in os.getenv(
    "EVIDENCE_DENY_HOSTS", "bloomberg.com,reuters.com,wsj.com,ft.com,barrons.com,seekingalpha.com,x.com,twitter.com,facebook.com,linkedin.com").split(",") if h.strip())
EVIDENCE_PRESS_ALLOWLIST: tuple = ("Reuters", "Bloomberg", "The Wall Street Journal", "Financial Times", "Associated Press", "CNBC",
                                   "MarketWatch", "Barron's", "Benzinga")
EVIDENCE_SOCIAL_HOSTS: tuple = ("reddit.com", "x.com", "twitter.com", "stocktwits.com", "youtube.com", "facebook.com", "tiktok.com")
EVIDENCE_IR_DOMAINS: dict = _env_json("EVIDENCE_IR_DOMAINS_JSON", {})     # {"SNDK": ["investor.sandisk.com"]}
EVIDENCE_IR_URLS: dict = _env_json("EVIDENCE_IR_URLS_JSON", {})           # {"SNDK": ["https://investor.sandisk.com/..."]}
EVIDENCE_RELATED: dict = _env_json("EVIDENCE_RELATED_JSON", {})           # {"SNDK": {"suppliers": [...], "customers": [...]}}
EVIDENCE_CIK_OVERRIDES: dict = _env_json("EVIDENCE_CIK_JSON", {})         # {"SNDK": "0001234567"} when the SEC ticker map lags
# Sector benchmark extensions for the EVIDENCE layer only: ticker -> an EXISTING sector name in data.sector_map.SECTOR_MAP (its ETF is
# reused). The curated map itself is unchanged because earlier stages' tests rely on these tickers being unclassified there.
EVIDENCE_SECTOR_EXTENSIONS: dict = {"SNDK": "Semiconductors", **_env_json("EVIDENCE_SECTOR_EXTENSIONS_JSON", {})}   # SNDK: NAND / flash memory, same bucket as MU (SOXX)

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.5 — TRADER VIEW (trader_view/). A read-only terminal tab: simulated data unless real read-only market data is
# configured (TRADER_DATA_MODE=auto). No order path, no broker write, no Robinhood write, no model call, no evidence building.
# The cadences bound the market-data requests while the tab is open; nothing is polled when no browser is subscribed.
# ------------------------------------------------------------------------------------------------------------------
TRADER_DATA_MODE: str = os.getenv("TRADER_DATA_MODE", "auto").strip().lower()                  # auto | real | simulated
TRADER_TICK_SECONDS: float = 1.0                                                                 # stream loop period (simulated price step)
TRADER_QUOTE_POLL_SECONDS: float = float(os.getenv("TRADER_QUOTE_POLL_SECONDS", "3"))            # active ticker quote + current candle (real mode)
TRADER_WATCHLIST_POLL_SECONDS: float = float(os.getenv("TRADER_WATCHLIST_POLL_SECONDS", "10"))   # lightweight watchlist quotes
TRADER_POSITION_POLL_SECONDS: float = float(os.getenv("TRADER_POSITION_POLL_SECONDS", "30"))     # read-only position sources (cached)
TRADER_HISTORY_MINUTES: int = int(os.getenv("TRADER_HISTORY_MINUTES", "240"))                    # minute candles kept / backfilled
TRADER_SIM_SEED: int = int(os.getenv("TRADER_SIM_SEED", "7"))
TRADER_DEMO_SYMBOLS: tuple = ("AAPL", "NVDA", "SNDK")

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.6 — PPE ENGINE (ppe/). Deterministic PPE Stretch / Confidence shown in Trader View: computed on subscribe, on refresh
# and every TRADER_PPE_REFRESH_SECONDS for the active ticker — never per quote tick. Stored Stage 5.4 evidence only; no order
# path, no recommendation, no model call, no research run.
# ------------------------------------------------------------------------------------------------------------------
TRADER_PPE_REFRESH_SECONDS: float = float(os.getenv("TRADER_PPE_REFRESH_SECONDS", "60"))
PPE_BARS_LOOKBACK_DAYS: int = int(os.getenv("PPE_BARS_LOOKBACK_DAYS", "420"))          # calendar days of daily bars (~250 sessions + warm-up)
PPE_BARS_CACHE_SECONDS: float = float(os.getenv("PPE_BARS_CACHE_SECONDS", "900"))        # real mode: one batched bar read per symbol group per 15 min
# Sector benchmark extensions for PPE only: ticker -> an EXISTING sector name in data.sector_map.SECTOR_MAP (its ETF is reused). The curated map
# itself is unchanged (earlier stages' tests rely on it). AAPL -> "Software" because that sector's ETF is XLK (Technology Select Sector), which holds AAPL.
PPE_SECTOR_EXTENSIONS: dict = {"AAPL": "Software", **_env_json("PPE_SECTOR_EXTENSIONS_JSON", {})}

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.7 — MARKET DEMAND (demand/). Deterministic buyer-confirmation score (0 … 100) shown in Trader View NEXT TO PPE and never
# combined with it: computed on subscribe, on refresh and every TRADER_DEMAND_REFRESH_SECONDS for the active ticker — never per quote
# tick. Bars only (minute + daily); no order path, no recommendation, no model call, no research run, no change to PPE.
# ------------------------------------------------------------------------------------------------------------------
TRADER_DEMAND_REFRESH_SECONDS: float = float(os.getenv("TRADER_DEMAND_REFRESH_SECONDS", "30"))
DEMAND_PRIOR_SESSIONS: int = int(os.getenv("DEMAND_PRIOR_SESSIONS", "5"))                # prior sessions behind the expected intraday volume curve
DEMAND_DAILY_LOOKBACK_DAYS: int = int(os.getenv("DEMAND_DAILY_LOOKBACK_DAYS", "120"))    # calendar days of daily bars for the daily fallback (~80 sessions)

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.8 — SETUP STATE MACHINE + MARKET-HOURS ALERTS (setups/). NONE / WATCH / READY (= READY FOR REVIEW) / INVALIDATED from
# already-computed research only; persisted; alerts on transitions. The evaluator thread runs only inside the US regular session
# (weekdays 09:30–16:00 ET; no holiday calendar exists) and never in a test process; subscribe / refresh evaluate on demand.
# No order path, no recommendation, no model call, no research run, no 24/7 runtime.
# ------------------------------------------------------------------------------------------------------------------
SETUP_EVALUATOR_ENABLED: bool = os.getenv("SETUP_EVALUATOR_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
SETUP_EVAL_INTERVAL_SECONDS: float = float(os.getenv("SETUP_EVAL_INTERVAL_SECONDS", "60"))
SETUP_MARKET_HOURS_ONLY: bool = os.getenv("SETUP_MARKET_HOURS_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")
SETUP_ALERT_COOLDOWN_MINUTES: int = int(os.getenv("SETUP_ALERT_COOLDOWN_MINUTES", "15"))
TRADER_SETUP_REFRESH_SECONDS: float = float(os.getenv("TRADER_SETUP_REFRESH_SECONDS", "60"))    # the active ticker's in-session cadence in Trader View

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.9 — DETERMINISTIC TRADE PROPOSAL / REVIEW LAYER (proposals/). For a READY FOR REVIEW setup only: a review artifact
# (reference price, invalidation level, risk per share, risk budget, capped whole-share quantity, blockers, warnings, controls).
# Review only: no order path, no broker write, no recommendation, no target price, no expected return, no model call.
# The position cap reuses MAX_POSITION_PERCENT; the ATR period is the project's 14; the 4.6B hard limits apply unchanged.
# ------------------------------------------------------------------------------------------------------------------
TRADE_PROPOSAL_RISK_FRACTION: float = float(os.getenv("TRADE_PROPOSAL_RISK_FRACTION", "0.005"))   # 0.5 % of equity per review proposal (valid: 0 < f <= 0.01)
TRADE_PROPOSAL_REVIEW_EQUITY: float = float(os.getenv("TRADE_PROPOSAL_REVIEW_EQUITY", "0"))         # simulated mode only: explicit hypothetical equity (0 = not set)
TRADE_PROPOSAL_TTL_SECONDS: int = int(os.getenv("TRADE_PROPOSAL_TTL_SECONDS", "300"))               # intraday proposal expiry
TRADE_PROPOSAL_PRICE_CHANGE_PCT: float = float(os.getenv("TRADE_PROPOSAL_PRICE_CHANGE_PCT", "0.5"))   # reference move that regenerates a proposal
TRADE_PROPOSAL_HALT: bool = os.getenv("TRADE_PROPOSAL_HALT", "false").strip().lower() in ("1", "true", "yes", "on")             # review-only halt (no kill switch existed)
TRADE_PROPOSAL_DAILY_LOSS_HALT: bool = os.getenv("TRADE_PROPOSAL_DAILY_LOSS_HALT", "false").strip().lower() in ("1", "true", "yes", "on")   # explicit; no automatic daily P&L exists

# ------------------------------------------------------------------------------------------------------------------
# Stage 5.9.1 — REAL EXECUTION SAFETY CONTROLS (safety/). A persisted manual kill switch, a persisted no-new-entries pause and a
# REAL daily-loss halt measured against a persisted daily equity baseline. MAX_DAILY_LOSS_PERCENT (declared above with an older
# "informational only" comment) is ENFORCED by this stage as the daily-loss threshold. The runtime state lives in the database;
# the values below are policy / limits only. Nothing here submits, previews, liquidates or calls a broker.
# ------------------------------------------------------------------------------------------------------------------
EXECUTION_SAFETY_STARTUP_POLICY: str = os.getenv("EXECUTION_SAFETY_STARTUP_POLICY", "KEEP").strip().upper()      # KEEP | HALT_ON_START (never clears a stop)
EXECUTION_SAFETY_ACCOUNT_MAX_AGE_SECONDS: float = float(os.getenv("EXECUTION_SAFETY_ACCOUNT_MAX_AGE_SECONDS", "900"))   # an older Alpaca snapshot = daily loss unverifiable
EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL: bool = os.getenv("EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", "false").strip().lower() in ("1", "true", "yes", "on")   # explicit operator choice; default off
EXECUTION_SAFETY_EVALUATION_HEARTBEAT_SECONDS: float = float(os.getenv("EXECUTION_SAFETY_EVALUATION_HEARTBEAT_SECONDS", "60"))   # unchanged results re-persist at most this often

# ------------------------------------------------------------------------------------------------------------------
# Stage 6.0 — AUTONOMOUS ALPACA PAPER TRADING (execution/). The first autonomous loop: READY setup (5.8) -> reviewable proposal (5.9)
# -> execution safety gate (5.9.1) -> ONE paper entry -> order monitoring -> fills -> internal position -> deterministic exits -> ONE
# paper exit, on the Alpaca PAPER account only (constant host; no live mode exists anywhere). MONITOR_ONLY by default; PAPER_AUTO is an
# explicit, persisted user choice. MAX_OPEN_POSITIONS (declared above with an older "informational only" comment) is ENFORCED by this
# stage. No real money, no Robinhood, no model call. The executor thread never runs in a test / harness process and never contacts
# the broker while idle in MONITOR_ONLY with nothing open.
# ------------------------------------------------------------------------------------------------------------------
EXECUTION_SCHEDULER_ENABLED: bool = os.getenv("EXECUTION_SCHEDULER_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
EXECUTION_BROKER: str = os.getenv("EXECUTION_BROKER", "alpaca_paper").strip().lower()                  # alpaca_paper | simulated (deterministic in-memory paper broker)
EXECUTION_CYCLE_SECONDS: float = float(os.getenv("EXECUTION_CYCLE_SECONDS", "30"))                    # executor cadence inside the regular session
EXECUTION_IDLE_CYCLE_SECONDS: float = float(os.getenv("EXECUTION_IDLE_CYCLE_SECONDS", "300"))         # outside the session (day orders; nothing fills)
EXECUTION_MAX_ENTRIES_PER_CYCLE: int = int(os.getenv("EXECUTION_MAX_ENTRIES_PER_CYCLE", "1"))
EXECUTION_MAX_SHARES_PER_ENTRY: int = int(os.getenv("EXECUTION_MAX_SHARES_PER_ENTRY", "0"))             # 0 = the proposal quantity exactly; > 0 = an explicit operator hard cap (first paper smoke)
EXECUTION_NEAR_CLOSE_MINUTES: int = int(os.getenv("EXECUTION_NEAR_CLOSE_MINUTES", "15"))                # no market order in the final minutes of the session
EXECUTION_MAX_HOLD_SESSIONS: int = int(os.getenv("EXECUTION_MAX_HOLD_SESSIONS", "5"))                   # TIME_EXIT after this many regular sessions
EXECUTION_DEMAND_EXIT: int = int(os.getenv("EXECUTION_DEMAND_EXIT", "35"))                              # DEMAND_FAILURE: Demand below this on EXECUTION_DEMAND_EXIT_EVALS consecutive evaluations
EXECUTION_DEMAND_EXIT_EVALS: int = int(os.getenv("EXECUTION_DEMAND_EXIT_EVALS", "2"))
EXECUTION_STRETCH_EXIT: float = float(os.getenv("EXECUTION_STRETCH_EXIT", "-20"))                       # DISLOCATION_NORMALIZED: PPE Stretch above this
EXECUTION_MAX_EXIT_CYCLES: int = int(os.getenv("EXECUTION_MAX_EXIT_CYCLES", "3"))                       # bounded exit attempts per position; beyond it the position needs attention
EXECUTION_LOOKUP_SPAN_SECONDS: float = float(os.getenv("EXECUTION_LOOKUP_SPAN_SECONDS", "30"))        # after an ambiguous POST: two lookups at least this far apart before NOT_SENT

# ------------------------------------------------------------------------------------------------------------------
# Stage 6.0.1 - CONTINUOUS ALPACA PAPER ACCOUNT REFRESH (account_refresh/). One background owner keeps a READ-ONLY, persisted snapshot of the
# paper account fresh (status, blocked flags, equity, cash, buying power, positions, open orders) for the Stage 5.9.1 gate and the Stage 5.9
# proposals. Read only: no order, no cancel, no position change, no strategy change. It does NOT solve deposit / withdrawal adjustment:
# EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL keeps its meaning and is never set by this stage. Never runs in a test / harness process.
# ------------------------------------------------------------------------------------------------------------------
ACCOUNT_REFRESH_ENABLED: bool = os.getenv("ACCOUNT_REFRESH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ACCOUNT_REFRESH_SECONDS: float = float(os.getenv("ACCOUNT_REFRESH_SECONDS", "60"))                 # inside the US regular session
ACCOUNT_REFRESH_IDLE_SECONDS: float = float(os.getenv("ACCOUNT_REFRESH_IDLE_SECONDS", "300"))      # outside it
ACCOUNT_REFRESH_STALE_SECONDS: float = float(os.getenv("ACCOUNT_REFRESH_STALE_SECONDS", "600"))    # no success for this long = stale (gate limit is 900 s)
ACCOUNT_REFRESH_MAX_FAILURES: int = int(os.getenv("ACCOUNT_REFRESH_MAX_FAILURES", "3"))            # consecutive failed refreshes = stale
ACCOUNT_REFRESH_READ_ATTEMPTS: int = int(os.getenv("ACCOUNT_REFRESH_READ_ATTEMPTS", "3"))          # bounded READ retries per refresh (never for orders)
ACCOUNT_REFRESH_BACKOFF_SECONDS: str = os.getenv("ACCOUNT_REFRESH_BACKOFF_SECONDS", "2,5")         # delays between read attempts
ACCOUNT_REFRESH_TEST_FAULTS: bool = os.getenv("ACCOUNT_REFRESH_TEST_FAULTS", "false").strip().lower() in ("1", "true", "yes", "on")   # test mode only
ACCOUNT_REFRESH_FAULT_FILE: str = os.getenv("ACCOUNT_REFRESH_FAULT_FILE", "")                        # test mode: while this file exists every refresh fails (no broker call)

# ------------------------------------------------------------------------------------------------------------------
# Stage 6.0.2 - CASH-FLOW-ADJUSTED DAILY P&L (cash_flow/). A READ-ONLY, persisted ledger of the paper account's non-trade activities
# (GET /v2/account/activities, paper host only), refreshed in the Stage 6.0.1 refresh cycle; the Stage 5.9.1 gate evaluates the daily loss
# on (current - (start + net external cash flow)). EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL stays OFF and is no longer needed. Never runs in a
# test / harness process (it rides on the account refresh loop).
# ------------------------------------------------------------------------------------------------------------------
CASH_FLOW_ENABLED: bool = os.getenv("CASH_FLOW_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
CASH_FLOW_STALE_SECONDS: float = float(os.getenv("CASH_FLOW_STALE_SECONDS", "600"))       # no successful activity read for this long = stale
CASH_FLOW_MAX_FAILURES: int = int(os.getenv("CASH_FLOW_MAX_FAILURES", "3"))               # consecutive failed reads = stale
CASH_FLOW_READ_ATTEMPTS: int = int(os.getenv("CASH_FLOW_READ_ATTEMPTS", "3"))             # bounded READ retries per refresh
CASH_FLOW_BACKOFF_SECONDS: str = os.getenv("CASH_FLOW_BACKOFF_SECONDS", "2,5")            # delays between read attempts (a 429 Retry-After is honoured, capped at 30 s)
CASH_FLOW_MARGIN_SECONDS: float = float(os.getenv("CASH_FLOW_MARGIN_SECONDS", "60"))      # timing margin around the baseline read and the current snapshot
CASH_FLOW_LOOKBACK_DAYS: int = int(os.getenv("CASH_FLOW_LOOKBACK_DAYS", "7"))             # each read covers activities created in the last N days
CASH_FLOW_BOOTSTRAP_DAYS: int = int(os.getenv("CASH_FLOW_BOOTSTRAP_DAYS", "30"))          # the first read of a process covers N days
CASH_FLOW_PAGE_SIZE: int = int(os.getenv("CASH_FLOW_PAGE_SIZE", "100"))                   # Alpaca maximum
CASH_FLOW_MAX_PAGES: int = int(os.getenv("CASH_FLOW_MAX_PAGES", "20"))                    # reaching it = INCOMPLETE (never silently truncated)
CASH_FLOW_FAULT_FILE: str = os.getenv("CASH_FLOW_FAULT_FILE", "")                         # test mode (ACCOUNT_REFRESH_TEST_FAULTS) only: while it exists every activity read fails
CASH_FLOW_CYCLE_WAIT_SECONDS: float = float(os.getenv("CASH_FLOW_CYCLE_WAIT_SECONDS", "15"))  # the gate waits at most this long for the activity read of the cycle whose snapshot it sees

# ------------------------------------------------------------------------------------------------------------------
# Stage 6.1 - FORWARD PAPER VALIDATION (forward_validation/). Instrumentation only: an observer records one sample per minute inside the
# regular session (every five minutes outside it), one record per New York trading session, price marks of open paper positions and
# process restarts, and derives descriptive operational / strategy statistics from the PERSISTED events of Stages 5.8 - 6.0.2. No
# strategy, threshold, size, exit, mode or order change; its only action is the early stop (Paper Auto -> Monitor only on a critical
# defect). Alpaca PAPER only. Never runs in a test / harness process.
# ------------------------------------------------------------------------------------------------------------------
FORWARD_VALIDATION_ENABLED: bool = os.getenv("FORWARD_VALIDATION_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
FORWARD_VALIDATION_INTERVAL_SECONDS: float = float(os.getenv("FORWARD_VALIDATION_INTERVAL_SECONDS", "60"))     # observer cadence inside the regular session
FORWARD_VALIDATION_IDLE_SECONDS: float = float(os.getenv("FORWARD_VALIDATION_IDLE_SECONDS", "300"))            # outside it
FORWARD_VALIDATION_TARGET_SESSIONS: int = int(os.getenv("FORWARD_VALIDATION_TARGET_SESSIONS", "30"))           # the operational milestone (never a profitability claim)
FORWARD_VALIDATION_TARGET_EVENTS: int = int(os.getenv("FORWARD_VALIDATION_TARGET_EVENTS", "100"))              # READY transitions + reviewable proposals wanted before conclusions
FORWARD_VALIDATION_MIN_TRADES: int = int(os.getenv("FORWARD_VALIDATION_MIN_TRADES", "30"))                     # below it the strategy status stays INSUFFICIENT_DATA
FORWARD_VALIDATION_EARLY_STOP: bool = os.getenv("FORWARD_VALIDATION_EARLY_STOP", "true").strip().lower() in ("1", "true", "yes", "on")   # Paper Auto -> Monitor only on a critical defect

# Stage 6.9: Robinhood READ-ONLY portfolio source of the unified portfolio (unified_portfolio/; DESIGN_69_ROBINHOOD_UNIFIED_PORTFOLIO.md).
# Reads go through the existing loopback rh_gateway (RH_GATEWAY_URL, PORTFOLIO_ACCOUNT_ALIAS), which calls only read tools. There is NO
# Robinhood execution setting: no configuration path enables a Robinhood write, and Robinhood holdings never become bot-managed.
ROBINHOOD_PORTFOLIO_ENABLED: bool = os.getenv("ROBINHOOD_PORTFOLIO_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
ROBINHOOD_PORTFOLIO_SOURCE: str = os.getenv("ROBINHOOD_PORTFOLIO_SOURCE", "gateway").strip().lower()       # gateway | fixture (synthetic data for previews / tests)
ROBINHOOD_PORTFOLIO_FIXTURE: str = os.getenv("ROBINHOOD_PORTFOLIO_FIXTURE", "")                               # the synthetic gateway-shaped JSON file (fixture only)
ROBINHOOD_PORTFOLIO_TTL_SECONDS: float = float(os.getenv("ROBINHOOD_PORTFOLIO_TTL_SECONDS", "120"))           # a read older than this is refreshed on the next unified read
ROBINHOOD_DUPLICATE_ENTRY_POLICY: str = os.getenv("ROBINHOOD_DUPLICATE_ENTRY_POLICY", "ADVISORY").strip().upper()   # ADVISORY | OFF - never enforced by the executor in Stage 6.9
UNIFIED_CONCENTRATION_WARN_PCT: float = float(os.getenv("UNIFIED_CONCENTRATION_WARN_PCT", "20"))              # one symbol >= this % of the known invested value -> warning

# Stage 6.9.1: UNIFIED EXTERNAL EXPOSURE GATE (execution/exposure_gate.py; DESIGN_691_UNIFIED_EXPOSURE_GATE.md). One deterministic entry control,
# UNIFIED_EXTERNAL_EXPOSURE, for NEW Alpaca PAPER entries only: ALLOW or BLOCK - never a resize, never an exit. The other accounts of the unified
# portfolio (Robinhood, read only) are a risk input only. OFF by default (advisory, as in Stage 6.9) until reviewed. Still no Robinhood execution setting.
UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED: bool = os.getenv("UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
UNIFIED_BLOCK_DUPLICATE_SYMBOL: bool = os.getenv("UNIFIED_BLOCK_DUPLICATE_SYMBOL", "true").strip().lower() in ("1", "true", "yes", "on")   # another account holds it -> BLOCK
UNIFIED_MAX_SYMBOL_EXPOSURE_PCT: str = os.getenv("UNIFIED_MAX_SYMBOL_EXPOSURE_PCT", "20").strip()             # Decimal text in (0, 100]: BLOCK if projected > it (100 never blocks)
UNIFIED_STALE_SOURCE_POLICY: str = os.getenv("UNIFIED_STALE_SOURCE_POLICY", "BLOCK_DUPLICATE_ONLY_IF_KNOWN").strip().upper()   # | BLOCK_IF_UNKNOWN (fail closed while unknown)

# Stage 6.10A: FORWARD PAPER AUTO-RUN (autorun/; DESIGN_610A_FORWARD_PAPER_AUTORUN.md) - WHEN Paper Auto may be armed, never how an entry is decided.
# MANUAL_ARM (default): every start or restart is Monitor only and a person arms Paper Auto. SCHEDULED_ARM arms at most once per regular session after
# every startup check passes, and ONLY with PAPER_AUTO_SCHEDULED_ARM_ENABLED=true as well (default false; not enabled during development).
PAPER_AUTO_RUN_POLICY: str = os.getenv("PAPER_AUTO_RUN_POLICY", "MANUAL_ARM").strip().upper()                               # MANUAL_ARM | SCHEDULED_ARM (unknown = MANUAL_ARM)
PAPER_AUTO_SCHEDULED_ARM_ENABLED: bool = os.getenv("PAPER_AUTO_SCHEDULED_ARM_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES: str = os.getenv("PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES", "2").strip()                # minutes after the 09:30 ET open (integer text)
PAPER_AUTO_DISARM_AT_SESSION_END: bool = os.getenv("PAPER_AUTO_DISARM_AT_SESSION_END", "true").strip().lower() in ("1", "true", "yes", "on")   # never carried over a close
AUTORUN_SUPERVISOR_ENABLED: bool = os.getenv("AUTORUN_SUPERVISOR_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")   # lease renewal, preflight, automatic disarm
