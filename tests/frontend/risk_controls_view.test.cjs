// Risk controls panel (RISK_CONTROLS_PANEL_DESIGN.md sections 3.3, 3.4, 4, 5, 6): the panel's markup row by row against section 5, and the
// product shell's one effective view. A page that could not read the server never says allowed, healthy or blocked; the server's own UNKNOWN
// says what it blocks; Emergency Stop is rendered and enabled in every row; the status bar keeps a Stop, its banner wins over everything.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../../frontend/trader_render.js");
const PUI = require("../../frontend/product_ui.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const read = (f) => fs.readFileSync(path.join(FRONT, f), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const T0 = Date.UTC(2026, 10, 9, 15, 0, 0);

function view(over) {
  return Object.assign({ state: "TRADING_ALLOWED", state_label: "Trading allowed", icon: "🟢", allowed_new_entry: true, reason_codes: [], reason_text: "",
                         daily_loss: { status: "OK" }, kill_switch: { active: false }, no_new_entries: { active: false }, evaluated_at: "2026-11-09T15:00:00+00:00", disclaimer: "" }, over || {});
}
const ALLOWED = view();
const PAUSED = view({ state: "NO_NEW_ENTRIES", state_label: "New automatic entries paused", no_new_entries: { active: true }, reason_text: "New automatic entries paused by you" });
const DATA_NA = view({ state: "NO_NEW_ENTRIES", state_label: "New automatic entries blocked: daily-loss data unavailable", reason_text: "Account snapshot is stale", daily_loss: { status: "UNAVAILABLE" } });
const DAILY = view({ state: "HALTED", state_label: "New automatic entries stopped", reason_text: "Daily loss limit reached", daily_loss: { status: "TRIGGERED" } });
const KILLED = view({ state: "HALTED", state_label: "New automatic entries stopped", kill_switch: { active: true }, reason_text: "Emergency stop active" });
const UNKNOWN = view({ state: "UNKNOWN", state_label: "Risk status unknown", reason_text: "Safety status unavailable" });
const UNKNOWN_KILL = view({ state: "UNKNOWN", state_label: "Risk status unknown", kill_switch: { active: true }, reason_text: "Safety status unavailable" });

function btns(html) {
  const out = {};
  for (const m of html.matchAll(/data-safety="(pause|resume|stop|clear)"( disabled)?/g)) out[m[1]] = m[2] ? "off" : "on";
  return out;
}
const head = (html) => text((html.match(/<span class="tv-safe-state[^>]*>(.*?)<\/span>/) || [])[1] || "");

// ---- section 5, row by row (the panel) ------------------------------------------------------------------------------------------------------
const ROWS = [
  ["First load, no read yet", null, { loading: true }, "⚪ Checking risk controls…", { stop: "on" }, []],
  ["First read failed", null, { loading: false, error: "Risk controls unavailable (HTTP 500)." }, "⚪ Risk status unknown", { pause: "on", stop: "on" }, ["Risk controls unavailable (HTTP 500)."]],
  ["Fresh, allowed", ALLOWED, {}, "🟢 Trading allowed", { pause: "on", resume: "off", stop: "on" }, []],
  ["Fresh, paused", PAUSED, {}, "🟡 New automatic entries paused", { pause: "off", resume: "on", stop: "on" }, ["Reason: New automatic entries paused by you"]],
  ["Fresh, no pause, daily data unavailable", DATA_NA, {}, "🟡 New automatic entries blocked: daily-loss data unavailable", { pause: "on", resume: "off", stop: "on" }, ["Reason: Account snapshot is stale"]],
  ["Fresh, HALTED by daily loss, kill off", DAILY, {}, "🔴 New automatic entries stopped", { pause: "on", resume: "off", stop: "on" }, ["Reason: Daily loss limit reached"]],
  ["Fresh, kill active", KILLED, { openOrders: { status: "FRESH", count: 2, at: "2026-11-09T14:32:00+00:00" } }, "🔴 New automatic entries stopped", { pause: "off", resume: "off", stop: "on", clear: "on" },
   ["Reason: Emergency stop active", "Open orders at Alpaca: 2, as of 14:32 UTC. This app does not cancel orders. Use Alpaca's website to cancel."]],
  ["Fresh, server UNKNOWN, kill not shown active", UNKNOWN, {}, "🔴 Risk status unknown", { pause: "on", resume: "off", stop: "on" },
   ["Reason: Safety status unavailable", "Automatic entries are blocked. This state does not by itself block manual paper orders."]],
  ["Fresh, server UNKNOWN, kill active", UNKNOWN_KILL, {}, "🔴 Risk status unknown · Emergency Stop active", { pause: "on", resume: "off", stop: "on", clear: "off" }, ["Reason: Safety status unavailable"]],
  ["Read failed or older than 65 s, last state with a kill", KILLED, { uncertain: true, lastGoodAt: T0 }, "⚪ Status uncertain · last known New automatic entries stopped at 15:00 UTC",
   { pause: "on", resume: "off", stop: "on", clear: "off" }, ["Could not confirm the current state."]],
  ["Read failed or older than 65 s, last state without a kill", ALLOWED, { uncertain: true, lastGoodAt: T0 }, "⚪ Status uncertain · last known Trading allowed at 15:00 UTC",
   { pause: "on", resume: "off", stop: "on" }, ["Could not confirm the current state."]],
  ["Stop POST in flight", ALLOWED, { pending: { stop: true }, outcomes: { stop: { result: "PENDING" } } }, "🟢 Trading allowed", { pause: "off", resume: "off", stop: "on" }, ["Sending Emergency Stop…"]],
  ["POST showed active, read failed", KILLED, { uncertain: true, outcomes: { stop: { result: "CONFIRMED", note: "posted-unread" } } }, "⚪ Status uncertain · last known New automatic entries stopped at —",
   { pause: "on", resume: "off", stop: "on", clear: "off" }, ["The server confirmed Emergency Stop, but the state could not be re-read."]],
  ["POST errored or was aborted, later read shows active", KILLED, { outcomes: { stop: { result: "CONFIRMED", note: "error-but-active" } } }, "🔴 New automatic entries stopped",
   { pause: "off", resume: "off", stop: "on", clear: "on" }, ["The request reported an error, but the server shows Emergency Stop active."]],
  ["A real read shows not active after every earlier action finished", ALLOWED, { outcomes: { stop: { result: "NOT_CONFIRMED" } } }, "🟢 Trading allowed", { pause: "on", resume: "off", stop: "on" },
   ["Emergency Stop NOT confirmed. Press it again."]],
  ["POST errored and reads fail", ALLOWED, { uncertain: true, outcomes: { stop: { result: "NOT_CONFIRMED" } } }, "⚪ Status uncertain · last known Trading allowed at —",
   { pause: "on", resume: "off", stop: "on" }, ["Emergency Stop NOT confirmed. Press it again."]],
  ["A stop confirmed earlier reads not active, not cleared by this page", ALLOWED, { announce: "Emergency Stop is no longer active." }, "🟢 Trading allowed", { pause: "on", resume: "off", stop: "on" },
   ["Emergency Stop is no longer active."]],
];
for (const [name, v, opts, headline, buttons, lines] of ROWS) {
  test(`section 5 row: ${name}`, () => {
    const html = R.safetyHtml(v, opts);
    assert.equal(head(html), headline);
    assert.deepEqual(btns(html), buttons);
    for (const l of lines) assert.ok(text(html).includes(l), `${l}\n${text(html)}`);
    assert.match(html, /data-safety="stop">Emergency Stop<\/button>/);              // Stop: rendered and enabled in every row
  });
}

test("a page that could not read the server never claims a state: no reason line, no explanation, no open-order line while uncertain", () => {
  const html = R.safetyHtml(KILLED, { uncertain: true, lastGoodAt: T0, openOrders: { status: "FRESH", count: 3, at: "2026-11-09T14:00:00+00:00" } });
  assert.ok(!text(html).includes("Reason:") && !text(html).includes("Open orders") && !text(html).includes("Automatic entries are blocked"));
  assert.ok(!/Trading allowed|New automatic entries stopped<\/span>/.test(html));
});

test("the open-order line (C-4): fresh, stale, starting, disabled, unavailable, no snapshot, the 500 limit; the same words in the panel and the shell", () => {
  const at = "2026-11-09T14:32:00+00:00";
  const cases = [[{ status: "FRESH", count: 2, at }, "Open orders at Alpaca: 2, as of 14:32 UTC. This app does not cancel orders. Use Alpaca's website to cancel."],
                 [{ status: "STALE", count: 0, at }, "Open orders at Alpaca: 0, as of 14:32 UTC (stale). This app does not cancel orders. Use Alpaca's website to cancel."],
                 [{ status: "STARTING", count: 1, at }, "Open orders at Alpaca: 1, as of 14:32 UTC (stale). This app does not cancel orders. Use Alpaca's website to cancel."],
                 [{ status: "DISABLED", count: 4, at }, "Open orders at Alpaca: unavailable. Check Alpaca's website."],
                 [{ status: "UNAVAILABLE", count: null, at: null }, "Open orders at Alpaca: unavailable. Check Alpaca's website."],
                 [{ status: "FRESH", count: null, at }, "Open orders at Alpaca: unavailable. Check Alpaca's website."],
                 [{ status: "FRESH", count: 500, at }, "Open orders at Alpaca: at least 500, as of 14:32 UTC. This app does not cancel orders. Use Alpaca's website to cancel."]];
  for (const [oo, want] of cases) { assert.equal(R.openOrdersLine(oo), want); assert.equal(PUI.openOrdersLine(oo), want); }
});

test("the pending and outcome lines read the same in the panel and in the Risk card", () => {
  const outcomes = [{ stop: { result: "PENDING" } }, { stop: { result: "NOT_CONFIRMED" } }, { stop: { result: "CONFIRMED", note: "posted-unread" } }, { stop: { result: "CONFIRMED", note: "error-but-active" } },
                    { pause: { result: "PENDING" }, resume: { result: "NOT_CONFIRMED" }, clear: { result: "NOT_CONFIRMED" } }];
  for (const oc of outcomes) {
    const a = R.safetyMessages({ outcomes: oc, announce: "Emergency Stop is no longer active." }).map((x) => x.text);
    const b = PUI.riskOutcomeLines({ outcomes: oc, announce: "Emergency Stop is no longer active." }, null, false).map((x) => x.text);
    assert.deepEqual(b, a);
  }
  assert.deepEqual(PUI.riskOutcomeLines({}, "stop", false).map((x) => x.text), ["Emergency Stop was not sent."]);
  assert.deepEqual(PUI.riskOutcomeLines({ pending: { stop: true } }, "stop", false).map((x) => x.text), []);   // not while a Stop is in flight
  assert.deepEqual(PUI.riskOutcomeLines({}, "stop", true).map((x) => x.text), []);                           // nor while one is active
  assert.deepEqual(PUI.riskOutcomeLines({}, "pause", false).map((x) => x.text), ["Pause was not sent."]);
});

// ---- the product shell's one effective view (3.4, M1; B12) --------------------------------------------------------------------------------
const EXEC = { mode: "PAPER_AUTO", safety_state: "TRADING_ALLOWED", account_data: { status: "FRESH" }, reconciliation_healthy: true, scheduler: { market: { regular_session: true } }, environment: {} };
const RC = (over) => Object.assign({ loading: false, uncertain: false, pending: {}, error: null, outcomes: {}, lastGoodAt: T0, announce: null, openOrders: null }, over || {});

test("effectiveSafety: fresh passes the server view through; uncertain, no status, or no view is UNKNOWN with a default kill and pause, the last known kept apart", () => {
  assert.equal(PUI.effectiveSafety(KILLED, RC()).kill_switch.active, true);
  for (const [sv, rc] of [[KILLED, RC({ uncertain: true })], [KILLED, null], [null, RC()]]) {
    const e = PUI.effectiveSafety(sv, rc);
    assert.equal(e.state, "UNKNOWN"); assert.equal(e.uncertain, true);
    assert.equal(e.kill_switch.active, false); assert.equal(e.no_new_entries.active, false);
  }
  assert.deepEqual(PUI.effectiveSafety(KILLED, RC({ uncertain: true })).lastKnown, { state: "HALTED", label: "New automatic entries stopped", kill: true, pause: false, at: T0 });
});

test("B12 uncertain: blocker, agent, AUTO note, position note, risk level, headline and system line never say allowed, healthy or blocked", () => {
  for (const sv of [KILLED, ALLOWED, null]) {
    const e = PUI.effectiveSafety(sv, RC({ uncertain: true }));
    const sys = PUI.systemModel(EXEC, e), risk = PUI.riskModel(EXEC, e), auto = PUI.autoModel(EXEC, e), cm = PUI.controlsModel(EXEC, e, {}, {});
    const agent = PUI.agentModel({ key: "NONE" }, "WATCH", e.state, EXEC, e.uncertain), bl = PUI.blockerModel(EXEC, e, {}, null, null, null);
    const sm = PUI.sessionModel(EXEC, e, {});
    assert.ok(["UNCERTAIN"].includes(sys.level) && sys.text === "RISK STATUS UNCERTAIN", sys.text);
    assert.equal(risk.headline, sv ? `Status uncertain · last known ${sv.state_label} at 15:00 UTC` : "Risk status unknown");   // the last state only as "last known"
    assert.equal(risk.primary, "Could not confirm the current state.");
    assert.ok(!/allowed|healthy|blocked|stopped/i.test(risk.primary), risk.primary);
    assert.equal(risk.level, "warn");                                                // never the colour of a claimed state
    assert.equal(risk.critical, false); assert.equal(risk.killActive, false);
    assert.ok(!/blocks/.test(auto.text) && auto.text.includes("could not be confirmed"), auto.text);
    assert.ok(!/blocks/.test(sm.text), sm.text);
    assert.notEqual(agent.key, "STOPPED");
    assert.equal(bl.code, "SAFETY_UNCERTAIN");
    assert.equal(cm.stop.label, "UNKNOWN"); assert.equal(cm.stop.action, "stop"); assert.equal(cm.stop.enabled, true);
    assert.equal(cm.trading.action, "pause");                                       // Pause only tightens: it stays offered
    assert.equal(PUI.positionNote({ key: "IN_TRADE" }, EXEC, e), "");
  }
});

test("server UNKNOWN is not uncertain: it says Risk status unknown and what it blocks; a kill read in UNKNOWN is ACTIVE, Clear mirrors the legacy button", () => {
  const e = PUI.effectiveSafety(UNKNOWN, RC());
  assert.equal(PUI.riskModel(EXEC, e).headline, "Risk status unknown");
  assert.equal(PUI.riskModel(EXEC, e).explain, "Automatic entries are blocked. This state does not by itself block manual paper orders.");
  assert.equal(PUI.systemModel(EXEC, e).text, "RISK STATUS UNKNOWN");
  assert.equal(PUI.agentModel({ key: "NONE" }, "WATCH", "UNKNOWN", EXEC, false).text, "Risk status unknown");
  const k = PUI.controlsModel(EXEC, PUI.effectiveSafety(UNKNOWN_KILL, RC()), { clear: false }, {});
  assert.deepEqual([k.stop.label, k.stop.action, k.stop.enabled], ["ACTIVE", "clear", false]);
  const u = PUI.controlsModel(EXEC, e, {}, {});
  assert.deepEqual([u.stop.label, u.stop.action, u.trading.action], ["UNKNOWN", "stop", "pause"]);
  // section 5: a server UNKNOWN with kill_switch.active true reads "Risk status unknown · Emergency Stop active", and Pause stays offered
  const ek = PUI.effectiveSafety(UNKNOWN_KILL, RC());
  assert.equal(PUI.riskModel(EXEC, ek).headline, "Risk status unknown · Emergency Stop active");
  assert.equal(k.trading.action, "pause");
  assert.equal(PUI.controlsModel(EXEC, PUI.effectiveSafety(KILLED, RC()), {}, {}).trading.action, null);   // a fresh, known kill: no Pause / Resume (today's rule)
});

test("the status bar never shows a stop in the present tense while the page is uncertain (section 5, 1a); a fresh confirmed stop reads 'Emergency Stop active'", () => {
  const confirmed = { stop: { result: "CONFIRMED" } };
  assert.equal(PUI.barModel({ rc: RC({ outcomes: confirmed }) }).outcome, "Emergency Stop active");
  for (const oc of [confirmed, { stop: { result: "CONFIRMED", note: "error-but-active" } }]) {
    const m = PUI.barModel({ rc: RC({ uncertain: true, outcomes: oc }) });
    assert.equal(m.outcome, "Last known: Emergency Stop active. Could not confirm the current state.");
    assert.equal(m.cls, "warn");
  }
  const html = PUI.statusHtml(EXEC, PUI.effectiveSafety(KILLED, RC({ uncertain: true, outcomes: confirmed })), { bar: PUI.barModel({ rc: RC({ uncertain: true, outcomes: confirmed }) }) });
  assert.ok(text(html).includes("RISK STATUS UNCERTAIN") && text(html).includes("Last known: Emergency Stop active.") && !/(^|[^:] )Emergency Stop active\b(?!\.)/.test(text(html).replace("Last known: Emergency Stop active.", "")), text(html));
});

test("first load (section 5 row 1): 'Checking risk controls…' and no message, never 'could not confirm' or a claimed state, while the first read is on its way", () => {
  const rc = RC({ loading: true, uncertain: true, lastGoodAt: null });
  const e = PUI.effectiveSafety(null, rc);
  assert.equal(PUI.checkingSafety(e), true);
  assert.equal(PUI.checkingSafety(PUI.effectiveSafety(null, RC({ loading: true, uncertain: true, error: "Risk controls unavailable (HTTP 500)." }))), false);   // a failed first read is no longer checking
  assert.equal(PUI.checkingSafety(PUI.effectiveSafety(ALLOWED, RC({ uncertain: true, loading: true }))), false);   // a last known state: uncertain, not checking
  const sys = PUI.systemModel(EXEC, e), risk = PUI.riskModel(EXEC, e), cm = PUI.controlsModel(EXEC, e, {}, {}), bl = PUI.blockerModel(EXEC, e, {}, null, null, null);
  assert.deepEqual([sys.level, sys.text], ["UNKNOWN", "SYSTEM —"]);
  assert.deepEqual([risk.headline, risk.primary], ["Checking risk controls…", ""]);
  assert.equal(cm.trading.text, "Checking risk controls…");
  assert.equal(bl.reason, "Checking risk controls…");
  assert.equal(PUI.autoModel(EXEC, e).text, "Automatic paper entries enabled.");
  assert.equal(PUI.sessionModel(EXEC, e, {}).text, "Checking risk controls…");
  assert.equal(PUI.automationTrading(e), "UNKNOWN · Checking risk controls…");
  const all = text(PUI.statusHtml(EXEC, e, { bar: PUI.barModel({ rc }) })) + " " + text(PUI.cardsHtml({ exec: EXEC, safety: e, card: null, position: null, dom: {}, symbol: "SNDK", limits: null, avail: {}, confirm: null, rc }));
  assert.ok(!all.includes("Could not confirm") && !all.includes("could not be confirmed") && !/RISK STATUS UNCERTAIN/.test(all), all.slice(0, 400));
  assert.ok(/Emergency Stop/.test(all));                                           // Stop is there from the first tick
});

test("presses are not lost (M8): setHtml compares with the string it generated, never with innerHTML; the status bar and the Risk card re-render only through it", () => {
  let writes = 0;
  const el = { puiHtml: undefined, set innerHTML(h) { writes++; this._h = h; }, get innerHTML() { return String(this._h).replace(/ disabled>/g, ' disabled="">'); } };
  PUI.setHtml(el, '<button disabled>Stop</button>'); PUI.setHtml(el, '<button disabled>Stop</button>');
  assert.equal(writes, 1, "the same generated string never re-renders, whatever innerHTML serialises to");
  PUI.setHtml(el, '<button>Stop</button>');
  assert.equal(writes, 2);
  const JS = read("product_ui.js");
  assert.ok(JS.includes('  function setHtml(el, html) { if (el.puiHtml !== html) { el.innerHTML = html; el.puiHtml = html; } }'));
  assert.equal((JS.match(/\bsetHtml\(/g) || []).length, 3);                         // the definition, renderStatus and renderCards
  assert.ok(/function renderStatus\(\) \{[\s\S]*?const ev = effView\(\);[\s\S]*?setHtml\(el, statusHtml\(exec\(\), ev,/.test(JS) && /setHtml\(host, cardsHtml\(\{ exec: exec\(\), safety: effView\(\),/.test(JS));
  assert.ok(JS.includes("rc: rcStatus(), port: pagePort(), killShown: !!(ev.kill_switch && ev.kill_switch.active === true) });"));   // the bar's present tense follows the same view
});

test("one effective view (M1, SD-LOW-4): the raw server view is read only inside effView(); MORE › Risk 'Gate now' and MORE › Automation read effView()", () => {
  const JS = read("product_ui.js");
  assert.equal((JS.match(/\bsafety\(\)/g) || []).length, 2, "safety() is defined once and called only by effView()");
  assert.ok(JS.includes("  function effView() { return effectiveSafety(safety(), rcStatus()); }"));
  const risk = JS.slice(JS.indexOf('if (name === "risk") {'), JS.indexOf('if (name === "automation") {'));
  const automation = JS.slice(JS.indexOf('if (name === "automation") {'), JS.indexOf("return;", JS.indexOf('if (name === "automation") {')));
  assert.ok(risk.includes("const sv = effView();") && risk.includes('["Gate now", sv.uncertain ? uncText(sv) : `${safetyLabel(sv)}'));
  assert.ok(automation.includes("const sv = effView();") && automation.includes('["Trading", automationTrading(sv)]'));
});

test("the Risk card shows the outcome lines, the not-sent line and the open-order line in an element visible in both layouts (pui-outcome)", () => {
  const ctx = { exec: EXEC, safety: KILLED, card: null, position: null, dom: {}, symbol: "SNDK", limits: null, avail: {}, confirm: null,
                rc: RC({ outcomes: { stop: { result: "NOT_CONFIRMED" } }, openOrders: { status: "FRESH", count: 3, at: "2026-11-09T14:00:00+00:00" } }), notSent: "pause" };
  const html = PUI.cardsHtml(ctx);
  // J-04 (design 2.1 Part D): the card's NOT confirmed line names the fallback command
  assert.match(html, /<div class="pui-outcome pui-bad" data-pui-outcome="stop">Emergency Stop NOT confirmed\. Press it again\. If it stays NOT confirmed, run the fallback command \(status bar, or README › Emergency Stop fallback\)\.<\/div>/);
  assert.match(html, /data-pui-outcome="not-sent">Pause was not sent\./);
  assert.match(html, /data-pui-outcome="orders">Open orders at Alpaca: 3, as of 14:00 UTC/);
  const css = read("product_ui.css");
  assert.ok(!/\.pui-outcome[^{]*\{[^}]*display:\s*none/.test(css) && !/pui-collapsed\)[^{]*\.pui-outcome/.test(css));
  assert.ok(/body\.pui:not\(\.pui-collapsed\) \.pui-card \.pui-ctl h5, body\.pui:not\(\.pui-collapsed\) \.pui-card \.pui-ctl-text \{ display: none; \}/.test(css), "the expanded layout shows the confirmation");
});

test("section 4 words: the bar names what is stopped, the Risk card's stop and pause notes say exit rules, never 'protected'", () => {
  assert.equal(PUI.systemModel(EXEC, KILLED).text, "NEW AUTOMATIC ENTRIES STOPPED");
  assert.equal(PUI.systemModel({ ...EXEC, attention: true }, ALLOWED).text, "NEW AUTOMATIC ENTRIES STOPPED");
  assert.equal(PUI.riskModel(EXEC, KILLED).primary, "New automatic entries blocked. Bot exit rules keep running while a position is monitored. A position marked Needs attention gets no automatic exit.");
  assert.equal(PUI.riskModel(EXEC, PAUSED).primary, "Bot exit rules keep running while a position is monitored. A position marked Needs attention gets no automatic exit.");
  assert.equal(PUI.controlsModel(EXEC, ALLOWED, {}, {}).stop.text, "Blocks new automatic entries once saved. Nothing at Alpaca is cancelled.");
  assert.equal(PUI.positionNote({ key: "IN_TRADE" }, EXEC, KILLED), "No new automatic entries. Bot exit rules keep running for this position unless it needs attention.");
  assert.equal(PUI.CONFIRM.stop.text, "Once saved, this blocks new automatic entries. A Paper Auto entry past its last check can still reach Alpaca. Orders already at Alpaca are not cancelled. Bot exit rules keep running while a position is monitored. The stop stays active until cleared manually.");
  assert.equal(PUI.agentModel({ key: "NONE" }, "WATCH", "HALTED", EXEC, false).text, "New automatic entries stopped by risk controls");
});

test("MORE › Automation 'Trading': one word and one sentence per case; never a manual-order sentence on the kill; a stale read never reads ALLOWED", () => {
  const RCs = RC();
  const cases = [[ALLOWED, "ALLOWED"], [PAUSED, "PAUSED · New automatic entries paused."], [DATA_NA, "BLOCKED · New automatic entries blocked: daily-loss data unavailable."],
                 [DAILY, "STOPPED · New automatic entries stopped."], [KILLED, "STOPPED · Blocks new automatic entries once saved. Nothing at Alpaca is cancelled."],
                 [UNKNOWN, "UNKNOWN · Risk status unknown. Automatic entries are blocked. This state does not by itself block manual paper orders."]];
  for (const [v, want] of cases) assert.equal(PUI.automationTrading(PUI.effectiveSafety(v, RCs)), want);
  assert.equal(PUI.automationTrading(PUI.effectiveSafety(ALLOWED, RC({ uncertain: true }))), "UNKNOWN · Could not confirm the current state. Last known: Trading allowed.");
  assert.equal(PUI.automationTrading(PUI.effectiveSafety(null, null)), "UNKNOWN · Could not confirm the current state.");
  assert.ok(!/manual/i.test(PUI.automationTrading(PUI.effectiveSafety(KILLED, RCs))));
});

test("the status bar: a Stop in every state; the banner wins over the outcome and the 'part of this page' line, and prints both fallback commands with this page's port", () => {
  const bar = (f) => PUI.statusHtml(EXEC, ALLOWED, { bar: PUI.barModel(f) });
  assert.match(bar({}), /data-pui="stop-bar"[^>]*>Emergency Stop<\/button>/);
  assert.match(PUI.statusHtml(EXEC, ALLOWED, {}), /data-pui="stop-bar"/);                  // no bar model given: the Stop is still there
  const pending = bar({ rc: RC({ outcomes: { stop: { result: "PENDING" } } }), tabsDead: true });
  assert.ok(text(pending).includes("Sending Emergency Stop…") && text(pending).includes("Part of this page did not load. Emergency Stop still works from this bar."));
  for (const f of [{ scriptsMissing: true }, { noLegacyStop: true }, { confirmHidden: true }]) {
    const html = bar({ ...f, tabsDead: true, confirm: true, rc: RC({ outcomes: { stop: { result: "PENDING" } } }), port: "8123" });
    const t = text(html);
    assert.ok(t.includes("Emergency Stop unavailable on this page. Page scripts did not load. Use the fallback command."), t);
    assert.ok(!t.includes("Part of this page") && !t.includes("Sending Emergency Stop") && !/data-pui="stop-bar"/.test(html) && !/data-pui-confirm/.test(html));
    assert.ok(t.includes('curl.exe -sS --max-time 60 -X POST http://127.0.0.1:8123/api/execution-safety/emergency-stop -H "X-Stock-Agent-Intent: dashboard" -H "Content-Type: application/json" -d "{}"'), t);
    // J-04: the second command reads the persisted latch alone (GET /api/execution-safety/latch, 15 s), never the full evaluation
    assert.ok(t.includes('(curl.exe -sS --max-time 15 http://127.0.0.1:8123/api/execution-safety/latch -H "X-Stock-Agent-Intent: dashboard" | ConvertFrom-Json).kill_active'), t);
  }
  const conf = bar({ confirm: true });
  assert.match(conf, /data-pui-confirm="stop-bar"/); assert.match(conf, /data-pui="stop-bar-confirm"/); assert.match(conf, /data-pui="stop-bar-cancel"/);
  assert.equal(PUI.barModel({ rc: RC({ outcomes: { stop: { result: "CONFIRMED" } } }) }).outcome, "Emergency Stop active");
  const nc = PUI.barModel({ rc: RC({ outcomes: { stop: { result: "NOT_CONFIRMED" } } }) });                                  // J-04: the bar's NOT confirmed carries the F2 commands
  assert.deepEqual([nc.outcome, nc.fallback], ["Emergency Stop NOT confirmed. Press it again. If it stays NOT confirmed, run the fallback command:", true]);
  // one message at a time: the model itself never carries the "part of this page" line beside the banner (3.4), not only the markup
  for (const f of [{ scriptsMissing: true }, { noLegacyStop: true }, { confirmHidden: true }]) assert.equal(PUI.barModel({ ...f, tabsDead: true }).partial, false);
  assert.equal(PUI.barModel({ tabsDead: true }).partial, true);
});

// ---- J-04 (design 2.1 Part D): the overlay, the slow-Stop texts, the fallback line and the paused line ---------------------------------------
test("J-04 the latch overlay wins over any server view in the effective model; the bar's NOT confirmed line prints both commands; the paused line shows only while this tab preempts and never beside the banner", () => {
  const over = RC({ latchKill: { active: true, at: T0, activated_at: "2026-11-10T15:00:00+00:00" } });
  for (const v of [ALLOWED, PAUSED, UNKNOWN, null]) {
    const e = PUI.effectiveSafety(v, over);
    assert.deepEqual([e.state, e.kill_switch.active, e.uncertain, e.latch], ["HALTED", true, false, true], JSON.stringify(v && v.state));
  }
  assert.equal(PUI.effectiveSafety(ALLOWED, RC({ latchKill: null })).state, "TRADING_ALLOWED");
  const aged = PUI.effectiveSafety(ALLOWED, RC({ latchKill: { active: true, at: T0 }, uncertain: true }));                     // the overlay aged out (65 s): last known, not a claim
  assert.deepEqual([aged.state, aged.uncertain, aged.lastKnown && aged.lastKnown.state], ["UNKNOWN", true, "TRADING_ALLOWED"]);
  assert.equal(PUI.automationTrading(PUI.effectiveSafety(ALLOWED, over)), "STOPPED · Blocks new automatic entries once saved. Nothing at Alpaca is cancelled.");
  const nc = PUI.barModel({ rc: RC({ outcomes: { stop: { result: "NOT_CONFIRMED" } } }), port: "8123" });
  const html = PUI.statusHtml(EXEC, ALLOWED, { bar: nc });
  assert.match(html, /data-pui-bar-fallback/);
  assert.ok(text(html).includes('(curl.exe -sS --max-time 15 http://127.0.0.1:8123/api/execution-safety/latch -H "X-Stock-Agent-Intent: dashboard" | ConvertFrom-Json).kill_active'));
  assert.ok(text(html).includes("Success is True from the second command; False means not saved"));
  const paused = PUI.barModel({ rc: RC({ preempting: true, outcomes: { stop: { result: "PENDING", at: T0 } }, now: T0 + 500 }) });
  assert.equal(paused.paused, true);
  assert.ok(text(PUI.statusHtml(EXEC, ALLOWED, { bar: paused })).includes(PUI.PANELS_PAUSED_TEXT));
  assert.equal(PUI.barModel({ scriptsMissing: true, tabsDead: true, rc: RC({ preempting: true }) }).paused, false);     // one message at a time: the banner wins
  assert.equal(PUI.barModel({ rc: RC({ preempting: false }) }).paused, false);
});

test("J-04 the pending Stop's texts: 'Sending' for 10 s, then 'checking the saved state' or 'not saved yet' from the latch answers; NOT confirmed names the fallback, in the bar and on the card", () => {
  const at = T0;
  const pend = (now, latchLast) => RC({ outcomes: { stop: { result: "PENDING", at } }, pending: { stop: true }, now, latchLast });
  assert.equal(PUI.barModel({ rc: pend(at + 9000, null) }).outcome, "Sending Emergency Stop…");
  assert.equal(PUI.barModel({ rc: pend(at + 10001, null) }).outcome, PUI.STOP_CHECKING_SAVED_TEXT);
  assert.equal(PUI.barModel({ rc: pend(at + 10001, { active: false, at: at + 9000 }) }).outcome, PUI.STOP_NOT_SAVED_TEXT);
  assert.equal(PUI.STOP_NOT_SAVED_TEXT, "Emergency Stop not saved yet. Checking the saved state every 2 s.");
  const card = (rc) => PUI.riskOutcomeLines(rc, null, false).map((x) => x.text);
  assert.deepEqual(card(RC({ outcomes: { stop: { result: "PENDING", at } }, now: at + 12000, latchLast: null })), [PUI.STOP_CHECKING_SAVED_TEXT]);
  assert.deepEqual(card(RC({ outcomes: { stop: { result: "NOT_CONFIRMED", at } } })), [PUI.STOP_NOT_CONFIRMED_TEXT]);
  assert.ok(PUI.STOP_NOT_CONFIRMED_TEXT.startsWith("Emergency Stop NOT confirmed. Press it again.") && /fallback command/.test(PUI.STOP_NOT_CONFIRMED_TEXT));
  assert.equal(PUI.barModel({ rc: RC({ outcomes: { stop: { result: "NOT_CONFIRMED" } } }) }).fallback, true);
  assert.equal(PUI.barModel({ rc: RC({ outcomes: { pause: { result: "NOT_CONFIRMED" } } }) }).fallback, false);   // only the Stop names the fallback
  assert.equal(PUI.STOP_SLOW_MS, 10000);
});

test("the bar's 'Emergency Stop active' is present tense only while a fresh view shows the kill; a server UNKNOWN's default kill switch reads 'last known' (3.1, B-3)", () => {
  const rc = RC({ outcomes: { stop: { result: "CONFIRMED" } } });
  assert.equal(PUI.barModel({ rc, killShown: true }).outcome, "Emergency Stop active");
  assert.equal(PUI.barModel({ rc, killShown: false }).outcome, "Last known: Emergency Stop active. Could not confirm the current state.");
});

test("the Trading row: BLOCKED (no Pause) is amber like PAUSED (section 5: 🟡); a stop is red; ALLOWED green", () => {
  const cls = (sv) => { const h = PUI.cardsHtml({ exec: EXEC, safety: PUI.effectiveSafety(sv, RC()), card: null, position: null, dom: {}, symbol: "SNDK", limits: null, avail: {}, confirm: null, rc: RC() });
    const m = h.match(/data-pui-row="trading" data-pui-status="([A-Z]+)"><span class="pui-ctl-name">[^<]*<\/span><b class="pui-ctl-status ([a-z-]+)">/); return m ? [m[1], m[2]] : null; };
  assert.deepEqual(cls(DATA_NA), ["BLOCKED", "pui-warn"]);
  assert.deepEqual(cls(PAUSED), ["PAUSED", "pui-warn"]);
  assert.deepEqual(cls(DAILY), ["STOPPED", "pui-bad"]);
  assert.deepEqual(cls(ALLOWED), ["ALLOWED", "pui-ok"]);
});

test("first load never hides an executor stop: attention set while the first safety read is on its way still reads NEW AUTOMATIC ENTRIES STOPPED", () => {
  const e = PUI.effectiveSafety(null, RC({ loading: true, uncertain: true, lastGoodAt: null }));
  const s = PUI.systemModel({ ...EXEC, attention: true, attention_reasons: ["RECONCILIATION_REQUIRED"] }, e);
  assert.deepEqual([s.level, s.text], ["STOPPED", "NEW AUTOMATIC ENTRIES STOPPED"]);
  assert.equal(PUI.systemModel(EXEC, e).text, "SYSTEM —");
});

test("the static notice needs no script: no handler, no inline script, the command and the README pointer; mount() removes it last and only if nothing threw", () => {
  const html = read("index.html");
  const notice = (html.match(/<div id="pui-boot-notice"[\s\S]*?<\/div>/) || [""])[0];
  assert.ok(notice.includes("If this message stays visible") && notice.includes("Emergency Stop button may not be available") && notice.includes('More in README.md, "Emergency Stop fallback".'));
  assert.ok(notice.includes('curl.exe -sS --max-time 60 -X POST http://127.0.0.1:8000/api/execution-safety/emergency-stop -H "X-Stock-Agent-Intent: dashboard"'));
  assert.ok(!/\son[a-z]+=|<script/i.test(notice));
  const css = read("product_ui.css");
  assert.ok(/\.pui-boot-notice \{ visibility: hidden; animation: pui-boot-show 0s linear 3s forwards;/.test(css) && css.includes("@keyframes pui-boot-show { to { visibility: visible; } }"));
  const js = read("product_ui.js");
  const mount = js.slice(js.indexOf("  function mount() {"), js.indexOf("  if (typeof document !== \"undefined\") {"));
  assert.ok(mount.indexOf("state.timer = setInterval(tick, 2000);") < mount.indexOf("let failed = null;") && mount.indexOf("let failed = null;") < mount.indexOf("try {\n    const nav"));
  assert.ok(mount.includes("    try { renderStatus(); } catch (e) { failed = e; state.mountError = String((e && e.message) || e); }"));   // a status-bar throw at mount keeps the notice
  assert.ok(mount.trimEnd().endsWith(`if (!failed && !state.statusError) { const n = $("pui-boot-notice"); if (n) n.remove(); }   // only if nothing threw (3.6), the status bar's first tick included\n  }`));
  assert.ok(js.includes("    try { renderStatus(); } catch (e) { state.statusError = String((e && e.message) || e); }"));   // tick(): a status-bar throw never stops the cards
});

test("the Trader View mounts Risk Controls directly after the shell, before anything that can throw", () => {
  const js = read("trader_view.js");
  const boot = js.slice(js.indexOf("  async function boot() {"), js.indexOf("  function activate() {"));
  assert.ok(boot.indexOf("shell();") < boot.indexOf('window.RiskControls.mount("tv-safety")') && boot.indexOf('window.RiskControls.mount("tv-safety")') < boot.indexOf("try {") && boot.indexOf("try {") < boot.indexOf("setupChart();"));
});

test("no overclaim word in the operator-facing scripts or the page (section 4, review HIGH-2)", () => {
  const OVER = /\bimmediately\b|remain protected|remains protected|stay protected|all new entries|Bot exits continue|exits continue|SYSTEM STOPPED|decides its exit|Trading stopped/;
  const codeOnly = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/.*$/gm, "$1");
  for (const f of ["product_ui.js", "trader_render.js", "risk_controls.js", "index.html"]) assert.doesNotMatch(codeOnly(read(f)), OVER, f);
});

test("XSS: every server- or error-supplied string the panel adds renders as text in the panel, the status bar and the Risk card (XSS-01 regression)", () => {
  const H = `<img src=x onerror="alert(1)">'&`;
  const hostile = (over) => view(Object.assign({ state_label: H, reason_text: H, error: H, evaluated_at: H, disclaimer: H,
    kill_switch: { active: true, activated_at: H, activated_by: H, reason: H }, no_new_entries: { active: true, activated_at: H, activated_by: H, reason: H },
    events: [{ at: H, kind: H, actor: H, reason: H }] }, over || {}));
  const OO = { status: H, count: H, at: H }, OO2 = { status: "FRESH", count: H, at: H };
  const outcomes = { stop: { result: "NOT_CONFIRMED", note: H }, pause: { result: "PENDING" }, resume: { result: H }, clear: { result: "CONFIRMED", note: H } };
  const raw = (html) => /<img|onerror="/i.test(html);
  for (const sv of [hostile(), hostile({ state: "HALTED" }), hostile({ state: "UNKNOWN" }), hostile({ state: H }), hostile({ state: "NO_NEW_ENTRIES", kill_switch: { active: false } })]) {
    for (const o of [{}, { detailsOpen: true, error: H, announce: H, openOrders: OO }, { uncertain: true, lastGoodAt: T0, error: H, outcomes, openOrders: OO2 }, { loading: true, error: H }]) {
      const html = R.safetyHtml(sv, o);
      assert.ok(!raw(html), html.slice(0, 300));
    }
    for (const rc of [RC({ error: H, announce: H, openOrders: OO, outcomes }), RC({ uncertain: true, error: H, announce: H, openOrders: OO2, outcomes }), null]) {
      const eff = PUI.effectiveSafety(sv, rc);
      const exec = { ...EXEC, attention: true, attention_reasons: [H], mode: H, account_data: { status: H, cash_flow: { status: H } } };
      for (const bar of [PUI.barModel({ rc, tabsDead: true, confirm: true, port: H }), PUI.barModel({ rc, scriptsMissing: true, port: H })]) {
        const st = PUI.statusHtml(exec, eff, { sysOpen: true, bar });
        assert.ok(!raw(st), st.slice(0, 300));
      }
      for (const notSent of [null, "stop", "pause", H]) {
        const cards = PUI.cardsHtml({ exec, safety: eff, card: null, position: null, dom: {}, symbol: "SNDK", limits: null, avail: {}, confirm: "stop", rc, notSent });
        assert.ok(!raw(cards), cards.slice(0, 300));
      }
    }
  }
  assert.ok(R.safetyHtml(hostile(), { detailsOpen: true }).includes("&lt;img src=x onerror="));      // shown, as text
  // MORE › Automation: automationTrading returns text, and its one call site renders it through kv(), which escapes every value
  const PJS = read("product_ui.js");
  assert.equal((PJS.match(/automationTrading\(/g) || []).length, 2);                                  // the definition and the one call site
  assert.ok(PJS.includes('["Trading", automationTrading(sv)]'));
  assert.ok(PJS.includes('  const kv = (rows) => `<div class="pui-kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><b>${esc(v == null || v === "" ? "—" : v)}</b>`).join("")}</div>`;\n'));
});
