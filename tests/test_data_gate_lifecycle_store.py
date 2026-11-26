"""
HDR-2 (EXECUTION_DATA_GATE_SPEC_r14) stage C1b: the safety-finding lifecycle store (r14 3.13.1-3.13.5, 3.13.8 two-row form, 7 "Store API").
Unit evidence only (class S): ExecutionStore.safety_states and ExecutionStore.add_safety_transition over temporary databases. The service-cycle
variants of r14 test 19 (BLIND / HIT / NOT_HIT cycles through run_cycle) are DEFERRED to C1c1 / C1c2 and are not claimed here.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from execution.store import ExecutionStore, ExecutionStoreError

UTC = timezone.utc
T0 = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
STOP, PROT, HELD = "STOP_CHECK_BLIND", "PROTECTIVE_EXIT_UNVERIFIED", "STRATEGY_EXIT_HELD"


def store(tmp_path) -> ExecutionStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    s = ExecutionStore(tmp_path / "lifecycle.db")
    s.ensure()
    return s


def rows(s: ExecutionStore, pid=None):
    c = sqlite3.connect(str(s.path))
    c.row_factory = sqlite3.Row
    try:
        q = "SELECT rowid AS rid, * FROM execution_reconciliations" + (" WHERE position_id = ?" if pid else "") + " ORDER BY rowid"
        return [dict(r) for r in c.execute(q, (pid,) if pid else ())]
    finally:
        c.close()


def raw_insert(s: ExecutionStore, kind, pid="p1", detail="{}", at=T0, symbol="SNDK", blocking=1):
    c = sqlite3.connect(str(s.path))
    try:
        c.execute("INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, position_id, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, None, at.isoformat(timespec="seconds"), "TEST", kind, symbol, blocking, pid, detail))
        c.commit()
    finally:
        c.close()


def state(s, fam=STOP, pid="p1"):
    return s.safety_states([pid])[(fam, pid)]


def alert(s, fam=STOP, pid="p1", expected="NONE", codes=("PRICE_STALE",), now=T0, **extra):
    return s.add_safety_transition(fam, pid, "SNDK", expected, now, "CYCLE", "run-1", alert={"reason_codes": list(codes), **extra})


def resolve(s, fam=STOP, pid="p1", expected="ACTIVE", evidence="STOP_VERIFIED_NOT_HIT", now=T0, **extra):
    return s.add_safety_transition(fam, pid, "SNDK", expected, now, "CYCLE", "run-1", resolution={"evidence": evidence, **extra})


# ---------------------------------------------------------------- r14 3.13.3: states, derivation -------------------------------------------
def test_r14_19_no_row_is_none_for_every_family(tmp_path):
    s = store(tmp_path)
    out = s.safety_states(["p1", "p2"])
    assert set(out) == {(f, p) for f in (STOP, PROT, HELD) for p in ("p1", "p2")}
    assert all(v["state"] == "NONE" and v["alerts"] == 0 and v["rowid"] is None for v in out.values())
    assert s.safety_states([]) == {}
    assert s.safety_states() == {}


def test_r14_19_A_B_A_active_resolved_recurrent_episode_2_three_rows_two_blocking(tmp_path):
    s = store(tmp_path)
    assert alert(s) is True
    assert state(s)["state"] == "ACTIVE"
    assert resolve(s) is True
    assert state(s)["state"] == "RESOLVED"
    assert alert(s, expected="RESOLVED", codes=("FEED_NOT_REAL",)) is True
    assert state(s)["state"] == "RECURRENT"
    r = rows(s)
    assert [x["kind"] for x in r] == [STOP, STOP + "_RESOLVED", STOP]
    assert [x["blocking"] for x in r] == [1, 0, 1]
    d = [json.loads(x["detail_json"]) for x in r]
    assert d[0] == {"lifecycle": "ACTIVE", "episode": 1, "reason_codes": ["PRICE_STALE"]}
    assert d[1] == {"episode": 1, "evidence": "STOP_VERIFIED_NOT_HIT"}
    assert d[2] == {"lifecycle": "RECURRENT", "episode": 2, "reason_codes": ["FEED_NOT_REAL"]}
    assert resolve(s, expected="RECURRENT", evidence="STOP_VERIFIED_HIT") is True
    assert json.loads(rows(s)[-1]["detail_json"]) == {"episode": 2, "evidence": "STOP_VERIFIED_HIT"}


def test_r14_19_3_13_3_row_content_position_id_symbol_trigger_run_id_and_empty_ids(tmp_path):
    s = store(tmp_path)
    alert(s, fam=PROT, exit_cycle=2, codes=("P6_OPEN_ORDER",))
    r = rows(s)[0]
    assert r["kind"] == PROT and r["position_id"] == "p1" and r["symbol"] == "SNDK" and r["blocking"] == 1
    assert r["trigger"] == "CYCLE" and r["run_id"] == "run-1" and r["at"] == T0.isoformat(timespec="seconds")
    assert r["execution_id"] is None and r["broker_order_id"] is None
    assert json.loads(r["detail_json"]) == {"lifecycle": "ACTIVE", "episode": 1, "reason_codes": ["P6_OPEN_ORDER"], "exit_cycle": 2}
    assert state(s, PROT)["exit_cycle"] == 2


@pytest.mark.parametrize("pid,symbol", [("", "SNDK"), (None, "SNDK"), ("p1", ""), ("p1", None)])
def test_r14_19_3_13_2_position_id_and_symbol_are_always_set(tmp_path, pid, symbol):
    s = store(tmp_path)
    with pytest.raises(ExecutionStoreError):
        s.add_safety_transition(STOP, pid, symbol, "NONE", T0, "CYCLE", None, alert={"reason_codes": ["PRICE_STALE"]})
    assert rows(s) == []


def test_r14_19_strategy_family_is_informational_never_blocking(tmp_path):
    s = store(tmp_path)
    assert alert(s, fam=HELD, codes=("DG_CALENDAR_UNCOVERED",), exit_cycle=0, evaluation="2026-11-09T14:30:00+00:00") is True
    r = rows(s)[0]
    assert r["blocking"] == 0
    st = state(s, HELD)
    assert st["state"] == "ACTIVE" and st["evaluation"] == "2026-11-09T14:30:00+00:00" and st["exit_cycle"] == 0
    assert resolve(s, fam=HELD, evidence="HELD_REASONS_CLEARED") is True
    assert rows(s)[-1]["blocking"] == 0 and state(s, HELD)["state"] == "RESOLVED"
    assert state(s, STOP)["state"] == "NONE" and state(s, PROT)["state"] == "NONE"     # families are independent


@pytest.mark.parametrize("fam,evidence", [(STOP, "STRATEGY_EXIT_ACCEPTED"), (STOP, "HELD_REASONS_CLEARED"), (PROT, "STRATEGY_EXIT_ACCEPTED"),
                                          (PROT, "HELD_REASONS_CLEARED"), (PROT, "STOP_VERIFIED_NOT_HIT"), (PROT, "STOP_VERIFIED_HIT"),
                                          (STOP, "PROTECTIVE_EXIT_ACCEPTED"), (STOP, "PROTECTIVE_FACTS_VERIFIED"), (HELD, "STOP_VERIFIED_NOT_HIT"),
                                          (STOP, None), (STOP, "ABSENCE_OF_FAILURE")])
def test_r14_19_3_13_2_N11_evidence_of_another_family_never_resolves(tmp_path, fam, evidence):
    s = store(tmp_path)
    alert(s, fam=fam)
    with pytest.raises(ExecutionStoreError):
        resolve(s, fam=fam, evidence=evidence)
    assert state(s, fam)["state"] == "ACTIVE" and len(rows(s)) == 1


@pytest.mark.parametrize("fam,evidence", [(STOP, "STOP_VERIFIED_NOT_HIT"), (STOP, "STOP_VERIFIED_HIT"), (STOP, "POSITION_CLOSED"),
                                          (PROT, "POSITION_CLOSED"), (PROT, "PROTECTIVE_EXIT_ACCEPTED"), (PROT, "PROTECTIVE_FACTS_VERIFIED"),
                                          (HELD, "STRATEGY_EXIT_ACCEPTED"), (HELD, "POSITION_CLOSED"), (HELD, "HELD_REASONS_CLEARED")])
def test_r14_19_3_13_5_each_family_s_own_evidence_resolves(tmp_path, fam, evidence):
    s = store(tmp_path)
    alert(s, fam=fam)
    assert resolve(s, fam=fam, evidence=evidence) is True
    assert state(s, fam)["state"] == "RESOLVED"


def test_r14_19_3_13_3_derived_resolution_detail_carries_execution_id_and_derived(tmp_path):
    s = store(tmp_path)
    alert(s, fam=PROT, exit_cycle=1)
    assert resolve(s, fam=PROT, evidence="PROTECTIVE_EXIT_ACCEPTED", execution_id="x-1", derived=True) is True
    assert json.loads(rows(s)[-1]["detail_json"]) == {"episode": 1, "evidence": "PROTECTIVE_EXIT_ACCEPTED", "execution_id": "x-1", "derived": True}


@pytest.mark.parametrize("detail", ["not json", "", "null", "[]", "42", '{"lifecycle": "RESOLVED"}', '{"lifecycle": "WEIRD", "exit_cycle": 5}', '{"exit_cycle": 5}',
                                    '{"lifecycle": null}'])
def test_r14_19_unreadable_detail_reads_as_active_and_derived_evidence_does_not_apply(tmp_path, detail):
    s = store(tmp_path)
    raw_insert(s, PROT, detail=detail)
    st = state(s, PROT)
    assert st["state"] == "ACTIVE" and st["detail_readable"] is False
    assert st["exit_cycle"] is None and st["evaluation"] is None                       # (2') / (1') never apply to an unreadable detail


def test_r14_19_readable_detail_with_a_non_integer_exit_cycle_reports_none(tmp_path):
    s = store(tmp_path)
    for i, ec in enumerate(('"3"', "true", "3.0", "null")):
        raw_insert(s, PROT, pid=f"p{i}", detail='{"lifecycle": "ACTIVE", "episode": 1, "reason_codes": [], "exit_cycle": %s}' % ec)
        assert state(s, PROT, f"p{i}")["exit_cycle"] is None and state(s, PROT, f"p{i}")["state"] == "ACTIVE"


def test_r14_19_ordering_by_rowid_not_at_with_the_clock_set_back(tmp_path):
    s = store(tmp_path)
    alert(s, now=T0)
    resolve(s, now=T0 - timedelta(hours=1))                                             # the clock went back between the two rows
    assert state(s)["state"] == "RESOLVED"
    alert(s, expected="RESOLVED", now=T0 - timedelta(hours=2))
    assert state(s)["state"] == "RECURRENT"
    s2 = store(tmp_path / "b")
    raw_insert(s2, STOP + "_RESOLVED", detail='{"episode": 1, "evidence": "STOP_VERIFIED_NOT_HIT"}', at=T0 + timedelta(hours=1))
    raw_insert(s2, STOP, detail='{"lifecycle": "ACTIVE", "episode": 1, "reason_codes": []}', at=T0)
    assert state(s2)["state"] == "ACTIVE"                                               # inserted last wins, although dated earlier


def test_r14_19_no_duplicate_while_unresolved_cas_refuses_a_second_alert(tmp_path):
    s = store(tmp_path)
    assert alert(s, codes=("PRICE_STALE",)) is True
    for code in ("PRICE_MISSING", "FEED_NOT_REAL"):
        assert alert(s, codes=(code,)) is False                                         # the caller's NONE is stale: compare-and-set
    assert len(rows(s)) == 1 and state(s)["state"] == "ACTIVE"


def test_r14_19_compare_and_set_state_changed_between_decision_and_write_gives_no_row(tmp_path):
    s = store(tmp_path)
    alert(s)
    assert resolve(s, expected="RECURRENT") is False                                     # decided from RECURRENT, store says ACTIVE
    assert len(rows(s)) == 1
    resolve(s)
    assert resolve(s) is False                                                            # already RESOLVED by another writer
    assert alert(s, expected="NONE") is False                                             # decided from NONE, store says RESOLVED
    assert len(rows(s)) == 2 and state(s)["state"] == "RESOLVED"


@pytest.mark.parametrize("expected,form", [("ACTIVE", "alert"), ("RECURRENT", "alert"), ("NONE", "resolution"), ("RESOLVED", "resolution"),
                                           ("ACTIVE", "two"), ("BOGUS", "alert")])
def test_r14_3_13_4_illegal_transitions_raise_and_write_nothing(tmp_path, expected, form):
    s = store(tmp_path)
    kw = {"alert": {"reason_codes": ["X"], "deferred": True}} if form in ("alert", "two") else {}
    if form in ("resolution", "two"):
        kw["resolution"] = {"evidence": "STOP_VERIFIED_NOT_HIT"}
    with pytest.raises(ExecutionStoreError):
        s.add_safety_transition(STOP, "p1", "SNDK", expected, T0, "CYCLE", None, **kw)
    assert rows(s) == []


@pytest.mark.parametrize("fam,alert_kw", [(STOP, {"exit_cycle": 1}), (STOP, {"evaluation": "e"}), (PROT, {"evaluation": "e"}), (PROT, {"price": 1.0}),
                                          (PROT, {"exit_cycle": True}), (PROT, {"exit_cycle": "1"}), (HELD, {"evaluation": ""}), (STOP, {"deferred": False}),
                                          (STOP, {"reason_codes": "PRICE_STALE"}), (STOP, {"reason_codes": [None]}), (STOP, {"lifecycle": "RESOLVED"}),
                                          (STOP, {"episode": 9})])
def test_r14_3_13_3_alert_detail_is_stable_values_only(tmp_path, fam, alert_kw):
    s = store(tmp_path)
    a = {"reason_codes": ["X"], **alert_kw}
    with pytest.raises(ExecutionStoreError):
        s.add_safety_transition(fam, "p1", "SNDK", "NONE", T0, "CYCLE", None, alert=a)
    assert rows(s) == []


def test_r14_3_13_8_two_row_form_writes_both_in_one_transaction(tmp_path):
    s = store(tmp_path)
    ok = s.add_safety_transition(PROT, "p1", "SNDK", "NONE", T0, "CYCLE", "r", alert={"reason_codes": ["P2_IDENTITY"], "exit_cycle": 0, "deferred": True},
                                 resolution={"evidence": "POSITION_CLOSED"})
    assert ok is True
    r = rows(s)
    assert [x["kind"] for x in r] == [PROT, PROT + "_RESOLVED"]
    assert json.loads(r[0]["detail_json"]) == {"lifecycle": "ACTIVE", "episode": 1, "reason_codes": ["P2_IDENTITY"], "exit_cycle": 0, "deferred": True}
    assert json.loads(r[1]["detail_json"]) == {"episode": 1, "evidence": "POSITION_CLOSED"}
    assert state(s, PROT)["state"] == "RESOLVED"
    ok = s.add_safety_transition(PROT, "p1", "SNDK", "RESOLVED", T0, "CYCLE", "r", alert={"reason_codes": ["P6"], "deferred": True},
                                 resolution={"evidence": "PROTECTIVE_FACTS_VERIFIED"})
    assert ok is True and json.loads(rows(s)[2]["detail_json"])["lifecycle"] == "RECURRENT" and json.loads(rows(s)[3]["detail_json"])["episode"] == 2


def test_r14_3_13_8_two_row_form_needs_the_deferred_mark(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ExecutionStoreError):
        s.add_safety_transition(STOP, "p1", "SNDK", "NONE", T0, "CYCLE", None, alert={"reason_codes": ["X"]}, resolution={"evidence": "POSITION_CLOSED"})
    assert rows(s) == []


def test_r14_3_13_8_two_row_form_both_or_neither_on_a_failed_second_insert(tmp_path):
    s = store(tmp_path)
    c = sqlite3.connect(str(s.path))
    c.execute("CREATE TRIGGER t_fail BEFORE INSERT ON execution_reconciliations WHEN NEW.kind LIKE '%_RESOLVED' BEGIN SELECT RAISE(ABORT, 'injected'); END;")
    c.commit()
    c.close()
    with pytest.raises(sqlite3.Error):
        s.add_safety_transition(STOP, "p1", "SNDK", "NONE", T0, "CYCLE", None, alert={"reason_codes": ["X"], "deferred": True},
                                resolution={"evidence": "POSITION_CLOSED"})
    assert rows(s) == [] and state(s)["state"] == "NONE"


def test_r14_3_13_3_append_only_rows_are_never_updated_and_other_kinds_are_ignored(tmp_path):
    s = store(tmp_path)
    alert(s)
    raw_insert(s, "QTY_MISMATCH", detail='{"broker_qty": 2}')                           # another kind of the same position: not this key
    raw_insert(s, STOP + "_RESOLVED", pid="p2", detail="{}")                           # another position: not this key
    raw_insert(s, STOP, pid=None, detail='{"lifecycle": "ACTIVE"}')                    # no position_id: never a lifecycle key
    assert state(s)["state"] == "ACTIVE" and state(s)["alerts"] == 1
    assert state(s, STOP, "p2")["state"] == "RESOLVED"
    assert set(s.safety_states()) == {(STOP, "p1"), (STOP, "p2")}
    c = sqlite3.connect(str(s.path))
    with pytest.raises(sqlite3.Error):
        c.execute("UPDATE execution_reconciliations SET detail_json = '{}' WHERE kind = ?", (STOP,))
    c.close()


def test_r14_3_13_3_strict_read_raises_never_empty(tmp_path):
    missing = ExecutionStore(tmp_path / "nope.db")
    with pytest.raises(Exception):
        missing.safety_states(["p1"])
    assert not (tmp_path / "nope.db").exists()
    bare = tmp_path / "bare.db"
    c = sqlite3.connect(str(bare))
    c.execute("CREATE TABLE other (x INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(sqlite3.Error):
        ExecutionStore(bare).safety_states(["p1"])
    with pytest.raises(sqlite3.Error):
        ExecutionStore(bare).safety_states()


def test_r14_3_13_3_episode_counts_every_earlier_alert_row_of_the_key_only(tmp_path):
    s = store(tmp_path)
    for i in range(3):
        assert alert(s, expected="NONE" if i == 0 else "RESOLVED") is True
        assert resolve(s, expected="ACTIVE" if i == 0 else "RECURRENT") is True
    alert(s, fam=PROT)                                                                   # another family: its own count
    alert(s, pid="p2")                                                                   # another position: its own count
    eps = [json.loads(r["detail_json"])["episode"] for r in rows(s, "p1") if r["kind"] == STOP]
    assert eps == [1, 2, 3]
    assert json.loads(rows(s, "p1")[-1]["detail_json"])["episode"] == 1
    assert json.loads(rows(s, "p2")[-1]["detail_json"])["episode"] == 1
    assert state(s)["alerts"] == 3
