"""
EX-06: the local request gate (the design and threat model: DESIGN_EX06_LOCAL_REQUEST_GATE.md).

A pure ASGI middleware, installed outermost (D-15 a), that refuses any HTTP request or WebSocket handshake that does not come from the Stock
Agent dashboard (or a local tool) on this computer, BEFORE routing, body parsing, static files, 404 / 405 handling or any handler runs:

  1. the client is a loopback address (D-18 a);
  2. exactly one Host header: 127.0.0.1, localhost or [::1], with the bound port or no port (D-1 b'); never 0.0.0.0, a LAN name or a
     rebinding name (DNS rebinding);
  3. no forwarding header: no proxy, tunnel or port forward is supported, so one fails closed (D-5 a, D-12 a);
  4. an Origin, when present, is exactly http://<allowed host>:<bound port> ("null" is refused);
  5. a WebSocket handshake needs nothing more (D-13 a: browsers always send Origin on a WebSocket);
  6. fetch metadata (a page cannot set Sec-Fetch-*): a cross-site or same-site request is refused, except a top-level navigation to a page
     outside /api (a link or a bookmark to the dashboard) (D-2 a); an iframe, image or script load from another site is refused;
  7. a page or static file (GET / HEAD outside /api) needs nothing more (no side effect; framing is refused by the headers below);
  8. with no Origin, a Referer, when present, must have an allowed origin (D-3 a for state-changing requests; D-14 for /api reads);
  9. every /api GET / HEAD (D-14: any of them may persist) needs the dashboard intent header, or strict same-origin browser evidence (H-1):
     Sec-Fetch-Site same-origin with exactly one Sec-Fetch-Mode and one Sec-Fetch-Dest, the Host with the bound port, and exactly one Referer
     (and any Origin) whose origin is exactly http://<that Host>; a typed, bookmarked or externally opened /api URL (Sec-Fetch-Site none), a
     header-less request (curl, scripts, old browsers) or incomplete metadata is refused;
 10. every other method needs exactly one X-Stock-Agent-Intent in {dashboard, paper-order} (D-4 a) and, with a body, a JSON content type.

Every allowed HTTP response the app sends through the gate (every route, static file, 404 / 405) carries X-Frame-Options: DENY and CSP
frame-ancestors 'none' (D-17); the one exception is the 500 of an unhandled error, which Starlette's ServerErrorMiddleware (outside every
added middleware) sends itself, a JSON body. A refusal is 403 {"status": "FORBIDDEN", "error": "LOCAL_GATE_<CODE>", "message"} (422
INVALID_REQUEST / LOCAL_GATE_CONTENT_TYPE for a non-JSON body), a WebSocket close 1008, and one WARNING log line (method, escaped path, code;
never a header value). Any error inside the gate refuses (LOCAL_GATE_ERROR); an ASGI scope type other than http, websocket or lifespan is
never handed to the app. A non-empty ASGI root_path (a mounted or prefixed deployment: unsupported, D-12) refuses every request and handshake
(LOCAL_GATE_ROOT_PATH, H-4); lifespan is unaffected.

This is NOT authentication (D-10 A): any local process can send every one of these headers. It defends a local-only paper-trading app
against browser CSRF, DNS rebinding, cross-site WebSocket hijacking and clickjacking. Stage 4.6B's own order gate stays unchanged behind it.
"""
from __future__ import annotations

import ipaddress
import json
import logging
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD"})
INTENT_HEADER = "X-Stock-Agent-Intent"
INTENTS = frozenset({"dashboard", "paper-order"})                                       # D-4 (a); 4.6B still requires paper-order itself
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})                          # D-1 (b')
FORWARDING = frozenset({b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto", b"x-forwarded-port", b"x-forwarded-prefix",
                        b"x-real-ip", b"via"})                                            # D-5 (a)
SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})                                   # browser-asserted: this origin, or typed / bookmarked
OTHER_SITES = frozenset({"cross-site", "same-site"})
FRAME_HEADERS = ((b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'"))   # D-17 (a)
MESSAGE = "Refused: this action must come from the Stock Agent dashboard on this computer."
CODES = ("CLIENT", "HOST", "PROXY", "ORIGIN", "REFERER", "FETCH_SITE", "INTENT", "CONTENT_TYPE", "ERROR", "ROOT_PATH")


def allowed_hosts(port: int) -> set:
    return {f"{n}:{port}" for n in LOOPBACK_NAMES}


def allowed_origins(port: int) -> set:
    return {f"http://{h}" for h in allowed_hosts(port)}


def is_api(path: str) -> bool:
    """/api and everything under it, case-insensitively and with extra leading slashes ignored (//API/x is an API path). Other spellings
    (/./api/x, /api;x) are not: browsers remove dot segments before sending, no route matches them, and they reach only the static files."""
    p = "/" + str(path or "").lstrip("/").lower()
    return p == "/api" or p.startswith("/api/")


def _loopback_client(host) -> bool:
    try:
        ip = ipaddress.ip_address(str(host))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _values(headers: List[Tuple[bytes, bytes]], name: bytes) -> List[str]:
    return [v.decode("latin-1").strip() for k, v in headers if k.lower() == name]


def _refuse(code: str, status: int = 403) -> Tuple[int, str, str]:
    return (status, "FORBIDDEN" if status == 403 else "INVALID_REQUEST", "LOCAL_GATE_" + code)


def _same_origin_read(host: str, port: int, site, modes: List[str], dests: List[str], origin: List[str], headers) -> bool:
    """H-1: an /api read without the intent header passes only on complete browser fetch metadata asserting THIS origin: Sec-Fetch-Site
    same-origin (never none: a typed, bookmarked or externally opened URL), exactly one Sec-Fetch-Mode and one Sec-Fetch-Dest, the Host with
    the bound port (no portless name), and exactly one Referer (and any Origin) whose origin is exactly http://<that Host>."""
    if site != "same-origin" or len(modes) != 1 or len(dests) != 1 or host not in allowed_hosts(port):
        return False
    ref = _values(headers, b"referer")
    if len(ref) != 1:
        return False
    p = urlsplit(ref[0])
    own = "http://" + host
    return f"{p.scheme}://{p.netloc}".lower() == own and (not origin or origin[0].lower() == own)


def refusal(scope) -> Optional[Tuple[int, str, str]]:
    """(HTTP status, status text, error code) when the request is refused, else None. Pure: reads the ASGI scope only, never the body."""
    headers = list(scope.get("headers") or [])
    path = scope.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        return _refuse("ERROR")                                                         # a malformed scope: fail closed
    if scope.get("root_path") not in (None, ""):
        return _refuse("ROOT_PATH")                                                     # H-4: a mounted / prefixed deployment is unsupported (D-12)
    if not _loopback_client((scope.get("client") or ("", 0))[0]):
        return _refuse("CLIENT")
    port = (scope.get("server") or (None, None))[1]
    if not isinstance(port, int) or port <= 0:
        return _refuse("HOST")
    origins = allowed_origins(port)
    host = _values(headers, b"host")
    if len(host) != 1 or host[0].lower() not in allowed_hosts(port) | LOOPBACK_NAMES:
        return _refuse("HOST")
    if any(k.lower() in FORWARDING for k, _ in headers):
        return _refuse("PROXY")
    origin = _values(headers, b"origin")
    if len(origin) > 1 or (origin and origin[0].lower() not in origins):
        return _refuse("ORIGIN")
    if scope.get("type") == "websocket":
        return None
    method = str(scope.get("method") or "").upper()
    sites, modes, dests = _values(headers, b"sec-fetch-site"), _values(headers, b"sec-fetch-mode"), _values(headers, b"sec-fetch-dest")
    if len(sites) > 1 or len(modes) > 1 or len(dests) > 1:
        return _refuse("FETCH_SITE")
    site = sites[0].lower() if sites else None
    if site is not None and site not in SAME_ORIGIN_SITES:
        navigation = method in SAFE_METHODS and not is_api(path) and modes == ["navigate"] and dests == ["document"]
        if site not in OTHER_SITES or not navigation:
            return _refuse("FETCH_SITE")
    if method in SAFE_METHODS and not is_api(path):
        return None                                                                     # a page or a static file: no side effect
    if not origin:                                                                      # D-3 (a) / D-14: state-changing requests and /api reads
        ref = _values(headers, b"referer")
        if len(ref) > 1:
            return _refuse("REFERER")
        if ref:
            p = urlsplit(ref[0])
            if f"{p.scheme}://{p.netloc}".lower() not in origins:
                return _refuse("REFERER")
    intent = _values(headers, INTENT_HEADER.lower().encode())
    if len(intent) > 1 or (intent and intent[0] not in INTENTS):
        return _refuse("INTENT")
    if method in SAFE_METHODS:                                                          # an /api read (D-14: it may persist)
        return None if (intent or _same_origin_read(host[0].lower(), port, site, modes, dests, origin, headers)) else _refuse("INTENT")
    if not intent:
        return _refuse("INTENT")
    length = (_values(headers, b"content-length") or [""])[0]
    if _values(headers, b"transfer-encoding") or length not in ("", "0"):
        ctype = (_values(headers, b"content-type") or [""])[0]
        if ctype.split(";")[0].strip().lower() != "application/json":
            return _refuse("CONTENT_TYPE", 422)
    return None


class LocalRequestGate:
    """The outermost ASGI middleware (added LAST in api/server.py). Not authentication: see the module docstring."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "lifespan":
            return await self.app(scope, receive, send)                                  # startup / shutdown: no request
        if kind not in ("http", "websocket"):
            raise RuntimeError(f"local gate: unsupported ASGI scope type {kind!r}")     # fail closed: never handed to the app
        try:
            refused = refusal(scope)
        except Exception:  # noqa: BLE001 - fail closed
            refused = _refuse("ERROR")
        if refused is None:
            if kind == "websocket":
                return await self.app(scope, receive, send)

            async def framed(message):
                if message.get("type") == "http.response.start":
                    message = {**message, "headers": [*(message.get("headers") or []), *FRAME_HEADERS]}
                await send(message)
            return await self.app(scope, receive, framed)
        status, text, code = refused
        log.warning("local gate refused %s %r (%s)", scope.get("method", "WEBSOCKET"), str(scope.get("path") or "")[:200], code)
        if kind == "websocket":
            await receive()                                                              # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"status": text, "error": code, "message": MESSAGE}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"), *FRAME_HEADERS]})
        await send({"type": "http.response.body", "body": body})
