"""
api/routes/setups.py — Stage 5.8 SETUP STATE (read only) + the existing alert read-acknowledgement pattern.

GET  /api/setups                      open episodes (current view per symbol) + control liveness
GET  /api/setups/events               transitions, newest first
GET  /api/setups/evaluations          evaluations (TRANSITION / HOLD / PAUSED / ERROR), newest first — "when did it last fire?"
GET  /api/setups/alerts               persisted alerts (unread_only, limit)
POST /api/setups/alerts/read-all      acknowledgement only (the Stage 4.1 pattern)
POST /api/setups/alerts/{alert_id}/read
GET  /api/setups/controls             control liveness rows + scheduler status
GET  /api/setups/{symbol}             the current stored view for one ticker (never evaluates)

Nothing here evaluates, trades, orders or previews; READY is READY FOR REVIEW. Symbols are validated like the terminal routes.
"""
import re
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.validation import validate_symbol
from setups import DISCLAIMER, SETUP_NAME, SETUP_TYPE, STATE_LABELS, VERSION
from setups.store import SetupStore

router = APIRouter(prefix="/api/setups", tags=["setups"])
_ID = re.compile(r"^[0-9a-f]{32}$")


class EmptyBody(BaseModel):
    pass


def _service():
    from api.routes.trader_view import stream
    return stream().setups


def _store() -> SetupStore:
    return _service().store                                   # the same store the evaluator writes (tests substitute the stream)


@router.get("")
def list_setups() -> JSONResponse:
    svc = _service()
    out = svc.overview()
    out.update({"setup_name": SETUP_NAME, "states": STATE_LABELS, "note": "READY means READY FOR REVIEW — a deterministic review threshold, not an entry signal."})
    return JSONResponse(out)


@router.get("/events")
def list_events(symbol: Optional[str] = Query(default=None, max_length=12), limit: int = Query(default=50, ge=1, le=200)) -> JSONResponse:
    s = None
    if symbol:
        try:
            s = validate_symbol(symbol)
        except ValueError as exc:
            return JSONResponse({"status": "INVALID_SYMBOL", "message": str(exc)}, status_code=400)
    return JSONResponse({"transitions": _store().transitions(s, limit), "version": VERSION})


@router.get("/evaluations")
def list_evaluations(symbol: Optional[str] = Query(default=None, max_length=12), limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    s = None
    if symbol:
        try:
            s = validate_symbol(symbol)
        except ValueError as exc:
            return JSONResponse({"status": "INVALID_SYMBOL", "message": str(exc)}, status_code=400)
    return JSONResponse({"evaluations": _store().evaluations(s, limit), "version": VERSION})


@router.get("/alerts")
def list_alerts(unread_only: bool = False, limit: int = Query(default=30, ge=1, le=200)) -> JSONResponse:
    st = _store()
    return JSONResponse({"alerts": st.alerts(unread_only=unread_only, limit=limit), "unread": st.unread(), "disclaimer": DISCLAIMER})


@router.post("/alerts/read-all")
def read_all(body: Optional[EmptyBody] = None) -> JSONResponse:
    st = _store()
    n = st.mark_read(None)
    return JSONResponse({"marked": n, "unread": st.unread()})


@router.post("/alerts/{alert_id}/read")
def read_one(alert_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _ID.match(alert_id):
        return JSONResponse({"status": "NOT_FOUND", "message": "No alert with that id."}, status_code=404)
    st = _store()
    n = st.mark_read(alert_id)
    return JSONResponse({"marked": n, "unread": st.unread()})


@router.get("/controls")
def list_controls() -> JSONResponse:
    from setups.scheduler import status as scheduler_status
    return JSONResponse({"controls": _store().controls(), "scheduler": scheduler_status(), "setup_type": SETUP_TYPE, "version": VERSION})


@router.get("/{symbol}")
def get_setup(symbol: str) -> JSONResponse:
    try:
        s = validate_symbol(symbol)
    except ValueError as exc:
        return JSONResponse({"status": "INVALID_SYMBOL", "message": str(exc)}, status_code=400)
    return JSONResponse(_service().current(s).model_dump())
