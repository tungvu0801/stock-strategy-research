// Node tests for Stage 6.9.1 (frontend/product_ui.js): the UNIFIED_EXTERNAL_EXPOSURE entry gate on TRADE (the Position card's gate line and the
// "Cannot enter" execution line) and POSITIONS (the gate's state and its READY pre-checks), and its reason code in Bot Activity. The gate is shown
// BLOCKED only when the executor enforces it; off, it is "WOULD BLOCK · advisory". It never claims the bot holds the external position.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const HELD_TEXT = "Robinhood already holds NVDA as a user-managed real position. Blocked: 5.283683 NVDA shares already held in Robinhood REAL (user managed), $1,142.97.";
const PRE = (enforced, over) => ({ control_id: "UNIFIED_EXTERNAL_EXPOSURE", symbol: "NVDA", result: "BLOCK", reason_code: "EXTERNAL_SYMBOL_ALREADY_HELD", text: HELD_TEXT, enforced,
  would_block: true, label: enforced ? "BLOCKED" : "WOULD BLOCK · advisory", unified_state: "COMPLETE", projected_pct: "8.30", threshold_pct: "20",
  external_holdings: [{ source: "ROBINHOOD", account_type: "REAL", ownership: "USER_MANAGED", quantity: "5.283683", market_value: "1142.97" }], ...(over || {}) });
const GATE = (enabled, symbols) => ({ control_id: "UNIFIED_EXTERNAL_EXPOSURE", version: "entry_controls_v2", enabled, enforced: enabled,
  settings: { block_duplicate_symbol: true, max_symbol_exposure_pct: "20", stale_source_policy: "BLOCK_DUPLICATE_ONLY_IF_KNOWN" }, symbols: symbols || {} });
const UNI = (gate) => ({ state: "COMPLETE", sources: [{ source: "ALPACA_PAPER", account_type: "PAPER", label: "Alpaca PAPER", state: "LOADED" },
  { source: "ROBINHOOD", account_type: "REAL", label: "Robinhood · REAL · user managed", state: "LOADED", refreshed_at: "2026-10-26T23:38:00+00:00" }],
  rows: [{ source: "ROBINHOOD", account_type: "REAL", symbol: "NVDA", quantity: "5.283683", market_value: "1142.97", ownership: "USER_MANAGED", ownership_label: "User managed · the bot never trades it", managed: false }],
  exposure: { basis: "ALL_SOURCES", total_market_value: "1142.97", by_account_type: { REAL: "1142.97" }, by_ownership: { USER_MANAGED: "1142.97" }, largest: { symbol: "NVDA", weight_pct: "100.00" }, top5_pct: "100.00",
    by_symbol: [{ symbol: "NVDA", quantity: "5.283683", gross_quantity: "5.283683", market_value: "1142.97", gross_market_value: "1142.97", fully_valued: true, weight_pct: "100.00", duplicate: false,
                  accounts: [{ source: "ROBINHOOD", account_type: "REAL", ownership: "USER_MANAGED", quantity: "5.283683" }] }], missing_sources: [] },
  equity: { paper: { equity: "100000.00" }, real_external: { equity: "12423.66" }, note: "never added together" }, warnings: [], disclosure: [], current_entry_policy: "p",
  entry_gate: gate });

test("TRADE Position card: the gate line says BLOCKED when enforced, WOULD BLOCK · advisory when off, and names the real account", () => {
  const on = PUI.exposureCardHtml(UNI(GATE(true, { NVDA: PRE(true) })), "NVDA");
  assert.match(on, /data-pui-x="gate" data-pui-gate-result="BLOCK" data-pui-gate-code="EXTERNAL_SYMBOL_ALREADY_HELD" data-enforced="1"/);
  assert.match(on, /class="pui-line pui-bad"/);
  assert.match(text(on), /Entry gate · Unified exposure: BLOCKED · EXTERNAL_SYMBOL_ALREADY_HELD · Robinhood already holds NVDA as a user-managed real position\./);
  assert.match(text(on), /5\.283683 NVDA shares already held in Robinhood REAL \(user managed\)/);       // fractional, never rounded
  assert.match(text(on), /No bot position/);
  assert.doesNotMatch(text(on), /bot (already )?(owns|holds) NVDA/i);
  const off = PUI.exposureCardHtml(UNI(GATE(false, { NVDA: PRE(false) })), "NVDA");
  assert.match(off, /data-enforced="0"/);
  assert.match(off, /class="pui-line pui-warn" data-pui-x="gate"/);
  assert.match(text(off), /Unified exposure: WOULD BLOCK · advisory/);
  assert.equal(PUI.gateFor(UNI(GATE(true, {})), "NVDA"), null);
  assert.equal(PUI.gateFor(null, "NVDA"), null);
});

test("TRADE: a READY ticker with no holding anywhere still shows the gate when it has a pre-check (e.g. a concentration block)", () => {
  const u = UNI(GATE(true, { MSFT: PRE(true, { symbol: "MSFT", reason_code: "SYMBOL_CONCENTRATION_ABOVE_LIMIT", text: "Projected MSFT exposure 31.34% of the known invested value is above the 20% limit. No trade and no resize.", projected_pct: "31.34" }) }));
  const html = PUI.exposureCardHtml(u, "MSFT");
  assert.ok(html, "the card exists");
  assert.match(html, /data-pui-gate-code="SYMBOL_CONCENTRATION_ABOVE_LIMIT"/);
  assert.match(text(html), /Projected MSFT exposure 31\.34% .* above the 20% limit\. No trade and no resize\./);
  assert.equal(PUI.exposureCardHtml(UNI(GATE(true, {})), "MSFT"), null);                             // no row and no pre-check: the plain card
});

test("TRADE execution line: Cannot enter only when the gate is enforced and says BLOCK; advisory or allowed changes nothing", () => {
  const exec = { mode: "MONITOR_ONLY", safety_state: "TRADING_ALLOWED", reconciliation_healthy: true, account_data: { status: "FRESH" }, open_positions: 0, pending_entries: 0, max_open_positions: 10,
                 scheduler: { market: { regular_session: true, et_time: "11:00" } } };
  const safety = { state: "TRADING_ALLOWED" };
  const base = PUI.blockerModel(exec, safety, {}, null, { max_open_positions: 10, near_close_minutes: 15 });
  assert.equal(base.code, "MONITOR_ONLY");
  const blocked = PUI.blockerModel(exec, safety, {}, null, { max_open_positions: 10, near_close_minutes: 15 }, PRE(true));
  assert.deepEqual([blocked.level, blocked.title, blocked.code], ["blocked", "Cannot enter", "EXTERNAL_SYMBOL_ALREADY_HELD"]);
  assert.match(blocked.reason, /^Unified exposure: Robinhood already holds NVDA as a user-managed real position\./);
  assert.deepEqual(PUI.blockerModel(exec, safety, {}, null, { max_open_positions: 10, near_close_minutes: 15 }, PRE(false)), base);   // advisory: unchanged
  assert.deepEqual(PUI.blockerModel(exec, safety, {}, null, { max_open_positions: 10, near_close_minutes: 15 }, PRE(true, { result: "WARN" })), base);
  const halted = PUI.blockerModel({ ...exec, safety_state: "HALTED" }, { state: "HALTED", kill_switch: { active: true } }, {}, null, {}, PRE(true));
  assert.equal(halted.code, "KILL_SWITCH_ACTIVE");                                                 // the emergency stop still comes first
});

test("POSITIONS: the gate's state, settings and READY pre-checks; OFF reads advisory only", () => {
  const on = PUI.unifiedHtml(UNI(GATE(true, { NVDA: PRE(true) })));
  assert.match(on, /data-pui-gate="ON"/);
  assert.match(text(on), /Entry gate · UNIFIED_EXTERNAL_EXPOSURE · ON · enforced: a new Alpaca PAPER entry is blocked when it says BLOCK · symbol limit 20% \(blocks above\) · duplicate rule on · unknown data: block duplicate only if known · entry_controls_v2/);
  assert.match(on, /data-pui-gate-sym="NVDA" data-result="BLOCK"/);
  assert.match(text(on), /NVDA · BLOCKED · EXTERNAL_SYMBOL_ALREADY_HELD · Robinhood already holds NVDA/);
  const off = PUI.unifiedHtml(UNI(GATE(false, {})));
  assert.match(off, /data-pui-gate="OFF"/);
  assert.match(text(off), /OFF · advisory only, nothing is blocked/);
  assert.match(text(off), /No READY ticker to pre-check\./);
  assert.doesNotMatch(PUI.unifiedHtml(UNI(undefined)), /data-pui-gate/);                           // a payload without the gate (older server): nothing invented
  const evil = PUI.unifiedHtml(UNI(GATE(true, { NVDA: PRE(true, { text: "<img src=x onerror=alert(1)>" }) })));
  assert.doesNotMatch(evil, /<img/);
});

test("Bot Activity: a blocked ticker names the gate's reason code; other controls keep their text", () => {
  const a = { worker: { state: "IDLE" }, mode: { mode: "PAPER_AUTO", label: "Paper Auto" }, last_cycle: { at: "2026-10-26T15:00:00+00:00", mode: "PAPER_AUTO", text: "t" },
    tickers: [{ symbol: "NVDA", status: "BLOCKED", label: "Entry blocked", scope: "LATEST_CYCLE", at: "2026-10-26T15:00:00+00:00", text: "Entry blocked: x",
                reasons: [{ control: "UNIFIED_EXTERNAL_EXPOSURE", label: "Unified exposure", code: "EXTERNAL_SYMBOL_ALREADY_HELD", text: HELD_TEXT },
                          { control: "SESSION_POLICY", label: "Market session", text: "outside the US regular session" }] }] };
  const m = PUI.botActivityModel(a, { okAt: 1, okAtIso: "2026-10-26T15:00:01+00:00" });
  assert.deepEqual(m.tickers[0].reasons, [`Unified exposure: EXTERNAL_SYMBOL_ALREADY_HELD · ${HELD_TEXT}`, "Market session: outside the US regular session"]);
  assert.match(PUI.botActivityHtml(m), /Unified exposure: EXTERNAL_SYMBOL_ALREADY_HELD · Robinhood already holds NVDA/);
});
