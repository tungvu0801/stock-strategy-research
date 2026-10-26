"""
Stage 6.0 — AUTONOMOUS ALPACA PAPER TRADING. Focused tests against the REAL stack (setups -> proposals -> safety gate -> executor) with the deterministic
simulated PAPER broker below the real paper client (no socket is ever opened):
  P paper-only boundary · M modes · E entry preconditions and the single entry · X exactly-once / ambiguous POST · C crash points A–F · O order lifecycle ·
  T deterministic exits · R reconciliation · S restart at every state · B broker failure script · N boundaries (no Robinhood / LLM / 4.6B / live) ·
  A API · U frontend · Z base pins
"""
import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
from freeze_ledger import view as ledger_view, view_bytes as ledger_bytes  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import execution as X  # noqa: E402
from execution import broker as BK  # noqa: E402
from execution import engine as EN  # noqa: E402
from execution.models import Intent, Position  # noqa: E402
from execution.scheduler import LEASE_NAME, ExecutionScheduler, start_if_enabled  # noqa: E402
from execution.service import ExecutionRefused, ExecutionService, SimulatedCrash  # noqa: E402
from execution.simulated import SimulatedPaperBroker  # noqa: E402
from execution.store import ExecutionStore, ExecutionStoreError  # noqa: E402
from database.execution_migrations import TABLES, run_execution_migrations  # noqa: E402
from proposals.account import AccountAdapter  # noqa: E402
from proposals.service import ProposalService  # noqa: E402
from proposals.store import ProposalStore  # noqa: E402
from safety.service import ExecutionSafetyService  # noqa: E402
from safety.store import SafetyStore  # noqa: E402
from setups.models import EpisodeRecord, SetupInputs  # noqa: E402
from setups.service import SetupService  # noqa: E402
from setups.store import SetupStore  # noqa: E402
from trader_view import MESSAGE_TYPES  # noqa: E402
from trader_view.models import TerminalState  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_trader_view_55 import FORBIDDEN_IMPORTS, T0, WL, FakePositions, FakeResearch  # noqa: E402
from test_setups_58 import IN_SESSION, READY_IN, WATCH_IN, FakeClock, fx, strip_code  # noqa: E402

BASE = "a58297a"                                                       # origin/main the stage was branched from (Stage 5.9.1 merged)
UTC = timezone.utc
EXEC_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "execution").glob("*.py")) + ["api/routes/execution.py", "database/execution_migrations.py"]
BOT_JS = (ROOT / "frontend" / "paper_bot.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
STREAM_SRC = (ROOT / "trader_view" / "stream.py").read_text(encoding="utf-8")
TRADE_WORDS = re.compile(r"(?i)\b(buy now|sell now|enter now|trade now|recommend\w*|you should|approved|ready to buy|short it|go long)\b")
CONTRADICTION = dict(stretch=-60.0, confidence=30, demand=40, evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="STRONG", evidence_created_at="2099-01-01T00:00:00+00:00",
                     evidence_packet_id="q" * 32)


def source_of(files):
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in files)


@pytest.fixture(autouse=True)
def no_execution_wire(monkeypatch):
    """The execution layer's real network adapter is a tripwire in EVERY test; only the simulated broker is ever reached."""
    hits = []

    def tripwire():
        hits.append("wire")
        raise AssertionError("the real Alpaca paper network adapter was requested by the execution layer during a test")
    monkeypatch.setattr(BK, "WIRE", tripwire)
    yield
    assert not hits


@pytest.fixture(autouse=True)
def execution_config(monkeypatch):
    monkeypatch.setattr(config, "TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", False)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", False)
    monkeypatch.setattr(config, "EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", False)
    monkeypatch.setattr(config, "EXECUTION_SAFETY_STARTUP_POLICY", "KEEP")
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(config, "EXECUTION_MAX_SHARES_PER_ENTRY", 0)


# ---------------------------------------------------------------- the stack (the real path) --------------------------------------------------

def make_stack(tmp_path, clock=None, **kw):
    holder = {}
    clock = clock or FakeClock()
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "stage60.db"
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(db), clock=clock, cooldown_minutes=15)
    safety = ExecutionSafetyService(stream_fn=lambda: holder["s"], store=SafetyStore(db), clock=clock)
    psvc = ProposalService(stream_fn=lambda: holder["s"], store=ProposalStore(db), account=AccountAdapter(path=db), clock=clock, safety=safety)
    sim = kw.pop("sim", None) or SimulatedPaperBroker(now_fn=clock)
    broker = kw.pop("broker", None) or BK.AlpacaPaperBroker(simulated=True, wire=lambda: sim)
    xsvc = ExecutionService(stream_fn=lambda: holder["s"], store=ExecutionStore(db), broker=broker, clock=clock)
    s = TerminalStream(SimulatedMarket(seed=11, start_time=T0), FakePositions(), FakeResearch(), watchlist_fn=lambda: list(WL), tick_seconds=1.0, allow_manual_tick=True, run_loop=False,
                       now_fn=lambda: T0, setup_service=svc, proposal_service=psvc, safety_service=safety, execution_service=xsvc, **kw)
    holder["s"] = s
    return SimpleNamespace(s=s, setups=svc, proposals=psvc, safety=safety, x=xsvc, sim=sim, clock=clock, db=db, holder=holder)


def reopen(st):
    """A process restart: a NEW executor on the same database and the same broker; nothing in memory survives."""
    st.x = ExecutionService(stream_fn=lambda: st.holder["s"], store=ExecutionStore(st.db), broker=BK.AlpacaPaperBroker(simulated=True, wire=lambda: st.sim), clock=st.clock)
    st.holder["s"].execution = st.x
    return st.x


def go_ready(st, symbol="SNDK"):
    st.setups.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **WATCH_IN))
    st.clock.advance(60)
    st.setups.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **READY_IN))
    pv = st.proposals.current(symbol, st.clock.now)
    assert pv.effective_status == "REVIEWABLE" and pv.final_qty >= 1, (pv.effective_status, pv.blockers)
    st.sim.set_price(symbol, float(pv.reference_price))
    return pv


def arm(st, symbol="SNDK"):
    pv = go_ready(st, symbol)
    st.x.enable_auto(actor="test", now=st.clock.now)
    return pv


def cyc(st, advance=30, trigger="TEST"):
    st.clock.advance(advance)
    return st.x.run_cycle(st.clock.now, trigger)


def entered(st, symbol="SNDK"):
    """READY -> enabled -> one cycle (entry POST) -> one cycle (fill) -> an open position."""
    pv = arm(st, symbol)
    cyc(st)
    cyc(st)
    pos = st.x.store.open_position(symbol)
    assert pos is not None and pos.quantity == pv.final_qty, st.x.store.intents()
    return pv, pos


def below_invalidation(st, pv):
    inv = Decimal(pv.invalidation_price) - Decimal("0.50")
    st.x.price_fn = lambda sym: (inv, "TEST", st.clock.now.isoformat(timespec="seconds"))
    st.sim.set_price(pv.symbol, float(inv))


def ctl(st, cid):
    return {c["control_id"]: c for c in st.x.store.controls()}[cid]


def boom(ctx):
    raise SimulatedCrash("simulated process death")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    monkeypatch.setitem(RT.STREAM, "instance", st.s)
    from api.server import app
    st.api = TestClient(app)
    return st


# ================================================================ P paper-only boundary ========================================================

def test_P01_paper_host_is_a_constant_and_no_live_endpoint_exists_in_the_execution_layer():
    assert BK.PAPER_HOST == "paper-api.alpaca.markets" and BK.BASE == "https://paper-api.alpaca.markets"
    src = source_of(EXEC_FILES)
    for m in re.finditer(r"api\.alpaca\.markets", src):
        assert src[max(0, m.start() - 6):m.start()] == "paper-", src[max(0, m.start() - 40):m.end() + 10]
    assert not re.search(r"TradingClient|TradingStream|alpaca\.trading|url_override|TRADING_LIVE|base_url|live_mode|LIVE_CONFIRM|AUTO_LIVE", src)
    import inspect
    assert not {"url", "host", "base", "base_url", "endpoint"} & set(inspect.signature(BK.AlpacaPaperBroker.__init__).parameters)
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")


def test_P02_transport_refuses_any_other_host_scheme_port_verb_or_path_before_the_wire(tmp_path):
    sim = SimulatedPaperBroker()
    import requests
    t = BK._Transport(sim, budget=5)
    http = requests.Session()
    http.mount("https://", t)
    http.mount("http://", t)
    for url in ("https://api.alpaca.markets/v2/account", "http://paper-api.alpaca.markets/v2/account", "https://paper-api.alpaca.markets:8443/v2/account",
                "https://paper-api.alpaca.markets/v2/account/activities", "https://paper-api.alpaca.markets/v2/orders:by_client_order_id?client_order_id=sa46b-" + "0" * 32,
                "https://paper-api.alpaca.markets/v2/watchlists"):
        with pytest.raises(BK.BrokerRefused):
            http.get(url)
    with pytest.raises(BK.BrokerRefused):
        http.post("https://paper-api.alpaca.markets/v2/orders", data=b"{}")              # a read transport never POSTs
    with pytest.raises(BK.BrokerRefused):
        http.delete("https://paper-api.alpaca.markets/v2/orders/" + "0" * 36)
    assert sim.requests == []
    tp = BK._Transport(sim, post_body=b'{"a":1}', budget=0)
    http2 = requests.Session()
    http2.mount("https://", tp)
    with pytest.raises(BK.BrokerRefused):
        http2.post("https://paper-api.alpaca.markets/v2/orders", data=b'{"a":2}')        # body must equal the stored payload
    with pytest.raises(BK.BrokerRefused):
        http2.post("https://paper-api.alpaca.markets/v2/orders?x=1", data=b'{"a":1}')
    assert sim.requests == []


def test_P03_post_order_refuses_foreign_ids_and_non_canonical_payloads_and_needs_credentials():
    sim = SimulatedPaperBroker()
    from paper import alpaca_order_rules as RU
    b = BK.AlpacaPaperBroker(simulated=True, wire=lambda: sim)
    good = RU.canonical_payload("stockagent-entry-" + "a" * 32, "SNDK", "buy", 3)
    with pytest.raises(BK.BrokerRefused):
        b.post_order(RU.canonical_payload("sa46b-" + "a" * 32, "SNDK", "buy", 3), "sa46b-" + "a" * 32)
    with pytest.raises(BK.BrokerRefused):
        b.post_order(good.replace(b",", b", "), "stockagent-entry-" + "a" * 32)                    # same fields, not the canonical bytes
    with pytest.raises(BK.BrokerRefused):
        b.post_order(RU.canonical_payload("stockagent-entry-" + "b" * 32, "SNDK", "buy", 0), "stockagent-entry-" + "b" * 32)
    assert sim.posts == []
    unconfigured = BK.AlpacaPaperBroker(credentials_fn=lambda: (None, None), wire=lambda: sim)
    assert unconfigured.post_order(good, "stockagent-entry-" + "a" * 32) == {"kind": "not_sent", "reason": "NOT_CONFIGURED"}
    assert unconfigured.environment()["paper"] is False and unconfigured.environment()["error"] == "NOT_CONFIGURED"
    assert sim.posts == [] and sim.requests == []
    assert b.environment()["paper"] is True and b.environment()["simulated"] is True and b.environment()["host"] == BK.PAPER_HOST


def test_P04_paper_auto_cannot_be_enabled_without_the_paper_proof(tmp_path):
    sim = SimulatedPaperBroker()
    st = make_stack(tmp_path, broker=BK.AlpacaPaperBroker(credentials_fn=lambda: (None, None), wire=lambda: sim))
    with pytest.raises(ExecutionRefused) as e:
        st.x.enable_auto(actor="test", now=st.clock.now)
    assert e.value.code == "PAPER_ENVIRONMENT_UNPROVEN" and st.x.mode()["mode"] == "MONITOR_ONLY" and sim.requests == []
    st.sim = sim
    reopen(st)
    st.sim.account["trading_blocked"] = True
    with pytest.raises(ExecutionRefused) as e2:
        st.x.enable_auto(actor="test", now=st.clock.now)
    assert e2.value.code == "BROKER_ACCOUNT_NOT_ACTIVE" and st.x.mode()["mode"] == "MONITOR_ONLY"


# ================================================================ M modes ======================================================================

def test_M01_default_monitor_only_idle_cycle_touches_neither_broker_nor_database(tmp_path):
    st = make_stack(tmp_path)
    assert st.x.mode() == {"mode": "MONITOR_ONLY", "changed_at": None, "changed_by": None, "reason": None, "environment": {}, "persisted": False}
    out = st.x.run_cycle(st.clock.now, "TEST")
    assert out["result"] == "IDLE" and st.sim.requests == [] and st.x.ready
    assert all(v == 0 for v in st.x.store.counts().values()), st.x.store.counts()
    assert st.x.status().headline == "Monitor only" and st.x.status().icon == "⚪"


def test_M02_enable_persists_paper_auto_with_the_environment_proof_and_a_restart_restores_it_after_reconciliation(tmp_path):
    st = make_stack(tmp_path)
    pv = go_ready(st)
    out = st.x.enable_auto(actor="user", reason="first paper run", now=st.clock.now)
    m = st.x.mode()
    assert out["mode"] == "PAPER_AUTO" and m["persisted"] and m["changed_by"] == "user" and m["environment"]["paper"] is True and m["environment"]["simulated"] is True
    assert [e["to_mode"] for e in st.x.store.mode_events()] == ["PAPER_AUTO"]
    assert st.sim.posts == []                                                                 # enabling submits nothing
    new = reopen(st)
    assert new.mode()["mode"] == "PAPER_AUTO" and new.ready is False
    st.sim.requests.clear()
    cyc(st)
    paths = [p for m_, p, _ in st.sim.requests if m_ == "GET"]
    first_post = next(i for i, (m_, p, _) in enumerate(st.sim.requests) if m_ == "POST")
    assert "/v2/positions" in paths and "/v2/orders" in paths
    assert st.sim.requests.index(("GET", "/v2/positions", "")) < first_post, st.sim.requests  # startup reconciliation before any write
    assert len(st.sim.posts) == 1 and st.x.store.intents()[0].proposal_id == pv.proposal_id


def test_M03_disable_stops_new_entries_while_monitoring_and_exits_continue(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    st.x.disable_auto(actor="user", now=st.clock.now)
    assert st.x.mode()["mode"] == "MONITOR_ONLY"
    go_ready(st, "NVDA")
    out = cyc(st)
    assert out["result"] == "RUN" and "entries" not in out["summary"] and len(st.sim.posts) == 1          # still monitored (not idle), no new entry
    below_invalidation(st, pv)
    out = cyc(st)
    assert out["summary"]["exits_submitted"] == 1 and len(st.sim.posts) == 2 and json.loads(st.sim.posts[1])["side"] == "sell"
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED"
    assert st.x.store.open_position("NVDA") is None


def test_M04_enable_refused_when_the_gate_is_unknown_or_reconciliation_is_unhealthy(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    go_ready(st)
    from safety.models import SafetyView
    mp = pytest.MonkeyPatch()
    mp.setattr(st.safety, "current", lambda now=None, **k: SafetyView(state="UNKNOWN", state_label="Trading stopped", icon="🔴", error="x"))
    with pytest.raises(ExecutionRefused) as e:
        st.x.enable_auto(actor="test", now=st.clock.now)
    mp.undo()
    assert e.value.code == "SAFETY_GATE_UNKNOWN"
    # a broker order carrying our prefix that no intent knows -> reconciliation unhealthy -> refused
    st.sim.auto_fill = "never"
    o = st.sim._order_json({"client_order_id": "stockagent-entry-" + "f" * 32, "symbol": "AMD", "qty": "1", "type": "market", "side": "buy", "time_in_force": "day"})
    st.sim.orders[o["id"]] = o
    st.x.store.set_mode("PAPER_AUTO", st.clock.now, "test", None, {})                       # pretend a persisted mode so the startup reconciliation queries the broker
    st.x.store.set_mode("MONITOR_ONLY", st.clock.now, "test", None, {})
    st.x.ready = False
    st.x.startup(st.clock.now)
    assert st.x.recon_healthy is True                                                        # MONITOR_ONLY with nothing open: the broker is not consulted
    st.x.store.set_mode("PAPER_AUTO", st.clock.now, "test", None, {})
    st.x.ready = False
    with pytest.raises(ExecutionRefused) as e2:
        st.x.enable_auto(actor="test", now=st.clock.now)
    assert e2.value.code == "RECONCILIATION_REQUIRED" and [f["kind"] for f in st.x.store.findings()] == ["UNKNOWN_INTERNAL_ORDER"]


# ================================================================ E entries ====================================================================

def test_E01_one_entry_exactly_one_post_canonical_payload_and_the_intent_is_persisted_before_the_wire(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    seen = []
    st.sim.on_post = lambda req: seen.append([i.model_dump() for i in ExecutionStore(st.db).intents()])
    out = cyc(st)
    assert out["summary"]["entries"]["submitted"] == ["SNDK"] and len(st.sim.posts) == 1
    body = json.loads(st.sim.posts[0])
    xid = EN.execution_id_for(pv.proposal_id)
    assert body == {"client_order_id": "stockagent-entry-" + xid, "qty": str(pv.final_qty), "side": "buy", "symbol": "SNDK", "time_in_force": "day", "type": "market"}
    assert len(seen) == 1 and len(seen[0]) == 1 and seen[0][0]["state"] == "PENDING" and seen[0][0]["attempt_count"] == 1 and seen[0][0]["broker_order_id"] is None
    it = st.x.store.intents()[0]
    assert it.state == "SUBMITTED" and it.broker_order_id and it.proposal_id == pv.proposal_id and it.setup_id == pv.setup_id and it.qty == pv.final_qty == it.proposal_qty
    assert it.reference_price == pv.reference_price and it.invalidation_price == pv.invalidation_price and it.risk_budget == pv.risk_budget
    assert ctl(st, "ENTRY_SUBMISSION")["fire_count"] == 1 and ctl(st, "PROPOSAL_VALIDITY")["last_result"] == "PASS"
    assert st.x.card("SNDK").key == "ENTERING" and st.x.card("SNDK").state == "ENTRY_SUBMITTED"


def test_E02_position_created_only_from_the_broker_fill(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    o = st.sim.order_by_coid(st.x.store.intents()[0].client_order_id)
    assert (pos.quantity, pos.entry_qty, pos.average_entry_price) == (int(o["filled_qty"]), int(o["filled_qty"]), o["filled_avg_price"])
    assert pos.source == "BROKER_FILLS" and pos.broker == "SIMULATED_PAPER" and pos.invalidation_price == pv.invalidation_price and pos.state in ("OPEN", "HEALTHY")
    assert [e["kind"] for e in st.x.store.position_events(pos.position_id)][:1] == ["OPENED"]
    card = st.x.card("SNDK")
    assert (card.key, card.label, card.icon, card.state, card.shares, card.entry) == ("IN_TRADE", "In trade", "🟢", "POSITION_OPEN", pos.quantity, pos.average_entry_price)


def test_E03_no_duplicate_entry_for_the_same_proposal_or_symbol(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    for _ in range(3):
        out = cyc(st)
        assert out["summary"]["entries"]["submitted"] == [] and set(out["summary"]["entries"]["blocked"]["SNDK"]) >= {"DUPLICATE_ENTRY", "BROKER_POSITION"}
    assert len(st.sim.posts) == 1
    it = st.x.store.intents()[0]
    with pytest.raises(ExecutionStoreError):
        st.x.store.insert_intent(it.model_copy(update={"client_order_id": "stockagent-entry-" + "0" * 32}))
    assert EN.execution_id_for(pv.proposal_id) == it.execution_id and it.client_order_id == "stockagent-entry-" + it.execution_id


def _blocked(st, out, sym="SNDK"):
    return set((out["summary"].get("entries") or {}).get("blocked", {}).get(sym, []))


def test_E04a_safety_states_block_entries(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.safety.pause(actor="user")
    assert "SAFETY_GATE" in _blocked(st, cyc(st)) and st.sim.posts == []
    st.safety.resume(actor="user")
    st.safety.emergency_stop(actor="user")
    assert "SAFETY_GATE" in _blocked(st, cyc(st)) and st.sim.posts == []
    st.safety.clear_emergency_stop(actor="user")
    st.safety.overrides = {"equity": "94000.00"}
    st.safety.evaluate("TEST", st.clock.now)
    assert st.safety.current(now=st.clock.now).state == "HALTED"
    assert "SAFETY_GATE" in _blocked(st, cyc(st)) and st.sim.posts == []
    assert ctl(st, "SAFETY_GATE")["fire_count"] >= 3


def test_E04b_proposal_validity_blocks_stale_expired_and_blocked_proposals(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    st.clock.advance(400)                                                                      # past the proposal TTL without a re-evaluation
    assert st.proposals.current("SNDK", st.clock.now).effective_status == "EXPIRED"
    assert "PROPOSAL_VALIDITY" in _blocked(st, cyc(st, 1)) and st.sim.posts == [] and ctl(st, "PROPOSAL_VALIDITY")["fire_count"] == 1
    st2 = make_stack(tmp_path / "stale")
    arm(st2)
    st2.setups.evaluate_symbol("SNDK", trigger="TEST", inputs=fx("SNDK", stretch=-54.0, confidence=58, demand=44))    # READY -> WATCH
    assert st2.proposals.current("SNDK", st2.clock.now).effective_status == "STALE"
    out = cyc(st2)
    assert "SNDK" not in (out["summary"]["entries"]["candidates"]) and st2.sim.posts == []                            # not READY: not even a candidate
    assert pv.proposal_id and st.x.store.intents() == [] and st2.x.store.intents() == []


def test_E04c_session_policy_blocks_outside_the_regular_session_near_the_close_and_when_the_broker_clock_is_closed(tmp_path):
    e = EN.session_policy(datetime(2026, 10, 24, 15, 0, tzinfo=UTC), {"is_open": True}, 15)                           # Saturday
    assert e.result == "BLOCK" and "outside" in e.reason
    e = EN.session_policy(datetime(2026, 10, 21, 19, 50, tzinfo=UTC), {"is_open": True}, 15)                           # 15:50 ET
    assert e.result == "BLOCK" and "final 15" in e.reason
    e = EN.session_policy(datetime(2026, 10, 21, 15, 0, tzinfo=UTC), {"is_open": False}, 15)                           # a holiday the weekday rule cannot see
    assert e.result == "BLOCK" and "broker clock" in e.reason
    assert EN.session_policy(datetime(2026, 10, 21, 15, 0, tzinfo=UTC), None, 15).result == "PASS"
    st = make_stack(tmp_path)
    arm(st)
    st.sim.is_open = False
    assert "SESSION_POLICY" in _blocked(st, cyc(st)) and st.sim.posts == []
    st.sim.is_open = True
    st.clock.now = datetime(2026, 10, 21, 19, 50, tzinfo=UTC)
    st.proposals.evaluate("SNDK", st.clock.now, "TEST")
    assert "SESSION_POLICY" in _blocked(st, cyc(st, 1)) and st.sim.posts == []


def test_E04d_broker_tradability_duplicates_position_account_cap_and_reconciliation_block(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.untradable.add("SNDK")
    assert "BROKER_TRADABILITY" in _blocked(st, cyc(st))
    st.sim.untradable.clear()
    st.sim.unknown_assets.add("SNDK")
    assert "BROKER_TRADABILITY" in _blocked(st, cyc(st))
    st.sim.unknown_assets.clear()
    o = st.sim._order_json({"client_order_id": "manual-1", "symbol": "SNDK", "qty": "1", "type": "market", "side": "buy", "time_in_force": "day"})
    st.sim.orders[o["id"]] = o
    st.sim.auto_fill = "never"
    assert "DUPLICATE_ENTRY" in _blocked(st, cyc(st))
    del st.sim.orders[o["id"]]
    st.sim.auto_fill = "next_read"
    st.sim.set_position("SNDK", 2, 100.0)
    assert "BROKER_POSITION" in _blocked(st, cyc(st))
    st.sim.set_position("SNDK", 0)
    st.sim.account["buying_power"] = "10.00"
    assert "BROKER_ACCOUNT" in _blocked(st, cyc(st))
    st.sim.account["buying_power"] = "200000.00"
    st.sim.account["status"] = "INACTIVE"
    assert "BROKER_ACCOUNT" in _blocked(st, cyc(st))
    st.sim.account["status"] = "ACTIVE"
    st.x.max_open_positions = 1
    st.sim.set_position("AMD", 5, 50.0)                                                         # an external position counts toward the cap
    out = cyc(st)
    assert "MAX_OPEN_POSITIONS" in _blocked(st, out) and [f["kind"] for f in st.x.store.findings() if f["symbol"] == "AMD"] == ["EXTERNAL_POSITION"]
    st.x.max_open_positions = 10
    u = st.sim._order_json({"client_order_id": "stockagent-entry-" + "e" * 32, "symbol": "MU", "qty": "1", "type": "market", "side": "buy", "time_in_force": "day"})
    st.sim.orders[u["id"]] = u
    st.sim.auto_fill = "never"
    assert "BROKER_RECONCILIATION" in _blocked(st, cyc(st))
    assert st.sim.posts == []
    for cid in ("BROKER_TRADABILITY", "DUPLICATE_ENTRY", "BROKER_POSITION", "BROKER_ACCOUNT", "MAX_OPEN_POSITIONS", "BROKER_RECONCILIATION"):
        assert ctl(st, cid)["fire_count"] >= 1, cid


def test_E05_quantity_is_the_proposal_quantity_exactly_unless_an_explicit_operator_cap_is_set(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    cyc(st)
    assert json.loads(st.sim.posts[0])["qty"] == str(pv.final_qty) and pv.final_qty > 1
    st2 = make_stack(tmp_path / "cap")
    st2.x.qty_cap = 1
    pv2 = arm(st2)
    cyc(st2)
    it = st2.x.store.intents()[0]
    assert json.loads(st2.sim.posts[0])["qty"] == "1" and it.qty == 1 and it.proposal_qty == pv2.final_qty
    assert EN.entry_quantity(7, 0) == 7 and EN.entry_quantity(7, 3) == 3 and EN.entry_quantity(2, 5) == 2


def test_E06_long_only_and_market_day_whole_shares(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    short = pv.model_copy(update={"side_context": "SHORT_REVIEW"})
    mp = pytest.MonkeyPatch()
    mp.setattr(st.proposals, "current", lambda symbol, now=None: short)
    assert "PROPOSAL_VALIDITY" in _blocked(st, cyc(st)) and st.sim.posts == []
    mp.undo()
    cyc(st)
    b = json.loads(st.sim.posts[0])
    assert b["side"] == "buy" and b["type"] == "market" and b["time_in_force"] == "day" and "extended_hours" not in b and "limit_price" not in b and "order_class" not in b
    assert all(i.side == "buy" for i in st.x.store.intents(intent_type="ENTRY"))


def test_E07_at_most_one_new_entry_per_cycle(tmp_path):
    st = make_stack(tmp_path)
    arm(st, "SNDK")
    go_ready(st, "NVDA")
    out = cyc(st)
    assert len(out["summary"]["entries"]["submitted"]) == 1 and len(st.sim.posts) == 1
    out = cyc(st)
    assert len(out["summary"]["entries"]["submitted"]) == 1 and len(st.sim.posts) == 2
    assert {json.loads(b)["symbol"] for b in st.sim.posts} == {"SNDK", "NVDA"}


def test_E08_the_last_recheck_before_the_wire_catches_a_change(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.x.hooks["after_intent"] = lambda ctx: st.safety.pause(actor="user")
    out = cyc(st)
    it = st.x.store.intents()[0]
    assert st.sim.posts == [] and it.state == "NOT_SENT" and it.failure_code == "PRECHECK_FAILED" and it.attempt_count == 0 and "safety gate" in it.failure_text
    assert out["summary"]["entries"]["submitted"] == ["SNDK"]


# ================================================================ X exactly-once / ambiguous ===================================================

def test_X01_ambiguous_timeout_after_the_broker_recorded_the_order_is_never_retried_and_the_order_is_adopted(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.post_modes = ["timeout_after_record"]
    cyc(st)
    it = st.x.store.intents()[0]
    assert it.state == "RECONCILIATION_REQUIRED" and it.outcome_class == "O8" and it.failure_code == "UNCERTAIN_TIMEOUT" and it.attempt_count == 1 and it.broker_order_id is None
    assert st.x.card("SNDK").key == "ATTENTION" and st.x.status().attention
    cyc(st)                                                                                   # the lookup finds the order: adopted, no second POST
    it = st.x.store.intents()[0]
    assert it.state in ("SUBMITTED", "FILLED") and it.broker_order_id == st.sim.order_by_coid(it.client_order_id)["id"] and len(st.sim.posts) == 1
    assert "ORDER_ADOPTED" in [f["kind"] for f in st.x.store.findings()]
    cyc(st)
    assert st.x.store.open_position("SNDK").quantity == it.qty and len(st.sim.posts) == 1


def test_X02_ambiguous_timeout_before_the_broker_recorded_anything_ends_not_sent_after_two_spaced_lookups(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.post_modes = ["timeout_before"]
    cyc(st)
    it = st.x.store.intents()[0]
    assert it.state == "RECONCILIATION_REQUIRED" and it.lookup_count == 0
    cyc(st, 10)                                                                               # first lookup
    assert st.x.store.intents()[0].lookup_count == 1 and st.x.store.intents()[0].state == "RECONCILIATION_REQUIRED"
    cyc(st, 10)                                                                               # 10 s later: too soon for a second lookup
    assert st.x.store.intents()[0].lookup_count == 1
    cyc(st, 30)
    it = st.x.store.intents()[0]
    assert it.state == "NOT_SENT" and it.lookup_count == 2 and it.failure_code == "NOT_FOUND_AFTER_LOOKUPS" and len(st.sim.posts) == 0
    assert "ORDER_NOT_FOUND_FINAL" in [f["kind"] for f in st.x.store.findings()]
    for _ in range(3):
        cyc(st)
    assert len(st.sim.posts) == 0 and st.x.store.intents()[0].state == "NOT_SENT"           # the proposal is burnt: no blind resubmission


@pytest.mark.parametrize("mode,klass,code", [("http:429", "O8", "UNCERTAIN_HTTP_429"), ("http_after_record:500", "O8", "UNCERTAIN_HTTP_500"), ("garbage200", "O2", "UNREADABLE_200"),
                                             ("inconsistent200", "O2", "INCONSISTENT_200"), ("reset_after_record", "O8", "UNCERTAIN_CONNECTION")])
def test_X03_uncertain_http_and_transport_results_need_reconciliation_and_are_never_retried(tmp_path, mode, klass, code):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.post_modes = [mode]
    cyc(st)
    it = st.x.store.intents()[0]
    assert (it.state, it.outcome_class, it.failure_code) == ("RECONCILIATION_REQUIRED", klass, code), it
    for _ in range(4):
        cyc(st, 31)
    it = st.x.store.intents()[0]
    recorded = st.sim.order_by_coid(it.client_order_id) is not None
    assert len([b for b in st.sim.posts]) <= 1 and st.x.broker.posts == 1
    assert (it.state in ("SUBMITTED", "FILLED") and it.broker_order_id) if recorded else it.state == "NOT_SENT"


def test_X04_duplicate_client_order_id_adopts_the_existing_order_or_flags_an_inconsistent_one(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    coid = EN.entry_coid(EN.execution_id_for(pv.proposal_id))
    o = st.sim._order_json({"client_order_id": coid, "symbol": "SNDK", "qty": str(pv.final_qty), "type": "market", "side": "buy", "time_in_force": "day"}, "canceled")
    st.sim.orders[o["id"]] = o                                                                  # the broker already knows our id (a closed order): no open order, no position
    cyc(st)
    it = st.x.store.intents()[0]
    assert len(st.sim.posts) == 1 and it.outcome_class == "O4" and it.broker_order_id == o["id"] and it.state == "CANCELED" and st.x.broker.posts == 1
    cyc(st)
    assert len(st.sim.posts) == 1 and st.x.store.open_position("SNDK") is None
    st2 = make_stack(tmp_path / "b")
    pv2 = arm(st2)
    coid2 = EN.entry_coid(EN.execution_id_for(pv2.proposal_id))
    o2 = st2.sim._order_json({"client_order_id": coid2, "symbol": "SNDK", "qty": str(pv2.final_qty + 1), "type": "market", "side": "buy", "time_in_force": "day"}, "canceled")
    st2.sim.orders[o2["id"]] = o2
    cyc(st2)
    it2 = st2.x.store.intents()[0]
    assert it2.state == "RECONCILIATION_REQUIRED" and it2.failure_code == "INCONSISTENT_ORDER" and it2.broker_order_id is None
    assert "UNKNOWN_STATUS" in [f["kind"] for f in st2.x.store.findings()] and len(st2.sim.posts) == 1
    for _ in range(3):
        cyc(st2, 31)
    assert len(st2.sim.posts) == 1


def test_X05_definitive_rejects_end_the_intent_without_resubmission(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.post_modes = ['http:403:{"code":40310000,"message":"insufficient buying power"}']
    cyc(st)
    it = st.x.store.intents()[0]
    assert (it.state, it.outcome_class, it.failure_code) == ("REJECTED", "O6", "INSUFFICIENT_BUYING_POWER") and it.resolved_at
    for _ in range(3):
        cyc(st)
    assert len(st.sim.posts) == 1 and st.x.store.open_position("SNDK") is None and st.x.card("SNDK").key == "ATTENTION" and st.x.card("SNDK").state == "FAILED"
    assert st.x.history()[0]["kind"] == "NO_TRADE" and st.x.history()[0]["code"] == "INSUFFICIENT_BUYING_POWER"
    st2 = make_stack(tmp_path / "b")
    arm(st2)
    st2.sim.post_modes = ["reject"]                                                            # a 200 whose status is rejected
    cyc(st2)
    it2 = st2.x.store.intents()[0]
    assert it2.state == "REJECTED" and it2.failure_code == "BROKER_REJECTED" and it2.broker_order_id
    cyc(st2)
    assert len(st2.sim.posts) == 1


def test_X06_the_store_enforces_the_exactly_once_invariants():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    run_execution_migrations(conn)
    for t in TABLES:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (t,)).fetchone()
    now = "2026-10-21T15:00:00+00:00"
    conn.execute("INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, state, created_at, updated_at) VALUES ('x', 'ENTRY', 'SNDK', 'buy', 3, 'stockagent-entry-x', 'PENDING', ?, ?)", (now, now))
    conn.execute("UPDATE execution_intents SET attempt_count = 1 WHERE execution_id = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE execution_intents SET attempt_count = 2 WHERE execution_id = 'x'")               # never a second attempt
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE execution_intents SET attempt_count = 0 WHERE execution_id = 'x'")               # never decreases
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE execution_intents SET qty = 4 WHERE execution_id = 'x'")                         # identity immutable
    conn.execute("UPDATE execution_intents SET broker_order_id = 'o1' WHERE execution_id = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE execution_intents SET broker_order_id = 'o2' WHERE execution_id = 'x'")          # a broker id never changes
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM execution_intents")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, state, created_at, updated_at) VALUES ('y', 'ENTRY', 'SNDK', 'buy', 3, 'stockagent-entry-x', 'PENDING', ?, ?)", (now, now))
    conn.execute("INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, updated_at) VALUES ('p', 'x', 'SNDK', 3, 3, '100.00', ?, 'OPEN', ?)", (now, now))
    conn.execute("UPDATE paper_positions SET state = 'CLOSED', quantity = 0 WHERE position_id = 'p'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE paper_positions SET state = 'OPEN' WHERE position_id = 'p'")                     # CLOSED is final
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE paper_positions SET symbol = 'AMD' WHERE position_id = 'p'")
    conn.execute("INSERT INTO execution_runs (run_id, started_at, trigger, mode, result) VALUES ('r', ?, 'TEST', 'MONITOR_ONLY', 'RUN')", (now,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE execution_runs SET result = 'ERROR'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT OR REPLACE INTO execution_runs (run_id, started_at, trigger, mode, result) VALUES ('r', ?, 'TEST', 'MONITOR_ONLY', 'X')", (now,))
    run_execution_migrations(conn)                                                                            # idempotent


# ================================================================ C crash points A–F ===========================================================

@pytest.mark.parametrize("hook,posts_before,posts_after,end", [
    ("after_intent", 0, 0, "NOT_SENT"),                     # A: intent persisted, crash before the attempt: never submitted blindly
    ("after_attempt_recorded", 0, 0, "NOT_SENT"),           # B: attempt recorded, crash before the wire: lookups, then NOT_SENT; no blind POST
    ("after_post", 1, 1, "FILLED"),                         # C: POST sent, crash before the broker id was persisted: adopted by client_order_id
    ("before_fill_persisted", 1, 1, "FILLED"),              # D: fill seen, crash before it was persisted: re-read from the broker
])
def test_C01_entry_crash_points_recover_without_a_duplicate_order(tmp_path, hook, posts_before, posts_after, end):
    st = make_stack(tmp_path)
    pv = arm(st)
    if hook == "before_fill_persisted":
        cyc(st)                                                                                # submitted; the fill arrives on the next read
    st.x.hooks[hook] = boom
    with pytest.raises(SimulatedCrash):
        cyc(st)
    assert len(st.sim.posts) == posts_before
    reopen(st)
    for _ in range(4):
        cyc(st, 31)
    it = st.x.store.intents()[0]
    assert it.state == end and len(st.sim.posts) == posts_after and it.attempt_count <= 1, it
    pos = st.x.store.open_position("SNDK")
    if end == "FILLED":
        assert pos is not None and pos.quantity == pv.final_qty and st.sim.positions["SNDK"]["qty"] == pv.final_qty
    else:
        assert pos is None and "SNDK" not in st.sim.positions
    assert len({i.client_order_id for i in st.x.store.intents()}) == len(st.x.store.intents())


@pytest.mark.parametrize("hook,end_posts", [("after_exit_intent", 2), ("after_post", 2)])
def test_C02_exit_crash_points_recover_with_exactly_one_effective_exit(tmp_path, hook, end_posts):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    st.x.hooks[hook] = boom
    with pytest.raises(SimulatedCrash):
        cyc(st)
    reopen(st)
    below_invalidation(st, pv)
    for _ in range(4):
        cyc(st, 31)
    p = st.x.store.position(pos.position_id)
    assert p.state == "CLOSED" and p.quantity == 0 and "SNDK" not in st.sim.positions
    sells = [b for b in st.sim.posts if json.loads(b)["side"] == "sell"]
    assert len(st.sim.posts) == end_posts and len(sells) == end_posts - 1
    exits = st.x.store.intents(intent_type="EXIT")
    assert sum(1 for i in exits if i.state == "FILLED") == 1 and all(i.attempt_count <= 1 for i in exits)
    if hook == "after_exit_intent":                                                             # E: the first exit intent was never attempted; the second cycle carried the exit
        assert [i.state for i in sorted(exits, key=lambda i: i.exit_cycle)] == ["NOT_SENT", "FILLED"] and len(sells) == 1


# ================================================================ O order lifecycle ============================================================

def test_O01_partial_fills_create_the_position_with_the_filled_quantity_only_and_the_status_history_is_appended(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = st.x.store.intents()[0].client_order_id
    st.sim.fill(coid, 2)
    cyc(st)
    it, pos = st.x.store.intents()[0], st.x.store.open_position("SNDK")
    assert it.state == "PARTIALLY_FILLED" and it.filled_qty == 2 and pos.quantity == 2 and pos.entry_qty == 2 and st.x.card("SNDK").state == "ENTRY_PARTIAL"
    st.sim.fill(coid)
    cyc(st)
    it, pos = st.x.store.intents()[0], st.x.store.open_position("SNDK")
    assert it.state == "FILLED" and pos.quantity == pv.final_qty and pos.entry_qty == pv.final_qty
    assert [e["status"] for e in st.x.store.order_events(it.broker_order_id)] == ["accepted", "partially_filled", "filled"]
    kinds = [e["kind"] for e in st.x.store.position_events(pos.position_id)]
    assert kinds[0] == "OPENED" and "FILL_UPDATED" in kinds
    assert len(st.sim.posts) == 1


def test_O02_partial_then_canceled_keeps_the_filled_shares_and_never_replaces_the_order(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = st.x.store.intents()[0].client_order_id
    st.sim.fill(coid, 3)
    cyc(st)
    st.sim.set_status(coid, "canceled")
    cyc(st)
    it, pos = st.x.store.intents()[0], st.x.store.open_position("SNDK")
    assert it.state == "CANCELED" and it.filled_qty == 3 and it.failure_code == "CANCELED_OR_EXPIRED" and pos.quantity == 3
    for _ in range(2):
        cyc(st)
    assert len(st.sim.posts) == 1 and st.x.card("SNDK").key == "IN_TRADE" and st.x.history() == []


def test_O03_rejected_or_canceled_without_a_fill_means_no_position_and_no_automatic_resubmission(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = st.x.store.intents()[0].client_order_id
    st.sim.set_status(coid, "expired")
    cyc(st)
    it = st.x.store.intents()[0]
    assert it.state == "CANCELED" and it.filled_qty == 0 and st.x.store.open_position("SNDK") is None
    for _ in range(3):
        cyc(st)
    assert len(st.sim.posts) == 1 and st.x.card("SNDK").key == "ATTENTION" and st.x.card("SNDK").state == "FAILED"
    st2 = make_stack(tmp_path / "b")
    arm(st2)
    st2.sim.auto_fill = "manual"
    cyc(st2)
    st2.sim.set_status(st2.x.store.intents()[0].client_order_id, "rejected")
    cyc(st2)
    assert st2.x.store.intents()[0].state == "REJECTED" and st2.x.store.intents()[0].failure_code == "BROKER_REJECTED"
    cyc(st2)
    assert len(st2.sim.posts) == 1


def test_O04_late_fill_and_unknown_status(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    for _ in range(5):
        cyc(st)
    assert st.x.store.intents()[0].state == "SUBMITTED" and st.x.store.open_position("SNDK") is None and len(st.sim.posts) == 1
    st.sim.fill(st.x.store.intents()[0].client_order_id)
    cyc(st)
    assert st.x.store.open_position("SNDK").quantity == pv.final_qty
    st2 = make_stack(tmp_path / "b")
    arm(st2)
    st2.sim.auto_fill = "manual"
    cyc(st2)
    st2.sim.set_status(st2.x.store.intents()[0].client_order_id, "replaced")
    cyc(st2)
    it = st2.x.store.intents()[0]
    assert it.state == "RECONCILIATION_REQUIRED" and it.failure_code == "BROKER_REPLACED" and "UNKNOWN_STATUS" in [f["kind"] for f in st2.x.store.findings()]
    assert st2.x.recon_healthy is False and st2.x.status().attention


# ================================================================ T deterministic exits ========================================================

def _pos(**over):
    base = dict(position_id="p" * 32, execution_id="e" * 32, symbol="SNDK", quantity=5, entry_qty=5, average_entry_price="100.00", opened_at="2026-10-21T15:00:00+00:00",
                invalidation_price="95.00", state="OPEN", updated_at="2026-10-21T15:00:00+00:00")
    return Position(**{**base, **over})


def _ctx(**over):
    base = dict(now=datetime(2026, 10, 21, 15, 30, tzinfo=UTC), price=Decimal("100"), setup=None, episode={"state": "READY", "closed_at": None, "invalidation_reason": None}, inputs=None)
    return EN.ExitContext(**{**base, **over})


def test_T01_exit_reasons_and_precedence_are_deterministic():
    assert EN.exit_decision(_pos(), _ctx()).reason is None
    assert EN.exit_decision(_pos(), _ctx(price=Decimal("95"))).reason == "HARD_INVALIDATION"
    assert EN.exit_decision(_pos(), _ctx(price=Decimal("95.01"))).reason is None
    assert EN.exit_decision(_pos(), _ctx(episode={"state": "INVALIDATED", "closed_at": None, "invalidation_reason": "EVIDENCE_CONTRADICTION"})).reason == "EVIDENCE_INVALIDATION"
    assert EN.exit_decision(_pos(), _ctx(episode={"state": "INVALIDATED", "closed_at": None, "invalidation_reason": "DEMAND_COLLAPSED"})).reason == "DEMAND_FAILURE"
    assert EN.exit_decision(_pos(), _ctx(episode={"state": "INVALIDATED", "closed_at": None, "invalidation_reason": "STRETCH_NORMALIZED"})).reason == "DISLOCATION_NORMALIZED"
    assert EN.exit_decision(_pos(), _ctx(episode={"state": "NONE", "closed_at": "2026-10-21T15:20:00+00:00", "invalidation_reason": None})).reason == "SETUP_INVALIDATED"
    assert EN.exit_decision(_pos(), _ctx(episode=None)).reason == "SETUP_INVALIDATED"
    i1 = fx("SNDK", stretch=-50.0, confidence=50, demand=30).model_copy(update={"evaluated_at": "2026-10-21T15:10:00+00:00"})
    d1 = EN.exit_decision(_pos(), _ctx(inputs=i1))
    assert d1.reason is None and d1.demand_low_streak == 1 and d1.weakening == "demand weakening"
    i2 = i1.model_copy(update={"evaluated_at": "2026-10-21T15:11:00+00:00"})
    d2 = EN.exit_decision(_pos(demand_low_streak=1, last_demand_eval_at=i1.evaluated_at), _ctx(inputs=i2))
    assert d2.reason == "DEMAND_FAILURE" and d2.demand_low_streak == 2
    d_same = EN.exit_decision(_pos(demand_low_streak=1, last_demand_eval_at=i1.evaluated_at), _ctx(inputs=i1))
    assert d_same.reason is None and d_same.demand_low_streak == 1                               # the same evaluation never counts twice
    assert EN.exit_decision(_pos(), _ctx(inputs=fx("SNDK", stretch=-10.0, confidence=50, demand=70).model_copy(update={"evaluated_at": "2026-10-21T15:10:00+00:00"}))).reason == "DISLOCATION_NORMALIZED"
    assert EN.exit_decision(_pos(), _ctx(now=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))).reason == "TIME_EXIT"
    assert EN.exit_decision(_pos(), _ctx(now=datetime(2026, 10, 27, 15, 0, tzinfo=UTC))).reason is None
    everything = _ctx(price=Decimal("90"), episode={"state": "INVALIDATED", "closed_at": None, "invalidation_reason": "EVIDENCE_CONTRADICTION"}, now=datetime(2026, 11, 4, 15, 0, tzinfo=UTC),
                      inputs=fx("SNDK", stretch=-10.0, confidence=50, demand=20).model_copy(update={"evaluated_at": "2026-10-21T15:10:00+00:00"}))
    d = EN.exit_decision(_pos(demand_low_streak=1, last_demand_eval_at="2026-10-21T15:09:00+00:00"), everything)
    assert d.reason == "HARD_INVALIDATION" and d.candidates == ["HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED"]
    assert EN.exit_decision(_pos(), everything.__class__(**{**everything.__dict__, "price": None})).reason == "EVIDENCE_INVALIDATION"
    assert X.EXIT_REASONS == ("HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED")
    assert not re.search(r"(?i)take.?profit|profit.?target|target.?price|expected.?return|predict", source_of(EXEC_FILES))


def test_T02_sessions_held_counts_weekday_sessions_after_the_entry_day():
    opened = "2026-10-21T15:00:00+00:00"                                                              # Wednesday
    assert EN.sessions_held(opened, datetime(2026, 10, 21, 19, 0, tzinfo=UTC)) == 0
    assert EN.sessions_held(opened, datetime(2026, 10, 22, 15, 0, tzinfo=UTC)) == 1
    assert EN.sessions_held(opened, datetime(2026, 10, 24, 15, 0, tzinfo=UTC)) == 2                    # Saturday: Thu, Fri
    assert EN.sessions_held(opened, datetime(2026, 10, 26, 15, 0, tzinfo=UTC)) == 3                    # Monday
    assert EN.sessions_held(opened, datetime(2026, 10, 28, 15, 0, tzinfo=UTC)) == 5
    assert EN.sessions_held("garbage", datetime(2026, 10, 28, 15, 0, tzinfo=UTC)) == 0


def test_T03_hard_invalidation_exit_through_the_real_path_closes_the_position_with_realized_pnl(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    out = cyc(st)
    assert out["summary"]["exits_decided"] == 1 and out["summary"]["exits_submitted"] == 1
    ex = st.x.store.intents(intent_type="EXIT")[0]
    assert ex.client_order_id == f"stockagent-exit-{pos.position_id}-1" and ex.execution_id == EN.exit_id_for(pos.position_id, 1) and ex.side == "sell" and ex.qty == pos.quantity
    assert json.loads(st.sim.posts[1]) == {"client_order_id": ex.client_order_id, "qty": str(pos.quantity), "side": "sell", "symbol": "SNDK", "time_in_force": "day", "type": "market"}
    p = st.x.store.position(pos.position_id)
    assert p.state == "EXIT_SUBMITTED" and p.exit_reason == "HARD_INVALIDATION" and st.x.card("SNDK").key == "EXITING"
    cyc(st)
    p = st.x.store.position(pos.position_id)
    exp = (Decimal(pv.invalidation_price) - Decimal("0.50") - Decimal(p.average_entry_price)) * pos.quantity
    assert p.state == "CLOSED" and p.quantity == 0 and p.exit_qty == pos.quantity and Decimal(p.realized_pnl) == exp.quantize(Decimal("0.01")) and p.closed_at
    assert "SNDK" not in st.sim.positions and len(st.sim.posts) == 2
    h = st.x.history()
    assert h[0]["kind"] == "TRADE" and h[0]["reason_code"] == "HARD_INVALIDATION" and h[0]["pnl"] == p.realized_pnl
    assert st.x.card("SNDK").key == "CLOSED" and st.x.card("SNDK").state == "CLOSED" and [e["kind"] for e in st.x.store.position_events(pos.position_id)][-1] == "CLOSED"
    assert ctl(st, "EXIT_TRIGGER")["fire_count"] == 1 and ctl(st, "EXIT_SUBMISSION")["fire_count"] == 1


@pytest.mark.parametrize("steps,reason", [
    ([CONTRADICTION], "EVIDENCE_INVALIDATION"),
    ([dict(stretch=-54.0, confidence=58, demand=30), dict(stretch=-54.0, confidence=58, demand=30)], "DEMAND_FAILURE"),
    ([dict(stretch=-10.0, confidence=58, demand=72)], "DISLOCATION_NORMALIZED"),
    ([dict(stretch=-54.0, confidence=58, demand=44), dict(stretch=-25.0, confidence=58, demand=44)], "SETUP_INVALIDATED"),          # READY -> WATCH -> NONE (episode closed)
])
def test_T04_setup_driven_exits_through_the_real_path(tmp_path, steps, reason):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    for over in steps:
        st.clock.advance(60)
        st.setups.evaluate_symbol("SNDK", trigger="TEST", inputs=fx("SNDK", **over))
    out = cyc(st)
    assert out["summary"]["exits_decided"] == 1 and st.x.store.position(pos.position_id).exit_reason == reason
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED" and len(st.sim.posts) == 2
    assert st.x.history()[0]["reason"] == X.EXIT_TEXT[reason]


def test_T05_time_exit_after_five_sessions(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    st.clock.now = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)                                     # Tuesday: 4 sessions
    out = cyc(st, 0)
    assert out["summary"]["exits_decided"] == 0 and st.x.store.position(pos.position_id).sessions_held == 4
    st.clock.now = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    out = cyc(st, 0)
    assert out["summary"]["exits_decided"] == 1 and st.x.store.position(pos.position_id).exit_reason == "TIME_EXIT"


def test_T06_safety_blocks_entries_only_exits_continue_and_a_stop_never_liquidates(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    go_ready(st, "NVDA")
    st.safety.emergency_stop(actor="user")
    out = cyc(st)
    assert out["summary"]["exits_decided"] == 0 and st.x.store.position(pos.position_id).state in ("OPEN", "HEALTHY") and len(st.sim.posts) == 1   # no liquidation
    assert "SAFETY_GATE" in _blocked(st, out, "NVDA")
    below_invalidation(st, pv)
    out = cyc(st)
    assert out["summary"]["exits_submitted"] == 1 and len(st.sim.posts) == 2 and json.loads(st.sim.posts[1])["side"] == "sell"
    st.safety.clear_emergency_stop(actor="user")
    st2 = make_stack(tmp_path / "pause")
    pv2, pos2 = entered(st2)
    st2.safety.pause(actor="user")
    below_invalidation(st2, pv2)
    assert cyc(st2)["summary"]["exits_submitted"] == 1
    st3 = make_stack(tmp_path / "daily")
    pv3, pos3 = entered(st3)
    st3.safety.overrides = {"equity": "94000.00"}
    st3.safety.evaluate("TEST", st3.clock.now)
    assert st3.safety.current(now=st3.clock.now).state == "HALTED"
    below_invalidation(st3, pv3)
    assert cyc(st3)["summary"]["exits_submitted"] == 1


def test_T07_exit_outside_the_session_is_deferred_not_dropped(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    st.sim.is_open = False
    out = cyc(st)
    p = st.x.store.position(pos.position_id)
    assert out["summary"]["exits_deferred"] == 1 and p.state == "EXIT_REQUIRED" and p.exit_reason == "HARD_INVALIDATION" and len(st.sim.posts) == 1
    assert ctl(st, "EXIT_SUBMISSION")["last_result"] == "WARN"
    cyc(st)
    assert len(st.sim.posts) == 1
    st.sim.is_open = True
    out = cyc(st)
    assert out["summary"]["exits_submitted"] == 1 and len(st.sim.posts) == 2


def test_T08_exit_rejects_open_bounded_exit_cycles_then_attention(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    st.sim.post_modes = ["reject", "reject", "reject"]
    for n in (1, 2, 3):
        cyc(st)
        ex = st.x.store.intents(intent_type="EXIT")
        assert len(ex) == n and ex[0].client_order_id.endswith(f"-{n}") and ex[0].state == "REJECTED"
        assert st.x.store.position(pos.position_id).state == "EXIT_REQUIRED" and st.x.store.position(pos.position_id).exit_cycle == n
    cyc(st)
    p = st.x.store.position(pos.position_id)
    assert p.state == "ATTENTION" and p.attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and len(st.sim.posts) == 4
    cyc(st)
    assert len(st.sim.posts) == 4 and st.x.card("SNDK").key == "ATTENTION" and st.x.status().attention


def test_T09_partial_exit_then_expiry_finishes_in_the_next_exit_cycle(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = st.x.store.intents(intent_type="EXIT")[0].client_order_id
    st.sim.fill(coid, 2, 95.0)
    cyc(st)
    p = st.x.store.position(pos.position_id)
    assert p.quantity == pos.quantity - 2 and p.exit_qty == 2 and p.state == "EXIT_SUBMITTED" and st.x.card("SNDK").state == "EXIT_PARTIAL"
    st.sim.set_status(coid, "expired")
    cyc(st)                                                                                    # the expiry hands the position back to EXIT_REQUIRED and the same cycle opens exit cycle 2
    p = st.x.store.position(pos.position_id)
    assert p.state == "EXIT_SUBMITTED" and p.quantity == pos.quantity - 2 and p.exit_cycle == 2
    assert [e["kind"] for e in st.x.store.position_events(pos.position_id) if e["kind"] in ("EXIT_NOT_DONE", "EXIT_INTENT")] == ["EXIT_INTENT", "EXIT_NOT_DONE", "EXIT_INTENT"]
    ex = sorted(st.x.store.intents(intent_type="EXIT"), key=lambda i: i.exit_cycle)
    assert len(ex) == 2 and ex[1].qty == pos.quantity - 2 and ex[1].client_order_id.endswith("-2")
    st.sim.fill(ex[1].client_order_id, None, 97.0)
    cyc(st)
    p = st.x.store.position(pos.position_id)
    exp = ((Decimal("95") * 2 + Decimal("97") * (pos.quantity - 2)) / pos.quantity - Decimal(p.average_entry_price)) * pos.quantity
    assert p.state == "CLOSED" and p.exit_qty == pos.quantity and Decimal(p.realized_pnl) == exp.quantize(Decimal("0.01"))
    assert "SNDK" not in st.sim.positions


# ================================================================ R reconciliation =============================================================

def test_R01_order_missing_at_the_broker_blocks_entries_and_needs_attention(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    it = st.x.store.intents()[0]
    del st.sim.orders[it.broker_order_id]
    go_ready(st, "NVDA")
    out = cyc(st)
    it = st.x.store.intents(symbol="SNDK")[0]
    assert it.state == "RECONCILIATION_REQUIRED" and it.failure_code == "ORDER_MISSING_AT_BROKER"
    assert "ORDER_MISSING_AT_BROKER" in [f["kind"] for f in st.x.store.findings()] and "BROKER_RECONCILIATION" in _blocked(st, out, "NVDA")
    assert st.x.card("SNDK").key == "ATTENTION" and st.x.status().headline == "Needs attention" and st.x.status().icon == "🔴"


def test_R02_quantity_mismatch_and_broker_flat_mark_the_position_attention_and_never_fix_silently(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    st.sim.set_position("SNDK", pos.quantity - 1, 100.0)
    below_invalidation(st, pv)
    out = cyc(st)
    p = st.x.store.position(pos.position_id)
    assert p.state == "ATTENTION" and p.attention_reason == "QTY_MISMATCH" and out["summary"]["exits_submitted"] == 0 and len(st.sim.posts) == 1
    assert "QTY_MISMATCH" in [f["kind"] for f in st.x.store.findings()] and st.x.recon_healthy is False
    go_ready(st, "NVDA")
    assert "BROKER_RECONCILIATION" in _blocked(st, cyc(st), "NVDA")
    st.sim.set_position("SNDK", pos.quantity, 100.0)                                             # the operator fixed the broker side: the mismatch disappears
    cyc(st)
    p = st.x.store.position(pos.position_id)
    assert p.state in ("EXIT_REQUIRED", "EXIT_SUBMITTED", "CLOSED") or p.state in ("OPEN", "HEALTHY")
    assert [e["kind"] for e in st.x.store.position_events(pos.position_id) if e["kind"] in ("ATTENTION", "ATTENTION_CLEARED")] == ["ATTENTION", "ATTENTION_CLEARED"]
    st2 = make_stack(tmp_path / "flat")
    pv2, pos2 = entered(st2)
    st2.sim.set_position("SNDK", 0)
    cyc(st2)
    p2 = st2.x.store.position(pos2.position_id)
    assert p2.state == "ATTENTION" and p2.attention_reason == "BROKER_FLAT_INTERNAL_OPEN" and p2.quantity == pos2.quantity          # never closed silently


def test_R03_external_positions_are_informational_but_count_toward_the_cap_and_block_their_symbol(tmp_path):
    st = make_stack(tmp_path)
    st.sim.set_position("AMD", 4, 50.0)
    arm(st)
    out = cyc(st)
    assert out["summary"]["entries"]["submitted"] == ["SNDK"]                                  # not blocked globally
    f = [x for x in st.x.store.findings() if x["kind"] == "EXTERNAL_POSITION"]
    assert len(f) == 1 and f[0]["blocking"] is False and f[0]["symbol"] == "AMD" and st.x.recon_healthy
    cyc(st)
    assert st.x.status().external_positions == 1
    st.x.max_open_positions = 2                                                                  # AMD + SNDK = 2: the cap blocks NVDA
    go_ready(st, "NVDA")
    assert "MAX_OPEN_POSITIONS" in _blocked(st, cyc(st), "NVDA")
    assert len([x for x in st.x.store.findings() if x["kind"] == "EXTERNAL_POSITION"]) == 1       # findings are not repeated every cycle


def test_R04_broker_unavailable_blocks_entries_and_recovers(tmp_path):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.read_fail_paths = ["/v2/positions"]
    out = cyc(st)
    assert "BROKER_UNAVAILABLE" in [f["kind"] for f in st.x.store.findings()] and st.sim.posts == [] and "BROKER_RECONCILIATION" in _blocked(st, out)
    out = cyc(st)
    assert out["summary"]["entries"]["submitted"] == ["SNDK"] and st.x.recon_healthy


def test_R05_internal_flat_broker_position_in_a_traded_symbol_is_blocking(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    cyc(st)
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED"
    st.sim.set_position("SNDK", 3, 100.0)                                                       # someone bought SNDK outside Stage 6.0 after our trade closed
    cyc(st)
    assert "INTERNAL_FLAT_BROKER_POSITION" in [f["kind"] for f in st.x.store.findings()] and st.x.recon_healthy is False and st.x.status().attention


# ================================================================ S restart at every state =====================================================

def test_S01_restart_with_a_submitted_entry_keeps_monitoring_and_fills_later(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    reopen(st)
    cyc(st)
    assert st.x.store.intents()[0].state == "SUBMITTED" and len(st.sim.posts) == 1
    st.sim.fill(st.x.store.intents()[0].client_order_id)
    cyc(st)
    assert st.x.store.open_position("SNDK").quantity == pv.final_qty and len(st.sim.posts) == 1


def test_S02_restart_with_an_open_position_resumes_monitoring_and_exits(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    reopen(st)
    cyc(st)
    assert st.x.store.position(pos.position_id).state in ("OPEN", "HEALTHY") and len(st.sim.posts) == 1
    below_invalidation(st, pv)
    cyc(st)
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED" and len(st.sim.posts) == 2


def test_S03_restart_with_an_exit_required_or_submitted_or_reconciliation_required(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    st.sim.is_open = False
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "EXIT_REQUIRED"
    reopen(st)
    st.sim.is_open = True
    below_invalidation(st, pv)
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "EXIT_SUBMITTED" and len(st.sim.posts) == 2
    reopen(st)
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED" and len(st.sim.posts) == 2
    st2 = make_stack(tmp_path / "rr")
    arm(st2)
    st2.sim.post_modes = ["timeout_after_record"]
    cyc(st2)
    assert st2.x.store.intents()[0].state == "RECONCILIATION_REQUIRED"
    reopen(st2)
    cyc(st2, 31)
    assert st2.x.store.intents()[0].state in ("SUBMITTED", "FILLED") and len(st2.sim.posts) == 1
    st3 = make_stack(tmp_path / "closed")
    pv3, pos3 = entered(st3)
    below_invalidation(st3, pv3)
    cyc(st3)
    cyc(st3)
    st3.x.disable_auto(actor="test", now=st3.clock.now)
    reopen(st3)
    st3.sim.requests.clear()
    assert cyc(st3)["result"] == "IDLE" and st3.sim.requests == []                               # nothing open, MONITOR_ONLY: idle, no broker call


def test_S04_restart_between_a_partial_fill_and_the_rest_resumes_from_broker_evidence(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = st.x.store.intents()[0].client_order_id
    st.sim.fill(coid, 2)
    cyc(st)
    assert st.x.store.open_position("SNDK").quantity == 2 and st.x.card("SNDK").state == "ENTRY_PARTIAL"
    reopen(st)                                                                                  # the process dies after the partial fill was persisted
    st.sim.fill(coid)                                                                           # the rest fills while the process is down
    cyc(st)
    it, pos = st.x.store.intents()[0], st.x.store.open_position("SNDK")
    assert it.state == "FILLED" and it.filled_qty == pv.final_qty and pos.quantity == pv.final_qty and pos.entry_qty == pv.final_qty
    assert [e["status"] for e in st.x.store.order_events(it.broker_order_id)] == ["accepted", "partially_filled", "filled"]
    assert len(st.sim.posts) == 1 and st.sim.positions["SNDK"]["qty"] == pv.final_qty and st.x.recon_healthy
    cyc(st)
    assert len(st.sim.posts) == 1 and st.x.store.open_position("SNDK").quantity == pv.final_qty


def test_E09_an_unknown_safety_gate_blocks_entries_inside_the_cycle(tmp_path):
    from safety.models import SafetyView
    st = make_stack(tmp_path)
    arm(st)
    unknown = SafetyView(state="UNKNOWN", state_label="Trading stopped", icon="🔴", error="forced evaluator failure")
    mp = pytest.MonkeyPatch()
    mp.setattr(st.safety, "evaluate", lambda *a, **k: unknown)
    mp.setattr(st.safety, "current", lambda *a, **k: unknown)
    try:
        out = cyc(st)
    finally:
        mp.undo()
    assert "SAFETY_GATE" in _blocked(st, out) and st.sim.posts == [] and st.x.store.intents() == []
    assert ctl(st, "SAFETY_GATE")["last_result"] == "BLOCK" and out["summary"]["safety"] == "UNKNOWN"
    out = cyc(st)                                                                               # the gate is readable again: the entry goes out once
    assert out["summary"]['entries']["submitted"] == ["SNDK"] and len(st.sim.posts) == 1


# ================================================================ B broker failure script =======================================================

@pytest.mark.parametrize("mode,state,posts", [("connect_timeout", "NOT_SENT", 0), ("dns", "NOT_SENT", 0), ("timeout_before", "RECONCILIATION_REQUIRED", 0),
                                              ("timeout_after_record", "RECONCILIATION_REQUIRED", 1), ("reset_after_record", "RECONCILIATION_REQUIRED", 1),
                                              ("http:429", "RECONCILIATION_REQUIRED", 1), ("http:500", "RECONCILIATION_REQUIRED", 1),
                                              ('http:422:{"code":40010001,"message":"qty must be integer"}', "REJECTED", 1), ("reject", "REJECTED", 1), ("accept:filled", "FILLED", 1)])
def test_B01_broker_failure_script_never_produces_a_second_post(tmp_path, mode, state, posts):
    st = make_stack(tmp_path)
    arm(st)
    st.sim.post_modes = [mode]
    cyc(st)
    it = st.x.store.intents()[0]
    assert it.state == state and len(st.sim.posts) == posts and it.attempt_count == 1, it
    for _ in range(3):
        cyc(st, 31)
    assert st.x.broker.posts == 1 and len(st.sim.posts) == posts
    if state == "FILLED":
        assert st.x.store.open_position("SNDK") is not None


# ================================================================ N boundaries ==================================================================

def test_N01_no_robinhood_no_model_no_46b_writer_in_a_full_paper_trade(tmp_path, monkeypatch):
    calls = []
    from paper import alpaca_order_writer as W
    from paper import alpaca_orders as AO
    import api.routes.portfolio as PRT
    monkeypatch.setattr(W, "submit", lambda *a, **k: calls.append("46b submit") or (_ for _ in ()).throw(AssertionError("4.6B writer touched")))
    for name in ("preview", "confirm", "retry"):
        if hasattr(AO, name):
            monkeypatch.setattr(AO, name, lambda *a, **k: calls.append("46b " + name) or (_ for _ in ()).throw(AssertionError("4.6B touched")))
    monkeypatch.setattr(PRT, "provider_factory", lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(RuntimeError("robinhood touched")))
    import anthropic
    monkeypatch.setattr(anthropic.resources.messages.Messages, "create", lambda *a, **k: calls.append("llm") or (_ for _ in ()).throw(AssertionError("model called")))
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    below_invalidation(st, pv)
    cyc(st)
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED" and calls == [] and len(st.sim.posts) == 2


def test_N02_execution_files_import_no_broker_sdk_robinhood_model_or_46b_writer():
    allowed_requests = {"execution/broker.py", "execution/simulated.py"}
    forbidden = tuple(p for p in FORBIDDEN_IMPORTS if p not in ("forward", "paper.alpaca_order", "requests", "urllib")) + (
        "paper.alpaca_order_writer", "paper.alpaca_orders", "paper.alpaca_order_reads", "paper.alpaca_readonly", "paper.alpaca_view", "paper.execution", "robin_stocks", "robinhood", "alpaca",
        "anthropic", "agents", "ai_explain", "research_flow", "subprocess", "mcp", "evidence", "ppe", "demand")
    for rel in EXEC_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == p or name.startswith(p + ".") for p in forbidden), (rel, name)
            if name.split(".")[0] in ("requests", "urllib3", "urllib"):
                assert rel in allowed_requests, (rel, name)
        assert not re.search(r"TradingClient|TradingStream|messages\.create|get_provider|alpaca_order_writer|W\.submit|place_order|cancel_order|\beval\(|\bexec\(|__import__|os\.system|subprocess", text), rel
    src = strip_code(source_of(EXEC_FILES))
    assert "recommend" not in src.lower() and not re.search(r"(?i)robinhood|robin_stocks", src)


def test_N03_trader_view_stream_registers_no_execution_listener_and_the_socket_carries_no_order(tmp_path):
    st = make_stack(tmp_path)
    assert [fn.__name__ for fn in st.setups.listeners] == ["_setup_event", "on_setup_event"] and [fn.__name__ for fn in st.proposals.listeners] == ["_proposal_event"] and st.safety.listeners == []
    assert "execution" not in TerminalState.model_fields and "execution" not in MESSAGE_TYPES and "paper_bot" not in MESSAGE_TYPES
    assert not re.search(r"/v2/orders|\.submit\(|preview\(|post_order|requests\.", STREAM_SRC)
    assert "self.execution" in STREAM_SRC and "listeners.append(self.execution" not in STREAM_SRC
    acts = {a.id: a for a in st.s.state("SNDK", with_candles=False).actions}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False


def test_N04_exact_routes_and_no_banned_path_words(stack):
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/execution") and not p.startswith("/api/execution-safety")}
    assert mine == {"/api/execution": {"get"}, "/api/execution/status": {"get"}, "/api/execution/history": {"get"}, "/api/execution/controls": {"get"}, "/api/execution/intents": {"get"},
                    "/api/execution/positions": {"get"}, "/api/execution/findings": {"get"}, "/api/execution/symbol/{symbol}": {"get"}, "/api/execution/auto/enable": {"post"},
                    "/api/execution/auto/disable": {"post"}, "/api/execution/activity": {"get"}}   # Stage 6.8: + Bot Activity (GET)
    for p in mine:
        assert not re.search(r"paper|alpaca|broker|execute|trade|place|cancel|submit|order", p), p
    assert stack.api.post("/api/execution/auto/enable", json={"reason": "x", "qty": 5}).status_code == 422


def test_N05_scheduler_lease_gating_idle_and_exits_before_entries(tmp_path, monkeypatch):
    assert LEASE_NAME == "paper_executor"
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    assert start_if_enabled() is False
    monkeypatch.setenv("STOCK_AGENT_ENV", "browser_test")
    assert start_if_enabled() is False
    monkeypatch.delenv("STOCK_AGENT_ENV", raising=False)
    monkeypatch.setattr(config, "EXECUTION_SCHEDULER_ENABLED", False)
    assert start_if_enabled() is False
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    go_ready(st, "NVDA")
    below_invalidation(st, pv)
    st.clock.advance(30)
    sched = ExecutionScheduler(lambda: st.x, clock=lambda: st.clock.now, interval_s=30, idle_interval_s=300, startup_delay_s=0)
    out = sched.step(st.clock.now)
    assert out["result"] == "RUN" and sched.runs == 1 and sched.next_at == st.clock.now + timedelta(seconds=30)
    sides = [json.loads(b)["side"] for b in st.sim.posts]
    assert sides == ["buy", "sell", "buy"] and [json.loads(b)["symbol"] for b in st.sim.posts][1:] == ["SNDK", "NVDA"]        # the exit went out before the new entry
    assert sched.step(st.clock.now) is None                                                         # not due
    assert sched.wait_seconds(st.clock.now) == 30.0

    class HeldLease:
        def acquire(self, owner, now, ttl_min, name):
            assert name == "paper_executor"
            return False

        def release(self, owner, name):
            pass
    sched2 = ExecutionScheduler(lambda: st.x, clock=lambda: st.clock.now, interval_s=30, lease_store_fn=HeldLease, startup_delay_s=0)
    posts = len(st.sim.posts)
    assert sched2.step(st.clock.now)["result"] == "LEASE_HELD" and len(st.sim.posts) == posts
    assert sched2.status()["lease"] == "paper_executor" and sched2.running() is False
    assert sched.status()["interval_s"] == 30 and sched._interval(datetime(2026, 10, 24, 15, 0, tzinfo=UTC)) == 300


def test_N06_no_real_money_only_the_paper_label_market_day_orders_and_the_default_config():
    assert config.EXECUTION_BROKER in ("alpaca_paper", "simulated") and config.EXECUTION_SCHEDULER_ENABLED in (True, False)
    assert X.ORDER_TYPE == "market" and X.TIME_IN_FORCE == "day" and X.SIDE == "LONG" and X.COID_PREFIX == "stockagent-"
    assert "no real money" in X.DISCLAIMER.lower() and "PAPER" in X.DISCLAIMER
    b = BK.AlpacaPaperBroker()
    assert b.label == "ALPACA_PAPER" and b.configured() is False                                   # the conftest removed the paper credentials: nothing can be sent
    assert b.environment()["paper"] is False
    from execution.service import default_broker
    assert isinstance(default_broker(), BK.AlpacaPaperBroker)


# ================================================================ A API ======================================================================

def test_A01_read_routes_shapes(stack):
    st = stack
    go_ready(st)
    r = st.api.get("/api/execution")
    assert r.status_code == 200
    j = r.json()
    assert j["mode"] == "MONITOR_ONLY" and j["modes"] == ["MONITOR_ONLY", "PAPER_AUTO"] and set(j["exit_reasons"]) == set(X.EXIT_REASONS) and j["known_controls"] == list(X.CONTROLS)
    assert "disclaimer" in j and j["limits"]["max_open_positions"] == 10
    s = st.api.get("/api/execution/status").json()
    assert s["headline"] == "Monitor only" and s["icon"] == "⚪" and any(c["symbol"] == "SNDK" and c["key"] == "READY" for c in s["cards"])
    assert st.api.get("/api/execution/history").json()["history"] == []
    assert st.api.get("/api/execution/controls").json()["known_controls"] == list(X.CONTROLS)
    assert st.api.get("/api/execution/intents").json()["intents"] == [] and st.api.get("/api/execution/positions").json()["positions"] == []
    assert st.api.get("/api/execution/findings").json()["healthy"] is True
    c = st.api.get("/api/execution/symbol/SNDK").json()
    assert (c["key"], c["label"], c["icon"]) == ("READY", "Ready", "🔵")
    assert st.api.get("/api/execution/symbol/bad!").status_code == 400
    assert st.api.get("/api/execution/symbol/MSFT").json()["key"] == "NONE"


def test_A02_mode_actions_through_the_api(stack):
    st = stack
    go_ready(st)
    r = st.api.post("/api/execution/auto/enable", json={"reason": "first run"})
    assert r.status_code == 200 and r.json()["mode"] == "PAPER_AUTO" and r.json()["action"] == "enable_auto"
    assert st.x.mode()["changed_by"] == "user" and st.x.mode()["reason"] == "first run"
    r = st.api.post("/api/execution/auto/disable")
    assert r.status_code == 200 and r.json()["mode"] == "MONITOR_ONLY"
    st.safety.emergency_stop(actor="user")
    mp = pytest.MonkeyPatch()
    from safety.models import SafetyView
    mp.setattr(st.safety, "current", lambda now=None, **k: SafetyView(state="UNKNOWN", state_label="Trading stopped", icon="🔴", error="x"))
    r = st.api.post("/api/execution/auto/enable")
    mp.undo()
    assert r.status_code == 409 and r.json()["error"] == "SAFETY_GATE_UNKNOWN" and st.x.mode()["mode"] == "MONITOR_ONLY"
    st.safety.clear_emergency_stop(actor="user")


# ================================================================ U frontend ===================================================================

def test_U01_node_tests_for_the_paper_bot_panel():
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_bot.test.cjs")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout and "# pass 10" in r.stdout, r.stdout


def test_U02_page_wiring_and_beginner_wording():
    assert '<script src="paper_bot.js"></script>' in HTML and HTML.index("risk_controls.js") < HTML.index("paper_bot.js")
    src = strip_code(BOT_JS)
    assert not re.search(r"(?i)recommend|probability|expected.return|target price|win rate", src)
    assert not TRADE_WORDS.search(src)
    for t in ("Monitor only", "Paper Auto", "Needs attention", "Enable Paper Auto", "Stop New Trades", "View details", "/api/execution/status", "/api/execution/auto/enable", "/api/execution/auto/disable"):
        assert t in BOT_JS, t
    assert "/v2/orders" not in BOT_JS and "alpaca" not in BOT_JS.lower().replace("alpaca paper", "").replace("alpaca paper account", "")
    css = (ROOT / "frontend" / "trader_view.css").read_text(encoding="utf-8")
    assert ".tv-bot-state" in css and ".tv-panel-bot" in css


# ================================================================ Z base pins ==================================================================

def test_Z01_prior_stage_files_identical_to_base_and_config_append_only():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    pinned = [p.relative_to(ROOT).as_posix() for d in ("setups", "proposals", "safety", "ppe", "demand") for p in (ROOT / d).glob("*.py")] + \
             ["trader_view/models.py", "trader_view/hub.py", "trader_view/adapters.py", "trader_view/positions.py", "trader_view/research.py", "trader_view/market_real.py", "trader_view/simulated.py",
              "trader_view/ppe_provider.py", "trader_view/demand_provider.py", "trader_view/__init__.py", "api/routes/trader_view.py", "api/routes/setups.py", "api/routes/proposals.py",
              "api/routes/execution_safety.py", "paper/alpaca_order_writer.py", "paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_store.py", "paper/alpaca_order_reads.py",
              "paper/alpaca_readonly.py", "paper/alpaca_view.py", "paper/execution.py", "paper/portfolio.py", "portfolio/provider.py", "database/migrations.py", "database/setup_state_migrations.py",
              "database/proposal_migrations.py", "database/execution_safety_migrations.py", "forward/automation.py", "frontend/trade_review.js", "frontend/setup_alerts.js", "frontend/risk_controls.js",
              "frontend/trader_view.js", "frontend/trader_render.js", "tests/conftest.py", "tests/alpaca_order_fakes.py", "tests/test_setups_58.py", "tests/test_proposals_59.py",
              "tests/test_execution_safety_591.py", "tests/test_trader_view_55.py", "tests/frontend/trader_core.test.cjs", "tests/frontend/trader_render.test.cjs", "tests/frontend/trader_setup.test.cjs",
              "tests/frontend/trader_proposal.test.cjs", "tests/frontend/trader_safety.test.cjs", "DESIGN_58_SETUP_STATE_ALERTS.md", "DESIGN_59_TRADE_PROPOSAL_REVIEW.md", "DESIGN_591_EXECUTION_SAFETY.md"]
    # >>> Stage 6.0.2: the safety files carry marked, additive Stage 6.0.2 blocks (DESIGN_602 section 6); everything outside them stays byte-identical
    import re as _re602
    for rel in [p for p in pinned if p.startswith("safety/")]:
        now602 = _re602.sub(r"(?ms)^[ \t]*# >>> Stage 6\.0\.2.*?^[ \t]*# <<< Stage 6\.0\.2[^\n]*\n", "", (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n"))
        assert now602.encode("utf-8") == blob(rel), f"{rel} differs from {BASE} outside the Stage 6.0.2 blocks"
    pinned = [p for p in pinned if not (p.startswith("safety/"))]
    # <<< Stage 6.0.2
    # >>> Stage 6.0.3: the Stage 5.5 pin test carries one marked, additive Stage 6.0.3 block (the workflow is verified structurally by
    # tests/test_ci_groups_603.py instead of byte-pinned); everything outside it stays byte-identical
    import re as _re603
    now603 = _re603.sub(r"(?ms)^[ \t]*# >>> Stage 6\.0\.3.*?^[ \t]*# <<< Stage 6\.0\.3[^\n]*\n", "", ledger_view("tests/test_trader_view_55.py", (ROOT / "tests/test_trader_view_55.py").read_text(encoding="utf-8").replace("\r\n", "\n")))
    assert now603.encode("utf-8") == blob("tests/test_trader_view_55.py"), f"tests/test_trader_view_55.py differs from {BASE} outside the Stage 6.0.3 block"
    pinned = [p for p in pinned if p != "tests/test_trader_view_55.py"]
    # <<< Stage 6.0.3
    for rel in pinned:
        assert ledger_bytes(rel, (ROOT / rel).read_bytes().replace(b"\r\n", b"\n")) == blob(rel), f"{rel} differs from {BASE}"
    base_cfg = blob("config.py").decode("utf-8")
    now_cfg = (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert now_cfg.startswith(base_cfg.rstrip("\n")) and "EXECUTION_SCHEDULER_ENABLED" in now_cfg and config.MAX_OPEN_POSITIONS == 10
    base_html = blob("frontend/index.html").decode("utf-8").splitlines()
    now_html = HTML.splitlines()
    # >>> Stage 6.1: index.html carries two marked, additive Stage 6.1 blocks (the Forward Paper Validation stylesheet and script); they are stripped before
    # the Stage 6.0 line rule below, so every other added line is still exactly the Stage 6.0 script
    import re as _re61
    now_html = _re61.sub(r"(?ms)^<!-- >>> Stage 6\.1\b.*?^<!-- <<< Stage 6\.1 -->\n", "", "\n".join(now_html) + "\n").splitlines()
    # <<< Stage 6.1
    # >>> Stage 6.1.1: index.html also carries two marked, additive Product UI 6.1.1 blocks (the product shell stylesheet and script, pinned against the
    # Stage 6.1.1 base by tests/test_product_ui_611.py); they are stripped as well, so the line rule below still sees exactly the Stage 6.0 script
    now_html = _re61.sub(r"(?ms)^<!-- >>> Product UI 6\.1\.1\b.*?^<!-- <<< Product UI 6\.1\.1 -->\n", "", "\n".join(now_html) + "\n").splitlines()
    # <<< Stage 6.1.1
    assert [l for l in base_html if l not in now_html] == [] and [l for l in now_html if l not in base_html] == ['<script src="paper_bot.js"></script>']
    base_css = blob("frontend/trader_view.css").decode("utf-8")
    assert (ROOT / "frontend" / "trader_view.css").read_text(encoding="utf-8").replace("\r\n", "\n").startswith(base_css.rstrip("\n"))
