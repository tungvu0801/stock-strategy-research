"""
market_intel/validate.py - deterministic validation (design C.6). Each check returns quality codes; nothing is ever repaired, dropped or
re-ordered here. A later stage stores the codes beside the value (models.Observation.quality_flags) and decides what a code blocks.

Codes (QUALITY_CODES is the closed set the envelope accepts):
  bars    NON_FINITE, NON_POSITIVE_PRICE, NEGATIVE_VOLUME, HIGH_BELOW_BODY, LOW_ABOVE_BODY, NOT_INCREASING, DUPLICATE_TS, OUTSIDE_SESSION,
          GAP_MINUTES, SESSION_HOLE (a hole of >= 2 market days blocks a daily dataset, the Stage 3.2 rule), DAY_JUMP (> 25 %, the
          Stage 6.6 NORMAL_DAY: a corporate action to classify with the split-integrity contract, never "fixed" here)
  quotes  NON_POSITIVE_QUOTE, CROSSED, LOCKED, WIDE_SPREAD, STALE_PRINT
  feeds   FEED_DIVERGENCE (IEX vs SIP close > 10 bps: a report, never a correction), CLOCK_SKEW
  news    EMPTY_HEADLINE, MISSING_PUBLISHED, FUTURE_DATED, DENY_HOST_LINK (stored as metadata only)
  events  UNCONFIRMED_PAST (an earnings date in the past without an 8-K item 2.02), HALTED_OPEN (a halt without a resumption time)
  time    FUTURE_OBSERVED (a provider time after our retrieval, beyond the skew), BACKFILLED (a row fetched after the fact)
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from market_intel import CLOSED
from market_intel import calendar as CAL

QUALITY_CODES = frozenset((
    "NON_FINITE", "NON_POSITIVE_PRICE", "NEGATIVE_VOLUME", "HIGH_BELOW_BODY", "LOW_ABOVE_BODY", "NOT_INCREASING", "DUPLICATE_TS", "OUTSIDE_SESSION",
    "GAP_MINUTES", "SESSION_HOLE", "DAY_JUMP",
    "NON_POSITIVE_QUOTE", "CROSSED", "LOCKED", "WIDE_SPREAD", "STALE_PRINT",
    "FEED_DIVERGENCE", "CLOCK_SKEW",
    "EMPTY_HEADLINE", "MISSING_PUBLISHED", "FUTURE_DATED", "DENY_HOST_LINK",
    "UNCONFIRMED_PAST", "HALTED_OPEN",
    "FUTURE_OBSERVED", "BACKFILLED",
))
BLOCKING_CODES = frozenset(("NON_FINITE", "NON_POSITIVE_PRICE", "HIGH_BELOW_BODY", "LOW_ABOVE_BODY", "NOT_INCREASING", "DUPLICATE_TS",
                            "SESSION_HOLE", "NON_POSITIVE_QUOTE", "CROSSED", "FUTURE_DATED", "FUTURE_OBSERVED"))   # never usable as-is

# pre-registered thresholds (pinned by tests)
DAY_JUMP = 0.25                     # mean_reversion.NORMAL_DAY (pinned equal by a test)
FEED_DIVERGENCE_BPS = 10.0
WIDE_SPREAD_BPS = 50.0              # a quote whose spread is wider than 0.5 % of the mid
STALE_PRINT_S = 60.0                # the last trade is older than the quote by more than this
NEWS_SKEW_S = 300.0                 # published_at may lead retrieved_at by at most 5 minutes (clock skew)
CLOCK_SKEW_S = 2.0
SESSION_HOLE_DAYS = 2


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def bar_codes(bar: Dict[str, Any]) -> List[str]:
    """One OHLCV bar: {open, high, low, close, volume}."""
    o, h, l_, c, v = (_num(bar.get(k)) for k in ("open", "high", "low", "close", "volume"))
    if None in (o, h, l_, c, v):
        return ["NON_FINITE"]
    out = []
    if min(o, h, l_, c) <= 0:
        out.append("NON_POSITIVE_PRICE")
    if v < 0:
        out.append("NEGATIVE_VOLUME")
    if h < max(o, c):
        out.append("HIGH_BELOW_BODY")
    if l_ > min(o, c):
        out.append("LOW_ABOVE_BODY")
    return out


def minute_series_codes(bars: Sequence[Dict[str, Any]], *, regular_only: bool = True) -> Dict[str, Any]:
    """Minute bars of one symbol and one dataset: {"codes", "gap_minutes", "per_bar"}. `ts` is the bar start (aware datetime)."""
    codes: List[str] = []
    per_bar: List[List[str]] = []
    seen = set()
    prev: Optional[datetime] = None
    gaps = 0
    for b in bars:
        ts = _utc(b["ts"])
        c = bar_codes(b)
        if ts in seen:
            c.append("DUPLICATE_TS")
        elif prev is not None and ts < prev:
            c.append("NOT_INCREASING")
        s = CAL.session(ts)["session"]
        if s == CLOSED or (regular_only and s != "REG"):
            c.append("OUTSIDE_SESSION")
        if prev is not None and ts > prev and CAL.session(ts)["session_date"] == CAL.session(prev)["session_date"]:
            gaps += max(0, int((ts - prev).total_seconds() // 60) - 1)
        seen.add(ts)
        prev = ts if prev is None or ts > prev else prev
        per_bar.append(c)
        codes.extend(c)
    if gaps:
        codes.append("GAP_MINUTES")
    return {"codes": sorted(set(codes)), "gap_minutes": gaps, "per_bar": per_bar}


def daily_series_codes(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Daily bars of one symbol (`day` is a date): {"codes", "holes", "jumps", "blocked"}. A hole of >= 2 market days blocks the dataset."""
    codes: List[str] = []
    holes: List[dict] = []
    jumps: List[str] = []
    prev = None
    for b in bars:
        d = b["day"]
        codes.extend(bar_codes(b))
        if prev is not None:
            if d <= prev["day"]:
                codes.append("DUPLICATE_TS" if d == prev["day"] else "NOT_INCREASING")
            else:
                missing, x = 0, CAL.next_market_day(prev["day"])
                while x < d:
                    missing += 1
                    x = CAL.next_market_day(x)
                if missing >= SESSION_HOLE_DAYS:
                    holes.append({"after": prev["day"].isoformat(), "before": d.isoformat(), "market_days": missing})
                pc, cc = _num(prev.get("close")), _num(b.get("close"))
                if pc and cc and abs(cc / pc - 1.0) > DAY_JUMP:
                    jumps.append(d.isoformat())
        prev = b
    if holes:
        codes.append("SESSION_HOLE")
    if jumps:
        codes.append("DAY_JUMP")
    return {"codes": sorted(set(codes)), "holes": holes, "jumps": jumps, "blocked": bool(holes) or bool(set(codes) & BLOCKING_CODES)}


def quote_codes(q: Dict[str, Any], *, wide_spread_bps: float = WIDE_SPREAD_BPS) -> List[str]:
    """A quote snapshot: {bid, ask, quote_at, last_trade_at?}."""
    bid, ask = _num(q.get("bid")), _num(q.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return ["NON_POSITIVE_QUOTE"]
    out = []
    if ask < bid:
        out.append("CROSSED")
    elif ask == bid:
        out.append("LOCKED")
    else:
        mid = (ask + bid) / 2.0
        if (ask - bid) / mid * 10_000 > wide_spread_bps:
            out.append("WIDE_SPREAD")
    qa, ta = q.get("quote_at"), q.get("last_trade_at")
    if qa is not None and ta is not None and (_utc(qa) - _utc(ta)).total_seconds() > STALE_PRINT_S:
        out.append("STALE_PRINT")
    return out


def feed_divergence(iex_close: float, sip_close: float) -> List[str]:
    a, b = _num(iex_close), _num(sip_close)
    if a is None or b is None or b <= 0:
        return ["NON_FINITE"]
    return ["FEED_DIVERGENCE"] if abs(a - b) / b * 10_000 > FEED_DIVERGENCE_BPS else []


def clock_skew(server_date: Optional[datetime], local: datetime) -> List[str]:
    """The HTTP Date header of a response against our clock (design C.7)."""
    if server_date is None:
        return []
    return ["CLOCK_SKEW"] if abs((_utc(server_date) - _utc(local)).total_seconds()) > CLOCK_SKEW_S else []


def time_codes(observed_at: Optional[datetime], retrieved_at: datetime, *, backfilled: bool = False) -> List[str]:
    out = []
    if observed_at is not None and (_utc(observed_at) - _utc(retrieved_at)).total_seconds() > CLOCK_SKEW_S:
        out.append("FUTURE_OBSERVED")
    if backfilled:
        out.append("BACKFILLED")
    return out


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def news_codes(item: Dict[str, Any], deny_hosts: Iterable[str]) -> List[str]:
    """An article: {headline, published_at?, retrieved_at, url?}. A deny-list link is metadata only (never fetched, never quoted)."""
    out = []
    if not str(item.get("headline") or "").strip():
        out.append("EMPTY_HEADLINE")
    pub, ret = item.get("published_at"), item.get("retrieved_at")
    if pub is None:
        out.append("MISSING_PUBLISHED")
    elif ret is not None and (_utc(pub) - _utc(ret)).total_seconds() > NEWS_SKEW_S:
        out.append("FUTURE_DATED")
    host = _host(str(item.get("url") or ""))
    if host and any(host == d or host.endswith("." + d) for d in (str(x).strip().lower() for x in deny_hosts) if d):
        out.append("DENY_HOST_LINK")
    return out


def earnings_codes(event_day: date, today: date, *, confirmed_by_8k_202: bool) -> List[str]:
    return ["UNCONFIRMED_PAST"] if event_day < today and not confirmed_by_8k_202 else []


def halt_codes(halt: Dict[str, Any]) -> List[str]:
    """A trading halt: {halted_at, resumed_at?}."""
    return ["HALTED_OPEN"] if halt.get("halted_at") is not None and halt.get("resumed_at") is None else []


def blocking(codes: Iterable[str]) -> bool:
    return bool(set(codes) & BLOCKING_CODES)
