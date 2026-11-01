"""
market_intel/sources/alpaca_bars.py - Stage 7.1: Alpaca historical bars -> Stage 7.0 Observation envelopes. PARSE-ONLY.

There is no network code here: request_params() builds the query a caller would send, and parse_page() turns a response body into
observations. The caller injects the transport (ingest.run_backfill takes a fetch function); Stage 7.1 ships no real one, so no Alpaca
request can be made by this stage (a real backfill needs separate operator approval, a SIP request its own).

Mapping (Alpaca market-data v2 bars: {"bars": [{"t", "o", "h", "l", "c", "v", "n", "vw"}], "symbol", "next_page_token"}):
* observed_at = the bar's start time "t"; session_date = its New York date; daily bars are session REG, intraday bars carry the session
  of their start minute.
* payload = open, high, low, close, volume, trade_count, vwap and the timeframe; nothing is filled, rounded or adjusted here.
* backfilled = True and available_at = retrieved_at (Stage 7.0): a backfilled bar is never presented as known at its market time.
* a row missing a required field, with an unparsable time, with a time outside the requested [start_date, end_date] sessions
  (OUT_OF_RANGE) or that the envelope refuses, is NOT stored (no zero, no guess): it is reported as a problem (index + code), the run
  records it as a REJECTED dataset member and the dataset can only end PARTIAL. One bad row never stops the rest of the page.
* the feed and the adjustment requested are recorded on every row; IEX and SIP rows never share a dataset or a natural key.

Response validation (fail closed; the exact shape to re-verify on the first approved run, DESIGN_71 section 6). An HTTP 200 body is a
page only when it is a JSON object with exactly the keys bars / symbol / next_page_token (currency allowed), bars a list or null, symbol
the requested one, next_page_token null or a plain token string, at most PAGE_LIMIT bars. An error envelope ({"message": ...} or
{"code": ...}) is ERROR_ENVELOPE; anything else is UNEXPECTED_SHAPE / SYMBOL_MISMATCH / BAD_PAGE_TOKEN / PAGE_TOO_LARGE - a page problem,
so the fetch is INVALID_RESPONSE and the dataset can never be EMPTY because of it. A bar is a JSON object of the known keys only, with
a string time and numeric (never boolean, never string) o / h / l / c / v (n and vw numeric or null), else MALFORMED_ROW.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from market_intel import calendar as CAL
from market_intel import validate as VA
from market_intel.models import Observation

PROVIDER = "ALPACA"
ENDPOINT = "/v2/stocks/{symbol}/bars"
TIMEFRAMES: Dict[str, str] = {"1Day": "BAR_1DAY", "1Hour": "BAR_1HOUR", "1Min": "BAR_1MIN"}
FEEDS = ("iex", "sip")
ADJUSTMENTS = ("raw", "split", "dividend", "all")
PAGE_LIMIT = 10_000                      # Alpaca's documented maximum bars per page (design C.9: verify on the first approved run)
REQUIRED = ("t", "o", "h", "l", "c", "v")
BAR_KEYS = frozenset((*REQUIRED, "n", "vw"))
PAGE_KEYS_REQUIRED = frozenset(("bars", "symbol", "next_page_token"))
PAGE_KEYS_ALLOWED = PAGE_KEYS_REQUIRED | {"currency"}
_TOKEN = re.compile("^[A-Za-z0-9+/=_.:-]{1,512}$")


def request_params(symbol: str, timeframe: str, start: datetime, end: datetime, feed: str, adjustment: str,
                   page_token: Optional[str] = None) -> dict:
    """The query of one page - never a key or a secret (the transport adds its own headers)."""
    if timeframe not in TIMEFRAMES or feed not in FEEDS or adjustment not in ADJUSTMENTS:
        raise ValueError("timeframe, feed and adjustment must be explicit and known")
    p = {"symbol": symbol.upper(), "timeframe": timeframe, "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
         "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "feed": feed, "adjustment": adjustment, "limit": PAGE_LIMIT,
         "sort": "asc"}
    if page_token:
        p["page_token"] = page_token
    return p


def _t(s) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else None


PAGE_PROBLEMS = ("INVALID_JSON", "UNEXPECTED_SHAPE", "ERROR_ENVELOPE", "SYMBOL_MISMATCH", "BAD_PAGE_TOKEN", "PAGE_TOO_LARGE")   # the whole page is unusable


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _bar_ok(b) -> bool:
    return (isinstance(b, dict) and set(b) <= BAR_KEYS and all(k in b for k in REQUIRED) and isinstance(b["t"], str)
            and all(_number(b[k]) for k in ("o", "h", "l", "c", "v")) and all(b.get(k) is None or _number(b[k]) for k in ("n", "vw")))


def parse_page(body: bytes, *, symbol: str, timeframe: str, feed: str, adjustment: str, retrieved_at: datetime,
               dataset_id: Optional[str] = None, start_date: Optional[date] = None,
               end_date: Optional[date] = None) -> Tuple[List[Observation], Optional[str], List[dict]]:
    """(observations, next_page_token, problems). A body that is not the expected JSON is one page problem and no rows."""
    kind = TIMEFRAMES[timeframe]
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return [], None, [{"problem": "INVALID_JSON"}]
    if isinstance(doc, dict) and ("message" in doc or "code" in doc):
        return [], None, [{"problem": "ERROR_ENVELOPE"}]
    if not isinstance(doc, dict) or not PAGE_KEYS_REQUIRED <= set(doc) or not set(doc) <= PAGE_KEYS_ALLOWED \
            or not isinstance(doc["bars"], (list, type(None))):
        return [], None, [{"problem": "UNEXPECTED_SHAPE"}]
    if doc["symbol"] != symbol.upper():
        return [], None, [{"problem": "SYMBOL_MISMATCH", "got": str(doc["symbol"])[:12]}]
    token = doc["next_page_token"]
    if token is not None and not (isinstance(token, str) and _TOKEN.match(token)):
        return [], None, [{"problem": "BAD_PAGE_TOKEN"}]
    bars = doc["bars"] or []
    if len(bars) > PAGE_LIMIT:
        return [], None, [{"problem": "PAGE_TOO_LARGE", "bars": len(bars)}]
    out: List[Observation] = []
    problems: List[dict] = []
    for i, b in enumerate(bars):
        if not _bar_ok(b):
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        t = _t(b["t"])
        if t is None:
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        try:
            s = CAL.session(t)
        except Exception:                                           # noqa: BLE001 - a time the calendar cannot classify is a problem row
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        sdate: date = s["session_date"]
        if sdate is None or (start_date is not None and sdate < start_date) or (end_date is not None and sdate > end_date):
            problems.append({"problem": "OUT_OF_RANGE", "index": i})
            continue
        payload = {"open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"], "trade_count": b.get("n"),
                   "vwap": b.get("vw"), "timeframe": timeframe}
        try:
            flags = VA.bar_codes({"open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]})
            flags += VA.time_codes(t, retrieved_at, backfilled=True)
            out.append(Observation(kind=kind, symbol=symbol, provider=PROVIDER, feed=feed, adjustment=adjustment, observed_at=t,
                                   retrieved_at=retrieved_at, available_at=retrieved_at, session="REG" if kind == "BAR_1DAY" else s["session"],
                                   session_date=sdate, status="AVAILABLE", dataset_id=dataset_id,   # identity: kind, symbol, feed, adjustment, time
                                   quality_flags=tuple(sorted(set(flags))), backfilled=True, payload=payload))
        except (ValueError, TypeError) as exc:                      # pydantic: a value the envelope refuses (e.g. not finite)
            problems.append({"problem": "REFUSED_BY_ENVELOPE", "index": i, "detail": str(exc)[:160]})
    return out, token, problems
