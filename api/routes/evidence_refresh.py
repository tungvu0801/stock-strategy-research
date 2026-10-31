"""
api/routes/evidence_refresh.py - Stage 5.4.2 evidence refresh status (GET only).

GET /api/evidence-refresh: the configured mode, the refresh thread's status and recent attempts, and the plan computed now from the
stored packets (which watchlist symbols are FRESH / DUE / STALE / MISSING, when each goes stale, which would be built next). Reading
it never builds a packet, never fetches anything and never calls a model or a broker.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from evidence_refresh import DISCLAIMER, VERSION
from evidence_refresh import scheduler as S
from evidence_refresh.service import EvidenceRefresher, mode

router = APIRouter(prefix="/api/evidence-refresh", tags=["evidence-refresh"])


@router.get("")
def get_evidence_refresh() -> JSONResponse:
    now = datetime.now(timezone.utc)
    sch = S._SCHEDULER
    refresher = sch.refresher if sch is not None else EvidenceRefresher(lease_store_fn=None)
    try:
        plan = refresher.plan(now)
    except Exception as exc:  # noqa: BLE001 - a status read never fails the page
        plan = {"error": type(exc).__name__}
    return JSONResponse({"version": VERSION, "read_only": True, "mode": mode(), "checked_at": now.isoformat(timespec="seconds"),
                         "scheduler": S.status(), "plan": plan, "model_requests": 0, "broker_calls": 0, "disclaimer": DISCLAIMER})
