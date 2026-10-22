// setup_alerts.js — Stage 5.8: the persisted setup-state alerts (NONE → WATCH, WATCH → READY FOR REVIEW, READY → WATCH, → INVALIDATED)
// listed in the Alerts tab. Read-only list plus the existing read-acknowledgement pattern (mark read / mark all read) and "Open in
// Trader View". No trade action exists here: no Buy, no Sell, no order, no preview.
(function () {
  "use strict";
  const host = document.getElementById("setup-alerts");
  if (!host) return;
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LABELS = { NONE: "NO ACTIVE SETUP", WATCH: "WATCH", READY: "READY FOR REVIEW", INVALIDATED: "INVALIDATED" };
  const CLS = { WATCH: "watch", READY: "ready", INVALIDATED: "invalidated", NONE: "" };
  let data = null, busy = false, error = "";

  async function load() {
    try {
      const r = await fetch("/api/setups/alerts?limit=30");
      data = await r.json();
      error = r.ok ? "" : (data.message || `HTTP ${r.status}`);
    } catch (e) { error = e.message || "unavailable"; }
    draw();
  }
  async function markRead(id) {
    busy = true; draw();
    try { await fetch(id ? `/api/setups/alerts/${encodeURIComponent(id)}/read` : "/api/setups/alerts/read-all", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
    catch (e) { error = e.message || "could not mark read"; }
    busy = false;
    await load();
  }
  function openTrader(sym) {
    const tab = document.querySelector('.tab-btn[data-tab="trader"]');
    if (tab) tab.click();
    if (window.TraderView && typeof window.TraderView.switchTo === "function") window.TraderView.switchTo(sym);
  }
  function item(a) {
    const label = LABELS[a.to_state] || a.to_state;
    const when = String(a.created_at || "").replace("T", " ").slice(0, 16) + " UTC";
    return `<li class="sa-item${a.read_at ? " sa-read" : ""}" data-sa-alert="${esc(a.alert_id)}">
      <div class="sa-row"><span class="sa-state ${CLS[a.to_state] || ""}">${esc(label)}</span><strong class="sa-sym">${esc(a.symbol)}</strong><span class="sa-when">${esc(when)}</span></div>
      <div class="sa-body">${esc(a.body).replace(/\n/g, "<br>")}</div>
      <div class="sa-actions"><button type="button" class="sa-btn" data-sa="open" data-sym="${esc(a.symbol)}">Open in Trader View</button>` +
      `${a.read_at ? "" : `<button type="button" class="sa-btn sa-link" data-sa="read" data-id="${esc(a.alert_id)}"${busy ? " disabled" : ""}>Mark read</button>`}</div></li>`;
  }
  function draw() {
    const alerts = (data && data.alerts) || [];
    const unread = data ? data.unread : 0;
    host.innerHTML = `<div class="sa">
      <div class="sa-head"><h3>Setup alerts <span class="sa-count">${esc(unread)} unread</span></h3>` +
      `${alerts.some((a) => !a.read_at) ? `<button type="button" class="sa-btn sa-link" data-sa="readall"${busy ? " disabled" : ""}>Mark all read</button>` : ""}</div>
      <div class="sa-hint">Deterministic setup-state transitions for watchlist tickers (WATCH · READY FOR REVIEW · INVALIDATED). READY means the review threshold was met — not an entry signal. ${esc((data && data.disclaimer) || "")}</div>
      ${error ? `<div class="sa-hint sa-error">Setup alerts unavailable: ${esc(error)}</div>` : ""}
      ${alerts.length ? `<ul class="sa-list">${alerts.map(item).join("")}</ul>` : `<div class="sa-hint">No setup alerts yet.</div>`}
    </div>`;
  }
  host.addEventListener("click", (e) => {
    const b = e.target.closest("[data-sa]");
    if (!b) return;
    if (b.dataset.sa === "open") openTrader(b.dataset.sym);
    else if (b.dataset.sa === "read") markRead(b.dataset.id);
    else if (b.dataset.sa === "readall") markRead(null);
  });
  window.SetupAlerts = { refresh: load, get state() { return { data, error, busy }; } };
  load();
  setInterval(load, 30000);
})();
