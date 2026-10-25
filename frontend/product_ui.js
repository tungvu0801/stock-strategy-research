// Stage 6.1.1 — PRODUCT UI. Four primary sections (TRADE · POSITIONS · ACTIVITY · MORE) layered over the existing dashboard: the legacy tab
// bar is hidden, every legacy workspace stays reachable through MORE, TRADE opens by default on the Trader View with a persistent status bar
// (PAPER · MARKET · SYSTEM · AUTO), an Agent / Setup card (WATCHING · READY · ENTERING · IN TRADE · EXITING · CLOSED · ENTRY FAILED · NEEDS
// ATTENTION), a Position card, a Risk card with the Automation / Trading Controls (Paper Auto, Pause / Resume New Trades, Emergency Stop /
// Clear Emergency Stop), a compact chart empty state and one diagnostics line; the legacy panels become "View details".
// Display only: every number comes from the existing read-only state (Paper Bot, Risk Controls, Trader View) or the existing GET routes. Every
// action delegates to the existing Risk Controls / Paper Bot buttons (the same POSTs, the same persisted semantics): Paper Auto enable and Clear
// Emergency Stop keep their legacy two-click confirmation (the shell shows the explanation first, then performs the legacy clicks), Emergency Stop
// gets an explanatory confirmation in front of the legacy one-click button, Disable Auto / Pause / Resume are direct. Nothing here decides a state
// or forces one: SYSTEM HEALTHY / DEGRADED / STOPPED and every control status are display summaries of the gate, account, cash-flow,
// reconciliation and attention fields. Loaded as a classic script (window.ProductUI) and required by the node tests.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  root.ProductUI = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const G = typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : {});
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const words = (x) => (x == null || x === "" ? "" : String(x).replace(/_/g, " ").toLowerCase());
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const num = (x) => { const n = Number(x); return x == null || x === "" || !isFinite(n) ? null : n; };
  const money = (x) => { const n = num(x); return n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}`; };
  const hm = (iso) => (iso ? String(iso).slice(11, 16) : "—");
  const day = (iso) => (iso ? String(iso).slice(0, 10) : "");

  const NAV = [{ id: "trade", label: "TRADE", host: "trader" }, { id: "positions", label: "POSITIONS", host: "paper" }, { id: "activity", label: "ACTIVITY", host: "alerts" }, { id: "more", label: "MORE", host: "settings" }];
  const HOST_OF = { trader: "trade", paper: "positions", alerts: "activity", settings: "more" };
  const TAGLINE = "Paper trading only. Strategy and risk decisions are deterministic. Live trading is disabled.";
  const DEFAULT = "trade";
  const MAX_HOLD_DEFAULT = 5;
  const PENDING_GRACE_MS = 8000;                                               // "Loading candles" longer than this is shown as unavailable
  const CONFIRM_MS = 20000;                                                    // an open confirmation panel closes by itself
  // the user-facing state model: the Stage 6.0 card keys / Stage 5.8 setup states mapped for display only (the backend state machines are untouched)
  const AGENT = { WATCHING: ["WATCHING", "Waiting for setup conditions"], READY: ["READY", "Eligible for entry review"], ENTERING: ["ENTERING", "Order submitted. Waiting for broker reconciliation."],
                  IN_TRADE: ["IN TRADE", "Existing trade continues. Exit conditions monitored."], EXITING: ["EXITING", "Exit required / order submitted"], CLOSED: ["CLOSED", "Trade completed."],
                  ATTENTION: ["NEEDS ATTENTION", "Manual review required"], STOPPED: ["NEEDS ATTENTION", "Trading stopped by risk controls"], NONE: ["WATCHING", "Waiting for setup conditions"],
                  ENTRY_FAILED: ["ENTRY FAILED", "No position opened. No automatic replacement order."] };
  const SETUP_TO_AGENT = { WATCH: "WATCHING", READY: "READY", INVALIDATED: "WATCHING", NONE: "WATCHING" };
  const SAFETY_TEXT = { TRADING_ALLOWED: "Trading allowed", NO_NEW_ENTRIES: "New trades paused", HALTED: "Trading stopped", UNKNOWN: "Risk status unknown" };
  const SAFETY_CLS = { TRADING_ALLOWED: "ok", NO_NEW_ENTRIES: "warn", HALTED: "bad", UNKNOWN: "bad" };
  const ACCOUNT_TEXT = { FRESH: "Account data fresh", STALE: "Account data stale", STARTING: "Account data checking", UNAVAILABLE: "Account data unavailable", DISABLED: "Account refresh disabled" };
  const CASH_TEXT = { FRESH: "fresh", STALE: "stale", UNAVAILABLE: "unavailable", AMBIGUOUS: "unclear" };
  const SYSTEM_CLS = { HEALTHY: "ok", DEGRADED: "warn", STOPPED: "bad", UNKNOWN: "" };
  // the control vocabulary (display) and the explanations shown before an action is delegated to the legacy button
  const AUTO_ON_TEXT = "Automatic paper entries enabled.";
  const AUTO_OFF_TEXT = "Monitoring only. No new automatic entries.";
  const AUTO_OFF_NOTE = "Existing positions continue to be monitored.";
  const AUTO_OFF_POSITION = "New automatic entries are disabled. Existing position monitoring continues.";
  const STOP_ACTIVE_HEAD = "EMERGENCY STOP ACTIVE";
  const STOP_ACTIVE_TEXT = "New entries blocked.";
  const STOP_ACTIVE_NOTE = "Existing positions remain protected.";
  const STOP_POSITION = "No new entries. Existing position remains protected.";
  const PAUSED_NOTE = "Existing positions continue normally.";
  const CONFIRM = {
    "auto-on": { title: "Enable Paper Auto?", text: "You are enabling automatic Alpaca PAPER trading. New eligible entries may be submitted automatically when all strategy and risk conditions pass. Live trading remains disabled.",
                 ok: "Confirm: enable Paper Auto", cls: "go" },
    stop: { title: "Emergency Stop?", text: "This immediately blocks all new entries. Existing positions remain monitored and protective exits remain allowed. The emergency stop stays active until cleared manually.",
            ok: "Confirm Emergency Stop", cls: "stop" },
    clear: { title: "Clear Emergency Stop?", text: "This removes the emergency-stop latch. Trading resumes only if all safety checks pass.", ok: "Confirm: clear Emergency Stop", cls: "danger" },
  };
  // MORE: every legacy workspace, grouped; nothing is deleted. Strategy Lab workspaces are read from its own sub-navigation at run time.
  const MORE = [
    { title: "Research", items: [{ tab: "strategy", label: "Strategy Lab", lab: true }, { tab: "setups", label: "Research Setups" }, { tab: "history", label: "Research History" }] },
    { title: "Tools", items: [{ tab: "agent", label: "AI Agent" }, { tab: "overview", label: "Market dashboard" }, { tab: "watchlist", label: "Watchlist editor" }, { tab: "daily", label: "Daily Review" }, { tab: "review", label: "Trader Review" }] },
    { title: "System", items: [{ href: "forward_validation.html", label: "Forward Validation" }, { action: "health", label: "Account / execution health" }, { action: "risk", label: "Risk Controls" },
                                 { tab: "portfolio", label: "Portfolio (Robinhood, read only)" }] },
    { title: "Settings", items: [{ action: "settings", label: "Advanced configuration" }] },
  ];
  const state = { section: null, legacyTab: null, timer: null, fetchTimer: null, posDetails: null, posData: null, actData: null, limits: null, booted: false, error: null,
                  whyOpen: false, riskOpen: false, posOpen: false, sysOpen: false, diagOpen: false, pendingSince: null, chartForced: null, chartKind: null, confirm: null, confirmTimer: null };

  // ---------------------------------------------------------------- pure models ------------------------------------------------------
  function agentModel(card, setupState, safetyState, exec) {
    const failed = !!(card && card.key === "ATTENTION" && card.state === "FAILED" && card.shares == null);   // a rejected / cancelled / unsent entry today, no position
    const key = failed ? "ENTRY_FAILED" : card && card.key && card.key !== "NONE" ? card.key : (SETUP_TO_AGENT[setupState] || "WATCHING");
    const [label, text] = AGENT[key] || AGENT.WATCHING;
    const stopped = safetyState === "HALTED" || safetyState === "UNKNOWN";
    const attention = !!(exec && exec.attention);
    let out = { key, label, text, cls: label.toLowerCase().replace(/ /g, "-") };
    if (attention && key !== "ATTENTION" && key !== "ENTRY_FAILED") out = { key: "ATTENTION", label: "NEEDS ATTENTION", text: (exec.attention_reasons && exec.attention_reasons[0]) || "Manual review required", cls: "needs-attention" };
    else if (stopped && !["IN_TRADE", "EXITING", "ENTERING", "ATTENTION", "ENTRY_FAILED"].includes(key)) out = { key: "STOPPED", label: "NEEDS ATTENTION", text: AGENT.STOPPED[1], cls: "needs-attention" };
    if (card && card.line && ["EXITING", "CLOSED", "ATTENTION"].includes(key) && out.key === key) out.text = card.line;
    return out;
  }

  // the setup line under the Agent state: plain words from the stored setup state; a count only from the engine's own blocker list (never invented)
  function setupLines(agent, dom) {
    const s = (dom && dom.setupState) || null;
    const blockers = (dom && dom.blockers) || [];
    switch (agent.key) {
      case "READY": return { line: "Setup conditions met", sub: "" };
      case "ENTERING": return { line: "", sub: "" };                                      // the state line already says "Order submitted …"
      case "IN_TRADE": return { line: "", sub: "" };                                      // "Existing trade continues …" is the state line
      case "EXITING": return { line: "Exit in progress", sub: "Waiting for the exit order to fill" };
      case "CLOSED": return { line: "Watching for the next setup", sub: "" };
      case "ENTRY_FAILED": return { line: "Watching for the next setup", sub: "" };
      case "ATTENTION": case "STOPPED": return { line: "Setup · on hold", sub: "" };
      default:
        if (s === "WATCH") return { line: "Setup · Watching", sub: blockers.length ? `${blockers.length} condition${blockers.length === 1 ? "" : "s"} outstanding` : "Waiting for conditions" };
        if (s === "INVALIDATED") return { line: "Setup · Invalidated", sub: "Waiting for a new setup" };
        if (s === "NONE") return { line: "Setup · No active setup", sub: "" };                  // the state line already says it is waiting
        return { line: "Setup", sub: "Waiting for conditions" };
    }
  }

  // what an existing position needs to hear when automation is off or the emergency stop is active (display only; the backend keeps monitoring it)
  function positionNote(card, exec, safety) {
    const live = !!(card && ["IN_TRADE", "EXITING"].includes(card.key));
    if (!live) return "";
    const k = (safety && safety.kill_switch) || {};
    if (k.active) return STOP_POSITION;
    if (exec && exec.mode && exec.mode !== "PAPER_AUTO") return AUTO_OFF_POSITION;
    return "";
  }

  // SYSTEM HEALTHY / DEGRADED / STOPPED: a display summary of the existing dependencies, in precedence order; every item stays listed in the detail
  function systemModel(exec, safety) {
    const st = (safety && safety.state) || (exec && exec.safety_state) || null;
    const acct = (exec && exec.account_data) || {};
    const cash = acct.cash_flow || null;
    const items = [];
    if (st) items.push({ key: "gate", level: SAFETY_CLS[st] || "bad", text: `Safety gate: ${SAFETY_TEXT[st] || words(st)}`, reason: (safety && safety.reason_text) || "" });
    for (const r of (exec && exec.attention_reasons) || []) items.push({ key: "attention", level: "bad", text: `Needs attention: ${words(r)}` });
    if (acct.status) items.push({ key: "account", level: acct.status === "FRESH" ? "ok" : "warn", text: ACCOUNT_TEXT[acct.status] || `Account data ${words(acct.status)}` });
    if (cash && cash.status) items.push({ key: "cash", level: cash.status === "FRESH" ? "ok" : "warn", text: `Cash-flow data ${CASH_TEXT[cash.status] || words(cash.status)}` });
    if (exec && exec.reconciliation_healthy != null) items.push({ key: "recon", level: exec.reconciliation_healthy ? "ok" : "bad", text: exec.reconciliation_healthy ? "Reconciliation healthy" : "Reconciliation needs a look" });
    const loaded = !!exec && !!st;
    const stopped = st === "HALTED" || st === "UNKNOWN" || !!(exec && exec.attention);
    const level = !loaded ? "UNKNOWN" : stopped ? "STOPPED" : items.some((i) => i.level !== "ok") ? "DEGRADED" : "HEALTHY";
    const primary = items.find((i) => i.level === "bad") || items.find((i) => i.level === "warn") || null;
    return { level, loaded, text: loaded ? `SYSTEM ${level}` : "SYSTEM —", cls: SYSTEM_CLS[level], items, primary: primary ? (primary.reason || primary.text) : "" };
  }

  function statusModel(exec, safety) {
    const env = (exec && exec.environment) || {};
    const market = (exec && exec.scheduler && exec.scheduler.market) || {};
    const mode = exec && exec.mode;
    const sys = systemModel(exec, safety);
    const paper = { key: "paper", text: env.simulated ? "SIMULATED PAPER" : "PAPER", cls: "ok" };
    const mk = market.regular_session == null ? { key: "market", text: "MARKET —", cls: "" } : { key: "market", text: market.regular_session ? "MARKET OPEN" : "MARKET CLOSED", cls: market.regular_session ? "ok" : "" };
    const system = { key: "system", text: sys.text, cls: sys.cls, level: sys.level };
    const auto = { key: "auto", text: mode === "PAPER_AUTO" ? "AUTO ON" : mode ? "AUTO OFF" : "AUTO —", cls: mode === "PAPER_AUTO" ? "ok" : "" };
    return [paper, mk, system, auto];
  }

  function riskModel(exec, safety) {
    const st = (safety && safety.state) || (exec && exec.safety_state) || "UNKNOWN";
    const acct = (exec && exec.account_data) || {};
    const cash = acct.cash_flow || null;
    const k = (safety && safety.kill_switch) || {};
    const lines = [];
    const head = k.active ? STOP_ACTIVE_HEAD : (SAFETY_TEXT[st] || SAFETY_TEXT.UNKNOWN);
    lines.push({ ok: st === "TRADING_ALLOWED", level: SAFETY_CLS[st] || "bad", text: SAFETY_TEXT[st] || SAFETY_TEXT.UNKNOWN });
    if (safety && safety.reason_text && st !== "TRADING_ALLOWED") lines.push({ ok: false, level: SAFETY_CLS[st] || "warn", text: safety.reason_text, sub: true });
    if (acct.status) lines.push({ ok: acct.status === "FRESH", level: acct.status === "FRESH" ? "ok" : "warn", text: ACCOUNT_TEXT[acct.status] || `Account data ${words(acct.status)}` });
    if (cash && cash.status) lines.push({ ok: cash.status === "FRESH", level: cash.status === "FRESH" ? "ok" : "warn", text: `Cash-flow data ${CASH_TEXT[cash.status] || words(cash.status)}` });
    if (exec && exec.reconciliation_healthy != null) lines.push({ ok: !!exec.reconciliation_healthy, level: exec.reconciliation_healthy ? "ok" : "bad", text: exec.reconciliation_healthy ? "Reconciliation healthy" : "Reconciliation needs a look" });
    for (const r of (exec && exec.attention_reasons) || []) lines.push({ ok: false, level: "bad", text: r });
    const level = lines.some((l) => l.level === "bad") ? "bad" : lines.some((l) => l.level === "warn") ? "warn" : "ok";
    // one primary reason: the gate's own sentence, else the first dependency that is not healthy; stopped states say so plainly
    const dep = lines.slice(1).find((l) => l.level === "bad") || lines.slice(1).find((l) => l.level === "warn") || null;
    const paused = st === "NO_NEW_ENTRIES" && !!((safety && safety.no_new_entries) || {}).active;
    const primary = k.active ? `${STOP_ACTIVE_TEXT} ${STOP_ACTIVE_NOTE}` : paused ? PAUSED_NOTE
      : st === "TRADING_ALLOWED" ? (dep ? dep.text : "") : (safety && safety.reason_text) || (st === "HALTED" || st === "UNKNOWN" ? "Critical issue requires attention" : (dep ? dep.text : ""));
    return { state: st, headline: head, level, lines, stopped: st === "HALTED", killActive: !!k.active, paused, primary, critical: st === "HALTED" || st === "UNKNOWN" || !!(exec && exec.attention) };
  }

  // the Automation / Trading Controls rows: statuses from the payloads, exactly one relevant action per row, mirroring the legacy buttons' availability
  function controlsModel(exec, safety, avail) {
    const a = avail || {};
    const mode = exec && exec.mode;
    const on = mode === "PAPER_AUTO";
    const st = (safety && safety.state) || (exec && exec.safety_state) || null;
    const k = (safety && safety.kill_switch) || {};
    const p = (safety && safety.no_new_entries) || {};
    const d = (safety && safety.daily_loss) || {};
    const auto = { label: !mode ? "—" : on ? "ON" : "OFF", text: !mode ? "Paper Auto status not loaded." : on ? AUTO_ON_TEXT : `${AUTO_OFF_TEXT} ${AUTO_OFF_NOTE}`,
                   action: !mode ? null : on ? "auto-off" : "auto-on", actionLabel: on ? "Disable Auto" : "Enable Paper Auto", enabled: a.auto !== false };
    let tLabel = "UNKNOWN", tText = "Risk status unknown";
    if (st === "TRADING_ALLOWED") { tLabel = "ALLOWED"; tText = "New entries allowed when every check passes."; }
    else if (st === "NO_NEW_ENTRIES") { tLabel = "PAUSED"; tText = p.active ? PAUSED_NOTE : ((safety && safety.reason_text) || "Paused by a safety check."); }
    else if (st === "HALTED") { tLabel = "STOPPED"; tText = k.active ? "Blocked by the emergency stop." : d.status === "TRIGGERED" ? "Daily loss limit reached. Resets on the next trading day." : ((safety && safety.reason_text) || "Blocked by a safety check."); }
    const tradingAction = p.active ? "resume" : (st && st !== "HALTED" && st !== "UNKNOWN" && !k.active ? "pause" : null);
    const trading = { label: tLabel, text: tText, action: tradingAction, actionLabel: tradingAction === "resume" ? "Resume New Trades" : "Pause New Trades",
                      enabled: tradingAction === "resume" ? a.resume !== false : a.pause !== false };
    const stop = { label: !st ? "—" : k.active ? "ACTIVE" : "INACTIVE", text: !st ? "" : k.active ? `${STOP_ACTIVE_TEXT} ${STOP_ACTIVE_NOTE}` : "Blocks all new entries at once; existing positions stay protected.",
                   action: !st ? null : k.active ? "clear" : "stop", actionLabel: k.active ? "Clear Emergency Stop" : "Emergency Stop", enabled: k.active ? a.clear !== false : a.stop !== false };
    return { auto, trading, stop, killActive: !!k.active, paused: !!p.active };
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
             state: p.state || d.position_state || (card && card.state) || "", invalidation: p.invalidation_price || d.invalidation_price || null };
  }

  function marketModel(dom) {
    // dom = { stretch, confidence, demand, evidence, evidenceText, setupState, setupLabel, setupReason, proposalState } read from the legacy panels (display only)
    const s = num(dom.stretch), c = num(dom.confidence), d = num(dom.demand);
    const ev = dom.evidence || null;
    return { ppe: s == null ? "—" : `${s > 0 ? "+" : ""}${Math.round(s)}`, confidence: c == null ? "—" : String(Math.round(c)), demand: d == null ? "—" : String(Math.round(d)),
             evidence: ev === "LOADED" ? (dom.evidenceText && dom.evidenceText !== "—" ? dom.evidenceText : "Stored") : "None stored", setup: SETUP_TO_AGENT[dom.setupState] ? (dom.setupState === "READY" ? "READY" : dom.setupState === "WATCH" ? "WATCHING" : "No active setup") : "—",
             setupLabel: dom.setupLabel || (dom.setupState === "READY" ? "READY FOR REVIEW" : dom.setupState === "WATCH" ? "WATCH" : dom.setupState === "INVALIDATED" ? "INVALIDATED" : "NONE"),
             setupReason: dom.setupReason || "", blockers: dom.blockers || [],
             proposal: dom.proposalState === "REVIEWABLE" ? "Risk checks passed" : dom.proposalState === "BLOCKED" ? "Blocked by a risk check" : dom.proposalState ? cap(words(dom.proposalState)) : "" };
  }

  // the chart area: OK (normal height) or a compact, intentional empty state; LOADING keeps the normal layout (no flicker on a ticker switch)
  function chartModel(tv, regularSession, source, now) {
    if (!tv || !tv.active) return { kind: "NONE" };
    const sym = tv.active;
    if (tv.candles > 0) return { kind: "OK", symbol: sym };
    const pending = !!(tv.pending && tv.pending.candles);
    if (pending && !(now && state.pendingSince && now - state.pendingSince > PENDING_GRACE_MS)) return { kind: "LOADING", level: "info", title: "Waiting for market data", text: `Loading candles for ${sym}.`, symbol: sym };
    const connected = tv.connState === "connected";
    if (!connected) return { kind: "DISCONNECTED", level: "warn", title: "Market data unavailable", text: "The data stream is reconnecting. The chart will return automatically when data reconnects.", symbol: sym };
    if (regularSession === false) return { kind: "CLOSED", level: "info", title: "Market closed", text: "Next session data will appear automatically. Monitoring and historical data remain available where supported.", symbol: sym };
    return { kind: "NO_DATA", level: "warn", title: "Market data unavailable", text: `The market data feed returned no candles for ${sym}${source ? ` (${source})` : ""}. The chart will return automatically when data reconnects.`, symbol: sym };
  }
  function chartEmptyHtml(m) {
    if (!m || m.kind === "OK" || m.kind === "NONE" || m.kind === "LOADING") return "";
    return `<div class="pui-chart-empty ${esc(m.level)}" data-pui-chart="${esc(m.kind)}" role="status"><h3>${esc(m.title)}</h3><p>${esc(m.text)}</p></div>`;
  }

  // the diagnostics line: the legacy status chips (data source, position source, evidence, PPE, session) collapse into one summary + an expander
  const DIAG_TEXT = [[/^simulated data$/i, "Simulated market data"], [/^market (.+)$/i, (m) => `Market data ${m[1]}`], [/^positions (.+)$/i, (m) => `Position source ${m[1]}`],
                     [/^evidence (.+)$/i, (m) => `Evidence ${m[1]}`], [/^ppe (.+)$/i, (m) => `PPE ${m[1] === "not available" ? "unavailable" : m[1]}`], [/^session (.+)$/i, (m) => `Market session ${m[1]}`]];
  function diagText(text) {
    for (const [re, out] of DIAG_TEXT) { const m = re.exec(text); if (m) return typeof out === "function" ? out(m) : out; }
    return cap(text);
  }
  function diagModel(chips) {
    const items = (chips || []).map((c) => { const text = diagText(String(c.text || "").trim()); const sim = /^simulated/i.test(text); return { text, level: c.bad ? "bad" : (c.warn && !sim ? "warn" : "info"), simulated: sim }; });
    const bad = items.filter((i) => i.level === "bad"), warn = items.filter((i) => i.level === "warn");
    const summary = bad.length ? `⚠ ${bad[0].text}` : warn.length ? "⚠ Some data unavailable" : items.some((i) => i.simulated) ? "Simulated market data" : items.length ? "Data sources OK" : "";
    return { summary, level: bad.length ? "bad" : warn.length ? "warn" : "info", items, count: items.length };
  }
  function diagHtml(m, open) {
    if (!m || !m.count) return "";
    return `<details class="pui-fold pui-diag ${esc(m.level)}" data-pui-diag="${esc(m.level)}"${open ? " open" : ""}><summary data-pui="diag"><span class="pui-diag-summary">${esc(m.summary)}</span><span class="pui-sub"> · ${m.count} source${m.count === 1 ? "" : "s"}</span></summary>` +
      `<ul>${m.items.map((i) => `<li class="${esc(i.level)}">${esc(i.text)}</li>`).join("")}</ul></details>`;
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
  const lvl = (l) => (l === "ok" ? "pui-ok" : l === "warn" ? "pui-warn" : l === "bad" ? "pui-bad" : "pui-dim");
  function statusHtml(exec, safety, opts) {
    const o = opts || {};
    const segs = statusModel(exec, safety);
    const sys = systemModel(exec, safety);
    const mode = exec && exec.mode;
    const autoOn = mode === "PAPER_AUTO";
    const seg = (s) => s.key === "system"
      ? `<button type="button" class="pui-seg ${esc(s.cls)}" data-pui-seg="system" data-pui="system" aria-expanded="${o.sysOpen ? "true" : "false"}" title="System status: click for the details">${esc(s.text)} <span class="pui-caret">${o.sysOpen ? "▾" : "▸"}</span></button>`
      : `<span class="pui-seg ${esc(s.cls)}" data-pui-seg="${esc(s.key)}">${esc(s.text)}</span>`;
    const auto = `<span class="pui-auto" data-pui-auto="${autoOn ? "ON" : mode ? "OFF" : "—"}" title="Paper Auto: the controls are on the Risk card"><span class="pui-seg ${autoOn ? "ok" : ""}" data-pui-seg="auto">${esc(segs[3].text)}</span></span>`;
    const details = o.sysOpen ? `<div class="pui-system-details" data-pui-system-details>` + (sys.items.length ? sys.items.map((i) => `<div class="${lvl(i.level)}">${i.level === "ok" ? "✓" : "⚠"} ${esc(i.text)}${i.reason ? ` <span class="pui-sub">· ${esc(i.reason)}</span>` : ""}</div>`).join("")
      : `<div class="pui-dim">System status not loaded yet.</div>`) + `</div>` : "";
    return segs.slice(0, 3).map(seg).join("") + auto + `<span class="pui-tagline" data-pui-tagline>${esc(TAGLINE)}</span>` + details;
  }

  function confirmHtml(kind) {
    const c = CONFIRM[kind];
    if (!c) return "";
    return `<div class="pui-confirm ${esc(c.cls)}" data-pui-confirm="${esc(kind)}" role="alertdialog" aria-live="assertive"><b>${esc(c.title)}</b><p>${esc(c.text)}</p>` +
      `<div class="pui-row"><button type="button" class="pui-btn ${esc(c.cls)}" data-pui="${esc(kind)}-confirm">${esc(c.ok)}</button><button type="button" class="pui-btn" data-pui="cancel">Cancel</button></div></div>`;
  }

  function controlsHtml(exec, safety, avail, confirm) {
    const m = controlsModel(exec, safety, avail);
    const row = (key, title, r) => `<div class="pui-ctl-row" data-pui-row="${key}" data-pui-status="${esc(r.label)}"><span class="pui-ctl-name">${esc(title)}</span><b class="pui-ctl-status ${key === "stop" ? (r.label === "ACTIVE" ? "pui-bad" : "pui-dim") : r.label === "ON" || r.label === "ALLOWED" ? "pui-ok" : r.label === "PAUSED" ? "pui-warn" : r.label === "STOPPED" || r.label === "UNKNOWN" ? "pui-bad" : "pui-dim"}">${esc(r.label)}</b>` +
      `<span class="pui-ctl-action">${r.action ? `<button type="button" class="pui-btn ${r.action === "auto-on" ? "go" : r.action === "stop" ? "stop" : r.action === "clear" || r.action === "auto-off" ? "danger" : ""}" data-pui="${esc(r.action)}"${r.enabled ? "" : " disabled"}>${esc(r.actionLabel)}</button>` : ""}</span>` +
      `<span class="pui-ctl-text">${esc(r.text)}</span></div>` + (confirm && ((confirm === "auto-on" && key === "auto") || ((confirm === "stop" || confirm === "clear") && key === "stop")) ? confirmHtml(confirm) : "");
    return `<div class="pui-ctl" data-pui-controls><h5>Automation / Trading Controls</h5>` + row("auto", "Paper Auto", m.auto) + row("trading", "Trading", m.trading) + row("stop", "Emergency Stop", m.stop) + `</div>`;
  }

  function cardsHtml(ctx) {
    const a = agentModel(ctx.card, ctx.dom.setupState, ctx.safety && ctx.safety.state, ctx.exec);
    const m = marketModel(ctx.dom);
    const r = riskModel(ctx.exec, ctx.safety);
    const p = positionModel(ctx.card, ctx.position, ctx.limits);
    const sl = setupLines(a, ctx.dom);
    const note = positionNote(ctx.card, ctx.exec, ctx.safety);
    const sym = ctx.symbol || "—";
    const fold = (key, open, label, body) => `<details class="pui-fold pui-why" data-pui-fold="${key}"${open ? " open" : ""}><summary data-pui="fold:${key}">${esc(label)}</summary><div class="pui-why-body">${body}</div></details>`;
    const why = fold("why", !!ctx.whyOpen, "Why?", `<div class="pui-kv"><span>PPE stretch</span><b>${esc(m.ppe)}</b><span>Confidence</span><b>${esc(m.confidence)}</b><span>Demand</span><b>${esc(m.demand)}</b><span>Evidence</span><b>${esc(m.evidence)}</b>` +
      `<span>Stored setup state</span><b>${esc(m.setupLabel)}</b><span>Proposal status</span><b>${esc(m.proposal || "—")}</b></div>` +
      (m.setupReason ? `<div class="pui-sub">${esc(m.setupReason)}</div>` : "") + (m.blockers.length ? `<div class="pui-sub">Not ready: ${esc(m.blockers.join(" · "))}</div>` : ""));
    const agent = `<div class="pui-card agent primary" data-pui-card="agent" data-pui-primary data-agent="${esc(a.label)}"><h4>Agent · ${esc(sym)}</h4><div class="pui-big">${esc(a.label)}</div><div class="pui-sub">${esc(a.text)}</div>` +
      `${note ? `<div class="pui-line pui-note-line" data-pui-position-note>${esc(note)}</div>` : ""}${sl.line ? `<div class="pui-line">${esc(sl.line)}</div>` : ""}${sl.sub ? `<div class="pui-sub">${esc(sl.sub)}</div>` : ""}${why}</div>`;
    const posDetails = p.open ? fold("pos", !!ctx.posOpen, "Details", `<div class="pui-kv"><span>Position state</span><b>${esc(p.state || "—")}</b><span>Invalidation level</span><b>${esc(p.invalidation || "—")}</b><span>Status</span><b>${esc(p.status)}</b></div>` +
      `<div class="pui-sub">Broker orders, fills and reconciliation live under POSITIONS.</div>`) : "";
    const pos = p.open ? `<div class="pui-card position" data-pui-card="position" data-open="1"><h4>Position</h4><div class="pui-big">${esc(p.symbol)} · ${esc(p.shares)} share${Number(p.shares) === 1 ? "" : "s"}</div>` +
      `<div class="pui-kv"><span>Entry</span><b>${esc(p.entry)}</b><span>Current</span><b>${esc(p.current)}</b><span>P/L</span><b>${esc(p.pnl)}</b></div><div class="pui-line">${esc(p.holding)} · ${esc(p.status)}</div>` +
      (p.reason ? `<div class="pui-sub">${esc(p.reason)}</div>` : "") + posDetails + `</div>`
      : `<div class="pui-card position" data-pui-card="position" data-open="0"><h4>Position</h4><div class="pui-big">${esc(p.entering ? "Entering" : "No position")}</div><div class="pui-sub">${esc(p.entering ? "Order submitted. Waiting for broker fill." : a.key === "ENTRY_FAILED" ? "The entry did not fill. No automatic replacement order." : "")}</div></div>`;
    const riskDetails = fold("risk", !!ctx.riskOpen, "Details", r.lines.slice(1).map((l) => `<div class="${lvl(l.level)}">${l.ok ? "✓" : "⚠"} ${esc(l.text)}</div>`).join("") || `<div class="pui-dim">No further detail.</div>`);
    const risk = `<div class="pui-card risk" data-pui-card="risk" data-risk="${esc(r.level)}" data-kill="${r.killActive ? "1" : "0"}"><h4>Risk</h4><div class="pui-big">${esc(r.headline)}</div>` +
      (r.primary ? `<div class="pui-sub ${r.critical ? "pui-bad" : ""}" data-pui-risk-reason>${esc(r.primary)}</div>` : "") +
      controlsHtml(ctx.exec, ctx.safety, ctx.avail, ctx.confirm) + riskDetails + `</div>`;
    return agent + pos + risk;
  }

  function positionsHtml(rows) {
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
      `<ul class="pui-timeline" data-pui-timeline>${rows || '<li class="pui-day">No activity yet.</li>'}</ul>` + val +
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
  function tvState() { const tv = window.TraderView; return (tv && tv.state) || null; }
  function activeSymbol() { const s = tvState(); return (s && s.active) || null; }
  function cardFor(sym) { const e = exec(); return sym && e && e.cards ? e.cards.find((c) => c.symbol === sym) || null : null; }
  function readDom() {
    const txt = (sel) => { const e = q(sel); return e ? e.textContent.trim() : null; };
    const ds = (sel) => { const e = q(sel); return e ? e.dataset.state || null : null; };
    const evRow = q("#tv-evidence table.tv-kv tr td:last-child");                       // the Strength row of the loaded evidence table
    const blockEl = [...document.querySelectorAll("#tv-setup .tv-dim")].find((e) => e.textContent.trim().startsWith("Not READY:"));
    return { stretch: txt('#tv-ppe [data-ppe="stretch"] .tv-ppe-val'), confidence: txt('#tv-ppe [data-ppe="confidence"] .tv-ppe-val'), demand: txt('#tv-demand [data-demand="score"] .tv-dem-val'),
             evidence: ds("#tv-evidence"), evidenceText: evRow ? evRow.textContent.trim() : null, setupState: ds("#tv-setup"), setupLabel: txt("#tv-setup [data-setup-state]"),
             setupReason: txt("#tv-setup .tv-set-reason div"), blockers: blockEl ? blockEl.textContent.replace("Not READY:", "").split("·").map((s) => s.trim()).filter(Boolean) : [],
             proposalState: ds("#tv-proposal") };
  }
  // the legacy buttons this shell delegates to (Risk Controls panel: pause / resume / stop / clear; Paper Bot panel: enable / disable)
  const LEGACY = { "auto-on": '#tv-bot [data-bot="enable"]', "auto-off": '#tv-bot [data-bot="disable"]', pause: '#tv-safety [data-safety="pause"]', resume: '#tv-safety [data-safety="resume"]',
                   stop: '#tv-safety [data-safety="stop"]', clear: '#tv-safety [data-safety="clear"]' };
  function legacyAvailable(kind) { const b = q(LEGACY[kind]); return !!b && !b.disabled; }
  function avail() { return { auto: legacyAvailable("auto-on") || legacyAvailable("auto-off"), pause: legacyAvailable("pause"), resume: legacyAvailable("resume"), stop: legacyAvailable("stop"), clear: legacyAvailable("clear") }; }
  function clickLegacy(kind, times) {
    // a two-click legacy button (Paper Auto enable, Clear Emergency Stop) is clicked twice in a row: the first click arms it, the second posts; an already-armed button needs one
    for (let i = 0; i < (times || 1); i++) { const b = q(LEGACY[kind]); if (!b || b.disabled) return i > 0; b.click(); }
    return true;
  }
  function readChips() { return [...document.querySelectorAll("#tv-status-line .tv-chip")].map((e) => ({ text: e.textContent.trim(), warn: e.classList.contains("warn"), bad: e.classList.contains("bad") })); }
  const collapsed = () => { try { return localStorage.getItem("pui.details") !== "open"; } catch (e) { return true; } };
  function setCollapsed(v) { try { localStorage.setItem("pui.details", v ? "closed" : "open"); } catch (e) { /* per-browser convenience only */ } document.body.classList.toggle("pui-collapsed", v); const b = $("pui-details-btn"); if (b) { b.textContent = v ? "View details ▸" : "Hide details ▾"; b.setAttribute("aria-expanded", v ? "false" : "true"); } }

  function renderStatus() { const el = $("pui-status"); if (el) el.innerHTML = statusHtml(exec(), safety(), { sysOpen: state.sysOpen }); }
  function renderCards() {
    const host = $("pui-cards");
    if (!host) return;
    const sym = activeSymbol();
    const card = cardFor(sym);
    let position = null;
    if (card && card.position_id && state.posData) position = (state.posData.positions || []).find((p) => p.position_id === card.position_id) || null;
    host.innerHTML = cardsHtml({ exec: exec(), safety: safety(), card, position, dom: readDom(), symbol: sym, limits: state.limits, avail: avail(), confirm: state.confirm,
                                 whyOpen: state.whyOpen, riskOpen: state.riskOpen, posOpen: state.posOpen });
  }
  function renderChart(forced) {
    if (forced !== undefined) state.chartForced = forced;                     // harness / tests: force one empty state (null restores the live one)
    const wrap = q("#tv-body .tv-chart-wrap");
    if (!wrap) return null;
    const tv = tvState();
    const now = Date.now();
    if (tv && tv.pending && tv.pending.candles && !(tv.candles > 0)) { if (!state.pendingSince) state.pendingSince = now; } else state.pendingSince = null;
    const market = (exec() && exec().scheduler && exec().scheduler.market) || {};
    const m = state.chartForced || chartModel(tv, market.regular_session, (q("#tv-mode") || {}).textContent, now);
    let el = $("pui-chart-empty");
    const html = chartEmptyHtml(m);
    if (!el) { el = document.createElement("div"); el.id = "pui-chart-empty"; wrap.appendChild(el); }
    if (el.innerHTML !== html) el.innerHTML = html;
    document.body.classList.toggle("pui-chart-empty", !!html);
    state.chartKind = m.kind;
    return m;
  }
  function renderDiag() {
    const center = q("#tv-body .tv-center");
    if (!center) return;
    let el = $("pui-diag");
    if (!el) { el = document.createElement("div"); el.id = "pui-diag"; const line = $("tv-status-line"); if (line) line.insertAdjacentElement("afterend", el); else center.appendChild(el); }
    const html = diagHtml(diagModel(readChips()), state.diagOpen);
    if (el.innerHTML !== html) el.innerHTML = html;
  }
  function ensureTradeCards() {
    const grid = q("#tv-body .tv-grid");
    if (!grid || $("pui-cards")) return !!$("pui-cards");
    const cards = document.createElement("div");
    cards.id = "pui-cards";
    cards.className = "pui-cards";
    grid.insertAdjacentElement("beforebegin", cards);
    cards.addEventListener("click", onAction);
    setCollapsed(collapsed());
    return true;
  }
  function openConfirm(kind) {
    state.confirm = kind;
    clearTimeout(state.confirmTimer);
    state.confirmTimer = setTimeout(() => { if (state.confirm === kind) { state.confirm = null; renderCards(); } }, CONFIRM_MS);
    renderCards();
  }
  function closeConfirm() { state.confirm = null; clearTimeout(state.confirmTimer); state.confirmTimer = null; }
  function refreshSoon() { setTimeout(() => { renderStatus(); renderCards(); }, 50); setTimeout(() => { renderStatus(); renderCards(); }, 400); setTimeout(() => { renderStatus(); renderCards(); }, 1500); }
  function onAction(e) {
    const b = e.target.closest("[data-pui]");
    if (!b || b.disabled) return;
    const k = b.dataset.pui;
    if (k.startsWith("fold:")) { e.preventDefault(); const key = k.slice(5); if (key === "why") state.whyOpen = !state.whyOpen; if (key === "risk") state.riskOpen = !state.riskOpen; if (key === "pos") state.posOpen = !state.posOpen; renderCards(); return; }
    if (k === "diag") { e.preventDefault(); state.diagOpen = !state.diagOpen; renderDiag(); return; }
    if (k === "system") { state.sysOpen = !state.sysOpen; renderStatus(); return; }
    // explanations first: Enable Paper Auto, Emergency Stop and Clear Emergency Stop open a confirmation panel; nothing is clicked or posted yet
    if (k === "auto-on" || k === "stop" || k === "clear") { openConfirm(k); return; }
    if (k === "cancel") { closeConfirm(); renderCards(); return; }
    // the confirmed actions delegate to the existing Risk Controls / Paper Bot buttons: identical POSTs, identical persisted semantics
    if (k === "auto-on-confirm") { closeConfirm(); const armed = /^confirm/i.test(((q(LEGACY["auto-on"]) || {}).textContent || "").trim()); clickLegacy("auto-on", armed ? 1 : 2); refreshSoon(); return; }
    if (k === "stop-confirm") { closeConfirm(); clickLegacy("stop", 1); refreshSoon(); return; }
    if (k === "clear-confirm") { closeConfirm(); const armed = /^confirm/i.test(((q(LEGACY.clear) || {}).textContent || "").trim()); clickLegacy("clear", armed ? 1 : 2); refreshSoon(); return; }
    if (k === "auto-off" || k === "pause" || k === "resume") { closeConfirm(); clickLegacy(k, 1); refreshSoon(); }
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
    if (nav.id === "trade" && ensureTradeCards()) { renderCards(); renderChart(); renderDiag(); }
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
    if (activeLegacyTab() === "trader") { if (ensureTradeCards()) { renderCards(); renderChart(); renderDiag(); } }
  }

  function mount() {
    if (typeof document === "undefined" || state.booted) return;
    if (!q("nav.tabs") || !$("tab-trader")) return;
    state.booted = true;
    document.body.classList.add("pui");
    const tabs = q("nav.tabs");
    const status = document.createElement("div"); status.id = "pui-status"; status.className = "pui-status"; status.setAttribute("role", "status");
    status.addEventListener("click", onAction);
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
    document.addEventListener("click", (e) => { const d = $("pui-diag"); if (d && d.contains(e.target)) onAction(e); });
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
  return { NAV, MORE, TAGLINE, DEFAULT, AGENT, SETUP_TO_AGENT, SAFETY_TEXT, CONFIRM, LEGACY, agentModel, setupLines, positionNote, systemModel, statusModel, riskModel, controlsModel, positionModel, marketModel,
           chartModel, chartEmptyHtml, diagModel, diagHtml, positionCards, timeline, todaySummary, moreGroups, statusHtml, confirmHtml, controlsHtml, cardsHtml, positionsHtml, positionDetailsHtml,
           activityHtml, moreHtml, go, mount, tick, renderCards, renderStatus, renderChart, renderDiag, renderPositions, renderActivity, renderMore, state,
           expandDetails: () => setCollapsed(false), collapseDetails: () => setCollapsed(true), isCollapsed: collapsed };
});
