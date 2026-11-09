// XSS-01 (frontend/app.js): the dashboard's HTML builders - the "XSS-01 render" block - run in a bare sandbox (no document, window, fetch or
// storage; only URL injected) against hostile payloads: every string is escaped, only an absolute http(s) URL becomes a link, attributes stay
// quoted, text round-trips (encoding, not stripping). A structural walk of EVERY HTML template in app.js proves that no interpolation escapes
// the encoder, that no attribute is interpolated unquoted and that every /api path interpolation is URL-encoded. The block sits after the
// EX-06 block and before api(); the three other modules with external links (X-1) carry the identical safeHref / extLink helpers. N15-N16 extend
// the guard to the other 39 frontend modules through xss01_raw_interpolations.json.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

const FRONT = path.join(__dirname, "..", "..", "frontend");
const SRC = fs.readFileSync(path.join(FRONT, "app.js"), "utf8");
const B0 = "// XSS-01 render: begin", B1 = "// XSS-01 render: end";
const A = SRC.indexOf(B0), B = SRC.indexOf(B1);
const BLOCK = SRC.slice(A, B);
const BLOCK_FUNCS = [...BLOCK.matchAll(/^function (\w+)\(/gm)].map((m) => m[1]);
const EXPORTS = ["esc", "safeHref", "extLink", "fmt", "pctClass", "viewBadgeClass", "eventRiskBadgeClass", ...BLOCK_FUNCS];

function sandbox(extra = "") {
  const ctx = { URL };                                                  // the only non-intrinsic the block needs (safeHref); a DOM reference throws
  vm.runInNewContext(BLOCK + extra + `\nthis.__x = { ${EXPORTS.join(", ")} };`, ctx, { filename: "app.js[xss01-block]" });
  return Object.assign(ctx.__x, ctx.__cols ? { __cols: ctx.__cols } : {});
}

// ---- hostile fixtures ---------------------------------------------------------------------------------------------------------------
const P_IMG = '<img src=x onerror="window.__xss=(window.__xss||0)+1">';
const P_SVG = '<svg onload="window.__xss=99">';
const P_SCRIPT = "<script>window.__xss=7</script>";
const P_ATTR = '"><b>x</b>';
const P_QUOTE = "' onmouseover='window.__xss=5";
const P_JS_URL = "javascript:window.__xss=3";
const P_DATA_URL = "data:text/html,<script>window.__xss=4</script>";
const P_REL_URL = "/api/execution/auto/enable";
const P_OK_URL = 'https://example.com/a?b=c&d="e"';
const P_MIXED = `Tom & "Jerry" <3 'quotes'`;
const P_MIXED_ESC = "Tom &amp; &quot;Jerry&quot; &lt;3 &#39;quotes&#39;";
// Escaped text cannot contain < or >, so every <...> in an output is real markup: it must be a tag the templates write, with no handler
// attribute, no javascript: URL and no srcdoc; and no hostile payload may appear unescaped.
const TEMPLATE_TAGS = new Set(["div", "span", "strong", "a", "p", "h2", "h3", "h4", "ul", "li", "br", "button", "table", "tr", "th", "td"]);
const PAYLOADS = [P_IMG, P_SVG, P_SCRIPT, "<b>x</b>", P_QUOTE, "href=\"javascript:", "href='javascript:"];
function assertSafe(html, label = "") {
  for (const tag of String(html).match(/<[^>]*>/g) || []) {
    const name = (tag.match(/^<\/?([a-zA-Z][\w-]*)/) || [])[1];
    assert.ok(name && TEMPLATE_TAGS.has(name.toLowerCase()), `${label} unexpected tag ${tag.slice(0, 80)}`);
    const skeleton = tag.replace(/="[^"]*"/g, '=""');                                             // attribute names only: a value may legitimately read "onerror=" as text
    assert.doesNotMatch(skeleton, /\son[a-z]+\s*=|\ssrcdoc|\sstyle=""\s|='|=[^"\s>]/i, `${label} hostile or unquoted attribute in ${tag.slice(0, 80)}`);
    assert.doesNotMatch(tag, /href="\s*javascript:|src="\s*javascript:|style="[^"]*(expression|url\()/i, `${label} hostile attribute value in ${tag.slice(0, 80)}`);
  }
  for (const p of PAYLOADS) assert.ok(!String(html).includes(p), `${label} raw payload present: ${p.slice(0, 40)}`);
}
// kept for the regex form some tests use on text they expect to be fully escaped
const RAW = /<img|<svg|<script|<b>x<\/b>|href="javascript:/i;

const EV = (o = {}) => ({ event_id: "ev-1", event_type: "EARNINGS", category: "COMPANY", symbol: "NVDA", title: "Q3 results", event_date: "2026-11-12",
                          event_time: null, timezone: null, event_datetime_utc: null, time_precision: "DATE_ONLY", source: "Wire", source_url: P_OK_URL,
                          source_provider: "ALPACA", status: "UPCOMING", importance: "HIGH", confirmed: true, ...o });
const EVENTS = (ev, o = {}) => ({ company_events: [ev], macro_events: [], event_risk_level: "HIGH", earnings_available: true, earnings_reason: null, ...o });
const METRICS = (p) => ({ symbol: p, price: 101.5, pct_change: 1.25, rsi: 55.1, ema_fast: 100, ema_medium: 99, ema_slow: 95, trend: p, atr: 2.5, relative_volume: 1.4,
                          volume_expansion: 1.1, support: 98, resistance: 105, dist_from_support_pct: 3.4, dist_from_resistance_pct: 3.3, momentum_5d_pct: 2.1,
                          momentum_10d_pct: 4.2, volatility_pct: 31.2, signal: p, attention_score: p });   // a string score must not carry markup (fmt.score coerces)
const RESEARCH = (p, url = P_JS_URL) => ({
  symbol: p, metrics: METRICS(p), evidence: { bullish_pct: 50, neutral_pct: 30, bearish_pct: 20 }, research_view: p, research_view_disclaimer: p,
  data_quality: { level: p, explanation: p }, narrative: { whats_happening: p, why: p, whats_good: [p], be_careful: [p], what_to_watch: p },
  catalysts: [{ title: p, source: p, published_at: p, url, summary: p, sentiment: p, reason: p }], catalyst_note: p,
  events: EVENTS(EV({ title: p, source: p, source_url: url, event_type: p, status: "UPCOMING", time_precision: p, source_provider: p, event_id: p }), { event_risk_level: p, earnings_available: false, earnings_reason: p }),
  risk_flags: [{ code: p, severity: p, description: p }], risk_explanation: p,
  sector: { sector_name: p, sector_etf: p, sector_pct_change: 0.5, peer_avg_pct_change: 0.4, stock_vs_sector_pct: 0.7, market_proxy_symbol: p, market_pct_change: 0.2 },
  analysis_id: "a-1",
});
const SNAPSHOT = (p) => ({
  id: 7, symbol: p, historical_validation: true, created_at: "2026-11-01T14:00:00Z", market_timestamp: p, price: 100, price_source: p, price_timestamp: "2026-11-01T14:00:00Z",
  bullish_pct: 50, neutral_pct: 30, bearish_pct: 20, research_view: p, scanner_signal: p, data_quality_level: p,
  catalysts: [{ title: p, sentiment: p }], risk_flags: [{ code: p, severity: p, description: p }],
  events: EVENTS(EV({ title: p, source: p, source_url: P_JS_URL, status: "RELEASED" }), { event_risk_level: p }), narrative: { whats_happening: p, why: p },
  outcomes: [{ horizon_trading_days: 1, status: "ERROR", error_message: p }, { horizon_trading_days: 3, status: "PENDING" },
             { horizon_trading_days: 5, status: "COMPLETED", return_pct: 1.5, price_at_horizon: 101.5, max_favorable_excursion_pct: 2, max_adverse_excursion_pct: -1,
               did_hit_target_1: true, did_hit_invalidation: false, sequencing_ambiguous: true }],
});
const count = (s, re) => (String(s).match(re) || []).length;

// ---- N1-N3: eventLineHtml and the link rule -------------------------------------------------------------------------------------------
test("N1 eventLineHtml: a hostile title, source and javascript: URL render as text, never as markup or a link", () => {
  const x = sandbox();
  const html = x.eventLineHtml(EV({ title: P_IMG, source: P_SVG, source_url: P_JS_URL }));
  assertSafe(html, "N1");
  assert.ok(html.includes("&lt;img src=x onerror=&quot;") && html.includes("&lt;svg onload=&quot;"));
  assert.ok(!html.includes("<a"), html);
  // the link branch: a hostile source text on an allowed https URL is escaped inside the anchor, and the href is the normalised URL
  const linked = x.eventLineHtml(EV({ title: P_IMG, source: P_SVG, source_url: P_OK_URL }));
  assertSafe(linked, "N1-link");
  assert.ok(linked.includes('<a href="https://example.com/a?b=c&amp;d=%22e%22" target="_blank" rel="noopener noreferrer">&lt;svg onload=&quot;window.__xss=99&quot;&gt;</a>'), linked);
  assert.equal(count(linked, /<a /g), 1);
});

test("N2 eventLineHtml: an absolute https URL is exactly one link (query quotes percent-encoded by URL, & encoded by esc), no referrer, no opener", () => {
  const x = sandbox();
  const html = x.eventLineHtml(EV());
  assert.equal(count(html, /<a /g), 1);
  assert.ok(html.includes('<a href="https://example.com/a?b=c&amp;d=%22e%22" target="_blank" rel="noopener noreferrer">Wire</a>'), html);
});

test("N3 eventLineHtml: data:, relative, empty, null, numeric and object URLs are not links; the source text still shows", () => {
  const x = sandbox();
  for (const u of [P_DATA_URL, P_REL_URL, "", null, 42, {}, "//evil.test/x", "example.com/x", "ftp://example.com/x"]) {
    const html = x.eventLineHtml(EV({ source_url: u, source: "Wire & Co" }));
    assert.ok(!html.includes("<a"), String(u));
    assert.ok(html.includes("Source: Wire &amp; Co"), String(u));
  }
});

// ---- N4-N5: the advanced grid and the risk card -----------------------------------------------------------------------------------
test("N4 eventAdvancedDetailsHtml: every string field escaped (attribute break-out and <script> as text); six label cells per event", () => {
  const x = sandbox();
  const html = x.eventAdvancedDetailsHtml(EVENTS(EV({ title: P_ATTR, event_type: P_ATTR, status: P_SCRIPT, time_precision: P_ATTR, source_provider: P_ATTR, event_id: P_ATTR })));
  assertSafe(html, "N4");
  assert.ok(count(html, /&quot;&gt;&lt;b&gt;x&lt;\/b&gt;/g) >= 5, html);
  assert.equal(count(html, /<div class="label">/g), 6);
});

test("N5 eventRiskCardHtml: the class attribute cannot be broken out of; the earnings reason is escaped", () => {
  const x = sandbox();
  const html = x.eventRiskCardHtml(EVENTS(EV(), { company_events: [], event_risk_level: P_QUOTE, earnings_available: false, earnings_reason: P_IMG }));
  assertSafe(html, "N5");
  assert.ok(html.includes('class="event-risk-badge event-risk-NONE"'), html);                       // the class helper maps unknown levels to a fixed word
  assert.ok(html.includes("&#39; onmouseover=&#39;window.__xss=5"), html);
  assert.ok(x.eventRiskCardHtml(null).includes("Event data unavailable"));
});

// ---- N6-N8: the three named functions -------------------------------------------------------------------------------------------------
test("N6 researchDetailHtml: every string field of a research bundle set to <img onerror> renders as text; the catalyst's javascript: URL is not a link; structure intact", () => {
  const x = sandbox();
  const html = x.researchDetailHtml(RESEARCH(P_IMG));
  assertSafe(html, "N6");
  assert.ok(count(html, /&lt;img src=x onerror=/g) >= 30, count(html, /&lt;img/g));
  assert.equal(count(html, /<a /g), 0);
  for (const id of ["beginner-panel", "advanced-panel", "save-snapshot-btn", "save-snapshot-msg"]) assert.equal(count(html, new RegExp(`id="${id}"`, "g")), 1, id);
  assert.ok(html.includes("NaN/100"));                                                               // fmt.score coerces: a string score cannot carry markup
  const ok = x.researchDetailHtml(RESEARCH("NVDA", P_OK_URL));
  assert.equal(count(ok, /<a href="https:\/\/example\.com\/a\?b=c&amp;d=%22e%22" target="_blank" rel="noopener noreferrer">/g), 2);   // the catalyst and the event
});

test("N7 snapshotDetailHtml: a stored snapshot with hostile narrative, catalysts, risk flags, events, timestamps and outcome errors renders as text", () => {
  const x = sandbox();
  const html = x.snapshotDetailHtml(SNAPSHOT(P_IMG));
  assertSafe(html, "N7");
  assert.ok(count(html, /&lt;img src=x onerror=/g) >= 14, count(html, /&lt;img/g));
  assert.ok(html.includes("HISTORICAL VALIDATION") && html.includes("PENDING — not enough trading sessions") && html.includes("Target 1: HIT"));
  assert.ok(!html.includes("<a"), "the stored event's javascript: URL is not a link");
});

test("N8 chatMessageHtml: the answer, the tool names and the tool arguments are escaped; the role label is fixed text", () => {
  const x = sandbox();
  const html = x.chatMessageHtml("assistant", P_SCRIPT + P_IMG, [{ tool: P_ATTR, input: { a: P_IMG, b: 1 }, available: true }]);
  assertSafe(html, "N8");
  assert.ok(html.startsWith('<span class="role">Assistant</span>&lt;script&gt;'), html);
  assert.ok(html.includes('<div class="chat-tool-calls">Used: &quot;&gt;&lt;b&gt;x&lt;/b&gt;(&lt;img'), html);
  assert.equal(x.chatMessageHtml("user", "hi"), '<span class="role">You</span>hi');
  assert.equal(x.chatMessageHtml("assistant", "hi", []), '<span class="role">Assistant</span>hi');
});

// ---- N9-N10: encoding, not stripping; the column renderers and small builders -------------------------------------------------------
test("N9 encoding round-trip: & \" < ' are entities, nothing is removed (esc, chat text, narrative, catalyst title)", () => {
  const x = sandbox();
  assert.equal(x.esc(P_MIXED), P_MIXED_ESC);
  assert.equal(x.esc(null), "");
  assert.equal(x.esc(0), "0");
  assert.ok(x.chatMessageHtml("user", P_MIXED).includes(P_MIXED_ESC));
  const html = x.researchDetailHtml({ ...RESEARCH("NVDA", P_OK_URL), narrative: { whats_happening: P_MIXED, why: "", whats_good: [], be_careful: [], what_to_watch: "" } });
  assert.ok(html.includes(`<p>${P_MIXED_ESC}</p>`) && html.includes("<li>(none)</li>"));
});

test("N10 column renderers, group rows and outcome cells: symbols, signals, labels and warnings are escaped; attributes stay quoted", () => {
  const cols = SRC.slice(SRC.indexOf("const moverColumns = ["), SRC.indexOf("function renderOpportunities("));
  const x = sandbox("\n" + cols + "\nthis.__cols = { moverColumns, levelColumns, volColumns };");
  assert.ok(cols.includes("render: (r) => `<strong>${esc(r.symbol)}</strong>`") && !/\$\{r\.(symbol|signal)\}/.test(cols), cols);
  const hostileRow = new Proxy({}, { get: () => P_IMG });                                   // every field of every row is hostile, numeric fields included
  let rendered = 0;
  for (const [name, columns] of Object.entries(x.__cols)) {
    for (const c of columns) { assertSafe(c.render(hostileRow), `N10 ${name}.${c.label}`); rendered++; }
  }
  assert.equal(rendered, 11);
  const row = x.groupStatsRowHtml({ label: P_IMG, n: P_ATTR, avg_return_pct: 1, median_return_pct: 1, positive_return_frequency: 0.5, avg_mfe_pct: 1, avg_mae_pct: -1, sample_warning: P_SVG });
  assertSafe(row, "N10");
  assert.ok(row.includes("<td>&lt;img") && row.includes("<td>50%</td>") && row.includes('<span class="hint">&lt;svg'), row);
  assertSafe(x.groupStatsTableHtml(P_IMG, [{ label: P_IMG, n: P_ATTR, avg_return_pct: 1, median_return_pct: 1, positive_return_frequency: 0.5, avg_mfe_pct: 1, avg_mae_pct: -1, sample_warning: P_SVG }]), "N10 groupStatsTableHtml");
  assert.equal(x.groupStatsTableHtml("t", []), "");
  assert.equal(x.outcomeCellHtml([{ horizon_trading_days: 1, status: "COMPLETED", return_pct: 2 }], 1), '<span class="pct-up">+2.00%</span>');
  assert.equal(x.outcomeCellHtml([], 1), '<span class="hint">N/A</span>');
  for (const [k, want] of [["price", "$NaN"], ["pct", "NaN%"], ["num", "NaN"], ["rvol", "NaNx"], ["score", "NaN/100"]]) assert.equal(x.fmt[k](P_IMG), want, `fmt.${k} coerces its argument`);   // fmt is exempt from the walk because of this
  assert.equal(x.fmt.score(72), "72/100");
});

// ---- N11: the structural walk (no interpolation escapes the encoder; attributes quoted; /api paths encoded) --------------------------
const SAFE_CALL = new RegExp(`^(esc|extLink|fmt\\.(price|pct|num|rvol)|pctClass|viewBadgeClass|eventRiskBadgeClass|c\\.render|${BLOCK_FUNCS.join("|")})\\(`);

function scanTemplates(src, comments = []) {
  // every top-level template literal in the source, with its start offset; comments and quoted strings are skipped; nesting is tracked
  const out = [];
  let i = 0, n = src.length;
  const stack = [];                                                      // "`" for a template, "${" for an interpolation
  let start = -1;
  while (i < n) {
    const c = src[i], d = src[i + 1];
    const top = stack[stack.length - 1];
    if (top === "`") {
      if (c === "\\") { i += 2; continue; }
      if (c === "`") { stack.pop(); if (!stack.length) { out.push({ text: src.slice(start, i + 1), start }); start = -1; } i++; continue; }
      if (c === "$" && d === "{") { stack.push("${"); i += 2; continue; }
      i++; continue;
    }
    // code (top-level or inside ${ })
    if (c === "/" && d === "/") { const e = src.indexOf("\n", i); comments.push([i, e < 0 ? n : e]); if (e < 0) break; i = e; continue; }
    if (c === "/" && d === "*") { const e = src.indexOf("*/", i) + 2; if (e < 2) break; comments.push([i, e]); i = e; continue; }
    if (c === "/") {                                                     // a regex literal when it follows an operator / opener / keyword, not an operand
      const prev = src.slice(0, i).replace(/\s+$/, "");
      if (/(^|[(,=:\[!&|?{};+\-*%<>~^]|\breturn|\btypeof|\bcase)$/.test(prev)) {
        const re = i;
        let j = i + 1, cls = false;
        while (j < n) { const ch = src[j]; if (ch === "\\") { j += 2; continue; } if (ch === "[") cls = true; else if (ch === "]") cls = false; else if (ch === "/" && !cls) break; j++; }
        i = j + 1; while (i < n && /[a-z]/.test(src[i])) i++;         // flags
        comments.push([re, i]);
        continue;
      }
    }
    if (c === '"' || c === "'") { let j = i + 1; while (j < n && src[j] !== c) { if (src[j] === "\\") j++; j++; } i = j + 1; continue; }
    if (c === "`") { if (!stack.length) start = i; stack.push("`"); i++; continue; }
    if (c === "{" && (top === "${" || top === "{")) { stack.push("{"); i++; continue; }
    if (c === "}" && top === "{") { stack.pop(); i++; continue; }
    if (c === "}" && top === "${") { stack.pop(); i++; continue; }
    i++;
  }
  return out;
}

function splitTemplate(tpl) {
  // tpl includes its backticks; returns [{lit: "..."}, {expr: "..."}, ...] at the template's own level
  const parts = [];
  let i = 1, lit = "";
  while (i < tpl.length - 1) {
    const c = tpl[i];
    if (c === "\\") { lit += tpl.slice(i, i + 2); i += 2; continue; }
    if (c === "$" && tpl[i + 1] === "{") {
      parts.push({ lit }); lit = "";
      const end = skipExpr(tpl, i + 2);                                 // just after the matching }
      parts.push({ expr: tpl.slice(i + 2, end - 1) });
      i = end; continue;
    }
    lit += c; i++;
  }
  parts.push({ lit });
  return parts;
}

function stripParens(e) {
  e = e.trim();
  while (e.startsWith("(") && e.endsWith(")") && balancedAt(e, 0) === e.length - 1) e = e.slice(1, -1).trim();
  return e;
}
function balancedAt(s, i) {
  // index of the bracket closing the one at i, skipping strings and templates
  const open = s[i], close = { "(": ")", "[": "]", "{": "}" }[open];
  let depth = 0;
  for (let j = i; j < s.length; j++) {
    const c = s[j];
    if (c === '"' || c === "'" || c === "`") { j = skipString(s, j); continue; }
    if (c === open) depth++; else if (c === close) { depth--; if (!depth) return j; }
  }
  return -1;
}
function skipString(s, j) {
  // j at the opening quote; returns the index of the closing quote (a template's ${...} expressions are skipped recursively)
  const q = s[j]; let k = j + 1;
  while (k < s.length) {
    const c = s[k];
    if (c === "\\") { k += 2; continue; }
    if (q === "`" && c === "$" && s[k + 1] === "{") { k = skipExpr(s, k + 2); continue; }
    if (c === q) return k;
    k++;
  }
  return s.length;
}
function skipExpr(s, k) {
  // k just after "${" (or "{"); returns the index just after the matching "}", skipping nested strings, templates and braces
  let depth = 1;
  while (k < s.length) {
    const c = s[k];
    if (c === '"' || c === "'" || c === "`") { k = skipString(s, k) + 1; continue; }
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (!depth) return k + 1; }
    k++;
  }
  return s.length;
}
function topLevelSplit(e, ops) {
  // split e at top-level occurrences of any op (longest first), outside strings / templates / brackets
  const parts = []; let depth = 0, last = 0;
  for (let i = 0; i < e.length; i++) {
    const c = e[i];
    if (c === '"' || c === "'" || c === "`") { i = skipString(e, i); continue; }
    if ("([{".includes(c)) depth++; else if (")]}".includes(c)) depth--;
    if (depth) continue;
    for (const op of ops) if (e.startsWith(op, i) && !(op === "+" && e[i + 1] === "+") ) { parts.push(e.slice(last, i)); last = i + op.length; i += op.length - 1; break; }
  }
  parts.push(e.slice(last));
  return parts.length > 1 ? parts : null;
}
function ternary(e) {
  // a ? b : c at the top level (right-associative): returns [cond, then, else] or null
  let depth = 0, q = -1;
  for (let i = 0; i < e.length; i++) {
    const c = e[i];
    if (c === '"' || c === "'" || c === "`") { i = skipString(e, i); continue; }
    if ("([{".includes(c)) depth++; else if (")]}".includes(c)) depth--;
    if (depth) continue;
    if (c === "?" && e[i + 1] !== "?" && e[i + 1] !== "." && e[i - 1] !== "?") { q = i; break; }
  }
  if (q < 0) return null;
  let nest = 0; depth = 0;
  for (let i = q + 1; i < e.length; i++) {
    const c = e[i];
    if (c === '"' || c === "'" || c === "`") { i = skipString(e, i); continue; }
    if ("([{".includes(c)) depth++; else if (")]}".includes(c)) depth--;
    if (depth) continue;
    if (c === "?" && e[i + 1] !== "?" && e[i + 1] !== ".") nest++;
    else if (c === ":" ) { if (!nest) return [e.slice(0, q), e.slice(q + 1, i), e.slice(i + 1)]; nest--; }
  }
  return null;
}

const problems = [];
let resolved = {};                                                                   // bare identifier -> how many times an interpolation resolved it
function classify(expr, where, src, pos, seen = new Set()) {
  let e = stripParens(expr);
  if (!e) return;
  const t = ternary(e);                                                              // split first: "<p>" + err.message + "</p>" starts with a quote and is not a literal
  if (t) { classify(t[1], where, src, pos, seen); classify(t[2], where, src, pos, seen); return; }
  const ops = topLevelSplit(e, ["||", "&&", "??", "+"]);
  if (ops) { for (const p of ops) classify(p, where, src, pos, seen); return; }
  if ((e[0] === '"' || e[0] === "'") && skipString(e, 0) === e.length - 1) return;   // one whole string literal
  if (/^-?\d+(\.\d+)?$/.test(e) || e === "null") return;                              // a number literal, or null
  if (e[0] === "`" && skipString(e, 0) === e.length - 1) { checkTemplate(e, where, src, pos); return; }   // one whole nested template: scanned with the same rule
  if (/^fmt\.score\(/.test(e)) { problems.push(`${where}: fmt.score is not exempt, wrap it: ${e}`); return; }
  if (SAFE_CALL.test(e)) {                                                            // the safe call must be the WHOLE leaf: esc(x).replace(...) is not esc(x)
    if (balancedAt(e, e.indexOf("(")) === e.length - 1) return;
    problems.push(`${where}: a safe call is not the whole interpolation: \${${e.slice(0, 80)}}`);
    return;
  }
  const m = e.match(/^(.*)\.map\(/s);
  if (m && /\)\s*\.join\(/.test(e)) {                                                // xs.map(fn).join(...), possibly across lines
    const argStart = e.indexOf(".map(") + 4, argEnd = balancedAt(e, argStart);
    if (!/^\s*\.join\(\s*("[^"]*"|'[^']*')?\s*\)$/.test(e.slice(argEnd + 1))) {        // nothing may follow the join: ....join("").replace(...) is not escaped text
      problems.push(`${where}: something follows the map / join: \${${e.slice(0, 80)}}`);
      return;
    }
    const arg = e.slice(argStart + 1, argEnd).trim();
    if (BLOCK_FUNCS.includes(arg)) return;
    const arrow = arg.indexOf("=>");
    if (arrow < 0) { problems.push(`${where}: unknown map callback ${arg.slice(0, 60)}`); return; }
    let body = arg.slice(arrow + 2).trim();
    if (body.startsWith("{")) {                                                        // a block body: every `return <expr>;`
      for (const r of body.matchAll(/\breturn\s+/g)) {
        let k = r.index + r[0].length, depth = 0, s = k;
        for (; k < body.length; k++) { const c = body[k]; if (c === '"' || c === "'" || c === "`") { k = skipString(body, k); continue; } if ("([{".includes(c)) depth++; else if (")]}".includes(c)) depth--; if (!depth && c === ";") break; if (depth < 0) break; }
        classify(body.slice(s, k), where, src, pos, seen);
      }
      return;
    }
    classify(body, where, src, pos, seen); return;
  }
  if (/^[A-Za-z_]\w*$/.test(e)) {                                                    // an identifier: resolve its nearest declaration before this template
    resolved[e] = (resolved[e] || 0) + 1;
    if (seen.has(e)) return;
    seen.add(e);
    const before = src.slice(0, pos);
    const decl = [...before.matchAll(new RegExp(`\\b(?:const|let|var)\\s+${e}\\s*=\\s*`, "g"))].pop();
    if (!decl) { problems.push(`${where}: unresolved identifier ${e}`); return; }
    const rhsStart = decl.index + decl[0].length;
    const rhs = statementAt(src, rhsStart);
    classify(rhs, `${where} <- ${e}`, src, rhsStart, seen);
    for (const add of src.slice(rhsStart, pos).matchAll(new RegExp(`\\b${e}\\s*(?:\\+=|\\|\\|=|\\?\\?=|&&=|=(?![=>]))\\s*`, "g"))) {     // every later `e += <expr>` and `e = <expr>`
      const s2 = rhsStart + add.index + add[0].length;
      classify(statementAt(src, s2), `${where} <- ${e} reassigned`, src, s2, seen);
    }
    return;
  }
  problems.push(`${where}: unescaped interpolation \${${e.slice(0, 80)}}`);
}
function statementAt(src, i) {
  let depth = 0;
  for (let k = i; k < src.length; k++) {
    const c = src[k];
    if (c === '"' || c === "'" || c === "`") { k = skipString(src, k); continue; }
    if ("([{".includes(c)) depth++; else if (")]}".includes(c)) depth--;
    if (!depth && c === ";") return src.slice(i, k);
  }
  return src.slice(i);
}
const URL_ATTR_BEFORE = /\b(href|src|srcset|srcdoc|action|formaction|poster|background|xlink:href|on[a-z]+)\s*=\s*["'][^"']*$/i;   // an attribute that a browser navigates to, loads or runs
function checkTemplate(tpl, where, src, pos) {
  const parts = splitTemplate(tpl);
  let inTag = false, quote = null;                                                   // the HTML state where the literal text stopped
  for (let k = 0; k < parts.length; k++) {
    const p = parts[k];
    if ("lit" in p) {
      if (k + 1 < parts.length && /=\s*$|=[^"'\s<>]+$/.test(p.lit)) problems.push(`${where}: unquoted attribute before \${...}: ...${p.lit.slice(-40)}`);   // class=${x} and class=badge-${x}
      for (let i = 0; i < p.lit.length; i++) {
        const c = p.lit[i];
        if (quote) { if (c === quote) quote = null; }
        else if (inTag) { if (c === '"' || c === "'") quote = c; else if (c === ">") inTag = false; }
        else if (c === "<" && /[a-zA-Z!/]/.test(p.lit[i + 1] || "")) inTag = true;
      }
    } else {
      if (inTag && !quote) problems.push(`${where}: interpolation in a tag, outside a quoted attribute value: \${${p.expr.trim().slice(0, 60)}}`);   // <span ${esc(attrs)}>
      const before = parts[k - 1].lit.match(URL_ATTR_BEFORE);                        // only extLink's checked h may fill one, whatever the quote style; esc() does not check a scheme
      if (before && p.expr.trim() !== "h") problems.push(`${where}: a URL or handler attribute built from \${${p.expr.trim().slice(0, 60)}}: ${before[1]}`);
      classify(p.expr, where, src, pos);
    }
  }
}
function bodyUntilClose(src, i) {
  // the arrow function body starting at i, up to the } that closes the enclosing object literal
  let depth = 0;
  for (let k = i; k < src.length; k++) {
    const c = src[k];
    if (c === '"' || c === "'" || c === "`") { k = skipString(src, k); continue; }
    if ("([{".includes(c)) depth++;
    else if (c === ")" || c === "]") depth--;
    else if (c === "}") { if (!depth) return src.slice(i, k).trim().replace(/,$/, ""); depth--; }
  }
  return src.slice(i);
}
function returnsIn(body) {
  // the offset after each `return` of a function body that is code, not text inside a string or template (a template's own expressions are walked by classify)
  const out = [];
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (c === '"' || c === "'" || c === "`") { i = skipString(body, i); continue; }
    if (c === "/" && body[i + 1] === "/") { i = body.indexOf("\n", i); if (i < 0) break; continue; }
    if (body.startsWith("return", i) && !/\w/.test(body[i - 1] || " ") && !/\w/.test(body[i + 6] || " ")) out.push(i + 6);
  }
  return out;
}

test("N11 structural walk: every HTML template in app.js escapes every leaf interpolation, quotes every attribute, encodes every /api path parameter", () => {
  problems.length = 0;
  resolved = {};
  const templates = scanTemplates(SRC);
  // A template with a tag anywhere in the file is HTML. So is EVERY template in the render block and in the table columns' render functions, tag or
  // not, because their results reach innerHTML through a builder or c.render(r), which the walk trusts by name. Only fmt's number formatters are exempt.
  const offset = (needle) => { const i = SRC.indexOf(needle); assert.ok(i >= 0, needle); return i; };
  const inRange = (t, a, b) => t.start >= a && t.start < b;
  const fmtA = offset("const fmt = {"), fmtB = SRC.indexOf("};", fmtA);
  const colA = offset("const moverColumns = ["), colB = offset("function renderOpportunities(");
  const html = templates.filter((t) => /<[a-zA-Z!/]/.test(t.text.replace(/\$\{[\s\S]*?\}/g, "")) || (inRange(t, A, B) && !inRange(t, fmtA, fmtB)) || inRange(t, colA, colB));
  assert.ok(html.length >= 45, `found ${html.length} HTML templates`);
  assert.equal(count(SRC, /\b(const|let|var|function)\s+esc\b/g), 1, "esc is defined once: a shadowing definition would defeat the walk by name");
  const at = (i) => `app.js@${SRC.slice(0, i).split("\n").length}`;
  for (const t of html) checkTemplate(t.text, at(t.start), SRC, t.start);
  // c.render(r) and a builder call are trusted by name, so what they return is walked too, template or not
  let bodies = 0;
  for (const m of SRC.matchAll(/\brender:\s*(?:\(\s*\w*\s*\)|\w+)\s*=>\s*/g)) {
    const s = m.index + m[0].length;
    classify(bodyUntilClose(SRC, s), `${at(s)} column render`, SRC, s);
    bodies++;
  }
  let returns = 0;
  for (const name of BLOCK_FUNCS) {
    const fn = SRC.indexOf(`function ${name}(`), open = SRC.indexOf("{", SRC.indexOf(")", fn)), close = balancedAt(SRC, open);
    for (const off of returnsIn(SRC.slice(open, close))) {
      const s = open + off;
      classify(statementAt(SRC, s), `${at(s)} return of ${name}`, SRC, s);
      returns++;
    }
  }
  assert.ok(returns >= 12, `walked ${returns} builder returns`);
  for (const t of templates) {                                                             // every /api path with a parameter is URL-encoded
    if (/^`\/api\//.test(t.text) && t.text.includes("${")) {
      for (const p of splitTemplate(t.text)) if ("expr" in p && !/^encodeURIComponent\(/.test(p.expr.trim())) problems.push(`app.js@${SRC.slice(0, t.start).split("\n").length}: unencoded path parameter \${${p.expr}}`);
    }
  }
  for (const m of SRC.matchAll(/\[\s*["'`]innerHTML/g)) problems.push(`app.js@${SRC.slice(0, m.index).split("\n").length}: innerHTML in bracket form`);
  for (const m of SRC.matchAll(/\.innerHTML\s*(?:\+|\|\||\?\?|&&)?=(?!=)\s*/g)) {          // every innerHTML right-hand side, whatever its shape
    const s = m.index + m[0].length;
    classify(statementAt(SRC, s), `app.js@${SRC.slice(0, s).split("\n").length} innerHTML`, SRC, s);
  }
  assert.deepEqual(problems, []);
  assert.equal(bodies, 11, "every column's render body was walked");
  assert.equal(count(SRC.slice(B + B1.length), /\brender\s*:/g), 11, "a render key in another form is not walked");
  // The walk resolves a bare identifier to its nearest earlier declaration, with no notion of scope, so a parameter or a shadow could borrow a safe declaration. Every use is therefore listed.
  assert.deepEqual(resolved, { h: 2, tentative: 2, earningsLine: 2, toolsHtml: 2, header: 1, body: 1, html: 1 });
  assert.equal(count(SRC, /\.innerHTML\s*\+?=/g), 27);                                      // the sink count is unchanged by XSS-01
});

// ---- N12-N13: the block's purity and position; safeHref -------------------------------------------------------------------------------
test("N12 the render block has no DOM, network or storage reference, follows the EX-06 block and REFRESH_MS, and precedes api(); nothing but comments precede EX-06", () => {
  assert.ok(A > 0 && B > A, "markers present");
  // references (member access or call), not prose: template text may say "window" or "document"
  assert.doesNotMatch(BLOCK, /\b(document|window|navigator|localStorage|sessionStorage|location|modal|globalThis|self|top|parent)\s*(\.\s*[A-Za-z_$]|\[)|\b(fetch|setTimeout|setInterval|XMLHttpRequest|eval|Function|alert|open)\s*\(|\bnew\s+(XMLHttpRequest|WebSocket|Worker|Function)\b|\bimport\s*\(/);
  const later = [...SRC.slice(B).matchAll(/^(?:async )?function (\w+)\(/gm)].map((m) => m[1]);
  assert.ok(later.length >= 15 && later.includes("api") && later.includes("refreshAll"), later.join(","));
  for (const name of later) assert.doesNotMatch(BLOCK, new RegExp(`\\b${name}\\s*\\(`), `the render block must not call ${name}, which is declared after it`);
  const ex0 = SRC.indexOf("// EX-06 local intent: begin"), ex1 = SRC.indexOf("// EX-06 local intent: end"), rm = SRC.indexOf("const REFRESH_MS = 30000;"), api = SRC.indexOf("async function api(");
  assert.ok(ex0 >= 0 && ex0 < ex1 && ex1 < rm && rm < A && B < api, [ex0, ex1, rm, A, B, api].join(","));
  assert.ok(SRC.slice(0, ex0).split("\n").every((l) => l.trim() === "" || l.startsWith("//")), "only comment lines precede the EX-06 block");
  assert.ok(/score: \(v\) => \(v == null \? "N\/A" : `\$\{Number\(v\)\}\/100`\)/.test(BLOCK), "fmt.score coerces");
  assert.ok(BLOCK.includes("const esc = ") && BLOCK.includes("function safeHref(") && BLOCK.includes("function extLink("));
  assert.deepEqual(BLOCK_FUNCS, ["safeHref", "extLink", "pctClass", "viewBadgeClass", "eventRiskBadgeClass", "evidenceBarHtml", "eventLineHtml", "eventRiskCardHtml", "eventAdvancedDetailsHtml",
                                 "researchDetailHtml", "chatMessageHtml", "outcomeCellHtml", "groupStatsRowHtml", "groupStatsTableHtml", "snapshotDetailHtml"]);
  assert.throws(() => vm.runInNewContext(BLOCK + "\ndocument;", { URL }), /document is not defined/);
});

test("N13 safeHref: only absolute http(s) URLs, normalised and escaped; every other scheme, relative, empty or non-string value is null", () => {
  const x = sandbox();
  assert.equal(x.safeHref(P_OK_URL), "https://example.com/a?b=c&amp;d=%22e%22");
  assert.equal(x.safeHref("HTTP://Example.COM/x"), "http://example.com/x");
  assert.equal(x.safeHref("HTTPS:example.com"), "https://example.com/");                         // the WHATWG parser accepts this form as absolute https
  assert.equal(x.safeHref("http://127.0.0.1:8000/api/execution/auto/enable"), "http://127.0.0.1:8000/api/execution/auto/enable");   // http(s) is allowed whatever the host: the browser then asks the gate
  for (const u of [P_JS_URL, "JAVASCRIPT:alert(1)", "java\tscript:alert(1)", " javascript:x", P_DATA_URL, "blob:https://x/y", "vbscript:x", "file:///C:/x", "//evil.test/x", P_REL_URL, "example.com", "", null, undefined, 0, {}, []]) {
    assert.equal(x.safeHref(u), null, String(u));
  }
  assert.equal(x.extLink(P_JS_URL, P_IMG), x.esc(P_IMG));
  assert.equal(x.extLink("https://a.b/c", "t & u"), '<a href="https://a.b/c" target="_blank" rel="noopener noreferrer">t &amp; u</a>');
});

// ---- N14 (X-1): the three other modules with external links carry the identical helpers and no href="${esc(" remains ----------------
test("N14 market.js, command_center.js and evidence_research.js: identical safeHref / extLink helpers, every external href goes through extLink", () => {
  const helper = BLOCK.match(/function safeHref\(u\) \{[\s\S]*?\n\}/)[0];
  assert.ok(helper.includes('p.protocol === "http:" || p.protocol === "https:" ? esc(p.href) : null'));
  const ONE_LINE_SAFE = 'const safeHref = (u) => { try { const p = new URL(String(u)); return p.protocol === "http:" || p.protocol === "https:" ? esc(p.href) : null; } catch (e) { return null; } };';
  const ONE_LINE_LINK = 'const extLink = (u, text) => { const h = safeHref(u); return h ? `<a href="${h}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text); };';
  for (const name of ["market.js", "command_center.js", "evidence_research.js"]) {
    const js = fs.readFileSync(path.join(FRONT, name), "utf8");
    const lines = js.split(/\r?\n/);                                                         // the WHOLE line, comment included: `...; window.x = 1;` after the helper must not pass
    assert.equal(lines.filter((l) => l === `  ${ONE_LINE_SAFE}   // XSS-01: only an absolute http(s) URL may become a link`).length, 1, name);
    assert.equal(lines.filter((l) => l === `  ${ONE_LINE_LINK}   // XSS-01: a link, or the escaped text alone`).length, 1, name);
    assert.ok(!/href="\$\{esc\(/.test(js), `${name} still builds an href from esc()`);
    assert.ok(count(js, /\bextLink\(/g) >= 1, name);                                          // at least one use (the definition is an arrow: `extLink = (`)
    assert.ok(!/<a href="\$\{(?!h\})/.test(js), `${name} builds an href from something other than the helper's h`);
  }
  // the helpers behave identically to the block's when evaluated with the project's esc
  const ctx = { URL };
  vm.runInNewContext(`const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));\n${ONE_LINE_SAFE}\n${ONE_LINE_LINK}\nthis.r = [safeHref(${JSON.stringify(P_JS_URL)}), extLink(${JSON.stringify(P_JS_URL)}, ${JSON.stringify(P_IMG)}), extLink(${JSON.stringify(P_OK_URL)}, "t")];`, ctx);
  assert.equal(ctx.r[0], null);
  assert.equal(ctx.r[1], sandbox().esc(P_IMG));
  assert.equal(ctx.r[2], '<a href="https://example.com/a?b=c&amp;d=%22e%22" target="_blank" rel="noopener noreferrer">t</a>');
});

// ---- N15-N16: the other 39 modules. N11 walks app.js alone. Here every value this walk can see reaching markup without esc() is a line in xss01_raw_interpolations.json, by exact text and exact
// count, per module. A new raw field fails, and so does one raw field replacing another, which a count alone would miss. A listed call or variable is pinned where it is used, not where it is
// built, so what a local helper returns, what a local variable is given (by assignment, an indexed write, push, unshift, splice or fill) and what a local function that assigns a parameter to innerHTML is passed are walked too, until nothing new appears. A line means "present when this list was generated", not "reviewed"
// or "proven safe". Not seen, and this list is not exhaustive: a helper from another module, unless its own module reaches it; a value that arrives by destructuring, as a callback's parameter, or through a field set by name, as in o.x = v;
// a variable named only inside a call's argument expression, such as f(a + b), rather than as the argument itself; and sinks outside markup strings and innerHTML, which test C4 covers by name. Regex literals are blanked in a line's text, so a change inside one alone is not seen.
const RAW_FILE = path.join(__dirname, "xss01_raw_interpolations.json");
const OTHER_MODULES = fs.readdirSync(FRONT).filter((n) => n.endsWith(".js") && n !== "app.js").sort();
const readModule = (name) => fs.readFileSync(path.join(FRONT, name), "utf8");
const rawKey = (e) => { const s = e.replace(/\s+/g, " "); return s.length > 160 ? `${s.slice(0, 100)} #${crypto.createHash("sha256").update(s).digest("hex").slice(0, 12)}` : s; };
const isHtmlTemplate = (tpl) => /<[a-zA-Z!/]|=["']/.test(splitTemplate(tpl).filter((p) => "lit" in p).map((p) => p.lit).join("\n"));
const TAG_STRING = /(["'])(?:(?!\1)[^\n\\])*<\/?[a-zA-Z](?:(?!\1)[^\n\\])*\1/g;
const isMarkupLeaf = (p) => { const s = stripParens(p); return s[0] === "`" ? skipString(s, 0) === s.length - 1 && isHtmlTemplate(s) : (s[0] === '"' || s[0] === "'") && skipString(s, 0) === s.length - 1 && /<\/?[a-zA-Z]/.test(s); };
function rawBody(body, out, html) {
  const b = body.trim();
  if (b[0] !== "{") { rawIn(b, out, html); return; }
  for (const off of returnsIn(b)) rawIn(statementAt(b, off), out, html);
  for (const n of scanTemplates(b)) rawInTemplate(n.text, out, false);
}
function rawFunction(e) {
  // the body of an arrow or function expression, or null. Parameters may hold defaults with brackets
  const head = e.match(/^(?:async\s+)?/)[0].length;
  if (e.startsWith("function", head)) { const close = balancedAt(e, e.indexOf("(", head)), open = e.indexOf("{", close); return close < 0 || open < 0 ? null : e.slice(open, balancedAt(e, open) + 1); }
  if (e[head] === "(") { const close = balancedAt(e, head); return close >= 0 && /^\s*=>/.test(e.slice(close + 1)) ? e.slice(close + 1).replace(/^\s*=>\s*/, "") : null; }
  const one = e.slice(head).match(/^[A-Za-z_$][\w$]*\s*=>\s*/);
  return one ? e.slice(head + one[0].length) : null;
}
function rawInTemplate(tpl, out, html) {
  const inHtml = html || isHtmlTemplate(tpl);
  for (const p of splitTemplate(tpl)) if ("expr" in p) rawIn(p.expr, out, inHtml);
}
function rawIn(expr, out, html) {
  const e = stripParens(expr);
  if (!e) return;
  const t = ternary(e);                                                                  // a condition prints nothing; both branches can
  if (t) { const inHtml = html || isMarkupLeaf(t[1]) || isMarkupLeaf(t[2]); rawIn(t[1], out, inHtml); rawIn(t[2], out, inHtml); return; }   // a raw branch beside a markup branch prints into the same place
  const ops = topLevelSplit(e, ["||", "&&", "??", "+"]);
  if (ops) { const inHtml = html || ops.some(isMarkupLeaf); for (const p of ops) rawIn(p, out, inHtml); return; }   // "<p>" + r.x is markup, whatever else surrounds it
  if ((e[0] === '"' || e[0] === "'") && skipString(e, 0) === e.length - 1) return;       // one whole string literal
  if (/^-?\d+(\.\d+)?$/.test(e) || e === "null") return;                                 // a number literal, or null
  if (e[0] === "`" && skipString(e, 0) === e.length - 1) { rawInTemplate(e, out, html); return; }   // one whole nested template
  if (/^esc\(/.test(e) && balancedAt(e, 3) === e.length - 1) return;                     // esc(...) as the WHOLE leaf; N15 pins each module's esc
  for (const m of e.matchAll(/\.map\(/g)) {                                              // xs.map(callback).join(...): what the callback returns, and any template in its body
    const open = m.index + 4, close = balancedAt(e, open);
    if (close < 0 || !/^\s*\.join\(\s*("[^"]*"|'[^']*')?\s*\)$/.test(e.slice(close + 1))) continue;
    const arg = e.slice(open + 1, close).trim();
    if (arg === "esc") return;
    const fn = rawFunction(arg);
    if (fn === null) break;
    rawBody(fn, out, html);
    if (html) out.exprs.push(e.slice(0, m.index));                                       // bits.map((b) => `<i>${b}</i>`).join(""): what bits holds reaches the markup through b
    return;
  }
  const fn = rawFunction(e);                                                             // a helper's own definition, reached from a line below
  if (fn !== null) { rawBody(fn, out, html); return; }
  if (html) { out.push(rawKey(e)); out.exprs.push(e); }                                  // anything else, by its exact text: esc dropped inside a helper's argument changes this line
  else for (const n of scanTemplates(e)) rawInTemplate(n.text, out, false);              // not markup itself, but a template inside it may be
}
function exprEnd(src, i) {
  // from just after a piece of markup to the end of its expression: a top-level ; or , or the bracket that closes the enclosing one
  let depth = 0;
  for (let k = i; k < src.length; k++) {
    const c = src[k];
    if (c === '"' || c === "'" || c === "`") { k = skipString(src, k); continue; }
    if ("([{".includes(c)) depth++;
    else if (")]}".includes(c)) { if (!depth) return k; depth--; }
    else if (!depth && (c === ";" || c === ",")) return k;
  }
  return src.length;
}
function exprStart(src, i, tplEnds) {
  // back from a piece of markup to the start of its expression: past strings, templates and balanced brackets, stopping at an assignment, an arrow, return, an unmatched opener, , or ;
  for (let k = i; k > 0;) {
    const c = src[k - 1];
    if (c === "`" && tplEnds.has(k)) { k = tplEnds.get(k); continue; }
    if (c === '"' || c === "'") { const q = src.lastIndexOf(c, k - 2); if (q < 0) return k; k = q; continue; }
    if (")]}".includes(c)) { const open = "([{"[")]}".indexOf(c)]; let d = 0, j = k - 1; for (; j >= 0; j--) { if (src[j] === c) d++; else if (src[j] === open && !--d) break; } k = Math.max(j, 0); continue; }
    if ("([{,;".includes(c)) return k;
    if (c === "=") { let j = k - 1; while (src[j - 1] === "=") j--; if (k - j >= 2 || "!<>".includes(src[j - 1])) { k = j; continue; } return k; }
    if (c === ">" && src[k - 2] === "=") return k;
    if (/\breturn$/.test(src.slice(Math.max(0, k - 7), k))) return k;
    k--;
  }
  return 0;
}
const blankTpl = (tpl) => ` ${splitTemplate(tpl).map((p) => ("lit" in p ? " ".repeat(p.lit.length) : `  ${blankIn(p.expr)} `)).join("")} `;
const blankIn = (text) => { let s = text; for (const n of scanTemplates(text)) s = s.slice(0, n.start) + blankTpl(n.text) + s.slice(n.start + n.text.length); return s; };
const QUOTED = /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/g;
const codeOnly = (code) => blankIn(code).replace(QUOTED, (q) => " ".repeat(q.length));
function rawSinkCalls(code, visible, out) {
  // MED-1: a local function that assigns one of its parameters to innerHTML is itself a sink: what each call passes there reaches markup
  for (const m of visible.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^()]*)\)\s*\{|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\(([^()]*)\)\s*=>\s*\{/g)) {
    const name = m[1] || m[3], open = m.index + m[0].length - 1, end = balancedAt(code, open);
    if (end < 0) continue;
    (m[2] || m[4] || "").split(",").map((p) => p.trim().split("=")[0].trim()).forEach((p, i) => {
      if (!p || !new RegExp(`\\.innerHTML\\s*(?:\\+|\\|\\||\\?\\?|&&)?=(?!=)\\s*${p.replace(/\$/g, "\\$")}\\s*[;\\n}]`).test(code.slice(open, end))) return;
      for (const c of visible.matchAll(new RegExp(`(?<![.\\w$])${name.replace(/\$/g, "\\$")}\\s*\\(`, "g"))) {
        const o = c.index + c[0].length - 1, e = balancedAt(code, o);
        if (e < 0) { out.push(`unreadable call: ${name}`); continue; }
        if (code.slice(0, c.index).trimEnd().endsWith("function")) continue;                // its own declaration
        const inner = code.slice(o + 1, e), args = topLevelSplit(inner, [","]) || [inner];
        if (args[i] !== undefined) rawIn(args[i], out, true);
      }
    });
  }
}
function rawBuilt(name, code, masked, out) {
  // what a local helper returns and what a local variable is given reach the same markup as the line that names them
  const id = name.replace(/\$/g, "\\$");
  for (const m of masked.matchAll(new RegExp(`\\bfunction\\s+${id}\\s*\\(`, "g"))) {
    const close = balancedAt(code, m.index + m[0].length - 1), open = code.indexOf("{", close), end = open < 0 ? -1 : balancedAt(code, open);
    if (close < 0 || end < 0) out.push(`unreadable helper: ${name}`);
    else rawBody(code.slice(open, end + 1), out, true);
  }
  for (const m of masked.matchAll(new RegExp(`(?<![.\\w$])${id}\\s*(?:\\+=|\\|\\|=|\\?\\?=|&&=|=(?![=>]))\\s*`, "g"))) {
    const s = m.index + m[0].length;
    rawIn(code.slice(s, exprEnd(code, s)), out, true);
  }
  for (const m of masked.matchAll(new RegExp(`(?<![.\\w$])${id}\\.(push|unshift|splice|fill)\\(`, "g"))) {
    const open = m.index + m[0].length - 1, close = balancedAt(code, open);
    if (close < 0) { out.push(`unreadable ${m[1]}: ${name}`); continue; }
    const inner = code.slice(open + 1, close), args = topLevelSplit(inner, [","]) || [inner];
    if (m[1] === "push" || m[1] === "unshift") rawIn(inner, out, true);
    else for (const a of m[1] === "splice" ? args.slice(2) : args.slice(0, 1)) rawIn(a, out, true);   // splice(start, count, ...values), fill(value, ...): a removal adds no value
  }
  for (const m of masked.matchAll(new RegExp(`(?<![.\\w$])${id}\\s*(?=\\[)`, "g"))) {                  // name[i] = v, name[i][j] = v, name[i] += v
    let k = m.index + m[0].length;
    while (k >= 0 && code[k] === "[") { const c = balancedAt(code, k); k = c < 0 ? -1 : c + 1; while (k > 0 && /\s/.test(code[k])) k++; }
    if (k < 0) { out.push(`unreadable index: ${name}`); continue; }
    const write = code.slice(k, k + 40).match(/^\s*(?:\+=|\|\|=|\?\?=|&&=|=(?![=>]))\s*/);
    if (write) rawIn(code.slice(k + write[0].length, exprEnd(code, k + write[0].length)), out, true);
  }
}
function rawLeaves(src) {
  const out = [], comments = [], tpls = scanTemplates(src, comments), ends = new Map(tpls.map((t) => [t.start + t.text.length, t.start])), walked = new Set();
  out.exprs = [];
  let code = src;                                                                        // comments blanked, offsets kept: an apostrophe in a comment must not read as a string
  for (const [a, b] of comments) code = code.slice(0, a) + " ".repeat(b - a) + code.slice(b);
  const grouping = (i) => { const before = code.slice(Math.max(0, i - 40), i).trimEnd(); return !/[\w$)\]]$/.test(before) || /\breturn$/.test(before); };   // ( after a value is a call
  const walkExpr = (a, b) => {                                                           // the WHOLE expression holding the markup: `<p>` + r.x, ok ? r.x : `<i></i>` and r.x || `<i></i>` all print r.x
    let s = exprStart(code, a, ends), e = exprEnd(code, b);
    while (code[s - 1] === "(" && code[e] === ")" && grouping(s - 1)) { s = exprStart(code, s - 1, ends); e = exprEnd(code, e + 1); }   // head + err + (x ? `<p></p>` : ""): the parentheses only group
    const id = `${s}:${e}`;
    if (walked.has(id)) return;
    walked.add(id);
    const x = code.slice(s, e);
    rawIn(ternary(x) ? x : x.replace(/^\s*[\w$]+\s*:(?!:)/, ""), out, true);             // an object key is not printed
  };
  let masked = code;
  for (const t of tpls) {
    const end = t.start + t.text.length, text = code.slice(t.start, end);
    masked = masked.slice(0, t.start) + " ".repeat(text.length) + masked.slice(end);
    if (isHtmlTemplate(text)) walkExpr(t.start, end);
    else rawInTemplate(text, out, false);
  }
  for (const m of masked.matchAll(TAG_STRING)) walkExpr(m.index, m.index + m[0].length);
  for (const m of code.matchAll(/\.innerHTML\s*(?:\+|\|\||\?\?|&&)?=(?!=)\s*/g)) rawIn(statementAt(code, m.index + m[0].length), out, true);   // every right-hand side, template or not
  const visible = codeOnly(code);
  rawSinkCalls(code, visible, out);
  const named = new Set(["esc"]);
  for (let i = 0; i < out.exprs.length; i++) {                                           // out.exprs grows as this walks, so it runs until nothing new appears
    const bare = out.exprs[i].trim().replace(/"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/g, '""');
    const names = [...bare.matchAll(/^([A-Za-z_$][\w$]*)(?:$|\s*[.\[])/g),                // a variable, or the object or array a chain starts from
                   ...bare.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g),               // a called helper
                   ...bare.matchAll(/[(,]\s*([A-Za-z_$][\w$]*)\s*(?=[,)])/g)];            // a variable or helper passed as an argument, since the callee may not escape it
    for (const m of names) {
      if (named.has(m[1])) continue;
      named.add(m[1]);
      rawBuilt(m[1], code, visible, out);
    }
  }
  const counts = {};
  for (const e of out) counts[e] = (counts[e] || 0) + 1;
  return counts;
}
function rawChanges(name, src, frozen) {
  const now = rawLeaves(src), want = frozen[name] || {};
  return [...new Set([...Object.keys(now), ...Object.keys(want)])].filter((k) => now[k] !== want[k]).map((k) => `${name}: ${k}: ${want[k] || 0} -> ${now[k] || 0}`);
}
const ESC_FORMS = ["v", "s"].map((p) => `constesc=(${p})=>String(${p}==null?"":${p}).replace(/[&<>"']/g,(c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));`);
function escProblems(src) {
  const out = [], defs = [...src.matchAll(/\b(?:const|let|var)\s+esc\s*=(?!=)/g)];
  if (/\bfunction\s+esc\b/.test(src)) out.push("esc as a function declaration");
  if ([...src.matchAll(/(?<![.\w$])esc\s*=(?![=>])/g)].length !== defs.length) out.push("esc reassigned");
  if (defs.length > 1) out.push("esc defined more than once");
  if (!defs.length && /(?<![.\w$])esc\(/.test(src)) out.push("esc used with no definition");
  for (const d of defs) {
    const end = src.indexOf("[c]));", d.index);
    if (end < 0 || !ESC_FORMS.includes(src.slice(d.index, end + 6).replace(/\s+/g, ""))) out.push(`esc is not the project's encoder: ${src.slice(d.index, d.index + 70)}`);
  }
  return out;
}
const otherSinkForms = (src) => [...src.matchAll(/\[\s*["'`]innerHTML|[{,]\s*["']?innerHTML["']?\s*:/g)].map((m) => m[0]);

test("N15 every other frontend module: each value the walk can see reaching markup without esc() is a listed line, each module's esc is the project's encoder, and innerHTML has no other form", () => {
  assert.ok(OTHER_MODULES.length >= 39, OTHER_MODULES.join(","));
  const frozen = JSON.parse(fs.readFileSync(RAW_FILE, "utf8"));
  const changes = [];
  for (const name of new Set([...OTHER_MODULES, ...Object.keys(frozen)])) changes.push(...rawChanges(name, OTHER_MODULES.includes(name) ? readModule(name) : "", frozen));
  assert.deepEqual(changes, [], "each entry is a changed line, 'text: old count -> new count'. A value from a provider or from storage: wrap it in esc(). Fixed text or a computed number: update xss01_raw_interpolations.json by hand, after checking the value cannot carry markup");
  for (const name of OTHER_MODULES) {
    assert.deepEqual(escProblems(readModule(name)), [], name);
    assert.deepEqual(otherSinkForms(readModule(name)), [], name);
  }
});

test("N16 the N15 guard catches a newly introduced unsafe sink in another module, in each listed form; an escaped addition with new names needs no new line", () => {
  const frozen = JSON.parse(fs.readFileSync(RAW_FILE, "utf8"));
  const edit = (name, re, fn) => { const s = readModule(name); assert.match(s, re, `${name}: the attack's anchor is gone`); return s.replace(re, fn); };
  const add = (name, code) => `${readModule(name)}\n${code}\n`;
  const rawMember = (name) => Object.keys(frozen[name]).find((k) => /^[A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*$/.test(k) && readModule(name).includes(`\${${k}}`));
  const RM = "portfolio_rotation.js", key = rawMember("strategy_lab.js");
  assert.ok(key, "a raw member expression to swap");
  const swap = edit("strategy_lab.js", new RegExp(`\\$\\{${key.replace(".", "\\.")}\\}`), () => "${r.headline}");
  const attacks = [
    ["esc dropped from a provider field, a ledger module", "market.js", edit("market.js", /<strong>\$\{esc\(e\.title\)\}<\/strong>/, () => "<strong>${e.title}</strong>"), "e.title"],
    ["esc dropped from a field, a module outside the ledger", RM, edit(RM, /\$\{esc\(([\w.]+)\)\}/, (m, f) => `\${${f}}`), null],
    ["a new innerHTML template with a raw field", "saved_scans.js", add("saved_scans.js", "function zz(el, r) { el.innerHTML = `<div>${r.headline}</div>`; }"), "r.headline"],
    ["a raw field assigned straight to innerHTML", "trader_review.js", add("trader_review.js", "function zz(el, r) { el.innerHTML = r.headline; }"), "r.headline"],
    ["a raw field concatenated on an innerHTML right-hand side", "daily_brief.js", add("daily_brief.js", 'function zz(el, r) { el.innerHTML = "<p>" + r.headline + "</p>"; }'), "r.headline"],
    ["markup built by string concatenation", "strategy_lab.js", add("strategy_lab.js", 'function zz(r) { return "<p>" + r.headline; }'), "r.headline"],
    ["markup built by template concatenation", "strategy_lab.js", add("strategy_lab.js", "function zz(r) { return `<p>` + r.headline; }"), "r.headline"],
    ["a raw field before a template, in a real concatenation", "forward_validation.js", edit("forward_validation.js", /runLines \+ `<p class="fv-sub">/, () => 'r.headline + `<p class="fv-sub">'), "r.headline"],
    ["a raw branch of a conditional beside markup", "market.js", add("market.js", "function zz(r, ok) { const s = ok ? r.headline : `<i>none</i>`; return s; }"), "r.headline"],
    ["a raw left side of || beside markup", "market.js", add("market.js", "function zz(r) { const s = r.headline || `<i>none</i>`; return s; }"), "r.headline"],
    ["a raw field beside a tag string in an object value", "market.js", add("market.js", 'const zz = (r) => ({ a: "<b>" + r.headline });'), "r.headline"],
    ["a raw field in a chain ending in a grouped template", "trader_render.js", add("trader_render.js", 'function zz(r, m) { return r.headline + (m ? `<p>${esc(m)}</p>` : ""); }'), "r.headline"],
    ["a raw field after a comment with an apostrophe", "market.js", add("market.js", "function zz(r) { // don't <b> trip here\n  return r.headline + `<p></p>`; }"), "r.headline"],
    ["a raw branch beside a nested markup branch", "market.js", add("market.js", 'function zz(r) { return `${r.ok ? `<ul></ul>` : r.headline}`; }'), "r.headline"],
    ["one raw field replacing another: the count is unchanged", "strategy_lab.js", swap, "r.headline"],
    ["an already listed raw field used once more", "strategy_lab.js", add("strategy_lab.js", `function zz() { return \`<i>\${${key}}</i>\`; }`), `${key}: `],
    ["esc dropped inside a helper's argument", "alpaca_paper.js", edit("alpaca_paper.js", /esc\(usd\(a\.cash\)\)/, () => "a.cash"), "a.cash"],
    ["a raw field in a block-bodied map callback", "market.js", add("market.js", 'function zz(xs) { return `<ul>${xs.map((x) => { const t = esc(x.a); return `<li>${t} ${x.source_raw}</li>`; }).join("")}</ul>`; }'), "x.source_raw"],
    ["a raw field in a template nested in a tagless one", "market.js", add("market.js", 'function zz(r) { return `${r.ok ? `<b>${r.headline}</b>` : ""}`; }'), "r.headline"],
    ["a raw field after nested braces, which once derailed the scanner", "market.js", add("market.js", 'function zz(xs) { return `<ul>${xs.map((h) => { const c = h.c || {}; return `<li>${esc(c.a)}</li>`; }).join("")}</ul>`; }\nfunction yy(r) { return `<p>${r.headline}</p>`; }'), "r.headline"],
    ["a raw field in an attribute fragment", "market.js", add("market.js", 'const zz = (r) => ` class="${r.kind_raw}"`;'), "r.kind_raw"],
    ["esc applied, then undone", "market.js", add("market.js", 'function zz(r) { return `<p>${esc(r.a).replace(/x/g, r.headline)}</p>`; }'), "r.headline"],
    ["a brand-new module", "zz_new_module.js", "function z(r) { return `<p>${r.headline}</p>`; }", "r.headline"],
    ["a block helper's return loses esc", "evidence.js", edit("evidence.js", /return esc\(t\)/, () => "return t"), ": "],
    ["a helper's raw early return", "trader_render.js", edit("trader_render.js", /function stateHtml\(code, text, detail\) \{/, () => "function stateHtml(code, text, detail) {\n    if (!code) return text;"), "text"],
    ["an arrow helper loses esc", "market.js", add("market.js", "const zz1 = (s) => s.sector_raw;\nfunction yy(el, s) { el.innerHTML = `<p>${zz1(s)}</p>`; }"), "s.sector_raw"],
    ["a helper reached only through another helper", "market.js", add("market.js", "function zz2(s) { return s.deep_raw; }\nfunction zz3(s) { return zz2(s); }\nfunction yy(el, s) { el.innerHTML = `<p>${zz3(s)}</p>`; }"), "s.deep_raw"],
    ["a map callback named, not inlined", "market.js", add("market.js", "function zz4(x) { return x.row_raw; }\nfunction yy(el, xs) { el.innerHTML = `<ul>${xs.map(zz4).join(\"\")}</ul>`; }"), "x.row_raw"],
    ["a variable's initialiser loses esc", "market.js", add("market.js", "function yy(el, r) { const body1 = r.note_raw; el.innerHTML = `<p>${body1}</p>`; }"), "r.note_raw"],
    ["a variable given a raw value later", "market.js", add("market.js", "function yy(el, r) { let body2 = \"\"; body2 += r.later_raw; el.innerHTML = `<p>${body2}</p>`; }"), "r.later_raw"],
    ["a raw value pushed, then joined", "market.js", add("market.js", "function yy(el, r) { const parts3 = []; parts3.push(r.pushed_raw); el.innerHTML = `<p>${parts3.join(\"\")}</p>`; }"), "r.pushed_raw"],
    ["what a function that sets innerHTML is passed", "forward_journal.js", add("forward_journal.js", 'part("status", r.headline_raw);'), "r.headline_raw"],
    ["a variable inside a template's callback", "market.js", add("market.js", "function yy(el, xs) { el.innerHTML = `<ul>${xs.map((x) => { const t9 = x.inner_raw; return `<li>${t9}</li>`; }).join(\"\")}</ul>`; }"), "x.inner_raw"],
    ["a variable printed by index", "market.js", add("market.js", "function yy(el, x) { const s8 = [x.indexed_raw]; el.innerHTML = `<p>${s8[0]}</p>`; }"), "x.indexed_raw"],
    ["a value spliced into an array printed raw", "daily_review.js", edit("daily_review.js", /bits\.push\(`Volume \$\{esc\(w\.volume\)\}`\);/, (x) => `${x}\n    bits.splice(1, 0, w.note_raw);`), "w.note_raw"],
    ["a value written by index into an array printed raw", "daily_review.js", edit("daily_review.js", /bits\.push\(`Volume \$\{esc\(w\.volume\)\}`\);/, (x) => `${x}\n    bits[0] = w.note_raw;`), "w.note_raw"],
    ["a value given by fill to an array printed raw", "daily_review.js", edit("daily_review.js", /bits\.push\(`Volume \$\{esc\(w\.volume\)\}`\);/, (x) => `${x}\n    bits.fill(w.note_raw);`), "w.note_raw"],
    ["a real indexed write printed raw takes a text field", "strategy_scanner.js", edit("strategy_scanner.js", /counts\[g\.group\] = g\.count;/, () => "counts[g.group] = g.label_raw;"), "g.label_raw"],
    ["an indexed write through two levels", "market.js", add("market.js", "function yy(el, r) { const t7 = [[]]; t7[0][1] = r.deep_raw; el.innerHTML = `<p>${t7[0][1]}</p>`; }"), "r.deep_raw"],
    ["an indexed append", "market.js", add("market.js", 'function yy(el, r) { const t6 = [""]; t6[0] += r.appended_raw; el.innerHTML = `<p>${t6[0]}</p>`; }'), "r.appended_raw"],
    ["a pushed value loses esc, then mapped into markup", "daily_review.js", edit("daily_review.js", /bits\.push\(`Volume \$\{esc\(w\.volume\)\}`\);/, () => "bits.push(`Volume ${w.volume}`);"), "w.volume"],
    ["a variable passed to a helper loses esc", "backtest_lab.js", edit("backtest_lab.js", /const pfv = m\.profit_factor\.text \? esc\(m\.profit_factor\.text\)/, () => "const pfv = m.profit_factor.text ? (m.profit_factor.text)"), "m.profit_factor.text"],
  ];
  for (const [title, name, src, want] of attacks) {
    const changes = rawChanges(name, src, frozen);
    assert.ok(changes.length >= 1, `not detected: ${title}`);
    if (want) assert.ok(changes.some((c) => c.includes(want)), `${title}: expected ${want} in ${changes.join(" | ")}`);
  }
  assert.ok(rawChanges("strategy_lab.js", swap, frozen).length >= 2, "a swap shows both the removed and the added line");
  assert.deepEqual(rawChanges("market.js", add("market.js", "function zz(el, r) { el.innerHTML = `<p>${esc(r.headline)}</p>`; }"), frozen), [], "an escaped addition must not need a new line");
  assert.deepEqual(rawChanges("market.js", add("market.js", "function zz(r) { const q7 = []; q7[0] = r.a_raw; q7.splice(0, 0, r.b_raw); q7.fill(r.c_raw); return q7.length; }"), frozen), [], "an array that never reaches markup needs no line: this guard follows printed names only");
  assert.deepEqual(rawChanges("daily_review.js", edit("daily_review.js", /bits\.push\(`Volume \$\{esc\(w\.volume\)\}`\);/, (x) => `${x}\n    bits.splice(0, 1);`), frozen), [], "a removal adds no value, so it needs no line");
  const weak = [["identity", (s) => s.replace(/const esc = \(v\) =>[\s\S]*?\[c\]\)\);/, "const esc = (v) => v;")], ["ampersand kept", (s) => s.replace('"&": "&amp;"', '"&": "&"')],
                ["redefined", (s) => `${s}\nconst esc = (v) => v;`], ["reassigned", (s) => `${s}\nesc = (v) => v;`], ["a function", (s) => `${s}\nfunction esc(v) { return v; }`]];
  for (const [title, fn] of weak) { const s = fn(readModule("market.js")); assert.notEqual(s, readModule("market.js"), title); assert.notDeepEqual(escProblems(s), [], title); }
  assert.notDeepEqual(escProblems("function z(r) { return `<p>${esc(r.a)}</p>`; }"), [], "esc with no definition");
  assert.notDeepEqual(otherSinkForms('el["innerHTML"] = r.a;'), []);
  assert.notDeepEqual(otherSinkForms("Object.assign(el, { innerHTML: r.a });"), []);
  assert.deepEqual(otherSinkForms('const keep = body ? body.innerHTML : "";'), []);
  assert.deepEqual(scanTemplates("/* never closed `<p>${x}</p>`"), [], "an unterminated comment ends the scan and does not hang it");
});
