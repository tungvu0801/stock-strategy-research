// Risk controls panel (RISK_CONTROLS_PANEL_DESIGN.md 3.2, section 6 "Node"): frontend/risk_controls.js run under node:vm with a controllable
// fetch, fake timers and a fake clock, for every request order of B1 to B11, B11a, B11b and B17 to B19. Each fetch is held until the test
// answers it, fails it or lets its 20 s timeout abort it, so the order of answers is exact. Nothing here starts a server or opens a socket.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const R = require("../../frontend/trader_render.js");

const RISK_JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "risk_controls.js"), "utf8");
const T0 = Date.UTC(2026, 10, 9, 15, 0, 0);

function view(over) {
  return Object.assign({ version: "safety_v1", state: "TRADING_ALLOWED", state_label: "Trading allowed", icon: "🟢", allowed_new_entry: true, halted: false, reason_codes: [], reason_text: "",
                         daily_loss: { status: "OK" }, kill_switch: { active: false }, no_new_entries: { active: false }, evaluated_at: "2026-11-09T15:00:00+00:00", persisted: true,
                         disclaimer: "", events: [] }, over || {});
}
const ALLOWED = view();
const PAUSED = view({ state: "NO_NEW_ENTRIES", state_label: "New automatic entries paused", allowed_new_entry: false, no_new_entries: { active: true }, reason_text: "New automatic entries paused by you" });
const KILLED = view({ state: "HALTED", state_label: "New automatic entries stopped", allowed_new_entry: false, halted: true, kill_switch: { active: true, activated_by: "user" }, reason_text: "Emergency stop active" });
const UNKNOWN_DEFAULT_KILL = view({ state: "UNKNOWN", state_label: "Risk status unknown", allowed_new_entry: false, halted: true, persisted: false, reason_text: "Safety status unavailable" });
const UNKNOWN_KILL = view({ state: "UNKNOWN", state_label: "Risk status unknown", allowed_new_entry: false, halted: true, persisted: false, kill_switch: { active: true }, reason_text: "Safety status unavailable" });

// ---- the sandbox: fake timers, a fake clock, a held fetch, a minimal DOM ---------------------------------------------------------------------
function world() {
  const w = { now: T0, timers: [], seq: 0, calls: [], html: "" };
  const setTimeout_ = (fn, ms) => { const t = { id: ++w.seq, at: w.now + (ms || 0), fn, every: 0 }; w.timers.push(t); return t.id; };
  const setInterval_ = (fn, ms) => { const t = { id: ++w.seq, at: w.now + ms, fn, every: ms }; w.timers.push(t); return t.id; };
  const clear = (id) => { w.timers = w.timers.filter((t) => t.id !== id); };
  class FakeDate extends Date { constructor(...a) { super(...(a.length ? a : [w.now])); } static now() { return w.now; } }
  const el = { dataset: {}, set innerHTML(h) { w.html = h; }, get innerHTML() { return w.html; }, listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }, querySelector() { return null; } };
  const doc = { hidden: false, listeners: {}, getElementById: (id) => (id === "tv-safety" ? el : null), addEventListener(t, f) { this.listeners[t] = f; } };
  function fetch_(url, init) {
    const call = { url, method: (init && init.method) || "GET", headers: (init && init.headers) || {}, settled: false };
    call.promise = new Promise((resolve, reject) => {
      call.respond = (status, body) => { if (call.settled) return; call.settled = true; resolve({ ok: status >= 200 && status < 300, status, json: async () => (body === undefined ? (() => { throw new Error("bad json"); })() : body) }); };
      call.fail = (msg) => { if (call.settled) return; call.settled = true; reject(new TypeError(msg || "Failed to fetch")); };
      if (init && init.signal) init.signal.addEventListener("abort", () => { if (call.settled) return; call.settled = true; const e = new Error("aborted"); e.name = "AbortError"; reject(e); });
    });
    w.calls.push(call);
    return call.promise;
  }
  const sandbox = { window: { TraderRender: R }, document: doc, fetch: fetch_, setTimeout: setTimeout_, clearTimeout: clear, setInterval: setInterval_, clearInterval: clear, Date: FakeDate,
                    AbortController, Promise, console };
  sandbox.window.document = doc;
  vm.createContext(sandbox);
  vm.runInContext(RISK_JS, sandbox, { filename: "risk_controls.js" });
  w.rc = sandbox.window.RiskControls;
  w.win = sandbox.window;                                                         // J-04: a test may install a fake StopPriority here
  w.el = el;
  w.doc = doc;
  w.flush = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
  w.advance = async (ms) => {                                                     // run every timer due within ms, in time order, settling promises in between
    const end = w.now + ms;
    for (;;) {
      w.timers.sort((a, b) => a.at - b.at);
      const t = w.timers[0];
      if (!t || t.at > end) break;
      w.now = t.at;
      if (t.every) t.at += t.every; else w.timers.shift();
      t.fn();
      await w.flush();
    }
    w.now = end;
    await w.flush();
  };
  w.open = (pred) => w.calls.filter((c) => !c.settled && (!pred || pred(c)));
  w.gets = () => w.open((c) => c.method === "GET" && c.url === "/api/execution-safety");
  w.posts = (route) => w.calls.filter((c) => c.method === "POST" && (!route || c.url.endsWith(`/${route}`)));   // exact route: "emergency-stop" is not "clear-emergency-stop"
  w.click = async (kind) => { w.el.listeners.click({ target: { closest: () => ({ dataset: { safety: kind }, disabled: false }) } }); await w.flush(); };
  w.answerReads = async (body) => { for (const c of w.gets()) c.respond(200, body); await w.flush(); };
  // J-04 (design 2.1 Part C): the Stop's decision reads are latch reads, GET /api/execution-safety/latch, the first 1 s after the press and then
  // every 2 s; answered here with kill_active true / false, or failed. A decision needs a counted answer or the 60 s deadline.
  w.latches = () => w.open((c) => c.method === "GET" && c.url === "/api/execution-safety/latch");
  w.answerLatch = async (active) => { for (const c of w.latches()) c.respond(200, { version: "safety_v1", status: "OK", kill_active: active, kill_activated_at: active ? "2026-11-09T15:00:01+00:00" : null }); await w.flush(); };
  w.failLatches = async () => { for (const c of w.latches()) c.fail(); await w.flush(); };
  w.untilDecided = async (answer) => { for (let i = 0; i < 40 && out(w, "stop") === "PENDING"; i++) { await w.advance(2000); await answer(); } };
  return w;
}
async function mounted(first) {
  const w = world();
  w.rc.mount("tv-safety");
  await w.flush();
  if (first !== undefined) { w.gets()[0].respond(200, first); await w.flush(); }
  return w;
}
const shown = (w) => w.rc.state.data;
const out = (w, k) => (w.rc.status().outcomes[k] || {}).result || null;

test("the panel reports only what the server shows: every POST and latch read carries the intent header, exactly the four action routes and the latch read, one POST site", async () => {
  const w = await mounted(ALLOWED);
  await w.click("pause"); w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush(); await w.answerReads(PAUSED);
  await w.click("stop"); w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush(); await w.advance(1000);
  for (const c of w.latches()) assert.equal(c.headers["X-Stock-Agent-Intent"], "dashboard", c.url);  // J-04: the latch read never depends on app.js either
  await w.answerLatch(true);
  for (const c of w.posts()) assert.equal(c.headers["X-Stock-Agent-Intent"], "dashboard", c.url);   // design 3.2 item 9: Stop never depends on app.js
  const routes = [...RISK_JS.matchAll(/"(\/api\/execution-safety\/[a-z-]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(routes, ["/api/execution-safety/clear-emergency-stop", "/api/execution-safety/emergency-stop", "/api/execution-safety/latch", "/api/execution-safety/pause-new-entries", "/api/execution-safety/resume-new-entries"]);
  assert.equal((RISK_JS.match(/method: "POST"/g) || []).length, 1);
  assert.deepEqual(Object.keys(w.rc.status()).sort(), ["announce", "error", "lastGoodAt", "latchKill", "latchLast", "loading", "now", "openOrders", "outcomes", "pending", "preempting", "stopActive", "stopPressedAt", "uncertain"]);
});

test("B1 a poll issued before Pause and held until after it never overwrites the pause", async () => {
  const w = await mounted(ALLOWED);
  await w.advance(30000);                                                         // the 30 s poll is issued and held
  const poll = w.gets()[0];
  await w.click("pause");
  w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush();
  const settle = w.gets().filter((c) => c !== poll)[0];
  settle.respond(200, PAUSED); await w.flush();
  poll.respond(200, ALLOWED); await w.flush();                                    // the old poll lands last
  assert.equal(shown(w).state, "NO_NEW_ENTRIES");
  assert.equal(out(w, "pause"), "CONFIRMED");
});

test("B2 a poll issued during the Pause POST and released after it never overwrites the pause", async () => {
  const w = await mounted(ALLOWED);
  await w.click("pause");
  w.rc.refresh(); await w.flush();                                                // a read issued while the POST is in flight (no clock move: the POST's 20 s timeout stays)
  const poll = w.gets()[0];
  w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush();
  w.gets().filter((c) => c !== poll)[0].respond(200, PAUSED); await w.flush();
  poll.respond(200, ALLOWED); await w.flush();
  assert.equal(shown(w).state, "NO_NEW_ENTRIES");
});

test("B2b a poll issued during the Pause POST and answered between the Pause response and its settling read is discarded at once", async () => {
  const w = await mounted(ALLOWED);
  await w.click("pause");
  w.rc.refresh(); await w.flush();
  const poll = w.gets()[0];
  w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush();
  assert.equal(shown(w).state, "NO_NEW_ENTRIES");                                  // the response of the latest action applies
  poll.respond(200, ALLOWED); await w.flush();                                     // the stale poll lands before the settling read
  assert.equal(shown(w).state, "NO_NEW_ENTRIES", "a write that finished after the poll was issued wins (the generation moved at the end)");
});

test("B3 two overlapping polls: the older answered last never replaces the newer", async () => {
  const w = await mounted(ALLOWED);
  w.rc.refresh(); w.rc.refresh(); await w.flush();
  const [older, newer] = w.gets();
  newer.respond(200, PAUSED); await w.flush();
  older.respond(200, ALLOWED); await w.flush();
  assert.equal(shown(w).state, "NO_NEW_ENTRIES");
});

test("B4 a stale stop: after 65 s with no good read the state is uncertain; Stop and Pause enabled, Clear disabled, nothing claimed", async () => {
  const w = await mounted(KILLED);
  for (let i = 0; i < 3; i++) { await w.advance(30000); for (const c of w.gets()) c.fail(); await w.flush(); }
  const s = w.rc.status();
  assert.equal(s.uncertain, true);
  const html = w.html;
  assert.match(html, /Status uncertain · last known New automatic entries stopped/);
  assert.match(html, /data-safety="stop">Emergency Stop/);                       // never disabled
  assert.match(html, /data-safety="pause">Pause New Trades/);
  assert.match(html, /data-safety="clear" disabled/);
  assert.doesNotMatch(html, /Trading allowed|New automatic entries stopped<\/span>/);
});

test("B5 a hung Pause, then Stop: the Stop POST is sent at once, and the late Pause answer does not overwrite the stop", async () => {
  const w = await mounted(ALLOWED);
  await w.click("pause");
  const pause = w.posts("pause-new-entries")[0];
  await w.click("stop");
  const stop = w.posts("emergency-stop")[0];
  assert.ok(stop && !stop.settled, "Emergency Stop is sent while the Pause is still in flight");
  stop.respond(200, KILLED); await w.flush();
  pause.respond(200, PAUSED); await w.flush();                                    // the Pause finishes after the Stop
  // 3.2 item 2: an action's answer applies only if it is the latest action started; checked before any read can repaint the state
  assert.deepEqual([shown(w).state, shown(w).kill_switch.active], ["HALTED", true], "the late Pause answer never replaces the Stop's state");
  await w.answerReads(KILLED);                                                    // the Pause's settling read
  await w.advance(1000); await w.answerLatch(true);                               // J-04: the Stop's decision is its latch read, issued after every earlier action finished
  assert.equal(shown(w).kill_switch.active, true);
  assert.equal(out(w, "stop"), "CONFIRMED");
});

test("a server UNKNOWN with the default inactive kill switch after a confirmed stop: no 'no longer active', the stop's outcome stays (3.1, B-3)", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000); await w.answerLatch(true);
  assert.equal(out(w, "stop"), "CONFIRMED");
  await w.advance(30000); await w.answerReads(UNKNOWN_DEFAULT_KILL);              // the evaluation failed and could not read the stored kill switch
  assert.equal(w.rc.status().announce, null);
  assert.equal(out(w, "stop"), "CONFIRMED");
  assert.ok(w.rc.status().latchKill && w.rc.status().latchKill.active === true, "J-04: the latch overlay survives a server UNKNOWN with the default kill switch");
  await w.advance(30000); await w.answerReads(ALLOWED);                           // a real read that shows it cleared (not by this page) is still announced
  assert.equal(w.rc.status().announce, "Emergency Stop is no longer active.");
});

test("a stop confirmed only by its POST (the read failed), then cleared elsewhere: 'Emergency Stop is no longer active' is shown (3.2 item 4)", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000); await w.failLatches();                                   // J-04: the latch reads fail (the server's fast read never answers)
  assert.equal(out(w, "stop"), "PENDING");                                         // a sent request is not proof: the flow keeps reading
  await w.untilDecided(() => w.failLatches());                                     // until the 60 s deadline
  assert.deepEqual([out(w, "stop"), w.rc.status().outcomes.stop.note], ["CONFIRMED", "posted-unread"]);
  assert.ok(w.now - T0 >= 60000 && w.now - T0 < 66000, `decided at +${w.now - T0} ms`);
  await w.advance(5000); await w.answerReads(ALLOWED);                            // the +5 s retry read shows the stop gone
  assert.equal(w.rc.status().announce, "Emergency Stop is no longer active.");
});

test("'loading' is reported only while mounted: a page where Risk Controls never mounted is uncertain, never 'checking' forever", async () => {
  const w = world();
  assert.equal(w.rc.status().loading, false);
  assert.equal(w.rc.status().uncertain, true);
  w.rc.mount("tv-safety"); await w.flush();
  assert.equal(w.rc.status().loading, true);
  w.gets()[0].respond(200, ALLOWED); await w.flush();
  assert.equal(w.rc.status().loading, false);
});

test("B6 every read fails from page load: Emergency Stop is still there and its click reaches the server", async () => {
  const w = await mounted();
  w.gets()[0].fail(); await w.flush();
  assert.match(w.html, /data-safety="stop">Emergency Stop/);
  assert.match(w.html, /Risk status unknown/);
  await w.click("stop");
  assert.equal(w.posts("emergency-stop").length, 1);
});

test("B7 Stop answers 200 UNKNOWN with the kill inactive and a later read shows it not active: re-sent once, then NOT confirmed, never success", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, UNKNOWN_DEFAULT_KILL); await w.flush();
  await w.advance(1000); await w.answerLatch(false);                              // J-04: the latch read says not saved
  assert.equal(w.posts("emergency-stop").length, 2, "re-sent once");
  w.posts("emergency-stop")[1].respond(200, UNKNOWN_DEFAULT_KILL); await w.flush();
  await w.advance(2000); await w.answerLatch(false);
  assert.equal(w.posts("emergency-stop").length, 2);
  assert.equal(out(w, "stop"), "NOT_CONFIRMED");
  assert.match(w.html, /Emergency Stop NOT confirmed\. Press it again\. If it stays NOT confirmed, run the fallback command/);   // J-04: the panel's line names the fallback
});

test("B8 Stop answers 500 and a later read shows it active: the server's state is shown, with the error disclosed", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(500, { message: "Internal Server Error" }); await w.flush();
  await w.advance(1000); await w.answerLatch(true);                               // J-04: the latch read shows it saved
  assert.equal(out(w, "stop"), "CONFIRMED");
  assert.match(w.html, /The request reported an error, but the server shows Emergency Stop active\./);
});

test("B9 the Stop POST and the reads all fail: NOT confirmed, and reads retried at +5, +15 and +30 s", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].fail(); await w.flush();
  await w.advance(1000); await w.failLatches();                                   // J-04: the latch reads fail too
  assert.equal(out(w, "stop"), "PENDING");
  assert.match(w.html, /Sending Emergency Stop|Checking whether the Emergency Stop was saved/);
  await w.untilDecided(() => w.failLatches());                                     // nothing answered for 60 s: NOT confirmed, never success
  assert.equal(out(w, "stop"), "NOT_CONFIRMED");
  assert.match(w.html, /Emergency Stop NOT confirmed/);
  assert.equal(w.posts("emergency-stop").length, 1, "no counted 'not saved' answer: nothing to re-send on");
  const full = () => w.calls.filter((c) => c.method === "GET" && c.url === "/api/execution-safety").length;
  const before = full();
  await w.advance(5000); for (const c of w.gets()) c.fail(); await w.flush();
  await w.advance(10000); for (const c of w.gets()) c.fail(); await w.flush();
  await w.advance(15000);
  assert.ok(full() - before >= 3, "three retry reads");
  await w.answerReads(KILLED);                                                    // the stop had in fact been saved: a later read settles it
  assert.equal(out(w, "stop"), "CONFIRMED");
});

test("B10 no successful read for 65 s turns the state uncertain on its own, even when no poll ran (a throttled tab)", async () => {
  const w = await mounted(ALLOWED);
  assert.equal(w.rc.status().uncertain, false);
  w.now += 64000;                                                                 // the clock moves, no timer runs
  assert.equal(w.rc.status().uncertain, false);
  w.now += 2000;
  assert.equal(w.rc.status().uncertain, true);
  const w2 = await mounted(ALLOWED);
  await w2.advance(30000); w2.gets()[0].fail(); await w2.flush();                 // a failed read is uncertain at once
  assert.equal(w2.rc.status().uncertain, true);
});

test("B11 a Clear in flight, then Stop: the end state is stopped or a visible NOT confirmed, never a silent allowed", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");                                 // two clicks
  const clear = w.posts("clear-emergency-stop")[0];
  await w.click("stop");
  const stop = w.posts("emergency-stop")[0];
  stop.respond(200, KILLED); await w.flush();                                     // the server handles Stop first (already active: no change)
  clear.respond(200, ALLOWED); await w.flush();                                   // then commits the Clear: the stop is gone
  await w.answerReads(ALLOWED);                                                   // the Clear's settling read
  assert.equal(w.rc.status().announce, null, "a stop that goes away while a Stop is pending is never announced (it re-sends)");   // J-04
  await w.advance(1000); await w.answerLatch(false);                              // J-04: the Stop's latch read, issued after the Clear finished, says not saved
  assert.equal(w.posts("emergency-stop").length, 2, "Stop re-sent: the operator's latest intent is Stop");
  w.posts("emergency-stop")[1].respond(200, KILLED); await w.flush();
  await w.advance(2000); await w.answerLatch(true);
  assert.equal(shown(w).kill_switch.active, true);
  assert.equal(out(w, "stop"), "CONFIRMED");
});

test("B11c Stop's decision waits for the earlier Clear: no decision read is issued while the Clear is in flight", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000);                                                          // J-04: the first latch read would be due now
  assert.equal(w.gets().length + w.latches().length, 0, "no read before every earlier action finished");
  assert.equal(out(w, "stop"), "PENDING");
  w.posts("clear-emergency-stop")[0].respond(200, ALLOWED); await w.flush();
  assert.ok(w.gets().length >= 1 && w.latches().length === 1, "the Clear's settling read and the Stop's latch read go once the Clear finished");
});

test("a Clear answered 200 UNKNOWN with the default inactive kill switch is not a confirmed Clear", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  w.posts("clear-emergency-stop")[0].respond(200, UNKNOWN_DEFAULT_KILL); await w.flush();
  await w.answerReads(UNKNOWN_DEFAULT_KILL);
  assert.equal(out(w, "clear"), "NOT_CONFIRMED");
});

test("a read answered after 15 s (the cash-flow cycle wait) is still applied: the timeout is 20 s", async () => {
  const w = await mounted(ALLOWED);
  await w.advance(30000);
  await w.advance(15000);
  w.gets()[0].respond(200, PAUSED); await w.flush();
  assert.equal(shown(w).state, "NO_NEW_ENTRIES");
  assert.equal(w.rc.status().uncertain, false);
});

test("B11a a Clear aborted at 20 s, Stop pressed while the kill still reads on, the Clear commits later: Stop stays pending and re-sends", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  await w.advance(20000);                                                         // the client gives up on the Clear; its server thread may still commit
  await w.answerReads(KILLED);                                                    // the settle read still shows the stop
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000); await w.answerLatch(true);                               // J-04: saved, but the aborted Clear is unfinished for 60 s
  assert.equal(out(w, "stop"), "PENDING");
  await w.advance(2000);
  await w.answerLatch(false);                                                     // the aborted Clear committed late
  assert.ok(w.posts("emergency-stop").length >= 2, "re-sent when the kill went inactive");
  w.posts("emergency-stop")[w.posts("emergency-stop").length - 1].respond(200, KILLED); await w.flush();
  for (let i = 0; i < 40 && out(w, "stop") === "PENDING"; i++) { await w.advance(2000); await w.answerLatch(true); }
  assert.equal(out(w, "stop"), "CONFIRMED");
  assert.equal(shown(w).kill_switch.active, true);
});

test("B11b a stop confirmed earlier, then cleared from another tab: 'Emergency Stop is no longer active' is shown", async () => {
  const w = await mounted(KILLED);
  await w.advance(30000); await w.answerReads(ALLOWED);
  assert.equal(w.rc.status().announce, "Emergency Stop is no longer active.");
  assert.match(w.html, /Emergency Stop is no longer active\./);
});

test("B11b a stop this page cleared is not announced", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  w.posts("clear-emergency-stop")[0].respond(200, ALLOWED); await w.flush();
  await w.answerReads(ALLOWED);
  assert.equal(w.rc.status().announce, null);
  assert.equal(out(w, "clear"), "CONFIRMED");
});

test("B17 Stop answers 200 UNKNOWN with kill_switch.active true: confirmed, no re-send", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, UNKNOWN_KILL); await w.flush();
  await w.advance(1000); await w.answerLatch(true);
  assert.equal(w.posts("emergency-stop").length, 1);
  assert.equal(out(w, "stop"), "CONFIRMED");
});

test("B18 a later confirmed Pause after a NOT confirmed Stop never hides Stop's outcome", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(500, {}); await w.flush();
  await w.advance(1000); await w.answerLatch(false);                              // J-04: not saved -> re-sent once
  w.posts("emergency-stop")[1].respond(500, {}); await w.flush();
  await w.advance(2000); await w.answerLatch(false);
  assert.equal(out(w, "stop"), "NOT_CONFIRMED");
  await w.click("pause");
  w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush();
  await w.answerReads(PAUSED);
  assert.equal(out(w, "pause"), "CONFIRMED");
  assert.equal(out(w, "stop"), "NOT_CONFIRMED");
  assert.match(w.html, /Emergency Stop NOT confirmed/);
});

// ---- J-04 (design 2.1 Part C, acceptance A2, A10): the latch overlay and a newer action ------------------------------------------------------
test("J-04 A2 the latch overlay drives the display: the POST failed, the latch read true: the stop is shown active, the open-order line is read, and a later full read showing it cleared removes the overlay and announces", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].fail(); await w.flush();                           // no answer from the POST
  await w.advance(1000); await w.answerLatch(true);
  assert.deepEqual([out(w, "stop"), w.rc.status().outcomes.stop.note], ["CONFIRMED", "error-but-active"]);
  assert.ok(w.rc.status().latchKill && w.rc.status().latchKill.active === true && w.rc.status().stopActive === false);
  assert.equal(shown(w).state, "TRADING_ALLOWED", "state.data is still the last server view");
  assert.match(w.html, /New automatic entries stopped/);                          // rendered from the overlay, not from state.data
  assert.equal(w.open((c) => c.url === "/api/account-refresh").length, 1, "the open-order line is read once the latch shows the stop");
  await w.advance(30000); await w.answerReads(ALLOWED);                            // cleared elsewhere: a later full read replaces the overlay
  assert.equal(w.rc.status().latchKill, null);
  assert.equal(w.rc.status().announce, "Emergency Stop is no longer active.");
  assert.doesNotMatch(w.html, /New automatic entries stopped/);
});

test("J-04 A10 a newer Clear ends a pending Stop at once: NOT confirmed when no counted read showed it saved (the POST's 200 body is not proof), with the retry reads; a latch answer arriving after the Clear started is not counted (D1)", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000);
  assert.equal(w.latches().length, 1, "the latch read is in flight");
  await w.click("clear"); await w.click("clear");                                 // allowed: the POST settled (design M-3b)
  assert.equal(w.posts("clear-emergency-stop").length, 1);
  await w.answerLatch(true);                                                      // after the Clear started: not counted
  assert.deepEqual([out(w, "stop"), w.rc.status().stopActive, w.rc.status().latchKill], ["NOT_CONFIRMED", false, null]);
  assert.equal(w.rc.status().outcomes.stop.note, undefined, "never 'posted-unread' on a newer action");
  w.posts("clear-emergency-stop")[0].respond(200, ALLOWED); await w.flush(); await w.answerReads(ALLOWED);
  assert.equal(out(w, "clear"), "CONFIRMED");
  assert.equal(w.rc.status().announce, null, "this page cleared it: nothing announced");
  assert.equal(out(w, "stop"), null, "this page's confirmed Clear settles the Stop it could not confirm: no stale 'NOT confirmed' line");
  assert.doesNotMatch(w.html, /NOT confirmed/);
  const before = w.calls.filter((c) => c.method === "GET" && c.url === "/api/execution-safety").length;
  await w.advance(5000);
  assert.ok(w.calls.filter((c) => c.method === "GET" && c.url === "/api/execution-safety").length > before, "the +5 s retry read");
});

test("J-04 (review MED-3) a newer Pause while the aborted-Clear window is open never finalises CONFIRMED, even after a counted 'saved'", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  await w.advance(20000); await w.answerReads(KILLED);                             // the Clear aborted: its window is open until +80 s
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000); await w.answerLatch(true);
  assert.equal(out(w, "stop"), "PENDING");
  await w.click("pause");                                                         // a newer action ends the reconciliation at the flow's next step
  w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush();
  await w.advance(2000);
  assert.equal(out(w, "stop"), "NOT_CONFIRMED", "the saved read may still be undone by the aborted Clear: no CONFIRMED");
  assert.equal(w.rc.status().stopActive, false);
});

test("J-04 (review HIGH-1) a Clear in flight at the press that aborts after it opens the window: a 'saved' read does not decide until it closes; a late commit re-sends", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");                                 // the Clear hangs
  await w.advance(5000);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000);
  assert.equal(w.latches().length, 0, "no latch read before the earlier Clear settled");
  await w.advance(14000);                                                         // the Clear aborts at its 20 s: the window opens now, after the press
  await w.answerReads(KILLED);                                                    // the Clear's settling read
  assert.equal(w.latches().length, 1);
  await w.answerLatch(true);
  assert.equal(out(w, "stop"), "PENDING", "saved, but the aborted Clear may still commit");
  await w.advance(2000); await w.answerLatch(false);                              // it committed late: re-sent
  assert.equal(w.posts("emergency-stop").length, 2);
  assert.equal(w.rc.status().latchKill, null, "the overlay follows the latest counted read");
  w.posts("emergency-stop")[1].respond(200, KILLED); await w.flush();
  for (let i = 0; i < 40 && out(w, "stop") === "PENDING"; i++) { await w.advance(2000); await w.answerLatch(true); }
  assert.equal(out(w, "stop"), "CONFIRMED");
  assert.ok(w.now - T0 >= 80000, `decided only once the window closed (at +${w.now - T0} ms)`);
});

test("J-04 (review MED-1) a 'saved' read inside the aborted-Clear window, then only failed reads: CONFIRMED from the latest counted read once the window closes", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  await w.advance(20000); await w.answerReads(KILLED);                             // window until +80 s
  await w.click("stop");
  w.posts("emergency-stop")[0].fail(); await w.flush();
  await w.advance(1000); await w.answerLatch(true);
  assert.equal(out(w, "stop"), "PENDING");
  await w.untilDecided(() => w.failLatches());
  assert.deepEqual([out(w, "stop"), w.rc.status().outcomes.stop.note], ["CONFIRMED", "error-but-active"]);
  assert.ok(w.rc.status().latchKill && w.rc.status().latchKill.active === true);
  assert.ok(w.now - T0 >= 80000 && w.now - T0 < 90000, `at +${w.now - T0} ms`);
});

test("J-04 (review MED-2) a counted 'not saved' clears the latch overlay; the second one is final only once the re-send settled and the window closed", async () => {
  const w = await mounted(KILLED);
  await w.click("clear"); await w.click("clear");
  await w.advance(20000); await w.answerReads(KILLED);                             // window until +80 s
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000); await w.answerLatch(true);
  assert.ok(w.rc.status().latchKill);
  await w.advance(2000); await w.answerLatch(false);                               // the Clear committed late: re-send
  assert.equal(w.rc.status().latchKill, null);
  assert.equal(w.posts("emergency-stop").length, 2);
  await w.advance(2000);
  assert.equal(w.latches().length, 1, "the reads go on while the re-send is in flight (review LOW-3)");
  await w.answerLatch(false);
  assert.equal(out(w, "stop"), "PENDING", "not final: the re-send has not settled");
  w.posts("emergency-stop")[1].fail(); await w.flush();
  for (let i = 0; i < 40 && out(w, "stop") === "PENDING"; i++) { await w.advance(2000); await w.answerLatch(false); }
  assert.equal(out(w, "stop"), "NOT_CONFIRMED");
  assert.equal(w.rc.status().latchKill, null);
  assert.ok(w.now - T0 >= 80000, `only once the window closed (at +${w.now - T0} ms)`);
});

test("J-04 (review MED-4) on a stale page the Stop's own late 200 body never demotes a latch-confirmed stop to 'uncertain'; the overlay itself ages after 65 s", async () => {
  const w = await mounted(ALLOWED);
  for (let i = 0; i < 3; i++) { await w.advance(30000); for (const c of w.gets()) c.fail(); await w.flush(); }   // 90 s without a good read
  assert.equal(w.rc.status().uncertain, true);
  await w.click("stop");
  await w.advance(1000); await w.answerLatch(true);                               // the POST still pending
  assert.deepEqual([out(w, "stop"), w.rc.status().uncertain], ["CONFIRMED", false]);
  assert.match(w.html, /New automatic entries stopped/); assert.doesNotMatch(w.html, /Status uncertain/);
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();            // the slow answer lands after the decision
  assert.ok(w.rc.status().latchKill && w.rc.status().latchKill.active === true, "the overlay stays: the body agrees");
  assert.equal(w.rc.status().uncertain, false);
  assert.doesNotMatch(w.html, /Status uncertain/);
  await w.advance(66000); for (const c of w.gets()) c.fail(); await w.flush();   // no good read for 65 s after the latch read
  assert.equal(w.rc.status().uncertain, true, "the overlay is a read like any other: it ages");
});

test("J-04 the latch read is issued with the intent header, 1 s after the press and then every 2 s, one at a time, and it times out at 5 s without deciding anything", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  assert.equal(w.latches().length, 0);
  await w.advance(999); assert.equal(w.latches().length, 0);
  await w.advance(1); assert.equal(w.latches().length, 1);
  assert.equal(w.latches()[0].headers["X-Stock-Agent-Intent"], "dashboard");
  await w.advance(4000); assert.equal(w.latches().length, 1, "still the one read: the next waits for its answer");
  await w.advance(1001);                                                          // the 5 s timeout aborts it
  assert.equal(w.latches().length, 0);
  assert.equal(out(w, "stop"), "PENDING");
  await w.advance(2000); assert.equal(w.latches().length, 1, "the next read 2 s later");
  w.latches()[0].respond(503, { version: "safety_v1", status: "UNREADABLE", kill_active: null }); await w.flush();   // unreadable: no information, no re-send
  assert.deepEqual([out(w, "stop"), w.posts("emergency-stop").length, w.rc.status().latchLast], ["PENDING", 1, null]);
  await w.advance(2000);                                                          // +10 s: the panel's own pending line says it is checking the saved state
  assert.match(w.html, /Checking whether the Emergency Stop was saved/);
  await w.answerLatch(true);
  assert.equal(out(w, "stop"), "CONFIRMED");
});

test("J-04 a counted 'not saved' before the POST settled never re-sends; after it settled, once", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  await w.advance(1000); await w.answerLatch(false);                              // the POST is still in flight
  assert.equal(w.posts("emergency-stop").length, 1);
  assert.match(w.html, /Sending Emergency Stop/);
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(2000); await w.answerLatch(false);                              // settled: re-sent once
  assert.equal(w.posts("emergency-stop").length, 2);
  w.posts("emergency-stop")[1].respond(200, KILLED); await w.flush();
  await w.advance(2000); await w.answerLatch(true);
  assert.equal(out(w, "stop"), "CONFIRMED");
});

test("J-04 the priority hooks: preempt() before each Stop POST (the re-send too), release() once at the decision, never for Pause or Clear", async () => {
  const w = await mounted(ALLOWED);
  const calls = [];
  w.win.StopPriority = { preempt: () => calls.push("preempt"), release: () => calls.push("release"), preempting: () => calls.length > 0 && calls[calls.length - 1] === "preempt" };
  await w.click("pause"); w.posts("pause-new-entries")[0].respond(200, PAUSED); await w.flush(); await w.answerReads(PAUSED);
  assert.deepEqual(calls, []);
  await w.click("stop");
  assert.deepEqual(calls, ["preempt"]);
  assert.equal(w.rc.status().preempting, true);
  w.posts("emergency-stop")[0].respond(200, KILLED); await w.flush();
  await w.advance(1000); await w.answerLatch(false);
  assert.deepEqual(calls, ["preempt", "preempt"]);
  w.posts("emergency-stop")[1].respond(200, KILLED); await w.flush();
  await w.advance(2000); await w.answerLatch(true);
  assert.deepEqual(calls, ["preempt", "preempt", "release"]);
  assert.equal(w.rc.status().preempting, false, "released at the decision");
  await w.click("clear"); await w.click("clear"); w.posts("clear-emergency-stop")[0].respond(200, ALLOWED); await w.flush(); await w.answerReads(ALLOWED);
  assert.equal(calls.length, 3, "Clear never touches the priority hooks");
});

test("B19 a 200 with an unreadable body is a failed read, never a state", async () => {
  const w = await mounted();
  w.gets()[0].respond(200, undefined); await w.flush();                           // .json() throws: getJson reads {}
  assert.equal(shown(w), null);
  assert.equal(w.rc.status().uncertain, true);
  assert.match(w.rc.status().error, /Risk controls unavailable/);
  const w2 = await mounted();
  w2.gets()[0].respond(200, { status: "ok" }); await w2.flush();                  // a 200 with no known state
  assert.equal(shown(w2), null);
});

test("Pause, Resume and Clear cannot start while a Stop is pending; Stop itself is never blocked by a busy action", async () => {
  const w = await mounted(ALLOWED);
  await w.click("stop");
  await w.click("pause");
  assert.equal(w.posts("pause-new-entries").length, 0);
  assert.match(w.html, /data-safety="pause" disabled/);
  assert.match(w.html, /Sending Emergency Stop…/);
  await w.click("stop");                                                          // a second press: harmless, sent again
  assert.equal(w.posts("emergency-stop").length, 2);
});

test("a hung read is aborted after 20 s and the state turns uncertain; a visible page reads again", async () => {
  const w = await mounted(ALLOWED);
  await w.advance(30000);
  await w.advance(20000);                                                         // the poll times out
  assert.equal(w.rc.status().uncertain, true);
  assert.match(w.rc.status().error, /no answer in 20 s/);
  const before = w.calls.length;
  w.doc.listeners.visibilitychange(); await w.flush();                            // a throttled tab reads again when it becomes visible (3.2 item 8)
  assert.equal(w.calls.length, before + 1);
  assert.equal(w.calls[w.calls.length - 1].url, "/api/execution-safety");
});

test("B31's answer: the 30 s poll keeps at most one read in flight; reads after an action still go while a poll hangs", async () => {
  const w = await mounted(ALLOWED);
  await w.advance(30000);
  assert.equal(w.gets().length, 1);                                               // the poll hangs (not answered, not yet aborted)
  await w.advance(15000);
  await w.advance(15000);                                                         // the next tick finds it still in flight (aborted at 20 s, then none in flight)
  const polls = w.calls.filter((c) => c.method === "GET" && c.url === "/api/execution-safety").length;
  assert.ok(polls <= 3, `polls ${polls}`);
  const w2 = await mounted(ALLOWED);
  await w2.advance(30000);                                                        // a hung poll
  await w2.click("pause");
  assert.equal(w2.posts("pause-new-entries").length, 1);
  w2.posts("pause-new-entries")[0].respond(200, PAUSED); await w2.flush();
  assert.equal(w2.gets().length, 2, "the settling read goes although a poll is in flight");
  const src = RISK_JS.slice(RISK_JS.indexOf("function poll()"), RISK_JS.indexOf("function poll()") + 300);
  assert.ok(src.includes("if (state.pollInFlight || state.stopActive) return;") && /state\.timer = setInterval\(\(\) => \{ if \(document\.getElementById\(id\)\) poll\(\);/.test(RISK_JS));
});

test("the open-order line is read from the stored account snapshot only while the stop is active (C-4, 0 broker calls)", async () => {
  const w = await mounted(KILLED);
  const acct = w.calls.find((c) => c.url === "/api/account-refresh");
  assert.ok(acct, "read while the kill is active");
  acct.respond(200, { status: "FRESH", refreshed_at: "2026-11-09T14:32:05+00:00", snapshot: { open_orders: 2 } }); await w.flush();
  assert.match(w.html, /Open orders at Alpaca: 2, as of 14:32 UTC\. This app does not cancel orders\. Use Alpaca&#39;s website to cancel\./);   // escaped by esc()
  const w2 = await mounted(ALLOWED);
  assert.equal(w2.calls.filter((c) => c.url === "/api/account-refresh").length, 0);
});
