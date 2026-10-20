// Node tests for frontend/trader_core.js — the exact logic the Trader View tab runs (run by tests/test_trader_view_55.py through node --test).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const core = require("../../frontend/trader_core.js");

test("Back / Forward history: SNDK -> NVDA -> AAPL, back twice, forward twice, branching truncates", () => {
  const nav = new core.NavigationHistory();
  for (const s of ["SNDK", "NVDA", "AAPL"]) nav.push(s);
  assert.equal(nav.current, "AAPL");
  assert.equal(nav.back(), "NVDA");
  assert.equal(nav.back(), "SNDK");
  assert.equal(nav.back(), null);
  assert.equal(nav.forward(), "NVDA");
  assert.equal(nav.forward(), "AAPL");
  assert.equal(nav.forward(), null);
  nav.back(); nav.push("MSFT");
  assert.deepEqual(nav.items, ["SNDK", "NVDA", "MSFT"]);
  nav.push("MSFT");
  assert.deepEqual(nav.items, ["SNDK", "NVDA", "MSFT"]);
  assert.equal(nav.canForward(), false);
  assert.equal(nav.canBack(), true);
});

test("stale-ticker guard: symbol-bound messages for another symbol are ignored, control messages pass", () => {
  for (const type of ["quote", "candle", "candles", "state", "position", "evidence"]) {
    assert.equal(core.acceptsMessage({ type, symbol: "SNDK" }, "NVDA"), false, type);
    assert.equal(core.acceptsMessage({ type, symbol: "NVDA" }, "NVDA"), true, type);
    assert.equal(core.acceptsMessage({ type }, "NVDA"), false, `${type} without symbol`);
  }
  for (const type of ["watchlist", "heartbeat", "hello", "subscribed", "unsubscribed", "error"]) assert.equal(core.acceptsMessage({ type, symbol: "SNDK" }, "NVDA"), true, type);
  assert.equal(core.acceptsMessage(null, "NVDA"), false);
  assert.equal(core.acceptsMessage("garbage", "NVDA"), false);
  assert.equal(core.acceptsMessage({ symbol: "NVDA" }, "NVDA"), false);
});

test("subscription manager: a switch sends unsubscribe(old) then subscribe(new); repeats are no-ops", () => {
  const sent = [];
  const sm = new core.SubscriptionManager((m) => sent.push(m));
  sm.setActive("SNDK");
  assert.deepEqual(sent, []);                                                 // socket not open: nothing is sent
  sm.onOpen();
  assert.deepEqual(sent, [{ type: "subscribe", symbol: "SNDK" }]);
  sm.setActive("NVDA");
  assert.deepEqual(sent.slice(1), [{ type: "unsubscribe", symbol: "SNDK" }, { type: "subscribe", symbol: "NVDA" }]);
  sm.setActive("NVDA");
  assert.equal(sent.length, 3);
  sm.setActive("AAPL");
  assert.deepEqual(sent.slice(3), [{ type: "unsubscribe", symbol: "NVDA" }, { type: "subscribe", symbol: "AAPL" }]);
});

test("reconnect sends exactly one subscribe for the active ticker and never a duplicate", () => {
  const sent = [];
  const sm = new core.SubscriptionManager((m) => sent.push(m));
  sm.onOpen(); sm.setActive("NVDA");
  assert.deepEqual(sent, [{ type: "subscribe", symbol: "NVDA" }]);
  sm.onClose();
  sm.onOpen();                                                                // new socket: one re-subscribe, no unsubscribe for a dead socket
  assert.deepEqual(sent.slice(1), [{ type: "subscribe", symbol: "NVDA" }]);
  sm.flush(); sm.flush();
  assert.equal(sent.length, 2);
  assert.equal(sm.sent, "NVDA");
});

test("leaving the tab (setActive(null)) sends only an unsubscribe; returning re-subscribes once", () => {
  const sent = [];
  const sm = new core.SubscriptionManager((m) => sent.push(m));
  sm.onOpen(); sm.setActive("AAPL");
  sm.setActive(null);
  assert.deepEqual(sent.slice(1), [{ type: "unsubscribe", symbol: "AAPL" }]);
  sm.setActive(null);
  assert.equal(sent.length, 2);
  sm.setActive("AAPL");
  assert.deepEqual(sent.slice(2), [{ type: "subscribe", symbol: "AAPL" }]);
});

test("reconnect backoff is exponential and bounded; symbols are normalised", () => {
  assert.equal(core.nextBackoff(0), 1000);
  assert.equal(core.nextBackoff(1), 2000);
  assert.equal(core.nextBackoff(4), 16000);
  assert.equal(core.nextBackoff(5), 30000);
  assert.equal(core.nextBackoff(50), 30000);
  assert.equal(core.nextBackoff(-3), 1000);
  assert.equal(core.normalizeSymbol(" brk.b "), "BRK.B");
  assert.equal(core.normalizeSymbol("msft"), "MSFT");
  assert.equal(core.normalizeSymbol("12345"), null);
  assert.equal(core.normalizeSymbol("ZZ$$"), null);
  assert.equal(core.normalizeSymbol(""), null);
  assert.equal(core.formatPct(1.234), "+1.23%");
  assert.equal(core.formatPct(null), "—");
});
