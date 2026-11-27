"""
HDR-2 (EXECUTION_DATA_GATE_SPEC_r14) stage C1b: the recovery filter (OD-R4-4(a), r14 3.13.9, r14:1463) and forward validation's lifecycle
branch and carry-in (OD-R3-1(a), OD-R5-7(b), r14 3.13.10, r14:1464). Unit portions of r14 tests 21 and 31 (class S), over temporary databases,
with the lifecycle rows written by the real ExecutionStore.add_safety_transition. Only the chosen options are implemented, so the (a'), (b)
and OD-R5-7(a) variants of tests 21 / 31 are NOT APPLICABLE. DEFERRED to C1c1 / C1c2 (not claimed): the same outcomes driven by service cycles.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from account_refresh.store import AccountRefreshStore
from database.account_refresh_migrations import run_account_refresh_migrations
from database.execution_migrations import run_execution_migrations
from execution import recovery as R
from execution.store import ExecutionStore
from forward_validation.service import BLOCKING_FINDINGS, ForwardValidationService
from forward_validation.store import ForwardValidationStore

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)                         # Wednesday 11:00 New York (the recovery tests' regular-session instant)
PID = "pos-1"
STOP, PROT, HELD = "STOP_CHECK_BLIND", "PROTECTIVE_EXIT_UNVERIFIED", "STRATEGY_EXIT_HELD"


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def sql(path, q, args=()):
    c = sqlite3.connect(str(path), timeout=10)
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def write_alert(path, fam, at, pid=PID, expected="NONE", **extra):
    assert ExecutionStore(path).add_safety_transition(fam, pid, "SNDK", expected, at, "CYCLE", None, alert={"reason_codes": ["X"], **extra}) is True


def write_resolution(path, fam, at, evidence, pid=PID, expected="ACTIVE"):
    assert ExecutionStore(path).add_safety_transition(fam, pid, "SNDK", expected, at, "CYCLE", None, resolution={"evidence": evidence}) is True


# ================================================================ r14 test 21: recovery under OD-R4-4(a) ==================================
def recovery_db(path, *, finding_kind="QTY_MISMATCH", broker=2, reason="QTY_MISMATCH"):
    """A recoverable PARTIAL as test_bot_position_recovery's Lab builds it: ATTENTION position (5 shares), its QTY finding, a fresh
    snapshot holding `broker` shares, a live executor run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    run_execution_migrations(c)
    run_account_refresh_migrations(c)
    c.close()
    at = lambda s: iso(NOW + timedelta(seconds=s))  # noqa: E731
    sql(path, "INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, attention_reason, exit_reason, "
              "exit_cycle, exit_qty, realized_pnl, updated_at) VALUES (?,?,?,?,?,'100.00',?,?,?,?,?,?,?,?)",
        (PID, "x-" + PID, "SNDK", 5, 5, at(-3600), "ATTENTION", reason, None, 0, 0, None, at(-300)))
    sql(path, "INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, position_id, detail_json) VALUES (?,?,?,?,?,?,1,?,?)",
        (uuid.uuid4().hex, None, at(-290), "TEST", finding_kind, "SNDK", PID, json.dumps({"internal_qty": 5, "broker_qty": broker})))
    rid = uuid.uuid4().hex
    snap = {"refresh_id": rid, "refreshed_at": at(-30), "source": "SIMULATED_PAPER", "host": "paper-api.alpaca.markets", "account": {"status": "ACTIVE"},
            "positions": [{"symbol": "SNDK", "qty": str(broker), "side": "long"}] if broker else [], "open_orders": []}
    AccountRefreshStore(path).commit_success(snap, {"started_at": at(-30), "finished_at": at(-30), "trigger": "TEST", "result": "SUCCESS", "read_attempts": 1,
                                                    "refresh_id": rid, "owner": "test"}, NOW - timedelta(seconds=30))
    sql(path, "INSERT INTO execution_runs (run_id, started_at, finished_at, trigger, mode, result, summary_json, error, owner) VALUES (?,?,?,?,?,?,'{}',?,?)",
        (uuid.uuid4().hex, at(-20), at(-19), "SCHEDULED", "MONITOR_ONLY", "RUN", None, "test"))
    return path


def p7_p10(path):
    pv = R.preview(path, PID, NOW)
    return {c["n"]: (c["ok"], c["detail"]) for c in pv["preconditions"] if c["n"] in (7, 10)}, pv


@pytest.mark.parametrize("fam", [STOP, PROT, HELD])
@pytest.mark.parametrize("offset", [-10, -295, -1])
def test_r14_21_recovery_a_lifecycle_row_after_or_before_the_qty_finding_gives_today_s_preconditions_7_and_10(tmp_path, fam, offset):
    base, _ = p7_p10(recovery_db(tmp_path / "base" / "r.db"))
    assert base[7][0] is True and base[10][0] is True
    path = recovery_db(tmp_path / "life" / "r.db")
    write_alert(path, fam, NOW + timedelta(seconds=offset), **({"exit_cycle": 0} if fam != STOP else {}))
    got, pv = p7_p10(path)
    assert got == base                                                                   # exactly today's results: the QTY finding stays the latest finding
    assert pv["ok"] is True


def test_r14_21_recovery_a_resolution_row_after_the_qty_finding_is_ignored_too(tmp_path):
    base, _ = p7_p10(recovery_db(tmp_path / "base" / "r.db"))
    path = recovery_db(tmp_path / "life" / "r.db")
    write_alert(path, PROT, NOW - timedelta(seconds=295), exit_cycle=0)
    write_resolution(path, PROT, NOW - timedelta(seconds=5), "PROTECTIVE_FACTS_VERIFIED")
    assert p7_p10(path)[0] == base


def test_r14_21_recovery_today_s_refusals_are_unchanged_by_the_filter(tmp_path):
    dis = recovery_db(tmp_path / "dis" / "r.db", broker=3)                               # snapshot 3; a later QTY finding says 1: they disagree
    sql(dis, "INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, position_id, detail_json) VALUES (?,?,?,?,?,?,1,?,?)",
        (uuid.uuid4().hex, None, iso(NOW - timedelta(seconds=200)), "TEST", "QTY_MISMATCH", "SNDK", PID, json.dumps({"internal_qty": 5, "broker_qty": 1})))
    write_alert(dis, STOP, NOW - timedelta(seconds=10))
    got, pv = p7_p10(dis)
    assert got[10][0] is False and "FINDING_DISAGREES" in pv["refusals"] and "broker 1" in got[10][1]   # the latest QTY finding decides, never the safety row
    late = recovery_db(tmp_path / "late" / "r.db")
    sql(late, "INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, position_id, detail_json) VALUES (?,?,?,?,?,?,1,?,?)",
        (uuid.uuid4().hex, None, iso(NOW - timedelta(seconds=10)), "TEST", "QTY_MISMATCH", "SNDK", PID, json.dumps({"internal_qty": 5, "broker_qty": 2})))
    got, pv = p7_p10(late)
    assert got[7][0] is False and "SNAPSHOT_OLDER_THAN_FINDING" in pv["refusals"]        # a QTY finding after the snapshot still refuses (today's rule)


# ================================================================ r14 test 21: forward-validation classification (OD-R3-1(a)) ================
def fv_on(path) -> ForwardValidationService:
    ExecutionStore(path).ensure()
    return ForwardValidationService(lambda: None, ForwardValidationStore(path), clock=lambda: NOW)


def rec_rows(path):
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT *, rowid AS _rowid FROM execution_reconciliations ORDER BY rowid")]
    finally:
        c.close()


def test_r14_21_OD_R3_1a_the_two_blocking_kinds_are_blocking_the_others_informational():
    assert STOP in BLOCKING_FINDINGS and PROT in BLOCKING_FINDINGS
    for k in (HELD, STOP + "_RESOLVED", PROT + "_RESOLVED", HELD + "_RESOLVED"):
        assert k not in BLOCKING_FINDINGS


def test_r14_21_classification_of_every_lifecycle_row(tmp_path):
    db = tmp_path / "fv.db"
    fv = fv_on(db)
    write_alert(db, STOP, NOW - timedelta(minutes=30))
    write_alert(db, PROT, NOW - timedelta(minutes=29), exit_cycle=0)
    write_alert(db, HELD, NOW - timedelta(minutes=28), exit_cycle=0)
    rec = fv.reconciliation(None)
    assert rec["open"] == 2 and {d["kind"]: d["resolution"] for d in rec["detail"]} == {STOP: "OPEN", PROT: "OPEN"}
    write_resolution(db, STOP, NOW - timedelta(minutes=20), "STOP_VERIFIED_NOT_HIT")
    write_resolution(db, HELD, NOW - timedelta(minutes=20), "HELD_REASONS_CLEARED")
    res = {r["kind"]: fv._resolution(r) for r in rec_rows(db)}
    assert res == {STOP: "RESOLVED", PROT: "OPEN", HELD: "INFORMATIONAL", STOP + "_RESOLVED": "INFORMATIONAL", HELD + "_RESOLVED": "INFORMATIONAL"}
    assert fv.reconciliation(None)["open"] == 1
    write_alert(db, STOP, NOW - timedelta(minutes=10), expected="RESOLVED")              # RECURRENT: a new OPEN row, the first stays RESOLVED
    got = [(r["kind"], fv._resolution(r)) for r in rec_rows(db) if r["kind"] == STOP]
    assert got == [(STOP, "RESOLVED"), (STOP, "OPEN")]
    assert fv.reconciliation(None)["open"] == 2


def test_r14_21_resolution_follows_rowid_not_at(tmp_path):
    db = tmp_path / "fv.db"
    fv = fv_on(db)
    write_alert(db, STOP, NOW)
    write_resolution(db, STOP, NOW - timedelta(hours=1), "STOP_VERIFIED_HIT")             # the clock went back: dated earlier, inserted later
    assert [fv._resolution(r) for r in rec_rows(db)][0] == "RESOLVED"


def test_r14_21_the_lifecycle_branch_precedes_the_position_id_branch(tmp_path):
    db = tmp_path / "fv.db"
    fv = fv_on(db)
    write_alert(db, PROT, NOW - timedelta(minutes=30), exit_cycle=0)
    sql(db, "INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, PID, iso(NOW - timedelta(minutes=20)), "ATTENTION_CLEARED", "ATTENTION", "EXIT_REQUIRED", "test", "{}"))
    assert fv._resolution(rec_rows(db)[0]) == "OPEN"                                     # ATTENTION_CLEARED resolves a QTY finding, never a lifecycle alert
    write_resolution(db, PROT, NOW - timedelta(minutes=10), "PROTECTIVE_FACTS_VERIFIED")
    assert fv._resolution(rec_rows(db)[0]) == "RESOLVED"                                 # the resolution row, with no CLOSED event


def test_r14_21_a_later_closed_position_event_resolves_and_an_earlier_one_does_not(tmp_path):
    db = tmp_path / "fv.db"
    fv = fv_on(db)
    sql(db, "INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, PID, iso(NOW - timedelta(hours=2)), "CLOSED", "EXIT_SUBMITTED", "CLOSED", "test", "{}"))
    write_alert(db, STOP, NOW - timedelta(minutes=30))
    assert fv._resolution(rec_rows(db)[0]) == "OPEN"
    sql(db, "INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, PID, iso(NOW - timedelta(minutes=1)), "MANUAL_RECOVERY", "ATTENTION", "CLOSED", "test", "{}"))
    assert fv._resolution(rec_rows(db)[0]) == "RESOLVED"


def test_r14_21_another_position_s_or_family_s_resolution_never_resolves(tmp_path):
    db = tmp_path / "fv.db"
    fv = fv_on(db)
    write_alert(db, STOP, NOW - timedelta(minutes=30))
    write_alert(db, PROT, NOW - timedelta(minutes=30), exit_cycle=0)
    write_alert(db, STOP, NOW - timedelta(minutes=30), pid="pos-2")
    write_resolution(db, PROT, NOW - timedelta(minutes=20), "POSITION_CLOSED")
    write_resolution(db, STOP, NOW - timedelta(minutes=20), "STOP_VERIFIED_NOT_HIT", pid="pos-2")
    res = [(r["kind"], r["position_id"], fv._resolution(r)) for r in rec_rows(db) if r["kind"] in (STOP, PROT)]
    assert res == [(STOP, PID, "OPEN"), (PROT, PID, "RESOLVED"), (STOP, "pos-2", "RESOLVED")]


def test_r14_21_a_row_without_rowid_or_position_reads_open(tmp_path):
    db = tmp_path / "fv.db"
    fv = fv_on(db)
    write_alert(db, STOP, NOW - timedelta(minutes=30))
    write_resolution(db, STOP, NOW - timedelta(minutes=20), "STOP_VERIFIED_NOT_HIT")
    r = rec_rows(db)[0]
    assert fv._resolution(r) == "RESOLVED"
    assert fv._resolution({k: v for k, v in r.items() if k != "_rowid"}) == "OPEN"       # fail closed: never resolved without its rowid
    assert fv._resolution(dict(r, _rowid=str(r["_rowid"]))) == "OPEN"
    assert fv._resolution(dict(r, position_id=None)) == "OPEN"


# ================================================================ r14 tests 21 and 31: run readiness, all-time record, promotion, carry-in ===
import wave2_prc_support as C  # noqa: E402

RNOW = C.et(2026, 11, 9, 11, 0)                                                       # Monday 11:00 ET (test_wave2_prc_run's start instant)
LATER = RNOW + timedelta(minutes=10)


def started(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    r.ready(RNOW)
    out = r.start(RNOW)
    assert out["ok"], out
    return r, out["run"]


def codes(cum):
    return [x["code"] for x in cum["run"]["readiness"]["reasons"]]


def all_time_blocked(cum):
    return [b for b in cum["promotion"]["blocked_by"] if b.startswith("all-time record NOT READY")]


def test_r14_21_31_in_window_alert_run_not_ready_all_time_not_ready_until_the_resolution_row(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    base = r.fv.cumulative(LATER)
    assert "READ_FAILED" not in codes(base) and "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(base)
    assert base["operational"]["reconciliation_open"] == 0
    write_alert(Path(r.db), STOP, RNOW + timedelta(minutes=1))
    cum = r.fv.cumulative(LATER)
    assert "RUN_WINDOW_RECONCILIATION_OPEN" in codes(cum)
    assert cum["operational"]["reconciliation_open"] == 1
    assert cum["all_time"]["status"] == "NOT READY" and all_time_blocked(cum) and cum["promotion"]["eligible"] is False
    write_resolution(Path(r.db), STOP, RNOW + timedelta(minutes=2), "STOP_VERIFIED_NOT_HIT")
    after = r.fv.cumulative(LATER)
    assert "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(after)
    assert after["operational"]["reconciliation_open"] == 0
    assert after["all_time"]["status"] == base["all_time"]["status"] and all_time_blocked(after) == all_time_blocked(base)
    assert after["promotion"]["blocked_by"] == base["promotion"]["blocked_by"]


def test_r14_31_OD_R5_7b_carry_in_a_pre_window_alert_still_open_makes_the_run_not_ready(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    assert "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(r.fv.cumulative(LATER))
    write_alert(Path(r.db), PROT, RNOW - timedelta(hours=1), exit_cycle=0)              # dated before the run's started_at
    assert iso(RNOW - timedelta(hours=1)) < run["started_at"]
    cum = r.fv.cumulative(LATER)
    reason = next(x for x in cum["run"]["readiness"]["reasons"] if x["code"] == "RUN_WINDOW_RECONCILIATION_OPEN")
    assert reason["detail"] == {"reconciliation_open": 1}
    write_resolution(Path(r.db), PROT, RNOW + timedelta(minutes=1), "PROTECTIVE_FACTS_VERIFIED")
    assert "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(r.fv.cumulative(LATER))     # a resolved pre-window alert is never counted


def test_r14_31_OD_R5_7b_carry_in_counts_only_blocking_alert_kinds_still_open(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    write_alert(Path(r.db), HELD, RNOW - timedelta(hours=2), exit_cycle=0)              # informational: never carried in
    write_alert(Path(r.db), STOP, RNOW - timedelta(hours=2), pid="pos-9")
    write_resolution(Path(r.db), STOP, RNOW - timedelta(hours=1), "STOP_VERIFIED_NOT_HIT", pid="pos-9")   # resolved before the window
    sql(Path(r.db), "INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, position_id, detail_json) VALUES (?,?,?,?,?,?,1,?,?)",
        (uuid.uuid4().hex, None, iso(RNOW - timedelta(hours=3)), "CYCLE", "QTY_MISMATCH", "SNDK", "pos-8", "{}"))   # an old non-lifecycle finding: today's rule (not carried)
    assert "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(r.fv.cumulative(LATER))
    write_alert(Path(r.db), STOP, RNOW - timedelta(minutes=30), pid="pos-9", expected="RESOLVED")   # RECURRENT before the window, still open
    cum = r.fv.cumulative(LATER)
    assert next(x for x in cum["run"]["readiness"]["reasons"] if x["code"] == "RUN_WINDOW_RECONCILIATION_OPEN")["detail"] == {"reconciliation_open": 1}


def test_c1d_l4_carry_in_is_judged_at_the_window_s_end_a_resolution_or_close_written_after_it_does_not_count(tmp_path, monkeypatch):
    """c1d-fix2 L-4 (B2 review LOW L-4; r14:1370, OD-R5-7(b)): a pre-window alert counts when it is still OPEN AT THE WINDOW'S END (t_stop, here
    the read instant LATER), not at query time. A resolution row (PROT) or a CLOSED event (STOP) dated after the window's end does not count;
    the same rows inside the window do. Every other caller of _resolution keeps today's behaviour (judged now)."""
    r, run = started(tmp_path, monkeypatch)
    db = Path(r.db)
    write_alert(db, PROT, RNOW - timedelta(hours=1), exit_cycle=0)
    write_alert(db, STOP, RNOW - timedelta(hours=1), pid="pos-2")
    write_resolution(db, PROT, LATER + timedelta(hours=1), "PROTECTIVE_FACTS_VERIFIED")          # written after the window's end
    sql(db, "INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "pos-2", iso(LATER + timedelta(hours=1)), "CLOSED", "EXIT_SUBMITTED", "CLOSED", "test", "{}"))
    cum = r.fv.cumulative(LATER)
    assert next(x for x in cum["run"]["readiness"]["reasons"] if x["code"] == "RUN_WINDOW_RECONCILIATION_OPEN")["detail"] == {"reconciliation_open": 2}
    rows = {(x["kind"], x["position_id"]): x for x in rec_rows(db)}
    prot, stop = rows[(PROT, PID)], rows[(STOP, "pos-2")]
    assert r.fv._resolution(prot) == r.fv._resolution(stop) == "RESOLVED"                       # the default: judged now (today's behaviour)
    assert r.fv._resolution(prot, until=iso(LATER)) == r.fv._resolution(stop, until=iso(LATER)) == "OPEN"
    assert r.fv._resolution(prot, until=iso(LATER + timedelta(hours=2))) == r.fv._resolution(stop, until=iso(LATER + timedelta(hours=2))) == "RESOLVED"
    assert r.fv.reconciliation(None)["open"] == 0                                                # another user of _resolution: unchanged
    assert "RUN_WINDOW_RECONCILIATION_OPEN" not in codes(r.fv.cumulative(LATER + timedelta(hours=2)))   # once the window's end passes both rows
