// trader_view.js — Stage 5.5 TRADER VIEW: a read-only terminal tab. READ ONLY · NO ORDER PATH · PREVIEW NOT CONNECTED.
//
// One WebSocket (/api/terminal/stream) opened the first time the tab is shown; exactly one detailed subscription at a time
// (unsubscribe old → subscribe new on every switch, one re-subscribe after a reconnect, unsubscribe when the tab is left so the
// server polls nothing). Every symbol-bound message is dropped unless it carries the ACTIVE ticker (TraderCore.acceptsMessage), so a
// late message for a previous ticker can never paint the screen. Candles: TradingView Lightweight Charts served locally from
// frontend/vendor (no CDN), with a plain canvas fallback. Prepare Buy / Prepare Sell / Add Alert only show a local notice; Research
// navigates to the stored evidence workspace. This file never sends a POST and never names an order endpoint.
(function () {
  "use strict";
  const tabEl = document.getElementById("tab-trader");
  const body = document.getElementById("tv-body");
  const core = window.TraderCore;
  if (!tabEl || !body || !core) return;

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

  const st = { booted: false, visible: false, active: null, mode: "", source: "", symbols: { watchlist: [], demo: ["AAPL", "NVDA", "SNDK"], default: "AAPL" },
               candles: new Map(), quotes: new Map(), ws: null, attempt: 0, timer: null, toastTimer: null, chart: null, series: null, volume: null,
               fallback: null, haveHistory: false, nav: new core.NavigationHistory(), lastState: null, conn: "Disconnected", lastCandle: null };
  const subs = new core.SubscriptionManager((m) => { if (st.ws && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(m)); });

  // ---------------------------------------------------------------- layout -------------------------------------------------
  function shell() {
    body.className = "tv";
    body.innerHTML = `
      <div class="tv-top">
        <button type="button" id="tv-back" title="Back (previous ticker)" disabled>◀ Back</button>
        <button type="button" id="tv-forward" title="Forward" disabled>Forward ▶</button>
        <form id="tv-search" autocomplete="off"><input id="tv-input" placeholder="Ticker (e.g. NVDA)" maxlength="10" aria-label="Ticker search" /><button type="submit">Open</button></form>
        <span id="tv-symbol" class="tv-symbol">—</span>
        <span id="tv-mode" class="tv-pill dim" title="Market data mode">—</span>
        <span class="tv-spacer"></span>
        <span id="tv-hb" class="tv-pill dim" title="Last heartbeat"></span>
        <span id="tv-conn" class="tv-pill bad">Disconnected</span>
      </div>
      <div class="tv-grid">
        <aside class="tv-left">
          <h3>Watchlist <span class="hint" id="tv-wl-note">(watchlist.txt)</span></h3>
          <ul id="tv-watchlist" class="tv-list"></ul>
          <h3>Recent</h3>
          <ul id="tv-recent" class="tv-list"></ul>
        </aside>
        <section class="tv-center">
          <div class="tv-quote">
            <span id="tv-price" class="tv-price">—</span><span id="tv-change" class="tv-change">—</span>
            <span class="tv-ba">Bid <b id="tv-bid">—</b></span><span class="tv-ba">Ask <b id="tv-ask">—</b></span>
            <span class="tv-ba">1m candle <b id="tv-candle">—</b></span><span class="tv-ba" id="tv-clock"></span>
          </div>
          <div id="tv-chart" class="tv-chart"></div>
          <canvas id="tv-fallback" class="tv-fallback tv-hidden" width="900" height="380"></canvas>
          <div class="tv-status-line" id="tv-status-line"></div>
          <div class="tv-footnote" id="tv-footnote"></div>
        </section>
        <aside class="tv-right">
          <div class="tv-panel"><h3>Position</h3><div id="tv-position" class="tv-panel-body tv-dim">Not loaded</div></div>
          <div class="tv-panel"><h3>PPE</h3><div id="tv-ppe" class="tv-panel-body"></div></div>
          <div class="tv-panel"><h3>Evidence</h3><div id="tv-evidence" class="tv-panel-body"><div class="tv-ev-state">NOT LOADED</div></div></div>
          <div class="tv-actions" id="tv-actions"></div>
        </aside>
      </div>
      <div id="tv-toast" class="tv-toast tv-hidden" role="status" aria-live="polite"></div>`;
    $("tv-search").addEventListener("submit", (e) => { e.preventDefault(); switchTo($("tv-input").value, true); });
    $("tv-back").addEventListener("click", () => { const s = st.nav.back(); if (s) switchTo(s, false); });
    $("tv-forward").addEventListener("click", () => { const s = st.nav.forward(); if (s) switchTo(s, false); });
    $("tv-watchlist").addEventListener("click", (e) => { const li = e.target.closest("li[data-sym]"); if (li) switchTo(li.dataset.sym, true); });
    $("tv-recent").addEventListener("click", (e) => { const li = e.target.closest("li[data-sym]"); if (li) switchTo(li.dataset.sym, true); });
    $("tv-actions").addEventListener("click", (e) => { const b = e.target.closest("[data-action]"); if (b) onAction(b.dataset.action); });
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
  function setCandles(list) {
    st.haveHistory = list.length > 0;
    if (st.series) { st.series.setData(list.map(bar)); st.volume.setData(list.map(vol)); if (list.length) st.chart.timeScale().scrollToRealTime(); }
    else drawFallback(list);
    if (list.length) renderCandleLabel(list[list.length - 1]); else $("tv-candle").textContent = "—";
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
  function setConn(text, cls) { st.conn = text; const el = $("tv-conn"); el.textContent = text; el.className = `tv-pill ${cls}`; }
  function setMode() {
    const el = $("tv-mode");
    el.textContent = st.mode === "real" ? "LIVE · POLLED (read only)" : "SIMULATED DATA";
    el.className = `tv-pill ${st.mode === "real" ? "ok" : "warn"}`;
    el.title = st.source || "";
    $("tv-wl-note").textContent = st.mode === "real" ? "(watchlist.txt · live quotes)" : "(watchlist.txt · simulated quotes)";
  }
  function updateNav() { $("tv-back").disabled = !st.nav.canBack(); $("tv-forward").disabled = !st.nav.canForward(); }
  function renderCandleLabel(c) { st.lastCandle = c; $("tv-candle").textContent = `O ${core.formatPrice(c.open)} H ${core.formatPrice(c.high)} L ${core.formatPrice(c.low)} C ${core.formatPrice(c.close)}`; }
  function renderQuote(q) {
    const ok = q && q.status === "OK";
    $("tv-price").textContent = ok || (q && q.price != null) ? core.formatPrice(q.price) : (q && q.status === "PENDING" ? "…" : "UNAVAILABLE");
    const ch = $("tv-change"); ch.textContent = core.formatPct(q && q.change_pct);
    ch.className = `tv-change ${q && q.change_pct > 0 ? "up" : q && q.change_pct < 0 ? "down" : ""}`;
    $("tv-bid").textContent = core.formatPrice(q && q.bid); $("tv-ask").textContent = core.formatPrice(q && q.ask);
    $("tv-clock").textContent = ok && q.ts ? `as of ${new Date(q.ts * 1000).toLocaleTimeString()}` : ((q && q.message) || "");
  }
  function renderWatchlist(items) {
    const ul = $("tv-watchlist");
    const rows = items && items.length ? items : (st.symbols.watchlist || []).map((s) => ({ symbol: s }));
    ul.innerHTML = rows.map((it) => {
      const q = it.price != null ? it : (st.quotes.get(it.symbol) || it);
      const cls = q.change_pct > 0 ? "up" : q.change_pct < 0 ? "down" : "";
      return `<li data-sym="${esc(it.symbol)}" class="${it.symbol === st.active ? "active" : ""}"><span class="tv-sym">${esc(it.symbol)}</span>` +
             `<span class="tv-px">${esc(core.formatPrice(q.price))}</span><span class="tv-chg ${cls}">${esc(core.formatPct(q.change_pct))}</span></li>`;
    }).join("") || `<li class="tv-empty">Watchlist is empty (watchlist.txt). Demo: ${esc((st.symbols.demo || []).join(", "))}</li>`;
  }
  function renderRecent() {
    const seen = new Set(); const out = [];
    for (let i = st.nav.items.length - 1; i >= 0 && out.length < MAX_RECENT; i -= 1) { const s = st.nav.items[i]; if (!seen.has(s)) { seen.add(s); out.push(s); } }
    $("tv-recent").innerHTML = out.map((s) => `<li data-sym="${esc(s)}" class="${s === st.active ? "active" : ""}"><span class="tv-sym">${esc(s)}</span><span></span><span class="tv-chg">${esc(core.formatPct((st.quotes.get(s) || {}).change_pct))}</span></li>`).join("") ||
      `<li class="tv-empty">No history yet.</li>`;
  }
  function renderPosition(p) {
    const el = $("tv-position"); el.className = "tv-panel-body";
    if (!p || !p.sources || !p.sources.length) { el.innerHTML = `<div class="tv-dim">${esc((p && p.note) || "Position sources unavailable.")}</div>`; return; }
    el.innerHTML = p.sources.map((s) => {
      const tag = s.status === "POSITION" ? "ok" : s.status === "NO_POSITION" ? "dim" : "warn";
      const head = `<div class="tv-src"><span class="tv-src-label">${esc(s.label)}</span><span class="tv-tag ${tag}">${esc(String(s.status).replace(/_/g, " "))}</span></div>`;
      if (s.status === "POSITION") {
        return head + `<table class="tv-kv"><tr><td>Quantity</td><td>${esc(v(s.quantity))}</td></tr><tr><td>Average cost</td><td>${esc(v(s.average_cost))}</td></tr>` +
          `<tr><td>Market value</td><td>${esc(v(s.market_value))}</td></tr><tr><td>Unrealized P/L</td><td>${esc(v(s.unrealized_pnl))}</td></tr>` +
          `<tr><td>Unrealized P/L %</td><td>${esc(s.unrealized_pnl_pct == null ? "—" : s.unrealized_pnl_pct + "%")}</td></tr>` +
          (s.as_of ? `<tr><td>As of</td><td>${esc(s.as_of)}</td></tr>` : "") + `</table>` + (s.message ? `<div class="tv-dim">${esc(s.message)}</div>` : "");
      }
      if (s.status === "NO_POSITION") return head + `<div class="tv-dim">No position${s.as_of ? ` · as of ${esc(s.as_of)}` : ""}</div>`;
      return head + `<div class="tv-dim">${esc(s.message || s.status)}</div>`;
    }).join("") + `<div class="tv-note">${esc(p.note)}</div>`;
  }
  function renderPPE(p) {
    // Stage 5.6: the deterministic engine result. Never a recommendation: no buy/sell text, no probability of a rise.
    const el = $("tv-ppe");
    if (!p || !p.stretch) {
      el.innerHTML = `<div class="tv-ppe-grid"><span>PPE Stretch</span><b class="tv-ppe-score">NOT AVAILABLE</b><span></span><span>PPE Confidence</span><b class="tv-ppe-score">NOT AVAILABLE</b><span></span></div><div class="tv-note">PPE not loaded.</div>`;
      return;
    }
    const st = p.stretch || {}, cf = p.confidence || {}, src = p.sources || {};
    const band = (x) => (x ? String(x).replace(/_/g, " ") : "");
    const sim = p.status === "SIMULATED_DATA";
    const badge = sim ? `<span class="tv-tag warn">SIMULATED DATA</span>` : (p.status !== "AVAILABLE" ? `<span class="tv-tag warn">${esc(band(p.status))}</span>` : `<span class="tv-tag ok">${esc(src.benchmark_kind || "")}</span>`);
    const stretchVal = st.score == null ? "NOT AVAILABLE" : `${st.score > 0 ? "+" : ""}${Number(st.score).toFixed(1)}`;
    const confVal = cf.score == null ? "NOT AVAILABLE" : String(cf.score);
    const stretchCls = st.score == null ? "" : st.score < -40 ? "down" : st.score > 40 ? "up" : "";
    const confStatus = cf.status && cf.status !== "AVAILABLE" ? band(cf.status) : "";
    const factors = (st.key_factors || []).slice(0, 3);
    const reasons = (cf.reasons || []).slice(0, 3);
    const ev = cf.evidence;
    el.innerHTML = `
      <div class="tv-ppe-head">${badge}</div>
      <div class="tv-ppe-grid">
        <span>PPE Stretch</span><b class="tv-ppe-score ${stretchCls}">${esc(stretchVal)}</b><span class="tv-ppe-band">${esc(band(st.label) || band(st.status))}</span>
        <span>PPE Confidence</span><b class="tv-ppe-score">${esc(confVal)}</b><span class="tv-ppe-band">${esc(band(cf.label) || confStatus)}</span>
        <span>Coverage</span><b>${st.coverage_weight != null ? Math.round(st.coverage_weight * 100) + "%" : "—"}</b><span></span>
      </div>
      ${factors.length ? `<div class="tv-ppe-sub">Key factors</div><ul class="tv-ppe-list">${factors.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      ${reasons.length && cf.status === "AVAILABLE" ? `<div class="tv-ppe-sub">Confidence drivers</div><ul class="tv-ppe-list">${reasons.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : (confStatus ? `<div class="tv-dim">${esc((cf.reasons || [])[0] || confStatus)}</div>` : "")}
      <div class="tv-ppe-sub">Evidence</div><div class="tv-dim">${ev ? esc(`${ev.strength || "—"} / ${ev.cause || "—"} · packet ${String(ev.created_at || "").slice(0, 10)} (${ev.age_days}d old)`) : "NOT LOADED"}</div>
      <div class="tv-ppe-src">${esc(src.market_data || "")}${src.benchmark_label ? ` · benchmark ${esc(src.benchmark_label)}` : " · no benchmark"}${src.bars_through ? ` · bars through ${esc(src.bars_through)}` : ""}${src.evidence_packet_at ? ` · evidence ${esc(String(src.evidence_packet_at).slice(0, 16))}` : ""}${src.computed_at ? ` · computed ${esc(String(src.computed_at).slice(11, 19))} UTC` : ""}</div>
      <div class="tv-note">${esc(p.disclaimer || "")}</div>${sim && p.note ? `<div class="tv-dim">${esc(p.note)}</div>` : ""}`;
  }
  function renderEvidence(e) {
    const el = $("tv-evidence");
    if (!e || e.status !== "LOADED") {
      el.innerHTML = `<div class="tv-ev-state">${e && e.status === "FAILED" ? "FAILED" : "NOT LOADED"}</div><div class="tv-dim">${esc((e && e.message) || "")}</div>` +
        `<div class="tv-dim">[Research] opens the stored evidence workspace. Nothing runs automatically on a ticker switch.</div>`;
      return;
    }
    const row = (k, x) => `<tr><td>${esc(k)}</td><td>${esc(x == null ? "—" : x)}</td></tr>`;
    el.innerHTML = `<table class="tv-kv">${row("Evidence", e.strength)}${row("Cause", e.cause)}${row("Company confirm", e.company_confirm)}${row("Market confirm", e.market_confirm)}` +
      `${row("Last research", e.created_at)}${row("Data status", e.data_status + (e.packet_status && e.packet_status !== "COMPLETED" ? ` · ${e.packet_status}` : ""))}</table>` +
      (e.strength_text ? `<div class="tv-dim">${esc(e.strength_text)}</div>` : "") + (e.message ? `<div class="tv-dim">${esc(e.message)}</div>` : "") +
      `<div class="tv-note">${esc(e.disclaimer || "")}</div>`;
  }
  function renderActions(actions) {
    $("tv-actions").innerHTML = (actions && actions.length ? actions : DEFAULT_ACTIONS).map((a) =>
      `<button type="button" class="tv-act ${a.enabled ? "" : "tv-disabled"}" data-action="${esc(a.id)}" aria-disabled="${a.enabled ? "false" : "true"}" title="${esc(a.message)}">` +
      `${esc(a.label)}<small>${esc(a.enabled ? (a.status === "NAVIGATE_ONLY" ? "stored evidence only" : a.status) : String(a.status).replace(/_/g, " "))}</small></button>`).join("");
  }
  function renderStatusLine(d) {
    const ds = d.data_status || {};
    $("tv-status-line").textContent = `Mode ${String(ds.mode || st.mode).toUpperCase()} · Market ${v(ds.market)} · Position ${v(ds.position)} · Evidence ${v(ds.evidence)} · ` +
      `Tradability ${d.tradability ? d.tradability.status : "—"} · PPE ${d.ppe ? d.ppe.status : "—"} · ${d.market_state && d.market_state.session_hint ? `Session ${d.market_state.session_hint} · ` : ""}${v(ds.market_source, "")}`;
  }
  function applyState(d) {
    st.lastState = d;
    if (d.data_status && d.data_status.mode) { st.mode = d.data_status.mode; st.source = d.data_status.market_source || st.source; setMode(); }
    st.quotes.set(d.symbol, d.quote); renderQuote(d.quote); renderPosition(d.position); renderEvidence(d.evidence); renderPPE(d.ppe); renderActions(d.actions); renderStatusLine(d);
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
    if (!s) { toast(`"${String(raw == null ? "" : raw).trim()}" is not a valid ticker.`); return false; }
    if (record) st.nav.push(s);
    st.active = s; st.haveHistory = false; st.lastCandle = null;
    $("tv-symbol").textContent = s; $("tv-input").value = "";
    updateNav(); renderWatchlist(null); renderRecent();
    const cached = st.candles.get(s);
    setCandles(cached ? cached.slice() : []);                                 // cached history paints immediately, the stream refreshes it
    renderQuote(st.quotes.get(s) || { symbol: s, status: "PENDING", message: "Loading…" });
    $("tv-position").innerHTML = `<div class="tv-dim">Loading…</div>`;
    $("tv-evidence").innerHTML = `<div class="tv-ev-state">NOT LOADED</div><div class="tv-dim">Loading…</div>`;
    if (st.visible) subs.setActive(s);                                         // unsubscribe(old) + subscribe(new) when the socket is open
    if (!(st.ws && st.ws.readyState === WebSocket.OPEN)) loadStateHttp(s);     // no socket yet: plain GET of the same state
    return true;
  }
  async function loadStateHttp(s) {
    try {
      const res = await fetch(`/api/terminal/state/${encodeURIComponent(s)}?candles=${HISTORY}`);
      const d = await res.json();
      if (st.active !== s) return;                                            // switched while loading: discard
      if (!res.ok) { toast(`${d.status || res.status}: ${d.message || "request failed"}`); renderQuote({ symbol: s, status: "UNAVAILABLE", message: d.message || d.status }); return; }
      applyState(d);
      if (d.candles) { st.candles.set(s, d.candles.slice()); setCandles(d.candles); }
    } catch (e) { if (st.active === s) toast("Could not load the terminal state."); }
  }

  // ---------------------------------------------------------------- socket -------------------------------------------------
  function connect() {
    if (st.ws && (st.ws.readyState === WebSocket.OPEN || st.ws.readyState === WebSocket.CONNECTING)) return;   // never two sockets
    clearTimeout(st.timer); st.timer = null;
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${WS_PATH}`;
    setConn(st.attempt ? `Reconnecting… (try ${st.attempt + 1})` : "Connecting…", "warn");
    let ws;
    try { ws = new WebSocket(url); } catch (e) { scheduleReconnect(); return; }
    st.ws = ws;
    ws.onopen = () => { if (st.ws !== ws) return; st.attempt = 0; setConn("Connected", "ok"); subs.onOpen(); };
    ws.onmessage = (ev) => {
      if (st.ws !== ws) return;
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!core.acceptsMessage(msg, st.active)) return;                       // stale-ticker guard
      onMessage(msg);
    };
    ws.onclose = () => {
      if (st.ws !== ws) return;
      st.ws = null; subs.onClose();
      if (st.visible) { setConn("Disconnected — reconnecting", "bad"); scheduleReconnect(); } else setConn("Disconnected", "bad");
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
      case "candles": st.candles.set(m.symbol, m.candles.slice()); setCandles(m.candles); break;
      case "quote": st.quotes.set(m.symbol, m); renderQuote(m); renderRecent(); break;
      case "candle": updateCandle(m); break;
      case "watchlist": m.items.forEach((it) => { if (it.price != null) st.quotes.set(it.symbol, Object.assign({}, st.quotes.get(it.symbol) || {}, it)); }); renderWatchlist(m.items); break;
      case "position": renderPosition(m); break;
      case "evidence": renderEvidence(m); break;
      case "ppe": renderPPE(m); break;
      case "heartbeat": $("tv-hb").textContent = `♥ ${m.server_time ? m.server_time.slice(11, 19) : ""}`; break;
      case "subscribed": case "unsubscribed": break;
      case "error": toast(`${m.code}: ${m.message}`); if (m.code === "UNKNOWN_SYMBOL" || m.code === "INVALID_SYMBOL") renderQuote({ symbol: m.symbol, status: "UNAVAILABLE", message: m.message }); break;
      default: break;
    }
  }

  // ---------------------------------------------------------------- tab lifecycle ------------------------------------------
  async function boot() {
    st.booted = true;
    shell(); setupChart(); renderPPE(null); renderActions(DEFAULT_ACTIONS);
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
    get state() { return { active: st.active, visible: st.visible, mode: st.mode, conn: st.conn, history: st.nav.items.slice(), index: st.nav.index, subscribed: subs.sent,
                           candles: (st.candles.get(st.active) || []).length, lastCandle: st.lastCandle, attempt: st.attempt }; },
    switchTo: (s) => switchTo(s, true), back: () => { const s = st.nav.back(); if (s) switchTo(s, false); return s; },
    forward: () => { const s = st.nav.forward(); if (s) switchTo(s, false); return s; }, activate,
  };
})();
