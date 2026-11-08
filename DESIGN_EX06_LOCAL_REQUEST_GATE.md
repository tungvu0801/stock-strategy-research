# EX-06: the local request gate

**What.** `api/local_gate.py` is a pure ASGI middleware, added last in `api/server.py`, so it is the outermost layer. It refuses any HTTP
request or WebSocket handshake that does not come from the Stock Agent dashboard, or from a local tool, on this computer. It runs before
routing, body parsing, static files, 404/405 handling or any handler.

**Why.** Before EX-06, any website open in the operator's browser could send state-changing requests to the local app (CSRF). The
targets were Arm Paper Auto, Clear Emergency Stop, Resume and the side-effecting reads. A site could also read responses through DNS
rebinding, or open the trader-view WebSocket. Only Stage 4.6B's order routes had their own gate.

**Not authentication.** Any local process can send every header the gate checks. It defends a **local-only paper-trading** app against
browser-borne attacks: CSRF, DNS rebinding, cross-site WebSocket hijacking and clickjacking. Real-money trading, remote access, several
users or exposure through a proxy or tunnel are out of scope. They would need real authentication and HTTPS: a new decision and a new design.

## Threat model
**In scope:**
- a malicious or compromised site in the operator's browser, including through DNS rebinding;
- another local web origin (another localhost port);
- a framing page;
- a LAN device if the server were bound to `0.0.0.0`;
- accidental exposure through a proxy or tunnel;
- a compromised CDN script on the docs page.

**Out of scope:** local processes or malware, browser extensions, physical access, a compromised OS or browser.

**Preconditions:** the server binds loopback only (the runner enforces it); no proxy; a current browser.

## The rules (in order)
1. **Malformed scope:** a scope without a string path starting with `/` is refused (`ERROR`).
2. **Client:** the client must be loopback: `127.0.0.0/8`, `::1` or IPv4-mapped loopback (`CLIENT`).
3. **Host:** exactly one Host header, case-insensitive: `127.0.0.1`, `localhost` or `[::1]`, with the bound port or with no port
   (`HOST`). This is the DNS-rebinding defence.
4. **Proxy:** any of these headers is refused: `Forwarded`, `X-Forwarded-For`, `-Host`, `-Proto`, `-Port`, `-Prefix`, `X-Real-IP`,
   `Via` (`PROXY`). No proxy is supported, so a proxy fails closed.
5. **Origin:** when present, it must be exactly `http://<allowed host>:<bound port>`, and at most one; `null` is refused (`ORIGIN`).
6. **WebSocket:** a handshake needs nothing more. Browsers always send Origin.
7. **Fetch metadata:** at most one each of `Sec-Fetch-Site`, `-Mode`, `-Dest`. A cross-site or same-site request, or an unknown value, is
   refused, except a top-level navigation (navigate + document) to a page outside `/api` (`FETCH_SITE`).
8. **Pages and static files:** a GET or HEAD outside `/api` needs nothing more. It has no side effect, and framing is refused by the
   headers below.
9. **Referer:** with no Origin, a Referer, when present, must have an allowed origin (`REFERER`).
10. **`/api` reads:** every `/api` GET or HEAD, any of which may persist, needs the intent header, or strict same-origin browser evidence
    (`INTENT` otherwise): `Sec-Fetch-Site: same-origin` with exactly one `Sec-Fetch-Mode` and one `Sec-Fetch-Dest`, the Host with the
    bound port, and exactly one Referer (and any Origin) whose origin is exactly `http://<that Host>`. A typed, bookmarked or externally
    opened `/api` URL (`Sec-Fetch-Site: none`), a header-less request (curl, scripts, old browsers) or incomplete metadata is refused (H-1).
    Pages still open from a typed URL; the dashboard and every local tool send the header. The strict branch depends on the browser's
    Referer: a `Referrer-Policy: no-referrer`, a `<meta name="referrer">` on the Forward Validation page or `rel="noreferrer"` on its
    export links would make that page's reads fail closed (`INTENT`); the dashboard is unaffected. X19 pins that none exists.
11. **Every other method:** needs exactly one `X-Stock-Agent-Intent` of `dashboard` or `paper-order` (`INTENT`). With a body, it needs
    `Content-Type: application/json` (422 `CONTENT_TYPE`). Stage 4.6B still requires `paper-order` itself, behind the gate.

**How `/api` is matched.**
- Case-insensitively, and extra leading slashes are ignored: `//API/x` is an `/api` path.
- Other spellings, such as `/./api/x` or `/api;x`, are not, and they need nothing more as pages. That is safe because:
  - browsers remove dot segments before sending;
  - uvicorn decodes the path once, and the gate and the router read the same string;
  - no route matches those spellings, so they only reach the static files, which stay inside `frontend/`.

**Every allowed response the app sends through the gate** gets `X-Frame-Options: DENY` and
`Content-Security-Policy: frame-ancestors 'none'`. That covers every route, static file and 404/405.
- The one exception is the 500 of an unhandled error. Starlette's `ServerErrorMiddleware`, which sits outside every added middleware, sends it itself.
- It is a JSON body with nothing to frame.

**Other ASGI scope types:** only http, websocket and lifespan scopes exist. Any other type raises and is never handed to the app.

**A mounted or prefixed deployment** (a non-empty ASGI `root_path`, from uvicorn `--root-path` or `FastAPI(root_path=...)`) is not
supported (D-12): every request and handshake is refused with `LOCAL_GATE_ROOT_PATH`, so the readiness probe fails at once; startup is
unaffected (H-4). Without this, Starlette would route `/<root>/api/...` to the API while the gate saw a page.

**The port:** the bound port must not be 80; the default is 8000. On port 80, browsers send an Origin without a port, so state changes from the dashboard would be refused. That fails closed.

**A refusal** is 403 `{"status": "FORBIDDEN", "error": "LOCAL_GATE_<CODE>", "message"}`, with `cache-control: no-store` and the
framing headers, or a WebSocket close 1008. It logs one WARNING line: method, escaped path, code, never a header value. Any exception
inside the gate refuses (`ERROR`).

## Wiring
- **`api/server.py`:** `docs_url`, `redoc_url` and `openapi_url` are None, so no Swagger page loads CDN scripts into this origin; then
  `app.add_middleware(LocalRequestGate)`, last.
- **`frontend/app.js`:** a marked block at the top (`// EX-06 local intent: begin / end`), the first script of `index.html`. It wraps
  `window.fetch` so every same-origin request carries `X-Stock-Agent-Intent: dashboard`. It keeps an existing value (4.6B's
  `paper-order`) and never touches another origin. `index.html` is unchanged.
- **The Forward Validation page** (`forward_validation.html`, which does not load `app.js`) passes on its browser metadata, including its
  export links. Those links are downloads (`Content-Disposition: attachment`), so the browser never shows an export as a document and
  there is no export tab to reload. Verified in Edge (hardening Step 1): the clicked link and a page-initiated navigation to an export are
  allowed on the strict evidence; a reload of the page re-requests the page and its reads, nothing refused; a history navigation back to a
  typed `/api` URL is refused like the typed URL (`Sec-Fetch-Site: none`).
- **Local tools send the header:**
  - `scripts/stock_agent_common.ps1` `Invoke-SAGet` (the start and check scripts);
  - `scripts/start_market_morning.ps1` (the activity read-back);
  - `scripts/run_aapl_paper_smoke.py`;
  - the Browser Harness helper `browser_tests/flows.py`;
  - the Windows runner test probe.
- **`tests/conftest.py`:** every test's default `TestClient` is the trusted local dashboard. It sends exactly what headless Edge sends.
  The gate's own tests use Starlette's raw client.

## The independent emergency shutdown
Nothing is exempt from the gate. `scripts/stop_stock_agent.ps1` stops the bot by PID with no HTTP call. A restart never keeps Paper Auto
armed (MANUAL_ARM). Broker-side orders can be cancelled in the Alpaca paper dashboard.

## Tests
- **`tests/test_ex06_local_gate.py`** pins the behaviour:
  - every route is behind the gate;
  - browser CSRF shapes;
  - Arm / Disarm / Emergency Stop / Clear / Pause / Resume never reach a service when forged;
  - DNS rebinding;
  - the Host / Origin / Referer forms;
  - the `/api` read rule;
  - WebSocket checks;
  - IPv4 / IPv6;
  - proxies;
  - fail-closed behaviour;
  - the refusal contract;
  - 4.6B unchanged, and its own gate still refusing on its own as a second layer, including with no intent header at all (X18, H-2);
  - the test client's browser shapes;
  - the runner probe and local tools;
  - every local-capable HTTP caller in production code, classified (X19, H-3);
  - the stop script;
  - the wrapper's position.
- **`tests/frontend/ex06_local_intent.test.cjs`** pins the wrapper.
- **`tests/test_ex06_freeze.py`** pins the change set and the freeze bookkeeping (`tests/freeze_ledger.py`: BASE_EX06, NEW_EX06,
  EDITED_EX06, SEMANTIC_EX06, NEW_LEDGER_EX06, UNDO_EX06, SEMANTIC_TESTS_EX06), and (T-X21) the hardening's own change set since main
  `9c9e9f5c` with the gate's exact diff.

## Not in EX-06
- a server-side confirmation for Arm / Clear / Resume (D-6: designed separately, pending UX and safety review);
- log rate-limiting (D-22): a hostile tab can grow the stdout log with refusals, which is rotated only at start;
- the Robinhood gateway (`127.0.0.1:8787`);
- **script injection on the dashboard origin.**
  - Any script running there passes the gate by design: it is same-origin and carries the intent.
  - Some dashboard views insert external text (news, events, LLM narratives) as HTML without escaping. This predates EX-06.
  - An XSS audit of those views is a prerequisite before Paper Auto.
