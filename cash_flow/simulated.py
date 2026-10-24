"""
cash_flow/simulated.py - the deterministic SIMULATED activity feed for the simulated paper broker (execution/simulated.py has no
activities endpoint). `ActivityWire` sits BELOW the real cash-flow reader's transport: GET /v2/account/activities is answered here with
Alpaca's shape and paging semantics (newest first, page_size, page_token = the last id, `after` on the creation time, the
non_trade_activity category); every other request goes to the wrapped wire unchanged. Nothing here opens a socket.

Tests and the browser harness script deposits, withdrawals, journals, fees, dividends, interest, unknown types, failures (connection,
timeout, 429, 5xx, broken JSON) and late postings; `deposit()` / `withdraw()` also move the simulated account's cash and equity, like a
real transfer does.
"""
from __future__ import annotations

import json
import threading
import uuid
import weakref
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter

from cash_flow import ACTIVITIES_PATH

_FEEDS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_GUARD = threading.Lock()


def _iso_ms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ActivityFeed:
    def __init__(self, now_fn: Optional[Callable[[], datetime]] = None):
        self.lock = threading.RLock()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.rows: List[dict] = []
        self.fail: List[tuple] = []                    # consumed one per request: ("connection",) ("timeout",) ("status", 429|500, retry_after) ("body", bytes)
        self.requests: List[dict] = []
        self.ignore_category = False                   # behave like an API that ignores the category filter
        self._seq = 0

    # ---------------------------------------------------------------- scripting -------------------------------------------------
    def add(self, activity_type: str, net_amount, *, created_at: Optional[datetime] = None, date: Optional[str] = None, status: str = "executed",
            sub_type: Optional[str] = None, description: Optional[str] = None, currency: Optional[str] = "USD", activity_id: Optional[str] = None,
            no_created_at: bool = False, **extra) -> dict:
        with self.lock:
            at = created_at or self.now_fn()
            self._seq += 1
            d = date or at.astimezone(timezone.utc).date().isoformat()
            row = {"id": activity_id or f"{d.replace('-', '')}000000{self._seq:03d}::{uuid.uuid4()}", "activity_type": activity_type,
                   "date": d, "net_amount": None if net_amount is None else str(net_amount), "status": status, "currency": currency}
            if not no_created_at:
                row["created_at"] = _iso_ms(at)
            if sub_type:
                row["activity_sub_type"] = sub_type
            if description is not None:
                row["description"] = description
            row.update(extra)
            self.rows.append(row)
            return row

    def amend(self, activity_id: str, **changes) -> dict:
        with self.lock:
            for r in self.rows:
                if r["id"] == activity_id:
                    r.update(changes)
                    return r
        raise KeyError(activity_id)

    def fill_row(self, symbol: str = "AAPL", at: Optional[datetime] = None) -> dict:
        """A trade row (what the API returns if the category filter is ignored)."""
        t = at or self.now_fn()
        with self.lock:
            self._seq += 1
            row = {"id": f"{t.strftime('%Y%m%d%H%M%S')}{self._seq:03d}::{uuid.uuid4()}", "activity_type": "FILL", "transaction_time": _iso_ms(t), "type": "fill",
                   "price": "100", "qty": "1", "side": "buy", "symbol": symbol, "order_id": str(uuid.uuid4()), "order_status": "filled"}
            self.rows.append(row)
            return row

    # ---------------------------------------------------------------- the endpoint ----------------------------------------------
    def _sorted(self, q: Dict[str, List[str]]) -> List[dict]:
        rows = list(self.rows)
        cat = (q.get("category") or [""])[0]
        if cat == "non_trade_activity" and not self.ignore_category:
            rows = [r for r in rows if r.get("activity_type") != "FILL"]
        after = (q.get("after") or [""])[0]
        if after:
            cut = datetime.fromisoformat(after.replace("Z", "+00:00"))
            if cut.tzinfo is None:
                cut = cut.replace(tzinfo=timezone.utc)

            def ts(r):
                v = r.get("created_at") or r.get("transaction_time")
                return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.fromisoformat(r["date"] + "T00:00:00+00:00")
            rows = [r for r in rows if ts(r) > cut]
        desc = (q.get("direction") or ["desc"])[0] != "asc"
        return sorted(rows, key=lambda r: r["id"], reverse=desc)

    def answer(self, request) -> requests.Response:
        u = urlsplit(request.url)
        q = parse_qs(u.query)
        with self.lock:
            self.requests.append({"method": request.method, "path": unquote(u.path), "query": u.query})
            if self.fail:
                f = self.fail.pop(0)
                if f[0] == "connection":
                    raise requests.exceptions.ConnectionError("activities read failed (simulated)")
                if f[0] == "timeout":
                    raise requests.exceptions.ReadTimeout("activities read timed out (simulated)")
                if f[0] == "status":
                    r = self._resp(request, int(f[1]), {"code": int(f[1]) * 100000, "message": "simulated"})
                    if len(f) > 2 and f[2] is not None:
                        r.headers["Retry-After"] = str(f[2])
                    return r
                if f[0] == "body":
                    return self._resp(request, 200, f[1])
            if request.method != "GET":
                return self._resp(request, 405, {"code": 40500000, "message": "method not allowed"})
            rows = self._sorted(q)
            size = int((q.get("page_size") or ["100"])[0])
            token = (q.get("page_token") or [""])[0]
            if token:
                ids = [r["id"] for r in rows]
                rows = rows[ids.index(token) + 1:] if token in ids else []
            return self._resp(request, 200, [dict(r) for r in rows[:size]])

    @staticmethod
    def _resp(request, status, body):
        r = requests.Response()
        r.status_code, r.url, r.request = status, request.url, request
        r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        r.headers["Content-Type"] = "application/json"
        return r

    def activity_gets(self) -> int:
        return len([r for r in self.requests if r["method"] == "GET"])


class ActivityWire(BaseAdapter):
    """GET /v2/account/activities -> the feed; everything else -> the wrapped wire (the simulated paper broker)."""

    def __init__(self, inner, feed: ActivityFeed):
        super().__init__()
        self.inner, self.feed = inner, feed

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if unquote(urlsplit(request.url).path) == ACTIVITIES_PATH:
            return self.feed.answer(request)
        return self.inner.send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

    def close(self):
        pass


def feed_for(wire) -> ActivityFeed:
    """The activity feed attached to one simulated wire object (created on first use, with the wire's own clock when it has one)."""
    with _GUARD:
        f = _FEEDS.get(wire)
        if f is None:
            f = ActivityFeed(getattr(wire, "now_fn", None))
            _FEEDS[wire] = f
        return f


def move_cash(sim, amount) -> None:
    """A transfer moves the simulated account's cash and equity by `amount` (negative = out)."""
    with sim.lock:
        for k in ("cash", "equity"):
            sim.account[k] = str((Decimal(str(sim.account[k])) + Decimal(str(amount))).quantize(Decimal("0.01")))


def deposit(sim, amount, *, kind: str = "CSD", at: Optional[datetime] = None, move: bool = True, feed: Optional[ActivityFeed] = None) -> dict:
    feed = feed or feed_for(sim)
    if move:
        move_cash(sim, Decimal(str(amount)))
    return feed.add(kind, Decimal(str(amount)), created_at=at, description="Cash deposit (simulated)")


def withdraw(sim, amount, *, kind: str = "CSW", at: Optional[datetime] = None, move: bool = True, feed: Optional[ActivityFeed] = None) -> dict:
    feed = feed or feed_for(sim)
    if move:
        move_cash(sim, -Decimal(str(amount)))
    return feed.add(kind, -Decimal(str(amount)), created_at=at, description="Cash withdrawal (simulated)")
