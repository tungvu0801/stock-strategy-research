// Node tests for Stage 6.10A (frontend/product_ui.js): TRADE › Bot Activity shows the auto-run POLICY as a row of its own - apart from the current
// mode and from the mode the last cycle ran in - and today's forward session (cohort, operational health, Paper Auto times, cycles, candidates,
// READY, blocked, orders, positions, incidents) in a fold that is closed by default. Everything comes from the existing activity payload and is escaped.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const CONN = { okAt: 1, okAtIso: "2026-10-21T15:30:01.000Z", error: null };
const BASE = {
  version: "bot_activity_v1", generated_at: "2026-10-21T15:30:00+00:00", broker_calls: 0,
  mode: { mode: "PAPER_AUTO", label: "Paper Auto", changed_at: "2026-10-21T13:35:00+00:00", last_cycle_mode: "PAPER_AUTO" },
  worker: { state: "IDLE", label: "Idle", text: "Alive.", heartbeat_at: "2026-10-21T15:29:58+00:00", heartbeat_age_s: 2, expected_every_s: 30, next_at: "2026-10-21T15:30:30+00:00", runs: 12 },
  last_cycle: { source: "THIS_PROCESS", at: "2026-10-21T15:29:30+00:00", finished_at: "2026-10-21T15:29:31+00:00", result: "RUN", mode: "PAPER_AUTO", trigger: "SCHEDULED", text: "1 READY ticker evaluated." },
  tickers: [], risk: { gate: { label: "Trading allowed", allowed_new_entry: true, checks: [] }, controls: [] }, orders: [], positions: { bot: [], account: null }, history: [],
  auto_run: { policy: "MANUAL_ARM", label: "Manual arm", text: "The bot starts in Monitor only after every start or restart; a person arms Paper Auto.",
              preflight: { status: "READY", at: "2026-10-21T13:15:03+00:00", blocking: [], checks: [] } },
  forward_session: { trading_date: "2026-10-21", recorded: true, cohort: { cohort_id: "FP-0a1b2c3d4e5f", label: "FP_2026_01" },
                     health: { status: "DEGRADED", final: false, reasons: ["RESTART_IN_SESSION"], reason_text: ["the process restarted during the session"] },
                     paper_auto: { spans: [{ from: "2026-10-21T13:35:00+00:00", to: "2026-10-21T15:30:00+00:00" }], last_disarmed_at: null },
                     cycles: { recorded: 41, errors: 0, worker_coverage: 0.98 }, candidates: 7, ready: 2, blocked: 1, orders: 1, fills: 1, entered: 1, exited: 0, open_positions: 1,
                     incidents: ["the process restarted during the session"], note: "Operational health of the day's evidence only - not a strategy result." },
};

test("the auto-run policy is its own row: never conflated with the current mode or the mode the last cycle ran in", () => {
  const m = PUI.botActivityModel(BASE, CONN);
  assert.equal(m.autoRun.label, "Manual arm");
  assert.equal(m.mode, "Paper Auto");
  const html = PUI.botActivityHtml(m, {});
  assert.match(html, /<span>Current mode<\/span><b data-pui-mode="PAPER_AUTO">Paper Auto · since /);
  assert.match(html, /<span>Last cycle ran in<\/span><b data-pui-last-cycle-mode>Paper Auto<\/b><span>Auto-run policy<\/span><b data-pui-autorun="MANUAL_ARM" title="[^"]*">Manual arm<\/b>/);
  const sched = PUI.botActivityHtml(PUI.botActivityModel({ ...BASE, auto_run: { ...BASE.auto_run, policy: "SCHEDULED_ARM", label: "Scheduled arm" } }, CONN), {});
  assert.match(sched, /data-pui-autorun="SCHEDULED_ARM"[^>]*>Scheduled arm</);
});

test("the forward session fold is the first fold, closed by default, and shows the day's operational record", () => {
  const html = PUI.botActivityHtml(PUI.botActivityModel(BASE, CONN), {});
  const folds = [...html.matchAll(/data-pui-bot-fold="([a-z]+)"/g)].map((x) => x[1]);
  assert.deepEqual(folds, ["forward", "tickers", "risk", "orders", "positions", "history"]);
  assert.match(html, /<details class="pui-fold" data-pui-bot-fold="forward"><summary data-pui-bot-summary="forward">Forward session · degraded so far<\/summary>/);
  const t = text(html);
  for (const s of ["Cohort FP_2026_01 · FP-0a1b2c3d4e5f", "Session health degraded so far", "Startup check READY at 13:15:03", "Paper Auto (UTC) 13:35:00 → 15:30:00",
                   "Cycles 41 recorded · 0 errors · worker up 98 %", "Candidates seen 7", "READY 2", "Blocked 1", "Orders 1 (1 filled)", "Positions 1 entered · 0 exited · 1 open",
                   "the process restarted during the session", "not a strategy result"]) {
    assert.ok(t.includes(s), s);
  }
  assert.match(PUI.botActivityHtml(PUI.botActivityModel(BASE, CONN), { forward: true }), /data-pui-bot-fold="forward" open>/);   // the user's open fold is kept
});

test("a startup check from an earlier day carries its date, never passing for today's", () => {
  const prev = { ...BASE, auto_run: { ...BASE.auto_run, preflight: { status: "READY", at: "2026-10-20T19:16:00+00:00", blocking: [], checks: [] } } };
  assert.ok(text(PUI.botActivityHtml(PUI.botActivityModel(prev, CONN), {})).includes("Startup check READY at 2026-10-20 19:16:00"));
  assert.ok(text(PUI.botActivityHtml(PUI.botActivityModel(BASE, CONN), {})).includes("Startup check READY at 13:15:03 "));   // today's: the time alone
});

test("not recorded yet, a missing payload key and a clean day degrade to plain text, never a crash", () => {
  const none = PUI.botActivityHtml(PUI.botActivityModel({ ...BASE, forward_session: { ...BASE.forward_session, recorded: false, cohort: null, health: { status: null, reasons: [], reason_text: [] },
                                                                                       paper_auto: { spans: [] }, cycles: {}, incidents: [] } }, CONN), {});
  assert.match(text(none), /Forward session · not recorded yet/);
  assert.match(text(none), /Paper Auto \(UTC\) not armed in today's session/);
  assert.match(none, /data-pui-incidents="none">Incidents: none\./);
  const old = { ...BASE };
  delete old.auto_run;
  delete old.forward_session;
  const h = PUI.botActivityHtml(PUI.botActivityModel(old, CONN), {});
  assert.match(h, /data-pui-autorun=""[^>]*>—</);
  assert.match(text(h), /No forward-session data from the server\./);
});

test("every forward-session value is escaped", () => {
  const evil = { ...BASE, auto_run: { ...BASE.auto_run, label: "<img src=x onerror=alert(1)>" },
                 forward_session: { ...BASE.forward_session, incidents: ["<script>x()</script>"], cohort: { cohort_id: "<b>", label: "\"q'" } } };
  const html = PUI.botActivityHtml(PUI.botActivityModel(evil, CONN), {});
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;script&gt;x\(\)&lt;\/script&gt;/);
  assert.match(html, /data-pui-cohort>&quot;q&#39; · &lt;b&gt;<\/b>/);
  assert.match(html, />&lt;img src=x onerror=alert\(1\)&gt;<\/b>/);
});

test("the UI reads the existing activity payload only: no new fetch path, no forbidden word", () => {
  const src = fs.readFileSync(path.join(__dirname, "../../frontend/product_ui.js"), "utf8");
  assert.doesNotMatch(src, /fetch\([^)]*(autorun|forward-capture|cohort|preflight)/);
  assert.doesNotMatch(src.toLowerCase(), /retry/);
  const html = text(PUI.botActivityHtml(PUI.botActivityModel(BASE, CONN), {}));
  assert.doesNotMatch(html, /\b(buy now|sell now|recommend\w*|profitable|guaranteed|you should)\b|target price|expected return/i);
});
