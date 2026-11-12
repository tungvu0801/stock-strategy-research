"""
Emergency Stop order gate (EMERGENCY_STOP_ORDER_GATE_DESIGN.md revision 3.5, decisions C-1 to C-8; Task U): the freeze pins, as every stage since 6.8.
  F01G  the change set since BASE_GATE (main after J-04 merged, PR #59) is exactly NEW_GATE + EDITED_GATE; nothing is deleted; the 4.6B writer, routes,
        migrations and pane, the execution broker, the safety service, store and package, every migration, config, the API and the server, app.js,
        the risk controls script and stop_priority.js are byte-identical; the production files are exactly the declared ones; the CI manifest gains
        exactly the two new test files, each in its group
  F02G  the declared changes only: one VERSION line; the safety engine's kill branch; one GET-only open-orders line in the 4.6B reads; the
        manifest's two lines; in the 4.6B service, the store, the rules, the execution store and service, and the safety engine only the declared
        definitions change or appear
  F03G  the scripts keep their routes, fetch sites and delegation; no added line carries a forbidden token; XSS-01's raw-interpolation list
        changes only for product_ui.js and trader_render.js
  F04G  every changed older test function is declared and every bookkeeping edit is undone exactly
  F05G  the ledger changes only its docstring sentence and paragraph, its declarations, its two helpers, the re-hashed entries of the registered
        files the gate edits and the registrations NEW_LEDGER_GATE declares (each unchanged from BASE_68 to BASE_GATE)
  F06G  design 7a: the panel's and J-04's freeze tests read their side at BASE_GATE for exactly the files the gate edits; BASE_GATE is an
        ancestor of HEAD with J-04's reviewed tree; J-04's set plus the gate's set is the whole change since BASE_J04; XSS-01's pins stay live
"""
import ast
import difflib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL
import test_j04_freeze as TJ
import test_panel_freeze as TPF

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_GATE = {"tests/test_stop_order_gate.py", "tests/test_gate_freeze.py", "tests/frontend/stop_gate_copy.test.cjs"}
PROD_GATE = {"paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_store.py", "paper/alpaca_order_reads.py", "execution/store.py",
             "execution/service.py", "execution/__init__.py", "safety/engine.py", "frontend/product_ui.js", "frontend/trader_render.js"}
DOCS_GATE = {"README.md", "DESIGN_591_EXECUTION_SAFETY.md", "paper/ALPACA_ORDERS.md"}
HARNESS_GATE = {"browser_tests/flows.py", "browser_tests/risk_controls_panel_flows.py"}
TESTS_EDITED_GATE = {"tests/frontend/xss01_raw_interpolations.json"}                       # XSS-01's list of raw interpolations, regenerated (F03G)
UNTOUCHED_GATE = ("paper/alpaca_order_writer.py", "api/routes/alpaca_paper_orders.py", "database/alpaca_order_migrations.py", "frontend/alpaca_orders.js",
                  "frontend/alpaca_orders.css", "execution/broker.py", "execution/engine.py", "execution/models.py", "execution/scheduler.py", "safety/service.py",
                  "safety/store.py", "safety/__init__.py", "safety/models.py", "database/execution_migrations.py", "database/execution_safety_migrations.py",
                  "api/routes/execution_safety.py", "api/local_gate.py", "api/server.py", "config.py", "frontend/app.js", "frontend/risk_controls.js",
                  "frontend/stop_priority.js", "frontend/index.html", "frontend/forward_validation.html", "browser_tests/app_server.py", "tests/conftest.py",
                  "tests/test_stop_latch_read.py", "tests/frontend/stop_priority.test.cjs", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py",
                  "tests/frontend/xss01_app_js.test.cjs", "scripts/run_aapl_paper_smoke.py")
FORBIDDEN_PREFIX_GATE = ("database/", "api/", "autorun/", "forward_validation/", "forward_capture/", "setups/", "cash_flow/", "account_refresh/", "scripts/", "data/",
                         "agents/", "unified_portfolio/", "market_intel/", "research_data/", "swing_eval/", "swing_pullback/", "proposals/", "rotation/")
REVIEWED_J04_TREE = "8e7559aee9b4ebed93c1ddeda7b196f53635b630"                              # main after PR #59: J-04 merged with its reviewed head dec8f40b
CI_ADDED = ['        "test_gate_freeze.py",', '        "test_stop_order_gate.py",']
LINE_OPS_GATE = {                                                                           # file -> (main's removed lines, the gate's added lines), in order
    "execution/__init__.py": (
        ['VERSION = "execution_v3"                                     # Wave 2 PR C: the W2-6 MULTIPLE_ORDERS attribution (_reconcile) and the run acknowledgement (_early_stop); a new 6.10A cohort'],
        ['VERSION = "execution_v4"                                     # Emergency Stop order gate (C-7): the latch is read in the transaction that records an entry\'s attempt; a new 6.10A cohort']),
    "safety/engine.py": (
        ['    pause flag (risk controls panel design 3.1, section 4). Every HALTED label says "automatic" while manual paper orders are not gated;',
         '    `kill_active` is for the Emergency Stop order gate, which makes "New entries stopped" true for the kill and changes that one label."""'],
        ['STATE_LABEL_KILL = "New entries stopped"          # HALTED by the kill (risk controls panel design, section 4, after the order-gate PR)', '', '',
         '    pause flag (risk controls panel design 3.1, section 4). HALTED by the kill reads "New entries stopped": the Emergency Stop order gate',
         '    (EMERGENCY_STOP_ORDER_GATE_DESIGN.md 3.5) refuses manual paper BUYs while the latch is set. HALTED by the daily-loss halt keeps',
         '    "automatic", since that halt does not block manual paper orders (C-1)."""',
         '    if state == "HALTED" and kill_active:', '        return icon, STATE_LABEL_KILL']),
    ".github/ci/pytest_groups.json": ([], CI_ADDED),
}
# the definitions each production file may change (existing) or add (new); everything else in it stays as on main
DEFS_GATE = {
    "paper/alpaca_orders.py": ({"_view", "list_intents", "preview", "_fresh_gates", "confirm", "retry", "check_status"},
                               {"_pre_gate_refusal", "_arm_refusal", "_armed_before_stop"}),
    "paper/alpaca_order_rules.py": ({"=RULE_TEXT"}, {"=STOP_OFF", "=STOP_ON", "=STOP_UNKNOWN", "=RISK_REDUCING", "=RISK_INCREASING", "=AMBIGUOUS", "=STOP_READS_MAX_AGE_S",
                                                     "=OPEN_ORDERS_LIMIT", "=STOP_EXIT_ALLOWED", "=ARMED_BEFORE_STOP", "=ARMED_BEFORE_STOP_LABEL", "_num",
                                                     "classify_manual_order", "v18_stop", "v19_disclosure", "v20_bot_owned_warning"}),
    "paper/alpaca_order_reads.py": ({"_ReadTransport", "PaperOrderReader"}, {"=_OPEN_ORDERS", "=_OPEN_ORDERS_QUERY", "=_SYMBOL_ONLY"}),
    "paper/alpaca_order_store.py": ({"OrderStore"}, set()),
    "execution/store.py": ({"ExecutionStore"}, set()),
    "execution/service.py": ({"ExecutionService"}, set()),
    "browser_tests/flows.py": ({"risk_controls", "alpaca_orders", "=PUI_CONFIRM", "_pui_system", "product_ui", "product_flow_degraded", "product_flow_links"}, set()),   # product_flow_links: main's PR #61 change (merged with main 4205cdf4), not the gate's; every other flow unchanged
    "browser_tests/risk_controls_panel_flows.py": ({"risk_controls_races", "risk_controls_priority"}, set()),   # risk_controls_priority: main's PR #61 change (J7), not the gate's
    "safety/engine.py": ({"icon_label"}, {"=STATE_LABEL_KILL"}),
}
METHODS_GATE = {                                                                            # class -> (methods that change, methods that appear)
    ("paper/alpaca_order_store.py", "OrderStore"): (set(), {"_gate_read", "stop_state", "=BOT_INTENT_OPEN", "bot_owned", "safety_flags", "armed_before_stop_ids"}),
    ("execution/store.py", "ExecutionStore"): (set(), {"=STOP_REFUSALS", "_latch", "record_entry_attempt", "stop_state"}),
    ("execution/service.py", "ExecutionService"): ({"_submit"}, set()),
    ("paper/alpaca_order_reads.py", "PaperOrderReader"): (set(), {"position_strict", "open_orders"}),
    ("paper/alpaca_order_reads.py", "_ReadTransport"): ({"send"}, set()),
}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    run = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {commit} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def at_base(rel):
    return at(FL.BASE_GATE, rel)


# Bot-position recovery (design 7a, its design section 13 step 1, as the gate did for J-04): the files the recovery edits are read at BASE_REC, the
# gate's side as merged (PR #60), a commit that cannot change; the gate's change set is the fixed diff to BASE_REC and its ledger checks read the
# ledger there. tests/test_recovery_freeze.py F06R pins that this list is exactly the recovery's edited files.
REC_MOVED = ('.github/ci/pytest_groups.json', 'README.md', 'execution/__init__.py', 'execution/service.py', 'forward_capture/capture.py',
             'forward_validation/reports.py', 'forward_validation/service.py', 'frontend/product_ui.js', 'tests/freeze_ledger.py',
             'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py',
             'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py',
             'tests/test_evidence_refresh_542.py', 'tests/test_ex06_freeze.py', 'tests/test_forward_paper_610a.py',
             'tests/test_j04_freeze.py', 'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py',
             'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py',
             'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py', 'tests/test_panel_freeze.py',
             'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_sip_eval_78b1.py',
             'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_unified_exposure_gate_691.py',
             'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py',
             'tests/test_wave2_prb.py', 'tests/test_wave2_prc.py', 'tests/test_wave2_prc_drawdown.py')


def at_rec(rel):
    return at(FL.BASE_REC, rel)


def ledger_rec():
    """The ledger as the recovery found it (BASE_REC): the hashes the gate's bookkeeping is checked against."""
    t = at_rec("tests/freeze_ledger.py")
    led = t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]
    return ast.literal_eval(led[len("LEDGER = "):])


def now_text(rel):
    if rel in REC_MOVED:
        return at_rec(rel)
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    # bot-position recovery (design 7a): the gate's side is the fixed diff from `base` to BASE_REC; the working tree's change set belongs to tests/test_recovery_freeze.py
    return set(_git("diff", "--name-only", base, FL.BASE_REC).split())


def line_ops(rel):
    base, now = at_base(rel).split("\n"), now_text(rel).split("\n")
    ops = [(base[i1:i2], now[j1:j2]) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
    return base, now, [l for b, _ in ops for l in b], [l for _, n in ops for l in n]


def defs(nodes):
    out = {}
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ast.dump(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                for x in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    if isinstance(x, ast.Name):
                        out["=" + x.id] = ast.dump(n)
    return out


def module_defs(text):
    return defs(ast.parse(text).body)


def class_defs(text, name):
    return defs(next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == name).body)


def unnamed(text):
    """Every module-level statement defs() does not hold (imports, expressions, if / try blocks, attribute, subscript or tuple assignments), in
    order, the docstring included (as tests/test_wave2_prb.py's unnamed(), which leaves the docstring out)."""
    return [ast.dump(n) for n in ast.parse(text).body if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
        isinstance(n, (ast.Assign, ast.AnnAssign)) and all(isinstance(t, ast.Name) for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))))]


def test_F01G_the_change_set_is_exactly_the_declared_files_and_the_writer_routes_migrations_and_pane_are_byte_identical():
    assert FL.BASE_GATE == "0182428077926a077a7fa94d55814fc6cc201306" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_GATE}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_GATE)
    assert changed == FL.NEW_GATE | FL.EDITED_GATE, {"unexpected": sorted(changed - FL.NEW_GATE - FL.EDITED_GATE), "missing": sorted((FL.NEW_GATE | FL.EDITED_GATE) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_GATE).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_GATE == NEW_FILES_GATE and {p for p in changed if p in in_base} == FL.EDITED_GATE
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_GATE, FL.BASE_REC).split()
    production = {p for p in FL.EDITED_GATE if not p.startswith(("tests/", "browser_tests/", ".github/"))}
    assert production == PROD_GATE | DOCS_GATE and set(FL.SEMANTIC_GATE) == PROD_GATE | DOCS_GATE | HARNESS_GATE
    assert {p for p in FL.EDITED_GATE if p.startswith("tests/") and not p.endswith((".py", ".cjs"))} == TESTS_EDITED_GATE
    assert all(len(v) > 60 for v in FL.SEMANTIC_GATE.values())
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_GATE)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_GATE))
    assert {p for p in changed if p.startswith(("paper/", "execution/", "safety/"))} == {p for p in PROD_GATE | DOCS_GATE if p.startswith(("paper/", "execution/", "safety/"))}
    for rel in UNTOUCHED_GATE:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_GATE, FL.BASE_REC, "--", "paper/alpaca_order_writer.py", "api", "database", "frontend/alpaca_orders.js",
                           "execution/broker.py", "safety/service.py", "safety/store.py", "config.py"], cwd=ROOT).returncode == 0
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g not in ("core", "paper")} == {g: v for g, v in base_groups.items() if g not in ("core", "paper")}
    for g, after, new in (("core", "test_j04_freeze.py", "test_gate_freeze.py"), ("paper", "test_alpaca_orders_46b.py", "test_stop_order_gate.py")):
        i = base_groups[g]["files"].index(after) + 1
        assert groups[g] == {**base_groups[g], "files": base_groups[g]["files"][:i] + [new] + base_groups[g]["files"][i:]}, g
    assert ledger_rec()[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))   # the recovery: the manifest and its hash, both at BASE_REC


def test_F02G_the_declared_lines_and_definitions_change_and_nothing_else():
    for rel, (removed_want, added_want) in LINE_OPS_GATE.items():
        base, now, removed, added = line_ops(rel)
        assert (removed, added) == (removed_want, added_want), rel
        assert len(now) == len(base) + len(added) - len(removed), rel
        if rel in FL.SEMANTIC_GATE:
            assert FL.pre_gate(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    for rel, (may_change, may_add) in DEFS_GATE.items():
        b, n = module_defs(at_base(rel)), module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        assert {k for k in b if b[k] != n[k]} == may_change, (rel, sorted({k for k in b if b[k] != n[k]}))
        assert set(n) - set(b) == may_add, (rel, sorted(set(n) - set(b)))
        if rel in HARNESS_GATE:
            assert unnamed(now_text(rel)) == unnamed(at_base(rel)), rel                      # the harness: imports, expressions and blocks unchanged too
    for (rel, cls), (may_change, may_add) in METHODS_GATE.items():
        b, n = class_defs(at_base(rel), cls), class_defs(now_text(rel), cls)
        assert set(b) <= set(n) and {k for k in b if b[k] != n[k]} == may_change and set(n) - set(b) == may_add, (rel, cls)
    reads = now_text("paper/alpaca_order_reads.py")
    assert reads.count('"/v2/orders"') == 1 and reads.count('_OPEN_ORDERS = "/v2/orders"') == 1 and "requests.post" not in reads and ".post(" not in reads
    for rel in ("paper/alpaca_order_store.py", "paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_reads.py", "execution/store.py"):
        tree = ast.parse(now_text(rel))
        mods = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in mods if m.split(".")[0] == "safety"}, rel                  # the latch is read by raw SQL, never through the safety package
    for rel in ("paper/alpaca_order_store.py", "paper/alpaca_orders.py"):
        assert not {m.split(".")[0] for m in ({(n.module or "") for n in ast.walk(ast.parse(now_text(rel))) if isinstance(n, ast.ImportFrom)})} & {"execution"}, rel


def test_F03G_the_scripts_keep_their_routes_and_delegation_and_add_no_forbidden_token():
    routes = lambda s: sorted(set(re.findall(r"[\"`](/api/[a-z0-9_\-/]+)", s)))  # noqa: E731
    for rel in ("frontend/product_ui.js", "frontend/trader_render.js"):
        now, base = now_text(rel), at_base(rel)
        assert routes(now) == routes(base) and now.count("fetch(") == base.count("fetch(") and now.count('method: "POST"') == base.count('method: "POST"'), rel
        added = "\n".join(line_ops(rel)[3])
        assert added and not TPF.SCRIPT_BAN_PANEL.search(added), (rel, TPF.SCRIPT_BAN_PANEL.findall(added)[:5])
        assert FL.pre_gate(rel, now) == base and now != base, rel
    pu, bpu = now_text("frontend/product_ui.js"), at_base("frontend/product_ui.js")
    assert pu.count("clickLegacy(") == bpu.count("clickLegacy(") and pu[pu.index("const LEGACY = "):pu.index("function legacyAvailable")] == bpu[bpu.index("const LEGACY = "):bpu.index("function legacyAvailable")]
    tr = now_text("frontend/trader_render.js")
    assert "${esc(manual)}" in tr and "${manual}" not in tr                                  # the panel's C-1 line is escaped
    raw, base_raw = json.loads(now_text("tests/frontend/xss01_raw_interpolations.json")), json.loads(at_base("tests/frontend/xss01_raw_interpolations.json"))
    assert {m for m in set(raw) | set(base_raw) if raw.get(m) != base_raw.get(m)} == {"product_ui.js", "trader_render.js"}   # regenerated, XSS-01's scanner unchanged


def test_F04G_every_changed_older_test_function_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_GATE if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = module_defs(at_base(rel)), module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    actual |= {f"{rel}::*" for rel in FL.EDITED_GATE if rel.startswith("tests/frontend/") and rel.endswith(".cjs")}
    declared = set(FL.SEMANTIC_TESTS_GATE)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^GATE-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_GATE.values())
    edited_tests = {p for p in FL.EDITED_GATE if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_GATE) == edited_tests
    for rel in sorted(FL.UNDO_GATE):
        assert FL.undo_gate(rel, now_text(rel)) == at_base(rel), rel                           # now -> 01824280, byte for byte
        for old, new in FL.UNDO_GATE[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:80])
    for rel in FL.SEMANTIC_GATE:
        assert FL.pre_gate(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    FL.UNDO_GATE["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_gate("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_gate("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_GATE["tests/_probe.py"]


def test_F05G_the_ledger_changes_only_its_declarations_its_two_helpers_and_the_rehashed_entries():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    for a in ("\nLEDGER = {\n", "\nBASE_J04 = ", "\nBASE_GATE = ", "\n\ndef unchanged(", "\n\ndef pre_j04(", "\n\ndef pre_gate(", "\n\ndef undo_gate(",
              "\n\nEmergency Stop order gate (", " + NEW_XSS01 + NEW_PANEL + NEW_J04 + NEW_GATE plus this ledger."):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_GATE = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_GATE", "NEW_GATE", "EDITED_GATE", "SEMANTIC_GATE", "INSTRUMENTATION_GATE", "NEW_LEDGER_GATE", "UNDO_GATE", "SEMANTIC_TESTS_GATE", "VERSION_PINS_W2A['execution_v4']"]
    v3, v4 = FL.VERSION_PINS_W2A["execution_v3"], FL.VERSION_PINS_W2A["execution_v4"]
    assert set(v4) == set(v3) == set(FL.VERSION_BLOCKS_W2A) and {k for k in v3 if v3[k] != v4[k]} == {"execution/service.py::ExecutionService._submit"}   # C-7 moves _submit only
    assert FL.INSTRUMENTATION_GATE == {}
    assert set(ledger) - set(main_ledger) == FL.NEW_LEDGER_GATE and set(main_ledger) <= set(ledger)
    for rel in sorted(FL.NEW_LEDGER_GATE):
        assert FL.base_text(rel) == at_base(rel) != now_text(rel), rel                              # unchanged from BASE_68 to main and edited here
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == {k for k in main_ledger if k in FL.EDITED_GATE}, sorted(rehashed ^ {k for k in main_ledger if k in FL.EDITED_GATE})
    for rel in rehashed | FL.NEW_LEDGER_GATE:
        assert ledger[rel] == FL.sha(now_text(rel)) == ledger_rec()[rel], rel                     # the recovery: the gate's hashes against the gate's texts, both at BASE_REC
    start = fl.index("\n\nEmergency Stop order gate (") + 1
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_GATE = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_gate("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_XSS01 + NEW_PANEL + NEW_J04 + NEW_GATE plus this ledger.",
                                                                     " + NEW_XSS01 + NEW_PANEL + NEW_J04 plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, two helpers and the ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06G_design_7a_the_panel_and_j04_evidence_at_fixed_commits_and_an_unbroken_chain():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_GATE, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_J04, FL.BASE_GATE], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_GATE}^{{tree}}").strip() == REVIEWED_J04_TREE
    j04 = set(_git("diff", "--name-only", FL.BASE_J04, FL.BASE_GATE).split())
    assert j04 == FL.NEW_J04 | FL.EDITED_J04, sorted(j04 ^ (FL.NEW_J04 | FL.EDITED_J04))   # J-04 as merged (PR #59)
    gate = changed_files(FL.BASE_GATE)
    whole = changed_files(FL.BASE_J04)
    in_j04_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_J04).split())
    assert whole == (j04 | gate) - {p for p in j04 & gate if p in in_j04_base and now_text(p) == at(FL.BASE_J04, p)}, sorted(whole ^ (j04 | gate))
    pf, jf = now_text("tests/test_panel_freeze.py"), now_text("tests/test_j04_freeze.py")
    for src in (pf, jf):
        assert "def at_gate(rel):\n    return at(FL.BASE_GATE, rel)\n" in src and "    if rel in GATE_MOVED:\n        return at_gate(rel)\n" in src
    assert "    if rel in J04_MOVED:\n        return at_j04(rel)\n    if rel in GATE_MOVED:\n        return at_gate(rel)\n" in pf     # J-04's files first, at BASE_J04
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_GATE).split())\n' in jf and "def ledger_gate():" in jf
    assert set(TPF.GATE_MOVED) == FL.EDITED_GATE - {"tests/test_panel_freeze.py"} and len(TPF.GATE_MOVED) == len(set(TPF.GATE_MOVED))
    assert set(TJ.GATE_MOVED) == FL.EDITED_GATE - {"tests/test_j04_freeze.py"} and len(TJ.GATE_MOVED) == len(set(TJ.GATE_MOVED))
    assert set(TPF.J04_MOVED) == FL.EDITED_J04 - {"tests/test_panel_freeze.py"}                # J-04's own move is untouched
    for rel in ("tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs", "tests/test_stop_latch_read.py",
                "tests/frontend/stop_priority.test.cjs"):
        assert now_text(rel) == at_base(rel), rel                                               # XSS-01's and J-04's live security pins are not edited
