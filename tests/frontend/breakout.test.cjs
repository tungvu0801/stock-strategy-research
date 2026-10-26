// Node tests for the Stage 6.7 Breakout fields of the Strategy Lab research section (frontend/product_ui.js): the invested fraction and the weeks by
// number of positions, the beta to SPY, the excess on invested capital, follow-through, failed breakouts, the cash part of the excess and the
// Momentum overlap - shown only when the diagnostics carry them (Mean Reversion / Momentum unchanged), escaped, and labelled as never a gate.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const seg = (n, net) => ({ trade_count: n, candidate_count: n, accepted_count: n, stand_aside_count: 0, win_rate: 0.5, gross_pnl: net + 5, slippage_cost: 5, fees: 0, friction_total: 5,
                           net_pnl: net, mean_R: 0.002, average_R_gross: 0.003, mean_R_t_stat: 1.1, expectancy_usd: 10, profit_factor: 1.05, max_drawdown: 9000 });
const csSeg = (n, t) => ({ period_count: n, strategy_total_return: 0.2, control_total_return: 0.31, benchmark_total_return: 0.4, mean_excess_period_return: -0.0005, excess_t_stat: t,
                           excess_win_rate: 0.45, active_volatility: 0.06, worst_relative_period: { rebalance: "2025-04-07", excess: -0.03 }, max_relative_drawdown: 0.08,
                           cumulative_excess_return: -0.11, strategy_turnover: 0.6, control_turnover: 0.03, concentration: { applicable: false, concentrated: false, text: "n/a" } });
const CS = { unit: "one observation per rebalance period", control: { control_id: "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", label: "Equal-weight eligible universe", role: "SELECTION_CONTROL" },
             market_benchmark: { symbol: "SPY", role: "MARKET_BENCHMARK" }, excluded_periods: { BOUNDARY: 1 }, parity: { status: "PASS", note: "control parity: 8 / 8 checks pass" },
             segments: { TRAIN: csSeg(223, -0.4), OOS: csSeg(98, -0.7), ALL: csSeg(321, -0.8) } };
const ABS = { verdict: "PASS", label: "PASS", reasons: [], warnings: [], decision: "Eligible for review", eligible_for_review: true };
const OVERALL = { verdict: "FAIL", label: "FAIL", decision: "Not ready for paper", eligible_for_review: false, reasons: [{ code: "SELECTION_EDGE_NOT_ESTABLISHED", text: "No evidence." }], warnings: [] };
const BO_DIAG = { construction: { average_one_way_turnover: 0.24, annualized_one_way_turnover: 12.5 },
                  exposure: { ALL: { mean_invested_fraction: 0.41, median_invested_fraction: 0.35, min_invested_fraction: 0.0, max_invested_fraction: 1.0, mean_positions: 8.2,
                                     weeks_by_positions: { "0": 12, "1-5": 90, "6-10": 110, "11-19": 80, "20": 29 } } },
                  market_sensitivity: { ALL: { strategy_beta_to_spy: 0.52, control_beta_to_spy: 1.01 } },
                  invested_capital: { OOS: { weeks_with_a_position: 93, mean_excess_on_invested_capital: 0.0007, t_stat: 0.41 } },
                  follow_through: { legs: 2600, continued_higher: 0.38, flat: 0.25, failed: 0.37 },
                  failed_breakouts: { legs: 2600, share_ending_below_entry_price: 0.46, share_ending_below_breakout_reference: 0.31 },
                  concentration: { excess_contribution: { cash: -0.31, names: 0.05, top: [["<i>Z</i>", 0.02]] } },
                  momentum_overlap: { weeks: 270, share_of_breakout_holdings_in_momentum_portfolio: 0.44, weekly_excess_correlation: 0.37 } };
const RUN = (diag) => ({ run_id: "7".repeat(32), created_at: "2026-10-26T09:00:00+00:00", strategy_version: "1.0", dataset_kind: "HISTORICAL", dataset_start: "2020-07-27", dataset_end: "2026-10-23",
  train_start: "2020-07-27", train_end: "2024-12-05", oos_start: "2024-12-06", oos_end: "2026-10-23", oos_sessions: 471, candidate_count: 2700, verdict: OVERALL,
  integrity: { same_candidate_set: true }, friction_model: { slippage_bps_per_side: 5 }, causality: { automated_checks: [] }, audit: { status: "PASS", note: "all trades validated" },
  diagnostics: { strategy: diag },
  arms: { RULES: { status: "EVALUATED", label: "PASS", candidate_count: 2700, TRAIN: seg(1900, 100), OOS: seg(800, 50), ALL: seg(2700, 150) },
          GATED: { status: "EVALUATED", label: "PASS", candidate_count: 2700, TRAIN: seg(1900, 100), OOS: seg(800, 50), ALL: seg(2700, 150) },
          MODEL: { status: "NOT_EVALUATED", label: "Not evaluated", candidate_count: 2700 } } });
const DETAIL = (diag) => ({ validation_status: "FAIL", validation_label: "FAIL", latest_validation_run_id: "7".repeat(32), evaluation_mode: "CROSS_SECTIONAL_PORTFOLIO",
  performance_verdict: "PASS", selection_edge_status: "NOT_ESTABLISHED", selection_edge_label: "Not established", universe_integrity_status: "UNTRUSTED", research_flags: ["SURVIVORSHIP_BIAS_PRESENT"],
  policy: {}, disclaimer: "Backtest results are hypothetical.", run: RUN(diag), overall_verdict: OVERALL, absolute_verdict: ABS,
  selection_edge: { status: "NOT_ESTABLISHED", label: "Not established", reasons: [] }, cross_sectional: CS, methodology_evaluation: null,
  universe_integrity: { status: "UNTRUSTED", method: "FIXED_CURRENT_UNIVERSE", survivorship_bias: "PRESENT", includes_delisted: false, point_in_time: false, raw_edge_status: "NOT_ESTABLISHED",
                        final_edge_status: "NOT_ESTABLISHED", source: "run", consequence: "cannot establish alpha", declaration: null } });

test("1 a Breakout run shows exposure, beta, the invested-capital excess, follow-through, failed breakouts, the cash part and the Momentum overlap", () => {
  const h = PUI.validationHtml(DETAIL(BO_DIAG));
  const t = text(h.slice(h.indexOf('data-pui-sv-section="research"')));
  for (const s of ["Exposure, follow-through, turnover and concentration", "Invested fraction (mean / median / min / max) 41.0% / 35.0% / 0.0% / 100.0% · 8.2 positions on average",
                   "Weeks by number of positions 0: 12 · 1-5: 90 · 6-10: 110 · 11-19: 80 · 20: 29", "Beta to SPY (strategy / control) 0.52 / 1.01",
                   "Excess on invested capital (OOS) +0.07% a week, t 0.41 over 93 weeks with a position", "Follow-through (next week) 38.0% continued higher · 25.0% flat · 37.0% failed",
                   "Failed breakouts 46.0% ended below the entry price · 31.0% below the breakout reference", "Cash part of the summed excess -31.00% (names +5.00%)",
                   "Overlap with the Momentum portfolio 44.0% of holdings · weekly excess correlation 0.37", "One-way turnover 24.0% per period · 12.5× a year",
                   "never a gate and not selection-edge evidence"]) {
    assert.ok(t.includes(s), s + " | " + t.slice(0, 1200));
  }
});

test("2 without exposure the section keeps its earlier title and no Breakout field is invented", () => {
  const t = text(PUI.validationHtml(DETAIL({ construction: { average_one_way_turnover: 0.71, annualized_one_way_turnover: 37.2 } })));
  assert.ok(t.includes("Turnover, friction and concentration") && !t.includes("Invested fraction") && !t.includes("Follow-through") && !t.includes("Momentum portfolio"));
  const none = PUI.validationHtml(DETAIL({ exposure: { ALL: { mean_invested_fraction: 0, median_invested_fraction: 0, min_invested_fraction: 0, max_invested_fraction: 0, mean_positions: 0,
                                                              weeks_by_positions: { "0": 5 } } }, invested_capital: { OOS: { weeks_with_a_position: 0 } } }));
  assert.ok(text(none).includes("Excess on invested capital (OOS) no week with a position"));
});

test("3 names are escaped", () => {
  const h = PUI.validationHtml(DETAIL({ ...BO_DIAG, concentration: { excess_contribution: { top: [["<i>Z</i>", 0.02]] } } }));
  assert.ok(!h.includes("<i>Z</i>") && h.includes("&lt;i&gt;Z&lt;/i&gt;"));
});
