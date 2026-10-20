"""
Latest STORED Stage 5.4 evidence packet for a ticker — read only. Never builds a packet, never fetches, never calls a model: the
Trader View only summarises what Strategy Lab › Evidence already stored. Compact fields only (strength, cause, the two confirmation
rungs, timestamps, data status); the full packet stays in the evidence workspace.
"""
from __future__ import annotations

from typing import Callable, Optional

from evidence import STRENGTH_DISCLAIMER
from evidence.store import EvidenceStore
from trader_view.models import EvidenceSummary

RUNG_COMPANY, RUNG_MARKET = 5, 6                         # "company guidance confirming recovery", "market / price confirmation"
NOT_LOADED_TEXT = "No stored evidence packet for this ticker. Research runs only on explicit request (Strategy Lab › Evidence)."


class EvidenceResearchAdapter:
    def __init__(self, store_factory: Callable[[], EvidenceStore] = EvidenceStore):
        self._factory = store_factory

    def latest(self, symbol: str) -> EvidenceSummary:
        s = symbol.upper()
        try:
            store = self._factory()
            rows = store.packets(limit=1, ticker=s) if store.exists() else []
        except Exception as exc:  # noqa: BLE001 - a broken store is reported, never hidden
            return EvidenceSummary(symbol=s, status="FAILED", data_status="FAILED", message=f"Evidence store unavailable ({type(exc).__name__}).",
                                   disclaimer=STRENGTH_DISCLAIMER)
        if not rows:
            return EvidenceSummary(symbol=s, status="NOT_LOADED", data_status="NOT_LOADED", message=NOT_LOADED_TEXT, disclaimer=STRENGTH_DISCLAIMER)
        p = rows[0]
        ladder = [r for r in (p.get("ladder") or []) if isinstance(r, dict)]
        cause = p.get("cause") if isinstance(p.get("cause"), dict) else {}
        by_rung = {r.get("rung"): r for r in ladder}
        failed = p.get("status") != "COMPLETED"
        return EvidenceSummary(
            symbol=s, status="LOADED", packet_id=p.get("packet_id"), packet_status=p.get("status"), created_at=p.get("created_at"),
            as_of_date=p.get("as_of_date"), strength=p.get("strength"), strength_text=p.get("strength_text"),
            cause=cause.get("verdict"), cause_reason=cause.get("reason"),
            company_confirm=(by_rung.get(RUNG_COMPANY) or {}).get("status"), market_confirm=(by_rung.get(RUNG_MARKET) or {}).get("status"),
            rungs=[{"rung": r.get("rung"), "name": r.get("name"), "status": r.get("status")} for r in ladder],
            n_items=p.get("n_items"), n_failed_fetches=p.get("n_failed_fetches"), data_status="STORED",
            message=(f"Stored packet {p.get('status')}: {p.get('failure_code') or ''}".strip() if failed else None),
            disclaimer=STRENGTH_DISCLAIMER)
