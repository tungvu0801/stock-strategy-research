"""
tests/freeze_ledger.py - the Stage 6.8 post-freeze ledger (account position visibility + Bot Activity; DESIGN_68 section 6).

The earlier freeze tests pin execution, Trader View, route and UI files to their own base commits. Stage 6.8 changes some of those files on
purpose. Instead of loosening any freeze test, every such file is registered here with the SHA-256 of the exact text Stage 6.8 leaves, and
the freeze tests read it through `view()`: a registered file whose text matches its hash is seen exactly as it was on main (BASE_68), where
every freeze test passed. Any other change - or any later edit of a registered file without updating its hash - still fails, and
tests/test_bot_activity_68.py::test_F01 proves that the change set since main is exactly NEW_68 + NEW_69 + NEW_691 + NEW_610A + NEW_610C + NEW_541 + NEW_611FIX + NEW_OPS plus this ledger.
Stage 6.9 extends the same ledger (its edits of registered files are new hashes; its new files are NEW_69) - BASE_68 stays the view base.

Stage 6.9.1 (the unified external exposure gate) is the first stage that changes EXECUTION SEMANTICS on purpose: a new entry control,
UNIFIED_EXTERNAL_EXPOSURE, can block a new Alpaca PAPER entry when UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true. Its edited files are pinned here
byte-for-byte like every other entry, so an older freeze test still checks exactly what ITS stage froze - but a registration here is NOT a claim
that execution is unchanged. SEMANTIC_691 names every file whose change alters what the executor does, and
tests/test_unified_exposure_gate_691.py (F tests) pins that delta explicitly against main BASE_691: the files differ from pre-6.9.1 main, every
removed line is listed, every added line is a Stage 6.9.1 line, and the entry controls are entry_controls_v1 + exactly one control
(entry_controls_v2). The Stage 6.1 frozen strategy-controls hash stays b5d4a56e... (its values did not move); the entry controls are versioned
apart from it (execution/exposure_gate.py: VERSION, contract_hash).

Stage 6.10A (forward paper auto-run + evidence capture) changes WHEN Paper Auto may enter - the ARMING and RESTART semantics - on purpose,
and not HOW an entry or exit is decided. SEMANTIC_610A names every file of that change: a process start never keeps Paper Auto armed
(MANUAL_ARM, superseding DESIGN_60 section 1's 'persisted mode restored after restart'), deterministic conditions disarm it (new entries stop;
monitoring and exits continue) and an opt-in, default-off SCHEDULED_ARM may arm it once per regular session. INSTRUMENTATION_610A names the files
that only record evidence: the executor hands decision-time records to an in-memory sink through its existing failure-swallowing _record. The
order-decision logic is pinned unchanged by tests/test_forward_paper_610a.py (F tests): execution/engine.py is byte-identical to BASE_610A,
every added executor line is a _record call or inside the new _mark, and the frozen and entry-contract hashes are the Stage 6.9.1 values.

Stage 6.10C (agent decision flow + an inactive dynamic universe) is observability only: a read-only projection of persisted rows (decision_flow/,
GET /api/decision-flow*), the TRADE / ACTIVITY panels that show it, and an inactive dynamic_universe/ that no live path imports. It changes no
execution, strategy, risk, Paper Auto or startup behaviour: SEMANTIC_610C and INSTRUMENTATION_610C are empty, execution/* is byte-identical to
BASE_610C, and tests/test_decision_flow_610c.py (F tests) pins exactly that.

Stage 5.4.1 (evidence gate reliability) corrects one reader: the setup's evidence check now resolves the same Stage 5.4 packet as PPE Confidence
(the newest COMPLETED packet created at or before now), so a newer FAILED build can no longer hide a valid packet and block READY with
READY_BLOCKED_NO_EVIDENCE. SEMANTIC_541 names that one file; no threshold, PPE rule, setup-engine rule or execution file changes, and
tests/test_evidence_gate_541.py pins the fix and its scope.

Stage 6.1.1 CSS fix (chart empty state) scopes the four chart empty-state overlay rules of frontend/product_ui.css to
`#pui-chart-empty .pui-chart-empty`: the bare `.pui-chart-empty` selector also matched the body state class of the same name (set while
the chart has no candles, e.g. market closed) and centred / narrowed the whole page. CSS only: no script, backend, threshold or
execution change; tests/test_chart_empty_state_611fix.py pins the fix and its scope.

Ops runtime reliability (after Stage 5.4.1) is operations only. The weekday start wrapper comes under version control
(scripts/start_market_morning.ps1: every live path a parameter defaulting to today's value, a launch line written before any other
work, a trap that always logs a RESULT), a task installer adds at-logon and on-resume triggers with a restart policy
(scripts/install_market_morning_task.ps1; -PrintOnly registers nothing), the runner logs a detected suspend/resume gap and the signal
it shuts down on (log only, no behaviour change), and the check script shows the scheduled tasks' last results, whether Task Scheduler
history is on and the last resume. No execution, strategy, risk, Paper Auto, startup-demotion or broker behaviour changes: SEMANTIC_OPS
is empty, scripts/start_stock_agent.ps1 / stop / common are byte-identical, and tests/test_runtime_reliability_ops.py pins the change set.
"""
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_68 = "b2a20ea0e5418f92f2c943dcc54a7268260f1a9b"                                     # main: Stage 6.7 merged (PR #30)
NEW_68 = {"execution/account_positions.py", "execution/activity.py", "tests/test_bot_activity_68.py", "tests/frontend/bot_activity.test.cjs", "tests/freeze_ledger.py",
          "DESIGN_68_ACCOUNT_POSITIONS_BOT_ACTIVITY.md"}
NEW_69 = {"unified_portfolio/__init__.py", "unified_portfolio/fixture.py", "unified_portfolio/robinhood.py", "unified_portfolio/state.py",
          "api/routes/unified_portfolio.py", "tests/test_robinhood_portfolio_69.py", "tests/frontend/unified_portfolio.test.cjs",
          "DESIGN_69_ROBINHOOD_UNIFIED_PORTFOLIO.md"}                                    # Stage 6.9 (Robinhood read-only portfolio): its new files
BASE_691 = "98c59e29e8de800226a47b1994038e902cfde0e2"                                    # main: Stage 6.9 merged (PR #32)
NEW_691 = {"execution/exposure_gate.py", "tests/test_unified_exposure_gate_691.py", "tests/frontend/exposure_gate.test.cjs",
           "DESIGN_691_UNIFIED_EXPOSURE_GATE.md"}                                       # Stage 6.9.1 (unified external exposure gate): its new files
EDITED_691 = {".github/ci/pytest_groups.json", "api/routes/execution.py", "api/routes/unified_portfolio.py", "browser_tests/flows.py", "config.py",
              "execution/activity.py", "execution/engine.py", "execution/service.py", "forward_validation/reports.py", "forward_validation/service.py",
              "frontend/product_ui.css", "frontend/product_ui.js", "trader_view/stream.py", "unified_portfolio/__init__.py", "unified_portfolio/state.py", "tests/freeze_ledger.py",
              "tests/test_bot_activity_68.py", "tests/test_robinhood_portfolio_69.py", "tests/test_strategy_validation_62.py",
              "tests/frontend/unified_portfolio.test.cjs"}                               # Stage 6.9.1: the files of main it edits (the last one: the display precision fix)
SEMANTIC_691 = {                                                                         # Stage 6.9.1: the INTENTIONAL execution-semantics change, file by file
    "execution/exposure_gate.py": "new: the UNIFIED_EXTERNAL_EXPOSURE rule (duplicate symbol held in another account; projected symbol concentration; stale / unknown policy)",
    "execution/engine.py": "entry_controls() appends UNIFIED_EXTERNAL_EXPOSURE when the gate is enabled: it can BLOCK a new Alpaca PAPER entry (entry_controls_v2)",
    "execution/service.py": "_scan_entries() reads the unified portfolio once per Paper Auto scan when the gate is on, gives it to the entry controls, "
                            "records the gate in the cycle summary and the entry-controls version on every entry intent",
    "trader_view/stream.py": "TerminalStream.unified_portfolio(): the executor's read-only gate input",
    "config.py": "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED (default false), UNIFIED_BLOCK_DUPLICATE_SYMBOL, UNIFIED_MAX_SYMBOL_EXPOSURE_PCT, UNIFIED_STALE_SOURCE_POLICY",
}
BASE_610A = "27926f627c5cb83a6e1b3b6a9b646ce455cfbe52"                                   # main: Stage 6.9.1 merged (PR #33)
NEW_610A = {"autorun/__init__.py", "autorun/calendar.py", "autorun/facts.py", "autorun/policy.py", "autorun/scheduler.py", "autorun/service.py", "autorun/session.py",
            "autorun/store.py", "autorun/view.py", "forward_capture/__init__.py", "forward_capture/capture.py", "forward_capture/cohort.py", "forward_capture/exports.py",
            "forward_capture/health.py", "forward_capture/project.py", "forward_capture/store.py", "forward_capture/view.py", "database/autorun_migrations.py",
            "database/forward_capture_migrations.py", "tests/test_forward_paper_610a.py", "tests/frontend/forward_session.test.cjs",
            "DESIGN_610A_FORWARD_PAPER_AUTORUN.md"}                                      # Stage 6.10A (forward paper auto-run + evidence capture): its new files
EDITED_610A = {".github/ci/pytest_groups.json", "api/routes/forward_validation.py", "api/server.py", "browser_tests/flows.py", "config.py", "execution/activity.py",
               "execution/service.py", "forward_validation/reports.py", "forward_validation/service.py", "frontend/product_ui.js", "scripts/install_stock_agent_task.ps1",
               "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_robinhood_portfolio_69.py", "tests/test_strategy_validation_62.py",
               "tests/test_unified_exposure_gate_691.py", "tests/test_windows_runner_60.py"}   # Stage 6.10A: the files of main it edits
SEMANTIC_610A = {                                                                        # Stage 6.10A: the INTENTIONAL arming / restart change (WHEN Paper Auto may enter)
    "api/server.py": "the lifespan takes the single-instance lease, then demotes a persisted PAPER_AUTO to MONITOR_ONLY BEFORE the executor starts; refused = "
                     "neither the executor nor the observer starts (fail closed)",
    "autorun/service.py": "new: the startup demotion, the automatic disarms (Paper Auto -> Monitor only through disable_auto) and the opt-in scheduled arm "
                          "(enable_auto at most once per regular session, only with PAPER_AUTO_SCHEDULED_ARM_ENABLED=true)",
    "autorun/policy.py": "new: the deterministic preflight, automatic-disarm and scheduled-arm conditions (pure functions over gathered facts)",
    "autorun/scheduler.py": "new: the supervisor thread that applies autorun/service.py every 30 s in the session / 60 s outside it and keeps the instance lease",
    "config.py": "PAPER_AUTO_RUN_POLICY (default MANUAL_ARM), PAPER_AUTO_SCHEDULED_ARM_ENABLED (default false), PAPER_AUTO_SCHEDULED_ARM_DELAY_MINUTES, "
                 "PAPER_AUTO_DISARM_AT_SESSION_END (default true), AUTORUN_SUPERVISOR_ENABLED",
}
INSTRUMENTATION_610A = {                                                                 # Stage 6.10A: evidence recording only (no decision reads it)
    "execution/service.py": "six self._record(...) markers hand decision-time records (allowlisted) to the forward-capture sink, plus the new _mark",
    "forward_validation/service.py": "the capture tick after the observer's own try/except (its own guard) and read-only evidence views",
}
BASE_610C = "fed5b3cd3d602eccd71e473046ce58ad2f7819f4"                                   # main: Stage 6.10A merged (PR #34)
NEW_610C = {'tools/evaluator_capacity.py', 'api/routes/decision_flow.py', 'decision_flow/__init__.py', 'tests/frontend/decision_flow.test.cjs', 'tests/test_dynamic_universe_610c.py', 'tools/__init__.py', 'decision_flow/conditions.py', 'dynamic_universe/core.py', 'dynamic_universe/__init__.py', 'DESIGN_610C_DECISION_FLOW.md', 'dynamic_universe/plan.py', 'dynamic_universe/signals.py', 'tests/test_decision_flow_610c.py', 'decision_flow/view.py', 'decision_flow/store.py'}
EDITED_610C = {'tests/freeze_ledger.py', 'tests/frontend/product_flow.test.cjs', 'tests/test_product_ui_611.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_product_flow_612.py', 'tests/test_strategy_validation_62.py', 'tests/frontend/strategy_validation.test.cjs', 'tests/test_unified_exposure_gate_691.py', 'browser_tests/flows.py', '.github/ci/pytest_groups.json', 'frontend/product_ui.css', 'api/server.py', 'frontend/product_ui.js', 'tests/frontend/product_ui.test.cjs', 'tests/test_forward_paper_610a.py', 'tests/test_bot_activity_68.py', 'config.py'}
SEMANTIC_610C: dict = {}                                                                  # Stage 6.10C: no execution semantic change
INSTRUMENTATION_610C: dict = {}                                                           # Stage 6.10C: no executor instrumentation
BASE_541 = "a7dc4456b407458c9751ea88398cee37f3084d2f"                                   # main: Stage 6.10C merged (PR #35)
NEW_541 = {"tests/test_evidence_gate_541.py"}                                             # Stage 5.4.1 (evidence gate reads the valid packet): its new file
EDITED_541 = {"trader_view/research.py", ".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_robinhood_portfolio_69.py",
              "tests/test_unified_exposure_gate_691.py", "tests/test_forward_paper_610a.py", "tests/test_decision_flow_610c.py",
              "tests/test_strategy_validation_62.py"}                                    # Stage 5.4.1: the files of main it edits (the fix, its CI entry, the freeze bookkeeping)
SEMANTIC_541 = {                                                                          # Stage 5.4.1: the INTENTIONAL correction, file by file
    "trader_view/research.py": "EvidenceResearchAdapter.latest() returns the newest COMPLETED packet created at or before now (PPE Confidence's rule, "
                               "ppe/evidence_context.py) instead of the newest packet of any status: a newer FAILED build no longer reads as no evidence "
                               "in the setup's evidence gate (DESIGN_58 section 5); with no COMPLETED packet it is read exactly as before",
}
BASE_611FIX = "2fe29f3a48359a365a1506c9f70093129be72602"                                # main: Stage 5.4.1 merged (PR #36)
NEW_611FIX = {"tests/test_chart_empty_state_611fix.py"}                                   # Stage 6.1.1 CSS fix (chart empty state): its new file
EDITED_611FIX = {"frontend/product_ui.css", ".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py",
                 "tests/test_robinhood_portfolio_69.py", "tests/test_unified_exposure_gate_691.py", "tests/test_forward_paper_610a.py",
                 "tests/test_decision_flow_610c.py", "tests/test_strategy_validation_62.py", "tests/test_evidence_gate_541.py"}   # the CSS fix, its CI entry, the freeze bookkeeping
SEMANTIC_611FIX: dict = {}                                                                # Stage 6.1.1 CSS fix: no execution semantic change (display only)
BASE_OPS = "1bd7542dd392035e445ae0c93ecb503a934fcf53"                                   # main: chart empty-state CSS fix merged (PR #37)
NEW_OPS = {"scripts/install_market_morning_task.ps1", "scripts/start_market_morning.ps1", "tests/test_runtime_reliability_ops.py"}   # ops runtime reliability: its new files
EDITED_OPS = {"tests/test_chart_empty_state_611fix.py", ".github/ci/pytest_groups.json", "scripts/check_stock_agent.ps1", "scripts/run_stock_agent.py", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_decision_flow_610c.py", "tests/test_evidence_gate_541.py", "tests/test_forward_paper_610a.py", "tests/test_robinhood_portfolio_69.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # ops runtime reliability: the files of main it edits
SEMANTIC_OPS: dict = {}                                                                    # ops runtime reliability: no execution, strategy, risk or startup semantics change
LEDGER = {
    ".github/ci/pytest_groups.json": "546b18afe10df2d8ca4e167331d89b0530f4c2c8a46cb5fc821698344140a157",
    "api/routes/account_refresh.py": "26f4ebfa3059e2d7f24fe608a8d4f0a6c582daaa237828920e561bbcc4891319",
    "api/routes/execution.py": "1bf1b7ecb079ecffe1e44141405c747e6deb18053cf527de35d628a313a930b2",
    "api/routes/forward_validation.py": "33593a7a4215d5bef1cb2b15609152775d0887cda8f27bad235bc33f4699f17b",
    "api/server.py": "17bd28fe17c81cd4efe5aa8c95562bbf1afe63b79876f97d6b3a16e35d699b33",
    "browser_tests/flows.py": "5965eaac2f5ae36ebfe1e52eed45cafcc4f0aaf33a3dbb3c30c3d35ea36679fc",
    "config.py": "1dc47eba4d6813162ea726d71839dde44c4b788fb15dce6114e8f5ecd9acd00b",
    "execution/engine.py": "06e898d9e075ad766c247ed095806c49d994f413ecc056935e5fb9a3dbdd084c",
    "execution/models.py": "177a81a89604583cfffc9254da31227c1f35fb98e376dbfdeca9448128c1bfa8",
    "execution/scheduler.py": "6d8459eecb230fa52193c24a6fd0aade17de2959162aefb43010f3da61f57183",
    "execution/service.py": "4a21f8a69bc1e086fb89f0a225d0f47bb8ce15ebf7246b62d2079a9b0b900d01",
    "forward_validation/reports.py": "c4f24a09cd46180e60584d0315fe3b1b00db220615817fd915d87e4cdf8ee473",
    "forward_validation/service.py": "c204e7b589912169159302c493d0cfcd6bd42d3af8340a0764c805c8af04bc33",
    "frontend/product_ui.css": "7ccb310cc903b95740ba84f9a76a97766640f7f5c1d3ac2747575f061b2f356a",
    "frontend/product_ui.js": "73064019c9fbf62e9a60d9a3d7c721c4ed352c1ea2f70ee90c3026458ff966dc",
    "frontend/trader_view.js": "cc83857e686c92ea04235c86954c96ea505073381d31664af26d1f18c356bebd",
    "scripts/check_stock_agent.ps1": "f4dab15027feb15457f2107e04702de4e5bd595b45d570eb74f8de5768c4a677",
    "scripts/install_stock_agent_task.ps1": "fef37c2a6da6fb90539ee32c577036a701eb04ad7ac5e16baa5454eb3d98718e",
    "scripts/run_stock_agent.py": "1090690982d115e1f268738ebdb2b797e4f79b88be2c5d58e5f042010ff710c3",
    "tests/frontend/product_flow.test.cjs": "7828fabb3f5902ac9582f5dc429d91628742a5f8e3c7c98640230c694b618c49",
    "tests/frontend/product_ui.test.cjs": "ab696a8b48a8d2f732f9addbe4e897bffd6a13339862ffaba28f1740f3d18743",
    "tests/frontend/strategy_validation.test.cjs": "a1acf70be167661df5a64f515b25e498167a912c9176d34548aeb9859ba28580",
    "tests/test_account_refresh_601.py": "42aeb7db6c614d1c0cf41cc7d9c8bf8bbcabebd5301d7dea1fafba4e0eab2587",
    "tests/test_cash_flow_602.py": "15581c69f6fe12e475f7311a03652bb0e128e06eaaa7bc227eab6281eae141ec",
    "tests/test_ci_groups_603.py": "8b1b0a4659424e7fc733a275dae76e3b72ac3f7df156234a8bf8c72f3e57c477",
    "tests/test_db_bootstrap_busy.py": "0253939a872b26f83dd479c31432adc75c5ba7000d3842a84dcc8d3717b38d86",
    "tests/test_demand_57.py": "2b76510307e4973f2c932ca64fb6759990d2e9ebd6328e1a3a6b8f4cb2a16ba0",
    "tests/test_execution_60.py": "578562ba9f96c1fa337669c91269abe011783ff54b9c08f84b20d99688bd24cf",
    "tests/test_execution_safety_591.py": "e5d2a1faefc7be7f04077f744155c2e9dbe82b2b0b35ebe2cff27f26b39f0c89",
    "tests/test_forward_validation_61.py": "d7b5260e7b70bdae9344f5d26212ac0dcf3200651b374ab72e668166a4363fe7",
    "tests/test_product_flow_612.py": "6864546373db260d6e4e4aeb168718c6c811bfe15b55f90401ab066a6cecdb51",
    "tests/test_product_ui_611.py": "67f85272b938d19ab22522fd4c6481308437f953bbed2e85a86f5c35e03e90d4",
    "tests/test_proposals_59.py": "9ac55e1325936c2d084474f6207a4efb0cbc618e98edc9dcbdec626965a081bd",
    "tests/test_regression_isolation.py": "f596017894f68ced030b1d4de720b201150b9637d390c2211347f9bb7cb15b83",
    "tests/test_setups_58.py": "e4d4bbaff2c9c380d117345767119c05197ed93dd8cd647e8003aaae9f3ce307",
    "tests/test_strategy_validation_62.py": "76c324b6bf11ff8e2d4723ad70e826fc77452cf9412a314c121ee4f6e0f10f0a",
    "tests/test_trader_view_55.py": "998366a1d1cf3bc939f9ca00bcdaa8f60824be2b3712619b10947578433047e3",
    "tests/test_trader_view_561.py": "c98637d7b4acaf253c26bfee172a0b1d775454ac912c8a2406b33f5f87eae620",
    "tests/test_windows_runner_60.py": "7862cdc250a7c5da7db73a1385cefccd969a1b872d6c88e31199736500bedf58",
    "trader_view/positions.py": "3765462dea8fcbb52b6c1880338a8e0e1c5151a60091ef0d11c3489a55267558",
    "trader_view/research.py": "01c27bfe8c4d852a2be425a1258760af5d1c5bd3b175531e24cf5f49be1551f6",
    "trader_view/stream.py": "deee331b13a7ceb6fd04bfe4adbe716494db0f87c345fa8195005cb7a0cc45a5",
}


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def sha(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def now_text(rel: str) -> str:
    return _norm((ROOT / rel).read_text(encoding="utf-8"))


def base_text(rel: str) -> str:
    out = subprocess.run(["git", "show", f"{BASE_68}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert out, f"{rel} is not in {BASE_68} (fetch-depth 0 is required)"
    return _norm(out.decode("utf-8"))


def view(rel: str, text: str) -> str:
    """`text` (the current file) as the freeze tests see it: a registered file is main's version, after proving it is exactly the registered one."""
    if rel not in LEDGER:
        return text
    base = base_text(rel)
    if _norm(text) == base:
        return base                                                                      # already read through the ledger (e.g. undo_62 after a shadowed read)
    assert sha(text) == LEDGER[rel], f"{rel} differs from the Stage 6.8 ledger entry (tests/freeze_ledger.py): register the change or revert it"
    return base


def view_bytes(rel: str, data: bytes) -> bytes:
    return data if rel not in LEDGER else view(rel, data.decode("utf-8")).encode("utf-8")


def unchanged(paths, blob_fn) -> set:
    """The registered files among `paths` that, read through the ledger, equal the caller's base (`blob_fn(rel)`, str or bytes): for that freeze
    test they did not change. A registered file that differed from the caller's base on main stays in its change set and is checked as before."""
    out = set()
    for rel in paths:
        if rel in LEDGER:
            b = blob_fn(rel)
            if view(rel, now_text(rel)) == _norm(b.decode("utf-8") if isinstance(b, bytes) else b):
                out.add(rel)
    return out
