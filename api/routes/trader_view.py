"""
api/routes/trader_view.py — Stage 5.5 TRADER VIEW (read only). GET routes plus ONE WebSocket; no POST, PUT, PATCH or DELETE exists.

GET /api/terminal/config            mode (simulated | real), source, cadences, chart asset, PPE contract, actions — no market call
GET /api/terminal/symbols           the existing watchlist (watchlist.txt) + demo symbols — no market call
GET /api/terminal/state/{symbol}    terminal state: quote, candles, position per source, latest stored evidence, PPE slot, tradability
GET /api/terminal/candles/{symbol}  minute candles (history)
GET /api/terminal/ppe/{symbol}      Stage 5.6 PPE Stretch / Confidence (deterministic; stored evidence only; no recommendation)
GET /api/terminal/demand/{symbol}   Stage 5.7 Market Demand (deterministic buyer confirmation from bars; separate from PPE; no recommendation)
WS  /api/terminal/stream            hello · subscribe / unsubscribe / ping / refresh · quote · candle · watchlist · position · evidence

Symbols are validated and normalised (api.validation.validate_symbol); malformed input is 400 INVALID_SYMBOL, a symbol the feed has no
data for is 404 UNKNOWN_SYMBOL. Nothing here writes the database, the watchlist, a broker or a model.
"""
from fastapi import APIRouter, Query, WebSocket
from fastapi.responses import JSONResponse

from api.validation import validate_symbol
from trader_view.adapters import MarketUnavailable, UnknownSymbol
from trader_view.stream import TerminalStream, build_stream

router = APIRouter(prefix="/api/terminal", tags=["trader-view"])   # "terminal": no route path may contain "trade" (tests/test_regression_isolation.py)
STREAM = {"instance": None, "factory": build_stream}        # tests substitute an instance built on fakes


def stream() -> TerminalStream:
    if STREAM["instance"] is None:
        STREAM["instance"] = STREAM["factory"]()
    return STREAM["instance"]


def _err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _resolved(symbol: str):
    try:
        s = validate_symbol(symbol)
    except ValueError as exc:
        return None, _err("INVALID_SYMBOL", str(exc), 400)
    try:
        return stream().resolve(s), None
    except UnknownSymbol as exc:
        return None, _err("UNKNOWN_SYMBOL", str(exc), 404)
    except MarketUnavailable as exc:
        return None, _err("MARKET_UNAVAILABLE", str(exc), 503)


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse(stream().config_payload())


@router.get("/symbols")
def get_symbols() -> JSONResponse:
    return JSONResponse(stream().symbols())


@router.get("/state/{symbol}")
def get_state(symbol: str, candles: int = Query(default=240, ge=0, le=2000)) -> JSONResponse:
    s, err = _resolved(symbol)
    if err is not None:
        return err
    d = stream().state(s, with_candles=candles > 0).model_dump()
    d["candles"] = d["candles"][-candles:] if candles else []
    return JSONResponse(d)


@router.get("/candles/{symbol}")
def get_candles(symbol: str, limit: int = Query(default=240, ge=1, le=2000)) -> JSONResponse:
    s, err = _resolved(symbol)
    if err is not None:
        return err
    st = stream()
    return JSONResponse({"symbol": s, "candles": [c.model_dump() for c in st.candles(s, limit)], "source": st.market.source, "mode": st.market.mode})


@router.get("/ppe/{symbol}")
def get_ppe(symbol: str) -> JSONResponse:
    s, err = _resolved(symbol)
    if err is not None:
        return err
    return JSONResponse(stream().ppe_result(s).model_dump())


@router.get("/demand/{symbol}")
def get_demand(symbol: str) -> JSONResponse:
    """Stage 5.7 Market Demand (deterministic buyer-confirmation score; bars only; separate from PPE; no recommendation). GET only."""
    s, err = _resolved(symbol)
    if err is not None:
        return err
    return JSONResponse(stream().demand_result(s).model_dump())


@router.websocket("/stream")
async def ws_stream(websocket: WebSocket) -> None:
    await stream().handle(websocket)
