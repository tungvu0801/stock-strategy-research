"""
tests/test_forward_paper_610a.py - Stage 6.10A FORWARD PAPER AUTO-RUN + EVIDENCE CAPTURE (DESIGN_610A_FORWARD_PAPER_AUTORUN.md).

  A  the auto-run policy: MANUAL_ARM by default, the startup demotion (fail closed), the preflight, the automatic disarms, the opt-in scheduled arm
  C  the forward evidence capture: every candidate observation and decision, blocked / skipped READY candidates, the immutable entry snapshot,
     the outcome, the lineage candidate -> decision -> intent -> order -> position -> exit -> outcome, no duplicate logical row
  H  the operational session health (HEALTHY / DEGRADED / INVALID), kept apart from strategy results; exports; read paths never write
  R  robustness: recorder faults, a locked database, crash recovery, partial fill, rejected and ambiguous orders, single instance, Robinhood context
  F  the freeze: the change set, the order-decision logic unchanged (engine byte-identical, executor additions only _record markers), the hashes,
     replay pins of every edited file no ledger entry can hold, and the text bans
Alpaca PAPER only through the simulated paper broker; no network, no Robinhood, no live endpoint.
"""
import ast
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
import freeze_ledger as FL  # noqa: E402
import autorun as AR  # noqa: E402
import forward_capture as FC  # noqa: E402
from autorun import calendar as CAL  # noqa: E402
from autorun import policy as POL  # noqa: E402
from autorun import session as SES  # noqa: E402
from autorun.service import STARTUP, AutoRunService, StartupRefused, startup_step  # noqa: E402
from autorun.store import AutoRunStore  # noqa: E402
from execution import exposure_gate as G  # noqa: E402
from execution.engine import entry_coid, execution_id_for  # noqa: E402
from execution.service import SimulatedCrash  # noqa: E402
from forward.automation import AutomationStore  # noqa: E402
from forward_capture import exports as EXP  # noqa: E402
from forward_capture.capture import candidate_id  # noqa: E402
from forward_capture.store import ForwardCaptureStore, fid  # noqa: E402
from forward_validation import reports as REP  # noqa: E402
from test_cash_flow_602 import cash_config, cycle, make_rig, started  # noqa: E402,F401 - cash_config: the autouse config fixture of the shared rig
from test_setups_58 import READY_IN, WATCH_IN, fx  # noqa: E402

UTC = timezone.utc
DAY = "2026-10-21"                                                    # the rig's session: Wednesday, 11:00 New York at the start
AFTER_CLOSE = datetime(2026, 10, 21, 20, 20, tzinfo=UTC)              # 16:20 New York
FROZEN_V1 = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
CONTRACT_HASH_OFF = "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa"
CONTRACT_HASH_ON = "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"


# ================================================================ the rig ==================================================================
def rig(tmp_path, monkeypatch, name="s610a.db"):
    r = make_rig(tmp_path, monkeypatch, db_name=name)
    r.fv = r.s.forward_validation
    r.fv.clock = r.clock
    r.cap = r.fv.capture
    r.store = ForwardCaptureStore(r.db)
    return r


def cyc(r, advance=30):
    r.clock.advance(advance)
    return r.x.run_cycle(r.clock.now, "TEST")


def obs(r, advance=0):
    if advance:
        r.clock.advance(advance)
    return r.fv.observe(r.clock.now, "TEST")


def decisions(r, **kw):
    return r.store.decisions(limit=10000, **kw)


def by(r, decision):
    return [d for d in decisions(r) if d["decision"] == decision]


def go_ready(r, symbol="SNDK", trigger="SCHEDULED"):
    """WATCH -> READY through the evaluator as the market-hours loop runs it (trigger SCHEDULED: TEST evaluations are never evidence)."""
    r.setups.evaluate_symbol(symbol, trigger=trigger, inputs=fx(symbol, **WATCH_IN))
    r.clock.advance(60)
    r.setups.evaluate_symbol(symbol, trigger=trigger, inputs=fx(symbol, **READY_IN))
    pv = r.s.proposals.current(symbol, r.clock.now)
    assert pv.effective_status == "REVIEWABLE", (pv.effective_status, pv.blockers)
    r.sim.set_price(symbol, float(pv.reference_price))
    return pv


def armed(r, symbol="SNDK"):
    pv = go_ready(r, symbol)
    r.x.enable_auto(actor="test", now=r.clock.now)
    return pv


# ================================================================ C the forward evidence capture ===========================================
def test_C01_paper_auto_off_records_every_candidate_observation_and_no_intent(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    out = obs(r)                                                                       # the capture epoch: earlier evaluations are not evidence
    assert out["capture"]["result"] == "OK" and r.store.state()["epoch"]
    pv = go_ready(r)                                                                   # WATCH -> READY while Monitor only
    cyc(r)
    obs(r)
    assert r.x.mode()["mode"] == "MONITOR_ONLY" and r.x.store.intents() == []
    rows = decisions(r)
    assert [d["decision"] for d in rows] == ["NOT_READY", "SKIPPED"], [(d["decision"], d["reason_codes"]) for d in rows]
    skip = rows[1]
    assert skip["reason_codes"][0] == "PAPER_AUTO_OFF" and skip["mode"] == "MONITOR_ONLY" and skip["setup_state"] == "READY" and skip["source"] == "SETUP_EVALUATION"
    assert skip["candidate_id"] == candidate_id(pv.setup_id) == fid("candidate", pv.setup_id) and skip["market_session"] == "REGULAR"
    assert skip["features"]["stretch"] is not None and skip["score"] is not None and skip["account"]["status"] == "FRESH" and skip["proposal"]["status"] == "REVIEWABLE"
    assert "broker-side entry controls" in skip["detail"]["not_evaluated_in_monitor_only"]
    cands = r.store.candidates()
    assert len(cands) == 1 and cands[0]["setup_id"] == pv.setup_id and cands[0]["strategy_id"] == "DOWNSIDE_DISLOCATION_RECOVERY" and cands[0]["legacy"] == 0
    assert r.store.entry_snapshot(execution_id_for(pv.proposal_id)) is None


def close(r, pv):
    inv = Decimal(pv.invalidation_price) - Decimal("0.50")
    r.x.price_fn = lambda sym: (inv, "TEST", r.clock.now.isoformat(timespec="seconds"))
    r.sim.set_price(pv.symbol, float(inv))
    cyc(r)                                                                             # exit decided and POSTed
    cyc(r)                                                                             # exit filled -> CLOSED


def test_C02_a_ready_candidate_allowed_traces_candidate_decision_intent_order_position_exit_outcome(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv = armed(r)
    cyc(r)
    cyc(r)
    obs(r)
    pos = r.x.store.open_position("SNDK")
    assert pos is not None
    close(r, pv)
    obs(r)
    xid = execution_id_for(pv.proposal_id)
    cid = candidate_id(pv.setup_id)
    ent = by(r, "ENTRY_INTENT")
    assert len(ent) == 1 and ent[0]["execution_id"] == xid and ent[0]["candidate_id"] == cid and ent[0]["source"] == "EXECUTOR_CYCLE" and ent[0]["recovered"] == 0
    assert ent[0]["decision_id"] == fid("decision", "ENTRY_INTENT", xid) and ent[0]["mode"] == "PAPER_AUTO" and ent[0]["reason_codes"] == ["ALL_CONTROLS_PASSED"]
    assert ent[0]["controls_version"] == G.VERSION and ent[0]["contract_hash"] == CONTRACT_HASH_OFF
    assert {c["control_id"] for c in ent[0]["controls"]} == set(G.ENTRY_CONTROLS_V1) and all(c["result"] == "PASS" for c in ent[0]["controls"])
    snap = r.store.entry_snapshot(xid)
    it = r.x.store.intent(xid)
    assert snap["decision_id"] == ent[0]["decision_id"] and snap["recovered"] == 0 and snap["snapshot"]["planned"]["qty"] == it.qty
    assert snap["snapshot"]["planned"]["reference_price"] == it.reference_price and snap["snapshot"]["controls"]["version"] == G.VERSION
    assert snap["snapshot"]["account"]["equity"] and snap["snapshot"]["market"]["session"] == "REGULAR" and snap["snapshot"]["strategy"]["strategy_id"] == FC.STRATEGY_ID
    assert it.broker_order_id and it.client_order_id == entry_coid(xid) and r.x.store.order_events(it.broker_order_id)        # intent -> broker order
    p = r.x.store.position_for_execution(xid)
    entered, exited = by(r, "ENTERED"), by(r, "EXITED")
    assert [d["position_id"] for d in entered] == [p.position_id] == [d["position_id"] for d in exited]
    assert entered[0]["candidate_id"] == cid == exited[0]["candidate_id"] and exited[0]["reason_codes"] == ["HARD_INVALIDATION"]
    out = r.store.outcome(p.position_id)
    assert out["execution_id"] == xid and out["candidate_id"] == cid and out["cohort_id"] == snap["cohort_id"] and out["cross_cohort"] == 0
    o = out["outcome"]
    assert o["exit_reason"] == "HARD_INVALIDATION" and o["realized_pnl"] == p.realized_pnl and o["average_entry"] == p.average_entry_price and o["holding_seconds"] > 0
    assert o["controls_version"] == G.VERSION and o["entry_slippage"]["slippage_bps"] is not None
    assert o["excursions"]["marks"] >= 1 and "sampled observer marks" in o["excursions"]["basis"]                        # sampled, labelled - never invented
    from forward_capture import health as HL
    assert by(r, "BLOCKED") and HL.compute(r.cap, DAY, r.clock.now)["metrics"]["entries_blocked"] == 0   # blocked by its own entry: not a blocked candidate
    assert r.fv.overview(r.clock.now)["forward_evidence"]["cohorts"][0]["blocked_ready_candidates"] == 0
    cols, rows = REP.export_rows(r.fv, "trades")
    t = next(x for x in rows if x["position_id"] == p.position_id)
    assert (t["candidate_id"], t["entry_decision_id"], t["cohort_id"]) == (cid, ent[0]["decision_id"], snap["cohort_id"]) and "cohort_id" in cols


# ================================================================ A the auto-run policy ====================================================
@pytest.fixture(autouse=True)
def fresh_startup_state(monkeypatch):
    """The process-wide startup record of autorun/service.py as a normal lifespan start leaves it (the lifespan marked the executor and observer
    it started: autorun.service.mark_started). test_A14 starts from a fresh process (components_started False) through that real path."""
    for k, v in {"pending": False, "code": None, "text": None, "attempts": 0, "demoted": None, "at": None, "components_started": True, "passive": False,
                 "lease_taken": False, "owned_since": None}.items():
        monkeypatch.setitem(STARTUP, k, v)


def healthy_loops(monkeypatch, r, worker=True, observer=True):
    """The executor loop and the forward-validation observer as running threads report themselves (no thread is started in a test)."""
    import execution.scheduler as XS
    import forward_validation.scheduler as FS
    iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
    monkeypatch.setattr(XS, "status", lambda: {"running": worker, "in_cycle": False, "heartbeat_at": iso(r.clock.now), "next_at": iso(r.clock.now + timedelta(seconds=30)),
                                               "last_error": None})
    monkeypatch.setattr(FS, "status", lambda: {"running": observer})


def supervisor(r, **kw):
    calls = []
    s = AutoRunService(stream_fn=lambda: r.s, store=AutoRunStore(r.db), lease_store_fn=lambda: AutomationStore(r.db), clock=r.clock,
                       process_start=kw.pop("process_start", r.clock.now - timedelta(minutes=5)), start_fn=lambda: calls.append(1) or {"executor": True, "observer": True},
                       stop_fn=lambda: calls.append(-1))
    s.calls = calls
    return s


def policy(monkeypatch, name="MANUAL_ARM", enabled=False):
    monkeypatch.setattr(config, "PAPER_AUTO_RUN_POLICY", name)
    monkeypatch.setattr(config, "PAPER_AUTO_SCHEDULED_ARM_ENABLED", enabled)


def test_A01_a_process_start_never_keeps_paper_auto_armed_and_sends_nothing(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    r.x.enable_auto(actor="user", now=r.clock.now)
    assert r.x.mode()["mode"] == "PAPER_AUTO"
    posts = len(r.sim.posts)
    out = startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None)
    assert out["demoted"] is True and out["mode_before"] == "PAPER_AUTO" and r.x.mode()["mode"] == "MONITOR_ONLY" and len(r.sim.posts) == posts
    ev = r.x.store.mode_events(1)[0]
    assert (ev["from_mode"], ev["to_mode"], ev["actor"]) == ("PAPER_AUTO", "MONITOR_ONLY", "autorun_policy") and ev["reason"].startswith(AR.STARTUP_REASON)
    st = AutoRunStore(r.db)
    assert st.events(kind="STARTUP")[0]["code"] == AR.STARTUP_DISARM and st.day(DAY)["last_disarmed_by"] == "startup"
    assert st.lease(AR.INSTANCE_LEASE)["owner"] == AR.instance_id() and STARTUP["pending"] is False
    again = startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None)   # Monitor only already: nothing to demote
    assert again["demoted"] is False and r.x.store.mode_events(1)[0]["event_id"] == ev["event_id"]


def test_A02_startup_fails_closed_when_another_instance_owns_the_database_or_the_demotion_cannot_be_written(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    r.x.enable_auto(actor="user", now=r.clock.now)
    assert AutomationStore(r.db).acquire("other-instance:1", r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)
    with pytest.raises(StartupRefused) as e:
        startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None)
    assert e.value.code == "LEASE_HELD" and STARTUP["pending"] and STARTUP["passive"] and r.x.mode()["mode"] == "PAPER_AUTO"   # the owner's state is not touched
    r.clock.advance(AR.INSTANCE_LEASE_TTL_MIN * 60 + 1)                              # the other instance stopped renewing: its lease lapses
    real = r.x.disable_auto
    monkeypatch.setattr(r.x, "disable_auto", lambda **kw: (_ for _ in ()).throw(RuntimeError("database locked")))
    sleeps = []
    with pytest.raises(StartupRefused) as e:
        startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=sleeps.append)
    assert e.value.code == "DEMOTION_FAILED" and sleeps == [AR.STARTUP_DEMOTION_BACKOFF_S] * (AR.STARTUP_DEMOTION_ATTEMPTS - 1) and r.x.mode()["mode"] == "PAPER_AUTO"
    monkeypatch.setattr(r.x, "disable_auto", real)
    healthy_loops(monkeypatch, r)
    STARTUP.update(pending=True, passive=False, components_started=False)
    s = supervisor(r)
    t = s.tick()                                                                       # the supervisor retries: demote first, THEN start the executor and observer
    assert t["startup"]["result"] == "STARTED" and r.x.mode()["mode"] == "MONITOR_ONLY" and s.calls == [1] and STARTUP["pending"] is False


def test_A03_manual_arm_is_the_default_and_the_supervisor_never_arms(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    s0 = POL.settings_from(config)
    assert (s0["policy"], s0["effective_policy"], s0["scheduled_arm_enabled"], s0["disarm_at_session_end"]) == ("MANUAL_ARM", "MANUAL_ARM", False, True)
    assert (config.PAPER_AUTO_RUN_POLICY, config.PAPER_AUTO_SCHEDULED_ARM_ENABLED, config.AUTORUN_SUPERVISOR_ENABLED) == ("MANUAL_ARM", False, True)
    started(r)
    healthy_loops(monkeypatch, r)
    go_ready(r)
    s = supervisor(r)
    for _ in range(4):
        r.clock.advance(60)
        cycle(r)
        t = s.tick()
        assert t["preflight"] == "READY" and t["scheduled_arm"] is None and r.x.mode()["mode"] == "MONITOR_ONLY", (t, s.last_preflight)
    assert [e for e in AutoRunStore(r.db).events(limit=50) if e["kind"] == "SCHEDULED_ARM"] == [] and r.x.store.intents() == []
    policy(monkeypatch, "SCHEDULED_ARM", enabled=False)                               # the policy name alone is not enough
    s1 = POL.settings_from(config)
    assert s1["effective_policy"] == "MANUAL_ARM" and any("PAPER_AUTO_SCHEDULED_ARM_ENABLED" in p for p in s1["problems"])
    assert s.tick()["scheduled_arm"] is None and r.x.mode()["mode"] == "MONITOR_ONLY"
    policy(monkeypatch, "SCHEDULED", enabled=True)                                   # an unknown value is MANUAL_ARM (and the config check blocks)
    assert POL.settings_from(config)["effective_policy"] == "MANUAL_ARM" and s.tick()["preflight"] == "BLOCKED"


def test_A04_the_opt_in_scheduled_arm_needs_every_condition_and_runs_once_per_trading_date(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.clock.now = datetime(2026, 10, 21, 13, 31, tzinfo=UTC)                         # 09:31 New York
    started(r)
    healthy_loops(monkeypatch, r)
    policy(monkeypatch, "SCHEDULED_ARM", enabled=True)
    s = supervisor(r)
    t = s.tick()                                                                       # inside the arm delay after the open
    assert t["scheduled_arm"]["result"] == "NOT_ELIGIBLE" and any("after the open" in x for x in t["scheduled_arm"]["reasons"]), t
    r.clock.advance(120)
    cycle(r)
    t = s.tick()
    assert t["scheduled_arm"]["result"] == "ARMED" and r.x.mode()["mode"] == "PAPER_AUTO", (t, s.last_preflight)
    ev = r.x.store.mode_events(1)[0]
    assert ev["actor"] == AR.ACTOR_SCHEDULED_ARM and ev["reason"].startswith("SCHEDULED_ARM: all conditions met")
    day = AutoRunStore(r.db).day(DAY)
    assert day["scheduled_arm_result"] == "ARMED" and day["scheduled_arm_at"] and day["first_armed_by"] == AR.ACTOR_SCHEDULED_ARM
    r.x.disable_auto(actor="user", reason="a person stops it", now=r.clock.now)
    r.clock.advance(60)
    t = s.tick()                                                                       # once per trading date, and never after a person disarmed
    assert t["scheduled_arm"]["result"] == "NOT_ELIGIBLE" and r.x.mode()["mode"] == "MONITOR_ONLY"
    assert any("already ran today" in x for x in t["scheduled_arm"]["reasons"]) and any("disarmed today by user" in x for x in t["scheduled_arm"]["reasons"]), t


def test_A05_the_scheduled_arm_never_arms_outside_the_regular_session_on_a_holiday_or_near_the_close(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    policy(monkeypatch, "SCHEDULED_ARM", enabled=True)
    s = supervisor(r)
    for when, why in ((datetime(2026, 10, 21, 13, 0, tzinfo=UTC), "PREMARKET"), (datetime(2026, 10, 21, 19, 50, tzinfo=UTC), "near-close"),
                      (datetime(2026, 10, 21, 21, 0, tzinfo=UTC), "AFTER_HOURS"), (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), "CLOSED"),
                      (datetime(2026, 10, 24, 15, 0, tzinfo=UTC), "CLOSED")):
        r.clock.now = when
        cycle(r)
        t = s.tick()
        assert t["scheduled_arm"]["result"] == "NOT_ELIGIBLE" and any(why in x for x in t["scheduled_arm"]["reasons"]) and r.x.mode()["mode"] == "MONITOR_ONLY", (when, t)
    assert SES.session_of(datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) == "CLOSED" and CAL.is_holiday(datetime(2026, 11, 26).date())   # Thanksgiving
    assert SES.classify(datetime(2026, 11, 27, 18, 30, tzinfo=UTC))["session"] == "AFTER_HOURS"                    # the 13:00 early close
    assert SES.classify(datetime(2026, 10, 21, 15, 0, tzinfo=UTC), {"is_open": False})["session"] == "CLOSED"     # the broker clock wins


def test_A06_preflight_ready_then_an_emergency_stop_blocks_and_disarms_while_exits_keep_running(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    s = supervisor(r)
    t = s.tick()
    pf = AutoRunStore(r.db).last_preflight()
    assert t["preflight"] == "READY" == pf["status"] and pf["trigger"] == "STARTUP" and t["disarm"] is None, s.last_preflight
    assert {c["id"] for c in pf["checks"]} >= {"config", "database", "single_instance", "foreign_orders", "paper_proof", "emergency_stop", "reconciliation", "open_orders",
                                                "controls_version", "executor_loop_enabled", "worker", "account_equity", "evidence_observer", "market_clock",
                                                "historical_attention", "external_accounts", "evidence_capture"}
    pv = armed(r)
    cyc(r)
    cyc(r)
    assert r.x.store.open_position("SNDK") is not None
    posts = len(r.sim.posts)
    r.s.safety.emergency_stop(reason="operator", now=r.clock.now)
    t = s.tick()
    assert t["preflight"] == "BLOCKED" and t["disarm"]["result"] == "DISARMED" and t["disarm"]["codes"][0] == "EMERGENCY_STOP" and r.x.mode()["mode"] == "MONITOR_ONLY"
    ev = AutoRunStore(r.db).events(kind="AUTO_DISARM")[0]
    assert ev["code"] == "EMERGENCY_STOP" and ev["actor"] == AR.ACTOR_POLICY and AutoRunStore(r.db).day(DAY)["last_disarm_code"] == "EMERGENCY_STOP"
    assert "emergency_stop" in s.last_preflight["blocking"]
    assert r.x.needs_broker() and len(r.sim.posts) == posts                         # disarm sent nothing; the open position is still monitored
    close(r, pv)                                                                       # ... and its exit still runs in Monitor only (never stranded)
    assert [p.state for p in r.x.store.positions(open_only=False)] == ["CLOSED"] and len(r.sim.posts) == posts + 1
    assert s.tick()["disarm"] is None                                                  # Monitor only already: nothing more to do


def healthy_facts(now):
    lc = SES.last_close(now)
    return {"settings": POL.settings_from(config), "db": {"readable": True}, "lease": {"held_by_self": True}, "foreign": {"orders": []}, "broker_configured": True,
            "proof": {"state": "PROVEN", "definitive": False}, "account": {"source": "SIMULATED_PAPER", "status": "FRESH", "consecutive_failures": 0, "last_attempt_at": None},
            "safety": {"kill_active": False, "state": "TRADING_ALLOWED", "allowed_new_entry": True}, "intents": {"open": 0, "reconciliation_required": 0, "attempted_unresolved": 0},
            "positions": {"open": 0, "attention": 0}, "recon": {"reconciled": True, "healthy": True, "reasons": []},
            "gate": {"version": G.VERSION, "supported": True, "problems": [], "enabled": False}, "scheduler_enabled": True, "worker": {"state": "ALIVE", "text": "alive"},
            "observer": {"running": True}, "session": SES.classify(now), "clock": None, "attention": {"critical": []}, "external": {"state": "PARTIAL"}, "capture": {"faults_today": 0},
            "mode": {"mode": "PAPER_AUTO", "changed_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"), "event_id": "e1"},
            "last_close": lc.isoformat(timespec="seconds") if lc else None, "open_at": SES.open_of(now).isoformat(timespec="seconds"),
            "process_start": (now - timedelta(hours=1)).isoformat(timespec="seconds"), "trading_date": DAY, "foreign_executor": [], "disarms_today": []}


def codes(f, mem, now):
    return [c for c, _ in POL.disarm_conditions(f, mem, now)]


def test_A07_every_disarm_condition_is_deterministic_and_transient_noise_never_disarms():
    now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    f = healthy_facts(now)
    assert codes(f, {}, now) == [] and POL.preflight(f)["status"] == "READY"
    iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
    cases = [({"safety": {**f["safety"], "kill_active": True}}, {}, "EMERGENCY_STOP"),
             ({"proof": {"state": "LOST", "definitive": True, "text": "HTTP_401"}}, {}, "PAPER_PROOF_LOST"), ({"broker_configured": False}, {}, "PAPER_PROOF_LOST"),
             ({"intents": {**f["intents"], "open": 1, "reconciliation_required": 1}}, {}, "RECONCILIATION_AMBIGUITY"),
             ({"intents": {**f["intents"], "open": 1, "attempted_unresolved": 1}}, {}, "RECONCILIATION_AMBIGUITY"),
             ({"positions": {"open": 1, "attention": 1}}, {}, "RECONCILIATION_AMBIGUITY"),
             ({"account": {**f["account"], "status": "STALE"}}, {"equity_unavailable_since": iso(now - timedelta(seconds=300))}, "ACCOUNT_EQUITY_UNAVAILABLE"),
             ({"account": {**f["account"], "consecutive_failures": 3, "last_attempt_at": iso(now)}}, {}, "BROKER_READ_FAILURES"),
             ({"worker": {"state": "STALE", "text": "no heartbeat"}}, {}, "WORKER_FATAL"), ({"worker": {"state": "STOPPED", "text": "gone"}}, {}, "WORKER_FATAL"),
             ({"worker": {"state": "ERROR", "text": "x"}}, {"worker_error_ticks": 3}, "WORKER_FATAL"), ({"db": {"readable": False, "error": "OperationalError"}}, {}, "DATABASE_FAILURE"),
             ({"gate": {**f["gate"], "supported": False, "version": "entry_controls_v9"}}, {}, "UNSUPPORTED_CONTROLS"),
             ({"lease": {"held_by_self": False, "owner": "x:1", "other_renewing": True}}, {}, "DUPLICATE_INSTANCE"), ({"foreign_executor": ["x:2"]}, {}, "DUPLICATE_INSTANCE"),
             ({"foreign": {"orders": [{"client_order_id": "stockagent-entry-zz"}]}}, {}, "FOREIGN_INSTANCE"),
             ({"mode": {**f["mode"], "changed_at": iso(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))}}, {}, "SESSION_ENDED")]
    for patch, mem, code in cases:
        assert code in codes({**f, **patch}, mem, now), (code, patch)
    quiet = [({"account": {**f["account"], "status": "STALE"}}, {"equity_unavailable_since": iso(now - timedelta(seconds=299))}),          # under 300 s
             ({"account": {**f["account"], "consecutive_failures": 3, "last_attempt_at": iso(now - timedelta(hours=2))}}, {}),                # a previous process's failures
             ({"worker": {"state": "ERROR", "text": "x"}}, {"worker_error_ticks": 2}), ({"worker": {"state": "RUNNING", "text": "in a cycle"}}, {}),
             ({"proof": {"state": "FAILED", "definitive": False, "text": "TIMEOUT"}}, {}), ({"lease": {"held_by_self": False, "owner": "x:1", "other_static": True}}, {}),
             ({"gate": {**f["gate"], "enabled": False, "problems": []}}, {}), ({"external": {"state": "UNAVAILABLE"}}, {}), ({"attention": {"critical": ["LIVE_HOST_ATTEMPT"]}}, {}),
             ({"capture": {"faults_today": 9}}, {}), ({"clock": None}, {})]
    for patch, mem in quiet:
        assert codes({**f, **patch}, mem, now) == [], patch
    off = {**f, "settings": {**f["settings"], "disarm_at_session_end": False}, "mode": {**f["mode"], "changed_at": iso(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))}}
    assert codes(off, {}, now) == []                                                   # PAPER_AUTO_DISARM_AT_SESSION_END=false keeps it over the close
    assert POL.proof_state({"paper": False, "error": "TIMEOUT"}, 1)["definitive"] is False and POL.proof_state({"paper": False, "error": "TIMEOUT"}, 3)["definitive"] is True
    assert POL.proof_state({"paper": False, "error": "HTTP_401"}, 0)["state"] == "LOST" and POL.proof_state({}, 0)["state"] == "NOT_YET"
    hb = {"running": True, "in_cycle": False, "heartbeat_at": iso(now), "next_at": iso(now + timedelta(seconds=299))}
    assert POL.worker_liveness(hb, now + timedelta(seconds=298))["state"] == "ALIVE"                       # a healthy loop asleep until its next decision
    assert POL.worker_liveness(hb, now + timedelta(seconds=299 + AR.LIVENESS_GRACE_SECONDS + 1))["state"] == "STALE"
    for env in ({"paper": True, "status": "ACTIVE", "blocked": True}, {"paper": True, "status": "ACCOUNT_CLOSED", "blocked": False}):
        pr = POL.proof_state(env, 0)
        assert pr["state"] == "LOST" and pr["definitive"] is True, env                  # a PAPER account that cannot trade is no paper proof
        assert "PAPER_PROOF_LOST" in codes({**f, "proof": pr}, {}, now) and "paper_proof" in POL.preflight({**f, "proof": pr})["blocking"]
    assert POL.proof_state({"paper": True, "status": "ACTIVE", "blocked": False}, 0)["state"] == "PROVEN"
    err = {"running": True, "in_cycle": False, "heartbeat_at": iso(now), "next_at": iso(now + timedelta(seconds=30)), "last_result": {"result": "ERROR", "error": "KeyError"}}
    assert POL.worker_liveness(err, now)["state"] == "ERROR"                           # a cycle that failed inside run_cycle is a loop error too
    slow = {"running": True, "in_cycle": False, "heartbeat_at": iso(now), "next_at": iso(now + timedelta(seconds=30)), "last_finished_at": iso(now + timedelta(seconds=100))}
    assert POL.worker_liveness(slow, now + timedelta(seconds=100.5))["state"] == "ALIVE"   # a 100 s cycle: the loop decides again on finishing
    hung = {"running": True, "in_cycle": True, "cycle_started_at": iso(now - timedelta(seconds=601)), "heartbeat_at": iso(now - timedelta(seconds=601))}
    assert POL.worker_liveness(hung, now)["state"] == "STALE" and "WORKER_FATAL" in codes({**f, "worker": POL.worker_liveness(hung, now)}, {}, now)
    assert "DATABASE_FAILURE" in codes(f, {"db_failure": {"since": iso(now), "error": "OperationalError"}}, now)
    from autorun.facts import fresh_clock
    assert fresh_clock({"is_open": False, "timestamp": iso(now - timedelta(hours=20))}, now) is None   # yesterday's 'closed' never relabels today
    assert fresh_clock({"is_open": True, "timestamp": iso(now - timedelta(seconds=30))}, now)["is_open"] is True and fresh_clock(None, now) is None
    blocked = {**f, "intents": {**f["intents"], "open": 1, "reconciliation_required": 1}}
    assert POL.preflight(blocked)["status"] == "BLOCKED" and "reconciliation" in POL.preflight(blocked)["blocking"]
    assert POL.preflight({**f, "account": {**f["account"], "status": "STALE"}})["status"] == "DEGRADED"


def test_A08_the_supervisor_disarms_on_stale_equity_a_session_close_and_a_foreign_bot_order(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    s = supervisor(r)
    armed(r)
    assert s.tick()["disarm"] is None
    real = r.ref.freshness
    monkeypatch.setattr(r.ref, "freshness", lambda now=None: {**real(now), "status": "STALE", "consecutive_failures": 1})
    s.tick()
    r.clock.advance(AR.EQUITY_UNAVAILABLE_SECONDS - 1)
    assert s.tick()["disarm"] is None                                                  # stale, but not for 300 s yet
    r.clock.advance(2)
    t = s.tick()
    assert t["disarm"]["result"] == "DISARMED" and "ACCOUNT_EQUITY_UNAVAILABLE" in t["disarm"]["codes"] and r.x.mode()["mode"] == "MONITOR_ONLY"
    monkeypatch.setattr(r.ref, "freshness", real)
    cycle(r)
    r.x.enable_auto(actor="user", now=r.clock.now)                                     # re-armed by a person: a new arm event
    r.clock.now = datetime(2026, 10, 21, 20, 1, tzinfo=UTC)                           # 16:01 New York: the session it was armed for has closed
    cycle(r)
    t = s.tick()
    assert t["disarm"]["codes"] == ["SESSION_ENDED"] and r.x.mode()["mode"] == "MONITOR_ONLY"
    snap = r.ref.store.snapshot()
    monkeypatch.setattr(r.ref.store, "snapshot", lambda: {**snap, "open_orders": [{"client_order_id": "stockagent-entry-" + "f" * 24, "symbol": "AAPL", "status": "new"}]})
    r.x.enable_auto(actor="user", now=r.clock.now)
    t = s.tick()
    assert "FOREIGN_INSTANCE" in t["disarm"]["codes"] and t["preflight"] == "BLOCKED" and r.x.mode()["mode"] == "MONITOR_ONLY"
    assert [e["code"] for e in AutoRunStore(r.db).events(kind="AUTO_DISARM", limit=10)] == ["FOREIGN_INSTANCE", "SESSION_ENDED", "ACCOUNT_EQUITY_UNAVAILABLE"]


def test_A09_single_instance_a_live_owner_keeps_this_process_passive_and_a_crashed_owners_lease_is_taken_over(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    other, lease = "other-instance:2", AutomationStore(r.db)
    assert lease.acquire(other, r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)
    r.x.enable_auto(actor="user", now=r.clock.now)                                     # the OTHER instance's Paper Auto
    STARTUP.update(pending=True, passive=True, components_started=False, code="LEASE_HELD")   # this process's lifespan was refused (test_A02)
    s = supervisor(r)
    t = s.tick()
    assert t["lease"] == "WAITING" and s.calls == [] and "disarm" not in t
    r.clock.advance(60)
    assert lease.acquire(other, r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)          # the owner is alive: it renews
    t = s.tick()
    assert t["lease"] == "DUPLICATE" and t["preflight"] == "BLOCKED" and "single_instance" in s.last_preflight["blocking"]
    assert s.calls == [] and r.x.mode()["mode"] == "PAPER_AUTO" and "disarm" not in t                           # passive: no executor here, the owner's mode untouched
    out = obs(r)
    assert out["capture"]["result"] == "NOT_OWNER" and r.store.faults()[0]["code"] == "DUPLICATE_INSTANCE" and decisions(r) == []   # and no evidence written here
    r.clock.advance(AR.INSTANCE_LEASE_TTL_MIN * 60 + 1)                               # the owner crashed: no renewal, its lease lapses
    t = s.tick()
    assert t["lease"] == "HELD" and t["startup"]["result"] == "STARTED" and s.calls == [1] and r.x.mode()["mode"] == "MONITOR_ONLY"   # taken over: demoted, then started
    assert lease.acquire(other, r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE) is False   # a restarted old owner cannot take it back
    from autorun.scheduler import AutoRunScheduler
    sch = AutoRunScheduler(lambda: s, startup_delay_s=3600)
    try:
        assert sch.start() is True and sch.start() is False                            # exactly one supervisor thread per process
    finally:
        assert sch.stop(timeout=5) is True
    import autorun.scheduler as ASCH
    assert AutoRunStore(r.db).lease(AR.INSTANCE_LEASE)["owner"] == AR.instance_id()   # the supervisor stops first; the lease is released LAST ...
    ASCH.release_lease()
    assert AutoRunStore(r.db).lease(AR.INSTANCE_LEASE) is None                         # ... after the executor (the lifespan's last step)


def test_A10_a_process_that_loses_the_lease_disarms_at_once_and_stops_its_executor_only_when_the_new_owner_runs_one(tmp_path, monkeypatch):
    from execution.store import ExecutionStore
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    s = supervisor(r)
    assert s.tick()["lease"] == "HELD"
    armed(r)
    lease = AutomationStore(r.db)
    r.clock.advance(AR.INSTANCE_LEASE_TTL_MIN * 60 + 1)                               # this process stalled past its lease: another instance took it ...
    assert lease.acquire("other-instance:3", r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)
    s.tick()
    r.clock.advance(30)
    assert lease.acquire("other-instance:3", r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)   # ... and renews it
    t = s.tick()
    assert t["lost_lease"] == {"result": "KEPT_RUNNING", "disarmed": True} and s.calls == [] and STARTUP["passive"] is False   # no executor seen there:
    assert r.x.mode()["mode"] == "MONITOR_ONLY" and AutoRunStore(r.db).events(kind="LEASE_LOST")[0]["code"] == "DUPLICATE_INSTANCE"   # this one keeps exits
    ExecutionStore(r.db).add_run("f" * 32, r.clock.now, r.clock.now, "SCHEDULED", "MONITOR_ONLY", "RUN", {}, None, "other-executor:9")   # the new owner's cycle
    r.clock.advance(30)
    assert lease.acquire("other-instance:3", r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)
    t = s.tick()
    assert t["lost_lease"]["result"] == "PASSIVE" and s.calls == [-1] and STARTUP["passive"] is True and t["preflight"] == "BLOCKED"


def gate_on(monkeypatch, r, symbol="SNDK"):
    """The Stage 6.9.1 gate ON with another (read-only, real) account holding `symbol` - SENTINEL values that must never reach the forward dataset."""
    monkeypatch.setattr(config, "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", True)
    at = r.clock.now.isoformat(timespec="seconds")
    u = {"state": "COMPLETE", "sources": [{"source": "ALPACA_PAPER", "account_type": "PAPER", "state": "LOADED", "refreshed_at": at, "age_s": 5, "label": "Paper"},
                                         {"source": "ROBINHOOD", "account_type": "REAL", "state": "LOADED", "refreshed_at": at, "age_s": 7, "label": "Holdings SENTINELLABEL"}],
         "rows": [{"symbol": symbol, "source": "ROBINHOOD", "account_type": "REAL", "ownership": "USER_MANAGED", "quantity": "7.777123", "market_value": "98765.43"}],
         "exposure": {"total_market_value": "98765.43"}, "warnings": ["SENTINELWARNING"], "account_number": "SENTINELACCT99"}
    monkeypatch.setattr(r.s, "unified_portfolio", lambda refresh=True: u)
    return u


def test_C03_ready_candidates_blocked_by_safety_session_entry_limit_and_external_exposure_are_all_captured(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    for sym in ("NVDA", "SNDK"):
        go_ready(r, sym)
    r.x.enable_auto(actor="test", now=r.clock.now)
    r.s.safety.emergency_stop(reason="test", now=r.clock.now)
    cyc(r)                                                                             # every READY candidate blocked by the risk gate (no supervisor here)
    r.s.safety.clear_emergency_stop(reason="test", now=r.clock.now)
    cycle(r)
    gate_on(monkeypatch, r, "SNDK")
    cyc(r)                                                                             # NVDA allowed (the per-cycle limit is then used); SNDK held elsewhere -> blocked
    obs(r)
    rows = [d for d in decisions(r) if d["source"] == "EXECUTOR_CYCLE"]
    first = [d for d in rows if d["run_id"] == rows[0]["run_id"]]
    assert sorted(d["symbol"] for d in first) == ["NVDA", "SNDK"] and all(d["decision"] == "BLOCKED" and "SAFETY_GATE" in d["reason_codes"] for d in first), first
    ctrl = {c["control_id"]: c for c in first[0]["controls"]}
    assert ctrl["SAFETY_GATE"]["result"] == "BLOCK" and first[0]["controls_version"] == G.VERSION and first[0]["account"]["status"] == "ACTIVE"
    second = [d for d in rows if d["run_id"] != rows[0]["run_id"]]
    got = {d["symbol"]: d for d in second}
    assert got["NVDA"]["decision"] == "ENTRY_INTENT" and got["NVDA"]["contract_hash"] == CONTRACT_HASH_ON, second
    if got["SNDK"]["decision"] == "SKIPPED":                                           # the episode order decides which one the per-cycle limit leaves
        assert got["SNDK"]["reason_codes"] == ["ENTRIES_PER_CYCLE"]
        cyc(r)
        obs(r)
        got["SNDK"] = [d for d in decisions(r) if d["symbol"] == "SNDK" and d["source"] == "EXECUTOR_CYCLE"][-1]
    sn = got["SNDK"]
    assert sn["decision"] == "BLOCKED" and "UNIFIED_EXTERNAL_EXPOSURE" in sn["reason_codes"], sn
    g = {c["control_id"]: c for c in sn["controls"]}["UNIFIED_EXTERNAL_EXPOSURE"]
    assert g["result"] == "BLOCK" and g["input"]["reason_code"] == "EXTERNAL_SYMBOL_ALREADY_HELD" and g["reason"] == FC.GATE_LABELS["EXTERNAL_SYMBOL_ALREADY_HELD"]
    assert sn["exposure"]["gate_enabled"] is True and sn["exposure"]["external"]["unified_state"] == "COMPLETE"
    assert {s["source"] for s in sn["exposure"]["external"]["sources"]} == {"ALPACA_PAPER", "ROBINHOOD"}
    r.clock.now = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)                           # after the close: a Paper Auto cycle cannot enter
    cycle(r)
    cyc(r)
    obs(r)
    late = [d for d in decisions(r) if d["source"] == "EXECUTOR_CYCLE" and d["decided_at"] >= "2026-10-21T21:00"]
    assert late and all(d["decision"] == "BLOCKED" and "SESSION_POLICY" in d["reason_codes"] and d["market_session"] == "AFTER_HOURS" for d in late), late
    assert all(i.symbol == "NVDA" for i in r.x.store.intents())                       # no intent from any blocked or skipped candidate


def dump_capture(db):
    """Every row of every forward-capture and auto-run table as text (the privacy scan's corpus)."""
    conn = sqlite3.connect(str(db))
    try:
        names = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND (name LIKE 'forward_capture_%' OR name LIKE 'autorun_%')")]
        return "\n".join(json.dumps(list(row), default=str) for n in names for row in conn.execute(f"SELECT * FROM {n}"))
    finally:
        conn.close()


def test_C04_no_external_holding_account_number_or_credential_reaches_the_forward_dataset_its_exports_or_views(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    r.sim.account["account_number"] = "SENTINELACCT42"
    gate_on(monkeypatch, r, "SNDK")
    healthy_loops(monkeypatch, r)
    s = supervisor(r)
    s.tick()
    obs(r)
    armed(r)                                                                           # the paper proof fills the executor's environment cache ...
    s.tick()                                                                           # ... which the supervisor's preflight then reads
    cyc(r)
    cyc(r)
    obs(r)
    r.clock.advance(3700)
    s.tick()                                                                           # the hourly preflight persistence
    from execution import activity as ACT
    corpus = dump_capture(r.db)
    for kind in FC.EXPORT_KINDS:
        cols, rows = REP.export_rows(r.fv, kind)
        corpus += REP.to_csv(cols, rows) + REP.to_json(kind, cols, rows)
    a = ACT.activity(r.x, scheduler_status={"running": False}, enabled=True, now=r.clock.now)
    corpus += json.dumps({"auto_run": a["auto_run"], "forward_session": a["forward_session"]}, default=str) + json.dumps(r.fv.overview(r.clock.now)["forward_evidence"], default=str)
    assert decisions(r) and "SNDK" in corpus
    assert "paper proof (" in json.dumps(AutoRunStore(r.db).preflights(limit=50)) and len(AutoRunStore(r.db).preflights(limit=50)) >= 2
    for sentinel in ("SENTINELLABEL", "SENTINELWARNING", "SENTINELACCT99", "SENTINELACCT42", "7.777123", "98765.43", "CASHFLOWTEST-paperkey", "CASHFLOWTEST-papersecret",
                     "account_number", "APCA-API", "secret_key", "paper-api.alpaca.markets", "projected_pct"):
        assert sentinel not in corpus, sentinel


def test_C05_evidence_is_immutable_every_write_path_is_idempotent_and_one_candidate_is_one_row(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv = armed(r)
    for _ in range(4):
        cyc(r)
        obs(r)
    xid = execution_id_for(pv.proposal_id)
    assert len(r.store.candidates()) == 1 and len(by(r, "ENTRY_INTENT")) == 1 and len(by(r, "ENTERED")) == 1
    before = {k: v for k, v in r.store.counts().items() if k != "forward_capture_context"}
    from forward_capture.capture import ForwardCapture
    again = ForwardCapture(r.fv)                                                       # a fresh capture on the same database re-derives every path
    assert again.tick(r.clock.now)["result"] == "OK" and again.tick(r.clock.now)["result"] == "OK"
    assert {k: v for k, v in r.store.counts().items() if k != "forward_capture_context"} == before and not [f for f in r.store.faults() if f["code"] == "CONFLICT"]
    conn = sqlite3.connect(str(r.db))
    try:
        for sql in ("UPDATE forward_capture_decisions SET decision = 'X'", "DELETE FROM forward_capture_decisions", "UPDATE forward_capture_entry_snapshots SET snapshot_json = '{}'",
                    "DELETE FROM forward_capture_entry_snapshots", "UPDATE forward_capture_candidates SET symbol = 'X'", "DELETE FROM forward_capture_session_health",
                    "DELETE FROM forward_capture_state", "UPDATE autorun_events SET code = 'X'", "DELETE FROM autorun_preflights"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
    finally:
        conn.close()
    snap = r.store.entry_snapshot(xid)
    forged = {"execution_id": xid, "candidate_id": snap["candidate_id"], "decision_id": snap["decision_id"], "cohort_id": snap["cohort_id"], "captured_at": snap["captured_at"],
              "snapshot_json": json.dumps({"planned": {"qty": 999}}), "snapshot_sha256": "0" * 64, "recovered": 0}
    res = r.store.put([("forward_capture_entry_snapshots", forged)], r.clock.now)        # a later different 'snapshot' of the same entry
    assert res["conflicts"] == [{"table": "forward_capture_entry_snapshots", "id": xid}] and r.store.entry_snapshot(xid)["snapshot"] == snap["snapshot"]
    assert [f["code"] for f in r.store.faults()] == ["CONFLICT"]                      # recorded as an evidence fault, never an overwrite


def test_C06_strategy_and_control_versions_the_config_snapshot_and_the_cohort_are_recorded_and_a_change_starts_a_new_cohort(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    snaps = r.store.config_snapshots()
    assert sorted(s["trigger"] for s in snaps) == ["DAY", "STARTUP"] and len({s["config_hash"] for s in snaps}) == 1
    c = snaps[0]["config"]
    import forward_validation as FV
    assert c["frozen_controls_hash"] == FV.frozen_hash() and c["entry_controls"]["version"] == G.VERSION and c["entry_controls"]["contract_hash"] == CONTRACT_HASH_OFF   # (FROZEN_V1: F03)
    assert c["autorun"]["effective_policy"] == "MANUAL_ARM" and c["executor_limits"]["MAX_OPEN_POSITIONS"] == config.MAX_OPEN_POSITIONS and c["broker"]
    assert c["universe"]["symbols"] == ["AMD", "NVDA", "SNDK"] and c["calendar"] == CAL.CALENDAR_VERSION and c["versions"]["setups"] == "setups_v1"
    assert [x["strategy_id"] for x in c["strategies"] if x["executes"]] == [FC.STRATEGY_ID]                    # research strategies never reach the executor
    keys = json.dumps(c).lower()
    assert not re.search(r'"[^"]*(secret|password|token|api_key|apikey|account_number)[^"]*":', keys)
    co = r.store.cohorts()
    assert len(co) == 1 and co[0]["label"] == "FP_2026_01" and co[0]["cohort_id"] == "FP-" + co[0]["cohort_key"][:12] and snaps[0]["cohort_id"] == co[0]["cohort_id"]
    go_ready(r)
    obs(r)
    assert {d["cohort_id"] for d in decisions(r)} == {co[0]["cohort_id"]} and r.store.candidates()[0]["strategy_version"].endswith("/setups_v1")
    monkeypatch.setattr(r.s, "watchlist_fn", lambda: ["NVDA", "AMD", "SNDK", "MU"])  # the universe changes midday: a new snapshot, the same cohort
    obs(r, 60)
    assert r.store.config_snapshots(limit=1)[0]["trigger"] == "CHANGE" and len(r.store.cohorts()) == 1
    monkeypatch.setattr(config, "EXECUTION_MAX_SHARES_PER_ENTRY", 1)                 # a decision-relevant limit changes: a NEW cohort, never pooled silently
    obs(r, 60)
    co2 = r.store.cohorts()
    assert [x["label"] for x in co2] == ["FP_2026_01", "FP_2026_02"] and r.store.config_snapshots(limit=1)[0]["cohort_id"] == co2[1]["cohort_id"]
    r.setups.evaluate_symbol("SNDK", trigger="SCHEDULED", inputs=fx("SNDK", **READY_IN))
    obs(r, 60)
    assert {d["cohort_id"] for d in decisions(r)} == {co2[0]["cohort_id"], co2[1]["cohort_id"]}
    monkeypatch.setattr(config, "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", True)     # the gate switched ON: the entry contract changes, so does the cohort
    obs(r, 60)
    assert r.store.config_snapshots(limit=1)[0]["config"]["entry_controls"]["contract_hash"] == CONTRACT_HASH_ON and len(r.store.cohorts()) == 3
    r.setups.evaluate_symbol("SNDK", trigger="SCHEDULED", inputs=fx("SNDK", **READY_IN))
    obs(r, 60)
    last = decisions(r)[-1]
    assert last["contract_hash"] == CONTRACT_HASH_ON and last["cohort_id"] == r.store.cohorts()[-1]["cohort_id"]
    co2 = r.store.cohorts()
    h = r.store.health(DAY)
    assert h["status"] != "HEALTHY" and "MIXED_COHORTS" in h["reasons"] and sorted(h["cohort_ids"]) == sorted(x["cohort_id"] for x in co2)   # (coverage: test H01)


# ================================================================ H the operational session health ========================================
OPEN_AT = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)                                 # 09:30 New York
CLOSE_AT = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)                                 # 16:00 New York


def observed_day(r, monkeypatch, *, worker=True, during=None, step_min=30, paused_from=None):
    """A whole regular session observed at a 30-minute cadence (the coverage cadences set to match), then the post-close observation."""
    monkeypatch.setattr(config, "FORWARD_VALIDATION_INTERVAL_SECONDS", step_min * 60.0)
    monkeypatch.setattr(config, "SETUP_EVAL_INTERVAL_SECONDS", step_min * 60.0)
    healthy_loops(monkeypatch, r, worker=worker)
    r.clock.now = OPEN_AT
    started(r)
    obs(r)
    n = 0
    while r.clock.now + timedelta(minutes=step_min) < CLOSE_AT:
        r.clock.advance(step_min * 60)
        cycle(r)
        paused = paused_from is not None and n >= paused_from
        r.setups.evaluate_symbol("NVDA", trigger="SCHEDULED", inputs=fx("NVDA", **WATCH_IN, **({"ppe_status": "NOT_AVAILABLE"} if paused else {})))
        if during is not None:
            during(r, n)
        obs(r)
        n += 1
    r.clock.now = AFTER_CLOSE
    cycle(r)
    return obs(r)


def test_H01_a_fully_observed_day_is_healthy_even_with_a_losing_trade_and_its_summary_is_operational_only(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    trade = {}

    def during(r, n):
        if n == 2:
            trade["pv"] = armed(r, "SNDK")
            cyc(r)
            cyc(r)
        if n == 4:
            close(r, trade["pv"])                                                     # closed at the invalidation level: a LOSS
            r.x.disable_auto(actor="user", now=r.clock.now)
    out = observed_day(r, monkeypatch, during=during)
    assert out["capture"]["health"] == {"trading_date": DAY, "status": "HEALTHY", "final": True}, (r.store.health(DAY) or {}).get("reasons")
    h = r.store.health(DAY)
    p = [x for x in r.x.store.positions(open_only=False) if x.state == "CLOSED"][0]
    assert Decimal(p.realized_pnl) < 0 and h["final"] == 1 and h["source_session_status"] == "HEALTHY"           # a losing session can be HEALTHY
    m = h["metrics"]
    assert m["worker_coverage"] == 1.0 and m["account_fresh_coverage"] == 1.0 and m["cycles_expected"] == int(390 * 60 / config.EXECUTION_CYCLE_SECONDS)
    assert m["entries_attempted"] == 1 and m["fills"] == 2 and m["exits"] == 1 and m["paper_auto"] and m["window"]["expected_minutes"] == 390
    assert not {"realized_pnl", "pnl", "win_rate", "return_pct", "expectancy"} & set(m)                            # no P/L input: operational only
    lines = h["summary_text"].splitlines()
    assert lines[0] == "Market session: HEALTHY" and [ln.split(":")[0] for ln in lines] == ["Market session", "Candidates observed", "READY", "Blocked", "Entered", "Open",
                                                                                          "Closed", "Paper Auto", "Incidents"]
    assert "Closed: 1" in lines and "Entered: 1" in lines and lines[-1] == "Incidents: none" and lines[7].endswith(" PT")
    rep = REP.daily_report(r.fv.store.daily(DAY))                                       # the STORED summary: rebuilt once the record was final
    assert "FORWARD EVIDENCE (operational health, not a strategy result)" in rep and "  Market session: HEALTHY" in rep and r.fv.store.daily(DAY)["forward_capture"]["final"]
    ev = r.fv.overview(r.clock.now)["forward_evidence"]
    row = ev["cohorts"][0]
    assert row["label"] == "FP_2026_01" and row["healthy_sessions"] == 1 and row["closed_trades"] == 1 and row["status"] == "INSUFFICIENT_DATA"
    assert row["insufficient_text"] == "INSUFFICIENT FOR STRATEGY CONCLUSION" and row["healthy_target"] == 30 and row["events_target"] == 100
    assert "no status approves a strategy" in ev["note"] and ev["sessions"]["HEALTHY"] == 1
    assert any("healthy session(s) of the 30 needed" in x for x in row["reasons"])
    with pytest.raises(sqlite3.DatabaseError):
        c = sqlite3.connect(str(r.db))
        try:
            c.execute("UPDATE forward_capture_session_health SET status = 'INVALID' WHERE trading_date = ?", (DAY,))
        finally:
            c.close()


def test_H02_a_recording_fault_degrades_the_day_a_locked_database_delays_evidence_and_never_an_order(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    seen = {}

    def during(r, n):
        if n == 3:
            seen["pv"] = armed(r, "SNDK")
            cyc(r)                                                                     # the entry goes out as always; its markers wait in the sink
            seen["intents"] = len(r.x.store.intents())
            lock = sqlite3.connect(str(r.db), timeout=0.1, isolation_level=None)
            lock.execute("BEGIN IMMEDIATE")                                            # another writer holds the database during one capture tick
            try:
                t = r.cap.tick(r.clock.now)
            finally:
                lock.execute("ROLLBACK")
                lock.close()
            seen["locked"] = t
            seen["written_during_lock"] = len(by(r, "ENTRY_INTENT"))
    observed_day(r, monkeypatch, during=during)
    assert seen["intents"] == 1 and seen["written_during_lock"] == 0 and seen["locked"]["result"] == "ERROR"   # the locked tick wrote nothing ...
    ent = by(r, "ENTRY_INTENT")
    xid = execution_id_for(seen["pv"].proposal_id)
    assert [(d["execution_id"], d["source"], d["recovered"]) for d in ent] == [(xid, "EXECUTOR_CYCLE", 0)]                    # ... the later tick wrote it, decision-time
    assert r.store.entry_snapshot(xid)["recovered"] == 0 and r.store.entry_snapshot(xid)["snapshot"]["controls"]["results"]
    sndk = [d for d in decisions(r) if d["symbol"] == "SNDK" and d["source"] == "SETUP_EVALUATION"]
    assert len(sndk) == len({d["evaluation_id"] for d in sndk}) == 2                  # its WATCH and READY evaluations, exactly once each
    codes = [f["code"] for f in r.store.faults(DAY)]
    assert "WRITE_FAILED" in codes, codes
    h = r.store.health(DAY)
    assert h["status"] == "DEGRADED" and "CAPTURE_FAULTS" in h["reasons"] and h["final"] == 1, h["reasons"]


def test_H03_a_day_without_the_executor_loop_is_invalid_and_a_missed_market_day_is_recorded_not_skipped(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    observed_day(r, monkeypatch, worker=False)
    h = r.store.health(DAY)
    assert h["status"] == "INVALID" and "WORKER_COVERAGE_LOW" in h["reasons"] and h["metrics"]["worker_coverage"] == 0.0 and h["final"] == 1
    assert "the executor loop ran for less than 75 % of the regular session" in h["summary_text"]
    r.clock.now = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)                           # Friday: Thursday 22nd was never observed
    cycle(r)
    obs(r)
    missed = r.store.health("2026-10-22")
    assert missed is not None and missed["status"] == "INVALID" and missed["final"] == 1 and "FV_SESSION_NOT_RUN" in missed["reasons"], missed
    r.clock.now = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)                           # Thanksgiving: an exchange holiday is MARKET_CLOSED, not a failure
    cycle(r)
    obs(r)
    assert r.store.health("2026-11-26")["status"] == "MARKET_CLOSED" and r.store.health("2026-11-26")["final"] == 1
    assert r.store.health("2026-10-24") is None                                        # weekends get no record


def test_H04_premarket_writes_no_health_record_and_unresolved_reconciliation_at_the_close_invalidates_the_day(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.clock.now = datetime(2026, 10, 21, 13, 10, tzinfo=UTC)                          # 09:10 New York (06:10 Pacific): before the open
    started(r)
    obs(r)
    assert r.store.health(DAY) is None and r.store.config_snapshots()                  # the startup snapshot exists; the day's record does not yet
    observed_day(r, monkeypatch)
    from forward_capture import health as HL
    assert HL.compute(r.cap, DAY, r.clock.now)["status"] == "HEALTHY"
    conn = sqlite3.connect(str(r.db))
    try:
        last = conn.execute("SELECT max(at) FROM forward_validation_samples WHERE trading_date = ? AND in_session = 1", (DAY,)).fetchone()[0]
    finally:
        conn.close()
    real_query = r.cap.store.query
    monkeypatch.setattr(r.cap.store, "query", lambda sql, args=(): [dict(x, reconciliation_healthy=0) if x.get("at") == last else x for x in real_query(sql, args)]
                        if "FROM forward_validation_samples" in sql else real_query(sql, args))   # the session's last sample: reconciliation unresolved
    rec = HL.compute(r.cap, DAY, r.clock.now)
    assert rec["status"] == "INVALID" and "UNRESOLVED_RECONCILIATION" in rec["reasons"]


# ================================================================ R robustness ==============================================================
def reopen(r):
    """A process restart: a NEW executor and a NEW capture on the same database and broker; nothing in memory survives (the sink included)."""
    from execution.service import ExecutionService
    from execution.store import ExecutionStore
    from forward_capture.capture import ForwardCapture
    r.x = ExecutionService(stream_fn=lambda: r.holder["s"], store=ExecutionStore(r.db), broker=r.mk(), clock=r.clock)
    r.holder["s"].execution = r.x
    r.fv.capture = r.cap = ForwardCapture(r.fv)
    return r.x


def boom(ctx):
    raise SimulatedCrash("simulated process death")


@pytest.mark.parametrize("hook,posts,end,entered", [("after_intent", 0, "NOT_SENT", False), ("after_attempt_recorded", 0, "NOT_SENT", False),
                                                    ("after_post", 1, "FILLED", True), ("before_fill_persisted", 1, "FILLED", True)])
def test_R01_a_crash_at_every_entry_point_recovers_without_a_duplicate_trade_or_decision_and_says_so(tmp_path, monkeypatch, hook, posts, end, entered):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv = armed(r)
    if hook == "before_fill_persisted":
        r.sim.auto_fill = "manual"
        cyc(r)                                                                         # submitted (its markers were recorded); the fill comes later
        obs(r)
        r.sim.fill(entry_coid(execution_id_for(pv.proposal_id)))
    r.x.hooks[hook] = boom
    with pytest.raises(SimulatedCrash):
        cyc(r)
    reopen(r)
    for _ in range(4):
        cyc(r, 31)
        obs(r)
    xid = execution_id_for(pv.proposal_id)
    it = r.x.store.intent(xid)
    assert it.state == end and len(r.sim.posts) == posts and len(r.x.store.intents()) == 1 and it.attempt_count <= 1    # no blind retry, no duplicate
    ent = by(r, "ENTRY_INTENT")
    assert [d["execution_id"] for d in ent] == [xid] and len(r.store.query("SELECT 1 FROM forward_capture_entry_snapshots")) == 1
    recovered = hook != "before_fill_persisted"
    assert ent[0]["recovered"] == int(recovered) and r.store.entry_snapshot(xid)["recovered"] == int(recovered)
    if recovered:
        assert ent[0]["reason_codes"] == ["RECOVERED_FROM_INTENT"] and r.store.entry_snapshot(xid)["snapshot"]["controls"]["results"] == "UNAVAILABLE"
        assert "ENTRY_RECOVERED" in [f["code"] for f in r.store.faults()]               # the evidence gap is a recorded fact
    assert [d["execution_id"] for d in by(r, "ENTERED")] == ([xid] if entered else [])
    assert [d["reason_codes"][0] for d in by(r, "NOT_ENTERED")] == ([] if entered else [it.failure_code])
    assert not [f for f in r.store.faults() if f["code"] == "CONFLICT"]


def test_R02_a_recorder_that_throws_never_changes_order_handling_and_the_gap_is_visible(tmp_path, monkeypatch):
    def run(r):
        started(r)
        obs(r)
        pv = armed(r)
        for _ in range(3):
            cyc(r)
            obs(r)
        close(r, pv)
        obs(r)
        return {"intents": [(i.intent_type, i.state, i.qty, i.filled_qty) for i in r.x.store.intents()], "posts": len(r.sim.posts),
                "positions": [(p.state, p.quantity, p.realized_pnl) for p in r.x.store.positions(open_only=False)], "runs": [x["error"] for x in r.x.store.runs(100)]}
    clean = run(rig(tmp_path / "clean", monkeypatch))
    hit = rig(tmp_path / "hit", monkeypatch)
    calls = []
    monkeypatch.setattr(hit.cap, "marker", lambda kind, **kw: (calls.append(kind), (_ for _ in ()).throw(RuntimeError("recorder failed")))[1])
    assert run(hit) == clean and calls and all(e is None for e in clean["runs"])         # identical intents, posts, positions and cycle results
    assert by(hit, "ENTRY_INTENT")[0]["recovered"] == 1 and len(by(hit, "EXITED")) == 1   # the lifecycle comes from persisted rows; the lost markers show
    assert [d for d in decisions(hit) if d["source"] == "EXECUTOR_CYCLE"] == []


def test_R03_partial_fill_rejected_and_ambiguous_orders_are_captured_once_without_any_blind_retry(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    s = supervisor(r)
    s.tick()
    obs(r)
    pv = armed(r)
    r.sim.auto_fill = "manual"
    cyc(r)
    coid = entry_coid(execution_id_for(pv.proposal_id))
    r.sim.fill(coid, 2)
    cyc(r)
    obs(r)
    r.sim.fill(coid)
    cyc(r)
    obs(r)
    pos = r.x.store.open_position("SNDK")
    ent = by(r, "ENTERED")
    assert len(ent) == 1 and ent[0]["detail"]["first_observed"]["entry_qty"] == 2 and pos.entry_qty == pv.final_qty > 2   # one ENTERED, first observed partial
    r.sim.auto_fill = "next_read"
    close(r, pv)
    obs(r)
    out = r.store.outcome(pos.position_id)["outcome"]
    assert out["entry_qty"] == pv.final_qty and out["exit_qty"] == pv.final_qty and len(by(r, "EXITED")) == 1
    r.x.disable_auto(actor="user", now=r.clock.now)
    pv2 = go_ready(r, "NVDA")
    r.x.enable_auto(actor="user", now=r.clock.now)
    posts = len(r.sim.posts)
    r.sim.post_modes = ["reject"]
    cyc(r)
    cyc(r)                                                                             # the proposal's entry exists: DUPLICATE_ENTRY, never a resubmission
    obs(r)
    x2 = execution_id_for(pv2.proposal_id)
    assert r.x.store.intent(x2).state == "REJECTED" and len(r.sim.posts) == posts + 1
    assert [d["reason_codes"] for d in by(r, "NOT_ENTERED") if d["execution_id"] == x2] == [["BROKER_REJECTED"]]
    assert any(d["symbol"] == "NVDA" and d["decision"] == "BLOCKED" and "DUPLICATE_ENTRY" in d["reason_codes"] for d in decisions(r))
    pv3 = go_ready(r, "AMD")
    r.sim.post_modes = ["timeout_before"]
    cyc(r)                                                                             # an ambiguous POST: reconciliation required, no second POST
    x3 = execution_id_for(pv3.proposal_id)
    assert r.x.store.intent(x3).state == "RECONCILIATION_REQUIRED" and r.x.store.intent(x3).attempt_count == 1 and len(r.sim.posts) == posts + 1   # it never arrived
    t = s.tick()
    assert t["disarm"]["result"] == "DISARMED" and "RECONCILIATION_AMBIGUITY" in t["disarm"]["codes"] and r.x.mode()["mode"] == "MONITOR_ONLY"
    for _ in range(3):
        cyc(r, 31)
    obs(r)
    assert len(r.sim.posts) == posts + 1 and r.x.store.intent(x3).attempt_count == 1 and r.x.store.intent(x3).state in ("NOT_SENT", "RECONCILIATION_REQUIRED")   # never re-POSTed
    assert [d["execution_id"] for d in by(r, "ENTRY_INTENT")].count(x3) == 1


def test_R04_broker_read_timeouts_disarm_after_three_failed_refreshes_of_this_process_only(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    s = supervisor(r, process_start=r.clock.now)
    armed(r)
    for n in range(3):
        r.wire.script += [("/v2/account", "timeout")] * int(config.ACCOUNT_REFRESH_READ_ATTEMPTS)
        r.clock.advance(60)
        r.ref.refresh(trigger="TEST")
        t = s.tick()
        if n < 2:
            assert t["disarm"] is None, (n, t)
    assert t["disarm"]["result"] == "DISARMED" and "BROKER_READ_FAILURES" in t["disarm"]["codes"] and r.x.mode()["mode"] == "MONITOR_ONLY"
    assert r.sleeps and all(isinstance(x, (int, float)) for x in r.sleeps)               # bounded read retries only (the refresher's own); no order retried


def unified(state, rh_state, at):
    return {"state": state, "sources": [{"source": "ALPACA_PAPER", "account_type": "PAPER", "state": "LOADED", "refreshed_at": at, "age_s": 3},
                                        {"source": "ROBINHOOD", "account_type": "REAL", "state": rh_state, "refreshed_at": at, "age_s": 900}], "rows": []}


def test_R05_stale_expired_or_unreachable_robinhood_context_is_recorded_as_such_and_never_as_zero_exposure(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    states = iter([unified("STALE_SOURCE", "STALE", "2026-10-21T14:00:00+00:00"), unified("PARTIAL", "AUTH_REQUIRED", None), None])

    def read(refresh=True):
        u = next(states)
        if u is None:
            raise ConnectionError("gateway down")
        return u
    monkeypatch.setattr(r.s, "unified_portfolio", read)
    obs(r)
    go_ready(r)
    obs(r, 60)
    obs(r, 60)
    ctx = r.store.query("SELECT unified_state, context_json FROM forward_capture_context ORDER BY at, rowid")
    assert [c["unified_state"] for c in ctx] == ["STALE_SOURCE", "PARTIAL", "UNAVAILABLE"]
    assert [json.loads(c["context_json"])["sources"][1]["state"] for c in ctx[:2]] == ["STALE", "AUTH_REQUIRED"] and json.loads(ctx[2]["context_json"])["error"] == "ConnectionError"
    assert all("total" not in c["context_json"] and "rows" not in c["context_json"] for c in ctx)   # states and freshness only: no fabricated zero
    skip = by(r, "SKIPPED")[0]
    assert skip["exposure"]["unified_state"] == "STALE_SOURCE" and skip["context_at"] == skip["exposure"]["context_at"] <= skip["decided_at"]   # the context IN FORCE then
    monkeypatch.setattr(config, "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED", True)
    from forward_capture import health as HL
    states = iter([unified("PARTIAL", "AUTH_REQUIRED", None)] * 3)
    monkeypatch.setattr(r.s, "unified_portfolio", lambda refresh=True: next(states))
    obs(r, 60)
    assert "PARTIAL_EXTERNAL_CONTEXT" in HL.compute(r.cap, DAY, r.clock.now)["reasons"]   # gate ON with partial data: the day says so


# ================================================================ P read paths and exports =================================================
def capture_state(db):
    """The schema plus every forward-capture / auto-run row: what a read path must never change."""
    conn = sqlite3.connect(str(db))
    try:
        schema = sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
        return schema, dump_capture(db)
    finally:
        conn.close()


def test_P01_read_paths_never_write_never_migrate_and_never_create_a_cohort_snapshot_or_health_record(tmp_path, monkeypatch):
    from execution import activity as ACT
    from autorun.view import activity_view
    from forward_capture import view as V
    r = rig(tmp_path, monkeypatch)
    started(r)
    before = capture_state(r.db)
    assert not [n for (t, n, _) in before[0] if n.startswith(("forward_capture_", "autorun_"))]         # nothing captured yet: the tables do not exist
    a = ACT.activity(r.x, scheduler_status={"running": False}, enabled=True, now=r.clock.now)
    r.fv.overview(r.clock.now)
    r.fv.daily_summary(DAY, r.clock.now)
    for kind in FC.EXPORT_KINDS:
        assert REP.export_rows(r.fv, kind)[1] == []
    activity_view(r.db, r.clock.now)
    V.forward_session(r.store, DAY, r.clock.now)
    V.evidence(r.store, r.fv, r.clock.now)
    assert capture_state(r.db)[0] == before[0], "a read path created a table"                       # no DDL from any read
    assert a["forward_session"]["recorded"] is False and a["forward_session"]["health"]["status"] is None and a["auto_run"]["label"] == "Manual arm"
    obs(r)
    pv = armed(r)
    cyc(r)
    cyc(r)
    obs(r)
    snap = capture_state(r.db)
    for _ in range(2):
        a = ACT.activity(r.x, scheduler_status={"running": False}, enabled=True, now=r.clock.now)
        r.fv.overview(r.clock.now)
        r.fv.cumulative(r.clock.now)
        r.fv.daily_summary(DAY, r.clock.now)
        for kind in FC.EXPORT_KINDS:
            REP.export_rows(r.fv, kind)
        activity_view(r.db, r.clock.now)
        V.daily(r.store, DAY)
    assert capture_state(r.db) == snap                                                                  # read paths are read-only
    ev = r.fv.overview(r.clock.now)["forward_evidence"]
    assert ev["cohorts"][0]["healthy_sessions"] == 0 and ev["cohorts"][0]["in_progress"] == 1 and ev["sessions"]["in_progress"] == 1   # not final: not counted
    fs = a["forward_session"]
    assert fs["recorded"] and fs["cohort"]["label"] == "FP_2026_01" and fs["orders"] == 1 and fs["entered"] == 1 and fs["candidates"] == 1
    assert a["auto_run"]["policy"] == "MANUAL_ARM" and a["auto_run"]["scheduled_arm_enabled"] is False and "account_number" not in json.dumps(a)
    assert a["mode"]["mode"] == "PAPER_AUTO" and a["auto_run"]["label"] == "Manual arm"                 # the current mode and the policy are separate fields
    assert pv


def test_P02_every_forward_export_is_deterministic_read_only_and_served_by_the_existing_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.routes import trader_view as RT
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None)
    supervisor(r).tick()
    obs(r)
    pv = armed(r)
    cyc(r)
    cyc(r)
    close(r, pv)
    obs(r)
    for kind in FC.EXPORT_KINDS:
        cols, rows = REP.export_rows(r.fv, kind)
        text = REP.to_csv(cols, rows)
        assert text.splitlines()[0] == ",".join(cols) and text == REP.to_csv(*REP.export_rows(r.fv, kind)), kind
        assert json.loads(REP.to_json(kind, cols, rows))["columns"] == list(cols), kind
        low = text.lower()
        assert "secret" not in low and "apca-api" not in low and "paper-api.alpaca.markets" not in low, kind
    assert all(REP.export_rows(r.fv, k)[1] for k in ("candidates", "decisions", "controls", "session_health", "entry_snapshots", "outcomes", "config_snapshots", "cohorts",
                                                       "context", "autorun_events", "autorun_preflights"))
    ids = [d["decision_id"] for d in REP.export_rows(r.fv, "decisions")[1]]
    assert len(ids) == len(set(ids))                                                                   # unique, time-ordered rows
    monkeypatch.setitem(RT.STREAM, "instance", r.s)
    from api.server import app
    api = TestClient(app)
    x = api.get("/api/forward-validation/export/decisions")
    assert x.status_code == 200 and x.headers["content-type"].startswith("text/csv") and x.text.splitlines()[0].startswith("decision_id,")
    j = api.get("/api/forward-validation/export/session_health?format=json")
    assert j.status_code == 200 and json.loads(j.text)["rows"][0]["trading_date"] == DAY
    assert api.get("/api/forward-validation/export/secrets").status_code == 400
    for path in ("/api/forward-validation/export/decisions", "/api/forward-validation/export/cohorts"):
        assert api.post(path).status_code == 405 and api.delete(path).status_code == 405                # GET only: no delete / edit endpoint
    mine = {p for p in app.openapi()["paths"] if p.startswith(("/api/forward-validation", "/api/execution"))}
    assert not [p for p in mine if re.search(r"(?i)forward-capture|autorun|cohort|preflight", p)]       # no new route: the existing export route serves it


@pytest.mark.parametrize("change,code", [("disarm", "mode is no longer PAPER_AUTO"), ("kill", "safety gate")])
def test_A11_a_mode_change_or_an_emergency_stop_mid_cycle_stops_the_entry_before_the_wire_and_the_evidence_says_so(tmp_path, monkeypatch, change, code):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv = armed(r)
    posts = len(r.sim.posts)
    act = {"disarm": lambda ctx: r.x.disable_auto(actor="user", reason="mid-cycle", now=r.clock.now),
           "kill": lambda ctx: r.s.safety.emergency_stop(reason="mid-cycle", now=r.clock.now)}[change]
    r.x.hooks["after_intent"] = act                                                    # between the intent row and the last look before the wire
    cyc(r)
    obs(r)
    it = r.x.store.intent(execution_id_for(pv.proposal_id))
    assert it.state == "NOT_SENT" and it.failure_code == "PRECHECK_FAILED" and code in it.failure_text and len(r.sim.posts) == posts   # nothing left the machine
    assert sorted(d["decision"] for d in decisions(r) if d["execution_id"] == it.execution_id) == ["ENTRY_INTENT", "NOT_ENTERED"]   # the same second
    assert by(r, "NOT_ENTERED")[0]["reason_codes"] == ["PRECHECK_FAILED"] and code in by(r, "NOT_ENTERED")[0]["detail"]["failure_text"]


def test_A12_the_scheduled_arm_never_arms_over_a_mode_change_it_did_not_see_and_spends_its_one_attempt(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.clock.now = datetime(2026, 10, 21, 13, 40, tzinfo=UTC)
    started(r)
    healthy_loops(monkeypatch, r)
    policy(monkeypatch, "SCHEDULED_ARM", enabled=True)
    s = supervisor(r)
    real, seen = r.x.store.mode_events, []

    def events(limit=20):                                                              # the facts see one event; the re-check under the lock another
        seen.append(limit)
        return [{"event_id": "changed-meanwhile"}] if limit == 1 and seen.count(1) >= 2 else real(limit)
    monkeypatch.setattr(r.x.store, "mode_events", events)
    t = s.tick()
    assert t["scheduled_arm"]["result"] == "MODE_CHANGED" and r.x.mode()["mode"] == "MONITOR_ONLY"
    monkeypatch.setattr(r.x.store, "mode_events", real)
    r.clock.advance(60)
    t = s.tick()
    assert t["scheduled_arm"]["result"] == "NOT_ELIGIBLE" and any("already ran today (MODE_CHANGED)" in x for x in t["scheduled_arm"]["reasons"]) and r.x.mode()["mode"] == "MONITOR_ONLY"


def test_A13_without_a_supervisor_no_long_held_lease_is_taken_so_a_quick_restart_is_never_left_passive(tmp_path, monkeypatch):
    import autorun.scheduler as ASCH
    r = rig(tmp_path, monkeypatch)
    started(r)
    r.x.enable_auto(actor="user", now=r.clock.now)
    out = startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None, take_lease=False)   # AUTORUN_SUPERVISOR_ENABLED=false
    assert out["demoted"] is True and r.x.mode()["mode"] == "MONITOR_ONLY" and STARTUP["lease_taken"] is False and AutoRunStore(r.db).lease(AR.INSTANCE_LEASE) is None
    r.x.enable_auto(actor="user", now=r.clock.now)
    r.clock.advance(60)                                                                # a crash and a restart a minute later: never refused, still demoted
    assert startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None, take_lease=False)["demoted"] is True
    monkeypatch.setattr(ASCH, "_SCHEDULER", None)
    startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None)
    ASCH.shutdown()
    assert AutoRunStore(r.db).lease(AR.INSTANCE_LEASE)["owner"] == AR.instance_id()   # stopping the supervisor does not release it ...
    ASCH.release_lease()
    assert AutoRunStore(r.db).lease(AR.INSTANCE_LEASE) is None                         # ... the lifespan's last step does


def test_A14_a_normal_start_is_not_a_takeover_and_a_failed_executor_start_is_retried(tmp_path, monkeypatch):
    import autorun.service as SV
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    STARTUP.update(components_started=False)                                           # a fresh process
    startup_step(r.x, AutomationStore(r.db), AutoRunStore(r.db), r.clock.now, sleep=lambda s: None)
    SV.mark_started()                                                                  # the lifespan started the executor and observer itself
    s = supervisor(r)
    t = s.tick()
    assert "startup" not in t and s.calls == [] and AutoRunStore(r.db).events(kind="TAKEOVER") == []
    monkeypatch.setattr(SV, "in_test_process", lambda: False)                          # as in a real process whose executor did not start
    monkeypatch.setattr(config, "EXECUTION_SCHEDULER_ENABLED", True)
    import execution.scheduler as XS
    monkeypatch.setattr(XS, "status", lambda: {"running": False})
    STARTUP.update(components_started=False)
    s2 = supervisor(r)
    s2.start_fn = lambda: {"executor": False, "observer": False}
    s2.tick()
    s2.tick()
    assert len(AutoRunStore(r.db).events(kind="TAKEOVER")) == 2 and STARTUP["components_started"] is False   # not marked started: retried every tick


def test_A15_a_database_outage_while_armed_disarms_as_soon_as_the_mode_can_be_written(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    s = supervisor(r)
    armed(r)
    assert s.tick()["disarm"] is None
    real = r.x.store.mode
    monkeypatch.setattr(r.x.store, "mode", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    t = s.tick()
    assert t["preflight"] == "BLOCKED" and t["disarm"] is None and s.mem["db_failure"]["error"] == "OperationalError"   # nothing can be written yet
    monkeypatch.setattr(r.x.store, "mode", real)
    t = s.tick()
    assert t["disarm"]["result"] == "DISARMED" and "DATABASE_FAILURE" in t["disarm"]["codes"] and r.x.mode()["mode"] == "MONITOR_ONLY" and "db_failure" not in s.mem


class _Lock:
    """The executor's RLock with a hook that runs while the supervisor waits for it."""

    def __init__(self, real, before):
        self.real, self.before = real, before

    def acquire(self, *a, **k):
        if self.before:
            self.before()
            self.before = None
        return self.real.acquire(*a, **k)

    def release(self):
        return self.real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def test_A16_the_scheduled_arm_checks_its_conditions_again_under_the_executor_lock(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.clock.now = datetime(2026, 10, 21, 13, 40, tzinfo=UTC)
    started(r)
    healthy_loops(monkeypatch, r)
    policy(monkeypatch, "SCHEDULED_ARM", enabled=True)
    s = supervisor(r)
    r.x._lock = _Lock(r.x._lock, lambda: r.s.safety.emergency_stop(reason="pressed during the lock wait", now=r.clock.now))
    t = s.tick()
    assert t["scheduled_arm"]["result"] == "NOT_ELIGIBLE_UNDER_LOCK" and any("Emergency Stop" in x for x in t["scheduled_arm"]["reasons"])
    assert r.x.mode()["mode"] == "MONITOR_ONLY" and (AutoRunStore(r.db).day(DAY) or {}).get("scheduled_arm_at") is None   # not armed; the attempt not spent


def test_A17_after_taking_a_database_over_the_predecessors_cycles_are_not_a_foreign_executor_but_a_live_one_is(tmp_path, monkeypatch):
    from execution.store import ExecutionStore
    r = rig(tmp_path, monkeypatch)
    started(r)
    healthy_loops(monkeypatch, r)
    lease = AutomationStore(r.db)
    assert lease.acquire("old-instance:1", r.clock.now, ttl_min=AR.INSTANCE_LEASE_TTL_MIN, name=AR.INSTANCE_LEASE)
    STARTUP.update(pending=True, passive=True, components_started=False)
    s = supervisor(r)
    ExecutionStore(r.db).add_run("a" * 32, r.clock.now, r.clock.now, "SCHEDULED", "PAPER_AUTO", "RUN", {}, None, "old-executor:1")   # its last cycles
    s.tick()
    r.clock.advance(AR.INSTANCE_LEASE_TTL_MIN * 60 + 1)
    t = s.tick()
    assert t["startup"]["result"] == "STARTED" and t["preflight"] == "READY" and s.last_facts["foreign_executor"] == [], s.last_preflight
    r.x.enable_auto(actor="user", now=r.clock.now)
    assert s.tick()["disarm"] is None and r.x.mode()["mode"] == "PAPER_AUTO"           # a person can arm: nothing foreign since the takeover
    r.clock.advance(30)
    ExecutionStore(r.db).add_run("b" * 32, r.clock.now, r.clock.now, "SCHEDULED", "PAPER_AUTO", "RUN", {}, None, "old-executor:1")   # it is still alive
    t = s.tick()
    assert "DUPLICATE_INSTANCE" in t["disarm"]["codes"] and r.x.mode()["mode"] == "MONITOR_ONLY"


def test_H06_a_day_the_evaluator_was_paused_is_not_healthy_evidence(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    observed_day(r, monkeypatch, paused_from=1)                                        # market data stale from 10:00 to the close: every evaluation PAUSED
    h = r.store.health(DAY)
    assert h["status"] == "DEGRADED" and "SETUP_EVALUATION_GAPS" in h["reasons"] and h["metrics"]["setup_evaluation_coverage"] < 0.2, (h["reasons"], h["metrics"]["setup_evaluation_coverage"], r.store.query("SELECT decision, count(*) n FROM setup_evaluations GROUP BY decision"))


def test_H07_another_bot_on_the_paper_account_invalidates_the_day_even_in_monitor_only(tmp_path, monkeypatch):
    from forward_capture import health as HL
    r = rig(tmp_path, monkeypatch)
    observed_day(r, monkeypatch)
    assert HL.compute(r.cap, DAY, r.clock.now)["status"] == "HEALTHY"
    healthy_loops(monkeypatch, r)
    r.clock.now = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    snap = r.ref.store.snapshot()
    monkeypatch.setattr(r.ref.store, "snapshot", lambda: {**snap, "open_orders": [{"client_order_id": "stockagent-entry-" + "e" * 24, "symbol": "AMD", "status": "new"}]})
    t = supervisor(r).tick()                                                           # Monitor only: nothing to disarm, but the preflight records it
    assert t["preflight"] == "BLOCKED" and t["disarm"] is None
    rec = HL.compute(r.cap, DAY, datetime(2026, 10, 21, 20, 30, tzinfo=UTC))
    assert rec["status"] == "INVALID" and "DUPLICATE_INSTANCE" in rec["reasons"] and rec["metrics"]["other_instance"] is True


def test_H05_a_degraded_stage_61_session_never_reads_as_a_healthy_evidence_day(tmp_path, monkeypatch):
    from forward_capture import health as HL
    r = rig(tmp_path, monkeypatch)
    observed_day(r, monkeypatch)
    assert HL.compute(r.cap, DAY, r.clock.now)["status"] == "HEALTHY"
    real = r.fv.store.session
    monkeypatch.setattr(r.fv.store, "session", lambda d: dict(real(d), session_status="DEGRADED", notes_code="CASH_FLOW_DATA_NEVER_FRESH"))
    rec = HL.compute(r.cap, DAY, r.clock.now)
    assert rec["status"] == "DEGRADED" and rec["reasons"] == ["FV_SESSION_DEGRADED"] and "Stage 6.1 session record is DEGRADED" in rec["summary_text"]


def test_R06_a_tick_that_fails_before_recording_keeps_the_executor_markers_for_the_next_tick(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    started(r)
    obs(r)
    pv = armed(r)
    cyc(r)                                                                             # the entry's SCAN / EVALUATED / INTENT / CYCLE_END markers wait in the sink
    waiting = r.cap.pending()
    r.cap._epoch = lambda now: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))   # the tick fails before it records them
    t = r.cap.tick(r.clock.now)
    assert t["result"] == "ERROR" and waiting > 0 and r.cap.pending() == waiting and by(r, "ENTRY_INTENT") == []   # put back, nothing written
    del r.cap._epoch
    obs(r, 30)
    xid = execution_id_for(pv.proposal_id)
    assert [(d["execution_id"], d["source"], d["recovered"]) for d in by(r, "ENTRY_INTENT")] == [(xid, "EXECUTOR_CYCLE", 0)]   # decision-time, not recovered
    codes = [f["code"] for f in r.store.faults()]
    assert "TICK_FAILED" in codes and "EVIDENCE_LOST" not in codes and "ENTRY_RECOVERED" not in codes


# ================================================================ F the freeze ===============================================================
SERVER_LINES_610A = [
    '        from autorun.service import startup_if_enabled as _autorun_startup  # Stage 6.10A: the single-instance lease, then Monitor only (MANUAL_ARM)',
    '        _autorun_startup()  # Stage 6.10A: a start never keeps Paper Auto armed; raises (no executor, no observer: fail closed) when this process may not run them',
    "        from autorun.service import mark_started as _autorun_started  # Stage 6.10A: the threads above are this process's own (no takeover on the first tick)",
    '        _autorun_started()  # Stage 6.10A',
    '    try:                                                    # Stage 6.10A: the auto-run supervisor (lease renewal, preflight, automatic disarm, opt-in scheduled arm; never in tests)',
    '        from autorun.scheduler import start_if_enabled as _autorun_start',
    '        _autorun_start()',
    '    except Exception as exc:  # noqa: BLE001 - the supervisor must never stop the server from starting',
    '        logging.getLogger(__name__).warning("Auto-run supervisor could not start: %s", type(exc).__name__)',
    '    try:                                                    # Stage 6.10A: stop the auto-run supervisor first (it can disarm Paper Auto)',
    '        from autorun.scheduler import shutdown as _autorun_shutdown',
    '        _autorun_shutdown()',
    '        logging.getLogger(__name__).warning("Auto-run supervisor did not stop cleanly: %s", type(exc).__name__)',
    '    try:                                                    # Stage 6.10A: release the instance lease last, after the executor and the observer stopped',
    '        from autorun.scheduler import release_lease as _autorun_release',
    '        _autorun_release()',
    '    except Exception as exc:  # noqa: BLE001 - the lease expires by itself',
    '        logging.getLogger(__name__).warning("Auto-run lease not released: %s", type(exc).__name__)',
]
CONFIG_610A = ["PAPER_AUTO_RUN_POLICY", "PAPER_AUTO_SCHEDULED_ARM_ENABLED", "PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES", "PAPER_AUTO_DISARM_AT_SESSION_END",
               "AUTORUN_SUPERVISOR_ENABLED"]
NEW_LEDGER_610A = {"api/routes/forward_validation.py", "tests/test_windows_runner_60.py", "scripts/install_stock_agent_task.ps1"}
REPLAY_610A = {
    'execution/activity.py': [
        ('\n',
         '\ndef auto_run(service, now: datetime) -> Optional[Dict[str, Any]]:\n    """Stage 6.10A: the auto-run policy (apart from the current mode and the last cycle\'s mode), the latest preflight and today\'s arm / disarm record."""\n    try:\n        from autorun.view import activity_view\n        return activity_view(getattr(service.store, "path", None), now)\n    except Exception:  # noqa: BLE001 - display only\n        return None\n\n\ndef forward_session(service, now: datetime) -> Optional[Dict[str, Any]]:\n    """Stage 6.10A: today\'s forward-session record - cohort, operational health, Paper Auto times, candidates, orders, incidents (read only)."""\n    try:\n        from forward_capture.capture import trading_date\n        from forward_capture.store import ForwardCaptureStore\n        from forward_capture.view import forward_session as session_view\n        return session_view(ForwardCaptureStore(service.store.path), trading_date(now.astimezone(timezone.utc).isoformat(timespec="seconds")), now)\n    except Exception:  # noqa: BLE001 - display only\n        return None\n\n\n'),
        ('            "risk": risk(service, now), "orders": orders(service.store), "positions": positions(service, runs, account), "history": history(service, service.store, runs),\n',
         '            "risk": risk(service, now), "orders": orders(service.store), "positions": positions(service, runs, account), "history": history(service, service.store, runs),\n            "auto_run": auto_run(service, now), "forward_session": forward_session(service, now),           # Stage 6.10A (display only, read only)\n'),
    ],
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691), "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691) - new)}   # + Stage 6.9 / 6.9.1\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691                                                                # Stage 6.9.1: an INTENTIONAL execution-semantics change (FL.SEMANTIC_691),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A                                 # Stage 6.9.1 (FL.SEMANTIC_691) and Stage 6.10A (FL.SEMANTIC_610A),\n'),
        ('    base_srv = subprocess.run(["git", "show", f"{BASE_69}:api/server.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n").splitlines()\n    now_srv = (ROOT / "api" / "server.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n").splitlines()\n',
         '    base_srv = subprocess.run(["git", "show", f"{BASE_69}:api/server.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n").splitlines()\n    now_srv = subprocess.run(["git", "show", f"{FL.BASE_610A}:api/server.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n").splitlines()   # Stage 6.10A pins its own lines\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    assert FL.BASE_691 == BASE_691 and subprocess.run(["git", "cat-file", "-e", f"{BASE_691}^{{commit}}"], cwd=ROOT).returncode == 0\n',
         '    assert FL.BASE_691 == BASE_691 and subprocess.run(["git", "cat-file", "-e", f"{BASE_691}^{{commit}}"], cwd=ROOT).returncode == 0\n    stage691 = set(_git("diff", "--name-only", BASE_691, FL.BASE_610A).split())                # Stage 6.9.1 itself, between two fixed commits (main before / after PR #33)\n    in_base = set(_git("ls-tree", "-r", "--name-only", BASE_691).split())\n    new, edited = {p for p in stage691 if p not in in_base}, {p for p in stage691 if p in in_base}\n    assert new == FL.NEW_691, {"unexpected_new": sorted(new - FL.NEW_691), "missing_new": sorted(FL.NEW_691 - new)}\n    assert edited == FL.EDITED_691, {"unexpected": sorted(edited - FL.EDITED_691), "missing": sorted(FL.EDITED_691 - edited)}\n    assert {p for p in stage691 if p.startswith("execution/")} == {"execution/exposure_gate.py", "execution/engine.py", "execution/service.py", "execution/activity.py"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    in_base = set(_git("ls-tree", "-r", "--name-only", BASE_691).split())\n    new, edited = {p for p in changed if p not in in_base}, {p for p in changed if p in in_base}\n    assert new == FL.NEW_691, {"unexpected_new": sorted(new - FL.NEW_691), "missing_new": sorted(FL.NEW_691 - new)}\n    assert edited == FL.EDITED_691, {"unexpected": sorted(edited - FL.EDITED_691), "missing": sorted(FL.EDITED_691 - edited)}\n    assert {p for p in changed if p.startswith("execution/")} == {"execution/exposure_gate.py", "execution/engine.py", "execution/service.py", "execution/activity.py"}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A                                                       # Stage 6.10A, pinned by tests/test_forward_paper_610a.py\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n                                                            "missing": sorted((FL.NEW_691 | FL.EDITED_691 | later) - changed)}\n'),
        ('              "cash_flow", "paper", "portfolio", "database", "scripts", "rh_gateway"):\n        assert not [p for p in changed if p.startswith(d + "/")], d                          # no risk gate, strategy, broker, legacy portfolio or database file\n',
         '              "cash_flow", "paper", "portfolio", "database", "scripts", "rh_gateway"):\n        assert not [p for p in stage691 if p.startswith(d + "/")], d                         # no risk gate, strategy, broker, legacy portfolio or database file\n        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10A\'s declared files\n'),
        ('def test_F02_the_execution_semantics_changed_on_purpose_and_exactly_as_declared():\n',
         'def test_F02_the_execution_semantics_changed_on_purpose_and_exactly_as_declared():\n    import freeze_ledger as FL\n    post = lambda rel: subprocess.run(["git", "show", f"{FL.BASE_610A}:{rel}"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n")  # noqa: E731 - main after PR #33\n    _added = lambda rel: [l for l in post(rel).splitlines() if l not in set(_base(rel).splitlines())]  # noqa: E731 - Stage 6.9.1\'s own lines (main after it, not the working tree)\n'),
        ('    for rel in ("execution/engine.py", "execution/service.py", "trader_view/stream.py", "config.py"):\n        assert _now(rel) != _base(rel), f"{rel} is NOT byte-equivalent to pre-6.9.1 main, and no test may pretend it is"\n        base, now = _base(rel).splitlines(), set(_now(rel).splitlines())\n',
         '    for rel in ("execution/engine.py", "execution/service.py", "trader_view/stream.py", "config.py"):\n        assert post(rel) != _base(rel), f"{rel} is NOT byte-equivalent to pre-6.9.1 main, and no test may pretend it is"\n        base, now = _base(rel).splitlines(), set(post(rel).splitlines())\n'),
        ('        assert [l for l in base if l not in now] == REPLACED_691.get(rel, []), rel           # nothing of main removed but the listed line\n',
         '        assert [l for l in base if l not in now] == REPLACED_691.get(rel, []), rel           # nothing of main removed but the listed line\n        assert not [l for l in post(rel).splitlines() if l not in set(_now(rel).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left\n'),
        ('    assert added_stream[0] == "    def unified_portfolio(self, refresh: bool = True) -> dict:" and len(added_stream) == 5 and "precheck=False)" in added_stream[-1]\n    svc = _now("execution/service.py")\n',
         '    assert added_stream[0] == "    def unified_portfolio(self, refresh: bool = True) -> dict:" and len(added_stream) == 5 and "precheck=False)" in added_stream[-1]\n    svc = post("execution/service.py")\n'),
        ('    assert \'evaluated(sym, "BLOCKED", [{"control": c.control_id, "text": c.reason, **exposure_gate.record(c)} for c in blocked])\' in svc   # the replaced line keeps its fields\n    cfg_base, cfg_now = _base("config.py"), _now("config.py")\n',
         '    assert \'evaluated(sym, "BLOCKED", [{"control": c.control_id, "text": c.reason, **exposure_gate.record(c)} for c in blocked])\' in svc   # the replaced line keeps its fields\n    cfg_base, cfg_now = _base("config.py"), post("config.py")\n'),
    ],
}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    out = subprocess.run(["git", "show", f"{FL.BASE_610A}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_610A} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #34 (Stage 6.10A merged): what Stage 6.10A left, whatever a later stage changed since."""
    out = subprocess.run(["git", "show", f"{FL.BASE_610C}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_610C} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def test_F01_stage_610a_changes_exactly_its_declared_files_and_no_strategy_risk_broker_or_market_data_file():
    assert FL.BASE_610A == "27926f627c5cb83a6e1b3b6a9b646ce455cfbe52" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_610A}^{{commit}}"], cwd=ROOT).returncode == 0
    stage610a = set(_git("diff", "--name-only", FL.BASE_610A, FL.BASE_610C).split())          # Stage 6.10A itself, between two fixed commits (main before / after PR #34)
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_610A).split())
    new, edited = {p for p in stage610a if p not in in_base}, {p for p in stage610a if p in in_base}
    assert new == FL.NEW_610A, {"unexpected_new": sorted(new - FL.NEW_610A), "missing_new": sorted(FL.NEW_610A - new)}
    assert edited == FL.EDITED_610A, {"unexpected": sorted(edited - FL.EDITED_610A), "missing": sorted(FL.EDITED_610A - edited)}
    assert {p for p in stage610a if p.startswith("execution/")} == {"execution/service.py", "execution/activity.py"}          # never engine / broker / store / scheduler
    assert {p for p in stage610a if p.startswith("database/")} == {"database/autorun_migrations.py", "database/forward_capture_migrations.py"}
    assert {p for p in stage610a if p.startswith("scripts/")} == {"scripts/install_stock_agent_task.ps1"}
    changed = set(_git("diff", "--name-only", FL.BASE_610A).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests
    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),
                                                              "missing": sorted((FL.NEW_610A | FL.EDITED_610A | later) - changed)}
    for d in ("safety", "proposals", "setups", "strategy_validation", "strategy_registry", "momentum", "value_quality", "mean_reversion", "breakout", "account_refresh",
              "cash_flow", "paper", "portfolio", "rh_gateway", "trader_view", "unified_portfolio", "forward", "ppe", "demand", "evidence"):
        assert not [p for p in stage610a if p.startswith(d + "/")], d                        # no strategy, risk, broker, market-data or Robinhood file
        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10C's / 5.4.1's / the 6.1.1 CSS fix's declared files
    assert not changed & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",
                          "forward_validation/metrics.py", "README.md"}
    assert set(FL.SEMANTIC_610A) <= FL.NEW_610A | FL.EDITED_610A and all(len(v) > 40 for v in FL.SEMANTIC_610A.values())
    assert set(FL.INSTRUMENTATION_610A) <= FL.EDITED_610A and all(len(v) > 40 for v in FL.INSTRUMENTATION_610A.values())
    assert not set(FL.SEMANTIC_610A) & set(FL.INSTRUMENTATION_610A)                       # a semantic change is never filed as instrumentation
    unregistrable = FL.EDITED_610A & (FL.NEW_68 | FL.NEW_69 | FL.NEW_691)                 # files main did not have at BASE_68: no ledger entry can hold them
    assert FL.EDITED_610A - unregistrable <= set(FL.LEDGER) and unregistrable == set(REPLAY_610A) | {"tests/freeze_ledger.py"}


def test_F02_the_order_decision_logic_is_unchanged_and_the_executor_only_gained_failure_swallowing_markers():
    assert post("execution/engine.py") == at_base("execution/engine.py")             # entry controls, exits, sizing: byte-identical
    svc, base = post("execution/service.py"), at_base("execution/service.py")
    base_lines = set(base.splitlines())
    assert [l for l in base.splitlines() if l not in set(svc.splitlines())] == []        # nothing of main removed
    tree = ast.parse(svc)
    mark = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_mark"]
    assert len(mark) == 1
    a, b = mark[0].lineno, mark[0].end_lineno
    exprs = {n.lineno: n for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    markers = []
    for i, line in enumerate(svc.splitlines(), 1):
        if not line.strip() or line in base_lines or a <= i <= b:
            continue
        n = exprs.get(i)
        assert n is not None and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "_record", (i, line)
        lam = n.value.args[0]
        assert isinstance(lam, ast.Lambda) and isinstance(lam.body, ast.Call) and lam.body.func.attr == "_mark", (i, line)   # self._record(lambda: self._mark(...))
        markers.append(lam.body.args[0].value)
    assert markers == ["CYCLE_END", "SCAN", "SKIPPED", "EVALUATED", "DUPLICATE", "INTENT"]
    body = "\n".join(svc.splitlines()[a - 1:b])
    assert not re.search(r"\.(store|broker|post_order|set_mode|enable_auto|disable_auto|insert_intent|update_intent)\b", body)   # the sink only: no I/O
    import execution as X
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    cfg_base, cfg_now = at_base("config.py"), post("config.py")
    assert cfg_now.startswith(cfg_base.rstrip("\n"))
    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_610A
    srv_base, srv_now = at_base("api/server.py").splitlines(), post("api/server.py").splitlines()
    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610A
    srv = post("api/server.py")
    life = srv[srv.index("async def _lifespan"):srv.index("app = FastAPI(")]
    assert "alpaca" not in life.lower() and "rotation" not in life.lower()
    assert life.index("_autorun_startup()") < life.index("_exec_start()") < life.index("_fv_start()") < life.index("_autorun_start()") < life.index("yield")
    assert life.index("yield") < life.index("_autorun_shutdown()") < life.index("_fv_shutdown()")   # the supervisor stops first
    for rel in ("execution/engine.py", "execution/service.py", "config.py", "api/server.py"):
        assert not [l for l in post(rel).splitlines() if l not in set(now_text(rel).splitlines())], rel   # a later stage keeps every line Stage 6.10A left


def test_F03_the_frozen_and_entry_contract_hashes_and_the_default_policy_hold_in_a_clean_process():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; from autorun.policy import settings_from; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True))); "
                          "s = settings_from(config); print(s['effective_policy'], s['scheduled_arm_enabled'], s['disarm_at_session_end'], config.PAPER_AUTO_RUN_POLICY)"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == [FROZEN_V1, CONTRACT_HASH_OFF, CONTRACT_HASH_ON, "MANUAL_ARM", "False", "True", "MANUAL_ARM"], out.stderr[-800:]
    assert G.VERSION in AR.SUPPORTED_CONTROLS_VERSIONS


def test_F04_every_edit_no_ledger_entry_can_hold_is_pinned_exactly():
    for rel, edits in REPLAY_610A.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    para = fl[fl.index("\nStage 6.10A (forward paper auto-run"):fl.index('"""', fl.index("\nStage 6.10A (forward paper auto-run"))]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_691 + NEW_610A plus this ledger.", " + NEW_691 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_610A = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the declarations, the paragraph and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 6.10A left (main after PR #34)
    assert set(mine) == set(old) | NEW_LEDGER_610A
    assert {k for k in old if old[k] != mine[k]} == (FL.EDITED_610A & set(old)) - {"tests/freeze_ledger.py"}   # every re-hash is a declared Stage 6.10A edit
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


def code_strings(path):
    out = []
    for n in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def test_F05_the_text_bans_of_every_new_and_edited_file_hold():
    marker = "# >>> " + "Stage 6.1"
    for rel in sorted(FL.NEW_610A):
        assert marker not in now_text(rel), rel
    ui = now_text("frontend/product_ui.js")
    assert "retry" not in ui.lower() and ui.count("runAutoAction(") == 3 and ui.count("clickLegacy(") == 5
    assert not re.search(r"fetch\([^)]*(autorun|forward-capture|cohort|preflight)", ui)                    # the UI reads the existing activity payload only
    for rel in ("execution/service.py", "execution/activity.py"):                      # every line Stage 6.10A added to an execution file
        added = "\n".join(l for l in now_text(rel).splitlines() if l not in set(at_base(rel).splitlines()))
        assert added and not re.search(r"(?i)robinhood|recommend|predict|take profit|profit target|target price|expected return|base_url|live_mode", added), rel
    assert not [s for s in code_strings(ROOT / "execution" / "activity.py") if "account_number" in s]
    fv = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "forward_validation").glob("*.py")) + ["api/routes/forward_validation.py", "database/forward_validation_migrations.py"]
    text = "".join(now_text(p) for p in fv)
    assert not re.search(r"post_order|insert_intent|enable_auto|\brequests\b|robinhood|update_intent|set_mode\(", text) and text.count("disable_auto(") == 1   # the early stop only
    cap = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "forward_capture").glob("*.py")) + ["database/forward_capture_migrations.py"]
    text = "".join(now_text(p) for p in cap)
    assert not re.search(r"post_order|insert_intent|update_intent|update_position|enable_auto|disable_auto|set_mode\(|\brequests\b|subprocess|robin_stocks|TradingClient", text)
    auto = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "autorun").glob("*.py")) + ["database/autorun_migrations.py"]
    for rel in auto:
        t = now_text(rel)
        assert not re.search(r"post_order|insert_intent|update_intent|update_position|set_mode\(|\brequests\b|subprocess|robin_stocks|TradingClient", t), rel
        assert t.count("enable_auto(") + t.count("disable_auto(") == (2 if rel == "autorun/service.py" else 0), rel   # one arm path, one disarm path
    ps1 = (ROOT / "scripts" / "install_stock_agent_task.ps1").read_bytes()
    assert all(b < 128 for b in ps1)
    assert not re.search(r"(?i)robinhood|rh_gateway|TradingClient|LIVE_CONFIRM|AUTO_LIVE|live[_ ]mode|base_url|url_override|/v2/orders|APCA-API|(PK|AK|SK)[A-Z0-9]{16,}", ps1.decode("ascii"))


def test_F06_the_design_document_states_the_policy_the_semantics_the_rollout_and_the_limits():
    doc = now_text("DESIGN_610A_FORWARD_PAPER_AUTORUN.md")
    for s in (FROZEN_V1, CONTRACT_HASH_OFF, CONTRACT_HASH_ON, "MANUAL_ARM", "SCHEDULED_ARM", "PAPER_AUTO_SCHEDULED_ARM_ENABLED=false", "SEMANTIC_610A", "INSTRUMENTATION_610A",
              "supersedes DESIGN_60", "Phase A", "Phase B", "Phase C", "Phase D", "Phase E", "First paper limits", "06:15 PT", "StockAgentSmokeStart", "READY | DEGRADED | BLOCKED",
              "HEALTHY | DEGRADED | INVALID", "INSUFFICIENT_DATA", "candidate_id", "decision_id", "execution_id", "position_id", "FP_2026_01", "never a broker call",
              "mode=ro", "STOP-and-explain"):
        assert s in doc, s
