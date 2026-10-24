// Node tests for the Stage 6.0.2 cash-flow line in the Paper Bot panel (frontend/paper_bot.js): one beginner line - "Cash-flow data: Fresh (12s ago)",
// "Cash-flow data: Stale", "Cash-flow data: Unclear", "Cash-flow data unavailable" - with today's trading P/L, the deposits / withdrawals adjustment,
// the daily loss and its limit under View details only; no raw payload, no internal enum, no broker id.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const P = require("../../frontend/paper_bot.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/\s+/g, " ").trim();
const ACCT = { status: "FRESH", fresh: true, age_seconds: 20, last_success_at: "2026-10-23T16:59:01+00:00", last_result: "SUCCESS", consecutive_failures: 0, attempt_count: 3, failure_count: 0, reason: "" };
const CF_FRESH = { status: "FRESH", adjustment_status: "ADJUSTED", age_seconds: 11.6, read_at: "2026-10-23T16:59:10+00:00", last_result: "SUCCESS", attempt_count: 9, failure_count: 1,
                   consecutive_failures: 0, event_count_today: 1, net_external_cash_flow: "5000.00", gate_net_external_cash_flow: "5000.00", adjustments: "CASH_FLOW_ADJUSTED",
                   daily_pnl: "-1000.00", daily_loss_pct: "0.95", threshold_pct: "5", reason: "" };
const CF_UNCLEAR = { status: "AMBIGUOUS", adjustment_status: "UNCERTAIN", age_seconds: 4, read_at: "2026-10-23T17:00:00+00:00", last_result: "SUCCESS", attempt_count: 10, failure_count: 1,
                     consecutive_failures: 0, event_count_today: 2, net_external_cash_flow: "0", adjustments: "UNAVAILABLE", daily_pnl: null, daily_loss_pct: null, threshold_pct: "5",
                     reason: "1 account activity record(s) since the baseline could not be classified" };
const CF_STALE = { status: "STALE", adjustment_status: "STALE", age_seconds: 900, read_at: "2026-10-23T16:45:00+00:00", last_result: "FAILED", last_error: "RATE_LIMITED: HTTP 429",
                   attempt_count: 14, failure_count: 4, consecutive_failures: 3, threshold_pct: "5", reason: "3 consecutive failed activity reads" };
const bot = (cf, over) => Object.assign({ mode: "MONITOR_ONLY", account_data: Object.assign({}, ACCT, cf === undefined ? {} : { cash_flow: cf }), recent: [] }, over || {});

test("1 fresh cash-flow data reads as one short line with its age", () => {
  const h = P.botHtml(bot(CF_FRESH), {});
  assert.ok(h.includes('data-bot-cashflow="FRESH"'));
  assert.ok(text(h).includes("Account data: Fresh (20s ago)") && text(h).includes("Cash-flow data: Fresh (12s ago)"));
});

test("2 stale, unclear and unavailable states read plainly", () => {
  assert.ok(text(P.botHtml(bot(CF_STALE), {})).includes("Cash-flow data: Stale"));
  assert.ok(text(P.botHtml(bot(CF_UNCLEAR), {})).includes("Cash-flow data: Unclear"));
  assert.ok(text(P.botHtml(bot({ status: "UNAVAILABLE", reason: "waiting for the first activity read of this process" }), {})).includes("Cash-flow data unavailable"));
  assert.ok(P.botHtml(bot(CF_STALE), {}).includes('tv-bot-acct stale" data-bot-cashflow="STALE"'));
});

test("3 today's trading P/L, the deposits / withdrawals adjustment, daily loss and limit only under View details", () => {
  const html = P.botHtml(bot(CF_FRESH), {});
  const visible = text(html.split("data-bot-details")[0]);
  assert.ok(!visible.includes("trading P/L") && !visible.includes("Deposits/withdrawals") && !visible.includes("-1000.00"));
  const open = text(P.botHtml(bot(CF_FRESH), { detailsOpen: true }));
  for (const s of ["Today's trading P/L -1000.00", "Deposits/withdrawals adjustment +5000.00", "Daily loss 0.95%", "Daily loss limit 5%", "9 read(s), 1 failed, 0 in a row", "1 activity record(s) today"]) {
    assert.ok(open.includes(s), s);
  }
  const unclear = text(P.botHtml(bot(CF_UNCLEAR), { detailsOpen: true }));
  assert.ok(unclear.includes("Today's trading P/L not available") && unclear.includes("Daily loss not available") && unclear.includes("could not be classified"));
  const stale = text(P.botHtml(bot(CF_STALE), { detailsOpen: true }));
  assert.ok(stale.includes("Last cash-flow error RATE_LIMITED: HTTP 429") && stale.includes("3 in a row"));
});

test("4 no cash-flow line without cash-flow data; no raw payload, enum or id in the visible text", () => {
  assert.ok(!P.botHtml(bot(undefined), {}).includes("data-bot-cashflow"));
  for (const c of [CF_FRESH, CF_UNCLEAR, CF_STALE]) {
    const t = text(P.botHtml(bot(c), { detailsOpen: true }));
    assert.ok(!/FRESH|STALE|AMBIGUOUS|ADJUSTED|UNCERTAIN|CASH_FLOW|broker_event_id|equity|::|\{|\}/.test(t), t);
  }
});

test("5 the model is exported and pure", () => {
  const m = P.cashFlowModel(CF_FRESH);
  assert.deepEqual([m.shown, m.status, m.label, m.cls], [true, "FRESH", "Cash-flow data: Fresh (12s ago)", "fresh"]);
  assert.deepEqual(P.cashFlowModel(null), { shown: false, status: "", label: "", cls: "", rows: [] });
  assert.deepEqual(P.cashFlowModel(CF_FRESH), P.cashFlowModel(CF_FRESH));
});
