// Node tests for the RISK CONTROLS panel (Stage 5.9.1) in frontend/trader_render.js, the page wiring in trader_view.js and the controls in
// risk_controls.js. Beginner wording only: 🟢 Trading allowed · 🟡 New trades paused · 🔴 Trading stopped, one reason line, Pause / Resume /
// Emergency Stop, a separate two-step Clear Emergency Stop, details hidden by default, no BUY / SELL wording. The POSTs change safety state only.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../../frontend/trader_render.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const VIEW_JS = fs.readFileSync(path.join(FRONT, "trader_view.js"), "utf8");
const RISK_JS = fs.readFileSync(path.join(FRONT, "risk_controls.js"), "utf8");
const RENDER_JS = fs.readFileSync(path.join(FRONT, "trader_render.js"), "utf8");
const HTML = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
const CSS = fs.readFileSync(path.join(FRONT, "trader_view.css"), "utf8");
const codeOnly = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/.*$/gm, "$1");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const TRADE_WORDS = /\b(buy|sell|buy now|sell now|enter now|trade now|recommend\w*|you should|ready to buy|short it|go long)\b/i;
const DISCLAIMER = "Risk controls gate future entries only. Nothing here places, previews or cancels an order.";

function view(over) {
  return Object.assign({
    version: "safety_v1", state: "TRADING_ALLOWED", state_label: "Trading allowed", icon: "🟢", allowed_new_entry: true, halted: false, reason_codes: [], reason_text: "",
    daily_loss: { status: "OK", code: null, trading_date: "2026-10-21", starting_equity: "100000.00", current_equity: "99400.00", daily_pnl: "-600.00", daily_loss_pct: "0.60", threshold_pct: "3.0",
                  adjustments: "NONE_POSSIBLE", baseline_created_at: "2026-10-21T14:30:00+00:00", equity_source: "HYPOTHETICAL_REVIEW", equity_as_of: null, equity_age_s: null, latched: false,
                  triggered_at: null, triggered_loss_pct: null, note: null },
    kill_switch: { active: false, activated_at: null, activated_by: null, reason: null }, no_new_entries: { active: false, activated_at: null, activated_by: null, reason: null },
    freshness: { account_fresh: true, account_age_s: null, max_age_s: 900, evaluated_within_s: 12 }, controls: [], evaluated_at: "2026-10-21T15:01:02+00:00", trigger: "API", persisted: true,
    error: null, disclaimer: DISCLAIMER, events: [{ at: "2026-10-21T14:30:00+00:00", kind: "BASELINE_CREATED", actor: "system", reason: "HYPOTHETICAL_REVIEW" }],
  }, over || {});
}
const PAUSED = view({ state: "NO_NEW_ENTRIES", state_label: "New trades paused", icon: "🟡", allowed_new_entry: false, reason_codes: ["NO_NEW_ENTRIES_ACTIVE"], reason_text: "New trades paused by you",
                      no_new_entries: { active: true, activated_at: "2026-10-21T15:00:00+00:00", activated_by: "user", reason: null } });
const DAILY = view({ state: "HALTED", state_label: "Trading stopped", icon: "🔴", allowed_new_entry: false, halted: true, reason_codes: ["DAILY_LOSS_LIMIT_REACHED"], reason_text: "Daily loss limit reached",
                     daily_loss: Object.assign({}, view().daily_loss, { status: "TRIGGERED", code: "DAILY_LOSS_LIMIT_REACHED", current_equity: "94000.00", daily_pnl: "-6000.00", daily_loss_pct: "6.00", latched: true, triggered_at: "2026-10-21T15:01:00+00:00", triggered_loss_pct: "6.00" }) });
const KILLED = view({ state: "HALTED", state_label: "Trading stopped", icon: "🔴", allowed_new_entry: false, halted: true, reason_codes: ["KILL_SWITCH_ACTIVE"], reason_text: "Emergency stop active",
                      kill_switch: { active: true, activated_at: "2026-10-21T15:02:00+00:00", activated_by: "user", reason: "testing" } });
const UNKNOWN = view({ state: "UNKNOWN", state_label: "Trading stopped", icon: "🔴", allowed_new_entry: false, halted: true, reason_codes: ["PERSISTENCE_FAILED"], reason_text: "Safety status could not be saved", persisted: false, error: "OperationalError: disk" });

test("1 TRADING_ALLOWED renders as 🟢 Trading allowed with no reason line", () => {
  const html = R.safetyHtml(view());
  assert.match(html, /<span class="tv-safe-state ok" data-safety-state="TRADING_ALLOWED">🟢 Trading allowed<\/span>/);
  assert.doesNotMatch(html, /Reason:/);
  assert.equal(R.safetyModel(view()).allowed, true);
});

test("2 NO_NEW_ENTRIES renders as 🟡 New trades paused with its reason", () => {
  const html = R.safetyHtml(PAUSED);
  assert.match(html, /<span class="tv-safe-state paused" data-safety-state="NO_NEW_ENTRIES">🟡 New trades paused<\/span>/);
  assert.match(html, /<div class="tv-safe-reason">Reason: New trades paused by you<\/div>/);
});

test("3 HALTED renders as 🔴 Trading stopped with the daily-loss reason", () => {
  const html = R.safetyHtml(DAILY);
  assert.match(html, /<span class="tv-safe-state stopped" data-safety-state="HALTED">🔴 Trading stopped<\/span>/);
  assert.match(html, /Reason: Daily loss limit reached/);
});

test("4 the emergency-stop reason is visible", () => {
  assert.match(R.safetyHtml(KILLED), /Reason: Emergency stop active/);
  assert.match(R.safetyHtml(KILLED, { detailsOpen: true }), /Kill switch<\/span><b>Active since 15:02 UTC by user · testing<\/b>/);
});

test("5 Pause New Trades / Resume / Emergency Stop buttons follow the state", () => {
  const allowed = R.safetyHtml(view());
  assert.match(allowed, /<button type="button" class="tv-safe-btn" data-safety="pause">Pause New Trades<\/button>/);
  assert.match(allowed, /<button type="button" class="tv-safe-btn" data-safety="resume" disabled>Resume<\/button>/);
  assert.match(allowed, /<button type="button" class="tv-safe-btn danger" data-safety="stop">Emergency Stop<\/button>/);
  const paused = R.safetyHtml(PAUSED);
  assert.match(paused, /data-safety="pause" disabled>Pause New Trades/);
  assert.match(paused, /data-safety="resume">Resume/);
  const killed = R.safetyHtml(KILLED);
  assert.match(killed, /data-safety="pause" disabled>Pause New Trades/);
  assert.match(killed, /data-safety="stop" disabled>Emergency Stop/);
});

test("6 clearing an emergency stop is a separate, explicit, two-step control", () => {
  assert.doesNotMatch(R.safetyHtml(view()), /data-safety="clear"/);
  assert.doesNotMatch(R.safetyHtml(PAUSED), /data-safety="clear"/);
  assert.match(R.safetyHtml(KILLED), /<button type="button" class="tv-safe-btn danger" data-safety="clear">Clear Emergency Stop<\/button>/);
  assert.match(R.safetyHtml(KILLED, { confirmClear: true }), /data-safety="clear">Confirm: clear emergency stop<\/button>/);
  assert.match(RISK_JS, /if \(!state\.confirmClear\) \{[\s\S]*?state\.confirmClear = true;[\s\S]*?return;[\s\S]*?\}\s*clearTimeout\(state\.confirmTimer\); state\.confirmClear = false;\s*return act\("\/api\/execution-safety\/clear-emergency-stop"/);
  assert.match(RISK_JS, /if \(kind === "resume"\) return act\("\/api\/execution-safety\/resume-new-entries"/);
});

test("7 advanced details are hidden by default and hold the numbers when opened", () => {
  const closed = R.safetyHtml(DAILY);
  assert.match(closed, /<button type="button" class="tv-safe-link" data-safety="details" aria-expanded="false">View details<\/button>/);
  assert.match(closed, /<div class="tv-safe-details tv-hidden" data-safety-details>/);
  const open = R.safetyHtml(DAILY, { detailsOpen: true });
  assert.match(open, /<div class="tv-safe-details" data-safety-details>/);
  assert.match(open, /Daily loss<\/span><b>6\.00%<\/b>/);
  assert.match(open, /Threshold<\/span><b>3\.00%<\/b>/);
  assert.match(open, /Starting equity<\/span><b>\$100,000\.00 \(2026-10-21\)<\/b>/);
  assert.match(open, /Current equity<\/span><b>\$94,000\.00 · HYPOTHETICAL REVIEW<\/b>/);
  assert.match(open, /Last evaluated<\/span><b>15:01:02 UTC<\/b>/);
  assert.match(open, /Control history<\/div><ul class="tv-safe-hist" data-safety-history><\/ul>/);
});

test("8 an unavailable gate renders as stopped, never as allowed", () => {
  const html = R.safetyHtml(UNKNOWN);
  assert.match(html, /data-safety-state="UNKNOWN">🔴 Trading stopped<\/span>/);
  assert.match(html, /Reason: Safety status could not be saved/);
  assert.equal(R.safetyModel(UNKNOWN).allowed, false);
  assert.match(R.safetyHtml(null), /⚪ Checking risk controls…/);
  assert.match(R.safetyHtml(null, { error: "Risk controls unavailable (HTTP 500)." }), /Risk controls unavailable/);
});

test("9 the headline is plain language: no internal enum in the visible text, no wall of text", () => {
  for (const v of [view(), PAUSED, DAILY, KILLED, UNKNOWN]) {
    const html = R.safetyHtml(v);
    const visible = text(html);
    assert.doesNotMatch(visible, /TRADING_ALLOWED|NO_NEW_ENTRIES|HALTED|UNKNOWN|PERSISTENCE_FAILED|KILL_SWITCH_ACTIVE/);
    const headline = text(html.replace(/<div class="tv-safe-details[\s\S]*$/, ""));                       // what a beginner sees before "View details"
    assert.ok(headline.length < 260, headline);
  }
});

test("10 no BUY / SELL / recommendation wording in the panel or the control script", () => {
  for (const v of [view(), PAUSED, DAILY, KILLED, UNKNOWN]) assert.doesNotMatch(text(R.safetyHtml(v, { detailsOpen: true })), TRADE_WORDS);
  const section = RENDER_JS.split("Risk controls (Stage 5.9.1")[1].split("status strip")[0];
  assert.doesNotMatch(codeOnly(section), TRADE_WORDS);
  assert.doesNotMatch(codeOnly(RISK_JS), TRADE_WORDS);
  assert.doesNotMatch(codeOnly(RISK_JS), /probabilit|target price|expected return/i);
});

test("11 Trader View stays the primary workspace: the panel sits first in the right column and the page only mounts it", () => {
  assert.ok(VIEW_JS.indexOf("<h3>Risk Controls</h3>") < VIEW_JS.indexOf("<h3>Position</h3>"));
  assert.match(VIEW_JS, /window\.RiskControls\.mount\("tv-safety"\)/);
  assert.ok(HTML.includes('<script src="risk_controls.js"></script>') && HTML.indexOf("trade_review.js") < HTML.indexOf("risk_controls.js"));
  assert.match(RISK_JS, /window\.RiskControls = \{ mount, refresh, state \};/);
  assert.ok(CSS.includes(".tv-safe-state.stopped") && CSS.includes(".tv-safe-details") && CSS.includes(".tv-safe-btn.danger"));
});

test("12 the control script POSTs exactly the four action routes; the page itself never writes", () => {
  const posts = [...RISK_JS.matchAll(/act\("(\/api\/execution-safety\/[a-z-]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(posts, ["/api/execution-safety/clear-emergency-stop", "/api/execution-safety/emergency-stop", "/api/execution-safety/pause-new-entries", "/api/execution-safety/resume-new-entries"]);
  assert.equal((RISK_JS.match(/method: "POST"/g) || []).length, 1);
  assert.doesNotMatch(codeOnly(RISK_JS), /alpaca|robinhood|\/v2\/orders|\/orders|preview|confirm\(|submit\(/i);
  assert.doesNotMatch(codeOnly(VIEW_JS), /method:\s*["'](POST|PUT|PATCH|DELETE)/);
});

test("13 the proposal card shows entry eligibility from the live gate", () => {
  const base = { symbol: "SNDK", effective_status: "REVIEWABLE", status_label: "REVIEWABLE PROPOSAL", proposal_id: "b".repeat(32), reference_price: "172.40", side_label: "Long review proposal",
                 blockers: [], warnings: [], controls: [], submission_status: "NOT SUBMITTED", review_state: "UNREVIEWED", disclaimer: "x" };
  const ok = R.proposalHtml(Object.assign({}, base, { execution_eligibility: { allowed: true, state: "TRADING_ALLOWED", reason_codes: [], reason_text: "" } }));
  assert.match(ok, /<div class="tv-prop-exec" data-exec-allowed="true">Entry eligibility: allowed by risk controls<\/div>/);
  const no = R.proposalHtml(Object.assign({}, base, { execution_eligibility: { allowed: false, state: "NO_NEW_ENTRIES", reason_codes: ["NO_NEW_ENTRIES_ACTIVE"], reason_text: "New trades paused by you" } }));
  assert.match(no, /<div class="tv-prop-exec" data-exec-allowed="false">Entry eligibility: blocked — New trades paused by you<\/div>/);
  assert.doesNotMatch(R.proposalHtml(base), /tv-prop-exec/);
});

test("14 control history lines are short and plain", () => {
  assert.equal(R.safetyEventLine({ at: "2026-10-21T15:01:00+00:00", kind: "DAILY_HALT_TRIGGERED", actor: "system", reason: "DAILY_LOSS_LIMIT_REACHED" }), "15:01 UTC · daily halt triggered · daily loss limit reached (system)");
  assert.equal(R.safetyEventLine({ at: "2026-10-21T15:02:00+00:00", kind: "KILL_ON", actor: "user", reason: null }), "15:02 UTC · kill on (user)");
});
