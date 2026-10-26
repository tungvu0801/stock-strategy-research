"""
api/routes/account_refresh.py - Stage 6.0.1 CONTINUOUS ACCOUNT REFRESH status (GET only; nothing here refreshes, places or cancels anything).

GET /api/account-refresh            freshness (status, fresh, age, last success / attempt, last error, counts), loop state, a snapshot summary
GET /api/account-refresh/history    the append-only refresh attempts (newest first)
GET /api/account-refresh/positions  the refreshed account positions, each marked bot-managed or external, with source and freshness (0 broker calls)

The path deliberately avoids the words "alpaca" / "paper" / "broker" (the Stage 4.5 / 4.6A route pins cover every such route).
No credential, auth header or account UUID is ever returned; the account number is masked.
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from account_refresh import VERSION

router = APIRouter(prefix="/api/account-refresh", tags=["account-refresh"])


def _service():
    from api.routes.trader_view import stream
    return stream().account_refresh


@router.get("")
def get_status() -> JSONResponse:
    return JSONResponse(_service().overview())


@router.get("/history")
def get_history(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    svc = _service()
    return JSONResponse({"attempts": svc.store.attempts(limit), "total": svc.store.attempt_count(), "version": VERSION})


@router.get("/positions")
def get_positions() -> JSONResponse:
    from api.routes.trader_view import stream
    from execution.account_positions import view
    s = stream()
    return JSONResponse(view(s.account_refresh, s.execution.store))
