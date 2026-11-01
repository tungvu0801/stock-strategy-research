"""
market_scan/ - Stage 7.5: the SHARED MARKET SCANNER v1, offline core (DESIGN_75_MARKET_SCAN.md; master design D.1).

  features.py  the pre-registered feature maths, pure: gap %, premarket volume and range, time-of-day relative volume (same feed both
               sides), intraday move (% and ATR units), ATR(14), unusual volatility, sector strength, dollar volume, spread
  flags.py     halt, catalyst, earnings proximity and regime, pure, each with its own "not established" state
  filters.py   the Stage 7.4 quality gate, the shared eligibility filters per horizon (INTRADAY / SWING) and the gap-list order
  run.py       one offline scan as of an explicit time T over byte copies of the store: the run record, one result per symbol, a
               reproducible scan hash; `python -m market_scan.run`

Research only. A result is a research CANDIDATE, never a buy / sell recommendation, a signal of a strategy or an order: it carries no
order, quantity, side, broker or account field (the Strategy Standard's forbidden keys are checked over the whole document), no score,
and it is ranked only inside its own horizon. Nothing in the live path imports this package; nothing here fetches, writes a store,
starts a thread or reads a broker, Robinhood or a model.
"""
VERSION = "market_scan_v1"
STAGE = "7.5"
PRE_REGISTRATION = "market_scan_features_v1"         # the formulas and constants below and in DESIGN_75 section 4: frozen once merged

INTRADAY, SWING = "INTRADAY", "SWING"                # the Strategy Standard's time_horizon values; the scanner never ranks across them
HORIZONS = (INTRADAY, SWING)

# run states
COMPLETED, BLOCKED, INSUFFICIENT_DATA = "COMPLETED", "BLOCKED", "INSUFFICIENT_DATA"
# per-horizon states of one symbol
ELIGIBLE, INELIGIBLE, NOT_APPLICABLE = "ELIGIBLE", "INELIGIBLE", "NOT_APPLICABLE"
# flag values: a flag that cannot be established says so; it is never read as its favourable value
TRUE, FALSE, UNKNOWN, NOT_ESTABLISHED = "TRUE", "FALSE", "UNKNOWN", "NOT_ESTABLISHED"

# universe roles (Tier A, master design D.1)
CORE, WATCHLIST, SECTOR_ETF, INDEX_PROXY = "CORE", "WATCHLIST", "SECTOR_ETF", "INDEX_PROXY"
INDEX_PROXIES = ("SPY", "QQQ", "IWM")
MARKET_PROXY = "SPY"                                 # sector strength and the regime are measured against it

# pre-registered constants: every one is the master design's (D.1 / C.5 / C.11); none is tuned
MIN_PRICE = 5.0                                      # D.1 eligibility: price >= $5, never looser than config.MIN_PRICE (pinned equal)
RVOL_PRIOR_SESSIONS = 10                             # relative_volume_tod: the median of the prior 10 sessions
ATR_SESSIONS = 14                                    # ATR(14) daily
DOLLAR_VOLUME_SESSIONS = 20                          # dollar_volume_20d: 20 completed sessions
SPREAD_SNAPSHOTS = 5                                 # spread_bps: the median of the last 5 snapshots
HALT_RESUMPTION_S = 600                              # halt_flag: a resumption less than 10 minutes ago
CATALYST_WINDOW_S = 18 * 3600                        # catalyst_flag: a PRIMARY news mention in the last 18 h
REGIME_TREND_SESSIONS = 200                          # rotation_walkforward.regimes: SMA200 trend (its own constant, pinned equal)
REGIME_VOL_SESSIONS = 21                             # and 20-session realised volatility (21 closes)
CURVE_SERIES = "T10Y2Y"                              # the macro flag: curve inversion is T10Y2Y < 0 (C.11)
FEEDS = ("iex", "sip")
DEFAULT_FEED = "iex"                                 # the assumed Basic plan; every price / volume feature reads ONE feed
BAR_ADJUSTMENT = "raw"                               # raw bars only: raw and adjusted series are never mixed in one ratio (C.6)

# what the master design's Stage 7.5 names but this offline core does not build (BLOCKED_BY_DEFERRED_7_3_WIRING): each needs the Stage 7.3
# daemon / router / config wiring, which the operator deferred
DEFERRED = "BLOCKED_BY_DEFERRED_7_3_WIRING"
BLOCKED_BY_DEFERRED_7_3_WIRING = (
    "MARKET_SCAN_MODE (OFF | SHADOW): declared by Stage 7.3's config.py marked block, which does not exist",
    "the cadence: PRE every 5 min, REG every 60 s with shortlist minute bars, POST at 16:20 and 18:00 ET (a scheduler is the daemon's)",
    "persisting a run as one mi_scan_runs row plus one mi_scan_results row per symbol (no schema exists; this core returns the same "
    "content as a JSON document and writes nothing)",
    "the edits to 7.3's own daemon / router files (they do not exist)",
)
NOT_PRE_REGISTERED = ("the VIX percentile of the regime's macro flags: the master design names it without a window or a threshold",)
SEC_BLOCKED = "SEC_ACCEPTANCE_TZ_UNVERIFIED"         # an 8-K or an earnings release (SEC acceptance times) never counts: the catalyst
                                                     # flag is TRUE or NOT_ESTABLISHED, never FALSE

DISCLAIMER = ("Research candidates only: not buy or sell recommendations, not strategy signals and not orders. Nothing is executed, "
              "proposed or scheduled. Fixture and offline-verification data are not production observations, and a quality verdict is "
              "not proof of live-data readiness.")
