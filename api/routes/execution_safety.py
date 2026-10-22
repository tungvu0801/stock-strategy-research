"""
api/routes/execution_safety.py — Stage 5.9.1 EXECUTION SAFETY GATE (state reads + the four review-only user actions).

GET  /api/execution-safety                      evaluate the gate now (persisted) + recent events, control liveness, baselines
GET  /api/execution-safety/events               append-only event history (user actions, baseline creation, daily-loss trigger, day reset, errors)
GET  /api/execution-safety/controls             control liveness rows + recent persisted evaluations
POST /api/execution-safety/pause-new-entries    NO_NEW_ENTRIES on
POST /api/execution-safety/resume-new-entries   NO_NEW_ENTRIES off (never clears an emergency stop)
POST /api/execution-safety/emergency-stop       manual kill switch on
POST /api/execution-safety/clear-emergency-stop manual kill switch off (a distinct, explicit action)

The actions mutate safety state only: nothing here submits, previews, liquidates or calls a broker.
"""
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from safety import CONTROLS, DISCLAIMER, STATE_LABELS, VERSION

router = APIRouter(prefix="/api/execution-safety", tags=["execution-safety"])


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=200)


def _service():
    from api.routes.trader_view import stream
    return stream().safety


@router.get("")
def get_state() -> JSONResponse:
    svc = _service()
    out = svc.overview()
    out.update({"labels": STATE_LABELS, "note": "Risk controls gate future entries only; nothing here places, previews or cancels an order."})
    return JSONResponse(out)


@router.get("/events")
def list_events(kind: Optional[str] = Query(default=None, max_length=32), limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    st = _service().store
    return JSONResponse({"events": st.events(limit, kind), "count": st.event_count(kind), "total": st.event_count(), "version": VERSION})


@router.get("/controls")
def list_controls(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    st = _service().store
    return JSONResponse({"controls": st.controls(), "evaluations": st.evaluations(limit), "known_controls": list(CONTROLS), "version": VERSION, "disclaimer": DISCLAIMER})


def _act(fn_name: str, body: Optional[ActionBody]) -> JSONResponse:
    svc = _service()
    view = getattr(svc, fn_name)(reason=body.reason if body else None, actor="user")
    return JSONResponse({"action": fn_name, **view.model_dump(), "events": svc.store.events(5)})


@router.post("/pause-new-entries")
def pause_new_entries(body: Optional[ActionBody] = None) -> JSONResponse:
    return _act("pause", body)


@router.post("/resume-new-entries")
def resume_new_entries(body: Optional[ActionBody] = None) -> JSONResponse:
    return _act("resume", body)


@router.post("/emergency-stop")
def emergency_stop(body: Optional[ActionBody] = None) -> JSONResponse:
    return _act("emergency_stop", body)


@router.post("/clear-emergency-stop")
def clear_emergency_stop(body: Optional[ActionBody] = None) -> JSONResponse:
    return _act("clear_emergency_stop", body)
