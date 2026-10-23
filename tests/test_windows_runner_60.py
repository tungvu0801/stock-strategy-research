"""
Windows background runner (scripts/start|stop|check|install_stock_agent*.ps1 + scripts/run_stock_agent.py) — tests A–J against REAL
processes started in an OFFLINE configuration: fake credentials, simulated market data, a temporary database / runtime / log folder,
the runner's loopback-only network guard and a free local port. No broker is reachable from these processes; no order can be sent.

  A one instance starts · B a second start is refused · C stop stops only our process · D stale PID files · E logs · F health check
  G restart restores the persisted Stage 6.0 state and reconciles before any write · H starting sends nothing · I no secret in any log
  J the Task Scheduler definition · S supervisor (foreground) restarts after a crash, never after a stop · P paper-only / static checks
  K a plain checkout (no .venv, no worktree): runs on the STOCK_AGENT_PYTHON interpreter; without any interpreter source it fails clearly

Every child PowerShell process gets the RUNNING test interpreter through the scripts' existing STOCK_AGENT_PYTHON override
(scripts/stock_agent_common.ps1 Resolve-SAPython: -Python, then STOCK_AGENT_PYTHON, then a repository / main-checkout .venv, else
"No Python environment found"), so the tests never depend on a developer .venv: a CI checkout has none.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PS = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not PS, reason="the Windows runner needs Windows PowerShell")
FAKE = {"ALPACA_PAPER_API_KEY": "RUNNERTEST-paperkey-7f3a91c2", "ALPACA_PAPER_SECRET_KEY": "RUNNERTEST-papersecret-b81d44e0",
        "ALPACA_API_KEY": "RUNNERTEST-datakey-55aa66bb", "ALPACA_SECRET_KEY": "RUNNERTEST-datasecret-0c0ffee1"}
OVERRIDES = {"TRADER_DATA_MODE": "simulated", "EVENT_WARMUP_ON_STARTUP": "false", "PORTFOLIO_AWARENESS_ENABLED": "false"}
PY_OVERRIDE = "STOCK_AGENT_PYTHON"                                   # the scripts' existing interpreter override (Resolve-SAPython)


def child_env(python=sys.executable):
    """The environment of a child PowerShell process: the running test interpreter through STOCK_AGENT_PYTHON (passed as an environment
    value, never spliced into a command line, so a path with spaces needs no quoting). python=None removes the override."""
    env = dict(os.environ)
    env.pop(PY_OVERRIDE, None)
    if python is not None:
        env[PY_OVERRIDE] = str(python)
    return env


def ps(script, *args, timeout=240, env=None, scripts=SCRIPTS):
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(scripts / script), *[str(a) for a in args]],
                       capture_output=True, text=True, timeout=timeout, cwd=str(scripts.parent), env=child_env() if env is None else env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_get(port, path):
    """A raw loopback GET (the conftest blocks socket.create_connection / getaddrinfo in the test process)."""
    s = socket.socket()
    s.settimeout(20)
    s.connect(("127.0.0.1", port))
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head[:80]
    return json.loads(body)


def runner_processes(marker):
    code, out = ps_inline(f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine -like '*run_stock_agent.py*' -and $_.CommandLine -like '*{marker}*' }}) | "
                          f"ForEach-Object {{ $_.ProcessId }}")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def ps_inline(command, timeout=60):
    r = subprocess.run([PS, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout or ""


def write_env(path, values):
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


class Rig:
    def __init__(self, base: Path):
        base = base.parent / f"{base.name}-{uuid.uuid4().hex[:10]}"          # a unique folder name doubles as a process marker
        self.base = base
        self.runtime, self.logs, self.db = base / "runtime", base / "logs", base / "bot.db"
        self.env, self.overrides = base / "fake.env", base / "overrides.env"
        base.mkdir(parents=True, exist_ok=True)
        write_env(self.env, FAKE)
        write_env(self.overrides, OVERRIDES)
        self.port = free_port()

    @property
    def dirs(self):
        return ["-RuntimeDir", self.runtime, "-LogDir", self.logs]

    def start(self, *extra):
        return ps("start_stock_agent.ps1", "-Port", self.port, "-EnvFile", self.env, "-OverridesFile", self.overrides, "-DbPath", self.db, "-LoopbackOnly",
                  *self.dirs, *extra)

    def stop(self):
        return ps("stop_stock_agent.ps1", *self.dirs)

    def check(self):
        return ps("check_stock_agent.ps1", *self.dirs)

    def pid_info(self):
        p = self.runtime / "stock_agent.pid"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def log_text(self):
        return "".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(self.logs.glob("*.log")))

    def wait_cycle(self, seconds=45):
        t0 = time.time()
        while time.time() - t0 < seconds:
            st = http_get(self.port, "/api/execution/status")
            if st.get("last_cycle_at"):
                return st
            time.sleep(1)
        raise AssertionError("the executor loop did not complete a cycle")

    def cleanup(self):
        for pid in runner_processes(self.base.name):
            ps_inline(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    r = Rig(tmp_path_factory.mktemp("runner60"))
    had_root_db = (ROOT / "data" / "stock_agent.db").exists()
    yield r
    r.stop()
    r.cleanup()
    assert had_root_db or not (ROOT / "data" / "stock_agent.db").exists(), "the runner tests created data/stock_agent.db in the repository"


# ================================================================ P paper-only / static ============================================

def test_P01_scripts_are_paper_only_ascii_and_carry_no_secret_or_live_switch():
    files = sorted(SCRIPTS.glob("*.ps1")) + [SCRIPTS / "run_stock_agent.py"]
    assert {f.name for f in files} >= {"start_stock_agent.ps1", "stop_stock_agent.ps1", "check_stock_agent.ps1", "install_stock_agent_task.ps1", "stock_agent_common.ps1",
                                       "run_stock_agent.py"}
    for f in files:
        raw = f.read_bytes()
        assert all(b < 128 for b in raw), f"{f.name} must stay ASCII (Windows PowerShell 5.1 reads BOM-less files as ANSI)"
        text = raw.decode("ascii")
        for m in re.finditer(r"api\.alpaca\.markets", text):
            assert text[max(0, m.start() - 6):m.start()] == "paper-", f.name
        assert not re.search(r"(?i)robinhood|robin_stocks|rh_gateway|TradingClient|LIVE_CONFIRM|AUTO_LIVE|live[_ ]mode|base_url|url_override|/v2/orders", text), f.name
        assert not re.search(r"(?i)(PK|AK|SK)[A-Z0-9]{16,}|sk-ant-|Bearer ", text), f.name
        assert "APCA-API" not in text, f.name
    py = (SCRIPTS / "run_stock_agent.py").read_text(encoding="ascii")
    assert not re.search(r"^\s*(import subprocess|from subprocess)", py, re.M)
    assert 'XB.PAPER_HOST == "paper-api.alpaca.markets"' in py and "load_dotenv(envp, override=False)" in py


def _launcher():
    spec = importlib.util.spec_from_file_location("run_stock_agent_under_test", SCRIPTS / "run_stock_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_P02_log_redaction_replaces_every_secret_looking_value():
    m = _launcher()
    r = m.Redactor()
    assert r.refresh({"ALPACA_PAPER_API_KEY": "abc123secretvalue", "ROBINHOOD_PASSWORD": "hunter2hunter2", "ROBINHOOD_USERNAME": "someone@example.com", "SHORT_KEY": "abc",
                      "TRADER_DATA_MODE": "simulated"}) == 3
    out = r("key=abc123secretvalue pw=hunter2hunter2 user=someone@example.com mode=simulated short=abc")
    assert out == "key=*** pw=*** user=*** mode=simulated short=abc"
    assert m.LOOPBACK >= {"127.0.0.1", "localhost", "::1"} and m.EXIT_DUPLICATE == 3


# ================================================================ D stale PID files =================================================

def test_D01_stale_pid_files_are_cleaned_and_no_unrelated_process_is_touched(tmp_path):
    r = Rig(tmp_path / "stale")
    r.runtime.mkdir(parents=True)
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    pid_file = r.runtime / "stock_agent.pid"
    pid_file.write_text(json.dumps({"pid": done.pid, "instance_id": "deadbeef" * 4, "port": r.port}), encoding="utf-8")
    code, out = r.check()
    assert code == 1 and "Stock Agent: NOT RUNNING" in out and "stale PID file" in out, out
    code, out = r.stop()
    assert code == 0 and "stale PID file" in out and not pid_file.exists(), out
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "run_stock_agent.py"])      # alive, looks similar, not ours
    try:
        pid_file.write_text(json.dumps({"pid": decoy.pid, "instance_id": "cafebabe" * 4, "port": r.port}), encoding="utf-8")
        code, out = r.stop()
        assert code == 0 and "no process was touched" in out and not pid_file.exists(), out
        time.sleep(1)
        assert decoy.poll() is None, "the stop script touched an unrelated process"
    finally:
        decoy.kill()


# ================================================================ A–I one real background instance ==================================

def test_A01_start_launches_exactly_one_instance_and_cleans_a_stale_pid_file(rig):
    rig.runtime.mkdir(parents=True, exist_ok=True)
    (rig.runtime / "stock_agent.pid").write_text(json.dumps({"pid": os.getpid(), "instance_id": "0" * 32, "port": rig.port}), encoding="utf-8")   # alive but not a runner
    code, out = rig.start()
    assert code == 0, out
    assert "removed a stale PID file" in out and "Stock Agent: STARTED" in out, out
    info = rig.pid_info()
    assert info and info["port"] == rig.port and info["instance_id"] != "0" * 32
    procs = runner_processes(info["instance_id"])
    assert info["pid"] in procs and len(procs) <= 2, procs                        # the venv launcher stub + the interpreter: one instance
    assert http_get(rig.port, "/api/health")["status"] == "ok"


def test_B01_a_second_start_is_refused(rig):
    before = rig.pid_info()
    code, out = rig.start()
    assert code == 3 and "ALREADY RUNNING" in out, out
    assert rig.pid_info() == before and len(runner_processes(before["instance_id"])) <= 2


def test_F01_health_check_reports_running_with_mode_safety_and_reconciliation(rig):
    code, out = rig.check()
    assert code == 0, out
    for line in ("Stock Agent: RUNNING", "Server: reachable", "Mode: Monitor only", "Reconciliation: Healthy", "Executor loop: running", "Account data", "Account refresh loop: running"):
        assert line in out, (line, out)
    assert re.search(r"Safety: (Trading allowed|New trades paused|Trading stopped)", out), out


def test_H01_starting_the_bot_sends_nothing_and_only_reads_the_paper_account(rig):
    st = rig.wait_cycle()
    assert st["mode"] == "MONITOR_ONLY" and st["last_cycle_result"] == "IDLE" and st["pending_entries"] == 0 and st["open_positions"] == 0
    intents = http_get(rig.port, "/api/execution/intents")
    assert intents["intents"] == [] and intents["orders"] == []
    # Stage 6.0.1: the only outbound attempt is the read-only account refresh (3 GETs to the paper host; blocked by this offline run's guard)
    blocked = re.findall(r"network guard: blocked (\S+)", rig.log_text())
    assert blocked and set(blocked) == {"paper-api.alpaca.markets:443"}, blocked
    deadline = time.time() + 45                                                  # the startup refresh retries its reads (2 s, 5 s) before it records the failure
    hist = http_get(rig.port, "/api/account-refresh/history")
    while hist["total"] < 1 and time.time() < deadline:
        time.sleep(1)
        hist = http_get(rig.port, "/api/account-refresh/history")
    assert hist["total"] >= 1 and {a["result"] for a in hist["attempts"]} == {"FAILED"} and hist["attempts"][-1]["trigger"] == "STARTUP"
    acct = http_get(rig.port, "/api/account-refresh")
    assert acct["status"] == "UNAVAILABLE" and acct["fresh"] is False and acct["loop"]["running"] is True and acct["loop"]["lease"] == "alpaca_account_refresher"


def test_E01_logs_record_startup_scheduler_mode_and_paper_broker(rig):
    out = (rig.logs / "stock_agent_stdout.log").read_text(encoding="utf-8")
    for needle in ("runner: startup", "loaded 4 environment names", "paper broker - Alpaca PAPER host paper-api.alpaca.markets only, credentials configured: yes (no broker call at startup)",
                   "execution mode at startup: MONITOR_ONLY", "server listening on http://127.0.0.1:", "paper executor loop running (lease paper_executor)",
                   "setup evaluator loop running", "loopback-only network guard installed", "account refresh loop running (lease alpaca_account_refresher"):
        assert needle in out, needle
    assert (rig.logs / "stock_agent_stderr.log").exists() and (rig.logs / "stock_agent_runner.log").exists()


def test_I01_no_secret_value_or_auth_header_in_any_log(rig):
    text = rig.log_text()
    for v in FAKE.values():
        assert v not in text
    assert "APCA-API" not in text and "Authorization" not in text


def test_C01_stop_stops_only_our_process_and_shuts_down_cleanly(rig):
    info = rig.pid_info()
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "run_stock_agent.py"])
    try:
        code, out = rig.stop()
        assert code == 0 and f"STOPPED cleanly (PID {info['pid']})" in out, out
        assert runner_processes(info["instance_id"]) == [] and rig.pid_info() is None
        assert decoy.poll() is None, "the stop script touched an unrelated process"
    finally:
        decoy.kill()
    out = (rig.logs / "stock_agent_stdout.log").read_text(encoding="utf-8")
    assert "stop requested by scripts/stop_stock_agent.ps1" in out and "Application shutdown complete" in out and "runner: shutdown complete" in out
    code, out = rig.check()
    assert code == 1 and "NOT RUNNING" in out


def test_G01_restart_restores_the_persisted_mode_and_reconciles_before_any_write(rig):
    sys.path.insert(0, str(ROOT))
    from execution.store import ExecutionStore
    ExecutionStore(rig.db).set_mode("PAPER_AUTO", datetime.now(timezone.utc), "test", "restart test", {"paper": True, "simulated": False})
    code, out = rig.start()
    assert code == 0, out
    log = (rig.logs / "stock_agent_stdout.log").read_text(encoding="utf-8")
    assert "execution mode at startup: PAPER_AUTO (persisted); the executor reconciles with the broker before any write" in log
    st = rig.wait_cycle()
    assert st["mode"] == "PAPER_AUTO" and st["mode_label"] == "Paper Auto"
    deadline = time.time() + 30
    while time.time() < deadline and st.get("reconciliation_healthy"):
        time.sleep(1)
        st = http_get(rig.port, "/api/execution/status")
    assert st["reconciliation_healthy"] is False and st["attention"] is True       # the broker is unreachable here: entries stay blocked
    findings = http_get(rig.port, "/api/execution/findings")
    assert "BROKER_UNAVAILABLE" in [f["kind"] for f in findings["findings"]]
    assert http_get(rig.port, "/api/execution/intents")["intents"] == []          # reconciliation came first; nothing was written or sent
    assert "network guard: blocked paper-api.alpaca.markets:443" in rig.log_text()   # the only outbound attempt: the paper reconciliation read
    code, out = rig.check()
    assert "Mode: Paper Auto (needs attention)" in out and "Reconciliation: Needs attention" in out, out
    code, out = rig.stop()
    assert code == 0 and "STOPPED cleanly" in out, out
    for v in FAKE.values():
        assert v not in rig.log_text()


# ================================================================ S foreground supervisor (Task Scheduler mode) =======================

def test_S01_foreground_supervisor_restarts_after_a_crash_but_never_after_a_stop(tmp_path):
    r = Rig(tmp_path / "fg")
    args = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "start_stock_agent.ps1"), "-Foreground", "-RestartDelaySeconds", "2", "-MaxRestarts", "2",
            "-Port", str(r.port), "-EnvFile", str(r.env), "-OverridesFile", str(r.overrides), "-DbPath", str(r.db), "-LoopbackOnly", "-RuntimeDir", str(r.runtime), "-LogDir", str(r.logs)]
    sup = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=child_env())
    try:
        def wait_up(old=None, seconds=90):
            t0 = time.time()
            while time.time() - t0 < seconds:
                info = r.pid_info()
                if info and info.get("instance_id") != old:
                    try:
                        if http_get(r.port, "/api/health")["status"] == "ok":
                            return info
                    except OSError:
                        pass
                time.sleep(0.5)
            raise AssertionError("the supervised server did not come up")
        first = wait_up()
        ps_inline(f"Stop-Process -Id {first['pid']} -Force")                          # a crash
        second = wait_up(old=first["instance_id"])
        assert second["pid"] != first["pid"] and sup.poll() is None
        runner_log = (r.logs / "stock_agent_runner.log").read_text(encoding="utf-8")
        assert "restarting in 2 s (1 of 2); the executor reconciles with the broker before any write" in runner_log
        code, out = r.stop()
        assert code == 0 and "STOPPED cleanly" in out, out
        assert sup.wait(timeout=60) == 0                                              # a deliberate stop is never restarted
        assert runner_processes(r.base.name) == []
        runner_log = (r.logs / "stock_agent_runner.log").read_text(encoding="utf-8")
        assert "after a stop request" in runner_log and runner_log.count("foreground start") == 2
    finally:
        if sup.poll() is None:
            sup.kill()
        r.cleanup()


# ================================================================ J Task Scheduler definition ======================================

def test_J01_task_scheduler_definitions_are_generated_without_registering_anything():
    name = "StockAgentRunnerTest"
    code, out = ps("install_stock_agent_task.ps1", "-Trigger", "Logon", "-TaskName", name, "-PrintOnly")
    assert code == 0, out
    for needle in (f"Task name: {name}", "Trigger: at logon of", "only when logged on, no elevation", "Execute: powershell.exe", "-WindowStyle Hidden",
                   "start_stock_agent.ps1", "-Foreground", "-Port 8000", "MultipleInstances=IgnoreNew", "RestartCount=3", "RestartInterval=PT1M",
                   "ExecutionTimeLimit=PT0S", "WakeToRun=False", "PrintOnly: nothing registered."):
        assert needle in out, (needle, out)
    assert f'-python "{sys.executable}"'.lower() in out.lower(), out             # the task hands the resolved interpreter to the project start script
    future = (date.today() + timedelta(days=3)).isoformat()
    code, out = ps("install_stock_agent_task.ps1", "-Trigger", "Once", "-Date", future, "-EasternTime", "09:35", "-TaskName", name, "-PrintOnly")
    assert code == 0 and f"once on {future} at 09:35 ET" in out and "PrintOnly: nothing registered." in out, out
    code, out = ps("install_stock_agent_task.ps1", "-Trigger", "Weekdays", "-EasternTime", "09:20", "-TaskName", name, "-PrintOnly")
    assert code == 0 and "Monday-Friday at 09:20 ET" in out, out
    code, out = ps("install_stock_agent_task.ps1", "-Trigger", "Once", "-Date", "2020-01-02", "-EasternTime", "09:35", "-TaskName", name, "-PrintOnly")
    assert code != 0 and "in the past" in out, out
    code, out = ps_inline(f"[bool](Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue)")
    assert out.strip() == "False"                                                     # PrintOnly registered nothing
    for text in (out,):
        assert "/v2/orders" not in text


# ================================================================ K plain checkout (a CI runner: no .venv, no worktree) =================

def plain_checkout(dst: Path) -> Path:
    """A project tree like a CI checkout: the tracked files only - no .venv, no .git entry (so no worktree main-checkout .venv either), no .env."""
    names = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    for rel in filter(None, names):
        src = ROOT / rel
        if src.is_file():
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
    for absent in (".venv", ".git", ".env"):
        assert not (dst / absent).exists(), absent
    assert (dst / "scripts" / "start_stock_agent.ps1").is_file() and (dst / "scripts" / "run_stock_agent.py").is_file()
    return dst


def test_K01_a_plain_checkout_without_any_venv_starts_on_the_STOCK_AGENT_PYTHON_interpreter_and_sends_nothing(tmp_path):
    app = plain_checkout(tmp_path / "checkout")
    scripts = app / "scripts"
    r = Rig(tmp_path / "plain")
    try:
        code, out = ps("start_stock_agent.ps1", "-Port", r.port, "-EnvFile", r.env, "-OverridesFile", r.overrides, "-DbPath", r.db, "-LoopbackOnly", *r.dirs, scripts=scripts)
        assert code == 0 and "Stock Agent: STARTED" in out, out
        info = r.pid_info()
        procs = runner_processes(info["instance_id"])
        assert info["pid"] in procs and len(procs) <= 2, procs
        assert http_get(r.port, "/api/health")["status"] == "ok"
        runner_log = (r.logs / "stock_agent_runner.log").read_text(encoding="utf-8").lower()
        assert f"python {sys.executable}".lower() in runner_log, runner_log                       # resolved from STOCK_AGENT_PYTHON
        stdout = (r.logs / "stock_agent_stdout.log").read_text(encoding="utf-8").lower()
        assert f"app root {app.resolve()}".lower() in stdout and f"python {sys.executable}".lower() in stdout, stdout[:600]   # the plain checkout's own code
        code, out = ps("check_stock_agent.ps1", *r.dirs, scripts=scripts)
        assert code == 0, out
        for line in ("Stock Agent: RUNNING", "Server: reachable", "Mode: Monitor only", "Reconciliation: Healthy", "Executor loop: running"):
            assert line in out, (line, out)
        st = r.wait_cycle()
        assert st["mode"] == "MONITOR_ONLY" and st["pending_entries"] == 0 and st["open_positions"] == 0
        intents = http_get(r.port, "/api/execution/intents")
        assert intents["intents"] == [] and intents["orders"] == []                                      # no order of any kind
        assert "network guard: blocked" not in r.log_text()                                             # no outbound connection was even attempted
        code, out = ps("stop_stock_agent.ps1", *r.dirs, scripts=scripts)
        assert code == 0 and f"STOPPED cleanly (PID {info['pid']})" in out, out
        assert runner_processes(info["instance_id"]) == []
    finally:
        r.cleanup()


def test_K02_without_STOCK_AGENT_PYTHON_and_without_any_venv_every_entry_point_fails_clearly_and_starts_nothing(tmp_path):
    app = plain_checkout(tmp_path / "checkout")
    scripts = app / "scripts"
    r = Rig(tmp_path / "nopython")
    bare = child_env(python=None)
    code, out = ps("start_stock_agent.ps1", "-Port", r.port, "-EnvFile", r.env, "-OverridesFile", r.overrides, "-DbPath", r.db, "-LoopbackOnly", *r.dirs,
                   scripts=scripts, env=bare)
    assert code == 1 and "Stock Agent: START FAILED - No Python environment found" in out, out
    assert ".venv\\scripts\\python.exe" in out.lower(), out                                          # it names where it looked; no PATH fallback
    assert r.pid_info() is None and runner_processes(r.base.name) == []
    code, out = ps("install_stock_agent_task.ps1", "-Trigger", "Logon", "-TaskName", "StockAgentRunnerTest", "-PrintOnly", scripts=scripts, env=bare)
    assert code != 0 and "No Python environment found" in out, out
    code, out = ps_inline("[bool](Get-ScheduledTask -TaskName StockAgentRunnerTest -ErrorAction SilentlyContinue)")
    assert out.strip() == "False"
