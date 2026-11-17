"""
MD-A (the historical data contract package historical_data/): the freeze pins, as every stage since 6.8.
  F01A  the change set since BASE_MDA (main after bot-position recovery merged, PR #62) is exactly NEW_MDA + EDITED_MDA; nothing is deleted; the one
        production edit is requirements.txt; no trading, broker, safety, setup, proposal, data-layer, scanner, research-data or validation file, no
        config, server, route, page or harness changes; the CI manifest gains exactly the three new test files
  F02A  requirements.txt gains exactly the one tzdata line; it was unchanged from BASE_68 to BASE_MDA (the precondition of NEW_LEDGER_MDA), and
        the ledger view gives the older freeze tests main's text
  F04A  every changed older test definition is declared and every bookkeeping edit is undone exactly
  F05A  the ledger changes only its docstring sentence and paragraph, its declarations, its three helpers, the re-hashed entries of the
        registered files MD-A edits and the one new registration (requirements.txt)
  F06A  design 7a: the panel's, J-04's, the gate's and the recovery's freeze tests read MD-A's files at BASE_MDA; BASE_MDA is an ancestor of HEAD
        with the recovery's reviewed tree; the recovery's set is the whole change from BASE_REC to BASE_MDA; the live security pins are not edited
"""
import ast
import difflib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL
import test_gate_freeze as TG
import test_j04_freeze as TJ
import test_panel_freeze as TPF
import test_recovery_freeze as TR

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_MDA = {f"historical_data/{m}" for m in ("ACCEPTANCE_CRITERIA.md", "__init__.py", "acceptance.py", "calendar.py", "canonical.py", "expected_events.py",
                                                  "provider.py", "schema.py", "status.py", "validate.py", "adapters/__init__.py", "adapters/ms1_evidence.py",
                                                  "adapters/sip_daily_v1.py", "adapters/synthetic.py", "contract/CONTRACT_SHA256.txt",
                                                  "contract/HISTORICAL_DATA_CONTRACT_v1.md", "contract/HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md")} | {
    "tests/historical_data_fixtures.py", "tests/test_historical_data_mda.py", "tests/test_historical_data_ms1_local_mda.py", "tests/test_mda_freeze.py"}
PROD_MDA = {"requirements.txt"}
TZ_LINE = "tzdata==2026.4   # MD-A historical_data calendar: zoneinfo's time-zone database, pinned (contract section 2; installed with pandas)"
REVIEWED_REC_TREE = "409baf114820e7f909b777117b2867c9ea8ef40c"                             # main after PR #62: bot-position recovery merged with its approved tree
UNTOUCHED_MDA = ("config.py", "api/server.py", "api/local_gate.py", "frontend/index.html", "frontend/app.js", "frontend/product_ui.js", "README.md", "watchlist.txt",
                 "requirements-dev.txt", ".github/workflows/tests.yml", "research_data/__init__.py", "research_data/nyse_calendar.py", "research_data/sip_bundle.py",
                 "research_data/quality.py", "market_intel/calendar.py", "market_intel/calendar_history.py", "market_intel/archive.py", "autorun/calendar.py",
                 "strategy_validation/dataset.py", "execution/service.py", "execution/engine.py", "tests/conftest.py", "tests/sip_eval_fixtures.py",
                 "tests/test_ex06_local_gate.py", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs",
                 "tests/test_stop_order_gate.py", "tests/test_stop_latch_read.py", "tests/frontend/stop_priority.test.cjs", "tests/frontend/stop_gate_copy.test.cjs",
                 "tests/test_bot_position_recovery.py", "tests/frontend/recovery_activity.test.cjs")
FORBIDDEN_PREFIX_MDA = ("execution/", "safety/", "paper/", "setups/", "proposals/", "trader_view/", "autorun/", "database/", "account_refresh/", "forward_validation/",
                        "forward_capture/", "market_intel/", "market_scan/", "scan_report/", "research_data/", "swing_eval/", "swing_pullback/", "strategy_validation/",
                        "scanner/", "dynamic_universe/", "data/", "scripts/", "agents/", "browser_tests/", "rh_gateway/", "unified_portfolio/", "strategy_registry/",
                        "explorer/", "cash_flow/", "api/", "frontend/", "value_quality/")


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    return TR.at(commit, rel)


def at_base(rel):
    return at(FL.BASE_MDA, rel)


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def line_ops(rel):
    base, now = at_base(rel).split("\n"), now_text(rel).split("\n")
    ops = [(base[i1:i2], now[j1:j2]) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
    return base, now, [l for b, _ in ops for l in b], [l for _, n in ops for l in n]


def test_F01A_the_change_set_is_exactly_the_declared_files_and_no_trading_data_layer_or_security_file_changed():
    assert FL.BASE_MDA == "96d3a7665d11206111e88dd7dace588d374b42e9" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_MDA}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_MDA)
    assert changed == FL.NEW_MDA | FL.EDITED_MDA, {"unexpected": sorted(changed - FL.NEW_MDA - FL.EDITED_MDA), "missing": sorted((FL.NEW_MDA | FL.EDITED_MDA) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_MDA).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_MDA == NEW_FILES_MDA and {p for p in changed if p in in_base} == FL.EDITED_MDA
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_MDA).split()
    production = {p for p in FL.EDITED_MDA if not p.startswith(("tests/", ".github/"))}
    assert production == PROD_MDA == set(FL.SEMANTIC_MDA) and all(len(v) > 60 for v in FL.SEMANTIC_MDA.values())
    assert {p for p in FL.NEW_MDA if not p.startswith("tests/")} == {p for p in NEW_FILES_MDA if p.startswith("historical_data/")}
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_MDA)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_MDA))
    for rel in UNTOUCHED_MDA:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_MDA, "--", "execution", "safety", "paper", "setups", "proposals", "trader_view", "autorun", "database",
                           "market_intel", "market_scan", "research_data", "strategy_validation", "swing_eval", "swing_pullback", "config.py", "api", "frontend",
                           "data"], cwd=ROOT).returncode == 0
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g not in ("core", "research")} == {g: v for g, v in base_groups.items() if g not in ("core", "research")}
    i = base_groups["core"]["files"].index("test_recovery_freeze.py") + 1
    assert groups["core"] == {**base_groups["core"], "files": base_groups["core"]["files"][:i] + ["test_mda_freeze.py"] + base_groups["core"]["files"][i:]}
    assert groups["research"] == {**base_groups["research"], "files": base_groups["research"]["files"] + ["test_historical_data_mda.py", "test_historical_data_ms1_local_mda.py"]}
    assert FL.LEDGER[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))


def test_F02A_requirements_gains_only_the_pinned_tzdata_line_and_was_unchanged_from_base_68_to_base_mda():
    base68, main = FL.base_text("requirements.txt"), at_base("requirements.txt")
    assert base68 == main, "requirements.txt changed between BASE_68 and BASE_MDA: NEW_LEDGER_MDA's precondition fails - STOP and report (do not work around it)"
    base, now, removed, added = line_ops("requirements.txt")
    assert (removed, added) == ([], [TZ_LINE]) and now[now.index(TZ_LINE) - 1] == "pandas>=3.0.6"
    assert FL.pre_mda("requirements.txt", now_text("requirements.txt")) == main and FL.pre_mda("config.py", "x") == "x"
    assert FL.view("requirements.txt", now_text("requirements.txt")) == base68                     # the older freeze tests read main's text
    assert FL.NEW_LEDGER_MDA == {"requirements.txt"} and FL.LEDGER["requirements.txt"] == FL.sha(now_text("requirements.txt"))


def test_F04A_every_changed_older_test_definition_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_MDA if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = TR.module_defs(at_base(rel)), TR.module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_MDA)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^MDA-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_MDA.values())
    edited_tests = {p for p in FL.EDITED_MDA if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_MDA) == edited_tests, sorted(set(FL.UNDO_MDA) ^ edited_tests)
    for rel in sorted(FL.UNDO_MDA):
        for old, new in FL.UNDO_MDA[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:120])
        assert FL.undo_mda(rel, now_text(rel)) == at_base(rel), rel                          # now -> 96d3a766, byte for byte
    FL.UNDO_MDA["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_mda("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_mda("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_MDA["tests/_probe.py"]


def test_F05A_the_ledger_changes_only_its_declarations_its_helpers_the_rehashed_entries_and_one_registration():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    want = {k for k in main_ledger if k in FL.EDITED_MDA} | FL.NEW_LEDGER_MDA
    bad = sorted((rel, FL.sha(now_text(rel))) for rel in want if ledger.get(rel) != FL.sha(now_text(rel)))
    assert not bad, "re-hash these LEDGER entries:\n" + "\n".join(f'    "{r}": "{h}",' for r, h in bad)
    for a in ("\nLEDGER = {\n", "\nBASE_REC = ", "\nBASE_MDA = ", "\n\ndef unchanged(", "\n\ndef undo_rec(", "\n\n\ndef pre_mda(", "\n\ndef undo_mda(", "\n\ndef ledger_at(",
              "\nMD-A (the historical data contract package", " + NEW_GATE + NEW_REC + NEW_MDA plus this ledger."):
        assert fl.count(a) == 1, a
    region = ast.parse(fl[fl.index("BASE_MDA = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_MDA", "NEW_MDA", "EDITED_MDA", "SEMANTIC_MDA", "INSTRUMENTATION_MDA", "NEW_LEDGER_MDA", "_U_MDA", "_G_MDA", "UNDO_MDA", "_F_MDA", "_I_MDA", "SEMANTIC_TESTS_MDA"]
    assert FL.INSTRUMENTATION_MDA == {} and set(ledger) == set(main_ledger) | FL.NEW_LEDGER_MDA and not set(main_ledger) & FL.NEW_LEDGER_MDA
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == {k for k in main_ledger if k in FL.EDITED_MDA} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}, sorted(rehashed)
    for rel in rehashed | FL.NEW_LEDGER_MDA:
        assert ledger[rel] == FL.sha(now_text(rel)) == FL.LEDGER[rel], rel
    start = fl.index("\nMD-A (the historical data contract package")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_MDA = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\n\ndef pre_mda(") + 1:rebuilt.index("\n\ndef unchanged(") + 1], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_GATE + NEW_REC + NEW_MDA plus this ledger.", " + NEW_GATE + NEW_REC plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, three helpers and the ledger
    assert FL.ledger_at(FL.BASE_MDA) == main_ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06A_design_7a_the_earlier_freeze_tests_read_mdas_files_at_a_fixed_commit_and_the_chain_is_unbroken():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_MDA, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_REC, FL.BASE_MDA], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_MDA}^{{tree}}").strip() == REVIEWED_REC_TREE
    rec = set(_git("diff", "--name-only", FL.BASE_REC, FL.BASE_MDA).split())
    assert rec == FL.NEW_REC | FL.EDITED_REC, sorted(rec ^ (FL.NEW_REC | FL.EDITED_REC))          # the recovery as merged (PR #62, its approved tree)
    assert TR.changed_files(FL.BASE_REC) == rec
    for mod, own in ((TPF, "tests/test_panel_freeze.py"), (TJ, "tests/test_j04_freeze.py"), (TG, "tests/test_gate_freeze.py"), (TR, "tests/test_recovery_freeze.py")):
        assert set(mod.MDA_MOVED) == FL.EDITED_MDA - {own} and len(mod.MDA_MOVED) == len(set(mod.MDA_MOVED)), own
        src = now_text(own)
        assert "def at_mda(rel):\n    return at(FL.BASE_MDA, rel)\n" in src and "    if rel in MDA_MOVED:\n        return at_mda(rel)\n" in src, own
        assert mod.now_text("requirements.txt") == at_base("requirements.txt") != now_text("requirements.txt"), own   # a file only MD-A edits (earlier stages' moves come first)
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_MDA).split())\n' in now_text("tests/test_recovery_freeze.py") and TR.ledger_mda() == FL.ledger_at(FL.BASE_MDA)
    whole = changed_files(FL.BASE_REC)
    in_rec_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_REC).split())
    mda = changed_files(FL.BASE_MDA)
    assert whole == (rec | mda) - {p for p in rec & mda if p in in_rec_base and now_text(p) == at(FL.BASE_REC, p)}, sorted(whole ^ (rec | mda))
    for rel in ("tests/test_ex06_local_gate.py", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs", "tests/test_stop_latch_read.py",
                "tests/frontend/stop_priority.test.cjs", "tests/test_stop_order_gate.py", "tests/frontend/stop_gate_copy.test.cjs", "tests/test_bot_position_recovery.py",
                "tests/frontend/recovery_activity.test.cjs"):
        assert now_text(rel) == at_base(rel), rel                                               # the live security pins are not edited
