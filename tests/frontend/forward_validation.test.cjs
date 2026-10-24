// Node tests for the Stage 6.1 Forward Paper Validation panel and page (frontend/forward_validation.js). Compact beginner numbers (sessions 7 / 30,
// healthy, closed trades, duplicates, reconciliation issues, paper P/L, strategy status), INSUFFICIENT FOR STRATEGY CONCLUSION until the sample is
// meaningful, NEEDS ATTENTION on a critical condition, advanced metrics under View details only, operational and strategy blocks kept apart on the page.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const FV = require("../../frontend/forward_validation.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const JS = fs.readFileSync(path.join(FRONT, "forward_validation.js"), "utf8");
const HTML = fs.readFileSync(path.join(FRONT, "index.html"), "utf8");
const PAGE = fs.readFileSync(path.join(FRONT, "forward_validation.html"), "utf8");
const CSS = fs.readFileSync(path.join(FRONT, "forward_validation.css"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const words = /\b(buy|sell|buy now|sell now|enter now|trade now|you should|approved|execute|recommend\w*|profitable|winning|guaranteed|high confidence)\b/i;

const BASE = { sessions: { completed: 7, healthy: 7, degraded: 0, failed: 0, not_run: 0, in_progress: 1, target: 30, label: "7 / 30" },
               events: { total: 12, target: 100 },
               operational: { duplicate_broker_orders: 0, reconciliation_attention: 0, reconciliation_open: 0, broker_rejections: 1, partial_fills: 0, broker_errors: 2, refresh_failures: 1, safety_fires: 3,
                              restarts: 1, live_host_attempts: 0, early_stops: 0, status: "READY" },
               strategy: { closed_trades: 3, win_count: 2, loss_count: 1, flat_count: 0, gross_pnl: "41.20", win_rate: "66.67", average_win: "30.10", average_loss: "19.00", expectancy: "13.73",
                           edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", insufficient: true, insufficient_text: "INSUFFICIENT FOR STRATEGY CONCLUSION",
                           drawdown: { max_drawdown: "120.00", max_drawdown_pct: "0.12" }, exit_reasons: { HARD_INVALIDATION: 2, TIME_EXIT: 1 }, funnel: { WATCH: 5, READY: 4 }, conversion: { watch_to_ready: "80.00" },
                           unrealized: { unrealized_pnl: "-3.00" } },
               attention: [], needs_attention: false, loop: { running: true, interval_s: 60 }, disclaimer: "Descriptive forward-paper statistics; not a forecast." };

test("1 the compact panel shows the seven beginner numbers and the insufficient-data warning", () => {
  const h = FV.compactHtml(BASE, {});
  const t = text(h);
  for (const s of ["Sessions 7 / 30", "Healthy 7", "Closed trades 3", "Duplicates 0", "Reconciliation issues 0", "Paper P/L +41.20", "Strategy status ⚪ Insufficient data"]) assert.ok(t.includes(s), s);
  assert.ok(h.includes('data-fv-insufficient') && t.includes("INSUFFICIENT FOR STRATEGY CONCLUSION"));
  assert.ok(h.includes('data-fv-state="OK"') && t.includes("Validation in progress"));
  assert.ok(!words.test(t), t);
});

test("2 advanced metrics only under View details; the details link to the separate page", () => {
  const closed = FV.compactHtml(BASE, {});
  const visible = text(closed.split("data-fv-details")[0]);
  assert.ok(!visible.includes("Expectancy") && !visible.includes("Win rate") && !visible.includes("Max drawdown"));
  assert.ok(/class="tv-bot-details tv-hidden" data-fv-details/.test(closed));
  const open = text(FV.compactHtml(BASE, { detailsOpen: true }));
  for (const s of ["Win / loss / flat 2 / 1 / 0", "Win rate 66.67 %", "Expectancy per trade (descriptive) +13.73", "Max drawdown (adjusted equity) +120.00 (0.12 %)", "Safety fires 3", "Restarts observed 1",
                   "Operational status READY", "Setup events (READY + reviewable) 12 / 100", "Observer running · every 60 s in session"]) assert.ok(open.includes(s), s);
  assert.ok(FV.compactHtml(BASE, { detailsOpen: true }).includes('href="forward_validation.html"'));
});

test("3 a critical condition reads NEEDS ATTENTION and is never hidden", () => {
  const d = { ...BASE, attention: [{ code: "DUPLICATE_BROKER_ORDER", critical: true, text: "a broker order was duplicated", count: 1 }], needs_attention: true };
  const h = FV.compactHtml(d, {});
  assert.ok(h.includes('data-fv-state="ATTENTION"') && text(h).includes("NEEDS ATTENTION") && text(h).includes("critical: duplicate broker order"));
  const w = FV.compactHtml({ ...BASE, attention: [{ code: "FAILED_EXECUTION", critical: false, count: 1 }], needs_attention: true }, {});
  assert.ok(w.includes('data-fv-state="WARN"') && text(w).includes("Needs a look"));
  assert.equal(FV.headline(FV.summaryModel({ ...BASE, sessions: { ...BASE.sessions, completed: 30 } })).text, "Validation target reached");
});

test("4 the strategy status is never a profitability word; a positive early P/L still reads insufficient", () => {
  const rich = { ...BASE, strategy: { ...BASE.strategy, gross_pnl: "8000.00", closed_trades: 2, win_count: 2, insufficient: true, edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data" } };
  const t = text(FV.compactHtml(rich, { detailsOpen: true }));
  assert.ok(t.includes("Paper P/L +8000.00") && t.includes("INSUFFICIENT FOR STRATEGY CONCLUSION") && !words.test(t), t);
  const m = FV.summaryModel(rich);
  assert.equal(m.edge, "INSUFFICIENT_DATA");
  const pos = FV.summaryModel({ ...BASE, strategy: { ...BASE.strategy, insufficient: false, edge_status: "EARLY_POSITIVE", edge_label: "Early positive" } });
  assert.deepEqual([pos.insufficient, pos.edgeLabel, pos.edgeIcon], [false, "Early positive", "🟢"]);
});

test("5 the page keeps operational and strategy blocks apart, lists sessions, closed trades, daily summaries, reports and exports", () => {
  const sessions = [{ trading_date: "2026-10-21", session_status: "HEALTHY", market_open_seen: true, market_close_seen: true, paper_auto_enabled: true, account_refresh_healthy: true, cash_flow_healthy: true,
                      reconciliation_healthy: true, in_session_samples: 390, notes: [] }, { trading_date: "2026-10-22", session_status: "NOT_RUN", notes: ["NO_OBSERVATION"] }];
  const closed = [{ position_id: "p1", closed_at: "2026-10-21T19:00:00+00:00", symbol: "SNDK", qty: 12, entry_price: "80.10", exit_price: "82.00", realized_pnl: "22.80", return_pct: "2.37", holding_sessions: 1,
                    exit_reason: "DISLOCATION_NORMALIZED", entry_slippage: { slippage_bps: "4.00" }, exit_slippage: { slippage_bps: "-2.00" }, excursions: { mfe_pnl: "30.00", mae_pnl: "-6.00" } }];
  const dailies = [{ trading_date: "2026-10-21", session_status: "HEALTHY", funnel: { READY: 1, REVIEWABLE: 1, ENTRY_SUBMITTED: 1, ENTRY_FILLED: 1, CLOSED: 1 }, duplicates: 0, reconciliation_issues: 0, safety_fires: 0, realized_pnl: "22.80" }];
  const h = FV.pageHtml(BASE, sessions, closed, dailies, { daily: "FORWARD PAPER VALIDATION - DAILY REPORT", weekly: "Not yet available" });
  assert.ok(h.indexOf("data-fv-operational") < h.indexOf("data-fv-strategy"));
  const op = text(h.split("data-fv-strategy")[0].split("data-fv-operational")[1]);
  assert.ok(op.includes("Duplicate broker orders 0") && op.includes("Operational status READY") && !op.includes("Win rate"));
  const st = text(h.split("data-fv-strategy")[1].split("Funnel")[0]);
  assert.ok(st.includes("INSUFFICIENT FOR STRATEGY CONCLUSION") && st.includes("Win rate 66.67 %") && !st.includes("Duplicate broker orders"));
  assert.ok(h.includes('data-session="2026-10-21"') && text(h).includes("2026-10-22 ⚪ not run") && h.includes('data-closed="p1"') && text(h).includes("dislocation normalized") && h.includes('data-daily="2026-10-21"'));
  assert.ok(h.includes("data-fv-report-daily") && h.includes("data-fv-report-weekly") && h.includes('data-fv-export="trades"') && h.includes("/api/forward-validation/export/sessions"));
  assert.ok(!words.test(text(h)));
});

test("6 empty states and the wiring: index.html loads the script and the stylesheet inside the Stage 6.1 markers; the page is standalone", () => {
  const t = text(FV.compactHtml(null, {}));
  assert.ok(t.includes("Checking forward validation") && !t.includes("Sessions"));
  const e = FV.pageHtml(null, [], [], [], {});
  assert.ok(text(e).includes("No session recorded yet.") && text(e).includes("No closed paper trade yet.") && text(e).includes("No daily summary yet."));
  assert.ok(HTML.includes('<script src="forward_validation.js"></script>') && HTML.includes('<link rel="stylesheet" href="forward_validation.css" />'));
  assert.ok(HTML.indexOf("<!-- >>> Stage 6.1") < HTML.indexOf('href="forward_validation.css"') && HTML.indexOf("paper_bot.js") < HTML.indexOf('src="forward_validation.js"'));
  assert.ok(PAGE.includes('id="fv-page"') && PAGE.includes('src="forward_validation.js"') && PAGE.includes('href="forward_validation.css"'));
  assert.ok(CSS.includes(".tv-panel-fv") && CSS.includes(".fv-table") && CSS.includes(".fv-warn"));
  assert.ok(JS.includes('"/api/forward-validation"') && JS.includes("tv-panel-bot") && !/fetch\([^)]*method:\s*"POST"/.test(JS) && !JS.includes("/api/execution/auto"));
  assert.equal(FV.POLL_MS, 60000);
});
