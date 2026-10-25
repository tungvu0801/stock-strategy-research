// Node tests for the Stage 6.1.1 product shell (frontend/product_ui.js): four primary sections (TRADE default · POSITIONS · ACTIVITY · MORE) over the
// unchanged legacy page, the user-facing state model (WATCHING · READY · ENTERING · IN TRADE · EXITING · CLOSED · ENTRY FAILED · NEEDS ATTENTION) mapped
// from the Stage 6.0 card keys / Stage 5.8 setup states / Stage 5.9.1 safety states, the simplified header (PAPER · MARKET · SYSTEM HEALTHY / DEGRADED /
// STOPPED · AUTO) with its detail expander, the three cards (Agent / Setup primary, Position, Risk with the Automation / Trading Controls), the
// explanatory confirmations in front of the legacy buttons, the compact chart empty state, the one-line diagnostics summary, the persisted-rows-only
// ACTIVITY timeline, the MORE groups, the current wording and the read-only wiring.
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
const AUTO = { ...EXEC, mode: "PAPER_AUTO" };
const SAFE = { state: "TRADING_ALLOWED", state_label: "Trading allowed", reason_text: "", kill_switch: { active: false }, no_new_entries: { active: false }, daily_loss: { status: "OK" } };
const PAUSED = { ...SAFE, state: "NO_NEW_ENTRIES", state_label: "New trades paused", reason_text: "New trades paused by you.", no_new_entries: { active: true, activated_by: "user" } };
const BLOCKED = { ...SAFE, state: "NO_NEW_ENTRIES", reason_text: "Daily loss control degraded: account data stale." };
const KILLED = { ...SAFE, state: "HALTED", state_label: "Trading stopped", reason_text: "Emergency stop active.", kill_switch: { active: true, activated_by: "user", activated_at: "2026-10-21T15:00:00Z" } };
const DAILY = { ...SAFE, state: "HALTED", reason_text: "Daily loss limit reached.", daily_loss: { status: "TRIGGERED", daily_loss_pct: "3.2" } };
const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "3 shares · entry 61.20 · now 62.10 · +2.70", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1",
                   state: "POSITION_OPEN", details: { sessions_held: 2 } };
const MU = { key: "IN_TRADE", symbol: "MU", label: "In trade", line: "4 shares · entry 830.00 · now 835.50 · +22.00", shares: 4, entry: "830.00", current: "835.50", pnl: "+22.00", pnl_pct: "0.66", position_id: "p2",
             state: "POSITION_OPEN", details: { sessions_held: 1 } };
const ENTERING = { key: "ENTERING", symbol: "AAPL", label: "Entering", line: "Waiting for the paper order to fill · 2 shares", shares: 2, state: "ENTRY_SUBMITTED", details: { intent: { state: "SUBMITTED" } } };
const FAILED = { key: "ATTENTION", symbol: "AAPL", label: "Needs attention", line: "Last paper order rejected (broker rejected)", state: "FAILED", details: { intent: { intent_type: "ENTRY", state: "REJECTED", failure_code: "BROKER_REJECTED" } } };
const CLOSED = { key: "CLOSED", symbol: "AAPL", label: "Closed", line: "Trade completed · +4.20 · Time exit after 5 sessions", shares: 2, entry: "227.10", current: "229.20", pnl: "+4.20", state: "CLOSED" };
const DOM = { stretch: "+1.8", confidence: "62", demand: "71", evidence: "LOADED", evidenceText: "strong", setupState: "READY", setupLabel: "READY FOR REVIEW", setupReason: "Buyer confirmation strengthened.",
              blockers: [], proposalState: "REVIEWABLE" };
const WATCH_DOM = { ...DOM, setupState: "WATCH", setupLabel: "WATCH", proposalState: "NOT_ELIGIBLE", blockers: ["demand 48 < 60", "confidence 31 < 40"] };
const AVAIL = { auto: true, pause: true, resume: true, stop: true, clear: true };
const CTX = { exec: EXEC, safety: SAFE, card: null, position: null, dom: DOM, symbol: "AAPL", limits: { max_hold_sessions: 5 }, avail: AVAIL, confirm: null };
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
const buttons = (html) => [...String(html).matchAll(/<button[^>]*data-pui="([a-z:-]+)"[^>]*>([\s\S]*?)<\/button>/g)].map((m) => [m[1], text(m[2])]);
const rows = (html) => Object.fromEntries([...String(html).matchAll(/data-pui-row="([a-z]+)" data-pui-status="([A-Z—]+)"/g)].map((m) => [m[1], m[2]]));

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
  assert.deepEqual(lab(ENTERING, "READY", "TRADING_ALLOWED", EXEC), ["ENTERING", "Order submitted. Waiting for broker reconciliation."]);
  assert.deepEqual(lab(IN_TRADE, "NONE", "TRADING_ALLOWED", EXEC), ["IN TRADE", "Existing trade continues. Exit conditions monitored."]);
  assert.deepEqual(lab({ key: "EXITING", line: "3 shares · Price reached the invalidation level" }, "NONE", "TRADING_ALLOWED", EXEC), ["EXITING", "3 shares · Price reached the invalidation level"]);
  assert.deepEqual(lab(CLOSED, "NONE", "TRADING_ALLOWED", EXEC), ["CLOSED", "Trade completed · +4.20 · Time exit after 5 sessions"]);
  assert.deepEqual(lab({ key: "CLOSED" }, "NONE", "TRADING_ALLOWED", EXEC), ["CLOSED", "Trade completed."]);
  assert.deepEqual(lab({ key: "ATTENTION", line: "3 shares · qty mismatch", shares: 3 }, "NONE", "TRADING_ALLOWED", EXEC), ["NEEDS ATTENTION", "3 shares · qty mismatch"]);
  assert.deepEqual(lab(FAILED, "NONE", "TRADING_ALLOWED", EXEC), ["ENTRY FAILED", "No position opened. No automatic replacement order."]);
  assert.deepEqual(lab({ key: "STOPPED" }, "NONE", "TRADING_ALLOWED", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(null, "WATCH", "HALTED", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(null, "WATCH", "UNKNOWN", EXEC), ["NEEDS ATTENTION", "Trading stopped by risk controls"]);
  assert.deepEqual(lab(IN_TRADE, "NONE", "HALTED", EXEC), ["IN TRADE", "Existing trade continues. Exit conditions monitored."]);   // an open paper position is still monitored
  assert.deepEqual(lab(FAILED, "NONE", "HALTED", EXEC), ["ENTRY FAILED", "No position opened. No automatic replacement order."]);
  assert.deepEqual(lab({ key: "WATCHING" }, "WATCH", "TRADING_ALLOWED", { ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }), ["NEEDS ATTENTION", "RECONCILIATION_REQUIRED"]);
  assert.deepEqual(Object.values(PUI.AGENT).map((x) => x[0]).filter((v, i, a) => a.indexOf(v) === i).sort(),
                   ["CLOSED", "ENTERING", "ENTRY FAILED", "EXITING", "IN TRADE", "NEEDS ATTENTION", "READY", "WATCHING"]);
});

test("4 Setup line: plain words from the stored setup state; a count only from the engine's own blocker list, never invented; no sentence repeated", () => {
  const sl = (card, dom) => PUI.setupLines(PUI.agentModel(card, dom.setupState, "TRADING_ALLOWED", EXEC), dom);
  assert.deepEqual(sl(null, WATCH_DOM), { line: "Setup · Watching", sub: "2 conditions outstanding" });
  assert.deepEqual(sl(null, { ...WATCH_DOM, blockers: ["demand 48 < 60"] }), { line: "Setup · Watching", sub: "1 condition outstanding" });
  assert.deepEqual(sl(null, { ...WATCH_DOM, blockers: [] }), { line: "Setup · Watching", sub: "Waiting for conditions" });
  assert.deepEqual(sl(null, { ...DOM, setupState: "NONE", blockers: [] }), { line: "Setup · No active setup", sub: "" });
  assert.deepEqual(sl(null, { ...DOM, setupState: "INVALIDATED" }), { line: "Setup · Invalidated", sub: "Waiting for a new setup" });
  assert.deepEqual(sl(null, { ...DOM, setupState: null }), { line: "Setup", sub: "Waiting for conditions" });
  assert.deepEqual(sl(null, DOM), { line: "Setup conditions met", sub: "" });
  assert.deepEqual(sl(ENTERING, DOM), { line: "", sub: "" });
  assert.deepEqual(sl(IN_TRADE, DOM), { line: "", sub: "" });
  assert.deepEqual(sl(CLOSED, DOM), { line: "Watching for the next setup", sub: "" });
  assert.deepEqual(sl(FAILED, DOM), { line: "Watching for the next setup", sub: "" });
  assert.ok(!/\d+ \/ \d+ conditions/.test(JS));                                                                     // no "2 / 4 conditions met": the engine exposes no total
});

test("5 SYSTEM HEALTHY / DEGRADED / STOPPED is derived from the existing gate, account, cash-flow, reconciliation and attention fields only", () => {
  const sys = (e, s) => { const m = PUI.systemModel(e, s); return [m.level, m.text, m.cls, m.primary]; };
  assert.deepEqual(sys(EXEC, SAFE), ["HEALTHY", "SYSTEM HEALTHY", "ok", ""]);
  assert.deepEqual(sys({ ...EXEC, account_data: { status: "STALE", cash_flow: { status: "FRESH" } } }, SAFE), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Account data stale"]);
  assert.deepEqual(sys({ ...EXEC, account_data: { status: "UNAVAILABLE" } }, { state: "NO_NEW_ENTRIES", reason_text: "" }), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Safety gate: New trades paused"]);
  assert.deepEqual(sys(EXEC, PAUSED), ["DEGRADED", "SYSTEM DEGRADED", "warn", "New trades paused by you."]);
  assert.deepEqual(sys({ ...EXEC, account_data: { status: "FRESH", cash_flow: { status: "AMBIGUOUS" } } }, SAFE), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Cash-flow data unclear"]);
  assert.deepEqual(sys({ ...EXEC, reconciliation_healthy: false }, SAFE), ["DEGRADED", "SYSTEM DEGRADED", "warn", "Reconciliation needs a look"]);
  assert.deepEqual(sys(EXEC, KILLED), ["STOPPED", "SYSTEM STOPPED", "bad", "Emergency stop active."]);
  assert.deepEqual(sys({ ...EXEC, safety_state: "UNKNOWN" }, null)[0], "STOPPED");
  assert.deepEqual(sys({ ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }, SAFE), ["STOPPED", "SYSTEM STOPPED", "bad", "Needs attention: reconciliation required"]);
  assert.deepEqual(sys(null, null), ["UNKNOWN", "SYSTEM —", "", ""]);                                             // nothing loaded: a dash, never a guess
  const items = PUI.systemModel({ ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "UNAVAILABLE" } } }, BLOCKED).items;
  assert.deepEqual(items.map((i) => [i.text, i.level]), [["Safety gate: New trades paused", "warn"], ["Account data unavailable", "warn"], ["Cash-flow data unavailable", "warn"], ["Reconciliation healthy", "ok"]]);
  assert.equal(items[0].reason, "Daily loss control degraded: account data stale.");
  assert.ok(!JS.includes("fetch(\"/api/system"));                                                                   // a display summary: no backend health state exists or is called
});

test("6 header status bar: PAPER · MARKET · SYSTEM · AUTO as statuses, the detail reasons behind the SYSTEM expander, no action button in the bar", () => {
  const segs = (e, s) => PUI.statusModel(e, s).map((x) => [x.key, x.text, x.cls]);
  assert.deepEqual(segs(EXEC, SAFE), [["paper", "PAPER", "ok"], ["market", "MARKET OPEN", "ok"], ["system", "SYSTEM HEALTHY", "ok"], ["auto", "AUTO OFF", ""]]);
  const degraded = { ...AUTO, environment: { simulated: true }, scheduler: { market: { regular_session: false } }, account_data: { status: "UNAVAILABLE" } };
  assert.deepEqual(segs(degraded, { state: "NO_NEW_ENTRIES" }), [["paper", "SIMULATED PAPER", "ok"], ["market", "MARKET CLOSED", ""], ["system", "SYSTEM DEGRADED", "warn"], ["auto", "AUTO ON", "ok"]]);
  assert.deepEqual(segs(null, null).map((x) => x[1]), ["PAPER", "MARKET —", "SYSTEM —", "AUTO —"]);
  const h = PUI.statusHtml(EXEC, SAFE, { sysOpen: false });
  assert.ok(h.includes('data-pui-seg="paper"') && h.includes('data-pui-seg="market"') && h.includes('data-pui-seg="system"') && h.includes('data-pui-seg="auto"') && h.includes('data-pui-auto="OFF"'));
  assert.ok(!h.includes('data-pui-seg="account"') && !h.includes('data-pui-seg="trading"') && !text(h).includes("ACCOUNT FRESH") && !text(h).includes("TRADING ALLOWED"));   // not repeated in the header
  assert.ok(h.includes('<button type="button" class="pui-seg ok" data-pui-seg="system" data-pui="system" aria-expanded="false"') && !h.includes("data-pui-system-details"));
  assert.deepEqual(buttons(h).map((b) => b[0]), ["system"]);                                                       // the only button: the SYSTEM expander (the controls live on the Risk card)
  assert.ok(h.includes(PUI.TAGLINE));
  const open = PUI.statusHtml({ ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "UNAVAILABLE" } } }, BLOCKED, { sysOpen: true });
  const d = text(open.slice(open.indexOf("data-pui-system-details")));
  assert.ok(open.includes("SYSTEM DEGRADED") && d.includes("Safety gate: New trades paused · Daily loss control degraded: account data stale.") && d.includes("Account data unavailable") && d.includes("Reconciliation healthy"), d);
  assert.ok(PUI.statusHtml(AUTO, SAFE, {}).includes('data-pui-auto="ON"') && PUI.statusHtml(AUTO, SAFE, {}).includes("AUTO ON"));
});

test("7 Risk: one headline and one primary reason in front; every dependency, reason and attention item behind Details; stopped states say so plainly", () => {
  const r = PUI.riskModel(EXEC, SAFE);
  assert.deepEqual([r.headline, r.level, r.stopped, r.killActive, r.primary, r.critical], ["Trading allowed", "ok", false, false, "", false]);
  assert.deepEqual(r.lines.map((l) => l.text), ["Trading allowed", "Account data fresh", "Cash-flow data fresh", "Reconciliation healthy"]);
  assert.deepEqual([PUI.riskModel({ ...EXEC, account_data: { status: "STALE", cash_flow: { status: "FRESH" } } }, SAFE).level, PUI.riskModel({ ...EXEC, account_data: { status: "STALE", cash_flow: { status: "FRESH" } } }, SAFE).primary], ["warn", "Account data stale"]);
  const paused = PUI.riskModel({ ...EXEC, account_data: { status: "UNAVAILABLE", cash_flow: { status: "AMBIGUOUS" } } }, PAUSED);
  assert.deepEqual([paused.headline, paused.level, paused.paused, paused.primary], ["New trades paused", "warn", true, "Existing positions continue normally."]);
  assert.deepEqual(paused.lines.map((l) => l.text), ["New trades paused", "New trades paused by you.", "Account data unavailable", "Cash-flow data unclear", "Reconciliation healthy"]);
  const blocked = PUI.riskModel({ ...EXEC, account_data: { status: "UNAVAILABLE" } }, { ...BLOCKED, reason_text: "" });
  assert.deepEqual([blocked.headline, blocked.paused, blocked.primary], ["New trades paused", false, "Account data unavailable"]);                 // one root cause, not four warnings
  const killed = PUI.riskModel({ ...EXEC, reconciliation_healthy: false, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }, KILLED);
  assert.deepEqual([killed.headline, killed.level, killed.stopped, killed.killActive, killed.critical, killed.primary], ["EMERGENCY STOP ACTIVE", "bad", true, true, true, "New entries blocked. Existing positions remain protected."]);
  assert.deepEqual(killed.lines.filter((l) => l.level === "bad").map((l) => l.text), ["Trading stopped", "Emergency stop active.", "Reconciliation needs a look", "RECONCILIATION_REQUIRED"]);   // critical details stay exposed
  const daily = PUI.riskModel(EXEC, DAILY);
  assert.deepEqual([daily.headline, daily.killActive, daily.primary], ["Trading stopped", false, "Daily loss limit reached."]);
  assert.deepEqual([PUI.riskModel(null, null).headline, PUI.riskModel(null, null).level, PUI.riskModel(null, null).primary], ["Risk status unknown", "bad", "Critical issue requires attention"]);
});

test("8 Position: no position / entering / in trade with the holding day from the backend limit; raw broker fields are not part of the model's front", () => {
  assert.deepEqual(PUI.positionModel(null, null, null), { open: false, text: "No position", entering: false, symbol: null });
  assert.deepEqual(PUI.positionModel(ENTERING, null, null).entering, true);
  const p = PUI.positionModel(IN_TRADE, { position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY", invalidation_price: "59.90" }, { max_hold_sessions: 5 });
  assert.deepEqual([p.open, p.symbol, p.shares, p.entry, p.current, p.pnl, p.holding, p.status, p.state, p.invalidation], [true, "SNDK", 3, "61.20", "62.10", "+2.70 (+1.47%)", "Day 2 / 5", "Monitoring", "HEALTHY", "59.90"]);
  assert.equal(PUI.positionModel(IN_TRADE, null, { max_hold_sessions: 3 }).holding, "Day 2 / 3");            // the limit is read from /api/execution, not hard-coded
  assert.equal(PUI.positionModel(IN_TRADE, null, null).holding, "Day 2 / 5");
  assert.equal(PUI.positionModel({ ...IN_TRADE, key: "EXITING", reason: "Price reached the invalidation level" }, null, null).reason, "Price reached the invalidation level");
});

test("9 TRADE cards: Agent / Setup primary, Position, Risk with the controls; technical terms only behind Why?; Emergency Stop visible and not implied active", () => {
  const h = PUI.cardsHtml({ ...CTX, dom: WATCH_DOM });
  assert.deepEqual([...h.matchAll(/data-pui-card="([a-z]+)"/g)].map((m) => m[1]), ["agent", "position", "risk"]);
  assert.ok(!h.includes('data-pui-card="market"') && !h.includes('data-pui-card="auto"'));
  const agent = cardOf(h, "agent");
  assert.ok(agent.includes('class="pui-card agent primary"') && agent.includes("data-pui-primary") && agent.includes('data-agent="WATCHING"'));
  const front = text(primaryPart(agent));
  assert.ok(front.includes("Agent · AAPL WATCHING Waiting for setup conditions Setup · Watching 2 conditions outstanding"), front);
  for (const term of ["PPE", "Demand", "Evidence", "Proposal", "Confidence", "Stored setup"]) assert.ok(!front.includes(term), term);   // hidden from the primary view
  assert.ok(agent.includes('data-pui-fold="why"') && agent.includes('data-pui="fold:why">Why?</summary>'));
  const why = text(agent.slice(agent.indexOf("pui-why-body")));
  assert.ok(why.includes("PPE stretch +2") && why.includes("Confidence 62") && why.includes("Demand 71") && why.includes("Evidence strong") && why.includes("Stored setup state WATCH") && why.includes("Proposal status Not eligible")
            && why.includes("Not ready: demand 48 < 60 · confidence 31 < 40"), why);
  assert.ok(PUI.cardsHtml({ ...CTX, whyOpen: true }).includes('data-pui-fold="why" open>'));
  const pos = cardOf(h, "position");
  assert.ok(text(pos) === "Position No position" && pos.includes('data-open="0"'), text(pos));
  const risk = cardOf(h, "risk");
  assert.ok(risk.includes('data-risk="ok"') && risk.includes('data-kill="0"') && text(primaryPart(risk)).startsWith("Risk Trading allowed Automation / Trading Controls Paper Auto OFF Enable Paper Auto"), text(primaryPart(risk)));
  assert.deepEqual(buttons(primaryPart(risk)), [["auto-on", "Enable Paper Auto"], ["pause", "Pause New Trades"], ["stop", "Emergency Stop"]]);   // one relevant button per row, nothing duplicated
  assert.ok(!text(risk).includes("ACTIVE") || text(risk).includes("INACTIVE"));                                      // the inactive stop never reads as active
  assert.ok(risk.includes('data-pui-fold="risk"') && text(risk.slice(risk.indexOf("pui-why-body"))).includes("✓ Account data fresh ✓ Cash-flow data fresh ✓ Reconciliation healthy"));
  assert.ok(!words.test(text(h)) && !/never places a trade|no order path|STOP TRADING/.test(text(h)));
});

test("10 controls: statuses from the payloads and exactly one relevant action per row; pause and resume are distinct; resume never clears the emergency stop", () => {
  const cm = (e, s, a) => { const m = PUI.controlsModel(e, s, a || AVAIL); return { auto: [m.auto.label, m.auto.action, m.auto.actionLabel], trading: [m.trading.label, m.trading.action, m.trading.actionLabel, m.trading.text], stop: [m.stop.label, m.stop.action, m.stop.actionLabel] }; };
  assert.deepEqual(cm(EXEC, SAFE), { auto: ["OFF", "auto-on", "Enable Paper Auto"], trading: ["ALLOWED", "pause", "Pause New Trades", "New entries allowed when every check passes."], stop: ["INACTIVE", "stop", "Emergency Stop"] });
  assert.deepEqual(cm(AUTO, SAFE).auto, ["ON", "auto-off", "Disable Auto"]);
  assert.equal(PUI.controlsModel(AUTO, SAFE).auto.text, "Automatic paper entries enabled.");
  assert.equal(PUI.controlsModel(EXEC, SAFE).auto.text, "Monitoring only. No new automatic entries. Existing positions continue to be monitored.");
  assert.deepEqual(cm(EXEC, PAUSED).trading, ["PAUSED", "resume", "Resume New Trades", "Existing positions continue normally."]);
  assert.deepEqual(cm(EXEC, PAUSED).stop, ["INACTIVE", "stop", "Emergency Stop"]);                                   // pausing is not stopping
  assert.deepEqual(cm(EXEC, BLOCKED).trading, ["PAUSED", "pause", "Pause New Trades", "Daily loss control degraded: account data stale."]);   // paused by a check: the real reason, the user may still pause
  assert.deepEqual(cm(EXEC, KILLED).trading, ["STOPPED", null, "Pause New Trades", "Blocked by the emergency stop."]);                          // no pause / resume while the stop is active
  assert.deepEqual(cm(EXEC, KILLED).stop, ["ACTIVE", "clear", "Clear Emergency Stop"]);
  const both = cm(EXEC, { ...KILLED, no_new_entries: { active: true } });
  assert.deepEqual([both.trading[1], both.stop], ["resume", ["ACTIVE", "clear", "Clear Emergency Stop"]]);          // resume lifts the pause only; the stop stays ACTIVE with its own Clear
  assert.deepEqual(cm(EXEC, DAILY).trading, ["STOPPED", null, "Pause New Trades", "Daily loss limit reached. Resets on the next trading day."]);
  assert.deepEqual(cm(EXEC, DAILY).stop, ["INACTIVE", "stop", "Emergency Stop"]);                                    // a daily halt is not an emergency stop: nothing to clear
  assert.deepEqual(cm(EXEC, { state: "UNKNOWN" }).trading.slice(0, 2), ["UNKNOWN", null]);
  assert.deepEqual(cm(EXEC, { state: "UNKNOWN" }).stop.slice(0, 2), ["INACTIVE", "stop"]);                           // the stop stays reachable while the gate is unknown
  assert.deepEqual(cm(null, null).auto.slice(0, 2), ["—", null]);
  const m = PUI.controlsModel(EXEC, SAFE, { stop: false, pause: false, auto: false });
  assert.deepEqual([m.stop.enabled, m.trading.enabled, m.auto.enabled], [false, false, false]);                    // availability mirrors the legacy buttons
  assert.ok(PUI.controlsModel(EXEC, SAFE).stop.text.includes("existing positions stay protected"));
});

test("11 confirmations: Enable Paper Auto, Emergency Stop and Clear Emergency Stop open an explanation first; Confirm / Cancel; one panel at a time; exact wording", () => {
  assert.equal(PUI.CONFIRM["auto-on"].text, "You are enabling automatic Alpaca PAPER trading. New eligible entries may be submitted automatically when all strategy and risk conditions pass. Live trading remains disabled.");
  assert.equal(PUI.CONFIRM.stop.text, "This immediately blocks all new entries. Existing positions remain monitored and protective exits remain allowed. The emergency stop stays active until cleared manually.");
  assert.equal(PUI.CONFIRM.clear.text, "This removes the emergency-stop latch. Trading resumes only if all safety checks pass.");
  const none = PUI.cardsHtml(CTX);
  assert.ok(!none.includes("data-pui-confirm"));
  const a = PUI.cardsHtml({ ...CTX, confirm: "auto-on" });
  assert.ok(a.includes('data-pui-confirm="auto-on"') && text(a).includes(PUI.CONFIRM["auto-on"].text) && a.includes('data-pui="auto-on-confirm">Confirm: enable Paper Auto</button>') && a.includes('data-pui="cancel">Cancel</button>'));
  assert.equal((a.match(/data-pui-confirm=/g) || []).length, 1);
  const s = PUI.cardsHtml({ ...CTX, confirm: "stop" });
  assert.ok(s.includes('data-pui-confirm="stop"') && text(s).includes(PUI.CONFIRM.stop.text) && s.includes('data-pui="stop-confirm">Confirm Emergency Stop</button>') && (s.match(/data-pui-confirm=/g) || []).length === 1);
  const c = PUI.cardsHtml({ ...CTX, safety: KILLED, confirm: "clear" });
  assert.ok(c.includes('data-pui-confirm="clear"') && text(c).includes(PUI.CONFIRM.clear.text) && c.includes('data-pui="clear-confirm">Confirm: clear Emergency Stop</button>'));
  assert.ok(a.includes('role="alertdialog"') && !PUI.confirmHtml("nope"));
  // the confirm buttons delegate to the legacy buttons: the two-click ones get their two clicks, nothing in the shell posts
  assert.ok(JS.includes('runAutoAction("enable", armed ? 1 : 2)') && JS.includes('runAutoAction("disable", 1)') && JS.includes('clickLegacy("clear", armed ? 1 : 2)') && JS.includes('clickLegacy("stop", 1)') && !JS.includes('method: "POST"'));
  assert.deepEqual(PUI.LEGACY, { "auto-on": '#tv-bot [data-bot="enable"]', "auto-off": '#tv-bot [data-bot="disable"]', pause: '#tv-safety [data-safety="pause"]', resume: '#tv-safety [data-safety="resume"]',
                                 stop: '#tv-safety [data-safety="stop"]', clear: '#tv-safety [data-safety="clear"]' });
});

test("12 Risk card states: paused, emergency stop active (Clear exposed), after a clear the backend truth is shown, never a forced Trading allowed", () => {
  const paused = cardOf(PUI.cardsHtml({ ...CTX, safety: PAUSED }), "risk");
  assert.ok(text(primaryPart(paused)).startsWith("Risk New trades paused Existing positions continue normally.") && buttons(paused).some((b) => b[0] === "resume" && b[1] === "Resume New Trades") && !buttons(paused).some((b) => b[0] === "pause"));
  const killed = cardOf(PUI.cardsHtml({ ...CTX, safety: KILLED }), "risk");
  assert.ok(killed.includes('data-kill="1"') && text(primaryPart(killed)).startsWith("Risk EMERGENCY STOP ACTIVE New entries blocked. Existing positions remain protected."), text(primaryPart(killed)));
  assert.deepEqual(buttons(primaryPart(killed)), [["auto-on", "Enable Paper Auto"], ["clear", "Clear Emergency Stop"]]);                     // no Emergency Stop, no Pause / Resume while active
  assert.ok(rows(killed).stop === "ACTIVE" && rows(killed).trading === "STOPPED");
  const cleared = cardOf(PUI.cardsHtml({ ...CTX, exec: { ...EXEC, account_data: { status: "UNAVAILABLE" } }, safety: { ...BLOCKED, reason_text: "" } }), "risk");
  assert.ok(text(primaryPart(cleared)).startsWith("Risk New trades paused Account data unavailable") && rows(cleared).stop === "INACTIVE" && rows(cleared).trading === "PAUSED");   // another blocker: the real reason
  const healthy = cardOf(PUI.cardsHtml(CTX), "risk");
  assert.ok(rows(healthy).trading === "ALLOWED" && rows(healthy).stop === "INACTIVE" && rows(healthy).auto === "OFF");
  assert.ok(!/exits? (are |remain )?(blocked|disabled|stopped)/i.test(JS) && !/reset trade/i.test(JS) && !/data-pui="reset/.test(JS));       // never implies exits are blocked; no Reset Trade
  assert.ok(!PUI.cardsHtml({ ...CTX, safety: KILLED }).includes("TRADING STOPPED"));
});

test("13 existing position with Paper Auto OFF or the emergency stop active; entering / failed / closed messaging; Disable Auto does not reset the position display", () => {
  const live = (exec, safety, card) => PUI.cardsHtml({ ...CTX, exec: { ...exec, open_positions: 1, cards: [card] }, safety, card, symbol: card.symbol });
  const off = live(EXEC, SAFE, MU);
  assert.ok(cardOf(off, "agent").includes('data-agent="IN TRADE"') && text(cardOf(off, "agent")).includes("New automatic entries are disabled. Existing position monitoring continues."));
  const on = live(AUTO, SAFE, MU);
  assert.ok(!text(cardOf(on, "agent")).includes("automatic entries are disabled") && cardOf(on, "position") === cardOf(off, "position"));   // the position card is identical ON vs OFF
  assert.ok(text(cardOf(off, "position")).startsWith("Position MU · 4 shares Entry 830.00 Current 835.50 P/L +22.00 (+0.66%) Day 1 / 5 · Monitoring"));
  const stopped = live(EXEC, KILLED, MU);
  assert.ok(cardOf(stopped, "agent").includes('data-agent="IN TRADE"') && text(cardOf(stopped, "agent")).includes("No new entries. Existing position remains protected.") && cardOf(stopped, "position") === cardOf(off, "position"));
  assert.ok(!/reset|abandon|delete/i.test(text(cardOf(stopped, "agent")) + text(cardOf(stopped, "position"))));
  assert.equal(PUI.positionNote(MU, EXEC, KILLED), "No new entries. Existing position remains protected.");
  assert.equal(PUI.positionNote(MU, AUTO, SAFE), "");
  assert.equal(PUI.positionNote(null, EXEC, KILLED), "");
  const entering = PUI.cardsHtml({ ...CTX, card: ENTERING });
  assert.ok(text(cardOf(entering, "agent")).includes("ENTERING Order submitted. Waiting for broker reconciliation.") && text(cardOf(entering, "position")) === "Position Entering Order submitted. Waiting for broker fill.");
  const failed = PUI.cardsHtml({ ...CTX, card: FAILED });
  assert.ok(cardOf(failed, "agent").includes('data-agent="ENTRY FAILED"') && text(cardOf(failed, "agent")).includes("ENTRY FAILED No position opened. No automatic replacement order."));
  assert.equal(text(cardOf(failed, "position")), "Position No position The entry did not fill. No automatic replacement order.");
  assert.ok(!/resubmit|replacement order will|retry/i.test(text(failed)));
  const closed = PUI.cardsHtml({ ...CTX, card: CLOSED });
  assert.ok(cardOf(closed, "agent").includes('data-agent="CLOSED"') && text(cardOf(closed, "agent")).includes("CLOSED Trade completed · +4.20") && text(cardOf(closed, "position")) === "Position No position");
});

test("14 chart state: normal when candles exist; compact empty state only once the feed is settled; market closed is informational; no Retry", () => {
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

test("15 diagnostics: the legacy status chips collapse into one summary with an expander; faults keep their level; simulated data is a note, not a fault", () => {
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

test("16 ACTIVITY timeline: persisted rows only (setups, proposals, orders, fills, positions, safety gate, reconciliation, validation), newest first", () => {
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

test("17 ACTIVITY and POSITIONS views: the Today strip, the timeline, the Validation block, position cards and details", () => {
  const h = PUI.activityHtml(SRC, EXEC, FV, "2026-10-21");
  const t = text(h);
  for (const k of ["Trades", "Open", "Closed", "Paper P/L", "Issues"]) assert.ok(h.includes(`<h4>${k}</h4>`), k);
  assert.ok(h.includes("data-pui-timeline") && h.includes('data-pui-kind="fill"') && h.includes('class="pui-day">Today</li>'));
  assert.ok(h.includes("data-pui-validation") && t.includes("Sessions 7 / 30") && t.includes("INSUFFICIENT FOR STRATEGY CONCLUSION") && h.includes('href="forward_validation.html"') && h.includes("data-pui-alerts-fold"));
  assert.ok(text(PUI.activityHtml({}, null, null, "2026-10-21")).includes("Validation status unavailable.") && text(PUI.activityHtml({}, null, null, "2026-10-21")).includes("No activity yet."));
  assert.ok(!words.test(t));
  assert.ok(text(PUI.positionsHtml([])).startsWith("No open paper position."));
  const rowsP = PUI.positionCards({ ...EXEC, cards: [IN_TRADE, { key: "WATCHING", symbol: "AAPL" }, ENTERING, CLOSED] }, [{ position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY" }]);
  assert.deepEqual(rowsP.map((r) => [r.card.symbol, r.agent.label, r.model.open]), [["SNDK", "IN TRADE", true], ["AAPL", "ENTERING", false]]);
  const ph = PUI.positionsHtml(rowsP);
  assert.ok(ph.includes('data-pui-pos="SNDK"') && text(ph).includes("Quantity 3 Entry 61.20 Current 62.10 P/L +2.70 (+1.47%) Holding Day 2 / 5") && ph.includes('data-pui-pos="AAPL"'));
  const d = text(PUI.positionDetailsHtml({ symbol: "SNDK", agent: "IN TRADE", setup: "READY FOR REVIEW", demand: "71", evidence: "strong", exit: "monitored · HEALTHY", risk: "Trading allowed", state: "HEALTHY", invalidation: "59.90",
                                           events: [{ at: "2026-10-21T14:36:07Z", kind: "OPENED", to_state: "OPEN" }], intents: [{ intent_type: "ENTRY", side: "buy", qty: 3, state: "FILLED", filled_qty: 3, filled_avg_price: "61.20", broker_order_id: "abcdef1234" }] }));
  for (const k of ["Setup state READY FOR REVIEW", "Broker orders and fills", "ENTRY buy 3 · filled · filled 3 @ 61.20 · broker abcdef12", "14:36 · opened → open"]) assert.ok(d.includes(k), k);
});

test("18 wording: standard empty / error messages, no stale sentences, the policy line, the control vocabulary, no raw exception names in the front", () => {
  for (const stale of ["never places a trade", "This tool never places", "no order path", "Not available in this version", "read-only Trader View", "never places, prepares", "STOP TRADING"])
    assert.ok(!JS.includes(stale) && !CSS.includes(stale), stale);
  for (const msg of ["No position", "No active setup", "Waiting for market data", "Account data unavailable", "Market closed", "No activity yet", "Waiting for broker fill", "Monitoring only. No new automatic entries.",
                     "Automatic paper entries enabled.", "Existing positions continue to be monitored.", "Existing positions continue normally.", "EMERGENCY STOP ACTIVE", "New entries blocked.",
                     "Existing positions remain protected.", "Disable Auto", "Pause New Trades", "Resume New Trades", "Clear Emergency Stop", "Emergency Stop",
                     "Checking Paper Auto status…", "Paper Auto status unavailable", "Could not enable Paper Auto. "]) assert.ok(JS.includes(msg), msg);
  assert.ok(JS.includes(PUI.TAGLINE) && JS.includes("Robinhood holdings are read only") && JS.includes("Paper orders go to the Alpaca PAPER account only"));
  assert.match(CSS, /body\.pui #tab-portfolio \.pf-readonly-note \{ display: none; \}/);
  assert.ok(!/Error\(|Exception|Traceback/.test(text(PUI.cardsHtml(CTX)) + text(PUI.statusHtml(EXEC, SAFE, {}))));
  assert.ok(!words.test(text(PUI.cardsHtml({ ...CTX, card: IN_TRADE, symbol: "SNDK" }))));
});

test("19 read-only wiring: GET routes only, every action delegates to a legacy button, no broker / Robinhood / model call, index.html carries two marked blocks", () => {
  assert.ok(!JS.includes('method: "POST"') && !JS.includes("/api/execution/auto") && !/\/api\/execution-safety\/(pause|resume|emergency|clear)/.test(JS) && !JS.includes("pause-new-entries") && !JS.includes("resume-new-entries") && !JS.includes("clear-emergency-stop"));   // the safety action routes are never called by the shell (the words "emergency-stop latch" are wording, not a route)
  assert.ok(!JS.includes("/api/portfolio") && !JS.includes("/api/agent"));
  const routes = [...JS.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((m) => m[1]);
  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",
                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/"];
  assert.ok(routes.length >= 10 && routes.every((r) => allowed.includes(r)), routes);
  for (const sel of ['#tv-safety [data-safety="stop"]', '#tv-safety [data-safety="pause"]', '#tv-safety [data-safety="resume"]', '#tv-safety [data-safety="clear"]', '#tv-bot [data-bot="enable"]', '#tv-bot [data-bot="disable"]']) assert.ok(JS.includes(sel), sel);
  assert.ok(!JS.includes("data-action") && !JS.includes("Prepare Buy") && !JS.includes("Prepare Sell") && !JS.includes("Add Alert"));   // no fake primary action
  assert.equal(HTML.match(/<!-- >>> Product UI 6\.1\.1/g).length, 2);
  assert.equal(HTML.match(/<!-- <<< Product UI 6\.1\.1 -->/g).length, 2);
  assert.ok(HTML.includes('<link rel="stylesheet" href="product_ui.css" />') && HTML.includes('<script src="product_ui.js"></script>'));
  assert.ok(HTML.lastIndexOf("<script") === HTML.indexOf('<script src="product_ui.js">'));
  assert.ok(typeof PUI.expandDetails === "function" && typeof PUI.collapseDetails === "function" && typeof PUI.go === "function" && typeof PUI.renderChart === "function");
});

test("20 Paper Auto state sync: ON only from the authoritative mode, pending while a confirmed action is re-read, the backend failure text, header and row from one model", () => {
  const am = (e, s, p, err) => { const m = PUI.autoModel(e, s, p, err); return [m.key, m.label, m.header, m.action, m.actionLabel, m.text, m.failure]; };
  assert.deepEqual(am(EXEC, SAFE), ["OFF", "OFF", "AUTO OFF", "auto-on", "Enable Paper Auto", "Monitoring only. No new automatic entries. Existing positions continue to be monitored.", ""]);
  assert.deepEqual(am(AUTO, SAFE), ["ON", "ON", "AUTO ON", "auto-off", "Disable Auto", "Automatic paper entries enabled.", ""]);
  assert.deepEqual(am(AUTO, PAUSED).slice(0, 3), ["ON", "ON", "AUTO ON"]);
  assert.ok(am(AUTO, PAUSED)[5] === "Automatic paper entries enabled. Auto is armed, but safety currently blocks new entries.");     // safety PAUSED coexists with AUTO ON
  assert.deepEqual(am(AUTO, KILLED).slice(0, 3), ["ON", "ON", "AUTO ON"]);                                                        // the stop does not rewrite the mode: the backend does
  assert.deepEqual(am(EXEC, KILLED).slice(0, 3), ["OFF", "OFF", "AUTO OFF"]);
  assert.deepEqual(am(EXEC, SAFE, { kind: "enable", since: 0 }), ["PENDING", "…", "AUTO …", null, "", "Checking Paper Auto status…", ""]);
  assert.deepEqual(am(AUTO, SAFE, { kind: "disable", since: 0 }).slice(0, 3), ["PENDING", "…", "AUTO …"]);                        // never ON or OFF on a click alone
  assert.deepEqual(am(null, null), ["UNKNOWN", "—", "AUTO —", null, "", "Paper Auto status unavailable", ""]);
  assert.deepEqual(am({ ...EXEC, mode: null }, SAFE, null, "Paper bot status unavailable (HTTP 502)").slice(0, 3).concat([am({ ...EXEC, mode: null }, SAFE, null, "Paper bot status unavailable (HTTP 502)")[5]]), ["UNKNOWN", "—", "AUTO —", "Paper Auto status unavailable (HTTP 502)"]);
  const failed = am(EXEC, SAFE, null, "Enable Paper Auto failed (Paper environment not confirmed (CONNECTION))");
  assert.deepEqual([failed[0], failed[2], failed[3], failed[6]], ["OFF", "AUTO OFF", "auto-on", "Could not enable Paper Auto. Paper environment not confirmed (CONNECTION)"]);   // a failed enable stays OFF with the backend text
  assert.equal(PUI.autoFailureText("Stop New Trades failed (HTTP 500)"), "Could not disable Paper Auto. HTTP 500");
  assert.equal(PUI.autoFailureText("Enable Paper Auto failed (the mode did not change)"), "Could not enable Paper Auto. the mode did not change");
  assert.equal(PUI.autoFailureText(""), "");
  for (const [e, s, p, err] of [[EXEC, SAFE, null, null], [AUTO, SAFE, null, null], [AUTO, PAUSED, null, null], [EXEC, SAFE, { kind: "enable" }, null], [null, null, null, null], [EXEC, SAFE, null, "Enable Paper Auto failed (x)"]]) {
    const m = PUI.autoModel(e, s, p, err);
    const seg = PUI.statusModel(e, s, { autoPending: p, autoError: err })[3];
    const row = PUI.controlsModel(e, s, AVAIL, { autoPending: p, autoError: err }).auto;
    assert.deepEqual([seg.text, seg.state, row.label, row.action, row.failure], [m.header, m.key, m.label, m.action, m.failure]);   // header and control row: one model, one source
  }
  const h = PUI.cardsHtml({ ...CTX, autoError: "Enable Paper Auto failed (Paper environment not confirmed (CONNECTION))" });
  assert.ok(h.includes('data-pui-auto-error>Could not enable Paper Auto. Paper environment not confirmed (CONNECTION)</div>') && rows(h).auto === "OFF" && buttons(h).some((b) => b[0] === "auto-on"));
  const pending = PUI.cardsHtml({ ...CTX, autoPending: { kind: "enable", since: 0 } });
  assert.ok(pending.includes('data-pui-auto-state="PENDING"') && text(pending).includes("Checking Paper Auto status…") && !buttons(pending).some((b) => b[0].startsWith("auto-")));
  const bar = PUI.statusHtml(EXEC, SAFE, { autoPending: { kind: "enable", since: 0 } });
  assert.ok(bar.includes('data-pui-auto="PENDING"') && bar.includes("AUTO …") && !bar.includes("AUTO ON"));
  assert.ok(PUI.statusHtml(AUTO, SAFE, {}).includes('data-pui-auto="ON"') && PUI.statusHtml(EXEC, SAFE, {}).includes('data-pui-auto="OFF"') && PUI.statusHtml(null, null, {}).includes('data-pui-auto="UNKNOWN"'));
  // the re-read is the legacy Paper Bot refresh (GET /api/execution/status), awaited after the legacy POST settles; no second poll, no retry of the mutation
  assert.ok(JS.includes("await pb.refresh()") && !JS.includes('fetch("/api/execution/status"') && !/set(Timeout|Interval)\([^;]*clickLegacy/.test(JS));
  assert.equal((JS.match(/clickLegacy\(/g) || []).length, 5);                                                                     // the definition + runAutoAction + stop / clear / pause-resume, nothing else
  assert.equal((JS.match(/runAutoAction\(/g) || []).length, 3);                                                                   // the definition + the enable confirm + the disable
});
