// Node tests for the Stage 6.2 Validation block of the Strategy Lab (frontend/product_ui.js): the registry Validation column, the detail block with no run
// ("Not yet evaluated" and why, nothing estimated), a PASS (eligible for review, never a lifecycle change), a FAIL (stated plainly, "should not progress"),
// INSUFFICIENT DATA (the real counts, no invented performance), VALIDATION INVALID (candidate sets differ), the arm comparison (one candidate count for every arm,
// MODEL "Not evaluated"), escaping, and the read-only wiring (no control, no form, no new route: the detail payload already carries the validation).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PUI = require("../../frontend/product_ui.js");

const JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "product_ui.js"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();

const POLICY = { min_trades: 30, min_oos_trades: 20, min_oos_sessions: 60, t_stat_weak: 2, validation_version: "validation_v1" };
const DISCLAIMER = "Backtest results are hypothetical.";
const DDR_REASON = "No honest historical replay exists for Downside Dislocation Recovery.";
const NOT_RUN = { validation_status: "NOT_RUN", validation_label: "Not yet evaluated", not_run_reason: DDR_REASON, latest_validation_run_id: null, validation_verdict: null, train_trade_count: null,
                  oos_trade_count: null, mechanical_audit_status: null, last_validated_at: null, validation_synthetic: false, policy: POLICY, disclaimer: DISCLAIMER, run: null };
const seg = (n, net, r) => ({ trade_count: n, candidate_count: n, accepted_count: n, stand_aside_count: 0, win_rate: 0.6, gross_pnl: net + 10, slippage_cost: 10, fees: 0, friction_total: 10, net_pnl: net,
                              average_R_gross: r + 0.02, mean_R: r, mean_R_t_stat: 2.86, expectancy_usd: 385.9, profit_factor: 2.91, max_drawdown: 1009.3, max_drawdown_pct: 0.98, sharpe_per_trade: 0.52,
                              average_holding_bars: 2, ambiguous_bars_count: 0, configured_risk_total: 15000, achieved_risk_total: 14998.5, simulation_skipped_count: 0 });
const ARMS = (verdict) => ({ RULES: { status: "EVALUATED", label: verdict, note: "", candidate_count: 100, TRAIN: seg(70, 27013.14, 0.772), OOS: seg(30, 11577.06, 0.772), ALL: seg(100, 38590.2, 0.772) },
                             GATED: { status: "EVALUATED", label: verdict, note: "NO_ADDITIONAL_GATES: no real gate is defined, so GATED equals RULES", candidate_count: 100, TRAIN: seg(70, 27013.14, 0.772), OOS: seg(30, 11577.06, 0.772), ALL: seg(100, 38590.2, 0.772) },
                             MODEL: { status: "NOT_EVALUATED", label: "Not evaluated", note: "NOT_EVALUATED: no model decider is configured (no provider, no external call)", candidate_count: 100, TRAIN: null, OOS: null, ALL: null } });
const RUN = (verdict, over) => ({ run_id: "svr_0123456789abcdef", created_at: "2026-10-25T00:00:00Z", strategy_version: "1.0", dataset_id: "sha256:abc", dataset_kind: "SYNTHETIC", dataset_start: "2019-01-02",
  dataset_end: "2021-09-08", train_start: "2019-01-02", train_end: "2021-01-05", oos_start: "2021-01-06", oos_end: "2021-09-08", oos_sessions: 176, candidate_count: 100, candidate_set_hash: "dfabec1f8242e6a7",
  candidates_skipped: 0, integrity: { same_candidate_set: true, problems: [], candidate_set_hash: "dfabec1f8242e6a713770f951b171254", invalid_arms: [] }, friction_model: { slippage_bps_per_side: 5, commission_per_order: 0, fee_per_share: 0 },
  execution_assumptions: { entry_policy: "NEXT_BAR_OPEN", ambiguous_bar_policy: "STOP_FIRST" }, causality: { automated_checks: [{ check: "STATIC_FUTURE_SHIFT", result: "PASS" }, { check: "RUNTIME_FEATURE_AFTER_SIGNAL", result: "PASS" }],
  critical: false, manual_review_required: true, manual_review_note: "MANUAL_CAUSALITY_REVIEW_REQUIRED: automated checks catch known patterns only." },
  audit: { status: "PASS", checked: 200, validated: 200, note: "200 / 200 trades validated independently", violations: 0, first_violations: [] },
  diagnostics: { train_vs_oos: { code: "CONSISTENT", text: "TRAIN positive, OOS positive: consistent." }, sensitivity: { label: "NOT_EVALUATED", note: "no neighbouring parameter set supplied" } },
  verdict: { verdict, label: verdict === "INSUFFICIENT_DATA" ? "INSUFFICIENT DATA" : verdict === "INVALID" ? "VALIDATION INVALID" : verdict, reasons: [{ code: "R", text: "reason text" }], warnings: [],
             decision: verdict === "PASS" ? "Eligible for review" : "Not ready for paper", eligible_for_review: verdict === "PASS", note: "", lifecycle_effect: "none" },
  arms: ARMS(verdict), validation_version: "validation_v1", git_commit_sha: "9fae08b", ...(over || {}) });
const DETAIL = (run) => ({ ...NOT_RUN, validation_status: run.verdict.verdict, validation_label: run.verdict.label, not_run_reason: "", latest_validation_run_id: run.run_id, validation_verdict: run.verdict.verdict,
                           train_trade_count: 70, oos_trade_count: 30, mechanical_audit_status: run.audit.status, last_validated_at: run.created_at, validation_synthetic: run.dataset_kind === "SYNTHETIC", run });
const ENTRY = { strategy_id: "DOWNSIDE_DISLOCATION_RECOVERY", display_name: "Downside Dislocation Recovery", implemented: true, active: true, status: "PAPER_CANDIDATE", status_label: "Paper candidate", source: "INTERNAL",
                time_horizon: "SWING", direction_capability: "LONG", version: "1.0", updated_at: "2026-10-24", created_at: "2026-10-21", asset_class: "US_EQUITY" };
const DET = (validation) => ({ entry: { ...ENTRY, ...validation }, lifecycle: { label: "Paper candidate", status: "PAPER_CANDIDATE", declared: "PAPER_CANDIDATE", basis: [], decision: "" }, sections: [], links: {}, disclaimer: "d", validation });

test("1 registry: a Validation column after Lifecycle; NOT_RUN reads Not yet evaluated; a verdict shows its label; synthetic runs are marked", () => {
  const reg = { strategies: [{ ...ENTRY, ...NOT_RUN }, { ...ENTRY, strategy_id: "FIXTURE_GOOD", validation_status: "PASS", validation_label: "PASS", validation_synthetic: true },
                             { ...ENTRY, strategy_id: "FIXTURE_FAIL", validation_status: "FAIL", validation_label: "FAIL" }, { ...ENTRY, strategy_id: "OLD_ROW" }], sources: [], pipeline: [] };
  const h = PUI.registryHtml(reg, [], null);
  assert.ok(h.includes("<th>Lifecycle</th><th>Validation</th><th>Horizon</th>"));
  assert.equal((h.match(/data-pui-sv-status="NOT_RUN"/g) || []).length, 2);                                             // a row without validation fields (older payload) is NOT_RUN too
  assert.ok(h.includes('<span class="pui-badge ok" data-pui-sv-status="PASS">PASS</span>') && h.includes('<span class="pui-badge bad" data-pui-sv-status="FAIL">FAIL</span>'));
  assert.equal((h.match(/data-pui-sv-synthetic/g) || []).length, 1);
  assert.equal((text(h).match(/Not yet evaluated/g) || []).length, 2);
});

test("2 no run: Not yet evaluated with the reason and the sample policy; no metric, no arm table, nothing estimated", () => {
  const h = PUI.validationHtml(NOT_RUN);
  assert.ok(h.includes('data-pui-sv-state="NOT_RUN"') && text(h).includes("Not yet evaluated.") && text(h).includes(DDR_REASON));
  assert.ok(text(h).includes("at least 30 trades, 20 out-of-sample trades and 60 out-of-sample sessions") && text(h).includes("INSUFFICIENT DATA") && text(h).includes(DISCLAIMER));
  assert.ok(!h.includes("data-pui-sv-metrics") && !h.includes("data-pui-sv-arms") && !h.includes("data-pui-sv-verdict") && !/Net P&amp;L|Win rate|t-stat/.test(h));
  const d = PUI.validationHtml(undefined);                                                                                 // a detail payload without the block (an older backend) is the same empty state
  assert.ok(d.includes('data-pui-sv-state="NOT_RUN"') && text(d).includes("Not yet evaluated.") && !d.includes("data-pui-sv-metrics"));
  const det = PUI.strategyDetailHtml({ ...DET(NOT_RUN), validation: undefined }, []);
  assert.ok(det.includes("data-pui-sv ") && text(det).includes("Not yet evaluated"));
});

test("3 PASS: eligible for review, lifecycle unchanged, TRAIN / OOS / All metrics after friction, the audit line, the causality line", () => {
  const h = PUI.validationHtml(DETAIL(RUN("PASS")));
  assert.ok(h.includes('data-pui-sv-state="PASS"') && h.includes('data-pui-sv-verdict="PASS"') && text(h).includes("PASS · Eligible for review"));
  assert.ok(text(h).includes("Lifecycle: unchanged by validation. A lifecycle change is an explicit human decision.") && !/should not progress/.test(h));
  assert.ok(h.includes("<th>RULES arm</th><th>TRAIN</th><th>OOS</th><th>All</th>") && text(h).includes("Net P&L 27013.14 11577.06 38590.20") && text(h).includes("Gross P&L 27023.14 11587.06 38600.20"));
  assert.ok(text(h).includes("Slippage 10.00 10.00 10.00") && text(h).includes("Mean R t-stat 2.86 2.86 2.86") && text(h).includes("Win rate 60.0% 60.0% 60.0%") && text(h).includes("Trades 70 30 100"));
  assert.ok(text(h).includes("Ambiguous bars (stop first) 0 0 0") && text(h).includes("Configured risk") && text(h).includes("Achieved risk"));
  assert.ok(h.includes('data-pui-sv-audit="PASS">200 / 200 trades validated independently') && text(h).includes("Synthetic test data · 2019-01-02 – 2021-09-08"));
  assert.ok(text(h).includes("Out of sample 2021-01-06 – 2021-09-08 (176 sessions)") && text(h).includes("5 bps slippage per side"));
  assert.ok(text(h).includes("Causality: 2 automated checks, 0 critical, 0 warnings. MANUAL_CAUSALITY_REVIEW_REQUIRED") && text(h).includes("TRAIN positive, OOS positive: consistent."));
});

test("4 arm comparison: RULES / GATED / MODEL on one candidate set; MODEL reads Not evaluated with no numbers; GATED states NO_ADDITIONAL_GATES", () => {
  const h = PUI.validationHtml(DETAIL(RUN("PASS")));
  const rows = [...h.matchAll(/<tr data-pui-sv-arm="([A-Z]+)" data-pui-sv-arm-status="([A-Z_]+)"><td>[A-Z]+<\/td><td>(\d+)<\/td>(.*?)<\/tr>/g)].map((m) => [m[1], m[2], m[3], text(m[4])]);
  assert.deepEqual(rows.map((r) => r.slice(0, 3)), [["RULES", "EVALUATED", "100"], ["GATED", "EVALUATED", "100"], ["MODEL", "NOT_EVALUATED", "100"]]);
  assert.equal(rows[2][3], "— — — — — Not evaluated");
  assert.ok(text(h).includes("NO_ADDITIONAL_GATES: no real gate is defined, so GATED equals RULES") && text(h).includes("NOT_EVALUATED: no model decider is configured"));
  assert.ok(h.includes('data-pui-sv-same="1"') && text(h).includes("One candidate set for every arm (100 candidates, set dfabec1f8242)."));
});

test("5 FAIL: stated plainly with the reason, Not ready for paper, should not progress once", () => {
  const run = RUN("FAIL", { verdict: { verdict: "FAIL", label: "FAIL", reasons: [{ code: "OOS_NOT_POSITIVE", text: "OOS net expectancy is not positive after friction." }], warnings: [{ code: "W", text: "a warning" }],
                                        decision: "Not ready for paper", eligible_for_review: false, note: "This strategy should not progress to paper trading. A failed validation is a valid research result." } });
  const h = PUI.validationHtml(DETAIL(run));
  assert.ok(h.includes('data-pui-sv-verdict="FAIL"') && text(h).includes("FAIL · Not ready for paper") && h.includes('data-pui-sv-reason="OOS_NOT_POSITIVE"'));
  assert.equal((text(h).match(/This strategy should not progress to paper trading\./g) || []).length, 1);
  assert.ok(text(h).includes("Warnings: a warning") && text(h).includes("Lifecycle: unchanged by validation."));
});

test("6 INSUFFICIENT DATA: the real counts, '—' where a metric is absent, no invented performance", () => {
  const small = { ...seg(7, 1200, 0.5), mean_R_t_stat: null, profit_factor: null, sharpe_per_trade: null };
  const run = RUN("INSUFFICIENT_DATA", { oos_start: null, oos_end: null, oos_sessions: 0 });
  run.arms.RULES = { ...run.arms.RULES, TRAIN: small, OOS: null, ALL: small };
  const h = PUI.validationHtml(DETAIL(run));
  assert.ok(text(h).includes("INSUFFICIENT DATA · Not ready for paper") && text(h).includes("Out of sample none") && text(h).includes("Trades 7 — 7") && text(h).includes("Mean R t-stat — — —"));
  assert.ok(!/should not progress/.test(h));
});

test("7 VALIDATION INVALID: different candidate sets are said, with the problem", () => {
  const run = RUN("INVALID", { integrity: { same_candidate_set: false, problems: ["GATED received 99 candidates, expected 100"], invalid_arms: ["GATED"] } });
  const h = PUI.validationHtml(DETAIL(run));
  assert.ok(h.includes('data-pui-sv-same="0"') && text(h).includes("VALIDATION INVALID: the arms did not receive the same candidate set. GATED received 99 candidates, expected 100"));
  assert.ok(text(h).includes("This strategy should not progress to paper trading."));
});

test("8 escaping and the read-only wiring: no control, no form, no new route, no wording the product forbids", () => {
  const run = RUN("FAIL", { verdict: { verdict: "FAIL", label: "FAIL", reasons: [{ code: "X", text: "<script>alert(1)</script>" }], warnings: [], decision: "Not ready for paper", note: "" } });
  const h = PUI.validationHtml(DETAIL(run));
  assert.ok(!h.includes("<script>") && h.includes("&lt;script&gt;"));
  const d = PUI.strategyDetailHtml(DET(DETAIL(RUN("PASS"))), []);
  assert.ok(!/<form|<input|<select|<textarea/.test(d) && !/<button[^>]*data-pui-sv/.test(d) && !/Run backtest|Start validation|Promote|Activate|Execute|Place order|Enable Paper Auto/.test(d));
  const routes = [...JS.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((m) => m[1]);
  const allowed = ["/api/execution", "/api/execution/positions", "/api/execution/intents", "/api/execution/findings", "/api/execution-safety/events", "/api/setups/events", "/api/proposals/history",
                   "/api/forward-validation", "/api/forward-validation/events", "/api/account-refresh", "/api/terminal/state/", "/api/strategy-registry", "/api/strategy-registry/", "/api/forward-validation/frozen-controls",
                   "/api/execution/activity", "/api/account-refresh/positions", "/api/unified-portfolio",                                      // Stage 6.8: Bot Activity and the account positions (GET)
                   "/api/decision-flow", "/api/decision-flow/history", "/api/decision-flow/cycle", "/api/decision-flow/explain"];             // Stage 6.10C: Decision Flow and Decision history (GET)
  assert.ok(routes.every((r) => allowed.includes(r)), routes);                                                          // the Stage 6.1.2 GET set, unchanged: the detail read carries the validation
  assert.ok(!/regime|retry/i.test(JS) && !/\b(profitable|winning|guaranteed)\b/i.test(JS));
});

// ---- Stage 6.3.1: validation integrity is shown next to the performance verdict, never merged with it; a Stage 6.2 run says it was not recorded
const V2 = (verdict, over) => RUN(verdict, { integrity_gate: { recorded: true, status: "PASS", eligibility: "ELIGIBLE", checks: [], reasons: [], gate_version: "integrity_gate_v1" },
                                             accounting: { status: "PASS" }, boundary: { policy: "PURGE", train_boundary_purged_count: 1 }, ...(over || {}) });

test("9 integrity (v2 run): status, persistence, accounting and the TRAIN / OOS boundary purge, with the purge as a metric row", () => {
  const run = V2("PASS");
  run.arms.RULES.TRAIN = { ...run.arms.RULES.TRAIN, boundary_purged_count: 1 };
  run.arms.RULES.OOS = { ...run.arms.RULES.OOS, boundary_purged_count: 0 };
  run.arms.RULES.ALL = { ...run.arms.RULES.ALL, boundary_purged_count: 1 };
  run.causality = { ...run.causality, manual_review_status: "PENDING", manual_review_reference: "" };
  const h = PUI.validationHtml(DETAIL(run));
  assert.ok(h.includes('data-pui-sv-integrity="PASS"') && text(h).includes("Integrity PASS · persistence ELIGIBLE · accounting PASS · 1 purged at the TRAIN / OOS boundary"));
  assert.ok(text(h).includes("Purged at the TRAIN / OOS boundary 1 0 1") && text(h).includes("Manual review: PENDING."));
});

test("10 a Stage 6.2 run (no integrity record): 'not recorded', nothing invented", () => {
  const h = PUI.validationHtml(DETAIL(RUN("PASS", { integrity_gate: { recorded: false, note: "not recorded" } })));
  assert.ok(h.includes('data-pui-sv-integrity="NOT_RECORDED"') && text(h).includes("Integrity not recorded (a Stage 6.2 run, before the integrity gate)"));
  assert.ok(text(h).includes("Purged at the TRAIN / OOS boundary — — —") && !/persistence ELIGIBLE|accounting PASS|Manual review:/.test(text(h)));
});

test("11 performance and integrity are separate: a FAIL verdict with integrity PASS shows both, plainly", () => {
  const run = V2("FAIL", { verdict: { verdict: "FAIL", label: "FAIL", reasons: [{ code: "OOS_NOT_POSITIVE", text: "OOS expectancy turned negative after friction." }], warnings: [],
                                      decision: "Not ready for paper", eligible_for_review: false, note: "" } });
  const h = PUI.validationHtml(DETAIL(run));
  assert.ok(text(h).includes("FAIL · Not ready for paper") && h.includes('data-pui-sv-integrity="PASS"') && text(h).includes("This strategy should not progress to paper trading."));
});
