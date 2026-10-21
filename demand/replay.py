"""
Market Demand validation (DESIGN_57 §8) — descriptive, point-in-time, never used to tune weights or bands.

  python -m demand.replay --db data/stock_agent.db --out <dir> [--symbols ...] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--sim-symbols 40]

DAILY FALLBACK VALIDATION: the Stage 3.2 point-in-time daily store (read only; loaders reused from ppe.replay). Each session T of each
symbol is evaluated in DAILY_MODE with bars strictly before T, exactly like the live daily fallback. Outcome diagnostics (forward 1 / 5 / 20
session returns from the last used close, excess vs the benchmark) are computed separately afterwards and are descriptive only.
INTRADAY VALIDATION (SYNTHETIC): the project has no intraday history store, so intraday behaviour is validated on deterministic
synthetic sessions — constructed scenarios plus simulator sessions over a diversified symbol list at several checkpoints — and is
labelled SYNTHETIC. Daily bars are never presented as validating VWAP or intraday structure.
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from demand import BANDS, MIN_DAILY_BARS, WEIGHTS
from demand.engine import compute_demand
from demand.inputs import BenchmarkInputs, DemandInputs, MinuteBar
from ppe.inputs import Benchmark, DailyBar
from ppe.replay import _outcomes, _pct, benchmark_for, load_stored_series, regime_notes, stored_symbols  # noqa: F401 - reused loaders

HORIZONS = (1, 5, 20)
FIRST_EVAL = MIN_DAILY_BARS + 5
SIM_UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "MU", "SNDK", "JPM", "BAC", "GS", "PFE", "LLY", "UNH", "KO", "PG", "WMT", "HD", "CAT", "DE",
                "XOM", "CVX", "ORCL", "CRM", "SHOP", "MRVL", "CLS", "MCD", "COST", "BA", "HON", "ABBV", "MRK", "NKE", "SBUX", "GE", "SLB", "COP",
                "TXN", "QCOM", "INTC"]
SIM_CHECKPOINTS = (60, 120, 180, 240)


@dataclass
class DailyRow:
    symbol: str
    date: str
    score: Optional[int]
    label: Optional[str]
    coverage: float
    status: str
    benchmark_kind: str
    a: Optional[float]
    d: Optional[float]
    e: Optional[float]
    f: Optional[float]
    rv: Optional[float]
    close_location: Optional[float]
    rel_1d: Optional[float]
    fwd: Dict[str, Optional[float]]
    excess: Dict[str, Optional[float]]


def _bench_inputs(b: Optional[Benchmark]) -> Optional[BenchmarkInputs]:
    return BenchmarkInputs(symbol=b.symbol, kind=b.kind, label=b.label, daily=list(b.bars)) if b else None


def replay_daily(symbol: str, bars: List[DailyBar], bench: Optional[Benchmark], *, start: Optional[date] = None, end: Optional[date] = None) -> List[DailyRow]:
    rows: List[DailyRow] = []
    bench_bars = list(bench.bars) if bench else None
    bi = _bench_inputs(bench)
    for i in range(FIRST_EVAL, len(bars)):
        T = bars[i].date
        if (start and T < start) or (end and T > end):
            continue
        now = int(datetime(T.year, T.month, T.day, 14, 30, tzinfo=timezone.utc).timestamp())
        inp = DemandInputs(symbol=symbol, now=now, session_date=T, session_bars=[], prior_sessions={}, daily_bars=bars[:i], benchmark=bi, mode="replay",
                           market_data="Stage 3.2 point-in-time daily store (replay)", price_source="last completed close", session_complete=True)
        r = compute_demand(inp, now_fn=lambda: now)
        c = r.components
        fwd, ex, _mfe, _mae = _outcomes(bars, i - 1, bench_bars)
        rows.append(DailyRow(symbol=symbol, date=T.isoformat(), score=r.score, label=r.label, coverage=r.coverage_weight, status=r.status,
                             benchmark_kind=r.sources.benchmark_kind,
                             a=c["relative_volume"].score, d=c["range_strength"].score, e=c["relative_strength"].score, f=c["follow_through"].score,
                             rv=c["relative_volume"].raw.get("relative_volume"), close_location=c["range_strength"].raw.get("close_location"),
                             rel_1d=(c["relative_strength"].raw.get("horizons") or {}).get("1d"), fwd=fwd, excess=ex))
    return rows


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return round(statistics.fmean(v), 4) if v else None


def _med(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return round(statistics.median(v), 6) if v else None


def summarize_daily(rows: List[DailyRow]) -> dict:
    scored = [r for r in rows if r.score is not None]
    scores = [float(r.score) for r in scored]
    bands = {name: 0 for _, _, name in BANDS}
    for r in scored:
        bands[r.label] += 1
    by_band = {}
    for name in bands:
        sel = [r for r in scored if r.label == name]
        by_band[name] = {"n": len(sel), "mean_close_location": _mean([r.close_location for r in sel]), "mean_relative_volume": _mean([r.rv for r in sel]),
                         "mean_rel_1d_pct": _mean([r.rel_1d for r in sel]), "mean_component_scores": {k: _mean([getattr(r, k) for r in sel]) for k in ("a", "d", "e", "f")},
                         "outcome_diagnostics_descriptive_only": {f"median_fwd_{h}d": _med([r.fwd.get(str(h)) for r in sel]) for h in HORIZONS} |
                                                                  {f"median_excess_{h}d": _med([r.excess.get(str(h)) for r in sel]) for h in HORIZONS}}
    insufficient = [r for r in rows if r.score is None]
    return {"mode": "DAILY_MODE (completed daily bars only; components B and C NOT_AVAILABLE by design; coverage at the 0.60 floor)",
            "observations": len(rows), "scored": len(scored), "insufficient_data": len(insufficient),
            "insufficient_reasons": {"missing volume / history": len(insufficient)},
            "symbols": len({r.symbol for r in rows}), "date_range": [min(r.date for r in rows), max(r.date for r in rows)] if rows else None,
            "score": {"mean": _mean(scores), "median": _med(scores), "std": round(statistics.pstdev(scores), 3) if len(scores) > 1 else None,
                      "p05": _pct(scores, 0.05), "p25": _pct(scores, 0.25), "p75": _pct(scores, 0.75), "p95": _pct(scores, 0.95), "min": min(scores) if scores else None,
                      "max": max(scores) if scores else None},
            "bands": bands, "band_share": {k: round(v / len(scored), 4) if scored else None for k, v in bands.items()},
            "extreme_share": round(sum(1 for r in scored if r.label in ("VERY_WEAK", "VERY_STRONG")) / len(scored), 4) if scored else None,
            "by_band": by_band, "benchmark_kinds": {k: sum(1 for r in rows if r.benchmark_kind == k) for k in ("SECTOR_ETF", "PEER_BASKET", "MARKET", "NONE")},
            "coverage_values": sorted({r.coverage for r in rows})}


# ---------------------------------------------------------------- synthetic intraday validation --------------------------------

def _session(start: int, closes: Sequence[float], volumes: Sequence[float], spread: float = 0.2) -> List[MinuteBar]:
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        o = prev
        out.append(MinuteBar(time=start + 60 * i, open=o, high=max(o, c) + spread, low=min(o, c) - spread, close=c, volume=float(v), final=True))
        prev = c
    return out


def synthetic_scenarios(now: int = 1_800_000_000) -> Dict[str, DemandInputs]:
    """Constructed sessions with known structure (prices around 100, 90 completed bars, three flat prior sessions for the volume curve)."""
    start = now - 91 * 60
    flat_prior = {f"p{k}": _session(start - (k + 1) * 86400, [100.0] * 390, [1000.0] * 390) for k in range(3)}
    T = date(2027, 1, 15)
    daily = [DailyBar(date=date(2026, 12, 1), open=100, high=101, low=99, close=100, volume=1e6)] * 0
    base = dict(symbol="SYN", now=now, session_date=T, prior_sessions=flat_prior, daily_bars=daily, prev_close=100.0, session_open=100.0, mode="replay",
                market_data="synthetic session (validation)", price_source="synthetic")
    rising = [100.0 + 0.02 * i for i in range(90)]
    falling = [100.0 - 0.02 * i for i in range(90)]
    flat = [100.0 + (0.01 if i % 2 else -0.01) for i in range(90)]
    bounce = [100.0 - 0.02 * i for i in range(89)] + [100.0 - 0.02 * 88 + 0.3]
    brief = [100.0 - 0.004 * i + (0.03 if i % 2 else -0.03) for i in range(88)] + [99.75 + 0.25, 99.75 + 0.30]   # a drifting-flat session; the last two closes pop just above VWAP
    held = [100.0 - 0.01 * i for i in range(60)] + [99.4 + 0.03 * j for j in range(30)]
    s = {}
    s["neutral"] = DemandInputs(session_bars=_session(start, flat, [1000.0] * 90), current_price=flat[-1], **base)
    s["weak_below_vwap_thin"] = DemandInputs(session_bars=_session(start, falling, [500.0] * 90), current_price=falling[-1], **base)
    s["strong_hold_high_volume"] = DemandInputs(session_bars=_session(start, rising, [1800.0] * 90), current_price=rising[-1], **base)
    s["volume_spike_no_acceptance"] = DemandInputs(session_bars=_session(start, falling, [3000.0] * 90), current_price=falling[-1], **base)
    s["brief_vwap_cross"] = DemandInputs(session_bars=_session(start, brief, [1000.0] * 90), current_price=brief[-1], **base)
    s["reclaim_held"] = DemandInputs(session_bars=_session(start, held, [1200.0] * 90), current_price=held[-1], **base)
    s["one_bar_bounce"] = DemandInputs(session_bars=_session(start, bounce, [1000.0] * 90), current_price=bounce[-1], **base)
    return s


def simulator_sessions(symbols: Sequence[str], seed: int = 7, checkpoints: Sequence[int] = SIM_CHECKPOINTS) -> List[dict]:
    """Deterministic simulator sessions (trader_view.simulated) scored at several checkpoints — SYNTHETIC data, labelled as such."""
    from trader_view.demand_provider import DemandProvider
    from trader_view.simulated import SimulatedMarket
    rows = []
    for sym in symbols:
        for cp in checkpoints:
            market = SimulatedMarket(seed=seed, start_time=1_800_000_000, backfill_minutes=cp, symbols=[sym])
            prov = DemandProvider(market, now_fn=lambda: 1_800_000_000.0)
            r = prov.result(sym)
            rows.append({"symbol": sym, "checkpoint_bars": cp, "mode": r.mode, "status": r.status, "score": r.score, "label": r.label, "coverage": r.coverage_weight,
                         "components": {k: c.score for k, c in r.components.items()}})
    return rows


def summarize_synthetic(scen: Dict[str, dict], sim_rows: List[dict]) -> dict:
    scores = [float(r["score"]) for r in sim_rows if r["score"] is not None]
    checks = {
        "neutral session scores near 50": abs(scen["neutral"]["score"] - 50) <= 10,
        "weak participation below VWAP scores low": scen["weak_below_vwap_thin"]["score"] < 40,
        "high participation with a sustained VWAP hold scores high": scen["strong_hold_high_volume"]["score"] >= 75,
        "volume spike without acceptance stays below neutral and is not extreme": scen["volume_spike_no_acceptance"]["score"] < 50 and scen["volume_spike_no_acceptance"]["components"]["relative_volume"] <= 50,
        "brief VWAP cross without persistence stays below STRENGTHENING": scen["brief_vwap_cross"]["score"] < 60 and scen["brief_vwap_cross"]["components"]["vwap_acceptance"] < 60,
        "held reclaim scores well above the brief cross": scen["reclaim_held"]["score"] >= scen["brief_vwap_cross"]["score"] + 15,
        "one-bar bounce does not over-score": scen["one_bar_bounce"]["score"] < 50,
    }
    return {"label": "SYNTHETIC — constructed scenarios and deterministic simulator sessions; not market data", "scenarios": scen, "scenario_checks": checks,
            "simulator": {"rows": len(sim_rows), "symbols": len({r["symbol"] for r in sim_rows}), "checkpoints": sorted({r["checkpoint_bars"] for r in sim_rows}),
                          "modes": {m: sum(1 for r in sim_rows if r["mode"] == m) for m in ("INTRADAY_MODE", "DAILY_MODE")},
                          "scored": len(scores), "score": {"mean": _mean(scores), "median": _med(scores), "p05": _pct(scores, 0.05), "p95": _pct(scores, 0.95),
                                                           "min": min(scores) if scores else None, "max": max(scores) if scores else None},
                          "bands": {name: sum(1 for r in sim_rows if r["label"] == name) for _, _, name in BANDS}}}


def run(db_path: str, out_dir: Path, symbols: Optional[List[str]] = None, start: Optional[date] = None, end: Optional[date] = None,
        sim_symbols: int = len(SIM_UNIVERSE), progress: Optional[Callable[[str], None]] = None) -> dict:
    series = load_stored_series(db_path)
    universe = [s for s in (symbols or sorted(series)) if s in series and s != "SPY"]
    rows: List[DailyRow] = []
    per_symbol = {}
    for sym in universe:
        bench = benchmark_for(sym, series)
        r = replay_daily(sym, series[sym], bench, start=start, end=end)
        rows.extend(r)
        per_symbol[sym] = {"rows": len(r), "benchmark_kind": bench.kind if bench else "NONE", "scored": sum(1 for x in r if x.score is not None),
                           "mean_score": _mean([x.score for x in r])}
        if progress:
            progress(f"{sym}: {len(r)} sessions, benchmark {per_symbol[sym]['benchmark_kind']}")
    daily = summarize_daily(rows)
    daily["per_symbol"] = per_symbol
    daily["regimes"] = regime_notes([b for b in series.get("SPY", []) if (not start or b.date >= start) and (not end or b.date <= end)])
    scen = {}
    for name, inp in synthetic_scenarios().items():
        r = compute_demand(inp, now_fn=lambda: inp.now)
        scen[name] = {"score": r.score, "label": r.label, "mode": r.mode, "coverage": r.coverage_weight, "components": {k: c.score for k, c in r.components.items()},
                      "key_factors": r.key_factors}
    sim_rows = simulator_sessions(SIM_UNIVERSE[:sim_symbols]) if sim_symbols else []
    summary = {"weights": WEIGHTS, "daily_fallback_validation": daily, "intraday_validation_synthetic": summarize_synthetic(scen, sim_rows),
               "note": "weights, scales and bands were fixed in DESIGN_57 before this replay and were not changed by it; outcome diagnostics are descriptive only"}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "demand_replay_daily_rows.jsonl").write_text("\n".join(json.dumps(asdict(r), separators=(",", ":")) for r in rows) + "\n", encoding="utf-8")
    (out_dir / "demand_replay_simulator_rows.json").write_text(json.dumps(sim_rows, indent=1), encoding="utf-8")
    (out_dir / "demand_replay_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m demand.replay", description=__doc__.strip().splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--sim-symbols", type=int, default=len(SIM_UNIVERSE))
    a = p.parse_args(argv)
    s = run(a.db, Path(a.out), a.symbols, date.fromisoformat(a.start) if a.start else None, date.fromisoformat(a.end) if a.end else None, a.sim_symbols, progress=print)
    d = s["daily_fallback_validation"]
    print(json.dumps({"observations": d["observations"], "scored": d["scored"], "score": d["score"], "bands": d["bands"], "extreme_share": d["extreme_share"],
                      "synthetic_checks": s["intraday_validation_synthetic"]["scenario_checks"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
