// Stage 5.9.1 — RISK CONTROLS panel for Trader View: the persisted execution safety gate for a beginner, one reason line, Pause New Trades /
// Resume / Emergency Stop, and — only while an emergency stop is active — Clear Emergency Stop (two clicks). "View details" reveals the numbers
// and the control history. The POSTs change safety state only; nothing here places, previews or cancels an order.
// Risk controls panel (RISK_CONTROLS_PANEL_DESIGN.md 3.2): the server is the truth; this page only reports what a server read shows.
//  - Every write to the shown state is ordered: a generation counter moves at the start and at the end of every action, and a read applies
//    only if no action started or finished since it was issued and no newer read was applied (D1).
//  - Emergency Stop is never blocked or disabled. Its outcome is decided only by a read issued after every earlier action finished; a read
//    that shows it not active sends it once more. An aborted Clear counts as unfinished for 60 s, because it may still commit on the server.
//  - Reads and actions time out after 20 s; after an aborted action, reads follow at +5, +15 and +30 s. A 200 with no known state is a
//    failed read.
//  - Each action has its own outcome: PENDING, CONFIRMED or NOT_CONFIRMED. NOT_CONFIRMED stays until a read shows the wanted state or the
//    operator presses again; it never fades on a timer.
//  - status() is the single source for the product shell (product_ui.js).
(function () {
  "use strict";
  const R = window.TraderRender;
  const POLL_MS = 30000;
  const TIMEOUT_MS = 20000;                                                     // reads must outlast the 15 s cash-flow cycle wait
  const STALE_MS = 65000;                                                       // no good read for this long: the shown state is uncertain
  const ABORTED_CLEAR_MS = 60000;                                               // an aborted Clear's server thread can still commit
  const RETRY_MS = [5000, 15000, 30000];                                        // reads after an aborted action or a failed decision read
  const STOP_RECHECK_MS = 5000;                                                 // the pace of a pending Stop's reads while an aborted Clear may commit
  const KNOWN = ["TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN"];
  const PATHS = { pause: "/api/execution-safety/pause-new-entries", resume: "/api/execution-safety/resume-new-entries",
                  stop: "/api/execution-safety/emergency-stop", clear: "/api/execution-safety/clear-emergency-stop" };
  // an action is confirmed only when a real read shows the wanted state (design 3.2 item 6)
  const WANTED = {
    stop: (v) => !!v && !!v.kill_switch && v.kill_switch.active === true,                                    // in any state, UNKNOWN included (B-3)
    clear: (v) => !!v && v.state !== "UNKNOWN" && !!v.kill_switch && v.kill_switch.active === false,
    pause: (v) => !!v && v.state !== "UNKNOWN" && !!v.no_new_entries && v.no_new_entries.active === true,
    resume: (v) => !!v && v.state !== "UNKNOWN" && !!v.no_new_entries && v.no_new_entries.active === false,
  };
  const state = { data: null, el: null, timer: null, detailsOpen: false, confirmClear: false, confirmTimer: null, error: null,
                  loading: true, readOk: false, lastGoodAt: null, gen: 0, readSeq: 0, appliedSeq: 0, actionSeq: 0, busy: null,
                  pending: { stop: false, pause: false, resume: false, clear: false }, outcomes: { stop: null, pause: null, resume: null, clear: null },
                  inflight: [], stopSeq: 0, abortedClearUntil: 0, clearedByPage: false, stopSeen: false, announce: null, openOrders: null, retryTimers: [] };

  const now = () => Date.now();
  const readable = (j) => !!j && typeof j === "object" && KNOWN.indexOf(j.state) >= 0;     // getJson's {} for an unreadable 200 is a failed read (3.2 item 7)
  function errText(e) { return e && e.name === "AbortError" ? "no answer in 20 s" : (e && e.message) || "network error"; }
  function waitMs(ms) { return new Promise((r) => setTimeout(r, Math.max(0, ms))); }

  async function request(path, init) {
    const ac = typeof AbortController === "function" ? new AbortController() : null;
    const t = ac ? setTimeout(() => ac.abort(), TIMEOUT_MS) : null;
    try {
      const r = await fetch(path, Object.assign({}, init, ac ? { signal: ac.signal } : {}));
      const j = await r.json().catch(() => ({}));
      return { ok: r.ok, status: r.status, body: j };
    } finally { if (t) clearTimeout(t); }
  }
  function post(path) {
    // the intent header is set here, so Emergency Stop never depends on app.js's fetch wrapper (design 3.2 item 9; the EX-06 local gate)
    return request(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Stock-Agent-Intent": "dashboard" }, body: "{}" });
  }

  // ---------------------------------------------------------------- what the page shows -------------------------------------------
  function uncertain() { return !state.data || !state.readOk || state.lastGoodAt == null || now() - state.lastGoodAt > STALE_MS; }
  function status() {
    const outcomes = {};
    for (const k of Object.keys(state.outcomes)) outcomes[k] = state.outcomes[k] ? Object.assign({}, state.outcomes[k]) : null;
    return { loading: state.loading && !!state.el, uncertain: uncertain(), pending: Object.assign({}, state.pending), error: state.error, outcomes, lastGoodAt: state.lastGoodAt,
             announce: state.announce, openOrders: state.openOrders ? Object.assign({}, state.openOrders) : null };
  }
  function render() {
    if (!state.el) return;
    const s = status();
    state.el.innerHTML = R.safetyHtml(state.data, { detailsOpen: state.detailsOpen, confirmClear: state.confirmClear, error: s.error, loading: s.loading, uncertain: s.uncertain,
                                                    pending: s.pending, outcomes: s.outcomes, lastGoodAt: s.lastGoodAt, announce: s.announce, openOrders: s.openOrders });
    state.el.dataset.state = (state.data && state.data.state) || "UNKNOWN";
    state.el.dataset.uncertain = s.uncertain ? "1" : "0";
    const hist = state.el.querySelector("[data-safety-history]");
    if (hist && state.data && Array.isArray(state.data.events)) hist.innerHTML = state.data.events.slice(0, 8).map((e) => `<li>${R.esc(R.safetyEventLine(e))}</li>`).join("") || "<li>No control history yet.</li>";
  }

  // ---------------------------------------------------------------- applying a server view --------------------------------------
  function applyView(v, viaRead) {
    state.data = v;
    const kill = WANTED.stop(v);
    if (!kill) state.openOrders = null;
    if (!viaRead) return;                                                      // only a real read settles an outcome (a later read beats an earlier POST)
    if (kill) { state.stopSeen = true; state.clearedByPage = false; state.announce = null; }
    else if (WANTED.clear(v) && !state.pending.stop) {
      // a stop that goes away is announced, unless this page cleared it (3.2 item 4); a pending Stop re-sends instead. A server UNKNOWN's
      // default inactive kill switch is no information (3.1, B-3): it never announces and never drops a confirmed stop
      if (state.stopSeen && !state.clearedByPage) state.announce = "Emergency Stop is no longer active.";
      state.stopSeen = false;
      const so = state.outcomes.stop;
      if (so && so.result === "CONFIRMED") state.outcomes.stop = null;
    }
    for (const k of Object.keys(WANTED)) {
      const o = state.outcomes[k];
      if (o && !state.pending[k] && o.result === "NOT_CONFIRMED" && WANTED[k](v)) state.outcomes[k] = { result: "CONFIRMED", note: o.errored ? "error-but-active" : null, at: now() };
      else if (k === "stop" && o && o.result === "CONFIRMED" && o.note === "posted-unread" && kill) state.outcomes.stop = { result: "CONFIRMED", note: null, at: now() };
    }
  }

  async function read() {
    const seq = ++state.readSeq, gen = state.gen;
    let res = null, err = null;
    try { res = await request("/api/execution-safety", { method: "GET" }); } catch (e) { err = e; }
    const good = !!res && res.ok && readable(res.body);
    if (gen === state.gen && seq > state.appliedSeq) {                         // D1: nothing started or finished since it was issued, nothing newer applied
      state.appliedSeq = seq;
      state.loading = false;
      if (good) { state.readOk = true; state.lastGoodAt = now(); state.error = null; applyView(res.body, true); }
      else { state.readOk = false; state.error = `Risk controls unavailable (${err ? errText(err) : res && !res.ok ? (res.body && res.body.message) || `HTTP ${res.status}` : "unreadable answer"}).`; }
    }
    if (good && WANTED.stop(res.body)) loadOpenOrders();
    render();
    return good ? res.body : null;
  }
  async function refresh() { await read(); return state.data; }
  // the 30 s poll keeps at most one read in flight (design B31): a hung poll never stacks up the browser's per-host connections, which
  // would queue an Emergency Stop POST behind them; reads after an action are not polls and always go
  function poll() {
    if (state.pollInFlight) return;
    state.pollInFlight = true;
    read().finally(() => { state.pollInFlight = false; });
  }

  async function loadOpenOrders() {
    // C-4: read only, from the stored account snapshot (0 broker calls); shown only while the kill is active
    let line = { status: "UNAVAILABLE", count: null, at: null };
    try {
      const r = await request("/api/account-refresh", { method: "GET" });
      const b = r.ok && r.body ? r.body : {};
      const snap = b.snapshot && typeof b.snapshot === "object" ? b.snapshot : null;          // account_refresh overview(): the summary, open_orders is a count
      line = { status: String(b.status || "UNAVAILABLE"), count: snap && typeof snap.open_orders === "number" ? snap.open_orders : null, at: b.refreshed_at || null };
    } catch (e) { /* stays unavailable */ }
    if (state.data && WANTED.stop(state.data)) { state.openOrders = line; render(); }
  }

  function scheduleRetries() {
    for (const t of state.retryTimers) clearTimeout(t);
    state.retryTimers = RETRY_MS.map((ms) => setTimeout(() => { read(); }, ms));
  }

  // ---------------------------------------------------------------- actions -------------------------------------------------------
  async function send(kind) {
    // one POST: { ok, view } when the server answered with a readable state, { failed } on an HTTP error, { aborted } on no answer
    try {
      const r = await post(PATHS[kind]);
      if (r.ok && readable(r.body)) return { ok: true, view: r.body };
      return { failed: true, message: (r.body && r.body.message) || `HTTP ${r.status}` };
    } catch (e) { return { aborted: true, message: errText(e) }; }
  }

  async function simpleAction(kind) {
    // Pause, Resume and Clear: one at a time, and never while a Stop is pending (3.2 item 4)
    if (state.busy || state.pending.stop) return;
    state.busy = kind; state.pending[kind] = true; state.outcomes[kind] = { result: "PENDING", at: now() }; state.announce = null;
    if (kind === "clear") state.clearedByPage = true;
    const seq = ++state.actionSeq; state.gen++;
    render();
    const promise = send(kind);
    const entry = { kind, promise };
    state.inflight.push(entry);
    const res = await promise;
    state.inflight = state.inflight.filter((x) => x !== entry);
    state.gen++;
    state.busy = null; state.pending[kind] = false;
    if (res.aborted && kind === "clear") state.abortedClearUntil = now() + ABORTED_CLEAR_MS;
    if (res.ok && seq === state.actionSeq) applyView(res.view, false);         // a response applies only if it is the latest action started
    const v = await read();                                                    // settle at once (3.2 item 3)
    if (state.outcomes[kind] && state.outcomes[kind].result === "PENDING") {
      state.outcomes[kind] = v && WANTED[kind](v) ? { result: "CONFIRMED", at: now() }
        : { result: "NOT_CONFIRMED", errored: !!(res.aborted || res.failed), message: res.message || null, at: now() };
    }
    if (kind === "clear" && !(v && WANTED.clear(v))) state.clearedByPage = false;
    if (res.aborted || !v) scheduleRetries();
    render();
  }

  async function stopFlow() {
    // Emergency Stop: never blocked, never disabled; repeating it is harmless (the server records no event when the latch is already set)
    const mine = ++state.stopSeq;
    const earlier = state.inflight.slice();                                    // the actions that started before this press
    state.pending.stop = true; state.outcomes.stop = { result: "PENDING", at: now() }; state.announce = null; state.clearedByPage = false;
    const seq = ++state.actionSeq; state.gen++;
    render();
    let res = await send("stop");
    state.gen++;
    if (mine !== state.stopSeq) return;                                        // a newer press owns the outcome
    if (res.ok && seq === state.actionSeq) applyView(res.view, false);
    let errored = !!(res.aborted || res.failed), resent = false;
    const finish = (outcome) => { state.pending.stop = false; state.outcomes.stop = Object.assign({ at: now() }, outcome); render(); };
    for (;;) {
      await Promise.all(earlier.map((x) => x.promise.catch(() => null)));     // decided only by a read issued after every earlier action finished
      const v = await read();
      if (mine !== state.stopSeq) return;
      const clearOpen = now() < state.abortedClearUntil;                       // an aborted Clear may still commit (review M-4)
      if (!v) {                                                                // the decision read failed
        const postedActive = !!res.ok && WANTED.stop(res.view) && earlier.length === 0 && !clearOpen;
        if (postedActive) state.stopSeen = true;                                 // a later read that shows it gone is announced (3.2 item 4)
        finish(postedActive ? { result: "CONFIRMED", note: "posted-unread" } : { result: "NOT_CONFIRMED", errored });
        scheduleRetries(); return;
      }
      if (WANTED.stop(v)) {
        if (!clearOpen) { finish({ result: "CONFIRMED", note: errored ? "error-but-active" : null }); return; }
        await waitMs(Math.min(STOP_RECHECK_MS, state.abortedClearUntil - now() + 50));
        if (mine !== state.stopSeq) return;
        continue;
      }
      if (resent && !clearOpen) { finish({ result: "NOT_CONFIRMED", errored }); if (res.aborted) scheduleRetries(); return; }
      resent = true;                                                           // a real read shows the stop not active: the latest intent is Stop
      res = await send("stop"); state.gen++;
      if (mine !== state.stopSeq) return;
      errored = errored || !!(res.aborted || res.failed);
      if (clearOpen) { await waitMs(STOP_RECHECK_MS); if (mine !== state.stopSeq) return; }
    }
  }

  function onClick(e) {
    const b = e.target.closest("[data-safety]");
    if (!b || b.disabled) return;
    const kind = b.dataset.safety;
    if (kind === "details") { state.detailsOpen = !state.detailsOpen; render(); return; }
    if (kind === "stop") return stopFlow();
    if (kind === "pause" || kind === "resume") return simpleAction(kind);
    if (kind === "clear") {
      if (!state.confirmClear) {                                             // a distinct, explicit two-step action
        state.confirmClear = true; render();
        clearTimeout(state.confirmTimer); state.confirmTimer = setTimeout(() => { state.confirmClear = false; render(); }, 8000);
        return;
      }
      clearTimeout(state.confirmTimer); state.confirmClear = false;
      return simpleAction("clear");
    }
  }

  function mount(id) {
    state.el = document.getElementById(id);
    if (!state.el || state.el.dataset.mounted) return;
    state.el.dataset.mounted = "1";
    state.el.addEventListener("click", onClick);
    render();
    read();
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (document.getElementById(id)) poll(); else clearInterval(state.timer); }, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) read(); });   // a throttled background tab reads on return (3.2 item 8)
  }

  window.RiskControls = { mount, refresh, state, status };
})();
