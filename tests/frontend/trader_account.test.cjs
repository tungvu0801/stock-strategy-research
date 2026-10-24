// Node tests for the Stage 6.0.1 account-data line in the Paper Bot panel (frontend/paper_bot.js): one beginner line - "Account data: Fresh (42s ago)",
// "Account data: Stale", "Account data: Checking", "Account data unavailable" - with the refresh details under View details only; no raw payload.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const P = require("../../frontend/paper_bot.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const FRESH = { status: "FRESH", fresh: true, age_seconds: 41.6, last_success_at: "2026-10-23T16:59:01+00:00", last_attempt_at: "2026-10-23T16:59:01+00:00", last_result: "SUCCESS",
                last_error: null, consecutive_failures: 0, attempt_count: 12, failure_count: 1, reason: "" };
const STALE = { status: "STALE", fresh: false, age_seconds: 195, last_success_at: "2026-10-23T16:55:00+00:00", last_result: "FAILED", last_error: "TIMEOUT", consecutive_failures: 3,
                attempt_count: 15, failure_count: 4, reason: "3 consecutive failed refreshes" };
const bot = (acct, over) => Object.assign({ mode: "MONITOR_ONLY", account_data: acct, recent: [] }, over || {});

test("1 fresh account data reads as one short line with its age", () => {
  const h = P.botHtml(bot(FRESH), {});
  assert.ok(h.includes('data-bot-account="FRESH"'));
  assert.ok(text(h).includes("Account data: Fresh (42s ago)"));
});

test("2 stale and unavailable states read plainly", () => {
  assert.ok(text(P.botHtml(bot(STALE), {})).includes("Account data: Stale"));
  assert.ok(text(P.botHtml(bot({ status: "UNAVAILABLE", reason: "no successful refresh yet" }), {})).includes("Account data unavailable"));
  assert.ok(text(P.botHtml(bot({ status: "DISABLED", reason: "no paper broker configured" }), {})).includes("Account data unavailable"));
  assert.ok(text(P.botHtml(bot({ status: "STARTING", reason: "waiting" }), {})).includes("Account data: Checking"));
  assert.ok(P.botHtml(bot(STALE), {}).includes("tv-bot-acct stale"));
});

test("3 refresh details only under View details", () => {
  const html = P.botHtml(bot(STALE), {});
  const visible = text(html.split('data-bot-details')[0]);                       // everything before the (hidden) details block
  assert.ok(visible.includes("Account data: Stale") && !visible.includes("3 in a row") && !visible.includes("TIMEOUT"));
  assert.ok(/class="tv-bot-details tv-hidden" data-bot-details/.test(html));
  const open = text(P.botHtml(bot(STALE), { detailsOpen: true }));
  assert.ok(open.includes("Account refresh") && open.includes("3 consecutive failed refreshes") && open.includes("15 attempt(s), 4 failed, 3 in a row") && open.includes("Last refresh error TIMEOUT"));
  const fresh = text(P.botHtml(bot(FRESH), { detailsOpen: true }));
  assert.ok(fresh.includes("last success 16:59:01 UTC (42s ago)") && !fresh.includes("Last refresh error"));
});

test("4 no account line without account data; no raw payload or internal enum in the visible text", () => {
  assert.ok(!P.botHtml(bot(undefined), {}).includes("data-bot-account"));
  for (const a of [FRESH, STALE]) {
    const t = text(P.botHtml(bot(a), { detailsOpen: true }));
    assert.ok(!/FRESH|STALE|UNAVAILABLE|equity|buying_power|account_number|\{|\}/.test(t), t);
  }
});

test("5 the model is exported and pure", () => {
  const m = P.accountModel(FRESH);
  assert.deepEqual([m.shown, m.status, m.label, m.cls], [true, "FRESH", "Account data: Fresh (42s ago)", "fresh"]);
  assert.deepEqual(P.accountModel(null), { shown: false, status: "", label: "", cls: "", detail: "", counts: "", error: "" });
});
