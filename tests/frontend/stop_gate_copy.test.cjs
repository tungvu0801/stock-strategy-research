// Emergency Stop order gate (EMERGENCY_STOP_ORDER_GATE_DESIGN.md revision 3.5; RISK_CONTROLS_PANEL_DESIGN.md section 4, the "After the order-gate PR"
// column): the kill case says "new entries", because the gate also refuses manual paper BUYs; the daily-loss halt keeps "automatic". A Pause, the
// daily-loss halt and unavailable daily-loss data each say they do not block manual paper orders (C-1); the kill never says that. A page that could
// not read the server, or a view without the server's flags, claims none of it.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const R = require("../../frontend/trader_render.js");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const EXEC = { mode: "MONITOR_ONLY", attention: false, attention_reasons: [], open_positions: 0, pending_entries: 0, reconciliation_healthy: true, environment: { paper: true, simulated: false },
               scheduler: { market: { regular_session: true, session_date: "2026-11-10" } }, account_data: { status: "FRESH", cash_flow: { status: "FRESH" } }, cards: [], recent: [] };
function view(over) {
  return Object.assign({ state: "TRADING_ALLOWED", state_label: "Trading allowed", icon: "🟢", allowed_new_entry: true, reason_codes: [], reason_text: "",
                         daily_loss: { status: "OK" }, kill_switch: { active: false }, no_new_entries: { active: false }, evaluated_at: "2026-11-10T15:00:00+00:00", disclaimer: "" }, over || {});
}
const ALLOWED = view();
const PAUSED = view({ state: "NO_NEW_ENTRIES", state_label: "New automatic entries paused", no_new_entries: { active: true }, reason_text: "New automatic entries paused by you" });
const DATA_NA = view({ state: "NO_NEW_ENTRIES", state_label: "New automatic entries blocked: daily-loss data unavailable", reason_text: "Account snapshot is stale", daily_loss: { status: "UNAVAILABLE" } });
const DAILY = view({ state: "HALTED", state_label: "New automatic entries stopped", reason_text: "Daily loss limit reached", daily_loss: { status: "TRIGGERED" } });
const KILLED = view({ state: "HALTED", state_label: "New entries stopped", kill_switch: { active: true }, reason_text: "Emergency stop active" });
const UNKNOWN = view({ state: "UNKNOWN", state_label: "Risk status unknown", reason_text: "Safety status unavailable" });
const C1 = { pause: "A Pause does not block manual paper orders.", daily: "The daily-loss halt does not block manual paper orders.",
             unavailable: "Unavailable daily-loss data does not block manual paper orders." };
const UNKNOWN_TEXT = "Automatic entries are blocked. Manual paper orders are refused if the Emergency Stop state cannot be read. While Emergency Stop is active, BUYs are refused and a SELL is checked.";
const REG = { strategies: [], sources: [], pipeline: [], quantconnect: { integrated: false, registered: 0 }, disclaimer: "" };
const DOM = { stretch: "-54", confidence: "58", demand: "48", evidence: "LOADED", evidenceText: "weak", setupState: "WATCH", setupLabel: "WATCH", setupReason: "Market Demand below 60.",
              blockers: ["demand 48 < 60"], proposalState: "NOT_ELIGIBLE" };
const CTX = { exec: EXEC, safety: ALLOWED, card: null, position: null, dom: DOM, symbol: "AAPL", limits: { max_hold_sessions: 5, max_open_positions: 10, near_close_minutes: 15 },
              avail: { auto: true, pause: true, resume: true, stop: true, clear: true }, confirm: null, registry: REG };

test("the strings are the panel design's 'After the order-gate PR' column, the same in the panel and the product shell", () => {
  assert.deepEqual(PUI.MANUAL_NOT_BLOCKED, C1);
  assert.deepEqual(R.SAFETY_MANUAL_NOT_BLOCKED, C1);
  assert.equal(PUI.UNKNOWN_EXPLAIN, UNKNOWN_TEXT);
  assert.equal(R.SAFETY_UNKNOWN_EXPLAIN, UNKNOWN_TEXT);
  assert.equal(PUI.STOP_ROW_TEXT, "Blocks new automatic entries and manual paper BUYs once saved. Nothing at Alpaca is cancelled.");
  assert.equal(PUI.CONFIRM.stop.text, "Once saved, this blocks new automatic entries and manual paper BUYs. An order already being sent can still reach Alpaca. Orders already at "
    + "Alpaca are not cancelled. Bot exit rules keep running while a position is monitored. A manual SELL of an existing long may be possible within the usual limits, including "
    + "20 confirmed orders a day, but is refused for a position the bot manages. The stop stays active until cleared manually.");
  assert.ok(!/immediately|all broker activity|cancels? (every|all)/i.test(PUI.CONFIRM.stop.text + PUI.STOP_ROW_TEXT + UNKNOWN_TEXT));   // section 4: no stop halts everything at once
});

test("the kill says new entries: agent line, system line and question; the daily-loss halt keeps 'automatic'; only the server's kill flag counts", () => {
  const agent = (st, unc, kill) => PUI.agentModel({ key: "NONE" }, "WATCH", st, EXEC, unc, kill);
  assert.deepEqual([agent("HALTED", false, true).key, agent("HALTED", false, true).text], ["STOPPED", "New entries stopped by risk controls"]);
  assert.equal(agent("HALTED", false, false).text, "New automatic entries stopped by risk controls");
  assert.equal(agent("UNKNOWN", false, true).text, "Risk status unknown");                                     // UNKNOWN is never the kill case
  assert.equal(agent("HALTED", true, true).key, "WATCHING");                                                   // an uncertain page never claims a stop
  assert.equal(PUI.systemModel(EXEC, KILLED).text, "NEW ENTRIES STOPPED");
  assert.equal(PUI.systemModel(EXEC, DAILY).text, "NEW AUTOMATIC ENTRIES STOPPED");
  assert.equal(PUI.systemModel({ ...EXEC, attention: true }, ALLOWED).text, "NEW AUTOMATIC ENTRIES STOPPED");
  assert.equal(PUI.systemModel(EXEC, { ...KILLED, uncertain: true, lastKnown: { label: "New entries stopped", at: Date.UTC(2026, 10, 10, 15) } }).text, "RISK STATUS UNCERTAIN");
  assert.equal(PUI.whyModel({ ...CTX, safety: KILLED }).question, "Why are new entries stopped?");
  assert.equal(PUI.whyModel({ ...CTX, safety: DAILY }).question, "Why are new automatic entries stopped?");
  assert.deepEqual([PUI.killOf(KILLED), PUI.killOf({ ...KILLED, uncertain: true }), PUI.killOf(DAILY), PUI.killOf(null), PUI.killOf({ state: "HALTED" })], [true, false, false, false, false]);
  assert.equal(PUI.riskModel(EXEC, KILLED).primary.split(" Bot exit")[0], "New entries blocked.");
  assert.equal(PUI.positionNote({ key: "IN_TRADE" }, EXEC, KILLED), "No new entries. Bot exit rules keep running for this position unless it needs attention.");
});

test("a saved kill shown by the latch overlay reads New entries stopped in the shell and in the panel; a server view keeps its own label", () => {
  assert.equal(PUI.KILL_LABEL, "New entries stopped");
  assert.equal(R.SAFETY_KILL_LABEL, "New entries stopped");
  const over = PUI.effectiveSafety(ALLOWED, { latchShown: true, latchKill: { active: true, activated_at: "2026-11-10T15:00:00+00:00" }, uncertain: false });
  assert.deepEqual([over.state, over.state_label, over.latch, PUI.killOf(over)], ["HALTED", "New entries stopped", true, true]);
  assert.equal(PUI.manualNotBlocked(over), "");                                                // the overlay is the kill: no manual-order sentence
  const panel = R.safetyHtml({ ...ALLOWED, state: "HALTED", state_label: "New automatic entries stopped", kill_switch: { active: true }, latch: true }, {});
  assert.match(panel, />🔴 New entries stopped</);
  assert.match(R.safetyHtml(DAILY, {}), />🔴 New automatic entries stopped</);                 // the daily-loss halt keeps its own label
  assert.match(R.safetyHtml({ ...DAILY, latch: true, kill_switch: { active: false } }, {}), />🔴 New entries stopped</);   // the overlay only exists for a saved kill
});

test("C-1: the risk card, the Automation row and the panel say what does not block manual paper orders; the kill never does", () => {
  const cases = [[PAUSED, C1.pause], [DAILY, C1.daily], [DATA_NA, C1.unavailable], [KILLED, ""], [ALLOWED, ""], [UNKNOWN, UNKNOWN_TEXT]];
  for (const [v, want] of cases) {
    assert.equal(PUI.riskModel(EXEC, v).explain, want, v.state);
    const html = R.safetyHtml(v, {});
    const lines = [...html.matchAll(/<div class="tv-safe-reason" data-safety-explain>(.*?)<\/div>/g)].map((m) => text(m[1]));
    assert.deepEqual(lines, want ? [want] : [], v.state);
    if (v !== UNKNOWN && want) assert.ok(PUI.automationTrading(v).endsWith(` ${want}`), PUI.automationTrading(v));
  }
  assert.ok(!/does not block manual/.test(PUI.automationTrading(KILLED) + PUI.riskModel(EXEC, KILLED).explain + R.safetyHtml(KILLED, {})));
});

test("C-1 is never claimed without the server's flags, on an uncertain page, or from the executor's state alone", () => {
  assert.equal(PUI.manualNotBlocked({ state: "HALTED" }), "");                                                   // no kill flag: the daily halt cannot be told apart
  assert.equal(PUI.manualNotBlocked({ ...DAILY, uncertain: true }), "");
  assert.equal(PUI.manualNotBlocked(null), "");
  assert.equal(PUI.riskModel({ ...EXEC, safety_state: "HALTED" }, null).explain, "");
  assert.equal(PUI.riskModel(EXEC, { ...PAUSED, uncertain: true, lastKnown: { label: "New automatic entries paused", at: 0 } }).explain, "");
  const unc = R.safetyHtml(DAILY, { uncertain: true, lastGoodAt: Date.UTC(2026, 10, 10, 15) });
  assert.ok(!unc.includes("data-safety-explain") && !text(unc).includes(C1.daily));
  const bare = R.safetyHtml({ state: "HALTED", state_label: "New automatic entries stopped" }, {});
  assert.ok(!bare.includes("data-safety-explain"));
});
