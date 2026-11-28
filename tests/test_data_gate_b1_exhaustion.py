"""
HDR-2 OD-C-7 (b1) (EXECUTION_DATA_GATE_SPEC_r15 3.8 step 4, 3.8.3; test 40 cases 1 to 14; 9-C rows A5 and A6): the exhausted HIT raises the
critical alert (protective_exit_problems, the PROTECTIVE_EXIT_UNVERIFIED FAILURE, a PROTECTIVE_EXIT BLOCK row) before today's ATTENTION write
and EXIT_SUBMISSION BLOCK touch. Carried corrections: E-1 (the plain route-3 setup gives an ACTIVE row), E-4 (case 10b in the P6 form, an open
order of another origin). Stage-local evidence.

Rig: the C1 rigs (tests/test_data_gate_exits_service.py Rig: the real AlpacaPaperBroker with simulated=False over the simulated wire; and
tests/test_data_gate_entries_c1c2.py ERig: READY candidates in other symbols, PAPER_AUTO). A restart is a new ExecutionService on the same store.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from execution import broker as BK
from execution import data_gate as DG
from execution import service as SVC
from execution.engine import exit_coid
from execution.service import ExecutionService
from execution.store import ExecutionStore
from test_data_gate_entries_c1c2 import ERig, _c1d_fail, _exhausted, _failing_prot, engine_pass, entry_intents, entry_verdicts, rec  # noqa: F401
from test_data_gate_exits_service import (HELD, LIFE, PROT, T0, T36, UTC, Rig, _c1d_touch_spy, an_order, clock_answer, codes_of, exhausted, hit_rig,
                                          iso, seam_case)

EXH = ["EXIT_ATTEMPTS_EXHAUSTED"]


def restart(rig, entries=2):
    rig.x = ExecutionService(lambda: rig.stream, rig.store, broker=rig.broker, clock=lambda: rig.t, owner="test-owner")
    rig.x.max_entries_per_cycle = entries
    return rig.x


def alert_rows(rig, p):
    return [r for r in rig.life(PROT, p.position_id) if r["kind"] == PROT]


def step4_rows(seen, p):
    return [(c, r, t, i) for c, r, t, i in seen if c == "PROTECTIVE_EXIT" and i.get("codes") == EXH and i.get("position_id") == p.position_id]


def store_spy(monkeypatch, rig, crash_on_attention=None, raise_on_attention=None):
    """The write-order spy (case 1 (a)): every add_safety_transition and update_position call, in order. `crash_on_attention` /
    `raise_on_attention`: raise SimulatedCrash / an ordinary store error from the update_position call that sets that position to ATTENTION,
    before it writes."""
    calls, real_add, real_upd = [], rig.store.add_safety_transition, rig.store.update_position

    def add(fam, pid, *a, **k):
        calls.append(("add", fam, pid, sorted((k.get("alert") or {}).items()) if k.get("alert") else None))
        return real_add(fam, pid, *a, **k)

    def upd(pid, now, event=None, **f):
        if f.get("state") == "ATTENTION":
            calls.append(("attention", pid))
            if crash_on_attention == pid:
                raise SVC.SimulatedCrash("injected before the ATTENTION write")
            if raise_on_attention is not None and raise_on_attention.get("pid") == pid:
                raise sqlite3.OperationalError("injected: database is locked")
        return real_upd(pid, now, event, **f)
    monkeypatch.setattr(rig.store, "add_safety_transition", add)
    monkeypatch.setattr(rig.store, "update_position", upd)
    return calls


def route3(rig, a):
    """Route 3: each of A's exit cycles, up to max_exit_cycles, ends with an accepted protective exit that is later canceled."""
    for k in range(1, rig.x.max_exit_cycles + 1):
        n = rig.posts()
        rig.cycle()
        assert rig.posts() == n + 1 and rig.pos(a).exit_cycle == k
        rig.sim.set_status(exit_coid(a.position_id, k), "canceled")
    assert rig.life(PROT, a.position_id) == []                                     # no refusal row and no latch on this route
    assert rig.x._unsaved_blocking_alerts == {}


def route3_rig(tmp_path, **kw):
    rig = ERig(tmp_path, ready=("NEWA", "NEWB"), **kw)
    rig.x.max_entries_per_cycle = 2
    a = rig.open("AAA")
    rig.live("AAA", "8.50")                                                       # A is HIT
    route3(rig, a)
    return rig, a


def assert_refused_b1(vs, n=2):
    assert len(vs) == n
    for v in vs:
        assert not v.allowed and {"DG_SAFETY_CHECK_FAILED", "DG_ATTENTION_ENTERED_THIS_CYCLE"} <= set(v.codes)
        assert {"E10(i)", "E10(ii)", "E10(v)"} <= set(v.failed_facts)


# ================================================================ (1) route 3, and the write order ==============================================
def test_b1_40_1_route_3_alert_row_then_attention_entries_refused_and_the_spy_order(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = route3_rig(tmp_path)
    ec = rig.pos(a).exit_cycle
    assert ec == rig.x.max_exit_cycles
    calls = store_spy(monkeypatch, rig)
    seen = _c1d_touch_spy(rig)
    n, posts = len(rec), rig.posts()
    rig.cycle()                                                                   # cycle c: the exhaustion check runs
    rows = rig.life(PROT, a.position_id)
    assert len(rows) == 1 and rows[0]["kind"] == PROT and rows[0]["detail"]["lifecycle"] == "ACTIVE" and rows[0]["detail"]["episode"] == 1   # E-1
    assert codes_of(rows[0]) == EXH and rows[0]["detail"]["exit_cycle"] == ec and "deferred" not in rows[0]["detail"]
    assert set(rig.x.protective_exit_problems) == {"AAA"}
    assert "AAA" in rig.x.protective_exit_problems["AAA"] and "EXIT_ATTEMPTS_EXHAUSTED" in rig.x.protective_exit_problems["AAA"]
    blk = step4_rows(seen, a)
    assert len(blk) == 1 and blk[0][1] == "BLOCK" and blk[0][3] == {"position_id": a.position_id, "codes": EXH}
    assert "AAA" in blk[0][2] and "EXIT_ATTEMPTS_EXHAUSTED" in blk[0][2] and blk[0][2] == rig.x.protective_exit_problems["AAA"]
    sub = [(r, t, i) for c, r, t, i in seen if c == "EXIT_SUBMISSION"]
    assert sub == [("BLOCK", f"AAA: {ec} exit attempts exhausted; needs attention", {"position_id": a.position_id})]
    q = rig.pos(a)
    assert q.state == "ATTENTION" and q.attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.events(a, "ATTENTION")[-1]["reason"] == "EXIT_ATTEMPTS_EXHAUSTED"
    assert_refused_b1(entry_verdicts(rec[n:], DG.DG_EARLY))
    assert rig.posts() == posts and entry_intents(rig) == [] and rig.entry_posts == []
    # (a) the store-call spy: the alert write comes before the ATTENTION write, and no ATTENTION write of A comes before it
    alert_at = next(i for i, c in enumerate(calls) if c[:3] == ("add", PROT, a.position_id))
    att_at = calls.index(("attention", a.position_id))
    assert alert_at < att_at and ("attention", a.position_id) not in calls[:alert_at]
    n = len(rec)
    rig.cycle()                                                                   # the next cycle's candidates are refused too
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert len(vs) == 2 and all(not v.allowed and "E10(ii)" in v.failed_facts for v in vs) and rig.posts() == posts
    assert len(rig.life(PROT, a.position_id)) == 1


def test_b1_40_1b_crash_before_the_attention_write_leaves_the_row_then_a_restart_moves_a_to_attention(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = route3_rig(tmp_path)
    crash = {"pid": a.position_id}
    real_upd = rig.store.update_position

    def upd(pid, now, event=None, **f):
        if f.get("state") == "ATTENTION" and crash.get("pid") == pid:
            raise SVC.SimulatedCrash("injected before the ATTENTION write")
        return real_upd(pid, now, event, **f)
    monkeypatch.setattr(rig.store, "update_position", upd)
    with pytest.raises(SVC.SimulatedCrash):
        rig.cycle()                                                               # run_cycle re-raises the injected crash
    crash.clear()
    rows = rig.life(PROT, a.position_id)                                          # read from the database afterwards
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and codes_of(rows[0]) == EXH   # E-1
    assert rig.pos(a).state == "EXIT_REQUIRED" and rig.events(a, "ATTENTION") == []
    restart(rig)
    posts, n = rig.posts(), len(rec)
    rig.cycle()                                                                   # A still HIT
    assert len(rig.life(PROT, a.position_id)) == 1                               # no second alert row
    assert rig.pos(a).state == "ATTENTION" and rig.pos(a).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == posts
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert len(vs) == 2 and all(not v.allowed for v in vs) and entry_intents(rig) == []


# ================================================================ (2) route 2 and (3) route 1 =================================================
def test_b1_40_2_route_2_dg_final_refusals_then_exhaustion_one_row_in_total(tmp_path):
    rig, p = hit_rig(tmp_path)
    for k in range(1, rig.x.max_exit_cycles + 1):
        rig.broker.faults["orders"] = [None, lambda *a_: [an_order("SNDK", "sell")]]   # P6 at DG-final: an open order of another origin appears
        rig.cycle()
        assert rig.pos(p).exit_cycle == k and rig.pos(p).state == "EXIT_REQUIRED" and rig.posts() == 0
    rig.broker.faults.pop("orders", None)
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and "EXIT_ATTEMPTS_EXHAUSTED" not in codes_of(rows[0]) and codes_of(rows[0])
    first = list(rows[0]["detail"]["reason_codes"])
    seen = _c1d_touch_spy(rig)
    rig.cycle()                                                                   # the exhaustion check
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and codes_of(rows[0]) == first                          # one row in total, written at the first refusal
    blk = step4_rows(seen, p)
    assert len(blk) == 1 and blk[0][1] == "BLOCK" and blk[0][3] == {"position_id": p.position_id, "codes": EXH}
    assert rig.pos(p).state == "ATTENTION" and rig.pos(p).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0


@pytest.mark.parametrize("case", ["i", "ii", "iii"])
def test_b1_40_3_route_1_session_failure_on_the_last_cycle_then_no_second_row(tmp_path, case):
    rig, p = hit_rig(tmp_path, t0=T36[case])
    rig.x.max_exit_cycles = 1
    seam_case(rig, case)
    rig.cycle()
    assert codes_of(rig.life(PROT, p.position_id)[0]) == ["DG_FINAL_SESSION_FAILED", "EXIT_ATTEMPTS_EXHAUSTED"]
    rig.x.hooks.clear()
    rig.x.near_close_minutes = 15
    rig.sim.is_open = True
    rig.t = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    seen = _c1d_touch_spy(rig)
    rig.cycle()                                                                   # the next in-session cycle, the stop still HIT
    assert len(rig.life(PROT, p.position_id)) == 1                               # no second row
    blk = step4_rows(seen, p)
    assert len(blk) == 1 and blk[0][3] == {"position_id": p.position_id, "codes": EXH} and "SNDK" in rig.x.protective_exit_problems
    assert rig.pos(p).state == "ATTENTION" and rig.pos(p).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0


def test_b1_40_3b_a_verifying_probe_resolves_then_the_next_hit_exhaustion_writes_a_recurrent_row(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path, t0=T36["i"])
    rig.x.max_exit_cycles = 1
    seam_case(rig, "i")
    rig.cycle()
    rig.x.hooks.clear()
    rig.sim.is_open = True
    rig.t = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    rig.live("SNDK", "12.00")                                                     # a verified NOT_HIT cycle: the OD-R4-2(a) probe verifies every fact
    rig.cycle()
    assert [r["kind"] for r in rig.life(PROT, p.position_id)] == [PROT, PROT + "_RESOLVED"]
    assert rig.life(PROT, p.position_id)[-1]["detail"]["evidence"] == "PROTECTIVE_FACTS_VERIFIED" and rig.pos(p).state == "EXIT_REQUIRED"
    rig.live("SNDK", "8.50")
    calls = store_spy(monkeypatch, rig)
    rig.cycle()                                                                   # HIT again: the exhaustion check
    rows = rig.life(PROT, p.position_id)
    assert [r["kind"] for r in rows] == [PROT, PROT + "_RESOLVED", PROT] and rows[-1]["detail"]["lifecycle"] == "RECURRENT" and codes_of(rows[-1]) == EXH
    alert_at = next(i for i, c in enumerate(calls) if c[:3] == ("add", PROT, p.position_id))
    assert alert_at < calls.index(("attention", p.position_id)) and rig.pos(p).state == "ATTENTION"


def test_b1_40_3c_an_exhausted_verified_not_hit_with_no_strategy_candidate_is_not_evaluated(tmp_path, monkeypatch):
    rig, p = exhausted(tmp_path)
    rig.store.update_position(p.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION")
    rig.live("SNDK", "12.00")
    gate = []
    real = rig.x._maybe_submit_exit
    monkeypatch.setattr(rig.x, "_maybe_submit_exit", lambda pos, *a_, **k: (gate.append(pos.symbol), real(pos, *a_, **k))[1])
    rig.cycle()
    assert gate == [] and rig.recs(LIFE) == [] and rig.pos(p).state == "EXIT_REQUIRED" and rig.events(p, "ATTENTION") == []
    assert rig.ctl("PROTECTIVE_EXIT") is None and rig.x.protective_exit_problems == {}


# ================================================================ (4) a strategy exhaustion, (5) a simulated broker ============================
def test_b1_40_4_strategy_exhaustion_is_today_s_attention_with_no_lifecycle_or_protective_exit_row(tmp_path):
    rig = Rig(tmp_path)
    p = rig.open(at=rig.t - timedelta(days=8))                                    # a TIME_EXIT candidate is reached
    rig.live("SNDK", "12.00")                                                     # a verified NOT_HIT
    rig.store.update_position(p.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION", exit_cycle=rig.x.max_exit_cycles)
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    q = rig.pos(p)
    assert q.state == "ATTENTION" and q.attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.posts() == 0
    assert rig.recs(LIFE) == [] and [c for c, *_ in seen if c == "PROTECTIVE_EXIT"] == [] and rig.ctl("PROTECTIVE_EXIT") is None
    assert rig.x.protective_exit_problems == {}
    sub = [(r, i) for c, r, _t, i in seen if c == "EXIT_SUBMISSION"]
    assert sub == [("BLOCK", {"position_id": p.position_id})] and rig.ctl("EXIT_SUBMISSION")["last_input"] == {"position_id": p.position_id}


def test_b1_40_5_simulated_broker_writes_no_row_and_is_today_s(tmp_path, monkeypatch):
    """On the simulated rig of tests/test_bot_position_recovery.py (T08's pattern: three rejected POSTs, then the exhaustion check, a HIT)."""
    import json
    import test_bot_position_recovery as BPR
    r = BPR.rig(tmp_path, monkeypatch)
    _pv, pos = BPR.exhausted(r)                                                   # asserts ATTENTION EXIT_ATTEMPTS_EXHAUSTED at exit_cycle 3
    c = sqlite3.connect(str(r.db))
    try:
        kinds = [k for (k,) in c.execute("SELECT kind FROM execution_reconciliations")]
        ctl = {row[0]: row[1:] for row in c.execute("SELECT control_id, last_result, last_input_json FROM execution_controls")}
    finally:
        c.close()
    assert not set(kinds) & LIFE and "PROTECTIVE_EXIT" not in ctl and "DATA_GATE" not in ctl
    assert ctl["EXIT_SUBMISSION"][0] == "BLOCK" and json.loads(ctl["EXIT_SUBMISSION"][1]) == {"position_id": pos.position_id}
    assert r.x.protective_exit_problems == {} and r.x._unsaved_blocking_alerts == {}
    assert r.x.store.position(pos.position_id).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED"


# ================================================================ (6) recovery CLOSE with the row present ======================================
def _account_snapshot(rig, positions):
    from account_refresh.store import AccountRefreshStore
    from database.account_refresh_migrations import run_account_refresh_migrations
    c = sqlite3.connect(str(rig.db))
    try:
        run_account_refresh_migrations(c)
    finally:
        c.close()
    at, rid = rig.t + timedelta(seconds=1), uuid.uuid4().hex
    snap = {"refresh_id": rid, "refreshed_at": iso(at), "source": "SIMULATED_PAPER", "host": "paper-api.alpaca.markets", "account": {"status": "ACTIVE"},
            "positions": positions, "open_orders": []}
    AccountRefreshStore(rig.db).commit_success(snap, {"started_at": iso(at), "finished_at": iso(at), "trigger": "TEST", "result": "SUCCESS", "read_attempts": 1,
                                                      "refresh_id": rid, "owner": "test"}, at)
    return at + timedelta(seconds=1)


def _manual_sale_and_close(rig, a, cycle=True):
    """The owner's manual sale on the website, MONITOR_ONLY (recovery precondition 2), one RECONCILE that sees the broker flat, then the recovery
    command's preview and CLOSE. Returns (preview, now)."""
    from execution import recovery as R
    rig.sim.set_position(a.symbol, 0)
    rig.store.set_mode("MONITOR_ONLY", rig.t, "test", None, {})
    if cycle:
        rig.cycle()
    now = _account_snapshot(rig, [])
    pv = R.preview(rig.db, a.position_id, now)
    done = R.apply(rig.db, a.position_id, pv["evidence_hash"], "owner", "manual sale on the website", confirmed_flat=True, now=now)
    assert done["ok"] is True and rig.pos(a).state == "CLOSED", done
    rig.t = now
    return pv, now


def test_b1_40_6_recovery_close_with_the_row_present_then_position_closed_resolves_it(tmp_path, rec, engine_pass):
    rig = ERig(tmp_path)
    a = _exhausted(rig)
    rig.cycle()                                                                   # cycle c: the alert row, then ATTENTION
    assert [r["kind"] for r in rig.life(PROT, a.position_id)] == [PROT] and rig.pos(a).state == "ATTENTION"
    pv, _now = _manual_sale_and_close(rig, a)
    pre = {x["n"]: x for x in pv["preconditions"]}
    assert pre[1]["ok"] is True and pre[7]["ok"] is True and pre[10]["ok"] is True, (pre[1], pre[7], pre[10])
    assert pv["case"] == "CLOSE" and pv["ok"] is True
    rig.cycle()                                                                   # the next cycle of the same process: the CLOSED sweep
    rows = rig.life(PROT, a.position_id)
    assert [r["kind"] for r in rows] == [PROT, PROT + "_RESOLVED"] and rows[-1]["detail"]["evidence"] == "POSITION_CLOSED"


# ================================================================ (7) the alert write fails (outcome 3) =========================================
def _outcome_3(tmp_path, monkeypatch, rec):
    rig = ERig(tmp_path)
    a = _exhausted(rig)
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    n = len(rec)
    rig.cycle()                                                                   # cycle c
    assert (PROT, a.position_id) in rig.x._unsaved_blocking_alerts
    assert rig.x._unsaved_blocking_alerts[(PROT, a.position_id)]["reason_codes"] == EXH
    v = entry_verdicts(rec[n:], DG.DG_EARLY)[-1]
    assert "DG_SAFETY_ALERT_UNSAVED" in v.codes and "E10(iii)" in v.failed_facts and "E10(i)" in v.failed_facts
    assert rig.pos(a).state == "ATTENTION" and rig.pos(a).attention_reason == "EXIT_ATTEMPTS_EXHAUSTED" and rig.life(PROT) == []
    s["fail"] = set()                                                             # the store recovers
    return rig, a


def test_b1_40_7_alert_write_fails_latch_holds_then_after_a_recovery_close_the_next_cycle_writes_the_two_row_episode(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_3(tmp_path, monkeypatch, rec)
    for _ in range(3):                                                            # cycles c+1 to c+3
        rig.cycle()
        assert rig.life(PROT, a.position_id) == [] and (PROT, a.position_id) in rig.x._unsaved_blocking_alerts
    _manual_sale_and_close(rig, a)                                                # no enable_auto in between (E-2); MONITOR_ONLY, nothing else open
    out = rig.cycle()                                                            # the next cycle of the same process is NOT idle (CF-1)
    assert out["result"] == "RUN"
    kinds = [(r["kind"], r["detail"].get("deferred"), r["detail"].get("evidence")) for r in rig.life(PROT, a.position_id)]
    assert kinds == [(PROT, True, None), (PROT + "_RESOLVED", None, "POSITION_CLOSED")] and rig.x._unsaved_blocking_alerts == {}
    assert rig.cycle()["result"] == "IDLE"                                         # the cycle after it is idle


def test_b1_40_7b_window_r3_a_restart_while_in_attention_keeps_entries_refused_and_no_row(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_3(tmp_path, monkeypatch, rec)
    restart(rig, entries=1)
    n = len(rec)
    rig.cycle()
    assert any("AAA" in r for r in rig.x.recon_reasons) and not rig.x.recon_healthy
    assert entry_intents(rig) == [] and rig.entry_posts == []
    assert entry_verdicts(rec[n:], DG.DG_EARLY) == [] or all(not v.allowed for v in entry_verdicts(rec[n:], DG.DG_EARLY))
    assert rig.life(PROT, a.position_id) == [] and rig.x._unsaved_blocking_alerts == {}   # residual R-B1-1 (i)


def test_b1_40_7c_window_r4_a_restart_straight_after_the_close_is_idle_and_no_row_is_ever_written(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_3(tmp_path, monkeypatch, rec)
    rig.cycle()
    _manual_sale_and_close(rig, a)
    restart(rig, entries=1)                                                       # before any cycle of the old process
    for _ in range(2):
        assert rig.cycle()["result"] == "IDLE"
    rig.store.set_mode("PAPER_AUTO", rig.t, "test", None, {})
    assert rig.cycle()["result"] == "RUN"
    assert rig.life(PROT, a.position_id) == []                                    # residual R-B1-1 (i)


# ================================================================ (8) forward validation =======================================================
def test_b1_40_8_forward_validation_not_ready_until_the_resolution_row_and_the_all_time_record_recovers(tmp_path, monkeypatch):
    from test_data_gate_lifecycle_recovery_fv import LATER, RNOW, all_time_blocked, codes, started
    rig, p = exhausted(tmp_path / "rig")
    rig.cycle()
    row = rig.life(PROT, p.position_id)[0]
    assert codes_of(row) == EXH and row["blocking"] == 1
    r, _run = started(tmp_path / "fv", monkeypatch)
    base = r.fv.cumulative(LATER)
    st = ExecutionStore(Path(r.db))                                               # the row step 4 wrote, through the same store method, in the run's window
    assert st.add_safety_transition(PROT, p.position_id, "SNDK", "NONE", RNOW + timedelta(minutes=1), row["trigger"], None,
                                    alert={"reason_codes": codes_of(row), "exit_cycle": row["detail"]["exit_cycle"]}) is True
    cum = r.fv.cumulative(LATER)
    assert "RUN_WINDOW_RECONCILIATION_OPEN" in codes(cum) and cum["all_time"]["status"] == "NOT READY" and all_time_blocked(cum)
    assert st.add_safety_transition(PROT, p.position_id, "SNDK", "ACTIVE", RNOW + timedelta(minutes=2), "CYCLE", None, resolution={"evidence": "POSITION_CLOSED"}) is True
    after = r.fv.cumulative(LATER)
    assert "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(after) and after["all_time"]["status"] == base["all_time"]["status"]
    assert all_time_blocked(after) == all_time_blocked(base)


# ================================================================ (9) the ATTENTION write raises, (10) both writes fail ========================
def test_b1_40_9_attention_write_raises_the_row_stands_then_the_next_cycle_moves_a_to_attention(tmp_path, monkeypatch, rec, engine_pass):
    rig = ERig(tmp_path)
    a = _exhausted(rig)
    boom = {"pid": a.position_id}
    store_spy(monkeypatch, rig, raise_on_attention=boom)
    out = rig.cycle()
    assert out["result"] == "ERROR" and entry_verdicts(rec, DG.DG_EARLY) == [] and entry_intents(rig) == []   # no ENTRIES step
    assert [r["detail"]["lifecycle"] for r in rig.life(PROT, a.position_id)] == ["ACTIVE"] and rig.pos(a).state == "EXIT_REQUIRED"
    boom.clear()
    rig.cycle()                                                                   # A still HIT
    assert len(rig.life(PROT, a.position_id)) == 1 and rig.pos(a).state == "ATTENTION" and rig.posts() == 0


def _outcome_4(tmp_path, monkeypatch, rec):
    rig = ERig(tmp_path)
    a = _exhausted(rig)
    s = {"fail": {PROT}, "calls": []}
    _failing_prot(monkeypatch, rig, s)
    boom = {"pid": a.position_id}
    real_upd = rig.store.update_position

    def upd(pid, now, event=None, **f):
        if f.get("state") == "ATTENTION" and boom.get("pid") == pid:
            raise sqlite3.OperationalError("injected: database is locked")
        return real_upd(pid, now, event, **f)
    monkeypatch.setattr(rig.store, "update_position", upd)
    out = rig.cycle()
    assert out["result"] == "ERROR" and entry_verdicts(rec, DG.DG_EARLY) == [] and entry_intents(rig) == []
    assert (PROT, a.position_id) in rig.x._unsaved_blocking_alerts and rig.pos(a).state == "EXIT_REQUIRED" and rig.life(PROT) == []
    s["fail"] = set()
    boom.clear()
    return rig, a


def test_b1_40_10a_both_writes_fail_then_the_latch_retry_writes_the_deferred_row_before_the_exit_gate(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_4(tmp_path, monkeypatch, rec)
    order, real_add, real_exit = [], rig.store.add_safety_transition, rig.x._maybe_submit_exit
    monkeypatch.setattr(rig.store, "add_safety_transition", lambda fam, pid, *a_, **k: (order.append(("add", fam, (k.get("alert") or {}).get("deferred"))), real_add(fam, pid, *a_, **k))[1])
    monkeypatch.setattr(rig.x, "_maybe_submit_exit", lambda pos, *a_, **k: (order.append(("exit", pos.symbol)), real_exit(pos, *a_, **k))[1])
    rig.cycle()                                                                   # the store recovered, A still HIT
    assert order[:2] == [("add", PROT, True), ("exit", "AAA")]
    rows = rig.life(PROT, a.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["deferred"] is True and codes_of(rows[0]) == EXH   # no second row at step 4
    assert rig.pos(a).state == "ATTENTION" and rig.x._unsaved_blocking_alerts == {}


def test_b1_40_10b_a_probe_that_cannot_verify_leaves_the_deferred_row_unresolved_and_entries_refused(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_4(tmp_path, monkeypatch, rec)
    rig.live("AAA", "12.00")                                                      # a verified NOT_HIT, no strategy candidate
    rig.broker.faults["orders"] = lambda *a_: [an_order("AAA", "sell")]           # E-4: P6, an open order of another origin
    gate = []
    real_exit = rig.x._maybe_submit_exit
    monkeypatch.setattr(rig.x, "_maybe_submit_exit", lambda pos, *a_, **k: (gate.append(pos.symbol), real_exit(pos, *a_, **k))[1])
    n = len(rec)
    rig.cycle()
    rows = rig.life(PROT, a.position_id)
    assert [r["kind"] for r in rows] == [PROT] and rows[0]["detail"]["deferred"] is True and rig.state(PROT, a) == "ACTIVE"
    assert gate == [] and rig.pos(a).state == "EXIT_REQUIRED" and rig.events(a, "ATTENTION") == []
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert vs and all("DG_SAFETY_CHECK_FAILED" in v.codes and "E10(ii)" in v.failed_facts for v in vs) and entry_intents(rig) == []


def test_b1_40_10b_prime_a_verifying_probe_resolves_the_deferred_row_before_the_entries_step(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_4(tmp_path, monkeypatch, rec)
    rig.live("AAA", "12.00")
    n = len(rec)
    rig.cycle()
    rows = rig.life(PROT, a.position_id)
    assert [(r["kind"], r["detail"].get("deferred"), r["detail"].get("evidence")) for r in rows] == [(PROT, True, None), (PROT + "_RESOLVED", None, "PROTECTIVE_FACTS_VERIFIED")]
    assert rows[0]["rid"] < rows[1]["rid"] and rig.pos(a).state == "EXIT_REQUIRED" and rig.events(a, "ATTENTION") == []
    vs = entry_verdicts(rec[n:], DG.DG_EARLY)
    assert vs and all("E10(ii)" not in v.failed_facts for v in vs)


def test_b1_40_10c_window_r5_a_restart_before_any_retry_then_the_next_hit_runs_step_4_in_full(tmp_path, monkeypatch, rec, engine_pass):
    rig, a = _outcome_4(tmp_path, monkeypatch, rec)
    restart(rig)
    rig.live("AAA", "12.00")                                                      # a verified NOT_HIT
    rig.cycle()
    assert rig.life(PROT) == [] and rig.pos(a).state == "EXIT_REQUIRED" and rig.events(a, "ATTENTION") == []   # residual R-B1-1 (ii)
    rig.live("AAA", "8.50")
    calls = store_spy(monkeypatch, rig)
    rig.cycle()
    assert [r["detail"]["lifecycle"] for r in rig.life(PROT, a.position_id)] == ["ACTIVE"] and rig.pos(a).state == "ATTENTION"
    alert_at = next(i for i, c in enumerate(calls) if c[:3] == ("add", PROT, a.position_id))
    assert alert_at < calls.index(("attention", a.position_id))


# ================================================================ (11) route 4, (12) route 5 ====================================================
def test_b1_40_11_route_4_broker_rejections_at_the_post_then_an_active_row_then_attention(tmp_path, monkeypatch):
    rig, p = hit_rig(tmp_path)
    for k in range(1, rig.x.max_exit_cycles + 1):
        rig.sim.post_modes.append("reject")
        rig.cycle()
        assert rig.posts() == k
    assert rig.life(PROT, p.position_id) == [] and rig.pos(p).exit_cycle == rig.x.max_exit_cycles
    calls = store_spy(monkeypatch, rig)
    rig.cycle()
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and codes_of(rows[0]) == EXH and rig.posts() == rig.x.max_exit_cycles
    assert next(i for i, c in enumerate(calls) if c[:3] == ("add", PROT, p.position_id)) < calls.index(("attention", p.position_id))
    assert rig.pos(p).state == "ATTENTION"


def test_b1_40_12_route_5_strategy_exits_used_the_cycles_then_the_stop_is_hit(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    p = rig.open(at=rig.t - timedelta(days=8))
    rig.live("SNDK", "12.00")                                                     # NOT_HIT: TIME_EXIT strategy exits, accepted then canceled
    for k in range(1, rig.x.max_exit_cycles + 1):
        rig.cycle()
        assert rig.posts() == k and rig.pos(p).exit_cycle == k
        rig.sim.set_status(exit_coid(p.position_id, k), "canceled")
    assert rig.life(PROT) == []
    rig.live("SNDK", "8.50")                                                      # the stop becomes HIT
    calls = store_spy(monkeypatch, rig)
    rig.cycle()
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and codes_of(rows[0]) == EXH
    assert next(i for i, c in enumerate(calls) if c[:3] == ("add", PROT, p.position_id)) < calls.index(("attention", p.position_id))
    assert rig.pos(p).state == "ATTENTION" and rig.posts() == rig.x.max_exit_cycles


# ================================================================ (13) steps 1 to 3 end the evaluation first ===================================
def test_b1_40_13_a_p8a1_deferral_writes_nothing_then_the_first_in_session_cycle_applies_step_4(tmp_path):
    rig, p = exhausted(tmp_path, t0=datetime(2026, 11, 10, 20, 50, tzinfo=UTC) - timedelta(seconds=30))   # 15:50 New York: the clock open, past the cutoff
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    assert rig.recs(LIFE) == [] and step4_rows(seen, p) == [] and rig.x.protective_exit_problems == {} and rig.pos(p).state != "ATTENTION"
    w = rig.ctl("EXIT_SUBMISSION")
    assert w["last_result"] == "WARN" and w["last_input"]["step"] == "P8a-1"
    rig.t = T0 + timedelta(days=1)                                                # the first in-session cycle
    rig.cycle()
    assert [r["detail"]["lifecycle"] for r in rig.life(PROT, p.position_id)] == ["ACTIVE"] and len(step4_rows(seen, p)) == 1
    assert rig.pos(p).state == "ATTENTION"


def test_b1_40_13b_a_p8a2_deferral_writes_nothing_then_the_first_open_cycle_applies_step_4(tmp_path):
    rig, p = exhausted(tmp_path)
    rig.broker.faults["clock"] = [None, clock_answer(rig, is_open=False)]        # the exit gate's own verified clock says closed
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    assert rig.recs(LIFE) == [] and step4_rows(seen, p) == [] and rig.x.protective_exit_problems == {}
    assert rig.pos(p).state == "EXIT_REQUIRED" and rig.pos(p).exit_cycle == 3
    w = rig.ctl("EXIT_SUBMISSION")
    assert w["last_result"] == "WARN" and w["last_input"]["step"] == "P8a-2"
    rig.cycle()
    assert [r["detail"]["lifecycle"] for r in rig.life(PROT, p.position_id)] == ["ACTIVE"] and len(step4_rows(seen, p)) == 1
    assert rig.pos(p).state == "ATTENTION"


def test_b1_40_13c_a_p8b_refusal_writes_p8b_s_row_then_the_readable_cycle_adds_the_block_row_and_attention(tmp_path):
    rig, p = exhausted(tmp_path)
    rig.broker.faults["clock"] = [None, BK.BrokerError("TIMEOUT")]               # the exit gate's own clock read raises
    seen = _c1d_touch_spy(rig)
    rig.cycle()
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and codes_of(rows[0]) == ["P8_CLOCK_UNREADABLE"] and "EXIT_ATTEMPTS_EXHAUSTED" not in codes_of(rows[0])
    blocks = [(c, i.get("codes")) for c, r, _t, i in seen if c in ("PROTECTIVE_EXIT", "EXIT_SUBMISSION") and r == "BLOCK"]
    assert blocks and all(codes == ["P8_CLOCK_UNREADABLE"] for _c, codes in blocks) and {c for c, _ in blocks} == {"PROTECTIVE_EXIT", "EXIT_SUBMISSION"}
    assert rig.pos(p).state == "EXIT_REQUIRED" and rig.events(p, "ATTENTION") == []
    rig.cycle()                                                                   # the clock readable, A still HIT
    assert len(rig.life(PROT, p.position_id)) == 1 and len(step4_rows(seen, p)) == 1 and rig.pos(p).state == "ATTENTION"


# ================================================================ (14) a HIT whose own pass-1 evaluation failed =================================
def test_b1_40_14_a_hit_whose_pass_1_failed_still_gets_step_4_in_full(tmp_path, monkeypatch):
    rig, p = exhausted(tmp_path)
    _c1d_fail(monkeypatch, rig, "setup", "SNDK")                                  # its exit context raises in pass 1
    seen = _c1d_touch_spy(rig)
    calls = store_spy(monkeypatch, rig)
    rig.cycle()
    assert "SNDK" in rig.x.stop_check_problems and any(c == "EXIT_TRIGGER" and r == "UNKNOWN" and i.get("position_id") == p.position_id for c, r, _t, i in seen)
    rows = rig.life(PROT, p.position_id)
    assert len(rows) == 1 and rows[0]["detail"]["lifecycle"] == "ACTIVE" and codes_of(rows[0]) == EXH and rows[0]["detail"]["exit_cycle"] == 3
    assert len(step4_rows(seen, p)) == 1 and "SNDK" in rig.x.protective_exit_problems
    assert next(i for i, c in enumerate(calls) if c[:3] == ("add", PROT, p.position_id)) < calls.index(("attention", p.position_id))
    assert rig.pos(p).state == "ATTENTION" and rig.posts() == 0


# ================================================================ 9-C rows A5 and A6: the replacement pins =====================================
def test_b1_9c_a5_no_row_still_holds_off_the_protective_path_strategy_simulated_and_not_hit(tmp_path):
    """r15 9-C row A5: the C1 pin's 'no lifecycle row' stays pinned where it still holds (cases 4, 5 and 3c are its kills); a HELD row never
    comes from step 4 either."""
    rig = Rig(tmp_path)
    p = rig.open(at=rig.t - timedelta(days=8))
    rig.live("SNDK", "12.00")
    rig.store.update_position(p.position_id, rig.t, None, state="EXIT_REQUIRED", exit_reason="HARD_INVALIDATION", exit_cycle=rig.x.max_exit_cycles)
    rig.cycle()
    assert rig.pos(p).state == "ATTENTION" and rig.life(PROT) == [] and rig.life(HELD) == []


def test_b1_9c_a6_step_4_i_to_iii_are_the_statements_before_the_existing_lines_under_the_protective_path_test():
    """r15 9-C row A6 (the AST half; tests/test_data_gate_exits_structure.py keeps the line pins): the exhaustion if's body is exactly one
    protective-path if, holding (i) the stored text, (ii) the FAILURE, (iii) the BLOCK row, then today's statements."""
    import ast
    src = (Path(__file__).resolve().parents[1] / "execution" / "service.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "ExecutionService")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_maybe_submit_exit")
    ex = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == "pos.exit_cycle >= self.max_exit_cycles")
    inner = ex.body[0]
    assert isinstance(inner, ast.If) and not inner.orelse and ast.unparse(inner.test) == "gate is not None and gate.get('order_class') == DG.PROTECTIVE_EXIT"
    body = [ast.unparse(s) for s in inner.body]
    assert len(body) == 4 and body[0].startswith("b1_text = f'{pos.symbol}: ") and body[1] == "self.protective_exit_problems[pos.symbol] = b1_text"
    assert body[2].startswith("self._safety_transition(now, gate.get('trigger') or trigger, gate.get('run_id'), 'PROTECTIVE_EXIT_UNVERIFIED', pos.position_id, pos.symbol, 'FAILURE'")
    assert body[2].endswith("reason_codes=['EXIT_ATTEMPTS_EXHAUSTED'], exit_cycle=pos.exit_cycle)")
    assert body[3] == "self._touch(now, 'PROTECTIVE_EXIT', 'BLOCK', b1_text, {'position_id': pos.position_id, 'codes': ['EXIT_ATTEMPTS_EXHAUSTED']})"
    assert [type(s).__name__ for s in ex.body[1:]] == ["Expr", "Expr", "Return"] and len(ex.body) == 4
    assert "ATTENTION" in ast.unparse(ex.body[1]) and "EXIT_SUBMISSION" in ast.unparse(ex.body[2])
