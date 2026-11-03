"""
Wave 2 PR A - X2, the early stop consumed per incident (WAVE2_REMEDIATION_DESIGN.md section 3.7, 3.12.1; WAVE2_TEST_AND_ACCEPTANCE_PLAN.md
section 3.2, T-E01 ... T-E25).

The real Stage 6.1 observer over the real executor, refresher, cash-flow ledger and safety gate on the simulated PAPER broker (the Stage 6.1 rig of
tests/test_forward_validation_61.py). An arm is a PAPER_AUTO mode event of a named actor (ExecutionStore.set_mode, as enable_auto writes it). No
network, no credential, no real order.
"""
import ast
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_cash_flow_602 import cash_config  # noqa: F401 - the autouse config fixture of the shared rig
from test_forward_validation_61 import obs, rig, started, trade_one
import wave2_pra_support as S
from execution.models import ControlEvaluation, Finding, Intent
from forward_validation import attention_signature as SIG
from forward_validation import service as FVS
from forward_validation.service import LIVE_HOST_MARK, ForwardValidationService

ROOT = Path(__file__).resolve().parents[1]
REFUSAL = f"BrokerError: refused - only the {LIVE_HOST_MARK} (simulated)"


# ---------------------------------------------------------------- the conditions ---------------------------------------------------------
def armed(tmp_path, monkeypatch):
    """The rig with one open paper trade, Paper Auto armed (by the test actor), no critical condition yet."""
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    trade_one(r)
    assert r.x.mode()["mode"] == "PAPER_AUTO" and obs(r)["early_stop"] is None
    return r


def rec(r, kind="MULTIPLE_ORDERS", tag=None):
    r.x._finding(r.clock.now, "TEST", Finding(kind=kind, symbol="AMD", blocking=True, detail={"tag": tag or uuid.uuid4().hex}))


def dup_order(r, n):
    entry = r.x.store.intents(intent_type="ENTRY")[0]
    stamp = r.clock.now.isoformat(timespec="seconds")
    r.x.store.record_order({"id": f"11111111-2222-4333-8444-{n:012d}", "client_order_id": entry.client_order_id, "symbol": entry.symbol, "side": "buy", "qty": str(entry.qty),
                            "status": "accepted", "filled_qty": 0, "filled_avg_price": None, "submitted_at": stamp, "updated_at": stamp, "raw": {}}, entry.execution_id, r.clock.now, "test")


def live_row(r):
    r.x.store.add_run(uuid.uuid4().hex, r.clock.now, r.clock.now, "TEST", "PAPER_AUTO", "ERROR", {}, REFUSAL, "test")


def ctrl_touch(r, error):
    r.x.store.touch_controls([ControlEvaluation(control_id="PAPER_ENVIRONMENT", result="UNKNOWN", reason="test", input={})], r.clock.now, error=error)


def gate_violation(r, n=1):
    """An ENTRY intent submitted while the latest persisted safety evaluation did not allow new entries (the T-A03 evidence shape)."""
    at = r.clock.now
    before = (at - timedelta(seconds=1)).isoformat(timespec="seconds")
    with sqlite3.connect(str(r.db)) as conn:
        conn.execute("INSERT INTO execution_safety_evaluations (evaluation_id, evaluated_at, trigger, state, allowed_new_entry, reason_codes_json, daily_loss_json, kill_json, pause_json, "
                     "freshness_json, input_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, before, "TEST", "HALTED", 0, '["EMERGENCY_STOP"]', "{}", "{}", "{}", "{}", "{}"))
    xid = f"{n:032x}"
    stamp = at.isoformat(timespec="seconds")
    r.x.store.insert_intent(Intent(execution_id=xid, intent_type="ENTRY", symbol="AMD", side="buy", qty=1, client_order_id="stockagent-entry-" + xid, created_at=stamp, updated_at=stamp,
                                   attempt_count=1, submitted_at=stamp, state="REJECTED"))
    r.clock.advance(2)


def attention_position(r, pos, minutes_ago=0):
    r.x.store.update_position(pos.position_id, r.clock.now - timedelta(minutes=minutes_ago), {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": "QTY_MISMATCH",
                                                                                              "detail": {}}, state="ATTENTION", attention_reason="QTY_MISMATCH")


def closed_sessions_after(r, n=2):
    """Two closed sessions that started after every ATTENTION entry: the persistence rule of attention() is met."""
    base = r.clock.now + timedelta(days=1)
    for k in range(n):
        d = (base + timedelta(days=k)).date().isoformat()
        r.fv.store.open_session(d, base + timedelta(days=k), "other-owner", in_session=True)
        r.fv.store.update_session(d, base + timedelta(days=k), session_status="HEALTHY", ended_at=(base + timedelta(days=k)).isoformat(timespec="seconds"))


def arm_as(r, actor):
    r.clock.advance(1)
    r.x.store.set_mode("PAPER_AUTO", r.clock.now, actor, f"armed by {actor}", {})


def mode(r):
    return r.x.mode()["mode"]


def stops(r):
    return len(r.fv.store.events(kind="EARLY_STOP", limit=1000))


def tick(r, advance=60):
    return obs(r, advance)["early_stop"]


# ================================================================ T-E01 ... T-E19 ==========================================================

def test_TE01_a_condition_seen_in_monitor_only_still_stops_the_first_arm(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    r.x.disable_auto(actor="user", now=r.clock.now)
    rec(r)
    assert tick(r) == {"critical": ["DUPLICATE_BROKER_ORDER"], "stopped": False} and stops(r) == 0     # nothing consumed in MONITOR_ONLY (PF-12)
    arm_as(r, "user")
    assert tick(r)["stopped"] is True and mode(r) == "MONITOR_ONLY"                                     # baseline: never stopped


def test_TE02_after_a_startup_demotion_the_arm_is_stopped(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    r.x.store.set_mode("MONITOR_ONLY", r.clock.now, "startup", "process start: Monitor only", {})
    r.fv = ForwardValidationService(stream_fn=r.fv.stream_fn, store=r.fv.store, clock=r.clock)        # a new observer process
    assert tick(r)["stopped"] is False
    arm_as(r, "autorun_scheduled_arm")
    assert tick(r)["stopped"] is True and mode(r) == "MONITOR_ONLY"


def test_TE03_already_armed_at_the_first_tick_stops(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    assert tick(r) == {"critical": ["DUPLICATE_BROKER_ORDER"], "stopped": True} and mode(r) == "MONITOR_ONLY"


def test_TE04_a_persons_rearm_after_a_stop_is_not_stopped_again_for_those_elements(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False and mode(r) == "PAPER_AUTO" and stops(r) == 1


def test_TE05_a_stop_that_failed_before_the_mode_changed_is_retried(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    real = r.x.disable_auto
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return real(**kw)
    monkeypatch.setattr(r.x, "disable_auto", flaky)
    assert tick(r)["stopped"] is False and mode(r) == "PAPER_AUTO"                                       # W2-N30: baseline never retried
    assert tick(r)["stopped"] is True and mode(r) == "MONITOR_ONLY" and len(calls) == 2


def test_TE06_attention_raised_is_written_once_per_process_per_code(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    for _ in range(3):
        tick(r)
    assert [e["code"] for e in r.fv.store.events(kind="ATTENTION_RAISED")] == ["DUPLICATE_BROKER_ORDER"]


def test_TE07_nothing_is_consumed_while_the_early_stop_is_disabled(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    r.fv.early_stop_enabled = False
    assert tick(r)["stopped"] is False and tick(r)["stopped"] is False and mode(r) == "PAPER_AUTO"
    r.fv.early_stop_enabled = True
    assert tick(r)["stopped"] is True and stops(r) == 1


def test_TE08_a_new_element_after_an_acknowledgement_stops(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r, tag="first")
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False
    rec(r, tag="second")                                                                                  # baseline: stopped False (R6)
    assert tick(r)["stopped"] is True and stops(r) == 2


def test_TE09_a_historical_element_stops_the_next_days_scheduled_arm(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    r.x.disable_auto(actor="autorun_policy", now=r.clock.now)
    tick(r)
    r.clock.advance(86400)
    arm_as(r, "autorun_scheduled_arm")
    assert tick(r)["stopped"] is True                                                                    # declared: no person acknowledged it


@pytest.mark.parametrize("actor", ["autorun_policy", "aapl-smoke", "autorun_scheduled_arm"])
def test_TE10_TE14_an_arm_by_policy_smoke_or_schedule_never_acknowledges(tmp_path, monkeypatch, actor):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    assert tick(r)["stopped"] is True
    arm_as(r, actor)
    assert tick(r)["stopped"] is True and stops(r) == 2 and r.fv._acked == {}


def test_TE11_a_persons_rearm_acknowledges_exactly_the_stopped_elements(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r, tag="a")
    tick(r)
    at_stop = r.fv._stopped["DUPLICATE_BROKER_ORDER"][0]
    arm_as(r, "user")
    tick(r)
    assert r.fv._acked == {"DUPLICATE_BROKER_ORDER": at_stop} and len(at_stop) == 1


def test_TE12_the_acknowledgement_holds_for_the_process(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    tick(r)
    arm_as(r, "user")
    tick(r)
    r.x.disable_auto(actor="autorun_policy", now=r.clock.now)
    r.clock.advance(86400)
    arm_as(r, "autorun_scheduled_arm")
    assert tick(r)["stopped"] is False and mode(r) == "PAPER_AUTO"


def test_TE13p_pr_a_has_no_run_acknowledgement_path_so_an_element_on_record_stops(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    assert not r.fv.store.query("SELECT name FROM sqlite_master WHERE name IN ('forward_runs', 'forward_run_events')")
    rec(r)
    assert tick(r)["stopped"] is True
    src = (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8") + (ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8")
    assert "forward_runs" not in src and "run_ack" not in src


def test_TE15_a_persons_disarm_never_acknowledges(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    tick(r)
    r.clock.advance(1)
    r.x.store.set_mode("MONITOR_ONLY", r.clock.now, "user", "a person disarms", {})                     # F-07: to_mode MONITOR_ONLY
    tick(r)
    arm_as(r, "autorun_scheduled_arm")
    assert tick(r)["stopped"] is True and r.fv._acked == {}


def test_TE16a_duplicate_sources_beyond_the_first_row(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    dup_order(r, 1)
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False
    rec(r, kind="UNKNOWN_INTERNAL_ORDER")                                                                # (i)
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False
    dup_order(r, 2)                                                                                       # (ii) a third order on the duplicated client id
    assert tick(r)["stopped"] is True and stops(r) == 3


def test_TE16b_live_host_rows_and_the_mutable_control_row(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    live_row(r)
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False
    live_row(r)                                                                                           # (i) a new refusal row
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    tick(r)
    r2 = armed(tmp_path / "ctrl", monkeypatch)
    ctrl_touch(r2, REFUSAL)
    assert tick(r2)["stopped"] is True
    arm_as(r2, "user")
    assert tick(r2)["stopped"] is False
    ctrl_touch(r2, None)                                                                                  # (ii) a benign touch clears the mark: no element, no stop
    assert tick(r2) is None and mode(r2) == "PAPER_AUTO"
    ctrl_touch(r2, REFUSAL)                                                                               # (iii) a new refusal: a new evaluation_count
    assert tick(r2)["stopped"] is True
    r3 = armed(tmp_path / "same", monkeypatch)                                                            # (iv) everything in ONE second: updated_at cannot tell
    now = r3.clock.now
    es = lambda: r3.fv._early_stop(now, r3.fv.attention(now), {"trading_date": now.date().isoformat()})  # noqa: E731
    ctrl_touch(r3, REFUSAL)
    assert es()["stopped"] is True
    r3.x.store.set_mode("PAPER_AUTO", now, "user", "a person re-arms in the same second", {})
    assert es()["stopped"] is False
    ctrl_touch(r3, REFUSAL)                                                                               # same updated_at, a new evaluation_count
    assert r3.fv.store.query("SELECT updated_at FROM execution_controls WHERE control_id = 'PAPER_ENVIRONMENT'")[0]["updated_at"] == now.isoformat(timespec="seconds")
    assert es()["stopped"] is True


def test_TE16c_a_second_gate_violation_after_an_acknowledgement_stops(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    gate_violation(r, 1)
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False
    gate_violation(r, 2)
    assert tick(r)["stopped"] is True


def test_TE16d_a_persistent_mismatch_stops_once_and_again_only_on_reentry(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    pos = r.x.store.open_position("SNDK")
    attention_position(r, pos)
    closed_sessions_after(r)
    o = obs(r, 60)
    assert "PERSISTENT_RECONCILIATION_MISMATCH" in o["attention"] and o["early_stop"]["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False                                                                   # (i) still in ATTENTION: no repeated stop
    p = r.x.store.position(pos.position_id)
    r.x.store.update_position(p.position_id, r.clock.now, {"kind": "ATTENTION_CLEARED", "from_state": "ATTENTION", "to_state": "OPEN", "reason": "test", "detail": {}},
                              state="OPEN", attention_reason=None)
    tick(r)
    attention_position(r, r.x.store.position(pos.position_id), minutes_ago=0)                             # (ii) re-entered: a new to-ATTENTION event (the rule keeps its first entry)
    assert tick(r)["stopped"] is True


def test_TE16e_a_fill_correction_on_an_attention_position_is_not_a_new_incident(tmp_path, monkeypatch):
    """Checkpoint 1 K-10: a price-only fill correction writes FILL_UPDATED with to_state ATTENTION; it is not an ENTRY into ATTENTION, so the
    acknowledged PRM element stays the same and no new stop follows."""
    from decimal import Decimal
    r = armed(tmp_path, monkeypatch)
    pos = r.x.store.open_position("SNDK")
    attention_position(r, pos)
    closed_sessions_after(r)
    assert tick(r)["stopped"] is True
    arm_as(r, "user")
    assert tick(r)["stopped"] is False
    entry = r.x.store.intent(pos.execution_id)
    view = {"id": entry.broker_order_id, "client_order_id": entry.client_order_id, "symbol": entry.symbol, "side": "buy", "qty": str(entry.qty), "status": "filled",
            "filled_qty": entry.filled_qty, "filled_avg_price": str(Decimal(entry.filled_avg_price) + Decimal("0.01")), "raw": {}}
    r.x._apply_order(entry, view, r.clock.now, "monitor")                                  # the broker corrects the entry's average price
    ev = r.x.store.position_events(pos.position_id)[-1]
    assert (ev["kind"], ev["from_state"], ev["to_state"]) == ("FILL_UPDATED", "ATTENTION", "ATTENTION")
    assert tick(r)["stopped"] is False and mode(r) == "PAPER_AUTO"


def test_TE16f_a_refusal_row_in_each_of_the_four_live_host_tables_is_its_own_element_and_stops(tmp_path, monkeypatch):
    """Checkpoint 2 L-02: sigma reads all four append-only LIVE_HOST sources (account and cash-flow refresh attempts, reconciliation details, executor
    runs). Each new marked row is a new element: it stops even after a person acknowledged the earlier ones."""
    r = armed(tmp_path, monkeypatch)
    stamp = lambda: r.clock.now.isoformat(timespec="seconds")  # noqa: E731

    def account():
        with sqlite3.connect(str(r.db)) as c:
            c.execute("INSERT INTO account_refresh_attempts (attempt_id, started_at, finished_at, trigger, result, error_text) VALUES (?,?,?,?,?,?)",
                      (uuid.uuid4().hex, stamp(), stamp(), "TEST", "FAILED", REFUSAL))

    def cash():
        with sqlite3.connect(str(r.db)) as c:
            c.execute("INSERT INTO cash_flow_refresh_attempts (attempt_id, started_at, finished_at, trigger, result, error_text) VALUES (?,?,?,?,?,?)",
                      (uuid.uuid4().hex, stamp(), stamp(), "TEST", "FAILED", REFUSAL))

    def recon():
        r.x._finding(r.clock.now, "TEST", Finding(kind="BROKER_UNAVAILABLE", symbol=None, blocking=False, detail={"error": REFUSAL, "tag": uuid.uuid4().hex}))
    for table, make in (("account_refresh_attempts", account), ("cash_flow_refresh_attempts", cash), ("execution_reconciliations", recon), ("execution_runs", lambda: live_row(r))):
        make()
        el = SIG.signature(r.fv.store, "LIVE_HOST_ATTEMPT")
        assert any(e[0] == "ROW" and e[1] == table for e in el), (table, el)
        assert tick(r)["stopped"] is True, table                                                # a new element in THIS table stops
        arm_as(r, "user")
        assert tick(r)["stopped"] is False, table                                               # acknowledged: no repeated stop
    assert set(SIG.LIVE_HOST_TABLES) == {("account_refresh_attempts", "error_text"), ("cash_flow_refresh_attempts", "error_text"),
                                         ("execution_reconciliations", "detail_json"), ("execution_runs", "error")}


def test_TE18b_iv_v_a_failed_mode_precheck_or_event_write_never_records_a_stop_or_skips_one(tmp_path, monkeypatch):
    """Checkpoint 2 L-03 / L-04. (iv) The pre-check mode read fails while Monitor only: the stop is attempted but NOT recorded, so a person's next
    arm is stopped (no acknowledgement of a stop that never happened). (v) The announcement write fails: the same tick still stops."""
    r = armed(tmp_path, monkeypatch)
    r.x.disable_auto(actor="user", now=r.clock.now)
    rec(r)
    real = r.fv.store.query

    def no_precheck(sql, args=()):
        if sql == "SELECT mode FROM execution_mode WHERE id = 1":
            raise sqlite3.OperationalError("database is locked")
        return real(sql, args)
    monkeypatch.setattr(r.fv.store, "query", no_precheck)
    assert tick(r)["stopped"] is False and r.fv._stopped == {}                                  # (iv) unknown pre-check: nothing recorded
    monkeypatch.setattr(r.fv.store, "query", real)
    arm_as(r, "user")
    assert tick(r)["stopped"] is True and mode(r) == "MONITOR_ONLY"                             # so the arm is stopped, not silently acknowledged
    r2 = armed(tmp_path / "announce", monkeypatch)
    rec(r2)
    real_add = r2.fv.store.add_event

    def no_announce(now, kind, *a, **k):
        if kind == "ATTENTION_RAISED":
            raise sqlite3.OperationalError("database is locked")
        return real_add(now, kind, *a, **k)
    monkeypatch.setattr(r2.fv.store, "add_event", no_announce)
    o = obs(r2, 60)
    assert o["result"] == "OK" and o["early_stop"]["stopped"] is True and mode(r2) == "MONITOR_ONLY"   # (v) the same tick, not the next one
    assert "DUPLICATE_BROKER_ORDER" not in r2.fv._stopped_for                                    # announced again once the write works


def test_TE17_a_stop_whose_status_read_raises_is_confirmed_by_the_persisted_mode(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    monkeypatch.setattr(r.x, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("status failed after set_mode")))
    assert tick(r)["stopped"] is True and mode(r) == "MONITOR_ONLY"
    ev = r.fv.store.events(kind="EARLY_STOP")
    assert ev[0]["detail"]["stopped"] is True and "DUPLICATE_BROKER_ORDER" in r.fv._stopped
    tick(r)
    tick(r)
    assert stops(r) == 1                                                                                  # no retry loop


def test_TE18_the_signature_is_coherent_with_attention_for_all_four_codes(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    r.fv.early_stop_enabled = False
    corpus = [("DUPLICATE_BROKER_ORDER", lambda: rec(r)), ("DUPLICATE_BROKER_ORDER", lambda: dup_order(r, 7)), ("LIVE_HOST_ATTEMPT", lambda: live_row(r)),
              ("LIVE_HOST_ATTEMPT", lambda: ctrl_touch(r, REFUSAL)), ("SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", lambda: gate_violation(r, 9))]
    for code in FVS.CRITICAL_CODES:
        assert SIG.signature(r.fv.store, code) == frozenset()
    for code, make in corpus:
        make()
        raised = {a["code"] for a in r.fv.attention(r.clock.now) if a["critical"]}
        for c in FVS.CRITICAL_CODES:
            assert (c in raised) == bool(SIG.signature(r.fv.store, c)), (code, c)
    # PRM: a persistent ATTENTION position next to a non-persistent one (G-06)
    q = make_two_attention(tmp_path / "prm", monkeypatch)
    raised = {a["code"] for a in q.fv.attention(q.clock.now) if a["critical"]}
    prm = SIG.signature(q.fv.store, "PERSISTENT_RECONCILIATION_MISMATCH")
    assert "PERSISTENT_RECONCILIATION_MISMATCH" in raised and {e[1] for e in prm} == {q.old.position_id}
    # a forced empty signature: UNSIGNED, which stops once and can be acknowledged
    r3 = armed(tmp_path / "unsigned", monkeypatch)
    rec(r3)
    monkeypatch.setattr(FVS, "signature", lambda store, code: frozenset())
    assert tick(r3)["stopped"] is True and r3.fv._stopped["DUPLICATE_BROKER_ORDER"][0] == frozenset({("UNSIGNED", "DUPLICATE_BROKER_ORDER")})
    arm_as(r3, "user")
    assert tick(r3)["stopped"] is False


def make_two_attention(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    trade_one(r)
    old = r.x.store.open_position("SNDK")
    stamp = r.clock.now.isoformat(timespec="seconds")
    it = r.x.store.insert_intent(Intent(execution_id="9" * 32, intent_type="ENTRY", symbol="NVDA", side="buy", qty=1, client_order_id="stockagent-entry-" + "9" * 32, created_at=stamp,
                                        updated_at=stamp, attempt_count=1, submitted_at=stamp, state="SUBMITTED", invalidation_price="40.00"))
    r.x._apply_order(it, S.order(S.OID(77), it.client_order_id, "NVDA", "buy", 1, "filled", 1, "50.00"), r.clock.now, "monitor")   # a second open position, no executor cycle after
    new = r.x.store.open_position("NVDA")
    assert new is not None
    attention_position(r, old)
    closed_sessions_after(r)
    later = r.clock.now + timedelta(days=30)                                                              # entered ATTENTION after both sessions started: not persistent
    r.x.store.update_position(new.position_id, later, {"kind": "ATTENTION", "from_state": new.state, "to_state": "ATTENTION", "reason": "QTY_MISMATCH", "detail": {}},
                              state="ATTENTION", attention_reason="QTY_MISMATCH")
    with sqlite3.connect(str(r.db)) as conn:
        conn.execute("UPDATE paper_positions SET updated_at = ? WHERE position_id = ?", (later.isoformat(timespec="seconds"), new.position_id))
    r.old, r.new = old, new
    return r


def test_TE18b_a_read_failure_inside_the_early_stop_fails_closed(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    monkeypatch.setattr(FVS, "signature", lambda store, code: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    assert tick(r)["stopped"] is True                                                                    # (i) an unreadable signature stops (UNSIGNED)
    monkeypatch.setattr(FVS, "signature", SIG.signature)
    r2 = armed(tmp_path / "ack", monkeypatch)
    rec(r2)
    tick(r2)
    arm_as(r2, "user")
    real = r2.fv.store.query

    def no_ack(sql, args=()):
        if "execution_mode_events WHERE rowid > ?" in sql:
            raise sqlite3.OperationalError("database is locked")
        return real(sql, args)
    monkeypatch.setattr(r2.fv.store, "query", no_ack)
    assert tick(r2)["stopped"] is True                                                                   # (ii) an unreadable acknowledgement acknowledges nothing
    monkeypatch.setattr(r2.fv.store, "query", real)
    r3 = armed(tmp_path / "reread", monkeypatch)
    rec(r3)
    real3 = r3.fv.store.query

    def no_reread(sql, args=()):
        if "MAX(rowid) FROM execution_mode_events" in sql:
            raise sqlite3.OperationalError("database is locked")
        return real3(sql, args)
    monkeypatch.setattr(r3.fv.store, "query", no_reread)
    o = tick(r3)
    assert o["stopped"] is False and mode(r3) == "MONITOR_ONLY" and r3.fv._stopped == {}                 # (iii) unconfirmed: nothing recorded
    monkeypatch.setattr(r3.fv.store, "query", real3)
    arm_as(r3, "user")
    assert tick(r3)["stopped"] is True                                                                   # so the next arm is stopped again (fail-closed)


def test_TE19_a_restarted_observer_stops_the_first_arm_and_a_persons_rearm_acknowledges(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    tick(r)
    arm_as(r, "user")
    tick(r)
    r.fv = ForwardValidationService(stream_fn=r.fv.stream_fn, store=r.fv.store, clock=r.clock)        # a new process: nothing in memory
    assert tick(r)["stopped"] is True and stops(r) == 2
    arm_as(r, "user")
    assert tick(r)["stopped"] is False


# ================================================================ T-E20 ... T-E25: the safety contract ===================================

def table_digests(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            n = len(conn.execute(f"PRAGMA table_info('{name}')").fetchall())
            rows = conn.execute(f"SELECT * FROM \"{name}\" ORDER BY {', '.join(str(i + 1) for i in range(n))}").fetchall()
            out[name] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return out
    finally:
        conn.close()


def test_TE20_a_stop_tick_writes_only_the_declared_tables(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    obs(r, 60)                                                                                            # the safety cache is warm; no stored summary exists, so persistence is quiet
    a = table_digests(r.db)
    obs(r, 60)
    b = table_digests(r.db)
    reference = {t for t in b if a.get(t) != b[t]}
    rec(r)
    c = table_digests(r.db)
    assert tick(r)["stopped"] is True
    e = table_digests(r.db)
    stop = {t for t in e if c.get(t) != e[t]}
    allowed = {"forward_validation_events", "forward_validation_controls", "execution_mode", "execution_mode_events", "execution_controls"}
    assert stop - reference <= allowed, sorted(stop - reference - allowed)
    assert r.fv.store.dailies() == [] and r.fv.last_observation["summaries"]["checked"] == 0


def test_TE21_an_x2_stop_touches_no_broker_and_no_safety_latch(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    obs(r, 60)
    latch = lambda: r.fv.store.query("SELECT * FROM execution_safety_state")  # noqa: E731
    before = latch()
    rec(r)
    hits = []

    class Tripwire:
        def __getattr__(self, name):
            hits.append(name)
            raise AssertionError(f"the broker was touched during an X2 stop: {name}")
    real_stop = r.fv._early_stop

    def guarded(now, att, sample):
        keep, r.x.broker = r.x.broker, Tripwire()
        try:
            return real_stop(now, att, sample)
        finally:
            r.x.broker = keep
    monkeypatch.setattr(r.fv, "_early_stop", guarded)
    assert tick(r)["stopped"] is True and hits == []
    after = latch()
    latches = lambda rows: [{k: v for k, v in row.items() if k != "updated_at"} for row in rows]  # noqa: E731 - kill, pause and the daily halt with their stamps
    assert before and latches(after) == latches(before)


def test_TE22_static_the_early_stop_and_the_signature_call_nothing_but_reads_and_the_one_disarm():
    src = (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "ForwardValidationService")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_early_stop")
    calls = [c.func for c in ast.walk(fn) if isinstance(c, ast.Call)]
    on_execution = [c.attr for c in calls if isinstance(c, ast.Attribute) and isinstance(c.value, ast.Attribute) and c.value.attr == "execution"]
    assert on_execution == ["disable_auto"]
    names = {getattr(c, "attr", getattr(c, "id", None)) for c in calls}
    assert not names & {"enable_auto", "set_mode", "post_order", "submit", "cancel", "kill", "pause", "action", "_action", "insert_intent", "update_intent", "update_position"}
    store_calls = {c.attr for c in calls if isinstance(c, ast.Attribute) and isinstance(c.value, ast.Attribute) and c.value.attr == "store"}
    assert store_calls <= {"query", "add_event"}, store_calls
    sig = ast.parse((ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8"))
    sig_calls = {c.func.attr for c in ast.walk(sig) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert {c for c in sig_calls if c not in ("query", "sessions", "get", "add")} == set(), sig_calls
    banned = ("execution", "requests", "safety", "alpaca", "broker", "cash_flow", "autorun")
    assert not any(isinstance(n, ast.Import) and any(a.name.split(".")[0] in banned for a in n.names) for n in ast.walk(sig))
    assert not any(isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in banned for n in ast.walk(sig))       # L-06: the module of a from-import


def test_TE23_static_only_the_three_known_actors_arm_and_only_a_person_acknowledges():
    hits = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "browser_tests/", ".venv/", "scratchpad/")) or "/site-packages/" in rel:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"\benable_auto\(|_act\(\"enable_auto\"|set_mode\(\"PAPER_AUTO\"", line):
                hits.append((rel, line.strip()))
    actors = sorted({(rel, m.group(1)) for rel, line in hits for m in [re.search(r"actor=([A-Za-z_\"\-]+)", line)] if m})
    assert {rel for rel, _ in hits} == {"api/routes/execution.py", "autorun/service.py", "execution/service.py", "scripts/run_aapl_paper_smoke.py"}, hits
    assert actors == [("autorun/service.py", "ACTOR_SCHEDULED_ARM"), ("scripts/run_aapl_paper_smoke.py", "\"aapl-smoke\"")], actors
    assert [h for h in hits if "set_mode(\"PAPER_AUTO\"" in h[1]] == [("execution/service.py", 'self.store.set_mode("PAPER_AUTO", now, actor, reason, env)')]
    assert 'def enable_auto(self, actor: str = "user"' in (ROOT / "execution" / "service.py").read_text(encoding="utf-8")
    assert 'getattr(svc, fn_name)(actor="user"' in (ROOT / "api" / "routes" / "execution.py").read_text(encoding="utf-8")      # the HTTP route: a person
    from autorun.service import ACTOR_SCHEDULED_ARM
    assert ACTOR_SCHEDULED_ARM == "autorun_scheduled_arm" and SIG.HUMAN_ACTORS == frozenset({"user"})


def test_TE25_a_persistence_failure_never_prevents_the_early_stop(tmp_path, monkeypatch):
    r = armed(tmp_path, monkeypatch)
    rec(r)
    monkeypatch.setattr(r.fv, "_persist_stale_summaries", lambda now: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    o = obs(r, 60)
    assert o["early_stop"]["stopped"] is True and o["result"] == "OK" and o["summaries"] == {"result": "ERROR", "error": "OperationalError"}     # (i)
    assert mode(r) == "MONITOR_ONLY" and stops(r) == 1
    d = S.Direct(tmp_path / "direct")                                                                     # (ii), (iii): per-date isolation and the per-tick bound
    days = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", S.DAY]
    for t in days:
        d.fvs.put_daily(t, d.t, "v", d.fv.daily_summary(t))
    real = d.fv.bracketed_summary
    monkeypatch.setattr(d.fv, "bracketed_summary", lambda td, now=None: (_ for _ in ()).throw(RuntimeError("one date")) if td == S.DAY else real(td, now))
    o1 = d.fv._persist_stale_summaries(d.tick())                                                         # the newest date (processed FIRST) raises
    assert (o1["failed"], o1["refreshed"] + o1["rebuilt"], o1["checked"]) == (1, 2, 5)
    monkeypatch.setattr(d.fv, "bracketed_summary", real)
    o2 = d.fv._persist_stale_summaries(d.tick())
    o3 = d.fv._persist_stale_summaries(d.tick())
    assert (o2["refreshed"] + o2["rebuilt"], o3["refreshed"] + o3["rebuilt"], o3["pending"]) == (3, 0, False)   # at most 3 per tick, the rest next tick
    src = (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8")                      # (iv) static order
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "ForwardValidationService")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "observe")
    trys = [n for n in fn.body if isinstance(n, ast.Try)]
    assert len(trys) == 3
    main, capture, persist = trys
    in_body = lambda node, name: any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == name for c in ast.walk(node))  # noqa: E731
    assert in_body(main, "_early_stop") and not in_body(main, "_persist_stale_summaries") and in_body(capture, "tick") and not in_body(capture, "_persist_stale_summaries")
    assert [ast.unparse(s) for s in persist.body] == ["out['summaries'] = self._persist_stale_summaries(now)"]
    assert len(persist.handlers) == 1 and ast.unparse(persist.handlers[0].type) == "Exception"
    assert fn.body.index(persist) == fn.body.index(capture) + 1 and ast.unparse(fn.body[fn.body.index(persist) + 1]) == "self.last_observation = out"
