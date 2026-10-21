// trader_render.js — Stage 5.6.1 Trader View: pure view-models and HTML builders (no DOM, no network, no socket), so node can test
// exactly what the tab renders. Loaded as a classic script in the dashboard (window.TraderRender) and required by
// tests/frontend/trader_render.test.cjs. Display only: nothing here computes PPE, evidence or market data — it formats server results.
//   connectionView     CONNECTED / CONNECTING / RECONNECTING / DISCONNECTED + whether shown values are live
//   sourceView         the persistent data-source badge: SIMULATED DATA or the named read-only market-data source
//   quoteView          price / change / bid / ask and an explicit state (LOADING, FEED UNAVAILABLE, NO DATA, INVALID SYMBOL, STALE)
//   watchlistRowsHtml  compact rows (ticker · price · %), the active ticker marked; an unavailable quote shows "—", never 0
//   evidenceHtml       the Evidence section with explicit EVIDENCE NOT LOADED / EVIDENCE FAILED states
//   ppeModel / ppeHtml the PPE card: Stretch and Confidence as two separate neutral blocks, coverage, key factors, explicit
//                      PPE INSUFFICIENT DATA / PPE EVIDENCE NOT AVAILABLE states, a visible explainer and the exact disclaimer
//   skeletonHtml       the small loading state shown for the NEW ticker while its data is on the way
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  root.TraderRender = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const words = (x) => (x == null || x === "" ? "" : String(x).replace(/_/g, " "));
  const num = (v) => (v == null || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
  const price = (v) => (num(v) == null ? "—" : num(v).toFixed(2));
  const pct = (v) => (num(v) == null ? "—" : `${num(v) >= 0 ? "+" : ""}${num(v).toFixed(2)}%`);
  const dir = (v) => (num(v) == null ? "" : num(v) > 0 ? "up" : num(v) < 0 ? "down" : "");

  const EXPLAIN_STRETCH = "Stretch: how far price is displaced from its normal conditions.";
  const EXPLAIN_CONFIDENCE = "Confidence: how well the stored evidence supports an overreaction or dislocation reading. It is not a forecast.";

  // ---------------------------------------------------------------- connection + source ------------------------------------
  const CONN = { connected: ["CONNECTED", "ok"], connecting: ["CONNECTING", "warn"], reconnecting: ["RECONNECTING", "warn"], disconnected: ["DISCONNECTED", "bad"] };
  function connectionView(state, attempt) {
    const key = CONN[state] ? state : "disconnected";
    const [label, cls] = CONN[key];
    const detail = key === "reconnecting" && Number(attempt) > 0 ? `try ${Number(attempt) + 1}` : "";
    return { state: key, label, cls, live: key === "connected", detail, text: detail ? `${label} · ${detail}` : label };
  }
  function sourceView(mode, source) {
    const simulated = mode !== "real";
    const src = String(source || "");
    const label = simulated ? "SIMULATED DATA" : (/alpaca/i.test(src) ? "ALPACA MARKET DATA" : "LIVE MARKET DATA");
    return { simulated, label, cls: simulated ? "sim" : "real", detail: simulated ? "deterministic simulator · not a market feed" : "read only · polled",
             title: src || (simulated ? "deterministic simulator" : "market data") };
  }

  // ---------------------------------------------------------------- quote ------------------------------------------------
  function quoteView(q, live) {
    const ok = !!q && q.status === "OK";
    let state = "OK";
    if (!q || q.status === "PENDING") state = "LOADING";
    else if (q.status === "INVALID") state = "INVALID SYMBOL";
    else if (q.status !== "OK") state = q.code === "NO_DATA" ? "NO DATA" : "FEED UNAVAILABLE";
    const p = q ? num(q.price) : null;
    const stale = live === false && ok;
    return { state, ok, stale, price: p == null ? "—" : price(p), change: ok ? pct(q.change_pct) : "—", dir: ok ? dir(q.change_pct) : "",
             bid: ok ? price(q.bid) : "—", ask: ok ? price(q.ask) : "—", asOf: ok && q.ts ? `as of ${new Date(q.ts * 1000).toLocaleTimeString()}` : "",
             message: (q && q.message) || "", label: state === "OK" ? (stale ? "STALE" : "") : state };
  }

  // ---------------------------------------------------------------- watchlist --------------------------------------------
  function rowHtml(symbol, q, active, withPrice) {
    const unavailable = !q || q.price == null;
    const isActive = symbol === active;
    return `<li data-sym="${esc(symbol)}" class="${isActive ? "active" : ""}"${isActive ? ' aria-current="true"' : ""}><span class="tv-sym">${esc(symbol)}</span>` +
      `<span class="tv-px">${withPrice ? (unavailable ? "—" : esc(price(q.price))) : ""}</span>` +
      `<span class="tv-chg ${unavailable ? "" : dir(q.change_pct)}">${unavailable ? "—" : esc(pct(q.change_pct))}</span></li>`;
  }
  function watchlistRowsHtml(items, active, quoteOf) {
    return (items || []).map((it) => rowHtml(it.symbol, it.price != null ? it : ((quoteOf && quoteOf(it.symbol)) || null), active, true)).join("");
  }
  function recentRowsHtml(symbols, active, quoteOf) {
    return (symbols || []).map((s) => rowHtml(s, (quoteOf && quoteOf(s)) || null, active, false)).join("");
  }

  // ---------------------------------------------------------------- states -----------------------------------------------
  function firstSentence(s) {                                                   // state details stay one sentence; the full text lives in the result
    const t = String(s == null ? "" : s).trim();
    const i = t.indexOf(". ");
    return i > 0 ? t.slice(0, i + 1) : t;
  }
  function stateHtml(code, text, detail) {
    return `<div class="tv-state" data-state="${esc(code)}">${esc(text)}</div>${detail ? `<p class="tv-dim">${esc(firstSentence(detail))}</p>` : ""}`;
  }
  function skeletonHtml(what, symbol) {
    return `<div class="tv-skel" data-state="LOADING" aria-busy="true"><div class="tv-state tv-state-dim">LOADING ${esc(what)}${symbol ? ` · ${esc(symbol)}` : ""}</div>` +
      `<div class="tv-skel-bar w70"></div><div class="tv-skel-bar w40"></div></div>`;
  }

  // ---------------------------------------------------------------- evidence ---------------------------------------------
  function evidenceHtml(e) {
    if (!e || e.status !== "LOADED") {
      const failed = !!e && e.status === "FAILED";
      return stateHtml(failed ? "EVIDENCE_FAILED" : "EVIDENCE_NOT_LOADED", failed ? "EVIDENCE FAILED" : "EVIDENCE NOT LOADED") +
        (e && e.message ? `<p class="tv-dim">${esc(e.message)}</p>` : "") +
        `<p class="tv-dim">Research opens the stored evidence workspace. Nothing runs on a ticker switch.</p>`;
    }
    const row = (k, x) => `<tr><td>${esc(k)}</td><td>${esc(x == null || x === "" ? "—" : words(x))}</td></tr>`;
    return `<table class="tv-kv">${row("Strength", e.strength)}${row("Cause", e.cause)}${row("Company confirm", e.company_confirm)}${row("Market confirm", e.market_confirm)}` +
      `${row("Last research", e.created_at ? String(e.created_at).slice(0, 10) : null)}${e.packet_status && e.packet_status !== "COMPLETED" ? row("Packet", e.packet_status) : ""}</table>` +
      (e.strength_text ? `<p class="tv-dim">${esc(e.strength_text)}</p>` : "") + (e.message ? `<p class="tv-dim">${esc(e.message)}</p>` : "") +
      (e.disclaimer ? `<p class="tv-disclaimer">${esc(e.disclaimer)}</p>` : "");
  }

  // ---------------------------------------------------------------- PPE --------------------------------------------------
  const BAND_CLS = { EXTREME_DOWNSIDE: "down extreme", DOWNSIDE_STRETCHED: "down", NORMAL: "", UPSIDE_STRETCHED: "up", EXTREME_UPSIDE: "up extreme" };
  const CONF_NA = { EVIDENCE_NOT_AVAILABLE: "EVIDENCE NOT AVAILABLE", STRETCH_NOT_AVAILABLE: "NOT AVAILABLE", NOT_APPLICABLE: "NOT APPLICABLE" };

  function ppeBadgeHtml(p) {
    if (!p) return "";
    if (p.status === "SIMULATED_DATA") return `<span class="tv-tag warn">SIMULATED DATA</span>`;
    if (p.status !== "AVAILABLE") return `<span class="tv-tag warn">${esc(words(p.status))}</span>`;
    return `<span class="tv-tag ok">${esc(words((p.sources || {}).benchmark_kind || "AVAILABLE"))}</span>`;
  }

  function ppeModel(p) {
    const stt = (p && p.stretch) || {}, cf = (p && p.confidence) || {}, src = (p && p.sources) || {};
    const s = num(stt.score), c = num(cf.score), cov = num(stt.coverage_weight);
    const stretchOk = stt.status === "AVAILABLE" && s != null;
    const confOk = cf.status === "AVAILABLE" && c != null;
    let state = "AVAILABLE";
    if (!p || !p.stretch) state = "NOT_LOADED";
    else if (stt.status === "INSUFFICIENT_DATA" || p.status === "INSUFFICIENT_DATA") state = "INSUFFICIENT_DATA";
    else if (!stretchOk) state = "NOT_AVAILABLE";
    const stretch = { ok: stretchOk, value: stretchOk ? `${s > 0 ? "+" : ""}${s.toFixed(1)}` : "—", status: stt.status || "NOT_AVAILABLE",
                      band: stretchOk ? words(stt.label) : (state === "INSUFFICIENT_DATA" ? "INSUFFICIENT DATA" : "NOT AVAILABLE"), cls: stretchOk ? (BAND_CLS[stt.label] || "") : "" };
    const confidence = { ok: confOk, value: confOk ? String(Math.round(c)) : "—", status: cf.status || "STRETCH_NOT_AVAILABLE",
                         band: confOk ? words(cf.label) : (CONF_NA[cf.status] || "NOT AVAILABLE"), cls: "", reasons: (cf.reasons || []).slice(0, 3) };
    const lines = [];                                                            // explicit states: never a 0 or 0 % for an unavailable value
    if (state === "INSUFFICIENT_DATA") lines.push({ code: "PPE_INSUFFICIENT_DATA", text: "PPE INSUFFICIENT DATA", detail: cov != null ? `Only ${Math.round(cov * 100)}% of the component weight is available (60% floor).` : (stt.note || "") });
    else if (state === "NOT_AVAILABLE") lines.push({ code: "PPE_NOT_AVAILABLE", text: "PPE NOT AVAILABLE", detail: stt.note || (p && p.note) || "" });
    if (state !== "NOT_LOADED" && cf.status === "EVIDENCE_NOT_AVAILABLE") lines.push({ code: "PPE_EVIDENCE_NOT_AVAILABLE", text: "PPE EVIDENCE NOT AVAILABLE", detail: (cf.reasons || [])[0] || "" });
    else if (state === "AVAILABLE" && cf.status === "NOT_APPLICABLE") lines.push({ code: "PPE_CONFIDENCE_NOT_APPLICABLE", text: "CONFIDENCE NOT APPLICABLE", detail: (cf.reasons || [])[0] || "Stretch is inside the normal band: there is no stretched move to classify." });
    const ev = cf.evidence || null;
    const bits = [src.market_data, src.benchmark_label ? `benchmark ${src.benchmark_label}` : "no benchmark", src.bars_through ? `bars through ${src.bars_through}` : "",
                  src.sessions_used ? `${src.sessions_used} sessions` : "", src.evidence_packet_at ? `evidence ${String(src.evidence_packet_at).slice(0, 16)}` : "",
                  src.computed_at ? `computed ${String(src.computed_at).slice(11, 19)} UTC` : ""].filter(Boolean);
    return { state, simulated: !!p && p.status === "SIMULATED_DATA", symbol: p ? p.symbol || null : null, stretch, confidence, lines,
             coverage: cov == null ? "—" : `${Math.round(cov * 100)}%`, factors: (stt.key_factors || []).slice(0, 3),
             evidence: ev ? `${ev.strength || "—"} / ${ev.cause || "—"} · packet ${String(ev.created_at || "").slice(0, 10)} (${ev.age_days}d old)` : null,
             benchmark: src.benchmark_label || null, source: bits.join(" · "), note: p ? p.note || null : null, disclaimer: p ? p.disclaimer || "" : "",
             explain: { stretch: EXPLAIN_STRETCH, confidence: EXPLAIN_CONFIDENCE } };
  }

  function ppeHtml(p, opts) {
    const o = opts || {};
    const m = ppeModel(p);
    if (m.state === "NOT_LOADED") return o.loading ? skeletonHtml("PPE", o.symbol) : stateHtml("PPE_NOT_AVAILABLE", "PPE NOT AVAILABLE", o.symbol ? `No PPE result for ${o.symbol} yet.` : "No PPE result yet.");
    const badge = o.badge != null ? o.badge : ppeBadgeHtml(p);
    const block = (key, label, x, extra) => `<div class="tv-ppe-block${x.ok ? "" : " na"}" data-ppe="${key}"><span class="tv-ppe-lbl">${label}${extra || ""}</span>` +
      `<b class="tv-ppe-val ${x.ok ? x.cls : ""}">${esc(x.value)}</b><span class="tv-ppe-band ${x.ok ? x.cls : ""}">${esc(x.band)}</span></div>`;
    const info = `<button type="button" class="tv-info" data-ppe-info="confidence" title="${esc(EXPLAIN_CONFIDENCE)}" aria-label="What PPE Confidence means">?</button>`;
    return `<div class="tv-ppe-head">${badge}${m.benchmark ? `<span class="tv-ppe-bench" title="benchmark used for the relative move">vs ${esc(m.benchmark)}</span>` : ""}</div>` +
      `<div class="tv-ppe-scores">${block("stretch", "Stretch", m.stretch)}${block("confidence", "Confidence", m.confidence, info)}</div>` +
      `<div class="tv-ppe-meta"><span>Coverage</span><b>${esc(m.coverage)}</b></div>` +
      m.lines.map((l) => stateHtml(l.code, l.text, l.detail)).join("") +
      (m.factors.length ? `<div class="tv-ppe-sub">Key factors</div><ul class="tv-ppe-list">${m.factors.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "") +
      (m.confidence.ok && m.confidence.reasons.length ? `<div class="tv-ppe-sub">Confidence drivers</div><ul class="tv-ppe-list">${m.confidence.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "") +
      (m.evidence ? `<div class="tv-ppe-sub">Evidence</div><div class="tv-ppe-ev">${esc(m.evidence)}</div>` : "") +
      `<p class="tv-explain">${esc(EXPLAIN_STRETCH)} ${esc(EXPLAIN_CONFIDENCE)}</p>` +
      (o.includeDisclaimer === false ? "" : `<p class="tv-disclaimer">${esc(m.disclaimer)}</p>`);
  }
  function ppeSourceHtml(p, note) {
    const m = ppeModel(p);
    if (!m.source && !note) return "";
    return `<details class="tv-details"><summary>Data used</summary>${m.source ? `<div class="tv-dim">${esc(m.source)}</div>` : ""}${note ? `<div class="tv-dim">${esc(note)}</div>` : ""}</details>`;
  }

  // ---------------------------------------------------------------- Market Demand (Stage 5.7; separate from PPE) ----------
  const DEMAND_CLS = { VERY_WEAK: "weak", WEAK: "weak", MIXED: "", STRENGTHENING: "strong", STRONG: "strong", VERY_STRONG: "strong extreme" };
  const EXPLAIN_DEMAND = "Market Demand: observed buyer participation and price acceptance in the bars (volume vs normal, VWAP acceptance, intraday structure, range, strength vs sector, follow-through). It is separate from PPE and is not a forecast.";
  function demandModel(d) {
    const src = (d && d.sources) || {};
    const s = num(d && d.score);
    const simulated = !!d && (d.status === "SIMULATED_DATA" || src.mode === "simulated");
    let state = "AVAILABLE";
    if (!d || !d.status) state = "NOT_LOADED";
    else if (d.status === "NOT_AVAILABLE") state = "NOT_AVAILABLE";
    else if (s == null) state = "INSUFFICIENT_DATA";
    const mode = d && d.mode === "INTRADAY_MODE" ? "INTRADAY" : d && d.mode === "DAILY_MODE" ? "DAILY" : "";
    const cov = num(d && d.coverage_weight);
    const sess = src.session || {};
    const lines = [];
    if (state === "INSUFFICIENT_DATA") lines.push({ code: "DEMAND_INSUFFICIENT_DATA", text: "DEMAND INSUFFICIENT DATA", detail: (d && d.note) || "" });
    else if (state === "NOT_AVAILABLE") lines.push({ code: "DEMAND_NOT_AVAILABLE", text: "DEMAND NOT AVAILABLE", detail: (d && d.note) || "" });
    const bits = [src.market_data, src.benchmark_label ? `benchmark ${src.benchmark_label}` : "no benchmark", src.bars_through ? `bars through ${src.bars_through}` : "",
                  sess.date ? `session ${sess.date} · ${sess.bars_completed || 0} completed bars · ${sess.complete ? "complete" : "in progress"}` : "",
                  src.computed_at ? `computed ${String(src.computed_at).slice(11, 19)} UTC` : ""].filter(Boolean);
    const bt = src.bars_through ? (String(src.bars_through).length > 10 ? `bars through ${String(src.bars_through).slice(11, 16)} UTC` : `bars through ${src.bars_through}`) : "";
    const stamp = [src.benchmark_label ? `vs ${src.benchmark_label}` : "", bt,
                   sess.date && mode ? (sess.complete ? "session complete" : (mode === "INTRADAY" ? `session in progress · ${sess.bars_completed || 0} bars` : "completed sessions only")) : "",
                   src.computed_at ? `computed ${String(src.computed_at).slice(11, 19)} UTC` : ""].filter(Boolean).join(" · ");
    return { state, simulated, symbol: d ? d.symbol || null : null, value: s == null ? "—" : String(Math.round(s)), band: state === "AVAILABLE" ? words(d.label) : (state === "INSUFFICIENT_DATA" ? "INSUFFICIENT DATA" : "NOT AVAILABLE"),
             cls: state === "AVAILABLE" ? (DEMAND_CLS[d.label] || "") : "", mode, modeText: mode === "DAILY" ? "DAILY · completed bars" : mode === "INTRADAY" ? "INTRADAY · session bars" : "",
             coverage: cov == null ? "—" : `${Math.round(cov * 100)}%`, factors: (d && d.key_factors || []).slice(0, 4), lines, benchmark: src.benchmark_label || null,
             source: bits.join(" · "), stamp, note: d ? d.note || null : null, disclaimer: d ? d.disclaimer || "" : "", explain: EXPLAIN_DEMAND };
  }
  function demandHtml(d, opts) {
    const o = opts || {};
    const m = demandModel(d);
    if (m.state === "NOT_LOADED") return o.loading ? skeletonHtml("DEMAND", o.symbol) : stateHtml("DEMAND_NOT_AVAILABLE", "DEMAND NOT AVAILABLE", o.symbol ? `No Market Demand result for ${o.symbol} yet.` : "No Market Demand result yet.");
    const badge = m.simulated ? `<span class="tv-tag warn">SIMULATED DATA</span>` : "";
    const modeChip = m.mode ? `<span class="tv-dem-mode" data-demand-mode="${esc(m.mode)}" title="${esc(m.mode === "DAILY" ? "Only completed daily bars are available: no real-time confirmation is implied." : "Completed bars of the current session.")}">${esc(m.modeText)}</span>` : "";
    return `<div class="tv-dem-head">${badge}${modeChip}</div>` +
      `<div class="tv-dem-score${m.state === "AVAILABLE" ? "" : " na"}" data-demand="score"><b class="tv-dem-val ${m.cls}">${esc(m.value)}</b><span class="tv-dem-band ${m.cls}">${esc(m.band)}</span>` +
      `<button type="button" class="tv-info" data-demand-info="score" title="${esc(EXPLAIN_DEMAND)}" aria-label="What Market Demand means">?</button></div>` +
      `<div class="tv-ppe-meta"><span>Coverage</span><b>${esc(m.coverage)}</b></div>` +
      (m.stamp ? `<div class="tv-dem-stamp">${esc(m.stamp)}</div>` : "") +
      m.lines.map((l) => stateHtml(l.code, l.text, l.detail)).join("") +
      (m.factors.length ? `<div class="tv-ppe-sub">Key factors</div><ul class="tv-ppe-list">${m.factors.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "") +
      `<p class="tv-explain">${esc(EXPLAIN_DEMAND)}</p>` +
      (o.includeDisclaimer === false ? "" : `<p class="tv-disclaimer">${esc(m.disclaimer)}</p>`);
  }
  function demandSourceHtml(d, note) {
    const m = demandModel(d);
    if (!m.source && !note) return "";
    return `<details class="tv-details"><summary>Data used</summary>${m.source ? `<div class="tv-dim">${esc(m.source)}</div>` : ""}${note ? `<div class="tv-dim">${esc(note)}</div>` : ""}</details>`;
  }

  // ---------------------------------------------------------------- status strip -----------------------------------------
  function statusChips(d, mode) {
    const ds = (d && d.data_status) || {};
    const m = String(ds.mode || mode || "").toLowerCase();
    const chips = [{ text: m === "real" ? `market ${String(ds.market || "").toLowerCase() || "ok"}` : "simulated data", cls: m === "real" ? "" : "warn" }];
    if (ds.position) chips.push({ text: `positions ${words(ds.position).toLowerCase()}`, cls: ds.position === "OK" ? "" : "warn" });
    if (ds.evidence) chips.push({ text: `evidence ${words(ds.evidence).toLowerCase()}`, cls: "" });
    if (d && d.ppe && d.ppe.status) chips.push({ text: `PPE ${words(d.ppe.status).toLowerCase()}`, cls: "" });
    if (d && d.market_state && d.market_state.session_hint) chips.push({ text: `session ${words(d.market_state.session_hint).toLowerCase()}`, cls: "" });
    return chips;
  }

  return { esc, words, connectionView, sourceView, quoteView, watchlistRowsHtml, recentRowsHtml, stateHtml, skeletonHtml, evidenceHtml,
           ppeBadgeHtml, ppeModel, ppeHtml, ppeSourceHtml, demandModel, demandHtml, demandSourceHtml, statusChips, EXPLAIN_STRETCH, EXPLAIN_CONFIDENCE, EXPLAIN_DEMAND };
});
