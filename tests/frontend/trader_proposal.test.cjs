// Node tests for the Trade Review card (Stage 5.9) in frontend/trader_render.js, the page wiring in trader_view.js and the review-state
// controls in trade_review.js. A proposal is a review artifact: REVIEWABLE PROPOSAL / BLOCKED / NOT ELIGIBLE / STALE / EXPIRED, the status
// line always reads NOT SUBMITTED, no target price, no expected return, no BUY / SELL wording, symbol-bound, dismissal renders.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../../frontend/trader_render.js");
const core = require("../../frontend/trader_core.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const VIEW_JS = fs.readFileSync(path.join(FRONT, "trader_view.js"), "utf8");
const REVIEW_JS = fs.readFileSync(path.join(FRONT, "trade_review.js"), "utf8");
const RENDER_JS = fs.readFileSync(path.join(FRONT, "trader_render.js"), "utf8");
const HTML = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
const codeOnly = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/.*$/gm, "$1");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const TRADE_WORDS = /\b(buy now|sell now|buy|sell|enter now|trade now|ready to buy|short it|go long|you should)\b/i;
const FORECAST_WORDS = /target price|price target|take.profit|expected (return|upside|value)|probability|win rate|reward\/risk|predicted/i;
const DISCLAIMER = "This is a deterministic review proposal, not a recommendation or submitted order.";

function proposal(over) {
  return Object.assign({
    version: "proposals_v1", symbol: "SNDK", setup_id: "a".repeat(32), setup_type: "DOWNSIDE_DISLOCATION_RECOVERY", side_context: "LONG_REVIEW", side_label: "Long review proposal",
    proposal_id: "b".repeat(32), status: "REVIEWABLE", effective_status: "REVIEWABLE", status_label: "REVIEWABLE PROPOSAL", stale_reason: null,
    created_at: "2026-10-21T15:01:00+00:00", expires_at: "2026-10-21T15:06:00+00:00", price_mode: "INTRADAY", reference_price: "172.4000", price_source: "QUOTE_MID",
    price_timestamp: "2026-10-21T15:00:58+00:00", invalidation_price: "166.10", stop_rule: "STRUCTURAL", structural_low: "166.80", atr: "2.80", atr_period: 14,
    risk_per_share: "6.3000", risk_pct: "3.65", equity: "5000.00", equity_source: "HYPOTHETICAL_REVIEW", risk_fraction: "0.005", risk_budget: "25.00",
    base_qty: 3, position_cap_qty: 2, buying_power_qty: 27, final_qty: 3, max_notional: "500.00", max_position_pct: "10", cap_source: "config.MAX_POSITION_PERCENT",
    existing_position_qty: null, existing_position_notional: null, existing_avg_cost: null, proposed_notional: "517.20",
    caps: { risk_based: 3, position_cap: 3, buying_power: 27, hard_limit: 10000, final: 3, binding: "risk_based" },
    blockers: [], warnings: [{ code: "CONFIDENCE_MODERATE", text: "PPE Confidence is only moderate." }, { code: "EQUITY_HYPOTHETICAL", text: "Equity is the configured hypothetical review equity (simulated mode)." }],
    controls: [{ control_id: "SETUP_FRESHNESS", result: "PASS", reason: "fresh", input: {} }, { control_id: "KILL_SWITCH", result: "PASS", reason: "not active", input: {} },
               { control_id: "TRADABILITY", result: "WARN", reason: "unknown", input: {} }, { control_id: "QUANTITY", result: "PASS", reason: "binding cap: risk_based", input: {} }],
    inputs: { account: { equity_as_of: null } }, ppe_snapshot: {}, demand_snapshot: {}, evidence_snapshot: { available: true, strength: "WEAK", cause: "MIXED", age_days: 11 },
    setup_snapshot: { state: "READY", state_label: "READY FOR REVIEW", since: "2026-10-21T15:00:00+00:00", inputs: { stretch: -54.0, confidence: 58, demand: 72 } },
    review_state: "UNREVIEWED", reviewed_at: null, submission_status: "NOT SUBMITTED", trigger: "SETUP_EVENT", source: "simulator", mode: "simulated", disclaimer: DISCLAIMER, note: null,
  }, over || {});
}
const BLOCKED = proposal({ status: "BLOCKED", effective_status: "BLOCKED", status_label: "BLOCKED", final_qty: 0, proposed_notional: null,
                           blockers: [{ code: "KILL_SWITCH_ACTIVE", text: "The review halt is active." }, { code: "BUYING_POWER_INSUFFICIENT", text: "Buying power does not cover one share with the 5 % buffer." }] });
const NOT_ELIGIBLE = { version: "proposals_v1", symbol: "AAPL", setup_id: null, status: "NOT_ELIGIBLE", effective_status: "NOT_ELIGIBLE", status_label: "NOT ELIGIBLE", proposal_id: null,
                       blockers: [{ code: "SETUP_NOT_FOUND", text: "No open setup episode for this ticker." }], warnings: [], controls: [], review_state: "UNREVIEWED", submission_status: "NOT SUBMITTED",
                       side_label: "Long review proposal", disclaimer: DISCLAIMER, mode: "simulated" };
const STALE = proposal({ effective_status: "STALE", status_label: "STALE", stale_reason: "the setup is no longer READY (WATCH)" });
const EXPIRED = proposal({ effective_status: "EXPIRED", status_label: "EXPIRED", stale_reason: "past expires_at" });
const DISMISSED = proposal({ review_state: "DISMISSED", reviewed_at: "2026-10-21T15:03:00+00:00" });

test("1 REVIEWABLE PROPOSAL renders with the long-review wording and the six key values", () => {
  const html = R.proposalHtml(proposal());
  assert.match(html, /<span class="tv-prop-state reviewable" data-proposal-status="REVIEWABLE">REVIEWABLE PROPOSAL<\/span>/);
  assert.match(html, /Long review proposal/);
  assert.match(html, /Downside Dislocation Recovery/);
  for (const k of ["Reference", "Invalidation", "Risk \/ share", "Risk budget", "Max quantity", "Max notional"]) assert.match(html, new RegExp(`<span>${k}</span>`));
  assert.equal(R.proposalModel(proposal()).cls, "reviewable");
});

test("2 BLOCKED renders its blockers (hard) in their own list", () => {
  const html = R.proposalHtml(BLOCKED);
  assert.match(html, /<span class="tv-prop-state blocked" data-proposal-status="BLOCKED">BLOCKED<\/span>/);
  assert.match(html, /<div class="tv-prop-list block"><span class="tv-ppe-sub">Blockers<\/span><ul><li>The review halt is active\.<\/li><li>Buying power does not cover one share with the 5 % buffer\.<\/li><\/ul><\/div>/);
  assert.match(html, /Warnings/);
});

test("3 NOT ELIGIBLE renders the eligibility blocker, no numbers and no buttons", () => {
  const html = R.proposalHtml(NOT_ELIGIBLE);
  assert.match(html, /<span class="tv-prop-state " data-proposal-status="NOT_ELIGIBLE">NOT ELIGIBLE<\/span>/);
  assert.match(html, /Not eligible<\/span><ul><li>No open setup episode for this ticker\.<\/li>/);
  assert.doesNotMatch(html, /tv-prop-grid|tv-prop-actions|<button/);
});

test("4 STALE and EXPIRED proposals render as such with the reason", () => {
  assert.match(R.proposalHtml(STALE), /data-proposal-status="STALE">STALE<\/span>/);
  assert.match(R.proposalHtml(STALE), /Stale: the setup is no longer READY \(WATCH\) — regenerated on the next evaluation\./);
  assert.match(R.proposalHtml(EXPIRED), /data-proposal-status="EXPIRED">EXPIRED<\/span>/);
  assert.match(R.proposalHtml(EXPIRED), /Expired: past expires_at/);
});

test("5 reference, invalidation, risk per share, budget, quantity and notional render with exact money formatting", () => {
  const html = R.proposalHtml(proposal());
  assert.match(html, /<span>Reference<\/span><b>\$172\.40<\/b>/);
  assert.match(html, /<span>Invalidation<\/span><b>\$166\.10<\/b>/);
  assert.match(html, /<span>Risk \/ share<\/span><b>\$6\.30<\/b>/);
  assert.match(html, /<span>Risk budget<\/span><b>\$25\.00<\/b>/);
  assert.match(html, /<span>Max quantity<\/span><b>3 shares<\/b>/);
  assert.match(html, /<span>Max notional<\/span><b>\$517\.20<\/b>/);
  assert.match(html, /generated 15:01:00 UTC · expires 15:06 UTC · simulator/);
});

test("6 blockers render from the server text and never as a side", () => {
  const m = R.proposalModel(BLOCKED);
  assert.deepEqual(m.blockers, ["The review halt is active.", "Buying power does not cover one share with the 5 % buffer."]);
  assert.doesNotMatch(text(R.proposalHtml(BLOCKED)), TRADE_WORDS);
});

test("7 warnings render separately from blockers", () => {
  const html = R.proposalHtml(proposal());
  assert.match(html, /<div class="tv-prop-list warn"><span class="tv-ppe-sub">Warnings<\/span><ul><li>PPE Confidence is only moderate\.<\/li><li>Equity is the configured hypothetical review equity \(simulated mode\)\.<\/li><\/ul><\/div>/);
  assert.doesNotMatch(html, /tv-prop-list block/);
});

test("8 the status line says NOT SUBMITTED on every status", () => {
  for (const p of [proposal(), BLOCKED, STALE, EXPIRED, DISMISSED, NOT_ELIGIBLE]) {
    assert.match(R.proposalHtml(p), /Status <b data-proposal-submission>NOT SUBMITTED<\/b>/);
  }
  assert.match(R.proposalDetailsHtml(proposal()), /<span>Submission<\/span><b>NOT SUBMITTED<\/b>/);
});

test("9 no target price, expected return or probability anywhere in the card, the details or the module sources", () => {
  for (const p of [proposal(), BLOCKED, STALE, NOT_ELIGIBLE]) assert.doesNotMatch(text(R.proposalHtml(p)) + " " + text(R.proposalDetailsHtml(p)), FORECAST_WORDS);
  const section = RENDER_JS.split("Trade review proposal (Stage 5.9")[1].split("status strip")[0];
  assert.doesNotMatch(codeOnly(section), FORECAST_WORDS);
  assert.doesNotMatch(codeOnly(REVIEW_JS), FORECAST_WORDS);
  assert.ok(!("target_price" in proposal()) && !("expected_return" in proposal()));
});

test("10 no BUY NOW / SELL NOW / buy / sell wording; the only side wording is the long-review label", () => {
  for (const p of [proposal(), BLOCKED, STALE, NOT_ELIGIBLE, DISMISSED]) {
    const t = text(R.proposalHtml(p)) + " " + text(R.proposalDetailsHtml(p));
    assert.doesNotMatch(t, TRADE_WORDS, t.slice(0, 200));
    assert.doesNotMatch(t, /ready to buy|approved|execute/i);
  }
  const section = codeOnly(RENDER_JS.split("Trade review proposal (Stage 5.9")[1].split("status strip")[0]);
  assert.doesNotMatch(section, TRADE_WORDS);
  assert.doesNotMatch(codeOnly(REVIEW_JS), TRADE_WORDS);
  assert.match(R.proposalHtml(proposal({ side_label: "Long review proposal (server)" })), /Long review proposal \(server\)/);   // the label comes from the server (side_label)
});

test("11 a stale ticker's proposal is ignored: the message is symbol-bound and the panel carries its own symbol", () => {
  assert.equal(core.acceptsMessage({ type: "proposal", symbol: "SNDK", effective_status: "REVIEWABLE" }, "NVDA"), false);
  assert.equal(core.acceptsMessage({ type: "proposal", symbol: "NVDA" }, "NVDA"), true);
  assert.equal(core.acceptsMessage({ type: "proposal" }, "NVDA"), false);
  assert.ok(core.SYMBOL_BOUND.has("proposal"));
  assert.equal(R.proposalModel(proposal()).symbol, "SNDK");
  assert.match(VIEW_JS, /function renderProposal\(p\) \{[\s\S]*?el\.dataset\.symbol = \(p && p\.symbol\) \|\| st\.active \|\| "";/);
});

test("12 a ticker switch resets the Trade Review panel and the page wires the proposal message", () => {
  assert.match(VIEW_JS, /\["tv-proposal", "TRADE REVIEW"\]/);
  assert.match(VIEW_JS, /st\.pending\.proposal = true;/);
  assert.match(VIEW_JS, /case "proposal": renderProposal\(m\); break;/);
  assert.match(VIEW_JS, /renderProposal\(d\.proposal\)/);
  assert.match(VIEW_JS, /renderProposal\(null\)/);
  assert.ok(VIEW_JS.indexOf("<h3>Setup</h3>") < VIEW_JS.indexOf("<h3>Trade Review</h3>") && VIEW_JS.indexOf("<h3>Trade Review</h3>") < VIEW_JS.indexOf("<h3>Evidence</h3>"));
  assert.match(VIEW_JS, /applyProposal\(p\) \{[\s\S]*?if \(p && p\.symbol === st\.active\) renderProposal\(p\);/);
  assert.ok(HTML.includes('<script src="trade_review.js"></script>') && HTML.indexOf("trader_view.js") < HTML.indexOf("trade_review.js"));
});

test("13 a dismissed proposal renders as dismissed with the Dismiss button disabled", () => {
  const html = R.proposalHtml(DISMISSED);
  assert.match(html, /<span class="tv-tag">DISMISSED<\/span>/);
  assert.match(html, /<button type="button" class="tv-prop-btn" data-prop="dismiss" disabled>Dismiss<\/button>/);
  assert.match(html, /NOT SUBMITTED<\/b> · dismissed/);
  assert.match(R.proposalHtml(proposal()), /<button type="button" class="tv-prop-btn" data-prop="dismiss">Dismiss<\/button>/);
  assert.match(R.proposalHtml(proposal()), /<button type="button" class="tv-prop-btn" data-prop="details" aria-expanded="false">View Details<\/button>/);
});

test("14 the detail view has the Phase 22 sections and the controls; trade_review.js only POSTs the two review-state routes", () => {
  const d = R.proposalDetailsHtml(proposal());
  for (const s of ["SETUP CONTEXT", "PRICE / INVALIDATION", "ACCOUNT RISK", "SIZE", "CONTROLS", "WARNINGS", "STATUS"]) assert.match(d, new RegExp(`<div class="tv-ppe-sub">${s.replace("/", "\\/")}</div>`));
  assert.match(d, /<span class="tv-prop-res pass">PASS<\/span> SETUP FRESHNESS · fresh/);
  assert.match(d, /<span class="tv-prop-res warn">WARN<\/span> TRADABILITY · unknown/);
  assert.match(d, /ATR \(14\)<\/span><b>\$2\.80<\/b>/);
  assert.match(d, /Risk-based qty<\/span><b>3 shares<\/b>/);
  assert.match(d, /Equity<\/span><b>\$5,000\.00 \(HYPOTHETICAL REVIEW\)<\/b>/);
  assert.match(d, /Risk fraction<\/span><b>0\.50% of equity<\/b>/);
  const posts = [...REVIEW_JS.matchAll(/post\(`\/api\/proposals\/\$\{encodeURIComponent\(id\)\}\/(view|dismiss)`\)/g)].map((m) => m[1]).sort();
  assert.deepEqual(posts, ["dismiss", "view"]);                                   // the only two writes are review-state acknowledgements
  assert.equal((REVIEW_JS.match(/method: "POST"/g) || []).length, 1);
  assert.doesNotMatch(codeOnly(REVIEW_JS), /alpaca|robinhood|\/v2\/orders|orders|preview|confirm|submit\(/i);
  assert.doesNotMatch(codeOnly(VIEW_JS), /method:\s*["'](POST|PUT|PATCH|DELETE)/);
});
