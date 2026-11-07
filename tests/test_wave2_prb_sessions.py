"""
Wave 2 PR B - the Stage 6.1 session classifier v2 (WAVE2_PRB_DESIGN_REV3_4.md Rev 3.4.1; WAVE2_PRB_TEST_PLAN_REV3_4.md section 3.1).

Levels (test plan section 1): A = persisted evidence + ForwardValidationService._assess at a stated trigger sample; O = observe() ticks over the
stub-stream Lab (wave2_prb_support), with the day's in-window samples inserted in bulk (time-ordered, own owner) where only the boundary ticks
matter ("hybrid"); Q = SQL; P = pins. G = 360 s, h = 60 s, k = 3. Synthetic data only: tmp_path SQLite, no network, no credential.
"""
import ast
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import wave2_prb_support as S2B  # noqa: E402
from test_cash_flow_602 import cash_config  # noqa: E402,F401 - the autouse config fixture of the shared rig (T-S21 / T-S57 use the 6.1 / 6.10A rigs)
from wave2_prb_support import et, iso  # noqa: E402

UTC = timezone.utc
D = "2026-10-21"                                  # a Wednesday (EDT): W = 13:30Z-20:00Z, C = 20:06Z
O, CL = et(2026, 10, 21, 9, 30), et(2026, 10, 21, 16, 0)
C = CL + timedelta(seconds=360)
SEC = timedelta(seconds=1)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return S2B.Lab(tmp_path, monkeypatch)


def day(lab, start=O, end=CL, step=60, owner=None, inclusive=True, **f):
    return S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, owner or lab.owner, **f) for t in S2B.minutes(start, end, step, inclusive=inclusive)])


def classify(lab, d=D, trig=None, late=False, owner=None):
    trig = trig or (CL + timedelta(seconds=630))                                    # 16:10:30
    S2B.add_refresh(lab.db, trig - timedelta(seconds=5))
    return S2B.assess(lab.fv, d, trig, late=late, owner=owner)


def codes(notes_or_assessment):
    return set(notes_or_assessment)


def post_close(lab, times, **kw):
    return [lab.tick(t, **kw) for t in times]


def pc(d_close=CL):                                                                 # the PC sequence: close + 30 s, +5:30, +10:30, +15:30
    return [d_close + timedelta(seconds=s) for s in (30, 330, 630, 930)]


# ================================================================ coverage, gaps, edges ======================================================
def test_TS01_two_samples_around_the_close_are_degraded_and_the_row_is_opened_and_finalized_when_confirmed(lab):
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 15, 55), lab.owner), S2B.healthy_sample(et(2026, 10, 21, 16, 5), lab.owner)])
    st, notes, a = classify(lab)
    assert st == "DEGRADED" and {"COVERAGE_LOW", "OPEN_NOT_OBSERVED"} <= codes(notes) and a["samples"]["in_window"] == 1


def test_TS01_observer_level(tmp_path, monkeypatch):
    lab = S2B.Lab(tmp_path, monkeypatch)
    lab.tick(et(2026, 10, 21, 15, 55))
    lab.tick(et(2026, 10, 21, 16, 5))
    o = lab.tick(et(2026, 10, 21, 16, 10, 30))
    assert lab.session(D) is None and o["session_status"] is None                  # unconfirmed: the step into 16:05 is 600 s
    o = lab.tick(et(2026, 10, 21, 16, 15, 30))
    row = lab.session(D)
    assert row["session_status"] == "DEGRADED" and {"COVERAGE_LOW", "OPEN_NOT_OBSERVED"} <= set(row["notes"])
    assert row["started_at"] == iso(et(2026, 10, 21, 15, 55)) and row["assessment"]["finalized_by"]["at"] == iso(et(2026, 10, 21, 16, 15, 30))
    assert row["assessment"]["opened_by"]["at"] == iso(et(2026, 10, 21, 16, 15, 30)) and row["classifier_version"] == "session_classifier_v2_1"


def test_TS02_two_short_runs_are_a_long_gap_with_low_coverage(lab):
    day(lab, et(2026, 10, 21, 9, 31), et(2026, 10, 21, 9, 35))
    day(lab, et(2026, 10, 21, 15, 58), CL)
    st, notes, _ = classify(lab)
    assert st == "DEGRADED" and {"LONG_GAP", "COVERAGE_LOW"} <= codes(notes)


def test_TS03_died_at_ten_restarted_after_the_close_is_degraded_and_finalized_on_time(lab):
    day(lab, O, et(2026, 10, 21, 10, 0))
    lab.new_process("7002:bbbbbbbb")
    S2B.add_startup(lab.db, et(2026, 10, 21, 16, 59, 50), 7002, D)
    post_close(lab, [et(2026, 10, 21, 17, 0), et(2026, 10, 21, 17, 5), et(2026, 10, 21, 17, 10)])
    row = lab.session(D)
    assert row["session_status"] == "DEGRADED" and {"COVERAGE_LOW", "CLOSE_NOT_OBSERVED"} <= set(row["notes"]) and row["assessment"]["finalized_late"] is False
    assert row["assessment"]["finalized_by"]["at"] == iso(et(2026, 10, 21, 17, 10))


def test_TS04_a_94_minute_gap_is_exactly_long_gap_and_the_hold_is_not_from_the_environment(lab, monkeypatch):
    day(lab, O, et(2026, 10, 21, 11, 0))
    day(lab, et(2026, 10, 21, 12, 34), CL)
    st, notes, a = classify(lab)
    assert (st, notes, a["measures"]["obs_cov"]) == ("DEGRADED", ["LONG_GAP"], 0.7615)        # 297 / 390
    import config
    lab2 = S2B.Lab(lab.db.parent / "ii", monkeypatch)
    day(lab2, O, et(2026, 10, 21, 12, 0))
    day(lab2, et(2026, 10, 21, 12, 10), CL)                                                 # one 600 s gap
    base = classify(lab2)[2]["measures"]["obs_cov"]
    monkeypatch.setattr(config, "FORWARD_VALIDATION_INTERVAL_SECONDS", 1800.0)
    monkeypatch.setattr(config, "FORWARD_VALIDATION_IDLE_SECONDS", 1800.0)
    st2, notes2, a2 = classify(lab2, trig=CL + timedelta(seconds=700))
    assert notes2 == ["LONG_GAP"] and a2["measures"]["obs_cov"] == base                     # neither G nor h follows the environment


def test_TS05_account_starting_almost_all_day_is_account_coverage_low(lab):
    times = S2B.minutes(O, CL)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner, account_status="FRESH" if i == 0 else "STARTING") for i, t in enumerate(times)])
    assert "ACCOUNT_DATA_COVERAGE_LOW" in classify(lab)[1]


def test_TS06_thanksgiving_with_the_bot_running_has_no_row_and_one_calendar_conflict(lab):
    ticks = S2B.minutes(et(2026, 11, 26, 9, 30), et(2026, 11, 26, 16, 0), 60, inclusive=False)
    for t in ticks:
        lab.tick(t)
    assert lab.session("2026-11-26") is None and len(lab.events("CALENDAR_CONFLICT")) == 1 and lab.store.sample_count() == len(ticks) == 390


def seed_row(lab, d, status="HEALTHY"):
    lab.store.open_session(d, et(2026, 1, 1), "seed", in_session=True)
    if status != "OPEN":
        lab.store.update_session(d, et(2026, 1, 1), session_status=status)


def test_TS07_bot_off_on_thanksgiving_is_never_backfilled(lab):
    seed_row(lab, "2026-11-25")
    for t in (et(2026, 11, 27, 9, 0), et(2026, 11, 27, 9, 5), et(2026, 11, 27, 9, 10)):
        lab.tick(t)
    assert lab.session("2026-11-26") is None


def test_TS08_early_close_late_start_backfills_and_a_day_observed_to_13_is_healthy(lab, tmp_path, monkeypatch):
    seed_row(lab, "2026-11-25")
    lab.tick(et(2026, 11, 27, 13, 30))                                               # after the 13:00 close: opens nothing
    assert lab.session("2026-11-27") is None
    for t in (et(2026, 11, 30, 9, 0), et(2026, 11, 30, 9, 5), et(2026, 11, 30, 9, 10)):
        o = lab.tick(t)
    nr = lab.session("2026-11-27")
    assert o["backfilled"] == ["2026-11-27"] and nr["session_status"] == "NOT_RUN" and nr["assessment"]["backfilled_by"]["at"] == iso(et(2026, 11, 30, 9, 10))
    assert lab.session("2026-11-26") is None
    lab2 = S2B.Lab(tmp_path / "ii", monkeypatch)
    day(lab2, et(2026, 11, 27, 9, 30), et(2026, 11, 27, 13, 0))
    post_close(lab2, pc(et(2026, 11, 27, 13, 0)))
    row = lab2.session("2026-11-27")
    assert row["session_status"] == "HEALTHY" and row["assessment"]["finalized_by"]["at"] == iso(et(2026, 11, 27, 13, 10, 30))
    assert row["assessment"]["window"]["close"] == "2026-11-27T18:00:00+00:00" and row["assessment"]["window"]["cutoff"] == "2026-11-27T18:06:00+00:00"


def test_TS09_a_start_before_the_open_after_a_previous_owner_is_healthy(lab):
    day(lab, et(2026, 10, 20, 9, 30), et(2026, 10, 20, 16, 0), owner="6000:aaaaaaaa")    # the previous owner, the day before
    lab.new_process("8123:bbbbbbbb")
    S2B.add_startup(lab.db, et(2026, 10, 21, 7, 59, 50), 8123, D)
    day(lab, et(2026, 10, 21, 8, 0), CL)
    post_close(lab, pc())
    row = lab.session(D)
    assert row["session_status"] == "HEALTHY", row["notes"]


def test_TS10_a_restart_at_eleven_is_restart_during_session_and_not_a_long_gap(lab):
    day(lab, O, et(2026, 10, 21, 10, 58), owner="6000:aaaaaaaa")
    lab.new_process("8123:bbbbbbbb")
    S2B.add_startup(lab.db, et(2026, 10, 21, 10, 59, 50), 8123, D)
    day(lab, et(2026, 10, 21, 11, 0), CL)
    post_close(lab, pc())
    row = lab.session(D)
    assert "RESTART_DURING_SESSION" in row["notes"] and "LONG_GAP" not in row["notes"] and row["assessment"]["restarts"] == [iso(et(2026, 10, 21, 10, 59, 50))]


def test_TS11_a_full_minute_cadence_day_is_healthy(lab):
    day(lab)
    post_close(lab, pc())
    row = lab.session(D)
    assert (row["session_status"], row["notes_code"]) == ("HEALTHY", "") and all(v >= 0.99 for v in row["assessment"]["measures"].values())
    assert row["assessment"]["finalized_by"]["at"] == iso(CL + timedelta(seconds=630))


def test_TS12_a_late_finalize_reads_no_live_state(lab):
    M = "2026-10-26"
    day(lab, et(2026, 10, 26, 9, 30), et(2026, 10, 26, 16, 0))
    S2B.add_intent(lab.db, et(2026, 10, 27, 8, 0), state="RECONCILIATION_REQUIRED")
    for t in (et(2026, 10, 27, 9, 0), et(2026, 10, 27, 9, 5), et(2026, 10, 27, 9, 10)):
        lab.tick(t)
    row = lab.session(M)
    assert row["session_status"] == "HEALTHY" and row["assessment"]["finalized_late"] is True and row["assessment"]["finalized_by"]["at"] == iso(et(2026, 10, 27, 9, 10))


def test_TS13_an_rr_intent_in_the_last_in_window_sample_fails_the_day_even_when_resolved_later(lab):
    M = "2026-10-26"
    day(lab, et(2026, 10, 26, 9, 30), et(2026, 10, 26, 15, 59))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 26, 16, 0), lab.owner, rr_intents=1)])
    for t in (et(2026, 10, 27, 9, 0), et(2026, 10, 27, 9, 5), et(2026, 10, 27, 9, 10)):
        lab.tick(t)
    row = lab.session(M)
    assert row["session_status"] == "FAILED" and row["notes"] == ["UNRESOLVED_RECONCILIATION"]


# ================================================================ integrity triggers ==========================================================
def test_TS14_TS15_TS16_closed_rows_are_frozen_and_inserts_are_open_or_not_run(lab):
    st = lab.store
    st.insert_not_run("2026-10-19", et(2026, 10, 20), "NO_OBSERVATION")
    seed_row(lab, "2026-10-20", "DEGRADED")
    seed_row(lab, D, "HEALTHY")
    for sql in ("UPDATE forward_validation_sessions SET session_status = 'HEALTHY' WHERE trading_date = '2026-10-19'",
                "INSERT INTO forward_validation_sessions (trading_date, session_status, notes_code, in_session_samples, owners_json, created_at, updated_at) "
                "VALUES ('2026-10-22', 'HEALTHY', '', 0, '[]', 'x', 'x')",
                "UPDATE forward_validation_sessions SET notes_code = 'X' WHERE trading_date = '2026-10-20'",
                f"UPDATE forward_validation_sessions SET assessment_json = '{{}}' WHERE trading_date = '{D}'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn = sqlite3.connect(str(lab.db))
            try:
                conn.execute(sql)
                conn.commit()
            finally:
                conn.close()


def test_TS14b_each_b29_trigger_holds_on_its_own(tmp_path, monkeypatch):
    """B-M10: the three B-29 triggers overlap (a NOT_RUN row is also not OPEN), so each is checked with the other two dropped."""
    names = ("forward_validation_sessions_v2_not_run_final", "forward_validation_sessions_v2_closed_frozen", "forward_validation_sessions_v2_insert_status")
    cases = {names[0]: ("NOT_RUN", "UPDATE forward_validation_sessions SET notes_code = 'X' WHERE trading_date = '2026-10-19'"),
             names[1]: ("DEGRADED", "UPDATE forward_validation_sessions SET notes_code = 'X' WHERE trading_date = '2026-10-19'"),
             names[2]: (None, "INSERT INTO forward_validation_sessions (trading_date, session_status, notes_code, in_session_samples, owners_json, created_at, updated_at) "
                              "VALUES ('2026-10-22', 'HEALTHY', '', 0, '[]', 'x', 'x')")}
    for keep, (status, sql) in cases.items():
        lb = S2B.Lab(tmp_path / keep, monkeypatch)
        if status == "NOT_RUN":
            lb.store.insert_not_run("2026-10-19", et(2026, 10, 20), "NO_OBSERVATION")
        elif status:
            seed_row(lb, "2026-10-19", status)
        for other in names:
            if other != keep:
                lb.sql(f"DROP TRIGGER {other}")
        conn = sqlite3.connect(str(lb.db))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        finally:
            conn.close()


# ================================================================ the clock-skew guard =======================================================
def test_TS17_one_tick_a_week_ahead_opens_nothing_and_degrades_its_dates(lab):
    day(lab, O, et(2026, 10, 21, 13, 59))
    lab.tick(et(2026, 10, 28, 14, 0))                                                # +7 days
    for t in (et(2026, 10, 21, 14, 1), et(2026, 10, 21, 14, 2), et(2026, 10, 21, 14, 3)):
        lab.tick(t)
    day(lab, et(2026, 10, 21, 14, 4), CL)
    post_close(lab, pc())
    assert [r["trading_date"] for r in lab.store.sessions(newest_first=False)] == [D]
    ev = lab.events("CLOCK_INCONSISTENT")
    assert len(ev) == 1 and ev[0]["detail"]["affected_dates"] == [D, "2026-10-28"] and len(ev[0]["detail"]["suspects"]) == 1
    row = lab.session(D)
    assert row["session_status"] == "DEGRADED" and "CLOCK_INCONSISTENT" in row["notes"]


def test_TS18_one_tick_a_day_ahead_degrades_its_anchor_date_and_its_own_date(lab):
    day(lab, O, et(2026, 10, 21, 13, 56))
    for t in (et(2026, 10, 21, 13, 57), et(2026, 10, 21, 13, 58), et(2026, 10, 21, 13, 59)):
        lab.tick(t)                                                                     # a confirmed run of real ticks (D is opened)
    before = {r["trading_date"]: r["session_status"] for r in lab.store.sessions(newest_first=False)}
    o = lab.tick(et(2026, 10, 22, 14, 0))
    after = {r["trading_date"]: r["session_status"] for r in lab.store.sessions(newest_first=False)}
    assert before == after == {D: "OPEN"} and o["backfilled"] == []                    # (a) the skewed tick applies no transition (V-08, B-M13: nothing sticky)
    lab.tick(et(2026, 10, 21, 14, 1))
    assert lab.session("2026-10-22") is None
    day(lab, et(2026, 10, 21, 14, 2), CL)
    post_close(lab, pc())
    day(lab, et(2026, 10, 22, 9, 30), et(2026, 10, 22, 16, 0))
    post_close(lab, pc(et(2026, 10, 22, 16, 0)))
    assert "CLOCK_INCONSISTENT" in lab.session(D)["notes"] and "CLOCK_INCONSISTENT" in lab.session("2026-10-22")["notes"]
    assert lab.events("CLOCK_INCONSISTENT")[0]["detail"]["affected_dates"] == [D, "2026-10-22"]


def test_TS19_a_stalled_observer_thread_is_a_long_gap_with_low_coverage(lab):
    day(lab, O, et(2026, 10, 21, 10, 0))
    day(lab, et(2026, 10, 21, 15, 58), CL)
    post_close(lab, pc())
    assert {"LONG_GAP", "COVERAGE_LOW"} <= set(lab.session(D)["notes"])


def test_TS20_down_monday_to_wednesday_backfills_at_the_third_tick_only(lab):
    seed_row(lab, "2026-10-16")
    outs = [lab.tick(t) for t in (et(2026, 10, 22, 8, 0), et(2026, 10, 22, 8, 5), et(2026, 10, 22, 8, 10))]
    assert outs[0]["backfilled"] == [] and outs[1]["backfilled"] == [] and outs[2]["backfilled"] == ["2026-10-19", "2026-10-20", "2026-10-21"]


# ================================================================ 6.10A AUTO_DISARM (3d) ======================================================
def test_TS21_TS33_the_session_ended_disarm_at_the_close_is_exempt_and_nothing_else_is():
    from forward_capture.health import _auto_disarm_exempt
    ev = lambda at, code="SESSION_ENDED", codes_=None: {"code": code, "at": iso(at), "detail_json": json.dumps({"codes": codes_ or [code]})}  # noqa: E731
    assert _auto_disarm_exempt(ev(CL + timedelta(seconds=30)), CL)
    assert _auto_disarm_exempt(ev(CL - timedelta(seconds=60)), CL) and not _auto_disarm_exempt(ev(CL - timedelta(seconds=61)), CL)
    assert _auto_disarm_exempt(ev(CL + timedelta(seconds=360)), CL) and not _auto_disarm_exempt(ev(CL + timedelta(seconds=361)), CL)
    assert not _auto_disarm_exempt(ev(CL + timedelta(seconds=30), "STALE_EQUITY"), CL)
    assert not _auto_disarm_exempt(ev(CL + timedelta(seconds=30), "SESSION_ENDED", ["SESSION_ENDED", "STALE_EQUITY"]), CL)
    assert not _auto_disarm_exempt(ev(et(2026, 10, 22, 10, 0)), CL)                         # 10:00 the next day (T-S33 i)


def test_TS21_TS33_compute_level_on_the_610a_rig(tmp_path, monkeypatch):
    from test_forward_paper_610a import observed_day, rig as rig610
    from forward_capture import health as HL
    r = rig610(tmp_path, monkeypatch)
    observed_day(r, monkeypatch)
    S2B.migrate(r.db)                                                                  # the 6.10A rig has no autorun tables
    S2B.add_disarm(r.db, CL + timedelta(seconds=30), D)
    rec = HL.compute(r.cap, D, r.clock.now)
    assert "AUTO_DISARM" not in rec["reasons"] and rec["metrics"]["auto_disarm"] == {"events": 1, "exempt_session_ended": 1}
    S2B.add_disarm(r.db, CL + timedelta(seconds=40), D, code="STALE_EQUITY")
    rec = HL.compute(r.cap, D, r.clock.now)
    assert "AUTO_DISARM" in rec["reasons"] and rec["metrics"]["auto_disarm"] == {"events": 2, "exempt_session_ended": 1}


# ================================================================ calendar, worker, DST =====================================================
def test_TS23_an_uncovered_year_is_calendar_not_covered_and_its_backfill_says_so(lab):
    day(lab, et(2028, 1, 17, 9, 30), et(2028, 1, 17, 16, 0))
    st, notes, _ = classify(lab, "2028-01-17", trig=et(2028, 1, 17, 16, 10, 30))
    assert "CALENDAR_NOT_COVERED" in notes
    seed_row(lab, "2028-01-13")
    for t in (et(2028, 1, 19, 9, 0), et(2028, 1, 19, 9, 5), et(2028, 1, 19, 9, 10)):
        lab.tick(t)
    assert lab.session("2028-01-14")["notes_code"] == "NO_OBSERVATION|CALENDAR_NOT_COVERED"
    assert lab.session("2028-01-18")["notes_code"] == "NO_OBSERVATION|CALENDAR_NOT_COVERED"


def test_TS24_a_stale_worker_heartbeat_is_worker_coverage_low(lab):
    times = S2B.minutes(O, CL)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner, last_cycle_at=iso(t - timedelta(seconds=300 if i % 5 < 2 else 30))) for i, t in enumerate(times)])
    assert "WORKER_COVERAGE_LOW" in classify(lab)[1]


def test_TS26_dst_mondays_resolve_their_windows(lab):
    for d, o, c in (("2026-03-09", et(2026, 3, 9, 9, 30), et(2026, 3, 9, 16, 0)), ("2026-11-02", et(2026, 11, 2, 9, 30), et(2026, 11, 2, 16, 0))):
        day(lab, o, c)
        st, notes, a = classify(lab, d, trig=c + timedelta(seconds=630))
        assert st == "HEALTHY", notes
        assert (a["window"]["open"], a["window"]["close"], a["window"]["cutoff"]) == (iso(o), iso(c), iso(c + timedelta(seconds=360)))
    assert iso(et(2026, 3, 9, 9, 30)) == "2026-03-09T13:30:00+00:00" and iso(et(2026, 11, 2, 9, 30)) == "2026-11-02T14:30:00+00:00"


def test_TS28_two_skewed_ticks_confirm_nothing(lab):
    day(lab, O, et(2026, 10, 21, 13, 58))
    lab.tick(et(2026, 10, 28, 14, 0))
    lab.tick(et(2026, 10, 28, 14, 1))
    for t in (et(2026, 10, 21, 14, 2), et(2026, 10, 21, 14, 3), et(2026, 10, 21, 14, 4)):
        lab.tick(t)
    assert [r["trading_date"] for r in lab.store.sessions(newest_first=False)] == [D]


def test_TS29_alternating_freshness_is_sample_and_hold(lab):
    times = S2B.minutes(O, CL)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner, account_status="FRESH" if i % 2 else "STARTING") for i, t in enumerate(times)])
    st, notes, a = classify(lab)
    assert abs(a["measures"]["fresh_cov"] - 0.5) <= 1 / 390 and "ACCOUNT_DATA_COVERAGE_LOW" in notes


@pytest.mark.parametrize("gap,expect", [(360, []), (361, ["LONG_GAP"])])
def test_TS30_interior_gaps_of_g_and_g_plus_one(lab, gap, expect):
    day(lab, O, et(2026, 10, 21, 12, 0))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner) for t in S2B.minutes(et(2026, 10, 21, 12, 0) + timedelta(seconds=gap), CL)])
    assert classify(lab)[1] == expect


@pytest.mark.parametrize("first,last,expect", [(360, 0, set()), (361, 0, {"OPEN_NOT_OBSERVED"}), (0, 360, set()), (0, 361, {"CLOSE_NOT_OBSERVED"})])
def test_TS30_edges(lab, first, last, expect):
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner) for t in S2B.minutes(O + timedelta(seconds=first), CL - timedelta(seconds=last))])
    notes = set(classify(lab)[1])
    assert expect <= notes and not ({"OPEN_NOT_OBSERVED", "CLOSE_NOT_OBSERVED"} - expect) & notes


# ================================================================ restarts ==================================================================
@pytest.mark.parametrize("startup", [True, False])
def test_TS31_a_start_before_the_open_is_not_a_restart_during_the_session(lab, startup):
    day(lab, et(2026, 10, 20, 9, 30), et(2026, 10, 20, 16, 0), owner="6000:aaaaaaaa")
    lab.new_process("8123:bbbbbbbb")
    if startup:
        S2B.add_startup(lab.db, et(2026, 10, 21, 9, 27, 5), 8123, D)
    day(lab, et(2026, 10, 21, 9, 27, 10), CL)
    post_close(lab, pc())
    assert "RESTART_DURING_SESSION" not in lab.session(D)["notes"]


def test_TS32_a_last_sample_at_1556_then_a_late_finalize_is_healthy_with_empty_notes(lab):
    day(lab, O, et(2026, 10, 21, 15, 56))
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    row = lab.session(D)
    assert (row["session_status"], row["notes_code"], row["assessment"]["finalized_late"]) == ("HEALTHY", "", True)


def test_TS34_a_refresh_failure_before_the_open_counts_for_the_day(lab):
    day(lab)
    S2B.add_refresh(lab.db, et(2026, 10, 21, 8, 0), result="FAILED", error_code="HTTP_503")
    assert {"REFRESH_FAILURES", "BROKER_ERRORS"} <= set(classify(lab)[1])


def test_TS35_a_stale_reconciliation_flag_is_not_failed(lab):
    day(lab, O, et(2026, 10, 21, 15, 59))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL, lab.owner, reconciliation_healthy=False)])
    st, notes, _ = classify(lab)
    assert st == "DEGRADED" and "RECONCILIATION_BLOCK_RECOVERED" in notes


def test_TS36_a_date_with_own_in_window_samples_is_never_backfilled(lab):
    seed_row(lab, "2026-10-20")
    day(lab, O, et(2026, 10, 21, 11, 0))                                             # observed, then the process died
    lab.new_process("7002:bbbbbbbb")
    for t in (et(2026, 10, 23, 9, 0), et(2026, 10, 23, 9, 5), et(2026, 10, 23, 9, 10)):
        lab.tick(t)
    assert lab.session(D)["session_status"] == "DEGRADED" and lab.session("2026-10-22")["session_status"] == "NOT_RUN"


def test_TS37_a_broker_error_after_the_cutoff_never_counts(lab):
    day(lab)
    S2B.add_run(lab.db, et(2026, 10, 21, 18, 0), result="ERROR", error="BROKER_TIMEOUT: x")
    on_time = classify(lab)
    late = classify(lab, trig=et(2026, 10, 22, 9, 10), late=True)
    assert on_time[0] == late[0] == "HEALTHY"


# ================================================================ T-S37b: the post-close grace window (U-04, V-03) ==========================
def test_TS37b_i_an_early_stop_at_1600_30_fails_the_day(lab):
    S2B.add_reconciliation(lab.db, et(2026, 10, 16, 11, 0), "MULTIPLE_ORDERS")          # a historical critical element (not in E(d))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner) for t in S2B.minutes(O + timedelta(seconds=30), CL - timedelta(seconds=30))])
    lab.x.store.set_mode("PAPER_AUTO", et(2026, 10, 21, 15, 59, 40), "user", "test arm", {})
    outs = post_close(lab, pc())
    assert outs[0]["early_stop"]["stopped"] is True and lab.events("EARLY_STOP")[0]["at"] == iso(CL + timedelta(seconds=30))
    row = lab.session(D)
    assert row["session_status"] == "FAILED" and row["notes"] == ["EARLY_STOP"] and row["assessment"]["finalized_by"]["at"] == iso(CL + timedelta(seconds=630))


@pytest.mark.parametrize("late", [False, True])
def test_TS37b_ii_post_close_evidence_fails_on_time_and_late(lab, late):
    day(lab)
    S2B.add_event(lab.db, CL + timedelta(seconds=30), "EARLY_STOP", "DUPLICATE_BROKER_ORDER", {"stopped": False}, trading_date=D)
    S2B.add_reconciliation(lab.db, CL + timedelta(seconds=30), "MULTIPLE_ORDERS")
    st, notes, _ = classify(lab, trig=et(2026, 10, 22, 9, 10) if late else None, late=late)
    assert st == "FAILED" and notes == ["DUPLICATE_BROKER_ORDER", "EARLY_STOP"]


@pytest.mark.parametrize("offset,counted", [(0, True), (1, False)])
@pytest.mark.parametrize("late", [False, True])
def test_TS37b_iii_the_cutoff_is_inclusive(lab, offset, counted, late):
    day(lab)
    S2B.add_event(lab.db, C + timedelta(seconds=offset), "EARLY_STOP", "X", {"stopped": False}, trading_date=D)
    st, notes, _ = classify(lab, trig=et(2026, 10, 22, 9, 10) if late else None, late=late)
    assert (st, notes) == (("FAILED", ["EARLY_STOP"]) if counted else ("HEALTHY", [])), (offset, late, notes)


@pytest.mark.parametrize("mm,counted", [(3, True), (7, False)])
def test_TS37b_iv_the_early_close_grace(lab, mm, counted):
    day(lab, et(2026, 11, 27, 9, 30), et(2026, 11, 27, 13, 0))
    S2B.add_event(lab.db, et(2026, 11, 27, 13, mm), "EARLY_STOP", "X", {"stopped": False}, trading_date="2026-11-27")
    st, notes, _ = classify(lab, "2026-11-27", trig=et(2026, 11, 27, 13, 10, 30))
    assert (st, notes) == (("FAILED", ["EARLY_STOP"]) if counted else ("HEALTHY", [])), (mm, notes)


def test_TS37b_v_no_finalize_before_c_plus_h_even_when_confirmed_and_settled(lab):
    day(lab)
    lab.tick(CL + timedelta(seconds=30))
    lab.tick(CL + timedelta(seconds=330))
    lab.x.last_cycle = {"at": iso(et(2026, 10, 21, 16, 6, 10))}
    S2B.add_refresh(lab.db, et(2026, 10, 21, 16, 6, 15))
    lab.tick(et(2026, 10, 21, 16, 6, 30), cycle=False, refresh=False)
    assert lab.session(D)["session_status"] == "OPEN"
    lab.tick(et(2026, 10, 21, 16, 7, 30))
    assert lab.session(D)["session_status"] == "HEALTHY"


def test_TS37b_vi_an_executor_cycle_in_flight_at_the_cutoff_delays_the_finalize(lab):
    day(lab)
    lab.tick(CL + timedelta(seconds=30))
    lab.tick(CL + timedelta(seconds=330))
    lab.x.last_cycle = {"at": iso(et(2026, 10, 21, 16, 5, 20))}
    lab.tick(et(2026, 10, 21, 16, 7, 30), cycle=False)
    assert lab.session(D)["session_status"] == "OPEN"
    S2B.add_reconciliation(lab.db, et(2026, 10, 21, 16, 5, 50), "MULTIPLE_ORDERS")       # the 16:05:50 cycle commits now
    lab.x.last_cycle = {"at": iso(et(2026, 10, 21, 16, 5, 50))}
    lab.tick(et(2026, 10, 21, 16, 8, 30), cycle=False)
    assert lab.session(D)["session_status"] == "OPEN"                                    # that cycle started before C: still not settled
    lab.x.last_cycle = {"at": iso(et(2026, 10, 21, 16, 6, 40))}
    lab.tick(et(2026, 10, 21, 16, 9, 30), cycle=False)
    row = lab.session(D)
    assert row["session_status"] == "FAILED" and row["notes"] == ["DUPLICATE_BROKER_ORDER"]


def test_TS37b_vii_no_account_attempt_after_the_cutoff_delays_the_finalize(lab):
    day(lab)
    lab.tick(CL + timedelta(seconds=30), refresh=False)
    lab.tick(CL + timedelta(seconds=330), refresh=False)
    lab.tick(et(2026, 10, 21, 16, 7, 30), refresh=False)
    assert lab.session(D)["session_status"] == "OPEN"
    S2B.add_refresh(lab.db, et(2026, 10, 21, 16, 8, 0))
    lab.tick(et(2026, 10, 21, 16, 8, 30), refresh=False)
    assert lab.session(D)["session_status"] == "HEALTHY"


@pytest.mark.parametrize("failed", [False, True])
def test_TS37b_viii_a_stalled_executor_finalizes_late_and_never_healthy(lab, failed):
    day(lab)
    lab.x.last_cycle = {"at": iso(et(2026, 10, 21, 15, 59))}
    if failed:
        S2B.add_event(lab.db, et(2026, 10, 21, 12, 0), "EARLY_STOP", "X", {"stopped": False}, trading_date=D)
    for t in S2B.minutes(CL + timedelta(seconds=30), et(2026, 10, 21, 16, 40, 30), 300):
        lab.tick(t, cycle=False)
    assert lab.session(D)["session_status"] == "OPEN"
    for t in (et(2026, 10, 22, 0, 0, 30), et(2026, 10, 22, 0, 5, 30), et(2026, 10, 22, 0, 10, 30)):
        lab.tick(t, cycle=False)
    row = lab.session(D)
    assert row["assessment"]["finalized_late"] is True and "EVIDENCE_NOT_SETTLED" in row["assessment"]["degraded_codes"]
    assert row["session_status"] == ("FAILED" if failed else "DEGRADED")


@pytest.mark.parametrize("late", [False, True])
def test_TS37b_ix_x_an_rr_intent_first_visible_in_a_grace_sample(lab, late):
    day(lab)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL + timedelta(seconds=30), lab.owner, rr_intents=1)])
    st, notes, _ = classify(lab, trig=et(2026, 10, 22, 9, 10) if late else None, late=late)
    assert (st, notes) == ("FAILED", ["UNRESOLVED_RECONCILIATION"])
    lab2 = S2B.Lab(lab.db.parent / f"x{int(late)}", lab.monkeypatch)
    day(lab2)
    S2B.bulk_samples(lab2.db, [S2B.healthy_sample(C + timedelta(seconds=30), lab2.owner, rr_intents=1)])
    assert classify(lab2, trig=et(2026, 10, 22, 9, 10) if late else None, late=late)[0] == "HEALTHY"


@pytest.mark.parametrize("sec,expect", [(210, "FAILED"), (390, "HEALTHY")])
def test_TS37b_xi_early_close_grace_end_state(lab, sec, expect):
    day(lab, et(2026, 11, 27, 9, 30), et(2026, 11, 27, 13, 0))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 11, 27, 13, 0) + timedelta(seconds=sec), lab.owner, safety_state="UNKNOWN")])
    st, notes, _ = classify(lab, "2026-11-27", trig=et(2026, 11, 27, 13, 10, 30))
    assert st == expect and (notes == ["SAFETY_UNKNOWN_AT_END"] if expect == "FAILED" else True)


def test_TS37b_xii_a_null_count_in_a_grace_sample_is_end_state_unknown(lab):
    day(lab)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL + timedelta(seconds=330), lab.owner, failed_intents=None)])
    st, notes, _ = classify(lab)
    assert st == "DEGRADED" and "END_STATE_UNKNOWN" in notes


def test_TS37b_xiii_an_unobserved_open_row_finalized_late_and_unsettled_stays_not_run(lab):
    seed_row(lab, D, "OPEN")
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 9, 0), lab.owner)])
    st, notes, a = S2B.assess(lab.fv, D, et(2026, 10, 22, 9, 10), late=True)           # no account attempt after C: unsettled
    assert (st, notes) == ("NOT_RUN", ["NO_OBSERVATION"]) and a["degraded_codes"] == ["EVIDENCE_NOT_SETTLED"]


# ================================================================ back-fill scope, NULLs, audit trail ======================================
def test_TS38_backfill_scope(lab, tmp_path, monkeypatch):
    seed_row(lab, "2026-10-19")
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 20, 9, 0), lab.owner), S2B.healthy_sample(et(2026, 10, 20, 9, 1), lab.owner)])   # (i) 2 pre-open
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 11, 0), "6000:aaaaaaaa")])                                                     # (ii) another owner
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    assert lab.session("2026-10-20")["session_status"] == "NOT_RUN" and lab.session("2026-10-20")["assessment"]["backfilled_by"]
    assert lab.session(D)["session_status"] == "DEGRADED"                              # observed by another owner: accounted, never NOT_RUN
    lab2 = S2B.Lab(tmp_path / "i2", monkeypatch)
    for t in (et(2026, 10, 20, 9, 0), et(2026, 10, 20, 9, 1), et(2026, 10, 20, 9, 2)):  # (i') 3 pre-open: opened, then finalized NOT_RUN
        lab2.tick(t)
    assert lab2.session("2026-10-20")["session_status"] == "OPEN"
    for t in (et(2026, 10, 21, 9, 0), et(2026, 10, 21, 9, 5), et(2026, 10, 21, 9, 10)):
        lab2.tick(t)
    assert lab2.session("2026-10-20")["session_status"] == "NOT_RUN"


@pytest.mark.parametrize("field", ["rr_intents", "attention_positions", "failed_intents", "safety_state"])
def test_TS39_a_null_in_the_last_sample_is_end_state_unknown(lab, field):
    day(lab, O, et(2026, 10, 21, 15, 59))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL, lab.owner, **{field: None})])
    st, notes, _ = classify(lab)
    assert st == "DEGRADED" and "END_STATE_UNKNOWN" in notes


def test_TS39e_positive_evidence_wins_over_a_null(lab):
    day(lab, O, et(2026, 10, 21, 15, 59))
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL, lab.owner, rr_intents=None, attention_positions=2)])
    st, notes, a = classify(lab)
    assert (st, notes) == ("FAILED", ["UNRESOLVED_RECONCILIATION"]) and "END_STATE_UNKNOWN" in a["degraded_codes"]


def test_TS39b_an_unreadable_executor_table_persists_null_counts_not_zero(lab):
    day(lab, O, et(2026, 10, 21, 15, 59))
    lab.sql("ALTER TABLE execution_intents RENAME TO execution_intents_hidden")
    lab.tick(CL)
    s = lab.store.samples(D)[-1]
    assert (s["rr_intents"], s["attention_positions"], s["failed_intents"]) == (None, None, None) and s["open_positions"] == 0
    lab.sql("ALTER TABLE execution_intents_hidden RENAME TO execution_intents")
    post_close(lab, pc())
    row = lab.session(D)
    assert row["session_status"] == "DEGRADED" and "END_STATE_UNKNOWN" in row["notes"]


def test_TS40_the_backfill_audit_trail_and_one_retroactive_event_per_new_suspect_set(lab):
    seed_row(lab, "2026-10-19")
    for t in (et(2026, 10, 21, 9, 0), et(2026, 10, 21, 9, 1), et(2026, 10, 21, 9, 2)):
        o = lab.tick(t)
    assert o["backfilled"] == ["2026-10-20"] and lab.session("2026-10-20")["assessment"]["backfilled_by"]["at"] == iso(et(2026, 10, 21, 9, 2))
    lab.tick(et(2026, 10, 21, 8, 59))                                                # the clock jumps back: 09:01 and 09:02 are now suspect
    ev = lab.events("CLOCK_INCONSISTENT")
    assert len(ev) == 1 and [s["at"] for s in ev[0]["detail"]["suspects"]] == [iso(et(2026, 10, 21, 9, 1)), iso(et(2026, 10, 21, 9, 2))]
    assert "2026-10-20" in ev[0]["detail"]["rows_triggered"]
    lab.tick(et(2026, 10, 21, 8, 59, 30))
    assert len(lab.events("CLOCK_INCONSISTENT")) == 1                                  # the same suspects again: no second event
    lab.tick(et(2026, 10, 21, 8, 55))
    ev = lab.events("CLOCK_INCONSISTENT")
    second = [e for e in ev if e["detail"]["proven_by"]["at"] == iso(et(2026, 10, 21, 8, 55))]   # events sort by `at` (the suspect clock): pick by the prover
    assert second and second[0]["detail"]["rows_triggered"] == []                       # B-27: the rows N triggered (none), not the known suspects' rows
    assert len(ev) == 2 and len(second) == 1 and {s["at"] for s in second[0]["detail"]["suspects"]} == {iso(et(2026, 10, 21, 9, 0)), iso(et(2026, 10, 21, 8, 59)), iso(et(2026, 10, 21, 8, 59, 30))}


def test_TS41_worker_freshness_uses_the_persisted_threshold_and_no_live_scheduler(lab, monkeypatch):
    times = S2B.minutes(O, CL)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, lab.owner, last_cycle_at=iso(t - timedelta(seconds=200)), worker_stale_after_s=270.0) for t in times])
    import execution.scheduler as ES
    monkeypatch.setattr(ES, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no live scheduler at finalize")))
    st, notes, _ = classify(lab)
    assert st == "HEALTHY", notes


def test_TS42_a_skew_carried_across_a_restart_is_still_suspect(lab):
    d7 = "2026-10-28"
    skew = [S2B.healthy_sample(et(2026, 10, 28, 11, 0) + timedelta(minutes=i), "6000:aaaaaaaa") for i in range(1, 61)]   # P1, +7 days, 60 ticks
    S2B.bulk_samples(lab.db, skew)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, "7000:bbbbbbbb") for t in S2B.minutes(et(2026, 10, 21, 12, 1), et(2026, 10, 21, 12, 10))])   # P2, real clock
    day(lab, et(2026, 10, 28, 9, 30), et(2026, 10, 28, 11, 0))
    day(lab, et(2026, 10, 28, 12, 0), et(2026, 10, 28, 16, 0))                                                          # a real 60-minute stall
    st, notes, _ = classify(lab, d7, trig=et(2026, 10, 28, 16, 10, 30))
    assert st == "DEGRADED" and {"LONG_GAP", "CLOCK_INCONSISTENT"} <= set(notes)


def test_TS43_rows_written_by_baseline_code_and_an_idempotent_migration(lab):
    """The in-suite half of T-S43 (the cross-worktree round trip against 5d6f7dd and 50cb2d7b is the evidence procedure of test plan 7 step 8)."""
    conn = sqlite3.connect(str(lab.db))
    try:
        conn.execute("INSERT INTO forward_validation_samples (sample_id, at, trading_date, in_session, owner, mode) VALUES ('v1', ?, ?, 1, 'base', 'MONITOR_ONLY')", (iso(CL), D))
        conn.commit()
        before = [tuple(r) for r in conn.execute("PRAGMA table_info(forward_validation_sessions)")]
    finally:
        conn.close()
    from database.forward_validation_migrations import run_forward_validation_migrations
    for _ in range(2):
        c2 = sqlite3.connect(str(lab.db))
        run_forward_validation_migrations(c2)
        c2.close()
    conn = sqlite3.connect(str(lab.db))
    try:
        assert [tuple(r) for r in conn.execute("PRAGMA table_info(forward_validation_sessions)")] == before
    finally:
        conn.close()
    seed_row(lab, "2026-11-26", "OPEN")                                                # a baseline-opened holiday row
    for t in (et(2026, 11, 27, 9, 0), et(2026, 11, 27, 9, 5), et(2026, 11, 27, 9, 10)):
        lab.tick(t)
    assert lab.session("2026-11-26")["notes"] == ["CALENDAR_CONFLICT"]
    st, notes, _ = classify(lab)                                                       # the v1 sample (NULL new columns) is the last in-window one
    assert st == "DEGRADED" and "END_STATE_UNKNOWN" in notes and lab.store.samples(D)[0]["rr_intents"] is None


# ================================================================ FAILED evidence (F-18, U-09, U-23) ======================================
def test_TS44_each_failed_code_from_its_v2_evidence(tmp_path, monkeypatch):
    def fresh(name):
        lb = S2B.Lab(tmp_path / name, monkeypatch)
        day(lb, O, et(2026, 10, 21, 15, 59))
        return lb
    cases = {}
    lb = fresh("dup")
    S2B.add_order(lb.db, "c1", et(2026, 10, 21, 11, 0)); S2B.add_order(lb.db, "c1", et(2026, 10, 21, 11, 1)); day(lb, CL, CL)
    cases["DUPLICATE_BROKER_ORDER"] = lb
    lb = fresh("live"); S2B.add_refresh(lb.db, et(2026, 10, 21, 11, 0), result="FAILED", error_text="refused: only the PAPER host is allowed"); day(lb, CL, CL)
    cases["LIVE_HOST_ATTEMPT"] = lb
    lb = fresh("gate"); S2B.add_safety_eval(lb.db, et(2026, 10, 21, 10, 59), state="NO_NEW_ENTRIES", allowed=False)
    S2B.add_intent(lb.db, et(2026, 10, 21, 11, 0), submitted=et(2026, 10, 21, 11, 0)); day(lb, CL, CL)
    cases["SAFETY_GATE_ALLOWED_UNDER_BAD_STATE"] = lb
    lb = fresh("stop"); S2B.add_event(lb.db, et(2026, 10, 21, 12, 0), "EARLY_STOP", "DUPLICATE_BROKER_ORDER,LIVE_HOST_ATTEMPT", {"stopped": False}, trading_date=D); day(lb, CL, CL)
    cases["EARLY_STOP"] = lb
    for code, field in (("UNRESOLVED_RECONCILIATION", {"attention_positions": 1}), ("UNRESOLVED_EXECUTION_STATE", {"failed_intents": 1}),
                        ("SAFETY_UNKNOWN_AT_END", {"safety_state": "UNKNOWN"})):
        lb = fresh(code.lower()); S2B.bulk_samples(lb.db, [S2B.healthy_sample(CL, lb.owner, **field)])
        cases[code] = lb
    for code, lb in cases.items():
        st, notes, _ = classify(lb)
        assert (st, notes) == ("FAILED", [code]), (code, notes)
    for k, expect in ((None, "FAILED"), (-1, "HEALTHY")):
        lb = S2B.Lab(tmp_path / f"bu{k}", monkeypatch)
        times = S2B.minutes(O, CL)
        n = len(times)
        bad = n // 2 + 1 if k is None else n // 2
        S2B.bulk_samples(lb.db, [S2B.healthy_sample(t, lb.owner, attention=["SNDK: broker unavailable"] if i < bad else []) for i, t in enumerate(times)])
        st, notes, _ = classify(lb)
        assert st == expect and (notes == ["BROKER_UNAVAILABLE_PERSISTENT"] if expect == "FAILED" else True)
    lb = S2B.Lab(tmp_path / "bu_count", monkeypatch)                                   # U-23 / B-M27: by sample COUNT, never by hold time
    dense = S2B.minutes(O, O + timedelta(minutes=60), 10)                               # 361 unavailable samples in the first hour (15 % of the window held)
    rest = S2B.minutes(O + timedelta(minutes=61), CL, 60)                               # 330 healthy samples
    S2B.bulk_samples(lb.db, [S2B.healthy_sample(t, lb.owner, attention=["SNDK: broker unavailable"]) for t in dense] + [S2B.healthy_sample(t, lb.owner) for t in rest])
    st, notes, _ = classify(lb)
    assert st == "FAILED" and notes == ["BROKER_UNAVAILABLE_PERSISTENT"]


def test_TS45_a_startup_at_0929_55_is_not_a_restart_during_the_session(lab):
    day(lab, et(2026, 10, 20, 9, 30), et(2026, 10, 20, 16, 0), owner="6000:aaaaaaaa")
    lab.new_process("8123:bbbbbbbb")
    S2B.add_startup(lab.db, et(2026, 10, 21, 9, 29, 55), 8123, D)
    day(lab, et(2026, 10, 21, 9, 30, 5), CL)
    post_close(lab, pc())
    assert "RESTART_DURING_SESSION" not in lab.session(D)["notes"]


@pytest.mark.parametrize("other_pid", [False, True])
def test_TS45b_a_takeover_after_an_older_startup_is_a_restart_during_the_session(lab, other_pid):
    S2B.add_startup(lab.db, et(2026, 10, 21, 9, 0), 7724, D)
    day(lab, O, et(2026, 10, 21, 10, 59), owner="7724:aaaaaaaa")
    if other_pid:
        S2B.add_startup(lab.db, et(2026, 10, 21, 11, 2), 9999, D)
    lab.new_process("7724:bbbbbbbb")                                                  # the same pid, no STARTUP (TAKEOVER)
    day(lab, et(2026, 10, 21, 11, 3), CL)
    post_close(lab, pc())
    row = lab.session(D)
    assert "RESTART_DURING_SESSION" in row["notes"] and row["assessment"]["restarts"] == [iso(et(2026, 10, 21, 11, 3))]


def test_TS46_the_deployed_duplicate_rule_and_its_pin_to_order_reliability(lab):
    day(lab)
    S2B.add_order(lab.db, "c1", et(2026, 10, 21, 11, 0))
    S2B.add_order(lab.db, "c1", et(2026, 10, 21, 11, 1))
    S2B.add_reconciliation(lab.db, et(2026, 10, 21, 12, 0), "MULTIPLE_ORDERS")
    st, notes, _ = classify(lab)
    lo, hi = "2026-10-21T04:00:00+00:00", "2026-10-22T03:59:59+00:00"
    assert (st, notes) == ("FAILED", ["DUPLICATE_BROKER_ORDER"]) and lab.fv._duplicate_evidence(lo, hi) == lab.fv.order_reliability(D)["duplicate_broker_orders"] == 2
    lab2 = S2B.Lab(lab.db.parent / "v", lab.monkeypatch)
    day(lab2)
    S2B.add_order(lab2.db, "c2", et(2026, 10, 21, 15, 0))
    S2B.add_order(lab2.db, "c2", et(2026, 10, 21, 16, 30))                              # (v) the second order after C
    assert classify(lab2)[0] == classify(lab2, trig=et(2026, 10, 22, 9, 10), late=True)[0] == "HEALTHY"


def test_TS47_samples_are_ordered_at_then_rowid_and_read_without_the_5000_cap(lab):
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 10, 0), lab.owner), S2B.healthy_sample(et(2026, 10, 21, 10, 0, 40), lab.owner, attention_positions=1),
                              S2B.healthy_sample(et(2026, 10, 21, 10, 0, 20), lab.owner)])
    got = [s["at"] for s in lab.store.samples_between(iso(O), iso(CL))]
    assert got == [iso(et(2026, 10, 21, 10, 0)), iso(et(2026, 10, 21, 10, 0, 20)), iso(et(2026, 10, 21, 10, 0, 40))]
    st, notes, a = classify(lab)
    assert a["samples"]["suspect_excluded"] == 0 and "UNRESOLVED_RECONCILIATION" in notes and a["end_state"][0]["at"] == iso(et(2026, 10, 21, 10, 0, 40))
    lab2 = S2B.Lab(lab.db.parent / "big", lab.monkeypatch)
    S2B.bulk_samples(lab2.db, [S2B.healthy_sample(O + timedelta(seconds=3.9 * i), lab2.owner) for i in range(6000)])
    assert len(lab2.store.samples_between(iso(O), iso(CL))) == 6000


def test_TS48_confirmation_needs_two_plausible_steps_and_is_never_queued(lab, tmp_path, monkeypatch):
    seed_row(lab, "2026-10-19")
    outs = [lab.tick(t) for t in (et(2026, 10, 21, 9, 0), et(2026, 10, 21, 9, 1), et(2026, 10, 21, 9, 2))]
    assert [o["backfilled"] for o in outs] == [[], [], ["2026-10-20"]] and lab.session("2026-10-20")["assessment"]["backfilled_by"]["at"] == iso(et(2026, 10, 21, 9, 2))
    lab2 = S2B.Lab(tmp_path / "v", monkeypatch)
    seed_row(lab2, "2026-10-19")
    t0 = et(2026, 10, 21, 9, 0)
    times = [t0, t0 + timedelta(seconds=60), t0 + timedelta(seconds=421), t0 + timedelta(seconds=481), t0 + timedelta(seconds=541)]
    outs = [lab2.tick(t) for t in times]
    assert [bool(o["backfilled"]) for o in outs] == [False, False, False, False, True]


def test_TS49_finalized_late_means_a_later_ny_date(lab, tmp_path, monkeypatch):
    day(lab)
    post_close(lab, pc())
    assert lab.session(D)["assessment"]["finalized_late"] is False
    lab2 = S2B.Lab(tmp_path / "late", monkeypatch)
    day(lab2, O, et(2026, 10, 21, 15, 59))
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab2.tick(t)
    assert lab2.session(D)["assessment"]["finalized_late"] is True


@pytest.mark.parametrize("source", ["orders", "recon", "gate", "runs", "acct", "cash", "events"])
@pytest.mark.parametrize("edge,counted", [("d0", True), ("d0-1", False), ("c", True), ("c+1", False)])
def test_TS50_every_evidence_source_is_bounded_by_e_of_d(lab, source, edge, counted):
    day(lab)
    at = {"d0": et(2026, 10, 21), "d0-1": et(2026, 10, 21) - SEC, "c": C, "c+1": C + SEC}[edge]
    if source == "orders":
        S2B.add_order(lab.db, "c1", at); S2B.add_order(lab.db, "c1", at)
    elif source == "recon":
        S2B.add_reconciliation(lab.db, at, "MULTIPLE_ORDERS")
    elif source == "gate":
        S2B.add_safety_eval(lab.db, at - SEC, state="NO_NEW_ENTRIES", allowed=False); S2B.add_intent(lab.db, at, submitted=at)
    elif source == "runs":
        S2B.add_run(lab.db, at, result="ERROR", error="X")
    elif source == "acct":
        S2B.add_refresh(lab.db, at, result="FAILED", error_code="E")
    elif source == "cash":
        S2B.add_cash_refresh(lab.db, at, result="FAILED", error_code="E")
    else:
        S2B.add_event(lab.db, at, "EARLY_STOP", "X", {"stopped": False}, trading_date=D)
    st, notes, _ = classify(lab)
    code = {"orders": "DUPLICATE_BROKER_ORDER", "recon": "DUPLICATE_BROKER_ORDER", "gate": "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", "runs": "EXECUTOR_CYCLE_ERRORS",
            "acct": "REFRESH_FAILURES", "cash": "REFRESH_FAILURES"}.get(source, "EARLY_STOP")
    assert (code in notes and "EVIDENCE_UNREADABLE" not in notes) if counted else st == "HEALTHY", (source, edge, notes)   # the reason, not only the class


@pytest.mark.parametrize("late", [False, True])
def test_TS51_a_mutable_control_row_with_the_live_host_mark_counts_without_an_upper_bound(lab, late):
    day(lab)
    S2B.add_control(lab.db, "TRANSPORT", et(2026, 10, 21, 18, 0), last_error="refused: only the PAPER host is allowed")
    st, notes, _ = classify(lab, trig=et(2026, 10, 22, 9, 10) if late else None, late=late)
    assert (st, notes) == ("FAILED", ["LIVE_HOST_ATTEMPT"])


def test_TS52_the_notes_are_exactly_the_code_set_in_order(lab):
    day(lab)
    post_close(lab, pc())
    assert lab.session(D)["notes_code"] == ""
    lab2 = S2B.Lab(lab.db.parent / "deg", lab.monkeypatch)
    day(lab2, et(2026, 10, 21, 10, 0), et(2026, 10, 21, 15, 0))
    S2B.add_refresh(lab2.db, et(2026, 10, 21, 12, 0), result="FAILED")
    st, notes, _ = classify(lab2)
    assert notes == ["OPEN_NOT_OBSERVED", "CLOSE_NOT_OBSERVED", "REFRESH_FAILURES"]


# ================================================================ store, metrics, read failures ==============================================
def test_TS53_store_behaviour(lab, tmp_path):
    from forward_validation.store import EvidenceSourceAbsent, ForwardValidationStore, ForwardValidationStoreError
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL, lab.owner, rr_intents=None, attention_positions=0)])
    s = lab.store.samples(D)[-1]
    assert s["rr_intents"] is None and s["attention_positions"] == 0
    lab.store.add_sample({**S2B.healthy_sample(CL + SEC, lab.owner), "failed_intents": None, "rr_intents": 0})
    s = lab.store.samples(D)[-1]
    assert s["failed_intents"] is None and s["rr_intents"] == 0
    seed_row(lab, D, "OPEN")
    lab.store.update_session(D, CL, classifier_version="x", assessment_json='{"a": 1}')
    assert lab.store.session(D)["assessment"] == {"a": 1}
    lab.store.update_session(D, CL, session_status="HEALTHY")
    with pytest.raises(ForwardValidationStoreError):
        lab.store.update_session(D, CL, assessment_json="{}")
    lab.store.insert_not_run("2026-10-19", CL, "NO_OBSERVATION", assessment={"backfilled_by": {"at": "x"}}, classifier_version="session_classifier_v2")
    assert lab.store.session("2026-10-19")["assessment"] == {"backfilled_by": {"at": "x"}}
    with pytest.raises(EvidenceSourceAbsent):
        lab.store.query_strict("SELECT * FROM no_such_table")
    with pytest.raises(EvidenceSourceAbsent):
        lab.store.query_strict("SELECT no_such_column FROM forward_validation_samples")
    with pytest.raises(ForwardValidationStoreError):
        ForwardValidationStore(tmp_path / "missing.db").query_strict("SELECT 1")


def test_TS54_v1_classify_session_is_unchanged_and_v2_is_separate_with_literal_constants():
    base = subprocess.run(["git", "show", "50cb2d7bc0384f1a020c01c9ba22c52c1283f339:forward_validation/metrics.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")
    now = (ROOT / "forward_validation" / "metrics.py").read_text(encoding="utf-8")
    pick = lambda t, n: next(ast.dump(x) for x in ast.parse(t).body if isinstance(x, ast.FunctionDef) and x.name == n)  # noqa: E731
    assert pick(base, "classify_session") == pick(now, "classify_session")
    consts = {x.targets[0].id: x.value.value for x in ast.parse(now).body if isinstance(x, ast.Assign) and isinstance(x.value, ast.Constant) and isinstance(x.targets[0], ast.Name)}
    assert (consts["G"], consts["H"], consts["K"], consts["CLASSIFIER_VERSION"], consts["EVIDENCE_READ_ATTEMPTS"]) == (360, 60, 3, "session_classifier_v2_1", 3)
    v2 = now[now.index("# ---------------------------------------------------------------- Wave 2 PR B"):]
    assert "getenv" not in v2 and "config." not in v2 and "import config" not in v2                  # T-S59 (i): nothing from the environment
    init_now = (ROOT / "forward_validation" / "__init__.py").read_text(encoding="utf-8")
    init_base = subprocess.run(["git", "show", "50cb2d7bc0384f1a020c01c9ba22c52c1283f339:forward_validation/__init__.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")
    assert init_now.replace("\r\n", "\n") == init_base.replace("\r\n", "\n")
    from forward_capture.health import MIN_COVERAGE
    assert MIN_COVERAGE == 0.75


def test_TS55_i_ii_a_missing_table_or_column_is_unavailable_at_once(lab, tmp_path, monkeypatch):
    day(lab)
    lab.sql("DROP TABLE execution_runs")
    st, notes, a = classify(lab)
    assert st == "DEGRADED" and "EVIDENCE_UNREADABLE" in notes and "execution_runs" in a["unavailable_sources"]
    lab2 = S2B.Lab(tmp_path / "col", monkeypatch)
    day(lab2)
    lab2.sql("ALTER TABLE execution_reconciliations RENAME COLUMN detail_json TO detail_hidden")
    st, notes, a = classify(lab2)
    assert st == "DEGRADED" and "EVIDENCE_UNREADABLE" in notes


def test_TS55_ii_viii_a_transient_failure_never_skips_the_early_stop(lab, monkeypatch):
    from forward_validation import store as FS
    S2B.add_reconciliation(lab.db, et(2026, 10, 16, 11, 0), "MULTIPLE_ORDERS")
    day(lab)
    lab.tick(CL + timedelta(seconds=30))
    lab.tick(CL + timedelta(seconds=330))
    lab.x.store.set_mode("PAPER_AUTO", et(2026, 10, 21, 16, 8), "user", "test arm", {})
    real = FS.ForwardValidationStore.query_strict
    state = {"armed": True}

    def flaky(self, sql, args=()):
        if state["armed"] and "execution_runs" in sql:
            state["armed"] = False
            raise sqlite3.OperationalError("database is locked")
        return real(self, sql, args)
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict", flaky)
    o = lab.tick(CL + timedelta(seconds=630))
    assert lab.session(D)["session_status"] == "OPEN" and o["early_stop"]["stopped"] is True and lab.x.mode()["mode"] == "MONITOR_ONLY"
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ"]
    assert len(errs) == 1 and errs[0]["trading_date"] == D
    lab.tick(CL + timedelta(seconds=930))
    assert lab.session(D)["session_status"] == "HEALTHY"                                # finalized at the next tick; the 16:10:30 stop is after C (B-11)


def test_TS55_iii_iv_a_persistent_failure_escalates_after_the_bound_and_the_date_end(lab, monkeypatch):
    from forward_validation import store as FS
    day(lab)
    S2B.add_event(lab.db, et(2026, 10, 21, 12, 0), "EARLY_STOP", "X", {"stopped": False}, trading_date=D)
    real = FS.ForwardValidationStore.query_strict

    def broken(self, sql, args=()):
        if "execution_runs" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, sql, args)
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict", broken)
    post_close(lab, pc() + [CL + timedelta(seconds=s) for s in (1230, 1530)])
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ" and e["trading_date"] == D]
    assert len(errs) >= 3 and lab.session(D)["session_status"] == "OPEN"               # past the bound, retried, never escalated before the date ends (B-M53)
    lab.new_process("7002:bbbbbbbb")                                                   # B-M60: the attempt count is persisted - a restart does not reset it
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    row = lab.session(D)
    assert row["session_status"] == "FAILED" and row["notes"] == ["EARLY_STOP"] and "execution_runs" in row["assessment"]["unavailable_sources"]
    assert "EVIDENCE_UNREADABLE" in row["assessment"]["degraded_codes"]


def test_TS55_v_vi_unreadable_backfill_is_never_not_run_and_other_dates_proceed(lab, monkeypatch):
    from forward_validation import store as FS
    seed_row(lab, "2026-10-16")
    real = FS.ForwardValidationStore.samples_between

    def broken(self, lo, hi):
        if lo.startswith("2026-10-20"):
            raise RuntimeError("bug: unreadable (test)")
        return real(self, lo, hi)
    monkeypatch.setattr(FS.ForwardValidationStore, "samples_between", broken)
    for t in S2B.minutes(et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 2), 60):
        lab.tick(t)
    assert lab.session("2026-10-19")["session_status"] == "NOT_RUN" and lab.session("2026-10-21")["session_status"] == "NOT_RUN"   # (ix) in the SAME tick (B-M63)
    assert lab.session("2026-10-20") is None                                           # unreadable: retried, never NOT_RUN
    for t in S2B.minutes(et(2026, 10, 22, 9, 3), et(2026, 10, 22, 9, 6), 60):
        lab.tick(t)
    row = lab.session("2026-10-20")
    assert row is not None and row["session_status"] == "DEGRADED" and "EVIDENCE_UNREADABLE" in row["notes"]
    before = {r["trading_date"]: r["session_status"] for r in lab.store.sessions(newest_first=False)}
    n0 = len([e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ"])
    monkeypatch.setattr(FS.ForwardValidationStore, "own_tail", lambda self, owner, n: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    o = lab.tick(et(2026, 10, 22, 9, 7))
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ"][n0:]
    assert o["result"] == "OK" and "early_stop" in o and "attention" in o and o["backfilled"] == []   # (vi) contained: early stop and attention ran
    assert [(e["trading_date"], e["detail"]["error"]) for e in errs] == [("2026-10-22", "OperationalError")]   # exactly one event, dated today
    assert {r["trading_date"]: r["session_status"] for r in lab.store.sessions(newest_first=False)} == before   # not confirmed: no transition


def test_TS55_viii_a_raw_error_of_the_tolerant_reader_is_contained_and_the_early_stop_runs(lab, monkeypatch):
    """W-02 (viii): ForwardValidationStore._ro raises a raw sqlite3.OperationalError once, inside the evidence assembly, so the pinned
    tolerant _gate_violations read fails; a critical condition with Paper Auto armed by a person."""
    from forward_validation import service as SV
    from forward_validation import store as FS
    S2B.add_reconciliation(lab.db, et(2026, 10, 16, 11, 0), "MULTIPLE_ORDERS")
    day(lab)
    lab.tick(CL + timedelta(seconds=30))
    lab.tick(CL + timedelta(seconds=330))
    lab.x.store.set_mode("PAPER_AUTO", et(2026, 10, 21, 16, 8), "user", "test arm", {})
    real_ro, real_rows = FS.ForwardValidationStore._ro, SV.ForwardValidationService._row_evidence
    state = {"armed": False, "fired": 0}

    def ro(self):
        if state["armed"]:
            state["armed"], state["fired"] = False, state["fired"] + 1
            raise sqlite3.OperationalError("disk I/O error")
        return real_ro(self)

    def rows(self, *a, **k):
        state["armed"] = state["fired"] == 0
        try:
            return real_rows(self, *a, **k)
        finally:
            state["armed"] = False
    monkeypatch.setattr(FS.ForwardValidationStore, "_ro", ro)
    monkeypatch.setattr(SV.ForwardValidationService, "_row_evidence", rows)
    o = lab.tick(CL + timedelta(seconds=630))
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ"]
    assert state["fired"] == 1 and lab.session(D)["session_status"] == "OPEN"             # no finalize in that tick
    assert [(e["trading_date"], e["detail"]["error"], e["detail"]["source"]) for e in errs] == [(D, "OperationalError", "execution_intents+execution_safety_evaluations")]
    assert o["early_stop"]["stopped"] is True and lab.x.mode()["mode"] == "MONITOR_ONLY" and lab.events("EARLY_STOP")   # the same tick's early stop ran
    lab.tick(CL + timedelta(seconds=930))
    assert lab.session(D)["session_status"] == "HEALTHY"                                # finalized normally at the next confirmed tick


def _ix_ticks(lab):
    outs = post_close(lab, pc() + [CL + timedelta(seconds=s) for s in (1230, 1530)])
    open_at_midnight = lab.session(D)["session_status"]
    outs += [lab.tick(t) for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10), et(2026, 10, 22, 9, 15))]
    return outs, open_at_midnight


def test_TS55_ix_a_bug_in_the_assessment_is_contained_every_tick_and_escalates_fail_closed(lab, monkeypatch):
    """W-02 (ix): _assess raises a RuntimeError (a bug) at every confirmed sample. Contained every tick; after 3 events and the date's end
    the date escalates to DEGRADED EVIDENCE_UNREADABLE even though the escalated assessment fails too (B-44 row 5: never OPEN forever)."""
    from forward_validation import service as SV
    day(lab)
    monkeypatch.setattr(SV.ForwardValidationService, "_assess", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("bug (test)")))
    outs, at_midnight = _ix_ticks(lab)
    assert all(o["result"] == "OK" and "early_stop" in o and "attention" in o for o in outs)   # the early stop and attention ran every tick
    row = lab.session(D)
    assert at_midnight == "OPEN" and row["session_status"] == "DEGRADED" and row["notes"] == ["EVIDENCE_UNREADABLE"]
    assert row["assessment"]["finalized_late"] is True and row["assessment"]["unavailable_sources"]["assessment"]["error"].startswith("RuntimeError")
    assert row["assessment"]["unavailable_sources"]["assessment"]["attempts"] >= 3 and row["classifier_version"] == "session_classifier_v2_1"
    n = len([e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ" and e["trading_date"] == D])
    lab.tick(et(2026, 10, 22, 9, 20))
    assert len([e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ" and e["trading_date"] == D]) == n   # closed: the events stop


def test_TS55_ix_with_the_event_write_failing_the_tick_completes_logs_and_the_date_stays_open(lab, monkeypatch, caplog):
    from forward_validation import service as SV
    from forward_validation import store as FS
    day(lab)
    monkeypatch.setattr(SV.ForwardValidationService, "_assess", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("bug (test)")))
    real = FS.ForwardValidationStore.add_event

    def add_event(self, now, kind, *a, **k):
        if kind == "OBSERVER_ERROR":
            raise sqlite3.OperationalError("disk full (test)")
        return real(self, now, kind, *a, **k)
    monkeypatch.setattr(FS.ForwardValidationStore, "add_event", add_event)
    outs, at_midnight = _ix_ticks(lab)
    assert all(o["result"] == "OK" and "early_stop" in o and "attention" in o for o in outs)
    assert at_midnight == "OPEN" and lab.session(D)["session_status"] == "OPEN" and not lab.events("OBSERVER_ERROR")   # escalation waits for persisted events
    assert "was not recorded" in caplog.text


def test_TS55_i_iii_an_absent_source_never_escalates_another_sources_transient_failure(lab, monkeypatch):
    """B-44 rows 5-6: a structurally absent table is unavailable at once, but a transient failure of ANOTHER source while accounting a date
    is retried (the bound is not reached), so that source's FAILED evidence is not hidden behind EVIDENCE_UNREADABLE."""
    from forward_validation import service as SV
    from forward_validation import store as FS
    seed_row(lab, "2026-10-16")
    day(lab, et(2026, 10, 20, 9, 30), et(2026, 10, 20, 16, 0))
    S2B.add_order(lab.db, "c1", et(2026, 10, 20, 11, 0))
    S2B.add_order(lab.db, "c1", et(2026, 10, 20, 11, 1))
    lab.sql("DROP TABLE execution_runs")
    real_q, real_fin = FS.ForwardValidationStore.query_strict, SV.ForwardValidationService._finalize
    state = {"in": False, "fired": False}

    def fin(self, *a, **k):
        state["in"] = True
        try:
            return real_fin(self, *a, **k)
        finally:
            state["in"] = False

    def flaky(self, sql, args=()):
        if state["in"] and not state["fired"] and "FROM execution_orders WHERE first_seen_at" in sql:
            state["fired"] = True
            raise sqlite3.OperationalError("database is locked")
        return real_q(self, sql, args)
    monkeypatch.setattr(SV.ForwardValidationService, "_finalize", fin)
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict", flaky)
    lab.run([et(2026, 10, 21, 9, 0), et(2026, 10, 21, 9, 1), et(2026, 10, 21, 9, 2)])
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ" and e["trading_date"] == "2026-10-20"]
    assert state["fired"] and [(e["detail"]["phase"], e["detail"]["error"]) for e in errs] == [("finalize", "OperationalError")]   # one failure, not escalated
    row = lab.session("2026-10-20")                                                    # retried by the same tick's late finalize of OPEN rows
    assert (row["session_status"], row["notes"]) == ("FAILED", ["DUPLICATE_BROKER_ORDER"]) and "execution_runs" in row["assessment"]["unavailable_sources"]


def test_TS55_vii_a_missing_safety_table_is_unavailable_never_zero_gate_violations(lab):
    day(lab)
    lab.sql("DROP TABLE execution_safety_evaluations")
    st, notes, a = classify(lab)
    assert "EVIDENCE_UNREADABLE" in notes and "execution_intents+execution_safety_evaluations" in a["unavailable_sources"]


def test_TS56_persistent_mismatch_never_counts_thanksgiving(lab):
    S2B.add_position(lab.db, et(2026, 11, 25, 10, 0))
    day(lab, et(2026, 11, 25, 9, 30), et(2026, 11, 25, 16, 0))
    post_close(lab, pc(et(2026, 11, 25, 16, 0)))
    first = None
    day(lab, et(2026, 11, 27, 9, 30), et(2026, 11, 27, 13, 0))
    for t in pc(et(2026, 11, 27, 13, 0)):
        o = lab.tick(t)
        if "PERSISTENT_RECONCILIATION_MISMATCH" in o["attention"] and first is None:
            first = o["at"]
    assert first is None
    day(lab, et(2026, 11, 30, 9, 30), et(2026, 11, 30, 16, 0))
    for t in pc(et(2026, 11, 30, 16, 0)):
        o = lab.tick(t)
        if "PERSISTENT_RECONCILIATION_MISMATCH" in o["attention"] and first is None:
            first = o["at"]
    assert first == lab.session("2026-11-30")["ended_at"]


def test_TS57_the_610a_record_is_final_only_after_the_v2_finalize(tmp_path, monkeypatch):
    from test_forward_validation_61 import rig as rig61, obs, cycle, started, AFTER_CLOSE as AC
    r = rig61(tmp_path, monkeypatch)
    started(r)
    S2B.cover_until(r, r.clock.now)
    obs(r)
    S2B.cover_until(r, CL, inclusive=True)
    finals = []
    for k in range(3):
        r.clock.now = AC + timedelta(minutes=5 * k)
        r.x.run_cycle(r.clock.now, "TEST")
        cycle(r)
        o = obs(r)
        finals.append((r.fv.store.session(D)["session_status"], (o.get("capture") or {}).get("health", {}).get("final")))
    assert finals[0][0] == "OPEN" and not finals[0][1] and finals[1][0] != "OPEN" and finals[1][1] is True


def test_TS58_an_open_holiday_row_is_calendar_conflict_unless_failed(lab, tmp_path, monkeypatch):
    seed_row(lab, "2026-11-26", "OPEN")
    S2B.add_refresh(lab.db, et(2026, 11, 27, 9, 9))                                    # settled: a non-market day's cutoff is the next NY midnight
    st, notes, _ = S2B.assess(lab.fv, "2026-11-26", et(2026, 11, 27, 9, 10), late=True)
    assert (st, notes) == ("DEGRADED", ["CALENDAR_CONFLICT"])
    for hh, mm, ss in ((11, 0, 0), (23, 59, 59)):
        lb = S2B.Lab(tmp_path / f"h{hh}", monkeypatch)
        seed_row(lb, "2026-11-26", "OPEN")
        S2B.add_event(lb.db, et(2026, 11, 26, hh, mm, ss), "EARLY_STOP", "X", {"stopped": False}, trading_date="2026-11-26")
        S2B.add_refresh(lb.db, et(2026, 11, 27, 9, 9))
        assert S2B.assess(lb.fv, "2026-11-26", et(2026, 11, 27, 9, 10), late=True)[:2] == ("FAILED", ["EARLY_STOP"])


def test_TS59_the_idle_cadence_gap_at_the_open_is_tolerated(lab):
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 9, 29, 59), lab.owner)])
    day(lab, et(2026, 10, 21, 9, 34, 59), CL)
    assert "OPEN_NOT_OBSERVED" not in classify(lab)[1]


def test_TS60_thanksgiving_week_backfills_only_the_early_close_friday(lab):
    seed_row(lab, "2026-11-25")
    for t in (et(2026, 11, 30, 9, 0), et(2026, 11, 30, 9, 5), et(2026, 11, 30, 9, 10)):
        o = lab.tick(t)
    assert o["backfilled"] == ["2026-11-27"]


# ================================================================ session accounting (V-02, V-10) ==========================================
def test_TS61_a_late_start_at_1558_30_gets_a_row(lab):
    for t in (et(2026, 10, 21, 15, 58, 30), et(2026, 10, 21, 15, 59, 30), et(2026, 10, 21, 16, 0, 30)):
        lab.tick(t)
    row = lab.session(D)
    assert row is not None and row["started_at"] == iso(et(2026, 10, 21, 15, 58, 30))
    post_close(lab, [et(2026, 10, 21, 16, 5, 30), et(2026, 10, 21, 16, 10, 30)])
    assert lab.session(D)["session_status"] == "DEGRADED" and {"OPEN_NOT_OBSERVED", "COVERAGE_LOW"} <= set(lab.session(D)["notes"])


def test_TS62_a_crash_after_two_ticks_with_an_early_stop_is_failed_the_next_day(lab):
    S2B.add_reconciliation(lab.db, et(2026, 10, 16, 11, 0), "MULTIPLE_ORDERS")
    lab.x.store.set_mode("PAPER_AUTO", et(2026, 10, 21, 10, 59), "user", "test arm", {})
    lab.tick(et(2026, 10, 21, 11, 0))
    assert lab.events("EARLY_STOP")                                                  # the stop at 11:00 (the first tick)
    lab.tick(et(2026, 10, 21, 11, 1))
    lab.new_process("7002:bbbbbbbb")
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    row = lab.session(D)
    assert row["session_status"] == "FAILED" and "EARLY_STOP" in row["notes"] and row["started_at"] == iso(et(2026, 10, 21, 11, 0))
    assert row["assessment"]["finalized_late"] is True


def test_TS63_failed_evidence_without_a_sample_is_accounted_never_not_run(lab):
    seed_row(lab, "2026-10-20")
    S2B.add_reconciliation(lab.db, et(2026, 10, 21, 11, 0), "MULTIPLE_ORDERS")
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    row = lab.session(D)
    assert row["session_status"] == "FAILED" and row["notes"] == ["DUPLICATE_BROKER_ORDER"]


def test_TS64_one_in_window_sample_gets_a_row_and_the_next_day_is_backfilled(lab):
    lab.tick(et(2026, 10, 21, 10, 0))
    lab.new_process("7002:bbbbbbbb")
    for t in (et(2026, 10, 23, 9, 0), et(2026, 10, 23, 9, 5), et(2026, 10, 23, 9, 10)):
        lab.tick(t)
    row = lab.session(D)
    assert row["session_status"] == "DEGRADED" and {"COVERAGE_LOW", "OPEN_NOT_OBSERVED", "CLOSE_NOT_OBSERVED"} <= set(row["notes"])
    assert lab.session("2026-10-22")["session_status"] == "NOT_RUN"


def test_TS65_a_fresh_database_accounts_its_first_observed_day(lab):
    lab.tick(et(2026, 10, 21, 10, 0))
    lab.tick(et(2026, 10, 21, 10, 1))
    lab.new_process("7002:bbbbbbbb")
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    assert lab.session(D) is not None and [r["trading_date"] for r in lab.store.sessions(newest_first=False)] == [D, "2026-10-22"]
    row = lab.session(D)                                                               # B-24 (a): accounted by P2's confirmed sample (v1 opened it at P1's first tick)
    assert row["classifier_version"] == "session_classifier_v2_1" and row["assessment"]["opened_by"]["owner"] == "7002:bbbbbbbb"
    assert row["assessment"]["finalized_late"] is True and row["session_status"] == "DEGRADED" and row["started_at"] == iso(et(2026, 10, 21, 10, 0))


def test_TS66_a_crash_after_a_stall_is_finalized_late_from_its_samples(lab):
    for t in (et(2026, 10, 21, 9, 30, 30), et(2026, 10, 21, 9, 31, 30), et(2026, 10, 21, 9, 32, 30)):
        lab.tick(t)
    day(lab, et(2026, 10, 21, 9, 33, 30), et(2026, 10, 21, 10, 0, 30))
    lab.tick(et(2026, 10, 21, 15, 58, 30))
    lab.new_process("7002:bbbbbbbb")
    for t in (et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), et(2026, 10, 22, 9, 10)):
        lab.tick(t)
    row = lab.session(D)
    assert {"LONG_GAP", "COVERAGE_LOW"} <= set(row["notes"]) and row["assessment"]["end_state"][0]["at"] == iso(et(2026, 10, 21, 15, 58, 30))


# ================================================================ T-S25: the HEALTHY transition matrix (design 5.6; R-06, U-16, V-05, W-04) ==
TS25_TRACES = 400
TS25_CLASSES = ("i", "ii", "iii", "iv", "v", "vi")


def _ts25_template(tmp_path):
    tpl = tmp_path / "ts25_template.db"
    S2B.migrate(tpl)                                                                   # one migration; every trace starts from a byte copy
    return tpl


def _ts25_one(tmp_path, monkeypatch, tpl, tr):
    import shutil
    try:
        res = S2B.ts25_run(tr, tmp_path, monkeypatch, tpl)
    finally:
        shutil.rmtree(tmp_path / f"ts25_{tr['seed']}", ignore_errors=True)
    return res


def test_TS25_the_v1_oracle_is_50cb2d7b_verbatim():
    from forward_validation import service as SV
    base = subprocess.run(["git", "show", "50cb2d7bc0384f1a020c01c9ba22c52c1283f339:forward_validation/service.py"], cwd=ROOT, capture_output=True, check=True)
    cls = next(n for n in ast.parse(base.stdout.decode("utf-8").replace("\r\n", "\n")).body if isinstance(n, ast.ClassDef) and n.name == "ForwardValidationService")
    want = {m.name: ast.dump(m) for m in cls.body if isinstance(m, ast.FunctionDef) and m.name in S2B.V1_ORACLE_METHODS}
    sup = ast.parse((ROOT / "tests" / "wave2_prb_support.py").read_text(encoding="utf-8"))
    fac = next(n for n in sup.body if isinstance(n, ast.FunctionDef) and n.name == "v1_oracle_class")
    v1 = next(n for n in fac.body if isinstance(n, ast.ClassDef) and n.name == "V1Oracle")
    got = {m.name: ast.dump(m) for m in v1.body if isinstance(m, ast.FunctionDef)}
    assert set(got) == set(want) == set(S2B.V1_ORACLE_METHODS) and got == want        # W-04: _backfill_not_run included, each AST-equal to 50cb2d7b
    V1 = S2B.v1_oracle_class()
    assert all(getattr(V1, n) is not getattr(SV.ForwardValidationService, n) for n in S2B.V1_ORACLE_METHODS) and V1.__mro__[1] is SV.ForwardValidationService


def test_TS25_the_healthy_transition_matrix_over_400_seeded_traces(tmp_path, monkeypatch):
    """For every trace where v1 is not HEALTHY and v2 is HEALTHY, every v1 code is explained by its class (i)-(vi) with the class's predicate
    true on the trace; v1 NOT_RUN -> v2 HEALTHY and v1 OPEN_NOT_OBSERVED -> v2 HEALTHY always fail; v2 is never HEALTHY with FAILED evidence in
    E(d) or a FAILED end state. Each class explains at least 20 traces; class (vi) only on traces with a synthetic FAILED intent (W-03)."""
    from collections import Counter
    tpl = _ts25_template(tmp_path)
    problems, per_class, outcomes, negatives = [], {k: [] for k in TS25_CLASSES}, Counter(), 0
    for seed in range(TS25_TRACES):
        res = _ts25_one(tmp_path, monkeypatch, tpl, S2B.ts25_trace(seed))
        bad, used = S2B.ts25_check(res)
        problems += [(seed, res["trace"]["family"], res["trace"]["variant"], b) for b in bad]
        for cl in used:
            per_class[cl].append(seed)
        if "vi" in used:
            assert any(p["kind"] == "failed_intent" for p in res["trace"]["planted"]), seed
        outcomes[f"{res['v1']['status']}->{res['v2']['status']}"] += 1
        negatives += bool(res["facts"]["failed_in_E"] or res["facts"]["end_failed"])
    summary = {"traces": TS25_TRACES, "outcomes": dict(sorted(outcomes.items())), "per_class": {k: len(v) for k, v in per_class.items()}, "negatives": negatives,
               "violations": len(problems)}
    print("T-S25", json.dumps(summary, sort_keys=True))
    assert problems == [], problems[:20]
    assert all(len(v) >= 20 for v in per_class.values()), summary["per_class"]
    assert negatives >= 20 and outcomes["HEALTHY->HEALTHY"] >= 20 and sum(n for k, n in outcomes.items() if k.endswith("->HEALTHY")) >= 150, summary
    assert sum(n for k, n in outcomes.items() if k.endswith("->OPEN") or k.startswith(("NO_TICK", "OPEN"))) == 0, summary   # every trace finalized by both


@pytest.mark.parametrize("mutant", ["gap_rule_ignored", "grace_samples_ignored", "early_stop_ignored"])
def test_TS25_negative_controls_the_property_fails_on_each_planted_v2_mutant(tmp_path, monkeypatch, mutant):
    """B-M59: the property detects unexpected transitions. The long-gap mutant ignores the gap rule (every gap of S over G: open, interior and
    close); an interior-only gap has no v1 code, so no v1 -> v2 transition can show it (T-S02 / T-S04 pin LONG_GAP itself)."""
    from forward_validation import metrics as M
    from forward_validation import service as SV
    if mutant == "gap_rule_ignored":
        monkeypatch.setattr(M, "session_gaps", lambda S, o, c: [0.0] * (len(S) + 1))
        family, variants = "gap", ("close gap",)
    elif mutant == "grace_samples_ignored":
        real_obs = SV.ForwardValidationService._session_observations

        def no_grace(self, *a, **k):
            ev, info = real_obs(self, *a, **k)
            ev["end_state"] = ev["S"][-1:]
            return ev, info
        monkeypatch.setattr(SV.ForwardValidationService, "_session_observations", no_grace)
        family, variants = "neg", ("rr_grace", "failed_grace")
    else:
        real_rows = SV.ForwardValidationService._row_evidence

        def no_stop(self, *a, **k):
            failed, degraded = real_rows(self, *a, **k)
            return {**failed, "EARLY_STOP": 0}, degraded
        monkeypatch.setattr(SV.ForwardValidationService, "_row_evidence", no_stop)
        family, variants = "neg", ("early_in",)
    tpl = _ts25_template(tmp_path)
    traces = [tr for tr in (S2B.ts25_trace(1000 + s, family) for s in range(200)) if tr["variant"] in variants][:5]
    assert len(traces) == 5
    detected = [bool(S2B.ts25_check(_ts25_one(tmp_path, monkeypatch, tpl, tr))[0]) for tr in traces]
    assert all(detected), (mutant, detected)


# ================================================================ T-S67: a FAILED end state in the grace window (operator D-2) ===============
def test_TS67_i_no_in_session_sample_and_a_failed_grace_end_state_is_failed_on_time(lab):
    """D-2 (i): the process starts after the close with a RECONCILIATION_REQUIRED intent open; the grace samples persist rr_intents = 1 and no
    in-window sample exists. The confirmed tick after C + h opens the day (B-22) and finalizes it FAILED, never NOT_RUN."""
    S2B.add_intent(lab.db, et(2026, 10, 21, 15, 0), state="RECONCILIATION_REQUIRED")
    for t in (CL + timedelta(seconds=60), CL + timedelta(seconds=300), CL + timedelta(seconds=540)):   # 16:01 and 16:05 (G(d)), then 16:09
        lab.tick(t)
    row = lab.session(D)
    assert row is not None and (row["session_status"], row["notes"]) == ("FAILED", ["UNRESOLVED_RECONCILIATION"])
    assert row["assessment"]["samples"]["in_window"] == 0 and row["assessment"]["finalized_late"] is False


@pytest.mark.parametrize("seeded", [True, False])
def test_TS67_i_late_the_accounting_never_backfills_a_failed_grace_day_not_run(lab, seeded):
    """D-2 (i), late path: two grace samples, never confirmed on d; the next day's process accounts d FAILED (B-24 (a)), never NOT_RUN (b).
    With L0 present d is also a back-fill candidate; on a fresh database only (a) can account it."""
    if seeded:
        seed_row(lab, "2026-10-20")
    S2B.add_intent(lab.db, et(2026, 10, 21, 15, 0), state="RECONCILIATION_REQUIRED")
    lab.tick(CL + timedelta(seconds=60))
    lab.tick(CL + timedelta(seconds=300))
    lab.new_process("7002:bbbbbbbb")
    lab.run([et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 1), et(2026, 10, 22, 9, 2)])
    row = lab.session(D)
    assert (row["session_status"], row["notes"]) == ("FAILED", ["UNRESOLVED_RECONCILIATION"]) and row["assessment"]["finalized_late"] is True
    assert not [e for e in lab.events("SESSION_NOT_RUN") if e["trading_date"] == D]


def test_TS67_ii_no_in_session_sample_and_no_qualifying_evidence_is_not_run(lab):
    """D-2 (ii): grace samples with a known, clean end state; a RECONCILIATION_REQUIRED intent only after C (outside G(d) and E(d)). The day is
    genuinely unobserved: nothing is opened at the close, and it is back-filled NOT_RUN."""
    seed_row(lab, "2026-10-20")
    lab.tick(CL + timedelta(seconds=60))
    lab.tick(CL + timedelta(seconds=300))
    S2B.add_intent(lab.db, C + timedelta(seconds=30), state="RECONCILIATION_REQUIRED")
    lab.tick(C + timedelta(seconds=60))                                                # confirmed, after C: not an end-state sample
    assert lab.session(D) is None
    lab.new_process("7002:bbbbbbbb")
    lab.run([et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 1), et(2026, 10, 22, 9, 2)])
    row = lab.session(D)
    assert (row["session_status"], row["notes"]) == ("NOT_RUN", ["NO_OBSERVATION"])


@pytest.mark.parametrize("case", ["null_end_state", "unreadable_source", "in_window_unreadable", "restart_unknown"])
def test_TS67_iii_missing_or_unreadable_required_evidence_never_yields_healthy_or_not_run(lab, case):
    """D-2 (iii): missing (a NULL count in the end state) or unreadable evidence is never HEALTHY, and a day without an in-window sample is then
    opened and finalized DEGRADED - never back-filled NOT_RUN."""
    seed_row(lab, "2026-10-20")
    if case == "null_end_state":
        S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL + timedelta(seconds=120), "6000:aaaaaaaa", rr_intents=None)])   # a v1-written grace sample
    elif case == "unreadable_source":
        S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL + timedelta(seconds=120), "6000:aaaaaaaa")])
        lab.sql("DROP TABLE execution_runs")
    elif case == "in_window_unreadable":
        day(lab, owner="6000:aaaaaaaa")
        lab.sql("DROP TABLE execution_safety_evaluations")
    else:
        day(lab, O, et(2026, 10, 21, 15, 57), owner="6000:aaaaaaaa")
        S2B.bulk_samples(lab.db, [S2B.healthy_sample(CL + timedelta(seconds=30), "6001:bbbbbbbb")])   # the new process's first sample after the close
        lab.sql("DROP TABLE autorun_events")
    lab.run([et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 1), et(2026, 10, 22, 9, 2)])
    row = lab.session(D)
    assert row is not None and row["session_status"] == "DEGRADED", (case, row and row["notes"])
    want = {"null_end_state": "END_STATE_UNKNOWN", "unreadable_source": "EVIDENCE_UNREADABLE", "in_window_unreadable": "EVIDENCE_UNREADABLE",
            "restart_unknown": "EVIDENCE_UNREADABLE"}[case]
    assert want in row["notes"] and not [e for e in lab.events("SESSION_NOT_RUN") if e["trading_date"] == D]


@pytest.mark.parametrize("sec,expect", [(360, "FAILED"), (361, "NOT_RUN")])
def test_TS67_iv_the_early_close_grace_boundary(lab, sec, expect):
    """D-2 (iv): the day after Thanksgiving closes at 13:00 ET, so C = 13:06:00. A sample with rr_intents = 1 at exactly C is in G(d): the
    day is FAILED. One second later it is outside G(d): no qualifying evidence, the day is NOT_RUN."""
    seed_row(lab, "2026-11-25")
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 11, 27, 13, 0) + timedelta(seconds=sec), "6000:aaaaaaaa", rr_intents=1)])
    lab.run([et(2026, 11, 30, 9, 0), et(2026, 11, 30, 9, 1), et(2026, 11, 30, 9, 2)])
    row = lab.session("2026-11-27")
    assert (row["session_status"], row["notes"]) == ((expect, ["UNRESOLVED_RECONCILIATION"]) if expect == "FAILED" else (expect, ["NO_OBSERVATION"]))
    assert lab.session("2026-11-26") is None                                           # Thanksgiving: never a row


def test_TS67_iv_on_time_at_an_early_close(lab):
    S2B.add_intent(lab.db, et(2026, 11, 27, 12, 0), state="RECONCILIATION_REQUIRED")
    for t in (et(2026, 11, 27, 13, 1), et(2026, 11, 27, 13, 4), et(2026, 11, 27, 13, 8)):   # G(d) = (13:00, 13:06]; finalized after 13:07
        lab.tick(t)
    row = lab.session("2026-11-27")
    assert (row["session_status"], row["notes"]) == ("FAILED", ["UNRESOLVED_RECONCILIATION"]) and row["assessment"]["samples"]["in_window"] == 0


def test_TS45c_an_unreadable_startup_lookup_never_hides_a_restart_that_straddles_the_close(lab):
    """D-3 R-4 (B-25): P1 stops at 15:57 and P2's first sample is 16:00:30. With autorun_events readable, P2's STARTUP at 15:59 puts the restart
    in the session. With the table unreadable the restart time is unknown: it is counted at W.close and the source is unavailable - never HEALTHY."""
    day(lab, O, et(2026, 10, 21, 15, 57), owner="6000:aaaaaaaa")
    S2B.add_startup(lab.db, et(2026, 10, 21, 15, 59), 7001, D)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 16, 0, 30), lab.owner)])
    st, notes, a = classify(lab)
    assert (st, notes, a["restarts"]) == ("DEGRADED", ["RESTART_DURING_SESSION"], [iso(et(2026, 10, 21, 15, 59))])
    lab.sql("DROP TABLE autorun_events")
    st, notes, a = classify(lab)
    assert st == "DEGRADED" and {"RESTART_DURING_SESSION", "EVIDENCE_UNREADABLE"} <= set(notes) and "autorun_events" in a["unavailable_sources"]
    assert a["restarts"] == [iso(CL)]


@pytest.mark.parametrize("case", ["no_startup", "startup_after_close", "change_after_close", "change_in_grace"])
def test_TS45d_an_unknown_restart_time_that_straddles_the_close_is_counted(lab, case):
    """D-3 R-4 (B-25): with the autorun table readable but no matching STARTUP (a manual start or a takeover), an owner change from 15:57 to
    16:00:30 has an unknown restart time: it is counted at W.close, with no source unavailable. A matching STARTUP after the close (16:00:10)
    is the restart time, and the day stays HEALTHY. An unmatched owner change entirely after the close (16:20 -> 17:00) is not counted."""
    if case in ("change_after_close", "change_in_grace"):                              # P1 last sampled after the close (16:20, or 16:03 inside G(d))
        day(lab, O, et(2026, 10, 21, 16, 20) if case == "change_after_close" else et(2026, 10, 21, 16, 3), owner="6000:aaaaaaaa")
        S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 17, 0) if case == "change_after_close" else et(2026, 10, 21, 16, 30), lab.owner)])
        st, notes, a = classify(lab, trig=et(2026, 10, 21, 17, 10, 30))
        assert (st, notes, a["restarts"]) == ("HEALTHY", [], [])
        return
    day(lab, O, et(2026, 10, 21, 15, 57), owner="6000:aaaaaaaa")
    if case == "startup_after_close":
        S2B.add_startup(lab.db, et(2026, 10, 21, 16, 0, 10), 7001, D)
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 21, 16, 0, 30), lab.owner)])
    st, notes, a = classify(lab)
    if case == "startup_after_close":
        assert (st, notes, a["restarts"]) == ("HEALTHY", [], [])
    else:
        assert (st, notes, a["restarts"], a["unavailable_sources"]) == ("DEGRADED", ["RESTART_DURING_SESSION"], [iso(CL)], {})


def test_TS45e_a_transient_startup_lookup_failure_at_the_close_is_retried_not_frozen(lab, monkeypatch):
    """D-3 R-4 under B-44 (round-2 review F-3): a transient error of the STARTUP lookup, for a change that straddles the close, is contained
    and retried; the day is not frozen DEGRADED by it. The next confirmed tick reads the STARTUP (after the close) and finalizes HEALTHY."""
    from forward_validation import store as FS
    day(lab, O, et(2026, 10, 21, 15, 57), owner="6000:aaaaaaaa")
    S2B.add_startup(lab.db, et(2026, 10, 21, 16, 0, 10), 7001, D)
    real = FS.ForwardValidationStore.query_strict
    state = {"fired": False}

    def flaky(self, sql, args=()):
        if not state["fired"] and "FROM autorun_events" in sql:
            state["fired"] = True
            raise sqlite3.OperationalError("database is locked")
        return real(self, sql, args)
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict", flaky)
    post_close(lab, [CL + timedelta(seconds=s) for s in (30, 330, 630)])
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ"]
    assert state["fired"] and lab.session(D)["session_status"] == "OPEN"
    assert [(e["trading_date"], e["detail"]["source"], e["detail"]["error"]) for e in errs] == [(D, "autorun_events", "OperationalError")]
    lab.tick(CL + timedelta(seconds=930))
    row = lab.session(D)
    assert (row["session_status"], row["notes"]) == ("HEALTHY", []) and row["assessment"]["restarts"] == []


def test_TS55_x_a_date_whose_classification_raises_never_stalls_the_other_dates_of_the_tick(lab):
    """Round-2 review F-1 (B-44 row 3): date 10-19's only sample carries a malformed count, so its classification raises. It is contained per
    date: 10-20's FAILED evidence is accounted in the same tick, and 10-19 escalates fail-closed after the bound - never NOT_RUN."""
    seed_row(lab, "2026-10-16")
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 19, 16, 2), "6000:aaaaaaaa", rr_intents="not-a-count")])
    S2B.add_reconciliation(lab.db, et(2026, 10, 20, 11, 0), "MULTIPLE_ORDERS")
    lab.run([et(2026, 10, 21, 9, 0), et(2026, 10, 21, 9, 1), et(2026, 10, 21, 9, 2)])
    row = lab.session("2026-10-20")
    assert (row["session_status"], row["notes"]) == ("FAILED", ["DUPLICATE_BROKER_ORDER"])   # the same tick, not stalled behind 10-19
    assert lab.session("2026-10-19") is None and [e["detail"]["phase"] for e in lab.events("OBSERVER_ERROR") if e["trading_date"] == "2026-10-19"] == ["accounting"]
    lab.run([et(2026, 10, 21, 9, 3), et(2026, 10, 21, 9, 4)])
    row = lab.session("2026-10-19")
    assert (row["session_status"], row["notes"]) == ("DEGRADED", ["EVIDENCE_UNREADABLE"])
    assert not [e for e in lab.events("SESSION_NOT_RUN") if e["trading_date"] == "2026-10-19"]


def test_TS55_xi_an_observed_day_whose_classification_keeps_raising_keeps_its_row_and_start(lab):
    """Round-2 follow-up N-1 (B-44 rows 3 and 5): on a fresh database, an observed day whose last in-window sample carries a malformed count
    cannot be classified. After the bound it is still accounted from its samples - opened with its real start and finalized fail-closed."""
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(t, "6000:aaaaaaaa", **({"rr_intents": "not-a-count"} if t == et(2026, 10, 20, 16, 0) else {}))
                              for t in S2B.minutes(et(2026, 10, 20, 9, 30), et(2026, 10, 20, 16, 0))])
    lab.run([et(2026, 10, 21, 9, 0) + timedelta(minutes=k) for k in range(5)])        # confirmed from 09:02: three failing evaluations, then escalated
    row = lab.session("2026-10-20")
    assert row is not None and (row["session_status"], row["notes"]) == ("DEGRADED", ["EVIDENCE_UNREADABLE"])
    assert row["started_at"] == iso(et(2026, 10, 20, 9, 30)) and not lab.events("SESSION_NOT_RUN")


def test_TS55_xii_a_malformed_candidate_date_is_skipped_and_the_other_dates_proceed(lab):
    """Round-2 follow-up (B-44 row 3): a sample stored with a malformed trading_date makes that candidate unreadable as a date; it is skipped
    per date and the other candidates of the same tick are still accounted."""
    seed_row(lab, "2026-10-16")
    S2B.bulk_samples(lab.db, [S2B.healthy_sample(et(2026, 10, 19, 12, 0), "6000:aaaaaaaa", trading_date="2026-02-30")])   # sorts before today: reaches the calendar
    S2B.add_reconciliation(lab.db, et(2026, 10, 20, 11, 0), "MULTIPLE_ORDERS")
    lab.run([et(2026, 10, 21, 9, 0), et(2026, 10, 21, 9, 1), et(2026, 10, 21, 9, 2)])
    row = lab.session("2026-10-20")
    assert (row["session_status"], row["notes"]) == ("FAILED", ["DUPLICATE_BROKER_ORDER"])
    assert lab.session("2026-02-30") is None


# ================================================================ operator round 3: R-1 (classification attempts) and D-5 (fail-closed acceptance) ====
R1_FAIL = "FROM execution_orders WHERE first_seen_at"                                  # the duplicate-order read: the source of the FAILED evidence below


def _r1_phases(lab, d):
    return [(e["detail"] or {}).get("phase") for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ" and e["trading_date"] == d]


def _r1_reads(monkeypatch, state, scan=False):
    """query_strict fails while state["fail"] for the duplicate-order read (every such read of that tick), and while state["scan"] for the
    accounting scan's first query."""
    from forward_validation import store as FS
    real = FS.ForwardValidationStore.query_strict

    def q(self, sql, args=()):
        if (state.get("fail") and R1_FAIL in sql) or (state.get("scan") and "SELECT MAX(trading_date) AS d FROM forward_validation_sessions" in sql):
            state["n"] = state.get("n", 0) + 1
            raise sqlite3.OperationalError("database is locked")
        return real(self, sql, args)
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict", q)


def _r1_duplicates(lab, d_open=O):
    S2B.add_order(lab.db, "c1", d_open + timedelta(minutes=90))
    S2B.add_order(lab.db, "c1", d_open + timedelta(minutes=91))                         # confirmed FAILED evidence: DUPLICATE_BROKER_ORDER


def test_TS55_xiii_two_unrelated_failures_never_escalate_a_transient_classification_failure_and_failed_stays_failed(lab, monkeypatch):
    """R-1 (operator round 3; B-44 row 5): 10-21 already holds two EVIDENCE_READ events of unrelated phases (the confirmation and the display
    counters). Its first classification attempt (the accounting pass) fails - every read of the duplicate-order source fails for that whole
    tick - and counts alone (1 < 3): no escalation, the date is retried, the source recovers and the confirmed FAILED evidence is FAILED."""
    day(lab, O, et(2026, 10, 21, 15, 0))                                              # observed, then the bot stopped: no row, accounted late
    _r1_duplicates(lab)
    for at, phase in ((et(2026, 10, 21, 12, 0), "confirm"), (et(2026, 10, 21, 12, 1), "counters")):
        S2B.add_event(lab.db, at, "OBSERVER_ERROR", "EVIDENCE_READ", {"phase": phase, "source": None, "error": "OperationalError", "text": "database is locked"},
                      trading_date=D, owner=lab.owner)
    state = {}
    _r1_reads(monkeypatch, state)
    lab.run([et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5)])                         # not yet confirmed
    state["fail"] = True
    o = lab.tick(et(2026, 10, 22, 9, 10))                                             # confirmed: the first classification attempt of 10-21 fails
    state["fail"] = False
    assert state["n"] >= 1 and o["result"] == "OK" and lab.session(D) is None and not lab.events("SESSION_NOT_RUN")   # retried: never escalated, never NOT_RUN
    assert _r1_phases(lab, D) == ["confirm", "counters", "accounting"] and lab.fv._evidence_read_failed(None, D, None, None) == 1
    lab.tick(et(2026, 10, 22, 9, 15))                                                 # the transient failure recovers
    row = lab.session(D)
    assert (row["session_status"], row["notes"]) == ("FAILED", ["DUPLICATE_BROKER_ORDER"]) and not row["assessment"].get("unavailable_sources")


def test_TS55_xiv_accounting_scan_failures_are_not_classification_attempts_of_the_date(lab, monkeypatch):
    """R-1: the accounting scan fails at two confirmed ticks of 10-21 (recorded under today, phase accounting_scan, renamed from 'accounting').
    They are not classification attempts of 10-21: its late finalize the next morning fails for one whole tick, counts 1 and is retried, and
    the confirmed FAILED evidence is FAILED - never downgraded to EVIDENCE_UNREADABLE by the scan's counters."""
    day(lab)
    _r1_duplicates(lab)
    state = {"scan": True}
    _r1_reads(monkeypatch, state)
    lab.run([CL + timedelta(seconds=30), CL + timedelta(seconds=330)])                # before C + h: no on-time finalize
    state["scan"] = False
    assert lab.session(D)["session_status"] == "OPEN" and _r1_phases(lab, D) == ["accounting_scan", "accounting_scan"]
    assert lab.fv._evidence_read_failed(None, D, None, None) == 0
    lab.run([et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5)])
    state["fail"] = True
    lab.tick(et(2026, 10, 22, 9, 10))                                                 # confirmed: the late finalize of 10-21 fails for the whole tick
    state["fail"] = False
    assert lab.session(D)["session_status"] == "OPEN" and _r1_phases(lab, D) == ["accounting_scan", "accounting_scan", "finalize"]
    assert lab.fv._evidence_read_failed(None, D, None, None) == 1
    lab.tick(et(2026, 10, 22, 9, 15))
    row = lab.session(D)
    assert (row["session_status"], row["notes"]) == ("FAILED", ["DUPLICATE_BROKER_ORDER"]) and row["assessment"]["finalized_late"] is True


def test_TS55_xv_attempts_count_once_per_tick_and_persistent_unreadable_evidence_is_degraded_never_ready(lab, monkeypatch):
    """R-1: an observed day accounted late whose classification read fails at every tick. In its first confirmed tick it fails twice (the
    accounting pass opens and finalizes it, then the late finalize of OPEN rows retries it): ONE attempt. The second tick is attempt 2 (still
    OPEN); the third reaches the bound and the date is DEGRADED EVIDENCE_UNREADABLE with attempts 3 - never FAILED, never NOT_RUN - and (D-5)
    it never counts toward the session target or readiness."""
    from forward_validation import service as SV
    from forward_validation import store as FS
    day(lab, O, et(2026, 10, 21, 15, 0))
    real_q, real_fin = FS.ForwardValidationStore.query_strict, SV.ForwardValidationService._finalize
    state = {"in": False}

    def fin(self, *a, **k):
        state["in"] = True
        try:
            return real_fin(self, *a, **k)
        finally:
            state["in"] = False

    def q(self, sql, args=()):
        if state["in"] and R1_FAIL in sql:
            raise sqlite3.OperationalError("database is locked")
        return real_q(self, sql, args)
    monkeypatch.setattr(SV.ForwardValidationService, "_finalize", fin)
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict", q)
    t1 = et(2026, 10, 22, 9, 10)
    lab.run([et(2026, 10, 22, 9, 0), et(2026, 10, 22, 9, 5), t1])
    errs = [e for e in lab.events("OBSERVER_ERROR") if e["code"] == "EVIDENCE_READ" and e["trading_date"] == D]
    assert [(e["detail"]["phase"], e["at"]) for e in errs] == [("finalize", iso(t1)), ("finalize", iso(t1))]   # two failures in one tick
    assert lab.session(D)["session_status"] == "OPEN" and lab.fv._evidence_read_failed(None, D, None, None) == 1   # ... one attempt
    lab.tick(et(2026, 10, 22, 9, 15))
    assert lab.session(D)["session_status"] == "OPEN" and lab.fv._evidence_read_failed(None, D, None, None) == 2   # under the bound: retried
    lab.tick(et(2026, 10, 22, 9, 20))
    row = lab.session(D)
    assert row["session_status"] == "DEGRADED" and "EVIDENCE_UNREADABLE" in row["notes"] and row["started_at"] == iso(O)
    assert {k: v["attempts"] for k, v in row["assessment"]["unavailable_sources"].items()} == {"execution_orders+execution_reconciliations": 3}
    assert not lab.events("SESSION_NOT_RUN") and "DUPLICATE_BROKER_ORDER" not in row["notes"]
    cum = lab.fv.cumulative(et(2026, 10, 22, 9, 21))
    assert (cum["sessions"]["unreadable"], cum["sessions"]["degraded"], cum["sessions"]["completed"], cum["sessions"]["label"]) == (1, 1, 0, "0 / 30")
    assert cum["operational"]["status"] == "NOT READY"


def test_TS55_xvi_the_attempt_count_reads_only_the_dates_classification_phases_one_per_tick(lab, monkeypatch):
    """R-1, the count itself: distinct ticks of the date's finalize / accounting EVIDENCE_READ events. Every other phase, the escalated reads,
    another date, another code and an unreadable detail (malformed, or nested beyond the parser) are not attempts and never raise; a call with
    an exception records one event and counts it. An unreadable count is 0 for the escalation decision and None (never 0) for B-37."""
    def ev(at, phase, d=D, code="EVIDENCE_READ"):
        S2B.add_event(lab.db, at, "OBSERVER_ERROR", code, {"phase": phase, "source": None, "error": "OperationalError", "text": "x"}, trading_date=d, owner=lab.owner)
    for k, phase in enumerate(("confirm", "counters", "status", "open", "open_sessions", "accounting_scan", "accounting_pass", "account_open", "backfill", "clock_check")):
        ev(et(2026, 10, 21, 12, k), phase)
    t1, t2, t3 = et(2026, 10, 22, 9, 10), et(2026, 10, 22, 9, 15), et(2026, 10, 22, 9, 20)
    ev(t1, "accounting")
    ev(t1, "finalize")                                                                # the same tick: one attempt
    ev(t2, "finalize")
    ev(t3, "finalize_escalated")
    ev(t3, "accounting_escalated")
    ev(et(2026, 10, 22, 9, 25), "finalize", d="2026-10-20")
    ev(et(2026, 10, 22, 9, 30), "finalize", code="OTHER")
    lab.sql("INSERT INTO forward_validation_events (event_id, at, trading_date, kind, code, detail_json, owner) VALUES ('bad', ?, ?, 'OBSERVER_ERROR', 'EVIDENCE_READ', "
            "'not json', 'x')", (iso(et(2026, 10, 22, 9, 35)), D))
    assert lab.fv._evidence_read_failed(None, D, None, None) == 2
    n0 = len(lab.events("OBSERVER_ERROR"))
    assert lab.fv._evidence_read_failed(et(2026, 10, 22, 9, 40), D, "finalize", sqlite3.OperationalError("database is locked")) == 3
    assert len(lab.events("OBSERVER_ERROR")) == n0 + 1 and _r1_phases(lab, D)[-1] == "finalize"
    assert lab.fv._evidence_read_failed(et(2026, 10, 22, 9, 40), D, "counters", sqlite3.OperationalError("x")) == 3   # recorded, not an attempt
    assert lab.fv._evidence_read_failed(None, "2026-10-20", None, None) == 1 and lab.fv._evidence_read_failed(None, "2026-10-19", None, None) == 0
    lab.sql("INSERT INTO forward_validation_events (event_id, at, trading_date, kind, code, detail_json, owner) VALUES ('deep', ?, ?, 'OBSERVER_ERROR', 'EVIDENCE_READ', "
            "?, 'x')", (iso(et(2026, 10, 22, 9, 50)), D, "[" * 200000 + "]" * 200000))   # json.loads raises RecursionError, not ValueError (append-only: last)
    assert lab.fv._evidence_read_failed(None, D, None, None) == 3                       # not an attempt, and nothing escapes
    from forward_validation import store as FS
    real = FS.ForwardValidationStore.query_strict
    monkeypatch.setattr(FS.ForwardValidationStore, "query_strict",
                        lambda self, sql, args=(): (_ for _ in ()).throw(sqlite3.OperationalError("locked")) if "FROM forward_validation_events" in sql else real(self, sql, args))
    assert lab.fv._evidence_read_failed(None, D, None, None) is None                    # B-37: unknown, never 0
    assert lab.fv._evidence_read_failed(et(2026, 10, 22, 9, 45), D, "finalize", sqlite3.OperationalError("x")) == 0   # no escalation on an unknown count


def _d5_seed(lab, d, status, notes=()):
    lab.store.open_session(d, et(2026, 1, 1), "seed", in_session=True)
    lab.store.update_session(d, et(2026, 1, 1), session_status=status, notes_code="|".join(notes))


def test_TS68_ii_d5_unreadable_days_never_count_toward_the_target_or_readiness(tmp_path, monkeypatch):
    """D-5 (operator round 3): a DEGRADED day whose notes hold EVIDENCE_UNREADABLE is never acceptable evidence - it is not a completed session
    (the target, its label and the strategy registry's sessions_completed read `completed`) and operational readiness is NOT READY while
    one exists. It stays DEGRADED (never relabelled FAILED); other DEGRADED and FAILED days keep their merged meaning."""
    def cum(seeds, target=3):
        lab = S2B.Lab(tmp_path / str(len(list(tmp_path.iterdir()))), monkeypatch)
        for d, status, notes in seeds:
            _d5_seed(lab, d, status, notes)
        lab.fv.target_sessions = target
        c = lab.fv.cumulative(et(2026, 10, 23, 12, 0))
        return c["sessions"], c["operational"]["status"], c["strategy"]["edge_status"]
    H3 = [("2026-10-19", "HEALTHY", ()), ("2026-10-20", "HEALTHY", ()), ("2026-10-21", "HEALTHY", ())]
    s, status, edge = cum(H3)                                                         # positive: three healthy days meet the target, READY
    assert (s["completed"], s["healthy"], s["unreadable"], s["target_met"], s["label"], status) == (3, 3, 0, True, "3 / 3", "READY")
    s, status, edge2 = cum(H3[:2] + [("2026-10-21", "DEGRADED", ("EVIDENCE_UNREADABLE",))])   # negative: the unreadable day never qualifies
    assert (s["completed"], s["healthy"], s["degraded"], s["failed"], s["unreadable"], s["target_met"], s["label"], status) == (2, 2, 1, 0, 1, False, "2 / 3", "NOT READY")
    assert edge2 == edge                                                              # the edge status reads HEALTHY sessions only: unchanged
    s, status, _ = cum(H3[:2] + [("2026-10-21", "DEGRADED", ("COVERAGE_LOW", "EVIDENCE_UNREADABLE"))])   # mixed codes: still unreadable
    assert (s["completed"], s["unreadable"], status) == (2, 1, "NOT READY")
    s, status, _ = cum(H3[:2] + [("2026-10-21", "DEGRADED", ("COVERAGE_LOW",))])      # regression: an ordinary DEGRADED day is unchanged
    assert (s["completed"], s["degraded"], s["unreadable"], s["target_met"], status) == (3, 1, 0, True, "READY")
    s, status, _ = cum(H3[:2] + [("2026-10-21", "FAILED", ("DUPLICATE_BROKER_ORDER",))])   # regression: FAILED is completed and NOT READY, as merged
    assert (s["completed"], s["failed"], s["unreadable"], status) == (3, 1, 0, "NOT READY")
    s, status, _ = cum(H3 + [("2026-10-22", "DEGRADED", ("EVIDENCE_UNREADABLE",)), ("2026-10-23", "NOT_RUN", ("NO_OBSERVATION",))])   # mixed healthy + unreadable
    assert (s["completed"], s["healthy"], s["degraded"], s["unreadable"], s["not_run"], s["target_met"], status) == (3, 3, 1, 1, 1, True, "NOT READY")


def test_TS68_i_d5_an_unreadable_session_record_is_invalid_610a_evidence_never_a_failed_record(tmp_path, monkeypatch):
    """D-5 in the 6.10A evidence record (forward_capture/health.compute): EVIDENCE_UNREADABLE is INVALID with its own reason, never
    FV_SESSION_FAILED; a plain DEGRADED record and a FAILED record keep their merged reasons; the healthy day stays HEALTHY."""
    from autorun.service import STARTUP
    from forward_capture import health as HL
    from test_forward_paper_610a import DAY, observed_day, rig
    for k, v in {"pending": False, "code": None, "text": None, "attempts": 0, "demoted": None, "at": None, "components_started": True, "passive": False,
                 "lease_taken": False, "owned_since": None}.items():
        monkeypatch.setitem(STARTUP, k, v)                                            # test_forward_paper_610a's autouse fresh_startup_state
    r = rig(tmp_path, monkeypatch)
    observed_day(r, monkeypatch)
    real = r.fv.store.session

    def as_(status, notes):
        monkeypatch.setattr(r.fv.store, "session", lambda d: dict(real(d), session_status=status, notes_code="|".join(notes), notes=list(notes)))
        return HL.compute(r.cap, DAY, r.clock.now)
    assert as_("HEALTHY", [])["status"] == "HEALTHY"                                    # positive
    rec = as_("DEGRADED", ["EVIDENCE_UNREADABLE"])
    assert rec["status"] == "INVALID" and rec["reasons"] == ["FV_SESSION_EVIDENCE_UNREADABLE", "FV_SESSION_DEGRADED"] and "FV_SESSION_FAILED" not in rec["reasons"]
    assert "evidence was unreadable (EVIDENCE_UNREADABLE)" in rec["summary_text"] and rec["source_session_status"] == "DEGRADED"
    rec = as_("DEGRADED", ["COVERAGE_LOW", "EVIDENCE_UNREADABLE"])
    assert rec["status"] == "INVALID" and "FV_SESSION_EVIDENCE_UNREADABLE" in rec["reasons"]
    assert as_("DEGRADED", ["COVERAGE_LOW"])["reasons"] == ["FV_SESSION_DEGRADED"]       # regression: unchanged
    rec = as_("FAILED", ["DUPLICATE_BROKER_ORDER"])
    assert rec["status"] == "INVALID" and "FV_SESSION_FAILED" in rec["reasons"] and "FV_SESSION_EVIDENCE_UNREADABLE" not in rec["reasons"]
