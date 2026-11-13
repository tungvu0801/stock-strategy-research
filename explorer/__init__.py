"""
explorer/ - Market Explorer, Phase A0: the read-only boundary (project-planning TASK_AM_MARKET_EXPLORER_AUDIT_2026-11-12.md, section 3).

Looking at a symbol through the explorer is NOT analysing it and NOT trading it. This package:

  * reads market data only (quotes and minute candles) through its OWN market-data adapter instance;
  * never touches the Trader View stream, its hub or its socket subscription (which evaluates the setup and the proposal of an opened ticker);
  * reaches no service, store, engine or scheduler of the setup, proposal, execution, safety, auto-run, paper-order or account code, and nothing
    that reads or writes watchlist.txt (the strategy universe). The only strategy-package modules in its import closure are the constants and
    pydantic view models that the market-data models themselves import (setups, setups.models, proposals, proposals.models);
  * has no database, writes no file and offers GET routes only (explorer/routes.py);
  * is OFF unless MARKET_EXPLORER_ENABLED is set: while OFF the data routes answer 404 EXPLORER_DISABLED (a query parameter that is invalid or
    out of range is rejected with 422 first), the service refuses, and nothing is read;
  * is not mounted in the server in Phase A0: no existing file changes, and the running application has no /api/explorer route yet. Setting the
    flag therefore does nothing in the running server until a later phase mounts the routes.

tests/test_market_explorer_a0.py pins all of this, including the dependency boundary (the exact set of project modules this package can reach).
Phase A0 has no symbol directory and no page: "search" is an exact-ticker lookup against the data feed.

Before the routes are mounted (a later phase), two things are still owed and are NOT in Phase A0: a bound on the adapter's per-symbol caches and
a request budget of the explorer's own, so that browsing cannot crowd the market-data plan the strategy's readers share.
"""
import os

VERSION = "explorer_a0"
FLAG = "MARKET_EXPLORER_ENABLED"
DEFAULT_CANDLES = 240
MAX_CANDLES = 2000
READ_ONLY_NOTE = ("Read only: market data for display. Opening or searching a symbol here does not evaluate a setup, create a proposal, "
                  "change the strategy universe, or place, prepare or approve an order.")


class ExplorerDisabled(RuntimeError):
    """The feature flag is not set: the explorer reads nothing."""


def enabled() -> bool:
    """The feature flag, read at call time (default OFF). Only the literal values 1 / true / yes / on switch the explorer on."""
    return os.getenv(FLAG, "false").strip().lower() in ("1", "true", "yes", "on")
