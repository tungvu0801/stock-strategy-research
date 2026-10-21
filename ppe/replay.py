"""
Historical PPE replay (DESIGN_56 §8): strictly point-in-time evaluation of PPE Stretch / Confidence for every session of a symbol,
plus SEPARATE outcome diagnostics (forward returns, excess vs benchmark, win rate, MFE / MAE) that are never used to compute or
tune PPE. Data: the Stage 3.2 point-in-time daily-bar store (historical_daily_bars), read only; benchmark per DESIGN_56 §5 using
only stored symbols; evidence per packet created_at.

CLI:  python -m ppe.replay --db <stock_agent.db> --out <dir> [--symbols A B …] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ppe import MIN_RETURN_OBS, WEIGHTS, stretch_label
from ppe.engine import compute_ppe
from ppe.inputs import Benchmark, DailyBar, MarketInputs
from ppe.models import EvidenceSnapshot, PPEResult
from ppe.sectors import basket_bars, resolve_benchmark

HORIZONS = (1, 5, 20)
FIRST_EVAL_SESSIONS = MIN_RETURN_OBS + 5                   # the first evaluated session has 65 completed sessions before it


# ---------------------------------------------------------------- stored bars (read only) -------------------------------------

def load_stored_series(db_path: str, symbols: Optional[Iterable[str]] = None) -> Dict[str, List[DailyBar]]:
    """The widest stored dataset per symbol from the Stage 3.2 point-in-time store. Read-only connection."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        rows = conn.execute("SELECT dataset_id, symbol, bar_count FROM historical_bar_datasets ORDER BY symbol, bar_count DESC, fetched_at DESC").fetchall()
        wanted = {s.upper() for s in symbols} if symbols else None
        chosen: Dict[str, str] = {}
        for ds, sym, _n in rows:
            if (wanted is None or sym in wanted) and sym not in chosen:
                chosen[sym] = ds
        out: Dict[str, List[DailyBar]] = {}
        for sym, ds in chosen.items():
            bars = conn.execute("SELECT session_date, open, high, low, close, volume FROM historical_daily_bars WHERE dataset_id = ? ORDER BY session_date", (ds,)).fetchall()
            out[sym] = [DailyBar(date=date.fromisoformat(d[:10]), open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v) if v is not None else None)
                        for d, o, h, l, c, v in bars]
        return out
    finally:
        conn.close()


def stored_symbols(db_path: str) -> List[str]:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        return sorted({r[0] for r in conn.execute("SELECT symbol FROM historical_bar_datasets")})
    finally:
        conn.close()


# ---------------------------------------------------------------- benchmark from stored data ----------------------------------

def benchmark_for(symbol: str, series: Dict[str, List[DailyBar]]) -> Optional[Benchmark]:
    syms, kind, label = resolve_benchmark(symbol, available=lambda s: bool(series.get(s)))
    if kind == "NONE":
        return None
    if kind == "PEER_BASKET":
        bars = basket_bars({p: series[p] for p in syms})
        return Benchmark(symbol="PEERS:" + ",".join(syms), kind=kind, bars=bars, label=label) if bars else None
    return Benchmark(symbol=syms[0], kind=kind, bars=series[syms[0]], label=label)


# ---------------------------------------------------------------- replay ------------------------------------------------------

@dataclass
class ReplayRow:
    symbol: str
    session: str
    stretch: Optional[float]
    stretch_status: str
    label: Optional[str]
    coverage: float
    direction: Optional[str]
    components: Dict[str, Optional[float]]
    confidence: Optional[int]
    confidence_status: str
    confidence_label: Optional[str]
    benchmark_kind: str
    # OUTCOME DIAGNOSTICS — computed separately from the score, never fed back into PPE
    fwd: Dict[str, Optional[float]]
    fwd_excess: Dict[str, Optional[float]]
    mfe_20: Optional[float]
    mae_20: Optional[float]


def _evidence_at(packets_by_symbol: Optional[Dict[str, List[Tuple[datetime, EvidenceSnapshot]]]], symbol: str, T: date) -> Optional[EvidenceSnapshot]:
    if not packets_by_symbol:
        return None
    cutoff = datetime(T.year, T.month, T.day, 23, 59, 59, tzinfo=timezone.utc)
    eligible = [(c, s) for c, s in packets_by_symbol.get(symbol, []) if c <= cutoff]
    if not eligible:
        return None
    _, snap = max(eligible, key=lambda cs: cs[0])
    age = max(0, (T - datetime.fromisoformat(snap.created_at.replace("Z", "+00:00")).date()).days)
    return snap.model_copy(update={"age_days": age})


def _outcomes(bars: List[DailyBar], i: int, bench: Optional[List[DailyBar]]) -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[float]], Optional[float], Optional[float]]:
    """Forward returns from close[i] over the HORIZONS; excess vs the benchmark close on the same sessions; MFE / MAE over 20 sessions."""
    c0 = bars[i].close
    fwd: Dict[str, Optional[float]] = {}
    ex: Dict[str, Optional[float]] = {}
    bby = {b.date: b.close for b in bench} if bench else {}
    b0 = bby.get(bars[i].date)
    for h in HORIZONS:
        j = i + h
        if j < len(bars) and c0 > 0:
            r = bars[j].close / c0 - 1.0
            fwd[str(h)] = round(r, 6)
            bj = bby.get(bars[j].date)
            ex[str(h)] = round(r - (bj / b0 - 1.0), 6) if (b0 and bj) else None
        else:
            fwd[str(h)], ex[str(h)] = None, None
    window = bars[i + 1:i + 21]
    mfe = round(max(b.high for b in window) / c0 - 1.0, 6) if window and c0 > 0 else None
    mae = round(min(b.low for b in window) / c0 - 1.0, 6) if window and c0 > 0 else None
    return fwd, ex, mfe, mae


def replay_symbol(symbol: str, bars: List[DailyBar], benchmark: Optional[Benchmark], *, start: Optional[date] = None, end: Optional[date] = None,
                  packets_by_symbol=None, market_data: str = "Stage 3.2 point-in-time store (Alpaca IEX, adjusted)") -> List[ReplayRow]:
    rows: List[ReplayRow] = []
    bars = sorted(bars, key=lambda b: b.date)
    for i in range(FIRST_EVAL_SESSIONS, len(bars)):
        T = bars[i].date
        if (start and T < start) or (end and T > end):
            continue
        pit_bars = bars[:i + 1]                                              # only sessions <= T (bar T is the evaluated one)
        bench = None
        if benchmark is not None:
            bench = Benchmark(symbol=benchmark.symbol, kind=benchmark.kind, bars=[b for b in benchmark.bars if b.date <= T], label=benchmark.label)
        inputs = MarketInputs(symbol=symbol, bars=pit_bars, as_of=T, current_price=None, benchmark=bench, mode="replay", market_data=market_data,
                              price_source="session close")
        res: PPEResult = compute_ppe(inputs, _evidence_at(packets_by_symbol, symbol, T), now_fn=lambda: 0.0)
        fwd, ex, mfe, mae = _outcomes(bars, i, benchmark.bars if benchmark else None)
        st, cf = res.stretch, res.confidence
        rows.append(ReplayRow(symbol=symbol, session=T.isoformat(), stretch=st.score, stretch_status=st.status, label=st.label, coverage=st.coverage_weight,
                              direction=st.direction, components={k: c.score for k, c in st.components.items()}, confidence=cf.score, confidence_status=cf.status,
                              confidence_label=cf.label, benchmark_kind=res.sources.benchmark_kind, fwd=fwd, fwd_excess=ex, mfe_20=mfe, mae_20=mae))
    return rows


# ---------------------------------------------------------------- summary -----------------------------------------------------

def _pct(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _med(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return round(statistics.median(v), 6) if v else None


def summarize(rows: List[ReplayRow]) -> dict:
    scored = [r for r in rows if r.stretch is not None]
    scores = [r.stretch for r in scored]
    n = len(rows)
    bands = Counter(r.label for r in scored)
    comp_avail = defaultdict(int)
    for r in scored:
        for k, v in r.components.items():
            comp_avail[k] += 1 if v is not None else 0
    out = {
        "observations": n, "scored": len(scored), "insufficient_data": sum(1 for r in rows if r.stretch_status == "INSUFFICIENT_DATA"),
        "symbols": sorted({r.symbol for r in rows}), "sessions": [min(r.session for r in rows), max(r.session for r in rows)] if rows else None,
        "stretch_distribution": {"mean": round(statistics.fmean(scores), 3) if scores else None, "median": _med(scores),
                                 "std": round(statistics.pstdev(scores), 3) if len(scores) > 1 else None,
                                 "p01": _pct(scores, 0.01), "p05": _pct(scores, 0.05), "p25": _pct(scores, 0.25), "p75": _pct(scores, 0.75),
                                 "p95": _pct(scores, 0.95), "p99": _pct(scores, 0.99),
                                 "share_extreme_downside": round(bands.get("EXTREME_DOWNSIDE", 0) / len(scored), 4) if scored else None,
                                 "share_extreme_upside": round(bands.get("EXTREME_UPSIDE", 0) / len(scored), 4) if scored else None,
                                 "share_abs_over_40": round(sum(1 for s in scores if abs(s) > 40) / len(scored), 4) if scored else None},
        "bands": {b: bands.get(b, 0) for b in ("EXTREME_DOWNSIDE", "DOWNSIDE_STRETCHED", "NORMAL", "UPSIDE_STRETCHED", "EXTREME_UPSIDE")},
        "component_availability": {k: round(v / len(scored), 4) for k, v in comp_avail.items()} if scored else {},
        "benchmark_kinds": dict(Counter(r.benchmark_kind for r in rows)),
        "coverage": {"mean": round(statistics.fmean(r.coverage for r in rows), 4) if rows else None, "min": min((r.coverage for r in rows), default=None)},
        "confidence": dict(Counter(r.confidence_status for r in rows)),
    }
    # sanity: 5-day realised return by band (the move the score is supposed to describe; this is NOT an outcome)
    by_band = defaultdict(list)
    for r in scored:
        by_band[r.label].append(r)
    out["band_consistency"] = {}
    for band, rs in by_band.items():
        r5 = [r.components.get("abnormal_return") for r in rs]
        out["band_consistency"][band] = {"n": len(rs), "median_abnormal_return_component": _med(r5),
                                         "median_price_dislocation_component": _med([r.components.get("price_dislocation") for r in rs])}
    # OUTCOME DIAGNOSTICS — descriptive only, not used anywhere in the score
    diag = {}
    for band, rs in by_band.items():
        d = {"n": len(rs)}
        for h in HORIZONS:
            v = [r.fwd[str(h)] for r in rs if r.fwd.get(str(h)) is not None]
            e = [r.fwd_excess[str(h)] for r in rs if r.fwd_excess.get(str(h)) is not None]
            d[f"median_fwd_{h}d"] = _med(v)
            d[f"median_excess_{h}d"] = _med(e)
            d[f"win_rate_{h}d"] = round(sum(1 for x in v if x > 0) / len(v), 4) if v else None
        d["median_mfe_20d"] = _med([r.mfe_20 for r in rs])
        d["median_mae_20d"] = _med([r.mae_20 for r in rs])
        diag[band] = d
    out["OUTCOME_DIAGNOSTICS"] = {"note": "Descriptive forward returns after each observation. Not used to compute or tune PPE; not evidence of predictive power.",
                                  "by_band": diag}
    return out


def regime_notes(spy: List[DailyBar]) -> dict:
    """Factual description of the market regimes in the replay window from SPY: drawdowns and realised volatility."""
    if len(spy) < 30:
        return {}
    closes = [b.close for b in spy]
    peak, dd_min, dd_date = closes[0], 0.0, spy[0].date
    for b, c in zip(spy, closes):
        peak = max(peak, c)
        dd = c / peak - 1.0
        if dd < dd_min:
            dd_min, dd_date = dd, b.date
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    vol20 = [statistics.pstdev(rets[i - 20:i]) * (252 ** 0.5) for i in range(20, len(rets) + 1)]
    return {"spy_start": spy[0].date.isoformat(), "spy_end": spy[-1].date.isoformat(), "spy_total_return": round(closes[-1] / closes[0] - 1.0, 4),
            "spy_max_drawdown": round(dd_min, 4), "spy_max_drawdown_date": dd_date.isoformat(),
            "spy_realised_vol_20d_min": round(min(vol20), 4), "spy_realised_vol_20d_max": round(max(vol20), 4), "spy_realised_vol_20d_median": round(statistics.median(vol20), 4)}


def load_packet_snapshots(db_path: str, symbols: Iterable[str]) -> Dict[str, List[Tuple[datetime, EvidenceSnapshot]]]:
    """Every COMPLETED stored packet per symbol as (created_at, snapshot) — the replay makes each visible only from created_at on."""
    from evidence.store import EvidenceStore
    from ppe.evidence_context import _dt, snapshot_from_packet
    store = EvidenceStore(Path(db_path))
    out: Dict[str, List[Tuple[datetime, EvidenceSnapshot]]] = defaultdict(list)
    if not store.exists():
        return out
    for s in symbols:
        for p in store.packets(limit=200, ticker=s):
            c = _dt(p.get("created_at"))
            if p.get("status") != "COMPLETED" or c is None:
                continue
            out[s].append((c, snapshot_from_packet(p, store.items(p["packet_id"]), c)))
    return out


def run(db_path: str, out_dir: Path, symbols: Optional[List[str]] = None, start: Optional[date] = None, end: Optional[date] = None,
        packets_by_symbol=None, progress: Optional[Callable[[str], None]] = None, evidence_db: Optional[str] = None) -> dict:
    all_series = load_stored_series(db_path)
    universe = [s for s in (symbols or sorted(all_series)) if s in all_series and s != "SPY"]
    if packets_by_symbol is None:
        packets_by_symbol = load_packet_snapshots(evidence_db or db_path, universe)
    rows: List[ReplayRow] = []
    per_symbol = {}
    for sym in universe:
        bench = benchmark_for(sym, all_series)
        r = replay_symbol(sym, all_series[sym], bench, start=start, end=end, packets_by_symbol=packets_by_symbol)
        rows.extend(r)
        per_symbol[sym] = {"rows": len(r), "benchmark": bench.symbol if bench else None, "benchmark_kind": bench.kind if bench else "NONE",
                           "min_stretch": min((x.stretch for x in r if x.stretch is not None), default=None),
                           "max_stretch": max((x.stretch for x in r if x.stretch is not None), default=None)}
        if progress:
            progress(f"{sym}: {len(r)} sessions, benchmark {per_symbol[sym]['benchmark_kind']}")
    summary = summarize(rows)
    summary["per_symbol"] = per_symbol
    summary["regimes"] = regime_notes([b for b in all_series.get("SPY", []) if (not start or b.date >= start) and (not end or b.date <= end)])
    summary["weights"] = WEIGHTS
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ppe_replay_rows.jsonl").write_text("\n".join(json.dumps(asdict(r), separators=(",", ":")) for r in rows) + "\n", encoding="utf-8")
    (out_dir / "ppe_replay_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Point-in-time PPE replay over the stored daily bars (read only).")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--evidence-db", help="database holding Stage 5.4 packets (default: --db)")
    a = p.parse_args(argv)
    s = run(a.db, Path(a.out), a.symbols, date.fromisoformat(a.start) if a.start else None, date.fromisoformat(a.end) if a.end else None, progress=print,
            evidence_db=a.evidence_db)
    summary = {k: s[k] for k in ("observations", "scored", "stretch_distribution", "bands", "confidence")}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
