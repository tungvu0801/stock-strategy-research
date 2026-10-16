"""
evidence/ — Stage 5.4 MULTI-SOURCE EVIDENCE RESEARCH (DESIGN_54). Research-evidence layer only.

For one ticker that has declined, build an immutable, hashed evidence packet from sourced items (SEC EDGAR, the company's
own investor-relations page, existing Alpaca headlines, user-supplied public URLs, internal market / sector / event data),
then answer with deterministic rules: what caused the decline (market-wide vs company-specific), the six-rung recovery
ladder, contradictions, gaps, next events and an evidence-strength grade. The grade is evidence strength, never a
price probability. Nothing here places an order, sizes a position, calls Robinhood, or calls a model automatically.
"""
from __future__ import annotations

ENGINE_VERSION = "5.4.0"
RULES_VERSION = "ev_v1"
PROMPT_VERSION = "ev_explain_v1"

MARKET_WIDE, COMPANY_SPECIFIC, INDUSTRY_SUPPLY_CHAIN, COMPANY_CONFIRMATION, MARKET_CONFIRMATION = (
    "MARKET_WIDE", "COMPANY_SPECIFIC", "INDUSTRY_SUPPLY_CHAIN", "COMPANY_CONFIRMATION", "MARKET_CONFIRMATION")
CATEGORIES = (MARKET_WIDE, COMPANY_SPECIFIC, INDUSTRY_SUPPLY_CHAIN, COMPANY_CONFIRMATION, MARKET_CONFIRMATION)

FACT, INFERENCE, UNCONFIRMED = "FACT", "INFERENCE", "UNCONFIRMED"
EPISTEMIC = (FACT, INFERENCE, UNCONFIRMED)

UP, DOWN, NEUTRAL, NA = "UP", "DOWN", "NEUTRAL", "NA"
DIRECTIONS = (UP, DOWN, NEUTRAL, NA)

SUPPORTED, PARTIAL, NOT_SUPPORTED, NOT_AVAILABLE = "SUPPORTED", "PARTIAL", "NOT_SUPPORTED", "NOT_AVAILABLE"
RUNG_STATUSES = (SUPPORTED, PARTIAL, NOT_SUPPORTED, NOT_AVAILABLE)

WEAK, MODERATE, STRONG = "WEAK", "MODERATE", "STRONG"
STRENGTHS = (WEAK, MODERATE, STRONG)

# Cause of the decline. NOT_SEPARABLE is the fail-closed answer when no sector benchmark exists: the engine never infers company
# specificity from target-vs-SPY divergence alone.
NOT_SEPARABLE = "NOT_SEPARABLE"
CAUSE_VERDICTS = ("MARKET_WIDE_DOMINANT", "COMPANY_SPECIFIC_DOMINANT", "MIXED", NOT_SEPARABLE, "NO_MATERIAL_DECLINE", "NOT_AVAILABLE")

SOURCE_TYPES = ("SEC_FILING", "IR_RELEASE", "ALPACA_NEWS", "MANUAL_URL", "MANUAL_TEXT", "MARKET_DATA", "EVENT")
KINDS = ("FILING", "EARNINGS", "GUIDANCE", "HEADLINE", "PAGE", "METRIC", "EVENT", "TEXT")
ROLES = ("company", "supplier", "customer", "industry", "press", "social", "market", "event")
TIERS = (1, 2, 3, 4, 5)

PACKET_STATUSES = ("COMPLETED", "FAILED")
FAILURE_CODES = ("INVALID_TICKER", "NO_MARKET_DATA", "NETWORK_DISABLED", "INTERNAL_ERROR")
FETCH_STATUSES = ("OK", "TIMEOUT", "HTTP_ERROR", "ACCESS_DENIED", "HOST_NOT_ALLOWED", "HOST_DENIED", "TOO_LARGE", "PARSE_FAILED",
                  "EMPTY", "REDIRECT_BLOCKED", "DISABLED")
EXPLANATION_STATUSES = ("COMPLETED", "FAILED", "WITHHELD")

RUNGS = ((1, "market backdrop improving"), (2, "industry fundamentals improving"), (3, "supplier / customer confirmation"),
         (4, "company-specific fundamentals improving"), (5, "company guidance confirming recovery"), (6, "market / price confirmation"))

STRENGTH_DISCLAIMER = "This is an evidence-strength grade, not a probability that the stock will rise."
CONFIRMATION_MISSING = "Recovery thesis is strengthening, but company-specific confirmation is still missing."

LIMITS = {"sec_requests": 12, "ir_urls": 6, "manual_urls": 12, "headlines": 10, "items": 200, "fetch_timeout_s": 15, "build_s": 120,
          "lookback_min_days": 10, "lookback_max_days": 365, "text_chars": 2000,          # stored display snippet
          "rule_text_chars": 200_000}                                                     # full extracted document the rules evaluate (never stored)

CONVENTIONS = {
    "epistemic": "FACT = verbatim from a fetched, hashed source of tier <= 3 or computed from official market data; INFERENCE = rule output derived from "
                 "FACT items (ids stored); UNCONFIRMED = tier 5, manual text without a fetchable source, or a failed fetch. The model never assigns a label.",
    "tiers": "1 official (the ticker's SEC filings, its declared IR domain, official market data) · 2 official material of a declared supplier / customer "
             "· 3 established press reached through Alpaca headlines · 4 other public page supplied by the user · 5 social (sentiment only, excluded from the ladder)",
    "strength": STRENGTH_DISCLAIMER,
    "network": "https only; per-adapter allowed hosts; deny list; same-host redirects only; 401/402/403/429 = ACCESS_DENIED without retry; one attempt per URL; "
               "timeout and size cap; every attempt stored; a failure never falls back to model knowledge.",
    "boundaries": "No order, no position size, no Robinhood call of any kind, no automatic model call, no change to Stage 4.7 or Stage 5.x semantics.",
}
