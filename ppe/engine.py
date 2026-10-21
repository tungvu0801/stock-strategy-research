"""PPE engine entry point: Stretch + Confidence + source labels -> PPEResult. Pure computation over the inputs it is given."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from ppe import DISCLAIMER, NOT_A_SIGNAL, WEIGHTS
from ppe.confidence import compute_confidence
from ppe.inputs import MarketInputs, point_in_time
from ppe.models import EvidenceSnapshot, PPEResult, PPESources
from ppe.stretch import compute_stretch

SIMULATED_NOTE = "SIMULATED DATA — synthetic prices from the development simulator. This PPE result demonstrates the display path and is not a market reading."


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat(timespec="seconds")


def compute_ppe(inputs: MarketInputs, evidence: Optional[EvidenceSnapshot], *, weights: Optional[Dict[str, float]] = None,
                now_fn: Callable[[], float] = time.time) -> PPEResult:
    weights = dict(weights or WEIGHTS)
    stretch = compute_stretch(inputs, weights)
    confidence = compute_confidence(stretch, evidence, inputs.as_of)
    pit = point_in_time(inputs.bars, inputs.as_of)
    b = inputs.benchmark
    sources = PPESources(mode=inputs.mode, market_data=inputs.market_data, price_source=inputs.price_source,
                         bars_through=pit[-1].date.isoformat() if pit else None, sessions_used=len(pit),
                         benchmark=b.symbol if b is not None else None, benchmark_kind=b.kind if b is not None else "NONE",
                         benchmark_label=(b.label or b.symbol) if b is not None else None,
                         evidence_packet_at=evidence.created_at if evidence is not None else None, computed_at=_iso(now_fn()))
    if inputs.mode == "simulated":
        status, note = "SIMULATED_DATA", SIMULATED_NOTE
    elif stretch.status == "AVAILABLE":
        status, note = "AVAILABLE", NOT_A_SIGNAL
    else:
        status, note = stretch.status, stretch.note or NOT_A_SIGNAL
    as_of = inputs.price_time or inputs.as_of.isoformat()
    return PPEResult(symbol=inputs.symbol.upper(), as_of=as_of, status=status, stretch=stretch, confidence=confidence, sources=sources,
                     weights=weights, disclaimer=DISCLAIMER, note=note)
