"""
Stage 6.9.1 - UNIFIED EXTERNAL EXPOSURE GATE (execution/exposure_gate.py, DESIGN_691_UNIFIED_EXPOSURE_GATE.md). Synthetic data only: the
gateway-shaped dummy holdings of tests/pf_fixtures.py or plain unified-state dicts; no network, no real Robinhood login, no real account. The real
executor stack runs on the deterministic simulated Alpaca PAPER broker.
  G the pure rule (duplicate symbol, concentration, boundaries, Decimal, stale / partial / unavailable, ownership, malformed data, contract)
  E the real executor path (block, allow, exits, Monitor only, Paper Auto, Pause / Emergency Stop, reconciliation, exactly-once, failures)
  W write isolation · A API, Bot Activity and forward-validation metadata · F the Stage 6.9.1 freeze (the semantic change acknowledged, not hidden)
"""
import ast
import copy
import json
import re
import subprocess
import sys
import types
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
from execution import activity as ACT  # noqa: E402
from execution import engine as EN  # noqa: E402
from execution import exposure_gate as G  # noqa: E402
from execution.service import SimulatedCrash  # noqa: E402
from unified_portfolio import robinhood as RH  # noqa: E402
from unified_portfolio import state as US  # noqa: E402
from unified_portfolio.robinhood import RobinhoodSource  # noqa: E402
from test_execution_60 import arm, below_invalidation, boom, cyc, execution_config, go_ready, make_stack, no_execution_wire, reopen, stack  # noqa: E402,F401
from test_robinhood_portfolio_69 import SECRET, SpyGatewayHttp, alpaca, provider, rh_view  # noqa: E402
from test_setups_58 import strip_code  # noqa: E402

BASE_691 = "98c59e29e8de800226a47b1994038e902cfde0e2"                                   # main: Stage 6.9 merged (PR #32)
FROZEN_V1 = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"           # the Stage 6.1 frozen strategy controls: unchanged (historical evidence)
ON = G.GateSettings(enabled=True)
BIG = {"account": {"alias": "holdings", "masked_id": PF.MASKED}, "positions": [PF.position("AAPL", "1000.000000", "900.000000")],
       "quotes": {"AAPL": PF.quote("AAPL", "1000.00", reg_price="1000.00")}}             # $1,000,000 of AAPL: any PAPER-sized entry is far below 20 %


def ev(sym, u, s=ON, qty=1, ref="100", **kw):
    return G.evaluate(sym, qty, Decimal(ref) if ref is not None else None, u, s, **kw)


def code(e):
    return e.input["reason_code"]


def rh_rows(rows, state="LOADED"):
    return rh_view(rows, state)


class SimRefresher:
    """The account refresh as the unified portfolio reads it, from the simulated broker's state directly (0 broker calls), always FRESH."""
    def __init__(self, st):
        self.st = st
        self.store = SimpleNamespace(snapshot=lambda: {"refreshed_at": "2026-09-28T00:04:00+00:00", "source": "SIMULATED_PAPER", "host": None,
                                                       "positions": [{"symbol": s, "qty": str(p["qty"]), "avg_entry_price": str(p["avg_entry_price"]),
                                                                      "market_value": str(Decimal(p["qty"]) * p["avg_entry_price"]), "current_price": str(p["avg_entry_price"])}
                                                                     for s, p in sorted(st.sim.positions.items())]})

    def enabled(self):
        return True

    def freshness(self):
        return {"status": "FRESH", "fresh": True, "label": "Account data: Fresh", "age_seconds": 1, "refreshed_at": "2026-09-28T00:04:00+00:00"}


def gate(monkeypatch, on=True, **kw):
    monkeypatch.setattr(config, "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", on)
    for k, v in kw.items():
        monkeypatch.setattr(config, k, v)


def robinhood(monkeypatch, http=None, **kw):
    http = http if http is not None else SpyGatewayHttp()
    src = RobinhoodSource(enabled_fn=lambda: True, provider_fn=lambda: provider(http), now_fn=lambda: PF.NOW, sector_fn=lambda s: None, **kw)
    monkeypatch.setattr(RH, "_SOURCE", src)
    return src, http


def gated_stack(tmp_path, monkeypatch, http=None, **kw):
    st = make_stack(tmp_path)
    monkeypatch.setattr(st.s, "account_refresh", SimRefresher(st))
    src, http = robinhood(monkeypatch, http, **kw)
    return st, src, http


def gate_row(controls):
    return next((c for c in controls if c.control_id == G.CONTROL_ID), None)


def evaluations(out):
    return {e["symbol"]: e for e in (out["summary"].get("entries") or {}).get("evaluations") or []}


# ================================================================ G the pure rule ===============================================================

def test_G01_a_robinhood_user_managed_holding_blocks_with_the_exact_reason_and_a_complete_record():
    u = US.build(alpaca([("F", "1", "12.1", "11.58", "11.58", "EXTERNAL")]), rh_rows([("NVDA", "5.283683", "1142.97"), ("SNDK", "4.277343", "5417.34")]))
    e = ev("NVDA", u, qty=2, ref="150")
    assert u["state"] == "COMPLETE" and e.control_id == "UNIFIED_EXTERNAL_EXPOSURE" and e.result == "BLOCK" and code(e) == "EXTERNAL_SYMBOL_ALREADY_HELD"
    assert e.reason.startswith("Robinhood already holds NVDA as a user-managed real position. Blocked: 5.283683 NVDA shares already held in Robinhood REAL (user managed), $1,142.97.")
    assert "the Robinhood holding is not touched" in e.reason and "bot already owns" not in e.reason
    rec = G.record(e)["detail"]
    assert rec["symbol"] == "NVDA" and rec["reason_code"] == code(e) and rec["unified_state"] == "COMPLETE" and rec["snapshot_at"] == "2026-09-28T00:04:59+00:00"
    assert rec["external_holdings"] == [{"source": "ROBINHOOD", "account_type": "REAL", "ownership": "USER_MANAGED", "quantity": "5.283683", "market_value": "1142.97"}]
    assert rec["threshold_pct"] == "20" and rec["projected_pct"] is not None and rec["policy"] == "BLOCK_DUPLICATE_ONLY_IF_KNOWN" and rec["sources"]
    blob = json.dumps(e.model_dump())
    assert "3450" not in blob and "account_label" not in blob and "holdings ••••" not in blob                       # no account number, not even the masked label
    assert G.record(EN.ControlEvaluation(control_id="BROKER_POSITION", result="BLOCK")) == {}


def test_G02_no_external_holding_and_a_small_entry_is_allowed():
    u = US.build(alpaca([]), rh_rows([("AAPL", "10", "9000.00")]))
    e = ev("MSFT", u, qty=10, ref="100")
    assert e.result == "PASS" and code(e) == "WITHIN_LIMITS" and e.input["concentration"]["projected_pct"] == "10.00"
    assert e.reason == ("No Robinhood holding in MSFT; projected MSFT exposure 10.00% of the known invested value ($0.00 known + $1,000.00 planned) / "
                        "($9,000.00 known + $1,000.00 planned) is within the 20% limit.")
    assert not EN.blocking([e])


def test_G03_the_concentration_example_44_pct_blocks_and_both_rules_are_reported():
    u = US.build(alpaca([]), rh_rows([("SNDK", "10", "5000.00"), ("AAPL", "10", "7000.00")]))           # known invested $12,000, SNDK $5,000
    only = ev("SNDK", u, G.GateSettings(enabled=True, block_duplicate_symbol=False), qty=1, ref="500")
    c = only.input["concentration"]
    assert only.result == "BLOCK" and code(only) == "SYMBOL_CONCENTRATION_ABOVE_LIMIT" and c["projected_pct"] == "44.00"
    assert (c["symbol_value"], c["planned_notional"], c["invested_value"], c["projected_invested_value"]) == ("5000.00", "500.00", "12000.00", "12500.00")
    assert "No trade and no resize" in only.reason and only.input["rules"]["duplicate_symbol"]["result"] == "OFF"
    both = ev("SNDK", u, qty=1, ref="500")
    assert both.result == "BLOCK" and code(both) == "EXTERNAL_SYMBOL_ALREADY_HELD" and "Also projected SNDK exposure 44.00%" in both.reason
    new = ev("MSFT", u, qty=40, ref="100")                                                               # a new symbol: $4,000 / $16,000 = 25 %
    assert new.result == "BLOCK" and code(new) == "SYMBOL_CONCENTRATION_ABOVE_LIMIT" and new.input["concentration"]["projected_pct"] == "25.00"


def test_G04_the_threshold_comparison_is_strictly_greater_and_exact():
    u = US.build(alpaca([]), rh_rows([("AAPL", "1", "800.00")]))
    at = ev("MSFT", u, qty=1, ref="200")                                                                 # 200 / 1000 = exactly 20 %
    above = ev("MSFT", u, qty=1, ref="200.01")                                                           # 200.01 / 1000.01 = 20.0008 %
    assert at.result == "PASS" and at.input["concentration"]["projected_pct"] == "20.00" and Decimal(at.input["concentration"]["projected_pct_exact"]) == 20
    assert above.result == "BLOCK" and code(above) == "SYMBOL_CONCENTRATION_ABOVE_LIMIT" and above.input["concentration"]["projected_pct"] == "20.00"   # displays 20.00, blocks
    s = G.GateSettings(enabled=True, max_symbol_exposure_pct="20.5")
    assert ev("MSFT", u, s, qty=1, ref="206.28").result == "PASS" and ev("MSFT", u, s, qty=1, ref="206.29").result == "BLOCK"   # 20.4993 % / 20.50005 %
    assert ev("MSFT", u, G.GateSettings(enabled=True, max_symbol_exposure_pct="100"), qty=1000, ref="1000").result == "PASS"     # 100 never blocks (<= 100 always)


def test_G05_decimal_money_has_no_binary_float_threshold_flip():
    rows = rh_rows([("XYZ", "1", "0.1"), ("AAPL", "1", "0.7")])
    u = US.build(alpaca([]), rows)
    s = G.GateSettings(enabled=True, block_duplicate_symbol=False, max_symbol_exposure_pct="30")
    assert (0.1 + 0.2) / (0.1 + 0.7 + 0.2) * 100 > 30                                                    # binary float would block here ...
    e = ev("XYZ", u, s, qty=1, ref="0.2")
    assert e.result == "PASS" and Decimal(e.input["concentration"]["projected_pct_exact"]) == Decimal(30)   # ... Decimal: exactly 30 %, allowed
    assert isinstance(G.GateSettings().max_symbol_exposure_pct, str) and isinstance(config.UNIFIED_MAX_SYMBOL_EXPOSURE_PCT, str)


def test_G06_fractional_quantities_are_kept_exactly():
    u = US.build(alpaca([]), rh_rows([("NVDA", "5.283683", "1142.97"), ("SNDK", "4.277343", "5417.34")]))
    for sym, q in (("NVDA", "5.283683"), ("SNDK", "4.277343")):
        e = ev(sym, u)
        assert e.input["external_holdings"][0]["quantity"] == q and f"{q} {sym} shares" in e.reason
    tiny = US.build(alpaca([]), rh_rows([("NVDA", "0.000001", "0.01")]))
    assert ev("NVDA", tiny).result == "BLOCK"                                                            # a fraction of a share is a holding


def test_G07_stale_source_uses_the_last_known_holdings_and_says_so():
    st = US.build(alpaca([]), rh_rows([("NVDA", "5.283683", "1142.97")], state="STALE"))
    assert st["state"] == "STALE_SOURCE"
    held = ev("NVDA", st)
    assert held.result == "BLOCK" and code(held) == "EXTERNAL_SYMBOL_ALREADY_HELD" and "Based on STALE data read at 2026-09-28T00:04:59+00:00" in held.reason
    other = ev("AMD", st, qty=1, ref="10")
    assert other.result == "WARN" and code(other) == "ALLOWED_ON_STALE_DATA" and other.input["concentration"]["basis"] == "STALE" and not EN.blocking([other])
    assert ev("AMD", st, G.GateSettings(enabled=True, stale_source_policy="BLOCK_IF_UNKNOWN"), qty=1, ref="10").result == "WARN"   # stale is known data, not unknown


@pytest.mark.parametrize("rh_state", ["NOT_CONFIGURED", "AUTH_REQUIRED", "NOT_LOADED", "ERROR"])
def test_G08_partial_never_reads_a_missing_robinhood_as_zero_holdings(rh_state):
    u = US.build(alpaca([("F", "1", "12.1", "11.58", "11.58", "EXTERNAL")]), rh_rows([], state=rh_state))
    assert u["state"] == "PARTIAL"
    e = ev("NVDA", u)
    assert e.result == "WARN" and code(e) == "EXTERNAL_DATA_UNKNOWN" and e.input["rules"]["duplicate_symbol"]["result"] == "UNKNOWN"
    assert e.input["concentration"]["evaluated"] is False and e.input["concentration"]["projected_pct"] is None   # no falsely precise share from incomplete totals
    assert f"Robinhood holdings unknown ({rh_state}), not zero" in e.reason and "No Robinhood holding" not in e.reason
    closed = ev("NVDA", u, G.GateSettings(enabled=True, stale_source_policy="BLOCK_IF_UNKNOWN"))
    assert closed.result == "BLOCK" and code(closed) == "EXTERNAL_DATA_UNKNOWN" and "Blocked by policy BLOCK_IF_UNKNOWN" in closed.reason


def test_G09_partial_with_the_paper_account_missing_still_blocks_a_known_robinhood_holding():
    u = US.build(alpaca(state="NOT_LOADED"), rh_rows([("NVDA", "2", "300.00")]))
    assert u["state"] == "PARTIAL"
    assert code(ev("NVDA", u)) == "EXTERNAL_SYMBOL_ALREADY_HELD"                                         # a KNOWN holding blocks whatever the data state
    e = ev("MSFT", u)
    assert e.result == "WARN" and e.input["rules"]["duplicate_symbol"]["result"] == "PASS" and e.input["concentration"]["evaluated"] is False


@pytest.mark.parametrize("unified", [None, {}, {"state": "BANANA"}, "garbage", {"state": "UNAVAILABLE", "error": "RuntimeError", "sources": [], "rows": []}])
def test_G10_unavailable_is_never_zero_exposure_and_the_policy_decides(unified):
    e = ev("NVDA", unified)
    assert e.result == "WARN" and code(e) == "EXTERNAL_DATA_UNKNOWN" and e.input["unified_state"] == "UNAVAILABLE" and "not zero" in e.reason
    assert ev("NVDA", unified, G.GateSettings(enabled=True, stale_source_policy="BLOCK_IF_UNKNOWN")).result == "BLOCK"
    assert US.build(alpaca(state="NOT_LOADED"), None)["state"] == "UNAVAILABLE" and ev("NVDA", US.build(alpaca(state="NOT_LOADED"), None)).result == "WARN"


def test_G11_ownership_external_needs_review_and_a_mislabelled_real_row_all_count_and_paper_rows_stay_broker_position_s_job():
    for own in ("USER_MANAGED", "EXTERNAL", "NEEDS_REVIEW", "BOT_MANAGED"):
        u = US.build(alpaca([]), rh_rows([("NVDA", "1", "100.00")]))
        u["rows"][0]["ownership"] = own                                                                  # a non-paper row is never the bot's
        e = ev("NVDA", u)
        assert e.result == "BLOCK" and code(e) == "EXTERNAL_SYMBOL_ALREADY_HELD", own
    u = US.build(alpaca([("NVDA", "3", "100", "100", "300.00", "EXTERNAL")]), rh_rows([("AAPL", "1", "100000.00")]))
    e = ev("NVDA", u)                                                                                    # Alpaca holds it: BROKER_POSITION blocks that, not this gate
    assert e.result == "PASS" and e.input["external_holdings"] == [] and e.input["holdings"][0]["account_type"] == "PAPER"
    assert e.input["concentration"]["symbol_value"] == "300.00"                                         # ... but the Alpaca value counts in the concentration
    before = copy.deepcopy(u)
    ev("NVDA", u)
    assert u == before                                                                                   # the gate never mutates its input


def test_G12_same_symbol_two_accounts_are_summed_but_a_duplicated_row_is_counted_once():
    u = US.build(alpaca([("NVDA", "2", "250", "250", "500.00", "EXTERNAL")]), rh_rows([("NVDA", "10", "4000.00"), ("AAPL", "1", "15500.00")]))
    s = G.GateSettings(enabled=True, block_duplicate_symbol=False)
    two = ev("NVDA", u, s, qty=1, ref="500")
    assert two.input["concentration"]["symbol_value"] == "4500.00" and two.input["concentration"]["invested_value"] == "20000.00"   # two real accounts: $500 + $4,000
    dup = copy.deepcopy(u)
    dup["rows"].append(dict(next(r for r in u["rows"] if r["source"] == "ROBINHOOD" and r["symbol"] == "NVDA")))   # the SAME Robinhood row twice
    once = ev("NVDA", dup, s, qty=1, ref="500")
    assert once.input["concentration"]["symbol_value"] == "4500.00" and once.input["concentration"]["invested_value"] == "20000.00"
    assert any("identical ROBINHOOD rows counted once" in n for n in once.input["notes"])
    bad = copy.deepcopy(u)
    bad["rows"].append({**next(r for r in u["rows"] if r["source"] == "ROBINHOOD" and r["symbol"] == "NVDA"), "market_value": "3000.00"})
    worst = ev("NVDA", bad, s, qty=1, ref="500")                                                         # conflicting rows: the candidate's largest value
    assert worst.input["concentration"]["symbol_value"] == "4500.00" and any("conflicting" in n for n in worst.input["notes"])
    other = copy.deepcopy(u)
    other["rows"].append({**next(r for r in u["rows"] if r["symbol"] == "AAPL"), "market_value": "100.00"})
    low = ev("NVDA", other, s, qty=1, ref="500")                                                         # another symbol's conflict: its smallest value (a smaller total)
    assert low.input["concentration"]["invested_value"] == "4600.00"


def test_G13_malformed_zero_and_negative_quantities_and_values():
    zero = US.build(alpaca([]), rh_rows([("NVDA", "0", "0.00"), ("AAPL", "1", "100000.00")]))
    assert ev("NVDA", zero).result == "PASS"                                                             # quantity 0 is no position
    short = US.build(alpaca([]), rh_rows([("NVDA", "-3", "-300.00"), ("AAPL", "1", "100000.00")]))
    e = ev("NVDA", short)
    assert e.result == "BLOCK" and e.input["external_holdings"][0]["quantity"] == "-3" and e.input["external_holdings"][0]["market_value"] == "300.00"   # gross
    for bad in ("abc", "NaN", "Infinity", None, ""):
        u = US.build(alpaca([]), rh_rows([("NVDA", "2", "100.00"), ("AAPL", "1", "100000.00")]))
        u["rows"][0]["market_value"] = bad
        assert code(ev("NVDA", u)) == "EXTERNAL_SYMBOL_ALREADY_HELD", bad                                # the holding is known even without a value
        assert code(ev("NVDA", u, G.GateSettings(enabled=True, block_duplicate_symbol=False))) == "EXTERNAL_VALUE_UNREADABLE", bad   # cannot be bounded: blocks
        u["rows"][0]["symbol"] = "AMD"
        o = ev("MSFT", u, qty=1, ref="100")                                                              # another symbol's bad value: excluded, a lower bound
        assert o.result == "PASS" and o.input["concentration"]["unvalued_positions_excluded"] == 1, bad
    noqty = US.build(alpaca([]), rh_rows([("NVDA", "2", "100.00")]))
    noqty["rows"][0]["quantity"] = "two"
    assert code(ev("NVDA", noqty)) == "EXTERNAL_SYMBOL_ALREADY_HELD"                                     # an unreadable quantity of a present row is still a holding
    nosym = US.build(alpaca([]), rh_rows([("NVDA", "2", "100.00"), ("AAPL", "1", "100000.00")]))
    nosym["rows"][0]["symbol"] = ""
    e = ev("NVDA", nosym)
    assert e.result == "WARN" and code(e) == "EXTERNAL_DATA_UNKNOWN" and e.input["unreadable_external_rows"] == 1   # never silently dropped


def test_G17_an_empty_known_portfolio_makes_any_entry_100_pct_by_the_specified_formula():
    empty = US.build(alpaca([]), rh_rows([]))                                                            # both accounts KNOWN and empty (COMPLETE)
    e = ev("MSFT", empty, qty=1, ref="10")
    assert empty["state"] == "COMPLETE" and e.result == "BLOCK" and code(e) == "SYMBOL_CONCENTRATION_ABOVE_LIMIT" and e.input["concentration"]["projected_pct"] == "100.00"
    assert ev("MSFT", empty, G.GateSettings(enabled=True, max_symbol_exposure_pct="100"), qty=1, ref="10").result == "PASS"   # documented: 100 turns the rule off


@pytest.mark.parametrize("qty,ref", [(0, "100"), (1, None), (1, "0"), (1, "-5"), (None, "100")])
def test_G14_a_missing_or_zero_planned_notional_blocks_and_the_display_precheck_only_skips_the_concentration(qty, ref):
    u = US.build(alpaca([]), rh_rows([("AAPL", "1", "100000.00")]))
    e = G.evaluate("MSFT", qty, Decimal(ref) if ref is not None else None, u, ON)
    assert e.result == "BLOCK" and code(e) == "PLANNED_NOTIONAL_INVALID"
    p = G.evaluate("MSFT", qty, Decimal(ref) if ref is not None else None, u, ON, planned_required=False)
    assert p.result == "WARN" and code(p) == "NO_PLANNED_ENTRY"
    h = US.build(alpaca([]), rh_rows([("MSFT", "1", "100.00")]))
    assert code(G.evaluate("MSFT", qty, None, h, ON, planned_required=False)) == "EXTERNAL_SYMBOL_ALREADY_HELD"   # the duplicate rule still answers


@pytest.mark.parametrize("over", [{"max_symbol_exposure_pct": "abc"}, {"max_symbol_exposure_pct": "0"}, {"max_symbol_exposure_pct": "100.01"},
                                  {"max_symbol_exposure_pct": "-1"}, {"max_symbol_exposure_pct": "NaN"}, {"stale_source_policy": "ALLOW_EVERYTHING"}])
def test_G15_an_invalid_configuration_fails_closed(over):
    e = ev("MSFT", US.build(alpaca([]), rh_rows([])), G.GateSettings(enabled=True, **over))
    assert e.result == "BLOCK" and code(e) == "GATE_CONFIG_INVALID"


def test_G16_settings_defaults_versions_and_the_contract():
    s = G.from_config(config)
    assert s == G.GateSettings() and s.enabled is False and s.block_duplicate_symbol is True and s.max_symbol_exposure_pct == "20" and s.stale_source_policy == "BLOCK_DUPLICATE_ONLY_IF_KNOWN"
    assert (config.UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED, config.UNIFIED_BLOCK_DUPLICATE_SYMBOL, config.UNIFIED_MAX_SYMBOL_EXPOSURE_PCT, config.UNIFIED_STALE_SOURCE_POLICY) == \
        (False, True, "20", "BLOCK_DUPLICATE_ONLY_IF_KNOWN")
    assert G.VERSION == "entry_controls_v2" and G.PREVIOUS_VERSION == "entry_controls_v1" and G.ENTRY_CONTROLS_V2 == G.ENTRY_CONTROLS_V1 + ("UNIFIED_EXTERNAL_EXPOSURE",)
    off, on = G.contract(G.GateSettings()), G.contract(ON)
    assert off["entry_controls"] == list(G.ENTRY_CONTROLS_V1) and on["entry_controls"] == list(G.ENTRY_CONTROLS_V2)
    assert on["unified_external_exposure"]["comparison"].startswith("BLOCK if projected_symbol_exposure_pct > max_symbol_exposure_pct") and "never account equity" in on["unified_external_exposure"]["denominator"]
    assert G.contract_hash(G.GateSettings()) != G.contract_hash(ON) and G.summary(ON)["enforced"] is True and G.summary(G.GateSettings())["enforced"] is False


# ================================================================ E the real executor path =======================================================

def test_E01_gate_on_and_robinhood_holds_the_ready_symbol_no_order_no_robinhood_write(tmp_path, monkeypatch):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch)                                  # the default fixture: Robinhood holds NVDA 2, SNDK 1, SHOP 3, MU 1
    rs_calls = []
    fake_rs = types.ModuleType("robin_stocks")
    fake_rs.robinhood = types.SimpleNamespace(**{f: (lambda *a, _f=f, **k: rs_calls.append(_f)) for f in ("order_buy_market", "order_sell_market", "cancel_stock_order", "order")})
    monkeypatch.setitem(sys.modules, "robin_stocks", fake_rs)
    monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", fake_rs.robinhood)
    arm(st, "SNDK")
    out = cyc(st)
    e = evaluations(out)["SNDK"]
    assert out["summary"]["entries"]["blocked"]["SNDK"] == ["UNIFIED_EXTERNAL_EXPOSURE"] and e["result"] == "BLOCKED"
    r = e["reasons"][0]
    assert r["control"] == "UNIFIED_EXTERNAL_EXPOSURE" and r["code"] == "EXTERNAL_SYMBOL_ALREADY_HELD" and r["text"].startswith("Robinhood already holds SNDK as a user-managed real position.")
    assert r["detail"]["external_holdings"][0]["quantity"] == "1" and r["detail"]["unified_state"] == "COMPLETE"
    assert st.sim.posts == [] and st.x.store.intents() == []                                            # Alpaca submit_order = 0, no intent at all
    assert http.writes == [] and rs_calls == [] and {q["path"] for q in http.requests} == {"/portfolio", "/positions"}   # Robinhood writes = 0, GETs only
    row = {c["control_id"]: c for c in st.x.store.controls()}["UNIFIED_EXTERNAL_EXPOSURE"]
    assert row["last_result"] == "BLOCK" and row["fire_count"] == 1 and row["last_input"]["reason_code"] == "EXTERNAL_SYMBOL_ALREADY_HELD"
    run = st.x.store.runs(1)[0]["summary"]
    assert run["entries"]["evaluations"][0]["reasons"][0]["code"] == "EXTERNAL_SYMBOL_ALREADY_HELD"    # persisted with the cycle
    u = st.s.unified_portfolio(refresh=False)
    assert {r["ownership"] for r in u["rows"] if r["source"] == "ROBINHOOD"} == {"USER_MANAGED"} and [r["quantity"] for r in u["rows"] if r["symbol"] == "SNDK"] == ["1"]


def test_E02_gate_on_and_allowed_the_entry_follows_the_existing_path_exactly_once_with_its_version_recorded(tmp_path, monkeypatch):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch, SpyGatewayHttp({"/positions": (200, PF.env(BIG))}))
    pv = arm(st, "SNDK")
    out = cyc(st)
    assert out["summary"]["entries"]["submitted"] == ["SNDK"] and len(st.sim.posts) == 1
    body = st.sim.posts[0] if isinstance(st.sim.posts[0], dict) else json.loads(st.sim.posts[0])
    assert body["side"] == "buy" and str(body["qty"]) == str(pv.final_qty) and body["type"] == "market"   # the quantity is the proposal's, never resized
    it = st.x.store.intents()[0]
    rec = it.environment["entry_controls"]
    assert it.attempt_count == 1 and it.client_order_id.startswith("stockagent-entry-") and rec["version"] == "entry_controls_v2" and rec["contract_hash"] == G.contract_hash(ON)
    assert rec["unified_external_exposure"]["enabled"] is True and rec["unified_external_exposure"]["result"] == "PASS" and rec["unified_external_exposure"]["unified_state"] == "COMPLETE"
    assert http.writes == []
    cyc(st)
    pos = st.x.store.open_position("SNDK")
    assert pos is not None and pos.quantity == pv.final_qty and len(st.sim.posts) == 1                 # one fill, one POST
    for _ in range(3):
        cyc(st)
    assert len(st.sim.posts) == 1 and len({i.client_order_id for i in st.x.store.intents()}) == len(st.x.store.intents())


def test_E03_gate_off_by_default_is_the_stage_69_behaviour_and_the_executor_reads_no_robinhood(tmp_path, monkeypatch):
    st, src, http = gated_stack(tmp_path, monkeypatch)                                  # Robinhood holds SNDK; the gate is OFF (code default)
    arm(st, "SNDK")
    out = cyc(st)
    assert out["summary"]["entries"]["submitted"] == ["SNDK"] and len(st.sim.posts) == 1 and http.requests == []   # advisory only: no executor read at all
    it = st.x.store.intents()[0]
    assert it.environment["entry_controls"]["version"] == "entry_controls_v2" and it.environment["entry_controls"]["unified_external_exposure"] == {
        "enabled": False, "enforced": False, "result": "NOT_EVALUATED", "reason_code": None, "unified_state": None, "snapshot_at": None, "projected_pct": None,
        "threshold_pct": "20", "policy": "BLOCK_DUPLICATE_ONLY_IF_KNOWN"}
    assert "UNIFIED_EXTERNAL_EXPOSURE" not in {c["control_id"] for c in st.x.store.controls()}


def test_E04_entry_controls_v1_without_the_gate_and_v2_with_it(tmp_path, monkeypatch):
    st, _, _ = gated_stack(tmp_path, monkeypatch)
    pv = go_ready(st, "SNDK")
    base = dict(now=st.clock.now, mode="PAPER_AUTO", environment={"paper": True}, safety=None, proposal=pv, setup=st.setups.current("SNDK"))
    assert [c.control_id for c in EN.entry_controls(EN.EntryContext(**base))] == list(G.ENTRY_CONTROLS_V1)
    assert [c.control_id for c in EN.entry_controls(EN.EntryContext(**base, exposure_gate=G.GateSettings()))] == list(G.ENTRY_CONTROLS_V1)
    assert [c.control_id for c in EN.entry_controls(EN.EntryContext(**base, exposure_gate=ON, unified_portfolio=None))] == list(G.ENTRY_CONTROLS_V2)


def test_E05_exits_continue_while_the_gate_blocks_entries_and_the_robinhood_holding_is_untouched(tmp_path, monkeypatch):
    st, src, http = gated_stack(tmp_path, monkeypatch)
    pv = arm(st, "SNDK")
    cyc(st)
    cyc(st)
    pos = st.x.store.open_position("SNDK")
    assert pos is not None
    gate(monkeypatch)                                                                     # now the gate is ON and Robinhood holds SNDK
    below_invalidation(st, pv)
    for _ in range(3):
        cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED"
    sides = [(b if isinstance(b, dict) else json.loads(b))["side"] for b in st.sim.posts]
    assert sides == ["buy", "sell"] and http.writes == []                                              # the bot's own PAPER shares only
    u = st.s.unified_portfolio(refresh=False)
    assert [(r["quantity"], r["ownership"]) for r in u["rows"] if r["source"] == "ROBINHOOD" and r["symbol"] == "SNDK"] == [("1", "USER_MANAGED")]


def test_E06_a_robinhood_holding_never_exits_and_monitor_only_never_enters_or_reads(tmp_path, monkeypatch):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch)
    st.sim.set_price("MU", 1.0)                                                           # Robinhood holds MU; the bot does not
    go_ready(st, "SNDK")                                                                  # READY, but Monitor only
    for _ in range(3):
        out = cyc(st)
    assert out["result"] == "IDLE" and st.sim.posts == [] and st.x.store.intents() == [] and http.requests == []
    assert "UNIFIED_EXTERNAL_EXPOSURE" not in {c["control_id"] for c in st.x.store.controls()}


def test_E07_paper_auto_is_still_required_and_a_mode_switch_during_evaluation_sends_nothing(tmp_path, monkeypatch):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch, SpyGatewayHttp({"/positions": (200, PF.env(BIG))}))
    arm(st, "SNDK")
    st.x.hooks["after_intent"] = lambda ctx: st.x.disable_auto(actor="user", now=st.clock.now)       # the user switches to Monitor only mid-evaluation
    cyc(st)
    it = st.x.store.intents()[0]
    assert it.state == "NOT_SENT" and it.failure_code == "PRECHECK_FAILED" and "mode is no longer PAPER_AUTO" in it.failure_text and st.sim.posts == []


def test_E08_pause_and_emergency_stop_are_unchanged_and_the_gate_never_overrides_them(tmp_path, monkeypatch):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch, SpyGatewayHttp({"/positions": (200, PF.env(BIG))}))
    arm(st, "SNDK")
    st.safety.pause(actor="user")
    out = cyc(st)
    assert "SAFETY_GATE" in out["summary"]["entries"]["blocked"]["SNDK"] and "UNIFIED_EXTERNAL_EXPOSURE" not in out["summary"]["entries"]["blocked"]["SNDK"] and st.sim.posts == []
    assert {c["control_id"]: c for c in st.x.store.controls()}["UNIFIED_EXTERNAL_EXPOSURE"]["last_result"] == "PASS"   # evaluated, allowed, and still no entry
    st.safety.resume(actor="user")
    st.safety.emergency_stop(actor="user")
    out = cyc(st)
    assert "SAFETY_GATE" in out["summary"]["entries"]["blocked"]["SNDK"] and st.sim.posts == [] and st.x.store.positions(open_only=False) == []   # a stop never liquidates


def test_E09_reconciliation_is_identical_with_the_gate_on_or_off(tmp_path, monkeypatch):
    seen = []
    for on in (False, True):
        gate(monkeypatch, on=on)
        st, src, http = gated_stack(tmp_path / str(on), monkeypatch)
        st.sim.set_position("F", 1, 12.1)                                                 # an external Alpaca position
        arm(st, "SNDK")
        cyc(st)
        seen.append((st.x.recon_healthy, sorted((f["kind"], f["symbol"], bool(f["blocking"])) for f in st.x.store.findings(50))))
    assert seen[0] == seen[1] == (True, [("EXTERNAL_POSITION", "F", False)])


@pytest.mark.parametrize("hook,posts", [("after_attempt_recorded", 0), ("after_post", 1)])
def test_E10_exactly_once_is_unchanged_for_an_allowed_entry(tmp_path, monkeypatch, hook, posts):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch, SpyGatewayHttp({"/positions": (200, PF.env(BIG))}))
    arm(st, "SNDK")
    st.x.hooks[hook] = boom
    with pytest.raises(SimulatedCrash):
        cyc(st)
    reopen(st)
    for _ in range(4):
        cyc(st, 31)
    it = st.x.store.intents()[0]
    assert len(st.sim.posts) == posts and it.attempt_count <= 1 and it.state == ("FILLED" if posts else "NOT_SENT")
    assert len({i.client_order_id for i in st.x.store.intents()}) == len(st.x.store.intents()) == 1


def test_E11_a_gateway_failure_during_the_cycle_keeps_the_last_known_holding_or_is_unknown_never_zero(tmp_path, monkeypatch):
    gate(monkeypatch)

    class Down(SpyGatewayHttp):
        down = False

        def get(self, *a, **k):
            if self.down:
                raise requests.ConnectionError("refused")
            return super().get(*a, **k)
    http = Down()
    st, src, _ = gated_stack(tmp_path, monkeypatch, http, ttl_s=0)
    src.view(refresh=True)                                                                # a good snapshot: SNDK held
    http.down = True
    arm(st, "SNDK")
    e = evaluations(cyc(st))["SNDK"]
    assert e["result"] == "BLOCKED" and e["reasons"][0]["code"] == "EXTERNAL_SYMBOL_ALREADY_HELD" and "STALE" in e["reasons"][0]["text"] and st.sim.posts == []
    for policy, posts in (("BLOCK_IF_UNKNOWN", 0), ("BLOCK_DUPLICATE_ONLY_IF_KNOWN", 1)):
        gate(monkeypatch, UNIFIED_STALE_SOURCE_POLICY=policy)
        down = Down()
        down.down = True
        st2, _, _ = gated_stack(tmp_path / policy, monkeypatch, down, ttl_s=0)            # no snapshot ever: ERROR -> PARTIAL
        arm(st2, "SNDK")
        out = cyc(st2)
        assert len(st2.sim.posts) == posts, policy
        if posts:
            assert st2.x.store.intents()[0].environment["entry_controls"]["unified_external_exposure"]["reason_code"] == "EXTERNAL_DATA_UNKNOWN"   # allowed and recorded as such
        else:
            assert evaluations(out)["SNDK"]["reasons"][0]["code"] == "EXTERNAL_DATA_UNKNOWN"


def test_E12_a_unified_portfolio_failure_is_unavailable_never_an_empty_portfolio(tmp_path, monkeypatch):
    gate(monkeypatch, UNIFIED_STALE_SOURCE_POLICY="BLOCK_IF_UNKNOWN")
    st, src, http = gated_stack(tmp_path, monkeypatch)

    def broken(refresh=True):
        raise RuntimeError("unified portfolio exploded")
    monkeypatch.setattr(st.s, "unified_portfolio", broken)
    arm(st, "SNDK")
    e = evaluations(cyc(st))["SNDK"]
    assert e["result"] == "BLOCKED" and e["reasons"][0]["code"] == "EXTERNAL_DATA_UNKNOWN" and e["reasons"][0]["detail"]["unified_state"] == "UNAVAILABLE" and st.sim.posts == []
    assert st.x._unified_portfolio()["error"] == "RuntimeError"
    monkeypatch.delattr(type(st.s), "unified_portfolio")                                   # not wired at all
    monkeypatch.delattr(st.s, "unified_portfolio")
    assert st.x._unified_portfolio() == {"state": "UNAVAILABLE", "error": "NOT_WIRED", "sources": [], "rows": []}


def test_E13_one_consistent_snapshot_per_scan_even_with_two_candidates(tmp_path, monkeypatch):
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch, SpyGatewayHttp({"/positions": (200, PF.env(BIG))}))
    calls = []
    real = st.s.unified_portfolio
    monkeypatch.setattr(st.s, "unified_portfolio", lambda refresh=True: calls.append(refresh) or real(refresh=refresh))
    st.x.max_entries_per_cycle = 2
    go_ready(st, "SNDK")
    go_ready(st, "AMD")
    st.x.enable_auto(actor="test", now=st.clock.now)
    out = cyc(st)
    assert calls == [True] and sorted(out["summary"]["entries"]["submitted"]) == ["AMD", "SNDK"]
    snaps = {i.symbol: i.environment["entry_controls"]["unified_external_exposure"]["snapshot_at"] for i in st.x.store.intents()}
    assert len(set(snaps.values())) == 1                                                                 # both decided on the same snapshot


# ================================================================ W write isolation ===============================================================

def test_W01_the_gate_module_imports_nothing_that_can_write_and_names_no_account_in_code():
    src = (ROOT / "execution" / "exposure_gate.py").read_text(encoding="utf-8")
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module)
    assert mods == {"__future__", "hashlib", "json", "dataclasses", "decimal", "typing", "execution.models"}
    assert not re.search(r"(?i)robinhood|robin_stocks|requests|post_order|insert_intent|update_intent|set_mode|place_|cancel_|submit|\.post\(|open\(", strip_code(src))
    for f in ("execution/engine.py", "execution/service.py"):
        assert not re.search(r"(?i)robinhood|robin_stocks", strip_code((ROOT / f).read_text(encoding="utf-8"))), f   # the Stage 6.0 boundary holds


# ================================================================ A API, Bot Activity, forward validation ===========================================

def test_A01_the_unified_route_shows_the_gate_advisory_when_off_and_enforced_when_on(stack, monkeypatch):
    monkeypatch.setattr(stack.s, "account_refresh", SimRefresher(stack))
    robinhood(monkeypatch, ttl_s=0)
    go_ready(stack, "SNDK")
    j = stack.api.get("/api/unified-portfolio").json()
    g = j["entry_gate"]
    assert g["enabled"] is False and g["version"] == "entry_controls_v2" and g["entry_controls"] == list(G.ENTRY_CONTROLS_V1)
    assert g["symbols"]["SNDK"]["label"] == "WOULD BLOCK · advisory" and g["symbols"]["SNDK"]["reason_code"] == "EXTERNAL_SYMBOL_ALREADY_HELD" and g["symbols"]["SNDK"]["enforced"] is False
    assert j["duplicate_entry_policy"]["SNDK"]["enforced"] is False and "not an executor input" in j["current_entry_policy"]
    gate(monkeypatch)
    raw = stack.api.get("/api/unified-portfolio").text
    j = json.loads(raw)
    assert j["entry_gate"]["enabled"] is True and j["entry_gate"]["symbols"]["SNDK"]["label"] == "BLOCKED" and j["entry_gate"]["symbols"]["SNDK"]["would_block"] is True
    assert j["duplicate_entry_policy"]["SNDK"]["enforced"] is True and j["duplicate_entry_policy"]["SNDK"]["mode"] == "ENFORCED" and "UNIFIED_EXTERNAL_EXPOSURE" in j["current_entry_policy"]
    assert SECRET not in raw and "3450" not in json.dumps(j["entry_gate"]) and stack.sim.posts == []
    assert stack.api.post("/api/unified-portfolio", json={}).status_code == 405


def test_A02_execution_overview_and_bot_activity_show_the_gate(tmp_path, monkeypatch, stack):
    gate(monkeypatch)
    monkeypatch.setattr(stack.s, "account_refresh", SimRefresher(stack))
    robinhood(monkeypatch)
    ov = stack.api.get("/api/execution").json()
    assert ov["entry_controls"]["enabled"] is True and ov["entry_controls"]["entry_controls"][-1] == "UNIFIED_EXTERNAL_EXPOSURE" and ov["entry_controls"]["contract_hash"] == G.contract_hash(ON)
    arm(stack, "SNDK")
    cyc(stack)
    a = ACT.activity(stack.x, scheduler_status={"running": True}, enabled=True, now=stack.clock.now)
    row = {t["symbol"]: t for t in a["tickers"]}["SNDK"]
    r = row["reasons"][0]
    assert row["status"] == "BLOCKED" and r["label"] == "Unified exposure" and r["code"] == "EXTERNAL_SYMBOL_ALREADY_HELD" and r["detail"]["external_holdings"][0]["source"] == "ROBINHOOD"
    assert "Unified exposure" in {c["label"] for c in a["risk"]["controls"]} and a["mode"]["mode"] == "PAPER_AUTO" and a["last_cycle"]["mode"] == "PAPER_AUTO"


def test_A03_forward_validation_identifies_the_entry_control_regime_of_every_trade(tmp_path, monkeypatch):
    from forward_validation import reports as REP
    from forward_validation.service import entry_controls_of, entry_controls_mix
    import forward_validation as FV
    gate(monkeypatch)
    st, src, http = gated_stack(tmp_path, monkeypatch, SpyGatewayHttp({"/positions": (200, PF.env(BIG))}))
    pv = arm(st, "SNDK")
    cyc(st)
    cyc(st)
    below_invalidation(st, pv)
    for _ in range(3):
        cyc(st)
    t = st.s.forward_validation.closed_trades()[0]
    assert t["entry_controls"]["version"] == "entry_controls_v2" and t["entry_controls"]["recorded"] is True and t["entry_controls"]["unified_external_exposure"]["enabled"] is True
    cols, rows = REP.export_rows(st.s.forward_validation, "trades")
    assert {"entry_controls_version", "unified_exposure_gate_enabled", "unified_exposure_result"} <= set(cols)
    assert (rows[0]["entry_controls_version"], rows[0]["unified_exposure_gate_enabled"], rows[0]["unified_exposure_result"]) == ("entry_controls_v2", True, "PASS")
    old = entry_controls_of(SimpleNamespace(environment={"paper": True, "host": "paper-api.alpaca.markets"}))   # an intent recorded before Stage 6.9.1
    assert old["version"] == "entry_controls_v1" and old["recorded"] is False and old["unified_external_exposure"]["result"] == "NOT_PRESENT"
    assert entry_controls_mix([t, {"entry_controls": old}, {"entry_controls": {"version": "entry_controls_v2", "unified_external_exposure": {"enabled": False}}}]) == {
        "entry_controls_v2 gate on": 1, "entry_controls_v1": 1, "entry_controls_v2 gate off": 1}
    cum = st.s.forward_validation.cumulative()
    assert cum["frozen_controls_hash"] == FV.frozen_hash() and cum["entry_controls"]["closed_trades_by_entry_controls"] == {"entry_controls_v2 gate on": 1}


# ================================================================ F the Stage 6.9.1 freeze =========================================================
# Stage 6.9.1 changes execution semantics ON PURPOSE. The ledger (tests/freeze_ledger.py) pins the edited files byte-for-byte for the older freeze
# tests; these tests say what changed: the execution files are NOT byte-equivalent to pre-6.9.1 main, every removed line is listed, every added
# line is a Stage 6.9.1 line, and the entry controls are versioned apart from the unchanged Stage 6.1 frozen strategy controls.

CONTRACT_HASH_OFF = "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa"   # entry_controls_v2, gate off (the code default)
CONTRACT_HASH_ON = "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"    # entry_controls_v2, gate on with the default settings
REPLACED_691 = {"execution/service.py": ['                evaluated(sym, "BLOCKED", [{"control": c.control_id, "text": c.reason} for c in blocked])']}   # a Stage 6.8 line
ENGINE_ADDED_691 = [
    "from execution import exposure_gate                                    # Stage 6.9.1: the UNIFIED_EXTERNAL_EXPOSURE entry control (pure)",
    "    exposure_gate: Any = None                             # Stage 6.9.1: exposure_gate.GateSettings; None or not enabled = the entry_controls_v1 set",
    "    unified_portfolio: Optional[dict] = None              # Stage 6.9.1: the UnifiedPortfolioState read once for this scan (None = not read)",
    "    # Stage 6.9.1 (entry_controls_v2): UNIFIED_EXTERNAL_EXPOSURE, only when UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true. The planned notional is the",
    "    # quantity and reference price above; the other accounts are a risk input only. PASS / WARN allow, BLOCK blocks (blocking() below) - never a resize.",
    "    if c.exposure_gate is not None and c.exposure_gate.enabled:",
    "        out.append(exposure_gate.evaluate(sym, qty, ref, c.unified_portfolio, c.exposure_gate))"]
GATE_SETTINGS_691 = ["UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", "UNIFIED_BLOCK_DUPLICATE_SYMBOL", "UNIFIED_MAX_SYMBOL_EXPOSURE_PCT", "UNIFIED_STALE_SOURCE_POLICY"]


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def _base(rel):
    out = subprocess.run(["git", "show", f"{BASE_691}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {BASE_691} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def _now(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _added(rel):
    base = set(_base(rel).splitlines())
    return [l for l in _now(rel).splitlines() if l not in base]


def test_F01_stage_691_changes_exactly_its_declared_files_and_no_risk_strategy_broker_or_database_file():
    import freeze_ledger as FL
    assert FL.BASE_691 == BASE_691 and subprocess.run(["git", "cat-file", "-e", f"{BASE_691}^{{commit}}"], cwd=ROOT).returncode == 0
    stage691 = set(_git("diff", "--name-only", BASE_691, FL.BASE_610A).split())                # Stage 6.9.1 itself, between two fixed commits (main before / after PR #33)
    in_base = set(_git("ls-tree", "-r", "--name-only", BASE_691).split())
    new, edited = {p for p in stage691 if p not in in_base}, {p for p in stage691 if p in in_base}
    assert new == FL.NEW_691, {"unexpected_new": sorted(new - FL.NEW_691), "missing_new": sorted(FL.NEW_691 - new)}
    assert edited == FL.EDITED_691, {"unexpected": sorted(edited - FL.EDITED_691), "missing": sorted(FL.EDITED_691 - edited)}
    assert {p for p in stage691 if p.startswith("execution/")} == {"execution/exposure_gate.py", "execution/engine.py", "execution/service.py", "execution/activity.py"}
    changed = set(_git("diff", "--name-only", BASE_691).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests
    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),
                                                            "missing": sorted((FL.NEW_691 | FL.EDITED_691 | later) - changed)}
    for d in ("safety", "proposals", "setups", "strategy_validation", "strategy_registry", "momentum", "value_quality", "mean_reversion", "breakout", "account_refresh",
              "cash_flow", "paper", "portfolio", "database", "scripts", "rh_gateway"):
        assert not [p for p in stage691 if p.startswith(d + "/")], d                         # no risk gate, strategy, broker, legacy portfolio or database file
        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10A's declared files
    assert set(FL.SEMANTIC_691) <= FL.NEW_691 | FL.EDITED_691 and all(len(v) > 40 for v in FL.SEMANTIC_691.values())
    assert set(FL.SEMANTIC_691) - FL.NEW_691 <= set(FL.LEDGER)                                # every edited semantic file is pinned byte-for-byte in the ledger


def test_F02_the_execution_semantics_changed_on_purpose_and_exactly_as_declared():
    import freeze_ledger as FL
    post = lambda rel: subprocess.run(["git", "show", f"{FL.BASE_610A}:{rel}"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")  # noqa: E731 - main after PR #33
    _added = lambda rel: [l for l in post(rel).splitlines() if l not in set(_base(rel).splitlines())]  # noqa: E731 - Stage 6.9.1's own lines (main after it, not the working tree)
    for rel in ("execution/engine.py", "execution/service.py", "trader_view/stream.py", "config.py"):
        assert post(rel) != _base(rel), f"{rel} is NOT byte-equivalent to pre-6.9.1 main, and no test may pretend it is"
        base, now = _base(rel).splitlines(), set(post(rel).splitlines())
        assert [l for l in base if l not in now] == REPLACED_691.get(rel, []), rel           # nothing of main removed but the listed line
        assert not [l for l in post(rel).splitlines() if l not in set(_now(rel).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left
    assert _added("execution/engine.py") == ENGINE_ADDED_691                                    # the new control, exactly
    added_stream = _added("trader_view/stream.py")
    assert added_stream[0] == "    def unified_portfolio(self, refresh: bool = True) -> dict:" and len(added_stream) == 5 and "precheck=False)" in added_stream[-1]
    svc = post("execution/service.py")
    spans = [(n.lineno, n.end_lineno) for n in ast.walk(ast.parse(svc)) if isinstance(n, ast.FunctionDef) and n.name in ("_unified_portfolio", "exposure_precheck")]
    base = set(_base("execution/service.py").splitlines())
    assert len(spans) == 2
    for i, line in enumerate(svc.splitlines(), 1):
        if line.strip() and line not in base:
            assert "Stage 6.9.1" in line or any(a <= i <= b for a, b in spans), (i, line)     # a marked line, or inside one of the two new read-only methods
    assert 'evaluated(sym, "BLOCKED", [{"control": c.control_id, "text": c.reason, **exposure_gate.record(c)} for c in blocked])' in svc   # the replaced line keeps its fields
    cfg_base, cfg_now = _base("config.py"), post("config.py")
    assert cfg_now.startswith(cfg_base.rstrip("\n"))                                          # append-only
    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == GATE_SETTINGS_691
    assert not [n for n in GATE_SETTINGS_691 if re.search(r"EXECUTION|WRITE|ORDER|TRADE|SUBMIT|LIVE", n)]   # still no Robinhood execution setting


def test_F03_the_frozen_v1_hash_is_kept_as_evidence_and_the_entry_controls_are_versioned_apart():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)                              # a clean process: the real configuration
    assert out.stdout.split() == [FROZEN_V1, CONTRACT_HASH_OFF, CONTRACT_HASH_ON], out.stderr[-800:]
    pins = {p.name: re.findall(r'^FROZEN_HASH = "([0-9a-f]{64})"', p.read_text(encoding="utf-8"), re.M) for p in (ROOT / "tests").glob("test_*.py")}
    assert {h for v in pins.values() for h in v} == {FROZEN_V1} and sum(len(v) for v in pins.values()) >= 9   # every old pin kept, none rewritten
    import forward_validation as FV
    assert "entry_controls" not in json.dumps(FV.frozen_controls()) and "UNIFIED" not in json.dumps(FV.frozen_controls())   # the v1 contract is untouched ...
    assert G.contract(ON)["previous_version"] == "entry_controls_v1" and G.contract(ON)["entry_controls"][-1] == "UNIFIED_EXTERNAL_EXPOSURE"   # ... v2 names its delta


def test_F04_the_design_document_states_the_old_hash_the_semantic_delta_and_the_new_versions():
    doc = _now("DESIGN_691_UNIFIED_EXPOSURE_GATE.md")
    for s in (FROZEN_V1, CONTRACT_HASH_OFF, CONTRACT_HASH_ON, "entry_controls_v1", "entry_controls_v2", "UNIFIED_EXTERNAL_EXPOSURE", "EXTERNAL_SYMBOL_ALREADY_HELD",
              "SYMBOL_CONCENTRATION_ABOVE_LIMIT", "BLOCK_DUPLICATE_ONLY_IF_KNOWN", "BLOCK_IF_UNKNOWN", "projected_pct > threshold", "SEMANTIC_691", "never a resize",
              "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=false", "forward validation"):
        assert s in doc, s
