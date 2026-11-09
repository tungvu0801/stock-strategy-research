"""
XSS-01 (R3): the Content Security Policy and nosniff headers the EX-06 gate stamps on every response, and the preconditions that make
`script-src 'self'` and `nosniff` safe to ship: no inline script or handler attribute in either HTML page, no eval / Function / string timer /
document.write / insertAdjacentHTML / outerHTML / srcdoc / javascript: / direct href or src assignment / setAttribute href or src / window.open in any
non-vendor module, none in the vendor chart library, a policy that keeps this exact floor whatever its pinned value says, and the
served MIME types of the scripts and stylesheets (Python's mimetypes can be overridden by the Windows registry; with nosniff a wrong type
would leave the dashboard without scripts). The X-1 modules carry the link helpers. The CSP value itself is pinned here and duplicated in
tests/test_ex06_local_gate.py (X12); both compare it to the live header, so the copies cannot drift unnoticed. Design: XSS01_REMEDIATION_DESIGN.md
(project-planning); tests/test_xss01_freeze.py pins the change set.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient as RawTestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

CSP_XSS01 = "frame-ancestors 'none'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'none'"
HEADERS_XSS01 = (("x-frame-options", "DENY"), ("content-security-policy", CSP_XSS01), ("x-content-type-options", "nosniff"))
VENDOR = ROOT / "frontend" / "vendor" / "lightweight-charts" / "lightweight-charts.standalone.production.js"
MODULES = sorted(p for p in (ROOT / "frontend").glob("*.js"))
PAGES = sorted((ROOT / "frontend").glob("*.html"))
FORBIDDEN_JS = (r"\beval\(", r"new Function\(", r"set(Timeout|Interval)\(\s*[\"'`]", r"document\.write", r"insertAdjacentHTML", r"\.outerHTML\s*=",
                r"srcdoc", r"javascript:", r"\.setAttribute\(\s*[\"']on", r"createElement\(\s*[\"']script",
                r"\.(href|src)\s*=(?!=)", r"\.setAttribute\(\s*[\"'](href|src)[\"']", r"\bwindow\.open\(")   # a URL set on a DOM property skips safeHref and N11; script-src does not stop leaving for an external page


@pytest.fixture(scope="module")
def client():
    from api.server import app
    from fastapi.testclient import TestClient            # the D-20 dashboard client (tests/conftest.py)
    with TestClient(app) as c:
        yield c


def _hdr(r):
    return tuple((k, r.headers.get(k)) for k, _ in HEADERS_XSS01)


def test_C1_every_response_carries_the_csp_nosniff_and_frame_headers_and_the_scripts_and_styles_have_their_mime_types(client):
    from api.server import app
    raw = RawTestClient(app)                                                               # presents as nothing: refused by the gate (403)
    seen = {}
    for path in ("/", "/index.html", "/forward_validation.html", "/app.js", "/style.css", "/product_ui.css", "/api/health", "/nope-xss01.html"):
        r = client.get(path)
        seen[path] = r.status_code
        assert _hdr(r) == HEADERS_XSS01, (path, dict(r.headers))
    assert seen["/"] == 200 and seen["/index.html"] == 200 and seen["/forward_validation.html"] == 200 and seen["/app.js"] == 200 and seen["/api/health"] == 200
    assert seen["/nope-xss01.html"] == 404
    r405 = client.post("/api/health", headers={"Content-Type": "application/json"}, content=b"{}")
    assert r405.status_code == 405 and _hdr(r405) == HEADERS_XSS01
    r403 = raw.get("/api/health")
    assert r403.status_code == 403 and r403.json()["error"].startswith("LOCAL_GATE_") and _hdr(r403) == HEADERS_XSS01
    js, css = client.get("/app.js"), client.get("/style.css")
    assert js.headers["content-type"].split(";")[0].strip() in ("text/javascript", "application/javascript"), js.headers["content-type"]   # nosniff precondition
    assert css.headers["content-type"].split(";")[0].strip() == "text/css", css.headers["content-type"]
    html = client.get("/")
    assert html.headers["content-type"].split(";")[0].strip() == "text/html"
    assert "// XSS-01 render: begin" in js.text and "// EX-06 local intent: begin" in js.text                  # the served file is the escaped one


def test_C2_the_gate_defines_exactly_these_three_header_pairs_in_this_order_and_nothing_else_sets_a_csp():
    import api.local_gate as G
    assert G.FRAME_HEADERS == tuple((k.encode(), v.encode()) for k, v in HEADERS_XSS01)
    others = [p for p in (ROOT / "api").rglob("*.py") if p.name != "local_gate.py"]
    assert not [p for p in others if re.search(r"content-security-policy|x-content-type-options", p.read_text(encoding="utf-8"), re.I)]
    doc = (ROOT / "api" / "local_gate.py").read_text(encoding="utf-8")
    assert "script-src 'self'" in doc.split('"""')[1] and "nosniff" in doc.split('"""')[1]                  # the module docstring states the policy


def test_C3_the_two_pages_have_no_inline_script_no_handler_attribute_no_javascript_url_and_only_relative_same_origin_scripts():
    assert [p.name for p in PAGES] == ["forward_validation.html", "index.html"]
    for p in PAGES:
        html = p.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), p.name                                 # no inline script body
        assert not re.search(r"<script[^>]*>\s*[^<\s]", html), p.name
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), p.name                                                # no handler attribute
        assert "javascript:" not in html.lower() and "<base" not in html.lower() and "srcdoc" not in html.lower(), p.name
        for src in re.findall(r'<script src="([^"]+)"', html):
            assert not re.match(r"^(https?:)?//", src) and not src.startswith("/"), (p.name, src)              # relative, same origin
        for href in re.findall(r'<link rel="stylesheet" href="([^"]+)"', html):
            assert not re.match(r"^(https?:)?//", href), (p.name, href)
        assert "<form" not in html.lower(), p.name                                                                 # form-action 'self' constrains nothing legitimate


def test_C4_no_non_vendor_module_uses_eval_function_string_timers_document_write_insertadjacenthtml_outerhtml_srcdoc_or_javascript_urls():
    bad = []
    for p in MODULES:
        js = p.read_text(encoding="utf-8")
        for pat in FORBIDDEN_JS:
            for m in re.finditer(pat, js):
                bad.append((p.name, js[:m.start()].count("\n") + 1, pat))
    assert bad == [], bad
    assert len(MODULES) >= 40


def test_C5_the_vendor_chart_library_needs_neither_unsafe_eval_nor_workers_nor_blob_urls():
    js = VENDOR.read_text(encoding="utf-8", errors="replace")
    assert "eval(" not in js and "Function(" not in js and "new Worker" not in js and "createObjectURL" not in js and "importScripts" not in js


def test_C6_the_x1_modules_build_external_links_only_through_the_shared_helper_and_no_module_interpolates_a_raw_url_into_an_href():
    one_safe = 'const safeHref = (u) => { try { const p = new URL(String(u)); return p.protocol === "http:" || p.protocol === "https:" ? esc(p.href) : null; } catch (e) { return null; } };'
    one_link = 'const extLink = (u, text) => { const h = safeHref(u); return h ? `<a href="${h}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text); };'
    for name in ("market.js", "command_center.js", "evidence_research.js"):
        js = (ROOT / "frontend" / name).read_text(encoding="utf-8")
        lines = js.replace("\r\n", "\n").split("\n")                                          # the whole line, comment included: nothing may follow the helper
        assert lines.count(f"  {one_safe}   // XSS-01: only an absolute http(s) URL may become a link") == 1 and lines.count(f"  {one_link}   // XSS-01: a link, or the escaped text alone") == 1, name
        assert 'href="${esc(' not in js, name
        assert len(re.findall(r"\bextLink\(", js)) >= 1, name                                   # at least one use (the definition is an arrow)
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert app.count("// XSS-01 render: begin") == 1 and app.count("// XSS-01 render: end") == 1
    assert 'href="${esc(' not in app and 'href="${e.source_url}' not in app and 'href="${c.url}' not in app
    for name in [p.name for p in MODULES]:                                                    # no module anywhere interpolates a raw URL into an href
        js = (ROOT / "frontend" / name).read_text(encoding="utf-8")
        for m in re.finditer(r'href=["\']?\$\{([^}]*)\}', js):
            expr = m.group(1).strip()
            # h: the shared helper's checked URL; PAGE: forward_validation.js's own page path constant; esc(<server-composed internal link>) in product_ui.js only
            assert expr in ("h", "PAGE") or (expr.startswith("esc(") and name == "product_ui.js"), (name, m.group(0))


def test_C7_node_runs_the_xss01_frontend_test_file_when_node_is_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    run = subprocess.run([node, "--test", "--test-reporter=tap", "tests/frontend/xss01_app_js.test.cjs"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    m = re.search(r"^# pass (\d+)", run.stdout, re.M)
    assert m and int(m.group(1)) >= 16 and re.search(r"^# fail 0$", run.stdout, re.M), run.stdout[-2000:]


def test_C8_the_csp_stays_this_strict_whatever_the_pinned_value_says():
    import api.local_gate as G
    values = [v for k, v in G.FRAME_HEADERS if k == b"content-security-policy"]
    assert len(values) == 1, values                                                              # exactly one policy
    parts = [d.split() for d in values[0].decode().split(";") if d.strip()]
    d = {p[0]: p[1:] for p in parts}
    assert len(d) == len(parts), parts                                                           # no directive twice
    assert all(d.get(k) == ["'none'"] for k in ("frame-ancestors", "object-src", "base-uri", "frame-src")), d
    assert all(d.get(k) == ["'self'"] for k in ("script-src", "form-action")), d
    assert all(t in ("'self'", "'none'") for v in d.values() for t in v), d                      # no host, scheme, wildcard, nonce or 'unsafe-*', in any directive, whatever CSP_XSS01 says
