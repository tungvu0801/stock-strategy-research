"""
XSS-01 (the dashboard's HTML insertion paths; XSS01_REMEDIATION_DESIGN.md): the freeze pins, as every stage since 6.8. The change set since
BASE_XSS01 (main after PR #55) is exactly NEW_XSS01 + EDITED_XSS01; the gate's diff is exactly the declared lines (its CSP / nosniff header
tuple and the docstring sentence); app.js's diff is exactly the wrapped lines plus the listed new code (every removed base line and every new line of code is pinned verbatim, the new lines are
hashed, the EX-06 header block is byte-identical, the 27 innerHTML sinks and the /api routes are unchanged); the three X-1 modules gain the
two shared helper lines and change one link line each; every changed older test function is declared; every bookkeeping edit is undone
exactly; the ledger changes only its four re-hashed entries and registers the three X-1 modules; the CI manifest gains exactly the two new test files; Stage 4.6B, execution,
safety, config, the server and both HTML pages are untouched. tests/test_xss01_csp.py and tests/frontend/xss01_app_js.test.cjs pin the behaviour.
"""
import ast
import collections
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL
from test_xss01_csp import CSP_XSS01

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_XSS01 = {"tests/frontend/xss01_app_js.test.cjs", "tests/frontend/xss01_raw_interpolations.json", "tests/test_xss01_csp.py", "tests/test_xss01_freeze.py"}
CI_FILES_XSS01 = ("test_xss01_csp.py", "test_xss01_freeze.py")
PROD_XSS01 = {"api/local_gate.py", "frontend/app.js", "frontend/command_center.js", "frontend/evidence_research.js", "frontend/market.js"}
X1_FILES = ("frontend/command_center.js", "frontend/evidence_research.js", "frontend/market.js")
X1_LINK_EDIT = {                                                                          # each module's one link line changes by exactly this fragment, nothing else on the line
    "frontend/command_center.js": ('<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>', "${extLink(x.url, x.headline)}"),
    "frontend/evidence_research.js": ('`<a href="${esc(i.source_url)}" target="_blank" rel="noopener">source</a>`', 'extLink(i.source_url, "source")'),
    "frontend/market.js": ('<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>', "${extLink(x.url, x.headline)}"),
}
APP_JS_NEW_CODE_XSS01 = [   # app.js's genuinely new lines, in file order: everything else that differs from main is a main line with esc( ) or encodeURIComponent( ) added (F03Y)
    '// XSS-01 render: begin. Every string this dashboard inserts into innerHTML is escaped here (esc); an external URL becomes a link only when it',
    '// is an absolute http(s) URL (safeHref / extLink); and every HTML builder below is a pure function of its payload (no DOM, network or',
    '// browser-storage reference), so tests/frontend/xss01_app_js.test.cjs runs this block in a bare sandbox against hostile payloads.',
    '// Rule: inside an HTML template every ${...} is esc(...), extLink(...), fmt.price / pct / num / rvol (number-derived), a class helper',
    '// (pctClass / viewBadgeClass / eventRiskBadgeClass: fixed words), or a call to one of these builders; attribute values are always "quoted".',
    'const esc = (v) => String(v == null ? "" : v).replace(/[&<>"\']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;" }[c]));',
    'function safeHref(u) {',
    '  // only an absolute http(s) URL may become a link; a javascript, data, blob or file scheme, a relative or a malformed value is not a link',
    '    const p = new URL(String(u));',
    '    return p.protocol === "http:" || p.protocol === "https:" ? esc(p.href) : null;',
    '  } catch (e) {',
    '    return null;',
    'function extLink(url, text) {',
    '  // an external link (new tab, no opener, no referrer), or the escaped text alone when the URL is not an absolute http(s) URL',
    '  const h = safeHref(url);',
    '  return h ? `<a href="${h}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text);',
    '  score: (v) => (v == null ? "N/A" : `${Number(v)}/100`),',
    '  return `<div class="event-item"><strong>${esc(e.title)}</strong> &mdash; ${esc(when)}${tentative}<div class="hint">Source: ${extLink(e.source_url, e.source)}</div></div>`;',
    'function researchDetailHtml(r) {',
    '  const m = r.metrics;',
    '                  ${extLink(c.url, c.title)}',
    'function chatMessageHtml(role, text, toolCalls) {',
    '  return `<span class="role">${role === "user" ? "You" : "Assistant"}</span>${esc(text)}${toolsHtml}`;',
    'function snapshotDetailHtml(s) {',
    '  const narrative = s.narrative || {};',
    '// XSS-01 render: end',
    '    body.innerHTML = researchDetailHtml(r);',
    '  div.innerHTML = chatMessageHtml(role, text, toolCalls);',
    '    body.innerHTML = snapshotDetailHtml(s);',
]
APP_JS_BASE_LINES_REPEATED_XSS01 = {'  }': 1, '': 6, '  try {': 1, '}': 5, '  return `': 2}   # main's lines that now occur more often: only the new builders' scaffolding
UNTOUCHED = ("config.py", "api/server.py", "frontend/index.html", "frontend/forward_validation.html", "tests/conftest.py", "api/routes/alpaca_paper_orders.py",
             "frontend/product_ui.js", "frontend/trader_view.js", "frontend/forward_validation.js", "browser_tests/flows.py", "browser_tests/app_server.py", "browser_tests/cdp.py")
FORBIDDEN_PREFIX_XSS01 = ("execution/", "safety/", "autorun/", "forward_validation/", "forward_capture/", "strategy_registry/", "setups/", "proposals/", "paper/",
                          "unified_portfolio/", "cash_flow/", "account_refresh/", "market_intel/", "research_data/", "swing_eval/", "swing_pullback/", "api/routes/",
                          "agents/", "data/", "database/", "scripts/", "browser_tests/")
APP_JS = "frontend/app.js"
ONE_LINE_SAFE = 'const safeHref = (u) => { try { const p = new URL(String(u)); return p.protocol === "http:" || p.protocol === "https:" ? esc(p.href) : null; } catch (e) { return null; } };'
ONE_LINE_LINK = 'const extLink = (u, text) => { const h = safeHref(u); return h ? `<a href="${h}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text); };'
GATE_REMOVED_XSS01 = [                                                                   # main's lines, in order (T-X20's SequenceMatcher form)
    'Every allowed HTTP response the app sends through the gate (every route, static file, 404 / 405) carries X-Frame-Options: DENY and CSP',
    "frame-ancestors 'none' (D-17); the one exception is the 500 of an unhandled error, which Starlette's ServerErrorMiddleware (outside every",
    'FRAME_HEADERS = ((b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors \'none\'"))   # D-17 (a)',
]
GATE_ADDED_XSS01 = [                                                                     # XSS-01's lines, in order
    'Every allowed HTTP response the app sends through the gate (every route, static file, 404 / 405) carries X-Frame-Options: DENY, a CSP of',
    "frame-ancestors 'none' (D-17) plus script-src 'self', object-src 'none', base-uri 'none', form-action 'self' and frame-src 'none' (XSS-01 R3:",
    'the browser refuses inline or foreign script, a javascript: URL, a plugin, an injected <base>, form or frame even if a page ever rendered',
    "hostile text), and X-Content-Type-Options: nosniff; the one exception is the 500 of an unhandled error, which Starlette's ServerErrorMiddleware (outside every",
    'FRAME_HEADERS = ((b"x-frame-options", b"DENY"),                                                                                           # D-17 (a)',
    '                 (b"content-security-policy", b"frame-ancestors \'none\'; script-src \'self\'; object-src \'none\'; base-uri \'none\'; form-action \'self\'; frame-src \'none\'"),   # D-17 (a) + XSS-01 (R3)',
    '                 (b"x-content-type-options", b"nosniff"))                                                                                 # XSS-01 (R3)',
]
APP_JS_MOVED_INTO_BLOCK = {"    const m = r.metrics;", "    body.innerHTML = `", "    const narrative = s.narrative || {};"}   # the three structural lines that moved into the builders
APP_JS_NEW_LINES_XSS01 = (145, "bac0da6c052b6fc5fad26a4a04c4455871c623ec180f67b6819ba064faf49e93")   # count and SHA-256 of app.js's lines that are not in main's (joined by \n)
APP_JS_GONE_UNPAIRED_XSS01 = collections.Counter([   # main's gone lines with no wrapped partner: moved into a builder or replaced by listed code. Every other gone line has exactly one (F03Y)
    '  score: (v) => (v == null ? "N/A" : `${v}/100`),',
    '  const link = e.source_url ? `<a href="${e.source_url}" target="_blank" rel="noopener">${e.source}</a>` : e.source;',
    '  return `<div class="event-item"><strong>${e.title}</strong> &mdash; ${when}${tentative}<div class="hint">Source: ${link}</div></div>`;',
    '    const m = r.metrics;',
    '    body.innerHTML = `',
    '    body.innerHTML = `',
    '                  ${c.url ? `<a href="${c.url}" target="_blank" rel="noopener">${c.title}</a>` : c.title}',
    '  div.innerHTML = `<span class="role">${role === "user" ? "You" : "Assistant"}</span>${text}${toolsHtml}`;',
    '    const narrative = s.narrative || {};',
])
APP_JS_LINES_GONE_XSS01 = [   # main's app.js lines with no identical line in XSS-01's app.js: each wrapped (esc / extLink / encodeURIComponent / Number) or moved into a builder
    '  score: (v) => (v == null ? "N/A" : `${v}/100`),',
    '      <strong>Research</strong> ${line(r)}${r.limit_reached ? " · <strong>limit reached</strong>" : ""} &middot;',
    '      Tokens ${(r.input_tokens + r.output_tokens).toLocaleString()} &middot; Est. $${r.estimated_cost_usd.toFixed(4)}',
    '      <br><strong>Explain</strong> ${line(x)}${x.limit_reached ? " · <strong>limit reached</strong>" : ""} &middot;',
    '      Tokens ${(x.input_tokens + x.output_tokens).toLocaleString()} &middot; Est. $${x.estimated_cost_usd.toFixed(4)}',
    '  const header = `<tr>${columns.map((c) => `<th>${c.label}</th>`).join("")}</tr>`;',
    '        `<tr data-symbol="${r.symbol}">${columns.map((c) => `<td>${c.render(r)}</td>`).join("")}</tr>`',
    '  { label: "Symbol", render: (r) => `<strong>${r.symbol}</strong>` },',
    '  { label: "Signal", render: (r) => `<span class="signal-badge">${r.signal}</span>` },',
    '  { label: "Symbol", render: (r) => `<strong>${r.symbol}</strong>` },',
    '  { label: "Symbol", render: (r) => `<strong>${r.symbol}</strong>` },',
    '    <div class="card" data-symbol="${m.symbol}">',
    '      <div class="card-symbol">${m.symbol}</div>',
    '      <div class="card-signal"><span class="signal-badge">${m.signal}</span></div>',
    '      <div class="card-row"><span>Attention</span><span>${fmt.score(m.attention_score)}</span></div>',
    '          return `<div class="card"><div class="card-symbol">${sym}</div><div class="hint">No data available</div></div>`;',
    '        <div class="card" data-symbol="${sym}">',
    '          <button class="card-remove" data-remove="${sym}" title="Remove from watchlist">×</button>',
    '          <div class="card-symbol">${sym}</div>',
    '          <div class="card-row"><span>RSI ${fmt.num(m.rsi, 1)}</span><span>Trend ${m.trend}</span></div>',
    '          <div class="card-signal"><span class="signal-badge">${m.signal}</span></div>',
    '          <div class="card-row"><span>Attention</span><span>${fmt.score(m.attention_score)}</span></div>',
    '        await api(`/api/watchlist/${btn.dataset.remove}`, { method: "DELETE" });',
    '    grid.innerHTML = `<div class="hint">Watchlist unavailable: ${err.message}</div>`;',
    '      <span class="pct-up">Bullish ${evidence.bullish_pct}%</span>',
    '      <span class="hint">Neutral ${evidence.neutral_pct}%</span>',
    '      <span class="pct-down">Bearish ${evidence.bearish_pct}%</span>',
    '      ${evidence.bullish_pct > 0 ? `<div class="seg bullish" style="width:${evidence.bullish_pct}%">${evidence.bullish_pct}%</div>` : ""}',
    '      ${evidence.neutral_pct > 0 ? `<div class="seg neutral" style="width:${evidence.neutral_pct}%">${evidence.neutral_pct}%</div>` : ""}',
    '      ${evidence.bearish_pct > 0 ? `<div class="seg bearish" style="width:${evidence.bearish_pct}%">${evidence.bearish_pct}%</div>` : ""}',
    '  const link = e.source_url ? `<a href="${e.source_url}" target="_blank" rel="noopener">${e.source}</a>` : e.source;',
    '  return `<div class="event-item"><strong>${e.title}</strong> &mdash; ${when}${tentative}<div class="hint">Source: ${link}</div></div>`;',
    '    : `<p class="hint">${events.earnings_reason || "Earnings-calendar data is not currently available from a verified provider."}</p>`;',
    '      <h4>Event Risk <span class="event-risk-badge event-risk-${eventRiskBadgeClass(events.event_risk_level)}">${events.event_risk_level}</span></h4>',
    '        <div class="label">Event</div><div>${e.title} (${e.event_type})</div>',
    '        <div class="label">Status</div><div>${e.status}${e.confirmed ? "" : " (tentative)"}</div>',
    '        <div class="label">Time precision</div><div>${e.time_precision}</div>',
    '        <div class="label">UTC timestamp</div><div>${e.event_datetime_utc ? new Date(e.event_datetime_utc).toISOString() : "N/A (date only)"}</div>',
    '        <div class="label">Source provider</div><div>${e.source_provider}</div>',
    '        <div class="label">Event ID</div><div class="hint">${e.event_id}</div>',
    '    const r = await api(`/api/stocks/${symbol}/research`);',
    '    const m = r.metrics;',
    '    body.innerHTML = `',
    '      <h2>${m.symbol}</h2>',
    '      <div class="view-badge ${viewBadgeClass(r.research_view)}">${r.research_view}</div>',
    '      <div class="hint">${r.research_view_disclaimer}</div>',
    '        <span class="data-quality-badge" title="${r.data_quality.explanation}">Data quality: ${r.data_quality.level}</span>',
    '        <div class="narrative-section"><h4>What is happening?</h4><p>${r.narrative.whats_happening}</p></div>',
    '        <div class="narrative-section"><h4>Why?</h4><p>${r.narrative.why}</p></div>',
    '          <ul class="narrative-list">${r.narrative.whats_good.map((x) => `<li>&#10003; ${x}</li>`).join("") || "<li>(none)</li>"}</ul>',
    '          <ul class="narrative-list">${r.narrative.be_careful.map((x) => `<li>&#9888; ${x}</li>`).join("") || "<li>(none)</li>"}</ul>',
    '        <div class="narrative-section"><h4>What should I watch?</h4><p>${r.narrative.what_to_watch}</p></div>',
    '                  <span class="sentiment-badge sentiment-${c.sentiment}">${c.sentiment}</span>',
    '                  ${c.url ? `<a href="${c.url}" target="_blank" rel="noopener">${c.title}</a>` : c.title}',
    '                  <div class="hint">${c.source}${c.published_at ? " &middot; " + c.published_at : ""}</div>',
    '                  <div>${c.reason}</div>',
    '            : `<div class="narrative-section"><h4>Current catalysts</h4><p class="hint">${r.catalyst_note || "No clear current catalyst identified."}</p></div>`',
    '          <div class="label">Trend</div><div>${m.trend}</div>',
    '          <div class="label">Signal</div><div><span class="signal-badge">${m.signal}</span></div>',
    '          <div class="label">Attention Score</div><div>${fmt.score(m.attention_score)} <span class="hint">(activity, not a rating)</span></div>',
    '            ? r.risk_flags.map((f) => `<div class="risk-flag-item"><strong>${f.code}</strong> (${f.severity})<br>${f.description}</div>`).join("")',
    '        <div class="ai-box">${r.risk_explanation}</div>',
    '               <p>${m.symbol} vs ${r.sector.sector_name} (${r.sector.sector_etf}): sector ${fmt.pct(r.sector.sector_pct_change)},',
    '               market (${r.sector.market_proxy_symbol}) ${fmt.pct(r.sector.market_pct_change)},',
    '    body.innerHTML = `<p>Could not load research for ${symbol}: ${err.message}</p>`;',
    '      ? `<div class="chat-tool-calls">Used: ${toolCalls.map((t) => `${t.tool}(${Object.values(t.input || {}).join(", ")})`).join(", ")}</div>`',
    '  div.innerHTML = `<span class="role">${role === "user" ? "You" : "Assistant"}</span>${text}${toolsHtml}`;',
    '      <div class="risk-flag-item" data-symbol="${a.symbol}" style="cursor:pointer;">',
    '        <strong>${a.symbol}</strong> — <span class="signal-badge">${a.signal}</span>',
    '        &middot; Attention ${fmt.score(a.attention_score)}',
    '    el.innerHTML = `<div class="hint">Alerts unavailable: ${err.message}</div>`;',
    '    const setup = await api(`/api/stocks/${symbol}/research-setup`);',
    '    const sizing = await api(`/api/stocks/${symbol}/position-sizing?portfolio_value=${portfolioValue}`);',
    '        <h2>${symbol} — Research Setup</h2>',
    '          <div class="label">Time Horizon</div><div>${setup.time_horizon}</div>',
    '          <div class="label">Support</div><div>${setup.support.map(fmt.price).join(", ") || "N/A"}</div>',
    '          <div class="label">Resistance</div><div>${setup.resistance.map(fmt.price).join(", ") || "N/A"}</div>',
    '          <div class="label">Possible Targets</div><div>${setup.possible_targets.map(fmt.price).join(", ") || "N/A"}</div>',
    '          <div class="label">Risk/Reward</div><div>${setup.risk_reward_ratio != null ? setup.risk_reward_ratio + "x" : "N/A"}</div>',
    '          <div class="label">Data Timestamp</div><div>${setup.data_timestamp}</div>',
    '          <div class="label">Max Risk-Based Shares</div><div>${sizing.max_risk_based_shares ?? "N/A"}</div>',
    '          <div class="label">Max Allocation-Based Shares</div><div>${sizing.max_allocation_based_shares ?? "N/A"}</div>',
    '          <div class="label">Recommended Shares</div><div>${sizing.recommended_shares ?? "N/A"}</div>',
    '          <div class="label">% of Portfolio</div><div>${sizing.position_pct_of_portfolio != null ? sizing.position_pct_of_portfolio + "%" : "N/A"}</div>',
    '        <div class="ai-box">${sizing.explanation}</div>',
    '        <div class="hint" style="margin-top:8px;">If you already hold this stock: ${sizing.position_adjustment_message}</div>',
    '        <span id="setup-save-snapshot-msg" class="hint">Generates the full beginner analysis (evidence, catalysts, risk) for ${symbol} and saves it for later outcome tracking.</span>',
    '        const research = await api(`/api/stocks/${symbol}/research`);',
    '    result.innerHTML = `<div class="hint">Could not load setup for ${symbol}: ${err.message}</div>`;',
    '      <div class="risk-flag-item" data-snapshot-id="${s.id}" style="cursor:pointer;">',
    '        <strong>${s.symbol}</strong> &middot;',
    '        <span class="view-badge ${viewBadgeClass(s.research_view)}" style="margin:0;">${s.research_view}</span> &middot;',
    '        <span class="pct-up">Bullish ${s.bullish_pct}%</span> &middot;',
    '          Saved: ${new Date(s.created_at).toLocaleString()} &middot; Market snapshot: ${s.market_timestamp}',
    '    el.innerHTML = `<div class="hint">Research history unavailable: ${err.message}</div>`;',
    '      <td>${g.label}</td>',
    '      <td>${g.n}</td>',
    '      <td>${g.positive_return_frequency != null ? Math.round(g.positive_return_frequency * 100) + "%" : "N/A"}</td>',
    '      <td>${g.sample_warning ? `<span class="hint">${g.sample_warning}</span>` : ""}</td>',
    '    <h4 style="margin-top:14px;">${title}</h4>',
    '      Total saved: <strong>${perf.total_snapshots}</strong> &middot;',
    '      Completed: <strong>${perf.completed_snapshots}</strong> &middot;',
    '      Pending: <strong>${perf.pending_snapshots}</strong>',
    '      ${perf.data_issues ? ` &middot; <span class="hint">${perf.data_issues} row(s) had unreadable stored data, excluded</span>` : ""}',
    '    summaryEl.innerHTML = `<div class="hint">Performance analytics unavailable: ${err.message}</div>`;',
    '    const s = await api(`/api/research/snapshots/${snapshotId}`);',
    '    const narrative = s.narrative || {};',
    '    body.innerHTML = `',
    '      <h2>${s.symbol} ${s.historical_validation ? `<span class="historical-badge">HISTORICAL VALIDATION</span>` : ""}</h2>',
    '        Saved: ${new Date(s.created_at).toLocaleString()}<br>',
    '        Market snapshot: ${s.market_timestamp}<br>',
    '        Price captured: ${new Date(s.price_timestamp).toLocaleString()} (${s.price_source})',
    '        <div class="label">Price</div><div>${fmt.price(s.price)} (${s.price_source})</div>',
    '        <div class="label">Bullish / Neutral / Bearish</div><div>${s.bullish_pct}% / ${s.neutral_pct}% / ${s.bearish_pct}%</div>',
    '        <div class="label">Research View</div><div>${s.research_view}</div>',
    '        <div class="label">Signal</div><div>${s.scanner_signal}</div>',
    '        <div class="label">Data Quality</div><div>${s.data_quality_level}</div>',
    '            s.catalysts.map((c) => `<div class="catalyst-item"><span class="sentiment-badge sentiment-${c.sentiment}">${c.sentiment}</span> ${c.title}</div>`).join("") +',
    '            s.risk_flags.map((f) => `<div class="risk-flag-item"><strong>${f.code}</strong> (${f.severity})<br>${f.description}</div>`).join("") +',
    '        <h4>Events known at the time ${s.events ? `<span class="event-risk-badge event-risk-${eventRiskBadgeClass(s.events.event_risk_level)}">${s.events.event_risk_level}</span>` : ""}</h4>',
    '        <p>${narrative.whats_happening || ""}</p>',
    '        <p>${narrative.why || ""}</p>',
    '          <div class="label">${o.horizon_trading_days} trading day(s)</div>',
    '              ? `ERROR: ${o.error_message}`',
    '    body.innerHTML = `<p>Could not load snapshot #${snapshotId}: ${err.message}</p>`;',
]
HELPERS_XSS01 = '''
def pre_xss01(rel: str, text: str) -> str:
    if rel not in SEMANTIC_XSS01:
        return text
    out = subprocess.run(["git", "show", f"{BASE_XSS01}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_XSS01} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_xss01(rel: str, text: str) -> str:
    text = _norm(text)
    for old, new in reversed(UNDO_XSS01.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text
'''                                                                                         # the two helpers' code, exactly (docstrings aside)


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def unwrap(line):
    """`line` with every esc( ... ) and encodeURIComponent( ... ) call removed, keeping its argument: what the line was before it was wrapped."""
    for name in ("encodeURIComponent(", "esc("):
        while True:
            start, i = 0, -1
            while True:
                i = line.find(name, start)
                if i <= 0 or not (line[i - 1].isalnum() or line[i - 1] in "_."):
                    break
                start = i + 1
            if i < 0:
                break
            j, depth = i + len(name), 1
            while j < len(line) and depth:
                c = line[j]
                if c in "\"'`":
                    k = j + 1
                    while k < len(line) and line[k] != c:
                        k += 2 if line[k] == "\\" else 1
                    j = k
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            if depth:
                break
            line = line[:i] + line[i + len(name):j - 1] + line[j:]
    return line


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_XSS01}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_XSS01} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def defs(text):
    out = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ast.dump(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out["=" + t.id] = ast.dump(n)
    return out


def line_ops(rel):
    base, now = at_base(rel).split("\n"), now_text(rel).split("\n")
    ops = [(base[i1:i2], now[j1:j2]) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
    return base, now, [l for b, _ in ops for l in b], [l for _, n in ops for l in n]


def test_F01Y_the_change_set_is_exactly_the_declared_files_and_no_trading_server_page_or_conftest_file_changed():
    assert FL.BASE_XSS01 == "9df1717a0eb20e666a2e641fbcabac466b30ad2d" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_XSS01}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_XSS01)
    assert changed == FL.NEW_XSS01 | FL.EDITED_XSS01, {"unexpected": sorted(changed - FL.NEW_XSS01 - FL.EDITED_XSS01), "missing": sorted((FL.NEW_XSS01 | FL.EDITED_XSS01) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_XSS01).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_XSS01 == NEW_FILES_XSS01 and {p for p in changed if p in in_base} == FL.EDITED_XSS01
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_XSS01).split()
    assert {p for p in FL.EDITED_XSS01 if not p.startswith("tests/")} == PROD_XSS01 | {".github/ci/pytest_groups.json"}
    assert set(FL.SEMANTIC_XSS01) == PROD_XSS01 and FL.INSTRUMENTATION_XSS01 == {} and FL.NEW_LEDGER_XSS01 == set(X1_FILES) and all(len(v) > 60 for v in FL.SEMANTIC_XSS01.values())
    assert all(p.endswith((".py", ".cjs")) for p in FL.EDITED_XSS01 if p.startswith("tests/"))
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_XSS01)} - {"api/local_gate.py"} and not {p for p in changed if p.startswith("api/routes/")}
    for rel in UNTOUCHED:
        assert rel not in changed and now_text(rel) == at_base(rel), rel
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_XSS01, "--", "api/routes/alpaca_paper_orders.py"], cwd=ROOT).returncode == 0   # Stage 4.6B byte-identical
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")
    for rel in sorted(changed):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "core"} == {g: v for g, v in base_groups.items() if g != "core"}
    i = base_groups["core"]["files"].index("test_ex06_freeze.py") + 1
    assert groups["core"] == {**base_groups["core"], "files": base_groups["core"]["files"][:i] + list(CI_FILES_XSS01) + base_groups["core"]["files"][i:]}
    assert FL.LEDGER[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))


def test_F02Y_the_gates_diff_is_exactly_the_csp_nosniff_header_tuple_and_its_docstring_sentence():
    base, now, removed, added = line_ops("api/local_gate.py")
    assert removed == GATE_REMOVED_XSS01 and added == GATE_ADDED_XSS01, (removed, added)
    assert len(now) == len(base) + len(GATE_ADDED_XSS01) - len(GATE_REMOVED_XSS01)
    import api.local_gate as G
    assert G.FRAME_HEADERS == ((b"x-frame-options", b"DENY"), (b"content-security-policy", CSP_XSS01.encode()), (b"x-content-type-options", b"nosniff"))
    assert G.CODES == ("CLIENT", "HOST", "PROXY", "ORIGIN", "REFERER", "FETCH_SITE", "INTENT", "CONTENT_TYPE", "ERROR", "ROOT_PATH")   # no rule changed
    assert FL.pre_xss01("api/local_gate.py", now_text("api/local_gate.py")) == at_base("api/local_gate.py") and FL.pre_xss01("config.py", "x") == "x"


def test_F03Y_app_js_changes_exactly_the_wrapped_lines_keeps_the_ex06_block_the_27_sinks_and_the_routes_and_the_x1_modules_gain_only_the_helpers():
    base, now = at_base(APP_JS).split("\n"), now_text(APP_JS).split("\n")
    assert base[:31] == now[:31] and now[6].startswith("// EX-06 local intent: begin") and now[29] == "// EX-06 local intent: end" and now[30] == "const REFRESH_MS = 30000;"
    gone = [l for l in base if l not in set(now)]
    assert gone == APP_JS_LINES_GONE_XSS01, {"unexpected": [l for l in gone if l not in APP_JS_LINES_GONE_XSS01], "missing": [l for l in APP_JS_LINES_GONE_XSS01 if l not in gone]}
    assert all("${" in l or l in APP_JS_MOVED_INTO_BLOCK for l in gone)
    js, bjs = "\n".join(now), "\n".join(base)
    assert len(re.findall(r"\.innerHTML\s*\+?=", js)) == len(re.findall(r"\.innerHTML\s*\+?=", bjs)) == 27
    assert js.count("fetch(") == bjs.count("fetch(") == 2
    norm = lambda s: re.sub(r"encodeURIComponent\(([^)]*)\)", r"\1", s)  # noqa: E731
    assert sorted(set(re.findall(r"[\"`](/api/[^\"`]*)", norm(js)))) == sorted(set(re.findall(r"[\"`](/api/[^\"`]*)", bjs)))   # the same routes, parameters URL-encoded
    assert js.count("// XSS-01 render: begin") == js.count("// XSS-01 render: end") == 1 and js.index("// EX-06 local intent: end") < js.index("const REFRESH_MS") < js.index("// XSS-01 render: begin") < js.index("// XSS-01 render: end") < js.index("async function api(")
    assert 'score: (v) => (v == null ? "N/A" : `${Number(v)}/100`)' in js and 'href="${esc(' not in js
    assert FL.pre_xss01(APP_JS, js) == at_base(APP_JS)
    # Every line that is not in main's file is either a main line with only esc( ) / encodeURIComponent( ) added, or listed code: a stray line that unwraps to nothing in main, with or
    # without an interpolation, fails here with its own text (a hash derived from the current file certified one). A duplicate of a main line is invisible to the set, so the multiset is pinned too.
    base_set = set(base)
    new = [l for l in now if l not in base_set]
    code = [l for l in new if unwrap(l) not in base_set]
    assert code == APP_JS_NEW_CODE_XSS01, {"unexpected": [l for l in code if l not in APP_JS_NEW_CODE_XSS01], "missing": [l for l in APP_JS_NEW_CODE_XSS01 if l not in code]}
    repeated = {l: c for l, c in (collections.Counter(now) - collections.Counter(base)).items() if l in base_set}
    assert repeated == APP_JS_BASE_LINES_REPEATED_XSS01, repeated
    # The wrapped lines are one to one with main's gone lines: a wrapped copy of some other main line (esc(refreshAll());) fails, and so does a deleted wrapped line or a deleted main line.
    # Main's lines after the block keep main's order, so a moved line fails. An empty call would unwrap to nothing and hide a raw main line from these pins, so there is none.
    wrapped = collections.Counter(unwrap(l) for l in new if l not in code)
    gone_wrapped = collections.Counter(APP_JS_LINES_GONE_XSS01) - APP_JS_GONE_UNPAIRED_XSS01
    assert wrapped == gone_wrapped, {"extra": wrapped - gone_wrapped, "missing": gone_wrapped - wrapped}
    removed_lines = collections.Counter(base) - collections.Counter(now)
    assert removed_lines == collections.Counter(APP_JS_LINES_GONE_XSS01), removed_lines - collections.Counter(APP_JS_LINES_GONE_XSS01)
    after_block = iter(base)
    assert all(l in after_block for l in now[now.index("// XSS-01 render: end") + 1:] if l in base_set), "main's lines after the render block keep main's order"
    assert "esc()" not in js and "encodeURIComponent()" not in js
    assert (len(new), hashlib.sha256("\n".join(new).encode("utf-8")).hexdigest()) == APP_JS_NEW_LINES_XSS01, (len(new), hashlib.sha256("\n".join(new).encode("utf-8")).hexdigest())   # last: the exact text
    for rel in X1_FILES:
        b, n, removed, added = line_ops(rel)
        assert len(removed) == 1 and 'href="${esc(' in removed[0] and 'rel="noopener"' in removed[0], (rel, removed)
        assert len(added) == 3 and added[0] == f"  {ONE_LINE_SAFE}   // XSS-01: only an absolute http(s) URL may become a link" and added[1] == f"  {ONE_LINE_LINK}   // XSS-01: a link, or the escaped text alone", (rel, added)   # the whole line
        old_fragment, new_fragment = X1_LINK_EDIT[rel]
        assert removed[0].count(old_fragment) == 1 and added[2] == removed[0].replace(old_fragment, new_fragment), (rel, added[2])   # every other esc() on the line is kept, verbatim
        assert len(n) == len(b) + 2 and "javascript:" not in "\n".join(n) and FL.pre_xss01(rel, now_text(rel)) == at_base(rel), rel


def test_F04Y_every_changed_older_test_function_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_XSS01 if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = defs(at_base(rel)), defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_XSS01)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^XSS01-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_XSS01.values())
    assert set(FL.UNDO_XSS01) == {p for p in FL.EDITED_XSS01 if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    for rel in sorted(FL.UNDO_XSS01):
        assert FL.undo_xss01(rel, now_text(rel)) == at_base(rel), rel                          # now -> 9df1717a, byte for byte
        for old, new in FL.UNDO_XSS01[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:80])
    for rel in FL.SEMANTIC_XSS01:
        assert FL.pre_xss01(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    FL.UNDO_XSS01["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_xss01("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_xss01("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_XSS01["tests/_probe.py"]


def test_F05Y_the_ledger_changes_only_its_declarations_its_two_helpers_the_four_rehashed_entries_and_the_three_registered_modules():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    for a in ("LEDGER = {", "BASE_EX06 = ", "BASE_XSS01 = ", "\n\ndef unchanged(", "\n\ndef pre_ex06(", "\n\ndef pre_xss01(", "\nEX-06 (", "\nXSS-01 ("):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_XSS01 = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert all(isinstance(n, (ast.Assign, ast.AnnAssign)) for n in region)
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_XSS01", "NEW_XSS01", "EDITED_XSS01", "SEMANTIC_XSS01", "INSTRUMENTATION_XSS01", "NEW_LEDGER_XSS01", "UNDO_XSS01", "SEMANTIC_TESTS_XSS01"]
    code = lambda src: [ast.dump(ast.FunctionDef(name=f.name, args=f.args, body=[s for s in f.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))],  # noqa: E731
                                                 decorator_list=f.decorator_list, returns=f.returns, type_params=[])) for f in ast.parse(src).body]
    assert code(fl[fl.index("\n\ndef pre_xss01("):fl.index("\n\ndef unchanged(")]) == code(HELPERS_XSS01)
    start = fl.index("\nXSS-01 (")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1).replace(" + NEW_EX06 + NEW_XSS01 plus this ledger.", " + NEW_EX06 plus this ledger.", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_XSS01 = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_xss01("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1)
    assert rebuilt == main_fl                                                                 # the paragraph, one line, the declarations, two helpers and the ledger
    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_XSS01 and set(main_ledger) <= set(FL.LEDGER)   # the only new registrations are the X-1 modules
    rehashed = {"frontend/app.js", ".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py", "tests/test_trader_view_55.py"}   # the registered files XSS-01 edits
    assert {k for k in main_ledger if main_ledger[k] != FL.LEDGER[k]} == rehashed == {k for k in main_ledger if k in FL.EDITED_XSS01}
    for rel in rehashed | FL.NEW_LEDGER_XSS01:
        assert FL.LEDGER[rel] == FL.sha(now_text(rel)), rel
    for rel in sorted(FL.NEW_LEDGER_XSS01):
        assert FL.base_text(rel) == at_base(rel) == FL.pre_xss01(rel, now_text(rel)), rel     # unchanged from BASE_68 to main, as EX-06's registrations were
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                      # every registered file still reads as main (BASE_68) to the older freeze tests
