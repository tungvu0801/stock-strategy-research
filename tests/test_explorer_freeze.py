"""
Market Explorer, Phase A0 (the read-only boundary; Task AN): the freeze pins, as every stage since 6.8.
  F01E  the change set since BASE_EXP (main after bot-position recovery merged, PR #62) is exactly NEW_EXP + EDITED_EXP; nothing is deleted; NO
        production file is edited - every tracked file outside tests/, the CI manifest and the new explorer package is byte-identical to BASE_EXP;
        the new production files are exactly the three files of the explorer package; the CI manifest gains exactly the two new test files
  F02E  SEMANTIC_EXP and INSTRUMENTATION_EXP are empty; the server, config, the Trader View stream and its routes, and the setup, proposal,
        execution, safety and auto-run packages are byte-identical
  F04E  every changed older test definition is declared and every bookkeeping edit is undone exactly
  F05E  the ledger changes only its docstring sentence and paragraph, its declarations, its one helper and the re-hashed entries of the
        registered files this stage edits
  F06E  design 7a: the recovery's freeze test reads its side at BASE_EXP for exactly the files this stage edits; BASE_EXP is an ancestor of HEAD
        with the recovery's reviewed tree; the recovery's set is the whole change from BASE_REC to BASE_EXP; the live security pins are not edited
"""
import ast
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL
import test_recovery_freeze as TR

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
OWN_EXP = "tests/test_explorer_freeze.py"
NEW_FILES_EXP = {"explorer/__init__.py", "explorer/routes.py", "explorer/service.py", "tests/test_explorer_freeze.py", "tests/test_market_explorer_a0.py"}
REVIEWED_REC_TREE = "409baf114820e7f909b777117b2867c9ea8ef40c"                              # main after PR #62: bot-position recovery merged with its approved tree
UNTOUCHED_EXP = ("api/server.py", "api/local_gate.py", "api/validation.py", "api/routes/trader_view.py", "api/routes/watchlist.py", "api/routes/market.py",
                 "api/routes/stocks.py", "config.py", "watchlist.txt", "trader_view/stream.py", "trader_view/hub.py", "trader_view/adapters.py", "trader_view/market_real.py",
                 "trader_view/simulated.py", "trader_view/models.py", "scanner/watchlist.py", "data/market_data.py", "setups/service.py", "setups/store.py",
                 "setups/engine.py", "proposals/service.py", "proposals/store.py", "execution/service.py", "execution/engine.py", "execution/store.py",
                 "execution/broker.py", "execution/recovery.py", "safety/service.py", "safety/store.py", "safety/engine.py", "autorun/service.py", "autorun/policy.py",
                 "scripts/run_stock_agent.py", "frontend/index.html", "frontend/trader_view.js", "frontend/product_ui.js", "README.md", "tests/conftest.py")
CI_ADDED = ['        "test_explorer_freeze.py",', '        "test_market_explorer_a0.py",']


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    run = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {commit} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def at_base(rel):
    return at(FL.BASE_EXP, rel)


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def module_defs(text):
    return TR.defs(ast.parse(text).body)


def test_F01E_the_change_set_is_exactly_the_declared_files_and_no_production_file_is_edited():
    assert FL.BASE_EXP == "96d3a7665d11206111e88dd7dace588d374b42e9" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_EXP}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_EXP)
    assert changed == FL.NEW_EXP | FL.EDITED_EXP, {"unexpected": sorted(changed - FL.NEW_EXP - FL.EDITED_EXP), "missing": sorted((FL.NEW_EXP | FL.EDITED_EXP) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_EXP).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_EXP == NEW_FILES_EXP and {p for p in changed if p in in_base} == FL.EDITED_EXP
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_EXP).split()
    assert {p for p in FL.EDITED_EXP if not p.startswith("tests/")} == {".github/ci/pytest_groups.json"}                      # no production file is edited
    assert {p for p in FL.NEW_EXP if not p.startswith("tests/")} == {"explorer/__init__.py", "explorer/routes.py", "explorer/service.py"}
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_EXP, "--", ".", ":(exclude)tests", ":(exclude).github/ci/pytest_groups.json", ":(exclude)explorer"],
                          cwd=ROOT).returncode == 0                                              # the new package is pinned to exactly its three files above
    for rel in UNTOUCHED_EXP:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "core"} == {g: v for g, v in base_groups.items() if g != "core"}
    i = base_groups["core"]["files"].index("test_recovery_freeze.py") + 1
    assert groups["core"] == {**base_groups["core"], "files": base_groups["core"]["files"][:i] + ["test_explorer_freeze.py", "test_market_explorer_a0.py"] + base_groups["core"]["files"][i:]}
    base_lines, now_lines = at_base(".github/ci/pytest_groups.json").split("\n"), now_text(".github/ci/pytest_groups.json").split("\n")
    assert [l for l in now_lines if l not in base_lines] == CI_ADDED and len(now_lines) == len(base_lines) + 2
    assert FL.LEDGER[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))


def test_F02E_no_semantic_change_and_the_trading_packages_are_byte_identical():
    assert FL.SEMANTIC_EXP == {} and FL.INSTRUMENTATION_EXP == {} and FL.NEW_LEDGER_EXP == set()
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_EXP, "--", "api", "database", "safety", "paper", "account_refresh", "cash_flow", "autorun", "setups", "proposals",
                           "execution", "forward_validation", "forward_capture", "trader_view", "scanner", "data", "frontend", "scripts", "config.py", "watchlist.txt"],
                          cwd=ROOT).returncode == 0
    tracked_new = set(_git("ls-files", "--others", "--exclude-standard").split()) | {p for p in _git("diff", "--name-only", "--diff-filter=A", FL.BASE_EXP).split()}
    assert {p for p in tracked_new if not p.startswith(SCRATCH)} == FL.NEW_EXP


def test_F04E_every_changed_older_test_definition_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_EXP if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = module_defs(at_base(rel)), module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_EXP)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^EXP-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_EXP.values())
    edited_tests = {p for p in FL.EDITED_EXP if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_EXP) == edited_tests
    for rel in sorted(FL.UNDO_EXP):
        assert FL.undo_exp(rel, now_text(rel)) == at_base(rel), rel                            # now -> 96d3a766, byte for byte
        for old, new in FL.UNDO_EXP[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:80])
    FL.UNDO_EXP["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_exp("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_exp("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_EXP["tests/_probe.py"]


def test_F05E_the_ledger_changes_only_its_declarations_its_helper_and_the_rehashed_entries():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    for a in ("\nLEDGER = {\n", "\nBASE_REC = ", "\nBASE_EXP = ", "\n\ndef unchanged(", "\n\ndef undo_rec(", "\n\ndef undo_exp(",
              "\nMarket Explorer, Phase A0 (", " + NEW_J04 + NEW_GATE + NEW_REC + NEW_EXP plus this ledger."):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_EXP = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_EXP", "NEW_EXP", "EDITED_EXP", "SEMANTIC_EXP", "INSTRUMENTATION_EXP", "NEW_LEDGER_EXP", "UNDO_EXP", "SEMANTIC_TESTS_EXP"]
    assert set(ledger) == set(main_ledger)                                                      # no new registration
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == {k for k in main_ledger if k in FL.EDITED_EXP}, sorted(rehashed ^ {k for k in main_ledger if k in FL.EDITED_EXP})
    for rel in rehashed:
        assert ledger[rel] == FL.sha(now_text(rel)) == FL.LEDGER[rel], rel
    start = fl.index("\nMarket Explorer, Phase A0 (")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_EXP = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef undo_exp("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_J04 + NEW_GATE + NEW_REC + NEW_EXP plus this ledger.",
                                                                     " + NEW_J04 + NEW_GATE + NEW_REC plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, one helper and the ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06E_design_7a_the_recovery_evidence_at_a_fixed_commit_and_an_unbroken_chain():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_EXP, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_REC, FL.BASE_EXP], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_EXP}^{{tree}}").strip() == REVIEWED_REC_TREE
    rec = set(_git("diff", "--name-only", FL.BASE_REC, FL.BASE_EXP).split())
    assert rec == FL.NEW_REC | FL.EDITED_REC, sorted(rec ^ (FL.NEW_REC | FL.EDITED_REC))          # the recovery as merged (PR #62, its approved tree)
    assert TR.changed_files(FL.BASE_REC) == rec
    rf = now_text("tests/test_recovery_freeze.py")
    assert "def at_exp(rel):\n    return at(FL.BASE_EXP, rel)\n" in rf and "    if rel in EXP_MOVED:\n        return at_exp(rel)\n" in rf
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_EXP).split())\n' in rf and "def ledger_exp():" in rf
    assert set(TR.EXP_MOVED) == FL.EDITED_EXP - {"tests/test_recovery_freeze.py"} and len(TR.EXP_MOVED) == len(set(TR.EXP_MOVED))
    assert TR.ledger_exp() == ast.literal_eval(at_base("tests/freeze_ledger.py")[at_base("tests/freeze_ledger.py").index("\nLEDGER = {\n") + 1:
                                                                               at_base("tests/freeze_ledger.py").index("}\n", at_base("tests/freeze_ledger.py").index("\nLEDGER = {\n")) + 2][len("LEDGER = "):])
    for rel in ("tests/test_gate_freeze.py", "tests/test_j04_freeze.py", "tests/test_panel_freeze.py", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py",
                "tests/frontend/xss01_app_js.test.cjs", "tests/test_stop_latch_read.py", "tests/frontend/stop_priority.test.cjs", "tests/test_stop_order_gate.py",
                "tests/frontend/stop_gate_copy.test.cjs", "tests/test_bot_position_recovery.py", "tests/frontend/recovery_activity.test.cjs", "tests/test_ex06_local_gate.py"):
        assert now_text(rel) == at_base(rel), rel                                               # the live security pins and the earlier freeze tests are not edited
