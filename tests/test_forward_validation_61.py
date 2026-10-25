"""
Stage 6.1 - FORWARD PAPER VALIDATION (DESIGN_61_FORWARD_PAPER_VALIDATION.md). Simulation only: the real executor, the real refresher, the real
cash-flow ledger and the real safety gate on the deterministic simulated PAPER broker (tests/test_cash_flow_602.py rig); no network, no real
order, no Robinhood, no model call. Sections:

  S session records: creation, health classification (HEALTHY / DEGRADED / FAILED / NOT_RUN), back-fill, start / end checks
  F the funnel and the descriptive statistics from PERSISTED events: order reliability, reconciliation, safety fires, freshness, broker health
  T closed trades: slippage, P/L, sampled MFE / MAE, exit reason, attribution, the end-to-end audit trace
  A attention and the early stop (the one action: Paper Auto -> Monitor only); restart continuity
  M the arithmetic (pure functions): slippage, P/L, excursions, win / loss / expectancy, drawdown, edge status, session classification, rates
  R reports and exports; D data immutability and the additive migration; L the observer loop never runs in a test; X the read-only API
  U the frontend (node tests, wiring); N no real order / no production write proof
  Z freeze: the strategy controls (hash + values), prior-stage files identical to the Stage 6.1 base outside the marked blocks, the CI group
"""
import csv
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import forward_validation as FV  # noqa: E402
from forward_validation import metrics as M  # noqa: E402
from forward_validation import reports as REP  # noqa: E402
from forward_validation.scheduler import ForwardValidationScheduler, start_if_enabled  # noqa: E402
from forward_validation.service import ForwardValidationService, ny_window, weekdays_between  # noqa: E402
from forward_validation.store import ForwardValidationStore, ForwardValidationStoreError  # noqa: E402
from database.forward_validation_migrations import TABLES, run_forward_validation_migrations  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_cash_flow_602 import cash_config, cycle, equity, make_rig, started, go_ready  # noqa: E402,F401 - cash_config: the autouse config fixture of the shared rig

BASE = "3904175bcd9dd7dbc875442aac36fde429694d06"                       # main = the Stage 6.0.3 merge (PR #16): the base this stage is built on
UTC = timezone.utc
DATE = "2026-10-21"                                                   # the rig's session: Wednesday 11:00 ET
AFTER_CLOSE = datetime(2026, 10, 21, 20, 5, tzinfo=UTC)               # 16:05 ET
NEXT_MONDAY = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
SRC = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "forward_validation").glob("*.py")) + ["api/routes/forward_validation.py", "database/forward_validation_migrations.py"]
NEW_FILES = set(SRC) | {"frontend/forward_validation.js", "frontend/forward_validation.css", "frontend/forward_validation.html", "tests/test_forward_validation_61.py",
                        "tests/frontend/forward_validation.test.cjs", "DESIGN_61_FORWARD_PAPER_VALIDATION.md"}
ADDITIVE = {"config.py", "api/server.py", "trader_view/stream.py", "frontend/index.html"}
RECONCILED_TESTS = {"tests/test_trader_view_561.py": 1, "tests/test_execution_60.py": 1, "tests/test_cash_flow_602.py": 2, "tests/test_ci_groups_603.py": 1}
CI_FILES = {".github/ci/pytest_groups.json", ".github/workflows/tests.yml", "browser_tests/flows.py", "README.md"}
STRIP_61 = r"(?ms)^[ \t]*# >>> Stage 6\.1\b.*?^[ \t]*# <<< Stage 6\.1[^\n]*\n"
STRIP_61_HTML = r"(?ms)^<!-- >>> Stage 6\.1\b.*?^<!-- <<< Stage 6\.1 -->\n"
OPEN_61, CLOSE_61 = "# >>> " + "Stage 6.1", "# <<< " + "Stage 6.1"                   # built, not literal: this file must not read as a marked-block carrier
STRIP_611 = r"(?ms)^[ \t]*# >>> Stage 6\.1\.1\b.*?^[ \t]*# <<< Stage 6\.1\.1[^\n]*\n"                                                 # Stage 6.1.1: its marked blocks are stripped first
STRIP_611_HTML = r"(?ms)^<!-- >>> Product UI 6\.1\.1\b.*?^<!-- <<< Product UI 6\.1\.1 -->\n"                                            # Stage 6.1.1: the product shell blocks of index.html
NEW_611 = {"frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/frontend/product_ui.test.cjs", "DESIGN_611_PRODUCT_UI.md"}   # Stage 6.1.1 files


def rig(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.fv = r.s.forward_validation                                     # wired by the stream on the same database
    r.fv.clock = r.clock
    return r


def cyc(r, advance=30):
    r.clock.advance(advance)
    return r.x.run_cycle(r.clock.now, "TEST")


def obs(r, advance=0):
    if advance:
        r.clock.advance(advance)
    return r.fv.observe(r.clock.now, "TEST")


def trade_one(r, symbol="SNDK"):
    """READY -> Paper Auto -> one entry POST -> fill -> one evaluated cycle (a price mark) -> an open paper position."""
    pv = go_ready(r, symbol)
    r.x.enable_auto(actor="test", now=r.clock.now)
    cyc(r)
    cyc(r)
    pos = r.x.store.open_position(symbol)
    assert pos is not None and pos.quantity == pv.final_qty, r.x.store.intents()
    cyc(r)
    return pv, r.x.store.open_position(symbol)


def close_one(r, pv):
    inv = Decimal(pv.invalidation_price) - Decimal("0.50")
    r.x.price_fn = lambda sym: (inv, "TEST", r.clock.now.isoformat(timespec="seconds"))
    r.sim.set_price(pv.symbol, float(inv))
    cyc(r)                                                            # exit decided and POSTed
    cyc(r)                                                            # exit filled -> CLOSED
    closed = [p for p in r.x.store.positions(pv.symbol, open_only=False) if p.state == "CLOSED"]
    assert closed, r.x.store.positions(pv.symbol, open_only=False)
    return closed[0]


def end_day(r):
    """The first observation after the close. The Stage 6.0.1 loop refreshes every five minutes outside the session, so the data is fresh then."""
    r.clock.now = AFTER_CLOSE
    cycle(r)
    return obs(r)


def full_day(r):
    """A complete observed session: fresh data, one closed paper trade, observations through the day, the post-close observation."""
    started(r)
    o0 = obs(r)
    pv, pos = trade_one(r)
    o1 = obs(r)
    closed = close_one(r, pv)
    cycle(r)
    o2 = obs(r)
    o3 = end_day(r)
    return pv, pos, closed, (o0, o1, o2, o3)


def blob(rel):
    out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
    return out.replace(b"\r\n", b"\n").decode("utf-8")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


# ================================================================ S session records ===========================================================

def test_S01_a_complete_session_is_recorded_and_classified_healthy_with_its_daily_summary(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, (o0, o1, o2, o3) = full_day(r)
    assert (o0["result"], o0["trading_date"], o0["session_status"], o0["attention"], o0["early_stop"], o0["restart"]) == ("OK", DATE, "OPEN", [], None, None)
    s0 = r.fv.store.samples(DATE)[0]
    assert s0["in_session"] and s0["mode"] == "MONITOR_ONLY" and s0["account_status"] == "FRESH" and s0["cash_flow_status"] == "FRESH" and s0["safety_state"] == "TRADING_ALLOWED"
    assert s0["equity"] == "100000.00" and s0["reconciliation_healthy"] is True and s0["attention"] == [] and s0["positions"] == [] and s0["controls"] == {}   # no executor cycle yet
    s1 = r.fv.store.samples(DATE)[1]
    assert s1["mode"] == "PAPER_AUTO" and s1["open_positions"] == 1 and s1["positions"][0]["position_id"] == pos.position_id and s1["positions"][0]["quantity"] == pv.final_qty
    assert "SAFETY_GATE" in s1["controls"] and s1["controls"]["ENTRY_SUBMISSION"] >= 1 and s1["executor_ready"] is True
    assert r.fv.store.marks(pos.position_id) and r.fv.store.marks(pos.position_id)[0]["last_price"] == pos.last_price
    assert o3["session_status"] == "HEALTHY" and o3["backfilled"] == []
    row = r.fv.store.session(DATE)
    assert row["session_status"] == "HEALTHY" and row["notes_code"] == "" and row["market_open_seen"] and row["market_close_seen"] and row["paper_auto_enabled"]
    assert row["account_refresh_healthy"] and row["cash_flow_healthy"] and row["reconciliation_healthy"] and row["in_session_samples"] == 3 and row["ended_at"] == AFTER_CLOSE.isoformat(timespec="seconds")
    assert row["bot_start_time"] == o0["at"] and row["bot_stop_time"] == o3["at"] and row["owners"] == [r.fv.owner]
    kinds = [e["kind"] for e in r.fv.store.events(newest_first=False)]
    assert kinds == ["SESSION_OPENED", "SESSION_CLOSED"] and r.fv.store.events(kind="SESSION_CLOSED")[0]["code"] == "HEALTHY"
    daily = r.fv.store.daily(DATE)
    assert daily and daily["session_status"] == "HEALTHY" and daily["funnel"]["CLOSED"] == 1 and daily["closed_trades"][0]["symbol"] == "SNDK" and daily["realized_pnl"] == closed.realized_pnl
    assert daily["duplicates"] == 0 and daily["reconciliation_issues"] == 0 and daily["drawdown"]["points"] >= 3 and daily["freshness"]["account"]["refresh_failures"] == 0
    ctl = {c["control_id"]: c for c in r.fv.store.controls()}
    assert ctl["observer"]["evaluation_count"] == 4 and ctl["session_recorder"]["fire_count"] == 2 and ctl["summary_builder"]["fire_count"] == 1 and ctl["attention_monitor"]["last_result"] == "PASS"


def test_S02_an_unobserved_close_reads_degraded_and_unobserved_weekdays_are_backfilled_as_not_run(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    r.clock.now = NEXT_MONDAY
    o = obs(r)
    assert o["backfilled"] == ["2026-10-22", "2026-10-23"] and o["session_status"] == "OPEN"
    old = r.fv.store.session(DATE)
    assert old["session_status"] == "DEGRADED" and "CLOSE_NOT_OBSERVED" in old["notes"] and old["market_close_seen"] is False and old["ended_at"] == NEXT_MONDAY.isoformat(timespec="seconds")
    for d in ("2026-10-22", "2026-10-23"):
        nr = r.fv.store.session(d)
        assert nr["session_status"] == "NOT_RUN" and nr["notes"] == ["NO_OBSERVATION", "HOLIDAYS_NOT_MODELLED"] and nr["in_session_samples"] == 0
    assert r.fv.store.session("2026-10-24") is None and r.fv.store.session("2026-10-25") is None                      # the weekend is never a session
    assert [e["code"] for e in r.fv.store.events(kind="SESSION_NOT_RUN")] == ["NO_OBSERVATION", "NO_OBSERVATION"]
    cum = r.fv.cumulative(r.clock.now)
    assert (cum["sessions"]["completed"], cum["sessions"]["degraded"], cum["sessions"]["not_run"], cum["sessions"]["in_progress"]) == (1, 1, 2, 1)


def test_S03_pre_market_only_observations_read_not_run_and_a_first_observation_after_the_close_opens_nothing(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    r.clock.now = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)          # 09:00 ET: before the open
    o = obs(r)
    assert o["session_status"] == "OPEN" and r.fv.store.session(DATE)["market_open_seen"] is False
    r.clock.now = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)
    obs(r)
    assert r.fv.store.session(DATE)["session_status"] == "NOT_RUN" and r.fv.store.session(DATE)["notes"] == ["NO_OBSERVATION", "CLOSE_NOT_OBSERVED"]
    r2 = rig(tmp_path / "second", monkeypatch)
    r2.clock.now = AFTER_CLOSE
    o = obs(r2)
    assert o["session_status"] is None and r2.fv.store.session(DATE) is None and r2.fv.store.sample_count() == 1


def test_S04_stale_account_data_that_recovers_reads_degraded_and_the_freshness_and_broker_health_metrics_count_it(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    for _ in range(3):
        r.sim.read_fail_paths.extend(["/v2/account"] * r.ref.read_attempts)
        assert r.ref.refresh(trigger="TEST")["result"] == "FAILED"
    o = obs(r, 60)
    s = r.fv.store.samples(DATE)[-1]
    assert s["account_status"] == "STALE" and s["account_failures"] == 3 and s["safety_state"] == "NO_NEW_ENTRIES" and o["attention"] == ["PERSISTENT_REFRESH_FAILURE"]
    cycle(r, 60)
    obs(r)
    assert r.fv.store.samples(DATE)[-1]["account_status"] == "FRESH"
    assert end_day(r)["session_status"] == "DEGRADED"
    row = r.fv.store.session(DATE)
    assert "ACCOUNT_DATA_STALE_RECOVERED" in row["notes"] and "REFRESH_FAILURES" in row["notes"] and row["account_refresh_healthy"] is False and row["cash_flow_healthy"] is True
    fr = r.fv.freshness(DATE)
    assert fr["account"]["refresh_failures"] == 3 and fr["account"]["max_consecutive_failures"] == 3 and fr["account"]["statuses"] == {"FRESH": 2, "STALE": 1} and fr["cash_flow"]["refresh_failures"] == 0
    bh = r.fv.broker_health(DATE)
    assert bh["total_errors"] >= 3 and bh["account_read_failures"] == 3 and sum(bh["by_code"].values()) == bh["total_errors"]
    fires = r.fv.safety_fires(DATE)
    assert fires["fire_count"] >= 1 and fires["recovered"] >= 1 and fires["persistent"] == 0 and any("ACCOUNT_SNAPSHOT_STALE" in f["reason_codes"] for f in fires["fires"])


def test_S05_an_unresolved_quantity_mismatch_at_session_end_reads_failed_and_is_a_non_critical_attention(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv, pos = trade_one(r)
    r.sim.set_position("SNDK", pos.quantity - 1, float(pos.average_entry_price))
    cyc(r)
    o = obs(r)
    assert o["attention"] == ["UNRESOLVED_QTY_MISMATCH"] and o["early_stop"] == {"critical": [], "stopped": False} or o["early_stop"] is None
    assert r.x.mode()["mode"] == "PAPER_AUTO"                                                     # non-critical: no early stop
    rec = r.fv.reconciliation(DATE)
    assert rec["quantity_mismatches"] == 1 and rec["needs_attention_cases"] == 1 and rec["open"] == 1 and rec["detail"][-1]["resolution"] == "OPEN"
    assert end_day(r)["session_status"] == "FAILED"
    row = r.fv.store.session(DATE)
    assert "UNRESOLVED_RECONCILIATION" in row["notes"] and row["reconciliation_healthy"] is False
    cum = r.fv.cumulative(r.clock.now)
    assert cum["operational"]["status"] == "NOT READY" and cum["operational"]["reconciliation_open"] == 1 and cum["needs_attention"]


# ================================================================ F funnel + statistics ======================================================

def test_F01_the_funnel_counts_come_from_persisted_events_only(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    f = r.fv.funnel(DATE)
    c = f["counts"]
    assert (c["WATCH"], c["READY"], c["REVIEWABLE"], c["ENTRY_SUBMITTED"], c["ENTRY_FILLED"], c["ENTRY_PARTIAL"], c["POSITION_OPEN"]) == (1, 1, c["REVIEWABLE"], 1, 1, 0, 1) and c["REVIEWABLE"] >= 1
    assert (c["EXIT_REQUIRED"], c["EXIT_SUBMITTED"], c["EXIT_FILLED"], c["CLOSED"], c["ATTENTION"]) == (1, 1, 1, 1, 0) and c["universe_scanned"] >= 1 and c["BLOCKED"] >= 0
    assert f["rates"]["watch_to_ready"] == "100.00" and f["rates"]["entry_to_fill"] == "100.00" and f["rates"]["position_to_normal_close"] == "100.00" and f["rates"]["position_to_attention"] == "0.00"
    assert set(c) >= set(FV.FUNNEL) and r.fv.funnel("2026-10-22")["counts"]["CLOSED"] == 0                           # another date: nothing inferred
    assert r.fv.funnel(None)["counts"] == {**c} or r.fv.funnel(None)["counts"]["CLOSED"] == 1
    rel = r.fv.order_reliability(DATE)
    assert (rel["entry_intents"], rel["exit_intents"], rel["broker_submissions"], rel["fills"], rel["partial_fills"], rel["rejects"], rel["cancels"], rel["expired_orders"]) == (1, 1, 2, 2, 0, 0, 0, 0)
    assert (rel["ambiguous_submissions"], rel["duplicate_attempts"], rel["duplicate_broker_orders"], rel["target"]) == (0, 0, 0, {"duplicate_broker_orders": 0, "met": True})
    rec = r.fv.reconciliation(DATE)
    assert rec["findings"] == rec["blocking"] == rec["open"] == 0 and rec["healthy_reconciliations"] >= 1
    assert r.fv.safety_fires(DATE)["persistent"] == 0 and r.fv.restarts_view(DATE)["count"] == 0


def test_F02_safety_fires_are_counted_and_classified_from_the_persisted_gate_rows(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    r.s.safety.pause(reason="test pause", actor="user", now=r.clock.now)
    r.clock.advance(60)
    r.s.safety.resume(reason="test resume", actor="user", now=r.clock.now)
    r.clock.advance(60)
    r.s.safety.emergency_stop(reason="test stop", actor="user", now=r.clock.now)
    r.clock.advance(60)
    r.s.safety.clear_emergency_stop(reason="cleared", actor="user", now=r.clock.now)
    f = r.fv.safety_fires(DATE)
    assert f["fire_count"] == 2 and f["recovered"] == 2 and f["persistent"] == 0 and f["pauses"] == 1 and f["emergency_stops"] == 1 and f["daily_loss_halts"] == 0
    assert [x["state"] for x in f["fires"]] == ["NO_NEW_ENTRIES", "HALTED"] and f["by_reason_code"] and "BASELINE_CREATED" in f["expected_events"]
    r.clock.advance(60)
    r.s.safety.pause(reason="still paused", actor="user", now=r.clock.now)
    f2 = r.fv.safety_fires(DATE)
    assert f2["fire_count"] == 3 and f2["persistent"] == 1 and f2["fires"][-1]["outcome"] == "PERSISTENT"
    assert r.fv.safety_fires("2026-10-22") == {**r.fv.safety_fires("2026-10-22"), "fire_count": 0}


# ================================================================ T closed trades + trace =====================================================

def test_T01_a_closed_trade_carries_slippage_pnl_sampled_excursions_exit_reason_and_setup_attribution(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    trades = r.fv.closed_trades()
    assert len(trades) == 1
    t = trades[0]
    assert (t["symbol"], t["qty"], t["entry_price"], t["exit_price"], t["realized_pnl"], t["exit_reason"], t["holding_sessions"]) == ("SNDK", pv.final_qty, closed.average_entry_price, closed.average_exit_price, closed.realized_pnl, "HARD_INVALIDATION", closed.sessions_held)
    pnl = (Decimal(closed.average_exit_price) - Decimal(closed.average_entry_price)) * closed.exit_qty
    assert Decimal(t["gross_pnl"]) == pnl.quantize(Decimal("0.01")) and t["fees"] is None and t["net_pnl"] == t["gross_pnl"] and t["return_pct"] is not None and "not attributable" in t["fees_note"]
    es, xs = t["entry_slippage"], t["exit_slippage"]
    assert es["reference_price"] == M.money(Decimal(pv.reference_price)) and es["fill_price"] == closed.average_entry_price and es["slippage_absolute"] == "0.00" and es["slippage_bps"] == "0.00"
    assert xs["reference_price"] is not None and xs["slippage_absolute"] == "0.00" and "positive = adverse" in xs["definition"]
    ex = t["excursions"]
    assert ex["marks"] >= 1 and ex["basis"] == "sampled marks" and ex["mfe_pnl"] is not None and ex["mae_pnl"] is not None and Decimal(ex["mae_pnl"]) <= 0 <= Decimal(ex["mfe_pnl"])
    a = t["attribution"]
    assert a["setup_type"] == "DOWNSIDE_DISLOCATION_RECOVERY" and a["cycle_id"] == 1 and a["ready_reason"] == "CONFIRMATION_STRENGTHENED" and (a["stretch"], a["confidence"], a["demand"]) == (-54.0, 58, 72)
    assert a["reference_price"] == pv.reference_price and a["invalidation_price"] == pv.invalidation_price and a["risk_per_share"] == pv.risk_per_share and a["proposal_qty"] == pv.final_qty and a["equity_at_proposal"] == pv.equity
    assert t["market_context"]["market_trend_proxy"] is None and "no in-process" in t["market_context"]["note"] and t["trading_date"] == DATE
    assert r.fv.closed_trades(trading_date="2026-10-22") == [] and r.fv.closed_trades(trading_date=DATE)[0]["position_id"] == pos.position_id
    assert r.fv.exit_reason_distribution(trades)["HARD_INVALIDATION"] == 1 and sum(r.fv.exit_reason_distribution(trades).values()) == 1


def test_T02_the_audit_trace_answers_every_question_of_the_trade_from_persisted_rows(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    tr = r.fv.trace(pos.position_id)
    assert tr["why_considered"] and tr["why_considered"][0]["to_state"] == "WATCH" and tr["why_considered"][0]["inputs"]["stretch"] == -67.0
    assert tr["why_ready"] and tr["why_ready"][0]["reason_code"] == "CONFIRMATION_STRENGTHENED" and tr["why_ready"][0]["inputs"]["demand"] == 72
    q = tr["why_quantity"]
    assert q["final_qty"] == pv.final_qty and q["risk_budget"] == pv.risk_budget and q["equity"] == pv.equity and q["reference_price"] == pv.reference_price
    allowed = tr["why_entry_allowed"]
    assert allowed["safety_gate_before_submission"]["allowed_new_entry"] == 1 and allowed["safety_gate_before_submission"]["state"] == "TRADING_ALLOWED"
    assert allowed["proposal_controls"] and all(c["result"] in ("PASS", "WARN") for c in allowed["proposal_controls"]) and allowed["entry_environment"]["paper"] is True
    o = tr["broker_order_sent"]
    assert o["client_order_id"].startswith("stockagent-entry-") and o["side"] == "buy" and o["qty"] == pv.final_qty and o["state"] == "FILLED" and o["broker_order_id"]
    assert tr["fill"]["filled_qty"] == pv.final_qty and tr["fill"]["filled_avg_price"] == closed.average_entry_price and tr["fill"]["order_events"]
    assert tr["why_exit_required"]["kind"] == "EXIT_REQUIRED" and tr["why_exit_required"]["reason"] == "HARD_INVALIDATION" and "price" in json.dumps(tr["why_exit_required"]["detail"]).lower()
    assert len(tr["exit_orders"]) == 1 and tr["exit_orders"][0]["state"] == "FILLED" and tr["exit_orders"][0]["client_order_id"].startswith("stockagent-exit-")
    assert tr["final_pnl"]["realized_pnl"] == closed.realized_pnl and tr["final_pnl"]["gross_pnl"] is not None and [e["kind"] for e in tr["position_events"]][0] == "OPENED" and tr["position_events"][-1]["kind"] == "CLOSED"
    assert r.fv.trace("0" * 32) is None


# ================================================================ A attention, early stop, restarts ===========================================

def test_A01_a_duplicate_broker_order_is_critical_stops_paper_auto_once_and_fails_the_session(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv, pos = trade_one(r)
    entry = r.x.store.intent(pos.execution_id)
    stamp = r.clock.now.isoformat(timespec="seconds")
    dup = {"id": "11111111-2222-4333-8444-555555555555", "client_order_id": entry.client_order_id, "symbol": "SNDK", "side": "buy", "qty": str(entry.qty), "status": "accepted", "filled_qty": 0,
           "filled_avg_price": None, "submitted_at": stamp, "updated_at": stamp, "raw": {}}
    r.x.store.record_order(dup, entry.execution_id, r.clock.now, "test")                          # a second broker order under our client id
    rel = r.fv.order_reliability(None)
    assert rel["duplicate_broker_orders"] == 1 and rel["target"]["met"] is False and entry.client_order_id in rel["duplicate_detail"][0]
    mode_events0 = len(r.x.store.mode_events())
    o = obs(r)
    assert o["attention"] == ["DUPLICATE_BROKER_ORDER"] and o["early_stop"] == {"critical": ["DUPLICATE_BROKER_ORDER"], "stopped": True}
    assert r.x.mode()["mode"] == "MONITOR_ONLY" and r.x.mode()["changed_by"] == "forward_validation" and "early stop" in r.x.mode()["reason"]
    ev = r.fv.store.events(kind="EARLY_STOP")
    assert len(ev) == 1 and ev[0]["code"] == "DUPLICATE_BROKER_ORDER" and ev[0]["detail"]["stopped"] is True and r.fv.store.events(kind="ATTENTION_RAISED")[0]["code"] == "DUPLICATE_BROKER_ORDER"
    o2 = obs(r, 60)
    assert o2["early_stop"] == {"critical": ["DUPLICATE_BROKER_ORDER"], "stopped": False} and len(r.fv.store.events(kind="EARLY_STOP")) == 1 and len(r.x.store.mode_events()) == mode_events0 + 1
    att = r.fv.attention(r.clock.now)
    assert att[0]["critical"] is True and att[0]["text"] == "a broker order was duplicated"
    assert end_day(r)["session_status"] == "FAILED"
    notes = r.fv.store.session(DATE)["notes"]
    assert "DUPLICATE_BROKER_ORDER" in notes and "EARLY_STOP" in notes
    cum = r.fv.cumulative(r.clock.now)
    assert cum["operational"]["duplicate_broker_orders"] == 1 and cum["operational"]["early_stops"] == 1 and cum["operational"]["status"] == "NOT READY" and cum["needs_attention"]
    ctl = {c["control_id"]: c for c in r.fv.store.controls()}
    assert ctl["early_stop"]["fire_count"] == 1 and ctl["attention_monitor"]["last_result"] == "BLOCK"


def test_A02_the_early_stop_can_be_disabled_and_never_touches_positions_or_orders(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.fv.early_stop_enabled = False
    started(r)
    pv, pos = trade_one(r)
    entry = r.x.store.intent(pos.execution_id)
    stamp = r.clock.now.isoformat(timespec="seconds")
    r.x.store.record_order({"id": "11111111-2222-4333-8444-666666666666", "client_order_id": entry.client_order_id, "symbol": "SNDK", "side": "buy", "qty": str(entry.qty), "status": "accepted",
                            "filled_qty": 0, "filled_avg_price": None, "submitted_at": stamp, "updated_at": stamp, "raw": {}}, entry.execution_id, r.clock.now, "test")
    posts, intents = r.sim.post_count(), len(r.x.store.intents())
    o = obs(r)
    assert o["early_stop"] == {"critical": ["DUPLICATE_BROKER_ORDER"], "stopped": False} and r.x.mode()["mode"] == "PAPER_AUTO"
    assert r.sim.post_count() == posts and len(r.x.store.intents()) == intents and r.x.store.open_position("SNDK").quantity == pos.quantity   # observing never orders or changes a position
    assert r.fv.store.events(kind="EARLY_STOP") == [] and r.fv.store.events(kind="ATTENTION_RAISED")


def test_A03_an_entry_recorded_while_the_gate_was_blocking_is_detected_from_the_persisted_rows(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    pv, pos = trade_one(r)
    assert r.fv._gate_violations() == []
    entry = r.x.store.intent(pos.execution_id)
    # forge the evidence the detector reads: a persisted blocking evaluation stamped just before the (real) submission time
    conn = sqlite3.connect(str(r.db))
    before = (datetime.fromisoformat(entry.submitted_at) - __import__("datetime").timedelta(seconds=1)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO execution_safety_evaluations (evaluation_id, evaluated_at, trigger, state, allowed_new_entry, reason_codes_json, daily_loss_json, kill_json, pause_json, freshness_json, input_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("f" * 32, before, "TEST", "HALTED", 0, '["EMERGENCY_STOP"]', "{}", "{}", "{}", "{}", "{}"))
    conn.commit()
    conn.close()
    v = r.fv._gate_violations()
    assert len(v) == 1 and v[0]["execution_id"] == entry.execution_id and v[0]["gate_state"] == "HALTED"
    o = obs(r)
    assert "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE" in o["attention"] and o["early_stop"]["stopped"] is True and r.x.mode()["mode"] == "MONITOR_ONLY"


def test_A04_a_restart_is_detected_with_state_before_and_after_reconciliation_result_and_position_continuity(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv, pos = trade_one(r)
    o1 = obs(r)
    assert o1["restart"] is None
    fv2 = ForwardValidationService(lambda: r.s, ForwardValidationStore(r.db), clock=r.clock, owner="proc2:restart")      # the next process on the same database
    r.clock.advance(120)
    o = fv2.observe(r.clock.now, "TEST")
    assert o["restart"] == {"recorded": True, "continuity": "CONTINUOUS", "reconciliation": "HEALTHY"}
    rs = fv2.store.restarts()
    assert len(rs) == 1 and rs[0]["previous_owner"] == r.fv.owner and rs[0]["owner"] == "proc2:restart" and rs[0]["duplicate_orders"] == 0 and rs[0]["broker_queried"] is True
    assert rs[0]["state_before"]["open_positions"] == 1 and rs[0]["state_after"]["open_positions"] == 1 and rs[0]["state_before"]["positions"][0]["position_id"] == pos.position_id
    assert fv2.observe(r.clock.now, "TEST")["restart"] is None and len(fv2.store.restarts()) == 1                        # recorded once per process
    assert fv2.restarts_view(DATE)["continuous"] == 1 and fv2.store.events(kind="RESTART_DETECTED")[0]["code"] == "CONTINUOUS"
    # a position that vanished without a CLOSED event is a discontinuity
    fv3 = ForwardValidationService(lambda: r.s, ForwardValidationStore(r.db), clock=r.clock, owner="proc3:restart")
    conn = sqlite3.connect(str(r.db))
    conn.execute("INSERT INTO forward_validation_samples (sample_id, at, trading_date, in_session, owner, mode, positions_json) VALUES (?,?,?,?,?,?,?)",
                 ("a" * 32, r.clock.advance(30).isoformat(timespec="seconds"), DATE, 1, "proc2:restart", "PAPER_AUTO", json.dumps([{"position_id": "ghost", "symbol": "AMD", "quantity": 3, "state": "OPEN"}])))
    conn.commit()
    conn.close()
    o = fv3.observe(r.clock.advance(30), "TEST")
    assert o["restart"]["continuity"].startswith("CHANGED: AMD")


# ================================================================ M the arithmetic ===========================================================

def test_M01_slippage_pnl_and_excursions_are_deterministic_and_never_invent_a_price():
    s = M.slippage("buy", "100.00", "100.25")
    assert (s["slippage_absolute"], s["slippage_bps"]) == ("0.25", "25.00") and "positive = adverse" in s["definition"]
    assert M.slippage("sell", "100.00", "99.80")["slippage_bps"] == "20.00" and M.slippage("sell", "100.00", "100.30")["slippage_absolute"] == "-0.30"
    assert M.slippage("buy", None, "10")["slippage_bps"] is None and M.slippage("buy", "0", "10")["slippage_absolute"] is None and M.slippage("buy", "abc", "10")["reference_price"] is None
    p = M.trade_pnl("100", "103", 10)
    assert (p["gross_pnl"], p["net_pnl"], p["return_pct"], p["notional"], p["fees"]) == ("30.00", "30.00", "3.00", "1000.00", None)
    assert M.trade_pnl("100", "103", 10, fees="1.50")["net_pnl"] == "28.50" and M.trade_pnl(None, "1", 1)["gross_pnl"] is None and M.trade_pnl("100", "99", 3)["return_pct"] == "-1.00"
    e = M.excursions("100", ["101", "99.5", "104", "102"], 10)
    assert (e["mfe_price"], e["mae_price"], e["mfe_pnl"], e["mae_pnl"], e["mfe_pct"], e["mae_pct"], e["marks"]) == ("104.00", "99.50", "40.00", "-5.00", "4.00", "-0.50", 4)
    assert M.excursions("100", [], 10)["mfe_pnl"] is None and M.excursions("100", ["100"], 1)["mae_pnl"] == "0.00"


def test_M02_win_loss_expectancy_drawdown_and_the_conservative_edge_status():
    w = M.win_loss(["10", "-5", "0", "20"])
    assert (w["closed_trades"], w["win_count"], w["loss_count"], w["flat_count"], w["gross_pnl"], w["win_rate"], w["average_win"], w["average_loss"]) == (4, 2, 1, 1, "25.00", "50.00", "15.00", "5.00")
    assert w["expectancy"] == "6.25" and w["largest_win"] == "20.00" and w["largest_loss"] == "-5.00" and "not a forecast" in w["expectancy_label"]
    assert M.win_loss([])["expectancy"] is None and M.win_loss([])["win_rate"] is None
    d = M.drawdown([("t1", "100"), ("t2", "110"), ("t3", "99"), ("t4", "105"), ("t5", "120")])
    assert (d["peak_equity"], d["trough_equity"], d["max_drawdown"], d["max_drawdown_pct"], d["current_drawdown"], d["current_drawdown_pct"], d["points"]) == ("120.00", "99.00", "11.00", "10.00", "0.00", "0.00", 5)
    assert d["trough_at"] == "t3" and d["basis"].startswith("adjusted equity") and M.drawdown([])["max_drawdown"] is None
    assert M.edge_status(2, 40, "5", "60", "1")["status"] == "INSUFFICIENT_DATA" and "closed trade(s)" in M.edge_status(2, 40, "5", "60", "1")["reasons"][0]
    assert M.edge_status(40, 10, "5", "60", "1")["status"] == "INSUFFICIENT_DATA" and "healthy session(s)" in M.edge_status(40, 10, "5", "60", "1")["reasons"][0]
    assert M.edge_status(40, 40, "-2", "60", "1")["status"] == "EARLY_NEGATIVE" and M.edge_status(40, 40, "5", "60", "1")["status"] == "EARLY_POSITIVE"
    assert M.edge_status(40, 40, "5", "30", "1")["status"] == "MIXED" and M.edge_status(40, 40, "5", "60", "15")["status"] == "MIXED" and M.edge_status(40, 40, None, "60", "1")["status"] == "INSUFFICIENT_DATA"
    assert M.edge_status(5, 3, "5", "60", "1", min_trades=5, min_sessions=3)["status"] == "EARLY_POSITIVE"
    for bad in ("PROFITABLE", "WINNING", "GUARANTEED", "HIGH_CONFIDENCE"):
        assert bad not in FV.EDGE_STATUSES and bad.lower() not in " ".join(FV.EDGE_LABELS.values()).lower()


def test_M03_session_classification_rules_and_funnel_rates():
    assert M.classify_session({"in_session_samples": 0}) == ("NOT_RUN", "NO_OBSERVATION")
    healthy = {"in_session_samples": 390, "market_open_seen": True, "market_close_seen": True, "account_fresh_seen": True, "cash_flow_fresh_seen": True}
    assert M.classify_session(healthy) == ("HEALTHY", "")
    assert M.classify_session({**healthy, "duplicate_orders": 1}) == ("FAILED", "DUPLICATE_BROKER_ORDER")
    assert M.classify_session({**healthy, "duplicate_orders": 1, "account_stale_samples": 4}) == ("FAILED", "DUPLICATE_BROKER_ORDER")             # FAILED wins over DEGRADED notes
    assert M.classify_session({**healthy, "live_host_attempts": 1, "early_stop": True})[1] == "LIVE_HOST_ATTEMPT|EARLY_STOP"
    for k in ("gate_violations", "unresolved_reconciliation_at_end", "unresolved_execution_at_end", "safety_unknown_at_end", "broker_unavailable_persistent"):
        assert M.classify_session({**healthy, k: 1})[0] == "FAILED", k
    assert M.classify_session({**healthy, "account_stale_samples": 2}) == ("DEGRADED", "ACCOUNT_DATA_STALE_RECOVERED")
    assert M.classify_session({**healthy, "market_close_seen": False}) == ("DEGRADED", "CLOSE_NOT_OBSERVED")
    assert M.classify_session({**healthy, "market_open_seen": False, "restarts": 1})[1] == "RESTART_DURING_SESSION|OPEN_NOT_OBSERVED"
    assert M.classify_session({**healthy, "account_fresh_seen": False})[1] == "ACCOUNT_DATA_NEVER_FRESH"
    assert M.rates({"WATCH": 4, "READY": 2, "REVIEWABLE": 1, "ENTRY_SUBMITTED": 1, "ENTRY_FILLED": 1, "POSITION_OPEN": 1, "CLOSED": 0, "ATTENTION": 0}) == \
           {"watch_to_ready": "50.00", "ready_to_reviewable": "50.00", "reviewable_to_entry": "100.00", "entry_to_fill": "100.00", "position_to_normal_close": "0.00", "position_to_attention": "0.00"}
    assert M.rates({})["watch_to_ready"] is None
    fires = M.classify_safety_fires([{"state": "TRADING_ALLOWED"}, {"state": "NO_NEW_ENTRIES", "reason_codes": ["X"], "evaluated_at": "a"}, {"state": "NO_NEW_ENTRIES"}, {"state": "TRADING_ALLOWED"},
                                     {"state": "HALTED", "reason_codes": ["Y"], "evaluated_at": "b"}])
    assert [(f["state"], f["outcome"]) for f in fires] == [("NO_NEW_ENTRIES", "RECOVERED"), ("HALTED", "PERSISTENT")]
    assert ny_window("2026-10-21") == ("2026-10-21T04:00:00+00:00", "2026-10-22T04:00:00+00:00") and ny_window("2026-01-15")[0] == "2026-01-15T05:00:00+00:00"
    assert weekdays_between("2026-10-16", "2026-10-21") == ["2026-10-19", "2026-10-20"] and weekdays_between("2026-10-21", "2026-10-22") == []


# ================================================================ R reports + exports =========================================================

def test_R01_the_daily_and_weekly_reports_are_deterministic_plain_text_without_marketing_language(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    summary = r.fv.store.daily(DATE)
    rep = REP.daily_report(summary)
    assert rep == REP.daily_report(summary) and rep.startswith(f"FORWARD PAPER VALIDATION - DAILY REPORT - {DATE}")
    for section in ("SESSION HEALTH", "SIGNALS", "TRADES", "SAFETY", "EXECUTION ISSUES", "P/L (descriptive, paper)", "OPEN POSITIONS", "NOTES"):
        assert f"\n{section}\n" in rep, section
    assert re.search(r"Status\s+HEALTHY", rep) and f"SNDK: {pv.final_qty} sh" in rep and re.search(r"Duplicate broker orders\s+0\n", rep) and FV.INSUFFICIENT_TEXT in rep
    assert not re.search(r"(?i)\b(profitable|winning|guaranteed|high confidence|you should|buy now|sell now|recommend)\b", rep)
    cum = r.fv.cumulative(r.clock.now)
    weekly = REP.weekly_report(cum, r.fv.store.dailies(newest_first=False))
    assert "Not yet available: 1 closed session(s) of the 5 needed" in weekly
    dailies = [dict(summary, trading_date=f"2026-10-{20 + i:02d}") for i in range(5)]
    weekly = REP.weekly_report(cum, dailies)
    assert "HEALTH (last 5 sessions: 2026-10-20 .. 2026-10-24)" in weekly and re.search(r"Healthy / degraded / failed / not run\s+5 / 0 / 0 / 0", weekly)
    assert re.search(r"Strategy status\s+Insufficient data - INSUFFICIENT FOR STRATEGY CONCLUSION", weekly) and "EXIT REASONS" in weekly and weekly == REP.weekly_report(cum, dailies)


def test_R02_exports_are_deterministic_csv_and_json_without_secrets(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    for kind in FV.EXPORTS:
        cols, rows = REP.export_rows(r.fv, kind)
        text = REP.to_csv(cols, rows)
        parsed = list(csv.DictReader(io.StringIO(text)))
        assert text.splitlines()[0] == ",".join(cols) and (parsed == [] or list(parsed[0]) == list(cols)), kind
        assert parsed or kind == "restarts", kind                                                             # every export but the (empty) restarts has rows here
        assert text == REP.to_csv(*REP.export_rows(r.fv, kind)), kind                                          # deterministic
        j = json.loads(REP.to_json(kind, cols, rows))
        assert j["export"] == kind and j["columns"] == list(cols) and len(j["rows"]) == len(rows)
        low = text.lower()
        assert "secret" not in low and "apca-api" not in low and "paper-api.alpaca.markets" not in low, kind
    cols, rows = REP.export_rows(r.fv, "trades")
    assert rows[0]["symbol"] == "SNDK" and rows[0]["entry_slippage_bps"] == "0.00" and rows[0]["exit_reason"] == "HARD_INVALIDATION" and rows[0]["mfe_pnl"] is not None
    assert REP.export_rows(r.fv, "sessions")[1][0]["session_status"] == "HEALTHY" and REP.export_rows(r.fv, "daily")[1][0]["CLOSED"] == 1
    assert [e["kind"] for e in REP.export_rows(r.fv, "control_events")[1]] == ["SESSION_OPENED", "SESSION_CLOSED"]
    with pytest.raises(ValueError):
        REP.export_rows(r.fv, "secrets")


# ================================================================ D data immutability + migration ===========================================

def test_D01_raw_rows_are_append_only_sessions_are_never_reclassified_and_the_migration_is_additive(tmp_path):
    db = tmp_path / "fv.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE earlier (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO earlier (v) VALUES ('kept')")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    before = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE '%forward_validation%'"))
    for _ in range(3):
        run_forward_validation_migrations(conn)
    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE '%forward_validation%'")) == before and conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= set(TABLES) and conn.execute("SELECT v FROM earlier").fetchone()[0] == "kept"
    conn.close()
    st = ForwardValidationStore(db)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    st.open_session(DATE, now, "o1", in_session=True)
    sid = st.add_sample({"at": "2026-10-21T14:00:00+00:00", "trading_date": DATE, "in_session": True, "owner": "o1", "mode": "MONITOR_ONLY", "positions": [], "controls": {}, "attention": [], "safety_codes": []})
    st.add_marks([{"at": "2026-10-21T14:00:00+00:00", "position_id": "p1", "symbol": "SNDK", "state": "OPEN", "quantity": 1, "last_price": "10.00", "last_price_at": None}])
    st.add_restart({"at": "2026-10-21T14:00:00+00:00", "trading_date": DATE, "owner": "o2", "reconciliation_result": "HEALTHY", "position_continuity": "CONTINUOUS"})
    st.add_event(now, "SESSION_OPENED", None, {}, DATE, "o1")
    st.touch_control("observer", now, "PASS", "tick")
    conn = sqlite3.connect(str(db))
    for sql in ("UPDATE forward_validation_samples SET mode = 'x'", "DELETE FROM forward_validation_samples", "UPDATE forward_validation_marks SET last_price = '1'", "DELETE FROM forward_validation_marks",
                "UPDATE forward_validation_restarts SET owner = 'x'", "DELETE FROM forward_validation_restarts", "UPDATE forward_validation_events SET kind = 'x'", "DELETE FROM forward_validation_events",
                "DELETE FROM forward_validation_sessions", "UPDATE forward_validation_sessions SET trading_date = '2026-01-01'", "DELETE FROM forward_validation_controls"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO forward_validation_samples (sample_id, at, trading_date, in_session, owner, mode) VALUES (?, 'x', ?, 1, 'o', 'm')", (sid, DATE))
    conn.close()
    with pytest.raises(ForwardValidationStoreError):
        st.insert_not_run(DATE, now, "x")                                                                 # a trading date has one row
    st.update_session(DATE, now, session_status="HEALTHY")
    with pytest.raises(ForwardValidationStoreError):
        st.update_session(DATE, now, session_status="DEGRADED")                                             # a closed session is never reclassified
    with pytest.raises(ForwardValidationStoreError):
        st.update_session(DATE, now, created_at="x")
    st.put_daily(DATE, now, "v1", {"a": 1})
    st.put_daily(DATE, now, "v2", {"a": 2})                                                                 # derived: rebuilt
    assert st.daily(DATE)["a"] == 2 and st.daily(DATE)["version"] == "v2"
    conn = sqlite3.connect(str(db))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM forward_validation_daily_summary")
    conn.close()
    assert ForwardValidationStore(tmp_path / "missing.db").sessions() == [] and ForwardValidationStore(tmp_path / "missing.db").query("SELECT 1 FROM execution_intents") == []


# ================================================================ L the observer loop ==========================================================

def test_L01_the_observer_loop_never_starts_in_a_test_process_and_paces_by_the_session(monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    assert start_if_enabled() is False
    monkeypatch.delenv("STOCK_AGENT_ENV", raising=False)
    assert start_if_enabled() is False                                                                      # PYTEST_CURRENT_TEST is set under pytest
    monkeypatch.setattr(config, "FORWARD_VALIDATION_ENABLED", False)
    assert start_if_enabled() is False
    calls = []

    class FakeService:
        def observe(self, now, trigger):
            calls.append((now, trigger))
            return {"result": "OK", "trading_date": DATE, "session_status": "OPEN"}

    class HeldLease:
        def acquire(self, owner, now, ttl_min, name):
            return False

        def release(self, owner, name):
            pass
    clock = {"now": datetime(2026, 10, 21, 15, 0, tzinfo=UTC)}
    s = ForwardValidationScheduler(lambda: FakeService(), clock=lambda: clock["now"], interval_s=60, idle_interval_s=300, startup_delay_s=0)
    assert s.step()["result"] == "OK" and len(calls) == 1 and s.next_at == clock["now"].replace(minute=1) and s.step() is None
    clock["now"] = clock["now"].replace(minute=1)
    assert s.step()["result"] == "OK" and s.runs == 2 and s.wait_seconds() == 60.0
    clock["now"] = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)                                                  # 18:00 ET: outside the session
    s.next_at = None
    s.step()
    assert s.next_at == datetime(2026, 10, 21, 22, 5, tzinfo=UTC) and s.wait_seconds() == 300.0
    st = s.status()
    assert st["lease"] == FV.LEASE_NAME == "forward_validation_observer" and st["running"] is False and st["runs"] == 3 and st["last_result"]["session_status"] == "OPEN" and st["market"]["regular_session"] is False
    held = ForwardValidationScheduler(lambda: FakeService(), clock=lambda: clock["now"], lease_store_fn=HeldLease, startup_delay_s=0)
    assert held.step() == {"result": "LEASE_HELD"} and len(calls) == 3


# ================================================================ X the read-only API ==========================================================

def test_X01_read_only_api_routes_shapes_and_route_words(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    from api.server import app
    api = TestClient(app)
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/forward-validation")}
    assert mine == {"/api/forward-validation": {"get"}, "/api/forward-validation/sessions": {"get"}, "/api/forward-validation/sessions/{trading_date}": {"get"}, "/api/forward-validation/closed": {"get"},
                    "/api/forward-validation/closed/{position_id}": {"get"}, "/api/forward-validation/daily": {"get"}, "/api/forward-validation/daily/{trading_date}": {"get"},
                    "/api/forward-validation/report/daily": {"get"}, "/api/forward-validation/report/daily/{trading_date}": {"get"}, "/api/forward-validation/report/weekly": {"get"},
                    "/api/forward-validation/export/{kind}": {"get"}, "/api/forward-validation/restarts": {"get"}, "/api/forward-validation/events": {"get"}, "/api/forward-validation/samples": {"get"},
                    "/api/forward-validation/funnel": {"get"}, "/api/forward-validation/funnel/{trading_date}": {"get"}, "/api/forward-validation/frozen-controls": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|trade|alpaca|robinhood|paper|tune|optimi", p), p
    j = api.get("/api/forward-validation").json()
    for k in ("version", "stage", "targets", "sessions", "events", "operational", "strategy", "attention", "needs_attention", "frozen_controls_hash", "disclaimer", "today", "today_session", "recent_sessions",
              "loop", "controls", "counts", "policy"):
        assert k in j, k
    assert j["sessions"]["completed"] == 1 and j["sessions"]["label"] == "1 / 30" and j["strategy"]["edge_status"] == "INSUFFICIENT_DATA" and j["strategy"]["insufficient_text"] == FV.INSUFFICIENT_TEXT
    assert j["operational"]["duplicate_broker_orders"] == 0 and j["operational"]["status"] == "READY" and j["needs_attention"] is False and j["frozen_controls_hash"] == FV.frozen_hash()
    s = api.get("/api/forward-validation/sessions").json()
    assert s["total"] == 1 and s["sessions"][0]["session_status"] == "HEALTHY"
    one = api.get(f"/api/forward-validation/sessions/{DATE}").json()
    assert one["session"]["trading_date"] == DATE and one["daily"]["funnel"]["CLOSED"] == 1 and one["samples"] == 4
    assert api.get("/api/forward-validation/sessions/2026-10-22").status_code == 404 and api.get("/api/forward-validation/sessions/20261021").status_code == 400
    c = api.get("/api/forward-validation/closed").json()
    assert c["count"] == 1 and c["closed"][0]["exit_reason"] == "HARD_INVALIDATION" and c["closed"][0]["entry_slippage"]["slippage_bps"] == "0.00"
    tr = api.get(f"/api/forward-validation/closed/{pos.position_id}").json()
    assert tr["why_ready"] and tr["broker_order_sent"]["client_order_id"].startswith("stockagent-entry-") and api.get("/api/forward-validation/closed/" + "0" * 32).status_code == 404
    assert api.get("/api/forward-validation/closed/not-hex!").status_code == 400
    d = api.get(f"/api/forward-validation/daily/{DATE}").json()
    assert d["session_status"] == "HEALTHY" and api.get("/api/forward-validation/daily").json()["daily"][0]["trading_date"] == DATE
    rep = api.get(f"/api/forward-validation/report/daily/{DATE}")
    assert rep.status_code == 200 and rep.headers["content-type"].startswith("text/plain") and rep.text.startswith("FORWARD PAPER VALIDATION - DAILY REPORT")
    assert api.get("/api/forward-validation/report/daily").text == rep.text and "Not yet available" in api.get("/api/forward-validation/report/weekly").text
    x = api.get("/api/forward-validation/export/trades")
    assert x.status_code == 200 and x.headers["content-type"].startswith("text/csv") and x.text.splitlines()[0].startswith("position_id,") and "attachment" in x.headers["content-disposition"]
    xj = api.get("/api/forward-validation/export/sessions?format=json")
    assert xj.status_code == 200 and json.loads(xj.text)["rows"][0]["session_status"] == "HEALTHY" and api.get("/api/forward-validation/export/nope").status_code == 400
    assert api.get("/api/forward-validation/restarts").json()["count"] == 0 and [e["kind"] for e in api.get("/api/forward-validation/events").json()["events"]] == ["SESSION_CLOSED", "SESSION_OPENED"]
    assert len(api.get(f"/api/forward-validation/samples?date={DATE}").json()["samples"]) == 4 and api.get(f"/api/forward-validation/funnel/{DATE}").json()["counts"]["CLOSED"] == 1
    fz = api.get("/api/forward-validation/frozen-controls").json()
    assert fz["hash"] == FV.frozen_hash() and fz["controls"]["ready_demand"] == 60 and "never tunes" in fz["note"]
    assert "APCA" not in json.dumps(j) and "secret" not in json.dumps(j).lower()


# ================================================================ U the frontend ===============================================================

def test_U01_node_tests_of_the_forward_validation_panel_and_page():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "forward_validation.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 6\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


def test_U02_wiring_the_compact_panel_and_the_separate_page_are_read_only():
    html = now_text("frontend/index.html")
    js = now_text("frontend/forward_validation.js")
    page = now_text("frontend/forward_validation.html")
    assert html.count("<!-- >>> Stage 6.1") == 2 == html.count("<!-- <<< Stage 6.1 -->") and '<script src="forward_validation.js"></script>' in html and 'href="forward_validation.css"' in html
    assert html.index('src="paper_bot.js"') < html.index('src="forward_validation.js"') and html.index('href="trader_view.css"') < html.index('href="forward_validation.css"')
    assert re.sub(STRIP_61_HTML, "", re.sub(STRIP_611_HTML, "", html)) == blob("frontend/index.html")      # the page differs from the base by the two marked blocks only (Stage 6.1.1 blocks stripped first)
    assert 'method: "POST"' not in js and "/api/execution/auto" not in js and "fetch(" in js and '"/api/forward-validation"' in js
    assert "INSUFFICIENT FOR STRATEGY CONCLUSION" in js and "NEEDS ATTENTION" in js and "tv-panel-bot" in js and "forward_validation.html" in js
    assert 'id="fv-page"' in page and 'src="forward_validation.js"' in page and "<form" not in page and "POST" not in page
    assert not re.search(r"(?i)\b(profitable|winning|guaranteed|buy now|sell now|you should)\b", js + page)


# ================================================================ N no real order / no production write ========================================

def test_N01_the_observer_never_orders_never_imports_a_broker_wire_and_writes_only_its_own_tables_plus_the_early_stop(tmp_path, monkeypatch):
    src = "\n".join(now_text(p) for p in SRC)
    for banned in ("post_order", "insert_intent", "enable_auto", "import requests", "from requests", "robinhood", "anthropic", "paper-api.alpaca.markets", "alpaca_order_writer", "TradingClient",
                   "update_intent", "update_position", "set_mode(", "apply_fill", "record_order("):
        assert banned not in src, banned
    assert src.count("disable_auto(") == 1                                                                   # the one action: Paper Auto -> Monitor only
    r = rig(tmp_path, monkeypatch)
    pv, pos, closed, _ = full_day(r)
    assert r.x.broker.simulated is True and r.sim.post_count() == 2                                            # exactly the executor's entry and exit; observing added none
    conn = sqlite3.connect(f"file:{r.db.as_posix()}?mode=ro", uri=True)
    tables = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert set(TABLES) <= tables and all(t.startswith("forward_validation_") for t in TABLES)
    assert r.fv.store.counts()["forward_validation_samples"] == 4 and r.fv.store.counts()["forward_validation_sessions"] == 1


# ================================================================ Z freeze ======================================================================

def test_Z01_the_strategy_controls_are_frozen_and_hashed():
    c = FV.frozen_controls()
    expected = {"setup_type": "DOWNSIDE_DISLOCATION_RECOVERY", "watch_enter_stretch": -40.0, "watch_exit_stretch": -30.0, "ready_confidence": 40, "ready_demand": 60, "ready_demand_coverage": 0.60,
                "ready_ppe_coverage": 0.60, "ready_exit_demand": 45, "invalidate_demand": 35, "invalidate_demand_evals": 2, "invalidate_stretch": -20.0, "evidence_max_age_days": 30,
                "risk_fraction": 0.005, "max_position_pct": 10.0, "proposal_ttl_s": 300, "proposal_price_change_pct": 0.5, "max_daily_loss_pct": float(config.MAX_DAILY_LOSS_PERCENT),
                "max_open_positions": 10, "max_hold_sessions": 5, "demand_exit": 35, "demand_exit_evals": 2, "stretch_exit": -20.0, "near_close_minutes": 15, "max_entries_per_cycle": 1,
                "max_exit_cycles": 3, "order_type": "market", "time_in_force": "day", "side": "LONG",
                "exit_reasons": ["HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED"],
                "session_rule": "regular session only (weekdays 09:30-16:00 ET), no extended hours", "leverage": "none", "shorts": "none"}
    assert c == expected, {k: (c.get(k), expected.get(k)) for k in set(c) | set(expected) if c.get(k) != expected.get(k)}
    assert FV.frozen_hash() == hashlib.sha256(json.dumps(c, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest() and len(FV.frozen_hash()) == 64
    assert config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL is False and config.FORWARD_VALIDATION_EARLY_STOP is True and config.FORWARD_VALIDATION_TARGET_SESSIONS == 30
    assert (FV.TARGET_SESSIONS, FV.TARGET_EVENTS, FV.MIN_TRADES_FOR_EDGE) == (30, 100, 30) and "not a forecast" in FV.DISCLAIMER.lower()


def test_Z02_prior_stage_files_are_identical_to_the_stage_61_base_outside_the_marked_blocks_and_the_additive_wiring():
    tracked = set(subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    in_base = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    assert in_base, "the base commit must be reachable (fetch-depth 0)"
    new = {p for p in tracked - in_base if not p.startswith(("scratchpad/", "runtime/", "data/", "browser_tests/artifacts/")) and not p.endswith((".pyc", ".db"))}
    assert new == NEW_FILES | NEW_611, {"unexpected_new": sorted(new - NEW_FILES - NEW_611), "missing_new": sorted((NEW_FILES | NEW_611) - new)}   # Stage 6.1.1 files added
    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""} - new   # files of the base that differ
    allowed = ADDITIVE | set(RECONCILED_TESTS) | {"tests/test_account_refresh_601.py", "tests/test_forward_validation_61.py"} | CI_FILES   # Stage 6.1.1 reconciles this file in place
    assert changed <= allowed, {"unexpected_changes": sorted(changed - allowed)}
    # production packages of the earlier stages: byte-identical (every file that exists in the base)
    for d in ("setups", "proposals", "safety", "execution", "account_refresh", "cash_flow", "ppe", "demand", "evidence", "paper", "forward", "notifications", "trader_view", "api/routes", "database", "scripts"):
        for rel in sorted(p for p in in_base if p.startswith(d + "/") and p.endswith((".py", ".ps1"))):
            if rel in ADDITIVE or rel in NEW_FILES:
                continue
            assert now_text(rel) == blob(rel), f"{rel} differs from {BASE}"
    for rel in ("frontend/risk_controls.js", "frontend/trader_view.js", "frontend/trader_render.js", "frontend/trade_review.js", "frontend/paper_bot.js", "frontend/setup_alerts.js", "frontend/trader_view.css",
                "frontend/trader_core.js", "tests/conftest.py", "tests/alpaca_order_fakes.py", "DESIGN_60_AUTONOMOUS_PAPER.md", "DESIGN_601_CONTINUOUS_ACCOUNT_REFRESH.md", "DESIGN_602_ADJUSTED_DAILY_PNL.md"):
        assert now_text(rel) == blob(rel), f"{rel} differs from {BASE}"
    assert re.sub(STRIP_61_HTML, "", re.sub(STRIP_611_HTML, "", now_text("frontend/index.html"))) == blob("frontend/index.html")   # Stage 6.1.1 blocks stripped first
    assert now_text("config.py").startswith(blob("config.py").rstrip("\n")) and "FORWARD_VALIDATION_ENABLED" in now_text("config.py")                      # config is append-only
    sig_old = "                 execution_service=None, account_refresh_service=None, cash_flow_service=None):"
    sig_new = "                 execution_service=None, account_refresh_service=None, cash_flow_service=None, forward_validation_service=None):"
    for rel in ("api/server.py", "trader_view/stream.py"):                                                                                                  # wiring: additive lines, every one Stage 6.1
        base_lines, now_lines = blob(rel).splitlines(), now_text(rel).splitlines()
        removed = [l for l in base_lines if l not in now_lines]
        assert removed == ([sig_old] if rel == "trader_view/stream.py" else []), (rel, removed)                                                        # the stream signature gains one keyword
        added = [l for l in now_lines if l not in base_lines]
        assert added and all("Stage 6.1" in l or "forward_validation" in l or "ForwardValidation" in l for l in added), (rel, added)
        if rel == "trader_view/stream.py":
            assert sig_new in now_lines and sig_new.startswith(sig_old[:-2])
    for rel, n in RECONCILED_TESTS.items():                                                                                                                 # the reconciled pin tests: marked blocks only
        text = re.sub(STRIP_611, "", now_text(rel))                                                                                                      # Stage 6.1.1 blocks stripped first
        assert text.count(OPEN_61) == n == text.count(CLOSE_61) and len(re.findall(STRIP_61, text)) == n, rel
        assert re.sub(STRIP_61, "", text) == blob(rel), f"{rel} differs from {BASE} outside its Stage 6.1 block(s)"
    old = r'    now603 = _re603.sub(r"(?ms)^[ \t]*# >>> Stage (6\.0\.[23]).*?^[ \t]*# <<< Stage \1[^\n]*\n", "", (ROOT / "tests/test_execution_60.py").read_text(encoding="utf-8").replace("\r\n", "\n"))'
    new_line = r'    now603 = _re603.sub(r"(?ms)^[ \t]*# >>> Stage (6\.0\.[23]|6\.1\b).*?^[ \t]*# <<< Stage \1[^\n]*\n", "", (ROOT / "tests/test_execution_60.py").read_text(encoding="utf-8").replace("\r\n", "\n"))   # Stage 6.1 widened'
    assert now_text("tests/test_account_refresh_601.py") == blob("tests/test_account_refresh_601.py").replace(old, new_line) != blob("tests/test_account_refresh_601.py")   # one widened regex
    carriers = sorted(p for p in tracked if (ROOT / p).is_file() and not p.endswith((".pyc", ".db")) and OPEN_61.encode("utf-8") in (ROOT / p).read_bytes())
    assert carriers == sorted(RECONCILED_TESTS), carriers


def test_Z03_the_ci_manifest_and_workflow_carry_the_stage_61_group():
    groups = json.loads((ROOT / ".github" / "ci" / "pytest_groups.json").read_text(encoding="utf-8"))["groups"]
    assert groups["validation"]["job"] == "Pytest Forward Validation" and groups["validation"]["files"] == ["test_forward_validation_61.py"]
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    entries = {e["suite"]: e["name"] for e in wf["jobs"]["pytest"]["strategy"]["matrix"]["include"]}
    assert entries["validation"] == "Pytest Forward Validation" and entries == {k: v["job"] for k, v in groups.items()}
    assert (ROOT / "DESIGN_61_FORWARD_PAPER_VALIDATION.md").exists()
