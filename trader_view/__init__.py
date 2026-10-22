"""
trader_view/ — Stage 5.5 TRADER VIEW: a read-only terminal page inside the existing dashboard (DESIGN_55_TRADER_VIEW.md).

One active ticker with live candles and a quote, a watchlist with lighter updates, Back / Forward navigation, a read-only position
panel shown PER SOURCE (never combined), the latest STORED Stage 5.4 evidence packet, a PPE placeholder slot whose contract Stage 5.6
fills, and action controls that are PREVIEW NOT CONNECTED.

Boundaries (tests/test_trader_view_55.py pins them):
  - no order path: no POST route, no order writer, no trading client, no broker write, no Robinhood write;
  - no model call and no evidence building: research is READ from the store only;
  - market data is the deterministic simulator or the EXISTING read-only Alpaca market-data client (data.market_data), polled at a
    bounded cadence only while a browser is subscribed; nothing is fabricated when a feed is unavailable;
  - nothing here writes the database or watchlist.txt.
"""
STAGE = "5.5"
LABEL = "TRADER VIEW · READ ONLY · NO ORDER PATH · PREVIEW NOT CONNECTED"
DATA_MODES = ("auto", "real", "simulated")
NOT_CONNECTED = "Paper order preview not connected in Trader View."
MESSAGE_TYPES = ("hello", "subscribed", "unsubscribed", "state", "quote", "candle", "candles", "watchlist", "position", "evidence",
                 "ppe", "demand", "setup", "setup_alert", "proposal", "safety",
                 "heartbeat", "error")
SYMBOL_BOUND_TYPES = ("quote", "candle", "candles", "state", "position", "evidence", "ppe", "demand", "setup", "proposal")      # must carry the active symbol
# "setup_alert" is deliberately NOT symbol-bound: a persisted transition alert for any watchlist ticker reaches every open Trader View tab.
# "proposal" (Stage 5.9) is symbol-bound: a review proposal is only ever painted under its own ticker. It is never an order.
# "safety" (Stage 5.9.1) is global (not symbol-bound): the persisted execution safety gate state, sent only to the requester of a test-only safety_eval.
CLIENT_COMMANDS = ("subscribe", "unsubscribe", "ping", "refresh")
