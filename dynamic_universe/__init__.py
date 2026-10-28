"""
dynamic_universe/ - Stage 6.10C INACTIVE DYNAMIC UNIVERSE (DESIGN_610C_DECISION_FLOW.md section 8): the two-layer universe, prepared and not
activated.

  core.py     Core universe v1: a frozen list of 79 liquid US stocks (symbol, sector) built once from the data/sector_map.py peers plus the
              live symbols the map lacks, pinned by sha256; DYNAMIC_UNIVERSE_CORE_FILE may replace it (validated, never an empty core)
  signals.py  the cheap scanner's six DECLARED signal specs (inputs, expected request cost): none is implemented, nothing is computed
  plan.py     the settings (OFF | SHADOW), the read-only universe status and pipeline plan, and select_active(), the pure future selection rule

Nothing in the live path imports this package: the setup evaluator, Trader View, the executor and the scanner keep reading watchlist.txt, so
the active universe is exactly the live watchlist. SHADOW only describes the plan on demand (no thread, no request, no write). ACTIVE does not
exist in Stage 6.10C: it reads as OFF and is reported as a problem.
"""
VERSION = "dynamic_universe_v1"
STAGE = "6.10C"

OFF, SHADOW = "OFF", "SHADOW"
MODES = (OFF, SHADOW)                   # ACTIVE is deliberately absent: activation is a later stage
ACTIVE_MAX_DEFAULT = 25                 # DYNAMIC_UNIVERSE_ACTIVE_MAX default (the future active-set bound)
ACTIVE_MAX_LIMIT = 500                  # largest accepted bound (= the core file cap)
UNMAPPED = "Unmapped"                   # sector tag of a symbol the repo does not classify (never guessed)
LIVE_SOURCE = "watchlist.txt"           # the live universe: config.WATCHLIST_FILE, read fresh by the evaluator every cycle

NOTE = ("Inactive. The setup evaluator, Trader View and the paper executor still use the live watchlist (watchlist.txt) unchanged; the core "
        "universe and the scanner's declared signals feed no live path. Activation is a later stage.")
