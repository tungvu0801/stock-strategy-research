"""
api/routes/execution_safety.py — Stage 5.9.1 EXECUTION SAFETY GATE (state reads + the four review-only user actions).

GET  /api/execution-safety                      evaluate the gate now (persisted) + recent events, control liveness, baselines
GET  /api/execution-safety/events               append-only event history (user actions, baseline creation, daily-loss trigger, day reset, errors)
GET  /api/execution-safety/controls             control liveness rows + recent persisted evaluations
GET  /api/execution-safety/latch                J-04: the persisted Emergency Stop latch alone (no evaluation, no account read, no write)
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


@router.get("/latch")
def read_latch() -> JSONResponse:
    """J-04 (TASK_P plan, design 2.1 Part B): the persisted Emergency Stop latch, read strictly from its row and nothing else, so a page can confirm
    a saved stop in milliseconds while the full evaluation (GET /api/execution-safety) waits for the cash-flow cycle. Never evaluates, never
    reads the account, writes nothing and records no event. The mapping is pinned by tests/test_stop_latch_read.py:
      row with kill_active 1 -> 200 {"status": "OK", "kill_active": true, "kill_activated_at": ...}
      row with kill_active 0 -> 200 {"status": "OK", "kill_active": false}
      no database, table or row -> 200 {"status": "NO_ROW", "kill_active": null}      (only kill_active true is information to a page)
      anything unreadable      -> 503 {"status": "UNREADABLE", "kill_active": null}   (never a guess, never a 500 outside the local gate)
    """
    st = _service().store
    try:
        rec = st.state()                                                           # strict: raises SafetyStoreError when the row cannot be read
        present = st.has_state_row()
    except Exception as exc:  # noqa: BLE001 - unreadable is reported, never guessed
        return JSONResponse({"version": VERSION, "status": "UNREADABLE", "kill_active": None, "error": type(exc).__name__}, status_code=503)
    if not present:
        if rec.kill_active:                                                        # a saved stop whose row check found nothing: the check failed, never "no row"
            return JSONResponse({"version": VERSION, "status": "UNREADABLE", "kill_active": None, "error": "RowCheckFailed"}, status_code=503)
        return JSONResponse({"version": VERSION, "status": "NO_ROW", "kill_active": None, "kill_activated_at": None})
    return JSONResponse({"version": VERSION, "status": "OK", "kill_active": bool(rec.kill_active), "kill_activated_at": rec.kill_activated_at if rec.kill_active else None})


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
