// Node tests for the Stage 6.1.1 product shell (frontend/product_ui.js): four primary sections (TRADE default · POSITIONS · ACTIVITY · MORE) over the
// unchanged legacy page, the user-facing state model (WATCHING · READY · ENTERING · IN TRADE · EXITING · CLOSED · NEEDS ATTENTION) mapped from the
// Stage 6.0 card keys / Stage 5.8 setup states / Stage 5.9.1 safety states, the simplified header (PAPER · MARKET · SYSTEM HEALTHY / DEGRADED / STOPPED ·
// AUTO) with its detail expander, the three cards (Agent / Setup primary, Position, Risk headline + one reason), the compact chart empty state, the
// one-line diagnostics summary, the persisted-rows-only ACTIVITY timeline, the MORE groups, the current wording and the read-only wiring.
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
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const words = /\b(buy now|sell now|enter now|trade now|you should|approved|execute|recommend\w*|profitable|winning|guaranteed|high confidence)\b/i;
const LEGACY_TABS = [...HTML.matchAll(/data-tab="([a-z]+)"/g)].map((m) => m[1]);

const EXEC = { mode: "MONITOR_ONLY", attention: false, attention_reasons: [], open_positions: 0, reconciliation_healthy: true, environment: { paper: true, simulated: false },
               scheduler: { market: { regular_session: true, session_date: "2026-10-21" } }, account_data: { status: "FRESH", cash_flow: { status: "FRESH" } }, cards: [], recent: [] };
const SAFE = { state: "TRADING_ALLOWED", state_label: "Trading allowed", reason_text: "" };
const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "Holding 3 shares since 10:31", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1",
                   state: "POSITION_OPEN", details: { sessions_held: 2 } };
const DOM = { stretch: "+1.8", confidence: "62", demand: "71", evidence: "LOADED", evidenceText: "strong", setupState: "READY", setupLabel: "READY FOR REVIEW", setupReason: "Buyer confirmation strengthened.",
              blockers: [], proposalState: "REVIEWABLE" };
const WATCH_DOM = { ...DOM, setupState: "WATCH", setupLabel: "WATCH", proposalState: "NOT_ELIGIBLE", blockers: ["demand 48 < 60", "confidence 31 < 40"] };
const CTX = { exec: EXEC, safety: SAFE, card: null, position: null, dom: DOM, symbol: "AAPL", limits: { max_hold_sessions: 5 }, stopAvailable: true };
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
const primaryPart = (card) => card.split("<details")[0];                              // what a beginner sees before expanding Why? / Details
const cardOf = (html, key) => { const i = html.indexOf(`data-pui-card="${key}"`); const j = html.indexOf('<div class="pui-card', i + 1); return html.slice(html.lastIndexOf('<div class="pui-card', i), j < 0 ? undefined : j); };

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
  assert.deepEqual(lab({ key: "NONE" }, "READY", "TRADING_ALLOWED", EXEC), ["READY", "Eligible for entry review"]);
  assert.deepEqual(lab({ key: "WATCHING" }, "READY", "TRADING_ALLOWED", EXEC), ["WATCHING", "Waiting for setup conditions"]);
  assert.deepEqual(lab({ key: "READY" }, "READY", "TRADING_ALLOWED", EXEC), ["READY", "Eligible for entry review"]);
  assert.deepEqual(lab({ key: "ENTERING" }, "READY", "TRADING_ALLOWED", EXEC), ["ENTERING", "Order submitted / awaiting fill"]);
  assert.deepEqual(lab(IN_TRADE, "NONE", "TRADING_ALLOWED", EXEC), ["IN TRADE", "Monitoring exit conditions"]);
  assert.deepEqual(lab({ key: "EXITING" }, "NONE", "TRADING_ALLOWED", EXEC), ["EXITING", "Exit required / order submitted"]);
  assert.deepEqual(lab({ key: "CLOSED" }, "NONE", "TRADING_ALLOWED", EXEC), ["CLOSED", "Trade completed"]);
  assert.deepEqual(lab({ key: "ATTENTION" }, "NONE", "TRADING_ALLOWED", EXEC), ["NEEDS ATTENTION", "Manual review required"]);
  assert.deepEqual(lab({ key: "STOPPED" }, "NONE", "TRADING_ALLOWED", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(null, "WATCH", "HALTED", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(null, "WATCH", "UNKNOWN", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(IN_TRADE, "NONE", "HALTED", EXEC), ["IN TRADE", "Monitoring exit conditions"]);                // an open paper position is still monitored
  assert.deepEqual(lab({ key: "WATCHING" }, "WATCH", "TRADING_ALLOWED", { ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }), ["NEEDS ATTENTION", "RECONCILIATION_REQUIRED"]);
  assert.deepEqual(Object.values(PUI.AGENT).map((x) => x[0]).filter((v, i, a) => a.indexOf(v) === i).sort(),
                   ["CLOSED", "ENTERING", "EXITING", "IN TRADE", "NEEDS ATTENTION", "READY", "WATCHING"]);
});

test("4 Setup line: plain words from the stored setup state; a count only from the engine's own blocker list, never invented", () => {
  const sl = (card, dom) => PUI.setupLines(PUI.agentModel(card, dom.setupState, "TRADING_ALLOWED", EXEC), dom);
  assert.deepEqual(sl(null, WATCH_DOM), { line: "Setup · Watching", sub: "2 conditions outstanding" });
  assert.deepEqual(sl(null, { ...WATCH_DOM, blockers: ["demand 48 < 60"] }), { line: "Setup · Watching", sub: "1 condition outstanding" });
  assert.deepEqual(sl(null, { ...WATCH_DOM, blockers: [] }), { line: "Setup · Watching", sub: "Waiting for conditions" });
  assert.deepEqual(sl(null, { ...DOM, setupState: "NONE", blockers: [] }), { line: "Setup · No active setup", sub: "" });
  assert.deepEqual(sl(null, { ...DOM, setupState: "INVALIDATED" }), { line: "Setup · Invalidated", sub: "Waiting for a new setup" });
  assert.deepEqual(sl(null, { ...DOM, setupState: null }), { line: "Setup", sub: "Waiting for conditions" });
  assert.deepEqual(sl(null, DOM), { line: "Setup conditions met", sub: "" });                               // the state line already reads "Eligible for entry review"
  assert.deepEqual(sl({ key: "ENTERING" }, DOM), { line: "", sub: "" });
  assert.deepEqual(sl(IN_TRADE, DOM), { line: "", sub: "" });                                               // no sentence repeated under the state
  assert.deepEqual(sl({ key: "CLOSED" }, DOM), { line: "Watching for the next setup", sub: "" });
  assert.ok(!JS.includes("conditions met\n") && !/\d+ \/ \d+ conditions/.test(JS));                           // no "2 / 4 conditions met": the engine exposes no total
});

test("5 SYSTEM HEALTHY / DEGRADED / STOPPED is derived from the existing gate, account, cash-flow, reconciliation and attention fields only", () => {
  const sys = (e, s) => { const m = PUI.systemModel(e, s); return [m.level, m.text, m.cls, m.primary]; };
  assert.deepEqual(sys(EXEC, SAFE), ["HEALTHY", "SYSTEM HEALTHY", "ok", ""]);
  assert.deepEqual(sys({ ...EXEC, account_data: { status: "STALE", cash_flow: { status: "FRESH" } } }, SAFE), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Account data stale"]);
  assert.deepEqual(sys({ ...EXEC, account_data: { status: "UNAVAILABLE" } }, { state: "NO_NEW_ENTRIES", reason_text: "" }), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Safety gate: New trades paused"]);
  assert.deepEqual(sys(EXEC, { state: "NO_NEW_ENTRIES", reason_text: "Paused by the user." }), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Paused by the user."]);
  assert.deepEqual(sys({ ...EXEC, account_data: { status: "FRESH", cash_flow: { status: "AMBIGUOUS" } } }, SAFE), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Cash-flow data unclear"]);
  assert.deepEqual(sys({ ...EXEC, reconciliation_healthy: false }, SAFE), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Reconciliation needs a look"]);
  assert.deepEqual(sys(EXEC, { state: "HALTED", reason_text: "Emergency stop active." }), ["STOPPED", "SYSTEM STOPPED", "bad", "Emergency stop active."]);
  assert.deepEqual(sys({ ...EXEC, safety_state: "UNKNOWN" }, null)[0], "STOPPED");
  assert.deepEqual(sys({ ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }, SAFE), ["STOPPED", "SYSTEM STOPPED", "bad", "Needs attention: reconciliation required"]);
  assert.deepEqual(sys(null, null), ["UNKNOWN", "SYSTEM —", "", ""]);                                             // nothing loaded: a dash, never a guess
  const items = PUI.systemModel({ ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "UNAVAILABLE" } } }, { state: "NO_NEW_ENTRIES", reason_text: "Daily loss control degraded." }).items;
  assert.deepEqual(items.map((i) => [i.text, i.level]), [["Safety gate: New trades paused", "warn"], ["Account data unavailable", "warn"], ["Cash-flow data unavailable", "warn"], ["Reconciliation healthy", "ok"]]);
  assert.equal(items[0].reason, "Daily loss control degraded.");
  assert.ok(!/SYSTEM (HEALTHY|DEGRADED|STOPPED)/.test(JSON.stringify(EXEC)) && !JS.includes("fetch(\"/api/system"));   // a display summary: no backend health state exists or is called
});

test("6 header status bar: PAPER · MARKET · SYSTEM · AUTO, the detail reasons behind the SYSTEM expander, Paper Auto as a compact header control", () => {
  const segs = (e, s) => PUI.statusModel(e, s).map((x) => [x.key, x.text, x.cls]);
  assert.deepEqual(segs(EXEC, SAFE), [["paper", "PAPER", "ok"], ["market", "MARKET OPEN", "ok"], ["system", "SYSTEM HEALTHY", "ok"], ["auto", "AUTO OFF", ""]]);
  const degraded = { ...EXEC, mode: "PAPER_AUTO", environment: { simulated: true }, scheduler: { market: { regular_session: false } }, account_data: { status: "UNAVAILABLE" } };
  assert.deepEqual(segs(degraded, { state: "NO_NEW_ENTRIES" }), [["paper", "SIMULATED PAPER", "ok"], ["market", "MARKET CLOSED", ""], ["system", "SYSTEM DEGRADED", "warn"], ["auto", "AUTO ON", "ok"]]);
  assert.deepEqual(segs(null, null).map((x) => x[1]), ["PAPER", "MARKET —", "SYSTEM —", "AUTO —"]);
  const h = PUI.statusHtml(EXEC, SAFE, { sysOpen: false, enableLabel: "Enable Paper Auto", autoAvailable: true });
  assert.ok(h.includes('data-pui-seg="paper"') && h.includes('data-pui-seg="market"') && h.includes('data-pui-seg="system"') && h.includes('data-pui-seg="auto"'));
  assert.ok(!h.includes('data-pui-seg="account"') && !h.includes('data-pui-seg="trading"') && !text(h).includes("ACCOUNT FRESH") && !text(h).includes("TRADING ALLOWED"));   // not repeated in the header
  assert.ok(h.includes('<button type="button" class="pui-seg ok" data-pui-seg="system" data-pui="system" aria-expanded="false"') && !h.includes("data-pui-system-details"));
  assert.ok(h.includes('data-pui-auto="OFF"') && h.includes('data-pui="auto-on">Enable Paper Auto</button>') && h.includes(PUI.TAGLINE));
  const open = PUI.statusHtml({ ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "UNAVAILABLE" } } }, { state: "NO_NEW_ENTRIES", reason_text: "Daily loss control degraded." }, { sysOpen: true });
  const d = text(open.slice(open.indexOf("data-pui-system-details")));
  assert.ok(open.includes("SYSTEM DEGRADED") && d.includes("Safety gate: New trades paused · Daily loss control degraded.") && d.includes("Account data unavailable") && d.includes("Cash-flow data unavailable") && d.includes("Reconciliation healthy"), d);
  const on = PUI.statusHtml({ ...EXEC, mode: "PAPER_AUTO" }, SAFE, {});
  assert.ok(on.includes('data-pui-auto="ON"') && on.includes("AUTO ON") && on.includes('data-pui="auto-off"') && on.includes("Stop New Trades"));
  assert.ok(PUI.statusHtml(EXEC, SAFE, { enableLabel: "Confirm: enable Paper Auto" }).includes('data-pui="auto-on">Confirm: enable Paper Auto</button>'));   // the legacy two-click label is mirrored
  assert.ok(PUI.statusHtml(EXEC, SAFE, { autoAvailable: false }).includes('data-pui="auto-on" disabled>'));
  assert.ok(!PUI.statusHtml(null, null, {}).includes("data-pui=\"auto-"));                                         // no mode known: no button
});

test("7 Risk: one headline and one primary reason in front; every dependency, reason and attention item behind Details; stopped states say so plainly", () => {
  const r = PUI.riskModel(EXEC, SAFE);
  assert.deepEqual([r.headline, r.level, r.stopped, r.primary, r.critical], ["Trading allowed", "ok", false, "", false]);
  assert.deepEqual(r.lines.map((l) => l.text), ["Trading allowed", "Account data fresh", "Cash-flow data fresh", "Reconciliation healthy"]);
  const stale = PUI.riskModel({ ...EXEC, account_data: { status: "STALE", cash_flow: { status: "FRESH" } } }, SAFE);
  assert.deepEqual([stale.headline, stale.level, stale.primary], ["Trading allowed", "warn", "Account data stale"]);
  const paused = PUI.riskModel({ ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "AMBIGUOUS" } } }, { state: "NO_NEW_ENTRIES", reason_text: "Paused by the user." });
  assert.deepEqual([paused.headline, paused.level, paused.primary], ["New trades paused", "warn", "Paused by the user."]);
  assert.deepEqual(paused.lines.map((l) => l.text), ["New trades paused", "Paused by the user.", "Account data unavailable", "Cash-flow data unclear", "Reconciliation healthy"]);
  const noReason = PUI.riskModel({ ...EXEC, account_data: { status: "UNAVAILABLE" } }, { state: "NO_NEW_ENTRIES", reason_text: "" });
  assert.equal(noReason.primary, "Account data unavailable");                                                       // one root cause, not four warnings
  const halted = PUI.riskModel({ ...EXEC, reconciliation_healthy: false, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED", "BROKER_UNAVAILABLE"] }, { state: "HALTED", reason_text: "" });
  assert.deepEqual([halted.headline, halted.level, halted.stopped, halted.critical, halted.primary], ["Trading stopped", "bad", true, true, "Critical issue requires attention"]);
  assert.deepEqual(halted.lines.filter((l) => l.level === "bad").map((l) => l.text), ["Trading stopped", "Reconciliation needs a look", "RECONCILIATION_REQUIRED", "BROKER_UNAVAILABLE"]);   // critical details stay exposed
  assert.deepEqual([PUI.riskModel(null, null).headline, PUI.riskModel(null, null).level, PUI.riskModel(null, null).primary], ["Risk status unknown", "bad", "Critical issue requires attention"]);
});

test("8 Position: no position / entering / in trade with the holding day from the backend limit; raw broker fields are not part of the model's front", () => {
  assert.deepEqual(PUI.positionModel(null, null, null), { open: false, text: "No position", entering: false, symbol: null });
  assert.deepEqual(PUI.positionModel({ key: "ENTERING", symbol: "AAPL" }, null, null).entering, true);
  const p = PUI.positionModel(IN_TRADE, { position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY", invalidation_price: "59.90" }, { max_hold_sessions: 5 });
  assert.deepEqual([p.open, p.symbol, p.shares, p.entry, p.current, p.pnl, p.holding, p.status, p.state, p.invalidation], [true, "SNDK", 3, "61.20", "62.10", "+2.70 (+1.47%)", "Day 2 / 5", "Monitoring", "HEALTHY", "59.90"]);
  assert.equal(PUI.positionModel(IN_TRADE, null, { max_hold_sessions: 3 }).holding, "Day 2 / 3");            // the limit is read from /api/execution, not hard-coded
  assert.equal(PUI.positionModel(IN_TRADE, null, null).holding, "Day 2 / 5");
  assert.equal(PUI.positionModel({ ...IN_TRADE, key: "EXITING", reason: "Price reached the invalidation level" }, null, null).reason, "Price reached the invalidation level");
});

test("9 TRADE cards: Agent / Setup primary, Position, Risk; technical terms only behind Why?; Paper Auto no longer a card; [ STOP TRADING ] visible", () => {
  const h = PUI.cardsHtml({ ...CTX, dom: WATCH_DOM });
  assert.deepEqual([...h.matchAll(/data-pui-card="([a-z]+)"/g)].map((m) => m[1]), ["agent", "position", "risk"]);
  assert.ok(!h.includes('data-pui-card="market"') && !h.includes('data-pui-card="auto"') && !h.includes('data-pui="auto-on"'));
  const agent = cardOf(h, "agent");
  assert.ok(agent.includes('class="pui-card agent primary"') && agent.includes("data-pui-primary") && agent.includes('data-agent="WATCHING"'));
  const front = text(primaryPart(agent));
  assert.ok(front.includes("Agent · AAPL WATCHING Waiting for setup conditions Setup · Watching 2 conditions outstanding"), front);
  for (const term of ["PPE", "Demand", "Evidence", "Proposal", "Confidence", "Stored setup"]) assert.ok(!front.includes(term), term);   // hidden from the primary view
  assert.ok(agent.includes('data-pui-fold="why"') && agent.includes('data-pui="fold:why">Why?</summary>') && !agent.includes("<details class=\"pui-fold pui-why\" data-pui-fold=\"why\" open>"));
  const why = text(agent.slice(agent.indexOf("pui-why-body")));
  assert.ok(why.includes("PPE stretch +2") && why.includes("Confidence 62") && why.includes("Demand 71") && why.includes("Evidence strong") && why.includes("Stored setup state WATCH") && why.includes("Proposal status Not eligible")
            && why.includes("Buyer confirmation strengthened.") && why.includes("Not ready: demand 48 < 60 · confidence 31 < 40"), why);
  assert.ok(PUI.cardsHtml({ ...CTX, whyOpen: true }).includes('data-pui-fold="why" open>'));
  const pos = cardOf(h, "position");
  assert.ok(text(pos) === "Position No position" && pos.includes('data-open="0"'), text(pos));
  const risk = cardOf(h, "risk");
  assert.ok(risk.includes('data-risk="ok"') && text(primaryPart(risk)) === "Risk Trading allowed STOP TRADING", text(primaryPart(risk)));
  assert.ok(risk.includes('<button type="button" class="pui-btn stop" data-pui="stop">STOP TRADING</button>') && risk.includes('data-pui-fold="risk"') && risk.includes('data-pui="fold:risk">Details</summary>'));
  assert.ok(text(risk.slice(risk.indexOf("pui-why-body"))).includes("✓ Account data fresh ✓ Cash-flow data fresh ✓ Reconciliation healthy"));
  assert.ok(!words.test(text(h)) && !/never places a trade|no order path/i.test(text(h)));
});

test("10 TRADE cards in a live trade, a paused gate and a halt: simple position front, one risk reason, critical details exposed, stop disabled when halted", () => {
  const live = PUI.cardsHtml({ ...CTX, exec: { ...EXEC, mode: "PAPER_AUTO", open_positions: 1, cards: [IN_TRADE] }, card: IN_TRADE, position: { position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 1, state: "HEALTHY", invalidation_price: "59.90" }, symbol: "SNDK" });
  const agent = cardOf(live, "agent"), pos = cardOf(live, "position");
  assert.ok(agent.includes('data-agent="IN TRADE"') && text(primaryPart(agent)).includes("IN TRADE Monitoring exit conditions Monitoring exit conditions") === false);
  assert.ok(text(primaryPart(agent)).includes("Agent · SNDK IN TRADE Monitoring exit conditions"), text(primaryPart(agent)));
  assert.equal(text(primaryPart(pos)), "Position SNDK · 3 shares Entry 61.20 Current 62.10 P/L +2.70 (+1.47%) Day 1 / 5 · Monitoring");
  for (const raw of ["broker", "reconciliation", "client_order", "execution_id"]) assert.ok(!primaryPart(pos).toLowerCase().includes(raw), raw);
  assert.ok(pos.includes('data-pui-fold="pos"') && text(pos.slice(pos.indexOf("pui-why-body"))).includes("Position state HEALTHY Invalidation level 59.90 Status Monitoring"));
  const entering = cardOf(PUI.cardsHtml({ ...CTX, card: { key: "ENTERING", symbol: "AAPL", line: "Order submitted" } }), "position");
  assert.equal(text(entering), "Position Entering Waiting for broker fill");
  const paused = cardOf(PUI.cardsHtml({ ...CTX, exec: { ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "UNAVAILABLE" } } }, safety: { state: "NO_NEW_ENTRIES", reason_text: "" } }), "risk");
  assert.equal(text(primaryPart(paused)), "Risk New trades paused Account data unavailable STOP TRADING");
  assert.equal((primaryPart(paused).match(/⚠/g) || []).length, 0);                                                 // not four warnings for one root cause
  assert.ok(text(paused.slice(paused.indexOf("pui-why-body"))).includes("⚠ Account data unavailable ⚠ Cash-flow data unavailable ✓ Reconciliation healthy"));
  const halted = cardOf(PUI.cardsHtml({ ...CTX, exec: { ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }, safety: { state: "HALTED", reason_text: "Emergency stop active." } }), "risk");
  assert.ok(halted.includes('data-risk="bad"') && halted.includes('data-pui="stop" disabled>TRADING STOPPED</button>') && text(primaryPart(halted)).includes("Trading stopped Emergency stop active."));
  assert.ok(halted.includes('class="pui-sub pui-bad" data-pui-risk-reason') && text(halted).includes("RECONCILIATION_REQUIRED") && text(halted).includes("Clear it under View details"));
  assert.ok(cardOf(PUI.cardsHtml({ ...CTX, stopAvailable: false }), "risk").includes('data-pui="stop" disabled>STOP TRADING</button>'));
});

test("11 chart state: normal when candles exist; compact empty state only once the feed is settled; market closed is informational; no Retry", () => {
  const tv = (o) => ({ active: "MSFT", candles: 0, pending: { candles: false }, connState: "connected", ...o });
  assert.equal(PUI.chartModel(null).kind, "NONE");
  assert.deepEqual(PUI.chartModel(tv({ candles: 240 }), false), { kind: "OK", symbol: "MSFT" });
  assert.equal(PUI.chartModel(tv({ pending: { candles: true } }), true, "", Date.now()).kind, "LOADING");
  assert.equal(PUI.chartEmptyHtml(PUI.chartModel(tv({ pending: { candles: true } }), true)), "");                 // loading keeps the normal layout (no flicker on a switch)
  const closed = PUI.chartModel(tv(), false, "SIMULATED DATA");
  assert.deepEqual([closed.kind, closed.level, closed.title], ["CLOSED", "info", "Market closed"]);
  assert.ok(closed.text.includes("Next session data will appear automatically") && closed.text.includes("Monitoring and historical data remain available"));
  const nodata = PUI.chartModel(tv(), true, "LIVE DATA · IEX");
  assert.deepEqual([nodata.kind, nodata.level, nodata.title], ["NO_DATA", "warn", "Market data unavailable"]);
  assert.ok(nodata.text.includes("MSFT") && nodata.text.includes("LIVE DATA · IEX") && nodata.text.includes("return automatically when data reconnects"));
  const down = PUI.chartModel(tv({ connState: "reconnecting" }), true);
  assert.deepEqual([down.kind, down.level, down.title], ["DISCONNECTED", "warn", "Market data unavailable"]);
  PUI.state.pendingSince = Date.now() - 9000;                                                                       // "Loading candles" for longer than the grace period
  assert.equal(PUI.chartModel(tv({ pending: { candles: true } }), true, "", Date.now()).kind, "NO_DATA");
  PUI.state.pendingSince = null;
  const h = PUI.chartEmptyHtml(closed);
  assert.ok(h.includes('class="pui-chart-empty info"') && h.includes('data-pui-chart="CLOSED"') && h.includes('role="status"') && h.includes("<h3>Market closed</h3>"));
  assert.ok(PUI.chartEmptyHtml(nodata).includes('class="pui-chart-empty warn"') && PUI.chartEmptyHtml({ kind: "OK" }) === "" && PUI.chartEmptyHtml(null) === "");
  assert.ok(!/retry/i.test(JS) && !/<button[^>]*>[^<]*Retry/i.test(h));                                             // no refresh action exists, so no fake Retry button
  assert.match(CSS, /body\.pui\.pui-chart-empty \.tv-chart-wrap \{ flex: 0 0 auto; height: 250px; min-height: 250px; \}/);
  assert.match(CSS, /body\.pui\.pui-chart-empty \.tv-grid \{ height: auto; \}/);
});

test("12 diagnostics: the legacy status chips collapse into one summary with an expander; faults keep their level; simulated data is a note, not a fault", () => {
  const chips = [{ text: "simulated data", warn: true }, { text: "positions ok" }, { text: "evidence not loaded" }, { text: "PPE not available" }, { text: "session weekend" }];
  const m = PUI.diagModel(chips);
  assert.deepEqual(m.items.map((i) => [i.text, i.level]), [["Simulated market data", "info"], ["Position source ok", "info"], ["Evidence not loaded", "info"], ["PPE unavailable", "info"], ["Market session weekend", "info"]]);
  assert.deepEqual([m.summary, m.level, m.count], ["Simulated market data", "info", 5]);
  const faults = PUI.diagModel([{ text: "market unavailable", warn: true }, { text: "positions partial", warn: true }, { text: "evidence not loaded" }]);
  assert.deepEqual([faults.summary, faults.level, faults.items.map((i) => i.level)], ["⚠ Some data unavailable", "warn", ["warn", "warn", "info"]]);
  const bad = PUI.diagModel([{ text: "market unavailable", bad: true }, { text: "positions ok" }]);
  assert.deepEqual([bad.summary, bad.level], ["⚠ Market data unavailable", "bad"]);                               // a critical item stays directly visible
  assert.deepEqual([PUI.diagModel([]).count, PUI.diagModel([]).summary, PUI.diagHtml(PUI.diagModel([]), false)], [0, "", ""]);
  const h = PUI.diagHtml(faults, false);
  assert.ok(h.startsWith('<details class="pui-fold pui-diag warn" data-pui-diag="warn"><summary data-pui="diag">') && h.includes("⚠ Some data unavailable") && h.includes("3 sources") && (h.match(/<li/g) || []).length === 3);
  assert.ok(PUI.diagHtml(faults, true).includes('data-pui-diag="warn" open>') && h.includes('<li class="warn">Market data unavailable</li>'));
  assert.match(CSS, /body\.pui\.pui-collapsed #tv-status-line \{ display: none; \}/);
  assert.match(CSS, /body\.pui:not\(\.pui-collapsed\) #pui-diag \{ display: none; \}/);                            // View details brings the legacy chips back unchanged
});

test("13 ACTIVITY timeline: persisted rows only (setups, proposals, orders, fills, positions, safety gate, reconciliation, validation), newest first", () => {
  const tl = PUI.timeline(SRC);
  const texts = tl.map((e) => e.text);
  assert.deepEqual(tl.map((e) => e.at), tl.map((e) => e.at).slice().sort().reverse());
  for (const want of ["AAPL → READY", "AAPL → invalidated · bounce failed", "AAPL · Proposal created · 2 shares", "AAPL · Risk checks passed", "MSFT · Blocked: demand na",
                      "AAPL · BUY submitted · 2 shares", "AAPL · BUY filled @ 227.10", "MSFT · BUY rejected · broker rejected", "AAPL · Position opened · 2 of 2 filled at 227.10",
                      "AAPL · Exit required · time exit", "AAPL · Trade closed · P/L +4.20", "Trading stopped (emergency stop)", "Emergency stop cleared", "Risk controls started",
                      "AAPL · Reconciliation: qty mismatch", "Validation session started", "Validation early stop · Paper Auto turned off · duplicate broker order"]) assert.ok(texts.includes(want), want);
  assert.ok(!texts.some((x) => /FILL_UPDATED|SAMPLE|ORDER_ADOPTED|adopted/i.test(x)));
  assert.deepEqual(new Set(tl.map((e) => e.kind)), new Set(["setup", "proposal", "order", "fill", "position", "exit", "safety", "reconciliation", "validation"]));
  assert.deepEqual(PUI.timeline({}), []);
  assert.deepEqual(PUI.timeline({ intents: [{ symbol: "X", intent_type: "ENTRY", side: "buy", qty: 1, state: "PENDING" }] }), []);   // no timestamp -> no entry (nothing invented)
  assert.deepEqual(PUI.todaySummary(SRC, { ...EXEC, open_positions: 1, attention_reasons: ["RECONCILIATION_REQUIRED"] }, "2026-10-21"), { trades: 1, open: 1, closed: 1, pnl: "+4.20", issues: 2 });
  assert.deepEqual(PUI.todaySummary(SRC, EXEC, "2026-10-22"), { trades: 0, open: 0, closed: 0, pnl: "—", issues: 0 });
});

test("14 ACTIVITY and POSITIONS views: the Today strip, the timeline, the Validation block, position cards and details", () => {
  const h = PUI.activityHtml(SRC, EXEC, FV, "2026-10-21");
  const t = text(h);
  for (const k of ["Trades", "Open", "Closed", "Paper P/L", "Issues"]) assert.ok(h.includes(`<h4>${k}</h4>`), k);
  assert.ok(h.includes("data-pui-timeline") && h.includes('data-pui-kind="fill"') && h.includes('class="pui-day">Today</li>'));
  assert.ok(h.includes("data-pui-validation") && t.includes("Sessions 7 / 30") && t.includes("INSUFFICIENT FOR STRATEGY CONCLUSION") && h.includes('href="forward_validation.html"') && h.includes("data-pui-alerts-fold"));
  assert.ok(text(PUI.activityHtml({}, null, null, "2026-10-21")).includes("Validation status unavailable.") && text(PUI.activityHtml({}, null, null, "2026-10-21")).includes("No activity yet."));
  assert.ok(!words.test(t));
  assert.ok(text(PUI.positionsHtml([])).startsWith("No open paper position."));
  const rows = PUI.positionCards({ ...EXEC, cards: [IN_TRADE, { key: "WATCHING", symbol: "AAPL" }, { key: "ENTERING", symbol: "MSFT", line: "Order submitted" }, { key: "CLOSED", symbol: "JPM" }] },
                                 [{ position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY" }]);
  assert.deepEqual(rows.map((r) => [r.card.symbol, r.agent.label, r.model.open]), [["SNDK", "IN TRADE", true], ["MSFT", "ENTERING", false]]);
  const ph = PUI.positionsHtml(rows);
  assert.ok(ph.includes('data-pui-pos="SNDK"') && text(ph).includes("Quantity 3 Entry 61.20 Current 62.10 P/L +2.70 (+1.47%) Holding Day 2 / 5") && ph.includes('data-pui-pos="MSFT"'));
  const d = text(PUI.positionDetailsHtml({ symbol: "SNDK", agent: "IN TRADE", setup: "READY FOR REVIEW", demand: "71", evidence: "strong", exit: "monitored · HEALTHY", risk: "Trading allowed", state: "HEALTHY", invalidation: "59.90",
                                           events: [{ at: "2026-10-21T14:36:07Z", kind: "OPENED", to_state: "OPEN" }], intents: [{ intent_type: "ENTRY", side: "buy", qty: 3, state: "FILLED", filled_qty: 3, filled_avg_price: "61.20", broker_order_id: "abcdef1234" }] }));
  for (const k of ["Setup state READY FOR REVIEW", "Broker orders and fills", "ENTRY buy 3 · filled · filled 3 @ 61.20 · broker abcdef12", "14:36 · opened → open"]) assert.ok(d.includes(k), k);
});

test("15 wording: standard empty / error messages, no stale sentences, the policy line, no raw exception names in the front", () => {
  for (const stale of ["never places a trade", "This tool never places", "no order path", "Not available in this version", "read-only Trader View", "never places, prepares"])
    assert.ok(!JS.includes(stale) && !CSS.includes(stale), stale);
  for (const msg of ["No position", "No active setup", "Waiting for market data", "Account data unavailable", "Market closed", "No activity yet", "Waiting for broker fill"]) assert.ok(JS.includes(msg), msg);
  assert.ok(JS.includes(PUI.TAGLINE) && JS.includes("Robinhood holdings are read only") && JS.includes("Paper orders go to the Alpaca PAPER account only"));
  assert.match(CSS, /body\.pui #tab-portfolio \.pf-readonly-note \{ display: none; \}/);
  assert.ok(!/Error\(|Exception|Traceback/.test(text(PUI.cardsHtml(CTX)) + text(PUI.statusHtml(EXEC, SAFE, {}))));
  assert.ok(!words.test(text(PUI.cardsHtml({ ...CTX, card: IN_TRADE, symbol: "SNDK" }))));
});

test("16 read-only wiring: GET routes only, the two actions delegate to the legacy buttons, no broker / Robinhood / model call, index.html carries two marked blocks", () => {
  assert.ok(!JS.includes('method: "POST"') && !JS.includes("/api/execution/auto") && !JS.includes("emergency-stop") && !JS.includes("pause-new-entries") && !JS.includes("/api/portfolio") && !JS.includes("/api/agent"));
  const routes = [...JS.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((m) => m[1]);
  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",
                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/"];
  assert.ok(routes.length >= 10 && routes.every((r) => allowed.includes(r)), routes);
  assert.ok(JS.includes(`'#tv-safety [data-safety="stop"]'`) && JS.includes(`'#tv-bot [data-bot="enable"]'`) && JS.includes(`'#tv-bot [data-bot="disable"]'`));
  assert.ok(!JS.includes("data-action") && !JS.includes("Prepare Buy") && !JS.includes("Prepare Sell") && !JS.includes("Add Alert"));   // no fake primary action
  assert.equal(HTML.match(/<!-- >>> Product UI 6\.1\.1/g).length, 2);
  assert.equal(HTML.match(/<!-- <<< Product UI 6\.1\.1 -->/g).length, 2);
  assert.ok(HTML.includes('<link rel="stylesheet" href="product_ui.css" />') && HTML.includes('<script src="product_ui.js"></script>'));
  assert.ok(HTML.lastIndexOf("<script") === HTML.indexOf('<script src="product_ui.js">'));
  assert.ok(typeof PUI.expandDetails === "function" && typeof PUI.collapseDetails === "function" && typeof PUI.go === "function" && typeof PUI.renderChart === "function");
});
