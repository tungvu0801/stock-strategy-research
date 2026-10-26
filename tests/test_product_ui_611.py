"""
tests/test_product_ui_611.py — Stage 6.1.1 PRODUCT UI SIMPLIFICATION (display only; nothing about strategy, risk, execution or Paper Auto changed).

Focused tests:
  W wiring: frontend/index.html carries exactly two marked, additive Product UI blocks; the shell reads GET routes only and delegates its two actions
    (STOP TRADING, Paper Auto) to the existing Risk Controls / Paper Bot buttons; the legacy markup is hidden by the shell, never removed; no stale wording,
    no fake primary action
  N the node tests of the shell's pure models (state mapping, status bar, risk / position summaries, timeline, MORE groups)
  H the browser harness carries the product_ui flow (before the clock-moving flows) and opens the detail column before every Trader View detail flow
  C the CI manifest carries this file; the frontend job runs every tests/frontend/*.test.cjs
  F freeze: every tracked file of the Stage 6.1 base is byte-identical except the eight wiring / reconciliation files; the strategy, execution, risk and
    broker constants (forward_validation.frozen_controls: hash + explicit values) and the Forward Validation calculations are unchanged; the four
    reconciled prior-stage tests differ from the base only by one marked block (561, 60, 602) or the listed exact in-place edits (61); no other carrier
  D the design document and the README section
"""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from freeze_ledger import unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)
BASE = "ec87dc6f6e8b7fec64e7aedefa77e5af88da5e79"                                          # origin/main with Stage 6.1 merged (PR #17)
NEW_611 = {"frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/frontend/product_ui.test.cjs", "DESIGN_611_PRODUCT_UI.md"}
NEW_612 = {"strategy_registry/__init__.py", "strategy_registry/models.py", "strategy_registry/standard.py", "strategy_registry/registry.py", "strategy_registry/service.py",
           "strategy_registry/downside_recovery.py", "api/routes/strategy_registry.py", "tests/test_product_flow_612.py", "tests/frontend/product_flow.test.cjs",
           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}   # Stage 6.1.2 files (tests/test_product_flow_612.py pins them)
from test_db_bootstrap_busy import EDITS_61_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix: its new test file and its in-place edits of the Stage 6.1 test
from test_strategy_validation_62 import EDITS_62_61, NEW_62  # noqa: E402 - Stage 6.2: its new files and its in-place edits of the Stage 6.1 test
ALLOWED_CHANGED = {"frontend/index.html", "tests/test_trader_view_561.py", "tests/test_execution_60.py", "tests/test_cash_flow_602.py", "tests/test_forward_validation_61.py",
                   "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json", "api/server.py", "database/database.py"}   # Stage 6.1.2: two additive router lines in api/server.py (checked below); the WAL bootstrap fix
BLOCK_TESTS = ("tests/test_trader_view_561.py", "tests/test_execution_60.py", "tests/test_cash_flow_602.py")
OPEN_611, CLOSE_611 = "# >>> " + "Stage 6.1.1", "# <<< " + "Stage 6.1.1"                   # built, not literal: this file is not a marked-block carrier
HTML_OPEN = "<!-- >>> " + "Product UI 6.1.1"
STRIP_611 = r"(?ms)^[ \t]*# >>> Stage 6\.1\.1\b.*?^[ \t]*# <<< Stage 6\.1\.1[^\n]*\n"
STRIP_611_HTML = r"(?ms)^<!-- >>> Product UI 6\.1\.1\b.*?^<!-- <<< Product UI 6\.1\.1 -->\n"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
TAGLINE = "Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled."
STALE = ("never places a trade", "This tool never places", "no order path", "Not available in this version", "read-only Trader View", "never places, prepares", "STOP TRADING", "Reset Trade")
GET_ROUTES = {"/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events",
              "/api/proposals/history", "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/",
              "/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls",   # Stage 6.1.2: the strategy registry and the frozen controls (GET only)
              "/api/execution/activity", "/api/account-refresh/positions", "/api/unified-portfolio"}   # Stage 6.8: Bot Activity and the account positions (GET only)
# the exact in-place edits of tests/test_forward_validation_61.py (each `old` occurs once in the base; applied in order they produce the current file)
EDITS_61 = [
    ('OPEN_61, CLOSE_61 = "# >>> " + "Stage 6.1", "# <<< " + "Stage 6.1"                   # built, not literal: this file must not read as a marked-block carrier\n',
     'OPEN_61, CLOSE_61 = "# >>> " + "Stage 6.1", "# <<< " + "Stage 6.1"                   # built, not literal: this file must not read as a marked-block carrier\n'
     'STRIP_611 = r"(?ms)^[ \\t]*# >>> Stage 6\\.1\\.1\\b.*?^[ \\t]*# <<< Stage 6\\.1\\.1[^\\n]*\\n"                                                 # Stage 6.1.1: its marked blocks are stripped first\n'
     'STRIP_611_HTML = r"(?ms)^<!-- >>> Product UI 6\\.1\\.1\\b.*?^<!-- <<< Product UI 6\\.1\\.1 -->\\n"                                            # Stage 6.1.1: the product shell blocks of index.html\n'
     'NEW_611 = {"frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/frontend/product_ui.test.cjs", "DESIGN_611_PRODUCT_UI.md"}   # Stage 6.1.1 files\n'),
    ('    assert re.sub(STRIP_61_HTML, "", html) == blob("frontend/index.html")                                     # the page differs from the base by the two marked blocks only\n',
     '    assert re.sub(STRIP_61_HTML, "", re.sub(STRIP_611_HTML, "", html)) == blob("frontend/index.html")      # the page differs from the base by the two marked blocks only (Stage 6.1.1 blocks stripped first)\n'),
    ('    assert new == NEW_FILES, {"unexpected_new": sorted(new - NEW_FILES), "missing_new": sorted(NEW_FILES - new)}\n',
     '    assert new == NEW_FILES | NEW_611, {"unexpected_new": sorted(new - NEW_FILES - NEW_611), "missing_new": sorted((NEW_FILES | NEW_611) - new)}   # Stage 6.1.1 files added\n'),
    ('    allowed = ADDITIVE | set(RECONCILED_TESTS) | {"tests/test_account_refresh_601.py"} | CI_FILES\n',
     '    allowed = ADDITIVE | set(RECONCILED_TESTS) | {"tests/test_account_refresh_601.py", "tests/test_forward_validation_61.py"} | CI_FILES   # Stage 6.1.1 reconciles this file in place\n'),
    ('    assert re.sub(STRIP_61_HTML, "", now_text("frontend/index.html")) == blob("frontend/index.html")\n',
     '    assert re.sub(STRIP_61_HTML, "", re.sub(STRIP_611_HTML, "", now_text("frontend/index.html"))) == blob("frontend/index.html")   # Stage 6.1.1 blocks stripped first\n'),
    ('        text = now_text(rel)\n        assert text.count(OPEN_61) == n == text.count(CLOSE_61) and len(re.findall(STRIP_61, text)) == n, rel\n',
     '        text = re.sub(STRIP_611, "", now_text(rel))                                                                                                      # Stage 6.1.1 blocks stripped first\n'
     '        assert text.count(OPEN_61) == n == text.count(CLOSE_61) and len(re.findall(STRIP_61, text)) == n, rel\n'),
]
# Stage 6.1.2 reconciles tests/test_forward_validation_61.py in place too (two exact edits, applied after EDITS_61; tests/test_product_flow_612.py pins them as well)
EDITS_612_61 = [
    ('NEW_611 = {"frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/frontend/product_ui.test.cjs", "DESIGN_611_PRODUCT_UI.md"}   # Stage 6.1.1 files\n',
     'NEW_611 = {"frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/frontend/product_ui.test.cjs", "DESIGN_611_PRODUCT_UI.md"}   # Stage 6.1.1 files\n'
     'NEW_612 = {"strategy_registry/__init__.py", "strategy_registry/models.py", "strategy_registry/standard.py", "strategy_registry/registry.py", "strategy_registry/service.py",\n'
     '           "strategy_registry/downside_recovery.py", "api/routes/strategy_registry.py", "tests/test_product_flow_612.py", "tests/frontend/product_flow.test.cjs",\n'
     '           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}   # Stage 6.1.2 files\n'),
    ('    assert new == NEW_FILES | NEW_611, {"unexpected_new": sorted(new - NEW_FILES - NEW_611), "missing_new": sorted((NEW_FILES | NEW_611) - new)}   # Stage 6.1.1 files added\n',
     '    assert new == NEW_FILES | NEW_611 | NEW_612, {"unexpected_new": sorted(new - NEW_FILES - NEW_611 - NEW_612), "missing_new": sorted((NEW_FILES | NEW_611 | NEW_612) - new)}   # Stage 6.1.1 / 6.1.2 files added\n'),
]


def blob(rel: str) -> str:
    out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} missing in {BASE} (fetch-depth 0 needed)"
    return out.replace(b"\r\n", b"\n").decode("utf-8")


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def tracked() -> set:
    return set(subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}


# ================================================================ W wiring ========================================================================

def test_W01_index_html_carries_exactly_two_marked_product_ui_blocks_and_is_otherwise_the_stage_61_page():
    html = now_text("frontend/index.html")
    assert html.count(HTML_OPEN) == 2 == html.count("<!-- <<< " + "Product UI 6.1.1 -->")
    assert "<!-- >>> Stage 6.1.1" not in html                                                   # the HTML markers never start like the Stage 6.1 ones (their strip would swallow the page)
    assert '<link rel="stylesheet" href="product_ui.css" />' in html and '<script src="product_ui.js"></script>' in html
    assert html.index('href="forward_validation.css"') < html.index('href="product_ui.css"') < html.index("</head>")
    assert html.index('src="forward_validation.js"') < html.index('src="product_ui.js"') and html.rindex("<script") == html.index('<script src="product_ui.js">')   # the shell loads last
    assert re.sub(STRIP_611_HTML, "", html) == blob("frontend/index.html")                        # outside the two blocks: byte-identical to the base (the Stage 6.1 blocks included)
    assert len(re.findall(STRIP_611_HTML, html)) == 2
    for t in ("overview", "daily", "watchlist", "agent", "alerts", "setups", "history", "portfolio", "review", "strategy", "paper", "trader", "settings"):
        assert f'data-tab="{t}"' in html, t                                                          # every legacy tab is still in the markup: functionality preserved, reachable through MORE


def test_W02_the_shell_reads_get_routes_only_and_delegates_its_two_actions_to_the_legacy_buttons():
    js = now_text("frontend/product_ui.js")
    assert 'method: "POST"' not in js and "/api/execution/auto" not in js and not re.search(r"/api/execution-safety/(pause|resume|emergency|clear)", js) and "pause-new-entries" not in js and "resume-new-entries" not in js
    assert "clear-emergency-stop" not in js and "/api/portfolio" not in js and "/api/agent" not in js and "/api/rh" not in js   # no Robinhood route: Robinhood is only named under MORE
    routes = re.findall(r'["`](/api/[a-z0-9_\-/]+)', js)
    assert len(routes) >= 10 and set(routes) <= GET_ROUTES, sorted(set(routes) - GET_ROUTES)
    assert "WebSocket(" not in js and "EventSource(" not in js                                       # no second stream: the Trader View socket stays the only one
    assert '\'#tv-safety [data-safety="stop"]\'' in js and '\'#tv-bot [data-bot="enable"]\'' in js and '\'#tv-bot [data-bot="disable"]\'' in js
    assert js.count(".click()") >= 3                                                                 # the delegation: the legacy buttons are clicked, their handlers do the (unchanged) POSTs
    for sel in ('[data-safety="pause"]', '[data-safety="resume"]', '[data-safety="clear"]', '[data-bot="disable"]'):
        assert sel in js, sel                                                                          # every control delegates to a legacy button (pause / resume / clear / disable)
    assert not re.search(r"(?i)reset trade", js) and "resume-new-entries" not in js and "clear-emergency-stop" not in js   # no Reset Trade; no safety route is called by the shell itself
    assert "data-action" not in js and "Prepare Buy" not in js and "Prepare Sell" not in js and "Add Alert" not in js   # no fake primary action; the disabled placeholders stay under View details
    assert "localStorage" in js and '"pui.details"' in js                                             # the only persisted thing: the View details preference, per browser
    assert "location.hash" in js and 'already !== "overview"' in js                                   # a deep link (/#daily-brief) or an already-switched tab wins over the TRADE default
    assert not re.search(r"(?i)retry", js) and "systemModel" in js and "chartModel" in js and "diagModel" in js   # polish pass: no fake Retry; the SYSTEM / chart / diagnostics summaries are pure models
    nav = re.search(r"const NAV = \[(.*?)\];", js, re.S).group(1)
    assert re.findall(r'label: "([A-Z]+)"', nav) == ["TRADE", "POSITIONS", "ACTIVITY", "MORE"] and 'const DEFAULT = "trade";' in js
    assert re.findall(r'host: "([a-z]+)"', nav) == ["trader", "paper", "alerts", "settings"]


def test_W03_wording_current_and_the_legacy_notes_hidden_not_deleted():
    js, css = now_text("frontend/product_ui.js"), now_text("frontend/product_ui.css")
    for s in STALE:
        assert s not in js and s not in css, s
    assert TAGLINE in js
    for rule in ("body.pui nav.tabs { display: none; }", "body.pui .disclaimer { display: none; }", "body.pui #tab-paper .unavailable-panel { display: none; }",
                 "body.pui #tab-settings .unavailable-panel { display: none; }", "body.pui #tab-portfolio .pf-readonly-note { display: none; }", "body.pui.pui-collapsed .tv-right { display: none; }"):
        assert rule in css, rule
    assert "#tv-actions" not in css and "tv-safety" not in css and "tv-bot" not in css                 # the detail panels keep their own look and controls
    assert not re.search(r"(?i)\b(profitable|winning|guaranteed|buy now|sell now|you should)\b", js)
    assert "Robinhood holdings are read only" in js and "Alpaca PAPER account only" in js
    assert "NEEDS ATTENTION" in js and "Risk status unknown" in js and "Validation status unavailable." in js   # problems and gaps are shown, never hidden
    for msg in ("No position", "No active setup", "Waiting for market data", "Account data unavailable", "Market closed", "No activity yet", "Critical issue requires attention", "Waiting for broker fill",
                "EMERGENCY STOP ACTIVE", "Existing positions remain protected.", "Existing positions continue normally.", "Existing positions continue to be monitored.", "Pause New Trades", "Resume New Trades",
                "Clear Emergency Stop", "Disable Auto", "No automatic replacement order.", "Checking Paper Auto status…", "Paper Auto status unavailable", "Could not enable Paper Auto. "):
        assert msg in js, msg                                                                           # standard messages; critical states stay in plain words
    assert "body.pui.pui-collapsed #tv-status-line { display: none; }" in css and "body.pui:not(.pui-collapsed) #pui-diag { display: none; }" in css   # one diagnostics line in the simple view; the chips return with View details
    assert "body.pui.pui-chart-empty .tv-chart-wrap { flex: 0 0 auto; height: 250px; min-height: 250px; }" in css      # compact chart empty state


# ================================================================ N node tests ====================================================================

def test_N01_node_tests_of_the_product_shell():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "product_ui.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 20\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


# ================================================================ H harness =======================================================================

def test_H01_harness_carries_the_product_ui_flow_and_opens_the_details_before_every_trader_view_detail_flow():
    src = now_text("browser_tests/flows.py")
    assert "def product_ui(c: Ctx):" in src and "def _tv_details(c: Ctx):" in src
    assert "cash_flow, forward_validation, product_ui, product_flow, product_flow_degraded, product_flow_lab, product_flow_links, product_flow_ready, product_flow_session, automation, saved_scans," in src   # Stage 6.1.2 flows follow product_ui; the last eight stay (tests/test_browser_harness_39.py)
    assert src.count("c.js(TV_JS)") == 12 and len(re.findall(r"^([ \t]*)c\.js\(TV_JS\)\n\1_tv_details\(c\)\n", src, re.M)) == 9   # every prior flow; product_ui tests the collapsed default first; Stage 6.1.2 installs the helpers once more (_pf_boot)
    flow = src[src.index("def product_ui(c: Ctx):"):src.index("\n\nFLOWS = (")]
    assert 'c.http("POST"' not in flow and ".post(" not in flow                                             # the flow writes only through the shell's buttons (the legacy POSTs), never itself
    for shot in ("product_ui_trade", "product_ui_positions", "product_ui_activity", "product_ui_more", "product_ui_trade_1280", "product_ui_trade_768", "product_ui_chart_empty", "product_ui_paused",
                 "product_ui_emergency_stop", "product_ui_auto_on", "product_ui_auto_on_reload"):
        assert f'c.shot("{shot}"' in flow, shot
    assert "c.b.viewport(1280, 720)" in flow and "__tvInView('#tv-ppe [data-ppe=\\\"stretch\\\"]')" in flow and "__tvActions()" in flow
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert names.index("product_ui") == names.index("forward_validation") + 1 and names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]
    assert "GET /api/forward-validation" in F.POLLING and "GET /api/execution/status" in F.POLLING          # the shell adds no poll of its own


# ================================================================ C CI ============================================================================

def test_C01_ci_manifest_carries_this_file_and_the_frontend_job_runs_every_node_test_file():
    groups = json.loads((ROOT / ".github" / "ci" / "pytest_groups.json").read_text(encoding="utf-8"))["groups"]
    assert "test_product_ui_611.py" in groups["core"]["files"]
    spec = importlib.util.spec_from_file_location("ci_pytest_groups", ROOT / "scripts" / "ci_pytest_groups.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    assert h.problems(h.load()) == []
    wf = now_text(".github/workflows/tests.yml")
    assert 'node --test "tests/frontend/*.test.cjs"' in wf and "python -m browser_tests.run" in wf


# ================================================================ F freeze ========================================================================

def test_F01_every_tracked_file_of_the_stage_61_base_is_identical_except_the_eight_wiring_files_and_the_five_new_ones():
    now_text = lambda rel, _read=globals()["now_text"]: ledger_view(rel, _read(rel))  # noqa: E731 - Stage 6.8: registered files are read as on main
    in_base = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    assert in_base, "the base commit must be reachable (fetch-depth 0)"
    new = {p for p in tracked() - in_base if not p.startswith(("scratchpad/", "runtime/", "data/", "browser_tests/artifacts/")) and not p.endswith((".pyc", ".db"))}
    assert new == NEW_611 | NEW_612 | NEW_FIX | NEW_62, {"unexpected_new": sorted(new - NEW_611 - NEW_612 - NEW_FIX - NEW_62), "missing_new": sorted((NEW_611 | NEW_612 | NEW_FIX | NEW_62) - new)}   # Stage 6.1.2 files + the WAL bootstrap fix + Stage 6.2
    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""} - new
    changed -= ledger_unchanged(changed, blob)                                                  # Stage 6.8: registered files equal to this base on main
    assert changed <= ALLOWED_CHANGED, {"unexpected_changes": sorted(changed - ALLOWED_CHANGED)}
    # Stage 6.1.2: api/server.py gains exactly two additive lines (the strategy-registry import and its GET-only router), nothing removed
    base_lines, now_lines = blob("api/server.py").splitlines(), now_text("api/server.py").splitlines()
    added = [l for l in now_lines if l not in base_lines]
    assert [l for l in base_lines if l not in now_lines] == [] and len(added) == 2 and all("Stage 6.1.2" in l and "strategy_registry" in l for l in added), added
    # belt and braces for the files a UI stage must never touch (bytes, CRLF-normalised)
    for rel in ("config.py", "trader_view/stream.py", "setups/engine.py", "proposals/engine.py", "execution/service.py", "execution/engine.py",
                "execution/broker.py", "execution/scheduler.py", "safety/engine.py", "forward_validation/metrics.py", "forward_validation/service.py", "forward_validation/store.py", "database/execution_migrations.py",
                "database/forward_validation_migrations.py", "api/routes/execution.py", "api/routes/execution_safety.py", "api/routes/forward_validation.py", "api/routes/trader_view.py",
                "frontend/trader_view.js", "frontend/trader_render.js", "frontend/paper_bot.js", "frontend/risk_controls.js", "frontend/trade_review.js", "frontend/forward_validation.js",
                "frontend/forward_validation.html", "frontend/app.js", "frontend/style.css", "frontend/trader_view.css", "scripts/start_stock_agent.ps1", "scripts/run_stock_agent.py",
                "requirements.txt", "requirements-dev.txt", ".github/workflows/tests.yml"):
        assert rel in in_base, rel
        assert now_text(rel) == blob(rel), f"{rel} differs from {BASE}"


def test_F02_strategy_execution_risk_and_broker_constants_and_the_validation_calculations_are_unchanged():
    import forward_validation as FV
    c = FV.frozen_controls()
    assert FV.frozen_hash() == FROZEN_HASH
    assert c["setup_type"] == "DOWNSIDE_DISLOCATION_RECOVERY" and c["watch_enter_stretch"] == -40.0 and c["watch_exit_stretch"] == -30.0 and c["ready_confidence"] == 40 and c["ready_demand"] == 60
    assert c["ready_exit_demand"] == 45 and c["invalidate_demand"] == 35 and c["invalidate_demand_evals"] == 2 and c["invalidate_stretch"] == -20.0 and c["evidence_max_age_days"] == 30
    assert c["risk_fraction"] == 0.005 and c["max_position_pct"] == 10.0 and c["max_daily_loss_pct"] == 3.0 and c["max_open_positions"] == 10 and c["proposal_ttl_s"] == 300
    assert c["max_hold_sessions"] == 5 and c["demand_exit"] == 35 and c["demand_exit_evals"] == 2 and c["stretch_exit"] == -20.0 and c["near_close_minutes"] == 15
    assert c["max_entries_per_cycle"] == 1 and c["max_exit_cycles"] == 3 and c["order_type"] == "market" and c["time_in_force"] == "day" and c["side"] == "LONG"
    assert c["leverage"] == "none" and c["shorts"] == "none" and c["exit_reasons"] == ["HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED"]
    assert c["session_rule"] == "regular session only (weekdays 09:30-16:00 ET), no extended hours"
    import execution as X
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO") and X.SIDE == "LONG" and X.ORDER_TYPE == "market"
    assert now_text("forward_validation/metrics.py") == blob("forward_validation/metrics.py") and now_text("forward_validation/__init__.py") == blob("forward_validation/__init__.py")
    assert now_text("execution/__init__.py") == blob("execution/__init__.py") and now_text("safety/__init__.py") == blob("safety/__init__.py") and now_text("setups/__init__.py") == blob("setups/__init__.py")


def test_F03_reconciled_prior_stage_tests_differ_from_the_base_only_by_one_marked_block_or_the_listed_exact_edits_and_nothing_else_carries_the_markers():
    now_text = lambda rel, _read=globals()["now_text"]: ledger_view(rel, _read(rel))  # noqa: E731 - Stage 6.8: registered files are read as on main
    for rel in BLOCK_TESTS:
        text = now_text(rel)
        assert text.count(OPEN_611) == 1 == text.count(CLOSE_611) and len(re.findall(STRIP_611, text)) == 1, rel
        block = re.findall(STRIP_611, text)[0]
        assert "Product UI 6\\.1\\.1" in block and "tests/test_product_ui_611.py" in block, rel     # the block strips the Product UI blocks of index.html and names this pin
        assert re.sub(STRIP_611, "", text) == blob(rel), f"{rel} differs from {BASE} outside its Stage 6.1.1 block"
    base61, now61 = blob("tests/test_forward_validation_61.py"), now_text("tests/test_forward_validation_61.py")
    text = base61
    for old, new in EDITS_61:
        assert text.count(old) == 1 and new not in text, old[:70]
        text = text.replace(old, new)
    for old, new in EDITS_612_61:                                                                 # Stage 6.1.2: its two in-place edits, applied after the six
        assert text.count(old) == 1 and new not in text, old[:70]
        text = text.replace(old, new)
    for old, new in EDITS_61_FIX:                                                                 # the WAL bootstrap fix: its in-place edits, applied after Stage 6.1.2's
        assert text.count(old) == 1 and new not in text, old[:70]
        text = text.replace(old, new)
    for old, new in EDITS_62_61:                                                                  # Stage 6.2: its in-place edits, applied after the fix's
        assert text.count(old) == 1 and new not in text, old[:70]
        text = text.replace(old, new)
    assert text == now61, "tests/test_forward_validation_61.py differs from the base by other than the six (6.1.1) + two (6.1.2) listed in-place edits"
    assert OPEN_611 not in now61 and now61.count("# >>> " + "Stage 6.1") == 0                   # the Stage 6.1 test stays a non-carrier (its own carrier scan)
    files = [p for p in tracked() if (ROOT / p).is_file() and not p.endswith((".pyc", ".db", ".png"))]
    py_carriers = sorted(p for p in files if OPEN_611.encode("utf-8") in (ROOT / p).read_bytes())
    assert py_carriers == sorted(BLOCK_TESTS), py_carriers
    html_carriers = sorted(p for p in files if HTML_OPEN.encode("utf-8") in (ROOT / p).read_bytes())
    assert html_carriers == ["frontend/index.html"], html_carriers


# ================================================================ D documentation ================================================================

def test_D01_design_document_and_readme_section():
    doc = now_text("DESIGN_611_PRODUCT_UI.md")
    for k in ("TRADE", "POSITIONS", "ACTIVITY", "MORE", "WATCHING", "READY", "ENTERING", "IN TRADE", "EXITING", "CLOSED", "NEEDS ATTENTION", "STOP TRADING", "Paper Auto",
              "View details", TAGLINE, "Display only", "byte-identical", BASE[:7]):
        assert k in doc, k
    assert OPEN_611 not in doc and HTML_OPEN not in doc
    readme = now_text("README.md")
    assert "## Product UI (Stage 6.1.1, four sections, display only)" in readme and TAGLINE in readme and "DESIGN_611_PRODUCT_UI.md" in readme
    assert readme.index("## Forward Paper Validation (Stage 6.1)") < readme.index("## Product UI (Stage 6.1.1") < readme.index("## Continuous integration (Stage 6.0.3")
    assert OPEN_611 not in readme and HTML_OPEN not in readme
