"""md_eval/prereg.py - MD-4: the pre-registration template. Every verdict threshold, cost level, segment date, seed and statistic is fixed here,
BEFORE any look; the verdict reads its thresholds only from a pre-registration whose sha256 the holdout ledger recorded before the look.
validate() refuses an unknown or missing key, a book that differs from the frozen book, and a cost multiplier outside the levels."""
from __future__ import annotations

import copy
import hashlib
import json

from md_eval import BOOK, CONTRACT_V2, CONTRACT_V2_SHA256
from md_eval import cash_rate as CR
from md_eval.costs import DEFAULT_BUCKETS, MULTIPLIERS

PREREG_VERSION = "md_eval_prereg_v1"
CASH_MODES = ("ZERO", "HYPOTHETICAL_CONSTANT", "REGISTERED_SERIES")
DSR_BASES = ("STRATEGY_EXCESS_OVER_CASH", "EXCESS_OVER_EXPOSURE_MATCHED_SPY")

TEMPLATE = {
    "prereg_version": PREREG_VERSION,
    "hypothesis_id": "<id>",
    "family": ["<every hypothesis id opened jointly with this one>"],
    "strategy": {"id": "SWING_TREND_PULLBACK", "version": "1.0", "preregistration_hash": "<the strategy's frozen hash>"},
    "control": {"id": "SWING_TREND_PULLBACK_CONTROL", "version": "1.0"},
    "gating_comparison": "EXPOSURE_MATCHED_SPY",
    "benchmark_instrument": "<instrument_id of SPY in the dataset>",    # the engine computes its total-return series from the run's own provider
    "contract": {"version": CONTRACT_V2, "sha256": CONTRACT_V2_SHA256},
    "dataset_sha256": "TO_BE_ACCEPTED",
    "segments": {"TRAIN": ["YYYY-MM-DD", "YYYY-MM-DD"], "VALIDATION": ["YYYY-MM-DD", "YYYY-MM-DD"], "TEST": ["YYYY-MM-DD", "YYYY-MM-DD"]},
    "embargo_sessions": 5,
    "book": dict(BOOK),
    "costs": {"buckets": [dict(b) for b in DEFAULT_BUCKETS], "multipliers": list(MULTIPLIERS), "label": "ASSUMPTION", "fees": True},
    "cash_rate": {"mode": "ZERO"},
    "delisting_primary": "MINUS_100_PERCENT",
    "initial_cash": 100000.0,
    "static_map_sha256": None,                                       # or the sha256 of the static classification map every gating run uses (A4)
    "dsr_basis": "STRATEGY_EXCESS_OVER_CASH",                        # or EXCESS_OVER_EXPOSURE_MATCHED_SPY
    "statistic": "MEAN_DAILY_DIFFERENCE_VS_EXPOSURE_MATCHED_SPY",
    "bootstrap": {"mean_block_sessions": 20, "resamples": 10000, "seed": 0, "confidence": 0.90},
    "random_control": {"base_seed": 0, "n_seeds": 1000},
    "thresholds": {"alpha": None, "n_min_sessions": None, "min_dsr": None},
    "author": "<name>",
    "created_at": "<ISO-8601 UTC>",
}


class PreregError(ValueError):
    pass


def _hex64(x) -> bool:
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)


SETTINGS_KEYS = ("costs", "embargo_sessions", "cash_rate", "initial_cash", "static_map_sha256", "segments")


def settings_mismatch(st: dict, p: dict) -> list:
    """MED-3 / LOW-R2 / LOW-R3: the run settings in `st` (buckets, fees, label, embargo_sessions, cash_rate {mode, annual, sha256},
    delisting_mode, initial_cash, static_map_sha256, book, segment, segment_label) that differ from the pre-registration `p` (the full
    pre-registration, or the trial registry's copy of its SETTINGS_KEYS). Shared by the verdict and by the engine's TEST guard."""
    want = {"buckets": [dict(b) for b in p["costs"]["buckets"]], "fees": p["costs"]["fees"], "label": p["costs"]["label"],
            "embargo_sessions": p["embargo_sessions"], "delisting_mode": "PRIMARY", "initial_cash": p["initial_cash"],
            "static_map_sha256": p["static_map_sha256"], "book": dict(BOOK)}
    bad = [k for k, v in want.items() if (st.get(k) if k != "buckets" else [dict(b) for b in (st.get(k) or [])]) != v]
    cr, have = p["cash_rate"], st.get("cash_rate") or {}
    if have.get("mode") != cr["mode"]:
        bad.append("cash_rate.mode")
    elif cr["mode"] == "HYPOTHETICAL_CONSTANT" and have.get("annual") != cr["annual"]:
        bad.append("cash_rate.annual")
    elif cr["mode"] == "REGISTERED_SERIES" and have.get("sha256") != cr["sha256"]:
        bad.append("cash_rate.sha256")
    label = st.get("segment_label")
    if label not in p["segments"] or list(st.get("segment") or []) != list(p["segments"][label]):
        bad.append("segment")
    return bad


def template() -> dict:
    return copy.deepcopy(TEMPLATE)


def canonical(p: dict) -> str:
    return json.dumps(p, sort_keys=True, separators=(",", ":"))


def sha256(p: dict) -> str:
    return hashlib.sha256(canonical(p).encode("utf-8")).hexdigest()


def validate(p: dict) -> str:
    """The pre-registration's sha256, or PreregError."""
    if set(p) != set(TEMPLATE):
        raise PreregError(f"keys differ from the template: unknown {sorted(set(p) - set(TEMPLATE))}, missing {sorted(set(TEMPLATE) - set(p))}")
    if p["prereg_version"] != PREREG_VERSION:
        raise PreregError("unknown pre-registration version")
    if p["book"] != dict(BOOK):
        raise PreregError("the book is FROZEN: the pre-registration must carry it unchanged")
    if p["contract"] != {"version": CONTRACT_V2, "sha256": CONTRACT_V2_SHA256}:
        raise PreregError("the pre-registration names an unsupported contract")
    if not set(p["costs"].get("multipliers", [])) <= set(MULTIPLIERS) or set(p["costs"]) != {"buckets", "multipliers", "label", "fees"} \
            or not isinstance(p["costs"]["fees"], bool) or not p["costs"]["buckets"]:
        raise PreregError(f"costs are exactly buckets, multipliers (among {MULTIPLIERS}), label and fees (bool)")
    cr = p["cash_rate"]
    if cr.get("mode") not in CASH_MODES:
        raise PreregError(f"cash_rate.mode is one of {CASH_MODES}")
    if cr["mode"] == "ZERO" and set(cr) != {"mode"}:
        raise PreregError("cash_rate ZERO carries no other field")
    if cr["mode"] == "HYPOTHETICAL_CONSTANT" and (set(cr) != {"mode", "annual"} or not isinstance(cr["annual"], float)):
        raise PreregError("cash_rate HYPOTHETICAL_CONSTANT needs exactly a float `annual` (LOW-R2)")
    if cr["mode"] == "REGISTERED_SERIES" and (set(cr) != {"mode", "sha256"} or not _hex64(cr["sha256"])):
        raise PreregError("cash_rate REGISTERED_SERIES needs exactly the series `sha256` (LOW-R2)")
    if cr["mode"] == "REGISTERED_SERIES" and cr["sha256"] not in CR.REGISTERED_SERIES_SHA256:
        raise PreregError("cash_rate REGISTERED_SERIES names a series that is not registered by sha256 (LOW-2; none is registered)")
    if p["delisting_primary"] != "MINUS_100_PERCENT":
        raise PreregError("the primary delisting treatment is MINUS_100_PERCENT (the last-price run is a labelled sensitivity)")
    if not (isinstance(p["initial_cash"], float) and p["initial_cash"] > 0):
        raise PreregError("initial_cash is a positive float")
    sm = p["static_map_sha256"]
    if sm is not None and not _hex64(sm):
        raise PreregError("static_map_sha256 is None or a sha256 hex digest")
    if p["dsr_basis"] not in DSR_BASES:
        raise PreregError(f"dsr_basis is one of {DSR_BASES}")
    th = p["thresholds"]
    if set(th) != {"alpha", "n_min_sessions", "min_dsr"}:
        raise PreregError("thresholds are exactly alpha, n_min_sessions, min_dsr")
    if not (isinstance(th["alpha"], float) and 0.0 < th["alpha"] < 0.5):
        raise PreregError("thresholds.alpha must be a float in (0, 0.5), fixed before any look")
    if not (isinstance(th["n_min_sessions"], int) and th["n_min_sessions"] > 0):
        raise PreregError("thresholds.n_min_sessions must be a positive integer")
    if th["min_dsr"] is not None and not (isinstance(th["min_dsr"], float) and 0.0 < th["min_dsr"] < 1.0):
        raise PreregError("thresholds.min_dsr is None or a float in (0, 1)")
    b = p["bootstrap"]
    if not (isinstance(b.get("mean_block_sessions"), int) and b["mean_block_sessions"] >= 1 and isinstance(b.get("resamples"), int)
            and b["resamples"] >= 1 and isinstance(b.get("seed"), int) and isinstance(b.get("confidence"), float) and 0.5 < b["confidence"] < 1.0):
        raise PreregError("bootstrap needs mean_block_sessions, resamples, seed (int) and confidence (float in (0.5, 1))")
    bi = p["benchmark_instrument"]
    if not (isinstance(bi, str) and bi.strip() and not bi.startswith("<")):
        raise PreregError("benchmark_instrument is the dataset's instrument_id of the gating benchmark (SPY), fixed before any look")
    if p["hypothesis_id"] not in p["family"]:
        raise PreregError("the family lists the hypothesis itself")
    if not isinstance(p["embargo_sessions"], int) or p["embargo_sessions"] < 0:
        raise PreregError("embargo_sessions is a non-negative integer")
    for label, rng in p["segments"].items():
        if label not in ("TRAIN", "VALIDATION", "TEST") or len(rng) != 2 or rng[0] > rng[1]:
            raise PreregError(f"segment {label}: [start, end] dates")
    ordered = [p["segments"][k] for k in ("TRAIN", "VALIDATION", "TEST") if k in p["segments"]]
    if any(a[1] >= b[0] for a, b in zip(ordered, ordered[1:])):
        raise PreregError("segments are chronological and never overlap: TRAIN, then VALIDATION, then TEST")
    if "TEST" not in p["segments"]:
        raise PreregError("a pre-registration names its TEST segment")
    return sha256(p)
