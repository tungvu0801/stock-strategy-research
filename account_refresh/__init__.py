"""
account_refresh/ - Stage 6.0.1 CONTINUOUS ALPACA PAPER ACCOUNT REFRESH (DESIGN_601_CONTINUOUS_ACCOUNT_REFRESH.md).

One background owner keeps a READ-ONLY, persisted snapshot of the Alpaca PAPER account fresh (status, blocked flags, equity, cash,
buying power, positions, open orders) so the Stage 5.9.1 safety gate and the Stage 5.9 proposals no longer depend on a manual refresh.

Boundaries (tests/test_account_refresh_601.py pins them):
  - read only: GET /v2/account, /v2/positions, /v2/orders?status=open through the Stage 6.0 paper client (constant paper host); no POST,
    no cancel, no position change, no execution-mode change, no strategy state;
  - a failed or invalid read never replaces the last good snapshot and never reads as fresh; the gate fails closed;
  - this stage does NOT solve deposit / withdrawal adjustment: DAILY_PNL_ADJUSTMENT_UNAVAILABLE keeps its Stage 5.9.1 meaning;
  - no Robinhood, no model call.
"""
VERSION = "account_refresh_v1"
STAGE = "6.0.1"
LEASE_NAME = "alpaca_account_refresher"
STATUSES = ("FRESH", "STARTING", "STALE", "UNAVAILABLE", "DISABLED")
LABELS = {"FRESH": "Fresh", "STARTING": "Checking", "STALE": "Stale", "UNAVAILABLE": "Unavailable", "DISABLED": "Unavailable"}
RESULTS = ("SUCCESS", "FAILED", "INVALID")
TRIGGERS = ("STARTUP", "SCHEDULED", "MANUAL", "TEST")
REQUIRED_ACCOUNT_FIELDS = ("status", "equity", "cash", "buying_power", "trading_blocked", "account_blocked")
RETRYABLE_CODES = ("TIMEOUT", "CONNECTION", "RATE_LIMITED", "UNREADABLE")            # plus any HTTP_5xx
READ_PATHS = ("/v2/account", "/v2/positions", "/v2/orders")
DISCLAIMER = ("Read-only Alpaca PAPER account data, refreshed automatically. It feeds the risk controls; it never places, cancels or changes an "
              "order or a position. Deposits and withdrawals still cannot be separated from trading P&L.")
