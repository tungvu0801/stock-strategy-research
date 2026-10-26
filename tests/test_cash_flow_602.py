"""
Stage 6.0.2 - CASH-FLOW-ADJUSTED DAILY P&L. Focused tests against the REAL wiring: the Trader View stream builds the default Stage 5.9.1 gate
on top of the Stage 6.0.1 refresher and the Stage 6.0.2 cash-flow ledger, with the deterministic simulated paper broker and its simulated
activity feed below the real paper clients. No socket is opened; no real order exists.

  T01-T26  the 26 cases the stage requires (numbered as in the spec)
  X        Examples A / B, the window rules, classification table, transport refusals, migrations, liveness, API, secrets, runner / health check
  M01      multi-hour simulated validation (no funding, deposit mid-session, withdrawal mid-session, a real halt)
  Z        no strategy / execution change: prior-stage files identical to the base outside the marked Stage 6.0.2 blocks
"""
import ast
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import BaseAdapter

ROOT = Path(__file__).resolve().parents[1]
from freeze_ledger import view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database.database as dbm  # noqa: E402
import cash_flow as CF  # noqa: E402
import safety as SF  # noqa: E402
from cash_flow import rules as RU  # noqa: E402
from cash_flow.reader import ActivityReader, CashFlowRefused, _ActivityTransport  # noqa: E402
from cash_flow.service import CashFlowService, make_cash_flow_account_fn  # noqa: E402
from cash_flow.simulated import ActivityFeed, deposit, feed_for, move_cash, withdraw  # noqa: E402
from cash_flow.store import CashFlowStore  # noqa: E402
from database.cash_flow_migrations import TABLES, run_cash_flow_migrations  # noqa: E402
from account_refresh.scheduler import AccountRefreshScheduler  # noqa: E402
from account_refresh.service import AccountRefreshService  # noqa: E402
from account_refresh.store import AccountRefreshStore  # noqa: E402
from execution import broker as BK  # noqa: E402
from execution.service import ExecutionService  # noqa: E402
from execution.simulated import SimulatedPaperBroker  # noqa: E402
from execution.store import ExecutionStore  # noqa: E402
from safety.engine import daily_loss  # noqa: E402
from safety.models import AccountEquity, Baseline  # noqa: E402
from safety.store import SafetyStore  # noqa: E402
from setups.service import SetupService  # noqa: E402
from setups.store import SetupStore  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_trader_view_55 import T0, WL, FakePositions, FakeResearch  # noqa: E402
from test_setups_58 import READY_IN, WATCH_IN, FakeClock, fx  # noqa: E402

BASE = "47e79a7"                                                     # main = the Stage 6.0.1 merge (PR #14, incl. the Stage 6.0 / 6.0.1 CI test fixes): the base this stage is finalized on
UTC = timezone.utc
OPEN = datetime(2026, 10, 21, 13, 31, tzinfo=UTC)                     # Wednesday 09:31 New York
FAKE_KEY, FAKE_SECRET = "CASHFLOWTEST-paperkey-9f8e7d6c", "CASHFLOWTEST-papersecret-5b4a3928"
SRC = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "cash_flow").glob("*.py")) + ["api/routes/cash_flow_status.py", "database/cash_flow_migrations.py"]
Q = Decimal("0.01")


class RealishMarket(SimulatedMarket):
    mode = "real"                                                     # the real-data account path (Alpaca snapshot) on simulated quotes


class Wire602(BaseAdapter):
    """Below the real paper clients: scripted failures for GET paths, the activity feed for /v2/account/activities, else the simulated broker."""

    def __init__(self, sim):
        super().__init__()
        self.sim, self.script = sim, []

    def send(self, request, **kw):
        path = urlsplit(request.url).path
        for i, item in enumerate(self.script):
            if item[0] == path:
                self.script.pop(i)
                if item[1] == "timeout":
                    raise requests.exceptions.ReadTimeout("scripted timeout")
                raise requests.exceptions.ConnectionError("scripted reset")
        if path == CF.ACTIVITIES_PATH:
            return feed_for(self).answer(request)
        return self.sim.send(request, **kw)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def cash_config(monkeypatch):
    monkeypatch.setattr(config, "TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", False)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", False)
    monkeypatch.setattr(config, "EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", False)
    monkeypatch.setattr(config, "EXECUTION_MAX_SHARES_PER_ENTRY", 0)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_STALE_SECONDS", 600.0)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_MAX_FAILURES", 3)
    monkeypatch.setattr(config, "ACCOUNT_REFRESH_READ_ATTEMPTS", 3)
    for k, v in (("CASH_FLOW_ENABLED", True), ("CASH_FLOW_STALE_SECONDS", 600.0), ("CASH_FLOW_MAX_FAILURES", 3), ("CASH_FLOW_READ_ATTEMPTS", 3),
                 ("CASH_FLOW_BACKOFF_SECONDS", "2,5"), ("CASH_FLOW_MARGIN_SECONDS", 60.0), ("CASH_FLOW_LOOKBACK_DAYS", 7), ("CASH_FLOW_BOOTSTRAP_DAYS", 30),
                 ("CASH_FLOW_PAGE_SIZE", 100), ("CASH_FLOW_MAX_PAGES", 20)):
        monkeypatch.setattr(config, k, v)
    hits = []

    def tripwire():
        hits.append("wire")
        raise AssertionError("the real paper network adapter was requested")
    monkeypatch.setattr(BK, "WIRE", tripwire)
    yield
    assert not hits


class Rig:
    pass


def make_rig(tmp_path, monkeypatch, *, clock=None, sim=None, real_creds=False, real_mode=True, db_name="stage602.db", **cash_kw):
    """The stream's DEFAULT gate wiring (refresher + cash-flow ledger) on a temporary database, one simulated paper account and its activity feed."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / db_name
    monkeypatch.setattr(dbm, "_db_instance", dbm.ResearchDatabase(db))
    r = Rig()
    r.clock = clock or FakeClock()
    r.sim = sim or SimulatedPaperBroker(now_fn=r.clock)
    r.wire = Wire602(r.sim)
    r.feed = feed_for(r.wire)
    r.feed.now_fn = r.clock
    r.sleeps = []
    creds = lambda: (FAKE_KEY, FAKE_SECRET)  # noqa: E731
    mk = (lambda: BK.AlpacaPaperBroker(credentials_fn=creds, wire=lambda: r.wire)) if real_creds else (lambda: BK.AlpacaPaperBroker(simulated=True, wire=lambda: r.wire))
    r.ref = AccountRefreshService(AccountRefreshStore(db), broker=mk(), clock=r.clock, sleep=r.sleeps.append)
    r.cf = CashFlowService(CashFlowStore(db), broker=mk(), clock=r.clock, sleep=r.sleeps.append, snapshot_at_fn=lambda: (r.ref.store.snapshot() or {}).get("refreshed_at"),
                           baseline_fn=lambda d: SafetyStore(db).baseline(d), **cash_kw)
    holder = {}
    r.setups = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(db), clock=r.clock, cooldown_minutes=15)
    r.x = ExecutionService(stream_fn=lambda: holder["s"], store=ExecutionStore(db), broker=mk(), clock=r.clock)
    market = RealishMarket(seed=11, start_time=T0) if real_mode else SimulatedMarket(seed=11, start_time=T0)
    r.s = TerminalStream(market, FakePositions(), FakeResearch(), watchlist_fn=lambda: list(WL), tick_seconds=1.0, allow_manual_tick=True, run_loop=False,
                         now_fn=lambda: T0, setup_service=r.setups, execution_service=r.x, account_refresh_service=r.ref, cash_flow_service=r.cf)
    holder["s"] = r.s
    r.s.proposals.clock = r.clock
    r.s.safety.clock = r.clock
    r.db, r.mk, r.holder = db, mk, holder
    return r


def gate(r):
    return r.s.safety.evaluate("TEST", r.clock.now)


def cycle(r, advance=0):
    """One refresh cycle as the Stage 6.0.1 loop runs it: account snapshot -> cash-flow read -> gate."""
    if advance:
        r.clock.advance(advance)
    r.ref.refresh(trigger="TEST")
    r.cf.refresh(trigger="TEST")
    return gate(r)


def equity(r, value):
    r.sim.account["equity"] = format(Decimal(str(value)).quantize(Q), "f")


def fund(r, amount, kind=None):
    a = Decimal(str(amount))
    return deposit(r.sim, a, feed=r.feed, kind=kind or "CSD") if a > 0 else withdraw(r.sim, -a, feed=r.feed, kind=kind or "CSW")


def started(r):
    v = cycle(r)
    assert v.state == "TRADING_ALLOWED" and v.daily_loss.adjustments == "CASH_FLOW_ADJUSTED" and v.daily_loss.starting_equity == "100000.00", (v.state, v.reason_codes)
    return v


def go_ready(r, symbol="SNDK"):
    r.setups.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **WATCH_IN))
    r.clock.advance(60)
    r.setups.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **READY_IN))
    pv = r.s.proposals.current(symbol, r.clock.now)
    assert pv.effective_status == "REVIEWABLE", (pv.effective_status, pv.blockers)
    r.sim.set_price(symbol, float(pv.reference_price))
    return pv


def pct(adj_start, current):
    a, c = Decimal(str(adj_start)), Decimal(str(current))
    return format(max(Decimal(0), (a - c) / a * 100).quantize(Q, rounding=ROUND_HALF_UP), "f")


# ================================================================ T01-T26: the required cases ===========================================

def test_T01_no_cash_flows_trading_allowed_on_the_real_data_path_with_net_zero(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    v = started(r)
    assert (v.daily_loss.net_external_cash_flow, v.daily_loss.adjusted_starting_equity, v.daily_loss.daily_pnl, v.daily_loss.cash_flow_status) == ("0.00", "100000.00", "0.00", "ADJUSTED")
    equity(r, 99000)
    v = cycle(r, 60)
    assert v.state == "TRADING_ALLOWED" and (v.daily_loss.daily_pnl, v.daily_loss.daily_loss_pct) == ("-1000.00", "1.00")
    assert v.daily_loss.equity_source == "ALPACA_PAPER_VIEW" and config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False


def test_T02_deposit_adjustment_example_A(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(600)
    fund(r, 5000)
    equity(r, 104000)                                                                     # deposit 5000 and a 1000 trading loss
    v = cycle(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_PENDING"]          # not yet attributable: paused one cycle, never halted
    v = cycle(r, 60)
    d = v.daily_loss
    assert v.state == "TRADING_ALLOWED" and (d.net_external_cash_flow, d.adjusted_starting_equity, d.daily_pnl, d.daily_loss_pct) == ("5000.00", "105000.00", "-1000.00", "0.95")
    assert d.unadjusted_daily_pnl == "4000.00" and d.starting_equity == "100000.00"         # the deposit is NOT trading profit


def test_T03_withdrawal_adjustment_example_B(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(600)
    fund(r, -5000)
    equity(r, 96000)
    assert cycle(r).reason_codes == ["CASH_FLOW_PENDING"]
    d = cycle(r, 60).daily_loss
    assert (d.net_external_cash_flow, d.adjusted_starting_equity, d.daily_pnl, d.daily_loss_pct, d.status) == ("-5000.00", "95000.00", "1000.00", "0.00", "OK")


def test_T04_multiple_deposits(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, 1000)
    r.clock.advance(300)
    fund(r, 2000, kind="TRANS")
    r.clock.advance(300)
    v = cycle(r)
    assert v.state == "TRADING_ALLOWED" and (v.daily_loss.net_external_cash_flow, v.daily_loss.adjusted_starting_equity, v.daily_loss.daily_pnl) == ("3000.00", "103000.00", "0.00")


def test_T05_multiple_withdrawals(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, -1000)
    r.clock.advance(300)
    fund(r, -500, kind="TRANS")
    r.clock.advance(300)
    v = cycle(r)
    assert v.state == "TRADING_ALLOWED" and (v.daily_loss.net_external_cash_flow, v.daily_loss.adjusted_starting_equity, v.daily_loss.daily_pnl) == ("-1500.00", "98500.00", "0.00")


def test_T06_mixed_flows_journals_and_performance(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, 3000)
    fund(r, -1000)
    r.feed.add("JNLC", "500")
    r.feed.add("JNLC", "-200")
    r.feed.add("FEE", "-1.00", sub_type="CAT")
    r.feed.add("DIV", "2.00", symbol="AAPL")
    move_cash(r.sim, Decimal("500") - 200 - 1 + 2)                                         # fund() already moved the deposit and the withdrawal
    r.clock.advance(300)
    v = cycle(r)
    d = v.daily_loss
    assert (d.net_external_cash_flow, d.adjusted_starting_equity, d.daily_pnl) == ("2300.00", "102300.00", "1.00")         # (3000 + 500) - (1000 + 200); fee + dividend = trading P&L
    adj = r.cf.adjustment(r.clock.now, use_default_inputs=True)
    assert (adj["deposits"], adj["withdrawals"], adj["journal_in"], adj["journal_out"]) == ("3000", "1000", "500", "200")
    assert adj["counts"] == {"COUNTED": 4, "PERFORMANCE": 2}


def test_T07_dedupe_by_broker_id_and_revisions_never_mutate_history(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    row = fund(r, 2500)
    for _ in range(4):
        out = r.cf.refresh(trigger="TEST")
    assert out["new_events"] == 0 and r.cf.store.event_row_count() == 1                    # the same broker event is stored once
    assert cycle(r, 120).daily_loss.net_external_cash_flow == "2500.00"                      # and counted once
    r.feed.amend(row["id"], status="canceled")
    out = r.cf.refresh(trigger="TEST")
    assert out["revised_events"] == 1 and r.cf.store.event_row_count() == 2
    revs = sorted((e["revision"], e["broker_status"]) for e in r.cf.store.all_rows())
    assert revs == [(1, "executed"), (2, "canceled")]                                        # history kept; the latest revision decides
    v = cycle(r, 60)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_TIMING_AMBIGUOUS"]  # a corrected funding record is never guessed
    conn = sqlite3.connect(r.db)
    for sql in ("UPDATE cash_flow_events SET amount = '1'", "DELETE FROM cash_flow_events", "UPDATE cash_flow_refresh_attempts SET result = 'SUCCESS'",
                "DELETE FROM cash_flow_refresh_attempts", "DELETE FROM cash_flow_liveness"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()


def test_T08_bounded_pagination_complete_incomplete_repeated_and_filter_ignored(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, page_size=2, max_pages=3)
    for i in range(5):
        r.feed.add("FEE", "-0.01", created_at=r.clock.now - timedelta(hours=10 - i))
    out = r.cf.refresh(trigger="TEST")
    assert out["result"] == "SUCCESS" and out["pages"] == 3 and out["new_events"] == 5
    q = [x["query"] for x in r.feed.requests]
    assert "page_token" not in q[0] and all("page_token=" in x for x in q[1:]) and all("category=non_trade_activity" in x and "direction=desc" in x for x in q)
    r.feed.add("FEE", "-0.01", created_at=r.clock.now - timedelta(hours=1))                # 6 rows: three full pages, the limit, and maybe more
    out = r.cf.refresh(trigger="TEST")
    assert out["result"] == "INCOMPLETE" and out["pages"] == 3 and r.cf.freshness()["status"] == "AMBIGUOUS"
    v = cycle(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_CLASSIFICATION_UNCERTAIN"]          # never silently truncated
    r2 = make_rig(tmp_path / "rep", monkeypatch, page_size=2, max_pages=5)
    for i in range(4):
        r2.feed.add("FEE", "-0.01", created_at=r2.clock.now - timedelta(hours=5 - i))
    real = r2.feed.answer

    def same_page(request):                                                                 # a broker that ignores page_token
        return real(requests.Request("GET", re.sub(r"&?page_token=[^&]*", "", request.url)).prepare())
    r2.feed.answer = same_page
    out = r2.cf.refresh(trigger="TEST")
    assert out["result"] == "INVALID" and out["read_attempts"] == 1 and len(r2.feed.requests) == 2           # no pagination loop, no retry of an invalid read
    r3 = make_rig(tmp_path / "ign", monkeypatch)
    r3.feed.ignore_category = True
    r3.feed.fill_row("AAPL")
    r3.feed.add("FEE", "-0.01")
    out = r3.cf.refresh(trigger="TEST")
    assert out["skipped_trade_rows"] == 1 and out["new_events"] == 1 and [e["activity_type"] for e in r3.cf.store.all_rows()] == ["FEE"]


@pytest.mark.parametrize("kind", ["XYZ", "MISC", "JNLS", "ACATS"])
def test_T09_unknown_or_unvalued_type_is_uncertain_and_blocks_entries(tmp_path, monkeypatch, kind):
    r = make_rig(tmp_path, monkeypatch)
    r.feed.add(kind, "750.00", created_at=r.clock.now - timedelta(days=2), activity_id="20261019000000000::old")    # before the baseline: no effect
    started(r)
    r.clock.advance(600)
    r.feed.add(kind, "750.00", description="Transfer PA3XAMPLE0001 ref 1234567")
    v = cycle(r, 120)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_CLASSIFICATION_UNCERTAIN"] and v.allowed_new_entry is False
    ev = [e for e in r.cf.store.current_events() if e["broker_event_id"] != "20261019000000000::old"][0]
    assert ev["category"] == "UNCLASSIFIED" and ev["external"] is False and ev["note"] and ev["raw_reference"]["type"] == kind
    assert "PA3XAMPLE0001" not in json.dumps(ev) and "****" in ev["raw_reference"]["description"]                   # raw-safe audit metadata
    assert cycle(r, 3600).reason_codes == ["CASH_FLOW_CLASSIFICATION_UNCERTAIN"]                                     # until explicitly handled (next day)


def test_T10_stale_activity_data_pauses_new_entries(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(30)
    r.ref.refresh(trigger="TEST")                                                          # a newer snapshot than the last activity read
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_DATA_STALE"]
    assert r.cf.adjustment(r.clock.now, use_default_inputs=True)["reason"] == "the activity read is older than the account snapshot"
    r.cf.refresh(trigger="TEST")
    assert gate(r).state == "TRADING_ALLOWED"
    r.clock.advance(601)                                                                   # no activity read for > CASH_FLOW_STALE_SECONDS
    assert r.cf.freshness()["status"] == "STALE" and gate(r).reason_codes in (["CASH_FLOW_DATA_STALE"], ["ACCOUNT_SNAPSHOT_STALE"])
    r.ref.refresh(trigger="TEST")
    assert gate(r).reason_codes == ["CASH_FLOW_DATA_STALE"]
    assert cycle(r).state == "TRADING_ALLOWED"                                              # automatic recovery


def test_T11_activity_failure_keeps_events_records_attempts_and_fails_closed(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, 1000)
    cycle(r, 120)
    n_events = r.cf.store.event_row_count()
    r.feed.fail.extend([("connection",)] * 3)
    r.clock.advance(60)
    r.ref.refresh(trigger="TEST")
    out = r.cf.refresh(trigger="TEST")
    assert out == {"result": "FAILED", "trigger": "TEST", "error_code": "CONNECTION", "error_text": "ConnectionError", "read_attempts": 3}
    assert r.sleeps[-2:] == [2.0, 5.0] and r.cf.store.event_row_count() == n_events                # bounded retries; prior events kept
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_DATA_STALE"]
    att = r.cf.store.attempts(1)[0]
    assert (att["result"], att["error_code"], att["read_attempts"]) == ("FAILED", "CONNECTION", 3)
    r.feed.fail.append(("status", 404))
    out = r.cf.refresh(trigger="TEST")
    assert (out["result"], out["error_code"], out["read_attempts"]) == ("FAILED", "HTTP_404", 1)                   # not retried
    r.feed.fail.append(("body", b'{"not": "a list"}'))
    out = r.cf.refresh(trigger="TEST")
    assert (out["result"], out["error_code"], out["read_attempts"]) == ("INVALID", "INVALID_PAYLOAD", 1)         # never retried, never stored
    live = r.cf.store.liveness()
    assert live["consecutive_failures"] == 3 and r.cf.freshness()["status"] == "STALE" and live["event_count"] == 1 and live["failure_count"] == 3
    r.feed.fail.append(("body", b"<html>"))                                                 # unreadable once, then fine: one retry after 2 s
    n = len(r.sleeps)
    v = cycle(r, 60)
    assert v.state == "TRADING_ALLOWED" and r.sleeps[n:] == [2.0] and r.cf.store.liveness()["consecutive_failures"] == 0


def test_T12_restart_persists_events_and_reads_starting_until_the_first_read(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, 4000)
    v = cycle(r, 120)
    assert v.daily_loss.net_external_cash_flow == "4000.00"
    r2 = make_rig(tmp_path, monkeypatch, clock=r.clock, sim=r.sim)                         # a process restart on the same database
    r2.feed.rows = r.feed.rows                                                             # the same broker history
    assert r2.cf.store.event_row_count() == 1 and r2.cf.freshness()["status"] == "UNAVAILABLE"
    r2.ref.refresh(trigger="TEST")
    v2 = gate(r2)
    assert v2.state == "NO_NEW_ENTRIES" and v2.reason_codes == ["DAILY_PNL_ADJUSTMENT_UNAVAILABLE"]   # a stored ledger never makes entries eligible on startup
    out = r2.cf.refresh(trigger="STARTUP")
    assert out["new_events"] == 0 and r2.cf.store.event_row_count() == 1                 # restart re-read dedupes
    v2 = gate(r2)
    assert v2.state == "TRADING_ALLOWED" and v2.daily_loss.net_external_cash_flow == "4000.00" and v2.daily_loss.starting_equity == "100000.00"


def test_T13_late_arriving_event_is_persisted_and_recomputes_with_a_recorded_reevaluation(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(OPEN))
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300,
                                    after_refresh=lambda: r.s.safety.evaluate("SCHEDULED", r.clock.now), cash_flow_fn=lambda: r.cf)
    sched.step(r.clock.now)
    for _ in range(5):
        r.clock.advance(60)
        sched.step(r.clock.now)
    effective = r.clock.now - timedelta(seconds=30)
    move_cash(r.sim, 5000)                                                                 # the money arrived ...
    for _ in range(10):                                                                    # ... but the broker posts the record 10 minutes later
        r.clock.advance(60)
        sched.step(r.clock.now)
    hidden = r.s.safety.store.last_evaluation()
    assert hidden["daily_loss"]["daily_pnl"] == "5000.00" and hidden["state"] == "TRADING_ALLOWED"   # unseen deposit reads as a gain: never a false halt
    r.feed.add("CSD", "5000.00", created_at=effective)
    r.clock.advance(60)
    out = sched.step(r.clock.now)
    assert out["result"] == "SUCCESS" and sched.last_cash_flow["new_events"] == 1
    ev = r.cf.store.current_events()[0]
    assert ev["effective_at"] == RU.iso(effective) and ev["trading_date"] == "2026-10-21"   # placed by its own effective time
    last = r.s.safety.store.last_evaluation()
    assert last["trigger"] == "SCHEDULED" and last["daily_loss"]["daily_pnl"] == "0.00" and last["daily_loss"]["net_external_cash_flow"] == "5000.00"
    assert last["input"]["account"]["net_external_cash_flow"] == "5000.00" and r.cf.store.attempts(1)[0]["new_events"] == 1


def test_T14_day_boundary_new_baseline_new_window_controls_unaffected(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(datetime(2026, 10, 21, 19, 0, tzinfo=UTC)))     # 15:00 New York
    started(r)
    r.clock.advance(300)
    fund(r, 10000)
    assert cycle(r, 120).daily_loss.net_external_cash_flow == "10000.00"
    r.s.safety.emergency_stop("test")
    r.s.safety.pause("test")
    r.clock.now = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)                                # next New York date, 09:00
    v = cycle(r)
    d = v.daily_loss
    assert d.trading_date == "2026-10-22" and d.starting_equity == "110000.00" and d.net_external_cash_flow == "0.00"   # yesterday's deposit is inside the new baseline
    assert v.state == "HALTED" and "KILL_SWITCH_ACTIVE" in v.reason_codes and v.no_new_entries.active                    # kill switch / pause unaffected
    r.s.safety.clear_emergency_stop("test")
    r.s.safety.resume("test")
    assert gate(r).state == "TRADING_ALLOWED"
    ev = r.cf.store.current_events()[0]
    assert ev["trading_date"] == "2026-10-21"


def test_T15_deposit_after_a_halt_does_not_clear_the_latch(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    equity(r, 94000)                                                                       # a 6% trading loss
    v = cycle(r, 60)
    assert v.state == "HALTED" and v.daily_loss.daily_loss_pct == "6.00" and v.daily_loss.latched
    equity(r, 96000)
    r.clock.advance(60)
    fund(r, 10000)                                                                         # equity 106000; adjusted start 110000: loss 3.64% < 5%
    assert cycle(r).state == "HALTED"                                                      # pending deposit: still latched
    v = cycle(r, 60)
    assert v.state == "HALTED" and v.reason_codes == ["DAILY_LOSS_LIMIT_REACHED"] and v.daily_loss.daily_loss_pct == "3.64" and v.daily_loss.latched
    assert r.s.safety.store.state().daily_halt_active is True


def test_T16_withdrawal_never_fakes_a_loss_or_a_halt(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, -10000)                                                                        # equity 90000: a 10% "loss" unadjusted (threshold 5%)
    states = [cycle(r).state, cycle(r, 60).state, cycle(r, 60).state]
    assert states == ["NO_NEW_ENTRIES", "TRADING_ALLOWED", "TRADING_ALLOWED"] and "HALTED" not in states
    d = gate(r).daily_loss
    assert (d.daily_pnl, d.daily_loss_pct, d.unadjusted_daily_pnl, d.adjusted_starting_equity) == ("0.00", "0.00", "-10000.00", "90000.00")
    assert r.s.safety.store.state().daily_halt_active is False and r.s.safety.store.event_count("DAILY_HALT_TRIGGERED") == 0


@pytest.mark.parametrize("kind,amount,cat", [("FEE", "-0.01", "FEE"), ("CFEE", "-1.50", "FEE"), ("PTC", "-0.02", "FEE"), ("PTR", "0.02", "FEE")])
def test_T17_fees_are_trading_pnl_not_funding(tmp_path, monkeypatch, kind, amount, cat):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    r.feed.add(kind, amount, sub_type="CAT" if kind == "FEE" else None)
    move_cash(r.sim, Decimal(amount))
    v = cycle(r, 60)
    ev = r.cf.store.current_events()[0]
    assert (ev["category"], ev["external"]) == (cat, False) and v.daily_loss.net_external_cash_flow == "0.00" and v.daily_loss.daily_pnl == format(Decimal(amount).quantize(Q), "f")
    assert v.state == "TRADING_ALLOWED"


@pytest.mark.parametrize("kind", ["DIV", "DIVCGL", "DIVNRA", "DIVROC", "DIVTW"])
def test_T18_dividends_are_trading_pnl_not_funding(tmp_path, monkeypatch, kind):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    r.feed.add(kind, "42.10", symbol="AAPL")
    move_cash(r.sim, Decimal("42.10"))
    v = cycle(r, 60)
    assert r.cf.store.current_events()[0]["category"] == "DIVIDEND" and v.daily_loss.net_external_cash_flow == "0.00" and v.daily_loss.daily_pnl == "42.10"


@pytest.mark.parametrize("kind", ["INT", "INTNRA", "INTTW"])
def test_T19_interest_is_trading_pnl_not_funding(tmp_path, monkeypatch, kind):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    r.feed.add(kind, "3.21")
    move_cash(r.sim, Decimal("3.21"))
    v = cycle(r, 60)
    assert r.cf.store.current_events()[0]["category"] == "INTEREST" and v.daily_loss.net_external_cash_flow == "0.00" and v.daily_loss.daily_pnl == "3.21"


def test_T20_invalid_adjusted_baseline_is_unavailable_and_blocks(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    v = r.s.safety.evaluate("TEST", r.clock.now, overrides={"cash_flow_status": "ADJUSTED", "net_external_cash_flow": "-100000.00", "equity": "250.00"})
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["ADJUSTED_BASELINE_INVALID"] and v.daily_loss.adjusted_starting_equity == "0.00"
    acct = AccountEquity(equity="100.00", source="ALPACA_PAPER_VIEW", as_of="2026-10-21T15:00:00+00:00", age_s=1.0, cash_flow_status="ADJUSTED", net_external_cash_flow="-150000")
    b = Baseline(trading_date="2026-10-21", starting_equity="100000.00", baseline_created_at="2026-10-21T13:30:00+00:00", source="ALPACA_PAPER_VIEW", adjustments="UNAVAILABLE")
    d = daily_loss(b, acct, 5, max_age_s=900, accept_unadjusted=False, latched=False, latched_trading_date=None, triggered_at=None, triggered_loss_pct=None, today="2026-10-21")
    assert (d.status, d.code, d.adjusted_starting_equity, d.daily_pnl) == ("UNAVAILABLE", "ADJUSTED_BASELINE_INVALID", "-50000.00", None)
    assert RU.adjusted(Decimal("100000"), Decimal("-100000"), Decimal("1"))["adjusted_daily_loss_pct"] is None


def test_T21_normal_real_data_paper_auto_trades_with_the_unadjusted_flag_off(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(OPEN))
    assert config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False and r.s.safety.accept_unadjusted is False
    v = cycle(r)
    assert v.state == "TRADING_ALLOWED" and v.allowed_new_entry and v.daily_loss.adjustments == "CASH_FLOW_ADJUSTED"
    assert "DAILY_PNL_ADJUSTMENT_UNAVAILABLE" not in v.reason_codes
    pv = go_ready(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    for _ in range(3):
        cycle(r, 30)
        r.x.run_cycle(r.clock.now, "TEST")
    assert [json.loads(b)["side"] for b in r.sim.posts] == ["buy"] and r.x.store.open_position("SNDK") is not None
    assert pv.equity_source == "ALPACA_PAPER_VIEW"


def test_T22_the_daily_loss_halt_fires_from_adjusted_pnl(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    r.clock.advance(300)
    fund(r, 5000)
    equity(r, 99500)                                                                       # deposit 5000 hides a 5500 trading loss: unadjusted -0.5%
    cycle(r)
    v = cycle(r, 60)
    assert v.state == "HALTED" and v.reason_codes == ["DAILY_LOSS_LIMIT_REACHED"] and v.daily_loss.daily_loss_pct == pct(105000, 99500) == "5.24"
    assert v.daily_loss.unadjusted_daily_pnl == "-500.00" and r.s.safety.store.event_count("DAILY_HALT_TRIGGERED") == 1
    r2 = make_rig(tmp_path / "plain", monkeypatch)
    started(r2)
    equity(r2, 95000)
    assert cycle(r2, 60).state == "HALTED"                                                  # exactly at the threshold, no cash flow


def test_T23_exits_continue_while_cash_flow_is_unhealthy(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(OPEN))
    cycle(r)
    pv = go_ready(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    for _ in range(2):
        cycle(r, 30)
        r.x.run_cycle(r.clock.now, "TEST")
    pos = r.x.store.open_position("SNDK")
    assert pos is not None and len(r.sim.posts) == 1
    go_ready(r, "NVDA")
    r.feed.add("XYZ", "10.00")                                                             # an unknown activity: entries pause
    r.clock.advance(120)
    r.feed.fail.extend([("connection",)] * 3)                                              # and the activity feed fails
    v = cycle(r)
    assert v.state == "NO_NEW_ENTRIES"
    out = r.x.run_cycle(r.clock.now, "TEST")
    assert "SAFETY_GATE" in out["summary"]["entries"]["blocked"]["NVDA"] and len(r.sim.posts) == 1
    inv = Decimal(pv.invalidation_price) - Decimal("0.50")
    r.x.price_fn = lambda sym: (inv, "TEST", r.clock.now.isoformat(timespec="seconds"))
    r.clock.advance(30)
    out = r.x.run_cycle(r.clock.now, "TEST")
    assert out["summary"]["exits_submitted"] == 1 and json.loads(r.sim.posts[-1])["side"] == "sell"
    r.clock.advance(30)
    r.x.run_cycle(r.clock.now, "TEST")
    assert r.x.store.position(pos.position_id).state == "CLOSED"


def test_T24_cash_flow_refresh_alone_writes_nothing_to_the_broker(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(OPEN))
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300,
                                    after_refresh=lambda: r.s.safety.evaluate("SCHEDULED", r.clock.now), cash_flow_fn=lambda: r.cf)
    for i in range(30):
        if i == 10:
            fund(r, 2500)
        if i == 20:
            r.feed.fail.append(("status", 503))
        sched.step(r.clock.now)
        r.clock.advance(60)
    assert r.sim.posts == [] and r.x.store.intents() == [] and r.x.store.positions(open_only=False) == []
    conn = sqlite3.connect(r.db)
    have = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in ("execution_intents", "execution_orders", "execution_order_events", "paper_positions", "position_events"):
        assert table not in have or conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    conn.close()
    assert {m for m, _p, _q in r.sim.requests} == {"GET"} and {x["method"] for x in r.feed.requests} == {"GET"}
    assert {x["path"] for x in r.feed.requests} == {CF.ACTIVITIES_PATH}
    t = _ActivityTransport(r.wire, budget=1)
    for method, url, needle in (("POST", BK.BASE + CF.ACTIVITIES_PATH, "only reads"), ("GET", BK.BASE + "/v2/orders", "not the activities path"),
                                ("GET", "https://api.alpaca.markets" + CF.ACTIVITIES_PATH, "PAPER host"), ("DELETE", BK.BASE + CF.ACTIVITIES_PATH, "only reads"),
                                ("GET", "http://paper-api.alpaca.markets" + CF.ACTIVITIES_PATH, "PAPER host")):
        with pytest.raises(CashFlowRefused, match=needle):
            t.send(requests.Request(method, url).prepare())
    t.send(requests.Request("GET", BK.BASE + CF.ACTIVITIES_PATH).prepare())
    with pytest.raises(CashFlowRefused, match="budget"):
        t.send(requests.Request("GET", BK.BASE + CF.ACTIVITIES_PATH).prepare())


def _tripwires(monkeypatch, calls):
    import anthropic
    import api.routes.portfolio as PRT
    import portfolio.provider as PP
    monkeypatch.setattr(anthropic.resources.messages.Messages, "create", lambda *a, **k: calls.append("model") or (_ for _ in ()).throw(AssertionError("model")))
    monkeypatch.setattr(PRT, "provider_factory", lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(AssertionError("robinhood")))
    for name in ("get_provider", "provider_factory", "RobinhoodProvider"):
        if hasattr(PP, name):
            monkeypatch.setattr(PP, name, lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(AssertionError("robinhood")))


def _run_mixed(r):
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300,
                                    after_refresh=lambda: r.s.safety.evaluate("SCHEDULED", r.clock.now), cash_flow_fn=lambda: r.cf)
    for i in range(12):
        if i == 3:
            fund(r, 1000)
        if i == 6:
            r.feed.add("XYZ", "1")
        if i == 8:
            r.feed.fail.append(("timeout",))
        sched.step(r.clock.now)
        r.clock.advance(60)
    TestClient(__import__("api.server", fromlist=["app"]).app).get("/api/cash-flow-status")


def test_T25_robinhood_tripwire_zero_calls(tmp_path, monkeypatch):
    calls = []
    _tripwires(monkeypatch, calls)
    r = make_rig(tmp_path, monkeypatch)
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    _run_mixed(r)
    assert [c for c in calls if c == "robinhood"] == []
    text = "".join((ROOT / f).read_text(encoding="utf-8") for f in SRC)
    code = re.sub(r"#[^\n]*", "", re.sub(r"(?s)\"\"\".*?\"\"\"", "", text))                   # the docstrings say "no Robinhood"; the code never names it
    assert not re.search(r"(?i)robinhood|robin_stocks|rh_gateway", code) and "robin_stocks" not in text and "rh_gateway" not in text


def test_T26_model_tripwire_zero_calls(tmp_path, monkeypatch):
    calls = []
    _tripwires(monkeypatch, calls)
    r = make_rig(tmp_path, monkeypatch)
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    _run_mixed(r)
    assert calls == []
    text = "".join((ROOT / f).read_text(encoding="utf-8") for f in SRC)
    assert "anthropic" not in text and "messages.create" not in text


# ================================================================ X formula, rules, transport, persistence, API ========================

def test_X01_examples_A_and_B_exact_decimal():
    a = RU.adjusted(Decimal("100000"), Decimal("5000"), Decimal("104000"))
    assert a["adjusted_starting_equity"] == Decimal("105000") and a["adjusted_daily_pnl"] == Decimal("-1000") and a["adjusted_daily_loss_pct"] == Decimal("1000") / Decimal("105000") * 100
    b = RU.adjusted(Decimal("100000"), Decimal("-5000"), Decimal("96000"))
    assert b["adjusted_starting_equity"] == Decimal("95000") and b["adjusted_daily_pnl"] == Decimal("1000") and b["adjusted_daily_loss_pct"] == Decimal(0)
    base = Baseline(trading_date="2026-10-21", starting_equity="100000.00", baseline_created_at="2026-10-21T13:30:00+00:00", source="ALPACA_PAPER_VIEW", adjustments="UNAVAILABLE")
    for net, cur, want in (("5000", "104000", ("-1000.00", "0.95", "OK")), ("-5000", "96000", ("1000.00", "0.00", "OK")), ("0", "94999.99", ("-5000.01", "5.00", "TRIGGERED"))):
        acct = AccountEquity(equity=cur, source="ALPACA_PAPER_VIEW", as_of="2026-10-21T15:00:00+00:00", age_s=5.0, cash_flow_status="ADJUSTED", net_external_cash_flow=net)
        d = daily_loss(base, acct, 5, max_age_s=900, accept_unadjusted=False, latched=False, latched_trading_date=None, triggered_at=None, triggered_loss_pct=None, today="2026-10-21")
        assert (d.daily_pnl, d.daily_loss_pct, d.status) == want and d.adjustments == "CASH_FLOW_ADJUSTED"


def test_X02_engine_compatibility_no_status_is_stage_591_and_the_flag_is_only_a_fallback():
    base = Baseline(trading_date="2026-10-21", starting_equity="100000.00", baseline_created_at="2026-10-21T13:30:00+00:00", source="ALPACA_PAPER_VIEW", adjustments="UNAVAILABLE")
    kw = dict(max_age_s=900, latched=False, latched_trading_date=None, triggered_at=None, triggered_loss_pct=None, today="2026-10-21")
    legacy = AccountEquity(equity="99000", source="ALPACA_PAPER_VIEW", as_of="x", age_s=5.0)
    assert daily_loss(base, legacy, 5, accept_unadjusted=False, **kw).code == "DAILY_PNL_ADJUSTMENT_UNAVAILABLE"
    assert daily_loss(base, legacy, 5, accept_unadjusted=True, **kw).adjustments == "ACCEPTED_UNADJUSTED"
    for st, code in (("PENDING", "CASH_FLOW_PENDING"), ("AMBIGUOUS", "CASH_FLOW_TIMING_AMBIGUOUS"), ("UNCERTAIN", "CASH_FLOW_CLASSIFICATION_UNCERTAIN"),
                     ("STALE", "CASH_FLOW_DATA_STALE"), ("UNAVAILABLE", "DAILY_PNL_ADJUSTMENT_UNAVAILABLE"), ("SOMETHING_NEW", "DAILY_PNL_ADJUSTMENT_UNAVAILABLE")):
        acct = legacy.model_copy(update={"cash_flow_status": st, "equity": "90000"})                # a 10% unadjusted drop with unknown cash flow
        d = daily_loss(base, acct, 5, accept_unadjusted=False, **kw)
        assert (d.status, d.code, d.daily_pnl, d.unadjusted_daily_pnl) == ("UNAVAILABLE", code, None, "-10000.00"), st     # paused, never a halt from unknown flows
        assert daily_loss(base, acct, 5, accept_unadjusted=True, **kw).adjustments == "ACCEPTED_UNADJUSTED"                 # the documented dev fallback only
        latched = daily_loss(base, acct, 5, accept_unadjusted=False, **{**kw, "latched": True, "latched_trading_date": "2026-10-21"})
        assert latched.status == "TRIGGERED" and latched.note == SF.REASON_TEXT[code]
    ok = legacy.model_copy(update={"cash_flow_status": "ADJUSTED", "net_external_cash_flow": "0"})
    assert daily_loss(base, ok, 5, accept_unadjusted=True, **kw).adjustments == "CASH_FLOW_ADJUSTED"      # adjusted wins even with the flag on
    for text in [SF.REASON_TEXT[c] for c in SF.CASH_FLOW_REASONS.values()] + [SF.REASON_TEXT[SF.R_ADJUSTED_BASELINE_INVALID]]:
        assert not re.search(r"(?i)\b(buy|sell|recommend\w*|approved|you should)\b", text), text
    assert "CASH_FLOW_ADJUSTED" in SF.ADJUSTMENTS and config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False


def test_X03_window_rules_margin_and_date_only_records():
    cutoff, upper = datetime(2026, 10, 21, 13, 31, tzinfo=UTC), datetime(2026, 10, 21, 15, 0, tzinfo=UTC)

    def ev(at, cat="DEPOSIT", status="executed", date=None):
        return {"category": cat, "external": cat in CF.EXTERNAL, "amount": "100", "broker_status": status, "effective_at": RU.iso(at) if at else None, "business_date": date}
    P = lambda e: RU.place(e, cutoff=cutoff, upper=upper, margin_s=60)  # noqa: E731
    assert P(ev(cutoff - timedelta(seconds=61))) == "BEFORE_BASELINE"
    assert P(ev(cutoff - timedelta(seconds=59))) == "AMBIGUOUS" and P(ev(cutoff + timedelta(seconds=60))) == "AMBIGUOUS"
    assert P(ev(cutoff + timedelta(seconds=61))) == "COUNTED" and P(ev(upper - timedelta(seconds=60))) == "COUNTED"
    assert P(ev(upper - timedelta(seconds=59))) == "PENDING" and P(ev(upper + timedelta(seconds=5))) == "PENDING"
    assert P(ev(cutoff + timedelta(hours=1), status="canceled")) == "AMBIGUOUS"
    assert P(ev(None, date="2026-10-20")) == "BEFORE_BASELINE" and P(ev(None, date="2026-10-21")) == "AMBIGUOUS"
    assert P(ev(cutoff + timedelta(hours=1), cat="UNCLASSIFIED")) == "UNCERTAIN" and P(ev(cutoff + timedelta(hours=1), cat="FEE")) == "PERFORMANCE"
    res = RU.adjust([ev(cutoff + timedelta(hours=1)), ev(upper + timedelta(seconds=1))], cutoff=cutoff, upper=upper, margin_s=60)
    assert res["status"] == "PENDING" and res["net_external_cash_flow"] == "100"
    assert RU.ny_date(datetime(2026, 10, 22, 3, 59, tzinfo=UTC)).isoformat() == "2026-10-21"          # 23:59 New York belongs to the 21st


def test_X04_classification_table_is_explicit_and_never_guesses():
    C = RU.classify
    assert C("CSD", Decimal("5"), "USD")[:3] == ("DEPOSIT", "IN", True) and C("CSW", Decimal("-5"), "USD")[:3] == ("WITHDRAWAL", "OUT", True)
    assert C("JNLC", Decimal("5"), "USD")[:3] == ("JOURNAL_IN", "IN", True) and C("JNLC", Decimal("-5"), "USD")[:3] == ("JOURNAL_OUT", "OUT", True)
    assert C("TRANS", Decimal("-5"), None)[:3] == ("WITHDRAWAL", "OUT", True) and C("ACATC", Decimal("5"), "USD")[0] == "DEPOSIT"
    for bad in (("CSD", Decimal("-5"), "USD"), ("CSW", Decimal("5"), "USD"), ("CSD", None, "USD"), ("CSD", Decimal("5"), "EUR"), ("JNLS", Decimal("5"), "USD"),
                ("MISC", Decimal("5"), "USD"), ("ACATS", Decimal("0"), "USD"), ("NEWTYPE", Decimal("5"), "USD"), ("", Decimal("5"), "USD")):
        assert C(*bad)[0] == "UNCLASSIFIED" and C(*bad)[2] is False, bad
    for t, cat in (("FEE", "FEE"), ("DIV", "DIVIDEND"), ("INT", "INTEREST"), ("REORG", "OTHER"), ("SPLIT", "OTHER")):
        assert C(t, Decimal("-1"), "USD")[:3:2] == (cat, False)
    assert set(CF.EXTERNAL) == {"DEPOSIT", "WITHDRAWAL", "JOURNAL_IN", "JOURNAL_OUT"} and set(CF.CATEGORIES) >= set(CF.EXTERNAL) | {"FEE", "DIVIDEND", "INTEREST", "OTHER"}
    assert RU.normalize({"id": "x", "activity_type": "FILL"}) is None
    e = RU.normalize({"id": "20261007000000000::a", "activity_type": "FEE", "net_amount": "-0.01", "created_at": "2026-10-08T00:05:19.86713Z", "date": "2026-10-07",
                      "status": "executed", "description": "CAT fee for PA3ABCDEF1234 on 2026-10-07"})
    assert (e["category"], e["effective_at"], e["trading_date"], e["business_date"]) == ("FEE", "2026-10-08T00:05:19+00:00", "2026-10-07", "2026-10-07")
    assert "PA3ABCDEF1234" not in json.dumps(e) and e["fingerprint"] == RU.normalize({"id": "y", "activity_type": "FEE", "net_amount": "-0.01", "created_at": "2026-10-08T00:05:19.86713Z",
                                                                                      "date": "2026-10-07", "status": "executed"})["fingerprint"]


def test_X05_retry_policy_bounded_429_retry_after_respected_and_capped(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.feed.fail.extend([("status", 429, 12), ("status", 500)])
    out = r.cf.refresh(trigger="TEST")
    assert out["result"] == "SUCCESS" and out["read_attempts"] == 3 and r.sleeps == [12.0, 5.0]
    r.sleeps.clear()
    r.feed.fail.extend([("status", 429, 9999)] * 3)
    out = r.cf.refresh(trigger="TEST")
    assert out["result"] == "FAILED" and out["error_code"] == "RATE_LIMITED" and r.sleeps == [30.0, 30.0]          # capped; no infinite retry
    assert r.cf.store.attempts(1)[0]["error_code"] == "RATE_LIMITED" and r.cf.store.liveness()["failure_count"] == 1
    r.sleeps.clear()
    r.feed.fail.extend([("timeout",), ("connection",), ("timeout",)])
    assert r.cf.refresh(trigger="TEST")["error_code"] == "TIMEOUT" and len(r.sleeps) == 2


def test_X06_migrations_idempotent_and_liveness_fields(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    run_cash_flow_migrations(conn)
    run_cash_flow_migrations(conn)
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= set(TABLES)
    r = make_rig(tmp_path, monkeypatch)
    started(r)
    fund(r, 100)
    cycle(r, 120)
    live = r.cf.store.liveness()
    for k in ("last_attempt_at", "last_success_at", "attempt_count", "success_count", "failure_count", "event_count", "last_result", "last_error", "last_event_at", "updated_at"):
        assert k in live, k
    assert (live["attempt_count"], live["success_count"], live["event_count"], live["last_result"]) == (2, 2, 1, "SUCCESS")
    with pytest.raises(sqlite3.DatabaseError):
        c = sqlite3.connect(r.db)
        c.execute("INSERT INTO cash_flow_events (event_id, broker_event_id, revision, activity_type, category, external, direction, source, raw_reference, fingerprint, created_at) "
                  "SELECT 'new', broker_event_id, revision, activity_type, category, external, direction, source, raw_reference, fingerprint, created_at FROM cash_flow_events")


def test_X07_read_only_api(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    from api.server import app
    api = TestClient(app)
    j = api.get("/api/cash-flow-status").json()
    assert j["status"] == "UNAVAILABLE" and j["cash_flow_status"] == "UNAVAILABLE"
    started(r)
    r.clock.advance(300)
    fund(r, 5000)
    equity(r, 104000)
    cycle(r, 60)
    j = api.get("/api/cash-flow-status").json()
    for k in ("status", "last_refresh", "event_count_today", "net_external_cash_flow", "adjusted_starting_equity", "adjusted_daily_pnl", "adjusted_daily_loss_pct",
              "last_cash_flow_refresh_at", "cash_flow_age_seconds", "cash_flow_status", "events_since_baseline", "disclaimer"):
        assert k in j, k
    assert (j["status"], j["net_external_cash_flow"], j["adjusted_starting_equity"], j["adjusted_daily_pnl"], j["adjusted_daily_loss_pct"], j["event_count_today"]) == \
           ("FRESH", "5000", "105000.00", "-1000.00", "0.95", 1)
    assert j["events_since_baseline"][0]["effect"] == "COUNTED" and "broker_event_id" not in json.dumps(j) and "::" not in json.dumps(j)
    ev = api.get("/api/cash-flow-status/events").json()
    assert ev["total_rows"] == 1 and ev["events"][0]["category"] == "DEPOSIT" and "::" not in json.dumps(ev)
    at = api.get("/api/cash-flow-status/attempts?limit=5").json()
    assert at["total"] == 2 and at["attempts"][0]["result"] == "SUCCESS"
    st = api.get("/api/execution/status").json()["account_data"]["cash_flow"]
    assert st["label"].startswith("Cash-flow data: Fresh") and st["daily_pnl"] == "-1000.00" and st["adjustments"] == "CASH_FLOW_ADJUSTED"
    paths = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/cash-flow-status")}
    assert paths == {"/api/cash-flow-status": {"get"}, "/api/cash-flow-status/events": {"get"}, "/api/cash-flow-status/attempts": {"get"}}
    for p in paths:
        assert not re.search(r"alpaca|paper|broker|order|trade|submit|execute", p), p


def test_X08_secrets_and_identifiers_never_reach_the_database_logs_or_api(tmp_path, monkeypatch, caplog):
    r = make_rig(tmp_path, monkeypatch, real_creds=True)
    caplog.set_level("DEBUG")
    assert r.cf.source() == "ALPACA_PAPER"
    r.feed.add("JNLC", "100000.00", created_at=r.clock.now - timedelta(days=20), description="Cash journal from PA3ZZZZZZZZZ to PA6SIM0000060")
    started(r)
    r.feed.fail.append(("timeout",))
    r.cf.refresh(trigger="TEST")
    blob = open(r.db, "rb").read().decode("latin-1") + caplog.text
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    from api.server import app
    api = TestClient(app)
    blob += json.dumps(api.get("/api/cash-flow-status").json()) + json.dumps(api.get("/api/cash-flow-status/events").json()) + json.dumps(api.get("/api/cash-flow-status/attempts").json())
    for v in (FAKE_KEY, FAKE_SECRET, "APCA-API-KEY-ID", "APCA-API-SECRET-KEY", "Authorization", "PA6SIM0000060", "PA3ZZZZZZZZZ"):
        assert v not in blob, v


def test_X09_sources_are_paper_only_read_only_and_import_no_writer_robinhood_or_model():
    text = "".join((ROOT / f).read_text(encoding="utf-8") for f in SRC)
    for m in re.finditer(r"api\.alpaca\.markets", text):
        assert text[max(0, m.start() - 6):m.start()] == "paper-"
    code = re.sub(r"(?s)\"\"\".*?\"\"\"", "", text)
    assert not re.search(r"post_order|\.submit\(|cancel_order|\.cancel\(|TradingClient|requests\.(get|post|put|delete)\(|urlopen|base_url|url_override|LIVE|_post_once", code)
    for rel in SRC:
        for n in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8"))):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else ([n.module or ""] if isinstance(n, ast.ImportFrom) else [])
            for name in names:
                assert not any(name == p or name.startswith(p + ".") for p in ("paper.alpaca_order_writer", "paper.alpaca_orders", "robin_stocks", "anthropic", "agents",
                                                                                "ai_explain", "subprocess", "alpaca", "execution.engine", "ppe", "demand", "evidence")), (rel, name)
            if rel not in ("cash_flow/reader.py", "cash_flow/simulated.py"):
                assert not any(name == "requests" or name.startswith("requests.") or name.startswith("urllib") for name in names), (rel, name)
    assert CF.ACTIVITIES_PATH == "/v2/account/activities" and BK.PAPER_HOST == "paper-api.alpaca.markets"
    assert not BK._GET_PATH.match(CF.ACTIVITIES_PATH)                                       # the order client's allow-list is untouched


def test_X10_loop_runs_snapshot_then_cash_flow_then_gate_and_never_starts_in_tests(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(OPEN))
    order = []
    real_cf, real_eval = r.cf.refresh, r.s.safety.evaluate
    r.cf.refresh = lambda now=None, trigger="SCHEDULED": order.append(("cash_flow", trigger, len(r.ref.store.attempts()))) or real_cf(now, trigger)
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300,
                                    after_refresh=lambda: order.append(("gate", r.cf.store.attempt_count())) or real_eval("SCHEDULED", r.clock.now), cash_flow_fn=lambda: r.cf)
    sched.step(r.clock.now)
    assert order == [("cash_flow", "STARTUP", 1), ("gate", 1)] and sched.status()["cash_flow"]["result"] == "SUCCESS"
    r.cf.refresh = lambda now=None, trigger="SCHEDULED": (_ for _ in ()).throw(RuntimeError("boom"))
    r.clock.advance(60)
    assert sched.step(r.clock.now)["result"] == "SUCCESS" and sched.last_cash_flow == {"result": "ERROR", "error": "RuntimeError"}     # never crashes the loop
    import account_refresh.scheduler as SCH
    assert SCH.get_scheduler().cash_flow_fn is SCH._default_cash_flow and SCH.start_if_enabled() is False
    old = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now)
    assert old.cash_flow_fn is None and old.status()["cash_flow"] is None                    # the Stage 6.0.1 constructor is unchanged


def test_X11_disabled_or_unconfigured_cash_flow_equals_the_stage_601_gate(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "CASH_FLOW_ENABLED", False)
    r.ref.refresh(trigger="TEST")
    assert r.cf.refresh(trigger="TEST")["result"] == "NOT_CONFIGURED" and r.feed.requests == []
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["DAILY_PNL_ADJUSTMENT_UNAVAILABLE"]
    monkeypatch.setattr(dbm, "_db_instance", dbm.ResearchDatabase(tmp_path / "plain.db"))
    s = TerminalStream(RealishMarket(seed=1, start_time=T0), FakePositions(), FakeResearch(), watchlist_fn=lambda: [], run_loop=False, now_fn=lambda: T0)
    assert s.cash_flow.enabled() is False and s.cash_flow.freshness()["status"] == "UNAVAILABLE"
    acct = AccountEquity(equity="5", source="ALPACA_PAPER_VIEW")
    assert make_cash_flow_account_fn(lambda ts: acct, s.cash_flow, tmp_path / "plain.db")(0.0) is acct


def test_X12_health_check_runner_logs_and_harness_flow_are_wired():
    ps = (ROOT / "scripts" / "check_stock_agent.ps1").read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in ps)
    for needle in ('"/api/cash-flow-status"', '"Cash-flow data: Fresh ({0}s ago)"', '"Cash-flow data: Stale - {0}"', '"Cash-flow data: unavailable - {0}"',
                   'Write-Output "Cash-flow data: status unavailable"'):
        assert needle in ps, needle
    assert ps.index("Account data: Fresh") < ps.index("Cash-flow data: Fresh") and 'Write-Output ("Safety: {0}" -f $s.safety_label)' in ps
    runner = (ROOT / "scripts" / "run_stock_agent.py").read_text(encoding="utf-8")
    assert "runner: heartbeat - cash-flow data" in runner and "cash-flow ledger refreshed in the same cycle" in runner
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert names.index("cash_flow") == names.index("account_refresh") + 1 and names.index("cash_flow") < names.index("automation")
    app_server = (ROOT / "browser_tests" / "app_server.py").read_text(encoding="utf-8")
    assert '_tv_stream().cash_flow.refresh(trigger="STARTUP")' in app_server


def test_X13_node_tests_for_the_cash_flow_line():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_cashflow.test.cjs")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout and "# pass 5" in r.stdout, r.stdout


def test_X14_the_gate_sees_one_refresh_cycle_atomically_with_a_bounded_wait(tmp_path, monkeypatch):
    import threading
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(OPEN))
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300, cash_flow_fn=lambda: r.cf)
    sched.step(r.clock.now)
    assert gate(r).state == "TRADING_ALLOWED" and r.cf.cycle_active() is False
    entered, release = threading.Event(), threading.Event()
    real = r.cf.refresh

    def slow(now=None, trigger="SCHEDULED"):
        entered.set()
        release.wait(10)
        return real(now, trigger)
    r.cf.refresh = slow
    r.clock.advance(60)
    t = threading.Thread(target=sched.step, args=(r.clock.now,))
    t.start()
    assert entered.wait(10) and r.cf.cycle_active()                                         # this cycle's snapshot is committed, its activity read in flight
    assert r.ref.store.snapshot()["refreshed_at"] > r.cf.process_success_at
    threading.Timer(0.3, release.set).start()
    v = gate(r)                                                                              # waits for the same cycle's ledger instead of reading STALE
    t.join(10)
    assert v.state == "TRADING_ALLOWED" and v.daily_loss.cash_flow_status == "ADJUSTED" and not t.is_alive() and r.cf.cycle_active() is False
    monkeypatch.setattr(config, "CASH_FLOW_CYCLE_WAIT_SECONDS", 0.2)                          # a cycle stuck past the bound fails closed
    entered.clear()
    release.clear()
    r.clock.advance(60)
    t = threading.Thread(target=sched.step, args=(r.clock.now,))
    t.start()
    assert entered.wait(10)
    v = gate(r)
    assert v.state == "NO_NEW_ENTRIES" and v.reason_codes == ["CASH_FLOW_DATA_STALE"]
    release.set()
    t.join(10)
    assert gate(r).state == "TRADING_ALLOWED" and r.cf.cycle_active() is False
    r.cf.refresh = lambda now=None, trigger="SCHEDULED": (_ for _ in ()).throw(RuntimeError("boom"))
    r.clock.advance(60)
    sched.step(r.clock.now)
    assert r.cf.cycle_active() is False                                                    # an exception never leaves the cycle open


# ================================================================ M01 multi-hour simulated validation ====================================

def test_M01_six_and_a_half_hours_no_funding_deposit_withdrawal_and_a_real_halt(tmp_path, monkeypatch):
    """09:30-16:00 New York, one refresh cycle per minute with Paper Auto on: the gate's adjusted numbers are checked against an independent
    Decimal ledger at every minute; funding never halts; the real trading loss does, and stays latched through a later deposit."""
    r = make_rig(tmp_path, monkeypatch, clock=FakeClock(datetime(2026, 10, 21, 13, 30, tzinfo=UTC)))
    sched = AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300,
                                    after_refresh=lambda: r.s.safety.evaluate("SCHEDULED", r.clock.now), cash_flow_fn=lambda: r.cf)
    sched.step(r.clock.now)
    go_ready(r)
    r.x.enable_auto(actor="test", now=r.clock.now)
    start, flows, trading = Decimal("100000"), Decimal(0), Decimal(0)
    script = {120: Decimal("20000"), 200: Decimal("-30000"), 330: Decimal("15000")}       # minute -> deposit (+) / withdrawal (-)
    pending_until = {}
    log, states = [], {}
    halted_at = None
    for minute in range(1, 388):                                                           # 09:32 .. 15:58 New York (in session)
        r.clock.advance(60)
        if minute <= 300:
            trading = (Decimal(400) * Decimal(minute % 7 - 3) / 3).quantize(Q)            # small noise around zero
        elif minute <= 320:
            trading = (Decimal("-6000") * (minute - 300) / 20).quantize(Q)                # a real trading loss ramp to -6% of the adjusted start
        else:
            trading = Decimal("-5200")
        if minute in script:
            amt = script[minute]
            fund(r, amt)
            pending_until[minute] = amt
        cur = start + flows + sum(pending_until.values()) + trading
        equity(r, cur)
        sched.step(r.clock.now)
        r.x.run_cycle(r.clock.now, "TEST")
        v = r.s.safety.current(max_age_s=0, now=r.clock.now)
        d = v.daily_loss
        states[v.state] = states.get(v.state, 0) + 1
        if pending_until and any(m == minute for m in pending_until):
            assert v.state in ("NO_NEW_ENTRIES", "HALTED") and (v.state == "HALTED" or v.reason_codes == ["CASH_FLOW_PENDING"]), (minute, v.reason_codes)
        else:
            for m in list(pending_until):
                flows += pending_until.pop(m)                                             # counted from the cycle after it posted
            adj = start + flows
            assert d.adjustments == "CASH_FLOW_ADJUSTED" and d.net_external_cash_flow == format(flows.quantize(Q), "f"), (minute, d)
            assert d.daily_pnl == format((cur - adj).quantize(Q), "f") and d.daily_loss_pct == pct(adj, cur), (minute, d.daily_pnl, d.daily_loss_pct)
            if halted_at is None and Decimal(d.daily_loss_pct) < 5:
                assert v.state == "TRADING_ALLOWED", (minute, v.reason_codes)
        if v.state == "HALTED" and halted_at is None:
            halted_at = minute
        if halted_at is None:
            assert v.state != "HALTED", minute                                              # no false halt from any funding event
        else:
            assert v.state == "HALTED", minute                                              # latched for the rest of the day
        log.append((minute, v.state, d.daily_loss_pct, d.net_external_cash_flow))
    assert 300 < halted_at <= 320 and states["HALTED"] == log[-1][0] - halted_at + 1
    assert states.get("NO_NEW_ENTRIES") == 2                                                # exactly one PENDING minute per pre-halt funding event
    assert [json.loads(b)["side"] for b in r.sim.posts][:1] == ["buy"] and r.s.safety.store.event_count("DAILY_HALT_TRIGGERED") == 1
    assert r.s.safety.store.state().daily_halt_active and r.s.safety.store.state().daily_halt_loss_pct == log[halted_at - 1][2]
    (tmp_path / "m01_summary.json").write_text(json.dumps({"minutes": len(log), "states": states, "halted_at_minute": halted_at, "flows": str(flows),
                                                           "posts": len(r.sim.posts)}), encoding="utf-8")
    print("M01 summary:", json.dumps({"minutes": len(log), "states": states, "halted_at_minute": halted_at, "net_external_at_close": str(flows), "paper_posts": len(r.sim.posts)}))


# ================================================================ Z no strategy / execution change =======================================

def _strip602(text: str) -> str:
    return re.sub(r"(?ms)^[ \t]*# >>> Stage 6\.0\.2.*?^[ \t]*# <<< Stage 6\.0\.2[^\n]*\n", "", text)
# >>> Stage 6.0.3: tests/test_execution_60.py and tests/test_account_refresh_601.py also carry one marked, additive Stage 6.0.3 block each (the
# CI-parallelization pin reconciliation, pinned against the Stage 6.0.3 base by tests/test_ci_groups_603.py); strip those blocks as well
_strip602_blocks_only = _strip602


def _strip602(text: str) -> str:  # noqa: F811 - wraps the Stage 6.0.2 strip above
    return _strip602_blocks_only(re.sub(r"(?ms)^[ \t]*# >>> Stage 6\.0\.3.*?^[ \t]*# <<< Stage 6\.0\.3[^\n]*\n", "", text))
# <<< Stage 6.0.3
# >>> Stage 6.1: tests/test_execution_60.py also carries one marked, additive Stage 6.1 block (the index.html rule of the forward paper validation panel,
# pinned against the Stage 6.1 base by tests/test_forward_validation_61.py); strip Stage 6.1 blocks as well
_strip602_through_603 = _strip602


def _strip602(text: str) -> str:  # noqa: F811 - wraps the Stage 6.0.2 / 6.0.3 strips above
    return _strip602_through_603(re.sub(r"(?ms)^[ \t]*# >>> Stage 6\.1\b.*?^[ \t]*# <<< Stage 6\.1[^\n]*\n", "", text))
# <<< Stage 6.1


def test_Z01_strategy_execution_and_prior_stage_files_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n").decode("utf-8")

    def now(rel):
        return ledger_view(rel, (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n"))
    pinned = [p.relative_to(ROOT).as_posix() for d in ("ppe", "demand", "evidence", "setups", "proposals", "paper") for p in (ROOT / d).glob("*.py")] + \
             [p.relative_to(ROOT).as_posix() for p in (ROOT / "account_refresh").glob("*.py") if p.name not in ("scheduler.py", "__init__.py")] + \
             ["execution/engine.py", "execution/broker.py", "execution/store.py", "execution/scheduler.py", "execution/simulated.py", "execution/models.py", "execution/__init__.py",
              "database/execution_migrations.py", "database/account_refresh_migrations.py", "database/execution_safety_migrations.py", "api/routes/execution.py",
              "api/routes/execution_safety.py", "api/routes/proposals.py", "api/routes/setups.py", "api/routes/account_refresh.py", "trader_view/models.py", "trader_view/__init__.py",
              "frontend/risk_controls.js", "frontend/trader_view.js", "frontend/trader_render.js", "frontend/trade_review.js", "frontend/index.html", "frontend/trader_view.css",
              "scripts/run_aapl_paper_smoke.py", "scripts/start_stock_agent.ps1", "scripts/stop_stock_agent.ps1", "tests/conftest.py", "tests/test_aapl_smoke_60.py",
              "tests/test_execution_safety_591.py", "tests/test_proposals_59.py", "tests/test_setups_58.py", "tests/test_windows_runner_60.py", "tests/frontend/trader_account.test.cjs",
              "DESIGN_591_EXECUTION_SAFETY.md", "DESIGN_60_AUTONOMOUS_PAPER.md", "DESIGN_601_CONTINUOUS_ACCOUNT_REFRESH.md"]
    # >>> Stage 6.1.1: frontend/index.html also carries two marked, additive Product UI 6.1.1 blocks (the product shell stylesheet and script, pinned against
    # the Stage 6.1.1 base by tests/test_product_ui_611.py); they are stripped from the page before the Stage 6.1 / 6.0.2 comparisons below
    _now_through_61 = now

    def now(rel):  # noqa: F811 - wraps the read above for the one page that carries Product UI blocks
        text = _now_through_61(rel)
        return re.sub(r"(?ms)^<!-- >>> Product UI 6\.1\.1\b.*?^<!-- <<< Product UI 6\.1\.1 -->\n", "", text) if rel == "frontend/index.html" else text
    # <<< Stage 6.1.1
    # >>> Stage 6.1: frontend/index.html carries two marked, additive Stage 6.1 blocks (the Forward Paper Validation stylesheet and script); everything
    # outside them stays byte-identical (the blocks themselves are pinned by tests/test_forward_validation_61.py)
    html61 = re.sub(r"(?ms)^<!-- >>> Stage 6\.1\b.*?^<!-- <<< Stage 6\.1 -->\n", "", now("frontend/index.html"))
    assert html61 == blob("frontend/index.html"), f"frontend/index.html differs from {BASE} outside the Stage 6.1 blocks"
    pinned = [p for p in pinned if p != "frontend/index.html"]
    # <<< Stage 6.1
    for rel in pinned:
        assert now(rel) == blob(rel), f"{rel} differs from {BASE}"
    blocks = {"safety/__init__.py": 1, "safety/models.py": 2, "safety/engine.py": 2, "tests/test_execution_60.py": 1, "tests/test_account_refresh_601.py": 1}
    for rel, n in blocks.items():
        assert now(rel).count("# >>> Stage 6.0.2") == n and now(rel).count("# <<< Stage 6.0.2") == n, rel
        assert _strip602(now(rel)) == blob(rel), f"{rel} differs from {BASE} outside its Stage 6.0.2 blocks"
    for rel in (p.relative_to(ROOT).as_posix() for p in (ROOT / "safety").glob("*.py")):
        if rel not in blocks:
            assert now(rel) == blob(rel), rel
    svc_now, svc_base = now("execution/service.py"), blob("execution/service.py")
    i = svc_now.rindex(chr(10), 0, svc_now.index("# Stage 6.0.2: cash-flow data")) + 1
    j = svc_now.index("        return BotStatus(", i)
    assert svc_now[:i] + svc_now[j:] == svc_base                                                 # the executor changed by a display-only block
    assert now("config.py").startswith(blob("config.py").rstrip("\n")) and config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False
    old_line = '              "order or a position. Deposits and withdrawals still cannot be separated from trading P&L.")'
    new_line = '              "order or a position. Deposits and withdrawals are separated from trading P&L by the Stage 6.0.2 cash-flow ledger.")'
    assert now("account_refresh/__init__.py") == blob("account_refresh/__init__.py").replace(old_line, new_line) != blob("account_refresh/__init__.py")   # one outdated sentence
    sched_now, sched_base = now("account_refresh/scheduler.py").splitlines(), blob("account_refresh/scheduler.py").splitlines()
    removed = [l for l in sched_base if l not in sched_now]
    assert len(removed) == 4, removed                                                          # docstring tail, the signature, status() and get_scheduler(): extended in place
