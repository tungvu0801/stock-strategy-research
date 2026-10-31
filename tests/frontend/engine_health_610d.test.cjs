// Node tests for the Stage 6.10D engine health fold (frontend/product_ui.js, marked block "Stage 6.10D engine health visibility"): the deployed
// commit from GET /api/ops/engine, the Stage 6.10A start-up outcome, the preflight with its non-PASS checks, the supervisor, the worker heartbeat,
// auto-run problems and the Stage 6.9 Robinhood gateway state, as one display-only fold under the diagnostics line. Payload shapes are the ones the
// live server returned on 2026-10-31 (Phase B verification).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PUI = require("../../frontend/product_ui.js");

const JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "product_ui.js"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const words = /\b(buy now|sell now|enter now|trade now|you should|approved|execute|recommend\w*|profitable|winning|guaranteed|high confidence)\b/i;
const NOW = Date.parse("2026-10-31T05:45:00Z");
const by = (m, key) => m.items.find((i) => i.key === key);

const ENGINE = { version: "engine_health_v1", read_only: true,
                 deployed: { sha: "2fe29f3a48359a365a1506c9f70093129be72602", ref: null, source: "worktree", app_root: "C:\\live" },
                 tree_now: { sha: "2fe29f3a48359a365a1506c9f70093129be72602", ref: null, source: "worktree" }, tree_matches_running: true,
                 process: { pid: 66084, started_at: "2026-10-31T05:39:30+00:00", python: "py", cwd: "C:\\live" }, runner: null };
const ACT = { mode: { mode: "MONITOR_ONLY" },
              auto_run: { policy: "MANUAL_ARM", scheduled_arm_enabled: false, problems: [],
                          startup: { pending: false, passive: false, code: null, text: null, demoted: false, at: "2026-10-31T05:39:34+00:00", components_started: true },
                          preflight: { status: "READY", at: "2026-10-31T05:40:40+00:00", blocking: [],
                                       checks: [{ id: "config", status: "PASS", text: "auto-run policy MANUAL_ARM" }, { id: "paper_proof", status: "PASS", text: "paper account read" },
                                                { id: "market_clock", status: "INFO", text: "session CLOSED (nyse_2026_2027_v1)" }, { id: "external_accounts", status: "INFO", text: "unified portfolio PARTIAL; exposure gate OFF" }] },
                          supervisor: { running: true, instance: "66084:542f2064", lease: "stock_agent_instance", runs: 2, last_run_at: "2026-10-31T05:40:40+00:00", last_error: null,
                                        last_result: { at: "2026-10-31T05:40:40+00:00", result: "OK", lease: "HELD", preflight: "READY" } } },
              worker: { state: "IDLE", label: "Idle", heartbeat_at: "2026-10-31T05:44:39+00:00", heartbeat_age_s: 21.0, expected_every_s: 300.0, stale_after_s: 630.0, running: true,
                        in_cycle: false, next_at: "2026-10-31T05:49:39+00:00" }, broker_calls: 0 };
const RH_DOWN = { source: "ROBINHOOD", account_type: "REAL", label: "Robinhood · REAL · user managed", positions: null, write_capable: false, state: "ERROR", reason: "GATEWAY_DOWN",
                  message: "Robinhood read failed (GATEWAY_DOWN): holdings unknown, not zero. The local Robinhood gateway is not reachable." };

test("1 the healthy live shape: every row ok, the summary is the deployed line, nothing is decided", () => {
  const m = PUI.engineModel(ACT, ENGINE, null, NOW);
  assert.equal(m.level, "ok");
  assert.deepEqual(m.items.map((i) => i.key), ["deployed", "startup", "preflight", "supervisor", "worker"]);
  assert.equal(by(m, "deployed").text, "Deployed 2fe29f3 · started 05:39 UTC");
  assert.equal(m.summary, "Deployed 2fe29f3 · started 05:39 UTC");
  assert.equal(by(m, "startup").text, "Start-up: Monitor only at 05:39 UTC · policy manual arm");
  assert.match(by(m, "preflight").text, /^Preflight READY \(4 min ago\) · market clock: session CLOSED \(nyse_2026_2027_v1\) · external accounts: unified portfolio PARTIAL; exposure gate OFF$/);
  assert.equal(by(m, "preflight").level, "ok");                                       // INFO checks are information, not a degradation
  assert.equal(by(m, "supervisor").text, "Supervisor running · lease held · last run 4 min ago");
  assert.equal(by(m, "worker").text, "Worker Idle · heartbeat 21 s ago · next run 05:49 UTC");
  for (const i of m.items) assert.ok(i.level === "ok" && !words.test(i.text), i.text);
});

test("2 deployed: a branch ref is named, an unresolved commit is a warning, a tree checked out under the running process asks for a restart", () => {
  const ref = PUI.engineModel(ACT, { ...ENGINE, deployed: { ...ENGINE.deployed, ref: "refs/heads/main" } }, null, NOW);
  assert.equal(by(ref, "deployed").text, "Deployed 2fe29f3 (main) · started 05:39 UTC");
  const none = PUI.engineModel(ACT, { ...ENGINE, deployed: { sha: null, ref: null, source: "no_git" }, tree_now: { sha: null } }, null, NOW);
  assert.equal(by(none, "deployed").level, "warn");
  assert.equal(by(none, "deployed").text, "Deployed commit unknown (no git) · started 05:39 UTC");
  const moved = PUI.engineModel(ACT, { ...ENGINE, tree_now: { sha: "edaaebb0000000000000000000000000000000000", ref: null, source: "worktree" }, tree_matches_running: false }, null, NOW);
  assert.equal(by(moved, "tree").level, "warn");
  assert.equal(by(moved, "tree").text, "Checked-out tree edaaebb differs from the running code 2fe29f3 · restart to deploy it");
  assert.equal(moved.level, "warn");
  assert.equal(moved.summary, "⚠ Checked-out tree edaaebb differs from the running code 2fe29f3 · restart to deploy it");
});

test("3 not loaded: no engine descriptor and no activity read as unknown, never as healthy", () => {
  const m = PUI.engineModel(null, undefined, null, NOW);
  assert.equal(m.level, "unknown");
  assert.equal(m.summary, PUI.ENGINE_NOT_LOADED);
  assert.deepEqual(m.items.map((i) => [i.key, i.level, i.text]), [["deployed", "unknown", "Deployed commit not loaded"], ["startup", "unknown", "Engine status not loaded"]]);
  assert.equal(PUI.engineModel(ACT, null, null, NOW).items[0].text, "Deployed commit not loaded");  // a failed GET /api/ops/engine (state.engine = null)
});

test("4 start-up: a demoted Paper Auto names its code, components still starting is a warning", () => {
  const demoted = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, startup: { ...ACT.auto_run.startup, demoted: true, code: "PERSISTED_PAPER_AUTO" } } }, ENGINE, null, NOW);
  assert.equal(by(demoted, "startup").level, "warn");
  assert.equal(by(demoted, "startup").text, "Start-up: Paper Auto demoted to Monitor only (persisted paper auto) at 05:39 UTC");
  const pending = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, startup: { ...ACT.auto_run.startup, pending: true } } }, ENGINE, null, NOW);
  assert.equal(by(pending, "startup").text, "Start-up: components still starting");
  assert.equal(pending.level, "warn");
});

test("5 preflight: DEGRADED lists the blocking codes and the failed checks; a READY preflight with a non-INFO check is a warning", () => {
  const deg = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, preflight: { status: "DEGRADED", at: "2026-10-31T05:39:37+00:00", blocking: ["PAPER_PROOF_PENDING"],
                                                       checks: [{ id: "paper_proof", status: "FAIL", text: "paper account not yet read" }, { id: "config", status: "PASS", text: "x" }] } } }, ENGINE, null, NOW);
  assert.equal(by(deg, "preflight").level, "warn");
  assert.equal(by(deg, "preflight").text, "Preflight DEGRADED (5 min ago) · blocking: paper proof pending · paper proof: paper account not yet read");
  assert.equal(deg.summary, "⚠ Preflight DEGRADED (5 min ago) · blocking: paper proof pending · paper proof: paper account not yet read");
  const readyWarn = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, preflight: { ...ACT.auto_run.preflight, checks: [{ id: "reconciliation", status: "WARN", text: "1 unmatched order" }] } } }, ENGINE, null, NOW);
  assert.equal(by(readyWarn, "preflight").level, "warn");
  assert.equal(by(readyWarn, "preflight").text, "Preflight READY (4 min ago) · reconciliation: 1 unmatched order");
});

test("6 supervisor and worker: an error, a stopped supervisor, a stale heartbeat and a worker that is not running are bad or warn, never hidden", () => {
  const err = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, supervisor: { ...ACT.auto_run.supervisor, last_error: "lease lost" } } }, ENGINE, null, NOW);
  assert.equal(by(err, "supervisor").level, "bad");
  assert.equal(by(err, "supervisor").text, "Supervisor error: lease lost");
  assert.equal(err.level, "bad");
  assert.equal(err.summary, "⚠ Supervisor error: lease lost");
  const stopped = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, supervisor: { ...ACT.auto_run.supervisor, running: false } } }, ENGINE, null, NOW);
  assert.equal(by(stopped, "supervisor").level, "warn");
  assert.equal(by(stopped, "supervisor").text, "Supervisor not running · lease held · last run 4 min ago");
  const stale = PUI.engineModel({ ...ACT, worker: { ...ACT.worker, heartbeat_age_s: 700.5 } }, ENGINE, null, NOW);
  assert.equal(by(stale, "worker").level, "bad");
  assert.equal(by(stale, "worker").text, "Worker Idle · heartbeat 701 s ago (STALE) · next run 05:49 UTC");
  const dead = PUI.engineModel({ ...ACT, worker: { ...ACT.worker, running: false, heartbeat_age_s: null, next_at: null } }, ENGINE, null, NOW);
  assert.equal(by(dead, "worker").text, "Worker not running");
  assert.equal(dead.level, "bad");
  const problems = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, problems: ["LEASE_NOT_HELD", { code: "X", text: "clock skew 90 s" }] } }, ENGINE, null, NOW);
  assert.equal(by(problems, "problems").text, "Auto-run problems: lease not held; clock skew 90 s");
  assert.equal(by(problems, "problems").level, "bad");
});

test("7 Robinhood: the gateway state is shown as read-only and user managed; a gateway that is down is a warning with the backend's message, not an error of the bot", () => {
  const down = PUI.engineModel(ACT, ENGINE, RH_DOWN, NOW);
  assert.equal(by(down, "robinhood").level, "warn");
  assert.equal(by(down, "robinhood").text, `Robinhood read-only · real account · gateway down: ${RH_DOWN.message}`);
  assert.equal(down.level, "warn");
  const ok = PUI.engineModel(ACT, ENGINE, { ...RH_DOWN, state: "FRESH", reason: null, message: null }, NOW);
  assert.equal(by(ok, "robinhood").text, "Robinhood read-only · real account · fresh");
  assert.equal(ok.level, "ok");
  assert.equal(PUI.engineModel(ACT, ENGINE, null, NOW).items.some((i) => i.key === "robinhood"), false);   // not read until the fold is opened
});

test("8 the fold: level attribute, one summary, one row per item with its key, escaped text, closed unless asked, empty for no model", () => {
  const m = PUI.engineModel({ ...ACT, auto_run: { ...ACT.auto_run, supervisor: { ...ACT.auto_run.supervisor, last_error: "<lease> & lost" } } }, ENGINE, RH_DOWN, NOW);
  const html = PUI.engineHtml(m, false);
  assert.match(html, /^<details class="pui-fold pui-diag pui-engine bad" data-pui-engine="bad"><summary data-pui="engine"><span class="pui-diag-summary">ENGINE · /);
  assert.ok(!/<details[^>]* open/.test(html) && /<details[^>]* open/.test(PUI.engineHtml(m, true)));
  assert.equal((html.match(/<li /g) || []).length, m.items.length);
  for (const i of m.items) assert.ok(html.includes(`data-pui-engine-item="${i.key}"`), i.key);
  assert.ok(html.includes("&lt;lease&gt; &amp; lost") && !html.includes("<lease>"));
  assert.ok(text(html).includes("ENGINE · ⚠ Supervisor error: <lease> & lost"));
  assert.equal(PUI.engineHtml(null, true), "");
  assert.equal(PUI.engineHtml({ level: "ok", summary: "x", items: [], count: 0 }, true), "");
  assert.deepEqual(PUI.ENGINE_KEYS, ["deployed", "tree", "startup", "preflight", "supervisor", "worker", "problems", "robinhood"]);
});

test("9 helpers and wiring: UTC clock and ages, the marked block, the diagnostics render calls the engine render, GET only, no new POST", () => {
  assert.equal(PUI.engineClock("2026-10-31T05:39:34+00:00"), "05:39 UTC");
  assert.equal(PUI.engineClock(null), "—");
  assert.equal(PUI.engineAge("2026-10-31T05:44:39+00:00", NOW), "21 s ago");
  assert.equal(PUI.engineAge("2026-10-31T05:40:40+00:00", NOW), "4 min ago");
  assert.equal(PUI.engineAge("2026-10-31T02:00:00+00:00", NOW), "4 h ago");
  assert.equal(PUI.engineAge("bad", NOW), "—");
  const begin = JS.indexOf("// >>> Engine health visibility, Stage 6.10D"), end = JS.indexOf("// <<< Engine health visibility, Stage 6.10D");
  assert.ok(!/(\/\/|#|<!--) *>>> *Stage 6\.1/.test(JS), "a block marker must never start with Stage 6.1 (the Stage 6.1 freeze tests scan for that prefix)");
  assert.ok(begin > 0 && end > begin);
  const block = JS.slice(begin, end);
  for (const fn of ["function engineModel(", "function engineHtml(", "function renderEngine(", "function loadEngine(", "function loadRobinhood("]) assert.ok(block.includes(fn), fn);
  assert.ok(block.includes('getJson("/api/ops/engine")') && block.includes('getJson("/api/unified-portfolio/robinhood")'));
  assert.equal((block.match(/fetch\(|method:\s*"POST"|clickLegacy\(|runAutoAction\(/g) || []).length, 0);       // reads through getJson only; no action, no POST
  assert.ok(JS.includes("renderEngine();                                                                                      // Stage 6.10D"));
  assert.ok(JS.indexOf("renderEngine();") < begin);                                                                 // called from renderDiag, outside the block
  assert.equal(typeof PUI.renderEngine, "function");
});
