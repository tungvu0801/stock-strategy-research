"""Test setup: isolated DB copy, no network, no .env and no credential. Must run before the app is imported."""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Wave 1 (audit EP-13): no test process, and no process that inherits its environment, loads this checkout's .env (config.py skips it), and no
# credential reaches a test from the environment, whichever folder the tests run from. A process started through WMI (the Stage 6.0 runner rig's
# background start) inherits neither, so run tests outside the production checkout. A test that needs a credential-shaped value sets a fake
# one itself; a script given an explicit synthetic env file still loads that file (python-dotenv itself is not switched off).
os.environ["STOCK_AGENT_SKIP_DOTENV"] = "1"
CREDENTIAL_ENV = re.compile(r"(?i)^(ALPACA|APCA|ANTHROPIC|ROBINHOOD|FRED_API_KEY|SEC_USER_AGENT|OPENAI|POLYGON|FINNHUB|ALPHA_?VANTAGE)")
for _name in [n for n in os.environ if CREDENTIAL_ENV.match(n)]:
    del os.environ[_name]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fastapi.testclient as _ftc  # noqa: E402 - EX-06 (D-20): the default test client is the trusted local dashboard
from starlette.testclient import TestClient as _RawTestClient  # noqa: E402
from urllib.parse import urljoin as _urljoin, urlsplit as _urlsplit  # noqa: E402


def _headers(base: dict, extra) -> dict:
    """`base` with `extra` (a dict, a header object or pairs) on top, case-insensitively: a test's own header always wins and is never doubled."""
    out = dict(base)
    for k, v in (extra.items() if hasattr(extra, "items") else (extra or ())):
        for old in [o for o in out if o.lower() == str(k).lower()]:
            del out[old]
        out[k] = v
    return out


class _LocalTestClient(_RawTestClient):
    """EX-06 (D-20): every test's default client is the trusted local dashboard in a browser on this computer, sending exactly what headless
    Edge sends (recorded in the EX-06 simulation; tests/test_ex06_local_gate.py T-X14 pins the shapes): the loopback client and Host; on an
    /api request the dashboard intent (frontend/app.js's wrapper), same-origin fetch metadata and the page as Referer, plus an Origin when the
    method changes state; on a page a typed-URL navigation; on a WebSocket handshake an Origin only. A test's own headers always win. The
    gate's own tests use starlette's raw client, which presents as nothing (and is refused)."""

    def __init__(self, app, base_url="http://127.0.0.1:8000", *args, headers=None, client=("127.0.0.1", 50000), **kwargs):
        u = _urlsplit(str(base_url))
        self._origin, self._given = f"{u.scheme}://{u.netloc}", dict(headers or {})
        super().__init__(app, base_url, *args, headers=headers, client=client, **kwargs)

    def _browser(self, method, url) -> dict:
        safe, api = str(method).upper() in ("GET", "HEAD"), ("/" + (_urlsplit(str(url)).path or "/").lstrip("/").lower() + "/").startswith("/api/")
        if safe and not api:
            return {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        shape = {"X-Stock-Agent-Intent": "dashboard", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Referer": self._origin + "/"}
        return shape if safe else {**shape, "Origin": self._origin}

    def request(self, method, url, *args, headers=None, **kwargs):
        return super().request(method, url, *args, headers=_headers(_headers(self._browser(method, url), self._given), headers), **kwargs)

    def websocket_connect(self, url, *args, headers=None, **kwargs):
        return super().websocket_connect(_urljoin("ws" + self._origin[4:] + "/", url), *args, headers=_headers({"Origin": self._origin}, headers),
                                         **kwargs)   # starlette hard-codes ws://testserver


_ftc.TestClient = _LocalTestClient

import database.database as dbm  # noqa: E402

# Point the process-wide DB singleton at a COPY of the real database (read via the sqlite backup API,
# opened read-only), so tests can prove the portfolio code writes nothing without touching real data.
TMP_DIR = Path(tempfile.mkdtemp(prefix="stock-agent-tests-"))
TEST_DB = TMP_DIR / "stock_agent.db"
_real = ROOT / "data" / "stock_agent.db"
if _real.exists():
    src = sqlite3.connect(f"file:{_real.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(TEST_DB))
    src.backup(dst)
    src.close()
    dst.close()
dbm._db_instance = dbm.ResearchDatabase(TEST_DB)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*a, **k):
        raise AssertionError("network access attempted during tests")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def no_os_notifications(monkeypatch):
    """Stage 4.3: no test ever shows a real desktop notification (tests use a fake adapter)."""
    from notifications import windows
    hits = []
    monkeypatch.setattr(windows, "send", lambda *a, **k: hits.append(a) or {"status": "FAILED", "error_code": "BLOCKED_IN_TESTS"})
    yield
    assert not hits, "a real OS notification adapter call was attempted during tests"


@pytest.fixture(autouse=True)
def no_registry_no_browser(monkeypatch):
    """Stage 4.4: no test writes the real registry or opens a real browser (an in-memory identity + a recording opener)."""
    from notifications import identity, windows

    class MemoryRegistry:
        value = None

        def get(self):
            return self.value

        def put(self):
            self.value = identity.DISPLAY_NAME

        def remove(self):
            self.value = None
    monkeypatch.setattr(identity, "BACKEND", MemoryRegistry())
    monkeypatch.setattr(windows, "OPENER", lambda url: windows._stats.__setitem__("test_opened_url", url))


@pytest.fixture(autouse=True)
def no_alpaca_paper_broker(monkeypatch):
    """Stage 4.6A: no test ever sees real Alpaca PAPER credentials (both names are removed from the environment) or reaches
    the paper trading API (the adapter's real network wire is replaced by one that fails the test). Tests use fakes."""
    from paper import alpaca_readonly as R
    for name in (R.KEY_ENV, R.SECRET_ENV):
        monkeypatch.delenv(name, raising=False)

    def real_wire_blocked():
        raise AssertionError("a real Alpaca paper network adapter was requested during tests")
    monkeypatch.setattr(R, "WIRE", real_wire_blocked)


_ALPACA_ORDER_WIRE_HITS: list = []


@pytest.fixture(autouse=True)
def no_alpaca_paper_orders(monkeypatch):
    """Stage 4.6B: in EVERY test the real network wires of the paper order adapters (reads and the POST writer) are
    tripwires: requesting one fails the test (recorded here and asserted at teardown, so it cannot be swallowed).
    Only tests that install the fake broker (tests/alpaca_order_fakes.py) reach a wire at all."""
    from paper import alpaca_order_reads, alpaca_order_writer

    def tripwire(which):
        def _f():
            _ALPACA_ORDER_WIRE_HITS.append(which)
            raise AssertionError(f"a real Alpaca paper {which} network adapter was requested during tests")
        return _f
    monkeypatch.setattr(alpaca_order_reads, "WIRE", tripwire("read"))
    monkeypatch.setattr(alpaca_order_writer, "WIRE", tripwire("order POST"))
    yield
    hits, _ALPACA_ORDER_WIRE_HITS[:] = list(_ALPACA_ORDER_WIRE_HITS), []
    assert not hits, f"real Alpaca paper wire requested during a test: {hits}"


_EVIDENCE_WIRE_HITS: list = []


@pytest.fixture(autouse=True)
def no_evidence_network(monkeypatch):
    """Stage 5.4: in EVERY test the evidence layer's network flag is off and its single wire (evidence.net.WIRE) is a tripwire: reaching
    it fails the test (recorded here and asserted at teardown). Tests install tests/evidence_fakes.FakeWire explicitly when they need it."""
    import config as _cfg
    from evidence import net as _net
    monkeypatch.setattr(_cfg, "EVIDENCE_NETWORK_ENABLED", False)
    monkeypatch.setattr(_cfg, "SEC_USER_AGENT", "")

    def tripwire(url, headers, timeout_s, max_bytes):
        _EVIDENCE_WIRE_HITS.append(url)
        raise AssertionError(f"the real evidence network wire was requested during a test: {url}")
    monkeypatch.setattr(_net, "WIRE", tripwire)
    yield
    hits, _EVIDENCE_WIRE_HITS[:] = list(_EVIDENCE_WIRE_HITS), []
    assert not hits, f"real evidence network wire requested during a test: {hits}"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TMP_DIR, ignore_errors=True)
