// J-04 Emergency Stop request reliability (TASK_P_STOP_RELIABILITY_ORDER_GATE_PLAN_2026-11-10.md, design revision 2.1, Part A): the page's
// request discipline, so an Emergency Stop is never queued behind this dashboard's own background reads.
//   - The browser allows six HTTP/1.1 connections per host, shared by every tab of one profile. The dashboard's timers (app.js, the paper bot,
//     the setup alerts, the forward-validation panel and page, the product shell) have no in-flight guard and no timeout, so when the server is
//     slow they fill every connection and a Stop POST waits unsent until the page gives it up.
//   - This module wraps window.fetch once, after app.js's EX-06 intent wrapper (which stays the first code the dashboard runs). Same-origin
//     reads are classified: SAFETY (exactly GET /api/execution-safety and /api/execution-safety/latch), KEEP (user-initiated slow or paid reads,
//     never aborted or held in the pressing tab) and GENERAL (everything else, sent with a low priority). Writes, other origins and the
//     WebSocket are never touched. In normal operation nothing else changes: no cap, no timeout.
//   - preempt(), called by risk_controls.js right before every Stop POST: aborts this tab's in-flight GENERAL reads, holds new ones until
//     release() or the window ends (30 s at most), and tells the other tabs and the forward-validation page on a BroadcastChannel; they abort
//     and hold every read of theirs for the window (a tab with its own Stop pending keeps its SAFETY reads). A held read honours its caller's
//     AbortSignal; an aborted one rejects with AbortError, which every poller already handles as "unavailable".
//   - Cooperative only: a tab that does not load this module or does not receive the message keeps its connections, and a request already on
//     the wire frees its socket only when the browser cancels it. Nothing here claims that a Stop always bypasses the connection limit, and
//     nothing here saves, confirms or clears a Stop: the server's latch does that, and only a server read confirms it.
//   - No DOM, no storage, no markup. Loaded as a classic script (window.StopPriority) and required by tests/frontend/stop_priority.test.cjs.
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.StopPriority = api;
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : null), function (root) {
  "use strict";
  const VERSION = "stop_priority_v1";
  const SAFETY = ["/api/execution-safety", "/api/execution-safety/latch"];           // exact paths (design N-1): the Risk Controls read and the latch read
  const KEEP_PREFIXES = ["/api/stocks/", "/api/daily-brief"];                        // user-initiated slow or paid reads (the research narrative, the Daily Brief)
  const CHANNEL = "stock-agent-stop";
  const WINDOW_MAX_MS = 30000;
  const WINDOW_MIN_MS = 1000;
  const OWN_MAX_MS = 120000;                                                          // a Stop's own reconciliation never outlives this without a release (review LOW-4)
  const G = typeof globalThis !== "undefined" ? globalThis : {};
  const timers = { set: (fn, ms) => (root && root.setTimeout ? root.setTimeout : G.setTimeout)(fn, ms), clear: (id) => (root && root.clearTimeout ? root.clearTimeout : G.clearTimeout)(id) };
  const now = () => (root && root.Date ? root.Date : Date).now();
  const AC = root && typeof root.AbortController === "function" ? root.AbortController : (typeof G.AbortController === "function" ? G.AbortController : null);
  const TAB_ID = `${now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  const state = { installed: false, orig: null, inflight: new Set(), held: [], holdUntil: 0, holdTimer: null, own: false, ownUntil: 0, preemptAt: 0,
                  remoteFrom: null, remoteAt: 0, channel: null, counts: { aborted: 0, held: 0, released: 0, preempts: 0, remote: 0 } };

  function abortError() {
    try { return new (root && root.DOMException ? root.DOMException : G.DOMException)("The operation was aborted.", "AbortError"); }
    catch (e) { const err = new Error("The operation was aborted."); err.name = "AbortError"; return err; }
  }
  function signalOf(input, init) {
    if (init && init.signal) return init.signal;
    return input && typeof input === "object" && input.signal ? input.signal : null;
  }
  // the request's kind: WRITE (any method but GET / HEAD), FOREIGN (another origin, or no page location to compare with), SAFETY, KEEP, GENERAL
  function classify(input, init) {
    const method = String((init && init.method) || (input && typeof input === "object" && input.method) || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") return { kind: "WRITE", method, path: null };
    const raw = typeof input === "string" ? input : input && typeof input === "object" && typeof input.url === "string" ? input.url : String(input);
    const loc = root && root.location && root.location.href ? root.location : null;
    if (!loc) return { kind: "FOREIGN", method, path: null };
    const u = new URL(raw, loc.href);
    if (u.origin !== new URL(loc.href).origin) return { kind: "FOREIGN", method, path: u.pathname };
    const path = u.pathname;
    if (SAFETY.indexOf(path) >= 0) return { kind: "SAFETY", method, path };
    if (KEEP_PREFIXES.some((p) => path === p || path.startsWith(p))) return { kind: "KEEP", method, path };
    return { kind: "GENERAL", method, path };
  }

  // ---------------------------------------------------------------- holding and releasing ---------------------------------------------
  const holding = () => now() < state.holdUntil;
  const own = () => !!state.own && now() < state.preemptAt + OWN_MAX_MS;       // this tab's Stop is pending or reconciling (until its release, OWN_MAX_MS at most)
  const preempting = () => own() && now() < state.ownUntil;                     // ... and its hold window is open (the UI's "panels paused" line)
  function armHoldTimer() {
    if (state.holdTimer != null) timers.clear(state.holdTimer);
    state.holdTimer = null;
    if (holding()) state.holdTimer = timers.set(() => { state.holdTimer = null; endHold(); }, Math.max(0, state.holdUntil - now()));
    else endHold();                                                              // the window ended between the caller's check and this one (review LOW-1): nothing stays held
  }
  function setHold(until) {
    state.holdUntil = Math.min(until, now() + WINDOW_MAX_MS);
    armHoldTimer();
  }
  function endHold() {
    state.holdUntil = 0;
    if (state.holdTimer != null) { timers.clear(state.holdTimer); state.holdTimer = null; }
    const run = state.held.slice();
    state.held = [];
    for (const h of run) { state.counts.released++; h.run(); }                       // in the order they were asked for
  }
  function abortInflight(pred) {
    for (const e of Array.from(state.inflight)) {
      if (!pred(e)) continue;
      state.inflight.delete(e);
      state.counts.aborted++;
      try { e.ac.abort(abortError()); } catch (err) { /* the caller sees the rejection either way */ }
    }
  }

  // ---------------------------------------------------------------- the wrapper -------------------------------------------------------
  function tracked(input, init, c) {
    const orig = state.orig;
    if (!AC) return orig(input, init);
    const ac = new AC();
    const signal = signalOf(input, init);
    const entry = { ac, kind: c.kind, path: c.path, at: now() };
    if (signal) {
      if (signal.aborted) ac.abort(signal.reason);
      else signal.addEventListener("abort", () => ac.abort(signal.reason), { once: true });
    }
    state.inflight.add(entry);
    const done = () => { state.inflight.delete(entry); };
    let p;
    try { p = orig(input, Object.assign({}, init || {}, { signal: ac.signal })); } catch (e) { done(); throw e; }
    p.then(done, done);
    return p;
  }
  function hold(input, init, c) {
    const signal = signalOf(input, init);
    state.counts.held++;
    return new Promise((resolve, reject) => {
      const h = { run: null, drop: null };
      const remove = () => { state.held = state.held.filter((x) => x !== h); };
      h.run = () => { remove(); try { resolve(tracked(input, init, c)); } catch (e) { reject(e); } };
      h.drop = () => { remove(); reject(abortError()); };
      if (signal) {
        if (signal.aborted) return h.drop();
        signal.addEventListener("abort", h.drop, { once: true });
      }
      state.held.push(h);
      armHoldTimer();
    });
  }
  function wrapped(input, init) {
    let c;
    try { c = classify(input, init); } catch (e) { c = { kind: "FOREIGN" }; }
    if (c.kind === "WRITE" || c.kind === "FOREIGN") return state.orig(input, init);     // writes and other origins: untouched
    if (c.kind === "GENERAL") {
      const init2 = init && init.priority ? init : Object.assign({}, init || {}, { priority: "low" });   // a Stop's requests load at the default (high) priority
      return holding() ? hold(input, init2, c) : tracked(input, init2, c);
    }
    // SAFETY and KEEP: never held or aborted by this tab's own Stop (pending or reconciling); held and aborted while another tab's Stop is on its way (design O-4)
    return holding() && !own() ? hold(input, init, c) : tracked(input, init, c);
  }

  // ---------------------------------------------------------------- the Stop's hooks -------------------------------------------------
  function post(msg) {
    try { if (state.channel) state.channel.postMessage(Object.assign({ from: TAB_ID, at: now() }, msg)); } catch (e) { /* cooperative only */ }
  }
  function preempt(windowMs) {
    // called right before every Stop POST (the re-send included): this tab's GENERAL reads are aborted, new ones held, the other tabs told
    try {
      const w = Math.min(Math.max(Number(windowMs) || WINDOW_MAX_MS, WINDOW_MIN_MS), WINDOW_MAX_MS);
      const until = now() + w;
      state.own = true; state.ownUntil = until; state.preemptAt = now(); state.counts.preempts++;
      abortInflight((e) => e.kind === "GENERAL");
      setHold(until);
      post({ type: "stop-preempt", until });
    } catch (e) { /* a failure here never blocks the Stop */ }
  }
  function release() {
    // the Stop's outcome is final (or the page is leaving): polling resumes here and in the other tabs
    try {
      state.own = false; state.ownUntil = 0;
      endHold();
      post({ type: "stop-release" });
    } catch (e) { /* cooperative only */ }
  }
  function onMessage(ev) {
    const m = ev && ev.data;
    if (!m || typeof m !== "object" || m.from === TAB_ID) return;
    if (m.type === "stop-preempt") {
      const until = Math.min(Number(m.until) || 0, now() + WINDOW_MAX_MS);
      if (until <= now()) return;                                                      // expired, or out of order
      if (own()) return;                                                               // this tab's own Stop is pending or reconciling: it keeps its reads
      state.counts.remote++; state.remoteFrom = m.from; state.remoteAt = Number(m.at) || now();
      abortInflight(() => true);                                                       // every read of this page: it is not the pressing tab
      setHold(Math.max(state.holdUntil, until));
    } else if (m.type === "stop-release") {
      if (!own() && m.from === state.remoteFrom && (Number(m.at) || 0) >= state.remoteAt) endHold();   // only the tab that preempted, never out of order
    }
  }

  function install() {
    if (state.installed || !root || typeof root.fetch !== "function" || root.fetch.__stopPriority) return false;
    state.orig = root.fetch.bind(root);
    wrapped.__stopPriority = true;                                                     // never __stockAgentIntent: app.js's wrapper keeps its own guard
    root.fetch = wrapped;
    state.installed = true;
    try {
      if (typeof root.BroadcastChannel === "function") { state.channel = new root.BroadcastChannel(CHANNEL); state.channel.onmessage = onMessage; }
    } catch (e) { state.channel = null; }
    try {
      if (typeof root.addEventListener === "function") root.addEventListener("pagehide", () => { if (state.own) release(); });   // a reload mid-Stop frees the other tabs
    } catch (e) { /* no page */ }
    return true;
  }
  install();

  return { VERSION, SAFETY, KEEP_PREFIXES, CHANNEL, WINDOW_MAX_MS, OWN_MAX_MS, TAB_ID, classify, preempt, release, preempting, reconciling: own, holding, state, install,
           _onMessage: onMessage };
});
