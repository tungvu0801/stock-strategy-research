"""
execution/broker.py — Stage 6.0 Alpaca PAPER broker client (DESIGN_60 §2). The ONLY broker client of the execution layer.

  * Every URL is built from the constant host paper-api.alpaca.markets. There is no base-URL parameter, no environment switch and no
    live mode: the transport refuses any other scheme / host / port BEFORE anything is sent.
  * Reads: an exact GET allow-list (account, clock, one asset, positions, one position, orders list, one order, order by OUR
    client_order_id prefix) with a per-cycle request budget. Writes: exactly one POST /v2/orders per attempt whose body must equal the
    canonical payload of the intent (whole shares, market, day, no extended hours, no order class, no price).
  * Credentials: ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY (names only; values are never logged or returned). The simulated broker
    (execution/simulated.py) sits BELOW this client as a requests adapter, so tests and the browser harness exercise the same code.
  * No SDK, no automatic retry (urllib3 max_retries 0), no redirects, finite timeouts. POST outcomes are classified with the frozen
    Stage 4.6B pure rules (response / not_sent / uncertain -> O1 … O8).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import NewConnectionError

from execution import COID_PREFIX
from paper import alpaca_order_rules as RU

PAPER_HOST = "paper-api.alpaca.markets"
BASE = f"https://{PAPER_HOST}"
KEY_ENV, SECRET_ENV = "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY"
TIMEOUT = (5.0, 10.0)                                      # (connect, read) for reads
POST_TIMEOUT = (5.0, 15.0)
GET_BUDGET = 80                                            # reads per cycle (reset by the service at the start of each cycle)
MAX_BODY = 262144
SYMBOL = r"[A-Z][A-Z0-9.\-]{0,11}"
_GET_PATH = re.compile(rf"^/v2/(account|clock|positions|orders|assets/{SYMBOL}|positions/{SYMBOL}|orders/[0-9a-f\-]{{36}}|orders:by_client_order_id)$")
_ORDER_KEYS = ("id", "client_order_id", "symbol", "side", "qty", "filled_qty", "filled_avg_price", "status", "type", "order_type", "time_in_force", "order_class",
               "extended_hours", "submitted_at", "created_at", "updated_at", "filled_at", "canceled_at", "expired_at", "failed_at", "asset_class")


class BrokerRefused(RuntimeError):
    """Refused by this client before anything was sent (wrong host, path, verb, body or budget)."""


class BrokerError(RuntimeError):
    def __init__(self, code: str, http_status: Optional[int] = None, detail: str = ""):
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code, self.http_status, self.detail = code, http_status, detail


def _wire() -> HTTPAdapter:
    return HTTPAdapter(max_retries=0)


WIRE = _wire                                                # the real network adapter factory (tests and the harness replace it)


def env_credentials() -> Tuple[Optional[str], Optional[str]]:
    return os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)


def _not_sent(exc: requests.exceptions.ConnectionError) -> bool:
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


class _Transport(HTTPAdapter):
    """Paper host only. GET: the allow-list within the budget. POST: /v2/orders once, body == the stored payload."""

    def __init__(self, wire, *, post_body: Optional[bytes] = None, budget: int = GET_BUDGET):
        super().__init__(max_retries=0)
        self._wire, self._post_body, self._posted, self.left = wire, post_body, False, int(budget)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        if u.scheme != "https" or u.hostname != PAPER_HOST or u.port not in (None, 443):
            raise BrokerRefused("only the Alpaca PAPER host is allowed")
        path = unquote(u.path)
        if request.method == "POST":
            if self._post_body is None:
                raise BrokerRefused("no POST on a read transport")
            if path != "/v2/orders" or u.query:
                raise BrokerRefused("POST only to the paper /v2/orders URL")
            if request.body != self._post_body:
                raise BrokerRefused("the body is not the stored payload")
            if self._posted:
                raise BrokerRefused("one POST per attempt")
            self._posted = True
            return self._wire.send(request, stream=stream, timeout=POST_TIMEOUT, verify=True, cert=cert, proxies=proxies)
        if request.method != "GET":
            raise BrokerRefused("only GET reads and the single order POST are allowed")
        if not _GET_PATH.match(path):
            raise BrokerRefused(f"GET {path} is not in the allow-list")
        if path == "/v2/orders:by_client_order_id":
            coid = (parse_qs(u.query).get("client_order_id") or [""])[0]
            if not coid.startswith(COID_PREFIX):
                raise BrokerRefused("only Stage 6.0 client order ids are looked up")
        if self.left <= 0:
            raise BrokerRefused("read budget of this cycle exhausted")
        self.left -= 1
        return self._wire.send(request, stream=stream, timeout=TIMEOUT, verify=True, cert=cert, proxies=proxies)

    def close(self):
        try:
            self._wire.close()
        except Exception:  # noqa: BLE001
            pass


def norm_order(o: dict) -> Optional[dict]:
    """The fields the execution layer keeps of a broker order (strings for money, int for quantities)."""
    if not isinstance(o, dict) or not isinstance(o.get("id"), str) or not RU.UUID_RE.match(o["id"]):
        return None
    try:
        qty = RU.dec(o.get("qty"))
        fq = RU.dec(o.get("filled_qty"))
        fp = RU.dec(o.get("filled_avg_price"))
    except ValueError:
        return None
    return {"id": o["id"], "client_order_id": o.get("client_order_id"), "symbol": o.get("symbol"), "side": o.get("side"), "qty": RU.qty_str(qty),
            "filled_qty": int(fq) if fq is not None else 0, "filled_avg_price": RU.money(fp) if fp is not None else None, "status": o.get("status"),
            "type": o.get("type") or o.get("order_type"), "time_in_force": o.get("time_in_force"), "order_class": o.get("order_class") or "",
            "extended_hours": bool(o.get("extended_hours")), "submitted_at": o.get("submitted_at") or o.get("created_at"), "updated_at": o.get("updated_at"),
            "filled_at": o.get("filled_at"), "raw": {k: o.get(k) for k in _ORDER_KEYS if k in o}}


def norm_position(p: dict) -> Optional[dict]:
    if not isinstance(p, dict) or not p.get("symbol"):
        return None
    try:
        qty = RU.dec(p.get("qty"))
        avg = RU.dec(p.get("avg_entry_price"))
    except ValueError:
        return None
    return {"symbol": str(p["symbol"]).upper(), "qty": int(qty) if qty is not None else 0, "qty_available": p.get("qty_available"), "side": p.get("side") or "long",
            "avg_entry_price": RU.money(avg) if avg is not None else None, "market_value": p.get("market_value"), "unrealized_pl": p.get("unrealized_pl")}


class AlpacaPaperBroker:
    """The paper broker as the execution layer sees it. `simulated=True` labels a client whose wire is the deterministic simulated broker."""

    def __init__(self, *, credentials_fn: Callable[[], Tuple[Optional[str], Optional[str]]] = env_credentials, wire: Optional[Callable[[], object]] = None,
                 simulated: bool = False, budget: int = GET_BUDGET):
        self.credentials_fn = (lambda: ("simulated", "simulated")) if simulated else credentials_fn
        self.wire = wire or (lambda: WIRE())
        self.simulated = bool(simulated)
        self.label = "SIMULATED_PAPER" if simulated else "ALPACA_PAPER"
        self.budget = int(budget)
        self.reads = 0
        self.posts = 0
        self._transport: Optional[_Transport] = None

    # ---------------------------------------------------------------- plumbing --------------------------------------------------
    def configured(self) -> bool:
        key, secret = self.credentials_fn()
        return bool(key and secret)

    def _headers(self) -> dict:
        key, secret = self.credentials_fn()
        if not key or not secret:
            raise BrokerError("NOT_CONFIGURED")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    def reset_budget(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = None

    def _read_session(self) -> requests.Session:
        if self._transport is None:
            self._transport = _Transport(self.wire(), budget=self.budget)
        http = requests.Session()
        http.mount("https://", self._transport)
        http.mount("http://", self._transport)
        return http

    def _get(self, path: str, params: Optional[dict] = None, allow_404: bool = False):
        headers = self._headers()
        http = self._read_session()
        self.reads += 1
        try:
            r = http.get(BASE + path, params=params, headers=headers, allow_redirects=False, timeout=TIMEOUT)
        except BrokerRefused as exc:
            raise BrokerError("BLOCKED", detail=str(exc)) from None
        except requests.exceptions.Timeout:
            raise BrokerError("TIMEOUT") from None
        except requests.exceptions.RequestException as exc:
            raise BrokerError("CONNECTION", detail=type(exc).__name__) from None
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code == 429:
            raise BrokerError("RATE_LIMITED", 429)
        if r.status_code != 200:
            raise BrokerError(f"HTTP_{r.status_code}", r.status_code)
        try:
            return json.loads((r.content or b"")[:MAX_BODY])
        except ValueError:
            raise BrokerError("UNREADABLE", 200) from None

    # ---------------------------------------------------------------- reads ----------------------------------------------------
    def environment(self, now: Optional[datetime] = None) -> dict:
        """The paper proof: true only when the constant paper host answered the account read."""
        out = {"paper": False, "host": PAPER_HOST, "label": self.label, "simulated": self.simulated, "account_number_masked": None, "status": None, "blocked": False,
               "checked_at": _iso(now), "error": None}
        if not self.configured():
            return {**out, "error": "NOT_CONFIGURED"}
        try:
            a = self.account()
        except BrokerError as exc:
            return {**out, "error": exc.code}
        return {**out, "paper": True, "account_number_masked": a["account_number_masked"], "status": a["status"], "blocked": a["blocked"]}

    def account(self) -> dict:
        a = self._get("/v2/account")
        if not isinstance(a, dict) or "status" not in a:
            raise BrokerError("UNREADABLE", 200)
        blocked = bool(a.get("trading_blocked") or a.get("account_blocked") or a.get("trade_suspended_by_user"))
        return {"account_number_masked": RU.mask(a.get("account_number")), "status": a.get("status"), "blocked": blocked, "equity": a.get("equity"), "cash": a.get("cash"),
                "buying_power": a.get("buying_power"), "currency": a.get("currency"), "pattern_day_trader": bool(a.get("pattern_day_trader"))}

    def clock(self) -> dict:
        c = self._get("/v2/clock")
        if not isinstance(c, dict) or "is_open" not in c:
            raise BrokerError("UNREADABLE", 200)
        return {"timestamp": c.get("timestamp"), "is_open": bool(c.get("is_open")), "next_open": c.get("next_open"), "next_close": c.get("next_close")}

    def asset(self, symbol: str) -> Optional[dict]:
        a = self._get(f"/v2/assets/{symbol.upper()}", allow_404=True)
        if a is None:
            return None
        return {"symbol": a.get("symbol"), "tradable": bool(a.get("tradable")), "status": a.get("status"), "asset_class": a.get("class") or a.get("asset_class"),
                "fractionable": bool(a.get("fractionable")), "exchange": a.get("exchange")}

    def position(self, symbol: str) -> Optional[dict]:
        p = self._get(f"/v2/positions/{symbol.upper()}", allow_404=True)
        return norm_position(p) if p is not None else None

    def positions(self) -> List[dict]:
        rows = self._get("/v2/positions")
        if not isinstance(rows, list):
            raise BrokerError("UNREADABLE", 200)
        return [x for x in (norm_position(p) for p in rows) if x is not None]

    def orders(self, status: str = "open", symbols: Optional[List[str]] = None, limit: int = 200) -> List[dict]:
        params = {"status": status, "limit": int(limit), "direction": "desc"}
        if symbols:
            params["symbols"] = ",".join(s.upper() for s in symbols)
        rows = self._get("/v2/orders", params=params)
        if not isinstance(rows, list):
            raise BrokerError("UNREADABLE", 200)
        return [x for x in (norm_order(o) for o in rows) if x is not None]

    def order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        o = self._get("/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}, allow_404=True)
        return norm_order(o) if o is not None else None

    def order_by_id(self, order_id: str) -> Optional[dict]:
        if not RU.UUID_RE.match(order_id or ""):
            raise BrokerError("BLOCKED", detail="not an order id")
        o = self._get(f"/v2/orders/{order_id}", allow_404=True)
        return norm_order(o) if o is not None else None

    # ---------------------------------------------------------------- the one write --------------------------------------------
    def post_order(self, payload: bytes, client_order_id: str) -> dict:
        """ONE POST of the canonical payload. Returns {"kind": "response", "status", "body", "latency_ms"} | {"kind": "not_sent", "reason"} |
        {"kind": "uncertain", "reason"} — exactly the Stage 4.6B result shape, classified by paper.alpaca_order_rules.classify_post."""
        if not isinstance(payload, bytes) or not client_order_id.startswith(COID_PREFIX):
            raise BrokerRefused("invalid payload or client order id")
        try:
            body = json.loads(payload)
        except ValueError:
            raise BrokerRefused("the payload is not JSON") from None
        if body.get("client_order_id") != client_order_id or payload != RU.canonical_payload(client_order_id, body.get("symbol", ""), body.get("side", ""), int(body.get("qty", 0))):
            raise BrokerRefused("the payload is not the canonical payload of this client order id")
        if body.get("side") not in ("buy", "sell") or int(body.get("qty", 0)) < 1:
            raise BrokerRefused("only whole-share buy / sell market day orders")
        key, secret = self.credentials_fn()
        if not key or not secret:
            return {"kind": "not_sent", "reason": "NOT_CONFIGURED"}
        transport = _Transport(self.wire(), post_body=payload, budget=0)
        http = requests.Session()
        http.mount("https://", transport)
        http.mount("http://", transport)
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json", "Accept": "application/json"}
        self.posts += 1
        t0 = time.perf_counter()
        try:
            r = http.post(BASE + "/v2/orders", data=payload, headers=headers, allow_redirects=False, timeout=POST_TIMEOUT)
        except BrokerRefused:
            return {"kind": "not_sent", "reason": "LOCAL_REFUSAL"}
        except requests.exceptions.ConnectTimeout:
            return {"kind": "not_sent", "reason": "NOT_SENT"}
        except requests.exceptions.SSLError:
            return {"kind": "uncertain", "reason": "CONNECTION"}
        except requests.exceptions.Timeout:
            return {"kind": "uncertain", "reason": "TIMEOUT"}
        except requests.exceptions.ConnectionError as exc:
            return {"kind": "not_sent", "reason": "NOT_SENT"} if _not_sent(exc) else {"kind": "uncertain", "reason": "CONNECTION"}
        except Exception:  # noqa: BLE001 - anything unexpected after the request may have left: uncertain (never raw text)
            return {"kind": "uncertain", "reason": "CONNECTION"}
        finally:
            http.close()
        return {"kind": "response", "status": r.status_code, "body": (r.content or b"")[:MAX_BODY], "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
