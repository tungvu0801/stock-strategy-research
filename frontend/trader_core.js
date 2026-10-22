// trader_core.js — Stage 5.5 Trader View: the pure browser logic (no DOM, no network) so node can test exactly what the page runs.
//   NavigationHistory    Back / Forward over visited tickers (branching truncates the forward stack; re-pushing the current is a no-op)
//   acceptsMessage       the stale-ticker guard: symbol-bound messages are dropped unless they carry the ACTIVE ticker
//   SubscriptionManager  one subscribe per socket: a switch sends unsubscribe(old) then subscribe(new); a reconnect re-subscribes once
//   nextBackoff          bounded exponential reconnect delay
// Loaded as a classic script in the dashboard (window.TraderCore) and required by tests/frontend/trader_core.test.cjs.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  root.TraderCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const SYMBOL_BOUND = new Set(["quote", "candle", "candles", "state", "position", "evidence", "ppe", "demand", "setup"]);
  const SYMBOL_RE = /^[A-Z.\-]{1,10}$/;

  class NavigationHistory {
    constructor(items) { this.items = Array.isArray(items) ? items.slice() : []; this.index = this.items.length - 1; }
    get current() { return this.index >= 0 && this.index < this.items.length ? this.items[this.index] : null; }
    push(symbol) {
      if (symbol === this.current) return;
      this.items.length = this.index + 1;
      this.items.push(symbol);
      this.index = this.items.length - 1;
    }
    canBack() { return this.index > 0; }
    canForward() { return this.index < this.items.length - 1; }
    back() { if (!this.canBack()) return null; this.index -= 1; return this.current; }
    forward() { if (!this.canForward()) return null; this.index += 1; return this.current; }
  }

  function acceptsMessage(msg, active) {
    if (!msg || typeof msg !== "object" || typeof msg.type !== "string") return false;
    if (!SYMBOL_BOUND.has(msg.type)) return true;
    return typeof msg.symbol === "string" && msg.symbol === active;
  }

  class SubscriptionManager {
    constructor(send) { this.send = send; this.active = null; this.sent = null; this.open = false; }
    setActive(symbol) { this.active = symbol || null; this.flush(); }
    onOpen() { this.open = true; this.sent = null; this.flush(); }
    onClose() { this.open = false; this.sent = null; }
    flush() {
      if (!this.open || this.sent === this.active) return;
      if (this.sent) this.send({ type: "unsubscribe", symbol: this.sent });
      if (this.active) this.send({ type: "subscribe", symbol: this.active });
      this.sent = this.active;
    }
  }

  function nextBackoff(attempt, base, cap) {
    const b = base == null ? 1000 : base;
    const c = cap == null ? 30000 : cap;
    const a = Math.max(0, Number(attempt) || 0);
    return Math.min(c, b * Math.pow(2, a));
  }

  function normalizeSymbol(raw) {
    const s = String(raw == null ? "" : raw).trim().toUpperCase();
    return SYMBOL_RE.test(s) ? s : null;
  }
  function formatPct(v) { return v == null || Number.isNaN(Number(v)) ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`; }
  function formatPrice(v) { return v == null || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(2); }

  return { NavigationHistory, acceptsMessage, SubscriptionManager, nextBackoff, normalizeSymbol, formatPct, formatPrice, SYMBOL_BOUND };
});
