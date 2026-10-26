// Node tests for the Stage 6.4 additions to the Strategy Lab Validation block (frontend/product_ui.js): the research flags shown with the verdict
// (SURVIVORSHIP_BIAS_PRESENT, SELECTION_EDGE_NOT_ESTABLISHED - displayed, never suppressed), the portfolio research table of a cross-sectional
// strategy (strategy vs SPY vs the equal-weight universe control, the per-rebalance-period t), its absence when a strategy has no portfolio view
// (nothing invented), escaping, and the read-only wiring (no control, no form).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const seg = (n, net) => ({ trade_count: n, candidate_count: n, accepted_count: n, stand_aside_count: 0, win_rate: 0.6, gross_pnl: net + 5, slippage_cost: 5, fees: 0, friction_total: 5,
                           net_pnl: net, average_R_gross: 0.02, mean_R: 0.017, mean_R_t_stat: 6.3, expectancy_usd: 83.7, profit_factor: 2.1, max_drawdown: 18923, max_drawdown_pct: 18.9,
                           sharpe_per_trade: 0.2, average_holding_bars: 21, ambiguous_bars_count: 0, configured_risk_total: 5000, achieved_risk_total: 4990, simulation_skipped_count: 0 });
const perf = (tr, sh, dd) => ({ total_return: tr, sharpe: sh, max_drawdown: dd, annualized_volatility: 0.18, sessions: 300 });
const PORTFOLIO = {
  TRAIN: { strategy: perf(1.199, 1.14, 0.221), benchmark: perf(0.831, 0.94, 0.245), universe_equal_weight: perf(1.212, 1.16, 0.232), excess_total_return: 0.368,
           excess_vs_universe: -0.013, excess_vs_universe_per_period: { periods: 52, mean: -0.00008, t_stat: -0.03, positive_share: 0.44 } },
  OOS: { strategy: perf(0.537, 1.48, 0.176), benchmark: perf(0.281, 0.9, 0.187), universe_equal_weight: perf(0.447, 1.3, 0.187), excess_total_return: 0.256,
         excess_vs_universe: 0.09, excess_vs_universe_per_period: { periods: 23, mean: 0.003, t_stat: 0.51, positive_share: 0.48 } },
  ALL: { strategy: perf(2.38, 1.24, 0.221), benchmark: perf(1.346, 0.92, 0.245), universe_equal_weight: perf(2.202, 1.22, 0.232), excess_total_return: 1.034,
         excess_vs_universe: 0.178, excess_vs_universe_per_period: { periods: 74, mean: 0.001, t_stat: 0.36, positive_share: 0.46 } },
};
const RUN = (strategyDiag) => ({ run_id: "svr_vq", created_at: "2026-10-25T00:00:00Z", strategy_version: "1.0", dataset_id: "sha256:vq", dataset_kind: "HISTORICAL", dataset_start: "2020-07-31",
  dataset_end: "2026-10-23", train_start: "2020-07-31", train_end: "2024-12-05", oos_start: "2024-12-06", oos_end: "2026-10-23", oos_sessions: 471, candidate_count: 1480,
  candidate_set_hash: "abc", candidates_skipped: 0, integrity: { same_candidate_set: true, problems: [], invalid_arms: [] }, friction_model: { slippage_bps_per_side: 5 },
  execution_assumptions: { entry_policy: "NEXT_BAR_OPEN" }, causality: { automated_checks: [], critical: false, manual_review_required: true, manual_review_note: "" },
  audit: { status: "PASS", checked: 2920, validated: 2920, note: "2920 / 2920 trades validated independently", violations: 0, first_violations: [] },
  diagnostics: { train_vs_oos: { code: "CONSISTENT", text: "consistent" }, sensitivity: { label: "ROBUST", note: "" }, ...(strategyDiag ? { strategy: strategyDiag } : {}) },
  verdict: { verdict: "PASS", label: "PASS", reasons: [{ code: "R", text: "reason" }], warnings: [], decision: "Eligible for review", eligible_for_review: true, note: "", lifecycle_effect: "none" },
  arms: { RULES: { status: "EVALUATED", label: "PASS", candidate_count: 1480, TRAIN: seg(1020, 85409), OOS: seg(440, 45538), ALL: seg(1460, 130947) },
          GATED: { status: "EVALUATED", label: "PASS", note: "NO_ADDITIONAL_GATES", candidate_count: 1480, TRAIN: seg(1020, 85409), OOS: seg(440, 45538), ALL: seg(1460, 130947) },
          MODEL: { status: "NOT_EVALUATED", label: "Not evaluated", note: "", candidate_count: 1480, TRAIN: null, OOS: null, ALL: null } },
  validation_version: "validation_v2", git_commit_sha: "abc" });
const DETAIL = (run, flags) => ({ validation_status: "PASS", validation_label: "PASS", not_run_reason: "", latest_validation_run_id: run.run_id, validation_verdict: "PASS", train_trade_count: 1020,
  oos_trade_count: 440, mechanical_audit_status: "PASS", last_validated_at: run.created_at, validation_synthetic: false, research_flags: flags, policy: {}, disclaimer: "Backtest results are hypothetical.", run });

test("1 research flags: every flag is shown with the verdict as a badge (never suppressed); none -> no flag row", () => {
  const h = PUI.validationHtml(DETAIL(RUN(null), ["SELECTION_EDGE_NOT_ESTABLISHED", "SURVIVORSHIP_BIAS_PRESENT"]));
  const flags = [...h.matchAll(/data-pui-sv-flag="([A-Z_]+)">([A-Z_]+)</g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(flags, [["SELECTION_EDGE_NOT_ESTABLISHED", "SELECTION_EDGE_NOT_ESTABLISHED"], ["SURVIVORSHIP_BIAS_PRESENT", "SURVIVORSHIP_BIAS_PRESENT"]]);
  assert.ok(h.includes("data-pui-sv-flags") && h.indexOf("data-pui-sv-flags") > h.indexOf("Lifecycle: unchanged by validation"));
  assert.ok(!PUI.validationHtml(DETAIL(RUN(null), [])).includes("data-pui-sv-flags") && !PUI.validationHtml(DETAIL(RUN(null), undefined)).includes("data-pui-sv-flags"));
});

test("2 portfolio table: strategy / SPY / universe total returns, excess vs both, the per-period t, Sharpe and drawdown, per TRAIN / OOS / All", () => {
  const h = PUI.validationHtml(DETAIL(RUN({ portfolio: PORTFOLIO, note: "research diagnostics: never a gate, never used to choose a rule" }), ["SURVIVORSHIP_BIAS_PRESENT"]));
  assert.ok(h.includes("data-pui-sv-portfolio") && h.includes("<th>Equal-weight portfolio</th><th>TRAIN</th><th>OOS</th><th>All</th>"));
  const t = text(h);
  assert.ok(t.includes("Strategy total return 119.9% 53.7% 238.0%") && t.includes("Market benchmark (SPY) total return 83.1% 28.1% 134.6%"), t);
  assert.ok(t.includes("Selection control (equal-weight universe) total return 121.2% 44.7% 220.2%") && t.includes("Excess vs the market benchmark (SPY) 36.8% 25.6% 103.4%"));
  assert.ok(t.includes("Excess vs the selection control -1.3% 9.0% 17.8%") && t.includes("Excess vs universe: t per rebalance period -0.03 0.51 0.36"));
  assert.ok(t.includes("Max drawdown (strategy / SPY) 22.1% / 24.5% 17.6% / 18.7% 22.1% / 24.5%"));
  assert.ok(t.includes("The universe control shares the universe's survivorship bias; the excess over it is what the selection added."));
  assert.ok(h.indexOf("data-pui-sv-metrics") < h.indexOf("data-pui-sv-portfolio") && h.indexOf("data-pui-sv-portfolio") < h.indexOf("data-pui-sv-arms"));
});

test("3 no portfolio view (e.g. a per-trade strategy): no table, nothing invented; missing values read as a dash", () => {
  assert.ok(!PUI.validationHtml(DETAIL(RUN(null), [])).includes("data-pui-sv-portfolio"));
  assert.ok(!PUI.validationHtml(DETAIL(RUN({ note: "x" }), [])).includes("data-pui-sv-portfolio"));
  const h = PUI.validationHtml(DETAIL(RUN({ portfolio: { TRAIN: { strategy: perf(0.1, null, 0.05) } }, note: "n" }), []));
  assert.ok(text(h).includes("Strategy total return 10.0% — —") && text(h).includes("Market benchmark (SPY) total return — — —"));
});

test("4 escaping and read-only: flags and notes are escaped; the block holds no control and no form", () => {
  const h = PUI.validationHtml(DETAIL(RUN({ portfolio: PORTFOLIO, note: "<img src=x onerror=alert(1)>" }), ['<i>"x"</i>']));
  assert.ok(!h.includes("<img") && !h.includes("<i>") && h.includes("&lt;img") && h.includes("&lt;i&gt;"));
  assert.ok(!/<(button|form|input|select|textarea)\b/.test(h));
});
