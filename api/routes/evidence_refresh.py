"""
api/routes/evidence_refresh.py - Stage 5.4.2 evidence refresh status (GET only).

GET /api/evidence-refresh: the configured mode, the refresh thread's status and recent attempts, the attempt journal (errors that stored
no packet, the run circuit) and the plan computed now from the stored packets (which watchlist symbols are FRESH / DUE / STALE /
MISSING, when each goes stale, which would be built next).

GET /api/evidence-refresh?at=<ISO time>: the same plan evaluated at a later instant (at most PREVIEW_MAX_DAYS ahead) - a preview of the
decision path on the stored packets ("which symbols would a run at that time build?"), so a DRY_RUN evening on fresh packets is not the
only evidence. Reading it never builds a packet, never fetches anything, never writes the journal and never calls a model or a broker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from evidence_refresh import DISCLAIMER, VERSION
from evidence_refresh import scheduler as S
from evidence_refresh.service import EvidenceRefresher, mode

router = APIRouter(prefix="/api/evidence-refresh", tags=["evidence-refresh"])
PREVIEW_MAX_DAYS = 90


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_at(at: str, now: datetime) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(at.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return t if now <= t <= now + timedelta(days=PREVIEW_MAX_DAYS) else None


@router.get("")
def get_evidence_refresh(at: Optional[str] = None) -> JSONResponse:
    now = _utcnow()
    when = now
    if at is not None:
        when = _parse_at(at, now)
        if when is None:
            return JSONResponse({"status": "INVALID_AT", "message": f"at must be an ISO time from now to {PREVIEW_MAX_DAYS} days ahead"},
                                status_code=422)
    sch = S._SCHEDULER
    refresher = sch.refresher if sch is not None else EvidenceRefresher(lease_store_fn=None)
    try:
        plan = refresher.plan(when)
    except Exception as exc:  # noqa: BLE001 - a status read never fails the page
        plan = {"error": type(exc).__name__}
    return JSONResponse({"version": VERSION, "read_only": True, "mode": mode(), "checked_at": now.isoformat(timespec="seconds"),
                         "preview_at": when.isoformat(timespec="seconds") if at is not None else None, "scheduler": S.status(), "plan": plan,
                         "model_requests": 0, "broker_calls": 0, "disclaimer": DISCLAIMER})
