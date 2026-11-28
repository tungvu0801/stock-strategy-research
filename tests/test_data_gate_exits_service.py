"""
HDR-2 (EXECUTION_DATA_GATE_SPEC_r14) stage EXEC-C1c1: the positions step on a real-flag broker (exits). Service tests through the REAL seams:
the real ExecutionService / ExecutionStore, the real AlpacaPaperBroker client with simulated=False over the deterministic simulated wire
(execution/simulated.py; no socket, no credential: a stub key), a spy that counts every broker read and can inject a fault per call, a stub
stream whose market mode is `real`, and a temporary SQLite file per test. No network, no real order, no data/stock_agent.db.

Covered (class S, stage-local): r14 tests 4-11, 13, 17, the service part of 18, 22, 25-28, 30, 32, 33, 36, the service-cycle variants of 19, 21,
29 and 31, and the pins required by AUTH EXEC_4 (OD-C-7(a) exhaustion, the DG-final session failure on the last cycle, an unknown outcome, the
exhausted-HIT residual, the service side of the A1 truth table). DEFERRED to C1c2 (not claimed): every entry-side assertion (E10 refusals,
`protective_exit_problems`, DATA_GATE rows, `_attention()` / card surfaces, the latch), the OD-R4-2(a) verification probe and the OD-R6-2(a)
startup probe.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from execution import broker as BK
from execution import data_gate as DG
from execution.engine import exit_coid, exit_id_for
from execution.models import Intent
from execution.service import ExecutionService, stop_price_problem
from execution.simulated import SimulatedPaperBroker
from execution.store import ExecutionStore
from paper import alpaca_order_rules as RU

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)                         # Tuesday 10:00 New York (EST), a covered session
STOP, PROT, HELD = "STOP_CHECK_BLIND", "PROTECTIVE_EXIT_UNVERIFIED", "STRATEGY_EXIT_HELD"
LIFE = {STOP, PROT, HELD, STOP + "_RESOLVED", PROT + "_RESOLVED", HELD + "_RESOLVED"}
IDENT = {"account_number_masked": RU.mask("PA6SIM0000060"), "host": BK.PAPER_HOST, "label": "ALPACA_PAPER"}


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


# ================================================================ the rig =====================================================================
class Spy(BK.AlpacaPaperBroker):
    """The real paper client (simulated=False) over the simulated wire. `faults[name]`: a list consumed one per call (None = the real read,
    an exception = raised, a callable = its result), or one exception / callable applied on every call. Reconcile's orders("open") (no symbols)
    and positions() are never faulted."""

    def __init__(self, sim):
        super().__init__(credentials_fn=lambda: ("stub-key", "stub-secret"), wire=lambda: sim, simulated=False)
        self.calls, self.faults = [], {}

    def _go(self, name, real, *a):
        self.calls.append(name)
        f = self.faults.get(name)
        if isinstance(f, list):
            f = f.pop(0) if f else None
        if isinstance(f, BaseException):
            raise f
        if callable(f):
            return f(*a)
        return real(*a)

    def clock(self):
        return self._go("clock", super().clock)

    def environment(self, now=None):
        return self._go("environment", super().environment, now)

    def position(self, symbol):
        return self._go("position", super().position, symbol)

    def orders(self, status="open", symbols=None, limit=200):
        if not symbols:
            return super().orders(status, symbols, limit)
        return self._go("orders", super().orders, status, symbols, limit)

    def order_by_client_id(self, client_order_id):
        return self._go("order_by_client_id", super().order_by_client_id, client_order_id)

    def n(self, name):
        return self.calls.count(name)


class Market:
    def __init__(self):
        self.mode, self.quotes, self.bars = "real", {}, {}

    def quote(self, sym):
        q = self.quotes.get(sym)
        if isinstance(q, BaseException):
            raise q
        if callable(q):
            return q()
        return q if q is not None else SimpleNamespace(status="UNAVAILABLE", price=None, ts=None)


class Setups:
    def __init__(self):
        self.inputs, self.ep, self.store = None, {}, self

    def current(self, sym):
        return SimpleNamespace(inputs=self.inputs, state="READY")

    def episodes(self, sym, n):
        return [{"setup_id": "setup-" + sym, "state": "READY", "closed_at": None, "invalidation_reason": None, **self.ep.get(sym, {})}]

    def open_episode(self, sym):
        return None


class Rig:
    def __init__(self, tmp_path, t0=T0, simulated=False, broker_cls=None):
        self.t = t0
        self.sim = SimulatedPaperBroker(now_fn=lambda: self.t)
        self.sim.auto_fill = "manual"
        self.broker = BK.AlpacaPaperBroker(simulated=True, wire=lambda: self.sim) if simulated else (broker_cls or Spy)(self.sim)
        self.market, self.setups = Market(), Setups()
        self.stream = SimpleNamespace(market=self.market, candles=self._candles, setups=self.setups)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.db = tmp_path / "c1c1.db"
        self.store = ExecutionStore(self.db)
        self.store.ensure()
        self.x = ExecutionService(lambda: self.stream, self.store, broker=self.broker, clock=lambda: self.t, owner="test-owner")
        self.k = 0

    def _candles(self, sym):
        v = self.market.bars.get(sym, [])
        if isinstance(v, BaseException):
            raise v
        return v

    def live(self, sym, px, age=0):
        """A real quote stamped `age` seconds before the rig's clock at every read."""
        self.market.quotes[sym] = lambda: SimpleNamespace(status="OK", price=float(px), ts=int(self.t.timestamp()) - age)

    def open(self, sym="SNDK", qty=10, inv="9.00", at=None, ident=None):
        self.k += 1
        xid = f"{self.k:032d}"
        when = at or self.t
        self.store.insert_intent(Intent(execution_id=xid, intent_type="ENTRY", symbol=sym, side="buy", qty=qty, client_order_id="stockagent-entry-" + xid, setup_id="setup-" + sym,
                                        created_at=iso(when), updated_at=iso(when), attempt_count=1, submitted_at=iso(when), state="SUBMITTED", invalidation_price=inv,
                                        environment=dict(IDENT if ident is None else ident)))
        o = {"id": f"bbbbbbbb-0000-4000-8000-{self.k:012d}", "client_order_id": "stockagent-entry-" + xid, "symbol": sym, "side": "buy", "qty": str(qty), "filled_qty": qty,
             "filled_avg_price": "10.00", "status": "filled", "raw": {}}
        self.x._apply_order(self.store.intent(xid), o, when, "monitor")
        self.sim.set_position(sym, qty, 10.0)
        return self.store.position_for_execution(xid)

    def cycle(self, adv=30):
        self.t += timedelta(seconds=adv)
        return self.x.run_cycle(self.t, "TEST")

    def pos(self, p):
        return self.store.position(p.position_id)

    def recs(self, kinds=None, pid=None):
        c = sqlite3.connect(str(self.db))
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute("SELECT rowid AS rid, * FROM execution_reconciliations ORDER BY rowid")]
        finally:
            c.close()
        for r in rows:
            r["detail"] = json.loads(r["detail_json"]) if r.get("detail_json") else None
        return [r for r in rows if (kinds is None or r["kind"] in kinds) and (pid is None or r["position_id"] == pid)]

    def life(self, fam, pid=None):
        return self.recs({fam, fam + "_RESOLVED"}, pid)

    def state(self, fam, p):
        return self.store.safety_states([p.position_id])[(fam, p.position_id)]["state"]

    def ctl(self, cid):
        return {c["control_id"]: c for c in self.store.controls()}.get(cid)

    def posts(self):
        return len(self.sim.posts)

    def exits(self, p):
        return [i for i in self.store.intents(intent_type="EXIT", limit=500) if i.position_id == p.position_id]

    def events(self, p, kind=None):
        return [e for e in self.store.position_events(p.position_id, limit=500) if kind is None or e["kind"] == kind]


def norm_pos(sym, qty, side="long", avail=None):
    raw = {"symbol": sym, "qty": str(qty), "qty_available": str(qty if avail is None else avail), "avg_entry_price": "10.00"}
    if side is not None:
        raw["side"] = side
    return lambda *_a: BK.norm_position(raw)


def an_order(sym, side, coid="manual-1"):
    return BK.norm_order({"id": str(uuid.uuid4()), "client_order_id": coid, "symbol": sym, "side": side, "qty": "1", "filled_qty": "0", "status": "new"})


def clock_answer(rig, ts="now", is_open=True, skew=0):
    def f(*_a):
        stamp = iso(rig.t + timedelta(seconds=skew)) if ts == "now" else ts
        return {"timestamp": stamp, "is_open": is_open, "next_open": None, "next_close": None}
    return f


def hit_rig(tmp_path, **kw):
    rig = Rig(tmp_path, **kw)
    p = rig.open()
    rig.live("SNDK", "8.50")                                                    # a fresh real breach of the 9.00 level
    return rig, p


def codes_of(row):
    return (row or {}).get("detail", {}).get("reason_codes")


# ================================================================ r14 test 5 (H-1) and 6 (M-1) ================================================
@pytest.mark.parametrize("variant", ["missing", "stale_quote_above", "minute_close_901"])
def test_r14_05_h1_blind_market_open_one_active_row_wave1_row_and_attention(tmp_path, variant):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), "invalidation_reason": "DEMAND_COLLAPSED"}   # held EXIT_REQUIRED for DEMAND_FAILURE
    if variant == "stale_quote_above":
        rig.live("SNDK", "12.00", age=301)
    for _ in range(3):
        if variant == "minute_close_901":
            start = int(rig.t.timestamp()) + 30 - 901
            rig.market.bars["SNDK"] = [SimpleNamespace(time=start, close=12.0, final=True)]
        rig.cycle()
        assert "SNDK" in rig.x.stop_check_problems                                   # the Wave 1 entry block (row and recheck) and E10(i)'s input
        assert any("stop check could not run" in r for r in rig.x._attention())       # Wave 1's _attention() listing (unchanged)
        assert rig.ctl("EXIT_TRIGGER")["last_result"] == "UNKNOWN"
    rows = rig.life(STOP, p.position_id)
    assert len(rows) == 1 and rows[0]["kind"] == STOP and rows[0]["detail"]["lifecycle"] == "ACTIVE" and rows[0]["blocking"] == 1
    assert rig.posts() == 0
    assert rig.pos(p).state == "EXIT_REQUIRED" and rig.pos(p).exit_reason == "DEMAND_FAILURE"


def test_r14_05_h1_market_closed_warn_only_no_row(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.sim.is_open = False
    for _ in range(3):
        rig.cycle()
        assert rig.ctl("EXIT_TRIGGER")["last_result"] == "WARN" and rig.x.stop_check_problems == {}
    assert rig.life(STOP) == [] and rig.posts() == 0


@pytest.mark.parametrize("mode", ["simulated", "test", "replay", "", "RAISE"])
def test_r14_06_m1_feed_not_real_is_blind_in_every_cycle_whatever_the_price(tmp_path, mode):
    rig, p = hit_rig(tmp_path)
    if mode == "RAISE":
        class Boom:
            @property
            def mode(self):
                raise RuntimeError("market unreadable")

            def quote(self, sym):
                return SimpleNamespace(status="OK", price=8.5, ts=int(rig.t.timestamp()))
        rig.stream.market = Boom()
    else:
        rig.market.mode = mode
    for _ in range(3):
        rig.cycle()
        assert rig.ctl("EXIT_TRIGGER")["last_result"] == "UNKNOWN"
        assert "SNDK" in rig.x.stop_check_problems and "FEED_NOT_REAL" in rig.x.stop_check_problems["SNDK"]
    assert rig.posts() == 0 and rig.exits(p) == []
    assert codes_of(rig.life(STOP)[0]) == ["FEED_NOT_REAL"] and len(rig.life(STOP)) == 1


# ================================================================ r14 test 7 (M-3) ============================================================
def test_r14_07_m3_demand_failure_then_time_exit_reached_sends_time_exit(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open(at=datetime(2026, 11, 2, 15, 0, tzinfo=UTC))                       # 6 covered sessions before 2026-11-10
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), "invalidation_reason": "DEMAND_COLLAPSED"}
    rig.cycle()
    xs = rig.exits(p)
    assert rig.posts() == 1 and len(xs) == 1 and xs[0].exit_reason == "TIME_EXIT"
    assert rig.pos(p).exit_reason == "TIME_EXIT" and rig.events(p, "EXIT_INTENT")[-1]["reason"] == "TIME_EXIT"


def test_r14_07_m3_setup_invalidated_with_time_exit_not_reached_is_held(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open(at=datetime(2026, 11, 9, 15, 0, tzinfo=UTC))
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0)}                                    # SETUP_INVALIDATED
    rig.cycle()
    assert rig.posts() == 0 and rig.exits(p) == []
    held = rig.life(HELD, p.position_id)
    assert len(held) == 1 and held[0]["blocking"] == 0 and codes_of(held[0]) == ["DG_UNVERIFIED"]
    assert rig.ctl("EXIT_SUBMISSION")["last_result"] == "WARN"


def test_r14_07_m3_hit_with_a_strategy_candidate_takes_the_protective_path(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), "invalidation_reason": "DEMAND_COLLAPSED"}
    rig.cycle()
    xs = rig.exits(p)
    assert rig.posts() == 1 and xs[0].exit_reason == "HARD_INVALIDATION" and rig.pos(p).exit_reason == "HARD_INVALIDATION"


@pytest.mark.parametrize("variant", ["yesterday", "undated"])
def test_r14_07_m3_engine_breach_on_a_stale_or_undated_price_is_blind_never_a_sell(tmp_path, variant):
    rig = Rig(tmp_path)
    p = rig.open()
    if variant == "yesterday":
        rig.live("SNDK", "8.50", age=20 * 3600)                                       # before 09:30 of the session date: outside OD-R2-1(b)
    else:
        rig.market.quotes["SNDK"] = SimpleNamespace(status="OK", price=8.5, ts=None)
    rig.cycle()
    assert rig.posts() == 0 and rig.exits(p) == [] and "SNDK" in rig.x.stop_check_problems
    assert rig.life(PROT) == [] and len(rig.life(STOP)) == 1


# ================================================================ r14 test 8: restart with a HIT stop ========================================
def test_r14_08_restart_fresh_service_hit_verified_exactly_one_post_with_a_fresh_environment_read(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.x = ExecutionService(lambda: rig.stream, ExecutionStore(rig.db), broker=rig.broker, clock=lambda: rig.t, owner="new-process")
    rig.cycle()
    assert rig.posts() == 1 and rig.broker.n("environment") == 2                      # exit gate + DG-final, each fresh
    assert rig.x.environment_cache == {} and rig.x._env_at is None                    # the volatile cache was never the source


def test_r14_08_restart_with_the_environment_read_failing_zero_posts_and_the_critical_alert(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.x = ExecutionService(lambda: rig.stream, ExecutionStore(rig.db), broker=rig.broker, clock=lambda: rig.t, owner="new-process")
    rig.broker.faults["environment"] = BK.BrokerError("CONNECTION")
    rig.cycle()
    assert rig.posts() == 0
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["blocking"] == 1 and "P1" in codes_of(rows[0])
    assert rig.ctl("EXIT_SUBMISSION")["last_result"] == "BLOCK"


def test_r14_08_restart_on_a_simulated_broker_is_today_s_single_post(tmp_path):
    rig = Rig(tmp_path, simulated=True)
    p = rig.open()
    rig.live("SNDK", "8.50")
    rig.x = ExecutionService(lambda: rig.stream, ExecutionStore(rig.db), broker=rig.broker, clock=lambda: rig.t, owner="new-process")
    rig.cycle()
    assert rig.posts() == 1 and rig.recs(LIFE) == []                                 # r14 3.11: nothing recorded on a simulated broker


# ================================================================ r14 tests 9 and 25: the protective facts, P8 classification ================
REFUSALS = {
    "side_short": (lambda r: r.broker.faults.__setitem__("position", norm_pos("SNDK", 10, "short")), "P3_SIDE"),
    "p4_fractional_short": (lambda r: r.broker.faults.__setitem__("position", norm_pos("SNDK", 10, avail="9.5")), "P4_AVAILABLE"),
    "p4_unparsable": (lambda r: r.broker.faults.__setitem__("position", norm_pos("SNDK", 10, avail="abc")), "P4_UNPARSABLE"),
    "open_sell": (lambda r: r.broker.faults.__setitem__("orders", lambda *a: [an_order("SNDK", "sell")]), "P6"),
    "open_buy": (lambda r: r.broker.faults.__setitem__("orders", lambda *a: [an_order("SNDK", "buy")]), "P6"),
    "other_origin": (lambda r: r.broker.faults.__setitem__("orders", lambda *a: [an_order("SNDK", "sell", "stockagent-exit-foreign-9")]), "P6"),
    "orders_unreadable": (lambda r: r.broker.faults.__setitem__("orders", BK.BrokerError("TIMEOUT")), "P6"),
    "position_unreadable": (lambda r: r.broker.faults.__setitem__("position", BK.BrokerError("TIMEOUT")), "P3"),
    "identity_mask_broker_side": (lambda r: r.broker.faults.__setitem__("environment", lambda now: {**BK.AlpacaPaperBroker.environment(r.broker, now),
                                                                                                  "account_number_masked": "••••"}), "P2"),
    "account_blocked": (lambda r: r.sim.account.__setitem__("trading_blocked", True), "P1"),
    "account_not_active": (lambda r: r.sim.account.__setitem__("status", "ACCOUNT_CLOSED"), "P1"),
    "clock_unreadable": (lambda r: r.broker.faults.__setitem__("clock", [None, BK.BrokerError("TIMEOUT")]), "P8_CLOCK_UNREADABLE"),
    "clock_undated": (lambda r: r.broker.faults.__setitem__("clock", [None, clock_answer(r, ts=None)]), "P8_CLOCK_UNDATED"),
    "clock_naive": (lambda r: r.broker.faults.__setitem__("clock", [None, clock_answer(r, ts="2026-11-10T15:00:30")]), "P8_CLOCK_NAIVE"),
    "clock_skew_61": (lambda r: r.broker.faults.__setitem__("clock", [None, clock_answer(r, skew=61)]), "P8_CLOCK_SKEW"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_r14_09_25_protective_refusals_zero_posts_critical_alert_no_attention_no_cycle(tmp_path, case):
    rig, p = hit_rig(tmp_path)
    setup, code = REFUSALS[case]
    setup(rig)
    rig.cycle()
    assert rig.posts() == 0 and rig.exits(p) == []
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and code in codes_of(rows[0]) and rows[0]["detail"]["exit_cycle"] == 0
    blk = rig.ctl("EXIT_SUBMISSION")
    assert blk["last_result"] == "BLOCK" and code in blk["last_input"]["codes"]
    q = rig.pos(p)
    assert q.state == "EXIT_REQUIRED" and q.exit_cycle == 0 and q.attention_reason is None


@pytest.mark.parametrize("ident", [dict(IDENT, account_number_masked="••••9999"), dict(IDENT, account_number_masked="••••"), {}])
def test_r14_09_identity_differs_placeholder_or_empty_entry_identity_is_p2(tmp_path, ident):
    rig = Rig(tmp_path)
    p = rig.open(ident=ident)
    rig.live("SNDK", "8.50")
    rig.cycle()
    assert rig.posts() == 0 and "P2" in codes_of(rig.life(PROT, p.position_id)[0])


def test_r14_09_entry_intent_link_missing_is_p2(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    real = rig.store.intent
    monkeypatch.setattr(rig.store, "intent", lambda xid: None if xid == p.execution_id else real(xid))
    rig.cycle()
    assert rig.posts() == 0 and "P2" in codes_of(rig.life(PROT, p.position_id)[0])


def test_r14_09_fractional_qty_available_at_least_the_order_is_allowed(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["position"] = norm_pos("SNDK", 10, avail="10.5")
    rig.cycle()
    assert rig.posts() == 1 and rig.life(PROT) == []


def test_r14_09_p4_short_does_not_flap_across_five_cycles_with_reconciliation_running(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["position"] = norm_pos("SNDK", 10, avail="9.5")
    for _ in range(5):
        out = rig.cycle()
        assert out["summary"]["findings"] == 0 and rig.pos(p).state == "EXIT_REQUIRED" and rig.ctl("EXIT_SUBMISSION")["last_result"] == "BLOCK"
    assert len(rig.life(PROT, p.position_id)) == 1 and rig.posts() == 0
    assert rig.recs({"QTY_MISMATCH", "BROKER_FLAT_INTERNAL_OPEN"}) == []


@pytest.mark.parametrize("raw", [("-10", "long"), ("5", "long"), ("-3", None)])
def test_r14_09_21_qty_disagreement_is_3_5_2_lifecycle_row_before_the_qty_finding(tmp_path, raw):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["position"] = norm_pos("SNDK", raw[0], raw[1], avail=raw[0])
    rig.cycle()
    q = rig.pos(p)
    assert rig.posts() == 0 and q.state == "ATTENTION" and q.attention_reason == "QTY_MISMATCH"
    mine = rig.recs(pid=p.position_id)
    kinds = [r["kind"] for r in mine]
    assert kinds.count(PROT) == 1 and kinds.count("QTY_MISMATCH") == 1
    assert kinds.index(PROT) < kinds.index("QTY_MISMATCH") and mine[-1]["kind"] == "QTY_MISMATCH"   # 3.5.2 / 3.13.9: the QTY finding is the latest row


def test_r14_09_25_verified_clock_closed_at_the_exit_gate_is_a_p8a2_deferral(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["clock"] = [None, clock_answer(rig, is_open=False)]
    rig.cycle()
    w = rig.ctl("EXIT_SUBMISSION")
    assert rig.posts() == 0 and w["last_result"] == "WARN" and w["last_input"]["step"] == "P8a-2"
    assert rig.life(PROT) == [] and rig.pos(p).exit_cycle == 0


def test_r14_09_25_outside_the_session_with_the_clock_unreadable_is_a_p8a1_deferral_without_a_gate_clock_read(tmp_path):
    rig = Rig(tmp_path, t0=datetime(2026, 11, 11, 1, 0, tzinfo=UTC))                 # 20:00 New York
    p = rig.open()
    rig.live("SNDK", "8.50")
    rig.broker.faults["clock"] = [None, BK.BrokerError("TIMEOUT")]
    rig.cycle()
    w = rig.ctl("EXIT_SUBMISSION")
    assert rig.posts() == 0 and w["last_result"] == "WARN" and w["last_input"]["step"] == "P8a-1"
    assert rig.broker.n("clock") == 1 and rig.life(PROT) == []                       # SC's per-cycle read only: no exit-gate clock read


# ================================================================ r14 test 10: containment ===================================================
def test_r14_10_z1_exception_is_blind_gate_error_and_the_next_position_is_still_evaluated(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a, b = rig.open("AAA"), rig.open("BBB")
    rig.live("AAA", "8.50")
    rig.live("BBB", "8.50")
    real = rig.x._gate_price
    monkeypatch.setattr(rig.x, "_gate_price", lambda sym, *z: (_ for _ in ()).throw(RuntimeError("boom")) if sym == "AAA" else real(sym, *z))
    out = rig.cycle()
    assert out["result"] == "RUN" and "GATE_ERROR" in rig.x.stop_check_problems["AAA"]
    assert codes_of(rig.life(STOP, a.position_id)[0]) == ["GATE_ERROR"] and len(rig.exits(b)) == 1 and rig.posts() == 1


def test_r14_10_z2_before_the_insert_is_a_gate_error_refusal(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    real = rig.store.intent
    monkeypatch.setattr(rig.store, "intent", lambda xid: (_ for _ in ()).throw(sqlite3.OperationalError("locked")) if xid == exit_id_for(p.position_id, 1) else real(xid))
    out = rig.cycle()
    assert out["result"] == "RUN" and rig.posts() == 0 and rig.exits(p) == []
    assert codes_of(rig.life(PROT, p.position_id)[0]) == ["GATE_ERROR"]


def test_r14_10_z2_after_the_insert_not_sent_dg_gate_error_exit_cycle_unchanged_and_the_next_position_runs(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a, b = rig.open("AAA"), rig.open("BBB")
    rig.live("AAA", "8.50")
    rig.live("BBB", "8.50")
    real = rig.store.update_position

    def upd(pid, now, event=None, **f):
        if pid == a.position_id and (event or {}).get("kind") == "EXIT_INTENT":
            raise sqlite3.OperationalError("disk I/O error")
        return real(pid, now, event, **f)
    monkeypatch.setattr(rig.store, "update_position", upd)
    out = rig.cycle()
    it = rig.store.intent(exit_id_for(a.position_id, 1))
    assert out["result"] == "RUN" and it.state == "NOT_SENT" and it.failure_code == "DG_GATE_ERROR" and it.attempt_count == 0
    assert rig.pos(a).exit_cycle == 0 and len(rig.exits(b)) == 1 and rig.posts() == 1
    assert codes_of(rig.life(PROT, a.position_id)[0]) == ["GATE_ERROR"]
    monkeypatch.setattr(rig.store, "update_position", real)
    rig.cycle()                                                                       # the next cycle meets the 3.8.2 collision (test 28)
    assert "EXIT_CYCLE_COLLISION" in rig.ctl("EXIT_SUBMISSION")["last_input"]["codes"] and rig.pos(a).exit_cycle == 0


def test_r14_10_z2_inside_dg_final_not_sent_dg_gate_error_cycle_consumed_never_advanced_by_containment(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    real = rig.x._gate_facts

    def facts(oc, stage, *a, **k):
        if stage == DG.DG_FINAL:
            raise sqlite3.OperationalError("locked")                                   # an exception inside DG-final, before the attempt write
        return real(oc, stage, *a, **k)
    monkeypatch.setattr(rig.x, "_gate_facts", facts)
    out = rig.cycle()
    it = rig.store.intent(exit_id_for(p.position_id, 1))
    assert out["result"] == "RUN" and rig.posts() == 0 and it.state == "NOT_SENT" and it.failure_code == "DG_GATE_ERROR" and it.attempt_count == 0
    q = rig.pos(p)
    assert q.exit_cycle == 1 and q.state == "EXIT_REQUIRED" and len(rig.events(p, "EXIT_NOT_DONE")) == 1
    assert codes_of(rig.life(PROT, p.position_id)[0]) == ["GATE_ERROR"]


def test_r14_10_z3_an_exception_after_the_attempt_write_leaves_the_attempted_intent_untouched(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.x.hooks["after_post"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("after the POST"))
    out = rig.cycle()
    it = rig.store.intent(exit_id_for(p.position_id, 1))
    assert out["result"] == "ERROR" and rig.posts() == 1 and it.attempt_count == 1 and it.state != "NOT_SENT"


# ================================================================ r14 test 11: strategy exits =================================================
@pytest.mark.parametrize("ep,reason", [({"invalidation_reason": "EVIDENCE_CONTRADICTION"}, "EVIDENCE_INVALIDATION"), ({}, "SETUP_INVALIDATED"),
                                       ({"invalidation_reason": "DEMAND_COLLAPSED"}, "DEMAND_FAILURE"),
                                       ({"invalidation_reason": "STRETCH_NORMALIZED"}, "DISLOCATION_NORMALIZED")])
def test_r14_11_each_phase1_reason_is_held_informational_never_blocking(tmp_path, ep, reason):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), **ep}
    for _ in range(3):
        rig.cycle()
    held = rig.life(HELD, p.position_id)
    assert len(held) == 1 and held[0]["blocking"] == 0 and codes_of(held[0]) == ["DG_UNVERIFIED"]
    assert rig.posts() == 0 and rig.x.stop_check_problems == {} and rig.life(PROT) == [] and rig.pos(p).exit_reason == reason


@pytest.mark.parametrize("opened,now,sent", [
    (datetime(2026, 11, 20, 15, 0, tzinfo=UTC), datetime(2026, 11, 30, 15, 0, tzinfo=UTC), True),      # 23,24,25,27(early close),30 = 5
    (datetime(2026, 11, 23, 15, 0, tzinfo=UTC), datetime(2026, 11, 30, 15, 0, tzinfo=UTC), False),     # 24,25,27,30 = 4: the holiday is not counted
])
def test_r14_11_time_exit_recount_across_a_holiday_and_an_early_close(tmp_path, opened, now, sent):
    rig = Rig(tmp_path, t0=now)
    p = rig.open(at=opened)
    rig.live("SNDK", "12.00")
    rig.cycle()
    assert (rig.posts() == 1) is sent
    if not sent:
        assert codes_of(rig.life(HELD, p.position_id)[0]) == ["DG_UNVERIFIED"]


def test_r14_11_22_time_exit_on_an_uncovered_date_is_held(tmp_path):
    rig = Rig(tmp_path, t0=datetime(2028, 1, 11, 15, 0, tzinfo=UTC))
    p = rig.open(at=datetime(2028, 1, 3, 15, 0, tzinfo=UTC))
    rig.live("SNDK", "12.00")
    rig.cycle()
    assert rig.posts() == 0 and "DG_CALENDAR_UNCOVERED" in codes_of(rig.life(HELD, p.position_id)[0])


def test_r14_11_26_time_exit_with_the_calendar_unreadable_is_held(tmp_path, monkeypatch):
    import autorun.calendar as CAL
    rig = Rig(tmp_path)
    p = rig.open(at=datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    rig.live("SNDK", "12.00")
    monkeypatch.setattr(CAL, "covered", lambda d: (_ for _ in ()).throw(RuntimeError("broken deploy")))
    rig.cycle()
    assert rig.posts() == 0 and "DG_CALENDAR_UNREADABLE" in codes_of(rig.life(HELD, p.position_id)[0])
    assert rig.life(STOP) == [] and rig.x.stop_check_problems == {}                 # SC unchanged (verified NOT_HIT): it never reads the calendar


# ================================================================ r14 test 13: collector failures ==============================================
@pytest.mark.parametrize("fault", ["quote_raises", "candles_raise", "quote_none", "price_fn_raises"])
def test_r14_13_stream_side_failures_are_blind_never_an_allow(tmp_path, fault):
    rig, p = hit_rig(tmp_path)
    if fault == "quote_raises":
        rig.market.quotes["SNDK"] = RuntimeError("quote")
        rig.market.bars["SNDK"] = RuntimeError("bars")
    elif fault == "candles_raise":
        rig.market.quotes["SNDK"] = None
        rig.market.bars["SNDK"] = RuntimeError("bars")
    elif fault == "quote_none":
        rig.market.quotes["SNDK"] = None
    else:
        rig.x.price_fn = lambda sym: (_ for _ in ()).throw(RuntimeError("price_fn"))
    out = rig.cycle()
    assert out["result"] == "RUN" and rig.posts() == 0 and "SNDK" in rig.x.stop_check_problems and len(rig.life(STOP)) == 1


def test_r14_13_lifecycle_read_failure_never_aborts_the_step_and_writes_nothing(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    monkeypatch.setattr(rig.store, "safety_states", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such table")))
    out = rig.cycle()
    assert out["result"] == "RUN" and rig.posts() == 1 and rig.recs(LIFE) == []


def test_r14_13_calendar_raising_refuses_the_protective_exit_never_uncovered(tmp_path, monkeypatch):
    import autorun.calendar as CAL
    rig, p = hit_rig(tmp_path)
    monkeypatch.setattr(CAL, "covered", lambda d: (_ for _ in ()).throw(ImportError("calendar")))
    rig.cycle()
    assert rig.posts() == 0 and codes_of(rig.life(PROT, p.position_id)[0]) == ["P8_CALENDAR_UNREADABLE"]


# ================================================================ r14 test 17: the read budget ===============================================
def test_r14_17_sc_reads_the_clock_once_per_cycle_with_several_positions(tmp_path):
    rig = Rig(tmp_path)
    ps = []
    for s in ("AAA", "BBB", "CCC"):
        ps.append(rig.open(s))
        rig.live(s, "12.00")
    rig.cycle()                                                                       # C1c2 (owner D1): SC's 1 read + one P8b read per startup probe (OD-R6-2(a))
    assert rig.broker.n("clock") == 1 + 3 and rig.broker.n("orders") == 3 and {p.position_id for p in ps} <= rig.x._verified_positions
    rig.broker.calls.clear()
    rig.cycle()                                                                       # every startup probe verified: SC's own read alone
    assert rig.broker.n("clock") == 1 and rig.broker.n("orders") == 0 and rig.ctl("EXIT_TRIGGER")["last_result"] == "PASS"


def test_r14_17_a_slow_cycle_re_reads_the_clock_once(tmp_path):
    rig = Rig(tmp_path)
    ps = []
    for s in ("AAA", "BBB", "CCC", "DDD"):
        ps.append(rig.open(s))
        rig.live(s, "12.00")
    rig.cycle()                                                                       # C1c2 (owner D1): let every startup probe verify first
    assert rig.broker.n("clock") == 1 + 4 and {p.position_id for p in ps} <= rig.x._verified_positions

    def slow():
        rig.t += timedelta(seconds=70)
        return rig.t
    rig.x.clock = slow
    rig.broker.calls.clear()
    rig.cycle()
    assert rig.broker.n("clock") == 2 and rig.broker.n("orders") == 0 and rig.life(STOP) == []   # re-read once; every position still verified; no probe read


def test_r14_17_ten_hits_give_nine_posts_and_one_budget_reserved_refusal(tmp_path):
    rig = Rig(tmp_path)
    ps = []
    for i in range(10):
        s = f"S{i}AA"
        ps.append(rig.open(s))
        rig.live(s, "8.50")
    rig.cycle()
    assert rig.posts() == 9
    refused = [p for p in ps if rig.life(PROT, p.position_id)]
    assert len(refused) == 1 and codes_of(rig.life(PROT, refused[0].position_id)[0]) == ["BUDGET_RESERVED"]
    assert rig.exits(refused[0]) == [] and rig.pos(refused[0]).exit_cycle == 0


def test_r14_17_a_broker_with_a_non_integer_budget_defers_every_exit_fail_closed(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "8.50")
    rig.broker.budget = 80.0                                                         # the transport still accepts it; the estimate does not (remaining 0)
    rig.cycle()
    assert rig.posts() == 0 and codes_of(rig.life(PROT, p.position_id)[0]) == ["BUDGET_RESERVED"]


def test_r14_17_a_reset_by_another_component_never_raises_the_estimate():
    reads0, budget = 100, 80
    assert DG.reads_remaining(budget, reads0 + 10, reads0) == 70                     # true remaining after a foreign reset + 5 reads is 75 >= 70
    assert DG.reads_remaining(budget, reads0 + 200, reads0) == 0
    assert DG.reads_remaining(budget, None, reads0) == 0 and DG.reads_remaining("80", 1, 0) == 0 and DG.reads_remaining(True, 1, 0) == 0


# ================================================================ r14 test 18 (service part): price selection =================================
def test_r14_18_quote_outage_with_the_newest_bar_forming_selects_the_previous_bar_aged_from_its_start(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    now = int((T0 + timedelta(seconds=30)).timestamp())
    rig.market.bars["SNDK"] = [SimpleNamespace(time=now - 90, close=12.0, final=True), SimpleNamespace(time=now - 30, close=12.5, final=True)]   # the last ends in the future
    rig.cycle()
    q = rig.pos(p)
    assert rig.ctl("EXIT_TRIGGER")["last_result"] == "PASS" and q.last_price == "12.00" and rig.life(STOP) == []


def test_r14_18_simulated_broker_price_is_today_s(tmp_path):
    rig = Rig(tmp_path, simulated=True)
    rig.open()
    now = int((T0 + timedelta(seconds=30)).timestamp())
    rig.market.bars["SNDK"] = [SimpleNamespace(time=now - 90, close=12.0, final=True), SimpleNamespace(time=now - 30, close=12.5, final=True)]
    assert rig.x._price("SNDK", T0)[0] == Decimal("12.5")                             # finals[-1], unchanged


# ================================================================ r14 test 22: calendar ======================================================
def test_r14_22_uncovered_date_verified_hit_exactly_one_post_with_calendar_uncovered_provenance(tmp_path):
    rig, p = hit_rig(tmp_path, t0=datetime(2028, 1, 4, 15, 0, tzinfo=UTC))
    rig.cycle()
    xs = rig.exits(p)
    assert rig.posts() == 1 and xs[0].environment["data_gate"]["exit_gate"]["provenance"]["calendar"] == "UNCOVERED"


@pytest.mark.parametrize("fault,code", [("raise", "P8_CLOCK_UNREADABLE"), ("skew", "P8_CLOCK_SKEW")])
def test_r14_22_uncovered_date_unverified_clock_refuses_critical(tmp_path, fault, code):
    rig, p = hit_rig(tmp_path, t0=datetime(2028, 1, 4, 15, 0, tzinfo=UTC))
    rig.broker.faults["clock"] = [None, BK.BrokerError("TIMEOUT") if fault == "raise" else clock_answer(rig, skew=61)]
    rig.cycle()
    assert rig.posts() == 0 and codes_of(rig.life(PROT, p.position_id)[0]) == [code]


def test_r14_22_uncovered_date_verified_closed_clock_is_a_warn_deferral(tmp_path):
    rig, p = hit_rig(tmp_path, t0=datetime(2028, 1, 4, 15, 0, tzinfo=UTC))
    rig.broker.faults["clock"] = [None, clock_answer(rig, is_open=False)]
    rig.cycle()
    assert rig.posts() == 0 and rig.life(PROT) == [] and rig.ctl("EXIT_SUBMISSION")["last_result"] == "WARN"


@pytest.mark.parametrize("when,calendar_raises", [(T0, False), (datetime(2028, 1, 4, 15, 0, tzinfo=UTC), False), (T0, True)])
def test_r14_22_sc_gives_identical_statuses_on_covered_uncovered_and_unreadable_dates(tmp_path, monkeypatch, when, calendar_raises):
    import autorun.calendar as CAL
    if calendar_raises:
        monkeypatch.setattr(CAL, "covered", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    rig = Rig(tmp_path, t0=when)
    rig.open()
    rig.live("SNDK", "12.00")
    rig.cycle()
    assert rig.ctl("EXIT_TRIGGER")["last_result"] == "PASS" and rig.life(STOP) == []
    rig.live("SNDK", "12.00", age=301)
    rig.cycle()
    assert codes_of(rig.life(STOP)[0]) == ["PRICE_STALE"]


def test_r14_22_covered_holiday_with_the_broker_open_is_od_r4_3a_refusal(tmp_path):
    rig, p = hit_rig(tmp_path, t0=datetime(2026, 11, 26, 15, 0, tzinfo=UTC))       # Thanksgiving (covered, closed); the broker clock says open
    rig.cycle()
    assert rig.posts() == 0 and codes_of(rig.life(PROT, p.position_id)[0]) == ["P8_SESSION_SOURCES_DISAGREE"]


def test_r14_22_26_covered_holiday_with_the_calendar_raising_is_p8_calendar_unreadable(tmp_path, monkeypatch):
    import autorun.calendar as CAL
    rig, p = hit_rig(tmp_path, t0=datetime(2026, 11, 26, 15, 0, tzinfo=UTC))
    monkeypatch.setattr(CAL, "is_market_day", lambda d: (_ for _ in ()).throw(RuntimeError("import")))
    rig.cycle()
    assert rig.posts() == 0 and codes_of(rig.life(PROT, p.position_id)[0]) == ["P8_CALENDAR_UNREADABLE"]


def test_r14_22_uncovered_strategy_and_the_engine_count_reached_is_held_not_sent(tmp_path):
    rig = Rig(tmp_path, t0=datetime(2028, 1, 11, 15, 0, tzinfo=UTC))
    p = rig.open(at=datetime(2028, 1, 3, 15, 0, tzinfo=UTC))
    rig.live("SNDK", "12.00")
    rig.cycle()
    assert rig.posts() == 0 and rig.life(HELD, p.position_id)[0]["blocking"] == 0


# ================================================================ r14 test 26: UNREADABLE vs UNCOVERED ======================================
@pytest.mark.parametrize("answer,posts", [("raise", 0), (False, 1), ("yes", 0)])
def test_r14_26_covered_raising_false_or_non_bool(tmp_path, monkeypatch, answer, posts):
    import autorun.calendar as CAL
    rig, p = hit_rig(tmp_path)
    monkeypatch.setattr(CAL, "covered", (lambda d: (_ for _ in ()).throw(RuntimeError("x"))) if answer == "raise" else (lambda d: answer))
    rig.cycle()
    assert rig.posts() == posts
    if posts == 0:
        assert codes_of(rig.life(PROT, p.position_id)[0]) == ["P8_CALENDAR_UNREADABLE"]
    else:
        assert rig.exits(p)[0].environment["data_gate"]["exit_gate"]["provenance"]["calendar"] == "UNCOVERED"


# ================================================================ r14 test 27: the age basis =================================================
@pytest.mark.parametrize("age,status", [(899, "NOT_HIT"), (900, "NOT_HIT"), (901, "BLIND"), (950, "BLIND")])
def test_r14_27_minute_close_aged_from_its_stamped_start_equal_to_stop_price_problem(tmp_path, age, status):
    rig = Rig(tmp_path)
    rig.open()
    now = T0 + timedelta(seconds=30)
    start = int(now.timestamp()) - age
    rig.market.bars["SNDK"] = [SimpleNamespace(time=start, close=12.0, final=True)]
    rig.cycle()
    blind = bool(rig.life(STOP))
    assert blind is (status == "BLIND")
    if blind:
        assert codes_of(rig.life(STOP)[0]) == ["PRICE_STALE"]
    wave1 = stop_price_problem(Decimal("12.0"), "MINUTE_CLOSE", iso(datetime.fromtimestamp(start, tz=UTC)), now, False)
    assert (wave1 is None) is (status == "NOT_HIT")


# ================================================================ r14 test 28: collisions (OD-R5-4(b)) ======================================
def collide(rig, p, *, failure_code="DG_GATE_ERROR", attempt=0, submitted=None, state="NOT_SENT"):
    xid = exit_id_for(p.position_id, p.exit_cycle + 1)
    rig.store.insert_intent(Intent(execution_id=xid, intent_type="EXIT", symbol=p.symbol, side="sell", qty=p.quantity, client_order_id=exit_coid(p.position_id, p.exit_cycle + 1),
                                   position_id=p.position_id, exit_cycle=p.exit_cycle + 1, exit_reason="HARD_INVALIDATION", state="PENDING", created_at=iso(rig.t),
                                   updated_at=iso(rig.t)))
    f = {}
    if attempt:
        f["attempt_count"] = attempt
    if submitted:
        f["submitted_at"] = submitted
    if state != "PENDING":
        f.update(state=state, failure_code=failure_code, resolved_at=iso(rig.t))
    if f:
        rig.store.update_intent(xid, rig.t, **f)
    return xid


def test_r14_28_default_b_five_cycles_visible_refusal_no_advance_no_lookup(tmp_path):
    rig, p = hit_rig(tmp_path)
    collide(rig, p)
    for _ in range(5):
        rig.cycle()
        assert rig.posts() == 0 and rig.broker.n("order_by_client_id") == 0
        assert rig.ctl("EXIT_SUBMISSION")["last_result"] == "BLOCK" and rig.ctl("EXIT_SUBMISSION")["last_input"]["codes"] == ["EXIT_CYCLE_COLLISION"]
        assert rig.pos(p).exit_cycle == 0
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and rig.events(p, "EXIT_NOT_DONE") == []


def test_r14_28_stale_pending_from_the_orders_step_of_a_new_service_is_the_same_collision(tmp_path):
    rig, p = hit_rig(tmp_path)
    collide(rig, p, state="PENDING")
    rig.x = ExecutionService(lambda: rig.stream, ExecutionStore(rig.db), broker=rig.broker, clock=lambda: rig.t, owner="new")
    rig.cycle()
    it = rig.store.intent(exit_id_for(p.position_id, 1))
    assert it.failure_code == "STALE_PENDING" and rig.broker.n("order_by_client_id") == 1   # the monitor's own lookup, never the gate's
    assert rig.ctl("EXIT_SUBMISSION")["last_input"]["codes"] == ["EXIT_CYCLE_COLLISION"] and rig.posts() == 0
    rig.cycle()
    assert rig.broker.n("order_by_client_id") == 1 and rig.posts() == 0 and len(rig.life(PROT)) == 1


@pytest.mark.parametrize("kw", [{"attempt": 1}, {"submitted": iso(T0)}, {"failure_code": "BROKER_REJECTED"}])
def test_r14_28_attempted_or_unexpected_colliding_intent_is_collision_attempted(tmp_path, kw):
    rig, p = hit_rig(tmp_path)
    collide(rig, p, **kw)
    for _ in range(2):
        rig.cycle()
        assert rig.posts() == 0 and rig.ctl("EXIT_SUBMISSION")["last_input"]["codes"] == ["EXIT_CYCLE_COLLISION_ATTEMPTED"] and rig.pos(p).exit_cycle == 0


def test_r14_28_simulated_broker_keeps_today_s_silent_return(tmp_path):
    rig = Rig(tmp_path, simulated=True)
    p = rig.open()
    rig.live("SNDK", "8.50")
    collide(rig, p)
    rig.cycle()
    assert rig.posts() == 0 and rig.recs(LIFE) == [] and rig.ctl("EXIT_SUBMISSION") is None and rig.pos(p).exit_cycle == 0


# ================================================================ r14 test 30: strategy-hold flapping ========================================
def inputs(at, stretch):
    return SimpleNamespace(evaluated_at=iso(at), stretch=stretch, stretch_status="AVAILABLE", demand=None, demand_status=None)


def test_r14_30_one_evaluation_one_row_a_new_clear_evaluation_resolves_a_new_normalized_one_recurs(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    rig.setups.inputs = inputs(T0 + timedelta(seconds=10), -10.0)                    # DISLOCATION_NORMALIZED (b): only in a cycle with a new evaluation
    for _ in range(10):
        rig.cycle()
    rows = rig.life(HELD, p.position_id)
    assert [r["kind"] for r in rows] == [HELD] and rows[0]["detail"]["evaluation"] == iso(T0 + timedelta(seconds=10))
    rig.setups.inputs = inputs(rig.t + timedelta(seconds=5), -30.0)
    rig.cycle()
    rows = rig.life(HELD, p.position_id)
    assert [r["kind"] for r in rows] == [HELD, HELD + "_RESOLVED"] and rows[-1]["detail"]["evidence"] == "HELD_REASONS_CLEARED"
    rig.setups.inputs = inputs(rig.t + timedelta(seconds=5), -10.0)
    rig.cycle()
    rows = rig.life(HELD, p.position_id)
    assert [r["kind"] for r in rows] == [HELD, HELD + "_RESOLVED", HELD] and rows[-1]["detail"]["lifecycle"] == "RECURRENT"
    assert all(r["blocking"] == 0 for r in rows) and rig.posts() == 0


# ================================================================ r14 test 32: early close ===================================================
def test_r14_32_listed_early_close_1250_sent_1301_closed_clock_deferred(tmp_path):
    rig, p = hit_rig(tmp_path, t0=datetime(2026, 11, 27, 17, 50, tzinfo=UTC))       # 12:50 New York on 2026-11-27
    rig.cycle()
    assert rig.posts() == 1                                                          # OD-R5-6(a): the residual reproduced
    rig2, p2 = hit_rig(tmp_path / "b", t0=datetime(2026, 11, 27, 18, 1, tzinfo=UTC))   # 13:01: the verified clock flips closed for the exit gate
    rig2.broker.faults["clock"] = [None, clock_answer(rig2, is_open=False)]
    rig2.cycle()
    assert rig2.posts() == 0 and rig2.ctl("EXIT_SUBMISSION")["last_input"]["step"] == "P8a-2" and rig2.life(PROT) == []


def test_r14_32_uncovered_weekday_unlisted_early_close_same_as_a(tmp_path):
    rig, p = hit_rig(tmp_path, t0=datetime(2028, 11, 24, 17, 50, tzinfo=UTC))
    rig.cycle()
    assert rig.posts() == 1


# ================================================================ r14 test 33: the recorded reason ===========================================
def test_r14_33_held_demand_failure_then_hit_is_recorded_hard_invalidation_everywhere(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), "invalidation_reason": "DEMAND_COLLAPSED"}
    rig.cycle()
    assert rig.pos(p).exit_reason == "DEMAND_FAILURE" and rig.posts() == 0
    rig.live("SNDK", "8.50")
    rig.cycle()
    it = rig.exits(p)[0]
    assert it.exit_reason == "HARD_INVALIDATION" and rig.pos(p).exit_reason == "HARD_INVALIDATION"
    assert rig.events(p, "EXIT_INTENT")[-1]["reason"] == "HARD_INVALIDATION"
    rig.sim.fill(it.client_order_id)
    rig.cycle()
    assert rig.pos(p).state == "CLOSED" and rig.events(p, "CLOSED")[-1]["reason"] == "HARD_INVALIDATION"


def test_r14_33_a_refused_protective_exit_leaves_the_position_s_exit_reason(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open(ident=dict(IDENT, account_number_masked="••••9999"))
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), "invalidation_reason": "DEMAND_COLLAPSED"}
    rig.cycle()
    rig.live("SNDK", "8.50")
    rig.cycle()
    assert rig.posts() == 0 and rig.pos(p).exit_reason == "DEMAND_FAILURE"


def test_r14_33_verified_time_exit_after_a_held_setup_invalidated_is_time_exit(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open(at=datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0)}
    rig.cycle()
    it = rig.exits(p)[0]
    assert it.exit_reason == "TIME_EXIT" and rig.pos(p).exit_reason == "TIME_EXIT"


def test_r14_33_simulated_broker_copies_pos_exit_reason_as_today(tmp_path):
    rig = Rig(tmp_path, simulated=True)
    p = rig.open()
    rig.live("SNDK", "12.00")
    rig.setups.ep["SNDK"] = {"closed_at": iso(T0), "invalidation_reason": "DEMAND_COLLAPSED"}
    rig.cycle()
    assert rig.exits(p)[0].exit_reason == "DEMAND_FAILURE"                          # today: every reason exits on a simulated broker


# ================================================================ r14 test 36: the DG-final session failure =================================
def seam_case(rig, case):
    if case == "i":
        rig.x.hooks["after_exit_intent"] = lambda ctx: setattr(rig.sim, "is_open", False)
    elif case == "ii":
        rig.x.hooks["after_exit_intent"] = lambda ctx: setattr(rig, "t", datetime(2026, 11, 10, 20, 45, 0, tzinfo=UTC))
    else:
        rig.x.near_close_minutes = 0
        rig.x.hooks["after_exit_intent"] = lambda ctx: setattr(rig, "t", datetime(2026, 11, 10, 21, 0, 0, tzinfo=UTC))


T36 = {"i": T0, "ii": datetime(2026, 11, 10, 20, 44, 59, 900000, tzinfo=UTC) - timedelta(seconds=30),
       "iii": datetime(2026, 11, 10, 20, 59, 59, 900000, tzinfo=UTC) - timedelta(seconds=30)}
STEP = {"i": "P8a-2", "ii": "P8a-1", "iii": "P8a-1"}


@pytest.mark.parametrize("case", ["i", "ii", "iii"])
def test_r14_36_dg_final_session_failure_not_sent_cycle_consumed_critical_then_next_cycle_one_post_at_k2(tmp_path, case):
    rig, p = hit_rig(tmp_path, t0=T36[case])
    seam_case(rig, case)
    rig.cycle()
    it = rig.store.intent(exit_id_for(p.position_id, 1))
    assert rig.posts() == 0 and it.attempt_count == 0 and it.state == "NOT_SENT" and it.failure_code == "DG_FINAL_SESSION_FAILED" and STEP[case] in it.failure_text
    q = rig.pos(p)
    assert q.state == "EXIT_REQUIRED" and q.exit_cycle == 1 and len(rig.events(p, "EXIT_NOT_DONE")) == 1
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and codes_of(rows[0]) == ["DG_FINAL_SESSION_FAILED"]
    blk = rig.ctl("EXIT_SUBMISSION")
    assert blk["last_result"] == "BLOCK" and blk["last_input"]["step"] == STEP[case] and blk["last_input"]["session_reason"]
    rig.x.hooks.clear()
    rig.x.near_close_minutes = 15
    rig.sim.is_open = True
    rig.t = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    rig.cycle()
    xs = sorted(rig.exits(p), key=lambda i: i.exit_cycle)
    assert rig.posts() == 1 and xs[-1].exit_cycle == 2 and xs[-1].client_order_id == exit_coid(p.position_id, 2) != xs[0].client_order_id


@pytest.mark.parametrize("case", ["i", "ii", "iii"])
def test_r14_36_pin_session_failure_on_the_last_cycle_is_critical_with_both_codes_then_attention(tmp_path, case):
    rig, p = hit_rig(tmp_path, t0=T36[case])
    rig.x.max_exit_cycles = 1
    seam_case(rig, case)
    rig.cycle()
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["blocking"] == 1 and codes_of(rows[0]) == ["DG_FINAL_SESSION_FAILED", "EXIT_ATTEMPTS_EXHAUSTED"]
    assert rig.ctl("EXIT_SUBMISSION")["last_result"] == "BLOCK"
    rig.x.hooks.clear()
    rig.x.near_close_minutes = 15
    rig.sim.is_open = True
    rig.t = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    rig.cycle()
    q = rig.pos(p)
    assert q.state == "ATTENTION" and q.attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0
    assert len(rig.life(PROT, p.position_id)) == 1                                   # no further lifecycle row once the position is in ATTENTION


def test_r14_36_strategy_case_i_not_sent_same_code_informational_hold(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open(at=datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    rig.live("SNDK", "12.00")
    seam_case(rig, "i")
    rig.cycle()
    it = rig.store.intent(exit_id_for(p.position_id, 1))
    assert rig.posts() == 0 and it.failure_code == "DG_FINAL_SESSION_FAILED"
    held = rig.life(HELD, p.position_id)
    assert len(held) == 1 and held[0]["blocking"] == 0 and "DG_FINAL_SESSION_FAILED" in codes_of(held[0]) and rig.life(PROT) == []


# ================================================================ pins: OD-C-7(a) exhaustion and the exhausted-HIT residual ===================
def exhausted(tmp_path, **kw):
    rig, p = hit_rig(tmp_path, **kw)
    rig.store.update_position(p.position_id, rig.t, None, exit_cycle=3)
    return rig, rig.pos(p)


def test_pin_od_c7a_exhaustion_is_today_s_attention_with_a_block_touch_and_no_lifecycle_row(tmp_path):
    rig, p = exhausted(tmp_path)
    rig.cycle()
    q = rig.pos(p)
    assert q.state == "ATTENTION" and q.attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0
    rows = rig.life(PROT)                                                            # r15 9-C A5 (OD-C-7 (b1)): exactly one ACTIVE alert row, the onset exit_cycle
    assert len(rows) == 1 and rows[0]["position_id"] == p.position_id and rows[0]["detail"]["lifecycle"] == "ACTIVE" and codes_of(rows[0]) == ["EXIT_ATTEMPTS_EXHAUSTED"] and rows[0]["detail"]["exit_cycle"] == 3 and rig.exits(p) == []
    blk = rig.ctl("EXIT_SUBMISSION")
    assert blk["last_result"] == "BLOCK" and "exit attempts exhausted" in blk["last_reason"] and blk["last_input"] == {"position_id": p.position_id}
    assert rig.events(p, "ATTENTION")[-1]["reason"] == "EXIT_ATTEMPTS_EXHAUSTED"


@pytest.mark.parametrize("fault", ["clock", "budget"])
def test_pin_exhausted_hit_residual_feed_clock_or_budget_failure_alerts_first_attention_in_the_first_verified_cycle(tmp_path, fault):
    rig, p = exhausted(tmp_path)
    if fault == "clock":
        rig.broker.faults["clock"] = [None, BK.BrokerError("TIMEOUT")]
        code = "P8_CLOCK_UNREADABLE"
    else:
        rig.broker.budget = 10                                                       # reconcile 2 + SC 1 leaves 7 < 8
        code = "BUDGET_RESERVED"
    rig.cycle()
    q = rig.pos(p)
    assert q.state == "EXIT_REQUIRED" and rig.posts() == 0 and codes_of(rig.life(PROT, p.position_id)[0]) == [code]
    rig.broker.budget = BK.GET_BUDGET
    rig.cycle()
    q = rig.pos(p)
    assert q.state == "ATTENTION" and q.attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0


def test_pin_exhausted_position_with_a_blind_feed_never_reaches_exhaustion_that_cycle(tmp_path):
    rig, p = exhausted(tmp_path)
    rig.market.mode = "simulated"
    rig.cycle()
    assert rig.pos(p).state != "ATTENTION" and len(rig.life(STOP)) == 1 and rig.posts() == 0
    rig.market.mode = "real"
    rig.cycle()
    assert rig.pos(p).state == "ATTENTION"


# ================================================================ pin: an unknown outcome (condition (k)) ======================================
@pytest.mark.parametrize("prov", [{}, {"outcome": "SOMETHING_NEW"}])
@pytest.mark.parametrize("protective", [True, False])
def test_pin_unknown_or_missing_outcome_on_a_refused_exit_is_a_refusal_or_a_hold_never_suppressed(tmp_path, monkeypatch, prov, protective):
    if protective:
        rig, p = hit_rig(tmp_path)
    else:
        rig = Rig(tmp_path)
        p = rig.open(at=datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
        rig.live("SNDK", "12.00")
    monkeypatch.setattr(DG, "evaluate", lambda f: DG.GateVerdict(False, f.order_class, "P2", ("P2",), ("P2",), dict(prov)))
    rig.cycle()
    assert rig.posts() == 0
    fam = PROT if protective else HELD
    rows = rig.life(fam, p.position_id)
    assert len(rows) == 1 and codes_of(rows[0]) == ["P2"]
    assert rig.ctl("EXIT_SUBMISSION")["last_result"] == ("BLOCK" if protective else "WARN")


def test_pin_unknown_outcome_at_dg_final_is_a_dg_final_refusal(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    real = DG.evaluate
    monkeypatch.setattr(DG, "evaluate", lambda f: real(f) if f.stage == DG.EXIT_GATE else DG.GateVerdict(False, f.order_class, "X", ("X",), ("P1",), {}))
    rig.cycle()
    it = rig.store.intent(exit_id_for(p.position_id, 1))
    assert rig.posts() == 0 and it.state == "NOT_SENT" and it.failure_code == "DG_FINAL_REFUSED" and it.attempt_count == 0
    assert codes_of(rig.life(PROT, p.position_id)[0]) == ["X"] and rig.pos(p).exit_cycle == 1


# ================================================================ pin: the service side of the A1 truth table ==================================
def snapshot(rig):
    ctl = {c["control_id"]: (c["last_result"], c["last_reason"]) for c in rig.store.controls()}
    return ctl, rig.posts(), sorted((p.symbol, p.state, p.exit_cycle) for p in rig.store.positions(open_only=False)), dict(rig.x.stop_check_problems)


@pytest.mark.parametrize("scenario", ["hit_refused", "blind", "hit_sent"])
def test_pin_a1_the_writer_s_return_value_never_changes_an_in_cycle_effect(tmp_path, monkeypatch, scenario):
    outs = []
    for ret in (True, False):
        rig = Rig(tmp_path / str(ret))
        p = rig.open(ident=dict(IDENT, account_number_masked="••••9999") if scenario == "hit_refused" else None)
        if scenario != "blind":
            rig.live("SNDK", "8.50")
        monkeypatch.setattr(rig.x, "_safety_transition", lambda *a, _r=ret, **k: _r)
        rig.cycle()
        outs.append(snapshot(rig))
    assert outs[0] == outs[1]


def test_pin_a1_a_raising_store_write_never_leaves_the_writer_and_effects_stand(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a, b = rig.open("AAA"), rig.open("BBB")
    rig.market.mode = "real"
    rig.live("BBB", "8.50")
    monkeypatch.setattr(rig.store, "add_safety_transition", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    out = rig.cycle()
    assert out["result"] == "RUN" and "AAA" in rig.x.stop_check_problems and rig.posts() == 1 and rig.recs(LIFE) == []


# ================================================================ r14 test 19 (service-cycle variants) ==========================================
def test_r14_19_a_b_a_active_resolved_recurrent_three_rows_two_blocking(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.cycle()                                                                       # no quote: BLIND
    rig.live("SNDK", "12.00")
    rig.cycle()                                                                       # verified NOT_HIT
    rig.market.quotes["SNDK"] = None
    rig.cycle()
    rows = rig.life(STOP, p.position_id)
    assert [r["kind"] for r in rows] == [STOP, STOP + "_RESOLVED", STOP]
    assert [r["detail"].get("lifecycle") for r in rows] == ["ACTIVE", None, "RECURRENT"] and [r["detail"]["episode"] for r in rows] == [1, 1, 2]
    assert sum(r["blocking"] for r in rows) == 2 and rows[1]["detail"]["evidence"] == "STOP_VERIFIED_NOT_HIT"


def test_r14_19_no_duplicate_while_unresolved_across_20_cycles_with_the_code_changing(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    for i in range(20):
        if i < 7:
            rig.live("SNDK", "12.00", age=400)
        elif i < 14:
            rig.market.quotes["SNDK"] = None
        else:
            rig.market.mode = "simulated"
        rig.cycle()
    assert len(rig.life(STOP, p.position_id)) == 1


def test_r14_19_non_evidence_leaves_active_closed_market_simulated_cycle_failed_write(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.cycle()
    assert rig.state(STOP, p) == "ACTIVE"
    rig.sim.is_open = False
    rig.cycle()                                                                       # a closed-market cycle
    rig.sim.is_open = True
    real = rig.x.broker
    rig.x.broker = BK.AlpacaPaperBroker(simulated=True, wire=lambda: rig.sim)
    rig.live("SNDK", "12.00")
    rig.cycle()                                                                       # a simulated-broker cycle with a good price
    rig.x.broker = real
    monkeypatch.setattr(rig.store, "add_safety_transition", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    rig.cycle()                                                                       # verified NOT_HIT but the write fails
    assert rig.state(STOP, p) == "ACTIVE" and len(rig.life(STOP, p.position_id)) == 1
    monkeypatch.undo()
    rig.cycle()
    assert rig.state(STOP, p) == "RESOLVED"


def test_r14_19_protective_family_refused_p6_active_then_accepted_resolved_then_recurrent(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["orders"] = [lambda *a: [an_order("SNDK", "sell")]]
    rig.cycle()
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["exit_cycle"] == 0
    rig.cycle()                                                                       # every fact verifies: the POST is accepted (O1)
    rows = rig.life(PROT, p.position_id)
    assert rig.posts() == 1 and rows[-1]["kind"] == PROT + "_RESOLVED" and rows[-1]["detail"]["evidence"] == "PROTECTIVE_EXIT_ACCEPTED"
    rig.sim.set_status(rig.exits(p)[0].client_order_id, "canceled")
    rig.broker.faults["orders"] = [lambda *a: [an_order("SNDK", "sell")]]
    rig.cycle()                                                                       # the monitor sees the cancel; the next exit gate refuses on P6
    rows = rig.life(PROT, p.position_id)
    assert rows[-1]["kind"] == PROT and rows[-1]["detail"]["lifecycle"] == "RECURRENT" and rows[-1]["detail"]["episode"] == 2


def test_r14_19_every_lifecycle_row_has_position_id_and_symbol_and_never_goes_through_finding(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a, b = rig.open("AAA"), rig.open("BBB", ident=dict(IDENT, account_number_masked="••••9999"))
    rig.live("BBB", "8.50")
    rig.setups.ep["AAA"] = {"closed_at": iso(T0)}
    seen = []
    real = rig.x._finding
    monkeypatch.setattr(rig.x, "_finding", lambda now, trig, f, run_id=None: (seen.append(f.kind), real(now, trig, f, run_id))[1])
    for _ in range(3):
        rig.cycle()
    rows = rig.recs(LIFE)
    assert {r["kind"] for r in rows} >= {STOP, PROT, HELD}
    assert all(r["position_id"] and r["symbol"] for r in rows) and not (set(seen) & LIFE)


# ================================================================ r14 test 29 (service variant) and the sweeps ================================
def seed_active(rig, p, fam=PROT, exit_cycle=0):
    alert = {"reason_codes": ["P6"]}
    if fam != STOP:
        alert["exit_cycle"] = exit_cycle
    assert rig.store.add_safety_transition(fam, p.position_id, p.symbol, "NONE", rig.t, "TEST", None, alert=alert)


def test_r14_29_write_failure_after_an_o1_b_still_sent_a_stays_active_then_derived_resolution(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a, b = rig.open("AAA"), rig.open("BBB")
    for p in (a, b):
        seed_active(rig, p)
        rig.live(p.symbol, "8.50")
    real = rig.store.add_safety_transition

    def flaky(fam, pid, *args, **kw):
        if pid == a.position_id:
            raise sqlite3.OperationalError("locked")
        return real(fam, pid, *args, **kw)
    monkeypatch.setattr(rig.store, "add_safety_transition", flaky)
    out = rig.cycle()
    assert out["result"] == "RUN" and rig.posts() == 2 and rig.state(PROT, a) == "ACTIVE" and rig.state(PROT, b) == "RESOLVED"
    monkeypatch.setattr(rig.store, "add_safety_transition", real)
    rig.cycle()                                                                       # A in flight: the derived sweep (2')
    last = rig.life(PROT, a.position_id)[-1]
    assert last["kind"] == PROT + "_RESOLVED" and last["detail"]["derived"] is True and last["detail"]["execution_id"] == exit_id_for(a.position_id, 1)


def test_r14_29_canceled_before_the_sweep_still_resolves_by_derived_evidence_and_the_next_refusal_recurs(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    seed_active(rig, p)
    real = rig.store.add_safety_transition
    monkeypatch.setattr(rig.store, "add_safety_transition", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    rig.cycle()
    monkeypatch.setattr(rig.store, "add_safety_transition", real)
    rig.sim.set_status(rig.exits(p)[0].client_order_id, "canceled")
    rig.broker.faults["orders"] = [lambda *a: [an_order("SNDK", "sell")]]
    rig.cycle()
    kinds = [(r["kind"], (r["detail"] or {}).get("lifecycle")) for r in rig.life(PROT, p.position_id)]
    assert kinds == [(PROT, "ACTIVE"), (PROT + "_RESOLVED", None), (PROT, "RECURRENT")]


def test_r14_29_position_closed_write_failure_is_written_in_the_next_active_cycle(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    a, b = rig.open("AAA"), rig.open("BBB")
    rig.live("BBB", "12.00")                                                          # B keeps every cycle active
    seed_active(rig, a, STOP)
    rig.live("AAA", "8.50")
    real = rig.store.add_safety_transition
    monkeypatch.setattr(rig.store, "add_safety_transition", lambda *x, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    rig.cycle()                                                                       # A's HIT is sent; its STOP_VERIFIED_HIT write fails
    rig.sim.fill(rig.exits(a)[0].client_order_id)
    rig.cycle()                                                                       # the fill closes A; the CLOSED sweep write fails
    assert rig.pos(a).state == "CLOSED" and rig.state(STOP, a) == "ACTIVE"
    monkeypatch.setattr(rig.store, "add_safety_transition", real)
    rig.cycle()
    last = rig.life(STOP, a.position_id)[-1]
    assert last["kind"] == STOP + "_RESOLVED" and last["detail"]["evidence"] == "POSITION_CLOSED"


def test_r14_29_nothing_resolves_on_absence_ten_cycles(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "12.00")
    seed_active(rig, p)
    rig.broker.faults["orders"] = lambda *_a: [an_order("SNDK", "sell")]             # C1c2 (owner D1): P6 fails, so the OD-R4-2(a) probe never verifies every fact
    for _ in range(10):
        rig.cycle()
    assert rig.state(PROT, p) == "ACTIVE" and len(rig.life(PROT, p.position_id)) == 1   # a NOT_HIT is never protective evidence
    assert rig.broker.n("orders") == 10 and rig.posts() == 0 and rig.exits(p) == []     # the probe ran every cycle and failed; no O1 intent, no POST


# ================================================================ r14 test 31 (service-cycle variant) ==========================================
def test_r14_31_an_open_stop_check_blind_from_a_cycle_is_open_in_the_all_time_record_until_resolved(tmp_path):
    from forward_validation.service import ForwardValidationService
    from forward_validation.store import ForwardValidationStore
    rig = Rig(tmp_path)
    p = rig.open()
    fvs = ForwardValidationStore(rig.db)
    fvs.ensure()
    fv = ForwardValidationService(lambda: None, fvs, clock=lambda: rig.t)
    base = fv.reconciliation(None)["open"]
    rig.cycle()                                                                       # BLIND: no quote
    assert fv.reconciliation(None)["open"] == base + 1
    rig.live("SNDK", "12.00")
    rig.cycle()
    assert fv.reconciliation(None)["open"] == base


def test_mut_l1592_l1593_strategy_evidence_never_resolves_the_protective_family(tmp_path):
    """r14:1592-1593 (C1d mutation campaign): an accepted STRATEGY exit (O1, TIME_EXIT) is never evidence for PROTECTIVE_EXIT_UNVERIFIED, neither
    through the derived sweep (2') nor through the after-POST evidence; only the family's own evidence resolves it."""
    rig = Rig(tmp_path)
    p = rig.open()
    assert rig.store.add_safety_transition(PROT, p.position_id, p.symbol, "NONE", rig.t, "TEST", None, alert={"reason_codes": ["P2"], "exit_cycle": 0})
    it = rig.store.insert_intent(Intent(execution_id=exit_id_for(p.position_id, 1), intent_type="EXIT", symbol=p.symbol, side="sell", qty=10,
                                        client_order_id=exit_coid(p.position_id, 1), position_id=p.position_id, exit_cycle=1, exit_reason="TIME_EXIT",
                                        state="SUBMITTED", created_at=iso(rig.t), updated_at=iso(rig.t)))
    it = rig.store.update_intent(it.execution_id, rig.t, attempt_count=1, submitted_at=iso(rig.t), outcome_class="O1")
    rig.x._safety_sweeps(rig.t, "TEST", None)
    assert rig.state(PROT, p) == "ACTIVE"
    rig.x._exit_after_post(it, rig.t, {"order_class": DG.STRATEGY_EXIT, "dg_final_allowed": True, "trigger": "TEST"})
    assert rig.state(PROT, p) == "ACTIVE" and [r["kind"] for r in rig.life(PROT, p.position_id)] == [PROT]


def test_mut_l1526_an_unknown_broker_never_takes_the_simulated_branch(tmp_path):
    """r14:1526 (C1d mutation campaign): `simulated` neither True nor False is an UNKNOWN broker: the gated path refuses (P1), never today's exit."""
    class Unknown(Spy):
        def __init__(self, sim):
            super().__init__(sim)
            self.simulated = None
    rig = Rig(tmp_path, broker_cls=Unknown)
    p = rig.open()
    rig.live("SNDK", "8.50")
    rig.cycle()
    assert rig.posts() == 0 and rig.exits(p) == [] and rig.state(PROT, p) == "ACTIVE"


def test_mut_l1528_an_engine_breach_on_a_stale_price_decides_no_exit(tmp_path):
    """r14:1528 (C1d mutation campaign): the engine's HARD_INVALIDATION alone (SC BLIND) decides nothing: no EXIT_REQUIRED, no hold row."""
    rig = Rig(tmp_path)
    p = rig.open()
    rig.live("SNDK", "8.50", age=20 * 3600)                                       # before 09:30 of the session date: BLIND
    out = rig.cycle()
    assert rig.pos(p).state != "EXIT_REQUIRED" and rig.pos(p).exit_reason is None
    assert out["summary"]["exits_decided"] == 0 and rig.life(HELD) == [] and rig.posts() == 0


def test_mut_l1533_containment_never_touches_an_attempted_intent(tmp_path):
    """r14:1533 / 3.1 Z2 (C1d mutation campaign): containment marks NOT_SENT only an intent with attempt_count 0 and no broker_order_id."""
    rig = Rig(tmp_path)
    p = rig.open()
    it = rig.store.insert_intent(Intent(execution_id=exit_id_for(p.position_id, 1), intent_type="EXIT", symbol=p.symbol, side="sell", qty=10,
                                        client_order_id=exit_coid(p.position_id, 1), position_id=p.position_id, exit_cycle=1, exit_reason="HARD_INVALIDATION",
                                        state="PENDING", created_at=iso(rig.t), updated_at=iso(rig.t)))
    rig.store.update_intent(it.execution_id, rig.t, attempt_count=1, submitted_at=iso(rig.t), state="SUBMITTED")
    rig.x._exit_contained(rig.pos(p), rig.t, {"order_class": DG.PROTECTIVE_EXIT, "intent_id": it.execution_id, "trigger": "TEST"}, RuntimeError("x"))
    back = rig.store.intent(it.execution_id)
    assert back.state == "SUBMITTED" and back.attempt_count == 1 and back.failure_code != "DG_GATE_ERROR"


def test_mut_l1593_derived_evidence_needs_o1_and_a_strictly_later_cycle(tmp_path):
    """r14:1593 (C1d mutation campaign): (2') needs an O1 HARD_INVALIDATION exit whose exit_cycle is STRICTLY greater than the alert's."""
    rig = Rig(tmp_path)
    p = rig.open()
    assert rig.store.add_safety_transition(PROT, p.position_id, p.symbol, "NONE", rig.t, "TEST", None, alert={"reason_codes": ["P2"], "exit_cycle": 1})
    for k, oc in ((1, "O1"), (2, "O5")):                                          # the same cycle accepted; a later cycle rejected
        it = rig.store.insert_intent(Intent(execution_id=exit_id_for(p.position_id, k), intent_type="EXIT", symbol=p.symbol, side="sell", qty=10,
                                            client_order_id=exit_coid(p.position_id, k), position_id=p.position_id, exit_cycle=k, exit_reason="HARD_INVALIDATION",
                                            state="PENDING", created_at=iso(rig.t), updated_at=iso(rig.t)))
        rig.store.update_intent(it.execution_id, rig.t, attempt_count=1, submitted_at=iso(rig.t), outcome_class=oc, state="CANCELED" if oc == "O1" else "REJECTED")
    rig.x._safety_sweeps(rig.t, "TEST", None)
    assert rig.state(PROT, p) == "ACTIVE" and [r["kind"] for r in rig.life(PROT, p.position_id)] == [PROT]


def _c1d_touch_spy(rig):
    """c1d-fix2: every control row the service writes, in order: (control_id, result, reason, input). The real _touch still runs."""
    seen, real = [], rig.x._touch

    def spy(now, cid, result, reason, inp=None, error=None):
        seen.append((cid, result, reason, dict(inp or {})))
        return real(now, cid, result, reason, inp, error)
    rig.x._touch = spy
    return seen


def _c1d_dg(seen):
    return [(r, i.get("stage"), i.get("codes"), i.get("failed_facts"), (i.get("provenance") or {}).get("outcome")) for c, r, _t, i in seen if c == "DATA_GATE"]


@pytest.mark.parametrize("step", ["P8a-1", "P8b", "P8a-2"])
def test_c1d_l3_each_exit_gate_short_circuit_writes_its_data_gate_row_beside_today_s_rows(tmp_path, step):
    """c1d-fix2 L-3 (B2 review LOW L-3; r14 3.11, r14:952 'every verdict is a DATA_GATE row'): P8a-1, P8b and P8a-2 at the exit gate each write
    one DATA_GATE BLOCK row in the pure gate's form (the stage, the code, the failed fact and the outcome), in the same evaluation as the
    EXIT_SUBMISSION (and, for P8b, PROTECTIVE_EXIT) rows."""
    if step == "P8a-1":
        rig = Rig(tmp_path, t0=datetime(2026, 11, 11, 1, 0, tzinfo=UTC))                  # 20:00 New York: P8a-1 defers with no broker read
        p = rig.open()
        rig.live("SNDK", "8.50")
    else:
        rig, p = hit_rig(tmp_path)
        rig.broker.faults["clock"] = [None, BK.BrokerError("TIMEOUT") if step == "P8b" else clock_answer(rig, is_open=False)]
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    code = "P8_CLOCK_UNREADABLE" if step == "P8b" else step
    assert _c1d_dg(seen) == [("BLOCK", DG.EXIT_GATE, [code], [step], DG.REFUSE if step == "P8b" else DG.DEFER)]
    row = next(i for c, _r, _t, i in seen if c == "DATA_GATE")
    assert row["position_id"] == p.position_id and row["symbol"] == "SNDK" and row["code"] == code
    sub = [(r, i.get("step")) for c, r, _t, i in seen if c == "EXIT_SUBMISSION"]
    assert sub == [("BLOCK" if step == "P8b" else "WARN", step)] and rig.posts() == 0
    assert [c for c, _r, _t, _i in seen if c == "PROTECTIVE_EXIT"] == (["PROTECTIVE_EXIT"] if step == "P8b" else [])


@pytest.mark.parametrize("step", ["P8a", "P8b"])
def test_c1d_l3_each_dg_final_short_circuit_writes_its_data_gate_row(tmp_path, step):
    """c1d-fix2 L-3: a DG-final session failure (P8a) and a DG-final P8b refusal each write one DATA_GATE BLOCK row (stage DG_FINAL, with the
    intent's execution_id) after the exit gate's PASS row, beside today's NOT_SENT handling."""
    rig, p = hit_rig(tmp_path)
    if step == "P8a":
        rig.x.hooks["after_exit_intent"] = lambda ctx: setattr(rig.sim, "is_open", False)        # a verified clock saying closed at DG-final: P8a-2
    else:
        rig.x.hooks["after_exit_intent"] = lambda ctx: rig.broker.faults.__setitem__("clock", BK.BrokerError("TIMEOUT"))
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    it = rig.exits(p)[0]
    want = ("BLOCK", DG.DG_FINAL, ["DG_FINAL_SESSION_FAILED"], ["P8a-2"], DG.SESSION_FAILURE) if step == "P8a" else \
        ("BLOCK", DG.DG_FINAL, ["P8_CLOCK_UNREADABLE"], ["P8b"], DG.REFUSE)
    dg = _c1d_dg(seen)
    assert len(dg) == 2 and dg[0][:2] == ("PASS", DG.EXIT_GATE) and dg[1] == want
    assert [i for c, _r, _t, i in seen if c == "DATA_GATE"][1]["execution_id"] == it.execution_id
    assert rig.posts() == 0 and it.state == "NOT_SENT" and it.attempt_count == 0
    assert it.failure_code == ("DG_FINAL_SESSION_FAILED" if step == "P8a" else "DG_FINAL_REFUSED")


def test_c1d_l3_the_dg_final_unknown_exit_reason_refusal_writes_its_data_gate_row(tmp_path):
    """c1d-fix2 L-3: the DG-final unknown-reason refusal (C1C1-A12, DG_EXIT_REASON_UNKNOWN) writes a DATA_GATE BLOCK row; an unknown reason is
    not HARD_INVALIDATION, so the order class is a strategy exit and the outcome a hold."""
    rig = Rig(tmp_path)
    p = rig.open()
    it = rig.store.insert_intent(Intent(execution_id=exit_id_for(p.position_id, 1), intent_type="EXIT", symbol="SNDK", side="sell", qty=10, client_order_id=exit_coid(p.position_id, 1),
                                        position_id=p.position_id, exit_cycle=1, exit_reason="NOT_A_REASON", state="PENDING", created_at=iso(rig.t), updated_at=iso(rig.t)))
    seen = _c1d_touch_spy(rig)
    rig.x._submit(it, rig.t, "EXIT_SUBMISSION", recheck=None)
    assert _c1d_dg(seen) == [("BLOCK", DG.DG_FINAL, ["DG_EXIT_REASON_UNKNOWN"], ["REASON"], DG.HOLD)]
    row = next(i for c, _r, _t, i in seen if c == "DATA_GATE")
    assert row["execution_id"] == it.execution_id and row["provenance"]["exit_reason"] == "NOT_A_REASON"
    back = rig.store.intent(it.execution_id)
    assert rig.posts() == 0 and back.state == "NOT_SENT" and back.failure_code == "DG_FINAL_REFUSED" and "EXIT_SUBMISSION" in [c for c, *_ in seen]
