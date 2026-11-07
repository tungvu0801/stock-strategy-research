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
    // Wave 2 PR C (C-81, C-85, C-37, C-89): the run block, the all-time block and the promotion view are the server's; this only formats them (no sum, no decision).
    // The edge label is the run's (design 7.2), the all-time strategy block is informational, and every P/L sum is the server's verified-only one, labelled (Rule F).
    const run = loaded && d.run ? d.run : null, rn = run || {}, rs = rn.strategy || {}, rss = rn.sessions || {}, rdd = rn.drawdown || {}, rr = rn.readiness || {}, rt = rn.trades || {};
    const at = (loaded && d.all_time) || {}, pr = (loaded && d.promotion) || {}, fe = (loaded && d.forward_evidence) || {};
    const edge = rs.edge_status || "INSUFFICIENT_DATA";
    const insufficient = !loaded || edge === "INSUFFICIENT_DATA";
    const cnt = (x) => (x == null || x === "" || !isFinite(Number(x)) ? 0 : Number(x));
    const ruleF = (v, unverified, closed, label) => (v != null && v !== "" ? `${money(v)}${label || cnt(unverified) ? ` · ${label || `partial (${cnt(unverified)} unverified)`}` : ""}`
      : cnt(unverified) ? `not available (${cnt(unverified)} unverified)` : cnt(closed) ? "not available" : "—");
    const unpriced = (e) => e == null || e === "" || !isFinite(Number(e)) || Number(e) <= 0;
    const ur = st.unrealized || {}, urRows = Array.isArray(ur.positions) ? ur.positions : [];
    const unrealized = urRows.some((p) => !p || p.unrealized_pnl == null || unpriced(p.entry)) || (ur.unrealized_pnl == null && cnt(ur.open_positions) > 0) ? "unknown" : money(ur.unrealized_pnl);
    const runDefined = !!run && run.defined !== false && !!run.run_id;
    return { loaded, needsAttention: needs, critical, attention: att, sessionsLabel: loaded ? `${num(s.completed, 0)} / ${num(s.target, 30)}` : "— / —",
             completed: Number(s.completed || 0), target: Number(s.target || 30), healthy: num(s.healthy, 0), degraded: num(s.degraded, 0), failed: num(s.failed, 0), notRun: num(s.not_run, 0),
             inProgress: Number(s.in_progress || 0), closed: `${num(st.closed_trades, 0)}${cnt(st.unverified) ? ` verified · ${cnt(st.unverified)} unverified` : ""}`, duplicates: num(op.duplicate_broker_orders, 0),
             reconIssues: num(op.reconciliation_attention, 0), reconOpen: num(op.reconciliation_open, 0), pnl: ruleF(st.gross_pnl, st.unverified, st.closed_trades), unrealized, edge, edgeLabel: rs.edge_label || "Insufficient data",
             edgeIcon: EDGE_ICON[edge] || "⚪", insufficient, insufficientText: insufficient ? rs.insufficient_text || st.insufficient_text || "INSUFFICIENT FOR STRATEGY CONCLUSION" : "", edgeReasons: Array.isArray(rs.reasons) ? rs.reasons : [],
             run, runDefined, runReady: rr.status === "READY", runTargetMet: rss.target_met === true, allTimeReady: at.status === "READY", promotionEligible: pr.eligible === true, promotionText: pr.text || "",
             runText: rr.text || (runDefined && rr.status ? `Run ${run.run_id}: ${rr.status}` : rn.headline || "Forward run: not available"),
             allTimeText: at.text || (at.status ? `All-time record: ${at.status}` : "All-time record: not available"), runSessionsLabel: rss.label || "— / —", runSide: rs.side_text || "—",
             runClosed: num(rs.closed_trades, 0), runPnl: ruleF(rs.gross_pnl, rt.unverified, rs.closed_trades, rs.pnl_label), runExpectancy: money(rs.expectancy), runWinRate: rs.win_rate == null ? "—" : `${rs.win_rate} %`,
             runDd: rdd.status === "KNOWN" && rdd.max_drawdown_pct != null && rdd.max_drawdown_pct !== "" ? `${rdd.max_drawdown_pct} %` : `unknown${rdd.text ? ` (${rdd.text})` : ""}`,
             cohorts: Array.isArray(fe.cohorts) ? fe.cohorts : [],
             winRate: st.win_rate == null ? "—" : `${st.win_rate} %`, wins: num(st.win_count, 0), losses: num(st.loss_count, 0), flats: num(st.flat_count, 0), avgWin: money(st.average_win),
             avgLoss: money(st.average_loss), expectancy: money(st.expectancy), maxDd: st.drawdown && st.drawdown.max_drawdown != null ? `${money(st.drawdown.max_drawdown)} (${st.drawdown.max_drawdown_pct} %)` : "not available",
             exitReasons: st.exit_reasons || {}, funnel: st.funnel || {}, conversion: st.conversion || {}, opStatus: op.status || "—", brokerErrors: num(op.broker_errors, 0), refreshFailures: num(op.refresh_failures, 0),
             safetyFires: num(op.safety_fires, 0), restarts: num(op.restarts, 0), rejections: num(op.broker_rejections, 0), partials: num(op.partial_fills, 0), liveHost: num(op.live_host_attempts, 0),
             earlyStops: num(op.early_stops, 0), events: d && d.events ? `${num(d.events.total, 0)} / ${num(d.events.target, 100)}` : "— / —", today: d && d.today_session ? d.today_session : null,
             loop: (d && d.loop) || {}, disclaimer: (d && d.disclaimer) || "", raw: d || null };
  }

  function headline(m) {
    // Wave 2 PR C (C-85, C-37, N-03, N-11): the run line and the all-time line always travel together, with the promotion view beside them; "Validation target
    // reached" needs the run's target_met, the run READY and no critical attention (the all-time session count alone never reaches it)
    const lines = { run: m.loaded ? m.runText : "", allTime: m.loaded ? m.allTimeText : "", promotion: m.loaded ? m.promotionText : "" };
    if (!m.loaded) return { icon: "⚪", text: "Checking forward validation…", cls: "", ...lines };
    if (m.critical) return { icon: "🔴", text: "NEEDS ATTENTION", cls: "attention", ...lines };
    if (m.needsAttention) return { icon: "🟡", text: "Needs a look", cls: "warn", ...lines };
    if (m.runTargetMet && m.runReady && !m.critical) return { icon: "🟢", text: "Validation target reached", cls: "ok", ...lines };
    return { icon: "🔵", text: "Validation in progress", cls: "", ...lines };
  }

  function compactHtml(d, opts) {
    const o = opts || {};
    const m = summaryModel(d);
    const h = headline(m);
    const head = `<div class="tv-bot-head"><span class="tv-fv-state ${h.cls}" data-fv-state="${esc(m.critical ? "ATTENTION" : (m.needsAttention ? "WARN" : (m.loaded ? "OK" : "LOADING")))}">${esc(h.icon)} ${esc(h.text)}</span></div>`;
    // Wave 2 PR C (C-37, N-03, N-10, N-11): the run line and the all-time line together, the promotion view beside them, and the suspended-run banner
    const blocks = m.loaded ? `<div class="tv-bot-line" data-fv-run-line>${esc(h.run)}</div><div class="tv-bot-line" data-fv-alltime-line>${esc(h.allTime)}</div>` +
      (h.promotion ? `<div class="tv-bot-line" data-fv-promotion>${esc(h.promotion)}</div>` : "") + runBanner(m.run) : "";
    const grid = m.loaded ? `<div class="tv-fv-grid" data-fv-grid>` + [["Sessions", m.sessionsLabel], ["Healthy", m.healthy], ["Closed trades", m.closed], ["Duplicates", m.duplicates],
      ["Reconciliation issues", m.reconIssues], ["Paper P/L", m.pnl], ["Strategy status", `${m.edgeIcon} ${m.edgeLabel}`]]
      .map(([k, v]) => `<div class="tv-fv-cell"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") + `</div>` : "";
    const warn = m.loaded && m.insufficient ? `<div class="tv-fv-warn" data-fv-insufficient>${esc(m.insufficientText)}</div>` : "";
    const att = m.attention.length ? `<div class="tv-bot-line tv-fv-att" data-fv-attention>${m.attention.map((a) => esc(`${a.critical ? "critical" : "attention"}: ${words(a.code)}`)).join("<br>")}</div>` : "";
    const err = o.error ? `<div class="tv-bot-err" data-fv-error>${esc(o.error)}</div>` : "";
    const rows = [["Operational status", m.opStatus], ["Sessions degraded / failed / not run", `${m.degraded} / ${m.failed} / ${m.notRun}`], ["Forward run sessions", m.runSessionsLabel],
                  ["Forward run trades", m.runSide], ["Setup events (READY + reviewable)", m.events],
                  ["Win / loss / flat", `${m.wins} / ${m.losses} / ${m.flats}`], ["Win rate", m.winRate], ["Average win / loss", `${m.avgWin} / ${m.avgLoss}`], ["Expectancy per trade (descriptive)", m.expectancy],
                  ["Max drawdown (forward run · bot only)", m.runDd], ["Max drawdown (adjusted equity · informational)", m.maxDd], ["Unrealized paper P/L", m.unrealized], ["Broker rejections / partial fills", `${m.rejections} / ${m.partials}`],
                  ["Broker errors / refresh failures", `${m.brokerErrors} / ${m.refreshFailures}`], ["Safety fires", m.safetyFires], ["Restarts observed", m.restarts],
                  ["Non-paper host attempts / early stops", `${m.liveHost} / ${m.earlyStops}`],
                  ["Observer", m.loop.running ? `running · every ${m.loop.interval_s || "—"} s in session` : "not running in this process"]];
    const details = m.loaded ? `<div class="tv-bot-details${o.detailsOpen ? "" : " tv-hidden"}" data-fv-details>` + rows.map(([k, v]) => `<div class="tv-bot-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") +
      `<div class="tv-fv-link"><a href="${PAGE}" target="_blank" rel="noopener" data-fv-page>Open the Forward Validation page</a></div></div>` : "";
    const toggle = m.loaded ? `<button type="button" class="tv-bot-link" data-fv="details" aria-expanded="${o.detailsOpen ? "true" : "false"}">${o.detailsOpen ? "Hide details" : "View details"}</button>` : "";
    return head + blocks + grid + warn + att + err + toggle + details + `<p class="tv-disclaimer">${esc(m.disclaimer)}</p>`;
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
    // Wave 2 PR C (C-82, Rule F): the server's `verified` flag decides; an unverified trade reads "unverified" (never a number), a row without the flag "unknown"
    const pl = t.verified === true ? money(t.realized_pnl != null ? t.realized_pnl : t.gross_pnl) : t.verified === false ? "unverified" : "unknown";
    return `<tr data-closed="${esc(t.position_id)}"><td>${esc(dateOf(t.closed_at))}</td><td>${esc(t.symbol)}</td><td>${esc(num(t.qty))}</td><td>${esc(num(t.entry_price))}</td><td>${esc(num(t.exit_price))}</td>` +
      `<td>${esc(pl)}</td><td>${esc(t.return_pct == null ? "—" : `${t.return_pct} %`)}</td><td>${esc(num(t.holding_sessions, 0))}</td>` +
      `<td>${esc(words(t.exit_reason))}</td><td>${esc(num(es.slippage_bps))} / ${esc(num(xs.slippage_bps))}</td><td>${esc(num(ex.mfe_pnl))} / ${esc(num(ex.mae_pnl))}</td></tr>`;
  }

  function dailyRow(d) {
    const f = d.funnel || {};
    // Wave 2 PR C (C-82, Rule F): the server's verified-only day total by its pnl_status - "—" without trades, "partial (n unverified)", "not available"; never a browser sum
    const u = isFinite(Number(d.unverified)) ? Number(d.unverified || 0) : 0, v = d.realized_pnl != null && d.realized_pnl !== "" ? money(d.realized_pnl) : null;
    const pl = d.pnl_status === "NO_TRADES" ? "—" : d.pnl_status === "OK" && v != null ? v
      : d.pnl_status === "UNVERIFIED_PRESENT" ? (v != null ? `${v} · partial (${u} unverified)` : `not available (${u} unverified)`) : "not available";
    return `<tr data-daily="${esc(d.trading_date)}"><td>${esc(d.trading_date)}</td><td>${esc(words(d.session_status))}</td><td>${esc(num(f.READY, 0))}</td><td>${esc(num(f.REVIEWABLE, 0))}</td>` +
      `<td>${esc(num(f.ENTRY_SUBMITTED, 0))} / ${esc(num(f.ENTRY_FILLED, 0))}</td><td>${esc(num(f.CLOSED, 0))}</td><td>${esc(num(d.duplicates, 0))}</td><td>${esc(num(d.reconciliation_issues, 0))}</td>` +
      `<td>${esc(num(d.safety_fires, 0))}</td><td>${esc(pl)}</td></tr>`;
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
    // Wave 2 PR C (C-37, C-86, C-87, N-01, N-09…N-11): the run line and the all-time line together under the state, the promotion view, the run header (with the
    // suspended banner) and the per-cohort table; every server text is escaped
    const runLines = m.loaded ? `<div class="fv-state" data-fv-run-line>${esc(h.run)}</div><div class="fv-state" data-fv-alltime-line>${esc(h.allTime)}</div>` +
      (h.promotion ? `<div class="fv-sub" data-fv-promotion>${esc(h.promotion)}</div>` : "") : "";
    const cohorts = m.loaded ? `<section class="fv-card"><h2>Forward evidence per cohort</h2><table class="fv-table" data-fv-cohorts><thead><tr><th>Cohort</th><th>Since</th><th>Healthy sessions</th><th>Closed trades</th>` +
      `<th>Status</th><th>Expectancy / win rate</th></tr></thead><tbody>${m.cohorts.map(cohortRow).join("") || '<tr><td colspan="6">No cohort recorded yet.</td></tr>'}</tbody></table></section>` : "";
    return `<header class="fv-head"><h1>Forward Paper Validation</h1><div class="fv-state ${h.cls}" data-fv-state="${esc(m.critical ? "ATTENTION" : (m.needsAttention ? "WARN" : (m.loaded ? "OK" : "LOADING")))}">${esc(h.icon)} ${esc(h.text)}</div>` +
      runLines + `<p class="fv-sub">Alpaca PAPER only · descriptive · nothing here is advice or a performance claim · <a href="/">back to the dashboard</a></p></header>` + (m.loaded ? runHeader(m.run) : "") +
      (m.attention.length ? `<section class="fv-card fv-attention" data-fv-attention><h2>Needs attention</h2><ul>${m.attention.map((a) => `<li>${esc(a.critical ? "critical" : "attention")}: ${esc(words(a.code))} — ${esc(a.text || "")}${a.count > 1 ? ` (${esc(a.count)})` : ""}</li>`).join("")}</ul></section>` : "") +
      `<div class="fv-two"><section class="fv-card" data-fv-operational><h2>Operational</h2>` + kv([["Sessions", m.sessionsLabel], ["Healthy / degraded / failed / not run", `${m.healthy} / ${m.degraded} / ${m.failed} / ${m.notRun}`],
        ["In progress", m.inProgress], ["Duplicate broker orders", m.duplicates], ["Reconciliation issues (open)", `${m.reconIssues} (${m.reconOpen})`], ["Broker rejections / partial fills", `${m.rejections} / ${m.partials}`],
        ["Broker errors", m.brokerErrors], ["Refresh failures", m.refreshFailures], ["Safety fires", m.safetyFires], ["Restarts", m.restarts], ["Non-paper host attempts", m.liveHost], ["Early stops", m.earlyStops], ["Operational status", m.opStatus]]) + `</section>` +
      `<section class="fv-card" data-fv-strategy><h2>Strategy (descriptive)</h2>` + (m.insufficient ? `<div class="fv-warn" data-fv-insufficient>${esc(m.insufficientText)}</div>` : "") +
      kv([["Strategy status", `${m.edgeIcon} ${m.edgeLabel}`], ["Forward run status reasons", m.edgeReasons.join("; ") || "—"], ["Forward run trades", m.runSide], ["Forward run closed trades", m.runClosed],
          ["Forward run expectancy per trade", m.runExpectancy], ["Forward run win rate", m.runWinRate], ["Forward run paper P/L", m.runPnl], ["Max drawdown (forward run · bot only)", m.runDd],
          ["Setup events (READY + reviewable)", m.events], ["Closed trades (win / loss / flat)", `${m.closed} (${m.wins} / ${m.losses} / ${m.flats})`], ["Win rate", m.winRate],
          ["Average win / loss", `${m.avgWin} / ${m.avgLoss}`], ["Expectancy per trade", m.expectancy], ["Gross paper P/L", m.pnl], ["Unrealized paper P/L", m.unrealized], ["Max drawdown (adjusted equity · informational)", m.maxDd]]) + `</section></div>` +
      `<div class="fv-two"><section class="fv-card"><h2>Funnel (all sessions)</h2>${kv(funnel)}</section><section class="fv-card"><h2>Conversion rates</h2>${kv(conv)}<h2>Exit reasons</h2>${kv(exits)}</section></div>` + cohorts +
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
  function runHeader(run) {
    // Wave 2 PR C (C-86, N-02, N-04, N-09): the run header from the server's run block - run id, since, frozen hash, cohort, the accepted blockers, the acknowledged
    // prior invalid days by date, the strategy-session label and the suspended banner; nothing is derived here
    if (!run) return "";
    if (run.defined === false || !run.run_id) return `<section class="fv-card" data-fv-run-header data-fv-run-defined="0"><h2>Forward run</h2><p>${esc(run.headline || "No forward run defined")}</p></section>`;
    const hash = run.frozen_hash ? String(run.frozen_hash) : "";
    const list = (x) => (Array.isArray(x) ? x : []);
    const prior = list(run.prior_invalid_days).map((p) => `${(p && p.date) || "—"}${p && p.session_status ? ` (${words(p.session_status)}${list(p.notes).length ? ` · ${list(p.notes).map(words).join(", ")}` : ""})` : ""}`).join("; ");
    const rows = [["Run", run.run_id], ["Since", dateOf(run.started_at)], ["First session", run.first_session_date || "—"], ["Frozen hash", hash ? `${hash.slice(0, 12)}${hash.length > 12 ? "…" : ""}` : "—"],
                  ["Cohort", run.cohort_id || "—"], ["Accepted blockers", list(run.accepted_blockers).join(", ") || "none"], ["Acknowledged prior invalid days", prior || "none"],
                  ["Strategy sessions", (run.sessions && run.sessions.label) || "—"]];
    if (run.ended_at) rows.push(["Ended", `${dateOf(run.ended_at)}${run.end_reason ? ` · ${run.end_reason}` : ""}`]);
    return `<section class="fv-card" data-fv-run-header data-fv-run-defined="1" data-fv-run="${esc(run.run_id)}" data-fv-run-hash="${esc(hash)}"><h2>Forward run ${esc(run.run_id)}</h2>` +
      (run.headline ? `<p class="fv-sub">${esc(run.headline)}</p>` : "") + runBanner(run) +
      `<div class="fv-kv">${rows.map(([k, v]) => `<div class="fv-kv-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div></section>`;
  }
  function runBanner(run) {
    // Wave 2 PR C (N-10, C-13): a suspended run stays visible with a banner (its code and first-evidence time); empty when the run is not suspended
    if (!run || run.suspended !== true) return "";
    const s = run.suspension || {};
    return `<div class="fv-warn tv-fv-warn" data-fv-run-banner="${esc(s.code || "UNKNOWN")}">Run ${esc(run.run_id || "—")} suspended · ${esc(s.code || "code unknown")} · first evidence ${esc(dateOf(s.first_evidence_at))}` +
      `${s.at ? ` · recorded ${esc(dateOf(s.at))}` : ""}${run.t_stop ? ` · statistics up to ${esc(dateOf(run.t_stop))}` : ""}</div>`;
  }
  function cohortRow(c) {
    // Wave 2 PR C (C-87, N-01, C-80): one per-cohort row; only the active run's cohort (the server's forward_run flag) carries an edge label - every other cohort
    // reads "not a forward run" and no P&L edge (expectancy, win rate) is ever rendered for it
    const x = c || {}, fr = x.forward_run === true;
    return `<tr data-fv-cohort="${esc(x.cohort_id)}" data-fv-forward-run="${fr ? "1" : "0"}"><td>${esc(x.label || x.cohort_id || "—")}</td><td>${esc(dateOf(x.started_at))}</td>` +
      `<td>${esc(num(x.healthy_sessions, 0))} / ${esc(num(x.healthy_target))}</td><td>${esc(num(x.closed_trades, 0))}</td><td>${esc(fr ? `forward run · ${x.label_text || "Insufficient data"}` : "not a forward run")}</td>` +
      `<td>${esc(fr ? `${money(x.expectancy)} / ${x.win_rate == null ? "—" : `${x.win_rate} %`}` : "—")}</td></tr>`;
  }
  return { mount, refresh, render, renderPage, state, summaryModel, headline, compactHtml, pageHtml, sessionRow, tradeRow, dailyRow, POLL_MS, PAGE, cohortRow, runBanner, runHeader };
});
