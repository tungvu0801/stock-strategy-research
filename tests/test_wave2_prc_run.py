"""
Wave 2 PR C - the forward run: start, identity, suspension and stamps (WAVE2_PRC_DESIGN_REV4.md Revision 4.3 section 4; WAVE2_PRC_TEST_PLAN_REV4.md
section 3.1: T-C01 ... T-C22). Synthetic data only: tmp_path databases, PR B's stub-stream Lab, run_admin in-process with an explicit clock.
"""
import json
import sqlite3
from datetime import timedelta

import pytest

import wave2_prc_support as C
from wave2_prc_support import et, iso

NOW = et(2026, 11, 9, 11, 0)                                                     # Monday 11:00 ET: a regular session


def lab(tmp_path, monkeypatch, **kw):
    r = C.RunLab(tmp_path, monkeypatch, **kw)
    r.ready(NOW)
    return r


def started(tmp_path, monkeypatch, at=NOW, **opts):
    r = lab(tmp_path, monkeypatch)
    out = r.start(at, **opts)
    assert out["ok"], out
    return r, out["run"]


def suspensions(r, run_id):
    return [e for e in r.store.run_events(run_id) if e["kind"] == "SUSPENDED"]


# ================================================================ start (C-01 ... C-12) ======================================================

def test_TC01_no_run_reads_no_forward_run_everywhere(tmp_path, monkeypatch):
    from forward_validation import run_admin
    r = lab(tmp_path, monkeypatch)
    assert run_admin.status(r.store, NOW)["status"] == "no forward run"
    b = r.fv._run_block(NOW)
    assert (b["defined"], b["strategy"]["edge_status"], b["strategy"]["reasons"], b["sessions"]["label"], b["sessions"]["target_met"], b["headline"]) == \
        (False, "INSUFFICIENT_DATA", ["no forward run"], "— / 30", False, "No forward run defined")
    assert b["readiness"]["status"] == "NOT READY"


def test_TC02_a_second_start_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    out = r.start(NOW + timedelta(seconds=5))
    assert not out["ok"] and any(e.startswith("C-01") for e in out["errors"])
    assert len(r.store.runs()) == 1 and len(r.store.run_events(run["run_id"])) == 1


def test_TC03_the_server_identity_never_back_dates_a_run(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path / "a", monkeypatch)
    r.ready(NOW - timedelta(seconds=571))                                          # the sample is 601 s old at NOW
    out = r.start(NOW)
    assert not out["ok"] and any("C-03" in e and "601 s old" in e for e in out["errors"])
    r = C.RunLab(tmp_path / "b", monkeypatch)
    r.ready(NOW + timedelta(seconds=35))                                           # the sample is in the future (age -5 s)
    out = r.start(NOW)
    assert not out["ok"] and any("C-03" in e and "in the future" in e for e in out["errors"])
    r = C.RunLab(tmp_path / "c", monkeypatch)
    r.ready(NOW)
    C.sql(r.db, "INSERT INTO execution_mode_events (event_id, at, from_mode, to_mode, actor, reason) VALUES (?,?,?,?,?,?)",
          ("m1", iso(NOW + timedelta(seconds=1)), "MONITOR_ONLY", "MONITOR_ONLY", "user", "x"))
    out = r.start(NOW)
    assert not out["ok"] and any("C-03" in e and "newer than the start" in e for e in out["errors"])
    r = C.RunLab(tmp_path / "d", monkeypatch)
    r.ready(NOW)
    out = r.start(NOW)
    assert out["ok"] and out["run"]["started_at"] == iso(NOW) == out["run"]["created_at"] and out["run"]["first_session_date"] == "2026-11-10"
    assert "base_equity" not in out["run"]                                         # stored, never printed


def test_TC04_a_cli_environment_that_differs_from_the_server_is_refused(tmp_path, monkeypatch):
    import forward_validation as FV
    r = lab(tmp_path, monkeypatch)
    monkeypatch.setattr(FV, "frozen_hash", lambda: "f" * 64)
    out = r.start(NOW)
    assert not out["ok"] and any("CLI environment differs from the server" in e for e in out["errors"])


def _eval_row(db, at):
    import uuid
    C.sql(db, "INSERT INTO setup_evaluations (evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, reason_text, "
              "inputs_json, freshness_json, trigger) VALUES (?, ?, 'SNDK', 'DOWNSIDE_DISLOCATION_RECOVERY', NULL, 'NONE', 'NONE', 'HOLD', 'X', 'x', '{}', '{}', 'SCHEDULED')",
          (uuid.uuid4().hex, iso(at)))


def test_TC05_capture_liveness_is_built_from_existing_rows_only(tmp_path, monkeypatch):
    import ast
    from pathlib import Path
    r = lab(tmp_path / "a", monkeypatch)
    C.sql(r.db, "INSERT INTO app_leases (name, owner, acquired_at, expires_at) VALUES ('stock_agent_instance', 'other-owner', ?, ?)", (iso(NOW - timedelta(hours=1)), iso(NOW + timedelta(hours=1))))
    out = r.start(NOW)
    assert not out["ok"] and any(e.startswith("C-06 (d)") for e in out["errors"])
    r = C.RunLab(tmp_path / "b", monkeypatch)
    r.ready(NOW, cfg=C.config(**{"flags.FORWARD_VALIDATION_ENABLED": False}))
    assert any(e.startswith("C-06 (a)") for e in r.start(NOW)["errors"])
    for age, ok in ((631, False), (630, True)):                                    # (b): the sample is 30 s old, so 600 s before it is 630 s before NOW
        r = C.RunLab(tmp_path / f"c{age}", monkeypatch)
        r.ready(NOW)
        _eval_row(r.db, NOW - timedelta(seconds=age))                              # above the (absent = 0) capture watermark
        out = r.start(NOW)
        assert (not any(e.startswith("C-06 (b)") for e in out.get("errors", []))) is ok, (age, out)
    for code in ("DUPLICATE_INSTANCE", "SINK_OVERFLOW", "EVIDENCE_LOST"):
        r = C.RunLab(tmp_path / f"d{code}", monkeypatch)
        r.ready(NOW)
        C.sql(r.db, "INSERT INTO forward_capture_faults (fault_id, at, trading_date, component, code, detail_json) VALUES (?,?,?,?,?,?)",
              (code, iso(NOW - timedelta(seconds=100)), "2026-11-09", "capture", code, "{}"))
        assert any(e.startswith("C-06 (c)") for e in r.start(NOW)["errors"]), code
    src = (Path(__file__).resolve().parents[1] / "forward_validation" / "run_admin.py").read_text(encoding="utf-8")
    calls = [n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]
    assert "instance_id" not in calls and "CAPTURE_ENABLED" not in src and "FORWARD_CAPTURE_ENABLED" not in src   # [N-1]: no invented flag, no instance_id()


def test_TC06_the_account_must_be_fresh_and_the_bot_flat(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path / "a", monkeypatch)
    r.ready(NOW, sample_over={"account_status": "STALE"})
    assert any(e.startswith("C-04") for e in r.start(NOW)["errors"])
    r = lab(tmp_path / "b", monkeypatch)
    C.trade(r.db, entry_at=NOW - timedelta(days=3), exit_at=None)
    assert any(e.startswith("C-05") for e in r.start(NOW)["errors"])
    r = lab(tmp_path / "c", monkeypatch)
    C.sql(r.db, "INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, state, attempt_count, created_at, updated_at) "
                "VALUES ('i1', 'ENTRY', 'SNDK', 'buy', 1, 'sa-i1', 'SUBMITTED', 1, ?, ?)", (iso(NOW), iso(NOW)))
    assert any(e.startswith("C-05") for e in r.start(NOW)["errors"])


def test_TC07_critical_attention_must_be_acknowledged_exactly_over_a_strict_store(tmp_path, monkeypatch):
    r = lab(tmp_path, monkeypatch)
    for i, boid in enumerate(("b1", "b2")):                                        # one client order id seen with two broker orders: DUPLICATE_BROKER_ORDER
        C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
              (boid, "sa-dup", "SNDK", "buy", iso(NOW - timedelta(days=2, seconds=i)), iso(NOW - timedelta(days=2))))
    assert any(e.startswith("C-07") for e in r.start(NOW)["errors"])
    assert any(e.startswith("C-07") for e in r.start(NOW, acknowledge_attention=["LIVE_HOST_ATTEMPT"])["errors"])
    out = r.start(NOW, acknowledge_attention=["DUPLICATE_BROKER_ORDER"])
    assert out["ok"], out
    assert sorted(out["run"]["acknowledged_attention"]) == ["DUPLICATE_BROKER_ORDER"] and len(out["run"]["acknowledged_attention"]["DUPLICATE_BROKER_ORDER"]) >= 1
    r2 = lab(tmp_path / "x", monkeypatch)
    C.sql(r2.db, "DROP TABLE execution_reconciliations")
    with pytest.raises(Exception):
        r2.start(NOW)                                                              # a schema error refuses: never "no attention"


def test_TC08_every_open_blocker_must_be_accepted_by_name(tmp_path, monkeypatch):
    r = lab(tmp_path, monkeypatch)
    out = r.start(NOW, accept_blockers=C.BLOCKERS[:4])
    assert not out["ok"] and any(e.startswith("C-09") and "W2-N03" in e for e in out["errors"])
    out = r.start(NOW)
    assert out["ok"] and sorted(out["run"]["accepted_blockers"]) == sorted(C.BLOCKERS)


def test_TC09_prior_invalid_days_are_acknowledged_by_exact_date_and_change_nothing(tmp_path, monkeypatch):
    r = lab(tmp_path, monkeypatch)
    C.put_session(r.db, "2026-11-02", "FAILED", notes="DUPLICATE_BROKER_ORDER")
    C.put_session(r.db, "2026-11-03", "DEGRADED", notes="EVIDENCE_UNREADABLE")
    C.put_session(r.db, "2026-11-04", "DEGRADED", notes="WORKER_COVERAGE_LOW", classifier_version=None)
    C.put_session(r.db, "2026-11-05", "DEGRADED", notes="X", classifier_version=None)
    C.put_session(r.db, "2026-11-06", "HEALTHY")
    before_rows = C.rows(r.db, "SELECT * FROM forward_validation_sessions ORDER BY trading_date")
    before_cum = {k: r.fv.cumulative(NOW)[k] for k in ("sessions", "operational")}
    assert any(e.startswith("C-08 (ii)") for e in r.start(NOW)["errors"])
    assert any(e.startswith("C-08 (ii)") for e in r.start(NOW, acknowledge_invalid_days=["2026-11-02"], reason="x")["errors"])
    assert any(e.startswith("C-08 (ii)") for e in r.start(NOW, acknowledge_invalid_days=["2026-11-02", "2026-11-03"])["errors"])
    out = r.start(NOW, acknowledge_invalid_days=["2026-11-02", "2026-11-03"], reason="reviewed: historic duplicate and unreadable days")
    assert out["ok"], out
    assert [d["date"] for d in out["run"]["prior_invalid_days"]] == ["2026-11-02", "2026-11-03"]
    ev = {e["kind"]: e for e in r.store.run_events(out["run"]["run_id"])}
    assert ev["PRIOR_INVALID_ACKNOWLEDGED"]["detail"]["dates"] == ["2026-11-02", "2026-11-03"] and ev["PRIOR_INVALID_ACKNOWLEDGED"]["detail"]["reason"].startswith("reviewed")
    assert ev["PRIOR_INVALID_ACKNOWLEDGED"]["detail"]["pre_v2_days"] == ["2026-11-04", "2026-11-05"]
    assert C.rows(r.db, "SELECT * FROM forward_validation_sessions ORDER BY trading_date") == before_rows                 # no row changes
    after = r.fv.cumulative(NOW)
    assert {k: after[k] for k in ("sessions", "operational")} == before_cum and after["operational"]["status"] == "NOT READY"   # D-5 / C-36 kept
    assert after["all_time"]["status"] == "NOT READY" and after["all_time"]["invalid_days"] == ["2026-11-02", "2026-11-03"]


def test_TC15_minimums_come_from_code_defaults_or_explicit_flags_never_the_environment(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "FORWARD_VALIDATION_MIN_TRADES", 2, raising=False)
    r, run = started(tmp_path / "a", monkeypatch)
    assert (run["min_trades"], run["target_sessions"], run["target_events"], run["minimums_source"]) == (30, 30, 100, "CODE_DEFAULTS")
    r, run = started(tmp_path / "b", monkeypatch, min_trades=5, target_sessions=7, target_events=9)
    assert (run["min_trades"], run["target_sessions"], run["target_events"], run["minimums_source"]) == (5, 7, 9, "CLI_FLAGS")


def test_TC17_the_cadences_flags_and_early_stop_must_be_as_designed(tmp_path, monkeypatch):
    for over, code in (({"cadences.FORWARD_VALIDATION_INTERVAL_SECONDS": 120}, "INTERVAL"), ({"cadences.FORWARD_VALIDATION_IDLE_SECONDS": 420}, "IDLE"),
                       ({"flags.FORWARD_VALIDATION_EARLY_STOP": False}, "EARLY_STOP"), ({"flags.EXECUTION_SCHEDULER_ENABLED": False}, "SCHEDULER")):
        r = C.RunLab(tmp_path / code, monkeypatch)
        r.ready(NOW, cfg=C.config(**over))
        assert any(e.startswith("C-11") for e in r.start(NOW)["errors"]), code


def test_TC18_TC19_pre_pr_c_samples_and_a_missing_evidence_ref_are_refused(tmp_path, monkeypatch):
    for over in ({"frozen_hash": None}, {"rr_intents": None}):
        r = C.RunLab(tmp_path / str(sorted(over)[0]), monkeypatch)
        r.ready(NOW, sample_over=over)
        assert any(e.startswith("C-02") for e in r.start(NOW)["errors"]), over
    r = lab(tmp_path / "e", monkeypatch)
    assert any(e.startswith("C-12") for e in r.start(NOW, evidence_ref="")["errors"])
    r, run = started(tmp_path / "f", monkeypatch)
    assert run["classifier_version"] == "session_classifier_v2_1"


def test_TC20_a_start_after_the_close_waits_for_the_finalize(tmp_path, monkeypatch):
    at = et(2026, 11, 9, 16, 10)
    r = C.RunLab(tmp_path, monkeypatch)
    r.ready(at)
    C.put_session(r.db, "2026-11-09", "OPEN")
    out = r.start(at)
    assert not out["ok"] and any("an earlier market day is not final" in e for e in out["errors"])
    C.sql(r.db, "UPDATE forward_validation_sessions SET session_status = 'HEALTHY', classifier_version = 'session_classifier_v2_1' WHERE trading_date = '2026-11-09'")
    assert r.start(at)["ok"]


def test_TC14_start_needs_database_rows_only(tmp_path, monkeypatch):
    import api.routes.trader_view as TV
    r = lab(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise AssertionError("no stream, broker or network from run_admin")
    monkeypatch.setattr(TV, "stream", boom)
    import socket
    monkeypatch.setattr(socket, "create_connection", boom)
    assert r.start(NOW)["ok"]


# ================================================================ the triggers (T-C13) =====================================================

def test_TC13_the_run_tables_refuse_every_rewrite(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    rid = run["run_id"]
    for stmt in (f"UPDATE forward_runs SET cohort_id = 'x' WHERE run_id = '{rid}'", f"DELETE FROM forward_runs WHERE run_id = '{rid}'",
                 "DELETE FROM forward_run_events", "UPDATE forward_run_events SET code = 'x'"):
        with pytest.raises(sqlite3.DatabaseError):
            C.sql(r.db, stmt)
    ev = C.rows(r.db, "SELECT event_id FROM forward_run_events")[0]["event_id"]
    with pytest.raises(sqlite3.DatabaseError):
        C.sql(r.db, "INSERT OR REPLACE INTO forward_run_events (event_id, run_id, at, kind) VALUES (?, ?, ?, 'ENDED')", (ev, rid, iso(NOW)))
    row = C.rows(r.db, "SELECT * FROM forward_runs")[0]
    keys = [k for k in row if k not in ("ended_at", "end_reason")]

    def insert(run_id, **over):
        C.sql(r.db, f"INSERT INTO forward_runs ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
              [run_id if k == "run_id" else over.get(k, row[k]) for k in keys])
    with pytest.raises(sqlite3.DatabaseError):                                     # a second active run
        insert("FR-0002")
    r.store.end_run(rid, NOW + timedelta(hours=1), "test")
    with pytest.raises(sqlite3.DatabaseError):                                     # ended_at set twice
        C.sql(r.db, f"UPDATE forward_runs SET ended_at = ? WHERE run_id = '{rid}'", (iso(NOW + timedelta(hours=2)),))
    with pytest.raises(sqlite3.DatabaseError):                                     # a start before the previous end
        insert("FR-0003", created_at=iso(NOW + timedelta(minutes=30)), started_at=iso(NOW + timedelta(minutes=30)))
    insert("FR-0003", created_at=iso(NOW + timedelta(hours=2)), started_at=iso(NOW + timedelta(hours=2)))   # after it: accepted


# ================================================================ stamps (T-C16) and suspension (C-13) =====================================

def test_TC16_samples_are_stamped_and_never_null_from_pr_c_code(tmp_path, monkeypatch):
    r = lab(tmp_path, monkeypatch)
    r.tick(NOW - timedelta(seconds=10))
    C.put_snapshot(r.db, NOW - timedelta(seconds=5), owner=r.owner)                # the stub stream has no universe or market mode: a complete snapshot after it
    out = r.start(NOW)
    assert out["ok"], out
    r.tick(NOW + timedelta(seconds=60))
    monkeypatch.setattr(r.store, "active_run", lambda: (_ for _ in ()).throw(RuntimeError("read failed")))
    r.tick(NOW + timedelta(seconds=120))
    got = [(s["run_id"], s["frozen_hash"] == r.fh) for s in C.rows(r.db, "SELECT run_id, frozen_hash FROM forward_validation_samples WHERE owner = ? ORDER BY at", (r.owner,))]
    assert got[-3:] == [("", True), (out["run"]["run_id"], True), ("UNREADABLE", True)]
    assert not C.rows(r.db, "SELECT 1 FROM forward_validation_samples WHERE owner = ? AND at >= ? AND (run_id IS NULL OR frozen_hash IS NULL)", (r.owner, iso(NOW - timedelta(seconds=10))))
    with pytest.raises(sqlite3.DatabaseError):
        C.sql(r.db, "UPDATE forward_validation_samples SET run_id = 'x'")


def test_TC11_the_snapshot_read_is_retried_three_ticks_before_config_unreadable(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    real = r.store.query_strict
    fail = {"n": 0}

    def flaky(q, args=()):
        if "forward_capture_config_snapshots" in q and fail["n"] > 0:
            fail["n"] -= 1
            raise RuntimeError("locked")
        return real(q, args)
    monkeypatch.setattr(r.store, "query_strict", flaky)
    fail["n"] = 2
    for i in range(2):
        r.fv._run_check(NOW + timedelta(minutes=1 + i))
    assert r.fv._run_check(NOW + timedelta(minutes=3))["result"] == "OK" and not suspensions(r, run["run_id"])
    fail["n"] = 3
    outs = [r.fv._run_check(NOW + timedelta(minutes=4 + i)) for i in range(3)]
    s = suspensions(r, run["run_id"])
    assert outs[-1]["result"] == "SUSPENDED" and len(s) == 1 and s[0]["code"] == "CONFIG_UNREADABLE" and s[0]["detail"]["first_evidence_at"] == iso(NOW + timedelta(minutes=4))


@pytest.mark.parametrize("case", ["HASH_MISMATCH", "COHORT_CHANGED", "EXECUTION_VERSION_CHANGED", "FOREIGN_SAMPLE", "FOREIGN_ROW"])
def test_TC12_one_suspension_per_code_with_its_first_evidence_and_no_resume(tmp_path, monkeypatch, case):
    import execution as X
    import forward_validation as FV
    r, run = started(tmp_path, monkeypatch)
    t1 = NOW + timedelta(minutes=5)
    if case == "HASH_MISMATCH":
        monkeypatch.setattr(FV, "frozen_hash", lambda: "e" * 64)
        import forward_validation.service as S
        monkeypatch.setattr(S, "frozen_hash", lambda: "e" * 64)
    elif case == "COHORT_CHANGED":
        C.put_snapshot(r.db, t1, owner=r.owner, cohort_id="FP-other0000001")
    elif case == "EXECUTION_VERSION_CHANGED":
        monkeypatch.setattr(X, "VERSION", "execution_v9")
    elif case == "FOREIGN_SAMPLE":
        C.B.bulk_samples(r.db, [C.stamped(t1, "5d6f:rollback", None, None)])
    else:
        C.put_session(r.db, "2026-11-09", "HEALTHY", classifier_version=None, at=t1)
    out = r.fv._run_check(t1 + timedelta(seconds=30))
    code = {"FOREIGN_SAMPLE": "FOREIGN_CODE", "FOREIGN_ROW": "FOREIGN_CODE"}.get(case, case)
    s = suspensions(r, run["run_id"])
    assert out["result"] == "SUSPENDED" and len(s) == 1 and s[0]["code"] == code, (out, s)
    if case in ("COHORT_CHANGED", "FOREIGN_SAMPLE", "FOREIGN_ROW"):
        assert s[0]["detail"]["first_evidence_at"] == iso(t1)
    if case == "COHORT_CHANGED":
        C.put_snapshot(r.db, t1 + timedelta(minutes=1), owner=r.owner)            # A -> B -> A: no resume
    r.fv._run_check(t1 + timedelta(minutes=2))
    assert len(suspensions(r, run["run_id"])) == 1
    b = r.fv._run_block(t1 + timedelta(minutes=3))
    assert b["suspended"] and b["readiness"]["status"] == "NOT READY" and b["t_stop"] == s[0]["detail"]["first_evidence_at"]


def test_TC10_a_changed_drift_field_suspends_once_naming_it_and_app_never_does(tmp_path, monkeypatch):
    from forward_validation.run_admin import _drift_fields
    fields = sorted(_drift_fields(C.config()))
    assert {f.split(".")[0] for f in fields} >= {"flags", "cadences", "executor_limits", "universe", "autorun", "market_data_mode", "calendar", "versions", "entry_controls", "broker"}
    assert sum(1 for f in fields if f.startswith("flags.")) == 8 and sum(1 for f in fields if f.startswith("cadences.")) == 8 and sum(1 for f in fields if f.startswith("executor_limits.")) == 13
    for i, f in enumerate(fields + ["NULL"]):
        r, run = started(tmp_path / f"f{i}", monkeypatch)
        if f == "NULL":
            cfg = C.config(**{"broker": None})
            name = "broker"
        else:
            g, _, n = f.partition(".")
            base = C.config()
            cur = base[g][n] if n else base[g]
            new = (not cur) if isinstance(cur, bool) else (cur + 7 if isinstance(cur, int) else f"{cur}-changed")
            cfg = C.config(**{f: new})
            name = f
        C.put_snapshot(r.db, NOW + timedelta(minutes=1), owner=r.owner, cfg=cfg)
        r.fv._run_check(NOW + timedelta(minutes=2))
        s = suspensions(r, run["run_id"])
        assert len(s) == 1 and s[0]["code"] == "CONFIG_CHANGED" and name in s[0]["detail"]["evidence"]["fields"], (f, s)
    r, run = started(tmp_path / "app", monkeypatch)
    C.put_snapshot(r.db, NOW + timedelta(minutes=1), owner=r.owner, cfg=C.config(app={"app_root": "elsewhere", "db_name": "z"}))
    assert r.fv._run_check(NOW + timedelta(minutes=2))["result"] == "OK" and not suspensions(r, run["run_id"])


def test_TC21_capture_liveness_holds_over_a_night_and_a_lease_mismatch_needs_three_ticks(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    night = et(2026, 11, 13, 21, 0)                                                # a Friday night: no setup evaluation, an empty backlog
    for i in range(5):
        assert r.fv._run_check(night + timedelta(minutes=i))["result"] == "OK"
    C.sql(r.db, "INSERT INTO app_leases (name, owner, acquired_at, expires_at) VALUES ('stock_agent_instance', 'new-process', ?, ?)",
          (iso(night + timedelta(minutes=10)), iso(night + timedelta(hours=1))))
    for i in range(5):                                                             # N-2: no snapshot of the new holder yet (deferred STARTUP): never a suspension
        assert r.fv._run_check(night + timedelta(minutes=11 + i))["result"] == "OK"
    C.put_snapshot(r.db, night + timedelta(minutes=20), owner="third-owner")
    outs = [r.fv._run_check(night + timedelta(minutes=21 + i))["result"] for i in range(3)]
    s = suspensions(r, run["run_id"])
    assert outs == ["OK", "OK", "SUSPENDED"] and len(s) == 1 and s[0]["code"] == "CAPTURE_STALE" and s[0]["detail"]["first_evidence_at"] == iso(night + timedelta(minutes=21))


def test_TC22_a_raising_suspension_check_never_skips_the_early_stop(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    r.x.store.set_mode("PAPER_AUTO", NOW, "user", "arm", {})
    for i, boid in enumerate(("b1", "b2")):
        C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
              (boid, "sa-dup2", "SNDK", "buy", iso(NOW + timedelta(seconds=i)), iso(NOW)))
    monkeypatch.setattr(r.fv, "_run_check", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    out = r.tick(NOW + timedelta(minutes=1))
    assert out["early_stop"]["stopped"] is True and out["run"]["result"] == "ERROR" and r.x.mode()["mode"] == "MONITOR_ONLY"


def test_TC23_the_cli_prints_json_never_the_equity_and_exits_by_outcome(tmp_path, monkeypatch, capsys):
    import forward_validation.store as ST
    from forward_validation import run_admin
    r = lab(tmp_path, monkeypatch)
    monkeypatch.setattr(ST, "ForwardValidationStore", lambda *a, **k: r.store)        # the CLI's default store, here the lab's database
    assert run_admin.main(["status"]) == 0 and json.loads(capsys.readouterr().out)["status"] == "no forward run"
    code = run_admin.main(["start", "--approved-by", "operator", "--evidence-ref", "DW2K-1", "--accept-blockers", ",".join(C.BLOCKERS)])
    out = capsys.readouterr().out
    assert code == 1 and "C-03" in out and "base_equity" not in out                    # the wall clock is before the lab's samples: refused, each error printed
    run = r.start(NOW)["run"]
    assert run_admin.main(["status"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["run"]["run_id"] == run["run_id"] and "base_equity" not in out and "100000.00" not in out
    assert run_admin.main(["end", "--reason", "test end"]) == 0 and r.store.active_run() is None
    assert run_admin.main(["end", "--reason", "again"]) == 1


def test_TC24_a_start_day_finalized_failed_before_the_start_is_a_prior_day_to_acknowledge(tmp_path, monkeypatch):
    at17 = et(2026, 11, 9, 17, 0)                                                    # after the close: the day is outside O(R)
    r = C.RunLab(tmp_path, monkeypatch)
    r.ready(at17)
    C.put_session(r.db, "2026-11-09", "FAILED", notes="UNRESOLVED_RECONCILIATION")
    out = r.start(at17)
    assert not out["ok"] and any(e.startswith("C-08 (ii)") and "2026-11-09" in e for e in out["errors"]), out
    out = r.start(at17, acknowledge_invalid_days=["2026-11-09"], reason="reviewed: the start day failed before the start")
    assert out["ok"], out
    assert [d["date"] for d in out["run"]["prior_invalid_days"]] == ["2026-11-09"]
    assert [e["detail"]["dates"] for e in r.store.run_events(out["run"]["run_id"]) if e["kind"] == "PRIOR_INVALID_ACKNOWLEDGED"] == [["2026-11-09"]]
    assert "2026-11-09" not in r.fv._run_block(et(2026, 11, 10, 17, 0))["sessions"]["overlap_dates"]
    r2 = C.RunLab(tmp_path / "noon", monkeypatch)                                     # before the close the start day is the run's own (partial) day
    r2.ready(NOW)
    assert r2.start(NOW)["ok"]


def test_TC11b_a_persistent_read_failure_suspends_once(tmp_path, monkeypatch):
    r, run = started(tmp_path, monkeypatch)
    assert r.fv._run_check(NOW + timedelta(minutes=1))["result"] == "OK"
    real_q, real_runs = r.store.query_strict, r.store.runs
    monkeypatch.setattr(r.store, "query_strict", lambda q, args=(): (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    monkeypatch.setattr(r.store, "runs", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    outs = [r.fv._run_check(NOW + timedelta(minutes=2 + i))["result"] for i in range(6)]
    monkeypatch.setattr(r.store, "runs", real_runs)
    monkeypatch.setattr(r.store, "query_strict", real_q)
    s = suspensions(r, run["run_id"])
    assert outs[:3] == ["UNREADABLE", "UNREADABLE", "SUSPENDED"] and len(s) == 1 and s[0]["code"] == "CONFIG_UNREADABLE", (outs, s)


# ================================================================ L-2 (Option B): the start check agrees with attention (C-07, C-65) ========

@pytest.mark.parametrize("case", sorted(C.L2_CASES))
def test_TC25_L2_the_start_requires_naming_exactly_the_duplicates_attention_shows_and_records_the_whole_sigma(tmp_path, monkeypatch, case):
    detail, counted = C.L2_CASES[case]
    r = lab(tmp_path, monkeypatch)
    rid = C.put_recon(r.db, NOW - timedelta(days=2), "AMD", detail)
    assert ("DUPLICATE_BROKER_ORDER" in {a["code"] for a in r.fv.attention(NOW) if a["critical"]}) is counted
    if counted:
        out = r.start(NOW)
        assert not out["ok"] and any(e.startswith("C-07") and "DUPLICATE_BROKER_ORDER" in e for e in out["errors"]), out
        out = r.start(NOW, acknowledge_attention=["DUPLICATE_BROKER_ORDER"])
    else:
        bad = r.start(NOW, acknowledge_attention=["DUPLICATE_BROKER_ORDER"])     # naming a code attention does not show is refused, as before
        assert not bad["ok"] and any(e.startswith("C-07") for e in bad["errors"]), bad
        out = r.start(NOW)
    assert out["ok"], (case, out)
    run = out["run"]
    assert ["REC", "execution_reconciliations", rid] in run["acknowledged_attention"]["DUPLICATE_BROKER_ORDER"]     # the whole sigma is recorded (run_ack as before)
    started = [e for e in r.store.run_events(run["run_id"]) if e["kind"] == "STARTED"][0]
    assert started["detail"]["duplicate_rows_not_counted"] == ([] if counted else [rid])


def test_TC26_L2_an_unreadable_ownership_read_refuses_the_start(tmp_path, monkeypatch):
    r = lab(tmp_path, monkeypatch)
    C.put_recon(r.db, NOW - timedelta(days=2), "AMD", C.L2_CASES["manual_unrelated"][0])
    real = r.store.query_strict

    def flaky(q, args=()):
        if "kind = 'MULTIPLE_ORDERS'" in q:
            raise sqlite3.OperationalError("database is locked")
        return real(q, args)
    monkeypatch.setattr(r.store, "query_strict", flaky)
    with pytest.raises(sqlite3.OperationalError):
        r.start(NOW)
    monkeypatch.setattr(r.store, "query_strict", real)
    assert r.store.runs() == []


def test_TC27_LB_an_unresolved_manual_pair_is_shown_but_never_has_to_be_named_at_the_start(tmp_path, monkeypatch):
    r = lab(tmp_path, monkeypatch)
    rid = C.put_recon(r.db, NOW - timedelta(days=2), "AMD", C.L2_CASES["manual_unrelated"][0])   # unresolved: no clean reconciliation after it
    att = {a["code"]: a for a in r.fv.attention(NOW)}
    assert (att["MANUAL_ORDERS_UNRESOLVED"]["critical"], att["MANUAL_ORDERS_UNRESOLVED"]["detail"]) == (False, ["AMD"]) and "DUPLICATE_BROKER_ORDER" not in att
    bad = r.start(NOW, acknowledge_attention=["MANUAL_ORDERS_UNRESOLVED"])           # C-07 names critical codes only: an informational code is refused
    assert not bad["ok"] and any(e.startswith("C-07") for e in bad["errors"]), bad
    out = r.start(NOW)
    assert out["ok"], out
    started = [e for e in r.store.run_events(out["run"]["run_id"]) if e["kind"] == "STARTED"][0]
    assert started["detail"]["duplicate_rows_not_counted"] == [rid]
