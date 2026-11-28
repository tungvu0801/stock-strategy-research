"""
HDR-2 (EXECUTION_DATA_GATE_SPEC_r14) stage EXEC-C1c1: the exit half of the r14 9.16 structural pins, the AUTH EXEC_4 BEHAVIOUR pins (post_order
keeps one call site, _submit exactly two callers, DG-final the first statement of _submit, the OD-C-7(a) exhaustion block's lines unchanged at
step 4 of the 3.8 exit-gate order) and condition (d) (no fact read once P8b fails or a P8a step ends the evaluation), by AST over
execution/service.py and by read counts through the real seams. Entry-side pins (E10, DG-early, the latch, _attention, card) are EXEC-C1c2.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from execution import broker as BK
from test_data_gate_exits_service import Rig, T0, clock_answer, hit_rig

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "execution" / "service.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
CLS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "ExecutionService")
FN = {n.name: n for n in CLS.body if isinstance(n, ast.FunctionDef)}
GATE_FUNCS = ("_simulated_broker", "_broker_class", "_entry_identity", "_market_mode", "_gate_price", "_gate_calendar", "_reads_left", "_gate_facts",
              "_session_and_clock", "_gate_prov", "_exit_refused", "_exit_contained", "_exit_gate_pre", "_exit_gate_intent", "_exit_qty", "_dg_final_exit",
              "_dg_final_refused", "_dg_final_session_failure", "_exit_after_post", "_safety_sweeps", "_exit_context_gated", "_evaluate_positions_gated")
EXHAUSTION = (                                                                     # r15 9-C A6 (OD-C-7 (b1)): (the if line, the four existing body lines)
    '        if pos.exit_cycle >= self.max_exit_cycles:\n',
    '            self.store.update_position(pos.position_id, now, {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": "EXIT_ATTEMPTS_EXHAUSTED"},\n'
    '                                       state="ATTENTION", attention_reason="EXIT_ATTEMPTS_EXHAUSTED")\n'
    '            self._touch(now, "EXIT_SUBMISSION", "BLOCK", f"{pos.symbol}: {pos.exit_cycle} exit attempts exhausted; needs attention", {"position_id": pos.position_id})\n'
    '            return False\n')


def calls(node, attr):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == attr]


def attrs(node):
    return {a.attr for a in ast.walk(node) if isinstance(a, ast.Attribute)}


def enclosing(name_attr):
    out = []
    for fn in FN.values():
        if calls(fn, name_attr):
            out.extend([fn.name] * len(calls(fn, name_attr)))
    return out


def test_gate_functions_exist():
    assert set(GATE_FUNCS) <= set(FN)


def test_9_16_post_order_has_one_call_site_and_submit_exactly_two_callers():
    assert len(calls(TREE, "post_order")) == 1 and enclosing("post_order") == ["_submit"]
    assert sorted(enclosing("_submit")) == ["_maybe_submit_exit", "_scan_entries"]


def test_9_16_dg_final_is_the_first_statement_of_submit_and_precedes_the_post():
    first = FN["_submit"].body[0]
    assert isinstance(first, ast.If) and isinstance(first.body[0], ast.Return)
    src = ast.unparse(first.test)
    assert isinstance(first.test, ast.BoolOp) and isinstance(first.test.op, ast.And) and ast.unparse(first.test.values[0]) == "it.intent_type == 'EXIT'"
    neg = first.test.values[1]
    assert isinstance(neg, ast.UnaryOp) and isinstance(neg.op, ast.Not) and ast.unparse(neg.operand) == "self._dg_final_exit(it, now, control_id, gate)", src
    post_line = calls(FN["_submit"], "post_order")[0].lineno
    assert first.lineno < post_line
    attempt = [c for c in calls(FN["_submit"], "update_intent") if any(k.arg == "attempt_count" for k in c.keywords)]
    assert attempt and first.lineno < attempt[0].lineno < post_line


def test_9_16_broker_class_reads_no_cache_no_intent_environment_no_clock_cache():
    forbidden = {"environment_cache", "_env_at", "_clock_cache", "_transport", "left"}
    for name in ("_broker_class", "_simulated_broker"):
        assert not (attrs(FN[name]) & forbidden), name
    env_reads = [a for a in ast.walk(FN["_broker_class"]) if isinstance(a, ast.Attribute) and a.attr == "environment"]
    assert len(env_reads) == 1 and ast.unparse(env_reads[0].value) == "self.broker"


def test_9_16_the_only_intent_environment_read_in_the_gate_is_entry_identity():
    hits = []
    for name in GATE_FUNCS:
        for a in ast.walk(FN[name]):
            if isinstance(a, ast.Attribute) and a.attr == "environment":
                hits.append((name, ast.unparse(a.value)))
    other = [(n, v) for n, v in hits if v != "self.broker"]
    assert ("_entry_identity", "ent") in other
    assert all(v in ("ent", "facts") for _, v in other) and {n for n, v in other if v == "ent"} == {"_entry_identity"}
    assert {n for n, v in other if v == "facts"} == {"_exit_gate_intent"}             # ExitFacts.environment: the FRESH P1 read, copied into the new intent


def test_9_16_the_gate_never_reads_transport_left_or_the_volatile_caches_for_a_decision():
    for name in GATE_FUNCS:
        assert not (attrs(FN[name]) & {"_transport", "left", "environment_cache", "_env_at", "_clock_cache", "_finding_sigs"}), name


def test_9_16_real_broker_exit_intent_reason_is_the_decided_reason_never_pos_exit_reason():
    builds = [c for c in ast.walk(FN["_exit_gate_intent"]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "Intent"]
    assert len(builds) == 1
    kw = {k.arg: ast.unparse(k.value) for k in builds[0].keywords}
    assert kw["exit_reason"] == "decided"
    assert "pos.exit_reason" not in ast.unparse(FN["_exit_gate_intent"])
    upd = [c for c in calls(FN["_exit_gate_intent"], "update_position")]
    assert len(upd) == 1 and {k.arg: ast.unparse(k.value) for k in upd[0].keywords}["exit_reason"] == "decided"


def test_9_16_sc_is_computed_before_and_apart_from_the_exit_decision_and_never_reads_the_calendar():
    fn = FN["_evaluate_positions_gated"]
    sc = calls(fn, "stop_status")
    dec = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "exit_decision"]
    assert len(sc) == 1 and len(dec) == 1 and sc[0].lineno < dec[0].lineno
    assert "dec" not in {n.id for n in ast.walk(sc[0]) if isinstance(n, ast.Name)}
    for name in ("_gate_price", "_market_mode"):
        assert "calendar" not in ast.unparse(FN[name]).lower()
    assert "calendar" not in ast.unparse(sc[0]).lower()


def test_9_16_safety_transition_is_one_try_whose_handler_reraises_only_simulated_crash():
    body = FN["_safety_transition"].body
    assert len(body) == 1 and isinstance(body[0], ast.Try) and len(body[0].handlers) == 1
    h = ast.unparse(body[0].handlers[0])
    assert "isinstance(exc, SimulatedCrash)" in h and h.count("raise") == 1


def test_a1_service_side_the_writer_s_return_value_is_never_branched_on():
    for fn in FN.values():
        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "_safety_transition" and fn.name != "_safety_transition":
                    assert isinstance(node, ast.Expr), (fn.name, child.lineno)


def test_od_c7a_exhaustion_block_lines_unchanged_once_and_at_step_4_of_the_exit_gate_order():
    if_line, body = EXHAUSTION
    assert SRC.count(if_line) == 1 and SRC.count(body) == 1
    fn = FN["_maybe_submit_exit"]
    fn_src = ast.get_source_segment(SRC, fn)
    assert if_line in fn_src and body in fn_src + "\n"
    pre = calls(fn, "_exit_gate_pre")[0].lineno
    post = calls(fn, "_exit_gate_intent")[0].lineno
    ex = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == "pos.exit_cycle >= self.max_exit_cycles")
    assert pre < ex.lineno < post                                                  # P8a-1, reservation, P8b, P8a-2 -> exhaustion -> collision, P1-P7, P8c
    first = SRC[:SRC.index(body)].count("\n") + 1                                  # (b1): the four existing lines are contiguous and last in the if body,
    assert len(ex.body) == 4 and ex.body[1].lineno == first and ex.end_lineno == first + 3 and ex.body[0].end_lineno == first - 1   # after step 4 (i)-(iii)
    inner = ex.body[0]
    assert isinstance(inner, ast.If) and not inner.orelse and ast.unparse(inner.test) == "gate is not None and gate.get('order_class') == DG.PROTECTIVE_EXIT"
    assert [type(s).__name__ for s in inner.body] == ["Assign", "Assign", "Expr", "Expr"] and SRC.splitlines()[ex.lineno - 1] + "\n" == if_line


def test_simulated_branch_keeps_today_s_session_check_and_intent_build():
    fn_src = ast.get_source_segment(SRC, FN["_maybe_submit_exit"])
    for line in ('sp = session_policy(now, self._clock_cache, self.near_close_minutes)', 'exit_reason=pos.exit_reason, reference_price=pos.last_price',
                 'environment=dict(self.environment_cache))', 'if existing is None or existing.state not in INTENT_OPEN:'):
        assert line in fn_src


def test_lifecycle_kinds_are_never_written_through_finding():
    for fn in FN.values():
        for c in calls(fn, "_finding"):
            assert not any(k in ast.unparse(c) for k in ("STOP_CHECK_BLIND", "PROTECTIVE_EXIT_UNVERIFIED", "STRATEGY_EXIT_HELD")), fn.name


# ================================================================ condition (d): no fact read after P8b fails or a P8a step ends ===============
def test_condition_d_p8b_failure_reads_no_p1_to_p7_fact(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["clock"] = [None, BK.BrokerError("TIMEOUT")]
    rig.cycle()
    assert rig.broker.n("environment") == rig.broker.n("position") == rig.broker.n("orders") == 0 and rig.posts() == 0


def test_condition_d_p8a1_deferral_reads_nothing_from_the_broker(tmp_path):
    rig = Rig(tmp_path, t0=datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc))
    rig.open()
    rig.live("SNDK", "8.50")
    rig.cycle()
    assert rig.broker.n("clock") == 1 and rig.broker.n("environment") == rig.broker.n("position") == rig.broker.n("orders") == 0


def test_condition_d_p8a2_deferral_reads_no_p1_to_p7_fact(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.broker.faults["clock"] = [None, clock_answer(rig, is_open=False)]
    rig.cycle()
    assert rig.broker.n("clock") == 2 and rig.broker.n("environment") == rig.broker.n("position") == rig.broker.n("orders") == 0


def test_condition_d_dg_final_session_failure_reads_no_further_fact(tmp_path):
    rig, p = hit_rig(tmp_path)
    rig.x.hooks["after_exit_intent"] = lambda ctx: setattr(rig.sim, "is_open", False)
    rig.cycle()
    assert rig.broker.n("clock") == 3 and rig.broker.n("environment") == 1 and rig.broker.n("position") == 1 and rig.broker.n("orders") == 1
