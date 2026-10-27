"""
unified_portfolio/ - Stage 6.9 ROBINHOOD READ-ONLY PORTFOLIO + UNIFIED EXPOSURE (DESIGN_69_ROBINHOOD_UNIFIED_PORTFOLIO.md).

  robinhood.py   RobinhoodPortfolioAdapter (ONE read operation) + RobinhoodSource (cached snapshot, freshness, last known snapshot kept on failure)
  fixture.py     a synthetic, gateway-shaped provider for previews / tests (labelled SYNTHETIC, never a real account)
  state.py       UnifiedPortfolioState: Alpaca PAPER rows (bot-managed / external) + Robinhood REAL rows (user-managed), exposure, concentration,
                 informational warnings and the advisory duplicate-entry policy

Robinhood is NOT an execution broker: nothing here can place, change, cancel or close an order or a position, nothing is passed to an execution
client, and Robinhood holdings are never bot-managed. Alpaca PAPER stays the only execution broker; its semantics are unchanged.
"""
VERSION = "unified_portfolio_v1"
STAGE = "6.9"

ALPACA_PAPER, SIMULATED_PAPER, ROBINHOOD, ROBINHOOD_FIXTURE = "ALPACA_PAPER", "SIMULATED_PAPER", "ROBINHOOD", "ROBINHOOD_FIXTURE"
PAPER, REAL, SYNTHETIC = "PAPER", "REAL", "SYNTHETIC"
ACCOUNT_TYPE = {ALPACA_PAPER: PAPER, SIMULATED_PAPER: PAPER, ROBINHOOD: REAL, ROBINHOOD_FIXTURE: SYNTHETIC}   # a fixture is never presented as REAL
SOURCE_LABELS = {ALPACA_PAPER: "Alpaca PAPER", SIMULATED_PAPER: "Simulated paper broker", ROBINHOOD: "Robinhood · REAL · user managed",
                 ROBINHOOD_FIXTURE: "Robinhood · SYNTHETIC FIXTURE (not a real account)"}

BOT_MANAGED, EXTERNAL, USER_MANAGED, NEEDS_REVIEW = "BOT_MANAGED", "EXTERNAL", "USER_MANAGED", "NEEDS_REVIEW"
OWNERSHIP_LABELS = {BOT_MANAGED: "Bot managed", EXTERNAL: "External · not managed by the bot", USER_MANAGED: "User managed · the bot never trades it",
                    NEEDS_REVIEW: "Not managed · needs a look"}

# one source's state; "positions" is None in every state but LOADED / STALE: unknown is never zero holdings
NOT_CONFIGURED, AUTH_REQUIRED, NOT_LOADED, LOADED, STALE, ERROR = "NOT_CONFIGURED", "AUTH_REQUIRED", "NOT_LOADED", "LOADED", "STALE", "ERROR"
SOURCE_STATES = (NOT_CONFIGURED, AUTH_REQUIRED, NOT_LOADED, LOADED, STALE, ERROR)
# the unified state: COMPLETE only when every source is loaded and fresh
COMPLETE, STALE_SOURCE, PARTIAL, UNAVAILABLE = "COMPLETE", "STALE_SOURCE", "PARTIAL", "UNAVAILABLE"

DUPLICATE_ENTRY_POLICY = "BLOCK_NEW_BOT_ENTRY_IF_EXTERNAL_EXPOSURE"
CURRENT_ENTRY_POLICY = ("The executor blocks a new bot entry only when the Alpaca PAPER account already holds the symbol (control BROKER_POSITION) and "
                        "counts every Alpaca position toward the open-positions limit. Robinhood holdings are not an executor input in Stage 6.9.")
# Stage 6.9.1, when UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true (off by default: CURRENT_ENTRY_POLICY above stays the policy)
CURRENT_ENTRY_POLICY_691 = ("The executor blocks a new bot entry when the Alpaca PAPER account already holds the symbol (control BROKER_POSITION, unchanged) and, "
                            "through control UNIFIED_EXTERNAL_EXPOSURE (Stage 6.9.1), when Robinhood already holds it or the projected symbol share of the known "
                            "invested value exceeds the limit. Robinhood holdings are a risk input only: never traded, exited or resized.")
