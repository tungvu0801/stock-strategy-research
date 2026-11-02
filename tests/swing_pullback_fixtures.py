"""tests/swing_pullback_fixtures.py — SYNTHETIC, deterministic daily bars for the Stage 7.8 swing-baseline tests.

Test-only: invented price paths under real universe tickers (the core reads swing_pullback.RESEARCH_UNIVERSE), never written to a real database
(the store refuses SYNTHETIC runs outside a test process) and never fetched from anywhere. A stock is a list of adjusted closes on the session
calendar; a bar's open is the previous close (the first bar opens at its close), its high / low 0.2 % beyond max / min(open, close). The raw
series equals the adjusted one times `raw_mult` (an all-adjusted bundle restates older prices; the raw close is what the $5 rule reads); raw
volume is `volume` shares a session. The dataset declares its volume feed (provenance volume_feed) and carries an issuer map."""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_validation.dataset import Dataset  # noqa: E402

PROVENANCE = {"source": "synthetic fixture (tests/swing_pullback_fixtures.py)", "retrieved_at": "2026-11-01T00:00:00+00:00", "bar_frequency": "1Day",
              "adjustment": "all-adjusted (no corporate action in the fixture unless designed)",
              "missing_data_policy": "no gap unless designed; a missing bar is a gap, never filled", "corporate_actions": "none unless designed",
              "universe": "fixed synthetic universe (survivorship by construction)", "point_in_time": "prices only; every bar is a completed session",
              "volume_feed": "SIP"}
SYMBOLS = ("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "AXP")


def weekdays(start: str = "2019-01-02", end: str = "2021-12-31") -> List[str]:
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


SESSIONS = weekdays()


def wave(n: int, *, price: float = 50.0, drift: float = 0.001, amp: float = 0.04, period: float = 23.0, phase: float = 0.0) -> List[float]:
    """An uptrend with regular pullbacks: price x (1 + drift)^i x (1 + amp x sin(2 pi i / period + phase))."""
    return [price * (1.0 + drift) ** i * (1.0 + amp * math.sin(2.0 * math.pi * i / period + phase)) for i in range(n)]


def stock(symbol: str, closes: Sequence[float], *, volume: float = 1_000_000.0, raw_mult: float = 1.0, cik: Optional[str] = None,
          gaps: Sequence[int] = (), start: int = 0, raw_gaps: Sequence[int] = (), raw_override: Optional[Dict[int, float]] = None) -> dict:
    return {"symbol": symbol, "closes": list(closes), "volume": volume, "raw_mult": raw_mult, "cik": cik or f"CIK-{symbol}", "gaps": set(gaps),
            "start": start, "raw_gaps": set(raw_gaps), "raw_override": dict(raw_override or {})}


def bars_of(closes: Sequence[float], dates: Sequence[str], skip=()) -> List[dict]:
    out, prev = [], None
    for i, (d, c) in enumerate(zip(dates, closes)):
        if i in skip:
            continue
        o = prev if prev is not None else c
        out.append({"date": d, "open": round(o, 6), "high": round(max(o, c) * 1.002, 6), "low": round(min(o, c) * 0.998, 6), "close": round(c, 6),
                    "volume": 1000.0})
        prev = c
    return out


def dataset(specs: List[dict], *, sessions: Sequence[str] = SESSIONS, spy: Optional[Sequence[float]] = None, spy_start: int = 0, spy_gaps=(),
            feed: Optional[str] = "SIP", issuers: Optional[dict] = None, kind: str = "SYNTHETIC", provenance: Optional[dict] = None) -> Dataset:
    n = len(sessions)
    spy_closes = list(spy) if spy is not None else wave(n, price=300.0, drift=0.0005, amp=0.01, period=37.0)
    bars = {"SPY": bars_of(spy_closes, sessions, skip=set(range(spy_start)) | set(spy_gaps))}
    raw_c, raw_v, iss = {}, {}, {}
    for sp in specs:
        skip = set(range(sp["start"])) | sp["gaps"]
        bars[sp["symbol"]] = bars_of(sp["closes"], sessions, skip=skip)
        rc, rv = {}, {}
        for i, (d, c) in enumerate(zip(sessions, sp["closes"])):
            if i in skip or i in sp["raw_gaps"]:
                continue
            rc[d] = sp["raw_override"][i] if i in sp["raw_override"] else round(c * sp["raw_mult"], 6)   # an override is exact
            rv[d] = float(sp["volume"])
        raw_c[sp["symbol"]], raw_v[sp["symbol"]] = rc, rv
        info = iss.setdefault(sp["cik"], {"name": f"{sp['symbol']} Synthetic", "symbols": []})
        info["symbols"] = sorted(set(info["symbols"]) | {sp["symbol"]})
    prov = dict(provenance if provenance is not None else PROVENANCE)
    if feed is None:
        prov.pop("volume_feed", None)
    else:
        prov["volume_feed"] = feed
    aux = {"series": {"raw_close": raw_c, "raw_volume": raw_v}, "static": {"issuers": iss if issuers is None else issuers}}
    return Dataset(bars, kind=kind, source="swing_pullback_fixture", provenance=prov, aux=aux)


def universe(n: int = 8) -> List[dict]:
    """n trending stocks with phase-shifted pullbacks (each dips to RSI(2) <= 10 regularly)."""
    m = len(SESSIONS)
    return [stock(s, wave(m, price=40.0 + 7 * i, drift=0.0006 + 0.0001 * (i % 4), amp=0.035 + 0.005 * (i % 3), period=19.0 + 2 * i, phase=0.7 * i))
            for i, s in enumerate(SYMBOLS[:n])]
