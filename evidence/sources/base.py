"""
evidence/sources/base.py — the shapes every adapter produces (DESIGN_54 §3, §7).

SourceItem: one sourced statement BEFORE the deterministic rules run (tier, epistemic label and category may be None here and are
filled by evidence.rules; adapters only record provenance and the declared role). AdapterResult: the items plus every fetch attempt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from evidence.net import FetchResult


@dataclass
class SourceItem:
    subject_ticker: str                    # the company the text is about (target or a declared related ticker)
    source_type: str                       # evidence.SOURCE_TYPES
    source_name: str
    source_url: Optional[str]
    source_identifier: Optional[str]       # accession number, article id, bar range, event id
    published_at: Optional[str]            # ISO timestamp from the source, when known
    retrieved_at: str                      # ISO timestamp of this build's retrieval
    evidence_date: str                     # YYYY-MM-DD the statement is about / was published
    kind: str                              # evidence.KINDS
    text: str                              # <= 2000 chars, verbatim or computed sentence
    role: str                              # evidence.ROLES (declared by the adapter / the user)
    value: Optional[Dict[str, Any]] = None  # numbers for computed items
    source_hash: Optional[str] = None      # sha256 of the fetched body (or of the record for headlines); None for computed / manual text
    fetch_index: Optional[int] = None      # index into AdapterResult.fetches
    date_source: str = "source"            # "source" | "retrieval" (no date found in the document)
    category: Optional[str] = None         # filled by rules unless the adapter is certain (market data / events)
    source_tier: Optional[int] = None      # filled by rules
    epistemic: Optional[str] = None        # filled by rules
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AdapterResult:
    adapter: str
    items: List[SourceItem] = field(default_factory=list)
    fetches: List[FetchResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)       # human-readable reasons something was skipped (stored in the packet request/notes)
    requests: int = 0                                    # live requests made by this adapter in this build
    facts: Dict[str, Any] = field(default_factory=dict)  # computed numbers the rules read directly (internal adapter)

    def add_fetch(self, fr: FetchResult) -> int:
        self.fetches.append(fr)
        self.requests += 1
        return len(self.fetches) - 1


def iso_day(dt) -> str:
    """YYYY-MM-DD from a date / datetime / ISO string; '' when unknown."""
    if dt is None:
        return ""
    if hasattr(dt, "date") and callable(dt.date):
        return dt.date().isoformat()
    if hasattr(dt, "isoformat"):
        return dt.isoformat()[:10]
    return str(dt)[:10]
