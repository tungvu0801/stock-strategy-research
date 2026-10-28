// Node tests for the Stage 6.1.2 unified product flow (frontend/product_ui.js): exactly four primary sections and no legacy tab as a peer, the legacy deep-link map, MORE as
// three collapsed groups, the strategy display from the registry (placeholders never active, no invented score), the one blocker surface, the presentation-only priority model,
// READY as setup eligibility (not execution), the deterministic WHY panel, the Paper Auto / safety combinations, the position lifecycle, the de-duplicated activity timeline
// with filters and the Daily Review, the Strategy Lab registry / detail rendering, and the read-only wiring (GET routes only, no execution action in the lab).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PUI = require("../../frontend/product_ui.js");
require("../../frontend/forward_validation.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const JS = fs.readFileSync(path.join(FRONT, "product_ui.js"), "utf8");
const CSS = fs.readFileSync(path.join(FRONT, "product_ui.css"), "utf8");
const HTML = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const LEGACY_LABELS = [...HTML.matchAll(/class="tab-btn[^"]*" data-tab="[a-z]+">([^<]+)</g)].map((m) => m[1]);

const EXEC = { mode: "MONITOR_ONLY", attention: false, attention_reasons: [], open_positions: 0, pending_entries: 0, reconciliation_healthy: true, environment: { paper: true, simulated: false },
               scheduler: { market: { regular_session: true, session_date: "2026-10-21" } }, account_data: { status: "FRESH", cash_flow: { status: "FRESH" } }, cards: [], recent: [] };
const AUTO = { ...EXEC, mode: "PAPER_AUTO" };
const SAFE = { state: "TRADING_ALLOWED", state_label: "Trading allowed", reason_text: "", kill_switch: { active: false }, no_new_entries: { active: false }, daily_loss: { status: "OK" } };
const PAUSED = { ...SAFE, state: "NO_NEW_ENTRIES", state_label: "New trades paused", reason_text: "New trades paused by you.", no_new_entries: { active: true, activated_by: "user" } };
const KILLED = { ...SAFE, state: "HALTED", state_label: "Trading stopped", reason_text: "Emergency stop active.", kill_switch: { active: true, activated_by: "user" } };
const DAILY = { ...SAFE, state: "HALTED", reason_text: "Daily loss limit reached.", daily_loss: { status: "TRIGGERED", daily_loss_pct: "3.2" } };
const DDR = { strategy_id: "DOWNSIDE_DISLOCATION_RECOVERY", display_name: "Downside Dislocation Recovery", implemented: true, active: true, status: "PAPER_CANDIDATE", status_label: "Paper candidate", source: "INTERNAL",
              time_horizon: "SWING", direction_capability: "LONG", version: "1.0", updated_at: "2026-10-24", created_at: "2026-10-21", asset_class: "US_EQUITY" };
const PH = (id, name) => ({ strategy_id: id, display_name: name, implemented: false, active: false, status: "RESEARCH", status_label: "Research", source: "INTERNAL", time_horizon: "SWING", direction_capability: "LONG", version: "0.0", updated_at: "2026-10-25", created_at: "2026-10-25", asset_class: "US_EQUITY" });
const PLACEHOLDERS = [PH("VALUE_QUALITY", "Value + Quality"), PH("MOMENTUM", "Momentum"), PH("MEAN_REVERSION", "Mean Reversion"), PH("BREAKOUT", "Breakout")];
const REG = { strategies: [DDR, ...PLACEHOLDERS], sources: [{ source: "INTERNAL", count: 5, note: "" }, { source: "QUANTCONNECT", count: 0, note: "supported, none registered" }],
              pipeline: ["Strategy", "Opportunity / Proposal", "Portfolio", "Risk / Safety", "Execution", "Broker"], quantconnect: { integrated: false, registered: 0 }, disclaimer: "Strategies produce evidence and signals; no strategy executes." };
const IN_TRADE = { key: "IN_TRADE", symbol: "SNDK", label: "In trade", line: "3 shares · entry 61.20 · now 62.10 · +2.70", shares: 3, entry: "61.20", current: "62.10", pnl: "+2.70", pnl_pct: "1.47", position_id: "p1", state: "POSITION_OPEN", details: { sessions_held: 2 } };
const DOM = { stretch: "-54", confidence: "58", demand: "72", evidence: "LOADED", evidenceText: "weak", setupState: "READY", setupLabel: "READY FOR REVIEW", setupReason: "Buyer confirmation strengthened.", blockers: [], proposalState: "REVIEWABLE" };
const WATCH_DOM = { ...DOM, setupState: "WATCH", setupLabel: "WATCH", setupReason: "Market Demand below 60.", proposalState: "NOT_ELIGIBLE", blockers: ["demand 48 < 60", "confidence 31 < 40"], demand: "48", confidence: "31" };
const AVAIL = { auto: true, pause: true, resume: true, stop: true, clear: true };
const CTX = { exec: EXEC, safety: SAFE, card: null, position: null, dom: DOM, symbol: "AAPL", limits: { max_hold_sessions: 5, max_open_positions: 10, near_close_minutes: 15 }, avail: AVAIL, confirm: null, registry: REG };
const SRC = { transitions: [{ symbol: "AAPL", to_state: "READY", evaluated_at: "2026-10-21T14:35:00Z" }, { symbol: "AAPL", to_state: "INVALIDATED", reason_code: "BOUNCE_FAILED", evaluated_at: "2026-10-20T19:00:00Z" }],
              proposals: [{ symbol: "AAPL", status: "REVIEWABLE", final_qty: 2, created_at: "2026-10-21T14:36:00Z" }, { symbol: "MSFT", status: "BLOCKED", blockers: [{ code: "DEMAND_NA" }], created_at: "2026-10-21T13:00:00Z" }],
              intents: [{ symbol: "AAPL", intent_type: "ENTRY", side: "buy", qty: 2, state: "FILLED", submitted_at: "2026-10-21T14:36:05Z", resolved_at: "2026-10-21T14:36:07Z", filled_avg_price: "227.10" },
                        { symbol: "MSFT", intent_type: "ENTRY", side: "buy", qty: 1, state: "REJECTED", submitted_at: "2026-10-20T15:00:00Z", resolved_at: "2026-10-20T15:00:01Z", failure_code: "BROKER_REJECTED" }],
              positions: [{ position_id: "p1", symbol: "AAPL", state: "CLOSED", closed_at: "2026-10-21T19:50:00Z", realized_pnl: "4.20", entry_qty: 2, average_entry_price: "227.10", average_exit_price: "229.20", exit_reason: "TIME_EXIT", sessions_held: 5 }],
              position_events: [{ position_id: "p1", kind: "OPENED", at: "2026-10-21T14:36:07Z", reason: "2 of 2 filled at 227.10" }, { position_id: "p1", kind: "EXIT_REQUIRED", at: "2026-10-21T19:45:00Z", reason: "TIME_EXIT" },
                                { position_id: "p1", kind: "CLOSED", at: "2026-10-21T19:50:00Z", detail: { realized_pnl: "4.20" } }],
              safety_events: [{ kind: "KILL_ON", at: "2026-10-19T15:00:00Z" }, { kind: "KILL_OFF", at: "2026-10-19T15:10:00Z" }],
              findings: [{ kind: "QTY_MISMATCH", symbol: "AAPL", blocking: true, at: "2026-10-21T16:00:00Z" }],
              fv_events: [{ kind: "SESSION_OPENED", at: "2026-10-21T13:31:00Z" }] };
const FV = { sessions: { completed: 7, healthy: 7, degraded: 0, failed: 0, not_run: 0, in_progress: 1, target: 30 }, operational: { duplicate_broker_orders: 0, reconciliation_attention: 0, reconciliation_open: 0 },
             strategy: { closed_trades: 3, edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", insufficient: true, insufficient_text: "INSUFFICIENT FOR STRATEGY CONCLUSION", gross_pnl: "41.20" }, attention: [], needs_attention: false };
const cardOf = (html, key) => { const i = html.indexOf(`data-pui-card="${key}"`); const j = html.indexOf('<div class="pui-card', i + 1); return html.slice(html.lastIndexOf('<div class="pui-card', i), j < 0 ? undefined : j); };

test("1 exactly four primary sections; no legacy tab is a peer of them; the legacy bar stays hidden by the shell", () => {
  assert.deepEqual(PUI.NAV.map((n) => n.label), ["TRADE", "POSITIONS", "ACTIVITY", "MORE"]);
  assert.equal(LEGACY_LABELS.length, 13);
  for (const l of LEGACY_LABELS) assert.ok(!PUI.NAV.some((n) => n.label === l.toUpperCase() || n.label === l), l);          // Dashboard, Paper Trades, Trader View … are not peer navigation
  assert.match(CSS, /body\.pui nav\.tabs \{ display: none; \}/);
  assert.equal(JS.match(/const NAV = \[(.*?)\];/s)[1].match(/label: "/g).length, 4);
});

test("2 legacy deep links map to the new sections; a hash that belongs to a legacy consumer is left alone", () => {
  const r = (h) => { const x = PUI.routeFor(h); return x ? [x.section, x.filter, x.more] : null; };
  assert.deepEqual(r("#portfolio"), ["positions", null, null]);                                                           // old Portfolio -> POSITIONS
  assert.deepEqual(r("#paper"), ["activity", "trades", null]);                                                           // old Paper Trades -> ACTIVITY · Trades
  assert.deepEqual(r("#paper-trades"), ["activity", "trades", null]);
  assert.deepEqual(r("#trader"), ["trade", null, null]);                                                                 // old Trader View -> TRADE
  assert.deepEqual(r("#trader-view"), ["trade", null, null]);
  assert.deepEqual(r("#strategy"), ["more", null, "lab"]);                                                               // old Strategy Lab -> MORE › Research › Strategy Lab
  assert.deepEqual(r("#strategy-lab"), ["more", null, "lab"]);
  assert.deepEqual(r("#alerts"), ["activity", null, null]);
  assert.deepEqual(r("#daily"), ["activity", null, null]);
  assert.deepEqual(r("#review"), ["activity", null, null]);
  assert.deepEqual(r("#settings"), ["more", null, "settings"]);
  assert.deepEqual(r("#agent"), ["more", null, "diagnostics"]);                                                          // the AI Agent is a tool, not a primary tab
  assert.deepEqual(r("#forward-validation"), ["more", null, "system"]);
  assert.deepEqual(r("#watchlist"), ["trade", null, null]);
  for (const own of ["trade", "positions", "activity", "more"]) assert.equal(r(`#${own}`)[0], own);
  assert.deepEqual([r("#daily-brief"), r(""), r("#"), r("#nope")], [null, null, null, null]);                            // #daily-brief stays with brief_delivery.js
  assert.deepEqual(r("#Paper-Trades?x=1"), ["activity", "trades", null]);
  assert.ok(JS.includes('addEventListener("hashchange"') && JS.includes("replaceState") && JS.includes("routeFor(location.hash)"));
});

test("3 MORE: three collapsed groups (Research / System / Settings), every legacy workspace reachable, nothing rendered open by default", () => {
  const groups = PUI.moreGroups([]);
  assert.deepEqual(groups.map((g) => g.title), ["Research", "System", "Settings"]);
  assert.deepEqual(groups.map((g) => g.items.map((i) => i.label)), [["Strategy Lab", "Backtests (rule builder, legacy lab)", "Research History", "Research Setups"],
                                                                      ["Broker Connection", "Data Sources", "Forward Validation", "Diagnostics", "Logs"], ["Risk", "Automation", "Notifications", "Advanced configuration"]]);
  const hosts = new Set(PUI.NAV.map((n) => n.host));
  assert.deepEqual(new Set(PUI.moreTabs()), new Set([...HTML.matchAll(/data-tab="([a-z]+)"/g)].map((m) => m[1]).filter((t) => !hosts.has(t))));
  const h = PUI.moreHtml(groups, {});
  assert.equal((h.match(/<details class="pui-more-group"/g) || []).length, 3);
  assert.ok(!/<details class="pui-more-group"[^>]* open>/.test(h));                                                     // progressive disclosure: closed until asked
  assert.equal(h.match(/data-pui-group=/g).length, 3);
  for (const a of ["action:lab", "action:health", "action:data", "action:logs", "action:risk", "action:automation", "action:notifications", "action:settings", "tab:strategy", "tab:history", "tab:setups", "tab:agent", "tab:portfolio"]) assert.ok(h.includes(`data-pui-more="${a}"`), a);
  assert.ok(h.includes('data-pui-more="forward_validation.html"') && h.includes("Robinhood, read only"));
  assert.ok(PUI.moreHtml(groups, { research: true }).includes('data-pui-group="Research" open>'));
  assert.ok(!h.includes("Tools") || !/data-pui-group="Tools"/.test(h));
});

test("4 strategy display: the registry decides; one implemented strategy, placeholders never evaluated or supporting, no opportunity score invented", () => {
  const sm = PUI.strategyModel(null, DOM, REG, "REVIEWABLE");
  assert.deepEqual([sm.loaded, sm.best, sm.bestId, sm.evaluated, sm.placeholders, sm.supporting, sm.supportingText, sm.score, sm.scoreText, sm.ready, sm.label, sm.proposalLink],
                   [true, "Downside Dislocation Recovery", "DOWNSIDE_DISLOCATION_RECOVERY", 1, 4, [], PUI.NO_SUPPORT, null, PUI.NO_SCORE, true, "Best strategy", true]);
  assert.deepEqual([PUI.strategyModel(null, WATCH_DOM, REG, "NOT_ELIGIBLE").label, PUI.strategyModel(null, WATCH_DOM, REG, "NOT_ELIGIBLE").ready, PUI.strategyModel(null, WATCH_DOM, REG, "NOT_ELIGIBLE").proposalLink], ["Best setup", false, false]);
  const first = PUI.strategyModel(null, DOM, { strategies: [...PLACEHOLDERS, DDR] }, "REVIEWABLE");
  assert.equal(first.bestId, "DOWNSIDE_DISLOCATION_RECOVERY");                                                            // a placeholder listed first is never "best"
  const none = PUI.strategyModel(null, DOM, { strategies: PLACEHOLDERS }, "REVIEWABLE");
  assert.deepEqual([none.best, none.evaluated], [null, 0]);
  assert.deepEqual([PUI.strategyModel(null, DOM, null, null).loaded, PUI.strategyModel(null, DOM, null, null).best], [false, null]);
  const h = PUI.cardsHtml({ ...CTX, dom: WATCH_DOM });
  assert.ok(text(cardOf(h, "agent")).includes("Best setup · Downside Dislocation Recovery") && !text(cardOf(h, "agent")).includes("Momentum"));
  assert.ok(text(cardOf(PUI.cardsHtml({ ...CTX, registry: { strategies: PLACEHOLDERS } }), "agent")).includes("No strategy result available"));
  assert.ok(text(cardOf(PUI.cardsHtml({ ...CTX, registry: null }), "agent")).includes("Best strategy · —"));
  assert.ok(!/Momentum \d|Value \+ Quality \d|Opportunity Score \d/.test(JS));                                           // no fake score anywhere
});

test("5 one blocker surface: the first deterministic reason in precedence, consistent wording", () => {
  const b = (e, s, dom, t, l) => { const x = PUI.blockerModel(e, s, dom || {}, t || null, l || CTX.limits); return [x.level, x.title, x.reason, x.code]; };
  assert.deepEqual(b(EXEC, KILLED), ["blocked", "Cannot enter", "Trading safety is stopped (emergency stop).", "KILL_SWITCH_ACTIVE"]);
  assert.deepEqual(b(EXEC, DAILY), ["blocked", "Cannot enter", "Trading safety is stopped: Daily loss limit reached.", "SAFETY_HALTED"]);
  assert.deepEqual(b(EXEC, PAUSED), ["blocked", "Cannot enter", "Trading safety is paused: New trades paused by you.", "NO_NEW_ENTRIES"]);
  assert.deepEqual(b({ ...EXEC, reconciliation_healthy: false }, SAFE), ["blocked", "Cannot enter", "Reconciliation required.", "RECONCILIATION_REQUIRED"]);
  assert.deepEqual(b({ ...EXEC, account_data: { status: "STALE" } }, SAFE), ["blocked", "Cannot enter", "Account equity unavailable.", "ACCOUNT_STALE"]);
  assert.deepEqual(b({ ...EXEC, open_positions: 10 }, SAFE), ["blocked", "Cannot enter", "Maximum open positions reached.", "MAX_OPEN_POSITIONS"]);
  assert.deepEqual(b({ ...EXEC, open_positions: 9, pending_entries: 1 }, SAFE)[3], "MAX_OPEN_POSITIONS");
  assert.deepEqual(b({ ...EXEC, scheduler: { market: { regular_session: false } } }, SAFE).slice(0, 2).concat([b({ ...EXEC, scheduler: { market: { regular_session: false } } }, SAFE)[3]]), ["blocked", "Cannot enter", "MARKET_CLOSED"]);
  assert.deepEqual(b({ ...EXEC, scheduler: { market: { regular_session: true, et_time: "15:50" } } }, SAFE), ["blocked", "Cannot enter", "Near-close entry cutoff reached.", "NEAR_CLOSE"]);
  assert.equal(b({ ...EXEC, scheduler: { market: { regular_session: true, et_time: "15:40" } } }, SAFE)[0], "off");       // 20 minutes left: not near close yet
  const prop = { proposal: { effective_status: "BLOCKED", blockers: [{ code: "EQUITY_NOT_AVAILABLE", text: "Account equity is not available." }] } };
  assert.deepEqual(b(AUTO, SAFE, {}, prop), ["blocked", "Cannot enter", "Account equity is not available.", "EQUITY_NOT_AVAILABLE"]);
  assert.deepEqual(b(EXEC, SAFE), ["off", "Automatic entry off", "Monitor only: no order is sent. Paper Auto would allow automatic paper entries.", "MONITOR_ONLY"]);
  assert.deepEqual(b(AUTO, SAFE).slice(0, 2).concat([b(AUTO, SAFE)[3]]), ["ok", "Entry allowed when every check passes", "ALLOWED"]);
  assert.equal(b(null, null)[0], "unknown");
  assert.equal(PUI.minutesToClose("15:50"), 10);
  assert.equal(PUI.minutesToClose(null), null);
});

test("6 priority model: a system problem outranks a stock; exits before positions needing attention; degraded before READY / WATCHING; market summary last", () => {
  const mix = PUI.priorityModel({ exec: { ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"], reconciliation_healthy: false, account_data: { status: "STALE" },
                                          cards: [{ key: "EXITING", symbol: "SNDK", line: "3 shares · exit" }, { key: "ATTENTION", symbol: "MU", shares: 4, line: "4 shares · qty mismatch" }, { key: "IN_TRADE", symbol: "AAPL", line: "x" }] },
                                  safety: SAFE, card: null, dom: { setupState: "READY", proposalState: "REVIEWABLE" }, symbol: "NVDA" });
  assert.deepEqual(mix.items.map((i) => i.kind), ["ATTENTION", "EXIT", "POSITION_ATTENTION", "IN_TRADE", "REVIEWABLE", "MARKET"]);
  assert.deepEqual(mix.items.map((i) => i.rank), mix.items.map((i) => i.rank).slice().sort((a, b) => a - b));
  assert.equal(mix.focus.kind, "ATTENTION");
  const degraded = PUI.priorityModel({ exec: { ...EXEC, account_data: { status: "STALE" } }, safety: SAFE, card: null, dom: { setupState: "WATCH" }, symbol: "NVDA" });
  assert.deepEqual(degraded.items.map((i) => i.kind), ["DEGRADED", "WATCHING", "MARKET"]);                                 // system degraded outranks the WATCHING stock
  assert.equal(degraded.focus.title, "SYSTEM DEGRADED");
  assert.equal(PUI.priorityModel({ exec: EXEC, safety: PAUSED, card: null, dom: { setupState: "WATCH" }, symbol: "NVDA" }).focus.kind, "DEGRADED");
  const watch = PUI.priorityModel({ exec: EXEC, safety: SAFE, card: null, dom: { setupState: "WATCH" }, symbol: "NVDA" });
  assert.deepEqual([watch.items.map((i) => i.kind), watch.focus.title], [["WATCHING", "MARKET"], "NVDA · WATCHING"]);
  const ready = PUI.priorityModel({ exec: EXEC, safety: SAFE, card: { key: "READY", symbol: "MU" }, dom: { setupState: "READY" }, symbol: "MU" });
  assert.deepEqual([ready.items.map((i) => i.kind), ready.focus.text], [["READY", "MARKET"], PUI.READY_NOTE]);
  assert.equal(PUI.priorityModel({ exec: EXEC, safety: KILLED, card: null, dom: { setupState: "READY" }, symbol: "MU" }).focus.kind, "ATTENTION");
  assert.deepEqual(PUI.PRIORITY, ["ATTENTION", "EXIT", "POSITION_ATTENTION", "DEGRADED", "IN_TRADE", "REVIEWABLE", "READY", "WATCHING", "MARKET"]);
  const h = PUI.cardsHtml({ ...CTX, exec: { ...EXEC, account_data: { status: "STALE" } }, dom: WATCH_DOM });
  assert.ok(h.indexOf('data-pui-focus="DEGRADED"') >= 0 && h.indexOf('data-pui-focus="DEGRADED"') < h.indexOf('data-pui-card="agent"'));   // the first thing on TRADE is the system issue
  assert.ok(!/data-pui-card="[a-z]+"[^>]*data-pui-focus/.test(h) && [...h.matchAll(/data-pui-card="([a-z]+)"/g)].map((m) => m[1]).join() === "agent,position,risk");   // the strip is not a card
});

test("7 READY means setup eligibility: the execution line is separate and never implies an order will be sent", () => {
  const off = PUI.cardsHtml(CTX);
  assert.ok(cardOf(off, "agent").includes('data-agent="READY"') && off.includes('data-pui-exec="off"') && off.includes('data-pui-blocker="MONITOR_ONLY"'));
  assert.ok(text(cardOf(off, "agent")).includes("READY Eligible for entry review") && text(cardOf(off, "agent")).includes("Automatic entry off · Monitor only: no order is sent."));
  assert.ok(cardOf(off, "agent").includes('data-pui="proposal">View Proposal</button>') && cardOf(off, "agent").includes('data-pui="fold:why">Why?</button>'));
  const paused = PUI.cardsHtml({ ...CTX, safety: PAUSED });
  assert.ok(paused.includes('data-pui-exec="blocked"') && text(cardOf(paused, "agent")).includes("Cannot enter · Trading safety is paused: New trades paused by you."));
  const on = PUI.cardsHtml({ ...CTX, exec: AUTO });
  assert.ok(on.includes('data-pui-exec="ok"') && text(cardOf(on, "agent")).includes("Entry allowed when every check passes"));
  for (const h of [off, paused, on]) assert.ok(!/will (be )?(execut|plac|submit|fill)|order will be (placed|sent)|guaranteed|trade now/i.test(text(h)), text(h).slice(0, 200));
  const watch = PUI.cardsHtml({ ...CTX, dom: WATCH_DOM });
  assert.ok(!watch.includes("data-pui-exec") && !watch.includes("View Proposal"));                                        // the execution line and the proposal link belong to READY only
  assert.ok(!cardOf(PUI.cardsHtml({ ...CTX, dom: { ...DOM, proposalState: "NOT_ELIGIBLE" } }), "agent").includes("View Proposal"));
  assert.ok(JS.includes("function viewProposal()") && JS.includes('q("#tv-proposal")') && !JS.includes('method: "POST"'));   // View Proposal opens the Stage 5.9 review panel, nothing else
});

test("8 WHY panel: deterministic (same input, same output), seven sections in order, questions per state, backend evidence only", () => {
  const ctx = { ...CTX, dom: WATCH_DOM, frozen: { watch_enter_stretch: -40, ready_confidence: 40, ready_demand: 60 },
                terminal: { setup: { state: "WATCH", reason_text: "Market Demand below 60.", blockers: ["DEMAND_BELOW_60", "CONFIDENCE_BELOW_40"], setup_id: "abc123", since: "2026-10-21T14:00:00Z" }, proposal: { status: "NOT_ELIGIBLE", effective_status: "NOT_ELIGIBLE", warnings: [], blockers: [] } } };
  const w = PUI.whyModel(ctx);
  assert.deepEqual(w, PUI.whyModel(ctx));
  assert.deepEqual(w.sections.map((s) => s[0]), ["Signal", "Strategy", "Supporting evidence", "Against / risks", "Portfolio", "Safety", "Decision"]);
  assert.equal(w.question, "Why is this stock WATCHING?");
  assert.deepEqual(w.sections[0][1], ["Setup WATCH since 14:00 · Market Demand below 60.", "Not ready: demand below 60 · confidence below 40"]);
  assert.deepEqual(w.sections[2][1], ["PPE stretch -54 (needs <= -40)", "Confidence 31 (needs >= 40)", "Demand 48 (needs >= 60)", "Evidence weak"]);
  assert.deepEqual(w.sections[1][1][0], "Downside Dislocation Recovery (DOWNSIDE_DISLOCATION_RECOVERY · Paper candidate)");
  assert.deepEqual(w.sections[6][1], ["The engine holds WATCHING: Market Demand below 60.", "Execution: Automatic entry off - Monitor only: no order is sent. Paper Auto would allow automatic paper entries."]);
  assert.ok(w.technical.some(([k, v]) => k === "Setup id" && v === "abc123") && w.technical.some(([k]) => k === "Blocker code"));
  assert.equal(PUI.whyModel(CTX).question, "Why is it READY?");
  assert.deepEqual(PUI.whyModel(CTX).sections[6][1][0], PUI.READY_NOTE);
  const held = PUI.whyModel({ ...CTX, card: IN_TRADE, position: { position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY", invalidation_price: "59.90", last_price: "62.10", demand_low_streak: 1 }, dom: { ...DOM, setupState: "NONE", stretch: "-33" } });
  assert.equal(held.question, "Why is the position still held?");
  assert.deepEqual(held.sections[6][1], ["Invalidation 59.90 vs current 62.10 - not reached", "Demand below 35: 1 / 2 consecutive evaluations", "Holding Day 2 / 5", "PPE stretch -33 vs exit level -20", "Held: no exit rule has fired."]);
  const closed = PUI.whyModel({ ...CTX, card: { key: "CLOSED", symbol: "AAPL", reason: "Maximum holding time reached" }, dom: { ...DOM, setupState: "NONE" } });
  assert.deepEqual([closed.question, closed.sections[6][1]], ["Why did the position exit?", ["Exit reason: Maximum holding time reached"]]);
  const h = PUI.cardsHtml({ ...ctx, whyOpen: true });
  const why = text(h.slice(h.indexOf("pui-why-body")));
  for (const k of ["Why is this stock WATCHING?", "Signal", "Strategy", "Supporting evidence", "Against / risks", "Portfolio", "Safety", "Decision", "Technical details", "Stored setup state WATCH", "Proposal status Not eligible"]) assert.ok(why.includes(k), k);
  assert.ok(!/Math\.random|Date\.now\(\)[^;]*why/i.test(JS.slice(JS.indexOf("function whyModel"), JS.indexOf("function positionLifecycle"))));   // no randomness, no clock inside the model
  assert.ok(!/anthropic|claude|openai|\/api\/ai/i.test(JS));                                                               // no model is consulted anywhere in the shell
});

test("9 Paper Auto and the safety gate stay independent; the valid combinations are named on the Risk card", () => {
  const s = (e, g) => PUI.sessionModel(e, g).headline;
  assert.deepEqual([s(AUTO, PAUSED), s(AUTO, KILLED), s(EXEC, SAFE), s(AUTO, SAFE), s(EXEC, PAUSED), s(AUTO, DAILY)],
                   ["AUTO ON · TRADING PAUSED", "AUTO ON · TRADING STOPPED", "AUTO OFF · TRADING ALLOWED", "AUTO ON · TRADING ALLOWED", "AUTO OFF · TRADING PAUSED", "AUTO ON · TRADING STOPPED"]);
  assert.equal(PUI.sessionModel(AUTO, PAUSED).text, "Auto is armed, but safety currently blocks new entries. Existing positions stay monitored.");
  assert.equal(PUI.sessionModel(null, null).headline, "AUTO — · TRADING UNKNOWN");
  assert.equal(PUI.autoModel(AUTO, KILLED).key, "ON");                                                                     // the stop never rewrites the mode in the display
  assert.equal(PUI.controlsModel(EXEC, PAUSED, AVAIL).trading.label, "PAUSED");
  const h = PUI.controlsHtml(AUTO, PAUSED, AVAIL, null, {});
  assert.ok(h.includes('data-pui-session="AUTO ON · TRADING PAUSED"') && h.indexOf("data-pui-session") > h.indexOf('data-pui-row="stop"'));   // after the three real control rows
  assert.ok(PUI.sessionModel(EXEC, SAFE).future.includes("START TRADING SESSION") && !/data-pui="(start|session)/.test(JS));   // planned, not a fake button
});

test("10 position lifecycle: ENTERING · IN TRADE · EXIT REQUIRED · EXITING · CLOSED · NEEDS ATTENTION from the persisted position; nothing about the exit logic itself", () => {
  const lim = { max_hold_sessions: 5 };
  const open = PUI.positionLifecycle({ position_id: "p1", symbol: "SNDK", quantity: 3, average_entry_price: "61.20", last_price: "62.10", state: "HEALTHY", sessions_held: 2, invalidation_price: "59.90", risk_budget: "500.00", opened_at: "2026-10-20T14:36:07Z" },
                                     IN_TRADE, [], lim, "Downside Dislocation Recovery", [{ position_id: "p1", kind: "OPENED", reason: "3 of 3 filled at 61.20" }]);
  assert.deepEqual([open.stage, open.strategy, open.entryReason, open.holding, open.invalidation, open.riskBudget, open.pnl], ["IN TRADE", "Downside Dislocation Recovery", "READY setup with a reviewable proposal · 3 of 3 filled at 61.20", "Day 2 / 5", "59.90", "500.00", "+2.70 (+1.47%)"]);
  assert.deepEqual(open.whyHolding.slice(-1), ["Held: no exit rule has fired."]);
  const req = PUI.positionLifecycle({ position_id: "p1", symbol: "SNDK", quantity: 3, state: "EXIT_REQUIRED", exit_reason: "TIME_EXIT", sessions_held: 5 }, { ...IN_TRADE, key: "EXITING", reason: "Maximum holding time reached" }, [], lim, null, []);
  assert.deepEqual([req.stage, req.exitReason, req.holding], ["EXIT REQUIRED", "Maximum holding time reached", "Day 5 / 5"]);
  const exiting = PUI.positionLifecycle({ position_id: "p1", symbol: "SNDK", quantity: 3, state: "EXIT_SUBMITTED", exit_reason: "HARD_INVALIDATION" }, { ...IN_TRADE, key: "EXITING" }, [{ intent_type: "EXIT", state: "SUBMITTED", broker_status: "accepted" }], lim, null, []);
  assert.deepEqual([exiting.stage, exiting.exitReason, exiting.brokerStatus], ["EXITING", "Hard invalidation", "Submitted · broker accepted"]);
  const closed = PUI.positionLifecycle(SRC.positions[0], { key: "CLOSED", symbol: "AAPL" }, [], lim, null, []);
  assert.deepEqual([closed.stage, closed.pnl, closed.exitReason, closed.entry, closed.current, closed.holding, closed.whyHolding], ["CLOSED", "+4.20", "Time exit", "227.10", "229.20", "Day 5 / 5", []]);
  assert.deepEqual([PUI.positionLifecycle({ symbol: "MU", quantity: 4, state: "ATTENTION", attention_reason: "QTY_MISMATCH" }, { key: "ATTENTION", symbol: "MU", shares: 4 }, [], lim, null, []).stage, PUI.positionLifecycle({ symbol: "MU", quantity: 4, state: "ATTENTION", attention_reason: "QTY_MISMATCH" }, { key: "ATTENTION", symbol: "MU", shares: 4 }, [], lim, null, []).attention], ["NEEDS ATTENTION", "Qty mismatch"]);
  assert.equal(PUI.positionLifecycle(null, { key: "ENTERING", symbol: "AAPL", shares: 2 }, [], lim, null, []).stage, "ENTERING");
  const rows = PUI.positionCards({ ...EXEC, cards: [IN_TRADE] }, [{ position_id: "p1", symbol: "SNDK", quantity: 3, sessions_held: 2, state: "HEALTHY", invalidation_price: "59.90", last_price: "62.10" }], [], "Downside Dislocation Recovery");
  const ph = PUI.positionsHtml(rows);
  assert.ok(ph.includes('data-pui-stage="IN TRADE"') && text(ph).includes("Strategy Downside Dislocation Recovery") && text(ph).includes("Why still holding?") && text(ph).includes("Risk / invalidation 59.90"));
  const ch = PUI.closedHtml(SRC.positions, "Downside Dislocation Recovery", lim);
  assert.ok(ch.includes('data-pui-stage="CLOSED"') && text(ch).includes("Realized paper P/L +4.20") && text(ch).includes("Exit reason Time exit"));
  assert.ok(text(PUI.closedHtml([], null, lim)).includes("No closed paper trade yet."));
  assert.ok(!/reset trade|abandon|liquidate/i.test(ph + ch));
});

test("11 ACTIVITY: one de-duplicated timeline with filter groups; the Daily Review counts only what the persisted rows support; no market regime", () => {
  const items = PUI.activityItems(SRC);
  assert.equal(items.length, PUI.dedupe(PUI.timeline(SRC)).length);
  const dup = PUI.activityItems({ transitions: [SRC.transitions[0], { ...SRC.transitions[0] }] });
  assert.equal(dup.length, 1);                                                                                             // the same persisted row twice renders once
  assert.deepEqual(new Set(items.map((i) => i.group)), new Set(["research", "trades", "safety", "system"]));
  assert.ok(items.filter((i) => i.kind === "setup").every((i) => i.group === "research") && items.filter((i) => ["order", "fill", "position", "exit", "proposal"].includes(i.kind)).every((i) => i.group === "trades"));
  assert.ok(items.filter((i) => i.kind === "safety").every((i) => i.group === "safety") && items.filter((i) => ["reconciliation", "validation"].includes(i.kind)).every((i) => i.group === "system"));
  assert.equal(PUI.filterItems(items, "all").length, items.length);
  assert.ok(PUI.filterItems(items, "trades").every((i) => i.group === "trades") && PUI.filterItems(items, "trades").length > 0);
  assert.deepEqual(PUI.FILTERS.map((f) => f[0]), ["all", "trades", "safety", "system", "research"]);
  const dr = PUI.dailyReview(SRC, EXEC, SAFE, FV, "2026-10-21");
  assert.deepEqual(dr, { today: "2026-10-21", system: "HEALTHY", systemCls: "ok", market: "OPEN", setupChanges: 1, symbols: 1, ready: 1, entries: 1, exits: 0, closed: 1, blocked: 1, safetyBlocks: 0, pnl: "+4.20", attention: 1 });
  assert.deepEqual(PUI.dailyReview({}, null, null, null, "2026-10-22"), { today: "2026-10-22", system: "—", systemCls: "", market: "—", setupChanges: 0, symbols: 0, ready: 0, entries: 0, exits: 0, closed: 0, blocked: 0, safetyBlocks: 0, pnl: "—", attention: 0 });
  const h = PUI.activityHtml(SRC, EXEC, FV, "2026-10-21", { safety: SAFE, filter: "safety" });
  assert.ok(h.includes("data-pui-filters") && h.match(/data-pui-filter="/g).length === 5 && h.includes('data-pui-filter="safety">Safety</button>'));
  assert.ok([...h.matchAll(/data-pui-group="([a-z]+)"/g)].every((m) => m[1] === "safety") && h.includes('class="pui-chip active" data-pui-filter="safety"'));
  assert.equal((PUI.activityHtml(SRC, EXEC, FV, "2026-10-21", { filter: "all" }).match(/data-pui-kind=/g) || []).length, items.length);
  assert.ok(!/regime/i.test(JS));                                                                                          // no regime is implemented in the execution path, so none is shown
  assert.ok((h.match(/data-pui-timeline/g) || []).length === 1 && (h.match(/data-pui-today/g) || []).length === 1);       // one timeline, one review strip
});

test("12 Strategy Lab: the registry renders the real Downside Recovery entry, marks placeholders as not active, shows Not yet evaluated; no execution control in the lab", () => {
  const h = PUI.registryHtml(REG, [{ view: "fit", label: "Strategy Fit" }], null);
  assert.ok(h.includes('data-pui-strategy-row="DOWNSIDE_DISLOCATION_RECOVERY" data-pui-implemented="1" data-pui-active="1"') && text(h).includes("Downside Dislocation Recovery") && text(h).includes("Paper candidate"));
  for (const id of ["VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"]) assert.ok(h.includes(`data-pui-strategy-row="${id}" data-pui-implemented="0" data-pui-active="0"`), id);
  assert.equal((h.match(/placeholder · not active/g) || []).length, 4);
  assert.ok(h.includes('data-pui-source="INTERNAL"') && h.includes('data-pui-source="QUANTCONNECT"') && text(h).includes("QUANTCONNECT · 0") && text(h).includes("not integrated"));
  assert.ok(text(h).includes("Strategy → Opportunity / Proposal → Portfolio → Risk / Safety → Execution → Broker") && h.includes('data-pui-more="tab:strategy:fit"'));
  const det = { entry: { ...PLACEHOLDERS[1], source_label: "Internal", status_label: "Research" }, lifecycle: { label: "Research", declared: "RESEARCH", status: "RESEARCH", basis: ["placeholder: not implemented"], decision: "No approval or rejection has been recorded." },
                sections: [{ key: "overview", title: "Overview", state: "DOCUMENTED", items: ["Future internal strategy."], facts: {}, sources: ["strategy_registry/registry.py"], note: null },
                           { key: "backtest", title: "Backtest", state: "NOT_EVALUATED", items: [], facts: {}, sources: [], note: "Not yet evaluated" }, { key: "paper_results", title: "Paper results", state: "UNAVAILABLE", items: [], facts: {}, sources: [], note: "Unavailable in this process" }], links: {}, disclaimer: "d" };
  const d = PUI.strategyDetailHtml(det, []);
  assert.ok(d.includes('data-pui-lab-detail="MOMENTUM"') && d.includes('data-pui-section="backtest" data-pui-section-state="NOT_EVALUATED"') && text(d).includes("Not yet evaluated") && text(d).includes("Unavailable in this process"));
  assert.ok(d.includes("placeholder · not active") && d.includes('data-pui-lab="back"') && text(d).includes("placeholder: not implemented"));
  for (const html of [h, d]) assert.ok(!/data-pui="(auto-on|auto-off|stop|clear|pause|resume|proposal)"|Enable Paper Auto|Emergency Stop|Execute|Place order|Activate/.test(html), "no execution control in the lab");
  assert.ok(!/<form|<input/.test(h + d));                                                                                 // nothing to submit: the registry is read only
});

test("13 wiring: GET routes only (registry, frozen controls added), no new poll, no broker route, the legacy delegation untouched, the focus strip spans the grid", () => {
  const routes = [...JS.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((m) => m[1]);
  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",
                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/", "/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls",
                   "/api/execution/activity", "/api/account-refresh/positions", "/api/unified-portfolio",                                      // Stage 6.8: Bot Activity and the account positions (GET)
                   "/api/decision-flow", "/api/decision-flow/history", "/api/decision-flow/cycle", "/api/decision-flow/explain"];             // Stage 6.10C: Decision Flow and Decision history (GET)
  assert.ok(routes.every((r) => allowed.includes(r)), routes);
  assert.ok(routes.includes("/api/strategy-registry") && routes.includes("/api/strategy-registry/") && routes.includes("/api/forward-validation/frozen-controls"));
  assert.ok(!JS.includes('method: "POST"') && !/fetch\([^)]*\{[^}]*method/.test(JS) && !JS.includes("/api/portfolio") && !JS.includes("/api/rh") && !JS.includes("/api/alpaca") && !JS.includes("/api/execution/auto"));
  assert.equal((JS.match(/clickLegacy\(/g) || []).length, 5);
  assert.equal((JS.match(/runAutoAction\(/g) || []).length, 3);
  assert.ok(!/setInterval\([^)]*(loadRegistry|loadFrozen|loadTerminal|renderLab)/.test(JS));                              // the new reads are on demand, never a new timer poll
  assert.ok(JS.includes("if (state.whyOpen) loadTerminal(sym)") && JS.includes("Date.now() - t.at < 10000"));
  assert.match(CSS, /\.pui-focus \{ grid-column: 1 \/ -1;/);
  assert.match(CSS, /details\.pui-more-group > summary \{ cursor: pointer;/);
  assert.ok(JS.includes(PUI.TAGLINE) && JS.includes("No strategy result available") && JS.includes("Not yet evaluated"));
  assert.ok(!/\b(profitable|winning|guaranteed|buy now|sell now|you should)\b/i.test(JS));
});
