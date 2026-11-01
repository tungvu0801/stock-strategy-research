"""
market_intel/sources/common.py - Stage 7.2: the shared, pure helpers of the event and reference adapters (DESIGN_72 section 3).

Every Stage 7.2 adapter is PARSE-ONLY, like Stage 7.1's alpaca_bars: it builds the request a caller would send (never a key or a
secret) and turns a saved response body into Stage 7.0 Observation envelopes. No network code exists here or in any adapter; Stage
7.2 ships no transport (a future, approved stage passes bodies fetched through evidence.net.fetch).

Shared rules (each a test):
* a parser NEVER raises: any exception becomes the page problem PARSER_ERROR:<type>, and the page yields no rows;
* time is explicit: provider times must carry an offset or a Z (a naive time is a BAD_TIME row); New York wall-clock times (halts,
  dates) are converted with zoneinfo; a date-only fact is placed at New York midnight and says so (time_precision DATE_ONLY);
* availability is ours, never the provider's: available_at = retrieved_at (the first time we saw the row), so nothing is presented as
  known before we had it - the Stage 7.0 invariant the store enforces again;
* a request names the credential its transport must add (e.g. ALPHAVANTAGE_API_KEY) but never contains it; without the credential the
  request is NOT_CONFIGURED and nothing could be sent.
"""
from __future__ import annotations

import functools
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from market_intel import calendar as CAL

NY = "America/New_York"
PAGE_PROBLEMS = ("INVALID_JSON", "INVALID_XML", "INVALID_CSV", "UNSAFE_XML", "UNEXPECTED_SHAPE", "ERROR_ENVELOPE", "SYMBOL_MISMATCH",
                 "BAD_PAGE_TOKEN", "PAGE_TOO_LARGE", "PARSER_ERROR")              # the whole page is unusable: the fetch is INVALID_RESPONSE
TOKEN = re.compile("^[A-Za-z0-9+/=_.:-]{1,512}$")
READY, NOT_CONFIGURED = "READY", "NOT_CONFIGURED"
Parsed = Tuple[list, Optional[str], List[dict]]                                   # (observations, next page token, problems)


def page_problem(code: str, **detail) -> Parsed:
    return [], None, [{"problem": code, **detail}]


def never_raises(fn: Callable[..., Parsed]) -> Callable[..., Parsed]:
    """A parser that raises returns PARSER_ERROR instead (C.3: an adapter never raises)."""
    @functools.wraps(fn)
    def wrapper(*a, **k) -> Parsed:
        try:
            return fn(*a, **k)
        except Exception as exc:                                         # noqa: BLE001 - a parser bug is a page problem, never a crash
            return page_problem(f"PARSER_ERROR:{type(exc).__name__}")
    return wrapper


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def utc_time(v) -> Optional[datetime]:
    """An RFC 3339 provider time WITH an offset or Z -> aware UTC (nanoseconds truncated to microseconds); anything else -> None."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo is not None and d.tzinfo.utcoffset(d) is not None else None


def iso_date(v, fmt: str = "%Y-%m-%d") -> Optional[date]:
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(v.strip(), fmt).date()
    except ValueError:
        return None


def ny_datetime(d: date, t: time = time(0, 0)) -> datetime:
    """A New York wall-clock date and time -> aware UTC (zoneinfo: DST handled)."""
    from zoneinfo import ZoneInfo
    return datetime.combine(d, t, tzinfo=ZoneInfo(NY)).astimezone(timezone.utc)


def ny_date(t: datetime) -> date:
    from zoneinfo import ZoneInfo
    return t.astimezone(ZoneInfo(NY)).date()


def session_fields(t: datetime) -> Tuple[str, Optional[date]]:
    """(session, New York session date) of a time: what the store derives again for session-bound kinds."""
    s = CAL.session(t)
    return s["session"], s["session_date"]


def load_json(body) -> Tuple[Any, Optional[str]]:
    try:
        return json.loads(body), None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None, "INVALID_JSON"


def request(provider: str, host: str, endpoint: str, params: Dict[str, Any], *, credential: Optional[str] = None,
            configured: bool = True, requires: Tuple[str, ...] = (), reason: str = "") -> dict:
    """What a transport WOULD send: host, endpoint and parameters (never a credential), the credential it must add by NAME, and the
    approvals a real request needs. NOT_CONFIGURED when the credential is missing - nothing could be sent."""
    for k, v in params.items():
        if any(w in str(k).lower() for w in ("key", "token", "secret", "password")) and k not in ("page_token",):
            raise ValueError(f"a request never carries a credential parameter ({k})")
        if isinstance(v, str) and any(w in v.lower() for w in ("apikey=", "api_key=", "secret")):
            raise ValueError("a request never carries a credential value")
    return {"provider": provider, "host": host, "endpoint": endpoint, "params": dict(params), "credential": credential,
            "status": READY if configured else NOT_CONFIGURED, "reason": "" if configured else (reason or f"{credential} is not configured"),
            "requires_approval": list(requires), "network": False}


def market_sessions(first: date, last: date) -> Optional[List[str]]:
    """The market days of [first, last] (ISO) - or None when any weekday in it is outside the verified calendar (never guessed)."""
    out, x = [], first
    while x <= last:
        st = CAL.market_day_status(x)
        if st == CAL.NOT_COVERED:
            return None
        if st == CAL.MARKET_DAY:
            out.append(x.isoformat())
        x += timedelta(days=1)
    return out
