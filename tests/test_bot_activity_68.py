"""
Stage 6.8 — ACCOUNT POSITION VISIBILITY + BOT ACTIVITY. Focused tests on the REAL stack (setups -> proposals -> safety gate -> executor) with the
deterministic simulated PAPER broker of tests/test_execution_60.py (no socket is ever opened; the real network adapter is a tripwire):
  A account positions: the account refresh's snapshot classified bot-managed / external by the executor's own reconciliation rule, NOT_LOADED vs
    "no position", the external count without Paper Auto, Trader View's source and its accurate navigation
  P Paper Auto ordering: reconciliation completes before any entry is evaluated or submitted; external positions count toward the cap and block
    their symbol; an unreadable broker blocks every entry; per-ticker reasons are persisted; Monitor only stays idle
  B Bot Activity: the worker heartbeat and its states, cycle progress, broker-reported order lifecycle (never an inferred fill), position
    decisions, collapsed quiet cycles, persistence across a restart, zero broker calls, no credential, no model
  F freeze: every change since main is a new file or a registered ledger entry (tests/freeze_ledger.py), and the old freeze tests see main
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from execution import account_positions as AP  # noqa: E402
from execution import activity as ACT  # noqa: E402
from execution.engine import entry_coid, execution_id_for  # noqa: E402
from execution.scheduler import ExecutionScheduler  # noqa: E402
from execution import service as S  # noqa: E402
from trader_view import positions as TP  # noqa: E402
from test_execution_60 import (_blocked, arm, below_invalidation, cyc, entered, execution_config, go_ready, make_stack, no_execution_wire, reopen,  # noqa: E402,F401
                               stack)

UTC = timezone.utc
T = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)


class FakeRefresher:
    """The account refresh's read surface (enabled / freshness / store.snapshot) over a fixed snapshot; it has no broker."""
    def __init__(self, positions=None, *, enabled=True, status="FRESH", refreshed_at="2026-10-26T18:54:25+00:00"):
        self._enabled, self.status = enabled, status
        snap = None if positions is None else {"refresh_id": "r1", "refreshed_at": refreshed_at, "source": "ALPACA_PAPER", "host": "paper-api.alpaca.markets",
                                               "account": {}, "positions": positions, "open_orders": [], "updated_at": refreshed_at}
        self.store = SimpleNamespace(snapshot=lambda: snap)

    def enabled(self):
        return self._enabled

    def freshness(self):
        label = {"FRESH": "Account data: Fresh (31s ago)", "STALE": "Account data: Stale", "UNAVAILABLE": "Account data unavailable"}.get(self.status, self.status)
        return {"status": self.status, "fresh": self.status == "FRESH", "label": label, "short_label": self.status.title(), "reason": "", "age_seconds": 31.0,
                "refreshed_at": None if self.store.snapshot() is None else self.store.snapshot()["refreshed_at"], "last_success_at": None, "consecutive_failures": 0,
                "source": "ALPACA_PAPER"}


F_ROW = {"symbol": "F", "qty": "1", "side": "long", "avg_entry_price": "12.1", "cost_basis": "12.1", "market_value": "11.58", "current_price": "11.58",
         "unrealized_pl": "-0.52", "unrealized_plpc": "-0.0429752066"}


def fresh_store(tmp_path):
    from execution.store import ExecutionStore
    s = ExecutionStore(tmp_path / "x.db")
    s.ensure()
    return s


# ================================================================ A account positions ============================================================

def test_A01_classification_matches_the_executors_reconciliation_rule(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st, "SNDK")                                                          # a bot position (broker-confirmed fill)
    st.sim.set_position("F", 1, 12.1)                                                      # opened outside the bot
    rows = {r["symbol"]: r for r in AP.classify(st.x.broker.positions(), st.x.store)}
    cyc(st)                                                                                # the executor's own reconciliation of the same account
    kinds = {f["symbol"]: f["kind"] for f in st.x.store.findings() if f["kind"] in ("EXTERNAL_POSITION", "INTERNAL_FLAT_BROKER_POSITION")}
    assert rows["SNDK"]["management"] == "BOT" and rows["SNDK"]["managed"] and "SNDK" not in kinds
    assert rows["F"]["management"] == "EXTERNAL" and not rows["F"]["managed"] and kinds["F"] == "EXTERNAL_POSITION"
    below_invalidation(st, pv)                                                             # the bot exits SNDK and closes its position
    cyc(st)
    cyc(st)
    assert st.x.store.position(pos.position_id).state == "CLOSED"
    st.sim.set_position("SNDK", 3, 100.0)                                                  # shares appear again in a symbol the bot closed
    rows = {r["symbol"]: r for r in AP.classify(st.x.broker.positions(), st.x.store)}
    cyc(st)
    kinds = {f["symbol"]: f["kind"] for f in st.x.store.findings() if f["kind"] in ("EXTERNAL_POSITION", "INTERNAL_FLAT_BROKER_POSITION")}
    assert rows["SNDK"]["management"] == "NEEDS_REVIEW" and kinds["SNDK"] == "INTERNAL_FLAT_BROKER_POSITION"
    assert set(AP.MANAGEMENT) == {"BOT", "BOT_ENTRY", "NEEDS_REVIEW", "EXTERNAL"}


def test_A02_an_open_entry_intent_marks_its_symbol_as_the_bots(tmp_path):
    st = make_stack(tmp_path)
    arm(st, "SNDK")
    st.sim.auto_fill = "manual"
    cyc(st)                                                                                # the entry is at the broker, unfilled
    rows = {r["symbol"]: r for r in AP.classify([{"symbol": "SNDK", "qty": "1"}], st.x.store)}
    assert rows["SNDK"]["management"] == "BOT_ENTRY" and rows["SNDK"]["managed"]


def test_A03_view_states_never_read_not_loaded_as_no_position(tmp_path):
    store = fresh_store(tmp_path)
    assert AP.view(None, store, loop_running=True)["state"] == "DISABLED"
    assert AP.view(FakeRefresher([F_ROW], enabled=False), store, loop_running=True)["state"] == "DISABLED"
    nl = AP.view(FakeRefresher(None, status="UNAVAILABLE"), store, loop_running=True)
    assert nl["state"] == "NOT_LOADED" and nl["positions"] is None and nl["counts"] is None and "Not loaded yet" in nl["message"] and "No position" not in nl["message"]
    off = AP.view(FakeRefresher(None, status="UNAVAILABLE"), store, loop_running=False)
    assert "ACCOUNT_REFRESH_ENABLED=false" in off["message"]
    empty = AP.view(FakeRefresher([]), store, loop_running=True)
    assert empty["state"] == "LOADED" and empty["positions"] == [] and empty["counts"]["total"] == 0 and empty["message"] == "No positions in the Alpaca PAPER account."
    v = AP.view(FakeRefresher([F_ROW]), store, loop_running=True)
    assert v["state"] == "LOADED" and v["source"] == "ALPACA_PAPER" and v["host"] == "paper-api.alpaca.markets" and v["freshness"]["status"] == "FRESH"
    (f,) = v["positions"]
    assert f["symbol"] == "F" and f["management"] == "EXTERNAL" and f["management_label"] == "External · not managed by the bot" and f["qty"] == "1"
    assert v["counts"] == {"total": 1, "bot": 0, "bot_entry": 0, "external": 1, "needs_review": 0, "unmanaged": 1} and v["broker_calls"] == 0
    sim = FakeRefresher([F_ROW])
    sim.store.snapshot()["source"] = "SIMULATED_PAPER"                                     # a simulated paper broker is never labelled as the Alpaca account
    s = AP.view(sim, store, loop_running=True)
    assert s["account_label"] == "Simulated paper account" and "not an Alpaca account" in s["source_label"] and s["host"] is None and "Simulated paper account" in s["message"]
    assert v["account_label"] == "Alpaca PAPER account" and v["source_label"] == "Alpaca PAPER account · read-only account refresh"
    stale = AP.view(FakeRefresher([F_ROW], status="STALE"), store, loop_running=True)
    assert stale["state"] == "LOADED" and "Account data: Stale" in stale["message"] and "may be out of date" in stale["message"]


def test_A04_the_account_positions_route_is_get_only_and_calls_no_broker(stack, monkeypatch):
    monkeypatch.setattr(stack.s, "account_refresh", FakeRefresher([F_ROW]))
    n = len(stack.sim.requests)
    r = stack.api.get("/api/account-refresh/positions").json()
    assert r["state"] == "LOADED" and [(p["symbol"], p["management"]) for p in r["positions"]] == [("F", "EXTERNAL")]
    assert len(stack.sim.requests) == n                                                    # no broker request
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/account-refresh")}
    assert mine == {"/api/account-refresh": {"get"}, "/api/account-refresh/history": {"get"}, "/api/account-refresh/positions": {"get"}}


def test_A05_external_positions_are_counted_in_monitor_only_from_the_account_refresh(stack, monkeypatch):
    monkeypatch.setattr(stack.s, "account_refresh", FakeRefresher([F_ROW], refreshed_at="2099-01-01T00:00:00+00:00"))
    n = len(stack.sim.requests)
    st = stack.x.status()
    assert st.mode == "MONITOR_ONLY" and st.external_positions == 1 and st.external_symbols == ["F"] and st.external_positions_source == "ACCOUNT_REFRESH"
    assert st.external_positions_as_of == "2099-01-01T00:00:00+00:00" and len(stack.sim.requests) == n and stack.sim.posts == []
    out = stack.api.get("/api/execution/status").json()
    assert out["external_positions"] == 1 and out["external_positions_source"] == "ACCOUNT_REFRESH" and out["mode"] == "MONITOR_ONLY"


def test_A06_the_executors_own_newer_broker_read_wins(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    monkeypatch.setattr(st.s, "account_refresh", FakeRefresher([F_ROW], refreshed_at="2000-01-01T00:00:00+00:00"))   # an older account read
    st.sim.set_position("AMD", 4, 50.0)
    arm(st)
    cyc(st)
    s = st.x.status()
    assert s.external_positions_source == "BROKER_RECONCILIATION" and s.external_symbols == ["AMD"]


def test_A07_trader_view_reads_the_account_refresh_and_tells_not_loaded_from_no_position():
    calls = []
    legacy = dict(alpaca_status=lambda: calls.append("status") or {"connection": {"state": "READY"}, "refreshed_at": None}, alpaca_view=lambda: calls.append("view") or {},
                  local_view=lambda: {}, awareness_enabled=lambda: False, cache_seconds=0)
    ad = TP.StockAgentPositionAdapter(account_view=lambda: AP.view(FakeRefresher(None, status="UNAVAILABLE"), SimpleNamespace(), loop_running=True), **legacy)
    p = ad.position("F")
    a = p.sources[0]
    assert a.source == "ALPACA_PAPER_ACCOUNT" and a.status == "NOT_LOADED" and "Not loaded yet" in a.message and p.has_position is False and calls == []
    store = SimpleNamespace(positions=lambda open_only=True, limit=200: [], open_intents=lambda: [])
    ad = TP.StockAgentPositionAdapter(account_view=lambda: AP.view(FakeRefresher([F_ROW]), store, loop_running=True), **legacy)
    p = ad.position("F")
    a = p.sources[0]
    assert a.status == "POSITION" and p.has_position and a.quantity == "1" and a.average_cost == "12.1" and a.unrealized_pnl_pct == "-4.30" and a.as_of == "2026-10-26T18:54:25+00:00"
    assert a.message.startswith("External · not managed by the bot.") and "Account data: Fresh" in a.message and "read-only account refresh" in a.message
    n = ad.position("NVDA").sources[0]
    assert n.status == "NO_POSITION" and n.message.startswith("No position in the Alpaca PAPER account.") and n.label == "Alpaca PAPER account"
    sim = FakeRefresher([F_ROW])
    sim.store.snapshot()["source"] = "SIMULATED_PAPER"
    a = TP.StockAgentPositionAdapter(account_view=lambda: AP.view(sim, store, loop_running=True), **legacy).position("F").sources[0]
    assert a.label == "Simulated paper account" and "not an Alpaca account" in a.message and "Alpaca PAPER account" not in a.label
    ad = TP.StockAgentPositionAdapter(account_view=lambda: {"state": "DISABLED"}, **legacy)  # no paper broker configured: the manual view is the Alpaca source
    a = ad.position("F").sources[0]
    assert a.source == "ALPACA_PAPER_VIEW" and a.status == "NOT_REFRESHED" and TP.ALPACA_VIEW_PATH in a.message and "Paper Trades" not in a.message


def test_A08_the_outdated_navigation_is_gone_and_the_new_one_exists():
    src = "".join((ROOT / f).read_text(encoding="utf-8") for f in ("trader_view/positions.py", "frontend/trader_view.js", "frontend/product_ui.js"))
    assert "Paper Trades › Alpaca Paper" not in src
    assert TP.ALPACA_VIEW_PATH == "MORE › Research › Backtests (rule builder, legacy lab) › Paper Portfolio › Alpaca Paper — Read Only"
    ui = (ROOT / "frontend" / "product_ui.js").read_text(encoding="utf-8")
    assert '{ tab: "strategy", label: "Backtests (rule builder, legacy lab)" }' in ui and '{ id: "positions", label: "POSITIONS", host: "paper" }' in ui
    pf = (ROOT / "frontend" / "paper_portfolio.js").read_text(encoding="utf-8")
    assert 'label: "Paper Portfolio"' in pf and "Alpaca Paper — Read Only" in pf and "Refresh Alpaca Paper" in (ROOT / "frontend" / "alpaca_paper.js").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
    assert 'R.stateHtml("NOT_LOADED", "NOT LOADED"' in js and 'p.sources.some((s) => s.status === "NO_POSITION")' in js


def test_A09_build_stream_wires_the_account_view_of_its_own_stream():
    src = (ROOT / "trader_view" / "stream.py").read_text(encoding="utf-8")
    assert 'StockAgentPositionAdapter(account_view=lambda: account_view(holder["s"].account_refresh, holder["s"].execution.store),' in src   # + Stage 6.9's Robinhood snapshot


# ================================================================ P Paper Auto ordering =========================================================

def test_P01_reconciliation_completes_before_any_entry_is_evaluated_or_submitted(tmp_path):
    st = make_stack(tmp_path)
    st.sim.set_position("F", 1, 12.1)                                                      # held before Paper Auto is ever enabled
    arm(st, "SNDK")
    n = len(st.sim.requests)
    out = cyc(st)                                                                          # the first Paper Auto cycle
    seq = [(m, p) for m, p, _ in st.sim.requests[n:]]
    first = lambda pred: next(i for i, x in enumerate(seq) if pred(x))  # noqa: E731
    recon = max(first(lambda x: x == ("GET", "/v2/positions")), first(lambda x: x == ("GET", "/v2/orders")))
    entry_reads = [i for i, x in enumerate(seq) if x in (("GET", "/v2/account"), ("GET", "/v2/clock")) or x[1].startswith("/v2/assets/") or x[0] == "POST"]
    assert entry_reads and recon < min(entry_reads), seq
    f = [x for x in st.x.store.findings() if x["kind"] == "EXTERNAL_POSITION"]
    assert [x["symbol"] for x in f] == ["F"] and f[0]["blocking"] is False                 # F was recorded external in the SAME cycle, before the scan
    assert out["summary"]["entries"]["submitted"] == ["SNDK"] and st.x.store.open_position("F") is None


def test_P02_an_external_position_blocks_a_bot_entry_in_its_symbol_and_counts_toward_the_cap(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    st.sim.set_position("SNDK", 1, 12.1)                                                   # external shares in the very symbol that turns READY
    arm(st, "SNDK")
    out = cyc(st)
    assert "BROKER_POSITION" in _blocked(st, out, "SNDK") and st.sim.posts == []
    ev = {e["symbol"]: e for e in out["summary"]["entries"]["evaluations"]}
    assert ev["SNDK"]["result"] == "BLOCKED" and any(r["control"] == "BROKER_POSITION" and "broker already holds" in r["text"] for r in ev["SNDK"]["reasons"])
    st2 = make_stack(tmp_path / "cap")
    st2.sim.set_position("F", 1, 12.1)
    st2.x.max_open_positions = 1                                                          # F alone fills the cap
    arm(st2, "SNDK")
    out = cyc(st2)
    assert "MAX_OPEN_POSITIONS" in _blocked(st2, out, "SNDK") and st2.sim.posts == []


def test_P03_an_unreadable_broker_blocks_every_entry_in_that_cycle(tmp_path):
    st = make_stack(tmp_path)
    st.sim.set_position("F", 1, 12.1)
    arm(st, "SNDK")
    st.sim.read_fail_paths = ["/v2/positions"]
    out = cyc(st)
    assert "BROKER_RECONCILIATION" in _blocked(st, out, "SNDK") and st.sim.posts == [] and st.x.recon_healthy is False
    assert any(f["kind"] == "BROKER_UNAVAILABLE" for f in st.x.store.findings())


def test_P04_per_ticker_reasons_are_persisted_including_skipped(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    go_ready(st, "AMD")
    arm(st, "SNDK")
    out = cyc(st)
    ev = {e["symbol"]: e for e in out["summary"]["entries"]["evaluations"]}
    assert set(ev) == {"AMD", "SNDK"} and sorted(e["result"] for e in ev.values()) == ["ENTRY_INTENT", "SKIPPED"]
    skipped = next(e for e in ev.values() if e["result"] == "SKIPPED")
    assert skipped["reasons"][0]["control"] == "ENTRIES_PER_CYCLE" and "per cycle" in skipped["reasons"][0]["text"]
    sent = next(e for e in ev.values() if e["result"] == "ENTRY_INTENT")
    assert sent["intent_state"] in ("SUBMITTED", "FILLED", "PARTIALLY_FILLED") and sent["broker_status"]
    run = st.x.store.last_run()
    assert run["summary"]["entries"]["evaluations"] == out["summary"]["entries"]["evaluations"]   # persisted with the run
    assert run["finished_at"] >= run["started_at"] and isinstance(run["summary"]["duration_ms"], int)


def test_P05_monitor_only_stays_idle_with_no_broker_call_and_no_write(tmp_path):
    st = make_stack(tmp_path)
    st.sim.set_position("F", 1, 12.1)
    go_ready(st, "SNDK")
    n = len(st.sim.requests)
    out = cyc(st)
    assert out["result"] == "IDLE" and st.x.mode()["mode"] == "MONITOR_ONLY" and len(st.sim.requests) == n and st.x.store.runs() == [] and st.x.progress is None
    a = ACT.activity(st.x, scheduler_status={"running": True, "heartbeat_at": out["at"]}, enabled=True, now=st.clock.now)
    t = {r["symbol"]: r for r in a["tickers"]}
    assert t["SNDK"]["status"] == "READY_MONITOR_ONLY" and "Monitor only" in t["SNDK"]["text"]
    assert a["last_cycle"]["result"] == "IDLE" and "no broker call" in a["last_cycle"]["text"] and len(st.sim.requests) == n


# ================================================================ B Bot Activity ================================================================

def test_B01_worker_states_come_from_the_heartbeat_not_the_dashboard():
    now = T
    iso = lambda s: (now - timedelta(seconds=s)).isoformat(timespec="seconds")  # noqa: E731
    base = {"running": True, "interval_s": 30.0, "idle_interval_s": 300.0, "max_sleep_s": 300.0, "market": {"regular_session": True}, "heartbeat_at": iso(5),
            "next_at": (now + timedelta(seconds=25)).isoformat(timespec="seconds"), "last_result": {"result": "RUN"}}
    W = lambda **kw: ACT.worker({**base, **kw}, kw.pop("_enabled", True), now)  # noqa: E731
    assert ACT.worker(base, False, now)["state"] == "DISABLED"
    assert W(running=False)["state"] == "STOPPED" and "dashboard is connected" in W(running=False)["text"]
    assert W(in_cycle=True, cycle_started_at=iso(1))["state"] == "RUNNING"
    assert W(heartbeat_at=None)["state"] == "STARTING"
    stale = W(heartbeat_at=iso(200))
    assert stale["state"] == "STALE" and stale["stale_after_s"] == 90 and stale["heartbeat_age_s"] == 200
    assert W(heartbeat_at=iso(200), market={"regular_session": False})["state"] == "IDLE"   # outside the session the loop wakes every 300 s
    assert W(last_result={"result": "ERROR", "error": "RuntimeError"}, last_error="RuntimeError")["state"] == "ERROR"
    assert W(last_result={"result": "LEASE_HELD"})["state"] == "STANDBY"
    idle = W()
    assert idle["state"] == "IDLE" and idle["label"] == "Idle" and idle["heartbeat_age_s"] == 5 and idle["expected_every_s"] == 30


def test_B02_the_scheduler_heartbeat_and_cycle_flags(tmp_path):
    clock = SimpleNamespace(now=T)
    seen = {}

    class Svc:
        def run_cycle(self, now, trigger):
            seen["mid"] = sch.status(now)
            return {"result": "RUN", "at": now.isoformat(), "finished_at": now.isoformat(), "trigger": trigger}
    sch = ExecutionScheduler(lambda: Svc(), clock=lambda: clock.now, interval_s=30, idle_interval_s=300)
    assert sch.status()["heartbeat_at"] is None and sch.status()["in_cycle"] is False
    sch.step(T)
    assert seen["mid"]["in_cycle"] is True and seen["mid"]["cycle_started_at"] == T.isoformat(timespec="seconds")
    s = sch.status(T)
    assert s["in_cycle"] is False and s["heartbeat_at"] == T.isoformat(timespec="seconds") and s["last_finished_at"] == T.isoformat(timespec="seconds") and s["runs"] == 1
    later = T + timedelta(seconds=5)
    assert sch.step(later) is None                                                        # not due: no cycle, but the heartbeat moves
    assert sch.status(later)["heartbeat_at"] == later.isoformat(timespec="seconds") and sch.status(later)["runs"] == 1


def test_B03_progress_reports_the_real_step_and_ticker_and_clears(tmp_path):
    st = make_stack(tmp_path)
    arm(st, "SNDK")
    seen = []
    st.x.hooks["after_intent"] = lambda ctx: seen.append(dict(st.x.progress))
    cyc(st)
    assert seen and seen[0]["step"] == "ENTRIES" and seen[0]["symbol"] == "SNDK" and seen[0]["index"] == 1 and seen[0]["total"] == 1
    assert seen[0]["step_label"] == "Evaluating READY tickers for entry" and st.x.progress is None


def test_B04_the_order_lifecycle_shows_only_broker_reported_states(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st, "SNDK")
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = entry_coid(execution_id_for(pv.proposal_id))
    (o,) = ACT.orders(st.x.store)
    labels = [s["label"] for s in o["steps"]]
    assert labels[:2] == ["Intent recorded (before any broker call)", "Sent to the broker"] and not any("Filled" in l for l in labels)
    assert [s["kind"] for s in o["steps"][2:]] == ["BROKER"] * (len(o["steps"]) - 2) and o["state"] == "SUBMITTED"
    st.sim.set_status(coid, "new")
    cyc(st, advance=31)
    st.sim.fill(coid, qty=1)                                                               # a partial fill, reported by the broker
    cyc(st, advance=31)
    labels = [s["label"] for s in ACT.orders(st.x.store)[0]["steps"]]
    assert "Accepted by the broker" in labels and any(l.startswith("Partially filled 1/") for l in labels) and not any(l.startswith("Filled") for l in labels)
    st.sim.fill(coid)
    cyc(st, advance=31)
    o = ACT.orders(st.x.store)[0]
    assert o["state"] == "FILLED" and o["steps"][-1]["label"].startswith("Filled") and o["steps"][-1]["kind"] == "BROKER"
    for status, label in (("rejected", "Rejected by the broker"), ("canceled", "Canceled or expired"), ("accepted", "Submitted (received by the broker)")):
        from paper import alpaca_order_rules as RU
        assert ACT.ORDER_LABELS[RU.map_status(status)[0]] == label


def test_B05_position_decisions_and_external_positions_marked_unmanaged(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    pv, pos = entered(st, "SNDK")
    cyc(st)
    monkeypatch.setattr(st.s, "account_refresh", FakeRefresher([F_ROW, {"symbol": "SNDK", "qty": str(pos.quantity)}]))
    a = ACT.activity(st.x, scheduler_status={"running": True}, enabled=True, account=AP.view(st.s.account_refresh, st.x.store, loop_running=True), now=st.clock.now)
    (b,) = a["positions"]["bot"]
    assert b["symbol"] == "SNDK" and b["decision"] == "HOLD" and b["decision_text"].startswith("No exit condition is met")
    acct = {r["symbol"]: r for r in a["positions"]["account"]["positions"]}
    assert acct["F"]["management"] == "EXTERNAL" and acct["F"]["managed"] is False and acct["SNDK"]["management"] == "BOT"
    below_invalidation(st, pv)
    st.sim.auto_fill = "manual"
    out = cyc(st)
    d = out["summary"]["decisions"][0]
    assert d["decision"] == "EXIT_SENT" and "Price reached the invalidation level" in d["text"]


def test_B06_quiet_cycles_collapse_and_activity_survives_a_restart(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    monkeypatch.setattr("execution.service.RUN_ROW_HEARTBEAT_S", 0.0)                   # every quiet cycle writes its row (the default writes one per 300 s)
    arm(st, "AMD")                                                                         # Paper Auto with a READY ticker whose entry is blocked every cycle
    st.safety.pause(actor="user")
    for _ in range(3):
        cyc(st)
    h = [x for x in ACT.history(st.x, st.x.store, st.x.store.runs(30)) if x["kind"] == "CYCLE"]
    assert len(h) == 1 and h[0]["count"] == 3 and h[0]["first_at"] < h[0]["at"] and "No trade." in h[0]["text"]
    x2 = reopen(st)                                                                        # a restart: nothing in memory
    a = ACT.activity(x2, scheduler_status={"running": False}, enabled=True, now=st.clock.now)
    assert a["last_cycle"]["source"] == "DATABASE" and a["worker"]["state"] == "STOPPED"
    amd = {t["symbol"]: t for t in a["tickers"]}["AMD"]
    assert amd["status"] == "BLOCKED" and any(r["control"] == "SAFETY_GATE" and r["label"] == "Risk gate" for r in amd["reasons"])


def test_B07_activity_makes_no_broker_call_carries_no_credential_and_no_model(stack, monkeypatch):
    secret = "PKSECRETSENTINEL0123456789"
    monkeypatch.setattr(config, "ALPACA_PAPER_API_KEY", secret, raising=False)
    monkeypatch.setattr(config, "ALPACA_PAPER_SECRET_KEY", secret + "S", raising=False)
    monkeypatch.setattr(stack.s, "account_refresh", FakeRefresher([F_ROW]))
    entered(stack, "SNDK")
    n = len(stack.sim.requests)
    r = stack.api.get("/api/execution/activity")
    assert r.status_code == 200 and len(stack.sim.requests) == n
    body = r.text
    assert secret not in body and "account_number" not in body and "raw" not in json.loads(body)["orders"][0]
    a = r.json()
    assert a["broker_calls"] == 0 and a["mode"]["mode"] == "PAPER_AUTO" and {"worker", "progress", "last_cycle", "tickers", "risk", "orders", "positions", "history"} <= set(a)
    src = (ROOT / "execution" / "activity.py").read_text(encoding="utf-8") + (ROOT / "execution" / "account_positions.py").read_text(encoding="utf-8")
    assert not re.search(r"(?i)import\s+(anthropic|openai)|ai_explain|requests\.|\.post\(|post_order", src)
    from api.server import app
    ex = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/execution") and not p.startswith("/api/execution-safety")}
    assert ex["/api/execution/activity"] == {"get"} and sum("post" in o for o in ex.values()) == 2   # the two mode actions only


def test_B08_the_frontend_panel_and_the_positions_card_are_wired():
    ui = (ROOT / "frontend" / "product_ui.js").read_text(encoding="utf-8")
    for k in ('getJson("/api/execution/activity")', 'getJson("/api/account-refresh/positions")', "accountPositionsHtml(state.acctPos, state.acctPosError)",
              'cards.insertAdjacentElement("afterend", el)', "if (ensureBotActivity()) loadBotActivity();",
              # View details (the window-fitting Stage 5.5 layout): hosted inside the detail column before the sticky Actions, re-hosted on every toggle
              "inColumn = !collapsed() && !!right;", "right.insertBefore(el, last);", 'b.setAttribute("aria-expanded", v ? "false" : "true"); } placeBotActivity(); }'):
        assert k in ui, k
    assert not re.search(r"(?i)ai_explain|/api/ai|anthropic", ui[ui.index("Stage 6.8: account positions + Bot Activity"):ui.index("page wiring (browser only)")])


class Exploding(dict):
    """A recording store that fails on every use (a real fault inside the recording code, not an injected hook)."""
    def __setitem__(self, k, v):
        raise RuntimeError("recording store failed")

    def pop(self, *a):
        raise RuntimeError("recording store failed")

    def get(self, *a):
        raise RuntimeError("recording store failed")


def _order_handling(st):
    """Entry POST -> partial fill -> fill -> hold -> exit decided -> exit POST -> fill -> closed; returns everything order handling decided."""
    pv = arm(st, "SNDK")
    st.sim.auto_fill = "manual"
    cyc(st)
    coid = entry_coid(execution_id_for(pv.proposal_id))
    st.sim.fill(coid, qty=1)
    cyc(st, advance=31)
    st.sim.fill(coid)
    cyc(st, advance=31)
    cyc(st, advance=31)
    below_invalidation(st, pv)
    st.sim.auto_fill = "next_read"
    for _ in range(3):
        cyc(st, advance=31)
    x = st.x
    intents = sorted(x.store.intents(), key=lambda i: (i.created_at, i.intent_type))
    return {"intents": [(i.intent_type, i.symbol, i.side, i.qty, i.state, i.filled_qty, i.filled_avg_price, i.attempt_count, i.broker_status) for i in intents],
            "orders": sorted((o["symbol"], o["side"], o["status"], o["filled_qty"]) for o in x.store.orders(100)),
            "order_events": len(x.store.order_events(limit=500)), "posts": len(st.sim.posts),
            "positions": [(p.symbol, p.state, p.quantity, p.entry_qty, p.average_entry_price, p.exit_reason, p.realized_pnl) for p in x.store.positions(open_only=False)],
            "runs": [(r["result"], r["error"]) for r in x.store.runs(100)], "mode": x.mode()["mode"], "recon": x.recon_healthy}


def test_B09_a_failing_activity_record_never_changes_or_interrupts_order_handling(tmp_path, monkeypatch):
    clean = _order_handling(make_stack(tmp_path / "clean"))
    assert clean["positions"][0][1] == "CLOSED" and clean["posts"] == 2 and all(e is None for _, e in clean["runs"])
    hooked = make_stack(tmp_path / "hooked")                                              # a failure injected at EVERY recording call site
    calls = []
    hooked.x.hooks["before_record"] = lambda ctx: (calls.append(1), (_ for _ in ()).throw(RuntimeError("recording failed")))
    assert _order_handling(hooked) == clean and len(calls) > 20
    real = make_stack(tmp_path / "real")                                                   # real faults inside the recording code itself
    real.x._last_touch = Exploding()
    monkeypatch.setattr(S, "CYCLE_STEPS", {})                                              # every step label lookup fails
    assert _order_handling(real) == clean
    runs = real.x.store.runs(100)
    assert all(r["error"] is None for r in runs) and real.x.progress is None
    assert not any((r["summary"].get("entries") or {}).get("evaluations") or r["summary"].get("decisions") for r in hooked.x.store.runs(100))   # the records were skipped, nothing else


def test_B10_the_current_mode_and_the_last_cycles_mode_are_separate_and_awaiting_is_explicit(tmp_path):
    st = make_stack(tmp_path)
    go_ready(st, "SNDK")
    idle = cyc(st)                                                                         # Monitor only: an IDLE cycle
    sched = lambda: {"running": True, "heartbeat_at": idle["at"], "next_at": (st.clock.now + timedelta(seconds=272)).isoformat(timespec="seconds"),  # noqa: E731
                     "interval_s": 30.0, "idle_interval_s": 300.0, "max_sleep_s": 300.0, "market": {"regular_session": False}, "last_result": {"result": "IDLE"}}
    st.clock.advance(28)
    st.x.enable_auto(actor="test", now=st.clock.now)                                      # Paper Auto switched on AFTER that cycle
    a = ACT.activity(st.x, scheduler_status=sched(), enabled=True, now=st.clock.now)
    m = a["mode"]
    assert m["mode"] == "PAPER_AUTO" and m["changed_at"] > a["last_cycle"]["at"] and a["last_cycle"]["mode"] == "MONITOR_ONLY" and m["last_cycle_mode"] == "MONITOR_ONLY"
    assert m["awaiting_first_cycle"] is True and m["note"].startswith("Paper Auto since ") and "awaiting its first cycle (next scheduled run " in m["note"]
    assert "ran in Monitor only" in m["note"] and a["latest_cycle_evaluations"] == [] and a["last_cycle"]["text"].startswith("Monitor only")
    not_running = ACT.activity(st.x, scheduler_status={**sched(), "running": False}, enabled=True, now=st.clock.now)["mode"]["note"]
    assert "the worker is not running, so no cycle is scheduled" in not_running
    cyc(st)                                                                                # the first Paper Auto cycle
    a = ACT.activity(st.x, scheduler_status=sched(), enabled=True, now=st.clock.now)
    assert a["mode"]["awaiting_first_cycle"] is False and a["mode"]["last_cycle_mode"] == "PAPER_AUTO" and a["mode"]["note"] is None
    assert a["latest_cycle_evaluations"] == [{"symbol": "SNDK", "result": "ENTRY_INTENT"}]
    st.x.disable_auto(actor="test", now=st.clock.now)
    a = ACT.activity(st.x, scheduler_status=sched(), enabled=True, now=st.clock.now)
    assert a["mode"]["awaiting_first_cycle"] is False and a["mode"]["note"].startswith("Monitor only since ") and "ran in Paper Auto" in a["mode"]["note"]


def test_B11_ticker_rows_tell_the_latest_cycle_from_earlier_cycles_and_the_setup_state(tmp_path):
    st = make_stack(tmp_path)
    go_ready(st, "AMD")
    arm(st, "SNDK")
    st.safety.pause(actor="user")
    first = cyc(st)                                                                         # both READY tickers blocked by the paused gate
    a = ACT.activity(st.x, scheduler_status={"running": True}, enabled=True, now=st.clock.now)
    rows = {r["symbol"]: r for r in a["tickers"]}
    assert {s: (r["status"], r["scope"]) for s, r in rows.items()} == {"AMD": ("BLOCKED", "LATEST_CYCLE"), "SNDK": ("BLOCKED", "LATEST_CYCLE")}
    assert rows["SNDK"]["evaluated_at"] == first["at"] == a["last_cycle"]["at"] and rows["SNDK"]["checked_at"] is None
    assert sorted((e["symbol"], e["result"]) for e in a["latest_cycle_evaluations"]) == [("AMD", "BLOCKED"), ("SNDK", "BLOCKED")]
    st.x.disable_auto(actor="user", now=st.clock.now)
    cyc(st)                                                                                 # an IDLE Monitor-only cycle: no ticker evaluated
    st.x.enable_auto(actor="user", now=st.clock.now)                                        # Paper Auto again, awaiting its first cycle
    a = ACT.activity(st.x, scheduler_status={"running": True}, enabled=True, now=st.clock.now)
    rows = {r["symbol"]: r for r in a["tickers"]}
    assert rows["SNDK"]["scope"] == "EARLIER_CYCLE" and rows["SNDK"]["evaluated_at"] == first["at"] != a["last_cycle"]["at"] and a["latest_cycle_evaluations"] == []
    st.setups.evaluate_symbol("AMD", trigger="TEST", inputs=__import__("test_setups_58").fx("AMD", **__import__("test_setups_58").WATCH_IN))   # AMD leaves READY
    a = ACT.activity(st.x, scheduler_status={"running": True}, enabled=True, now=st.clock.now)
    amd = {r["symbol"]: r for r in a["tickers"]}["AMD"]
    assert amd["scope"] == "SETUP" and amd["status"] == "WAITING" and amd["checked_at"] and amd["evaluated_at"] is None


# ================================================================ F freeze ======================================================================

def test_F01_every_change_since_main_is_a_new_file_or_an_exact_ledger_entry():
    import freeze_ledger as FL
    base = FL.BASE_68
    assert subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=ROOT).returncode == 0, "main must be reachable (fetch-depth 0)"
    changed = set(subprocess.run(["git", "diff", "--name-only", base], cwd=ROOT, capture_output=True, text=True).stdout.split()) | \
        set(subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    in_base = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", base], cwd=ROOT, capture_output=True, text=True).stdout.split())
    new = {p for p in changed if p not in in_base}
    assert new == FL.NEW_68 | FL.NEW_69, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69), "missing_new": sorted((FL.NEW_68 | FL.NEW_69) - new)}   # + Stage 6.9
    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                      # the ledger restores main exactly (and the hash pins today's file)


REPLACED_68 = {                                                                          # the ONLY lines of main's executor / scheduler that Stage 6.8 replaced
    "execution/service.py": [
        "from datetime import datetime, timezone",
        '                self.last_cycle = {"at": _iso(now), "result": "IDLE", "trigger": trigger, "mode": mode}',
        "                    self.store.add_run(run_id, now, now, trigger, mode, result, summary, error, self.owner)",
        '            self.last_cycle = {"at": _iso(now), "result": result, "trigger": trigger, "mode": mode, "run_id": run_id, "summary": summary, "error": error}',
        '        out = {"positions": 0, "exits_decided": 0, "exits_submitted": 0, "exits_deferred": 0}',
        '        out: Dict[str, Any] = {"candidates": [], "submitted": [], "blocked": {}}',
        "        for ep in episodes:"],
    "execution/scheduler.py": [
        '                "last_result": {k: v for k, v in (self.last_result or {}).items() if k in ("result", "at", "trigger", "error")}}'],
    "execution/models.py": [],
}


def test_F02_the_executor_and_scheduler_only_gained_display_bookkeeping_and_every_guard_line_is_kept():
    import freeze_ledger as FL
    for rel, replaced in REPLACED_68.items():
        base, now = FL.base_text(rel).splitlines(), set(FL.now_text(rel).splitlines())
        assert [l for l in base if l not in now] == replaced, rel                           # every other line of main is still there, verbatim
    svc, sch = FL.now_text("execution/service.py"), FL.now_text("execution/scheduler.py")
    for guard in ("it = self.store.update_intent(it.execution_id, now, attempt_count=1, submitted_at=_iso(now))          # the attempt is recorded BEFORE the wire",
                  "        if not self.needs_broker():", "                summary[\"findings\"] = self._reconcile(now, trigger, run_id)", "            controls = entry_controls(ctx)",
                  "            blocked = blocking(controls)", "        with self._lock:"):
        assert guard in svc, guard
    assert svc.index('summary["findings"] = self._reconcile(now, trigger, run_id)') < svc.index('summary["entries"] = self._scan_entries(now, trigger, sv)')   # reconcile, then entries
    for guard in ("if not lease.acquire(self.owner, now, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):", "if not self._lock.acquire(blocking=False):", "lease.release(self.owner, name=LEASE_NAME)"):
        assert guard in sch, guard
    new_src = (ROOT / "execution" / "activity.py").read_text(encoding="utf-8") + (ROOT / "execution" / "account_positions.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(post_order|set_mode|enable_auto|disable_auto|insert_intent|update_intent|update_position|add_run|touch_controls)\(", new_src)   # read-only
    import execution as X
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")                                     # the frozen-controls hash is pinned by tests/test_product_flow_612.py and _62.py


def test_F03_the_ledger_rejects_an_unregistered_change_to_a_registered_file_and_passes_frozen_files_through_unchanged():
    import freeze_ledger as FL
    svc = FL.now_text("execution/service.py")
    assert FL.view("execution/service.py", svc) == FL.base_text("execution/service.py")                    # the registered text reads as main
    tampered = svc.replace("self.max_entries_per_cycle = int(", "self.max_entries_per_cycle = 1 + int(", 1)  # one unauthorized executor change
    assert tampered != svc
    with pytest.raises(AssertionError, match="differs from the Stage 6.8 ledger entry"):
        FL.view("execution/service.py", tampered)
    eng = FL.now_text("execution/engine.py")                                                              # a frozen file Stage 6.8 did NOT register
    assert "execution/engine.py" not in FL.LEDGER and eng == FL.base_text("execution/engine.py")
    assert FL.view("execution/engine.py", eng + "# unauthorized\n") == eng + "# unauthorized\n"           # passed through: the old freeze tests compare real bytes
