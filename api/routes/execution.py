"""
api/routes/execution.py — Stage 6.0 AUTONOMOUS PAPER EXECUTION (reads + the two mode actions).

GET  /api/execution                 overview: mode, environment proof, bot headline, cards, controls, findings, runs, intents, positions
                                    (+ Stage 6.9.1 entry_controls: the entry-controls version and the UNIFIED_EXTERNAL_EXPOSURE gate settings)
GET  /api/execution/status          the compact beginner view (bot headline + symbol cards + recent paper trades)
GET  /api/execution/history         closed paper trades and entries that did not fill
GET  /api/execution/controls        control liveness rows
GET  /api/execution/intents         intents (newest first)
GET  /api/execution/positions       internal paper positions
GET  /api/execution/findings        reconciliation findings
GET  /api/execution/symbol/{symbol} one symbol card
GET  /api/execution/activity        the Bot Activity view: worker heartbeat, cycle progress / summary, per-ticker results, risk, broker-reported orders, history
POST /api/execution/auto/enable     mode -> PAPER_AUTO (refused unless the PAPER environment is proven and reconciliation is healthy)
POST /api/execution/auto/disable    mode -> MONITOR_ONLY (monitoring and exits of existing paper positions continue)

The POSTs change the persisted mode only. Nothing here submits an order: the executor loop does, by its own deterministic rules,
on the Alpaca PAPER account only.
"""
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.validation import validate_symbol
from execution import CONTROLS, DISCLAIMER, EXIT_REASONS, EXIT_TEXT, MODE_LABELS, MODES, VERSION
from execution.service import ExecutionRefused

router = APIRouter(prefix="/api/execution", tags=["execution"])


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=200)


def _service():
    from api.routes.trader_view import stream
    return stream().execution


@router.get("")
def get_overview() -> JSONResponse:
    out = _service().overview()
    out.update({"modes": list(MODES), "mode_labels": MODE_LABELS, "exit_reasons": {r: EXIT_TEXT[r] for r in EXIT_REASONS}, "known_controls": list(CONTROLS)})
    import config
    from execution import exposure_gate
    out["entry_controls"] = exposure_gate.summary(exposure_gate.from_config(config))   # Stage 6.9.1: the entry-controls version a new PAPER entry is decided under
    return JSONResponse(out)


@router.get("/status")
def get_status() -> JSONResponse:
    return JSONResponse(_service().status().model_dump())


@router.get("/history")
def get_history(limit: int = Query(default=20, ge=1, le=200)) -> JSONResponse:
    return JSONResponse({"history": _service().history(limit), "version": VERSION, "disclaimer": DISCLAIMER})


@router.get("/controls")
def get_controls() -> JSONResponse:
    svc = _service()
    return JSONResponse({"controls": svc.store.controls(), "known_controls": list(CONTROLS), "runs": svc.store.runs(10), "version": VERSION})


@router.get("/intents")
def get_intents(symbol: Optional[str] = Query(default=None, max_length=12), limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    svc = _service()
    return JSONResponse({"intents": [i.model_dump() for i in svc.store.intents(symbol, limit=limit)], "orders": svc.store.orders(limit), "version": VERSION})


@router.get("/positions")
def get_positions(open_only: bool = Query(default=False), limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    svc = _service()
    return JSONResponse({"positions": [p.model_dump() for p in svc.store.positions(open_only=open_only, limit=limit)], "events": svc.store.position_events(None, 100), "version": VERSION})


@router.get("/findings")
def get_findings(limit: int = Query(default=50, ge=1, le=500), blocking_only: bool = Query(default=False)) -> JSONResponse:
    svc = _service()
    return JSONResponse({"findings": svc.store.findings(limit, blocking_only), "healthy": svc.recon_healthy, "reasons": svc.recon_reasons, "version": VERSION})


@router.get("/symbol/{symbol}")
def get_symbol(symbol: str) -> JSONResponse:
    try:
        s = validate_symbol(symbol)
    except ValueError as exc:
        return JSONResponse({"error": "INVALID_SYMBOL", "message": str(exc)}, status_code=400)
    return JSONResponse(_service().card(s).model_dump())


@router.get("/activity")
def get_activity() -> JSONResponse:
    import config
    from api.routes.trader_view import stream
    from execution.account_positions import view
    from execution.activity import activity
    from execution.scheduler import status as sched_status
    s = stream()
    return JSONResponse(activity(s.execution, scheduler_status=sched_status(), enabled=bool(config.EXECUTION_SCHEDULER_ENABLED),
                                 account=view(s.account_refresh, s.execution.store)))


def _act(fn_name: str, body: Optional[ActionBody]) -> JSONResponse:
    svc = _service()
    try:
        out = getattr(svc, fn_name)(actor="user", reason=body.reason if body else None)
    except ExecutionRefused as exc:
        return JSONResponse({"error": exc.code, "message": exc.text, **svc.status().model_dump()}, status_code=409)
    return JSONResponse({"action": fn_name, **out})


@router.post("/auto/enable")
def enable_auto(body: Optional[ActionBody] = None) -> JSONResponse:
    return _act("enable_auto", body)


@router.post("/auto/disable")
def disable_auto(body: Optional[ActionBody] = None) -> JSONResponse:
    return _act("disable_auto", body)
