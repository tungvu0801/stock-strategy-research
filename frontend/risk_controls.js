// Stage 5.9.1 — RISK CONTROLS panel for Trader View: the persisted execution safety gate for a beginner. 🟢 Trading allowed ·
// 🟡 New trades paused · 🔴 Trading stopped, one reason line, Pause New Trades / Resume / Emergency Stop, and — only while an emergency
// stop is active — Clear Emergency Stop (two clicks). "View details" reveals the numbers and the control history. The POSTs change
// safety state only; nothing here places, previews or cancels an order.
(function () {
  "use strict";
  const R = window.TraderRender;
  const state = { data: null, el: null, timer: null, busy: false, detailsOpen: false, confirmClear: false, confirmTimer: null, error: null };
  const POLL_MS = 30000;

  async function getJson(path) {
    const r = await fetch(path);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
    return j;
  }
  async function post(path, reason) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(reason ? { reason } : {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
    return j;
  }

  function render() {
    if (!state.el) return;
    state.el.innerHTML = R.safetyHtml(state.data, { detailsOpen: state.detailsOpen, confirmClear: state.confirmClear, error: state.error });
    state.el.dataset.state = (state.data && state.data.state) || "UNKNOWN";
    const hist = state.el.querySelector("[data-safety-history]");
    if (hist && state.data && Array.isArray(state.data.events)) hist.innerHTML = state.data.events.slice(0, 8).map((e) => `<li>${R.esc(R.safetyEventLine(e))}</li>`).join("") || "<li>No control history yet.</li>";
  }

  async function refresh() {
    try { state.data = await getJson("/api/execution-safety"); state.error = null; }
    catch (e) { state.error = `Risk controls unavailable (${e.message}).`; }
    render();
    return state.data;
  }

  async function act(path, label) {
    if (state.busy) return;
    state.busy = true;
    try { state.data = await post(path); state.error = null; }
    catch (e) { state.error = `${label} failed (${e.message}).`; }
    finally { state.busy = false; render(); }
  }

  function onClick(e) {
    const b = e.target.closest("[data-safety]");
    if (!b || b.disabled) return;
    const kind = b.dataset.safety;
    if (kind === "details") { state.detailsOpen = !state.detailsOpen; render(); return; }
    if (kind === "pause") return act("/api/execution-safety/pause-new-entries", "Pause");
    if (kind === "resume") return act("/api/execution-safety/resume-new-entries", "Resume");
    if (kind === "stop") return act("/api/execution-safety/emergency-stop", "Emergency Stop");
    if (kind === "clear") {
      if (!state.confirmClear) {                                             // a distinct, explicit two-step action
        state.confirmClear = true; render();
        clearTimeout(state.confirmTimer); state.confirmTimer = setTimeout(() => { state.confirmClear = false; render(); }, 8000);
        return;
      }
      clearTimeout(state.confirmTimer); state.confirmClear = false;
      return act("/api/execution-safety/clear-emergency-stop", "Clear Emergency Stop");
    }
  }

  function mount(id) {
    state.el = document.getElementById(id);
    if (!state.el || state.el.dataset.mounted) return;
    state.el.dataset.mounted = "1";
    state.el.addEventListener("click", onClick);
    render();
    refresh();
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (document.getElementById(id)) refresh(); else clearInterval(state.timer); }, POLL_MS);
  }

  window.RiskControls = { mount, refresh, state };
})();
