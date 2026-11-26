"""EXEC-C1a: r14 §9 test 1 (the pure matrix of data_gate.evaluate / stop_status) and test 3 (a single-factor positive control per
negative cell). Offline, synthetic; no broker, store, network or clock read.

Phase 1 note for test 3 on ENTRY: E7 refuses every real-broker entry before U-3 (OD-R2-2(a); r14 §5, brief §7), so the ENTRY
positive control is "only E7 remains" (the cell's fact and code disappear); an allowed real-broker entry is impossible in Phase 1.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from execution import data_gate as dg

UTC = timezone.utc
T = datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)          # Monday 10:00 New York time, a covered session
MASK = "••••"


def iso(dt):
    return dt.isoformat()


def ago(s, base=T):
    return base - timedelta(seconds=s)


def clock(ts=T, is_open=True):
    return {"timestamp": iso(ts) if isinstance(ts, datetime) else ts, "is_open": is_open}


ENV = {"paper": True, "host": dg.PAPER_HOST, "label": "alpaca-paper", "error": None, "blocked": False, "status": "ACTIVE",
       "account_number_masked": MASK + "1234", "simulated": False}
ENTRY_ID = {"account_number_masked": MASK + "1234", "host": dg.PAPER_HOST, "label": "alpaca-paper"}
SETUP = {"mode": "real", "ppe_status": "AVAILABLE", "demand_status": "AVAILABLE", "stretch_status": "AVAILABLE", "quote_status": "OK",
         "ppe_fresh": True, "demand_fresh": True, "quote_fresh": True, "stale_reasons": [], "evaluated_at": iso(ago(60))}
PROPOSAL = {"proposal_id": "P-1", "mode": "real", "warnings": [], "price_mode": "INTRADAY", "price_source": "LAST_TRADE",
            "price_timestamp": iso(ago(100))}


def prop(**kw):
    return {**PROPOSAL, **kw}


def setup(**kw):
    return {**SETUP, **kw}


def entry(**kw):
    base = dict(stage=dg.DG_FINAL, eval_clock=T, broker_simulated=False, environment=ENV, market_mode="real", proposal=PROPOSAL,
                expected_proposal_id="P-1", proposal_executable=True, setup_inputs=SETUP, broker_clock=clock(), calendar=dg.COVERED_SESSION,
                live_price=dg.GatePrice("QUOTE", "10.00", iso(ago(100)), "OK"), stop_check_problems={}, protective_exit_problems={},
                lifecycle_states=[], latch_keys=set(), attention_baseline=dg.AttentionBaseline("c1", {"pos-A": "OPEN"}), cycle_id="c1",
                open_positions_now={"pos-A": "OPEN"}, verified_positions={"pos-A"})
    base.update(kw)
    return dg.EntryFacts(**base)


def sc(**kw):
    base = dict(eval_clock=T, market_mode="real", price=dg.GatePrice("QUOTE", "10.00", iso(ago(100)), "OK"), level="9.00", clock=clock())
    base.update(kw)
    return dg.StopCheckFacts(**base)


HIT_PRICE = dg.GatePrice("QUOTE", "8.50", iso(ago(100)), "OK")
POS = {"symbol": "AAPL", "qty": 10, "qty_available": "10", "side": "long"}


def pexit(**kw):
    base = dict(order_class=dg.PROTECTIVE_EXIT, stage=dg.EXIT_GATE, eval_clock=T, broker_simulated=False, environment=ENV, entry_identity=ENTRY_ID,
                broker_position=POS, order_qty=10, pos_quantity=10, open_orders=[], broker_clock=clock(), near_close_minutes=15,
                calendar=dg.COVERED_SESSION, stop=dg.StopStatus(dg.HIT, None), trigger=sc(price=HIT_PRICE))
    base.update(kw)
    return dg.ExitFacts(**base)


def sexit(**kw):
    base = dict(order_class=dg.STRATEGY_EXIT, stage=dg.EXIT_GATE, eval_clock=T, broker_simulated=False, environment=ENV, entry_identity=ENTRY_ID,
                broker_position=POS, order_qty=10, pos_quantity=10, open_orders=[], broker_clock=clock(), near_close_minutes=15,
                calendar=dg.COVERED_SESSION, reason="TIME_EXIT", recount=dg.Recount(True, 5, None), max_hold_sessions=5)
    base.update(kw)
    return dg.ExitFacts(**base)


# ====================================================================== pins and purity
def test_pinned_constants_equal_their_sources():
    from execution import broker
    from proposals import engine as pe
    assert dg.PAPER_HOST == broker.PAPER_HOST
    assert tuple(dg.SIMULATED_MODES) == tuple(pe.SIMULATED_MODES)
    assert (dg.PRICE_MAX_AGE_SECONDS, dg.MINUTE_BAR_MAX_AGE_SECONDS) == (300, 900)


def test_module_is_pure_no_io_imports_or_clock_reads():
    tree = ast.parse(Path(dg.__file__).read_text(encoding="utf-8"))
    banned_mods = {"execution.broker", "execution.store", "execution.service", "config", "requests", "socket", "sqlite3", "os", "time",
                   "urllib", "http", "subprocess", "autorun.calendar", "autorun"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name in banned_mods or a.name.split(".")[0] in {"requests", "socket", "sqlite3", "os", "time"} for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in banned_mods
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "utcnow", "today", "time_ns", "perf_counter", "open"), node.attr
        if isinstance(node, ast.Name):
            assert node.id != "open"


def test_broker_class_takes_no_intent_cache_or_clock():
    assert list(inspect.signature(dg.broker_class).parameters) == ["simulated", "environment"]


def test_sc_has_no_calendar_input():
    assert not any("calendar" in f.name for f in fields(dg.StopCheckFacts))


# ====================================================================== broker class (§2.2)
@pytest.mark.parametrize("simulated,env,expected", [
    (True, None, dg.SIMULATED),
    (True, dg.UNREADABLE, dg.SIMULATED),
    (False, ENV, dg.REAL),
    (False, {**ENV, "paper": False}, dg.UNKNOWN),
    (False, {**ENV, "host": "api.alpaca.markets"}, dg.UNKNOWN),
    (False, {**ENV, "error": "TIMEOUT"}, dg.UNKNOWN),
    (False, {**ENV, "blocked": True}, dg.UNKNOWN),
    (False, {**ENV, "status": "ACCOUNT_CLOSED"}, dg.UNKNOWN),
    (False, {k: v for k, v in ENV.items() if k != "status"}, dg.UNKNOWN),
    (False, {k: v for k, v in ENV.items() if k != "blocked"}, dg.UNKNOWN),
    (False, dg.UNREADABLE, dg.UNKNOWN),
    (False, None, dg.UNKNOWN),
    (None, ENV, dg.UNKNOWN),
    ("False", ENV, dg.UNKNOWN),
    (0, ENV, dg.UNKNOWN),
])
def test_broker_class(simulated, env, expected):
    assert dg.broker_class(simulated, env) == expected


# ====================================================================== SC (§3.3.2)
def test_sc_baselines():
    assert dg.stop_status(sc()).status == dg.NOT_HIT
    assert dg.stop_status(sc(price=HIT_PRICE)).status == dg.HIT


@pytest.mark.parametrize("mode", [None, "", "REAL", "auto", "test", "simulated", "replay", 1])
def test_sc_feed_not_real_allow_list(mode):
    for price in (sc().price, HIT_PRICE):
        r = dg.stop_status(sc(market_mode=mode, price=price))
        assert (r.status, r.reason) == (dg.BLIND, "FEED_NOT_REAL")


SC_BLIND_CELLS = [
    ("missing_value", dict(price=dg.GatePrice("QUOTE", None, iso(ago(100)), "OK")), "PRICE_MISSING"),
    ("unavailable", dict(price=dg.GatePrice("UNAVAILABLE")), "PRICE_MISSING"),
    ("status_not_ok", dict(price=dg.GatePrice("QUOTE", "10.00", iso(ago(100)), "STALE")), "PRICE_MISSING"),
    ("zero", dict(price=dg.GatePrice("QUOTE", "0", iso(ago(100)), "OK")), "PRICE_INVALID"),
    ("negative", dict(price=dg.GatePrice("QUOTE", "-1", iso(ago(100)), "OK")), "PRICE_INVALID"),
    ("nan", dict(price=dg.GatePrice("QUOTE", "nan", iso(ago(100)), "OK")), "PRICE_INVALID"),
    ("inf", dict(price=dg.GatePrice("QUOTE", "inf", iso(ago(100)), "OK")), "PRICE_INVALID"),
    ("text", dict(price=dg.GatePrice("QUOTE", "abc", iso(ago(100)), "OK")), "PRICE_INVALID"),
    ("undated", dict(price=dg.GatePrice("QUOTE", "10.00", None, "OK")), "PRICE_UNDATED"),
    ("undated_empty", dict(price=dg.GatePrice("QUOTE", "10.00", "", "OK")), "PRICE_UNDATED"),
    ("undated_garbage", dict(price=dg.GatePrice("QUOTE", "10.00", "yesterday", "OK")), "PRICE_UNDATED"),
    ("naive", dict(price=dg.GatePrice("QUOTE", "10.00", "2026-11-23T14:58:20", "OK")), "PRICE_NAIVE"),
    ("stale_quote_301_above_level", dict(price=dg.GatePrice("QUOTE", "10.00", iso(ago(301)), "OK")), "PRICE_STALE"),
    ("stale_minute_901_above_level", dict(price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(901)), "OK", True)), "PRICE_STALE"),
    ("future_61", dict(price=dg.GatePrice("QUOTE", "10.00", iso(T + timedelta(seconds=61)), "OK")), "PRICE_FROM_FUTURE"),
    ("bar_flag_not_final", dict(price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(120)), "OK", False)), "BAR_NOT_FINAL"),
    ("bar_end_after_eval", dict(price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(30)), "OK", True)), "BAR_NOT_FINAL"),
    ("bar_end_after_broker_clock", dict(price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(60)), "OK", True), clock=clock(ago(30))), "BAR_NOT_FINAL"),
    ("skew_61", dict(clock=clock(ago(61))), "CLOCK_SKEW"),
    ("clock_unreadable", dict(clock=dg.UNREADABLE), "CLOCK_UNREADABLE"),
    ("clock_none", dict(clock=None), "CLOCK_UNREADABLE"),
    ("clock_undated", dict(clock={"timestamp": None, "is_open": True}), "CLOCK_UNREADABLE"),
    ("clock_naive", dict(clock={"timestamp": "2026-11-23T15:00:00", "is_open": True}), "CLOCK_UNREADABLE"),
    ("level_none", dict(level=None), "LEVEL_MISSING"),
    ("level_zero", dict(level="0"), "LEVEL_MISSING"),
    ("level_text", dict(level="abc"), "LEVEL_MISSING"),
]


@pytest.mark.parametrize("name,kw,reason", SC_BLIND_CELLS, ids=[c[0] for c in SC_BLIND_CELLS])
def test_sc_blind_cell_and_its_positive_control(name, kw, reason):
    r = dg.stop_status(sc(**kw))
    assert r.status == dg.BLIND and reason in r.codes and r.reason == r.codes[0]
    assert dg.stop_status(sc()).status == dg.NOT_HIT        # test 3: the same fixture with that fact corrected


def test_sc_gate_error_is_blind():
    r = dg.stop_status(sc(eval_clock=datetime(2026, 11, 23, 15, 0, 0)))
    assert (r.status, r.reason) == (dg.BLIND, "GATE_ERROR")


@pytest.mark.parametrize("age,status", [(300, dg.NOT_HIT), (301, dg.BLIND)])
def test_sc_quote_age_boundary(age, status):
    assert dg.stop_status(sc(price=dg.GatePrice("QUOTE", "10.00", iso(ago(age)), "OK"))).status == status


@pytest.mark.parametrize("age,status", [(899, dg.NOT_HIT), (900, dg.NOT_HIT), (901, dg.BLIND), (950, dg.BLIND)])
def test_sc_minute_close_aged_from_stamped_start(age, status):
    r = dg.stop_status(sc(price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(age)), "OK", True)))
    assert r.status == status
    if status == dg.BLIND:
        assert r.codes == ("PRICE_STALE",)


@pytest.mark.parametrize("ahead,status", [(60, dg.NOT_HIT), (61, dg.BLIND)])
def test_sc_future_boundary(ahead, status):
    assert dg.stop_status(sc(price=dg.GatePrice("QUOTE", "10.00", iso(T + timedelta(seconds=ahead)), "OK"))).status == status


@pytest.mark.parametrize("skew,status", [(60, dg.NOT_HIT), (61, dg.BLIND), (-60, dg.NOT_HIT), (-61, dg.BLIND)])
def test_sc_skew_boundary(skew, status):
    assert dg.stop_status(sc(clock=clock(T + timedelta(seconds=skew)))).status == status


def test_sc_slow_cycle_adds_the_read_age():
    old = clock(ago(100))
    assert dg.stop_status(sc(clock=old)).reason == "CLOCK_SKEW"
    assert dg.stop_status(sc(clock=old, clock_read_age_s=100.0)).status == dg.NOT_HIT


@pytest.mark.parametrize("ts", ["2026-11-23T14:58:20Z", "2026-11-23T14:58:20+00:00", "2026-11-23T09:58:20-05:00"])
def test_sc_aware_timestamp_forms(ts):
    assert dg.stop_status(sc(price=dg.GatePrice("QUOTE", "10.00", ts, "OK"))).status == dg.NOT_HIT


T2 = datetime(2026, 11, 23, 19, 0, 0, tzinfo=UTC)          # 14:00 New York time


@pytest.mark.parametrize("ts,status", [
    (datetime(2026, 11, 23, 14, 30, 0, tzinfo=UTC), dg.HIT),         # 09:30:00 New York: inside OD-R2-1(b)
    (datetime(2026, 11, 23, 14, 29, 59, tzinfo=UTC), dg.BLIND),      # 09:29:59: outside
    (datetime(2026, 11, 22, 20, 0, 0, tzinfo=UTC), dg.BLIND),        # a previous date
])
def test_sc_hit_bound_od_r2_1_b(ts, status):
    r = dg.stop_status(sc(eval_clock=T2, clock=clock(T2), price=dg.GatePrice("QUOTE", "8.50", iso(ts), "OK")))
    assert r.status == status
    if status == dg.BLIND:
        assert r.codes == ("PRICE_STALE",)


def test_sc_stale_real_price_above_level_is_blind_but_breach_after_open_is_hit():
    old = iso(datetime(2026, 11, 23, 18, 0, 0, tzinfo=UTC))           # 3600 s old, after 09:30 New York
    assert dg.stop_status(sc(eval_clock=T2, clock=clock(T2), price=dg.GatePrice("QUOTE", "10.00", old, "OK"))).reason == "PRICE_STALE"
    assert dg.stop_status(sc(eval_clock=T2, clock=clock(T2), price=dg.GatePrice("QUOTE", "8.50", old, "OK"))).status == dg.HIT


def test_sc_hit_needs_clock_open_not_hit_does_not():
    assert dg.stop_status(sc(price=HIT_PRICE, clock=clock(is_open=False))).reason == "PRICE_STALE"
    assert dg.stop_status(sc(clock=clock(is_open=False))).status == dg.NOT_HIT


def test_sc_minute_close_hit_and_boundary_price_equal_level_is_hit():
    assert dg.stop_status(sc(price=dg.GatePrice("MINUTE_CLOSE", "8.50", iso(ago(900)), "OK", True))).status == dg.HIT
    assert dg.stop_status(sc(price=dg.GatePrice("QUOTE", "9.00", iso(ago(100)), "OK"))).status == dg.HIT


def test_sc_every_failing_reason_listed_in_order():
    r = dg.stop_status(sc(market_mode="test", price=dg.GatePrice("QUOTE", "10.00", "2026-11-23T14:58:20", "OK"), clock=dg.UNREADABLE, level=None))
    assert r.codes == ("FEED_NOT_REAL", "PRICE_NAIVE", "CLOCK_UNREADABLE", "LEVEL_MISSING")
    assert r.reason == "FEED_NOT_REAL"


# ====================================================================== ENTRY (§3.2)
def only_e7(v):
    return (v.allowed, v.codes, v.failed_facts) == (False, ("DG_UNVERIFIED",), ("E7",))


@pytest.mark.parametrize("stage", [dg.DG_EARLY, dg.DG_FINAL])
def test_entry_baseline_refused_only_by_e7_in_phase1(stage):
    v = dg.evaluate(entry(stage=stage, live_price=None if stage == dg.DG_EARLY else entry().live_price))
    assert only_e7(v) and v.order_class == dg.ENTRY and v.code == "DG_UNVERIFIED"


def test_entry_simulated_broker_allows_and_records_nothing():
    v = dg.evaluate(entry(broker_simulated=True, market_mode="test", calendar=dg.UNCOVERED))
    assert (v.allowed, v.code, v.codes, v.failed_facts, v.provenance) == (True, None, (), (), {})


@pytest.mark.parametrize("sim,env", [(False, {**ENV, "paper": False}), (False, dg.UNREADABLE), (None, ENV), (False, {**ENV, "blocked": True})])
def test_entry_unknown_broker_is_dg_unknown_never_simulated_branch(sim, env):
    v = dg.evaluate(entry(broker_simulated=sim, environment=env))
    assert (v.allowed, v.codes, v.failed_facts) == (False, ("DG_UNKNOWN",), ("E0",))


ENTRY_CELLS = [
    # E1
    ("E1_none", dict(market_mode=None), "E1", "DG_UNKNOWN"),
    ("E1_empty", dict(market_mode=""), "E1", "DG_UNKNOWN"),
    ("E1_REAL", dict(market_mode="REAL"), "E1", "DG_UNKNOWN"),
    ("E1_auto", dict(market_mode="auto"), "E1", "DG_UNKNOWN"),
    ("E1_test", dict(market_mode="test"), "E1", "DG_SIMULATED"),
    ("E1_simulated", dict(market_mode="simulated"), "E1", "DG_SIMULATED"),
    ("E1_replay", dict(market_mode="replay"), "E1", "DG_SIMULATED"),
    # E2
    ("E2_unreadable", dict(proposal=dg.UNREADABLE), "E2", "DG_UNVERIFIED"),
    ("E2_none", dict(proposal=None), "E2", "DG_UNVERIFIED"),
    ("E2_other_id", dict(proposal=prop(proposal_id="P-2")), "E2", "DG_UNVERIFIED"),
    ("E2_mode_test", dict(proposal=prop(mode="test")), "E2", "DG_SIMULATED"),
    ("E2_mode_REAL", dict(proposal=prop(mode="REAL")), "E2", "DG_UNVERIFIED"),
    ("E2_mode_empty", dict(proposal=prop(mode="")), "E2", "DG_UNVERIFIED"),
    ("E2_simulated_warning", dict(proposal=prop(warnings=[{"code": "SIMULATED_DATA", "text": "x"}])), "E2", "DG_SIMULATED"),
    ("E2_not_executable", dict(proposal_executable=False), "E2", "DG_UNVERIFIED"),
    ("E2_executable_none", dict(proposal_executable=None), "E2", "DG_UNVERIFIED"),
    # E5 (DG-final)
    ("E5_unavailable", dict(live_price=dg.GatePrice("UNAVAILABLE")), "E5", "DG_UNVERIFIED"),
    ("E5_none", dict(live_price=None), "E5", "DG_UNVERIFIED"),
    ("E5_status", dict(live_price=dg.GatePrice("QUOTE", "10.00", iso(ago(100)), "STALE")), "E5", "DG_UNVERIFIED"),
    ("E5_quote_301", dict(live_price=dg.GatePrice("QUOTE", "10.00", iso(ago(301)), "OK")), "E5", "DG_STALE"),
    ("E5_minute_901", dict(live_price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(901)), "OK", True)), "E5", "DG_STALE"),
    ("E5_minute_not_final", dict(live_price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(120)), "OK", False)), "E5", "DG_UNVERIFIED"),
    ("E5_minute_forming", dict(live_price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(30)), "OK", True)), "E5", "DG_UNVERIFIED"),
    ("E5_naive", dict(live_price=dg.GatePrice("QUOTE", "10.00", "2026-11-23T14:58:20", "OK")), "E5", "DG_UNVERIFIED"),
    ("E5_future_61", dict(live_price=dg.GatePrice("QUOTE", "10.00", iso(T + timedelta(seconds=61)), "OK")), "E5", "DG_UNVERIFIED"),
    # E6
    ("E6_mode_test", dict(setup_inputs=setup(mode="test")), "E6", "DG_SIMULATED"),
    ("E6_mode_REAL", dict(setup_inputs=setup(mode="REAL")), "E6", "DG_SIMULATED"),
    ("E6_ppe_simulated", dict(setup_inputs=setup(ppe_status="SIMULATED_DATA")), "E6", "DG_SIMULATED"),
    ("E6_demand_insufficient", dict(setup_inputs=setup(demand_status="INSUFFICIENT_DATA")), "E6", "DG_INCOMPLETE"),
    ("E6_stretch_na", dict(setup_inputs=setup(stretch_status="NOT_AVAILABLE")), "E6", "DG_INCOMPLETE"),
    ("E6_quote_status", dict(setup_inputs=setup(quote_status="UNAVAILABLE")), "E6", "DG_INCOMPLETE"),
    ("E6_ppe_fresh", dict(setup_inputs=setup(ppe_fresh=False)), "E6", "DG_STALE"),
    ("E6_demand_fresh", dict(setup_inputs=setup(demand_fresh=False)), "E6", "DG_STALE"),
    ("E6_quote_fresh", dict(setup_inputs=setup(quote_fresh=False)), "E6", "DG_STALE"),
    ("E6_stale_reasons", dict(setup_inputs=setup(stale_reasons=["DEMAND_STALE"])), "E6", "DG_STALE"),
    ("E6_evaluated_at_missing", dict(setup_inputs=setup(evaluated_at=None)), "E6", "DG_INCOMPLETE"),
    ("E6_evaluated_at_naive", dict(setup_inputs=setup(evaluated_at="2026-11-23T14:59:00")), "E6", "DG_INCOMPLETE"),
    ("E6_evaluated_at_ahead", dict(setup_inputs=setup(evaluated_at=iso(T + timedelta(seconds=1)))), "E6", "DG_INCOMPLETE"),
    ("E6_unreadable", dict(setup_inputs=dg.UNREADABLE), "E6", "DG_INCOMPLETE"),
    # E8
    ("E8_uncovered", dict(calendar=dg.UNCOVERED), "E8", "DG_CALENDAR_UNCOVERED"),
    ("E8_unreadable", dict(calendar=dg.UNREADABLE_CALENDAR), "E8", "DG_CALENDAR_UNREADABLE"),
    ("E8_garbage_is_unreadable", dict(calendar=None), "E8", "DG_CALENDAR_UNREADABLE"),
    ("E8_closed", dict(calendar=dg.COVERED_CLOSED), "E8", "DG_CALENDAR_CLOSED"),
    ("E8_broker_closed", dict(broker_clock=clock(is_open=False)), "E8", "DG_UNVERIFIED"),
    ("E8_clock_unreadable", dict(broker_clock=dg.UNREADABLE), "E8", "DG_UNVERIFIED"),
    # E9
    ("E9_skew_61", dict(broker_clock=clock(ago(61))), "E9", "DG_UNVERIFIED"),
    ("E9_clock_naive", dict(broker_clock={"timestamp": "2026-11-23T15:00:00", "is_open": True}), "E9", "DG_UNVERIFIED"),
    # E10(i)
    ("E10i_blind", dict(stop_check_problems={"AAPL": "PRICE_STALE"}), "E10(i)", "DG_SAFETY_CHECK_FAILED"),
    ("E10i_refused", dict(protective_exit_problems={"AAPL": "P2"}), "E10(i)", "DG_SAFETY_CHECK_FAILED"),
    ("E10i_unreadable", dict(stop_check_problems=None), "E10(i)", "DG_SAFETY_STATE_UNREADABLE"),
    # E10(ii)
    ("E10ii_blind_active", dict(lifecycle_states=[("pos-A", "STOP_CHECK_BLIND", "ACTIVE")]), "E10(ii)", "DG_SAFETY_CHECK_FAILED"),
    ("E10ii_blind_recurrent", dict(lifecycle_states=[("pos-A", "STOP_CHECK_BLIND", "RECURRENT")]), "E10(ii)", "DG_SAFETY_CHECK_FAILED"),
    ("E10ii_protective_active", dict(lifecycle_states=[("pos-A", "PROTECTIVE_EXIT_UNVERIFIED", "ACTIVE")]), "E10(ii)", "DG_SAFETY_CHECK_FAILED"),
    ("E10ii_unreadable_state", dict(lifecycle_states=[("pos-A", "PROTECTIVE_EXIT_UNVERIFIED", None)]), "E10(ii)", "DG_SAFETY_CHECK_FAILED"),
    ("E10ii_unreadable_read", dict(lifecycle_states=dg.UNREADABLE), "E10(ii)", "DG_SAFETY_STATE_UNREADABLE"),
    # E10(iii)
    ("E10iii_latch", dict(latch_keys={("pos-A", "PROTECTIVE_EXIT_UNVERIFIED")}), "E10(iii)", "DG_SAFETY_ALERT_UNSAVED"),
    # E10(iv)
    ("E10iv_unverified", dict(verified_positions=set()), "E10(iv)", "DG_SAFETY_UNVERIFIED_SINCE_START"),
    ("E10iv_sr_unreadable", dict(attention_baseline=dg.UNREADABLE), "E10(iv)", "DG_SAFETY_STATE_UNREADABLE"),
    ("E10iv_read2_unreadable", dict(open_positions_now=dg.UNREADABLE), "E10(iv)", "DG_SAFETY_STATE_UNREADABLE"),
    # E10(v)
    ("E10v_entered", dict(open_positions_now={"pos-A": "ATTENTION"}), "E10(v)", "DG_ATTENTION_ENTERED_THIS_CYCLE"),
    ("E10v_stale_cycle", dict(attention_baseline=dg.AttentionBaseline("c0", {"pos-A": "OPEN"})), "E10(v)", "DG_SAFETY_STATE_UNREADABLE"),
    ("E10v_no_cycle", dict(cycle_id=None), "E10(v)", "DG_SAFETY_STATE_UNREADABLE"),
    ("E10v_sr_bad_state", dict(attention_baseline=dg.AttentionBaseline("c1", {"pos-A": "???"})), "E10(v)", "DG_SAFETY_STATE_UNREADABLE"),
    ("E10v_read2_bad_state", dict(open_positions_now={"pos-A": None}), "E10(v)", "DG_SAFETY_STATE_UNREADABLE"),
    ("E10v_read2_unreadable", dict(open_positions_now=dg.UNREADABLE), "E10(v)", "DG_SAFETY_STATE_UNREADABLE"),
]


@pytest.mark.parametrize("name,kw,fact,code", ENTRY_CELLS, ids=[c[0] for c in ENTRY_CELLS])
def test_entry_negative_cell_and_single_factor_positive_control(name, kw, fact, code):
    v = dg.evaluate(entry(**kw))
    assert v.allowed is False
    assert fact in v.failed_facts and code in v.codes
    assert v.code == v.codes[0]
    assert only_e7(dg.evaluate(entry()))                     # test 3 (Phase 1 form): with that fact corrected only E7 remains


def test_entry_e5_is_a_dg_final_fact_only():
    assert only_e7(dg.evaluate(entry(stage=dg.DG_EARLY, live_price=None)))
    assert "E5" in dg.evaluate(entry(stage=dg.DG_FINAL, live_price=None)).failed_facts


@pytest.mark.parametrize("age,ok", [(300, True), (301, False)])
def test_entry_e5_quote_boundary(age, ok):
    v = dg.evaluate(entry(live_price=dg.GatePrice("QUOTE", "10.00", iso(ago(age)), "OK")))
    assert only_e7(v) is ok


@pytest.mark.parametrize("age,ok", [(900, True), (901, False)])
def test_entry_e5_minute_close_boundary_from_start(age, ok):
    v = dg.evaluate(entry(live_price=dg.GatePrice("MINUTE_CLOSE", "10.00", iso(ago(age)), "OK", True)))
    assert only_e7(v) is ok


@pytest.mark.parametrize("skew,ok", [(60, True), (61, False)])
def test_entry_e9_skew_boundary(skew, ok):
    ts = T - timedelta(seconds=skew)
    v = dg.evaluate(entry(broker_clock=clock(ts), proposal=prop(price_timestamp=iso(ago(100))), live_price=dg.GatePrice("QUOTE", "10", iso(ago(100)), "OK")))
    assert only_e7(v) is ok
    if not ok:
        assert {"E8", "E9"} <= set(v.failed_facts)


@pytest.mark.parametrize("ahead,ok", [(60, True), (61, False)])
def test_entry_e4_future_boundary(ahead, ok):
    v = dg.evaluate(entry(proposal=prop(price_timestamp=iso(T + timedelta(seconds=ahead)))))
    assert only_e7(v) is ok


def test_entry_e10_positive_cells():
    assert only_e7(dg.evaluate(entry(lifecycle_states=[("pos-A", "STOP_CHECK_BLIND", "RESOLVED")])))
    assert only_e7(dg.evaluate(entry(lifecycle_states=[("pos-A", "STRATEGY_EXIT_HELD", "ACTIVE")])))   # never blocks entries
    # in ATTENTION in BOTH S_R and read 2: left out of (iv), not in N_t
    both = dict(attention_baseline=dg.AttentionBaseline("c1", {"pos-A": "ATTENTION"}), open_positions_now={"pos-A": "ATTENTION"}, verified_positions=set())
    assert only_e7(dg.evaluate(entry(**both)))


def test_entry_e10_iv_uses_sr_and_read2_not_read2_alone():
    # in ATTENTION at the evaluation only (entered during the positions step), not verified: (iv) AND (v) refuse
    v = dg.evaluate(entry(open_positions_now={"pos-A": "ATTENTION"}, verified_positions=set()))
    assert {"E10(iv)", "E10(v)"} <= set(v.failed_facts)
    assert v.codes.index("DG_SAFETY_UNVERIFIED_SINCE_START") < v.codes.index("DG_ATTENTION_ENTERED_THIS_CYCLE")
    # cleared in RECONCILE (in ATTENTION in S_R only), not verified: in scope of (iv)
    v = dg.evaluate(entry(attention_baseline=dg.AttentionBaseline("c1", {"pos-A": "ATTENTION"}), open_positions_now={"pos-A": "OPEN"}, verified_positions=set()))
    assert "E10(iv)" in v.failed_facts and "E10(v)" not in v.failed_facts


def test_entry_e10_v_is_a_per_id_set_difference_not_a_count():
    v = dg.evaluate(entry(attention_baseline=dg.AttentionBaseline("c1", {"pos-A": "OPEN", "pos-B": "ATTENTION"}),
                          open_positions_now={"pos-A": "ATTENTION"}, verified_positions={"pos-A"}))
    assert "E10(v)" in v.failed_facts and "DG_ATTENTION_ENTERED_THIS_CYCLE" in v.codes


def test_entry_unreadable_sr_fails_both_iv_and_v_separately():
    v = dg.evaluate(entry(attention_baseline=None))
    assert {"E10(iv)", "E10(v)"} <= set(v.failed_facts) and "DG_SAFETY_STATE_UNREADABLE" in v.codes


def test_entry_precedence_lists_every_failing_code():
    v = dg.evaluate(entry(market_mode="test", calendar=dg.UNCOVERED, latch_keys={"k"}, setup_inputs=setup(ppe_fresh=False)))
    assert v.failed_facts == ("E1", "E6", "E7", "E8", "E10(iii)")
    assert v.codes == ("DG_SIMULATED", "DG_STALE", "DG_UNVERIFIED", "DG_CALENDAR_UNCOVERED", "DG_SAFETY_ALERT_UNSAVED")
    assert v.code == "DG_SIMULATED"


def test_entry_exception_never_allows():
    v = dg.evaluate(entry(eval_clock=datetime(2026, 11, 23, 15, 0, 0)))
    assert (v.allowed, v.code) == (False, "DG_GATE_ERROR")


def test_unknown_order_class_is_refused():
    v = dg.evaluate(object())
    assert v.allowed is False and v.code == "DG_UNKNOWN"


# ====================================================================== PROTECTIVE EXIT (§3.5)
@pytest.mark.parametrize("stage", [dg.EXIT_GATE, dg.DG_FINAL])
def test_protective_baseline_allowed(stage):
    v = dg.evaluate(pexit(stage=stage))
    assert (v.allowed, v.outcome, v.codes) == (True, dg.ALLOW, ())
    assert v.provenance["calendar"] == dg.COVERED_SESSION


def test_protective_simulated_broker_allows_and_records_nothing():
    v = dg.evaluate(pexit(broker_simulated=True, calendar=dg.UNREADABLE_CALENDAR, stop=dg.StopStatus(dg.BLIND, "FEED_NOT_REAL")))
    assert (v.allowed, v.provenance) == (True, {})


NEAR_CLOSE = datetime(2026, 11, 23, 20, 50, 0, tzinfo=UTC)   # 15:50 New York: inside the final 15 minutes
EVENING = datetime(2026, 11, 24, 1, 0, 0, tzinfo=UTC)        # 20:00 New York

PROTECTIVE_CELLS = [
    # P9
    ("P9_not_hit", dict(stop=dg.StopStatus(dg.NOT_HIT, None)), dg.NOT_ENTERED, "P9", "P9"),
    ("P9_blind", dict(stop=dg.StopStatus(dg.BLIND, "PRICE_STALE")), dg.NOT_ENTERED, "P9", "P9"),
    ("P9_none", dict(stop=None), dg.NOT_ENTERED, "P9", "P9"),
    # P8a-1 (deferral at the exit gate; no broker read needed: an unreadable clock does not matter)
    ("P8a1_near_close", dict(eval_clock=NEAR_CLOSE, broker_clock=clock(NEAR_CLOSE)), dg.DEFER, "P8a-1", "P8a-1"),
    ("P8a1_evening_clock_unreadable", dict(eval_clock=EVENING, broker_clock=dg.UNREADABLE), dg.DEFER, "P8a-1", "P8a-1"),
    ("P8a1_dg_final", dict(stage=dg.DG_FINAL, eval_clock=NEAR_CLOSE, broker_clock=clock(NEAR_CLOSE)), dg.SESSION_FAILURE, "P8a-1", "DG_FINAL_SESSION_FAILED"),
    # P8b (refusal)
    ("P8b_unreadable", dict(broker_clock=dg.UNREADABLE), dg.REFUSE, "P8b", "P8_CLOCK_UNREADABLE"),
    ("P8b_none", dict(broker_clock=None), dg.REFUSE, "P8b", "P8_CLOCK_UNREADABLE"),
    ("P8b_undated", dict(broker_clock={"timestamp": None, "is_open": True}), dg.REFUSE, "P8b", "P8_CLOCK_UNDATED"),
    ("P8b_naive", dict(broker_clock={"timestamp": "2026-11-23T15:00:00", "is_open": True}), dg.REFUSE, "P8b", "P8_CLOCK_NAIVE"),
    ("P8b_naive_and_closed_is_refusal_not_deferral", dict(broker_clock={"timestamp": "2026-11-23T15:00:00", "is_open": False}), dg.REFUSE, "P8b", "P8_CLOCK_NAIVE"),
    ("P8b_skew_61", dict(broker_clock=clock(T + timedelta(seconds=61))), dg.REFUSE, "P8b", "P8_CLOCK_SKEW"),
    ("P8b_skew_61_closed", dict(broker_clock=clock(ago(61), is_open=False)), dg.REFUSE, "P8b", "P8_CLOCK_SKEW"),
    # P8a-2 (OD-R5-1(a): a verified closed clock is a deferral at the exit gate; a DG-final session failure at DG-final)
    ("P8a2_closed", dict(broker_clock=clock(is_open=False)), dg.DEFER, "P8a-2", "P8a-2"),
    ("P8a2_dg_final", dict(stage=dg.DG_FINAL, broker_clock=clock(is_open=False)), dg.SESSION_FAILURE, "P8a-2", "DG_FINAL_SESSION_FAILED"),
    # P1
    ("P1_paper_false", dict(environment={**ENV, "paper": False}), dg.REFUSE, "P1", "P1"),
    ("P1_blocked", dict(environment={**ENV, "blocked": True}), dg.REFUSE, "P1", "P1"),
    ("P1_not_active", dict(environment={**ENV, "status": "INACTIVE"}), dg.REFUSE, "P1", "P1"),
    ("P1_error", dict(environment={**ENV, "error": "TIMEOUT"}), dg.REFUSE, "P1", "P1"),
    ("P1_unreadable", dict(environment=dg.UNREADABLE), dg.REFUSE, "P1", "P1"),
    ("P1_unknown_flag", dict(broker_simulated=None), dg.REFUSE, "P1", "P1"),
    # P2
    ("P2_other_account", dict(entry_identity={**ENTRY_ID, "account_number_masked": MASK + "9999"}), dg.REFUSE, "P2", "P2"),
    ("P2_placeholder_entry", dict(entry_identity={**ENTRY_ID, "account_number_masked": MASK}), dg.REFUSE, "P2", "P2"),
    ("P2_placeholder_env", dict(environment={**ENV, "account_number_masked": MASK}), dg.REFUSE, "P2", "P2"),
    ("P2_empty_entry", dict(entry_identity={**ENTRY_ID, "account_number_masked": ""}), dg.REFUSE, "P2", "P2"),
    ("P2_none_env", dict(environment={**ENV, "account_number_masked": None}), dg.REFUSE, "P2", "P2"),
    ("P2_link_missing", dict(entry_identity=None), dg.REFUSE, "P2", "P2"),
    ("P2_link_unreadable", dict(entry_identity=dg.UNREADABLE), dg.REFUSE, "P2", "P2"),
    ("P2_host", dict(entry_identity={**ENTRY_ID, "host": "other"}), dg.REFUSE, "P2", "P2"),
    ("P2_label", dict(entry_identity={**ENTRY_ID, "label": "other"}), dg.REFUSE, "P2", "P2"),
    # P3 (§3.5.2's exact condition gives QTY_DISAGREEMENT; a short side with a positive quantity is §3.5.3)
    ("P3_side_short", dict(broker_position={**POS, "side": "short"}), dg.REFUSE, "P3", "P3_SIDE"),
    ("P3_side_missing", dict(broker_position={k: v for k, v in POS.items() if k != "side"}), dg.REFUSE, "P3", "P3_SIDE"),
    ("P3_flat", dict(broker_position=None), dg.QTY_DISAGREEMENT, "P3", "P3_FLAT"),
    ("P3_fewer", dict(broker_position={**POS, "qty": 5, "qty_available": "5"}), dg.QTY_DISAGREEMENT, "P3", "P3_QTY"),
    ("P3_negative", dict(broker_position={**POS, "qty": -10, "qty_available": "-10"}), dg.QTY_DISAGREEMENT, "P3", "P3_FLAT"),
    ("P3_unreadable", dict(broker_position=dg.UNREADABLE), dg.REFUSE, "P3", "P3"),
    # P4
    ("P4_available_short", dict(broker_position={**POS, "qty_available": "9.5"}), dg.REFUSE, "P4", "P4_AVAILABLE"),
    ("P4_unparsable", dict(broker_position={**POS, "qty_available": "abc"}), dg.REFUSE, "P4", "P4_UNPARSABLE"),
    ("P4_missing", dict(broker_position={**POS, "qty_available": None}), dg.REFUSE, "P4", "P4_UNPARSABLE"),
    # P5
    ("P5_differs", dict(order_qty=9), dg.REFUSE, "P5", "P5"),
    ("P5_zero", dict(order_qty=0, pos_quantity=0), dg.REFUSE, "P5", "P5"),
    ("P5_fractional", dict(order_qty="1.5", pos_quantity="1.5"), dg.REFUSE, "P5", "P5"),
    ("P5_bool", dict(order_qty=True, pos_quantity=1), dg.REFUSE, "P5", "P5"),
    # P6
    ("P6_open_sell", dict(open_orders=[{"id": "o1", "side": "sell", "symbol": "AAPL"}]), dg.REFUSE, "P6", "P6"),
    ("P6_open_buy", dict(open_orders=[{"id": "o2", "side": "buy", "symbol": "AAPL"}]), dg.REFUSE, "P6", "P6"),
    ("P6_foreign_origin", dict(open_orders=[{"id": "o3", "client_order_id": "manual-web", "symbol": "AAPL"}]), dg.REFUSE, "P6", "P6"),
    ("P6_unreadable", dict(open_orders=dg.UNREADABLE), dg.REFUSE, "P6", "P6"),
    ("P6_none", dict(open_orders=None), dg.REFUSE, "P6", "P6"),
    # P7 (derived; fails with a short side)
    ("P7_short", dict(broker_position={**POS, "side": "short"}), dg.REFUSE, "P7", "P7"),
    # P8c
    ("P8c_covered_closed", dict(calendar=dg.COVERED_CLOSED), dg.REFUSE, "P8c", "P8_SESSION_SOURCES_DISAGREE"),
    ("P8c_unreadable", dict(calendar=dg.UNREADABLE_CALENDAR), dg.REFUSE, "P8c", "P8_CALENDAR_UNREADABLE"),
    ("P8c_garbage", dict(calendar="covered"), dg.REFUSE, "P8c", "P8_CALENDAR_UNREADABLE"),
    # uncovered date: P8b and P8a-2 still apply
    ("uncovered_clock_unreadable", dict(calendar=dg.UNCOVERED, broker_clock=dg.UNREADABLE), dg.REFUSE, "P8b", "P8_CLOCK_UNREADABLE"),
    ("uncovered_skew_61", dict(calendar=dg.UNCOVERED, broker_clock=clock(ago(61))), dg.REFUSE, "P8b", "P8_CLOCK_SKEW"),
    ("uncovered_closed", dict(calendar=dg.UNCOVERED, broker_clock=clock(is_open=False)), dg.DEFER, "P8a-2", "P8a-2"),
    # DG-final trigger validity (the level is not re-tested)
    ("P9_dg_final_feed", dict(stage=dg.DG_FINAL, trigger=sc(price=HIT_PRICE, market_mode="test")), dg.REFUSE, "P9", "FEED_NOT_REAL"),
    ("P9_dg_final_before_open", dict(stage=dg.DG_FINAL, trigger=sc(price=dg.GatePrice("QUOTE", "8.5", iso(datetime(2026, 11, 23, 14, 0, tzinfo=UTC)), "OK"))),
     dg.REFUSE, "P9", "PRICE_STALE"),
    ("P9_dg_final_missing", dict(stage=dg.DG_FINAL, trigger=None), dg.REFUSE, "P9", "PRICE_MISSING"),
]


@pytest.mark.parametrize("name,kw,outcome,fact,code", PROTECTIVE_CELLS, ids=[c[0] for c in PROTECTIVE_CELLS])
def test_protective_negative_cell_and_single_factor_positive_control(name, kw, outcome, fact, code):
    v = dg.evaluate(pexit(**kw))
    assert v.allowed is False
    assert v.outcome == outcome
    assert fact in v.failed_facts and code in v.codes and v.code == v.codes[0]
    corrected = pexit(stage=kw.get("stage", dg.EXIT_GATE))
    assert dg.evaluate(corrected).allowed is True             # test 3


def test_protective_dg_final_session_failure_names_the_step():
    v = dg.evaluate(pexit(stage=dg.DG_FINAL, broker_clock=clock(is_open=False)))
    assert v.provenance["step"] == "P8a-2" and v.provenance["session_reason"]


@pytest.mark.parametrize("kw", [
    dict(calendar=dg.UNCOVERED),
    dict(broker_position={**POS, "qty": 11, "qty_available": "10.5"}),        # fractional qty_available >= order
    dict(broker_clock=clock(T + timedelta(seconds=60))),                       # skew 60
    dict(stage=dg.DG_FINAL, trigger=sc(price=dg.GatePrice("QUOTE", "10.00", iso(ago(100)), "OK"))),   # level not re-tested at DG-final
])
def test_protective_positive_cells(kw):
    v = dg.evaluate(pexit(**kw))
    assert v.allowed is True and v.outcome == dg.ALLOW


def test_protective_uncovered_provenance_records_calendar_uncovered():
    assert dg.evaluate(pexit(calendar=dg.UNCOVERED)).provenance["calendar"] == "UNCOVERED"


def test_protective_precedence_lists_every_failing_code():
    v = dg.evaluate(pexit(entry_identity=None, open_orders=[{"id": "x"}], calendar=dg.COVERED_CLOSED, order_qty=9))
    assert v.failed_facts == ("P2", "P5", "P6", "P8c")
    assert v.codes == ("P2", "P5", "P6", "P8_SESSION_SOURCES_DISAGREE")
    assert v.code == "P2" and v.outcome == dg.REFUSE
    v = dg.evaluate(pexit(broker_position={**POS, "side": "short", "qty_available": "x"}, environment={**ENV, "paper": False}))
    assert v.failed_facts == ("P1", "P3", "P4", "P7")


def test_protective_exception_is_gate_error_refusal():
    v = dg.evaluate(pexit(eval_clock=datetime(2026, 11, 23, 15, 0, 0)))
    assert (v.allowed, v.code, v.outcome) == (False, "GATE_ERROR", dg.REFUSE)


# ====================================================================== STRATEGY EXIT (§3.6, X8)
def test_strategy_time_exit_allowed_with_recount():
    v = dg.evaluate(sexit())
    assert (v.allowed, v.order_class) == (True, dg.STRATEGY_EXIT)


STRATEGY_CELLS = [
    ("evidence", dict(reason="EVIDENCE_INVALIDATION"), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("setup", dict(reason="SETUP_INVALIDATED"), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("demand", dict(reason="DEMAND_FAILURE"), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("dislocation", dict(reason="DISLOCATION_NORMALIZED"), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("hard_inv_never_strategy", dict(reason="HARD_INVALIDATION"), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("reason_none", dict(reason=None), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("recount_short", dict(recount=dg.Recount(True, 4, None)), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("recount_missing", dict(recount=None), dg.HOLD, "REASON", "DG_UNVERIFIED"),
    ("recount_uncovered", dict(recount=dg.Recount(False, None, "DG_CALENDAR_UNCOVERED", ("DG_CALENDAR_UNCOVERED",))), dg.HOLD, "REASON", "DG_CALENDAR_UNCOVERED"),
    ("recount_unreadable", dict(recount=dg.Recount(False, None, "DG_CALENDAR_UNREADABLE", ("DG_CALENDAR_UNREADABLE",))), dg.HOLD, "REASON", "DG_CALENDAR_UNREADABLE"),
    ("X8_uncovered", dict(calendar=dg.UNCOVERED), dg.HOLD, "X8", "DG_CALENDAR_UNCOVERED"),
    ("X8_unreadable", dict(calendar=dg.UNREADABLE_CALENDAR), dg.HOLD, "X8", "DG_CALENDAR_UNREADABLE"),
    ("X8_closed", dict(calendar=dg.COVERED_CLOSED), dg.HOLD, "X8", "DG_CALENDAR_CLOSED"),
    ("P6_open_order", dict(open_orders=[{"id": "o1"}]), dg.HOLD, "P6", "P6"),
    ("P2_identity", dict(entry_identity=None), dg.HOLD, "P2", "P2"),
    ("P3_flat", dict(broker_position=None), dg.QTY_DISAGREEMENT, "P3", "P3_FLAT"),
    ("P8b_unreadable", dict(broker_clock=dg.UNREADABLE), dg.HOLD, "P8b", "P8_CLOCK_UNREADABLE"),
    ("P8a2_closed", dict(broker_clock=clock(is_open=False)), dg.DEFER, "P8a-2", "P8a-2"),
    ("P8a1_near_close", dict(eval_clock=NEAR_CLOSE, broker_clock=clock(NEAR_CLOSE)), dg.DEFER, "P8a-1", "P8a-1"),
    ("dg_final_session", dict(stage=dg.DG_FINAL, broker_clock=clock(is_open=False)), dg.SESSION_FAILURE, "P8a-2", "DG_FINAL_SESSION_FAILED"),
]


@pytest.mark.parametrize("name,kw,outcome,fact,code", STRATEGY_CELLS, ids=[c[0] for c in STRATEGY_CELLS])
def test_strategy_negative_cell_and_single_factor_positive_control(name, kw, outcome, fact, code):
    v = dg.evaluate(sexit(**kw))
    assert v.allowed is False and v.outcome == outcome
    assert fact in v.failed_facts and code in v.codes
    assert dg.evaluate(sexit(stage=kw.get("stage", dg.EXIT_GATE))).allowed is True     # test 3


def test_strategy_on_uncovered_date_is_held_never_protective():
    v = dg.evaluate(sexit(calendar=dg.UNCOVERED))
    assert v.outcome == dg.HOLD and v.order_class == dg.STRATEGY_EXIT


@pytest.mark.parametrize("stage", [dg.EXIT_GATE, dg.DG_FINAL])
def test_strategy_both_stages_allowed(stage):
    assert dg.evaluate(sexit(stage=stage)).allowed is True


# ====================================================================== calendar facts and the TIME_EXIT recount (§2.6, §3.6)
def _cal():
    from autorun import calendar as cal
    return cal.covered, cal.is_market_day


@pytest.mark.parametrize("d,fact", [("2026-11-23", dg.COVERED_SESSION), ("2026-11-26", dg.COVERED_CLOSED), ("2026-11-28", dg.COVERED_CLOSED),
                                    ("2026-11-27", dg.COVERED_SESSION), ("2028-01-03", dg.UNCOVERED)])
def test_classify_calendar_with_the_table(d, fact):
    from datetime import date
    assert dg.classify_calendar(date.fromisoformat(d), *_cal()) == fact


def test_classify_calendar_unreadable_is_never_uncovered():
    from datetime import date
    d = date(2026, 11, 23)

    def boom(_d):
        raise ImportError("broken deploy")
    assert dg.classify_calendar(d, boom, lambda _d: True) == dg.UNREADABLE_CALENDAR
    assert dg.classify_calendar(d, lambda _d: 1, lambda _d: True) == dg.UNREADABLE_CALENDAR
    assert dg.classify_calendar(d, lambda _d: True, boom) == dg.UNREADABLE_CALENDAR
    assert dg.classify_calendar(d, lambda _d: True, lambda _d: None) == dg.UNREADABLE_CALENDAR


def test_recount_skips_a_holiday_counts_an_early_close_and_is_not_the_weekday_count():
    from execution.engine import sessions_held
    opened, now = "2026-11-25T15:00:00+00:00", datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
    r = dg.time_exit_recount(opened, now, *_cal())
    assert r.verified and r.sessions == 2                   # Fri 27 (early close) and Mon 30; Thu 26 is a holiday
    assert sessions_held(opened, now) == 3                  # the engine's weekday count differs
    assert dict(r.dates)["2026-11-26"] == dg.COVERED_CLOSED


def test_recount_entry_day_counts_zero():
    r = dg.time_exit_recount(iso(ago(3600)), T, *_cal())
    assert (r.verified, r.sessions) == (True, 0)


def test_recount_uncovered_date_in_window_is_unverified():
    r = dg.time_exit_recount("2027-12-30T15:00:00+00:00", datetime(2028, 1, 3, 15, 0, tzinfo=UTC), *_cal())
    assert (r.verified, r.code) == (False, "DG_CALENDAR_UNCOVERED")


def test_recount_unreadable_calendar_has_its_own_code():
    def boom(_d):
        raise RuntimeError("x")
    r = dg.time_exit_recount("2026-11-20T15:00:00+00:00", T, boom, lambda _d: True)
    assert (r.verified, r.code) == (False, "DG_CALENDAR_UNREADABLE")
    r = dg.time_exit_recount("2026-11-20T15:00:00+00:00", T, lambda _d: "yes", lambda _d: True)
    assert r.code == "DG_CALENDAR_UNREADABLE"


def test_recount_unreadable_and_uncovered_both_listed():
    from datetime import date

    def cov(d):
        if d == date(2028, 1, 1):
            raise RuntimeError("x")
        return d.year in (2026, 2027)
    r = dg.time_exit_recount("2027-12-30T15:00:00+00:00", datetime(2028, 1, 3, 15, 0, tzinfo=UTC), cov, lambda _d: True)
    assert r.codes == ("DG_CALENDAR_UNREADABLE", "DG_CALENDAR_UNCOVERED")


@pytest.mark.parametrize("opened", ["2026-11-20T15:00:00", None, "", "garbage"])
def test_recount_unverified_opened_at(opened):
    assert dg.time_exit_recount(opened, T, *_cal()).code == "DG_UNVERIFIED"


def test_recount_feeds_the_strategy_row():
    rc = dg.time_exit_recount("2026-11-16T15:00:00+00:00", T, *_cal())        # Mon 16 -> Mon 23: 5 sessions
    assert rc.sessions == 5
    assert dg.evaluate(sexit(recount=rc, max_hold_sessions=5)).allowed is True
    assert dg.evaluate(sexit(recount=rc, max_hold_sessions=6)).allowed is False
