"""
market_intel/sources/halts.py - Stage 7.2: the Nasdaq Trader Trade Halt RSS feed -> HALT observations. PARSE-ONLY (DESIGN_72 3.6).

The feed is free, covers Nasdaq-listed and other exchange-listed securities, and asks not to be queried more than once a minute (design
C.8). Its terms must be accepted before any real request (an approval). Times in the feed are New York wall clock.

Mapping:
* one fact per halt: (HALT, symbol, NASDAQ_TRADER, halt time, "<symbol>|<halt date>|<halt time>"). The resumption appears later in the
  same item: the same natural key with new content - a NEW REVISION, so "halted" and "resumed" are both kept, each as known when seen;
* observed_at = the halt time (New York -> UTC); payload: reason code, market, issue name, pause threshold price, resumption date and
  quote / trade times; resumed_at is the resumption TRADE time;
* quality code: validate.halt_codes - HALTED_OPEN while no resumption trade time is known;
* tier 1 (an exchange's own event record, evidence.rules EVENT).
Reason codes are kept verbatim; reason_code_known says whether the code is in the list below - the LULD pause codes and their meanings
are UNVERIFIED (DESIGN_72 section 6).

Response validation (fail closed): the body must not declare a DOCTYPE or an ENTITY (UNSAFE_XML: no entity expansion is ever parsed),
must be at most MAX_BYTES (PAGE_TOO_LARGE) and must be RSS (rss / channel / item) - else INVALID_XML / UNEXPECTED_SHAPE. An item needs
the HaltDate (MM/DD/YYYY), HaltTime (HH:MM:SS) and IssueSymbol fields of the ndaq namespace, else MALFORMED_ROW / BAD_TIME.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, time
from typing import List, Optional

from api.validation import validate_symbol
from evidence.rules import tier_for
from market_intel import validate as VA
from market_intel.models import Observation
from market_intel.sources import common as C

PROVIDER = "NASDAQ_TRADER"
HOST = "www.nasdaqtrader.com"
ENDPOINT = "/rss.aspx"
KIND = "HALT"
NS = "http://www.nasdaqtrader.com/"
MAX_BYTES = 2_000_000
KNOWN_REASON_CODES = frozenset(("T1", "T2", "T3", "T5", "T6", "T7", "T8", "T12", "H4", "H9", "H10", "H11", "O1", "IPO1", "IPOQ", "IPOE", "M1",
                                "M2", "LUDP", "LUDS", "MWC0", "MWC1", "MWC2", "MWC3", "MWCQ", "R4", "R9", "C3", "C4", "C9", "C11", "D", "M"))
_TIME = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?$")
_UNSAFE = (b"<!doctype", b"<!entity")                         # compared case-insensitively


def request() -> dict:
    return C.request(PROVIDER, HOST, ENDPOINT, {"feed": "tradehalts"}, credential=None, requires=("REAL_FETCH", "NASDAQ_RSS_TERMS"))


def _field(item: ET.Element, name: str) -> str:
    e = item.find(f"{{{NS}}}{name}")
    return (e.text or "").strip() if e is not None else ""


def _clock(s: str) -> Optional[time]:
    m = _TIME.match(s)
    if not m:
        return None
    try:
        return time(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _at(d: str, t: str) -> Optional[datetime]:
    day, clock = C.iso_date(d, "%m/%d/%Y"), _clock(t)
    return C.ny_datetime(day, clock) if day is not None and clock is not None else None


@C.never_raises
def parse(body, *, retrieved_at: datetime, backfilled: bool = False, dataset_id: Optional[str] = None) -> C.Parsed:
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    if len(raw) > MAX_BYTES:
        return C.page_problem("PAGE_TOO_LARGE", bytes=len(raw))
    if any(x in raw.lower() for x in _UNSAFE):
        return C.page_problem("UNSAFE_XML")                             # no DTD, no entity: nothing to expand
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return C.page_problem("INVALID_XML")
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        return C.page_problem("UNEXPECTED_SHAPE")
    tier = tier_for(host=HOST, subject_ticker="", ticker="", source_type="EVENT")
    out: List[Observation] = []
    problems: List[dict] = []
    for i, item in enumerate(channel.findall("item")):
        sym_raw, hd, ht = _field(item, "IssueSymbol"), _field(item, "HaltDate"), _field(item, "HaltTime")
        try:
            sym = validate_symbol(sym_raw)
        except ValueError:
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        halted = _at(hd, ht)
        if halted is None:
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        rd, rq, rt = _field(item, "ResumptionDate"), _field(item, "ResumptionQuoteTime"), _field(item, "ResumptionTradeTime")
        resumed = _at(rd, rt) if (rd and rt) else None
        quote_at = _at(rd, rq) if (rd and rq) else None
        if (rd and rt and resumed is None) or (rd and rq and quote_at is None) or (resumed is not None and resumed < halted):
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        code = _field(item, "ReasonCode")
        threshold = _field(item, "PauseThresholdPrice")
        try:
            price = float(threshold) if threshold else None
        except ValueError:
            price = None
        flags = VA.halt_codes({"halted_at": halted, "resumed_at": resumed}) + VA.time_codes(halted, retrieved_at, backfilled=backfilled)
        session, sdate = C.session_fields(halted)
        try:
            out.append(Observation(kind=KIND, symbol=sym, provider=PROVIDER, feed="none", adjustment="none", observed_at=halted, retrieved_at=retrieved_at,
                                   available_at=retrieved_at, session=session, session_date=sdate, status="AVAILABLE", dataset_id=dataset_id,
                                   source_url=f"https://{HOST}{ENDPOINT}?feed=tradehalts", source_identifier=f"{sym}|{hd}|{ht}", tier=tier,
                                   backfilled=backfilled, quality_flags=tuple(sorted(set(flags))),
                                   payload={"halted_at": halted.isoformat(), "reason_code": code[:12], "reason_code_known": code in KNOWN_REASON_CODES,
                                            "market": _field(item, "Market")[:40], "issue_name": _field(item, "IssueName")[:120],
                                            "pause_threshold_price": price, "resumption_date": rd or None,
                                            "resumption_quote_at": quote_at.isoformat() if quote_at else None,
                                            "resumed_at": resumed.isoformat() if resumed else None}))
        except (ValueError, TypeError) as exc:
            problems.append({"problem": "REFUSED_BY_ENVELOPE", "index": i, "detail": str(exc)[:160]})
    return out, None, problems
