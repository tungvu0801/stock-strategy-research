"""
tests/freeze_ledger.py - the Stage 6.8 post-freeze ledger (account position visibility + Bot Activity; DESIGN_68 section 6).

The earlier freeze tests pin execution, Trader View, route and UI files to their own base commits. Stage 6.8 changes some of those files on
purpose. Instead of loosening any freeze test, every such file is registered here with the SHA-256 of the exact text Stage 6.8 leaves, and
the freeze tests read it through `view()`: a registered file whose text matches its hash is seen exactly as it was on main (BASE_68), where
every freeze test passed. Any other change - or any later edit of a registered file without updating its hash - still fails, and
tests/test_bot_activity_68.py::test_F01 proves that the change set since main is exactly NEW_68 + NEW_69 + NEW_691 + NEW_610A + NEW_610C + NEW_541 + NEW_611FIX + NEW_OPS + NEW_610D + NEW_542 + NEW_70 + NEW_71 + NEW_72 + NEW_73 + NEW_731 + NEW_74 + NEW_75 + NEW_78 + NEW_78B1 + NEW_W1 + NEW_W2A + NEW_W2B plus this ledger.
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

Stage 6.10D (engine health visibility) is observability only: GET /api/ops/engine (the deployed commit read from .git without running git, the
process facts, the runner's PID file when it names this process) and one display-only fold under the diagnostics line (frontend/product_ui.js)
that shows what the backend already reports - the Stage 6.10A start-up outcome, the preflight and its non-PASS checks, the supervisor, the worker
heartbeat and the Stage 6.9 Robinhood gateway state (DESIGN_610D_ENGINE_HEALTH.md). SEMANTIC_610D and INSTRUMENTATION_610D are empty: no execution,
strategy, risk, Paper Auto, startup, Trader View or Robinhood file changes, and tests/test_engine_health_610d.py (F tests) pins exactly that.

Stage 5.4.2 (automatic evidence refresh; DESIGN_542_EVIDENCE_REFRESH.md) adds the evidence_refresh package, one GET route and a default-OFF
refresh thread wired into the server lifespan, appends its settings to config.py, and gives the SEC companyfacts request alone a larger
byte cap (evidence/sources/sec_edgar.py, now a ledger entry so the earlier freeze tests read it as on main). Its only intentional
semantic change is that evidence content (SEMANTIC_542); no execution, READY-threshold, PPE-scoring, startup or Paper Auto change, no
schema change. tests/test_evidence_refresh_542.py pins behaviour and scope (BASE_542 / NEW_542 / EDITED_542).

Stage 7.0 (market intelligence contracts; DESIGN_70_MARKET_INTEL_CONTRACTS.md) adds the pure market_intel package (the observation
envelope, settings, freshness policy, validation codes, session calendar and request budgets) and its tests, stacked on Stage 5.4.2
(BASE_70 is PR #40's head). No config, server, route, thread, network, database, execution, setup, proposal, safety or UI change and
nothing in the live path imports it (SEMANTIC_70 = {}). tests/test_market_intel_70.py pins behaviour and scope (BASE_70 / NEW_70 /
EDITED_70).

Stage 7.1 (market intelligence store; DESIGN_71_MARKET_INTEL_STORE.md) adds the append-only, point-in-time store in its own SQLite file
(created lazily, never at import), its migrations, the raw archive, the parse-only Alpaca bars adapter and the offline backfill planner
/ CLI (a real backfill stays unapproved and impossible from the CLI), plus the verified 2025 NYSE history calendar owned by market_intel.
Its one intentional semantic change: market_intel/calendar.py covers 2025 (SEMANTIC_71); autorun/calendar.py is untouched. No config,
server, route, thread, network, execution, setup, proposal, safety or UI change. tests/test_market_intel_store_71.py pins behaviour
and scope (BASE_71 / NEW_71 / EDITED_71).

Stage 7.2 (market intelligence event and reference adapters; DESIGN_72_MARKET_INTEL_SOURCES.md) adds parse-only adapters - Alpaca news
and snapshots, SEC filings and company facts, FRED / ALFRED, the Nasdaq halt RSS, the Alpha Vantage earnings calendar - plus deterministic
news attribution, deduplication and entity matching, and an offline CLI whose fetch is always refused. They emit Stage 7.0 envelopes into
the unchanged Stage 7.1 store. No transport, key, config, server, route, thread, network, execution, setup, proposal, safety or UI change;
the earnings provider is not wired. tests/test_market_intel_sources_72.py pins behaviour and scope (BASE_72 / NEW_72 / EDITED_72).

Stage 7.3 (market intelligence collector and scheduling, offline; DESIGN_73_MARKET_INTEL_COLLECTOR.md) adds market_intel/collector:
registered collection plans, an injectable transport (a fake in tests; the default refuses the network), budgets, leases, retry with
deterministic jitter, a circuit breaker, its own append-only state file (runs, watermarks, leases, breaker), feed health and a thread-free
scheduler. It writes FIXTURE datasets through the unchanged Stage 7.1 store and refuses any real transport, live sink, blocked (SEC) plan,
missing credential or historical window. No transport, key, config, server, route, thread, job, network, execution, setup, proposal,
safety or UI change. tests/test_market_intel_collector_73.py pins behaviour and scope (BASE_73 / NEW_73 / EDITED_73).

Stage 7.3.1 (market intelligence schema v2 and data lifecycle, offline; operator-designated intermediate stage; 7.4 keeps its scope;
DESIGN_731_MARKET_INTEL_SCHEMA_V2.md) adds schema v2 in new modules only: a distinct COLLECT dataset kind that only a store-checked,
single-use OFFLINE_VERIFICATION authorization creates (the schema admits no LIVE claim), an explicit copy-only v1 -> v2 migration with
pre- and post-verification, point-in-time news cluster and ticker-mention persistence, and copy-only lifecycle primitives. The Stage 7.1
store and migrations are unchanged and still refuse v2. No transport, key, config, server, route, thread, job, network, live ingestion,
production migration, deletion, execution or UI change. tests/test_market_intel_schema_v2_731.py pins it (BASE_731 / NEW_731 / EDITED_731).

Stage 7.4 (market intelligence data quality and latency report, offline and read-only; DESIGN_74_MARKET_INTEL_QUALITY.md) adds one new
module, market_intel/quality.py: freshness, coverage, latency, timestamp, integrity, provider-health and IEX/SIP divergence sections with an
explainable PASS / WARN / BLOCK / INSUFFICIENT_DATA verdict, as JSON or a text summary. It reads byte copies of its inputs only and reuses
the Stage 7.0-7.3.1 interfaces unchanged. No write, transport, key, config, server, route, thread, job, network, migration, execution or UI
change. tests/test_market_intel_quality_74.py pins it (BASE_74 / NEW_74 / EDITED_74).

Stage 7.5 (the shared market scanner v1, offline core; DESIGN_75_MARKET_SCAN.md) adds one new package, market_scan/ (features, flags, filters,
run): the pre-registered D.1 features and flags, the Stage 7.4 quality gate, per-horizon eligibility and the gap-list sequence, as one JSON
document per scan. It reads byte copies of its inputs only (the Stage 7.4 protocol) and reuses the Stage 7.0-7.4 interfaces unchanged. No
write, transport, key, config, server, route, thread, job, network, migration, execution or UI change. The earlier stages' isolation guards
name market_scan/ as the one declared reader. tests/test_market_scan_75.py pins it (BASE_75 / NEW_75 / EDITED_75).

Stage 7.8 Part B (the swing trend pullback research baseline V1 and its control C1, synthetic data only; DESIGN_78_SWING_PULLBACK.md) adds
one new package, swing_pullback/ (indicators, data, core, adapter, diagnostics, plan), and its synthetic fixtures. It runs the Stage 6.2
validation harness unchanged and registers nothing: no harness adapter entry, no strategy registry entry. No config, route, scheduler,
network, database, execution or UI change. tests/test_swing_pullback_78.py pins it (BASE_78 / NEW_78 / EDITED_78).

Stage 7.8B-1 (the isolated SIP research dataset builder, the pre-run data-quality report, the evaluation manifest and the offline V1 / C1
evaluator, synthetic data only; DESIGN_78B1_SIP_EVALUATION.md) adds two new packages, research_data/ and swing_eval/, and their synthetic
fixtures, and corrects the wording of DESIGN_78_SWING_PULLBACK.md (final-review M-6). No existing module changes: the IEX builder, the harness,
swing_pullback/, config, routes and every live path are byte-identical. No fetch, no database, no registration. tests/test_sip_eval_78b1.py
pins it (BASE_78B1 / NEW_78B1 / EDITED_78B1).

Wave 1 safety remediation (the independent validation audit of main 4c01519; DESIGN_W1_SAFETY_REMEDIATION.md) changes EXECUTION SAFETY
SEMANTICS on purpose, in three places, and nothing else: the safety state can no longer lose an Emergency Stop, a Pause or the day's loss halt
to a concurrent evaluation or action, and an unreadable state row is UNKNOWN (safety/service.py, safety/store.py); the stop-loss check fails
closed on a missing, invalid or stale price - never "no exit condition" - and new entries stop while a held position's stop cannot be checked
(execution/service.py, ADDITIVE lines only); and config.py loads only its own checkout's .env while test processes load none and hold no
credential (config.py, tests/conftest.py). SEMANTIC_W1 names those files; REMOVED_W1 lists the only lines of main it removes from a file whose
earlier freeze tests keep every line. No threshold, frozen control (b5d4a56e...), strategy rule, market-data reader or broker call changes.
tests/test_wave1_safety_remediation.py pins it (BASE_W1 / NEW_W1 / EDITED_W1).

Wave 2 PR A (forward-validation accounting and the early stop; the reviewed Wave 2 planning package, design Revision 3.2) changes EXECUTION
ACCOUNTING and the EARLY STOP on purpose, and no decision: the exit P/L of a paper position is recomputed from every exit order's cumulative
broker fill, a price-only observation re-runs it and every fill event carries the P/L snapshot (execution/service.py, execution/store.py; the
quantity, state, hand-back and every order decision are pinned unchanged by a golden taken on main); execution.VERSION moves to execution_v2;
the Stage 6.1 observer reports P/L from VERIFIED trades only, serves fresh read-only daily summaries, persists them after the early stop and
consumes the early stop per incident element, so a stop is retried until the persisted mode confirms it and only a person's later arm
acknowledges it (forward_validation/*, api/routes/forward_validation.py). SEMANTIC_W2A names those files; pre_w2a() gives an older freeze
test main's text of them (BASE_W2A, where every earlier freeze test passed); VERSION_PINS_W2A pins each decision / accounting block to the
execution.VERSION it belongs to. No threshold, frozen control (b5d4a56e...), strategy, sizing, risk, broker or market-data rule changes.
tests/test_wave2_pra.py pins it (BASE_W2A / NEW_W2A / EDITED_W2A, the closed function list, the version pins).

Wave 2 PR B (the Stage 6.1 session classifier v2; design Revision 3.4.1) changes SESSION HEALTH on purpose, and nothing of execution, risk,
strategy or the early stop: confirmed date transitions, the post-close evidence grace with settle conditions, strict evidence reads, the
clock-skew guard and the 6.10A AUTO_DISARM SESSION_ENDED exception (database/forward_validation_migrations.py, forward_validation/store.py,
forward_validation/metrics.py, forward_validation/service.py, forward_capture/health.py). SEMANTIC_W2B names those files; pre_w2b() gives an
older freeze test main's text of them (BASE_W2B); NEW_LEDGER_W2B registers the three that existed at BASE_68; UNDO_W2B undoes PR B's exact
in-place edits of older test files; SEMANTIC_TESTS_W2B declares every changed test function. tests/test_wave2_prb.py pins it (BASE_W2B /
NEW_W2B / EDITED_W2B, the closed function list). Its Stage 6.1 Browser Harness flow (browser_tests/flows.py, re-hashed here) gives the v2
classifier its k = 3 observations before it asserts the confirmed OPEN record (F04B pins that edit exactly; T-A42's lines are untouched).
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
BASE_610D = "c9e4f4522b29b5f0acb30e24b431ffda598fe2a0"                                  # main: ops reliability merged (PR #39)
NEW_610D = {"tests/test_engine_health_610d.py", "tests/frontend/engine_health_610d.test.cjs", "DESIGN_610D_ENGINE_HEALTH.md"}   # Stage 6.10D (engine health visibility): its new files
EDITED_610D = {"tests/test_chart_empty_state_611fix.py", "tests/test_runtime_reliability_ops.py", "api/routes/ops_status.py", "frontend/product_ui.js", ".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py",
               "tests/test_robinhood_portfolio_69.py", "tests/test_unified_exposure_gate_691.py", "tests/test_forward_paper_610a.py", "tests/test_decision_flow_610c.py",
               "tests/test_strategy_validation_62.py", "tests/test_evidence_gate_541.py",
               "tests/frontend/product_ui.test.cjs", "tests/frontend/product_flow.test.cjs", "tests/frontend/strategy_validation.test.cjs",
               "tests/test_product_ui_611.py", "tests/test_product_flow_612.py"}            # Stage 6.10D: the files of main it edits (the route, the fold, its CI entry, the freeze bookkeeping, the GET allow-lists)
SEMANTIC_610D: dict = {}                                                                  # Stage 6.10D: no execution semantic change
INSTRUMENTATION_610D: dict = {}                                                           # Stage 6.10D: no executor instrumentation
BASE_542 = "ccee3d31f3a291f178b667f46317d58ce3267bec"                                   # main: Stage 6.10D merged (PR #38)
NEW_542 = {"DESIGN_542_EVIDENCE_REFRESH.md", "api/routes/evidence_refresh.py", "evidence_refresh/__init__.py", "evidence_refresh/journal.py", "evidence_refresh/policy.py", "evidence_refresh/scheduler.py", "evidence_refresh/service.py", "tests/test_evidence_refresh_542.py"}   # Stage 5.4.2 (evidence refresh): its new files
EDITED_542 = {".github/ci/pytest_groups.json", "api/server.py", "config.py", "evidence/sources/sec_edgar.py", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_forward_paper_610a.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the wiring, the settings, the SEC cap, the CI entry and the freeze bookkeeping
SEMANTIC_542 = {                                                                          # Stage 5.4.2: the INTENTIONAL evidence-content change, file by file
    "evidence/sources/sec_edgar.py": "the SEC companyfacts request alone may be EVIDENCE_SEC_FACTS_MAX_BYTES (25 MB) instead of EVIDENCE_MAX_BYTES (2 MB): large caps regain their XBRL fundamentals rung, which can change a packet's strength and the company_down / guidance inputs the READY evidence check already reads; the READY rule and its thresholds are unchanged",
}
INSTRUMENTATION_542: dict = {}                                                            # Stage 5.4.2: no executor instrumentation
BASE_70 = "13c1635ca77bcd028d3e0aa9bf821376e4860905"                                    # PR #40 head: Stage 5.4.2 complete (Stage 7.0 is stacked on it)
NEW_70 = {"DESIGN_70_MARKET_INTEL_CONTRACTS.md", "market_intel/__init__.py", "market_intel/budget.py", "market_intel/calendar.py", "market_intel/freshness.py", "market_intel/models.py", "market_intel/settings.py", "market_intel/validate.py", "tests/test_market_intel_70.py"}   # Stage 7.0 (market intelligence contracts): its new files
EDITED_70 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry and the freeze bookkeeping only
SEMANTIC_70: dict = {}                                                                    # Stage 7.0: pure contracts, nothing in the live path reads them
INSTRUMENTATION_70: dict = {}                                                             # Stage 7.0: no executor instrumentation
BASE_71 = "4e2f115dd9826647c38474506984f89546e3d9e0"                                    # main: Stage 7.0 merged (PR #41)
NEW_71 = {"DESIGN_71_MARKET_INTEL_STORE.md", "market_intel/approvals.py", "market_intel/archive.py", "market_intel/calendar_history.py", "market_intel/ingest.py", "market_intel/migrations.py", "market_intel/sources/__init__.py", "market_intel/sources/alpaca_bars.py", "market_intel/store.py", "tests/test_market_intel_store_71.py"}   # Stage 7.1 (market intelligence store): its new files
EDITED_71 = {".github/ci/pytest_groups.json", ".gitignore", "DESIGN_70_MARKET_INTEL_CONTRACTS.md", "market_intel/__init__.py", "market_intel/calendar.py", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # calendar coverage, docs, .gitignore, the CI entry and the freeze bookkeeping
SEMANTIC_71 = {                                                                           # Stage 7.1: the INTENTIONAL behaviour change, file by file
    "market_intel/calendar.py": "2025 is covered by the verified market_intel history table (calendar_history.py: NYSE publications + an empirical cross-check): 2025 weekdays read MARKET_DAY / HOLIDAY instead of NOT_COVERED; 2024 and earlier and 2028 onward stay NOT_COVERED; on every NYSE early-close day the post-market ends at 17:00 New York time (the NYSE release), so 17:00-20:00 reads CLOSED: the 2025 history table, and for 2026-2027 the versioned market_intel late-trading override (nyse_late_trading_2026_2027_v1, operator-approved: the one declared exception to the Stage 7.0 single-source rule); post_market_end_minute() is new; autorun/calendar.py is unchanged",
}
INSTRUMENTATION_71: dict = {}                                                             # Stage 7.1: no executor instrumentation
BASE_72 = "05358cb0f617697cc760de4e53cda107841a352b"                                    # main: Stage 7.1 merged (PR #42)
NEW_72 = {"DESIGN_72_MARKET_INTEL_SOURCES.md", "market_intel/news/__init__.py", "market_intel/news/attribution.py", "market_intel/news/dedup.py", "market_intel/news/entities.py", "market_intel/sources/alpaca_news.py", "market_intel/sources/alpaca_snapshots.py", "market_intel/sources/cli.py", "market_intel/sources/common.py", "market_intel/sources/earnings_av.py", "market_intel/sources/fred.py", "market_intel/sources/halts.py", "market_intel/sources/sec_facts.py", "market_intel/sources/sec_filings.py", "tests/test_market_intel_sources_72.py"}   # Stage 7.2 (market intelligence adapters): its new files
EDITED_72 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_store_71.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the earlier stages' F01 unions, the 7.1 registry / F tests, bookkeeping
SEMANTIC_72: dict = {}                                                                    # Stage 7.2: no behaviour change of an existing file
INSTRUMENTATION_72: dict = {}                                                             # Stage 7.2: no executor instrumentation
BASE_73 = "7376568260bcd8e18282593fbc82b4927248644a"                                    # main: Stage 7.2 merged (PR #43)
NEW_73 = {"DESIGN_73_MARKET_INTEL_COLLECTOR.md", "market_intel/collector/__init__.py", "market_intel/collector/health.py", "market_intel/collector/plans.py", "market_intel/collector/policy.py", "market_intel/collector/runner.py", "market_intel/collector/scheduler.py", "market_intel/collector/state.py", "market_intel/collector/transport.py", "tests/test_market_intel_collector_73.py"}   # Stage 7.3 (market intelligence collector, offline): its new files
EDITED_73 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the earlier stages' F01 unions, the 7.1 registry, the 7.2 F tests, bookkeeping
SEMANTIC_73: dict = {}                                                                    # Stage 7.3: no behaviour change of an existing file
INSTRUMENTATION_73: dict = {}                                                             # Stage 7.3: no executor instrumentation
BASE_731 = "ad60a0211a1e76de86e8abd5d8734921d9034a9a"                                   # main: Stage 7.3 merged (PR #44)
NEW_731 = {"DESIGN_731_MARKET_INTEL_SCHEMA_V2.md", "market_intel/collect_auth.py", "market_intel/lifecycle.py", "market_intel/news/persist.py", "market_intel/schema_v2.py", "market_intel/store_v2.py", "tests/test_market_intel_schema_v2_731.py"}   # Stage 7.3.1 (market intelligence schema v2, offline): its new files
EDITED_731 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_collector_73.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the earlier stages' F01 unions, the 7.1 registry, the 7.3 F tests, bookkeeping
SEMANTIC_731: dict = {}                                                                   # Stage 7.3.1: no behaviour change of an existing file
INSTRUMENTATION_731: dict = {}                                                            # Stage 7.3.1: no executor instrumentation
BASE_74 = "b9c36212e0104831d665d22f8a2eaa3d4c4885c9"                                    # main: Stage 7.3.1 merged (PR #45)
NEW_74 = {"DESIGN_74_MARKET_INTEL_QUALITY.md", "market_intel/quality.py", "tests/test_market_intel_quality_74.py"}   # Stage 7.4 (market intelligence data quality and latency report, offline): its new files
EDITED_74 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_collector_73.py", "tests/test_market_intel_schema_v2_731.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the earlier stages' F01 unions, the 7.1 registry, the 7.3.1 F tests, bookkeeping
SEMANTIC_74: dict = {}                                                                    # Stage 7.4: no behaviour change of an existing file
INSTRUMENTATION_74: dict = {}                                                             # Stage 7.4: no executor instrumentation
BASE_75 = "1d817939af64f38e95745e456595225ebf809c10"                                    # main: Stage 7.4 merged (PR #46)
NEW_75 = {"DESIGN_75_MARKET_SCAN.md", "market_scan/__init__.py", "market_scan/features.py", "market_scan/filters.py", "market_scan/flags.py", "market_scan/run.py", "tests/test_market_scan_75.py"}   # Stage 7.5 (the shared market scanner v1, offline core): its new files
EDITED_75 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_dynamic_universe_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_collector_73.py", "tests/test_market_intel_quality_74.py", "tests/test_market_intel_schema_v2_731.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the earlier F01 unions and isolation guards, the 7.4 F tests, bookkeeping
SEMANTIC_75: dict = {}                                                                    # Stage 7.5: no behaviour change of an existing file
INSTRUMENTATION_75: dict = {}                                                             # Stage 7.5: no executor instrumentation
BASE_78 = "d1de7514d551f9eb261f60117fea58586af89a9b"                                    # main: Stage 7.5 merged (PR #47)
NEW_78 = {"DESIGN_78_SWING_PULLBACK.md", "swing_pullback/__init__.py", "swing_pullback/adapter.py", "swing_pullback/core.py", "swing_pullback/data.py", "swing_pullback/diagnostics.py", "swing_pullback/indicators.py", "swing_pullback/plan.py", "tests/swing_pullback_fixtures.py", "tests/test_swing_pullback_78.py"}   # Stage 7.8 Part B (the swing trend pullback baseline, synthetic only): its new files
EDITED_78 = {".github/ci/pytest_groups.json", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_collector_73.py", "tests/test_market_intel_quality_74.py", "tests/test_market_intel_schema_v2_731.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py", "tests/test_market_scan_75.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the earlier F01 unions, the 7.5 F tests, bookkeeping
SEMANTIC_78: dict = {}                                                                    # Stage 7.8: no behaviour change of an existing file
INSTRUMENTATION_78: dict = {}                                                             # Stage 7.8: no executor instrumentation
BASE_78B1 = "b52e0dc7d91672a2db7dd95f6f19f3e1c25d138b"                                  # main: Stage 7.8 Part B merged (PR #48)
NEW_78B1 = {"DESIGN_78B1_SIP_EVALUATION.md", "research_data/__init__.py", "research_data/nyse_calendar.py", "research_data/quality.py", "research_data/sip_bundle.py", "swing_eval/__init__.py", "swing_eval/analysis.py", "swing_eval/codetree.py", "swing_eval/evaluate.py", "swing_eval/manifest.py", "swing_eval/preflight.ps1", "tests/sip_eval_fixtures.py", "tests/test_sip_eval_78b1.py"}   # Stage 7.8B-1 (SIP dataset builder, pre-run report, manifest, offline evaluator; synthetic only): its new files
EDITED_78B1 = {".github/ci/pytest_groups.json", "DESIGN_78_SWING_PULLBACK.md", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py", "tests/test_decision_flow_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py", "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_collector_73.py", "tests/test_market_intel_quality_74.py", "tests/test_market_intel_schema_v2_731.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py", "tests/test_market_scan_75.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_strategy_validation_62.py", "tests/test_swing_pullback_78.py", "tests/test_unified_exposure_gate_691.py"}   # the CI entry, the M-6 wording, the earlier F01 unions, the 7.8 F tests and X02, bookkeeping
SEMANTIC_78B1: dict = {}                                                                  # Stage 7.8B-1: no behaviour change of an existing file
INSTRUMENTATION_78B1: dict = {}                                                           # Stage 7.8B-1: no executor instrumentation
BASE_W1 = "4c01519566a0bb66ebcff9cc51f5aa822194850b"                                    # main: Stage 7.8B-1 merged (PR #49); the audited baseline
NEW_W1 = {"DESIGN_W1_SAFETY_REMEDIATION.md", "tests/test_wave1_safety_remediation.py"}     # Wave 1 safety remediation: its new files
EDITED_W1 = {'.github/ci/pytest_groups.json', 'config.py', 'execution/service.py', 'safety/service.py', 'safety/store.py', 'tests/conftest.py', 'tests/freeze_ledger.py', 'tests/test_account_refresh_601.py', 'tests/test_automation_37.py', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py', 'tests/test_demand_57.py', 'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_execution_60.py', 'tests/test_execution_safety_591.py', 'tests/test_forward_paper_610a.py', 'tests/test_history_budget_39.py', 'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py', 'tests/test_proposals_59.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_setups_58.py', 'tests/test_sip_eval_78b1.py', 'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_trader_view_55.py', 'tests/test_unified_exposure_gate_691.py'}   # the files of main Wave 1 edits: the fix, the CI entry, the earlier freeze tests' unions / raw reads, bookkeeping
SEMANTIC_W1 = {                                                                            # Wave 1: the INTENTIONAL behaviour change, file by file
    "safety/service.py": "evaluations decide on a freshly read state and commit with expect= (re-decide on a conflict, at most 3 times, then UNKNOWN); actions apply to the state as read; a stop or pause whose evaluation fails is still saved alone",
    "safety/store.py": "state() reads strictly (unreadable = SafetyStoreError, never the default); commit(expect=) refuses a changed row or baseline inside BEGIN IMMEDIATE; latch() sets a stop or pause alone",
    "execution/service.py": "the stop check fails closed on a missing / invalid / undated / stale price (UNKNOWN + attention + new entries blocked; WARN while the market is closed)",
    "config.py": "load_dotenv(PROJECT_ROOT / '.env'): this checkout's own .env only, never a parent folder's; skipped when STOCK_AGENT_SKIP_DOTENV=1 (test processes)",
    "tests/conftest.py": "test processes and their children skip config.py's .env (STOCK_AGENT_SKIP_DOTENV) and hold no credential variable",
}
INSTRUMENTATION_W1: dict = {}                                                              # Wave 1: no executor instrumentation
REMOVED_W1 = {"config.py": ("# Load variables from a local .env file (if present) into the environment.", "load_dotenv()")}   # main's lines Wave 1 removes
UNDO_W1 = {                                                                                  # Wave 1's in-place edits of a file an older freeze test reads raw: (main, Wave 1)
    "config.py": [('# Load variables from a local .env file (if present) into the environment.\nload_dotenv()\n\nPROJECT_ROOT: Path = Path(__file__).resolve().parent\n\n',
                   'PROJECT_ROOT: Path = Path(__file__).resolve().parent\n\n# Load variables from THIS checkout\'s own .env file (if present) into the environment. The path is explicit (Wave 1, audit EP-13): with no\n# path, python-dotenv also searches every parent folder, so a checkout nested inside another one silently loaded the outer checkout\'s .env,\n# credentials included. Test processes set STOCK_AGENT_SKIP_DOTENV=1 (tests/conftest.py): neither they nor any process that inherits their\n# environment load a .env here (a process started through WMI inherits nothing), while a script\'s own explicit env file\n# (scripts/run_stock_agent.py -EnvFile, a synthetic one in tests) still loads.\nif os.environ.get("STOCK_AGENT_SKIP_DOTENV") != "1":\n    load_dotenv(PROJECT_ROOT / ".env")\n\n')],
    "tests/conftest.py": [('"""Test setup: isolated DB copy, no network. Must run before the app is imported."""\n',
                           '"""Test setup: isolated DB copy, no network, no .env and no credential. Must run before the app is imported."""\nimport os\nimport re\n'),
                          ('', '# Wave 1 (audit EP-13): no test process, and no process that inherits its environment, loads this checkout\'s .env (config.py skips it), and no\n# credential reaches a test from the environment, whichever folder the tests run from. A process started through WMI (the Stage 6.0 runner rig\'s\n# background start) inherits neither, so run tests outside the production checkout. A test that needs a credential-shaped value sets a fake\n# one itself; a script given an explicit synthetic env file still loads that file (python-dotenv itself is not switched off).\nos.environ["STOCK_AGENT_SKIP_DOTENV"] = "1"\nCREDENTIAL_ENV = re.compile(r"(?i)^(ALPACA|APCA|ANTHROPIC|ROBINHOOD|FRED_API_KEY|SEC_USER_AGENT|OPENAI|POLYGON|FINNHUB|ALPHA_?VANTAGE)")\nfor _name in [n for n in os.environ if CREDENTIAL_ENV.match(n)]:\n    del os.environ[_name]\n\n')],
}
BASE_W2A = "5d6f7dd1e1beb90fc5b2909a7dafabfd8e932a6a"                                   # main: Wave 1 merged (PR #50); the Wave 2 base
NEW_W2A = {"forward_validation/attention_signature.py", "tools/verify_exit_fills.py", "tests/test_wave2_pra.py", "tests/test_wave2_pra_accounting.py",
           "tests/test_wave2_pra_early_stop.py", "tests/wave2_pra_support.py", "tests/wave2_pra_golden.json"}   # Wave 2 PR A: its new files
EDITED_W2A = {".github/ci/pytest_groups.json", "api/routes/forward_validation.py", "execution/__init__.py", "execution/service.py", "execution/store.py",
              "forward_validation/reports.py", "forward_validation/service.py", "tests/freeze_ledger.py", "tests/test_bot_activity_68.py", "tests/test_chart_empty_state_611fix.py",
              "tests/test_decision_flow_610c.py", "tests/test_dynamic_universe_610c.py", "tests/test_engine_health_610d.py", "tests/test_evidence_gate_541.py",
              "tests/test_evidence_refresh_542.py", "tests/test_forward_paper_610a.py", "tests/test_market_intel_70.py", "tests/test_market_intel_collector_73.py",
              "tests/test_market_intel_quality_74.py", "tests/test_market_intel_schema_v2_731.py", "tests/test_market_intel_sources_72.py", "tests/test_market_intel_store_71.py",
              "tests/test_market_scan_75.py", "tests/test_robinhood_portfolio_69.py", "tests/test_runtime_reliability_ops.py", "tests/test_sip_eval_78b1.py",
              "tests/test_product_ui_611.py", "tests/test_strategy_validation_62.py", "tests/test_swing_pullback_78.py", "tests/test_unified_exposure_gate_691.py", "tests/test_wave1_safety_remediation.py"}   # the fix, the CI entry, the earlier freeze tests' unions / views, bookkeeping
SEMANTIC_W2A = {                                                                            # Wave 2 PR A: the INTENTIONAL behaviour change, file by file
    "execution/service.py": "the exit P/L is recomputed from every exit order's cumulative fill (N = sum(A_o * Q_o)); a price-only observation re-runs it; a price-less repeat keeps the stored price; every fill event carries the P/L snapshot. Quantities, states, hand-backs and every decision: unchanged (golden)",
    "execution/store.py": "three read-only accounting reads: exit_intents_for_position, accounting_view (one read transaction) and pnl_watermark_rows (raises on any read error)",
    "execution/__init__.py": "execution.VERSION execution_v1 -> execution_v2 (the accounting change; a new 6.10A cohort)",
    "forward_validation/service.py": "P/L from VERIFIED trades only and null - never 0 - for an unverified one (Rule F); fresh read-only daily summaries in a watermark bracket, persisted after the early stop; the early stop consumed per incident element and confirmed by the persisted mode",
    "forward_validation/reports.py": "the realized P/L of the text reports labelled partial / unstable / unknown; verified, pnl_status and unverified appended to the exports",
    "api/routes/forward_validation.py": "the six stored-summary read sites serve the fresh summary (read only)",
}
INSTRUMENTATION_W2A: dict = {}                                                             # Wave 2 PR A: no executor instrumentation
VERSION_BLOCKS_W2A = ('execution/service.py::ExecutionService._apply_order',
                     'execution/service.py::ExecutionService._apply_fill',
                     'execution/service.py::recompute_pnl',
                     'execution/service.py::ExecutionService._after_terminal',
                     'execution/service.py::ExecutionService._monitor_intent',
                     'execution/service.py::ExecutionService._monitor_intents',
                     'execution/service.py::ExecutionService._reconcile',
                     'execution/service.py::ExecutionService._set_recon',
                     'execution/service.py::ExecutionService._exit_context',
                     'execution/service.py::ExecutionService._evaluate_positions',
                     'execution/service.py::ExecutionService._maybe_submit_exit',
                     'execution/service.py::ExecutionService._scan_entries',
                     'execution/service.py::ExecutionService._recheck_entry',
                     'execution/service.py::ExecutionService._submit',
                     'execution/service.py::ExecutionService.enable_auto',
                     'execution/service.py::ExecutionService.disable_auto',
                     'execution/service.py::stop_price_problem',
                     'execution/engine.py::exit_decision',
                     'execution/engine.py::entry_controls',
                     'execution/engine.py::blocking',
                     'execution/engine.py::session_policy',
                     'execution/engine.py::entry_quantity',
                     'execution/engine.py::sessions_held',
                     'forward_validation/service.py::ForwardValidationService._early_stop',
                     'forward_validation/attention_signature.py::signature',
                     'forward_validation/attention_signature.py::_duplicates',
                     'forward_validation/attention_signature.py::_live_host',
                     'forward_validation/attention_signature.py::_gate',
                     'forward_validation/attention_signature.py::_persistent_mismatch',
                     'forward_validation/attention_signature.py::=HUMAN_ACTORS',
                     'forward_validation/attention_signature.py::=LIVE_HOST_TABLES',
                     'forward_validation/attention_signature.py::=READERS')
VERSION_PINS_W2A = {
    "execution_v1": {
        "execution/service.py::ExecutionService._apply_order": "0f759a824f1f9d0486e33f8e656f5e93fdb17a58bec6066e0ea34f545ab0fdd5",
        "execution/service.py::ExecutionService._apply_fill": "e06a9bef049691783318cc984138104258e59a027a403fa935a165bff9cf1e38",
        "execution/service.py::recompute_pnl": "ABSENT",
        "execution/service.py::ExecutionService._after_terminal": "ae38d36dcb3fda94898b0390b9ace836d5b2a28e6b9555a97b5f40accc153fe9",
        "execution/service.py::ExecutionService._monitor_intent": "3bfbe67b488b2f8b62b6d6690e05152d61957a7477b3a5dac0c1706e5bd86c85",
        "execution/service.py::ExecutionService._monitor_intents": "1a7d5d91c873d764a896476da1b7287a3a8843e198133aea004377d0dbb7d935",
        "execution/service.py::ExecutionService._reconcile": "f3bb9cccf13c9358b214b8da9bcab6639af25f11984627ea234ef55076b2ed07",
        "execution/service.py::ExecutionService._set_recon": "83af97fc5c3d3120568b562ebd1fc4247302acc099955e7f87dd301eb5c42923",
        "execution/service.py::ExecutionService._exit_context": "27d26403bf1541bbd1f04f14f5d32df3bfd3d0b1428149af4fd640bc5f30bd3a",
        "execution/service.py::ExecutionService._evaluate_positions": "7fdaca073f8f29f372bd0476d1b41a2afffa1feb9423b9407f14e6292901c72d",
        "execution/service.py::ExecutionService._maybe_submit_exit": "9517b81c8a9e64eb87e5f9e1d84cbb6799965e87e2e33f24b10df34b81356e42",
        "execution/service.py::ExecutionService._scan_entries": "60ac458bd0e6506fb70f1ff4c2f1fe20df87fb41fd3f7e4cffe5de9b7b347ae5",
        "execution/service.py::ExecutionService._recheck_entry": "e99a0b396778d5b0f43af7739f0bea85335ccd609507105866b487a88a2f3764",
        "execution/service.py::ExecutionService._submit": "76989ac3c65da51189aecbc1c344117573a7b229ec64606901327ecc0a35df06",
        "execution/service.py::ExecutionService.enable_auto": "3b0ea9c71fe041c7d870b07ecc72493f0112e65563e3e79d359996cff4532f07",
        "execution/service.py::ExecutionService.disable_auto": "50c1df4eaa55b2557290818ba1e12b2f2cc3c2f9dca7f6bcb80122a76b6205fe",
        "execution/service.py::stop_price_problem": "6ed950816c7bd4d6c6592c9331bb71542c181a362a8f40ea9bacedd02494f646",
        "execution/engine.py::exit_decision": "61a27b7aa0a57809fefe1ed183387384b5b7c05a6b212bb7679b77d302201a8f",
        "execution/engine.py::entry_controls": "daea54f6a9f86189834993eabfcac58ebe96edbf865ffd8ba88eee2f0929e7ce",
        "execution/engine.py::blocking": "7fae3bd49b0ec2bca4d62ac164b9279ab05ec05729ce0a6fd8c4f69fb828e415",
        "execution/engine.py::session_policy": "1092f637ad4a883196701ce094357653353acc5070ea009e3def5a8c964fd2db",
        "execution/engine.py::entry_quantity": "f0e2a0f4e8d2b98fcd6371e30aff71143ece2b15e3671070eaa77a3a11fab103",
        "execution/engine.py::sessions_held": "19941530e1d9d804fb14ea9040dd24621613810af18a4c296ba4e9c3dcaecfe1",
        "forward_validation/service.py::ForwardValidationService._early_stop": "89d27653504ebd498ce52eb70cae8d7694db2332e5ae7cb6be6840232b163fdd",
        "forward_validation/attention_signature.py::signature": "ABSENT",
        "forward_validation/attention_signature.py::_duplicates": "ABSENT",
        "forward_validation/attention_signature.py::_live_host": "ABSENT",
        "forward_validation/attention_signature.py::_gate": "ABSENT",
        "forward_validation/attention_signature.py::_persistent_mismatch": "ABSENT",
        "forward_validation/attention_signature.py::=HUMAN_ACTORS": "ABSENT",
        "forward_validation/attention_signature.py::=LIVE_HOST_TABLES": "ABSENT",
        "forward_validation/attention_signature.py::=READERS": "ABSENT",
    },
    "execution_v2": {
        "execution/service.py::ExecutionService._apply_order": "9b2760f71dd2bccf46e0d2c46b59ab512ae54111bc5f62a79bf60cabd9ebb884",
        "execution/service.py::ExecutionService._apply_fill": "6fb9726d464aa34bdb52c6147b681977dabe2053b54afd05b4b3beae04041c7d",
        "execution/service.py::recompute_pnl": "544ec665f8dfcf1138482409b842dc8421833c48163cde6db719aac11ac56dd2",
        "execution/service.py::ExecutionService._after_terminal": "ae38d36dcb3fda94898b0390b9ace836d5b2a28e6b9555a97b5f40accc153fe9",
        "execution/service.py::ExecutionService._monitor_intent": "3bfbe67b488b2f8b62b6d6690e05152d61957a7477b3a5dac0c1706e5bd86c85",
        "execution/service.py::ExecutionService._monitor_intents": "1a7d5d91c873d764a896476da1b7287a3a8843e198133aea004377d0dbb7d935",
        "execution/service.py::ExecutionService._reconcile": "f3bb9cccf13c9358b214b8da9bcab6639af25f11984627ea234ef55076b2ed07",
        "execution/service.py::ExecutionService._set_recon": "83af97fc5c3d3120568b562ebd1fc4247302acc099955e7f87dd301eb5c42923",
        "execution/service.py::ExecutionService._exit_context": "27d26403bf1541bbd1f04f14f5d32df3bfd3d0b1428149af4fd640bc5f30bd3a",
        "execution/service.py::ExecutionService._evaluate_positions": "7fdaca073f8f29f372bd0476d1b41a2afffa1feb9423b9407f14e6292901c72d",
        "execution/service.py::ExecutionService._maybe_submit_exit": "9517b81c8a9e64eb87e5f9e1d84cbb6799965e87e2e33f24b10df34b81356e42",
        "execution/service.py::ExecutionService._scan_entries": "60ac458bd0e6506fb70f1ff4c2f1fe20df87fb41fd3f7e4cffe5de9b7b347ae5",
        "execution/service.py::ExecutionService._recheck_entry": "e99a0b396778d5b0f43af7739f0bea85335ccd609507105866b487a88a2f3764",
        "execution/service.py::ExecutionService._submit": "76989ac3c65da51189aecbc1c344117573a7b229ec64606901327ecc0a35df06",
        "execution/service.py::ExecutionService.enable_auto": "3b0ea9c71fe041c7d870b07ecc72493f0112e65563e3e79d359996cff4532f07",
        "execution/service.py::ExecutionService.disable_auto": "50c1df4eaa55b2557290818ba1e12b2f2cc3c2f9dca7f6bcb80122a76b6205fe",
        "execution/service.py::stop_price_problem": "6ed950816c7bd4d6c6592c9331bb71542c181a362a8f40ea9bacedd02494f646",
        "execution/engine.py::exit_decision": "61a27b7aa0a57809fefe1ed183387384b5b7c05a6b212bb7679b77d302201a8f",
        "execution/engine.py::entry_controls": "daea54f6a9f86189834993eabfcac58ebe96edbf865ffd8ba88eee2f0929e7ce",
        "execution/engine.py::blocking": "7fae3bd49b0ec2bca4d62ac164b9279ab05ec05729ce0a6fd8c4f69fb828e415",
        "execution/engine.py::session_policy": "1092f637ad4a883196701ce094357653353acc5070ea009e3def5a8c964fd2db",
        "execution/engine.py::entry_quantity": "f0e2a0f4e8d2b98fcd6371e30aff71143ece2b15e3671070eaa77a3a11fab103",
        "execution/engine.py::sessions_held": "19941530e1d9d804fb14ea9040dd24621613810af18a4c296ba4e9c3dcaecfe1",
        "forward_validation/service.py::ForwardValidationService._early_stop": "c965276f153c0a68ba389203da6cc8c13bdb43b6fb16875b01342e6a6e390ca2",
        "forward_validation/attention_signature.py::signature": "ef5c28f1c9f626928e64024a7f797179ccf12967c911f4a4e581e06ad6e09ffd",
        "forward_validation/attention_signature.py::_duplicates": "3a4c822584ae000900fb3d51227488223485fc4d5b5f167e76581f063f3e6414",
        "forward_validation/attention_signature.py::_live_host": "1bfb59ae254faeff7b55ea60a17cdb0d918df9d2f708386a764b38e775d90a36",
        "forward_validation/attention_signature.py::_gate": "c1093f95e7bba8997a76a0cd21756f4388da742d1f10bdbaa211b21e63f6e2d8",
        "forward_validation/attention_signature.py::_persistent_mismatch": "f61ec3c94c5eb94fb5782e1767cdee064d74fac2e79bceb0816065f536efa6ab",
        "forward_validation/attention_signature.py::=HUMAN_ACTORS": "9017f60d64731465a2a894360ad90d61f642b89f31390272980ec1dd9e17e763",
        "forward_validation/attention_signature.py::=LIVE_HOST_TABLES": "77cbccb6e35b39e9adec3823e3ce7a378e192b9ac12952bebfc6bf8249cf86af",
        "forward_validation/attention_signature.py::=READERS": "d913c004b042826f9c6cc20b79ba45756d878cfa8d6ad306c3b5c39a92099ee4",
    },
}
BASE_W2B = "50cb2d7bc0384f1a020c01c9ba22c52c1283f339"                                   # main: Wave 2 PR A merged (PR #51); the PR B base
NEW_W2B = {'tests/test_wave2_prb.py', 'tests/test_wave2_prb_sessions.py', 'tests/wave2_prb_support.py'}   # Wave 2 PR B: its new files
EDITED_W2B = {'.github/ci/pytest_groups.json', 'browser_tests/flows.py', 'database/forward_validation_migrations.py', 'forward_capture/health.py', 'forward_validation/metrics.py', 'forward_validation/service.py', 'forward_validation/store.py', 'tests/freeze_ledger.py', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py', 'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_forward_paper_610a.py', 'tests/test_forward_validation_61.py', 'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py', 'tests/test_product_ui_611.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_sip_eval_78b1.py', 'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py'}   # the classifier, its CI entry, the earlier freeze tests' unions / views, the 6.1 / 6.10A rigs, the 6.1 harness flow, bookkeeping
SEMANTIC_W2B = {                                                                            # Wave 2 PR B: the INTENTIONAL behaviour change, file by file
    "database/forward_validation_migrations.py": "additive nullable sample and session columns (ALTER guarded by PRAGMA table_info), an at index and the three 3b session triggers",
    "forward_validation/store.py": "the four new sample columns written uncoerced, classifier_version / assessment_json writable while OPEN, insert_not_run with its assessment, strict and at-range reads",
    "forward_validation/metrics.py": "classify_session_v2 beside the unchanged v1 classify_session, with the literal G / H / K, EVIDENCE_READ_ATTEMPTS and CLASSIFIER_VERSION",
    "forward_validation/service.py": "session classifier v2: confirmed transitions, the post-close evidence grace with settle conditions, strict evidence reads, the clock-skew guard, rows for every observed day; D-5: unreadable evidence never counts toward the target or readiness",
    "forward_capture/health.py": "the AUTO_DISARM SESSION_ENDED exception at the close (its code set, trading date and time bounds); D-5: an EVIDENCE_UNREADABLE session record is INVALID evidence",
}
INSTRUMENTATION_W2B: dict = {}                                                             # Wave 2 PR B: no executor instrumentation
NEW_LEDGER_W2B = {'database/forward_validation_migrations.py', 'forward_validation/metrics.py', 'forward_validation/store.py'}   # main's files PR B registers (unchanged since BASE_68)
UNDO_W2B = {                                                                                 # Wave 2 PR B's in-place edits of older test files: (main, PR B)
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
        ('        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.now_text(rel)).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n',
         '        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.pre_w2b(rel, FL.now_text(rel))).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A\n',
         '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n",
         "            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n"),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n',
         'from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n'),
        ('        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A), rel   # Wave 1: its declared stop-check change, no universe wiring\n',
         '        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B), rel   # Wave 1: its declared stop-check change, no universe wiring\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                      # Stage 5.4.2, pinned by its own F tests\n',
         '    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                      # Stage 5.4.2, pinned by its own F tests\n'),
        ('            assert rel in FL.NEW_W2A or rel in FL.SEMANTIC_W2A or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n',
         '            assert rel in FL.NEW_W2A or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n',
         '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                         # Stage 7.0, pinned by its own F tests\n',
         '    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Stage 7.0, pinned by its own F tests\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('',
         'import wave2_prb_support as S2B  # noqa: E402 - Wave 2 PR B: the time-ordered minute cover of observed_day (design 8.5)\n'),
        ('    """A whole regular session observed at a 30-minute cadence (the coverage cadences set to match), then the post-close observation."""\n',
         '    """A whole regular session observed at a 30-minute cadence (the coverage cadences set to match), then the post-close observation.\n    Wave 2 PR B (design 8.5): the executor tables exist (W-01); between the 30-minute ticks a time-ordered minute-cadence cover of observer\n    samples with the variant\'s worker flag (W-05) keeps the Stage 6.1 v2 session observed; three settled post-close ticks (16:05 / 16:10 / 16:15)\n    confirm and finalize it before the 16:20 observation."""\n'),
        ('',
         '    r.x.store.ensure()\n'),
        ('',
         '        S2B.cover_until(r, r.clock.now + timedelta(minutes=step_min), loop_running=worker)\n'),
        ('',
         '    S2B.cover_until(r, CLOSE_AT, inclusive=True, loop_running=worker)\n    for k in (5, 10, 15):\n        r.clock.now = CLOSE_AT + timedelta(minutes=k)\n        r.x.run_cycle(r.clock.now, "TEST")\n        cycle(r)\n        obs(r)\n'),
        ('',
         '    obs(r, 60)\n    obs(r, 60)                                                                         # Wave 2 PR B: the third (confirmed) tick back-fills Thursday NOT_RUN\n'),
        ('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ('    assert not (changed - set(FL.SEMANTIC_W1)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n',
         '    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, now_text(rel))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, now_text(rel)))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_forward_validation_61.py': [
        ('from datetime import datetime, timezone\n',
         'from datetime import datetime, timedelta, timezone\n'),
        ('',
         'import wave2_prb_support as S2B  # noqa: E402 - Wave 2 PR B: the time-ordered cover and the worker status of the rig (design 8.4)\n'),
        ('',
         "    r.x.store.ensure()                                                # Wave 2 PR B (W-01): the executor tables exist, as the executor's startup makes them in production\n    S2B.worker_status(monkeypatch, running=True, interval_s=30)       # Wave 2 PR B (V-23): the worker status the samples persist (loop_running, stale_after_s 90)\n"),
        ('',
         '    r.cover_rows = 0\n'),
        ('    """The first observation after the close. The Stage 6.0.1 loop refreshes every five minutes outside the session, so the data is fresh then."""\n    r.clock.now = AFTER_CLOSE\n    cycle(r)\n    return obs(r)\n',
         '    """The post-close sequence (Wave 2 PR B, design 8.4): at 16:05, 16:10 and 16:15 ET an executor cycle, a refresh cycle (the Stage 6.0.1 loop\n    refreshes every five minutes outside the session), then the observer tick. The 16:05 tick is in the 360 s grace; the session is finalized\n    at the first confirmed, settled tick after 16:07 (16:10 after a full_day cover, else 16:15). Returns the last observation."""\n    for k in range(3):\n        r.clock.now = AFTER_CLOSE + timedelta(minutes=5 * k)\n        r.x.run_cycle(r.clock.now, "TEST")\n        cycle(r)\n        o = obs(r)\n    return o\n'),
        ('    """A complete observed session: fresh data, one closed paper trade, observations through the day, the post-close observation."""\n',
         '    """A complete observed session: fresh data, one closed paper trade, observations through the day (Wave 2 PR B: a time-ordered minute-cadence\n    cover of the rest of the regular session, design 8.4), the post-close sequence."""\n'),
        ('',
         '    S2B.cover_until(r, r.clock.now)\n'),
        ('',
         '    S2B.cover_until(r, datetime(2026, 10, 21, 20, 0, tzinfo=UTC), inclusive=True)\n'),
        ('    s0 = r.fv.store.samples(DATE)[0]\n',
         '    by_at = {s["at"]: s for s in r.fv.store.samples(DATE)}                # Wave 2 PR B: the real ticks among the design-8.4 minute cover\n    s0 = by_at[o0["at"]]\n'),
        ('    s1 = r.fv.store.samples(DATE)[1]\n',
         '    s1 = by_at[o1["at"]]\n'),
        ('    assert row["account_refresh_healthy"] and row["cash_flow_healthy"] and row["reconciliation_healthy"] and row["in_session_samples"] == 3 and row["ended_at"] == AFTER_CLOSE.isoformat(timespec="seconds")\n    assert row["bot_start_time"] == o0["at"] and row["bot_stop_time"] == o3["at"] and row["owners"] == [r.fv.owner]\n',
         '    closed_at = (AFTER_CLOSE + timedelta(minutes=5)).isoformat(timespec="seconds")    # v2: the 16:10 tick (confirmed by 300 s steps after the 16:00 cover sample, past C + h, settled)\n    assert row["account_refresh_healthy"] and row["cash_flow_healthy"] and row["reconciliation_healthy"] and row["in_session_samples"] == 3 and row["ended_at"] == closed_at\n    assert row["bot_start_time"] == "2026-10-21T13:30:00+00:00" and row["bot_stop_time"] == closed_at and row["owners"] == [r.fv.owner]   # v2: started at the first in-window sample (B-22)\n    assert row["classifier_version"] == "session_classifier_v2" and row["assessment"]["finalized_by"]["at"] == closed_at and row["assessment"]["finalized_late"] is False\n'),
        ('    assert ctl["observer"]["evaluation_count"] == 4 and ctl["session_recorder"]["fire_count"] == 2 and ctl["summary_builder"]["fire_count"] == 1 and ctl["attention_monitor"]["last_result"] == "PASS"\n',
         '    assert ctl["observer"]["evaluation_count"] == 6 and ctl["session_recorder"]["fire_count"] == 2 and ctl["summary_builder"]["fire_count"] == 1 and ctl["attention_monitor"]["last_result"] == "PASS"\n'),
        ('    obs(r)\n',
         '    obs(r)                                                                          # Wednesday: one (unconfirmed) sample - observed, no row yet (v2)\n'),
        ('    o = obs(r)\n',
         '    first, second, o = obs(r), obs(r, 60), obs(r, 60)                              # v2 (design 8.4): the third Monday tick is the first confirmed one\n    assert first["backfilled"] == [] and second["backfilled"] == []\n'),
        ('    old = r.fv.store.session(DATE)\n    assert old["session_status"] == "DEGRADED" and "CLOSE_NOT_OBSERVED" in old["notes"] and old["market_close_seen"] is False and old["ended_at"] == NEXT_MONDAY.isoformat(timespec="seconds")\n',
         '    old = r.fv.store.session(DATE)                                                  # accounted (B-24 (a)) and finalized late, unsettled (no executor cycle)\n    assert old["session_status"] == "DEGRADED" and "CLOSE_NOT_OBSERVED" in old["notes"] and old["market_close_seen"] is False and old["ended_at"] == o["at"]\n    assert {"OPEN_NOT_OBSERVED", "COVERAGE_LOW", "EVIDENCE_NOT_SETTLED"} <= set(old["notes"]) and old["assessment"]["finalized_late"] is True\n'),
        ('        assert nr["session_status"] == "NOT_RUN" and nr["notes"] == ["NO_OBSERVATION", "HOLIDAYS_NOT_MODELLED"] and nr["in_session_samples"] == 0\n',
         '        assert nr["session_status"] == "NOT_RUN" and nr["notes"] == ["NO_OBSERVATION"] and nr["in_session_samples"] == 0       # holidays are modelled now\n'),
        ('    o = obs(r)\n',
         '    obs(r)\n    obs(r, 60)\n    o = obs(r, 60)                                                    # v2 (design 8.4): the third pre-open tick confirms and opens the row (B-22)\n'),
        ('    assert r.fv.store.session(DATE)["session_status"] == "NOT_RUN" and r.fv.store.session(DATE)["notes"] == ["NO_OBSERVATION", "CLOSE_NOT_OBSERVED"]\n',
         '    obs(r, 60)\n    obs(r, 60)                                                        # the next day\'s third tick: the late finalize (unsettled: NOT_RUN kept, B-20)\n    row = r.fv.store.session(DATE)\n    assert row["session_status"] == "NOT_RUN" and row["notes"] == ["NO_OBSERVATION"] and row["assessment"]["degraded_codes"] == ["EVIDENCE_NOT_SETTLED"]\n'),
        ('    assert one["session"]["trading_date"] == DATE and one["daily"]["funnel"]["CLOSED"] == 1 and one["samples"] == 4\n',
         '    assert one["session"]["trading_date"] == DATE and one["daily"]["funnel"]["CLOSED"] == 1 and one["samples"] == 6 + r.cover_rows   # v2: 6 real ticks + the cover\n'),
        ('    assert len(api.get(f"/api/forward-validation/samples?date={DATE}").json()["samples"]) == 4 and api.get(f"/api/forward-validation/funnel/{DATE}").json()["counts"]["CLOSED"] == 1\n',
         '    assert len(api.get(f"/api/forward-validation/samples?date={DATE}").json()["samples"]) == min(6 + r.cover_rows, 500) and api.get(f"/api/forward-validation/funnel/{DATE}").json()["counts"]["CLOSED"] == 1\n'),
        ('    assert r.fv.store.counts()["forward_validation_samples"] == 4 and r.fv.store.counts()["forward_validation_sessions"] == 1\n',
         '    assert r.fv.store.counts()["forward_validation_samples"] == 6 + r.cover_rows and r.fv.store.counts()["forward_validation_sessions"] == 1   # v2: 6 real ticks + the cover\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # Stages 7.1-7.3: pinned by their own F tests\n',
         '    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # Stages 7.1-7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                           # Stages 7.3.1, 7.4: pinned by their own F tests\n',
         '    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                           # Stages 7.3.1, 7.4: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                         # Stage 7.5, pinned by its own F tests\n',
         '    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Stage 7.5, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                         # Stage 7.4, pinned by its own F tests\n',
         '    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Stage 7.4, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                           # Stages 7.3, 7.3.1: pinned by their own F tests\n',
         '    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                           # Stages 7.3, 7.3.1: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                             # Stages 7.2, 7.3: pinned by their own F tests\n',
         '    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                             # Stages 7.2, 7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n',
         '    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n'),
    ],
    'tests/test_product_ui_611.py': [
        ('    assert now_text("forward_validation/metrics.py") == blob("forward_validation/metrics.py") and now_text("forward_validation/__init__.py") == blob("forward_validation/__init__.py")\n',
         '    assert ledger_view("forward_validation/metrics.py", now_text("forward_validation/metrics.py")) == blob("forward_validation/metrics.py") and now_text("forward_validation/__init__.py") == blob("forward_validation/__init__.py")\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A\n',
         '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B\n'),
    ],
    'tests/test_sip_eval_78b1.py': [
        ('    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n',
         '    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n'),
    ],
    'tests/test_strategy_validation_62.py': [
        ('from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n',
         'from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n'),
        ('NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n',
         'NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n'),
        ('    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n',
         '    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                     # Stage 7.8B-1, pinned by its own F tests\n',
         '    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                     # Stage 7.8B-1, pinned by its own F tests\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, _now(rel))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, _now(rel)))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_wave1_safety_remediation.py': [
        ('    later = FL.NEW_W2A | FL.EDITED_W2A                                                         # Wave 2 PR A, pinned by its own F tests\n',
         '    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Wave 2 PR A, pinned by its own F tests\n'),
    ],
    'tests/test_wave2_pra.py': [
        ('',
         'def pra_files():\n    """Wave 2 PR B: PR A\'s own change set, between the two fixed commits (main before / after PR #51). PR B\'s delta: tests/test_wave2_prb.py."""\n    return set(_git("diff", "--name-only", FL.BASE_W2A, FL.BASE_W2B).split())\n\n\n'),
        ('    prod = {p for p in changed_files() if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json"}\n',
         '    prod = {p for p in pra_files() if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json"}\n'),
        ('        added = "\\n".join(l for l in now_text(rel).splitlines() if l not in base)\n',
         '        added = "\\n".join(l for l in FL.pre_w2b(rel, now_text(rel)).splitlines() if l not in base)\n'),
        ('    assert {"execution/engine.py", "execution/broker.py", "execution/exposure_gate.py", "config.py"}.isdisjoint(changed_files())\n    assert not {p for p in changed_files() if p.startswith(("safety/", "cash_flow/", "autorun/", "forward_capture/", "setups/", "proposals/", "frontend/"))}\n',
         '    assert {"execution/engine.py", "execution/broker.py", "execution/exposure_gate.py", "config.py"}.isdisjoint(pra_files())\n    assert not {p for p in pra_files() if p.startswith(("safety/", "cash_flow/", "autorun/", "forward_capture/", "setups/", "proposals/", "frontend/"))}\n'),
        ('        now, base = named_nodes(now_text(rel)), named_nodes(at_base(rel))\n',
         '        now, base = named_nodes(FL.pre_w2b(rel, now_text(rel))), named_nodes(at_base(rel))\n'),
        ('        u_now, u_base = unnamed(now_text(rel)), unnamed(at_base(rel))\n',
         '        u_now, u_base = unnamed(FL.pre_w2b(rel, now_text(rel))), unnamed(at_base(rel))\n'),
        ('        assert _normalized(rel, name, now_text(rel)) == ast.dump(base_node), (rel, name)\n',
         '        assert _normalized(rel, name, FL.pre_w2b(rel, now_text(rel))) == ast.dump(base_node), (rel, name)\n'),
        ('    assert changed == FL.NEW_W2A | FL.EDITED_W2A, {"unexpected": sorted(changed - FL.NEW_W2A - FL.EDITED_W2A), "missing": sorted((FL.NEW_W2A | FL.EDITED_W2A) - changed)}\n',
         '    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A)   # Wave 2 PR B: its own sets (its edits of PR A\'s new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py\n    assert changed == both_new | both_edited, {"unexpected": sorted(changed - both_new - both_edited), "missing": sorted((both_new | both_edited) - changed)}\n'),
        ('    assert {p for p in changed if p not in in_base} == FL.NEW_W2A and {p for p in changed if p in in_base} == FL.EDITED_W2A\n',
         '    assert {p for p in changed if p not in in_base} == both_new and {p for p in changed if p in in_base} == both_edited\n'),
        ('        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel\n',
         '        assert not [l for l in FL.pre_w2b(rel, now_text(rel)).splitlines() if l not in base and marker.match(l)], rel\n'),
        ('        text = now_text(rel)\n',
         '        text = FL.undo_w2b(rel, now_text(rel))                                                 # Wave 2 PR B: its own in-place edits undone first (its F03B)\n'),
        ('    fl = now_text("tests/freeze_ledger.py")\n',
         '    fl = subprocess.run(["git", "show", f"{FL.BASE_W2B}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n")   # Wave 2 PR B: the ledger as PR #51 left it\n'),
        ('    assert set(FL.LEDGER) - set(main_ledger) == NEW_LEDGER_W2A and set(main_ledger) <= set(FL.LEDGER)\n    assert {k for k in main_ledger if main_ledger[k] != FL.LEDGER[k]} == FL.EDITED_W2A & set(main_ledger)   # every re-hash is a declared PR A edit\n',
         '    pra_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # Wave 2 PR B: PR A\'s ledger; PR B\'s re-hashes are its F03B\n    assert set(pra_ledger) - set(main_ledger) == NEW_LEDGER_W2A and set(main_ledger) <= set(pra_ledger)\n    assert {k for k in main_ledger if main_ledger[k] != pra_ledger[k]} == FL.EDITED_W2A & set(main_ledger)   # every re-hash is a declared PR A edit\n'),
    ],
    'tests/test_wave2_pra_accounting.py': [
        ('',
         "import freeze_ledger as FL  # Wave 2 PR B (D-1): T-A42 reads the 6.1 / 6.10A tests on PR A's own delta\n"),
        ('    now61 = (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8")\n',
         '    now61 = FL.undo_w2b("tests/test_forward_validation_61.py", (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8"))   # Wave 2 PR B (D-1): PR A\'s delta; PR B\'s own edits are pinned by F02B / F03B\n'),
        ('    now610a, base610a = (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8"), at_base("tests/test_forward_paper_610a.py")\n',
         '    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8")), at_base("tests/test_forward_paper_610a.py")\n'),
    ],
}
SEMANTIC_TESTS_W2B = {                                                                       # Wave 2 PR B: every changed test function -> its finding (F02B)
    'tests/test_bot_activity_68.py::test_F01_every_change_since_main_is_a_new_file_or_an_exact_ledger_entry': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_bot_activity_68.py::test_F02_the_executor_and_scheduler_only_gained_display_bookkeeping_and_every_guard_line_is_kept': "V-01 / design 8.3 (c): the raw read goes through FL.pre_w2b, so the test still proves main's 50cb2d7b text; T-W05B pins PR B's delta",
    'tests/test_chart_empty_state_611fix.py::test_F01_the_fix_changes_exactly_the_declared_files_and_no_backend_file': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_decision_flow_610c.py::test_F01_stage_610c_changes_exactly_its_declared_files_and_no_execution_strategy_risk_or_policy_file': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_decision_flow_610c.py::test_F02_execution_setups_auto_run_and_the_live_coverage_are_byte_identical_and_only_the_router_and_three_inert_flags_were_added': "V-01 (e) / design 8.3 (b): a SEMANTIC_W2B file is a declared exemption beside SEMANTIC_W2A; PR B's own delta is pinned by T-W05B",
    'tests/test_dynamic_universe_610c.py::test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base': "V-01 (e) / design 8.3 (b): a SEMANTIC_W2B file is a declared exemption beside SEMANTIC_W2A; PR B's own delta is pinned by T-W05B",
    'tests/test_engine_health_610d.py::test_F01_the_change_set_is_exactly_the_declared_files_and_touches_no_execution_strategy_or_safety_file': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B; V-01 (e) / design 8.3 (b): a SEMANTIC_W2B file is a declared exemption beside SEMANTIC_W2A; PR B's own delta is pinned by T-W05B",
    'tests/test_evidence_gate_541.py::test_F01_the_fix_changes_one_function_and_nothing_in_the_setup_engine_ppe_evidence_or_execution': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B; V-01 (e) / design 8.3 (b): a SEMANTIC_W2B file is a declared exemption beside SEMANTIC_W2A; PR B's own delta is pinned by T-W05B",
    'tests/test_evidence_refresh_542.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_forward_paper_610a.py::observed_day': "W-01 / W-05 / design 8.5: the executor tables (store.ensure()), a time-ordered minute cover with the variant's worker flag, and the three settled post-close ticks",
    'tests/test_forward_paper_610a.py::test_F01_stage_610a_changes_exactly_its_declared_files_and_no_strategy_risk_broker_or_market_data_file': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B; V-01 (b) / design 8.3 (d): the 6.10A change-set ban subtracts SEMANTIC_W2B; T-S54 and T-W05B are the replacement pins for metrics.py",
    'tests/test_forward_paper_610a.py::test_F02_the_order_decision_logic_is_unchanged_and_the_executor_only_gained_failure_swallowing_markers': "V-01 / design 8.3 (c): the raw read goes through FL.pre_w2b, so the test still proves main's 50cb2d7b text; T-W05B pins PR B's delta",
    'tests/test_forward_paper_610a.py::test_H03_a_day_without_the_executor_loop_is_invalid_and_a_missed_market_day_is_recorded_not_skipped': 'W-05 / design 8.5: the Friday back-fill needs a confirmed tick (k = 3), so two more observations; its 6.10A assertions are kept',
    'tests/test_forward_validation_61.py::end_day': 'V-04 / design 8.4: three settled post-close ticks (16:05 / 16:10 / 16:15 ET: executor cycle, refresh, observe) confirm and finalize the v2 session',
    'tests/test_forward_validation_61.py::full_day': 'V-04 / design 8.4: a time-ordered minute-cadence in-window cover (cover_until) keeps the v2 session observed; its row count is kept exact',
    'tests/test_forward_validation_61.py::rig': "W-01 / V-23 / design 8.4: the rig creates the executor tables (store.ensure(), as the executor's startup) and persists the worker flag and threshold",
    'tests/test_forward_validation_61.py::test_N01_the_observer_never_orders_never_imports_a_broker_wire_and_writes_only_its_own_tables_plus_the_early_stop': "V-04 / design 8.4: the sample count becomes the helper's exact count; the token bans, one disable_auto( and post_count 2 are unchanged",
    'tests/test_forward_validation_61.py::test_S01_a_complete_session_is_recorded_and_classified_healthy_with_its_daily_summary': 'V-04 / design 8.4: HEALTHY through the denser full_day; the exact values (samples, ended_at, evaluation count, events) re-pinned from the construction',
    'tests/test_forward_validation_61.py::test_S02_an_unobserved_close_reads_degraded_and_unobserved_weekdays_are_backfilled_as_not_run': 'W-01 / design 8.4: re-derived on the ensure()d rig - Wednesday accounted late and DEGRADED, Thursday and Friday NOT_RUN, cumulative (1, 1, 2, 1)',
    'tests/test_forward_validation_61.py::test_S03_pre_market_only_observations_read_not_run_and_a_first_observation_after_the_close_opens_nothing': 'W-01 / design 8.4: re-derived - NOT_RUN with notes NO_OBSERVATION and EVIDENCE_NOT_SETTLED in the assessment; the r2 part is unchanged',
    'tests/test_forward_validation_61.py::test_X01_read_only_api_routes_shapes_and_route_words': "V-04 / design 8.4: session rows gain classifier_version and assessment; the sample counts become the helper's exact count",
    'tests/test_market_intel_70.py::test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_market_intel_collector_73.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_market_intel_quality_74.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_market_intel_schema_v2_731.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_market_intel_sources_72.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_market_intel_store_71.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_market_scan_75.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_product_ui_611.py::test_F02_strategy_execution_risk_and_broker_constants_and_the_validation_calculations_are_unchanged': "V-01 (c) / design 8.3 (e): metrics.py is read through ledger_view (a NEW_LEDGER_W2B entry), so the check still proves 50cb2d7b's text",
    'tests/test_robinhood_portfolio_69.py::test_F01_stage_69_changes_exactly_its_files_and_no_execution_strategy_or_risk_file': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_runtime_reliability_ops.py::test_F01_the_change_set_is_exactly_the_declared_ops_files_and_touches_no_trading_code': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_sip_eval_78b1.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_strategy_validation_62.py::=NEW_62': 'V-01 (d) / design 8.3 (f): NEW_62 gains NEW_W2B (its import and the F01 subtraction); it feeds 6.1 Z02, 6.1.1, 6.1.2 and the WAL fix',
    'tests/test_strategy_validation_62.py::test_F01_main_plus_only_the_stage_62_files_and_exact_edits': 'V-01 (d) / design 8.3 (f): NEW_62 gains NEW_W2B (its import and the F01 subtraction); it feeds 6.1 Z02, 6.1.1, 6.1.2 and the WAL fix',
    'tests/test_swing_pullback_78.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_unified_exposure_gate_691.py::test_F01_stage_691_changes_exactly_its_declared_files_and_no_risk_strategy_broker_or_database_file': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_unified_exposure_gate_691.py::test_F02_the_execution_semantics_changed_on_purpose_and_exactly_as_declared': "V-01 / design 8.3 (c): the raw read goes through FL.pre_w2b, so the test still proves main's 50cb2d7b text; T-W05B pins PR B's delta",
    'tests/test_wave1_safety_remediation.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_market_data_broker_or_research_file_changed': "U-01 / design 8.3 (a): the change-set union gains NEW_W2B / EDITED_W2B, every earlier set kept; PR B's own sets are pinned by F01B",
    'tests/test_wave2_pra.py::pra_files': "U-01 / design 8.1: PR A's own change set between the fixed commits BASE_W2A and BASE_W2B, read by T-W04's set checks",
    'tests/test_wave2_pra.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': 'U-01 / design 8.1: the exact-set rule over both PRs (new = NEW_W2A | NEW_W2B, edited = EDITED_W2A | (EDITED_W2B - NEW_W2A)); marked blocks read pre_w2b',
    'tests/test_wave2_pra.py::test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': 'U-01 / design 8.1: REPLAY_W2A is replayed on FL.undo_w2b (F03B), the ledger of 50cb2d7b is rebuilt and compared; the view() loop is unchanged',
    'tests/test_wave2_pra.py::test_TW04_the_production_diff_is_exactly_pr_a_and_names_no_pr_c_object': "U-01 / design 8.1: T-W04's three set checks are evaluated on PR A's delta (pra_files) and its token ban on pre_w2b text; PR B's T-W04B pins the rest",
    'tests/test_wave2_pra.py::test_TW05_only_the_listed_functions_changed_and_only_the_listed_names_are_new': "U-01 / design 8.1: T-W05's three reads take FL.pre_w2b, so PR A's closed list is proven on PR A's text; PR B's T-W05B proves design 7",
    'tests/test_wave2_pra_accounting.py::test_TA42_downstream_compatibility_capture_keeps_raw_values_and_the_known_consumers_are_unchanged': "U-01 / D-1 (design 8.1): T-A42 (ii) reads the 6.1 / 6.10A tests through FL.undo_w2b, so 'PR A changed none of them' stays pinned exactly on PR A's delta; PR B's own changes are pinned by F02B / F03B",
}
LEDGER = {
    ".github/ci/pytest_groups.json": "06042ce712fee67d0834174c11c6d20e0b926f86b84d52f492ff3ec04e923db1",
    ".gitignore": "d98fa877f978452439453e434c70117f472256f0da4b4f12cbe79042458ce909",
    "api/routes/account_refresh.py": "26f4ebfa3059e2d7f24fe608a8d4f0a6c582daaa237828920e561bbcc4891319",
    "api/routes/execution.py": "1bf1b7ecb079ecffe1e44141405c747e6deb18053cf527de35d628a313a930b2",
    "api/routes/forward_validation.py": "3d6bb8824338b53c72af3ae03ac911cef1958771500b07d1ad108ad8b7d39afd",
    "api/routes/ops_status.py": "5f80dc548368809b88e47a329f51dbadbb81e9dede47ff073e681924ae0b5ec2",
    "api/server.py": "8af3d1f26026023dc3bd704eaafb22533d191a49b1f233ab8edf395e71bc1ea8",
    "browser_tests/flows.py": "fb9491405ab3b7e6c227e75ac6cb072828b85e3542ce8d5904cd398116b093e7",
    "config.py": "339a99b635146bb07c182a554e5424a4953dbcdb00bab57b39fdd676bfa306ec",
    "database/forward_validation_migrations.py": "98ef4bc528c8f62481944f5c507048728ce6b5e10be4cd978ea570b26ace24ab",
    "evidence/sources/sec_edgar.py": "ede7c133a055785fc1c4579c823bc85dbf6e02856da7d1d9257a0e90822c08db",
    "execution/__init__.py": "59d68cae075c2b843083a7dd9400b86b01d3ce73b33247aaaa228de54237f296",
    "execution/engine.py": "06e898d9e075ad766c247ed095806c49d994f413ecc056935e5fb9a3dbdd084c",
    "execution/models.py": "177a81a89604583cfffc9254da31227c1f35fb98e376dbfdeca9448128c1bfa8",
    "execution/scheduler.py": "6d8459eecb230fa52193c24a6fd0aade17de2959162aefb43010f3da61f57183",
    "execution/service.py": "0a9fceea9e5b26cdb62aeb3d0af3a91837062eb34720fc6f50a17ce4c846d9ca",
    "execution/store.py": "e2bc106565703da3a9af938556fa841daa777f0beab97a215f6f858f24ec50c4",
    "forward_validation/metrics.py": "7e302d5c5ae5444a65f8030626e80ad6d61b48d02453eec60c4d1e29397eecab",
    "forward_validation/reports.py": "140b04b1fe7693092b31177c2210e9390f8293c087138a052a93ed9e47bfe655",
    "forward_validation/service.py": "ec6f9e25b86d4fe7e2a976aa0e8d9e9c95b52b031333a527d9f0b9eacd8056ef",
    "forward_validation/store.py": "aad3d3286a93d4e73330559b64cfbffbf45f9753dd86baddf62715601dba66c8",
    "frontend/product_ui.css": "7ccb310cc903b95740ba84f9a76a97766640f7f5c1d3ac2747575f061b2f356a",
    "frontend/product_ui.js": "5238523c099f374ee41cb1407cee1bf775681775af77b550626f179be298f72d",
    "frontend/trader_view.js": "cc83857e686c92ea04235c86954c96ea505073381d31664af26d1f18c356bebd",
    "safety/service.py": "e4403dd18459322a7fa26db7f360f0910eaf3a50f5ff40c661303a2cb9488213",
    "safety/store.py": "7e7635aae90686fd1c15ea6185fcf6fcc7903966cd16c8af9b741bea84a9d0c6",
    "scripts/check_stock_agent.ps1": "f4dab15027feb15457f2107e04702de4e5bd595b45d570eb74f8de5768c4a677",
    "scripts/install_stock_agent_task.ps1": "fef37c2a6da6fb90539ee32c577036a701eb04ad7ac5e16baa5454eb3d98718e",
    "scripts/run_stock_agent.py": "1090690982d115e1f268738ebdb2b797e4f79b88be2c5d58e5f042010ff710c3",
    "tests/conftest.py": "8d37bedc3b2b8085836424f35b352a7c839b2378e7c718ca9a823e47079a140d",
    "tests/frontend/product_flow.test.cjs": "5c4afa63a3510be05030dfce4b72d29f0eab2757a2be1f6ee8ccc593ad58229e",
    "tests/frontend/product_ui.test.cjs": "6d33db33f0cad805ff76ae8557c134ca1ed1bf98b20eeb826f111cd2e34159d7",
    "tests/frontend/strategy_validation.test.cjs": "fce1fdf90474dbeeb8881a6469d9d8bb0a0b6ad612455eea099cc5611e92a1e4",
    "tests/test_account_refresh_601.py": "ff47d14b3679e2e215db67968232414eab621cb97d7e5f17d9653772df318367",
    "tests/test_automation_37.py": "fd6421efebe4c6ea60920b8efeab64e5fb25a06991d22db6cf51a9a0510cf3db",
    "tests/test_cash_flow_602.py": "15581c69f6fe12e475f7311a03652bb0e128e06eaaa7bc227eab6281eae141ec",
    "tests/test_ci_groups_603.py": "8b1b0a4659424e7fc733a275dae76e3b72ac3f7df156234a8bf8c72f3e57c477",
    "tests/test_db_bootstrap_busy.py": "0253939a872b26f83dd479c31432adc75c5ba7000d3842a84dcc8d3717b38d86",
    "tests/test_demand_57.py": "fa6319bbc49a5770c52b6647973a3376df40d08df26e4ee9a21356b17eb981c9",
    "tests/test_execution_60.py": "ebc8b8aef3e07dc0b5db390bfc805476619577e8271ef609160a15dc7c9e7387",
    "tests/test_execution_safety_591.py": "7b8eb537ecf3cf3b554e4e983e3e4d8c5d3c318d38c5c0d74db14d1fb77beef2",
    "tests/test_forward_validation_61.py": "10cd174b60efd94c079fd81b1c0e331f3f2e02adb516c199c191a62fea081cba",
    "tests/test_history_budget_39.py": "97fb1fc1432b815a1a8b1c73ceff00d8728d5acde4aff9a16c75a6cd55babdd9",
    "tests/test_product_flow_612.py": "28f95d614292bc8f8f98aaf2e0b40da0427fd8aefc45a9b45a0a91e0c486f4f7",
    "tests/test_product_ui_611.py": "bfee639c228f6629d7c73270b3d8da630588cb4ef16a8f52b6c6edf2b9b04d89",
    "tests/test_proposals_59.py": "8ee1f4e30ae1e7044865b75ad0a8f53548f8941e5bed2777aa98e08b1e502883",
    "tests/test_regression_isolation.py": "f596017894f68ced030b1d4de720b201150b9637d390c2211347f9bb7cb15b83",
    "tests/test_setups_58.py": "3d89c910788e9ebf64c6ff97fb02d1932e77d24c0b7867ffa69b8b59f73684ea",
    "tests/test_strategy_validation_62.py": "252c6ead518776f880f0bf38293ab9d68f96caed1ada79e315397c9f38f0c449",
    "tests/test_trader_view_55.py": "b90299119e4f2ab2790fb514149900ad841e662057027ee4c010b3b551504846",
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


def undo_w1(rel: str, text: str) -> str:
    """`text` (a current file) with Wave 1's exact in-place edits undone (last first): config.py / tests/conftest.py as on main BASE_W1, for an older
    freeze test that reads them raw. Any other file passes through. tests/test_wave1_safety_remediation.py (F02) proves the result is main, byte for byte."""
    text = _norm(text)
    for old, new in reversed(UNDO_W1.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text


def pre_w2a(rel: str, text: str) -> str:
    """`text` (a current file) as main left it at BASE_W2A when Wave 2 PR A changed it on purpose (SEMANTIC_W2A): an older freeze test that
    checks "every line kept" / "byte-identical" reads main's text, where it passed. tests/test_wave2_pra.py pins the delta from that text exactly
    (the closed function list, the version pins, the decision golden). Any other file passes through."""
    if rel not in SEMANTIC_W2A:
        return text
    out = subprocess.run(["git", "show", f"{BASE_W2A}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_W2A} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def pre_w2b(rel: str, text: str) -> str:
    """`text` (a current file) as main left it at BASE_W2B when Wave 2 PR B changed it on purpose (SEMANTIC_W2B): an older freeze test reads
    main's text, where it passed. tests/test_wave2_prb.py pins the delta from that text (the closed function list). Any other file passes through."""
    if rel not in SEMANTIC_W2B:
        return text
    out = subprocess.run(["git", "show", f"{BASE_W2B}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_W2B} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_w2b(rel: str, text: str) -> str:
    """`text` (a current test file) with Wave 2 PR B's exact in-place edits undone (last first): as on main BASE_W2B. Any other file passes
    through. tests/test_wave2_prb.py (F03B) proves the result is main, byte for byte."""
    text = _norm(text)
    for old, new in reversed(UNDO_W2B.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text


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
