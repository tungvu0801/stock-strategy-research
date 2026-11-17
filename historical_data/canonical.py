"""
historical_data/canonical.py - the canonical form of contract section 6.3 and content_sha256 (P7).

UTF-8 JSON, sort_keys=True, separators (",", ":"); NaN and Infinity are refused (allow_nan=False); floats are written by Python's repr (the
json module's float formatting); missing values are {"status", "reason"} objects; timestamps ISO-8601 UTC ending in Z; sessions YYYY-MM-DD.
Instruments are sorted by instrument_id, bars and gaps by (instrument_id, session), events by (effective_session, event_id), membership
changes by (universe_id, session, instrument_id, action). The document is exactly
  {"manifest": <manifest minus content_sha256>, "instruments", "bars", "gaps", "events", "membership", "scheduled_events" | UNAVAILABLE}
and content_sha256 is the sha256 of its bytes.
"""
from __future__ import annotations

import hashlib
import json

from historical_data.schema import jv
from historical_data.status import is_missing


def dumps(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document(manifest, instruments, bars, gaps, events, membership, scheduled_events) -> dict:
    """The canonical document from the provider's tables (any order in, one order out)."""
    def bar_key(b):
        return (b.instrument_id, b.session, json.dumps(jv(b.source_ref), sort_keys=True), dumps(b.to_json()))
    return {
        "manifest": manifest.to_json(include_content_sha256=False),
        "instruments": [i.to_json() for i in sorted(instruments, key=lambda i: i.instrument_id)],
        "bars": [b.to_json() for b in sorted(bars, key=bar_key)],
        "gaps": [g.to_json() for g in sorted(gaps, key=lambda g: (g.instrument_id, g.session, g.reason))],
        "events": [e.to_json() for e in sorted(events, key=lambda e: (e.effective_session, e.event_id))],
        "membership": [m.to_json() for m in sorted(membership, key=lambda m: (m.universe_id, m.session, m.instrument_id, m.action))],
        "scheduled_events": scheduled_events.to_json() if is_missing(scheduled_events) else
        [s.to_json() for s in sorted(scheduled_events, key=lambda s: (s.event_session, s.event_id))],
    }


def content_sha256(doc: dict) -> str:
    return sha256_bytes(dumps(doc))
