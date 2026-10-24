// Stage 6.1 — FORWARD PAPER VALIDATION. A compact read-only panel in Trader View ("Forward Paper Validation": sessions 7 / 30, healthy, closed
// trades, duplicates, reconciliation issues, paper P/L, strategy status) with advanced metrics under View details, and the separate
// forward_validation.html page (operational and strategy blocks kept apart, sessions, closed paper trades, daily summaries, reports, exports).
// Everything is descriptive: no action, no advice, no profitability claim; the strategy block reads INSUFFICIENT FOR STRATEGY CONCLUSION until
// the sample is meaningful. Loaded as a classic script (window.ForwardValidation) and required by the node tests.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  root.ForwardValidation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const words = (x) => (x == null || x === "" ? "" : String(x).replace(/_/g, " ").toLowerCase());
  const dateOf = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "—");
  const num = (x, d) => (x == null || x === "" || !isFinite(Number(x)) ? (d === undefined ? "—" : d) : String(x));
  const money = (x) => { const n = Number(x); return x == null || x === "" || !isFinite(n) ? "not available" : `${n > 0 ? "+" : ""}${n.toFixed(2)}`; };
  const POLL_MS = 60000;
  const PAGE = "forward_validation.html";
  const SESSION_ICON = { HEALTHY: "🟢", DEGRADED: "🟡", FAILED: "🔴", NOT_RUN: "⚪", OPEN: "🔵" };
  const EDGE_ICON = { INSUFFICIENT_DATA: "⚪", EARLY_NEGATIVE: "🔴", MIXED: "🟡", EARLY_POSITIVE: "🟢" };
  const state = { data: null, el: null, timer: null, mountTimer: null, detailsOpen: false, error: null, page: null };

  // ---------------------------------------------------------------- pure models ------------------------------------------------------
  function summaryModel(d) {
    const loaded = !!d && !!d.sessions && !!d.strategy && !!d.operational;
    const s = loaded ? d.sessions : {}, st = loaded ? d.strategy : {}, op = loaded ? d.operational : {};
    const att = (d && d.attention) || [];
    const needs = !!(d && d.needs_attention);
    const critical = att.some((a) => a && a.critical);
    const edge = st.edge_status || "INSUFFICIENT_DATA";
    const insufficient = !loaded || st.insufficient !== false;
    return { loaded, needsAttention: needs, critical, attention: att, sessionsLabel: loaded ? `${num(s.completed, 0)} / ${num(s.target, 30)}` : "— / —",
             completed: Number(s.completed || 0), target: Number(s.target || 30), healthy: num(s.healthy, 0), degraded: num(s.degraded, 0), failed: num(s.failed, 0), notRun: num(s.not_run, 0),
             inProgress: Number(s.in_progress || 0), closed: num(st.closed_trades, 0), duplicates: num(op.duplicate_broker_orders, 0), reconIssues: num(op.reconciliation_attention, 0),
             reconOpen: num(op.reconciliation_open, 0), pnl: money(st.gross_pnl), unrealized: money(st.unrealized && st.unrealized.unrealized_pnl), edge, edgeLabel: st.edge_label || "Insufficient data",
             edgeIcon: EDGE_ICON[edge] || "⚪", insufficient, insufficientText: st.insufficient_text || (insufficient ? "INSUFFICIENT FOR STRATEGY CONCLUSION" : ""),
             winRate: st.win_rate == null ? "—" : `${st.win_rate} %`, wins: num(st.win_count, 0), losses: num(st.loss_count, 0), flats: num(st.flat_count, 0), avgWin: money(st.average_win),
             avgLoss: money(st.average_loss), expectancy: money(st.expectancy), maxDd: st.drawdown && st.drawdown.max_drawdown != null ? `${money(st.drawdown.max_drawdown)} (${st.drawdown.max_drawdown_pct} %)` : "not available",
             exitReasons: st.exit_reasons || {}, funnel: st.funnel || {}, conversion: st.conversion || {}, opStatus: op.status || "—", brokerErrors: num(op.broker_errors, 0), refreshFailures: num(op.refresh_failures, 0),
             safetyFires: num(op.safety_fires, 0), restarts: num(op.restarts, 0), rejections: num(op.broker_rejections, 0), partials: num(op.partial_fills, 0), liveHost: num(op.live_host_attempts, 0),
             earlyStops: num(op.early_stops, 0), events: d && d.events ? `${num(d.events.total, 0)} / ${num(d.events.target, 100)}` : "— / —", today: d && d.today_session ? d.today_session : null,
             loop: (d && d.loop) || {}, disclaimer: (d && d.disclaimer) || "", raw: d || null };
  }

  function headline(m) {
    if (!m.loaded) return { icon: "⚪", text: "Checking forward validation…", cls: "" };
    if (m.critical) return { icon: "🔴", text: "NEEDS ATTENTION", cls: "attention" };
    if (m.needsAttention) return { icon: "🟡", text: "Needs a look", cls: "warn" };
    if (m.completed >= m.target) return { icon: "🟢", text: "Validation target reached", cls: "ok" };
    return { icon: "🔵", text: "Validation in progress", cls: "" };
  }

  function compactHtml(d, opts) {
    const o = opts || {};
    const m = summaryModel(d);
    const h = headline(m);
    const head = `<div class="tv-bot-head"><span class="tv-fv-state ${h.cls}" data-fv-state="${esc(m.critical ? "ATTENTION" : (m.needsAttention ? "WARN" : (m.loaded ? "OK" : "LOADING")))}">${esc(h.icon)} ${esc(h.text)}</span></div>`;
    const grid = m.loaded ? `<div class="tv-fv-grid" data-fv-grid>` + [["Sessions", m.sessionsLabel], ["Healthy", m.healthy], ["Closed trades", m.closed], ["Duplicates", m.duplicates],
      ["Reconciliation issues", m.reconIssues], ["Paper P/L", m.pnl], ["Strategy status", `${m.edgeIcon} ${m.edgeLabel}`]]
      .map(([k, v]) => `<div class="tv-fv-cell"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") + `</div>` : "";
    const warn = m.loaded && m.insufficient ? `<div class="tv-fv-warn" data-fv-insufficient>${esc(m.insufficientText)}</div>` : "";
    const att = m.attention.length ? `<div class="tv-bot-line tv-fv-att" data-fv-attention>${m.attention.map((a) => esc(`${a.critical ? "critical" : "attention"}: ${words(a.code)}`)).join("<br>")}</div>` : "";
    const err = o.error ? `<div class="tv-bot-err" data-fv-error>${esc(o.error)}</div>` : "";
    const rows = [["Operational status", m.opStatus], ["Sessions degraded / failed / not run", `${m.degraded} / ${m.failed} / ${m.notRun}`], ["Setup events (READY + reviewable)", m.events],
                  ["Win / loss / flat", `${m.wins} / ${m.losses} / ${m.flats}`], ["Win rate", m.winRate], ["Average win / loss", `${m.avgWin} / ${m.avgLoss}`], ["Expectancy per trade (descriptive)", m.expectancy],
                  ["Max drawdown (adjusted equity)", m.maxDd], ["Unrealized paper P/L", m.unrealized], ["Broker rejections / partial fills", `${m.rejections} / ${m.partials}`],
                  ["Broker errors / refresh failures", `${m.brokerErrors} / ${m.refreshFailures}`], ["Safety fires", m.safetyFires], ["Restarts observed", m.restarts],
                  ["Non-paper host attempts / early stops", `${m.liveHost} / ${m.earlyStops}`],
                  ["Observer", m.loop.running ? `running · every ${m.loop.interval_s || "—"} s in session` : "not running in this process"]];
    const details = m.loaded ? `<div class="tv-bot-details${o.detailsOpen ? "" : " tv-hidden"}" data-fv-details>` + rows.map(([k, v]) => `<div class="tv-bot-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") +
      `<div class="tv-fv-link"><a href="${PAGE}" target="_blank" rel="noopener" data-fv-page>Open the Forward Validation page</a></div></div>` : "";
    const toggle = m.loaded ? `<button type="button" class="tv-bot-link" data-fv="details" aria-expanded="${o.detailsOpen ? "true" : "false"}">${o.detailsOpen ? "Hide details" : "View details"}</button>` : "";
    return head + grid + warn + att + err + toggle + details + `<p class="tv-disclaimer">${esc(m.disclaimer)}</p>`;
  }

  // ---------------------------------------------------------------- the separate page ---------------------------------------------------
  function sessionRow(s) {
    return `<tr data-session="${esc(s.trading_date)}"><td>${esc(s.trading_date)}</td><td>${esc(SESSION_ICON[s.session_status] || "⚪")} ${esc(words(s.session_status))}</td>` +
      `<td>${s.market_open_seen ? "yes" : "no"} / ${s.market_close_seen ? "yes" : "no"}</td><td>${s.paper_auto_enabled ? "yes" : "no"}</td>` +
      `<td>${s.account_refresh_healthy == null ? "—" : (s.account_refresh_healthy ? "yes" : "no")} / ${s.cash_flow_healthy == null ? "—" : (s.cash_flow_healthy ? "yes" : "no")} / ${s.reconciliation_healthy == null ? "—" : (s.reconciliation_healthy ? "yes" : "no")}</td>` +
      `<td>${esc(num(s.in_session_samples, 0))}</td><td>${esc((s.notes || []).map(words).join(", ") || "—")}</td></tr>`;
  }

  function tradeRow(t) {
    const es = t.entry_slippage || {}, xs = t.exit_slippage || {}, ex = t.excursions || {};
    return `<tr data-closed="${esc(t.position_id)}"><td>${esc(dateOf(t.closed_at))}</td><td>${esc(t.symbol)}</td><td>${esc(num(t.qty))}</td><td>${esc(num(t.entry_price))}</td><td>${esc(num(t.exit_price))}</td>` +
      `<td>${esc(money(t.realized_pnl != null ? t.realized_pnl : t.gross_pnl))}</td><td>${esc(t.return_pct == null ? "—" : `${t.return_pct} %`)}</td><td>${esc(num(t.holding_sessions, 0))}</td>` +
      `<td>${esc(words(t.exit_reason))}</td><td>${esc(num(es.slippage_bps))} / ${esc(num(xs.slippage_bps))}</td><td>${esc(num(ex.mfe_pnl))} / ${esc(num(ex.mae_pnl))}</td></tr>`;
  }

  function dailyRow(d) {
    const f = d.funnel || {};
    return `<tr data-daily="${esc(d.trading_date)}"><td>${esc(d.trading_date)}</td><td>${esc(words(d.session_status))}</td><td>${esc(num(f.READY, 0))}</td><td>${esc(num(f.REVIEWABLE, 0))}</td>` +
      `<td>${esc(num(f.ENTRY_SUBMITTED, 0))} / ${esc(num(f.ENTRY_FILLED, 0))}</td><td>${esc(num(f.CLOSED, 0))}</td><td>${esc(num(d.duplicates, 0))}</td><td>${esc(num(d.reconciliation_issues, 0))}</td>` +
      `<td>${esc(num(d.safety_fires, 0))}</td><td>${esc(money(d.realized_pnl))}</td></tr>`;
  }

  function pageHtml(d, sessions, closed, dailies, reports) {
    const m = summaryModel(d);
    const h = headline(m);
    const r = reports || {};
    const kv = (rows) => `<div class="fv-kv">${rows.map(([k, v]) => `<div class="fv-kv-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
    const funnel = Object.keys(m.funnel).map((k) => [words(k), num(m.funnel[k], 0)]);
    const conv = Object.keys(m.conversion).map((k) => [words(k), m.conversion[k] == null ? "—" : `${m.conversion[k]} %`]);
    const exits = Object.keys(m.exitReasons).map((k) => [words(k), num(m.exitReasons[k], 0)]);
    const exportLinks = ["sessions", "trades", "daily", "control_events", "samples", "restarts"].map((k) =>
      `<a href="/api/forward-validation/export/${k}" data-fv-export="${k}">${esc(k)} CSV</a> · <a href="/api/forward-validation/export/${k}?format=json">JSON</a>`).join(" | ");
    return `<header class="fv-head"><h1>Forward Paper Validation</h1><div class="fv-state ${h.cls}" data-fv-state="${esc(m.critical ? "ATTENTION" : (m.needsAttention ? "WARN" : (m.loaded ? "OK" : "LOADING")))}">${esc(h.icon)} ${esc(h.text)}</div>` +
      `<p class="fv-sub">Alpaca PAPER only · descriptive · nothing here is advice or a performance claim · <a href="/">back to the dashboard</a></p></header>` +
      (m.attention.length ? `<section class="fv-card fv-attention" data-fv-attention><h2>Needs attention</h2><ul>${m.attention.map((a) => `<li>${esc(a.critical ? "critical" : "attention")}: ${esc(words(a.code))} — ${esc(a.text || "")}${a.count > 1 ? ` (${esc(a.count)})` : ""}</li>`).join("")}</ul></section>` : "") +
      `<div class="fv-two"><section class="fv-card" data-fv-operational><h2>Operational</h2>` + kv([["Sessions", m.sessionsLabel], ["Healthy / degraded / failed / not run", `${m.healthy} / ${m.degraded} / ${m.failed} / ${m.notRun}`],
        ["In progress", m.inProgress], ["Duplicate broker orders", m.duplicates], ["Reconciliation issues (open)", `${m.reconIssues} (${m.reconOpen})`], ["Broker rejections / partial fills", `${m.rejections} / ${m.partials}`],
        ["Broker errors", m.brokerErrors], ["Refresh failures", m.refreshFailures], ["Safety fires", m.safetyFires], ["Restarts", m.restarts], ["Non-paper host attempts", m.liveHost], ["Early stops", m.earlyStops], ["Operational status", m.opStatus]]) + `</section>` +
      `<section class="fv-card" data-fv-strategy><h2>Strategy (descriptive)</h2>` + (m.insufficient ? `<div class="fv-warn" data-fv-insufficient>${esc(m.insufficientText)}</div>` : "") +
      kv([["Strategy status", `${m.edgeIcon} ${m.edgeLabel}`], ["Setup events (READY + reviewable)", m.events], ["Closed trades (win / loss / flat)", `${m.closed} (${m.wins} / ${m.losses} / ${m.flats})`], ["Win rate", m.winRate],
          ["Average win / loss", `${m.avgWin} / ${m.avgLoss}`], ["Expectancy per trade", m.expectancy], ["Gross paper P/L", m.pnl], ["Unrealized paper P/L", m.unrealized], ["Max drawdown (adjusted equity)", m.maxDd]]) + `</section></div>` +
      `<div class="fv-two"><section class="fv-card"><h2>Funnel (all sessions)</h2>${kv(funnel)}</section><section class="fv-card"><h2>Conversion rates</h2>${kv(conv)}<h2>Exit reasons</h2>${kv(exits)}</section></div>` +
      `<section class="fv-card"><h2>Sessions</h2><table class="fv-table" data-fv-sessions><thead><tr><th>Date</th><th>Health</th><th>Open / close seen</th><th>Paper Auto</th><th>Account / cash-flow / reconciliation</th><th>Samples</th><th>Notes</th></tr></thead>` +
      `<tbody>${(sessions || []).map(sessionRow).join("") || '<tr><td colspan="7">No session recorded yet.</td></tr>'}</tbody></table></section>` +
      `<section class="fv-card"><h2>Closed paper trades</h2><table class="fv-table" data-fv-closed><thead><tr><th>Closed</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Return</th><th>Sessions</th><th>Exit reason</th><th>Slippage bps (entry / exit)</th><th>MFE / MAE</th></tr></thead>` +
      `<tbody>${(closed || []).map(tradeRow).join("") || '<tr><td colspan="11">No closed paper trade yet.</td></tr>'}</tbody></table></section>` +
      `<section class="fv-card"><h2>Daily summaries</h2><table class="fv-table" data-fv-daily><thead><tr><th>Date</th><th>Health</th><th>READY</th><th>Reviewable</th><th>Entries / fills</th><th>Closed</th><th>Duplicates</th><th>Reconciliation</th><th>Safety fires</th><th>Realized P/L</th></tr></thead>` +
      `<tbody>${(dailies || []).map(dailyRow).join("") || '<tr><td colspan="10">No daily summary yet.</td></tr>'}</tbody></table></section>` +
      `<div class="fv-two"><section class="fv-card"><h2>Daily report</h2><pre class="fv-pre" data-fv-report-daily>${esc(r.daily || "")}</pre></section><section class="fv-card"><h2>Weekly report</h2><pre class="fv-pre" data-fv-report-weekly>${esc(r.weekly || "")}</pre></section></div>` +
      `<section class="fv-card"><h2>Exports</h2><p class="fv-exports" data-fv-exports>${exportLinks}</p></section>` +
      `<p class="fv-disclaimer">${esc(m.disclaimer)}</p>`;
  }

  // ---------------------------------------------------------------- page wiring (browser only) ---------------------------------------
  async function getJson(path) {
    const r = await fetch(path);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
    return j;
  }
  async function getText(path) {
    const r = await fetch(path);
    return r.ok ? r.text() : "";
  }
  function render() {
    if (state.el) {
      state.el.innerHTML = compactHtml(state.data, { detailsOpen: state.detailsOpen, error: state.error });
      state.el.dataset.state = summaryModel(state.data).loaded ? (summaryModel(state.data).critical ? "ATTENTION" : "OK") : "LOADING";
    }
  }
  async function refresh() {
    try { state.data = await getJson("/api/forward-validation"); state.error = null; }
    catch (e) { state.error = `Forward validation status unavailable (${e.message}).`; }
    render();
    return state.data;
  }
  function onClick(e) {
    const b = e.target.closest("[data-fv]");
    if (!b) return;
    if (b.dataset.fv === "details") { state.detailsOpen = !state.detailsOpen; render(); }
  }
  function ensureMounted() {
    if (typeof document === "undefined") return false;
    const anchor = document.querySelector(".tv-panel-bot") || document.querySelector(".tv-panel-safety");
    if (!anchor) return false;
    if (!document.getElementById("tv-fv")) {
      const sec = document.createElement("section");
      sec.className = "tv-panel tv-panel-fv";
      sec.innerHTML = `<h3>Forward Paper Validation</h3><div id="tv-fv" class="tv-panel-body" aria-live="polite"></div>`;
      anchor.insertAdjacentElement("afterend", sec);
      state.el = sec.querySelector("#tv-fv");
      state.el.addEventListener("click", onClick);
    }
    return true;
  }
  async function renderPage() {
    const el = document.getElementById("fv-page");
    if (!el) return;
    try {
      const [d, s, c, dl, rd, rw] = await Promise.all([getJson("/api/forward-validation"), getJson("/api/forward-validation/sessions?limit=60"), getJson("/api/forward-validation/closed?limit=200"),
                                                       getJson("/api/forward-validation/daily?limit=60"), getText("/api/forward-validation/report/daily"), getText("/api/forward-validation/report/weekly")]);
      state.page = d;
      el.innerHTML = pageHtml(d, s.sessions, c.closed, dl.daily, { daily: rd, weekly: rw });
    } catch (e) {
      el.innerHTML = `<p class="fv-warn" data-fv-error>Forward validation data unavailable (${esc(e.message)}).</p>`;
    }
  }
  function mount() {
    if (typeof document === "undefined") return;
    if (document.getElementById("fv-page")) { renderPage(); setInterval(renderPage, POLL_MS); return; }
    const tick = () => {
      if (ensureMounted()) {
        if (!state.timer) { refresh(); state.timer = setInterval(() => { if (document.getElementById("tv-fv")) refresh(); else { clearInterval(state.timer); state.timer = null; } }, POLL_MS); }
      }
    };
    tick();
    clearInterval(state.mountTimer);
    state.mountTimer = setInterval(tick, 1000);
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
  }
  return { mount, refresh, render, renderPage, state, summaryModel, headline, compactHtml, pageHtml, sessionRow, tradeRow, dailyRow, POLL_MS, PAGE };
});
