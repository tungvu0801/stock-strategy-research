"""
cash_flow/reader.py - the ONE read of the broker activity feed (DESIGN_602 sections 1, 4). It reuses the Stage 6.0 paper client's
credentials and network adapter but has its own transport, which admits exactly one request shape:

    GET https://paper-api.alpaca.markets/v2/account/activities   (constant paper host; no other path, verb, scheme, host or port)

with a per-read request budget, no redirects, finite timeouts and no automatic retry (urllib3 max_retries 0; the service retries a whole
read a bounded number of times). Pagination is bounded: newest first, `page_size` rows, `page_token` = the last id, at most `max_pages`
pages; reaching the limit is reported as INCOMPLETE, never silently truncated. A repeated page aborts the read (no pagination loop).
Values of the credentials are never logged or returned. The deterministic simulated paper broker gets its activity feed through
cash_flow/simulated.py below this transport.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter

from cash_flow import ACTIVITIES_PATH, CATEGORY_FILTER, TRADE_TYPES
from execution import broker as BK

MAX_BODY = 4 * 1024 * 1024


class CashFlowRefused(RuntimeError):
    """Refused by this transport before anything was sent."""


class ReadFailure(Exception):
    def __init__(self, code: str, detail: str = "", result: str = "FAILED", retry_after: Optional[float] = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.result, self.retry_after = code, detail, result, retry_after


class _ActivityTransport(HTTPAdapter):
    def __init__(self, wire, *, budget: int):
        super().__init__(max_retries=0)
        self._wire, self.left = wire, int(budget)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        if u.scheme != "https" or u.hostname != BK.PAPER_HOST or u.port not in (None, 443):
            raise CashFlowRefused("only the Alpaca PAPER host is allowed")
        if request.method != "GET":
            raise CashFlowRefused("the cash-flow reader only reads")
        if unquote(u.path) != ACTIVITIES_PATH:
            raise CashFlowRefused(f"GET {unquote(u.path)} is not the activities path")
        if self.left <= 0:
            raise CashFlowRefused("read budget exhausted")
        self.left -= 1
        return self._wire.send(request, stream=stream, timeout=BK.TIMEOUT, verify=True, cert=cert, proxies=proxies)

    def close(self):
        try:
            self._wire.close()
        except Exception:  # noqa: BLE001
            pass


def _retry_after(r) -> Optional[float]:
    try:
        v = float(r.headers.get("Retry-After"))
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


class ActivityReader:
    def __init__(self, broker, *, wire_fn=None, page_size: int = 100, max_pages: int = 20):
        self.broker = broker
        self.wire_fn = wire_fn or broker.wire                     # cash_flow.service supplies the simulated feed wrapper for a simulated client
        self.page_size = max(1, min(100, int(page_size)))
        self.max_pages = max(1, int(max_pages))
        self.requests = 0

    def _get(self, http: requests.Session, params: Dict[str, Any]):
        try:
            headers = self.broker._headers()
        except BK.BrokerError as exc:
            raise ReadFailure(exc.code, exc.detail) from None
        self.requests += 1
        try:
            r = http.get(BK.BASE + ACTIVITIES_PATH, params=params, headers=headers, allow_redirects=False, timeout=BK.TIMEOUT)
        except CashFlowRefused as exc:
            raise ReadFailure("BLOCKED", str(exc)) from None
        except requests.exceptions.Timeout:
            raise ReadFailure("TIMEOUT") from None
        except requests.exceptions.RequestException as exc:
            raise ReadFailure("CONNECTION", type(exc).__name__) from None
        if r.status_code == 429:
            raise ReadFailure("RATE_LIMITED", "HTTP 429", retry_after=_retry_after(r))
        if r.status_code != 200:
            raise ReadFailure(f"HTTP_{r.status_code}", f"HTTP {r.status_code}")
        content = r.content or b""
        if len(content) > MAX_BODY:
            raise ReadFailure("UNREADABLE", "response too large")
        try:
            return json.loads(content)
        except ValueError:
            raise ReadFailure("UNREADABLE", "not JSON") from None

    def read(self, after_iso: str) -> Dict[str, Any]:
        """All non-trade activities created after `after_iso`, newest first, within the page limit."""
        transport = _ActivityTransport(self.wire_fn(), budget=self.max_pages)
        http = requests.Session()
        http.mount("https://", transport)
        http.mount("http://", transport)
        rows: List[dict] = []
        seen: set = set()
        token, pages, skipped = None, 0, 0
        try:
            while pages < self.max_pages:
                params = {"category": CATEGORY_FILTER, "direction": "desc", "page_size": self.page_size, "after": after_iso}
                if token:
                    params["page_token"] = token
                body = self._get(http, params)
                pages += 1
                if not isinstance(body, list):
                    raise ReadFailure("INVALID_PAYLOAD", "activities is not a list", "INVALID")
                fresh = 0
                for a in body:
                    if not isinstance(a, dict) or not a.get("id") or not isinstance(a.get("id"), str):
                        raise ReadFailure("INVALID_PAYLOAD", "an activity has no id", "INVALID")
                    if a["id"] in seen:
                        continue
                    seen.add(a["id"])
                    fresh += 1
                    if str(a.get("activity_type") or "").upper() in TRADE_TYPES:
                        skipped += 1                                   # the category filter was ignored: trade rows never enter the ledger
                        continue
                    rows.append(a)
                if body and not fresh:
                    raise ReadFailure("INVALID_PAYLOAD", "pagination returned a page already read", "INVALID")
                if len(body) < self.page_size:
                    return {"rows": rows, "pages": pages, "complete": True, "skipped_trade_rows": skipped}
                token = body[-1]["id"]
            return {"rows": rows, "pages": pages, "complete": False, "skipped_trade_rows": skipped}
        finally:
            transport.close()
