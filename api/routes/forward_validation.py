"""
api/routes/forward_validation.py - Stage 6.1 FORWARD PAPER VALIDATION (GET only; nothing here places, cancels, refreshes, tunes or changes
anything - it reports the observer's records and the descriptive statistics of the persisted events).

GET /api/forward-validation                         cumulative operational + strategy summary, attention, targets, today's session, loop state
GET /api/forward-validation/sessions                the session records (newest first)
GET /api/forward-validation/sessions/{date}         one session + its daily summary (persisted, else computed live)
GET /api/forward-validation/closed                  closed paper trades with slippage, P/L, sampled MFE / MAE, exit reason, attribution
GET /api/forward-validation/closed/{position_id}    the end-to-end audit trail of one trade
GET /api/forward-validation/daily                   the persisted daily summaries (newest first)
GET /api/forward-validation/daily/{date}            one daily summary (persisted, else computed live)
GET /api/forward-validation/report/daily[/{date}]   the deterministic daily report (text/plain)
GET /api/forward-validation/report/weekly           the deterministic weekly aggregate (text/plain)
GET /api/forward-validation/export/{kind}           sessions | trades | daily | control_events | samples | restarts as CSV (default) or JSON, and the Stage 6.10A
                                                    forward dataset: candidates | decisions | controls | session_health | entry_snapshots | outcomes |
                                                    config_snapshots | cohorts | context | faults | autorun_events | autorun_preflights | autorun_days
GET /api/forward-validation/restarts, /events, /samples, /funnel[/{date}], /frozen-controls

Route words: no path may contain "trade" (tests/test_regression_isolation.py), "paper" (Stage 4.5) or "alpaca" / "broker" (Stage 4.6A), so the
closed-trade routes are called /closed. No credential, auth header or account identifier is ever returned.
"""
import re

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from forward_capture import EXPORT_KINDS as CAPTURE_EXPORTS   # Stage 6.10A: read-only exports of the forward dataset (no new route)
from forward_validation import EXPORTS, VERSION, frozen_controls, frozen_hash
from forward_validation import reports as REP

router = APIRouter(prefix="/api/forward-validation", tags=["forward-validation"])
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _service():
    from api.routes.trader_view import stream
    return stream().forward_validation


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "INVALID", "message": message}, status_code=status)


@router.get("")
def get_overview() -> JSONResponse:
    return JSONResponse(_service().overview())


@router.get("/sessions")
def get_sessions(limit: int = Query(default=60, ge=1, le=1000)) -> JSONResponse:
    svc = _service()
    return JSONResponse({"version": VERSION, "sessions": svc.store.sessions(limit), "total": svc.store.counts()["forward_validation_sessions"]})


@router.get("/sessions/{trading_date}")
def get_session(trading_date: str) -> JSONResponse:
    if not _DATE.match(trading_date):
        return _bad("trading_date must be YYYY-MM-DD")
    svc = _service()
    row = svc.store.session(trading_date)
    if row is None:
        return _bad("no session record for that trading date", 404)
    return JSONResponse({"version": VERSION, "session": row, "daily": svc.store.daily(trading_date) or svc.daily_summary(trading_date), "restarts": svc.restarts_view(trading_date),
                         "samples": len(svc.store.samples(trading_date))})


@router.get("/closed")
def get_closed(limit: int = Query(default=200, ge=1, le=5000), date: str = Query(default="")) -> JSONResponse:
    if date and not _DATE.match(date):
        return _bad("date must be YYYY-MM-DD")
    svc = _service()
    rows = svc.closed_trades(limit, date or None)
    return JSONResponse({"version": VERSION, "closed": rows, "count": len(rows), "note": "closed paper positions of the Alpaca PAPER account; descriptive, no recommendation"})


@router.get("/closed/{position_id}")
def get_trace(position_id: str) -> JSONResponse:
    if not re.match(r"^[0-9a-f]{8,64}$", position_id):
        return _bad("position_id must be a hex id")
    tr = _service().trace(position_id)
    if tr is None:
        return _bad("no paper position with that id", 404)
    return JSONResponse({"version": VERSION, **tr})


@router.get("/daily")
def get_dailies(limit: int = Query(default=60, ge=1, le=1000)) -> JSONResponse:
    svc = _service()
    return JSONResponse({"version": VERSION, "daily": svc.store.dailies(limit)})


@router.get("/daily/{trading_date}")
def get_daily(trading_date: str) -> JSONResponse:
    if not _DATE.match(trading_date):
        return _bad("trading_date must be YYYY-MM-DD")
    svc = _service()
    return JSONResponse(svc.store.daily(trading_date) or svc.daily_summary(trading_date))


@router.get("/report/daily", response_class=PlainTextResponse)
def get_daily_report_latest() -> PlainTextResponse:
    svc = _service()
    rows = svc.store.sessions(limit=1)
    if not rows:
        return PlainTextResponse("FORWARD PAPER VALIDATION - DAILY REPORT\nNo session has been recorded yet.\n")
    d = rows[0]["trading_date"]
    return PlainTextResponse(REP.daily_report(svc.store.daily(d) or svc.daily_summary(d)))


@router.get("/report/daily/{trading_date}", response_class=PlainTextResponse)
def get_daily_report(trading_date: str) -> PlainTextResponse:
    if not _DATE.match(trading_date):
        return PlainTextResponse("trading_date must be YYYY-MM-DD\n", status_code=400)
    svc = _service()
    return PlainTextResponse(REP.daily_report(svc.store.daily(trading_date) or svc.daily_summary(trading_date)))


@router.get("/report/weekly", response_class=PlainTextResponse)
def get_weekly_report() -> PlainTextResponse:
    svc = _service()
    return PlainTextResponse(REP.weekly_report(svc.cumulative(), svc.store.dailies(limit=100000, newest_first=False)))


@router.get("/export/{kind}")
def get_export(kind: str, format: str = Query(default="csv")):
    if kind not in EXPORTS and kind not in CAPTURE_EXPORTS:
        return _bad(f"kind must be one of {', '.join(EXPORTS + CAPTURE_EXPORTS)}")
    if format not in ("csv", "json"):
        return _bad("format must be csv or json")
    cols, rows = REP.export_rows(_service(), kind)
    if format == "json":
        return PlainTextResponse(REP.to_json(kind, cols, rows), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="forward_validation_{kind}.json"'})
    return PlainTextResponse(REP.to_csv(cols, rows), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="forward_validation_{kind}.csv"'})


@router.get("/restarts")
def get_restarts() -> JSONResponse:
    return JSONResponse({"version": VERSION, **_service().restarts_view()})


@router.get("/events")
def get_events(limit: int = Query(default=100, ge=1, le=5000), kind: str = Query(default="")) -> JSONResponse:
    svc = _service()
    return JSONResponse({"version": VERSION, "events": svc.store.events(limit, kind or None)})


@router.get("/samples")
def get_samples(date: str = Query(default=""), limit: int = Query(default=500, ge=1, le=20000)) -> JSONResponse:
    if date and not _DATE.match(date):
        return _bad("date must be YYYY-MM-DD")
    svc = _service()
    return JSONResponse({"version": VERSION, "samples": svc.store.samples(date or None, limit)})


@router.get("/funnel")
def get_funnel_all() -> JSONResponse:
    return JSONResponse({"version": VERSION, **_service().funnel(None)})


@router.get("/funnel/{trading_date}")
def get_funnel(trading_date: str) -> JSONResponse:
    if not _DATE.match(trading_date):
        return _bad("trading_date must be YYYY-MM-DD")
    return JSONResponse({"version": VERSION, **_service().funnel(trading_date)})


@router.get("/frozen-controls")
def get_frozen_controls() -> JSONResponse:
    return JSONResponse({"version": VERSION, "hash": frozen_hash(), "controls": frozen_controls(), "note": "the strategy controls Stage 6.1 observes and never tunes"})
