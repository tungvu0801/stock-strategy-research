"""
tests/test_product_flow_612.py — Stage 6.1.2 UNIFIED PRODUCT FLOW + STRATEGY LAB FOUNDATION (navigation, presentation, information architecture and a
read-only strategy registry; nothing about strategy logic, risk / safety, the Alpaca execution semantics, exactly-once orders or reconciliation changed).

Focused tests:
  W wiring / wording of the shell: exactly four primary sections, the legacy tabs are never peers, the legacy deep links map, MORE is three groups, GET routes
    only (the registry and the frozen controls added), no POST, the empty states are explicit, no regime / no fake score, index.html untouched
  A the GET-only API on the Stage 6.0 fake stack: shapes, route words, 404 / 400, the lifecycle derivation from persisted facts; no mutation endpoint, no
    broker / execution call from the registry package
  R the registry model (INTERNAL and QUANTCONNECT sources, the lifecycle statuses, placeholders) and the Strategy Standard v1 validation (execution fields
    rejected, score not a probability unless calibrated, a placeholder never emits a signal)
  D the Downside Dislocation Recovery sections: thresholds equal the frozen controls / module constants; backtest, out-of-sample and decision log are
    "Not yet evaluated"; paper results live or unavailable; nothing fabricated
  N the node tests · H the harness flows · C the CI manifest · F the freeze (base 1825958: every tracked file identical except the listed wiring /
    reconciliation files; the exact in-place edits of the reconciled tests; the frozen controls hash) · Z the documents and the README section
"""
import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "1825958fdf0fe6e18393a729fa2db32a28ce239f"                                          # origin/main: Stage 6.1 + 6.1.1 + the Paper Auto state-sync fix (PR #19)
NEW_612 = {"strategy_registry/__init__.py", "strategy_registry/models.py", "strategy_registry/standard.py", "strategy_registry/registry.py", "strategy_registry/service.py",
           "strategy_registry/downside_recovery.py", "api/routes/strategy_registry.py", "tests/test_product_flow_612.py", "tests/frontend/product_flow.test.cjs",
           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}
ALLOWED_CHANGED = {"api/server.py", "frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/test_forward_validation_61.py",
                   "tests/frontend/product_ui.test.cjs", "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json", "database/database.py"}   # + the WAL bootstrap fix
from test_db_bootstrap_busy import EDITS_61_FIX, EDITS_611_FIX, GROUPS_EDIT_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix pins (tests/test_db_bootstrap_busy.py)
from test_strategy_validation_62 import EDITS_62_61, EDITS_62_611, NEW_62, undo_62  # noqa: E402 - the Stage 6.2 pins (tests/test_strategy_validation_62.py)
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"             # the Stage 6.1 frozen controls: unchanged
MARK_OPEN, MARK_CLOSE = "# >>> " + "Product Flow 6.1.2", "# <<< " + "Product Flow 6.1.2"    # built, not literal: this file is not a marked-block carrier
STRIP_PF = r"(?ms)^# >>> Product Flow 6\.1\.2\b.*?^# <<< Product Flow 6\.1\.2[^\n]*\n\n\n"
GET_ROUTES = {"/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events",
              "/api/proposals/history", "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/",
              "/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls"}
LEGACY_TABS = ("overview", "daily", "watchlist", "agent", "alerts", "setups", "history", "portfolio", "review", "strategy", "paper", "trader", "settings")
SECTIONS = ("overview", "hypothesis", "rules", "data_requirements", "entry_logic", "invalidation", "exit_logic", "holding_horizon", "backtest", "out_of_sample", "risk_notes", "paper_results", "decision_log")
STALE = ("never places a trade", "This tool never places", "no order path", "Not available in this version", "read-only Trader View", "never places, prepares", "STOP TRADING", "Reset Trade")
FLOWS_612 = ["product_flow", "product_flow_degraded", "product_flow_lab", "product_flow_links", "product_flow_ready", "product_flow_session"]
SHOTS_612 = ("product_flow_trade", "product_flow_positions", "product_flow_activity", "product_flow_more", "product_flow_degraded", "product_flow_stopped", "product_flow_lab",
             "product_flow_lab_detail", "product_flow_link_portfolio", "product_flow_ready", "product_flow_session")

# ---- the exact in-place edits of the reconciled files (each `old` occurs once in the base; applied in order they produce the current file) --------------
NEW_612_LINES = ('NEW_612 = {"strategy_registry/__init__.py", "strategy_registry/models.py", "strategy_registry/standard.py", "strategy_registry/registry.py", "strategy_registry/service.py",\n'
                 '           "strategy_registry/downside_recovery.py", "api/routes/strategy_registry.py", "tests/test_product_flow_612.py", "tests/frontend/product_flow.test.cjs",\n'
                 '           "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"}')
NEW_611_LINE = 'NEW_611 = {"frontend/product_ui.js", "frontend/product_ui.css", "tests/test_product_ui_611.py", "tests/frontend/product_ui.test.cjs", "DESIGN_611_PRODUCT_UI.md"}'
EDITS_612_61 = [                                                                                 # tests/test_forward_validation_61.py (also replayed by tests/test_product_ui_611.py)
    (NEW_611_LINE + "   # Stage 6.1.1 files\n", NEW_611_LINE + "   # Stage 6.1.1 files\n" + NEW_612_LINES + "   # Stage 6.1.2 files\n"),
    ('    assert new == NEW_FILES | NEW_611, {"unexpected_new": sorted(new - NEW_FILES - NEW_611), "missing_new": sorted((NEW_FILES | NEW_611) - new)}   # Stage 6.1.1 files added\n',
     '    assert new == NEW_FILES | NEW_611 | NEW_612, {"unexpected_new": sorted(new - NEW_FILES - NEW_611 - NEW_612), "missing_new": sorted((NEW_FILES | NEW_611 | NEW_612) - new)}   # Stage 6.1.1 / 6.1.2 files added\n'),
]
EDITS_611 = [                                                                                    # tests/test_product_ui_611.py
    (NEW_611_LINE + "\n"
     'ALLOWED_CHANGED = {"frontend/index.html", "tests/test_trader_view_561.py", "tests/test_execution_60.py", "tests/test_cash_flow_602.py", "tests/test_forward_validation_61.py",\n'
     '                   "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json"}\n',
     NEW_611_LINE + "\n" + NEW_612_LINES + "   # Stage 6.1.2 files (tests/test_product_flow_612.py pins them)\n"
     'ALLOWED_CHANGED = {"frontend/index.html", "tests/test_trader_view_561.py", "tests/test_execution_60.py", "tests/test_cash_flow_602.py", "tests/test_forward_validation_61.py",\n'
     '                   "browser_tests/flows.py", "README.md", ".github/ci/pytest_groups.json", "api/server.py"}   # Stage 6.1.2: two additive router lines in api/server.py (checked below)\n'),
    ('GET_ROUTES = {"/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events",\n'
     '              "/api/proposals/history", "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/"}\n',
     'GET_ROUTES = {"/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events",\n'
     '              "/api/proposals/history", "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/",\n'
     '              "/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls"}   # Stage 6.1.2: the strategy registry and the frozen controls (GET only)\n'),
    ("     '        assert text.count(OPEN_61) == n == text.count(CLOSE_61) and len(re.findall(STRIP_61, text)) == n, rel\\n'),\n]\n",
     "     '        assert text.count(OPEN_61) == n == text.count(CLOSE_61) and len(re.findall(STRIP_61, text)) == n, rel\\n'),\n]\n"
     "# Stage 6.1.2 reconciles tests/test_forward_validation_61.py in place too (two exact edits, applied after EDITS_61; tests/test_product_flow_612.py pins them as well)\n"
     "EDITS_612_61 = [\n"
     "    ('NEW_611 = {\"frontend/product_ui.js\", \"frontend/product_ui.css\", \"tests/test_product_ui_611.py\", \"tests/frontend/product_ui.test.cjs\", \"DESIGN_611_PRODUCT_UI.md\"}   # Stage 6.1.1 files\\n',\n"
     "     'NEW_611 = {\"frontend/product_ui.js\", \"frontend/product_ui.css\", \"tests/test_product_ui_611.py\", \"tests/frontend/product_ui.test.cjs\", \"DESIGN_611_PRODUCT_UI.md\"}   # Stage 6.1.1 files\\n'\n"
     "     'NEW_612 = {\"strategy_registry/__init__.py\", \"strategy_registry/models.py\", \"strategy_registry/standard.py\", \"strategy_registry/registry.py\", \"strategy_registry/service.py\",\\n'\n"
     "     '           \"strategy_registry/downside_recovery.py\", \"api/routes/strategy_registry.py\", \"tests/test_product_flow_612.py\", \"tests/frontend/product_flow.test.cjs\",\\n'\n"
     "     '           \"DESIGN_612_UNIFIED_PRODUCT_FLOW.md\", \"DESIGN_612_STRATEGY_REGISTRY.md\", \"DESIGN_612_QUANTCONNECT_ADAPTER.md\", \"STRATEGY_STANDARD_V1.md\"}   # Stage 6.1.2 files\\n'),\n"
     "    ('    assert new == NEW_FILES | NEW_611, {\"unexpected_new\": sorted(new - NEW_FILES - NEW_611), \"missing_new\": sorted((NEW_FILES | NEW_611) - new)}   # Stage 6.1.1 files added\\n',\n"
     "     '    assert new == NEW_FILES | NEW_611 | NEW_612, {\"unexpected_new\": sorted(new - NEW_FILES - NEW_611 - NEW_612), \"missing_new\": sorted((NEW_FILES | NEW_611 | NEW_612) - new)}   # Stage 6.1.1 / 6.1.2 files added\\n'),\n"
     "]\n"),
    ('    assert "cash_flow, forward_validation, product_ui, automation, saved_scans," in src                     # before the clock-moving flows; the last eight stay (tests/test_browser_harness_39.py)\n'
     '    assert src.count("c.js(TV_JS)") == 11 and len(re.findall(r"^([ \\t]*)c\\.js\\(TV_JS\\)\\n\\1_tv_details\\(c\\)\\n", src, re.M)) == 9   # every prior flow; product_ui tests the collapsed default first\n',
     '    assert "cash_flow, forward_validation, product_ui, product_flow, product_flow_degraded, product_flow_lab, product_flow_links, product_flow_ready, product_flow_session, automation, saved_scans," in src   # Stage 6.1.2 flows follow product_ui; the last eight stay (tests/test_browser_harness_39.py)\n'
     '    assert src.count("c.js(TV_JS)") == 12 and len(re.findall(r"^([ \\t]*)c\\.js\\(TV_JS\\)\\n\\1_tv_details\\(c\\)\\n", src, re.M)) == 9   # every prior flow; product_ui tests the collapsed default first; Stage 6.1.2 installs the helpers once more (_pf_boot)\n'),
    ('    assert new == NEW_611, {"unexpected_new": sorted(new - NEW_611), "missing_new": sorted(NEW_611 - new)}\n'
     '    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\\n")) - {""} - new\n'
     '    assert changed <= ALLOWED_CHANGED, {"unexpected_changes": sorted(changed - ALLOWED_CHANGED)}\n'
     '    # belt and braces for the files a UI stage must never touch (bytes, CRLF-normalised)\n'
     '    for rel in ("config.py", "api/server.py", "trader_view/stream.py", "setups/engine.py", "proposals/engine.py", "execution/service.py", "execution/engine.py",\n',
     '    assert new == NEW_611 | NEW_612, {"unexpected_new": sorted(new - NEW_611 - NEW_612), "missing_new": sorted((NEW_611 | NEW_612) - new)}   # Stage 6.1.2 files added\n'
     '    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\\n")) - {""} - new\n'
     '    assert changed <= ALLOWED_CHANGED, {"unexpected_changes": sorted(changed - ALLOWED_CHANGED)}\n'
     '    # Stage 6.1.2: api/server.py gains exactly two additive lines (the strategy-registry import and its GET-only router), nothing removed\n'
     '    base_lines, now_lines = blob("api/server.py").splitlines(), now_text("api/server.py").splitlines()\n'
     '    added = [l for l in now_lines if l not in base_lines]\n'
     '    assert [l for l in base_lines if l not in now_lines] == [] and len(added) == 2 and all("Stage 6.1.2" in l and "strategy_registry" in l for l in added), added\n'
     '    # belt and braces for the files a UI stage must never touch (bytes, CRLF-normalised)\n'
     '    for rel in ("config.py", "trader_view/stream.py", "setups/engine.py", "proposals/engine.py", "execution/service.py", "execution/engine.py",\n'),
    ('    for old, new in EDITS_61:\n        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n'
     '    assert text == now61, "tests/test_forward_validation_61.py differs from the base by other than the six listed in-place edits"\n',
     '    for old, new in EDITS_61:\n        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n'
     '    for old, new in EDITS_612_61:                                                                 # Stage 6.1.2: its two in-place edits, applied after the six\n'
     '        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n'
     '    assert text == now61, "tests/test_forward_validation_61.py differs from the base by other than the six (6.1.1) + two (6.1.2) listed in-place edits"\n'),
]
EDITS_CJS = [                                                                                    # tests/frontend/product_ui.test.cjs (the Stage 6.1.1 node tests, reconciled expectations)
    ('test("2 MORE reaches every legacy workspace that is not one of the four hosts, plus Forward Validation and Advanced configuration", () => {\n'
     '  const hosts = new Set(PUI.NAV.map((n) => n.host));\n'
     '  const tabs = PUI.MORE.flatMap((g) => g.items.filter((i) => i.tab).map((i) => i.tab));\n'
     '  assert.deepEqual(new Set(tabs), new Set(LEGACY_TABS.filter((t) => !hosts.has(t))));\n'
     '  assert.deepEqual(PUI.MORE.map((g) => g.title), ["Research", "Tools", "System", "Settings"]);\n'
     '  assert.ok(PUI.MORE[2].items.some((i) => i.href === "forward_validation.html"));\n'
     '  assert.deepEqual(PUI.MORE[3].items.map((i) => i.label), ["Advanced configuration"]);\n'
     '  const h = PUI.moreHtml(PUI.moreGroups([{ view: "fit", label: "Strategy Fit" }]));\n'
     '  assert.ok(h.includes(\'data-pui-more="tab:strategy"\') && h.includes(\'data-pui-more="tab:strategy:fit"\') && h.includes(\'data-pui-more="tab:agent"\') && h.includes(\'data-pui-more="action:settings"\'));\n',
     'test("2 MORE reaches every legacy workspace that is not one of the four hosts, in three groups (Stage 6.1.2: Research / System / Settings), plus Forward Validation and Advanced configuration", () => {\n'
     '  const hosts = new Set(PUI.NAV.map((n) => n.host));\n'
     '  assert.deepEqual(new Set(PUI.moreTabs()), new Set(LEGACY_TABS.filter((t) => !hosts.has(t))));                // Stage 6.1.2: the Diagnostics tools (a nested fold) count\n'
     '  assert.deepEqual(PUI.MORE.map((g) => g.title), ["Research", "System", "Settings"]);                           // Stage 6.1.2: the Tools group folded into System › Diagnostics\n'
     '  assert.ok(PUI.MORE[1].items.some((i) => i.href === "forward_validation.html"));\n'
     '  assert.deepEqual(PUI.MORE[2].items.map((i) => i.label), ["Risk", "Automation", "Notifications", "Advanced configuration"]);\n'
     '  const h = PUI.moreHtml(PUI.moreGroups([{ view: "fit", label: "Strategy Fit" }]));\n'
     '  assert.ok(h.includes(\'data-pui-more="tab:strategy"\') && h.includes(\'data-pui-more="action:lab"\') && h.includes(\'data-pui-more="tab:agent"\') && h.includes(\'data-pui-more="action:settings"\'));   // Stage 6.1.2: the lab workspaces are listed inside the registry panel\n'),
    ('  for (const k of ["Trades", "Open", "Closed", "Paper P/L", "Issues"]) assert.ok(h.includes(`<h4>${k}</h4>`), k);\n',
     '  for (const k of ["System", "Market", "Setup changes", "READY setups", "Entries", "Exits", "Blocked", "Safety blocks", "Paper P/L", "Attention"]) assert.ok(h.includes(`<h4>${k}</h4>`), k);   // Stage 6.1.2: the Daily Review strip\n'),
    ('  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",\n'
     '                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/"];\n',
     '  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",\n'
     '                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/",\n'
     '                   "/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls"];                    // Stage 6.1.2: the registry and the frozen controls (GET)\n'),
    ('  assert.ok(!PUI.cardsHtml({ ...CTX, safety: KILLED }).includes("TRADING STOPPED"));\n});\n',
     '  assert.ok(!/pui-big">TRADING STOPPED/.test(PUI.cardsHtml({ ...CTX, safety: KILLED })));                                  // the headline reads EMERGENCY STOP ACTIVE; Stage 6.1.2 names the combination (AUTO … · TRADING STOPPED) on the session line only\n});\n'),
]
EDITS_FLOWS = [                                                                                  # browser_tests/flows.py outside the Product Flow 6.1.2 block
    ('           "GET /api/forward-validation")                                                                                # Stage 6.1: the Forward Paper Validation panel\'s 60 s poll\n',
     '           "GET /api/forward-validation",                                                                                # Stage 6.1: the Forward Paper Validation panel\'s 60 s poll\n'
     '           "GET /api/strategy-registry", "GET /api/forward-validation/frozen-controls")                                 # Stage 6.1.2: the shell\'s two one-off startup reads (registry, frozen controls)\n'),
    ('    c.r.check("ACTIVITY: the Today strip and a timeline of persisted events only (setups, proposals, orders, fills, positions, safety gate, reconciliation, validation)",\n'
     '              today == ["Trades", "Open", "Closed", "Paper P/L", "Issues"] and set(kinds) <= {"setup", "proposal", "order", "fill", "position", "exit", "attention", "safety", "reconciliation", "validation"}\n',
     '    c.r.check("ACTIVITY: the Today strip (Stage 6.1.2: the Daily Review counts) and a timeline of persisted events only (setups, proposals, orders, fills, positions, safety gate, reconciliation, validation)",\n'
     '              today == ["System", "Market", "Setup changes", "READY setups", "Entries", "Exits", "Blocked", "Safety blocks", "Paper P/L", "Attention"] and set(kinds) <= {"setup", "proposal", "order", "fill", "position", "exit", "attention", "safety", "reconciliation", "validation"}\n'),
    ('    c.step("__click(\'#pui-nav [data-pui-nav=\\"more\\"]\')", "document.getElementById(\'tab-settings\').classList.contains(\'active\') && document.querySelectorAll(\'#pui-more [data-pui-group]\').length === 4", 30)\n'
     '    groups = c.js("[...document.querySelectorAll(\'#pui-more [data-pui-group]\')].map((e) => e.dataset.puiGroup)")\n'
     '    items = c.js("[...document.querySelectorAll(\'#pui-more [data-pui-more]\')].map((e) => e.dataset.puiMore)")\n'
     '    c.r.check("MORE: Research / Tools / System / Settings groups reach every legacy workspace; the Settings placeholder is hidden until asked for",\n'
     '              groups == ["Research", "Tools", "System", "Settings"] and {"tab:strategy", "tab:setups", "tab:history", "tab:agent", "tab:overview", "tab:watchlist", "tab:daily", "tab:review", "tab:portfolio",\n'
     '                                                                              "forward_validation.html", "action:health", "action:risk", "action:settings"} <= set(items)\n',
     '    c.step("__click(\'#pui-nav [data-pui-nav=\\"more\\"]\')", "document.getElementById(\'tab-settings\').classList.contains(\'active\') && document.querySelectorAll(\'#pui-more [data-pui-group]\').length === 3", 30)\n'
     '    groups = c.js("[...document.querySelectorAll(\'#pui-more [data-pui-group]\')].map((e) => e.dataset.puiGroup)")\n'
     '    items = c.js("[...document.querySelectorAll(\'#pui-more [data-pui-more]\')].map((e) => e.dataset.puiMore)")\n'
     '    c.r.check("MORE (Stage 6.1.2): Research / System / Settings groups reach every legacy workspace (the tools under System › Diagnostics); the Settings placeholder is hidden until asked for",\n'
     '              groups == ["Research", "System", "Settings"] and {"action:lab", "tab:strategy", "tab:setups", "tab:history", "tab:agent", "tab:overview", "tab:watchlist", "tab:daily", "tab:review", "tab:portfolio",\n'
     '                                                                "forward_validation.html", "action:health", "action:data", "action:logs", "action:risk", "action:automation", "action:notifications", "action:settings"} <= set(items)\n'),
    ('         ai_evidence, history, scanner, small_screens, trader_view, market_demand, setup_state, trade_review, risk_controls, paper_bot, account_refresh, cash_flow, forward_validation, product_ui, automation, saved_scans, daily_brief, brief_delivery, notification_click,\n'
     '         paper_portfolio, alpaca_paper, alpaca_orders)          # Stage 5.6.1 / 5.7 / 5.8: the Trader View flows sit before the clock-moving flows\n',
     '         ai_evidence, history, scanner, small_screens, trader_view, market_demand, setup_state, trade_review, risk_controls, paper_bot, account_refresh, cash_flow, forward_validation, product_ui, product_flow, product_flow_degraded, product_flow_lab, product_flow_links, product_flow_ready, product_flow_session, automation, saved_scans, daily_brief, brief_delivery, notification_click,\n'
     '         paper_portfolio, alpaca_paper, alpaca_orders)          # Stage 5.6.1 / 5.7 / 5.8: the Trader View flows sit before the clock-moving flows; Stage 6.1.2: the product flows follow product_ui\n'),
]
GROUPS_EDIT = ('        "test_ppe_56.py",\n        "test_product_ui_611.py",', '        "test_ppe_56.py",\n        "test_product_flow_612.py",\n        "test_product_ui_611.py",')
SERVER_LINES = ("from api.routes import strategy_registry  # Stage 6.1.2: Strategy Lab registry + Strategy Standard v1 (GET only) - describes the setups -> proposals -> safety -> execution strategy; no order path",
                "app.include_router(strategy_registry.router)  # Stage 6.1.2: strategy registry + Strategy Standard v1 (GET only) - the setups -> proposals -> safety gate -> execution strategy described, never executed")


def blob(rel: str) -> str:
    r = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True)
    assert r.returncode == 0, f"{rel} missing in {BASE} (fetch-depth 0 needed)"                  # an empty file (database/__init__.py) is a valid, empty blob
    return r.stdout.replace(b"\r\n", b"\n").decode("utf-8")


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def tracked() -> set:
    return set(subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}


def replay(base: str, edits) -> str:
    text = base
    for old, new in edits:
        assert text.count(old) == 1 and new not in text, old[:80]
        text = text.replace(old, new)
    return text


def signal(**over) -> dict:
    return {"strategy_id": "DOWNSIDE_DISLOCATION_RECOVERY", "source": "INTERNAL", "asset_class": "US_EQUITY", "direction": "LONG", "symbol": "NVDA", "score": 72, "time_horizon": "SWING",
            "entry_condition": "setup READY", "invalidation_condition": "stretch > -20", "exit_condition": "TIME_EXIT after 5 sessions", "risk_class": "MODERATE",
            "generated_at": "2026-10-25T14:00:00Z", "version": "strategy_standard_v1", **over}


# ================================================================ W wiring / wording ============================================================

def test_W01_four_sections_legacy_links_three_more_groups_get_routes_only_and_the_page_untouched():
    js, css = now_text("frontend/product_ui.js"), now_text("frontend/product_ui.css")
    nav = re.search(r"const NAV = \[(.*?)\];", js, re.S).group(1)
    assert re.findall(r'label: "([A-Z]+)"', nav) == ["TRADE", "POSITIONS", "ACTIVITY", "MORE"] and 'const DEFAULT = "trade";' in js
    html = now_text("frontend/index.html")
    assert html == blob("frontend/index.html")                                                   # the shell evolves in place: the page carries no new block
    for t in LEGACY_TABS:
        assert f'data-tab="{t}"' in html, t                                                      # every legacy tab is still in the markup (hidden by the shell, never a peer)
    links = re.search(r"const LEGACY_LINKS = \{(.*?)\n  \};", js, re.S).group(1)
    for key in ("portfolio", "paper", '"paper-trades"', "trader", '"trader-view"', "strategy", '"strategy-lab"', "settings", "agent", "alerts", "daily", "review", "watchlist", "overview", "setups", "history", '"forward-validation"'):
        assert f"{key}:" in links, key
    assert 'portfolio: { section: "positions" }' in links and 'paper: { section: "activity", filter: "trades" }' in links and 'trader: { section: "trade" }' in links and 'strategy: { section: "more", more: "lab" }' in links
    more = re.search(r"const MORE = \[(.*?)\n  \];", js, re.S).group(1)
    assert re.findall(r'title: "([A-Za-z]+)"', more) == ["Research", "System", "Settings"] and 'title: "Tools"' not in more
    routes = re.findall(r'["`](/api/[a-z0-9_\-/]+)', js)
    assert set(routes) <= GET_ROUTES and {"/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls"} <= set(routes), sorted(set(routes) - GET_ROUTES)
    assert 'method: "POST"' not in js and "/api/execution/auto" not in js and not re.search(r"/api/execution-safety/(pause|resume|emergency|clear)", js) and "/api/portfolio" not in js and "/api/alpaca" not in js and "/api/rh" not in js
    assert js.count("clickLegacy(") == 5 and js.count("runAutoAction(") == 3                     # the Paper Auto / safety delegation of Stage 6.1.1 is untouched
    assert not re.search(r"setInterval\([^)]*(loadRegistry|loadFrozen|loadTerminal|renderLab)", js)   # no new poll
    assert "location.hash" in js and 'already !== "overview"' in js and 'addEventListener("hashchange"' in js and "replaceState" in js
    assert ".pui-focus { grid-column: 1 / -1;" in css and "details.pui-more-group > summary { cursor: pointer;" in css and "body.pui nav.tabs { display: none; }" in css


def test_W02_wording_explicit_empty_states_no_fake_data_no_regime_no_execution_promise():
    js, css = now_text("frontend/product_ui.js"), now_text("frontend/product_ui.css")
    for s in STALE:
        assert s not in js and s not in css, s
    for msg in ("Cannot enter", "Automatic entry off", "Entry allowed when every check passes", "No strategy result available", "No other strategy evaluated.", "No opportunity score",
                "Not yet evaluated", "Why still holding?", "READY means the setup is eligible. Execution eligibility is decided separately by the safety gate and the entry controls.",
                "Trading session", "placeholder · not active", "Reconciliation required.", "Account equity unavailable.", "Maximum open positions reached.", "Near-close entry cutoff reached.",
                "Trading safety is paused", "Trading safety is stopped", "Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled."):
        assert msg in js, msg
    assert not re.search(r"(?i)regime", js) and not re.search(r"(?i)\b(profitable|winning|guaranteed|buy now|sell now|you should)\b", js)
    assert not re.search(r"(?i)retry", js) and "data-action" not in js and "Prepare Buy" not in js and "Prepare Sell" not in js and "Add Alert" not in js
    assert not re.search(r"(?i)will (be )?(execut|plac|submit)", js) and not re.search(r"Momentum \d|Opportunity Score \d", js)
    assert not re.search(r"(?i)anthropic|claude|openai|/api/ai", js)                               # no model is consulted by the shell; the WHY panel is deterministic


# ================================================================ A the GET-only API ============================================================

@pytest.fixture
def stack(tmp_path, monkeypatch):
    import config
    import test_execution_60 as T
    from api.routes import trader_view as RT
    from execution import broker as BK
    monkeypatch.setattr(BK, "WIRE", lambda: (_ for _ in ()).throw(AssertionError("the real Alpaca wire was requested")))
    for k, v in (("TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0), ("MAX_DAILY_LOSS_PERCENT", 5.0), ("TRADE_PROPOSAL_HALT", False), ("TRADE_PROPOSAL_DAILY_LOSS_HALT", False),
                 ("EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", False), ("EXECUTION_SAFETY_STARTUP_POLICY", "KEEP"), ("MAX_OPEN_POSITIONS", 10), ("EXECUTION_MAX_SHARES_PER_ENTRY", 0)):
        monkeypatch.setattr(config, k, v)
    st = T.make_stack(tmp_path)
    monkeypatch.setitem(RT.STREAM, "instance", st.s)
    from fastapi.testclient import TestClient
    from api.server import app
    st.api, st.app, st.T = TestClient(app), app, T
    return st


def test_A01_registry_api_is_get_only_with_the_documented_shapes_and_derives_the_lifecycle_from_persisted_facts(stack):
    api = stack.api
    mine = {p: set(o) for p, o in stack.app.openapi()["paths"].items() if p.startswith("/api/strategy-registry")}
    assert mine == {"/api/strategy-registry": {"get"}, "/api/strategy-registry/standard": {"get"}, "/api/strategy-registry/{strategy_id}": {"get"},
                    "/api/strategy-registry/{strategy_id}/validation": {"get"}, "/api/strategy-registry/validation/runs/{run_id}": {"get"},   # Stage 6.2: the validation views (GET only)
                    "/api/strategy-registry/validation/runs/{run_id}/simulations": {"get"}, "/api/strategy-registry/validation/runs/{run_id}/audit": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|trade|alpaca|robinhood|paper|tune|optimi", p), p
    j = api.get("/api/strategy-registry").json()
    for k in ("version", "standard_version", "strategies", "sources", "lifecycle", "active_statuses", "pipeline", "quantconnect", "facts", "counts", "disclaimer"):
        assert k in j, k
    rows = {s["strategy_id"]: s for s in j["strategies"]}
    assert list(rows) == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"]
    ddr = rows["DOWNSIDE_DISLOCATION_RECOVERY"]
    assert ddr["implemented"] is True and ddr["active"] is True and ddr["declared_status"] == "PAPER_CANDIDATE" and ddr["status"] == "PAPER_CANDIDATE" and ddr["setup_type"] == "DOWNSIDE_DISLOCATION_RECOVERY"
    assert ddr["lifecycle_basis"] == ["no forward paper validation session recorded yet and Paper Auto is OFF"]
    vq = rows["VALUE_QUALITY"]                                                                    # Stage 6.4: implemented as a RESEARCH strategy - never active, no setup type
    assert vq["implemented"] is True and vq["active"] is False and vq["status"] == vq["declared_status"] == "RESEARCH" and vq["setup_type"] is None and vq["placeholder_note"] is None
    mo = rows["MOMENTUM"]                                                                         # Stage 6.5: implemented as a RESEARCH strategy - never active, no setup type
    assert mo["implemented"] is True and mo["active"] is False and mo["status"] == mo["declared_status"] == "RESEARCH" and mo["setup_type"] is None and mo["placeholder_note"] is None
    for pid in ("MEAN_REVERSION", "BREAKOUT"):
        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]
    assert {s["source"]: s["count"] for s in j["sources"]} == {"INTERNAL": 5, "QUANTCONNECT": 0} and j["quantconnect"]["integrated"] is False and j["quantconnect"]["registered"] == 0
    assert j["pipeline"] == ["Strategy", "Opportunity / Proposal", "Portfolio", "Risk / Safety", "Execution", "Broker"] and j["facts"]["mode"] == "MONITOR_ONLY" and j["facts"]["validation"]["sessions_completed"] == 0
    assert [l["status"] for l in j["lifecycle"]] == ["RESEARCH", "BACKTEST_CANDIDATE", "REVIEW", "PAPER_CANDIDATE", "PAPER_ACTIVE", "APPROVED", "REJECTED"] and j["counts"] == {"strategies": 5, "implemented": 3, "placeholders": 2}   # Stage 6.4: VALUE_QUALITY, Stage 6.5: MOMENTUM implemented
    s = api.get("/api/strategy-registry/standard").json()
    assert s["version"] == "strategy_standard_v1" and s["required"][:3] == ["strategy_id", "source", "asset_class"] and "qty" in s["forbidden_keys"] and "broker" in s["forbidden_keys"] and "not a probability" in s["score"]["note"].lower()
    d = api.get("/api/strategy-registry/downside_dislocation_recovery").json()                  # case-insensitive id
    assert [x["key"] for x in d["sections"]] == list(SECTIONS) and d["lifecycle"]["decided"] is False and d["entry"]["strategy_id"] == "DOWNSIDE_DISLOCATION_RECOVERY"
    assert {x["key"]: x["state"] for x in d["sections"] if x["key"] in ("backtest", "out_of_sample", "decision_log", "paper_results")} == {"backtest": "NOT_EVALUATED", "out_of_sample": "NOT_EVALUATED", "decision_log": "NOT_EVALUATED", "paper_results": "LIVE"}
    assert api.get("/api/strategy-registry/NOPE").status_code == 404 and api.get("/api/strategy-registry/NOPE").json()["status"] == "NOT_FOUND" and api.get("/api/strategy-registry/x!").status_code == 400
    m = api.get("/api/strategy-registry/MEAN_REVERSION").json()                                   # Stage 6.5: MOMENTUM is implemented; a placeholder
    assert m["entry"]["active"] is False and [x["state"] for x in m["sections"]] == ["DOCUMENTED"] + ["NOT_EVALUATED"] * 12 and all(x["note"].startswith("Not yet evaluated") for x in m["sections"][1:])
    mo_d = api.get("/api/strategy-registry/MOMENTUM").json()                                      # Stage 6.5: documented from its frozen rules, never active
    assert mo_d["entry"]["active"] is False and [x["state"] for x in mo_d["sections"]] == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2
    # the lifecycle follows persisted facts: Paper Auto ON reads PAPER_ACTIVE; APPROVED / REJECTED are never derived
    stack.T.arm(stack)
    j2 = api.get("/api/strategy-registry").json()
    assert j2["facts"]["mode"] == "PAPER_AUTO" and j2["strategies"][0]["status"] == "PAPER_ACTIVE" and j2["strategies"][0]["declared_status"] == "PAPER_CANDIDATE" and "Paper Auto is ON" in j2["strategies"][0]["lifecycle_basis"][0]
    assert all(s["status"] not in ("APPROVED", "REJECTED") for s in j2["strategies"]) and j2["strategies"][1]["status"] == "RESEARCH"


def test_A02_no_mutation_endpoint_and_no_execution_or_broker_call_from_the_registry_package():
    route = now_text("api/routes/strategy_registry.py")
    tree = ast.parse(route)
    decos = [ast.unparse(d.func) if isinstance(d, ast.Call) else ast.unparse(d) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for d in n.decorator_list]
    assert decos and all(d == "router.get" for d in decos), decos
    assert not re.search(r"router\.(post|put|patch|delete)|@router\.api_route", route) and "BaseModel" not in route   # no request body, nothing to write
    pkg = "".join(now_text(f"strategy_registry/{f}") for f in ("__init__.py", "models.py", "standard.py", "registry.py", "service.py", "downside_recovery.py")) + route
    assert not re.search(r"\.(enable_auto|disable_auto|run_cycle|submit\w*|place\w*|cancel\w*|post|put|delete)\(", pkg)       # no execution, broker or mutating call (the standard only NAMES the forbidden keys)
    for forbidden in ("AlpacaPaperBroker", "alpaca_order", "requests.", "httpx", "urllib", "socket", "anthropic", "sqlite3", "INSERT INTO", "UPDATE ", "DELETE FROM", ".broker", "wire()"):
        assert forbidden not in pkg, forbidden
    imports = {ast.unparse(n.names[0]) if isinstance(n, ast.Import) else (n.module or "") for n in ast.walk(ast.parse(pkg.replace(route, ""))) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not any(m.startswith(("execution.service", "execution.broker", "paper", "portfolio", "api.")) for m in imports), sorted(imports)


# ================================================================ R the registry model and the Strategy Standard v1 ===============================

def test_R01_registry_entries_sources_lifecycle_statuses_and_placeholders():
    import strategy_registry as SR
    from strategy_registry import registry as R, service as S
    from strategy_registry.models import StrategyEntry
    assert SR.SOURCES == ("INTERNAL", "QUANTCONNECT") and SR.STATUSES == ("RESEARCH", "BACKTEST_CANDIDATE", "REVIEW", "PAPER_CANDIDATE", "PAPER_ACTIVE", "APPROVED", "REJECTED")
    ids = [e.strategy_id for e in R.entries()]
    assert ids == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"] and [e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM"]   # + Stage 6.4, 6.5
    for e in R.entries():
        assert set(e.model_dump()) >= {"strategy_id", "display_name", "source", "asset_class", "direction_capability", "time_horizon", "status", "description", "version", "created_at", "updated_at"}
        assert (e.implemented and e.status == "PAPER_CANDIDATE" and e.setup_type == "DOWNSIDE_DISLOCATION_RECOVERY") or (not e.implemented and e.status == "RESEARCH" and e.setup_type is None and e.placeholder_note) \
            or (e.implemented and e.strategy_id in ("VALUE_QUALITY", "MOMENTUM") and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4 / 6.5: research strategies
    base = dict(strategy_id="QC_TEST", display_name="QC test", source="QUANTCONNECT", asset_class="US_EQUITY", direction_capability="LONG_SHORT", time_horizon="INTRADAY", status="RESEARCH",
                description="d", version="0.1", created_at="2026-10-25", updated_at="2026-10-25")
    assert StrategyEntry(**base).source == "QUANTCONNECT"                                      # a QuantConnect-sourced entry is a valid registry row
    for bad in (dict(source="BROKER"), dict(status="LIVE"), dict(strategy_id="qc"), dict(time_horizon="DAY"), dict(direction_capability="BOTH"), dict(created_at="yesterday"),
                dict(setup_type="X"), dict(status="PAPER_ACTIVE"), dict(implemented=True, status="RESEARCH", setup_type=None, display_name="")):
        with pytest.raises(ValueError):
            StrategyEntry(**{**base, **bad})
    for st in SR.STATUSES:
        StrategyEntry(**{**base, "implemented": True, "status": st, "setup_type": "SOME_SETUP"})     # every lifecycle status is representable for an implemented strategy
    ddr = R.get("downside_dislocation_recovery")
    assert ddr is not None and R.get("NOPE") is None
    assert S.derive_lifecycle(ddr, None)["status"] == "PAPER_CANDIDATE" and S.derive_lifecycle(ddr, {"available": True, "mode": "MONITOR_ONLY", "validation": {"sessions_completed": 0}})["status"] == "PAPER_CANDIDATE"
    assert S.derive_lifecycle(ddr, {"available": True, "mode": "PAPER_AUTO", "validation": {"sessions_completed": 0}})["status"] == "PAPER_ACTIVE"
    lc = S.derive_lifecycle(ddr, {"available": True, "mode": "MONITOR_ONLY", "validation": {"sessions_completed": 3, "sessions_target": 30}})
    assert lc["status"] == "PAPER_ACTIVE" and "3 session(s)" in lc["basis"][0] and lc["decided"] is False and "never derived" in lc["decision"]
    assert S.derive_lifecycle(R.get("MOMENTUM"), {"available": True, "mode": "PAPER_AUTO", "validation": {"sessions_completed": 9}})["status"] == "RESEARCH"   # a placeholder never becomes active
    v = S.registry_view({"available": False})
    assert v["counts"] == {"strategies": 5, "implemented": 3, "placeholders": 2} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4, 6.5
    assert v["sources"][1] == {"source": "QUANTCONNECT", "label": SR.SOURCE_LABELS["QUANTCONNECT"], "count": 0, "note": S.QUANTCONNECT_NOTE} and "nothing is wired" in S.QUANTCONNECT_NOTE
    assert "no strategy executes" in SR.DISCLAIMER and "not a probability" in SR.DISCLAIMER and SR.PIPELINE == ("Strategy", "Opportunity / Proposal", "Portfolio", "Risk / Safety", "Execution", "Broker")
    json.dumps(S.detail_view("BREAKOUT", None))


def test_R02_strategy_standard_v1_validation_rejects_execution_fields_uncalibrated_probabilities_and_placeholders():
    from strategy_registry import registry as R
    from strategy_registry.standard import ALL_FIELDS, REQUIRED_FIELDS, SCHEMA, StrategyStandardError, check_signal, forbidden_keys, validate_signal
    reg = R.by_id()
    sig = validate_signal(signal(), reg)
    assert sig.symbol == "NVDA" and sig.score == 72.0 and sig.score_kind == "RANK" and sig.calibration is None
    assert validate_signal(signal(score_kind="CALIBRATED_PROBABILITY", calibration="isotonic fit on the 2026 paper trades"), reg).score_kind == "CALIBRATED_PROBABILITY"
    assert validate_signal(signal(evidence={"ppe_stretch": -54, "demand": 72}), reg).evidence["demand"] == 72
    cases = {"qty": signal(qty=3), "evidence.order": signal(evidence={"order": {"side": "buy"}}), "evidence.nested": signal(evidence={"facts": [{"client_order_id": "x"}]}), "broker": signal(broker="alpaca"),
             "account": signal(account="PA123"), "notional": signal(notional="1000")}
    for name, bad in cases.items():
        r = check_signal(bad, reg)
        assert r["valid"] is False and r["error"]["code"] == "EXECUTION_FIELD_FORBIDDEN", (name, r["error"])
    assert forbidden_keys({"evidence": {"deep": [{"qty": 1}]}}) == ["evidence.deep[0].qty"]
    codes = {"missing": ({k: v for k, v in signal().items() if k != "symbol"}, "MISSING_FIELD"), "unknown": (signal(foo=1), "UNKNOWN_FIELD"), "score_high": (signal(score=101), "INVALID_VALUE"),
             "score_neg": (signal(score=-1), "INVALID_VALUE"), "score_text": (signal(score="72"), "INVALID_VALUE"), "direction": (signal(direction="LONG_SHORT"), "INVALID_VALUE"),
             "timestamp": (signal(generated_at="yesterday"), "INVALID_VALUE"), "empty_condition": (signal(exit_condition="  "), "INVALID_VALUE"), "symbol": (signal(symbol="nvda"), "INVALID_VALUE"),
             "probability": (signal(score_kind="CALIBRATED_PROBABILITY"), "SCORE_NOT_CALIBRATED"), "rank_with_calibration": (signal(calibration="x"), "SCORE_NOT_CALIBRATED"),
             "placeholder": (signal(strategy_id="MEAN_REVERSION"), "STRATEGY_NOT_IMPLEMENTED"), "unknown_strategy": (signal(strategy_id="VALUE"), "STRATEGY_UNKNOWN"),
             "source": (signal(source="QUANTCONNECT"), "SOURCE_MISMATCH"), "direction_capability": (signal(direction="SHORT"), "DIRECTION_NOT_SUPPORTED")}
    for name, (bad, code) in codes.items():
        r = check_signal(bad, reg)
        assert r["valid"] is False and r["error"]["code"] == code, (name, r["error"])
    for pid in ("MEAN_REVERSION", "BREAKOUT"):
        with pytest.raises(StrategyStandardError) as e:
            validate_signal(signal(strategy_id=pid), reg)
        assert e.value.code == "STRATEGY_NOT_IMPLEMENTED"                                        # a future placeholder never appears as an active signal
    for pid in ("VALUE_QUALITY", "MOMENTUM"):                                                     # Stage 6.4 / 6.5: a RESEARCH strategy never emits a live signal either
        with pytest.raises(StrategyStandardError) as e:
            validate_signal(signal(strategy_id=pid), reg)
        assert e.value.code == "STRATEGY_INACTIVE"
    from strategy_registry.models import StrategyEntry
    inactive = {**reg, "REVIEW_ONE": StrategyEntry(strategy_id="REVIEW_ONE", display_name="r", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING", status="REVIEW",
                                                   description="d", version="0.1", created_at="2026-10-25", updated_at="2026-10-25", implemented=True, setup_type="X")}
    assert check_signal(signal(strategy_id="REVIEW_ONE"), inactive)["error"]["code"] == "STRATEGY_INACTIVE"
    qc = {**reg, "QC_ONE": StrategyEntry(strategy_id="QC_ONE", display_name="q", source="QUANTCONNECT", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING", status="PAPER_CANDIDATE",
                                         description="d", version="0.1", created_at="2026-10-25", updated_at="2026-10-25", implemented=True, setup_type="QC_SETUP")}
    assert validate_signal(signal(strategy_id="QC_ONE", source="QUANTCONNECT"), qc).source == "QUANTCONNECT"   # the standard carries a QuantConnect-sourced signal like any other
    assert check_signal("not an object", reg)["error"]["code"] == "INVALID_VALUE" and validate_signal(signal()).strategy_id == "DOWNSIDE_DISLOCATION_RECOVERY"   # without a registry: shape only
    assert validate_signal(signal(direction="SHORT")).direction == "SHORT"                          # the direction capability is a registry rule: shape-only validation accepts SHORT
    assert tuple(SCHEMA["required"]) == REQUIRED_FIELDS and set(SCHEMA["optional"]) == set(ALL_FIELDS) - set(REQUIRED_FIELDS) and SCHEMA["score"]["default_kind"] == "RANK"
    assert any("never names an order" in r for r in SCHEMA["rules"]) and "qty" in SCHEMA["forbidden_keys"] and "side" in SCHEMA["forbidden_keys"]


# ================================================================ D Downside Dislocation Recovery ================================================

def test_D01_downside_recovery_sections_come_from_the_frozen_controls_and_nothing_is_fabricated():
    import execution as X
    import proposals as P
    import setups as S
    import forward_validation as FV
    from strategy_registry import downside_recovery as DR
    secs = {s.key: s for s in DR.sections(None)}
    assert list(secs) == list(SECTIONS)
    c = FV.frozen_controls()
    assert FV.frozen_hash() == FROZEN_HASH
    rules = secs["rules"]
    assert rules.state == "DOCUMENTED" and rules.facts["watch_enter_stretch"] == c["watch_enter_stretch"] == S.WATCH_ENTER_STRETCH == -40.0 and rules.facts["ready_demand"] == S.READY_DEMAND == 60
    assert rules.facts["invalidate_demand_evals"] == S.INVALIDATE_DEMAND_EVALS == 2 and rules.facts["evidence_max_age_days"] == S.EVIDENCE_MAX_AGE_DAYS == 30 and rules.facts["reason_texts"] == dict(S.REASON_TEXT)
    assert any("<= -40" in i for i in rules.items) and any("Confidence >= 40" in i and "Demand >= 60" in i for i in rules.items) and any("fixed before any outcome analysis" in i for i in rules.items)
    entry = secs["entry_logic"]
    assert entry.facts["execution_controls"] == list(X.CONTROLS) and entry.facts["proposal_controls"] == list(P.CONTROLS) and entry.facts["risk_fraction"] == c["risk_fraction"] == 0.005
    assert entry.facts["max_open_positions"] == c["max_open_positions"] == 10 and entry.facts["side"] == "LONG" and any(P.TEXT[P.B_EQUITY_NA] in i for i in entry.items) and any("exactly once" in i for i in entry.items)
    exit_ = secs["exit_logic"]
    assert exit_.facts["exit_reasons"] == list(X.EXIT_REASONS) and exit_.facts["exit_texts"] == dict(X.EXIT_TEXT) and exit_.facts["max_hold_sessions"] == c["max_hold_sessions"] == 5 and exit_.facts["demand_exit"] == c["demand_exit"] == 35
    assert exit_.items[0].startswith("Exit precedence (first match wins, every executor cycle): 1. HARD_INVALIDATION - Price reached the invalidation level") and any("No profit target, no predicted price" in i for i in exit_.items)
    assert secs["holding_horizon"].facts == {"max_hold_sessions": 5, "time_horizon": "SWING"} and secs["overview"].facts["frozen_controls_hash"] == FROZEN_HASH and secs["overview"].facts["leverage"] == "none" and secs["overview"].facts["shorts"] == "none"
    for k in ("backtest", "out_of_sample", "decision_log"):
        assert secs[k].state == "NOT_EVALUATED" and secs[k].items == [] and secs[k].facts == {} and secs[k].note.startswith("Not yet evaluated"), k
    assert "No historical backtest" in secs["backtest"].note and "different strategy family" in secs["backtest"].note and "No approval or rejection decision" in secs["decision_log"].note
    assert secs["paper_results"].state == "UNAVAILABLE" and secs["paper_results"].items == []               # no runtime facts in this process: said so, not invented
    live = {s.key: s for s in DR.sections({"available": True, "mode": "MONITOR_ONLY", "validation": {"sessions_completed": 2, "sessions_target": 30, "sessions_label": "2 / 30", "closed_trades": 0, "edge_status": "INSUFFICIENT_DATA",
                                                                                                     "edge_label": "Insufficient data", "insufficient": True, "insufficient_text": "INSUFFICIENT FOR STRATEGY CONCLUSION", "needs_attention": False, "attention": []}})}["paper_results"]
    assert live.state == "LIVE" and live.items[0] == "Forward paper validation sessions: 2 / 30 (2 / 30)" and live.items[1] == "Closed paper trades: 0 (no closed paper trade yet)" and "not a forecast" in live.note
    documented = [i for s in secs.values() if s.state == "DOCUMENTED" for i in s.items]
    assert not any(re.search(r"(?i)win rate [0-9]|expected return [0-9]|probability of|[0-9]+ ?% (return|win|edge)", i) for i in documented)   # no invented performance claim
    assert any("No expected return, recovery probability, win rate or edge is claimed" in i for i in secs["hypothesis"].items)
    for s in secs.values():
        assert s.state in ("NOT_EVALUATED", "UNAVAILABLE") or s.sources, s.key                              # every documented / live section names its sources
    assert set(DR.LINKS) == {"validation", "trade", "activity", "design_docs"} and DR.LINKS["validation"] == "forward_validation.html"


# ================================================================ N / H / C ========================================================================

def test_N01_node_tests_of_the_product_flow():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "product_flow.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 13\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "product_ui.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 20\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:]       # the Stage 6.1.1 node tests still pass (reconciled expectations)


def test_H01_harness_carries_the_six_product_flows_after_product_ui_and_the_flows_write_nothing_themselves():
    src = now_text("browser_tests/flows.py")
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    i = names.index("product_ui")
    assert names[i + 1:i + 7] == FLOWS_612 and names[i + 7] == "automation" and names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]
    assert src.count(MARK_OPEN) == 1 == src.count(MARK_CLOSE) and len(re.findall(STRIP_PF, src)) == 1
    block = re.findall(STRIP_PF, src)[0]
    assert all(f"def {n}(c: Ctx):" in block for n in FLOWS_612) and 'def _pf_boot(c: Ctx, path="/"):' in block and block.count("c.js(TV_JS)") == 1
    assert 'c.http("POST"' not in block and ".post(" not in block and "enable_auto(" not in block and "run_cycle(" not in block   # the flows drive the shell's buttons only; nothing is written by the harness itself
    for shot in SHOTS_612:
        assert f'c.shot("{shot}"' in block, shot
    assert "GET /api/strategy-registry" in F.POLLING and "GET /api/forward-validation/frozen-controls" in F.POLLING and "GET /api/execution/status" in F.POLLING
    assert "READY means the setup is eligible. Execution eligibility is decided separately by the safety gate and the entry controls." in block


def test_C01_ci_manifest_carries_this_file_in_core_and_the_node_glob_runs_the_new_file():
    groups = json.loads((ROOT / ".github" / "ci" / "pytest_groups.json").read_text(encoding="utf-8"))["groups"]
    assert "test_product_flow_612.py" in groups["core"]["files"] and "test_product_ui_611.py" in groups["core"]["files"]
    spec = importlib.util.spec_from_file_location("ci_pytest_groups", ROOT / "scripts" / "ci_pytest_groups.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    assert h.problems(h.load()) == []
    assert 'node --test "tests/frontend/*.test.cjs"' in now_text(".github/workflows/tests.yml")


# ================================================================ F freeze ========================================================================

def test_F01_every_tracked_file_of_the_base_is_identical_except_the_wiring_and_reconciliation_files_and_the_frozen_controls_are_unchanged():
    in_base = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    assert in_base, "the base commit must be reachable (fetch-depth 0)"
    new = {p for p in tracked() - in_base if not p.startswith(("scratchpad/", "runtime/", "data/", "browser_tests/artifacts/")) and not p.endswith((".pyc", ".db"))}
    assert new == NEW_612 | NEW_FIX | NEW_62, {"unexpected_new": sorted(new - NEW_612 - NEW_FIX - NEW_62), "missing_new": sorted((NEW_612 | NEW_FIX | NEW_62) - new)}   # + the WAL bootstrap fix test + Stage 6.2
    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""} - new
    assert changed <= ALLOWED_CHANGED, {"unexpected_changes": sorted(changed - ALLOWED_CHANGED)}
    for d in ("setups", "proposals", "safety", "execution", "account_refresh", "cash_flow", "ppe", "demand", "evidence", "paper", "forward", "forward_validation", "notifications", "trader_view", "database", "scripts", "api/routes"):
        for rel in sorted(p for p in in_base if p.startswith(d + "/") and p.endswith((".py", ".ps1"))):
            assert rel == "database/database.py" or now_text(rel) == blob(rel), f"{rel} differs from {BASE}"   # database.py: the WAL bootstrap fix, pinned by tests/test_db_bootstrap_busy.py
    for rel in ("config.py", "frontend/index.html", "frontend/app.js", "frontend/style.css", "frontend/trader_view.js", "frontend/trader_render.js", "frontend/paper_bot.js", "frontend/risk_controls.js",
                "frontend/trade_review.js", "frontend/forward_validation.js", "frontend/forward_validation.html", "frontend/strategy_lab.js", ".github/workflows/tests.yml", "requirements.txt", "requirements-dev.txt",
                "tests/conftest.py", "tests/test_browser_harness_39.py", "tests/test_trader_view_55.py", "tests/test_trader_view_561.py", "tests/test_execution_60.py", "tests/test_cash_flow_602.py", "tests/test_account_refresh_601.py",
                "tests/test_ci_groups_603.py", "DESIGN_611_PRODUCT_UI.md", "DESIGN_61_FORWARD_PAPER_VALIDATION.md", "DESIGN_60_AUTONOMOUS_PAPER.md"):
        assert rel in in_base, rel
        assert now_text(rel) == blob(rel), f"{rel} differs from {BASE}"
    import forward_validation as FV
    import execution as X
    c = FV.frozen_controls()
    assert FV.frozen_hash() == FROZEN_HASH and c["setup_type"] == "DOWNSIDE_DISLOCATION_RECOVERY" and c["max_open_positions"] == 10 and c["max_hold_sessions"] == 5 and c["side"] == "LONG"
    assert c["exit_reasons"] == ["HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED"] and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    base_lines, now_lines = blob("api/server.py").splitlines(), now_text("api/server.py").splitlines()
    assert [l for l in base_lines if l not in now_lines] == [] and [l for l in now_lines if l not in base_lines] == list(SERVER_LINES)
    assert [l for l in now_lines if l not in SERVER_LINES] == base_lines                           # order-exact: removing the two added lines gives the base file line for line


def test_F02_reconciled_files_differ_from_the_base_only_by_the_listed_exact_edits_and_one_marked_harness_block():
    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611 + EDITS_611_FIX + EDITS_62_611) == now_text("tests/test_product_ui_611.py"), "tests/test_product_ui_611.py: other than the listed in-place edits"
    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_612_61 + EDITS_61_FIX + EDITS_62_61) == now_text("tests/test_forward_validation_61.py"), "tests/test_forward_validation_61.py: other than the listed edits"
    assert replay(blob("tests/frontend/product_ui.test.cjs"), EDITS_CJS) == now_text("tests/frontend/product_ui.test.cjs"), "tests/frontend/product_ui.test.cjs: other than the four listed edits"
    flows = undo_62("browser_tests/flows.py", now_text("browser_tests/flows.py"))                 # Stage 6.2: its marked block and its FLOWS edit undone first
    assert len(re.findall(STRIP_PF, flows)) == 1 and replay(blob("browser_tests/flows.py"), EDITS_FLOWS) == re.sub(STRIP_PF, "", flows), "browser_tests/flows.py: other than the Product Flow block and the listed edits"
    old, new = GROUPS_EDIT
    assert blob(".github/ci/pytest_groups.json").count(old) == 1 and blob(".github/ci/pytest_groups.json").replace(old, new).replace(*GROUPS_EDIT_FIX) == undo_62(".github/ci/pytest_groups.json", now_text(".github/ci/pytest_groups.json"))
    readme = undo_62("README.md", now_text("README.md"))                                          # Stage 6.2: its README section removed first
    section = re.findall(r"(?ms)^## Unified product flow \+ Strategy Lab foundation \(Stage 6\.1\.2\)\n.*?(?=^## Continuous integration \(Stage 6\.0\.3)", readme)
    assert len(section) == 1 and readme.replace(section[0], "") == blob("README.md"), "README.md: other than the inserted Stage 6.1.2 section"
    files = [p for p in tracked() if (ROOT / p).is_file() and not p.endswith((".pyc", ".db", ".png"))]
    assert sorted(p for p in files if MARK_OPEN.encode("utf-8") in (ROOT / p).read_bytes()) == ["browser_tests/flows.py"]
    for rel in ("tests/test_product_ui_611.py", "tests/test_forward_validation_61.py", "README.md", "DESIGN_612_UNIFIED_PRODUCT_FLOW.md", "DESIGN_612_STRATEGY_REGISTRY.md", "DESIGN_612_QUANTCONNECT_ADAPTER.md", "STRATEGY_STANDARD_V1.md"):
        text = now_text(rel)
        assert ("# >>> " + "Stage 6.1") not in text and ("<!-- >>> " + "Product UI 6.1") not in text and MARK_OPEN not in text, rel   # no marker carrier is added


# ================================================================ Z documents ======================================================================

def test_Z01_documents_and_the_readme_section():
    flow = now_text("DESIGN_612_UNIFIED_PRODUCT_FLOW.md")
    for k in ("TRADE · POSITIONS · ACTIVITY · MORE", "START / RUNNING -> TRADE -> Opportunity -> READY -> Proposal / Auto Entry -> IN TRADE -> POSITIONS -> EXIT -> ACTIVITY",
              "MORE -> Strategy Lab -> Research -> Backtest -> Review -> Paper Candidate -> Paper Active -> Approved / Rejected", "Complex inside, simple outside", "One blocker surface",
              "Cannot enter", "READY is setup eligibility", "Why?", "priority", "Daily Review", "No fake data", "Not yet evaluated", BASE[:7], "exactly-once", "frozen_controls()"):
        assert k in flow, k
    reg = now_text("DESIGN_612_STRATEGY_REGISTRY.md")
    for k in ("strategy_id", "display_name", "source", "asset_class", "direction_capability", "time_horizon", "status", "description", "version", "created_at", "updated_at", "INTERNAL | QUANTCONNECT",
              "RESEARCH -> BACKTEST_CANDIDATE -> REVIEW -> PAPER_CANDIDATE -> PAPER_ACTIVE -> APPROVED | REJECTED", "never derived", "GET /api/strategy-registry", "never executes", "Not yet evaluated"):
        assert k in reg, k
    qc = now_text("DESIGN_612_QUANTCONNECT_ADAPTER.md")
    for k in ("QuantConnect / LEAN", "normalized strategy signal", "Stock Agent Strategy Adapter", "Opportunity Ranking", "Portfolio / Risk / Safety", "Alpaca Paper", "MUST NOT bypass", "not a broker", "not built", "Not built in this stage"):
        assert k in qc, k
    std = now_text("STRATEGY_STANDARD_V1.md")
    for k in ("strategy_id", "source", "asset_class", "direction", "symbol", "score", "time_horizon", "entry_condition", "invalidation_condition", "exit_condition", "risk_class", "generated_at", "version",
              "not a probability", "no strategy places a broker order", "EXECUTION_FIELD_FORBIDDEN", "STRATEGY_NOT_IMPLEMENTED", "Strategy (evidence / signal) -> Opportunity / Proposal -> Portfolio -> Risk / Safety -> Execution -> Broker"):
        assert k in std, k
    readme = now_text("README.md")
    assert "## Unified product flow + Strategy Lab foundation (Stage 6.1.2)" in readme and "Strategies produce evidence and signals; they never execute" in readme
    assert readme.index("## Product UI (Stage 6.1.1") < readme.index("## Unified product flow + Strategy Lab foundation (Stage 6.1.2)") < readme.index("## Continuous integration (Stage 6.0.3")
    assert not re.search(r"(?i)(?<!not )(?<!never )ready to buy", readme)
    for doc in (flow, reg, qc, std, readme):
        assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now)\b", doc)
