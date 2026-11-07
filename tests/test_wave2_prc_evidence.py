"""
Wave 2 PR C - membership, the strategy day, run readiness and the statistics (WAVE2_PRC_DESIGN_REV4.md Revision 4.3 sections 5-8;
WAVE2_PRC_TEST_PLAN_REV4.md sections 3.2-3.5: T-M, T-SD, T-S22R, T-S27R, T-RR, T-ST). Synthetic data only: tmp_path databases, PR B's
stub-stream Lab, explicit clocks; T-S27R classifies its days with the real Stage 6.1 classifier and the real 6.10A health computation.
"""
import inspect
import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import wave2_prb_support as B
import wave2_prc_support as C
from wave2_prc_support import et, iso

START = et(2026, 11, 10, 9, 0)                                                   # Tuesday 09:00 ET, before the open: O(R) starts with 2026-11-10
MID = et(2026, 11, 9, 11, 0)                                                     # Monday 11:00 ET: a partial start day
SMALL = {"min_trades": 1, "target_sessions": 1, "target_events": 1}


def rig(tmp_path, monkeypatch, at=START, **opts):
    r = C.RunLab(tmp_path, monkeypatch)
    r.ready(at)
    out = r.start(at, **opts)
    assert out["ok"], out
    return r, out["run"]


def mdays(first, n):
    from forward_capture import health as FH
    out, d = [], date.fromisoformat(first)
    while len(out) < n:
        if FH.window(d.isoformat()) is not None:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def at(d, hh, mm=0, ss=0):
    y, m, dd = map(int, d.split("-"))
    return et(y, m, dd, hh, mm, ss)


def evening(d):
    return at(d, 17, 0)


def win(r, d, hh=10, minutes=30, px="11.00"):
    """A verified winner inside day d with fresh marks throughout (the drawdown stays known)."""
    a, b = at(d, hh), at(d, hh) + timedelta(minutes=minutes)
    return C.trade(r.db, entry_at=a, exit_at=b, exit_px=px, marks=C.fresh_marks(a, b))


def exclusions(b):
    return {k: v for k, v in b["trades"]["exclusions"].items() if v}


def row(b, pid):
    return next(t for t in b["trades"]["rows"] if t["position_id"] == pid)


def code_names(fn):
    """Every name, attribute and string constant of a function's code (its docstring excluded)."""
    import ast
    import textwrap
    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    body = node.body[1:] if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) else node.body
    out = set()
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.add(n.value)
    return out


# ================================================================ 3.2 membership (C-16 ... C-27) =============================================

def test_TM01_the_start_day_is_in_O_but_not_a_member_and_a_pre_run_decision_is_excluded(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch, at=MID)
    C.trade(r.db, entry_at=MID + timedelta(minutes=5), exit_at=MID + timedelta(minutes=30), created_at=MID - timedelta(minutes=1))
    C.day_samples(r.db, "2026-11-09", r.owner, run["run_id"], run["frozen_hash"], start=MID + timedelta(minutes=1))
    C.put_session(r.db, "2026-11-09", "HEALTHY")
    C.put_health(r.db, "2026-11-09")
    b = r.fv._run_block(evening("2026-11-09"))
    assert b["sessions"]["overlap_dates"] == ["2026-11-09"] and b["sessions"]["member_dates"] == [] and b["sessions"]["nonmember"] == {"2026-11-09": "S1"}
    assert exclusions(b) == {"decided_pre_run": 1} and b["trades"]["members"] == 0


def test_TM02_pre_run_sessions_and_winning_trades_never_count(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    days = [d for d in mdays("2026-09-01", 60) if d < "2026-11-10"][-30:]
    for d in days:
        C.put_session(r.db, d, "HEALTHY")
        C.put_health(r.db, d)
        win(r, d)
    r.ready(START)
    run = r.start(START)["run"]
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    b = r.fv._run_block(evening("2026-11-10"))
    assert (b["sessions"]["members"], b["sessions"]["strategy_days"], b["trades"]["members"], b["strategy"]["closed_trades"], b["strategy"]["win_count"]) == (1, 1, 0, 0, 0)
    assert exclusions(b) == {"decided_pre_run": 30}


def test_TM03_a_two_cohort_record_is_not_a_member_and_a_record_not_final_is_pending(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, cohorts=(C.COHORT, "FP-other0000001"))
    C.member_day(r, d2, run, health_final=False)
    b = r.fv._run_block(evening(d2))
    assert b["sessions"]["nonmember"] == {d1: "S4"} and b["sessions"]["pending_dates"] == [d2] and b["sessions"]["member_dates"] == []


def test_TM04_a_missing_run_id_column_is_membership_unreadable_never_all_members(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    assert r.fv._run_block(evening("2026-11-10"))["sessions"]["members"] == 1
    C.sql(r.db, "ALTER TABLE forward_validation_samples DROP COLUMN run_id")
    b = r.fv._run_block(evening("2026-11-10"))
    assert (b["strategy"]["edge_status"], b["strategy"]["reasons"], b["readiness"]["status"], b["sessions"]["members"], b["sessions"]["strategy_days"]) == \
        ("INSUFFICIENT_DATA", ["membership unreadable"], "NOT READY", 0, 0)


def test_TM05_a_zero_sample_exit_day_keeps_the_trade_as_an_unverified_member(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch, **SMALL)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.member_day(r, d2, run, samples=False, status="NOT_RUN", notes="NO_OBSERVATION", health="INVALID", health_reasons=("FV_SESSION_NOT_RUN",))
    w = win(r, d1)
    t = C.trade(r.db, entry_at=at(d1, 14), exit_at=at(d2, 11), marks=C.fresh_marks(at(d1, 14), at(d1, 16)))
    b = r.fv._run_block(evening(d2))
    assert row(b, t["position_id"])["status"] == "member" and row(b, t["position_id"])["exclusion"] is None
    assert "zero_sample_day" in b["trades"]["unverified_detail"][t["position_id"]] and w["position_id"] not in b["trades"]["unverified_detail"]
    assert (b["trades"]["members"], b["trades"]["verified"], b["trades"]["unverified"]) == (2, 1, 1)
    assert b["strategy"]["edge_status"] == "INSUFFICIENT_DATA" and b["strategy"]["closed_trades"] == 2      # never EARLY_POSITIVE; the cap and the unknown drawdown both block


def test_TM06_a_middle_hold_day_with_zero_samples_is_an_unverified_member_and_an_unknown_drawdown(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2, d3 = mdays("2026-11-10", 3)
    C.member_day(r, d1, run)
    C.member_day(r, d2, run, samples=False, status="NOT_RUN", notes="NO_OBSERVATION", health="INVALID", health_reasons=("FV_SESSION_NOT_RUN",))
    C.member_day(r, d3, run)
    t = C.trade(r.db, entry_at=at(d1, 14), exit_at=at(d3, 11), marks=C.fresh_marks(at(d1, 14), at(d1, 16)) + C.fresh_marks(at(d3, 9, 30), at(d3, 11)))
    b = r.fv._run_block(evening(d3))
    assert row(b, t["position_id"])["status"] == "member" and "zero_sample_day" in b["trades"]["unverified_detail"][t["position_id"]]
    assert b["drawdown"]["status"] == "UNKNOWN" and b["drawdown"]["max_drawdown_pct"] is None


@pytest.mark.parametrize("stamp", ["UNREADABLE", ""])
def test_TM07_an_unreadable_or_empty_run_stamp_is_data_quality_never_other_code(tmp_path, monkeypatch, stamp):
    r, run = rig(tmp_path, monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run)
    B.bulk_samples(r.db, [C.stamped(at(d1, 12, 0, 30), r.owner, stamp, run["frozen_hash"])])
    t = win(r, d1, hh=11, minutes=120)
    b = r.fv._run_block(evening(d1))
    assert b["sessions"]["nonmember"] == {d1: "S3"}
    assert row(b, t["position_id"])["status"] == "member" and row(b, t["position_id"])["exclusion"] is None
    assert "unreadable_or_empty_run_stamp" in b["trades"]["unverified_detail"][t["position_id"]] and not exclusions(b)


def test_TM08_a_hold_day_not_final_in_610a_is_pending_then_an_unverified_member(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run)
    C.member_day(r, d2, run, health_final=False)
    t = C.trade(r.db, entry_at=at(d1, 14), exit_at=at(d2, 11), marks=C.fresh_marks(at(d1, 14), at(d1, 16)) + C.fresh_marks(at(d2, 9, 30), at(d2, 11)))
    b = r.fv._run_block(evening(d2))
    assert row(b, t["position_id"])["status"] == "pending" and b["trades"]["pending"] == 1 and b["trades"]["members"] == 0
    C.sql(r.db, "UPDATE forward_capture_session_health SET final = 1, cohort_ids_json = ? WHERE trading_date = ?", (json.dumps([C.COHORT, "FP-other0000001"]), d2))
    b = r.fv._run_block(evening(d2))
    assert row(b, t["position_id"])["status"] == "member" and "hold_day_not_member" in b["trades"]["unverified_detail"][t["position_id"]]


def test_TM09_a_rollback_day_inside_a_hold_is_other_code_and_suspends_foreign_code(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2, d3 = mdays("2026-11-10", 3)
    C.member_day(r, d1, run)
    C.day_samples(r.db, d2, "5d6f:rollback", None, None)                          # 5d6f7dd code writes NULL stamps
    C.put_session(r.db, d2, "HEALTHY", classifier_version="session_classifier_v1")
    C.put_health(r.db, d2)
    C.member_day(r, d3, run)
    t = C.trade(r.db, entry_at=at(d1, 14), exit_at=at(d3, 11), marks=C.fresh_marks(at(d1, 14), at(d1, 16)) + C.fresh_marks(at(d2, 9, 30), at(d3, 11)))
    rb = C.trade(r.db, entry_at=at(d2, 11), exit_at=at(d2, 11, 30), snapshot_cohort="FP-rollback00001")   # the old code's own trade carries its cohort too (T-B18)
    b = r.fv._run_block(evening(d3))
    assert row(b, t["position_id"])["exclusion"] == "other_code" and t["position_id"] not in b["trades"]["unverified_detail"]
    assert row(b, rb["position_id"])["exclusion"] == "other_code"                     # T6' before T2: foreign code is the root cause
    out = r.fv._run_check(evening(d3))
    s = [e for e in r.store.run_events(run["run_id"]) if e["kind"] == "SUSPENDED"]
    assert out["result"] == "SUSPENDED" and len(s) == 1 and s[0]["code"] == "FOREIGN_CODE" and s[0]["detail"]["first_evidence_at"] == iso(at(d2, 9, 30))
    b = r.fv._run_block(evening(d3))
    assert row(b, t["position_id"])["exclusion"] == "other_code" and b["t_stop"] == iso(at(d2, 9, 30))


def test_TM10_recovered_other_cohort_and_straddling(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run)
    rec = win(r, d1, hh=10)
    C.put_decision(r.db, rec["execution_id"], at(d1, 10), recovered=1)
    oth = C.trade(r.db, entry_at=at(d1, 11), exit_at=at(d1, 11, 30), snapshot_cohort="FP-other0000001", marks=C.fresh_marks(at(d1, 11), at(d1, 11, 30)))
    strad = C.trade(r.db, entry_at=at(d1, 15), exit_at=None, marks=C.fresh_marks(at(d1, 15), at(d1, 16)))
    b = r.fv._run_block(evening(d1))
    assert row(b, rec["position_id"])["status"] == "member" and "recovered" in b["trades"]["unverified_detail"][rec["position_id"]]
    assert row(b, oth["position_id"])["exclusion"] == "other_cohort"
    assert row(b, strad["position_id"])["exclusion"] == "straddling" and row(b, strad["position_id"])["provisional"] is True and b["trades"]["provisional"] == 1


def test_TM11_a_pending_loser_past_the_b20_bound_is_counted_unverified(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch, **SMALL)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.day_samples(r.db, d2, r.owner, run["run_id"], run["frozen_hash"], mode="PAPER_AUTO")    # d2 never finalizes (R-B10): no 6.1 row, no 6.10A record
    for h in (10, 11, 12):
        win(r, d1, hh=h)
    loser = C.trade(r.db, entry_at=at(d2, 10), exit_at=at(d2, 10, 30), exit_px="5.00", marks=C.fresh_marks(at(d2, 10), at(d2, 10, 30)))
    b = r.fv._run_block(at(d2, 15))
    assert row(b, loser["position_id"])["status"] == "pending" and b["trades"]["pending_past_bound"] == 0
    b = r.fv._run_block(evening(d2) + timedelta(days=1))
    assert row(b, loser["position_id"])["status"] == "pending" and b["trades"]["pending_past_bound"] == 1
    assert b["strategy"]["edge_status"] != "EARLY_POSITIVE" and "unverified trades" in " ".join(b["strategy"]["reasons"]) and b["strategy"]["side_text"].endswith("unverified 1")


def test_TM12_membership_is_outcome_blind(tmp_path, monkeypatch):
    def build(p, flip):
        r, run = rig(p, monkeypatch)
        d1, d2 = mdays("2026-11-10", 2)
        C.member_day(r, d1, run)
        C.member_day(r, d2, run, samples=False, status="NOT_RUN", notes="NO_OBSERVATION", health="INVALID")
        out = []
        for i, (a, b, kw) in enumerate(((at(d1, 10), at(d1, 10, 30), {}), (at(d1, 11), at(d1, 11, 30), {"snapshot_cohort": "FP-other0000001"}),
                                        (at(d1, 14), at(d2, 11), {}), (at(d1, 15), None, {}))):
            px = ("9.00" if i % 2 == 0 else "12.00") if flip else ("11.00" if i % 2 == 0 else "8.00")
            out.append(C.trade(r.db, entry_at=a, exit_at=b, exit_px=px if b else None, marks=C.fresh_marks(a, b or at(d1, 16)), **kw))
        bl = r.fv._run_block(evening(d2))
        return [(row(bl, t["position_id"])["status"], row(bl, t["position_id"])["exclusion"], tuple(row(bl, t["position_id"])["unverified"])) for t in out], exclusions(bl)
    assert build(tmp_path / "a", False) == build(tmp_path / "b", True)


def test_TM13_session_and_trade_membership_are_reported_apart_with_every_reason_counted(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run)
    C.day_samples(r.db, d2, r.owner, run["run_id"], run["frozen_hash"])
    win(r, d1, hh=10)
    C.trade(r.db, entry_at=at(d1, 11), exit_at=at(d1, 11, 30), snapshot_cohort="FP-other0000001", marks=C.fresh_marks(at(d1, 11), at(d1, 11, 30)))
    C.trade(r.db, entry_at=at(d1, 12), exit_at=at(d1, 12, 30), created_at=START - timedelta(minutes=1), marks=C.fresh_marks(at(d1, 12), at(d1, 12, 30)))
    C.trade(r.db, entry_at=at(d2, 10), exit_at=at(d2, 10, 30), marks=C.fresh_marks(at(d2, 10), at(d2, 10, 30)))
    C.trade(r.db, entry_at=at(d2, 11), exit_at=None, marks=C.fresh_marks(at(d2, 11), at(d2, 15)))
    b = r.fv._run_block(at(d2, 15))
    assert set(b["sessions"]) >= {"member_dates", "pending_dates", "nonmember", "overlap_dates"} and set(b["trades"]) >= {"members", "exclusions", "pending", "provisional"}
    assert set(b["trades"]["exclusions"]) == {"pre_run", "decided_pre_run", "other_cohort", "other_code", "straddling", "pending", "truncated"}
    assert exclusions(b) == {"decided_pre_run": 1, "other_cohort": 1, "straddling": 1, "pending": 1} and b["trades"]["members"] == 1


def test_TM14_a_lost_entry_snapshot_is_unverified_another_snapshot_or_legacy_is_other_cohort(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, health_final=False)
    lost = C.trade(r.db, entry_at=at(d1, 10), exit_at=at(d1, 10, 30), snapshot_cohort=None, marks=C.fresh_marks(at(d1, 10), at(d1, 10, 30)))
    other = C.trade(r.db, entry_at=at(d1, 11), exit_at=at(d1, 11, 30), snapshot_cohort="FP-other0000001", marks=C.fresh_marks(at(d1, 11), at(d1, 11, 30)))
    leg = C.trade(r.db, entry_at=at(d1, 12), exit_at=at(d1, 12, 30), marks=C.fresh_marks(at(d1, 12), at(d1, 12, 30)))
    C.put_decision(r.db, leg["execution_id"], at(d1, 12), legacy=1)
    b = r.fv._run_block(evening(d1))
    assert row(b, lost["position_id"])["status"] == "pending"                        # pending until the entry day's 6.10A record is final
    C.sql(r.db, "UPDATE forward_capture_session_health SET final = 1 WHERE trading_date = ?", (d1,))
    b = r.fv._run_block(evening(d1))
    assert row(b, lost["position_id"])["status"] == "member" and "no_entry_snapshot" in b["trades"]["unverified_detail"][lost["position_id"]]
    assert row(b, other["position_id"])["exclusion"] == "other_cohort" and row(b, leg["position_id"])["exclusion"] == "other_cohort"
    assert b["drawdown"]["status"] == "KNOWN" and lost["position_id"] not in b["drawdown"]["stale_positions"]


# ================================================================ 3.3 the strategy day (C-28, C-29; P-06, P-10) ==============================

def _pa_split(r, run, d, *, until, tail_start):
    """PAPER_AUTO samples from the open to `until` (inclusive, 60 s), then MONITOR_ONLY from `tail_start` to the close."""
    from forward_capture import health as FH
    o, c, _ = FH.window(d)
    B.bulk_samples(r.db, [C.stamped(t, r.owner, run["run_id"], run["frozen_hash"], mode="PAPER_AUTO") for t in B.minutes(o, until)]
                   + [C.stamped(t, r.owner, run["run_id"], run["frozen_hash"]) for t in B.minutes(tail_start, c)])
    C.put_session(r.db, d, "HEALTHY")
    C.put_health(r.db, d)


@pytest.mark.parametrize("tail_s, expected", [(15, (0.75, True)), (12, (0.7499, False))])
def test_TSD01_TSD02_pa_cov_of_exactly_075_counts_and_07499_does_not(tmp_path, monkeypatch, tail_s, expected):
    r, run = rig(tmp_path, monkeypatch)
    d = "2026-11-10"
    _pa_split(r, run, d, until=at(d, 14, 11), tail_start=at(d, 14, 11, tail_s))     # 281 full minutes + tail_s seconds of 375 minutes
    mem = r.fv._session_membership(run, evening(d), evening(d))
    assert (r.fv._pa_cov(d, run, mem), r.fv._strategy_day(d, run, mem)) == expected
    assert r.fv._run_block(evening(d))["sessions"]["strategy_days"] == int(expected[1])


@pytest.mark.parametrize("health, reasons", [("DEGRADED", ("AUTO_DISARM",)), ("INVALID", ("DUPLICATE_INSTANCE",)), ("INVALID", ("CAPTURE_LOST",)),
                                             ("INVALID", ("INCONSISTENT_TIMESTAMPS",)), ("DEGRADED", ("SETUP_EVALUATION_GAPS",)), ("DEGRADED", ("READY_WITHOUT_EXECUTOR_DECISION",))])
def test_TSD03_a_610a_record_that_is_not_healthy_is_never_a_strategy_day(tmp_path, monkeypatch, health, reasons):
    r, run = rig(tmp_path, monkeypatch)
    d = "2026-11-10"
    C.member_day(r, d, run, mode="PAPER_AUTO", health=health, health_reasons=reasons)
    b = r.fv._run_block(evening(d))
    assert b["sessions"]["member_dates"] == [d] and b["sessions"]["strategy_days"] == 0


def test_TSD04_process_downtime_is_never_paper_auto(tmp_path, monkeypatch):
    from forward_capture import health as FH
    r, run = rig(tmp_path, monkeypatch)
    d = "2026-11-10"
    o, c, _ = FH.window(d)
    C.sql(r.db, "INSERT INTO execution_mode_events (event_id, at, from_mode, to_mode, actor, reason) VALUES ('pa1', ?, 'MONITOR_ONLY', 'PAPER_AUTO', 'user', 'arm')", (iso(o),))
    B.bulk_samples(r.db, [C.stamped(t, r.owner, run["run_id"], run["frozen_hash"], mode="PAPER_AUTO") for t in B.minutes(o, at(d, 11)) + B.minutes(at(d, 13), c)])
    C.put_session(r.db, d, "HEALTHY")
    C.put_health(r.db, d)
    mem = r.fv._session_membership(run, evening(d), evening(d))
    assert FH.paper_auto_spans(r.store.query, o, c) == [(o, c)]                    # the persisted mode says Paper Auto all day
    assert r.fv._pa_cov(d, run, mem) < 0.75 and not r.fv._strategy_day(d, run, mem)
    from forward_validation.service import ForwardValidationService as S
    assert "paper_auto_spans" not in code_names(S._pa_cov) | code_names(S._strategy_day)


def test_TSD05_an_early_close_uses_the_calendar_window(tmp_path, monkeypatch):
    from forward_capture import health as FH
    s0 = et(2026, 11, 27, 9, 0)
    r, run = rig(tmp_path, monkeypatch, at=s0)
    d = "2026-11-27"
    o, c, _ = FH.window(d)
    assert c == et(2026, 11, 27, 13, 0)
    _pa_split(r, run, d, until=at(d, 12, 45), tail_start=at(d, 12, 46))
    mem = r.fv._session_membership(run, evening(d), evening(d))
    assert r.fv._pa_cov(d, run, mem) == 1.0 and r.fv._strategy_day(d, run, mem)


def test_TSD06_a_monitor_only_healthy_member_day_is_not_a_strategy_day(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d = "2026-11-10"
    C.member_day(r, d, run)
    b = r.fv._run_block(evening(d))
    assert b["sessions"]["member_dates"] == [d] and b["sessions"]["strategy_days"] == 0 and b["sessions"]["label"].startswith("0 / 30 strategy sessions (1 healthy")


def test_TSD07_one_strategy_day_predicate(tmp_path):
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    hits = []
    for p in sorted(root.glob("**/*.py")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("tests/", ".venv", "venv/", "node_modules/")) or "/site-packages/" in rel:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                if isinstance(n, ast.Compare) and "pa_cov" in ast.unparse(n):
                    hits.append((rel, fn.name))
    assert hits == [("forward_validation/service.py", "_strategy_day")], hits
    src = (root / "forward_validation" / "service.py").read_text(encoding="utf-8")
    assert src.count("self._strategy_day(") == 1                                  # called once, by _run_block; every consumer reads the run block
    for rel in ("forward_capture/view.py", "strategy_registry/service.py", "forward_validation/reports.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "_run_block" in text or 'get("run")' in text, rel
        assert "pa_cov" not in text and "_strategy_day" not in text, rel


def test_TS22R_a_monitor_only_day_is_healthy_but_never_a_strategy_day(tmp_path, monkeypatch):
    from forward_capture.store import ForwardCaptureStore
    from forward_capture.view import evidence
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run)
    C.member_day(r, d2, run, mode="PAPER_AUTO")
    fc = ForwardCaptureStore(r.db)
    fc.put_cohort(C.COHORT, "k" * 64, iso(START - timedelta(days=1)), "2026-11-09", {})
    b = r.fv._run_block(evening(d2))
    assert b["sessions"]["member_dates"] == [d1, d2] and b["sessions"]["strategy_dates"] == [d2]
    assert b["sessions"]["label"] == "1 / 30 strategy sessions (2 healthy · 0 degraded · 0 failed · 0 unreadable · 0 not run)"
    ev = evidence(fc, r.fv, evening(d2))
    mine = next(c for c in ev["cohorts"] if c["cohort_id"] == C.COHORT)
    assert (mine["healthy_sessions"], mine["strategy_days"], mine["forward_run"]) == (2, 1, True)


def test_TS27R_four_explicit_days_give_exact_classes_zero_strategy_days_and_not_ready(tmp_path, monkeypatch):
    from forward_capture import health as FH
    s0 = et(2026, 11, 9, 9, 0)                                                      # Monday 09:00 ET, before the open
    r, run = rig(tmp_path, monkeypatch, at=s0)
    monkeypatch.setattr(r.fv.capture, "tick", lambda now, trigger="TEST": {"result": "SKIPPED"})   # the 6.10A records come from the real health.compute below
    monkeypatch.setattr(r.fv, "_run_check", lambda now: {"result": "OK"})                       # suspension is T-C10...T-C22's; here: the classes
    mon, tue, wed, thu = "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"

    def cover(d, a, b, **over):
        B.bulk_samples(r.db, [C.stamped(t, r.owner, run["run_id"], run["frozen_hash"], **over) for t in B.minutes(a, b)])

    def evals(d):
        import uuid
        o, c, _ = FH.window(d)
        conn = sqlite3.connect(str(r.db))
        conn.executemany("INSERT INTO setup_evaluations (evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, "
                         "reason_text, inputs_json, freshness_json, trigger) VALUES (?, ?, 'SNDK', 'DOWNSIDE_DISLOCATION_RECOVERY', NULL, 'NONE', 'NONE', 'HOLD', 'X', 'x', '{}', '{}', "
                         "'SCHEDULED')", [(uuid.uuid4().hex, iso(t)) for t in B.minutes(o, c, inclusive=False)])
        conn.commit()
        conn.close()
    for d in (mon, tue, thu):
        evals(d)
    cover(mon, at(mon, 9, 30), at(mon, 16))                                         # Mon: MONITOR_ONLY, every source fresh
    C.sql(r.db, "INSERT INTO execution_mode_events (event_id, at, from_mode, to_mode, actor, reason) VALUES ('a1', ?, 'MONITOR_ONLY', 'PAPER_AUTO', 'user', 'arm')", (iso(at(tue, 9, 30)),))
    C.sql(r.db, "INSERT INTO execution_mode_events (event_id, at, from_mode, to_mode, actor, reason) VALUES ('a2', ?, 'PAPER_AUTO', 'MONITOR_ONLY', 'autorun_policy', 'disarm')",
          (iso(at(tue, 15, 30)),))
    cover(tue, at(tue, 9, 30), at(tue, 15, 29), mode="PAPER_AUTO")                 # Tue: armed at 09:30, one AUTO_DISARM at 15:30 (not SESSION_ENDED)
    cover(tue, at(tue, 15, 30), at(tue, 16))
    B.add_disarm(r.db, at(tue, 15, 30), tue, code="SAFETY_BLOCKED")
    for h in (10, 12, 14):                                                          # Wed: the observer is down all day; the executor is alive
        B.add_run(r.db, at(wed, h))
    cover(thu, at(thu, 9, 30), at(thu, 16))                                         # Thu: an RR intent open in the grace samples after the close
    B.bulk_samples(r.db, [C.stamped(at(thu, 16, 0, 30) + timedelta(seconds=60 * i), r.owner, run["run_id"], run["frozen_hash"], rr_intents=1, in_session=False) for i in range(4)])
    fri = "2026-11-13"
    for t in (at(fri, 9, 0), at(fri, 9, 5), at(fri, 9, 10), at(fri, 9, 15)):
        r.tick(t)
    got = {d: (r.session(d)["session_status"], r.session(d)["notes"]) for d in (mon, tue, wed, thu)}
    assert got == {mon: ("HEALTHY", []), tue: ("HEALTHY", []), wed: ("NOT_RUN", ["NO_OBSERVATION"]), thu: ("FAILED", ["UNRESOLVED_RECONCILIATION"])}, got
    assert r.session(wed)["assessment"].get("backfilled_by") is not None
    now = at(fri, 9, 20)
    h = {d: C.health_610a(r, d, now) for d in (mon, tue, wed, thu)}
    assert (h[mon]["status"], h[mon]["reasons"], h[tue]["status"], h[tue]["reasons"]) == ("HEALTHY", [], "DEGRADED", ["AUTO_DISARM"]), {d: (x["status"], x["reasons"]) for d, x in h.items()}
    b = r.fv._run_block(now)
    mem = r.fv._session_membership(run, now, now)
    assert r.fv._pa_cov(tue, run, mem) == pytest.approx(0.96, abs=0.005)
    assert b["sessions"]["strategy_days"] == 0
    assert b["sessions"]["label"] == "0 / 30 strategy sessions (2 healthy · 0 degraded · 1 failed · 0 unreadable · 1 not run)"
    assert b["readiness"]["status"] == "NOT READY" and {"code": "FAILED_DAY", "dates": [thu]} in b["readiness"]["reasons"]


# ================================================================ 3.4 readiness: run and all-time (C-30 ... C-37) ============================

def _clean(r, run, days, mode="PAPER_AUTO"):
    for d in days:
        C.member_day(r, d, run, mode=mode)


def test_TRR01_a_failed_day_that_is_not_a_member_keeps_the_run_not_ready(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2, d3 = mdays("2026-11-10", 3)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.put_session(r.db, d2, "FAILED", notes="UNRESOLVED_RECONCILIATION")           # D-2: a grace-window FAILED end state with no in-window sample
    C.put_health(r.db, d2, "INVALID", reasons=("FV_SESSION_FAILED",))
    C.member_day(r, d3, run, mode="PAPER_AUTO")
    for now in (evening(d2), evening(d3), evening(d3) + timedelta(days=20)):
        b = r.fv._run_block(now)
        assert d2 not in b["sessions"]["member_dates"] and b["readiness"]["status"] == "NOT READY" and {"code": "FAILED_DAY", "dates": [d2]} in b["readiness"]["reasons"]


def test_TRR02_an_escalated_unreadable_backfill_keeps_the_run_not_ready(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.put_session(r.db, d2, "DEGRADED", notes="EVIDENCE_UNREADABLE")
    C.put_health(r.db, d2, "INVALID", reasons=("FV_SESSION_EVIDENCE_UNREADABLE",))
    b = r.fv._run_block(evening(d2))
    assert b["readiness"]["status"] == "NOT READY" and {"code": "UNREADABLE_DAY", "dates": [d2]} in b["readiness"]["reasons"]


def test_TRR03_a_failed_partial_start_day_keeps_the_run_not_ready(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch, at=MID)
    C.put_session(r.db, "2026-11-09", "FAILED", notes="DUPLICATE_BROKER_ORDER")      # an early stop after started_at
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    b = r.fv._run_block(evening("2026-11-10"))
    assert b["sessions"]["member_dates"] == ["2026-11-10"] and b["readiness"]["status"] == "NOT READY"
    assert {"code": "FAILED_DAY", "dates": ["2026-11-09"]} in b["readiness"]["reasons"]


@pytest.mark.parametrize("version", [None, "session_classifier_v1", "session_classifier_v2"])
def test_TRR04_a_day_past_its_bound_without_a_final_row_of_the_run_classifier_is_not_ready(tmp_path, monkeypatch, version):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.day_samples(r.db, d2, r.owner, run["run_id"], run["frozen_hash"], mode="PAPER_AUTO")
    if version is not None:
        C.put_session(r.db, d2, "HEALTHY", classifier_version=version)              # a rollback finalized it with an older classifier
        C.put_health(r.db, d2)
    early = r.fv._run_block(evening(d2))
    assert early["readiness"]["status"] == "READY"                                   # before the B-20 bound (NY midnight + G) it is only pending
    assert (d2 in early["sessions"]["pending_dates"]) if version is None else early["sessions"]["nonmember"].get(d2) == "S2"   # S2: never a member
    b = r.fv._run_block(at(d2, 23, 59) + timedelta(seconds=361 + 60))
    assert b["readiness"]["status"] == "NOT READY" and {"code": "NOT_FINAL_DAY", "dates": [d2]} in b["readiness"]["reasons"]


def test_TRR05_TRR06_the_all_time_block_is_preserved_and_an_acknowledgement_never_overrides_d5(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    C.put_session(r.db, "2026-11-06", "DEGRADED", notes="EVIDENCE_UNREADABLE")
    C.put_health(r.db, "2026-11-06", "INVALID", reasons=("FV_SESSION_EVIDENCE_UNREADABLE",))
    r.ready(START)
    before = r.fv.cumulative(START)
    rows_before = C.rows(r.db, "SELECT * FROM forward_validation_sessions ORDER BY trading_date")
    h_before = C.rows(r.db, "SELECT * FROM forward_capture_session_health ORDER BY trading_date")
    run = r.start(START, acknowledge_invalid_days=["2026-11-06"], reason="reviewed: historic unreadable day")["run"]
    after = r.fv.cumulative(START)
    assert {k: after[k] for k in ("sessions", "operational")} == {k: before[k] for k in ("sessions", "operational")}
    assert after["sessions"]["completed"] == 0 and after["sessions"]["unreadable"] == 1 and after["operational"]["status"] == "NOT READY"
    assert after["all_time"]["status"] == "NOT READY" and after["all_time"]["invalid_days"] == ["2026-11-06"]
    assert C.rows(r.db, "SELECT * FROM forward_validation_sessions ORDER BY trading_date") == rows_before
    assert C.rows(r.db, "SELECT * FROM forward_capture_session_health ORDER BY trading_date") == h_before
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    cum = r.fv.cumulative(evening("2026-11-10"))
    assert cum["run"]["readiness"]["status"] == "READY" and cum["all_time"]["status"] == "NOT READY" and cum["operational"]["status"] == "NOT READY"
    assert cum["strategy"]["edge_status"] == "INSUFFICIENT_DATA" and cum["strategy"]["expectancy_status"]["reasons"][0].startswith("not a forward run")
    assert cum["sessions"]["completed"] == 1 and cum["sessions"]["unreadable"] == 1


def test_TRR07_a_failing_readiness_read_is_not_ready(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    assert r.fv._run_block(evening("2026-11-10"))["readiness"]["status"] == "READY"
    real = r.store.query_strict

    def flaky(q, args=()):
        if "execution_reconciliations" in q:
            raise sqlite3.OperationalError("database is locked")
        return real(q, args)
    monkeypatch.setattr(r.store, "query_strict", flaky)
    b = r.fv._run_block(evening("2026-11-10"))
    assert b["readiness"]["status"] == "NOT READY" and any(x["code"] == "READ_FAILED" for x in b["readiness"]["reasons"])


def test_TRR08_TRR10_a_clean_run_is_ready_and_readiness_never_reads_prior_invalid_days(tmp_path, monkeypatch):
    from forward_validation.service import ForwardValidationService as S
    r = C.RunLab(tmp_path, monkeypatch)
    C.put_session(r.db, "2026-11-06", "FAILED", notes="DUPLICATE_BROKER_ORDER")
    r.ready(START)
    run = r.start(START, acknowledge_invalid_days=["2026-11-06"], reason="reviewed")["run"]
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    b = r.fv._run_block(evening("2026-11-10"))
    assert b["readiness"] == {"status": "READY", "reasons": [], "text": f"Run {run['run_id']}: READY", "window_counts": b["readiness"]["window_counts"]}
    assert not [n for n in code_names(S._run_readiness) if "prior_invalid" in n]


def test_TRR09_a_suspended_run_is_not_ready_and_its_statistics_stop_at_t_stop(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    _clean(r, run, [d1, d2])
    w1 = win(r, d1)
    w2 = win(r, d2)
    r.store.add_run_event(run["run_id"], iso(at(d2, 9, 0)), "SUSPENDED", "COHORT_CHANGED", {"first_evidence_at": iso(at(d2, 9, 0)), "evidence": {}, "also": []})
    b = r.fv._run_block(evening(d2))
    assert b["suspended"] and b["readiness"]["status"] == "NOT READY" and b["t_stop"] == iso(at(d2, 9, 0))
    assert any(x["code"] == "SUSPENDED" for x in b["readiness"]["reasons"]) and b["sessions"]["member_dates"] == [d1]
    assert row(b, w1["position_id"])["status"] == "member" and row(b, w2["position_id"])["exclusion"] == "straddling"


def test_TRR11_acknowledged_attention_never_blocks_but_a_new_element_does(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    for i, boid in enumerate(("b1", "b2")):
        C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
              (boid, "sa-old", "SNDK", "buy", iso(START - timedelta(days=3, seconds=i)), iso(START - timedelta(days=3))))
    r.ready(START)
    run = r.start(START, acknowledge_attention=["DUPLICATE_BROKER_ORDER"])["run"]
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    assert r.fv._run_block(evening(d1))["readiness"]["status"] == "READY"
    for i, boid in enumerate(("b3", "b4")):
        C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
              (boid, "sa-new", "SNDK", "buy", iso(at(d1, 11, 0, i)), iso(at(d1, 11))))
    b = r.fv._run_block(evening(d1))
    codes = {x["code"] for x in b["readiness"]["reasons"]}
    assert b["readiness"]["status"] == "NOT READY" and {"ATTENTION_UNACKNOWLEDGED", "RUN_WINDOW_DUPLICATE_BROKER_ORDERS"} <= codes


@pytest.mark.parametrize("reason, blocks", [("DUPLICATE_INSTANCE", True), ("CAPTURE_LOST", True), ("INCONSISTENT_TIMESTAMPS", True), ("WORKER_COVERAGE_LOW", False)])
def test_TRR12_an_integrity_invalid_610a_record_blocks_readiness(tmp_path, monkeypatch, reason, blocks):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.member_day(r, d2, run, mode="PAPER_AUTO", health="INVALID", health_reasons=(reason,))
    b = r.fv._run_block(evening(d2))
    assert (b["readiness"]["status"] == "NOT READY") is blocks and d2 not in b["sessions"]["strategy_dates"]
    if blocks:
        assert {"code": "EVIDENCE_INTEGRITY_INVALID", "dates": [d2]} in b["readiness"]["reasons"]


# ================================================================ 3.5 statistics, the edge rule and the milestone (C-50 ... C-58) =============

def test_TST01_TST02_TST13ii_the_edge_rule_never_passes_an_unknown_drawdown_or_an_unverified_trade():
    from forward_validation import metrics as M
    base = dict(min_trades=1, min_sessions=1, session_kind="strategy")
    assert M.edge_status(5, 5, "3.00", "60.00", None, **base)["status"] == "INSUFFICIENT_DATA"
    assert M.edge_status(5, 5, "3.00", "60.00", None, **base)["reasons"][0].startswith("drawdown unknown")
    assert M.edge_status(5, 5, "-3.00", "20.00", None, **base)["status"] == "EARLY_NEGATIVE"
    assert M.edge_status(5, 5, "3.00", "60.00", "2.00", **base)["status"] == "EARLY_POSITIVE"
    assert M.edge_status(5, 5, "3.00", "60.00", "2.00", unverified=1, **base)["status"] == "INSUFFICIENT_DATA"
    assert M.edge_status(5, 5, "3.00", "60.00", "2.00", unverified=1, **base)["reasons"][0].startswith("unverified trades")
    assert M.edge_status(5, 5, "3.00", "60.00", "0.00", **base)["status"] == "EARLY_POSITIVE"   # a KNOWN 0 is a value; UNKNOWN is None (C-57)


def test_TST03_a_truncated_statistic_is_insufficient(tmp_path, monkeypatch):
    import forward_validation.service as S
    r, run = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "ROW_CAP", 1000)
    conn = sqlite3.connect(str(r.db))
    conn.executemany("INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, exit_qty, average_exit_price, "
                     "realized_pnl, closed_at, updated_at) VALUES (?,?,?,0,1,'10.00',?,'CLOSED',1,'11.00','1.00',?,?)",
                     [(f"p{i:05d}", f"x{i:05d}", "SNDK", iso(START - timedelta(days=1, seconds=i)), iso(START - timedelta(days=1)), iso(START - timedelta(days=1))) for i in range(5001)])
    conn.commit()
    conn.close()
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    b = r.fv._run_block(evening("2026-11-10"))
    assert b["strategy"]["truncated"] is True and b["strategy"]["edge_status"] == "INSUFFICIENT_DATA" and b["strategy"]["reasons"][0].startswith("statistics truncated")
    assert b["trades"]["exclusions"]["truncated"] == 1


def test_TST04_the_run_row_minimums_win_over_a_later_environment_change(tmp_path, monkeypatch):
    import forward_validation as FV
    import forward_validation.service as S
    r, run = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(FV, "MIN_TRADES_FOR_EDGE", 1)
    monkeypatch.setattr(S, "TARGET_SESSIONS", 1, raising=False)
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    win(r, "2026-11-10")
    b = r.fv._run_block(evening("2026-11-10"))
    assert "1 closed trade(s) of the 30 needed" in b["strategy"]["reasons"] and "1 strategy session(s) of the 30 needed" in b["strategy"]["reasons"]
    assert b["sessions"]["target"] == 30 and b["sessions"]["label"].startswith("1 / 30 ")


def test_TST05_degraded_member_sessions_never_meet_the_target(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch, min_trades=1, target_sessions=3, target_events=1)
    days = mdays("2026-11-10", 3)
    for d in days:
        C.member_day(r, d, run, mode="PAPER_AUTO", status="DEGRADED", notes="WORKER_COVERAGE_LOW")
    b = r.fv._run_block(evening(days[-1]))
    assert b["sessions"]["members"] == 3 and b["sessions"]["strategy_days"] == 0 and b["sessions"]["target_met"] is False and b["headline"] != "Validation target reached"


def test_TST06_the_label_counts_unreadable_apart_and_never_as_completed(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d = mdays("2026-11-10", 5)
    C.member_day(r, d[0], run, mode="PAPER_AUTO")
    C.member_day(r, d[1], run, mode="PAPER_AUTO")
    C.member_day(r, d[2], run, mode="PAPER_AUTO", status="DEGRADED", notes="WORKER_COVERAGE_LOW", health="DEGRADED", health_reasons=("FV_SESSION_DEGRADED",))
    C.member_day(r, d[3], run, mode="PAPER_AUTO", status="DEGRADED", notes="EVIDENCE_UNREADABLE", health="INVALID", health_reasons=("FV_SESSION_EVIDENCE_UNREADABLE",))
    C.member_day(r, d[4], run, samples=False, status="NOT_RUN", notes="NO_OBSERVATION", health="INVALID", health_reasons=("FV_SESSION_NOT_RUN",))
    b = r.fv._run_block(evening(d[4]))
    assert b["sessions"]["label"] == "2 / 30 strategy sessions (2 healthy · 1 degraded · 0 failed · 1 unreadable · 1 not run)"
    assert d[3] not in b["sessions"]["strategy_dates"] and {"code": "UNREADABLE_DAY", "dates": [d[3]]} in b["readiness"]["reasons"]


def test_TST07_pending_provisional_and_unverified_are_shown_beside_the_label(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.day_samples(r.db, d2, r.owner, run["run_id"], run["frozen_hash"], end=at(d2, 15), mode="PAPER_AUTO")
    win(r, d1)
    C.trade(r.db, entry_at=at(d2, 10), exit_at=at(d2, 10, 30), marks=C.fresh_marks(at(d2, 10), at(d2, 10, 30)))      # pending: d2 is not final
    C.trade(r.db, entry_at=at(d2, 11), exit_at=None, marks=C.fresh_marks(at(d2, 11), at(d2, 15)))                   # provisional
    b = r.fv._run_block(at(d2, 15))
    assert b["strategy"]["side_text"] == "pending 1 · provisional 1 · unverified 0"


def test_TST08_only_scheduled_ready_transitions_inside_the_run_window_count(tmp_path, monkeypatch):
    import uuid
    r, run = rig(tmp_path, monkeypatch)
    sid = uuid.uuid4().hex
    C.sql(r.db, "INSERT INTO setup_episodes (setup_id, symbol, setup_type, cycle_id, state, started_at, last_transition_at, last_evaluated_at, transition_reason, last_inputs_json, "
                "last_freshness_json, updated_at) VALUES (?, 'SNDK', 'DOWNSIDE_DISLOCATION_RECOVERY', 1, 'READY', ?, ?, ?, 'x', '{}', '{}', ?)", (sid,) + (iso(START - timedelta(days=2)),) * 4)
    for i, (when, trig) in enumerate(((START - timedelta(days=1), "SCHEDULED"), (at("2026-11-10", 10), "MANUAL"), (at("2026-11-10", 10, 5), "TEST"), (at("2026-11-10", 10, 10), "SCHEDULED"))):
        C.sql(r.db, "INSERT INTO setup_transitions (transition_id, setup_id, symbol, setup_type, from_state, to_state, reason_code, reason_text, evaluated_at, inputs_json, "
                    "freshness_json, trigger, created_at) VALUES (?, ?, 'SNDK', 'DOWNSIDE_DISLOCATION_RECOVERY', 'WATCH', 'READY', 'x', 'x', ?, '{}', '{}', ?, ?)",
              (uuid.uuid4().hex, sid, iso(when), trig, iso(when)))
    b = r.fv._run_block(evening("2026-11-10"))
    assert b["events"]["ready_transitions"] == 1 and b["events"]["target"] == 100


def _facts(r, now):
    from strategy_registry import registry as R
    from strategy_registry.service import derive_lifecycle, runtime_facts
    r.fv.clock = lambda: now
    facts = runtime_facts(lambda: SimpleNamespace(execution=r.x, forward_validation=r.fv))
    return derive_lifecycle(R.get("downside_dislocation_recovery"), facts), facts


def test_TST09_paper_active_needs_an_active_ready_unsuspended_run_with_a_strategy_day(tmp_path, monkeypatch):
    r, run = rig(tmp_path / "a", monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    lc, facts = _facts(r, evening(d1))
    assert lc["status"] == "PAPER_ACTIVE", (lc, facts)
    assert lc["promotion_eligible"] is False and "promotion blocked" in lc["promotion"]                 # PAPER_ACTIVE is not promotion eligibility
    r, run = rig(tmp_path / "b", monkeypatch)
    C.member_day(r, d1, run)                                                        # MONITOR_ONLY member days only (D-W2-c)
    lc, _ = _facts(r, evening(d1))
    assert lc["status"] == "PAPER_CANDIDATE" and "no strategy day yet" in lc["basis"][0]
    r = C.RunLab(tmp_path / "c", monkeypatch)                                       # Paper Auto ON with no run (the :62-63 branch removed)
    r.x.store.set_mode("PAPER_AUTO", START, "user", "arm", {})
    lc, _ = _facts(r, evening(d1))
    assert lc["status"] == "PAPER_CANDIDATE" and "no active forward run" in lc["basis"][0]
    r, run = rig(tmp_path / "d", monkeypatch)                                       # suspended, with a strategy day
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    r.store.add_run_event(run["run_id"], iso(evening(d1)), "SUSPENDED", "CONFIG_CHANGED", {"first_evidence_at": iso(evening(d1)), "evidence": {}, "also": []})
    lc, _ = _facts(r, evening(d1) + timedelta(minutes=5))
    assert lc["status"] == "PAPER_CANDIDATE" and "suspended" in lc["basis"][0]
    r, run = rig(tmp_path / "e", monkeypatch)                                       # NOT READY (a FAILED day), with a strategy day
    d2 = mdays(d1, 2)[1]
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.put_session(r.db, d2, "FAILED", notes="UNRESOLVED_RECONCILIATION")
    lc, _ = _facts(r, evening(d2))
    assert lc["status"] == "PAPER_CANDIDATE" and "NOT READY" in lc["basis"][0]


def test_TST10_the_610a_run_cohort_label_and_the_61_run_label_agree(tmp_path, monkeypatch):
    from forward_capture.store import ForwardCaptureStore
    from forward_capture.view import evidence
    r, run = rig(tmp_path, monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    win(r, d1)
    fc = ForwardCaptureStore(r.db)
    fc.put_cohort(C.COHORT, "k" * 64, iso(START - timedelta(days=1)), "2026-11-09", {})
    b = r.fv._run_block(evening(d1))
    mine = next(c for c in evidence(fc, r.fv, evening(d1))["cohorts"] if c["cohort_id"] == C.COHORT)
    assert (mine["status"], mine["reasons"], mine["closed_trades"], mine["expectancy"], mine["win_rate"]) == \
        (b["strategy"]["edge_status"], b["strategy"]["reasons"], b["strategy"]["closed_trades"], b["strategy"]["expectancy"], b["strategy"]["win_rate"])


def test_TST12_thirty_monitor_only_days_and_thirty_winners_are_not_the_minimum(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    days = mdays("2026-11-10", 30)
    for d in days:
        C.member_day(r, d, run)
        win(r, d)
    b = r.fv._run_block(evening(days[-1]))
    assert (b["sessions"]["members"], b["sessions"]["strategy_days"], b["sessions"]["target_met"], b["trades"]["verified"]) == (30, 0, False, 30)
    assert b["strategy"]["edge_status"] == "INSUFFICIENT_DATA" and b["strategy"]["reasons"] == ["0 strategy session(s) of the 30 needed"]
    r2, run2 = rig(tmp_path / "pa", monkeypatch)
    for d in days:
        C.member_day(r2, d, run2, mode="PAPER_AUTO")
        win(r2, d)
    b = r2.fv._run_block(evening(days[-1]))
    assert (b["sessions"]["strategy_days"], b["sessions"]["target_met"], b["strategy"]["edge_status"]) == (30, True, "EARLY_POSITIVE"), b["strategy"]


def test_TST13_an_unknown_bot_drawdown_is_never_early_positive_whatever_the_account_curve_says(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch, **SMALL)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.trade(r.db, entry_at=at(d1, 10), exit_at=at(d1, 10, 30))                    # no marks: the price age passes G
    for i, eq in enumerate(("100000.00", "98000.00", "99000.00")):
        B.bulk_samples(r.db, [C.stamped(at(d1, 16, 30) + timedelta(minutes=i), r.owner, run["run_id"], run["frozen_hash"], equity=eq, in_session=False)])
    b = r.fv._run_block(evening(d1))
    assert b["drawdown"]["status"] == "UNKNOWN" and b["strategy"]["edge_status"] == "INSUFFICIENT_DATA"
    seen = {}
    import forward_validation.metrics as M
    real = M.edge_status

    def spy(*a, **k):
        seen["dd"] = a[4] if len(a) > 4 else k.get("max_drawdown_pct")
        return real(*a, **k)
    monkeypatch.setattr(M, "edge_status", spy)
    r.fv._run_block(evening(d1))
    assert seen["dd"] is None                                                       # UNKNOWN is passed as None, never 0, never the account curve
    from forward_validation.service import ForwardValidationService as S
    src = inspect.getsource(S._run_block)
    assert "equity_series" not in src and "M.drawdown(" not in src


def test_TST14_promotion_is_blocked_by_the_all_time_record_a_new_run_or_cohort_never_clears_it(tmp_path, monkeypatch):
    def build(p, prior_invalid):
        r = C.RunLab(p, monkeypatch)
        if prior_invalid:
            C.put_session(r.db, "2026-11-06", "FAILED", notes="DUPLICATE_BROKER_ORDER")
        r.ready(START)
        opts = dict(SMALL, **({"acknowledge_invalid_days": ["2026-11-06"], "reason": "reviewed"} if prior_invalid else {}))
        run = r.start(START, **opts)["run"]
        C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
        win(r, "2026-11-10")
        return r, run
    r, run = build(tmp_path / "a", True)
    ov = r.fv.overview(evening("2026-11-10"))
    assert ov["run"]["readiness"]["status"] == "READY" and ov["run"]["sessions"]["target_met"] and ov["run"]["strategy"]["edge_status"] == "EARLY_POSITIVE"
    assert ov["promotion"]["eligible"] is False and ov["promotion"]["text"].startswith("promotion blocked: all-time record NOT READY (2026-11-06)")
    lc, _ = _facts(r, evening("2026-11-10"))
    assert lc["promotion_eligible"] is False and lc["status"] != "APPROVED" and lc["promotion"].startswith("promotion blocked: all-time record NOT READY")
    r2, _ = build(tmp_path / "b", False)
    assert r2.fv.overview(evening("2026-11-10"))["promotion"]["eligible"] is True                      # (ii) a clean all-time history
    from forward_validation import run_admin
    assert run_admin.end(r.store, evening("2026-11-10"), "new execution version")["ok"]
    import execution as X
    monkeypatch.setattr(X, "VERSION", "execution_v4")
    d3 = "2026-11-11"
    C.put_snapshot(r.db, at(d3, 8, 59), owner=r.owner, cohort_id="FP-run000000002")
    B.bulk_samples(r.db, [C.stamped(at(d3, 8, 59, 30), r.owner, "", r.fh)])
    out = run_admin.start(r.store, at(d3, 9), {"approved_by": "operator", "evidence_ref": "DW2K-1", "accept_blockers": list(C.BLOCKERS), "acknowledge_invalid_days": ["2026-11-06"],
                                               "reason": "reviewed", **SMALL})
    assert out["ok"], out
    C.member_day(r, d3, out["run"], mode="PAPER_AUTO", cohorts=("FP-run000000002",))
    win(r, d3)
    ov = r.fv.overview(evening(d3))
    assert ov["run"]["run_id"] == out["run"]["run_id"] and ov["promotion"]["eligible"] is False and "all-time record NOT READY" in ov["promotion"]["text"]   # (iii)


@pytest.mark.parametrize("case", ["failed_day", "suspended", "ended", "new_attention"])
def test_TST15_I4_a_not_ready_suspended_or_ended_run_never_reads_early_positive_or_target_met(tmp_path, monkeypatch, case):
    from forward_validation import run_admin
    r, run = rig(tmp_path, monkeypatch, **SMALL)
    d1, d2 = mdays("2026-11-10", 2)
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    win(r, d1)
    b = r.fv._run_block(evening(d1))
    assert (b["readiness"]["status"], b["strategy"]["edge_status"], b["sessions"]["target_met"], b["headline"]) == ("READY", "EARLY_POSITIVE", True, "Validation target reached")
    now = evening(d1)
    if case == "failed_day":
        C.put_session(r.db, d2, "FAILED", notes="UNRESOLVED_RECONCILIATION")
        now = evening(d2)
    elif case == "suspended":
        r.store.add_run_event(run["run_id"], iso(now), "SUSPENDED", "CONFIG_CHANGED", {"first_evidence_at": iso(now), "evidence": {}, "also": []})
        now += timedelta(minutes=5)
    elif case == "ended":
        assert run_admin.end(r.store, now, "ended")["ok"]
        now += timedelta(minutes=5)
    else:
        for i, boid in enumerate(("b1", "b2")):
            C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                  (boid, "sa-dup", "SNDK", "buy", iso(at(d1, 15, 0, i)), iso(at(d1, 15))))
    b = r.fv._run_block(now)
    assert b["readiness"]["status"] == "NOT READY" and b["strategy"]["edge_status"] == "INSUFFICIENT_DATA" and b["strategy"]["reasons"][0].startswith("run NOT READY")
    assert b["sessions"]["target_met"] is False and b["headline"] != "Validation target reached" and b["sessions"]["strategy_days"] == 1
    assert r.fv.overview(now)["promotion"]["eligible"] is False


def test_TP11b_an_ended_runs_cohort_is_not_a_forward_run(tmp_path, monkeypatch):
    from forward_capture.store import ForwardCaptureStore
    from forward_capture.view import evidence
    from forward_validation import run_admin
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, "2026-11-10", run, mode="PAPER_AUTO")
    fc = ForwardCaptureStore(r.db)
    fc.put_cohort(C.COHORT, "k" * 64, iso(START - timedelta(days=1)), "2026-11-09", {})
    assert next(c for c in evidence(fc, r.fv, evening("2026-11-10"))["cohorts"] if c["cohort_id"] == C.COHORT)["forward_run"] is True
    assert run_admin.end(r.store, evening("2026-11-10"), "ended")["ok"]
    row = next(c for c in evidence(fc, r.fv, evening("2026-11-10") + timedelta(minutes=1))["cohorts"] if c["cohort_id"] == C.COHORT)
    assert (row["forward_run"], row["reasons"], row["expectancy"]) == (False, ["not a forward run"], None)      # C-80: the ACTIVE run's cohort only


# ================================================================ L-2 (Option B): run readiness and attention agree (C-30 d, C-65) ==========

def _dup_critical(r, now):
    return "DUPLICATE_BROKER_ORDER" in {a["code"] for a in r.fv.attention(now) if a["critical"]}


@pytest.mark.parametrize("case", sorted(C.L2_CASES))
def test_TRR13_L2_only_bot_relevant_multiple_orders_block_run_readiness_as_attention_shows(tmp_path, monkeypatch, case):
    detail, counted = C.L2_CASES[case]
    r, run = rig(tmp_path, monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    assert r.fv._run_block(evening(d1))["readiness"]["status"] == "READY" and not _dup_critical(r, evening(d1))
    C.put_recon(r.db, at(d1, 11), "AMD", detail)
    b = r.fv._run_block(evening(d1))
    codes = {x["code"] for x in b["readiness"]["reasons"]}
    unack = {x["detail"]["attention_code"] for x in b["readiness"]["reasons"] if x["code"] == "ATTENTION_UNACKNOWLEDGED"}
    assert (b["readiness"]["status"] == "NOT READY") is counted, (case, b["readiness"])
    assert ("DUPLICATE_BROKER_ORDER" in unack) is counted and ("RUN_WINDOW_DUPLICATE_BROKER_ORDERS" in codes) is counted, (case, codes)
    assert _dup_critical(r, evening(d1)) is counted                                   # attention, run readiness and its window count agree
    d2 = mdays(d1, 2)[1]
    C.member_day(r, d2, run, mode="PAPER_AUTO")
    later = r.fv._run_block(evening(d2))
    assert (later["readiness"]["status"] == "READY") is (not counted), later["readiness"]   # a manual pair never invalidates the run; a bot one persists
    assert later["sessions"]["strategy_days"] == 2


def test_TRR14_L2_a_mixed_pair_without_an_open_intent_is_still_caught_as_an_unknown_internal_order(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.put_recon(r.db, at(d1, 11), "AMD", '{"count": 2, "client_order_ids": ["manual-1", "stockagent-entry-x"], "bot_orders": 1, "bot_exposed": false}')
    C.put_recon(r.db, at(d1, 11), "AMD", '{"client_order_id": "stockagent-entry-x", "status": "new"}', kind="UNKNOWN_INTERNAL_ORDER")   # as _reconcile writes it
    b = r.fv._run_block(evening(d1))
    assert b["readiness"]["status"] == "NOT READY" and _dup_critical(r, evening(d1))
    assert {"code": "ATTENTION_UNACKNOWLEDGED", "detail": {"attention_code": "DUPLICATE_BROKER_ORDER", "elements": 1}} in b["readiness"]["reasons"]


def test_TRR15_L2_an_unreadable_ownership_read_is_read_failed_never_no_duplicate(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    d1 = "2026-11-10"
    C.member_day(r, d1, run, mode="PAPER_AUTO")
    C.put_recon(r.db, at(d1, 11), "AMD", C.L2_CASES["manual_unrelated"][0])
    assert r.fv._run_block(evening(d1))["readiness"]["status"] == "READY"
    real = r.store.query_strict

    def flaky(q, args=()):
        if "kind = 'MULTIPLE_ORDERS'" in q:
            raise sqlite3.OperationalError("database is locked")
        return real(q, args)
    monkeypatch.setattr(r.store, "query_strict", flaky)
    b = r.fv._run_block(evening(d1))
    assert b["readiness"]["status"] == "NOT READY" and any(x["code"] == "READ_FAILED" for x in b["readiness"]["reasons"])
