"""tests/sip_eval_fixtures.py — SYNTHETIC fixtures for the Stage 7.8B-1 tests (research_data/, swing_eval/). Never network, never a real file.

  fake_provider(...)   a transport for research_data.sip_bundle.fetch that serves invented daily bars in the Alpaca /v2/stocks/bars JSON shape
                       (o h l c v, t at midnight New York: 04:00Z in daylight time, 05:00Z otherwise), honouring symbols, the window, `limit`
                       and next_page_token, plus an invented SEC company_tickers.json; it records every call (url, params, header names)
  sip_dataset(...)     a SYNTHETIC dataset on the real NYSE sessions (research_data.nyse_calendar) declaring volume_feed SIP and the builder
                       sip_daily_v1, with the raw series and an issuer map (the swing_pullback_fixtures generator, unchanged)"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import swing_pullback_fixtures as SPF  # noqa: E402
from research_data import BUILDER_VERSION  # noqa: E402
from research_data import nyse_calendar as CAL  # noqa: E402

SIP_PROVENANCE = {**SPF.PROVENANCE, "builder": BUILDER_VERSION}   # what the evaluation manifest requires (R14)
KEY, SECRET, UA = "FAKE-KEY-ID-123", "FAKE-SECRET-456", "Synthetic Tests tests@example.invalid"


def _dst(d: date) -> bool:
    """US daylight time: from the second Sunday of March to the first Sunday of November."""
    mar = date(d.year, 3, 8)
    start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = date(d.year, 11, 1)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return start <= d < end


def stamp(d: str) -> str:
    return f"{d}T04:00:00Z" if _dst(date.fromisoformat(d)) else f"{d}T05:00:00Z"


def synthetic_bars(symbols: List[str], sessions: List[str], *, split: Optional[Dict[str, tuple]] = None) -> Dict[str, Dict[str, List[dict]]]:
    """{adjustment: {symbol: [alpaca bar dicts]}}: raw prices, a split (date, ratio) dividing raw prices from that date on; split- and all-adjusted
    series are re-based to today (raw / ratio before the split); no dividends."""
    out = {"all": {}, "raw": {}, "split": {}}
    for k, sym in enumerate(symbols):
        closes = SPF.wave(len(sessions), price=30.0 + 9 * k, drift=0.0004, amp=0.03, period=17.0 + k)
        sp = (split or {}).get(sym)
        for adj in out:
            rows, prev = [], None
            for d, c in zip(sessions, closes):
                ratio = sp[1] if sp and d < sp[0] else 1.0
                raw = c if not sp or d < sp[0] else c / sp[1]
                px = raw if adj == "raw" else raw / ratio
                o = prev if prev is not None else px
                rows.append({"t": stamp(d), "o": round(o, 4), "h": round(max(o, px) * 1.002, 4), "l": round(min(o, px) * 0.998, 4), "c": round(px, 4),
                             "v": 1_500_000 if adj == "raw" else round(1_500_000 * (ratio if adj != "raw" else 1.0)), "n": 1000, "vw": round(px, 4)})
                prev = px
            out[adj][sym] = rows
    return out


def sec_tickers(symbols: List[str], *, missing=(), shared: Optional[Dict[str, str]] = None) -> dict:
    """An invented company_tickers.json: one CIK per symbol unless `shared` maps a symbol onto another's CIK; `missing` symbols are absent."""
    cik = {s: 1000 + i for i, s in enumerate(symbols)}
    for s, other in (shared or {}).items():
        cik[s] = cik[other]
    return {str(i): {"cik_str": cik[s], "ticker": s.replace(".", "-"), "title": f"{s} Synthetic Inc"} for i, s in enumerate(symbols) if s not in missing}


def fake_provider(bars: Dict[str, Dict[str, List[dict]]], tickers: dict, *, statuses: Optional[Dict[int, int]] = None, extra_symbol: Optional[str] = None):
    """A transport: (url, params, headers) -> (status, body). `statuses` {call index: HTTP status} injects failures."""
    calls: List[dict] = []

    def get(url: str, params: dict, headers: dict):
        calls.append({"url": url, "params": dict(params), "headers": sorted(headers)})
        idx = len(calls) - 1
        if statuses and idx in statuses:
            return statuses[idx], b'{"message": "injected"}'
        if url.endswith("company_tickers.json"):
            assert "APCA-API-KEY-ID" not in headers and "APCA-API-SECRET-KEY" not in headers
            return 200, json.dumps(tickers).encode("utf-8")
        assert headers.get("APCA-API-KEY-ID") == KEY and headers.get("APCA-API-SECRET-KEY") == SECRET and params["feed"] == "sip"
        adj, lo, hi = params["adjustment"], params["start"], params["end"]
        flat = [(s, b) for s in params["symbols"].split(",") for b in bars[adj].get(s, []) if lo <= b["t"] <= hi]
        start = int(params.get("page_token") or 0)
        page = flat[start:start + int(params["limit"])]
        body = {"bars": {}, "next_page_token": str(start + len(page)) if start + len(page) < len(flat) else None}
        for s, b in page:
            body["bars"].setdefault(s, []).append(b)
        if extra_symbol:
            body["bars"][extra_symbol] = [page[0][1]] if page else []
        return 200, json.dumps(body).encode("utf-8")
    get.calls = calls                                                     # type: ignore[attr-defined]
    return get


def sip_dataset(n_symbols: int = 6, *, start: str = "2016-01-04", end: str = "2019-12-31", **kw):
    sess = CAL.sessions(start, end)
    n = len(sess)
    specs = [SPF.stock(s, SPF.wave(n, price=40.0 + 7 * i, drift=0.0006 + 0.0001 * (i % 4), amp=0.035 + 0.005 * (i % 3), period=19.0 + 2 * i,
                                   phase=0.7 * i)) for i, s in enumerate(SPF.SYMBOLS[:n_symbols])]
    kw.setdefault("provenance", dict(SIP_PROVENANCE))
    return SPF.dataset(specs, sessions=sess, spy=SPF.wave(n, price=300.0, drift=0.0005, amp=0.01, period=37.0), **kw), sess


def wide_range_dataset(high_mult: float, low_mult: float, *, dips=(), start: str = "2016-01-04", end: str = "2017-12-29"):
    """One stock (AAPL) rising 0.1 % a session whose bars span close x low_mult .. close x high_mult: ATR(14) ~ (high_mult - low_mult) x close.
    3.0 / 0.3 puts every 2-ATR stop below zero (M-5); 1.25 / 0.80 only the 2.5-ATR neighbour's. `dips`: three tiny lower closes ending there."""
    from strategy_validation.dataset import Dataset
    sess = CAL.sessions(start, end)
    n = len(sess)
    r = [0.001] * n
    for T in dips:
        r[T - 2] = r[T - 1] = r[T] = -0.0001
    closes, c = [], 50.0
    for i, x in enumerate(r):
        c = 50.0 if i == 0 else c * (1.0 + x)
        closes.append(c)
    rows, prev = [], None
    for d, c in zip(sess, closes):
        o = prev if prev is not None else c
        rows.append({"date": d, "open": o, "high": max(o, c) * high_mult, "low": min(o, c) * low_mult, "close": c, "volume": 1e6})
        prev = c
    spy = SPF.bars_of(SPF.wave(n, price=300.0, drift=0.0005, amp=0.01, period=37.0), sess)
    aux = {"series": {"raw_close": {"AAPL": dict(zip(sess, closes))}, "raw_volume": {"AAPL": {d: 1e6 for d in sess}}},
           "static": {"issuers": {"CIK-AAPL": {"name": "AAPL Synthetic", "symbols": ["AAPL"]}}}}
    return Dataset({"AAPL": rows, "SPY": spy}, kind="SYNTHETIC", source="sip_eval_fixture", provenance=dict(SIP_PROVENANCE), aux=aux), sess
