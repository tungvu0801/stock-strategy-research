"""
EX-06: the local request gate (api/local_gate.py). Browser CSRF, DNS rebinding, Host / Origin / Referer, state-changing GETs (D-14), WebSocket
origin, the dashboard's Arm / Disarm / Emergency Stop / Clear / Pause / Resume, the Windows runner probe and the local tools, IPv4 / IPv6
localhost, proxies, fail-closed behaviour, the refusal contract, the D-20 test client and the independent emergency shutdown (D-19).

The gate is not authentication (D-10 A): every header it checks can be sent by any local process. These tests use starlette's RAW client (it
presents as nothing) or a probe app behind the gate; tests/conftest.py's default client presents as the dashboard in Edge (T-X14).
"""
import asyncio
import importlib
import io
import json
import logging
import re
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient as RawClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
BASE = "46ebc65671435a02a0c90f6cd40f7d277ab15da2"                                  # main before EX-06 (PR #53 merged)
O = "http://127.0.0.1:8000"
LOCAL = ("127.0.0.1", 50000)
# What headless Edge sends from the dashboard (recorded by the EX-06 simulation's real-browser check; T-X14 pins the conftest client to it).
EDGE_API_GET = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "referer": O + "/", "x-stock-agent-intent": "dashboard"}
EDGE_API_POST = {**EDGE_API_GET, "origin": O, "content-type": "application/json"}
EDGE_NAVIGATE = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
EDGE_WEBSOCKET = {"origin": O}
GET_H = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Referer": O + "/", "X-Stock-Agent-Intent": "dashboard"}
POST_H = {**GET_H, "Origin": O, "Content-Type": "application/json"}
CROSS = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "empty"}


def G():
    return importlib.import_module("api.local_gate")


def probe(client=LOCAL, base_url=O):
    """A probe app behind the gate: records every request that got through, answers 200 / accepts a WebSocket."""
    calls = []

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            calls.append(("lifespan",))
            while True:
                msg = await receive()
                await send({"type": msg["type"] + ".complete"})
                if msg["type"] == "lifespan.shutdown":
                    return
        if scope["type"] == "websocket":
            await receive()
            calls.append(("WS", scope["path"]))
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})
            return
        calls.append((scope["method"], scope["path"]))
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{\"ok\": true}"})
    return RawClient(G().LocalRequestGate(app), base_url=base_url, client=client), calls


def refused(r):
    return r.status_code, (r.json() if r.content else {}).get("error")


def ws_close_code(c, url, headers):
    try:
        with c.websocket_connect(url, headers=headers):
            return "ACCEPTED"
    except WebSocketDisconnect as exc:
        return exc.code


# ---------------------------------------------------------------- installation
def test_X01_the_gate_is_the_outermost_middleware_no_cors_no_trusted_host_and_the_docs_pages_are_off():
    from api.server import app
    from fastapi.testclient import TestClient
    assert app.user_middleware[0].cls is G().LocalRequestGate
    assert not [m for m in app.user_middleware if m.cls.__name__ in ("CORSMiddleware", "TrustedHostMiddleware", "GZipMiddleware", "SessionMiddleware")]
    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None) and app.openapi()["paths"]   # D-16; the schema still builds for tests
    c = TestClient(app)
    for page in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = c.get(page)
        assert r.status_code == 404 and "LOCAL_GATE" not in r.text, page


def _concrete(path):
    return re.sub(r"\{[^}]+\}", "AAPL", path)


def _all_routes(routes):
    """Every route, through FastAPI's included-router wrappers (fastapi 0.14x keeps them as _IncludedRouter.original_router)."""
    out = []
    for r in routes:
        sub = getattr(r, "original_router", None)
        out += _all_routes(sub.routes) if sub is not None else [r]
    return out


def test_X02_every_route_is_behind_the_gate_and_a_forged_request_never_reaches_a_handler():
    from api.server import app
    c = RawClient(app, base_url=O, client=LOCAL)
    routes = _all_routes(app.routes)
    verbs = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
    schema = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops if m.upper() in verbs}   # FastAPI's effective paths
    hidden = {(m, r.path) for r in routes if getattr(r, "methods", None) and getattr(r, "include_in_schema", True) is False for m in r.methods}
    http = sorted(schema | hidden)
    ws = sorted(r.path for r in routes if type(r).__name__ in ("WebSocketRoute", "APIWebSocketRoute"))
    assert len(schema) >= len({(m, r.path) for r in routes if getattr(r, "methods", None) for m in r.methods}) - len(hidden), "the schema lists every route"
    gets = [p for m, p in http if m == "GET"]
    unsafe = [(m, p) for m, p in http if m not in ("GET", "HEAD")]
    assert len(gets) >= 180 and len(unsafe) >= 79 and ws == ["/api/terminal/stream"], (len(gets), len(unsafe), ws)
    assert all(G().is_api(p) for p in gets + [p for _, p in unsafe]), "every route is an /api route (pages are static files)"
    for m, p in http:
        url = _concrete(p)
        if m in ("GET", "HEAD"):
            r = c.request(m, url)                                                       # a script, curl or an old browser: no intent, no metadata
            assert r.status_code == 403 and (m == "HEAD" or r.json()["error"] == "LOCAL_GATE_INTENT"), (m, p, r.status_code)
            r = c.request(m, url, headers={**GET_H, **CROSS})                           # a cross-site <img> / no-cors read, even with the header
            assert r.status_code == 403 and (m == "HEAD" or r.json()["error"] == "LOCAL_GATE_FETCH_SITE"), (m, p)
        else:
            r = c.request(m, url, headers={"Content-Type": "application/json"}, content=b"{}")
            assert refused(r) == (403, "LOCAL_GATE_INTENT"), (m, p, r.text)
            r = c.request(m, url, headers={**POST_H, "Origin": "https://evil.example"}, content=b"{}")
            assert refused(r) == (403, "LOCAL_GATE_ORIGIN"), (m, p, r.text)
            r = c.request("OPTIONS", url, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": m,
                                                  "Access-Control-Request-Headers": "x-stock-agent-intent,content-type"})
            assert refused(r) == (403, "LOCAL_GATE_ORIGIN") and not [k for k in r.headers if k.lower().startswith("access-control-")], (m, p)
    for p in ws:
        assert ws_close_code(c, "ws://127.0.0.1:8000" + p, {"Origin": "http://evil.example"}) == 1008


# ---------------------------------------------------------------- browser CSRF
@pytest.mark.parametrize("label,headers,body,want", [
    ("text/plain JSON form", {"Content-Type": "text/plain", "Origin": "http://evil.example", **CROSS}, b'{"reason":"=x"}', "LOCAL_GATE_ORIGIN"),
    ("url-encoded form", {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil.example", **CROSS}, b"reason=x", "LOCAL_GATE_ORIGIN"),
    ("multipart form", {"Content-Type": "multipart/form-data; boundary=b", "Origin": "http://evil.example", **CROSS}, b"--b--", "LOCAL_GATE_ORIGIN"),
    ("no-cors fetch, old browser (no Sec-Fetch)", {"Content-Type": "text/plain", "Origin": "http://evil.example"}, b"{}", "LOCAL_GATE_ORIGIN"),
    ("privacy-stripped Origin null", {"Content-Type": "text/plain", "Origin": "null", **CROSS}, b"{}", "LOCAL_GATE_ORIGIN"),
    ("no Origin, foreign Referer", {"Content-Type": "application/json", "Referer": "http://evil.example/x", "X-Stock-Agent-Intent": "dashboard"}, b"{}", "LOCAL_GATE_REFERER"),
    ("no Origin, same-site Sec-Fetch", {"Content-Type": "application/json", "Sec-Fetch-Site": "same-site", "X-Stock-Agent-Intent": "dashboard"}, b"{}", "LOCAL_GATE_FETCH_SITE"),
    ("no Origin, no metadata, no intent", {"Content-Type": "application/json"}, b"{}", "LOCAL_GATE_INTENT"),
    ("another local port (same-site)", {**POST_H, "Origin": "http://127.0.0.1:8031", "Sec-Fetch-Site": "same-site"}, b"{}", "LOCAL_GATE_ORIGIN"),
    ("https Origin", {**POST_H, "Origin": "https://127.0.0.1:8000"}, b"{}", "LOCAL_GATE_ORIGIN"),
    ("unknown intent value", {**POST_H, "X-Stock-Agent-Intent": "anything"}, b"{}", "LOCAL_GATE_INTENT"),
])
def test_X03_browser_csrf_shapes_are_refused_before_any_handler(label, headers, body, want):
    c, calls = probe()
    r = c.post("/api/execution/auto/enable", headers=headers, content=body)
    assert refused(r) == (403, want) and calls == [], label


ACTIONS = [("api.routes.execution", "/api/execution/auto/enable", "enable_auto"), ("api.routes.execution", "/api/execution/auto/disable", "disable_auto"),
           ("api.routes.execution_safety", "/api/execution-safety/emergency-stop", "emergency_stop"),
           ("api.routes.execution_safety", "/api/execution-safety/clear-emergency-stop", "clear_emergency_stop"),
           ("api.routes.execution_safety", "/api/execution-safety/pause-new-entries", "pause"),
           ("api.routes.execution_safety", "/api/execution-safety/resume-new-entries", "resume")]


class _Out(dict):
    def model_dump(self):
        return {}


class _Store:
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: _Out()


class _Svc:
    def __init__(self):
        self.calls, self.store = [], _Store()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def f(*a, **k):
            self.calls.append(name)
            return _Out()
        return f


@pytest.mark.parametrize("module,path,fn", ACTIONS)
def test_X04_arm_disarm_emergency_stop_clear_pause_resume_forged_never_reach_the_service_the_dashboard_does(monkeypatch, module, path, fn):
    svc = _Svc()
    monkeypatch.setattr(importlib.import_module(module), "_service", lambda: svc)
    from api.server import app
    c = RawClient(app, base_url=O, client=LOCAL)
    forged = [{"Content-Type": "text/plain"}, {**POST_H, "Origin": "http://evil.example"}, {**POST_H, **CROSS},
              {**POST_H, "Host": "rebind.evil:8000", "Origin": "http://rebind.evil:8000"}, {**POST_H, "Origin": "null"},
              {**POST_H, "X-Forwarded-For": "203.0.113.9"}, {**POST_H, "X-Stock-Agent-Intent": "evil"}, {k: v for k, v in POST_H.items() if k != "X-Stock-Agent-Intent"}]
    for h in forged:
        r = c.post(path, headers=h, content=b"{}")
        assert r.status_code == 403 and r.json()["error"].startswith("LOCAL_GATE_"), (h, r.text)
    assert svc.calls == [], "D-19: no safety route is exempt; a forged stop / pause / disarm never reaches the service either"
    r = c.post(path, headers=POST_H, content=b"{}")
    assert r.status_code == 200 and svc.calls == [fn], r.text
    r = c.post(path, headers=POST_H)                                                    # a body-less POST that still names JSON
    assert r.status_code == 200 and svc.calls == [fn, fn], r.text
    r = c.post(path, headers={k: v for k, v in POST_H.items() if k != "Content-Type"})  # the dashboard's body-less POST: no body, no type (market.js:84, app.js:692)
    assert r.status_code == 200 and svc.calls == [fn, fn, fn], r.text


# ---------------------------------------------------------------- DNS rebinding, Host, Origin, Referer
@pytest.mark.parametrize("host", ["rebind.evil:8000", "evil.example", "localhost.evil:8000", "127.0.0.1.nip.io:8000", "0.0.0.0:8000", "[::ffff:127.0.0.1]:8000",
                                  "192.168.1.20:8000", "MY-PC:8000", "localhost.:8000", "127.0.0.1:8001", "localhost:80", "127.0.0.1:8000.evil"])
def test_X05_dns_rebinding_and_foreign_hosts_are_refused_for_pages_reads_writes_and_websockets(host):
    c, calls = probe()
    same = {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin", "X-Stock-Agent-Intent": "dashboard", "Content-Type": "application/json"}
    for r in (c.get("/", headers={"Host": host, **{k.title(): v for k, v in EDGE_NAVIGATE.items()}}), c.get("/api/execution/status", headers={**GET_H, "Host": host}),
              c.post("/api/execution/auto/enable", headers=same, content=b"{}")):
        assert refused(r) == (403, "LOCAL_GATE_HOST"), (host, r.request.url)
    assert ws_close_code(c, "ws://127.0.0.1:8000/api/terminal/stream", {"Host": host, "Origin": f"http://{host}"}) == 1008
    assert calls == []


def test_X06_host_origin_and_referer_forms():
    c, calls = probe()
    for h in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST:8000", "127.0.0.1", "localhost", "[::1]"):   # D-1 (b'): port P or none
        assert c.get("/api/x", headers={**GET_H, "Host": h}).status_code == 200, h
    for o in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000", "HTTP://LOCALHOST:8000"):
        assert c.post("/api/x", headers={**POST_H, "Origin": o}).status_code == 200, o
    for o in ("http://127.0.0.1", "http://localhost", "http://127.0.0.1:8000/", "http://127.0.0.1:8000/x", "null", "", "http://127.0.0.1:8000, http://evil.example"):
        assert refused(c.post("/api/x", headers={**POST_H, "Origin": o})) == (403, "LOCAL_GATE_ORIGIN"), o
    assert refused(c.get("/api/x", headers=[("Host", "127.0.0.1:8000"), ("Host", "evil.example"), ("X-Stock-Agent-Intent", "dashboard")])) == (403, "LOCAL_GATE_HOST")
    assert refused(c.post("/api/x", headers=[("Origin", O), ("Origin", O), ("X-Stock-Agent-Intent", "dashboard")])) == (403, "LOCAL_GATE_ORIGIN")
    nori = {k: v for k, v in POST_H.items() if k != "Origin"}
    assert c.post("/api/x", headers={**nori, "Referer": O + "/forward_validation.html"}).status_code == 200
    for ref in ("http://evil.example/", "http://127.0.0.1:8031/", "https://127.0.0.1:8000/", "null"):
        assert refused(c.post("/api/x", headers={**nori, "Referer": ref})) == (403, "LOCAL_GATE_REFERER"), ref
        assert refused(c.get("/api/x", headers={**GET_H, "Referer": ref})) == (403, "LOCAL_GATE_REFERER"), ref
    assert len(calls) == 7 + 4 + 1


# ---------------------------------------------------------------- state-changing GETs (D-14)
def test_X07_every_api_read_needs_the_intent_or_browser_same_origin_metadata_pages_do_not(monkeypatch):
    c, calls = probe()
    ok = [GET_H, {"X-Stock-Agent-Intent": "dashboard"},                                  # the dashboard (app.js wrapper); a local tool (runner, scripts)
          {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Referer": O + "/forward_validation.html"},   # the FV page
          {**{k.title(): v for k, v in EDGE_NAVIGATE.items()}}, {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}]   # typed URL; export link
    for h in ok:
        assert c.get("/api/forward-validation/export/trades", headers=h).status_code == 200, h
        assert c.head("/api/execution-safety", headers=h).status_code == 200, h
    bad = [({}, "LOCAL_GATE_INTENT"), ({"Referer": O + "/"}, "LOCAL_GATE_INTENT"), ({**GET_H, **CROSS}, "LOCAL_GATE_FETCH_SITE"),
           ({**CROSS, "Sec-Fetch-Site": "same-site"}, "LOCAL_GATE_FETCH_SITE"), ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}, "LOCAL_GATE_FETCH_SITE"),
           ({"Referer": "http://evil.example/"}, "LOCAL_GATE_REFERER"), ({"Sec-Fetch-Site": "garbage", "X-Stock-Agent-Intent": "dashboard"}, "LOCAL_GATE_FETCH_SITE"),
           ([("X-Stock-Agent-Intent", "dashboard"), ("X-Stock-Agent-Intent", "dashboard")], "LOCAL_GATE_INTENT"),
           ([("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")], "LOCAL_GATE_FETCH_SITE")]
    for h, want in bad:
        assert refused(c.get("/api/execution-safety", headers=h)) == (403, want), h
    assert c.head("/api/execution-safety").status_code == 403                             # HEAD is a read like GET
    for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):                                 # a state change is never a "page", whatever the path
        assert refused(c.request(m, "/", headers={"Content-Type": "application/json"}, content=b"{}")) == (403, "LOCAL_GATE_INTENT"), m
        assert refused(c.request(m, "/index.html")) == (403, "LOCAL_GATE_INTENT"), m
    for page in ("/", "/index.html", "/app.js", "/forward_validation.html", "/product_ui.css"):
        assert c.get(page).status_code == 200, page                                      # pages and static files: no side effect
        assert c.get(page, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                                    "Referer": "http://evil.example/"}).status_code == 200, page   # a link from another site opens the dashboard
        for dest in ("iframe", "image", "script", "empty"):
            r = c.get(page, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate" if dest == "iframe" else "no-cors", "Sec-Fetch-Dest": dest})
            assert refused(r) == (403, "LOCAL_GATE_FETCH_SITE"), (page, dest)
    mod = G()
    for odd in ("/API/execution-safety", "//api/execution-safety", "/Api/x", "/api", "///api/x", "/api/"):  # raw scopes: the test client rewrites "//"
        scope = {"type": "http", "method": "GET", "path": odd, "client": LOCAL, "server": ("127.0.0.1", 8000), "headers": [(b"host", b"127.0.0.1:8000")]}
        assert mod.is_api(odd) and mod.refusal(scope) == (403, "FORBIDDEN", "LOCAL_GATE_INTENT"), odd   # fail closed on spelling
    assert not mod.is_api("/apix") and not mod.is_api("/") and not mod.is_api("/x/api")
    svc = _Svc()
    monkeypatch.setattr(importlib.import_module("api.routes.execution_safety"), "_service", lambda: svc)
    from api.server import app
    real = RawClient(app, base_url=O, client=LOCAL)
    for h in ({**CROSS, "Sec-Fetch-Dest": "image", "Referer": "http://evil.example/"}, {"Referer": "http://evil.example/"}, {}):
        assert real.get("/api/execution-safety", headers=h).status_code == 403            # GET /api/execution-safety may persist (safety/service.py)
    assert svc.calls == []
    assert real.get("/api/execution-safety", headers=GET_H).status_code == 200 and svc.calls == ["overview"]


# ---------------------------------------------------------------- WebSocket origin
def test_X08_websocket_origin_host_client_and_proxy_checks():
    c, calls = probe()
    url = "ws://127.0.0.1:8000/api/terminal/stream"
    for origin in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
        assert ws_close_code(c, url, {"Origin": origin}) == "ACCEPTED", origin
    assert ws_close_code(c, url, {}) == "ACCEPTED"                                        # D-13 (a): a non-browser client sends no Origin
    for h in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:8031"}, {"Origin": "https://127.0.0.1:8000"},
              {"Origin": O, "Host": "rebind.evil:8000"}, {"Origin": O, "X-Forwarded-Host": "evil.example"}, {"Origin": O, "Forwarded": "for=203.0.113.9"}):
        assert ws_close_code(c, url, h) == 1008, h
    far, _ = probe(client=("192.168.1.20", 5000))
    assert ws_close_code(far, url, {"Origin": O}) == 1008
    assert [x for x in calls if x[0] == "WS"] == [("WS", "/api/terminal/stream")] * 4


# ---------------------------------------------------------------- IPv4 / IPv6 localhost
def test_X09_ipv4_and_ipv6_loopback_clients_and_hosts():
    for client in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
        c, _ = probe(client=(client, 5000))
        assert c.get("/api/x", headers=GET_H).status_code == 200, client
    for client in ("192.168.1.20", "10.0.0.5", "::ffff:192.168.1.20", "fe80::1", "2001:db8::1", "0.0.0.0", "testclient", "", None):
        c, calls = probe(client=(client, 5000))
        assert refused(c.get("/api/x", headers=GET_H)) == (403, "LOCAL_GATE_CLIENT") and calls == [], client
    c6, _ = probe(base_url="http://[::1]:8000")
    assert c6.post("/api/x", headers={**POST_H, "Host": "[::1]:8000", "Origin": "http://[::1]:8000"}).status_code == 200
    other, _ = probe(base_url="http://127.0.0.1:8031")                                    # the bound port comes from the server, not a constant
    assert other.post("/api/x", headers={**POST_H, "Host": "127.0.0.1:8031", "Origin": "http://127.0.0.1:8031"}).status_code == 200
    assert refused(other.post("/api/x", headers={**POST_H, "Host": "127.0.0.1:8031", "Origin": O})) == (403, "LOCAL_GATE_ORIGIN")
    assert refused(other.get("/api/x", headers={**GET_H, "Host": "127.0.0.1:8000"})) == (403, "LOCAL_GATE_HOST")


# ---------------------------------------------------------------- proxies (D-5, D-12: none supported, fail closed)
@pytest.mark.parametrize("name", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Port", "X-Forwarded-Prefix", "X-Real-IP", "Via"])
def test_X10_any_forwarding_header_is_refused_even_on_an_otherwise_perfect_request(name):
    c, calls = probe()
    assert refused(c.post("/api/x", headers={**POST_H, name: "203.0.113.9"})) == (403, "LOCAL_GATE_PROXY")
    assert refused(c.get("/", headers={name: "x"})) == (403, "LOCAL_GATE_PROXY") and calls == []


# ---------------------------------------------------------------- fail closed
def test_X11_the_gate_fails_closed(monkeypatch):
    mod = G()
    c, calls = probe()

    def boom(scope):
        raise RuntimeError("gate bug")
    monkeypatch.setattr(mod, "refusal", boom)
    assert refused(c.get("/api/x", headers=GET_H)) == (403, "LOCAL_GATE_ERROR")
    assert refused(c.get("/")) == (403, "LOCAL_GATE_ERROR")
    assert ws_close_code(c, "ws://127.0.0.1:8000/api/terminal/stream", {"Origin": O}) == 1008
    monkeypatch.undo()
    base = {"type": "http", "method": "GET", "path": "/api/x", "client": LOCAL, "server": ("127.0.0.1", 8000),
            "headers": [(b"host", b"127.0.0.1:8000"), (b"x-stock-agent-intent", b"dashboard")]}
    assert mod.refusal(base) is None
    for bad, want in (({"server": None}, "LOCAL_GATE_HOST"), ({"server": ("127.0.0.1", None)}, "LOCAL_GATE_HOST"), ({"server": ("127.0.0.1", 0)}, "LOCAL_GATE_HOST"),
                      ({"server": ("127.0.0.1", "8000")}, "LOCAL_GATE_HOST"), ({"client": None}, "LOCAL_GATE_CLIENT"), ({"headers": []}, "LOCAL_GATE_HOST"),
                      ({"method": "POST", "headers": [(b"host", b"127.0.0.1:8000")]}, "LOCAL_GATE_INTENT"),
                      ({"method": None, "headers": [(b"host", b"127.0.0.1:8000")]}, "LOCAL_GATE_INTENT"),
                      ({"path": None}, "LOCAL_GATE_ERROR"), ({"path": ""}, "LOCAL_GATE_ERROR"), ({"path": "api/x"}, "LOCAL_GATE_ERROR"), ({"path": b"/api/x"}, "LOCAL_GATE_ERROR")):
        assert mod.refusal({**base, **bad})[2] == want, bad
    with c:                                                                               # lifespan passes straight through
        pass
    assert ("lifespan",) in calls
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])
    with pytest.raises(RuntimeError, match="unsupported ASGI scope type"):
        asyncio.run(mod.LocalRequestGate(app)({"type": "custom"}, None, None))
    assert seen == []                                                                     # an unknown scope type never reaches the app


# ---------------------------------------------------------------- the refusal contract (D-22) and framing (D-17)
def test_X12_refusal_contract_log_line_framing_and_json_body(caplog):
    c, calls = probe()
    with caplog.at_level(logging.WARNING, logger="api.local_gate"):
        r = c.post("/api/execution/auto/enable", headers={"Origin": "http://evil.example", "Content-Type": "text/plain", "Cookie": "secret=1"}, content=b"x")
    assert r.status_code == 403 and r.json() == {"status": "FORBIDDEN", "error": "LOCAL_GATE_ORIGIN", "message": G().MESSAGE}
    assert r.headers["x-frame-options"] == "DENY" and r.headers["content-security-policy"] == "frame-ancestors 'none'" and r.headers["cache-control"] == "no-store"
    lines = [x for x in caplog.records if x.name == "api.local_gate"]
    assert len(lines) == 1 and "POST" in lines[0].getMessage() and "/api/execution/auto/enable" in lines[0].getMessage() and "LOCAL_GATE_ORIGIN" in lines[0].getMessage()
    assert "evil.example" not in caplog.text and "secret" not in caplog.text                # never a header value
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="api.local_gate"):
        c.get("/api/a%0AFAKE%20LOG%20LINE")
    lines = [x for x in caplog.records if x.name == "api.local_gate"]
    assert len(lines) == 1 and "\\nFAKE LOG LINE" in lines[0].getMessage() and "\nFAKE LOG LINE" not in caplog.text   # logged once, the path escaped
    r = c.post("/api/x", headers={**POST_H, "Content-Type": "text/plain"}, content=b"{}")
    assert r.status_code == 422 and r.json()["status"] == "INVALID_REQUEST" and r.json()["error"] == "LOCAL_GATE_CONTENT_TYPE"
    assert c.post("/api/x", headers={**POST_H, "Content-Type": "application/json; charset=utf-8"}, content=b"{}").status_code == 200
    assert c.post("/api/x", headers={k: v for k, v in POST_H.items() if k != "Content-Type"}).status_code == 200     # no body, no type: allowed
    assert c.delete("/api/x", headers={k: v for k, v in POST_H.items() if k != "Content-Type"}).status_code == 200   # the watchlist DELETE (app.js:260)
    ok = c.get("/")
    assert ok.status_code == 200 and ok.headers["x-frame-options"] == "DENY" and ok.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert G().CODES == ("CLIENT", "HOST", "PROXY", "ORIGIN", "REFERER", "FETCH_SITE", "INTENT", "CONTENT_TYPE", "ERROR")


# ---------------------------------------------------------------- Stage 4.6B unchanged (D-21)
def test_X13_the_stage_46b_order_gate_is_byte_identical_and_still_requires_paper_order():
    rel = "api/routes/alpaca_paper_orders.py"
    base = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
    assert base and (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == base.replace(b"\r\n", b"\n")
    from api.server import app
    c = RawClient(app, base_url=O, client=LOCAL)
    body = b'{"preview_id": 1, "preview_hash": "' + b"0" * 64 + b'", "confirm": true}'   # a valid ConfirmBody, so 4.6B's own _gate decides
    r = c.post("/api/alpaca-paper-orders/confirm", headers=POST_H, content=body)         # through EX-06 with the dashboard intent ...
    assert (r.status_code, r.json().get("status"), r.json().get("message")) == (403, "FORBIDDEN", "Missing X-Stock-Agent-Intent header.")   # ... 4.6B still says no
    r = c.post("/api/alpaca-paper-orders/confirm", headers={**POST_H, "Host": "[::1]:8000", "Origin": "http://[::1]:8000"}, content=body)
    assert (r.status_code, r.json().get("message")) == (403, "Unexpected Host header.")   # its known [::1] limit, unchanged (D-21)


def test_X18_the_stage_46b_gate_is_still_a_working_second_layer_on_its_own():
    """D-21: with EX-06 in front, test_alpaca_orders_46b's test_L forged shapes are now refused by EX-06 first (its status-only asserts still
    pass). Here 4.6B's own _gate (byte-identical, X13) is driven WITHOUT EX-06 and must refuse each shape with its own message; EX-06 must
    refuse each one too, before any handler. Nothing here reaches the order service: only refusals, and one direct _gate() call."""
    from starlette.requests import Request
    from api.routes.alpaca_paper_orders import _gate

    def gate46(client="127.0.0.1", host="127.0.0.1:8000", origin=None, ctype="application/json", intent="dashboard", query=b""):
        h = [(b"host", host.encode())] + [(k, v.encode()) for k, v in ((b"origin", origin), (b"content-type", ctype), (b"x-stock-agent-intent", intent)) if v]
        r = _gate(Request({"type": "http", "method": "POST", "path": "/api/alpaca-paper-orders/preview", "headers": h, "client": (client, 50000),
                           "server": ("127.0.0.1", 8000), "query_string": query}))
        return None if r is None else (r.status_code, json.loads(r.body)["message"])
    assert gate46(client="192.168.1.50") == (403, "Paper order endpoints accept requests from this computer only.")
    assert gate46(host="evil.example:8000") == (403, "Unexpected Host header.")
    assert gate46(origin="http://evil.example") == (403, "Unexpected Origin.")
    assert gate46(ctype="text/plain") == (422, "JSON body required.")
    assert gate46(query=b"x=1") == (422, "This endpoint takes no query parameters.")
    assert gate46() == (403, "Missing X-Stock-Agent-Intent header.")                       # the dashboard intent is not 4.6B's
    assert gate46(origin=O, intent="paper-order") is None                                  # only its own intent passes (the gate alone)
    good = b'{"symbol": "AMD", "side": "BUY", "quantity": 1}'
    for client, headers, want in ((("192.168.1.50", 50000), POST_H, (403, "LOCAL_GATE_CLIENT")), (LOCAL, {**POST_H, "Host": "evil.example:8000"}, (403, "LOCAL_GATE_HOST")),
                                  (LOCAL, {**POST_H, "Origin": "http://evil.example"}, (403, "LOCAL_GATE_ORIGIN")),
                                  (LOCAL, {**POST_H, "Content-Type": "text/plain"}, (422, "LOCAL_GATE_CONTENT_TYPE"))):
        c, calls = probe(client=client)
        assert refused(c.post("/api/alpaca-paper-orders/preview", headers=headers, content=good)) == want and calls == [], want


# ---------------------------------------------------------------- the D-20 test client
def test_X14_the_default_test_client_sends_exactly_what_edge_sends_from_the_dashboard():
    import fastapi.testclient as ftc
    assert ftc.TestClient is not RawClient and issubclass(ftc.TestClient, RawClient)
    seen = []

    async def echo(scope, receive, send):
        if scope["type"] == "websocket":
            await receive()
            seen.append(("WS", scope["client"][0], {k.decode(): v.decode() for k, v in scope["headers"]}))
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})
            return
        seen.append((scope["method"], scope["client"][0], {k.decode(): v.decode() for k, v in scope["headers"]}))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    c = ftc.TestClient(echo)
    c.get("/api/x")
    c.post("/api/x", json={})
    c.get("/")
    with c.websocket_connect("/api/terminal/stream"):
        pass
    keys = set(EDGE_API_POST) | set(EDGE_NAVIGATE) | {"x-stock-agent-intent", "host"}
    shape = lambda h: {k: v for k, v in h.items() if k in keys}  # noqa: E731
    assert [s[1] for s in seen] == ["127.0.0.1"] * 4
    assert shape(seen[0][2]) == {**EDGE_API_GET, "host": "127.0.0.1:8000"}
    assert shape(seen[1][2]) == {**EDGE_API_POST, "host": "127.0.0.1:8000"}
    assert shape(seen[2][2]) == {**EDGE_NAVIGATE, "host": "127.0.0.1:8000"}
    assert shape(seen[3][2]) == {**EDGE_WEBSOCKET, "host": "127.0.0.1:8000"}             # no Sec-Fetch-*, no Referer, no intent on a handshake
    own = ftc.TestClient(echo, headers={"x-stock-agent-intent": "paper-order"})
    own.post("/api/x", json={}, headers={"origin": "http://localhost:8000"})
    h = seen[-1][2]
    assert (h["x-stock-agent-intent"], h["origin"]) == ("paper-order", "http://localhost:8000")   # a test's own header wins, never doubled
    from api.server import app
    assert refused(RawClient(app, base_url=O, client=LOCAL).get("/api/execution/status")) == (403, "LOCAL_GATE_INTENT")   # the raw client is refused


# ---------------------------------------------------------------- the Windows runner probe and the local tools
def test_X15_the_runner_probe_and_every_local_tool_read_carry_the_intent():
    c, _ = probe()
    assert c.get("/api/execution/status", headers={"Host": "127.0.0.1", "X-Stock-Agent-Intent": "dashboard", "Connection": "close"}).status_code == 200
    assert refused(c.get("/api/execution/status", headers={"Host": "127.0.0.1", "Connection": "close"})) == (403, "LOCAL_GATE_INTENT")
    probe_line = r'Host: 127.0.0.1\r\nX-Stock-Agent-Intent: dashboard\r\nConnection: close'
    assert probe_line in (ROOT / "tests" / "test_windows_runner_60.py").read_text(encoding="utf-8")
    callers = {"scripts/stock_agent_common.ps1": 1, "scripts/start_market_morning.ps1": 1, "scripts/run_aapl_paper_smoke.py": 2}
    for rel, n in callers.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert text.count("X-Stock-Agent-Intent") == n, rel
    tools = [p for p in (ROOT / "scripts").rglob("*") if p.suffix in (".ps1", ".py") and re.search(r"Invoke-RestMethod|Invoke-WebRequest|urlopen\(", p.read_text(encoding="utf-8", errors="replace"))]
    assert sorted(p.relative_to(ROOT).as_posix() for p in tools) == sorted(callers), "a new local HTTP caller must send the intent"


# ---------------------------------------------------------------- the independent emergency shutdown (D-19)
def test_X16_the_emergency_shutdown_never_goes_through_http_and_the_safety_routes_are_not_exempt():
    stop = (ROOT / "scripts" / "stop_stock_agent.ps1").read_text(encoding="utf-8")
    assert "Stop-Process -Id $pidToStop -Force" in stop
    assert not re.search(r"Invoke-RestMethod|Invoke-WebRequest|Invoke-SAGet|/api/|curl|urlopen", stop)   # works even when the gate refuses everything
    src = (ROOT / "api" / "local_gate.py").read_text(encoding="utf-8")
    assert not re.search(r"emergency|pause|disable|resume|stop", src.split('"""', 2)[2], re.I)   # no route exemption of any kind in the code


# ---------------------------------------------------------------- the dashboard wrapper (D-7 B)
def test_X17_the_wrapper_is_the_first_code_the_dashboard_runs_and_index_html_is_unchanged():
    base = subprocess.run(["git", "show", f"{BASE}:frontend/index.html"], cwd=ROOT, capture_output=True).stdout
    assert (ROOT / "frontend" / "index.html").read_bytes().replace(b"\r\n", b"\n") == base.replace(b"\r\n", b"\n")
    html = base.decode("utf-8")
    assert re.findall(r'<script src="([^"]+)"', html)[0] == "app.js"
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    a, b = js.index("// EX-06 local intent: begin"), js.index("// EX-06 local intent: end")
    assert a < b < js.index("const REFRESH_MS = 30000;") < js.index("async function api(")
    assert "document" not in js[a:b] and "localStorage" not in js[a:b]
