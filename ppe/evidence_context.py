"""
Stored Stage 5.4 evidence -> EvidenceSnapshot for the confidence rules. READ ONLY and point-in-time: the latest COMPLETED packet
whose created_at is at or before as_of (later packets are invisible), its items read from the store. Never builds a packet.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import List, Optional

from evidence.store import EvidenceStore
from ppe.models import EvidenceSnapshot

COMPANY_CATEGORIES = ("COMPANY_SPECIFIC", "COMPANY_CONFIRMATION")
SUPPLY_CATEGORY = "INDUSTRY_SUPPLY_CHAIN"
SUPPLY_ROLES = ("supplier", "customer")
COMPUTED_KINDS = ("METRIC", "EVENT")
SUPPORTED_EPISTEMIC = ("FACT", "INFERENCE")
MAX_TIER = 3


def _dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _supported(item: dict) -> bool:
    try:
        tier = int(item.get("source_tier") or 5)
    except (TypeError, ValueError):
        tier = 5
    return item.get("epistemic") in SUPPORTED_EPISTEMIC and item.get("kind") not in COMPUTED_KINDS and tier <= MAX_TIER


def snapshot_from_packet(packet: dict, items: List[dict], as_of) -> EvidenceSnapshot:
    created = _dt(packet.get("created_at"))
    cutoff = _dt(as_of) or datetime.now(timezone.utc)
    age_days = max(0, (cutoff.date() - created.date()).days) if created else 0
    ladder = packet.get("ladder") if isinstance(packet.get("ladder"), list) else []
    rungs = {str(r.get("rung")): str(r.get("status")) for r in ladder if isinstance(r, dict) and r.get("rung") is not None}
    cause = packet.get("cause") if isinstance(packet.get("cause"), dict) else {}
    share = (cause.get("inputs") or {}).get("explained_share") if isinstance(cause.get("inputs"), dict) else None
    counts = {"company_down": 0, "company_up": 0, "guidance_down": 0, "guidance_up": 0, "supply_down": 0, "supply_up": 0}
    contradictions = 0
    for it in items:
        contradictions += 1 if it.get("contradiction") else 0
        if not _supported(it):
            continue
        d = it.get("direction")
        if d not in ("UP", "DOWN"):
            continue
        suffix = d.lower()
        if it.get("category") in COMPANY_CATEGORIES:
            counts[f"company_{suffix}"] += 1
        if it.get("kind") == "GUIDANCE":
            counts[f"guidance_{suffix}"] += 1
        if it.get("category") == SUPPLY_CATEGORY or it.get("role") in SUPPLY_ROLES:
            counts[f"supply_{suffix}"] += 1
    return EvidenceSnapshot(packet_id=str(packet.get("packet_id")), packet_status=str(packet.get("status") or ""), created_at=str(packet.get("created_at") or ""),
                            as_of_date=packet.get("as_of_date"), age_days=age_days, strength=packet.get("strength"), cause=cause.get("verdict"),
                            explained_share=float(share) if isinstance(share, (int, float)) else None, rungs=rungs, contradictions=contradictions,
                            n_items=int(packet.get("n_items") or len(items)), **counts)


def latest_snapshot(symbol: str, as_of, store: Optional[EvidenceStore] = None, *, limit: int = 100) -> Optional[EvidenceSnapshot]:
    """The latest COMPLETED packet created at or before as_of, or None. Reads only (the store opens read-only connections)."""
    store = store or EvidenceStore()
    if not store.exists():
        return None
    cutoff = _dt(as_of) or datetime.now(timezone.utc)
    rows = [r for r in store.packets(limit=limit, ticker=symbol.upper()) if r.get("status") == "COMPLETED"]
    eligible = [(c, r) for r in rows for c in [_dt(r.get("created_at"))] if c is not None and c <= cutoff]
    if not eligible:
        return None
    _, packet = max(eligible, key=lambda cr: (cr[0], cr[1].get("packet_id") or ""))
    return snapshot_from_packet(packet, store.items(packet["packet_id"]), as_of)
