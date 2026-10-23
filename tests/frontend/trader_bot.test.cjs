// Node tests for the PAPER BOT panel and the PAPER TRADE card (Stage 6.0) in frontend/paper_bot.js. Beginner wording only: ⚪ Monitor only ·
// 🟢 Paper Auto · 🔴 Needs attention, one plain line, Enable Paper Auto (two clicks) / Stop New Trades, per ticker 🟡 Watching · 🔵 Ready · 🟠 Entering ·
// 🟢 In trade · 🟠 Exiting · ⚪ Closed · 🔴 Needs attention; internals under View Details; the POSTs change the persisted mode only.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const P = require("../../frontend/paper_bot.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const BOT_JS = fs.readFileSync(path.join(FRONT, "paper_bot.js"), "utf8");
const HTML = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
const CSS = fs.readFileSync(path.join(FRONT, "trader_view.css"), "utf8");
const codeOnly = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/.*$/gm, "$1");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const TRADE_WORDS = /\b(buy|sell|buy now|sell now|enter now|trade now|recommend\w*|you should|ready to buy|short it|go long|approved|execute)\b/i;
const DISCLAIMER = "Paper execution on the Alpaca PAPER account only: no real money.";

function status(over) {
  return Object.assign({
    version: "execution_v1", mode: "MONITOR_ONLY", mode_label: "Monitor only", icon: "⚪", headline: "Monitor only", attention: false, attention_reasons: [], ready: true,
    environment: { paper: true, host: "paper-api.alpaca.markets", label: "SIMULATED_PAPER", simulated: true, account_number_masked: "••••0060", status: "ACTIVE", blocked: false },
    safety_state: "TRADING_ALLOWED", safety_label: "Trading allowed", open_positions: 0, pending_entries: 0, pending_exits: 0, external_positions: 0, max_open_positions: 10,
    reconciliation_healthy: true, last_cycle_at: "2026-10-21T15:01:30+00:00", last_cycle_result: "RUN", scheduler: { running: false }, cards: [], recent: [],
    mode_changed_at: null, mode_changed_by: null, disclaimer: DISCLAIMER,
  }, over || {});
}
const AUTO = status({ mode: "PAPER_AUTO", mode_label: "Paper Auto", icon: "🟢", headline: "Paper Auto", open_positions: 1, pending_exits: 1, mode_changed_at: "2026-10-21T15:00:00+00:00", mode_changed_by: "user" });
const ATTN = status({ mode: "PAPER_AUTO", icon: "🔴", headline: "Needs attention", attention: true, attention_reasons: ["SNDK: order needs reconciliation"], reconciliation_healthy: false });
const TRADE = { kind: "TRADE", symbol: "SNDK", at: "2026-10-21T15:03:00+00:00", shares: 7, entry: "1325.50", exit: "1310.00", pnl: "-108.50", reason: "Price reached the invalidation level", reason_code: "HARD_INVALIDATION" };
const NO_TRADE = { kind: "NO_TRADE", symbol: "AMD", at: "2026-10-21T15:04:00+00:00", shares: 3, state: "REJECTED", code: "INSUFFICIENT_BUYING_POWER", reason: "insufficient buying power" };
function card(over) {
  return Object.assign({ symbol: "SNDK", key: "IN_TRADE", icon: "🟢", label: "In trade", line: "7 shares · entry 1325.50 · now 1330.00 · 31.50", state: "POSITION_OPEN", setup_state: "READY",
                         execution_id: "69e4050e43bb97c1ff4ca710aad3f6fd", position_id: "f47d95810e336b9e64f0129336b99495", shares: 7, entry: "1325.50", current: "1330.00", pnl: "31.50", pnl_pct: "0.34",
                         reason: null, since: "2026-10-21T15:01:30+00:00", details: { position_state: "HEALTHY", exit_cycle: 0, sessions_held: 0, invalidation_price: "1256.99",
                         events: [{ at: "2026-10-21T15:01:30+00:00", kind: "OPENED", to_state: "OPEN", reason: "7 of 7 filled at 1325.50" }] } }, over || {});
}

test("1 MONITOR_ONLY renders ⚪ Monitor only with Enable Paper Auto and no Stop button", () => {
  const h = P.botHtml(status(), {});
  assert.ok(h.includes('data-bot-mode="MONITOR_ONLY"') && text(h).startsWith("⚪ Monitor only"));
  assert.ok(h.includes('data-bot="enable"') && !h.includes('data-bot="disable"'));
  assert.ok(text(h).includes("Watching only; no paper orders"));
});

test("2 PAPER_AUTO renders 🟢 Paper Auto with Stop New Trades and a plain summary line", () => {
  const h = P.botHtml(AUTO, {});
  assert.ok(h.includes('data-bot-mode="PAPER_AUTO"') && text(h).startsWith("🟢 Paper Auto"));
  assert.ok(h.includes('data-bot="disable"') && !h.includes('data-bot="enable"'));
  assert.ok(text(h).includes("1 paper position open") && text(h).includes("1 exit in progress"));
});

test("3 attention renders 🔴 Needs attention with the first reason as the line", () => {
  const h = P.botHtml(ATTN, {});
  assert.ok(h.includes('data-bot-mode="ATTENTION"') && text(h).startsWith("🔴 Needs attention"));
  assert.ok(text(h).includes("SNDK: order needs reconciliation"));
});

test("4 details hidden by default, open on request, with the paper account, reconciliation and recent trades", () => {
  const closed = P.botHtml(status({ recent: [TRADE, NO_TRADE] }), {});
  assert.ok(/data-bot-details[^>]*class="[^"]*tv-hidden|class="tv-bot-details tv-hidden" data-bot-details/.test(closed));
  const open = P.botHtml(status({ recent: [TRADE, NO_TRADE] }), { detailsOpen: true });
  assert.ok(!/class="tv-bot-details tv-hidden"/.test(open));
  const t = text(open);
  assert.ok(t.includes("simulated paper") && t.includes("Reconciliation healthy") && t.includes("SNDK · Closed") && t.includes("AMD · Not filled"));
  assert.ok(t.includes("Hide details"));
});

test("5 enable is a two-step action: the confirm label; busy disables the buttons; errors are shown", () => {
  assert.ok(P.botHtml(status(), { confirmEnable: true }).includes("Confirm: enable Paper Auto"));
  assert.ok(/data-bot="enable" disabled/.test(P.botHtml(status(), { busy: true })));
  assert.ok(text(P.botHtml(status(), { error: "Enable Paper Auto failed (Paper environment not confirmed)." })).includes("Enable Paper Auto failed"));
});

test("6 no internal enum, no action language in the visible panel text", () => {
  for (const d of [status(), AUTO, ATTN, null]) {
    const t = text(P.botHtml(d, { detailsOpen: true }));
    assert.ok(!/MONITOR_ONLY|PAPER_AUTO|RECONCILIATION_REQUIRED|TRADING_ALLOWED/.test(t), t);
    assert.ok(!TRADE_WORDS.test(t.replace(/Stop New Trades/g, "")), t);
  }
  assert.ok(text(P.botHtml(null, {})).includes("Checking paper bot") && !P.botHtml(null, {}).includes("data-bot=\"enable\""));
});

test("7 the symbol card: In trade shows shares / entry / current / P/L; other keys read as intended", () => {
  const h = P.cardHtml(card(), {});
  assert.ok(h.includes('data-card-key="IN_TRADE"') && text(h).startsWith("🟢 In trade"));
  const t = text(h);
  assert.ok(t.includes("Shares 7") && t.includes("Entry 1325.50") && t.includes("Current 1330.00") && t.includes("P/L 31.50 (+0.34%)"));
  const ex = text(P.cardHtml(card({ key: "EXITING", icon: "🟠", label: "Exiting", line: "7 shares · Price reached the invalidation level", reason: "Price reached the invalidation level", state: "EXIT_SUBMITTED" }), {}));
  assert.ok(ex.startsWith("🟠 Exiting") && ex.includes("Reason: Price reached the invalidation level"));
  const cl = text(P.cardHtml(card({ key: "CLOSED", icon: "⚪", label: "Closed", line: "Trade completed · -108.50 · Price reached the invalidation level", current: "1310.00", pnl: "-108.50", pnl_pct: null, state: "CLOSED" }), {}));
  assert.ok(cl.startsWith("⚪ Closed") && cl.includes("Exit 1310.00") && cl.includes("P/L -108.50"));
  const none = text(P.cardHtml(null, { symbol: "MSFT" }));
  assert.ok(none.startsWith("⚪ No setup") && none.includes("No active setup") && !none.includes("View Details"));
  const att = text(P.cardHtml(card({ key: "ATTENTION", icon: "🔴", label: "Needs attention", line: "7 shares · qty mismatch", shares: 7, state: "RECONCILIATION_REQUIRED" }), {}));
  assert.ok(att.startsWith("🔴 Needs attention") && att.includes("qty mismatch"));
  for (const k of [["WATCHING", "🟡", "Watching"], ["READY", "🔵", "Ready"], ["ENTERING", "🟠", "Entering"], ["STOPPED", "🔴", "Stopped"]]) {
    const tt = text(P.cardHtml(card({ key: k[0], icon: k[1], label: k[2], shares: null, entry: null, line: "x" }), {}));
    assert.ok(tt.startsWith(`${k[1]} ${k[2]}`), tt);
  }
});

test("8 card details hidden by default and show internal state, ids, invalidation level and events when open", () => {
  const closed = P.cardHtml(card(), {});
  assert.ok(/class="tv-bot-details tv-hidden" data-card-details/.test(closed) && closed.includes("View Details"));
  const open = text(P.cardHtml(card(), { detailsOpen: true }));
  assert.ok(open.includes("Internal state position open") && open.includes("Invalidation level 1256.99") && open.includes("opened → open"));
  assert.ok(!/class="tv-bot-details tv-hidden"/.test(P.cardHtml(card(), { detailsOpen: true })));
});

test("9 history lines read as plain sentences", () => {
  assert.equal(P.historyLine(TRADE), "SNDK · Closed 2026-10-21 15:03 · 7 shares · entry 1325.50 → exit 1310.00 · P/L -108.50 · Price reached the invalidation level");
  assert.equal(P.historyLine(NO_TRADE), "AMD · Not filled 2026-10-21 15:04 · 3 shares · insufficient buying power");
  assert.equal(P.historyLine(null), "");
});

test("10 page wiring: the script is loaded after risk_controls.js, POSTs only the two mode actions, mounts beside the risk controls and the trade review", () => {
  assert.ok(HTML.includes('<script src="paper_bot.js"></script>') && HTML.indexOf("risk_controls.js") < HTML.indexOf("paper_bot.js"));
  const src = codeOnly(BOT_JS);
  const posts = [...src.matchAll(/act\("([^"]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(posts, ["/api/execution/auto/disable", "/api/execution/auto/enable"]);
  assert.ok(!/\/v2\/orders|robinhood|\/api\/alpaca|\/api\/paper/i.test(src) && !/alpaca/i.test(src.replace(/alpaca paper/gi, "")));
  assert.ok(!/recommend|probability|expected.return|target price|win rate/i.test(src));
  assert.ok(src.includes('".tv-panel-safety"') && src.includes('".tv-panel-proposal"') && src.includes("insertAdjacentElement"));
  assert.ok(src.includes('"/api/execution/status"'));
  for (const cls of [".tv-bot-state", ".tv-bot-card-state", ".tv-bot-grid", ".tv-bot-details", ".tv-panel-bot", ".tv-panel-trade"]) assert.ok(CSS.includes(cls), cls);
  assert.ok(!/recommend|buy now|sell now/i.test(CSS));
});
