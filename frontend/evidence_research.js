// evidence_research.js — Stage 5.4 MULTI-SOURCE EVIDENCE RESEARCH (a Strategy Lab workspace). RESEARCH ONLY — EVIDENCE STRENGTH IS NOT A PRICE PROBABILITY.
//
// Build one evidence packet for a ticker (SEC EDGAR, the declared IR page, existing Alpaca headlines, public URLs you supply, internal
// market / event data), then read the cause verdict, the six-rung recovery ladder with the items behind each rung, contradictions,
// gaps, next events, the fetch log and the strength grade. The Explain button is a dry run first and makes exactly one model request
// only after a second confirm. No broker button, nothing is written into any form, no timers, nothing on page load; every value is escaped.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("evr-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const BASE = "/api/evidence";
  const BANNER = "EVIDENCE RESEARCH — RESEARCH ONLY — EVIDENCE STRENGTH IS NOT A PRICE PROBABILITY";
  const SK = { SUPPORTED: "ok", PARTIAL: "warn", NOT_SUPPORTED: "alert", NOT_AVAILABLE: "dim", STRONG: "ok", MODERATE: "warn", WEAK: "alert",
               FACT: "ok", INFERENCE: "warn", UNCONFIRMED: "dim", UP: "ok", DOWN: "alert", NEUTRAL: "warn", NA: "dim", OK: "ok" };
  let cfg = null, packets = [], detail = null, busy = "", notice = "", requests = 0, modelRequests = 0, seq = 0, pendingExplain = null;
  let form = { ticker: "", lookback: 90, urls: [] };

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer." } }));
  };

  async function load() {
    const my = ++seq;
    const [c, p] = await Promise.all([api("GET", `${BASE}/config`), api("GET", `${BASE}/packets?limit=20`)]);
    if (my !== seq) return;
    if (c.status === 200) cfg = c.body;
    if (p.status === 200) packets = p.body.packets || [];
    draw();
  }

  async function build() {
    const ticker = form.ticker.trim().toUpperCase();
    if (!ticker) { notice = "Enter a ticker."; draw(); return; }
    const manual_urls = form.urls.filter((u) => u.url && u.role).map((u) => ({ url: u.url, role: u.role, related_ticker: u.related || null }));
    const hosts = ["www.sec.gov", "data.sec.gov"].concat(((cfg && cfg.ir_urls && cfg.ir_urls[ticker]) || []).map(hostOf)).concat(manual_urls.map((u) => hostOf(u.url)));
    const msg = `Build an evidence packet for ${ticker}?\n\nHosts that may be contacted (https, one attempt each):\n${[...new Set(hosts)].join("\n")}\n\n` +
      "Plus the existing Alpaca market-data and news client. 0 broker requests, 0 Robinhood requests, 0 model requests.";
    if (!window.confirm(msg)) return;
    busy = "building"; notice = ""; draw();
    const r = await api("POST", `${BASE}/packets`, { ticker, lookback_days: Number(form.lookback) || 90, manual_urls });
    busy = "";
    notice = r.status === 201 || r.status === 200 ? `${r.body.status}: ${r.body.strength || r.body.failure_code || ""} (${r.body.n_items} items, ${r.body.n_failed_fetches} failed fetches)`
      : (r.body.message || `HTTP ${r.status}`);
    await load();
    if (r.body && r.body.packet_id) open(r.body.packet_id);
  }

  async function open(id) {
    busy = "loading"; draw();
    const r = await api("GET", `${BASE}/packets/${id}`);
    busy = "";
    detail = r.status === 200 ? r.body : null;
    if (!detail) notice = r.body.message || `HTTP ${r.status}`;
    draw();
  }

  async function explain(confirm) {
    if (!detail) return;
    const id = detail.packet.packet_id;
    if (!confirm) {
      const r = await api("POST", `${BASE}/packets/${id}/explain`, { confirm: false });
      pendingExplain = r.status === 200 ? r.body : null;
      notice = pendingExplain ? `Dry run: exactly ${pendingExplain.requests} model request (${pendingExplain.prompt_chars} chars) would be made. Nothing was sent.` : (r.body.message || `HTTP ${r.status}`);
      draw(); return;
    }
    if (!window.confirm(`Make exactly 1 model request for packet ${id}?`)) return;
    busy = "explaining"; draw();
    const r = await api("POST", `${BASE}/packets/${id}/explain`, { confirm: true });
    busy = ""; pendingExplain = null;
    modelRequests += (r.body && r.body.requests_made) || 0;
    notice = r.status === 200 ? `Explanation ${r.body.status}` : (r.body.message || `HTTP ${r.status}`);
    await open(id);
  }

  function hostOf(u) { try { return new URL(u).hostname; } catch (e) { return "?"; } }

  function draw() {
    const d = detail;
    root.innerHTML = `
      <div class="evr-banner">${esc(BANNER)}</div>
      <div class="evr-form">
        <label>Ticker <input id="evr-ticker" maxlength="10" value="${esc(form.ticker)}"></label>
        <label>Lookback days <input id="evr-lookback" type="number" min="10" max="365" value="${esc(form.lookback)}"></label>
        <button id="evr-add-url" type="button">+ public URL</button>
        <button id="evr-build" type="button" ${busy ? "disabled" : ""}>Build packet…</button>
        ${cfg ? `<span class="evr-dim">network ${cfg.network_enabled ? "on" : "OFF"} · SEC user agent ${cfg.sec_user_agent_configured ? "set" : "MISSING"} · model ${cfg.model_configured ? "configured" : "not configured"}</span>` : ""}
      </div>
      <div class="evr-urls">${form.urls.map((u, i) => `
        <div class="evr-url-row"><input data-i="${i}" data-k="url" placeholder="https://…" value="${esc(u.url)}">
        <select data-i="${i}" data-k="role">${["company", "supplier", "customer", "industry", "press", "social"].map((r) => `<option ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}</select>
        <input data-i="${i}" data-k="related" placeholder="related ticker (optional)" maxlength="10" value="${esc(u.related || "")}"></div>`).join("")}</div>
      <div class="evr-notice">${busy ? esc(busy + "…") : esc(notice)}</div>
      <div class="evr-cols">
        <div class="evr-list"><h4>Packets</h4>${packets.map((p) => `<button type="button" class="evr-pk ${d && d.packet.packet_id === p.packet_id ? "sel" : ""}" data-id="${esc(p.packet_id)}">
          ${esc(p.ticker)} ${esc(p.as_of_date)} ${tag(p.status, p.status === "COMPLETED" ? "ok" : "alert")} ${p.strength ? tag(p.strength, SK[p.strength]) : ""}</button>`).join("") || '<div class="evr-dim">none yet</div>'}</div>
        <div class="evr-detail">${d ? detailHtml(d) : '<div class="evr-dim">Build or pick a packet.</div>'}</div>
      </div>`;
    root.querySelector("#evr-ticker").oninput = (e) => { form.ticker = e.target.value; };
    root.querySelector("#evr-lookback").oninput = (e) => { form.lookback = e.target.value; };
    root.querySelector("#evr-add-url").onclick = () => { form.urls.push({ url: "", role: "supplier", related: "" }); draw(); };
    root.querySelector("#evr-build").onclick = build;
    root.querySelectorAll(".evr-url-row input, .evr-url-row select").forEach((el) => { el.onchange = el.oninput = (e) => { form.urls[Number(e.target.dataset.i)][e.target.dataset.k] = e.target.value; }; });
    root.querySelectorAll(".evr-pk").forEach((b) => { b.onclick = () => open(b.dataset.id); });
    const dry = root.querySelector("#evr-explain-dry"); if (dry) dry.onclick = () => explain(false);
    const go = root.querySelector("#evr-explain-go"); if (go) go.onclick = () => explain(true);
  }

  function detailHtml(d) {
    const p = d.packet, items = d.items || [], byId = {};
    items.forEach((i) => { byId[i.item_id] = i; });
    const item = (i) => `<li>${tag(i.epistemic, SK[i.epistemic])} ${tag(i.direction, SK[i.direction])} <span class="evr-dim">tier ${esc(i.source_tier)} · ${esc(i.evidence_date)} · ${esc(i.source_name)}${i.contradiction ? " · CONTRADICTION" : ""}</span>
      ${i.source_url ? `<a href="${esc(i.source_url)}" target="_blank" rel="noopener">source</a>` : ""}<div class="evr-text">${esc(i.text)}</div>${i.rule_match ? `<div class="evr-dim">rule: ${esc(i.rule_match)}</div>` : ""}</li>`;
    const rungs = (p.ladder || []).map((r) => `<div class="evr-rung"><b>${esc(r.rung)}. ${esc(r.name)}</b> ${tag(r.status, SK[r.status])}<div class="evr-dim">${esc(r.reason)}</div>
      <ul>${(r.item_ids || []).map((id) => byId[id]).filter(Boolean).map(item).join("")}</ul></div>`).join("");
    const used = new Set((p.ladder || []).flatMap((r) => r.item_ids || []));
    const rest = items.filter((i) => !used.has(i.item_id));
    const ex = (d.explanations || []).map((e) => `<div class="evr-expl">${tag(e.status, e.status === "COMPLETED" ? "ok" : "alert")} ${esc(e.model)} ${esc(e.created_at)}
      <div class="evr-text">${esc(e.text || "")}</div>${Object.keys(e.guard_failures || {}).length ? `<div class="evr-dim">guard: ${esc(JSON.stringify(e.guard_failures))}</div>` : ""}</div>`).join("");
    return `
      <h3>${esc(p.ticker)} · as of ${esc(p.as_of_date)} · ${tag(p.status, p.status === "COMPLETED" ? "ok" : "alert")} ${p.strength ? tag(p.strength, SK[p.strength]) : ""}</h3>
      <div class="evr-dim">packet ${esc(p.packet_id)} · hash ${esc(p.packet_hash.slice(0, 16))}… · rules ${esc(p.rules_version)} · ${esc(p.n_items)} items · ${esc(p.n_fetches)} fetches (${esc(p.n_failed_fetches)} failed) · SEC requests ${esc(p.sec_requests)}</div>
      <div class="evr-card"><b>Cause:</b> ${tag(p.cause.verdict, "warn")} <div class="evr-dim">${esc(p.cause.reason)}</div></div>
      <div class="evr-card"><b>Strength:</b> ${esc(p.strength_text)}</div>
      <h4>Recovery ladder</h4>${rungs}
      <h4>Gaps</h4><ul>${(p.missing || []).map((m) => `<li>${esc(m.text)}</li>`).join("") || "<li>none</li>"}</ul>
      <h4>Next events</h4><ul>${(p.next_events || []).map((e) => `<li><b>${esc(e.event)}</b> ${esc(e.date || "date unknown")} — ${esc(e.would)}</li>`).join("")}</ul>
      <h4>Other items (${rest.length})</h4><ul>${rest.map(item).join("")}</ul>
      <h4>Fetch log</h4><table class="evr-fetch"><tr><th>adapter</th><th>status</th><th>url</th><th>http</th><th>bytes</th><th>error</th></tr>
        ${(d.fetches || []).map((f) => `<tr><td>${esc(f.adapter)}</td><td>${tag(f.status, f.status === "OK" ? "ok" : "alert")}</td><td class="evr-url">${esc(f.url)}</td><td>${esc(f.http_status || "")}</td><td>${esc(f.bytes)}</td><td>${esc(f.error)}</td></tr>`).join("")}</table>
      <h4>Explanation (optional, one model request)</h4>
      <button id="evr-explain-dry" type="button" ${busy ? "disabled" : ""}>Dry run</button>
      ${pendingExplain && pendingExplain.packet_id === p.packet_id ? `<button id="evr-explain-go" type="button" ${busy ? "disabled" : ""}>Confirm: make exactly 1 request</button>` : ""}
      ${ex}`;
  }

  function show() { if (!cfg) load(); else draw(); }
  window.StrategyFit.addWorkspace({ id: "evidence54", label: "Evidence", cls: "evr-mode", show });
  window.EvidenceResearch = { get state() { return { busy, notice, requests, modelRequests, packets: packets.length, packet: detail ? detail.packet.packet_id : null }; } };
})();
