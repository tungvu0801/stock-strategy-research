// Stage 6.0 — PAPER BOT panel and PAPER TRADE card for Trader View. A beginner sees: ⚪ Monitor only · 🟢 Paper Auto · 🔴 Needs attention,
// one plain line, Enable Paper Auto (two clicks) / Stop New Trades, and per ticker 🟡 Watching · 🔵 Ready · 🟠 Entering · 🟢 In trade ·
// 🟠 Exiting · ⚪ Closed · 🔴 Needs attention with shares, entry, current, P/L and the exit reason. Internal states, ids and events live under
// View Details. The two POSTs change the persisted mode only; the executor loop decides every paper order by its own deterministic rules on
// the Alpaca PAPER account. Nothing here is advice. Loaded as a classic script (window.PaperBot) and required by the node tests.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  root.PaperBot = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const words = (x) => (x == null || x === "" ? "" : String(x).replace(/_/g, " ").toLowerCase());
  const stampOf = (iso, n) => (iso ? `${String(iso).slice(11, n || 19)} UTC` : "—");
  const dateOf = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "—");
  const POLL_MS = 10000;
  const ICONS = { MONITOR_ONLY: "⚪", PAPER_AUTO: "🟢", ATTENTION: "🔴" };
  const LABELS = { MONITOR_ONLY: "Monitor only", PAPER_AUTO: "Paper Auto", ATTENTION: "Needs attention" };
  const state = { data: null, el: null, cardEl: null, timer: null, mountTimer: null, busy: false, detailsOpen: false, cardDetailsOpen: false, confirmEnable: false, confirmTimer: null, error: null, lastSymbol: null };

  // ---------------------------------------------------------------- pure models ------------------------------------------------------
  function botModel(d) {
    const loaded = !!d && !!d.mode && !!LABELS[d.mode];
    const mode = loaded ? d.mode : "MONITOR_ONLY";
    const attention = !!(d && d.attention);
    const key = attention ? "ATTENTION" : mode;
    const n = (k) => Number((d && d[k]) || 0);
    const parts = [];
    if (loaded) {
      if (n("open_positions")) parts.push(`${n("open_positions")} paper position${n("open_positions") === 1 ? "" : "s"} open`);
      if (n("pending_entries")) parts.push(`${n("pending_entries")} order${n("pending_entries") === 1 ? "" : "s"} waiting`);
      if (n("pending_exits")) parts.push(`${n("pending_exits")} exit${n("pending_exits") === 1 ? "" : "s"} in progress`);
    }
    const reasons = (d && d.attention_reasons) || [];
    const line = attention ? (reasons[0] || "Needs attention") : (parts.length ? parts.join(" · ") : (mode === "PAPER_AUTO" ? "Watching for ready setups" : "Watching only; no paper orders"));
    return { loaded, mode, key, icon: loaded ? ICONS[key] : "⚪", label: loaded ? LABELS[key] : "Checking paper bot…", attention, reasons, line, cls: key === "PAPER_AUTO" ? "auto" : (key === "ATTENTION" ? "attention" : ""),
             safety: (d && d.safety_label) || "", safetyState: (d && d.safety_state) || "", env: (d && d.environment) || {}, lastCycle: stampOf(d && d.last_cycle_at), lastResult: (d && d.last_cycle_result) || "—",
             openPositions: n("open_positions"), pendingEntries: n("pending_entries"), pendingExits: n("pending_exits"), maxOpen: n("max_open_positions"), external: n("external_positions"),
             healthy: !!(d && d.reconciliation_healthy), changedAt: (d && d.mode_changed_at) || null, changedBy: (d && d.mode_changed_by) || null, recent: (d && d.recent) || [],
             sched: (d && d.scheduler) || {}, disclaimer: (d && d.disclaimer) || "", raw: d || null };
  }

  function botHtml(d, opts) {
    const o = opts || {};
    const m = botModel(d);
    const head = `<div class="tv-bot-head"><span class="tv-bot-state ${m.cls}" data-bot-mode="${esc(m.key)}">${esc(m.icon)} ${esc(m.label)}</span></div>`;
    const line = `<div class="tv-bot-line" data-bot-line>${esc(m.line)}</div>`;
    const enableLabel = o.confirmEnable ? "Confirm: enable Paper Auto" : "Enable Paper Auto";
    const actions = m.loaded ? `<div class="tv-bot-actions">` +
      (m.mode === "PAPER_AUTO" ? `<button type="button" class="tv-bot-btn danger" data-bot="disable"${o.busy ? " disabled" : ""}>Stop New Trades</button>`
                               : `<button type="button" class="tv-bot-btn go" data-bot="enable"${o.busy ? " disabled" : ""}>${esc(enableLabel)}</button>`) +
      `</div>` : "";
    const err = o.error ? `<div class="tv-bot-err" data-bot-error>${esc(o.error)}</div>` : "";
    const rows = [["Paper account", m.env.paper ? `${m.env.simulated ? "simulated paper" : "Alpaca paper"} ${m.env.account_number_masked || ""}`.trim() : `not confirmed${m.env.error ? ` (${words(m.env.error)})` : ""}`],
                  ["Risk controls", m.safety || "—"], ["Open paper positions", `${m.openPositions} of ${m.maxOpen || "—"}`], ["Orders waiting", String(m.pendingEntries)], ["Exits in progress", String(m.pendingExits)],
                  ["Other broker positions", String(m.external)], ["Reconciliation", m.healthy ? "healthy" : "needs a look"], ["Last check", `${m.lastCycle} · ${words(m.lastResult)}`],
                  ["Loop", m.sched.running ? `running · every ${m.sched.interval_s || "—"} s in session` : "not running in this process"],
                  ["Mode changed", m.changedAt ? `${dateOf(m.changedAt)} by ${m.changedBy || "—"}` : "never (default)"]];
    const reasons = m.reasons.length ? `<div class="tv-bot-line">${m.reasons.map((r) => esc(r)).join("<br>")}</div>` : "";
    const details = `<div class="tv-bot-details${o.detailsOpen ? "" : " tv-hidden"}" data-bot-details>${reasons}` +
      rows.map(([k, v]) => `<div class="tv-bot-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") +
      `<div class="tv-ppe-sub" style="margin-top:6px">Recent paper trades</div><ul class="tv-bot-hist" data-bot-history>` +
      (m.recent.length ? m.recent.slice(0, 8).map((h) => `<li>${esc(historyLine(h))}</li>`).join("") : "<li>No paper trades yet.</li>") + `</ul></div>`;
    const toggle = m.loaded ? `<button type="button" class="tv-bot-link" data-bot="details" aria-expanded="${o.detailsOpen ? "true" : "false"}">${o.detailsOpen ? "Hide details" : "View details"}</button>` : "";
    return head + line + actions + err + toggle + details + `<p class="tv-disclaimer">${esc(m.disclaimer)}</p>`;
  }

  function historyLine(h) {
    if (!h) return "";
    if (h.kind === "TRADE") return `${h.symbol} · Closed ${dateOf(h.at)} · ${h.shares} shares · entry ${h.entry || "—"} → exit ${h.exit || "—"} · P/L ${h.pnl || "—"}${h.reason ? ` · ${h.reason}` : ""}`;
    return `${h.symbol} · Not filled ${dateOf(h.at)} · ${h.shares} shares · ${h.reason || words(h.state)}`;
  }

  function cardModel(c, symbol) {
    const loaded = !!c && !!c.key;
    const key = loaded ? c.key : "NONE";
    return { loaded, symbol: (c && c.symbol) || symbol || "", key, icon: loaded ? c.icon : "⚪", label: loaded ? c.label : "No setup", line: (c && c.line) || "No active setup", cls: key.toLowerCase(),
             hasNumbers: !!(c && c.shares != null && c.entry), shares: c && c.shares != null ? String(c.shares) : "—", entry: (c && c.entry) || "—", current: (c && c.current) || "—",
             pnl: c && c.pnl ? `${c.pnl}${c.pnl_pct ? ` (${Number(c.pnl_pct) >= 0 ? "+" : ""}${c.pnl_pct}%)` : ""}` : "—", reason: (c && c.reason) || "", since: c && c.since ? dateOf(c.since) : "",
             state: (c && c.state) || "IDLE", setupState: (c && c.setup_state) || "NONE", details: (c && c.details) || {}, raw: c || null };
  }

  function cardHtml(c, opts) {
    const o = opts || {};
    const m = cardModel(c, o.symbol);
    const head = `<div class="tv-bot-head"><span class="tv-bot-card-state ${esc(m.cls)}" data-card-key="${esc(m.key)}">${esc(m.icon)} ${esc(m.label)}</span></div>`;
    const line = `<div class="tv-bot-line" data-card-line>${esc(m.line)}</div>`;
    const grid = m.hasNumbers ? `<div class="tv-bot-grid">` + [["Shares", m.shares], ["Entry", m.entry], [m.key === "CLOSED" ? "Exit" : "Current", m.current], ["P/L", m.pnl]]
      .map(([k, v]) => `<div class="tv-bot-cell"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") + `</div>` : "";
    const reason = m.reason && m.key !== "IN_TRADE" ? `<div class="tv-bot-line">Reason: ${esc(m.reason)}</div>` : (m.reason && m.key === "IN_TRADE" ? `<div class="tv-dim">${esc(m.reason)}</div>` : "");
    const since = m.since ? `<div class="tv-dem-stamp">since ${esc(m.since)}</div>` : "";
    const ev = (m.details.events || []).slice(-6).map((e) => `<li>${esc(stampOf(e.at, 16))} · ${esc(words(e.kind))}${e.to_state ? ` → ${esc(words(e.to_state))}` : ""}${e.reason ? ` · ${esc(words(e.reason))}` : ""}</li>`).join("");
    const rows = [["Internal state", words(m.state)], ["Setup state", words(m.setupState)]];
    if (m.raw && m.raw.position_id) rows.push(["Position", String(m.raw.position_id).slice(0, 12)]);
    if (m.raw && m.raw.execution_id) rows.push(["Execution", String(m.raw.execution_id).slice(0, 12)]);
    if (m.details.invalidation_price) rows.push(["Invalidation level", m.details.invalidation_price]);
    if (m.details.sessions_held != null) rows.push(["Sessions held", String(m.details.sessions_held)]);
    const details = m.loaded && m.key !== "NONE" ? `<button type="button" class="tv-bot-link" data-card="details" aria-expanded="${o.detailsOpen ? "true" : "false"}">${o.detailsOpen ? "Hide details" : "View Details"}</button>` +
      `<div class="tv-bot-details${o.detailsOpen ? "" : " tv-hidden"}" data-card-details>` + rows.map(([k, v]) => `<div class="tv-bot-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") +
      (ev ? `<ul class="tv-bot-hist">${ev}</ul>` : "") + `</div>` : "";
    return head + line + grid + reason + since + details;
  }

  // ---------------------------------------------------------------- page wiring (browser only) ---------------------------------------
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
  function activeSymbol() {
    const g = typeof window !== "undefined" ? window : globalThis;            // the factory has no `root`: read the page global directly
    const tv = g.TraderView;
    return (tv && tv.state && tv.state.active) || null;
  }
  function cardFor(symbol) {
    const cards = (state.data && state.data.cards) || [];
    return cards.find((c) => c.symbol === symbol) || null;
  }
  function render() {
    if (state.el) {
      state.el.innerHTML = botHtml(state.data, { detailsOpen: state.detailsOpen, confirmEnable: state.confirmEnable, busy: state.busy, error: state.error });
      state.el.dataset.mode = botModel(state.data).key;
    }
    if (state.cardEl) {
      const sym = activeSymbol();
      if (sym !== state.lastSymbol) { state.cardDetailsOpen = false; state.lastSymbol = sym; }
      const c = sym ? cardFor(sym) : null;
      state.cardEl.innerHTML = cardHtml(c, { symbol: sym, detailsOpen: state.cardDetailsOpen });
      state.cardEl.dataset.symbol = sym || "";
      state.cardEl.dataset.state = c ? c.key : "NONE";
    }
  }
  async function refresh() {
    try { state.data = await getJson("/api/execution/status"); state.error = null; }
    catch (e) { state.error = `Paper bot status unavailable (${e.message}).`; }
    render();
    return state.data;
  }
  async function act(path, label) {
    if (state.busy) return;
    state.busy = true; render();
    try { const j = await post(path); state.data = j && j.mode ? j : state.data; state.error = null; }
    catch (e) { state.error = `${label} failed (${e.message}).`; }
    finally { state.busy = false; render(); refresh(); }
  }
  function onClick(e) {
    const b = e.target.closest("[data-bot]");
    if (!b || b.disabled) return;
    const kind = b.dataset.bot;
    if (kind === "details") { state.detailsOpen = !state.detailsOpen; render(); return; }
    if (kind === "disable") return act("/api/execution/auto/disable", "Stop New Trades");
    if (kind === "enable") {
      if (!state.confirmEnable) {                                              // an explicit two-step action: autonomous paper orders start here
        state.confirmEnable = true; render();
        clearTimeout(state.confirmTimer); state.confirmTimer = setTimeout(() => { state.confirmEnable = false; render(); }, 8000);
        return;
      }
      clearTimeout(state.confirmTimer); state.confirmEnable = false;
      return act("/api/execution/auto/enable", "Enable Paper Auto");
    }
  }
  function onCardClick(e) {
    const b = e.target.closest("[data-card]");
    if (!b) return;
    if (b.dataset.card === "details") { state.cardDetailsOpen = !state.cardDetailsOpen; render(); }
  }
  function ensureMounted() {
    if (typeof document === "undefined") return false;
    const safety = document.querySelector(".tv-panel-safety");
    const proposal = document.querySelector(".tv-panel-proposal");
    if (!safety || !proposal) return false;
    if (!document.getElementById("tv-bot")) {
      const sec = document.createElement("section");
      sec.className = "tv-panel tv-panel-bot";
      sec.innerHTML = `<h3>Paper Bot</h3><div id="tv-bot" class="tv-panel-body" aria-live="polite"></div>`;
      safety.insertAdjacentElement("afterend", sec);
      state.el = sec.querySelector("#tv-bot");
      state.el.addEventListener("click", onClick);
    }
    if (!document.getElementById("tv-paper")) {
      const sec = document.createElement("section");
      sec.className = "tv-panel tv-panel-trade";
      sec.innerHTML = `<h3>Paper Trade</h3><div id="tv-paper" class="tv-panel-body"></div>`;
      proposal.insertAdjacentElement("afterend", sec);
      state.cardEl = sec.querySelector("#tv-paper");
      state.cardEl.addEventListener("click", onCardClick);
    }
    return true;
  }
  function mount() {
    if (typeof document === "undefined") return;
    const tick = () => {
      if (ensureMounted()) {
        if (!state.timer) { refresh(); state.timer = setInterval(() => { if (document.getElementById("tv-bot")) refresh(); else { clearInterval(state.timer); state.timer = null; } }, POLL_MS); }
        else render();                                                          // the active ticker may have changed
      }
    };
    tick();
    clearInterval(state.mountTimer);
    state.mountTimer = setInterval(tick, 1000);
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
  }
  return { mount, refresh, render, state, botModel, botHtml, cardModel, cardHtml, historyLine, LABELS, ICONS };
});
