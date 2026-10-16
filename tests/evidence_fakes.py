"""
tests/evidence_fakes.py — fixtures-only doubles for the Stage 5.4 evidence layer. No test touches a network.

FakeWire serves tests/fixtures/evidence/* by URL (recording every call) and simulates failures by URL suffix; fake bars, events,
sector map, news and provider are deterministic. `adapters()` wires them into the real adapter classes so engine tests exercise
the real code paths; `request()` is the SNDK scenario used by the golden test.
"""
from __future__ import annotations

import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from agents.orchestrator import LLMProvider, LLMResponse
from evidence import engine as EN
from evidence.sources.alpaca_news import NewsAdapter
from evidence.sources.company_ir import IrAdapter
from evidence.sources.internal import InternalAdapter
from evidence.sources.manual_url import ManualAdapter
from evidence.sources.sec_edgar import SecAdapter

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "evidence"
AS_OF = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 21, 30, tzinfo=timezone.utc)
SEC_UA = "Stock Agent tests test@example.com"
IR_URL = "https://investor.sandisk.example/q4-2026"
LONG_URL = "https://investor.sandisk.example/long"            # guidance sentence after character 2,000 (full-document rule scan)
SUPPLIER_URL = "https://investor.lamresearch.example/results"
INDUSTRY_URL = "https://industry.example/nand"
SOCIAL_URL = "https://www.reddit.com/r/stocks/sndk"
IR_DOMAINS = {"SNDK": ["investor.sandisk.example"], "LRCX": ["investor.lamresearch.example"]}
_A = "https://www.sec.gov/Archives/edgar/data"
URLS: Dict[str, str] = {
    "https://www.sec.gov/files/company_tickers.json": "company_tickers.json",
    "https://data.sec.gov/submissions/CIK0001234567.json": "submissions_SNDK.json",
    "https://data.sec.gov/submissions/CIK0007654321.json": "submissions_LRCX.json",
    "https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json": "companyfacts_SNDK.json",
    f"{_A}/1234567/000123456726000010/index.json": "index_000010.json",
    f"{_A}/1234567/000123456726000010/ex99-1.htm": "8k_ex99_down.html",
    f"{_A}/1234567/000123456726000011/index.json": "index_000011.json",
    f"{_A}/1234567/000123456726000011/EX-99.1.htm": "8k_ex99_reaffirm.html",
    f"{_A}/7654321/000765432126000020/index.json": "index_000020.json",
    f"{_A}/7654321/000765432126000020/ex99_1.htm": "8k_ex99_up.html",
    IR_URL: "ir_release.html", SUPPLIER_URL: "supplier_page.html", INDUSTRY_URL: "industry_page.html", SOCIAL_URL: "social_page.html",
    LONG_URL: "8k_ex99_long.html",
}


class FakeWire:
    """Serves fixtures by exact URL; failure behaviours are selected by URL suffix."""

    def __init__(self, overrides: Optional[Dict[str, str]] = None):
        self.calls: List[dict] = []
        self.urls = {**URLS, **(overrides or {})}

    def __call__(self, url: str, headers: dict, timeout_s: float, max_bytes: int):
        self.calls.append({"url": url, "headers": dict(headers), "timeout_s": timeout_s, "max_bytes": max_bytes})
        if url.endswith("/timeout"):
            raise socket.timeout("timed out")
        if url.endswith("/denied"):
            return 403, {"content-type": "text/html"}, b"<html>forbidden</html>"
        if url.endswith("/rate-limited"):
            return 429, {}, b""
        if url.endswith("/server-error"):
            return 500, {}, b"<html>oops</html>"
        if url.endswith("/redirect-offhost"):
            return 302, {"location": "https://other.example/x"}, b""
        if url.endswith("/redirect-same"):
            return 301, {"location": url.replace("/redirect-same", "/landed")}, b""
        if url.endswith("/landed"):
            return 200, {"content-type": "text/html; charset=utf-8"}, b"<html><body><p>landed</p></body></html>"
        if url.endswith("/redirect-loop"):
            return 302, {"location": url}, b""
        if url.endswith("/huge"):
            return 200, {"content-type": "text/html"}, b"x" * (max_bytes + 1)
        if url.endswith("/empty"):
            return 200, {"content-type": "text/html"}, b"   "
        if url.endswith("/notjson"):
            return 200, {"content-type": "application/json"}, b"<html>not json</html>"
        name = self.urls.get(url)
        if name is None:
            return 404, {}, b"not found"
        body = (FIX / name).read_bytes()
        ctype = "application/json" if name.endswith(".json") else "text/html; charset=utf-8"
        return 200, {"content-type": ctype}, body


# ---------------------------------------------------------------- market data, events, sector, news ------------------------

def sessions(end: date, n: int) -> List[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _path(n: int, base: float, legs: List[tuple]) -> List[float]:
    """Piecewise geometric path: legs = [(end_index, total_return_over_leg), ...]."""
    closes, price, start = [], base, 0
    for end_i, ret in legs:
        steps = max(1, end_i - start)
        step = (1 + ret) ** (1 / steps)
        for _ in range(start, end_i):
            price *= step
            closes.append(price)
        start = end_i
    while len(closes) < n:
        closes.append(price)
    return closes[:n]


def fake_bars(symbols: List[str], lookback_days: int) -> Dict[str, list]:
    n = 220
    days = sessions(AS_OF, n)
    paths = {"SNDK": _path(n, 100.0, [(150, 0.20), (185, -0.25), (220, 0.14)]),      # peak mid-July, trough late August, recovery
             "SPY": _path(n, 500.0, [(150, 0.06), (185, -0.04), (220, 0.05)]),
             "SOXX": _path(n, 200.0, [(150, 0.10), (185, -0.12), (220, 0.08)]),
             "AAPL": _path(n, 150.0, [(220, 0.03)])}
    out = {}
    for s in symbols:
        if s in paths:
            out[s] = [{"date": d.isoformat(), "close": round(c, 4), "volume": 1_000_000 + (i % 7) * 50_000 + (400_000 if i == n - 1 else 0)}
                      for i, (d, c) in enumerate(zip(days, paths[s]))]
    return out


def _ev(event_id, etype, title, day, symbol=None, source="fixture events", confirmed=True):
    return SimpleNamespace(event_id=event_id, event_type=SimpleNamespace(value=etype), category=SimpleNamespace(value="MACRO" if symbol is None else "COMPANY"),
                           symbol=symbol, title=title, event_date=day, source=source, source_url=None, confirmed=confirmed, status=SimpleNamespace(value="SCHEDULED"))


def fake_events(symbol: str):
    return SimpleNamespace(company_events=[_ev("e1", "EARNINGS", f"{symbol} earnings report", date(2026, 11, 5), symbol)],
                           macro_events=[_ev("m1", "FOMC", "FOMC meeting", date(2026, 10, 28)), _ev("m0", "CPI", "CPI release", date(2026, 9, 11))],
                           nearest_event=None, event_risk_level="LOW", event_risk_flags=[], data_quality="MEDIUM", earnings_available=True, earnings_reason=None)


def fake_sector(symbol: str):
    return ("Semiconductors", "SOXX") if symbol in ("SNDK", "LRCX") else (None, None)


def fake_news(symbol: str, limit: int) -> List[dict]:
    rows = json.loads((FIX / "alpaca_news.json").read_text(encoding="utf-8"))
    return [{**r, "created_at": datetime.fromisoformat(r["created_at"])} for r in rows][:limit]


class FakeProvider(LLMProvider):
    def __init__(self, text: str = "", fail: bool = False):
        self.text, self.fail, self.calls = text, fail, 0

    def analyze(self, prompt, system_prompt, max_tokens=1400):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return LLMResponse(text=self.text, model="claude-sonnet-5", input_tokens=1200, output_tokens=300, cache_creation_tokens=0, cache_read_tokens=0)

    def chat(self, message, history=None):
        raise NotImplementedError

    def run_tool_loop(self, *a, **k):
        raise AssertionError("the evidence explanation must never run a tool loop")


# ---------------------------------------------------------------- wiring ---------------------------------------------------------

def now_fn():
    return NOW


def adapters(*, user_agent: str = SEC_UA, news=fake_news, bars=fake_bars, events=fake_events, sector=fake_sector, sec_max_requests: int = 12) -> dict:
    return {"internal": InternalAdapter(bars_fn=bars, events_fn=events, sector_fn=sector, now_fn=now_fn),
            "sec": SecAdapter(now_fn=now_fn, user_agent=user_agent, max_requests=sec_max_requests, min_interval_s=0, sleep_fn=lambda s: None),
            "ir": IrAdapter(now_fn=now_fn), "news": NewsAdapter(news_fn=news, now_fn=now_fn), "manual": ManualAdapter(now_fn=now_fn)}


def request(**over) -> EN.EvidenceRequest:
    base = dict(ticker="SNDK", as_of_date=AS_OF, lookback_days=90, related={"suppliers": ["LRCX"], "customers": []}, ir_urls=[IR_URL],
                manual_urls=[{"url": SUPPLIER_URL, "role": "supplier", "related_ticker": "LRCX"}, {"url": INDUSTRY_URL, "role": "industry"},
                             {"url": SOCIAL_URL, "role": "social"}],
                manual_texts=[{"text": "A friend says channel checks look better.", "role": "industry"}], cik_overrides={})
    base.update(over)
    return EN.EvidenceRequest(**base)
