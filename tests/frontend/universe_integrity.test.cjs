// Node tests for the Stage 6.5.1 universe-integrity display of the Strategy Lab Validation block (frontend/product_ui.js): the "Universe integrity"
// section after the selection edge, the raw -> final edge with the UNIVERSE_INTEGRITY_CAP marker, the concise "Universe untrusted" badge (once per
// place, never for event-driven or trusted universes), an assessment of an older run, a record from before Stage 6.5.1, escaping.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const seg = (n, net) => ({ trade_count: n, candidate_count: n, accepted_count: n, stand_aside_count: 0, win_rate: 0.55, gross_pnl: net + 5, slippage_cost: 5, fees: 0, friction_total: 5,
                           net_pnl: net, mean_R: 0.02, average_R_gross: 0.02, mean_R_t_stat: 5.1, expectancy_usd: 80, profit_factor: 1.6, max_drawdown: 15000 });
const csSeg = (n, t) => ({ period_count: n, strategy_total_return: 0.5, control_total_return: 0.45, benchmark_total_return: 0.3, mean_excess_period_return: 0.002, excess_t_stat: t,
                           excess_win_rate: 0.55, active_volatility: 0.04, worst_relative_period: { rebalance: "2025-04-01", excess: -0.02 }, max_relative_drawdown: 0.03,
                           cumulative_excess_return: 0.05, strategy_turnover: 0.3, control_turnover: 0.03, concentration: { applicable: true, concentrated: false, text: "spread" } });
const CS = { unit: "one observation per rebalance period", control: { control_id: "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", label: "Equal-weight eligible universe", role: "SELECTION_CONTROL" },
             market_benchmark: { symbol: "SPY", role: "MARKET_BENCHMARK" }, excluded_periods: { BOUNDARY: 1, NO_ELIGIBLE_STOCK: 0 }, parity: { status: "PASS", note: "control parity: 8 / 8 checks pass" },
             segments: { TRAIN: csSeg(30, 2.4), OOS: csSeg(14, 2.3), ALL: csSeg(44, 3.1) } };
const ABS = { verdict: "PASS", label: "PASS", reasons: [{ code: "ALL_CHECKS_PASSED", text: "Adequate samples" }], warnings: [], decision: "Eligible for review", eligible_for_review: true };
const OVERALL = { verdict: "FAIL", label: "FAIL", decision: "Not ready for paper", eligible_for_review: false, note: "A failed validation is a valid research result.",
                  reasons: [{ code: "SELECTION_EDGE_NOT_ESTABLISHED", text: "No evidence that the selection beats its control." }], warnings: [] };
const EDGE = { status: "NOT_ESTABLISHED", label: "Not established", raw_status: "ESTABLISHED", capped: true, universe_integrity: "UNTRUSTED",
               reasons: [{ code: "UNIVERSE_INTEGRITY_CAP", text: "The raw selection edge is ESTABLISHED; the universe is UNTRUSTED (FIXED_CURRENT_UNIVERSE), so the final edge is at most NOT_ESTABLISHED." }],
               policy: { min_oos_periods: 12, t_established: 2 } };
const RUN = { run_id: "5".repeat(32), created_at: "2026-10-26T01:00:00+00:00", strategy_version: "1.0", dataset_kind: "SYNTHETIC", dataset_start: "2019-01-02", dataset_end: "2022-12-30",
              train_start: "2019-01-02", train_end: "2021-08-01", oos_start: "2021-08-02", oos_end: "2022-12-30", oos_sessions: 360, candidate_count: 900, verdict: OVERALL,
              integrity: { same_candidate_set: true }, friction_model: { slippage_bps_per_side: 5 }, causality: { automated_checks: [] }, audit: { status: "PASS", note: "all trades validated" },
              arms: { RULES: { status: "EVALUATED", label: "PASS", candidate_count: 900, TRAIN: seg(600, 60000), OOS: seg(280, 30000), ALL: seg(880, 90000) },
                      GATED: { status: "EVALUATED", label: "PASS", candidate_count: 900, TRAIN: seg(600, 60000), OOS: seg(280, 30000), ALL: seg(880, 90000) },
                      MODEL: { status: "NOT_EVALUATED", label: "Not evaluated", candidate_count: 900 } } };
const DECL = { universe_method: "FIXED_CURRENT_UNIVERSE", universe_source: "today's top 100 by market value", membership_as_of_semantics: "today's list at every rebalance",
               includes_delisted: false, includes_acquired: false, includes_bankrupt: false, historical_additions_supported: false, historical_removals_supported: false,
               survivorship_bias: "PRESENT", coverage_start: null, coverage_end: null, notes: "" };
const UI = (over) => ({ policy_version: "universe_integrity_v1", method: "FIXED_CURRENT_UNIVERSE", status: "UNTRUSTED", cap: "NOT_ESTABLISHED", point_in_time: false,
                        survivorship_bias: "PRESENT", includes_delisted: false, reasons: ["today's list projected backwards"],
                        consequence: "The selection edge cannot be established: the universe is not point in time (cannot establish alpha).", declaration: DECL,
                        raw_edge_status: "ESTABLISHED", final_edge_status: "NOT_ESTABLISHED", source: "run", assessment: null, ...(over || {}) });
const DETAIL = (over) => ({ validation_status: "FAIL", validation_label: "FAIL", latest_validation_run_id: RUN.run_id, validation_verdict: "FAIL", evaluation_mode: "CROSS_SECTIONAL_PORTFOLIO",
                            performance_verdict: "PASS", selection_edge_status: "NOT_ESTABLISHED", selection_edge_label: "Not established", raw_selection_edge_status: "ESTABLISHED",
                            universe_integrity_status: "UNTRUSTED", universe_method: "FIXED_CURRENT_UNIVERSE", research_flags: ["SURVIVORSHIP_BIAS_PRESENT"], policy: {},
                            disclaimer: "Backtest results are hypothetical.", run: RUN, overall_verdict: OVERALL, absolute_verdict: ABS, selection_edge: EDGE, cross_sectional: CS,
                            methodology_evaluation: null, universe_integrity: UI(), ...(over || {}) });

test("1 the Universe integrity section follows the selection edge and states every field plainly", () => {
  const h = PUI.validationHtml(DETAIL());
  assert.ok(h.indexOf('data-pui-sv-section="edge"') < h.indexOf('data-pui-sv-section="universe"'));
  const t = text(h.slice(h.indexOf('data-pui-sv-section="universe"')));
  for (const s of ["Universe method Fixed current universe", "Point-in-time membership No", "Survivorship bias (declared) Present", "Delisted securities included No",
                   "Integrity status UNTRUSTED", "Effect on the selection-edge verdict The selection edge cannot be established",
                   "raw Established → final Not established (UNIVERSE_INTEGRITY_CAP)", "does not remove survivorship bias", "Source: today's top 100 by market value."]) {
    assert.ok(t.includes(s), s + " | " + t.slice(0, 900));
  }
  assert.match(h, /data-pui-sv-universe="UNTRUSTED"/);
  assert.ok(!text(h).includes("Eligible for review") || text(h).indexOf("Eligible for review") < text(h).indexOf("Control comparison"));
});

test("2 one concise Universe untrusted badge per place; none for a trusted universe or an event-driven run", () => {
  const sum = { validation_status: "FAIL", validation_label: "FAIL", evaluation_mode: "CROSS_SECTIONAL_PORTFOLIO", selection_edge_status: "NOT_ESTABLISHED", universe_integrity_status: "UNTRUSTED" };
  assert.equal((PUI.svBadge(sum).match(/data-pui-sv-universe-badge/g) || []).length, 1);
  assert.equal((PUI.validationHtml(DETAIL()).match(/data-pui-sv-universe-badge/g) || []).length, 1);
  assert.ok(!/universe-badge/.test(PUI.svBadge({ ...sum, universe_integrity_status: "TRUSTED" })));
  assert.ok(!/universe-badge/.test(PUI.svBadge({ validation_status: "PASS", evaluation_mode: "EVENT_DRIVEN", universe_integrity_status: "NOT_APPLICABLE" })));
  const trusted = PUI.validationHtml(DETAIL({ universe_integrity_status: "TRUSTED", universe_integrity: UI({ status: "TRUSTED", cap: null, point_in_time: true, final_edge_status: "ESTABLISHED" }) }));
  assert.ok(!/universe-badge/.test(trusted) && !text(trusted).includes("(UNIVERSE_INTEGRITY_CAP)") && text(trusted).includes("Point-in-time membership Yes"));
});

test("3 an append-only assessment of an older run says the run is unchanged; a record from before Stage 6.5.1 is 'not assessed'", () => {
  const a = { assessment_id: "a".repeat(32), policy_version: "universe_integrity_v1", created_at: "2026-10-26T02:00:00+00:00" };
  const h = PUI.validationHtml(DETAIL({ universe_integrity: UI({ source: "assessment", assessment: a }) }));
  assert.match(h, new RegExp(`data-pui-sv-universe-assessment="${"a".repeat(32)}"`));
  assert.ok(text(h).includes("universe_integrity_v1 assessment (2026-10-26T02:00:00+00:00) of a stored run: the run is unchanged."));
  const na = PUI.validationHtml(DETAIL({ universe_integrity: UI({ method: "UNKNOWN", declaration: null, survivorship_bias: null, includes_delisted: null, source: "not_assessed" }) }));
  assert.match(na, /data-pui-sv-universe-source="not_assessed"/);
  assert.ok(text(na).includes("Universe method Unknown") && text(na).includes("Survivorship bias (declared) Unknown") && text(na).includes("its universe is treated as unknown"));
  const old = PUI.validationHtml(DETAIL({ universe_integrity: null, universe_integrity_status: null }));
  assert.match(old, /data-pui-sv-universe="NOT_ASSESSED"/);
  assert.ok(text(old).includes("Universe integrity: not assessed") && !/universe-badge/.test(old));
});

test("4 declared texts are escaped", () => {
  const h = PUI.validationHtml(DETAIL({ universe_integrity: UI({ declaration: { ...DECL, universe_source: "<img src=x onerror=alert(1)>" } }) }));
  assert.ok(!h.includes("<img src=x") && h.includes("&lt;img src=x onerror=alert(1)&gt;"));
});
