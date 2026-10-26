// Node tests for Stage 6.9 (frontend/product_ui.js): POSITIONS groups PAPER — ALPACA and REAL — ROBINHOOD (read only, user managed) and the
// unified exposure summary; the TRADE Position card names the bot, the Alpaca account and Robinhood separately. A source without data is
// "unknown", never "no holdings"; paper and real equity are never added together; everything is escaped.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const PUI = require("../../frontend/product_ui.js");

const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const SRC = (state, extra) => [{ source: "ALPACA_PAPER", account_type: "PAPER", label: "Alpaca PAPER", state: "LOADED", refreshed_at: "2026-10-26T20:00:00+00:00" },
  { source: "ROBINHOOD", account_type: "REAL", label: "Robinhood · REAL · user managed", state, refreshed_at: state === "LOADED" || state === "STALE" ? "2026-10-26T20:00:05+00:00" : null,
    message: state === "STALE" ? "Stale: the last read failed (GATEWAY_DOWN); showing the last known snapshot." : state === "NOT_CONFIGURED" ? "Robinhood portfolio reads are off: real holdings are not included - unknown, not zero." : "m", ...(extra || {}) }];
const ROW = (source, type, sym, qty, mv, own, label) => ({ source, account_type: type, symbol: sym, quantity: qty, average_entry_price: "100", current_price: "110", market_value: mv,
  unrealized_pl: "10", ownership: own, ownership_label: label, managed: own === "BOT_MANAGED", notes: "n" });
const LOADED = { state: "COMPLETE", sources: SRC("LOADED"),
  rows: [ROW("ALPACA_PAPER", "PAPER", "NVDA", "2", "500.00", "BOT_MANAGED", "Bot managed"), ROW("ALPACA_PAPER", "PAPER", "F", "1", "11.58", "EXTERNAL", "External · not managed by the bot"),
         ROW("ROBINHOOD", "REAL", "NVDA", "10", "4000.00", "USER_MANAGED", "User managed · the bot never trades it")],
  exposure: { basis: "ALL_SOURCES", total_market_value: "4511.58", by_account_type: { PAPER: "511.58", REAL: "4000.00" }, by_ownership: { BOT_MANAGED: "500.00", EXTERNAL: "11.58", USER_MANAGED: "4000.00" },
    largest: { symbol: "NVDA", weight_pct: "99.74" }, top5_pct: "100.00",
    by_symbol: [{ symbol: "NVDA", quantity: "12", market_value: "4500.00", fully_valued: true, weight_pct: "99.74", duplicate: true,
                  accounts: [{ source: "ALPACA_PAPER", account_type: "PAPER", ownership: "BOT_MANAGED", quantity: "2" }, { source: "ROBINHOOD", account_type: "REAL", ownership: "USER_MANAGED", quantity: "10" }] },
                { symbol: "F", quantity: "1", market_value: "11.58", fully_valued: true, weight_pct: "0.26", duplicate: false, accounts: [{ source: "ALPACA_PAPER", account_type: "PAPER", ownership: "EXTERNAL", quantity: "1" }] }] },
  equity: { paper: { equity: "100000.00" }, real_external: { equity: "5000.00" }, note: "Paper and real money are separate accounts and are never added together." },
  warnings: [{ code: "DUPLICATE_SYMBOL", level: "info", text: "NVDA is held in 2 accounts <b>" }], disclosure: [], current_entry_policy: "The executor blocks a new bot entry only when the Alpaca PAPER account already holds the symbol." };

test("REAL — ROBINHOOD: user-managed rows when loaded; not configured / stale say so and are never 'no holdings'", () => {
  const html = PUI.robinhoodHtml(LOADED);
  assert.match(html, /data-pui-group="REAL">REAL — ROBINHOOD · user managed · read only</);
  assert.match(html, /data-pui-rh="LOADED"/);
  assert.match(html, /data-pui-rh-row="NVDA"/);
  assert.match(text(html), /User managed · the bot never trades it/);
  assert.match(text(html), /the bot never trades, exits or resizes these holdings/);
  assert.doesNotMatch(html, /data-pui-rh-row="F"/);                                                // a PAPER row never appears under REAL
  const off = PUI.robinhoodHtml({ ...LOADED, sources: SRC("NOT_CONFIGURED"), rows: LOADED.rows.filter((r) => r.account_type === "PAPER") });
  assert.match(off, /data-pui-rh-empty="NOT_CONFIGURED"/);
  assert.match(text(off), /Not configured\. .*unknown, not zero/);
  assert.doesNotMatch(text(off), /No holdings/);
  const stale = PUI.robinhoodHtml({ ...LOADED, sources: SRC("STALE") });
  assert.match(stale, /data-pui-rh-stale/);
  assert.match(stale, /data-pui-rh-row="NVDA"/);                                                    // the last known snapshot is kept
  assert.match(PUI.robinhoodHtml(null, "HTTP 500"), /unknown, not zero/);
});

test("unified exposure: the same symbol aggregates across accounts, ownership stays per account, equity is never summed", () => {
  const html = PUI.unifiedHtml(LOADED);
  const t = text(html);
  assert.match(html, /data-pui-unified="COMPLETE"/);
  assert.match(html, /data-pui-uni-row="NVDA" data-duplicate="1"/);
  assert.match(t, /NVDA 12 \$4,500\.00 99\.74% PAPER 2 \(bot\) \+ REAL 10 \(user managed\)/);
  assert.match(t, /By ownership bot \$500\.00 · external \$11\.58 · user managed \$4,000\.00/);
  assert.match(t, /Equity · PAPER \$100,000\.00 Equity · REAL \$5,000\.00/);
  assert.match(t, /never added together/);
  assert.doesNotMatch(t, /105,000/);
  assert.match(html, /NVDA is held in 2 accounts &lt;b&gt;/);                                      // escaped
  const partial = PUI.unifiedHtml({ ...LOADED, state: "PARTIAL", sources: SRC("AUTH_REQUIRED"), equity: { ...LOADED.equity, real_external: null },
    exposure: { ...LOADED.exposure, basis: "KNOWN_SOURCES_ONLY" }, disclosure: ["Robinhood · REAL · user managed: auth required - not included (unknown, not zero)."] });
  assert.match(partial, /data-pui-unified="PARTIAL"/);
  assert.match(text(partial), /\(known sources only\)/);
  assert.match(text(partial), /Equity · REAL unknown \(Robinhood not loaded\)/);
  assert.match(text(partial), /not included \(unknown, not zero\)/);
});

test("TRADE Position card: the bot, the Alpaca account and Robinhood are three separate lines; nothing held -> the plain card", () => {
  const card = PUI.exposureCardHtml(LOADED, "NVDA");
  const t = text(card);
  assert.match(card, /data-pui-exposure="NVDA"/);
  assert.match(t, /No bot position/);
  assert.match(t, /Alpaca PAPER account: no non-bot holding/);
  assert.match(t, /Robinhood · REAL: 10 shares · User managed · the bot never trades it/);
  assert.match(t, /Unified exposure: 12 shares · \$4,500\.00/);
  assert.match(text(PUI.exposureCardHtml(LOADED, "F")), /Alpaca PAPER account: 1 F · External · not managed by the bot · blocks a bot entry in F Robinhood · REAL: none/);
  assert.equal(PUI.exposureCardHtml(LOADED, "MSFT"), null);
  assert.equal(PUI.exposureCardHtml(null, "NVDA"), null);
  const unknown = { ...LOADED, sources: SRC("AUTH_REQUIRED"), rows: LOADED.rows.filter((r) => r.account_type === "PAPER") };
  assert.match(text(PUI.exposureCardHtml(unknown, "F")), /Robinhood · REAL: unknown \(Login required\)/);   // never "none" when it is unknown
});

test("review fixes: unknown PAPER data, known-sources-only totals, a synthetic fixture never shown as REAL, gross figures", () => {
  const paperUnknown = { ...LOADED, sources: [{ ...SRC("LOADED")[0], state: "NOT_LOADED" }, SRC("LOADED")[1]], rows: LOADED.rows.filter((r) => r.account_type === "REAL"),
    exposure: { ...LOADED.exposure, missing_sources: ["ALPACA_PAPER"] } };
  const t = text(PUI.exposureCardHtml(paperUnknown, "NVDA"));
  assert.match(t, /Alpaca PAPER account: unknown \(Not loaded yet\)/);                              // never "no non-bot holding" when unknown
  assert.match(t, /Unified exposure \(known sources only\): 12 shares/);
  const fx = { ...LOADED, sources: [SRC("LOADED")[0], { ...SRC("LOADED")[1], source: "ROBINHOOD_FIXTURE", account_type: "SYNTHETIC", label: "Robinhood · SYNTHETIC FIXTURE (not a real account)" }],
    rows: LOADED.rows.map((r) => (r.account_type === "REAL" ? { ...r, source: "ROBINHOOD_FIXTURE", account_type: "SYNTHETIC" } : r)) };
  const rh = PUI.robinhoodHtml(fx), uni = PUI.unifiedHtml(fx), card = PUI.exposureCardHtml(fx, "NVDA");
  assert.match(text(rh), /SYNTHETIC — ROBINHOOD FIXTURE · not a real account/);
  assert.match(text(rh), /Synthetic data, not a real account/);
  assert.doesNotMatch(text(rh), /REAL — ROBINHOOD|Real money/);
  assert.match(text(uni), /Known invested value \(PAPER \+ SYNTHETIC, gross\)/);
  assert.match(text(uni), /Equity · SYNTHETIC/);
  assert.match(text(card), /Robinhood · SYNTHETIC fixture: 10 shares/);
  assert.doesNotMatch(text(card), /Robinhood · REAL/);
  const gross = PUI.unifiedHtml({ ...LOADED, exposure: { ...LOADED.exposure, by_symbol: [{ ...LOADED.exposure.by_symbol[0], quantity: "0", gross_quantity: "20", market_value: "0.00", gross_market_value: "2000.00" }] } });
  assert.match(text(gross), /NVDA 20 \$2,000\.00/);                                                  // a long and a short never cancel into "0 exposure"
});
