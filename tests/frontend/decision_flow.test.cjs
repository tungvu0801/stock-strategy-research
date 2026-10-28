// Node tests for Stage 6.10C (frontend/product_ui.js): TRADE › Decision Flow (the latest setup-evaluation cycle: its ten items, a per-ticker table whose rows
// open their READY checks, distance and risk checks, the legend, Closest to READY) and ACTIVITY › Decision history (the day counts, the cycles, a
// reconstructed cycle, "why not at T"). Everything comes from the read-only GET payloads: the decisions keep the one vocabulary (READY · SKIPPED · BLOCKED ·
// WATCHING · WAIT), a missing value reads "Not available", times are New York with UTC, the backend's display order is kept, every string is escaped.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PUI = require("../../frontend/product_ui.js");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const JS = fs.readFileSync(path.join(FRONT, "product_ui.js"), "utf8");
const CSS = fs.readFileSync(path.join(FRONT, "product_ui.css"), "utf8");
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const BANNED = [/\b(buy now|sell now|enter now|trade now|you should|approved|execute|recommend\w*|profitable|winning|guaranteed|high confidence)\b/i, /will (be )?(execut|plac|submit)/i,
                /\d+ \/ \d+ conditions/, /Opportunity Score \d/];
const ROUTES = ["/api/decision-flow", "/api/decision-flow/history", "/api/decision-flow/cycle", "/api/decision-flow/explain"];

// ---- fixtures: the DESIGN_610C §6 shapes ---------------------------------------------------------------------------------------------------------------
const DEFS = [["STRETCH", "Downside stretch (PPE)", "<= -40"], ["PPE_COVERAGE", "PPE coverage", ">= 0.60"], ["CONFIDENCE", "PPE confidence", ">= 40"],
              ["DEMAND_DATA", "Market Demand data", "available, coverage >= 0.60, fresh"], ["DEMAND", "Market Demand", ">= 60"], ["EVIDENCE", "Stage 5.4 evidence", "COMPLETED packet <= 30 days"]];
const checks = (spec) => DEFS.map(([id, label, threshold_text]) => { const s = spec[id] || {};
  return { id, label, value: s.value === undefined ? null : s.value, value_text: s.text === undefined ? null : s.text, threshold_text, passed: s.passed === undefined ? null : s.passed,
           gap: s.gap === undefined ? null : s.gap, detail: s.detail || "", root_cause: s.root || null }; });
const NO_EVIDENCE = { STRETCH: { value: -3.6, text: "-3.6", passed: false, gap: 36.4, detail: "No downside stretch." }, PPE_COVERAGE: { value: 0.92, text: "0.92", passed: true },
                      CONFIDENCE: { detail: "no stored Stage 5.4 evidence packet", passed: false, root: "NO_EVIDENCE_PACKET" },
                      DEMAND_DATA: { value: 0.85, text: "available · 0.85 · fresh", passed: true }, DEMAND: { value: 55, text: "55", passed: false, gap: 5, detail: "Market Demand below 60." },
                      EVIDENCE: { text: "none stored", passed: false, detail: "No stored Stage 5.4 evidence packet; READY needs evidence.", root: "NO_EVIDENCE_PACKET" } };
const WATCH = { ...NO_EVIDENCE, STRETCH: { value: -45.2, text: "-45.2", passed: true, gap: -5.2 }, DEMAND: { value: 64, text: "64", passed: true, gap: -4 } };
const ALL_PASS = { STRETCH: { value: -52.4, text: "-52.4", passed: true, gap: -12.4 }, PPE_COVERAGE: { value: 0.91, text: "0.91", passed: true }, CONFIDENCE: { value: 63, text: "63", passed: true, gap: -23 },
                   DEMAND_DATA: { value: 0.88, text: "available · 0.88 · fresh", passed: true }, DEMAND: { value: 71, text: "71", passed: true, gap: -11 },
                   EVIDENCE: { value: "COMPLETED", text: "COMPLETED, 3 days old", passed: true } };
const SETUP = { type: "DOWNSIDE_DISLOCATION_RECOVERY", name: "Downside Dislocation Recovery", note: "only setup type; no cross-setup ranking", state: "NONE", prior_state: "NONE",
                state_label: "NO ACTIVE SETUP", evaluation: "HOLD", reason_code: "NO_DOWNSIDE_STRETCH", reason_text: "No downside stretch.", setup_id: null };
const READY_SETUP = { ...SETUP, state: "READY", prior_state: "WATCH", state_label: "READY FOR REVIEW", evaluation: "TRANSITION", reason_code: "READY_CONFIRMED", reason_text: "Buyer confirmation complete.", setup_id: "st-1" };
const ROW = (symbol, decision, o) => ({
  symbol, evaluated_at: "2026-10-27T19:59:03+00:00", evaluated_at_et: "2026-10-27T15:59:03-04:00", trigger: "SCHEDULED", origin: "SCHEDULER_CYCLE",
  price: { value: null, as_of: null, text: "Not available (no cached quote of 60 s or less)" }, setup: SETUP, score: { value: null, text: "Not available: the setup engine computes no composite score." },
  values: { stretch: -3.6, ppe_coverage: 0.92, confidence: null, confidence_status: "EVIDENCE_NOT_AVAILABLE", demand: 55, demand_status: "AVAILABLE", demand_coverage: 0.85 },
  checks: checks(NO_EVIDENCE), passed: ["PPE coverage", "Market Demand data"], failed: ["Downside stretch (PPE)", "PPE confidence", "Market Demand", "Stage 5.4 evidence"],
  distance: { unmet: 4, unmet_root_causes: 3, checks: 6, stretch_gap: 36.4, demand_gap: 5, confidence_gap: null, text: "4 of 6 checks unmet (3 distinct root causes)" },
  risk: { evaluated: false, text: "Not evaluated: entry controls run only for a READY setup during Paper Auto.", controls: [], blocked_by: [] },
  decision, decision_detail: null, decision_label: null, no_trade_reason: "No active setup: no downside stretch.", consistent: true, ...o });
const READY_RISK = (blocks) => ({ evaluated: true, text: blocks.length ? `${blocks.length} entry controls blocked.` : "Every entry control passed.",
  controls: [["EXECUTION_MODE", "Mode"], ["SAFETY_GATE", "Risk gate"], ["SESSION_POLICY", "Market session"], ["MAX_OPEN_POSITIONS", "Open-positions limit"]]
    .map(([id, label]) => ({ id, label, result: blocks.includes(id) ? "BLOCK" : "PASS", reason: blocks.includes(id) ? `${label} blocked the entry` : "ok" })), blocked_by: blocks });
const READY_ON = { setup: READY_SETUP, checks: checks(ALL_PASS), passed: DEFS.map((d) => d[1]), failed: [], no_trade_reason: null,
                   distance: { unmet: 0, unmet_root_causes: 0, checks: 6, stretch_gap: -12.4, demand_gap: -11, confidence_gap: -23, text: "Every check met" } };
const MU = ROW("MU", "READY", { ...READY_ON, price: { value: 101.25, as_of: "2026-10-27T19:58:59+00:00", text: "101.25 (cached quote, 19:58:59 UTC)" }, risk: READY_RISK([]),
                                decision_detail: "ORDER_INTENT", decision_label: null });
const AMD = ROW("AMD", "BLOCKED", { ...READY_ON, risk: READY_RISK(["SAFETY_GATE", "SESSION_POLICY"]), decision_detail: "SAFETY_GATE", decision_label: "Entry blocked by 2 controls",
                                   no_trade_reason: "Blocked by the risk gate and the market session." });
const AVGO = ROW("AVGO", "SKIPPED", { ...READY_ON, risk: READY_RISK([]), decision_detail: "ENTRIES_PER_CYCLE", no_trade_reason: "The per-cycle entry limit was used by MU." });
const SNDK = ROW("SNDK", "WATCHING", { setup: { ...SETUP, state: "WATCH", prior_state: "WATCH", state_label: "WATCH", reason_code: "DOWNSIDE_STRETCH_DETECTED", reason_text: "Downside stretch detected." },
                                       checks: checks(WATCH), passed: ["Downside stretch (PPE)", "PPE coverage", "Market Demand data", "Market Demand"], failed: ["PPE confidence", "Stage 5.4 evidence"],
                                       distance: { unmet: 2, unmet_root_causes: 1, checks: 6, stretch_gap: -5.2, demand_gap: -4, confidence_gap: null, text: "2 of 6 checks unmet (1 root cause)" },
                                       no_trade_reason: "Setup WATCH: confirmation incomplete (no stored Stage 5.4 evidence packet)." });
const NVDA = ROW("NVDA", "WAIT", { decision_detail: "NO_SETUP" });
const CYCLE = { cycle_id: "SE-20261027T195903Z", at: "2026-10-27T19:59:03+00:00", at_et: "2026-10-27T15:59:03-04:00", next_at: null, stale: false,
                market: { session: "REGULAR", label: "Regular session", calendar: "NYSE (holiday-aware static calendar)" }, tickers_scanned: 5, candidate_setups: 3,
                counts: { READY: 1, SKIPPED: 1, BLOCKED: 1, WATCHING: 1, WAIT: 1 },
                risk: { state: "TRADING_ALLOWED", allowed_new_entry: true, reason_codes: [], evaluated_at: "2026-10-27T19:59:00+00:00",
                        preflight: { status: "READY", at: "2026-10-27T13:15:03+00:00", failing: [] }, reconciliation: { healthy: true, attention: 0 } },
                paper_auto: { mode: "PAPER_AUTO", on: true, label: "Paper Auto", policy: "MANUAL_ARM", policy_label: "Manual arm · starts in Monitor only", since: "2026-10-27T13:35:00+00:00" },
                executor: { runs: 2, results: { RUN: 2, ERROR: 0 }, first_at: "2026-10-27T19:59:10+00:00", last_at: "2026-10-27T19:59:40+00:00", idle_note: "" },
                order_intents: 1, orders_submitted: 1, outcome: { code: "ORDER_INTENT", text: "1 order intent created (MU)." }, closest: { symbol: "MU", decision: "READY", unmet: 0, text: "MU · READY · every check met" } };
const CLOSEST = [{ rank: 1, symbol: "MU", decision: "READY", state: "READY", unmet: 0, unmet_root_causes: 0, stretch_gap: -12.4, demand_gap: -11, text: "every check met" },
                 { rank: 2, symbol: "AVGO", decision: "SKIPPED", state: "READY", unmet: 0, unmet_root_causes: 0, stretch_gap: -9.1, demand_gap: -2, text: "entry limit used" },
                 { rank: 3, symbol: "AMD", decision: "BLOCKED", state: "READY", unmet: 0, unmet_root_causes: 0, stretch_gap: -3, demand_gap: 0, text: "blocked by 2 controls" },
                 { rank: 4, symbol: "SNDK", decision: "WATCHING", state: "WATCH", unmet: 2, unmet_root_causes: 1, stretch_gap: -5.2, demand_gap: -4, text: "confirmation incomplete" },
                 { rank: 5, symbol: "NVDA", decision: "WAIT", state: "NONE", unmet: 4, unmet_root_causes: 3, stretch_gap: 36.4, demand_gap: 5, text: "no downside stretch" }];
const P = { version: "decision_flow_v1", generated_at: "2026-10-27T20:00:00+00:00", broker_calls: 0, cycle: CYCLE, tickers: [NVDA, SNDK, MU, AVGO, AMD], closest: CLOSEST, ranking_rule: null,
            universe: { mode: "OFF", live: { count: 5, source: "watchlist.txt" }, core: { count: 79 }, active: { count: 5, equals_live: true },
                        note: "Inactive. The live watchlist is unchanged. Activation is a later stage." }, legend: null, disclaimer: "A read-only projection of persisted rows." };
// a Monitor-only cycle: the READY setup is not sent (SKIPPED · PAPER_AUTO_OFF) and the entry controls never ran
const CLS = ROW("CLS", "SKIPPED", { ...READY_ON, decision_detail: "PAPER_AUTO_OFF", no_trade_reason: "Paper Auto OFF (Monitor only): a READY setup is not sent." });
const P_OFF = { ...P, cycle: { ...CYCLE, counts: { READY: 0, SKIPPED: 1, BLOCKED: 0, WATCHING: 0, WAIT: 1 }, paper_auto: { mode: "MONITOR_ONLY", on: false, label: "Monitor only", policy: "MANUAL_ARM", since: null },
                                order_intents: 0, orders_submitted: 0, outcome: { code: "NO_TRADE", text: "No trade: Paper Auto OFF (Monitor only)." }, closest: null },
                tickers: [CLS, NVDA], closest: [] };
const EARLIER = { ...CYCLE, cycle_id: "SE-20261027T195803Z", at: "2026-10-27T19:58:03+00:00", at_et: "2026-10-27T15:58:03-04:00", next_at: "2026-10-27T19:59:03+00:00",
                  counts: { READY: 0, SKIPPED: 0, BLOCKED: 0, WATCHING: 1, WAIT: 4 }, order_intents: 0, orders_submitted: 0, outcome: { code: "NO_TRADE", text: "No trade: no setup READY." } };
const H = { date: "2026-10-27", latest_date: "2026-10-27", has_more: true, cycles: [CYCLE, EARLIER],
            day: { date: "2026-10-27", timezone: "America/New_York", latest_date: "2026-10-27", cycles: 390, evaluations: 4156, off_cycle_evaluations: 251,
                   tickers: ["AMD", "AVGO", "MU", "NVDA", "SNDK"], ready_transitions: 1, ready_evaluations: 3, watch_transitions: 2,
                   skipped_paper_auto_off: { count: 2, basis: "READY evaluations while Paper Auto was OFF" }, skipped_entry_limit: 1, blocked: { count: 3, by_control: { SAFETY_GATE: 2, SESSION_POLICY: 1 }, labels: { SAFETY_GATE: "Risk gate", SESSION_POLICY: "Market session" } },
                   closest: { symbol: "SNDK", unmet: 2, stretch: -45.2, at: "2026-10-27T17:31:03+00:00", text: null }, paper_auto: { spans: [{ from: "2026-10-27T13:35:00+00:00", to: "2026-10-27T15:05:00+00:00" }], minutes: 90 },
                   executor: { runs: 533, with_candidates: 4 }, order_intents: 1, orders: 1, fills: 1, risk_states: { TRADING_ALLOWED: 380, NO_NEW_ENTRIES: 10 } } };
const SEL = { at: EARLIER.at, data: { cycle: EARLIER, tickers: [SNDK, NVDA], closest: CLOSEST.slice(3) } };
const X = { symbol: "MU", asked: { date: "2026-10-27", time: "10:32", tz: "ET", utc: "2026-10-27T14:32:00+00:00" },
            evaluation: { ...NVDA, symbol: "MU", evaluated_at: "2026-10-27T14:31:58+00:00", evaluated_at_et: "2026-10-27T10:31:58-04:00" },
            context: { mode: "MONITOR_ONLY", paper_auto: { mode: "MONITOR_ONLY", on: false, label: "Monitor only" }, risk: { state: "TRADING_ALLOWED", allowed_new_entry: true, reason_codes: [] },
                       executor: { result: "IDLE", text: "Monitor only: the cycle was IDLE (not persisted)." }, session: { session: "REGULAR", label: "Regular session" } },
            answer: "MU did not trade at 10:32 ET: no active setup (no downside stretch), and Paper Auto was OFF." };
const flow = (p, open, rows, conn) => PUI.decisionFlowHtml(PUI.decisionFlowModel(p, conn || null), open || {}, rows || {});
const rowOf = (html, sym) => { const i = html.indexOf(`data-pui-df-ticker="${sym}"`); return html.slice(html.lastIndexOf("<tr", i), html.indexOf("</tr>", i)); };

test("the models are pure: the same payload gives the same model, read from the payload only (no clock, no page state)", () => {
  assert.deepEqual(PUI.decisionFlowModel(P, null), PUI.decisionFlowModel(P, null));
  assert.deepEqual(PUI.decisionHistoryModel(H, null), PUI.decisionHistoryModel(H, null));
  assert.deepEqual(PUI.explainModel(X), PUI.explainModel(X));
  assert.equal(flow(P, { flow: true }, { MU: true }), flow(P, { flow: true }, { MU: true }));
  const pure = JS.slice(JS.indexOf("Stage 6.10C: Decision Flow + Decision history (pure"), JS.indexOf("page wiring (browser only)"));
  assert.ok(pure.length > 5000);
  assert.doesNotMatch(pure, /Date\.now|Math\.random|getJson\(|fetch\(|document\.|window\.|\bstate\./);
  assert.deepEqual(PUI.DF_DECISIONS, ["READY", "SKIPPED", "BLOCKED", "WATCHING", "WAIT"]);
  assert.deepEqual(PUI.DF_CLS, { READY: "ok", SKIPPED: "info", BLOCKED: "bad", WATCHING: "warn", WAIT: "dim" });
});

test("every decision class renders with its badge and its own text: READY order intent, SKIPPED (Paper Auto OFF / entry limit), BLOCKED with control ids, WATCHING with failed checks, WAIT", () => {
  const all = { flow: true }, h = flow(P, all, { MU: true, AMD: true, SNDK: true });
  const badge = (sym) => (rowOf(h, sym).match(/class="pui-badge ([a-z]+)" data-pui-df-decision="([A-Z]+)"/) || []).slice(1);
  assert.deepEqual(["MU", "AVGO", "AMD", "SNDK", "NVDA"].map(badge), [["ok", "READY"], ["info", "SKIPPED"], ["bad", "BLOCKED"], ["warn", "WATCHING"], ["dim", "WAIT"]]);
  assert.ok(text(rowOf(h, "MU")).includes("READY Order intent created"), text(rowOf(h, "MU")));
  assert.ok(text(rowOf(h, "AVGO")).includes("SKIPPED Per-cycle entry limit already used"));
  const amd = text(rowOf(h, "AMD"));
  assert.ok(amd.includes("BLOCKED Entry blocked by 2 controls") && amd.includes("Risk gate · Market session") && amd.includes("Blocked by the risk gate and the market session."), amd);
  const amdDetail = h.slice(h.indexOf('data-pui-df-detail="AMD"'));
  assert.match(amdDetail, /data-pui-df-risk="EVALUATED"/);
  assert.match(amdDetail, /data-pui-df-control="SAFETY_GATE"><span class="pui-badge bad">BLOCK<\/span> Risk gate/);
  assert.match(amdDetail, /data-pui-df-control="EXECUTION_MODE"><span class="pui-badge ok">PASS<\/span> Mode/);
  assert.ok(text(amdDetail).includes("Blocked by Risk gate, Market session"));
  const sndk = text(rowOf(h, "SNDK"));
  assert.ok(sndk.includes("WATCH") && sndk.includes("2 of 6 checks unmet PPE confidence · Stage 5.4 evidence") && sndk.includes("confirmation incomplete"), sndk);
  const sndkDetail = h.slice(h.indexOf('data-pui-df-detail="SNDK"'), h.indexOf('data-pui-df-ticker="MU"'));
  assert.deepEqual([...sndkDetail.matchAll(/data-pui-df-check="([A-Z_]+)" data-result="([a-z ]+)"/g)].map((m) => [m[1], m[2]]),
                   [["STRETCH", "passed"], ["PPE_COVERAGE", "passed"], ["CONFIDENCE", "failed"], ["DEMAND_DATA", "passed"], ["DEMAND", "passed"], ["EVIDENCE", "failed"]]);
  assert.ok(text(rowOf(h, "NVDA")).includes("WAIT No active setup") && text(rowOf(h, "NVDA")).includes("4 of 6 checks unmet"));
  const off = flow(P_OFF, all, { CLS: true });
  const cls = text(rowOf(off, "CLS"));
  assert.ok(cls.includes("SKIPPED Paper Auto OFF: monitor only, nothing sent") && cls.includes("All 6 checks met"), cls);
  assert.match(off, /data-pui-df-detail="CLS"[\s\S]*data-pui-df-risk="NOT_EVALUATED"/);
  assert.ok(text(off).includes("Paper Auto OFF · Monitor only") && text(off).includes("No trade: Paper Auto OFF (Monitor only)."));
  assert.equal((h.match(/data-pui-df-ticker=/g) || []).length, 5);
  assert.equal((h.match(/data-pui-df-detail=/g) || []).length, 3);                         // only the open rows carry their detail
});

test("missing values read Not available: price, score, confidence, a paused evaluation's checks, an empty cycle item; nothing is estimated", () => {
  const m = PUI.decisionRowModel(NVDA);
  assert.deepEqual([m.price, m.priceText, m.score], ["Not available", "Not available (no cached quote of 60 s or less)", "Not available: the setup engine computes no composite score."]);
  assert.equal(m.checks.find((c) => c.id === "CONFIDENCE").value, "Not available");
  const bare = PUI.decisionRowModel({ symbol: "ORCL", decision: "WAIT", price: { value: null }, score: { value: null }, checks: [{ id: "CONFIDENCE", label: "PPE confidence", value: null, passed: null }] });
  assert.deepEqual([bare.price, bare.priceText, bare.score, bare.checks[0].value, bare.checks[0].result, bare.unmetText], ["Not available", "Not available", PUI.DF_NO_SCORE, "Not available", "not evaluated", "Not evaluated"]);
  assert.equal(PUI.DF_NO_SCORE, "Not available: the setup engine computes no composite score.");
  const paused = PUI.decisionRowModel({ ...NVDA, setup: { ...SETUP, evaluation: "PAUSED" }, checks: checks({}), failed: [], distance: {}, decision_detail: "PAUSED" });
  assert.deepEqual([paused.unmetText, paused.detail, paused.checks.every((c) => c.result === "not evaluated")], ["Not evaluated", "Evaluation paused", true]);
  const h = flow(P, { flow: true }, { NVDA: true, MU: true });
  assert.ok(text(rowOf(h, "NVDA")).includes("NVDA Not available NO ACTIVE SETUP"));
  assert.ok(text(rowOf(h, "MU")).includes("101.25 cached 19:58:59 UTC"));
  const nvda = text(h.slice(h.indexOf('data-pui-df-detail="NVDA"')));
  assert.ok(nvda.includes("PPE confidence Not available >= 40 failed") && nvda.includes("Score Not available: the setup engine computes no composite score.") && nvda.includes("Confidence gap Not available"), nvda);
  assert.ok(nvda.includes("Not evaluated: entry controls run only for a READY setup during Paper Auto."));
  const empty = PUI.cycleModel({ cycle_id: "SE-x", at: "2026-10-27T19:59:03Z" });
  assert.deepEqual(empty.items.map((i) => i.key), ["cycle", "market", "scanned", "candidates", "decisions", "risk", "paper_auto", "executor", "orders", "outcome"]);
  assert.deepEqual(empty.items.filter((i) => !i.counts && i.key !== "cycle").map((i) => i.text), Array(8).fill("Not available"));
  assert.ok(PUI.cycleItemsHtml(empty).includes('data-pui-df-count="READY">READY —</span>'));   // a missing count is a dash, never zero
  assert.ok(!/\d+ points? to READY|probability|expected/i.test(text(h)));
});

test("Closest to READY keeps the backend's order exactly, shows the gaps (met at zero or below) and says it is a display order, not a score", () => {
  const shuffled = [CLOSEST[4], CLOSEST[0], CLOSEST[3], CLOSEST[2], CLOSEST[1]];
  const h = PUI.closestHtml(PUI.closestModel(shuffled), PUI.DF_RANKING);
  assert.deepEqual([...h.matchAll(/data-symbol="([A-Z]+)"/g)].map((m) => m[1]), ["NVDA", "MU", "SNDK", "AMD", "AVGO"]);
  assert.deepEqual([...h.matchAll(/data-pui-df-rank="(\d+)"/g)].map((m) => m[1]), ["5", "1", "4", "3", "2"]);   // ranks as given, never recomputed
  const t = text(h);
  assert.ok(t.includes("NVDA WAIT NONE 4 3 root causes 36.4 5 no downside stretch") && t.includes("AMD BLOCKED READY 0 -3 (met) 0 (met)"), t);
  assert.ok(t.includes("A display order of persisted values, not a score"));
  assert.deepEqual(PUI.closestModel([{ symbol: "X" }, { symbol: "Y" }]).map((c) => c.rank), ["1", "2"]);
  assert.ok(text(PUI.closestHtml([], PUI.DF_RANKING)).includes("No ticker to order."));
  const own = flow({ ...P, ranking_rule: "Backend rule text." }, { flow: true });
  assert.ok(text(own).includes("Backend rule text.") && !text(own).includes("A display order of persisted values"));
});

test("the fold is closed by default; the user's open fold and open rows are kept; a row's button says whether it is open", () => {
  const closed = flow(P, {});
  assert.match(closed, /<details class="pui-fold" data-pui-df-fold="flow"><summary data-pui-df-summary="flow"><b>Decision Flow<\/b> · /);
  assert.doesNotMatch(closed, / open>/);
  const open = flow(P, { flow: true }, { SNDK: true });
  assert.match(open, /data-pui-df-fold="flow" open>/);
  assert.deepEqual([...open.matchAll(/data-pui-df-row="([A-Z]+)" aria-expanded="(true|false)"/g)].map((m) => [m[1], m[2]]),
                   [["NVDA", "false"], ["SNDK", "true"], ["MU", "false"], ["AVGO", "false"], ["AMD", "false"]]);
  assert.deepEqual([...open.matchAll(/data-pui-df-detail="([A-Z]+)"/g)].map((m) => m[1]), ["SNDK"]);
  const hist = (o) => PUI.decisionHistoryHtml(PUI.decisionHistoryModel(H, null), o, null, {});
  assert.match(hist({}), /<details class="pui-fold" data-pui-dh-fold="cycles"><summary/);
  assert.match(hist({ cycles: true }), /data-pui-dh-fold="cycles" open>/);
  assert.match(PUI.decisionHistoryShellHtml(null), /<details class="pui-fold" data-pui-dh-fold="explain"><summary/);   // the explain fold is built once, closed
});

test("the cycle: ten items, the summary reads the outcome, New York time with UTC; the legend says WATCHING means setup state WATCH only", () => {
  const m = PUI.decisionFlowModel(P, null), h = flow(P, { flow: true });
  assert.equal(text(h.slice(h.indexOf("<summary"), h.indexOf("</summary>"))), "Decision Flow · 1 order intent created (MU). · cycle 15:59:03 ET · 19:59:03 UTC");
  assert.match(h, /data-pui-df-outcome="ORDER_INTENT"/);
  assert.equal(m.cycle.items.length, 10);
  const t = text(h);
  for (const s of ["Cycle 2026-10-27 15:59:03 ET · 19:59:03 UTC SE-20261027T195903Z", "Market Regular session NYSE (holiday-aware static calendar)", "Tickers scanned 5", "Candidate setups 3",
                   "Decisions READY 1 SKIPPED 1 BLOCKED 1 WATCHING 1 WAIT 1", "Risk gate Trading allowed startup check READY · reconciliation healthy · gate read 15:59:00 ET",
                   "Paper Auto ON · Paper Auto · since 09:35:00 ET Auto-run policy (current setting): Manual arm · starts in Monitor only", "Executor 2 runs (RUN 2) · 15:59:10 ET → 15:59:40 ET", "Orders 1 order intent · 1 submitted",
                   "Outcome 1 order intent created (MU). Closest to READY: MU · READY · every check met", "Tickers (5)",
                   "Universe: dynamic universe OFF · live coverage 5 symbols (watchlist.txt) · core list 79 · Inactive. The live watchlist is unchanged. Activation is a later stage. " +
                   "A read-only projection of persisted rows. Times are New York (ET) with UTC.", "(0 broker calls)"]) assert.ok(t.includes(s), s);
  assert.ok(!t.includes(".."), t);
  assert.ok(t.includes(PUI.DF_WATCH_NOTE) && PUI.DF_WATCH_NOTE.startsWith("Decision Flow WATCHING means setup state WATCH only."));
  assert.deepEqual([...h.matchAll(/data-pui-df-means="([A-Z]+)"><span class="pui-badge ([a-z]+)"/g)].map((x) => [x[1], x[2]]),
                   [["READY", "ok"], ["SKIPPED", "info"], ["BLOCKED", "bad"], ["WATCHING", "warn"], ["WAIT", "dim"]]);
  assert.ok(text(flow({ ...P, legend: { WATCHING: "WATCH in the setup engine." } }, { flow: true })).includes("WATCHING WATCH in the setup engine."));
  assert.equal(PUI.etUtc(null, "2026-10-27T19:59:03Z", true), "2026-10-27 15:59:03 ET · 19:59:03 UTC");          // EDT
  assert.equal(PUI.etUtc(null, "2026-11-02T14:32:00+00:00"), "09:32:00 ET · 14:32:00 UTC");                     // EST after the change
  assert.equal(PUI.etUtc(null, "2026-10-27T19:59:03.123456"), "15:59:03 ET · 19:59:03 UTC");                     // an offset-less stamp is UTC, never the browser's zone
  assert.equal(PUI.etUtc("2026-10-27 15:59:03 EDT", "2026-10-27T19:59:03+00:00"), "15:59:03 ET · 19:59:03 UTC");  // the backend's own ET text wins
  assert.equal(PUI.etUtc(null, null), "Not available");
  const stale = text(flow({ ...P, cycle: { ...CYCLE, stale: true, next_at: null } }, { flow: true }));
  assert.ok(stale.includes("stale: no newer cycle recorded"));
});

test("history: the day counts and details, the cycles newest first (a time reconstructs its cycle below), more cycles said, an empty day points to the latest one", () => {
  const m = PUI.decisionHistoryModel(H, null);
  assert.deepEqual(m.strip.map((x) => [x.label, x.value]), [["Cycles", "390"], ["Evaluations", "4156"], ["Tickers", "5"], ["READY transitions", "1"], ["WATCH transitions", "2"],
    ["Skipped · Paper Auto OFF", "2"], ["Skipped · entry limit", "1"], ["Blocked", "3"], ["Order intents", "1"], ["Orders", "1"], ["Fills", "1"]]);
  const h = PUI.decisionHistoryHtml(m, { cycles: true }, SEL, { SNDK: true });
  const t = text(h);
  for (const s of ["Trading date 2026-10-27 (America/New_York)", "Off-cycle evaluations 251", "READY evaluations 3", "Skipped (Paper Auto OFF) basis READY evaluations while Paper Auto was OFF",
                   "Blocked by control Risk gate 2 · Market session 1", "Paper Auto armed 90 min · 09:35:00 ET → 11:05:00 ET", "Executor runs 533 · 4 with candidates",
                   "Risk gate (rows by state) Trading allowed 380 · New trades paused 10", "Closest to READY SNDK · 2 unmet · stretch -45.2 · 13:31:03 ET",
                   "Recent cycles (2+, newest first)", "The newest 2 cycles are shown; the day has more."]) assert.ok(t.includes(s), s);
  assert.deepEqual([...h.matchAll(/data-pui-dh-cycle="([^"]+)"/g)].map((x) => x[1]), [CYCLE.at, EARLIER.at]);   // the backend's order (newest first)
  assert.match(h, /data-pui-dh-cycle-row="2026-10-27T19:58:03\+00:00" class="pui-df-open"/);
  assert.match(h, /data-pui-dh-cycle="2026-10-27T19:58:03\+00:00" aria-pressed="true"/);
  const at = h.indexOf('data-pui-dh-cycle-row="2026-10-27T19:59:03'), first = h.slice(h.lastIndexOf("<tr", at), h.indexOf("</tr>", at));
  assert.ok(text(first).startsWith("15:59:03 ET 19:59:03 UTC Regular session 5 READY 1 SKIPPED 1 BLOCKED 1 WATCHING 1 WAIT 1 ON · Paper Auto · since 09:35:00 ET Trading allowed 2 runs 1 order intent"), text(first));
  const det = h.slice(h.lastIndexOf("<div", h.indexOf("data-pui-dh-detail")));
  assert.ok(text(det).startsWith("Reconstructed cycle from the persisted rows Close Cycle 2026-10-27 15:58:03 ET · 19:58:03 UTC SE-20261027T195803Z"), text(det).slice(0, 160));
  assert.deepEqual([...det.matchAll(/data-pui-dh-row="([A-Z]+)" aria-expanded="(true|false)"/g)].map((x) => [x[1], x[2]]), [["SNDK", "true"], ["NVDA", "false"]]);
  assert.match(det, /data-pui-dh-detail="SNDK"/);
  assert.ok(text(PUI.cycleDetailHtml({ at: EARLIER.at, busy: true }, {})).includes("Reading the cycle…"));
  assert.ok(text(PUI.cycleDetailHtml({ at: EARLIER.at, error: "HTTP 404" }, {})).includes("2026-10-27 15:58:03 ET · 19:58:03 UTC · from the persisted rows Close Cycle unavailable (HTTP 404)."));
  const none = PUI.decisionHistoryHtml(PUI.decisionHistoryModel({ date: "2026-10-25", latest_date: "2026-10-27", has_more: false, day: { cycles: 0 }, cycles: [] }, null), {}, null, {});
  assert.ok(text(none).includes("No evaluation cycle on 2026-10-25. Latest day with data: 2026-10-27") && none.includes('data-pui-dh-goto="2026-10-27"'));
  assert.ok(text(none).includes("Cycles 0") && text(none).includes("Evaluations —") && text(none).includes("Executor runs Not available"));
});

test("explain: the asked time with its UTC instant, the backend's answer, the evaluation then (checks, risk) and the context; the form checks its fields before any read", () => {
  const h = PUI.explainHtml({ data: X }), t = text(h);
  assert.ok(t.startsWith("MU at 2026-10-27 10:32 ET (14:32:00 UTC) MU did not trade at 10:32 ET: no active setup (no downside stretch), and Paper Auto was OFF."), t.slice(0, 200));
  assert.ok(t.includes("WAIT No active setup · evaluated 2026-10-27 10:31:58 ET · 14:31:58 UTC · 4 of 6 checks unmet · No active setup: no downside stretch."));
  assert.match(h, /data-pui-dx-eval="WAIT"/);
  assert.match(h, /data-pui-df-checks="6"/);
  for (const s of ["Market session Regular session", "Paper Auto OFF · Monitor only", "Mode Monitor only", "Risk gate Trading allowed", "Executor Monitor only: the cycle was IDLE (not persisted)."]) assert.ok(t.includes(s), s);
  assert.ok(text(PUI.explainHtml({ data: { ...X, evaluation: null, answer: ["Market closed at that time.", "No cycle ran."] } })).includes("Market closed at that time. No cycle ran. Evaluation: Not available."));
  assert.ok(text(PUI.explainHtml(null)).startsWith("Why a ticker did or did not trade at a past moment"));
  assert.equal(text(PUI.explainHtml({ busy: true })), "Reading the persisted rows…");
  assert.equal(text(PUI.explainHtml({ error: "No answer (HTTP 400)." })), "No answer (HTTP 400).");
  const ok = { symbol: "MU", date: "2026-10-27", time: "10:32", tz: "ET" };
  assert.equal(PUI.explainProblem(ok), "");
  assert.equal(PUI.explainProblem({ ...ok, tz: "PT" }), "");
  assert.equal(PUI.explainProblem({ ...ok, time: "9:05", tz: "UTC" }), "");
  assert.deepEqual([{ ...ok, symbol: "" }, { ...ok, symbol: "mu<b>" }, { ...ok, date: "10/27/2026" }, { ...ok, time: "25:00" }, { ...ok, time: "10.32" }, { ...ok, tz: "CET" }].map(PUI.explainProblem),
                   ["Enter a ticker, for example MU.", "Enter a ticker, for example MU.", "Enter the date as YYYY-MM-DD.", "Enter the time as HH:MM (24-hour).", "Enter the time as HH:MM (24-hour).", "Choose ET, PT or UTC."]);
  const shell = PUI.decisionHistoryShellHtml({ symbol: "SNDK", date: "2026-10-26", time: "15:45", tz: "PT" });
  assert.match(shell, /<form class="pui-dx-form" data-pui-dx-form autocomplete="off">/);
  assert.match(shell, /name="symbol" maxlength="10" placeholder="MU" spellcheck="false" value="SNDK"/);
  assert.deepEqual([...shell.matchAll(/<option value="([A-Z]+)"( selected)?>/g)].map((x) => [x[1], !!x[2]]), [["ET", false], ["PT", true], ["UTC", false]]);
  assert.match(shell, /data-pui-dh-day autocomplete="off"><label>Trading date <input name="date" [^>]*required pattern="\[0-9\]\{4\}-\[0-9\]\{2\}-\[0-9\]\{2\}"/);
  assert.doesNotMatch(shell, /\b(action|method)=/);                                           // no form here can navigate or post: the submit is read through getJson
  assert.ok(shell.includes('<div data-pui-dh-part="history"></div>') && shell.includes('<div data-pui-dh-part="answer"></div>'));
});

test("the backend's real shapes: labels over codes, small gaps, attention items, no cycle, stale cycle, zones, a since from an earlier day, the no-evaluation answer", () => {
  // a failed check's gap below 1 keeps two decimals and is never "(met)"; the check's own result decides
  const cov = PUI.decisionRowModel({ ...NVDA, checks: [{ id: "PPE_COVERAGE", label: "PPE coverage", value: 0.56, value_text: "0.56", passed: false, gap: 0.04 },
                                                      { id: "PPE_COVERAGE", label: "PPE coverage", value: 0.6, value_text: "0.60", passed: true, gap: 0 },
                                                      { id: "PPE_COVERAGE", label: "PPE coverage", value: 0.599, value_text: "0.60", passed: false, gap: 0 }] });
  assert.deepEqual(cov.checks.map((c) => c.gap), ["0.04", "0 (met)", "0"]);
  // "DECISION · detail": the badge shows the decision once; a reworded reason drops its code; the backend's consistency note is shown
  const row = PUI.decisionRowModel({ ...NVDA, decision_label: "WAIT · no active setup", consistent: false, consistency_note: "Recomputed checks differ: STRETCH.",
                                     setup: { ...SETUP, reason_code: "CONFIDENCE_BELOW_40", reason_text: "PPE confidence not available (no stored Stage 5.4 evidence packet).", reason_reworded: true } });
  assert.equal(row.detail, "No active setup");
  const rh = PUI.tickerTableHtml([row], { NVDA: true }, "df");
  assert.ok(text(rh).includes("WAIT No active setup") && !text(rh).includes("WAIT WAIT") && !text(rh).includes("CONFIDENCE_BELOW_40"), text(rh));
  assert.ok(text(rh).includes("Recomputed checks differ: STRETCH."));
  // the cycle: reconciliation attention items listed, the risk reason as text, zero executor results dropped and the error types shown, a policy label
  const cyc = PUI.cycleModel({ ...CYCLE, risk: { ...CYCLE.risk, state: "NO_NEW_ENTRIES", allowed_new_entry: false, reason_codes: ["EQUITY_NOT_AVAILABLE"], reason_text: ["Account equity not available"],
                                                reconciliation: { healthy: false, attention: ["MU: order needs reconciliation"] } },
                               executor: { ...CYCLE.executor, results: { RUN: 1, ERROR: 1 }, error_types: ["TimeoutError"] } });
  const it = Object.fromEntries(cyc.items.map((x) => [x.key, x]));
  assert.equal(it.risk.text, "New trades paused · Account equity not available");
  assert.ok(it.risk.sub.includes("reconciliation needs a look · 1 attention item: MU: order needs reconciliation"), it.risk.sub);
  assert.ok(it.executor.text.startsWith("2 runs (RUN 1 · ERROR 1) · errors: TimeoutError"), it.executor.text);
  // no cycle (none recorded, or none at or before a requested time): the empty state, never ten "Not available" items under a time
  const none = { cycle_id: null, at: null, at_et: null, outcome: { code: "NO_CYCLE", text: "No evaluation cycle recorded yet." }, risk: { state: "NO_NEW_ENTRIES" } };
  assert.equal(PUI.cycleModel(none), null);
  const nf = flow({ broker_calls: 0, cycle: none, tickers: [], closest: [] }, { flow: true });
  assert.equal(text(nf.slice(nf.indexOf("<summary"), nf.indexOf("</summary>"))), "Decision Flow · No evaluation cycle recorded yet.");
  assert.ok(text(PUI.cycleDetailHtml({ at: "2026-10-26T18:00:00Z", data: { cycle: none, tickers: [], closest: [] } }, {}))
    .includes("2026-10-26 14:00:00 ET · 18:00:00 UTC · from the persisted rows Close No evaluation cycle at or before this time."));
  // a stale latest cycle: its date and "(stale)" in the fold summary
  const st = flow({ ...P, cycle: { ...CYCLE, stale: true } }, {});
  assert.ok(text(st.slice(st.indexOf("<summary"), st.indexOf("</summary>"))).endsWith("cycle 2026-10-27 15:59:03 ET · 19:59:03 UTC (stale)"));
  // explain: asked in PT or UTC also reads New York; a since from an earlier day carries its date; no evaluation in the window says when the last one was
  const pt = { ...X, asked: { date: "2026-10-27", time: "07:32", tz: "PT", utc: "2026-10-27T14:32:00+00:00", et: "2026-10-27 10:32:00 ET" },
               context: { ...X.context, paper_auto: { mode: "MONITOR_ONLY", on: false, label: "Monitor only", since: "2026-10-26T23:16:12+00:00", since_et: "2026-10-26 19:16:12 ET" } } };
  assert.ok(text(PUI.explainHtml({ data: pt })).startsWith("MU at 2026-10-27 07:32 PT (10:32:00 ET · 14:32:00 UTC)"));
  assert.ok(text(PUI.explainHtml({ data: pt })).includes("Paper Auto OFF · Monitor only · since 2026-10-26 19:16:12 ET"));
  assert.ok(text(PUI.explainHtml({ data: { ...pt, asked: { ...pt.asked, time: "14:32", tz: "UTC" } } })).startsWith("MU at 2026-10-27 14:32 UTC (10:32:00 ET)"));
  const gone = { ...X, found: false, evaluation: null, query: { max_age_s: 600 }, nearest_before: { evaluated_at: "2026-10-26T19:59:16+00:00", evaluated_at_et: "2026-10-26 15:59:16 ET", age_s: 39644 } };
  assert.ok(text(PUI.explainHtml({ data: gone })).includes("No evaluation in the 10 minutes up to that time; the last one before it was at 2026-10-26 15:59:16 ET · 19:59:16 UTC."));
  // the backend legend (decisions keyed by code, its WATCHING note and ranking rule) and its disclaimer are what the page shows
  const lg = flow({ ...P, legend: { decisions: [{ code: "BLOCKED", text: "Backend BLOCKED text." }], watching_note: "Backend watching note.", ranking_rule: "Backend ranking rule." } }, { flow: true });
  for (const s of ["BLOCKED Backend BLOCKED text.", "Backend watching note.", "Backend ranking rule.", PUI.DF_MEANS.READY]) assert.ok(text(lg).includes(s), s);
  assert.equal(PUI.closestModel([{ symbol: "SNDK", state: "NONE", state_label: "NO ACTIVE SETUP" }])[0].state, "NO ACTIVE SETUP");
  // the history: a span still running reads open
  const run = PUI.decisionHistoryModel({ ...H, day: { ...H.day, paper_auto: { spans: [{ from: "2026-10-27T15:33:47+00:00", to: null, open: true }], minutes: 20 } } }, null);
  assert.ok(run.details.find(([k]) => k === "Paper Auto armed")[1].endsWith("11:33:47 ET → open"));
  // the form takes the API's ticker rule (letters, '.', '-')
  assert.equal(PUI.explainProblem({ symbol: "A1", date: "2026-10-27", time: "10:32", tz: "ET" }), "Enter a ticker, for example MU.");
  assert.equal(PUI.explainProblem({ symbol: "BRK.B", date: "2026-10-27", time: "10:32", tz: "ET" }), "");
  assert.ok(JS.includes('time: val("time").replace(/^(\\d):/, "0$1:")'));                   // 9:30 is sent as 09:30 (the API takes HH:MM)
});

test("not loaded, a failed read and odd shapes degrade to plain text, never a crash; a failed read keeps the previous payload and says so", () => {
  assert.ok(text(flow(null, { flow: true })).includes("Decision Flow · Loading…"));
  const down = flow(null, { flow: true }, {}, { error: "HTTP 404" });
  assert.match(down, /data-pui-df="UNAVAILABLE"/);
  assert.ok(text(down).includes("Decision Flow · Unavailable (HTTP 404)."));
  const stale = text(flow(P, { flow: true }, {}, { error: "HTTP 502", okAtIso: "2026-10-27T19:59:10.000Z" }));
  assert.ok(stale.includes("Last read failed (HTTP 502); showing the read from 19:59:10 UTC.") && stale.includes("Tickers (5)"));
  assert.ok(text(flow({ broker_calls: 0, cycle: null, tickers: [], closest: [] }, { flow: true })).includes("Decision Flow · No evaluation cycle recorded yet."));
  const odd = { cycle: { counts: "x", risk: "UNKNOWN", paper_auto: "OFF", executor: 3, market: "CLOSED", outcome: [] }, tickers: { a: 1 }, closest: "x", legend: ["x"], universe: 7 };
  assert.doesNotThrow(() => flow(odd, { flow: true }));
  assert.doesNotThrow(() => flow({ tickers: [null, 3, "x", { checks: "x", risk: { controls: [null, 1] }, distance: [] }] }, { flow: true }, { "—": true }));
  assert.ok(text(flow(odd, { flow: true })).includes("Risk gate UNKNOWN") && text(flow(odd, { flow: true })).includes("Paper Auto OFF"));
  assert.ok(text(PUI.decisionHistoryHtml(PUI.decisionHistoryModel(null, "HTTP 500"), {}, null, {})) === "Decision history unavailable (HTTP 500).");
  assert.ok(text(PUI.decisionHistoryHtml(PUI.decisionHistoryModel(H, "HTTP 500"), {}, null, {})).includes("Last read failed (HTTP 500); showing the earlier read."));
  assert.doesNotThrow(() => PUI.decisionHistoryHtml(PUI.decisionHistoryModel({ day: [], cycles: {}, date: 5 }, null), { cycles: true }, { at: 1, data: "x" }, {}));
  assert.doesNotThrow(() => PUI.explainHtml({ data: { asked: "x", context: [], evaluation: 5, answer: { a: 1 } } }));
  assert.deepEqual(PUI.renderDecisionFixture(P, H), { flow: false, history: false });          // node: no page; the fixture is only remembered
  assert.ok(PUI.state.dfForced === P && PUI.state.dhForced === H);
  PUI.renderDecisionFixture(null, null);
  assert.ok(PUI.state.dfForced === null && PUI.state.dhForced === null);
});

test("every string field is escaped (<script> / <img> in every value, in the keys the panels print, and in the typed form values)", () => {
  const E = "<script>x()</script><img src=x onerror=alert(1)>";
  const evil = (v) => (Array.isArray(v) ? v.map(evil) : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, evil(x)])) : typeof v === "string" ? `${v}${E}` : v);
  const p = evil(P);
  p.cycle.executor.results = { [E]: 1 };
  p.legend = { [E]: E, WATCHING: E };
  const rows = Object.fromEntries(PUI.decisionFlowModel(p, null).rows.map((r) => [r.symbol, true]));
  const h = evil(H);
  h.day.blocked.by_control = { [E]: 2 };
  h.day.risk_states = { [E]: 3 };
  const sel = { at: E, data: evil(SEL.data) };
  const selRows = Object.fromEntries(evil(SEL.data).tickers.map((r) => [r.symbol, true]));
  const out = [flow(p, { flow: true }, rows), flow(p, { flow: true }, rows, { error: E, okAtIso: E }), flow(null, {}, {}, { error: E }),
               PUI.decisionHistoryHtml(PUI.decisionHistoryModel(h, E), { cycles: true }, sel, selRows), PUI.decisionHistoryHtml(PUI.decisionHistoryModel(null, E), {}, null, {}),
               PUI.decisionHistoryHtml(PUI.decisionHistoryModel(h, null), { cycles: true }, { at: E, error: E }, {}), PUI.explainHtml({ data: evil(X) }), PUI.explainHtml({ error: E }),
               PUI.decisionHistoryShellHtml({ symbol: E, date: E, time: E, tz: E })];
  for (const html of out) {
    assert.doesNotMatch(html, /<script|<img/i);
    assert.match(html, /&lt;script&gt;x\(\)&lt;\/script&gt;&lt;img src=x onerror=alert\(1\)&gt;/);
  }
  assert.match(out[0], /data-pui-df-detail="NVDA&lt;script&gt;/);                                // attribute values too
  assert.match(out[8], /value="&lt;script&gt;x\(\)&lt;\/script&gt;&lt;img src=x onerror=alert\(1\)&gt;"/);
});

test("no forbidden wording in anything rendered: every decision class, history, a reconstructed cycle, the explain answer, the empty and failed states", () => {
  const all = (p) => Object.fromEntries(PUI.decisionFlowModel(p, null).rows.map((r) => [r.symbol, true]));
  const pages = [flow(P, { flow: true }, all(P)), flow(P_OFF, { flow: true }, all(P_OFF)), flow(null, {}), flow(null, {}, {}, { error: "HTTP 404" }),
                 PUI.decisionHistoryHtml(PUI.decisionHistoryModel(H, null), { cycles: true }, SEL, { SNDK: true, NVDA: true }), PUI.decisionHistoryShellHtml(null),
                 PUI.explainHtml({ data: X }), PUI.explainHtml(null), PUI.cycleItemsHtml(null)].map(text);
  for (const t of pages) for (const re of BANNED) assert.doesNotMatch(t, re);
  for (const re of BANNED) assert.doesNotMatch(JS.slice(JS.indexOf("Stage 6.10C: Decision Flow + Decision history (pure"), JS.indexOf("function mount()")), re);
  assert.ok(!/Not available in this version/.test(JS) && !/regime|retry|claude|anthropic|openai|data-action/i.test(JS));
});

test("wiring: one fetch( call site (getJson), the four GET routes as literals, every read caught, no new timer, socket or write; placed after Bot Activity, hidden in View details", () => {
  assert.equal((JS.match(/fetch\(/g) || []).length, 1);
  assert.ok(JS.includes("async function getJson(path) { const r = await fetch(path);"));
  for (const r of ROUTES) assert.ok(JS.includes(`getJson("${r}"`), r);
  const routes = [...JS.matchAll(/["`](\/api\/[a-z0-9_\-\/]+)/g)].map((m) => m[1]).filter((r) => r.startsWith("/api/decision-flow"));
  assert.deepEqual([...new Set(routes)].sort(), ROUTES.slice().sort());
  const wire = JS.slice(JS.indexOf("Stage 6.10C TRADE › Decision Flow: GET"), JS.indexOf("function mount()"));
  assert.ok(wire.length > 3000);
  assert.doesNotMatch(wire, /setInterval\(|setTimeout\(|WebSocket\(|EventSource\(|method|POST|localStorage|\.click\(\)/);
  assert.equal((wire.match(/getJson\(/g) || []).length, 4);
  assert.equal((wire.match(/\.catch\(/g) || []).length, 4);
  assert.ok(wire.includes("const DF_EVERY_MS = 15000;") && wire.includes("Date.now() - state.dfReadAt < DF_EVERY_MS"));
  assert.ok(wire.includes("e.preventDefault();"));
  const tick = JS.slice(JS.indexOf("function tick()"), JS.indexOf("async function loadUnified"));
  assert.match(tick, /if \(activeLegacyTab\(\) === "trader"\) \{[^\n]*if \(ensureBotActivity\(\)\) loadBotActivity\(\); loadUnified\(\); if \(ensureDecision\(\)\) loadDecision\(\); \}/);
  assert.ok(JS.slice(JS.indexOf("async function loadActivity()"), JS.indexOf("function renderActivity()")).includes("loadDecisionHistory();"));
  assert.ok(JS.slice(JS.indexOf("function placeBotActivity()"), JS.indexOf("function loadBotActivity(")).includes("placeDecision();"));
  assert.ok(JS.includes('act.insertAdjacentElement("afterend", el);'));                       // ACTIVITY: a sibling after the Activity panel, never inside its rebuilt HTML
  assert.ok(JS.includes('state.dfOpen[d.dataset.puiDfFold] = d.open; }, true);') && JS.includes('state.dhOpen[d.dataset.puiDhFold] = d.open; }, true);'));   // capture-phase toggle listeners
  assert.ok(CSS.includes("body.pui:not(.pui-collapsed) #pui-decision { display: none; }"));
  assert.equal((JS.match(/clickLegacy\(/g) || []).length, 5);
  assert.equal((JS.match(/runAutoAction\(/g) || []).length, 3);
  assert.equal((JS.match(/setInterval\(/g) || []).length, 3);
});
