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
// J-04 Emergency Stop reliability (TASK_P_STOP_RELIABILITY_ORDER_GATE_PLAN_2026-11-10.md, design 2.1, Part C):
//  - Right before every Stop POST (the automatic re-send included) this tab's background reads are preempted (window.StopPriority), so the POST
//    is never queued behind them; polling resumes when the Stop's outcome is final.
//  - While the POST is pending and until the decision, the saved latch is read every 2 s (GET /api/execution-safety/latch: the persisted kill
//    flag alone, answered in milliseconds while GET /api/execution-safety waits for the cash-flow cycle). A counted read is ordered like every
//    other read (D1); one that shows the stop saved confirms it and drives the display (the latch overlay: the HALTED kill label, "Emergency Stop
//    active", Clear shown, the open-order line), until a server view showing the stop cleared or a counted "not saved" replaces it; only a
//    server view announces the stop gone (B-3). Like any read it ages: after 65 s without a newer good read the page is uncertain again.
//  - Today's rules stay on top: no read counts before every earlier action settled; a saved stop does not finalise while an aborted Clear
//    may still commit; a counted "not saved" after the POST settled re-sends once (preempting again), whatever the POST's own outcome.
//  - Pause, Resume and Clear are available again as soon as the POST settles; a newer action ends this Stop's reconciliation and resolves its
//    outcome (CONFIRMED if a counted read showed it saved, NOT_CONFIRMED otherwise). Reconciliation lasts 60 s at most, or 3 s past an
//    aborted Clear's window (about 83 s).
//  - A POST's 200 body, a request being sent and a stale view are never shown as proof that the stop was saved. NOT_CONFIRMED carries the
//    fallback pointer. Nothing here touches an order or a position.
(function () {
  "use strict";
  const R = window.TraderRender;
  const POLL_MS = 30000;
  const TIMEOUT_MS = 20000;                                                     // reads must outlast the 15 s cash-flow cycle wait
  const STALE_MS = 65000;                                                       // no good read for this long: the shown state is uncertain
  const ABORTED_CLEAR_MS = 60000;                                               // an aborted Clear's server thread can still commit
  const RETRY_MS = [5000, 15000, 30000];                                        // reads after an aborted action or a failed decision read
  const STOP_RECHECK_MS = 5000;                                                 // the pace of a pending Stop's reads while an aborted Clear may commit
  const LATCH_PATH = "/api/execution-safety/latch";                             // J-04: the persisted latch alone (api/routes/execution_safety.py read_latch)
  const LATCH_TIMEOUT_MS = 5000;                                                // a latch read answers in milliseconds; a slow one is no information
  const LATCH_FIRST_MS = 1000;                                                  // the first latch read, after the POST is sent
  const LATCH_GAP_MS = 2000;                                                    // between latch reads
  const RECONCILE_MS = 60000;                                                   // a Stop's reconciliation: this long after the press, or 3 s past an aborted Clear's window
  const PREEMPT_MS = 30000;                                                     // the window StopPriority holds this tab's background reads for, at most
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
                  inflight: [], stopSeq: 0, abortedClearUntil: 0, clearedByPage: false, stopSeen: false, announce: null, openOrders: null, retryTimers: [],
                  latchKill: null, latchLast: null, stopPressedAt: null, stopActive: false };   // J-04: the latch overlay, the last counted latch read, the press, press-to-decision

  const now = () => Date.now();
  const readable = (j) => !!j && typeof j === "object" && KNOWN.indexOf(j.state) >= 0;     // getJson's {} for an unreadable 200 is a failed read (3.2 item 7)
  function errText(e) { return e && e.name === "AbortError" ? "no answer in 20 s" : (e && e.message) || "network error"; }
  function waitMs(ms) { return new Promise((r) => setTimeout(r, Math.max(0, ms))); }
  // J-04 Part A hooks: this tab's background reads step aside for the Stop; a missing or failing module never blocks the Stop
  function priority() { const sp = window.StopPriority; return sp && typeof sp.preempt === "function" && typeof sp.release === "function" ? sp : null; }
  function preempt() { try { const sp = priority(); if (sp) sp.preempt(PREEMPT_MS); } catch (e) { /* never blocks the Stop */ } }
  function release() { try { const sp = priority(); if (sp) sp.release(); } catch (e) { /* polling resumes by the window's end regardless */ } }
  function preempting() { try { const sp = priority(); return !!(sp && typeof sp.preempting === "function" && sp.preempting()); } catch (e) { return false; } }

  async function request(path, init, timeoutMs) {
    const ac = typeof AbortController === "function" ? new AbortController() : null;
    const t = ac ? setTimeout(() => ac.abort(), timeoutMs || TIMEOUT_MS) : null;
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
  function stale() { return !state.data || !state.readOk || state.lastGoodAt == null || now() - state.lastGoodAt > STALE_MS; }
  // J-04: a counted latch read that showed the stop saved is a fresh read of the kill switch, for STALE_MS like any read (review LOW-1): with no newer
  // good read, the overlay too goes "uncertain · last known" after 65 s
  function latchFresh() { return !!state.latchKill && now() - (Number(state.latchKill.at) || 0) <= STALE_MS; }
  function uncertain() { return stale() && !latchFresh(); }
  // J-04: the view the page shows. With the latch overlay set (a counted latch read showed the stop saved, and no later server view replaced it),
  // the kill switch reads active and the state HALTED with its label, over the last server view's other fields; otherwise the last server view
  function shown() {
    if (!state.latchKill) return state.data;
    const v = state.data || {};
    const k = v.kill_switch && typeof v.kill_switch === "object" ? v.kill_switch : {};
    return Object.assign({}, v, { state: "HALTED", state_label: R.SAFETY_LABELS.HALTED, icon: "🔴", allowed_new_entry: false, halted: true, reason_text: "Emergency stop active",
                                  kill_switch: Object.assign({}, k, { active: true, activated_at: state.latchKill.activated_at || k.activated_at || null }), latch: true });
  }
  function status() {
    const outcomes = {};
    for (const k of Object.keys(state.outcomes)) outcomes[k] = state.outcomes[k] ? Object.assign({}, state.outcomes[k]) : null;
    return { loading: state.loading && !!state.el, uncertain: uncertain(), pending: Object.assign({}, state.pending), error: state.error, outcomes, lastGoodAt: state.lastGoodAt,
             announce: state.announce, openOrders: state.openOrders ? Object.assign({}, state.openOrders) : null,
             latchKill: state.latchKill ? Object.assign({}, state.latchKill) : null, latchLast: state.latchLast ? Object.assign({}, state.latchLast) : null,   // J-04
             stopPressedAt: state.stopPressedAt, stopActive: state.stopActive, preempting: preempting(), now: now() };
  }
  function render() {
    if (!state.el) return;
    const s = status();
    const v = shown();
    state.el.innerHTML = R.safetyHtml(v, { detailsOpen: state.detailsOpen, confirmClear: state.confirmClear, error: s.error, loading: s.loading, uncertain: s.uncertain,
                                           pending: s.pending, outcomes: s.outcomes, lastGoodAt: s.lastGoodAt, announce: s.announce, openOrders: s.openOrders,
                                           latchLast: s.latchLast, stopPressedAt: s.stopPressedAt, now: s.now });
    state.el.dataset.state = (v && v.state) || "UNKNOWN";
    state.el.dataset.uncertain = s.uncertain ? "1" : "0";
    const hist = state.el.querySelector("[data-safety-history]");
    if (hist && state.data && Array.isArray(state.data.events)) hist.innerHTML = state.data.events.slice(0, 8).map((e) => `<li>${R.esc(R.safetyEventLine(e))}</li>`).join("") || "<li>No control history yet.</li>";
  }

  // ---------------------------------------------------------------- applying a server view --------------------------------------
  function applyView(v, viaRead) {
    state.data = v;
    const kill = WANTED.stop(v);
    // J-04 (design O-1, O-2): a later server view, a read or an action's 200 body, that shows the stop cleared replaces the latch overlay; one that
    // shows the kill active confirms it and keeps it (review MED-4: the Stop's own late 200 body must not demote a confirmed stop to "uncertain");
    // a server UNKNOWN with the default inactive kill switch is no information (3.1, B-3) and leaves it
    if (state.latchKill && !kill && !(v && v.state === "UNKNOWN")) state.latchKill = null;
    if (!kill && !state.latchKill) state.openOrders = null;
    if (!viaRead) return;                                                      // only a real read settles an outcome (a later read beats an earlier POST)
    if (kill) { state.stopSeen = true; state.clearedByPage = false; state.announce = null; }
    else if (WANTED.clear(v) && !state.stopActive) {
      // a stop that goes away is announced, unless this page cleared it (3.2 item 4); a pending Stop re-sends instead. A server UNKNOWN's
      // default inactive kill switch is no information (3.1, B-3): it never announces and never drops a confirmed stop
      if (state.stopSeen && !state.clearedByPage) state.announce = "Emergency Stop is no longer active.";
      state.stopSeen = false;
      const so = state.outcomes.stop;
      // a confirmed stop, or one this page could not confirm before its own Clear, is settled by this page's confirmed Clear (review follow-up)
      if (so && (so.result === "CONFIRMED" || (so.result === "NOT_CONFIRMED" && state.clearedByPage))) state.outcomes.stop = null;
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
  // J-04 Part C: the persisted latch alone, ordered like every read (D1): it counts only if no action started or finished since it was issued,
  // nothing newer was applied and this press is still the newest. Only kill_active true is information for the display: it sets the latch overlay
  // and bumps the applied sequence, so an older full read answered later cannot replace it. The intent header is set here too (design 3.2 item 9).
  async function latchRead(mine) {
    const seq = ++state.readSeq, gen = state.gen;
    render();                                                                  // before each read: the slow-Stop texts follow the clock
    let res = null;
    try { res = await request(LATCH_PATH, { method: "GET", headers: { "X-Stock-Agent-Intent": "dashboard" } }, LATCH_TIMEOUT_MS); } catch (e) { res = null; }
    const b = res && res.ok && res.body && typeof res.body === "object" ? res.body : null;
    const active = b && (b.kill_active === true || b.kill_active === false) ? b.kill_active : null;
    if (active === null || gen !== state.gen || seq <= state.appliedSeq || mine !== state.stopSeq) return { counted: false, active };
    state.latchLast = { active, at: now() };
    state.appliedSeq = seq;                                                    // a counted latch read is newer than any read issued before it (D1)
    if (active) {
      state.latchKill = { active: true, at: now(), activated_at: b.kill_activated_at || null };
      state.stopSeen = true; state.clearedByPage = false; state.announce = null;
      if (!preempting()) loadOpenOrders(); else state.ordersDeferred = true;   // a GENERAL read: while this tab preempts it would only wait; finish() reads it after the release
    } else {
      state.latchKill = null;                                                  // the latest counted read says not saved: no overlay from an older one (review MED-2); the flow re-sends
    }
    render();
    return { counted: true, active };
  }
  async function refresh() { await read(); return state.data; }
  // the 30 s poll keeps at most one read in flight (design B31): a hung poll never stacks up the browser's per-host connections, which
  // would queue an Emergency Stop POST behind them; reads after an action are not polls and always go. J-04: no poll while a Stop is pending
  // or reconciling, so this tab holds at most the POST and one latch read
  function poll() {
    if (state.pollInFlight || state.stopActive) return;
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
    if ((state.data && WANTED.stop(state.data)) || state.latchKill) { state.openOrders = line; render(); }
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
    // Emergency Stop: never blocked, never disabled; repeating it is harmless (the server records no event when the latch is already set).
    // J-04 Part C: preempt, POST, then latch reads every 2 s (while the POST is pending too) until the decision; the rules of 3.2 stay on top.
    const mine = ++state.stopSeq;
    const earlier = state.inflight.slice();                                    // the actions that started before this press
    const pressedAt = now();
    state.pending.stop = true; state.stopActive = true; state.stopPressedAt = pressedAt; state.latchLast = null;
    state.outcomes.stop = { result: "PENDING", at: pressedAt }; state.announce = null; state.clearedByPage = false;
    const seq = ++state.actionSeq; state.gen++;
    render();
    preempt();
    let res = null, errored = false, resent = false;
    const settle = (r) => {                                                    // the POST settled: Pause, Resume and Clear are available again (design M-3b)
      if (mine !== state.stopSeq) return;
      res = r; state.gen++; state.pending.stop = false; errored = errored || !!(r.aborted || r.failed);
      if (r.ok && seq === state.actionSeq) applyView(r.view, false);          // a response applies only if it is the latest action started (3.2 item 2; J-01)
      render();
    };
    send("stop").then(settle);
    // the aborted-Clear window is read live (review HIGH-1): an earlier Clear still in flight at the press may abort after it and open the window;
    // a Clear pressed after this Stop is a newer action and ends this flow instead. While the window is open, no decision is final (review M-4).
    const clearOpen = () => now() < state.abortedClearUntil;
    const deadline = () => Math.max(pressedAt + RECONCILE_MS, state.abortedClearUntil + LATCH_GAP_MS + 1000);
    const last = () => (state.latchLast ? state.latchLast.active : null);     // the latest counted latch read of this press (review MED-1: it decides)
    const posted = () => !!res && !!res.ok && WANTED.stop(res.view) && earlier.length === 0;
    const confirmed = () => ({ result: "CONFIRMED", note: errored ? "error-but-active" : null });
    const finish = (outcome) => {
      state.pending.stop = false; state.stopActive = false; state.outcomes.stop = Object.assign({ at: now() }, outcome);
      release();
      if (state.latchKill && state.ordersDeferred) loadOpenOrders();          // C-4: the read deferred while this tab preempted goes after the release
      state.ordersDeferred = false;
      render();
    };
    const notConfirmed = () => { finish({ result: "NOT_CONFIRMED", errored }); scheduleRetries(); };
    const resolveEarly = () => {                                               // a newer Pause, Resume or Clear ends the reconciliation (design O-3):
      if (!clearOpen() && last() === true) finish(confirmed());                // CONFIRMED only on a counted "saved" outside the window, NOT confirmed otherwise
      else notConfirmed();                                                     // (never "posted-unread" here: design 2.1 Part C); the retry reads settle it
    };
    await waitMs(LATCH_FIRST_MS);
    for (;;) {
      if (mine !== state.stopSeq) return;                                      // a newer press owns the outcome
      if (state.actionSeq !== seq) { resolveEarly(); return; }
      await Promise.all(earlier.map((x) => x.promise.catch(() => null)));     // decided only by reads issued after every earlier action finished
      if (mine !== state.stopSeq) return;
      if (state.actionSeq !== seq) { resolveEarly(); return; }
      const lr = await latchRead(mine);
      if (mine !== state.stopSeq) return;
      if (state.actionSeq !== seq) { resolveEarly(); return; }
      if (!clearOpen() && last() === true) { finish(confirmed()); return; }    // the latest counted read says saved, and no aborted Clear can still commit
      if (lr.counted && lr.active === false && res !== null) {
        // not saved after the POST settled (whatever its own outcome) and the latest intent is Stop: once more, preempting again (review N-2); the
        // latch reads go on meanwhile (review LOW-3). A second counted "not saved" is final only once the re-send settled and the window is closed.
        if (resent) { if (!clearOpen()) { notConfirmed(); return; } }
        else {
          resent = true; res = null;
          preempt();
          send("stop").then((r2) => {
            if (mine !== state.stopSeq) return;
            res = r2; state.gen++; errored = errored || !!(r2.aborted || r2.failed);
            if (r2.ok && seq === state.actionSeq) applyView(r2.view, false);
            render();
          });
        }
      }
      if (now() > deadline()) {                                                // out of time (a counted "saved" outside the window decided above): never success without a read
        if (posted() && !clearOpen()) { state.stopSeen = true; finish({ result: "CONFIRMED", note: "posted-unread" }); scheduleRetries(); }
        else notConfirmed();
        return;
      }
      await waitMs(LATCH_GAP_MS);
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
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.stopActive) read(); });   // a throttled background tab reads on return (3.2 item 8); not during a Stop (J-04)
  }

  window.RiskControls = { mount, refresh, state, status };
})();
