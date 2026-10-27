"""
api/routes/unified_portfolio.py - Stage 6.9 UNIFIED PORTFOLIO (GET ONLY: there is no POST / PUT / PATCH / DELETE route here).

GET /api/unified-portfolio            UnifiedPortfolioState: Alpaca PAPER rows (bot-managed / external) + Robinhood REAL rows (user-managed),
                                      exposure, concentration, PAPER vs REAL equity kept apart, informational warnings, advisory entry policy
GET /api/unified-portfolio/robinhood  the Robinhood READ-ONLY source: state, freshness, user-managed holdings
Stage 6.9.1: the unified payload adds `entry_gate` - the UNIFIED_EXTERNAL_EXPOSURE gate's settings and, per READY ticker, its decision now (display
only); the same snapshot (without that pre-check) is the executor's gate input through TerminalStream.unified_portfolio.

Robinhood is read through the loopback rh_gateway (two GETs, at most once per ROBINHOOD_PORTFOLIO_TTL_SECONDS); Alpaca PAPER positions come
from the account-refresh snapshot (0 broker calls). Nothing here places, changes, cancels or closes an order or a position. Only normalized
portfolio data is returned: no credential, token, cookie, session id or full account number.
"""
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/unified-portfolio", tags=["unified-portfolio"])


def _alpaca(s) -> tuple:
    from execution.account_positions import view as account_view
    av = account_view(s.account_refresh, s.execution.store)
    snap = s.account_refresh.store.snapshot() if s.account_refresh.enabled() else None
    acct = (snap or {}).get("account") or {}
    return av, ({"equity": acct.get("equity"), "cash": acct.get("cash")} if snap else None)


def _ready(s) -> list:
    try:
        return [e.symbol for e in s.setups.store.open_episodes() if e.state == "READY"]
    except Exception:  # noqa: BLE001 - the advisory policy is display only
        return []


def _entry_gate(s, ready: list, u: dict) -> dict:
    """Stage 6.9.1: the UNIFIED_EXTERNAL_EXPOSURE entry gate - its settings and, per READY ticker, what it decides NOW from this payload and the
    current proposal (display only: the executor enforces it in Paper Auto cycles, and only when UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true)."""
    import config
    from execution import exposure_gate
    g = exposure_gate.summary(exposure_gate.from_config(config))
    try:
        g["symbols"] = s.execution.exposure_precheck(ready, u)
    except Exception as exc:  # noqa: BLE001 - display only
        g["symbols"], g["error"] = {}, type(exc).__name__
    return g


def unified_payload(refresh: bool = True, stream_obj: Optional[object] = None, precheck: bool = True) -> dict:
    from api.routes.trader_view import stream
    from portfolio.sectors import default_resolver
    from unified_portfolio.robinhood import get_source
    from unified_portfolio.state import build
    s = stream_obj or stream()
    av, acct = _alpaca(s)
    ready = _ready(s)
    out = build(av, get_source().view(refresh=refresh), alpaca_account=acct, sector_fn=default_resolver.sector_for, ready_symbols=ready)
    if precheck:                                                                       # Stage 6.9.1: the executor's own read (TerminalStream.unified_portfolio) skips it
        out["entry_gate"] = _entry_gate(s, ready, out)
    return out


@router.get("")
def get_unified() -> JSONResponse:
    return JSONResponse(unified_payload(refresh=True))


@router.get("/robinhood")
def get_robinhood() -> JSONResponse:
    from unified_portfolio.robinhood import get_source
    return JSONResponse(get_source().view(refresh=True))
