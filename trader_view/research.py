"""
Latest STORED Stage 5.4 evidence packet for a ticker — read only. Never builds a packet, never fetches, never calls a model: the
Trader View only summarises what Strategy Lab › Evidence already stored. Compact fields only (strength, cause, the two confirmation
rungs, timestamps, data status); the full packet stays in the evidence workspace.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List, Optional

from evidence import STRENGTH_DISCLAIMER
from evidence.store import EvidenceStore
from trader_view.models import EvidenceSummary

RUNG_COMPANY, RUNG_MARKET = 5, 6                         # "company guidance confirming recovery", "market / price confirmation"
NOT_LOADED_TEXT = "No stored evidence packet for this ticker. Research runs only on explicit request (Strategy Lab › Evidence)."
PACKETS_READ = 100                                       # the window ppe.evidence_context.latest_snapshot reads


def _created(row: dict) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick(rows: List[dict], now: datetime) -> Optional[dict]:
    """The packet the setup's evidence check reads (Stage 5.4.1), by PPE Confidence's own rule (ppe.evidence_context.latest_snapshot;
    DESIGN_58 section 5): the newest COMPLETED packet created at or before now, ties to the higher packet_id, so a newer FAILED build never
    hides a valid one. With no such packet, the newest other packet (a FAILED one reads as no evidence), else None."""
    done = [(c, r) for r in rows if r.get("status") == "COMPLETED" for c in [_created(r)] if c is not None and c <= now]
    if done:
        return max(done, key=lambda cr: (cr[0], cr[1].get("packet_id") or ""))[1]
    rest = [r for r in rows if r.get("status") != "COMPLETED"]
    return rest[0] if rest else None


class EvidenceResearchAdapter:
    def __init__(self, store_factory: Callable[[], EvidenceStore] = EvidenceStore, now_fn: Optional[Callable[[], datetime]] = None):
        self._factory = store_factory
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def latest(self, symbol: str) -> EvidenceSummary:
        s = symbol.upper()
        try:
            store = self._factory()
            rows = store.packets(limit=PACKETS_READ, ticker=s) if store.exists() else []
        except Exception as exc:  # noqa: BLE001 - a broken store is reported, never hidden
            return EvidenceSummary(symbol=s, status="FAILED", data_status="FAILED", message=f"Evidence store unavailable ({type(exc).__name__}).",
                                   disclaimer=STRENGTH_DISCLAIMER)
        p = pick(rows, self._now())
        if p is None:
            return EvidenceSummary(symbol=s, status="NOT_LOADED", data_status="NOT_LOADED", message=NOT_LOADED_TEXT, disclaimer=STRENGTH_DISCLAIMER)
        newer = next((r for r in rows if r is not p and r.get("status") != "COMPLETED" and (r.get("created_at") or "") > (p.get("created_at") or "")), None)
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
            message=(f"Stored packet {p.get('status')}: {p.get('failure_code') or ''}".strip() if failed else
                     f"A newer build ({newer.get('created_at')}) {newer.get('status')}: {newer.get('failure_code') or ''}; the newest COMPLETED packet is used."
                     if newer else None),
            disclaimer=STRENGTH_DISCLAIMER)
