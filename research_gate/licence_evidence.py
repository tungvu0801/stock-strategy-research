"""research_gate/licence_evidence.py - the admission's licence-evidence check for a HISTORICAL dataset (I-1A 2.8, OD-3; ID-8).

The evidence record must name the fields below. Its format and loader are undefined until the data-source gate, so check() ALWAYS refuses
with LICENCE_EVIDENCE_UNDEFINED, whatever the manifest's licence block says. Nothing is read: no loader, no file, no expiry presumed."""
from __future__ import annotations

from research_gate.snapshot import LICENCE_EVIDENCE_UNDEFINED, AdmissionRefused

REQUIRED_FIELDS = ("provider", "evidence_source", "permitted_usage", "retention_rights", "expiry", "commercial_restrictions")
EVIDENCE_SOURCE_FIELDS = ("document", "retrieval_date", "sha256")


def check(manifest, decision) -> None:
    """Always raises: no licence evidence can be admitted in I-1B (every HISTORICAL admission refuses)."""
    reasons = [LICENCE_EVIDENCE_UNDEFINED] + list((decision or {}).get("reasons", []))
    raise AdmissionRefused(LICENCE_EVIDENCE_UNDEFINED, reasons, decision=decision)
