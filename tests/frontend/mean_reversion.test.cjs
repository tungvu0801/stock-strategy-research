// Node tests for the Stage 6.6 research-diagnostics section of the Strategy Lab Validation block (frontend/product_ui.js): turnover, the share
// of the gross excess consumed by friction, concentration, the selected losers' next week and the market's up / down weeks - shown after the
// universe integrity, never invented when the diagnostics lack a field, escaped, and labelled as never a gate.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const seg = (n, net) => ({ trade_count: n, candidate_count: n, accepted_count: n, stand_aside_count: 0, win_rate: 0.5, gross_pnl: net + 5, slippage_cost: 5, fees: 0, friction_total: 5,
                           net_pnl: net, mean_R: 0.002, average_R_gross: 0.003, mean_R_t_stat: 1.1, expectancy_usd: 10, profit_factor: 1.05, max_drawdown: 9000 });
const csSeg = (n, t) => ({ period_count: n, strategy_total_return: 0.3, control_total_return: 0.31, benchmark_total_return: 0.4, mean_excess_period_return: -0.0001, excess_t_stat: t,
                           excess_win_rate: 0.49, active_volatility: 0.06, worst_relative_period: { rebalance: "2025-04-07", excess: -0.03 }, max_relative_drawdown: 0.08,
                           cumulative_excess_return: -0.01, strategy_turnover: 0.7, control_turnover: 0.03, concentration: { applicable: true, concentrated: false, text: "spread" } });
const CS = { unit: "one observation per rebalance period", control: { control_id: "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", label: "Equal-weight eligible universe", role: "SELECTION_CONTROL" },
             market_benchmark: { symbol: "SPY", role: "MARKET_BENCHMARK" }, excluded_periods: { BOUNDARY: 1 }, parity: { status: "PASS", note: "control parity: 8 / 8 checks pass" },
             segments: { TRAIN: csSeg(223, -0.2), OOS: csSeg(98, 0.1), ALL: csSeg(321, -0.1) } };
const ABS = { verdict: "PASS", label: "PASS", reasons: [], warnings: [], decision: "Eligible for review", eligible_for_review: true };
const OVERALL = { verdict: "FAIL", label: "FAIL", decision: "Not ready for paper", eligible_for_review: false, reasons: [{ code: "SELECTION_EDGE_NOT_ESTABLISHED", text: "No evidence." }], warnings: [] };
const MR_DIAG = { construction: { average_one_way_turnover: 0.71, annualized_one_way_turnover: 37.2 },
                  friction: { TRAIN: { share_of_gross_excess_consumed: null, mean_excess_gross: -0.0004, strategy_friction_per_year: 0.03 },
                              OOS: { share_of_gross_excess_consumed: 0.62, mean_excess_gross: 0.0008, strategy_friction_per_year: 0.04 },
                              ALL: { share_of_gross_excess_consumed: null, mean_excess_gross: -0.0001, strategy_friction_per_year: 0.035 } },
                  concentration: { most_held_names: [["TSLA", 0.31], ["MRVL", 0.29]], sic_major_group: { most_frequent_top_groups: [["Electronic equipment & semiconductors (SIC 36)", 120]] },
                                   excess_contribution: { top: [["<b>X</b>", 0.04]], top_five_share_of_summed_excess: 1.8 } },
                  falling_knife: { legs: 6000, share_rebounding: 0.52, share_falling_further: 0.48, share_beating_control: 0.49 },
                  market_weeks: { ALL: { benchmark_up: { periods: 180, mean_excess: 0.0012 }, benchmark_down: { periods: 141, mean_excess: -0.0016 } } } };
const RUN = (diag) => ({ run_id: "6".repeat(32), created_at: "2026-10-26T05:00:00+00:00", strategy_version: "1.0", dataset_kind: "HISTORICAL", dataset_start: "2020-07-27", dataset_end: "2026-10-23",
  train_start: "2020-07-27", train_end: "2024-12-05", oos_start: "2024-12-06", oos_end: "2026-10-23", oos_sessions: 471, candidate_count: 6400, verdict: OVERALL,
  integrity: { same_candidate_set: true }, friction_model: { slippage_bps_per_side: 5 }, causality: { automated_checks: [] }, audit: { status: "PASS", note: "all trades validated" },
  diagnostics: { strategy: diag },
  arms: { RULES: { status: "EVALUATED", label: "PASS", candidate_count: 6400, TRAIN: seg(4400, 100), OOS: seg(1960, 50), ALL: seg(6360, 150) },
          GATED: { status: "EVALUATED", label: "PASS", candidate_count: 6400, TRAIN: seg(4400, 100), OOS: seg(1960, 50), ALL: seg(6360, 150) },
          MODEL: { status: "NOT_EVALUATED", label: "Not evaluated", candidate_count: 6400 } } });
const DETAIL = (diag) => ({ validation_status: "FAIL", validation_label: "FAIL", latest_validation_run_id: "6".repeat(32), evaluation_mode: "CROSS_SECTIONAL_PORTFOLIO",
  performance_verdict: "PASS", selection_edge_status: "NOT_ESTABLISHED", selection_edge_label: "Not established", universe_integrity_status: "UNTRUSTED", research_flags: ["SURVIVORSHIP_BIAS_PRESENT"],
  policy: {}, disclaimer: "Backtest results are hypothetical.", run: RUN(diag), overall_verdict: OVERALL, absolute_verdict: ABS,
  selection_edge: { status: "NOT_ESTABLISHED", label: "Not established", reasons: [] }, cross_sectional: CS, methodology_evaluation: null,
  universe_integrity: { status: "UNTRUSTED", method: "FIXED_CURRENT_UNIVERSE", survivorship_bias: "PRESENT", includes_delisted: false, point_in_time: false, raw_edge_status: "NOT_ESTABLISHED",
                        final_edge_status: "NOT_ESTABLISHED", source: "run", consequence: "cannot establish alpha", declaration: null } });

test("1 the research section follows the universe integrity and states turnover, friction, concentration, the losers' next week and SPY weeks", () => {
  const h = PUI.validationHtml(DETAIL(MR_DIAG));
  assert.ok(h.indexOf('data-pui-sv-section="universe"') < h.indexOf('data-pui-sv-section="research"'));
  const t = text(h.slice(h.indexOf('data-pui-sv-section="research"')));
  for (const s of ["One-way turnover 71.0% per period · 37.2× a year", "Gross excess consumed by friction TRAIN no positive gross excess · OOS 62.0% · All no positive gross excess",
                   "Strategy friction 3.5% a year", "Top five names' share of the summed excess 180.0%", "Most held TSLA 31.0% · MRVL 29.0%",
                   "Most frequent top sector Electronic equipment & semiconductors (SIC 36) (120 rebalances)",
                   "Selected losers, next week 52.0% rebounded · 48.0% fell further · 49.0% beat the control", "Mean excess when SPY rose / fell +0.12% (180) / -0.16% (141)",
                   "never a gate and not selection-edge evidence"]) {
    assert.ok(t.includes(s), s + " | " + t.slice(0, 900));
  }
});

test("2 a field the diagnostics lack is not shown; no diagnostics, no section", () => {
  const mom = { construction: { average_one_way_turnover: 0.21, annualized_one_way_turnover: 2.5 }, market_months: { ALL: { benchmark_up: { periods: 41, mean_excess: 0.0124 }, benchmark_down: { periods: 21, mean_excess: -0.0084 } } } };
  const t = text(PUI.validationHtml(DETAIL(mom)));
  assert.ok(t.includes("One-way turnover 21.0% per period · 2.5× a year") && t.includes("Mean excess when SPY rose / fell +1.24% (41) / -0.84% (21)"));
  assert.ok(!t.includes("Selected losers") && !t.includes("Gross excess consumed by friction") && !t.includes("Top five names"));
  assert.ok(!PUI.validationHtml(DETAIL(null)).includes('data-pui-sv-section="research"') && !PUI.validationHtml(DETAIL({ note: "x" })).includes('data-pui-sv-research'));
});

test("3 names are escaped", () => {
  const h = PUI.validationHtml(DETAIL(MR_DIAG));
  assert.ok(!h.includes("<b>X</b>") && h.includes("&lt;b&gt;X&lt;/b&gt;"));
});
