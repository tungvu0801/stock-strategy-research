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
  const AUTO_PENDING_TEXT = "Checking Paper Auto status…";
  const AUTO_UNKNOWN_TEXT = "Paper Auto status unavailable";
  const AUTO_ARMED_NOTE = "Auto is armed, but safety currently blocks new entries.";
  const AUTO_TIMEOUT_MS = 20000;                                               // a pending Paper Auto action without a status answer becomes "unavailable"
  const CONFIRM = {
    "auto-on": { title: "Enable Paper Auto?", text: "You are enabling automatic Alpaca PAPER trading. New eligible entries may be submitted automatically when all strategy and risk conditions pass. Live trading remains disabled.",
                 ok: "Confirm: enable Paper Auto", cls: "go" },
    stop: { title: "Emergency Stop?", text: "This immediately blocks all new entries. Existing positions remain monitored and protective exits remain allowed. The emergency stop stays active until cleared manually.",
            ok: "Confirm Emergency Stop", cls: "stop" },
    clear: { title: "Clear Emergency Stop?", text: "This removes the emergency-stop latch. Trading resumes only if all safety checks pass.", ok: "Confirm: clear Emergency Stop", cls: "danger" },
  };
  // MORE (Stage 6.1.2): three groups only - Research / System / Settings - each a collapsed fold (progressive disclosure). Nothing is deleted: the Strategy
  // Lab registry is the research entry, the legacy rule builder / backtests and the research tabs sit under Research, the legacy tools under System ›
  // Diagnostics, Robinhood only there. The Strategy Lab's own workspaces are listed inside the registry panel (read from its sub-navigation at run time).
  const MORE = [
    { id: "research", title: "Research", text: "Strategy Lab, backtests, research history.",
      items: [{ action: "lab", label: "Strategy Lab" }, { tab: "strategy", label: "Backtests (rule builder, legacy lab)" }, { tab: "history", label: "Research History" }, { tab: "setups", label: "Research Setups" }] },
    { id: "system", title: "System", text: "Broker connection, data sources, validation, diagnostics, logs.",
      items: [{ action: "health", label: "Broker Connection" }, { action: "data", label: "Data Sources" }, { href: "forward_validation.html", label: "Forward Validation" },
              { action: "diagnostics", label: "Diagnostics", tools: [{ tab: "overview", label: "Market dashboard" }, { tab: "agent", label: "AI Agent" }, { tab: "watchlist", label: "Watchlist editor" },
                                                                   { tab: "daily", label: "Daily Review (research)" }, { tab: "review", label: "Trader Review" }, { tab: "portfolio", label: "Portfolio (Robinhood, read only)" }] },
              { action: "logs", label: "Logs" }] },
    { id: "settings", title: "Settings", text: "Risk, automation, notifications. Read only here; configured server-side.",
      items: [{ action: "risk", label: "Risk" }, { action: "automation", label: "Automation" }, { action: "notifications", label: "Notifications" }, { action: "settings", label: "Advanced configuration" }] },
  ];
  // legacy deep links (the old tab names and the old bookmarks) mapped to the product sections; the four product hashes map to themselves. A hash that is not
  // listed (e.g. #daily-brief, consumed by brief_delivery.js) is left to its legacy consumer.
  const LEGACY_LINKS = {
    trade: { section: "trade" }, positions: { section: "positions" }, activity: { section: "activity" }, more: { section: "more" },
    portfolio: { section: "positions" }, paper: { section: "activity", filter: "trades" }, "paper-trades": { section: "activity", filter: "trades" },
    trader: { section: "trade" }, "trader-view": { section: "trade" }, watchlist: { section: "trade" }, overview: { section: "trade" }, dashboard: { section: "trade" },
    alerts: { section: "activity" }, daily: { section: "activity" }, "daily-review": { section: "activity" }, review: { section: "activity" },
    strategy: { section: "more", more: "lab" }, "strategy-lab": { section: "more", more: "lab" }, setups: { section: "more", more: "lab" }, history: { section: "more", more: "research" },
    agent: { section: "more", more: "diagnostics" }, settings: { section: "more", more: "settings" }, "forward-validation": { section: "more", more: "system" }, validation: { section: "more", more: "system" },
  };
  const FILTERS = [["all", "All"], ["trades", "Trades"], ["safety", "Safety"], ["system", "System"], ["research", "Research"]];
  const GROUP_OF = { setup: "research", proposal: "trades", order: "trades", fill: "trades", position: "trades", exit: "trades", attention: "safety", safety: "safety", reconciliation: "system", validation: "system" };
  // the presentation-only priority order of the TRADE focus strip (no backend decision changes): a system problem outranks a stock
  const PRIORITY = ["ATTENTION", "EXIT", "POSITION_ATTENTION", "DEGRADED", "IN_TRADE", "REVIEWABLE", "READY", "WATCHING", "MARKET"];
  const READY_NOTE = "READY means the setup is eligible. Execution eligibility is decided separately by the safety gate and the entry controls.";
  const NO_SCORE = "No opportunity score: one strategy, no cross-strategy ranking yet.";
  const NO_SUPPORT = "No other strategy evaluated.";
  const state = { section: null, legacyTab: null, timer: null, fetchTimer: null, posDetails: null, posData: null, actData: null, limits: null, booted: false, error: null,
                  whyOpen: false, riskOpen: false, posOpen: false, sysOpen: false, diagOpen: false, pendingSince: null, chartForced: null, chartKind: null, confirm: null, confirmTimer: null,
                  autoPending: null, autoError: null, autoWatch: null, botObserver: null,
                  // Stage 6.1.2: the strategy registry and the frozen controls (read once), the per-symbol terminal state behind Why? (on demand, 10 s cache), the WHY technical fold,
                  // the ACTIVITY filter, the MORE fold state, the open MORE panel and the Strategy Lab detail
                  registry: null, registryBusy: false, registryFailedAt: null, frozen: null, frozenBusy: false, frozenFailedAt: null, terminal: {}, terminalBusy: false, techOpen: false, filter: "all", moreOpen: {}, moreAction: null,
                  labDetail: null, labError: null,
                  // Stage 6.8: the account's positions (POSITIONS) and the Bot Activity read (TRADE): the payload, this page's connection, the open folds
                  acctPos: null, acctPosError: null, botAct: null, botConn: null, botBusy: false, botReadAt: null, botOpen: {},
                  // Stage 6.9: the unified portfolio (Alpaca PAPER + Robinhood READ ONLY)
                  unified: null, unifiedError: null, unifiedBusy: false, unifiedReadAt: null };

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

  // Paper Auto for the header segment AND the control row: ON only when the authoritative status says PAPER_AUTO, OFF when it says MONITOR_ONLY, a
  // pending state while a confirmed action is being re-read, unavailable when no status is loaded; a failed action keeps OFF and shows the backend text
  function autoFailureText(err) {
    const s = String(err || "").trim();
    if (!s) return "";
    let m = /^Enable Paper Auto failed \((.*)\)\.?$/s.exec(s);
    if (m) return `Could not enable Paper Auto. ${m[1]}`;
    m = /^Stop New Trades failed \((.*)\)\.?$/s.exec(s);
    if (m) return `Could not disable Paper Auto. ${m[1]}`;
    m = /^Paper bot status unavailable \((.*)\)\.?$/s.exec(s);
    if (m) return `${AUTO_UNKNOWN_TEXT} (${m[1]})`;
    return s;
  }
  function autoModel(exec, safety, pending, error) {
    const mode = exec && exec.mode;
    const st = (safety && safety.state) || (exec && exec.safety_state) || null;
    const failure = autoFailureText(error);
    if (pending) return { key: "PENDING", label: "…", header: "AUTO …", cls: "", text: AUTO_PENDING_TEXT, action: null, actionLabel: "", failure: "" };
    if (!mode) return { key: "UNKNOWN", label: "—", header: "AUTO —", cls: "", text: failure || AUTO_UNKNOWN_TEXT, action: null, actionLabel: "", failure: "" };
    if (mode === "PAPER_AUTO") return { key: "ON", label: "ON", header: "AUTO ON", cls: "ok", text: `${AUTO_ON_TEXT}${st && st !== "TRADING_ALLOWED" ? ` ${AUTO_ARMED_NOTE}` : ""}`, action: "auto-off", actionLabel: "Disable Auto", failure };
    return { key: "OFF", label: "OFF", header: "AUTO OFF", cls: "", text: `${AUTO_OFF_TEXT} ${AUTO_OFF_NOTE}`, action: "auto-on", actionLabel: "Enable Paper Auto", failure };
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

  function statusModel(exec, safety, opts) {
    const o = opts || {};
    const env = (exec && exec.environment) || {};
    const market = (exec && exec.scheduler && exec.scheduler.market) || {};
    const mode = exec && exec.mode;
    const sys = systemModel(exec, safety);
    const paper = { key: "paper", text: env.simulated ? "SIMULATED PAPER" : "PAPER", cls: "ok" };
    const mk = market.regular_session == null ? { key: "market", text: "MARKET —", cls: "" } : { key: "market", text: market.regular_session ? "MARKET OPEN" : "MARKET CLOSED", cls: market.regular_session ? "ok" : "" };
    const system = { key: "system", text: sys.text, cls: sys.cls, level: sys.level };
    const am = autoModel(exec, safety, o.autoPending, o.autoError);
    const auto = { key: "auto", text: am.header, cls: am.cls, state: am.key };
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
  function controlsModel(exec, safety, avail, opts) {
    const a = avail || {};
    const o = opts || {};
    const mode = exec && exec.mode;
    const on = mode === "PAPER_AUTO";
    const st = (safety && safety.state) || (exec && exec.safety_state) || null;
    const k = (safety && safety.kill_switch) || {};
    const p = (safety && safety.no_new_entries) || {};
    const d = (safety && safety.daily_loss) || {};
    const am = autoModel(exec, safety, o.autoPending, o.autoError);
    const auto = { label: am.label, key: am.key, text: am.text, action: am.action, actionLabel: am.actionLabel, enabled: a.auto !== false && !o.autoPending, failure: am.failure };
    let tLabel = "UNKNOWN", tText = "Risk status unknown";
    if (st === "TRADING_ALLOWED") { tLabel = "ALLOWED"; tText = "New entries allowed when every check passes."; }
    else if (st === "NO_NEW_ENTRIES") { tLabel = "PAUSED"; tText = p.active ? PAUSED_NOTE : ((safety && safety.reason_text) || "Paused by a safety check."); }
    else if (st === "HALTED") { tLabel = "STOPPED"; tText = k.active ? "Blocked by the emergency stop." : d.status === "TRIGGERED" ? "Daily loss limit reached. Resets on the next trading day." : ((safety && safety.reason_text) || "Blocked by a safety check."); }
    const tradingAction = p.active ? "resume" : (st && st !== "HALTED" && st !== "UNKNOWN" && !k.active ? "pause" : null);
    const trading = { label: tLabel, text: tText, action: tradingAction, actionLabel: tradingAction === "resume" ? "Resume New Trades" : "Pause New Trades",
                      enabled: tradingAction === "resume" ? a.resume !== false : a.pause !== false };
    const stop = { label: !st ? "—" : k.active ? "ACTIVE" : "INACTIVE", text: !st ? "" : k.active ? `${STOP_ACTIVE_TEXT} ${STOP_ACTIVE_NOTE}` : "Blocks all new entries at once; existing positions stay protected.",
                   action: !st ? null : k.active ? "clear" : "stop", actionLabel: k.active ? "Clear Emergency Stop" : "Emergency Stop", enabled: k.active ? a.clear !== false : a.stop !== false };
    return { auto, trading, stop, killActive: !!k.active, paused: !!p.active, pending: !!o.autoPending };
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

  function positionCards(exec, positions, events, strategyName) {
    const cards = ((exec && exec.cards) || []).filter((c) => ["IN_TRADE", "EXITING", "ENTERING", "ATTENTION"].includes(c.key));
    const byId = {};
    for (const p of positions || []) byId[p.position_id] = p;
    return cards.map((c) => { const position = c.position_id ? byId[c.position_id] || null : null;
                              return { card: c, position, model: positionModel(c, position, state.limits), agent: agentModel(c, null, null, null), life: positionLifecycle(position, c, [], state.limits, strategyName, events, state.frozen) }; });
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
    // the Strategy Lab's workspaces are listed inside the registry panel (not in MORE); the argument stays for the callers and the tests
    void labViews;
    return MORE.map((g) => ({ id: g.id, title: g.title, text: g.text, items: g.items.map((it) => ({ ...it, tools: it.tools ? it.tools.map((t) => ({ ...t })) : undefined })) }));
  }
  function moreTabs() { return MORE.flatMap((g) => g.items.flatMap((it) => [it.tab, ...(it.tools || []).map((t) => t.tab)].filter(Boolean))); }

  // ---------------------------------------------------------------- Stage 6.1.2: pure models -------------------------------------------
  // legacy deep links: #portfolio -> POSITIONS, #paper -> ACTIVITY · Trades, #trader -> TRADE, #strategy -> MORE › Strategy Lab; null = not ours (left to the legacy consumer)
  function routeFor(hash) {
    const key = String(hash || "").replace(/^#\/?/, "").toLowerCase().split(/[?&/]/)[0];
    const r = key ? LEGACY_LINKS[key] : null;
    return r ? { section: r.section, filter: r.filter || null, more: r.more || null, hash: key } : null;
  }
  function minutesToClose(et) { const m = /^(\d{2}):(\d{2})$/.exec(String(et || "")); return m ? 16 * 60 - (Number(m[1]) * 60 + Number(m[2])) : null; }

  // the strategy display on TRADE: real strategy state only. The registry says which strategies are implemented and active (one today); placeholders are never
  // "evaluated", never "supporting" and never carry a score. No opportunity score exists (one strategy, no cross-strategy ranking), so none is shown.
  function strategyModel(card, dom, registry, proposalState) {
    const rows = (registry && registry.strategies) || [];
    const live = rows.filter((s) => s.implemented && s.active);
    const key = card && card.key && card.key !== "NONE" ? card.key : (SETUP_TO_AGENT[dom && dom.setupState] || "WATCHING");
    const setup = dom && dom.setupState;
    const best = live[0] || null;
    return { loaded: !!registry, best: best ? best.display_name : null, bestId: best ? best.strategy_id : null, bestStatus: best ? best.status_label || best.status : null,
             bestState: setup === "READY" ? "Setup conditions met" : setup === "WATCH" ? "Setup on watch" : setup === "INVALIDATED" ? "Setup invalidated" : "No active setup",
             evaluated: live.length, placeholders: rows.filter((s) => !s.implemented).length, supporting: [], supportingText: NO_SUPPORT, score: null, scoreText: NO_SCORE,
             ready: key === "READY", label: key === "READY" ? "Best strategy" : "Best setup", proposalLink: ["REVIEWABLE", "BLOCKED"].includes(proposalState || "") };
  }

  // ONE consistent execution-eligibility surface ("Cannot enter · <one sentence>"): the first deterministic reason in precedence - safety gate, reconciliation, account
  // data, position cap, session, the proposal's own blocker - else "automatic entry off" (Monitor only) or allowed. Never a prediction: READY is setup eligibility.
  function blockerModel(exec, safety, dom, terminal, limits) {
    const st = (safety && safety.state) || (exec && exec.safety_state) || null;
    const k = (safety && safety.kill_switch) || {};
    const acct = (exec && exec.account_data) || {};
    const market = (exec && exec.scheduler && exec.scheduler.market) || {};
    const prop = (terminal && terminal.proposal) || null;
    const blocked = (reason, code) => ({ level: "blocked", title: "Cannot enter", reason, code });
    if (!st) return { level: "unknown", title: "Execution status unknown", reason: "Risk status unknown.", code: "SAFETY_UNKNOWN" };
    if (st === "HALTED" || st === "UNKNOWN") return blocked(k.active ? "Trading safety is stopped (emergency stop)." : safety && safety.reason_text ? `Trading safety is stopped: ${safety.reason_text}` : "Trading safety is stopped.", k.active ? "KILL_SWITCH_ACTIVE" : "SAFETY_HALTED");
    if (st === "NO_NEW_ENTRIES") return blocked(safety && safety.reason_text ? `Trading safety is paused: ${safety.reason_text}` : "Trading safety is paused.", "NO_NEW_ENTRIES");
    if (exec && exec.reconciliation_healthy === false) return blocked("Reconciliation required.", "RECONCILIATION_REQUIRED");
    if (acct.status && acct.status !== "FRESH") return blocked("Account equity unavailable.", `ACCOUNT_${acct.status}`);
    const maxOpen = num(limits && limits.max_open_positions) || num(exec && exec.max_open_positions);
    if (maxOpen != null && (num(exec && exec.open_positions) || 0) + (num(exec && exec.pending_entries) || 0) >= maxOpen) return blocked("Maximum open positions reached.", "MAX_OPEN_POSITIONS");
    if (market.regular_session === false) return blocked("Market closed: no entry outside the regular session.", "MARKET_CLOSED");
    const mtc = minutesToClose(market.et_time);
    if (market.regular_session && mtc != null && mtc <= (num(limits && limits.near_close_minutes) || 15)) return blocked("Near-close entry cutoff reached.", "NEAR_CLOSE");
    if (prop && prop.effective_status === "BLOCKED" && (prop.blockers || []).length) return blocked(prop.blockers[0].text || cap(words(prop.blockers[0].code)), prop.blockers[0].code);
    const el = prop && prop.execution_eligibility;
    if (el && el.allowed === false && prop.effective_status === "REVIEWABLE" && el.reason_text) return blocked(el.reason_text, (el.reason_codes || [])[0] || el.state || "NOT_ELIGIBLE");
    if (exec && exec.mode !== "PAPER_AUTO") return { level: "off", title: "Automatic entry off", reason: "Monitor only: no order is sent. Paper Auto would allow automatic paper entries.", code: "MONITOR_ONLY" };
    return { level: "ok", title: "Entry allowed when every check passes", reason: "The safety gate allows new entries; the entry controls run at order time.", code: "ALLOWED" };
  }

  // the presentation-only priority of the TRADE focus strip: a system problem first, then exits, positions needing attention, a degraded system, a reviewable proposal, READY,
  // WATCHING, the market summary. Nothing here decides anything; it orders what the backend already says.
  function priorityModel(ctx) {
    const exec = ctx.exec, safety = ctx.safety, card = ctx.card, dom = ctx.dom || {};
    const sys = systemModel(exec, safety);
    const cards = (exec && exec.cards) || [];
    const items = [];
    const push = (kind, title, text, action) => items.push({ kind, rank: PRIORITY.indexOf(kind) + 1, title, text, action: action || null });
    if (sys.level === "STOPPED") push("ATTENTION", "NEEDS ATTENTION", sys.primary || "Critical issue requires attention", { go: "trade", label: "Risk details" });
    else if (exec && exec.reconciliation_healthy === false) push("ATTENTION", "NEEDS ATTENTION", "Reconciliation needs a look", { go: "positions", label: "Open POSITIONS" });
    for (const c of cards.filter((x) => x.key === "EXITING")) push("EXIT", `${c.symbol} · EXIT REQUIRED`, c.line || "Exit in progress", { go: "positions", label: "Open POSITIONS" });
    for (const c of cards.filter((x) => x.key === "ATTENTION" && x.shares != null)) push("POSITION_ATTENTION", `${c.symbol} · NEEDS ATTENTION`, c.line || "Manual review required", { go: "positions", label: "Open POSITIONS" });
    if (sys.level === "DEGRADED") push("DEGRADED", "SYSTEM DEGRADED", sys.primary || "Some data unavailable; new entries wait until it recovers.", { pui: "system", label: "System details" });
    for (const c of cards.filter((x) => x.key === "IN_TRADE")) push("IN_TRADE", `${c.symbol} · IN TRADE`, c.line || "Existing trade continues. Exit conditions monitored.", { go: "positions", label: "Open POSITIONS" });
    const sym = ctx.symbol || "—";
    if (dom.proposalState === "REVIEWABLE" && (!card || !["IN_TRADE", "EXITING", "ENTERING", "ATTENTION", "CLOSED"].includes(card.key))) push("REVIEWABLE", `${sym} · REVIEWABLE`, "A review proposal passed the risk checks. Execution eligibility is separate.", { fold: "why", label: "Why?" });
    else if ((card && card.key === "READY") || (dom.setupState === "READY" && (!card || card.key === "NONE"))) push("READY", `${sym} · READY`, READY_NOTE, { fold: "why", label: "Why?" });
    else if ((card && card.key === "WATCHING") || (["WATCH", "NONE", "INVALIDATED", null, undefined].includes(dom.setupState) && (!card || card.key === "NONE"))) push("WATCHING", `${sym} · WATCHING`, "Waiting for setup conditions.", { fold: "why", label: "Why?" });
    const market = (exec && exec.scheduler && exec.scheduler.market) || {};
    push("MARKET", market.regular_session == null ? "Market —" : market.regular_session ? "Market open" : "Market closed", market.regular_session ? "Regular session · monitoring." : "Monitoring continues; new entries wait for the regular session.", null);
    items.sort((a, b) => a.rank - b.rank);
    return { items, focus: items[0], systemLevel: sys.level };
  }

  // Paper Auto and the safety gate are independent: the valid combinations are shown as one label (AUTO ON · TRADING PAUSED …); the real controls stay the three rows
  function sessionModel(exec, safety, opts) {
    const am = autoModel(exec, safety, (opts || {}).autoPending, (opts || {}).autoError);
    const cm = controlsModel(exec, safety, {}, opts);
    const trading = cm.trading.label;
    const text = am.key === "ON" && trading === "ALLOWED" ? "Automatic paper entries may be submitted when every check passes."
      : am.key === "ON" ? "Auto is armed, but safety currently blocks new entries. Existing positions stay monitored."
      : trading === "ALLOWED" ? "Monitoring only: no automatic entry. The gate would allow entries." : "Monitoring only; the gate also blocks new entries.";
    return { auto: am.header, trading: `TRADING ${trading}`, headline: `${am.header} · TRADING ${trading}`, text, autoKey: am.key, tradingKey: trading,
             future: "A single START TRADING SESSION control is planned; today Paper Auto, Pause New Trades and Emergency Stop are the real controls." };
  }

  // why a position is still held: every exit rule that has NOT fired, in the engine's precedence, from the persisted position, its card and the live panel values
  function holdingLines(card, pos, dom, limits, fz) {
    const p = pos || {}, d = (card && card.details) || {}, f = fz || {};
    const out = [];
    const inv = p.invalidation_price || d.invalidation_price, last = p.last_price || (card && card.current);
    if (inv) out.push(`Invalidation ${inv} vs current ${last || "—"}${num(last) != null && num(inv) != null ? (num(last) <= num(inv) ? " - reached (HARD_INVALIDATION due)" : " - not reached") : ""}`);
    const streak = num(p.demand_low_streak) || 0, need = num(f.demand_exit_evals) || 2, lvl = f.demand_exit != null ? f.demand_exit : 35;
    out.push(`Demand below ${lvl}: ${streak} / ${need} consecutive evaluations${streak >= need ? " - DEMAND_FAILURE due" : ""}`);
    const held = num(p.sessions_held != null ? p.sessions_held : d.sessions_held), max = num(limits && limits.max_hold_sessions) || MAX_HOLD_DEFAULT;
    if (held != null) out.push(`Holding Day ${held} / ${max}${held >= max ? " - TIME_EXIT due" : ""}`);
    const s = num(dom && dom.stretch), se = f.stretch_exit != null ? f.stretch_exit : -20;
    if (s != null) out.push(`PPE stretch ${s > 0 ? "+" : ""}${Math.round(s)} vs exit level ${se}${s > se ? " - DISLOCATION_NORMALIZED due" : ""}`);
    if (p.weakening_reason) out.push(`Weakening (informational): ${words(p.weakening_reason)}`);
    if (p.exit_reason || (card && card.key === "EXITING")) out.push(`Exit reason: ${(card && card.reason) || words(p.exit_reason) || "exit in progress"}`);
    else out.push("Held: no exit rule has fired.");
    return out;
  }

  // the reusable WHY panel: deterministic backend evidence only (card, stored setup, proposal, gate, account, limits), structured Signal · Strategy · Supporting evidence ·
  // Against / risks · Portfolio · Safety · Decision, with the raw values under Technical details. Nothing is inferred and no model is consulted.
  function whyModel(ctx) {
    const exec = ctx.exec, safety = ctx.safety, card = ctx.card, dom = ctx.dom || {}, t = ctx.terminal || {}, limits = ctx.limits, pos = ctx.position || null, fz = ctx.frozen || {};
    const a = agentModel(card, dom.setupState, safety && safety.state, exec);
    const m = marketModel(dom);
    const sm = strategyModel(card, dom, ctx.registry, dom.proposalState);
    const setup = t.setup || {}, prop = t.proposal || {};
    const bl = blockerModel(exec, safety, dom, t, limits);
    const vs = (v, op, k, label) => (fz[k] == null ? `${label} ${v}` : `${label} ${v} (needs ${op} ${fz[k]})`);
    const blockers = setup.blockers && setup.blockers.length ? setup.blockers.map(words) : (dom.blockers || []);
    const question = { WATCHING: "Why is this stock WATCHING?", READY: "Why is it READY?", ENTERING: "Why is the order pending?", IN_TRADE: "Why is the position still held?", EXITING: "Why is the position exiting?",
                       CLOSED: "Why did the position exit?", ATTENTION: "Why does it need attention?", STOPPED: "Why is trading stopped?", ENTRY_FAILED: "Why did the entry fail?" }[a.key] || "Why?";
    const signal = [`Setup ${m.setupLabel}${setup.since ? ` since ${hm(setup.since)}` : ""}${setup.reason_text ? ` · ${setup.reason_text}` : dom.setupReason ? ` · ${dom.setupReason}` : ""}`];
    if (blockers.length && (a.key === "WATCHING" || a.key === "STOPPED")) signal.push(`Not ready: ${blockers.join(" · ")}`);
    const strategy = [sm.best ? `${sm.best} (${sm.bestId}${sm.bestStatus ? ` · ${sm.bestStatus}` : ""})` : "Strategy registry not loaded", `${sm.evaluated} strateg${sm.evaluated === 1 ? "y" : "ies"} evaluated · ${sm.supportingText}`, sm.scoreText];
    const support = [vs(m.ppe, "<=", "watch_enter_stretch", "PPE stretch"), vs(m.confidence, ">=", "ready_confidence", "Confidence"), vs(m.demand, ">=", "ready_demand", "Demand"), `Evidence ${m.evidence}`];
    const against = [...(prop.warnings || []).map((w) => w.text || cap(words(w.code))), ...(prop.blockers || []).map((b) => `Blocker: ${b.text || cap(words(b.code))}`)];
    if (!against.length) against.push("No warning or blocker recorded.");
    const portfolio = [`Open positions ${exec && exec.open_positions != null ? exec.open_positions : "—"} / ${num(limits && limits.max_open_positions) || (exec && exec.max_open_positions) || "—"}`,
                       exec && exec.pending_entries ? `${exec.pending_entries} pending entr${exec.pending_entries === 1 ? "y" : "ies"}` : "No pending entry"];
    const r = riskModel(exec, safety);
    const exe = `Execution: ${bl.title}${bl.level === "ok" ? "" : ` - ${bl.reason}`}`;
    const decision = [];
    if (a.key === "READY") decision.push(READY_NOTE, exe);
    else if (a.key === "WATCHING") decision.push(`The engine holds WATCHING: ${setup.reason_text || dom.setupReason || "setup conditions are not met"}`, exe);
    else if (a.key === "IN_TRADE" || a.key === "EXITING") decision.push(...holdingLines(card, pos, dom, limits, fz));
    else if (a.key === "CLOSED") decision.push(card && card.reason ? `Exit reason: ${card.reason}` : "Trade completed.");
    else if (a.key === "ENTRY_FAILED") decision.push(card && card.line ? card.line : "The entry did not fill.", "No automatic replacement order.");
    else if (a.key === "ENTERING") decision.push("Order submitted. Waiting for broker reconciliation.");
    else decision.push(a.text, exe);
    const el = prop.execution_eligibility;
    const technical = [["PPE stretch", m.ppe], ["Confidence", m.confidence], ["Demand", m.demand], ["Evidence", m.evidence], ["Stored setup state", m.setupLabel], ["Proposal status", m.proposal || "—"],
                       ["Setup id", setup.setup_id || "—"], ["Last evaluated", setup.last_evaluated_at || "—"], ["Proposal id", prop.proposal_id || "—"], ["Reference price", prop.reference_price || "—"],
                       ["Invalidation price", prop.invalidation_price || (pos && pos.invalidation_price) || "—"], ["Risk budget", prop.risk_budget || (pos && pos.risk_budget) || "—"], ["Final quantity", prop.final_qty != null ? prop.final_qty : "—"],
                       ["Execution eligibility", el ? (el.allowed ? "allowed" : `not allowed · ${(el.reason_codes || []).map(words).join(", ") || words(el.state)}`) : "—"], ["Blocker code", bl.code]];
    return { question, key: a.key, sections: [["Signal", signal], ["Strategy", strategy], ["Supporting evidence", support], ["Against / risks", against], ["Portfolio", portfolio], ["Safety", [r.headline, r.primary].filter(Boolean)], ["Decision", decision]],
             technical, blocker: bl, setupReason: setup.reason_text || dom.setupReason || "", blockers };
  }

  // POSITIONS: the lifecycle of one paper position - ENTERING · IN TRADE · EXIT REQUIRED · EXITING · CLOSED · NEEDS ATTENTION - from the persisted position, its card and its intents
  function positionLifecycle(position, card, intents, limits, strategyName, events, frozen) {
    const p = position || {}, c = card || {}, d = c.details || {};
    const exitIntent = (intents || []).find((i) => i.intent_type === "EXIT" && ["PENDING", "SUBMITTED", "PARTIALLY_FILLED", "RECONCILIATION_REQUIRED"].includes(i.state)) || null;
    const st = p.state || d.position_state || "";
    let stage = "IN TRADE";
    if (!position && c.key === "ENTERING") stage = "ENTERING";
    else if (st === "CLOSED" || (!position && c.key === "CLOSED")) stage = "CLOSED";
    else if (st === "ATTENTION" || (!position && c.key === "ATTENTION")) stage = "NEEDS ATTENTION";
    else if (st === "EXIT_SUBMITTED" || exitIntent) stage = "EXITING";
    else if (st === "EXIT_REQUIRED" || c.key === "EXITING") stage = "EXIT REQUIRED";
    const maxHold = num(limits && limits.max_hold_sessions) || MAX_HOLD_DEFAULT;
    const held = num(p.sessions_held != null ? p.sessions_held : d.sessions_held);
    const opened = (events || []).find((e) => e.kind === "OPENED" && (!p.position_id || e.position_id === p.position_id)) || null;
    const pnl = stage === "CLOSED" ? (p.realized_pnl != null ? money(p.realized_pnl) : c.pnl || "—") : c.pnl ? `${c.pnl}${c.pnl_pct ? ` (${Number(c.pnl_pct) >= 0 ? "+" : ""}${c.pnl_pct}%)` : ""}` : "—";
    return { stage, symbol: p.symbol || c.symbol || "—", shares: p.quantity != null ? p.quantity : c.shares != null ? c.shares : "—", entry: p.average_entry_price || c.entry || "—",
             current: stage === "CLOSED" ? p.average_exit_price || c.current || "—" : p.last_price || c.current || "—", pnl, strategy: strategyName || "—",
             entryReason: `READY setup with a reviewable proposal${opened && opened.reason ? ` · ${opened.reason}` : ""}`, holding: held == null ? "—" : `Day ${held} / ${maxHold}`,
             openedAt: p.opened_at || c.since || null, invalidation: p.invalidation_price || d.invalidation_price || null, riskBudget: p.risk_budget || null, exitCycle: num(p.exit_cycle != null ? p.exit_cycle : d.exit_cycle) || 0,
             exitReason: p.exit_reason ? c.reason || cap(words(p.exit_reason)) : c.key === "EXITING" ? c.reason || "" : "", brokerStatus: exitIntent ? `${cap(words(exitIntent.state))}${exitIntent.broker_status ? ` · broker ${words(exitIntent.broker_status)}` : ""}` : "",
             attention: p.attention_reason ? cap(words(p.attention_reason)) : c.key === "ATTENTION" ? c.line || "" : "", closedAt: p.closed_at || null,
             whyHolding: stage === "IN TRADE" || stage === "EXIT REQUIRED" ? holdingLines(card, p, null, limits, frozen || {}) : [] };   // the frozen thresholds when loaded; the stretch rule needs the live panel (TRADE › Why?)
  }

  // ACTIVITY: one timeline (persisted rows only), de-duplicated, grouped for the filters; the Daily Review counts only what the persisted rows support (no market classification, no estimate)
  function dedupe(items) { const seen = new Set(); return items.filter((it) => { const k = `${it.at}|${it.text}`; if (seen.has(k)) return false; seen.add(k); return true; }); }
  function activityItems(src) { return dedupe(timeline(src)).map((it) => ({ ...it, group: GROUP_OF[it.kind] || "system" })); }
  function filterItems(items, filter) { return !filter || filter === "all" ? items : items.filter((it) => it.group === filter); }
  function dailyReview(src, exec, safety, fv, today) {
    const d = (x) => day(x) === today;
    const sys = systemModel(exec, safety);
    const market = (exec && exec.scheduler && exec.scheduler.market) || {};
    const tr = (src.transitions || []).filter((t) => d(t.evaluated_at));
    const entries = (src.intents || []).filter((i) => i.intent_type === "ENTRY" && i.submitted_at && d(i.submitted_at));
    const exits = (src.intents || []).filter((i) => i.intent_type === "EXIT" && i.submitted_at && d(i.submitted_at));
    const closed = (src.positions || []).filter((p) => p.state === "CLOSED" && d(p.closed_at));
    const blocked = (src.proposals || []).filter((p) => p.status === "BLOCKED" && d(p.created_at));
    const safetyBlocks = (src.safety_events || []).filter((e) => ["KILL_ON", "PAUSE_ON", "DAILY_HALT_TRIGGERED"].includes(e.kind) && d(e.at));
    const pnl = closed.reduce((s, p) => s + (num(p.realized_pnl) || 0), 0);
    const attention = ((exec && exec.attention_reasons) || []).length + (src.findings || []).filter((f) => f.blocking && d(f.at)).length + ((fv && fv.attention) || []).length;
    return { today, system: sys.loaded ? sys.level : "—", systemCls: sys.cls, market: market.regular_session == null ? "—" : market.regular_session ? "OPEN" : "CLOSED", setupChanges: tr.length,
             symbols: new Set(tr.map((t) => t.symbol)).size, ready: tr.filter((t) => t.to_state === "READY").length, entries: entries.length, exits: exits.length, closed: closed.length,
             blocked: blocked.length, safetyBlocks: safetyBlocks.length, pnl: closed.length ? money(pnl) : "—", attention };
  }

  // ---------------------------------------------------------------- rendering ------------------------------------------------------
  const lvl = (l) => (l === "ok" ? "pui-ok" : l === "warn" ? "pui-warn" : l === "bad" ? "pui-bad" : "pui-dim");
  function statusHtml(exec, safety, opts) {
    const o = opts || {};
    const segs = statusModel(exec, safety, o);
    const sys = systemModel(exec, safety);
    const seg = (s) => s.key === "system"
      ? `<button type="button" class="pui-seg ${esc(s.cls)}" data-pui-seg="system" data-pui="system" aria-expanded="${o.sysOpen ? "true" : "false"}" title="System status: click for the details">${esc(s.text)} <span class="pui-caret">${o.sysOpen ? "▾" : "▸"}</span></button>`
      : `<span class="pui-seg ${esc(s.cls)}" data-pui-seg="${esc(s.key)}">${esc(s.text)}</span>`;
    const auto = `<span class="pui-auto" data-pui-auto="${esc(segs[3].state)}" title="Paper Auto: the controls are on the Risk card"><span class="pui-seg ${esc(segs[3].cls)}" data-pui-seg="auto">${esc(segs[3].text)}</span></span>`;
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

  function controlsHtml(exec, safety, avail, confirm, opts) {
    const m = controlsModel(exec, safety, avail, opts);
    const row = (key, title, r) => `<div class="pui-ctl-row" data-pui-row="${key}" data-pui-status="${esc(r.label)}"><span class="pui-ctl-name">${esc(title)}</span><b class="pui-ctl-status ${key === "stop" ? (r.label === "ACTIVE" ? "pui-bad" : "pui-dim") : r.label === "ON" || r.label === "ALLOWED" ? "pui-ok" : r.label === "PAUSED" ? "pui-warn" : r.label === "STOPPED" || r.label === "UNKNOWN" ? "pui-bad" : "pui-dim"}">${esc(r.label)}</b>` +
      `<span class="pui-ctl-action">${r.action ? `<button type="button" class="pui-btn ${r.action === "auto-on" ? "go" : r.action === "stop" ? "stop" : r.action === "clear" || r.action === "auto-off" ? "danger" : ""}" data-pui="${esc(r.action)}"${r.enabled ? "" : " disabled"}>${esc(r.actionLabel)}</button>` : ""}</span>` +
      `<span class="pui-ctl-text">${esc(r.text)}</span></div>` + (r.failure ? `<div class="pui-ctl-fail pui-bad" data-pui-auto-error>${esc(r.failure)}</div>` : "") +
      (confirm && ((confirm === "auto-on" && key === "auto") || ((confirm === "stop" || confirm === "clear") && key === "stop")) ? confirmHtml(confirm) : "");
    const sm = sessionModel(exec, safety, opts);                                          // Paper Auto and the gate are independent: the valid combination, named
    return `<div class="pui-ctl" data-pui-controls data-pui-auto-state="${esc(m.auto.key)}"><h5>Automation / Trading Controls</h5>` + row("auto", "Paper Auto", m.auto) + row("trading", "Trading", m.trading) + row("stop", "Emergency Stop", m.stop) +
      `<div class="pui-session" data-pui-session="${esc(sm.headline)}"><span class="pui-session-k">Trading session</span><b>${esc(sm.headline)}</b><span class="pui-sub">${esc(sm.text)}</span></div></div>`;
  }

  function cardsHtml(ctx) {
    const a = agentModel(ctx.card, ctx.dom.setupState, ctx.safety && ctx.safety.state, ctx.exec);
    const m = marketModel(ctx.dom);
    const r = riskModel(ctx.exec, ctx.safety);
    const p = positionModel(ctx.card, ctx.position, ctx.limits);
    const sl = setupLines(a, ctx.dom);
    const note = positionNote(ctx.card, ctx.exec, ctx.safety);
    const sym = ctx.symbol || "—";
    const sm = strategyModel(ctx.card, ctx.dom, ctx.registry, ctx.dom.proposalState);
    const bl = blockerModel(ctx.exec, ctx.safety, ctx.dom, ctx.terminal, ctx.limits);
    const fold = (key, open, label, body) => `<details class="pui-fold pui-why" data-pui-fold="${key}"${open ? " open" : ""}><summary data-pui="fold:${key}">${esc(label)}</summary><div class="pui-why-body">${body}</div></details>`;
    const why = fold("why", !!ctx.whyOpen, "Why?", whyHtml(whyModel(ctx), !!ctx.techOpen));
    // the strategy display (real state only): best setup / best strategy from the registry, no invented supporting strategies, no opportunity score; READY adds the execution line
    const strat = `<div class="pui-strategy" data-pui-strategy="${esc(sm.bestId || "")}"><div class="pui-line"><span class="pui-dim">${esc(sm.label)}</span> · ${esc(sm.loaded ? sm.best || "No strategy result available" : "—")}</div>` +
      (sm.ready ? `<div class="pui-sub">${esc(sm.supportingText)} ${esc(sm.scoreText)}</div>` : "") + `</div>`;
    const exe = sm.ready ? `<div class="pui-exec ${bl.level === "blocked" ? "pui-warn" : bl.level === "ok" ? "pui-ok" : "pui-dim"}" data-pui-exec="${esc(bl.level)}" data-pui-blocker="${esc(bl.code)}"><b>${esc(bl.title)}</b>${bl.level === "ok" ? "" : ` · ${esc(bl.reason)}`}</div>` : "";
    const actions = sm.ready ? `<div class="pui-row pui-actions"><button type="button" class="pui-btn" data-pui="fold:why">Why?</button>${sm.proposalLink ? `<button type="button" class="pui-btn" data-pui="proposal">View Proposal</button>` : ""}</div>` : "";
    const agent = `<div class="pui-card agent primary" data-pui-card="agent" data-pui-primary data-agent="${esc(a.label)}"><h4>Agent · ${esc(sym)}</h4><div class="pui-big">${esc(a.label)}</div><div class="pui-sub">${esc(a.text)}</div>` +
      `${note ? `<div class="pui-line pui-note-line" data-pui-position-note>${esc(note)}</div>` : ""}${sl.line ? `<div class="pui-line">${esc(sl.line)}</div>` : ""}${sl.sub ? `<div class="pui-sub">${esc(sl.sub)}</div>` : ""}${strat}${exe}${actions}${why}</div>`;
    const posDetails = p.open ? fold("pos", !!ctx.posOpen, "Details", `<div class="pui-kv"><span>Position state</span><b>${esc(p.state || "—")}</b><span>Invalidation level</span><b>${esc(p.invalidation || "—")}</b><span>Status</span><b>${esc(p.status)}</b></div>` +
      `<div class="pui-sub">Broker orders, fills and reconciliation live under POSITIONS.</div>`) : "";
    const pos = p.open ? `<div class="pui-card position" data-pui-card="position" data-open="1"><h4>Position</h4><div class="pui-big">${esc(p.symbol)} · ${esc(p.shares)} share${Number(p.shares) === 1 ? "" : "s"}</div>` +
      `<div class="pui-kv"><span>Entry</span><b>${esc(p.entry)}</b><span>Current</span><b>${esc(p.current)}</b><span>P/L</span><b>${esc(p.pnl)}</b></div><div class="pui-line">${esc(p.holding)} · ${esc(p.status)}</div>` +
      (p.reason ? `<div class="pui-sub">${esc(p.reason)}</div>` : "") + (exposureLines(ctx.unified, p.symbol) || "") + posDetails + `</div>`   // Stage 6.9: other accounts named, never merged
      : (exposureCardHtml(p.entering ? null : ctx.unified, ctx.symbol) || accountCardHtml(p.entering ? null : ctx.account, ctx.symbol) ||   // Stage 6.9 / 6.8: holdings the bot does not manage are named, never hidden
         `<div class="pui-card position" data-pui-card="position" data-open="0"><h4>Position</h4><div class="pui-big">${esc(p.entering ? "Entering" : "No position")}</div><div class="pui-sub">${esc(p.entering ? "Order submitted. Waiting for broker fill." : a.key === "ENTRY_FAILED" ? "The entry did not fill. No automatic replacement order." : "")}</div></div>`);
    const riskDetails = fold("risk", !!ctx.riskOpen, "Details", r.lines.slice(1).map((l) => `<div class="${lvl(l.level)}">${l.ok ? "✓" : "⚠"} ${esc(l.text)}</div>`).join("") || `<div class="pui-dim">No further detail.</div>`);
    const risk = `<div class="pui-card risk" data-pui-card="risk" data-risk="${esc(r.level)}" data-kill="${r.killActive ? "1" : "0"}"><h4>Risk</h4><div class="pui-big">${esc(r.headline)}</div>` +
      (r.primary ? `<div class="pui-sub ${r.critical ? "pui-bad" : ""}" data-pui-risk-reason>${esc(r.primary)}</div>` : "") +
      controlsHtml(ctx.exec, ctx.safety, ctx.avail, ctx.confirm, { autoPending: ctx.autoPending, autoError: ctx.autoError }) + riskDetails + `</div>`;
    return focusHtml(priorityModel(ctx)) + agent + pos + risk;
  }

  // the focus strip above the cards: the first item of the priority model (a system problem outranks a stock); never a card, never a decision
  function focusHtml(pr) {
    const f = pr && pr.focus;
    if (!f) return "";
    const act = !f.action ? "" : f.action.go ? `<button type="button" class="pui-link" data-pui="go:${esc(f.action.go)}">${esc(f.action.label)} ▸</button>`
      : f.action.fold ? `<button type="button" class="pui-link" data-pui="fold:${esc(f.action.fold)}">${esc(f.action.label)}</button>` : f.action.pui ? `<button type="button" class="pui-link" data-pui="${esc(f.action.pui)}">${esc(f.action.label)} ▸</button>` : "";
    return `<div class="pui-focus ${esc(f.kind.toLowerCase())}" data-pui-focus="${esc(f.kind)}" data-pui-focus-rank="${f.rank}"><span class="pui-focus-k">Focus</span><b>${esc(f.title)}</b><span class="pui-focus-text">${esc(f.text)}</span>${act}</div>`;
  }

  // the WHY panel body: the question, the seven sections in plain words, the raw values under a Technical details fold (level 3)
  function whyHtml(w, techOpen) {
    return `<div class="pui-why-q">${esc(w.question)}</div>` + w.sections.map(([t, lines]) => `<div class="pui-why-sec" data-pui-why="${esc(t.toLowerCase().replace(/[^a-z]+/g, "-"))}"><span class="pui-why-t">${esc(t)}</span>${lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>`).join("") +
      `<div class="pui-why-sec" data-pui-why="stored"><span class="pui-why-t">Stored state</span><div>Stored setup state ${esc(w.technical[4][1])} · Proposal status ${esc(w.technical[5][1])}</div></div>` +
      `<details class="pui-fold pui-tech" data-pui-fold="tech"${techOpen ? " open" : ""}><summary data-pui="fold:tech">Technical details</summary><div class="pui-kv">${w.technical.map(([k, v]) => `<span>${esc(k)}</span><b>${esc(v)}</b>`).join("")}</div>` +
      (w.setupReason ? `<div class="pui-sub">${esc(w.setupReason)}</div>` : "") + (w.blockers.length ? `<div class="pui-sub">Not ready: ${esc(w.blockers.join(" · "))}</div>` : "") + `</details>`;
  }

  // POSITIONS: one lifecycle card per open or pending paper position (ENTERING · IN TRADE · EXIT REQUIRED · EXITING · NEEDS ATTENTION) with strategy, entry reason, risk / invalidation,
  // holding duration, "Why still holding?" and the exit / broker order status; the exit logic itself is untouched (display of the persisted position only)
  function positionsHtml(rows) {
    if (!rows.length) return `<div class="pui-empty" data-pui-empty>No open paper position. The Agent opens one only when a setup is READY and every risk check passes.</div>`;
    return `<div class="pui-grid">` + rows.map(({ card, model, agent, life }) => {
      const L = life || positionLifecycle(null, card, [], null, null, []);
      return `<div class="pui-pos" data-pui-pos="${esc(card.symbol)}" data-position-id="${esc(card.position_id || "")}" data-pui-stage="${esc(L.stage)}">` +
        `<div class="pui-sym"><span>${esc(card.symbol)}</span><span class="pui-state">${esc(L.stage)}</span></div>` +
        (model.open ? `<div class="pui-kv"><span>Quantity</span><b>${esc(model.shares)}</b><span>Entry</span><b>${esc(model.entry)}</b><span>Current</span><b>${esc(model.current)}</b><span>P/L</span><b>${esc(model.pnl)}</b><span>Holding</span><b>${esc(model.holding)}</b></div>`
                    : `<div class="pui-sub">${esc(card.line || agent.text)}</div>`) +
        `<div class="pui-kv pui-life"><span>Strategy</span><b>${esc(L.strategy)}</b><span>Entry reason</span><b>${esc(L.entryReason)}</b>` +
        (L.invalidation ? `<span>Risk / invalidation</span><b>${esc(L.invalidation)}${L.riskBudget ? ` · budget ${esc(L.riskBudget)}` : ""}</b>` : "") + (L.openedAt ? `<span>Holding since</span><b>${esc(day(L.openedAt))} ${esc(hm(L.openedAt))}</b>` : "") + `</div>` +
        (L.stage === "EXIT REQUIRED" || L.stage === "EXITING" ? `<div class="pui-line pui-warn" data-pui-exit>${esc(L.stage)}${L.exitReason ? ` · ${esc(L.exitReason)}` : ""}${L.brokerStatus ? ` · order ${esc(L.brokerStatus)}` : ""}</div>` : "") +
        (L.stage === "NEEDS ATTENTION" && L.attention ? `<div class="pui-line pui-bad">${esc(L.attention)}</div>` : "") +
        (L.whyHolding.length ? `<details class="pui-fold" data-pui-fold="holding"><summary>Why still holding?</summary><div class="pui-why-body">${L.whyHolding.map((l) => `<div>${esc(l)}</div>`).join("")}</div></details>` : "") +
        `<div class="pui-sub">${esc(card.reason || "")}</div></div>`;
    }).join("") + `</div>`;
  }

  // the closed paper trades: realized paper P/L and the exit reason per position (persisted rows; nothing recomputed)
  function closedHtml(positions, strategyName, limits) {
    const closed = (positions || []).filter((p) => p.state === "CLOSED").sort((a, b) => ((a.closed_at || "") < (b.closed_at || "") ? 1 : -1)).slice(0, 20);
    if (!closed.length) return `<div class="pui-sub" data-pui-closed-empty>No closed paper trade yet.</div>`;
    return `<div class="pui-grid" data-pui-closed>` + closed.map((p) => { const L = positionLifecycle(p, { key: "CLOSED", symbol: p.symbol }, [], limits, strategyName, []);
      return `<div class="pui-pos pui-closed" data-pui-closed-pos="${esc(p.symbol)}" data-pui-stage="CLOSED"><div class="pui-sym"><span>${esc(p.symbol)}</span><span class="pui-state">CLOSED</span></div>` +
        `<div class="pui-kv"><span>Realized paper P/L</span><b>${esc(L.pnl)}</b><span>Exit reason</span><b>${esc(L.exitReason || "—")}</b><span>Entry → exit</span><b>${esc(L.entry)} → ${esc(L.current)}</b><span>Shares</span><b>${esc(p.entry_qty != null ? p.entry_qty : "—")}</b>` +
        `<span>Held</span><b>${esc(L.holding)}</b><span>Closed</span><b>${esc(day(p.closed_at))} ${esc(hm(p.closed_at))}</b></div></div>`; }).join("") + `</div>`;
  }

  function positionDetailsHtml(d) {
    const kv = (rows) => `<div class="pui-kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><b>${esc(v == null || v === "" ? "—" : v)}</b>`).join("")}</div>`;
    const ev = (d.events || []).slice(-8).map((e) => `<li>${esc(hm(e.at))} · ${esc(words(e.kind))}${e.to_state ? ` → ${esc(words(e.to_state))}` : ""}${e.reason ? ` · ${esc(words(e.reason))}` : ""}</li>`).join("");
    const intents = (d.intents || []).map((i) => `<li>${esc(i.intent_type)} ${esc(i.side)} ${esc(i.qty)} · ${esc(words(i.state))}${i.filled_qty ? ` · filled ${esc(i.filled_qty)} @ ${esc(i.filled_avg_price || "—")}` : ""}${i.broker_order_id ? ` · broker ${esc(String(i.broker_order_id).slice(0, 8))}` : ""}</li>`).join("");
    return `<div class="pui-pos-details" data-pui-pos-details="${esc(d.symbol)}"><b>${esc(d.symbol)}</b> · ${esc(d.agent)}` +
      kv([["Setup state", d.setup], ["Demand", d.demand], ["Evidence", d.evidence], ["Exit monitoring", d.exit], ["Risk", d.risk], ["Position state", d.state], ["Invalidation level", d.invalidation]]) +
      `<div class="pui-sub">Broker orders and fills</div><ul>${intents || "<li>none</li>"}</ul><div class="pui-sub">Recent events</div><ul>${ev || "<li>none</li>"}</ul></div>`;
  }

  function activityHtml(src, exec, fv, today, opts) {
    const o = opts || {};
    const dr = dailyReview(src, exec, o.safety, fv, today);
    const items = filterItems(activityItems(src), o.filter).slice(0, 120);
    let last = "";
    const rows = items.map((it) => { const d = day(it.at); const head = d !== last ? `<li class="pui-day">${esc(d === today ? "Today" : d)}</li>` : ""; last = d; return head + `<li class="${esc(it.cls)}" data-pui-kind="${esc(it.kind)}" data-pui-group="${esc(it.group)}"><time>${esc(hm(it.at))}</time><span>${esc(it.text)}</span></li>`; }).join("");
    // the Daily Review: only counts the persisted rows support (no market classification, no estimate); the market state is the scheduler's regular-session flag
    const review = `<div class="pui-today" data-pui-today>` + [["System", dr.system], ["Market", dr.market], ["Setup changes", dr.setupChanges], ["READY setups", dr.ready], ["Entries", dr.entries], ["Exits", dr.exits],
      ["Blocked", dr.blocked], ["Safety blocks", dr.safetyBlocks], ["Paper P/L", dr.pnl], ["Attention", dr.attention]]
      .map(([k, v]) => `<div class="pui-card"><h4>${esc(k)}</h4><div class="pui-big ${k === "Attention" && v ? "pui-bad" : k === "System" ? lvl(dr.systemCls) : ""}">${esc(v)}</div></div>`).join("") + `</div>`;
    const filters = `<div class="pui-filters" data-pui-filters>${FILTERS.map(([id, label]) => `<button type="button" class="pui-chip${(o.filter || "all") === id ? " active" : ""}" data-pui-filter="${id}">${esc(label)}</button>`).join("")}</div>`;
    const FVlib = G.ForwardValidation || null;
    const fvm = fv && FVlib ? FVlib.summaryModel(fv) : null;
    const val = `<div class="pui-validation" data-pui-validation><h3>Validation</h3>` + (fvm && fvm.loaded ?
      `<div class="pui-kv"><span>Sessions</span><b>${esc(fvm.sessionsLabel)}</b><span>Healthy</span><b>${esc(fvm.healthy)}</b><span>Duplicates</span><b>${esc(fvm.duplicates)}</b><span>Recon issues</span><b>${esc(fvm.reconIssues)}</b></div>` +
      `<div class="pui-line" style="margin-top:6px">Strategy <b>${esc(fvm.edgeLabel)}</b>${fvm.insufficient ? ` <span class="pui-sub">${esc(fvm.insufficientText)}</span>` : ""}</div>` +
      (fvm.needsAttention ? `<div class="pui-bad">⚠ ${esc(fvm.critical ? "NEEDS ATTENTION" : "Needs a look")}: ${fvm.attention.map((a) => esc(words(a.code))).join(", ")}</div>` : "")
      : `<div class="pui-empty">Validation status unavailable.</div>`) + `<div><a href="forward_validation.html" target="_blank" rel="noopener" data-pui-fv-link>View validation details</a></div></div>`;
    return `<h2>Activity</h2><h3>Today${dr.today ? ` · ${esc(dr.today)}` : ""}</h3>` + review + filters +
      `<ul class="pui-timeline" data-pui-timeline>${rows || '<li class="pui-day">No activity yet.</li>'}</ul>` + val +
      `<details class="pui-fold" data-pui-alerts-fold><summary>Rule alerts and setup alerts (legacy lists)</summary></details>`;
  }

  // MORE: three collapsed groups (progressive disclosure); a group opens on click, an item opens its panel below or its legacy workspace; the Diagnostics tools are a nested fold
  function moreHtml(groups, open) {
    const o = open || {};
    const item = (it) => it.href ? `<a href="${esc(it.href)}" target="_blank" rel="noopener" data-pui-more="${esc(it.href)}">${esc(it.label)}</a>`
      : it.tools ? `<details class="pui-more-sub" data-pui-sub="${esc(it.action || "tools")}"${o[it.action] ? " open" : ""}><summary data-pui-sub-toggle="${esc(it.action || "tools")}">${esc(it.label)}</summary>${it.tools.map((t) => `<button type="button" data-pui-more="tab:${esc(t.tab)}">${esc(t.label)}</button>`).join("")}</details>`
      : `<button type="button" data-pui-more="${esc(it.action ? "action:" + it.action : "tab:" + it.tab + (it.lab && it.lab !== true ? ":" + it.lab : ""))}">${esc(it.label)}</button>`;
    return `<h2>More</h2><p class="pui-sub">Research, system and settings. Nothing here is removed; it is simply not part of the daily TRADE workflow.</p><div class="pui-more">` +
      groups.map((g) => `<details class="pui-more-group" data-pui-group="${esc(g.title)}"${o[g.id] ? " open" : ""}><summary data-pui-group-toggle="${esc(g.id)}"><h3>${esc(g.title)}</h3><span class="pui-sub">${esc(g.text)}</span></summary>` +
        g.items.map(item).join("") + (g.id === "settings" ? `<span class="pui-sub">Server-side configuration (config.py / .env). Secrets are never shown.</span>` : "") + `</details>`).join("") + `</div>` +
      `<div id="pui-more-body"></div>`;
  }

  // the Strategy Lab registry (MORE › Research › Strategy Lab): every registered strategy with its real lifecycle status; placeholders are marked as such and never active
  function registryHtml(reg, views, error) {
    const rows = (reg && reg.strategies) || [];
    const badge = (s) => `<span class="pui-badge ${s.implemented ? (s.active ? "ok" : "") : "dim"}" data-pui-status="${esc(s.status)}">${esc(s.status_label || cap(words(s.status)))}</span>` +
      (s.implemented ? "" : ` <span class="pui-badge dim" data-pui-placeholder>placeholder · not active</span>`);
    const qc = reg && reg.quantconnect;
    return `<div class="pui-lab" data-pui-lab-panel><h3>Strategy Lab</h3><p class="pui-sub">${esc((reg && reg.disclaimer) || "")}</p>` +
      `<div class="pui-pipeline" data-pui-pipeline>${((reg && reg.pipeline) || []).map(esc).join(" → ")}</div>` +
      `<div class="pui-sources">${((reg && reg.sources) || []).map((s) => `<span class="pui-badge" data-pui-source="${esc(s.source)}" title="${esc(s.note || "")}">${esc(s.source)} · ${s.count}</span>`).join("")}` +
      (qc && !qc.integrated ? `<span class="pui-sub">QuantConnect: source supported, ${qc.registered} registered, not integrated (adapter architecture only).</span>` : "") + `</div>` +
      `<table class="pui-registry" data-pui-registry><thead><tr><th>Strategy</th><th>Source</th><th>Lifecycle</th><th>Validation</th><th>Horizon</th><th>Direction</th><th>Version</th><th>Updated</th></tr></thead><tbody>` +
      rows.map((s) => `<tr data-pui-strategy-row="${esc(s.strategy_id)}" data-pui-implemented="${s.implemented ? "1" : "0"}" data-pui-active="${s.active ? "1" : "0"}"><td><button type="button" class="pui-link" data-pui-lab="${esc(s.strategy_id)}">${esc(s.display_name)}</button><div class="pui-sub">${esc(s.strategy_id)}</div></td>` +
        `<td>${esc(s.source)}</td><td>${badge(s)}</td><td>${svBadge(s)}</td><td>${esc(words(s.time_horizon))}</td><td>${esc(words(s.direction_capability))}</td><td>${esc(s.version)}</td><td>${esc(s.updated_at)}</td></tr>`).join("") + `</tbody></table>` +
      (error ? `<div class="pui-bad">${esc(error)}</div>` : "") +
      `<details class="pui-fold" data-pui-lab-tools><summary>Legacy research workspaces (${views.length})</summary><div class="pui-more-group pui-tools">${views.map((v) => `<button type="button" data-pui-more="tab:strategy:${esc(v.view)}">${esc(v.label)}</button>`).join("") || `<span class="pui-sub">Open MORE › Research › Backtests once to load them.</span>`}</div></details></div>`;
  }

  // Stage 6.2: the historical validation of a strategy (GET /api/strategy-registry/{id} carries it). No run -> "Not yet evaluated" and why; a run -> its verdict, the
  // TRAIN / OOS metrics of the RULES arm after friction, the arm comparison on the one candidate set, causality, the mechanical audit. Display only: a verdict never
  // changes the lifecycle, and nothing here starts a run (runs are made with the research CLI).
  const SV_CLS = { PASS: "ok", FAIL: "bad", INVALID: "bad", MIXED: "", INSUFFICIENT_DATA: "dim" };
  function svBadge(s) {
    const st = (s && s.validation_status) || "NOT_RUN";
    return `<span class="pui-badge ${st === "NOT_RUN" ? "dim" : SV_CLS[st] || ""}" data-pui-sv-status="${esc(st)}">${esc(st === "NOT_RUN" ? "Not yet evaluated" : (s.validation_label || words(st)))}</span>` +
      (s && s.evaluation_mode === "CROSS_SECTIONAL_PORTFOLIO" && s.selection_edge_status ? ` ${svEdgeBadge(s.selection_edge_status, s.selection_edge_label)}` : "") +
      (s && s.universe_integrity_status === "UNTRUSTED" ? ` <span class="pui-badge bad" data-pui-sv-universe-badge="UNTRUSTED">Universe untrusted</span>` : "") +
      (s && s.validation_synthetic ? ` <span class="pui-badge dim" data-pui-sv-synthetic>synthetic test data</span>` : "");
  }
  // Stage 6.4.1 (validation_v3): a cross-sectional strategy is judged on its SELECTION EDGE over a declared SELECTION CONTROL, one observation per
  // rebalance period. The page keeps three things apart: the absolute performance (per leg: description, not selection evidence), the control
  // comparison (the strategy vs the control that omits the selection; SPY only as the MARKET BENCHMARK) and the selection-edge status.
  const SV_EDGE_CLS = { ESTABLISHED: "ok", WEAK: "", NOT_ESTABLISHED: "bad", INSUFFICIENT_DATA: "dim", INVALID: "bad", NOT_APPLICABLE: "dim" };
  function svEdgeBadge(st, label) {
    return `<span class="pui-badge ${SV_EDGE_CLS[st] != null ? SV_EDGE_CLS[st] : "dim"}" data-pui-sv-edge="${esc(st)}">Selection edge: ${esc(label || words(st))}</span>`;
  }
  const svPp = (x) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)} pp`);
  const svPct2 = (x) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)}%`);
  function svControl(cs) {
    const sg = cs.segments || {}, segs = ["TRAIN", "OOS", "ALL"], ctl = cs.control || {}, mb = cs.market_benchmark || {};
    const row = (label, f, attr) => `<tr data-pui-sv-cs-row="${esc(attr)}"><td>${label}</td>${segs.map((k) => `<td>${esc(f(sg[k] || {}))}</td>`).join("")}</tr>`;
    const par = cs.parity || {}, excl = Object.entries(cs.excluded_periods || {});
    return `<div class="pui-sv-scroll"><table class="pui-sv-table" data-pui-sv-control><thead><tr><th>Per rebalance period</th><th>TRAIN</th><th>OOS</th><th>All</th></tr></thead><tbody>` +
      row("Rebalance periods", (x) => svNum(x.period_count, 0), "periods") +
      row("Strategy", (x) => svPct(x.strategy_total_return), "strategy") +
      row(`<span class="pui-badge" data-pui-sv-role="SELECTION_CONTROL">SELECTION CONTROL</span> ${esc(ctl.label || ctl.control_id || "")}`, (x) => svPct(x.control_total_return), "control") +
      row("Excess over the control", (x) => svPp(x.cumulative_excess_return), "excess") +
      row("Mean excess per period", (x) => svPct2(x.mean_excess_period_return), "mean_excess") +
      row("Excess t-statistic (per period)", (x) => svNum(x.excess_t_stat), "t") +
      row("Excess win rate (periods)", (x) => svPct(x.excess_win_rate), "win") +
      row("Active volatility (annualized)", (x) => svPct(x.active_volatility), "active_vol") +
      row("Worst relative period", (x) => (x.worst_relative_period ? `${x.worst_relative_period.rebalance} ${svPct2(x.worst_relative_period.excess)}` : "—"), "worst") +
      row("Max relative drawdown", (x) => svPct(x.max_relative_drawdown), "rel_dd") +
      row("Turnover (strategy / control)", (x) => (x.strategy_turnover == null ? "—" : `${svPct(x.strategy_turnover)} / ${svPct(x.control_turnover)}`), "turnover") +
      row(`<span class="pui-badge dim" data-pui-sv-role="MARKET_BENCHMARK">MARKET BENCHMARK</span> ${esc(mb.symbol || "")}`, (x) => svPct(x.benchmark_total_return), "benchmark") +
      `</tbody></table></div><div class="pui-sub" data-pui-sv-cs-note>${esc(cs.unit || "")}. The selection control holds the same eligible universe with the same timing, ` +
      `friction and capital basis, without the selection. ${esc(mb.symbol || "The market benchmark")} is a market benchmark only: beating it is not evidence of selection.</div>` +
      `<div class="pui-sub" data-pui-sv-parity="${esc(par.status || "—")}">${esc(par.note || "")}` +
      `${excl.length ? ` · excluded on both sides: ${esc(excl.map(([k, n]) => `${n} ${words(k)}`).join(", "))}` : ""}</div>`;
  }
  // Stage 6.5.1: the universe a cross-sectional strategy ranked, and what its integrity does to the selection edge (UNTRUSTED: cannot establish
  // alpha). The section says plainly that a fair same-universe control does not remove survivorship bias.
  const SV_UI_CLS = { TRUSTED: "ok", LIMITED: "", UNTRUSTED: "bad", NOT_APPLICABLE: "dim" };
  function svUniverse(u) {
    if (!u) return `<div class="pui-sub" data-pui-sv-universe="NOT_ASSESSED">Universe integrity: not assessed (a record from before Stage 6.5.1).</div>`;
    const yn = (x) => (x === true ? "Yes" : x === false ? "No" : "Unknown");
    const bias = { PRESENT: "Present", ABSENT: "Absent", UNKNOWN: "Unknown" }[u.survivorship_bias] || "Unknown";
    const capped = u.raw_edge_status && u.final_edge_status && u.raw_edge_status !== u.final_edge_status;
    return `<div class="pui-kv" data-pui-sv-universe="${esc(u.status || "MISSING")}"><span>Universe method</span><b>${esc(cap(words(u.method || "not declared")))}</b>` +
      `<span>Point-in-time membership</span><b>${esc(u.point_in_time ? "Yes" : "No")}</b><span>Survivorship bias (declared)</span><b data-pui-sv-bias="${esc(u.survivorship_bias || "")}">${esc(bias)}</b>` +
      `<span>Delisted securities included</span><b>${esc(yn(u.includes_delisted))}</b>` +
      `<span>Integrity status</span><b><span class="pui-badge ${SV_UI_CLS[u.status] != null ? SV_UI_CLS[u.status] : "bad"}" data-pui-sv-universe-status="${esc(u.status || "MISSING")}">${esc(u.status || "Not declared")}</span></b>` +
      `<span>Effect on the selection-edge verdict</span><b>${esc(u.consequence || "")}</b>` +
      `<span>Selection edge</span><b data-pui-sv-edge-raw-final>raw ${esc(cap(words(u.raw_edge_status || "—")))} → final ${esc(cap(words(u.final_edge_status || "—")))}${capped ? " (UNIVERSE_INTEGRITY_CAP)" : ""}</b></div>` +
      `<div class="pui-sub">A fair control built from the same universe isolates the ranking within that universe; it does not remove survivorship bias and ` +
      `cannot show that the universe itself was historically unbiased.${u.declaration && u.declaration.universe_source ? ` Source: ${esc(u.declaration.universe_source)}.` : ""}</div>` +
      (u.source === "not_assessed" ? `<div class="pui-sub" data-pui-sv-universe-source="not_assessed">No universe-integrity assessment covers this record yet: ` +
        `its universe is treated as unknown, so the selection edge is capped (python -m strategy_validation assess-universe --run ...).</div>` : "") +
      (u.assessment ? `<div class="pui-sub" data-pui-sv-universe-assessment="${esc(u.assessment.assessment_id)}">${esc(u.assessment.policy_version)} assessment (${esc(u.assessment.created_at)}) of a ` +
        `stored run: the run is unchanged.</div>` : "");
  }
  // Stage 6.6: a cross-sectional strategy's research diagnostics in brief (turnover, friction, concentration, the selected losers' next week, the
  // market's up / down periods) - descriptive, never a gate and never selection-edge evidence; a field the diagnostics lack is not shown
  function svResearch(s) {
    if (!s || !(s.construction || s.friction || s.concentration || s.exposure)) return "";
    const c = s.construction || {}, f = s.friction || {}, cc = s.concentration || {}, ec = cc.excess_contribution || {}, k = s.falling_knife || {};
    const mk = s.market_weeks || s.market_months || {}, up = (mk.ALL || {}).benchmark_up, dn = (mk.ALL || {}).benchmark_down;
    const share = (x) => (!x ? "—" : x.share_of_gross_excess_consumed != null ? svPct(x.share_of_gross_excess_consumed) : "no positive gross excess");
    const grp = ((cc.sic_major_group || {}).most_frequent_top_groups || [])[0];
    // Stage 6.7 (Breakout): exposure, the cash-free selection effect, follow-through, failed breakouts, the cash part, the Momentum overlap
    const ex = (s.exposure || {}).ALL, bk = ex && ex.weeks_by_positions, ic = (s.invested_capital || {}).OOS, ft = s.follow_through || {}, fb = s.failed_breakouts || {};
    const ms = (s.market_sensitivity || {}).ALL, ov = s.momentum_overlap || {};
    const kv0 = [
      ex && ["Invested fraction (mean / median / min / max)", `${svPct(ex.mean_invested_fraction)} / ${svPct(ex.median_invested_fraction)} / ${svPct(ex.min_invested_fraction)} / ${svPct(ex.max_invested_fraction)} · ${svNum(ex.mean_positions, 1)} positions on average`],
      bk && ["Weeks by number of positions", ["0", "1-5", "6-10", "11-19", "20"].filter((k) => k in bk).map((k) => `${k}: ${bk[k]}`).join(" · ")],   // declared order (JS sorts integer keys first)
      ms && ms.strategy_beta_to_spy != null && ["Beta to SPY (strategy / control)", `${svNum(ms.strategy_beta_to_spy)} / ${svNum(ms.control_beta_to_spy)}`],
      ic && ic.selection_part_mean != null && ["Weekly excess split (OOS): selection part / cash part", `${svPct2(ic.selection_part_mean)} (t ${svNum(ic.selection_part_t)}) / ${svPct2(ic.cash_part_mean)}`],
      ic && ic.weeks_with_a_position != null && ["Excess on invested capital (OOS)", ic.weeks_with_a_position ? `${svPct2(ic.mean_excess_on_invested_capital)} a week, t ${svNum(ic.t_stat)} over ${ic.weeks_with_a_position} weeks with a position (unweighted)` : "no week with a position"],
      ft.legs && ["Follow-through (next week)", `${svPct(ft.continued_higher)} continued higher · ${svPct(ft.flat)} flat · ${svPct(ft.failed)} failed`],
      fb.legs && ["Failed breakouts", `${svPct(fb.share_ending_below_entry_price)} ended below the entry price · ${svPct(fb.share_ending_below_breakout_reference)} below the breakout reference`],
      ec.cash != null && ["Cash part of the summed excess", `${svPct2(ec.cash)} (names ${svPct2(ec.names)})`],
      ov.weeks && ["Overlap with the Momentum portfolio", `${svPct(ov.share_of_breakout_holdings_in_momentum_portfolio)} of holdings${ov.share_expected_by_chance != null ? ` (${svPct(ov.share_expected_by_chance)} by chance)` : ""} · weekly excess correlation ${svNum(ov.weekly_excess_correlation)}`],
    ];
    const kv = [...kv0,
      c.average_one_way_turnover != null && ["One-way turnover", `${svPct(c.average_one_way_turnover)} per period · ${svNum(c.annualized_one_way_turnover, 1)}× a year`],
      f.ALL && ["Gross excess consumed by friction", `TRAIN ${share(f.TRAIN)} · OOS ${share(f.OOS)} · All ${share(f.ALL)}`],
      f.ALL && f.ALL.strategy_friction_per_year != null && ["Strategy friction", `${svPct(f.ALL.strategy_friction_per_year)} a year`],
      ec.top_five_share_of_summed_excess != null && ["Top five names' share of the summed excess", svPct(ec.top_five_share_of_summed_excess)],
      (ec.top || []).length && ["Largest contributors", ec.top.map((x) => `${x[0]} ${svPct2(x[1])}`).join(" · ")],
      (cc.most_held_names || []).length && ["Most held", cc.most_held_names.slice(0, 5).map((x) => `${x[0]} ${svPct(x[1])}`).join(" · ")],
      grp && ["Most frequent top sector", `${grp[0]} (${grp[1]} rebalances)`],
      k.legs && ["Selected losers, next week", `${svPct(k.share_rebounding)} rebounded · ${svPct(k.share_falling_further)} fell further · ${svPct(k.share_beating_control)} beat the control`],
      up && dn && ["Mean excess when SPY rose / fell", `${svPct2(up.mean_excess)} (${svNum(up.periods, 0)}) / ${svPct2(dn.mean_excess)} (${svNum(dn.periods, 0)})`],
    ].filter(Boolean);
    if (!kv.length) return "";
    return `<h4 data-pui-sv-section="research">${ex ? "Exposure, follow-through, turnover and concentration" : "Turnover, friction and concentration"}</h4><div class="pui-kv" data-pui-sv-research>` +
      kv.map(([a, b]) => `<span>${esc(a)}</span><b>${esc(b)}</b>`).join("") + `</div>` +
      `<div class="pui-sub">Research diagnostics: descriptive, never a gate and not selection-edge evidence (the selection edge above is).</div>`;
  }
  function svEdge(edge, cs, ev) {
    const oo = ((cs || {}).segments || {}).OOS || {}, conc = oo.concentration || {}, pol = edge.policy || {};
    return `<div class="pui-sv-edge" data-pui-sv-edge-block="${esc(edge.status)}">${svEdgeBadge(edge.status, edge.label)}</div>` +
      (edge.reasons || []).map((x) => `<div data-pui-sv-edge-reason="${esc(x.code)}">${esc(x.text)}</div>`).join("") +
      `<div class="pui-sub">Primary statistic: OOS mean excess per period ${esc(svPct2(oo.mean_excess_period_return))}, t = ${esc(svNum(oo.excess_t_stat))} over ` +
      `${esc(svNum(oo.period_count, 0))} periods${conc.applicable ? ` · ${esc(conc.text)}` : ""}. Established needs at least ${esc(pol.min_oos_periods)} OOS periods, ` +
      `t >= ${esc(pol.t_established)}, no concentration and no strong TRAIN contradiction - never t alone.</div>` +
      (ev ? `<div class="pui-sub" data-pui-sv-evaluation="${esc(ev.evaluation_id)}">Methodology ${esc(ev.methodology_version)} evaluation of run ${esc(ev.run_id)} ` +
        `(${esc(ev.created_at)}), recorded under ${esc(ev.evaluated_run_validation_version)}: that record is unchanged; its own verdict is the absolute performance verdict.</div>` : "");
  }
  const svNum = (x, d) => (x == null || x === "" ? "—" : typeof x === "number" ? x.toFixed(d == null ? 2 : d) : String(x));
  const svPct = (x) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
  const SV_ROWS = [["Trades", "trade_count", 0], ["Win rate", "win_rate", "pct"], ["Gross P&L", "gross_pnl"], ["Slippage", "slippage_cost"], ["Fees", "fees"], ["Net P&L", "net_pnl"],
                   ["Mean R (net)", "mean_R", 3], ["Mean R (gross)", "average_R_gross", 3], ["Mean R t-stat", "mean_R_t_stat"], ["Expectancy ($ / trade)", "expectancy_usd"],
                   ["Profit factor", "profit_factor"], ["Max drawdown", "max_drawdown"], ["Max drawdown %", "max_drawdown_pct"], ["Sharpe (per trade)", "sharpe_per_trade"],
                   ["Average holding (bars)", "average_holding_bars", 1], ["Ambiguous bars (stop first)", "ambiguous_bars_count", 0], ["Configured risk", "configured_risk_total"],
                   ["Achieved risk", "achieved_risk_total"], ["Skipped in simulation", "simulation_skipped_count", 0],
                   ["Purged at the TRAIN / OOS boundary", "boundary_purged_count", 0]];
  // Stage 6.4: the portfolio research view of a cross-sectional strategy (never a gate): strategy vs SPY vs the equal-weight universe control, and
  // the excess over the control tested per rebalance period (the independent unit); absent for strategies without it - nothing is invented
  function svPortfolio(s) {
    const p = s && s.portfolio;
    if (!p) return "";
    const segs = ["TRAIN", "OOS", "ALL"], pct = (x) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
    const row = (label, f) => `<tr><td>${esc(label)}</td>${segs.map((k) => `<td>${esc(f(p[k] || {}))}</td>`).join("")}</tr>`;
    return `<h4>Portfolio (research diagnostics)</h4><div class="pui-sv-scroll"><table class="pui-sv-table" data-pui-sv-portfolio><thead><tr><th>Equal-weight portfolio</th><th>TRAIN</th><th>OOS</th><th>All</th></tr></thead><tbody>` +
      row("Strategy total return", (x) => pct((x.strategy || {}).total_return)) + row("Market benchmark (SPY) total return", (x) => pct((x.benchmark || {}).total_return)) +
      row("Selection control (equal-weight universe) total return", (x) => pct((x.universe_equal_weight || {}).total_return)) +
      row("Excess vs the market benchmark (SPY)", (x) => pct(x.excess_total_return)) +
      row("Excess vs the selection control", (x) => pct(x.excess_vs_universe)) +
      row("Excess vs universe: t per rebalance period", (x) => svNum((x.excess_vs_universe_per_period || {}).t_stat)) +
      row("Sharpe (strategy / SPY)", (x) => `${svNum((x.strategy || {}).sharpe)} / ${svNum((x.benchmark || {}).sharpe)}`) +
      row("Max drawdown (strategy / SPY)", (x) => `${pct((x.strategy || {}).max_drawdown)} / ${pct((x.benchmark || {}).max_drawdown)}`) +
      `</tbody></table></div><div class="pui-sub">${esc(s.note || "")}. The universe control shares the universe's survivorship bias; the excess over it is what the selection added.</div>`;
  }
  function validationHtml(v) {
    v = v || {};
    const st = v.validation_status || "NOT_RUN", r = v.run;
    const head = `<h4>Validation ${svBadge(v)}</h4>`;
    if (!r) {
      const pol = v.policy || {};
      return `<div class="pui-sv" data-pui-sv data-pui-sv-state="${esc(st)}">${head}<div>Not yet evaluated.</div>` + (v.not_run_reason ? `<div class="pui-sub" data-pui-sv-reason>${esc(v.not_run_reason)}</div>` : "") +
        (pol.min_trades ? `<div class="pui-sub">A verdict needs at least ${esc(pol.min_trades)} trades, ${esc(pol.min_oos_trades)} out-of-sample trades and ${esc(pol.min_oos_sessions)} out-of-sample sessions; below that it reads INSUFFICIENT DATA.</div>` : "") +
        `<div class="pui-sub">${esc(v.disclaimer || "")}</div></div>`;
    }
    const vd = v.overall_verdict || r.verdict || {}, arms = r.arms || {}, rules = arms.RULES || {}, au = r.audit || {}, ca = r.causality || {}, ig = r.integrity || {}, fr = r.friction_model || {}, dg = r.diagnostics || {};
    const cs = v.evaluation_mode === "CROSS_SECTIONAL_PORTFOLIO" && v.cross_sectional ? v.cross_sectional : null, absV = v.absolute_verdict || r.verdict || {};
    const cell = (m, row) => { const x = m ? m[row[1]] : null; return row[2] === "pct" ? svPct(x) : svNum(x, row[2]); };
    const rowLabel = (row) => (cs && row[1] === "mean_R_t_stat" ? "Mean R t-stat (per leg · NOT SELECTION-EDGE EVIDENCE)" : row[0]);
    const metrics = `<div class="pui-sv-scroll"><table class="pui-sv-table" data-pui-sv-metrics><thead><tr><th>RULES arm</th><th>TRAIN</th><th>OOS</th><th>All</th></tr></thead><tbody>` +
      SV_ROWS.map((row) => `<tr><td>${esc(rowLabel(row))}</td><td>${cell(rules.TRAIN, row)}</td><td>${cell(rules.OOS, row)}</td><td>${cell(rules.ALL, row)}</td></tr>`).join("") + `</tbody></table></div>`;
    const armRow = (name) => {
      const a = arms[name] || { status: "NOT_RUN", label: "Not run" }, o = a.OOS || {}, all = a.ALL || {}, evald = a.status === "EVALUATED";
      return `<tr data-pui-sv-arm="${esc(name)}" data-pui-sv-arm-status="${esc(a.status)}"><td>${esc(name)}</td><td>${esc(a.candidate_count != null ? a.candidate_count : "—")}</td>` +
        `<td>${evald ? esc(svNum(all.accepted_count, 0)) : "—"}</td><td>${evald ? esc(svNum(all.stand_aside_count, 0)) : "—"}</td><td>${evald ? esc(svNum(o.trade_count, 0)) : "—"}</td>` +
        `<td>${evald ? esc(svNum(o.net_pnl)) : "—"}</td><td>${evald ? esc(svNum(o.mean_R, 3)) : "—"}</td><td>${esc(a.status === "NOT_EVALUATED" ? "Not evaluated" : a.label || words(a.status))}</td></tr>` +
        (a.note ? `<tr class="pui-sv-note"><td></td><td colspan="7" class="pui-sub">${esc(a.note)}</td></tr>` : "");
    };
    const armTable = `<div class="pui-sv-scroll"><table class="pui-sv-table" data-pui-sv-arms><thead><tr><th>Arm</th><th>Candidates</th><th>Accepted</th><th>Stand aside</th><th>OOS trades</th><th>OOS net P&L</th><th>OOS mean R</th><th>Verdict</th></tr></thead><tbody>` +
      ["RULES", "GATED", "MODEL"].map(armRow).join("") + `</tbody></table></div>` +
      `<div class="pui-sub" data-pui-sv-same="${ig.same_candidate_set ? "1" : "0"}">${ig.same_candidate_set ? `One candidate set for every arm (${esc(r.candidate_count)} candidates, set ${esc(String(ig.candidate_set_hash || r.candidate_set_hash || "").slice(0, 12))}).`
        : `VALIDATION INVALID: the arms did not receive the same candidate set. ${esc((ig.problems || []).join(" · "))}`}</div>`;
    const crit = (ca.automated_checks || []).filter((c) => c.result === "CRITICAL"), warn = (ca.automated_checks || []).filter((c) => c.result === "WARN");
    const ttl = dg.train_vs_oos || {}, sens = dg.sensitivity || {};
    // Stage 6.3.1: validation integrity is a separate question from the performance verdict; a Stage 6.2 run never recorded it (shown as such)
    const ig2 = r.integrity_gate || {}, acc = r.accounting || {}, bnd = r.boundary || {};   // r.integrity stays the candidate-set integrity
    const integrityText = ig2.recorded ? `${ig2.status || "—"} · persistence ${ig2.eligibility || "—"} · accounting ${acc.status || "—"} · ${bnd.train_boundary_purged_count != null ? bnd.train_boundary_purged_count : "—"} purged at the TRAIN / OOS boundary`
      : "not recorded (a Stage 6.2 run, before the integrity gate)";
    return `<div class="pui-sv" data-pui-sv data-pui-sv-state="${esc(st)}" data-pui-sv-run="${esc(r.run_id)}">${head}` +
      `<div class="pui-sv-verdict" data-pui-sv-verdict="${esc(vd.verdict || st)}"><b>${esc(vd.label || st)}</b> · ${esc(vd.decision || "")}</div>` +
      (vd.reasons || []).map((x) => `<div data-pui-sv-reason="${esc(x.code)}">${esc(x.text)}</div>`).join("") +
      (cs ? `<div class="pui-row" data-pui-sv-split><span>Absolute performance: <b data-pui-sv-absolute="${esc(absV.verdict || "")}">${esc(absV.label || absV.verdict || "—")}</b></span>` +
        `${svEdgeBadge((v.selection_edge || {}).status, (v.selection_edge || {}).label)}</div>` : "") +
      (vd.verdict === "FAIL" || vd.verdict === "INVALID" ? `<div class="pui-sv-fail">This strategy should not progress to paper trading.</div>` : "") +
      (vd.note && !/should not progress/.test(vd.note) ? `<div class="pui-sub">${esc(vd.note)}</div>` : "") +
      ((vd.warnings || []).length ? `<div class="pui-sub" data-pui-sv-warnings>Warnings: ${esc(vd.warnings.map((w) => w.text || w.code || w).join(" · "))}</div>` : "") +
      `<div class="pui-sub">Lifecycle: unchanged by validation. A lifecycle change is an explicit human decision.</div>` +
      ((v.research_flags || []).length ? `<div class="pui-row" data-pui-sv-flags>${v.research_flags.map((f) => `<span class="pui-badge bad" data-pui-sv-flag="${esc(f)}">${esc(f)}</span>`).join("")}</div>` : "") +
      `<div class="pui-kv"><span>Dataset</span><b>${esc(r.dataset_kind === "SYNTHETIC" ? "Synthetic test data" : "Historical")} · ${esc(r.dataset_start)} – ${esc(r.dataset_end)}</b>` +
      `<span>TRAIN</span><b>${esc(r.train_start)} – ${esc(r.train_end)}</b><span>Out of sample</span><b>${r.oos_start ? `${esc(r.oos_start)} – ${esc(r.oos_end)} (${esc(r.oos_sessions)} sessions)` : "none"}</b>` +
      `<span>Strategy version</span><b>${esc(r.strategy_version)}</b><span>Candidates</span><b>${esc(r.candidate_count)}${r.candidates_skipped ? ` (${esc(r.candidates_skipped)} skipped: missing data)` : ""}</b>` +
      `<span>Friction</span><b>${esc(fr.slippage_bps_per_side)} bps slippage per side · ${esc(fr.commission_per_order)} per order · ${esc(fr.fee_per_share)} per share</b>` +
      `<span>Mechanical audit</span><b data-pui-sv-audit="${esc(au.status)}">${esc(au.note || au.status || "—")}</b>` +
      `<span>Integrity</span><b data-pui-sv-integrity="${esc(ig2.recorded ? ig2.status : "NOT_RECORDED")}">${esc(integrityText)}</b>` +
      `<span>Train vs OOS</span><b>${esc(ttl.text || "—")}</b><span>Parameter sensitivity</span><b>${esc(sens.label || "—")}${sens.note ? ` · ${esc(sens.note)}` : ""}</b>` +
      `<span>Run</span><b>${esc(r.run_id)} · ${esc(r.created_at)}</b></div>` +
      (cs ? `<h4 data-pui-sv-section="absolute">Absolute performance</h4><div class="pui-sub">${esc(absV.label || "—")} on the legs (the validation_v2 policy). A trade-level ` +
        `description: the legs of one rebalance are correlated, so neither these figures nor beating the market benchmark is evidence of selection.</div>` + metrics +
        `<h4 data-pui-sv-section="control">Control comparison</h4>` + svControl(cs) + `<h4 data-pui-sv-section="edge">Selection edge</h4>` +
        svEdge(v.selection_edge || {}, cs, v.methodology_evaluation) + `<h4 data-pui-sv-section="universe">Universe integrity</h4>` + svUniverse(v.universe_integrity) + svResearch(dg.strategy)
        : metrics + svPortfolio(dg.strategy)) + `<h4>Arm comparison</h4>` + armTable +
      `<div class="pui-sub" data-pui-sv-causality>Causality: ${esc((ca.automated_checks || []).length)} automated checks, ${esc(crit.length)} critical, ${esc(warn.length)} warnings. ${esc(ca.manual_review_note || "MANUAL_CAUSALITY_REVIEW_REQUIRED")}${ca.manual_review_status ? ` Manual review: ${esc(ca.manual_review_status)}${ca.manual_review_reference ? ` (${esc(ca.manual_review_reference)})` : ""}.` : ""}</div>` +
      `<details class="pui-fold pui-tech"><summary>Technical details</summary><pre class="pui-sub">${esc(JSON.stringify({ execution_assumptions: r.execution_assumptions, causality: ca.automated_checks, diagnostics: dg, skips: r.skips, first_audit_violations: au.first_violations, dataset_id: r.dataset_id, validation_version: r.validation_version, git_commit_sha: r.git_commit_sha }, null, 1))}</pre></details>` +
      `<div class="pui-sub">${esc(v.disclaimer || "")}</div></div>`;
  }

  // one strategy's detail: the thirteen sections as documented / live / "Not yet evaluated" / unavailable; raw facts and sources behind Technical details
  function strategyDetailHtml(det, views) {
    void views;
    const e = det.entry || {}, lc = det.lifecycle || {};
    const label = (st) => (st === "NOT_EVALUATED" ? "Not yet evaluated" : st === "UNAVAILABLE" ? "Unavailable" : st === "LIVE" ? "Live" : "Documented");
    const sec = (s, open) => `<details class="pui-fold pui-sec" data-pui-section="${esc(s.key)}" data-pui-section-state="${esc(s.state)}"${open ? " open" : ""}><summary>${esc(s.title)} <span class="pui-badge ${s.state === "LIVE" ? "ok" : s.state === "DOCUMENTED" ? "" : "dim"}">${esc(label(s.state))}</span></summary><div class="pui-why-body">` +
      (s.items || []).map((i) => `<div>${esc(i)}</div>`).join("") + (s.note ? `<div class="pui-sub">${esc(s.note)}</div>` : "") +
      (Object.keys(s.facts || {}).length ? `<details class="pui-fold pui-tech"><summary>Technical details</summary><pre class="pui-sub">${esc(JSON.stringify(s.facts, null, 1))}</pre></details>` : "") +
      ((s.sources || []).length ? `<div class="pui-sub">Sources: ${esc(s.sources.join(" · "))}</div>` : "") + `</div></details>`;
    return `<div class="pui-lab" data-pui-lab-panel data-pui-lab-detail="${esc(e.strategy_id)}"><button type="button" class="pui-link" data-pui-lab="back">◂ Strategy registry</button>` +
      `<h3>${esc(e.display_name)} <span class="pui-badge ${e.active ? "ok" : "dim"}">${esc(e.status_label || e.status)}</span>${e.implemented ? "" : ` <span class="pui-badge dim" data-pui-placeholder>placeholder · not active</span>`}</h3>` +
      `<div class="pui-kv"><span>Strategy id</span><b>${esc(e.strategy_id)}</b><span>Source</span><b>${esc(e.source_label || e.source)}</b><span>Asset class</span><b>${esc(words(e.asset_class))}</b><span>Direction</span><b>${esc(words(e.direction_capability))}</b>` +
      `<span>Horizon</span><b>${esc(words(e.time_horizon))}</b><span>Version</span><b>${esc(e.version)}</b><span>Created</span><b>${esc(e.created_at)}</b><span>Updated</span><b>${esc(e.updated_at)}</b>` +
      `<span>Lifecycle</span><b>${esc(lc.label || e.status)}${lc.declared && lc.declared !== lc.status ? ` (declared ${esc(words(lc.declared))})` : ""}</b></div>` +
      ((lc.basis || []).length ? `<div class="pui-sub">${esc(lc.basis.join(" · "))}</div>` : "") + (lc.decision ? `<div class="pui-sub">${esc(lc.decision)}</div>` : "") +
      validationHtml(det.validation) + (det.sections || []).map((s, i) => sec(s, i === 0)).join("") +
      `<div class="pui-row">${det.links && det.links.validation ? `<a class="pui-btn" href="${esc(det.links.validation)}" target="_blank" rel="noopener">Paper validation</a>` : ""}<button type="button" class="pui-btn" data-pui-go="trade">Open TRADE</button></div>` +
      `<div class="pui-sub">${esc(det.disclaimer || "")}</div></div>`;
  }

  // ---------------------------------------------------------------- Stage 6.8: account positions + Bot Activity (pure; real backend records only) -------
  const hms = (iso) => (iso ? String(iso).slice(11, 19) : "—");
  const MGMT_CLS = { BOT: "ok", BOT_ENTRY: "info", NEEDS_REVIEW: "bad", EXTERNAL: "warn" };
  // POSITIONS: the Alpaca PAPER account's positions as the read-only account refresh fetched them (GET /api/account-refresh/positions), each marked
  // bot-managed or external; "Not loaded" (no snapshot yet) is never shown as "no position"
  function accountPositionsHtml(v, error) {
    const head = `<h3>${esc((v && v.account_label) || "Alpaca PAPER account")} <span class="pui-sub">all positions · read-only account refresh</span></h3>`;
    if (!v) return `<section class="pui-acct" data-pui-acct="UNAVAILABLE">${head}<div class="pui-bad">Account positions unavailable${error ? ` (${esc(error)})` : ""}.</div></section>`;
    const fr = v.freshness || {};
    const fresh = `<div class="pui-sub" data-pui-acct-fresh="${esc(fr.status || v.state)}">Source: ${esc(v.source_label || "account refresh")}${v.host ? ` (${esc(v.host)})` : ""}` +
      `${fr.label ? ` · ${esc(fr.label)}` : ""}${v.refreshed_at ? ` · read at ${esc(day(v.refreshed_at))} ${esc(hms(v.refreshed_at))} UTC` : ""}</div>`;
    if (v.state !== "LOADED") return `<section class="pui-acct" data-pui-acct="${esc(v.state)}">${head}${fresh}<div class="pui-empty" data-pui-acct-empty="${esc(v.state)}">` +
      `<b>${v.state === "NOT_LOADED" ? "Not loaded yet" : "Not available"}.</b> ${esc(v.message || "")}</div></section>`;
    const stale = fr.status && fr.status !== "FRESH" ? `<div class="pui-line pui-warn" data-pui-acct-stale>${esc(v.message)}</div>` : "";
    if (!(v.positions || []).length) return `<section class="pui-acct" data-pui-acct="LOADED" data-pui-acct-count="0">${head}${fresh}${stale}<div class="pui-empty">No positions in the ${esc(v.account_label || "Alpaca PAPER account")}.</div></section>`;
    const rows = v.positions.map((p) => `<tr data-pui-acct-row="${esc(p.symbol)}" data-management="${esc(p.management)}"><td><b>${esc(p.symbol)}</b></td><td>${esc(p.qty)}</td>` +
      `<td>${esc(p.avg_entry_price || "—")}</td><td>${esc(p.current_price || "—")}</td><td>${esc(p.market_value || "—")}</td><td>${esc(money(p.unrealized_pl))}</td>` +
      `<td><span class="pui-badge ${MGMT_CLS[p.management] || "dim"}" title="${esc(p.management_text)}">${esc(p.management_label)}</span></td></tr>`).join("");
    const c = v.counts || {};
    return `<section class="pui-acct" data-pui-acct="LOADED" data-pui-acct-count="${esc(v.positions.length)}">${head}${fresh}${stale}` +
      `<table class="pui-table pui-acct-table"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg entry</th><th>Current</th><th>Market value</th><th>Unrealized P/L</th><th>Managed by</th></tr></thead><tbody>${rows}</tbody></table>` +
      `<div class="pui-sub">${esc(c.bot || 0)} bot-managed · ${esc(c.external || 0)} external (the bot never trades or exits these; they count toward the open-positions limit and block a bot entry in the same symbol)` +
      `${c.needs_review ? ` · ${esc(c.needs_review)} need a look` : ""}.</div></section>`;
  }


  // ---------------------------------------------------------------- Stage 6.9: Robinhood READ ONLY + unified exposure (pure; GET /api/unified-portfolio) ----------
  // PAPER (Alpaca) and REAL (Robinhood) are never mixed up: every block names its account type, Robinhood rows are USER MANAGED (the bot never
  // trades, exits or resizes them), and a source without data is "unknown", never zero holdings.
  const RH_STATE = { NOT_CONFIGURED: "Not configured", AUTH_REQUIRED: "Login required", NOT_LOADED: "Not loaded yet", LOADED: "Loaded", STALE: "Stale", ERROR: "Read failed" };
  const UNI_STATE = { COMPLETE: ["Complete", "ok"], STALE_SOURCE: ["A source is stale", "warn"], PARTIAL: ["Partial", "warn"], UNAVAILABLE: ["Unavailable", "bad"] };
  const OWN_CLS = { BOT_MANAGED: "ok", EXTERNAL: "warn", USER_MANAGED: "info", NEEDS_REVIEW: "bad" };
  const OWN_TXT = { BOT_MANAGED: "bot", EXTERNAL: "external", USER_MANAGED: "user managed", NEEDS_REVIEW: "needs review" };
  const usd = (x) => { const n = num(x); return n == null ? "—" : `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; };
  function realSource(u) { return u ? (u.sources || []).find((s) => s.account_type !== "PAPER") || null : null; }
  const isReal = (s) => !!s && s.account_type === "REAL";
  function robinhoodHtml(u, error) {
    const s = realSource(u);
    const head = s && s.account_type === "SYNTHETIC" ? `<div class="pui-group-label" data-pui-group="REAL">SYNTHETIC — ROBINHOOD FIXTURE · not a real account · read only</div>`
      : `<div class="pui-group-label" data-pui-group="REAL">REAL — ROBINHOOD · user managed · read only</div>`;
    if (!s) return head + `<section class="pui-acct pui-real" data-pui-rh="UNAVAILABLE"><div class="pui-bad">Robinhood holdings unavailable${error ? ` (${esc(error)})` : ""}: unknown, not zero.</div></section>`;
    const rows = (u.rows || []).filter((r) => r.account_type !== "PAPER");
    const meta = `<div class="pui-sub" data-pui-rh-state="${esc(s.state)}">${esc(s.label)} · ${esc(RH_STATE[s.state] || s.state)}${s.refreshed_at ? ` · read at ${esc(day(s.refreshed_at))} ${esc(hms(s.refreshed_at))} UTC` : ""}</div>`;
    const body = s.state !== "LOADED" && s.state !== "STALE"
      ? `<div class="pui-empty" data-pui-rh-empty="${esc(s.state)}"><b>${esc(RH_STATE[s.state] || s.state)}.</b> ${esc(s.message || "")}</div>`
      : (s.state === "STALE" ? `<div class="pui-line pui-warn" data-pui-rh-stale>${esc(s.message || "Stale")}</div>` : "") +
        (rows.length ? `<table class="pui-table pui-acct-table"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg cost</th><th>Current</th><th>Market value</th><th>Unrealized P/L</th><th>Ownership</th></tr></thead><tbody>` +
          rows.map((r) => `<tr data-pui-rh-row="${esc(r.symbol)}"><td><b>${esc(r.symbol)}</b></td><td>${esc(r.quantity)}</td><td>${esc(r.average_entry_price || "—")}</td><td>${esc(r.current_price || "—")}</td>` +
            `<td>${esc(r.market_value || "—")}</td><td>${esc(money(r.unrealized_pl))}</td><td><span class="pui-badge info" title="${esc(r.notes || "")}">${esc(r.ownership_label)}</span></td></tr>`).join("") + `</tbody></table>`
          : `<div class="pui-empty">No holdings in the Robinhood account.</div>`);
    return head + `<section class="pui-acct pui-real" data-pui-rh="${esc(s.state)}"><h3>${esc(s.label)}</h3>${meta}${body}` +
      `<div class="pui-sub">${isReal(s) ? "Real money, read only" : "Synthetic data, not a real account"}: the bot never trades, exits or resizes these holdings. Robinhood is not an execution broker.</div></section>`;
  }
  function unifiedHtml(u, error) {
    if (!u) return `<section class="pui-acct pui-unified" data-pui-unified="UNAVAILABLE"><h3>Unified exposure</h3><div class="pui-bad">Unavailable${error ? ` (${esc(error)})` : ""}.</div></section>`;
    const st = UNI_STATE[u.state] || [u.state, "dim"], ex = u.exposure || {}, eq = u.equity || {};
    const disc = (u.disclosure || []).length ? `<ul class="pui-sub" data-pui-unified-disclosure>${u.disclosure.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : "";
    const by = (o) => Object.entries(o || {}).map(([k, v]) => `${esc(OWN_TXT[k] || k)} ${esc(usd(v))}`).join(" · ") || "—";
    const sym = (ex.by_symbol || []).map((s) => `<tr data-pui-uni-row="${esc(s.symbol)}"${s.duplicate ? ' data-duplicate="1"' : ""}><td><b>${esc(s.symbol)}</b></td><td>${esc(s.gross_quantity || s.quantity)}</td><td>${esc(usd(s.gross_market_value || s.market_value))}${s.fully_valued ? "" : " (part unvalued)"}</td>` +
      `<td>${s.weight_pct == null ? "—" : `${esc(s.weight_pct)}%`}</td><td>${s.accounts.map((a) => `${esc(a.account_type)} ${esc(a.quantity)} (${esc(OWN_TXT[a.ownership] || a.ownership)})`).join(" + ")}</td></tr>`).join("");
    const warn = (u.warnings || []).length ? `<ul class="pui-bot-list" data-pui-unified-warnings>${u.warnings.map((w) => `<li class="${esc(w.level)}" data-code="${esc(w.code)}">${esc(w.text)}</li>`).join("")}</ul>` : `<div class="pui-sub">No exposure warning.</div>`;
    const real = eq.real_external, rs = realSource(u);
    return `<section class="pui-acct pui-unified" data-pui-unified="${esc(u.state)}"><h3>Unified exposure <span class="pui-badge ${st[1]}">${esc(st[0])}</span></h3>${disc}` +
      `<div class="pui-kv pui-bot-kv"><span>Known invested value (PAPER + ${isReal(rs) || !rs ? "REAL" : "SYNTHETIC"}, gross)</span><b data-pui-uni-total>${esc(usd(ex.total_market_value))}${ex.basis === "KNOWN_SOURCES_ONLY" ? " (known sources only)" : ""}</b>` +
      `<span>PAPER · REAL</span><b>${by(ex.by_account_type)}</b><span>By ownership</span><b>${by(ex.by_ownership)}</b>` +
      `<span>Largest</span><b>${ex.largest ? `${esc(ex.largest.symbol)} ${esc(ex.largest.weight_pct)}%` : "—"}</b><span>Top 5</span><b>${ex.top5_pct == null ? "—" : `${esc(ex.top5_pct)}%`}</b>` +
      `<span>Equity · PAPER</span><b>${esc(usd(eq.paper && eq.paper.equity))}</b><span>Equity · ${isReal(rs) || !rs ? "REAL" : "SYNTHETIC"}</span><b>${real ? esc(usd(real.equity)) : "unknown (Robinhood not loaded)"}</b></div>` +
      `<div class="pui-sub">${esc(eq.note || "")}</div>` +
      (sym ? `<table class="pui-table pui-acct-table"><thead><tr><th>Symbol</th><th>Shares (all accounts, gross)</th><th>Value (gross)</th><th>Weight</th><th>Per account</th></tr></thead><tbody>${sym}</tbody></table>` : `<div class="pui-empty">No known position in any account.</div>`) +
      `<div class="pui-sub">Warnings (informational):</div>${warn}<div class="pui-sub">${esc(u.current_entry_policy || "")}</div></section>`;
  }
  // TRADE › Position card: the bot's position, the Alpaca account and Robinhood stay three different things; unified exposure adds them up
  function exposureLines(u, sym) {
    if (!u || !sym) return null;
    const rows = (u.rows || []).filter((r) => r.symbol === sym);
    const rh = realSource(u), ps = (u.sources || []).find((s) => s.account_type === "PAPER") || null;
    const rhRows = rows.filter((r) => r.account_type !== "PAPER"), paper = rows.filter((r) => r.account_type === "PAPER" && r.ownership !== "BOT_MANAGED");
    if (!rhRows.length && !paper.length) return null;
    const known = (s) => !!s && (s.state === "LOADED" || s.state === "STALE");
    const line = (t, cls, attr) => `<div class="pui-line ${cls || ""}" ${attr || ""}>${t}</div>`;
    const tot = (u.exposure && (u.exposure.by_symbol || []).find((s) => s.symbol === sym)) || null;
    const partial = u.exposure && (u.exposure.missing_sources || []).length ? " (known sources only)" : "";
    const rhName = rh && rh.account_type === "SYNTHETIC" ? "Robinhood · SYNTHETIC fixture" : "Robinhood · REAL";
    return line(`Alpaca PAPER account: ${paper.length ? paper.map((r) => `${esc(r.quantity)} ${esc(sym)} · ${esc(r.ownership_label)}${r.ownership === "EXTERNAL" ? ` · blocks a bot entry in ${esc(sym)}` : ""}`).join("; ")
                                         : known(ps) ? "no non-bot holding" : `unknown (${esc(RH_STATE[(ps || {}).state] || "not loaded")})`}`, "", 'data-pui-x="paper"') +
      line(`${rhName}: ${rhRows.length ? rhRows.map((r) => `${esc(r.quantity)} shares · ${esc(r.ownership_label)}`).join("; ") : rh && !known(rh) ? `unknown (${esc(RH_STATE[rh.state] || rh.state)})` : "none"}`, rhRows.length ? "pui-warn" : "", 'data-pui-x="real"') +
      (tot ? line(`Unified exposure${partial}: ${esc(tot.gross_quantity || tot.quantity)} shares · ${esc(usd(tot.gross_market_value || tot.market_value))}`, "", 'data-pui-x="unified"') : "");
  }
  function exposureCardHtml(u, sym) {
    const lines = exposureLines(u, sym);
    return lines ? `<div class="pui-card position" data-pui-card="position" data-open="0" data-pui-exposure="${esc(sym)}"><h4>Position</h4><div class="pui-big">No bot position</div>${lines}</div>` : null;
  }
  // TRADE › Position card when the bot holds nothing in the symbol: the account's own holding (external, unmanaged) or "not loaded" - null keeps the plain card
  function accountCardHtml(v, sym) {
    if (!v || !sym) return null;
    const card = (attr, line, sub) => `<div class="pui-card position" data-pui-card="position" data-open="0" ${attr}><h4>Position</h4><div class="pui-big">No bot position</div>${line}<div class="pui-sub">${esc(sub)}</div></div>`;
    if (v.state === "NOT_LOADED") return card('data-pui-account="NOT_LOADED"', "", "Account positions not loaded yet: whether the account holds this symbol is unknown.");
    const row = v.state === "LOADED" ? (v.positions || []).find((r) => r.symbol === sym) : null;
    if (!row || row.managed) return null;
    return card(`data-pui-account="LOADED" data-pui-external="${esc(row.symbol)}"`, `<div class="pui-line pui-warn">Account holds ${esc(row.qty)} ${esc(row.symbol)} · ${esc(row.management_label)}</div>`,
                `${(v.freshness && v.freshness.label) || ""} The bot never trades or exits it; it blocks a bot entry in ${row.symbol}.`.trim());
  }

  // TRADE › Bot Activity: is the WORKER working (its own heartbeat), not just the page connected (the last successful read of this page)
  const STEP_ORDER = ["ORDERS", "RECONCILE", "POSITIONS", "SAFETY", "ENTRIES"];
  const WORKER_CLS = { RUNNING: "ok", IDLE: "ok", STANDBY: "info", STARTING: "info", STALE: "bad", ERROR: "bad", STOPPED: "bad", DISABLED: "dim" };
  const TICKER_CLS = { ENTRY_INTENT: "ok", BLOCKED: "warn", SKIPPED: "info", WAITING: "dim", READY_MONITOR_ONLY: "info", READY_PENDING: "info" };
  const EVALUATED = ["ENTRY_INTENT", "BLOCKED", "SKIPPED"];
  const MODE_TXT = { MONITOR_ONLY: "Monitor only", PAPER_AUTO: "Paper Auto" };
  function botActivityModel(a, conn) {
    const c = conn || {};
    const dashboard = c.okAt ? { state: c.error ? "STALE" : "CONNECTED", cls: c.error ? "warn" : "ok", text: c.error ? `Last read failed (${c.error}); showing the read from ${hms(c.okAtIso)}.` : `Connected · read at ${hms(c.okAtIso)}` }
      : { state: c.error ? "DISCONNECTED" : "CONNECTING", cls: c.error ? "bad" : "dim", text: c.error ? `Server not reachable (${c.error}).` : "Connecting…" };
    if (!a) return { dashboard, loaded: false };
    const w = a.worker || {};
    const p = a.progress;
    const progress = p && p.step ? `Step ${STEP_ORDER.indexOf(p.step) + 1} of ${STEP_ORDER.length}: ${p.step_label}${p.symbol ? ` · ${p.symbol} (${p.index} of ${p.total})` : ""} · cycle started ${hms(p.started_at)}`
      : w.in_cycle ? "A cycle is in progress (step not reported yet)." : "No cycle in progress.";
    const lc = a.last_cycle;
    const last = lc ? `${hms(lc.at)}${lc.finished_at ? ` → ${hms(lc.finished_at)}` : ""}${lc.duration_ms != null ? ` (${lc.duration_ms} ms)` : ""} · ${String(lc.trigger || "").toLowerCase()} · ${lc.text}` +
      (lc.source === "DATABASE" ? " (recorded before this server started)" : "") : "No cycle recorded yet.";
    // the CURRENT mode vs the mode the last cycle ran in: Paper Auto switched on after the last cycle is awaiting its first cycle (never shown as run)
    const md = a.mode || {}, lcMode = md.last_cycle_mode || (lc && lc.mode) || null, since = md.changed_at || null;
    const awaiting = md.awaiting_first_cycle != null ? !!md.awaiting_first_cycle : md.mode === "PAPER_AUTO" && lcMode !== "PAPER_AUTO" && !!since && (!lc || String(since) > String(lc.at || ""));
    const ran = lc && lcMode ? ` The last cycle (${hms(lc.at)}) ran in ${MODE_TXT[lcMode] || lcMode}.` : "";
    const modeNote = md.note || (awaiting ? `Paper Auto since ${hms(since)}: awaiting its first cycle (${w.running && w.next_at ? `next scheduled run ${hms(w.next_at)}` : "the worker is not running, so no cycle is scheduled"}).${ran}`
      : lcMode && lcMode !== md.mode ? `${md.label || MODE_TXT[md.mode] || "—"} since ${hms(since)}.${ran}` : "");
    // every ticker row names its origin: the LATEST cycle's evaluation, an EARLIER cycle's (not re-evaluated since), or the SETUP state - with its time
    const latestAt = lc ? lc.at : null;
    const tickers = (a.tickers || []).map((t) => {
      const scope = t.scope || (EVALUATED.includes(t.status) ? (t.at && t.at === latestAt ? "LATEST_CYCLE" : "EARLIER_CYCLE") : "SETUP");
      const evAt = t.evaluated_at || t.at, chAt = t.checked_at || t.at;
      const when = scope === "LATEST_CYCLE" ? `evaluated ${hms(evAt)} · latest cycle` : scope === "EARLIER_CYCLE" ? `evaluated ${hms(evAt)} · earlier cycle, not re-evaluated since`
        : `setup checked ${hms(chAt)}`;
      return { symbol: t.symbol, status: t.status, label: t.label, cls: TICKER_CLS[t.status] || "dim", text: t.text, at: t.at, scope, when,
               reasons: (t.reasons || []).map((r) => `${r.label || r.control}: ${r.text}`) };
    });
    const work = a.latest_cycle_evaluations || tickers.filter((t) => t.scope === "LATEST_CYCLE").map((t) => ({ symbol: t.symbol, result: t.status }));
    const latestWork = !lc ? "No cycle recorded yet." : work.length ? `Latest cycle (${hms(lc.at)}) evaluated: ${work.map((x) => `${x.symbol} ${String(x.result).replace(/_/g, " ").toLowerCase()}`).join(", ")}.`
      : `Latest cycle (${hms(lc.at)}) evaluated no ticker for entry${lcMode && lcMode !== "PAPER_AUTO" ? " (Monitor only)" : ""}.`;
    const r = a.risk || {}, g = r.gate || {};
    const orders = (a.orders || []).map((o) => ({ symbol: o.symbol, title: `${o.symbol} · ${o.type.toLowerCase()} ${o.side} ${o.qty} · ${o.state.replace(/_/g, " ").toLowerCase()}`,
      steps: (o.steps || []).map((s) => ({ at: s.at, kind: s.kind, label: s.label })) }));
    const pos = a.positions || {}, acct = pos.account || {};
    const external = acct.state === "LOADED" ? (acct.positions || []).filter((x) => !x.managed) : [];
    return { dashboard, loaded: true, mode: md.label || MODE_TXT[md.mode] || "—", modeKey: md.mode || null, modeSince: since ? `${day(since)} ${hms(since)}` : "—",
      lastCycleMode: lcMode ? (MODE_TXT[lcMode] || lcMode) : "—", awaiting, modeNote, latestWork, generatedAt: a.generated_at,
      worker: { state: w.state, label: w.label, cls: WORKER_CLS[w.state] || "dim", text: w.text,
        heartbeat: w.heartbeat_at ? `${hms(w.heartbeat_at)} (${w.heartbeat_age_s != null ? `${Math.round(w.heartbeat_age_s)} s ago` : "—"}${w.expected_every_s ? `, expected every ~${Math.round(w.expected_every_s)} s` : ""})` : "none yet",
        lastStart: hms(w.last_started_at), lastFinish: hms(w.last_finished_at), next: hms(w.next_at), runs: w.runs == null ? "—" : String(w.runs) },
      progress, last, tickers,
      gate: { label: g.label || "—", cls: g.allowed_new_entry ? "ok" : "warn", text: g.reason_text || (g.allowed_new_entry ? "New entries allowed." : "New entries paused."),
        checks: (g.checks || []).map((x) => ({ label: x.control, result: x.result, text: x.text })) },
      controls: (r.controls || []).map((x) => ({ label: x.label, result: x.result, text: x.text, at: x.at })),
      orders, bot: (pos.bot || []).map((b) => ({ symbol: b.symbol, state: b.state, text: b.decision_text, at: b.decision_at })),
      external: external.map((x) => ({ symbol: x.symbol, qty: x.qty, label: x.management_label })), accountState: acct.state || "UNAVAILABLE", accountMessage: acct.message || "",
      history: (a.history || []).map((h) => ({ at: h.at, kind: h.kind, level: h.level, text: h.text, count: h.count || 1, firstAt: h.first_at || null })) };
  }
  function botActivityHtml(m, open) {
    const o = open || {};
    const fold = (id, title, body) => `<details class="pui-fold" data-pui-bot-fold="${id}"${o[id] ? " open" : ""}><summary data-pui-bot-summary="${id}">${title}</summary><div class="pui-bot-body">${body}</div></details>`;
    const chip = (k, cls, label, text, attr) => `<div class="pui-bot-chip ${cls}" ${attr}><span class="pui-bot-k">${esc(k)}</span><b>${esc(label)}</b><span class="pui-sub">${esc(text)}</span></div>`;
    const dash = chip("Dashboard", m.dashboard.cls, m.dashboard.state === "CONNECTED" ? "Connected" : m.dashboard.state === "STALE" ? "Read failed" : m.dashboard.state === "DISCONNECTED" ? "Disconnected" : "Connecting", m.dashboard.text, `data-pui-dash="${esc(m.dashboard.state)}"`);
    if (!m.loaded) return `<section class="pui-bot" data-pui-bot="NOT_LOADED"><h3>Bot Activity</h3><div class="pui-bot-chips">${dash}${chip("Bot worker", "dim", "Unknown", "The bot's activity has not been read yet.", 'data-pui-worker="UNKNOWN"')}</div></section>`;
    const w = m.worker;
    const rowsOf = (items, fn, empty) => (items.length ? `<ul class="pui-bot-list">${items.map(fn).join("")}</ul>` : `<div class="pui-sub">${esc(empty)}</div>`);
    const tick = `<div class="pui-line" data-pui-latest-work>${esc(m.latestWork)}</div>` +
      rowsOf(m.tickers, (t) => `<li data-pui-ticker="${esc(t.symbol)}" data-status="${esc(t.status)}" data-scope="${esc(t.scope)}"><b>${esc(t.symbol)}</b> <span class="pui-badge ${t.cls}">${esc(t.label)}</span> ${esc(t.text)}` +
      ` <span class="pui-sub" data-pui-when>· ${esc(t.when)}</span>${t.reasons.length ? `<ul>${t.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</li>`, "No ticker has an open setup or an entry evaluation yet.");
    const risk = `<div class="pui-line"><span class="pui-badge ${m.gate.cls}">${esc(m.gate.label)}</span> ${esc(m.gate.text)}</div>` +
      rowsOf(m.gate.checks, (x) => `<li>${esc(x.label)} · <b>${esc(x.result)}</b> · ${esc(x.text)}</li>`, "") +
      `<div class="pui-sub">Executor checks (last result of each):</div>` + rowsOf(m.controls, (x) => `<li data-pui-control="${esc(x.label)}">${esc(x.label)} · <b>${esc(x.result)}</b> · ${esc(x.text || "")} <span class="pui-sub">· ${esc(hms(x.at))}</span></li>`, "No executor check has run yet.");
    const orders = rowsOf(m.orders, (x) => `<li data-pui-order="${esc(x.symbol)}"><b>${esc(x.title)}</b><ol class="pui-bot-steps">${x.steps.map((s) => `<li data-kind="${esc(s.kind)}">${esc(hms(s.at))} · ${esc(s.label)}${s.kind === "BROKER" ? " <span class=\"pui-sub\">(broker)</span>" : ""}</li>`).join("")}</ol></li>`,
      "No bot order yet. Orders appear here only with the broker's own reported statuses.");
    const pos = rowsOf(m.bot, (b) => `<li data-pui-bot-pos="${esc(b.symbol)}"><b>${esc(b.symbol)}</b> · ${esc(String(b.state).replace(/_/g, " ").toLowerCase())} · ${esc(b.text)}</li>`, "No bot-managed position.") +
      (m.accountState === "LOADED" ? rowsOf(m.external, (x) => `<li data-pui-external="${esc(x.symbol)}"><b>${esc(x.symbol)}</b> · ${esc(x.qty)} · <span class="pui-badge warn">${esc(x.label)}</span></li>`, "No external position in the account.")
        : `<div class="pui-sub" data-pui-external-state="${esc(m.accountState)}">Account positions: ${esc(m.accountState === "NOT_LOADED" ? "not loaded yet" : "not available")}. ${esc(m.accountMessage)}</div>`);
    const hist = rowsOf(m.history, (h) => `<li data-kind="${esc(h.kind)}" class="${esc(h.level)}">${esc(day(h.at))} ${esc(hms(h.at))} · ${esc(h.text)}${h.count > 1 ? ` <span class="pui-sub">×${esc(h.count)} since ${esc(hms(h.firstAt))}</span>` : ""}</li>`, "No activity recorded yet.");
    return `<section class="pui-bot" data-pui-bot="${esc(w.state)}"><h3>Bot Activity</h3>` +
      `<div class="pui-bot-chips">${dash}${chip("Bot worker", w.cls, w.label, w.text, `data-pui-worker="${esc(w.state)}"`)}</div>` +
      `<div class="pui-kv pui-bot-kv"><span>Current mode</span><b data-pui-mode="${esc(m.modeKey || "")}">${esc(m.mode)} · since ${esc(m.modeSince)}</b>` +
      `<span>Last cycle ran in</span><b data-pui-last-cycle-mode>${esc(m.lastCycleMode)}</b>` +
      `<span>Heartbeat</span><b data-pui-heartbeat>${esc(w.heartbeat)}</b><span>Last cycle</span><b>${esc(w.lastStart)} → ${esc(w.lastFinish)}</b>` +
      `<span>Next run</span><b>${esc(w.next)}</b><span>Cycles this run</span><b>${esc(w.runs)}</b></div>` +
      (m.modeNote ? `<div class="pui-line ${m.awaiting ? "pui-warn" : ""}" data-pui-mode-note data-awaiting="${m.awaiting ? "1" : "0"}">${esc(m.modeNote)}</div>` : "") +
      `<div class="pui-line" data-pui-progress>Now: ${esc(m.progress)}</div><div class="pui-line" data-pui-last-cycle>Last cycle: ${esc(m.last)}</div>` +
      fold("tickers", `Tickers (${m.tickers.length})`, tick) + fold("risk", `Risk checks · ${esc(m.gate.label)}`, risk) + fold("orders", `Orders (${m.orders.length})`, orders) +
      fold("positions", `Positions (${m.bot.length} bot · ${m.external.length} external, unmanaged)`, pos) + fold("history", `Activity history (${m.history.length})`, hist) +
      `<div class="pui-sub">Times are UTC. Real backend records only: nothing is estimated, no fill is inferred from a submission, no AI narration.</div></section>`;
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
  function setCollapsed(v) { try { localStorage.setItem("pui.details", v ? "closed" : "open"); } catch (e) { /* per-browser convenience only */ } document.body.classList.toggle("pui-collapsed", v); const b = $("pui-details-btn"); if (b) { b.textContent = v ? "View details ▸" : "Hide details ▾"; b.setAttribute("aria-expanded", v ? "false" : "true"); } placeBotActivity(); }

  function renderStatus() { const el = $("pui-status"); if (el) el.innerHTML = statusHtml(exec(), safety(), { sysOpen: state.sysOpen, autoPending: state.autoPending, autoError: state.autoError }); }
  function renderCards() {
    const host = $("pui-cards");
    if (!host) return;
    const sym = activeSymbol();
    const card = cardFor(sym);
    let position = null;
    if (card && card.position_id && state.posData) position = (state.posData.positions || []).find((p) => p.position_id === card.position_id) || null;
    loadRegistry(); loadFrozen();
    if (state.whyOpen) loadTerminal(sym);                                        // the proposal's own blocker and the stored setup detail: on demand (Why? open), 10 s cache
    host.innerHTML = cardsHtml({ exec: exec(), safety: safety(), card, position, dom: readDom(), symbol: sym, limits: state.limits, avail: avail(), confirm: state.confirm,
                                 whyOpen: state.whyOpen, riskOpen: state.riskOpen, posOpen: state.posOpen, autoPending: state.autoPending, autoError: state.autoError,
                                 registry: state.registry, terminal: (state.terminal[sym] || {}).data || null, frozen: state.frozen, techOpen: state.techOpen,
                                 account: (state.botAct && state.botAct.positions && state.botAct.positions.account) || state.acctPos || null,   // Stage 6.8
                                 unified: state.unified });                                                                                    // Stage 6.9
  }
  // Stage 6.1.2 read-only loaders: the registry and the frozen controls once per page, the terminal state of the active ticker only while Why? is open
  // a failed read is read again at most once a minute (never a 2 s poll)
  const REREAD_MS = 60000;
  function loadRegistry() {
    if (state.registry || state.registryBusy || (state.registryFailedAt && Date.now() - state.registryFailedAt < REREAD_MS)) return;
    state.registryBusy = true;
    getJson("/api/strategy-registry").then((r) => { state.registry = r; state.registryFailedAt = null; }).catch(() => { state.registry = null; state.registryFailedAt = Date.now(); }).finally(() => { state.registryBusy = false; });
  }
  function loadFrozen() {
    if (state.frozen || state.frozenBusy || (state.frozenFailedAt && Date.now() - state.frozenFailedAt < REREAD_MS)) return;
    state.frozenBusy = true;
    getJson("/api/forward-validation/frozen-controls").then((r) => { state.frozen = (r && r.controls) || null; state.frozenFailedAt = state.frozen ? null : Date.now(); }).catch(() => { state.frozen = null; state.frozenFailedAt = Date.now(); }).finally(() => { state.frozenBusy = false; });
  }
  function loadTerminal(sym) {
    const t = state.terminal[sym];
    if (!sym || state.terminalBusy || (t && Date.now() - t.at < 10000)) return;
    state.terminalBusy = true;
    getJson(`/api/terminal/state/${encodeURIComponent(sym)}?candles=0`).then((d) => { state.terminal[sym] = { at: Date.now(), data: d }; renderCards(); }).catch(() => { state.terminal[sym] = { at: Date.now(), data: null }; }).finally(() => { state.terminalBusy = false; });
  }
  function strategyName() { const s = ((state.registry && state.registry.strategies) || []).find((x) => x.implemented && x.active); return s ? s.display_name : null; }
  function viewProposal() { setCollapsed(false); const el = q("#tv-proposal"); if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" }); }   // the Stage 5.9 review panel; nothing is submitted
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
  function legacyBotError() { const e = q("#tv-bot [data-bot-error]"); return (e && e.textContent.trim()) || (window.PaperBot && window.PaperBot.state && window.PaperBot.state.error) || ""; }
  function observeBot() {
    // the legacy panel renders its failure text only until its own refresh succeeds (tens of ms): a MutationObserver catches it the moment it appears
    const bot = $("tv-bot");
    if (!bot || state.botObserver) return;
    state.botObserver = new MutationObserver(() => { const err = legacyBotError(); if (err && state.autoPending) state.autoPending.error = err; });
    state.botObserver.observe(bot, { childList: true, subtree: true });
  }
  function runAutoAction(kind, clicks) {
    // kind: "enable" | "disable". The legacy button does the POST; the shell only waits for it and then re-reads the authoritative status (never assumes success)
    observeBot();
    const pb = window.PaperBot;
    if (!pb || !clickLegacy(kind === "enable" ? "auto-on" : "auto-off", clicks)) return;
    state.autoError = null;
    state.autoPending = { kind, since: Date.now(), error: "" };
    clearInterval(state.autoWatch);
    renderStatus(); renderCards();
    state.autoWatch = setInterval(async () => {
      const p = state.autoPending;
      if (!p) { clearInterval(state.autoWatch); return; }
      const err = legacyBotError();
      if (err) p.error = err;
      if (pb.state.busy && Date.now() - p.since < AUTO_TIMEOUT_MS) return;             // the legacy POST is still in flight
      clearInterval(state.autoWatch); state.autoWatch = null;
      let data = null;
      try { data = await pb.refresh(); } catch (e) { data = null; }                    // the one authoritative read: GET /api/execution/status
      if (state.autoPending !== p) return;
      state.autoPending = null;
      const mode = data && data.mode;
      if (!mode) state.autoError = pb.state.error || `Paper bot status unavailable (no status after ${kind})`;
      else if (kind === "enable" && mode !== "PAPER_AUTO") state.autoError = p.error || "Enable Paper Auto failed (the mode did not change)";
      else if (kind === "disable" && mode !== "MONITOR_ONLY") state.autoError = p.error || "Stop New Trades failed (the mode did not change)";
      else state.autoError = null;
      renderStatus(); renderCards();
    }, 150);
  }
  function refreshSoon() { setTimeout(() => { renderStatus(); renderCards(); }, 50); setTimeout(() => { renderStatus(); renderCards(); }, 400); setTimeout(() => { renderStatus(); renderCards(); }, 1500); }
  function onAction(e) {
    const b = e.target.closest("[data-pui]");
    if (!b || b.disabled) return;
    const k = b.dataset.pui;
    if (k.startsWith("fold:")) { e.preventDefault(); const key = k.slice(5); if (key === "why") state.whyOpen = !state.whyOpen; if (key === "risk") state.riskOpen = !state.riskOpen; if (key === "pos") state.posOpen = !state.posOpen; if (key === "tech") state.techOpen = !state.techOpen; renderCards(); return; }
    if (k.startsWith("go:")) { go(k.slice(3)); return; }                                                     // the focus strip: open POSITIONS / TRADE
    if (k === "proposal") { viewProposal(); return; }                                                        // READY: the review proposal (Trade Review panel), never an order
    if (k === "diag") { e.preventDefault(); state.diagOpen = !state.diagOpen; renderDiag(); return; }
    if (k === "system") { state.sysOpen = !state.sysOpen; renderStatus(); return; }
    // explanations first: Enable Paper Auto, Emergency Stop and Clear Emergency Stop open a confirmation panel; nothing is clicked or posted yet
    if (k === "auto-on" || k === "stop" || k === "clear") { if (k === "auto-on") state.autoError = null; openConfirm(k); return; }
    if (k === "cancel") { closeConfirm(); renderCards(); return; }
    // the confirmed actions delegate to the existing Risk Controls / Paper Bot buttons: identical POSTs, identical persisted semantics
    if (k === "auto-on-confirm") { closeConfirm(); const armed = /^confirm/i.test(((q(LEGACY["auto-on"]) || {}).textContent || "").trim()); runAutoAction("enable", armed ? 1 : 2); return; }
    if (k === "stop-confirm") { closeConfirm(); clickLegacy("stop", 1); refreshSoon(); return; }
    if (k === "clear-confirm") { closeConfirm(); const armed = /^confirm/i.test(((q(LEGACY.clear) || {}).textContent || "").trim()); clickLegacy("clear", armed ? 1 : 2); refreshSoon(); return; }
    if (k === "auto-off") { closeConfirm(); runAutoAction("disable", 1); return; }
    if (k === "pause" || k === "resume") { closeConfirm(); clickLegacy(k, 1); refreshSoon(); }
  }

  async function getJson(path) { const r = await fetch(path); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`); return j; }
  async function loadPositions() {
    try { state.posData = await getJson("/api/execution/positions?limit=100"); if (window.PaperBot) await window.PaperBot.refresh(); state.error = null; }   // open and closed: the lifecycle view
    catch (e) { state.error = `Positions unavailable (${e.message}).`; }
    try { state.acctPos = await getJson("/api/account-refresh/positions"); state.acctPosError = null; }   // Stage 6.8: the account's positions (0 broker calls)
    catch (e) { state.acctPos = null; state.acctPosError = e.message; }
    loadRegistry(); loadFrozen();
    renderPositions();
    loadUnified(true).then(() => renderPositions());                                                    // Stage 6.9: Robinhood READ ONLY + unified exposure, never delaying PAPER
  }
  function renderPositions() {
    const host = $("pui-positions");
    if (!host) return;
    const positions = (state.posData && state.posData.positions) || [];
    const rows = positionCards(exec(), positions, state.posData && state.posData.events, strategyName());
    const closed = positions.filter((p) => p.state === "CLOSED").length;
    host.innerHTML = `<h2>Positions</h2>` + (state.error ? `<div class="pui-bad">${esc(state.error)}</div>` : "") + positionsHtml(rows) + `<div class="pui-group-label" data-pui-group="PAPER">PAPER — ALPACA</div>` + accountPositionsHtml(state.acctPos, state.acctPosError) +
      robinhoodHtml(state.unified, state.unifiedError) + unifiedHtml(state.unified, state.unifiedError) + `<div id="pui-pos-detail"></div>` +
      `<details class="pui-fold" data-pui-closed-fold><summary>Closed paper trades (${closed})</summary>${closedHtml(positions, strategyName(), state.limits)}</details>`;
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
    host.innerHTML = activityHtml(state.actData.src, exec(), state.actData.fv, today, { safety: safety(), filter: state.filter });
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
    host.innerHTML = moreHtml(moreGroups(labViews()), state.moreOpen);
    if (keep) $("pui-more-body").innerHTML = keep;
  }
  function onMoreClick(e) {
    const lab = e.target.closest("[data-pui-lab]");
    if (lab) { renderLab(lab.dataset.puiLab === "back" ? null : lab.dataset.puiLab); return; }
    const g = e.target.closest("[data-pui-go]");
    if (g) { go(g.dataset.puiGo); return; }
    const b = e.target.closest("[data-pui-more]");
    if (!b || b.tagName === "A") return;
    const [kind, name, lab2] = b.dataset.puiMore.split(":");
    if (kind === "tab") { openLegacy(name); if (lab2) setTimeout(() => { const v = q(`#sl-subnav [data-sfv="${lab2}"]`); if (v) v.click(); }, 300); return; }
    if (kind === "action") openMoreAction(name);
  }
  // a MORE target from a legacy deep link: open the group (and the Strategy Lab / Diagnostics panel) the old tab maps to
  function openMore(target) {
    const groupOf = { lab: "research", research: "research", system: "system", diagnostics: "system", settings: "settings" };
    if (groupOf[target]) state.moreOpen[groupOf[target]] = true;
    if (target === "diagnostics") state.moreOpen.diagnostics = true;
    renderMore();
    if (target === "lab") renderLab(null);
  }
  const kv = (rows) => `<div class="pui-kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><b>${esc(v == null || v === "" ? "—" : v)}</b>`).join("")}</div>`;
  function openMoreAction(name) {
    const body = $("pui-more-body");
    if (!body) return;
    state.moreAction = name;
    const fail = (what, err) => { body.innerHTML = `<div class="pui-bad">${esc(what)} unavailable (${esc(err.message)}).</div>`; };
    if (name === "settings") { const p = q("#tab-settings .unavailable-panel"); if (p) p.classList.toggle("pui-shown"); return; }
    if (name === "lab") { renderLab(null); return; }
    if (name === "health") {                                                                        // Broker Connection: the read-only account refresh + the executor's environment proof
      getJson("/api/account-refresh").then((a) => { const x = exec() || {}; const env = x.environment || {};
        body.innerHTML = `<h3>Broker Connection</h3><p class="pui-sub">Alpaca PAPER account, read only here. Robinhood holdings are read only (MORE › System › Diagnostics › Portfolio).</p>` +
          kv([["Account refresh", a.label || a.status], ["Last success", a.last_success_at], ["Consecutive failures", a.consecutive_failures], ["Refresh loop", a.loop && a.loop.running ? "running" : "off"],
              ["Paper environment", env.label || (env.paper ? "PAPER" : "—")], ["Simulated broker", env.simulated ? "yes" : env.paper ? "no" : "—"], ["Account", env.account_number_masked], ["Execution mode", x.mode],
              ["Reconciliation", x.reconciliation_healthy == null ? "—" : x.reconciliation_healthy ? "healthy" : "needs a look"], ["Last executor cycle", x.last_cycle_at]]) + `<div data-pui-health></div>`; }).catch((err) => fail("Broker connection", err));
      return;
    }
    if (name === "data") {                                                                          // Data Sources: the Trader View feed and the diagnostics chips, read from the page
      const d = diagModel(readChips());
      body.innerHTML = `<h3>Data Sources</h3>` + kv([["Market data", (q("#tv-mode") || {}).textContent || "—"], ["Stream", (q("#tv-conn") || {}).textContent || "—"], ["Summary", d.summary || "—"]]) +
        (d.items.length ? `<ul class="pui-sub">${d.items.map((i) => `<li class="${esc(i.level)}">${esc(i.text)}</li>`).join("")}</ul>` : `<div class="pui-sub">Open TRADE once to read the data-source chips.</div>`);
      return;
    }
    if (name === "logs") {                                                                          // Logs: the persisted executor cycle records (10 newest) and the scheduler's last error
      getJson("/api/execution").then((o) => { const runs = o.runs || []; const s = o.scheduler || {};
        body.innerHTML = `<h3>Logs</h3><p class="pui-sub">The persisted executor cycle records (newest ${runs.length}); file logs are written by the Windows runner under logs/ (README › Running without VS Code).</p>` +
          kv([["Scheduler", s.running ? "running" : "off"], ["Last error", s.last_error], ["Last result", s.last_result && s.last_result.result]]) +
          (runs.length ? `<ul class="pui-sub pui-runs" data-pui-runs>${runs.map((r) => `<li>${esc(hm(r.started_at))} · ${esc(r.trigger || "")} · ${esc(r.mode || "")} · ${esc(r.result || "")}${r.error ? ` · ${esc(r.error)}` : ""}</li>`).join("")}</ul>` : `<div class="pui-sub" data-pui-runs>No executor cycle recorded yet.</div>`); }).catch((err) => fail("Logs", err));
      return;
    }
    if (name === "risk") {                                                                          // Risk: the frozen risk constants (read only) and the gate's current state
      getJson("/api/forward-validation/frozen-controls").then((f) => { const c = f.controls || {}; const sv = safety(); const lim = state.limits || {};
        body.innerHTML = `<h3>Risk</h3><p class="pui-sub">Frozen Stage 6.1 controls (hash ${esc(String(f.hash || "").slice(0, 12))}…). Configured server-side (config.py / .env); the controls live on TRADE › Risk card.</p>` +
          kv([["Risk per entry", c.risk_fraction != null ? `${c.risk_fraction} of equity` : null], ["Max position", c.max_position_pct != null ? `${c.max_position_pct} % of equity` : null], ["Max daily loss", c.max_daily_loss_pct != null ? `${c.max_daily_loss_pct} %` : null],
              ["Max open positions", c.max_open_positions != null ? c.max_open_positions : lim.max_open_positions], ["Max hold", c.max_hold_sessions != null ? `${c.max_hold_sessions} sessions` : null], ["Near-close cutoff", c.near_close_minutes != null ? `${c.near_close_minutes} min` : null],
              ["Order", c.order_type && c.time_in_force ? `${c.order_type} / ${c.time_in_force} · ${c.side}` : null], ["Leverage / shorts", c.leverage ? `${c.leverage} / ${c.shorts}` : null],
              ["Gate now", sv ? `${SAFETY_TEXT[sv.state] || sv.state}${sv.reason_text ? ` · ${sv.reason_text}` : ""}` : "unavailable"]]) + `<div data-pui-health></div>`; }).catch((err) => fail("Risk settings", err));
      return;
    }
    if (name === "automation") {                                                                    // Automation: Paper Auto and the background loops, read only; the controls stay on TRADE
      getJson("/api/account-refresh").then((a) => { const x = exec() || {}; const s = x.scheduler || {}; const sv = safety();
        body.innerHTML = `<h3>Automation</h3><p class="pui-sub">Paper Auto, Pause New Trades and Emergency Stop are on TRADE › Risk card. Paper Auto ON is not trading allowed: the gate decides separately.</p>` +
          kv([["Paper Auto", x.mode === "PAPER_AUTO" ? "ON" : x.mode === "MONITOR_ONLY" ? "OFF" : "—"], ["Trading", sv ? (sv.state === "TRADING_ALLOWED" ? "ALLOWED" : sv.state === "NO_NEW_ENTRIES" ? "PAUSED" : sv.state === "HALTED" ? "STOPPED" : "UNKNOWN") : "—"],
              ["Executor loop", s.enabled === false ? "disabled" : s.running ? `running · ${s.interval_s || "—"} s` : "off"], ["Account refresh loop", a.loop && a.loop.running ? "running" : "off"], ["Last cycle", x.last_cycle_at], ["Mode changed", x.mode_changed_at ? `${hm(x.mode_changed_at)} by ${x.mode_changed_by || "—"}` : null]]) + `<div data-pui-health></div>`; }).catch((err) => fail("Automation", err));
      return;
    }
    if (name === "notifications") {                                                                 // Notifications: in-app setup alerts (ACTIVITY) and the Daily Brief delivery workspace
      const brief = labViews().find((v) => v.view === "brief");
      body.innerHTML = `<h3>Notifications</h3>` + kv([["Setup alerts", "in-app toasts + the ACTIVITY timeline (persisted)"], ["Daily Brief delivery", brief ? "Strategy Lab › Daily Brief" : "open MORE › Research › Backtests once to load the workspace"]]) +
        (brief ? `<div class="pui-more-group pui-tools"><button type="button" data-pui-more="tab:strategy:brief">Open Daily Brief delivery</button></div>` : "");
    }
  }
  // the Strategy Lab panel: GET /api/strategy-registry (list) and GET /api/strategy-registry/{id} (detail); every number is the backend's, nothing is posted
  async function renderLab(detailId) {
    const body = $("pui-more-body");
    if (!body) return;
    state.moreAction = "lab";
    body.innerHTML = `<div class="pui-lab" data-pui-lab-panel><h3>Strategy Lab</h3><div class="pui-sub">Loading the strategy registry…</div></div>`;
    try { if (!state.registry) state.registry = await getJson("/api/strategy-registry"); state.labError = null; } catch (e) { state.labError = `Strategy registry unavailable (${e.message}).`; }
    if (detailId && state.registry) { try { state.labDetail = await getJson(`/api/strategy-registry/${encodeURIComponent(detailId)}`); } catch (e) { state.labDetail = null; state.labError = `Strategy detail unavailable (${e.message}).`; } }
    else state.labDetail = null;
    if (state.moreAction !== "lab") return;
    body.innerHTML = state.labDetail ? strategyDetailHtml(state.labDetail, labViews()) : registryHtml(state.registry, labViews(), state.labError);
  }

  function openLegacy(tab) { const b = legacyTab(tab); if (b) b.click(); }
  function go(section) {
    const nav = NAV.find((n) => n.id === section) || NAV[0];
    state.section = nav.id;
    openLegacy(nav.host);
    syncNav();
    // the bookmarkable hash follows the section the user opens (a foreign hash such as #daily-brief is left alone at load time: mount() never calls go() for it)
    try { if (G.history && G.history.replaceState) G.history.replaceState(null, "", `#${nav.id}`); } catch (e) { /* display only */ }
    if (nav.id === "positions") loadPositions();
    if (nav.id === "activity") loadActivity();
    if (nav.id === "more") renderMore();
    if (nav.id === "trade" && ensureTradeCards()) { renderCards(); renderChart(); renderDiag(); }
  }
  function openRoute(route) {
    if (!route) return false;
    if (route.filter) state.filter = route.filter;
    go(route.section);
    if (route.more) openMore(route.more);
    return true;
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
    observeBot();
    renderStatus();
    if (activeLegacyTab() === "trader") { if (ensureTradeCards()) { renderCards(); renderChart(); renderDiag(); } if (ensureBotActivity()) loadBotActivity(); loadUnified(); }
  }
  // Stage 6.9: GET /api/unified-portfolio (Robinhood is read at most once per ROBINHOOD_PORTFOLIO_TTL_SECONDS on the server) - every 30 s on TRADE, on demand on POSITIONS
  async function loadUnified(force) {
    if (state.unifiedBusy || (!force && state.unifiedReadAt && Date.now() - state.unifiedReadAt < 30000)) return;
    state.unifiedBusy = true; state.unifiedReadAt = Date.now();
    try { state.unified = await getJson("/api/unified-portfolio"); state.unifiedError = null; }
    catch (e) { state.unified = null; state.unifiedError = e.message; }                                // a failed read never keeps an old "Loaded" on screen
    finally { state.unifiedBusy = false; }
  }
  // Stage 6.8 TRADE › Bot Activity: GET /api/execution/activity every 10 s (every 2 s while the worker reports a cycle in progress); never a broker call
  function ensureBotActivity() {
    if ($("pui-bot-activity")) return true;
    const cards = $("pui-cards");
    if (!cards) return false;
    const el = document.createElement("section"); el.id = "pui-bot-activity"; el.className = "pui-section pui-bot-host";
    el.addEventListener("toggle", (e) => { const d = e.target; if (d && d.dataset && d.dataset.puiBotFold) state.botOpen[d.dataset.puiBotFold] = d.open; }, true);
    cards.insertAdjacentElement("afterend", el);
    placeBotActivity();
    renderBotActivity();
    return true;
  }
  // where the panel lives: the default layout already scrolls (tall cards), so it sits below the TRADE cards; View details is the Stage 5.5 layout
  // that fits the window (.tv-grid = 100vh - 240px), so it moves INTO the detail column, before the sticky Actions panel: it scrolls inside the
  // column and never adds page height, moves Risk Controls / PPE / Actions, or pushes the search out of view. Nothing in it is hidden.
  function placeBotActivity() {
    const el = $("pui-bot-activity");
    if (!el) return;
    const right = q("#tv-body .tv-right"), cards = $("pui-cards"), inColumn = !collapsed() && !!right;
    if (inColumn) {
      const last = right.querySelector(":scope > .tv-panel-last");
      if (el.parentElement !== right || el.nextElementSibling !== last) right.insertBefore(el, last);
    } else if (cards && el.previousElementSibling !== cards) cards.insertAdjacentElement("afterend", el);
    el.classList.toggle("pui-bot-in-column", inColumn);
  }
  function loadBotActivity(force) {
    const a = state.botAct, every = a && a.worker && a.worker.state === "RUNNING" ? 2000 : 10000;
    if (state.botBusy || (!force && state.botReadAt && Date.now() - state.botReadAt < every)) return;
    state.botBusy = true; state.botReadAt = Date.now();
    getJson("/api/execution/activity").then((d) => { state.botAct = d; state.botConn = { okAt: Date.now(), okAtIso: new Date().toISOString(), error: null }; })
      .catch((e) => { state.botConn = Object.assign({}, state.botConn, { error: e.message || "error" }); })
      .finally(() => { state.botBusy = false; renderBotActivity(); });
  }
  function renderBotActivity() {
    const el = $("pui-bot-activity");
    if (!el) return;
    const html = botActivityHtml(botActivityModel(state.botAct, state.botConn), state.botOpen);
    if (el.innerHTML !== html) el.innerHTML = html;
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
    pos.addEventListener("click", (e) => { if (e.target.closest("summary, details.pui-fold")) return; const c = e.target.closest("[data-pui-pos]"); if (c) openPosition(c.dataset.puiPos, c.dataset.positionId || null); });
    const act = document.createElement("section"); act.id = "pui-activity"; act.className = "pui-section";
    act.addEventListener("click", (e) => { const f = e.target.closest("[data-pui-filter]"); if (f) { state.filter = f.dataset.puiFilter; renderActivity(); } });
    const alertsHost = $("tab-alerts");
    const legacyAlerts = document.createElement("div"); legacyAlerts.id = "pui-legacy-alerts";
    while (alertsHost.firstChild) legacyAlerts.appendChild(alertsHost.firstChild);          // the rule alerts and setup alerts stay mounted, inside a fold
    alertsHost.appendChild(act); alertsHost.appendChild(legacyAlerts);
    const more = document.createElement("section"); more.id = "pui-more"; more.className = "pui-section"; $("tab-settings").insertAdjacentElement("afterbegin", more);
    more.addEventListener("click", onMoreClick);
    more.addEventListener("toggle", (e) => { const t = e.target; if (t && t.dataset) { const g = t.querySelector(":scope > summary[data-pui-group-toggle], :scope > summary[data-pui-sub-toggle]"); const id = g ? (g.dataset.puiGroupToggle || g.dataset.puiSubToggle) : null; if (id) state.moreOpen[id] = t.open; } }, true);
    const pf = $("tab-portfolio");
    if (pf && !$("pui-pf-note")) { const n = document.createElement("div"); n.id = "pui-pf-note"; n.className = "pui-note";
      n.textContent = "Robinhood holdings are read only (local gateway): nothing in this app places, changes or cancels a Robinhood order. Paper orders go to the Alpaca PAPER account only."; pf.insertAdjacentElement("afterbegin", n); }
    document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => setTimeout(syncNav, 0)));
    document.addEventListener("click", (e) => { const d = $("pui-diag"); if (d && d.contains(e.target)) onAction(e); });
    getJson("/api/execution").then((o) => { state.limits = o.limits || null; }).catch(() => { state.limits = null; });
    // a deep link (e.g. the Daily Brief notification's /#daily-brief, consumed by brief_delivery.js) or a module that already switched the tab wins over
    // the TRADE default; the nav then simply follows the active legacy tab (MORE + crumb)
    const already = activeLegacyTab();
    // Stage 6.1.2: a legacy deep link (#portfolio, #paper, #trader, #strategy …) or a product hash opens its section; any other hash / an already-switched tab keeps the 6.1.1 rule
    const route = routeFor(location.hash);
    if (route) openRoute(route);
    else if (location.hash || (already && already !== "overview")) { state.section = HOST_OF[already] || "more"; syncNav(); } else go(DEFAULT);
    window.addEventListener("hashchange", () => { const r = routeFor(location.hash); if (r && r.section !== state.section) openRoute(r); });
    tick();
    state.timer = setInterval(tick, 2000);
    state.fetchTimer = setInterval(() => { if (state.section === "positions" && activeLegacyTab() === "paper") loadPositions(); if (state.section === "activity" && activeLegacyTab() === "alerts") loadActivity(); }, 30000);
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
  }
  return { NAV, MORE, TAGLINE, DEFAULT, AGENT, SETUP_TO_AGENT, SAFETY_TEXT, CONFIRM, LEGACY, agentModel, setupLines, positionNote, autoModel, autoFailureText, systemModel, statusModel, riskModel, controlsModel, positionModel, marketModel,
           chartModel, chartEmptyHtml, diagModel, diagHtml, positionCards, timeline, todaySummary, moreGroups, statusHtml, confirmHtml, controlsHtml, cardsHtml, positionsHtml, positionDetailsHtml,
           activityHtml, moreHtml, go, mount, tick, renderCards, renderStatus, renderChart, renderDiag, renderPositions, renderActivity, renderMore, state,
           expandDetails: () => setCollapsed(false), collapseDetails: () => setCollapsed(true), isCollapsed: collapsed,
           // Stage 6.1.2
           LEGACY_LINKS, FILTERS, PRIORITY, READY_NOTE, NO_SCORE, NO_SUPPORT, routeFor, minutesToClose, moreTabs, strategyModel, blockerModel, priorityModel, sessionModel, whyModel, holdingLines, positionLifecycle,
           dedupe, activityItems, filterItems, dailyReview, focusHtml, whyHtml, closedHtml, registryHtml, strategyDetailHtml, openRoute, openMore, renderLab,
           // Stage 6.2
           svBadge, validationHtml,
           // Stage 6.8
           accountPositionsHtml, accountCardHtml, botActivityModel, botActivityHtml, renderBotActivity, loadBotActivity,
           // Stage 6.9
           robinhoodHtml, unifiedHtml, exposureLines, exposureCardHtml, loadUnified };
});
