"""
Market Explorer, Phase A0 - the read-only boundary (explorer/: the package, its service and its route namespace).

  A  the flag: OFF by default, read at call time; while OFF the data routes answer 404, the service refuses, and nothing is read
  B  the reads: quote + candles and the exact-ticker search, for a watchlist symbol and for a symbol outside the watchlist; malformed input,
     a ticker the feed has no data for, and a feed that does not answer
  C  no trading effect: opening or searching through the explorer writes NO row in any table of the watched databases, opens no database,
     does not change the strategy universe, watchlist.txt, the configuration, the mode or the safety state - with POSITIVE CONTROLS: the
     Trader View socket subscription of the same symbols does write a setup evaluation, the table digest sees a probe row in every watched
     file, and the database tripwire sees an adapter that opens one. Around every test of the module, an autouse guard also checks that
     watchlist.txt, the environment and the configuration are unchanged, so a change made by an earlier test cannot hide in C01's baseline
  D  the dependency boundary: the explorer's import closure is exactly the pinned set and contains no forbidden module; the closure imports
     no trading client, no database module and no network module other than the Alpaca market-data client; GET routes only; the namespace
     is NOT mounted in the server in Phase A0 and no existing file names the explorer
  E  Trader View is untouched by an explorer read (its hub holds no subscription; its market adapter is another object); the default
     construction (no injected adapter) has the same properties

The behavioural tests run against BOTH adapters the explorer can hold: the simulator, and the real adapter (trader_view.market_real.RealMarketData,
the one a machine with market-data credentials uses) on a fake market-data client - no network, no credentials.
"""
import ast
import copy
import hashlib
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database.database as dbm  # noqa: E402
import explorer  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from data.market_data import get_data_client  # noqa: E402
from explorer import routes as RX  # noqa: E402
from explorer import service as XS  # noqa: E402
from setups.service import SetupService  # noqa: E402
from setups.store import SetupStore  # noqa: E402
from trader_view.adapters import NullPositionAdapter, NullResearchAdapter  # noqa: E402
from trader_view.market_real import RealMarketData  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402

T0 = 1_760_000_000
WATCHLIST = ["NVDA", "AMD"]
IN_LIST, OUTSIDE = "NVDA", "MSFT"            # one symbol of the strategy universe, one outside it
EXPLORER_DIR = os.path.normcase(str(ROOT / "explorer"))


class Clock:
    def __init__(self):
        self.now = datetime.fromtimestamp(T0, tz=timezone.utc)

    def __call__(self):
        return self.now


def _snap(price, ts):
    mk = SimpleNamespace
    t = datetime.fromtimestamp(ts, tz=timezone.utc)
    return mk(latest_trade=mk(price=price, timestamp=t), latest_quote=mk(bid_price=price - 0.02, ask_price=price + 0.02, timestamp=t),
              minute_bar=mk(timestamp=datetime.fromtimestamp(ts - ts % 60, tz=timezone.utc), open=price, high=price + 0.2, low=price - 0.2, close=price, volume=1500),
              daily_bar=mk(volume=1e6, open=price - 1), previous_daily_bar=mk(close=price - 2))


class FakeFeed:
    """A stand-in for the Alpaca market-data client (no network): snapshots and minute bars for three tickers, no data for any other ticker,
    and a switch that makes every request fail (the feed is down)."""
    KNOWN = {"NVDA": 121.7, "MSFT": 410.0, "AMD": 150.0}

    def __init__(self):
        self.snap_calls, self.bar_calls, self.fail = [], [], False

    def get_stock_snapshot(self, req):
        self.snap_calls.append(list(req.symbol_or_symbols))
        if self.fail:
            raise RuntimeError("feed down")
        return {s: _snap(self.KNOWN[s], T0 + 5) for s in req.symbol_or_symbols if s in self.KNOWN}

    def get_stock_bars(self, req):
        self.bar_calls.append(list(req.symbol_or_symbols))
        if self.fail:
            raise RuntimeError("feed down")
        s = req.symbol_or_symbols[0]
        p = self.KNOWN.get(s)
        bars = [] if p is None else [SimpleNamespace(timestamp=datetime.fromtimestamp(T0 - T0 % 60 - 60 * i, tz=timezone.utc), open=p, high=p + 0.5, low=p - 0.5,
                                                     close=p + 0.1, volume=100 + i) for i in range(300)]
        return SimpleNamespace(data={s: bars})                                                  # newest first, as the feed may answer


def explorer_market(kind):
    """(adapter, fake feed or None, its clock or None): the explorer's OWN adapter - the simulator, or the real adapter on a fake client."""
    if kind == "simulated":
        return SimulatedMarket(seed=2, start_time=T0), None, None
    feed, clock = FakeFeed(), [0.0]
    return RealMarketData(client_factory=lambda: feed, feed="iex", quote_cache_s=3, bars_cache_s=60, history_minutes=240, now_fn=lambda: clock[0]), feed, clock


def digest(path: Path) -> dict:
    """Every table of one SQLite file -> (row count, sha256 of all rows). A missing file is {}."""
    if not Path(path).exists():
        return {}
    conn = sqlite3.connect(str(path))
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            h, n = hashlib.sha256(), 0
            for row in conn.execute(f'SELECT * FROM "{name}"'):
                h.update(repr(row).encode("utf-8"))
                n += 1
            out[name] = (n, h.hexdigest())
        return out
    finally:
        conn.close()


def _fingerprint(items) -> str:
    """One hash of a mapping (never its values: the configuration and the environment hold credentials on some machines)."""
    return hashlib.sha256(repr(sorted((str(k), repr(v)) for k, v in items)).encode("utf-8")).hexdigest()


@contextmanager
def database_tripwire():
    """Lists every sqlite3.connect made from INSIDE an explorer call (any frame of the calling stack is a file of explorer/)."""
    real, hits = sqlite3.connect, []

    def connect(*a, **k):
        f = sys._getframe(1)
        while f is not None:
            if os.path.normcase(os.path.dirname(os.path.abspath(f.f_code.co_filename))) == EXPLORER_DIR:
                hits.append(str(a[0]) if a else "?")
                break
            f = f.f_back
        return real(*a, **k)

    sqlite3.connect = connect
    try:
        yield hits
    finally:
        sqlite3.connect = real


def _lab(tmp_path, mp, kind):
    """A production-shaped Trader View stream (its setup store in a temp file; its other stores in the session's test database, as in every
    Trader View test), the explorer on its OWN adapter of `kind`, a test application carrying the explorer routes, and the server application."""
    holder = {}
    setup_db = tmp_path / "setups.db"
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(setup_db), clock=Clock())
    stream = TerminalStream(SimulatedMarket(seed=1, start_time=T0), NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: list(WATCHLIST),
                            run_loop=False, now_fn=lambda: T0, setup_service=svc)
    holder["s"] = stream
    mp.setitem(RT.STREAM, "instance", stream)
    market, feed, clock = explorer_market(kind)
    xsvc = XS.ExplorerService(market=market)
    mp.setitem(XS._SERVICE, "instance", xsvc)
    from api.server import app
    dbs = [setup_db, Path(dbm._db_instance.db_path), Path(stream.proposals.store.path), Path(stream.execution.store.path), Path(stream.safety.store.path)]
    xapp = FastAPI()                                                                # Phase A0: the namespace is mounted on a test application only
    xapp.include_router(RX.router)
    return {"api": TestClient(xapp), "server": TestClient(app), "stream": stream, "setups": svc, "explorer": xsvc, "dbs": sorted({Path(p) for p in dbs}),
            "kind": kind, "mode": market.mode, "feed": feed, "clock": clock}


_ABSENT = object()


class _Hidden(dict):
    """A dict whose repr shows no value: pytest -l prints the locals of a failing frame, and these hold the environment and the configuration."""

    def __repr__(self):
        return f"<{len(self)} entries; values not shown>"


class _Saved:
    """What the guard compares and restores: the bytes of watchlist.txt, the environment and the uppercase configuration attributes (the objects,
    their reprs, and copies of the contents of list, dict and set values). No repr here or in restore()'s frame shows a value (pytest -l)."""

    def __init__(self):
        self.wl = Path(config.WATCHLIST_FILE)
        self.wl_bytes = self.wl.read_bytes() if self.wl.exists() else None
        self.env = _Hidden((k, v) for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST")
        self.cfg = _Hidden((k, v) for k, v in vars(config).items() if k.isupper())
        self.cfg_repr = _Hidden((k, repr(v)) for k, v in self.cfg.items())
        self.cfg_contents = _Hidden((k, copy.deepcopy(v)) for k, v in self.cfg.items() if type(v) in (list, dict, set))

    def __repr__(self):
        return "<saved state; values not shown>"

    def restore(self) -> list:
        """Puts back every change it can (the guard's docstring names the one kind it cannot undo) and returns the names of what had changed,
        never a value. The saved copies are copied again when used, so a restore never shares them with the live configuration."""
        changed = []
        if (self.wl.read_bytes() if self.wl.exists() else None) != self.wl_bytes:
            changed.append("watchlist.txt")
            if self.wl_bytes is None:
                self.wl.unlink(missing_ok=True)                                                # created during the test
            else:
                self.wl.write_bytes(self.wl_bytes)
        env = _Hidden((k, v) for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST")
        for k in sorted(set(self.env) | set(env)):
            if env.get(k, _ABSENT) != self.env.get(k, _ABSENT):
                changed.append(f"environment {k}")
                if k in self.env:
                    os.environ[k] = self.env[k]
                else:
                    os.environ.pop(k, None)
        cfg = _Hidden((k, v) for k, v in vars(config).items() if k.isupper())
        for k in sorted(set(self.cfg) | set(cfg)):
            if k not in self.cfg:
                changed.append(f"config.{k}")
                delattr(config, k)
            elif cfg.get(k, _ABSENT) is not self.cfg[k] or repr(cfg[k]) != self.cfg_repr[k]:
                changed.append(f"config.{k}")
                if k in self.cfg_contents:                                                      # a list, dict or set changed in place: its contents too
                    if type(self.cfg[k]) is list:
                        self.cfg[k][:] = copy.deepcopy(self.cfg_contents[k])
                    else:
                        self.cfg[k].clear()
                        self.cfg[k].update(copy.deepcopy(self.cfg_contents[k]))
                setattr(config, k, self.cfg[k])
        return changed


@pytest.fixture(autouse=True)
def nothing_lasting_changes():
    """Around EVERY test of this module, whatever ran before it: watchlist.txt (the strategy universe on disk), the process environment and the
    uppercase configuration attributes are the same after the test as before it. C01's snapshot compares only around its own calls, so an idempotent
    change made earlier in the run (a symbol that B01 appended to watchlist.txt, say) would already be in its baseline; this guard reports such a change
    in the test that made it. Names are reported, never values. Before it is reported, a change is put back: the file's bytes (a file the test
    created is removed), each environment variable, each configuration attribute, and the contents of a list, dict or set attribute changed in place.
    A change inside any other kind of attribute value is reported but cannot be undone.
    The tests patch through `mp` (below), which is undone before this guard compares; conftest's autouse fixtures patch through pytest's shared
    `monkeypatch`, which is set up before this guard and undone after it, so their patches are in place on both sides of the comparison. The server
    is imported here, before the baseline, as the rest of the suite imports it inside functions: its first import is not a change made by a test."""
    import api.server  # noqa: F401
    saved = _Saved()
    yield
    changed = saved.restore()
    assert changed == [], f"changed by this test and put back: {changed}"


@pytest.fixture
def mp(nothing_lasting_changes):
    """This module's own MonkeyPatch. It depends on the guard, so it is set up after it and undone before the guard compares."""
    with pytest.MonkeyPatch.context() as m:
        yield m


@pytest.fixture(params=["simulated", "real_adapter"])
def lab(request, tmp_path, mp):
    return _lab(tmp_path, mp, request.param)


@pytest.fixture
def sim_lab(tmp_path, mp):
    return _lab(tmp_path, mp, "simulated")


@pytest.fixture
def real_lab(tmp_path, mp):
    return _lab(tmp_path, mp, "real_adapter")


def snapshot(lab) -> dict:
    s = lab["stream"]
    return {"tables": {str(p): digest(p) for p in lab["dbs"]},
            "universe": list(s.setups.symbols_in_scope()),
            "watchlist_file": hashlib.sha256(Path(config.WATCHLIST_FILE).read_bytes()).hexdigest() if Path(config.WATCHLIST_FILE).exists() else None,
            "mode": dict(s.execution.store.mode()),
            "safety": repr(s.safety.store.state()),
            "config": _fingerprint((k, v) for k, v in vars(config).items() if k.isupper()),
            "environment": _fingerprint((k, v) for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"),
            "hub": s.hub.items(),
            "connections": len(s.connections)}


def subscribe(api, symbol):
    """The Trader View socket subscription (the path the explorer must never use)."""
    with api.websocket_connect("/api/terminal/stream") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "subscribe", "symbol": symbol})
        got = [ws.receive_json()["type"] for _ in range(8)]                          # a first watchlist push may precede the burst
        assert "subscribed" in got and "candles" in got, got


def spy(mp, market) -> list:
    """Every read of one adapter, by method name."""
    calls = []
    for name in ("resolve", "quote", "quotes", "candles", "current_candle"):
        orig = getattr(market, name)
        mp.setattr(market, name, lambda *a, _n=name, _o=orig, **k: calls.append(_n) or _o(*a, **k))
    return calls


# ---------------------------------------------------------------- A. the flag -------------------------------------------------------------

def test_A01_flag_is_off_by_default_and_read_at_call_time(mp):
    mp.delenv(explorer.FLAG, raising=False)
    assert explorer.enabled() is False and explorer.FLAG == "MARKET_EXPLORER_ENABLED"
    for v, want in (("true", True), ("1", True), ("ON", True), (" yes ", True), ("false", False), ("0", False), ("", False), ("enabled", False), ("2", False)):
        mp.setenv(explorer.FLAG, v)
        assert explorer.enabled() is want, v
    assert "MARKET_EXPLORER" not in (ROOT / "config.py").read_text(encoding="utf-8")          # the flag lives in the explorer package only


def test_A02_while_off_the_data_routes_answer_404_and_read_nothing(lab, mp):
    mp.delenv(explorer.FLAG, raising=False)
    calls = spy(mp, lab["explorer"].market)
    api = lab["api"]
    st = api.get("/api/explorer/status")
    assert st.status_code == 200 and st.json()["enabled"] is False and st.json()["read_only"] is True and st.json()["version"] == "explorer_a0"
    for path in (f"/api/explorer/symbols/{IN_LIST}", f"/api/explorer/symbols/{OUTSIDE}", f"/api/explorer/search?q={IN_LIST}", "/api/explorer/search?q=",
                 "/api/explorer/symbols/ZZ$$"):
        r = api.get(path)
        assert r.status_code == 404 and r.json()["status"] == "EXPLORER_DISABLED", (path, r.status_code, r.text)
    assert calls == []                                                                        # OFF means no market read at all
    assert lab["feed"] is None or (lab["feed"].snap_calls == [] and lab["feed"].bar_calls == [])


def test_A03_while_off_the_service_itself_refuses_and_reads_nothing(lab, mp):
    """The flag is not only a route check: a caller that reaches the service directly is refused too."""
    mp.delenv(explorer.FLAG, raising=False)
    calls = spy(mp, lab["explorer"].market)
    for read in (lambda: lab["explorer"].symbol(IN_LIST), lambda: lab["explorer"].search(OUTSIDE), lambda: lab["explorer"].resolve(IN_LIST)):
        with pytest.raises(explorer.ExplorerDisabled):
            read()
    assert calls == []
    mp.setattr(RX.explorer, "enabled", lambda: True)                                 # the route's check passes, the service's own check does not
    assert XS.enabled is not RX.explorer.enabled
    for path in (f"/api/explorer/symbols/{IN_LIST}", f"/api/explorer/search?q={OUTSIDE}"):
        r = lab["api"].get(path)
        assert r.status_code == 404 and r.json()["status"] == "EXPLORER_DISABLED", (path, r.text)
    assert calls == []


# ---------------------------------------------------------------- B. the reads ------------------------------------------------------------

@pytest.mark.parametrize("symbol", [IN_LIST, OUTSIDE])
def test_B01_symbol_view_returns_quote_and_candles(lab, mp, symbol):
    mp.setenv(explorer.FLAG, "true")
    r = lab["api"].get(f"/api/explorer/symbols/{symbol.lower()}?candles=30")
    d = r.json()
    assert r.status_code == 200 and d["symbol"] == symbol and d["read_only"] is True and d["mode"] == lab["mode"] and d["version"] == "explorer_a0"
    assert len(d["candles"]) == 30 and {"open", "high", "low", "close", "volume"} <= set(d["candles"][-1])
    assert [c["time"] for c in d["candles"]] == sorted(c["time"] for c in d["candles"])
    assert d["quote"]["symbol"] == symbol and d["quote"]["status"] == "OK" and d["quote"]["price"] > 0
    assert lab["api"].get(f"/api/explorer/symbols/{symbol}?candles=0").json()["candles"] == []
    assert set(d) == {"version", "symbol", "quote", "candles", "source", "mode", "read_only", "note"}       # market data only: no setup, proposal, position or action


@pytest.mark.parametrize("symbol", [IN_LIST, OUTSIDE])
def test_B02_search_is_an_exact_ticker_lookup(lab, mp, symbol):
    mp.setenv(explorer.FLAG, "true")
    d = lab["api"].get(f"/api/explorer/search?q={symbol.lower()}").json()
    assert d["results"] == [{"symbol": symbol, "data_available": True}] and d["match"] == "EXACT_TICKER" and d["read_only"] is True and d["query"] == symbol
    assert set(d) == {"version", "query", "results", "match", "read_only", "note"}


def test_B03_malformed_input_is_a_400_and_only_malformed_input_is(lab, mp):
    mp.setenv(explorer.FLAG, "true")
    api = lab["api"]
    calls = spy(mp, lab["explorer"].market)
    for path in ("/api/explorer/symbols/ZZ$$", "/api/explorer/symbols/12345", "/api/explorer/search?q=", "/api/explorer/search?q=A%20B"):
        r = api.get(path)
        assert r.status_code == 400 and r.json()["status"] == "INVALID_SYMBOL", (path, r.text)
    long = api.get("/api/explorer/symbols/" + "Z$" * 400)
    assert long.status_code == 400 and len(long.json()["message"]) <= 200                     # the echoed text is bounded
    assert api.get("/api/explorer/symbols/NVDA?candles=2001").status_code == 422 and api.get("/api/explorer/symbols/NVDA?candles=-1").status_code == 422
    assert api.get("/api/explorer/search?q=" + "A" * 33).status_code == 422
    assert calls == []                                                                        # none of these reaches the adapter

    def odd(symbol, candles=0):
        raise ValueError("a failure inside the read, not a malformed ticker")

    mp.setattr(lab["explorer"], "symbol", odd)
    with pytest.raises(ValueError, match="inside the read"):                                  # not reported as 400 INVALID_SYMBOL
        api.get("/api/explorer/symbols/NVDA")


def test_B04_a_ticker_without_data_is_unknown_and_a_feed_that_does_not_answer_is_503(real_lab, mp):
    """The real adapter never raises on a feed error: it answers an UNAVAILABLE / FEED_ERROR quote and keeps the symbol. The explorer must not
    present that as a chart (200) or as an available ticker."""
    mp.setenv(explorer.FLAG, "true")
    api, feed, clock = real_lab["api"], real_lab["feed"], real_lab["clock"]
    r = api.get("/api/explorer/symbols/ZZZZ")
    assert r.status_code == 404 and r.json()["status"] == "UNKNOWN_SYMBOL"
    d = api.get("/api/explorer/search?q=zzzz")
    assert d.status_code == 200 and d.json()["results"] == [] and d.json()["query"] == "ZZZZ"   # unknown ticker: an empty result, not an error
    assert api.get("/api/explorer/symbols/NVDA").status_code == 200
    feed.fail, clock[0] = True, 10.0                                                          # the feed goes down; the cached quote is stale
    for path in ("/api/explorer/symbols/NVDA", "/api/explorer/search?q=NVDA", "/api/explorer/symbols/TSLA", "/api/explorer/search?q=TSLA"):
        r = api.get(path)
        assert r.status_code == 503 and r.json()["status"] == "MARKET_UNAVAILABLE", (path, r.status_code, r.text)
        assert "data_available" not in r.text and "candles" not in r.text
    feed.fail, clock[0] = False, 20.0                                                         # ... and comes back
    assert api.get("/api/explorer/symbols/NVDA").status_code == 200
    assert api.get("/api/explorer/search?q=NVDA").json()["results"] == [{"symbol": "NVDA", "data_available": True}]
    assert api.get("/api/explorer/symbols/TSLA").status_code == 404                           # the feed answers: no data for this ticker


# ---------------------------------------------------------------- C. no trading effect ----------------------------------------------------

def test_C01_explorer_reads_write_nothing_and_change_no_universe_mode_or_safety_state(lab, mp):
    mp.setenv(explorer.FLAG, "true")
    api = lab["api"]
    s = lab["stream"]
    subscribe(lab["server"], "AMD")                                                                      # normal Trader View use first: the trading tables exist and hold rows
    counts_before, mode_before, _ = s.execution.store.counts(), s.execution.store.mode(), s.safety.store.state()   # (the stores create their tables on first use)
    before = snapshot(lab)
    names = {t for p in before["tables"].values() for t in p}
    assert before["universe"] == WATCHLIST and len(lab["dbs"]) >= 2
    assert {"execution_safety_state", "execution_safety_events"} <= names and any("setup" in t for t in names), sorted(names)   # the executor's tables are created on its first write: the store API below covers them
    assert len(s.setups.store.evaluations("AMD")) == 1                                         # real rows are being watched
    with database_tripwire() as opened:
        for symbol in (IN_LIST, OUTSIDE):                                                      # a watchlist symbol and one outside the watchlist
            for _ in range(3):
                assert api.get(f"/api/explorer/symbols/{symbol}").status_code == 200
                assert api.get(f"/api/explorer/symbols/{symbol}?candles=2000").status_code == 200
                assert api.get(f"/api/explorer/search?q={symbol}").status_code == 200
        assert api.get("/api/explorer/status").status_code == 200
    assert opened == []                                                                        # no database was opened from inside an explorer call
    after = snapshot(lab)
    assert after == before, {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert len(s.setups.store.evaluations("AMD")) == 1 and s.setups.store.evaluations(IN_LIST) == [] and s.setups.store.evaluations(OUTSIDE) == []          # no setup evaluation
    assert s.setups.symbols_in_scope() == WATCHLIST and not any(e.symbol in (IN_LIST, OUTSIDE) for e in s.setups.store.open_episodes())   # the strategy universe did not grow
    assert {"execution_intents", "execution_orders", "paper_positions"} <= set(counts_before)
    assert s.execution.store.counts() == counts_before                                         # no intent, no order, no position: every execution count is unchanged
    assert s.execution.store.mode() == mode_before                                             # the trading mode is unchanged (compared, not assumed: the session
                                                                                               # database starts as a copy of data/stock_agent.db where one exists)
    if lab["feed"] is not None:                                                                # the real adapter: one snapshot and one bars request per symbol, the rest from its cache
        assert lab["feed"].snap_calls == [[IN_LIST], [OUTSIDE]] and lab["feed"].bar_calls == [[IN_LIST], [OUTSIDE]]


@pytest.mark.parametrize("symbol", [IN_LIST, OUTSIDE])
def test_C02_positive_control_the_trader_view_subscription_does_write(lab, mp, symbol):
    """The detector of C01 is real: the SAME symbol opened through the Trader View socket leaves a setup evaluation; through the explorer, none."""
    mp.setenv(explorer.FLAG, "true")
    api, s = lab["api"], lab["stream"]
    before = snapshot(lab)
    assert api.get(f"/api/explorer/symbols/{symbol}").status_code == 200 and api.get(f"/api/explorer/search?q={symbol}").status_code == 200
    assert snapshot(lab) == before and s.setups.store.evaluations(symbol) == []
    subscribe(lab["server"], symbol)                                                                     # the path the explorer never uses
    ev = s.setups.store.evaluations(symbol)
    assert len(ev) == 1 and ev[0]["trigger"] == "SUBSCRIBE"
    after = snapshot(lab)
    assert after["tables"] != before["tables"]                                                 # ... and the table digest sees it
    changed = {t for p in after["tables"] for t in after["tables"][p] if after["tables"][p][t] != before["tables"][p].get(t)}
    assert any("setup" in t for t in changed), changed


def test_C03_positive_control_the_table_digest_sees_a_row_in_every_watched_database(sim_lab):
    """C02 shows the digest catching a write in the setup database. This shows it for every watched file, the session's test database included:
    a probe row is seen, a changed value with the same row count is seen, and the file reads as before once the probe is removed."""
    assert len(sim_lab["dbs"]) >= 2 and Path(dbm._db_instance.db_path) in sim_lab["dbs"]
    for path in sim_lab["dbs"]:
        before = digest(path)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE explorer_a0_probe (x TEXT)")
            conn.execute("INSERT INTO explorer_a0_probe VALUES ('a')")
            conn.commit()
            one = digest(path)
            conn.execute("UPDATE explorer_a0_probe SET x = 'b'")
            conn.commit()
            two = digest(path)
        finally:
            conn.execute("DROP TABLE IF EXISTS explorer_a0_probe")
            conn.commit()
            conn.close()
        assert one != before and two != one and one["explorer_a0_probe"][0] == two["explorer_a0_probe"][0] == 1, path
        assert digest(path) == before, path


def test_C04_positive_control_the_database_tripwire_sees_an_adapter_that_opens_one(mp):
    class Leaky(SimulatedMarket):
        def candles(self, symbol, limit=240):
            sqlite3.connect(":memory:").close()
            return super().candles(symbol, limit)

    mp.setenv(explorer.FLAG, "true")
    with database_tripwire() as opened:
        sqlite3.connect(":memory:").close()                                                    # not from inside an explorer call: not listed
        assert opened == []
        assert XS.ExplorerService(market=Leaky(seed=3, start_time=T0)).symbol(IN_LIST)["symbol"] == IN_LIST
    assert opened == [":memory:"]
    # ... and through the route, as C01 calls it: the synchronous route runs in a worker thread of the application
    mp.setitem(XS._SERVICE, "instance", XS.ExplorerService(market=Leaky(seed=4, start_time=T0)))
    xapp = FastAPI()
    xapp.include_router(RX.router)
    with database_tripwire() as via_route:
        assert TestClient(xapp).get(f"/api/explorer/symbols/{OUTSIDE}").status_code == 200
    assert via_route == [":memory:"]
    assert sqlite3.connect.__module__ != __name__                                              # the real function is restored


def test_C05_the_guard_names_each_change_and_puts_it_back(tmp_path, mp):
    """The autouse guard's restore step, on stand-ins (a temp watchlist file, probe attributes, probe variables): each kind of change is named,
    never shown, and put back - a changed file and a created file; a variable added, changed or deleted; an attribute added, deleted or rebound
    (a plain value and a list); a list and a dict changed in place. No assertion here mentions os.environ itself: a failing assertion prints the
    repr of what it mentions, and that repr would show the environment."""
    existing, missing = tmp_path / "watchlist.txt", tmp_path / "created.txt"
    existing.write_bytes(b"NVDA\nAMD\n")
    mp.setattr(config, "WATCHLIST_FILE", existing)
    mp.setattr(config, "EXPLORER_PROBE_LIST", [1, 2], raising=False)
    mp.setattr(config, "EXPLORER_PROBE_DICT", {"a": 1}, raising=False)
    mp.setattr(config, "EXPLORER_PROBE_INT", 5, raising=False)
    mp.setattr(config, "EXPLORER_PROBE_DEL", "kept", raising=False)
    mp.setenv("EXPLORER_PROBE_OLD", "before")
    mp.setenv("EXPLORER_PROBE_GONE", "present")
    plist, pdict = config.EXPLORER_PROBE_LIST, config.EXPLORER_PROBE_DICT
    saved = _Saved()
    existing.write_bytes(b"NVDA\nAMD\nMSFT\n")
    os.environ["EXPLORER_PROBE_ENV"] = "not-to-be-shown"                                        # added
    os.environ["EXPLORER_PROBE_OLD"] = "after"                                                 # changed
    del os.environ["EXPLORER_PROBE_GONE"]                                                      # deleted
    plist.append(3)
    pdict["b"] = 2
    config.EXPLORER_PROBE_NEW = 1                                                              # added
    config.EXPLORER_PROBE_INT = 6                                                              # a plain value rebound
    delattr(config, "EXPLORER_PROBE_DEL")                                                      # deleted
    changed = saved.restore()
    assert changed == ["watchlist.txt", "environment EXPLORER_PROBE_ENV", "environment EXPLORER_PROBE_GONE", "environment EXPLORER_PROBE_OLD",
                       "config.EXPLORER_PROBE_DEL", "config.EXPLORER_PROBE_DICT", "config.EXPLORER_PROBE_INT", "config.EXPLORER_PROBE_LIST",
                       "config.EXPLORER_PROBE_NEW"], changed
    assert "not-to-be-shown" not in repr(changed) + repr(saved) + repr(saved.env) + repr(saved.cfg)                # names only, never a value
    probe_env = [os.environ.get(k) for k in ("EXPLORER_PROBE_ENV", "EXPLORER_PROBE_OLD", "EXPLORER_PROBE_GONE")]
    assert probe_env == [None, "before", "present"]
    assert existing.read_bytes() == b"NVDA\nAMD\n" and not hasattr(config, "EXPLORER_PROBE_NEW")
    assert config.EXPLORER_PROBE_INT == 5 and config.EXPLORER_PROBE_DEL == "kept"
    assert config.EXPLORER_PROBE_LIST is plist and plist == [1, 2] and config.EXPLORER_PROBE_DICT is pdict and pdict == {"a": 1}
    plist.append(4)                                                                            # the saved copy is not shared with the live value
    assert saved.cfg_contents["EXPLORER_PROBE_LIST"] == [1, 2] and saved.restore() == ["config.EXPLORER_PROBE_LIST"] and plist == [1, 2]
    mp.setattr(config, "WATCHLIST_FILE", missing)
    saved = _Saved()
    missing.write_bytes(b"MSFT\n")                                                            # a file the test created
    config.EXPLORER_PROBE_LIST = [9]                                                           # a list rebound
    assert saved.restore() == ["watchlist.txt", "config.EXPLORER_PROBE_LIST"]
    assert not missing.exists() and config.EXPLORER_PROBE_LIST is plist and saved.restore() == []


# ---------------------------------------------------------------- D. the dependency boundary ----------------------------------------------

EXPLORER_FILES = ["explorer/__init__.py", "explorer/service.py", "explorer/routes.py"]
# the only top-level project packages the explorer may reach at all; EVERY other project package is forbidden (strategy, strategy_validation,
# execution, safety, autorun, paper, database, scanner, services, agents, alerts, notifications, portfolio, the rotation packages, ...)
ALLOWED_TOP = {"api", "config", "data", "demand", "evidence", "explorer", "ppe", "proposals", "setups", "trader_view"}
# of the strategy packages, only the package itself and its pydantic view models (which trader_view.models imports)
MODELS_ONLY = ("setups", "proposals")
# inside the allowed packages, the modules the explorer must never reach, directly or through anything it imports (prefix match on dotted names)
FORBIDDEN = ("api.server", "api.routes", "api.local_gate", "trader_view.stream", "trader_view.hub", "trader_view.positions", "trader_view.research",
             "trader_view.ppe_provider", "trader_view.demand_provider", "ppe.replay", "ppe.engine", "ppe.evidence_context", "demand.engine", "demand.replay",
             "evidence.store", "evidence.engine", "evidence.net", "data.news", "api.cache")
# the exact project modules the explorer can reach today (a new dependency must be added here on purpose, in review)
CLOSURE = {"api", "api.validation", "config", "data", "data.market_data", "demand", "demand.models", "evidence", "explorer", "explorer.routes", "explorer.service",
           "ppe", "ppe.inputs", "ppe.models", "proposals", "proposals.models", "setups", "setups.models", "trader_view", "trader_view.adapters", "trader_view.market_real",
           "trader_view.models", "trader_view.simulated"}
# of these, only pure model / constant modules belong to the strategy packages: setups, setups.models, proposals, proposals.models (pydantic views that
# trader_view.models imports); no service, store, engine or scheduler of any strategy, execution or safety package is reachable
# modules outside the project that no file of the closure may import: a trading or broker client, a database, a network or process module, a dynamic import
DENIED_OUTSIDE = ("alpaca.trading", "alpaca.broker", "sqlite3", "sqlalchemy", "requests", "httpx", "aiohttp", "urllib", "http", "socket", "websockets", "smtplib",
                  "subprocess", "importlib", "ctypes", "shutil")
# every top-level module from outside the project that the closure imports today (third party: alpaca, dotenv, fastapi, pandas, pydantic)
OUTSIDE_TOPS = {"__future__", "alpaca", "collections", "dataclasses", "datetime", "dotenv", "fastapi", "hashlib", "json", "logging", "math", "os", "pandas",
                "pathlib", "pydantic", "random", "re", "threading", "time", "typing", "zoneinfo"}


def is_forbidden(module: str) -> bool:
    top = module.split(".")[0]
    if top not in ALLOWED_TOP:
        return True
    if top in MODELS_ONLY:
        return module not in (top, top + ".models")
    return any(module == f or module.startswith(f + ".") for f in FORBIDDEN)


def _module_file(name: str):
    p = ROOT.joinpath(*name.split("."))
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    return p.with_suffix(".py") if p.with_suffix(".py").exists() else None


def _imported_names(path: Path, name: str) -> set:
    """Every dotted name a file imports, anywhere in it (function-level imports included)."""
    out = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = name if path.name == "__init__.py" else name.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".") if pkg else []
                base = ".".join(parts[:len(parts) - node.level + 1] + ([node.module] if node.module else []))
            out |= {base} | {f"{base}.{a.name}" for a in node.names}
    return out


def _imports(path: Path, name: str) -> set:
    """Every project module a file imports, with the packages on the way."""
    out = set()
    for n in _imported_names(path, name):
        parts = n.split(".")
        for i in range(1, len(parts) + 1):
            cand = ".".join(parts[:i])
            if _module_file(cand) is not None:
                out.add(cand)
    return out


def closure(start=("explorer", "explorer.service", "explorer.routes")) -> set:
    seen, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        f = _module_file(name)
        assert f is not None, name
        todo += sorted(_imports(f, name) - seen)
    return seen


def test_D01_the_import_closure_is_the_pinned_set_and_holds_no_forbidden_module():
    got = closure()
    bad = sorted(m for m in got if is_forbidden(m))
    assert bad == [], bad
    assert got == CLOSURE, {"new": sorted(got - CLOSURE), "gone": sorted(CLOSURE - got)}
    assert {m.split(".")[0] for m in CLOSURE} == ALLOWED_TOP and not any(is_forbidden(m) for m in CLOSURE)
    # the rule itself: every service, store, engine and scheduler of the strategy, execution and safety code is forbidden, named or not
    for m in ("setups.service", "setups.store", "setups.engine", "setups.scheduler", "setups.market_hours", "proposals.service", "proposals.store", "proposals.engine",
              "proposals.account", "execution", "execution.service", "safety.service", "autorun.service", "paper", "account_refresh", "cash_flow", "forward_validation",
              "forward_capture", "forward", "scanner.watchlist", "database.database", "evidence_refresh", "decision_flow", "strategy_registry", "dynamic_universe",
              "strategy", "strategy_validation", "services", "agents", "alerts", "notifications", "portfolio", "unified_portfolio", "market_scan", "market_intel",
              "swing_pullback", "fit", "rotation", "rotation_backtest", "trader_view.stream", "trader_view.hub", "api.server", "api.routes.trader_view", "api.local_gate",
              "ppe.replay", "main", "robinhood_sync"):
        assert is_forbidden(m), m
    # the guard itself works: the Trader View route file (which builds the stream) does reach the forbidden services
    reached = closure(("api.routes.trader_view",))
    assert {"trader_view.stream", "setups.service", "proposals.service", "execution.service", "safety.service"} <= reached
    assert sum(1 for m in reached if is_forbidden(m)) > 20


def test_D02_explorer_source_has_no_write_path_and_never_names_the_stream():
    for rel in EXPLORER_FILES:
        t = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(t)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for word in ("stream", "build_stream", "TerminalStream", "hub", "subscribe", "evaluate_symbol", "evaluate_all", "evaluate", "enable_auto", "post", "put",
                     "patch", "delete", "websocket", "WebSocket", "sqlite3", "connect", "open", "write_text", "write_bytes", "load_watchlist", "add_symbol",
                     "remove_symbol", "requests", "urlopen", "subprocess", "import_module", "__import__", "eval", "exec", "environ", "putenv", "setattr"):
            assert word not in names, (rel, word)
        assert t.isascii(), rel


def test_D03_routes_are_get_only_and_under_one_prefix(sim_lab):
    paths = sim_lab["api"].app.openapi()["paths"]
    mine = {p: sorted(v) for p, v in paths.items() if p.startswith("/api/explorer")}
    assert mine == {"/api/explorer/status": ["get"], "/api/explorer/symbols/{symbol}": ["get"], "/api/explorer/search": ["get"]}
    assert [r.path for r in RX.router.routes] == ["/api/explorer/status", "/api/explorer/symbols/{symbol}", "/api/explorer/search"]
    assert RX.router.prefix == "/api/explorer"
    assert all(set(r.methods) == {"GET"} for r in RX.router.routes)
    assert not any("trade" in p for p in mine)                                                 # tests/test_regression_isolation.py: no route path names trading


def test_D04_phase_a0_mounts_nothing_in_the_server_and_no_existing_file_names_the_explorer(sim_lab):
    server = sim_lab["server"]
    assert not [p for p in server.app.openapi()["paths"] if "explorer" in p]                  # the running application has no explorer route
    for path in ("/api/explorer/status", "/api/explorer/symbols/NVDA", "/api/explorer/search?q=NVDA"):
        assert server.get(path).status_code == 404, path
    for rel in ("api/server.py", "config.py", "trader_view/stream.py", "api/routes/trader_view.py", "scripts/run_stock_agent.py", "frontend/index.html"):
        assert "explorer" not in (ROOT / rel).read_text(encoding="utf-8").lower(), rel         # wiring is a later, separately reviewed step
    users = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("explorer/", "tests/", ".venv/", "scratchpad/", "runtime/")) or "/site-packages/" in rel:
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if "import explorer" in src or "from explorer" in src:
            users.append(rel)
    assert users == []                                                                        # nothing in the live path imports the package


def test_D05_no_file_of_the_closure_imports_a_trading_client_a_database_or_a_network_module():
    """The closure walk resolves project modules only. This reads the same files for what they import from OUTSIDE the project."""
    outside = {}
    for name in sorted(CLOSURE):
        for n in _imported_names(_module_file(name), name):
            if _module_file(n.split(".")[0]) is None:
                outside.setdefault(n, set()).add(name)
    denied = sorted((n, sorted(by)) for n, by in outside.items() if any(n == d or n.startswith(d + ".") for d in DENIED_OUTSIDE))
    assert denied == [], denied
    alpaca = sorted(n for n in outside if n.split(".")[0] == "alpaca")
    assert alpaca and all(n == "alpaca.common.exceptions" or n.startswith(("alpaca.data.", "alpaca.common.exceptions.")) for n in alpaca), alpaca   # the market-data client and its error type, nothing else
    assert {n.split(".")[0] for n in outside} == OUTSIDE_TOPS, sorted({n.split(".")[0] for n in outside} ^ OUTSIDE_TOPS)
    assert {by for n in alpaca for by in outside[n]} == {"data.market_data", "trader_view.market_real"}
    assert get_data_client.__module__ == "data.market_data" and "StockHistoricalDataClient" in (ROOT / "data" / "market_data.py").read_text(encoding="utf-8")
    # the rule itself: the same reading finds the trading client in the paper-account reader and sqlite3 in the setup store
    def denied_in(rel, name):
        return {d for d in DENIED_OUTSIDE for n in _imported_names(ROOT / rel, name) if n == d or n.startswith(d + ".")}

    assert "alpaca.trading" in denied_in("paper/alpaca_readonly.py", "paper.alpaca_readonly") and "sqlite3" in denied_in("setups/store.py", "setups.store")


# ---------------------------------------------------------------- E. Trader View is untouched ---------------------------------------------

def test_E01_an_explorer_read_never_touches_the_trader_view_stream(lab, mp):
    mp.setenv(explorer.FLAG, "true")
    s, api = lab["stream"], lab["api"]
    touched = []
    for name in ("resolve", "state", "candles", "_evaluate_setup", "_evaluate_proposal", "_subscribe"):
        orig = getattr(s, name)
        mp.setattr(s, name, lambda *a, _n=name, _o=orig, **k: touched.append(_n) or _o(*a, **k))
    theirs = spy(mp, s.market)
    mine = spy(mp, lab["explorer"].market)
    for symbol in (IN_LIST, OUTSIDE):
        assert api.get(f"/api/explorer/symbols/{symbol}").status_code == 200 and api.get(f"/api/explorer/search?q={symbol}").status_code == 200
    assert touched == [] and theirs == [] and len(s.connections) == 0
    assert {"resolve", "quote", "candles"} <= set(mine)                                        # the reads went to the explorer's own adapter
    assert lab["explorer"].market is not s.market and type(lab["explorer"].market).mode == lab["mode"]
    assert XS.service() is lab["explorer"]


def test_E02_the_default_construction_builds_its_own_adapter_with_the_same_properties(sim_lab, mp):
    """No injected adapter: service() builds the ExplorerService, which builds its adapter through build_market_adapter() on first use."""
    mp.setenv(explorer.FLAG, "true")
    mp.setitem(XS._SERVICE, "instance", None)
    mp.setattr(config, "TRADER_DATA_MODE", "simulated")
    api, s = sim_lab["api"], sim_lab["stream"]
    theirs = spy(mp, s.market)
    before = snapshot(sim_lab)
    with database_tripwire() as opened:
        for symbol in (IN_LIST, OUTSIDE):
            d = api.get(f"/api/explorer/symbols/{symbol}?candles=20")
            assert d.status_code == 200 and d.json()["mode"] == "simulated" and len(d.json()["candles"]) == 20 and d.json()["symbol"] == symbol
            assert api.get(f"/api/explorer/search?q={symbol}").json()["results"] == [{"symbol": symbol, "data_available": True}]
    built = XS.service()
    assert built is not sim_lab["explorer"] and XS.service() is built and XS._SERVICE["instance"] is built
    assert type(built.market) is SimulatedMarket and built.market is not s.market and built.market is built.market
    assert opened == [] and theirs == [] and snapshot(sim_lab) == before
    assert s.setups.store.evaluations(IN_LIST) == [] and s.setups.store.evaluations(OUTSIDE) == [] and s.setups.symbols_in_scope() == WATCHLIST


@pytest.mark.parametrize("mode", ["real", "auto"])
def test_E03_with_market_data_credentials_the_default_adapter_is_the_read_only_market_data_client(mp, mode):
    """The live configuration (mode auto + credentials, or mode real): the explorer's default adapter is RealMarketData on the market-data client
    factory - built without a request, and never the stream's adapter. (Nothing is read here: no network, no credentials.)"""
    mp.setattr(config, "TRADER_DATA_MODE", mode)
    mp.setattr(config, "has_credentials", lambda: True)
    svc = XS.ExplorerService()
    m = svc.market
    assert type(m) is RealMarketData and m.mode == "real" and "read only" in m.source and svc.market is m
    assert m._factory is get_data_client and m._client is None and m.requests == 0 and m._quotes == {} and m._bars == {}
