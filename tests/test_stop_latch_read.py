"""
J-04 Emergency Stop reliability (TASK_P_STOP_RELIABILITY_ORDER_GATE_PLAN_2026-11-10.md, design 2.1 Part B; acceptance A8): GET /api/execution-safety/latch,
the persisted Emergency Stop latch alone.
  L01  the mapping: row 1 -> OK / true with the activation time; row 0 -> OK / false; no database, table or row -> NO_ROW / null; an unreadable
       row -> 503 UNREADABLE / null, never a 500 and never a guess
  L02  it never evaluates, never reads the account, writes nothing and records no event: a service whose evaluation blocks on the cash-flow wait
       still answers the latch read in under 100 ms, and the state file is byte-identical before and after
  L03  the route is a GET read under the local gate: refused without the intent header (or the same-origin read metadata), served with it; the
       gate's headers are on the 503 too
  L04  the route set of the safety router gains exactly this GET; the route body reads the latch through SafetyStore.state() / has_state_row() only
  L05  the two readers of the same row agree (G-A2): the latch read's mapping and the future order gate's stop_state classes, pinned on one fixture
"""
import hashlib
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from safety.models import AccountEquity  # noqa: E402
from safety.service import ExecutionSafetyService  # noqa: E402
from safety.store import SafetyStore, SafetyStoreError  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
ROUTE = ROOT / "api" / "routes" / "execution_safety.py"
INTENT = {"X-Stock-Agent-Intent": "dashboard"}


def acct(equity="100000.00"):
    return AccountEquity(equity=equity, source="HYPOTHETICAL_REVIEW", as_of=None, age_s=None, mode="simulated", adjustments="NONE_POSSIBLE")


def svc(tmp_path, account_fn=None, name="j04.db"):
    return ExecutionSafetyService(store=SafetyStore(tmp_path / name), account_fn=account_fn or (lambda ts: acct()), clock=lambda: NOW, startup_policy="KEEP",
                                  threshold_pct=5, max_age_s=900, accept_unadjusted=False)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The real app with the safety service swapped for a scratch one (the stream's attribute the route reads), through the D-20 dashboard client."""
    from api.routes.trader_view import stream
    from api.server import app
    from fastapi.testclient import TestClient
    s = svc(tmp_path)
    st = stream()
    monkeypatch.setattr(st, "safety", s)
    with TestClient(app) as c:
        yield c, s


def mapping(body, status):
    return (status, body.get("status"), body.get("kill_active"), "kill_activated_at" in body and body["kill_activated_at"] is not None)


# ---------------------------------------------------------------- L01 the mapping ---------------------------------------------------------
def test_L01_the_mapping_row_1_row_0_no_row_and_unreadable(api, monkeypatch):
    c, s = api
    assert s.store.has_state_row()                                                           # the app's startup evaluation (startup_if_enabled) wrote the row, kill 0
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (200, "OK", False, False)
    s.emergency_stop("L01", actor="test", now=NOW)
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (200, "OK", True, True)
    assert r.json()["kill_activated_at"] == s.store.state().kill_activated_at
    s.clear_emergency_stop("L01", actor="test", now=NOW)
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (200, "OK", False, False)
    # unreadable: the strict read raises -> 503, never a guess, never a 500 outside the gate
    real_state = s.store.state

    def boom(*a, **k):
        raise SafetyStoreError("the safety state cannot be read (injected)")
    monkeypatch.setattr(s.store, "state", boom)
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (503, "UNREADABLE", None, False) and r.json()["error"] == "SafetyStoreError"
    # an sqlite error from the row check is unreadable too (has_state_row is not strict)
    monkeypatch.setattr(s.store, "state", real_state)
    monkeypatch.setattr(s.store, "has_state_row", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (503, "UNREADABLE", None, False)
    # a saved stop whose (non-strict) row check comes back empty is a contradiction: unreadable, never "no row" (review LOW-2)
    s.emergency_stop("L01", actor="test", now=NOW)
    monkeypatch.setattr(s.store, "has_state_row", lambda: False)
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (503, "UNREADABLE", None, False) and r.json()["error"] == "RowCheckFailed"
    for k in r.json():
        assert k in ("version", "status", "kill_active", "kill_activated_at", "error")      # no account, pause, halt or other field (design L-2)


def test_L01b_no_database_file_is_no_row_and_the_read_creates_nothing(tmp_path, api, monkeypatch):
    c, s = api
    from api.routes.trader_view import stream
    fresh = svc(tmp_path, name="fresh.db")                                                   # no database file at all (the schema's trigger keeps the row once written)
    monkeypatch.setattr(stream(), "safety", fresh)
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert mapping(r.json(), r.status_code) == (200, "NO_ROW", None, False)
    assert not (tmp_path / "fresh.db").exists()                                              # the read created no database
    with pytest.raises(sqlite3.IntegrityError):                                              # and the row of a written state cannot be deleted
        with sqlite3.connect(str(s.store.path)) as conn:
            conn.execute("DELETE FROM execution_safety_state WHERE id = 1")


# ---------------------------------------------------------------- L02 no evaluation, no write ----------------------------------------------
def test_L02_the_latch_read_answers_while_the_evaluation_blocks_and_writes_nothing(tmp_path, monkeypatch):
    from api.routes.trader_view import stream
    from api.server import app
    from fastapi.testclient import TestClient
    armed, entered, release = threading.Event(), threading.Event(), threading.Event()

    def slow_account(ts):
        if armed.is_set():                                                                   # the startup evaluation passes; the full read under test blocks
            entered.set()
            release.wait(30)                                                                 # the cash-flow cycle wait, as the gate's account read sees it
        return acct()
    s = svc(tmp_path, account_fn=slow_account)
    s.evaluate("TEST", NOW)
    s.emergency_stop("L02", actor="test", now=NOW)                                           # the stop is saved before the app starts
    monkeypatch.setattr(stream(), "safety", s)
    calls = {"evaluate": 0}
    real_eval = s.evaluate

    def counting_eval(*a, **k):
        calls["evaluate"] += 1
        return real_eval(*a, **k)
    monkeypatch.setattr(s, "evaluate", counting_eval)
    db = tmp_path / "j04.db"
    with TestClient(app) as c:
        armed.set()
        blocked = threading.Thread(target=lambda: c.get("/api/execution-safety", headers=INTENT))   # the full read: evaluates, blocks on the account read
        blocked.start()
        assert entered.wait(10)
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        n0, e0 = calls["evaluate"], s.store.event_count()
        t0 = time.monotonic()
        r = c.get("/api/execution-safety/latch", headers=INTENT)
        dt = time.monotonic() - t0
        after = hashlib.sha256(db.read_bytes()).hexdigest()
        n1, e1 = calls["evaluate"], s.store.event_count()
        release.set()
        blocked.join(30)
    assert r.status_code == 200 and r.json()["kill_active"] is True and r.json()["status"] == "OK"
    assert dt < 0.1, f"the latch read took {dt:.3f} s while the evaluation was blocked"
    assert (n1, e1) == (n0, e0)                                                              # the latch read evaluated nothing and recorded no event
    assert after == before                                                                   # and wrote nothing: the state file is byte-identical


# ---------------------------------------------------------------- L03 the gate ------------------------------------------------------------
def test_L03_the_route_is_a_gated_get_read(api, monkeypatch):
    c, s = api
    from starlette.testclient import TestClient as RawClient                                 # presents as nothing: no intent header, no fetch metadata (as the gate's own tests)
    s.evaluate("TEST", NOW)
    raw = RawClient(c.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50001))
    refused = raw.get("/api/execution-safety/latch")
    assert refused.status_code == 403 and refused.json().get("error") == "LOCAL_GATE_INTENT"
    assert raw.get("/api/execution-safety/latch", headers=INTENT).status_code == 200          # the header alone is enough (F2's second command, curl)
    ok = c.get("/api/execution-safety/latch", headers=INTENT)
    assert ok.status_code == 200 and ok.headers.get("content-security-policy") and ok.headers.get("x-content-type-options") == "nosniff"
    assert c.post("/api/execution-safety/latch", headers={**INTENT, "Content-Type": "application/json"}, json={}).status_code == 405   # a read, never an action
    monkeypatch.setattr(s.store, "state", lambda: (_ for _ in ()).throw(SafetyStoreError("x")))
    r = c.get("/api/execution-safety/latch", headers=INTENT)
    assert r.status_code == 503 and r.headers.get("content-security-policy")                 # the gate's headers are on the 503 as well (no bare 500)


# ---------------------------------------------------------------- L04 the route set and the route body ----------------------------------
def test_L04_the_route_set_gains_exactly_the_latch_get_and_the_body_reads_the_store_only():
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/execution-safety")}
    assert mine["/api/execution-safety/latch"] == {"get"} and len(mine) == 8
    src = ROUTE.read_text(encoding="utf-8")
    body = src[src.index('@router.get("/latch")'):src.index("def _act(")]
    code = re.sub(r'"""[\s\S]*?"""', "", body)                                               # the code without its docstring
    assert code.count("st.state()") == 1 and code.count("st.has_state_row()") == 1
    assert not re.search(r"evaluate\(|overview\(|current\(|\.account\(|emergency_stop|clear_emergency|\.latch\(|commit\(|record_|_act\(", code)
    assert "status_code=503" in code and '"NO_ROW"' in code and '"UNREADABLE"' in code and "except Exception" in code
    assert not re.search(r"(?i)pause_active|daily_halt|equity|balance|cash", code)         # the latch alone: no account or other state field


# ---------------------------------------------------------------- L05 the two readers of one row agree (G-A2) -----------------------------
@pytest.mark.parametrize("row", ["missing", "0", "1", "unreadable"])
def test_L05_the_latch_read_and_the_order_gates_stop_state_classes_agree_on_one_fixture(tmp_path, monkeypatch, row):
    """The order gate (EMERGENCY_STOP_ORDER_GATE_DESIGN.md 3.1) will read the same row as OFF / ON / UNKNOWN. The mapping both must satisfy, on one
    fixture: 1 -> ON / true; 0 -> OFF / false; missing -> UNKNOWN / NO_ROW (null); unreadable -> UNKNOWN / UNREADABLE (503). The gate's reader does not
    exist yet; this pins the fixture and the latch read's half, and names the gate's expected half."""
    import json
    from api.routes import execution_safety as R
    from api.routes.trader_view import stream
    s = svc(tmp_path)
    if row != "missing":
        s.evaluate("TEST", NOW)
        if row == "1":
            s.emergency_stop("L05", actor="test", now=NOW)
    if row == "unreadable":
        monkeypatch.setattr(s.store, "state", lambda: (_ for _ in ()).throw(SafetyStoreError("unreadable row (fixture)")))
    # (status, kill_active) from the latch read ; the gate's class for the same row
    want = {"missing": ("NO_ROW", None, "UNKNOWN"), "0": ("OK", False, "OFF"), "1": ("OK", True, "ON"), "unreadable": ("UNREADABLE", None, "UNKNOWN")}[row]
    monkeypatch.setattr(stream(), "safety", s)
    r = R.read_latch()
    body = json.loads(r.body)
    assert (body["status"], body["kill_active"]) == want[:2]
    assert r.status_code == (503 if row == "unreadable" else 200)
    assert (want[2] == "UNKNOWN") == (body["kill_active"] is None)                           # the gate's UNKNOWN is exactly the latch read's null
