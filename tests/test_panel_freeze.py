"""
Risk controls panel (RISK_CONTROLS_PANEL_DESIGN.md Revision 3.5): the freeze pins, as every stage since 6.8.
  F01P  the change set since BASE_PANEL (main after the XSS-01 merge) is exactly NEW_PANEL + EDITED_PANEL; nothing is deleted; Stage 4.6B, the
        safety route, the local gate, app.js, config, the server and every migration are byte-identical; the CI manifest gains exactly the two
        new test files
  F02P  the eight server files change exactly the declared lines: the one behaviour change (safety/service.py: the Emergency Stop latched
        first, the failed evaluation's kill switch, the label flags) and the text-only edits the operator approved
  F03P  the page, the Trader View boot and the stylesheet change exactly the declared lines (the static notice: no script, no handler);
        product_ui.js keeps its GET routes and its legacy delegation (one more clickLegacy call: the status bar's Stop); risk_controls.js
        POSTs exactly the four routes; XSS-01's raw-interpolation list changes only for the three scripts the panel edits
  F04P  every changed older test function is declared and every bookkeeping edit is undone exactly
  F05P  the ledger changes only its docstring sentence and paragraph, its declarations, its two helpers, the re-hashed entries of the
        registered files the panel edits and the registrations NEW_LEDGER_PANEL declares (each unchanged from BASE_68 to BASE_PANEL)
  F06P  design 7a: XSS-01's freeze tests read XSS-01's side at fixed commits; XSS-01's set plus the panel's set is the whole change since
        BASE_XSS01; BASE_PANEL is an ancestor of HEAD with the reviewed tree; XSS-01's security tests still read the working tree
"""
import ast
import difflib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_PANEL = {"tests/test_risk_controls_panel.py", "tests/test_panel_freeze.py", "tests/frontend/risk_controls_order.test.cjs", "tests/frontend/risk_controls_view.test.cjs",
                   "browser_tests/risk_controls_panel_flows.py"}
CI_FILES_PANEL = ("test_risk_controls_panel.py", "test_panel_freeze.py")
SERVER_PANEL = {"safety/__init__.py", "safety/engine.py", "safety/service.py", "proposals/service.py", "execution/service.py", "execution/account_positions.py",
                "trader_view/stream.py", "strategy_registry/downside_recovery.py"}
FRONT_PANEL = {"frontend/risk_controls.js", "frontend/trader_render.js", "frontend/product_ui.js", "frontend/trader_view.js", "frontend/product_ui.css", "frontend/index.html"}
DOCS_PANEL = {"README.md", "DESIGN_591_EXECUTION_SAFETY.md"}
SEMANTIC_OTHER_PANEL = {"README.md", "browser_tests/flows.py"}                                # the runbook (section 8) and the harness, changed on purpose
TESTS_EDITED_PANEL = {"tests/frontend/xss01_raw_interpolations.json"}                       # XSS-01's list of raw interpolations, regenerated (F03P)
UNTOUCHED_PANEL = ("api/routes/alpaca_paper_orders.py", "api/routes/execution_safety.py", "api/local_gate.py", "api/server.py", "frontend/app.js", "frontend/alpaca_orders.js",
                   "frontend/forward_validation.html", "config.py", "safety/store.py", "safety/models.py", "execution/__init__.py", "tests/conftest.py", "browser_tests/cdp.py",
                   "browser_tests/app_server.py", "browser_tests/run.py", "tests/frontend/xss01_app_js.test.cjs", "tests/test_xss01_csp.py")
FORBIDDEN_PREFIX_PANEL = ("paper/", "database/", "autorun/", "forward_validation/", "forward_capture/", "setups/", "cash_flow/", "account_refresh/", "api/", "scripts/", "data/",
                          "agents/", "unified_portfolio/", "market_intel/", "research_data/", "swing_eval/", "swing_pullback/")
REVIEWED_XSS01_TREE = "4ee7f2808da88571a6886b4e69a210faf3fee175"
# the lines the panel adds to its three scripts: no new I/O channel, storage, order, broker or model token, no dynamic code or markup sink beyond innerHTML
SCRIPT_BAN_PANEL = re.compile(r"WebSocket\(|EventSource\(|sendBeacon|XMLHttpRequest|localStorage|sessionStorage|indexedDB|/v2/orders|alpaca\.markets|robinhood|anthropic|openai|"
                              r"\bclaude\b|submit\w*\(|cancel\w*\(|liquidat|\beval\(|new Function|document\.write|innerHTML\s*\+=|insertAdjacentHTML|outerHTML|window\.open|"
                              r"location\.(assign|replace|href\s*=)", re.I)
NOTICE = ('<div id="pui-boot-notice" class="pui-boot-notice" role="alert">If this message stays visible, the dashboard\'s scripts did not start and the Emergency Stop button may not be '
          'available. To save an Emergency Stop, run this in PowerShell on this computer, changing 8000 if this page uses another port:')
# the server files' exact line changes against BASE_PANEL (difflib, autojunk off), pinned verbatim
SERVER_LINE_OPS_PANEL = {                                                                 # file -> (main's removed lines, the panel's added lines), in order
    'safety/__init__.py': (
        ['STATE_LABELS = {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New trades paused", "HALTED": "Trading stopped", "UNKNOWN": "Trading stopped"}', '    R_PAUSE: "New trades paused by you",'],
        ['STATE_LABELS = {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New automatic entries paused", "HALTED": "New automatic entries stopped",', '                "UNKNOWN": "Risk status unknown"}             # risk controls panel (design 3.1): the state-only defaults; safety.engine.icon_label picks per flag', 'STATE_LABEL_DAILY_UNAVAILABLE = "New automatic entries blocked: daily-loss data unavailable"   # NO_NEW_ENTRIES with no Pause (safety.engine.decide)', '    R_PAUSE: "New automatic entries paused by you",']),
    'safety/engine.py': (
        ['                    REASON_TEXT, STATE_ICONS, STATE_LABELS)', 'def icon_label(state: str) -> Tuple[str, str]:', '    return STATE_ICONS.get(state, STATE_ICONS["UNKNOWN"]), STATE_LABELS.get(state, STATE_LABELS["UNKNOWN"])'],
        ['                    REASON_TEXT, STATE_ICONS, STATE_LABEL_DAILY_UNAVAILABLE, STATE_LABELS)', 'def icon_label(state: str, kill_active: bool = False, pause_active: bool = False) -> Tuple[str, str]:', '    """(icon, label). NO_NEW_ENTRIES has two meanings (a Pause, or a daily-loss control that cannot be verified), so the label follows the', '    pause flag (risk controls panel design 3.1, section 4). Every HALTED label says "automatic" while manual paper orders are not gated;', '    `kill_active` is for the Emergency Stop order gate, which makes "New entries stopped" true for the kill and changes that one label."""', '    icon = STATE_ICONS.get(state, STATE_ICONS["UNKNOWN"])', '    if state == "NO_NEW_ENTRIES" and not pause_active:', '        return icon, STATE_LABEL_DAILY_UNAVAILABLE', '    return icon, STATE_LABELS.get(state, STATE_LABELS["UNKNOWN"])']),
    'safety/service.py': (
        ['                              persisted=False, error=f"{type(exc).__name__}: {exc}"[:500], daily_loss=DailyLoss(trading_date=today))', '        icon, label = icon_label(state)'],
        ['Risk controls panel (design 3.1): an Emergency Stop is saved FIRST, before the account read, which can wait up to', 'CASH_FLOW_CYCLE_WAIT_SECONDS behind the cash-flow cycle; a Pause, a Resume and a Clear keep the order above. A failed evaluation still', 'reports a saved stop: its UNKNOWN view carries the kill switch read on its own (only active=True is information).', '            kill = KillSwitch()', '            try:                                                                                   # risk controls panel (B-3): no new route', '                st = self.store.state()', '                kill = KillSwitch(active=st.kill_active, activated_at=st.kill_activated_at, activated_by=st.kill_activated_by, reason=st.kill_reason)', '            except Exception:  # noqa: BLE001 - stays the default; an inactive kill switch in UNKNOWN is no information', '                pass', '                              persisted=False, error=f"{type(exc).__name__}: {exc}"[:500], daily_loss=DailyLoss(trading_date=today), kill_switch=kill)', '        if persist and action is not None and action["kind"] == "KILL_ON":', '            # risk controls panel (design 3.1): the Emergency Stop is durable before the account read below, which may wait for the cash-flow', '            # cycle. latch() changes only the kill columns and only while the kill is off; once it ran (set now, or already set) the action is', '            # never applied again, so a Clear committed during the account read stands. If it raised, the action stays: _decide applies it', '            # and _latch_fallback tries once more.', '            try:', '                self.store.latch(now, action["update"], {"kind": "KILL_ON", "actor": actor, "reason": action["reason"], "trading_date": today,', '                                                         "from_state": self._last.state if self._last is not None else None, "to_state": "HALTED",', '                                                         "input": {**action["update"], "saved_before_evaluation": True}})', '                action = None', '            except Exception as exc:  # noqa: BLE001', '                log.error("execution safety: KILL_ON could not be saved before the evaluation (%s: %s)", type(exc).__name__, exc)', '        icon, label = icon_label(state, kill_active=bool(kill.active), pause_active=bool(pause.active))']),
    'proposals/service.py': (
        ['            return {"allowed": False, "state": "UNKNOWN", "state_label": "Trading stopped", "reason_codes": ["SAFETY_GATE_NOT_CONNECTED"], "reason_text": "Safety gate not connected", "evaluated_at": None}'],
        ['            return {"allowed": False, "state": "UNKNOWN", "state_label": "Risk status unknown", "reason_codes": ["SAFETY_GATE_NOT_CONNECTED"], "reason_text": "Safety gate not connected", "evaluated_at": None}']),
    'execution/service.py': (
        ['            card.line = "Risk controls stopped new paper trades"'],
        ['            card.line = "Risk controls stopped new automatic paper trades"']),
    'execution/account_positions.py': (
        ['    "BOT": ("Bot-managed", "Opened by the bot from a broker-confirmed fill; the bot monitors it and decides its exit."),'],
        ['    "BOT": ("Bot-managed", "Opened by the bot from a broker-confirmed fill; the bot runs its exit rules while it is monitored; a position marked Needs attention gets no automatic exit."),']),
    'trader_view/stream.py': (
        ['                "safety": {"version": "safety_v1", "states": ["TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN"], "labels": {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New trades paused",', '                           "HALTED": "Trading stopped"}, "cadence": "on every proposal evaluation, on the in-session cadence, on each API read and user action; persisted"},'],
        ['                "safety": {"version": "safety_v1", "states": ["TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN"], "labels": {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New automatic entries paused",', '                           "HALTED": "New automatic entries stopped"}, "cadence": "on every proposal evaluation, on the in-session cadence, on each API read and user action; persisted"},']),
    'strategy_registry/downside_recovery.py': (
        ['        "Pause, the daily-loss halt and the Emergency Stop block new entries only; exits continue and the stop never liquidates. Monitor only keeps the exit rules running.",'],
        ['        "Pause, the daily-loss halt and the Emergency Stop block new automatic entries only; exit rules keep running while a position is monitored, a position marked "', '        "Needs attention gets no automatic exit, and the stop never liquidates. Monitor only keeps the exit rules running.",']),
}
# the page, the Trader View boot and the stylesheet: the same, pinned verbatim
PAGE_LINE_OPS_PANEL = {                                                                 # file -> (main's removed lines, the panel's added lines), in order
    'frontend/index.html': (
        [],
        ['<div id="pui-boot-notice" class="pui-boot-notice" role="alert">If this message stays visible, the dashboard\'s scripts did not start and the Emergency Stop button may not be available. To save an Emergency Stop, run this in PowerShell on this computer, changing 8000 if this page uses another port:', '<code>curl.exe -sS --max-time 60 -X POST http://127.0.0.1:8000/api/execution-safety/emergency-stop -H "X-Stock-Agent-Intent: dashboard" -H "Content-Type: application/json" -d "{}"</code>', 'More in README.md, "Emergency Stop fallback".</div>']),
    'frontend/trader_view.js': (
        ['    shell(); setupChart(); setMode(); renderPPE(null); renderDemand(null); renderSetup(null); renderProposal(null); renderActions(DEFAULT_ACTIONS);', '    try { const cfg = await (await fetch("/api/terminal/config")).json(); st.mode = cfg.mode; st.source = cfg.source; setMode(); renderActions(cfg.actions); } catch (e) { /* defaults stay */ }', '    try { const sy = await (await fetch("/api/terminal/symbols")).json(); if (sy && sy.watchlist) st.symbols = sy; } catch (e) { /* demo symbols stay */ }', '    renderWatchlist(null);', '    switchTo(st.symbols.default || (st.symbols.watchlist || [])[0] || "AAPL", true);'],
        ['    shell();', '    // risk controls panel (design 3.5): mounted directly after the shell, before anything that can throw, so Emergency Stop exists even if the', '    // chart or a later step fails; everything after the mount is guarded and logged', '    try {', '      setupChart(); setMode(); renderPPE(null); renderDemand(null); renderSetup(null); renderProposal(null); renderActions(DEFAULT_ACTIONS);', '      try { const cfg = await (await fetch("/api/terminal/config")).json(); st.mode = cfg.mode; st.source = cfg.source; setMode(); renderActions(cfg.actions); } catch (e) { /* defaults stay */ }', '      try { const sy = await (await fetch("/api/terminal/symbols")).json(); if (sy && sy.watchlist) st.symbols = sy; } catch (e) { /* demo symbols stay */ }', '      renderWatchlist(null);', '      switchTo(st.symbols.default || (st.symbols.watchlist || [])[0] || "AAPL", true);', '    } catch (e) { if (window.console) window.console.error("Trader View: boot failed after the risk controls mounted", e); }']),
    'frontend/product_ui.css': (
        ['body.pui:not(.pui-collapsed) .pui-card .pui-ctl h5, body.pui:not(.pui-collapsed) .pui-card .pui-ctl-text, body.pui:not(.pui-collapsed) .pui-card .pui-confirm { display: none; }   /* the detail column shows the legacy panels; the strip keeps the three status rows */'],
        ['body.pui:not(.pui-collapsed) .pui-card .pui-ctl h5, body.pui:not(.pui-collapsed) .pui-card .pui-ctl-text { display: none; }   /* the detail column shows the legacy panels; the strip keeps the three status rows */', "/* Risk controls panel (RISK_CONTROLS_PANEL_DESIGN.md 3.4, 3.6): the status bar's Emergency Stop, its confirmation and messages, the outcome lines", '   shown in both layouts, and the static notice that is hidden for its first 3 s and shown at once if this stylesheet fails to load. */', 'button.pui-seg.pui-bar-stop { font-weight: 700; }', '.pui-status .pui-confirm { flex-basis: 100%; margin: 4px 0 0; }', '.pui-bar-msg { font-size: 12px; font-weight: 600; }', '.pui-banner { flex-basis: 100%; padding: 8px 10px; border: 2px solid var(--red); border-radius: 8px; background: rgba(248, 81, 73, .1); color: var(--text); font-size: 12px; }', '.pui-banner b, .pui-banner code, .pui-banner span { display: block; margin: 2px 0; }', '.pui-banner code, .pui-boot-notice code { white-space: pre-wrap; word-break: break-all; font-size: 11px; }', '.pui-outcome { margin: 2px 0; font-size: 12px; font-weight: 600; }', '.pui-boot-notice { visibility: hidden; animation: pui-boot-show 0s linear 3s forwards; margin: 8px; padding: 10px 12px; border: 2px solid #c62828; border-radius: 8px;', '                   background: #fff3f3; color: #5d1010; font: 13px/1.45 system-ui, sans-serif; }', '.pui-boot-notice code { display: block; margin: 6px 0; }', '@keyframes pui-boot-show { to { visibility: visible; } }']),
}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    run = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {commit} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def at_base(rel):
    return at(FL.BASE_PANEL, rel)


# J-04 Stop reliability (design 7a, as XSS-01's freeze test did when the panel changed XSS-01's files): the files J-04 edits are read at BASE_J04,
# the panel's side as merged, a commit that cannot change; every other file still reads the working tree. tests/test_j04_freeze.py F06J pins that
# this list is exactly J-04's edited files and that BASE_J04 is an ancestor of HEAD with the reviewed tree.
J04_MOVED = ('.github/ci/pytest_groups.json', 'DESIGN_591_EXECUTION_SAFETY.md', 'README.md', 'api/routes/execution_safety.py',
             'browser_tests/flows.py', 'browser_tests/risk_controls_panel_flows.py', 'frontend/forward_validation.html',
             'frontend/index.html', 'frontend/product_ui.js', 'frontend/risk_controls.js', 'frontend/trader_render.js',
             'tests/freeze_ledger.py', 'tests/frontend/risk_controls_order.test.cjs', 'tests/frontend/risk_controls_view.test.cjs',
             'tests/frontend/xss01_raw_interpolations.json', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py',
             'tests/test_decision_flow_610c.py', 'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py',
             'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_ex06_freeze.py',
             'tests/test_ex06_local_gate.py', 'tests/test_execution_safety_591.py', 'tests/test_forward_paper_610a.py',
             'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py',
             'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py',
             'tests/test_market_scan_75.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py',
             'tests/test_sip_eval_78b1.py', 'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py',
             'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py',
             'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_prb.py', 'tests/test_wave2_prc.py')


def at_j04(rel):
    return at(FL.BASE_J04, rel)


# The Emergency Stop order gate (design 7a, its design section 12, as J-04 did here): the files the gate edits are read at BASE_GATE, the panel's
# side as J-04 left it, a commit that cannot change; every other file still reads the working tree. tests/test_gate_freeze.py F06G pins that this
# list is exactly the gate's edited files and that BASE_GATE is an ancestor of HEAD with the reviewed tree.
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
              'tests/test_execution_safety_591.py', 'tests/test_forward_paper_610a.py', 'tests/test_j04_freeze.py',
              'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py',
              'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py',
              'tests/test_market_scan_75.py', 'tests/test_proposals_59.py', 'tests/test_research_flow_47.py',
              'tests/test_risk_controls_panel.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_rotation_handoff_47.py',
              'tests/test_runtime_reliability_ops.py', 'tests/test_setups_58.py', 'tests/test_sip_eval_78b1.py',
              'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_trader_view_55.py',
              'tests/test_trader_view_561.py', 'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py',
              'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_prb.py', 'tests/test_wave2_prc.py')


def at_gate(rel):
    return at(FL.BASE_GATE, rel)


def now_text(rel):
    if rel in J04_MOVED:
        return at_j04(rel)
    if rel in GATE_MOVED:
        return at_gate(rel)
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    # J-04 (design 7a): the panel's side is the fixed diff from `base` to BASE_J04; the working tree's change set belongs to tests/test_j04_freeze.py
    return set(_git("diff", "--name-only", base, FL.BASE_J04).split())


def ledger_j04():
    """The ledger as J-04 found it (BASE_J04): the hashes the panel's bookkeeping is checked against."""
    t = at_j04("tests/freeze_ledger.py")
    led = t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]
    return ast.literal_eval(led[len("LEDGER = "):])


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


def test_F01P_the_change_set_is_exactly_the_declared_files_and_no_route_gate_migration_or_4_6b_file_changed():
    assert FL.BASE_PANEL == "4bd9d874c041acdc88d9a375b9a796f4d3d7a8a7" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_PANEL}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_PANEL)
    assert changed == FL.NEW_PANEL | FL.EDITED_PANEL, {"unexpected": sorted(changed - FL.NEW_PANEL - FL.EDITED_PANEL), "missing": sorted((FL.NEW_PANEL | FL.EDITED_PANEL) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_PANEL).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_PANEL == NEW_FILES_PANEL and {p for p in changed if p in in_base} == FL.EDITED_PANEL
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_PANEL).split()
    production = {p for p in FL.EDITED_PANEL if not p.startswith(("tests/", "browser_tests/", ".github/"))}
    assert production == SERVER_PANEL | FRONT_PANEL | DOCS_PANEL and set(FL.SEMANTIC_PANEL) == SERVER_PANEL | FRONT_PANEL | SEMANTIC_OTHER_PANEL
    assert {p for p in FL.EDITED_PANEL if p.startswith("tests/") and not p.endswith((".py", ".cjs"))} == TESTS_EDITED_PANEL
    assert all(len(v) > 60 for v in FL.SEMANTIC_PANEL.values())
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_PANEL)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_PANEL))
    for rel in UNTOUCHED_PANEL:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_PANEL, FL.BASE_GATE, "--", "api/routes/alpaca_paper_orders.py", "paper", "database"], cwd=ROOT).returncode == 0   # Stage 4.6B and every migration (the order gate, design 7a: the panel's side, to BASE_GATE)
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "core"} == {g: v for g, v in base_groups.items() if g != "core"}
    i = base_groups["core"]["files"].index("test_xss01_freeze.py") + 1
    assert groups["core"] == {**base_groups["core"], "files": base_groups["core"]["files"][:i] + list(CI_FILES_PANEL) + base_groups["core"]["files"][i:]}
    assert ledger_j04()[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))   # J-04: the manifest and its hash, both at BASE_J04


def test_F02P_the_server_files_change_exactly_the_declared_lines():
    assert set(SERVER_LINE_OPS_PANEL) == SERVER_PANEL
    for rel, (removed_want, added_want) in SERVER_LINE_OPS_PANEL.items():
        base, now, removed, added = line_ops(rel)
        assert (removed, added) == (removed_want, added_want), rel
        assert len(now) == len(base) + len(added) - len(removed), rel
        assert FL.pre_panel(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    svc = now_text("safety/service.py")
    i, j = svc.index('        if persist and action is not None and action["kind"] == "KILL_ON":'), svc.index("            acct = self.account(now.timestamp(), overrides)")
    assert i < j and svc.count("self.store.latch(") == 2                              # latch first (here) and the fallback (_latch_fallback): nothing else writes a latch
    import safety.service as S
    assert S.MAX_STATE_ATTEMPTS == 3 and S.NO_CASH_FLOW_SOURCES == ("LOCAL_PAPER", "HYPOTHETICAL_REVIEW")


def test_F03P_the_page_the_stylesheet_and_the_scripts_keep_their_routes_and_delegation():
    assert set(PAGE_LINE_OPS_PANEL) == {"frontend/index.html", "frontend/trader_view.js", "frontend/product_ui.css"}
    for rel, (removed_want, added_want) in PAGE_LINE_OPS_PANEL.items():
        base, now, removed, added = line_ops(rel)
        assert (removed, added) == (removed_want, added_want), rel
        assert FL.pre_panel(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    notice = PAGE_LINE_OPS_PANEL["frontend/index.html"][1]
    assert notice[0].startswith(NOTICE) and notice[2] == 'More in README.md, "Emergency Stop fallback".</div>'
    assert "<script" not in "\n".join(notice) and not re.search(r"\son[a-z]+=", "\n".join(notice))   # no handler, no inline script: script-src 'self' holds
    css_removed, css_added = PAGE_LINE_OPS_PANEL["frontend/product_ui.css"]
    assert css_added[0] == css_removed[0].replace(", body.pui:not(.pui-collapsed) .pui-card .pui-confirm", "")   # the one-word edit (design 3.4)
    pu, bpu = now_text("frontend/product_ui.js"), at_base("frontend/product_ui.js")
    routes = lambda s: sorted(set(re.findall(r"[\"`](/api/[a-z0-9_\-/]+)", s)))  # noqa: E731
    assert routes(pu) == routes(bpu)                                                    # no new route: the shell reads what it read
    assert pu.count("clickLegacy(") == bpu.count("clickLegacy(") + 1 == 6 and pu.count("fetch(") == bpu.count("fetch(")   # one more delegation: the bar's Stop
    assert 'method: "POST"' not in pu and "const LEGACY = " in pu and pu[pu.index("const LEGACY = "):pu.index("function legacyAvailable")] == bpu[bpu.index("const LEGACY = "):bpu.index("function legacyAvailable")]
    rc = now_text("frontend/risk_controls.js")
    assert sorted(set(re.findall(r'"(/api/execution-safety/[a-z-]+)"', rc))) == ["/api/execution-safety/clear-emergency-stop", "/api/execution-safety/emergency-stop",
                                                                                  "/api/execution-safety/pause-new-entries", "/api/execution-safety/resume-new-entries"]
    assert rc.count('method: "POST"') == 1 and rc.count("fetch(") == 1 and "/api/account-refresh" in rc
    for rel in ("frontend/product_ui.js", "frontend/risk_controls.js", "frontend/trader_render.js"):
        added = "\n".join(line_ops(rel)[3])
        assert added and not SCRIPT_BAN_PANEL.search(added), (rel, SCRIPT_BAN_PANEL.findall(added)[:5])
    tv = now_text("frontend/trader_view.js")
    i = tv.index("    shell();\n")
    assert tv.index('window.RiskControls.mount("tv-safety")', i) < tv.index("setupChart();", i) < tv.index("switchTo(", i)   # mounted before anything that can throw (design 3.5)
    raw, base_raw = json.loads(now_text("tests/frontend/xss01_raw_interpolations.json")), json.loads(at_base("tests/frontend/xss01_raw_interpolations.json"))
    assert {m for m in set(raw) | set(base_raw) if raw.get(m) != base_raw.get(m)} == {"product_ui.js", "risk_controls.js", "trader_render.js"}   # regenerated, XSS-01's scanner unchanged


def test_F04P_every_changed_older_test_function_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_PANEL if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = defs(at_base(rel)), defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    actual |= {f"{rel}::*" for rel in FL.EDITED_PANEL if rel.startswith("tests/frontend/") and rel.endswith(".cjs")}
    declared = set(FL.SEMANTIC_TESTS_PANEL)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^PANEL-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_PANEL.values())
    edited_tests = {p for p in FL.EDITED_PANEL if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_PANEL) == edited_tests
    for rel in sorted(FL.UNDO_PANEL):
        assert FL.undo_panel(rel, now_text(rel)) == at_base(rel), rel                           # now -> 4bd9d874, byte for byte
        for old, new in FL.UNDO_PANEL[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:80])
    for rel in FL.SEMANTIC_PANEL:
        assert FL.pre_panel(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    FL.UNDO_PANEL["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_panel("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_panel("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_PANEL["tests/_probe.py"]


def test_F05P_the_ledger_changes_only_its_declarations_its_two_helpers_and_the_rehashed_entries():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    for a in ("LEDGER = {", "BASE_XSS01 = ", "BASE_PANEL = ", "\n\ndef unchanged(", "\n\ndef pre_xss01(", "\n\ndef pre_panel(", "\n\nRisk controls panel (", " + NEW_XSS01 + NEW_PANEL plus this ledger."):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_PANEL = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_PANEL", "NEW_PANEL", "EDITED_PANEL", "SEMANTIC_PANEL", "INSTRUMENTATION_PANEL", "NEW_LEDGER_PANEL", "UNDO_PANEL", "SEMANTIC_TESTS_PANEL"]
    assert FL.INSTRUMENTATION_PANEL == {}
    assert set(ledger) - set(main_ledger) == FL.NEW_LEDGER_PANEL and set(main_ledger) <= set(ledger)   # the only new registrations are declared
    for rel in sorted(FL.NEW_LEDGER_PANEL):
        assert FL.base_text(rel) == at_base(rel) != now_text(rel), rel                              # unchanged from BASE_68 to main and edited here, as XSS-01's registrations were
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == {k for k in main_ledger if k in FL.EDITED_PANEL}, sorted(rehashed ^ {k for k in main_ledger if k in FL.EDITED_PANEL})
    for rel in rehashed | FL.NEW_LEDGER_PANEL:
        assert ledger[rel] == FL.sha(now_text(rel)), rel                                       # J-04: the panel's hashes against the panel's texts, both at BASE_J04
        if rel not in J04_MOVED and rel not in GATE_MOVED:
            assert FL.LEDGER[rel] == ledger[rel], rel                                           # a file J-04 did not touch still carries the panel's hash today
    start = fl.index("\n\nRisk controls panel (") + 1
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_PANEL = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_panel("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_XSS01 + NEW_PANEL plus this ledger.", " + NEW_XSS01 plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, two helpers and the ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06P_design_7a_xss01s_evidence_at_fixed_commits_and_an_unbroken_chain():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_PANEL, "HEAD"], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_PANEL}^{{tree}}").strip() == REVIEWED_XSS01_TREE
    xss01 = set(_git("diff", "--name-only", FL.BASE_XSS01, FL.BASE_PANEL).split())
    panel = changed_files(FL.BASE_PANEL)
    whole = changed_files(FL.BASE_XSS01)
    assert xss01 == FL.NEW_XSS01 | FL.EDITED_XSS01                                             # XSS-01's set, as merged
    in_xss01_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_XSS01).split())
    assert whole == (xss01 | panel) - {p for p in xss01 & panel if p in in_xss01_base and now_text(p) == at(FL.BASE_XSS01, p)}, sorted(whole ^ (xss01 | panel))
    xf = now_text("tests/test_xss01_freeze.py")
    assert 'subprocess.run(["git", "show", f"{FL.BASE_PANEL}:{rel}"], cwd=ROOT, capture_output=True)' in xf and xf.count("at_panel(") == 12   # the definition and the eleven reads moved to the fixed view
    assert "def changed_files(base):" in xf and "    changed = xss01_files()\n" in xf
    # the fixed view cannot drift: XSS-01's own F01Y keeps both checks (BASE_PANEL is an ancestor of HEAD, its tree is the reviewed one)
    f01y = xf[xf.index("def test_F01Y_"):xf.index("\ndef test_", xf.index("def test_F01Y_") + 10)]
    assert '    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_PANEL, "HEAD"], cwd=ROOT).returncode == 0\n' in f01y
    assert f'    assert _git("rev-parse", f"{{FL.BASE_PANEL}}^{{{{tree}}}}").strip() == "{REVIEWED_XSS01_TREE}"\n' in f01y
    for live in ("def test_F02Y_", "def test_F03Y_"):                                         # XSS-01's pins on files the panel must not change stay on the working tree
        body = xf[xf.index(live):xf.index("\ndef test_", xf.index(live) + 10)]
        assert "at_panel(" not in body, live
    assert now_text("tests/frontend/xss01_app_js.test.cjs") == at_base("tests/frontend/xss01_app_js.test.cjs") and now_text("tests/test_xss01_csp.py") == at_base("tests/test_xss01_csp.py")
