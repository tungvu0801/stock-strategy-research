"""tests/momentum_fixtures.py — SYNTHETIC, deterministic price fixtures for the Stage 6.5 Momentum tests.

Test-only: invented price paths under real universe tickers (the adapter ranks momentum.RESEARCH_UNIVERSE), never written to a real database (the
store refuses SYNTHETIC runs outside a test process). Each stock: a raw close path price x (1 + drift)^i x (1 + wave), 2M shares a session, an
issuer id, an optional split (date, ratio) that divides the RAW prices from that date on - the all-adjusted bars are on today's basis (raw / ratio
before the split), exactly like the real bundle. `bumps` multiply the path from a date on (a designed jump), on raw AND adjusted prices."""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_validation.dataset import Dataset  # noqa: E402

PROVENANCE = {"source": "synthetic fixture (tests/momentum_fixtures.py)", "retrieved_at": "2026-10-26T00:00:00+00:00", "bar_frequency": "1Day",
              "adjustment": "all-adjusted (splits; no dividends in the fixture)", "missing_data_policy": "no gap unless designed; a missing bar is a gap, never filled",
              "corporate_actions": "splits re-based: adjusted = raw / split ratio", "universe": "fixed synthetic universe (survivorship by construction)",
              "point_in_time": "prices only; every bar is a completed session"}
SYMBOLS = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BAC", "BKNG", "CAT", "COST", "CRM", "CSCO", "CVX",
           "DHR", "DIS", "GE", "HD", "HON", "IBM")


def weekdays(start: str, end: str) -> List[str]:
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def stock(symbol: str, *, drift: float = 0.0003, phase: float = 0.0, price: float = 50.0, cik: Optional[str] = None, split: Optional[tuple] = None,
          volume: float = 2_000_000.0, bumps: Optional[List[tuple]] = None, start: Optional[str] = None, gaps: Optional[List[str]] = None) -> dict:
    return {"symbol": symbol, "drift": drift, "phase": phase, "price": price, "cik": cik or f"CIK-{symbol}", "split": split, "volume": volume,
            "bumps": list(bumps or []), "start": start, "gaps": set(gaps or [])}


def universe(n: int = 25, seed_drift: float = 0.0002) -> List[dict]:
    """n stocks with distinct, persistent drifts (a clean momentum cross-section) and phase-shifted waves."""
    return [stock(s, drift=seed_drift * ((i * 7) % 11 - 4), phase=i * 0.9, price=30.0 + 3 * i) for i, s in enumerate(SYMBOLS[:n])]


def dataset(specs: List[dict], start: str = "2019-01-02", end: str = "2022-12-30", *, spy: bool = True, kind: str = "SYNTHETIC") -> Dataset:
    sessions = weekdays(start, end)
    bars, raw_c, raw_v, ratio, issuers = {}, {}, {}, {}, {}
    for sp in (specs + ([stock("SPY", drift=0.0003, price=300.0, cik="SPY")] if spy else [])):
        sym, rows, rc, rv, rt = sp["symbol"], [], {}, {}, {}
        prev = None
        for i, d in enumerate(sessions):
            if (sp["start"] and d < sp["start"]) or d in sp["gaps"]:
                continue
            px = sp["price"] * (1.0 + sp["drift"]) ** i * (1.0 + 0.03 * math.sin(i / 11.0 + sp["phase"]))
            for bd, mult in sp["bumps"]:
                if d >= bd:
                    px *= mult
            r = sp["split"][1] if sp["split"] and d < sp["split"][0] else 1.0
            raw = px if not sp["split"] or d < sp["split"][0] else px / sp["split"][1]
            adj = raw / r
            o = prev if prev is not None else adj
            rows.append({"date": d, "open": round(o, 6), "high": round(max(o, adj) * 1.004, 6), "low": round(min(o, adj) * 0.996, 6), "close": round(adj, 6),
                         "volume": int(sp["volume"])})
            rc[d], rv[d], rt[d] = round(raw, 6), sp["volume"] * (sp["split"][1] if sp["split"] and d >= sp["split"][0] else 1.0), r
            prev = adj
        bars[sym] = rows
        if sym != "SPY":
            raw_c[sym], raw_v[sym], ratio[sym] = rc, rv, rt
            info = issuers.setdefault(sp["cik"], {"name": f"{sym} Synthetic", "sic": "3571", "sic_description": f"Sector {len(issuers) % 4}", "symbols": []})
            info["symbols"] = sorted(set(info["symbols"]) | {sym})
    aux = {"series": {"raw_close": raw_c, "raw_volume": raw_v, "split_ratio": ratio}, "static": {"issuers": issuers}}
    return Dataset(bars, kind=kind, source="momentum_fixture", provenance=PROVENANCE, aux=aux)
