"""
Stage 6.9 — ROBINHOOD READ-ONLY PORTFOLIO + UNIFIED EXPOSURE. Synthetic data only (tests/pf_fixtures.py, the gateway-shaped dummy holdings);
no network, no real Robinhood login, no real account. The real executor stack runs on the deterministic simulated Alpaca PAPER broker.
  A Robinhood source states · U unified state and arithmetic · O ownership · W write-path isolation · X ownership / exits on the real executor ·
  P the advisory duplicate-entry policy · R GET-only routes · V Trader View · S credentials · F Stage 6.9 freeze
"""
import ast
import json
import re
import subprocess
import sys
import types
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
import pf_fixtures as PF  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from execution import account_positions as AP  # noqa: E402
from portfolio.provider import RobinhoodGatewayPortfolioProvider  # noqa: E402
from trader_view import positions as TP  # noqa: E402
from unified_portfolio import robinhood as RH  # noqa: E402
from unified_portfolio import state as US  # noqa: E402
from unified_portfolio.robinhood import RobinhoodPortfolioAdapter, RobinhoodSource  # noqa: E402
from test_execution_60 import arm, below_invalidation, cyc, execution_config, make_stack, no_execution_wire, stack  # noqa: E402,F401

BASE_69 = "e85d3fab1c565a3e81835a7a8999413e60cf16c5"
SECRET = "S" * 43
WRITE_NAME = re.compile(r"(?i)buy|sell|cancel|replace|submit|close|place|exercise|order|post|put|patch|delete|transfer|withdraw|deposit")


class SpyGatewayHttp(PF.FakeGatewayHttp):
    """The fake loopback gateway with every write-shaped HTTP verb present, so a write attempt would be recorded instead of crashing."""
    def __init__(self, overrides=None):
        super().__init__(overrides)
        self.writes = []

    def _write(self, verb):
        def call(url, *a, **k):
            self.writes.append((verb, url))
            raise AssertionError(f"Robinhood write attempted: {verb} {url}")
        return call

    def __getattr__(self, name):
        if name in ("post", "put", "patch", "delete", "request"):
            return self._write(name.upper())
        raise AttributeError(name)


def provider(http):
    return RobinhoodGatewayPortfolioProvider("http://127.0.0.1:8787", "holdings", secret_loader=lambda: SECRET, http=http)


def source(http=None, *, enabled=True, now=PF.NOW, **kw):
    http = http if http is not None else SpyGatewayHttp()
    clock = SimpleNamespace(now=now)
    src = RobinhoodSource(enabled_fn=lambda: enabled, provider_fn=lambda: provider(http), now_fn=lambda: clock.now, sector_fn=kw.pop("sector_fn", lambda s: None), **kw)
    return src, http, clock


def alpaca(rows=(), fresh=True, state="LOADED"):
    pos = [{"symbol": s, "qty": q, "avg_entry_price": a, "current_price": c, "market_value": mv, "unrealized_pl": None, "management": m,
            "managed": m in ("BOT", "BOT_ENTRY"), "management_text": "t"} for s, q, a, c, mv, m in rows]
    return {"state": state, "source": "ALPACA_PAPER", "refreshed_at": "2026-09-28T00:04:00+00:00", "message": "m",
            "freshness": {"status": "FRESH" if fresh else "STALE", "label": "Account data: Fresh" if fresh else "Account data: Stale", "age_seconds": 30},
            "positions": pos if state == "LOADED" else None}


def rh_view(rows, state="LOADED"):
    return {"state": state, "source": "ROBINHOOD", "label": "Robinhood · REAL · user managed", "account_label": "Robinhood · REAL · user managed · holdings ••••3450",
            "refreshed_at": "2026-09-28T00:04:59+00:00", "age_s": 1, "equity": "5000.00", "cash": "100.00", "message": "m",
            "positions": None if state not in ("LOADED", "STALE") else [{"symbol": s, "quantity": q, "average_entry_price": None, "current_price": None,
                                                                      "market_value": mv, "unrealized_pl": None, "ownership": "USER_MANAGED", "managed": False} for s, q, mv in rows]}


# ================================================================ A Robinhood source states ===================================================

def test_A01_not_configured_reads_nothing_and_is_never_zero_holdings():
    src, http, _ = source(enabled=False)
    v = src.view(refresh=True)
    assert v["state"] == "NOT_CONFIGURED" and v["positions"] is None and "unknown, not zero" in v["message"] and http.requests == [] and src.reads == 0
    assert config.ROBINHOOD_PORTFOLIO_ENABLED is False                                             # off by default


def test_A02_auth_required_secret_missing_and_gateway_down_are_states_never_empty_holdings(monkeypatch):
    for override, expect in (((200, {"status": "REAUTH_REQUIRED", "message": "login"}), "AUTH_REQUIRED"), ((401, {"status": "REJECTED"}), "AUTH_REQUIRED"),
                             ((200, {"status": "UPSTREAM_ERROR", "message": "x"}), "ERROR"), ((200, {"status": "DATA_UNAVAILABLE"}), "ERROR")):
        src, http, _ = source(SpyGatewayHttp({"/portfolio": override}))
        v = src.view(refresh=True)
        assert v["state"] == expect and v["positions"] is None, (override, v)
    from portfolio.gateway_secret import GatewaySecretUnavailable

    def no_secret():
        raise GatewaySecretUnavailable("not set up")
    src = RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: RobinhoodGatewayPortfolioProvider("http://127.0.0.1:8787", "holdings", secret_loader=no_secret, http=SpyGatewayHttp()),
                          now_fn=lambda: PF.NOW, sector_fn=lambda s: None)
    v = src.view(refresh=True)
    assert v["state"] == "NOT_CONFIGURED" and v["reason"] == "GATEWAY_SECRET_UNAVAILABLE" and v["positions"] is None

    class Down(SpyGatewayHttp):
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    src, _, _ = source(Down())
    v = src.view(refresh=True)
    assert v["state"] == "ERROR" and v["reason"] == "GATEWAY_DOWN" and v["positions"] is None and "unknown, not zero" in v["message"]


def test_A03_loaded_holdings_are_user_managed_with_the_gateways_values():
    src, http, _ = source()
    v = src.view(refresh=True)
    rows = {p["symbol"]: p for p in v["positions"]}
    assert v["state"] == "LOADED" and v["source"] == "ROBINHOOD" and v["write_capable"] is False and [r["path"] for r in http.requests] == ["/portfolio", "/positions"]
    assert {s: (r["quantity"], r["average_entry_price"], r["market_value"], r["unrealized_pl"]) for s, r in rows.items()} == {
        "NVDA": ("2", "100.000000", "222.00", "22.00"), "SNDK": ("1", "500.000000", "450.00", "-50.00"), "SHOP": ("3", "100.000000", "306.00", "6.00"), "MU": ("1", None, "12.00", None)}
    assert all(r["ownership"] == "USER_MANAGED" and r["managed"] is False for r in rows.values())
    assert v["account_label"] == "Robinhood · REAL · user managed · holdings ••••3450" and v["equity"] == "995.00" and v["cash"] == "10.00"


def test_A04_stale_states_keep_the_last_known_snapshot():
    src, http, clock = source(SpyGatewayHttp({"/positions": (200, PF.env(PF.POSITIONS, status="STALE"))}))
    assert src.view(refresh=True)["state"] == "STALE"                                                # the gateway itself marked it stale
    src, http, clock = source()
    assert src.view(refresh=True)["state"] == "LOADED"
    clock.now = PF.NOW + timedelta(seconds=src.stale_after_s + 60)
    old = src.view(refresh=False)
    assert old["state"] == "STALE" and len(old["positions"]) == 4 and "last read" in old["message"]   # too old
    http.overrides = {"/portfolio": (200, {"status": "UPSTREAM_ERROR", "message": "down"})}
    v = src.view(refresh=True)
    assert v["state"] == "STALE" and v["reason"] == "UPSTREAM_ERROR" and len(v["positions"]) == 4 and "last known snapshot" in v["message"]


def test_A05_not_loaded_until_read_and_reads_are_throttled():
    src, http, clock = source()
    assert src.view(refresh=False)["state"] == "NOT_LOADED" and http.requests == []                   # a cached read never calls the gateway
    src.view(refresh=True)
    src.view(refresh=True)
    assert src.reads == 1 and len(http.requests) == 2                                                 # within the TTL: one read (two GETs)
    clock.now = PF.NOW + timedelta(seconds=src.ttl_s + 1)
    src.view(refresh=True)
    assert src.reads == 2



def test_A06_incomplete_or_truncated_data_is_a_failed_read_never_zero_holdings():
    for override in ({"/positions": (200, {"status": "OK", "data": None, "fetched_at": "2026-09-28T00:04:59+00:00"})},
                     {"/positions": (200, {"status": "OK", "data": {"account": {}}, "fetched_at": "2026-09-28T00:04:59+00:00"})},
                     {"/portfolio": (200, {"status": "OK", "data": None})},
                     {"/positions": (200, {**PF.env({"positions": PF.POSITIONS["positions"][:1], "quotes": PF.POSITIONS["quotes"]}), "truncated": True})}):
        src, http, _ = source(SpyGatewayHttp(override))
        v = src.view(refresh=True)
        assert v["state"] == "ERROR" and v["reason"] == "DATA_INCOMPLETE" and v["positions"] is None, (override, v)
        assert US.duplicate_entry_policy("NVDA", US.robinhood_source(v)["positions"] or [], v["state"])["would_block"] is None   # unknown, never "no holding"
    src, http, clock = source()
    src.view(refresh=True)
    http.overrides = {"/positions": (200, {**PF.env(PF.POSITIONS), "truncated": True})}
    src.ttl_s = 0
    v = src.view(refresh=True)
    assert v["state"] == "STALE" and v["reason"] == "DATA_INCOMPLETE" and len(v["positions"]) == 4      # the last complete snapshot is kept


def test_A07_one_read_per_ttl_even_under_concurrency_and_provider_errors_are_recorded():
    import threading
    src, http, _ = source()
    gate, started = threading.Event(), threading.Event()
    real_read = RobinhoodPortfolioAdapter.read

    def slow_read(self):
        started.set()
        gate.wait(5)
        return real_read(self)
    RobinhoodPortfolioAdapter.read = slow_read
    try:
        src.lock_wait_s = 0.2
        t1 = threading.Thread(target=lambda: src.view(refresh=True))
        t1.start()
        started.wait(5)
        busy = src.view(refresh=True)                                                                 # a second caller does not queue behind the read
        assert busy["state"] == "NOT_LOADED" and src.reads == 1
        gate.set()
        t1.join(5)
    finally:
        RobinhoodPortfolioAdapter.read = real_read
    assert src.refresh(only_if_due=True) == {"result": "SKIPPED_FRESH"} and src.reads == 1                # re-checked under the lock
    def boom():
        raise RuntimeError("bad provider")
    bad = RobinhoodSource(enabled_fn=lambda: True, provider_fn=boom, now_fn=lambda: PF.NOW, sector_fn=lambda s: None)
    v = bad.view(refresh=True)
    assert v["state"] == "ERROR" and v["reason"] == "PROVIDER_ERROR" and not bad.due()                  # recorded, throttled, never an exception


def test_A08_symbols_are_upper_cased_and_the_fixture_is_synthetic(tmp_path):
    lower = {"positions": [dict(PF.POSITIONS["positions"][0], symbol="nvda")], "quotes": {"nvda": dict(PF.POSITIONS["quotes"]["NVDA"], symbol="nvda")}}
    src, _, _ = source(SpyGatewayHttp({"/positions": (200, PF.env(lower))}))
    v = src.view(refresh=True)
    assert [p["symbol"] for p in v["positions"]] == ["NVDA"]
    u = US.build(alpaca([("NVDA", "2", "100", "111", "222.00", "BOT")]), v)
    assert {s["symbol"]: s["duplicate"] for s in u["exposure"]["by_symbol"]} == {"NVDA": True}
    import json as _json
    from unified_portfolio.fixture import FixturePortfolioProvider
    fx = tmp_path / "fx.json"
    fx.write_text(_json.dumps({"portfolio": PF.PORTFOLIO, "positions": PF.POSITIONS}), encoding="utf-8")
    fsrc = RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: FixturePortfolioProvider(str(fx)), now_fn=lambda: PF.NOW, sector_fn=lambda s: None)
    fv = fsrc.view(refresh=True)
    assert fv["source"] == "ROBINHOOD_FIXTURE" and fv["account_type"] == "SYNTHETIC" and "SYNTHETIC" in fv["label"]
    fu = US.build(alpaca([]), fv)
    assert {r["account_type"] for r in fu["rows"]} == {"SYNTHETIC"} and "REAL" not in fu["exposure"]["by_account_type"]
    tv = TP.StockAgentPositionAdapter(robinhood_snapshot=lambda: fv, alpaca_status=lambda: {"connection": {"state": "NOT_CONFIGURED"}}, local_view=lambda: {},
                                      awareness_enabled=lambda: False, cache_seconds=0).position("NVDA").sources[2]
    assert "SYNTHETIC" in tv.label and "SYNTHETIC fixture" in tv.message and "REAL account" not in tv.message


# ================================================================ U unified state ================================================================

def test_U01_the_same_symbol_in_two_accounts_aggregates_exposure_but_keeps_ownership_per_row():
    src, _, _ = source()
    u = US.build(alpaca([("NVDA", "2", "100", "111", "222.00", "BOT")]), src.view(refresh=True))
    nvda = [r for r in u["rows"] if r["symbol"] == "NVDA"]
    assert sorted((r["source"], r["ownership"], r["quantity"]) for r in nvda) == [("ALPACA_PAPER", "BOT_MANAGED", "2"), ("ROBINHOOD", "USER_MANAGED", "2")]
    e = {s["symbol"]: s for s in u["exposure"]["by_symbol"]}["NVDA"]
    assert e["quantity"] == "4" and e["market_value"] == "444.00" and e["duplicate"] is True and sorted(e["ownerships"]) == ["BOT_MANAGED", "USER_MANAGED"]
    assert any(w["code"] == "DUPLICATE_SYMBOL" and w["symbol"] == "NVDA" for w in u["warnings"]) and u["state"] == "COMPLETE"


def test_U02_the_requested_example_500_bot_plus_4000_user_is_4500_exposure_split_by_ownership():
    u = US.build(alpaca([("NVDA", "2", "250", "250", "500.00", "BOT")]), rh_view([("NVDA", "10", "4000.00")]))
    e = u["exposure"]
    assert {s["symbol"]: (s["quantity"], s["market_value"]) for s in e["by_symbol"]} == {"NVDA": ("12", "4500.00")}
    assert e["by_ownership"] == {"BOT_MANAGED": "500.00", "USER_MANAGED": "4000.00"} and e["by_account_type"] == {"PAPER": "500.00", "REAL": "4000.00"}
    assert e["total_market_value"] == "4500.00" and e["largest"] == {"symbol": "NVDA", "weight_pct": "100.00"}


def test_U03_robinhood_missing_is_partial_never_zero_holdings():
    for rv in (None, rh_view([], state="NOT_CONFIGURED"), rh_view([], state="AUTH_REQUIRED"), rh_view([], state="ERROR"), rh_view([], state="NOT_LOADED")):
        u = US.build(alpaca([("F", "1", "12.1", "11.58", "11.58", "EXTERNAL")]), rv)
        assert u["state"] == "PARTIAL" and u["exposure"]["basis"] == "KNOWN_SOURCES_ONLY" and u["exposure"]["missing_sources"] == ["ROBINHOOD"]
        assert not [r for r in u["rows"] if r["account_type"] == "REAL"] and u["equity"]["real_external"] is None
        assert any("unknown, not zero" in d for d in u["disclosure"]) and any(w["code"] == "SOURCE_MISSING" for w in u["warnings"])
    empty = US.build(alpaca([]), rh_view([]))                                                      # LOADED and truly empty is a different thing
    assert empty["state"] == "COMPLETE" and empty["exposure"]["total_market_value"] == "0.00" and empty["equity"]["real_external"]["equity"] == "5000.00"
    assert US.build(alpaca(state="NOT_LOADED"), None)["state"] == "UNAVAILABLE"


def test_U04_a_failed_robinhood_read_after_alpaca_succeeds_is_stale_source_with_the_last_snapshot():
    src, http, _ = source()
    src.view(refresh=True)
    http.overrides = {"/positions": (200, {"status": "UPSTREAM_ERROR"})}
    src.ttl_s = 0
    u = US.build(alpaca([("F", "1", "12.1", "11.58", "11.58", "EXTERNAL")]), src.view(refresh=True))
    assert u["state"] == "STALE_SOURCE" and u["exposure"]["stale_sources"] == ["ROBINHOOD"] and len([r for r in u["rows"] if r["source"] == "ROBINHOOD"]) == 4
    assert any(w["code"] == "SOURCE_STALE" for w in u["warnings"]) and u["state"] != "EMPTY"


def test_U05_metrics_concentration_top5_sector_and_unvalued_rows():
    rv = rh_view([("A", "1", "500.00"), ("B", "1", "200.00"), ("C", "1", "100.00"), ("D", "1", "100.00"), ("E", "1", "60.00"), ("G", "1", "40.00"), ("H", "1", None)])
    u = US.build(alpaca([]), rv, sector_fn=lambda s: {"A": "Technology", "B": "Technology"}.get(s), warn_pct=20)
    e = u["exposure"]
    assert e["total_market_value"] == "1000.00" and [(s["symbol"], s["weight_pct"]) for s in e["by_symbol"][:2]] == [("A", "50.00"), ("B", "20.00")]
    assert e["top5_pct"] == "96.00" and e["largest"] == {"symbol": "A", "weight_pct": "50.00"}
    assert e["by_sector"] == [{"sector": "Technology", "market_value": "700.00", "weight_pct": "70.00"}] and e["unclassified_market_value"] == "300.00"
    assert e["unvalued"] == [{"source": "ROBINHOOD", "symbol": "H"}] and any(w["code"] == "UNVALUED" for w in u["warnings"])
    assert sorted(w["symbol"] for w in u["warnings"] if w["code"] == "CONCENTRATION") == ["A", "B"]


def test_U06_paper_and_real_equity_are_never_added_together():
    u = US.build(alpaca([]), rh_view([("NVDA", "1", "100.00")]), alpaca_account={"equity": "100000.00", "cash": "99000.00"})
    eq = u["equity"]
    assert eq["paper"]["equity"] == "100000.00" and eq["real_external"]["equity"] == "5000.00" and "never added together" in eq["note"]
    assert "105000" not in json.dumps(u) and set(eq) == {"paper", "real_external", "note"}



def test_U07_a_long_and_a_short_never_cancel_into_zero_exposure():
    u = US.build(alpaca([("AAPL", "-10", "100", "100", "-1000.00", "EXTERNAL"), ("MSFT", "1", "100", "100", "100.00", "EXTERNAL")]), rh_view([("AAPL", "10", "1000.00")]))
    a = {s["symbol"]: s for s in u["exposure"]["by_symbol"]}["AAPL"]
    assert a["quantity"] == "0" and a["market_value"] == "0.00"                                       # net kept, explicitly
    assert a["gross_quantity"] == "20" and a["gross_market_value"] == "2000.00" and a["weight_pct"] == "95.24"   # exposure is gross
    assert u["exposure"]["largest"] == {"symbol": "AAPL", "weight_pct": "95.24"} and u["exposure"]["total_market_value"] == "2100.00"


def test_U08_top5_and_sector_shares_come_from_unrounded_values_never_from_rounded_strings():
    one = US.build(alpaca([("F", "1", "12.1", "11.575", "11.575", "EXTERNAL")]), rh_view([]))           # 11.575 displays as 11.58; the share is still 100 %
    assert one["exposure"]["top5_pct"] == "100.00" and one["exposure"]["largest"]["weight_pct"] == "100.00"   # (it read 100.04 when summed from "11.58")
    three = US.build(alpaca([]), rh_view([("A", "1", "0.005"), ("B", "1", "0.005"), ("C", "1", "0.005")]), sector_fn=lambda s: "Technology")
    assert three["exposure"]["top5_pct"] == "100.00" and three["exposure"]["by_sector"] == [{"sector": "Technology", "market_value": "0.02", "weight_pct": "100.00"}]
    assert [s["symbol"] for s in three["exposure"]["by_symbol"]] == ["A", "B", "C"]                    # equal values: ranked by symbol


# ================================================================ O ownership ====================================================================

def test_O01_robinhood_holdings_are_always_user_managed_whatever_the_bot_does():
    u = US.build(alpaca([("NVDA", "2", "100", "111", "222.00", "BOT"), ("SNDK", "1", "1", "1", "1.00", "BOT_ENTRY")]), rh_view([("NVDA", "10", "1110.00"), ("SNDK", "3", "3.00")]),
                 ready_symbols=["NVDA", "SNDK"])
    real = [r for r in u["rows"] if r["account_type"] == "REAL"]
    assert real and all(r["ownership"] == "USER_MANAGED" and r["managed"] is False for r in real)
    assert {r["symbol"]: r["ownership"] for r in u["rows"] if r["account_type"] == "PAPER"} == {"NVDA": "BOT_MANAGED", "SNDK": "BOT_MANAGED"}


# ================================================================ W write-path isolation ===========================================================

def test_W01_the_adapter_has_one_read_operation_and_no_write_shaped_member():
    names = [n for n in dir(RobinhoodPortfolioAdapter) if not n.startswith("__")]
    assert names == ["READS", "_provider", "read"] and RobinhoodPortfolioAdapter.READS == ("get_portfolio", "get_positions")
    assert not [n for n in names + [n for n in dir(RobinhoodSource) if not n.startswith("__")] if WRITE_NAME.search(n)]
    a = RobinhoodPortfolioAdapter(provider(SpyGatewayHttp()))
    with pytest.raises(AttributeError):
        a.submit_order = lambda *x: None                                                          # __slots__: nothing can be bolted on
    calls = []

    class Recorder:
        def __getattr__(self, name):
            calls.append(name)
            return lambda *a, **k: PF.env({})
    RobinhoodPortfolioAdapter(Recorder()).read()
    assert calls == ["get_portfolio", "get_positions"]                                            # nothing else is ever touched on the provider


def test_W02_every_read_path_makes_zero_robinhood_writes(stack, monkeypatch):
    http = SpyGatewayHttp()
    src = RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: provider(http), now_fn=lambda: PF.NOW, sector_fn=lambda s: None, ttl_s=0)
    monkeypatch.setattr(RH, "_SOURCE", src)
    fake_rs = types.ModuleType("robin_stocks")                                                   # the legacy library, spied: any order call is recorded
    fake_rs.robinhood = types.SimpleNamespace(**{f: (lambda *a, _f=f, **k: rs_calls.append(_f)) for f in (
        "order_buy_market", "order_sell_market", "order_buy_limit", "order_sell_limit", "cancel_stock_order", "cancel_all_stock_orders", "order")})
    rs_calls = []
    monkeypatch.setitem(sys.modules, "robin_stocks", fake_rs)
    monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", fake_rs.robinhood)
    for path in ("/api/unified-portfolio", "/api/unified-portfolio/robinhood", "/api/unified-portfolio", "/api/terminal/state/NVDA", "/api/execution/activity",
                 "/api/account-refresh/positions", "/api/execution/status"):
        assert stack.api.get(path).status_code == 200, path
    src.refresh()
    US.build(None, src.view(refresh=True), ready_symbols=["NVDA"])
    TP.StockAgentPositionAdapter(robinhood_snapshot=lambda: src.view(refresh=False), awareness_enabled=lambda: False, cache_seconds=0,
                                 alpaca_status=lambda: {"connection": {"state": "NOT_CONFIGURED"}}, local_view=lambda: {}).position("NVDA")
    assert http.writes == [] and rs_calls == [] and src.reads >= 3
    assert {(r["path"]) for r in http.requests} == {"/portfolio", "/positions"} and stack.sim.posts == []


def test_W03_no_execution_module_can_reach_robinhood_and_the_new_code_reaches_no_execution_client():
    def imports(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
        return out
    for d in ("execution", "safety", "proposals", "setups"):
        for f in (ROOT / d).glob("*.py"):
            bad = {m for m in imports(f) if m.startswith(("unified_portfolio", "portfolio", "rh_gateway", "robin_stocks", "robinhood_sync", "api.routes.portfolio"))}
            assert not bad, (f.name, bad)
    for f in (ROOT / "unified_portfolio").glob("*.py"):
        mods = imports(f)
        assert not {m for m in mods if m.startswith(("execution", "safety", "proposals", "robin_stocks", "robinhood_sync", "paper", "anthropic", "ai_explain", "agents"))}, (f.name, mods)
        tree = ast.parse(f.read_text(encoding="utf-8"))                                              # code only: docstrings and comments are not code
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        assert not re.search(r"\.(post|put|patch|delete|request)\(|post_order|place_|cancel_|submit|close_position", code), f.name
    route = ROOT / "api" / "routes" / "unified_portfolio.py"
    mods = imports(route)                                                                              # it imports the Stage 6.8 classifier, never an execution client
    assert "execution.account_positions" in mods and not {m for m in mods if m in ("execution.service", "execution.engine", "execution.broker", "execution.store", "execution.scheduler")}
    assert not re.search(r"post_order|enable_auto|disable_auto|set_mode|run_cycle|insert_intent|\.post\(", route.read_text(encoding="utf-8"))


def test_W04_get_only_routes_and_no_robinhood_execution_setting():
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/unified-portfolio")}
    assert mine == {"/api/unified-portfolio": {"get"}, "/api/unified-portfolio/robinhood": {"get"}}
    names = [n for n in dir(config) if "ROBINHOOD" in n or "UNIFIED" in n]
    assert not [n for n in names if re.search(r"EXECUTION|WRITE|ORDER|TRADE|SUBMIT|LIVE", n)], names
    assert config.ROBINHOOD_DUPLICATE_ENTRY_POLICY in ("ADVISORY", "OFF")


# ================================================================ X ownership and exits on the real executor =========================================

class FakeRefresher:
    def __init__(self, st):
        self.st = st
        self.store = SimpleNamespace(snapshot=lambda: {"refreshed_at": "2026-09-28T00:04:00+00:00", "source": "ALPACA_PAPER", "host": "paper-api.alpaca.markets",
                                                       "positions": [{"symbol": p["symbol"], "qty": str(p["qty"]), "avg_entry_price": p.get("avg_entry_price"),
                                                                      "market_value": p.get("market_value"), "current_price": p.get("current_price")} for p in st.x.broker.positions()]})

    def enabled(self):
        return True

    def freshness(self):
        return {"status": "FRESH", "fresh": True, "label": "Account data: Fresh", "age_seconds": 1, "refreshed_at": "2026-09-28T00:04:00+00:00"}


def test_X01_a_robinhood_holding_stays_user_managed_when_the_bot_trades_the_same_symbol(tmp_path):
    st = make_stack(tmp_path)
    rv = rh_view([("SNDK", "3", "1350.00")])
    pv = arm(st, "SNDK")                                                                            # READY + Paper Auto ON (simulated broker only)
    cyc(st)
    cyc(st)
    pos = st.x.store.open_position("SNDK")
    assert pos is not None
    u = US.build(AP.view(FakeRefresher(st), st.x.store, loop_running=True), rv, ready_symbols=["SNDK"])
    rows = {(r["source"], r["symbol"]): r for r in u["rows"]}
    assert rows[("ALPACA_PAPER", "SNDK")]["ownership"] == "BOT_MANAGED" and rows[("ROBINHOOD", "SNDK")]["ownership"] == "USER_MANAGED"
    assert rows[("ROBINHOOD", "SNDK")]["quantity"] == "3" and rows[("ROBINHOOD", "SNDK")]["managed"] is False
    sndk = {s["symbol"]: s for s in u["exposure"]["by_symbol"]}["SNDK"]
    assert Decimal(sndk["quantity"]) == pos.quantity + 3 and sndk["duplicate"]


def test_X02_the_bots_exit_touches_only_its_own_alpaca_paper_shares(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    http = SpyGatewayHttp()                                                                           # installed as THE Robinhood source: any executor read would show
    monkeypatch.setattr(RH, "_SOURCE", RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: provider(http), now_fn=lambda: PF.NOW, sector_fn=lambda s: None, ttl_s=0))
    pv = arm(st, "SNDK")
    cyc(st)
    cyc(st)
    pos = st.x.store.open_position("SNDK")
    below_invalidation(st, pv)
    for _ in range(3):
        cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED"
    sides = [json.loads(b)["side"] if isinstance(b, (str, bytes)) else b.get("side") for b in st.sim.posts]
    qtys = [str(json.loads(b)["qty"]) if isinstance(b, (str, bytes)) else str(b.get("qty")) for b in st.sim.posts]
    assert sides == ["buy", "sell"] and qtys == [str(pos.quantity), str(pos.quantity)]            # the bot's own shares, on the PAPER broker only
    assert http.requests == [] and http.writes == []                                                 # the executor never reaches Robinhood


def test_X03_no_bot_position_means_no_exit_whatever_a_robinhood_holding_does(tmp_path):
    st = make_stack(tmp_path)
    st.sim.set_price("MU", 1.0)                                                                       # far below any exit level
    for _ in range(3):
        cyc(st)
    u = US.build(AP.view(FakeRefresher(st), st.x.store, loop_running=True), rh_view([("MU", "5", "5.00")]))
    assert st.sim.posts == [] and st.x.store.positions(open_only=False) == [] and [r["ownership"] for r in u["rows"]] == ["USER_MANAGED"]


# ================================================================ P duplicate-entry policy (advisory) ===============================================

def test_P01_the_policy_is_advisory_and_the_executor_is_unchanged(tmp_path):
    rows = US.robinhood_source(rh_view([("NVDA", "10", "1.00")]))["positions"]
    assert US.duplicate_entry_policy("NVDA", rows, "LOADED")["would_block"] is True
    assert US.duplicate_entry_policy("MU", rows, "LOADED")["would_block"] is False
    assert US.duplicate_entry_policy("NVDA", [], "AUTH_REQUIRED")["would_block"] is None             # unknown, never "allowed"
    assert US.duplicate_entry_policy("NVDA", rows, "LOADED", mode="OFF")["would_block"] is False
    assert all(US.duplicate_entry_policy(s, rows, "LOADED")["enforced"] is False for s in ("NVDA", "MU"))
    st = make_stack(tmp_path)                                                                         # Robinhood holds SNDK; the bot still enters SNDK on PAPER
    arm(st, "SNDK")
    out = cyc(st)
    assert out["summary"]["entries"]["submitted"] == ["SNDK"]                                         # unchanged production behaviour (documented)
    assert "Robinhood holdings are not an executor input" in US.CURRENT_ENTRY_POLICY


# ================================================================ R routes ===========================================================================

def test_R01_the_unified_routes_read_alpaca_and_robinhood(stack, monkeypatch):
    http = SpyGatewayHttp()
    monkeypatch.setattr(RH, "_SOURCE", RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: provider(http), now_fn=lambda: PF.NOW, sector_fn=lambda s: None))
    monkeypatch.setattr(stack.s, "account_refresh", FakeRefresher(stack))
    stack.sim.set_position("F", 1, 12.1)
    u = stack.api.get("/api/unified-portfolio").json()
    assert u["state"] == "COMPLETE" and {(r["source"], r["symbol"], r["ownership"]) for r in u["rows"]} >= {("ALPACA_PAPER", "F", "EXTERNAL"), ("ROBINHOOD", "NVDA", "USER_MANAGED")}
    r = stack.api.get("/api/unified-portfolio/robinhood").json()
    assert r["state"] == "LOADED" and len(r["positions"]) == 4 and http.writes == [] and stack.sim.posts == []
    assert stack.api.post("/api/unified-portfolio", json={}).status_code == 405 and stack.api.delete("/api/unified-portfolio/robinhood").status_code == 405


# ================================================================ V Trader View =======================================================================

def test_V01_trader_view_names_the_real_account_and_never_reads_not_loaded_as_no_position():
    common = dict(alpaca_status=lambda: {"connection": {"state": "NOT_CONFIGURED"}}, local_view=lambda: {}, awareness_enabled=lambda: False, cache_seconds=0)
    src, http, _ = source()
    ad = TP.StockAgentPositionAdapter(robinhood_snapshot=lambda: src.view(refresh=False), **common)
    nl = ad.position("NVDA").sources[2]
    assert nl.status == "NOT_LOADED" and nl.label == "Robinhood · REAL · user managed" and http.requests == []
    src.refresh()
    p = ad.position("NVDA")
    rh = p.sources[2]
    assert len(p.sources) == 3 and rh.status == "POSITION" and rh.quantity == "2" and "User managed · REAL" in rh.message and "never trades" in rh.message
    assert ad.position("JPM").sources[2].status == "NO_POSITION"
    off = TP.StockAgentPositionAdapter(robinhood_snapshot=lambda: {"state": "NOT_CONFIGURED"}, **common).position("NVDA").sources[2]
    assert off.status == "DISABLED" and off.label == "Robinhood Read Only"                         # not configured: the earlier behaviour, unchanged
    src_txt = (ROOT / "trader_view" / "stream.py").read_text(encoding="utf-8")
    assert "robinhood_snapshot=lambda: robinhood_source().view(refresh=False)" in src_txt          # the poll never calls the gateway


# ================================================================ S credentials =======================================================================

def test_S01_no_credential_reaches_a_payload_or_a_log(caplog, stack, monkeypatch):
    http = SpyGatewayHttp()
    monkeypatch.setattr(RH, "_SOURCE", RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: provider(http), now_fn=lambda: PF.NOW, sector_fn=lambda s: None, ttl_s=0))
    bodies = [stack.api.get(p).text for p in ("/api/unified-portfolio", "/api/unified-portfolio/robinhood")]
    http.overrides = {"/portfolio": (401, {"status": "REJECTED"})}
    with caplog.at_level("WARNING"):
        bodies.append(stack.api.get("/api/unified-portfolio/robinhood").text)
    assert all(h["headers"]["X-RH-Gateway-Secret"] == SECRET for h in http.requests)                # the secret only travels on the loopback header
    for b in bodies + [caplog.text]:
        assert SECRET not in b and "X-RH-Gateway-Secret" not in b and not re.search(r"(?i)password|oauth|access_token|refresh_token|cookie|session_id", b)
    assert "••••3450" in bodies[1] and "PA" not in json.loads(bodies[1])["account_label"].split("·")[-1]


# ================================================================ F Stage 6.9 freeze ===================================================================

NEW_69_FILES = {"unified_portfolio/__init__.py", "unified_portfolio/fixture.py", "unified_portfolio/robinhood.py", "unified_portfolio/state.py",
                "api/routes/unified_portfolio.py", "tests/test_robinhood_portfolio_69.py", "tests/frontend/unified_portfolio.test.cjs",
                "DESIGN_69_ROBINHOOD_UNIFIED_PORTFOLIO.md"}
EDITED_69 = {"config.py", "api/server.py", "trader_view/positions.py", "trader_view/stream.py", "frontend/product_ui.js", "frontend/product_ui.css",
             "browser_tests/flows.py", ".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_strategy_validation_62.py",
             "tests/test_product_ui_611.py", "tests/test_product_flow_612.py", "tests/frontend/product_flow.test.cjs", "tests/frontend/product_ui.test.cjs",
             "tests/frontend/strategy_validation.test.cjs", "tests/test_regression_isolation.py", "tests/test_trader_view_55.py", "tests/test_execution_60.py"}
SERVER_LINES_69 = ["from api.routes import unified_portfolio  # Stage 6.9: unified portfolio (GET only) - Alpaca PAPER + Robinhood READ-ONLY holdings, exposure; no write path",
                   "app.include_router(unified_portfolio.router)  # Stage 6.9: unified portfolio (GET only) - Robinhood is read only and never an execution broker"]


def test_F01_stage_69_changes_exactly_its_files_and_no_execution_strategy_or_risk_file():
    import freeze_ledger as FL
    run = lambda *a: subprocess.run(list(a), cwd=ROOT, capture_output=True, text=True).stdout.split()  # noqa: E731
    assert subprocess.run(["git", "cat-file", "-e", f"{BASE_69}^{{commit}}"], cwd=ROOT).returncode == 0
    stage69 = set(run("git", "diff", "--name-only", BASE_69, FL.BASE_691))                      # Stage 6.9 itself, between two fixed commits (main before / after PR #32)
    assert stage69 == NEW_69_FILES | EDITED_69, {"unexpected": sorted(stage69 - NEW_69_FILES - EDITED_69), "missing": sorted((NEW_69_FILES | EDITED_69) - stage69)}
    changed = (set(run("git", "diff", "--name-only", BASE_69)) | set(run("git", "ls-files", "--others", "--exclude-standard")))
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),
    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py
                                                          "missing": sorted((NEW_69_FILES | EDITED_69 | later) - changed)}
    assert FL.NEW_69 == NEW_69_FILES and not (EDITED_69 - set(FL.LEDGER) - {"tests/freeze_ledger.py", "tests/test_bot_activity_68.py"})   # every edit is registered
    base_srv = subprocess.run(["git", "show", f"{BASE_69}:api/server.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n").splitlines()
    now_srv = subprocess.run(["git", "show", f"{FL.BASE_610A}:api/server.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n").splitlines()   # Stage 6.10A pins its own lines
    assert [l for l in base_srv if l not in now_srv] == [] and [l for l in now_srv if l not in base_srv] == SERVER_LINES_69   # api/server.py: exactly two added lines
    base_cfg = subprocess.run(["git", "show", f"{BASE_69}:config.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")
    assert (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n").startswith(base_cfg.rstrip("\n"))   # config.py: append-only
    for d in ("execution", "safety", "proposals", "setups", "forward_validation", "momentum", "value_quality", "mean_reversion", "breakout", "strategy_validation",
              "account_refresh", "cash_flow", "paper", "portfolio"):
        assert not [p for p in stage69 if p.startswith(d + "/")], d                                   # Stage 6.9 changed no execution, risk, strategy or legacy portfolio file
        assert not [p for p in changed - later if p.startswith(d + "/")], d                           # ... and nothing but Stage 6.9.1's declared files changed one since
    import execution as X
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    out = subprocess.run([sys.executable, "-c", "import forward_validation as FV; print(FV.frozen_hash())"], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530", out.stderr[-500:]   # a clean process: the real config
