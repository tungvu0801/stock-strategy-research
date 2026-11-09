"""
tests/freeze_ledger.py - the Stage 6.8 post-freeze ledger (account position visibility + Bot Activity; DESIGN_68 section 6).

The earlier freeze tests pin execution, Trader View, route and UI files to their own base commits. Stage 6.8 changes some of those files on
purpose. Instead of loosening any freeze test, every such file is registered here with the SHA-256 of the exact text Stage 6.8 leaves, and
the freeze tests read it through `view()`: a registered file whose text matches its hash is seen exactly as it was on main (BASE_68), where
every freeze test passed. Any other change - or any later edit of a registered file without updating its hash - still fails, and
tests/test_bot_activity_68.py::test_F01 proves that the change set since main is exactly NEW_68 + NEW_69 + NEW_691 + NEW_610A + NEW_610C + NEW_541 + NEW_611FIX + NEW_OPS + NEW_610D + NEW_542 + NEW_70 + NEW_71 + NEW_72 + NEW_73 + NEW_731 + NEW_74 + NEW_75 + NEW_78 + NEW_78B1 + NEW_W1 + NEW_W2A + NEW_W2B + NEW_W2C + NEW_EX06 + NEW_XSS01 plus this ledger.
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

Wave 2 PR C (design Revision 4.3 section 14) changes the forward run, membership, readiness, the bot-only drawdown, verified-only P/L
display and the W2-6 duplicate attribution on purpose. SEMANTIC_W2C names its production files; pre_w2c() gives an
older freeze test main's text of them (BASE_W2C); UNDO_W2C undoes PR C's exact in-place edits of older test files; SEMANTIC_TESTS_W2C declares
every changed test function; VERSION_PINS_W2A gains execution_v3. tests/test_wave2_prc.py pins it (BASE_W2C / NEW_W2C / EDITED_W2C, the closed
function list).

EX-06 (the local request gate; DESIGN_EX06_LOCAL_REQUEST_GATE.md) adds api/local_gate.py, the outermost ASGI middleware that refuses any request or
WebSocket handshake not from the Stock Agent dashboard (or a local tool) on this computer: loopback client, loopback Host, no forwarding header,
an allowed Origin / Referer, browser fetch metadata, the X-Stock-Agent-Intent header on every /api request and state change. It is NOT
authentication. No execution, safety, strategy, risk, broker, config or route file changes: SEMANTIC_EX06 names the files that wire it (the
server, the dashboard's fetch wrapper, the harness helper, the three local HTTP callers); pre_ex06() gives an older freeze test main's text of
them (BASE_EX06); NEW_LEDGER_EX06 registers the three that existed unchanged at BASE_68; UNDO_EX06 undoes EX-06's exact in-place edits of older
test files (the conftest test client, the runner probe, the bookkeeping); SEMANTIC_TESTS_EX06 declares every changed test function.
tests/test_ex06_freeze.py pins it (BASE_EX06 / NEW_EX06 / EDITED_EX06); tests/test_ex06_local_gate.py pins the behaviour.

XSS-01 (the dashboard's HTML insertion paths; XSS01_REMEDIATION_DESIGN.md) escapes every string frontend/app.js inserts into innerHTML, turns an
external URL into a link only when it is an absolute http(s) URL (safeHref / extLink, also in market.js, command_center.js and
evidence_research.js), gathers app.js's HTML builders into one pure, sandbox-testable block, and extends the gate's security headers with a
Content Security Policy (script-src 'self', object-src 'none', base-uri 'none', form-action 'self', frame-src 'none') and nosniff. No execution,
safety, strategy, risk, broker, config, route or database change: SEMANTIC_XSS01 names the five files it changes on purpose; pre_xss01() gives
an older freeze test main's text of them (BASE_XSS01); NEW_LEDGER_XSS01 registers the three X-1 modules, unchanged at BASE_68; it re-hashes frontend/app.js, the CI manifest and the
two registered older tests it bookkeeps; UNDO_XSS01 undoes its exact in-place edits of older test files (the unions / views, two header
assertions); SEMANTIC_TESTS_XSS01 declares every
changed test function. tests/test_xss01_freeze.py pins it (BASE_XSS01 / NEW_XSS01 / EDITED_XSS01); tests/test_xss01_csp.py and
tests/frontend/xss01_app_js.test.cjs pin the behaviour.
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
BASE_W2C = "875bc06dd08b36ace1d3930877f90cb96ff81b08"                                   # main: Wave 2 PR B merged (PR #52); the PR C base
NEW_W2C = {'forward_validation/pnl_view.py', 'forward_validation/run_admin.py', 'tests/frontend/wave2_prc.test.cjs', 'tests/test_wave2_prc.py', 'tests/test_wave2_prc_drawdown.py', 'tests/test_wave2_prc_evidence.py', 'tests/test_wave2_prc_pnl.py', 'tests/test_wave2_prc_run.py', 'tests/wave2_prc_support.py'}   # Wave 2 PR C: its new files
EDITED_W2C = {'.github/ci/pytest_groups.json', 'api/routes/execution.py', 'api/routes/forward_validation.py', 'database/forward_validation_migrations.py', 'execution/__init__.py', 'execution/service.py', 'forward_capture/view.py', 'forward_validation/metrics.py', 'forward_validation/reports.py', 'forward_validation/service.py', 'forward_validation/store.py', 'frontend/forward_validation.js', 'frontend/paper_bot.js', 'frontend/product_ui.js', 'strategy_registry/downside_recovery.py', 'strategy_registry/service.py', 'tests/freeze_ledger.py', 'tests/frontend/forward_validation.test.cjs', 'tests/frontend/product_flow.test.cjs', 'tests/frontend/product_ui.test.cjs', 'tests/frontend/trader_bot.test.cjs', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py', 'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_forward_paper_610a.py', 'tests/test_forward_validation_61.py', 'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py', 'tests/test_product_flow_612.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_sip_eval_78b1.py', 'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_pra_early_stop.py', 'tests/test_wave2_prb.py', 'tests/test_wave2_prb_sessions.py'}   # the production files of design 1.3, the CI entry, the older freeze tests' unions / views, the PR A / PR B pins, literals, bookkeeping
SEMANTIC_W2C = {                                                                            # Wave 2 PR C: the INTENTIONAL behaviour change, file by file
    'api/routes/execution.py': 'the five read routes decorate executor rows with the verified-only P/L status through forward_validation.pnl_view (design 12)',
    'api/routes/forward_validation.py': "the overview's run fields, the export's run columns and the read-only GET /api/forward-validation/run",
    'database/forward_validation_migrations.py': 'the additive forward_runs / forward_run_events tables with their triggers and the nullable frozen_hash / run_id sample columns',
    'execution/__init__.py': 'execution.VERSION execution_v2 -> execution_v3 (the _reconcile and _early_stop VERSION blocks; a new 6.10A cohort)',
    'execution/service.py': '_reconcile only: the W2-6 MULTIPLE_ORDERS finding detail (additive keys); no order, sizing, arming or permission path',
    'forward_capture/view.py': 'the 6.10A evidence view shows the run membership and the run-cohort label beside the 6.10A label',
    'forward_validation/metrics.py': 'edge_status on the run block, price_age_unknown and bot_drawdown_pct; CLASSIFIER_VERSION session_classifier_v2_1 (C-65a)',
    'forward_validation/reports.py': 'verified-only P/L text and the run columns of the exports and the daily / weekly reports (C-84c)',
    'forward_validation/service.py': 'the forward run: sample stamps, the suspension check, membership, the strategy day, run readiness beside the all-time block, the bot-only drawdown, W2-6; L-b: an unresolved unrelated manual pair stays visible as informational attention',
    'forward_validation/store.py': 'the run stamps in SAMPLE_COLS and add_sample, the run rows and events (insert, end, read) and strict_view (C-07)',
    'frontend/forward_validation.js': 'the run header, banner and cohort rows; the summary / headline / trade / daily rows from the run block (W2-7)',
    'frontend/paper_bot.js': 'verified-only P/L rendering of the executor card and history lines (P/L unknown when undecorated)',
    'frontend/product_ui.js': 'verified-only P/L rendering of the today summary, daily review, activity, lifecycle, closed list, timeline and position model',
    'strategy_registry/downside_recovery.py': '_paper_results reads verified-only run P/L for the registry (C-34)',
    'strategy_registry/service.py': 'derive_lifecycle and runtime_facts read the run block and promotion eligibility (C-59); APPROVED is never derived',
}
INSTRUMENTATION_W2C: dict = {}                                                             # Wave 2 PR C: no executor instrumentation
NEW_LEDGER_W2C: set = {'frontend/forward_validation.js', 'frontend/paper_bot.js', 'strategy_registry/downside_recovery.py', 'strategy_registry/service.py', 'tests/frontend/forward_validation.test.cjs', 'tests/frontend/trader_bot.test.cjs'}   # main's files PR C registers (unchanged since BASE_68)
UNDO_W2C = {                                                                                 # Wave 2 PR C's in-place edits of older test files: (main, PR C)
    'tests/frontend/forward_validation.test.cjs': [
        ('  for (const s of ["Win / loss / flat 2 / 1 / 0", "Win rate 66.67 %", "Expectancy per trade (descriptive) +13.73", "Max drawdown (adjusted equity) +120.00 (0.12 %)", "Safety fires 3", "Restarts observed 1",\n',
         '  for (const s of ["Win / loss / flat 2 / 1 / 0", "Win rate 66.67 %", "Expectancy per trade (descriptive) +13.73", "Max drawdown (adjusted equity · informational) +120.00 (0.12 %)", "Safety fires 3", "Restarts observed 1",   // Wave 2 PR C: C-81 labels the account curve informational (the drawdown tile is the run\'s)\n'),
        ('  assert.equal(FV.headline(FV.summaryModel({ ...BASE, sessions: { ...BASE.sessions, completed: 30 } })).text, "Validation target reached");\n',
         '  assert.equal(FV.headline(FV.summaryModel({ ...BASE, sessions: { ...BASE.sessions, completed: 30 }, run: { sessions: { target_met: true }, readiness: { status: "READY" } } })).text, "Validation target reached");   // Wave 2 PR C: C-85 needs the run\'s target_met and run READY\n'),
        ('  const pos = FV.summaryModel({ ...BASE, strategy: { ...BASE.strategy, insufficient: false, edge_status: "EARLY_POSITIVE", edge_label: "Early positive" } });\n',
         '  const pos = FV.summaryModel({ ...BASE, strategy: { ...BASE.strategy, insufficient: false, edge_status: "EARLY_POSITIVE", edge_label: "Early positive" }, run: { strategy: { edge_status: "EARLY_POSITIVE", edge_label: "Early positive" } } });   // Wave 2 PR C: the edge label is the run block\'s (design 7.2)\n'),
    ],
    'tests/frontend/product_flow.test.cjs': [
        ('const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "3 shares · entry 61.20 · now 62.10 · +2.70", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1", state: "POSITION_OPEN", details: { sessions_held: 2 } };\n',
         'const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "3 shares · entry 61.20 · now 62.10 · +2.70", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1", state: "POSITION_OPEN", details: { sessions_held: 2 }, pnl_status: "VERIFIED", pnl_display: "2.70", pnl_kind: "unrealized" };   // Wave 2 PR C: a verified (route-decorated) card\n'),
        ('              positions: [{ position_id: "p1", symbol: "AAPL", state: "CLOSED", closed_at: "2026-10-21T19:50:00Z", realized_pnl: "4.20", entry_qty: 2, average_entry_price: "227.10", average_exit_price: "229.20", exit_reason: "TIME_EXIT", sessions_held: 5 }],\n',
         '              positions: [{ position_id: "p1", symbol: "AAPL", state: "CLOSED", closed_at: "2026-10-21T19:50:00Z", realized_pnl: "4.20", entry_qty: 2, average_entry_price: "227.10", average_exit_price: "229.20", exit_reason: "TIME_EXIT", sessions_held: 5, pnl_status: "VERIFIED", pnl_display: "4.20", pnl_kind: "realized" }],   // Wave 2 PR C: a verified (route-decorated) closed position\n'),
        ('             strategy: { closed_trades: 3, edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", insufficient: true, insufficient_text: "INSUFFICIENT FOR STRATEGY CONCLUSION", gross_pnl: "41.20" }, attention: [], needs_attention: false };\n',
         '             strategy: { closed_trades: 3, edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", insufficient: true, insufficient_text: "INSUFFICIENT FOR STRATEGY CONCLUSION", gross_pnl: "41.20" }, attention: [], needs_attention: false, today_summary: { trading_date: "2026-10-21", realized_pnl: "4.20", pnl_status: "OK", unverified: 0, closed_trades: 1 } };   // Wave 2 PR C: the server\'s verified-only day total (C-71)\n'),
    ],
    'tests/frontend/product_ui.test.cjs': [
        ('const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "3 shares · entry 61.20 · now 62.10 · +2.70", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1",\n',
         'const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "3 shares · entry 61.20 · now 62.10 · +2.70", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1", pnl_status: "VERIFIED", pnl_display: "2.70", pnl_kind: "unrealized",   // Wave 2 PR C: a verified (route-decorated) card\n'),
        ('const MU = { key: "IN_TRADE", symbol: "MU", label: "In trade", line: "4 shares · entry 830.00 · now 835.50 · +22.00", shares: 4, entry: "830.00", current: "835.50", pnl: "+22.00", pnl_pct: "0.66", position_id: "p2",\n',
         'const MU = { key: "IN_TRADE", symbol: "MU", label: "In trade", line: "4 shares · entry 830.00 · now 835.50 · +22.00", shares: 4, entry: "830.00", current: "835.50", pnl: "+22.00", pnl_pct: "0.66", position_id: "p2", pnl_status: "VERIFIED", pnl_display: "22.00", pnl_kind: "unrealized",   // Wave 2 PR C: a verified (route-decorated) card\n'),
        ('                                { position_id: "p1", kind: "CLOSED", at: "2026-10-21T19:50:00Z", detail: { realized_pnl: "4.20" } }, { position_id: "p1", kind: "FILL_UPDATED", at: "2026-10-21T14:36:06Z" }],\n',
         '                                { position_id: "p1", kind: "CLOSED", at: "2026-10-21T19:50:00Z", detail: { realized_pnl: "4.20" }, pnl_status: "VERIFIED", pnl_display: "4.20", pnl_kind: "realized" }, { position_id: "p1", kind: "FILL_UPDATED", at: "2026-10-21T14:36:06Z" }],   // Wave 2 PR C: a verified (route-decorated) CLOSED event\n'),
        ('             attention: [], needs_attention: false };\n',
         '             attention: [], needs_attention: false, today_summary: { trading_date: "2026-10-21", realized_pnl: "4.20", pnl_status: "OK", unverified: 0, closed_trades: 1 } };   // Wave 2 PR C: the server\'s verified-only day total (C-70)\n'),
        ('  assert.deepEqual(PUI.todaySummary(SRC, { ...EXEC, open_positions: 1, attention_reasons: ["RECONCILIATION_REQUIRED"] }, "2026-10-21"), { trades: 1, open: 1, closed: 1, pnl: "+4.20", issues: 2 });\n',
         '  assert.deepEqual(PUI.todaySummary(SRC, { ...EXEC, open_positions: 1, attention_reasons: ["RECONCILIATION_REQUIRED"] }, "2026-10-21", FV), { trades: 1, open: 1, closed: 1, pnl: "+4.20", issues: 2 });   // Wave 2 PR C: the day total is the server\'s today_summary (C-70), passed as the new trailing fv\n'),
    ],
    'tests/frontend/trader_bot.test.cjs': [
        ('const TRADE = { kind: "TRADE", symbol: "SNDK", at: "2026-10-21T15:03:00+00:00", shares: 7, entry: "1325.50", exit: "1310.00", pnl: "-108.50", reason: "Price reached the invalidation level", reason_code: "HARD_INVALIDATION" };\n',
         'const TRADE = { kind: "TRADE", symbol: "SNDK", at: "2026-10-21T15:03:00+00:00", shares: 7, entry: "1325.50", exit: "1310.00", pnl: "-108.50", reason: "Price reached the invalidation level", reason_code: "HARD_INVALIDATION", pnl_status: "VERIFIED", pnl_display: "-108.50", pnl_kind: "realized" };   // Wave 2 PR C: a verified (route-decorated) history row; an undecorated one reads "P/L unknown"\n'),
        ('                         execution_id: "69e4050e43bb97c1ff4ca710aad3f6fd", position_id: "f47d95810e336b9e64f0129336b99495", shares: 7, entry: "1325.50", current: "1330.00", pnl: "31.50", pnl_pct: "0.34",\n',
         '                         execution_id: "69e4050e43bb97c1ff4ca710aad3f6fd", position_id: "f47d95810e336b9e64f0129336b99495", shares: 7, entry: "1325.50", current: "1330.00", pnl: "31.50", pnl_pct: "0.34", pnl_status: "VERIFIED", pnl_display: "31.50", pnl_kind: "unrealized",   // Wave 2 PR C: a verified (route-decorated) card; an undecorated one reads "P/L unknown"\n'),
        ('  const cl = text(P.cardHtml(card({ key: "CLOSED", icon: "⚪", label: "Closed", line: "Trade completed · -108.50 · Price reached the invalidation level", current: "1310.00", pnl: "-108.50", pnl_pct: null, state: "CLOSED" }), {}));\n',
         '  const cl = text(P.cardHtml(card({ key: "CLOSED", icon: "⚪", label: "Closed", line: "Trade completed · -108.50 · Price reached the invalidation level", current: "1310.00", pnl: "-108.50", pnl_pct: null, state: "CLOSED", pnl_display: "-108.50", pnl_kind: "realized" }), {}));   // Wave 2 PR C: the verified closed card\'s decoration\n'),
    ],
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
        ('        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.pre_w2b(rel, FL.now_text(rel))).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n',
         '        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.now_text(rel)))).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B\n',
         '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n",
         "            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n"),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n',
         'from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B, SEMANTIC_W2C  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n'),
        ('        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B), rel   # Wave 1: its declared stop-check change, no universe wiring\n',
         '        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B or rel in SEMANTIC_W2C), rel   # Wave 1: its declared stop-check change, no universe wiring\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                      # Stage 5.4.2, pinned by its own F tests\n',
         '    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                      # Stage 5.4.2, pinned by its own F tests\n'),
        ('            assert rel in FL.NEW_W2A or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n',
         '            assert rel in FL.NEW_W2A or rel in FL.NEW_W2C or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n'),
        ('    js = (ROOT / "frontend/product_ui.js").read_text(encoding="utf-8").replace("\\r\\n", "\\n")\n',
         '    js = FL.pre_w2c("frontend/product_ui.js", (ROOT / "frontend/product_ui.js").read_text(encoding="utf-8").replace("\\r\\n", "\\n"))   # Wave 2 PR C: main\'s text; its W2-7 edit: T-W05C\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n',
         '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Stage 7.0, pinned by its own F tests\n',
         '    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Stage 7.0, pinned by its own F tests\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    assert any("healthy session(s) of the 30 needed" in x for x in row["reasons"])\n',
         '    assert row["reasons"] == ["not a forward run"] and row["forward_run"] is False and row["expectancy"] is None   # Wave 2 PR C (C-80): no P/L edge outside a run\n'),
        ('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ('    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n',
         '    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B) - set(FL.SEMANTIC_W2C)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, now_text(rel)))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel))))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_forward_validation_61.py': [
        ('    assert row["classifier_version"] == "session_classifier_v2" and row["assessment"]["finalized_by"]["at"] == closed_at and row["assessment"]["finalized_late"] is False\n',
         '    assert row["classifier_version"] == "session_classifier_v2_1" and row["assessment"]["finalized_by"]["at"] == closed_at and row["assessment"]["finalized_late"] is False\n'),
        ('    assert "Not yet available: 1 closed session(s) of the 5 needed" in weekly\n',
         '    assert "Not yet available: 0 closed member session(s) of the forward run of the 5 needed for a weekly aggregate (no forward run defined)." in weekly   # Wave 2 PR C (C-88)\n'),
        ('',
         '    cum = {**cum, "run": {**cum["run"], "defined": True, "sessions": {**cum["run"]["sessions"], "member_dates": [x["trading_date"] for x in dailies]}}}   # Wave 2 PR C (C-88): a run whose members are the five days\n'),
        ('    before = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE \'%forward_validation%\'"))\n',
         '    before = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE \'%forward_validation%\' AND tbl_name NOT IN (\'forward_runs\', \'forward_run_events\')"))   # Wave 2 PR C: its run tables\n'),
        ('    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE \'%forward_validation%\'")) == before and conn.execute("PRAGMA user_version").fetchone()[0] == 7\n',
         '    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE \'%forward_validation%\' AND tbl_name NOT IN (\'forward_runs\', \'forward_run_events\')")) == before and conn.execute("PRAGMA user_version").fetchone()[0] == 7\n    run_objects = lambda: sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name IN (\'forward_runs\', \'forward_run_events\')"))  # noqa: E731 - Wave 2 PR C [Q43-6]\n    first = run_objects()\n    run_forward_validation_migrations(conn)\n    assert run_objects() == first and {r[1] for r in first if r[0] == "table"} == {"forward_runs", "forward_run_events"}   # Wave 2 PR C: its run objects are additive and idempotent\n'),
        ('                    "/api/forward-validation/funnel": {"get"}, "/api/forward-validation/funnel/{trading_date}": {"get"}, "/api/forward-validation/frozen-controls": {"get"}}\n',
         '                    "/api/forward-validation/funnel": {"get"}, "/api/forward-validation/funnel/{trading_date}": {"get"}, "/api/forward-validation/frozen-controls": {"get"}, "/api/forward-validation/run": {"get"}}\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # Stages 7.1-7.3: pinned by their own F tests\n',
         '    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # Stages 7.1-7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                           # Stages 7.3.1, 7.4: pinned by their own F tests\n',
         '    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                           # Stages 7.3.1, 7.4: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Stage 7.5, pinned by its own F tests\n',
         '    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Stage 7.5, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Stage 7.4, pinned by its own F tests\n',
         '    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Stage 7.4, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                           # Stages 7.3, 7.3.1: pinned by their own F tests\n',
         '    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                           # Stages 7.3, 7.3.1: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                             # Stages 7.2, 7.3: pinned by their own F tests\n',
         '    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                             # Stages 7.2, 7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n',
         '    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n'),
    ],
    'tests/test_product_flow_612.py': [
        ('    assert ddr["lifecycle_basis"] == ["no forward paper validation session recorded yet and Paper Auto is OFF"]\n',
         '    assert ddr["lifecycle_basis"][0] == ("not PAPER_ACTIVE: no active forward run; the forward run is NOT READY; no strategy day yet (MONITOR_ONLY days never count); "\n                                         "the run\'s bot-only drawdown is unknown") and any(b.startswith("promotion") for b in ddr["lifecycle_basis"][1:])   # Wave 2 PR C (C-34, C-59)\n'),
        ('    # the lifecycle follows persisted facts: Paper Auto ON reads PAPER_ACTIVE; APPROVED / REJECTED are never derived\n',
         '    # the lifecycle follows persisted facts (Wave 2 PR C, D-W2-c): Paper Auto ON without an active, READY forward run with a strategy day stays\n    # PAPER_CANDIDATE, and the basis says why; APPROVED / REJECTED are never derived\n'),
        ('    assert j2["facts"]["mode"] == "PAPER_AUTO" and j2["strategies"][0]["status"] == "PAPER_ACTIVE" and j2["strategies"][0]["declared_status"] == "PAPER_CANDIDATE" and "Paper Auto is ON" in j2["strategies"][0]["lifecycle_basis"][0]\n',
         '    assert j2["facts"]["mode"] == "PAPER_AUTO" and j2["strategies"][0]["status"] == "PAPER_CANDIDATE" and j2["strategies"][0]["declared_status"] == "PAPER_CANDIDATE" \\\n        and j2["strategies"][0]["lifecycle_basis"][0].startswith("not PAPER_ACTIVE: no active forward run")\n'),
        ('    assert S.derive_lifecycle(ddr, {"available": True, "mode": "PAPER_AUTO", "validation": {"sessions_completed": 0}})["status"] == "PAPER_ACTIVE"\n    lc = S.derive_lifecycle(ddr, {"available": True, "mode": "MONITOR_ONLY", "validation": {"sessions_completed": 3, "sessions_target": 30}})\n    assert lc["status"] == "PAPER_ACTIVE" and "3 session(s)" in lc["basis"][0] and lc["decided"] is False and "never derived" in lc["decision"]\n    assert S.derive_lifecycle(R.get("MOMENTUM"), {"available": True, "mode": "PAPER_AUTO", "validation": {"sessions_completed": 9}})["status"] == "RESEARCH"   # a placeholder never becomes active\n',
         '    assert S.derive_lifecycle(ddr, {"available": True, "mode": "PAPER_AUTO", "validation": {"sessions_completed": 0}})["status"] == "PAPER_CANDIDATE"   # Wave 2 PR C (D-W2-c): Paper Auto alone never\n    assert S.derive_lifecycle(ddr, {"available": True, "mode": "MONITOR_ONLY", "validation": {"sessions_completed": 3, "sessions_target": 30}})["status"] == "PAPER_CANDIDATE"\n    run = {"run_defined": True, "run_active": True, "run_suspended": False, "run_ready": "READY", "strategy_days": 1, "run_drawdown_status": "KNOWN", "run_truncated": False,\n           "run_unverified": 0, "run_id": "FR-0001", "run_label": "1 / 30 strategy sessions (1 healthy · 0 degraded · 0 failed · 0 unreadable · 0 not run)", "sessions_completed": 3}\n    lc = S.derive_lifecycle(ddr, {"available": True, "mode": "PAPER_AUTO", "validation": run})\n    assert lc["status"] == "PAPER_ACTIVE" and lc["basis"][0] == f"forward run FR-0001: {run[\'run_label\']}" and lc["decided"] is False and "never derived" in lc["decision"]\n    for bad in ({"strategy_days": 0}, {"run_suspended": True}, {"run_ready": "NOT READY"}, {"run_active": False}):   # MONITOR_ONLY days only, suspended, NOT READY, ended (Q43-C)\n        assert S.derive_lifecycle(ddr, {"available": True, "mode": "PAPER_AUTO", "validation": {**run, **bad}})["status"] == "PAPER_CANDIDATE", bad\n    assert S.derive_lifecycle(R.get("MOMENTUM"), {"available": True, "mode": "PAPER_AUTO", "validation": run})["status"] == "RESEARCH"   # a placeholder never becomes active\n'),
        ('    assert live.state == "LIVE" and live.items[0] == "Forward paper validation sessions: 2 / 30 (2 / 30)" and live.items[1] == "Closed paper trades: 0 (no closed paper trade yet)" and "not a forecast" in live.note\n',
         '    assert live.state == "LIVE" and live.items[0] == "Forward paper validation sessions: 2 / 30 (2 / 30)" and live.items[1] == "Closed paper trades: 0 verified · — unverified (no closed paper trade yet)" and "not a forecast" in live.note   # Wave 2 PR C (C-79)\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B\n',
         '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C\n'),
    ],
    'tests/test_sip_eval_78b1.py': [
        ('    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n',
         '    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n'),
    ],
    'tests/test_strategy_validation_62.py': [
        ('from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n',
         'from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, NEW_W2C, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n'),
        ('NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n',
         'NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B | NEW_W2C   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n'),
        ('    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n',
         '    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B - NEW_W2C) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                     # Stage 7.8B-1, pinned by its own F tests\n',
         '    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                     # Stage 7.8B-1, pinned by its own F tests\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, _now(rel)))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, _now(rel))))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_wave1_safety_remediation.py': [
        ('    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B                                                         # Wave 2 PR A, pinned by its own F tests\n',
         '    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Wave 2 PR A, pinned by its own F tests\n'),
    ],
    'tests/test_wave2_pra.py': [
        ('    assert (X.VERSION, at_base("execution/__init__.py").count(\'VERSION = "execution_v1"\')) == ("execution_v2", 1)\n',
         '    pra_version = ast.literal_eval(named_nodes(FL.pre_w2c("execution/__init__.py", now_text("execution/__init__.py")))["=VERSION"].value)   # Wave 2 PR C: PR A\'s bump as main left it (PR C\'s execution_v3: its T-W01C)\n    assert (pra_version, at_base("execution/__init__.py").count(\'VERSION = "execution_v1"\')) == ("execution_v2", 1)\n'),
        ('        added = "\\n".join(l for l in FL.pre_w2b(rel, now_text(rel)).splitlines() if l not in base)\n',
         '        added = "\\n".join(l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel))).splitlines() if l not in base)\n'),
        ('        now, base = named_nodes(FL.pre_w2b(rel, now_text(rel))), named_nodes(at_base(rel))\n',
         '        now, base = named_nodes(FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel)))), named_nodes(at_base(rel))\n'),
        ('        u_now, u_base = unnamed(FL.pre_w2b(rel, now_text(rel))), unnamed(at_base(rel))\n',
         '        u_now, u_base = unnamed(FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel)))), unnamed(at_base(rel))\n'),
        ('        assert _normalized(rel, name, FL.pre_w2b(rel, now_text(rel))) == ast.dump(base_node), (rel, name)\n',
         '        assert _normalized(rel, name, FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel)))) == ast.dump(base_node), (rel, name)\n'),
        ('    assert named_nodes(now_text("execution/service.py"))["ExecutionService._reconcile"] and digest(named_nodes(now_text("execution/service.py"))["ExecutionService._reconcile"]) == \\\n',
         '    pre_c = FL.pre_w2c("execution/service.py", now_text("execution/service.py"))              # Wave 2 PR C: its W2-6 _reconcile detail is its own pin (T-W05C, T-W01C)\n    assert named_nodes(pre_c)["ExecutionService._reconcile"] and digest(named_nodes(pre_c)["ExecutionService._reconcile"]) == \\\n'),
        ("    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A)   # Wave 2 PR B: its own sets (its edits of PR A's new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py\n",
         '    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A) | (FL.EDITED_W2C - FL.NEW_W2A - FL.NEW_W2B)   # Wave 2 PR B / PR C: their own sets (their edits of earlier new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py / test_wave2_prc.py\n'),
        ('        assert not [l for l in FL.pre_w2b(rel, now_text(rel)).splitlines() if l not in base and marker.match(l)], rel\n',
         '        assert not [l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel))).splitlines() if l not in base and marker.match(l)], rel\n'),
        ('        text = FL.undo_w2b(rel, now_text(rel))                                                 # Wave 2 PR B: its own in-place edits undone first (its F03B)\n',
         '        text = FL.undo_w2b(rel, FL.undo_w2c(rel, now_text(rel)))                               # Wave 2 PR B / PR C: their own in-place edits undone first (F03B / F03C)\n'),
    ],
    'tests/test_wave2_pra_accounting.py': [
        ('',
         '    real_block = d.fv._run_block                                                            # Wave 2 PR C (C-88): the weekly gate counts a run\'s member sessions;\n    monkeypatch.setattr(d.fv, "_run_block", lambda now=None: (lambda b: {**b, "defined": True, "sessions": {**b["sessions"], "member_dates": list(DATES)}})(real_block(now)))   # here the five days are\n'),
        ('              f"{fv}/events", f"{fv}/samples", f"{fv}/funnel", f"{fv}/funnel/{DAY}", f"{fv}/frozen-controls", "/api/strategy-registry",\n',
         '              f"{fv}/events", f"{fv}/samples", f"{fv}/funnel", f"{fv}/funnel/{DAY}", f"{fv}/frozen-controls", f"{fv}/run", "/api/strategy-registry",\n'),
        ('    assert st["edge_status"] == "EARLY_POSITIVE" and st["unverified"] == 0                     # the control: the rule can say positive\n',
         '    from forward_validation import metrics as M                                                # Wave 2 PR C (C-35): the all-time label is informational; the rule is the run block\'s\n    assert st["edge_status"] == "INSUFFICIENT_DATA" and st["unverified"] == 0 and st["expectancy_status"]["reasons"][0].startswith("not a forward run")\n    assert M.edge_status(st["closed_trades"], 30, st["expectancy"], st["win_rate"], "0.00", unverified=st["unverified"])["status"] == "EARLY_POSITIVE"   # the control: the rule can say positive\n'),
        ('    assert st["edge_status"] == "INSUFFICIENT_DATA" and st["unverified"] == 1 and "unverified trade" in st["expectancy_status"]["reasons"][0]\n',
         '    assert st["edge_status"] == "INSUFFICIENT_DATA" and st["unverified"] == 1\n    assert "unverified trade" in M.edge_status(st["closed_trades"], 30, st["expectancy"], st["win_rate"], "0.00", unverified=st["unverified"])["reasons"][0]   # the cap (C-53)\n'),
        ('    assert tuple(header) == _base_tuple("TRADE_COLS") + ("verified",) and header[0] == "position_id"\n',
         '    assert tuple(header) == _base_tuple("TRADE_COLS") + ("verified", "run_id", "member", "exclusion_reason", "time_source") and header[0] == "position_id"   # Wave 2 PR C: the run columns\n'),
        ('    assert j["columns"][-1] == "verified" and {r["position_id"]: r["gross_pnl"] for r in j["rows"]}[amb.position_id] is None\n',
         '    assert j["columns"][-5:] == ["verified", "run_id", "member", "exclusion_reason", "time_source"] and {r["position_id"]: r["gross_pnl"] for r in j["rows"]}[amb.position_id] is None   # Wave 2 PR C\n'),
        ('    assert tuple(header) == _base_tuple("DAILY_COLS") + ("pnl_status", "unverified")\n',
         '    assert tuple(header) == _base_tuple("DAILY_COLS") + ("pnl_status", "unverified", "run_id", "member", "exclusion_reason", "time_source")   # Wave 2 PR C: the run columns\n'),
        ('    cum0 = {"as_of": "x", "strategy": {"closed_trades": 5, "win_rate": "60.00", "average_win": "2.00", "average_loss": "1.00", "expectancy": "0.80", "unverified": 0}}\n',
         '    cum0 = {"as_of": "x", "strategy": {"closed_trades": 5, "win_rate": "60.00", "average_win": "2.00", "average_loss": "1.00", "expectancy": "0.80", "unverified": 0},\n            "run": {"defined": True, "sessions": {"member_dates": [f"2026-10-{19 + i}" for i in range(5)]}}}   # Wave 2 PR C (C-88): the five days are run members\n'),
        ('    now61 = FL.undo_w2b("tests/test_forward_validation_61.py", (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8"))   # Wave 2 PR B (D-1): PR A\'s delta; PR B\'s own edits are pinned by F02B / F03B\n',
         '    now61 = FL.undo_w2b("tests/test_forward_validation_61.py", FL.undo_w2c("tests/test_forward_validation_61.py", (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8")))   # Wave 2 PR B (D-1) / PR C (T-WC06): PR A\'s delta; their own edits: F02B / F03B, F02C / F03C\n'),
        ('    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8")), at_base("tests/test_forward_paper_610a.py")\n',
         '    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", FL.undo_w2c("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8"))), at_base("tests/test_forward_paper_610a.py")\n'),
        ('        assert (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel), rel\n',
         '        assert FL.undo_w2c(rel, FL.pre_w2c(rel, (ROOT / rel).read_text(encoding="utf-8"))) == at_base(rel), rel   # Wave 2 PR C (T-WC06): its W2-7 edits are its own pins\n'),
    ],
    'tests/test_wave2_pra_early_stop.py': [
        ('',
         "import freeze_ledger as FL  # Wave 2 PR C: T-E13p reads PR A's text of the service (pre_w2c)\n"),
        ('    assert not r.fv.store.query("SELECT name FROM sqlite_master WHERE name IN (\'forward_runs\', \'forward_run_events\')")\n',
         '    assert not r.fv.store.query("SELECT run_id FROM forward_runs WHERE ended_at IS NULL")   # Wave 2 PR C: its tables exist; no run is active, so no run acknowledgement applies\n'),
        ('    src = (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8") + (ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8")\n',
         '    src = FL.pre_w2c("forward_validation/service.py", (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8")) + (ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8")   # Wave 2 PR C: PR A\'s text\n'),
        ('    assert len(trys) == 3\n    main, capture, persist = trys\n',
         '    assert len(trys) == 4                                                                               # Wave 2 PR C (design 4.4): + the guarded run check\n    main, capture, run_check, persist = trys\n    assert [ast.unparse(s) for s in run_check.body] == ["out[\'run\'] = self._run_check(now)"] and fn.body.index(run_check) == fn.body.index(capture) + 1\n'),
        ('    assert fn.body.index(persist) == fn.body.index(capture) + 1 and ast.unparse(fn.body[fn.body.index(persist) + 1]) == "self.last_observation = out"\n',
         '    assert fn.body.index(persist) == fn.body.index(run_check) + 1 and ast.unparse(fn.body[fn.body.index(persist) + 1]) == "self.last_observation = out"\n'),
    ],
    'tests/test_wave2_prb.py': [
        ('',
         'def named_assign(text, name):\n    """Wave 2 PR C: the value of the module-level assignment `name` (an AST node, for literal_eval)."""\n    return next(n.value for n in ast.parse(text).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))\n\n\n'),
        ('    return "\\n".join(l for l in now_text(rel).splitlines() if l not in base)\n',
         '    return "\\n".join(l for l in FL.pre_w2c(rel, now_text(rel)).splitlines() if l not in base)   # Wave 2 PR C: PR B\'s lines (PR C\'s own: T-W04C)\n\n\ndef prb_files(base):\n    """Wave 2 PR C: the change set between `base` and main after PR #52 (fixed commits). PR C\'s delta: tests/test_wave2_prc.py."""\n    return set(_git("diff", "--name-only", base, FL.BASE_W2C).split())\n'),
        ('    prod = {p for p in changed_files(FL.BASE_W2A) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in HARNESS_B}   # the harness: F04B\n',
         '    prod = {p for p in prb_files(FL.BASE_W2A) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in HARNESS_B}   # the harness: F04B\n'),
        ('    mine = {p for p in changed_files(FL.BASE_W2B) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in HARNESS_B}\n',
         '    mine = {p for p in prb_files(FL.BASE_W2B) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in HARNESS_B}\n'),
        ('    from database.forward_validation_migrations import V2_SAMPLE_COLUMNS, V2_SESSION_COLUMNS\n    from forward_validation.store import SAMPLE_COLS\n',
         '    lit = lambda rel, name: ast.literal_eval(named_assign(FL.pre_w2c(rel, now_text(rel)), name))  # noqa: E731 - Wave 2 PR C: PR B\'s text (PR C adds run_id / frozen_hash)\n    V2_SAMPLE_COLUMNS, V2_SESSION_COLUMNS = lit("database/forward_validation_migrations.py", "V2_SAMPLE_COLUMNS"), lit("database/forward_validation_migrations.py", "V2_SESSION_COLUMNS")\n    SAMPLE_COLS = lit("forward_validation/store.py", "SAMPLE_COLS")\n'),
        ('        now, base = named_nodes(now_text(rel)), named_nodes(at_base(rel))\n',
         '        now, base = named_nodes(FL.pre_w2c(rel, now_text(rel))), named_nodes(at_base(rel))\n'),
        ('        assert sorted(unnamed(now_text(rel))) == sorted(unnamed(at_base(rel))), rel                 # no new module-level statement (imports go inside)\n    store_class = ast.parse(now_text("forward_validation/store.py")).body\n',
         '        assert sorted(unnamed(FL.pre_w2c(rel, now_text(rel)))) == sorted(unnamed(at_base(rel))), rel                 # no new module-level statement (imports go inside)\n    store_class = ast.parse(FL.pre_w2c("forward_validation/store.py", now_text("forward_validation/store.py"))).body\n'),
        ('        assert digest(named_nodes(now_text(rel))[name]) == digest(named_nodes(at_base(rel))[name]), (rel, name)\n',
         '        assert digest(named_nodes(FL.pre_w2c(rel, now_text(rel)))[name]) == digest(named_nodes(at_base(rel))[name]), (rel, name)\n'),
        ('    now, base = fn_text(now_text(rel), FN + "cumulative"), fn_text(at_base(rel), FN + "cumulative")\n',
         '    now, base = fn_text(FL.pre_w2c(rel, now_text(rel)), FN + "cumulative"), fn_text(at_base(rel), FN + "cumulative")   # Wave 2 PR C: PR B\'s text; the current text keeps D-5: T-W06C\n'),
        ('    assert changed == FL.NEW_W2B | FL.EDITED_W2B, {"unexpected": sorted(changed - FL.NEW_W2B - FL.EDITED_W2B), "missing": sorted((FL.NEW_W2B | FL.EDITED_W2B) - changed)}\n',
         '    both_new, both_edited = FL.NEW_W2B | FL.NEW_W2C, FL.EDITED_W2B | (FL.EDITED_W2C - FL.NEW_W2B)   # Wave 2 PR C: its own sets (its edits of PR B\'s new files stay new since BASE_W2B), pinned by tests/test_wave2_prc.py\n    assert changed == both_new | both_edited, {"unexpected": sorted(changed - both_new - both_edited), "missing": sorted((both_new | both_edited) - changed)}\n'),
        ('    assert {p for p in changed if p not in in_base} == FL.NEW_W2B and {p for p in changed if p in in_base} == FL.EDITED_W2B\n',
         '    assert {p for p in changed if p not in in_base} == both_new and {p for p in changed if p in in_base} == both_edited\n'),
        ('    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]\n',
         '    prb_groups = subprocess.run(["git", "show", f"{FL.BASE_W2C}:.github/ci/pytest_groups.json"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")   # Wave 2 PR C: the manifest as PR #52 left it (PR C\'s entry: F01C)\n    groups, base_groups = json.loads(prb_groups)["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]\n'),
        ('        b, n = defs(at_base(rel)), defs(now_text(rel))\n',
         '        b, n = defs(at_base(rel)), defs(FL.undo_w2c(rel, now_text(rel)))                          # Wave 2 PR C: its own in-place edits undone first (F02C)\n'),
        ('        assert FL.undo_w2b(rel, now_text(rel)) == at_base(rel), rel                             # now -> 50cb2d7b, byte for byte\n    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")\n',
         '        assert FL.undo_w2b(rel, FL.undo_w2c(rel, now_text(rel))) == at_base(rel), rel           # now -> (F03C) 875bc06d -> 50cb2d7b, byte for byte\n    main_fl = at_base("tests/freeze_ledger.py")\n    fl = subprocess.run(["git", "show", f"{FL.BASE_W2C}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n")   # Wave 2 PR C: the ledger as PR #52 left it\n'),
        ('    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_W2B and set(main_ledger) <= set(FL.LEDGER)\n    assert {k for k in main_ledger if main_ledger[k] != FL.LEDGER[k]} == FL.EDITED_W2B & set(main_ledger)   # every re-hash is a declared PR B edit\n',
         '    prb_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # Wave 2 PR C: PR B\'s ledger; PR C\'s re-hashes are its F03C\n    assert set(prb_ledger) - set(main_ledger) == FL.NEW_LEDGER_W2B and set(main_ledger) <= set(prb_ledger)\n    assert {k for k in main_ledger if main_ledger[k] != prb_ledger[k]} == FL.EDITED_W2B & set(main_ledger)   # every re-hash is a declared PR B edit\n'),
    ],
    'tests/test_wave2_prb_sessions.py': [
        ('    assert row["assessment"]["opened_by"]["at"] == iso(et(2026, 10, 21, 16, 15, 30)) and row["classifier_version"] == "session_classifier_v2"\n',
         '    assert row["assessment"]["opened_by"]["at"] == iso(et(2026, 10, 21, 16, 15, 30)) and row["classifier_version"] == "session_classifier_v2_1"\n'),
        ('    assert (consts["G"], consts["H"], consts["K"], consts["CLASSIFIER_VERSION"], consts["EVIDENCE_READ_ATTEMPTS"]) == (360, 60, 3, "session_classifier_v2", 3)\n',
         '    assert (consts["G"], consts["H"], consts["K"], consts["CLASSIFIER_VERSION"], consts["EVIDENCE_READ_ATTEMPTS"]) == (360, 60, 3, "session_classifier_v2_1", 3)\n'),
        ('    assert row["assessment"]["unavailable_sources"]["assessment"]["attempts"] >= 3 and row["classifier_version"] == "session_classifier_v2"\n',
         '    assert row["assessment"]["unavailable_sources"]["assessment"]["attempts"] >= 3 and row["classifier_version"] == "session_classifier_v2_1"\n'),
        ('    assert row["classifier_version"] == "session_classifier_v2" and row["assessment"]["opened_by"]["owner"] == "7002:bbbbbbbb"\n',
         '    assert row["classifier_version"] == "session_classifier_v2_1" and row["assessment"]["opened_by"]["owner"] == "7002:bbbbbbbb"\n'),
    ],
}
SEMANTIC_TESTS_W2C = {                                                                       # Wave 2 PR C: every changed test function -> its finding (F02C)
    'tests/test_bot_activity_68.py::test_F01_every_change_since_main_is_a_new_file_or_an_exact_ledger_entry': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_bot_activity_68.py::test_F02_the_executor_and_scheduler_only_gained_display_bookkeeping_and_every_guard_line_is_kept': "P-08 / design 14: the raw read goes through FL.pre_w2c, so the test still proves main's 875bc06d text; T-W05C pins PR C's delta",
    'tests/test_chart_empty_state_611fix.py::test_F01_the_fix_changes_exactly_the_declared_files_and_no_backend_file': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_decision_flow_610c.py::test_F01_stage_610c_changes_exactly_its_declared_files_and_no_execution_strategy_risk_or_policy_file': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_decision_flow_610c.py::test_F02_execution_setups_auto_run_and_the_live_coverage_are_byte_identical_and_only_the_router_and_three_inert_flags_were_added': "P-08 / design 14: a SEMANTIC_W2C file is a declared exemption beside SEMANTIC_W2B; PR C's own delta is pinned by T-W05C",
    'tests/test_dynamic_universe_610c.py::test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base': "P-08 / design 14: a SEMANTIC_W2C file is a declared exemption beside SEMANTIC_W2B; PR C's own delta is pinned by T-W05C",
    'tests/test_engine_health_610d.py::test_F01_the_change_set_is_exactly_the_declared_files_and_touches_no_execution_strategy_or_safety_file': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C; P-08 / design 14: a SEMANTIC_W2C file is a declared exemption beside SEMANTIC_W2B; PR C's own delta is pinned by T-W05C; the forward_validation loop admits NEW_W2C and the product_ui.js read takes pre_w2c",
    'tests/test_evidence_gate_541.py::test_F01_the_fix_changes_one_function_and_nothing_in_the_setup_engine_ppe_evidence_or_execution': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C; P-08 / design 14: a SEMANTIC_W2C file is a declared exemption beside SEMANTIC_W2B; PR C's own delta is pinned by T-W05C",
    'tests/test_evidence_refresh_542.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_forward_paper_610a.py::test_F01_stage_610a_changes_exactly_its_declared_files_and_no_strategy_risk_broker_or_market_data_file': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C; P-08 / design 14: the 6.10A change-set ban subtracts SEMANTIC_W2C; T-W04C bans health.py / capture.py / cohort.py for PR C",
    'tests/test_forward_paper_610a.py::test_F02_the_order_decision_logic_is_unchanged_and_the_executor_only_gained_failure_swallowing_markers': "P-08 / design 14: the raw read goes through FL.pre_w2c, so the test still proves main's 875bc06d text; T-W05C pins PR C's delta",
    'tests/test_forward_paper_610a.py::test_H01_a_fully_observed_day_is_healthy_even_with_a_losing_trade_and_its_summary_is_operational_only': "C-80 / C-87 / T-P11: a cohort outside a forward run reads 'not a forward run' with no P/L edge; the healthy-session and insufficiency checks kept",
    'tests/test_forward_validation_61.py::test_D01_raw_rows_are_append_only_sessions_are_never_reclassified_and_the_migration_is_additive': 'P-08 / design 4.1: the run tables are not forward_validation_* names; objects on them are excluded by tbl_name, compared across a further migration and the tables asserted',
    'tests/test_forward_validation_61.py::test_R01_the_daily_and_weekly_reports_are_deterministic_plain_text_without_marketing_language': "C-88 / P-09: the weekly gate counts the forward run's closed member sessions; the five-day aggregate is checked through a run whose members are those days",
    'tests/test_forward_validation_61.py::test_S01_a_complete_session_is_recorded_and_classified_healthy_with_its_daily_summary': 'C-65a / design 14 [Q41-04]: rows the current code finalizes carry session_classifier_v2_1; rows finalized before PR C keep v2 (B-38)',
    'tests/test_forward_validation_61.py::test_X01_read_only_api_routes_shapes_and_route_words': 'P-08 / design 14: the exact route map gains the read-only GET /api/forward-validation/run (may-add get_run)',
    'tests/test_market_intel_70.py::test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_market_intel_collector_73.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_market_intel_quality_74.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_market_intel_schema_v2_731.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_market_intel_sources_72.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_market_intel_store_71.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_market_scan_75.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_product_flow_612.py::test_A01_registry_api_is_get_only_with_the_documented_shapes_and_derives_the_lifecycle_from_persisted_facts': 'C-34 / design 8.5 (D-W2-c): the lifecycle basis says why the strategy is not PAPER_ACTIVE (no active forward run first) and carries the promotion text (C-59)',
    'tests/test_product_flow_612.py::test_D01_downside_recovery_sections_come_from_the_frozen_controls_and_nothing_is_fabricated': "C-79 / T-P10 [Q43-3]: the registry card text becomes 'Closed paper trades: n verified · m unverified' (Rule F counted apart); every other item kept",
    'tests/test_product_flow_612.py::test_R01_registry_entries_sources_lifecycle_statuses_and_placeholders': 'C-34 / Q43-C (D-W2-c): PAPER_ACTIVE needs an active, unsuspended, READY run with a strategy day; Paper Auto ON or completed sessions alone never; placeholders never',
    'tests/test_robinhood_portfolio_69.py::test_F01_stage_69_changes_exactly_its_files_and_no_execution_strategy_or_risk_file': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_runtime_reliability_ops.py::test_F01_the_change_set_is_exactly_the_declared_ops_files_and_touches_no_trading_code': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_sip_eval_78b1.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_strategy_validation_62.py::=NEW_62': 'P-08 / design 14: NEW_62 gains NEW_W2C (its import and the F01 subtraction); it feeds 6.1 Z02, 6.1.1, 6.1.2 and the WAL fix',
    'tests/test_strategy_validation_62.py::test_F01_main_plus_only_the_stage_62_files_and_exact_edits': 'P-08 / design 14: NEW_62 gains NEW_W2C (its import and the F01 subtraction); it feeds 6.1 Z02, 6.1.1, 6.1.2 and the WAL fix',
    'tests/test_swing_pullback_78.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_unified_exposure_gate_691.py::test_F01_stage_691_changes_exactly_its_declared_files_and_no_risk_strategy_broker_or_database_file': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_unified_exposure_gate_691.py::test_F02_the_execution_semantics_changed_on_purpose_and_exactly_as_declared': "P-08 / design 14: the raw read goes through FL.pre_w2c, so the test still proves main's 875bc06d text; T-W05C pins PR C's delta",
    'tests/test_wave1_safety_remediation.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_market_data_broker_or_research_file_changed': "P-08 / design 14: the change-set union gains NEW_W2C / EDITED_W2C, every earlier set kept; PR C's own sets are pinned by F01C",
    'tests/test_wave2_pra.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': 'P-08 / design 14: the exact-set rule over the three PRs (new: + NEW_W2C; edited: + EDITED_W2C - NEW_W2A - NEW_W2B); marked blocks read pre_w2c',
    'tests/test_wave2_pra.py::test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': 'P-08 / design 14: REPLAY_W2A is replayed on undo_w2b(undo_w2c(...)) (F03C, then F03B); the ledger of 50cb2d7b is unchanged',
    'tests/test_wave2_pra.py::test_TR05pre_reconciliation_output_is_identical_to_the_baseline': "P-08 / design 14 (T-WC04): the _reconcile hash pin reads main's 875bc06d text; the golden replay still runs the current code; T-W05C / T-W01C",
    'tests/test_wave2_pra.py::test_TW03_pr_a_bumps_the_execution_version_and_the_v1_pins_are_the_baseline': "P-08 / design 14 (T-WC04): PR A's bump v1 -> v2 is read from main's 875bc06d text (pre_w2c); PR C's execution_v3 is its T-W01C",
    'tests/test_wave2_pra.py::test_TW04_the_production_diff_is_exactly_pr_a_and_names_no_pr_c_object': "P-08 / design 14 (T-WC01): T-W04's PR C token ban reads pre_w2b(pre_w2c(...)), so it is evaluated on PR A's text; PR C's lines: T-W04C",
    'tests/test_wave2_pra.py::test_TW05_only_the_listed_functions_changed_and_only_the_listed_names_are_new': "P-08 / design 14 (T-WC01): T-W05's reads take pre_w2b(pre_w2c(...)), so PR A's closed list is proven on PR A's text; T-W05C pins PR C",
    'tests/test_wave2_pra_accounting.py::test_TA25_a_heal_after_the_close_is_served_by_every_read_site_and_persisted_once': 'C-88 / C-35: the weekly read site is served through a run whose members are the five days, so every read site still shows the healed 4.80, never 6.00',
    'tests/test_wave2_pra_accounting.py::test_TA29b_every_read_and_export_path_running_pr_a_code_leaves_the_database_contents_unchanged': "P-08 / design 14: T-A29b proves every GET route read-only; its path list gains PR C's /run, so the new route is proven read-only too",
    'tests/test_wave2_pra_accounting.py::test_TA34_no_early_positive_while_a_trade_is_unverified': 'C-35 / C-57 (D-W2-i): the all-time label is informational; the positive control and the unverified cap are asserted on the edge rule over the same verified trades',
    'tests/test_wave2_pra_accounting.py::test_TA37_the_trades_export_appends_verified_and_leaves_unverified_pnl_cells_empty': "C-84 / design 12: the trades export appends the run columns run_id, member, exclusion_reason, time_source after PR A's verified; every older column kept in place",
    'tests/test_wave2_pra_accounting.py::test_TA38_the_daily_export_appends_the_pnl_status_and_never_writes_a_zero_for_a_null': "C-84 / design 12: the daily export appends the run columns run_id, member, exclusion_reason, time_source after PR A's pnl_status / unverified",
    'tests/test_wave2_pra_accounting.py::test_TA40_the_weekly_report_never_sums_an_unstable_or_unknown_day_silently': "C-88: the weekly gate counts a run's member sessions, so the cumulative carries a run whose members are the five days; every P/L assertion unchanged",
    'tests/test_wave2_pra_accounting.py::test_TA42_downstream_compatibility_capture_keeps_raw_values_and_the_known_consumers_are_unchanged': 'Q41-04 / design 14 (T-WC06): T-A42 (ii) reads the 6.1 / 6.10A tests through undo_w2c and the FV frontend file / node test through pre_w2c / undo_w2c',
    'tests/test_wave2_pra_early_stop.py::test_TE13p_pr_a_has_no_run_acknowledgement_path_so_an_element_on_record_stops': "P-08 / design 11.2: PR C's tables exist, so T-E13p asserts no ACTIVE run; its run_ack ban reads PR A's text (pre_w2c); PR C: T-E13 / T-E13b",
    'tests/test_wave2_pra_early_stop.py::test_TE25_a_persistence_failure_never_prevents_the_early_stop': 'C-13 / design 4.4: observe gains the guarded suspension check between capture and persistence; the static order pins four try blocks and that one',
    'tests/test_wave2_prb.py::added_lines': "P-08 / design 14 (T-WC02): T-W04B's added lines are PR B's (pre_w2c text); PR C's own added lines are checked by T-W04C",
    'tests/test_wave2_prb.py::named_assign': "P-08 / design 14 (T-WC02): reads a module-level literal from PR B's text, so the run_id / frozen_hash ban stays on PR B's columns",
    'tests/test_wave2_prb.py::prb_files': "P-08 / design 14 (T-WC02): PR B's change set between fixed commits (BASE_W2A or BASE_W2B .. BASE_W2C), read by T-W04B",
    'tests/test_wave2_prb.py::test_F01B_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': "P-08 / design 14 (T-WC03): the exact-set rule over PR B and PR C since BASE_W2B; the CI manifest check reads BASE_W2C (PR C's entry: F01C)",
    'tests/test_wave2_prb.py::test_F02B_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added': "P-08 / design 14 (T-WC03): PR B's changed test functions are read on undo_w2c text (PR C's own: F02C); the frozen-hash and disable_auto pins kept",
    'tests/test_wave2_prb.py::test_F03B_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': 'P-08 / design 14 (T-WC03): UNDO_W2B replays on undo_w2c text and the ledger of 875bc06d is rebuilt to 50cb2d7b; the current view() loop kept',
    'tests/test_wave2_prb.py::test_TW04B_the_production_diff_is_pr_a_plus_pr_b_and_pr_b_adds_no_pr_c_or_order_token': 'P-08 / design 14 (T-WC02): T-W04B on BASE_W2B..BASE_W2C (prb_files) and pre_w2c text; config.py and the PR C token ban kept',
    'tests/test_wave2_prb.py::test_TW05B_only_the_listed_functions_changed_and_only_the_listed_names_are_new': "P-08 / design 14 (T-WC02): T-W05B's reads take pre_w2c, so PR B's closed list and PINNED_B are proven on PR B's text; T-W05C pins PR C",
    'tests/test_wave2_prb.py::test_TW06B_d5_unreadable_evidence_is_excluded_from_acceptance_by_exactly_the_declared_lines': "P-08 / design 14 (T-WC02): T-W06B proves PR B's D-5 delta on pre_w2c text; T-W06C proves the current cumulative() keeps each D-5 line",
    'tests/test_wave2_prb_sessions.py::test_TS01_observer_level': 'C-65a / design 14 [Q41-04]: rows the current code finalizes carry session_classifier_v2_1; rows finalized before PR C keep v2 (B-38)',
    'tests/test_wave2_prb_sessions.py::test_TS54_v1_classify_session_is_unchanged_and_v2_is_separate_with_literal_constants': 'C-65a / design 14 [Q41-04]: rows the current code finalizes carry session_classifier_v2_1; rows finalized before PR C keep v2 (B-38)',
    'tests/test_wave2_prb_sessions.py::test_TS55_ix_a_bug_in_the_assessment_is_contained_every_tick_and_escalates_fail_closed': 'C-65a / design 14 [Q41-04]: rows the current code finalizes carry session_classifier_v2_1; rows finalized before PR C keep v2 (B-38)',
    'tests/test_wave2_prb_sessions.py::test_TS65_a_fresh_database_accounts_its_first_observed_day': 'C-65a / design 14 [Q41-04]: rows the current code finalizes carry session_classifier_v2_1; rows finalized before PR C keep v2 (B-38)',
}
VERSION_PINS_W2A["execution_v3"] = {                                                    # Wave 2 PR C: _reconcile (W2-6) and _early_stop (run_ack) moved (T-W01C)
    'execution/service.py::ExecutionService._apply_order': '9b2760f71dd2bccf46e0d2c46b59ab512ae54111bc5f62a79bf60cabd9ebb884',
    'execution/service.py::ExecutionService._apply_fill': '6fb9726d464aa34bdb52c6147b681977dabe2053b54afd05b4b3beae04041c7d',
    'execution/service.py::recompute_pnl': '544ec665f8dfcf1138482409b842dc8421833c48163cde6db719aac11ac56dd2',
    'execution/service.py::ExecutionService._after_terminal': 'ae38d36dcb3fda94898b0390b9ace836d5b2a28e6b9555a97b5f40accc153fe9',
    'execution/service.py::ExecutionService._monitor_intent': '3bfbe67b488b2f8b62b6d6690e05152d61957a7477b3a5dac0c1706e5bd86c85',
    'execution/service.py::ExecutionService._monitor_intents': '1a7d5d91c873d764a896476da1b7287a3a8843e198133aea004377d0dbb7d935',
    'execution/service.py::ExecutionService._reconcile': 'c543025d2b1bfe2ba3007e0c0675b547dbefdcc0d02b0b11175b2ce001f646d9',
    'execution/service.py::ExecutionService._set_recon': '83af97fc5c3d3120568b562ebd1fc4247302acc099955e7f87dd301eb5c42923',
    'execution/service.py::ExecutionService._exit_context': '27d26403bf1541bbd1f04f14f5d32df3bfd3d0b1428149af4fd640bc5f30bd3a',
    'execution/service.py::ExecutionService._evaluate_positions': '7fdaca073f8f29f372bd0476d1b41a2afffa1feb9423b9407f14e6292901c72d',
    'execution/service.py::ExecutionService._maybe_submit_exit': '9517b81c8a9e64eb87e5f9e1d84cbb6799965e87e2e33f24b10df34b81356e42',
    'execution/service.py::ExecutionService._scan_entries': '60ac458bd0e6506fb70f1ff4c2f1fe20df87fb41fd3f7e4cffe5de9b7b347ae5',
    'execution/service.py::ExecutionService._recheck_entry': 'e99a0b396778d5b0f43af7739f0bea85335ccd609507105866b487a88a2f3764',
    'execution/service.py::ExecutionService._submit': '76989ac3c65da51189aecbc1c344117573a7b229ec64606901327ecc0a35df06',
    'execution/service.py::ExecutionService.enable_auto': '3b0ea9c71fe041c7d870b07ecc72493f0112e65563e3e79d359996cff4532f07',
    'execution/service.py::ExecutionService.disable_auto': '50c1df4eaa55b2557290818ba1e12b2f2cc3c2f9dca7f6bcb80122a76b6205fe',
    'execution/service.py::stop_price_problem': '6ed950816c7bd4d6c6592c9331bb71542c181a362a8f40ea9bacedd02494f646',
    'execution/engine.py::exit_decision': '61a27b7aa0a57809fefe1ed183387384b5b7c05a6b212bb7679b77d302201a8f',
    'execution/engine.py::entry_controls': 'daea54f6a9f86189834993eabfcac58ebe96edbf865ffd8ba88eee2f0929e7ce',
    'execution/engine.py::blocking': '7fae3bd49b0ec2bca4d62ac164b9279ab05ec05729ce0a6fd8c4f69fb828e415',
    'execution/engine.py::session_policy': '1092f637ad4a883196701ce094357653353acc5070ea009e3def5a8c964fd2db',
    'execution/engine.py::entry_quantity': 'f0e2a0f4e8d2b98fcd6371e30aff71143ece2b15e3671070eaa77a3a11fab103',
    'execution/engine.py::sessions_held': '19941530e1d9d804fb14ea9040dd24621613810af18a4c296ba4e9c3dcaecfe1',
    'forward_validation/service.py::ForwardValidationService._early_stop': '24a40966b76e4a5d9073aa26f805247030fec3e2e055dcceeb4224230b61e515',
    'forward_validation/attention_signature.py::signature': 'ef5c28f1c9f626928e64024a7f797179ccf12967c911f4a4e581e06ad6e09ffd',
    'forward_validation/attention_signature.py::_duplicates': '3a4c822584ae000900fb3d51227488223485fc4d5b5f167e76581f063f3e6414',
    'forward_validation/attention_signature.py::_live_host': '1bfb59ae254faeff7b55ea60a17cdb0d918df9d2f708386a764b38e775d90a36',
    'forward_validation/attention_signature.py::_gate': 'c1093f95e7bba8997a76a0cd21756f4388da742d1f10bdbaa211b21e63f6e2d8',
    'forward_validation/attention_signature.py::_persistent_mismatch': 'f61ec3c94c5eb94fb5782e1767cdee064d74fac2e79bceb0816065f536efa6ab',
    'forward_validation/attention_signature.py::=HUMAN_ACTORS': '9017f60d64731465a2a894360ad90d61f642b89f31390272980ec1dd9e17e763',
    'forward_validation/attention_signature.py::=LIVE_HOST_TABLES': '77cbccb6e35b39e9adec3823e3ce7a378e192b9ac12952bebfc6bf8249cf86af',
    'forward_validation/attention_signature.py::=READERS': 'd913c004b042826f9c6cc20b79ba45756d878cfa8d6ad306c3b5c39a92099ee4',
}
BASE_EX06 = "46ebc65671435a02a0c90f6cd40f7d277ab15da2"                                  # main: Wave 2 PR C merged (PR #53); the EX-06 base
NEW_EX06 = {'DESIGN_EX06_LOCAL_REQUEST_GATE.md', 'api/local_gate.py', 'tests/frontend/ex06_local_intent.test.cjs', 'tests/test_ex06_freeze.py', 'tests/test_ex06_local_gate.py'}   # EX-06: its new files
EDITED_EX06 = {'.github/ci/pytest_groups.json', 'api/server.py', 'browser_tests/flows.py', 'frontend/app.js', 'scripts/run_aapl_paper_smoke.py', 'scripts/start_market_morning.ps1', 'scripts/stock_agent_common.ps1', 'tests/conftest.py', 'tests/freeze_ledger.py', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py', 'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_forward_paper_610a.py', 'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_sip_eval_78b1.py', 'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_trader_view_55.py', 'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_pra_early_stop.py', 'tests/test_wave2_prb.py', 'tests/test_wave2_prc.py', 'tests/test_windows_runner_60.py'}   # the wiring of the gate, the local callers, the CI entry, the conftest client, the runner probe, bookkeeping
SEMANTIC_EX06 = {                                                                           # EX-06: the INTENTIONAL change, file by file
    'api/server.py': 'the EX-06 local request gate is installed as the outermost middleware (added last) and the FastAPI docs, ReDoc and OpenAPI pages are off (D-15, D-16)',
    'browser_tests/flows.py': "the Browser Harness's raw HTTP helper sends the dashboard intent header, as the dashboard does (the gate refuses a header-less /api request)",
    'frontend/app.js': 'a marked block at the top wraps window.fetch so every same-origin dashboard request carries X-Stock-Agent-Intent: dashboard (D-7 B)',
    'scripts/run_aapl_paper_smoke.py': "the AAPL paper smoke's server check reads /api/health and /api/execution/status with the dashboard intent header (D-14)",
    'scripts/start_market_morning.ps1': "the weekday start wrapper's read-back of GET /api/execution/activity sends the dashboard intent header the gate requires (D-14)",
    'scripts/stock_agent_common.ps1': "Invoke-SAGet (the start and check scripts' local reads) sends the dashboard intent header the gate requires on every /api read (D-14)",
}
INSTRUMENTATION_EX06: dict = {}                                                            # EX-06: no executor instrumentation
NEW_LEDGER_EX06: set = {'frontend/app.js', 'scripts/run_aapl_paper_smoke.py', 'scripts/stock_agent_common.ps1'}   # main's files EX-06 registers (unchanged since BASE_68)
UNDO_EX06 = {                                                                                # EX-06's in-place edits of older test files: (main, EX-06)
    'tests/conftest.py': [
        ('',
         '\nimport fastapi.testclient as _ftc  # noqa: E402 - EX-06 (D-20): the default test client is the trusted local dashboard\nfrom starlette.testclient import TestClient as _RawTestClient  # noqa: E402\nfrom urllib.parse import urljoin as _urljoin, urlsplit as _urlsplit  # noqa: E402\n\n\ndef _headers(base: dict, extra) -> dict:\n    """`base` with `extra` (a dict, a header object or pairs) on top, case-insensitively: a test\'s own header always wins and is never doubled."""\n    out = dict(base)\n    for k, v in (extra.items() if hasattr(extra, "items") else (extra or ())):\n        for old in [o for o in out if o.lower() == str(k).lower()]:\n            del out[old]\n        out[k] = v\n    return out\n\n\nclass _LocalTestClient(_RawTestClient):\n    """EX-06 (D-20): every test\'s default client is the trusted local dashboard in a browser on this computer, sending exactly what headless\n    Edge sends (recorded in the EX-06 simulation; tests/test_ex06_local_gate.py T-X14 pins the shapes): the loopback client and Host; on an\n    /api request the dashboard intent (frontend/app.js\'s wrapper), same-origin fetch metadata and the page as Referer, plus an Origin when the\n    method changes state; on a page a typed-URL navigation; on a WebSocket handshake an Origin only. A test\'s own headers always win. The\n    gate\'s own tests use starlette\'s raw client, which presents as nothing (and is refused)."""\n\n    def __init__(self, app, base_url="http://127.0.0.1:8000", *args, headers=None, client=("127.0.0.1", 50000), **kwargs):\n        u = _urlsplit(str(base_url))\n        self._origin, self._given = f"{u.scheme}://{u.netloc}", dict(headers or {})\n        super().__init__(app, base_url, *args, headers=headers, client=client, **kwargs)\n\n    def _browser(self, method, url) -> dict:\n        safe, api = str(method).upper() in ("GET", "HEAD"), ("/" + (_urlsplit(str(url)).path or "/").lstrip("/").lower() + "/").startswith("/api/")\n        if safe and not api:\n            return {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}\n        shape = {"X-Stock-Agent-Intent": "dashboard", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Referer": self._origin + "/"}\n        return shape if safe else {**shape, "Origin": self._origin}\n\n    def request(self, method, url, *args, headers=None, **kwargs):\n        return super().request(method, url, *args, headers=_headers(_headers(self._browser(method, url), self._given), headers), **kwargs)\n\n    def websocket_connect(self, url, *args, headers=None, **kwargs):\n        return super().websocket_connect(_urljoin("ws" + self._origin[4:] + "/", url), *args, headers=_headers({"Origin": self._origin}, headers),\n                                         **kwargs)   # starlette hard-codes ws://testserver\n\n\n_ftc.TestClient = _LocalTestClient\n'),
    ],
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
        ('        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.now_text(rel)))).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n',
         '        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, FL.now_text(rel))))).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C\n',
         '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n",
         "            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n"),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B, SEMANTIC_W2C  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n',
         'from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B, SEMANTIC_W2C, SEMANTIC_EX06  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n'),
        ('        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B or rel in SEMANTIC_W2C), rel   # Wave 1: its declared stop-check change, no universe wiring\n',
         '        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B or rel in SEMANTIC_W2C or rel in SEMANTIC_EX06), rel   # Wave 1: its declared stop-check change, no universe wiring\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                      # Stage 5.4.2, pinned by its own F tests\n',
         '    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                      # Stage 5.4.2, pinned by its own F tests\n'),
        ('            assert rel in FL.NEW_W2A or rel in FL.NEW_W2C or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n',
         '            assert rel in FL.NEW_W2A or rel in FL.NEW_W2C or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06 or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n'),
        ('    js = FL.pre_w2c("frontend/product_ui.js", (ROOT / "frontend/product_ui.js").read_text(encoding="utf-8").replace("\\r\\n", "\\n"))   # Wave 2 PR C: main\'s text; its W2-7 edit: T-W05C\n',
         '    js = FL.pre_w2c("frontend/product_ui.js", FL.pre_ex06("frontend/product_ui.js", (ROOT / "frontend/product_ui.js").read_text(encoding="utf-8").replace("\\r\\n", "\\n")))   # Wave 2 PR C: main\'s text; its W2-7 edit: T-W05C\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n',
         '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Stage 7.0, pinned by its own F tests\n',
         '    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Stage 7.0, pinned by its own F tests\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ('    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B) - set(FL.SEMANTIC_W2C)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n',
         '    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B) - set(FL.SEMANTIC_W2C) - set(FL.SEMANTIC_EX06)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel))))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_market_intel_70.py': [
        ('    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # Stages 7.1-7.3: pinned by their own F tests\n',
         '    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # Stages 7.1-7.3: pinned by their own F tests\n'),
        ('    assert FL.undo_w1("config.py", now_text("config.py")) == at_base("config.py") and now_text("api/server.py") == at_base("api/server.py")   # Wave 1\'s declared config.py edit undone first\n',
         '    assert FL.undo_w1("config.py", now_text("config.py")) == at_base("config.py") and FL.pre_ex06("api/server.py", now_text("api/server.py")) == at_base("api/server.py")   # Wave 1\'s declared config.py edit undone first\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                           # Stages 7.3.1, 7.4: pinned by their own F tests\n',
         '    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                           # Stages 7.3.1, 7.4: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Stage 7.5, pinned by its own F tests\n',
         '    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Stage 7.5, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Stage 7.4, pinned by its own F tests\n',
         '    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Stage 7.4, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                           # Stages 7.3, 7.3.1: pinned by their own F tests\n',
         '    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                           # Stages 7.3, 7.3.1: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                             # Stages 7.2, 7.3: pinned by their own F tests\n',
         '    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                             # Stages 7.2, 7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n',
         '    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C\n',
         '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06\n'),
        ('        assert now(rel) == at_base(rel), rel                                   # start / stop / the shared helpers / the other installers: untouched\n',
         "        assert FL.pre_ex06(rel, now(rel)) == at_base(rel), rel                 # start / stop / the shared helpers / the other installers: untouched (EX-06's declared lines: T-X20)\n"),
    ],
    'tests/test_sip_eval_78b1.py': [
        ('    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n',
         '    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n'),
    ],
    'tests/test_strategy_validation_62.py': [
        ('from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, NEW_W2C, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n',
         'from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, NEW_W2C, NEW_EX06, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n'),
        ('NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B | NEW_W2C   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n',
         'NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B | NEW_W2C | NEW_EX06   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n'),
        ('    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B - NEW_W2C) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n',
         '    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B - NEW_W2C - NEW_EX06) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                     # Stage 7.8B-1, pinned by its own F tests\n',
         '    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                     # Stage 7.8B-1, pinned by its own F tests\n'),
    ],
    'tests/test_trader_view_55.py': [
        ('',
         'from freeze_ledger import pre_ex06, undo_ex06  # noqa: E402 - EX-06: its declared app.js / conftest edits are pinned by tests/test_ex06_freeze.py\n'),
        ('        assert undo_w1(rel, (ROOT / rel).read_bytes().replace(b"\\r\\n", b"\\n").decode("utf-8")).encode("utf-8") == blob(rel), f"{rel} differs from {BASE}"   # Wave 1: its tests/conftest.py edit undone first\n',
         '        assert undo_w1(rel, undo_ex06(rel, pre_ex06(rel, (ROOT / rel).read_bytes().replace(b"\\r\\n", b"\\n").decode("utf-8")))).encode("utf-8") == blob(rel), f"{rel} differs from {BASE}"   # Wave 1: its tests/conftest.py edit undone first\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, _now(rel))))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, _now(rel)))))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_wave1_safety_remediation.py': [
        ('    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C                                                         # Wave 2 PR A, pinned by its own F tests\n',
         '    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Wave 2 PR A, pinned by its own F tests\n'),
        ('    assert FL.undo_w1("tests/conftest.py", now_text("tests/conftest.py")) == at_base("tests/conftest.py")\n',
         '    assert FL.undo_w1("tests/conftest.py", FL.undo_ex06("tests/conftest.py", now_text("tests/conftest.py"))) == at_base("tests/conftest.py")\n'),
    ],
    'tests/test_wave2_pra.py': [
        ('    pra_version = ast.literal_eval(named_nodes(FL.pre_w2c("execution/__init__.py", now_text("execution/__init__.py")))["=VERSION"].value)   # Wave 2 PR C: PR A\'s bump as main left it (PR C\'s execution_v3: its T-W01C)\n',
         '    pra_version = ast.literal_eval(named_nodes(FL.pre_w2c("execution/__init__.py", FL.pre_ex06("execution/__init__.py", now_text("execution/__init__.py"))))["=VERSION"].value)   # Wave 2 PR C: PR A\'s bump as main left it (PR C\'s execution_v3: its T-W01C)\n'),
        ('        added = "\\n".join(l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel))).splitlines() if l not in base)\n',
         '        added = "\\n".join(l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))).splitlines() if l not in base)\n'),
        ('        now, base = named_nodes(FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel)))), named_nodes(at_base(rel))\n',
         '        now, base = named_nodes(FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))), named_nodes(at_base(rel))\n'),
        ('        u_now, u_base = unnamed(FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel)))), unnamed(at_base(rel))\n',
         '        u_now, u_base = unnamed(FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))), unnamed(at_base(rel))\n'),
        ('        assert _normalized(rel, name, FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel)))) == ast.dump(base_node), (rel, name)\n',
         '        assert _normalized(rel, name, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))) == ast.dump(base_node), (rel, name)\n'),
        ('    pre_c = FL.pre_w2c("execution/service.py", now_text("execution/service.py"))              # Wave 2 PR C: its W2-6 _reconcile detail is its own pin (T-W05C, T-W01C)\n',
         '    pre_c = FL.pre_w2c("execution/service.py", FL.pre_ex06("execution/service.py", now_text("execution/service.py")))              # Wave 2 PR C: its W2-6 _reconcile detail is its own pin (T-W05C, T-W01C)\n'),
        ('    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A) | (FL.EDITED_W2C - FL.NEW_W2A - FL.NEW_W2B)   # Wave 2 PR B / PR C: their own sets (their edits of earlier new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py / test_wave2_prc.py\n',
         '    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A) | (FL.EDITED_W2C - FL.NEW_W2A - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C)   # Wave 2 PR B / PR C / EX-06: their own sets (their edits of earlier new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py / test_wave2_prc.py\n'),
        ('        assert not [l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, now_text(rel))).splitlines() if l not in base and marker.match(l)], rel\n',
         '        assert not [l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))).splitlines() if l not in base and marker.match(l)], rel\n'),
        ('        text = FL.undo_w2b(rel, FL.undo_w2c(rel, now_text(rel)))                               # Wave 2 PR B / PR C: their own in-place edits undone first (F03B / F03C)\n',
         '        text = FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel))))                               # Wave 2 PR B / PR C: their own in-place edits undone first (F03B / F03C)\n'),
    ],
    'tests/test_wave2_pra_accounting.py': [
        ('    now61 = FL.undo_w2b("tests/test_forward_validation_61.py", FL.undo_w2c("tests/test_forward_validation_61.py", (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8")))   # Wave 2 PR B (D-1) / PR C (T-WC06): PR A\'s delta; their own edits: F02B / F03B, F02C / F03C\n',
         '    now61 = FL.undo_w2b("tests/test_forward_validation_61.py", FL.undo_w2c("tests/test_forward_validation_61.py", FL.undo_ex06("tests/test_forward_validation_61.py", (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8"))))   # Wave 2 PR B (D-1) / PR C (T-WC06): PR A\'s delta; their own edits: F02B / F03B, F02C / F03C\n'),
        ('    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", FL.undo_w2c("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8"))), at_base("tests/test_forward_paper_610a.py")\n',
         '    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", FL.undo_w2c("tests/test_forward_paper_610a.py", FL.undo_ex06("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8")))), at_base("tests/test_forward_paper_610a.py")\n'),
        ('        assert FL.undo_w2c(rel, FL.pre_w2c(rel, (ROOT / rel).read_text(encoding="utf-8"))) == at_base(rel), rel   # Wave 2 PR C (T-WC06): its W2-7 edits are its own pins\n',
         '        assert FL.undo_w2c(rel, FL.undo_ex06(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, (ROOT / rel).read_text(encoding="utf-8"))))) == at_base(rel), rel   # Wave 2 PR C (T-WC06): its W2-7 edits are its own pins\n'),
    ],
    'tests/test_wave2_pra_early_stop.py': [
        ('    src = FL.pre_w2c("forward_validation/service.py", (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8")) + (ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8")   # Wave 2 PR C: PR A\'s text\n',
         '    src = FL.pre_w2c("forward_validation/service.py", FL.pre_ex06("forward_validation/service.py", (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8"))) + (ROOT / "forward_validation" / "attention_signature.py").read_text(encoding="utf-8")   # Wave 2 PR C: PR A\'s text\n'),
    ],
    'tests/test_wave2_prb.py': [
        ('    return "\\n".join(l for l in FL.pre_w2c(rel, now_text(rel)).splitlines() if l not in base)   # Wave 2 PR C: PR B\'s lines (PR C\'s own: T-W04C)\n',
         '    return "\\n".join(l for l in FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))).splitlines() if l not in base)   # Wave 2 PR C: PR B\'s lines (PR C\'s own: T-W04C)\n'),
        ("    lit = lambda rel, name: ast.literal_eval(named_assign(FL.pre_w2c(rel, now_text(rel)), name))  # noqa: E731 - Wave 2 PR C: PR B's text (PR C adds run_id / frozen_hash)\n",
         "    lit = lambda rel, name: ast.literal_eval(named_assign(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))), name))  # noqa: E731 - Wave 2 PR C: PR B's text (PR C adds run_id / frozen_hash)\n"),
        ('        now, base = named_nodes(FL.pre_w2c(rel, now_text(rel))), named_nodes(at_base(rel))\n',
         '        now, base = named_nodes(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))), named_nodes(at_base(rel))\n'),
        ('        assert sorted(unnamed(FL.pre_w2c(rel, now_text(rel)))) == sorted(unnamed(at_base(rel))), rel                 # no new module-level statement (imports go inside)\n    store_class = ast.parse(FL.pre_w2c("forward_validation/store.py", now_text("forward_validation/store.py"))).body\n',
         '        assert sorted(unnamed(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))) == sorted(unnamed(at_base(rel))), rel                 # no new module-level statement (imports go inside)\n    store_class = ast.parse(FL.pre_w2c("forward_validation/store.py", FL.pre_ex06("forward_validation/store.py", now_text("forward_validation/store.py")))).body\n'),
        ('        assert digest(named_nodes(FL.pre_w2c(rel, now_text(rel)))[name]) == digest(named_nodes(at_base(rel))[name]), (rel, name)\n',
         '        assert digest(named_nodes(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))[name]) == digest(named_nodes(at_base(rel))[name]), (rel, name)\n'),
        ('    now, base = fn_text(FL.pre_w2c(rel, now_text(rel)), FN + "cumulative"), fn_text(at_base(rel), FN + "cumulative")   # Wave 2 PR C: PR B\'s text; the current text keeps D-5: T-W06C\n',
         '    now, base = fn_text(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))), FN + "cumulative"), fn_text(at_base(rel), FN + "cumulative")   # Wave 2 PR C: PR B\'s text; the current text keeps D-5: T-W06C\n'),
        ("    both_new, both_edited = FL.NEW_W2B | FL.NEW_W2C, FL.EDITED_W2B | (FL.EDITED_W2C - FL.NEW_W2B)   # Wave 2 PR C: its own sets (its edits of PR B's new files stay new since BASE_W2B), pinned by tests/test_wave2_prc.py\n",
         "    both_new, both_edited = FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06, FL.EDITED_W2B | (FL.EDITED_W2C - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2B - FL.NEW_W2C)   # Wave 2 PR C / EX-06: its own sets (its edits of PR B's new files stay new since BASE_W2B), pinned by tests/test_wave2_prc.py\n"),
        ('        b, n = defs(at_base(rel)), defs(FL.undo_w2c(rel, now_text(rel)))                          # Wave 2 PR C: its own in-place edits undone first (F02C)\n',
         '        b, n = defs(at_base(rel)), defs(FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel))))                          # Wave 2 PR C: its own in-place edits undone first (F02C)\n'),
        ('        assert FL.undo_w2b(rel, FL.undo_w2c(rel, now_text(rel))) == at_base(rel), rel           # now -> (F03C) 875bc06d -> 50cb2d7b, byte for byte\n',
         '        assert FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel)))) == at_base(rel), rel           # now -> (F03C) 875bc06d -> 50cb2d7b, byte for byte\n'),
        ('    now, base = now_text(rel), at_base(rel)\n',
         "    now, base = FL.pre_ex06(rel, now_text(rel)), at_base(rel)                               # EX-06: PR B's text (EX-06's own edit: T-X20)\n"),
        ("    assert undone == base and rel in FL.EDITED_W2B and rel in FL.LEDGER and FL.sha(now) == FL.LEDGER[rel]   # the edit alone; the ledger holds PR B's text\n",
         '    assert undone == base and rel in FL.EDITED_W2B and rel in FL.LEDGER and FL.sha(now_text(rel)) == FL.LEDGER[rel]   # the edit alone; the ledger holds the current text (EX-06: F03X)\n'),
    ],
    'tests/test_wave2_prc.py': [
        ('',
         'def prc_files(base):\n    """EX-06: the change set between `base` and main after PR #53 (fixed commits). EX-06\'s delta: tests/test_ex06_freeze.py."""\n    return set(_git("diff", "--name-only", base, FL.BASE_EX06).split())\n\n\n'),
        ('    changed = changed_files(FL.BASE_W2C)\n',
         "    changed = prc_files(FL.BASE_W2C)                                                            # EX-06: PR C's own change set (fixed commits)\n"),
        ('    assert FL.BASE_W2C == "875bc06dd08b36ace1d3930877f90cb96ff81b08" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W2C}^{{commit}}"], cwd=ROOT).returncode == 0\n    changed = changed_files(FL.BASE_W2C)\n    assert changed == FL.NEW_W2C | FL.EDITED_W2C, {"unexpected": sorted(changed - FL.NEW_W2C - FL.EDITED_W2C), "missing": sorted((FL.NEW_W2C | FL.EDITED_W2C) - changed)}\n',
         '    assert FL.BASE_W2C == "875bc06dd08b36ace1d3930877f90cb96ff81b08" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W2C}^{{commit}}"], cwd=ROOT).returncode == 0\n    changed = prc_files(FL.BASE_W2C)                                                            # EX-06: PR C\'s own change set (fixed commits)\n    assert changed == FL.NEW_W2C | FL.EDITED_W2C, {"unexpected": sorted(changed - FL.NEW_W2C - FL.EDITED_W2C), "missing": sorted((FL.NEW_W2C | FL.EDITED_W2C) - changed)}\n'),
        ('    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]\n',
         '    prc_groups = subprocess.run(["git", "show", f"{FL.BASE_EX06}:.github/ci/pytest_groups.json"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")   # EX-06: the manifest as PR #53 left it (EX-06\'s entries: F01X)\n    groups, base_groups = json.loads(prc_groups)["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]\n'),
        ('        b, n = defs(at_base(rel)), defs(now_text(rel))\n',
         '        b, n = defs(at_base(rel)), defs(FL.undo_ex06(rel, now_text(rel)))                        # EX-06: its own in-place edits undone first (F02X)\n'),
        ('        assert FL.pre_w2c(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel\n',
         '        assert FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))) == at_base(rel) and now_text(rel) != at_base(rel), rel\n'),
        ('        assert FL.undo_w2c(rel, now_text(rel)) == at_base(rel), rel                             # now -> 875bc06d, byte for byte\n    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")\n',
         '        assert FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel))) == at_base(rel), rel                             # now -> 875bc06d, byte for byte\n    main_fl = at_base("tests/freeze_ledger.py")\n    fl = subprocess.run(["git", "show", f"{FL.BASE_EX06}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n")   # EX-06: the ledger as PR #53 left it\n'),
        ('    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_W2C and set(main_ledger) <= set(FL.LEDGER)\n    assert {k for k in main_ledger if main_ledger[k] != FL.LEDGER[k]} == FL.EDITED_W2C & set(main_ledger)\n',
         '    prc_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # EX-06: PR C\'s ledger; EX-06\'s re-hashes are its F03X\n    assert set(prc_ledger) - set(main_ledger) == FL.NEW_LEDGER_W2C and set(main_ledger) <= set(prc_ledger)\n    assert {k for k in main_ledger if main_ledger[k] != prc_ledger[k]} == FL.EDITED_W2C & set(main_ledger)\n'),
        ("        assert FL.base_text(rel) == at_base(rel) == FL.undo_w2c(rel, FL.pre_w2c(rel, now_text(rel))), rel   # unchanged from BASE_68 to main: the view IS main's text\n",
         "        assert FL.base_text(rel) == at_base(rel) == FL.undo_w2c(rel, FL.undo_ex06(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))), rel   # unchanged from BASE_68 to main: the view IS main's text\n"),
    ],
    'tests/test_windows_runner_60.py': [
        ('        s.sendall(f"GET {path} HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n".encode())\n',
         '        s.sendall(f"GET {path} HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\nX-Stock-Agent-Intent: dashboard\\r\\nConnection: close\\r\\n\\r\\n".encode())   # EX-06: a local tool\\\'s read\n'),
    ],
}
SEMANTIC_TESTS_EX06 = {                                                                      # EX-06: every changed test function -> its reason (F02X)
    'tests/conftest.py::_LocalTestClient': 'EX06-B01 behavioural (D-20): the default test client is the trusted local dashboard in a browser - the loopback client and Host, the shapes headless Edge sends (intent, same-origin fetch metadata, Referer, Origin on a state change; a navigation for a page; Origin only on a WebSocket handshake) and the websocket_connect override (starlette hard-codes ws://testserver)',
    'tests/conftest.py::_headers': "EX06-B02 behavioural (D-20): the case-insensitive header merge the dashboard test client uses, so a test's own header always wins and is never sent twice",
    'tests/test_bot_activity_68.py::test_F01_every_change_since_main_is_a_new_file_or_an_exact_ledger_entry': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_bot_activity_68.py::test_F02_the_executor_and_scheduler_only_gained_display_bookkeeping_and_every_guard_line_is_kept': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_chart_empty_state_611fix.py::test_F01_the_fix_changes_exactly_the_declared_files_and_no_backend_file': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_decision_flow_610c.py::test_F01_stage_610c_changes_exactly_its_declared_files_and_no_execution_strategy_risk_or_policy_file': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_decision_flow_610c.py::test_F02_execution_setups_auto_run_and_the_live_coverage_are_byte_identical_and_only_the_router_and_three_inert_flags_were_added': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_dynamic_universe_610c.py::test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_engine_health_610d.py::test_F01_the_change_set_is_exactly_the_declared_files_and_touches_no_execution_strategy_or_safety_file': "EX06-F06 freeze bookkeeping: the stage's union and declared-semantic exemption gain EX-06's sets and its reads of later stages' files go through pre_ex06 / undo_ex06 first, as main left them; EX-06's own delta is pinned by tests/test_ex06_freeze.py",
    'tests/test_evidence_gate_541.py::test_F01_the_fix_changes_one_function_and_nothing_in_the_setup_engine_ppe_evidence_or_execution': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_evidence_refresh_542.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_forward_paper_610a.py::test_F01_stage_610a_changes_exactly_its_declared_files_and_no_strategy_risk_broker_or_market_data_file': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_forward_paper_610a.py::test_F02_the_order_decision_logic_is_unchanged_and_the_executor_only_gained_failure_swallowing_markers': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_market_intel_70.py::test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_market_intel_70.py::test_F02_the_frozen_hashes_and_settings_are_unchanged_and_config_declares_no_market_intel_setting': "EX06-F03 freeze bookkeeping: a raw reader of a file EX-06 edits (server.py, app.js, the shared ops helper, conftest) reads it as main left it (pre_ex06 / undo_ex06); EX-06's declared lines are pinned by T-X20 and the undo by F03X",
    'tests/test_market_intel_collector_73.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_market_intel_quality_74.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_market_intel_schema_v2_731.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_market_intel_sources_72.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_market_intel_store_71.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_market_scan_75.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_robinhood_portfolio_69.py::test_F01_stage_69_changes_exactly_its_files_and_no_execution_strategy_or_risk_file': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_runtime_reliability_ops.py::test_F01_the_change_set_is_exactly_the_declared_ops_files_and_touches_no_trading_code': "EX06-F03 freeze bookkeeping: a raw reader of a file EX-06 edits (server.py, app.js, the shared ops helper, conftest) reads it as main left it (pre_ex06 / undo_ex06); EX-06's declared lines are pinned by T-X20 and the undo by F03X",
    'tests/test_sip_eval_78b1.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_strategy_validation_62.py::=NEW_62': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_strategy_validation_62.py::test_F01_main_plus_only_the_stage_62_files_and_exact_edits': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_swing_pullback_78.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_trader_view_55.py::test_23_prior_stage_files_identical_to_base_and_wiring_is_additive': "EX06-F03 freeze bookkeeping: a raw reader of a file EX-06 edits (server.py, app.js, the shared ops helper, conftest) reads it as main left it (pre_ex06 / undo_ex06); EX-06's declared lines are pinned by T-X20 and the undo by F03X",
    'tests/test_unified_exposure_gate_691.py::test_F01_stage_691_changes_exactly_its_declared_files_and_no_risk_strategy_broker_or_database_file': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_unified_exposure_gate_691.py::test_F02_the_execution_semantics_changed_on_purpose_and_exactly_as_declared': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave1_safety_remediation.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_market_data_broker_or_research_file_changed': "EX06-F01 freeze bookkeeping: the stage's change-set union or its declared-semantic exemption gains EX-06's sets (NEW_EX06 / EDITED_EX06 / SEMANTIC_EX06), exactly as every later stage did; EX-06's own delta is pinned by tests/test_ex06_freeze.py (F01X, T-X20)",
    'tests/test_wave1_safety_remediation.py::test_F02_the_safety_semantics_changed_exactly_as_declared_and_no_threshold_or_frozen_control_moved': "EX06-F03 freeze bookkeeping: a raw reader of a file EX-06 edits (server.py, app.js, the shared ops helper, conftest) reads it as main left it (pre_ex06 / undo_ex06); EX-06's declared lines are pinned by T-X20 and the undo by F03X",
    'tests/test_wave2_pra.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': "EX06-F04 freeze bookkeeping: PR A's / PR B's own pins exclude EX-06's sets (its edits of their new files stay new since their bases) and read the harness flow as PR B left it (PR C's method for PR B's pins); EX-06's lines are pinned by T-X20",
    'tests/test_wave2_pra.py::test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_pra.py::test_TR05pre_reconciliation_output_is_identical_to_the_baseline': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_pra.py::test_TW03_pr_a_bumps_the_execution_version_and_the_v1_pins_are_the_baseline': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_pra.py::test_TW04_the_production_diff_is_exactly_pr_a_and_names_no_pr_c_object': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_pra.py::test_TW05_only_the_listed_functions_changed_and_only_the_listed_names_are_new': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_pra_accounting.py::test_TA42_downstream_compatibility_capture_keeps_raw_values_and_the_known_consumers_are_unchanged': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_pra_early_stop.py::test_TE13p_pr_a_has_no_run_acknowledgement_path_so_an_element_on_record_stops': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prb.py::added_lines': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prb.py::test_F01B_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': "EX06-F04 freeze bookkeeping: PR A's / PR B's own pins exclude EX-06's sets (its edits of their new files stay new since their bases) and read the harness flow as PR B left it (PR C's method for PR B's pins); EX-06's lines are pinned by T-X20",
    'tests/test_wave2_prb.py::test_F02B_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prb.py::test_F03B_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prb.py::test_F04B_the_harness_flow_gives_k3_observations_exactly_and_keeps_t_a42s_lines_in_place': "EX06-F04 freeze bookkeeping: PR A's / PR B's own pins exclude EX-06's sets (its edits of their new files stay new since their bases) and read the harness flow as PR B left it (PR C's method for PR B's pins); EX-06's lines are pinned by T-X20",
    'tests/test_wave2_prb.py::test_TW04B_the_production_diff_is_pr_a_plus_pr_b_and_pr_b_adds_no_pr_c_or_order_token': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prb.py::test_TW05B_only_the_listed_functions_changed_and_only_the_listed_names_are_new': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prb.py::test_TW06B_d5_unreadable_evidence_is_excluded_from_acceptance_by_exactly_the_declared_lines': "EX06-F02 freeze bookkeeping: inside the later stages' views (pre_w2c / undo_w2c) the test reads EX-06's files as main left them first (pre_ex06 for a wiring file, undo_ex06 for an edited test file), where it passed; EX-06's own lines are pinned by T-X20 and F03X",
    'tests/test_wave2_prc.py::prc_files': "EX06-F05 freeze bookkeeping: PR C's own pins read PR C's change set between fixed commits (BASE_W2C..BASE_EX06), the CI manifest and the ledger as PR #53 left them, and its test files with EX-06's edits undone (PR C's method for PR B's pins)",
    'tests/test_wave2_prc.py::test_F01C_the_change_set_is_exactly_the_declared_files': "EX06-F05 freeze bookkeeping: PR C's own pins read PR C's change set between fixed commits (BASE_W2C..BASE_EX06), the CI manifest and the ledger as PR #53 left them, and its test files with EX-06's edits undone (PR C's method for PR B's pins)",
    'tests/test_wave2_prc.py::test_F02C_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added': "EX06-F05 freeze bookkeeping: PR C's own pins read PR C's change set between fixed commits (BASE_W2C..BASE_EX06), the CI manifest and the ledger as PR #53 left them, and its test files with EX-06's edits undone (PR C's method for PR B's pins)",
    'tests/test_wave2_prc.py::test_F03C_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "EX06-F05 freeze bookkeeping: PR C's own pins read PR C's change set between fixed commits (BASE_W2C..BASE_EX06), the CI manifest and the ledger as PR #53 left them, and its test files with EX-06's edits undone (PR C's method for PR B's pins)",
    'tests/test_wave2_prc.py::test_TW04C_the_production_diff_is_exactly_design_1_3_and_pr_c_adds_no_order_mode_or_broker_token': "EX06-F05 freeze bookkeeping: PR C's own pins read PR C's change set between fixed commits (BASE_W2C..BASE_EX06), the CI manifest and the ledger as PR #53 left them, and its test files with EX-06's edits undone (PR C's method for PR B's pins)",
    'tests/test_windows_runner_60.py::http_raw': "EX06-B03 behavioural (D-14): the frozen Windows runner probe is a local tool reading /api/execution/status (which may persist the safety evaluation), so its raw GET carries X-Stock-Agent-Intent; its portless Host stays as it was (D-1 b')",
}
BASE_XSS01 = "9df1717a0eb20e666a2e641fbcabac466b30ad2d"                                 # main: EX-06 hardening merged (PR #55); the XSS-01 base
NEW_XSS01 = {'tests/frontend/xss01_app_js.test.cjs', 'tests/frontend/xss01_raw_interpolations.json', 'tests/test_xss01_csp.py', 'tests/test_xss01_freeze.py'}   # XSS-01: its new files
EDITED_XSS01 = {'.github/ci/pytest_groups.json', 'api/local_gate.py', 'frontend/app.js', 'frontend/command_center.js', 'frontend/evidence_research.js', 'frontend/market.js', 'tests/freeze_ledger.py', 'tests/test_bot_activity_68.py', 'tests/test_chart_empty_state_611fix.py', 'tests/test_decision_flow_610c.py', 'tests/test_dynamic_universe_610c.py', 'tests/test_engine_health_610d.py', 'tests/test_evidence_gate_541.py', 'tests/test_evidence_refresh_542.py', 'tests/test_ex06_freeze.py', 'tests/test_ex06_local_gate.py', 'tests/test_forward_paper_610a.py', 'tests/test_market_intel_70.py', 'tests/test_market_intel_collector_73.py', 'tests/test_market_intel_quality_74.py', 'tests/test_market_intel_schema_v2_731.py', 'tests/test_market_intel_sources_72.py', 'tests/test_market_intel_store_71.py', 'tests/test_market_scan_75.py', 'tests/test_robinhood_portfolio_69.py', 'tests/test_runtime_reliability_ops.py', 'tests/test_sip_eval_78b1.py', 'tests/test_strategy_validation_62.py', 'tests/test_swing_pullback_78.py', 'tests/test_trader_view_55.py', 'tests/test_unified_exposure_gate_691.py', 'tests/test_wave1_safety_remediation.py', 'tests/test_wave2_pra.py', 'tests/test_wave2_pra_accounting.py', 'tests/test_wave2_prb.py', 'tests/test_wave2_prc.py'}   # the escaped dashboard, the three link sites, the gate's CSP line, the CI entry, the EX-06 pins' views, the older freeze tests' unions / views, bookkeeping
SEMANTIC_XSS01 = {                                                                          # XSS-01: the INTENTIONAL change, file by file
    'api/local_gate.py': "the gate's security headers gain a Content Security Policy (frame-ancestors 'none' kept; script-src 'self', object-src 'none', base-uri 'none', form-action 'self', frame-src 'none') and X-Content-Type-Options: nosniff on every response (R3); no rule changes",
    'frontend/app.js': "every string inserted into innerHTML is escaped (esc), an external URL becomes a link only when it is an absolute http(s) URL (safeHref / extLink), API path parameters are URL-encoded, fmt.score coerces, and the HTML builders are pure functions in the marked XSS-01 render block (R1, R2, R4); the EX-06 wrapper block is untouched",
    'frontend/command_center.js': "the news headline link is built by the shared safeHref / extLink helper (only an absolute http(s) URL is a link; noopener noreferrer) (X-1)",
    'frontend/evidence_research.js': "the evidence item's source link is built by the shared safeHref / extLink helper (only an absolute http(s) URL is a link; noopener noreferrer) (X-1)",
    'frontend/market.js': "the sourced-news headline link is built by the shared safeHref / extLink helper (only an absolute http(s) URL is a link; noopener noreferrer) (X-1)",
}
INSTRUMENTATION_XSS01: dict = {}                                                           # XSS-01: no executor instrumentation
NEW_LEDGER_XSS01: set = {'frontend/command_center.js', 'frontend/evidence_research.js', 'frontend/market.js'}   # main's files XSS-01 registers (unchanged since BASE_68); it also re-hashes four already-registered ones: frontend/app.js, .github/ci/pytest_groups.json and the two registered older tests it bookkeeps (test_strategy_validation_62, test_trader_view_55)
UNDO_XSS01 = {                                                                               # XSS-01's in-place edits of older test files: (main, XSS-01)
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06 | FL.NEW_XSS01, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06 - FL.NEW_XSS01),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06 | FL.NEW_XSS01) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06\n',
         '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n",
         "            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06 or rel in FL.SEMANTIC_XSS01, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n"),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B, SEMANTIC_W2C, SEMANTIC_EX06  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n',
         'from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A, SEMANTIC_W2B, SEMANTIC_W2C, SEMANTIC_EX06, SEMANTIC_XSS01  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n'),
        ('        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B or rel in SEMANTIC_W2C or rel in SEMANTIC_EX06), rel   # Wave 1: its declared stop-check change, no universe wiring\n',
         '        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A or rel in SEMANTIC_W2B or rel in SEMANTIC_W2C or rel in SEMANTIC_EX06 or rel in SEMANTIC_XSS01), rel   # Wave 1: its declared stop-check change, no universe wiring\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                      # Stage 5.4.2, pinned by its own F tests\n',
         '    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                      # Stage 5.4.2, pinned by its own F tests\n'),
        ('            assert rel in FL.NEW_W2A or rel in FL.NEW_W2C or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06 or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n',
         '            assert rel in FL.NEW_W2A or rel in FL.NEW_W2C or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06 or rel in FL.SEMANTIC_XSS01 or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n',
         '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A or rel in FL.SEMANTIC_W2B or rel in FL.SEMANTIC_W2C or rel in FL.SEMANTIC_EX06 or rel in FL.SEMANTIC_XSS01, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Stage 7.0, pinned by its own F tests\n',
         '    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                         # Stage 7.0, pinned by its own F tests\n'),
    ],
    'tests/test_ex06_freeze.py': [
        ('def defs(text):\n',
         'def ex06_files(base):\n    """XSS-01: EX-06\'s change set between fixed commits (`base`..main after PR #55, FL.BASE_XSS01); XSS-01\'s own delta is pinned by tests/test_xss01_freeze.py."""\n    return set(_git("diff", "--name-only", base, FL.BASE_XSS01).split())\n\n\ndef defs(text):\n'),
        ('def test_TX20_the_production_diff_is_exactly_the_declared_lines_and_no_trading_file_changed():\n    changed = changed_files(FL.BASE_EX06)\n',
         "def test_TX20_the_production_diff_is_exactly_the_declared_lines_and_no_trading_file_changed():\n    changed = ex06_files(FL.BASE_EX06)                                                          # XSS-01: EX-06's own change set (fixed commits)\n"),
        ('    js, base_js = now_text(APP_JS), at_base(APP_JS)\n',
         "    js, base_js = FL.pre_xss01(APP_JS, now_text(APP_JS)), at_base(APP_JS)                     # XSS-01: app.js as main left it after PR #55 (XSS-01's escaping: tests/test_xss01_freeze.py)\n"),
        ('    assert FL.BASE_EX06 == "46ebc65671435a02a0c90f6cd40f7d277ab15da2" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_EX06}^{{commit}}"], cwd=ROOT).returncode == 0\n    changed = changed_files(FL.BASE_EX06)\n',
         '    assert FL.BASE_EX06 == "46ebc65671435a02a0c90f6cd40f7d277ab15da2" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_EX06}^{{commit}}"], cwd=ROOT).returncode == 0\n    changed = ex06_files(FL.BASE_EX06)                                                          # XSS-01: EX-06\'s own change set (fixed commits)\n'),
        ('    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]\n',
         '    ex06_groups = subprocess.run(["git", "show", f"{FL.BASE_XSS01}:.github/ci/pytest_groups.json"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")   # XSS-01: the manifest as PR #55 left it (XSS-01\'s entries: tests/test_xss01_freeze.py)\n    groups, base_groups = json.loads(ex06_groups)["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]\n'),
        ('        b, n = defs(at_base(rel)), defs(now_text(rel))\n',
         '        b, n = defs(at_base(rel)), defs(FL.undo_xss01(rel, now_text(rel)))                     # XSS-01: its own in-place edits undone first (tests/test_xss01_freeze.py)\n'),
        ('        assert FL.undo_ex06(rel, now_text(rel)) == at_base(rel), rel                          # now -> 46ebc656, byte for byte\n    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")\n',
         '        assert FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel))) == at_base(rel), rel       # now -> (XSS-01 undone) 9df1717a -> 46ebc656, byte for byte\n    fl = subprocess.run(["git", "show", f"{FL.BASE_XSS01}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\\r\\n", "\\n")   # XSS-01: the ledger as PR #55 left it\n    main_fl = at_base("tests/freeze_ledger.py")\n'),
        ('    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_EX06 and set(main_ledger) <= set(FL.LEDGER)\n',
         '    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_EX06 | FL.NEW_LEDGER_XSS01 and set(main_ledger) <= set(FL.LEDGER)   # XSS-01: its registrations are its own pin (F05Y)\n'),
        ('    changed = changed_files(BASE_EX06H)\n',
         "    changed = ex06_files(BASE_EX06H)                                                            # XSS-01: the hardening's own change set (fixed commits)\n"),
        ('    base, now = run.stdout.decode("utf-8").replace("\\r\\n", "\\n").split("\\n"), now_text("api/local_gate.py").split("\\n")\n',
         '    base, now = run.stdout.decode("utf-8").replace("\\r\\n", "\\n").split("\\n"), FL.pre_xss01("api/local_gate.py", now_text("api/local_gate.py")).split("\\n")   # XSS-01: the gate as PR #55 left it (its CSP line: tests/test_xss01_freeze.py)\n'),
    ],
    'tests/test_ex06_local_gate.py': [
        ('\n',
         'CSP_XSS01 = "frame-ancestors \'none\'; script-src \'self\'; object-src \'none\'; base-uri \'none\'; form-action \'self\'; frame-src \'none\'"   # XSS-01 (R3): the policy on every response; pinned by tests/test_xss01_csp.py\n\n'),
        ('    assert r.headers["x-frame-options"] == "DENY" and r.headers["content-security-policy"] == "frame-ancestors \'none\'" and r.headers["cache-control"] == "no-store"\n',
         '    assert r.headers["x-frame-options"] == "DENY" and r.headers["content-security-policy"] == CSP_XSS01 and r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"   # XSS-01 (R3): the CSP / nosniff value is pinned by tests/test_xss01_csp.py\n'),
        ('    assert ok.status_code == 200 and ok.headers["x-frame-options"] == "DENY" and ok.headers["content-security-policy"] == "frame-ancestors \'none\'"\n',
         '    assert ok.status_code == 200 and ok.headers["x-frame-options"] == "DENY" and ok.headers["content-security-policy"] == CSP_XSS01 and ok.headers["x-content-type-options"] == "nosniff"   # XSS-01 (R3)\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ('    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B) - set(FL.SEMANTIC_W2C) - set(FL.SEMANTIC_EX06)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n',
         '    assert not (changed - set(FL.SEMANTIC_W1) - set(FL.SEMANTIC_W2B) - set(FL.SEMANTIC_W2C) - set(FL.SEMANTIC_EX06) - set(FL.SEMANTIC_XSS01)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # Stages 7.1-7.3: pinned by their own F tests\n',
         '    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01   # Stages 7.1-7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                           # Stages 7.3.1, 7.4: pinned by their own F tests\n',
         '    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                           # Stages 7.3.1, 7.4: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Stage 7.5, pinned by its own F tests\n',
         '    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                         # Stage 7.5, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Stage 7.4, pinned by its own F tests\n',
         '    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                         # Stage 7.4, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                           # Stages 7.3, 7.3.1: pinned by their own F tests\n',
         '    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                           # Stages 7.3, 7.3.1: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                             # Stages 7.2, 7.3: pinned by their own F tests\n',
         '    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                             # Stages 7.2, 7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n',
         '    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06\n',
         '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01\n'),
    ],
    'tests/test_sip_eval_78b1.py': [
        ('    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n',
         '    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n'),
    ],
    'tests/test_strategy_validation_62.py': [
        ('from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, NEW_W2C, NEW_EX06, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n',
         'from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_611FIX, NEW_OPS, NEW_610D, NEW_542, NEW_70, NEW_71, NEW_72, NEW_73, NEW_731, NEW_74, NEW_75, NEW_78, NEW_78B1, NEW_W1, NEW_W2A, NEW_W2B, NEW_W2C, NEW_EX06, NEW_XSS01, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)\n'),
        ('NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B | NEW_W2C | NEW_EX06   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n',
         'NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_611FIX | NEW_OPS | NEW_610D | NEW_542 | NEW_70 | NEW_71 | NEW_72 | NEW_73 | NEW_731 | NEW_74 | NEW_75 | NEW_78 | NEW_78B1 | NEW_W1 | NEW_W2A | NEW_W2B | NEW_W2C | NEW_EX06 | NEW_XSS01   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else\n'),
        ('    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B - NEW_W2C - NEW_EX06) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n',
         '    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691 - NEW_OPS - NEW_W2A - NEW_W2B - NEW_W2C - NEW_EX06 - NEW_XSS01) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1\'s new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                     # Stage 7.8B-1, pinned by its own F tests\n',
         '    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                     # Stage 7.8B-1, pinned by its own F tests\n'),
    ],
    'tests/test_trader_view_55.py': [
        ('from freeze_ledger import pre_ex06, undo_ex06  # noqa: E402 - EX-06: its declared app.js / conftest edits are pinned by tests/test_ex06_freeze.py\n',
         'from freeze_ledger import pre_ex06, undo_ex06, pre_xss01, undo_xss01  # noqa: E402 - EX-06 / XSS-01: its declared app.js / conftest edits are pinned by tests/test_ex06_freeze.py\n'),
        ('        assert undo_w1(rel, undo_ex06(rel, pre_ex06(rel, (ROOT / rel).read_bytes().replace(b"\\r\\n", b"\\n").decode("utf-8")))).encode("utf-8") == blob(rel), f"{rel} differs from {BASE}"   # Wave 1: its tests/conftest.py edit undone first\n',
         '        assert undo_w1(rel, undo_ex06(rel, undo_xss01(rel, pre_ex06(rel, pre_xss01(rel, (ROOT / rel).read_bytes().replace(b"\\r\\n", b"\\n").decode("utf-8")))))).encode("utf-8") == blob(rel), f"{rel} differs from {BASE}"   # Wave 1: its tests/conftest.py edit undone first\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
    ],
    'tests/test_wave1_safety_remediation.py': [
        ('    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06                                                         # Wave 2 PR A, pinned by its own F tests\n',
         '    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                         # Wave 2 PR A, pinned by its own F tests\n'),
    ],
    'tests/test_wave2_pra.py': [
        ('    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A) | (FL.EDITED_W2C - FL.NEW_W2A - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C)   # Wave 2 PR B / PR C / EX-06: their own sets (their edits of earlier new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py / test_wave2_prc.py\n',
         '    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06 | FL.NEW_XSS01, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A) | (FL.EDITED_W2C - FL.NEW_W2A - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C) | (FL.EDITED_XSS01 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06)   # Wave 2 PR B / PR C / EX-06: their own sets (their edits of earlier new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py / test_wave2_prc.py\n'),
        ('        text = FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel))))                               # Wave 2 PR B / PR C: their own in-place edits undone first (F03B / F03C)\n',
         '        text = FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel)))))                               # Wave 2 PR B / PR C: their own in-place edits undone first (F03B / F03C)\n'),
    ],
    'tests/test_wave2_pra_accounting.py': [
        ('    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", FL.undo_w2c("tests/test_forward_paper_610a.py", FL.undo_ex06("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8")))), at_base("tests/test_forward_paper_610a.py")\n',
         '    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", FL.undo_w2c("tests/test_forward_paper_610a.py", FL.undo_ex06("tests/test_forward_paper_610a.py", FL.undo_xss01("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8"))))), at_base("tests/test_forward_paper_610a.py")\n'),
        ('        assert FL.undo_w2c(rel, FL.undo_ex06(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, (ROOT / rel).read_text(encoding="utf-8"))))) == at_base(rel), rel   # Wave 2 PR C (T-WC06): its W2-7 edits are its own pins\n',
         '        assert FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, (ROOT / rel).read_text(encoding="utf-8")))))) == at_base(rel), rel   # Wave 2 PR C (T-WC06): its W2-7 edits are its own pins\n'),
    ],
    'tests/test_wave2_prb.py': [
        ("    both_new, both_edited = FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06, FL.EDITED_W2B | (FL.EDITED_W2C - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2B - FL.NEW_W2C)   # Wave 2 PR C / EX-06: its own sets (its edits of PR B's new files stay new since BASE_W2B), pinned by tests/test_wave2_prc.py\n",
         "    both_new, both_edited = FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06 | FL.NEW_XSS01, FL.EDITED_W2B | (FL.EDITED_W2C - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2B - FL.NEW_W2C) | (FL.EDITED_XSS01 - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06)   # Wave 2 PR C / EX-06: its own sets (its edits of PR B's new files stay new since BASE_W2B), pinned by tests/test_wave2_prc.py\n"),
        ('        b, n = defs(at_base(rel)), defs(FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel))))                          # Wave 2 PR C: its own in-place edits undone first (F02C)\n',
         '        b, n = defs(at_base(rel)), defs(FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel)))))                          # Wave 2 PR C: its own in-place edits undone first (F02C)\n'),
        ('        assert FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel)))) == at_base(rel), rel           # now -> (F03C) 875bc06d -> 50cb2d7b, byte for byte\n',
         '        assert FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel))))) == at_base(rel), rel           # now -> (F03C) 875bc06d -> 50cb2d7b, byte for byte\n'),
    ],
    'tests/test_wave2_prc.py': [
        ('        b, n = defs(at_base(rel)), defs(FL.undo_ex06(rel, now_text(rel)))                        # EX-06: its own in-place edits undone first (F02X)\n',
         '        b, n = defs(at_base(rel)), defs(FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel))))                        # EX-06: its own in-place edits undone first (F02X)\n'),
        ('        assert FL.undo_w2c(rel, FL.undo_ex06(rel, now_text(rel))) == at_base(rel), rel                             # now -> 875bc06d, byte for byte\n',
         '        assert FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel)))) == at_base(rel), rel                             # now -> 875bc06d, byte for byte\n'),
        ("        assert FL.base_text(rel) == at_base(rel) == FL.undo_w2c(rel, FL.undo_ex06(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))), rel   # unchanged from BASE_68 to main: the view IS main's text\n",
         "        assert FL.base_text(rel) == at_base(rel) == FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))))), rel   # unchanged from BASE_68 to main: the view IS main's text\n"),
    ],
}
SEMANTIC_TESTS_XSS01 = {                                                                     # XSS-01: every changed test function -> its reason
    'tests/test_bot_activity_68.py::test_F01_every_change_since_main_is_a_new_file_or_an_exact_ledger_entry': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_chart_empty_state_611fix.py::test_F01_the_fix_changes_exactly_the_declared_files_and_no_backend_file': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_decision_flow_610c.py::test_F01_stage_610c_changes_exactly_its_declared_files_and_no_execution_strategy_risk_or_policy_file': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_decision_flow_610c.py::test_F02_execution_setups_auto_run_and_the_live_coverage_are_byte_identical_and_only_the_router_and_three_inert_flags_were_added': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_dynamic_universe_610c.py::test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_engine_health_610d.py::test_F01_the_change_set_is_exactly_the_declared_files_and_touches_no_execution_strategy_or_safety_file': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_evidence_gate_541.py::test_F01_the_fix_changes_one_function_and_nothing_in_the_setup_engine_ppe_evidence_or_execution': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_evidence_refresh_542.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_freeze.py::ex06_files': "XSS01-F05 freeze bookkeeping: EX-06's own pins read EX-06's change set between fixed commits (BASE_EX06 / BASE_EX06H .. BASE_XSS01), the CI manifest, the ledger, app.js and the gate as PR #55 left them, and its test files with XSS-01's edits undone first (EX-06's method for PR C's pins); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_freeze.py::test_F01X_the_change_set_is_exactly_the_declared_files': "XSS01-F05 freeze bookkeeping: EX-06's own pins read EX-06's change set between fixed commits (BASE_EX06 / BASE_EX06H .. BASE_XSS01), the CI manifest, the ledger, app.js and the gate as PR #55 left them, and its test files with XSS-01's edits undone first (EX-06's method for PR C's pins); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_freeze.py::test_F02X_every_changed_older_test_function_is_declared': "XSS01-F05 freeze bookkeeping: EX-06's own pins read EX-06's change set between fixed commits (BASE_EX06 / BASE_EX06H .. BASE_XSS01), the CI manifest, the ledger, app.js and the gate as PR #55 left them, and its test files with XSS-01's edits undone first (EX-06's method for PR C's pins); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_freeze.py::test_F03X_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "XSS01-F05 freeze bookkeeping: EX-06's own pins read EX-06's change set between fixed commits (BASE_EX06 / BASE_EX06H .. BASE_XSS01), the CI manifest, the ledger, app.js and the gate as PR #55 left them, and its test files with XSS-01's edits undone first (EX-06's method for PR C's pins); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_freeze.py::test_TX20_the_production_diff_is_exactly_the_declared_lines_and_no_trading_file_changed': "XSS01-F05 freeze bookkeeping: EX-06's own pins read EX-06's change set between fixed commits (BASE_EX06 / BASE_EX06H .. BASE_XSS01), the CI manifest, the ledger, app.js and the gate as PR #55 left them, and its test files with XSS-01's edits undone first (EX-06's method for PR C's pins); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_freeze.py::test_TX21_the_hardening_changes_exactly_its_declared_files_and_the_gates_exact_lines': "XSS01-F05 freeze bookkeeping: EX-06's own pins read EX-06's change set between fixed commits (BASE_EX06 / BASE_EX06H .. BASE_XSS01), the CI manifest, the ledger, app.js and the gate as PR #55 left them, and its test files with XSS-01's edits undone first (EX-06's method for PR C's pins); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_ex06_local_gate.py::=CSP_XSS01': "XSS01-B01 behavioural (R3): the gate's Content-Security-Policy and X-Content-Type-Options values asserted on a refusal and on an allowed response are XSS-01's (CSP_XSS01; the value is pinned by tests/test_xss01_csp.py)",
    'tests/test_ex06_local_gate.py::test_X12_refusal_contract_log_line_framing_and_json_body': "XSS01-B01 behavioural (R3): the gate's Content-Security-Policy and X-Content-Type-Options values asserted on a refusal and on an allowed response are XSS-01's (CSP_XSS01; the value is pinned by tests/test_xss01_csp.py)",
    'tests/test_forward_paper_610a.py::test_F01_stage_610a_changes_exactly_its_declared_files_and_no_strategy_risk_broker_or_market_data_file': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_intel_70.py::test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_intel_collector_73.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_intel_quality_74.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_intel_schema_v2_731.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_intel_sources_72.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_intel_store_71.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_market_scan_75.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_robinhood_portfolio_69.py::test_F01_stage_69_changes_exactly_its_files_and_no_execution_strategy_or_risk_file': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_runtime_reliability_ops.py::test_F01_the_change_set_is_exactly_the_declared_ops_files_and_touches_no_trading_code': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_sip_eval_78b1.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_strategy_validation_62.py::=NEW_62': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_strategy_validation_62.py::test_F01_main_plus_only_the_stage_62_files_and_exact_edits': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_swing_pullback_78.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_trader_view_55.py::test_23_prior_stage_files_identical_to_base_and_wiring_is_additive': "XSS01-F02 freeze bookkeeping: the byte pin of the prior-stage files reads evidence_research.js (an X-1 link site) as main left it (pre_xss01) and the edited test files with XSS-01's edits undone first (undo_xss01); XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_unified_exposure_gate_691.py::test_F01_stage_691_changes_exactly_its_declared_files_and_no_risk_strategy_broker_or_database_file': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave1_safety_remediation.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_market_data_broker_or_research_file_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_pra.py::test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_pra.py::test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_pra_accounting.py::test_TA42_downstream_compatibility_capture_keeps_raw_values_and_the_known_consumers_are_unchanged': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_prb.py::test_F01B_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_prb.py::test_F02B_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_prb.py::test_F03B_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_prc.py::test_F02C_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
    'tests/test_wave2_prc.py::test_F03C_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries': "XSS01-F02 freeze bookkeeping: a later stage's change-set union or view gains XSS-01's declared files (NEW_XSS01 / EDITED_XSS01 / SEMANTIC_XSS01) or reads XSS-01's in-place edits undone first (undo_xss01), exactly as EX-06 did when it was the newest stage; XSS-01's own lines are pinned by tests/test_xss01_freeze.py",
}
LEDGER = {
    ".github/ci/pytest_groups.json": "11c4bb3e6be2b7b19896586b06f2cdbf3e3594282b592cb540e8f87f68249ab9",
    ".gitignore": "d98fa877f978452439453e434c70117f472256f0da4b4f12cbe79042458ce909",
    "api/routes/account_refresh.py": "26f4ebfa3059e2d7f24fe608a8d4f0a6c582daaa237828920e561bbcc4891319",
    "api/routes/execution.py": "9255e19d7b846a2500a865077e02703db305275b6cf90dcbb980de6ac644f2e6",
    "api/routes/forward_validation.py": "efc8a89dd8275f5461c35492fc192d7a538e9f16921727af8edda25717a549f2",
    "api/routes/ops_status.py": "5f80dc548368809b88e47a329f51dbadbb81e9dede47ff073e681924ae0b5ec2",
    "api/server.py": "2f1e067c7e2961c2f2af37cdfa94bbcaf13346128b6d1bd7c1599bab4e66c326",
    "browser_tests/flows.py": "a87937529f4a226a69511be6e8d070a3f07eada69a6512e1bccd683f69979cd5",
    "config.py": "339a99b635146bb07c182a554e5424a4953dbcdb00bab57b39fdd676bfa306ec",
    "database/forward_validation_migrations.py": "73248fe825d0516168dfeccb8301481376e7ce7bf1b456dc535f8e0b7fafb8fb",
    "evidence/sources/sec_edgar.py": "ede7c133a055785fc1c4579c823bc85dbf6e02856da7d1d9257a0e90822c08db",
    "execution/__init__.py": "ee8abcf37957e9487a4eaf001fbd72b29f2cfc9d19d4b7f6fd47bbef5bd03b01",
    "execution/engine.py": "06e898d9e075ad766c247ed095806c49d994f413ecc056935e5fb9a3dbdd084c",
    "execution/models.py": "177a81a89604583cfffc9254da31227c1f35fb98e376dbfdeca9448128c1bfa8",
    "execution/scheduler.py": "6d8459eecb230fa52193c24a6fd0aade17de2959162aefb43010f3da61f57183",
    "execution/service.py": "0d7c4f71d112bc80a1b301827a99ac67c2c6882f468fa3f2b1dac0a600bac9ec",
    "execution/store.py": "e2bc106565703da3a9af938556fa841daa777f0beab97a215f6f858f24ec50c4",
    "forward_validation/metrics.py": "0bb11472b9cfd3263c82a5eb239ef478dfc0f39f60c082966b019ff31a164d0e",
    "forward_validation/reports.py": "e8ca0a4b509d294632b3891570fa04f6aa2e695ede3df70f42f6ff7dd1a2e204",
    "forward_validation/service.py": "02e219a544628c43bad061265e47aa23fd15ddcb3f88da2bd4b8fd5facf84fcd",
    "forward_validation/store.py": "a037f765f6e0ce3e4123dd8a4791ffdce9817cee301dfb9066a299d7a33fb920",
    "frontend/app.js": "ab59a29ad88ceeacece6a760bef2eb807d6547a82c534d0b682de80071a0638a",
    "frontend/command_center.js": "b5b5c4800392041a20e46b669a6708cd4fa484e57cab98ed88505329e093cf2c",
    "frontend/evidence_research.js": "2e6ceac9e0f975d567e8c948763b7a9002792e0e029efb1f8ed842c958b63e8a",
    "frontend/forward_validation.js": "74124aecd609650029f5447a8001d5317423d143ecff7061f4b837028d9b834a",
    "frontend/market.js": "4c96c50ebe87f773bba858f91c9780ce3f61583d0a90b3e0172de89826a971d0",
    "frontend/paper_bot.js": "baa58c35cc3b051cc57b3978d4033e7d8f794161d47184eac517b25b65d52200",
    "frontend/product_ui.css": "7ccb310cc903b95740ba84f9a76a97766640f7f5c1d3ac2747575f061b2f356a",
    "frontend/product_ui.js": "bd8037e5fb2d89228d7997b7cc8de717e34b0b16ebc0c66c68e130a19c339402",
    "frontend/trader_view.js": "cc83857e686c92ea04235c86954c96ea505073381d31664af26d1f18c356bebd",
    "safety/service.py": "e4403dd18459322a7fa26db7f360f0910eaf3a50f5ff40c661303a2cb9488213",
    "safety/store.py": "7e7635aae90686fd1c15ea6185fcf6fcc7903966cd16c8af9b741bea84a9d0c6",
    "scripts/check_stock_agent.ps1": "f4dab15027feb15457f2107e04702de4e5bd595b45d570eb74f8de5768c4a677",
    "scripts/install_stock_agent_task.ps1": "fef37c2a6da6fb90539ee32c577036a701eb04ad7ac5e16baa5454eb3d98718e",
    "scripts/run_aapl_paper_smoke.py": "c6038a876b1b5b6bbc701d3eb26bf3174f0ac92388e148720bff3d869fa5738a",
    "scripts/run_stock_agent.py": "1090690982d115e1f268738ebdb2b797e4f79b88be2c5d58e5f042010ff710c3",
    "scripts/stock_agent_common.ps1": "b9666ba682b9e324873ac1a32497fa2f8c90bdf88b0e7dc0dbb83bbf35803d41",
    "strategy_registry/downside_recovery.py": "d88199fcac5af969709fca6dcd3572c976b93a033a77bb780328c9d2f1d8d2b7",
    "strategy_registry/service.py": "e6b9f62fd2d39dd402ff986db3b68f9d3dffd27aea75f73b8b2693b142148022",
    "tests/conftest.py": "ba9a3dd90a1aa553cf84d7f82e238fb4d7927216f13dcd1947803dc4093461ad",
    "tests/frontend/forward_validation.test.cjs": "58838711ac9f165b82542539a0d05442191319a1bd8395730eaeb4e41dd4f9b3",
    "tests/frontend/product_flow.test.cjs": "7363b272cfe3e9f37f935c45dd6370ba4b86b58b5fe0ad59aeeccf98e689993b",
    "tests/frontend/product_ui.test.cjs": "6ca918ef01c5382fa39e6355e91965116cadb8b0468b125b5e19c3046eb37314",
    "tests/frontend/strategy_validation.test.cjs": "fce1fdf90474dbeeb8881a6469d9d8bb0a0b6ad612455eea099cc5611e92a1e4",
    "tests/frontend/trader_bot.test.cjs": "13e494d83535212b8d401a08e82dfc37756a34c50f7051c1ca44ed36aaa0636a",
    "tests/test_account_refresh_601.py": "ff47d14b3679e2e215db67968232414eab621cb97d7e5f17d9653772df318367",
    "tests/test_automation_37.py": "fd6421efebe4c6ea60920b8efeab64e5fb25a06991d22db6cf51a9a0510cf3db",
    "tests/test_cash_flow_602.py": "15581c69f6fe12e475f7311a03652bb0e128e06eaaa7bc227eab6281eae141ec",
    "tests/test_ci_groups_603.py": "8b1b0a4659424e7fc733a275dae76e3b72ac3f7df156234a8bf8c72f3e57c477",
    "tests/test_db_bootstrap_busy.py": "0253939a872b26f83dd479c31432adc75c5ba7000d3842a84dcc8d3717b38d86",
    "tests/test_demand_57.py": "fa6319bbc49a5770c52b6647973a3376df40d08df26e4ee9a21356b17eb981c9",
    "tests/test_execution_60.py": "ebc8b8aef3e07dc0b5db390bfc805476619577e8271ef609160a15dc7c9e7387",
    "tests/test_execution_safety_591.py": "7b8eb537ecf3cf3b554e4e983e3e4d8c5d3c318d38c5c0d74db14d1fb77beef2",
    "tests/test_forward_validation_61.py": "75455daefed8c5f90c6a2a282c2da79e5e360ebcf703caadb556dcb4f2726023",
    "tests/test_history_budget_39.py": "97fb1fc1432b815a1a8b1c73ceff00d8728d5acde4aff9a16c75a6cd55babdd9",
    "tests/test_product_flow_612.py": "fdd31f8e2acbb09242e528757704caea48974b17ec96e3c02e14117fc94aa74e",
    "tests/test_product_ui_611.py": "bfee639c228f6629d7c73270b3d8da630588cb4ef16a8f52b6c6edf2b9b04d89",
    "tests/test_proposals_59.py": "8ee1f4e30ae1e7044865b75ad0a8f53548f8941e5bed2777aa98e08b1e502883",
    "tests/test_regression_isolation.py": "f596017894f68ced030b1d4de720b201150b9637d390c2211347f9bb7cb15b83",
    "tests/test_setups_58.py": "3d89c910788e9ebf64c6ff97fb02d1932e77d24c0b7867ffa69b8b59f73684ea",
    "tests/test_strategy_validation_62.py": "8acab7595ea7daf46928f8b1fd1548f7460ac0f403d1d9c5290fb80a8ff22b52",
    "tests/test_trader_view_55.py": "88a96dbf342d451e5d6606a8c014345003527591e18aaebf1ab10a913a3934d1",
    "tests/test_trader_view_561.py": "c98637d7b4acaf253c26bfee172a0b1d775454ac912c8a2406b33f5f87eae620",
    "tests/test_windows_runner_60.py": "e28bd34f989d74b6bfe357370dabd383b3e25906e58d0b0370b511672c2a7996",
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


def pre_w2c(rel: str, text: str) -> str:
    """`text` (a current file) as main left it at BASE_W2C when Wave 2 PR C changed it on purpose (SEMANTIC_W2C): an older freeze test reads
    main's text, where it passed. tests/test_wave2_prc.py pins the delta from that text (the closed function list). Any other file passes through."""
    if rel not in SEMANTIC_W2C:
        return text
    out = subprocess.run(["git", "show", f"{BASE_W2C}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_W2C} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_w2c(rel: str, text: str) -> str:
    """`text` (a current test file) with Wave 2 PR C's exact in-place edits undone (last first): as on main BASE_W2C. Any other file passes
    through. tests/test_wave2_prc.py (F03C) proves the result is main, byte for byte."""
    text = _norm(text)
    for old, new in reversed(UNDO_W2C.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text


def pre_ex06(rel: str, text: str) -> str:
    """`text` (a current file) as main left it at BASE_EX06 when EX-06 changed it on purpose (SEMANTIC_EX06): an older freeze test reads main's
    text, where it passed. tests/test_ex06_freeze.py pins the delta from that text (T-X20). Any other file passes through."""
    if rel not in SEMANTIC_EX06:
        return text
    out = subprocess.run(["git", "show", f"{BASE_EX06}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_EX06} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_ex06(rel: str, text: str) -> str:
    """`text` (a current test file) with EX-06's exact in-place edits undone (last first): as on main BASE_EX06. Any other file passes through.
    tests/test_ex06_freeze.py (F03X) proves the result is main, byte for byte."""
    text = _norm(text)
    for old, new in reversed(UNDO_EX06.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text


def pre_xss01(rel: str, text: str) -> str:
    """`text` (a current file) as main left it at BASE_XSS01 when XSS-01 changed it on purpose (SEMANTIC_XSS01): an older freeze test reads main's
    text, where it passed. tests/test_xss01_freeze.py pins the delta from that text. Any other file passes through."""
    if rel not in SEMANTIC_XSS01:
        return text
    out = subprocess.run(["git", "show", f"{BASE_XSS01}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_XSS01} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_xss01(rel: str, text: str) -> str:
    """`text` (a current test file) with XSS-01's exact in-place edits undone (last first): as on main BASE_XSS01. Any other file passes through.
    tests/test_xss01_freeze.py proves the result is main, byte for byte."""
    text = _norm(text)
    for old, new in reversed(UNDO_XSS01.get(rel, [])):
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
