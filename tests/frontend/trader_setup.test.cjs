// Node tests for the Setup card (Stage 5.8) in frontend/trader_render.js and the page wiring — NONE / WATCH / READY FOR REVIEW / INVALIDATED,
// symbol-bound state, alert toasts, no duplicate toast, persisted alert list, stale / unavailable states. READY is a label, never a button.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../../frontend/trader_render.js");
const core = require("../../frontend/trader_core.js");

const TRADE_WORDS = /\b(buy|sell|recommend\w*|enter now|trade now|ready to buy|short it|go long)\b/i;
const AFFIRMATIVE_ENTRY = /(?<!not an )(?<!not a )entry signal/i;              // "not an entry signal" is the one allowed (negated) use
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const VIEW_JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "trader_view.js"), "utf8");
const ALERTS_JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "setup_alerts.js"), "utf8");
const DISCLAIMER = "Setup state is deterministic research bookkeeping: READY means the setup met its review threshold, not that a trade should be made and not a probability that the stock will rise.";

function view(over) {
  return Object.assign({
    version: "setups_v1", symbol: "SNDK", setup_type: "DOWNSIDE_DISLOCATION_RECOVERY", setup_name: "Downside Dislocation Recovery", state: "WATCH", state_label: "WATCH",
    setup_id: "a".repeat(32), cycle_id: 1, previous_state: "NONE", since: "2026-10-21T15:42:00+00:00", started_at: "2026-10-21T15:42:00+00:00", last_evaluated_at: "2026-10-21T15:43:00+00:00",
    decision: "HOLD", reason_code: "DEMAND_BELOW_60", reason_text: "Market Demand below 60.", blockers: ["CONFIDENCE_BELOW_40", "DEMAND_BELOW_60"], invalidation_reason: null, paused: false,
    inputs: { symbol: "SNDK", evaluated_at: "2026-10-21T15:43:00+00:00", stretch: -67.0, confidence: 34, demand: 41 }, freshness: { ppe_fresh: true }, source: "simulator", mode: "simulated",
    disclaimer: DISCLAIMER, note: null,
  }, over || {});
}
const NONE = view({ state: "NONE", state_label: "NO ACTIVE SETUP", setup_id: null, cycle_id: null, since: null, started_at: null, decision: "HOLD", reason_code: "NO_DOWNSIDE_STRETCH", reason_text: "No downside stretch.", blockers: ["NO_DOWNSIDE_STRETCH"], inputs: { stretch: 16.2, confidence: null, demand: 55 } });
const READY = view({ state: "READY", state_label: "READY FOR REVIEW", previous_state: "WATCH", decision: "TRANSITION", reason_code: "CONFIRMATION_STRENGTHENED", reason_text: "Buyer confirmation strengthened.", blockers: [], inputs: { stretch: -61.0, confidence: 48, demand: 76 } });
const INVALID = view({ state: "INVALIDATED", state_label: "INVALIDATED", previous_state: "READY", decision: "TRANSITION", reason_code: "DEMAND_COLLAPSED", reason_text: "Demand collapsed after confirmation.", invalidation_reason: "DEMAND_COLLAPSED", inputs: { stretch: -58.0, confidence: 50, demand: 22 } });
const PAUSED = view({ decision: "PAUSED", reason_code: "EVALUATION_PAUSED_STALE_DATA", reason_text: "Evaluation paused: critical inputs are stale.", paused: true, freshness: { ppe_fresh: false, stale_reasons: ["PPE_STALE"] }, mode: "real" });
const ALERT = { alert_id: "b".repeat(32), transition_id: "c".repeat(32), setup_id: "a".repeat(32), symbol: "NVDA", setup_type: "DOWNSIDE_DISLOCATION_RECOVERY", from_state: "WATCH", to_state: "READY", to_state_label: "READY FOR REVIEW",
                reason_code: "CONFIRMATION_STRENGTHENED", title: "NVDA — READY FOR REVIEW", body: "Buyer confirmation strengthened.\nPPE Stretch: -54 · Confidence: 58 · Demand: 72", created_at: "2026-10-21T15:46:10+00:00", read_at: null };

test("1 NONE renders as NO ACTIVE SETUP with a neutral chip", () => {
  const html = R.setupHtml(NONE);
  assert.match(html, /<span class="tv-set-state " data-setup-state="NONE">NO ACTIVE SETUP<\/span>/);
  assert.match(html, /Downside Dislocation Recovery/);
  assert.doesNotMatch(html, /since /);
  assert.equal(R.setupModel(NONE).cls, "");
});

test("2 WATCH renders with since-time, the three linked metrics and the blockers", () => {
  const html = R.setupHtml(view());
  assert.match(html, /<span class="tv-set-state watch" data-setup-state="WATCH">WATCH<\/span>/);
  assert.match(html, /<span class="tv-set-since">since 15:42 UTC<\/span>/);
  assert.match(html, /PPE Stretch <b>-67<\/b><\/span><span>Confidence <b>34<\/b><\/span><span>Demand <b>41<\/b>/);
  assert.match(html, /Not READY: CONFIDENCE BELOW 40 · DEMAND BELOW 60/);
  assert.match(html, /<span class="tv-tag warn">SIMULATED DATA<\/span>/);
});

test("3 READY renders as READY FOR REVIEW — a label with muted styling, never a button", () => {
  const html = R.setupHtml(READY);
  assert.match(html, /<span class="tv-set-state ready" data-setup-state="READY">READY FOR REVIEW<\/span>/);
  assert.doesNotMatch(html, /<button[^>]*>(READY|BUY)/i);
  assert.doesNotMatch(text(html), /\bREADY TO BUY\b/i);
  assert.equal(R.SETUP_LABELS.READY, "READY FOR REVIEW");
  assert.match(html, /evaluated 15:43:00 UTC · transition · simulator/);
});

test("4 INVALIDATED renders with its invalidation reason", () => {
  const html = R.setupHtml(INVALID);
  assert.match(html, /<span class="tv-set-state invalidated" data-setup-state="INVALIDATED">INVALIDATED<\/span>/);
  assert.match(html, /Invalidation: DEMAND COLLAPSED/);
  assert.match(html, /Reason<\/span><div>Demand collapsed after confirmation\.<\/div>/);
});

test("5 no BUY / SELL wording anywhere in the setup card or explainer", () => {
  for (const v of [NONE, view(), READY, INVALID, PAUSED, null]) {
    const t = text(R.setupHtml(v, { symbol: "SNDK" }));
    assert.doesNotMatch(t, TRADE_WORDS, t.slice(0, 120));
    assert.doesNotMatch(t, AFFIRMATIVE_ENTRY, t.slice(0, 120));
  }
  assert.doesNotMatch(R.EXPLAIN_SETUP, TRADE_WORDS);
  assert.doesNotMatch(R.EXPLAIN_SETUP, AFFIRMATIVE_ENTRY);
  assert.match(R.EXPLAIN_SETUP, /not an entry signal/);
  const n = (text(R.setupHtml(READY)).match(/probability/g) || []).length;
  assert.ok(n === 1 && text(R.setupHtml(READY)).includes(DISCLAIMER));
});

test("6 the setup reason renders", () => {
  assert.match(R.setupHtml(view()), /<div class="tv-set-reason"><span class="tv-ppe-sub">Reason<\/span><div>Market Demand below 60\.<\/div><\/div>/);
  assert.match(R.setupHtml(READY), /Reason<\/span><div>Buyer confirmation strengthened\.<\/div>/);
});

test("7 setup state updates are symbol-bound; setup_alert is not", () => {
  assert.equal(core.acceptsMessage({ type: "setup", symbol: "SNDK", state: "READY" }, "NVDA"), false);
  assert.equal(core.acceptsMessage({ type: "setup", symbol: "NVDA" }, "NVDA"), true);
  assert.equal(core.acceptsMessage({ type: "setup" }, "NVDA"), false);
  assert.equal(core.acceptsMessage({ type: "setup_alert", symbol: "SNDK" }, "NVDA"), true);
});

test("8 a stale ticker's setup state is ignored and panels carry the result's own symbol", () => {
  assert.equal(R.setupModel(view()).symbol, "SNDK");
  assert.match(VIEW_JS, /el\.dataset\.symbol = \(s && s\.symbol\) \|\| st\.active \|\| "";/);
  assert.match(VIEW_JS, /\["tv-setup", "SETUP"\]/);
  assert.match(VIEW_JS, /st\.pending\.setup = true;/);
});

test("9 a transition alert produces a toast with symbol, state, reason and time", () => {
  assert.equal(R.setupAlertLine(ALERT), "NVDA — READY FOR REVIEW · confirmation strengthened · 15:46 UTC");
  assert.match(VIEW_JS, /case "setup_alert": onSetupAlert\(m\); break;/);
  assert.match(VIEW_JS, /toast\(R\.setupAlertLine\(a\)\)/);
  assert.doesNotMatch(R.setupAlertLine(ALERT), TRADE_WORDS);
});

test("10 no duplicate toast for the same persisted alert", () => {
  assert.match(VIEW_JS, /const seenAlerts = new Set\(\);/);
  assert.match(VIEW_JS, /if \(!a \|\| !a\.alert_id \|\| seenAlerts\.has\(a\.alert_id\)\) return;/);
  assert.match(VIEW_JS, /seenAlerts\.add\(a\.alert_id\);/);
});

test("11 the persisted alert list renders from the read-only endpoint with read-acknowledgement only", () => {
  assert.match(ALERTS_JS, /fetch\("\/api\/setups\/alerts\?limit=30"\)/);
  assert.match(ALERTS_JS, /\/api\/setups\/alerts\/\$\{encodeURIComponent\(id\)\}\/read` : "\/api\/setups\/alerts\/read-all"/);
  assert.match(ALERTS_JS, /Open in Trader View/);
  assert.match(ALERTS_JS, /READY: "READY FOR REVIEW"/);
  assert.equal((ALERTS_JS.match(/method: "POST"/g) || []).length, 1);
  assert.doesNotMatch(ALERTS_JS.replace(/\/\/.*/g, ""), TRADE_WORDS);
});

test("12 unavailable / stale / loading states are explicit", () => {
  assert.match(R.setupHtml(null, { symbol: "SNDK" }), /<div class="tv-state" data-state="SETUP_NOT_AVAILABLE">SETUP NOT AVAILABLE<\/div><p class="tv-dim">No setup state for SNDK yet\.<\/p>/);
  assert.match(R.setupHtml(null, { loading: true, symbol: "SNDK" }), /LOADING SETUP · SNDK/);
  const paused = R.setupHtml(PAUSED);
  assert.match(paused, /<span class="tv-tag warn">PAUSED · STALE DATA<\/span>/);
  assert.match(paused, /Evaluation paused: critical inputs are stale\./);
  assert.doesNotMatch(paused, /SIMULATED DATA/);
  assert.equal(R.setupModel({ state: "BOGUS" }).loaded, false);
});
