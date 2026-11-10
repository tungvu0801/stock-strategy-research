"""
browser_tests/risk_controls_panel_flows.py — the risk controls panel's browser checks (RISK_CONTROLS_PANEL_DESIGN.md section 6, B1 to B41).

Every check runs in a SECOND headless Edge with its own profile and debugging port (`Side`), against the harness's real server and real
persisted gate on its scratch world. Requests are held, failed, aborted or answered differently only through CDP Fetch interception, and
scripts are made to fail only by aborting their load or by a script injected before the page's own; nothing inside the page is mocked. The
scripts these checks abort and the errors they cause stay in the second browser, so the main page's 0-JS-error / 0-console-error checks are
untouched. Visible text is read with innerText in the default collapsed layout (the panel itself is hidden there: D10). Each flow restores
the gate to Trading allowed and never triggers a daily-loss halt (the risk_controls flow that follows counts exactly one).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

BANNER = "Emergency Stop unavailable on this page. Page scripts did not load. Use the fallback command."
PARTIAL = "Part of this page did not load. Emergency Stop still works from this bar."
UNCERTAIN = "Could not confirm the current state."
NOT_CONFIRMED = "Emergency Stop NOT confirmed. Press it again."
CSP_WORDS = ("Content Security Policy", "Content-Security-Policy")


def _pkey(p):
    """A paused request's identity: its id AND its stage (Edge pauses the same request id again at the response stage)."""
    return p["requestId"], ("response" if "responseStatusCode" in p or "responseErrorReason" in p else "request")


class Side:
    """A second headless Edge for the risk controls checks (see the module docstring)."""

    def __init__(self, c, width=1600, height=900):
        from browser_tests import app_server as AS
        from browser_tests.cdp import Browser
        self.c, self.port = c, AS.free_port()
        c.guard.allowed_ports.add(self.port)
        self.dir = Path(tempfile.mkdtemp(prefix="stock-agent-panel-"))
        self.b = Browser(self.port, self.dir / "edge-profile", width, height)
        self.b.launch()
        self.b.send("Network.setCacheDisabled", {"cacheDisabled": True})              # a cached script never reaches Fetch: every abort must see the load
        self.rules, self.held, self.done, self.seen, self.scripts = [], [], set(), 0, []
        self.trail, self.meta = [], {}                                               # (request, CDP call, error), shown when a wait times out
        self.abort, self.plain = (), False

    def close(self):
        try:
            try:                                                                         # leave the page first: its sockets close before the process ends
                self.b.send("Fetch.disable")
                self.b.navigate("about:blank")
                self.wait("location.href === 'about:blank'", 10, "blank page")
            except Exception:  # noqa: BLE001 - closing regardless
                pass
            # end the whole Edge process tree: after many navigations and aborted loads a child process outlived the main one and kept server
            # connections open, so the harness server could not stop (measured: server_stopped False without this, True with it)
            if os.name == "nt" and self.b.proc is not None and self.b.proc.poll() is None:
                try:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.b.proc.pid)], capture_output=True, check=False, timeout=30)   # a fixed argument list
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.b.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    # ---- request interception ---------------------------------------------------------------------------------------
    def intercept(self, abort=(), plain=False, api=False, respond=False):
        """Fetch.enable with exactly the patterns needed: script loads to abort, every .js answered as text/plain (B25), the safety API at
        the request stage (`api`) and at the response stage (`respond`)."""
        self.abort, self.plain = tuple(abort), plain
        pats = [{"urlPattern": f"*{a}*", "requestStage": "Request"} for a in abort]
        if plain:
            pats.append({"urlPattern": "*.js*", "requestStage": "Response"})
        if api:
            pats.append({"urlPattern": "*/api/*", "requestStage": "Request"})
        if respond:
            pats.append({"urlPattern": "*/api/execution-safety*", "requestStage": "Response"})
        if pats:
            self.b.send("Fetch.enable", {"patterns": pats})
        else:
            self.b.send("Fetch.disable")

    def pump(self, seconds=0.0):
        end = time.time() + seconds
        while True:
            self.b.send("Page.getFrameTree")
            evs = self.b.events
            while self.seen < len(evs):
                e = evs[self.seen]
                self.seen += 1
                if e.get("method") == "Fetch.requestPaused" and _pkey(e["params"]) not in self.done:
                    self._decide(e["params"])
            if time.time() >= end:
                return
            time.sleep(0.05)

    def _decide(self, p):
        rid, req = p["requestId"], p["request"]
        url, method, stage = req["url"], req.get("method", "GET"), ("response" if "responseStatusCode" in p or "responseErrorReason" in p else "request")
        self.meta[rid] = (method, url.split("?")[0][-40:], stage)
        key = (rid, stage)                                                               # Edge keeps the request id at the response stage
        if stage == "request" and any(a in url for a in self.abort):
            return self._send(key, "Fetch.failRequest", {"errorReason": "Aborted"})
        if stage == "response" and self.plain and url.split("?")[0].endswith(".js"):
            body = self.b.send("Fetch.getResponseBody", {"requestId": rid})
            data = body["body"] if body.get("base64Encoded") else base64.b64encode(body["body"].encode("utf-8")).decode()
            hdrs = [h for h in (p.get("responseHeaders") or []) if h["name"].lower() != "content-type"] + [{"name": "Content-Type", "value": "text/plain"}]
            return self._send(key, "Fetch.fulfillRequest", {"responseCode": p.get("responseStatusCode", 200), "responseHeaders": hdrs, "body": data})
        for rule in self.rules:
            act = rule(method, url, stage, p)
            if act is None:
                continue
            if act == "hold":
                self.held.append({"id": rid, "method": method, "url": url, "stage": stage})
                self.done.add(key)
                return None
            if act == "fail":
                return self._send(key, "Fetch.failRequest", {"errorReason": "Failed"})
            if isinstance(act, tuple) and act[0] == "fulfill":
                return self._send(key, "Fetch.fulfillRequest", {"responseCode": act[1], "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
                                                                "body": base64.b64encode(act[2].encode("utf-8")).decode()})
            break
        return self._send(key, "Fetch.continueRequest", {})

    def _send(self, key, method, params):
        rid = key[0]
        self.done.add(key)
        try:
            self.b.send(method, {"requestId": rid, **params})
            self.trail.append((self.meta.get(rid), method[6:], None))
        except RuntimeError as exc:                                                  # the page cancelled it meanwhile (an aborted fetch)
            self.trail.append((self.meta.get(rid), method[6:], str(exc)[:80]))

    def release(self, pred=None, how="continue"):
        keep = []
        for h in self.held:
            if pred and not pred(h):
                keep.append(h)
                continue
            try:
                self.b.send("Fetch.failRequest" if how == "fail" else "Fetch.continueRequest", {"requestId": h["id"], **({"errorReason": "Failed"} if how == "fail" else {})})
            except RuntimeError:
                pass
        self.held = keep
        self.pump(0.2)

    def posts(self, path):
        """Every POST the page sent to `path` (from Network.requestWillBeSent), with its headers."""
        return [e["params"]["request"] for e in self.b.events if e.get("method") == "Network.requestWillBeSent"
                and e["params"]["request"].get("method") == "POST" and e["params"]["request"]["url"].endswith(path)]

    # ---- the page ---------------------------------------------------------------------------------------------------
    def inject(self, source):
        self.scripts.append(self.b.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})["identifier"])

    def uninject(self):
        for i in self.scripts:
            self.b.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": i})
        self.scripts = []

    def js(self, expr):
        """Evaluate without awaiting a returned promise: an awaited RiskControls.refresh() would block this process while its own request is paused,
        and nothing would decide the pause until the page's 20 s abort."""
        try:
            r = self.b.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": False})
        except Exception:  # noqa: BLE001 - a navigation in progress
            return None
        return None if r.get("exceptionDetails") else r["result"].get("value")

    def js_await(self, expr):
        """Evaluate and await a promise: only where no request of this page is intercepted (B15)."""
        try:
            return self.b.js(expr)
        except Exception:  # noqa: BLE001 - a navigation in progress
            return None

    def wait(self, expr, timeout=30, what=""):
        end = time.time() + timeout
        while time.time() < end:
            self.pump()
            if self.js(expr):
                return True
            time.sleep(0.1)
        paused = [e["params"]["request"]["url"].split("?")[0][-60:] for e in self.b.events if e.get("method") == "Fetch.requestPaused" and _pkey(e["params"]) not in self.done]
        status = self.js("JSON.stringify(window.RiskControls && RiskControls.status ? [RiskControls.state.data && RiskControls.state.data.state, RiskControls.status()] : null)")
        safety = [t for t in self.trail if t[0] and "execution-safety" in t[0][1]]
        net = [(e["method"].split(".")[1], (e["params"].get("request") or e["params"].get("response") or {}).get("url", "")[-40:], e["params"].get("errorText"))
               for e in self.b.events if e.get("method") in ("Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFailed")
               and "execution-safety" in json.dumps(e["params"].get("request") or e["params"].get("response") or {})][-8:]
        (Path(self.c.out) / f"side_timeout_{int(time.time())}.json").write_text(json.dumps({"what": what or expr, "status": status, "paused": paused, "trail": self.trail,
                                                                                            "network": net}, indent=1, default=str), encoding="utf-8")
        raise TimeoutError(f"side browser: timed out waiting for {what or expr} | panel {str(status)[:300]} | paused {paused[:4]} | safety calls {safety[-4:]} | net {net[-4:]}")

    def open(self, path="/", mounted=True):
        self.held, self.done = [], set()
        self.b.navigate("about:blank")                                                   # always a new document: "/#a" -> "/#b" alone is a same-page hash change
        self.wait("location.href === 'about:blank'", 15, "blank page")
        self.b.navigate(self.c.base + path)
        self.wait("document.readyState === 'complete'", 60, f"load {path}")
        if mounted:
            self.wait("!!document.getElementById('pui-status') && document.getElementById('pui-status').innerText.length > 0", 30, "product shell")

    def text(self, sel):
        return self.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); return e ? e.innerText : null; }})()") or ""

    def visible(self, sel):
        return bool(self.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return false; const b = e.getBoundingClientRect(); "
                            "const s = getComputedStyle(e); return b.width > 0 && b.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; })()"))

    def press(self, sel, hold=0.0, release=True):
        """A real pointer press at the element's on-screen centre (B24, B28): a hidden element has no box, a covered one is not under the point."""
        r = self.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; e.scrollIntoView({{block: 'center'}}); "
                    "const b = e.getBoundingClientRect(); const x = b.left + b.width / 2, y = b.top + b.height / 2; const at = document.elementFromPoint(x, y); "
                    f"return [x, y, b.width, b.height, !!at && !!at.closest({json.dumps(sel)})]; }})()")
        if not r or r[2] <= 0 or r[3] <= 0 or not r[4]:
            return False
        x, y = r[0], r[1]
        self.b.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.b.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
        if hold:
            self.pump(hold)
        if release:
            self.b.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1})
        self.pump(0.1)
        return True

    def csp_violations(self):
        return [e["params"]["entry"].get("text", "") for e in self.b.events if e.get("method") == "Log.entryAdded"
                and any(w in e["params"]["entry"].get("text", "") for w in CSP_WORDS)]


# ---- the real gate ---------------------------------------------------------------------------------------------------------------------
def _svc():
    from api.routes.trader_view import stream
    return stream().safety


def _state(c):
    return c.http("GET", "/api/execution-safety")


def _reset(c, what):
    v = _state(c)
    if (v.get("kill_switch") or {}).get("active"):
        c.http("POST", "/api/execution-safety/clear-emergency-stop", {})
    if (v.get("no_new_entries") or {}).get("active"):
        c.http("POST", "/api/execution-safety/resume-new-entries", {})
    v = _state(c)
    c.r.eq(f"{what}: the gate is back to Trading allowed", v["state"], "TRADING_ALLOWED")


def _kill(c):
    return bool((_state(c).get("kill_switch") or {}).get("active"))


class _Patch:
    """In-process instance attribute patches on the harness's real safety service, always restored."""

    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.old = obj, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.old[k] = self.obj.__dict__.get(k, _MISSING)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is _MISSING:
                delattr(self.obj, k)
            else:
                setattr(self.obj, k, v)


_MISSING = object()


def _raise(*a, **k):
    import sqlite3
    raise sqlite3.OperationalError("injected by the browser harness")


def _bar(s):
    return s.text("#pui-status")


def _card(s):
    return s.text('#pui-cards [data-pui-card="risk"]')


def _confirm_bar_stop(s):
    s.wait("!!document.querySelector('#pui-status [data-pui=\"stop-bar\"]')", 20, "the status bar's Emergency Stop")
    pressed = s.press('#pui-status [data-pui="stop-bar"]')
    s.wait("!!document.querySelector('#pui-status [data-pui-confirm=\"stop-bar\"]')", 10, "the bar's confirmation")
    shown = "Emergency Stop?" in _bar(s) and s.visible('#pui-status [data-pui-confirm="stop-bar"]')
    confirmed = s.press('#pui-status [data-pui="stop-bar-confirm"]')
    return pressed, shown, confirmed


# =========================================================================================================================================
def risk_controls_scripts(c):
    """B16, B20 to B27, B29, B34, B35, B37, B38 to B41: page scripts or styles that fail, the static notice, the status bar's banner and its
    "part of this page" line, the bar's Emergency Stop pressed with a real pointer, and the F2 fallback commands through the real gate."""
    s = Side(c)
    try:
        # B16 + baseline: a healthy page removes the notice, shows the bar's Stop, no banner, no line, 0 CSP violations
        s.intercept()
        s.open("/")
        s.wait("!!window.RiskControls && !!RiskControls.state.data", 30, "risk controls loaded")
        s.pump(2.5)
        c.r.check("healthy page: the static notice is removed, the bar has its Emergency Stop, no banner, no 'part of this page' line",
                  s.js("!document.getElementById('pui-boot-notice')") is True and s.visible('#pui-status [data-pui="stop-bar"]')
                  and BANNER not in _bar(s) and PARTIAL not in _bar(s), _bar(s)[:300])
        # B38: View details open, the Research action pressed: no line, no banner; Stop still works from the bar
        s.js("ProductUI.expandDetails(); document.querySelector('#tv-actions [data-action=\"research\"]') && document.querySelector('#tv-actions [data-action=\"research\"]').click()")
        s.pump(2.5)
        c.r.check("B38 healthy page, details open, Research pressed: no 'part of this page' line and no banner", PARTIAL not in _bar(s) and BANNER not in _bar(s), _bar(s)[:300])
        s.js("ProductUI.collapseDetails(); document.querySelector('.tab-btn[data-tab=\"trader\"]').click()")
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status') && document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "bar outcome")
        c.r.check("B38 the bar's Stop (real pointer): confirmation visible in the bar, the stop saved, 'Emergency Stop active' shown", pressed and shown and confirmed and _kill(c))
        c.r.check("B16 a healthy page logs 0 CSP violations", not s.csp_violations(), s.csp_violations()[:3])
        _reset(c, "B38")

        for name, files in (("B20 trader_render.js", ("/trader_render.js",)), ("B21 trader_core.js", ("/trader_core.js",)), ("B22 risk_controls.js", ("/risk_controls.js",)),
                            ("B35 trader_view.js", ("/trader_view.js",)), ("B39 app.js and trader_render.js", ("/app.js", "/trader_render.js"))):
            s.intercept(abort=files)
            s.open("/")
            s.wait(f"document.getElementById('pui-status').innerText.includes({json.dumps(BANNER)})", 15, f"{name}: banner")
            bar = _bar(s)
            c.r.check(f"{name} aborted: the bar shows the banner with both fallback commands and this page's port, with no press, and nothing else",
                      BANNER in bar and f"http://127.0.0.1:{c.base.rsplit(':', 1)[1]}/api/execution-safety/emergency-stop" in bar and "ConvertFrom-Json" in bar
                      and PARTIAL not in bar and not s.js("!!document.querySelector('#pui-status [data-pui=\"stop-bar\"]')"), bar[:400])
            c.r.eq(f"{name} aborted: the server is unchanged", _state(c)["state"], "TRADING_ALLOWED")

        # B35 second half: paper_bot.js aborted leaves Emergency Stop working
        s.intercept(abort=("/paper_bot.js",))
        s.open("/")
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "B35 outcome")
        c.r.check("B35 paper_bot.js aborted: no banner, and the bar's Stop saves the stop", BANNER not in _bar(s) and pressed and shown and confirmed and _kill(c))
        _reset(c, "B35")

        # B23 product_ui.js aborted: the static notice after 3 s, the old tabs still there
        s.intercept(abort=("/product_ui.js",))
        s.open("/", mounted=False)
        s.pump(3.5)
        c.r.check("B23 product_ui.js aborted: the static notice is visible after 3 s, with the command; the legacy tabs still show",
                  s.visible("#pui-boot-notice") and "curl.exe -sS --max-time 60 -X POST http://127.0.0.1:8000/api/execution-safety/emergency-stop" in s.text("#pui-boot-notice")
                  and s.visible("nav.tabs"))
        # B25 every .js answered as text/plain under nosniff: no script runs; the notice shows
        s.intercept(plain=True)
        s.open("/", mounted=False)
        s.pump(3.5)
        c.r.check("B25 every .js served as text/plain (nosniff): the static notice is visible with the command",
                  s.visible("#pui-boot-notice") and "If this message stays visible" in s.text("#pui-boot-notice") and s.js("typeof window.ProductUI") == "undefined")
        # B26 product_ui.css aborted: mount() removes the notice; Stop still works
        s.intercept(abort=("/product_ui.css",))
        s.open("/")
        s.pump(2.5)
        c.r.check("B26 product_ui.css aborted: mount() removed the notice and the bar keeps its Stop",
                  s.js("!document.getElementById('pui-boot-notice')") is True and s.js("!!document.querySelector('#pui-status [data-pui=\"stop-bar\"]')") is True)

        # B24 app.js aborted: the line before any press, then the bar's Stop with a real pointer; its POST carries the intent header itself
        s.intercept(abort=("/app.js",))
        s.open("/")
        s.wait(f"document.getElementById('pui-status').innerText.includes({json.dumps(PARTIAL)})", 15, "B24 line")
        c.r.check("B24 app.js aborted: 'part of this page did not load' shows after the first go(), before any press, with no banner", BANNER not in _bar(s), _bar(s)[:300])
        before = len(s.posts("/api/execution-safety/emergency-stop"))
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "B24 outcome")
        sent = s.posts("/api/execution-safety/emergency-stop")[before:]
        c.r.check("B24 the bar's confirmation is visible text, pressed with a real pointer; the POST carries X-Stock-Agent-Intent and saves the stop; the outcome shows in the bar",
                  pressed and shown and confirmed and len(sent) >= 1 and all((r.get("headers") or {}).get("X-Stock-Agent-Intent") == "dashboard" for r in sent) and _kill(c),
                  [r.get("headers") for r in sent])
        _reset(c, "B24")

        # B40 app.js aborted + deep link #positions: the line after the first go(), no press; the bar's Stop works
        s.intercept(abort=("/app.js",))
        s.open("/#positions")
        s.wait(f"document.getElementById('pui-status').innerText.includes({json.dumps(PARTIAL)})", 15, "B40 line")
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "B40 outcome")
        c.r.check("B40 app.js aborted, deep link #positions: the line shows with no press, and the bar's Stop saves the stop", pressed and shown and confirmed and _kill(c))
        _reset(c, "B40")

        # B41 app.js aborted + a foreign hash nothing consumes: no line at mount; the line after the bar's press; Stop works; no banner
        s.intercept(abort=("/app.js",))
        s.open("/#not-a-section")
        s.pump(2.5)
        c.r.check("B41 app.js aborted, a foreign hash (#not-a-section): no line at mount", PARTIAL not in _bar(s) and BANNER not in _bar(s), _bar(s)[:300])
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "B41 outcome")
        c.r.check("B41 after the bar's press the line shows, the stop is saved, no banner", PARTIAL in _bar(s) and BANNER not in _bar(s) and pressed and shown and confirmed and _kill(c),
                  _bar(s)[:300])
        _reset(c, "B41")
        # B41b #daily-brief is consumed by brief_delivery.js before mount() (it clears the hash), so mount() opens TRADE: the line shows at once, no press
        s.intercept(abort=("/app.js",))
        s.open("/#daily-brief")
        s.wait(f"document.getElementById('pui-status').innerText.includes({json.dumps(PARTIAL)})", 15, "B41b line")
        c.r.check("B41b app.js aborted, #daily-brief (cleared by the brief module before mount): the line shows with no press, no banner, the bar keeps its Stop",
                  BANNER not in _bar(s) and s.js("!!document.querySelector('#pui-status [data-pui=\"stop-bar\"]')") is True, _bar(s)[:300])
        c.r.eq("B41b: the server is unchanged", _state(c)["state"], "TRADING_ALLOWED")

        # B29 a pre-boot deep link (no visit to TRADE), then the bar's Stop
        s.intercept()
        s.open("/#activity")
        c.r.check("B29 a deep link to ACTIVITY has not booted the Trader View yet", s.js("!document.querySelector('#tv-safety [data-safety=\"stop\"]')") is True)
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "B29 outcome")
        c.r.check("B29 the bar's Stop from a pre-boot deep link boots the panel, confirms in the bar and saves the stop", pressed and shown and confirmed and _kill(c))
        _reset(c, "B29")

        # B27 the chart throws inside boot(): the panel is mounted already; Stop works
        s.inject("Object.defineProperty(window, 'LightweightCharts', { configurable: true, get() { return { createChart() { throw new Error('injected by the harness (B27)'); } }; }, set() {} });")
        s.intercept()
        s.open("/")
        s.wait("!!document.querySelector('#tv-safety [data-safety=\"stop\"]')", 20, "B27 panel")
        pressed, shown, confirmed = _confirm_bar_stop(s)
        s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, "B27 outcome")
        c.r.check("B27 setupChart throws: Risk Controls is mounted and the bar's Stop saves the stop", pressed and shown and confirmed and _kill(c))
        s.uninject()
        _reset(c, "B27")

        # B34 renderCards throws once during mount(): the notice stays, the tick runs, the bar renders, the cards render later
        s.inject("(() => { const q = Document.prototype.querySelector; let once = false; Document.prototype.querySelector = function (sel) { "
                 "if (!once && String(sel).includes('[data-ppe=\"stretch\"]')) { once = true; throw new Error('injected by the harness (B34)'); } return q.call(this, sel); }; })();")
        s.intercept()
        s.open("/")
        s.pump(4.0)
        c.r.check("B34 renderCards threw once in mount(): the static notice stays (visible), the bar renders its Stop, the cards render on a later tick",
                  s.visible("#pui-boot-notice") and s.js("!!document.querySelector('#pui-status [data-pui=\"stop-bar\"]')") is True
                  and s.js("!!document.querySelector('#pui-cards [data-pui-card=\"risk\"]')") is True)
        s.uninject()

        # B37 the F2 commands (README, section 8) through the real gate of this scratch instance
        port = c.base.rsplit(":", 1)[1]
        post = ["curl.exe", "-sS", "--max-time", "60", "-X", "POST", f"http://127.0.0.1:{port}/api/execution-safety/emergency-stop", "-H", "X-Stock-Agent-Intent: dashboard",
                "-H", "Content-Type: application/json", "-d", "{}"]
        out = subprocess.run(post, capture_output=True, text=True, timeout=90, check=False)            # a fixed argument list, no shell
        read = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               f'(curl.exe -sS --max-time 60 http://127.0.0.1:{port}/api/execution-safety -H "X-Stock-Agent-Intent: dashboard" | ConvertFrom-Json).kill_switch.active'],
                              capture_output=True, text=True, timeout=120, check=False)
        c.r.check("B37 the F2 POST passes the real gate and saves the latch; the F2 read prints True",
                  out.returncode == 0 and '"kill_switch"' in out.stdout and _kill(c) and read.stdout.strip() == "True", [out.returncode, out.stdout[:120], read.stdout.strip(), read.stderr[:120]])
        _reset(c, "B37")
    finally:
        s.close()
        _reset(c, "risk_controls_scripts")


# =========================================================================================================================================
def risk_controls_races(c):
    """B1 to B12, B15, B17 to B19, B30, B31: the order of polls and actions on the real gate, answers held, failed or replaced through CDP Fetch
    interception, and server-side failures injected into the harness's real safety service (always restored)."""
    s = Side(c)
    svc = _svc()
    try:
        s.intercept(api=True, respond=True)
        s.open("/")
        s.wait("!!window.RiskControls && !!RiskControls.state.data && RiskControls.state.data.state === 'TRADING_ALLOWED'", 30, "allowed")
        rc = "RiskControls"
        GET = lambda m, u, st, p: m == "GET" and u.split("?")[0].endswith("/api/execution-safety")  # noqa: E731

        def poll_held_at_response():
            s.rules = [lambda m, u, st, p: "hold" if GET(m, u, st, p) and st == "response" and not s.held else None]
            s.js(f"{rc}.refresh()")
            s.wait("true", 1)
            s.pump(0.5)
            return bool(s.held)

        # B1: a poll issued before Pause, its answer held until after the Pause and its settling read
        ok = poll_held_at_response()
        s.rules = []
        s.js("document.querySelector('#tv-safety [data-safety=\"pause\"]').click()")
        s.wait(f"{rc}.status().outcomes.pause && {rc}.status().outcomes.pause.result === 'CONFIRMED'", 20, "B1 pause confirmed")
        s.release()
        s.pump(1.0)
        c.r.check("B1 a poll issued before Pause and answered after it never overwrites the pause", ok and s.js(f"{rc}.state.data.state") == "NO_NEW_ENTRIES")
        _reset(c, "B1")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B2: the Pause POST held at the request stage; a poll issued during it, answered (stale) after it
        s.rules = [lambda m, u, st, p: "hold" if m == "POST" and u.endswith("/pause-new-entries") and st == "request" and not s.held else None]
        s.js("document.querySelector('#tv-safety [data-safety=\"pause\"]').click()")
        s.pump(0.5)
        s.rules = [lambda m, u, st, p: "hold" if GET(m, u, st, p) and st == "response" and len(s.held) < 2 else None]
        s.js(f"{rc}.refresh()"); s.pump(0.8)
        s.rules = []
        s.release(lambda h: h["method"] == "POST")                                   # the Pause reaches the server
        s.wait(f"{rc}.status().outcomes.pause && {rc}.status().outcomes.pause.result === 'CONFIRMED'", 20, "B2 pause confirmed")
        s.release()                                                                    # the stale poll lands last
        s.pump(1.0)
        c.r.check("B2 a poll issued during the Pause POST and answered after it never overwrites the pause", s.js(f"{rc}.state.data.state") == "NO_NEW_ENTRIES")
        _reset(c, "B2")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B3: two overlapping polls, the older (allowed) answered last
        poll_held_at_response()
        s.rules = []
        c.http("POST", "/api/execution-safety/pause-new-entries", {})
        s.js(f"{rc}.refresh()"); s.pump(1.0)
        s.release()
        s.pump(1.0)
        c.r.check("B3 two overlapping polls: the older answered last never replaces the newer", s.js(f"{rc}.state.data.state") == "NO_NEW_ENTRIES")
        _reset(c, "B3")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B5: Pause answered late (held at the response stage), Emergency Stop pressed meanwhile: its POST is sent at once; the late Pause answer loses
        s.rules = [lambda m, u, st, p: "hold" if m == "POST" and u.endswith("/pause-new-entries") and st == "response" and not s.held else None]
        s.js("document.querySelector('#tv-safety [data-safety=\"pause\"]').click()")
        s.pump(0.8)
        s.rules = []
        n0 = len(s.posts("/api/execution-safety/emergency-stop"))
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.pump(0.8)
        stop_sent = len(s.posts("/api/execution-safety/emergency-stop")) > n0 and bool(s.held)
        s.release()
        s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result === 'CONFIRMED'", 30, "B5 stop confirmed")
        c.r.check("B5 a hung Pause, then Stop: the Stop POST is sent while the Pause is in flight, and the late Pause answer does not overwrite the stop",
                  stop_sent and _kill(c) and s.js(f"{rc}.state.data.kill_switch.active") is True)
        _reset(c, "B5")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B7 + B18: Stop answers 200 UNKNOWN with the kill inactive and reads show it not active (nothing can be saved): re-sent once, NOT confirmed
        n0 = len(s.posts("/api/execution-safety/emergency-stop"))
        with _Patch(svc.store, latch=_raise, commit=_raise):
            s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
            s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result === 'NOT_CONFIRMED'", 40, "B7 not confirmed")
        n1 = len(s.posts("/api/execution-safety/emergency-stop")) - n0
        s.pump(2.5)
        c.r.check("B7 nothing could be saved: re-sent once, then 'NOT confirmed' visible in the Risk card and the bar, never a claimed success",
                  n1 == 2 and NOT_CONFIRMED in _card(s) and NOT_CONFIRMED in _bar(s) and not _kill(c), [n1, _card(s)[:200]])
        s.js("document.querySelector('#tv-safety [data-safety=\"pause\"]').click()")
        s.wait(f"{rc}.status().outcomes.pause && {rc}.status().outcomes.pause.result === 'CONFIRMED'", 20, "B18 pause")
        s.pump(2.5)
        c.r.check("B18 a later confirmed Pause keeps Stop's NOT confirmed line visible", NOT_CONFIRMED in _card(s), _card(s)[:300])
        _reset(c, "B18")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B17: the latch is saved, the evaluation's commit fails: 200 UNKNOWN with kill_switch.active true is a confirmed stop, no re-send
        n0 = len(s.posts("/api/execution-safety/emergency-stop"))
        with _Patch(svc.store, commit=_raise):
            s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
            s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result === 'CONFIRMED'", 40, "B17 confirmed")
            s.pump(2.5)
            c.r.check("B17 200 UNKNOWN with the kill active: confirmed with no re-send; the server UNKNOWN reads 'Risk status unknown · Emergency Stop active'",
                      len(s.posts("/api/execution-safety/emergency-stop")) - n0 == 1 and _kill(c) and "Risk status unknown" in s.text("#tv-safety"))
        _reset(c, "B17")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B8: Stop answers 500 (replaced at the response stage after the server saved it); the next read shows it active
        s.rules = [lambda m, u, st, p: ("fulfill", 500, "{\"message\": \"Internal Server Error\"}") if m == "POST" and u.endswith("/emergency-stop") and st == "response" else None]
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result === 'CONFIRMED'", 30, "B8 confirmed")
        s.rules = []
        s.pump(2.5)
        c.r.check("B8 Stop answered 500 and a later read shows it active: 'the server shows Emergency Stop active' in the card and the bar",
                  "The request reported an error, but the server shows Emergency Stop active." in _card(s) and _kill(c), _card(s)[:300])
        _reset(c, "B8")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B9 + B30: the network is lost: every safety request fails; uncertain at once on the next read; Stop stays enabled; NOT confirmed after a press
        s.rules = [lambda m, u, st, p: "fail" if "/api/execution-safety" in u and st == "request" else None]
        s.js(f"{rc}.refresh()"); s.pump(2.5)
        head = s.text('#pui-cards [data-pui-card="risk"] .pui-big')
        c.r.check("B30 network lost: the Risk card's headline and the bar say uncertain, never allowed; the card's Stop is offered and enabled",
                  "RISK STATUS UNCERTAIN" in _bar(s) and UNCERTAIN in _card(s) and head.startswith("Status uncertain")
                  and s.js("!!document.querySelector('#pui-cards [data-pui=\"stop\"]:not([disabled])')") is True, [_bar(s)[:200], head, _card(s)[:200]])
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result === 'NOT_CONFIRMED'", 30, "B9 not confirmed")
        g0 = len([e for e in s.b.events if e.get("method") == "Fetch.requestPaused" and e["params"]["request"]["url"].split("?")[0].endswith("/api/execution-safety")])
        s.pump(6.0)
        g1 = len([e for e in s.b.events if e.get("method") == "Fetch.requestPaused" and e["params"]["request"]["url"].split("?")[0].endswith("/api/execution-safety")])
        c.r.check("B9 the Stop POST and the reads all fail: NOT confirmed is visible, and a read is retried within about 5 s", NOT_CONFIRMED in _bar(s) and g1 > g0, [g0, g1])
        s.rules = []
        s.js(f"{rc}.refresh()"); s.pump(1.0)
        _reset(c, "B9")

        # B19: a 200 with an unreadable body is a failed read
        s.rules = [lambda m, u, st, p: ("fulfill", 200, "not json") if GET(m, u, st, p) and st == "request" else None]
        s.js(f"{rc}.refresh()"); s.pump(1.0)
        c.r.check("B19 a 200 with an unreadable body is a failed read: uncertain, 'Risk controls unavailable'", s.js(f"{rc}.status().uncertain") is True
                  and "Risk controls unavailable" in (s.js(f"{rc}.status().error") or ""))
        s.rules = []
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B4 + B10 + B12: a stale stop while the server is allowed; no successful read for 65 s; the shell never says allowed, healthy or blocked
        c.http("POST", "/api/execution-safety/emergency-stop", {})
        s.js(f"{rc}.refresh()"); s.pump(1.0)
        c.http("POST", "/api/execution-safety/clear-emergency-stop", {})              # the server is allowed; the page still shows the stop
        s.js("(() => { const r = Date.now.bind(Date); Date.now = () => r() + 66000; })()")   # 66 s with no successful read (a throttled tab)
        s.pump(2.5)
        card, bar = _card(s), _bar(s)
        c.r.check("B10 no successful read for 65 s: the Risk card and the bar change on their own", "Status uncertain" in card and "RISK STATUS UNCERTAIN" in bar, [card[:200], bar[:200]])
        c.r.check("B4 a stale stop: the card's Stop row reads UNKNOWN with Emergency Stop offered and enabled, Pause offered, no Clear",
                  s.js("(() => { const r = document.querySelector('#pui-cards [data-pui-row=\"stop\"]'); return !!r && r.dataset.puiStatus === 'UNKNOWN' && !!r.querySelector('[data-pui=\"stop\"]:not([disabled])'); })()") is True
                  and s.js("!!document.querySelector('#pui-cards [data-pui=\"pause\"]')") is True and s.js("!document.querySelector('#pui-cards [data-pui=\"clear\"]')") is True)
        head = s.text('#pui-cards [data-pui-card="risk"] .pui-big')
        agent = s.text('#pui-cards [data-pui-card="agent"]')
        c.r.check("B12 uncertain: the headline, the agent card and the system line never say allowed, healthy, stopped or blocked",
                  head.startswith("Status uncertain") and "stopped by risk controls" not in agent and "SYSTEM HEALTHY" not in bar and "NEW AUTOMATIC ENTRIES STOPPED" not in bar
                  and "EMERGENCY STOP ACTIVE" not in card, [head, agent[:120], bar[:200]])
        s.js("location.reload()")                                                      # the real clock again
        s.wait("document.readyState === 'complete' && !!window.RiskControls && !!RiskControls.state.data", 60, "reload")
        _reset(c, "B4")

        # B12 server UNKNOWN: the gate itself cannot evaluate; the page says what it blocks
        with _Patch(svc, account=lambda *a, **k: _raise()):
            s.js(f"{rc}.refresh()"); s.pump(3.0)
            card, bar = _card(s), _bar(s)
            c.r.check("B12 server UNKNOWN: 'Risk status unknown', its explanation, and the bar says RISK STATUS UNKNOWN",
                      "Risk status unknown" in card and "Automatic entries are blocked. This state does not by itself block manual paper orders." in card and "RISK STATUS UNKNOWN" in bar,
                      [card[:300], bar[:200]])
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B11: a Clear in flight (held), then Stop: the end state is stopped
        c.http("POST", "/api/execution-safety/emergency-stop", {})
        s.js(f"{rc}.refresh()"); s.pump(1.0)
        s.rules = [lambda m, u, st, p: "hold" if m == "POST" and u.endswith("/clear-emergency-stop") and st == "request" and not s.held else None]
        s.js("document.querySelector('#tv-safety [data-safety=\"clear\"]').click(); document.querySelector('#tv-safety [data-safety=\"clear\"]').click()")
        s.pump(0.5)
        s.rules = []
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.pump(1.0)
        s.release()                                                                    # the Clear commits after the Stop
        s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result !== 'PENDING'", 40, "B11 stop decided")
        c.r.check("B11 a Clear in flight, then Stop: the end state is stopped, or NOT confirmed is visible; never a silent allowed",
                  _kill(c) or NOT_CONFIRMED in _bar(s), [_kill(c), _bar(s)[:200]])
        _reset(c, "B11")
        s.js(f"{rc}.refresh()"); s.pump(1.0)

        # B11b: a stop confirmed earlier, then cleared from another tab
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result === 'CONFIRMED'", 30, "B11b stop")
        c.http("POST", "/api/execution-safety/clear-emergency-stop", {})
        s.js(f"{rc}.refresh()"); s.pump(2.5)
        c.r.check("B11b a stop cleared from another tab is announced: 'Emergency Stop is no longer active.'", "Emergency Stop is no longer active." in _bar(s), _bar(s)[:200])
        _reset(c, "B11b")

        # B11a: a Clear aborted by the page after 20 s, Stop pressed while the kill still reads on, the server commits the Clear later
        c.http("POST", "/api/execution-safety/emergency-stop", {})
        s.js(f"{rc}.refresh()"); s.pump(1.0)
        s.rules = [lambda m, u, st, p: "hold" if m == "POST" and u.endswith("/clear-emergency-stop") and st == "request" and not s.held else None]
        s.js("document.querySelector('#tv-safety [data-safety=\"clear\"]').click(); document.querySelector('#tv-safety [data-safety=\"clear\"]').click()")
        s.pump(21.0)                                                                   # the page aborts it at 20 s; it never reached the server
        s.rules, s.held = [], []
        n0 = len(s.posts("/api/execution-safety/emergency-stop"))
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.pump(3.0)
        pending = s.js(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result") == "PENDING"
        c.http("POST", "/api/execution-safety/clear-emergency-stop", {})              # the aborted Clear's late commit
        s.wait(f"{rc}.status().outcomes.stop && {rc}.status().outcomes.stop.result !== 'PENDING'", 80, "B11a decided")
        c.r.check("B11a an aborted Clear committing late: Stop stayed pending, re-sent, and the stop holds",
                  pending and len(s.posts("/api/execution-safety/emergency-stop")) - n0 >= 2 and _kill(c) and s.js(f"{rc}.status().outcomes.stop.result") == "CONFIRMED")
        _reset(c, "B11a")

        # B6: every read fails from page load: Stop is there and its click reaches the server
        s.rules = [lambda m, u, st, p: "fail" if GET(m, u, st, p) and st == "request" else None]
        s.open("/")
        s.wait("!!window.RiskControls && RiskControls.status().loading === false", 30, "B6 first read failed")
        n0 = len(s.posts("/api/execution-safety/emergency-stop"))
        s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
        s.pump(2.0)
        c.r.check("B6 every read fails from load: the panel renders Emergency Stop and its click reaches the server",
                  s.js("!!document.querySelector('#tv-safety [data-safety=\"stop\"]')") is True and len(s.posts("/api/execution-safety/emergency-stop")) > n0 and _kill(c))
        s.rules = []
        _reset(c, "B6")

        # B31: six GETs held inside the server (the browser's per-host limit), then Stop: how long the POST waits (a measurement, design 3.4).
        # Interception is off here, so the queue measured is the browser's own.
        s.intercept()
        s.open("/")
        s.wait("!!window.RiskControls && !!RiskControls.state.data", 30, "B31 page")
        gate, arrived = threading.Event(), {}
        real_overview = type(svc).overview

        def held_overview(self, *a, **k):
            gate.wait(15)
            return real_overview(self, *a, **k)

        def timed_stop(reason=None, actor="user", now=None, _real=type(svc).emergency_stop):
            arrived.setdefault("at", time.monotonic())
            return _real(svc, reason=reason, actor=actor, now=now)
        with _Patch(svc, overview=held_overview.__get__(svc), emergency_stop=timed_stop):
            s.js("for (let i = 0; i < 6; i++) fetch('/api/execution-safety', { headers: { 'X-Stock-Agent-Intent': 'dashboard' } }).catch(() => null);")
            s.pump(1.0)
            t0 = time.monotonic()
            s.js("document.querySelector('#tv-safety [data-safety=\"stop\"]').click()")
            end = time.monotonic() + 17                                                  # the six held GETs end on their own after 15 s (gate.wait(15))
            while not arrived and time.monotonic() < end:
                s.pump(0.2)
            waited = (arrived["at"] - t0) if arrived else None
            gate.set()
            s.pump(2.0)
            stopped = _kill(c)
        # a measurement, recorded, not a pass / fail (design B31): the answer is the in-flight guard on the panel's own poll (risk_controls.js)
        c.r.check(f"B31 measured: with six GETs held in the server the Stop POST waited {('%.1f s' % waited) if waited is not None else 'more than 17 s'} "
                  "(the browser's six-connection limit: it is sent when a held request ends)", True, {"waited_s": None if waited is None else round(waited, 2), "stop_saved": stopped})
        _reset(c, "B31")
    finally:
        s.close()
        _reset(c, "risk_controls_races")
    _b15_natural_race(c)                                                                     # its own browser, no interception


B15_OFFSETS_MS, B15_TRIALS = (-10, -5, 0, 5, 10), 80                   # 400 trials within +-10 ms (design B15)
B15_TRIAL = """((d) => new Promise((done) => {
  const t0 = performance.now();
  const go = { refresh: null };
  const doRefresh = () => { go.refresh = RiskControls.refresh(); };
  const doClick = () => { document.querySelector('#tv-safety [data-safety="pause"]').click(); };
  if (d >= 0) { doRefresh(); setTimeout(doClick, d); } else { doClick(); setTimeout(doRefresh, -d); }
  const settle = () => { if (go.refresh && !RiskControls.state.busy) { go.refresh.then(() => setTimeout(() => {
      const s = RiskControls.state.data || {}; done({ panel: s.state, pause: !!(s.no_new_entries || {}).active, ms: performance.now() - t0 }); }, 15)); }
    else setTimeout(settle, 2); };
  setTimeout(settle, Math.abs(d) + 1);
}))(%d)"""


def _b15_natural_race(c):
    """B15: the stale-poll overwrite measured naturally (no answer held): 55 of 400 before the panel; the generation counter makes it 0.
    A measurement on this machine, not a proof; B1 to B5 prove the order with answers held."""
    s = Side(c)
    try:
        s.intercept()                                                                      # Fetch disabled: nothing is held or delayed
        s.open("/")
        s.wait("!!window.RiskControls && !!RiskControls.state.data && RiskControls.state.data.state === 'TRADING_ALLOWED'", 60, "B15 risk controls loaded")
        s.js("clearInterval(RiskControls.state.timer); true")                               # the real 30 s poll must not interfere with the measured refresh
        table, total, hits = {}, 0, 0
        for d in B15_OFFSETS_MS:
            n = 0
            for _ in range(B15_TRIALS):
                c.http("POST", "/api/execution-safety/resume-new-entries", {})
                s.js_await("RiskControls.refresh()")                                         # awaited: the panel is in sync before the trial
                r = s.js_await(B15_TRIAL % d) or {}
                server_paused = bool((_state(c).get("no_new_entries") or {}).get("active"))
                n += 1 if (server_paused and not r.get("pause")) else 0
                total += 1
            table[d], hits = n, hits + n
        c.r.eq(f"B15 natural race: a poll and a Pause within +-10 ms, {total} trials, nothing held: the panel never shows a stale state "
               "(55 of 400 before the panel)", [total, hits], [len(B15_OFFSETS_MS) * B15_TRIALS, 0])
        c.r.check("B15 per offset (ms -> overwritten)", True, table)
    finally:
        s.close()
        _reset(c, "B15")


# =========================================================================================================================================
def risk_controls_presses(c):
    """B13, B28, B32, B33, B36: the strip's Stop in the expanded layout, the bar's Stop from every section with a real pointer, presses held
    across a re-render, the read-only open-order line, and a lost pointerup."""
    s = Side(c)
    svc = _svc()
    try:
        s.intercept()
        s.open("/")
        s.wait("!!window.RiskControls && !!RiskControls.state.data", 30, "loaded")
        # B13: the expanded layout's strip: the confirmation shows and works
        s.js("ProductUI.expandDetails()")
        s.pump(2.5)
        pressed = s.press('#pui-cards [data-pui="stop"]')
        s.wait("!!document.querySelector('#pui-cards [data-pui-confirm=\"stop\"]')", 10, "B13 confirmation")
        shown = s.visible('#pui-cards [data-pui-confirm="stop"]')
        confirmed = s.press('#pui-cards [data-pui="stop-confirm"]')
        s.wait("RiskControls.status().outcomes.stop && RiskControls.status().outcomes.stop.result === 'CONFIRMED'", 30, "B13 confirmed")
        c.r.check("B13 expanded layout: the strip's Stop confirmation is visible and saves the stop", pressed and shown and confirmed and _kill(c))
        _reset(c, "B13")
        s.js("ProductUI.collapseDetails()")
        # B28: the bar's Stop from POSITIONS, ACTIVITY and MORE
        for section in ("positions", "activity", "more"):
            s.js(f"document.querySelector('#pui-nav [data-pui-nav=\"{section}\"]').click()")
            s.pump(1.0)
            pressed, shown, confirmed = _confirm_bar_stop(s)
            s.wait("document.querySelector('#pui-status').innerText.includes('Emergency Stop active')", 30, f"B28 {section}")
            c.r.check(f"B28 the bar's Stop from {section.upper()} (real pointer): confirmation in the bar, POST to the server, outcome in the bar",
                      pressed and shown and confirmed and _kill(c))
            _reset(c, f"B28 {section}")
            s.js("RiskControls.refresh()"); s.pump(2.5)
        s.js("document.querySelector('#pui-nav [data-pui-nav=\"trade\"]').click()")
        s.pump(1.5)
        # B32: a press held while the content changes and a re-render is attempted, 200 trials, on the bar's button and on the card's
        lost = 0
        for i in range(100):
            for target, opened, cancel, rerender in (('#pui-status [data-pui="stop-bar"]', "ProductUI.state.barConfirm === true", "ProductUI.state.barConfirm = false; ProductUI.renderStatus();",
                                                      "ProductUI.state.sysOpen = !ProductUI.state.sysOpen; ProductUI.renderStatus();"),
                                                     ('#pui-cards [data-pui="stop"]', "ProductUI.state.confirm === 'stop'", "ProductUI.state.confirm = null; ProductUI.renderCards();",
                                                      "ProductUI.state.riskOpen = !ProductUI.state.riskOpen; ProductUI.renderCards();")):
                r = s.js(f"(() => {{ const e = document.querySelector({json.dumps(target)}); const b = e.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; }})()")
                s.b.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": r[0], "y": r[1], "button": "left", "buttons": 1, "clickCount": 1})
                s.js(rerender)                                                         # the tick's re-render, with changed content, during the press
                s.b.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": r[0], "y": r[1], "button": "left", "buttons": 0, "clickCount": 1})
                if not s.js(opened):
                    lost += 1
                s.js(cancel)
        c.r.eq("B32 200 presses held across a re-render (100 on the bar's Stop, 100 on the card's): none lost", lost, 0)
        # B36: pointerup never arrives: re-rendering resumes within 1 s
        r = s.js("(() => { const b = document.querySelector('#pui-status [data-pui=\"stop-bar\"]').getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; })()")
        s.b.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": r[0], "y": r[1], "button": "left", "buttons": 1, "clickCount": 1})
        s.js("ProductUI.state.sysOpen = true; ProductUI.renderStatus()")
        held = s.js("!document.querySelector('#pui-status [data-pui-system-details]')")
        s.pump(1.2)
        s.js("ProductUI.renderStatus()")
        resumed = s.js("!!document.querySelector('#pui-status [data-pui-system-details]')")
        s.b.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 1, "y": 1, "button": "left", "buttons": 0, "clickCount": 1})
        s.js("ProductUI.state.sysOpen = false; ProductUI.state.barConfirm = false; ProductUI.renderStatus()")
        c.r.check("B36 a press without pointerup holds re-rendering, which resumes within 1 s", held is True and resumed is True)
        # B33: the read-only open-order line, from the stored snapshot only (0 broker calls)
        from api.routes.trader_view import stream
        acct = stream().account_refresh
        at = "2026-11-09T14:32:00+00:00"
        cases = [({"status": "FRESH", "refreshed_at": at, "snapshot": {"open_orders": 2}}, "Open orders at Alpaca: 2, as of 14:32 UTC. This app does not cancel orders. Use Alpaca's website to cancel."),
                 ({"status": "STALE", "refreshed_at": at, "snapshot": {"open_orders": 1}}, "Open orders at Alpaca: 1, as of 14:32 UTC (stale)."),
                 ({"status": "STARTING", "refreshed_at": at, "snapshot": {"open_orders": 0}}, "Open orders at Alpaca: 0, as of 14:32 UTC (stale)."),
                 ({"status": "DISABLED", "refreshed_at": None, "snapshot": None}, "Open orders at Alpaca: unavailable. Check Alpaca's website."),
                 ({"status": "FRESH", "refreshed_at": at, "snapshot": None}, "Open orders at Alpaca: unavailable. Check Alpaca's website."),
                 ({"status": "FRESH", "refreshed_at": at, "snapshot": {"open_orders": 500}}, "Open orders at Alpaca: at least 500, as of 14:32 UTC.")]
        calls0 = len(getattr(c.broker, "requests", []))
        c.http("POST", "/api/execution-safety/emergency-stop", {})
        for payload, want in cases:
            with _Patch(acct, overview=lambda p=payload: dict(p)):
                s.js("RiskControls.refresh()")
                s.pump(3.0)
                card = _card(s)
            c.r.check(f"B33 open-order line ({payload['status']}, {payload['snapshot']}): {want[:40]}…", want in card, card[:400])
        c.r.eq("B33 the open-order line costs 0 broker calls", len(getattr(c.broker, "requests", [])) - calls0, 0)
        _reset(c, "B33")
    finally:
        s.close()
        _reset(c, "risk_controls_presses")
