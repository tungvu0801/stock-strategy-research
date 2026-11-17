"""md_eval/benchmarks.py - the benchmarks, all through the same engine and costs where a book is involved:
  SPY total return      daily close-to-close returns of raw_close / total_return_factor (P3: total return only for benchmarks and
                        reconciliation); a session without both values is UNAVAILABLE (None), never filled
  SPY buy-and-hold      the same engine (BuyAndHold, the BENCHMARK book): the ledger's raw-price-plus-events return, reconciled to the total return
  exposure-matched SPY  E(t-1) = gross long / equity at the close of t-1, clamped to [0, 1]; r(t) = E(t-1) r_SPY(t) + (1 - E(t-1)) r_cash(t);
                        with rebalancing cost: each session the benchmark trades from its drifted weight to E(t-1) at the SPY bucket's
                        friction rate (slippage + half-spread; regulatory fees not applied to this synthetic benchmark)
  C1                    SwingBridge(use_pullback_rule=False) through the frozen book
  random entry (B3)     RandomEntry for each recorded seed: report only, never fed back into V1
  B4                    EqualWeight over the universe at the first session, through the BENCHMARK book"""
from __future__ import annotations

import hashlib
import json
import math
from typing import List, Optional, Sequence

from md_eval import engine as EN
from md_eval import metrics as MT
from md_eval.contract import is_missing, licence_gate, unavailable
from md_eval.signals import RandomEntry, SwingBridge


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def total_return_series(provider, instrument_id: str, sessions: Sequence[str]) -> List[Optional[float]]:
    """Daily total returns aligned to `sessions` (the first is 0.0); None where either close is unavailable (reporting, full history).
    MED-2: the shared licence gate first - a HISTORICAL dataset gives no returns unless personal_research_use is CONFIRMED.
    A dataset without the TOTAL_RETURN view gives the explicit UNAVAILABLE (P3, P4): never raw prices in its place."""
    m = provider.manifest()
    licence_gate(m)
    if "TOTAL_RETURN" not in tuple(getattr(m, "price_views_available", ()) or ()):
        return unavailable("TOTAL_RETURN_VIEW_UNAVAILABLE")
    rows = {b.session: b for b in provider.bars(instrument_id, sessions[0], sessions[-1])}
    tr = {}
    for s, b in rows.items():
        if _num(b.raw_close) and _num(b.total_return_factor) and b.total_return_factor > 0:
            tr[s] = b.raw_close / b.total_return_factor
    out: List[Optional[float]] = [0.0]
    for a, b in zip(sessions, sessions[1:]):
        out.append(tr[b] / tr[a] - 1.0 if a in tr and b in tr else None)
    return out


def bound_series(provider, instrument_id: str, sessions: Sequence[str]):
    """(the series as recorded, its identity): the pre-registered benchmark's total-return series from the run's own provider and sessions, or
    the canonical UNAVAILABLE object; the identity carries the instrument, the status and the series' sha256 (bound in the run identity and in
    the holdout ledger's run entry)."""
    series = total_return_series(provider, instrument_id, sessions)
    recorded = series.canonical() if is_missing(series) else list(series)
    status = recorded["status"] if isinstance(recorded, dict) else "AVAILABLE"
    return recorded, {"instrument_id": instrument_id, "status": status, "series_sha256": series_sha256(recorded)}


def series_sha256(recorded) -> str:
    return hashlib.sha256(json.dumps(recorded, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def exposure_matched(result: dict, spy_returns: Sequence[Optional[float]], friction_rate: float = 0.0) -> dict:
    rows = result["rows"]
    if len(rows) != len(spy_returns):
        raise ValueError("the SPY series must align with the run's sessions")
    E = [min(1.0, max(0.0, r["gross_long"] / r["equity"])) if r["equity"] > 0 else 0.0 for r in rows]
    plain: List[Optional[float]] = [0.0]
    costed: List[Optional[float]] = [0.0]
    w = 0.0                                                   # the benchmark's weight after the previous session's drift (starts in cash)
    unavailable = 0
    for t in range(1, len(rows)):
        e, rs, rc = E[t - 1], spy_returns[t], rows[t]["cash_return"]
        cost = abs(e - w) * friction_rate
        if rs is None:
            unavailable += 1
            plain.append(None)
            costed.append(None)
            w = e
            continue
        r = e * rs + (1.0 - e) * rc
        plain.append(r)
        costed.append(r - cost)
        w = e * (1.0 + rs) / (1.0 + r) if (1.0 + r) else 0.0
    return {"exposure": E, "returns": plain, "returns_with_rebalancing_cost": costed, "unavailable_sessions": unavailable,
            "friction_rate": friction_rate, "definition": "E(t-1) = gross long / equity, clamped to [0, 1]; r = E r_SPY + (1 - E) r_cash"}


def daily_differences(result: dict, bench: Sequence[Optional[float]]) -> List[float]:
    """r_strategy(t) - r_bench(t) for the sessions after the first where the benchmark is available."""
    rets = MT.daily_returns([r["equity"] for r in result["rows"]])
    return [rets[t] - bench[t] for t in range(1, len(rets)) if bench[t] is not None]


def random_control(provider, cfg, universe_id: str, seeds: Sequence[int], static_map=None, registry=None, holdout=None) -> dict:
    """B3, report only: one engine run per recorded seed, the same frozen book and costs as V1; never in the verdict. The licence gate applies
    first. Never on the TEST segment (it would consume holdout looks for a control that never decides anything). On HISTORICAL data only as a
    registered, recorded VALIDATION run (LOW-N1): each seed's run goes through the engine's segment guard and is recorded as a VALIDATION_RUN
    with role RANDOM_CONTROL (its seed is in the run identity); an unregistered HISTORICAL use is refused. A synthetic run labelled VALIDATION
    takes the same path."""
    m = provider.manifest()
    licence_gate(m)
    if cfg.segment_label == "TEST":
        raise EN.RunRefused("the random-entry control is report-only: refused on the TEST segment")
    if m.kind == "HISTORICAL" and (cfg.segment_label != "VALIDATION" or registry is None or holdout is None):
        raise EN.RunRefused("the random-entry control on HISTORICAL data runs only as a registered, recorded VALIDATION run")
    runs = []
    for seed in seeds:
        src = RandomEntry(SwingBridge(True, universe_id, static_map), SwingBridge(False, universe_id, static_map), seed)
        res = EN.run(provider, src, cfg, registry=registry, holdout=holdout)
        sm = MT.summary(res)
        runs.append({"seed": int(seed), "final_equity": res["final_equity"], "cagr": sm["cagr"], "sharpe_excess": sm["sharpe_excess"],
                     "identity_sha256": res["identity_sha256"], "result_sha256": res["result_sha256"]})
    return {"seeds": [int(s) for s in seeds], "runs": runs, "role": "REPORT_ONLY_NEVER_TUNES_V1"}


def percentile_of(value: float, draws: Sequence[float]) -> Optional[float]:
    xs = [d for d in draws if d is not None]
    if not xs:
        return None
    return sum(1 for d in xs if d < value) / len(xs)
