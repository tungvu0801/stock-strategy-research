"""strategy_validation/verdict.py — the deterministic verdict of one arm (the run's verdict is the RULES arm's: the strategy as defined).

  INVALID            an integrity failure: the arms did not receive the same candidate set, a CRITICAL causality finding, a mechanical-audit
                     violation or an arm that broke its contract. No conclusion is drawn.
  INSUFFICIENT_DATA  fewer than MIN_TRADES trades, fewer than MIN_OOS_TRADES OOS trades, or an OOS window shorter than MIN_OOS_SESSIONS.
  FAIL               gross positive but net not positive (friction-killed); OOS expectancy not positive after friction; total net not positive.
  MIXED              OOS positive but: TRAIN not positive, OOS degraded vs TRAIN, |t| < T_STAT_WEAK (weak statistical evidence), severe period
                     concentration, ambiguous-bar dependence, friction-heavy or a SENSITIVE / UNSTABLE parameter neighbourhood.
  PASS               none of the above. PASS = eligible for human review for lifecycle advancement - never an automatic promotion, never a forecast.
"""
from __future__ import annotations

from typing import List, Optional

from strategy_validation import MIN_OOS_SESSIONS, MIN_OOS_TRADES, MIN_TRADES, T_STAT_WEAK, VERDICT_LABELS

DECISION_PASS = "Eligible for review"
DECISION_OTHER = "Not ready for paper"
FAIL_NOTE = "This strategy should not progress to paper trading. A failed validation is a valid research result."


def _out(verdict: str, reasons: List[dict], warnings: List[dict]) -> dict:
    decision = DECISION_PASS if verdict == "PASS" else DECISION_OTHER
    note = ("PASS means eligible for human review for lifecycle advancement. It does not promote the strategy and does not mean future profitability."
            if verdict == "PASS" else FAIL_NOTE if verdict == "FAIL" else "")
    return {"verdict": verdict, "label": VERDICT_LABELS[verdict], "reasons": reasons, "warnings": warnings, "decision": decision, "eligible_for_review": verdict == "PASS",
            "note": note, "lifecycle_effect": "none: lifecycle changes stay an explicit human decision"}


def decide(m: dict, *, oos_sessions: int, integrity_ok: bool, integrity_problems: List[str], causality_critical: bool, audit_ok: bool,
           diagnostics: dict, sensitivity_label: Optional[str] = None) -> dict:
    warnings: List[dict] = []
    if not integrity_ok or causality_critical or not audit_ok:
        reasons = [{"code": "CANDIDATE_SET_MISMATCH", "text": p} for p in integrity_problems]
        if causality_critical:
            reasons.append({"code": "CAUSALITY_VIOLATION", "text": "A critical look-ahead / causality check failed."})
        if not audit_ok:
            reasons.append({"code": "AUDIT_FAILED", "text": "The independent mechanical audit found violations."})
        return _out("INVALID", reasons or [{"code": "INTEGRITY", "text": "Validation integrity failure."}], warnings)
    tr, oo, al = m.get("TRAIN") or {}, m.get("OOS") or {}, m.get("ALL") or {}
    n, n_oos = al.get("trade_count") or 0, oo.get("trade_count") or 0
    small = []
    if n < MIN_TRADES:
        small.append({"code": "SAMPLE_TOO_SMALL", "text": f"{n} trades; at least {MIN_TRADES} are required."})
    if n_oos < MIN_OOS_TRADES:
        small.append({"code": "OOS_SAMPLE_TOO_SMALL", "text": f"{n_oos} OOS trades; at least {MIN_OOS_TRADES} are required."})
    if oos_sessions < MIN_OOS_SESSIONS:
        small.append({"code": "OOS_WINDOW_TOO_SHORT", "text": f"{oos_sessions} OOS sessions; at least {MIN_OOS_SESSIONS} are required."})
    if small:
        return _out("INSUFFICIENT_DATA", small, warnings)
    fr = diagnostics.get("friction") or {}
    fails = []
    if fr.get("killed"):
        fails.append({"code": "FRICTION_KILLED", "text": "Gross P/L is positive but friction turns the net P/L negative."})
    if (oo.get("expectancy_R") or 0.0) <= 0:
        fails.append({"code": "OOS_NOT_POSITIVE", "text": "OOS expectancy turned negative after friction." if (oo.get("expectancy_R") or 0) < 0 else "OOS expectancy is zero after friction."})
    if (al.get("net_pnl") or 0.0) <= 0 and not fr.get("killed"):
        fails.append({"code": "NET_NOT_POSITIVE", "text": "Total net P/L is not positive."})
    if fails:
        return _out("FAIL", fails, warnings)
    mixed = []
    diag = diagnostics.get("train_vs_oos") or {}
    if diag.get("code") in ("TRAIN_WEAK_OOS_POSITIVE", "DEGRADATION"):
        mixed.append({"code": diag["code"], "text": diag["text"]})
    t = al.get("mean_R_t_stat")
    if t is None or abs(t) < T_STAT_WEAK:
        mixed.append({"code": "WEAK_STATISTICAL_EVIDENCE", "text": f"|t| = {t if t is not None else '—'} < {T_STAT_WEAK}: weak statistical evidence."})
    t_oos = oo.get("mean_R_t_stat")
    if t_oos is None or abs(t_oos) < T_STAT_WEAK:
        warnings.append({"code": "OOS_T_STAT_WEAK", "text": f"OOS |t| = {t_oos if t_oos is not None else '—'} < {T_STAT_WEAK}: the OOS slice alone is weak evidence."})
    conc = diagnostics.get("concentration") or {}
    if conc.get("severe"):
        mixed.append({"code": "PERIOD_CONCENTRATION", "text": conc.get("text") or "severe period concentration"})
    elif conc.get("warning"):
        warnings.append({"code": "PERIOD_CONCENTRATION_WARN", "text": conc.get("text")})
    amb = diagnostics.get("ambiguous") or {}
    if amb.get("dependent"):
        mixed.append({"code": "AMBIGUOUS_BAR_DEPENDENCE", "text": amb.get("text")})
    elif amb.get("warning"):
        warnings.append({"code": "AMBIGUOUS_BARS", "text": f"{amb.get('ambiguous_bars_count')} trades decided inside an ambiguous bar (stop-first)."})
    if fr.get("heavy"):
        mixed.append({"code": "FRICTION_HEAVY", "text": f"Friction takes {round((fr.get('friction_share_of_gross') or 0) * 100)} % of the gross P/L."})
    if sensitivity_label in ("SENSITIVE", "UNSTABLE"):
        mixed.append({"code": f"PARAMETERS_{sensitivity_label}", "text": f"The parameter neighbourhood is {sensitivity_label}."})
    if mixed:
        return _out("MIXED", mixed, warnings)
    return _out("PASS", [{"code": "ALL_CHECKS_PASSED", "text": "Adequate samples, positive OOS after friction, consistent with TRAIN, |t| >= 2, no concentration, audit passed."}], warnings)
