"""
J-04 Emergency Stop reliability (TASK_P_STOP_RELIABILITY_ORDER_GATE_PLAN_2026-11-10.md, design 2.1; Task Q): the freeze pins, as every stage since 6.8.
  F01J  the change set since BASE_J04 (main after the harness-fix merge, PR #58) is exactly NEW_J04 + EDITED_J04; nothing is deleted; app.js, the local
        gate, the server, the safety package, Stage 4.6B, every migration, config and the panel's own tests are byte-identical; the only api/ change is
        the safety route file; the CI manifest gains exactly the two new test files, after the panel's
  F02J  the safety route file changes exactly the declared lines (one docstring line, one GET route that reads the latch row and nothing else); the
        two pages each gain exactly one script line (stop_priority.js directly after app.js on index.html, before forward_validation.js on the other
        page); the README changes only its F2 lines; the safety design lists the route; the manifest adds its two lines
  F03J  the scripts: risk_controls.js POSTs the same four routes, reads the latch, one POST site, one fetch site; product_ui.js keeps its GET routes, its
        delegation and one use of each F2 command; stop_priority.js names exactly the two SAFETY paths and the two KEEP prefixes, opens nothing but the
        BroadcastChannel, never claims the EX-06 flag; no added line carries a forbidden token; XSS-01's raw-interpolation list changes only for the
        three scripts the panel wrote
  F04J  every changed older test function is declared and every bookkeeping edit is undone exactly
  F05J  the ledger changes only its docstring sentence and paragraph, its declarations, its two helpers, the re-hashed entries of the registered files
        J-04 edits and the registrations NEW_LEDGER_J04 declares (each unchanged from BASE_68 to BASE_J04)
  F06J  design 7a: the panel's freeze test reads the panel's side at BASE_J04 for exactly the files J-04 edits; the panel's set plus J-04's set is the
        whole change since BASE_PANEL; BASE_J04 is an ancestor of HEAD with the reviewed tree; XSS-01's and the panel's security pins stay live
"""
import ast
import difflib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL
import test_panel_freeze as TPF

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_J04 = {"frontend/stop_priority.js", "tests/test_stop_latch_read.py", "tests/test_j04_freeze.py", "tests/frontend/stop_priority.test.cjs"}
CI_FILES_J04 = ("test_stop_latch_read.py", "test_j04_freeze.py")
PROD_J04 = {"api/routes/execution_safety.py", "frontend/index.html", "frontend/forward_validation.html", "frontend/risk_controls.js", "frontend/trader_render.js",
            "frontend/product_ui.js"}
DOCS_J04 = {"README.md", "DESIGN_591_EXECUTION_SAFETY.md"}
HARNESS_J04 = {"browser_tests/flows.py", "browser_tests/risk_controls_panel_flows.py"}
TESTS_EDITED_J04 = {"tests/frontend/xss01_raw_interpolations.json"}                        # XSS-01's list of raw interpolations, regenerated (F03J)
UNTOUCHED_J04 = ("frontend/app.js", "api/local_gate.py", "api/server.py", "api/routes/alpaca_paper_orders.py", "config.py", "safety/service.py", "safety/store.py",
                 "safety/engine.py", "safety/__init__.py", "safety/models.py", "frontend/forward_validation.js", "frontend/paper_bot.js", "frontend/setup_alerts.js",
                 "frontend/backtest_lab.js", "frontend/trader_view.js", "frontend/product_ui.css", "browser_tests/cdp.py", "browser_tests/app_server.py", "browser_tests/run.py",
                 "tests/conftest.py", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs", "tests/test_risk_controls_panel.py",
                 "tests/frontend/product_ui.test.cjs", "tests/frontend/trader_safety.test.cjs")
FORBIDDEN_PREFIX_J04 = ("paper/", "database/", "autorun/", "forward_validation/", "forward_capture/", "setups/", "cash_flow/", "account_refresh/", "scripts/", "data/",
                        "agents/", "unified_portfolio/", "market_intel/", "research_data/", "swing_eval/", "swing_pullback/", "safety/", "execution/", "proposals/")
REVIEWED_PANEL_TREE = "2e877a9d3d5a4e9a0c86adeafd3bc3bc76e3a154"
HARNESS_FIX_FILES = {"browser_tests/flows.py", "tests/freeze_ledger.py"}                    # PR #58 (the browser harness fix) sits between the panel and BASE_J04
# the lines the stage adds to its scripts: no new I/O channel, storage, order, broker or model token, no dynamic code or markup sink beyond innerHTML
SCRIPT_BAN_J04 = TPF.SCRIPT_BAN_PANEL
ROUTE_ADDED = [
    'GET  /api/execution-safety/latch                J-04: the persisted Emergency Stop latch alone (no evaluation, no account read, no write)',
    '@router.get("/latch")',
    'def read_latch() -> JSONResponse:',
    '    """J-04 (TASK_P plan, design 2.1 Part B): the persisted Emergency Stop latch, read strictly from its row and nothing else, so a page can confirm',
    '    a saved stop in milliseconds while the full evaluation (GET /api/execution-safety) waits for the cash-flow cycle. Never evaluates, never',
    '    reads the account, writes nothing and records no event. The mapping is pinned by tests/test_stop_latch_read.py:',
    '      row with kill_active 1 -> 200 {"status": "OK", "kill_active": true, "kill_activated_at": ...}',
    '      row with kill_active 0 -> 200 {"status": "OK", "kill_active": false}',
    '      no database, table or row -> 200 {"status": "NO_ROW", "kill_active": null}      (only kill_active true is information to a page)',
    '      anything unreadable      -> 503 {"status": "UNREADABLE", "kill_active": null}   (never a guess, never a 500 outside the local gate)',
    '    """',
    '    st = _service().store',
    '    try:',
    '        rec = st.state()                                                           # strict: raises SafetyStoreError when the row cannot be read',
    '        present = st.has_state_row()',
    '    except Exception as exc:  # noqa: BLE001 - unreadable is reported, never guessed',
    '        return JSONResponse({"version": VERSION, "status": "UNREADABLE", "kill_active": None, "error": type(exc).__name__}, status_code=503)',
    '    if not present:',
    '        if rec.kill_active:                                                        # a saved stop whose row check found nothing: the check failed, never "no row"',
    '            return JSONResponse({"version": VERSION, "status": "UNREADABLE", "kill_active": None, "error": "RowCheckFailed"}, status_code=503)',
    '        return JSONResponse({"version": VERSION, "status": "NO_ROW", "kill_active": None, "kill_activated_at": None})',
    '    return JSONResponse({"version": VERSION, "status": "OK", "kill_active": bool(rec.kill_active), "kill_activated_at": rec.kill_activated_at if rec.kill_active else None})',
    '',
    '',
]
LINE_OPS_J04 = {                                                                            # file -> (main's removed lines, J-04's added lines), in order
    "api/routes/execution_safety.py": ([], ROUTE_ADDED),
    "frontend/index.html": ([], ['<script src="stop_priority.js"></script>']),
    "frontend/forward_validation.html": ([], ['<script src="stop_priority.js"></script>']),
    "README.md": (
        ['(curl.exe -sS --max-time 60 http://127.0.0.1:8000/api/execution-safety -H "X-Stock-Agent-Intent: dashboard" | ConvertFrom-Json).kill_switch.active',
         'second: the stop may still have been saved. A connection error means the server is not answering: go to F3.'],
        ['(curl.exe -sS --max-time 15 http://127.0.0.1:8000/api/execution-safety/latch -H "X-Stock-Agent-Intent: dashboard" | ConvertFrom-Json).kill_active',
         'second: the stop may still have been saved. The second command reads only the saved stop state, so it answers at once even while a cycle',
         'runs: `True` means the stop is saved; `False` means it is not, so run the first command again; a blank means no answer, no state row yet',
         'or an unreadable state (a running deployment always has the row), so go to F3. A connection error means the server is not answering: go to F3.']),
    "DESIGN_591_EXECUTION_SAFETY.md": (
        [],
        ['`GET /api/execution-safety/latch` (J-04: the persisted Emergency Stop latch alone — `kill_active` true / false, `NO_ROW` when no state row exists,',
         '503 `UNREADABLE` when the row cannot be read; it never evaluates, never reads the account and writes nothing),']),
    ".github/ci/pytest_groups.json": ([], ['        "test_stop_latch_read.py",', '        "test_j04_freeze.py",']),
}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    run = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {commit} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def at_base(rel):
    return at(FL.BASE_J04, rel)


# The Emergency Stop order gate (design 7a, its design section 12, as J-04 did for the panel): the files the gate edits are read at BASE_GATE,
# J-04's side as merged (PR #59), a commit that cannot change; J-04's change set is the fixed diff to BASE_GATE and its ledger checks read the
# ledger there. tests/test_gate_freeze.py F06G pins that this list is exactly the gate's edited files.
GATE_MOVED = ('.github/ci/pytest_groups.json', 'DESIGN_591_EXECUTION_SAFETY.md', 'README.md', 'browser_tests/flows.py',
              'browser_tests/risk_controls_panel_flows.py', 'execution/__init__.py', 'execution/service.py', 'execution/store.py',
              'frontend/product_ui.js', 'frontend/trader_render.js', 'paper/ALPACA_ORDERS.md', 'paper/alpaca_order_reads.py',
              'paper/alpaca_order_rules.py', 'paper/alpaca_order_store.py', 'paper/alpaca_orders.py', 'safety/engine.py',
              'tests/alpaca_order_fakes.py', 'tests/freeze_ledger.py', 'tests/frontend/product_ui.test.cjs',
              'tests/frontend/risk_controls_order.test.cjs', 'tests/frontend/risk_controls_view.test.cjs',
              'tests/frontend/trader_proposal.test.cjs', 'tests/frontend/trader_safety.test.cjs',
              'tests/frontend/xss01_raw_interpolations.json', 'tests/test_alpaca_orders_46b.py', 'tests/test_bot_activity_68.py',
              'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py', 'tests/test_demand_57.py',
              'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_54.py',
              'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_ex06_freeze.py',
              'tests/test_execution_safety_591.py', 'tests/test_forward_paper_610a.py', 'tests/test_market_intel_70.py',
              'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py',
              'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py',
              'tests/test_panel_freeze.py', 'tests/test_proposals_59.py', 'tests/test_research_flow_47.py',
              'tests/test_risk_controls_panel.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_rotation_handoff_47.py',
              'tests/test_runtime_reliability_ops.py', 'tests/test_setups_58.py', 'tests/test_sip_eval_78b1.py',
              'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_trader_view_55.py',
              'tests/test_trader_view_561.py', 'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py',
              'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_prb.py', 'tests/test_wave2_prc.py')


def at_gate(rel):
    return at(FL.BASE_GATE, rel)


def ledger_gate():
    """The ledger as the order gate found it (BASE_GATE): the hashes J-04's bookkeeping is checked against."""
    t = at_gate("tests/freeze_ledger.py")
    led = t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]
    return ast.literal_eval(led[len("LEDGER = "):])


def now_text(rel):
    if rel in GATE_MOVED:
        return at_gate(rel)
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    # the order gate (design 7a): J-04's side is the fixed diff from `base` to BASE_GATE; the working tree's change set belongs to tests/test_gate_freeze.py
    return set(_git("diff", "--name-only", base, FL.BASE_GATE).split())


def line_ops(rel):
    base, now = at_base(rel).split("\n"), now_text(rel).split("\n")
    ops = [(base[i1:i2], now[j1:j2]) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
    return base, now, [l for b, _ in ops for l in b], [l for _, n in ops for l in n]


def defs(text):
    out = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ast.dump(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out["=" + t.id] = ast.dump(n)
    return out


def test_F01J_the_change_set_is_exactly_the_declared_files_and_no_gate_server_safety_migration_or_4_6b_file_changed():
    assert FL.BASE_J04 == "daec71f5d14ff84a60178b31f1fbb1e408e7c944" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_J04}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_J04)
    assert changed == FL.NEW_J04 | FL.EDITED_J04, {"unexpected": sorted(changed - FL.NEW_J04 - FL.EDITED_J04), "missing": sorted((FL.NEW_J04 | FL.EDITED_J04) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_J04).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_J04 == NEW_FILES_J04 and {p for p in changed if p in in_base} == FL.EDITED_J04
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_J04, FL.BASE_GATE).split()
    production = {p for p in FL.EDITED_J04 if not p.startswith(("tests/", "browser_tests/", ".github/"))}
    assert production == PROD_J04 | DOCS_J04 and set(FL.SEMANTIC_J04) == PROD_J04 | DOCS_J04 | HARNESS_J04
    assert {p for p in FL.EDITED_J04 if p.startswith("tests/") and not p.endswith((".py", ".cjs"))} == TESTS_EDITED_J04
    assert all(len(v) > 60 for v in FL.SEMANTIC_J04.values())
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_J04)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_J04))
    assert {p for p in changed if p.startswith("api/")} == {"api/routes/execution_safety.py"}
    for rel in UNTOUCHED_J04:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_J04, FL.BASE_GATE, "--", "api/routes/alpaca_paper_orders.py", "paper", "database", "safety", "api/local_gate.py", "api/server.py"], cwd=ROOT).returncode == 0
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "core"} == {g: v for g, v in base_groups.items() if g != "core"}
    i = base_groups["core"]["files"].index("test_panel_freeze.py") + 1
    assert groups["core"] == {**base_groups["core"], "files": base_groups["core"]["files"][:i] + list(CI_FILES_J04) + base_groups["core"]["files"][i:]}
    assert ledger_gate()[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))   # the order gate: the manifest and its hash, both at BASE_GATE


def test_F02J_the_route_file_the_pages_the_readme_the_design_and_the_manifest_change_exactly_the_declared_lines():
    assert set(LINE_OPS_J04) == {"api/routes/execution_safety.py", "frontend/index.html", "frontend/forward_validation.html", "README.md", "DESIGN_591_EXECUTION_SAFETY.md", ".github/ci/pytest_groups.json"}
    for rel, (removed_want, added_want) in LINE_OPS_J04.items():
        base, now, removed, added = line_ops(rel)
        assert (removed, added) == (removed_want, added_want), rel
        assert len(now) == len(base) + len(added) - len(removed), rel
        if rel in FL.SEMANTIC_J04:
            assert FL.pre_j04(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    route = now_text("api/routes/execution_safety.py")
    body = route[route.index('@router.get("/latch")'):route.index("def _act(")]
    assert body.count("st.state()") == 1 and body.count("st.has_state_row()") == 1 and "status_code=503" in body
    assert not re.search(r"evaluate\(|overview\(|current\(|\.account\(|emergency_stop|clear_emergency|\.latch\(|commit\(|record_|_act\(", re.sub(r'"""[\s\S]*?"""', "", body))
    assert route.index('@router.get("/controls")') < route.index('@router.get("/latch")') < route.index("def _act(")   # a read among the reads, before the actions
    page = now_text("frontend/index.html").split("\n")
    i = page.index('<script src="app.js"></script>')
    assert page[i + 1] == '<script src="stop_priority.js"></script>' and page.count('<script src="stop_priority.js"></script>') == 1   # directly after the EX-06 wrapper (D-7 B)
    fv = now_text("frontend/forward_validation.html")
    assert re.findall(r'<script[^>]*src="([^"]+)"', fv) == ["stop_priority.js", "forward_validation.js"]
    assert now_text("README.md").count("/api/execution-safety/latch") == 1


def test_F03J_the_scripts_keep_their_routes_and_delegation_and_the_new_module_opens_nothing_but_the_channel():
    rc = now_text("frontend/risk_controls.js")
    assert sorted(set(re.findall(r'"(/api/execution-safety/[a-z-]+)"', rc))) == ["/api/execution-safety/clear-emergency-stop", "/api/execution-safety/emergency-stop",
                                                                                  "/api/execution-safety/latch", "/api/execution-safety/pause-new-entries", "/api/execution-safety/resume-new-entries"]
    assert rc.count('method: "POST"') == 1 and rc.count("fetch(") == 1 and "/api/account-refresh" in rc
    for line in ('  const LATCH_PATH = "/api/execution-safety/latch";', "  const LATCH_TIMEOUT_MS = 5000;", "  const LATCH_FIRST_MS = 1000;", "  const LATCH_GAP_MS = 2000;",
                 "  const RECONCILE_MS = 60000;", "  const PREEMPT_MS = 30000;"):
        assert rc.count(line) == 1, line
    pu, bpu = now_text("frontend/product_ui.js"), at_base("frontend/product_ui.js")
    routes = lambda s: sorted(set(re.findall(r"[\"`](/api/[a-z0-9_\-/]+)", s)))  # noqa: E731
    assert routes(pu) == routes(bpu)                                                    # no new route: the shell reads what it read
    assert pu.count("clickLegacy(") == bpu.count("clickLegacy(") == 6 and pu.count("fetch(") == bpu.count("fetch(")
    assert 'method: "POST"' not in pu and pu[pu.index("const LEGACY = "):pu.index("function legacyAvailable")] == bpu[bpu.index("const LEGACY = "):bpu.index("function legacyAvailable")]
    assert pu.count("f2Read(port)") == 1 and pu.count("f2Post(port)") == 1 and "/api/execution-safety/latch" in pu and "kill_switch.active" not in pu[pu.index("const f2Read"):pu.index("const f2Read") + 300]
    sp = now_text("frontend/stop_priority.js")
    for line in ('  const SAFETY = ["/api/execution-safety", "/api/execution-safety/latch"];', '  const KEEP_PREFIXES = ["/api/stocks/", "/api/daily-brief"];',
                 '  const CHANNEL = "stock-agent-stop";', "  const WINDOW_MAX_MS = 30000;", "  const WINDOW_MIN_MS = 1000;", '  const VERSION = "stop_priority_v1";'):
        assert sp.count(line) == 1, line
    code = re.sub(r"//[^\n]*", "", sp)
    assert code.count("fetch(") == 0 and "__stockAgentIntent =" not in code and code.count("new root.BroadcastChannel(") == 1
    assert not re.search(r"document\.|innerHTML|localStorage|sessionStorage|indexedDB|XMLHttpRequest|WebSocket\(|EventSource\(|sendBeacon|window\.open|\beval\(|new Function|\.(href|src)\s*=(?!=)", code)
    assert not SCRIPT_BAN_J04.search(code), SCRIPT_BAN_J04.findall(code)[:5]
    for rel in ("frontend/product_ui.js", "frontend/risk_controls.js", "frontend/trader_render.js"):
        added = "\n".join(line_ops(rel)[3])
        assert added and not SCRIPT_BAN_J04.search(added), (rel, SCRIPT_BAN_J04.findall(added)[:5])
        assert FL.pre_j04(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    raw, base_raw = json.loads(now_text("tests/frontend/xss01_raw_interpolations.json")), json.loads(at_base("tests/frontend/xss01_raw_interpolations.json"))
    assert {m for m in set(raw) | set(base_raw) if raw.get(m) != base_raw.get(m)} == {"product_ui.js", "risk_controls.js", "trader_render.js"}   # regenerated, XSS-01's scanner unchanged


def test_F04J_every_changed_older_test_function_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_J04 if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = defs(at_base(rel)), defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    actual |= {f"{rel}::*" for rel in FL.EDITED_J04 if rel.startswith("tests/frontend/") and rel.endswith(".cjs")}
    declared = set(FL.SEMANTIC_TESTS_J04)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^J04-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_J04.values())
    edited_tests = {p for p in FL.EDITED_J04 if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_J04) == edited_tests
    for rel in sorted(FL.UNDO_J04):
        assert FL.undo_j04(rel, now_text(rel)) == at_base(rel), rel                             # now -> daec71f5, byte for byte
        for old, new in FL.UNDO_J04[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:80])
    for rel in FL.SEMANTIC_J04:
        assert FL.pre_j04(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    FL.UNDO_J04["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_j04("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_j04("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_J04["tests/_probe.py"]


def test_F05J_the_ledger_changes_only_its_declarations_its_two_helpers_and_the_rehashed_entries():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    for a in ("\nLEDGER = {\n", "\nBASE_PANEL = ", "\nBASE_J04 = ", "\n\ndef unchanged(", "\n\ndef pre_panel(", "\n\ndef pre_j04(", "\n\ndef undo_j04(", "\n\nJ-04 Emergency Stop reliability (",
              " + NEW_XSS01 + NEW_PANEL + NEW_J04 plus this ledger."):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_J04 = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_J04", "NEW_J04", "EDITED_J04", "SEMANTIC_J04", "INSTRUMENTATION_J04", "NEW_LEDGER_J04", "UNDO_J04", "SEMANTIC_TESTS_J04"]
    assert FL.INSTRUMENTATION_J04 == {}
    assert set(ledger) - set(main_ledger) == FL.NEW_LEDGER_J04 == {"api/routes/execution_safety.py", "frontend/forward_validation.html"} and set(main_ledger) <= set(ledger)
    for rel in sorted(FL.NEW_LEDGER_J04):
        assert FL.base_text(rel) == at_base(rel) != now_text(rel), rel                              # unchanged from BASE_68 to main and edited here, as the panel's registrations were
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == {k for k in main_ledger if k in FL.EDITED_J04}, sorted(rehashed ^ {k for k in main_ledger if k in FL.EDITED_J04})
    for rel in rehashed | FL.NEW_LEDGER_J04:
        assert ledger[rel] == FL.sha(now_text(rel)) == ledger_gate()[rel], rel                     # the order gate: J-04's hashes against J-04's texts, both at BASE_GATE
    start = fl.index("\n\nJ-04 Emergency Stop reliability (") + 1
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_J04 = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_j04("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_XSS01 + NEW_PANEL + NEW_J04 plus this ledger.", " + NEW_XSS01 + NEW_PANEL plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, two helpers and the ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06J_design_7a_the_panels_evidence_at_fixed_commits_and_an_unbroken_chain():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_J04, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_PANEL, FL.BASE_J04], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_J04}^{{tree}}").strip() == REVIEWED_PANEL_TREE
    panel = set(_git("diff", "--name-only", FL.BASE_PANEL, FL.BASE_J04).split())
    assert panel == FL.NEW_PANEL | FL.EDITED_PANEL | HARNESS_FIX_FILES, sorted(panel ^ (FL.NEW_PANEL | FL.EDITED_PANEL | HARNESS_FIX_FILES))   # the panel as merged, plus the harness fix (PR #58)
    j04 = changed_files(FL.BASE_J04)
    whole = changed_files(FL.BASE_PANEL)
    in_panel_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_PANEL).split())
    assert whole == (panel | j04) - {p for p in panel & j04 if p in in_panel_base and now_text(p) == at(FL.BASE_PANEL, p)}, sorted(whole ^ (panel | j04))
    pf = now_text("tests/test_panel_freeze.py")
    assert "def at_j04(rel):\n    return at(FL.BASE_J04, rel)\n" in pf and "    if rel in J04_MOVED:\n        return at_j04(rel)\n" in pf
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_J04).split())\n' in pf and "def ledger_j04():" in pf
    assert set(TPF.J04_MOVED) == FL.EDITED_J04 - {"tests/test_panel_freeze.py"}             # exactly the files J-04 edits read at the fixed commit; the rest stays live
    assert len(TPF.J04_MOVED) == len(set(TPF.J04_MOVED))
    for live in ("def test_F02P_",):                                                          # the panel's pins on the server files J-04 must not change stay on the working tree
        body = pf[pf.index(live):pf.index("\ndef test_", pf.index(live) + 10)]
        assert "at_j04(" not in body, live
    assert all(rel not in TPF.J04_MOVED for rel in TPF.SERVER_PANEL)
    for rel in ("tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs"):
        assert now_text(rel) == at_base(rel), rel
    assert "FL.NEW_LEDGER_PANEL | FL.NEW_LEDGER_J04" in now_text("tests/test_ex06_freeze.py")   # EX-06's registration union gains J-04's two registrations (its one edit, undone by UNDO_J04)
