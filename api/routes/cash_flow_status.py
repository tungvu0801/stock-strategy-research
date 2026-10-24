"""
api/routes/cash_flow_status.py - Stage 6.0.2 CASH-FLOW-ADJUSTED DAILY P&L status (GET only; nothing here reads the broker, refreshes, places
or cancels anything - it reports the persisted ledger and the gate's latest evaluation).

GET /api/cash-flow-status            status, last refresh, today's event count, net external cash flow, adjusted starting equity,
                                     adjusted daily P&L, adjusted daily loss %, the events since the baseline (no broker ids, no free text)
GET /api/cash-flow-status/events     the append-only ledger (newest first; latest and earlier revisions)
GET /api/cash-flow-status/attempts   the append-only activity reads (newest first)

The path deliberately avoids the words "alpaca" / "paper" / "broker" (the Stage 4.5 / 4.6A route pins cover every such route).
No credential, auth header, account identifier or broker activity id is ever returned.
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from cash_flow import VERSION

router = APIRouter(prefix="/api/cash-flow-status", tags=["cash-flow"])


def _stream():
    from api.routes.trader_view import stream
    return stream()


@router.get("")
def get_status() -> JSONResponse:
    st = _stream()
    try:
        view = st.safety.current()
    except Exception:  # noqa: BLE001 - the status is still useful without the gate's numbers
        view = None
    return JSONResponse(st.cash_flow.overview(view))


@router.get("/events")
def get_events(limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return JSONResponse(_stream().cash_flow.events_view(limit))


@router.get("/attempts")
def get_attempts(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    svc = _stream().cash_flow
    rows = [{k: v for k, v in a.items() if k != "refresh_id"} for a in svc.store.attempts(limit)]
    return JSONResponse({"attempts": rows, "total": svc.store.attempt_count(), "version": VERSION})
