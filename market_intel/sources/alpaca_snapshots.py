"""
market_intel/sources/alpaca_snapshots.py - Stage 7.2: Alpaca stock snapshots -> QUOTE_SNAPSHOT observations. PARSE-ONLY (DESIGN_72 3.2).

`GET /v2/stocks/snapshots?symbols=A,B&feed=iex|sip`: one request returns, per symbol, the latest trade, the latest quote, the minute
bar, the daily bar and the previous daily bar. The feed is ALWAYS explicit and recorded on every row: IEX and SIP snapshots are
different facts (different natural keys) and are never mixed.

Mapping:
* observed_at = the latest QUOTE's time; session and session date are derived from it (the store derives them again);
* payload: bid / ask with sizes, quote time, last trade price / size / time, and the three embedded bars as they were reported (a
  snapshot is a point-in-time view: its bars are NOT ingested as bar facts - Stage 7.1's bar backfill owns bars);
* quality codes: validate.quote_codes (NON_POSITIVE_QUOTE, CROSSED, LOCKED, WIDE_SPREAD, STALE_PRINT) and validate.time_codes
  (FUTURE_OBSERVED when the quote is after our retrieval);
* a requested symbol the response omits (or returns as null) is an ABSENCE: status UNAVAILABLE, observed_at = our retrieval time (the
  moment we looked), no payload - a missing quote is never a zero and never the old quote.

Response validation (fail closed): an object whose keys are requested symbols only (another symbol: SYMBOL_MISMATCH); an error envelope
is ERROR_ENVELOPE; a snapshot is an object of the known parts with a valid latestQuote (numeric prices and sizes, an RFC 3339 time
with an offset), else MALFORMED_ROW. The response shape is verified on the first approved run (DESIGN_72 section 6).
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional, Sequence

from market_intel import validate as VA
from market_intel.models import Observation
from market_intel.sources import common as C

PROVIDER = "ALPACA"
HOST = "data.alpaca.markets"
ENDPOINT = "/v2/stocks/snapshots"
KIND = "QUOTE_SNAPSHOT"
FEEDS = ("iex", "sip")
MAX_SYMBOLS = 100                       # self-imposed per request (the provider's own limit: verify)
PARTS = frozenset(("latestTrade", "latestQuote", "minuteBar", "dailyBar", "prevDailyBar"))
QUOTE_KEYS = frozenset(("t", "ap", "as", "ax", "bp", "bs", "bx", "c", "z"))
TRADE_KEYS = frozenset(("t", "p", "s", "x", "c", "i", "z"))
BAR_KEYS = frozenset(("t", "o", "h", "l", "c", "v", "n", "vw"))


def request(symbols: Sequence[str], feed: str) -> dict:
    syms = sorted({str(s).upper() for s in symbols})
    if feed not in FEEDS:
        raise ValueError("a snapshot request names its feed explicitly: iex or sip (feeds are never mixed)")
    if not syms or len(syms) > MAX_SYMBOLS:
        raise ValueError(f"1-{MAX_SYMBOLS} symbols")
    return C.request(PROVIDER, HOST, ENDPOINT, {"symbols": ",".join(syms), "feed": feed}, credential="ALPACA_API_KEY",
                     requires=("REAL_FETCH",) + (("SIP_FEED",) if feed == "sip" else ()))


def _quote(q) -> Optional[dict]:
    if not (isinstance(q, dict) and {"t", "ap", "as", "bp", "bs"} <= set(q) <= QUOTE_KEYS):
        return None
    t = C.utc_time(q["t"])
    if t is None or not all(C.is_number(q[k]) for k in ("ap", "as", "bp", "bs")):
        return None
    return {"bid": q["bp"], "ask": q["ap"], "bid_size": q["bs"], "ask_size": q["as"], "quote_at": t}


def _trade(x) -> Optional[dict]:
    if x is None:
        return None
    if not (isinstance(x, dict) and {"t", "p", "s"} <= set(x) <= TRADE_KEYS and C.is_number(x["p"]) and C.is_number(x["s"])):
        raise ValueError("trade")
    t = C.utc_time(x["t"])
    if t is None:
        raise ValueError("trade time")
    return {"last_price": x["p"], "last_size": x["s"], "last_trade_at": t}


def _bar(b) -> Optional[dict]:
    if b is None:
        return None
    if not (isinstance(b, dict) and {"t", "o", "h", "l", "c", "v"} <= set(b) <= BAR_KEYS and all(C.is_number(b[k]) for k in ("o", "h", "l", "c", "v"))
            and C.utc_time(b["t"]) is not None and all(b.get(k) is None or C.is_number(b[k]) for k in ("n", "vw"))):
        raise ValueError("bar")
    return {"start": C.utc_time(b["t"]).isoformat(), "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"],
            "trade_count": b.get("n"), "vwap": b.get("vw")}


@C.never_raises
def parse(body, *, symbols: Iterable[str], feed: str, retrieved_at: datetime, dataset_id: Optional[str] = None) -> C.Parsed:
    if feed not in FEEDS:
        return C.page_problem("UNEXPECTED_SHAPE", detail="feed")
    doc, bad = C.load_json(body)
    if bad:
        return C.page_problem(bad)
    if isinstance(doc, dict) and ("message" in doc or "code" in doc):
        return C.page_problem("ERROR_ENVELOPE")
    if not isinstance(doc, dict):
        return C.page_problem("UNEXPECTED_SHAPE")
    wanted = sorted({str(s).upper() for s in symbols})
    if not set(doc) <= set(wanted):
        return C.page_problem("SYMBOL_MISMATCH", got=sorted(set(doc) - set(wanted))[:5])
    out: List[Observation] = []
    problems: List[dict] = []
    for sym in wanted:
        snap = doc.get(sym)
        if snap is None:                                                   # we looked, and the provider has nothing: an absence
            session, sdate = C.session_fields(retrieved_at)
            out.append(Observation(kind=KIND, symbol=sym, provider=PROVIDER, feed=feed, adjustment="none", observed_at=retrieved_at,
                                   retrieved_at=retrieved_at, available_at=retrieved_at, session=session, session_date=sdate, status="UNAVAILABLE",
                                   dataset_id=dataset_id, payload={}))
            continue
        try:
            if not (isinstance(snap, dict) and set(snap) <= PARTS):
                raise ValueError("parts")
            q = _quote(snap.get("latestQuote"))
            if q is None:
                raise ValueError("quote")
            tr = _trade(snap.get("latestTrade")) or {"last_price": None, "last_size": None, "last_trade_at": None}
            bars = {name: _bar(snap.get(part)) for name, part in (("minute_bar", "minuteBar"), ("daily_bar", "dailyBar"), ("prev_daily_bar", "prevDailyBar"))}
        except ValueError:
            problems.append({"problem": "MALFORMED_ROW", "symbol": sym})
            continue
        flags = VA.quote_codes({"bid": q["bid"], "ask": q["ask"], "quote_at": q["quote_at"], "last_trade_at": tr["last_trade_at"]})
        flags += VA.time_codes(q["quote_at"], retrieved_at)
        session, sdate = C.session_fields(q["quote_at"])
        try:
            out.append(Observation(kind=KIND, symbol=sym, provider=PROVIDER, feed=feed, adjustment="none", observed_at=q["quote_at"],
                                   retrieved_at=retrieved_at, available_at=retrieved_at, session=session, session_date=sdate, status="AVAILABLE",
                                   dataset_id=dataset_id, quality_flags=tuple(sorted(set(flags))),
                                   payload={"bid": q["bid"], "ask": q["ask"], "bid_size": q["bid_size"], "ask_size": q["ask_size"],
                                            "quote_at": q["quote_at"].isoformat(), "last_price": tr["last_price"], "last_size": tr["last_size"],
                                            "last_trade_at": tr["last_trade_at"].isoformat() if tr["last_trade_at"] else None, **bars}))
        except (ValueError, TypeError) as exc:
            problems.append({"problem": "REFUSED_BY_ENVELOPE", "symbol": sym, "detail": str(exc)[:160]})
    return out, None, problems
