"""
Stage 5.5 — TRADER VIEW (read-only terminal). Focused tests, numbered after the stage checklist:
  1 route loads · 2 ticker switch · 3 stale-symbol guard · 4 reconnect = one subscription · 5 Back/Forward · 6 arbitrary valid symbol
  7 invalid symbol fails cleanly · 8 watchlist uses existing state · 9 position read-only · 10 no position · 11 stored evidence loads
 12 missing evidence NOT LOADED · 13 PPE NOT AVAILABLE · 14 buttons cannot execute · 15 no order-writer import · 16 no Robinhood write
 17 no real model call · 18 chart asset local · 19 simulator fallback · 20 node tests of the browser logic
 21 real adapter shapes without network · 22 cadence / idle · 23 prior-stage files byte-identical to the base commit
(20 of the checklist, the full regression suite, is the whole tests/ directory.)
"""
import ast
import hashlib
import inspect
import re
import shutil
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from evidence import CAUSE_VERDICTS, RUNG_STATUSES, STRENGTH_DISCLAIMER, STRENGTHS  # noqa: E402
from trader_view import CLIENT_COMMANDS, LABEL, NOT_CONNECTED, SYMBOL_BOUND_TYPES  # noqa: E402
from trader_view.adapters import (NullExecutionPreviewAdapter, NullPositionAdapter, NullResearchAdapter, UnknownSymbol,  # noqa: E402
                                  build_market_adapter, resolve_mode)
from trader_view.hub import Hub, NavigationHistory, accepts_message  # noqa: E402
from trader_view.models import EvidenceSummary, PositionSource, PositionState  # noqa: E402
from trader_view.positions import StockAgentPositionAdapter  # noqa: E402
from trader_view.research import EvidenceResearchAdapter  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402

T0 = 1_760_000_000
WL = ["NVDA", "AMD", "SNDK"]
WS_PATH = "/api/terminal/stream"
CHART_SHA = "c7dda807d662a95b3d257119ed315cec669e3bdf5aaece75c480a39307f23540"
BASE = "f08b971"                                                                     # origin/main the stage was branched from
TV_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "trader_view").glob("*.py")) + ["api/routes/trader_view.py"]
JS = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend" / "trader_core.js").read_text(encoding="utf-8")


def tv_source() -> str:
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in TV_FILES)


# ---------------------------------------------------------------- fakes + fixtures ----------------------------------------------

class FakePositions:
    def __init__(self, rows=None):
        self.calls, self.rows = [], rows or {}

    def position(self, symbol):
        self.calls.append(symbol)
        s = symbol.upper()
        srcs = [PositionSource(source="ALPACA_PAPER_VIEW", label="Alpaca Paper View", status="NOT_REFRESHED", message="No refresh yet"),
                PositionSource(source="LOCAL_PAPER", label="Local Paper Portfolio", status="NO_POSITION", message="No position"),
                PositionSource(source="ROBINHOOD_READ_ONLY", label="Robinhood Read Only", status="DISABLED", message="off")]
        if s in self.rows:
            srcs[1] = PositionSource(source="LOCAL_PAPER", label="Local Paper Portfolio", status="POSITION", **self.rows[s])
        return PositionState(symbol=s, has_position=s in self.rows, sources=srcs, note="fake")

    def invalidate(self, symbol=None):
        self.calls.append(("invalidate", symbol))


class FakeResearch:
    def __init__(self, loaded=()):
        self.loaded, self.calls = set(loaded), []

    def latest(self, symbol):
        self.calls.append(symbol)
        s = symbol.upper()
        if s in self.loaded:
            return EvidenceSummary(symbol=s, status="LOADED", packet_id="a" * 32, packet_status="COMPLETED", created_at="2026-10-09T21:30:00+00:00",
                                   strength="WEAK", cause="MIXED", company_confirm="PARTIAL", market_confirm="NOT_SUPPORTED", data_status="STORED",
                                   disclaimer=STRENGTH_DISCLAIMER)
        return EvidenceSummary(symbol=s, status="NOT_LOADED", data_status="NOT_LOADED", message="none", disclaimer=STRENGTH_DISCLAIMER)


def make_stream(**kw):
    market = kw.pop("market", None) or SimulatedMarket(seed=11, start_time=T0)
    return TerminalStream(market, kw.pop("positions", None) or FakePositions(), kw.pop("research", None) or FakeResearch(),
                          watchlist_fn=kw.pop("watchlist_fn", lambda: list(WL)), tick_seconds=1.0, allow_manual_tick=True, run_loop=False,
                          now_fn=lambda: T0, **kw)


@pytest.fixture
def ts():
    return make_stream()


@pytest.fixture
def api(ts, monkeypatch):
    monkeypatch.setitem(RT.STREAM, "instance", ts)
    from api.server import app
    return TestClient(app)


def hello_of(ws):
    """The connection handshake: hello, then one immediate watchlist snapshot."""
    h = ws.receive_json()
    assert h["type"] == "hello"
    w = ws.receive_json()
    assert w["type"] == "watchlist" and [i["symbol"] for i in w["items"]] == [s for s in h["symbols"]["watchlist"]]
    return h


def burst(ws, symbol):
    """subscribe and collect the reply burst: subscribed · state · candles · quote · candle · position · evidence (or a single error)."""
    ws.send_json({"type": "subscribe", "symbol": symbol})
    first = ws.receive_json()
    out = [first]
    if first["type"] == "error":
        return out
    while out[-1]["type"] != "evidence":
        out.append(ws.receive_json())
    return out


def marker(ws):
    """Everything queued so far: an unknown command is answered in order, so its BAD_MESSAGE error marks the end."""
    ws.send_json({"type": "__marker__"})
    out = []
    while True:
        m = ws.receive_json()
        if m["type"] == "error" and m["code"] == "BAD_MESSAGE":
            return out
        out.append(m)


# ---------------------------------------------------------------- 1 route + assets ----------------------------------------------

def test_01_trader_view_route_and_assets_load(api):
    html = api.get("/").text
    assert 'data-tab="trader">Trader View' in html and 'id="tab-trader"' in html and 'id="tv-body"' in html
    for asset in ("trader_view.css", "trader_core.js", "trader_view.js", "vendor/lightweight-charts/lightweight-charts.standalone.production.js"):
        assert asset in html
        assert api.get("/" + asset).status_code == 200, asset
    assert "javascript" in api.get("/trader_view.js").headers["content-type"]
    c = api.get("/api/terminal/config")
    assert c.status_code == 200
    j = c.json()
    assert j["mode"] == "simulated" and j["label"] == LABEL and j["execution_preview"] == "NOT_CONNECTED" and j["chart"]["version"] == "4.2.3"
    assert set(j["client_commands"]) == set(CLIENT_COMMANDS) and j["cadence"]["evidence"] == "on subscribe / refresh only" and j["ppe"]["version"] == "ppe_v1"
    for t in ("overview", "daily", "watchlist", "agent", "alerts", "setups", "history", "portfolio", "review", "strategy", "paper", "settings"):
        assert f'data-tab="{t}"' in html                                           # Trader View is additive: every existing tab is still there


# ---------------------------------------------------------------- 2 switch ------------------------------------------------------

def test_02_ticker_switch_changes_active_symbol(api, ts):
    with api.websocket_connect(WS_PATH) as ws:
        hello = hello_of(ws)
        assert hello["type"] == "hello" and hello["mode"] == "simulated" and hello["symbols"]["watchlist"] == WL and hello["commands"] == list(CLIENT_COMMANDS)
        cid = hello["connection"]
        b = burst(ws, "aapl")
        assert [m["type"] for m in b] == ["subscribed", "state", "candles", "quote", "candle", "position", "evidence"]
        assert b[0] == {"type": "subscribed", "symbol": "AAPL", "replaced": None} and all(m["symbol"] == "AAPL" for m in b[1:])
        assert len(b[2]["candles"]) == 240 and b[1]["ppe"]["status"] == "SIMULATED_DATA" and b[1]["data_status"]["mode"] == "simulated"
        assert b[3]["status"] == "OK" and b[3]["price"] == b[1]["quote"]["price"] and b[4]["time"] % 60 == 0
        assert ts.hub.symbol_of(cid) == "AAPL"
        b2 = burst(ws, "NVDA")
        assert b2[0] == {"type": "subscribed", "symbol": "NVDA", "replaced": "AAPL"} and all(m["symbol"] == "NVDA" for m in b2[1:])
        assert ts.hub.symbol_of(cid) == "NVDA" and len(ts.hub) == 1 and ts.hub.subscribers("AAPL") == []
        assert ts.positions.calls == ["AAPL", "NVDA"] and ts.research.calls == ["AAPL", "NVDA"]
    assert len(ts.hub) == 0 and ts.connections == {}                              # closing the socket drops its subscription


# ---------------------------------------------------------------- 3 stale guard -------------------------------------------------

def test_03_stale_symbol_messages_are_ignored(api, ts):
    for t in SYMBOL_BOUND_TYPES:
        assert not accepts_message({"type": t, "symbol": "SNDK"}, "NVDA") and accepts_message({"type": t, "symbol": "NVDA"}, "NVDA")
        assert not accepts_message({"type": t}, "NVDA")
    assert accepts_message({"type": "watchlist", "items": []}, "NVDA") and accepts_message({"type": "heartbeat"}, "NVDA")
    assert not accepts_message(None, "NVDA") and not accepts_message("x", "NVDA") and not accepts_message({"symbol": "NVDA"}, "NVDA")
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        burst(ws, "SNDK")
        ws.send_json({"type": "tick", "now": T0 + 1})
        first = marker(ws)
        assert [m["type"] for m in first] == ["quote", "candle"] and all(m["symbol"] == "SNDK" for m in first) and first[0]["ts"] == T0 + 1
        ws.send_json({"type": "unsubscribe", "symbol": "SNDK"})                # the browser's switch: unsubscribe(old) then subscribe(new)
        assert ws.receive_json() == {"type": "unsubscribed", "symbol": "SNDK", "ok": True}
        after = burst(ws, "NVDA")
        assert after[0]["replaced"] is None
        for i in range(2, 12):
            ws.send_json({"type": "tick", "now": T0 + i})
        after += marker(ws)
        bound = [m for m in after if m["type"] in SYMBOL_BOUND_TYPES]
        assert len(bound) >= 20 and all(m["symbol"] == "NVDA" for m in bound)   # nothing from SNDK after the switch
        assert [m["type"] for m in after].count("watchlist") == 1 and [m["type"] for m in after].count("heartbeat") == 2   # ticks 10 and 5, 10
        wl = next(m for m in after if m["type"] == "watchlist")
        assert [i["symbol"] for i in wl["items"]] == WL and all(i["price"] and i["status"] == "OK" for i in wl["items"])
        ws.send_json({"type": "unsubscribe", "symbol": "AAPL"})                 # the wrong symbol never unsubscribes the active one
        assert ws.receive_json()["ok"] is False and ts.hub.symbol_of(1) == "NVDA"


# ---------------------------------------------------------------- 4 reconnect ---------------------------------------------------

def test_04_reconnect_produces_one_active_subscription(api, ts):
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        burst(ws, "NVDA")
        assert len(ts.hub) == 1 and len(ts.connections) == 1
    assert len(ts.hub) == 0 and len(ts.connections) == 0
    with api.websocket_connect(WS_PATH) as ws2:                                    # the reconnect
        hello = hello_of(ws2)
        b = burst(ws2, "NVDA")
        assert hello["connection"] == 2 and b[0]["replaced"] is None and ts.hub.subscribers("NVDA") == [2] and len(ts.hub) == 1
        b2 = burst(ws2, "NVDA")                                                     # a repeated subscribe replaces nothing and stacks nothing
        assert b2[0]["replaced"] is None and len(ts.hub) == 1 and ts.hub.items() == [(2, "NVDA")]
    h = Hub()
    assert h.subscribe(1, "nvda") is None and h.subscribe(1, "NVDA") is None and h.subscribe(1, "AAPL") == "NVDA" and len(h) == 1
    h.drop(1)
    h.subscribe(2, "AAPL")
    assert h.subscribers("AAPL") == [2] and h.values() == ["AAPL"] and h.unsubscribe(2) and len(h) == 0 and not h.unsubscribe(2)


# ---------------------------------------------------------------- 5 Back / Forward ----------------------------------------------

def test_05_back_forward_history():
    nav = NavigationHistory()
    for s in ("SNDK", "NVDA", "AAPL"):
        nav.push(s)
    assert nav.current == "AAPL" and nav.can_back() and not nav.can_forward()
    assert nav.back() == "NVDA" and nav.back() == "SNDK" and nav.back() is None and nav.current == "SNDK"
    assert nav.forward() == "NVDA" and nav.forward() == "AAPL" and nav.forward() is None
    nav.back()
    nav.push("MSFT")                                                                # branching from the past truncates the forward stack
    assert nav.items == ["SNDK", "NVDA", "MSFT"] and not nav.can_forward()
    nav.push("MSFT")                                                                # the current ticker again: no-op
    assert nav.items == ["SNDK", "NVDA", "MSFT"] and nav.index == 2
    assert "nav.back()" in JS and "nav.forward()" in JS and 'id="tv-back"' in JS and 'id="tv-forward"' in JS and "switchTo(s, false)" in JS


# ---------------------------------------------------------------- 6 / 7 symbols -------------------------------------------------

def test_06_arbitrary_valid_symbol(api, ts):
    r = api.get("/api/terminal/state/msft")
    assert r.status_code == 200
    j = r.json()
    assert j["symbol"] == "MSFT" and len(j["candles"]) == 240 and j["quote"]["status"] == "OK" and j["tradability"]["status"] == "SIMULATED"
    assert set(j) == {"label", "symbol", "quote", "position", "evidence", "ppe", "demand", "tradability", "data_status", "actions", "market_state", "candles"}   # "demand": Stage 5.7
    assert api.get("/api/terminal/candles/brk.b?limit=5").json()["symbol"] == "BRK.B"
    assert len(api.get("/api/terminal/state/shop?candles=10").json()["candles"]) == 10
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        assert burst(ws, "shop")[0]["symbol"] == "SHOP"
    assert ts.market.quote("MSFT").price != ts.market.quote("SHOP").price            # distinct deterministic paths per symbol


def test_07_invalid_and_unknown_symbols_fail_cleanly(api, ts):
    r = api.get("/api/terminal/state/12345")
    assert r.status_code == 400 and r.json()["status"] == "INVALID_SYMBOL"
    assert api.get("/api/terminal/state/AAPL?candles=-1").status_code == 422 and api.get("/api/terminal/candles/AAPL?limit=0").status_code == 422
    assert api.get("/api/terminal/state/" + "A" * 11).status_code == 400

    class NoData(SimulatedMarket):
        def resolve(self, symbol):
            s = super().resolve(symbol)
            if s == "ZZZZ":
                raise UnknownSymbol("No market data for 'ZZZZ'.")
            return s
    ts.market = NoData(seed=1, start_time=T0)
    r = api.get("/api/terminal/state/zzzz")
    assert r.status_code == 404 and r.json() == {"status": "UNKNOWN_SYMBOL", "message": "No market data for 'ZZZZ'."}
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        ws.send_json({"type": "subscribe", "symbol": "ZZ$$"})
        e = ws.receive_json()
        assert e["type"] == "error" and e["code"] == "INVALID_SYMBOL"
        ws.send_json({"type": "subscribe", "symbol": "zzzz"})
        e = ws.receive_json()
        assert e["code"] == "UNKNOWN_SYMBOL" and e["symbol"] == "ZZZZ" and len(ts.hub) == 0
        ws.send_json({"type": "subscribe"})
        assert ws.receive_json()["code"] == "INVALID_SYMBOL"
        ws.send_json({"type": "refresh"})
        assert ws.receive_json()["code"] == "NOT_SUBSCRIBED"
        ws.send_json([1, 2])
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
        assert burst(ws, "NVDA")[0]["symbol"] == "NVDA"                                 # the connection is still usable


# ---------------------------------------------------------------- 8 watchlist ---------------------------------------------------

def test_08_watchlist_adapter_uses_existing_state(api, ts, tmp_path):
    from scanner.watchlist import load_watchlist
    assert inspect.signature(TerminalStream.__init__).parameters["watchlist_fn"].default is load_watchlist   # production = watchlist.txt
    assert api.get("/api/terminal/symbols").json()["watchlist"] == WL
    f = tmp_path / "watchlist.txt"
    f.write_text("# comment\nnvda\n\nMU\nnvda\nbad$sym\n", encoding="utf-8")
    s2 = make_stream(watchlist_fn=lambda: load_watchlist(f))
    assert s2.watch_symbols() == ["NVDA", "MU"] and s2.symbols()["default"] == "NVDA" and s2.symbols()["watchlist_source"].startswith("watchlist.txt")
    s3 = make_stream(watchlist_fn=lambda: load_watchlist(tmp_path / "missing.txt"))
    assert s3.watch_symbols() == [] and s3.symbols()["default"] == "AAPL" and s3.watchlist_payload()["items"] == []
    s4 = make_stream(watchlist_fn=lambda: (_ for _ in ()).throw(OSError("unreadable")))
    assert s4.watch_symbols() == []
    assert not list((ROOT / "database").glob("*watch*")) and not list((ROOT / "database").glob("*trader*"))   # no second watchlist store
    src = tv_source()
    assert "add_to_watchlist" not in src and "remove_from_watchlist" not in src and "write_text" not in src and "open(" not in src


# ---------------------------------------------------------------- 9 / 10 positions ----------------------------------------------

def _alpaca_connected():
    return {"connection": {"state": "CONNECTED"}, "refreshed_at": "2026-10-20T14:00:00+00:00"}


def _alpaca_view(rows):
    return {"refreshed_at": "2026-10-20T14:00:00+00:00", "connection": {"state": "CONNECTED", "label": "Connected"}, "positions": rows}


def _local_view(rows, account=True):
    return {"account": {"account_id": "x"} if account else None, "summary": {"mark_session": "2026-10-17"}, "positions": rows}


def test_09_position_state_is_read_only():
    calls = []
    a = StockAgentPositionAdapter(
        alpaca_status=lambda: calls.append("status") or _alpaca_connected(),
        alpaca_view=lambda: calls.append("view") or _alpaca_view([{"symbol": "NVDA", "qty": "10", "side": "long", "avg_entry_price": "100.00",
                                                                    "market_value": "1200.00", "unrealized_pl": "200.00", "unrealized_plpc": "0.2"}]),
        local_view=lambda: calls.append("local") or _local_view([
            {"symbol": "NVDA", "shares": "5", "average_cost": "90.00", "market_value": "600.00", "unrealized_pnl": "150.00", "cost_basis": "450.00", "mark": "120.00"},
            {"symbol": "AMD", "shares": "0", "average_cost": "1", "market_value": "0", "unrealized_pnl": "0", "cost_basis": "0", "mark": None}]),
        robinhood_view=lambda: calls.append("rh") or (_ for _ in ()).throw(AssertionError("must not be called")),
        awareness_enabled=lambda: False, cache_seconds=30, now_fn=lambda: 0.0)
    p = a.position("nvda")
    assert p.symbol == "NVDA" and p.has_position and [s.source for s in p.sources] == ["ALPACA_PAPER_VIEW", "LOCAL_PAPER", "ROBINHOOD_READ_ONLY"]
    ap, lp, rp = p.sources
    assert ap.status == "POSITION" and ap.label == "Alpaca Paper View" and ap.quantity == "10" and ap.average_cost == "100.00" and ap.market_value == "1200.00"
    assert ap.unrealized_pnl == "200.00" and ap.unrealized_pnl_pct == "20.00" and ap.as_of.startswith("2026-10-20")
    assert lp.status == "POSITION" and lp.quantity == "5" and lp.average_cost == "90.00" and lp.unrealized_pnl_pct == "33.33" and lp.as_of == "2026-10-17"
    assert rp.status == "DISABLED" and "rh" not in calls
    assert "total" not in p.model_dump_json() and p.note.startswith("Each source is shown separately")
    amd = a.position("AMD")
    assert not amd.has_position and amd.sources[0].status == "NO_POSITION" and amd.sources[1].status == "NO_POSITION"    # zero shares = no position
    n = len(calls)
    a.position("NVDA")
    assert len(calls) == n                                                              # cached within the poll interval
    a.invalidate("NVDA")
    a.position("NVDA")
    assert len(calls) > n
    # one failing source never hides the others
    b = StockAgentPositionAdapter(alpaca_status=lambda: (_ for _ in ()).throw(RuntimeError("x")), alpaca_view=lambda: {}, local_view=lambda: _local_view([]),
                                  robinhood_view=lambda: None, awareness_enabled=lambda: False, cache_seconds=0, now_fn=lambda: 0.0)
    s = b.position("NVDA").sources
    assert s[0].status == "ERROR" and "RuntimeError" in s[0].message and s[1].status == "NO_POSITION" and s[2].status == "DISABLED"
    nr = StockAgentPositionAdapter(alpaca_status=lambda: {"connection": {"state": "READY"}, "refreshed_at": None}, alpaca_view=lambda: (_ for _ in ()).throw(AssertionError("no view without a refresh")),
                                   local_view=lambda: _local_view([], account=False), robinhood_view=lambda: None, awareness_enabled=lambda: False, cache_seconds=0, now_fn=lambda: 0.0)
    s = nr.position("NVDA").sources
    assert s[0].status == "NOT_REFRESHED" and "never calls the broker" in s[0].message and s[1].status == "NOT_SET_UP"
    # the real adapter against the isolated test database: reads only, the file is byte-identical afterwards
    import conftest
    before = hashlib.sha256(conftest.TEST_DB.read_bytes()).hexdigest() if conftest.TEST_DB.exists() else None
    real = StockAgentPositionAdapter(cache_seconds=0, now_fn=lambda: 0.0).position("NVDA")
    assert [x.source for x in real.sources] == ["ALPACA_PAPER_VIEW", "LOCAL_PAPER", "ROBINHOOD_READ_ONLY"]
    assert real.sources[0].status in ("NOT_CONFIGURED", "NOT_REFRESHED") and real.sources[1].status in ("NOT_SET_UP", "NO_POSITION", "POSITION")
    assert real.sources[2].status in ("DISABLED", "UNAVAILABLE", "ERROR", "NO_POSITION", "POSITION")
    after = hashlib.sha256(conftest.TEST_DB.read_bytes()).hexdigest() if conftest.TEST_DB.exists() else None
    assert before == after


def test_10_no_position_renders_correctly(ts):
    st = ts.state("AMD", with_candles=False)
    assert not st.position.has_position and [s.status for s in st.position.sources] == ["NOT_REFRESHED", "NO_POSITION", "DISABLED"]
    assert st.data_status.position == "OK"
    assert "No position" in JS and 'status === "NO_POSITION"' in JS and "renderPosition" in JS
    empty = TerminalStream(SimulatedMarket(seed=1, start_time=T0), NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: [], run_loop=False,
                           now_fn=lambda: T0).state("AAPL", with_candles=False)
    assert empty.position.sources == [] and empty.data_status.position == "UNAVAILABLE" and empty.evidence.status == "NOT_LOADED"

    class Boom:
        def position(self, symbol):
            raise RuntimeError("sources down")

        def invalidate(self, symbol=None):
            pass
    broken = make_stream(positions=Boom()).state("AAPL", with_candles=False)
    assert not broken.position.has_position and "RuntimeError" in broken.position.note and broken.data_status.position == "UNAVAILABLE"


# ---------------------------------------------------------------- 11 / 12 evidence ----------------------------------------------

def test_11_existing_evidence_packet_loads(api, tmp_path, monkeypatch):
    import evidence_fakes as F
    from evidence import engine as EN
    from evidence import net
    from evidence.store import EvidenceStore
    monkeypatch.setattr(config, "EVIDENCE_NETWORK_ENABLED", True)
    monkeypatch.setattr(net, "WIRE", F.FakeWire())
    monkeypatch.setattr(config, "SEC_USER_AGENT", F.SEC_UA)
    monkeypatch.setattr(config, "EVIDENCE_IR_DOMAINS", F.IR_DOMAINS)
    store = EvidenceStore(tmp_path / "ev.db")
    first = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, store=store)

    class Rec(EvidenceResearchAdapter):
        calls = []

        def latest(self, symbol):
            self.calls.append(symbol)
            return super().latest(symbol)
    adapter = Rec(store_factory=lambda: store)
    e = adapter.latest("sndk")
    assert e.status == "LOADED" and e.symbol == "SNDK" and e.packet_id == first["packet"]["packet_id"] and e.packet_status == "COMPLETED"
    assert e.strength in STRENGTHS and e.cause in CAUSE_VERDICTS and e.company_confirm in RUNG_STATUSES and e.market_confirm in RUNG_STATUSES
    assert [r["rung"] for r in e.rungs] == [1, 2, 3, 4, 5, 6] and all(r["status"] in RUNG_STATUSES for r in e.rungs)
    assert e.data_status == "STORED" and e.created_at and e.as_of_date == F.AS_OF.isoformat() and e.disclaimer == STRENGTH_DISCLAIMER and e.message is None
    assert e.n_items == first["packet"]["n_items"] and e.cause_reason
    second = EN.build_packet(F.request(lookback_days=60), adapters=F.adapters(), now_fn=lambda: F.NOW + timedelta(minutes=5), store=store)   # a different request: a new packet
    assert adapter.latest("SNDK").packet_id == second["packet"]["packet_id"]          # latest = most recently created
    assert adapter.latest("NVDA").status == "NOT_LOADED"
    # through the stream: loaded on subscribe and refresh only, never per tick, and the packet is only READ (the store has the same rows)
    rows_before = len(store.packets(limit=200))
    s = make_stream(research=adapter)
    monkeypatch.setitem(RT.STREAM, "instance", s)
    Rec.calls.clear()
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        b = burst(ws, "SNDK")
        ev = b[-1]
        assert ev["type"] == "evidence" and ev["status"] == "LOADED" and ev["strength"] == e.strength and ev["packet_id"] == second["packet"]["packet_id"]
        for i in range(1, 12):
            ws.send_json({"type": "tick", "now": T0 + i})
        marker(ws)
        assert Rec.calls == ["SNDK"]
        ws.send_json({"type": "refresh"})
        got = marker(ws)
        assert [m["type"] for m in got] == ["position", "evidence", "ppe", "demand"] and Rec.calls == ["SNDK", "SNDK"]   # "demand": Stage 5.7
    assert len(store.packets(limit=200)) == rows_before
    j = api.get("/api/terminal/state/SNDK?candles=0").json()["evidence"]
    assert j["status"] == "LOADED" and j["company_confirm"] in RUNG_STATUSES


def test_12_missing_evidence_renders_not_loaded(tmp_path):
    from database.research_packet_migrations import run_research_packet_migrations
    from evidence.store import EvidenceStore
    a = EvidenceResearchAdapter(store_factory=lambda: EvidenceStore(tmp_path / "none.db"))     # no database file at all
    e = a.latest("NVDA")
    assert e.status == "NOT_LOADED" and e.data_status == "NOT_LOADED" and "Strategy Lab" in e.message and e.strength is None and e.rungs == []
    p = tmp_path / "empty.db"
    conn = sqlite3.connect(str(p))
    run_research_packet_migrations(conn)
    conn.commit()
    conn.close()
    assert EvidenceResearchAdapter(store_factory=lambda: EvidenceStore(p)).latest("NVDA").status == "NOT_LOADED"

    def boom():
        raise sqlite3.OperationalError("locked")
    f = EvidenceResearchAdapter(store_factory=boom).latest("NVDA")
    assert f.status == "FAILED" and "OperationalError" in f.message
    assert "NOT LOADED" in JS and "Deep research integration coming in the next step." in JS
    assert EvidenceResearchAdapter().latest("ZZZZ").status in ("NOT_LOADED", "LOADED")   # the default reader opens the app database read-only
    s = make_stream(research=FakeResearch()).state("NVDA", with_candles=False)
    assert s.evidence.status == "NOT_LOADED" and s.data_status.evidence == "NOT_LOADED"


# ---------------------------------------------------------------- 13 PPE --------------------------------------------------------

def test_13_ppe_is_the_engine_result_never_a_fabricated_placeholder(api, ts):
    """Stage 5.6 filled the Stage 5.5 placeholder: the slot now carries the deterministic PPE result (simulated data in these tests)."""
    ppe = ts.state("NVDA", with_candles=False).ppe
    assert ppe.status == "SIMULATED_DATA" and ppe.symbol == "NVDA" and ppe.disclaimer.startswith("PPE Confidence measures confidence in the overreaction")
    assert ppe.stretch.status in ("AVAILABLE", "INSUFFICIENT_DATA") and set(ppe.weights) == {"price_dislocation", "abnormal_return", "relative_sector_move",
                                                                                            "volume_shock", "gap_extension", "short_term_exhaustion"}
    assert ppe.confidence.status in ("EVIDENCE_NOT_AVAILABLE", "NOT_APPLICABLE", "AVAILABLE", "STRETCH_NOT_AVAILABLE")
    cfg = api.get("/api/terminal/config").json()["ppe"]
    assert cfg["version"] == "ppe_v1" and cfg["refresh_s"] >= 1 and "never per quote tick" in cfg["cadence"]
    assert "PPE Stretch" in JS and "PPE Confidence" in JS and "NOT AVAILABLE" in JS and "SIMULATED DATA" in JS
    assert not re.search(r"(?i)(stretch|confidence)\s*[:=]\s*[0-9.]+", tv_source() + JS)   # no fabricated number anywhere


# ---------------------------------------------------------------- 14 / 15 / 16 / 17 safety ------------------------------------

def test_14_action_buttons_cannot_execute_orders(api, ts, monkeypatch):
    acts = {a.id: a for a in ts.state("NVDA", with_candles=False).actions}
    assert set(acts) == {"prepare_buy", "prepare_sell", "research", "add_alert"}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False and acts["add_alert"].enabled is False
    assert acts["prepare_buy"].status == acts["prepare_sell"].status == "PREVIEW_NOT_CONNECTED" and acts["prepare_buy"].message == NOT_CONNECTED
    assert acts["research"].status == "NAVIGATE_ONLY" and "no model" in acts["research"].message
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/terminal")}
    assert mine == {"/api/terminal/config": {"get"}, "/api/terminal/symbols": {"get"}, "/api/terminal/state/{symbol}": {"get"},
                    "/api/terminal/candles/{symbol}": {"get"}, "/api/terminal/ppe/{symbol}": {"get"}, "/api/terminal/demand/{symbol}": {"get"}}   # demand: Stage 5.7, GET only
    for p in mine:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|alert|alpaca|robinhood|trade", p), p
    for path in ("/api/terminal/config", "/api/terminal/symbols", "/api/terminal/state/NVDA", "/api/terminal/candles/NVDA"):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert api.request(method, path, json={"side": "buy", "symbol": "NVDA", "qty": 1}).status_code == 405, (method, path)
    assert "ACTION_TEXT" in JS and "PREVIEW NOT CONNECTED" in JS and 'data-sfv="evidence54"' in JS and 'aria-disabled="${a.enabled ? "false" : "true"}"' in JS
    assert not re.search(r"""method\s*:\s*["'](POST|PUT|PATCH|DELETE)["']""", JS) and "fetch(" in JS
    assert not re.search(r"/api/(paper-orders|alpaca-paper-orders|alpaca-paper/refresh|portfolio/scenario|saved-scans|strategy-alerts|agent/chat)|/v2/orders|TradingClient", JS)
    assert JS.count("ws.send(") == 1 and "st.ws.send(JSON.stringify(m))" in JS           # one socket writer, fed only by the SubscriptionManager
    assert set(re.findall(r'type: "(\w+)"', CORE)) == {"unsubscribe", "subscribe"}
    assert NullExecutionPreviewAdapter().preview("buy", "nvda") == {"status": "NOT_CONNECTED", "side": "BUY", "symbol": "NVDA", "message": NOT_CONNECTED}
    routes = (ROOT / "api" / "routes" / "trader_view.py").read_text(encoding="utf-8")
    stream = (ROOT / "trader_view" / "stream.py").read_text(encoding="utf-8")
    assert "preview(" not in routes and "preview(" not in stream and "@router.post" not in routes and "@router.put" not in routes and "@router.delete" not in routes
    with api.websocket_connect(WS_PATH) as ws:                                          # the socket refuses anything that is not a read command
        hello_of(ws)
        for cmd in ({"type": "order"}, {"type": "prepare_buy", "symbol": "NVDA"}, {"type": "execute"}, {"type": "preview", "side": "buy"}, {"type": "confirm"}):
            ws.send_json(cmd)
            e = ws.receive_json()
            assert e["type"] == "error" and e["code"] == "BAD_MESSAGE", cmd
    prod = TerminalStream(SimulatedMarket(seed=1, start_time=T0), NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: [], run_loop=False)
    assert prod.allow_manual_tick is False
    monkeypatch.setitem(RT.STREAM, "instance", prod)
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        ws.send_json({"type": "tick"})
        assert ws.receive_json()["code"] == "NOT_ALLOWED"                              # the test-only command is refused in production


FORBIDDEN_IMPORTS = ("alpaca.trading", "paper.alpaca_order", "paper.alpaca_orders", "paper.execution", "paper.alpaca_readonly", "robin_stocks",
                     "robinhood_sync", "agents", "anthropic", "ai_explain", "evidence.engine", "evidence.explain", "evidence.net", "evidence.sources",
                     "research_flow", "rotation", "requests", "urllib", "mcp", "subprocess", "forward", "notifications", "insights")


def test_15_no_execution_or_order_writer_module_imported():
    for rel in TV_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == p or name.startswith(p + ".") for p in FORBIDDEN_IMPORTS), (rel, name)
        assert not re.search(r"TradingClient|TradingStream|messages\.create|get_provider|one_request|/v2/orders|alpaca_order_writer|W\.submit|\.submit\(|"
                             r"place_order|cancel_order|requests\.(get|post)|urlopen|\beval\(|\bexec\(|__import__|os\.system|subprocess|build_packet", text), rel
    import api.routes.trader_view as route_mod
    import trader_view.adapters, trader_view.hub, trader_view.market_real, trader_view.models, trader_view.positions, trader_view.research, trader_view.simulated, trader_view.stream  # noqa: E401
    for mod in (route_mod, trader_view.adapters, trader_view.hub, trader_view.market_real, trader_view.models, trader_view.positions, trader_view.research,
                trader_view.simulated, trader_view.stream):
        for value in vars(mod).values():
            if isinstance(value, types.ModuleType):
                assert not any(value.__name__ == p or value.__name__.startswith(p + ".") for p in FORBIDDEN_IMPORTS), (mod.__name__, value.__name__)
    assert not list((ROOT / "database").glob("*trader*")) and not list((ROOT / "database").glob("*terminal*"))      # no new table, no migration


def test_16_no_robinhood_write_call(monkeypatch):
    from api.routes.portfolio import load_view
    from portfolio.provider import PortfolioUnavailable
    calls = []

    class FakeProvider:
        def __getattr__(self, name):
            def fn(*a, **k):
                calls.append(name)
                raise PortfolioUnavailable("DISABLED", "fake gateway")
            return fn
    common = dict(alpaca_status=lambda: {"connection": {"state": "NOT_CONFIGURED"}}, alpaca_view=lambda: {}, local_view=lambda: _local_view([], account=False),
                  cache_seconds=0, now_fn=lambda: 0.0)
    a = StockAgentPositionAdapter(robinhood_view=lambda: calls.append("view") or None, awareness_enabled=lambda: False, **common)
    assert a.position("NVDA").sources[2].status == "DISABLED" and calls == []                      # off: nothing is called at all
    b = StockAgentPositionAdapter(robinhood_view=lambda: load_view(FakeProvider(), with_events=False), awareness_enabled=lambda: True, **common)
    src = b.position("NVDA").sources[2]
    assert src.status == "UNAVAILABLE" and "DISABLED: fake gateway" == src.message and calls == ["get_portfolio"]   # the existing read-only loader, reads only
    view = SimpleNamespace(status="OK", snapshot=SimpleNamespace(fetched_at="2026-10-20T13:00:00Z"),
                           positions=[SimpleNamespace(symbol="NVDA", quantity="3", avg_cost="95.5", market_value="360", unrealized_pnl="73.5",
                                                      unrealized_pnl_pct="25.65", quote_quality="GOOD")])
    c = StockAgentPositionAdapter(robinhood_view=lambda: view, awareness_enabled=lambda: True, **common)
    s = c.position("nvda").sources[2]
    assert s.status == "POSITION" and s.label == "Robinhood Read Only" and s.quantity == "3" and s.average_cost == "95.5" and s.unrealized_pnl_pct == "25.65"
    assert s.as_of == "2026-10-20T13:00:00Z" and "GOOD" in s.message
    assert c.position("AMD").sources[2].status == "NO_POSITION"
    assert not re.search(r"place_|cancel_|/orders|rh_gateway|robin_stocks|\.post\(|requests\.|robinhood_sync", tv_source())
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)                            # the production flag is the existing opt-in, read at call time
    d = StockAgentPositionAdapter(robinhood_view=lambda: calls.append("real") or None, **common)
    assert d.position("NVDA").sources[2].status == "DISABLED" and "real" not in calls


def test_17_no_real_model_call(api, ts, monkeypatch):
    import anthropic

    def boom(self, *a, **k):
        raise AssertionError("a model client was constructed by Trader View")
    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    assert api.get("/api/terminal/config").status_code == 200 and api.get("/api/terminal/state/NVDA").status_code == 200
    assert api.get("/api/terminal/candles/NVDA").status_code == 200 and api.get("/api/terminal/symbols").status_code == 200
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        burst(ws, "SNDK")
        burst(ws, "NVDA")
        ws.send_json({"type": "refresh"})
        for i in range(1, 6):
            ws.send_json({"type": "tick", "now": T0 + i})
        marker(ws)
    assert ts.research.calls == ["NVDA", "SNDK", "NVDA", "NVDA"]                                   # GET state, subscribe, subscribe, refresh — never per tick
    assert not re.search(r"anthropic|get_provider|\.analyze\(|messages\.create|build_packet|run_tool_loop|LLMProvider|technical_agent", tv_source() + JS)


def test_18_chart_asset_is_local_not_cdn(api):
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html) and not re.search(r"unpkg|jsdelivr|cdnjs|cdn\.", html, re.I)
    f = ROOT / "frontend" / "vendor" / "lightweight-charts" / "lightweight-charts.standalone.production.js"
    assert f.exists() and hashlib.sha256(f.read_bytes()).hexdigest() == CHART_SHA
    head = f.read_bytes()[:400].decode("utf-8", "replace")
    assert "Lightweight Charts" in head and "v4.2.3" in head and "Apache License 2.0" in head
    assert (f.parent / "LICENSE").read_text(encoding="utf-8").lstrip().startswith("Apache License")
    r = api.get("/vendor/lightweight-charts/lightweight-charts.standalone.production.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and len(r.content) == f.stat().st_size
    assert "window.LightweightCharts" in JS and "drawFallback" in JS and not re.search(r"https?://", JS)
    from trader_view.stream import CHART_ASSET
    assert CHART_ASSET["version"] == "4.2.3" and CHART_ASSET["served_from"].startswith("/vendor/")
    assert html.index("lightweight-charts.standalone.production.js") < html.index("trader_core.js") < html.index("trader_view.js")
    assert html.index("ai_history.js") < html.index("trader_core.js")                             # appended after every existing script


def test_19_simulator_fallback_still_works(monkeypatch):
    from trader_view.market_real import RealMarketData
    assert resolve_mode("auto", credentials=False) == "simulated" and resolve_mode("auto", credentials=True) == "real"
    assert resolve_mode("simulated", credentials=True) == "simulated" and resolve_mode("real", credentials=False) == "real"
    assert resolve_mode("garbage", credentials=False) == "simulated"
    monkeypatch.setattr(config, "TRADER_DATA_MODE", "simulated")
    assert isinstance(build_market_adapter(credentials=True), SimulatedMarket)
    real = build_market_adapter("real", credentials=True)
    assert isinstance(real, RealMarketData) and real._client is None                            # nothing is contacted on construction
    monkeypatch.setattr(config, "TRADER_DATA_MODE", "auto")
    assert isinstance(build_market_adapter(credentials=False), SimulatedMarket)
    a, b = SimulatedMarket(seed=3, start_time=T0), SimulatedMarket(seed=3, start_time=T0)
    for m in (a, b):
        m.resolve("MSFT")
        m.resolve("AAPL")
    for i in range(1, 130):
        a.step(T0 + i)
        b.step(T0 + i)
    for s in ("MSFT", "AAPL"):
        assert [c.model_dump() for c in a.candles(s, 0)] == [c.model_dump() for c in b.candles(s, 0)] and a.quote(s) == b.quote(s)
    assert SimulatedMarket(seed=4, start_time=T0).quote("MSFT") != a.quote("MSFT")
    hist = a.candles("MSFT", 0)
    assert len(hist) == 243 and all(c.time % 60 == 0 for c in hist) and hist[-1].final is False and all(c.final for c in hist[:-1])
    assert all(hist[i].time < hist[i + 1].time for i in range(len(hist) - 1))
    assert all(c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high and c.volume > 0 for c in hist)
    q = a.quote("MSFT")
    assert q.status == "OK" and q.bid < q.price < q.ask and q.ts == T0 + 129 and q.source.endswith("NOT a market feed") and q.prev_close
    assert a.market_state()["state"] == "SIMULATED" and a.tradability("MSFT").status == "SIMULATED" and a.current_candle("MSFT").final is False
    with pytest.raises(ValueError):
        a.resolve("12345")
    a.step(T0 + 50)
    assert a.quote("MSFT") == q                                                                   # an earlier clock never rewinds the stream
    s = TerminalStream(SimulatedMarket(seed=7, start_time=T0), NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: ["AAPL"], run_loop=False,
                       now_fn=lambda: T0)
    st = s.state("NVDA")
    assert st.data_status.mode == "simulated" and st.data_status.market == "SIMULATED" and st.market_state["state"] == "SIMULATED" and len(st.candles) == 240
    assert s.quote_every == 1 and s.cadence()["idle"].startswith("nothing is polled")


def test_20_frontend_core_logic_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the same rules are tested in Python (Hub, accepts_message, NavigationHistory)")
    cmd = [node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_core.test.cjs")]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout and "# pass 6" in r.stdout, r.stdout


# ---------------------------------------------------------------- 21 real adapter (fakes, no network) ---------------------------

def _snap(symbol, price, prev, bid, ask, minute, vol, ts):
    mk = SimpleNamespace
    t = datetime.fromtimestamp(ts, tz=timezone.utc)
    return mk(symbol=symbol, latest_trade=mk(price=price, timestamp=t), latest_quote=mk(bid_price=bid, ask_price=ask, timestamp=t),
              minute_bar=mk(timestamp=datetime.fromtimestamp(ts - ts % 60, tz=timezone.utc), open=minute[0], high=minute[1], low=minute[2], close=minute[3], volume=minute[4]),
              daily_bar=mk(volume=vol), previous_daily_bar=mk(close=prev))


class FakeDataClient:
    def __init__(self):
        self.snap_calls, self.bar_calls, self.fail = [], [], False

    def get_stock_snapshot(self, req):
        self.snap_calls.append(list(req.symbol_or_symbols))
        if self.fail:
            raise RuntimeError("feed down")
        out = {}
        for s in req.symbol_or_symbols:
            if s == "NVDA":
                out[s] = _snap("NVDA", 121.7, 120.0, 121.68, 121.72, (121.5, 121.9, 121.4, 121.7, 1500), 1e6, T0 + 5)
            if s == "AAPL":
                out[s] = _snap("AAPL", 190.0, 200.0, 189.9, 190.1, (190.1, 190.2, 189.8, 190.0, 900), 5e5, T0 + 5)
        return out

    def get_stock_bars(self, req):
        self.bar_calls.append(list(req.symbol_or_symbols))
        if self.fail:
            raise RuntimeError("feed down")
        s = req.symbol_or_symbols[0]
        bars = [SimpleNamespace(timestamp=datetime.fromtimestamp(T0 - T0 % 60 - 60 * i, tz=timezone.utc), open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=10 * i)
                for i in range(5)]
        return SimpleNamespace(data={s: bars})                                                     # newest first on purpose


def test_21_real_adapter_shapes_and_caching_without_network():
    from trader_view.market_real import RealMarketData
    clock, client = [0.0], FakeDataClient()
    r = RealMarketData(client_factory=lambda: client, feed="iex", quote_cache_s=3, bars_cache_s=60, history_minutes=240, now_fn=lambda: clock[0])
    assert r.mode == "real" and "read only" in r.source and r.resolve("nvda") == "NVDA"
    q = r.quote("NVDA")
    assert q.status == "OK" and q.price == 121.7 and q.bid == 121.68 and q.ask == 121.72 and q.prev_close == 120.0 and q.change_pct == 1.417
    assert q.volume == 1e6 and q.ts == T0 + 5 and q.code is None
    c = r.current_candle("NVDA")
    assert c.time == (T0 + 5) - (T0 + 5) % 60 and c.final is False and c.close == 121.7 and c.volume == 1500
    assert client.snap_calls == [["NVDA"]]                                                           # resolve + quote + candle = ONE request
    qs = r.quotes(["AAPL", "NVDA"])
    assert client.snap_calls == [["NVDA"], ["AAPL"]] and qs["AAPL"].change_pct == -5.0 and qs["NVDA"].price == 121.7
    clock[0] = 4.0
    r.quotes(["AAPL", "NVDA"])
    assert client.snap_calls[-1] == ["AAPL", "NVDA"]                                                 # stale: one batched request for both
    with pytest.raises(UnknownSymbol):
        r.resolve("ZZZZ")                                                                            # the feed answered without data
    assert r.quote("ZZZZ").code == "NO_DATA" and r.quote("ZZZZ").price is None
    bars = r.candles("NVDA", 3)
    assert [b.time for b in bars] == sorted(b.time for b in bars) and len(bars) == 3 and all(b.final for b in bars) and client.bar_calls == [["NVDA"]]
    r.candles("NVDA", 5)
    assert client.bar_calls == [["NVDA"]]                                                            # cached
    client.fail, clock[0] = True, 10.0
    q2 = r.quote("NVDA")
    assert q2.status == "UNAVAILABLE" and q2.code == "FEED_ERROR" and q2.price is None and "RuntimeError" in q2.message and r.last_error == "RuntimeError"
    assert r.resolve("NVDA") == "NVDA"                                                               # a feed error is not an unknown symbol
    clock[0] = 100.0
    assert len(r.candles("NVDA", 5)) == 5                                                            # keeps the last good history
    ms = r.market_state()
    assert ms["state"] == "LIVE_POLL" and ms["session_hint"] in ("REGULAR", "PRE_MARKET", "AFTER_HOURS", "CLOSED", "WEEKEND", "UNKNOWN") and ms["last_error"] == "RuntimeError"
    assert r.tradability("NVDA").status == "UNKNOWN" and r.step(T0) is None
    bad = RealMarketData(client_factory=lambda: (_ for _ in ()).throw(RuntimeError("Missing Alpaca API credentials")), now_fn=lambda: 0.0)
    assert bad.quote("NVDA").code == "FEED_ERROR" and bad.candles("NVDA", 10) == []                 # no credentials: UNAVAILABLE, never a fake price
    st = TerminalStream(r, NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: ["AAPL"], tick_seconds=1.0, quote_poll_seconds=3,
                        watchlist_poll_seconds=10, position_poll_seconds=30, run_loop=False, now_fn=lambda: T0)
    assert st.quote_every == 3 and st.watchlist_every == 10 and st.position_every == 30 and st.cadence()["quote_s"] == 3.0
    assert st.state("NVDA", with_candles=False).data_status.market == "UNAVAILABLE"


# ---------------------------------------------------------------- 22 cadence / idle ---------------------------------------------

def test_22_stream_cadence_idle_and_heartbeat(api, ts):
    with api.websocket_connect(WS_PATH) as ws:
        hello_of(ws)
        for i in range(1, 6):
            ws.send_json({"type": "tick", "now": T0 + i})                                            # connected, nothing subscribed
        got = marker(ws)
        assert [m["type"] for m in got] == ["heartbeat"] and got[0]["subscriptions"] == 0            # heartbeat only: no quote, no watchlist poll
        assert ts.positions.calls == [] and ts.research.calls == []
        burst(ws, "AAPL")
        for i in range(6, 31):
            ws.send_json({"type": "tick", "now": T0 + i})
        got = marker(ws)
        types = [m["type"] for m in got]
        assert types.count("quote") == 25 and types.count("candle") == 25 and types.count("watchlist") == 3 and types.count("heartbeat") == 5
        assert types.count("position") == 1 and ts.positions.calls.count("AAPL") == 2 and ts.research.calls == ["AAPL"]
        assert [m["ts"] for m in got if m["type"] == "quote"] == list(range(T0 + 6, T0 + 31))
        assert all(m["symbol"] == "AAPL" for m in got if m["type"] in SYMBOL_BOUND_TYPES) and ts.ticks == 30
        ws.send_json({"type": "ping"})
        hb = ws.receive_json()
        assert hb["type"] == "heartbeat" and hb["subscriptions"] == 1 and hb["market_state"]["state"] == "SIMULATED"
        ws.send_json({"type": "unsubscribe"})
        assert ws.receive_json()["ok"] is True and len(ts.hub) == 0
        for i in range(31, 36):
            ws.send_json({"type": "tick", "now": T0 + i})
        assert [m["type"] for m in marker(ws)] == ["heartbeat"]                                      # left the tab: back to heartbeats only
    assert ts.run_loop is False and ts._task is None


# ---------------------------------------------------------------- 23 regression: base files untouched ---------------------------

PINNED = ["rotation/engine.py", "paper/alpaca_orders.py", "paper/alpaca_order_writer.py", "paper/alpaca_readonly.py", "paper/alpaca_view.py",
          "paper/portfolio.py", "paper/execution.py", "evidence/engine.py", "evidence/rules.py", "evidence/store.py", "evidence/explain.py",
          "api/routes/evidence.py", "api/routes/watchlist.py", "api/routes/stocks.py", "api/routes/market.py", "api/routes/portfolio.py",
          "api/routes/alpaca_paper.py", "api/routes/alpaca_paper_orders.py", "api/routes/paper.py", "api/validation.py", "scanner/watchlist.py",
          "data/market_data.py", "portfolio/provider.py", "database/migrations.py", "frontend/app.js", "frontend/style.css",
          "frontend/evidence_research.js", "frontend/alpaca_orders.js", "frontend/alpaca_paper.js", "frontend/paper_portfolio.js",
          "frontend/strategy_fit.js", "tests/conftest.py", ".github/workflows/tests.yml"]


def test_23_prior_stage_files_identical_to_base_and_wiring_is_additive():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    for rel in PINNED:
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob(rel), f"{rel} differs from {BASE}"
    base = blob("api/server.py").decode("utf-8").splitlines()
    now = (ROOT / "api" / "server.py").read_text(encoding="utf-8").splitlines()
    assert [l for l in base if l not in now] == []
    added = [l for l in now if l not in base]
    assert len(added) == 2 and added[0] == "from api.routes import trader_view" and added[1].startswith("app.include_router(trader_view.router)  # Stage 5.5")
    base_cfg, now_cfg = blob("config.py").decode("utf-8"), (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert now_cfg.startswith(base_cfg.rstrip("\n")) and "TRADER_DATA_MODE" in now_cfg                   # config is append-only
    base_html = blob("frontend/index.html").decode("utf-8").splitlines()
    now_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8").splitlines()
    assert [l for l in base_html if l not in now_html] == []                                            # nothing removed from the page
    assert config.TRADER_DATA_MODE in ("auto", "real", "simulated") and config.TRADER_DEMO_SYMBOLS == ("AAPL", "NVDA", "SNDK")
