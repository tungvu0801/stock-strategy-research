// Node tests for bot-position recovery in the browser (BOT_POSITION_RECOVERY_DESIGN.md revision 3.5, section 5 step 8 and section 6; decision R-3;
// Task AD). The ACTIVITY line of a MANUAL_RECOVERY position event reads "SYM · Position record corrected by <name>: <reason>" in the attention
// style, from the event's detail, rendered escaped. The MANUAL_EXTERNAL surfaces of the EXISTING code (review SD-LOW-1), in both cases (no earlier bot
// exit; an earlier partial bot exit): every surface reads "P/L unverified", except the shell's agent text, which prints the server's card line as it
// is (a dash when there was no earlier exit) - and no surface prints a raw number. The fixtures are the decorated payloads the server returns
// (tests/test_bot_position_recovery.py E11 / E12 pin the server side); no Wave 2 code is changed to make this true.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");
const P = require("../../frontend/paper_bot.js");

const AT = "2026-10-21T15:00:00+00:00";
const DAY = "2026-10-21";
const RAW = "12.34";                                                            // an earlier partial bot exit's raw realized P/L: never rendered
const recovery = (detail) => ({ position_id: "p1", kind: "MANUAL_RECOVERY", at: AT, from_state: "ATTENTION", to_state: "CLOSED", reason: "position record corrected: the broker is flat", detail });
const DETAIL = { actor: "operator", reason: "sold on Alpaca's website", evidence_hash: "ab".repeat(32), internal_qty: 5, broker_qty: 0, original_exit_reason: null, case: "CLOSE" };
const closedEvent = { position_id: "p1", kind: "CLOSED", at: AT, from_state: "ATTENTION", to_state: "CLOSED", reason: "MANUAL_EXTERNAL", detail: { source: "MANUAL_RECOVERY", exit_qty: 0 },
                      pnl_status: "UNVERIFIED", pnl_display: null, pnl_kind: "realized" };
const position = (pnl, exitQty) => ({ position_id: "p1", symbol: "SNDK", state: "CLOSED", quantity: 0, entry_qty: 5, exit_qty: exitQty, average_entry_price: "100.00", average_exit_price: pnl ? "95.00" : null,
                                     realized_pnl: pnl, exit_reason: "MANUAL_EXTERNAL", closed_at: AT, opened_at: "2026-10-21T14:00:00+00:00", sessions_held: 0,
                                     pnl_status: "UNVERIFIED", pnl_display: null, pnl_kind: "realized" });
const card = (line, pnl) => ({ key: "CLOSED", symbol: "SNDK", icon: "⚪", label: "Closed", line, shares: null, entry: "100.00", current: null, pnl, pnl_pct: null, position_id: "p1",
                               execution_id: "e-p1", state: "CLOSED", setup_state: "NONE", since: AT, reason: "Closed outside the app", details: { events: [] },
                               pnl_status: "UNVERIFIED", pnl_display: null, pnl_kind: "realized" });
const history = (pnl) => ({ kind: "TRADE", symbol: "SNDK", at: AT, shares: 5, entry: "100.00", exit: pnl ? "95.00" : null, pnl, reason: "Closed outside the app", reason_code: "MANUAL_EXTERNAL",
                            position_id: "p1", pnl_status: "UNVERIFIED", pnl_display: null, pnl_kind: "realized" });
const src = (events, positions) => ({ transitions: [], proposals: [], intents: [], positions, position_events: events, safety_events: [], findings: [], fv_events: [] });
const CASES = {                                                                  // the server's decorated card line, as tests/test_bot_position_recovery.py E11 / E12 pin it
  "no earlier exit": { pnl: null, exitQty: 0, line: "Trade completed · — · Closed outside the app" },
  "an earlier partial bot exit": { pnl: RAW, exitQty: 2, line: "Trade completed · P/L unverified · Closed outside the app" },
};

test("RA01 a MANUAL_RECOVERY event is one ACTIVITY line in the attention style, from the event's detail", () => {
  const items = PUI.timeline(src([recovery(DETAIL)], [position(null, 0)]));
  assert.deepEqual(items, [{ at: AT, text: "SNDK · Position record corrected by operator: sold on Alpaca's website", cls: "bad", kind: "attention", symbol: "SNDK" }]);
  const ai = PUI.activityItems(src([recovery(DETAIL)], [position(null, 0)]));
  const att = PUI.activityItems(src([{ position_id: "p1", kind: "ATTENTION", at: AT, reason: "BROKER_FLAT_INTERNAL_OPEN" }], [position(null, 0)]));
  assert.equal(ai[0].group, att[0].group);                                       // filtered with the other attention lines
});

test("RA02 the line is escaped when the ACTIVITY panel renders it, and a missing detail never prints an object", () => {
  const bad = recovery({ ...DETAIL, actor: "<img src=x onerror=alert(1)>", reason: "</span><script>x()</script>" });
  const html = PUI.activityHtml(src([bad], [position(null, 0)]), {}, null, DAY, {});
  assert.ok(!html.includes("<img src=x") && !html.includes("<script>"), html);
  assert.ok(html.includes("Position record corrected by &lt;img src=x onerror=alert(1)&gt;: &lt;/span&gt;&lt;script&gt;x()&lt;/script&gt;"));
  for (const detail of [undefined, null, {}]) {
    const t = PUI.timeline(src([recovery(detail)], [position(null, 0)]))[0].text;
    assert.equal(t, "SNDK · Position record corrected by the operator: no reason recorded");
  }
});

for (const [name, c] of Object.entries(CASES)) {
  test(`RA03 MANUAL_EXTERNAL, ${name}: every surface reads P/L unverified, the agent text prints the card line, and no surface prints a raw number`, () => {
    const pos = position(c.pnl, c.exitQty), cd = card(c.line, c.pnl);
    const close = PUI.timeline(src([closedEvent], [pos]))[0].text;                                   // the ACTIVITY close line
    assert.equal(close, "SNDK · Trade closed · P/L unverified");
    assert.equal(P.historyLine(history(c.pnl)), "SNDK · Closed 2026-10-21 15:00 · 5 shares · entry 100.00 → exit " + (c.pnl ? "95.00" : "—") + " · P/L unverified · Closed outside the app");
    assert.equal(P.cardModel(cd, "SNDK").line, "Trade completed · P/L unverified · Closed outside the app");        // the legacy card replaces the dash too
    assert.equal(PUI.positionLifecycle(pos, cd, [], { max_hold_sessions: 5 }, "Downside recovery", [], null).pnl, "P/L unverified");   // the shell's P/L cell
    const agent = PUI.agentModel(cd, null, null, null).text;
    assert.equal(agent, c.line);                                                  // printed as the server sent it: the dash is truthful
    for (const shown of [close, P.historyLine(history(c.pnl)), P.cardModel(cd, "SNDK").line, PUI.positionLifecycle(pos, cd, [], { max_hold_sessions: 5 }, "x", [], null).pnl, agent]) {
      assert.ok(!shown.includes(RAW), shown);
    }
  });
}
