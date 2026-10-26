"""tests/value_quality_fixtures.py — SYNTHETIC, deterministic point-in-time fixtures for the Stage 6.4 Value + Quality tests.

Test-only: invented companies under real universe tickers (the adapter ranks value_quality.RESEARCH_UNIVERSE), never written to a real database
(the store refuses SYNTHETIC runs outside a test process). A company is a dict of annual fundamentals spread evenly over calendar quarters; its
SEC-style XBRL rows follow the filing pattern of a real issuer:

  net income        a direct 3-month duration per quarter (10-Q filed 40 days after the quarter, the 10-K 60 days after the year)
  operating CF      YEAR-TO-DATE durations only (3 / 6 / 9 / 12 months, as cash-flow statements are reported): quarters are derived differences
  assets, equity    instants at each quarter end
  diluted shares    a 3-month weighted-average duration per quarter (a filing after a split reports on the post-split basis)

Prices: a deterministic drift + wave per symbol; volume 2M shares a session; no dividend, so the all-adjusted bars equal the split-adjusted ones.
A split (`split`: (date, ratio)) divides the raw prices from that date on; the split-adjusted history is on today's basis (raw / ratio before)."""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_validation.dataset import Dataset  # noqa: E402

PROVENANCE = {"source": "synthetic fixture (tests/value_quality_fixtures.py)", "retrieved_at": "2026-10-25T00:00:00+00:00", "bar_frequency": "1Day",
              "adjustment": "split-adjusted (no dividends in the fixture)", "missing_data_policy": "no gap; a missing bar is a gap, never filled",
              "corporate_actions": "splits re-based by the raw / split-adjusted close ratio", "universe": "fixed synthetic universe (survivorship by construction)",
              "point_in_time": "every fact carries its filing date; usable from the session after"}
NI, OCF, ASSETS, EQUITY, SHARES = ("NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities", "Assets", "StockholdersEquity",
                                   "WeightedAverageNumberOfDilutedSharesOutstanding")


def weekdays(start: str, end: str) -> List[str]:
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def quarters(first_year: int, last_year: int) -> List[tuple]:
    """(year, q, start, end) calendar quarters."""
    ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
    return [(y, q, f"{y}-{starts[q]}", f"{y}-{ends[q]}") for y in range(first_year, last_year + 1) for q in (1, 2, 3, 4)]


def filed_for(end: str, q: int) -> str:
    return (date.fromisoformat(end) + timedelta(days=60 if q == 4 else 40)).isoformat()


def company(symbol: str, cik: str, *, price: float = 50.0, drift: float = 0.0003, phase: float = 0.0, shares: float = 1e9, ni: float = 2e9, ocf: float = 2.5e9,
            assets: float = 20e9, equity: float = 10e9, sic: int = 3571, sic_description: str = "Electronic Computers", split: Optional[tuple] = None,
            overrides: Optional[dict] = None, symbols: Optional[List[str]] = None) -> dict:
    """`ni` / `ocf`: annual; `overrides`: {(concept, period_end): value or None} replaces (None removes) a row; extra rows go in `extra`."""
    return {"symbol": symbol, "cik": cik, "price": price, "drift": drift, "phase": phase, "shares": shares, "ni": ni, "ocf": ocf, "assets": assets,
            "equity": equity, "sic": sic, "sic_description": sic_description, "split": split, "overrides": dict(overrides or {}), "extra": {},
            "symbols": symbols or [symbol]}


def facts(co: dict, first_year: int = 2018, last_year: int = 2022) -> Dict[str, List[dict]]:
    rows: Dict[str, List[dict]] = {NI: [], OCF: [], ASSETS: [], EQUITY: [], SHARES: []}
    for y, q, s, e in quarters(first_year, last_year):
        f, form, fp = filed_for(e, q), ("10-K" if q == 4 else "10-Q"), ("FY" if q == 4 else f"Q{q}")
        base = {"filed": f, "form": form, "fy": y, "fp": fp, "accn": f"{co['cik']}-{y}-Q{q}"}
        rows[NI].append({**base, "start": s, "end": e, "val": co["ni"] / 4})
        rows[OCF].append({**base, "start": f"{y}-01-01", "end": e, "val": co["ocf"] / 4 * q})
        rows[ASSETS].append({**base, "end": e, "val": co["assets"]})
        rows[EQUITY].append({**base, "end": e, "val": co["equity"]})
        post = co["split"] is not None and f >= co["split"][0]                    # a filing after a split reports on the new basis
        rows[SHARES].append({**base, "start": s, "end": e, "val": co["shares"] * (co["split"][1] if post else 1.0)})
    for (concept, end), val in co["overrides"].items():
        hit = [r for r in rows.get(concept, []) if r["end"] == end]
        for r in hit:
            rows[concept].remove(r)
            if val is not None:
                rows[concept].append({**r, "val": val})
    for concept, extra in co["extra"].items():
        rows.setdefault(concept, []).extend(extra)
    return {k: v for k, v in rows.items() if v}


def prices(co: dict, sessions: List[str]) -> Dict[str, Dict[str, float]]:
    """raw close / raw volume / split ratio (raw / split-adjusted) per session."""
    raw, vol, ratio = {}, {}, {}
    sp = co["split"]
    for i, d in enumerate(sessions):
        px = co["price"] * (1.0 + co["drift"]) ** i * (1.0 + 0.02 * math.sin(i / 9.0 + co["phase"]))
        r = sp[1] if sp and d < sp[0] else 1.0
        raw[d] = round(px if not sp or d < sp[0] else px / sp[1], 4)
        vol[d] = 2_000_000.0 * (sp[1] if sp and d >= sp[0] else 1.0)
        ratio[d] = r
    return {"raw_close": raw, "raw_volume": vol, "split_ratio": ratio}


def bars(closes: Dict[str, float], ratio: Dict[str, float]) -> List[dict]:
    out, prev = [], None
    for d in sorted(closes):
        c = closes[d] / ratio[d]                                                  # split-adjusted (= all-adjusted: no dividends)
        o = prev if prev is not None else c
        out.append({"date": d, "open": round(o, 4), "high": round(max(o, c) * 1.004, 4), "low": round(min(o, c) * 0.996, 4), "close": round(c, 4),
                    "volume": 2_000_000})
        prev = c
    return out


def dataset(companies: List[dict], start: str = "2019-01-02", end: str = "2021-12-31", *, spy: bool = True, kind: str = "SYNTHETIC",
            first_year: int = 2018, last_year: int = 2022) -> Dataset:
    sessions = weekdays(start, end)
    series = {"raw_close": {}, "raw_volume": {}, "split_ratio": {}}
    b, issuers, fx = {}, {}, {}
    for co in companies:
        p = prices(co, sessions)
        for name in series:
            series[name][co["symbol"]] = p[name]
        b[co["symbol"]] = bars(p["raw_close"], p["split_ratio"])
        if co["cik"]:
            info = issuers.setdefault(co["cik"], {"name": f"{co['symbol']} Synthetic Inc", "sic": str(co["sic"]), "sic_description": co["sic_description"],
                                                  "fiscal_year_end": "1231", "symbols": []})
            info["symbols"] = sorted(set(info["symbols"]) | set(co["symbols"]))
            fx[co["cik"]] = facts(co, first_year, last_year)
    if spy:
        sp = prices(company("SPY", "", price=300.0, drift=0.0004), sessions)
        b["SPY"] = bars(sp["raw_close"], sp["split_ratio"])
    aux = {"series": series, "events": {"facts": fx}, "static": {"issuers": issuers}}
    return Dataset(b, kind=kind, source="value_quality_fixture", provenance=PROVENANCE, aux=aux)


# ---------------------------------------------------------------- named cross-sections (annual figures; shares 1e9 unless stated)
def ranking_set() -> List[dict]:
    """GOOD: cheap AND profitable. CHEAP_JUNK: the cheapest on every value factor, the worst on quality. PRICEY: the best quality, the most expensive.
    Five middling companies complete the cross-section."""
    good = company("AAPL", "0000000001", price=20.0, ni=2.0e9, ocf=2.5e9, assets=10e9, equity=6e9)          # E/P .10 B/P .30 CF/P .125 | ROA .20
    junk = company("ABBV", "0000000002", price=10.0, ni=1.2e9, ocf=1.5e9, assets=100e9, equity=5e9)         # E/P .12 B/P .50 CF/P .15  | ROA .012
    pricey = company("ABT", "0000000003", price=300.0, ni=3.0e9, ocf=3.5e9, assets=6e9, equity=5e9)        # E/P .01 B/P .017 CF/P .012 | ROA .50
    mids = [company(s, f"00000001{i:02d}", price=40.0 + 8 * i, ni=(1.4 - 0.1 * i) * 1e9, ocf=(1.8 - 0.1 * i) * 1e9, assets=(14 + 2 * i) * 1e9,
                    equity=(5 + 0.5 * i) * 1e9) for i, s in enumerate(("ACN", "ADBE", "ADI", "AMAT", "AMD"))]
    return [good, junk, pricey] + mids


def run_set() -> List[dict]:
    """Twelve companies for an end-to-end harness run: varied value / quality, varied price paths; HD's fundamentals deteriorate in 2020 (a
    new filing turns it unattractive), so it leaves the portfolio at a known rebalance."""
    cos = []
    for i, s in enumerate(("AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMZN", "AVGO", "HD")):
        cos.append(company(s, f"00000002{i:02d}", price=30.0 + 7 * i, drift=0.0002 + 0.00005 * (i % 5), phase=i * 0.7, ni=(0.8 + 0.15 * ((i * 7) % 11)) * 1e9,
                           ocf=(1.0 + 0.17 * ((i * 5) % 11)) * 1e9, assets=(10 + 3 * ((i * 3) % 7)) * 1e9, equity=(3 + 0.9 * ((i * 4) % 9)) * 1e9))
    hd = cos[-1]
    hd.update(price=25.0, ni=4.0e9, ocf=4.5e9, assets=12e9, equity=8e9)                            # the best until its 2020 deterioration
    for y, q, s, e in quarters(2020, 2022):
        hd["overrides"][(NI, e)] = -0.5e9                                                       # losses from Q1 2020 on (filed 2020-05-10)
        hd["overrides"][(OCF, e)] = -0.2e9 * q
    return cos
