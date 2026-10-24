// Stage 6.1.1 — PRODUCT UI. Four primary sections (TRADE · POSITIONS · ACTIVITY · MORE) layered over the existing dashboard: the legacy tab
// bar is hidden, every legacy workspace stays reachable through MORE, TRADE opens by default on the Trader View with a persistent status bar
// (PAPER · MARKET · ACCOUNT · TRADING · AUTO), an Agent card (WATCHING · READY · ENTERING · IN TRADE · EXITING · CLOSED · NEEDS ATTENTION), a
// compact market / setup summary, a Risk card with STOP TRADING, the current position and Paper Auto; the legacy panels become "View details".
// Display only: every number comes from the existing read-only state (Paper Bot, Risk Controls, Trader View) or the existing GET routes; the
// two actions (STOP TRADING, Paper Auto) delegate to the existing Risk Controls / Paper Bot buttons, so their semantics (two clicks for Paper
// Auto, the persisted POSTs) are exactly the Stage 5.9.1 / 6.0 ones. Nothing here decides a state: the backend truth is only mapped for display.
// Loaded as a classic script (window.ProductUI) and required by the node tests.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  root.ProductUI = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const G = typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : {});
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const words = (x) => (x == null || x === "" ? "" : String(x).replace(/_/g, " ").toLowerCase());
  const num = (x) => { const n = Number(x); return x == null || x === "" || !isFinite(n) ? null : n; };
  const money = (x) => { const n = num(x); return n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}`; };
  const hm = (iso) => (iso ? String(iso).slice(11, 16) : "—");
  const day = (iso) => (iso ? String(iso).slice(0, 10) : "");

  const NAV = [{ id: "trade", label: "TRADE", host: "trader" }, { id: "positions", label: "POSITIONS", host: "paper" }, { id: "activity", label: "ACTIVITY", host: "alerts" }, { id: "more", label: "MORE", host: "settings" }];
  const HOST_OF = { trader: "trade", paper: "positions", alerts: "activity", settings: "more" };
  const TAGLINE = "Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled.";
  const DEFAULT = "trade";
  const MAX_HOLD_DEFAULT = 5;
  // the user-facing state model: the Stage 6.0 card keys / Stage 5.8 setup states mapped for display only (the backend state machines are untouched)
  const AGENT = { WATCHING: ["WATCHING", "Waiting for setup conditions"], READY: ["READY", "Eligible for entry"], ENTERING: ["ENTERING", "Order submitted / awaiting fill"],
                  IN_TRADE: ["IN TRADE", "Monitoring position"], EXITING: ["EXITING", "Exit required / order submitted"], CLOSED: ["CLOSED", "Trade completed"],
                  ATTENTION: ["NEEDS ATTENTION", "Manual review required"], STOPPED: ["NEEDS ATTENTION", "Trading stopped by risk controls"], NONE: ["WATCHING", "Waiting for setup conditions"] };
  const SETUP_TO_AGENT = { WATCH: "WATCHING", READY: "READY", INVALIDATED: "WATCHING", NONE: "WATCHING" };
  const SAFETY_TEXT = { TRADING_ALLOWED: "Trading allowed", NO_NEW_ENTRIES: "New trades paused", HALTED: "Trading stopped", UNKNOWN: "Risk status unknown" };
  const SAFETY_CLS = { TRADING_ALLOWED: "ok", NO_NEW_ENTRIES: "warn", HALTED: "bad", UNKNOWN: "bad" };
  const CASH_TEXT = { FRESH: "fresh", STALE: "stale", UNAVAILABLE: "unavailable", AMBIGUOUS: "unclear" };
  // MORE: every legacy workspace, grouped; nothing is deleted. Strategy Lab workspaces are read from its own sub-navigation at run time.
  const MORE = [
    { title: "Research", items: [{ tab: "strategy", label: "Strategy Lab", lab: true }, { tab: "setups", label: "Research Setups" }, { tab: "history", label: "Research History" }] },
    { title: "Tools", items: [{ tab: "agent", label: "AI Agent" }, { tab: "overview", label: "Market dashboard" }, { tab: "watchlist", label: "Watchlist editor" }, { tab: "daily", label: "Daily Review" }, { tab: "review", label: "Trader Review" }] },
    { title: "System", items: [{ href: "forward_validation.html", label: "Forward Validation" }, { action: "health", label: "Account / execution health" }, { action: "risk", label: "Risk Controls" },
                                 { tab: "portfolio", label: "Portfolio (Robinhood, read only)" }] },
    { title: "Settings", items: [{ action: "settings", label: "Advanced configuration" }] },
  ];
  const state = { section: null, legacyTab: null, timer: null, fetchTimer: null, cards: null, posDetails: null, posData: null, actData: null, limits: null, booted: false, error: null };

  // ---------------------------------------------------------------- pure models ------------------------------------------------------
  function agentModel(card, setupState, safetyState, exec) {
    const key = card && card.key && card.key !== "NONE" ? card.key : (SETUP_TO_AGENT[setupState] || "WATCHING");
    const [label, text] = AGENT[key] || AGENT.WATCHING;
    const stopped = safetyState === "HALTED" || safetyState === "UNKNOWN";
    const attention = !!(exec && exec.attention);
    let out = { key, label, text, cls: label.toLowerCase().replace(/ /g, "-") };
    if (attention && key !== "ATTENTION") out = { key: "ATTENTION", label: "NEEDS ATTENTION", text: (exec.attention_reasons && exec.attention_reasons[0]) || "Manual review required", cls: "needs-attention" };
    else if (stopped && !["IN_TRADE", "EXITING", "ENTERING", "ATTENTION"].includes(key)) out = { key: "STOPPED", label: "NEEDS ATTENTION", text: AGENT.STOPPED[1], cls: "needs-attention" };
    if (card && card.line && ["ENTERING", "IN_TRADE", "EXITING", "CLOSED", "ATTENTION"].includes(key) && out.key === key) out.text = card.line;
    return out;
  }

  function statusModel(exec, safety) {
    const env = (exec && exec.environment) || {};
    const acct = (exec && exec.account_data) || {};
    const market = (exec && exec.scheduler && exec.scheduler.market) || {};
    const st = (safety && safety.state) || (exec && exec.safety_state) || null;
    const mode = exec && exec.mode;
    const paper = { key: "paper", text: env.simulated ? "SIMULATED PAPER" : "PAPER", cls: "ok" };
    const mk = market.regular_session == null ? { key: "market", text: "MARKET —", cls: "" } : { key: "market", text: market.regular_session ? "MARKET OPEN" : "MARKET CLOSED", cls: market.regular_session ? "ok" : "" };
    const a = acct.status || null;
    const account = { key: "account", text: a === "FRESH" ? "ACCOUNT FRESH" : a === "STALE" ? "ACCOUNT STALE" : a === "STARTING" ? "ACCOUNT CHECKING" : a ? "ACCOUNT UNAVAILABLE" : "ACCOUNT —",
                      cls: a === "FRESH" ? "ok" : a === "STALE" ? "warn" : a === "STARTING" || !a ? "" : "warn" };
    const trading = { key: "trading", text: st ? (SAFETY_TEXT[st] || words(st)).toUpperCase() : "RISK —", cls: st ? SAFETY_CLS[st] || "bad" : "" };
    const auto = { key: "auto", text: mode === "PAPER_AUTO" ? "AUTO ON" : mode ? "AUTO OFF" : "AUTO —", cls: mode === "PAPER_AUTO" ? "ok" : "" };
    return [paper, mk, account, trading, auto];
  }

  function riskModel(exec, safety) {
    const st = (safety && safety.state) || (exec && exec.safety_state) || "UNKNOWN";
    const acct = (exec && exec.account_data) || {};
    const cash = acct.cash_flow || null;
    const lines = [];
    const head = SAFETY_TEXT[st] || SAFETY_TEXT.UNKNOWN;
    lines.push({ ok: st === "TRADING_ALLOWED", level: SAFETY_CLS[st] || "bad", text: head });
    if (safety && safety.reason_text && st !== "TRADING_ALLOWED") lines.push({ ok: false, level: SAFETY_CLS[st] || "warn", text: safety.reason_text, sub: true });
    if (acct.status) lines.push({ ok: acct.status === "FRESH", level: acct.status === "FRESH" ? "ok" : "warn", text: acct.status === "FRESH" ? "Account fresh" : acct.status === "STARTING" ? "Account data checking" : acct.status === "STALE" ? "Account data stale" : "Account data unavailable" });
    if (cash && cash.status) lines.push({ ok: cash.status === "FRESH", level: cash.status === "FRESH" ? "ok" : "warn", text: `Cash-flow data ${CASH_TEXT[cash.status] || words(cash.status)}` });
    if (exec && exec.reconciliation_healthy != null) lines.push({ ok: !!exec.reconciliation_healthy, level: exec.reconciliation_healthy ? "ok" : "bad", text: exec.reconciliation_healthy ? "Reconciliation healthy" : "Reconciliation needs a look" });
    for (const r of (exec && exec.attention_reasons) || []) lines.push({ ok: false, level: "bad", text: r });
    const level = lines.some((l) => l.level === "bad") ? "bad" : lines.some((l) => l.level === "warn") ? "warn" : "ok";
    return { state: st, headline: head, level, lines, stopped: st === "HALTED" };
  }

  function positionModel(card, position, limits) {
    const maxHold = num(limits && limits.max_hold_sessions) || MAX_HOLD_DEFAULT;
    const open = !!(card && ["IN_TRADE", "EXITING", "ATTENTION"].includes(card.key) && card.shares != null) || !!(position && position.quantity > 0);
    if (!open) return { open: false, text: "No position", entering: !!(card && card.key === "ENTERING"), symbol: (card && card.symbol) || (position && position.symbol) || null };
    const p = position || {};
    const d = (card && card.details) || {};
    const held = num(p.sessions_held != null ? p.sessions_held : d.sessions_held);
    const status = card ? (AGENT[card.key] ? AGENT[card.key][0] : card.label) : words(p.state);
    return { open: true, symbol: (card && card.symbol) || p.symbol, shares: card && card.shares != null ? card.shares : p.quantity, entry: (card && card.entry) || p.average_entry_price || "—",
             current: (card && card.current) || p.last_price || "—", pnl: card && card.pnl ? `${card.pnl}${card.pnl_pct ? ` (${Number(card.pnl_pct) >= 0 ? "+" : ""}${card.pnl_pct}%)` : ""}` : "—",
             holding: held == null ? "—" : `Day ${held} / ${maxHold}`, status: status === "IN TRADE" ? "Monitoring" : status, reason: (card && card.reason) || p.exit_reason || p.attention_reason || "",
             state: p.state || d.position_state || (card && card.state) || "" };
  }

  function marketModel(dom) {
    // dom = { stretch, confidence, demand, evidence, setupState, proposalState } read from the legacy Trader View panels (display only)
    const s = num(dom.stretch), c = num(dom.confidence), d = num(dom.demand);
    const ev = dom.evidence || null;
    return { ppe: s == null ? "—" : `${s > 0 ? "+" : ""}${Math.round(s)}`, confidence: c == null ? "—" : String(Math.round(c)), demand: d == null ? "—" : String(Math.round(d)),
             evidence: ev === "LOADED" ? (dom.evidenceText && dom.evidenceText !== "—" ? dom.evidenceText : "Stored") : "None stored", setup: SETUP_TO_AGENT[dom.setupState] ? (dom.setupState === "READY" ? "READY" : dom.setupState === "WATCH" ? "WATCHING" : "No active setup") : "—",
             setupLabel: dom.setupState === "READY" ? "READY FOR REVIEW" : dom.setupState === "WATCH" ? "WATCH" : dom.setupState === "INVALIDATED" ? "INVALIDATED" : "NONE",
             proposal: dom.proposalState === "REVIEWABLE" ? "Risk checks passed" : dom.proposalState === "BLOCKED" ? "Blocked by a risk check" : "" };
  }

  function positionCards(exec, positions) {
    const cards = ((exec && exec.cards) || []).filter((c) => ["IN_TRADE", "EXITING", "ENTERING", "ATTENTION"].includes(c.key));
    const byId = {};
    for (const p of positions || []) byId[p.position_id] = p;
    return cards.map((c) => ({ card: c, position: c.position_id ? byId[c.position_id] || null : null, model: positionModel(c, c.position_id ? byId[c.position_id] : null, state.limits),
                                agent: agentModel(c, null, null, null) }));
  }

  // the ACTIVITY timeline: persisted rows of the earlier stages only; nothing is inferred and nothing is invented
  function timeline(src) {
    const items = [];
    const push = (at, text, cls, kind, symbol) => { if (at) items.push({ at: String(at), text, cls: cls || "", kind, symbol: symbol || "" }); };
    for (const t of (src.transitions || [])) push(t.evaluated_at, `${t.symbol} → ${t.to_state === "READY" ? "READY" : t.to_state === "WATCH" ? "WATCHING" : t.to_state === "INVALIDATED" ? "invalidated" : "no setup"}${t.to_state === "INVALIDATED" && t.reason_code ? ` · ${words(t.reason_code)}` : ""}`, t.to_state === "INVALIDATED" ? "warn" : "", "setup", t.symbol);
    for (const p of (src.proposals || [])) {
      push(p.created_at, `${p.symbol} · Proposal created${p.final_qty ? ` · ${p.final_qty} shares` : ""}`, "", "proposal", p.symbol);
      if (p.status === "REVIEWABLE") push(p.created_at, `${p.symbol} · Risk checks passed`, "", "proposal", p.symbol);
      else if (p.status === "BLOCKED") push(p.created_at, `${p.symbol} · Blocked: ${words((p.blockers && p.blockers[0] && (p.blockers[0].code || p.blockers[0].control_id)) || "risk check")}`, "warn", "proposal", p.symbol);
    }
    for (const i of (src.intents || [])) {
      const side = i.side === "sell" ? "SELL" : "BUY";
      if (i.submitted_at) push(i.submitted_at, `${i.symbol} · ${side} submitted · ${i.qty} shares`, "", "order", i.symbol);
      if (i.state === "FILLED" && i.resolved_at) push(i.resolved_at, `${i.symbol} · ${side} filled${i.filled_avg_price ? ` @ ${i.filled_avg_price}` : ""}`, "", "fill", i.symbol);
      else if (["REJECTED", "CANCELED", "NOT_SENT", "FAILED"].includes(i.state) && (i.resolved_at || i.updated_at)) push(i.resolved_at || i.updated_at, `${i.symbol} · ${side} ${words(i.state)}${i.failure_code ? ` · ${words(i.failure_code)}` : ""}`, i.state === "FAILED" ? "bad" : "warn", "order", i.symbol);
      else if (i.state === "RECONCILIATION_REQUIRED") push(i.updated_at, `${i.symbol} · ${side} order needs reconciliation`, "bad", "order", i.symbol);
    }
    const sym = {};
    for (const p of (src.positions || [])) sym[p.position_id] = p.symbol;
    for (const e of (src.position_events || [])) {
      const s = sym[e.position_id] || "";
      if (e.kind === "OPENED") push(e.at, `${s} · Position opened${e.reason ? ` · ${e.reason}` : ""}`, "", "position", s);
      else if (e.kind === "EXIT_REQUIRED") push(e.at, `${s} · Exit required · ${words(e.reason)}`, "", "exit", s);
      else if (e.kind === "CLOSED") push(e.at, `${s} · Trade closed${e.detail && e.detail.realized_pnl ? ` · P/L ${money(e.detail.realized_pnl)}` : ""}`, "", "exit", s);
      else if (e.kind === "ATTENTION") push(e.at, `${s} · Needs attention · ${words(e.reason)}`, "bad", "attention", s);
      else if (e.kind === "ATTENTION_CLEARED") push(e.at, `${s} · Attention cleared`, "", "attention", s);
    }
    const SAFETY_EVENT = { PAUSE_ON: ["New trades paused", "warn"], PAUSE_OFF: ["New trades resumed", ""], KILL_ON: ["Trading stopped (emergency stop)", "bad"], KILL_OFF: ["Emergency stop cleared", ""],
                           DAILY_HALT_TRIGGERED: ["Daily loss limit reached · trading halted", "bad"], DAILY_RESET: ["New trading day · daily halt reset", ""], BASELINE_CREATED: ["Daily risk baseline set", ""],
                           EVALUATOR_ERROR: ["Risk evaluator error", "bad"], STARTUP: ["Risk controls started", ""] };
    for (const e of (src.safety_events || [])) { const m = SAFETY_EVENT[e.kind]; if (m) push(e.at, m[0], m[1], "safety"); }
    for (const f of (src.findings || [])) if (f.blocking) push(f.at, `${f.symbol ? f.symbol + " · " : ""}Reconciliation: ${words(f.kind)}`, "bad", "reconciliation", f.symbol);
    const FV = { SESSION_OPENED: "Validation session started", SESSION_CLOSED: "Validation session closed", SESSION_NOT_RUN: "Validation session not run", RESTART_DETECTED: "Bot restart detected",
                 ATTENTION_RAISED: "Validation attention", EARLY_STOP: "Validation early stop · Paper Auto turned off", SUMMARY_REBUILT: "Validation summary rebuilt", OBSERVER_ERROR: "Validation observer error" };
    for (const e of (src.fv_events || [])) if (FV[e.kind]) push(e.at, `${FV[e.kind]}${e.code ? ` · ${words(e.code)}` : ""}`, e.kind === "EARLY_STOP" || e.kind === "ATTENTION_RAISED" ? "bad" : "", "validation");
    items.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
    return items;
  }

  function todaySummary(src, exec, today) {
    const d = (x) => day(x) === today;
    const intents = (src.intents || []).filter((i) => i.intent_type === "ENTRY" && i.submitted_at && d(i.submitted_at));
    const closed = (src.positions || []).filter((p) => p.state === "CLOSED" && d(p.closed_at));
    const pnl = closed.reduce((s, p) => s + (num(p.realized_pnl) || 0), 0);
    const issues = ((exec && exec.attention_reasons) || []).length + (src.findings || []).filter((f) => f.blocking && d(f.at)).length +
      (src.safety_events || []).filter((e) => ["KILL_ON", "DAILY_HALT_TRIGGERED", "EVALUATOR_ERROR"].includes(e.kind) && d(e.at)).length;
    return { trades: intents.length, open: (exec && exec.open_positions) || 0, closed: closed.length, pnl: closed.length ? money(pnl) : "—", issues };
  }

  function moreGroups(labViews) {
    return MORE.map((g) => ({ title: g.title, items: g.items.flatMap((it) => (it.lab && labViews && labViews.length ? [it, ...labViews.map((v) => ({ tab: "strategy", lab: v.view, label: `· ${v.label}` }))] : [it])) }));
  }

  // ---------------------------------------------------------------- rendering ------------------------------------------------------
  function statusHtml(exec, safety) {
    return statusModel(exec, safety).map((s) => `<span class="pui-seg ${esc(s.cls)}" data-pui-seg="${esc(s.key)}">${esc(s.text)}</span>`).join("") + `<span class="pui-tagline" data-pui-tagline>${esc(TAGLINE)}</span>`;
  }

  function cardsHtml(ctx) {
    const a = agentModel(ctx.card, ctx.dom.setupState, ctx.safety && ctx.safety.state, ctx.exec);
    const m = marketModel(ctx.dom);
    const r = riskModel(ctx.exec, ctx.safety);
    const p = positionModel(ctx.card, ctx.position, ctx.limits);
    const mode = ctx.exec && ctx.exec.mode;
    const autoOn = mode === "PAPER_AUTO";
    const enableLabel = ctx.enableLabel || "Enable Paper Auto";
    const sym = ctx.symbol || "—";
    const agent = `<div class="pui-card agent" data-pui-card="agent" data-agent="${esc(a.label)}"><h4>Agent · ${esc(sym)}</h4><div class="pui-big">${esc(a.label)}</div><div class="pui-sub">${esc(a.text)}</div>` +
      `<div class="pui-sub">${esc(autoOn ? "Paper Auto on" : "Paper Auto off")}${ctx.exec && ctx.exec.open_positions ? ` · ${ctx.exec.open_positions} position${ctx.exec.open_positions === 1 ? "" : "s"} open` : ""}</div></div>`;
    const market = `<div class="pui-card market" data-pui-card="market"><h4>Market condition · ${esc(sym)}</h4><div class="pui-kv"><span>PPE</span><b>${esc(m.ppe)}</b><span>Demand</span><b>${esc(m.demand)}</b><span>Evidence</span><b>${esc(m.evidence)}</b></div>` +
      `<div class="pui-line">Setup <b>${esc(m.setup)}</b>${m.proposal ? ` · ${esc(m.proposal)}` : ""}</div></div>`;
    const risk = `<div class="pui-card risk" data-pui-card="risk" data-risk="${esc(r.level)}"><h4>Risk</h4><div class="pui-big">${esc(r.headline)}</div>` +
      r.lines.slice(1).map((l) => `<div class="pui-line ${l.level === "ok" ? "pui-ok" : l.level === "warn" ? "pui-warn" : "pui-bad"}">${l.ok ? "✓" : "⚠"} ${esc(l.text)}</div>`).join("") +
      `<div class="pui-row"><button type="button" class="pui-btn stop" data-pui="stop"${r.stopped || ctx.stopAvailable === false ? " disabled" : ""}>${r.stopped ? "TRADING STOPPED" : "STOP TRADING"}</button>` +
      (r.stopped ? `<span class="pui-sub">Clear it under View details (Risk Controls)</span>` : "") + `</div></div>`;
    const pos = p.open ? `<div class="pui-card position" data-pui-card="position" data-open="1"><h4>Current position</h4><div class="pui-big">${esc(p.symbol)} · ${esc(p.shares)} shares</div>` +
      `<div class="pui-kv"><span>Entry</span><b>${esc(p.entry)}</b><span>Current</span><b>${esc(p.current)}</b><span>P/L</span><b>${esc(p.pnl)}</b><span>Holding</span><b>${esc(p.holding)}</b><span>Status</span><b>${esc(p.status)}</b></div>` +
      (p.reason ? `<div class="pui-sub">${esc(p.reason)}</div>` : "") + `</div>`
      : `<div class="pui-card position" data-pui-card="position" data-open="0"><h4>Current position</h4><div class="pui-big">${esc(p.entering ? "Entering" : "No position")}</div><div class="pui-sub">${esc(p.entering ? "Order submitted / awaiting fill" : (sym !== "—" ? `No paper position in ${sym}` : ""))}</div></div>`;
    const auto = `<div class="pui-card auto" data-pui-card="auto" data-auto="${autoOn ? "ON" : "OFF"}"><h4>Paper Auto</h4><div class="pui-big ${autoOn ? "pui-ok" : ""}">${autoOn ? "ON" : "OFF"}</div>` +
      `<div class="pui-sub">${esc(autoOn ? "The bot may open paper positions when every check passes" : "Watching only; no paper orders")}</div>` +
      `<div class="pui-row">${ctx.exec && ctx.exec.mode ? (autoOn ? `<button type="button" class="pui-btn danger" data-pui="auto-off"${ctx.autoAvailable === false ? " disabled" : ""}>Stop New Trades</button>` : `<button type="button" class="pui-btn go" data-pui="auto-on"${ctx.autoAvailable === false ? " disabled" : ""}>${esc(enableLabel)}</button>`) : ""}</div></div>`;
    return agent + market + risk + pos + auto;
  }

  function positionsHtml(rows, openId) {
    if (!rows.length) return `<div class="pui-empty" data-pui-empty>No open paper position. The Agent opens one only when a setup is READY and every risk check passes.</div>`;
    return `<div class="pui-grid">` + rows.map(({ card, model, agent }) => `<div class="pui-pos" data-pui-pos="${esc(card.symbol)}" data-position-id="${esc(card.position_id || "")}">` +
      `<div class="pui-sym"><span>${esc(card.symbol)}</span><span class="pui-state">${esc(agent.label)}</span></div>` +
      (model.open ? `<div class="pui-kv"><span>Quantity</span><b>${esc(model.shares)}</b><span>Entry</span><b>${esc(model.entry)}</b><span>Current</span><b>${esc(model.current)}</b><span>P/L</span><b>${esc(model.pnl)}</b><span>Holding</span><b>${esc(model.holding)}</b></div>`
                  : `<div class="pui-sub">${esc(card.line || agent.text)}</div>`) + `<div class="pui-sub">${esc(card.reason || "")}</div></div>`).join("") + `</div>`;
  }

  function positionDetailsHtml(d) {
    const kv = (rows) => `<div class="pui-kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><b>${esc(v == null || v === "" ? "—" : v)}</b>`).join("")}</div>`;
    const ev = (d.events || []).slice(-8).map((e) => `<li>${esc(hm(e.at))} · ${esc(words(e.kind))}${e.to_state ? ` → ${esc(words(e.to_state))}` : ""}${e.reason ? ` · ${esc(words(e.reason))}` : ""}</li>`).join("");
    const intents = (d.intents || []).map((i) => `<li>${esc(i.intent_type)} ${esc(i.side)} ${esc(i.qty)} · ${esc(words(i.state))}${i.filled_qty ? ` · filled ${esc(i.filled_qty)} @ ${esc(i.filled_avg_price || "—")}` : ""}${i.broker_order_id ? ` · broker ${esc(String(i.broker_order_id).slice(0, 8))}` : ""}</li>`).join("");
    return `<div class="pui-pos-details" data-pui-pos-details="${esc(d.symbol)}"><b>${esc(d.symbol)}</b> · ${esc(d.agent)}` +
      kv([["Setup state", d.setup], ["Demand", d.demand], ["Evidence", d.evidence], ["Exit monitoring", d.exit], ["Risk", d.risk], ["Position state", d.state], ["Invalidation level", d.invalidation]]) +
      `<div class="pui-sub">Broker orders and fills</div><ul>${intents || "<li>none</li>"}</ul><div class="pui-sub">Recent events</div><ul>${ev || "<li>none</li>"}</ul></div>`;
  }

  function activityHtml(src, exec, fv, today) {
    const s = todaySummary(src, exec, today);
    const items = timeline(src).slice(0, 120);
    let last = "";
    const rows = items.map((it) => { const d = day(it.at); const head = d !== last ? `<li class="pui-day">${esc(d === today ? "Today" : d)}</li>` : ""; last = d; return head + `<li class="${esc(it.cls)}" data-pui-kind="${esc(it.kind)}"><time>${esc(hm(it.at))}</time><span>${esc(it.text)}</span></li>`; }).join("");
    const FVlib = G.ForwardValidation || null;
    const fvm = fv && FVlib ? FVlib.summaryModel(fv) : null;
    const val = `<div class="pui-validation" data-pui-validation><h3>Validation</h3>` + (fvm && fvm.loaded ?
      `<div class="pui-kv"><span>Sessions</span><b>${esc(fvm.sessionsLabel)}</b><span>Healthy</span><b>${esc(fvm.healthy)}</b><span>Duplicates</span><b>${esc(fvm.duplicates)}</b><span>Recon issues</span><b>${esc(fvm.reconIssues)}</b></div>` +
      `<div class="pui-line" style="margin-top:6px">Strategy <b>${esc(fvm.edgeLabel)}</b>${fvm.insufficient ? ` <span class="pui-sub">${esc(fvm.insufficientText)}</span>` : ""}</div>` +
      (fvm.needsAttention ? `<div class="pui-bad">⚠ ${esc(fvm.critical ? "NEEDS ATTENTION" : "Needs a look")}: ${fvm.attention.map((a) => esc(words(a.code))).join(", ")}</div>` : "")
      : `<div class="pui-empty">Validation status unavailable.</div>`) + `<div><a href="forward_validation.html" target="_blank" rel="noopener" data-pui-fv-link>View validation details</a></div></div>`;
    return `<h2>Activity</h2><div class="pui-today" data-pui-today>` + [["Trades", s.trades], ["Open", s.open], ["Closed", s.closed], ["Paper P/L", s.pnl], ["Issues", s.issues]]
      .map(([k, v]) => `<div class="pui-card"><h4>${esc(k)}</h4><div class="pui-big ${k === "Issues" && v ? "pui-bad" : ""}">${esc(v)}</div></div>`).join("") + `</div>` +
      `<ul class="pui-timeline" data-pui-timeline>${rows || '<li class="pui-day">No activity recorded yet.</li>'}</ul>` + val +
      `<details class="pui-fold" data-pui-alerts-fold><summary>Rule alerts and setup alerts (legacy lists)</summary></details>`;
  }

  function moreHtml(groups) {
    return `<h2>More</h2><p class="pui-sub">Research, tools and system views. Nothing here is removed; it is simply not part of the daily TRADE workflow.</p><div class="pui-more">` +
      groups.map((g) => `<div class="pui-more-group" data-pui-group="${esc(g.title)}"><h3>${esc(g.title)}</h3>` + g.items.map((it) =>
        it.href ? `<a href="${esc(it.href)}" target="_blank" rel="noopener" data-pui-more="${esc(it.href)}">${esc(it.label)}</a>`
        : `<button type="button" data-pui-more="${esc(it.action ? "action:" + it.action : "tab:" + it.tab + (it.lab && it.lab !== true ? ":" + it.lab : ""))}">${esc(it.label)}</button>`).join("") +
        (g.title === "Settings" ? `<span class="pui-sub">Server-side configuration (config.py / .env). Secrets are never shown.</span>` : "") + `</div>`).join("") + `</div>` +
      `<div id="pui-more-body"></div>`;
  }

  // ---------------------------------------------------------------- page wiring (browser only) ---------------------------------------
  const $ = (id) => document.getElementById(id);
  const q = (sel) => document.querySelector(sel);
  function legacyTab(name) { return q(`.tab-btn[data-tab="${name}"]`); }
  function activeLegacyTab() { const b = q(".tab-btn.active"); return b ? b.dataset.tab : null; }
  function exec() { return (window.PaperBot && window.PaperBot.state && window.PaperBot.state.data) || null; }
  function safety() { return (window.RiskControls && window.RiskControls.state && window.RiskControls.state.data) || null; }
  function activeSymbol() { const tv = window.TraderView; return (tv && tv.state && tv.state.active) || null; }
  function cardFor(sym) { const e = exec(); return sym && e && e.cards ? e.cards.find((c) => c.symbol === sym) || null : null; }
  function readDom() {
    const txt = (sel) => { const e = q(sel); return e ? e.textContent.trim() : null; };
    const ds = (sel) => { const e = q(sel); return e ? e.dataset.state || null : null; };
    const evState = ds("#tv-evidence");
    const evRow = q("#tv-evidence table.tv-kv tr td:last-child");                       // the Strength row of the loaded evidence table
    return { stretch: txt('#tv-ppe [data-ppe="stretch"] .tv-ppe-val'), confidence: txt('#tv-ppe [data-ppe="confidence"] .tv-ppe-val'), demand: txt('#tv-demand [data-demand="score"] .tv-dem-val'), evidence: evState,
             evidenceText: evRow ? evRow.textContent.trim() : null, setupState: ds("#tv-setup"), proposalState: ds("#tv-proposal") };
  }
  function enableLabel() { const b = q('#tv-bot [data-bot="enable"]'); return b ? b.textContent.trim() : "Enable Paper Auto"; }
  const collapsed = () => { try { return localStorage.getItem("pui.details") !== "open"; } catch (e) { return true; } };
  function setCollapsed(v) { try { localStorage.setItem("pui.details", v ? "closed" : "open"); } catch (e) { /* per-browser convenience only */ } document.body.classList.toggle("pui-collapsed", v); const b = $("pui-details-btn"); if (b) { b.textContent = v ? "View details ▸" : "Hide details ▾"; b.setAttribute("aria-expanded", v ? "false" : "true"); } }

  function renderStatus() { const el = $("pui-status"); if (el) el.innerHTML = statusHtml(exec(), safety()); }
  function renderCards() {
    const host = $("pui-cards");
    if (!host) return;
    const sym = activeSymbol();
    const card = cardFor(sym);
    let position = null;
    if (card && card.position_id && state.posData) position = (state.posData.positions || []).find((p) => p.position_id === card.position_id) || null;
    const stopBtn = q('#tv-safety [data-safety="stop"]'), autoBtn = q('#tv-bot [data-bot="enable"], #tv-bot [data-bot="disable"]');
    host.innerHTML = cardsHtml({ exec: exec(), safety: safety(), card, position, dom: readDom(), symbol: sym, limits: state.limits, enableLabel: enableLabel(),
                                 stopAvailable: !!stopBtn && !stopBtn.disabled, autoAvailable: !!autoBtn && !autoBtn.disabled });
  }
  function ensureTradeCards() {
    const grid = q("#tv-body .tv-grid");
    if (!grid || $("pui-cards")) return !!$("pui-cards");
    const cards = document.createElement("div");
    cards.id = "pui-cards";
    cards.className = "pui-cards";
    grid.insertAdjacentElement("beforebegin", cards);
    cards.addEventListener("click", onCardClick);
    setCollapsed(collapsed());
    return true;
  }
  function onCardClick(e) {
    const b = e.target.closest("[data-pui]");
    if (!b || b.disabled) return;
    const k = b.dataset.pui;
    // the actions delegate to the existing Risk Controls / Paper Bot buttons: identical POSTs, identical two-click Paper Auto confirmation
    if (k === "stop") { const t = q('#tv-safety [data-safety="stop"]'); if (t && !t.disabled) t.click(); }
    if (k === "auto-on") { const t = q('#tv-bot [data-bot="enable"]'); if (t && !t.disabled) t.click(); }
    if (k === "auto-off") { const t = q('#tv-bot [data-bot="disable"]'); if (t && !t.disabled) t.click(); }
    setTimeout(renderCards, 50);
    setTimeout(renderCards, 400);
  }

  async function getJson(path) { const r = await fetch(path); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`); return j; }
  async function loadPositions() {
    try { state.posData = await getJson("/api/execution/positions?open_only=true&limit=100"); if (window.PaperBot) await window.PaperBot.refresh(); state.error = null; }
    catch (e) { state.error = `Positions unavailable (${e.message}).`; }
    renderPositions();
  }
  function renderPositions() {
    const host = $("pui-positions");
    if (!host) return;
    const rows = positionCards(exec(), state.posData && state.posData.positions);
    host.innerHTML = `<h2>Positions</h2>` + (state.error ? `<div class="pui-bad">${esc(state.error)}</div>` : "") + positionsHtml(rows) + `<div id="pui-pos-detail"></div>` +
      `<details class="pui-fold"><summary>Closed paper trades (history)</summary><div class="pui-sub" data-pui-history>${esc(((exec() || {}).recent || []).map((h) => window.PaperBot ? window.PaperBot.historyLine(h) : "").join("\n") || "No closed paper trade yet.")}</div></details>`;
    if (state.posDetails) renderPositionDetails(state.posDetails);
  }
  async function openPosition(symbol, positionId) {
    const e = exec();
    const card = cardFor(symbol);
    const p = state.posData ? (state.posData.positions || []).find((x) => x.position_id === positionId) : null;
    let setup = null, demand = null, evidence = null, intents = [];
    try { const st = await getJson(`/api/terminal/state/${encodeURIComponent(symbol)}?candles=0`); setup = st.setup && st.setup.state_label; demand = st.demand && st.demand.score != null ? String(st.demand.score) : "—"; evidence = st.evidence && st.evidence.status === "LOADED" ? (st.evidence.strength || "stored") : "none stored"; } catch (err) { /* summary stays partial */ }
    try { intents = (await getJson(`/api/execution/intents?symbol=${encodeURIComponent(symbol)}&limit=20`)).intents || []; } catch (err) { /* summary stays partial */ }
    const sv = safety();
    state.posDetails = { symbol, agent: agentModel(card, null, null, null).label, setup: setup || "—", demand, evidence, exit: p ? (p.exit_reason ? `${words(p.exit_reason)} · cycle ${p.exit_cycle}` : `monitored · ${words(p.state)}`) : "—",
                         risk: sv ? SAFETY_TEXT[sv.state] || sv.state : "—", state: p ? p.state : (card && card.state) || "—", invalidation: p ? p.invalidation_price : (card && card.details && card.details.invalidation_price) || null,
                         events: (card && card.details && card.details.events) || (state.posData && (state.posData.events || []).filter((ev) => ev.position_id === positionId)) || [], intents };
    renderPositionDetails(state.posDetails);
    if (e) renderPositions();
  }
  function renderPositionDetails(d) { const host = $("pui-pos-detail"); if (host) host.innerHTML = positionDetailsHtml(d); }

  async function loadActivity() {
    const get = (p) => getJson(p).catch(() => ({}));
    const [tr, pr, ex, po, se, fi, fv, fve] = await Promise.all([get("/api/setups/events?limit=100"), get("/api/proposals/history?limit=50"), get("/api/execution/intents?limit=100"),
                                                                get("/api/execution/positions?limit=100"), get("/api/execution-safety/events?limit=50"), get("/api/execution/findings?limit=50"),
                                                                get("/api/forward-validation"), get("/api/forward-validation/events?limit=50")]);
    state.actData = { src: { transitions: tr.transitions || [], proposals: pr.proposals || [], intents: ex.intents || [], positions: po.positions || [], position_events: po.events || [],
                             safety_events: se.events || [], findings: fi.findings || [], fv_events: fve.events || [] }, fv: fv && fv.sessions ? fv : null };
    renderActivity();
  }
  function renderActivity() {
    const host = $("pui-activity");
    if (!host || !state.actData) return;
    const today = (exec() && exec().scheduler && exec().scheduler.market && exec().scheduler.market.session_date) || new Date().toISOString().slice(0, 10);
    host.innerHTML = activityHtml(state.actData.src, exec(), state.actData.fv, today);
    const fold = host.querySelector("[data-pui-alerts-fold]");
    const legacy = $("pui-legacy-alerts");
    if (fold && legacy) fold.appendChild(legacy);
  }

  function labViews() { return [...document.querySelectorAll("#sl-subnav [data-sfv]")].map((b) => ({ view: b.dataset.sfv, label: b.textContent.trim() })).filter((v) => v.view && v.label); }
  function renderMore() {
    const host = $("pui-more");
    if (!host) return;
    const body = $("pui-more-body");
    const keep = body ? body.innerHTML : "";
    host.innerHTML = moreHtml(moreGroups(labViews()));
    if (keep) $("pui-more-body").innerHTML = keep;
  }
  function onMoreClick(e) {
    const b = e.target.closest("[data-pui-more]");
    if (!b || b.tagName === "A") return;
    const [kind, name, lab] = b.dataset.puiMore.split(":");
    if (kind === "tab") { openLegacy(name); if (lab) setTimeout(() => { const v = q(`#sl-subnav [data-sfv="${lab}"]`); if (v) v.click(); }, 300); return; }
    if (kind === "action") {
      const body = $("pui-more-body");
      if (name === "settings") { const p = q("#tab-settings .unavailable-panel"); if (p) p.classList.toggle("pui-shown"); return; }
      if (name === "risk") { body.innerHTML = `<h3>Risk Controls</h3><p class="pui-sub">The persisted execution safety gate. Pause / Resume / Emergency Stop live in TRADE (Risk card and View details). Current state:</p><pre class="pui-sub" data-pui-health>${esc(JSON.stringify(safety() ? { state: safety().state, label: safety().state_label, reasons: safety().reason_codes, evaluated_at: safety().evaluated_at } : { state: "unavailable" }, null, 1))}</pre>`; return; }
      if (name === "health") { getJson("/api/account-refresh").then((a) => { body.innerHTML = `<h3>Account / execution health</h3><pre class="pui-sub" data-pui-health>${esc(JSON.stringify({ account: { status: a.status, label: a.label, last_success_at: a.last_success_at, consecutive_failures: a.consecutive_failures, loop: a.loop && a.loop.running }, execution: exec() ? { mode: exec().mode, reconciliation_healthy: exec().reconciliation_healthy, last_cycle_at: exec().last_cycle_at, loop: exec().scheduler && exec().scheduler.running } : "unavailable" }, null, 1))}</pre>`; }).catch((err) => { body.innerHTML = `<div class="pui-bad">Health unavailable (${esc(err.message)}).</div>`; }); }
    }
  }

  function openLegacy(tab) { const b = legacyTab(tab); if (b) b.click(); }
  function go(section) {
    const nav = NAV.find((n) => n.id === section) || NAV[0];
    state.section = nav.id;
    openLegacy(nav.host);
    syncNav();
    if (nav.id === "positions") loadPositions();
    if (nav.id === "activity") loadActivity();
    if (nav.id === "more") renderMore();
    if (nav.id === "trade" && ensureTradeCards()) renderCards();
  }
  function syncNav() {
    const tab = activeLegacyTab();
    const section = HOST_OF[tab] || (tab ? "more" : state.section);
    state.legacyTab = tab;
    document.querySelectorAll("#pui-nav [data-pui-nav]").forEach((b) => b.classList.toggle("active", b.dataset.puiNav === section));
    const crumb = $("pui-crumb");
    if (crumb) {
      const legacyName = tab && !HOST_OF[tab] ? (legacyTab(tab) || {}).textContent : "";
      crumb.innerHTML = legacyName ? `${esc(legacyName.trim())} · <button type="button" data-pui-back>Back to MORE</button>` : "";
    }
    document.body.dataset.puiSection = section;
  }

  function tick() {
    renderStatus();
    if (activeLegacyTab() === "trader") { if (ensureTradeCards()) renderCards(); }
  }

  function mount() {
    if (typeof document === "undefined" || state.booted) return;
    if (!q("nav.tabs") || !$("tab-trader")) return;
    state.booted = true;
    document.body.classList.add("pui");
    const tabs = q("nav.tabs");
    const status = document.createElement("div"); status.id = "pui-status"; status.className = "pui-status"; status.setAttribute("role", "status");
    const nav = document.createElement("nav"); nav.id = "pui-nav"; nav.className = "pui-nav"; nav.setAttribute("aria-label", "Primary");
    nav.innerHTML = NAV.map((n) => `<button type="button" data-pui-nav="${n.id}">${n.label}</button>`).join("") + `<span class="pui-crumb" id="pui-crumb"></span>` +
      `<span class="pui-tools"><button type="button" id="pui-details-btn" class="pui-link" aria-expanded="false">View details ▸</button></span>`;
    tabs.insertAdjacentElement("beforebegin", status);
    tabs.insertAdjacentElement("afterend", nav);
    nav.addEventListener("click", (e) => { const b = e.target.closest("[data-pui-nav]"); if (b) { go(b.dataset.puiNav); return; } if (e.target.closest("[data-pui-back]")) go("more");
                                           if (e.target.closest("#pui-details-btn")) setCollapsed(!collapsed()); });
    setCollapsed(collapsed());
    // POSITIONS / ACTIVITY / MORE live inside legacy panels that only held placeholders or the alert lists, so the legacy tab logic keeps showing one panel at a time
    const pos = document.createElement("section"); pos.id = "pui-positions"; pos.className = "pui-section"; $("tab-paper").appendChild(pos);
    pos.addEventListener("click", (e) => { const c = e.target.closest("[data-pui-pos]"); if (c) openPosition(c.dataset.puiPos, c.dataset.positionId || null); });
    const act = document.createElement("section"); act.id = "pui-activity"; act.className = "pui-section";
    const alertsHost = $("tab-alerts");
    const legacyAlerts = document.createElement("div"); legacyAlerts.id = "pui-legacy-alerts";
    while (alertsHost.firstChild) legacyAlerts.appendChild(alertsHost.firstChild);          // the rule alerts and setup alerts stay mounted, inside a fold
    alertsHost.appendChild(act); alertsHost.appendChild(legacyAlerts);
    const more = document.createElement("section"); more.id = "pui-more"; more.className = "pui-section"; $("tab-settings").insertAdjacentElement("afterbegin", more);
    more.addEventListener("click", onMoreClick);
    const pf = $("tab-portfolio");
    if (pf && !$("pui-pf-note")) { const n = document.createElement("div"); n.id = "pui-pf-note"; n.className = "pui-note";
      n.textContent = "Robinhood holdings are read only (local gateway): nothing in this app places, changes or cancels a Robinhood order. Paper orders go to the Alpaca PAPER account only."; pf.insertAdjacentElement("afterbegin", n); }
    document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => setTimeout(syncNav, 0)));
    getJson("/api/execution").then((o) => { state.limits = o.limits || null; }).catch(() => { state.limits = null; });
    // a deep link (e.g. the Daily Brief notification's /#daily-brief, consumed by brief_delivery.js) or a module that already switched the tab wins over
    // the TRADE default; the nav then simply follows the active legacy tab (MORE + crumb)
    const already = activeLegacyTab();
    if (location.hash || (already && already !== "overview")) { state.section = HOST_OF[already] || "more"; syncNav(); } else go(DEFAULT);
    tick();
    state.timer = setInterval(tick, 2000);
    state.fetchTimer = setInterval(() => { if (state.section === "positions" && activeLegacyTab() === "paper") loadPositions(); if (state.section === "activity" && activeLegacyTab() === "alerts") loadActivity(); }, 30000);
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
  }
  return { NAV, MORE, TAGLINE, DEFAULT, AGENT, SETUP_TO_AGENT, SAFETY_TEXT, agentModel, statusModel, riskModel, positionModel, marketModel, positionCards, timeline, todaySummary, moreGroups,
           statusHtml, cardsHtml, positionsHtml, positionDetailsHtml, activityHtml, moreHtml, go, mount, tick, renderCards, renderPositions, renderActivity, renderMore, state,
           expandDetails: () => setCollapsed(false), collapseDetails: () => setCollapsed(true), isCollapsed: collapsed };
});
