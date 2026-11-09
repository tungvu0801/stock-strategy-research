"""
Wave 2 PR C - its scope pins and freeze bookkeeping (WAVE2_PRC_DESIGN_REV4.md Revision 4.3 section 14; WAVE2_PRC_TEST_PLAN_REV4.md section 3.11).
The behaviour is tested by tests/test_wave2_prc_run.py, test_wave2_prc_evidence.py, test_wave2_prc_drawdown.py, test_wave2_prc_pnl.py and
tests/frontend/wave2_prc.test.cjs; this file pins what may change, exactly.

  T-W04C      the production diff since 875bc06d is exactly design 1.3, and PR C's added lines carry no order, mode or broker token
  T-W05C      the closed function list of design 14 (Python by AST; the three frontend files by factory function), no new module-level statement
  T-W01C      T-WC04: execution.VERSION execution_v3, whose pin table moves exactly the two changed VERSION blocks from execution_v2
  T-W06C      T-WC02: D-5 stays in the current cumulative() (each declared line exactly once) and forward_capture/health.py is byte-identical
  F01C..F03C  T-WC03: PR C's change set, its declared test changes (SEMANTIC_TESTS_W2C) and every bookkeeping edit of an older test file
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import freeze_ledger as FL  # noqa: E402
from test_wave2_pra import block_hashes  # noqa: E402
from test_wave2_prb import D5_CUMULATIVE, N01_TOKENS, defs, digest, fn_text, named_nodes, unnamed  # noqa: E402

sys.path.insert(0, str(ROOT))

FN = "ForwardValidationService."
CLOSED_LIST_C = {                                                                                # design 14: file -> (may change, may add)
    "database/forward_validation_migrations.py": ({"=_STATEMENTS", "run_forward_validation_migrations"}, {"=RUN_STATEMENTS", "=RUN_SAMPLE_COLUMNS", "_add_run_columns"}),
    "forward_validation/store.py": ({"=SAMPLE_COLS", "ForwardValidationStore.add_sample"},
                                    {"ForwardValidationStore." + n for n in ("active_run", "runs", "run_events", "insert_run", "end_run", "add_run_event", "strict_view")}),
    "forward_validation/service.py": ({FN + n for n in ("__init__", "_snapshot", "observe", "_early_stop", "cumulative", "overview", "order_reliability", "_duplicate_evidence",
                                                        "_resolution", "trace", "unrealized_pnl", "attention")},       # __init__: self._run_* counters only (NARROW_INIT); attention: L-b
                                      {FN + n for n in ("_run_ack", "_run_check", "_session_membership", "_trade_membership", "_strategy_day", "_pa_cov", "_run_readiness",
                                                        "_run_block", "_bot_drawdown", "_fill_times")} | {"=ROW_CAP"}),
    "forward_validation/metrics.py": ({"edge_status", "=CLASSIFIER_VERSION"}, {"price_age_unknown", "bot_drawdown_pct"}),
    "forward_validation/reports.py": ({"export_rows", "weekly_report", "daily_report", "_pnl_text"}, set()),
    "forward_validation/run_admin.py": (set(), {"main", "start", "end", "status", "_preconditions", "_prior_days", "_capture_live", "_drift_fields"}),
    "forward_validation/pnl_view.py": (set(), {"decorate"}),
    "forward_capture/view.py": ({"evidence"}, set()),
    "strategy_registry/service.py": ({"derive_lifecycle", "runtime_facts"}, set()),
    "strategy_registry/downside_recovery.py": ({"_paper_results"}, set()),
    "api/routes/execution.py": ({"get_overview", "get_status", "get_history", "get_positions", "get_symbol"}, set()),
    "api/routes/forward_validation.py": ({"get_overview", "get_export"}, {"get_run"}),
    "execution/service.py": ({"ExecutionService._reconcile"}, set()),
    "execution/__init__.py": ({"=VERSION"}, set()),
}
JS_CLOSED_LIST_C = {                                                                             # design 14: the frontend files, by factory-level function
    "frontend/product_ui.js": ({"todaySummary", "dailyReview", "activityHtml", "positionLifecycle", "closedHtml", "timeline", "positionModel"}, set()),
    "frontend/paper_bot.js": ({"historyLine", "cardModel", "cardHtml"}, set()),
    "frontend/forward_validation.js": ({"summaryModel", "headline", "tradeRow", "dailyRow", "compactHtml", "pageHtml"}, {"runHeader", "runBanner", "cohortRow"}),   # [Q43-1] C-81, C-86
}
ROUTES_C = ("get_overview", "get_status", "get_history", "get_positions", "get_symbol")      # [Q43-2] the decorated execution read routes (design 12)
MAIN_GUARD = 'if __name__ == "__main__":\n    raise SystemExit(main())\n'                      # [Q43-1] design 4.2: python -m forward_validation.run_admin
NEW_MODULES_C = {"forward_validation/run_admin.py", "forward_validation/pnl_view.py"}
CI_FILES_C = ("test_wave2_prc.py", "test_wave2_prc_run.py", "test_wave2_prc_evidence.py", "test_wave2_prc_drawdown.py", "test_wave2_prc_pnl.py")   # the CI manifest's PR C entries
PR_C_FILES = set(CLOSED_LIST_C) | set(JS_CLOSED_LIST_C)
FORBIDDEN_C = {"forward_capture/health.py", "forward_capture/capture.py", "forward_capture/cohort.py", "config.py", "execution/broker.py", "execution/engine.py",
               "execution/exposure_gate.py", "execution/models.py", "execution/store.py", "browser_tests/flows.py"}
FORBIDDEN_PREFIX_C = ("safety/", "autorun/", "cash_flow/", "setups/", "proposals/", "unified_portfolio/", "market_intel/", "research_data/", "swing_eval/")
TOKENS_C = ("post_order", "insert_intent", "enable_auto", "set_mode(", "apply_fill", "record_order(", "TradingClient", "robinhood", "requests", "disable_auto(") + N01_TOKENS
ORDER_RE_C = re.compile(r"run_cycle\(|\bstartup\(|submit\w*\(|_scan_entries\(|_recheck_entry\(|_evaluate_positions\(|_apply_(?:order|fill)\(|cancel\w*\(|liquidat|"
                        r"flatten\w*\(|\bbroker\.|\.step\(|scheduler\.\w+\(")                     # [Q43-2, Q43-L] the order path, PR A F02's terms, the scheduler
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
HELPERS_C = '''
def pre_w2c(rel: str, text: str) -> str:
    if rel not in SEMANTIC_W2C:
        return text
    out = subprocess.run(["git", "show", f"{BASE_W2C}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_W2C} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_w2c(rel: str, text: str) -> str:
    text = _norm(text)
    for old, new in reversed(UNDO_W2C.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text
'''                                                                                               # [Q43-7] the two helpers' code, exactly (docstrings aside)


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_W2C}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W2C} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def prc_files(base):
    """EX-06: the change set between `base` and main after PR #53 (fixed commits). EX-06's delta: tests/test_ex06_freeze.py."""
    return set(_git("diff", "--name-only", base, FL.BASE_EX06).split())


def func(text, qual):
    """The AST node of a top-level function or a method ('Class.name')."""
    cls, name = qual.split(".") if "." in qual else (None, qual)
    for n in ast.parse(text).body:
        for m in ([n] if cls is None else (n.body if isinstance(n, ast.ClassDef) and n.name == cls else [])):
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == name:
                return m
    raise AssertionError(qual)


def _run_counter(stmt):
    """NARROW_INIT: `self._run_<name> = <literal>` (a constant or an empty / literal container), nothing else."""
    if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Attribute)):
        return False
    t = stmt.targets[0]
    if not (isinstance(t.value, ast.Name) and t.value.id == "self" and t.attr.startswith("_run_")):
        return False
    try:
        ast.literal_eval(stmt.value)
    except ValueError:
        return False
    return True


class _Unwrap(ast.NodeTransformer):
    """decorate(X) -> X."""
    def visit_Call(self, node):
        self.generic_visit(node)
        return node.args[0] if isinstance(node.func, ast.Name) and node.func.id == "decorate" else node


def js_blocks(text):
    """(name -> source of every factory-level function: '  function NAME(' up to its '  }' line, or the one line), the other lines with a
    '<fn NAME>' placeholder for each function). The reconstruction is checked, so a function this cannot delimit fails the test."""
    lines, blocks, rest, i = text.split("\n"), {}, [], 0
    while i < len(lines):
        m = re.match(r"^  (?:async )?function (\w+)\(", lines[i])
        if not m:
            rest.append(lines[i])
            i += 1
            continue
        j = i if lines[i].rstrip().endswith("}") and lines[i].count("{") == lines[i].count("}") else lines.index("  }", i + 1)
        assert m.group(1) not in blocks, m.group(1)
        blocks[m.group(1)] = "\n".join(lines[i:j + 1])
        rest.append(f"<fn {m.group(1)}>")
        i = j + 1
    assert "\n".join(blocks[r[4:-1]] if r.startswith("<fn ") else r for r in rest) == text
    return blocks, rest


# ================================================================ T-W04C, T-W05C: the allow-list and the closed function list ================

def test_TW04C_the_production_diff_is_exactly_design_1_3_and_pr_c_adds_no_order_mode_or_broker_token():
    changed = prc_files(FL.BASE_W2C)                                                            # EX-06: PR C's own change set (fixed commits)
    prod = {p for p in changed if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json"}
    assert prod == PR_C_FILES, {"unexpected": sorted(prod - PR_C_FILES), "missing": sorted(PR_C_FILES - prod)}
    for rel in sorted(prod):
        base = set() if rel in NEW_MODULES_C else set(at_base(rel).splitlines())
        added = "\n".join(l for l in now_text(rel).splitlines() if l not in base)
        assert added and not [t for t in TOKENS_C if t in added], (rel, [t for t in TOKENS_C if t in added])
        assert not ORDER_RE_C.search(added), (rel, ORDER_RE_C.findall(added))
    assert not FORBIDDEN_C & changed and not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_C)}
    assert {p for p in prod if p.startswith("execution/")} == {"execution/service.py", "execution/__init__.py"}


def test_TW05C_only_the_listed_functions_changed_and_only_the_listed_names_are_new():
    for rel, (may_change, may_add) in CLOSED_LIST_C.items():
        now = named_nodes(now_text(rel))
        base = {} if rel in NEW_MODULES_C else named_nodes(at_base(rel))
        assert set(base) <= set(now), (rel, sorted(set(base) - set(now)))                          # nothing of 875bc06d disappears
        assert set(now) - set(base) == may_add, (rel, sorted((set(now) - set(base)) ^ may_add))     # exactly the listed names are new
        changed = {k for k in base if digest(base[k]) != digest(now[k])}
        assert changed <= may_change, (rel, sorted(changed - may_change))                           # every other function hash-equal
        expected = ([ast.dump(ast.parse(MAIN_GUARD).body[0])] if rel == "forward_validation/run_admin.py" else []) if rel in NEW_MODULES_C else unnamed(at_base(rel))
        assert sorted(unnamed(now_text(rel))) == sorted(expected), rel                          # no new module-level statement (run_admin: the one main guard)
    init_now, init_base = func(now_text("forward_validation/service.py"), FN + "__init__"), func(at_base("forward_validation/service.py"), FN + "__init__")
    added_init = [s for s in init_now.body if _run_counter(s)]
    init_now.body = [s for s in init_now.body if not _run_counter(s)]
    assert ast.dump(init_now) == ast.dump(init_base) and len(added_init) >= 1                   # NARROW_INIT: only self._run_* literal initializations are added
    for name in ROUTES_C:                                                                          # [Q43-2] the route body is 875bc06d's, its result wrapped once in decorate()
        now_fn, base_fn = func(now_text("api/routes/execution.py"), name), func(at_base("api/routes/execution.py"), name)
        imports = [s for s in now_fn.body if isinstance(s, ast.ImportFrom) and s.module == "forward_validation.pnl_view"]
        assert len(imports) == 1 and [a.name for a in imports[0].names] == ["decorate"] and not imports[0].names[0].asname, name
        now_fn.body = [s for s in now_fn.body if s is not imports[0]]
        calls = [c for c in ast.walk(now_fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "decorate"]
        assert len(calls) == 1 and len(calls[0].args) == 1 and not calls[0].keywords, name
        assert ast.dump(_Unwrap().visit(now_fn)) == ast.dump(base_fn), name
    svc = named_nodes(now_text("execution/service.py"))
    assert {k for k in named_nodes(at_base("execution/service.py")) if digest(named_nodes(at_base("execution/service.py"))[k]) != digest(svc[k])} <= {"ExecutionService._reconcile"}
    for rel, (may_change, may_add) in JS_CLOSED_LIST_C.items():
        (bn, rn), (bb, rb) = js_blocks(now_text(rel)), js_blocks(at_base(rel))
        assert set(bb) <= set(bn) and set(bn) - set(bb) == may_add, (rel, sorted((set(bn) - set(bb)) ^ may_add))
        assert {k for k in bb if bb[k] != bn[k]} <= may_change, (rel, sorted({k for k in bb if bb[k] != bn[k]} - may_change))
        exports = [l for l in rb if l.startswith("  return { ")]
        assert len(exports) == 1 and (not may_add or exports[0].endswith(" };")), rel                 # a file that adds names exports them on its one-line return
        expected = [l if l != exports[0] or not may_add else l[:-3] + "".join(", " + n for n in sorted(may_add)) + " };" for l in rb]
        assert [l for l in rn if l not in {f"<fn {n}>" for n in may_add}] == expected, rel          # only the export line gains the new names


def test_TW01C_execution_v3_moves_exactly_the_two_changed_version_blocks():
    import execution as X
    assert X.VERSION == "execution_v3" and at_base("execution/__init__.py").count('VERSION = "execution_v2"') == 1
    v2, v3 = FL.VERSION_PINS_W2A["execution_v2"], FL.VERSION_PINS_W2A["execution_v3"]
    assert set(v3) == set(v2) == set(FL.VERSION_BLOCKS_W2A)
    assert block_hashes(at_base) == v2 and block_hashes(now_text) == v3                          # 875bc06d is execution_v2, the tree is execution_v3
    assert {k for k in v2 if v2[k] != v3[k]} == {"execution/service.py::ExecutionService._reconcile", "forward_validation/service.py::ForwardValidationService._early_stop"}


def test_TW06C_d5_stays_in_the_current_cumulative_and_health_is_untouched():
    cur = fn_text(now_text("forward_validation/service.py"), FN + "cumulative")
    for _, new in D5_CUMULATIVE:
        assert cur.count(new) == 1, new[:80]
    assert now_text("forward_capture/health.py") == at_base("forward_capture/health.py")


# ================================================================ F: scope and bookkeeping (as every stage since 6.8) =========================

def test_F01C_the_change_set_is_exactly_the_declared_files():
    assert FL.BASE_W2C == "875bc06dd08b36ace1d3930877f90cb96ff81b08" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W2C}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = prc_files(FL.BASE_W2C)                                                            # EX-06: PR C's own change set (fixed commits)
    assert changed == FL.NEW_W2C | FL.EDITED_W2C, {"unexpected": sorted(changed - FL.NEW_W2C - FL.EDITED_W2C), "missing": sorted((FL.NEW_W2C | FL.EDITED_W2C) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_W2C).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_W2C and {p for p in changed if p in in_base} == FL.EDITED_W2C
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_W2C).split()
    app = {p for p in FL.EDITED_W2C | FL.NEW_W2C if not p.startswith("tests/")}
    assert app == PR_C_FILES | {".github/ci/pytest_groups.json"} and set(FL.SEMANTIC_W2C) == PR_C_FILES - NEW_MODULES_C and FL.INSTRUMENTATION_W2C == {}
    assert all(p.endswith((".py", ".cjs")) for p in FL.EDITED_W2C if p.startswith("tests/"))    # [Q43-4] no merged test data (e.g. a golden) is edited: only code, pinned by F02C / F03C
    assert all(len(v) > 40 for v in FL.SEMANTIC_W2C.values())
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")
    for rel in sorted(changed):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel
    prc_groups = subprocess.run(["git", "show", f"{FL.BASE_EX06}:.github/ci/pytest_groups.json"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")   # EX-06: the manifest as PR #53 left it (EX-06's entries: F01X)
    groups, base_groups = json.loads(prc_groups)["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "execution"} == {g: v for g, v in base_groups.items() if g != "execution"}
    assert groups["execution"] == {**base_groups["execution"], "files": base_groups["execution"]["files"] + list(CI_FILES_C)}


def test_F02C_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_W2C if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = defs(at_base(rel)), defs(FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel))))                        # EX-06: its own in-place edits undone first (F02X)
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_W2C)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^(P|Q4|Q41|C)-\d\d", v) and len(v) > 40 for v in FL.SEMANTIC_TESTS_W2C.values())
    out = subprocess.run([sys.executable, "-c", "import forward_validation as FV; print(FV.frozen_hash())"], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"], out.stderr[-600:]
    assert now_text("forward_validation/service.py").count("disable_auto(") == at_base("forward_validation/service.py").count("disable_auto(") == 1
    for rel in FL.SEMANTIC_W2C:
        assert FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))) == at_base(rel) and now_text(rel) != at_base(rel), rel
    assert FL.pre_w2c("config.py", "x") == "x" and now_text("forward_validation/__init__.py") == at_base("forward_validation/__init__.py")


def test_F03C_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    assert set(FL.UNDO_W2C) == {p for p in FL.EDITED_W2C if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    for rel in sorted(FL.UNDO_W2C):
        assert FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel)))) == at_base(rel), rel                             # now -> 875bc06d, byte for byte
    main_fl = at_base("tests/freeze_ledger.py")
    fl = subprocess.run(["git", "show", f"{FL.BASE_EX06}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")   # EX-06: the ledger as PR #53 left it
    for a in ("LEDGER = {", "BASE_W2A = ", "BASE_W2B = ", "BASE_W2C = ", "\n\ndef unchanged(", "\n\ndef pre_w2b(", "\n\ndef pre_w2c(", "\nWave 2 PR B (the Stage 6.1"):
        assert fl.count(a) == 1, a                                                            # the anchors every older ledger test uses stay unique
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_W2C = "):fl.index("\nLEDGER = {\n") + 1]).body        # [Q43-7] the stripped regions hold exactly the declared names and helpers
    assert all(isinstance(n, (ast.Assign, ast.AnnAssign)) for n in region)
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_W2C", "NEW_W2C", "EDITED_W2C", "SEMANTIC_W2C", "INSTRUMENTATION_W2C", "NEW_LEDGER_W2C", "UNDO_W2C", "SEMANTIC_TESTS_W2C", "VERSION_PINS_W2A['execution_v3']"]
    code = lambda src: [ast.dump(ast.FunctionDef(name=f.name, args=f.args, body=[s for s in f.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))],  # noqa: E731
                                                 decorator_list=f.decorator_list, returns=f.returns, type_params=[])) for f in ast.parse(src).body]
    assert code(fl[fl.index("\n\ndef pre_w2c("):fl.index("\n\ndef unchanged(")]) == code(HELPERS_C)
    start = fl.index("\nWave 2 PR C (")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1).replace(" + NEW_W2B + NEW_W2C plus this ledger.", " + NEW_W2B plus this ledger.", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_W2C = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_w2c("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1)
    assert rebuilt == main_fl                                                                 # the paragraph, one line, the declarations, two helpers and the ledger
    prc_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # EX-06: PR C's ledger; EX-06's re-hashes are its F03X
    assert set(prc_ledger) - set(main_ledger) == FL.NEW_LEDGER_W2C and set(main_ledger) <= set(prc_ledger)
    assert {k for k in main_ledger if main_ledger[k] != prc_ledger[k]} == FL.EDITED_W2C & set(main_ledger)
    for rel in sorted(FL.NEW_LEDGER_W2C):
        assert FL.base_text(rel) == at_base(rel) == FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))))), rel   # unchanged from BASE_68 to main: the view IS main's text
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel
    FL.UNDO_W2C["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_w2c("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_w2c("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_W2C["tests/_probe.py"]
