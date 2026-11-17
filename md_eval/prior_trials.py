"""md_eval/prior_trials.py - the earlier trials, reconstructed CONSERVATIVELY: no trial registry existed before MD-4 (VALIDATION_AUDIT_MATRIX.md
SV-27, SV-35), so every count below is a LOWER BOUND (count_is_lower_bound) and an uncertain dataset or range counts against every new dataset
(range UNKNOWN). The list is reference data, appended to a trial registry once as RECONSTRUCTED_PRIOR entries; it is reviewed with the
milestone and only ever grows. V1 / C1 are absent on purpose: they have never run on real data (planning gate 2026-11-15 section 6, item 2)."""
from __future__ import annotations

IEX_DAILY = "IEX daily bundle, 81 large caps + SPY"
IEX_RANGE = ["2020-07-27", "2026-10-23"]
UNKNOWN_RANGE = {"status": "UNKNOWN", "reason": "RANGE_NOT_RECORDED"}

PRIOR_TRIALS = (
    {"prior_id": "PRIOR-6.4-VQ", "stage": "6.4", "family": "VALUE_QUALITY", "variants_lower_bound": 1, "dataset": IEX_DAILY, "range": IEX_RANGE,
     "outcome": "FAIL", "evidence": ["VALIDATION_AUDIT_MATRIX.md SV-35", "MD planning gate 2026-11-15 section 6 item 2"]},
    {"prior_id": "PRIOR-6.5-MOM", "stage": "6.5", "family": "MOMENTUM", "variants_lower_bound": 1, "dataset": IEX_DAILY, "range": IEX_RANGE,
     "outcome": "FAIL", "evidence": ["VALIDATION_AUDIT_MATRIX.md SV-35", "MD planning gate 2026-11-15 section 6 item 2"]},
    {"prior_id": "PRIOR-6.6-MR", "stage": "6.6", "family": "MEAN_REVERSION", "variants_lower_bound": 1, "dataset": IEX_DAILY, "range": IEX_RANGE,
     "outcome": "FAIL", "evidence": ["VALIDATION_AUDIT_MATRIX.md SV-35", "MD planning gate 2026-11-15 section 6 item 2"]},
    {"prior_id": "PRIOR-6.7-BO", "stage": "6.7", "family": "BREAKOUT", "variants_lower_bound": 1, "dataset": IEX_DAILY, "range": IEX_RANGE,
     "outcome": "FAIL", "evidence": ["VALIDATION_AUDIT_MATRIX.md SV-35", "MD planning gate 2026-11-15 section 6 item 2"]},
    {"prior_id": "PRIOR-5.2-SIGNAL-RESEARCH", "stage": "5.2", "family": "SIGNAL_RESEARCH", "variants_lower_bound": 20, "dataset": "UNKNOWN",
     "range": UNKNOWN_RANGE, "outcome": "NOT_CORRECTED_FOR_MULTIPLE_TESTING",
     "evidence": ["VALIDATION_AUDIT_MATRIX.md SV-34 (20 variants on the campaign's own 5 TEST windows; no correction)"]},
    {"prior_id": "PRIOR-A2-HARNESS-RUNS", "stage": "validation audit A2", "family": "HARNESS_STORED_RUNS", "variants_lower_bound": 6, "dataset": "UNKNOWN",
     "range": UNKNOWN_RANGE, "outcome": "UNKNOWN",
     "evidence": ["VALIDATION_AUDIT_MATRIX.md SV-26 (6 runs of one version stored, splits 0.5-0.9 plus 0 bps; counted conservatively)"]},
)
