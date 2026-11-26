"""
HDR-2 (EXECUTION_DATA_GATE_SPEC_r14) stage C1b: the never-raising lifecycle writer ExecutionService._safety_transition (r14 3.13.4, 3.13.8)
and its structural pins (r14 9.16, 9.19). Unit evidence only (class S): the writer is called directly with a stand-in `self` that has only
the real ExecutionStore (or a raising wrapper of it). No service cycle runs here. DEFERRED to C1c1 / C1c2 (not claimed): test 29's "B is still
evaluated and sent in the same cycle", the derived-evidence sweep (2') in the next cycle, the POSITION_CLOSED sweep retry, the runtime spy
that no cycle routes these kinds through _finding, and the unsaved-blocking-alert latch (E10(iii)).
"""
import ast
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from execution.service import ExecutionService, SimulatedCrash
from execution.store import ExecutionStore

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
T0 = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
STOP, PROT, HELD = "STOP_CHECK_BLIND", "PROTECTIVE_EXIT_UNVERIFIED", "STRATEGY_EXIT_HELD"
KINDS = {STOP, PROT, HELD, STOP + "_RESOLVED", PROT + "_RESOLVED", HELD + "_RESOLVED"}
W = ExecutionService._safety_transition


def svc(tmp_path, name="w.db", store=None):
    s = store or ExecutionStore(Path(tmp_path) / name)
    if store is None:
        s.ensure()
    return SimpleNamespace(store=s)


def rows(s):
    c = sqlite3.connect(str(s.path))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT rowid AS rid, * FROM execution_reconciliations ORDER BY rowid")]
    finally:
        c.close()


def call(self, outcome, fam=STOP, pid="p1", now=T0, **kw):
    return W(self, now, "CYCLE", "run-1", fam, pid, "SNDK", outcome, **kw)


def st(self, fam=STOP, pid="p1"):
    return self.store.safety_states([pid])[(fam, pid)]["state"]


class Raising:
    """The real store with one method raising (the r14 test 29 seam: add_safety_transition raises)."""

    def __init__(self, inner, method, exc=None):
        self.inner, self.method, self.exc, self.calls = inner, method, exc or sqlite3.OperationalError("database is locked"), 0
        self.path = inner.path

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name != self.method:
            return attr

        def boom(*a, **k):
            self.calls += 1
            raise self.exc
        return boom


class Stale:
    """safety_states answers from a snapshot taken before another writer's row (the decision read is stale); the write sees the truth."""

    def __init__(self, inner, snapshot):
        self.inner, self.snapshot, self.reads, self.path = inner, snapshot, 0, inner.path

    def safety_states(self, *a, **k):
        self.reads += 1
        return self.snapshot if self.reads == 1 else self.inner.safety_states(*a, **k)

    def __getattr__(self, name):
        return getattr(self.inner, name)


# ---------------------------------------------------------------- r14 3.13.4: the transition table through the writer ---------------------
def test_r14_19_A_B_A_through_the_writer(tmp_path):
    w = svc(tmp_path)
    assert call(w, "FAILURE", reason_codes=["PRICE_STALE"]) is True and st(w) == "ACTIVE"
    assert call(w, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT") is True and st(w) == "RESOLVED"
    assert call(w, "FAILURE", reason_codes=["PRICE_MISSING"]) is True and st(w) == "RECURRENT"
    r = rows(w.store)
    assert [x["kind"] for x in r] == [STOP, STOP + "_RESOLVED", STOP] and sum(x["blocking"] for x in r) == 2
    assert json.loads(r[2]["detail_json"])["episode"] == 2 and json.loads(r[2]["detail_json"])["lifecycle"] == "RECURRENT"


def test_r14_19_twenty_blind_cycles_with_changing_codes_write_one_alert_row(tmp_path):
    w = svc(tmp_path)
    codes = ["PRICE_STALE"] * 7 + ["PRICE_MISSING"] * 7 + ["FEED_NOT_REAL"] * 6
    for i, c in enumerate(codes):
        assert call(w, "FAILURE", now=T0 + timedelta(seconds=30 * i), reason_codes=[c]) is True
    r = rows(w.store)
    assert len(r) == 1 and json.loads(r[0]["detail_json"])["reason_codes"] == ["PRICE_STALE"]


def test_r14_19_neither_never_writes_or_resolves(tmp_path):
    w = svc(tmp_path)
    call(w, "FAILURE", reason_codes=["PRICE_STALE"])
    for _ in range(10):                                                               # closed market, ATTENTION, in flight, deferral, simulated, ...
        assert call(w, "NEITHER") is True
    assert st(w) == "ACTIVE" and len(rows(w.store)) == 1
    assert call(w, "NEITHER", pid="p2") is True and rows(w.store)[-1]["position_id"] == "p1"


def test_r14_19_evidence_without_an_unresolved_state_writes_nothing(tmp_path):
    w = svc(tmp_path)
    assert call(w, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT") is True and rows(w.store) == []
    call(w, "FAILURE", reason_codes=["X"])
    call(w, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT")
    assert call(w, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT") is True and len(rows(w.store)) == 2


def test_r14_19_N11_strategy_evidence_never_resolves_a_blocking_family_and_never_raises(tmp_path, caplog):
    w = svc(tmp_path)
    for fam in (STOP, PROT):
        call(w, "FAILURE", fam=fam, reason_codes=["X"])
        for ev in ("STRATEGY_EXIT_ACCEPTED", "HELD_REASONS_CLEARED"):
            with caplog.at_level(logging.WARNING, logger="execution.service"):
                assert call(w, "EVIDENCE", fam=fam, evidence=ev) is False
        assert st(w, fam) == "ACTIVE"
    assert len(rows(w.store)) == 2


def test_r14_19_protective_family_records_exit_cycle_and_resolves_on_an_accepted_exit(tmp_path):
    w = svc(tmp_path)
    assert call(w, "FAILURE", fam=PROT, reason_codes=["P6_OPEN_ORDER"], exit_cycle=1) is True
    assert w.store.safety_states(["p1"])[(PROT, "p1")]["exit_cycle"] == 1
    assert call(w, "EVIDENCE", fam=PROT, evidence="PROTECTIVE_EXIT_ACCEPTED", execution_id="x-2") is True
    assert json.loads(rows(w.store)[-1]["detail_json"]) == {"episode": 1, "evidence": "PROTECTIVE_EXIT_ACCEPTED", "execution_id": "x-2"}
    assert call(w, "FAILURE", fam=PROT, reason_codes=["P6_OPEN_ORDER"], exit_cycle=2) is True and st(w, PROT) == "RECURRENT"
    assert call(w, "EVIDENCE", fam=PROT, evidence="PROTECTIVE_EXIT_ACCEPTED", execution_id="x-3", derived=True) is True
    assert json.loads(rows(w.store)[-1]["detail_json"])["derived"] is True


def test_r14_19_strategy_family_records_evaluation(tmp_path):
    w = svc(tmp_path)
    assert call(w, "FAILURE", fam=HELD, reason_codes=["DG_CALENDAR_UNCOVERED"], exit_cycle=0, evaluation="2026-11-09T14:30:00+00:00") is True
    r = rows(w.store)[0]
    assert r["blocking"] == 0 and json.loads(r["detail_json"])["evaluation"] == "2026-11-09T14:30:00+00:00"


def test_r14_3_13_7_state_is_read_from_the_store_never_from_memory(tmp_path):
    a = svc(tmp_path)
    b = SimpleNamespace(store=ExecutionStore(a.store.path))                           # a second process over the same file
    call(a, "FAILURE", reason_codes=["X"])
    assert call(b, "FAILURE", reason_codes=["Y"]) is True and len(rows(a.store)) == 1   # a restart writes no duplicate
    call(b, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT")
    assert call(a, "FAILURE", reason_codes=["Z"]) is True and st(a) == "RECURRENT"     # recurrence is independent of the process


# ---------------------------------------------------------------- r14 3.13.8 / test 29 (unit portion): never raises ------------------------
@pytest.mark.parametrize("method", ["add_safety_transition", "safety_states"])
@pytest.mark.parametrize("outcome,kw", [("FAILURE", {"reason_codes": ["P2"]}), ("EVIDENCE", {"evidence": "PROTECTIVE_EXIT_ACCEPTED", "execution_id": "x"})])
def test_r14_29_a_failing_store_never_raises_out_of_the_writer(tmp_path, caplog, method, outcome, kw):
    base = svc(tmp_path)
    if outcome == "EVIDENCE":
        call(base, "FAILURE", fam=PROT, reason_codes=["P2"])
    w = SimpleNamespace(store=Raising(base.store, method))
    before = rows(base.store)
    with caplog.at_level(logging.WARNING, logger="execution.service"):
        assert call(w, outcome, fam=PROT, **kw) is False
    assert w.store.calls == 1
    assert rows(base.store) == before                                                 # A's state stays as it was (ACTIVE for the resolution)
    assert [r.getMessage() for r in caplog.records if r.name == "execution.service"] == [
        f"safety finding not saved ({type(w.store.exc).__name__})"]


@pytest.mark.parametrize("exc", [RuntimeError("x"), ValueError("x"), KeyError("x"), TypeError("x"), sqlite3.IntegrityError("x"), OSError("x")])
def test_r14_29_any_exception_is_contained(tmp_path, exc):
    w = SimpleNamespace(store=Raising(svc(tmp_path).store, "add_safety_transition", exc))
    assert call(w, "FAILURE", reason_codes=["X"]) is False


def test_r14_3_13_8_simulated_crash_is_the_only_exception_re_raised(tmp_path):
    w = SimpleNamespace(store=Raising(svc(tmp_path).store, "add_safety_transition", SimulatedCrash("injected")))
    with pytest.raises(SimulatedCrash):
        call(w, "FAILURE", reason_codes=["X"])
    w = SimpleNamespace(store=Raising(svc(tmp_path, "b.db").store, "safety_states", SimulatedCrash("injected")))
    with pytest.raises(SimulatedCrash):
        call(w, "FAILURE", reason_codes=["X"])


def test_r14_29_nothing_resolves_on_absence(tmp_path):
    w = svc(tmp_path)
    call(w, "FAILURE", fam=PROT, reason_codes=["P2"], exit_cycle=0)
    for i in range(10):
        assert call(w, "NEITHER", fam=PROT, now=T0 + timedelta(minutes=i)) is True
    assert st(w, PROT) == "ACTIVE" and len(rows(w.store)) == 1


def test_r14_29_a_failed_position_closed_write_is_written_on_the_next_attempt(tmp_path):
    base = svc(tmp_path)
    call(base, "FAILURE", fam=PROT, reason_codes=["P2"])
    bad = SimpleNamespace(store=Raising(base.store, "add_safety_transition"))
    assert call(bad, "EVIDENCE", fam=PROT, evidence="POSITION_CLOSED") is False and st(base, PROT) == "ACTIVE"
    assert call(base, "EVIDENCE", fam=PROT, evidence="POSITION_CLOSED") is True and st(base, PROT) == "RESOLVED"


def test_r14_19_unknown_outcome_or_family_is_contained(tmp_path):
    w = svc(tmp_path)
    assert call(w, "MAYBE") is False
    assert call(w, "FAILURE", fam="QTY_MISMATCH", reason_codes=["X"]) is False
    assert rows(w.store) == []


# ---------------------------------------------------------------- r14 3.13.8: compare-and-set through the writer ---------------------------
def test_r14_19_cas_lost_failure_that_finds_active_returns_true_and_writes_nothing(tmp_path):
    base = svc(tmp_path)
    snap = base.store.safety_states(["p1"])                                           # NONE, taken before another writer's alert
    call(base, "FAILURE", reason_codes=["X"])
    w = SimpleNamespace(store=Stale(base.store, snap))
    assert call(w, "FAILURE", reason_codes=["Y"]) is True                              # a durable blocking row exists
    assert len(rows(base.store)) == 1


def test_r14_19_cas_lost_failure_that_finds_resolved_returns_false(tmp_path):
    base = svc(tmp_path)
    call(base, "FAILURE", reason_codes=["X"])
    snap = {k: dict(v, state="NONE", alerts=0) for k, v in base.store.safety_states(["p1"]).items()}   # a stale NONE decision read
    call(base, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT")                           # now RESOLVED
    w = SimpleNamespace(store=Stale(base.store, snap))
    assert call(w, "FAILURE", reason_codes=["Y"]) is False                             # no ACTIVE / RECURRENT found: not saved
    assert len(rows(base.store)) == 2


def test_r14_19_cas_lost_evidence_returns_false_and_writes_nothing(tmp_path):
    base = svc(tmp_path)
    call(base, "FAILURE", reason_codes=["X"])
    snap = base.store.safety_states(["p1"])                                           # ACTIVE
    call(base, "EVIDENCE", evidence="STOP_VERIFIED_NOT_HIT")
    w = SimpleNamespace(store=Stale(base.store, snap))
    assert call(w, "EVIDENCE", evidence="STOP_VERIFIED_HIT") is False
    assert len(rows(base.store)) == 2


# ---------------------------------------------------------------- structural pins (r14 9.16, 9.19) -----------------------------------------
def _service_tree():
    return ast.parse((ROOT / "execution" / "service.py").read_text(encoding="utf-8"))


def _method(name):
    for node in ast.walk(_service_tree()):
        if isinstance(node, ast.ClassDef) and node.name == "ExecutionService":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == name:
                    return f
    raise AssertionError(name)


def test_r14_9_16_safety_transition_body_is_one_try_whose_handler_re_raises_only_simulated_crash():
    f = _method("_safety_transition")
    assert len(f.body) == 1 and isinstance(f.body[0], ast.Try)
    t = f.body[0]
    assert not t.orelse and not t.finalbody and len(t.handlers) == 1
    h = t.handlers[0]
    assert isinstance(h.type, ast.Name) and h.type.id == "Exception"
    raises = [n for n in ast.walk(h) if isinstance(n, ast.Raise)]
    assert len(raises) == 1 and raises[0].exc is None
    guard = h.body[0]
    assert isinstance(guard, ast.If) and ast.unparse(guard.test) == "isinstance(exc, SimulatedCrash)"
    assert len(guard.body) == 1 and guard.body[0] is raises[0] and not guard.orelse
    assert ast.unparse(h.body[1]) == "log.warning('safety finding not saved (%s)', type(exc).__name__)"
    assert isinstance(h.body[2], ast.Return) and ast.unparse(h.body[2].value) == "False"


def test_r14_9_19_the_safety_kinds_are_never_written_through_finding():
    tree = _service_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Finding":
            for k in node.keywords:
                if k.arg == "kind":
                    assert not (isinstance(k.value, ast.Constant) and k.value.value in KINDS), ast.unparse(node)
    f = _method("_safety_transition")
    calls = {ast.unparse(n.func) for n in ast.walk(f) if isinstance(n, ast.Call)}
    assert "self._finding" not in calls and "self.store.add_findings" not in calls
    assert "self.store.add_safety_transition" in calls and "self.store.safety_states" in calls


def test_r14_7_N8_the_two_store_methods_import_no_safety_package_and_avoid_the_forbidden_tokens():
    import re
    src = (ROOT / "execution" / "store.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) and n.module else [])
            assert not any(x == "safety" or x.startswith("safety.") for x in names)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ExecutionStore")
    forbidden = re.compile(r"post_order|insert_intent|update_intent|update_position|enable_auto|disable_auto|set_mode|submit|_scan_entries\(|_recheck_entry\(|"
                           r"cancel|liquidat|flatten|broker\.")
    for f in cls.body:
        if isinstance(f, ast.FunctionDef) and f.name in ("safety_states", "add_safety_transition"):
            body = ast.get_source_segment(src, f)
            assert not forbidden.search(body), (f.name, forbidden.search(body).group(0))
    wsrc = (ROOT / "execution" / "service.py").read_text(encoding="utf-8")
    seg = ast.get_source_segment(wsrc, _method("_safety_transition"))
    assert not forbidden.search(seg), forbidden.search(seg).group(0)
    module_names = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not {"SAFETY_FAMILIES", "SAFETY_KINDS"} & module_names                      # r14 8.2 row 31: nothing added at module level of store.py
