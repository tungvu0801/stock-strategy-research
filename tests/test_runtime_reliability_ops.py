"""
Ops runtime reliability (after Stage 5.4.1; tests/freeze_ledger.py BASE_OPS / NEW_OPS / EDITED_OPS). Operations only - no execution,
strategy, risk, Paper Auto, startup-demotion or broker behaviour changes.

  R01 the runner's suspend detector reports only a wall-clock jump above its threshold (injected clock)
  R02 exit logging names the signal, then uvicorn's own handler runs unchanged
  R03 the runner stays ASCII, imports no subprocess, keeps its markers; the new block is log only
  R04 the tracked wrapper, run from a fake live tree: the launch line is the first log line, one RESULT line, exit 1, nothing started
  R05 the wrapper is the live wrapper with parameters, a launch line before any read, a trap, GET only
  R06 the task installer -PrintOnly shows the three triggers and the restart policy and registers nothing
  R07 the check script prints the scheduler / history / resume lines in the NOT RUNNING path without changing its exit code
  F01 the change set since main is exactly NEW_OPS + EDITED_OPS; start / stop / common scripts byte-identical; the runner diff adds only
  F02 every bookkeeping edit of an earlier stage's test is replayed exactly; the ledger changes only the declared entries
"""
import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import freeze_ledger as FL  # noqa: E402

SCRIPTS = ROOT / "scripts"
PS = shutil.which("powershell.exe")
needs_ps = pytest.mark.skipif(PS is None, reason="Windows PowerShell only")


def ps(script, *args, timeout=240):
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / script), *[str(a) for a in args]],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def runner_module():
    spec = importlib.util.spec_from_file_location("run_stock_agent_ops_under_test", SCRIPTS / "run_stock_agent.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    out = subprocess.run(["git", "show", f"{FL.BASE_OPS}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_OPS} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def now(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


# ================================================================ R runner / scripts ==============================================

def test_R01_suspend_detector_reports_only_a_wall_clock_jump_above_the_threshold():
    m = runner_module()
    d = m.SuspendDetector(threshold_s=120)
    assert d.tick(1000.0) is None                                              # the first tick has nothing to compare with
    assert all(d.tick(1000.0 + i) is None for i in range(1, 60))               # one-second ticks
    assert d.tick(1059.0 + 120.0) is None                                      # exactly the threshold is not a suspend
    assert d.tick(1179.0 + 3600.5) == pytest.approx(3600.5)                    # the machine slept an hour
    assert d.tick(1179.0 + 3600.5 + 1.0) is None                               # back to normal
    assert m.SUSPEND_GAP_S == 120.0 and m.SuspendDetector().threshold_s == 120.0


def test_R02_exit_logging_names_the_signal_then_runs_uvicorns_own_handler(caplog):
    m = runner_module()
    calls = []

    class FakeServer:
        def handle_exit(self, sig, frame):
            calls.append((sig, frame))

    s = FakeServer()
    with caplog.at_level(logging.INFO, logger="stock_agent.runner"):
        m.install_exit_logging(s)
        s.handle_exit(int(m.signal.SIGTERM), None)
    assert calls == [(int(m.signal.SIGTERM), None)]                            # uvicorn's handler still runs, with the same arguments
    assert any("runner: shutting down (signal SIGTERM)" in r.getMessage() for r in caplog.records)


def test_R03_runner_stays_ascii_log_only_and_keeps_its_markers():
    py = (SCRIPTS / "run_stock_agent.py").read_text(encoding="ascii")
    for s in ("class SuspendDetector", "def install_exit_logging", "runner: resumed after", "runner: shutting down (signal", "runner: interpreter exit (atexit",
              "install_exit_logging(server)", "suspend.tick(time.time())", "runner: process exit", "runner: shutdown complete", "runner: stop requested"):
        assert s in py, s
    assert not re.search(r"^\s*(import subprocess|from subprocess)", py, re.M)
    block = py[py.index("SUSPEND_GAP_S"):py.index("def _now()")]
    assert not re.search(r"set_mode|post_order|insert_intent|ExecutionStore|should_exit\s*=|os\._exit|sys\.exit", block)


@needs_ps
def test_R04_wrapper_logs_a_launch_line_first_then_one_result_and_starts_nothing_on_a_wrong_tree(tmp_path):
    live = tmp_path / "stock-agent-live"
    (live / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "start_market_morning.ps1", live / "scripts" / "start_market_morning.ps1")   # from <live>\scripts: location OK, no .git: refused
    log = tmp_path / "mm.log"
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(live / "scripts" / "start_market_morning.ps1"), "-DryRun",
                        "-Live", str(live), "-MainCheckout", str(tmp_path / "main"), "-LogFile", str(log)], capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 1, out
    lines = log.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 3, lines
    assert " DRY-RUN launch (pid " in lines[0] and "dry-run True" in lines[0] and "start_market_morning.ps1" in lines[0], lines[0]
    assert " DRY-RUN start (user " in lines[1], lines[1]
    assert "RESULT:" in lines[2] and "is not the stock-agent-live worktree (exit 1)" in lines[2], lines[2]
    assert "launch (pid" in out and "RESULT:" in out                           # the same lines on stdout
    assert not list((live / "runtime").glob("*")) if (live / "runtime").exists() else True


def test_R05_wrapper_is_the_live_wrapper_with_parameters_a_launch_line_and_a_trap():
    t = (SCRIPTS / "start_market_morning.ps1").read_text(encoding="ascii")
    for s in ('[string]$Live = "C:\\Data Analyst Py\\DA IDX\\stock-agent-live"', '[string]$MainCheckout = "C:\\Data Analyst Py\\DA IDX\\stock-agent"', "[int]$Port = 8000",
              '[int]$ReadBackSeconds = 150', 'Log ("launch (pid', "trap {", "RESULT: unexpected error", "# ---- 1. canonical live path", "# ---- 2. paper environment",
              "# ---- 3. config", "# ---- 4. duplicate guard", "# ---- 5. start", "# ---- 6. read back", "if ($DryRun) { Finish 0", 'if ($policy -ne "MANUAL_ARM")',
              "scheduled Paper Auto must stay off", "not starting a second instance", 'Finish 0 "started in MONITOR_ONLY; Paper Auto OFF until a person arms it"',
              '"ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY"', "Set-StrictMode -Off"):
        assert s in t, s
    assert t.index('Log ("launch (pid') < t.index("$EnvValues = Read-DotEnv $EnvFile") < t.index('Log ("start (user')   # launch line before any file is read
    assert "-Method Post" not in t and "Invoke-WebRequest" not in t and t.count("-Method Get") == 1                        # GET only
    assert "auto/enable" not in t.replace("never calls /api/execution/auto/enable", "")
    assert t.count("Finish 0 ") == 4 and t.count("Finish 1 ") == 11 and t.count("Finish 2 ") == 1 and t.count("Finish 3 ") == 3


@needs_ps
def test_R06_installer_print_only_shows_three_triggers_and_the_restart_policy_and_registers_nothing():
    name = "StockAgentOpsTest-" + uuid.uuid4().hex[:8]
    code, out = ps("install_market_morning_task.ps1", "-PrintOnly", "-TaskName", name, "-AppRoot", str(ROOT))
    assert code == 0, out
    for needle in (f"Task name: {name}", "weekly Monday-Friday at 09:15 ET", "at logon of", "on resume from sleep (System log, Microsoft-Windows-Power-Troubleshooter event 1, delay PT2M)",
                   "MultipleInstances=IgnoreNew", "RestartCount=3", "RestartInterval=PT10M", "ExecutionTimeLimit=PT30M", "StartWhenAvailable=True", "WakeToRun=True",
                   "Event subscription: <QueryList>", "EventID=1", "-WindowStyle Hidden", "start_market_morning.ps1", "only when logged on, no elevation",
                   "wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true", "PrintOnly: nothing registered."):
        assert needle in out, (needle, out)
    r = subprocess.run([PS, "-NoProfile", "-Command", f"[bool](Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue)"], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "False"                                         # PrintOnly registered nothing
    code, out = ps("install_market_morning_task.ps1", "-PrintOnly", "-TaskName", name, "-NoLogonTrigger", "-NoResumeTrigger")
    assert code == 0 and "at logon of" not in out and "on resume" not in out and "Event subscription" not in out, out


@needs_ps
def test_R07_check_script_prints_scheduler_history_and_resume_lines_without_changing_its_exit_code(tmp_path):
    rt, lg = tmp_path / "runtime", tmp_path / "logs"
    rt.mkdir()
    lg.mkdir()
    code, out = ps("check_stock_agent.ps1", "-AppRoot", str(ROOT), "-RuntimeDir", str(rt), "-LogDir", str(lg))
    assert code == 1 and "Stock Agent: NOT RUNNING" in out, out
    assert re.search(r"^Scheduler: ", out, re.M) and re.search(r"^Scheduler history: (enabled|DISABLED|unknown)", out, re.M) and re.search(r"^Last resume from sleep: ", out, re.M), out
    t = (SCRIPTS / "check_stock_agent.ps1").read_text(encoding="ascii")
    assert "0xC000013A" in t and "terminated by the scheduler" in t and t.count("Write-SASchedulerStatus") == 3   # defined once, called in both exit paths
    assert "-Method Post" not in t and "Register-ScheduledTask" not in t and "wevtutil sl" not in t.replace("enable once, elevated: wevtutil sl", "")


# ================================================================ F freeze / change set ===========================================

def test_F01_the_change_set_is_exactly_the_declared_ops_files_and_touches_no_trading_code():
    assert FL.BASE_OPS == "2fe29f3a48359a365a1506c9f70093129be72602" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_OPS}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_OPS).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_OPS | FL.EDITED_OPS, {"unexpected": sorted(changed - FL.NEW_OPS - FL.EDITED_OPS), "missing": sorted((FL.NEW_OPS | FL.EDITED_OPS) - changed)}
    assert FL.SEMANTIC_OPS == {}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_OPS).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_OPS and {p for p in changed if p in in_base} == FL.EDITED_OPS
    for d in ("execution", "safety", "setups", "ppe", "proposals", "autorun", "evidence", "decision_flow", "dynamic_universe", "unified_portfolio", "trader_view", "api",
              "frontend", "database", "forward_validation", "forward_capture", "account_refresh", "cash_flow", "paper", "portfolio", "browser_tests", "rh_gateway"):
        assert not [p for p in changed if p.startswith(d + "/")], d
    assert not changed & {"config.py", "requirements.txt", "README.md", "watchlist.txt", ".github/workflows/tests.yml", "tests/conftest.py"}
    for rel in ("scripts/start_stock_agent.ps1", "scripts/stop_stock_agent.ps1", "scripts/stock_agent_common.ps1", "scripts/install_stock_agent_task.ps1",
                "scripts/run_aapl_paper_smoke.ps1", "scripts/install_aapl_smoke_task.ps1", "scripts/run_aapl_paper_smoke.py"):
        assert now(rel) == at_base(rel), rel                                   # start / stop / the shared helpers / the other installers: untouched
    diff = _git("diff", FL.BASE_OPS, "--", "scripts/run_stock_agent.py").splitlines()
    assert [l for l in diff if l.startswith("-") and not l.startswith("---")] == []          # the runner edit only adds lines
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    assert added and not any(re.search(r"should_exit\s*=|set_mode|post_order|insert_intent|os\._exit|sys\.exit\(|subprocess", l) for l in added), added


# every ops edit of a file no ledger entry can hold (an earlier stage's own test file): (text on main, text now), replayed by F02
REPLAY_OPS = {'tests/test_bot_activity_68.py': [('FL.NEW_610C | FL.NEW_541, {', 'FL.NEW_610C | FL.NEW_541 | FL.NEW_OPS, {'),
                                   ('- FL.NEW_610C - FL.NEW_541),', '- FL.NEW_610C - FL.NEW_541 - FL.NEW_OPS),'),
                                   ('FL.NEW_610C | FL.NEW_541) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
                                    'FL.NEW_610C | FL.NEW_541 | FL.NEW_OPS) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / ops reliability\n')],
 'tests/test_decision_flow_610c.py': [('    later = FL.NEW_541 | FL.EDITED_541                                                         # Stage 5.4.1, pinned by its own F tests\n',
                                       '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_OPS | FL.EDITED_OPS                           # Stage 5.4.1 / ops reliability, each pinned by its own F '
                                       'tests\n')],
 'tests/test_evidence_gate_541.py': [('\n\n# every Stage 5.4.1 edit of a file no ledger entry can hold',
                                      '\n'
                                      '\n'
                                      'def post(rel):\n'
                                      '    """main after PR #36 (Stage 5.4.1 merged): what Stage 5.4.1 left, whatever a later change set changed since."""\n'
                                      '    out = subprocess.run(["git", "show", f"{FL.BASE_OPS}:{rel}"], cwd=ROOT, capture_output=True).stdout\n'
                                      '    assert out, f"{rel} is not in {FL.BASE_OPS} (fetch-depth 0 is required)"\n'
                                      '    return out.decode("utf-8").replace("\\r\\n", "\\n")\n'
                                      '\n'
                                      '\n'
                                      '# every Stage 5.4.1 edit of a file no ledger entry can hold'),
                                     ('    assert changed == FL.NEW_541 | FL.EDITED_541, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541), "missing": sorted(FL.NEW_541 | FL.EDITED_541 - '
                                      'changed)}\n',
                                      '    later = FL.NEW_OPS | FL.EDITED_OPS                                                         # ops runtime reliability, pinned by its own F tests\n'
                                      '    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later),\n'
                                      '                                                            "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
                                     ('    for rel, edits in REPLAY_541.items():\n        text = (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n")\n',
                                      '    for rel, edits in REPLAY_541.items():\n        text = post(rel)\n'),
                                     ('    fl, base = (ROOT / "tests/freeze_ledger.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n"), at_base("tests/freeze_ledger.py")\n',
                                      '    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n'),
                                     ('    assert set(FL.LEDGER) - set(old) == {"trader_view/research.py"}                       # one new key: the corrected reader\n'
                                      '    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_541 & set(old)          # every re-hash is a declared Stage 5.4.1 edit\n',
                                      '    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 5.4.1 left (main after PR #36)\n'
                                      '    assert set(mine) - set(old) == {"trader_view/research.py"}                            # one new key: the corrected reader\n'
                                      '    assert {k for k in old if old[k] != mine[k]} == FL.EDITED_541 & set(old)              # every re-hash is a declared Stage 5.4.1 edit\n')],
 'tests/test_forward_paper_610a.py': [('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541                         # Stage 6.10C / 5.4.1, each pinned by its own F tests\n',
                                       '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_OPS | FL.EDITED_OPS   # Stage 6.10C / 5.4.1 / ops reliability, each pinned by '
                                       'its own F tests\n')],
 'tests/test_robinhood_portfolio_69.py': [('FL.NEW_541 | FL.EDITED_541   # Stage 6.9.1 (FL.SEMANTIC_691)',
                                           'FL.NEW_541 | FL.EDITED_541 | FL.NEW_OPS | FL.EDITED_OPS   # ops reliability (SEMANTIC_OPS empty), Stage 6.9.1 (FL.SEMANTIC_691)')],
 'tests/test_unified_exposure_gate_691.py': [('FL.NEW_541 | FL.EDITED_541   # Stage 6.10A / 6.10C / 5.4.1, each pinned by its own F tests',
                                              'FL.NEW_541 | FL.EDITED_541 | FL.NEW_OPS | FL.EDITED_OPS   # Stage 6.10A / 6.10C / 5.4.1 / ops reliability, each pinned by its own F tests')]}


def test_F02_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    import ast
    for rel, edits in REPLAY_OPS.items():
        text = now(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_OPS) == FL.EDITED_OPS - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = now("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nOps runtime reliability (after Stage 5.4.1)")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_610C + NEW_541 + NEW_OPS plus this ledger.", " + NEW_610C + NEW_541 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_OPS = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the paragraph, the declarations, one line and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(FL.LEDGER) - set(old) == {"scripts/check_stock_agent.ps1", "scripts/run_stock_agent.py"}   # two new keys: the two edited ops scripts
    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_OPS & set(old) == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel
    assert json.loads(now(".github/ci/pytest_groups.json"))["groups"]["runner"]["files"].count("test_runtime_reliability_ops.py") == 1
