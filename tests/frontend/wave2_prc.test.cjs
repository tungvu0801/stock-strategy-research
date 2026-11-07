// Node tests for Wave 2 PR C in the browser (design file 31 §12, §13, C-70…C-89; test plan file 32 §3.9 T-P01…T-P05, T-P09, T-P12, T-P13, T-P19, T-P20 and §3.10
// N-01…N-11). C-89: the browser only formats what the server decided - the verified-only day total (today_summary), the route decoration of every executor row
// (pnl_status / pnl_display / pnl_kind), the run block, the all-time block and the promotion view. Never a browser sum, never a verification decision.
// The §3.9 fixture: one day with three closed trades (verified +10.00; unverified with a raw +20.00; unverified with a NULL P&L) and one open position whose entry
// is the unpriced "0.00" placeholder (its raw card P/L is last x qty = 186.30, which must never be rendered).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const FV = require("../../frontend/forward_validation.js");                      // also registers globalThis.ForwardValidation (product_ui's Validation block)
const PUI = require("../../frontend/product_ui.js");
const P = require("../../frontend/paper_bot.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const FV_JS = fs.readFileSync(path.join(FRONT, "forward_validation.js"), "utf8");
const PUI_JS = fs.readFileSync(path.join(FRONT, "product_ui.js"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const cells = (row) => [...String(row).matchAll(/<td>([\s\S]*?)<\/td>/g)].map((m) => text(m[1]));
const WORDS = /\b(approved|execute|recommend\w*|profitable|winning|guaranteed|high confidence|buy now|sell now|you should)\b/i;
const NUM_PL = /P\/L [-+]?\d/;                                                 // a P/L rendered as a number
const amount = (t, v) => new RegExp(`(^|[^0-9.])[-+]?${v.replace(/\./g, "\\.")}(?![0-9])`).test(String(t));   // the exact amount v (so "20.00" is not found inside "120.00")

// ---------------------------------------------------------------- the §3.9 fixture -------------------------------------------------------------
const DAY = "2026-10-21";
const LIM = { max_hold_sessions: 5 };
const V = { position_id: "pv", symbol: "AAPL", state: "CLOSED", opened_at: "2026-10-21T14:00:00Z", closed_at: "2026-10-21T15:00:00Z", realized_pnl: "10.00", entry_qty: 1, quantity: 0,
            average_entry_price: "100.00", average_exit_price: "110.00", exit_reason: "TIME_EXIT", sessions_held: 1, pnl_status: "VERIFIED", pnl_display: "10.00", pnl_kind: "realized" };
const U20 = { position_id: "pu", symbol: "MSFT", state: "CLOSED", opened_at: "2026-10-21T14:10:00Z", closed_at: "2026-10-21T16:00:00Z", realized_pnl: "20.00", entry_qty: 2, quantity: 0,
              average_entry_price: "50.00", average_exit_price: "60.00", exit_reason: "TIME_EXIT", sessions_held: 1, pnl_status: "UNVERIFIED", pnl_display: null, pnl_kind: "realized" };
const UNULL = { position_id: "pn", symbol: "NVDA", state: "CLOSED", opened_at: "2026-10-21T14:20:00Z", closed_at: "2026-10-21T17:00:00Z", realized_pnl: null, entry_qty: 1, quantity: 0,
                average_entry_price: "70.00", average_exit_price: "75.00", exit_reason: "HARD_INVALIDATION", sessions_held: 1, pnl_status: "UNVERIFIED", pnl_display: null, pnl_kind: "realized" };
const OPEN = { position_id: "po", symbol: "SNDK", state: "HEALTHY", quantity: 3, average_entry_price: "0.00", last_price: "62.10", opened_at: "2026-10-21T14:30:00Z", sessions_held: 1,
               realized_pnl: null, pnl_status: "UNKNOWN", pnl_display: null, pnl_kind: "unrealized" };
const OPEN_CARD = { key: "IN_TRADE", symbol: "SNDK", icon: "🟢", label: "In trade", line: "3 shares · entry 0.00 · now 62.10 · 186.30", shares: 3, entry: "0.00", current: "62.10", pnl: "186.30",
                    pnl_pct: null, position_id: "po", execution_id: "e-po", state: "POSITION_OPEN", setup_state: "READY", since: "2026-10-21T14:30:00Z", reason: null,
                    details: { position_state: "HEALTHY", sessions_held: 1, events: [] }, pnl_status: "UNKNOWN", pnl_display: null, pnl_kind: "unrealized" };
const closedEvent = (p, at) => ({ position_id: p.position_id, kind: "CLOSED", at, detail: { realized_pnl: p.realized_pnl }, pnl_status: p.pnl_status, pnl_display: p.pnl_display, pnl_kind: "realized" });
const EVENTS = [{ position_id: "po", kind: "OPENED", at: "2026-10-21T14:30:00Z", reason: "3 of 3 filled" }, closedEvent(V, "2026-10-21T15:00:00Z"), closedEvent(U20, "2026-10-21T16:00:00Z"),
                closedEvent(UNULL, "2026-10-21T17:00:00Z")];
const SRC = { transitions: [], proposals: [], intents: [], positions: [V, U20, UNULL, OPEN], position_events: EVENTS, safety_events: [], findings: [], fv_events: [] };
const EXEC = { mode: "MONITOR_ONLY", attention: false, attention_reasons: [], open_positions: 1, reconciliation_healthy: true, environment: { paper: true, simulated: true },
               scheduler: { market: { regular_session: true, session_date: DAY } }, account_data: { status: "FRESH", cash_flow: { status: "FRESH" } }, cards: [OPEN_CARD], recent: [] };
const SAFE = { state: "TRADING_ALLOWED", state_label: "Trading allowed", reason_text: "", kill_switch: { active: false }, no_new_entries: { active: false }, daily_loss: { status: "OK" } };

const RUN = { defined: true, run_id: "FR-0001", started_at: "2026-10-20T13:00:00+00:00", first_session_date: "2026-10-20", frozen_hash: "b5d4a56e0123456789abcdef0123456789abcdef0123456789abcdef01234567",
              cohort_id: "C-0003", classifier_version: "session_classifier_v2_1", approved_by: "operator", accepted_blockers: ["PF-18", "EX-13", "PF-08", "W2-N23", "W2-N03"],
              prior_invalid_days: [{ date: "2026-10-14", session_status: "FAILED", notes: ["DUPLICATE_BROKER_ORDER"], classifier_version: "session_classifier_v2" },
                                   { date: "2026-10-16", session_status: "DEGRADED", notes: ["EVIDENCE_UNREADABLE"], classifier_version: "session_classifier_v2" }],
              ended_at: null, end_reason: null, suspended: false, suspension: null, t_stop: "2026-10-21T20:00:00+00:00",
              readiness: { status: "READY", reasons: [], text: "Run FR-0001: READY" },
              sessions: { strategy_days: 2, target: 30, target_met: false, label: "2 / 30 strategy sessions (2 healthy · 0 degraded · 0 failed · 1 unreadable · 0 not run)", members: 2, pending: 0 },
              trades: { members: 3, verified: 1, unverified: 2, pending: 0, provisional: 0, exclusions: {} },
              strategy: { edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", reasons: ["1 closed trade(s) of the 20 needed", "2 unverified trade(s)"], closed_trades: 1, expectancy: "10.00",
                          win_rate: "100.00", gross_pnl: "10.00", pnl_label: "partial (2 unverified)", side_text: "pending 0 · provisional 0 · unverified 2" },
              drawdown: { status: "UNKNOWN", max_drawdown_pct: null, text: "price age unknown" }, headline: "Forward run FR-0001 since 2026-10-20" };
const NO_RUN = { defined: false, headline: "No forward run defined", readiness: { status: "NOT READY", text: "No forward run defined" }, sessions: { label: "— / 30", target_met: false, strategy_days: 0 },
                 strategy: { edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", reasons: ["no forward run"] }, drawdown: { status: "UNKNOWN", max_drawdown_pct: null, text: "no forward run" } };
const ALL_TIME_NR = { status: "NOT READY", invalid_days: ["2026-10-14", "2026-10-16"], text: "All-time record: NOT READY (2 invalid days: 2026-10-14, 2026-10-16)" };
const PROMO = { eligible: false, text: "promotion blocked: all-time record NOT READY (2026-10-14, 2026-10-16)" };
const COHORTS = [{ cohort_id: "C-0003", label: "cohort 3", started_at: "2026-10-20T13:00:00+00:00", forward_run: true, healthy_sessions: 2, healthy_target: 30, closed_trades: 1, expectancy: "10.00",
                   win_rate: "100.00", status: "INSUFFICIENT_DATA", label_text: "Insufficient data", reasons: ["1 closed trade(s) of the 20 needed"] },
                 { cohort_id: "C-0002", label: "cohort 2", started_at: "2026-10-01T13:00:00+00:00", forward_run: false, healthy_sessions: 9, healthy_target: 30, closed_trades: 5, expectancy: null,
                   win_rate: null, status: "INSUFFICIENT_DATA", label_text: "Insufficient data", reasons: ["not a forward run"] },
                 // a stray non-run row that still carries raw-P&L numbers and a positive label: the browser must not render any of them
                 { cohort_id: "C-0001", label: "cohort 1", started_at: "2026-09-01T13:00:00+00:00", forward_run: false, healthy_sessions: 30, healthy_target: 30, closed_trades: 40, expectancy: "55.00",
                   win_rate: "80.00", status: "EARLY_POSITIVE", label_text: "Early positive", reasons: [] }];
const OVERVIEW = { sessions: { completed: 2, healthy: 2, degraded: 1, failed: 1, not_run: 0, in_progress: 1, unreadable: 1, target: 30, target_met: false, label: "2 / 30" }, events: { total: 4, target: 100 },
                   operational: { duplicate_broker_orders: 0, reconciliation_attention: 0, reconciliation_open: 0, status: "NOT READY" },
                   strategy: { closed_trades: 1, unverified: 2, win_count: 1, loss_count: 0, flat_count: 0, gross_pnl: "10.00", win_rate: "100.00", average_win: "10.00", average_loss: null, expectancy: "10.00",
                               edge_status: "INSUFFICIENT_DATA", edge_label: "Insufficient data", insufficient: true, insufficient_text: "INSUFFICIENT FOR STRATEGY CONCLUSION", reasons: ["not a forward run"],
                               drawdown: { max_drawdown: "120.00", max_drawdown_pct: "0.12" }, drawdown_basis: "account curve (informational; never used by the edge rule)",
                               unrealized: { open_positions: 1, unrealized_pnl: null, positions: [{ symbol: "SNDK", quantity: 3, entry: "0.00", last_price: "62.10", unrealized_pnl: null, state: "HEALTHY" }] },
                               exit_reasons: { TIME_EXIT: 2, HARD_INVALIDATION: 1 }, funnel: {}, conversion: {} },
                   attention: [], needs_attention: false, today: DAY, today_summary: { trading_date: DAY, realized_pnl: "10.00", pnl_status: "UNVERIFIED_PRESENT", unverified: 2, closed_trades: 3 },
                   run: RUN, all_time: ALL_TIME_NR, promotion: PROMO, forward_evidence: { cohorts: COHORTS }, loop: {}, disclaimer: "Descriptive forward-paper statistics; not a forecast." };
const READY_AT_TARGET = { ...OVERVIEW, run: { ...RUN, sessions: { ...RUN.sessions, strategy_days: 30, target_met: true } } };

// ---------------------------------------------------------------- §3.9: verified-only P&L on every surface ---------------------------------------
test("T-P01 / N-05 product_ui todaySummary: the day total is the server's today_summary, '+10.00 · partial (2 unverified)'; never 30.00, never 10.00 unlabelled, NULL never 0", () => {
  const s = PUI.todaySummary(SRC, EXEC, DAY, OVERVIEW);
  assert.deepEqual(s, { trades: 0, open: 1, closed: 3, pnl: "+10.00 · partial (2 unverified)", issues: 0 });
  const ts = (o) => ({ ...OVERVIEW, today_summary: { ...OVERVIEW.today_summary, ...o } });
  const pnl = (fv, src) => PUI.todaySummary(src || SRC, EXEC, DAY, fv).pnl;
  assert.equal(pnl(ts({ pnl_status: "OK", unverified: 0 })), "+10.00");
  assert.equal(pnl(ts({ realized_pnl: null })), "not available (2 unverified)");                   // none verified: never 0
  assert.equal(pnl(ts({ pnl_status: "UNSTABLE", realized_pnl: null })), "not available");
  assert.equal(pnl(ts({ pnl_status: "NO_TRADES", realized_pnl: null, unverified: 0, closed_trades: 0 }), {}), "—");
  assert.equal(pnl(ts({ pnl_status: "SOMETHING_NEW" })), "not available");                         // an unknown status is never a number
  assert.equal(pnl(null), "not available");                                                         // no server summary: never a browser sum of the rows (10 + 20 + null)
  assert.equal(pnl({ ...OVERVIEW, today_summary: undefined }), "not available");
  assert.equal(pnl(ts({ trading_date: "2026-10-20" })), "not available");                          // another date's summary is never shown as today's
  assert.equal(pnl(null, {}), "—");
  for (const v of [s.pnl, pnl(null), pnl(ts({ realized_pnl: null }))]) assert.ok(!amount(v, "30.00") && !/^[-+]?10\.00$/.test(v) && !amount(v, "0.00"), v);
  assert.equal(OVERVIEW.today, DAY);                                                                // the `today` key stays the ISO date ([Q43-A])
});

test("T-P02 / N-05 product_ui dailyReview / activityHtml: the Daily Review 'Paper P/L' is the same server day total", () => {
  const dr = PUI.dailyReview(SRC, EXEC, SAFE, OVERVIEW, DAY);
  assert.deepEqual([dr.pnl, dr.closed], ["+10.00 · partial (2 unverified)", 3]);
  assert.equal(PUI.dailyReview(SRC, EXEC, SAFE, { ...OVERVIEW, today_summary: undefined }, DAY).pnl, "not available");
  assert.equal(PUI.dailyReview(SRC, EXEC, SAFE, null, DAY).pnl, "not available");
  const h = PUI.activityHtml(SRC, EXEC, OVERVIEW, DAY, { safety: SAFE });
  const card = h.match(/<h4>Paper P\/L<\/h4><div class="pui-big[^"]*">([^<]*)<\/div>/);
  assert.ok(card, "the Paper P/L card");
  assert.equal(text(card[1]), "+10.00 · partial (2 unverified)");
  assert.ok(!amount(text(h), "30.00") && !amount(text(h), "20.00") && !amount(text(h), "186.30"));
});

test("T-P03 / N-06 product_ui positionLifecycle / closedHtml: '+10.00' for the verified trade, 'P/L unverified' for the other two; undecorated is 'P/L unknown'", () => {
  const L = (p) => PUI.positionLifecycle(p, { key: "CLOSED", symbol: p.symbol }, [], LIM, null, []);
  assert.deepEqual([V, U20, UNULL].map((p) => [L(p).stage, L(p).pnl]), [["CLOSED", "+10.00"], ["CLOSED", "P/L unverified"], ["CLOSED", "P/L unverified"]]);
  assert.equal(L({ ...V, realized_pnl: "99.99" }).pnl, "+10.00");                                  // the decorated value is shown; the raw field is never rendered
  assert.equal(L({ ...U20, pnl_status: undefined, pnl_display: undefined, pnl_kind: undefined }).pnl, "P/L unknown");   // an undecorated row: never the raw 20.00
  assert.equal(L({ ...V, pnl_status: "UNKNOWN", pnl_display: null }).pnl, "P/L unknown");
  assert.equal(PUI.positionLifecycle(null, { key: "CLOSED", symbol: "MSFT", pnl: "20.00", pnl_status: "UNVERIFIED", pnl_display: null }, [], LIM, null, []).pnl, "P/L unverified");   // card-only
  const h = PUI.closedHtml(SRC.positions, "Downside Dislocation Recovery", LIM);
  const per = Object.fromEntries(h.split('data-pui-closed-pos="').slice(1).map((s) => [s.slice(0, s.indexOf('"')), text(s.slice(s.indexOf(">") + 1))]));
  assert.deepEqual(Object.keys(per), ["NVDA", "MSFT", "AAPL"]);                                     // the three closed trades only (the open position is not listed)
  assert.ok(per.AAPL.includes("Realized paper P/L +10.00"), per.AAPL);
  assert.ok(per.MSFT.includes("Realized paper P/L P/L unverified") && per.NVDA.includes("Realized paper P/L P/L unverified"), JSON.stringify(per));
  assert.ok(!amount(text(h), "20.00") && !amount(text(h), "0.00"));                                 // the raw 20.00 never; the NULL never as 0.00
});

test("T-P04 / N-06 / T-P20 product_ui timeline: a CLOSED event shows the decorated event's P/L - never detail.realized_pnl as a number", () => {
  const tl = PUI.timeline(SRC).map((e) => e.text);
  assert.deepEqual(tl.filter((t) => t.includes("Trade closed")), ["NVDA · Trade closed · P/L unverified", "MSFT · Trade closed · P/L unverified", "AAPL · Trade closed · P/L +10.00"]);
  assert.ok(!tl.some((t) => amount(t, "20.00") || amount(t, "186.30")));
  // T-P20: the CLOSED event of an unverified trade whose detail still carries the raw +20.00
  const unv = PUI.timeline({ positions: [U20], position_events: [closedEvent(U20, "2026-10-21T16:00:00Z")] });
  assert.equal(unv[0].text, "MSFT · Trade closed · P/L unverified");
  // an undecorated CLOSED event (no pnl_status): "P/L unknown", never the raw detail value
  const raw = PUI.timeline({ positions: [U20], position_events: [{ position_id: "pu", kind: "CLOSED", at: "2026-10-21T16:00:00Z", detail: { realized_pnl: "20.00" } }] });
  assert.deepEqual(raw.map((e) => e.text), ["MSFT · Trade closed · P/L unknown"]);
  const act = text(PUI.activityHtml(SRC, EXEC, OVERVIEW, DAY, { filter: "trades" }));
  assert.ok(act.includes("AAPL · Trade closed · P/L +10.00") && act.includes("MSFT · Trade closed · P/L unverified") && !amount(act, "20.00"));
});

test("T-P05 / N-06 product_ui positionModel (open card): 'unknown' for the unpriced entry, never last price x quantity", () => {
  const m = PUI.positionModel(OPEN_CARD, OPEN, LIM);
  assert.deepEqual([m.open, m.symbol, m.entry, m.current, m.pnl], [true, "SNDK", "0.00", "62.10", "unknown"]);
  assert.equal(PUI.positionModel({ ...OPEN_CARD, pnl_status: undefined, pnl_display: undefined }, null, LIM).pnl, "unknown");   // undecorated, still unpriced
  assert.equal(PUI.positionModel({ ...OPEN_CARD, pnl_status: "VERIFIED", pnl_display: "186.30" }, null, LIM).pnl, "unknown");   // the entry check holds even then
  for (const e of [null, "", "0.00", "0", "-1.00"]) assert.equal(PUI.positionModel(null, { ...OPEN, average_entry_price: e, pnl_status: "VERIFIED", pnl_display: "186.30" }, LIM).pnl, "unknown", String(e));
  const priced = { ...OPEN_CARD, entry: "61.20", pnl: "2.70", pnl_pct: "1.47", line: "3 shares · entry 61.20 · now 62.10 · 2.70", pnl_status: "VERIFIED", pnl_display: "2.70" };
  assert.equal(PUI.positionModel(priced, null, LIM).pnl, "+2.70 (+1.47%)");
  assert.equal(PUI.positionModel({ ...priced, pnl_status: "UNVERIFIED", pnl_display: null }, null, LIM).pnl, "P/L unverified");
  assert.equal(PUI.positionModel({ ...priced, pnl_status: "UNKNOWN", pnl_display: null }, null, LIM).pnl, "unknown");
  assert.equal(PUI.positionModel({ ...priced, pnl_status: undefined, pnl_display: undefined }, null, LIM).pnl, "P/L unknown");   // an undecorated card: never the raw value
  assert.equal(PUI.positionLifecycle(OPEN, OPEN_CARD, [], LIM, null, EVENTS).pnl, "unknown");
  const ph = text(PUI.positionsHtml(PUI.positionCards(EXEC, SRC.positions, EVENTS, "Downside Dislocation Recovery")));
  assert.ok(ph.includes("Quantity 3 Entry 0.00 Current 62.10 P/L unknown") && !ph.includes("186.30"), ph);
  const cards = text(PUI.cardsHtml({ exec: EXEC, safety: SAFE, card: OPEN_CARD, position: OPEN, dom: {}, symbol: "SNDK", limits: LIM, avail: {}, confirm: null }));
  assert.ok(cards.includes("Entry 0.00 Current 62.10 P/L unknown"), cards);
});

test("T-P09 / N-07 paper_bot historyLine, cardModel, cardHtml: 'P/L unverified' / 'unknown' from pnl_status; never a number for those rows", () => {
  const H = (p, o) => ({ kind: "TRADE", symbol: p.symbol, at: p.closed_at, shares: p.entry_qty, entry: p.average_entry_price, exit: p.average_exit_price, pnl: p.realized_pnl, reason: "Maximum holding time reached",
                         reason_code: p.exit_reason, position_id: p.position_id, label: "Closed", pnl_status: p.pnl_status, pnl_display: p.pnl_display, pnl_kind: "realized", ...(o || {}) });
  assert.equal(P.historyLine(H(V)), "AAPL · Closed 2026-10-21 15:00 · 1 shares · entry 100.00 → exit 110.00 · P/L 10.00 · Maximum holding time reached");
  assert.equal(P.historyLine(H(U20)), "MSFT · Closed 2026-10-21 16:00 · 2 shares · entry 50.00 → exit 60.00 · P/L unverified · Maximum holding time reached");
  assert.equal(P.historyLine(H(UNULL)), "NVDA · Closed 2026-10-21 17:00 · 1 shares · entry 70.00 → exit 75.00 · P/L unverified · Maximum holding time reached");
  assert.equal(P.historyLine(H(V, { pnl_status: "UNKNOWN", pnl_display: null })), "AAPL · Closed 2026-10-21 15:00 · 1 shares · entry 100.00 → exit 110.00 · P/L unknown · Maximum holding time reached");
  const closedCard = (p, line) => ({ symbol: p.symbol, key: "CLOSED", icon: "⚪", label: "Closed", line, state: "CLOSED", setup_state: "NONE", position_id: p.position_id, execution_id: `e-${p.position_id}`,
                                     shares: p.entry_qty, entry: p.average_entry_price, current: p.average_exit_price, pnl: p.realized_pnl, pnl_pct: null, reason: "Maximum holding time reached", since: p.closed_at,
                                     details: { events: [] }, pnl_status: p.pnl_status, pnl_display: p.pnl_display, pnl_kind: "realized" });
  const m = (c) => { const x = P.cardModel(c); return [x.pnl, x.line]; };
  assert.deepEqual(m(closedCard(V, "Trade completed · 10.00 · Maximum holding time reached")), ["10.00", "Trade completed · 10.00 · Maximum holding time reached"]);
  assert.deepEqual(m(closedCard(U20, "Trade completed · 20.00 · Maximum holding time reached")), ["P/L unverified", "Trade completed · P/L unverified · Maximum holding time reached"]);
  assert.deepEqual(m(closedCard(UNULL, "Trade completed · — · Maximum holding time reached")), ["P/L unverified", "Trade completed · P/L unverified · Maximum holding time reached"]);
  assert.deepEqual(m(OPEN_CARD), ["unknown", "3 shares · entry 0.00 · now 62.10 · P/L unknown"]);
  assert.deepEqual(m({ ...OPEN_CARD, pnl_status: "VERIFIED", pnl_display: "186.30" }), ["unknown", "3 shares · entry 0.00 · now 62.10 · P/L unknown"]);   // unpriced entry wins
  assert.deepEqual(m({ key: "WATCHING", symbol: "AAPL", icon: "🟡", label: "Watching", line: "Setup on watch", state: "IDLE" }), ["—", "Setup on watch"]);       // no P/L shown at all
  const html = P.cardHtml(closedCard(U20, "Trade completed · 20.00 · Maximum holding time reached"), { detailsOpen: true }) + P.cardHtml(OPEN_CARD, { detailsOpen: true });
  const t = text(html);
  assert.ok(t.includes("P/L P/L unverified") && t.includes("P/L unknown") && !amount(t, "20.00") && !amount(t, "186.30") && !NUM_PL.test(t), t);
  const bot = text(P.botHtml({ mode: "MONITOR_ONLY", environment: { paper: true, simulated: true }, recent: [H(V), H(U20), H(UNULL)], cards: [] }, { detailsOpen: true }));
  assert.ok(bot.includes("AAPL · Closed") && bot.includes("P/L 10.00") && (bot.match(/P\/L unverified/g) || []).length === 2 && !amount(bot, "20.00"), bot);
});

test("T-P19 a POST action reply (undecorated rows, rendered by paper_bot after act()) reads 'P/L unknown'; never the raw value", () => {
  const undecorated = (o) => { const x = { ...o }; delete x.pnl_status; delete x.pnl_display; delete x.pnl_kind; return x; };
  const trade = undecorated({ kind: "TRADE", symbol: "MSFT", at: "2026-10-21T16:00:00Z", shares: 2, entry: "50.00", exit: "60.00", pnl: "20.00", reason: "Maximum holding time reached", position_id: "pu" });
  const closed = undecorated({ symbol: "MSFT", key: "CLOSED", icon: "⚪", label: "Closed", line: "Trade completed · 20.00 · Maximum holding time reached", state: "CLOSED", position_id: "pu", shares: 2, entry: "50.00",
                               current: "60.00", pnl: "20.00", pnl_pct: null, reason: "Maximum holding time reached", details: { events: [] } });
  const open = undecorated({ ...OPEN_CARD, entry: "61.20", pnl: "2.70", pnl_pct: "1.47", line: "3 shares · entry 61.20 · now 62.10 · 2.70" });
  const reply = { action: "enable_auto", mode: "PAPER_AUTO", environment: { paper: true, simulated: true }, recent: [trade], cards: [closed, open] };   // the shape of api/routes/execution.py _act
  assert.equal(P.historyLine(trade), "MSFT · Closed 2026-10-21 16:00 · 2 shares · entry 50.00 → exit 60.00 · P/L unknown · Maximum holding time reached");
  const bot = text(P.botHtml(reply, { detailsOpen: true }));
  const cards = text(reply.cards.map((c) => P.cardHtml(c, { detailsOpen: true })).join(" "));
  assert.ok(bot.includes("P/L unknown") && !amount(bot, "20.00"), bot);
  assert.deepEqual([P.cardModel(closed).pnl, P.cardModel(closed).line, P.cardModel(open).pnl, P.cardModel(open).line],
                   ["P/L unknown", "Trade completed · P/L unknown · Maximum holding time reached", "P/L unknown", "3 shares · entry 61.20 · now 62.10 · P/L unknown"]);
  assert.ok(!amount(cards, "20.00") && !amount(cards, "2.70") && !NUM_PL.test(cards), cards);
});

test("T-P12 / N-08 forward_validation summaryModel tiles: verified-only with the partial label; unrealized unknown; the run's bot-only drawdown or unknown; the account curve informational", () => {
  const m = FV.summaryModel(OVERVIEW);
  assert.deepEqual([m.closed, m.pnl, m.unrealized, m.runDd, m.maxDd, m.edge, m.edgeLabel, m.runPnl, m.runClosed, m.runSide],
                   ["1 verified · 2 unverified", "+10.00 · partial (2 unverified)", "unknown", "unknown (price age unknown)", "+120.00 (0.12 %)", "INSUFFICIENT_DATA", "Insufficient data",
                    "+10.00 · partial (2 unverified)", "1", "pending 0 · provisional 0 · unverified 2"]);
  const c = text(FV.compactHtml(OVERVIEW, { detailsOpen: true }));
  for (const s of ["Closed trades 1 verified · 2 unverified", "Paper P/L +10.00 · partial (2 unverified)", "Unrealized paper P/L unknown", "Max drawdown (forward run · bot only) unknown (price age unknown)",
                   "Max drawdown (adjusted equity · informational) +120.00 (0.12 %)", "Strategy status ⚪ Insufficient data"]) assert.ok(c.includes(s), s);
  assert.ok(!/30\.00|186\.30|Paper P\/L \+10\.00 Reconciliation|Paper P\/L \+10\.00 Strategy/.test(c), c);   // never the raw sum, never the verified part unlabelled
  const p = text(FV.pageHtml(OVERVIEW, [], [], [], {}));
  for (const s of ["Forward run paper P/L +10.00 · partial (2 unverified)", "Gross paper P/L +10.00 · partial (2 unverified)", "Max drawdown (forward run · bot only) unknown (price age unknown)",
                   "Max drawdown (adjusted equity · informational) +120.00 (0.12 %)", "Forward run trades pending 0 · provisional 0 · unverified 2"]) assert.ok(p.includes(s), s);
  // the run's drawdown when KNOWN; the edge label is the run block's, never the all-time block's
  assert.equal(FV.summaryModel({ ...OVERVIEW, run: { ...RUN, drawdown: { status: "KNOWN", max_drawdown_pct: "1.25", text: "bot only" } } }).runDd, "1.25 %");
  const stray = FV.summaryModel({ ...OVERVIEW, strategy: { ...OVERVIEW.strategy, edge_status: "EARLY_POSITIVE", edge_label: "Early positive", insufficient: false } });
  assert.deepEqual([stray.edge, stray.edgeLabel, stray.insufficient], ["INSUFFICIENT_DATA", "Insufficient data", true]);
  const legacy = FV.summaryModel({ ...OVERVIEW, run: undefined, all_time: undefined, promotion: undefined });   // a payload without the PR C keys never borrows the all-time edge
  assert.deepEqual([legacy.edge, legacy.runText, legacy.allTimeText, legacy.runDd], ["INSUFFICIENT_DATA", "Forward run: not available", "All-time record: not available", "unknown"]);
  // Rule F on the all-time tiles and the unrealized tile
  const st = (o) => FV.summaryModel({ ...OVERVIEW, strategy: { ...OVERVIEW.strategy, ...o } });
  assert.equal(st({ gross_pnl: null, closed_trades: 0 }).pnl, "not available (2 unverified)");
  assert.equal(st({ gross_pnl: null, closed_trades: 0, unverified: 0 }).pnl, "—");
  assert.equal(st({ unverified: 0 }).pnl, "+10.00");
  assert.equal(st({ unrealized: { open_positions: 1, unrealized_pnl: "2.70", positions: [{ entry: "61.20", unrealized_pnl: "2.70" }] } }).unrealized, "+2.70");
  assert.equal(st({ unrealized: { open_positions: 1, unrealized_pnl: "186.30", positions: [{ entry: "0.00", unrealized_pnl: "186.30" }] } }).unrealized, "unknown");   // an unpriced entry, even if a value slipped in
  assert.equal(st({ unrealized: { open_positions: 1, unrealized_pnl: null } }).unrealized, "unknown");
});

test("T-P13 / N-08 forward_validation tradeRow / dailyRow: the unverified label; never a number for an unverified trade; the day total by its pnl_status", () => {
  const row = (p, verified, o) => ({ position_id: p.position_id, closed_at: p.closed_at, symbol: p.symbol, qty: p.entry_qty, entry_price: p.average_entry_price, exit_price: p.average_exit_price,
                                     realized_pnl: p.realized_pnl, gross_pnl: p.realized_pnl, return_pct: verified ? "10.00" : null, holding_sessions: 1, exit_reason: p.exit_reason, verified, ...(o || {}) });
  const pl = (t) => cells(FV.tradeRow(t))[5];
  assert.deepEqual([pl(row(V, true)), pl(row(U20, false)), pl(row(UNULL, false)), pl(row(V, undefined))], ["+10.00", "unverified", "unverified", "unknown"]);
  assert.ok(!amount(text(FV.tradeRow(row(U20, false))), "20.00"));                                  // a raw value that slipped in is never rendered
  const day = (o) => cells(FV.dailyRow({ trading_date: DAY, session_status: "HEALTHY", funnel: { CLOSED: 3 }, duplicates: 0, reconciliation_issues: 0, safety_fires: 0,
                                         realized_pnl: "10.00", pnl_status: "UNVERIFIED_PRESENT", unverified: 2, ...o })).slice(-1)[0];
  assert.deepEqual([day({}), day({ pnl_status: "OK", unverified: 0 }), day({ realized_pnl: null }), day({ pnl_status: "UNSTABLE", realized_pnl: null }),
                    day({ pnl_status: "NO_TRADES", realized_pnl: null, unverified: 0 }), day({ pnl_status: undefined })],
                   ["+10.00 · partial (2 unverified)", "+10.00", "not available (2 unverified)", "not available", "—", "not available"]);
  const page = text(FV.pageHtml(OVERVIEW, [], [row(V, true), row(U20, false), row(UNULL, false)], [{ trading_date: DAY, session_status: "HEALTHY", funnel: {}, realized_pnl: "10.00", pnl_status: "UNVERIFIED_PRESENT", unverified: 2 }], {}));
  const closedTable = text(FV.pageHtml(OVERVIEW, [], [row(V, true), row(U20, false), row(UNULL, false)], [], {}).split("data-fv-closed")[1].split("</table>")[0]);
  assert.ok(page.includes("+10.00 · partial (2 unverified)") && (closedTable.match(/ unverified /g) || []).length === 2 && !amount(page, "20.00") && !amount(page, "30.00"), closedTable);
});

// ---------------------------------------------------------------- §3.10: UI (C-85…C-89) ---------------------------------------------------
test("N-01 the per-cohort table renders; non-run cohorts read 'not a forward run' with no P&L edge", () => {
  const h = FV.pageHtml(OVERVIEW, [], [], [], {});
  assert.ok(h.includes("data-fv-cohorts"));
  const rows = Object.fromEntries(h.split('data-fv-cohort="').slice(1).map((s) => [s.slice(0, s.indexOf('"')), cells(s.slice(0, s.indexOf("</tr>")))]));
  assert.deepEqual(rows["C-0003"], ["cohort 3", "2026-10-20 13:00", "2 / 30", "1", "forward run · Insufficient data", "+10.00 / 100.00 %"]);
  assert.deepEqual(rows["C-0002"], ["cohort 2", "2026-10-01 13:00", "9 / 30", "5", "not a forward run", "—"]);
  assert.deepEqual(rows["C-0001"], ["cohort 1", "2026-09-01 13:00", "30 / 30", "40", "not a forward run", "—"]);                 // the stray numbers and label never render
  assert.ok(!text(h).includes("55.00") && !text(h).includes("80.00 %") && !text(h).includes("Early positive"));
  assert.equal(cells(FV.cohortRow({ cohort_id: "X", label: "x", expectancy: "9.00", win_rate: "90.00", label_text: "Early positive" }))[4], "not a forward run");   // no forward_run flag: not a run
  assert.ok(text(FV.pageHtml({ ...OVERVIEW, forward_evidence: { error: "OperationalError" } }, [], [], [], {})).includes("No cohort recorded yet."));
});

test("N-02 'No forward run defined': the run line, the run header, the label, the drawdown; never 'Validation target reached' from the all-time count", () => {
  const d = { ...OVERVIEW, sessions: { ...OVERVIEW.sessions, completed: 30, target_met: true }, run: NO_RUN, all_time: { status: "READY", invalid_days: [], text: "All-time record: READY" },
              promotion: { eligible: false, text: "promotion blocked: no forward run" } };
  const m = FV.summaryModel(d);
  assert.deepEqual([m.runDefined, m.runText, m.runSessionsLabel, m.runDd, m.edgeLabel, m.edgeReasons], [false, "No forward run defined", "— / 30", "unknown (no forward run)", "Insufficient data", ["no forward run"]]);
  const h = FV.headline(m);
  assert.deepEqual([h.text, h.run, h.allTime], ["Validation in progress", "No forward run defined", "All-time record: READY"]);
  assert.ok(text(FV.compactHtml(d, {})).includes("No forward run defined All-time record: READY"));
  const rh = FV.runHeader(NO_RUN);
  assert.ok(rh.includes('data-fv-run-defined="0"') && text(rh) === "Forward run No forward run defined");
  assert.ok(FV.pageHtml(d, [], [], [], {}).includes(rh) && FV.runBanner(NO_RUN) === "" && FV.runHeader(null) === "");
});

test("N-03 the headline shows the run block and the all-time block (C-37); 'Validation target reached' needs target_met, run READY and no critical attention (F-6)", () => {
  const H = (d) => FV.headline(FV.summaryModel(d));
  const base = H(OVERVIEW);
  assert.deepEqual([base.text, base.run, base.allTime], ["Validation in progress", "Run FR-0001: READY", "All-time record: NOT READY (2 invalid days: 2026-10-14, 2026-10-16)"]);
  assert.deepEqual([H(READY_AT_TARGET).text, H(READY_AT_TARGET).cls], ["Validation target reached", "ok"]);
  const notReady = { ...READY_AT_TARGET, run: { ...READY_AT_TARGET.run, readiness: { status: "NOT READY", reasons: [{ code: "FAILED_DAY", dates: ["2026-10-21"] }], text: "Run FR-0001: NOT READY (FAILED_DAY 2026-10-21)" } } };
  assert.deepEqual([H(notReady).text, H(notReady).run], ["Validation in progress", "Run FR-0001: NOT READY (FAILED_DAY 2026-10-21)"]);
  const crit = { ...READY_AT_TARGET, attention: [{ code: "DUPLICATE_BROKER_ORDER", critical: true, count: 1 }], needs_attention: true };
  assert.equal(H(crit).text, "NEEDS ATTENTION");
  assert.equal(H({ ...READY_AT_TARGET, attention: [{ code: "FAILED_EXECUTION", critical: false, count: 1 }], needs_attention: true }).text, "Needs a look");
  assert.equal(H({ ...OVERVIEW, sessions: { ...OVERVIEW.sessions, completed: 30, target_met: true } }).text, "Validation in progress");   // the all-time count alone never reaches the target
  for (const v of [base, H(crit), H(notReady)]) assert.ok(v.run && v.allTime, JSON.stringify(v));                                         // both lines, whatever the state
  for (const html of [FV.compactHtml(READY_AT_TARGET, {}), FV.pageHtml(READY_AT_TARGET, [], [], [], {})]) {
    assert.equal((html.match(/data-fv-run-line/g) || []).length, 1);
    assert.equal((html.match(/data-fv-alltime-line/g) || []).length, 1);
    assert.ok(html.indexOf("data-fv-state") < html.indexOf("data-fv-run-line") && html.indexOf("data-fv-run-line") < html.indexOf("data-fv-alltime-line"));
  }
  const val = text(PUI.activityHtml(SRC, EXEC, OVERVIEW, DAY, {}).split("data-pui-validation")[1]);
  assert.ok(val.includes("Run FR-0001: READY") && val.includes("All-time record: NOT READY (2 invalid days: 2026-10-14, 2026-10-16)"), val);
});

test("N-04 the 'S / T strategy sessions' label with its breakdown, including unreadable", () => {
  const label = "2 / 30 strategy sessions (2 healthy · 0 degraded · 0 failed · 1 unreadable · 0 not run)";
  assert.equal(FV.summaryModel(OVERVIEW).runSessionsLabel, label);
  assert.ok(text(FV.compactHtml(OVERVIEW, { detailsOpen: true })).includes(`Forward run sessions ${label}`));
  assert.ok(text(FV.runHeader(RUN)).includes(`Strategy sessions ${label}`));
  assert.equal(FV.summaryModel(OVERVIEW).sessionsLabel, "2 / 30");                                   // the all-time label is unchanged beside it
});

test("N-09 the run header: run id, since, hash, cohort, the accepted blockers and the acknowledged prior invalid days by date", () => {
  const h = FV.runHeader(RUN);
  const t = text(h);
  for (const s of ["Forward run FR-0001", "Forward run FR-0001 since 2026-10-20", "Run FR-0001", "Since 2026-10-20 13:00", "First session 2026-10-20", "Frozen hash b5d4a56e0123…", "Cohort C-0003",
                   "Accepted blockers PF-18, EX-13, PF-08, W2-N23, W2-N03",
                   "Acknowledged prior invalid days 2026-10-14 (failed · duplicate broker order); 2026-10-16 (degraded · evidence unreadable)"]) assert.ok(t.includes(s), s);
  assert.ok(h.includes(`data-fv-run-hash="${RUN.frozen_hash}"`) && h.includes('data-fv-run="FR-0001"') && !h.includes("data-fv-run-banner"));
  const bare = text(FV.runHeader({ ...RUN, accepted_blockers: [], prior_invalid_days: [], ended_at: "2026-10-25T20:00:00+00:00", end_reason: "operator end" }));
  assert.ok(bare.includes("Accepted blockers none") && bare.includes("Acknowledged prior invalid days none") && bare.includes("Ended 2026-10-25 20:00 · operator end"), bare);
  assert.ok(FV.pageHtml(OVERVIEW, [], [], [], {}).includes(h));                                      // the page shows it
  assert.ok(!WORDS.test(t), t);                                                                       // approved_by is not rendered as a word
});

test("N-10 the suspended banner, with its code and first-evidence time; empty when the run is not suspended", () => {
  const sus = { ...RUN, suspended: true, suspension: { code: "FOREIGN_CODE", first_evidence_at: "2026-10-21T14:05:00+00:00", at: "2026-10-21T14:06:00+00:00" }, t_stop: "2026-10-21T14:05:00+00:00",
                readiness: { status: "NOT READY", reasons: [{ code: "SUSPENDED" }], text: "Run FR-0001: NOT READY (SUSPENDED)" } };
  const b = FV.runBanner(sus);
  assert.ok(b.includes('data-fv-run-banner="FOREIGN_CODE"'));
  assert.equal(text(b), "Run FR-0001 suspended · FOREIGN_CODE · first evidence 2026-10-21 14:05 · recorded 2026-10-21 14:06 · statistics up to 2026-10-21 14:05");
  assert.deepEqual([FV.runBanner(RUN), FV.runBanner(null), FV.runBanner({ ...RUN, suspended: false, suspension: sus.suspension })], ["", "", ""]);
  const d = { ...OVERVIEW, run: sus };
  for (const html of [FV.compactHtml(d, {}), FV.pageHtml(d, [], [], [], {}), FV.runHeader(sus)]) assert.equal((html.match(/data-fv-run-banner=/g) || []).length, 1);
  assert.ok(text(FV.compactHtml(d, {})).includes("Run FR-0001: NOT READY (SUSPENDED)"));
  assert.ok(text(FV.runBanner({ ...RUN, suspended: true, suspension: null, t_stop: null })).startsWith("Run FR-0001 suspended · code unknown · first evidence —"));
});

test("N-11 'promotion blocked: all-time record NOT READY' beside a run READY (the headline, the page, the product UI's Validation block); never APPROVED", () => {
  const h = FV.headline(FV.summaryModel(READY_AT_TARGET));
  assert.deepEqual([h.text, h.run, h.allTime, h.promotion], ["Validation target reached", "Run FR-0001: READY", "All-time record: NOT READY (2 invalid days: 2026-10-14, 2026-10-16)",
                                                             "promotion blocked: all-time record NOT READY (2026-10-14, 2026-10-16)"]);
  const c = text(FV.compactHtml(READY_AT_TARGET, {}));
  const i = ["Validation target reached", "Run FR-0001: READY", "All-time record: NOT READY", "promotion blocked: all-time record NOT READY (2026-10-14, 2026-10-16)"].map((s) => c.indexOf(s));
  assert.ok(i.every((x) => x >= 0) && i.every((x, k) => k === 0 || x > i[k - 1]), JSON.stringify(i));
  const p = text(FV.pageHtml(READY_AT_TARGET, [], [], [], {}));
  assert.ok(p.includes("Run FR-0001: READY All-time record: NOT READY (2 invalid days: 2026-10-14, 2026-10-16) promotion blocked: all-time record NOT READY (2026-10-14, 2026-10-16)"), p.slice(0, 400));
  const val = text(PUI.activityHtml(SRC, EXEC, READY_AT_TARGET, DAY, {}).split("data-pui-validation")[1]);
  assert.ok(val.includes("Run FR-0001: READY All-time record: NOT READY (2 invalid days: 2026-10-14, 2026-10-16) promotion blocked: all-time record NOT READY (2026-10-14, 2026-10-16)"), val);
  for (const t of [c, p, val]) assert.ok(!WORDS.test(t) && !/\bAPPROVED\b/.test(t), t);
});

test("C-89 the browser formats only: server text is escaped; no sum in the day-total paths; the export line gains exactly runHeader, runBanner, cohortRow", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const d = { ...OVERVIEW, run: { ...RUN, run_id: evil, headline: evil, readiness: { status: "READY", text: evil }, accepted_blockers: [evil], suspended: true, suspension: { code: evil, first_evidence_at: evil } },
              all_time: { status: "NOT READY", text: evil }, promotion: { eligible: false, text: evil }, forward_evidence: { cohorts: [{ cohort_id: evil, label: evil, forward_run: true, label_text: evil }] } };
  for (const html of [FV.compactHtml(d, { detailsOpen: true }), FV.pageHtml(d, [], [], [], {}), PUI.activityHtml(SRC, EXEC, d, DAY, {})]) {
    assert.ok(!html.includes("<img") && html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  }
  const fn = (src, name) => { const i = src.indexOf(`function ${name}(`); return src.slice(i, src.indexOf("\n  }\n", i)); };
  for (const name of ["todaySummary", "dailyReview", "positionLifecycle", "positionModel", "timeline"]) assert.ok(!/\.reduce\(/.test(fn(PUI_JS, name)), name);
  for (const name of ["summaryModel", "headline", "dailyRow", "tradeRow"]) assert.ok(!/\.reduce\(/.test(fn(FV_JS, name)), name);
  assert.ok(FV_JS.includes("  return { mount, refresh, render, renderPage, state, summaryModel, headline, compactHtml, pageHtml, sessionRow, tradeRow, dailyRow, POLL_MS, PAGE, cohortRow, runBanner, runHeader };"));   // the three names appended in sorted order (T-W05C)
  assert.deepEqual([typeof FV.runHeader, typeof FV.runBanner, typeof FV.cohortRow], ["function", "function", "function"]);
});
