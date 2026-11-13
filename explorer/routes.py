"""
explorer/routes.py - Market Explorer, Phase A0 (read only). GET routes only; no POST, PUT, PATCH, DELETE and no WebSocket exists.

Phase A0 defines the route namespace and does NOT mount it: api/server.py is unchanged and the running server has no /api/explorer route.
A later phase mounts `router` with one reviewed line; until then the routes are reachable only from a test application.

GET /api/explorer/status            the flag, the version and the read-only statement - no market call
GET /api/explorer/symbols/{symbol}  quote + minute candles of one symbol, for a chart (market data only)
GET /api/explorer/search?q=         Phase A0: an exact-ticker lookup against the data feed (no directory yet)

While MARKET_EXPLORER_ENABLED is not set (the default), the two data routes answer 404 EXPLORER_DISABLED and read nothing (a query parameter
that is invalid or out of range is rejected with 422 before that check). Malformed input is 400 INVALID_SYMBOL; on /symbols a symbol the feed has no data for
is 404 UNKNOWN_SYMBOL, while /search answers an empty result; a feed that does not answer is 503 on both.

Nothing here reaches the Trader View stream or its socket subscription, a setup, a proposal, the executor, the safety state, the watchlist,
a broker or the database, and nothing here imports the server (explorer/__init__.py; pinned by tests/test_market_explorer_a0.py).
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

import explorer
from api.validation import validate_symbol
from explorer.service import MarketUnavailable, UnknownSymbol, service

router = APIRouter(prefix="/api/explorer", tags=["explorer"])


def _err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": code, "message": message[:200]}, status_code=status)


def _disabled() -> JSONResponse:
    return _err("EXPLORER_DISABLED", f"The Market Explorer is off ({explorer.FLAG} is not set).", 404)


def _read(raw: str, fn) -> JSONResponse:
    """Validate the ticker on its own (only a malformed ticker is a 400), then read. `fn` receives the normalised symbol."""
    try:
        s = validate_symbol(str(raw or ""))
    except ValueError as exc:
        return _err("INVALID_SYMBOL", str(exc), 400)
    try:
        return JSONResponse(fn(s))
    except explorer.ExplorerDisabled:
        return _disabled()
    except UnknownSymbol as exc:
        return _err("UNKNOWN_SYMBOL", str(exc), 404)
    except MarketUnavailable as exc:
        return _err("MARKET_UNAVAILABLE", str(exc), 503)


@router.get("/status")
def get_status() -> JSONResponse:
    return JSONResponse({"version": explorer.VERSION, "enabled": explorer.enabled(), "flag": explorer.FLAG, "read_only": True, "note": explorer.READ_ONLY_NOTE})


@router.get("/symbols/{symbol}")
def get_symbol(symbol: str, candles: int = Query(default=explorer.DEFAULT_CANDLES, ge=0, le=explorer.MAX_CANDLES)) -> JSONResponse:
    if not explorer.enabled():
        return _disabled()
    return _read(symbol, lambda s: service().symbol(s, candles))


@router.get("/search")
def get_search(q: str = Query(default="", max_length=32)) -> JSONResponse:
    if not explorer.enabled():
        return _disabled()
    return _read(q, lambda s: service().search(s))
