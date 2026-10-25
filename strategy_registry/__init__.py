"""
strategy_registry/ — Stage 6.1.2 STRATEGY LAB FOUNDATION: the Strategy Registry (read only) and the Strategy Standard v1
(DESIGN_612_STRATEGY_REGISTRY.md, DESIGN_612_QUANTCONNECT_ADAPTER.md, STRATEGY_STANDARD_V1.md).

A strategy PRODUCES evidence and signals. It never executes. Every entry still runs the unchanged chain
    Strategy -> Opportunity / Proposal (Stage 5.9) -> Portfolio -> Risk / Safety (Stage 5.9.1) -> Execution (Stage 6.0) -> Broker (Alpaca PAPER)
and a signal that names an order, a quantity, a broker or an account is rejected by the standard. The registry describes the ONE implemented
strategy (DOWNSIDE_DISLOCATION_RECOVERY: the Stage 5.6-6.1 path) from its real constants and design documents, and holds placeholder entries
(status RESEARCH, implemented = False, no rules, no data, no signal) for the future internal strategies. QuantConnect is a SOURCE the data model
accepts, not a broker and not an integration: no QuantConnect strategy is registered and LEAN is not wired. Nothing here mutates anything, calls a
broker, a model or the network.
"""
VERSION = "strategy_registry_v1"
STANDARD_VERSION = "strategy_standard_v1"

SOURCES = ("INTERNAL", "QUANTCONNECT")
SOURCE_LABELS = {"INTERNAL": "Internal (Stock Agent rules)", "QUANTCONNECT": "QuantConnect / LEAN (research + backtest only; signals pass the adapter)"}
STATUSES = ("RESEARCH", "BACKTEST_CANDIDATE", "REVIEW", "PAPER_CANDIDATE", "PAPER_ACTIVE", "APPROVED", "REJECTED")
STATUS_LABELS = {"RESEARCH": "Research", "BACKTEST_CANDIDATE": "Backtest candidate", "REVIEW": "Review", "PAPER_CANDIDATE": "Paper candidate",
                 "PAPER_ACTIVE": "Paper active", "APPROVED": "Approved", "REJECTED": "Rejected"}
ACTIVE_STATUSES = ("PAPER_CANDIDATE", "PAPER_ACTIVE", "APPROVED")      # the only statuses whose strategy may appear as a live setup / signal source
DECIDED_STATUSES = ("APPROVED", "REJECTED")                            # never derived: they need a recorded human decision (none exists yet)
ASSET_CLASSES = ("US_EQUITY",)
DIRECTIONS = ("LONG", "SHORT", "LONG_SHORT")                           # a strategy's direction_capability; a signal carries LONG or SHORT
SIGNAL_DIRECTIONS = ("LONG", "SHORT")
TIME_HORIZONS = ("INTRADAY", "SWING", "POSITION")                      # SWING = a few sessions (the implemented strategy holds at most 5)
RISK_CLASSES = ("LOW", "MODERATE", "HIGH", "UNCLASSIFIED")
SCORE_KINDS = ("RANK", "CALIBRATED_PROBABILITY")                        # score is NOT a probability unless explicitly calibrated
SCORE_MIN, SCORE_MAX = 0.0, 100.0

SECTIONS = (("overview", "Overview"), ("hypothesis", "Hypothesis"), ("rules", "Rules"), ("data_requirements", "Data requirements"), ("entry_logic", "Entry logic"),
            ("invalidation", "Invalidation"), ("exit_logic", "Exit logic"), ("holding_horizon", "Holding horizon"), ("backtest", "Backtest"),
            ("out_of_sample", "Out-of-sample"), ("risk_notes", "Risk notes"), ("paper_results", "Paper results"), ("decision_log", "Decision log"))
SECTION_STATES = ("DOCUMENTED", "LIVE", "NOT_EVALUATED", "UNAVAILABLE")   # from code / docs; from persisted records; nothing exists; the process cannot read it right now
NOT_EVALUATED = "Not yet evaluated"
UNAVAILABLE = "Unavailable in this process"

PIPELINE = ("Strategy", "Opportunity / Proposal", "Portfolio", "Risk / Safety", "Execution", "Broker")
QUANTCONNECT_PIPELINE = ("QuantConnect / LEAN", "strategy research / backtest", "normalized strategy signal (Strategy Standard v1)", "Stock Agent Strategy Adapter",
                         "Opportunity Ranking", "Portfolio / Risk / Safety", "Alpaca Paper")
# a signal may never carry an execution instruction: these keys are rejected wherever they appear (top level or inside `evidence`)
FORBIDDEN_SIGNAL_KEYS = frozenset({"order", "orders", "order_type", "qty", "quantity", "shares", "notional", "broker", "broker_order_id", "client_order_id", "submit",
                                   "submitted", "execute", "execution", "side", "limit_price", "stop_price", "time_in_force", "account", "account_id", "buy", "sell",
                                   "place_order", "cancel", "leverage"})
DISCLAIMER = ("Strategies produce evidence and signals; no strategy executes. Every entry passes the Stage 5.9 proposal, the Stage 5.9.1 safety gate and the "
              "Stage 6.0 execution controls before the Alpaca PAPER broker. A score is a deterministic ranking value, not a probability, unless it is explicitly "
              "calibrated. Nothing here is a recommendation.")
