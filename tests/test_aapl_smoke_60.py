"""
Standalone scheduled AAPL one-share Alpaca PAPER smoke (scripts/run_aapl_paper_smoke.py + .ps1 + install_aapl_smoke_task.ps1).
Every run here uses the deterministic SIMULATED paper broker below the real paper client and simulated market data; no Alpaca call.

  P a full simulated PASS through the real Stage 6.0 path · G the independent order guard · W the time window · D duplicate runs
  F failure matrix (Paper Auto always off, no blind retry) · S secrets / credential isolation · T Robinhood / model tripwires
  X the whole script as a process (stub server, concurrent duplicate, PowerShell wrapper) · J the Task Scheduler definition
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from execution import broker as BK  # noqa: E402
from execution.service import SimulatedCrash  # noqa: E402
from execution.simulated import SimulatedPaperBroker  # noqa: E402
from execution.store import ExecutionStore  # noqa: E402
from paper import alpaca_order_rules as RU  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402

PS = shutil.which("powershell.exe")
RUN_DATE = "2026-10-23"
FAKE = {"ALPACA_PAPER_API_KEY": "SMOKETEST-paperkey-a1b2c3d4", "ALPACA_PAPER_SECRET_KEY": "SMOKETEST-papersecret-e5f6a7b8",
        "ALPACA_API_KEY": "SMOKETEST-datakey-c9d0e1f2", "ALPACA_SECRET_KEY": "SMOKETEST-datasecret-a3b4c5d6"}
OTHER = {"ROBINHOOD_USERNAME": "someone@example.com", "ROBINHOOD_PASSWORD": "SMOKETEST-rhpass-778899", "ANTHROPIC_API_KEY": "SMOKETEST-anthropic-445566",
         "FRED_API_KEY": "SMOKETEST-fred-112233"}


def _module():
    spec = importlib.util.spec_from_file_location("aapl_smoke_under_test", SCRIPTS / "run_aapl_paper_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                                                     # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


M = _module()


@pytest.fixture(autouse=True)
def smoke_config(monkeypatch):
    monkeypatch.setattr(config, "TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", False)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", False)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)
    hits = []

    def tripwire():
        hits.append("wire")
        raise AssertionError("the real Alpaca paper network adapter was requested")
    monkeypatch.setattr(BK, "WIRE", tripwire)
    yield
    assert not hits


def make_ctx(tmp_path, clock_et=f"{RUN_DATE} 09:46", server=None, run_date=RUN_DATE):
    base = datetime.strptime(clock_et, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    off = {"s": 0.0}
    clock = lambda: base + timedelta(seconds=off["s"])  # noqa: E731
    sim = SimulatedPaperBroker(now_fn=clock)
    broker = BK.AlpacaPaperBroker(simulated=True, wire=lambda: sim)
    ctx = M.Ctx(broker=broker, market=SimulatedMarket(seed=11, start_time=int(base.timestamp())), clock=clock, sleep=lambda sec: off.__setitem__("s", off["s"] + sec),
                server_check=server or (lambda: {"ok": True, "mode": "MONITOR_ONLY", "detail": "stub server"}), refresh_snapshot=None, simulated=True,
                runtime_dir=tmp_path / "runtime", log_dir=tmp_path / "logs", run_date=run_date, secrets=list(FAKE.values()), sim=sim, loaded_names=sorted(FAKE))
    return ctx


def result(ctx):
    return json.loads((ctx.runtime_dir / M.RESULT_FILE).read_text(encoding="utf-8"))


def state(ctx):
    return json.loads((ctx.runtime_dir / M.STATE_FILE).read_text(encoding="utf-8"))


def sides(ctx):
    return [json.loads(b)["side"] for b in ctx.sim.posts]


def mode_in_db(ctx):
    db = ctx.runtime_dir / M.DB_FILE
    return ExecutionStore(db).mode()["mode"] if db.exists() else "NO_DB"


# ================================================================ P full simulated PASS ============================================

def test_P01_full_simulated_smoke_passes_with_exactly_one_buy_and_one_sell(tmp_path):
    ctx = make_ctx(tmp_path)
    assert M.run(ctx) == M.EXIT_PASS
    r = result(ctx)
    assert r["result"] == "PASS" and r["failure_reason"] is None
    assert sides(ctx) == ["buy", "sell"] and all(json.loads(b)["qty"] == "1" and json.loads(b)["symbol"] == "AAPL" for b in ctx.sim.posts)
    assert "AAPL" not in ctx.sim.positions and r["broker_final_qty"] == 0 and r["internal_final_state"] == "CLOSED"
    assert (r["entry_posts"], r["exit_posts"], r["entry_filled_qty"], r["exit_filled_qty"], r["duplicate_orders"]) == (1, 1, 1, 1, 0)
    assert r["paper_auto_final"] == "OFF" and mode_in_db(ctx) == "MONITOR_ONLY" and r["reconciliation_before"] == "HEALTHY" and r["reconciliation_after"] == "HEALTHY"
    assert r["exit_reason"] == "DISLOCATION_NORMALIZED" and r["paper_account_proven"] is True and r["market_open"] is True and r["safety_state"] == "TRADING_ALLOWED"
    assert r["entry_client_order_id"].startswith("stockagent-entry-") and r["exit_client_order_id"].startswith("stockagent-exit-") and r["proposal_qty"] > 1
    for k in ("started_at", "finished_at", "symbol", "proposal_id", "execution_id", "entry_broker_order_id", "position_id", "exit_broker_order_id", "secret_matches"):
        assert k in r and r[k] is not None, k
    assert state(ctx)["state"] == "PASS" and state(ctx)["broker_submissions"] == 2
    log = (ctx.log_dir / M.LOG_FILE).read_text(encoding="utf-8")
    assert "SMOKE_FORCED_SETUP" in log and "SMOKE_FORCED_EXIT" in log and "NOT a strategy discovery" in log and "RESULT PASS" in log
    assert "UNADJUSTED_PNL_ACCEPTED_FOR_SMOKE_ONLY" not in log and r["labels"] == ["SMOKE_FORCED_SETUP", "SMOKE_FORCED_EXIT"]      # simulated: hypothetical equity, no exception
    store = ExecutionStore(ctx.runtime_dir / M.DB_FILE)
    assert len(store.intents(intent_type="ENTRY")) == 1 and len(store.intents(intent_type="EXIT")) == 1 and len(store.positions(open_only=False)) == 1


# ================================================================ G the order guard ====================================================

class _Dummy:
    def __init__(self):
        self.calls = []

    def post_order(self, payload, coid):
        self.calls.append(coid)
        return {"kind": "response", "status": 200}


def _payload(side, qty=1, symbol="AAPL", coid=None):
    coid = coid or ("stockagent-entry-" + "a" * 32 if side == "buy" else "stockagent-exit-" + "b" * 32 + "-1")
    return RU.canonical_payload(coid, symbol, side, qty), coid


@pytest.mark.parametrize("first,second,reason", [
    (("buy", 1, "MSFT"), None, "AAPL only"), (("buy", 2, "AAPL"), None, "exactly 1 share"), (("sell", 1, "AAPL"), None, "BUY then SELL"),
    (("buy", 1, "AAPL"), ("buy", 1, "AAPL"), "BUY then SELL"), (("buy", 1, "AAPL"), ("sell", 2, "AAPL"), "exactly 1 share")])
def test_G01_guard_refuses_before_the_wire(first, second, reason):
    d = _Dummy()
    g = M.OrderGuard(d)
    steps = [first] + ([second] if second else [])
    for i, (side, qty, sym) in enumerate(steps):
        payload, coid = _payload(side, qty, sym)
        if i == len(steps) - 1:
            with pytest.raises(M.SmokeGuardViolation, match=reason):
                d.post_order(payload, coid)
        else:
            d.post_order(payload, coid)
    assert len(d.calls) == len(steps) - 1 and reason in g.violation


def test_G02_guard_allows_exactly_buy_then_sell_and_refuses_a_third_order_and_foreign_ids():
    d = _Dummy()
    g = M.OrderGuard(d)
    for side in ("buy", "sell"):
        d.post_order(*_payload(side))
    with pytest.raises(M.SmokeGuardViolation, match="at most 2"):
        d.post_order(*_payload("sell"))
    assert len(d.calls) == 2 and g.count("buy") == 1 and g.count("sell") == 1
    d2 = _Dummy()
    M.OrderGuard(d2)
    with pytest.raises(M.SmokeGuardViolation, match="not the executor"):
        d2.post_order(*_payload("buy", coid="sa46b-" + "c" * 32))
    assert d2.calls == []


# ================================================================ W time window ========================================================

@pytest.mark.parametrize("clock_et,run_date", [(f"{RUN_DATE} 09:44", RUN_DATE), (f"{RUN_DATE} 10:00", RUN_DATE), (f"{RUN_DATE} 13:30", RUN_DATE), ("2026-10-26 09:46", RUN_DATE),
                                               (f"{RUN_DATE} 09:46", "2026-10-22")])
def test_W01_outside_the_window_or_on_another_date_no_order(tmp_path, clock_et, run_date):
    ctx = make_ctx(tmp_path, clock_et=clock_et, run_date=run_date)
    assert M.run(ctx) == M.EXIT_NO_ORDER
    r = result(ctx)
    assert r["result"] == "FAIL" and r["failure_reason"].startswith("NO ORDER: time window") and ctx.sim.requests == [] and r["paper_auto_final"] == "OFF (never enabled)"
    assert state(ctx)["state"] == "ABORTED_NO_ORDER"


# ================================================================ D duplicate runs =====================================================

def test_D01_a_second_run_after_pass_or_a_crashed_run_is_refused(tmp_path):
    a = make_ctx(tmp_path)
    assert M.run(a) == M.EXIT_PASS
    b = make_ctx(tmp_path, clock_et=f"{RUN_DATE} 09:50")
    assert M.run(b) == M.EXIT_REFUSED and b.sim.requests == [] and result(b)["result"] == "PASS"         # B never touched a broker and never rewrote A's result
    (tmp_path / "runtime" / M.STATE_FILE).write_text(json.dumps({"state": "RUNNING", "run_id": "x", "run_date": RUN_DATE, "started_at": "now"}), encoding="utf-8")
    c = make_ctx(tmp_path)
    assert M.run(c) == M.EXIT_REFUSED and c.sim.requests == []
    log = (tmp_path / "logs" / M.LOG_FILE).read_text(encoding="utf-8")
    assert "already finished PASS" in log and "is RUNNING or ended without a final state" in log


def test_D02_a_held_lock_or_a_database_with_intents_refuses_and_a_no_order_abort_allows_a_rerun(tmp_path):
    lock = M.SmokeLock(tmp_path / "runtime" / M.LOCK_FILE)
    assert lock.acquire()
    try:
        ctx = make_ctx(tmp_path)
        assert M.run(ctx) == M.EXIT_REFUSED and ctx.sim.requests == []
    finally:
        lock.release()
    early = make_ctx(tmp_path, clock_et=f"{RUN_DATE} 09:40")
    assert M.run(early) == M.EXIT_NO_ORDER and state(early)["state"] == "ABORTED_NO_ORDER"
    ok = make_ctx(tmp_path)
    assert M.run(ok) == M.EXIT_PASS                                                       # nothing was sent before: a run in the window may proceed
    (tmp_path / "runtime" / M.STATE_FILE).unlink()                                        # even without the marker, a database with intents refuses
    again = make_ctx(tmp_path)
    assert M.run(again) == M.EXIT_REFUSED and again.sim.requests == []
    assert M.refusal_reason({"state": "FAIL", "run_date": RUN_DATE, "broker_submissions": 1}, RUN_DATE)
    assert M.refusal_reason({"state": "FAIL", "run_date": RUN_DATE, "broker_submissions": 0}, RUN_DATE) is None
    assert M.refusal_reason({"state": "PASS", "run_date": "2026-10-16"}, RUN_DATE) is None


# ================================================================ F failure matrix ===================================================

def _unknown(s):
    from safety.models import SafetyView
    s.safety.evaluate = lambda *a, **k: SafetyView(state="UNKNOWN", state_label="Trading stopped", icon="x", error="forced")


def _open_order(sim, symbol, coid, status="accepted"):
    o = sim._order_json({"client_order_id": coid, "symbol": symbol, "qty": "1", "type": "market", "side": "buy", "time_in_force": "day"}, status)
    sim.orders[o["id"]] = o


def _raise_on_post(n):
    count = {"n": 0}

    def hook(ctx):
        count["n"] += 1
        if count["n"] == n:
            raise SimulatedCrash(f"forced exception after broker order {n}")
    return hook


CASES = {
    "server unreachable": (dict(server=lambda: {"ok": False, "detail": "connection refused"}), None, M.EXIT_NO_ORDER, 0, "server reachable"),
    "server bot in Paper Auto": (dict(server=lambda: {"ok": True, "mode": "PAPER_AUTO", "detail": "x"}), None, M.EXIT_NO_ORDER, 0, "Monitor only"),
    "market closed": ({}, lambda c: setattr(c.sim, "is_open", False), M.EXIT_NO_ORDER, 0, "broker clock open"),
    "paper account not proven": ({}, lambda c: c.sim.read_fail_paths.append("/v2/account"), M.EXIT_NO_ORDER, 0, "paper account reachable"),
    "AAPL not tradable": ({}, lambda c: c.sim.untradable.add("AAPL"), M.EXIT_NO_ORDER, 0, "AAPL tradable"),
    "safety paused": ({}, ("after_build", lambda s: s.safety.pause(actor="test")), M.EXIT_NO_ORDER, 0, "safety allows new entries"),
    "safety halted": ({}, ("after_build", lambda s: s.safety.emergency_stop(actor="test")), M.EXIT_NO_ORDER, 0, "safety allows new entries"),
    "safety unknown": ({}, ("after_build", _unknown), M.EXIT_NO_ORDER, 0, "safety service readable"),
    "reconciliation unhealthy": ({}, lambda c: _open_order(c.sim, "MU", "stockagent-entry-" + "f" * 32, "held"), M.EXIT_NO_ORDER, 0, "reconciliation healthy"),
    "existing AAPL position": ({}, lambda c: c.sim.set_position("AAPL", 1, 190.0), M.EXIT_NO_ORDER, 0, "no unexpected AAPL broker position"),
    "existing AAPL order": ({}, lambda c: _open_order(c.sim, "AAPL", "manual-123", "held"), M.EXIT_NO_ORDER, 0, "no conflicting AAPL broker order"),
    "earlier smoke order today": ({}, lambda c: _open_order(c.sim, "AAPL", "stockagent-entry-" + "e" * 32, "filled"), M.EXIT_NO_ORDER, 0, "broker-side idempotency"),
    "entry reject": ({}, lambda c: c.sim.post_modes.append("reject"), M.EXIT_FAIL, 1, "entry REJECTED"),
    "ambiguous entry unresolved": ({}, lambda c: c.sim.post_modes.append("timeout_before"), M.EXIT_FAIL, 1, "ambiguous BUY unresolved"),
    "entry never fills": ({}, lambda c: setattr(c.sim, "auto_fill", "manual"), M.EXIT_FAIL, 1, "entry not confirmed"),
    "exit reject": ({}, lambda c: c.sim.post_modes.extend(["accept", "reject"]), M.EXIT_FAIL, 2, "exit REJECTED"),
    "broker mismatch after the fill": ({}, ("after_entry_fill", lambda s: s.execution.broker.wire().set_position("AAPL", 2, 190.0)), M.EXIT_FAIL, 1, "reconciliation healthy after the entry"),
    "exception after BUY": ({}, ("exec_hook", _raise_on_post(1)), M.EXIT_FAIL, 1, "forced exception after broker order 1"),
    "exception after SELL": ({}, ("exec_hook", _raise_on_post(2)), M.EXIT_FAIL, 2, "forced exception after broker order 2"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_F01_failures_never_retry_blindly_and_always_end_with_paper_auto_off(tmp_path, name):
    kw, setup, code, posts, why = CASES[name]
    ctx = make_ctx(tmp_path, **kw)
    if callable(setup):
        setup(ctx)
    elif setup and setup[0] == "exec_hook":
        hook = setup[1]
        ctx.hooks["after_build"] = lambda s: s.execution.hooks.__setitem__("after_post", hook)
    elif setup:
        ctx.hooks[setup[0]] = setup[1]
    assert M.run(ctx) == code, result(ctx)
    r = result(ctx)
    assert r["result"] == "FAIL" and why in r["failure_reason"], r["failure_reason"]
    sent = r["orders_sent"]
    assert len(sent) == posts and [o["side"] for o in sent] == ["buy", "sell"][:posts] and all(o["qty"] == "1" and o["symbol"] == "AAPL" for o in sent)
    assert len(ctx.sim.posts) <= posts                                                                    # an ambiguous POST may never have reached the broker
    assert r["paper_auto_final"] in ("OFF", "OFF (never enabled)") and mode_in_db(ctx) in ("MONITOR_ONLY", "NO_DB")
    assert state(ctx)["state"] == ("ABORTED_NO_ORDER" if posts == 0 else "FAIL") and state(ctx)["broker_submissions"] == posts
    again = make_ctx(tmp_path)
    expect = M.EXIT_REFUSED if posts else None
    if expect is not None:
        assert M.run(again) == M.EXIT_REFUSED and again.sim.requests == []                               # a failed run after a submission is never repeated


def test_F02_an_ambiguous_entry_resolved_by_client_order_id_is_adopted_not_resent(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.sim.post_modes.append("timeout_after_record")
    assert M.run(ctx) == M.EXIT_PASS
    r = result(ctx)
    assert r["entry_posts"] == 1 and sides(ctx) == ["buy", "sell"] and r["result"] == "PASS"
    assert "ambiguous BUY resolved by client_order_id" in (ctx.log_dir / M.LOG_FILE).read_text(encoding="utf-8")


# ================================================================ S / T secrets, Robinhood, model ===================================

def test_S01_secret_scan_of_log_and_result_finds_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    assert M.run(ctx) == M.EXIT_PASS and result(ctx)["secret_matches"] == 0
    blob = "".join(p.read_text(encoding="utf-8") for p in list(ctx.log_dir.glob("*.log")) + [ctx.runtime_dir / M.RESULT_FILE])
    for v in FAKE.values():
        assert v not in blob
    r = M.Redactor(["abcdefgh1234"])
    assert r("x abcdefgh1234 y") == "x *** y"


def test_S02_only_the_four_alpaca_names_are_read_from_the_env_file(tmp_path):
    env = tmp_path / "x.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in {**FAKE, **OTHER}.items()), encoding="utf-8")
    assert M.select_credentials(env) == FAKE


def test_T01_zero_robinhood_and_zero_model_calls_in_a_passing_smoke(tmp_path, monkeypatch):
    calls = []
    import anthropic
    import api.routes.portfolio as PRT
    import portfolio.provider as PP
    monkeypatch.setattr(anthropic.resources.messages.Messages, "create", lambda *a, **k: calls.append("model") or (_ for _ in ()).throw(AssertionError("model")))
    monkeypatch.setattr(PRT, "provider_factory", lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(AssertionError("robinhood")))
    for name in ("get_provider", "provider_factory", "RobinhoodProvider"):
        if hasattr(PP, name):
            monkeypatch.setattr(PP, name, lambda *a, **k: calls.append("robinhood") or (_ for _ in ()).throw(AssertionError("robinhood")))
    ctx = make_ctx(tmp_path)
    assert M.run(ctx) == M.EXIT_PASS and calls == []
    import ast
    import re
    src = (SCRIPTS / "run_aapl_paper_smoke.py").read_text(encoding="ascii")
    names = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names.append(n.module or "")
    forbidden = ("anthropic", "agents", "ai_explain", "robin_stocks", "rh_gateway", "portfolio", "paper.alpaca_order_writer", "paper.alpaca_orders", "alpaca", "subprocess", "requests")
    assert not [m for m in names if any(m == f or m.startswith(f + ".") for f in forbidden)], names
    assert not re.search(r"messages\.create|TradingClient|/v2/orders|post_order\(payload|\.submit\(", src.replace("self._orig(payload, coid)", ""))
    for m in re.finditer(r"api\.alpaca\.markets", src):
        assert src[max(0, m.start() - 6):m.start()] == "paper-"


# ================================================================ X the script as a process =========================================

class _Stub(BaseHTTPRequestHandler):
    mode = "MONITOR_ONLY"

    def do_GET(self):
        body = {"status": "ok"} if self.path == "/api/health" else {"mode": self.mode}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _script(tmp_path, url, clock="2026-10-23 09:46", env=True):
    envf = tmp_path / "smoke.env"
    envf.write_text("".join(f"{k}={v}\n" for k, v in {**FAKE, **OTHER}.items()), encoding="utf-8")
    args = [sys.executable, str(SCRIPTS / "run_aapl_paper_smoke.py"), "--app-root", str(ROOT), "--runtime-dir", str(tmp_path / "runtime"), "--log-dir", str(tmp_path / "logs"),
            "--run-date", RUN_DATE, "--server-url", url, "--simulated", "--clock-et", clock]
    if env:
        args += ["--env-file", str(envf)]
    e = {k: v for k, v in os.environ.items() if k not in FAKE and k not in OTHER}
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=e, cwd=str(ROOT))


def test_X01_the_whole_script_passes_as_a_process_loads_only_alpaca_names_and_leaks_nothing(tmp_path, stub_server):
    p = _script(tmp_path, stub_server)
    out, _ = p.communicate(timeout=180)
    assert p.returncode == 0, out
    r = json.loads((tmp_path / "runtime" / M.RESULT_FILE).read_text(encoding="utf-8"))
    assert r["result"] == "PASS" and r["entry_posts"] == 1 and r["exit_posts"] == 1 and r["paper_auto_final"] == "OFF" and r["secret_matches"] == 0
    log = (tmp_path / "logs" / M.LOG_FILE).read_text(encoding="utf-8")
    assert "credentials in this process: ['ALPACA_API_KEY', 'ALPACA_PAPER_API_KEY', 'ALPACA_PAPER_SECRET_KEY', 'ALPACA_SECRET_KEY']" in log
    blob = log + out + (tmp_path / "runtime" / M.RESULT_FILE).read_text(encoding="utf-8")
    for v in list(FAKE.values()) + list(OTHER.values()):
        assert v not in blob, v
    assert not (ROOT / "data" / "stock_agent.db").exists() or (ROOT / "data" / "stock_agent.db").stat().st_mtime < time.time() - 3600


def test_X02_two_smoke_processes_started_together_trade_once(tmp_path, stub_server):
    a = _script(tmp_path, stub_server)
    b = _script(tmp_path, stub_server, clock="2026-10-23 09:47")
    out_a, _ = a.communicate(timeout=180)
    out_b, _ = b.communicate(timeout=180)
    codes = sorted([a.returncode, b.returncode])
    assert codes == [0, 3], (out_a, out_b)
    r = json.loads((tmp_path / "runtime" / M.RESULT_FILE).read_text(encoding="utf-8"))
    assert r["result"] == "PASS" and len(r["orders_sent"]) == 2
    log = (tmp_path / "logs" / M.LOG_FILE).read_text(encoding="utf-8")
    assert log.count("RESULT PASS") == 1 and log.count("ORDER SENT 1/2") == 1 and "REFUSED" in log
    c = _script(tmp_path, stub_server, clock="2026-10-23 09:48")
    c.communicate(timeout=180)
    assert c.returncode == 3


def test_X03_a_server_that_is_down_means_no_order(tmp_path):
    p = _script(tmp_path, "http://127.0.0.1:9")
    p.communicate(timeout=180)
    r = json.loads((tmp_path / "runtime" / M.RESULT_FILE).read_text(encoding="utf-8"))
    assert p.returncode == 2 and r["orders_sent"] == [] and "server reachable" in r["failure_reason"]


@pytest.mark.skipif(os.name != "nt" or not PS, reason="Windows PowerShell")
def test_X04_powershell_wrapper_runs_the_simulated_smoke(tmp_path, stub_server):
    env = tmp_path / "w.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in FAKE.items()), encoding="utf-8")
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "run_aapl_paper_smoke.ps1"), "-RunDate", RUN_DATE, "-Simulated",
                        "-ClockEt", "2026-10-23 09:46", "-ServerUrl", stub_server, "-EnvFile", str(env), "-RuntimeDir", str(tmp_path / "rt"), "-LogDir", str(tmp_path / "lg")],
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0 and "AAPL paper smoke: PASS" in r.stdout, r.stdout + r.stderr
    r2 = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "run_aapl_paper_smoke.ps1"), "-RunDate", RUN_DATE, "-ClockEt", "2026-10-23 09:46",
                         "-EnvFile", str(env), "-RuntimeDir", str(tmp_path / "rt2"), "-LogDir", str(tmp_path / "lg2")], capture_output=True, text=True, timeout=120)
    assert r2.returncode == 2 and "only allowed with -Simulated" in r2.stdout


# ================================================================ J Task Scheduler definition ======================================

@pytest.mark.skipif(os.name != "nt" or not PS, reason="Windows PowerShell")
def test_J01_smoke_task_definition_launches_project_code_only_without_retry_or_catch_up(tmp_path):
    future = (date.today() + timedelta(days=5)).isoformat()
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "install_aapl_smoke_task.ps1"), "-Date", future, "-TaskName", "StockAgentSmokeRunTest",
                        "-PrintOnly"], capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    for needle in ("Task name: StockAgentSmokeRunTest", f"once on {future} at 09:45 ET", "only when logged on, no elevation", "Execute: powershell.exe",
                   "run_aapl_paper_smoke.ps1", f"-RunDate {future}", "-ServerUrl http://127.0.0.1:8000", "MultipleInstances=IgnoreNew", "RestartCount=0",
                   "StartWhenAvailable=False", "WakeToRun=False", "ExecutionTimeLimit=PT45M", "PrintOnly: nothing registered.", "PC sleep"):
        assert needle in out, (needle, out)
    assert "-Simulated" not in out and "/v2/orders" not in out
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "install_aapl_smoke_task.ps1"), "-Date", "2020-01-03", "-TaskName", "StockAgentSmokeRunTest",
                        "-PrintOnly"], capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "in the past" in (r.stdout + r.stderr)
    r = subprocess.run([PS, "-NoProfile", "-Command", "[bool](Get-ScheduledTask -TaskName StockAgentSmokeRunTest -ErrorAction SilentlyContinue)"], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "False"
