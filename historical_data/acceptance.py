"""
historical_data/acceptance.py - the dataset acceptance criteria AS CODE (ACCEPTANCE_CRITERIA.md states the same rules as a document), so MD-7's
acceptance step is mechanical. Every rule is a contract rule; nothing here is new policy.

decide(record, manifest) -> {usable_for, reasons, verdict_caps, flags, warnings}
  usable_for NONE             the acceptance record is BLOCK (section 5), or the licence is NOT_PERMITTED (6.2: no use except deletion)
  usable_for INTEGRITY_ONLY   kind HISTORICAL and personal_research_use is not CONFIRMED (6.2, A10), or raw open / high / low are not
                              available (P3, 4.1): schema, integrity and validation checks only - no returns, signals, ledger or statistics
  usable_for EVALUATION       otherwise; the caps and flags below still travel with every result
  verdict caps (each caps an edge or alpha claim at NOT_ESTABLISHED): FIXED_CURRENT_UNIVERSE (3.4: SURVIVORSHIP_BIAS_PRESENT);
              events_completeness not COMPLETE (P3a: every held instrument CORPORATE_ACTION_UNACCOUNTED; A7 caps it); no split view (P3: signals
              only in a run flagged SPLIT_UNADJUSTED, which caps)
  flags       EARNINGS_DATA_UNAVAILABLE (3.5: D.3-bound strategies produce no candidates, reported); SYMBOL_HISTORY_UNKNOWN (A8);
              STATIC_CLASSIFICATION_MAP_NEEDED when sector / issuer is UNKNOWN (A4: a run that supplies a map is flagged and capped)
"""
from __future__ import annotations

from typing import Iterable

import historical_data as HD

CRITERIA_VERSION = "historical_data_acceptance_criteria_v1"
NONE, INTEGRITY_ONLY, EVALUATION = "NONE", "INTEGRITY_ONLY", "EVALUATION"
NOT_ESTABLISHED = "NOT_ESTABLISHED"


def decide(record: dict, manifest, *, instruments: Iterable = (), earnings_unavailable: bool = True) -> dict:
    reasons, caps, flags = [], [], []
    if record.get("verdict") == HD.BLOCK:
        reasons.append("DATASET_BLOCKED")
    if record.get("content_sha256") != manifest.content_sha256 or record.get("dataset_id") != manifest.dataset_id:
        reasons.append("RECORD_IS_NOT_THIS_DATASET")
    use = manifest.licence.personal_research_use
    if use == "NOT_PERMITTED":
        reasons.append("LICENCE_NOT_PERMITTED")
    if reasons:
        usable = NONE
    else:
        integrity = []
        if manifest.kind == HD.HISTORICAL and use != "CONFIRMED":
            integrity.append("LICENCE_NOT_CONFIRMED")
        if "RAW_OHL" not in manifest.price_views_available:
            integrity.append("RAW_OHL_UNAVAILABLE")
        reasons += integrity
        usable = INTEGRITY_ONLY if integrity else EVALUATION
    if manifest.membership_basis == "FIXED_CURRENT_UNIVERSE":
        caps.append({"flag": "SURVIVORSHIP_BIAS_PRESENT", "cap": NOT_ESTABLISHED, "rule": "3.4"})
    if manifest.events_completeness != "COMPLETE":
        caps.append({"flag": "CORPORATE_ACTION_UNACCOUNTED", "cap": NOT_ESTABLISHED, "rule": "P3a, A7"})
    if "SPLIT_ADJUSTED" not in manifest.price_views_available:
        caps.append({"flag": "SPLIT_UNADJUSTED", "cap": NOT_ESTABLISHED, "rule": "P3: no signals unless the run is flagged SPLIT_UNADJUSTED"})
    if earnings_unavailable:
        flags.append("EARNINGS_DATA_UNAVAILABLE")
    insts = list(instruments)
    if any(i.symbol_history_status == "UNKNOWN" for i in insts):
        flags.append("SYMBOL_HISTORY_UNKNOWN")
    if any(not isinstance(i.sector, str) or not isinstance(i.issuer_id, str) for i in insts):
        flags.append("STATIC_CLASSIFICATION_MAP_NEEDED")
    warnings = sorted({r["code"] for r in record.get("reasons", []) if r.get("severity") == HD.WARN})
    return {"criteria_version": CRITERIA_VERSION, "dataset_id": manifest.dataset_id, "content_sha256": manifest.content_sha256,
            "record_sha256": record.get("record_sha256"), "usable_for": usable, "reasons": reasons, "verdict_caps": caps, "flags": flags,
            "warnings": warnings}
