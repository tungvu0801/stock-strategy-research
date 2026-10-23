"""
execution/simulated.py — Stage 6.0 deterministic SIMULATED paper broker: an in-memory requests adapter that sits BELOW the real
AlpacaPaperBroker client (execution/broker.py), so the execution layer runs the same code against it. Nothing here opens a socket.

It answers the client's exact reads (account, clock, one asset, positions, one position, orders list, one order, order by client id)
and its single POST /v2/orders; it enforces client_order_id uniqueness like Alpaca; it records every request. Fills are scripted and
deterministic: `auto_fill` = "next_read" (an accepted order fills the next time it is read — the default), "manual" (tests fill with
`fill()`), or "never". `post_modes` consume one entry per POST (the Stage 4.6B fake's vocabulary) to script timeouts, errors and
rejects. Prices come from `prices` (default 100.00). Selected only by EXECUTION_BROKER=simulated, by tests and by the browser harness.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter
from urllib3.exceptions import MaxRetryError, NewConnectionError

ACCOUNT_ID = "66666666-7777-8888-9999-000000000060"
OPEN_STATUSES = ("new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding")
_Q = Decimal("0.01")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _money(v) -> str:
    return str(Decimal(str(v)).quantize(_Q))


class SimulatedPaperBroker(BaseAdapter):
    def __init__(self, now_fn: Optional[Callable[[], datetime]] = None, *, prices: Optional[Dict[str, float]] = None, equity: str = "100000.00"):
        super().__init__()
        self.lock = threading.RLock()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.account = {"id": ACCOUNT_ID, "account_number": "PA6SIM0000060", "status": "ACTIVE", "currency": "USD", "cash": equity, "buying_power": _money(Decimal(equity) * 2),
                        "equity": equity, "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False, "pattern_day_trader": False}
        self.is_open = True
        self.untradable: set = set()
        self.unknown_assets: set = set()
        self.prices: Dict[str, float] = dict(prices or {})
        self.positions: Dict[str, dict] = {}                  # symbol -> {qty, avg_entry_price}
        self.orders: Dict[str, dict] = {}                     # alpaca id -> order json (insertion order = time order)
        self.post_modes: List[str] = []
        self.auto_fill = "next_read"
        self.partial_first: Dict[str, int] = {}               # client_order_id -> first partial quantity
        self.lookup_lag = 0
        self.lookup_fail = 0
        self.read_fail_paths: List[str] = []                  # GET paths that raise a connection error once each
        self.requests: list = []                              # (method, path, query)
        self.posts: list = []                                 # raw POST bodies that reached the broker
        self.on_post: Optional[Callable] = None

    # ---------------------------------------------------------------- scripting -------------------------------------------------
    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol.upper()] = float(price)

    def price_of(self, symbol: str) -> Decimal:
        return Decimal(str(self.prices.get(symbol.upper(), 100.0)))

    def set_position(self, symbol: str, qty: int, avg_entry_price: float = 100.0) -> None:
        with self.lock:
            if qty <= 0:
                self.positions.pop(symbol.upper(), None)
            else:
                self.positions[symbol.upper()] = {"qty": int(qty), "avg_entry_price": Decimal(str(avg_entry_price))}

    def order_by_coid(self, coid: str) -> Optional[dict]:
        return next((o for o in self.orders.values() if o["client_order_id"] == coid), None)

    def set_status(self, coid: str, status: str, filled_qty: Optional[int] = None, filled_avg_price: Optional[float] = None) -> dict:
        with self.lock:
            o = self.order_by_coid(coid)
            o["status"], o["updated_at"] = status, iso(self.now_fn())
            if filled_qty is not None:
                o["filled_qty"] = str(int(filled_qty))
                o["filled_avg_price"] = _money(filled_avg_price) if filled_avg_price is not None else o["filled_avg_price"]
            return o

    def fill(self, coid: str, qty: Optional[int] = None, price: Optional[float] = None) -> dict:
        """Fill (or partially fill) an open order and move the position accordingly — the broker-side truth the client reads."""
        with self.lock:
            o = self.order_by_coid(coid)
            if o is None or o["status"] not in OPEN_STATUSES:
                return o
            total, done = int(o["qty"]), int(o["filled_qty"] or 0)
            add = min(total - done, int(qty) if qty is not None else total - done)
            if add <= 0:
                return o
            px = Decimal(str(price)) if price is not None else self.price_of(o["symbol"])
            prev_avg = Decimal(o["filled_avg_price"]) if o["filled_avg_price"] else Decimal("0")
            new_done = done + add
            o["filled_avg_price"] = _money((prev_avg * done + px * add) / new_done)
            o["filled_qty"] = str(new_done)
            o["status"] = "filled" if new_done >= total else "partially_filled"
            o["updated_at"] = iso(self.now_fn())
            if o["status"] == "filled":
                o["filled_at"] = o["updated_at"]
            self._apply_to_position(o["symbol"], o["side"], add, px)
            return o

    def _apply_to_position(self, symbol: str, side: str, qty: int, px: Decimal) -> None:
        p = self.positions.get(symbol)
        if side == "buy":
            if p is None:
                self.positions[symbol] = {"qty": qty, "avg_entry_price": px}
            else:
                tot = p["qty"] + qty
                p["avg_entry_price"] = (p["avg_entry_price"] * p["qty"] + px * qty) / tot
                p["qty"] = tot
        else:
            if p is not None:
                p["qty"] -= qty
                if p["qty"] <= 0:
                    self.positions.pop(symbol, None)

    def _maybe_auto_fill(self, o: dict) -> None:
        if self.auto_fill != "next_read" or o["status"] not in ("new", "accepted", "pending_new", "partially_filled"):
            return
        part = self.partial_first.pop(o["client_order_id"], None)
        self.fill(o["client_order_id"], part)

    # ---------------------------------------------------------------- the wire --------------------------------------------------
    def _resp(self, request, status, body):
        r = requests.Response()
        r.status_code, r.url, r.request = status, request.url, request
        r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        r.headers["Content-Type"] = "application/json"
        return r

    def _order_json(self, payload: dict, status: str = "accepted") -> dict:
        t = iso(self.now_fn())
        return {"id": str(uuid.uuid4()), "client_order_id": payload["client_order_id"], "created_at": t, "updated_at": t, "submitted_at": t, "filled_at": None,
                "symbol": payload["symbol"], "asset_class": "us_equity", "qty": payload["qty"], "filled_qty": "0", "filled_avg_price": None, "order_class": "",
                "order_type": payload["type"], "type": payload["type"], "side": payload["side"], "time_in_force": payload["time_in_force"], "status": status,
                "extended_hours": False, "notional": None, "limit_price": None, "stop_price": None}

    def _position_json(self, sym: str, p: dict) -> dict:
        px = self.price_of(sym)
        return {"asset_id": "sim", "symbol": sym, "exchange": "SIM", "asset_class": "us_equity", "qty": str(p["qty"]), "qty_available": str(p["qty"]), "side": "long",
                "avg_entry_price": _money(p["avg_entry_price"]), "market_value": _money(px * p["qty"]), "cost_basis": _money(p["avg_entry_price"] * p["qty"]),
                "unrealized_pl": _money((px - p["avg_entry_price"]) * p["qty"]), "current_price": _money(px)}

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        path, q = unquote(u.path), parse_qs(u.query)
        with self.lock:
            self.requests.append((request.method, path, u.query))
            if request.method == "POST":
                return self._post(request)
            if request.method != "GET":
                return self._resp(request, 405, {"code": 40500000, "message": "method not allowed"})
            if path in self.read_fail_paths:
                self.read_fail_paths.remove(path)
                raise requests.exceptions.ConnectionError("read failed (simulated)")
            if path == "/v2/account":
                return self._resp(request, 200, self.account)
            if path == "/v2/clock":
                now = self.now_fn()
                return self._resp(request, 200, {"timestamp": iso(now), "is_open": self.is_open, "next_open": iso(now + timedelta(hours=18)), "next_close": iso(now + timedelta(hours=4))})
            if path.startswith("/v2/assets/"):
                sym = path.rsplit("/", 1)[1]
                if sym in self.unknown_assets:
                    return self._resp(request, 404, {"code": 40410000, "message": "asset not found"})
                return self._resp(request, 200, {"id": "sim-" + sym, "class": "us_equity", "exchange": "SIM", "symbol": sym, "status": "active", "tradable": sym not in self.untradable,
                                                 "shortable": False, "fractionable": False})
            if path == "/v2/positions":
                return self._resp(request, 200, [self._position_json(s, p) for s, p in sorted(self.positions.items())])
            if path.startswith("/v2/positions/"):
                sym = path.rsplit("/", 1)[1]
                p = self.positions.get(sym)
                return self._resp(request, 200, self._position_json(sym, p)) if p else self._resp(request, 404, {"code": 40410000, "message": "position does not exist"})
            if path == "/v2/orders":
                status = (q.get("status") or ["open"])[0]
                syms = set((q.get("symbols") or [""])[0].split(",")) - {""}
                rows = list(self.orders.values())
                for o in rows:
                    self._maybe_auto_fill(o)
                if status == "open":
                    rows = [o for o in rows if o["status"] in OPEN_STATUSES]
                elif status == "closed":
                    rows = [o for o in rows if o["status"] not in OPEN_STATUSES]
                if syms:
                    rows = [o for o in rows if o["symbol"] in syms]
                limit = int((q.get("limit") or ["50"])[0])
                return self._resp(request, 200, list(reversed(rows))[:limit])
            if path == "/v2/orders:by_client_order_id" or path.startswith("/v2/orders/"):
                if self.lookup_fail:
                    self.lookup_fail -= 1
                    raise requests.exceptions.ConnectionError("lookup failed (simulated)")
                o = self.order_by_coid((q.get("client_order_id") or [""])[0]) if path.endswith(":by_client_order_id") else self.orders.get(path.rsplit("/", 1)[1])
                if o is None or self.lookup_lag > 0:
                    self.lookup_lag = max(0, self.lookup_lag - 1)
                    return self._resp(request, 404, {"code": 40410000, "message": "order not found"})
                self._maybe_auto_fill(o)
                return self._resp(request, 200, o)
            return self._resp(request, 404, {"code": 40410000, "message": "not found"})

    def _post(self, request):
        if self.on_post:
            self.on_post(request)
        mode = self.post_modes.pop(0) if self.post_modes else "accept"
        if mode == "connect_timeout":
            raise requests.exceptions.ConnectTimeout("connect timed out (simulated)")
        if mode == "dns":
            raise requests.exceptions.ConnectionError(MaxRetryError(None, request.url, NewConnectionError(None, "name resolution failed")))
        if mode == "timeout_before":
            raise requests.exceptions.ReadTimeout("read timed out (simulated)")
        self.posts.append(request.body)
        payload = json.loads(request.body)
        if self.order_by_coid(payload["client_order_id"]) is not None:
            return self._resp(request, 422, {"code": 40010001, "message": "client_order_id must be unique"})
        kind, _, arg = mode.partition(":")
        if kind == "accept":
            o = self._order_json(payload, arg or "accepted")
            self.orders[o["id"]] = o
            if arg == "filled":                                       # an immediate fill in the POST answer carries the fill fields, like Alpaca
                o["status"] = "accepted"
                self.fill(o["client_order_id"])
            return self._resp(request, 200, o)
        if kind == "reject":
            o = self._order_json(payload, "rejected")
            self.orders[o["id"]] = o
            return self._resp(request, 200, o)
        if kind in ("timeout_after_record", "reset_after_record", "garbage200_after_record", "http_after_record"):
            o = self._order_json(payload)
            self.orders[o["id"]] = o
            if kind == "timeout_after_record":
                raise requests.exceptions.ReadTimeout("read timed out (simulated)")
            if kind == "reset_after_record":
                raise requests.exceptions.ConnectionError("connection reset by peer (simulated)")
            if kind == "garbage200_after_record":
                return self._resp(request, 200, b"<html>gateway</html>")
            return self._resp(request, int(arg), {"code": 50000000, "message": "internal error"})
        if kind == "garbage200":
            return self._resp(request, 200, b"not json")
        if kind == "inconsistent200":
            return self._resp(request, 200, {**self._order_json(payload), "qty": str(int(payload["qty"]) + 1)})
        if kind == "http":
            status, _, body = arg.partition(":")
            return self._resp(request, int(status), body.encode() if body else {"code": 40000000, "message": "error"})
        raise AssertionError(f"unknown simulated POST mode {mode}")

    def close(self):
        pass

    # ---------------------------------------------------------------- conveniences ----------------------------------------------
    def gets(self) -> List[str]:
        return [p for m, p, _ in self.requests if m == "GET"]

    def post_count(self) -> int:
        return len(self.posts)
