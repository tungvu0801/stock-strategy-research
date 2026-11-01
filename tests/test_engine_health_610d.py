"""
tests/test_engine_health_610d.py — Stage 6.10D engine health visibility (DESIGN_610D_ENGINE_HEALTH.md).

  R01-R04 the read-only engine descriptor: GET /api/ops/engine (the deployed commit read from .git without git, the process facts, the runner's
          PID file only when it names this process), its parser on every .git layout, no subprocess, GET only.
  N01     the node tests of the fold (tests/frontend/engine_health_610d.test.cjs) pass.
  F01-F03 scope: the change set since main is exactly the declared files; no setup, PPE, evidence, execution, proposal, safety, auto-run,
          decision-flow or Trader View file changes; READY thresholds, PPE scoring and the frozen hashes are unchanged; every bookkeeping edit of an
          earlier stage's test is pinned exactly and the ledger changes only the declared entries.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import freeze_ledger as FL  # noqa: E402

SHA40 = "2fe29f3a48359a365a1506c9f70093129be72602"


# ---------------------------------------------------------------- R: the descriptor
def test_R01_get_api_ops_engine_reports_this_checkout_this_process_and_no_runner_claim():
    from fastapi.testclient import TestClient
    from api.server import app
    from api.routes import ops_status as OS
    r = TestClient(app).get("/api/ops/engine")
    assert r.status_code == 200
    d = r.json()
    assert d["version"] == "engine_health_v1" and d["read_only"] is True
    assert set(d) == {"version", "read_only", "deployed", "tree_now", "tree_matches_running", "process", "runner", "checked_at"}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert d["tree_now"]["sha"] == head and d["tree_now"]["source"] in ("plain", "worktree")        # the parser agrees with git on this checkout
    assert d["deployed"]["sha"] == OS._DEPLOYED["sha"] and d["deployed"]["app_root"] == str(Path.cwd().resolve())
    assert d["process"]["pid"] == os.getpid() and d["process"]["python"] == sys.executable
    assert d["runner"] is None or d["runner"].get("instance_id")                                # a test process is never claimed by a runner PID file
    assert d["tree_matches_running"] == (bool(OS._DEPLOYED["sha"]) and OS._DEPLOYED["sha"] == head)


def _repo(tmp_path: Path, head: str, refs: dict | None = None, packed: dict | None = None, worktree: bool = False) -> Path:
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    for ref, sha in (refs or {}).items():
        p = main / ".git" / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha + "\n", encoding="utf-8")
    if packed:
        (main / ".git" / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted\n" + "".join(f"{s} {r}\n" for r, s in packed.items()), encoding="utf-8")
    if not worktree:
        (main / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
        return main
    wt = tmp_path / "wt"
    wt.mkdir()
    gitdir = main / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text(head + "\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return wt


def test_R02_git_head_reads_every_layout_without_running_git(tmp_path, monkeypatch):
    from api.routes.ops_status import git_head
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("git_head ran a subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("git_head ran a subprocess"))
    a, b = "a" * 40, "b" * 40
    assert git_head(_repo(tmp_path / "1", "ref: refs/heads/main", refs={"refs/heads/main": a})) == {"sha": a, "ref": "refs/heads/main", "source": "plain"}
    assert git_head(_repo(tmp_path / "2", "ref: refs/heads/x", packed={"refs/heads/x": b})) == {"sha": b, "ref": "refs/heads/x", "source": "plain"}
    assert git_head(_repo(tmp_path / "3", a)) == {"sha": a, "ref": None, "source": "plain"}                                    # detached
    assert git_head(_repo(tmp_path / "4", b, worktree=True)) == {"sha": b, "ref": None, "source": "worktree"}                  # detached worktree (the live tree)
    assert git_head(_repo(tmp_path / "5", "ref: refs/heads/dev", refs={"refs/heads/dev": a}, worktree=True)) == {"sha": a, "ref": "refs/heads/dev", "source": "worktree"}
    assert git_head(_repo(tmp_path / "6", "ref: refs/heads/gone")) == {"sha": None, "ref": "refs/heads/gone", "source": "ref_unresolved"}
    assert git_head(_repo(tmp_path / "7", "not a sha")) == {"sha": None, "ref": None, "source": "unreadable"}
    (tmp_path / "8").mkdir()
    assert git_head(tmp_path / "8") == {"sha": None, "ref": None, "source": "no_git"}
    (tmp_path / "9").mkdir()
    (tmp_path / "9" / ".git").write_text("garbage", encoding="utf-8")
    assert git_head(tmp_path / "9") == {"sha": None, "ref": None, "source": "unreadable"}


def test_R03_the_runner_pid_file_is_only_claimed_when_it_names_this_process(tmp_path):
    from api.routes.ops_status import engine_descriptor, runner_pid_info
    rt = tmp_path / "runtime"
    rt.mkdir()
    assert runner_pid_info(rt) is None                                                                                  # no file
    (rt / "stock_agent.pid").write_text(json.dumps({"pid": os.getpid() + 1, "instance_id": "x"}), encoding="utf-8")
    assert runner_pid_info(rt) is None                                                                                  # another process
    (rt / "stock_agent.pid").write_text("{broken", encoding="utf-8")
    assert runner_pid_info(rt) is None
    (rt / "stock_agent.pid").write_text(json.dumps({"pid": os.getpid(), "instance_id": "abc", "started_at": "2026-10-31T05:39:34+00:00", "host": "127.0.0.1", "port": 8000,
                                                    "app_root": str(tmp_path), "python": "p", "db": "d"}), encoding="utf-8")
    assert runner_pid_info(rt) == {"instance_id": "abc", "started_at": "2026-10-31T05:39:34+00:00", "host": "127.0.0.1", "port": 8000, "app_root": str(tmp_path)}
    d = engine_descriptor(tmp_path)
    assert d["runner"]["instance_id"] == "abc" and d["tree_now"] == {"sha": None, "ref": None, "source": "no_git"} and d["tree_matches_running"] is False
    assert d["deployed"]["app_root"] == str(tmp_path.resolve())


def test_R04_the_ops_router_stays_get_only_and_the_route_is_registered():
    from api.routes import ops_status as OS
    from api.server import app
    methods = {(r.path, m) for r in OS.router.routes for m in r.methods}
    assert methods == {("/api/ops/status", "GET"), ("/api/ops/engine", "GET")}
    spec = app.openapi()["paths"]
    assert set(spec["/api/ops/engine"]) == {"get"} and set(spec["/api/ops/status"]) == {"get"}
    src = (ROOT / "api/routes/ops_status.py").read_text(encoding="utf-8")
    block = src[src.index("# >>> Engine health visibility, Stage 6.10D"):src.index("# <<< Engine health visibility, Stage 6.10D")]
    assert "subprocess" not in block and "urllib" not in block and "requests" not in block and "@router.post" not in block
    assert "execution" not in block.replace("executor,", "") and "robinhood" not in block.lower().replace("broker or robinhood", "")


# ---------------------------------------------------------------- N: node
def test_N01_the_node_tests_of_the_fold_pass():
    out = subprocess.run(["node", "--test", "--test-reporter=tap", "tests/frontend/engine_health_610d.test.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, (out.stdout[-1500:], out.stderr[-1500:])
    assert "# pass 9" in out.stdout and "# fail 0" in out.stdout, out.stdout[-600:]


# ---------------------------------------------------------------- F: scope
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    out = subprocess.run(["git", "show", f"{FL.BASE_610D}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_610D} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #38 (Stage 6.10D merged): what Stage 6.10D left, whatever a later stage changed since."""
    out = subprocess.run(["git", "show", f"{FL.BASE_542}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {FL.BASE_542} (fetch-depth 0 is required)"
    return out.decode("utf-8").replace("\r\n", "\n")


# every Stage 6.10D edit of a file no ledger entry can hold (an earlier stage's own test file): (text on main, text now), replayed by F03
REPLAY_610D = {'tests/test_bot_activity_68.py': [('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | '
                                    'FL.NEW_OPS, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - '
                                    'FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS),\n'
                                    '                                                                     "missing_new": sorted((FL.NEW_68 | '
                                    'FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS) - new)}   # + '
                                    'Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
                                    '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | '
                                    'FL.NEW_OPS | FL.NEW_610D, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - '
                                    'FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D),\n'
                                    '                                                                     "missing_new": sorted((FL.NEW_68 | '
                                    'FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D) - '
                                    'new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n')],
 'tests/test_chart_empty_state_611fix.py': [('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS\n',
                                             '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | '
                                             'FL.EDITED_610D\n')],
 'tests/test_decision_flow_610c.py': [('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | '
                                       'FL.EDITED_OPS                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
                                       '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | '
                                       'FL.NEW_610D | FL.EDITED_610D                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F '
                                       'tests\n')],
 'tests/test_evidence_gate_541.py': [('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | '
                                      'FL.EDITED_OPS                                                  # the Stage 6.1.1 CSS fix, pinned by its own F '
                                      'tests\n',
                                      '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | '
                                      'FL.EDITED_610D                                                  # the Stage 6.1.1 CSS fix, pinned by its own '
                                      'F tests\n'),
                                     ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel), rel\n',
                                      '            assert post(rel) == at_base(rel), rel\n'
                                      '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel), rel      # since '
                                      'then: Stage 6.10D touches none of these\n'),
                                     ('            assert (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel), rel\n',
                                      '            assert post(rel) == at_base(rel), rel\n'
                                      '            assert (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel), rel\n')],
 'tests/test_forward_paper_610a.py': [('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | '
                                       'FL.NEW_OPS | FL.EDITED_OPS   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
                                       '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | '
                                       'FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each '
                                       'pinned by its own F tests\n')],
 'tests/test_robinhood_portfolio_69.py': [('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | '
                                           'FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS   # the 6.1.1 '
                                           'CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 '
                                           '(FL.SEMANTIC_541),\n',
                                           '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | '
                                           'FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D '
                                           '| FL.EDITED_610D   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), '
                                           'Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n')],
 'tests/test_runtime_reliability_ops.py': [('    return out.decode("utf-8").replace("\\r\\n", "\\n")\n',
                                            '    return out.decode("utf-8").replace("\\r\\n", "\\n")\n'
                                            '\n'
                                            '\n'
                                            'def post(rel):\n'
                                            '    """PR #39\'s merged result; later stages have their own exact scope checks."""\n'
                                            '    result = subprocess.run(["git", "show", f"{FL.BASE_610D}:{rel}"], cwd=ROOT, capture_output=True)\n'
                                            '    assert result.returncode == 0, rel\n'
                                            '    return result.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n'),
                                           ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", '
                                            '"browser_tests/artifacts/")) and (ROOT / p).exists()}\n',
                                            '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", '
                                            '"browser_tests/artifacts/")) and (ROOT / p).exists()}\n'
                                            '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D\n'
                                            '    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
                                           ('        text = now(rel)\n', '        text = post(rel)\n'),
                                           ('    fl, base = now("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n',
                                            '    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n'),
                                           ('    assert set(FL.LEDGER) - set(old) == {"scripts/check_stock_agent.ps1", '
                                            '"scripts/run_stock_agent.py"}   # two new keys: the two edited ops scripts\n'
                                            '    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_OPS & set(old) == '
                                            '{".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}\n',
                                            '    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])\n'
                                            '    assert set(mine) - set(old) == {"scripts/check_stock_agent.ps1", "scripts/run_stock_agent.py"}   # '
                                            'two new keys: the two edited ops scripts\n'
                                            '    assert {k for k in old if old[k] != mine[k]} == FL.EDITED_OPS & set(old) == '
                                            '{".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}\n')],
 'tests/test_unified_exposure_gate_691.py': [('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 '
                                              '| FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS   # Stage 6.10A / 6.10C / 5.4.1 / the '
                                              '6.1.1 CSS fix, each pinned by its own F tests\n',
                                              '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 '
                                              '| FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D   # '
                                              'Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n')]}


def test_F01_the_change_set_is_exactly_the_declared_files_and_touches_no_execution_strategy_or_safety_file():
    assert FL.BASE_610D == "c9e4f4522b29b5f0acb30e24b431ffda598fe2a0" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_610D}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_610D).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    stage = set(_git("diff", "--name-only", FL.BASE_610D, FL.BASE_542).split())             # Stage 6.10D itself, between two fixed commits (main before / after PR #38)
    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}
    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                      # Stage 5.4.2, pinned by its own F tests
    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),
                                                             "missing": sorted((FL.NEW_610D | FL.EDITED_610D | later) - changed)}
    assert FL.SEMANTIC_610D == {} and FL.INSTRUMENTATION_610D == {}                                      # observability only
    for d in ("setups", "ppe", "evidence", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe", "unified_portfolio", "trader_view", "scripts", "forward_validation"):
        for p in sorted((ROOT / d).glob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            assert p.read_text(encoding="utf-8").replace("\r\n", "\n") == at_base(rel), rel
    for rel in ("api/server.py", "config.py", "frontend/index.html", "frontend/product_ui.css", "frontend/trader_view.js", "browser_tests/flows.py", "README.md"):
        assert post(rel) == at_base(rel), rel                                               # what Stage 6.10D left (later edits: their own F tests)
    src = (ROOT / "api/routes/ops_status.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert src.startswith(at_base("api/routes/ops_status.py").rstrip("\n")) and src.count("# >>> Engine health visibility, Stage 6.10D") == 1    # additive: the old module text is a prefix
    assert ("# >>> " + "Stage 6.1") not in src                                                            # never a Stage 6.1 marker carrier (tests/test_forward_validation_61.py::test_Z02 scans every file)
    js = (ROOT / "frontend/product_ui.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    base_js = at_base("frontend/product_ui.js")
    end_marker = "  // <<< Engine health visibility, Stage 6.10D\n"
    stripped = js[:js.index("  // >>> Engine health visibility, Stage 6.10D")] + js[js.index(end_marker) + len(end_marker) + 1:]
    assert not re.search(r"(//|#|<!--) *>>> *Stage 6\.1", js)                                            # a block marker never starts with Stage 6.1 (the Stage 6.1 freeze tests scan for that prefix)
    stripped = stripped.replace("    renderEngine();                                                                                      // Stage 6.10D: the engine fold sits under the diagnostics line\n", "", 1)
    stripped = stripped.replace(",\n           // Stage 6.10D\n           ENGINE_KEYS, ENGINE_NOT_LOADED, engineClock, engineAge, engineModel, engineHtml, renderEngine, loadEngine, loadRobinhood };", " };", 1)
    assert stripped == base_js                                                                            # the block, one call and the export line: nothing else


def test_F02_ready_thresholds_ppe_scoring_and_the_frozen_hashes_are_unchanged():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import setups as S
    import ppe as P
    assert (S.WATCH_ENTER_STRETCH, S.READY_CONFIDENCE, S.READY_DEMAND, S.READY_DEMAND_COVERAGE, S.READY_PPE_COVERAGE, S.EVIDENCE_MAX_AGE_DAYS) == (-40.0, 40, 60, 0.60, 0.60, 30)
    assert (P.NORMAL_BAND, P.CONFIDENCE_BASE, P.CAPS["WEAK"], P.CAPS["MODERATE"]) == (40, 50, 50, 84)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_610D.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_610D) == FL.EDITED_610D - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    para = fl[fl.index("\nStage 6.10D (engine health"):fl.index('"""', fl.index("\nStage 6.10D (engine health"))]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_610C + NEW_541 + NEW_611FIX + NEW_OPS + NEW_610D plus this ledger.", " + NEW_610C + NEW_541 + NEW_611FIX + NEW_OPS plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_610D = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the declarations, the paragraph, one line and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 6.10D left
    assert set(new_led) - set(old) == {"api/routes/ops_status.py"}                         # one new key: the extended ops router
    assert {k for k in old if old[k] != new_led[k]} == FL.EDITED_610D & set(old)           # every re-hash is a declared Stage 6.10D edit
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel
