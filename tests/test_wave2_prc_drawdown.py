"""
Wave 2 PR C - the bot-only drawdown and the fill time (WAVE2_PRC_DESIGN_REV4.md Revision 4.3 sections 9 and 10, F-05, P-02, semantic 1,
D-W2-i; WAVE2_PRC_TEST_PLAN_REV4.md sections 3.6 and 3.7: T-T01 ... T-T09, T-D01 ... T-D24). Synthetic data only: tmp_path databases, explicit
clocks, hand-computed expectations.
"""
import json
import sqlite3
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

import wave2_prb_support as B
import wave2_prc_support as C
from wave2_prc_support import et, iso
from test_wave2_prc_evidence import at, evening, mdays, rig

D1 = "2026-11-10"


def dd(r, now):
    return r.fv._run_block(now)["drawdown"]


def stale_marks(a, b, refresh_s, px="10.00"):
    """An observer mark every 60 s from a to b whose last_price_at only advances every `refresh_s` (the executor's refresh)."""
    return [(t, px, a + timedelta(seconds=refresh_s * int((t - a).total_seconds() // refresh_s))) for t in B.minutes(a, b)]


def add_event(db, execution_id, at_, filled_qty, price, raw=None, status="filled"):
    C.sql(db, "INSERT INTO execution_order_events (event_id, broker_order_id, execution_id, at, status, filled_qty, filled_avg_price, source, raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
          (uuid.uuid4().hex, "b-" + execution_id[:8], execution_id, iso(at_), status, filled_qty, price, "poll", json.dumps(raw if raw is not None else {"filled_at": iso(at_)})))


def add_pnl_event(db, position_id, at_, snap, kind="PARTIAL_EXIT"):
    C.sql(db, "INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
          (uuid.uuid4().hex, position_id, iso(at_), kind, "HEALTHY", "HEALTHY", "test", json.dumps({"pnl_snapshot": snap})))


# ================================================================ 3.7 drawdown (C-40 ... C-46) ===========================================

@pytest.mark.parametrize("equity", [("100000.00", "105000.00", "105000.00"), ("100000.00", "97000.00", "97000.00"), ("100000.00", "98000.00", "98000.00")],
                         ids=["TD01_deposit", "TD02_external_loss", "TD03_withdrawal"])
def test_TD01_TD02_TD03_funding_and_external_positions_never_move_the_bot_curve(tmp_path, monkeypatch, equity):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    B.bulk_samples(r.db, [C.stamped(at(D1, 16, 30) + timedelta(minutes=i), r.owner, run["run_id"], run["frozen_hash"], equity=e, in_session=False) for i, e in enumerate(equity)])
    d = dd(r, evening(D1))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.00")


def test_TD04_a_flat_bot_trade_is_zero_whatever_the_account_does(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    C.trade(r.db, entry_at=at(D1, 10), exit_at=at(D1, 11), exit_px="10.00", realized="0.00", marks=C.fresh_marks(at(D1, 10), at(D1, 11), px="10.00"))
    B.bulk_samples(r.db, [C.stamped(at(D1, 16, 30), r.owner, run["run_id"], run["frozen_hash"], equity="90000.00", in_session=False)])
    d = dd(r, evening(D1))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.00")


def test_TD05_a_marked_down_then_closed_loss_is_exactly_its_percentage(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    C.trade(r.db, entry_at=at(D1, 10), exit_at=at(D1, 11), qty=1000, entry_px="10.00", exit_px="7.00", marks=C.fresh_marks(at(D1, 10), at(D1, 11), px="7.00"))
    d = dd(r, evening(D1))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "3.00")


def test_TD06_a_loss_of_another_cohort_before_the_run_is_not_the_runs(tmp_path, monkeypatch):
    from forward_capture.store import ForwardCaptureStore
    from forward_capture.view import evidence
    r = C.RunLab(tmp_path, monkeypatch)
    fc = ForwardCaptureStore(r.db)
    fc.put_cohort("FP-cohortA00001", "a" * 64, iso(et(2026, 11, 2, 9)), "2026-11-02", {})
    fc.put_cohort(C.COHORT, "k" * 64, iso(START_B - timedelta(days=1)), "2026-11-09", {})
    C.put_session(r.db, "2026-11-06", "HEALTHY")
    C.put_health(r.db, "2026-11-06", cohort_ids=("FP-cohortA00001",))
    C.trade(r.db, entry_at=et(2026, 11, 6, 10), exit_at=et(2026, 11, 6, 11), qty=15000, entry_px="10.00", exit_px="9.00", snapshot_cohort="FP-cohortA00001")   # -15 %
    r.ready(START_B)
    run = r.start(START_B)["run"]
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    d = dd(r, evening(D1))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.00")
    a = next(c for c in evidence(fc, r.fv, evening(D1))["cohorts"] if c["cohort_id"] == "FP-cohortA00001")
    assert a["reasons"] == ["not a forward run"] and a["expectancy"] is None and a["forward_run"] is False


START_B = et(2026, 11, 10, 9, 0)


def _partial_exit_position(r, qty=1000, part=500, entry_px="10.00", exit_px="9.00", mark_px="9.00"):
    """A bot position: entry `qty` @ entry_px at 10:00; an exit order filling `part` @ exit_px at 11:00 (realized recorded), the rest still open."""
    pid, xid, eid = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    a, b = at(D1, 10), at(D1, 11)
    realized = f"{(Decimal(exit_px) - Decimal(entry_px)) * part:.2f}"
    C.sql(r.db, "INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, position_id, state, filled_qty, filled_avg_price, attempt_count, created_at, "
                "submitted_at, resolved_at, updated_at) VALUES (?, 'ENTRY', 'SNDK', 'buy', ?, ?, ?, 'FILLED', ?, ?, 1, ?, ?, ?, ?)",
          (xid, qty, f"sa-{xid[:12]}", pid, qty, entry_px, iso(a - timedelta(seconds=30)), iso(a - timedelta(seconds=30)), iso(a), iso(a)))
    add_event(r.db, xid, a, qty, entry_px)
    C.sql(r.db, "INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, position_id, exit_cycle, state, filled_qty, filled_avg_price, attempt_count, "
                "created_at, submitted_at, resolved_at, updated_at) VALUES (?, 'EXIT', 'SNDK', 'sell', ?, ?, ?, 1, 'FILLED', ?, ?, 1, ?, ?, ?, ?)",
          (eid, part, f"sa-{eid[:12]}", pid, part, exit_px, iso(b - timedelta(seconds=30)), iso(b - timedelta(seconds=30)), iso(b), iso(b)))
    add_event(r.db, eid, b, part, exit_px)
    add_pnl_event(r.db, pid, a, {"entry_qty": qty, "average_entry_price": entry_px, "exit_qty": 0, "average_exit_price": None, "realized_pnl": "0.00"}, kind="OPENED")
    add_pnl_event(r.db, pid, b, {"entry_qty": qty, "average_entry_price": entry_px, "exit_qty": part, "average_exit_price": exit_px, "realized_pnl": realized})
    C.sql(r.db, "INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, exit_qty, average_exit_price, realized_pnl, "
                "updated_at) VALUES (?, ?, 'SNDK', ?, ?, ?, ?, 'HEALTHY', ?, ?, ?, ?)", (pid, xid, qty - part, qty, entry_px, iso(a), part, exit_px, realized, iso(b)))
    C.sql(r.db, "INSERT INTO forward_capture_entry_snapshots (execution_id, candidate_id, decision_id, cohort_id, captured_at, snapshot_json, snapshot_sha256) VALUES (?,?,?,?,?,?,?)",
          (xid, None, None, C.COHORT, iso(a), "{}", "s" * 64))
    conn = sqlite3.connect(str(r.db))
    conn.executemany("INSERT INTO forward_validation_marks (mark_id, at, position_id, symbol, state, quantity, last_price, last_price_at) VALUES (?,?,?,?,?,?,?,?)",
                     [(uuid.uuid4().hex, iso(t), pid, "SNDK", "HEALTHY", qty, mark_px, iso(t)) for t in B.minutes(a, at(D1, 15))])
    conn.commit()
    conn.close()
    return pid


def test_TD07_a_partial_exit_realized_while_the_rest_is_open_is_included(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.day_samples(r.db, D1, r.owner, run["run_id"], run["frozen_hash"], end=at(D1, 15), mode="PAPER_AUTO")
    pid = _partial_exit_position(r)
    b = r.fv._run_block(at(D1, 15))
    t = next(x for x in b["trades"]["rows"] if x["position_id"] == pid)
    assert t["provisional"] is True
    assert (b["drawdown"]["status"], b["drawdown"]["max_drawdown_pct"]) == ("KNOWN", "1.00")      # -500 realized + -500 unrealized on 100,000


def test_TD09_a_hand_computed_three_trade_curve(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    for (h, px) in ((10, "11.00"), (11, "7.00"), (13, "10.50")):                  # +100.00, -300.00, +50.00 on 100 shares; marked at the entry until the exit
        C.trade(r.db, entry_at=at(D1, h), exit_at=at(D1, h, 30), qty=100, entry_px="10.00", exit_px=px, marks=C.fresh_marks(at(D1, h), at(D1, h, 29), px="10.00"))
    d = dd(r, evening(D1))
    exp = (Decimal("100100") - Decimal("99800")) / Decimal("100100") * 100
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", format(exp.quantize(Decimal("0.01")), "f")) == ("KNOWN", "0.30")


def test_TD10_TD11_the_account_curve_is_informational_and_flat_days_after_a_close_stay_known(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    days = mdays(D1, 3)
    for d in days:
        C.member_day(r, d, run, mode="PAPER_AUTO")
    C.trade(r.db, entry_at=at(days[0], 10), exit_at=at(days[0], 10, 30), marks=C.fresh_marks(at(days[0], 10), at(days[0], 10, 30), px="11.00"))
    cum = r.fv.cumulative(evening(days[-1]))
    assert cum["strategy"]["drawdown_basis"] == "account curve (informational; never used by the edge rule)"
    d = cum["run"]["drawdown"]
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.00") and d["basis"].startswith("bot-only")


def test_TD12_the_percentage_is_computed_directly_not_from_the_largest_absolute_drawdown():
    from forward_validation import metrics as M
    assert M.bot_drawdown_pct(Decimal(100), [Decimal(v) for v in (100, 90, 200, 189)]) == "10.00"
    assert M.drawdown([(str(i), v) for i, v in enumerate((100, 90, 200, 189))])["max_drawdown_pct"] == "5.50"     # why metrics.drawdown is never reused
    assert M.bot_drawdown_pct(Decimal(100), [Decimal(100), None]) is None and M.bot_drawdown_pct(Decimal(0), [Decimal(1)]) is None


def test_TD13_a_partial_entry_has_no_look_ahead(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    t1, t2 = at(D1, 10), at(D1, 10, 30)
    t = C.trade(r.db, entry_at=t2, exit_at=at(D1, 11), qty=1000, entry_px="10.30", exit_px="10.30", realized="0.00",
                marks=C.fresh_marks(t1, t2 - timedelta(minutes=1), px="9.00") + C.fresh_marks(t2, at(D1, 11), px="10.30"), filled_at=t2)
    add_event(r.db, t["execution_id"], t1, 400, "10.00", raw={"filled_at": iso(t1)})                  # 400 @ 10.00 first, observed at t1
    d = dd(r, evening(D1))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.40")              # (9.00 - 10.00) x 400; look-ahead to 10.30 would read 0.52


def test_TD14_a_null_realized_snapshot_is_an_invalid_point(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    t = C.trade(r.db, entry_at=at(D1, 10), exit_at=at(D1, 11), marks=C.fresh_marks(at(D1, 10), at(D1, 11)))
    add_pnl_event(r.db, t["position_id"], at(D1, 10, 20), {"entry_qty": 10, "average_entry_price": "10.00", "exit_qty": 0, "realized_pnl": None})
    d = dd(r, evening(D1))
    assert d["status"] == "UNKNOWN" and d["max_drawdown_pct"] is None and "NULL" in " ".join(d["reasons"])


def test_TD15_an_open_position_with_an_unpriced_entry_event_is_invalid(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    t = C.trade(r.db, entry_at=at(D1, 10), exit_at=at(D1, 11), marks=C.fresh_marks(at(D1, 10), at(D1, 11)))
    C.sql(r.db, "DROP TRIGGER IF EXISTS execution_order_events_no_update")
    C.sql(r.db, "UPDATE execution_order_events SET filled_avg_price = NULL WHERE execution_id = ?", (t["execution_id"],))
    d = dd(r, evening(D1))
    assert d["status"] == "UNKNOWN" and d["max_drawdown_pct"] is None


def test_TD16_a_hold_over_an_observer_down_day_is_unknown_never_zero(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    mon, tue, wed = mdays(D1, 3)
    C.member_day(r, mon, run, mode="PAPER_AUTO")
    C.member_day(r, tue, run, samples=False, status="NOT_RUN", notes="NO_OBSERVATION", health="INVALID", health_reasons=("FV_SESSION_NOT_RUN",))
    C.member_day(r, wed, run, mode="PAPER_AUTO")
    t = C.trade(r.db, entry_at=at(mon, 14), exit_at=at(wed, 11), qty=12000, entry_px="10.00", exit_px="9.00",
                marks=C.fresh_marks(at(mon, 14), at(mon, 16), px="10.00") + C.fresh_marks(at(wed, 9, 30), at(wed, 11), px="9.00"))   # the true drawdown: 12 %
    b = r.fv._run_block(evening(wed))
    assert b["drawdown"]["status"] == "UNKNOWN" and b["drawdown"]["max_drawdown_pct"] is None and t["position_id"] in b["drawdown"]["stale_positions"]
    assert "price_evidence_gap" in b["trades"]["unverified_detail"][t["position_id"]] and b["strategy"]["edge_status"] != "EARLY_POSITIVE"


def test_TD17_one_gap_over_g_in_a_long_run_is_unknown_whatever_the_fresh_share(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    days = mdays(D1, 30)
    C.member_day(r, days[0], run, mode="PAPER_AUTO")
    a, b = at(days[0], 9, 30), at(days[0], 16)
    marks = [m for m in C.fresh_marks(a, b) if not (at(days[0], 12) < m[0] < at(days[0], 12, 6, 1))] + [(at(days[0], 12, 6, 1), "10.00", at(days[0], 12, 6, 1))]
    C.trade(r.db, entry_at=a, exit_at=b, exit_px="10.00", realized="0.00", marks=marks, exit_filled_at=b - timedelta(seconds=1))
    d = dd(r, evening(days[-1]))
    assert d["status"] == "UNKNOWN" and d["fresh_share"] > 0.99


@pytest.mark.parametrize("spacing, known", [(360, True), (361, False), (400, False)])
def test_TD18_the_gap_rule_is_measured_continuously_and_g_is_counted_once(tmp_path, monkeypatch, spacing, known):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    a, b = at(D1, 10), at(D1, 11)
    C.trade(r.db, entry_at=a, exit_at=b, exit_px="10.00", realized="0.00", marks=stale_marks(a, b, spacing))
    d = dd(r, evening(D1))
    assert (d["status"] == "KNOWN") is known, d


def test_TD18b_no_marks_for_480_s_is_unknown(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    a, b = at(D1, 10), at(D1, 11)
    marks = [m for m in C.fresh_marks(a, b) if not (at(D1, 10, 20) < m[0] < at(D1, 10, 28))]       # 10:20 -> 10:28: 480 s without a mark
    C.trade(r.db, entry_at=a, exit_at=b, exit_px="10.00", realized="0.00", marks=marks)
    assert dd(r, evening(D1))["status"] == "UNKNOWN"


@pytest.mark.parametrize("stale_s", [480, 600, 420], ids=["TD19_exit_in_flight", "TD20_attention", "TD21_no_price"])
def test_TD19_TD20_TD21_marks_keeping_an_old_price_time_are_unknown(tmp_path, monkeypatch, stale_s):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    a, b = at(D1, 10), at(D1, 11)
    s0 = at(D1, 10, 30)
    marks = [(t, "10.00", t if not (s0 <= t < s0 + timedelta(seconds=stale_s)) else s0) for t in B.minutes(a, b)]   # the executor skips the refresh
    t = C.trade(r.db, entry_at=a, exit_at=b, exit_px="10.00", realized="0.00", marks=marks)
    blk = r.fv._run_block(evening(D1))
    assert blk["drawdown"]["status"] == "UNKNOWN" and "price_evidence_gap" in blk["trades"]["unverified_detail"][t["position_id"]]


def test_TD22_one_time_base_the_observation_time(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.member_day(r, D1, run, mode="PAPER_AUTO")
    a = at(D1, 10)
    C.trade(r.db, entry_at=a, exit_at=at(D1, 16, 1), qty=1000, entry_px="10.00", exit_px="11.00", exit_filled_at=at(D1, 15, 59), exit_submitted_at=at(D1, 15, 58),
            marks=C.fresh_marks(a, at(D1, 16), px="11.00"))
    d = dd(r, evening(D1))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.00")              # U = +1000 until the observation, then R = +1000: no dip, no double count


def test_TD23_the_grid_stops_at_now_and_at_an_end(tmp_path, monkeypatch):
    from forward_validation import run_admin
    r, run = rig(tmp_path, monkeypatch)
    C.day_samples(r.db, D1, r.owner, run["run_id"], run["frozen_hash"], end=at(D1, 11), mode="PAPER_AUTO")
    C.trade(r.db, entry_at=at(D1, 10), exit_at=None, marks=C.fresh_marks(at(D1, 10), at(D1, 11), px="9.00"))
    d = dd(r, at(D1, 11))
    assert d["status"] == "KNOWN" and d["max_drawdown_pct"] == "0.01"            # no grid point after now
    r2, run2 = rig(tmp_path / "ended", monkeypatch)
    C.day_samples(r2.db, D1, r2.owner, run2["run_id"], run2["frozen_hash"], mode="PAPER_AUTO")
    C.trade(r2.db, entry_at=at(D1, 10), exit_at=None, qty=1000, marks=C.fresh_marks(at(D1, 10), at(D1, 14), px="9.00"))
    assert run_admin.end(r2.store, at(D1, 14), "test end")["ok"]
    b = r2.fv._run_block(evening(D1))
    assert b["t_stop"] == iso(at(D1, 14)) and (b["drawdown"]["status"], b["drawdown"]["max_drawdown_pct"]) == ("KNOWN", "1.00") and b["trades"]["provisional"] == 1


def test_TD24_the_bot_quantity_is_its_own_fills_and_a_failed_read_is_unknown(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    C.day_samples(r.db, D1, r.owner, run["run_id"], run["frozen_hash"], end=at(D1, 12), mode="PAPER_AUTO")
    t = C.trade(r.db, entry_at=at(D1, 10), exit_at=None, qty=400, marks=C.fresh_marks(at(D1, 10), at(D1, 11), px="9.00"))
    C.sql(r.db, "UPDATE paper_positions SET quantity = 1000, state = 'ATTENTION', attention_reason = 'QTY_MISMATCH' WHERE position_id = ?", (t["position_id"],))   # the broker holds 1000
    d = dd(r, at(D1, 11))
    assert (d["status"], d["max_drawdown_pct"]) == ("KNOWN", "0.40")              # q = 400, the bot's own fills, never 1000
    d = dd(r, at(D1, 12))
    assert d["status"] == "UNKNOWN"                                                # the executor stopped refreshing under ATTENTION: past G
    real = r.store.query_strict

    def flaky(q, args=()):
        if "FROM position_events" in q:
            raise sqlite3.OperationalError("disk I/O error")
        return real(q, args)
    monkeypatch.setattr(r.store, "query_strict", flaky)
    d = dd(r, at(D1, 11))
    assert d["status"] == "UNKNOWN" and d["max_drawdown_pct"] is None and "unreadable" in d["reasons"][0]


# ================================================================ 3.6 fill time (C-60 ... C-63) ===========================================

def fills(r, run, now, pid):
    mem = r.fv._session_membership(run, now, now)
    t = next(x for x in r.fv._trade_membership(run, now, now, mem)["trades"] if x["position_id"] == pid)
    return t["fill_times"], t["hold_days"]


def test_TT01_a_wednesday_fill_observed_on_thursday_belongs_to_wednesday_and_spans_both(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    wed, thu = "2026-11-11", "2026-11-12"
    t = C.trade(r.db, entry_at=at(wed, 10), exit_at=at(thu, 9, 31), exit_submitted_at=at(wed, 15, 58),
                exit_raw={"filled_at": iso(at(wed, 15, 59)), "updated_at": iso(at(thu, 9, 31))})
    ft, H = fills(r, run, evening(thu), t["position_id"])
    assert (ft["exit_fill_time"], ft["exit_time_source"]) == (iso(at(wed, 15, 59)), "BROKER_FILLED_AT") and H == [wed, thu]


@pytest.mark.parametrize("raw, source", [({"updated_at": "U"}, "BROKER_UPDATED_AT"), ({}, "OBSERVED"), ({"filled_at": "EARLY"}, "OBSERVED"), ({"filled_at": "LATE"}, "OBSERVED")],
                         ids=["TT02_updated_at", "TT03_none", "TT04_before_submit", "TT04_after_event"])
def test_TT02_TT03_TT04_the_fill_time_source(tmp_path, monkeypatch, raw, source):
    r, run = rig(tmp_path, monkeypatch)
    sub, ev = at(D1, 10, 59), at(D1, 11, 5)
    stamp = {"U": iso(at(D1, 11, 1)), "EARLY": iso(sub - timedelta(seconds=61)), "LATE": iso(ev + timedelta(seconds=61))}
    t = C.trade(r.db, entry_at=at(D1, 10), exit_at=ev, exit_submitted_at=sub, exit_raw={k: stamp[v] for k, v in raw.items()})
    ft, _ = fills(r, run, evening(D1), t["position_id"])
    assert ft["exit_time_source"] == source and (ft["exit_fill_time"] == iso(ev)) is (source == "OBSERVED")


def test_TT05_a_partial_entry_over_two_observations_uses_the_first_fill(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    t1, t2 = at(D1, 10), at(D1, 10, 5)
    t = C.trade(r.db, entry_at=t2, exit_at=at(D1, 11), filled_at=t2, created_at=t1 - timedelta(seconds=30))
    add_event(r.db, t["execution_id"], t1, 4, "10.00", raw={"filled_at": iso(t1 - timedelta(seconds=5))})
    ft, _ = fills(r, run, evening(D1), t["position_id"])
    assert (ft["entry_fill_time"], ft["entry_time_source"], ft["entry_observed_at"]) == (iso(t1 - timedelta(seconds=5)), "BROKER_FILLED_AT", iso(t1))


def test_TT06_a_fill_before_the_start_observed_after_it_is_pre_run(tmp_path, monkeypatch):
    s0 = et(2026, 11, 9, 11, 0)
    r, run = rig(tmp_path, monkeypatch, at=s0)
    t = C.trade(r.db, entry_at=s0 + timedelta(seconds=40), exit_at=s0 + timedelta(minutes=30), created_at=s0 + timedelta(seconds=10), filled_at=s0 - timedelta(seconds=20))
    b = r.fv._run_block(evening("2026-11-09"))
    assert next(x for x in b["trades"]["rows"] if x["position_id"] == t["position_id"])["exclusion"] == "pre_run"


def test_TT07_a_canceled_partial_exit_is_broker_updated_at_labelled(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    ev = at(D1, 11, 5)
    t = C.trade(r.db, entry_at=at(D1, 10), exit_at=ev, exit_submitted_at=at(D1, 11), exit_raw={"updated_at": iso(at(D1, 11, 2))})
    eid = C.rows(r.db, "SELECT execution_id FROM execution_intents WHERE intent_type = 'EXIT' AND position_id = ?", (t["position_id"],))[0]["execution_id"]
    add_event(r.db, eid, ev + timedelta(seconds=1), 10, "11.00", raw={"status": "canceled"}, status="canceled")
    ft, _ = fills(r, run, evening(D1), t["position_id"])
    assert (ft["exit_fill_time"], ft["exit_time_source"]) == (iso(at(D1, 11, 2)), "BROKER_UPDATED_AT")
    b = r.fv._run_block(evening(D1))
    assert next(x for x in b["trades"]["rows"] if x["position_id"] == t["position_id"])["time_source"] == "entry BROKER_FILLED_AT; exit BROKER_UPDATED_AT"


def test_TT08_summaries_stay_on_the_observation_date(tmp_path, monkeypatch):
    r, run = rig(tmp_path, monkeypatch)
    wed, thu = "2026-11-11", "2026-11-12"
    w_wed = r.fv.pnl_watermark(wed)
    t = C.trade(r.db, entry_at=at(wed, 10), exit_at=at(thu, 9, 40), exit_submitted_at=at(wed, 15, 58), exit_raw={"filled_at": iso(at(wed, 15, 59))})
    assert [x["position_id"] for x in r.fv.closed_trades(trading_date=thu)] == [t["position_id"]] and r.fv.closed_trades(trading_date=wed) == []
    for d in (wed, thu):
        assert r.fv.fresh_daily(d)["pnl_status"] != "UNSTABLE", d
    assert r.fv.pnl_watermark(wed) == w_wed and r.fv.pnl_watermark(thu) != r.fv.pnl_watermark(wed)   # the bracket stays on closed_at


def test_TT09_the_610a_lifecycle_time_is_untouched():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("forward_capture/capture.py", "forward_capture/health.py", "forward_capture/cohort.py"):
        base = subprocess.run(["git", "show", f"875bc06dd08b36ace1d3930877f90cb96ff81b08:{rel}"], cwd=root, capture_output=True).stdout
        assert (root / rel).read_bytes().replace(b"\r\n", b"\n") == base.replace(b"\r\n", b"\n"), rel
        assert "_fill_times" not in (root / rel).read_text(encoding="utf-8")
