// Node tests for the Stage 6.4.1 (validation_v3) cross-sectional display of the Strategy Lab Validation block (frontend/product_ui.js): the three
// separate sections (absolute performance / control comparison / selection edge), the SELECTION CONTROL vs MARKET BENCHMARK labels, the per-leg
// t-statistic marked NOT SELECTION-EDGE EVIDENCE, a methodology evaluation of an older run, the registry badge, event-driven runs unchanged, escaping.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const seg = (n, net) => ({ trade_count: n, candidate_count: n, accepted_count: n, stand_aside_count: 0, win_rate: 0.56, gross_pnl: net + 5, slippage_cost: 5, fees: 0, friction_total: 5,
                           net_pnl: net, mean_R: 0.017, average_R_gross: 0.017, mean_R_t_stat: 6.3, expectancy_usd: 83.7, profit_factor: 1.7, max_drawdown: 18923 });
const csSeg = (n, s, c, b, m, t) => ({ period_count: n, strategy_total_return: s, control_total_return: c, benchmark_total_return: b, mean_excess_period_return: m, excess_t_stat: t,
                                       excess_win_rate: 0.5, active_volatility: 0.05, worst_relative_period: { rebalance: "2025-04-01", excess: -0.021 }, max_relative_drawdown: 0.04,
                                       cumulative_excess_return: s - c, strategy_turnover: 0.08, control_turnover: 0.03,
                                       concentration: { applicable: true, concentrated: false, text: "the best period carries 31 % of the summed excess" } });
const CS = { unit: "one observation per rebalance period (the legs of one rebalance are not independent)",
             control: { control_id: "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", label: "Equal-weight eligible universe", role: "SELECTION_CONTROL" },
             market_benchmark: { symbol: "SPY", role: "MARKET_BENCHMARK" }, excluded_periods: { BOUNDARY: 1, NO_ELIGIBLE_STOCK: 0 },
             parity: { status: "PASS", note: "control parity: 8 / 8 checks pass" },
             segments: { TRAIN: csSeg(51, 1.2266, 1.1532, 0.8104, 0.00073, 0.32), OOS: csSeg(22, 0.5481, 0.5217, 0.3187, 0.0011, 0.18), ALL: csSeg(73, 2.4469, 2.2767, 1.3874, 0.00084, 0.35) } };
const EDGE = { status: "NOT_ESTABLISHED", label: "Not established", reasons: [{ code: "EXCESS_NEGLIGIBLE", text: "OOS mean excess +0.11 % per period with t = 0.18 over 22 periods (< 1.0): statistically indistinguishable from zero." }],
               policy: { min_oos_periods: 12, t_established: 2 } };
const ABS = { verdict: "PASS", label: "PASS", reasons: [{ code: "ALL_CHECKS_PASSED", text: "Adequate samples ..." }], warnings: [], decision: "Eligible for review", eligible_for_review: true };
const OVERALL = { verdict: "FAIL", label: "FAIL", decision: "Not ready for paper", eligible_for_review: false, note: "This strategy should not progress to paper trading. A failed validation is a valid research result.",
                  reasons: [{ code: "SELECTION_EDGE_NOT_ESTABLISHED", text: "No evidence that the selection beats its control. Absolute performance: PASS - not selection evidence on its own." }], warnings: [] };
const RUN = { run_id: "1660bd5091ad4799a6908b9e8ddc6f2f", created_at: "2026-10-26T01:13:30+00:00", strategy_version: "1.0", dataset_kind: "HISTORICAL", dataset_start: "2020-07-27", dataset_end: "2026-10-23",
              train_start: "2020-07-27", train_end: "2024-12-05", oos_start: "2024-12-06", oos_end: "2026-10-23", oos_sessions: 471, candidate_count: 1480, verdict: ABS,
              integrity: { same_candidate_set: true }, friction_model: { slippage_bps_per_side: 5 }, causality: { automated_checks: [] }, audit: { status: "PASS", note: "2920 / 2920 trades validated" },
              diagnostics: { strategy: { portfolio: { OOS: { strategy: { total_return: 0.53 } } }, note: "v2 diagnostics" } },
              arms: { RULES: { status: "EVALUATED", label: "PASS", candidate_count: 1480, TRAIN: seg(1020, 85409), OOS: seg(440, 45538), ALL: seg(1460, 130947) },
                      GATED: { status: "EVALUATED", label: "PASS", candidate_count: 1480, TRAIN: seg(1020, 85409), OOS: seg(440, 45538), ALL: seg(1460, 130947) },
                      MODEL: { status: "NOT_EVALUATED", label: "Not evaluated", candidate_count: 1480 } } };
const EV = { evaluation_id: "e".repeat(32), created_at: "2026-10-26T02:00:00+00:00", methodology_version: "validation_v3", run_id: RUN.run_id, evaluated_run_validation_version: "validation_v2" };
const DETAIL = (over) => ({ validation_status: "FAIL", validation_label: "FAIL", latest_validation_run_id: RUN.run_id, validation_verdict: "FAIL", evaluation_mode: "CROSS_SECTIONAL_PORTFOLIO",
                            performance_verdict: "PASS", selection_edge_status: "NOT_ESTABLISHED", selection_edge_label: "Not established", research_flags: ["SURVIVORSHIP_BIAS_PRESENT"],
                            policy: {}, disclaimer: "Backtest results are hypothetical.", run: RUN, overall_verdict: OVERALL, absolute_verdict: ABS, selection_edge: EDGE, cross_sectional: CS,
                            methodology_evaluation: EV, ...(over || {}) });

test("1 three separate sections in order: absolute performance, control comparison, selection edge; the overall verdict is not PASS", () => {
  const h = PUI.validationHtml(DETAIL());
  assert.ok(h.indexOf('data-pui-sv-section="absolute"') < h.indexOf('data-pui-sv-section="control"') && h.indexOf('data-pui-sv-section="control"') < h.indexOf('data-pui-sv-section="edge"'));
  assert.ok(h.includes('data-pui-sv-verdict="FAIL"') && text(h).includes("FAIL · Not ready for paper") && !text(h).includes("Eligible for review"));
  assert.ok(h.includes('data-pui-sv-absolute="PASS"') && text(h).includes("Absolute performance: PASS") && h.includes('data-pui-sv-edge="NOT_ESTABLISHED"'));
  assert.ok(text(h).includes("Selection edge: Not established") && text(h).includes("statistically indistinguishable from zero"));
  assert.ok(!h.includes("data-pui-sv-portfolio"));                                                    // the v2 diagnostics table is not shown beside the v3 comparison
});

test("2 the control comparison: one row per measure, SELECTION CONTROL and MARKET BENCHMARK labelled apart, excess in pp and per period", () => {
  const t = text(PUI.validationHtml(DETAIL()));
  assert.ok(t.includes("Rebalance periods 51 22 73") && t.includes("Strategy 122.7% 54.8% 244.7%"), t);
  assert.ok(t.includes("SELECTION CONTROL Equal-weight eligible universe 115.3% 52.2% 227.7%") && t.includes("MARKET BENCHMARK SPY 81.0% 31.9% 138.7%"));
  assert.ok(t.includes("Excess over the control +7.3 pp +2.6 pp +17.0 pp") && t.includes("Mean excess per period +0.07% +0.11% +0.08%"));
  assert.ok(t.includes("Excess t-statistic (per period) 0.32 0.18 0.35") && t.includes("Worst relative period 2025-04-01 -2.10%"));
  assert.ok(t.includes("SPY is a market benchmark only: beating it is not evidence of selection.") && t.includes("control parity: 8 / 8 checks pass · excluded on both sides: 1 boundary"));
});

test("3 the per-leg t-statistic is labelled NOT SELECTION-EDGE EVIDENCE for a cross-sectional strategy only", () => {
  assert.ok(text(PUI.validationHtml(DETAIL())).includes("Mean R t-stat (per leg · NOT SELECTION-EDGE EVIDENCE) 6.30 6.30 6.30"));
  const ev = PUI.validationHtml({ ...DETAIL(), evaluation_mode: "EVENT_DRIVEN", cross_sectional: null, selection_edge: { status: "NOT_APPLICABLE" }, overall_verdict: ABS, validation_status: "PASS" });
  assert.ok(!ev.includes("NOT SELECTION-EDGE EVIDENCE") && !ev.includes("data-pui-sv-control") && !ev.includes("data-pui-sv-edge-block") && text(ev).includes("Mean R t-stat 6.30"));
  assert.ok(text(ev).includes("PASS · Eligible for review"));                                         // an event-driven PASS is unchanged
});

test("4 a methodology evaluation of an older run says what it is: the older record is unchanged and keeps its own verdict", () => {
  const h = PUI.validationHtml(DETAIL());
  assert.ok(h.includes(`data-pui-sv-evaluation="${"e".repeat(32)}"`));
  assert.ok(text(h).includes("Methodology validation_v3 evaluation of run 1660bd5091ad4799a6908b9e8ddc6f2f") && text(h).includes("recorded under validation_v2: that record is unchanged"));
  assert.ok(!PUI.validationHtml(DETAIL({ methodology_evaluation: null })).includes("data-pui-sv-evaluation"));
});

test("5 the registry badge carries the selection edge for a cross-sectional strategy only", () => {
  const b = PUI.svBadge({ validation_status: "FAIL", validation_label: "FAIL", evaluation_mode: "CROSS_SECTIONAL_PORTFOLIO", selection_edge_status: "NOT_ESTABLISHED", selection_edge_label: "Not established" });
  assert.ok(b.includes('data-pui-sv-status="FAIL"') && b.includes('data-pui-sv-edge="NOT_ESTABLISHED"') && text(b) === "FAIL Selection edge: Not established");
  assert.ok(!PUI.svBadge({ validation_status: "PASS", validation_label: "PASS", evaluation_mode: "EVENT_DRIVEN", selection_edge_status: "NOT_APPLICABLE" }).includes("data-pui-sv-edge"));
  assert.ok(!PUI.svBadge({ validation_status: "PASS", validation_label: "PASS" }).includes("data-pui-sv-edge"));   // an older record: nothing invented
});

test("6 escaping and read-only: control labels and reasons are escaped; no control, no form", () => {
  const h = PUI.validationHtml(DETAIL({ cross_sectional: { ...CS, control: { label: "<img src=x>" } }, selection_edge: { ...EDGE, reasons: [{ code: "X", text: "<i>x</i>" }] } }));
  assert.ok(!h.includes("<img") && !h.includes("<i>x") && h.includes("&lt;img") && !/<(button|form|input|select|textarea)\b/.test(h));
});
