"""
cash_flow/ - Stage 6.0.2 CASH-FLOW-ADJUSTED DAILY P&L (DESIGN_602_ADJUSTED_DAILY_PNL.md).

A read-only, persisted ledger of the Alpaca PAPER account's non-trade activities (GET /v2/account/activities on the paper host only),
classified deterministically into external funding (DEPOSIT / WITHDRAWAL / JOURNAL_IN / JOURNAL_OUT) and account performance (FEE /
DIVIDEND / INTEREST / OTHER), plus today's net external cash flow measured against the Stage 5.9.1 daily baseline. The Stage 5.9.1 gate
uses it to evaluate the daily-loss control on adjusted P&L:

    net_external_cash_flow   = (deposits + journal_in) - (withdrawals + journal_out)
    adjusted_starting_equity = starting_equity + net_external_cash_flow
    adjusted_daily_pnl       = current_equity - adjusted_starting_equity
    adjusted_daily_loss_pct  = max(0, (adjusted_starting_equity - current_equity) / adjusted_starting_equity * 100)

Boundaries (tests/test_cash_flow_602.py pins them): one GET path on the constant paper host with a request budget; no order, cancel,
position or execution-mode change; no strategy state; unknown activity types are never guessed into funding; no Robinhood; no model call.
"""
VERSION = "cash_flow_v1"
STAGE = "6.0.2"
ACTIVITIES_PATH = "/v2/account/activities"
CATEGORY_FILTER = "non_trade_activity"            # server-side filter (audited 2026-10-23); trade rows that still arrive are skipped

CATEGORIES = ("DEPOSIT", "WITHDRAWAL", "JOURNAL_IN", "JOURNAL_OUT", "FEE", "DIVIDEND", "INTEREST", "OTHER")
EXTERNAL = ("DEPOSIT", "WITHDRAWAL", "JOURNAL_IN", "JOURNAL_OUT")
INFLOWS = ("DEPOSIT", "JOURNAL_IN")
OUTFLOWS = ("WITHDRAWAL", "JOURNAL_OUT")
UNCLASSIFIED = "UNCLASSIFIED"

# Alpaca activity type -> (category when net_amount > 0, category when net_amount < 0). Funding is classified by the sign of the cash moved;
# a sign the type cannot have (a negative CSD, a positive CSW) is never forced into a category.
EXTERNAL_TYPES = {"CSD": ("DEPOSIT", None), "CSW": (None, "WITHDRAWAL"), "TRANS": ("DEPOSIT", "WITHDRAWAL"), "ACATC": ("DEPOSIT", "WITHDRAWAL"),
                  "JNLC": ("JOURNAL_IN", "JOURNAL_OUT"), "JNL": ("JOURNAL_IN", "JOURNAL_OUT")}
# Account performance: stays inside trading P&L (never an adjustment of the baseline).
PERFORMANCE_TYPES = {"FEE": "FEE", "CFEE": "FEE", "PTC": "FEE", "PTR": "FEE",
                     "DIV": "DIVIDEND", "DIVCGL": "DIVIDEND", "DIVCGS": "DIVIDEND", "DIVFEE": "DIVIDEND", "DIVFT": "DIVIDEND", "DIVNRA": "DIVIDEND",
                     "DIVROC": "DIVIDEND", "DIVTW": "DIVIDEND", "DIVTXEX": "DIVIDEND",
                     "INT": "INTEREST", "INTNRA": "INTEREST", "INTTW": "INTEREST",
                     "MA": "OTHER", "NC": "OTHER", "OPASN": "OTHER", "OPEXP": "OTHER", "OPXRC": "OTHER", "REORG": "OTHER", "SC": "OTHER", "SSO": "OTHER",
                     "SSP": "OTHER", "CIL": "OTHER", "SPIN": "OTHER", "SPLIT": "OTHER"}
TRADE_TYPES = ("FILL",)                            # trades: trading P&L; never stored in this ledger
# Known types deliberately left unclassified: MISC (meaning unknown), JNLS / ACATS (securities moved in or out: no cash value in the record).
UNVALUED_TYPES = ("MISC", "JNLS", "ACATS")
EXECUTED = "executed"

FRESHNESS = ("FRESH", "STALE", "UNAVAILABLE", "AMBIGUOUS")                      # the cash-flow data (DESIGN_602 section 4)
ADJUSTMENT_STATUSES = ("ADJUSTED", "PENDING", "AMBIGUOUS", "UNCERTAIN", "STALE", "UNAVAILABLE")   # today's adjustment (section 5)
EFFECTS = ("COUNTED", "BEFORE_BASELINE", "PERFORMANCE", "PENDING", "AMBIGUOUS", "UNCERTAIN")
LABELS = {"FRESH": "Fresh", "STALE": "Stale", "UNAVAILABLE": "Unavailable", "AMBIGUOUS": "Unclear"}
RESULTS = ("SUCCESS", "INCOMPLETE", "FAILED", "INVALID")
TRIGGERS = ("STARTUP", "SCHEDULED", "MANUAL", "TEST")
RETRYABLE_CODES = ("TIMEOUT", "CONNECTION", "RATE_LIMITED", "UNREADABLE")      # plus any HTTP_5xx
MAX_RETRY_AFTER_S = 30.0                                                         # a 429 Retry-After is honoured up to this many seconds
DISCLAIMER = ("Read-only Alpaca PAPER account activity (deposits, withdrawals, journals, fees, dividends, interest). It separates money moved in or "
              "out from trading P&L for the daily-loss control; it never places, cancels or changes an order or a position.")
