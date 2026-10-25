"""strategy_validation/causality.py — look-ahead / causality checks. AUTOMATED_CHECKS catch known patterns only:

  static (adapter source)   negative shift (future values), centered rolling windows, future-bar index arithmetic
  runtime (every candidate) a feature dated after the signal close, features without a date (unverifiable), a current-session value used by a
                            signal that is not a session-close signal, an entry at the signal close without a declared market-on-close assumption
  point in time             the dataset is cut at the signal date of sampled candidates (no bar after it exists) and the candidates are generated
                            again: every candidate signalled on or before the cut must come back identical. A difference means generation used
                            data from after the signal - including a SELECTION on future bars that no dated feature reveals

The simulator itself never fills before the signal is known (next-bar open by default; exits are scanned only after the entry fill), and the
mechanical audit re-checks the ordering of every trade. None of this proves causality: MANUAL_CAUSALITY_REVIEW_REQUIRED is always reported, and a
CRITICAL automated finding makes the run INVALID (no conclusion is drawn from a contaminated run)."""
from __future__ import annotations

import inspect
import re
from typing import Callable, Iterable, List, Optional

from strategy_validation.models import CausalityReport, StrategyCandidate

STATIC_PATTERNS = (
    ("FUTURE_SHIFT", "CRITICAL", re.compile(r"\.shift\(\s*(?:periods\s*=\s*)?-\s*\d"), "a negative shift reads values from later rows"),
    ("CENTERED_WINDOW", "CRITICAL", re.compile(r"center\s*=\s*True"), "a centered rolling window uses rows after the evaluation row"),
    ("FUTURE_INDEX", "WARN", re.compile(r"\[\s*(?:i|t|idx|k|n|j)\s*\+\s*\d+\s*\]"), "index arithmetic into a later row (verify it is not a future bar)"),
)


def static_scan(source: str) -> List[dict]:
    out = []
    for code, level, rx, text in STATIC_PATTERNS:
        hits = rx.findall(source or "")
        out.append({"check": f"STATIC_{code}", "result": level if hits else "PASS", "count": len(hits), "detail": text if hits else "pattern not found",
                    "kind": "AUTOMATED"})
    return out


def adapter_source(adapter) -> str:
    try:
        return inspect.getsource(type(adapter))
    except (OSError, TypeError):
        return ""


def runtime_checks(adapter, candidates: Iterable[StrategyCandidate]) -> List[dict]:
    after, undated, current, n = [], [], [], 0
    for c in candidates:
        n += 1
        for f in c.features:
            d = c.feature_as_of.get(f)
            if d is None:
                undated.append(f"{c.symbol} {c.signal_date} {f}")
            elif d > c.signal_date:
                after.append(f"{c.symbol} {c.signal_date} {f}@{d}")
            elif d == c.signal_date and c.signal_basis != "SESSION_CLOSE":
                current.append(f"{c.symbol} {c.signal_date} {f}")
    moc_bad = getattr(adapter, "entry_policy", "NEXT_BAR_OPEN") == "SIGNAL_CLOSE_MOC" and not getattr(adapter, "moc_declared", False)
    return [
        {"check": "RUNTIME_FEATURE_AFTER_SIGNAL", "result": "CRITICAL" if after else "PASS", "count": len(after), "kind": "AUTOMATED",
         "detail": ("feature dated after the signal close: " + "; ".join(after[:5])) if after else f"every dated feature of {n} candidates is at or before its signal close"},
        {"check": "RUNTIME_FEATURE_UNDATED", "result": "WARN" if undated else "PASS", "count": len(undated), "kind": "AUTOMATED",
         "detail": ("features without an as-of date cannot be verified: " + "; ".join(undated[:5])) if undated else "every feature carries an as-of date"},
        {"check": "RUNTIME_CURRENT_SESSION_VALUE", "result": "CRITICAL" if current else "PASS", "count": len(current), "kind": "AUTOMATED",
         "detail": ("a same-session value used before the session completed: " + "; ".join(current[:5])) if current else "signals are session-close signals or use earlier values"},
        {"check": "RUNTIME_ENTRY_AT_SIGNAL_CLOSE", "result": "CRITICAL" if moc_bad else "PASS", "count": 1 if moc_bad else 0, "kind": "AUTOMATED",
         "detail": "an entry at the signal close needs an explicit market-on-close assumption" if moc_bad else f"entry policy {getattr(adapter, 'entry_policy', 'NEXT_BAR_OPEN')}: the fill is not before the signal is known"},
        {"check": "SIMULATOR_EXIT_AFTER_ENTRY", "result": "PASS", "count": 0, "kind": "STRUCTURAL",       # by construction of the simulator, not a scan of this run
         "detail": "exits are scanned only on bars after the entry fill; a close-triggered decision never uses that bar's high / low (re-checked by the mechanical audit)"},
    ]


def truncation_check(regenerate: Callable, dataset, candidates: List[StrategyCandidate], samples: int = 5, extra_cuts: Iterable[str] = ()) -> dict:
    """regenerate(dataset) -> the normalized candidates of that dataset. Cuts at up to `samples` candidates' own signal dates, spread over the set,
    plus `extra_cuts` (Stage 6.3.1: the TRAIN end - no TRAIN candidate may depend on an OOS bar)."""
    n = len(candidates)
    if not n:
        return {"check": "RUNTIME_TRUNCATION_INVARIANCE", "result": "PASS", "count": 0, "kind": "AUTOMATED", "detail": "no candidate to cut at"}
    cuts = sorted({candidates[i].signal_date for i in sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1})[:samples]} | {c for c in extra_cuts if c})
    diffs = []
    for cut in cuts:
        want = {c.candidate_id for c in candidates if c.signal_date <= cut}
        got = {c.candidate_id for c in regenerate(dataset.until(cut)) if c.signal_date <= cut}
        if got != want:
            diffs.append(f"cut {cut}: {len(want - got)} missing, {len(got - want)} different / new")
    return {"check": "RUNTIME_TRUNCATION_INVARIANCE", "result": "CRITICAL" if diffs else "PASS", "count": len(diffs), "kind": "AUTOMATED",
            "detail": ("candidates change when later bars are removed (generation used data from after the signal): " + "; ".join(diffs)) if diffs
            else f"identical candidates at {len(cuts)} point-in-time cuts ({', '.join(cuts)})"}


def report(adapter, candidates: Iterable[StrategyCandidate], dataset=None, regenerate: Optional[Callable] = None, extra_cuts: Iterable[str] = ()) -> CausalityReport:
    candidates = list(candidates)
    checks = static_scan(adapter_source(adapter)) + runtime_checks(adapter, candidates)
    if dataset is not None and regenerate is not None:
        checks.append(truncation_check(regenerate, dataset, candidates, extra_cuts=extra_cuts))
    return CausalityReport(automated_checks=checks, critical=any(c["result"] == "CRITICAL" for c in checks), manual_review_required=True)
