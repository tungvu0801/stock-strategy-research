// Node tests for the Market Demand card (Stage 5.7) in frontend/trader_render.js — rendered separately from PPE, never combined.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../../frontend/trader_render.js");
const core = require("../../frontend/trader_core.js");

const DISCLAIMER = "Market Demand measures observed buyer participation and confirmation, not the probability that the stock will rise.";
const TRADE_WORDS = /\b(buy|sell|recommend\w*|should|target price|price target|expected return|win rate|enter now|trade now|short it)\b/i;
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const VIEW_JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "trader_view.js"), "utf8");

function demand(over) {
  return Object.assign({
    version: "demand_v1", symbol: "JPM", as_of: "2026-10-21T15:30:00+00:00", status: "AVAILABLE", mode: "INTRADAY_MODE", score: 72, label: "STRENGTHENING", coverage_weight: 0.85,
    components: {}, key_factors: ["holding above VWAP (10+ bars)", "relative volume 1.7x with price above VWAP", "higher lows and higher highs, 7 of last 10 closes up", "+0.8% vs XLF (session)"],
    sources: { mode: "real", market_data: "Alpaca market data · IEX feed · polled, read only · minute + daily bars", price_source: "latest quote", benchmark: "XLF", benchmark_kind: "SECTOR_ETF",
               benchmark_label: "XLF (Financials ETF)", bars_through: "2026-10-21T15:29:00+00:00", session: { date: "2026-10-21", complete: false, bars_completed: 119, open: 212.1, high: 214.9, low: 211.4, vwap: 213.2, prior_sessions: 5 },
               computed_at: "2026-10-21T15:30:02+00:00" },
    weights: {}, disclaimer: DISCLAIMER, note: null,
  }, over || {});
}
const SIM = demand({ symbol: "NVDA", status: "SIMULATED_DATA", score: 14, label: "VERY_WEAK", sources: Object.assign(demand().sources, { mode: "simulated", market_data: "simulator (deterministic, seeded) · NOT a market feed · minute + daily bars" }),
                     note: "SIMULATED DATA — synthetic bars from the development simulator. This Market Demand result demonstrates the display path and is not a market reading." });
const DAILY = demand({ symbol: "MSFT", mode: "DAILY_MODE", score: 61, label: "STRENGTHENING", coverage_weight: 0.6, key_factors: ["+1.8% vs XLK (1d)"], sources: Object.assign(demand().sources, { session: { date: "2026-10-21", complete: true, bars_completed: 0, prior_sessions: 0 } }) });
const INSUFFICIENT = demand({ symbol: "NEWCO", status: "INSUFFICIENT_DATA", mode: "DAILY_MODE", score: null, label: null, coverage_weight: 0.4, key_factors: [], note: "only 40% of the component weight is available (floor 60%)" });
const PPE = { version: "ppe_v1", symbol: "JPM", as_of: "2026-10-21", status: "AVAILABLE", stretch: { status: "AVAILABLE", score: -61.0, label: "DOWNSIDE_STRETCHED", coverage_weight: 1.0, key_factors: [] },
              confidence: { status: "AVAILABLE", score: 48, label: "MODERATE", reasons: [] }, sources: { mode: "real", market_data: "x", price_source: "y", benchmark_kind: "SECTOR_ETF", computed_at: "2026-10-21T15:30:02+00:00" }, weights: {},
              disclaimer: "PPE Confidence measures confidence in the overreaction classification, not the probability that the stock will rise." };

test("1 Demand renders separately from PPE: its own markup, no shared block, PPE unchanged", () => {
  const d = R.demandHtml(demand()), p = R.ppeHtml(PPE);
  assert.match(d, /data-demand="score"/);
  assert.doesNotMatch(d, /data-ppe=|tv-ppe-block|tv-ppe-val/);
  assert.doesNotMatch(p, /data-demand|tv-dem-/);
  assert.match(VIEW_JS, /<section class="tv-panel"><h3>PPE<\/h3><div id="tv-ppe" class="tv-panel-body"><\/div><\/section>\s*<section class="tv-panel tv-panel-demand"><h3>Market Demand<\/h3><div id="tv-demand" class="tv-panel-body"><\/div><\/section>/);
  assert.doesNotMatch(text(d), /stretch|confidence/i);                          // the Demand card never shows PPE numbers
});

test("2 Demand score and label render (72 STRENGTHENING)", () => {
  assert.match(R.demandHtml(demand()), /<b class="tv-dem-val strong">72<\/b><span class="tv-dem-band strong">STRENGTHENING<\/span>/);
  assert.match(R.demandHtml(SIM), /<b class="tv-dem-val weak">14<\/b><span class="tv-dem-band weak">VERY WEAK<\/span>/);
  const m = R.demandModel(demand({ score: 50, label: "MIXED" }));
  assert.deepEqual([m.value, m.band, m.cls], ["50", "MIXED", ""]);              // mixed is neutral, never coloured
});

test("3 coverage renders", () => {
  assert.match(R.demandHtml(demand()), /<div class="tv-ppe-meta"><span>Coverage<\/span><b>85%<\/b><\/div>/);
  assert.match(R.demandHtml(DAILY), /Coverage<\/span><b>60%<\/b>/);
  assert.equal(R.demandModel({ symbol: "X", status: "AVAILABLE", score: 50 }).coverage, "—");
});

test("4 missing Demand renders NOT AVAILABLE (and a loading skeleton during a switch)", () => {
  for (const d of [null, undefined, {}]) {
    const html = R.demandHtml(d, { symbol: "NVDA" });
    assert.match(html, /<div class="tv-state" data-state="DEMAND_NOT_AVAILABLE">DEMAND NOT AVAILABLE<\/div>/);
    assert.match(html, /No Market Demand result for NVDA yet\./);
    assert.doesNotMatch(text(html), /\d/);
  }
  assert.match(R.demandHtml(null, { loading: true, symbol: "MSFT" }), /data-state="LOADING" aria-busy="true"><div class="tv-state tv-state-dim">LOADING DEMAND · MSFT<\/div>/);
  const ins = R.demandHtml(INSUFFICIENT);
  assert.match(ins, /data-state="DEMAND_INSUFFICIENT_DATA">DEMAND INSUFFICIENT DATA<\/div><p class="tv-dim">only 40% of the component weight is available \(floor 60%\)<\/p>/);
  assert.match(ins, /<b class="tv-dem-val ">—<\/b><span class="tv-dem-band ">INSUFFICIENT DATA<\/span>/);
  assert.match(R.demandHtml(demand({ status: "NOT_AVAILABLE", mode: "NONE", score: null, label: null, coverage_weight: 0, note: "no bars" })), /DEMAND NOT AVAILABLE<\/div><p class="tv-dim">no bars<\/p>/);
});

test("5 intraday / daily mode is visible, never implied", () => {
  assert.match(R.demandHtml(demand()), /<span class="tv-dem-mode" data-demand-mode="INTRADAY" title="Completed bars of the current session\.">INTRADAY · session bars<\/span>/);
  assert.match(R.demandHtml(DAILY), /<span class="tv-dem-mode" data-demand-mode="DAILY" title="Only completed daily bars are available: no real-time confirmation is implied\.">DAILY · completed bars<\/span>/);
  assert.equal(R.demandModel(demand({ mode: "NONE" })).mode, "");
});

test("6 simulator results carry a visible SIMULATED DATA tag", () => {
  assert.match(R.demandHtml(SIM), /<span class="tv-tag warn">SIMULATED DATA<\/span>/);
  assert.equal(R.demandModel(SIM).simulated, true);
  assert.doesNotMatch(R.demandHtml(demand()), /SIMULATED/);
  assert.equal(R.demandModel(demand({ status: "AVAILABLE", sources: Object.assign(demand().sources, { mode: "simulated" }) })).simulated, true);   // mode alone is enough
  assert.match(R.demandSourceHtml(SIM, SIM.note), /SIMULATED DATA — synthetic bars/);
});

test("7 a stale ticker's demand message is ignored and panels are tagged with the result's own symbol", () => {
  assert.equal(core.acceptsMessage({ type: "demand", symbol: "SNDK", score: 90 }, "NVDA"), false);
  assert.equal(core.acceptsMessage({ type: "demand", symbol: "NVDA" }, "NVDA"), true);
  assert.equal(core.acceptsMessage({ type: "demand" }, "NVDA"), false);
  assert.equal(R.demandModel(demand()).symbol, "JPM");
  assert.match(VIEW_JS, /el\.dataset\.symbol = \(d && d\.symbol\) \|\| st\.active \|\| "";/);
});

test("8 a ticker switch shows the new ticker's loading state and then that ticker's demand", () => {
  assert.match(VIEW_JS, /\["tv-demand", "DEMAND"\]/);
  assert.match(VIEW_JS, /pending = \{ candles: true, position: true, ppe: true, demand: true, evidence: true \}/);
  assert.match(VIEW_JS, /case "demand": renderDemand\(m\); break;/);
  assert.match(VIEW_JS, /renderDemand\(d\.demand\)/);
  assert.notEqual(R.demandModel(demand()).symbol, R.demandModel(SIM).symbol);
});

test("9 the exact disclaimer is a visible paragraph (and the page adds the server's sentence itself)", () => {
  assert.match(R.demandHtml(demand()), new RegExp(`<p class="tv-disclaimer">${DISCLAIMER.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}</p>`));
  assert.doesNotMatch(R.demandHtml(demand(), { includeDisclaimer: false }), /tv-disclaimer/);
  assert.match(VIEW_JS, /R\.demandHtml\(d, \{ includeDisclaimer: false \}\) \+ `<p class="tv-disclaimer">\$\{esc\(d\.disclaimer \|\| ""\)\}<\/p>`/);
});

test("10 no BUY / SELL recommendation text; probability appears only in the disclaimer", () => {
  for (const d of [demand(), SIM, DAILY, INSUFFICIENT, null]) {
    const t = text(R.demandHtml(d, { symbol: "X" }));
    assert.doesNotMatch(t, TRADE_WORDS, t.slice(0, 120));
    const n = (t.match(/probability/g) || []).length;
    assert.ok(n === 0 || (n === 1 && t.includes(DISCLAIMER)), t.slice(0, 160));
  }
  assert.doesNotMatch(R.EXPLAIN_DEMAND, TRADE_WORDS);
  assert.doesNotMatch(R.EXPLAIN_DEMAND, /probability|reversal|expected/i);
  assert.match(R.demandHtml(demand()), /data-demand-info="score" title="Market Demand: observed buyer participation/);
});

test("11 Prepare Buy / Prepare Sell remain disabled buttons and Demand never feeds them", () => {
  assert.match(VIEW_JS, /\{ id: "prepare_buy", label: "Prepare Buy", enabled: false, status: "PREVIEW_NOT_CONNECTED"/);
  assert.match(VIEW_JS, /\{ id: "prepare_sell", label: "Prepare Sell", enabled: false, status: "PREVIEW_NOT_CONNECTED"/);
  assert.match(VIEW_JS, /aria-disabled="\$\{a\.enabled \? "false" : "true"\}"\$\{a\.enabled \? "" : " disabled"\}/);
  const actions = VIEW_JS.split("function renderActions")[1].split("function renderStatusLine")[0];
  assert.doesNotMatch(actions, /demand|ppe/);
});

test("12 key factors render as short lines and the colours are not the price palette", () => {
  const html = R.demandHtml(demand());
  assert.match(html, /Key factors<\/div><ul class="tv-ppe-list"><li>holding above VWAP \(10\+ bars\)<\/li><li>relative volume 1\.7x with price above VWAP<\/li>/);
  const css = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "trader_view.css"), "utf8");
  assert.match(css, /\.tv-dem-val\.weak \{ color: var\(--tv-down\); \}/);
  assert.match(css, /\.tv-dem-val\.strong \{ color: var\(--tv-up\); \}/);
  assert.doesNotMatch(css.split("Market Demand card")[1].split("actions:")[0], /--green|--red/);
});
