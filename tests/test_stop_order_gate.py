"""
Emergency Stop order gate (EMERGENCY_STOP_ORDER_GATE_DESIGN.md revision 3.5, section 10; decisions C-1 to C-8, R-4, R-5): Stage 4.6B manual
orders and the Paper Auto entry attempt (C-7). Every test runs against tests/alpaca_order_fakes.FakeAlpaca (manual orders) or the Stage 6.0
simulated paper broker (Paper Auto) on a scratch database: no socket, no broker, no real order.
  N   negative rows: one test per classification row (N1-N14), the strict latch (N15), C-2 (N16), malformed reads (N17, N18), budgets and
      today's sequences (N19, N20, N24), recovery stays open (N21), the transport (N22), a model check (N23), retry (N25), texts (N26),
      ownership C-8 (N27-N30), R-5 (N31, N39), C-3 (N32), C-5 (N33), C-1 (N34, N37), the latch not the evaluation (N35), staleness (N36),
      one query for a list (N38)
  R   races on a real SQLite file with separate connections; R4 and R23 number commits with a trigger written in the same transaction
  P   partial fills
  S   static pins
"""
import ast
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from test_alpaca_orders_46b import ROOT, broker, clock, confirm, err, fresh, lab, preview, ready, rules  # noqa: F401 - fixtures and helpers
import test_execution_60 as X60
from test_execution_60 import execution_config, no_execution_wire  # noqa: F401 - the execution layer's autouse fixtures
import execution as X
from database.execution_migrations import run_execution_migrations
from execution import engine as EN
from execution.models import Intent
from execution.store import ExecutionStore
from fit import readonly as RO
from paper import alpaca_order_reads as RD
from paper import alpaca_order_rules as RU
from paper import alpaca_order_store as S
from paper import alpaca_orders as O
from safety.store import SafetyStore

UTC = timezone.utc
NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
NEW_CODES = ("EMERGENCY_STOP_ACTIVE", "EMERGENCY_STOP_STATE_UNKNOWN", "STOP_BLOCKS_BUY", "STOP_BOT_OWNED", "STOP_OWNERSHIP_UNKNOWN", "STOP_READS_STALE",
             "STOP_UNKNOWN_SIDE", "STOP_SELL_NO_POSITION", "STOP_POSITION_NOT_LONG", "STOP_POSITION_INCONSISTENT", "STOP_SELL_EXCEEDS_AVAILABLE",
             "STOP_NO_SHORTING_UNCONFIRMED", "STOP_UNRESOLVED_IN_SYMBOL", "STOP_OPEN_ORDERS_UNREADABLE", "STOP_OPEN_ORDER_CONFLICT",
             "STOP_SELLS_EXCEED_POSITION", "PAUSE_DOES_NOT_BLOCK_MANUAL", "DAILY_HALT_DOES_NOT_BLOCK_MANUAL", "BOT_OWNED_SELL_WARNING")


# ---- the scratch database --------------------------------------------------------------------------------------------------------

def raw(path, sql, args=()):
    c = sqlite3.connect(str(path), timeout=10)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def rows(path, sql, args=()):
    c = sqlite3.connect(str(path), timeout=10)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def stop_on(path):
    """The real Emergency Stop write: safety.store.SafetyStore.latch, BEGIN IMMEDIATE on its own connection, with its KILL_ON event."""
    SafetyStore(Path(path)).latch(NOW, {"kill_active": True, "kill_activated_at": NOW.isoformat(), "kill_activated_by": "user", "kill_reason": "test"},
                                  {"kind": "KILL_ON", "actor": "user", "to_state": "HALTED"})


def stop_off(path):
    """A Clear's column change, on its own connection."""
    raw(path, "UPDATE execution_safety_state SET kill_active = 0, kill_activated_at = NULL, kill_activated_by = NULL, kill_reason = NULL WHERE id = 1")


def latch_gone(path):
    """C-2: the latch cannot be read (its table is gone), so stop_state is UNKNOWN."""
    raw(path, "ALTER TABLE execution_safety_state RENAME TO execution_safety_state_gone")


def bot_position(path, sym, state="OPEN", qty=5):
    n = f"{sym}-{state}-{time.monotonic_ns()}"
    raw(path, "INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, updated_at) "
              "VALUES (?, ?, ?, ?, ?, '100', ?, ?, ?)", ("p-" + n, "x-" + n, sym, qty, max(qty, 1), NOW.isoformat(), state, NOW.isoformat()))


def bot_intent(path, sym, state="PENDING", kind="ENTRY"):
    n = f"{sym}-{state}-{time.monotonic_ns()}"
    raw(path, "INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, state, created_at, updated_at) "
              "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)", ("x-" + n, kind, sym, "buy" if kind == "ENTRY" else "sell", "c-" + n, state, NOW.isoformat(), NOW.isoformat()))


def db(b):
    return Path(b.lab.path)


def gets(b):
    return [p for m, p, _ in b.requests if m == "GET"]


def ev_codes(intent_id):
    return [r["code"] for r in rows(RO.db_path(), "SELECT code FROM alpaca_paper_order_events WHERE intent_id = ? ORDER BY event_id", (intent_id,))]


def reason(e):
    return e.extra.get("reason")


@pytest.fixture
def gate(broker):
    """The 4.6B fixture with the bot's tables present (no bot position, no open intent) and manual orders linked and on."""
    c = sqlite3.connect(str(db(broker)))
    try:
        run_execution_migrations(c)
    finally:
        c.close()
    ready(broker)
    return broker


def stopped(b):
    stop_on(db(b))
    b.requests.clear()
    return b


# ==================================================================================================================================
# N1 - N14: one test per row of section 4 (class, code, 0 POST)
# ==================================================================================================================================

def test_N01_row1_a_buy_is_refused_before_any_request_buy_to_cover_included(gate):
    assert RU.classify_manual_order("BUY", 1, False, {"side": "short", "qty": "-5", "qty_available": "-5"}, [], 0, True) == (RU.RISK_INCREASING, "STOP_BLOCKS_BUY")
    i = preview("AMD", "BUY", 1)                                                            # previewed before the stop
    stopped(gate)
    e = err(O.preview, "AMD", "BUY", 1)
    assert e.code == "STOP_BLOCKS_BUY" and gate.requests == [] and len(S.OrderStore().intents()) == 1      # N19: 0 requests, no row
    e = err(confirm, i)
    assert e.code == "CONFIRM_REJECTED" and reason(e) == "STOP_BLOCKS_BUY" and gate.requests == [] and gate.posts == []
    assert S.OrderStore().intent(i["intent_id"])["state"] == RU.CONFIRM_REJECTED


def test_N02_row2_any_side_but_sell_is_ambiguous(gate):
    for side in ("SHORT", None, "sell", "Buy"):
        assert RU.classify_manual_order(side, 1, False, None, [], 0, True) == (RU.AMBIGUOUS, "STOP_UNKNOWN_SIDE"), side
    stopped(gate)
    assert err(O.preview, "AMD", "SHORT", 1).code == "INVALID_SIDE" and gate.requests == []


def test_N03_row3_unreadable_ownership_refuses_before_any_request(broker):
    ready(broker)                                                                           # the lab has no execution tables: ownership unreadable
    assert RU.classify_manual_order("SELL", 1, None, None, [], 0, True) == (RU.AMBIGUOUS, "STOP_OWNERSHIP_UNKNOWN")
    stopped(broker)
    e = err(O.preview, "AMD", "SELL", 1)
    assert e.code == "STOP_OWNERSHIP_UNKNOWN" and broker.requests == [] and S.OrderStore().intents() == []


def test_N04_row4_a_bot_symbol_is_refused_before_any_request(gate):
    assert RU.classify_manual_order("SELL", 1, True, {"side": "long", "qty": "10", "qty_available": "10", "symbol": "AMD"}, [], 0, True) == \
        (RU.AMBIGUOUS, "STOP_BOT_OWNED")
    bot_position(db(gate), "AMD")
    stopped(gate)
    e = err(O.preview, "AMD", "SELL", 1)
    assert e.code == "STOP_BOT_OWNED" and gate.requests == [] and gate.posts == []


def test_N05_row5_no_position_would_open_a_short(gate):
    stopped(gate)
    it = preview("CLS", "SELL", 1)
    assert rules(it)["V18"] == (False, "STOP_SELL_NO_POSITION") and it["confirmable"] is False
    assert gets(gate) == ["/v2/account", "/v2/account/configurations", "/v2/assets/CLS", "/v2/clock", "/v2/orders", "/v2/positions/CLS"]
    assert err(confirm, it).code == "PREVIEW_NOT_CONFIRMABLE" and gate.posts == []


@pytest.mark.parametrize("pos", [dict(side="short"), dict(drop=("side",)), dict(side="Long"), dict(side="")])
def test_N06_row6_a_side_missing_or_not_exactly_long(gate, pos):
    gate.set_position("AMD", 10, **pos)
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    assert rules(it)["V18"] == (False, "STOP_POSITION_NOT_LONG") and it["confirmable"] is False and gate.posts == []


@pytest.mark.parametrize("qty,avail,drop", [(10, None, ("qty_available",)), (10, None, ("qty",)), (0, 0, ()), (-5, -5, ()), (10, -1, ()), (10, 12, ())])
def test_N07_row7_and_N17_inconsistent_quantities(gate, qty, avail, drop):
    gate.set_position("AMD", qty, available=avail, drop=drop)
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    assert rules(it)["V18"] == (False, "STOP_POSITION_INCONSISTENT") and it["confirmable"] is False and gate.posts == []


@pytest.mark.parametrize("qty,avail", [("abc", "abc"), ("10", "x1")])
def test_N17_an_unparsable_position_value_fails_the_read_before_any_row(gate, qty, avail):
    gate.set_position("AMD", qty, available=avail)
    stopped(gate)
    n = len(S.OrderStore().intents())
    e = err(O.preview, "AMD", "SELL", 1)
    assert e.code == "BROKER_READ_FAILED" and len(S.OrderStore().intents()) == n and gate.posts == []


def test_N08_row8_a_sell_above_qty_available(gate):
    gate.set_position("AMD", 10, available=4)
    stopped(gate)
    assert rules(preview("AMD", "SELL", 5))["V18"] == (False, "STOP_SELL_EXCEEDS_AVAILABLE")
    assert rules(preview("AMD", "SELL", 4))["V18"] == (True, None)


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_N09_row9_no_shorting_must_be_exactly_true(gate, value):
    pos = {"symbol": "AMD", "side": "long", "qty": "10", "qty_available": "10"}
    assert RU.classify_manual_order("SELL", 1, False, pos, [], 0, value) == (RU.AMBIGUOUS, "STOP_NO_SHORTING_UNCONFIRMED")   # the rule itself, whatever the reader sends
    assert RU.classify_manual_order("SELL", 1, False, pos, [], 0, True) == (RU.RISK_REDUCING, None)
    if value is None:
        gate.config.pop("no_shorting")
    else:
        gate.config["no_shorting"] = value
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    assert rules(it)["V18"] == (False, "STOP_NO_SHORTING_UNCONFIRMED") and it["confirmable"] is False


def test_N10_row10_another_unresolved_own_intent_in_the_symbol(gate):
    pos = {"symbol": "AMD", "side": "long", "qty": "10", "qty_available": "10"}
    assert RU.classify_manual_order("SELL", 1, False, pos, [], 1, True) == (RU.AMBIGUOUS, "STOP_UNRESOLVED_IN_SYMBOL")
    a = confirm(preview("AMD", "SELL", 2))["intent"]
    i = preview("AMD", "SELL", 1)                                                           # preview refuses a symbol with an unresolved order, so
    raw(RO.db_path(), "UPDATE alpaca_paper_order_intents SET state = 'RECONCILIATION_REQUIRED' WHERE intent_id = ?", (a["intent_id"],))   # it turns after
    stopped(gate)
    e = err(confirm, i)
    assert e.code == "CONFIRM_REJECTED" and reason(e) in ("UNRESOLVED_IN_SYMBOL", "STOP_UNRESOLVED_IN_SYMBOL") and len(gate.posts) == 1
    st = S.OrderStore()
    row = st.intent(i["intent_id"])
    gates, klass, _ = O._fresh_gates(st, {**row, "intent_id": -1}, st.settings(), include_daily=True, stop=RU.STOP_ON)
    assert (klass, [g["code"] for g in gates if g["rule"] == "V18"]) == (RU.AMBIGUOUS, ["STOP_UNRESOLVED_IN_SYMBOL"])


def _oo(n=1, **kw):
    return [{"symbol": "AMD", "side": "sell", "order_class": "", "qty": "1", "filled_qty": "0", "status": "new", **kw} for _ in range(n)]


def test_N11_row11_and_N18_unreadable_open_orders(gate):
    pos = {"symbol": "AMD", "side": "long", "qty": "1000", "qty_available": "1000"}
    for oo in (None, {"orders": []}, _oo(RU.OPEN_ORDERS_LIMIT), _oo(1, symbol="MU"), ["x"]):
        assert RU.classify_manual_order("SELL", 1, False, pos, oo, 0, True) == (RU.AMBIGUOUS, "STOP_OPEN_ORDERS_UNREADABLE"), oo
    gate.set_position("AMD", 1000)
    for _ in range(RU.OPEN_ORDERS_LIMIT):                                                  # exactly the read limit
        gate.open_order("AMD", "sell", 1)
    stopped(gate)
    assert rules(preview("AMD", "SELL", 1))["V18"] == (False, "STOP_OPEN_ORDERS_UNREADABLE")
    gate.extra_open_orders = []
    gate.open_order("MU", "sell", 1)
    gate.open_orders_mode = "unfiltered"                                                    # Alpaca answered another symbol
    assert rules(preview("AMD", "SELL", 1))["V18"] == (False, "STOP_OPEN_ORDERS_UNREADABLE")
    for mode in ("notlist", "badrow", "garbage", "http:500", "fail"):                       # a failed read stays on today's path: refused, no row
        gate.open_orders_mode = mode
        n = len(S.OrderStore().intents())
        e = err(O.preview, "AMD", "SELL", 1)
        assert e.code == "BROKER_READ_FAILED" and len(S.OrderStore().intents()) == n, mode
    assert gate.posts == []


@pytest.mark.parametrize("kw", [dict(side="buy"), dict(side=None), dict(order_class="bracket"), dict(order_class=None), dict(qty=None),
                                dict(filled_qty=None), dict(qty="2", filled_qty="3"), dict(filled_qty="-1")])
def test_N12_row12_an_open_order_that_is_not_a_plain_sell(gate, kw):
    pos = {"symbol": "AMD", "side": "long", "qty": "10", "qty_available": "10"}
    oo = [{"symbol": "AMD", "side": "sell", "order_class": "", "qty": "1", "filled_qty": "0", "status": "new", **kw}]
    assert RU.classify_manual_order("SELL", 1, False, pos, oo, 0, True) == (RU.AMBIGUOUS, "STOP_OPEN_ORDER_CONFLICT")
    gate.open_order("AMD", **{"side": "sell", "qty": 1, "filled_qty": 0, **kw})
    stopped(gate)
    assert rules(preview("AMD", "SELL", 1))["V18"] == (False, "STOP_OPEN_ORDER_CONFLICT") and gate.posts == []


def test_N12b_an_unparsable_open_order_number_fails_the_whole_read(gate):
    gate.open_order("AMD", "sell", 1)
    gate.open_order("AMD", "sell", "abc")
    stopped(gate)
    assert err(O.preview, "AMD", "SELL", 1).code == "BROKER_READ_FAILED" and gate.posts == []


def test_N13_row13_resting_sells_plus_this_sell_above_qty(gate):
    gate.set_position("AMD", 10)                                                            # qty_available 10 reported: the open SELL 6 not yet reflected
    gate.open_order("AMD", "sell", 6)
    stopped(gate)
    assert rules(preview("AMD", "SELL", 8))["V18"] == (False, "STOP_SELLS_EXCEED_POSITION")
    assert rules(preview("AMD", "SELL", 4))["V18"] == (True, None)


def test_N14_row14_a_reducing_sell_is_sent_with_its_record(gate):
    gate.set_position("AMD", 10, available=4)
    gate.open_order("AMD", "sell", 6)
    stopped(gate)
    it = preview("AMD", "SELL", 4)
    assert rules(it)["V18"] == (True, None) and it["confirmable"] is True
    gate.requests.clear()
    out = confirm(it)
    assert out["intent"]["state"] == "SUBMITTED" and len(gate.posts) == 1 and json.loads(gate.posts[0])["side"] == "sell"
    assert gets(gate) == ["/v2/account", "/v2/account/configurations", "/v2/clock", "/v2/orders", "/v2/positions/AMD"]   # N24: confirm within 5
    codes = ev_codes(it["intent_id"])
    assert RU.STOP_EXIT_ALLOWED in codes and RU.ARMED_BEFORE_STOP not in codes                    # R15
    assert "armed before the stop" not in out["intent"]["state_label"]


# ==================================================================================================================================
# N15 - N39
# ==================================================================================================================================

def _store_states(path):
    return S.OrderStore(path).stop_state(), ExecutionStore(path).stop_state()


def test_N15_the_latch_is_strict_in_both_readers(tmp_path, monkeypatch):
    p = tmp_path / "latch.db"
    assert _store_states(p) == ("UNKNOWN", "UNKNOWN") and not p.exists()                     # no file: UNKNOWN, and the read creates nothing
    raw(p, "CREATE TABLE other (x)")
    assert _store_states(p) == ("UNKNOWN", "UNKNOWN")                                       # no table
    raw(p, "CREATE TABLE execution_safety_state (id INTEGER PRIMARY KEY, kill_active)")
    assert _store_states(p) == ("UNKNOWN", "UNKNOWN")                                       # no row
    for value, want in ((0, "OFF"), (1, "ON"), (2, "UNKNOWN"), ("1", "UNKNOWN"), ("0", "UNKNOWN"), (None, "UNKNOWN"), (1.0, "UNKNOWN"), (True, "ON")):
        raw(p, "INSERT OR REPLACE INTO execution_safety_state (id, kill_active) VALUES (1, ?)", (value,))
        assert _store_states(p) == (want, want), value
    raw(p, "INSERT OR REPLACE INTO execution_safety_state (id, kill_active) VALUES (1, 0)")

    class Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error (fixture)")

        def close(self):
            pass
    monkeypatch.setattr(S.OrderStore, "_ro", lambda self: Boom())
    assert S.OrderStore(p).stop_state() == "UNKNOWN"
    assert ExecutionStore._latch(Boom()) == "UNKNOWN"


@pytest.mark.parametrize("row", ["missing", "0", "1", "unreadable"])
def test_N15b_the_order_gates_readers_agree_with_the_latch_route_on_one_fixture(tmp_path, row):
    """J-04's L05 named the gate's half; it exists now: both gate readers map the same row as the latch route does (G-A2)."""
    p = tmp_path / "agree.db"
    if row != "missing":
        c = sqlite3.connect(str(p))
        from database.execution_safety_migrations import run_execution_safety_migrations
        run_execution_safety_migrations(c)
        c.execute("INSERT INTO execution_safety_state (id, kill_active, updated_at) VALUES (1, ?, ?)", (1 if row == "1" else 0, NOW.isoformat()))
        c.commit()
        c.close()
    if row == "unreadable":
        latch_gone(p)
    want = {"missing": "UNKNOWN", "0": "OFF", "1": "ON", "unreadable": "UNKNOWN"}[row]
    assert _store_states(p) == (want, want)


def test_N16_C2_an_unreadable_latch_refuses_every_order_before_any_request(gate):
    gate.set_position("AMD", 10)
    i = preview("AMD", "SELL", 1)                                                            # a reducing SELL, previewed while readable
    latch_gone(db(gate))
    gate.requests.clear()
    for side in ("SELL", "BUY"):
        assert err(O.preview, "AMD", side, 1).code == "EMERGENCY_STOP_STATE_UNKNOWN"
    e = err(confirm, i)
    assert e.code == "CONFIRM_REJECTED" and reason(e) == "EMERGENCY_STOP_STATE_UNKNOWN" and gate.requests == [] and gate.posts == []


def test_N16b_C2_a_retry_while_the_latch_is_unreadable_is_refused_with_its_state_unchanged(gate):
    gate.post_modes = ["connect_timeout"]
    it = confirm(preview("AMD", "SELL", 1))["intent"]
    assert it["state"] == "SUBMIT_NOT_SENT"
    latch_gone(db(gate))
    gate.requests.clear()
    e = err(O.retry, it["intent_id"], it["preview_hash"])
    assert e.code == "RETRY_REJECTED" and reason(e) == "EMERGENCY_STOP_STATE_UNKNOWN" and gate.requests == [] and gate.posts == []
    assert S.OrderStore().intent(it["intent_id"])["state"] == "SUBMIT_NOT_SENT"


def test_N20_stop_off_keeps_todays_request_sequences_with_one_local_ownership_read_for_a_sell(gate, monkeypatch):
    calls = []
    real = S.OrderStore.bot_owned
    monkeypatch.setattr(S.OrderStore, "bot_owned", lambda self, conn, sym: calls.append(sym) or real(self, conn, sym))
    i = preview("AMD", "BUY", 1)
    assert gets(gate) == ["/v2/account", "/v2/account/configurations", "/v2/assets/AMD", "/v2/clock"] and calls == []
    gate.requests.clear()
    confirm(i)
    assert [p for _, p, _ in gate.requests] == ["/v2/account", "/v2/account/configurations", "/v2/clock", "/v2/orders"] and calls == []
    gate.requests.clear()
    i = preview("AMD", "SELL", 1)
    assert gets(gate) == ["/v2/account", "/v2/account/configurations", "/v2/assets/AMD", "/v2/clock", "/v2/positions/AMD"] and calls == ["AMD"]
    gate.requests.clear()
    confirm(i)
    assert [p for _, p, _ in gate.requests] == ["/v2/account", "/v2/account/configurations", "/v2/clock", "/v2/positions/AMD", "/v2/orders"]
    assert calls == ["AMD"] and "V18" not in rules(i) and "V20" not in rules(i)


def test_N21_recovery_stays_open_during_a_stop_and_while_the_latch_is_unreadable(gate, clock):
    gate.post_modes = ["connect_timeout", "timeout_before", "connect_timeout"]
    a = confirm(preview("AMD", "BUY", 1))["intent"]                                        # SUBMIT_NOT_SENT
    b = confirm(preview("MU", "BUY", 1))["intent"]                                         # RECONCILIATION_REQUIRED
    c = confirm(preview("KO", "BUY", 1))["intent"]                                         # SUBMIT_NOT_SENT, abandoned while the latch is unreadable
    assert (a["state"], b["state"], c["state"]) == ("SUBMIT_NOT_SENT", "RECONCILIATION_REQUIRED", "SUBMIT_NOT_SENT")
    stopped(gate)
    clock[0] += timedelta(seconds=40)
    assert O.check_status()["checked"] >= 1
    assert {x["intent_id"] for x in O.list_intents()["intents"]} >= {a["intent_id"], b["intent_id"]}
    assert O.abandon(a["intent_id"], a["preview_hash"])["intent"]["state"] == "ABANDONED"
    assert O.disable()["enabled"] is False and O.enable()["enabled"] is True                 # disable during the stop (and enable is not an order)
    latch_gone(db(gate))
    clock[0] += timedelta(seconds=40)
    assert O.check_status()["checked"] >= 1 and O.list_intents()["intents"]
    assert O.abandon(c["intent_id"], c["preview_hash"])["intent"]["state"] == "ABANDONED"    # abandon while the latch is unreadable
    assert O.disable()["enabled"] is False
    assert len(gate.posts) == 1                                                             # only the timeout_before POST reached the fake


def test_N22_the_transport_allows_only_the_exact_open_orders_query(gate):
    n0 = len(gate.requests)
    t = RD._ReadTransport(gate, budget=20)
    s = requests.Session()
    s.mount("https://", t)
    base = "https://paper-api.alpaca.markets/v2/orders"
    good = {"status": "open", "symbols": "AMD", "limit": "200", "direction": "desc"}
    for q in ("", "?status=open", "?" + "&".join(f"{k}={v}" for k, v in {**good, "x": "1"}.items()),
              "?" + "&".join(f"{k}={v}" for k, v in {**good, "symbols": "amd"}.items()),
              "?" + "&".join(f"{k}={v}" for k, v in {**good, "symbols": "AMD,MU"}.items()),
              "?" + "&".join(f"{k}={v}" for k, v in {**good, "status": "all"}.items()),
              "?" + "&".join(f"{k}={v}" for k, v in {**good, "limit": "500"}.items()),
              "?" + "&".join(f"{k}={v}" for k, v in good.items()) + "&symbols=MU"):
        with pytest.raises(RD.ReadRefused):
            s.get(base + q)
    for method in ("POST", "DELETE", "PATCH"):
        with pytest.raises(RD.ReadRefused):
            s.request(method, base + "?" + "&".join(f"{k}={v}" for k, v in good.items()))
    assert len(gate.requests) == n0
    assert s.get(base, params=good).status_code == 200 and len(gate.requests) == n0 + 1


def test_N23_model_check_a_reducing_sell_never_shorts_and_lowers_exposure_in_every_fill_order():
    rng = random.Random(4623)
    seen = 0
    for _ in range(3000):
        held = Decimal(rng.choice([0, 1, 2, 3, 5, 8, 10])) + (Decimal("0.5") if rng.random() < 0.2 else 0)
        avail = max(Decimal(0), held - rng.randint(0, 4))
        side = rng.choice(["long", "long", "long", "short", None])
        pos = None if held == 0 and rng.random() < 0.5 else {"symbol": "AMD", "side": side, "qty": str(held), "qty_available": str(avail)}
        oo = []
        for _ in range(rng.randint(0, 3)):
            q = rng.randint(1, 6)
            oo.append({"symbol": "AMD", "side": rng.choice(["sell", "sell", "buy"]), "order_class": "", "qty": str(q),
                       "filled_qty": str(rng.randint(0, q)), "status": "new"})
        qty = rng.randint(1, 10)
        klass, code = RU.classify_manual_order("SELL", qty, False, pos, oo, 0, True)
        if klass != RU.RISK_REDUCING:
            continue
        seen += 1
        legs = [Decimal(o["qty"]) - Decimal(o["filled_qty"]) for o in oo] + [Decimal(qty)]
        for _ in range(6):                                                                  # every fill order, sampled: no short, exposure never grows
            rng.shuffle(legs)
            cur = Decimal(pos["qty"])
            for leg in legs:
                nxt = cur - leg
                assert nxt >= 0 and abs(nxt) <= abs(cur), (pos, oo, qty)
                cur = nxt
    assert seen > 100


def test_N24_budgets_preview_6_and_confirm_5_under_a_stop(gate):
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    assert len(gets(gate)) == 6
    gate.requests.clear()
    confirm(it)
    assert len(gets(gate)) == 5 and len(gate.posts) == 1


def test_N25_a_retry_does_not_block_itself_on_row10(gate, clock):
    stopped(gate)
    gate.post_modes = ["connect_timeout"]
    it = confirm(preview("AMD", "SELL", 1))["intent"]
    assert it["state"] == "SUBMIT_NOT_SENT"
    out = O.retry(it["intent_id"], it["preview_hash"])
    assert out["intent"]["state"] == "SUBMITTED" and len(gate.posts) == 1
    assert ev_codes(it["intent_id"]).count(RU.STOP_EXIT_ALLOWED) == 2


def test_N26_every_new_code_has_a_text_under_200_characters():
    for code in NEW_CODES + ("DAILY_LIMIT",):
        assert code in RU.RULE_TEXT and 0 < len(RU.RULE_TEXT[code]) < 200, code
    assert RU.RULE_TEXT["DAILY_LIMIT"].endswith("This limit also applies during Emergency Stop. Alpaca's website stays available.")
    assert "buy-to-cover" in RU.RULE_TEXT["STOP_BLOCKS_BUY"]


@pytest.mark.parametrize("qty", [2, 5])
def test_N27_C8_a_bot_position_partial_or_full_sell_is_refused_with_no_request(gate, qty):
    bot_position(db(gate), "AMD", qty=5)
    stopped(gate)
    assert err(O.preview, "AMD", "SELL", qty).code == "STOP_BOT_OWNED" and gate.requests == []


def test_N28_C8_an_open_bot_entry_intent_owns_the_symbol_and_a_closed_row_does_not(gate):
    for state in sorted(X.INTENT_OPEN):
        bot_intent(db(gate), "MU", state=state)
        assert S.OrderStore().bot_owned(None, "MU") is True, state
    for state in ("FILLED", "CANCELED", "REJECTED", "NOT_SENT", "FAILED"):
        bot_intent(db(gate), "KO", state=state)
    bot_position(db(gate), "KO", state="CLOSED", qty=0)
    assert S.OrderStore().bot_owned(None, "KO") is False
    stopped(gate)
    assert err(O.preview, "MU", "SELL", 1).code == "STOP_BOT_OWNED"
    gate.set_position("KO", 3)
    assert rules(preview("KO", "SELL", 1))["V18"] == (True, None)


@pytest.mark.parametrize("gone", ["paper_positions", "execution_intents"])
def test_N29_C8_a_missing_or_failing_ownership_table_refuses(gate, gone):
    raw(db(gate), f"ALTER TABLE {gone} RENAME TO {gone}_gone")
    assert S.OrderStore().bot_owned(None, "AMD") is None
    stopped(gate)
    assert err(O.preview, "AMD", "SELL", 1).code == "STOP_OWNERSHIP_UNKNOWN" and gate.requests == []


def test_N30_the_ownership_state_lists_match_the_execution_layer():
    assert set(S.OrderStore.BOT_INTENT_OPEN) == set(X.INTENT_OPEN)
    assert set(X.POSITION_STATES) - {"CLOSED"} == set(X.POSITION_OPEN)                     # "state != 'CLOSED'" is exactly POSITION_OPEN


def test_N31_R5_stop_off_a_bot_position_sell_behaves_as_today_at_the_broker_plus_the_warning(gate):
    bot_position(db(gate), "AMD")
    it = preview("AMD", "SELL", 1)
    assert gets(gate) == ["/v2/account", "/v2/account/configurations", "/v2/assets/AMD", "/v2/clock", "/v2/positions/AMD"]
    assert it["confirmable"] is True and rules(it)["V20"] == (False, "BOT_OWNED_SELL_WARNING")
    gate.requests.clear()
    assert confirm(it)["intent"]["state"] == "SUBMITTED" and len(gate.posts) == 1          # the unchanged cost: the bot's record goes wrong (PF-18)
    assert [p for _, p, _ in gate.requests] == ["/v2/account", "/v2/account/configurations", "/v2/clock", "/v2/positions/AMD", "/v2/orders"]


def test_N32_C3_a_buy_to_cover_on_a_short_is_refused(gate):
    gate.set_position("AMD", 10, side="short")
    stopped(gate)
    e = err(O.preview, "AMD", "BUY", 5)
    assert e.code == "STOP_BLOCKS_BUY" and "buy-to-cover" in e.message and gate.requests == []


def test_N33_C5_the_daily_limit_refuses_a_reducing_sell_and_says_it_applies_during_a_stop(gate, monkeypatch):
    monkeypatch.setattr(O, "_confirmed_today", lambda st, now: RU.DAILY_LIMIT)
    stopped(gate)
    e = err(O.preview, "AMD", "SELL", 1)
    assert e.code == "DAILY_LIMIT" and "This limit also applies during Emergency Stop. Alpaca's website stays available." in e.message


def test_N33b_C5_the_daily_limit_also_refuses_a_reducing_sell_at_confirm_during_a_stop(gate, monkeypatch):
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    assert it["confirmable"] is True
    monkeypatch.setattr(O, "_confirmed_today", lambda st, now: RU.DAILY_LIMIT)               # the 20th confirm landed meanwhile
    e = err(confirm, it)
    assert e.code == "CONFIRM_REJECTED" and reason(e) == "DAILY_LIMIT" and gate.posts == []


def test_N34_C1_a_pause_or_todays_daily_halt_adds_an_information_line_only(gate):
    p = db(gate)
    assert "V19" not in rules(preview("AMD", "BUY", 1))
    raw(p, "UPDATE execution_safety_state SET pause_active = 1 WHERE id = 1")
    it = preview("AMD", "BUY", 1)
    v19 = [r for r in it["rules"] if r["rule"] == "V19"]
    assert v19 and v19[0]["info"] is True and v19[0]["ok"] is False and v19[0]["code"] == "PAUSE_DOES_NOT_BLOCK_MANUAL" and it["confirmable"] is True
    assert confirm(it)["intent"]["state"] == "SUBMITTED"
    raw(p, "UPDATE execution_safety_state SET pause_active = 0, daily_halt_active = 1, daily_halt_trading_date = '2026-09-29' WHERE id = 1")
    assert rules(preview("AMD", "BUY", 1))["V19"] == (False, "DAILY_HALT_DOES_NOT_BLOCK_MANUAL")
    raw(p, "UPDATE execution_safety_state SET daily_halt_trading_date = '2026-09-28' WHERE id = 1")
    assert "V19" not in rules(preview("AMD", "BUY", 1))                                     # a stale daily latch: no line


def test_N35_a_server_unknown_evaluation_with_the_latch_at_0_lets_a_buy_through(gate):
    SafetyStore(db(gate)).record_error(NOW, "TEST", "evaluator failed (fixture)", ["GLOBAL_KILL_SWITCH"])
    assert SafetyStore(db(gate)).events(1, "EVALUATOR_ERROR")[0]["to_state"] == "UNKNOWN"
    assert confirm(preview("AMD", "BUY", 1))["intent"]["state"] == "SUBMITTED"             # pinned on purpose: the gate reads the latch only


def test_N36_reads_older_than_10_s_at_the_arm_are_stale(gate, clock):
    stopped(gate)

    def age(seconds):
        def hook(path, query):
            if path == "/v2/positions/AMD":
                clock[0] += timedelta(seconds=seconds)
        return hook
    for seconds, ok in ((10, True), (11, False)):
        it = preview("AMD", "SELL", 1)
        gate.on_get = age(seconds)
        if ok:
            assert confirm(it)["intent"]["state"] == "SUBMITTED"
        else:
            e = err(confirm, it)
            assert reason(e) == "STOP_READS_STALE" and S.OrderStore().intent(it["intent_id"])["state"] == RU.CONFIRM_REJECTED
        gate.on_get = None
    n = len(gate.posts)
    it = preview("AMD", "SELL", 1)                                                          # 8 s old reads plus a lock held past 10 s at the arm
    holding, release = threading.Event(), threading.Event()

    def locker():
        c = sqlite3.connect(str(db(gate)), timeout=10, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        holding.set()
        release.wait(10)
        clock[0] += timedelta(seconds=3)
        c.execute("COMMIT")
        c.close()

    def hook(path, query):
        if path == "/v2/positions/AMD":
            clock[0] += timedelta(seconds=8)
            threading.Thread(target=locker, daemon=True).start()
            assert holding.wait(10)
            threading.Timer(0.3, release.set).start()                                      # the arm waits on this lock
    gate.on_get = hook
    e = err(confirm, it)
    assert reason(e) == "STOP_READS_STALE" and len(gate.posts) == n


def test_N37_a_failing_disclosure_read_adds_no_line_and_leaves_the_latch_alone(gate, monkeypatch):
    raw(db(gate), "UPDATE execution_safety_state SET pause_active = 1 WHERE id = 1")
    real = S.OrderStore._ro

    class Proxy:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            if "pause_active" in sql:
                raise sqlite3.OperationalError("no such column (fixture)")
            return self.c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.c, name)
    monkeypatch.setattr(S.OrderStore, "_ro", lambda self: Proxy(real(self)))
    assert S.OrderStore().safety_flags() is None and S.OrderStore().stop_state() == "OFF"
    it = preview("AMD", "BUY", 1)
    assert "V19" not in rules(it) and it["confirmable"] is True
    stopped(gate)
    assert err(O.preview, "AMD", "BUY", 1).code == "STOP_BLOCKS_BUY"


def test_N38_a_list_of_more_than_200_orders_reads_the_armed_set_once(gate, monkeypatch):
    it = preview("AMD", "BUY", 1)
    p = RO.db_path()
    cols = [r["name"] for r in rows(p, "PRAGMA table_info(alpaca_paper_order_intents)") if r["name"] != "intent_id"]
    for _ in range(205):
        values = [RU.new_client_order_id() if c == "client_order_id" else "CONFIRM_REJECTED" if c == "state" else
                  RU.sha256_hex(RU.new_client_order_id()) if c == "preview_hash" else None for c in cols]
        src = rows(p, "SELECT * FROM alpaca_paper_order_intents WHERE intent_id = ?", (it["intent_id"],))[0]
        raw(p, f"INSERT INTO alpaca_paper_order_intents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(v if v is not None else src[c] for c, v in zip(cols, values)))
    opened, armed = [], []
    real_ro, real_armed = S.OrderStore._ro, S.OrderStore.armed_before_stop_ids
    monkeypatch.setattr(S.OrderStore, "_ro", lambda self: opened.append(1) or real_ro(self))
    monkeypatch.setattr(S.OrderStore, "armed_before_stop_ids", lambda self: armed.append(1) or real_armed(self))
    small = len(O.list_intents(limit=3)["intents"]), len(opened)
    opened.clear()
    big = len(O.list_intents(limit=250)["intents"]), len(opened)
    assert small[0] == 3 and big[0] == 206 and small[1] == big[1] and armed == [1, 1]


def test_N39_R5_the_bot_owned_warning_prints_with_the_stop_off_only(gate, monkeypatch):
    bot_position(db(gate), "AMD")
    it = preview("AMD", "SELL", 1)
    v20 = [r for r in it["rules"] if r["rule"] == "V20"]
    assert v20 == [{"rule": "V20", "ok": False, "code": "BOT_OWNED_SELL_WARNING", "text": RU.RULE_TEXT["BOT_OWNED_SELL_WARNING"], "info": True}]
    assert it["confirmable"] is True
    assert "V20" not in rules(preview("AMD", "BUY", 1))
    gate.set_position("KO", 2)
    assert "V20" not in rules(preview("KO", "SELL", 1))
    monkeypatch.setattr(S.OrderStore, "bot_owned", lambda self, conn, sym: None)              # an unreadable ownership read: no row, order unchanged
    it = preview("AMD", "SELL", 1)
    assert "V20" not in rules(it) and it["confirmable"] is True


# ==================================================================================================================================
# R1 - R18, R25: races on a real SQLite file (separate connections)
# ==================================================================================================================================

def _after_gates(monkeypatch, fn):
    real = O._fresh_gates

    def wrapped(*a, **k):
        out = real(*a, **k)
        fn()
        return out
    monkeypatch.setattr(O, "_fresh_gates", wrapped)
    return real


def test_R01_a_stop_committed_after_the_gates_refuses_a_buy_in_the_arm(gate, monkeypatch):
    i = preview("AMD", "BUY", 1)
    _after_gates(monkeypatch, lambda: stop_on(db(gate)))
    e = err(confirm, i)
    assert reason(e) == "EMERGENCY_STOP_ACTIVE" and gate.posts == [] and S.OrderStore().intent(i["intent_id"])["state"] == RU.CONFIRM_REJECTED


def test_R02_a_sell_classified_while_the_stop_was_off_is_refused_if_the_stop_commits_before_the_arm(gate, monkeypatch):
    i = preview("AMD", "SELL", 1)
    _after_gates(monkeypatch, lambda: stop_on(db(gate)))
    assert reason(err(confirm, i)) == "EMERGENCY_STOP_ACTIVE" and gate.posts == []


def test_R03_a_stop_committed_after_the_arm_lets_the_post_go_and_records_armed_before_the_stop(gate):
    i = preview("AMD", "BUY", 1)
    gate.on_post = lambda req: stop_on(db(gate))
    out = confirm(i)
    assert len(gate.posts) == 1 and out["intent"]["state"] == "SUBMITTED"
    assert out["intent"]["state_label"].endswith(" · armed before the stop")
    assert ev_codes(i["intent_id"]).count(RU.ARMED_BEFORE_STOP) == 1
    listed = {x["intent_id"]: x for x in O.list_intents()["intents"]}
    assert "armed before the stop" in listed[i["intent_id"]]["state_label"]


def number_commits(path):
    """R4 / R23: commits numbered by a trigger, written in the same transaction as the commit it numbers (a wrapper could misorder)."""
    for sql in ("CREATE TABLE IF NOT EXISTS t_seq (n INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT NOT NULL, ref TEXT)",
                "CREATE TRIGGER IF NOT EXISTS t_seq_kill AFTER UPDATE OF kill_active ON execution_safety_state WHEN new.kill_active = 1 AND old.kill_active = 0 "
                "BEGIN INSERT INTO t_seq (what, ref) VALUES ('KILL', NULL); END"):
        raw(path, sql)


def test_R04_threads_confirm_and_stop_200_interleavings_no_buy_post_armed_after_the_stop(gate, monkeypatch):
    monkeypatch.setattr(O, "_confirmed_today", lambda st, now: 0)                          # each trial resets the daily count (else DAILY_LIMIT at 20)
    p = db(gate)
    number_commits(p)
    raw(p, "CREATE TRIGGER IF NOT EXISTS t_seq_arm AFTER UPDATE OF state ON alpaca_paper_order_intents WHEN new.state = 'SUBMISSION_PENDING' "
           "AND old.state != 'SUBMISSION_PENDING' BEGIN INSERT INTO t_seq (what, ref) VALUES ('ARM', new.intent_id); END")
    rng = random.Random(4604)
    t0 = time.perf_counter()
    confirm(preview("AMD", "BUY", 1))
    dur = time.perf_counter() - t0
    outcomes = {"posted": 0, "refused": 0}
    last = rows(p, "SELECT coalesce(max(n), 0) AS n FROM t_seq")[0]["n"]
    for _ in range(200):
        stop_off(p)
        i = preview("AMD", "BUY", 1)
        n_posts = len(gate.posts)
        da, db_ = rng.uniform(0, dur * 0.3), rng.uniform(0, dur * 1.2)

        def a():
            time.sleep(da)
            try:
                confirm(i)
            except O.OrderError:
                pass

        def b():
            time.sleep(db_)
            stop_on(p)
        ts = [threading.Thread(target=a), threading.Thread(target=b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        seq = rows(p, "SELECT n, what, ref FROM t_seq WHERE n > ? ORDER BY n", (last,))
        last = seq[-1]["n"] if seq else last
        kill = [r["n"] for r in seq if r["what"] == "KILL"]
        arm = [r["n"] for r in seq if r["what"] == "ARM" and int(r["ref"]) == i["intent_id"]]
        posted = len(gate.posts) - n_posts
        assert len(kill) == 1 and posted == len(arm) <= 1
        if arm:
            assert arm[0] < kill[0]                                                         # every BUY POST's arm commit precedes KILL_ON
        outcomes["posted" if posted else "refused"] += 1
    assert outcomes["posted"] > 0 and outcomes["refused"] > 0, outcomes


def test_R05_a_hung_post_holding_the_lock_never_delays_the_stop_route(gate, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.routes.trader_view import stream
    from api.server import app
    from safety.models import AccountEquity
    from safety.service import ExecutionSafetyService
    s = ExecutionSafetyService(store=SafetyStore(tmp_path / "r05.db"), clock=lambda: NOW, startup_policy="KEEP", threshold_pct=5, max_age_s=900,
                               accept_unadjusted=False,
                               account_fn=lambda ts: AccountEquity(equity="100000.00", source="HYPOTHETICAL_REVIEW", as_of=None, age_s=None, mode="simulated",
                                                                   adjustments="NONE_POSSIBLE"))
    s.evaluate("TEST", NOW)
    monkeypatch.setattr(stream(), "safety", s)
    i = preview("AMD", "BUY", 1)
    entered, release = threading.Event(), threading.Event()
    gate.on_post = lambda req: (entered.set(), release.wait(30))
    t = threading.Thread(target=lambda: confirm(i))
    t.start()
    try:
        assert entered.wait(10)
        assert O._LOCK.acquire(blocking=False) is False                                    # the POST hangs with _LOCK held
        c = TestClient(app)
        out = {}
        r = threading.Thread(target=lambda: out.setdefault("r", c.post("/api/execution-safety/emergency-stop", json={},
                                                                        headers={"X-Stock-Agent-Intent": "dashboard"})))
        t0 = time.monotonic()
        r.start()
        r.join(10)
        dt, alive = time.monotonic() - t0, r.is_alive()
    finally:
        release.set()
        t.join(30)
    r.join(30)
    assert not alive and out["r"].status_code == 200 and dt < 10
    assert rows(tmp_path / "r05.db", "SELECT kill_active FROM execution_safety_state WHERE id = 1") == [{"kill_active": 1}]


def test_R06_a_stop_written_by_a_second_connection_only_is_seen(gate):
    raw(db(gate), "UPDATE execution_safety_state SET kill_active = 1 WHERE id = 1")
    assert err(O.preview, "AMD", "BUY", 1).code == "STOP_BLOCKS_BUY" and gate.requests == []


def test_R07_a_stop_then_clear_between_the_gates_and_the_arm_reads_off_and_proceeds(gate, monkeypatch):
    i = preview("AMD", "BUY", 1)
    _after_gates(monkeypatch, lambda: (stop_on(db(gate)), stop_off(db(gate))))
    assert confirm(i)["intent"]["state"] == "SUBMITTED" and len(gate.posts) == 1


def test_R08_a_retry_refused_by_the_stop_keeps_its_state_and_sends_nothing(gate, monkeypatch):
    gate.post_modes = ["connect_timeout"]
    it = confirm(preview("AMD", "BUY", 1))["intent"]
    assert it["state"] == "SUBMIT_NOT_SENT" and gate.posts == []
    real = _after_gates(monkeypatch, lambda: stop_on(db(gate)))
    e = err(O.retry, it["intent_id"], it["preview_hash"])
    assert e.code == "RETRY_REJECTED" and reason(e) == "EMERGENCY_STOP_ACTIVE" and gate.posts == []
    assert S.OrderStore().intent(it["intent_id"])["state"] == "SUBMIT_NOT_SENT"
    monkeypatch.setattr(O, "_fresh_gates", real)
    gate.requests.clear()
    e = err(O.retry, it["intent_id"], it["preview_hash"])                                   # the stop is on before the retry: 0 requests
    assert e.code == "RETRY_REJECTED" and reason(e) == "STOP_BLOCKS_BUY" and gets(gate) == [] and gate.posts == []
    assert S.OrderStore().intent(it["intent_id"])["state"] == "SUBMIT_NOT_SENT"


def test_R09_a_locked_database_at_the_arm_sends_nothing_and_the_stale_rule_resolves_it(gate, clock, monkeypatch):
    i = preview("AMD", "BUY", 1)
    real = S.OrderStore.write
    n = {"w": 0}

    def write(self):
        n["w"] += 1
        if n["w"] == 2:                                                                     # 1 = CONFIRMING, 2 = the arm
            raise sqlite3.OperationalError("database is locked")
        return real(self)
    monkeypatch.setattr(S.OrderStore, "write", write)
    with pytest.raises(sqlite3.OperationalError):
        confirm(i)
    monkeypatch.setattr(S.OrderStore, "write", real)
    assert gate.posts == [] and S.OrderStore().intent(i["intent_id"])["state"] == RU.CONFIRMING
    clock[0] += timedelta(seconds=RU.STALE_CONFIRMING_S + 1)
    O.list_intents()
    O._apply_stale(S.OrderStore())
    assert S.OrderStore().intent(i["intent_id"])["state"] == RU.CONFIRM_REJECTED and gate.posts == []


def test_R10_a_double_confirm_during_a_stop_makes_at_most_one_post(gate):
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    out = []

    def go():
        try:
            out.append(confirm(it))
        except O.OrderError as e:
            out.append(e)
    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(gate.posts) == 1


def _calls(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else "?")
    return out


def test_R11_no_network_call_inside_any_arm_transaction():
    tree = ast.parse((ROOT / "paper" / "alpaca_orders.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    allowed = {"write", "_now", "stop_state", "_arm_refusal", "transition", "event", "_iso"}
    for name in ("confirm", "retry"):
        blocks = [w for w in ast.walk(fns[name]) if isinstance(w, ast.With) and any(
            isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute) and i.context_expr.func.attr == "write" for i in w.items)]
        arms = [w for w in blocks if "stop_state" in _calls(w)]
        assert len(arms) == 1, name                                                         # the arm reads the latch in its transaction
        assert _calls(arms[0]) <= allowed, (name, _calls(arms[0]) - allowed)
    assert _calls(fns["_arm_refusal"]) <= {"bot_owned", "total_seconds"}
    xt = ast.parse((ROOT / "execution" / "store.py").read_text(encoding="utf-8"))
    rea = next(n for n in ast.walk(xt) if isinstance(n, ast.FunctionDef) and n.name == "record_entry_attempt")
    assert _calls(rea) <= {"_rw", "execute", "_latch", "_iso", "close", "ExecutionStoreError"}, _calls(rea)
    latch = next(n for n in ast.walk(xt) if isinstance(n, ast.FunctionDef) and n.name == "_latch")
    assert _calls(latch) <= {"execute", "fetchall", "len", "type"}
    src = (ROOT / "execution" / "store.py").read_text(encoding="utf-8")
    body = src[src.index("    def record_entry_attempt("):src.index("    def stop_state(self)")]
    assert body.index('conn.execute("BEGIN IMMEDIATE")') < body.index("self._latch(conn)") < body.index("UPDATE execution_intents")   # the latch read IS in the transaction
    po = (ROOT / "paper" / "alpaca_orders.py").read_text(encoding="utf-8")
    for name in ("def confirm(", "def retry("):
        f = po[po.index(name):]
        arm = f[f.index("why = _arm_refusal(") - 600:f.index("why = _arm_refusal(")]
        w = arm.rindex("with st.write() as c:")                                             # the arm's own transaction: the clock and the latch inside it
        assert "t = _now()" in arm[w:] and arm.index("t = _now()", w) < arm.index("stop = st.stop_state(c)", w), name


def test_R12_a_fill_between_the_open_orders_read_and_the_position_read_is_never_a_clean_reduction(gate):
    gate.set_position("AMD", 10)
    web = gate.open_order("AMD", "sell", 6)
    seen = []

    def hook(path, query):
        if path in ("/v2/orders", "/v2/positions/AMD"):
            seen.append(path)
            if len(seen) == 2:                                                              # between the two reads, whichever order they run in
                web["filled_qty"], web["status"] = "6", "filled"
                gate.set_position("AMD", 4)
    stopped(gate)
    gate.on_get = hook
    it = preview("AMD", "SELL", 4)
    assert seen == ["/v2/orders", "/v2/positions/AMD"]                                      # open orders FIRST
    assert rules(it)["V18"] == (False, "STOP_SELLS_EXCEED_POSITION")                       # double-counted: refused


def test_R13_R14_no_armed_before_the_stop_record_when_nothing_was_sent_or_alpaca_rejected(gate):
    for sym, mode, state in (("AMD", "connect_timeout", "SUBMIT_NOT_SENT"),
                             ("MU", 'http:403:{"code": 40310000, "message": "insufficient buying power"}', "BROKER_REJECTED")):
        stop_off(db(gate))
        i = preview(sym, "BUY", 1)
        gate.post_modes = [mode]
        gate.on_post = lambda req: stop_on(db(gate))
        out = confirm(i)
        assert out["intent"]["state"] == state and RU.ARMED_BEFORE_STOP not in ev_codes(i["intent_id"])
        assert "armed before the stop" not in out["intent"]["state_label"]
        gate.on_post = None


def test_R16_a_failed_record_write_after_settle_leaves_the_intent_settled(gate, monkeypatch, caplog):
    i = preview("AMD", "BUY", 1)
    gate.on_post = lambda req: stop_on(db(gate))
    real = S.OrderStore.event

    def event(*a, **k):
        if k.get("code") == RU.ARMED_BEFORE_STOP:
            raise sqlite3.OperationalError("disk full (fixture)")
        return real(*a, **k)
    monkeypatch.setattr(S.OrderStore, "event", staticmethod(event))
    out = confirm(i)
    assert out["intent"]["state"] == "SUBMITTED" and S.OrderStore().intent(i["intent_id"])["state"] == "SUBMITTED"
    assert "armed before the stop" not in out["intent"]["state_label"] and "ARMED_BEFORE_STOP not recorded" in caplog.text


def test_R17_C8_a_bot_entry_filling_between_the_classification_and_the_arm_is_refused_in_the_arm(gate, monkeypatch):
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    _after_gates(monkeypatch, lambda: bot_position(db(gate), "AMD"))
    assert reason(err(confirm, it)) == "STOP_BOT_OWNED" and gate.posts == []


@pytest.mark.parametrize("gone", ["paper_positions", "execution_intents"])
def test_R17b_C8_ownership_unreadable_between_the_classification_and_the_arm_is_refused_in_the_arm(gate, monkeypatch, gone):
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    _after_gates(monkeypatch, lambda: raw(db(gate), f"ALTER TABLE {gone} RENAME TO {gone}_gone"))
    e = err(confirm, it)
    assert reason(e) == "STOP_BOT_OWNED" and gate.posts == [] and S.OrderStore().intent(it["intent_id"])["state"] == RU.CONFIRM_REJECTED


@pytest.mark.parametrize("gone", ["paper_positions", "execution_intents"])
def test_N29b_C8_ownership_unreadable_at_confirm_is_refused_before_any_broker_read(gate, gone):
    stopped(gate)
    it = preview("AMD", "SELL", 1)
    raw(db(gate), f"ALTER TABLE {gone} RENAME TO {gone}_gone")
    gate.requests.clear()
    e = err(confirm, it)
    assert e.code == "CONFIRM_REJECTED" and reason(e) == "STOP_OWNERSHIP_UNKNOWN" and gets(gate) == [] and gate.posts == []


def test_R18_C2_a_latch_unreadable_between_the_gates_and_the_arm_refuses(gate, monkeypatch):
    i = preview("AMD", "BUY", 1)
    _after_gates(monkeypatch, lambda: latch_gone(db(gate)))
    assert reason(err(confirm, i)) == "EMERGENCY_STOP_STATE_UNKNOWN" and gate.posts == []


# ==================================================================================================================================
# R19 - R26: the Paper Auto entry attempt (C-7) on the Stage 6.0 stack and its simulated paper broker
# ==================================================================================================================================

def entry(st):
    return st.x.store.intents(intent_type="ENTRY")[0]


def test_R19_C7_a_stop_committed_after_the_recheck_refuses_the_entry_attempt(tmp_path, monkeypatch):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    real = st.x._recheck_entry

    def recheck(it, now):
        out = real(it, now)
        stop_on(st.db)
        return out
    monkeypatch.setattr(st.x, "_recheck_entry", recheck)
    X60.cyc(st)
    it = entry(st)
    assert st.sim.posts == [] and it.state == "NOT_SENT" and it.failure_code == "EMERGENCY_STOP_ACTIVE" and it.attempt_count == 0
    assert X60.ctl(st, "ENTRY_SUBMISSION")["last_result"] == "BLOCK" and "EMERGENCY_STOP_ACTIVE" in X60.ctl(st, "ENTRY_SUBMISSION")["last_reason"]


def test_R20_C7_a_stop_committed_during_a_slow_recheck_get_refuses_the_entry_attempt(tmp_path):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    real = st.x.broker.position
    made = {"intent": False}
    st.x.hooks["after_intent"] = lambda ctx: made.update(intent=True)

    def slow(sym):
        out = real(sym)
        if made["intent"]:
            stop_on(st.db)                                                                  # the last recheck GET: the safety read already passed
        return out
    st.x.broker.position = slow
    X60.cyc(st)
    it = entry(st)
    assert st.sim.posts == [] and it.state == "NOT_SENT" and it.failure_code == "EMERGENCY_STOP_ACTIVE"


def test_R21_C7_a_stop_committed_after_the_attempt_lets_the_post_go_with_the_note(tmp_path):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    st.x.hooks["after_attempt_recorded"] = lambda ctx: stop_on(st.db)
    X60.cyc(st)
    it = entry(st)
    c = X60.ctl(st, "ENTRY_SUBMISSION")
    assert len(st.sim.posts) == 1 and it.attempt_count == 1 and it.state in ("SUBMITTED", "FILLED", "PARTIALLY_FILLED")
    assert c["last_result"] == "RUN" and c["last_reason"].endswith("(armed before the stop)") and c["last_input"]["armed_before_stop"] is True


def test_R21b_C7_a_failing_note_read_after_the_post_still_records_the_outcome(tmp_path, monkeypatch):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    st.x.hooks["after_attempt_recorded"] = lambda ctx: stop_on(st.db)

    def boom(self):
        raise PermissionError("the database folder is not readable (fixture)")
    monkeypatch.setattr(ExecutionStore, "stop_state", boom)
    X60.cyc(st)
    it = entry(st)
    c = X60.ctl(st, "ENTRY_SUBMISSION")
    assert len(st.sim.posts) == 1 and it.state in ("SUBMITTED", "FILLED", "PARTIALLY_FILLED") and it.broker_order_id
    assert c["last_result"] == "RUN" and "armed before the stop" not in c["last_reason"] and "armed_before_stop" not in c["last_input"]


def test_R22_C7_an_exit_during_a_stop_is_posted_with_no_latch_read_and_todays_calls(tmp_path, monkeypatch):
    st = X60.make_stack(tmp_path)
    pv, pos = X60.entered(st)
    stop_on(st.db)
    calls = []
    for name in ("record_entry_attempt", "stop_state"):
        real = getattr(ExecutionStore, name)
        monkeypatch.setattr(ExecutionStore, name, (lambda r, nm: (lambda *a, **k: calls.append(nm) or r(*a, **k)))(real, name))
    real_latch = ExecutionStore._latch
    monkeypatch.setattr(ExecutionStore, "_latch", staticmethod(lambda conn: calls.append("_latch") or real_latch(conn)))
    upd = []
    real_upd = ExecutionStore.update_intent
    monkeypatch.setattr(ExecutionStore, "update_intent", lambda self, xid, now, **f: upd.append(sorted(f)) or real_upd(self, xid, now, **f))
    X60.below_invalidation(st, pv)
    out = X60.cyc(st)
    assert out["summary"]["exits_submitted"] == 1 and len(st.sim.posts) == 2 and json.loads(st.sim.posts[1])["side"] == "sell"
    assert calls == [] and ["attempt_count", "submitted_at"] in upd


def test_R23_C7_threads_entries_and_stop_200_interleavings_no_entry_post_after_the_stop(tmp_path):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    st.sim.auto_fill = "manual"
    number_commits(st.db)
    raw(st.db, "CREATE TRIGGER IF NOT EXISTS t_seq_attempt AFTER UPDATE OF attempt_count ON execution_intents WHEN new.attempt_count = 1 AND old.attempt_count = 0 "
               "BEGIN INSERT INTO t_seq (what, ref) VALUES ('ATTEMPT', new.execution_id); END")
    rng = random.Random(4623)
    now = st.clock.now
    outcomes = {"posted": 0, "refused": 0}
    last = 0

    def intent(k):
        xid = EN.execution_id_for(f"r23-proposal-{k}")
        return st.x.store.insert_intent(Intent(execution_id=xid, intent_type="ENTRY", symbol="SNDK", side="buy", qty=1, client_order_id=EN.entry_coid(xid),
                                               state="PENDING", created_at=now.isoformat(), updated_at=now.isoformat(), created_by="test"))
    t0 = time.perf_counter()
    st.x._submit(intent("warm"), now, "ENTRY_SUBMISSION", recheck=None)
    dur = time.perf_counter() - t0
    for k in range(200):
        stop_off(st.db)
        it = intent(k)
        n_posts = len(st.sim.posts)
        da, db_ = rng.uniform(0, dur * 0.3), rng.uniform(0, dur * 1.2)

        def a():
            time.sleep(da)
            st.x._submit(it, now, "ENTRY_SUBMISSION", recheck=None)

        def b():
            time.sleep(db_)
            stop_on(st.db)
        ts = [threading.Thread(target=a), threading.Thread(target=b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        seq = rows(st.db, "SELECT n, what, ref FROM t_seq WHERE n > ? ORDER BY n", (last,))
        last = seq[-1]["n"] if seq else last
        kill = [r["n"] for r in seq if r["what"] == "KILL"]
        att = [r["n"] for r in seq if r["what"] == "ATTEMPT" and r["ref"] == it.execution_id]
        posted = len(st.sim.posts) - n_posts
        assert len(kill) == 1 and posted == len(att) <= 1, (seq, posted)
        if att:
            assert att[0] < kill[0]                                                         # no entry POST whose attempt commit follows KILL_ON
        else:
            assert st.x.store.intent(it.execution_id).failure_code == "EMERGENCY_STOP_ACTIVE"
        outcomes["posted" if posted else "refused"] += 1
    assert outcomes["posted"] > 0 and outcomes["refused"] > 0, outcomes


@pytest.mark.parametrize("how", ["table", "row"])
def test_R24_C7_an_unreadable_latch_at_the_attempt_refuses_the_entry(tmp_path, monkeypatch, how):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    real = st.x._recheck_entry

    def recheck(it, now):
        out = real(it, now)
        if how == "table":
            latch_gone(st.db)
        else:
            raw(st.db, "DROP TRIGGER execution_safety_state_no_delete")
            raw(st.db, "DELETE FROM execution_safety_state")
        return out
    monkeypatch.setattr(st.x, "_recheck_entry", recheck)
    X60.cyc(st)
    it = entry(st)
    assert st.sim.posts == [] and it.state == "NOT_SENT" and it.failure_code == "EMERGENCY_STOP_STATE_UNKNOWN" and it.attempt_count == 0


def _shared(monkeypatch, broker, tmp_path):
    """One database for both paths (the real deployment): the 4.6B store on the Stage 6.0 stack's file."""
    st = X60.make_stack(tmp_path)
    monkeypatch.setattr(RO, "db_path", lambda: Path(st.db))
    return st


def test_R25_one_manual_order_and_one_entry_in_flight_are_all_that_reach_a_broker(broker, tmp_path, monkeypatch):
    st = _shared(monkeypatch, broker, tmp_path)
    X60.arm(st)
    X60.go_ready(st, "NVDA")                                                                # a second READY symbol for after the stop
    ready(broker)
    i = preview("AMD", "BUY", 1)
    m_in, e_in, release = threading.Event(), threading.Event(), threading.Event()
    broker.on_post = lambda req: (m_in.set(), release.wait(30))
    st.sim.on_post = lambda req: (e_in.set(), release.wait(30))
    tm = threading.Thread(target=lambda: confirm(i))
    te = threading.Thread(target=lambda: X60.cyc(st))
    tm.start()
    te.start()
    assert m_in.wait(10) and e_in.wait(10)
    stop_on(st.db)
    release.set()
    tm.join(30)
    te.join(30)
    broker.on_post = st.sim.on_post = None
    assert err(O.preview, "AMD", "BUY", 1).code == "STOP_BLOCKS_BUY"
    X60.cyc(st)
    X60.cyc(st)
    assert len(broker.posts) == 1 and len(st.sim.posts) == 1
    assert len(st.x.store.intents(intent_type="ENTRY")) == 1


def test_R26_a_clear_during_the_recheck_proceeds_only_if_every_other_check_passes(tmp_path):
    st = X60.make_stack(tmp_path)
    X60.arm(st)
    made = {"intent": False}
    st.x.hooks["after_intent"] = lambda ctx: (stop_on(st.db), made.update(intent=True))     # the recheck's safety read sees the stop
    real = st.x.broker.position
    st.x.broker.position = lambda sym: ((stop_off(st.db) if made["intent"] else None), real(sym))[1]   # Clear during the last recheck GET
    X60.cyc(st)
    it = entry(st)
    assert st.sim.posts == [] and it.state == "NOT_SENT" and it.failure_code == "PRECHECK_FAILED" and "safety gate" in it.failure_text
    st2 = X60.make_stack(tmp_path / "b")
    X60.arm(st2)
    real2 = st2.x.broker.position
    made2 = {"intent": False}
    st2.x.hooks["after_intent"] = lambda ctx: made2.update(intent=True)
    st2.x.broker.position = lambda sym: ((stop_on(st2.db), stop_off(st2.db)) if made2["intent"] else None, real2(sym))[1]   # both after the safety read
    X60.cyc(st2)
    assert len(st2.sim.posts) == 1 and entry(st2).attempt_count == 1


# ==================================================================================================================================
# P1 - P9: partial fills
# ==================================================================================================================================

def test_P01_an_own_buy_filling_at_the_stop_blocks_a_sell_on_row12_until_it_finishes(gate):
    gate.set_position("AMD", 10)
    b = confirm(preview("AMD", "BUY", 2))["intent"]
    gate.set_status(b["client_order_id"], "partially_filled", filled_qty=1, move_position=True)
    stopped(gate)
    assert rules(preview("AMD", "SELL", 3))["V18"] == (False, "STOP_OPEN_ORDER_CONFLICT")
    gate.set_status(b["client_order_id"], "filled", filled_qty=2, move_position=True)
    it = preview("AMD", "SELL", 3)
    assert rules(it)["V18"] == (True, None) and confirm(it)["intent"]["state"] == "SUBMITTED" and len(gate.posts) == 2


def test_P02_an_own_buy_armed_before_the_stop_keeps_its_record_and_check_status_tracks_its_fills(gate, clock):
    i = preview("AMD", "BUY", 3)
    gate.on_post = lambda req: stop_on(db(gate))
    out = confirm(i)
    gate.on_post = None
    assert "armed before the stop" in out["intent"]["state_label"]
    gate.set_status(i["client_order_id"], "partially_filled", filled_qty=1, filled_avg_price="101", move_position=True)
    clock[0] += timedelta(seconds=30)
    res = O.check_status()
    row = next(x for x in res["intents"] if x["intent_id"] == i["intent_id"])
    assert row["state"] == "PARTIALLY_FILLED" and "armed before the stop" in row["state_label"] and len(gate.posts) == 1


def test_P03_an_own_sell_partly_filled_leaves_qty_minus_filled_resting(gate):
    stopped(gate)
    s1 = confirm(preview("AMD", "SELL", 6))["intent"]
    gate.set_status(s1["client_order_id"], "partially_filled", filled_qty=2, move_position=True)   # held 8, resting 4
    assert rules(preview("AMD", "SELL", 4))["V18"] == (True, None)
    assert rules(preview("AMD", "SELL", 5))["V18"] == (False, "STOP_SELLS_EXCEED_POSITION")
    gate.set_position("AMD", 8, available=4)                                                # Alpaca holds the resting 4
    assert rules(preview("AMD", "SELL", 5))["V18"] == (False, "STOP_SELL_EXCEEDS_AVAILABLE")


def test_P04_P05_bot_fills_continue_at_the_stop_and_a_manual_sell_is_refused(broker, tmp_path, monkeypatch):
    st = _shared(monkeypatch, broker, tmp_path)
    pv = X60.arm(st)
    ready(broker)
    st.sim.auto_fill = "manual"
    X60.cyc(st)
    coid = entry(st).client_order_id
    st.sim.fill(coid, 2)
    stop_on(st.db)
    X60.cyc(st)
    pos = st.x.store.open_position("SNDK")
    assert pos is not None and pos.quantity == 2
    broker.requests.clear()
    assert err(O.preview, "SNDK", "SELL", 1).code == "STOP_BOT_OWNED" and broker.requests == []
    st.sim.fill(coid)
    X60.cyc(st)
    full = st.x.store.open_position("SNDK")
    assert full.quantity == entry(st).qty                                                  # P4: bot fills continue during the stop
    X60.below_invalidation(st, pv)
    X60.cyc(st)
    ex = st.x.store.intents(intent_type="EXIT")[0]
    st.sim.fill(ex.client_order_id, 1, 95.0)
    X60.cyc(st)
    assert st.x.store.position(full.position_id).quantity == full.quantity - 1             # P5: the exit keeps filling
    assert err(O.preview, "SNDK", "SELL", 1).code == "STOP_BOT_OWNED" and broker.posts == []


def test_P06_a_paper_auto_entry_attempted_before_the_stop_fills_after_it_and_stays_monitored(tmp_path):
    st = X60.make_stack(tmp_path)
    pv = X60.arm(st)
    st.sim.auto_fill = "manual"
    X60.cyc(st)
    coid = entry(st).client_order_id
    stop_on(st.db)
    st.sim.fill(coid, 1)
    X60.cyc(st)
    assert st.x.store.open_position("SNDK").quantity == 1
    st.sim.fill(coid)
    X60.cyc(st)
    assert st.x.store.open_position("SNDK").quantity == pv.final_qty
    X60.below_invalidation(st, pv)
    out = X60.cyc(st)
    assert out["summary"]["exits_submitted"] == 1 and len(st.sim.posts) == 2


def test_P07_a_website_sell_partly_filled_then_a_manual_sell(gate):
    gate.set_position("AMD", 10, available=6)
    gate.open_order("AMD", "sell", 6, filled_qty=2, status="partially_filled")              # resting 4
    stopped(gate)
    assert rules(preview("AMD", "SELL", 6))["V18"] == (True, None)                         # 4 + 6 <= 10
    assert rules(preview("AMD", "SELL", 7))["V18"] == (False, "STOP_SELL_EXCEEDS_AVAILABLE")
    gate.set_position("AMD", 10)                                                            # available not yet reflected
    assert rules(preview("AMD", "SELL", 7))["V18"] == (False, "STOP_SELLS_EXCEED_POSITION")
    gate.extra_open_orders[0]["order_class"] = "bracket"
    assert rules(preview("AMD", "SELL", 1))["V18"] == (False, "STOP_OPEN_ORDER_CONFLICT")


def test_P08_a_fill_between_preview_and_confirm_is_decided_on_the_fresh_reads(gate):
    stopped(gate)
    it = preview("AMD", "SELL", 8)
    assert it["confirmable"] is True
    gate.set_position("AMD", 5)                                                             # sold on Alpaca's website meanwhile
    e = err(confirm, it)
    assert e.code == "CONFIRM_REJECTED" and reason(e) in ("INSUFFICIENT_QTY_AVAILABLE", "STOP_SELL_EXCEEDS_AVAILABLE") and gate.posts == []


def test_P09_a_fractional_remainder_keeps_whole_share_rules_and_never_shorts(gate):
    gate.set_position("AMD", "10.5")
    stopped(gate)
    it = preview("AMD", "SELL", 10)
    assert rules(it)["V18"] == (True, None)
    out = confirm(it)["intent"]
    gate.set_status(out["client_order_id"], "filled", filled_qty=10, move_position=True)
    assert gate.positions["AMD"]["qty"] == "0.5"
    it = preview("AMD", "SELL", 1)
    assert rules(it)["V18"] == (False, "STOP_SELL_EXCEEDS_AVAILABLE") and it["confirmable"] is False


# ==================================================================================================================================
# S: static pins
# ==================================================================================================================================

def test_S01_the_stop_route_never_takes_the_manual_order_lock_and_the_gate_imports_nothing_from_safety_or_execution():
    route = (ROOT / "api" / "routes" / "execution_safety.py").read_text(encoding="utf-8")
    assert "_LOCK" not in route and "alpaca_orders" not in route
    for f in ("paper/alpaca_order_store.py", "paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_reads.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and (getattr(n, "module", "") or "").split(".")[0] in ("safety", "execution")
                       for n in ast.walk(ast.parse(src))), f
    xs = ast.parse((ROOT / "execution" / "store.py").read_text(encoding="utf-8"))
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and "safety" in ((getattr(n, "module", "") or "") + " ".join(a.name for a in n.names))
                   for n in ast.walk(xs))


def test_S02_the_writer_routes_migrations_and_the_pane_are_unchanged_by_the_gate():
    import subprocess
    base = "0182428077926a077a7fa94d55814fc6cc201306"
    try:
        subprocess.run(["git", "cat-file", "-e", base], cwd=str(ROOT), check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("the gate's base commit is not in this clone")
    for f in ("paper/alpaca_order_writer.py", "api/routes/alpaca_paper_orders.py", "database/alpaca_order_migrations.py", "frontend/alpaca_orders.js",
              "database/execution_migrations.py", "database/execution_safety_migrations.py", "execution/broker.py"):
        old = subprocess.run(["git", "show", f"{base}:{f}"], cwd=str(ROOT), check=True, capture_output=True).stdout
        assert (ROOT / f).read_bytes().replace(b"\r\n", b"\n") == old.replace(b"\r\n", b"\n"), f
