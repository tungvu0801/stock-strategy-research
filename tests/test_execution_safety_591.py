"""
Stage 5.9.1 — REAL EXECUTION SAFETY CONTROLS (persisted kill switch, daily-loss halt, no-new-entries; nothing submits an order). Focused tests:
  E01–E17 engine · S01–S12 service / persistence · F01–F04 forced fire through the REAL path · D01 dead control · R01 restart · Y01 day boundary
  X01–X09 no execution · U01–U05 frontend · B01 base pins
"""
import ast
import inspect
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
from freeze_ledger import view_bytes as ledger_bytes  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import safety as SF  # noqa: E402
from safety import engine as E  # noqa: E402
from safety.models import AccountEquity, Baseline, ControlEvaluation, DailyLoss, KillSwitch, Pause, SafetyStateRecord, SafetyView  # noqa: E402
from safety.service import ExecutionSafetyService, evaluate_execution_safety, startup_if_enabled  # noqa: E402
from safety.store import SafetyStore  # noqa: E402
from database.execution_safety_migrations import TABLES, run_execution_safety_migrations  # noqa: E402
from proposals.account import AccountAdapter  # noqa: E402
from proposals.service import ProposalService  # noqa: E402
from proposals.store import ProposalStore  # noqa: E402
from setups.service import SetupService  # noqa: E402
from setups.store import SetupStore  # noqa: E402
from trader_view import MESSAGE_TYPES, SYMBOL_BOUND_TYPES  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_trader_view_55 import FORBIDDEN_IMPORTS, T0, WL, FakePositions, FakeResearch, hello_of  # noqa: E402
from test_setups_58 import IN_SESSION, READY_IN, WATCH_IN, FakeClock, fx, strip_code  # noqa: E402

BASE = "bd57e45"                                                                       # origin/main the stage was branched from (Stage 5.9 merged)
UTC = timezone.utc
JS = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
RENDER = (ROOT / "frontend" / "trader_render.js").read_text(encoding="utf-8")
RISK_JS = (ROOT / "frontend" / "risk_controls.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "trader_view.css").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
SAFETY_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "safety").glob("*.py")) + ["api/routes/execution_safety.py", "database/execution_safety_migrations.py"]
TRADE_WORDS = re.compile(r"(?i)\b(buy|sell|buy now|sell now|enter now|trade now|recommend\w*|short it|go long|ready to buy|you should|approved)\b")
ALL = list(SF.CONTROLS)
TODAY = "2026-10-21"


def source_of(files):
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in files)


def acct(equity="100000.00", source="HYPOTHETICAL_REVIEW", age_s=None, adjustments=None, as_of=None):
    return AccountEquity(equity=equity, source=source, as_of=as_of, age_s=age_s, mode="simulated",
                         adjustments=adjustments or ("NONE_POSSIBLE" if source in ("LOCAL_PAPER", "HYPOTHETICAL_REVIEW") else "UNAVAILABLE"))


def baseline(starting="100000.00", date=TODAY, adjustments="NONE_POSSIBLE", source="HYPOTHETICAL_REVIEW"):
    return Baseline(trading_date=date, starting_equity=starting, baseline_created_at=f"{date}T14:30:00+00:00", source=source, adjustments=adjustments)


def dl(b, a, thr="5", **over):
    kw = dict(max_age_s=900.0, accept_unadjusted=False, latched=False, latched_trading_date=None, triggered_at=None, triggered_loss_pct=None, today=TODAY)
    kw.update(over)
    return E.daily_loss(b, a, thr, **kw)


# ---------------------------------------------------------------- stack (the real path) ----------------------------------------------

def make_stack(tmp_path, clock=None, **kw):
    holder = {}
    clock = clock or FakeClock()
    db = tmp_path / "stage591.db"
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(db), clock=clock, cooldown_minutes=15)
    safety = ExecutionSafetyService(stream_fn=lambda: holder["s"], store=SafetyStore(db), clock=clock)
    psvc = ProposalService(stream_fn=lambda: holder["s"], store=ProposalStore(db), account=AccountAdapter(path=db), clock=clock, safety=safety)
    s = TerminalStream(kw.pop("market", None) or SimulatedMarket(seed=11, start_time=T0), kw.pop("positions", None) or FakePositions(), kw.pop("research", None) or FakeResearch(),
                       watchlist_fn=lambda: list(WL), tick_seconds=1.0, allow_manual_tick=True, run_loop=False, now_fn=lambda: T0, setup_service=svc, proposal_service=psvc,
                       safety_service=safety, **kw)
    holder["s"] = s
    return s, svc, psvc, safety, clock


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", False)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", False)
    monkeypatch.setattr(config, "EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", False)
    monkeypatch.setattr(config, "EXECUTION_SAFETY_STARTUP_POLICY", "KEEP")
    s, svc, psvc, safety, clock = make_stack(tmp_path)
    monkeypatch.setitem(RT.STREAM, "instance", s)
    from api.server import app
    return s, svc, psvc, safety, clock, TestClient(app)


def go_ready(svc, clock, symbol="SNDK"):
    svc.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **WATCH_IN))
    clock.advance(60)
    return svc.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **READY_IN))


def ctl(safety, cid):
    return {c["control_id"]: c for c in safety.store.controls()}[cid]


# ================================================================ E engine =====================================================================

def test_E01_default_state_is_trading_allowed():
    state, allowed, codes, headline = E.decide(KillSwitch(), Pause(), dl(baseline(), acct("99000.00")))
    assert (state, allowed, codes, headline) == ("TRADING_ALLOWED", True, [], "")


def test_E02_manual_pause_is_no_new_entries():
    state, allowed, codes, headline = E.decide(KillSwitch(), Pause(active=True, activated_by="user"), dl(baseline(), acct()))
    assert (state, allowed, codes, headline) == ("NO_NEW_ENTRIES", False, [SF.R_PAUSE], "New trades paused by you")


def test_E03_manual_kill_is_halted():
    state, allowed, codes, headline = E.decide(KillSwitch(active=True, activated_by="user", reason="x"), Pause(), dl(baseline(), acct()))
    assert (state, allowed, codes, headline) == ("HALTED", False, [SF.R_KILL], "Emergency stop active")


def test_E04_precedence_kill_over_daily_over_pause_over_allowed():
    daily_hit = dl(baseline("10000.00"), acct("9400.00"))
    assert E.decide(KillSwitch(active=True), Pause(active=True), dl(baseline(), acct()))[0:3] == ("HALTED", False, [SF.R_KILL, SF.R_PAUSE])
    assert E.decide(KillSwitch(), Pause(active=True), daily_hit)[0:3] == ("HALTED", False, [SF.R_DAILY, SF.R_PAUSE])
    assert E.decide(KillSwitch(active=True), Pause(), daily_hit)[0:3] == ("HALTED", False, [SF.R_KILL, SF.R_DAILY])
    assert E.decide(KillSwitch(), Pause(active=True), dl(baseline(), acct()))[0] == "NO_NEW_ENTRIES"
    assert E.decide(KillSwitch(), Pause(), dl(None, acct()))[0:3] == ("NO_NEW_ENTRIES", False, [SF.R_BASELINE_MISSING])       # a degraded control is never healthy
    assert E.decide(KillSwitch(), Pause(), dl(baseline(), acct()))[0:2] == ("TRADING_ALLOWED", True)


def test_E05_valid_daily_baseline_gives_pnl_and_loss_pct():
    d = dl(baseline("10000.00"), acct("9800.00"))
    assert (d.status, d.code, d.starting_equity, d.current_equity, d.daily_pnl, d.daily_loss_pct, d.threshold_pct, d.adjustments) == ("OK", None, "10000.00", "9800.00", "-200.00", "2.00", "5", "NONE_POSSIBLE")
    up = dl(baseline("10000.00"), acct("10300.00"))
    assert (up.status, up.daily_pnl, up.daily_loss_pct) == ("OK", "300.00", "0.00")                                                 # a gain is never a negative loss


def test_E06_daily_loss_below_threshold_is_ok():
    assert dl(baseline("10000.00"), acct("9501.00")).status == "OK" and dl(baseline("10000.00"), acct("9501.00")).daily_loss_pct == "4.99"


def test_E07_exact_threshold_triggers():
    d = dl(baseline("10000.00"), acct("9500.00"))
    assert (d.status, d.code, d.daily_loss_pct) == ("TRIGGERED", SF.R_DAILY, "5.00")


def test_E08_above_threshold_triggers():
    d = dl(baseline("10000.00"), acct("9400.00"))
    assert (d.status, d.daily_loss_pct, d.daily_pnl) == ("TRIGGERED", "6.00", "-600.00") and E.decide(KillSwitch(), Pause(), d)[0] == "HALTED"


def test_E09_daily_loss_latch_persists_when_equity_recovers():
    d = dl(baseline("10000.00"), acct("10200.00"), latched=True, latched_trading_date=TODAY, triggered_at="2026-10-21T15:00:00+00:00", triggered_loss_pct="6.00")
    assert (d.status, d.latched, d.daily_loss_pct, d.triggered_loss_pct, d.triggered_at) == ("TRIGGERED", True, "0.00", "6.00", "2026-10-21T15:00:00+00:00")
    assert E.decide(KillSwitch(), Pause(), d)[0] == "HALTED"


def test_E10_next_day_resets_the_latch_in_the_engine_view():
    d = dl(baseline("10000.00", date="2026-10-22"), acct("10200.00"), latched=True, latched_trading_date=TODAY, triggered_at="x", triggered_loss_pct="6.00", today="2026-10-22")
    assert (d.status, d.latched, d.trading_date) == ("OK", False, "2026-10-22")


def test_E11_manual_kill_survives_a_daily_reset():
    d = dl(baseline("10000.00", date="2026-10-22"), acct("10200.00"), latched=True, latched_trading_date=TODAY, today="2026-10-22")
    assert E.decide(KillSwitch(active=True), Pause(), d)[0:3] == ("HALTED", False, [SF.R_KILL])


def test_E12_invalid_equity_blocks():
    for eq, code in (("0", SF.R_EQUITY_INVALID), ("-5", SF.R_EQUITY_INVALID), ("NaN", SF.R_EQUITY_INVALID), ("abc", SF.R_EQUITY_INVALID), (None, SF.R_EQUITY_NA)):
        d = dl(baseline(), acct(eq))
        assert (d.status, d.code) == ("UNAVAILABLE", code), eq
        assert E.decide(KillSwitch(), Pause(), d)[0:2] == ("NO_NEW_ENTRIES", False)
    bad = dl(baseline("0.00"), acct("100.00"))
    assert (bad.status, bad.code) == ("UNAVAILABLE", SF.R_EQUITY_INVALID)                                                              # starting_equity <= 0 blocks


def test_E13_missing_baseline_never_fakes_a_zero_loss():
    d = dl(None, acct("100000.00"))
    assert (d.status, d.code, d.starting_equity, d.daily_loss_pct, d.daily_pnl) == ("UNAVAILABLE", SF.R_BASELINE_MISSING, None, None, None)
    assert E.decide(KillSwitch(), Pause(), d)[0:2] == ("NO_NEW_ENTRIES", False)
    yesterday = dl(baseline(date="2026-10-20"), acct("100000.00"))
    assert (yesterday.status, yesterday.code) == ("UNAVAILABLE", SF.R_BASELINE_MISSING)                                               # yesterday's baseline is not today's


def test_E14_stale_account_snapshot_blocks():
    stale = dl(baseline(source="ALPACA_PAPER_VIEW", adjustments="UNAVAILABLE"), acct("99000.00", source="ALPACA_PAPER_VIEW", age_s=901.0, adjustments="UNAVAILABLE"))
    assert (stale.status, stale.code) == ("UNAVAILABLE", SF.R_SNAPSHOT_STALE)
    unknown_age = dl(baseline(source="ALPACA_PAPER_VIEW", adjustments="UNAVAILABLE"), acct("99000.00", source="ALPACA_PAPER_VIEW", age_s=None, adjustments="UNAVAILABLE"))
    assert unknown_age.code == SF.R_SNAPSHOT_STALE
    fresh = dl(baseline("100000.00"), acct("99000.00", source="LOCAL_PAPER", age_s=99999.0))
    assert fresh.status == "OK"                                                                                                        # local paper equity has no snapshot age


def test_E15_deterministic_repeatability():
    a, b = dl(baseline("10000.00"), acct("9400.00")), dl(baseline("10000.00"), acct("9400.00"))
    assert a.model_dump() == b.model_dump() and E.decide(KillSwitch(), Pause(), a) == E.decide(KillSwitch(), Pause(), b)
    rows = [c.model_dump() for c in E.controls(KillSwitch(), Pause(), a, baseline("10000.00"))]
    assert rows == [c.model_dump() for c in E.controls(KillSwitch(), Pause(), b, baseline("10000.00"))] and [r["control_id"] for r in rows] == ALL


def test_E16_no_nan_or_infinity():
    assert E.D(float("nan")) is None and E.D(float("inf")) is None and E.D("Infinity") is None and E.D("NaN") is None and E.D("1e3") == Decimal("1000")
    assert dl(baseline(), acct(), thr="inf").code == SF.R_THRESHOLD_INVALID and dl(baseline(), acct(), thr="0").code == SF.R_THRESHOLD_INVALID and dl(baseline(), acct(), thr=None).code == SF.R_THRESHOLD_INVALID
    d = dl(baseline("10000.00"), acct("9400.00"))
    for v in json.loads(json.dumps(d.model_dump())).values():
        assert v not in ("NaN", "Infinity", "-Infinity")
    for key in ("daily_pnl", "daily_loss_pct", "starting_equity", "current_equity"):
        assert Decimal(getattr(d, key)).is_finite()


def test_E17_deposit_withdrawal_uncertainty_is_never_reported_healthy():
    b = baseline("10000.00", source="ALPACA_PAPER_VIEW", adjustments="UNAVAILABLE")
    a = acct("9800.00", source="ALPACA_PAPER_VIEW", age_s=10.0, adjustments="UNAVAILABLE")
    d = dl(b, a)
    assert (d.status, d.code, d.daily_loss_pct, d.adjustments) == ("UNAVAILABLE", SF.R_ADJUSTMENT_NA, "2.00", "UNAVAILABLE")
    assert E.decide(KillSwitch(), Pause(), d)[0:3] == ("NO_NEW_ENTRIES", False, [SF.R_ADJUSTMENT_NA])
    assert dl(b, acct("9400.00", source="ALPACA_PAPER_VIEW", age_s=10.0, adjustments="UNAVAILABLE")).status == "TRIGGERED"             # a raw loss over the limit still halts
    ok = dl(b, a, accept_unadjusted=True)
    assert (ok.status, ok.adjustments) == ("OK", "ACCEPTED_UNADJUSTED")                                                               # only with the explicit operator choice


# ================================================================ S service / persistence =====================================================

def test_S01_migrations_idempotent_and_tables_append_only(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    run_execution_safety_migrations(conn)
    run_execution_safety_migrations(conn)
    assert set(TABLES) <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    st = SafetyStore(db)
    st.commit(IN_SESSION, state=SafetyStateRecord(pause_active=True), events=[{"kind": "PAUSE_ON", "actor": "test"}],
              evaluation={"trigger": "TEST", "state": "NO_NEW_ENTRIES", "allowed_new_entry": False, "reason_codes": [SF.R_PAUSE], "daily_loss": {}, "kill_switch": {}, "no_new_entries": {}, "freshness": {}},
              controls=[ControlEvaluation(control_id="NO_NEW_ENTRIES", result="BLOCK", reason="paused")], new_baseline=baseline())
    rw = sqlite3.connect(db)
    for sql in ("UPDATE daily_risk_baselines SET starting_equity = '1'", "DELETE FROM daily_risk_baselines", "UPDATE execution_safety_events SET kind = 'KILL_ON'", "DELETE FROM execution_safety_events",
                "UPDATE execution_safety_evaluations SET state = 'TRADING_ALLOWED'", "DELETE FROM execution_safety_evaluations", "DELETE FROM execution_safety_controls", "DELETE FROM execution_safety_state",
                f"INSERT OR REPLACE INTO daily_risk_baselines (trading_date, starting_equity, baseline_created_at, source, adjustments) VALUES ('{TODAY}', '1', 'x', 'x', 'x')"):
        with pytest.raises(sqlite3.IntegrityError):
            rw.execute(sql)
    assert st.state().pause_active is True and st.baseline(TODAY).starting_equity == "100000.00" and st.event_count() == 1 and st.evaluation_count() == 1
    assert st.controls()[0]["fire_count"] == 1 and SafetyStore(tmp_path / "missing.db").state() == SafetyStateRecord() and SafetyStore(tmp_path / "missing.db").events() == []


def test_S02_state_survives_restart(stack, tmp_path):
    s, svc, psvc, safety, clock, api = stack
    safety.pause("p")
    clock.advance(1)
    safety.emergency_stop("k")
    s2, svc2, psvc2, safety2, _ = make_stack(tmp_path, clock=FakeClock(clock.now + timedelta(seconds=1)))
    st = safety2.store.state()
    assert st.pause_active and st.kill_active and st.kill_reason == "k" and safety2.evaluate("STARTUP").state == "HALTED"


def test_S03_baseline_is_written_and_survives_restart(stack, tmp_path):
    s, svc, psvc, safety, clock, api = stack
    assert safety.store.baseline(TODAY) is None
    v = safety.evaluate("TEST")
    b = safety.store.baseline(TODAY)
    assert b is not None and b.starting_equity == "100000.00" and b.source == "HYPOTHETICAL_REVIEW" and b.adjustments == "NONE_POSSIBLE" and b.baseline_created_at == v.evaluated_at
    assert [e["kind"] for e in safety.store.events()] == ["BASELINE_CREATED"] and v.daily_loss.status == "OK" and v.state == "TRADING_ALLOWED"
    s2, svc2, psvc2, safety2, _ = make_stack(tmp_path, clock=FakeClock(clock.now + timedelta(seconds=5)))
    assert safety2.store.baseline(TODAY) == b and safety2.evaluate("STARTUP").daily_loss.baseline_created_at == b.baseline_created_at
    assert [e["kind"] for e in safety2.store.events()] == ["BASELINE_CREATED"]                                                       # the restart created nothing new


def test_S04_events_append_only_and_no_duplicate_on_hold(stack):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")
    n = safety.store.event_count()
    for _ in range(3):
        clock.advance(1)
        safety.evaluate("TEST")
    assert safety.store.event_count() == n == 1
    safety.pause("p")
    assert safety.store.event_count() == 2
    clock.advance(1)
    safety.pause("again")                                                                                                             # already paused: no second event
    assert safety.store.event_count() == 2 and [e["kind"] for e in safety.store.events()] == ["PAUSE_ON", "BASELINE_CREATED"]


def test_S05_fire_count_counts_blocking_evaluations(stack):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")
    assert ctl(safety, "GLOBAL_KILL_SWITCH")["fire_count"] == 0 and ctl(safety, "GLOBAL_KILL_SWITCH")["last_fired_at"] is None
    safety.emergency_stop("k")
    assert ctl(safety, "GLOBAL_KILL_SWITCH")["fire_count"] == 1 and ctl(safety, "GLOBAL_KILL_SWITCH")["last_fired_at"] == safety.clock().isoformat(timespec="seconds").replace("+00:00", "+00:00")
    clock.advance(1)
    safety.evaluate("TEST")
    assert ctl(safety, "GLOBAL_KILL_SWITCH")["fire_count"] == 2 and ctl(safety, "GLOBAL_KILL_SWITCH")["last_result"] == "BLOCK"
    assert ctl(safety, "DAILY_LOSS_HALT")["fire_count"] == 0 and ctl(safety, "NO_NEW_ENTRIES")["fire_count"] == 0


def test_S06_evaluation_count_and_bounded_evaluation_rows(stack):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")
    assert safety.store.evaluation_count() == 1 and ctl(safety, "DAILY_LOSS_HALT")["evaluation_count"] == 1
    for _ in range(5):
        clock.advance(1)
        safety.evaluate("TEST")
    assert ctl(safety, "DAILY_LOSS_HALT")["evaluation_count"] == 6 and safety.store.evaluation_count() == 1                          # unchanged within the heartbeat: no new row
    clock.advance(61)
    safety.evaluate("TEST")
    assert safety.store.evaluation_count() == 2                                                                                       # the heartbeat row
    clock.advance(1)
    safety.pause("p")
    assert safety.store.evaluation_count() == 3 and safety.store.last_evaluation()["state"] == "NO_NEW_ENTRIES"                      # a change always persists


def test_S07_database_failure_is_conservative(stack, monkeypatch):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk gone")
    mp = pytest.MonkeyPatch()                                                                                                        # scoped: the fixture's patches stay in place
    mp.setattr(SafetyStore, "commit", boom)
    clock.advance(1)
    v = safety.evaluate("TEST")
    assert (v.state, v.allowed_new_entry, v.halted, v.reason_codes, v.persisted) == ("UNKNOWN", False, True, [SF.R_PERSISTENCE], False) and "disk gone" in v.error
    mp.undo()
    assert [e["kind"] for e in safety.store.events()][0] == "EVALUATOR_ERROR" and all(c["last_error"] and "disk gone" in c["last_error"] for c in safety.store.controls())
    assert api.get("/api/execution-safety").json()["state"] != "UNKNOWN"                                                             # recovered once the store works again
    assert psvc.execution_eligibility("REVIEWABLE", clock())["allowed"] is True


def test_S08_proposal_controls_consume_the_real_gate(stack):
    s, svc, psvc, safety, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    k = next(c for c in v.controls if c["control_id"] == "KILL_SWITCH")
    assert v.effective_status == "REVIEWABLE" and k["result"] == "PASS" and v.execution_eligibility["allowed"] is True and v.execution_eligibility["state"] == "TRADING_ALLOWED"
    clock.advance(1)
    safety.emergency_stop("gate")
    clock.advance(1)
    v2 = psvc.evaluate("SNDK", trigger="TEST")
    k2 = next(c for c in v2.controls if c["control_id"] == "KILL_SWITCH")
    assert v2.effective_status == "BLOCKED" and k2["result"] == "BLOCK" and k2["input"]["source"].startswith("execution safety gate") and "Emergency stop active" in k2["input"]["source"]
    assert v2.execution_eligibility == {**v2.execution_eligibility, "allowed": False, "state": "HALTED"} and "KILL_SWITCH_ACTIVE" in v2.execution_eligibility["reason_codes"]


def test_S09_no_new_entries_keeps_the_proposal_visible_but_blocks_eligibility(stack):
    s, svc, psvc, safety, clock, api = stack
    go_ready(svc, clock)
    clock.advance(1)
    safety.pause("caution")
    clock.advance(1)
    v = psvc.evaluate("SNDK", trigger="TEST")
    n = next(c for c in v.controls if c["control_id"] == "NO_NEW_ENTRIES")
    assert v.effective_status == "REVIEWABLE" and n["result"] == "WARN" and "NO_NEW_ENTRIES_ACTIVE" in [w["code"] for w in v.warnings]
    assert v.execution_eligibility["allowed"] is False and v.execution_eligibility["state"] == "NO_NEW_ENTRIES" and v.execution_eligibility["reason_codes"] == ["NO_NEW_ENTRIES_ACTIVE"]
    assert api.get("/api/proposals/SNDK").json()["execution_eligibility"]["allowed"] is False and ctl(safety, "NO_NEW_ENTRIES")["fire_count"] >= 1


def test_S10_emergency_stop_blocks_until_cleared_explicitly(stack):
    s, svc, psvc, safety, clock, api = stack
    r = api.post("/api/execution-safety/emergency-stop", json={"reason": "now"}).json()
    assert (r["state"], r["allowed_new_entry"], r["halted"], r["kill_switch"]["active"], r["kill_switch"]["activated_by"]) == ("HALTED", False, True, True, "user")
    assert api.post("/api/execution-safety/resume-new-entries").json()["state"] == "HALTED"                                           # Resume never clears a stop
    assert api.post("/api/execution-safety/clear-emergency-stop", json={"reason": "ok"}).json()["state"] == "TRADING_ALLOWED"
    assert [e["kind"] for e in safety.store.events() if e["kind"] != "BASELINE_CREATED"][:2] == ["KILL_OFF", "KILL_ON"]


def test_S11_daily_loss_blocks_through_the_service_and_the_proposal(stack):
    s, svc, psvc, safety, clock, api = stack
    go_ready(svc, clock)
    assert safety.store.baseline(TODAY).starting_equity == "100000.00"
    clock.advance(1)
    v = safety.evaluate("TEST", overrides={"equity": "94000.00"})
    assert (v.state, v.reason_codes, v.daily_loss.status, v.daily_loss.daily_loss_pct, v.daily_loss.latched) == ("HALTED", [SF.R_DAILY], "TRIGGERED", "6.00", True)
    clock.advance(1)
    p = psvc.evaluate("SNDK", trigger="TEST")
    d = next(c for c in p.controls if c["control_id"] == "DAILY_LOSS_HALT")
    assert p.effective_status == "BLOCKED" and d["result"] == "BLOCK" and "6.00%" in d["input"]["source"] and p.execution_eligibility["allowed"] is False


def test_S12_api_reads_and_actions(stack):
    s, svc, psvc, safety, clock, api = stack
    j = api.get("/api/execution-safety").json()
    assert j["state"] == "TRADING_ALLOWED" and j["allowed_new_entry"] is True and j["labels"]["HALTED"] == "Trading stopped" and j["threshold_pct"] == "5.0" and j["baselines"][0]["trading_date"] == TODAY
    assert set(j["controls"][0]) >= {"control_id", "last_evaluated_at", "last_fired_at", "evaluation_count", "fire_count", "last_result", "last_reason", "last_input", "last_error"}
    assert api.get("/api/execution-safety/controls").json()["known_controls"] == ALL and api.get("/api/execution-safety/events").json()["count"] == 1
    assert api.post("/api/execution-safety/pause-new-entries", json={"reason": "x"}).json()["state"] == "NO_NEW_ENTRIES"
    assert api.post("/api/execution-safety/resume-new-entries").json()["state"] == "TRADING_ALLOWED"
    assert api.post("/api/execution-safety/pause-new-entries", json={"side": "buy"}).status_code == 422                                # no stray fields
    for path in ("/api/execution-safety", "/api/execution-safety/events", "/api/execution-safety/controls"):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert api.request(method, path, json={}).status_code == 405, (method, path)
    assert api.get("/api/execution-safety/events?kind=PAUSE_ON").json()["events"][0]["actor"] == "user"


# ================================================================ F forced fire (the real path) =================================================

def test_F01_A_kill_switch_halts_blocks_the_proposal_persists_and_survives_restart(stack, tmp_path):
    s, svc, psvc, safety, clock, api = stack
    go_ready(svc, clock)
    clock.advance(1)
    v = safety.emergency_stop("forced")
    assert v.state == "HALTED" and v.halted and v.reason_codes == [SF.R_KILL]
    clock.advance(1)
    p = psvc.evaluate("SNDK", trigger="TEST")
    assert p.effective_status == "BLOCKED" and next(c for c in p.controls if c["control_id"] == "KILL_SWITCH")["result"] == "BLOCK"
    ev = [e for e in safety.store.events() if e["kind"] == "KILL_ON"]
    assert len(ev) == 1 and ev[0]["actor"] == "user" and ev[0]["reason"] == "forced" and ev[0]["to_state"] == "HALTED"
    s2, svc2, psvc2, safety2, _ = make_stack(tmp_path, clock=FakeClock(clock.now + timedelta(seconds=2)))
    v2 = safety2.evaluate("STARTUP")
    assert v2.state == "HALTED" and v2.kill_switch.active and v2.kill_switch.reason == "forced" and ctl(safety2, "GLOBAL_KILL_SWITCH")["fire_count"] >= 2


def test_F02_B_daily_loss_six_percent_halts_with_evidence(stack):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST", overrides={"equity": "10000.00"})
    assert safety.store.baseline(TODAY).starting_equity == "10000.00"
    clock.advance(1)
    v = safety.evaluate("TEST", overrides={"equity": "9400.00"})
    assert (v.state, v.daily_loss.daily_loss_pct, v.daily_loss.threshold_pct, v.reason_codes, v.reason_text) == ("HALTED", "6.00", "5.0", [SF.R_DAILY], "Daily loss limit reached")
    ev = [e for e in safety.store.events() if e["kind"] == "DAILY_HALT_TRIGGERED"]
    assert len(ev) == 1 and ev[0]["input"]["daily_loss_pct"] == "6.00" and ev[0]["to_state"] == "HALTED"
    assert ctl(safety, "DAILY_LOSS_HALT")["fire_count"] == 1 and ctl(safety, "DAILY_LOSS_HALT")["last_fired_at"] == v.evaluated_at
    clock.advance(1)
    safety.evaluate("TEST", overrides={"equity": "9400.00"})
    assert ctl(safety, "DAILY_LOSS_HALT")["fire_count"] == 2 and safety.store.state().daily_halt_loss_pct == "6.00"


def test_F03_C_no_new_entries_keeps_the_proposal_visible_and_blocks_eligibility(stack):
    s, svc, psvc, safety, clock, api = stack
    go_ready(svc, clock)
    clock.advance(1)
    v = safety.pause("forced")
    assert v.state == "NO_NEW_ENTRIES" and not v.halted
    clock.advance(1)
    p = psvc.evaluate("SNDK", trigger="TEST")
    assert p.effective_status == "REVIEWABLE" and p.final_qty >= 1 and p.execution_eligibility["allowed"] is False
    assert api.get("/api/proposals/SNDK").json()["effective_status"] == "REVIEWABLE" and api.get("/api/proposals/SNDK").json()["execution_eligibility"]["state"] == "NO_NEW_ENTRIES"


def test_F04_D_normal_state_is_trading_allowed(stack):
    s, svc, psvc, safety, clock, api = stack
    v = evaluate_execution_safety("TEST")
    assert (v.state, v.allowed_new_entry, v.halted, v.reason_codes, v.daily_loss.status, v.persisted) == ("TRADING_ALLOWED", True, False, [], "OK", True)
    assert [c.result for c in v.controls] == ["PASS", "PASS", "PASS", "PASS"] and [c.control_id for c in v.controls] == ALL


# ================================================================ D dead control ====================================================================

def test_D01_no_baseline_is_never_silently_replaced_by_current_equity(stack, monkeypatch):
    s, svc, psvc, safety, clock, api = stack
    v = safety.evaluate("TEST", persist=False)                                                                                        # read-only: nothing may write a baseline
    assert safety.store.baseline(TODAY) is None and (v.state, v.allowed_new_entry, v.daily_loss.status, v.daily_loss.code, v.daily_loss.starting_equity, v.daily_loss.daily_loss_pct) == \
        ("NO_NEW_ENTRIES", False, "UNAVAILABLE", SF.R_BASELINE_MISSING, None, None)
    real_commit = SafetyStore.commit

    def no_baseline(self, now, **kw):
        if kw.get("new_baseline") is not None:
            raise sqlite3.OperationalError("baseline write refused")
        return real_commit(self, now, **kw)
    mp = pytest.MonkeyPatch()
    mp.setattr(SafetyStore, "commit", no_baseline)
    v2 = safety.evaluate("TEST")
    assert v2.state == "UNKNOWN" and v2.allowed_new_entry is False and v2.daily_loss.daily_loss_pct is None and safety.store.baseline(TODAY) is None
    mp.undo()
    clock.advance(1)
    safety.evaluate("TEST", overrides={"equity": "100000.00"})
    clock.advance(1)
    v3 = safety.evaluate("TEST", overrides={"equity": "90000.00"})
    assert v3.daily_loss.starting_equity == "100000.00" and v3.daily_loss.daily_loss_pct == "10.00" and v3.state == "HALTED"           # measured against the PERSISTED baseline
    src = source_of(["safety/service.py", "safety/engine.py"])
    assert not re.search(r"starting_equity\s*=\s*(cur|current|equity|acct\.equity)\b", src) and "Baseline(trading_date=today, starting_equity=s(money(cur))" in src   # the only write is the explicit, evented creation


# ================================================================ R restart ===========================================================================

def test_R01_restart_sequence(stack, tmp_path):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")                                                                                                           # 1 baseline
    b = safety.store.baseline(TODAY)
    clock.advance(1)
    safety.pause("p")                                                                                                                 # 2
    n_events = safety.store.event_count()

    def restart(seconds=1):
        nonlocal s, svc, psvc, safety
        s, svc, psvc, safety, _ = make_stack(tmp_path, clock=FakeClock(clock.advance(seconds)))
        return safety.evaluate("STARTUP")
    v = restart()                                                                                                                     # 3
    assert safety.store.baseline(TODAY) == b and v.daily_loss.starting_equity == b.starting_equity                                   # 4
    assert v.state == "NO_NEW_ENTRIES" and v.no_new_entries.active and safety.store.event_count() == n_events                        # 5 (+ no duplicate events)
    clock.advance(1)
    assert safety.resume().state == "TRADING_ALLOWED"                                                                                 # 6
    clock.advance(1)
    assert safety.emergency_stop("k").state == "HALTED"                                                                               # 7
    n_events = safety.store.event_count()
    v = restart()                                                                                                                     # 8
    assert v.state == "HALTED" and v.kill_switch.active and v.kill_switch.reason == "k" and safety.store.event_count() == n_events    # 9
    clock.advance(1)
    assert safety.clear_emergency_stop("done").state == "TRADING_ALLOWED"                                                             # 10
    clock.advance(1)
    v = safety.evaluate("TEST", overrides={"equity": "94000.00"})                                                                     # 11
    assert v.state == "HALTED" and v.daily_loss.latched
    n_events = safety.store.event_count()
    v = restart()                                                                                                                     # 12
    assert v.state == "HALTED" and v.daily_loss.latched and v.reason_codes == [SF.R_DAILY] and safety.store.event_count() == n_events  # 13
    assert [e["kind"] for e in safety.store.events()][::-1] == ["BASELINE_CREATED", "PAUSE_ON", "PAUSE_OFF", "KILL_ON", "KILL_OFF", "DAILY_HALT_TRIGGERED"]


# ================================================================ Y day boundary ===================================================================

def test_Y01_day_boundary_resets_only_the_daily_latch(stack, tmp_path):
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")
    clock.advance(1)
    v = safety.evaluate("TEST", overrides={"equity": "94000.00"})                                                                     # day 1: daily loss triggered
    assert v.state == "HALTED" and v.daily_loss.latched and v.daily_loss.trading_date == TODAY
    clock.advance(1)
    safety.emergency_stop("manual")
    day2 = FakeClock(clock.now + timedelta(days=1))
    s2, svc2, psvc2, safety2, _ = make_stack(tmp_path, clock=day2)
    v2 = safety2.evaluate("TEST", overrides={"equity": "97000.00"})                                                                   # day 2
    assert v2.daily_loss.trading_date == "2026-10-22" and v2.daily_loss.latched is False and v2.daily_loss.starting_equity == "97000.00" and v2.daily_loss.status == "OK"
    assert safety2.store.baseline("2026-10-22").starting_equity == "97000.00" and safety2.store.baseline(TODAY).starting_equity == "100000.00"
    assert v2.state == "HALTED" and v2.reason_codes == [SF.R_KILL] and v2.kill_switch.active                                          # the manual kill survives the day boundary
    kinds = [e["kind"] for e in safety2.store.events()]
    assert kinds[:2] == ["BASELINE_CREATED", "DAILY_RESET"] and safety2.store.state().daily_halt_active is False
    day2.advance(1)
    assert safety2.clear_emergency_stop().state == "TRADING_ALLOWED"


# ================================================================ X no execution ==================================================================

ALLOWED_FOREIGN = ("proposals.account", "fit.readonly", "fit", "config", "database.execution_safety_migrations", "database", "api.routes.trader_view", "api.validation", "api")


def test_X01_no_order_writer_broker_client_model_or_research_import():
    forbidden = tuple(p for p in FORBIDDEN_IMPORTS if p not in ("forward",)) + ("paper", "alpaca", "ppe", "demand", "evidence", "research_flow", "rotation", "setups.service", "setups.store")
    for rel in SAFETY_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == p or name.startswith(p + ".") for p in forbidden), (rel, name)
        assert not re.search(r"TradingClient|TradingStream|messages\.create|get_provider|/v2/orders|alpaca_order_writer|\.submit\(|place_order|cancel_order|_post_once|confirm\(|preview\(|"
                             r"requests\.|urlopen|\beval\(|\bexec\(|__import__|os\.system|subprocess|build_packet|get_data_client|anthropic|robin_stocks|rh_gateway|create_order", text), rel


def test_X02_no_alpaca_order_no_broker_submit_from_any_safety_path(stack, monkeypatch):
    import paper.alpaca_order_writer as W
    import paper.alpaca_orders as AO

    def boom(*a, **k):
        raise AssertionError("the broker writer was reached from the safety layer")
    for mod, name in ((W, "submit"), (AO, "preview"), (AO, "confirm"), (AO, "retry")):
        monkeypatch.setattr(mod, name, boom)
    s, svc, psvc, safety, clock, api = stack
    for path in ("pause-new-entries", "resume-new-entries", "emergency-stop", "clear-emergency-stop"):
        assert api.post(f"/api/execution-safety/{path}").status_code == 200
    clock.advance(1)
    safety.evaluate("TEST", overrides={"equity": "90000.00"})
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        ws.send_json({"type": "safety_eval", "account": {"equity": "100.00"}})
        assert ws.receive_json()["type"] == "safety"
    from paper import alpaca_order_store as AOS
    assert not AOS.OrderStore(safety.store.path).exists()


def test_X03_no_robinhood_write_or_read(stack, monkeypatch):
    import api.routes.portfolio as PRT
    calls = []
    monkeypatch.setattr(PRT, "provider_factory", lambda *a, **k: calls.append("factory") or (_ for _ in ()).throw(RuntimeError("robinhood touched")))
    s, svc, psvc, safety, clock, api = stack
    safety.evaluate("TEST")
    safety.emergency_stop("x")
    assert calls == [] and not re.search(r"(?i)robinhood|robin_stocks|rh_gateway", source_of(SAFETY_FILES))


def test_X04_no_execution_adapter_and_exact_routes(stack):
    s, svc, psvc, safety, clock, api = stack
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/execution-safety")}
    assert mine == {"/api/execution-safety": {"get"}, "/api/execution-safety/events": {"get"}, "/api/execution-safety/controls": {"get"}, "/api/execution-safety/pause-new-entries": {"post"},
                    "/api/execution-safety/resume-new-entries": {"post"}, "/api/execution-safety/emergency-stop": {"post"}, "/api/execution-safety/clear-emergency-stop": {"post"}}
    for p in mine:
        assert not re.search(r"(?i)order|execute\b|broker|place|cancel|submit|buy|sell|preview|trade|confirm|liquidat", p), p
    assert not re.search(r"ExecutionPreviewAdapter|NullExecutionPreviewAdapter|\.preview\(", source_of(SAFETY_FILES))
    assert s.config_payload()["execution_preview"] == "NOT_CONNECTED"


def test_X05_no_stage_46b_confirm_and_no_auto_trade_loop():
    src = source_of(SAFETY_FILES)
    assert not re.search(r"_post_once|confirm\(|alpaca_orders|Thread\(|while True|asyncio\.sleep", src)
    assert "class SetupScheduler" not in src and not re.search(r"(?i)auto.?trad", strip_code(src))


def test_X06_no_model_call(stack, monkeypatch):
    import anthropic
    s, svc, psvc, safety, clock, api = stack

    def boom(self, *a, **k):
        raise AssertionError("a model client was constructed by the safety layer")
    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    assert safety.evaluate("TEST").state == "TRADING_ALLOWED" and safety.pause().state == "NO_NEW_ENTRIES"
    assert "anthropic" not in source_of(SAFETY_FILES) and "messages.create" not in source_of(SAFETY_FILES)


def test_X07_alerts_and_transitions_cannot_fire_an_order_or_change_safety_state(stack):
    s, svc, psvc, safety, clock, api = stack
    assert [fn.__name__ for fn in svc.listeners] == ["_setup_event", "on_setup_event"] and safety.listeners == []
    before = safety.store.state().model_dump(exclude={"updated_at"})
    go_ready(svc, clock)                                                                                                              # a READY alert fired
    assert safety.store.state().model_dump(exclude={"updated_at"}) == before and [e["kind"] for e in safety.store.events()] == ["BASELINE_CREATED"]
    src = inspect.getsource(ExecutionSafetyService)
    assert not re.search(r"(?i)\border\b|broker|execute\b|preview\(|submit|confirm\(|liquidat", src)


def test_X08_prepare_buy_sell_remain_disabled(stack):
    s, svc, psvc, safety, clock, api = stack
    acts = {a.id: a for a in s.state("SNDK", with_candles=False).actions}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False and acts["prepare_buy"].status == "PREVIEW_NOT_CONNECTED"
    assert "safety" not in JS.split("function renderActions")[1].split("function renderStatusLine")[0]
    assert "safety" in MESSAGE_TYPES and "safety" not in SYMBOL_BOUND_TYPES


def test_X09_forced_evaluation_is_refused_outside_test_processes(stack, monkeypatch):
    s, svc, psvc, safety, clock, api = stack
    prod = TerminalStream(SimulatedMarket(seed=1, start_time=T0), FakePositions(), FakeResearch(), watchlist_fn=lambda: [], run_loop=False, now_fn=lambda: T0,
                          setup_service=svc, proposal_service=psvc, safety_service=safety)
    RT.STREAM["instance"] = prod
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        ws.send_json({"type": "safety_eval", "account": {"equity": "1.00"}})
        assert ws.receive_json()["code"] == "NOT_ALLOWED"
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    assert startup_if_enabled() is None                                                                                               # no startup evaluation in a test process


# ================================================================ U frontend =========================================================================

def test_U01_node_tests_of_the_risk_controls_markup():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "trader_safety.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 14\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


def test_U02_risk_controls_panel_wiring():
    assert JS.index("<h3>Risk Controls</h3>") < JS.index("<h3>Position</h3>") and 'window.RiskControls.mount("tv-safety")' in JS
    assert "safetyHtml" in RENDER and "safetyModel" in RENDER and 'data-safety-state="' in RENDER and "data-safety-details" in RENDER and "tv-prop-exec" in RENDER
    assert '<script src="risk_controls.js"></script>' in HTML and HTML.index("trade_review.js") < HTML.index("risk_controls.js")
    assert ".tv-panel-safety" in CSS and ".tv-safe-state.ok" in CSS and ".tv-safe-state.paused" in CSS and ".tv-safe-state.stopped" in CSS
    assert min(int(x) for x in re.findall(r"font-size:\s*(\d+)px", CSS)) >= 11


def test_U03_beginner_wording_and_no_trade_wording():
    section = RENDER.split("Risk controls (Stage 5.9.1")[1].split("status strip")[0]
    for label in ("Trading allowed", "New trades paused", "Trading stopped", "Pause New Trades", "Resume", "Emergency Stop", "Clear Emergency Stop", "View details"):
        assert label in section, label
    code = re.sub(r"//.*", "", section)
    assert not TRADE_WORDS.search(code) and not re.search(r"(?i)probabilit|expected return|target price", code)
    risk = re.sub(r"//.*", "", RISK_JS)
    assert not TRADE_WORDS.search(risk) and re.findall(r"method: \"(POST|PUT|PATCH|DELETE)\"", RISK_JS) == ["POST"]
    assert sorted(re.findall(r"act\(\"(/api/execution-safety/[a-z-]+)\"", RISK_JS)) == ["/api/execution-safety/clear-emergency-stop", "/api/execution-safety/emergency-stop",
                                                                                            "/api/execution-safety/pause-new-entries", "/api/execution-safety/resume-new-entries"]
    assert not re.search(r"(?i)method:\s*[\"'](POST|PUT|PATCH|DELETE)", JS)
    for text in list(SF.REASON_TEXT.values()) + list(SF.STATE_LABELS.values()) + [SF.DISCLAIMER]:
        assert not TRADE_WORDS.search(text), text


def test_U04_harness_flow_registered_after_trade_review():
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert "risk_controls" in names and names.index("risk_controls") == names.index("trade_review") + 1 and names.index("risk_controls") < names.index("automation")
    assert names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]


def test_U05_safety_view_field_names_name_no_side():
    for model in (SafetyView, DailyLoss, KillSwitch, Pause, AccountEquity, Baseline, SafetyStateRecord, ControlEvaluation):
        for k in model.model_fields:
            assert not re.search(r"(?i)\b(buy|sell|recommend\w*|probab\w*|expected|target|signal|execute|liquidate)\b", k.replace("_", " ")), (model.__name__, k)
    assert set(SF.STATES) == {"TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN"} and SF.STATE_LABELS["NO_NEW_ENTRIES"] == "New trades paused"


# ================================================================ B base pins =========================================================================

def test_B01_prior_stage_files_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    pinned = [p.relative_to(ROOT).as_posix() for p in (ROOT / "setups").glob("*.py")] + [p.relative_to(ROOT).as_posix() for p in (ROOT / "ppe").glob("*.py")] + \
             [p.relative_to(ROOT).as_posix() for p in (ROOT / "demand").glob("*.py")] + \
             ["proposals/store.py", "trader_view/ppe_provider.py", "trader_view/demand_provider.py", "trader_view/adapters.py", "trader_view/hub.py", "trader_view/positions.py",
              "trader_view/research.py", "trader_view/market_real.py", "trader_view/simulated.py", "trader_view/models.py", "evidence/engine.py", "evidence/rules.py", "evidence/store.py",
              "paper/alpaca_order_writer.py", "paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_store.py", "paper/alpaca_order_reads.py", "paper/alpaca_readonly.py",
              "paper/alpaca_view.py", "paper/execution.py", "paper/portfolio.py", "portfolio/provider.py", "database/migrations.py", "database/setup_state_migrations.py",
              "database/proposal_migrations.py", "api/routes/setups.py", "api/routes/proposals.py", "forward/automation.py", "analysis/position_sizing.py", "DESIGN_56_PPE_ENGINE.md",
              "DESIGN_57_MARKET_DEMAND.md", "DESIGN_58_SETUP_STATE_ALERTS.md", "DESIGN_59_TRADE_PROPOSAL_REVIEW.md", "tests/test_evidence_54.py", "tests/test_setups_58.py",
              "tests/frontend/trader_core.test.cjs", "tests/frontend/trader_demand.test.cjs", "tests/frontend/trader_render.test.cjs", "tests/frontend/trader_setup.test.cjs",
              "tests/frontend/trader_proposal.test.cjs", "frontend/trade_review.js", "frontend/setup_alerts.js"]
    for rel in pinned:
        assert ledger_bytes(rel, (ROOT / rel).read_bytes().replace(b"\r\n", b"\n")) == blob(rel), f"{rel} differs from {BASE}"
    base_cfg = blob("config.py").decode("utf-8")
    now_cfg = (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert now_cfg.startswith(base_cfg.rstrip("\n")) and "EXECUTION_SAFETY_STARTUP_POLICY" in now_cfg and config.MAX_DAILY_LOSS_PERCENT == 3.0
