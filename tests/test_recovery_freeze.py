"""
Bot-position recovery (BOT_POSITION_RECOVERY_DESIGN.md revision 3.5, decisions R-1 to R-5 of 2026-11-09; Task AD): the freeze pins, as every stage since 6.8.
  F01R  the change set since BASE_REC (main after the order gate merged, PR #60) is exactly NEW_REC + EDITED_REC; nothing is deleted; the broker, the
        engine, the execution store, models and scheduler, every migration, config, the API and the server, the Stage 4.6B package, the safety package,
        forward validation's package constants, store, P/L view and run command, app.js, the risk controls and stop scripts, trader_render.js and
        paper_bot.js are byte-identical; the production files are exactly the declared ones; the CI manifest gains exactly the two new test files
  F02R  the declared changes only: the VERSION line and the one EXIT_TEXT entry; in the executor exactly enable_auto and run_cycle; in forward
        validation exactly attention, fresh_daily and fresh_dailies change and interventions and _interventions_by_date appear; daily_report's NOTES
        lines; forward capture's one query; product_ui's one ACTIVITY line; the README's F6 row and its paragraph
  F03R  the scripts keep their routes, fetch sites and POSTs; no added line carries a forbidden token; no route, button or command text reaches the
        frontend; XSS-01's raw-interpolation list changes for product_ui.js only, or not at all
  F04R  every changed older test function is declared and every bookkeeping edit is undone exactly
  F05R  the ledger changes only its docstring sentence and paragraph, its declarations, its two helpers, the re-hashed entries of the registered
        files the recovery edits and the registrations NEW_LEDGER_REC declares (each unchanged from BASE_68 to BASE_REC)
  F06R  design 7a: the panel's, J-04's and the order gate's freeze tests read their side at BASE_REC for exactly the files the recovery edits;
        BASE_REC is an ancestor of HEAD with the gate's reviewed tree; the gate's set plus the recovery's set is the whole change since BASE_GATE;
        XSS-01's, J-04's and the gate's live security pins are not edited
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

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_REC = {"execution/recovery.py", "tests/test_bot_position_recovery.py", "tests/test_recovery_freeze.py", "tests/frontend/recovery_activity.test.cjs"}
PROD_REC = {"execution/service.py", "execution/__init__.py", "forward_capture/capture.py", "forward_validation/service.py", "forward_validation/reports.py", "frontend/product_ui.js"}
DOCS_REC = {"README.md"}
REVIEWED_GATE_TREE = "f42fed95d8ace60f5d67a214ce55034637bdee01"                             # main after PR #60: the order gate merged with its approved tree
UNTOUCHED_REC = ("execution/broker.py", "execution/engine.py", "execution/store.py", "execution/models.py", "execution/scheduler.py", "execution/simulated.py",
                 "execution/exposure_gate.py", "execution/account_positions.py", "execution/activity.py", "database/execution_migrations.py",
                 "database/account_refresh_migrations.py", "database/alpaca_order_migrations.py", "database/forward_validation_migrations.py",
                 "database/execution_safety_migrations.py", "config.py", "api/server.py", "api/local_gate.py", "api/routes/execution.py", "api/routes/forward_validation.py",
                 "api/routes/execution_safety.py", "api/routes/alpaca_paper_orders.py", "paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_store.py",
                 "paper/alpaca_order_reads.py", "paper/alpaca_order_writer.py", "safety/engine.py", "safety/service.py", "safety/store.py", "forward_validation/__init__.py",
                 "forward_validation/store.py", "forward_validation/pnl_view.py", "forward_validation/run_admin.py", "forward_validation/attention_signature.py",
                 "account_refresh/service.py", "account_refresh/store.py", "strategy_registry/downside_recovery.py", "frontend/app.js", "frontend/risk_controls.js",
                 "frontend/stop_priority.js", "frontend/trader_render.js", "frontend/paper_bot.js", "frontend/forward_validation.js", "frontend/index.html",
                 "frontend/forward_validation.html", "browser_tests/app_server.py", "tests/conftest.py", "tests/test_stop_order_gate.py", "tests/test_stop_latch_read.py",
                 "tests/frontend/stop_priority.test.cjs", "tests/frontend/stop_gate_copy.test.cjs", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py",
                 "tests/frontend/xss01_app_js.test.cjs")
FORBIDDEN_PREFIX_REC = ("database/", "api/", "safety/", "paper/", "account_refresh/", "cash_flow/", "autorun/", "setups/", "proposals/", "rotation/", "scripts/", "data/",
                        "agents/", "unified_portfolio/", "market_intel/", "research_data/", "swing_eval/", "swing_pullback/", "strategy_registry/", "strategy_validation/",
                        "backtest/", "trader_view/", "fit/")
CI_ADDED = ['        "test_bot_position_recovery.py",', '        "test_recovery_freeze.py",']
LINE_OPS_REC = {                                                                           # file -> (main's removed lines, the recovery's added lines), in order
    'execution/__init__.py': (
        ['VERSION = "execution_v4"                                     # Emergency Stop order gate (C-7): the latch is read in the transaction that records an entry\'s attempt; a new 6.10A cohort', '             "DISLOCATION_NORMALIZED": "The dislocation normalized"}'],
        ['VERSION = "execution_v5"                                     # bot-position recovery (design 3.5, section 6): enable_auto and run_cycle reconcile while unhealthy with nothing open; a new 6.10A cohort', '             "DISLOCATION_NORMALIZED": "The dislocation normalized",', '             "MANUAL_EXTERNAL": "Closed outside the app"}               # bot-position recovery: a close recorded by execution/recovery.py; NOT in EXIT_REASONS (not a strategy exit)']),
    'execution/service.py': (
        ['            elif self.needs_broker():', '            if not self.needs_broker():'],
        ['            elif self.needs_broker() or not self.recon_healthy:                # bot-position recovery (design 3.5, section 6): also with nothing open', '            if not self.needs_broker() and self.recon_healthy:                 # bot-position recovery (design 3.5, section 6): an unhealthy executor reconciles']),
    'forward_capture/capture.py': (
        ['                   "WHERE d.position_id = p.position_id AND d.decision = \'EXITED\') ORDER BY closed_at LIMIT 200", (epoch,)):'],
        ['                   "WHERE d.position_id = p.position_id AND d.decision = \'EXITED\') AND NOT EXISTS (SELECT 1 FROM position_events e WHERE e.position_id = p.position_id "', '                   "AND e.kind = \'MANUAL_RECOVERY\') ORDER BY closed_at LIMIT 200", (epoch,)):   # bot-position recovery: never EXITED evidence; excluded in the query (LIMIT 200)']),
    'forward_validation/service.py': (
        ['        """The read sites\' daily summary: the stored one when its watermark is current, otherwise a fresh bracketed one (not persisted). Never writes."""', '                    return stored', '        return self.bracketed_summary(trading_date)', '        return [r if wrows is not None and r.get("pnl_watermark") is not None and r["pnl_watermark"] == self.pnl_watermark(r["trading_date"], wrows)', '                else self.bracketed_summary(r["trading_date"]) for r in rows]'],
        ['        iv = self.interventions((session_date(now) or now.date()).isoformat())       # bot-position recovery (decision R-3): the live note, inline and', '        if iv is None:                                                               # never critical (no ATTENTION_RAISED, no early stop), as MANUAL_ORDERS_UNRESOLVED', '            out.append({"code": "MANUAL_RECOVERY_UNREADABLE", "text": "manual recovery records could not be read", "critical": False, "detail": None, "count": 1})', '        elif iv:', '            out.append({"code": "MANUAL_RECOVERY_IN_SESSION", "text": "a bot position record was corrected with the manual recovery command in this session (an intervention)",', '                        "critical": False, "detail": [f"{i[\'symbol\']} at {i[\'at\']} (by {i[\'actor\']})" for i in iv], "count": len(iv)})', '        """The read sites\' daily summary: the stored one when its watermark is current, otherwise a fresh bracketed one (not persisted). Never writes.', '        Bot-position recovery (decision R-3): either way it carries `interventions`, read now, never from the stored JSON."""', '                    return {**stored, "interventions": self.interventions(trading_date)}', '        return {**self.bracketed_summary(trading_date), "interventions": self.interventions(trading_date)}', '        out = [r if wrows is not None and r.get("pnl_watermark") is not None and r["pnl_watermark"] == self.pnl_watermark(r["trading_date"], wrows)', '               else self.bracketed_summary(r["trading_date"]) for r in rows]', '        iv = self._interventions_by_date([s["trading_date"] for s in out])        # bot-position recovery (decision R-3): one read for the whole range', '        return [{**s, "interventions": iv[s["trading_date"]] if iv is not None else None} for s in out]', '', '    # ---------------------------------------------------------------- bot-position recovery: the intervention note (decision R-3) --------', '    def interventions(self, trading_date: str) -> Optional[List[dict]]:', '        """BOT_POSITION_RECOVERY_DESIGN.md revision 3.5, section 5 step 8 (decision R-3; reviews SD-MED-1 and R-LOW-1): every MANUAL_RECOVERY', "        position event of the date's window, as [{code, at, position_id, symbol, actor}]. The window runs from the end of the New York day of", "        the latest EARLIER date with a session row to the end of the date's own New York day (both through ny_window), so a weekend or holiday", "        recovery belongs to the next session; with no earlier session it is the date's own New York day. Strict reads (query_strict): any", '        read error is None, never [], so a read error is never shown as "no recovery". Reads only."""', '        out = self._interventions_by_date([trading_date])', '        return None if out is None else out[trading_date]', '', '    def _interventions_by_date(self, dates: List[str]) -> Optional[Dict[str, List[dict]]]:', '        """interventions() of several dates from ONE read of the session dates and ONE read of the events; None on any error."""', '        if not dates:', '            return {}', '        try:', '            known = sorted(r["trading_date"] for r in self.store.query_strict("SELECT trading_date FROM forward_validation_sessions"))', '            bounds = {}', '            for d in dates:', '                earlier = [s for s in known if s < d]', '                bounds[d] = (ny_window(earlier[-1])[1] if earlier else ny_window(d)[0], ny_window(d)[1])', '            rows = self.store.query_strict("SELECT e.at, e.position_id, e.detail_json, p.symbol FROM position_events e JOIN paper_positions p ON p.position_id = e.position_id "', '                                           "WHERE e.kind = \'MANUAL_RECOVERY\' AND e.at >= ? AND e.at < ? ORDER BY e.at, e.rowid",', '                                           (min(b[0] for b in bounds.values()), max(b[1] for b in bounds.values())))', '            items = []', '            for r in rows:', '                detail = json.loads(r["detail_json"] or "{}")', '                if not isinstance(detail, dict):', '                    raise ValueError("a MANUAL_RECOVERY event\'s detail is not an object")', '                items.append({"code": "MANUAL_RECOVERY_IN_SESSION", "at": r["at"], "position_id": r["position_id"], "symbol": r["symbol"], "actor": detail.get("actor")})', '            return {d: [i for i in items if lo <= i["at"] < hi] for d, (lo, hi) in bounds.items()}', '        except Exception as exc:  # noqa: BLE001 - unreadable is None, never "no recovery"', '            log.warning("forward validation: manual recovery records unreadable (%s)", type(exc).__name__)', '            return None']),
    'forward_validation/reports.py': (
        [],
        ['    if "interventions" in s and s["interventions"] is None:                            # bot-position recovery (decision R-3): a read error is never "no recovery"', '        out.append("  interventions: unreadable")', '    for i in s.get("interventions") or []:                                           # an absent key (daily_summary(), store.daily()) or [] prints nothing', '        out.append(f"  intervention: {i.get(\'code\')} {i.get(\'symbol\')} at {i.get(\'at\')} (by {i.get(\'actor\')})")']),
    'frontend/product_ui.js': (
        [],
        ['      else if (e.kind === "MANUAL_RECOVERY") push(e.at, `${s} · Position record corrected by ${(e.detail && e.detail.actor) || "the operator"}: ${(e.detail && e.detail.reason) || "no reason recorded"}`, "bad", "attention", s);   // bot-position recovery (decision R-3): the event\'s detail; activityHtml escapes it']),
    'README.md': (
        ["| F6 | You closed or reduced a bot position on Alpaca's website | Expect **Needs attention**. Any remainder has no automatic exit. New entries and Paper Auto stay refused, even after a Clear, until a recovery command ships. Editing the database by hand is not a route | Nothing in the app resolves it yet |"],
        ["| F6 | You closed or reduced a bot position on Alpaca's website | Expect **Needs attention**. Any remainder has no automatic exit. New entries and Paper Auto stay refused, even after a Clear, until you run the recovery command (F6 in detail, below). Editing the database by hand is not a route | The command corrects the bot's record from the stored account data. It places, cancels and changes no order |", '', '**F6 in detail: the recovery command.** Set Monitor only, keep the server running, wait for the next account refresh, then run from the app folder, with the', "server's env file: `python -m execution.recovery preview --position-id <id>`, then `python -m execution.recovery apply --position-id <id> --evidence-hash <hash>", "--approved-by <your name> --reason <text>`, adding `--i-confirmed-flat-on-the-website` for a full close once you have looked at Alpaca's positions page. It works", "from the database only and never contacts Alpaca. It reads this checkout's `.env` only: `preview` prints the database it reads and the settings it applies", "(the refresh intervals and the exit-attempt limit); if the server runs with an overrides file and they differ, set the server's values in the shell first,", 'for example `$env:EXECUTION_MAX_EXIT_CYCLES = "2"`. It refuses unless the position needs attention because the broker is flat, the quantities differ or its exit', 'attempts are used up; Paper Auto is off; no bot order and no manual order is open or unresolved in the symbol, and no manual order of the symbol changed since', "the last account refresh began; the account data refreshed without a failure, after the position's latest finding, at most two refresh intervals ago (600 s for", 'a partial), and lists no open order in the symbol, an order placed on the website included; the broker shows fewer whole shares than the record, on a long;', "the latest finding agrees; the bot's loop ran in the last 600 s; and, for a partial, an exit attempt is left. **S1:** do not trade the symbol on Alpaca's", 'website between `preview` and the first cycle after `apply`. **S5:** watch that first cycle, without restarting the server before it, and read the Risk', 'card: a blocking "broker position without an internal position" finding means shares arrived, so sell them on the website. **S6:** prefer a time outside', 'regular hours; a resting order placed on the website still fills at the next open, and the command sees it only if the account data lists it, so S1 covers', "the rest. A recovery on a weekend or holiday shows in the next session's report. **The known cost:** a fill that lands between the newest account read and", '`apply`, from an order placed against S1, leaves shares the app has no record of and no automatic exit. The first cycle after `apply` reports them (S5); a fill', 'after that cycle shows only as "Not managed · needs a look" among the account\'s positions until Paper Auto is armed. Sell such shares on the website. A', 'partial gives the remainder its exit rules back, so a SELL of the remainder can follow in the next cycle if any exit rule is met; outside regular hours it', 'can come at the first cycle of the next session, and neither Monitor only nor Emergency Stop prevents it. A recovered trade stays unverified and is no', 'strategy evidence, though the funnel still counts the close. The command changes no Emergency Stop, Pause or daily-loss halt. Paper Auto stays off until', 'you enable it, or until the scheduled arm does if `PAPER_AUTO_RUN_POLICY=SCHEDULED_ARM` and `PAPER_AUTO_SCHEDULED_ARM_ENABLED=true` are both set (both', 'are off by default).']),
    '.github/ci/pytest_groups.json': (
        [],
        ['        "test_recovery_freeze.py",', '        "test_bot_position_recovery.py",']),
}
DEFS_REC = {
    "execution/__init__.py": ({"=VERSION", "=EXIT_TEXT"}, set()),
    "execution/service.py": ({"ExecutionService"}, set()),
    "forward_validation/service.py": ({"ForwardValidationService"}, set()),
    "forward_validation/reports.py": ({"daily_report"}, set()),
    "forward_capture/capture.py": ({"ForwardCapture"}, set()),
}
METHODS_REC = {                                                                             # class -> (methods that change, methods that appear)
    ("execution/service.py", "ExecutionService"): ({"enable_auto", "run_cycle"}, set()),
    ("forward_validation/service.py", "ForwardValidationService"): ({"attention", "fresh_daily", "fresh_dailies"}, {"interventions", "_interventions_by_date"}),
    ("forward_capture/capture.py", "ForwardCapture"): ({"_lifecycle"}, set()),
}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    run = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {commit} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def at_base(rel):
    return at(FL.BASE_REC, rel)


# Market Explorer A0 (design 7a, as the recovery did for the order gate): the files the explorer stage edits are read at BASE_EXP, the recovery's side
# as merged (PR #62), a commit that cannot change; the recovery's change set is the fixed diff to BASE_EXP and its ledger checks read the ledger
# there. tests/test_explorer_freeze.py F06E pins that this list is exactly the explorer stage's edited files.
EXP_MOVED = ('.github/ci/pytest_groups.json', 'tests/freeze_ledger.py', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py',
             'tests/test_decision_flow_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py',
             'tests/test_evidence_refresh_542.py', 'tests/test_ex06_freeze.py', 'tests/test_forward_paper_610a.py', 'tests/test_market_intel_70.py',
             'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py',
             'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py',
             'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_sip_eval_78b1.py',
             'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_unified_exposure_gate_691.py',
             'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_prb.py',
             'tests/test_wave2_prc.py')


def at_exp(rel):
    return at(FL.BASE_EXP, rel)


def ledger_exp():
    """The ledger as the explorer stage found it (BASE_EXP): the hashes the recovery's bookkeeping is checked against."""
    t = at_exp("tests/freeze_ledger.py")
    led = t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]
    return ast.literal_eval(led[len("LEDGER = "):])


def now_text(rel):
    if rel in EXP_MOVED:
        return at_exp(rel)
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    # Market Explorer A0 (design 7a): the recovery's side is the fixed diff from `base` to BASE_EXP; the working tree's change set belongs to tests/test_explorer_freeze.py
    return set(_git("diff", "--name-only", base, FL.BASE_EXP).split())


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
    """Every module-level statement defs() does not hold (imports, expressions, if / try blocks, attribute, subscript or tuple assignments), in order."""
    return [ast.dump(n) for n in ast.parse(text).body if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
        isinstance(n, (ast.Assign, ast.AnnAssign)) and all(isinstance(t, ast.Name) for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))))]


def test_F01R_the_change_set_is_exactly_the_declared_files_and_the_broker_store_migrations_api_and_safety_are_byte_identical():
    assert FL.BASE_REC == "2d3cf8fb7dc03daedc7792facc8f9d7630267d2f" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_REC}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_REC)
    assert changed == FL.NEW_REC | FL.EDITED_REC, {"unexpected": sorted(changed - FL.NEW_REC - FL.EDITED_REC), "missing": sorted((FL.NEW_REC | FL.EDITED_REC) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_REC).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_REC == NEW_FILES_REC and {p for p in changed if p in in_base} == FL.EDITED_REC
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_REC, FL.BASE_EXP).split()
    production = {p for p in FL.EDITED_REC if not p.startswith(("tests/", "browser_tests/", ".github/"))}
    assert production == PROD_REC | DOCS_REC and set(FL.SEMANTIC_REC) == PROD_REC | DOCS_REC
    assert all(len(v) > 60 for v in FL.SEMANTIC_REC.values())
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_REC)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_REC))
    assert {p for p in changed if p.startswith("execution/")} == {"execution/service.py", "execution/__init__.py", "execution/recovery.py"}
    for rel in UNTOUCHED_REC:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_REC, FL.BASE_EXP, "--", "api", "database", "safety", "paper", "account_refresh", "config.py", "execution/broker.py",
                           "execution/store.py", "execution/engine.py"], cwd=ROOT).returncode == 0
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g not in ("core", "execution")} == {g: v for g, v in base_groups.items() if g not in ("core", "execution")}
    for g, after, new in (("core", "test_gate_freeze.py", "test_recovery_freeze.py"), ("execution", "test_execution_60.py", "test_bot_position_recovery.py")):
        i = base_groups[g]["files"].index(after) + 1
        assert groups[g] == {**base_groups[g], "files": base_groups[g]["files"][:i] + [new] + base_groups[g]["files"][i:]}, g
    assert ledger_exp()[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))   # Market Explorer A0: the manifest and its hash, both at BASE_EXP


def test_F02R_the_declared_lines_and_definitions_change_and_nothing_else():
    for rel, (removed_want, added_want) in LINE_OPS_REC.items():
        base, now, removed, added = line_ops(rel)
        assert (removed, added) == (removed_want, added_want), rel
        assert len(now) == len(base) + len(added) - len(removed), rel
        if rel in FL.SEMANTIC_REC:
            assert FL.pre_rec(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    for rel, (may_change, may_add) in DEFS_REC.items():
        b, n = module_defs(at_base(rel)), module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        assert {k for k in b if b[k] != n[k]} == may_change, (rel, sorted({k for k in b if b[k] != n[k]}))
        assert set(n) - set(b) == may_add, (rel, sorted(set(n) - set(b)))
        assert unnamed(now_text(rel)) == unnamed(at_base(rel)), rel                            # imports, expressions and blocks unchanged too
    for (rel, cls), (may_change, may_add) in METHODS_REC.items():
        b, n = class_defs(at_base(rel), cls), class_defs(now_text(rel), cls)
        assert set(b) <= set(n) and {k for k in b if b[k] != n[k]} == may_change and set(n) - set(b) == may_add, (rel, cls, sorted({k for k in b if b[k] != n.get(k)}), sorted(set(n) - set(b)))
    x = now_text("execution/__init__.py")
    assert 'EXIT_REASONS = ("HARD_INVALIDATION", "EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "TIME_EXIT", "DISLOCATION_NORMALIZED")' in x   # not a strategy exit
    fv = now_text("forward_validation/service.py")
    assert fv.count("query_strict(") == at_base("forward_validation/service.py").count("query_strict(") + 2 and "MANUAL_RECOVERY" not in now_text("forward_validation/__init__.py")


def test_F03R_the_scripts_keep_their_routes_and_add_no_forbidden_token_and_no_recovery_control():
    routes = lambda s: sorted(set(re.findall(r"[\"`](/api/[a-z0-9_\-/]+)", s)))  # noqa: E731
    rel = "frontend/product_ui.js"
    now, base = now_text(rel), at_base(rel)
    assert routes(now) == routes(base) and now.count("fetch(") == base.count("fetch(") and now.count('method: "POST"') == base.count('method: "POST"')
    added = "\n".join(line_ops(rel)[3])
    assert added and not TPF.SCRIPT_BAN_PANEL.search(added), TPF.SCRIPT_BAN_PANEL.findall(added)[:5]
    assert "execution.recovery" not in now and "evidence-hash" not in now and "recovery/" not in now      # R-2: no route, no button, no command in the page
    raw, base_raw = json.loads(now_text("tests/frontend/xss01_raw_interpolations.json")), json.loads(at_base("tests/frontend/xss01_raw_interpolations.json"))
    assert {m for m in set(raw) | set(base_raw) if raw.get(m) != base_raw.get(m)} <= {"product_ui.js"}
    for rel in ("api/server.py", "api/routes/execution.py"):
        assert "recovery" not in now_text(rel).lower(), rel


def test_F04R_every_changed_older_test_function_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_REC if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = module_defs(at_base(rel)), module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    actual |= {f"{rel}::*" for rel in FL.EDITED_REC if rel.startswith("tests/frontend/") and rel.endswith(".cjs")}
    declared = set(FL.SEMANTIC_TESTS_REC)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^REC-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_REC.values())
    edited_tests = {p for p in FL.EDITED_REC if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_REC) == edited_tests
    for rel in sorted(FL.UNDO_REC):
        assert FL.undo_rec(rel, now_text(rel)) == at_base(rel), rel                            # now -> 2d3cf8fb, byte for byte
        for old, new in FL.UNDO_REC[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:80])
    for rel in FL.SEMANTIC_REC:
        assert FL.pre_rec(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    FL.UNDO_REC["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_rec("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_rec("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_REC["tests/_probe.py"]


def test_F05R_the_ledger_changes_only_its_declarations_its_two_helpers_and_the_rehashed_entries():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    for a in ("\nLEDGER = {\n", "\nBASE_GATE = ", "\nBASE_REC = ", "\n\ndef unchanged(", "\n\ndef pre_gate(", "\n\ndef pre_rec(", "\n\ndef undo_rec(",
              "\n\nBot-position recovery (", " + NEW_XSS01 + NEW_PANEL + NEW_J04 + NEW_GATE + NEW_REC plus this ledger."):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_REC = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_REC", "NEW_REC", "EDITED_REC", "SEMANTIC_REC", "INSTRUMENTATION_REC", "NEW_LEDGER_REC", "UNDO_REC", "SEMANTIC_TESTS_REC", "VERSION_PINS_W2A['execution_v5']"]
    v4, v5 = FL.VERSION_PINS_W2A["execution_v4"], FL.VERSION_PINS_W2A["execution_v5"]
    assert set(v5) == set(v4) == set(FL.VERSION_BLOCKS_W2A) and {k for k in v4 if v4[k] != v5[k]} == {"execution/service.py::ExecutionService.enable_auto"}   # run_cycle is not a listed block
    assert FL.INSTRUMENTATION_REC == {}
    assert set(ledger) - set(main_ledger) == FL.NEW_LEDGER_REC and set(main_ledger) <= set(ledger)
    for rel in sorted(FL.NEW_LEDGER_REC):
        assert FL.base_text(rel) == at_base(rel) != now_text(rel), rel                              # unchanged from BASE_68 to main and edited here
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == {k for k in main_ledger if k in FL.EDITED_REC}, sorted(rehashed ^ {k for k in main_ledger if k in FL.EDITED_REC})
    for rel in rehashed | FL.NEW_LEDGER_REC:
        assert ledger[rel] == FL.sha(now_text(rel)) == ledger_exp()[rel], rel                     # Market Explorer A0: the recovery's hashes against the recovery's texts, both at BASE_EXP
    start = fl.index("\n\nBot-position recovery (") + 1
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_REC = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_rec("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_XSS01 + NEW_PANEL + NEW_J04 + NEW_GATE + NEW_REC plus this ledger.",
                                                                     " + NEW_XSS01 + NEW_PANEL + NEW_J04 + NEW_GATE plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, two helpers and the ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06R_design_7a_the_panel_j04_and_gate_evidence_at_fixed_commits_and_an_unbroken_chain():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_REC, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_GATE, FL.BASE_REC], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_REC}^{{tree}}").strip() == REVIEWED_GATE_TREE
    gate = set(_git("diff", "--name-only", FL.BASE_GATE, FL.BASE_REC).split())
    assert gate == FL.NEW_GATE | FL.EDITED_GATE, sorted(gate ^ (FL.NEW_GATE | FL.EDITED_GATE))   # the gate as merged (PR #60, its approved tree)
    rec = changed_files(FL.BASE_REC)
    whole = changed_files(FL.BASE_GATE)
    in_gate_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_GATE).split())
    assert whole == (gate | rec) - {p for p in gate & rec if p in in_gate_base and now_text(p) == at(FL.BASE_GATE, p)}, sorted(whole ^ (gate | rec))
    pf, jf, gf = now_text("tests/test_panel_freeze.py"), now_text("tests/test_j04_freeze.py"), now_text("tests/test_gate_freeze.py")
    for src in (pf, jf, gf):
        assert "def at_rec(rel):\n    return at(FL.BASE_REC, rel)\n" in src and "    if rel in REC_MOVED:\n        return at_rec(rel)\n" in src
    assert "    if rel in GATE_MOVED:\n        return at_gate(rel)\n    if rel in REC_MOVED:\n        return at_rec(rel)\n" in pf and \
           "    if rel in GATE_MOVED:\n        return at_gate(rel)\n    if rel in REC_MOVED:\n        return at_rec(rel)\n" in jf        # the gate's files first, at BASE_GATE
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_REC).split())\n' in gf and "def ledger_rec():" in gf
    for mod, own in ((TPF, "tests/test_panel_freeze.py"), (TJ, "tests/test_j04_freeze.py"), (TG, "tests/test_gate_freeze.py")):
        assert set(mod.REC_MOVED) == FL.EDITED_REC - {own} and len(mod.REC_MOVED) == len(set(mod.REC_MOVED)), own
    assert set(TPF.GATE_MOVED) == FL.EDITED_GATE - {"tests/test_panel_freeze.py"} and set(TJ.GATE_MOVED) == FL.EDITED_GATE - {"tests/test_j04_freeze.py"}   # the gate's own move is untouched
    for rel in ("tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs", "tests/test_stop_latch_read.py",
                "tests/frontend/stop_priority.test.cjs", "tests/test_stop_order_gate.py", "tests/frontend/stop_gate_copy.test.cjs"):
        assert now_text(rel) == at_base(rel), rel                                               # XSS-01's, J-04's and the gate's live security pins are not edited
