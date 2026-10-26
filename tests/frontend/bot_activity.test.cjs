// Node tests for Stage 6.8 (frontend/product_ui.js): the POSITIONS card of the Alpaca PAPER account's positions (NOT_LOADED is never "no position";
// external positions are marked unmanaged) and TRADE › Bot Activity (a connected page is not a working worker; progress, orders and history are shown
// only from the payload - broker-reported order steps, collapsed quiet cycles - and everything is escaped).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const F = { symbol: "F", qty: "1", avg_entry_price: "12.1", current_price: "11.58", market_value: "11.58", unrealized_pl: "-0.52", management: "EXTERNAL",
            management_label: "External · not managed by the bot", management_text: "Not opened by the bot.", managed: false };
const FRESH = { status: "FRESH", fresh: true, label: "Account data: Fresh (31s ago)" };
const LOADED = { state: "LOADED", source: "ALPACA_PAPER", account_label: "Alpaca PAPER account", source_label: "Alpaca PAPER account · read-only account refresh", host: "paper-api.alpaca.markets",
                 refreshed_at: "2026-10-26T18:54:25+00:00", freshness: FRESH, positions: [F], counts: { total: 1, bot: 0, bot_entry: 0, external: 1, needs_review: 0, unmanaged: 1 },
                 message: "1 position(s) in the Alpaca PAPER account." };

test("account card: not loaded, empty, loaded with an external position, stale, unavailable", () => {
  const nl = PUI.accountPositionsHtml({ state: "NOT_LOADED", positions: null, freshness: { status: "UNAVAILABLE", label: "Account data unavailable" }, message: "Not loaded yet. The read-only account refresh runs automatically." });
  assert.match(nl, /data-pui-acct="NOT_LOADED"/);
  assert.match(text(nl), /Not loaded yet\./);
  assert.doesNotMatch(text(nl), /No positions/);
  const empty = PUI.accountPositionsHtml({ ...LOADED, positions: [], counts: { total: 0 }, message: "No positions in the Alpaca PAPER account." });
  assert.match(empty, /data-pui-acct-count="0"/);
  assert.match(text(empty), /No positions in the Alpaca PAPER account\./);
  const html = PUI.accountPositionsHtml(LOADED);
  assert.match(html, /data-pui-acct-row="F" data-management="EXTERNAL"/);
  assert.match(text(html), /External · not managed by the bot/);
  assert.match(text(html), /Account data: Fresh \(31s ago\)/);
  assert.match(text(html), /paper-api\.alpaca\.markets/);
  assert.match(text(html), /0 bot-managed · 1 external/);
  const sim = PUI.accountPositionsHtml({ ...LOADED, source: "SIMULATED_PAPER", account_label: "Simulated paper account", source_label: "Simulated paper broker · not an Alpaca account", host: null, positions: [] });
  assert.match(text(sim), /^Simulated paper account /);
  assert.match(text(sim), /No positions in the Simulated paper account\./);
  assert.doesNotMatch(text(sim), /Alpaca PAPER account/);
  const stale = PUI.accountPositionsHtml({ ...LOADED, freshness: { status: "STALE", label: "Account data: Stale" }, message: "Account data: Stale: these values are from 2026-10-26T18:54:25+00:00 and may be out of date." });
  assert.match(stale, /data-pui-acct-stale/);
  assert.match(PUI.accountPositionsHtml(null, "HTTP 500"), /data-pui-acct="UNAVAILABLE"/);
});

const ACT = {
  version: "bot_activity_v1", generated_at: "2026-10-26T18:55:00+00:00", broker_calls: 0, mode: { mode: "MONITOR_ONLY", label: "Monitor only" },
  worker: { state: "IDLE", label: "Idle", text: "Alive; waiting for the next scheduled run at 18:55:30 UTC.", heartbeat_at: "2026-10-26T18:54:58+00:00", heartbeat_age_s: 2,
            expected_every_s: 30, last_started_at: "2026-10-26T18:54:30+00:00", last_finished_at: "2026-10-26T18:54:30+00:00", next_at: "2026-10-26T18:55:30+00:00", runs: 37, in_cycle: false },
  progress: null,
  last_cycle: { source: "THIS_PROCESS", at: "2026-10-26T18:54:30+00:00", finished_at: "2026-10-26T18:54:30+00:00", result: "IDLE", trigger: "SCHEDULED", duration_ms: null,
                text: "Monitor only with nothing open: no broker call, nothing evaluated." },
  tickers: [{ symbol: "SNDK", status: "BLOCKED", label: "Entry blocked", text: "Entry blocked: broker already holds 1 SNDK", at: "2026-10-26T18:54:30+00:00",
              reasons: [{ control: "BROKER_POSITION", label: "Already held at the broker", text: "broker already holds 1 SNDK" }] },
            { symbol: "AMD", status: "WAITING", label: "Waiting", text: "Waiting for the dislocation <b>", reasons: [] }],
  risk: { gate: { state: "TRADING_ALLOWED", label: "Trading allowed", allowed_new_entry: true, reason_text: "", checks: [] },
          controls: [{ control: "SAFETY_GATE", label: "Risk gate", result: "PASS", text: "TRADING_ALLOWED", at: "2026-10-26T18:54:30+00:00" }] },
  orders: [{ symbol: "SNDK", type: "ENTRY", side: "buy", qty: 3, state: "PARTIALLY_FILLED",
             steps: [{ at: "2026-10-26T18:50:00+00:00", kind: "LOCAL", label: "Intent recorded (before any broker call)" }, { at: "2026-10-26T18:50:00+00:00", kind: "LOCAL", label: "Sent to the broker" },
                     { at: "2026-10-26T18:50:01+00:00", kind: "BROKER", label: "Submitted (received by the broker)" }, { at: "2026-10-26T18:50:31+00:00", kind: "BROKER", label: "Partially filled 1/3 @ 100.00" }] }],
  positions: { bot: [{ symbol: "SNDK", state: "HEALTHY", decision: "HOLD", decision_text: "No exit condition is met; holding.", decision_at: "2026-10-26T18:54:30+00:00" }], account: LOADED },
  history: [{ at: "2026-10-26T18:54:30+00:00", kind: "CYCLE", level: "info", text: "Cycle (scheduled): 0 open order(s) checked; reconciliation clean; no bot position; entries not evaluated (Monitor only). No trade.", count: 4, first_at: "2026-10-26T18:39:30+00:00" },
            { at: "2026-10-26T18:50:31+00:00", kind: "ORDER", level: "info", text: "SNDK entry order: Partially filled (broker status partially_filled)" }],
};

test("a connected dashboard is shown apart from the worker; a stopped worker reads as not running", () => {
  const conn = { okAt: 1, okAtIso: "2026-10-26T18:55:01.000Z", error: null };
  const m = PUI.botActivityModel({ ...ACT, worker: { ...ACT.worker, state: "STOPPED", label: "Not running", text: "The bot worker is not running in this server process: the dashboard is connected, but no cycle runs." } }, conn);
  const html = PUI.botActivityHtml(m, {});
  assert.match(html, /data-pui-dash="CONNECTED"/);
  assert.match(html, /data-pui-worker="STOPPED"/);
  assert.match(text(html), /Dashboard Connected/);
  assert.match(text(html), /Bot worker Not running/);
  const none = PUI.botActivityHtml(PUI.botActivityModel(null, { error: "HTTP 502" }), {});
  assert.match(none, /data-pui-dash="DISCONNECTED"/);
  assert.match(none, /data-pui-worker="UNKNOWN"/);
  const failed = PUI.botActivityModel(ACT, { ...conn, error: "timeout" });
  assert.equal(failed.dashboard.state, "STALE");
});

test("worker detail, the real progress step and the last cycle", () => {
  const idle = PUI.botActivityModel(ACT, { okAt: 1, okAtIso: "2026-10-26T18:55:01Z" });
  assert.equal(idle.progress, "No cycle in progress.");
  assert.match(idle.worker.heartbeat, /^18:54:58 \(2 s ago, expected every ~30 s\)$/);
  assert.match(idle.last, /no broker call, nothing evaluated/);
  const run = PUI.botActivityModel({ ...ACT, worker: { ...ACT.worker, state: "RUNNING", in_cycle: true },
    progress: { step: "ENTRIES", step_label: "Evaluating READY tickers for entry", symbol: "SNDK", index: 2, total: 3, started_at: "2026-10-26T18:55:00+00:00" } }, {});
  assert.equal(run.progress, "Step 5 of 5: Evaluating READY tickers for entry · SNDK (2 of 3) · cycle started 18:55:00");
  const db = PUI.botActivityModel({ ...ACT, last_cycle: { ...ACT.last_cycle, source: "DATABASE" } }, {});
  assert.match(db.last, /recorded before this server started/);
});

test("tickers, risk, broker-reported orders, unmanaged externals and collapsed history are rendered and escaped", () => {
  const html = PUI.botActivityHtml(PUI.botActivityModel(ACT, { okAt: 1, okAtIso: "2026-10-26T18:55:01Z" }), { tickers: true, orders: true, positions: true, history: true, risk: true });
  const t = text(html);
  assert.match(html, /data-pui-ticker="SNDK" data-status="BLOCKED"/);
  assert.match(t, /Already held at the broker: broker already holds 1 SNDK/);
  assert.match(html, /Waiting for the dislocation &lt;b&gt;/);                             // escaped
  assert.match(t, /Risk gate · PASS · TRADING_ALLOWED/);
  assert.match(t, /Partially filled 1\/3 @ 100\.00 \(broker\)/);
  assert.doesNotMatch(t, /\bFilled 3\/3\b/);                                                // never inferred from a submission
  assert.match(html, /data-pui-external="F"/);
  assert.match(t, /Positions \(1 bot · 1 external, unmanaged\)/);
  assert.match(t, /×4 since 18:39:30/);
  assert.match(html, /<details class="pui-fold" data-pui-bot-fold="history" open>/);
  const closed = PUI.botActivityHtml(PUI.botActivityModel(ACT, {}), {});
  assert.doesNotMatch(closed, / open>/);
  const nl = PUI.botActivityHtml(PUI.botActivityModel({ ...ACT, positions: { bot: [], account: { state: "NOT_LOADED", message: "Not loaded yet." } } }, {}), { positions: true });
  assert.match(nl, /data-pui-external-state="NOT_LOADED"/);
  assert.match(text(nl), /Account positions: not loaded yet\./);
});

test("the TRADE Position card names an external account holding instead of a bare No position, and says when the account is not loaded", () => {
  const ext = PUI.accountCardHtml(LOADED, "F");
  assert.match(ext, /data-pui-external="F"/);
  assert.match(text(ext), /No bot position Account holds 1 F · External · not managed by the bot Account data: Fresh \(31s ago\) The bot never trades or exits it; it blocks a bot entry in F\./);
  assert.equal(PUI.accountCardHtml(LOADED, "NVDA"), null);                                    // nothing held: the plain card stays
  assert.equal(PUI.accountCardHtml({ ...LOADED, positions: [{ ...F, management: "BOT", managed: true }] }, "F"), null);   // the bot's own: its lifecycle card
  assert.match(text(PUI.accountCardHtml({ state: "NOT_LOADED" }, "F")), /Account positions not loaded yet/);
  assert.equal(PUI.accountCardHtml(null, "F"), null);
});
