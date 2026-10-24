"""
Stage 6.0.1 - CONTINUOUS ALPACA PAPER ACCOUNT REFRESH. Focused tests against the REAL wiring (the Trader View stream builds the default proposal
account adapter and the default Stage 5.9.1 gate on top of the refresher) with the deterministic simulated paper broker below the real paper
client; a scripted wire injects 429 / 5xx / timeouts / broken payloads. No socket is opened.

  R refresh / atomicity / freshness · F failures, retries, recovery · S safety integration · E execution integration (Paper Auto, exits)
  L loop: lease, overlap, cadence, startup, restart · N boundaries (no order, paper only, secrets, Robinhood, model) · A API · U frontend · Z pins
"""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import BaseAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database.database as dbm  # noqa: E402
import account_refresh as AR  # noqa: E402
from account_refresh.scheduler import AccountRefreshScheduler, start_if_enabled  # noqa: E402
from account_refresh.service import AccountRefreshService, RefreshFailure  # noqa: E402
from account_refresh.store import AccountRefreshStore  # noqa: E402
from database.account_refresh_migrations import TABLES, run_account_refresh_migrations  # noqa: E402
from execution import broker as BK  # noqa: E402
from execution.service import ExecutionService  # noqa: E402
from execution.simulated import SimulatedPaperBroker  # noqa: E402
from execution.store import ExecutionStore  # noqa: E402
from setups.service import SetupService  # noqa: E402
from setups.store import SetupStore  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_trader_view_55 import T0, WL, FakePositions, FakeResearch  # noqa: E402
from test_setups_58 import READY_IN, WATCH_IN, FakeClock, fx  # noqa: E402

BASE = "1738956"                                                     # stage-60-autonomous-paper HEAD this stage branched from
UTC = timezone.utc
FAKE_KEY, FAKE_SECRET = "REFRESHTEST-paperkey-0a1b2c3d", "REFRESHTEST-papersecret-4e5f6a7b"
SRC = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "account_refresh").glob("*.py")) + ["api/routes/account_refresh.py", "database/account_refresh_migrations.py"]


class RealishMarket(SimulatedMarket):
    mode = "real"                                                     # the real-data account path (Alpaca snapshot) on simulated quotes


class ScriptedWire(BaseAdapter):
    """Below the real paper client: scripted answers for GET paths, then the simulated paper broker."""

    def __init__(self, sim):
        super().__init__()
        self.sim, self.script = sim, []                              # list of (path, kind[, arg])

    def send(self, request, **kw):
        from urllib.parse import urlsplit
        path = urlsplit(request.url).path
        for i, item in enumerate(self.script):
            if item[0] == path:
                self.script.pop(i)
                kind = item[1]
                if kind == "timeout":
                    raise requests.exceptions.ReadTimeout("scripted timeout")
                if kind == "reset":
                    raise requests.exceptions.ConnectionError("scripted reset")
                r = requests.Response()
                r.status_code, r.url, r.request = (item[2] if kind == "status" else 200), request.url, request
                body = item[2] if kind == "body" else {"code": 0, "message": "scripted"}
                r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
                return r
        return self.sim.send(request, **kw)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def refresh_config(monkeypatch):
    monkeypatch.setattr(config, "TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", False)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", False)
    monkeypatch.setattr(config, "EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", False)
    monkeypatch.setattr(config, "EXECUTION_MAX_SHARES_PER_ENTRY", 0)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_STALE_SECONDS", 600.0)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_MAX_FAILURES", 3)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_READ_ATTEMPTS", 3)
    hits = []

    def tripwire():
        hits.append("wire")
        raise AssertionError("the real paper network adapter was requested")
    monkeypatch.setattr(BK, "WIRE", tripwire)
    yield
    assert not hits


class Rig:
    pass


def make_rig(tmp_path, monkeypatch, *, market=None, clock=None, sim=None, real_creds=False, db_name="stage601.db"):
    """The stream's DEFAULT proposal / safety wiring on a temporary database, the refresher and the executor on one simulated paper account."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / db_name
    monkeypatch.setattr(dbm, "_db_instance", dbm.ResearchDatabase(db))
    r = Rig()
    r.clock = clock or FakeClock()
    r.sim = sim or SimulatedPaperBroker(now_fn=r.clock)
    r.wire = ScriptedWire(r.sim)
    r.sleeps = []
    creds = (lambda: (FAKE_KEY, FAKE_SECRET)) if real_creds else None
    mk = (lambda: BK.AlpacaPaperBroker(credentials_fn=creds, wire=lambda: r.wire)) if real_creds else (lambda: BK.AlpacaPaperBroker(simulated=True, wire=lambda: r.wire))
    r.ref = AccountRefreshService(AccountRefreshStore(db), broker=mk(), clock=r.clock, sleep=r.sleeps.append)
    holder = {}
    r.setups = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(db), clock=r.clock, cooldown_minutes=15)
    r.x = ExecutionService(stream_fn=lambda: holder["s"], store=ExecutionStore(db), broker=mk(), clock=r.clock)
    r.s = TerminalStream(market or SimulatedMarket(seed=11, start_time=T0), FakePositions(), FakeResearch(), watchlist_fn=lambda: list(WL), tick_seconds=1.0,
                         allow_manual_tick=True, run_loop=False, now_fn=lambda: T0, setup_service=r.setups, execution_service=r.x, account_refresh_service=r.ref)
    holder["s"] = r.s
    r.s.proposals.clock = r.clock
    r.s.safety.clock = r.clock
    r.db, r.holder, r.mk = db, holder, mk
    return r


def go_ready(r, symbol="SNDK"):
    r.setups.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **WATCH_IN))
    r.clock.advance(60)
    r.setups.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **READY_IN))
    pv = r.s.proposals.current(symbol, r.clock.now)
    assert pv.effective_status == "REVIEWABLE", (pv.effective_status, pv.blockers)
    r.sim.set_price(symbol, float(pv.reference_price))
    return pv


def gate(r):
    return r.s.safety.evaluate("TEST", r.clock.now)


def fail_next(r, n_refreshes=1, kind="reset"):
    for _ in range(n_refreshes * r.ref.read_attempts):
        r.wire.script.append(("/v2/account", kind))


# ================================================================ R refresh / atomicity / freshness ======================================

def test_R01_a_refresh_reads_three_paper_gets_and_persists_a_complete_snapshot(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.sim.set_position("F", 1, 12.0)
    o = r.sim._order_json({"client_order_id": "manual-x", "symbol": "MU", "qty": "2", "type": "market", "side": "buy", "time_in_force": "day"}, "held")
    r.sim.orders[o["id"]] = o
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "SUCCESS" and out["read_attempts"] == 1
    assert [(m, p) for m, p, _ in r.sim.requests] == [("GET", "/v2/account"), ("GET", "/v2/positions"), ("GET", "/v2/orders")] and r.sim.posts == []
    snap = r.ref.store.snapshot()
    a = snap["account"]
    assert (a["status"], a["equity"], a["cash"], a["buying_power"], a["trading_blocked"], a["account_blocked"]) == ("ACTIVE", "100000.00", "100000.00", "200000.00", False, False)
    assert a["account_number_masked"] == "****0060" and "account_number" not in a and "id" not in a
    assert snap["positions"][0]["symbol"] == "F" and snap["positions"][0]["qty"] == "1" and snap["open_orders"][0]["symbol"] == "MU" and snap["open_orders"][0]["status"] == "held"
    assert snap["refreshed_at"] == r.clock.now.isoformat(timespec="seconds") and snap["source"] == "SIMULATED_PAPER" and snap["host"] == "paper-api.alpaca.markets"
    live = r.ref.store.liveness()
    assert (live["attempt_count"], live["success_count"], live["failure_count"], live["consecutive_failures"], live["last_result"]) == (1, 1, 0, 0, "SUCCESS")
    fr = r.ref.freshness()
    assert fr["status"] == "FRESH" and fr["fresh"] and fr["age_seconds"] == 0 and fr["label"] == "Account data: Fresh (0s ago)"
    v = r.ref.view_payload()
    assert set(v) >= {"connection", "refreshed_at", "account", "positions", "orders"} and v["connection"]["state"] == "CONNECTED" and v["orders"][0]["id_short"] == o["id"][:8]


def test_R02_a_partial_or_invalid_read_never_replaces_the_good_snapshot(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    assert r.ref.refresh(trigger="TEST")["result"] == "SUCCESS"
    good = r.ref.store.snapshot()
    r.clock.advance(60)
    for _ in range(r.ref.read_attempts):
        r.wire.script.append(("/v2/positions", "status", 503))           # account succeeds, positions fail
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "FAILED" and out["error_code"] == "HTTP_503" and r.ref.store.snapshot() == good
    r.clock.advance(60)
    r.wire.script.append(("/v2/account", "body", {"status": "ACTIVE", "equity": "abc", "cash": "1", "buying_power": "1", "trading_blocked": False, "account_blocked": False}))
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "INVALID" and out["read_attempts"] == 1 and "equity is not a number" in out["error_text"] and r.ref.store.snapshot() == good
    attempts = r.ref.store.attempts()
    assert [a["result"] for a in attempts] == ["INVALID", "FAILED", "SUCCESS"] and attempts[0]["refresh_id"] is None
    live = r.ref.store.liveness()
    assert (live["success_count"], live["failure_count"], live["consecutive_failures"], live["last_success_at"]) == (1, 2, 2, good["refreshed_at"])
    with sqlite3.connect(r.db) as conn:                                                   # the history is append-only
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM account_refresh_attempts")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE account_refresh_attempts SET result = 'SUCCESS'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM account_refresh_snapshot")


def test_R03_age_makes_the_data_stale_and_the_next_success_recovers_it(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.ref.refresh(trigger="TEST")
    r.clock.advance(599)
    assert r.ref.freshness()["status"] == "FRESH"
    r.clock.advance(2)
    fr = r.ref.freshness()
    assert fr["status"] == "STALE" and "601 s ago" in fr["reason"] and fr["label"] == "Account data: Stale" and r.ref.view_payload()["connection"]["state"] == "STALE"
    assert gate(r).state == "NO_NEW_ENTRIES"
    r.ref.refresh(trigger="TEST")
    assert r.ref.freshness()["status"] == "FRESH" and gate(r).state == "TRADING_ALLOWED"


def test_R04_freshness_is_never_inferred_from_startup_and_is_disabled_without_a_broker(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    assert r.ref.freshness()["status"] == "UNAVAILABLE" and gate(r).state == "NO_NEW_ENTRIES"
    off = AccountRefreshService(AccountRefreshStore(tmp_path / "off.db"), broker=BK.AlpacaPaperBroker(credentials_fn=lambda: (None, None), wire=lambda: r.wire))
    fr = off.freshness()
    assert fr["status"] == "DISABLED" and fr["label"] == "Account data unavailable" and off.refresh()["result"] == "NOT_CONFIGURED" and off.store.attempt_count() == 0


# ================================================================ F failures, retries, recovery ==========================================

def test_F01_three_failed_refreshes_make_the_data_stale_and_one_success_recovers(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.ref.refresh(trigger="TEST")
    for n in (1, 2, 3):
        r.clock.advance(60)
        fail_next(r)
        assert r.ref.refresh(trigger="TEST")["result"] == "FAILED"
        assert r.ref.freshness()["status"] == ("FRESH" if n < 3 else "STALE")
    assert "3 consecutive failed refreshes" in r.ref.freshness()["reason"] and gate(r).state == "NO_NEW_ENTRIES"
    r.clock.advance(60)
    assert r.ref.refresh(trigger="TEST")["result"] == "SUCCESS"
    fr = r.ref.freshness()
    assert fr["status"] == "FRESH" and fr["consecutive_failures"] == 0 and fr["failure_count"] == 3 and gate(r).state == "TRADING_ALLOWED"


@pytest.mark.parametrize("kind,arg,code", [("status", 429, "RATE_LIMITED"), ("status", 500, "HTTP_500"), ("status", 502, "HTTP_502"), ("timeout", None, "TIMEOUT"),
                                            ("reset", None, "CONNECTION"), ("body", b"<html>gateway</html>", "UNREADABLE")])
def test_F02_retryable_read_failures_retry_with_bounded_backoff_then_succeed(tmp_path, monkeypatch, kind, arg, code):
    r = make_rig(tmp_path, monkeypatch)
    for _ in range(2):
        r.wire.script.append(("/v2/account", kind, arg) if arg is not None else ("/v2/account", kind))
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "SUCCESS" and out["read_attempts"] == 3 and r.sleeps == [2.0, 5.0] and r.sim.posts == []
    r2 = make_rig(tmp_path / "x", monkeypatch)
    for _ in range(5):
        r2.wire.script.append(("/v2/account", kind, arg) if arg is not None else ("/v2/account", kind))
    out = r2.ref.refresh(trigger="TEST")
    assert out["result"] == "FAILED" and out["error_code"] == code and out["read_attempts"] == 3 and r2.sleeps == [2.0, 5.0]     # never an infinite retry
    assert r2.ref.store.snapshot() is None and r2.ref.freshness()["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("body,needle", [({"status": "ACTIVE", "cash": "1", "buying_power": "1", "trading_blocked": False, "account_blocked": False}, "equity"),
                                         ({"status": "ACTIVE", "equity": "-5", "cash": "1", "buying_power": "1", "trading_blocked": False, "account_blocked": False}, "out of range"),
                                         ({"status": "ACTIVE", "equity": "1", "cash": "1", "buying_power": "1", "trading_blocked": "no", "account_blocked": False}, "boolean"),
                                         ({"status": "", "equity": "1", "cash": "1", "buying_power": "1", "trading_blocked": False, "account_blocked": False}, "status invalid"),
                                         ([1, 2], "not an object")])
def test_F03_invalid_payloads_are_rejected_without_retry_and_without_a_write(tmp_path, monkeypatch, body, needle):
    r = make_rig(tmp_path, monkeypatch)
    r.wire.script.append(("/v2/account", "body", body))
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "INVALID" and out["read_attempts"] == 1 and needle in out["error_text"] and r.sleeps == [] and r.ref.store.snapshot() is None
    r3 = make_rig(tmp_path / "p", monkeypatch)
    r3.wire.script.append(("/v2/positions", "body", [{"symbol": "AAPL", "qty": "x"}]))
    assert r3.ref.refresh(trigger="TEST")["result"] == "INVALID" and r3.ref.store.snapshot() is None


def test_F04_a_test_mode_fault_file_fails_refreshes_without_any_broker_call(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    fault = tmp_path / "account_refresh.fault"
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_TEST_FAULTS", True)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_FAULT_FILE", str(fault))
    fault.write_text("x")
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "FAILED" and out["error_code"] == "TEST_FAULT" and r.sim.requests == []
    fault.unlink()
    assert r.ref.refresh(trigger="TEST")["result"] == "SUCCESS"
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_TEST_FAULTS", False)
    fault.write_text("x")                                                                # ignored unless test faults are switched on
    assert r.ref.refresh(trigger="TEST")["result"] == "SUCCESS"


# ================================================================ S safety integration =================================================

def test_S01_simulated_data_mode_fresh_allows_and_any_unhealthy_refresh_pauses_never_allows(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and "EQUITY_NOT_AVAILABLE" in v.reason_codes                      # nothing refreshed yet: never allowed
    r.ref.refresh(trigger="TEST")
    v = gate(r)
    assert v.state == "TRADING_ALLOWED" and v.daily_loss.equity_source == "HYPOTHETICAL_REVIEW"
    for _ in range(3):
        r.clock.advance(60)
        fail_next(r)
        r.ref.refresh(trigger="TEST")
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and "EQUITY_NOT_AVAILABLE" in v.reason_codes and v.allowed_new_entry is False


def test_S02_real_data_mode_reads_the_refreshed_alpaca_equity_and_keeps_the_unadjusted_pnl_limitation(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, market=RealishMarket(seed=11, start_time=T0))
    r.ref.refresh(trigger="TEST")
    v = gate(r)
    assert v.daily_loss.equity_source == "ALPACA_PAPER_VIEW" and v.daily_loss.current_equity == "100000.00" and v.daily_loss.equity_age_s == 0
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["DAILY_PNL_ADJUSTMENT_UNAVAILABLE"]                # fresh, but deposits / withdrawals still cannot be separated
    assert config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False and r.s.safety.accept_unadjusted is False
    r.s.safety.accept_unadjusted = True                                                                       # only an explicit operator choice changes that
    assert gate(r).state == "TRADING_ALLOWED"
    for _ in range(3):
        r.clock.advance(60)
        fail_next(r)
        r.ref.refresh(trigger="TEST")
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["ACCOUNT_SNAPSHOT_STALE"] and v.daily_loss.current_equity == "100000.00"   # equity kept for observability
    r.clock.advance(60)
    r.ref.refresh(trigger="TEST")
    assert gate(r).state == "TRADING_ALLOWED"
    pv = r.s.proposals.evaluate("SNDK", r.clock.now, "TEST")
    assert pv.equity_source in ("NOT_AVAILABLE", "ALPACA_PAPER_VIEW")


def test_S03_proposals_size_against_the_refreshed_snapshot_in_real_mode(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, market=RealishMarket(seed=11, start_time=T0))
    r.sim.account["equity"] = "80000.00"
    r.ref.refresh(trigger="TEST")
    snap = r.s.proposals.account.snapshot("SNDK", "real", r.clock.now.timestamp())
    assert (snap.equity, snap.equity_source, snap.buying_power, snap.equity_as_of) == ("80000.00", "ALPACA_PAPER_VIEW", "200000.00", r.ref.store.snapshot()["refreshed_at"])


def test_S04_without_a_configured_broker_the_injected_sources_equal_the_old_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "_db_instance", dbm.ResearchDatabase(tmp_path / "d.db"))
    s = TerminalStream(RealishMarket(seed=1, start_time=T0), FakePositions(), FakeResearch(), watchlist_fn=lambda: [], run_loop=False, now_fn=lambda: T0)
    assert s.account_refresh.enabled() is False and s.account_refresh.freshness()["status"] == "DISABLED"
    from paper import alpaca_view as AV
    fake = {"connection": {"state": "CONNECTED"}, "refreshed_at": datetime.now(UTC).isoformat(timespec="seconds"), "account": {"equity": "1234.00", "cash": "1", "buying_power": "9"},
            "positions": [], "orders": []}
    monkeypatch.setattr(AV, "view", lambda *a, **k: fake)
    snap = s.proposals.account.snapshot("AAPL", "real", datetime.now(UTC).timestamp())
    assert (snap.equity, snap.equity_source) == ("1234.00", "ALPACA_PAPER_VIEW")                                # the manual 4.6A view, exactly as before
    from safety.service import ExecutionSafetyService
    from safety.store import SafetyStore
    ref = ExecutionSafetyService(stream_fn=lambda: s, store=SafetyStore(tmp_path / "d.db"))                    # the gate's own default account source
    mine, theirs = s.safety.account(datetime.now(UTC).timestamp()), ref.account(datetime.now(UTC).timestamp())
    assert mine.model_dump(exclude={"age_s"}) == theirs.model_dump(exclude={"age_s"})


# ================================================================ E execution integration ================================================

def test_E01_paper_auto_stays_operational_for_hours_with_a_fresh_snapshot_and_refresh_sends_nothing(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(datetime(2026, 10, 21, 13, 31, tzinfo=UTC)))    # 09:31 New York
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300, after_refresh=lambda: r.s.safety.evaluate("SCHEDULED", r.clock.now))
    sched.step(r.clock.now)
    pv = go_ready(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    states, minutes = set(), 0
    while minutes < 240:                                                                   # four hours of the regular session
        r.clock.advance(30)
        minutes += 0.5
        sched.step(r.clock.now)
        r.x.run_cycle(r.clock.now, "TEST")
        if minutes % 30 == 0:
            states.add(gate(r).state)
            assert r.ref.freshness()["age_seconds"] <= 60
    assert states == {"TRADING_ALLOWED"} and sched.runs >= 239
    assert [json.loads(b)["side"] for b in r.sim.posts] == ["buy"] and len(r.x.store.intents()) == 1                  # one entry for the one setup episode
    gets = [p for m, p, _ in r.sim.requests if m == "GET" and p == "/v2/account"]
    assert len(gets) >= sched.runs and all(m in ("GET", "POST") for m, _, _ in r.sim.requests)
    inv = Decimal(pv.invalidation_price) - Decimal("0.50")
    r.x.price_fn = lambda sym: (inv, "TEST", r.clock.now.isoformat(timespec="seconds"))
    r.clock.advance(30)
    r.x.run_cycle(r.clock.now, "TEST")
    r.clock.advance(30)
    r.x.run_cycle(r.clock.now, "TEST")
    assert [json.loads(b)["side"] for b in r.sim.posts] == ["buy", "sell"] and r.x.store.positions(open_only=False)[0].state == "CLOSED"


def test_E02_new_entries_block_while_the_refresh_is_unhealthy_but_exits_continue(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.ref.refresh(trigger="TEST")
    pv = go_ready(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    r.clock.advance(30)
    r.x.run_cycle(r.clock.now, "TEST")
    r.clock.advance(30)
    r.x.run_cycle(r.clock.now, "TEST")
    pos = r.x.store.open_position("SNDK")
    assert pos is not None and len(r.sim.posts) == 1
    go_ready(r, "NVDA")
    for _ in range(3):
        r.clock.advance(20)
        fail_next(r)
        r.ref.refresh(trigger="TEST")
    assert r.ref.freshness()["status"] == "STALE"
    r.clock.advance(30)
    out = r.x.run_cycle(r.clock.now, "TEST")
    assert "SAFETY_GATE" in out["summary"]["entries"]["blocked"]["NVDA"] and len(r.sim.posts) == 1                  # no new entry
    inv = Decimal(pv.invalidation_price) - Decimal("0.50")
    r.x.price_fn = lambda sym: (inv, "TEST", r.clock.now.isoformat(timespec="seconds"))
    r.clock.advance(30)
    out = r.x.run_cycle(r.clock.now, "TEST")
    assert out["summary"]["exits_submitted"] == 1 and json.loads(r.sim.posts[-1])["side"] == "sell"                # the deterministic exit still goes out
    r.clock.advance(30)
    r.x.run_cycle(r.clock.now, "TEST")
    assert r.x.store.position(pos.position_id).state == "CLOSED"


# ================================================================ L loop, lease, overlap, startup, restart ================================

def test_L01_no_overlapping_refresh_and_the_lease_admits_one_owner(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    assert r.ref._lock.acquire(blocking=False)
    try:
        assert r.ref.refresh(trigger="TEST")["result"] == "SKIPPED_RUNNING" and r.sim.requests == [] and r.ref.store.attempt_count() == 0
    finally:
        r.ref._lock.release()

    class HeldLease:
        def acquire(self, owner, now, ttl_min, name):
            assert name == "alpaca_account_refresher" and ttl_min == 2
            return False

        def release(self, owner, name):
            pass
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300, lease_store_fn=HeldLease)
    assert sched.step(r.clock.now)["result"] == "LEASE_HELD" and r.sim.requests == []
    from forward.automation import default_store
    lease = default_store()
    assert lease.acquire("someone-else", r.clock.now, ttl_min=2, name=AR.LEASE_NAME)
    sched2 = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300, lease_store_fn=default_store)
    assert sched2.step(r.clock.now)["result"] == "LEASE_HELD"
    lease.release("someone-else", name=AR.LEASE_NAME)
    sched2.next_at = None
    assert sched2.step(r.clock.now)["result"] == "SUCCESS"
    assert sched2._lock.acquire(blocking=False)
    try:
        sched2.next_at = None
        assert sched2.step(r.clock.now)["result"] == "RUN_IN_PROGRESS"
    finally:
        sched2._lock.release()


def test_L02_cadence_60s_in_session_300s_outside_and_the_first_step_is_the_startup_refresh(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(datetime(2026, 10, 21, 15, 0, tzinfo=UTC)))
    evals = []
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, after_refresh=lambda: evals.append(1))
    assert (sched.interval_s, sched.idle_interval_s) == (60.0, 300.0)
    out = sched.step(r.clock.now)
    assert out["trigger"] == "STARTUP" and r.ref.store.attempts()[0]["trigger"] == "STARTUP" and evals == [1]
    assert sched.next_at == r.clock.now + timedelta(seconds=60) and sched.step(r.clock.now) is None
    r.clock.advance(60)
    assert sched.step(r.clock.now)["trigger"] == "SCHEDULED"
    night = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)
    assert sched.interval(night) == 300.0 and sched.interval(r.clock.now) == 60.0
    assert sched.status()["lease"] == "alpaca_account_refresher"
    assert config.ACCOUNT_REFRESH_STALE_SECONDS < config.EXECUTION_SAFETY_ACCOUNT_MAX_AGE_SECONDS and config.ACCOUNT_REFRESH_IDLE_SECONDS < config.ACCOUNT_REFRESH_STALE_SECONDS


def test_L03_the_loop_never_starts_in_a_test_process_disabled_or_unconfigured(monkeypatch):
    assert start_if_enabled() is False                                                     # pytest sets PYTEST_CURRENT_TEST
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("STOCK_AGENT_ENV", "browser_test")
    assert start_if_enabled() is False
    monkeypatch.setenv("STOCK_AGENT_ENV", "")
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_ENABLED", False)
    assert start_if_enabled() is False
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_ENABLED", True)

    class Off:
        def enabled(self):
            return False
    import account_refresh.scheduler as SCH
    monkeypatch.setattr(SCH, "_default_service", lambda: Off())
    assert start_if_enabled() is False


def test_L04_restart_reads_starting_until_the_startup_refresh_and_no_order_follows(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.ref.refresh(trigger="TEST")
    assert gate(r).state == "TRADING_ALLOWED"
    r2 = make_rig(tmp_path, monkeypatch, clock=r.clock, sim=r.sim)                          # a process restart on the same database
    assert r2.ref.store.snapshot() is not None and r2.ref.freshness()["status"] == "STARTING"
    assert gate(r2).state == "NO_NEW_ENTRIES"                                                # a cached snapshot never makes entries eligible on startup
    sched = AccountRefreshScheduler(lambda: r2.ref, clock=lambda: r2.clock.now, interval_s=60, idle_interval_s=300, after_refresh=lambda: r2.s.safety.evaluate("SCHEDULED", r2.clock.now))
    r2.clock.advance(5)
    assert sched.step(r2.clock.now)["trigger"] == "STARTUP" and r2.ref.freshness()["status"] == "FRESH" and gate(r2).state == "TRADING_ALLOWED"
    assert r2.x.store.intents() == [] and r2.sim.posts == []


# ================================================================ N boundaries =========================================================

def test_N01_refreshing_creates_no_intent_order_position_or_order_event(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    for _ in range(25):
        r.ref.refresh(trigger="TEST")
        r.clock.advance(60)
    counts = r.x.store.counts()
    assert (counts["execution_intents"], counts["execution_orders"], counts["execution_order_events"], counts["paper_positions"], counts["position_events"]) == (0, 0, 0, 0, 0)
    assert r.sim.posts == [] and {m for m, _, _ in r.sim.requests} == {"GET"} and {p for _, p, _ in r.sim.requests} == {"/v2/account", "/v2/positions", "/v2/orders"}
    assert r.x.mode()["mode"] == "MONITOR_ONLY" and r.x.mode()["persisted"] is False and r.s.setups.store.open_episodes() == []


def test_N02_the_refresher_is_paper_only_read_only_and_imports_no_robinhood_model_or_writer():
    import ast
    text = "".join((ROOT / f).read_text(encoding="utf-8") for f in SRC)
    for m in re.finditer(r"api\.alpaca\.markets", text):
        assert text[max(0, m.start() - 6):m.start()] == "paper-"
    assert not re.search(r"post_order|\.submit\(|/v2/orders/|cancel_order|\.cancel\(|TradingClient|requests\.(get|post)|urlopen|base_url|url_override|LIVE|robinhood|robin_stocks|anthropic|messages\.create",
                         re.sub(r"(?s)\"\"\".*?\"\"\"", "", text))
    for rel in SRC:
        for n in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8"))):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else ([n.module or ""] if isinstance(n, ast.ImportFrom) else [])
            for name in names:
                assert not any(name == p or name.startswith(p + ".") for p in ("paper.alpaca_order_writer", "paper.alpaca_orders", "robin_stocks", "anthropic", "agents", "ai_explain",
                                                                                "requests", "urllib", "subprocess", "alpaca")), (rel, name)
    assert set(AR.READ_PATHS) == {"/v2/account", "/v2/positions", "/v2/orders"} and BK.PAPER_HOST == "paper-api.alpaca.markets"


def test_N03_secrets_never_reach_the_database_logs_or_api(tmp_path, monkeypatch, caplog):
    r = make_rig(tmp_path, monkeypatch, real_creds=True)
    caplog.set_level("DEBUG")
    assert r.ref.source() == "ALPACA_PAPER" and r.ref.refresh(trigger="TEST")["result"] == "SUCCESS"
    fail_next(r, 1, "timeout")
    r.ref.refresh(trigger="TEST")
    sent = [h for h in r.sim.requests]
    assert sent
    blob = "".join(open(r.db, "rb").read().decode("latin-1") for _ in [0]) + caplog.text
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    from api.server import app
    api = TestClient(app)
    blob += json.dumps(api.get("/api/account-refresh").json()) + json.dumps(api.get("/api/account-refresh/history").json()) + json.dumps(api.get("/api/execution/status").json())
    for v in (FAKE_KEY, FAKE_SECRET, "APCA-API-KEY-ID", "APCA-API-SECRET-KEY", "Authorization"):
        assert v not in blob, v


def test_N04_zero_robinhood_and_zero_model_calls_while_refreshing(tmp_path, monkeypatch):
    calls = []
    import anthropic
    import api.routes.portfolio as PRT
    import portfolio.provider as PP
    monkeypatch.setattr(anthropic.resources.messages.Messages, "create", lambda *a, **k: calls.append("model") or (_ for _ in ()).throw(AssertionError("model")))
    monkeypatch.setattr(PRT, "provider_factory", lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(AssertionError("robinhood")))
    for name in ("get_provider", "provider_factory", "RobinhoodProvider"):
        if hasattr(PP, name):
            monkeypatch.setattr(PP, name, lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(AssertionError("robinhood")))
    r = make_rig(tmp_path, monkeypatch)
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300, after_refresh=lambda: r.s.safety.evaluate("SCHEDULED", r.clock.now))
    for _ in range(5):
        sched.step(r.clock.now)
        r.clock.advance(60)
    fail_next(r)
    sched.step(r.clock.now)
    gate(r)
    assert calls == []


# ================================================================ A API ==================================================================

def test_A01_read_only_api(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    from api.server import app
    api = TestClient(app)
    j = api.get("/api/account-refresh").json()
    assert j["status"] == "UNAVAILABLE" and j["fresh"] is False and j["snapshot"] is None
    r.ref.refresh(trigger="TEST")
    j = api.get("/api/account-refresh").json()
    for k in ("status", "fresh", "age_seconds", "last_success_at", "last_attempt_at", "last_error", "attempt_count", "consecutive_failures", "loop", "snapshot", "disclaimer"):
        assert k in j, k
    assert j["status"] == "FRESH" and j["snapshot"]["equity"] == "100000.00" and j["snapshot"]["positions"] == 0 and j["snapshot"]["account_number_masked"] == "****0060"
    h = api.get("/api/account-refresh/history?limit=5").json()
    assert h["total"] == 1 and h["attempts"][0]["result"] == "SUCCESS"
    st = api.get("/api/execution/status").json()
    assert st["account_data"]["status"] == "FRESH" and st["account_data"]["label"].startswith("Account data: Fresh")
    paths = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/account-refresh")}
    assert paths == {"/api/account-refresh": {"get"}, "/api/account-refresh/history": {"get"}}
    for p in paths:
        assert not re.search(r"alpaca|paper|broker|order|trade|submit|execute", p), p


# ================================================================ U frontend =============================================================

def test_U01_node_tests_for_the_account_data_line():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_account.test.cjs")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout and "# pass 5" in r.stdout, r.stdout


# ================================================================ Z no strategy change ===================================================

def test_Z01_strategy_execution_and_prior_stage_files_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    pinned = [p.relative_to(ROOT).as_posix() for d in ("ppe", "demand", "evidence", "setups", "proposals", "safety", "paper") for p in (ROOT / d).glob("*.py")] + \
             ["execution/engine.py", "execution/broker.py", "execution/store.py", "execution/scheduler.py", "execution/simulated.py", "execution/__init__.py",
              "database/execution_migrations.py", "api/routes/execution.py", "api/routes/execution_safety.py", "api/routes/proposals.py", "api/routes/setups.py",
              "trader_view/models.py", "trader_view/__init__.py", "frontend/risk_controls.js", "frontend/trader_view.js", "frontend/trader_render.js", "frontend/trade_review.js",
              "scripts/run_aapl_paper_smoke.py", "scripts/start_stock_agent.ps1", "scripts/stop_stock_agent.ps1", "tests/conftest.py", "tests/test_execution_60.py",
              "tests/test_aapl_smoke_60.py", "tests/test_execution_safety_591.py", "tests/test_proposals_59.py", "tests/test_setups_58.py"]
    # >>> Stage 6.0.2: the safety files and the Stage 6.0 pin test carry marked, additive Stage 6.0.2 blocks (DESIGN_602 section 6); everything outside them stays byte-identical
    import re as _re602
    for rel in [p for p in pinned if p.startswith("safety/") or p == "tests/test_execution_60.py"]:
        now602 = _re602.sub(r"(?ms)^[ \t]*# >>> Stage 6\.0\.2.*?^[ \t]*# <<< Stage 6\.0\.2[^\n]*\n", "", (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n"))
        assert now602.encode("utf-8") == blob(rel), f"{rel} differs from {BASE} outside the Stage 6.0.2 blocks"
    pinned = [p for p in pinned if not (p.startswith("safety/") or p == "tests/test_execution_60.py")]
    # <<< Stage 6.0.2
    for rel in pinned:
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob(rel), f"{rel} differs from {BASE}"
    svc_now = (ROOT / "execution" / "service.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    svc_base = blob("execution/service.py").decode("utf-8")
    start = svc_now.index("        acct: Dict[str, Any] = {}\n")
    end = svc_now.index("        return BotStatus(", start)
    reverted = svc_now[:start] + svc_now[end:]
    reverted = reverted.replace(",\n                         account_data=acct)", ")")
    assert reverted == svc_base                                                         # the executor changed only by a display field
    base_cfg = blob("config.py").decode("utf-8")
    assert (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n").startswith(base_cfg.rstrip("\n")) and config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False
    import sqlite3 as _s
    conn = _s.connect(":memory:")
    run_account_refresh_migrations(conn)
    run_account_refresh_migrations(conn)
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= set(TABLES)
