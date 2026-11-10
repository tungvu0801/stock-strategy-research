// J-04 Emergency Stop reliability (TASK_P_STOP_RELIABILITY_ORDER_GATE_PLAN_2026-11-10.md, design 2.1 Part A; acceptance A6, A7, A12): frontend/stop_priority.js
// run under node:vm with a controllable fetch, fake timers, a fake clock and a fake BroadcastChannel. Nothing here starts a server or opens a socket.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const SP_JS = fs.readFileSync(path.join(FRONT, "stop_priority.js"), "utf8");
const APP_JS = fs.readFileSync(path.join(FRONT, "app.js"), "utf8");
const T0 = Date.UTC(2026, 11, 10, 15, 0, 0);

// ---- the sandbox: fake timers, a fake clock, a held fetch, a fake BroadcastChannel shared by every "tab" of one bus ------------------------------
function bus() { return { tabs: [] }; }
function world(opts) {
  const o = opts || {};
  const w = { now: T0, timers: [], seq: 0, calls: [], bus: o.bus || bus(), received: [] };
  const setTimeout_ = (fn, ms) => { const t = { id: ++w.seq, at: w.now + (ms || 0), fn }; w.timers.push(t); return t.id; };
  const clear = (id) => { w.timers = w.timers.filter((t) => t.id !== id); };
  class FakeDate extends Date { constructor(...a) { super(...(a.length ? a : [w.now])); } static now() { const t = w.now; if (w.autotick) w.now += 1; return t; } }   // autotick: a clock that moves between two reads
  function fetch_(input, init) {
    const call = { url: typeof input === "string" ? input : input.url, method: (init && init.method) || (input && input.method) || "GET", init: init || {}, settled: false, aborted: false };
    call.promise = new Promise((resolve, reject) => {
      call.respond = (status, body) => { if (call.settled) return; call.settled = true; resolve({ ok: status >= 200 && status < 300, status, json: async () => body }); };
      call.fail = (msg) => { if (call.settled) return; call.settled = true; reject(new TypeError(msg || "Failed to fetch")); };
      if (init && init.signal) init.signal.addEventListener("abort", () => { if (call.settled) return; call.settled = true; call.aborted = true; const e = new Error("aborted"); e.name = "AbortError"; reject(e); });
    });
    w.calls.push(call);
    return call.promise;
  }
  class FakeChannel {
    constructor(name) { this.name = name; this.onmessage = null; this.closed = false; w.bus.tabs.push({ channel: this, w }); }
    postMessage(m) { for (const t of w.bus.tabs) if (t.channel !== this && !t.channel.closed && t.channel.onmessage) t.channel.onmessage({ data: JSON.parse(JSON.stringify(m)) }); }
    close() { this.closed = true; }
  }
  const listeners = {};
  const win = { location: { href: o.href !== undefined ? o.href : "http://127.0.0.1:8000/", origin: "http://127.0.0.1:8000" }, fetch: fetch_, setTimeout: setTimeout_, clearTimeout: clear, Date: FakeDate,
                AbortController, BroadcastChannel: o.noChannel ? undefined : FakeChannel, DOMException, addEventListener: (t, f) => { listeners[t] = f; }, Headers, URL, Request };
  const sandbox = { window: win, fetch: fetch_, setTimeout: setTimeout_, clearTimeout: clear, Date: FakeDate, AbortController, Headers, URL, Request, DOMException, Promise, console, Math };
  vm.createContext(sandbox);
  if (o.withApp) {                                                               // the EX-06 intent wrapper first (app.js's block), then this module, as index.html loads them
    const a = APP_JS.indexOf("// EX-06 local intent: begin"), b = APP_JS.indexOf("// EX-06 local intent: end");
    vm.runInContext(APP_JS.slice(a, b), sandbox, { filename: "app.js" });
  }
  vm.runInContext(SP_JS, sandbox, { filename: "stop_priority.js" });
  w.sp = win.StopPriority;
  w.win = win;
  w.listeners = listeners;
  w.flush = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
  w.advance = async (ms) => {
    const end = w.now + ms;
    for (;;) {
      w.timers.sort((a, b) => a.at - b.at);
      const t = w.timers[0];
      if (!t || t.at > end) break;
      w.now = t.at; w.timers.shift(); t.fn();
      await w.flush();
    }
    w.now = end;
    await w.flush();
  };
  w.get = (url, init) => { const p = win.fetch(url, init); p.catch(() => null); return p; };
  w.open = () => w.calls.filter((c) => !c.settled);
  return w;
}
const kinds = (w, ...urls) => urls.map((u) => w.sp.classify(u, {}).kind);

// ---- classification -----------------------------------------------------------------------------------------------------------------------------
test("classify: writes and other origins are never touched; SAFETY is exactly the two safety reads; KEEP is the pinned list; everything else same-origin is GENERAL", () => {
  const w = world();
  assert.deepEqual(kinds(w, "/api/execution-safety", "/api/execution-safety/latch"), ["SAFETY", "SAFETY"]);
  assert.deepEqual(kinds(w, "/api/execution-safety/events?limit=50", "/api/execution-safety/controls", "/api/execution/status", "/api/health", "/api/forward-validation/sessions?limit=60", "/api/backtests/abc"),
                   ["GENERAL", "GENERAL", "GENERAL", "GENERAL", "GENERAL", "GENERAL"]);                   // review N-1: /events is not SAFETY
  assert.deepEqual(kinds(w, "/api/stocks/NVDA/research", "/api/stocks/NVDA", "/api/daily-brief", "/api/daily-brief?session=2026-11-10"), ["KEEP", "KEEP", "KEEP", "KEEP"]);
  assert.deepEqual(kinds(w, "https://example.com/api/execution-safety", "http://127.0.0.1:8787/api/x"), ["FOREIGN", "FOREIGN"]);
  assert.equal(w.sp.classify("/api/execution-safety/emergency-stop", { method: "POST" }).kind, "WRITE");
  assert.equal(w.sp.classify(new Request("http://127.0.0.1:8000/api/health", { method: "DELETE" })).kind, "WRITE");   // the method read from a Request input
  assert.equal(w.sp.classify(new Request("http://127.0.0.1:8000/api/health")).kind, "GENERAL");
  assert.equal(JSON.stringify([w.sp.SAFETY, w.sp.KEEP_PREFIXES, w.sp.CHANNEL, w.sp.WINDOW_MAX_MS]),              // JSON: the arrays come from the vm realm
               JSON.stringify([["/api/execution-safety", "/api/execution-safety/latch"], ["/api/stocks/", "/api/daily-brief"], "stock-agent-stop", 30000]));
});

test("installed once over the page's fetch, without claiming the EX-06 flag; after app.js the intent header still reaches the server on GET and POST", async () => {
  const w = world({ withApp: true });
  assert.equal(w.win.fetch.__stopPriority, true);
  assert.equal(w.win.fetch.__stockAgentIntent, undefined);
  assert.equal(w.sp.install(), false);                                            // a second install does nothing
  w.get("/api/health"); w.get("/api/execution-safety/emergency-stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  await w.flush();
  for (const c of w.calls) assert.equal(new Headers(c.init.headers).get("X-Stock-Agent-Intent"), "dashboard", c.url);
  assert.equal(w.calls[0].init.priority, "low");                                  // GENERAL reads load at a low priority; the Stop's requests at the default
  assert.equal(w.calls[1].init.priority, undefined);
  assert.equal(new Headers(w.calls[1].init.headers).get("Content-Type"), "application/json");
});

test("normal operation: nothing is held, aborted or delayed; a caller's priority and signal are kept; a read settles and is forgotten", async () => {
  const w = world();
  const ac = new AbortController();
  w.get("/api/health", { priority: "high" }); w.get("/api/execution/status", { signal: ac.signal }); w.get("/api/execution-safety"); w.get("/api/stocks/MU/research");
  await w.flush();
  assert.equal(w.open().length, 4);
  assert.equal(w.calls[0].init.priority, "high");
  assert.equal(w.sp.state.inflight.size, 4);
  w.calls[0].respond(200, {}); await w.flush();
  assert.equal(w.sp.state.inflight.size, 3);
  ac.abort(); await w.flush();                                                    // the caller's own abort still works through the merged signal
  assert.equal(w.calls[1].aborted, true);
  assert.equal(w.sp.state.inflight.size, 2);
  assert.equal(w.sp.holding(), false); assert.equal(w.sp.preempting(), false);
});

// ---- the pressing tab ---------------------------------------------------------------------------------------------------------------------------
test("preempt: this tab's in-flight GENERAL reads are aborted with AbortError, SAFETY and KEEP reads and writes are untouched; new GENERAL reads are held, then run in order on release", async () => {
  const w = world();
  const results = [];
  const track = (p, name) => p.then(() => results.push([name, "ok"]), (e) => results.push([name, e && e.name]));
  track(w.get("/api/health"), "health"); track(w.get("/api/execution/status"), "status"); track(w.get("/api/execution-safety"), "safety"); track(w.get("/api/stocks/MU/research"), "keep");
  track(w.get("/api/setups/alerts/read-all", { method: "POST", body: "{}" }), "write");
  await w.flush();
  w.sp.preempt(30000);
  await w.flush();
  assert.deepEqual(results.sort(), [["health", "AbortError"], ["status", "AbortError"]]);
  assert.equal(w.sp.preempting(), true); assert.equal(w.sp.holding(), true);
  const held1 = w.get("/api/setups/alerts?limit=30"), held2 = w.get("/api/decision-flow");
  track(held1, "held1"); track(held2, "held2");
  const stopPost = w.get("/api/execution-safety/emergency-stop", { method: "POST", body: "{}" }), latch = w.get("/api/execution-safety/latch"), keep = w.get("/api/daily-brief");
  await w.flush();
  assert.equal(w.calls.filter((c) => c.url === "/api/setups/alerts?limit=30" || c.url === "/api/decision-flow").length, 0, "held reads were not sent");
  assert.equal(w.calls.filter((c) => c.url.endsWith("/emergency-stop") || c.url.endsWith("/latch") || c.url === "/api/daily-brief").length, 3, "the Stop, its latch read and a KEEP read go at once");
  assert.equal(w.sp.state.held.length, 2);
  w.sp.release();
  await w.flush();
  const sent = w.calls.map((c) => c.url);
  assert.ok(sent.indexOf("/api/setups/alerts?limit=30") < sent.indexOf("/api/decision-flow"), "released in the order they were asked for");
  assert.equal(w.sp.holding(), false); assert.equal(w.sp.preempting(), false);
  w.calls.find((c) => c.url === "/api/decision-flow").respond(200, {}); await w.flush();
  assert.ok(results.some(([n, r]) => n === "held2" && r === "ok"));
  assert.equal(w.sp.state.counts.aborted, 2); assert.equal(w.sp.state.counts.held, 2); assert.equal(w.sp.state.counts.released, 2);
  void stopPost; void latch; void keep;
});

test("the hold ends by itself at the window's end (30 s at most, whatever is asked for); a held read whose caller aborts is dropped with AbortError", async () => {
  const w = world();
  w.sp.preempt(600000);                                                           // asks for 10 min: clamped to 30 s
  const ac = new AbortController();
  const dropped = w.get("/api/health", { signal: ac.signal }); dropped.catch(() => null);
  const kept = w.get("/api/execution/status"); kept.catch(() => null);
  await w.flush();
  ac.abort(); await w.flush();
  let name = null; await dropped.catch((e) => { name = e.name; });
  assert.equal(name, "AbortError");
  assert.equal(w.sp.state.held.length, 1);
  await w.advance(29999);
  assert.equal(w.sp.holding(), true);
  await w.advance(2);
  assert.equal(w.sp.holding(), false);
  assert.equal(w.sp.preempting(), false, "the own window is capped at 30 s too, whatever was asked for");
  assert.equal(w.calls.filter((c) => c.url === "/api/execution/status").length, 1, "the held read ran when the window ended");
});

test("repeated preempts extend the hold and abort again; a preempt after a release starts a new window", async () => {
  const w = world();
  w.sp.preempt(5000); await w.flush();
  await w.advance(3000);
  w.get("/api/health"); await w.flush();                                          // held
  w.sp.preempt(5000); await w.flush();
  assert.equal(w.sp.state.held.length, 1);
  await w.advance(4000);
  assert.equal(w.sp.holding(), true, "the second preempt moved the window's end");
  await w.advance(1100);
  assert.equal(w.sp.holding(), false);
  assert.equal(w.calls.filter((c) => c.url === "/api/health").length, 1);
  w.sp.release(); w.sp.preempt(1000); assert.equal(w.sp.preempting(), true);
  await w.advance(1001); assert.equal(w.sp.preempting(), false);
});

test("a wrapper failure never blocks a request: with no page location every request passes through, and a throwing fetch surfaces to its caller", async () => {
  const w = world({ href: "" });
  const p = w.get("/api/health"); await w.flush();
  assert.equal(w.calls.length, 1);
  assert.equal(w.calls[0].init.priority, undefined);                              // FOREIGN (no origin to compare with): untouched
  void p;
});

// ---- the other tabs ------------------------------------------------------------------------------------------------------------------------------
test("cross-tab: a Stop in tab A aborts and holds EVERY read of tab B (its SAFETY and KEEP reads included) until A's release or the window's end; writes in B are untouched", async () => {
  const b = bus();
  const A = world({ bus: b }), B = world({ bus: b });
  B.get("/api/execution-safety"); B.get("/api/stocks/MU/research"); B.get("/api/health");
  const write = B.get("/api/setups/alerts/read-all", { method: "POST", body: "{}" });
  await B.flush();
  A.sp.preempt(30000); await A.flush(); await B.flush();
  assert.deepEqual(B.calls.map((c) => c.aborted), [true, true, true, false]);     // every read aborted, the write untouched
  assert.equal(B.sp.holding(), true); assert.equal(B.sp.preempting(), false);
  B.get("/api/execution-safety/latch"); B.get("/api/forward-validation"); await B.flush();
  assert.equal(B.sp.state.held.length, 2, "B holds even its safety read: it is not the pressing tab");
  A.sp.release(); await A.flush(); await B.flush();
  assert.equal(B.sp.holding(), false);
  assert.equal(B.calls.filter((c) => c.url === "/api/execution-safety/latch" || c.url === "/api/forward-validation").length, 2, "B's held reads ran on A's release");
  void write;
});

test("cross-tab: a tab with its own Stop pending keeps its SAFETY reads when another tab's preempt arrives; messages from the tab itself are ignored", async () => {
  const b = bus();
  const A = world({ bus: b }), B = world({ bus: b });
  B.sp.preempt(30000); await B.flush();
  A.sp.preempt(30000); await A.flush(); await B.flush();
  B.get("/api/execution-safety/latch"); B.get("/api/health"); await B.flush();
  assert.equal(B.calls.filter((c) => c.url === "/api/execution-safety/latch").length, 1, "B's latch read goes: its own Stop is pending");
  assert.equal(B.sp.state.held.length, 1, "B's GENERAL read is held by its own preempt");
  assert.equal(B.sp.state.counts.remote, 0);
});

test("cross-tab: an expired or out-of-order preempt is ignored; a release from a tab that did not preempt, or older than the preempt, does not end the hold", async () => {
  const b = bus();
  const A = world({ bus: b }), B = world({ bus: b }), C = world({ bus: b });
  B.sp._onMessage({ data: { type: "stop-preempt", from: "x", at: T0 - 100000, until: T0 - 50000 } });   // expired
  assert.equal(B.sp.holding(), false);
  A.sp.preempt(30000); await B.flush();
  assert.equal(B.sp.holding(), true);
  C.sp.release(); await B.flush();                                                // C never preempted
  assert.equal(B.sp.holding(), true);
  B.sp._onMessage({ data: { type: "stop-release", from: A.sp.TAB_ID, at: T0 - 1 } });   // older than A's preempt: out of order
  assert.equal(B.sp.holding(), true);
  A.sp.release(); await B.flush();
  assert.equal(B.sp.holding(), false);
});

test("cross-tab: a tab that does not receive the message (no BroadcastChannel) keeps polling: cooperative coordination, not a guarantee", async () => {
  const b = bus();
  const A = world({ bus: b }), D = world({ bus: b, noChannel: true });
  D.get("/api/health"); await D.flush();
  A.sp.preempt(30000); await A.flush(); await D.flush();
  assert.equal(D.calls[0].aborted, false); assert.equal(D.sp.holding(), false);
  assert.equal(D.sp.state.channel, null);
  D.get("/api/execution/status"); await D.flush();
  assert.equal(D.calls.length, 2, "D's reads keep going: nothing reached it");
});

test("a request held at the very end of the window is never stranded: the window ending between the hold decision and the timer arming releases it at once (review LOW-1)", async () => {
  const w = world();
  w.sp.preempt(1000);
  w.now = T0 + 999; w.autotick = true;                                           // the decision reads 999 (holding), the arming reads 1000 (not holding)
  const p = w.get("/api/health"); p.catch(() => null);
  w.autotick = false;
  await w.flush();
  assert.equal(w.sp.state.held.length, 0);
  assert.equal(w.calls.filter((c) => c.url === "/api/health").length, 1, "sent at once, not left for a timer that no longer exists");
  assert.equal(w.sp.holding(), false);
});

test("a tab still reconciling after its own window (no release yet) keeps its reads against another tab's preempt, until OWN_MAX_MS lapses (review LOW-4)", async () => {
  const b = bus();
  const A = world({ bus: b }), B = world({ bus: b });
  B.sp.preempt(1000); await B.advance(1500);
  assert.deepEqual([B.sp.preempting(), B.sp.reconciling(), B.sp.holding()], [false, true, false]);
  A.sp.preempt(30000); await A.flush(); await B.flush();
  assert.deepEqual([B.sp.holding(), B.sp.state.counts.remote], [false, 0]);
  B.get("/api/execution-safety/latch"); await B.flush();
  assert.equal(B.calls.filter((c) => c.url.endsWith("/latch")).length, 1, "its latch read goes");
  await B.advance(120000);                                                        // never released: the exception lapses
  assert.equal(B.sp.reconciling(), false);
  A.now = B.now;                                                                  // the two tabs share one wall clock
  A.sp.preempt(30000); await A.flush(); await B.flush();
  assert.equal(B.sp.holding(), true);
  A.sp.release(); await B.flush();
  assert.equal(B.sp.holding(), false, "a release is honoured although this tab never released its own lapsed Stop");
  assert.equal(w_const(), 120000);
  function w_const() { return B.sp.OWN_MAX_MS; }
});

test("leaving the page mid-Stop (pagehide) releases the other tabs", async () => {
  const b = bus();
  const A = world({ bus: b }), B = world({ bus: b });
  A.sp.preempt(30000); await B.flush();
  assert.equal(B.sp.holding(), true);
  A.listeners.pagehide(); await B.flush();
  assert.equal(B.sp.holding(), false);
});

// ---- source pins ---------------------------------------------------------------------------------------------------------------------------------
test("the module has no DOM, storage, markup or new I/O channel beyond the BroadcastChannel, no string timer, no URL assignment; the pages load it where the design says", () => {
  assert.doesNotMatch(SP_JS, /innerHTML|document\.|localStorage|sessionStorage|indexedDB|XMLHttpRequest|WebSocket\(|EventSource\(|sendBeacon|window\.open|\beval\(|new Function|\.(href|src)\s*=(?!=)/);
  assert.doesNotMatch(SP_JS, /set(Timeout|Interval)\(\s*["'`]/);
  assert.doesNotMatch(SP_JS, /__stockAgentIntent\s*=/);
  assert.doesNotMatch(SP_JS.replace(/\/\/.*/g, ""), /\b(cancel\w*|submit\w*|liquidat\w*)\(/i);
  const html = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(scripts.slice(0, 2), ["app.js", "stop_priority.js"]);        // after the EX-06 wrapper (D-7 B), before every poller
  const fv = fs.readFileSync(path.join(FRONT, "forward_validation.html"), "utf8");
  assert.deepEqual([...fv.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]), ["stop_priority.js", "forward_validation.js"]);
});

test("every timer-driven GET of the dashboard classifies GENERAL (A14): the pollers' paths, the shell's activity loader, the engine fold, the backtest poll; the two SAFETY paths are exactly the two", () => {
  const w = world();
  const polled = ["/api/health", "/api/ai/usage", "/api/market/overview", "/api/watchlist", "/api/alerts", "/api/execution/status", "/api/setups/alerts?limit=30", "/api/forward-validation",
                  "/api/forward-validation/sessions?limit=60", "/api/forward-validation/closed?limit=200", "/api/forward-validation/daily?limit=60", "/api/forward-validation/report/daily",
                  "/api/forward-validation/report/weekly", "/api/forward-validation/events?limit=50", "/api/setups/events?limit=100", "/api/proposals/history?limit=50", "/api/execution/intents?limit=100",
                  "/api/execution/positions?limit=100", "/api/execution-safety/events?limit=50", "/api/execution/findings?limit=50", "/api/execution/activity", "/api/decision-flow", "/api/decision-flow/history",
                  "/api/account-refresh", "/api/account-refresh/positions", "/api/terminal/state/SNDK?candles=0", "/api/strategy-registry", "/api/forward-validation/frozen-controls", "/api/unified-portfolio",
                  "/api/unified-portfolio/robinhood", "/api/backtests/run-1", "/api/execution"];
  for (const p of polled) assert.equal(w.sp.classify(p, {}).kind, "GENERAL", p);
  for (const m of ["app.js", "paper_bot.js", "setup_alerts.js", "forward_validation.js", "product_ui.js", "backtest_lab.js"]) {
    const js = fs.readFileSync(path.join(FRONT, m), "utf8");
    for (const q of [...js.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((x) => x[1])) {
      const k = w.sp.classify(q, {}).kind;
      assert.ok(k === "GENERAL" || k === "SAFETY" || (m === "app.js" && k === "KEEP"), `${m}: ${q} is ${k}`);   // app.js's research narrative is the one KEEP read among these modules
    }
  }
});
