// Node tests for the Stage 6.1.1 product shell (frontend/product_ui.js): four primary sections (TRADE default · POSITIONS · ACTIVITY · MORE) over the
// unchanged legacy page, the user-facing state model (WATCHING · READY · ENTERING · IN TRADE · EXITING · CLOSED · NEEDS ATTENTION) mapped from the
// Stage 6.0 card keys / Stage 5.8 setup states / Stage 5.9.1 safety states, the header status bar, the Risk / Position / Paper Auto cards, the
// persisted-rows-only ACTIVITY timeline, the MORE groups, the current wording and the read-only wiring.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PUI = require("../../frontend/product_ui.js");
require("../../frontend/forward_validation.js");                                   // registers globalThis.ForwardValidation (the Validation block's model)

const FRONT = path.join(__dirname, "..", "..", "frontend");
const JS = fs.readFileSync(path.join(FRONT, "product_ui.js"), "utf8");
const CSS = fs.readFileSync(path.join(FRONT, "product_ui.css"), "utf8");
const HTML = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const words = /\b(buy now|sell now|enter now|trade now|you should|approved|execute|recommend\w*|profitable|winning|guaranteed|high confidence)\b/i;
const LEGACY_TABS = [...HTML.matchAll(/data-tab="([a-z]+)"/g)].map((m) => m[1]);

const EXEC = { mode: "MONITOR_ONLY", attention: false, attention_reasons: [], open_positions: 0, reconciliation_healthy: true, environment: { paper: true, simulated: false },
               scheduler: { market: { regular_session: true, session_date: "2026-10-21" } }, account_data: { status: "FRESH", cash_flow: { status: "FRESH" } }, cards: [], recent: [] };
const SAFE = { state: "TRADING_ALLOWED", state_label: "Trading allowed", reason_text: "" };
const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "Holding 3 shares since 10:31", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1",
                   state: "POSITION_OPEN", details: { sessions_held: 2 } };
const DOM = { stretch: "+1.8", confidence: "62", demand: "71", evidence: "LOADED", evidenceText: "strong", setupState: "READY", proposalState: "REVIEWABLE" };
const SRC = { transitions: [{ symbol: "AAPL", to_state: "READY", evaluated_at: "2026-10-21T14:35:00Z" }, { symbol: "AAPL", to_state: "INVALIDATED", reason_code: "BOUNCE_FAILED", evaluated_at: "2026-10-20T19:00:00Z" }],
              proposals: [{ symbol: "AAPL", status: "REVIEWABLE", final_qty: 2, created_at: "2026-10-21T14:36:00Z" }, { symbol: "MSFT", status: "BLOCKED", blockers: [{ code: "DEMAND_NA" }], created_at: "2026-10-21T13:00:00Z" }],
              intents: [{ symbol: "AAPL", intent_type: "ENTRY", side: "buy", qty: 2, state: "FILLED", submitted_at: "2026-10-21T14:36:05Z", resolved_at: "2026-10-21T14:36:07Z", filled_avg_price: "227.10" },
                        { symbol: "MSFT", intent_type: "ENTRY", side: "buy", qty: 1, state: "REJECTED", submitted_at: "2026-10-20T15:00:00Z", resolved_at: "2026-10-20T15:00:01Z", failure_code: "BROKER_REJECTED" }],
              positions: [{ position_id: "p1", symbol: "AAPL", state: "CLOSED", closed_at: "2026-10-21T19:50:00Z", realized_pnl: "4.20" }],
              position_events: [{ position_id: "p1", kind: "OPENED", at: "2026-10-21T14:36:07Z", reason: "2 of 2 filled at 227.10" }, { position_id: "p1", kind: "EXIT_REQUIRED", at: "2026-10-21T19:45:00Z", reason: "TIME_EXIT" },
                                { position_id: "p1", kind: "CLOSED", at: "2026-10-21T19:50:00Z", detail: { realized_pnl: "4.20" } }, { position_id: "p1", kind: "FILL_UPDATED", at: "2026-10-21T14:36:06Z" }],
              safety_events: [{ kind: "KILL_ON", at: "2026-10-19T15:00:00Z" }, { kind: "KILL_OFF", at: "2026-10-19T15:10:00Z" }, { kind: "STARTUP", at: "2026-10-19T13:00:00Z" }],
              findings: [{ kind: "QTY_MISMATCH", symbol: "AAPL", blocking: true, at: "2026-10-21T16:00:00Z" }, { kind: "ORDER_ADOPTED", blocking: false, at: "2026-10-21T16:01:00Z" }],
              fv_events: [{ kind: "SESSION_OPENED", at: "2026-10-21T13:31:00Z" }, { kind: "EARLY_STOP", code: "DUPLICATE_BROKER_ORDER", at: "2026-10-21T18:00:00Z" }, { kind: "SAMPLE", at: "2026-10-21T13:32:00Z" }] };
const FV = { sessions: { completed: 7, healthy: 7, degraded: 0, failed: 0, not_run: 0, in_progress: 1, target: 30 }, operational: { duplicate_broker_orders: 0, reconciliation_attention: 0, reconciliation_open: 0 },
             strategy: { closed_trades: 3, edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", insufficient: true, insufficient_text: "INSUFFICIENT FOR STRATEGY CONCLUSION", gross_pnl: "41.20" },
             attention: [], needs_attention: false };

test("1 four primary sections, TRADE first and default, each hosted by a legacy panel; the legacy tab bar is hidden by the shell, not removed", () => {
  assert.deepEqual(PUI.NAV.map((n) => [n.id, n.label]), [["trade", "TRADE"], ["positions", "POSITIONS"], ["activity", "ACTIVITY"], ["more", "MORE"]]);
  assert.equal(PUI.DEFAULT, "trade");
  for (const n of PUI.NAV) assert.ok(HTML.includes(`id="tab-${n.host}"`), n.host);
  assert.equal(LEGACY_TABS.length, 13);                                             // every legacy tab is still in the markup (functionality preserved)
  assert.match(CSS, /body\.pui nav\.tabs \{ display: none; \}/);
  assert.match(CSS, /body\.pui \.disclaimer \{ display: none; \}/);
  assert.match(CSS, /body\.pui #tab-paper \.unavailable-panel \{ display: none; \}/);
  assert.match(CSS, /body\.pui #tab-settings \.unavailable-panel \{ display: none; \}/);
});

test("2 MORE reaches every legacy workspace that is not one of the four hosts, plus Forward Validation and Advanced configuration", () => {
  const hosts = new Set(PUI.NAV.map((n) => n.host));
  const tabs = PUI.MORE.flatMap((g) => g.items.filter((i) => i.tab).map((i) => i.tab));
  assert.deepEqual(new Set(tabs), new Set(LEGACY_TABS.filter((t) => !hosts.has(t))));
  assert.deepEqual(PUI.MORE.map((g) => g.title), ["Research", "Tools", "System", "Settings"]);
  assert.ok(PUI.MORE[2].items.some((i) => i.href === "forward_validation.html"));
  assert.deepEqual(PUI.MORE[3].items.map((i) => i.label), ["Advanced configuration"]);
  const h = PUI.moreHtml(PUI.moreGroups([{ view: "fit", label: "Strategy Fit" }]));
  assert.ok(h.includes('data-pui-more="tab:strategy"') && h.includes('data-pui-more="tab:strategy:fit"') && h.includes('data-pui-more="tab:agent"') && h.includes('data-pui-more="action:settings"'));
  assert.ok(h.includes('href="forward_validation.html"') && h.includes("Robinhood, read only") && h.includes("Secrets are never shown") && !words.test(text(h)));
});

test("3 Agent state: the backend card keys and setup states are mapped for display only; a stopped or unknown risk gate reads NEEDS ATTENTION unless a trade is live", () => {
  const lab = (card, setup, safety, exec) => { const a = PUI.agentModel(card, setup, safety, exec); return [a.label, a.text]; };
  assert.deepEqual(lab(null, "NONE", "TRADING_ALLOWED", EXEC), ["WATCHING", "Waiting for setup conditions"]);
  assert.deepEqual(lab({ key: "NONE" }, "WATCH", "TRADING_ALLOWED", EXEC), ["WATCHING", "Waiting for setup conditions"]);
  assert.deepEqual(lab({ key: "NONE" }, "READY", "TRADING_ALLOWED", EXEC), ["READY", "Eligible for entry"]);
  assert.deepEqual(lab({ key: "WATCHING" }, "READY", "TRADING_ALLOWED", EXEC), ["WATCHING", "Waiting for setup conditions"]);
  assert.deepEqual(lab({ key: "READY" }, "READY", "TRADING_ALLOWED", EXEC), ["READY", "Eligible for entry"]);
  assert.deepEqual(lab({ key: "ENTERING" }, "READY", "TRADING_ALLOWED", EXEC), ["ENTERING", "Order submitted / awaiting fill"]);
  assert.deepEqual(lab(IN_TRADE, "NONE", "TRADING_ALLOWED", EXEC), ["IN TRADE", "Holding 3 shares since 10:31"]);
  assert.deepEqual(lab({ key: "EXITING" }, "NONE", "TRADING_ALLOWED", EXEC), ["EXITING", "Exit required / order submitted"]);
  assert.deepEqual(lab({ key: "CLOSED" }, "NONE", "TRADING_ALLOWED", EXEC), ["CLOSED", "Trade completed"]);
  assert.deepEqual(lab({ key: "ATTENTION" }, "NONE", "TRADING_ALLOWED", EXEC), ["NEEDS ATTENTION", "Manual review required"]);
  assert.deepEqual(lab({ key: "STOPPED" }, "NONE", "TRADING_ALLOWED", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(null, "WATCH", "HALTED", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(null, "WATCH", "UNKNOWN", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(IN_TRADE, "NONE", "HALTED", EXEC), ["IN TRADE", "Holding 3 shares since 10:31"]);           // an open paper position is still monitored
  assert.deepEqual(lab({ key: "WATCHING" }, "WATCH", "TRADING_ALLOWED", { ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }), ["NEEDS ATTENTION", "RECONCILIATION_REQUIRED"]);
  assert.deepEqual(Object.values(PUI.AGENT).map((x) => x[0]).filter((v, i, a) => a.indexOf(v) === i).sort(),
                   ["CLOSED", "ENTERING", "EXITING", "IN TRADE", "NEEDS ATTENTION", "READY", "WATCHING"]);
});

test("4 header status bar: PAPER · MARKET · ACCOUNT · TRADING · AUTO follow the Paper Bot and Risk Controls payloads; missing data shows a dash, never a guess", () => {
  const segs = (e, s) => PUI.statusModel(e, s).map((x) => [x.key, x.text, x.cls]);
  assert.deepEqual(segs(EXEC, SAFE), [["paper", "PAPER", "ok"], ["market", "MARKET OPEN", "ok"], ["account", "ACCOUNT FRESH", "ok"], ["trading", "TRADING ALLOWED", "ok"], ["auto", "AUTO OFF", ""]]);
  const closed = { ...EXEC, mode: "PAPER_AUTO", environment: { simulated: true }, scheduler: { market: { regular_session: false } }, account_data: { status: "STALE" } };
  assert.deepEqual(segs(closed, { state: "NO_NEW_ENTRIES" }), [["paper", "SIMULATED PAPER", "ok"], ["market", "MARKET CLOSED", ""], ["account", "ACCOUNT STALE", "warn"], ["trading", "NEW TRADES PAUSED", "warn"], ["auto", "AUTO ON", "ok"]]);
  assert.deepEqual(segs({ ...EXEC, account_data: { status: "UNAVAILABLE" } }, { state: "HALTED" }).slice(2, 4), [["account", "ACCOUNT UNAVAILABLE", "warn"], ["trading", "TRADING STOPPED", "bad"]]);
  assert.deepEqual(segs(null, null).map((x) => x[1]), ["PAPER", "MARKET —", "ACCOUNT —", "RISK —", "AUTO —"]);
  assert.deepEqual(segs({ ...EXEC, safety_state: "UNKNOWN" }, null)[3], ["trading", "RISK STATUS UNKNOWN", "bad"]);     // the gate's own state is never hidden
  assert.equal(PUI.TAGLINE, "Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled.");
  assert.ok(PUI.statusHtml(EXEC, SAFE).includes(PUI.TAGLINE) && PUI.statusHtml(EXEC, SAFE).includes('data-pui-seg="trading"'));
});

test("5 Risk summary: state, account / cash-flow freshness, reconciliation and every attention reason; problems are shown, never hidden", () => {
  const ok = PUI.riskModel(EXEC, SAFE);
  assert.deepEqual([ok.headline, ok.level, ok.stopped, ok.lines.map((l) => l.text)], ["Trading allowed", "ok", false, ["Trading allowed", "Account fresh", "Cash-flow data fresh", "Reconciliation healthy"]]);
  const paused = PUI.riskModel({ ...EXEC, account_data: { status: "STALE", cash_flow: { status: "AMBIGUOUS" } } }, { state: "NO_NEW_ENTRIES", reason_text: "Paused by the user." });
  assert.deepEqual([paused.headline, paused.level, paused.lines.map((l) => l.text)], ["New trades paused", "warn", ["New trades paused", "Paused by the user.", "Account data stale", "Cash-flow data unclear", "Reconciliation healthy"]]);
  const halted = PUI.riskModel({ ...EXEC, reconciliation_healthy: false, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED", "BROKER_UNAVAILABLE"] }, { state: "HALTED", reason_text: "Emergency stop active." });
  assert.deepEqual([halted.headline, halted.level, halted.stopped], ["Trading stopped", "bad", true]);
  assert.deepEqual(halted.lines.filter((l) => l.level === "bad").map((l) => l.text), ["Trading stopped", "Emergency stop active.", "Reconciliation needs a look", "RECONCILIATION_REQUIRED", "BROKER_UNAVAILABLE"]);
  assert.deepEqual([PUI.riskModel(null, null).headline, PUI.riskModel(null, null).level], ["Risk status unknown", "bad"]);
});

test("6 Position summary: no position / entering / in trade with the holding day from the backend limit", () => {
  assert.deepEqual(PUI.positionModel(null, null, null), { open: false, text: "No position", entering: false, symbol: null });
  assert.deepEqual(PUI.positionModel({ key: "ENTERING", symbol: "AAPL" }, null, null).entering, true);
  const p = PUI.positionModel(IN_TRADE, { position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY" }, { max_hold_sessions: 5 });
  assert.deepEqual([p.open, p.symbol, p.shares, p.entry, p.current, p.pnl, p.holding, p.status, p.state], [true, "SNDK", 3, "61.20", "62.10", "+2.70 (+1.47%)", "Day 2 / 5", "Monitoring", "HEALTHY"]);
  assert.equal(PUI.positionModel(IN_TRADE, null, { max_hold_sessions: 3 }).holding, "Day 2 / 3");            // the limit is read from /api/execution, not hard-coded
  assert.equal(PUI.positionModel(IN_TRADE, null, null).holding, "Day 2 / 5");
  assert.equal(PUI.positionModel({ ...IN_TRADE, key: "EXITING", reason: "Price reached the invalidation level" }, null, null).reason, "Price reached the invalidation level");
});

test("7 TRADE cards: Agent, Market condition, Risk with [ STOP TRADING ], Current position and Paper Auto; the two actions mirror the legacy buttons", () => {
  const ctx = { exec: EXEC, safety: SAFE, card: null, position: null, dom: DOM, symbol: "AAPL", limits: { max_hold_sessions: 5 }, enableLabel: "Enable Paper Auto", stopAvailable: true, autoAvailable: true };
  const h = PUI.cardsHtml(ctx);
  for (const k of ["agent", "market", "risk", "position", "auto"]) assert.ok(h.includes(`data-pui-card="${k}"`), k);
  assert.ok(h.includes('data-agent="READY"') && h.includes("Eligible for entry") && h.includes("Agent · AAPL"));
  const t = text(h);
  assert.ok(t.includes("PPE +2") && t.includes("Demand 71") && t.includes("Evidence strong") && t.includes("Setup READY · Risk checks passed"), t);
  assert.ok(h.includes('<button type="button" class="pui-btn stop" data-pui="stop">STOP TRADING</button>') && h.includes('data-risk="ok"'));
  assert.ok(h.includes('data-auto="OFF"') && h.includes('data-pui="auto-on">Enable Paper Auto</button>') && t.includes("No position"));
  const confirm = PUI.cardsHtml({ ...ctx, enableLabel: "Confirm: enable Paper Auto" });
  assert.ok(confirm.includes('data-pui="auto-on">Confirm: enable Paper Auto</button>'));                       // the legacy two-click label is mirrored, not re-implemented
  const on = PUI.cardsHtml({ ...ctx, exec: { ...EXEC, mode: "PAPER_AUTO", open_positions: 1, cards: [IN_TRADE] }, card: IN_TRADE, position: { position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 1 } });
  assert.ok(on.includes('data-auto="ON"') && on.includes('data-pui="auto-off"') && on.includes("Stop New Trades") && on.includes('data-agent="IN TRADE"') && on.includes('data-open="1"'));
  assert.ok(text(on).includes("SNDK · 3 shares") && text(on).includes("Holding Day 1 / 5") && text(on).includes("1 position open"));
  const halted = PUI.cardsHtml({ ...ctx, safety: { state: "HALTED", reason_text: "Emergency stop active." } });
  assert.ok(halted.includes('data-pui="stop" disabled>TRADING STOPPED</button>') && halted.includes('data-risk="bad"') && halted.includes('data-agent="NEEDS ATTENTION"'));
  const unavailable = PUI.cardsHtml({ ...ctx, stopAvailable: false, autoAvailable: false });
  assert.ok(unavailable.includes('data-pui="stop" disabled>STOP TRADING</button>') && unavailable.includes('data-pui="auto-on" disabled>'));
  assert.ok(!words.test(t) && !/never places a trade|no order path/i.test(t));
});

test("8 ACTIVITY timeline: persisted rows only (setups, proposals, orders, fills, positions, safety gate, reconciliation, validation), newest first", () => {
  const tl = PUI.timeline(SRC);
  const texts = tl.map((e) => e.text);
  assert.deepEqual(tl.map((e) => e.at), tl.map((e) => e.at).slice().sort().reverse());
  for (const want of ["AAPL → READY", "AAPL → invalidated · bounce failed", "AAPL · Proposal created · 2 shares", "AAPL · Risk checks passed", "MSFT · Blocked: demand na",
                      "AAPL · BUY submitted · 2 shares", "AAPL · BUY filled @ 227.10", "MSFT · BUY rejected · broker rejected", "AAPL · Position opened · 2 of 2 filled at 227.10",
                      "AAPL · Exit required · time exit", "AAPL · Trade closed · P/L +4.20", "Trading stopped (emergency stop)", "Emergency stop cleared", "Risk controls started",
                      "AAPL · Reconciliation: qty mismatch", "Validation session started", "Validation early stop · Paper Auto turned off · duplicate broker order"]) assert.ok(texts.includes(want), want);
  assert.ok(!texts.some((x) => /FILL_UPDATED|SAMPLE|ORDER_ADOPTED|adopted/i.test(x)));                        // informational rows are not part of the beginner timeline
  assert.deepEqual(new Set(tl.map((e) => e.kind)), new Set(["setup", "proposal", "order", "fill", "position", "exit", "safety", "reconciliation", "validation"]));
  assert.deepEqual(tl.filter((e) => e.cls === "bad").map((e) => e.text), ["Validation early stop · Paper Auto turned off · duplicate broker order", "AAPL · Reconciliation: qty mismatch", "Trading stopped (emergency stop)"]);
  assert.deepEqual(PUI.timeline({}), []);
  assert.deepEqual(PUI.timeline({ intents: [{ symbol: "X", intent_type: "ENTRY", side: "buy", qty: 1, state: "PENDING" }] }), []);   // no timestamp -> no entry (nothing invented)
});

test("9 Today summary counts only today's persisted rows", () => {
  const s = PUI.todaySummary(SRC, { ...EXEC, open_positions: 1, attention_reasons: ["RECONCILIATION_REQUIRED"] }, "2026-10-21");
  assert.deepEqual(s, { trades: 1, open: 1, closed: 1, pnl: "+4.20", issues: 2 });                              // 1 attention reason + 1 blocking finding today; the KILL_ON was two days ago
  assert.deepEqual(PUI.todaySummary(SRC, EXEC, "2026-10-22"), { trades: 0, open: 0, closed: 0, pnl: "—", issues: 0 });
});

test("10 ACTIVITY view: the Today strip, the timeline, the compact Validation block (shared Forward Validation model) and its link; nothing hidden when unavailable", () => {
  const h = PUI.activityHtml(SRC, EXEC, FV, "2026-10-21");
  const t = text(h);
  for (const k of ["Trades", "Open", "Closed", "Paper P/L", "Issues"]) assert.ok(h.includes(`<h4>${k}</h4>`), k);
  assert.ok(h.includes('data-pui-timeline') && h.includes('data-pui-kind="fill"') && h.includes('class="pui-day">Today</li>') && h.includes('class="pui-day">2026-10-20</li>'));
  assert.ok(h.includes("data-pui-validation") && t.includes("Sessions 7 / 30") && t.includes("Healthy 7") && t.includes("Duplicates 0") && t.includes("INSUFFICIENT FOR STRATEGY CONCLUSION"));
  assert.ok(h.includes('href="forward_validation.html"') && h.includes("data-pui-fv-link") && h.includes("data-pui-alerts-fold"));
  assert.ok(text(PUI.activityHtml({}, null, null, "2026-10-21")).includes("Validation status unavailable.") && text(PUI.activityHtml({}, null, null, "2026-10-21")).includes("No activity recorded yet."));
  const att = PUI.activityHtml(SRC, EXEC, { ...FV, needs_attention: true, attention: [{ code: "DUPLICATE_BROKER_ORDER", critical: true }] }, "2026-10-21");
  assert.ok(text(att).includes("NEEDS ATTENTION: duplicate broker order"));
  assert.ok(!words.test(t));
});

test("11 POSITIONS view: cards only for live or pending paper positions, with the details a beginner needs", () => {
  assert.ok(text(PUI.positionsHtml([])).startsWith("No open paper position."));
  const rows = PUI.positionCards({ ...EXEC, cards: [IN_TRADE, { key: "WATCHING", symbol: "AAPL" }, { key: "ENTERING", symbol: "MSFT", line: "Order submitted" }, { key: "CLOSED", symbol: "JPM" }] },
                                 [{ position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY" }]);
  assert.deepEqual(rows.map((r) => [r.card.symbol, r.agent.label, r.model.open]), [["SNDK", "IN TRADE", true], ["MSFT", "ENTERING", false]]);
  const h = PUI.positionsHtml(rows);
  assert.ok(h.includes('data-pui-pos="SNDK"') && h.includes('data-position-id="p1"') && text(h).includes("Quantity 3 Entry 61.20 Current 62.10 P/L +2.70 (+1.47%) Holding Day 2 / 5"));
  assert.ok(h.includes('data-pui-pos="MSFT"') && text(h).includes("Order submitted"));
  const d = PUI.positionDetailsHtml({ symbol: "SNDK", agent: "IN TRADE", setup: "READY FOR REVIEW", demand: "71", evidence: "strong", exit: "monitored · HEALTHY", risk: "Trading allowed", state: "HEALTHY", invalidation: "59.90",
                                      events: [{ at: "2026-10-21T14:36:07Z", kind: "OPENED", to_state: "OPEN" }], intents: [{ intent_type: "ENTRY", side: "buy", qty: 3, state: "FILLED", filled_qty: 3, filled_avg_price: "61.20", broker_order_id: "abcdef1234" }] });
  const dt = text(d);
  for (const k of ["Setup state READY FOR REVIEW", "Demand 71", "Evidence strong", "Exit monitoring monitored · HEALTHY", "Risk Trading allowed", "Position state HEALTHY", "Invalidation level 59.90",
                   "Broker orders and fills", "ENTRY buy 3 · filled · filled 3 @ 61.20 · broker abcdef12", "Recent events", "14:36 · opened → open"]) assert.ok(dt.includes(k), k);
});

test("12 wording: the stale sentences are gone from the product layer and the current policy line is present; the legacy notes are hidden by the shell", () => {
  for (const stale of ["never places a trade", "This tool never places", "no order path", "Not available in this version", "read-only Trader View", "never places, prepares"])
    assert.ok(!JS.includes(stale) && !CSS.includes(stale), stale);
  assert.ok(JS.includes(PUI.TAGLINE));
  assert.match(CSS, /body\.pui #tab-portfolio \.pf-readonly-note \{ display: none; \}/);
  assert.ok(JS.includes("Robinhood holdings are read only") && JS.includes("Paper orders go to the Alpaca PAPER account only"));
  assert.ok(!words.test(text(PUI.cardsHtml({ exec: EXEC, safety: SAFE, card: IN_TRADE, position: null, dom: DOM, symbol: "SNDK", limits: null, enableLabel: "Enable Paper Auto" }))));
});

test("13 read-only wiring: GET routes only, the two actions delegate to the legacy buttons, no broker / Robinhood / model call, and index.html carries two marked blocks", () => {
  assert.ok(!JS.includes('method: "POST"') && !JS.includes("/api/execution/auto") && !JS.includes("emergency-stop") && !JS.includes("pause-new-entries") && !JS.includes("/api/portfolio") && !JS.includes("/api/agent"));
  const routes = [...JS.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((m) => m[1]);
  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",
                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/"];
  assert.ok(routes.length >= 10 && routes.every((r) => allowed.includes(r)), routes);
  assert.ok(JS.includes(`'#tv-safety [data-safety="stop"]'`) && JS.includes(`'#tv-bot [data-bot="enable"]'`) && JS.includes(`'#tv-bot [data-bot="disable"]'`));
  assert.equal(HTML.match(/<!-- >>> Product UI 6\.1\.1/g).length, 2);
  assert.equal(HTML.match(/<!-- <<< Product UI 6\.1\.1 -->/g).length, 2);
  assert.ok(HTML.includes('<link rel="stylesheet" href="product_ui.css" />') && HTML.includes('<script src="product_ui.js"></script>'));
  assert.ok(HTML.indexOf('href="forward_validation.css"') < HTML.indexOf('href="product_ui.css"') && HTML.indexOf('src="forward_validation.js"') < HTML.indexOf('src="product_ui.js"'));
  assert.ok(HTML.lastIndexOf("<script") === HTML.indexOf('<script src="product_ui.js">'));                     // the shell loads last: every legacy module is already registered
  assert.ok(typeof PUI.expandDetails === "function" && typeof PUI.collapseDetails === "function" && typeof PUI.go === "function");
});
