"""
Wave 2 PR C - W2-6 attribution, F-09, the run acknowledgement (X2) and verified-only P/L on every read surface (WAVE2_PRC_DESIGN_REV4.md
Revision 4.3 sections 11 and 12, semantics 6 and 7; WAVE2_PRC_TEST_PLAN_REV4.md sections 3.8 - 3.10: T-R01 ... T-R11, T-E13, T-E13b, T-P06 ...
T-P23, T-RP01, T-B19). Synthetic data only: the executor's real stack over the simulated broker (test_execution_60), PR A's direct fill rig,
PR B's observer lab; routes through FastAPI's TestClient on tmp_path databases.
"""
import json
import sqlite3
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import wave2_prb_support as B
import wave2_prc_support as C
from test_execution_60 import execution_config, no_execution_wire  # noqa: F401 - the executor rig's two autouse fixtures
from wave2_prc_support import et, iso

ROOT = Path(__file__).resolve().parents[1]
BASE_W2C = "875bc06dd08b36ace1d3930877f90cb96ff81b08"
ROUTES = ("/api/execution", "/api/execution/status", "/api/execution/history", "/api/execution/positions", "/api/execution/symbol/MU")


def at_base(rel):
    return subprocess.run(["git", "show", f"{BASE_W2C}:{rel}"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout


def fv_over(db, execution):
    from forward_validation.service import ForwardValidationService
    from forward_validation.store import ForwardValidationStore
    return ForwardValidationService(lambda: SimpleNamespace(execution=execution), ForwardValidationStore(db), owner="test-owner")


def recon_row(db, at_, symbol, detail_json, kind="MULTIPLE_ORDERS"):
    C.sql(db, "INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, detail_json) VALUES (?, NULL, ?, 'CYCLE', ?, ?, 1, ?)",
          (uuid.uuid4().hex, iso(at_), kind, symbol, detail_json))


def run_row(db, started, summary, *, result="RUN", error=None):
    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(execution_runs)")]
        vals = {"run_id": uuid.uuid4().hex, "started_at": iso(started), "finished_at": iso(started + timedelta(seconds=1)), "trigger": "TEST", "mode": "MONITOR_ONLY",
                "result": result, "error": error, "summary_json": json.dumps(summary)}
        use = [c for c in cols if c in vals]
        conn.execute(f"INSERT INTO execution_runs ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})", [vals[c] for c in use])
        conn.commit()
    finally:
        conn.close()


def multiple_orders(db):
    return [dict(r, detail=json.loads(r["detail_json"] or "{}")) for r in C.rows(db, "SELECT * FROM execution_reconciliations WHERE kind = 'MULTIPLE_ORDERS' ORDER BY at, rowid")]


# ================================================================ 3.8 W2-6, F-09 and X2 (C-64 ... C-68) ======================================

def _stack(tmp_path, *orders, entered_symbol="SNDK"):
    from test_execution_60 import cyc, entered, make_stack
    from wave2_pra_support import _sim_order
    st = make_stack(tmp_path)
    entered(st, entered_symbol)
    st.sim.auto_fill = "manual"                                                    # the orders stay open at the broker
    for sym, coid in orders:
        _sim_order(st, sym, "buy", 1, coid)
    cyc(st)
    return st


def test_TR01_manual_orders_in_a_symbol_without_bot_exposure_are_not_a_duplicate(tmp_path):
    st = _stack(tmp_path, ("AMD", "manual-2"), ("AMD", "manual-1"))
    rows = [r for r in multiple_orders(st.db) if r["symbol"] == "AMD"]
    assert len(rows) == 1 and rows[0]["detail"] == {"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": 0, "bot_exposed": False}
    assert st.x.recon_healthy is False and "AMD: 2 open broker orders" in st.x.recon_reasons        # the executor's entry block is unchanged
    fv = fv_over(st.db, st.x)
    assert fv.order_reliability(None)["duplicate_broker_orders"] == 0 and fv._duplicate_evidence("", "9999") == 0
    assert not [a for a in fv.attention(st.clock.now) if a["code"] == "DUPLICATE_BROKER_ORDER" and "MULTIPLE" in json.dumps(a)]


@pytest.mark.parametrize("orders, key", [((("AMD", "stockagent-entry-" + "a" * 32), ("AMD", "stockagent-entry-" + "b" * 32)), "bot_orders"),
                                         ((("SNDK", "manual-a"), ("SNDK", "manual-b")), "bot_exposed")], ids=["TR02_bot_orders", "TR03_bot_exposed"])
def test_TR02_TR03_bot_orders_or_bot_exposure_is_a_duplicate(tmp_path, orders, key):
    st = _stack(tmp_path, *orders)
    sym = orders[0][0]
    row = next(r for r in multiple_orders(st.db) if r["symbol"] == sym)
    assert (row["detail"]["bot_orders"] >= 2) if key == "bot_orders" else row["detail"]["bot_exposed"] is True, row["detail"]
    fv = fv_over(st.db, st.x)
    assert fv.order_reliability(None)["duplicate_broker_orders"] >= 1 and fv._duplicate_evidence("", "9999") >= 1


def test_TR04_TR09_a_legacy_row_is_counted_and_both_readers_count_the_same_rows(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    t = et(2026, 11, 10, 11)
    for i, detail in enumerate(({"count": 2}, {"count": 2, "client_order_ids": ["m1", "m2"], "bot_orders": 0, "bot_exposed": False},
                                {"count": 2, "client_order_ids": ["m1", "stockagent-x"], "bot_orders": 1, "bot_exposed": True},
                                {"count": 2, "client_order_ids": ["stockagent-a", "stockagent-b"], "bot_orders": 2, "bot_exposed": False}, "not json")):
        recon_row(r.db, t + timedelta(minutes=i), f"S{i}", detail if isinstance(detail, str) else json.dumps(detail))
    rel = r.fv.order_reliability(None)["duplicate_broker_orders"]
    assert rel == r.fv._duplicate_evidence("", "9999") == 4                       # legacy {count}, exposed, two bot orders, unparsable: counted; the manual pair is not


def test_TR05_reconciliation_reasons_blocking_and_health_are_the_baseline(tmp_path):
    import wave2_pra_support as S
    from test_execution_60 import cyc, make_stack
    golden = json.loads((ROOT / "tests" / "wave2_pra_golden.json").read_text(encoding="utf-8"))
    for fn in S.reconciliation_scenarios():
        st = make_stack(tmp_path / fn.__name__)
        fn(st)
        snaps = []
        for _ in range(5):
            cyc(st)
            snaps.append(S.stack_snapshot(st))
        assert {"steps": len(snaps), "sha256": S.trace_digest(snaps)} == golden["stack"][fn.__name__], fn.__name__


def test_TR06_unsorted_client_order_ids_are_sorted_and_write_no_new_row_per_cycle(tmp_path):
    from test_execution_60 import cyc
    st = _stack(tmp_path, ("AMD", "manual-1"), ("AMD", "manual-2"))
    real = st.x.broker.orders
    flip = {"n": 0}

    def orders(status="open", *a, **k):
        out = list(real(status, *a, **k))
        flip["n"] += 1
        return out[::-1] if flip["n"] % 2 else out
    st.x.broker.orders = orders
    for _ in range(4):
        cyc(st)
    rows = [r for r in multiple_orders(st.db) if r["symbol"] == "AMD"]
    assert len(rows) == 1 and rows[0]["detail"]["client_order_ids"] == ["manual-1", "manual-2"]


def test_TR08_a_later_successful_reconciliation_with_no_finding_resolves_multiple_orders(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    t = et(2026, 11, 10, 11)
    recon_row(r.db, t, "SNDK", json.dumps({"count": 2, "client_order_ids": ["a", "b"], "bot_orders": 0, "bot_exposed": True}))
    f = C.rows(r.db, "SELECT * FROM execution_reconciliations")[0]
    assert r.fv._resolution(f) == "OPEN" and r.fv.reconciliation(None)["open"] == 1
    run_row(r.db, t + timedelta(minutes=1), {"findings": 1})
    assert r.fv._resolution(f) == "OPEN"                                            # a later run that still found something
    run_row(r.db, t + timedelta(minutes=2), {"findings": 0}, error="boom")
    assert r.fv._resolution(f) == "OPEN"                                            # an errored run never resolves
    run_row(r.db, t + timedelta(minutes=3), {"findings": 0})
    assert r.fv._resolution(f) == "RESOLVED" and r.fv.reconciliation(None)["open"] == 0


@pytest.mark.parametrize("exposed, expect", [(False, ("HEALTHY", [])), (True, ("FAILED", ["DUPLICATE_BROKER_ORDER"]))])
def test_TR10_the_declared_l1_effect_of_w2_6(tmp_path, monkeypatch, exposed, expect):
    d = "2026-10-26"
    lab = B.Lab(tmp_path, monkeypatch)
    B.bulk_samples(lab.db, [B.healthy_sample(t, lab.owner) for t in B.minutes(et(2026, 10, 26, 9, 30), et(2026, 10, 26, 16, 0))])
    B.add_reconciliation(lab.db, et(2026, 10, 26, 11), "MULTIPLE_ORDERS", symbol="AMD",
                         detail={"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": 0, "bot_exposed": exposed})
    for t in (et(2026, 10, 27, 9, 0), et(2026, 10, 27, 9, 5), et(2026, 10, 27, 9, 10)):
        lab.tick(t)
    row = lab.session(d)
    assert (row["session_status"], row["notes"]) == expect and row["classifier_version"] == "session_classifier_v2_1"


def test_TR10b_a_row_finalized_before_pr_c_is_never_reclassified(tmp_path, monkeypatch):
    d = "2026-10-26"
    lab = B.Lab(tmp_path, monkeypatch)
    B.bulk_samples(lab.db, [B.healthy_sample(t, lab.owner) for t in B.minutes(et(2026, 10, 26, 9, 30), et(2026, 10, 26, 16, 0))])
    B.add_reconciliation(lab.db, et(2026, 10, 26, 11), "MULTIPLE_ORDERS", symbol="AMD", detail={"count": 2})
    C.put_session(lab.db, d, "FAILED", notes="DUPLICATE_BROKER_ORDER", classifier_version="session_classifier_v2")
    before = C.rows(lab.db, "SELECT * FROM forward_validation_sessions")
    for t in (et(2026, 10, 27, 9, 0), et(2026, 10, 27, 9, 5)):
        lab.tick(t)
    assert C.rows(lab.db, "SELECT * FROM forward_validation_sessions WHERE trading_date = ?", (d,)) == [x for x in before if x["trading_date"] == d]


def test_TR11_a_raising_detail_computation_writes_a_legacy_row_and_the_reconciliation_completes(tmp_path):
    from test_execution_60 import cyc, entered, make_stack
    from wave2_pra_support import _sim_order

    class Bad(str):
        def __str__(self):
            raise RuntimeError("unprintable client order id")
    st = make_stack(tmp_path)
    entered(st, "SNDK")
    st.sim.auto_fill = "manual"
    _sim_order(st, "AMD", "buy", 1, "manual-1")
    _sim_order(st, "AMD", "buy", 1, "manual-2")
    real = st.x.broker.orders
    st.x.broker.orders = lambda status="open", *a, **k: [dict(o, client_order_id=Bad(o.get("client_order_id") or "")) for o in real(status, *a, **k)]
    cyc(st)
    rows = [r for r in multiple_orders(st.db) if r["symbol"] == "AMD"]
    assert len(rows) == 1 and rows[0]["detail"] == {"count": 2}                      # a legacy row without the keys
    assert st.x.recon_healthy is False and "AMD: 2 open broker orders" in st.x.recon_reasons
    assert fv_over(st.db, st.x).order_reliability(None)["duplicate_broker_orders"] == 1  # counted (fail-closed)


def _acked_lab(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    now = et(2026, 11, 9, 11)
    for i, boid in enumerate(("b1", "b2")):
        C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
              (boid, "sa-old", "SNDK", "buy", iso(now - timedelta(days=3, seconds=i)), iso(now - timedelta(days=3))))
    r.ready(now)
    run = r.start(now, acknowledge_attention=["DUPLICATE_BROKER_ORDER"])["run"]
    r.x.store.set_mode("PAPER_AUTO", now + timedelta(seconds=1), "user", "arm", {})
    return r, run, now


def test_TE13_the_active_runs_acknowledgement_prevents_the_stop_but_a_new_element_still_stops(tmp_path, monkeypatch):
    r, run, now = _acked_lab(tmp_path, monkeypatch)
    out = r.tick(now + timedelta(minutes=1))
    assert out["early_stop"]["stopped"] is False and r.x.mode()["mode"] == "PAPER_AUTO"
    for i, boid in enumerate(("b3", "b4")):
        C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
              (boid, "sa-new", "SNDK", "buy", iso(now + timedelta(minutes=2, seconds=i)), iso(now + timedelta(minutes=2))))
    out = r.tick(now + timedelta(minutes=3))
    assert out["early_stop"]["stopped"] is True and r.x.mode()["mode"] == "MONITOR_ONLY"


@pytest.mark.parametrize("case", ["suspended", "ended", "malformed", "missing"])
def test_TE13b_the_acknowledgement_is_empty_unless_the_run_is_active_unsuspended_and_readable(tmp_path, monkeypatch, case):
    r, run, now = _acked_lab(tmp_path, monkeypatch)
    if case == "suspended":
        r.store.add_run_event(run["run_id"], iso(now + timedelta(seconds=5)), "SUSPENDED", "CONFIG_CHANGED", {"first_evidence_at": iso(now + timedelta(seconds=5)), "evidence": {}, "also": []})
    elif case == "ended":
        r.store.end_run(run["run_id"], now + timedelta(seconds=5), "test")
    elif case == "malformed":
        C.sql(r.db, "DROP TRIGGER forward_runs_immutable")
        C.sql(r.db, "UPDATE forward_runs SET acknowledged_attention_json = '{not json' WHERE run_id = ?", (run["run_id"],))
    else:
        C.sql(r.db, "DROP TABLE forward_run_events")
        C.sql(r.db, "DROP TABLE forward_runs")
    assert r.fv._run_ack("DUPLICATE_BROKER_ORDER") == frozenset()
    out = r.tick(now + timedelta(minutes=1))
    assert out["early_stop"]["stopped"] is True and r.x.mode()["mode"] == "MONITOR_ONLY"


# ================================================================ 3.9 verified-only P/L on every surface (C-70 ... C-84) =====================

def pnl_fixture(tmp_path):
    """One day: a verified closed trade (+10.00), an unverified one with a raw +20.00, an unverified one with a NULL P/L, and one open position
    whose entry is unpriced ("0.00")."""
    from wave2_pra_support import Direct
    d = Direct(tmp_path)
    v = d.closed_trade(d.entry(xid="a" * 32, sym="SNDK"), "10.00", "11.00")
    u = d.closed_trade(d.entry(xid="b" * 32, sym="AMD"), "10.00", "11.00")
    n = d.closed_trade(d.entry(xid="c" * 32, sym="NVDA"), "10.00", "11.00")
    C.sql(d.db, "UPDATE paper_positions SET realized_pnl = '20.00' WHERE position_id = ?", (u.position_id,))
    C.sql(d.db, "UPDATE paper_positions SET realized_pnl = NULL WHERE position_id = ?", (n.position_id,))
    oe = d.entry(xid="d" * 32, sym="MU")
    d.observe(oe, "filled", 10, "10.00")
    o = d.pos(oe)
    C.sql(d.db, "UPDATE paper_positions SET average_entry_price = '0.00' WHERE position_id = ?", (o.position_id,))
    return d, {"verified": v.position_id, "raw20": u.position_id, "null": n.position_id, "open": o.position_id}


def client(monkeypatch, d, fv="real"):
    import api.routes.trader_view as RT
    from fastapi.testclient import TestClient
    monkeypatch.setitem(RT.STREAM, "instance", SimpleNamespace(forward_validation=d.fv if fv == "real" else fv, execution=d.x))
    from api.server import app
    return TestClient(app)


def decorated_rows(payload):
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "pnl_status" in o:
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(payload)
    return out


def test_TP06_TP07_TP08_TP17_every_execution_read_route_decorates_its_rows(tmp_path, monkeypatch):
    d, ids = pnl_fixture(tmp_path)
    c = client(monkeypatch, d)
    status = {}
    for route in ROUTES + ("/api/execution/symbol/SNDK", "/api/execution/symbol/AMD", "/api/execution/symbol/NVDA"):
        res = c.get(route)
        assert res.status_code == 200, (route, res.text[:300])
        for row in decorated_rows(res.json()):
            pid = row.get("position_id")
            status.setdefault(pid, set()).add((row["pnl_status"], row["pnl_display"]))
            if pid == ids["raw20"]:
                assert row.get("pnl") in (None, "20.00") or row.get("realized_pnl") in (None, "20.00")        # the raw key is kept as raw evidence
    assert status[ids["verified"]] == {("VERIFIED", "10.00")}
    assert status[ids["raw20"]] == {("UNVERIFIED", None)} and status[ids["null"]] == {("UNVERIFIED", None)}
    assert status[ids["open"]] == {("UNKNOWN", None)}
    hist = c.get("/api/execution/history").json()["history"]
    assert {r["position_id"]: r["pnl_status"] for r in hist if r.get("kind") == "TRADE"} == {ids["verified"]: "VERIFIED", ids["raw20"]: "UNVERIFIED", ids["null"]: "UNVERIFIED"}
    pos = c.get("/api/execution/positions").json()["positions"]
    assert {p["position_id"]: p["pnl_status"] for p in pos} == {ids["verified"]: "VERIFIED", ids["raw20"]: "UNVERIFIED", ids["null"]: "UNVERIFIED", ids["open"]: "UNKNOWN"}
    for res in (c.get("/api/execution").json(), c.get("/api/execution/status").json()):
        for card in [x for x in decorated_rows(res) if "key" in x and isinstance(x.get("line"), str) and x["pnl_status"] != "VERIFIED"]:
            assert "20.00" not in card["line"], card["line"]                                                   # a card line never shows a raw number


def test_TP14_unrealized_pnl_of_an_unpriced_entry_is_none_and_the_total_partial(tmp_path):
    d, ids = pnl_fixture(tmp_path)
    u = d.fv.unrealized_pnl()
    assert (u["unrealized_status"], u["unrealized_pnl"], u["unknown_positions"]) == ("UNKNOWN", None, 1)
    assert [p.get("unrealized_pnl") for p in u["positions"] if p.get("position_id") == ids["open"]] in ([None], [])
    from forward_validation.reports import daily_report
    assert "Unrealized paper P/L (priced positions only; informational)" in daily_report(d.fv.daily_summary("2026-10-21"))   # C-83: labelled


def test_TP15_trace_keeps_its_keys_and_marks_every_raw_pnl(tmp_path):
    d, ids = pnl_fixture(tmp_path)
    t = d.fv.trace(ids["raw20"])
    assert {"recorded_pnl", "position_events", "realized_pnl", "verified", "unverified_reasons"} <= set(t)
    rec = t["recorded_pnl"]
    assert rec["raw"] is True and rec["verified"] is False
    for e in t.get("position_events") or t.get("events") or []:
        snap = (e.get("detail") or {}).get("pnl_snapshot") if isinstance(e.get("detail"), dict) else None
        if snap is not None or "pnl_snapshot" in e:
            assert e.get("raw") is True and e.get("verified") is False, e


def test_TP16_only_reconcile_changes_in_the_executor(tmp_path):
    import ast

    def nodes(src):
        out = {}
        for n in ast.parse(src).body:
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[f"{n.name}.{m.name}"] = ast.dump(m)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[n.name] = ast.dump(n)
        return out
    import freeze_ledger as FL
    now, base = nodes(FL.pre_panel("execution/service.py", (ROOT / "execution" / "service.py").read_text(encoding="utf-8"))), nodes(at_base("execution/service.py"))   # risk controls panel: main's text (its own delta: tests/test_panel_freeze.py)
    assert set(now) == set(base) and [k for k in now if now[k] != base[k]] == ["ExecutionService._reconcile"]
    assert (ROOT / "execution" / "models.py").read_text(encoding="utf-8").replace("\r\n", "\n") == at_base("execution/models.py").replace("\r\n", "\n")


def test_TP18_the_outcomes_export_appends_verified_and_a_verified_only_pnl(tmp_path):
    from forward_capture.exports import export_rows as capture_rows
    from forward_validation.reports import export_rows
    d, ids = pnl_fixture(tmp_path)
    from database.forward_capture_migrations import run_forward_capture_migrations
    conn = sqlite3.connect(str(d.db))
    run_forward_capture_migrations(conn)
    for pid in (ids["verified"], ids["raw20"]):
        out = {"position_id": pid, "realized_pnl": "x"}
        conn.execute("INSERT INTO forward_capture_outcomes (position_id, execution_id, candidate_id, cohort_id, closed_at, outcome_json, outcome_sha256) VALUES (?,?,?,?,?,?,?)",
                     (pid, None, None, "FP-1", "2026-10-21T15:00:00+00:00", json.dumps(out, sort_keys=True), "h" * 64))
    conn.commit()
    conn.close()
    base_cols, base_rows = capture_rows(d.fv.capture.store, "outcomes")
    cols, rows = export_rows(d.fv, "outcomes")
    assert tuple(cols) == tuple(base_cols) + ("verified", "pnl")
    assert [{k: r[k] for k in base_cols} for r in rows] == base_rows
    got = {r["position_id"]: (r["verified"], r["pnl"]) for r in rows}
    assert got == {ids["verified"]: (True, "10.00"), ids["raw20"]: (False, None)}
    real = d.fv.execution.store.position
    d.fv.execution.store.position = lambda pid: (_ for _ in ()).throw(sqlite3.OperationalError("locked")) if pid == ids["raw20"] else real(pid)
    cols, rows = export_rows(d.fv, "outcomes")                                     # an unreadable position: unverified, never a 500
    assert {r["position_id"]: (r["verified"], r["pnl"]) for r in rows} == {ids["verified"]: (True, "10.00"), ids["raw20"]: (False, None)}


def test_TP10_TP11_the_registry_section_and_the_cohort_evidence(tmp_path, monkeypatch):
    from strategy_registry.downside_recovery import _paper_results
    sec = _paper_results({"validation": {"sessions_completed": 1, "sessions_target": 30, "verified_trades": 1, "unverified_trades": 2, "run_readiness_text": "Run FR-0001: READY",
                                         "run_label": "1 / 30 strategy sessions (1 healthy · 0 degraded · 0 failed · 0 unreadable · 0 not run)",
                                         "all_time_text": "All-time record: NOT READY (1 invalid day(s): 2026-11-06)", "promotion_text": "promotion blocked: all-time record NOT READY (2026-11-06)"}})
    assert "Closed paper trades: 1 verified · 2 unverified" in sec.items and any(i.startswith("Forward run: Run FR-0001: READY") for i in sec.items)
    assert "All-time record: NOT READY (1 invalid day(s): 2026-11-06)" in sec.items and "promotion blocked: all-time record NOT READY (2026-11-06)" in sec.items
    import ast

    def fallback_100(src):                                                        # the `x if ... else 100` drawdown fallback of 875bc06d's evidence()
        return [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.IfExp) and isinstance(n.orelse, ast.Constant) and n.orelse.value == 100]
    assert not fallback_100((ROOT / "forward_capture" / "view.py").read_text(encoding="utf-8")) and fallback_100(at_base("forward_capture/view.py"))


def test_TP22_every_execution_read_route_writes_nothing_and_calls_no_order_path(tmp_path, monkeypatch):
    from wave2_pra_support import content_hash
    from execution.service import ExecutionService
    d, ids = pnl_fixture(tmp_path)
    calls = []

    class Spy:
        label, simulated = "ALPACA_PAPER", False

        def __getattr__(self, name):
            calls.append(("broker", name))
            raise AssertionError(f"broker.{name} called from a read route")
    d.x.broker = Spy()
    for name in ("run_cycle", "_submit", "_reconcile", "enable_auto", "disable_auto", "_scan_entries", "_recheck_entry"):
        if hasattr(ExecutionService, name):
            monkeypatch.setattr(d.x, name, lambda *a, _n=name, **k: calls.append(("executor", _n)) or (_ for _ in ()).throw(AssertionError(_n)))
    c = client(monkeypatch, d)
    held = sqlite3.connect(f"file:{d.db.as_posix()}?mode=ro", uri=True)
    try:
        for route in ROUTES:
            before, v0 = content_hash(d.db), held.execute("PRAGMA data_version").fetchone()[0]
            assert c.get(route).status_code == 200, route
            assert content_hash(d.db) == before and held.execute("PRAGMA data_version").fetchone()[0] == v0, route
    finally:
        held.close()
    assert calls == []


@pytest.mark.parametrize("mode", ["verification_raises", "no_service"])
def test_TP23_decorate_never_raises_and_never_opens_a_default_store(tmp_path, monkeypatch, mode):
    import forward_validation.store as ST
    d, ids = pnl_fixture(tmp_path)
    data_dir = ROOT / "data"
    before = sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []
    default_stores, real_init = [], ST.ForwardValidationStore.__init__

    def spy_init(self, path=None, *a, **k):                                        # a store opened without an explicit path is the default (app) store
        if path is None:
            default_stores.append("ForwardValidationStore()")
        real_init(self, path, *a, **k)
    monkeypatch.setattr(ST.ForwardValidationStore, "__init__", spy_init)
    if mode == "verification_raises":
        monkeypatch.setattr(d.fv, "verification", lambda p: (_ for _ in ()).throw(RuntimeError("read failed")))
        c = client(monkeypatch, d)
    else:
        c = client(monkeypatch, d, fv=None)
    for route in ROUTES:
        res = c.get(route)
        assert res.status_code == 200, (route, res.text[:200])
        rows = decorated_rows(res.json())
        assert rows and all(r["pnl_status"] == "UNKNOWN" and r["pnl_display"] is None for r in rows), (route, rows[:2])
    assert (sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []) == before and default_stores == []


def test_TP23b_decorate_is_total_over_malformed_payloads():
    from forward_validation.pnl_view import decorate
    for payload in (None, [], "x", {"cards": None}, {"cards": [None, 1, {"key": "OPEN", "symbol": "X", "position_id": "p", "entry": "x", "current": None, "shares": 1}]},
                    {"history": [{"kind": "TRADE", "position_id": "p"}]}, {"events": [{"position_id": "p", "kind": "CLOSED"}]}):
        assert decorate(payload) is payload


# ================================================================ 3.10 reports, and the frozen hash (C-88, I9) ===============================

def test_TRP01_the_weekly_gate_counts_closed_member_sessions_that_are_not_unreadable():
    from forward_validation.reports import weekly_report
    days = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17"]

    def daily(t, status="HEALTHY", notes=()):
        return {"trading_date": t, "session_status": status, "session": {"notes": list(notes)}, "pnl_status": "OK", "realized_pnl": "1.00", "unverified": 0, "win_loss": {"closed_trades": 1}}
    dailies = [daily(days[0]), daily(days[1], "DEGRADED", ("EVIDENCE_UNREADABLE",)), daily(days[2]), daily(days[3], "FAILED", ("UNRESOLVED_RECONCILIATION",)), daily(days[4]),
               daily(days[5])]
    run = {"defined": True, "sessions": {"member_dates": days, "label": "x"}, "strategy": {"edge_label": "Insufficient data", "edge_status": "INSUFFICIENT_DATA"},
           "drawdown": {"text": "bot-only drawdown unknown: x"}}
    cum = {"as_of": "x", "run": run, "strategy": {}, "operational": {}}
    rep = weekly_report(cum, dailies)
    assert "HEALTH (last 5 sessions: 2026-11-10 .. 2026-11-17)" in rep                   # the unreadable day is not one of the five
    assert "Unreadable / failed days in the span" in rep and "1 / 1" in rep.split("Unreadable / failed days in the span")[1].splitlines()[0]
    assert "bot-only drawdown unknown" in rep and "Account curve (informational)" in rep
    assert "Forward run readiness" in rep and "All-time record" in rep                # C-37: both blocks wherever readiness shows
    rep = weekly_report({**cum, "run": {**run, "sessions": {"member_dates": days[:4]}}}, dailies)
    assert "Not yet available: 3 closed member session(s)" in rep
    rep = weekly_report({"as_of": "x"}, dailies)
    assert "Not yet available: 0 closed member session(s)" in rep and "(no forward run defined)" in rep


def test_TB19_the_frozen_strategy_hash_is_unchanged():
    out = subprocess.run([sys.executable, "-c", "import forward_validation as FV; print(FV.frozen_hash())"], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"], out.stderr[-600:]


# ================================================================ L-2 (Option B): the early stop as before; one canonical predicate =========

@pytest.mark.parametrize("history", ["manual_only", "genuine_and_manual"])
def test_TE13c_L2_the_early_stop_is_unchanged_and_the_recorded_sigma_keeps_its_acknowledgement(tmp_path, monkeypatch, history):
    r = C.RunLab(tmp_path, monkeypatch)
    now = et(2026, 11, 9, 11)
    if history == "genuine_and_manual":
        for i, boid in enumerate(("b1", "b2")):                                  # a historic genuine duplicate (counted, so attention is critical)
            C.sql(r.db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                  (boid, "sa-old", "SNDK", "buy", iso(now - timedelta(days=3, seconds=i)), iso(now - timedelta(days=3))))
    C.put_recon(r.db, now - timedelta(days=2), "AMD", C.L2_CASES["manual_unrelated"][0])   # a historic manual pair: recorded at start, never named
    r.ready(now)
    out = r.start(now, acknowledge_attention=["DUPLICATE_BROKER_ORDER"] if history == "genuine_and_manual" else [])
    assert out["ok"], out
    r.x.store.set_mode("PAPER_AUTO", now + timedelta(seconds=1), "user", "arm", {})
    o = r.tick(now + timedelta(minutes=1))
    assert (o.get("early_stop") or {}).get("stopped") is not True and r.x.mode()["mode"] == "PAPER_AUTO"   # the recorded history never stops (run_ack as before L-2)
    C.put_recon(r.db, now + timedelta(minutes=2), "MU", C.L2_CASES["manual_unrelated"][0])
    o = r.tick(now + timedelta(minutes=3))
    if history == "manual_only":
        assert r.x.mode()["mode"] == "PAPER_AUTO"                                           # a new manual pair alone: attention is not critical, no stop
        C.put_recon(r.db, now + timedelta(minutes=4), "SNDK", C.L2_CASES["bot_owned"][0])
        o = r.tick(now + timedelta(minutes=5))
    assert o["early_stop"]["stopped"] is True and r.x.mode()["mode"] == "MONITOR_ONLY"     # PR A's stop unchanged: with the code critical, any new element stops


def test_TR12_L2_one_canonical_duplicate_predicate_everywhere():
    import ast

    def counted_nodes(rel, owners):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        out = {}
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in owners]:
            inner = [m for m in ast.walk(fn) if isinstance(m, ast.FunctionDef) and m.name == "counted" and m is not fn]
            assert len(inner) == 1, (rel, fn.name)
            out[fn.name] = ast.dump(inner[0])
        assert set(out) == set(owners), (rel, sorted(out))
        return out
    nodes = {**counted_nodes("forward_validation/service.py", {"_duplicate_evidence", "order_reliability", "_run_readiness"}),
             **counted_nodes("forward_validation/run_admin.py", {"_preconditions"})}
    assert len(set(nodes.values())) == 1, sorted(nodes)                             # W2-6's rule (C-65) is one predicate in all four readers
    assert (ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8").replace("\r\n", "\n") == at_base("forward_validation/attention_signature.py")


# ================================================================ L-b (operator policy 2026-11-07): visible, never critical, fail-closed =====

def test_TR13_LB_the_executors_own_manual_pair_stays_visible_until_a_clean_reconciliation(tmp_path):
    from test_execution_60 import cyc
    st = _stack(tmp_path, ("AMD", "manual-2"), ("AMD", "manual-1"))
    fv = fv_over(st.db, st.x)
    shown = lambda: [(a["critical"], a["detail"], a["count"]) for a in fv.attention(st.clock.now) if a["code"] == "MANUAL_ORDERS_UNRESOLVED"]   # noqa: E731
    assert shown() == [(False, ["AMD"], 1)] and fv.order_reliability(None)["duplicate_broker_orders"] == 0      # shown, never a duplicate (W2-6)
    assert [d["resolution"] for d in fv.reconciliation(None)["detail"] if d["kind"] == "MULTIPLE_ORDERS"] == ["OPEN"]
    for k in [k for k, o in st.sim.orders.items() if o["client_order_id"].startswith("manual-")]:
        del st.sim.orders[k]                                                       # the operator clears the manual orders at the broker
    cyc(st)                                                                        # the next reconciliation is clean: F-09 resolves the finding
    assert [d["resolution"] for d in fv.reconciliation(None)["detail"] if d["kind"] == "MULTIPLE_ORDERS"] == ["RESOLVED"] and shown() == []


@pytest.mark.parametrize("case", C.LB_UNESTABLISHED)
def test_TR14_LB_ownership_the_row_does_not_establish_is_counted_by_both_readers_and_never_listed_as_manual(tmp_path, monkeypatch, case):
    r = C.RunLab(tmp_path, monkeypatch)
    recon_row(r.db, et(2026, 11, 10, 11), "AMD", C.L2_CASES[case][0])
    rel = r.fv.order_reliability(None)
    assert rel["duplicate_broker_orders"] == r.fv._duplicate_evidence("", "9999") == 1 and rel["manual_orders_open"] == [], (case, rel)
    att = {a["code"]: a for a in r.fv.attention(et(2026, 11, 10, 12))}
    assert att["DUPLICATE_BROKER_ORDER"]["critical"] is True and "MANUAL_ORDERS_UNRESOLVED" not in att      # fail-closed: a duplicate, critical


def test_TE13d_LB_the_informational_manual_item_never_stops_paper_auto(tmp_path, monkeypatch):
    r = C.RunLab(tmp_path, monkeypatch)
    now = et(2026, 11, 9, 11)
    r.ready(now)
    assert r.start(now)["ok"]
    r.x.store.set_mode("PAPER_AUTO", now + timedelta(seconds=1), "user", "arm", {})
    C.put_recon(r.db, now + timedelta(minutes=1), "MU", C.L2_CASES["manual_unrelated"][0])
    o = r.tick(now + timedelta(minutes=2))
    assert "MANUAL_ORDERS_UNRESOLVED" in o["attention"] and (o.get("early_stop") or {}).get("stopped") is not True and r.x.mode()["mode"] == "PAPER_AUTO"
    C.put_recon(r.db, now + timedelta(minutes=3), "SNDK", C.L2_CASES["null_ownership"][0])   # ownership not established: counted, critical, a new element
    o = r.tick(now + timedelta(minutes=4))
    assert o["early_stop"]["stopped"] is True and r.x.mode()["mode"] == "MONITOR_ONLY"      # PR A's stop, unchanged
