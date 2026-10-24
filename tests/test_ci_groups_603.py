"""
Stage 6.0.3 - CI PARALLELIZATION: the pytest-group manifest and the workflow agree with the repository, so splitting CI into parallel
jobs can never silently drop a test file.

  M the manifest: every tests/test_*.py file in exactly one group, every listed file exists, default pytest discovery, no nested test file
  W the workflow: one pytest job per manifest group (stable names), the Stage 4.6B step, the frontend and browser-harness jobs, the
    aggregator that needs every job, no fail-fast, bounded timeouts, Windows for the Windows-bound suites, no secret / credential use
  H the helper: `list` and `run` resolve the same files the manifest declares; no subprocess, no broker or strategy import
  P the pin reconciliation: the Stage 5.5 pin test byte-pinned the workflow and the Stage 6.0 / 6.0.1 / 6.0.2 pin tests pin each other, so
    each of the four carries exactly one marked, additive Stage 6.0.3 block and is byte-identical to the Stage 6.0.3 base outside it
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github" / "ci" / "pytest_groups.json"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
HELPER = ROOT / "scripts" / "ci_pytest_groups.py"
BASE = "ccb500e5c78480c66a446e5cb4cf67f66e341b09"                  # main at the start of Stage 6.0.3 (the Stage 6.0.2 merge, PR #15)
STAGE_603_BLOCKS = {"tests/test_trader_view_55.py": 1, "tests/test_execution_60.py": 1, "tests/test_account_refresh_601.py": 1,
                    "tests/test_cash_flow_602.py": 1}
STRIP_603 = r"(?ms)^[ \t]*# >>> Stage 6\.0\.3.*?^[ \t]*# <<< Stage 6\.0\.3[^\n]*\n"
OPEN_603, CLOSE_603 = "# >>> " + "Stage 6.0.3", "# <<< " + "Stage 6.0.3"           # built, not literal: this file must not look like a carrier


def _helper():
    spec = importlib.util.spec_from_file_location("ci_pytest_groups_under_test", HELPER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


# ================================================================ M manifest ==========================================================

def test_M01_every_test_file_is_in_exactly_one_group_and_the_helper_check_passes():
    groups = json.loads(MANIFEST.read_text(encoding="utf-8"))["groups"]
    listed = [f for g in groups.values() for f in g["files"]]
    on_disk = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
    assert sorted(listed) == on_disk, {"missing_from_manifest": sorted(set(on_disk) - set(listed)), "not_on_disk": sorted(set(listed) - set(on_disk))}
    assert len(listed) == len(set(listed)), [f for f in listed if listed.count(f) > 1]
    assert Path(__file__).name in groups["core"]["files"]                                   # this test runs in CI too
    assert [p for p in (ROOT / "tests").rglob("test_*.py") if p.parent != ROOT / "tests"] == []
    assert not [n for n in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg") if (ROOT / n).exists()]   # default discovery: test_*.py
    h = _helper()
    assert h.problems(h.load()) == []
    assert h.discovered() == on_disk


def test_M02_the_manifest_detects_a_missing_a_duplicated_and_a_non_existent_file(tmp_path, monkeypatch):
    h = _helper()
    groups = h.load()
    extra = tmp_path / "tests"
    extra.mkdir()
    for f in h.discovered():
        (extra / f).write_text("", encoding="utf-8")
    (extra / "test_future_61.py").write_text("", encoding="utf-8")                           # a Stage 6.1 file that nobody added to a group
    monkeypatch.setattr(h, "TESTS", extra)
    msgs = h.problems(groups)
    assert msgs == ["tests/test_future_61.py belongs to no CI group - add it to .github/ci/pytest_groups.json"], msgs
    dup = json.loads(json.dumps(groups))
    dup["research"]["files"].append("test_execution_60.py")                                  # listed twice
    dup["core"]["files"].append("test_gone.py")                                              # does not exist
    msgs = h.problems(dup)
    assert any("listed twice" in m and "test_execution_60.py" in m for m in msgs) and any("test_gone.py does not exist" in m for m in msgs), msgs
    (extra / "sub").mkdir()
    (extra / "sub" / "test_nested.py").write_text("", encoding="utf-8")
    assert any("test files must live directly in tests/" in m for m in h.problems(groups))


# ================================================================ W workflow ==========================================================

def test_W01_one_pytest_job_per_group_with_stable_names_fail_fast_off_and_bounded_timeouts():
    wf = _workflow()
    groups = json.loads(MANIFEST.read_text(encoding="utf-8"))["groups"]
    assert wf["name"] == "Stock Agent CI" and set(wf[True if True in wf else "on"]) >= {"push", "pull_request"}     # the workflow keeps its name and triggers
    job = wf["jobs"]["pytest"]
    assert job["runs-on"] == "windows-latest" and job["strategy"]["fail-fast"] is False
    entries = {e["suite"]: e["name"] for e in job["strategy"]["matrix"]["include"]}
    assert entries == {k: v["job"] for k, v in groups.items()}                                # every group is a job; no job without a group
    assert job["name"] == "${{ matrix.name }}" and 30 <= job["timeout-minutes"] <= 60
    steps = job["steps"]
    run_step = next(s for s in steps if s.get("name") == "Run the suite")
    assert "scripts/ci_pytest_groups.py run ${{ matrix.suite }}" in run_step["run"] and "--junitxml" in run_step["run"]
    assert any("scripts/ci_pytest_groups.py check" in (s.get("run") or "") for s in steps)   # the manifest is verified before any suite runs
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0                                               # the byte pins read prior commits with `git show`
    stage46b = next(s for s in steps if s.get("name", "").startswith("Stage 4.6B tests"))
    assert stage46b["run"].strip() == "python -m pytest tests/test_alpaca_orders_46b.py -q" and stage46b["if"] == "matrix.suite == 'paper'"
    assert "test_alpaca_orders_46b.py" in groups["paper"]["files"]                              # the 4.6B step runs in the job that owns that file
    assert job["env"]["TEMP"] == "${{ runner.temp }}" and job["env"]["TMP"] == "${{ runner.temp }}"   # tmp_path on the runner's work disk


def test_W02_frontend_harness_and_aggregator_jobs():
    wf = _workflow()
    jobs = wf["jobs"]
    fe = jobs["frontend"]
    assert fe["name"] == "Frontend Tests" and fe["runs-on"] == "windows-latest" and fe["timeout-minutes"] <= 15
    assert any('node --test "tests/frontend/*.test.cjs"' in (s.get("run") or "") for s in fe["steps"])
    assert any(str(s.get("uses", "")).startswith("actions/setup-node") for s in fe["steps"])
    hz = jobs["harness"]
    assert hz["name"] == "Browser Harness" and hz["runs-on"] == "windows-latest" and hz["timeout-minutes"] <= 45
    assert any("python -m browser_tests.run --out" in (s.get("run") or "") for s in hz["steps"])
    install = "\n".join(s.get("run") or "" for s in hz["steps"])
    assert "requirements.txt" in install and "requirements-dev.txt" in install        # the harness world imports the pytest fixtures (tests/pf_fixtures.py ...)
    assert hz["env"]["TEMP"] == "${{ runner.temp }}" and hz["env"]["TMP"] == "${{ runner.temp }}"
    up = [s for s in hz["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert up and up[0]["if"] == "failure()"                                                  # artifacts only when it fails, synthetic data only
    agg = jobs["ci-success"]
    assert agg["name"] == "CI Success" and sorted(agg["needs"]) == ["frontend", "harness", "pytest"] and agg["if"] == "always()"
    body = "\n".join(s.get("run") or "" for s in agg["steps"])
    for dep in ("pytest", "frontend", "harness"):
        assert f"needs.{dep}.result" in body                                                   # every dependency must have succeeded
    assert not any(s.get("uses") or "pytest" in (s.get("run") or "") for s in agg["steps"] if "pytest" in (s.get("run") or "") and "needs" not in (s.get("run") or ""))
    assert wf["permissions"] == {"contents": "read"}


def test_W03_ci_uses_no_secret_credential_or_real_service():
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert "secrets." not in code and "ALPACA" not in code and "ROBINHOOD" not in code and "ANTHROPIC" not in code and ".env" not in code
    assert "api.alpaca.markets" not in code and "robinhood" not in code.lower()
    assert "timeout-minutes" in code


# ================================================================ H helper ============================================================

def test_H01_helper_lists_the_manifest_files_and_imports_nothing_from_the_application():
    h = _helper()
    groups = h.load()
    for name, g in groups.items():
        assert h.files_of(groups, name) == [f"tests/{f}" for f in g["files"]]
    src = HELPER.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import subprocess|from subprocess|import requests|from execution|from safety|from paper|from cash_flow|from account_refresh)", src, re.M)
    assert "pytest.main(" in src and "os.chdir(ROOT)" in src


# ================================================================ P pin reconciliation ================================================

def test_P01_prior_stage_pin_tests_differ_from_the_stage_603_base_only_by_their_marked_blocks():
    """Stage 6.0.3 touches exactly four prior-stage test files, each with one marked block: the Stage 5.5 test stops byte-pinning the workflow,
    and the Stage 6.0 / 6.0.1 / 6.0.2 pin tests strip the Stage 6.0.3 block of the file they pin. Outside the blocks every byte equals BASE,
    and no other tracked file carries a Stage 6.0.3 block."""
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    for rel, n in STAGE_603_BLOCKS.items():
        text = (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert text.count(OPEN_603) == n and text.count(CLOSE_603) == n and len(re.findall(STRIP_603, text)) == n, rel
        assert re.sub(STRIP_603, "", text).encode("utf-8") == blob(rel), f"{rel} differs from {BASE} outside its Stage 6.0.3 block"
    tracked = [t for t in subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").split("\0") if t]
    carriers = sorted(rel for rel in tracked if (ROOT / rel).is_file() and OPEN_603.encode("utf-8") in (ROOT / rel).read_bytes())
    assert carriers == sorted(STAGE_603_BLOCKS), carriers
    block55 = re.findall(STRIP_603, (ROOT / "tests/test_trader_view_55.py").read_text(encoding="utf-8").replace("\r\n", "\n"))[0]
    assert [l for l in block55.splitlines() if not l.lstrip().startswith("#")] == ['PINNED.remove(".github/workflows/tests.yml")']   # the only pin dropped
    assert ".github/workflows/tests.yml" in blob("tests/test_trader_view_55.py").decode("utf-8")                                   # ... and it was pinned
