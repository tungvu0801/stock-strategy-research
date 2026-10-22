// trader_view.js — Stage 5.5 TRADER VIEW (Stage 5.6 PPE, Stage 5.6.1 UI polish): a read-only terminal tab. READ ONLY · NO ORDER PATH · PREVIEW NOT CONNECTED.
//
// One WebSocket (/api/terminal/stream) opened the first time the tab is shown; exactly one detailed subscription at a time
// (unsubscribe old → subscribe new on every switch, one re-subscribe after a reconnect, unsubscribe when the tab is left so the
// server polls nothing). Every symbol-bound message is dropped unless it carries the ACTIVE ticker (TraderCore.acceptsMessage), so a
// late message for a previous ticker can never paint the screen; on a switch the position / PPE / evidence panels show a loading
// state for the NEW ticker until its own data arrives. Candles: TradingView Lightweight Charts served locally from frontend/vendor
// (no CDN), with a plain canvas fallback. Prepare Buy / Prepare Sell are disabled buttons (no order path); Add Alert is a placeholder;
// Research navigates to the stored evidence workspace. This file never sends a POST and never names an order endpoint.
// Pure rendering helpers live in trader_render.js (window.TraderRender) so node tests cover the exact markup.
(function () {
  "use strict";
  const tabEl = document.getElementById("tab-trader");
  const body = document.getElementById("tv-body");
  const core = window.TraderCore;
  const R = window.TraderRender;
  if (!tabEl || !body || !core || !R) return;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const v = (x, fallback) => (x == null || x === "" ? (fallback == null ? "—" : fallback) : x);
  const $ = (id) => document.getElementById(id);
  const WS_PATH = "/api/terminal/stream";
  const HISTORY = 240;
  const MAX_RECENT = 8;
  const ACTION_TEXT = {
    prepare_buy: "PREVIEW NOT CONNECTED — Prepare Buy has no order path in Trader View.",
    prepare_sell: "PREVIEW NOT CONNECTED — Prepare Sell has no order path in Trader View.",
    research: "Deep research integration coming in the next step.",
    add_alert: "Add Alert is not connected in Trader View. The Alerts tab shows the rule-based alerts.",
  };
  const DEFAULT_ACTIONS = [
    { id: "prepare_buy", label: "Prepare Buy", enabled: false, status: "PREVIEW_NOT_CONNECTED", message: ACTION_TEXT.prepare_buy },
    { id: "prepare_sell", label: "Prepare Sell", enabled: false, status: "PREVIEW_NOT_CONNECTED", message: ACTION_TEXT.prepare_sell },
    { id: "research", label: "Research", enabled: true, status: "NAVIGATE_ONLY", message: "Opens the stored evidence workspace." },
    { id: "add_alert", label: "Add Alert", enabled: false, status: "PLACEHOLDER", message: ACTION_TEXT.add_alert },
  ];
  const PANELS = [["tv-position", "POSITION"], ["tv-ppe", "PPE"], ["tv-demand", "DEMAND"], ["tv-setup", "SETUP"], ["tv-proposal", "TRADE REVIEW"], ["tv-evidence", "EVIDENCE"]];
  const seenAlerts = new Set();                                                 // setup_alert ids already toasted (no duplicate toast)

  const st = { booted: false, visible: false, active: null, mode: "", source: "", symbols: { watchlist: [], demo: ["AAPL", "NVDA", "SNDK"], default: "AAPL" },
               candles: new Map(), quotes: new Map(), ws: null, attempt: 0, timer: null, toastTimer: null, chart: null, series: null, volume: null,
               fallback: null, haveHistory: false, nav: new core.NavigationHistory(), lastState: null, conn: "disconnected", lastCandle: null,
               pending: { candles: false, position: false, ppe: false, demand: false, setup: false, proposal: false, evidence: false }, watchlist: null, hbTime: "" };
  const subs = new core.SubscriptionManager((m) => { if (st.ws && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(m)); });

  // ---------------------------------------------------------------- layout -------------------------------------------------
  function shell() {
    body.className = "tv";
    body.innerHTML = `
      <div class="tv-top" role="toolbar" aria-label="Trader View controls">
        <div class="tv-nav">
          <button type="button" id="tv-back" title="Back (previous ticker)" disabled>◀ Back</button>
          <button type="button" id="tv-forward" title="Forward" disabled>Forward ▶</button>
        </div>
        <form id="tv-search" autocomplete="off" role="search"><input id="tv-input" placeholder="Ticker · press /" maxlength="10" aria-label="Ticker search" spellcheck="false" /><button type="submit">Open</button></form>
        <div class="tv-head">
          <span id="tv-symbol" class="tv-symbol">—</span>
          <span id="tv-price" class="tv-price">—</span>
          <span id="tv-change" class="tv-change">—</span>
          <span id="tv-qstate" class="tv-qstate tv-hidden" role="status"></span>
        </div>
        <span class="tv-spacer"></span>
        <span id="tv-mode" class="tv-source sim" title="Market data source">SIMULATED DATA</span>
        <span id="tv-conn" class="tv-conn bad" role="status" aria-live="polite"><i class="tv-dot" aria-hidden="true"></i><span id="tv-conn-text">DISCONNECTED</span></span>
      </div>
      <div class="tv-grid">
        <aside class="tv-left" aria-label="Watchlist">
          <h3>Watchlist <span class="hint" id="tv-wl-note"></span></h3>
          <ul id="tv-watchlist" class="tv-list"></ul>
          <h3>Recent</h3>
          <ul id="tv-recent" class="tv-list"></ul>
        </aside>
        <section class="tv-center" aria-label="Chart">
          <div class="tv-quote">
            <span class="tv-ba">Bid <b id="tv-bid">—</b></span><span class="tv-ba">Ask <b id="tv-ask">—</b></span>
            <span class="tv-ba">1m <b id="tv-candle">—</b></span><span class="tv-ba" id="tv-clock"></span><span class="tv-ba tv-hb" id="tv-hb" title="Last stream heartbeat (UTC)"></span>
          </div>
          <div class="tv-chart-wrap">
            <div id="tv-chart" class="tv-chart"></div>
            <canvas id="tv-fallback" class="tv-fallback tv-hidden" width="900" height="380"></canvas>
            <div id="tv-chart-note" class="tv-chart-note tv-hidden"></div>
          </div>
          <div class="tv-status-line" id="tv-status-line"></div>
          <div class="tv-footnote" id="tv-footnote"></div>
        </section>
        <aside class="tv-right" aria-label="Position, PPE, evidence and actions">
          <section class="tv-panel"><h3>Position</h3><div id="tv-position" class="tv-panel-body"></div></section>
          <section class="tv-panel"><h3>PPE</h3><div id="tv-ppe" class="tv-panel-body"></div></section>
          <section class="tv-panel tv-panel-demand"><h3>Market Demand</h3><div id="tv-demand" class="tv-panel-body"></div></section>
          <section class="tv-panel tv-panel-setup"><h3>Setup</h3><div id="tv-setup" class="tv-panel-body"></div></section>
          <section class="tv-panel tv-panel-proposal"><h3>Trade Review</h3><div id="tv-proposal" class="tv-panel-body"></div></section>
          <section class="tv-panel"><h3>Evidence</h3><div id="tv-evidence" class="tv-panel-body"></div></section>
          <section class="tv-panel tv-panel-last"><h3>Actions</h3><div class="tv-actions" id="tv-actions"></div></section>
        </aside>
      </div>
      <div id="tv-toast" class="tv-toast tv-hidden" role="status" aria-live="polite"></div>`;
    $("tv-search").addEventListener("submit", (e) => { e.preventDefault(); switchTo($("tv-input").value, true); });
    $("tv-back").addEventListener("click", () => { const s = st.nav.back(); if (s) switchTo(s, false); });
    $("tv-forward").addEventListener("click", () => { const s = st.nav.forward(); if (s) switchTo(s, false); });
    $("tv-watchlist").addEventListener("click", (e) => { const li = e.target.closest("li[data-sym]"); if (li) switchTo(li.dataset.sym, true); });
    $("tv-recent").addEventListener("click", (e) => { const li = e.target.closest("li[data-sym]"); if (li) switchTo(li.dataset.sym, true); });
    $("tv-actions").addEventListener("click", (e) => { const b = e.target.closest("[data-action]"); if (b && !b.disabled) onAction(b.dataset.action); });
    document.addEventListener("keydown", (e) => {                               // "/" focuses the ticker search while the tab is shown
      if (!st.visible || e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      $("tv-input").focus();
    });
  }

  // ---------------------------------------------------------------- chart --------------------------------------------------
  function setupChart() {
    const el = $("tv-chart"), cv = $("tv-fallback");
    const LW = window.LightweightCharts;
    if (LW && typeof LW.createChart === "function") {
      st.chart = LW.createChart(el, {
        layout: { background: { type: "solid", color: "#0d1117" }, textColor: "#8b949e", fontSize: 11 },
        grid: { vertLines: { color: "#161b22" }, horzLines: { color: "#161b22" } },
        rightPriceScale: { borderColor: "#30363d" }, timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
        autoSize: true,
      });
      st.series = st.chart.addCandlestickSeries({ upColor: "#3fb950", downColor: "#f85149", borderVisible: false, wickUpColor: "#3fb950", wickDownColor: "#f85149" });
      st.volume = st.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
      st.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      $("tv-footnote").textContent = "Chart: TradingView Lightweight Charts v4.2.3 served locally (frontend/vendor, Apache-2.0). 1-minute candles; volume below.";
    } else {
      st.fallback = cv; cv.classList.remove("tv-hidden"); el.classList.add("tv-hidden");
      $("tv-footnote").textContent = "Chart library not loaded — plain canvas fallback (last 60 candles).";
    }
  }
  const bar = (c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
  const vol = (c) => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? "rgba(63,185,80,0.45)" : "rgba(248,81,73,0.45)" });
  function chartNote() {
    const n = $("tv-chart-note");
    const text = st.haveHistory ? "" : (st.pending.candles ? `Loading candles · ${st.active || ""}` : "No candles");
    n.textContent = text; n.classList.toggle("tv-hidden", !text);
  }
  function setCandles(list) {
    st.haveHistory = list.length > 0;
    if (st.series) { st.series.setData(list.map(bar)); st.volume.setData(list.map(vol)); if (list.length) st.chart.timeScale().scrollToRealTime(); }
    else drawFallback(list);
    if (list.length) renderCandleLabel(list[list.length - 1]); else $("tv-candle").textContent = "—";
    chartNote();
  }
  function updateCandle(c) {
    const list = st.candles.get(st.active) || [];
    const last = list[list.length - 1];
    if (last && last.time === c.time) list[list.length - 1] = c;
    else if (!last || c.time > last.time) list.push(c);
    else return;                                                            // older than the chart's last bar: ignore
    st.candles.set(st.active, list);
    if (!st.haveHistory) { setCandles(list); return; }
    if (st.series) { st.series.update(bar(c)); st.volume.update(vol(c)); } else drawFallback(list);
    renderCandleLabel(c);
  }
  function drawFallback(list) {
    const cv = st.fallback; if (!cv) return;
    const ctx = cv.getContext("2d"); const W = cv.width, H = cv.height;
    ctx.fillStyle = "#0d1117"; ctx.fillRect(0, 0, W, H);
    const rows = list.slice(-60); if (!rows.length) return;
    const lo = Math.min(...rows.map((c) => c.low)), hi = Math.max(...rows.map((c) => c.high)); const span = hi - lo || 1;
    const y = (p) => H - 30 - ((p - lo) / span) * (H - 50); const w = (W - 40) / rows.length;
    rows.forEach((c, i) => {
      const x = 20 + i * w + w / 2; const up = c.close >= c.open; ctx.strokeStyle = ctx.fillStyle = up ? "#3fb950" : "#f85149";
      ctx.beginPath(); ctx.moveTo(x, y(c.high)); ctx.lineTo(x, y(c.low)); ctx.stroke();
      const top = y(Math.max(c.open, c.close)), bottom = y(Math.min(c.open, c.close));
      ctx.fillRect(x - w * 0.3, top, w * 0.6, Math.max(1, bottom - top));
    });
    ctx.fillStyle = "#8b949e"; ctx.font = "11px sans-serif"; ctx.fillText(`${hi.toFixed(2)}`, 4, 14); ctx.fillText(`${lo.toFixed(2)}`, 4, H - 34);
  }

  // ---------------------------------------------------------------- rendering ----------------------------------------------
  function setConn(state) {
    st.conn = state;
    const c = R.connectionView(state, st.attempt);
    const el = $("tv-conn"); el.className = `tv-conn ${c.cls}`; $("tv-conn-text").textContent = c.text;
    el.title = c.live ? `Stream connected${st.hbTime ? ` · last heartbeat ${st.hbTime} UTC` : ""}` : "The stream is down: shown values are the last received and may be stale.";
    body.classList.toggle("tv-stale", !c.live);
    if (st.active) renderQuote(st.quotes.get(st.active) || { symbol: st.active, status: "PENDING", message: "Loading…" });
  }
  function setMode() {
    const s = R.sourceView(st.mode, st.source);
    const el = $("tv-mode"); el.textContent = s.label; el.className = `tv-source ${s.cls}`; el.title = `${s.title} · ${s.detail}`;
    $("tv-wl-note").textContent = s.simulated ? "simulated quotes" : "live quotes · read only";
    body.classList.toggle("tv-sim", s.simulated);
  }
  function updateNav() { $("tv-back").disabled = !st.nav.canBack(); $("tv-forward").disabled = !st.nav.canForward(); }
  function renderCandleLabel(c) { st.lastCandle = c; $("tv-candle").textContent = `O ${core.formatPrice(c.open)} H ${core.formatPrice(c.high)} L ${core.formatPrice(c.low)} C ${core.formatPrice(c.close)}`; }
  function renderQuote(q) {
    const w = R.quoteView(q, st.conn === "connected");
    $("tv-price").textContent = w.price;
    const ch = $("tv-change"); ch.textContent = w.change; ch.className = `tv-change ${w.dir}`;
    $("tv-bid").textContent = w.bid; $("tv-ask").textContent = w.ask;
    $("tv-clock").textContent = w.asOf || w.message || "";
    const qs = $("tv-qstate"); qs.textContent = w.label;
    qs.className = `tv-qstate ${w.state === "LOADING" ? "dim" : w.state !== "OK" ? "bad" : w.stale ? "warn" : ""}`;
    qs.classList.toggle("tv-hidden", !w.label);
  }
  function renderWatchlist(items) {
    if (items) st.watchlist = items;
    const ul = $("tv-watchlist");
    const rows = st.watchlist && st.watchlist.length ? st.watchlist : (st.symbols.watchlist || []).map((s) => ({ symbol: s }));
    ul.innerHTML = R.watchlistRowsHtml(rows, st.active, (s) => st.quotes.get(s)) ||
      `<li class="tv-empty">Watchlist is empty (watchlist.txt). Demo: ${esc((st.symbols.demo || []).join(", "))}</li>`;
  }
  function renderRecent() {
    const seen = new Set(); const out = [];
    for (let i = st.nav.items.length - 1; i >= 0 && out.length < MAX_RECENT; i -= 1) { const s = st.nav.items[i]; if (!seen.has(s)) { seen.add(s); out.push(s); } }
    $("tv-recent").innerHTML = R.recentRowsHtml(out, st.active, (s) => st.quotes.get(s)) || `<li class="tv-empty">No history yet.</li>`;
  }
  function renderPosition(p) {
    const el = $("tv-position"); el.className = "tv-panel-body"; st.pending.position = false;
    el.dataset.symbol = (p && p.symbol) || st.active || "";
    if (!p || !p.sources || !p.sources.length) {
      el.dataset.state = "UNAVAILABLE";
      el.innerHTML = R.stateHtml("POSITION_UNAVAILABLE", "POSITION UNAVAILABLE", (p && p.note) || "Position sources unavailable.");
      return;
    }
    const held = p.sources.filter((s) => s.status === "POSITION");
    el.dataset.state = held.length ? "POSITION" : "NO_POSITION";
    const row = (s) => {
      const tag = s.status === "POSITION" ? "ok" : s.status === "NO_POSITION" ? "dim" : "warn";
      return `<div class="tv-src" title="${esc(s.message || "")}"><span class="tv-src-label">${esc(s.label)}</span><span class="tv-tag ${tag}">${esc(String(s.status).replace(/_/g, " "))}</span></div>`;
    };
    const kv = (s) => `<table class="tv-kv"><tr><td>Qty</td><td>${esc(v(s.quantity))}</td></tr><tr><td>Avg cost</td><td>${esc(v(s.average_cost))}</td></tr>` +
      `<tr><td>Mkt value</td><td>${esc(v(s.market_value))}</td></tr><tr><td>P/L</td><td>${esc(v(s.unrealized_pnl))}${s.unrealized_pnl_pct == null ? "" : ` (${esc(s.unrealized_pnl_pct)}%)`}</td></tr>` +
      (s.as_of ? `<tr><td>As of</td><td>${esc(s.as_of)}</td></tr>` : "") + `</table>`;
    el.innerHTML = (held.length ? "" : R.stateHtml("NO_POSITION", "NO POSITION")) +
      p.sources.map((s) => row(s) + (s.status === "POSITION" ? kv(s) + (s.message ? `<p class="tv-dim">${esc(s.message)}</p>` : "") : "")).join("") +
      `<details class="tv-details"><summary>Why each source shows this</summary>` +
      p.sources.map((s) => `<div class="tv-dim"><b>${esc(s.label)}</b> — ${esc(s.message || (s.status === "NO_POSITION" ? "No position" : s.status))}</div>`).join("") +
      `<div class="tv-dim">${esc(p.note)}</div></details>`;
  }
  function renderPPE(p) {
    // Stage 5.6: the deterministic engine result. Never a recommendation: no buy/sell text, no probability of a rise.
    const el = $("tv-ppe"); st.pending.ppe = false;
    el.dataset.symbol = (p && p.symbol) || st.active || "";
    if (!p || !p.stretch) {
      el.dataset.state = "NOT AVAILABLE";
      el.setAttribute("aria-label", "PPE Stretch and PPE Confidence: NOT AVAILABLE");
      el.innerHTML = R.ppeHtml(null, { symbol: st.active });
      return;
    }
    const sim = p.status === "SIMULATED_DATA";
    const badge = sim ? `<span class="tv-tag warn">SIMULATED DATA</span>` : R.ppeBadgeHtml(p);
    const m = R.ppeModel(p);
    el.dataset.state = p.status;
    el.setAttribute("aria-label", `PPE Stretch ${m.stretch.value} ${m.stretch.band} · PPE Confidence ${m.confidence.value} ${m.confidence.band}${sim ? " · SIMULATED DATA" : ""}`);
    el.innerHTML = R.ppeHtml(p, { badge, includeDisclaimer: false }) +
      `<p class="tv-disclaimer">${esc(p.disclaimer || "")}</p>` + R.ppeSourceHtml(p, sim ? p.note : null);   // the SIMULATED DATA tag stays on the card; the long note sits under "Data used"
  }
  function renderDemand(d) {
    // Stage 5.7: the deterministic Market Demand result — a separate section, never merged with PPE. No buy/sell text, no forecast.
    const el = $("tv-demand"); st.pending.demand = false;
    el.dataset.symbol = (d && d.symbol) || st.active || "";
    if (!d || !d.status) {
      el.dataset.state = "NOT AVAILABLE";
      el.innerHTML = R.demandHtml(null, { symbol: st.active });
      return;
    }
    const m = R.demandModel(d);
    el.dataset.state = d.status; el.dataset.mode = d.mode || "";
    el.setAttribute("aria-label", `Market Demand ${m.value} ${m.band}${m.mode ? " · " + m.mode : ""}${m.simulated ? " · SIMULATED DATA" : ""}`);
    el.innerHTML = R.demandHtml(d, { includeDisclaimer: false }) + `<p class="tv-disclaimer">${esc(d.disclaimer || "")}</p>` + R.demandSourceHtml(d, m.simulated ? d.note : null);
  }
  function renderSetup(s) {
    // Stage 5.8: the stored setup state (NONE / WATCH / READY FOR REVIEW / INVALIDATED). A label, never a button; never an entry signal.
    const el = $("tv-setup"); st.pending.setup = false;
    el.dataset.symbol = (s && s.symbol) || st.active || "";
    if (!s || !s.state) {
      el.dataset.state = "NOT AVAILABLE";
      el.innerHTML = R.setupHtml(null, { symbol: st.active });
      return;
    }
    const m = R.setupModel(s);
    el.dataset.state = s.state; el.dataset.decision = s.decision || "";
    el.setAttribute("aria-label", `Setup ${m.label}${m.paused ? " · paused, stale data" : ""}`);
    el.innerHTML = R.setupHtml(s, { includeDisclaimer: false }) + `<p class="tv-disclaimer">${esc(s.disclaimer || "")}</p>`;
  }
  function renderProposal(p) {
    // Stage 5.9: the stored review proposal (NOT ELIGIBLE / BLOCKED / REVIEWABLE PROPOSAL / STALE / EXPIRED). A review artifact: never a button that trades.
    const el = $("tv-proposal"); st.pending.proposal = false;
    el.dataset.symbol = (p && p.symbol) || st.active || "";
    if (!p || !p.effective_status) {
      el.dataset.state = "NOT AVAILABLE";
      el.innerHTML = R.proposalHtml(null, { symbol: st.active });
      return;
    }
    const m = R.proposalModel(p);
    el.dataset.state = p.effective_status; el.dataset.proposalId = p.proposal_id || "";
    el.setAttribute("aria-label", `Trade review ${m.label}`);
    el.innerHTML = R.proposalHtml(p, { includeDisclaimer: false }) + `<p class="tv-disclaimer">${esc(p.disclaimer || "")}</p>`;
    if (window.TradeReview && typeof window.TradeReview.bind === "function") window.TradeReview.bind(el, p);
  }
  function applyProposal(p) {                                                // trade_review.js hands back the proposal after a review-state change
    if (p && p.symbol === st.active) renderProposal(p);
  }
  function onSetupAlert(a) {
    if (!a || !a.alert_id || seenAlerts.has(a.alert_id)) return;              // one toast per persisted alert
    seenAlerts.add(a.alert_id);
    toast(R.setupAlertLine(a));
    if (window.SetupAlerts && typeof window.SetupAlerts.refresh === "function") window.SetupAlerts.refresh();
  }
  function renderEvidence(e) {
    const el = $("tv-evidence"); st.pending.evidence = false;
    el.dataset.symbol = (e && e.symbol) || st.active || "";
    el.dataset.state = e && e.status === "LOADED" ? "LOADED" : "NOT LOADED";
    el.innerHTML = R.evidenceHtml(e);
  }
  function renderActions(actions) {
    $("tv-actions").innerHTML = (actions && actions.length ? actions : DEFAULT_ACTIONS).map((a) =>
      `<button type="button" class="tv-act ${a.enabled ? "" : "tv-disabled"}" data-action="${esc(a.id)}" aria-disabled="${a.enabled ? "false" : "true"}"${a.enabled ? "" : " disabled"} title="${esc(a.message)}">` +
      `${esc(a.label)}<small>${esc(a.enabled ? (a.status === "NAVIGATE_ONLY" ? "stored evidence only" : String(a.status).replace(/_/g, " ")) : "disabled")}</small></button>`).join("");
  }
  function renderStatusLine(d) {
    $("tv-status-line").innerHTML = R.statusChips(d, st.mode).map((c) => `<span class="tv-chip ${c.cls}">${esc(c.text)}</span>`).join("") +
      (d.data_status && d.data_status.market_source ? `<span class="tv-chip-src">${esc(d.data_status.market_source)}</span>` : "");
  }
  function showLoading(symbol) {
    for (const [id, what] of PANELS) { const el = $(id); el.innerHTML = R.skeletonHtml(what, symbol); el.dataset.state = "LOADING"; el.dataset.symbol = symbol; }
  }
  function showNoData(symbol, message) {
    for (const [id, what] of PANELS) { const el = $(id); el.innerHTML = R.stateHtml(`${what}_NO_DATA`, `${what} · NO DATA`, message); el.dataset.state = "NO_DATA"; el.dataset.symbol = symbol; }
  }
  function applyState(d) {
    st.lastState = d;
    if (d.data_status && d.data_status.mode) { st.mode = d.data_status.mode; st.source = d.data_status.market_source || st.source; setMode(); }
    st.quotes.set(d.symbol, d.quote); renderQuote(d.quote); renderPosition(d.position); renderEvidence(d.evidence); renderPPE(d.ppe); renderDemand(d.demand); renderSetup(d.setup); renderProposal(d.proposal); renderActions(d.actions); renderStatusLine(d);
    renderWatchlist(null); renderRecent();
  }
  function toast(text) {
    const el = $("tv-toast"); el.textContent = text; el.classList.remove("tv-hidden");
    clearTimeout(st.toastTimer); st.toastTimer = setTimeout(() => el.classList.add("tv-hidden"), 3500);
  }

  // ---------------------------------------------------------------- actions (no execution backend) ------------------------
  function onAction(id) {
    if (id === "research") {
      const tab = document.querySelector('.tab-btn[data-tab="strategy"]');
      const ws = document.querySelector('[data-sfv="evidence54"]');
      if (tab && ws) { tab.click(); ws.click(); toast("Opened Strategy Lab › Evidence (stored packets only; no research was run)."); return; }
    }
    toast(ACTION_TEXT[id] || "Not connected in Trader View.");
  }

  // ---------------------------------------------------------------- ticker switching ---------------------------------------
  function switchTo(raw, record) {
    const s = core.normalizeSymbol(raw);
    if (!s) { toast(`"${String(raw == null ? "" : raw).trim()}" is not a valid ticker (1–10 letters, "." or "-").`); return false; }
    if (s === st.active) { $("tv-input").value = ""; return true; }           // already shown: nothing to reload (the subscription is unchanged)
    if (record) st.nav.push(s);
    st.active = s; st.haveHistory = false; st.lastCandle = null;
    st.pending = { candles: true, position: true, ppe: true, demand: true, evidence: true };
    st.pending.setup = true;
    st.pending.proposal = true;
    $("tv-symbol").textContent = s; $("tv-input").value = "";
    updateNav(); renderWatchlist(null); renderRecent();
    const cached = st.candles.get(s);
    setCandles(cached ? cached.slice() : []);                                 // cached history paints immediately, the stream refreshes it
    renderQuote(st.quotes.get(s) || { symbol: s, status: "PENDING", message: "Loading…" });
    showLoading(s);                                                            // never the previous ticker's panels under the new symbol
    if (st.visible) subs.setActive(s);                                         // unsubscribe(old) + subscribe(new) when the socket is open
    if (!(st.ws && st.ws.readyState === WebSocket.OPEN)) loadStateHttp(s);     // no socket yet: plain GET of the same state
    return true;
  }
  async function loadStateHttp(s) {
    try {
      const res = await fetch(`/api/terminal/state/${encodeURIComponent(s)}?candles=${HISTORY}`);
      const d = await res.json();
      if (st.active !== s) return;                                            // switched while loading: discard
      if (!res.ok) {
        toast(`${d.status || res.status}: ${d.message || "request failed"}`);
        renderQuote({ symbol: s, status: "UNAVAILABLE", code: d.status === "UNKNOWN_SYMBOL" ? "NO_DATA" : "FEED_ERROR", message: d.message || d.status });
        showNoData(s, d.message || d.status);
        return;
      }
      applyState(d);
      if (d.candles) { st.pending.candles = false; st.candles.set(s, d.candles.slice()); setCandles(d.candles); }
    } catch (e) { if (st.active === s) { toast("Could not load the terminal state."); renderQuote({ symbol: s, status: "UNAVAILABLE", code: "FEED_ERROR", message: "Could not load the terminal state." }); } }
  }

  // ---------------------------------------------------------------- socket -------------------------------------------------
  function connect() {
    if (st.ws && (st.ws.readyState === WebSocket.OPEN || st.ws.readyState === WebSocket.CONNECTING)) return;   // never two sockets
    clearTimeout(st.timer); st.timer = null;
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${WS_PATH}`;
    setConn(st.attempt ? "reconnecting" : "connecting");
    let ws;
    try { ws = new WebSocket(url); } catch (e) { scheduleReconnect(); return; }
    st.ws = ws;
    ws.onopen = () => { if (st.ws !== ws) return; st.attempt = 0; setConn("connected"); subs.onOpen(); };
    ws.onmessage = (ev) => {
      if (st.ws !== ws) return;
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!core.acceptsMessage(msg, st.active)) return;                       // stale-ticker guard
      onMessage(msg);
    };
    ws.onclose = () => {
      if (st.ws !== ws) return;
      st.ws = null; subs.onClose();
      if (st.visible) { setConn("reconnecting"); scheduleReconnect(); } else setConn("disconnected");
    };
    ws.onerror = () => {};
  }
  function scheduleReconnect() {
    if (st.timer) return;
    const delay = core.nextBackoff(st.attempt) + Math.floor(Math.random() * 250);
    st.attempt += 1;
    st.timer = setTimeout(() => { st.timer = null; if (st.visible) connect(); }, delay);
  }
  function onMessage(m) {
    switch (m.type) {
      case "hello": st.mode = m.mode; st.source = m.source; setMode(); if (m.symbols) { st.symbols = m.symbols; renderWatchlist(null); } break;
      case "state": applyState(m); break;
      case "candles": st.pending.candles = false; st.candles.set(m.symbol, m.candles.slice()); setCandles(m.candles); break;
      case "quote": st.quotes.set(m.symbol, m); renderQuote(m); renderWatchlist(null); renderRecent(); break;
      case "candle": updateCandle(m); break;
      case "watchlist": m.items.forEach((it) => { if (it.price != null) st.quotes.set(it.symbol, Object.assign({}, st.quotes.get(it.symbol) || {}, it)); }); renderWatchlist(m.items); break;
      case "position": renderPosition(m); break;
      case "evidence": renderEvidence(m); break;
      case "ppe": renderPPE(m); break;
      case "demand": renderDemand(m); break;
      case "setup": renderSetup(m); break;
      case "setup_alert": onSetupAlert(m); break;
      case "proposal": renderProposal(m); break;
      case "heartbeat": st.hbTime = m.server_time ? m.server_time.slice(11, 19) : ""; $("tv-hb").textContent = st.hbTime ? `heartbeat ${st.hbTime} UTC` : ""; break;
      case "subscribed": case "unsubscribed": break;
      case "error":
        toast(`${m.code}: ${m.message}`);
        if ((m.code === "UNKNOWN_SYMBOL" || m.code === "INVALID_SYMBOL" || m.code === "MARKET_UNAVAILABLE") && (!m.symbol || m.symbol === st.active)) {
          renderQuote({ symbol: m.symbol || st.active, status: "UNAVAILABLE", code: m.code === "UNKNOWN_SYMBOL" ? "NO_DATA" : "FEED_ERROR", message: m.message });
          if (m.code !== "MARKET_UNAVAILABLE") showNoData(st.active, m.message);
        }
        break;
      default: break;
    }
  }

  // ---------------------------------------------------------------- tab lifecycle ------------------------------------------
  async function boot() {
    st.booted = true;
    shell(); setupChart(); setMode(); renderPPE(null); renderDemand(null); renderSetup(null); renderProposal(null); renderActions(DEFAULT_ACTIONS);
    try { const cfg = await (await fetch("/api/terminal/config")).json(); st.mode = cfg.mode; st.source = cfg.source; setMode(); renderActions(cfg.actions); } catch (e) { /* defaults stay */ }
    try { const sy = await (await fetch("/api/terminal/symbols")).json(); if (sy && sy.watchlist) st.symbols = sy; } catch (e) { /* demo symbols stay */ }
    renderWatchlist(null);
    switchTo(st.symbols.default || (st.symbols.watchlist || [])[0] || "AAPL", true);
  }
  function activate() {
    st.visible = true;
    if (!st.booted) boot();
    if (st.ws && st.ws.readyState === WebSocket.OPEN) subs.setActive(st.active); else connect();
  }
  function deactivate() {
    if (!st.visible) return;
    st.visible = false;
    subs.setActive(null);                                                      // unsubscribe: the server polls nothing for this tab
  }
  document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => { if (b.dataset.tab === "trader") activate(); else deactivate(); }));
  if (tabEl.classList.contains("active")) activate();

  window.TraderView = {
    get state() { return { active: st.active, visible: st.visible, mode: st.mode, conn: R.connectionView(st.conn, st.attempt).label, connState: st.conn, history: st.nav.items.slice(),
                           index: st.nav.index, subscribed: subs.sent, candles: (st.candles.get(st.active) || []).length, lastCandle: st.lastCandle, attempt: st.attempt,
                           pending: Object.assign({}, st.pending) }; },
    switchTo: (s) => switchTo(s, true), back: () => { const s = st.nav.back(); if (s) switchTo(s, false); return s; },
    forward: () => { const s = st.nav.forward(); if (s) switchTo(s, false); return s; }, activate,
    dropSocket: () => { if (st.ws) st.ws.close(); return !!st.ws; },           // tests / harness only: forces the reconnect path (no server call)
    applyProposal,                                                             // Stage 5.9: trade_review.js hands back a proposal after a review-state change (symbol-guarded)
  };
})();
