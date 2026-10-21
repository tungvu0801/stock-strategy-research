// Node tests for frontend/trader_render.js — Stage 5.6.1 Trader View UI polish: the exact markup and view-models the tab renders
// (run by tests/test_trader_view_561.py through node --test). Display only: no PPE, evidence or market-data arithmetic lives here.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../../frontend/trader_render.js");
const core = require("../../frontend/trader_core.js");

const DISCLAIMER = "PPE Confidence measures confidence in the overreaction classification, not the probability that the stock will rise.";
const TRADE_WORDS = /\b(buy|sell|recommend\w*|should|target price|price target|expected return|win rate|enter now|trade now)\b/i;
const DEMAND_WORDS = /order.?flow|buyer.?pressure|setup readiness/i;   // Stage 5.7 added the Market Demand card itself (tests/frontend/trader_demand.test.cjs); these stay out
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const VIEW_JS = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "trader_view.js"), "utf8");

function ppe(over) {
  return Object.assign({
    version: "ppe_v1", symbol: "SNDK", as_of: "2026-10-21", status: "AVAILABLE",
    stretch: { status: "AVAILABLE", score: -82.0, raw_score: -82.3, label: "EXTREME_DOWNSIDE", coverage_weight: 0.9, direction: "DOWN", components: {},
               key_factors: ["2.6 ATR below EMA20", "5d return unusually negative (robust z -2.9)", "-9.1% vs SOXX over 5d (robust z -2.4)"] },
    confidence: { status: "AVAILABLE", score: 38, label: "LOW", reasons: ["-12 weak evidence", "+8 company confirms"], contributions: {}, caps: [],
                  evidence: { packet_id: "a".repeat(32), packet_status: "COMPLETED", created_at: "2026-10-09T21:30:00+00:00", age_days: 12, strength: "WEAK", cause: "MIXED", rungs: {} } },
    sources: { mode: "real", market_data: "Alpaca market data · IEX feed · polled, read only · daily bars", price_source: "snapshot", bars_through: "2026-10-20", sessions_used: 289,
               benchmark: "SOXX", benchmark_kind: "SECTOR_ETF", benchmark_label: "SOXX (Semiconductors ETF)", evidence_packet_at: "2026-10-09T21:30:00+00:00", computed_at: "2026-10-21T16:08:42+00:00" },
    weights: {}, disclaimer: DISCLAIMER, note: null,
  }, over || {});
}
const SIM = ppe({
  status: "SIMULATED_DATA", symbol: "NVDA",
  stretch: { status: "AVAILABLE", score: 58.7, label: "UPSIDE_STRETCHED", coverage_weight: 1.0, key_factors: ["4.2 ATR above EMA20"] },
  confidence: { status: "EVIDENCE_NOT_AVAILABLE", score: null, label: null, reasons: ["No stored Stage 5.4 evidence packet exists for this ticker as of this time. Research runs only on explicit request."] },
  sources: { mode: "simulated", market_data: "simulator (deterministic, seeded) · NOT a market feed · daily bars", price_source: "", benchmark_kind: "SECTOR_ETF",
             benchmark_label: "SOXX (Semiconductors ETF)", computed_at: "2026-10-21T16:08:42+00:00" },
  note: "SIMULATED DATA — synthetic prices from the development simulator. This PPE result demonstrates the display path and is not a market reading.",
});
const INSUFFICIENT = ppe({
  status: "INSUFFICIENT_DATA", symbol: "NEWCO",
  stretch: { status: "INSUFFICIENT_DATA", score: null, label: null, coverage_weight: 0.4, key_factors: [], note: "Only 40% of the component weight is available." },
  confidence: { status: "STRETCH_NOT_AVAILABLE", score: null, label: null, reasons: ["PPE Stretch is not available, so there is no move to classify."] },
});
const NORMAL = ppe({ symbol: "JPM", stretch: { status: "AVAILABLE", score: 11.5, label: "NORMAL", coverage_weight: 1.0, key_factors: ["0.4 ATR above EMA20"] },
                     confidence: { status: "NOT_APPLICABLE", score: null, label: null, reasons: ["Stretch is inside the normal band (|11.5| <= 40): there is no stretched move to classify."] } });
const WL = [{ symbol: "NVDA", price: 124.7, change_pct: 3.92, status: "OK" }, { symbol: "AMD", price: 891.9, change_pct: -0.29, status: "OK" },
            { symbol: "MU", price: null, change_pct: null, status: "UNAVAILABLE" }];
const Q = { symbol: "AAPL", status: "OK", price: 190.75, bid: 190.71, ask: 190.79, change_pct: 0.4, ts: 1792598936 };
const ALL_HTML = () => [R.ppeHtml(ppe()), R.ppeHtml(SIM), R.ppeHtml(INSUFFICIENT), R.ppeHtml(NORMAL), R.ppeHtml(null), R.ppeHtml(null, { loading: true, symbol: "MSFT" }),
                        R.ppeSourceHtml(ppe()), R.evidenceHtml(null), R.evidenceHtml({ status: "NOT_LOADED", message: "No stored evidence packet for this ticker." }),
                        R.evidenceHtml({ status: "FAILED", message: "Evidence unavailable (OperationalError)." }),
                        R.evidenceHtml({ status: "LOADED", strength: "WEAK", cause: "MIXED", company_confirm: "PARTIAL", market_confirm: "NOT_SUPPORTED", created_at: "2026-10-09T21:30:00+00:00", disclaimer: "Evidence strength describes the stored packet only." }),
                        R.watchlistRowsHtml(WL, "NVDA"), R.recentRowsHtml(["AAPL", "NVDA"], "NVDA"), R.skeletonHtml("POSITION", "JPM"), R.stateHtml("NO_POSITION", "NO POSITION"),
                        R.statusChips({ data_status: { mode: "simulated", position: "OK", evidence: "NOT_LOADED" }, ppe: { status: "SIMULATED_DATA" } }).map((c) => c.text).join(" ")];

test("1 active ticker is visually selected: exactly one row carries .active + aria-current", () => {
  const html = R.watchlistRowsHtml(WL, "NVDA");
  assert.match(html, /<li data-sym="NVDA" class="active" aria-current="true">/);
  assert.match(html, /<li data-sym="AMD" class="">/);
  assert.equal((html.match(/class="active"/g) || []).length, 1);
  assert.equal((html.match(/aria-current/g) || []).length, 1);
});

test("2 SIMULATED DATA badge in simulated mode (top bar view + PPE card tag)", () => {
  const s = R.sourceView("simulated", "simulator (deterministic, seeded) · NOT a market feed");
  assert.deepEqual([s.label, s.cls, s.simulated], ["SIMULATED DATA", "sim", true]);
  assert.equal(R.sourceView("auto", "").label, "SIMULATED DATA");                 // anything that is not "real" is labelled simulated
  assert.match(R.ppeBadgeHtml(SIM), /SIMULATED DATA/);
  assert.match(R.ppeHtml(SIM), /<span class="tv-tag warn">SIMULATED DATA<\/span>/);
  assert.equal(R.ppeModel(SIM).simulated, true);
});

test("3 a real source renders distinctly: ALPACA MARKET DATA, no simulated wording", () => {
  const s = R.sourceView("real", "Alpaca market data · IEX feed · polled, read only");
  assert.deepEqual([s.label, s.cls, s.simulated], ["ALPACA MARKET DATA", "real", false]);
  assert.equal(R.sourceView("real", "other vendor · read only").label, "LIVE MARKET DATA");
  const html = R.ppeHtml(ppe());
  assert.doesNotMatch(html, /SIMULATED/);
  assert.match(html, /<span class="tv-tag ok">SECTOR ETF<\/span>/);
  assert.equal(R.ppeModel(ppe()).simulated, false);
});

test("4 Stretch and Confidence are two separate blocks with their own value and band", () => {
  const html = R.ppeHtml(ppe());
  const stretch = html.match(/<div class="tv-ppe-block" data-ppe="stretch">[\s\S]*?<\/div>/)[0];
  const conf = html.match(/<div class="tv-ppe-block" data-ppe="confidence">[\s\S]*?<\/div>/)[0];
  assert.match(stretch, /<b class="tv-ppe-val down extreme">-82\.0<\/b><span class="tv-ppe-band down extreme">EXTREME DOWNSIDE<\/span>/);
  assert.match(conf, /<b class="tv-ppe-val ">38<\/b><span class="tv-ppe-band ">LOW<\/span>/);
  assert.ok(html.indexOf('data-ppe="stretch"') < html.indexOf('data-ppe="confidence"'));
  assert.doesNotMatch(conf, /\b(up|down)\b/);                                   // confidence is neutral: never up / down styled
  assert.match(html, /<div class="tv-ppe-meta"><span>Coverage<\/span><b>90%<\/b><\/div>/);
  assert.match(html, /Key factors<\/div><ul class="tv-ppe-list"><li>2\.6 ATR below EMA20<\/li>/);
  assert.match(html, /Evidence<\/div><div class="tv-ppe-ev">WEAK \/ MIXED · packet 2026-10-09 \(12d old\)<\/div>/);
  const up = R.ppeHtml(SIM);
  assert.match(up, /<b class="tv-ppe-val up">\+58\.7<\/b><span class="tv-ppe-band up">UPSIDE STRETCHED<\/span>/);
});

test("5 the exact PPE disclaimer is rendered (visible paragraph, not a tooltip)", () => {
  assert.match(R.ppeHtml(ppe()), new RegExp(`<p class="tv-disclaimer">${DISCLAIMER.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}</p>`));
  assert.doesNotMatch(R.ppeHtml(ppe(), { includeDisclaimer: false }), /tv-disclaimer/);   // the page appends its own copy in that mode
  assert.equal(R.ppeModel(ppe()).disclaimer, DISCLAIMER);
});

test("6 a confidence explainer exists (tooltip button + visible sentence) and does not read like a trade cue", () => {
  const html = R.ppeHtml(ppe());
  assert.match(html, /<button type="button" class="tv-info" data-ppe-info="confidence" title="Confidence: [^"]+" aria-label="What PPE Confidence means">\?<\/button>/);
  assert.match(html, /<p class="tv-explain">Stretch: how far price is displaced[^<]*Confidence: how well the stored evidence supports[^<]*<\/p>/);
  for (const s of [R.EXPLAIN_STRETCH, R.EXPLAIN_CONFIDENCE]) {
    assert.doesNotMatch(s, TRADE_WORDS);
    assert.doesNotMatch(s, /probability|reversal|win|target|expected/i);
  }
});

test("7 missing evidence renders an explicit state", () => {
  const nl = R.evidenceHtml({ status: "NOT_LOADED", message: "No stored evidence packet for this ticker." });
  assert.match(nl, /<div class="tv-state" data-state="EVIDENCE_NOT_LOADED">EVIDENCE NOT LOADED<\/div>/);
  assert.match(nl, /No stored evidence packet for this ticker\./);
  assert.match(R.evidenceHtml(null), /EVIDENCE NOT LOADED/);
  assert.match(R.evidenceHtml({ status: "FAILED", message: "Evidence unavailable (OperationalError)." }), /data-state="EVIDENCE_FAILED">EVIDENCE FAILED</);
  const ok = R.evidenceHtml({ status: "LOADED", strength: "WEAK", cause: "MIXED", company_confirm: "PARTIAL", market_confirm: "NOT_SUPPORTED", created_at: "2026-10-09T21:30:00+00:00", disclaimer: "x" });
  assert.match(ok, /<td>Strength<\/td><td>WEAK<\/td>[\s\S]*<td>Cause<\/td><td>MIXED<\/td>[\s\S]*<td>Company confirm<\/td><td>PARTIAL<\/td>[\s\S]*<td>Market confirm<\/td><td>NOT SUPPORTED<\/td>[\s\S]*<td>Last research<\/td><td>2026-10-09<\/td>/);
  assert.doesNotMatch(ok, /EVIDENCE NOT LOADED/);
});

test("8 missing PPE renders an explicit state, never a number", () => {
  for (const p of [null, undefined, { symbol: "X", status: "NOT_AVAILABLE" }]) {
    const html = R.ppeHtml(p, { symbol: "NVDA" });
    assert.match(html, /<div class="tv-state" data-state="PPE_NOT_AVAILABLE">PPE NOT AVAILABLE<\/div>/);
    assert.match(html, /No PPE result for NVDA yet\./);
    assert.doesNotMatch(text(html), /\d/);
    assert.equal(R.ppeModel(p).state, "NOT_LOADED");
  }
  const na = R.ppeHtml(ppe({ status: "NOT_AVAILABLE", stretch: { status: "NOT_AVAILABLE", note: "PPE unavailable (RuntimeError)." }, confidence: { status: "STRETCH_NOT_AVAILABLE", reasons: [] } }));
  assert.match(na, /data-state="PPE_NOT_AVAILABLE">PPE NOT AVAILABLE<\/div><p class="tv-dim">PPE unavailable \(RuntimeError\)\.<\/p>/);
  assert.match(na, /<b class="tv-ppe-val ">—<\/b><span class="tv-ppe-band ">NOT AVAILABLE<\/span>/);
});

test("9 insufficient PPE data / evidence not available / not applicable are explicit; no 0 or 0 % stands in for unavailable", () => {
  const ins = R.ppeHtml(INSUFFICIENT);
  assert.match(ins, /<div class="tv-state" data-state="PPE_INSUFFICIENT_DATA">PPE INSUFFICIENT DATA<\/div><p class="tv-dim">Only 40% of the component weight is available \(60% floor\)\.<\/p>/);
  assert.match(ins, /<div class="tv-ppe-block na" data-ppe="stretch">[\s\S]*?<b class="tv-ppe-val ">—<\/b><span class="tv-ppe-band ">INSUFFICIENT DATA<\/span>/);
  assert.match(ins, /<div class="tv-ppe-block na" data-ppe="confidence">[\s\S]*?<b class="tv-ppe-val ">—<\/b><span class="tv-ppe-band ">NOT AVAILABLE<\/span>/);
  assert.match(ins, /Coverage<\/span><b>40%<\/b>/);
  assert.doesNotMatch(text(ins), /\b0(\.0)?%?\b(?! floor)/);
  const sim = R.ppeHtml(SIM);
  assert.match(sim, /<div class="tv-state" data-state="PPE_EVIDENCE_NOT_AVAILABLE">PPE EVIDENCE NOT AVAILABLE<\/div><p class="tv-dim">No stored Stage 5\.4 evidence packet exists/);
  assert.match(sim, /data-ppe="confidence">[\s\S]*?<b class="tv-ppe-val ">—<\/b><span class="tv-ppe-band ">EVIDENCE NOT AVAILABLE<\/span>/);
  const normal = R.ppeHtml(NORMAL);
  assert.match(normal, /data-state="PPE_CONFIDENCE_NOT_APPLICABLE">CONFIDENCE NOT APPLICABLE</);
  assert.match(normal, /<b class="tv-ppe-val ">\+11\.5<\/b><span class="tv-ppe-band ">NORMAL<\/span>/);
  assert.equal(R.ppeModel({ symbol: "X", status: "AVAILABLE", stretch: { status: "AVAILABLE", score: 5 }, confidence: {} }).coverage, "—");   // missing coverage is "—", not 0 %
});

test("10 loading state during a ticker switch is a small skeleton for the NEW ticker", () => {
  const sk = R.skeletonHtml("PPE", "MSFT");
  assert.match(sk, /^<div class="tv-skel" data-state="LOADING" aria-busy="true"><div class="tv-state tv-state-dim">LOADING PPE · MSFT<\/div>/);
  assert.match(sk, /tv-skel-bar w70[\s\S]*tv-skel-bar w40/);
  assert.equal(R.ppeHtml(null, { loading: true, symbol: "MSFT" }), sk);
  assert.doesNotMatch(text(sk), /\d/);
});

test("11 a stale ticker's PPE stays ignored and rendered panels are tagged with the result's own symbol", () => {
  assert.equal(core.acceptsMessage({ type: "ppe", symbol: "SNDK", status: "AVAILABLE" }, "NVDA"), false);
  assert.equal(core.acceptsMessage({ type: "ppe", symbol: "NVDA" }, "NVDA"), true);
  assert.equal(R.ppeModel(ppe()).symbol, "SNDK");
  assert.equal(R.ppeModel(SIM).symbol, "NVDA");
  assert.match(VIEW_JS, /showLoading\(s\);/);                                   // a switch paints the new ticker's loading state first
  assert.match(VIEW_JS, /el\.dataset\.symbol = \(p && p\.symbol\) \|\| st\.active \|\| "";/);
});

test("12 Prepare Buy is a disabled button (enabled: false, disabled + aria-disabled, click ignored)", () => {
  assert.match(VIEW_JS, /\{ id: "prepare_buy", label: "Prepare Buy", enabled: false, status: "PREVIEW_NOT_CONNECTED"/);
  assert.match(VIEW_JS, /aria-disabled="\$\{a\.enabled \? "false" : "true"\}"\$\{a\.enabled \? "" : " disabled"\}/);
  assert.match(VIEW_JS, /if \(b && !b\.disabled\) onAction\(b\.dataset\.action\);/);
});

test("13 Prepare Sell is a disabled button with the same markup", () => {
  assert.match(VIEW_JS, /\{ id: "prepare_sell", label: "Prepare Sell", enabled: false, status: "PREVIEW_NOT_CONNECTED"/);
  assert.match(VIEW_JS, /String\(a\.status\)\.replace\(\/_\/g, " "\)\) : "disabled"\)\}<\/small>/);   // a disabled action says "disabled"; the full reason is its tooltip
  assert.doesNotMatch(VIEW_JS, /method\s*:\s*["'](POST|PUT|PATCH|DELETE)["']/);
});

test("14 connection transitions: CONNECTED / CONNECTING / RECONNECTING / DISCONNECTED and stale values while not live", () => {
  assert.deepEqual(R.connectionView("connected", 0), { state: "connected", label: "CONNECTED", cls: "ok", live: true, detail: "", text: "CONNECTED" });
  assert.deepEqual([R.connectionView("connecting").label, R.connectionView("connecting").cls], ["CONNECTING", "warn"]);
  const re = R.connectionView("reconnecting", 2);
  assert.deepEqual([re.label, re.cls, re.live, re.text], ["RECONNECTING", "warn", false, "RECONNECTING · try 3"]);
  assert.deepEqual([R.connectionView("disconnected").label, R.connectionView("disconnected").cls, R.connectionView("garbage").label], ["DISCONNECTED", "bad", "DISCONNECTED"]);
  assert.deepEqual([R.quoteView(Q, false).stale, R.quoteView(Q, false).label, R.quoteView(Q, true).stale, R.quoteView(Q, true).label], [true, "STALE", false, ""]);
});

test("15 the selected watchlist / recent row follows the active ticker", () => {
  assert.doesNotMatch(R.watchlistRowsHtml(WL, "AAPL"), /active/);              // the active ticker is not in the list: no row highlighted
  assert.match(R.watchlistRowsHtml(WL, "MU"), /<li data-sym="MU" class="active" aria-current="true">/);
  assert.match(R.recentRowsHtml(["AAPL", "NVDA"], "NVDA"), /<li data-sym="AAPL" class="">[\s\S]*<li data-sym="NVDA" class="active" aria-current="true">/);
  assert.match(R.watchlistRowsHtml(WL, "MU"), /<li data-sym="MU" class="active" aria-current="true"><span class="tv-sym">MU<\/span><span class="tv-px">—<\/span><span class="tv-chg ">—<\/span>/);   // unavailable: "—", never 0
  assert.match(R.watchlistRowsHtml(WL, "MU"), /<span class="tv-px">124\.70<\/span><span class="tv-chg up">\+3\.92%<\/span>/);
});

test("16 no BUY / SELL recommendation language anywhere in the rendered output", () => {
  for (const html of ALL_HTML()) assert.doesNotMatch(text(html), TRADE_WORDS, text(html).slice(0, 120));
  assert.doesNotMatch(R.EXPLAIN_STRETCH + R.EXPLAIN_CONFIDENCE, TRADE_WORDS);
});

test("17 no probability-of-rise / expected-return field or wording beyond the exact disclaimer", () => {
  const m = R.ppeModel(ppe());
  const keys = [...Object.keys(m), ...Object.keys(m.stretch), ...Object.keys(m.confidence)];
  for (const k of ["probability", "expected_return", "target", "signal", "recommendation", "reversal", "win"]) assert.ok(!keys.some((x) => x.toLowerCase().includes(k)), k);
  for (const html of ALL_HTML()) {
    const t = text(html);
    const n = (t.match(/probability/g) || []).length;
    assert.ok(n === 0 || (n === 1 && t.includes(DISCLAIMER)), t.slice(0, 160));
    assert.doesNotMatch(t, /expected return|price target|reversal probability|probability of (a )?(rise|recovery)/i);
  }
});

test("18 no order-flow / buyer-pressure / setup-readiness score element or export (Market Demand itself is Stage 5.7)", () => {
  for (const html of ALL_HTML()) assert.doesNotMatch(text(html), DEMAND_WORDS);
  assert.ok(!Object.keys(R).some((k) => /flow|readiness|pressure/i.test(k)), Object.keys(R).join(","));
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "trader_render.js"), "utf8"), DEMAND_WORDS);
  assert.doesNotMatch(VIEW_JS, DEMAND_WORDS);
});

test("19 quote states are explicit: LOADING, FEED UNAVAILABLE, NO DATA, INVALID SYMBOL", () => {
  assert.deepEqual([R.quoteView(null, true).state, R.quoteView({ status: "PENDING" }, true).label], ["LOADING", "LOADING"]);
  const feed = R.quoteView({ symbol: "NVDA", status: "UNAVAILABLE", code: "FEED_ERROR", message: "feed down" }, true);
  assert.deepEqual([feed.state, feed.price, feed.change, feed.message], ["FEED UNAVAILABLE", "—", "—", "feed down"]);
  assert.equal(R.quoteView({ status: "UNAVAILABLE", code: "NO_DATA" }, true).state, "NO DATA");
  assert.equal(R.quoteView({ status: "INVALID" }, true).state, "INVALID SYMBOL");
  const ok = R.quoteView(Q, true);
  assert.deepEqual([ok.state, ok.price, ok.change, ok.dir, ok.bid, ok.ask, ok.label], ["OK", "190.75", "+0.40%", "up", "190.71", "190.79", ""]);
});

test("20 status chips are plain words (no underscores), simulated data first", () => {
  const chips = R.statusChips({ data_status: { mode: "simulated", position: "OK", evidence: "NOT_LOADED" }, ppe: { status: "SIMULATED_DATA" }, market_state: { session_hint: "REGULAR" } });
  assert.deepEqual(chips.map((c) => c.text), ["simulated data", "positions ok", "evidence not loaded", "PPE simulated data", "session regular"]);
  assert.equal(chips[0].cls, "warn");
  assert.ok(chips.every((c) => !c.text.includes("_")));
  assert.equal(R.statusChips({ data_status: { mode: "real", market: "OK" } }, "real")[0].text, "market ok");
});
