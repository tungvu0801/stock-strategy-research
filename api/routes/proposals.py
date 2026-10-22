"""
api/routes/proposals.py — Stage 5.9 REVIEW PROPOSALS (read only + the review-state acknowledgement pattern).

GET  /api/proposals                         the current proposal view for every open setup episode
GET  /api/proposals/controls                control liveness rows + recent control evaluations ("was it evaluated, what did it see?")
GET  /api/proposals/history                 stored proposals, newest first (symbol, limit)
GET  /api/proposals/id/{proposal_id}        one stored proposal (effective status derived against the current setup)
GET  /api/proposals/{symbol}                the current view for one ticker (never generates)
POST /api/proposals/{proposal_id}/view      review state only (UNREVIEWED -> VIEWED)
POST /api/proposals/{proposal_id}/dismiss   review state only (-> DISMISSED)

Nothing here buys, sells, previews, submits, places or cancels. A proposal is a review artifact; its status line is always NOT SUBMITTED.
"""
import re
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.validation import validate_symbol
from proposals import CONTROLS, DISCLAIMER, SETUP_TYPE, SIDE_LABEL, STATUS_LABELS, SUBMISSION_STATUS, VERSION
from proposals.store import ProposalStore

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
_ID = re.compile(r"^[0-9a-f]{32}$")


class EmptyBody(BaseModel):
    pass


def _service():
    from api.routes.trader_view import stream
    return stream().proposals


def _store() -> ProposalStore:
    return _service().store                                   # the same store the generator writes (tests substitute the stream)


@router.get("")
def list_proposals() -> JSONResponse:
    out = _service().overview()
    out.update({"statuses": STATUS_LABELS, "note": "A proposal is a deterministic review artifact for a READY FOR REVIEW setup — not a recommendation, never submitted."})
    return JSONResponse(out)


@router.get("/controls")
def list_controls(control: Optional[str] = Query(default=None, max_length=40), limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    st = _store()
    return JSONResponse({"controls": st.control_status(), "evaluations": st.control_evaluations(control, limit), "known_controls": list(CONTROLS), "version": VERSION})


@router.get("/history")
def list_history(symbol: Optional[str] = Query(default=None, max_length=12), limit: int = Query(default=50, ge=1, le=200)) -> JSONResponse:
    s = None
    if symbol:
        try:
            s = validate_symbol(symbol)
        except ValueError as exc:
            return JSONResponse({"status": "INVALID_SYMBOL", "message": str(exc)}, status_code=400)
    return JSONResponse({"proposals": _store().history(s, limit), "version": VERSION, "submission_status": SUBMISSION_STATUS, "disclaimer": DISCLAIMER})


@router.get("/id/{proposal_id}")
def get_by_id(proposal_id: str) -> JSONResponse:
    if not _ID.match(proposal_id):
        return JSONResponse({"status": "NOT_FOUND", "message": "No proposal with that id."}, status_code=404)
    svc = _service()
    row = svc.store.get(proposal_id)
    if row is None:
        return JSONResponse({"status": "NOT_FOUND", "message": "No proposal with that id."}, status_code=404)
    view = svc.view_of(row, svc.setup_view(row["symbol"]), svc.clock())
    return JSONResponse({**view.model_dump(), "control_rows": svc.store.controls_for(proposal_id)})


@router.get("/{symbol}")
def get_proposal(symbol: str) -> JSONResponse:
    try:
        s = validate_symbol(symbol)
    except ValueError as exc:
        return JSONResponse({"status": "INVALID_SYMBOL", "message": str(exc)}, status_code=400)
    return JSONResponse(_service().current(s).model_dump())


@router.post("/{proposal_id}/view")
def mark_viewed(proposal_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    return _review(proposal_id, "VIEWED")


@router.post("/{proposal_id}/dismiss")
def dismiss(proposal_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    return _review(proposal_id, "DISMISSED")


def _review(proposal_id: str, state: str) -> JSONResponse:
    if not _ID.match(proposal_id):
        return JSONResponse({"status": "NOT_FOUND", "message": "No proposal with that id."}, status_code=404)
    view = _service().set_review(proposal_id, state)
    if view is None:
        return JSONResponse({"status": "NOT_FOUND", "message": "No proposal with that id."}, status_code=404)
    return JSONResponse({"review_state": view.review_state, "reviewed_at": view.reviewed_at, "proposal": view.model_dump(), "side_label": SIDE_LABEL, "setup_type": SETUP_TYPE,
                         "submission_status": SUBMISSION_STATUS})
