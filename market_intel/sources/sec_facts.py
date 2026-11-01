"""
market_intel/sources/sec_facts.py - Stage 7.2: SEC companyfacts (XBRL) -> FACT observations. PARSE-ONLY (DESIGN_72 3.4).

Only declared concepts are read: value_quality.ALL_CONCEPTS (us-gaap, the Stage 6.4 list - reused, never redefined) plus
dei:EntityCommonStockSharesOutstanding. Display and filters only; no fundamental feeds DDR (design C.11).

Mapping:
* one row per reported value: (taxonomy, concept, unit, accession, fiscal year / period, start, end). A restatement is a NEW fact under
  its own accession (each filing is its own vintage), never an overwrite;
* observed_at = the period end at New York midnight (a date-only fact, time_precision DATE_ONLY); the filed date is kept in the payload;
* operational availability is ours (available_at = retrieved_at). RESEARCH visibility reuses value_quality.pit unchanged: a fact is
  visible from the session AFTER its filing date (pit.Calendar.available_at) - `visible_from` in the payload - and is None, with
  visibility CALENDAR_NOT_COVERED, when the sessions after the filing date are outside the verified calendar (never guessed);
* tier 1 (an official SEC filing of the company itself).
Response validation: an object with the requested cik and a `facts` object of taxonomies; a value row needs end, val (a finite number),
accn, fy, fp, form and filed - else MALFORMED_ROW; more than MAX_FACTS selected rows is PAGE_TOO_LARGE.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

import config
import value_quality as VQ
from evidence.rules import tier_for
from evidence.sources import sec_edgar as EDGAR
from market_intel.models import Observation
from market_intel.sources import common as C
from market_intel.sources.sec_filings import ACCESSION, cik10
from value_quality import pit

PROVIDER = "SEC"
HOST = "data.sec.gov"
KIND = "FACT"
DEFAULT_CONCEPTS: Tuple[Tuple[str, str], ...] = tuple(("us-gaap", c) for c in VQ.ALL_CONCEPTS) + (("dei", "EntityCommonStockSharesOutstanding"),)
MAX_FACTS = 50_000
ROW_REQUIRED = frozenset(("end", "val", "accn", "fy", "fp", "form", "filed"))
VISIBILITY_SPAN_DAYS = 15                  # sessions looked up after a filing date (enough for any holiday run)


def request(cik: str, *, user_agent: Optional[str] = None) -> dict:
    ua = config.SEC_USER_AGENT if user_agent is None else user_agent
    url = EDGAR.companyfacts_url(cik10(cik))
    return C.request(PROVIDER, HOST, url.split(HOST, 1)[1], {}, credential="SEC_USER_AGENT", configured=bool(str(ua or "").strip()),
                     reason="SEC requires a User-Agent of 'Name email' (config.SEC_USER_AGENT)", requires=("REAL_FETCH",))


def visible_from(filed) -> Tuple[Optional[str], str]:
    """value_quality.pit's rule on the verified calendar: (the first session after the filing date, "PIT_RULE"), or (None,
    "CALENDAR_NOT_COVERED") when those sessions are not all covered."""
    sessions = C.market_sessions(filed, filed + timedelta(days=VISIBILITY_SPAN_DAYS))
    if not sessions:
        return None, "CALENDAR_NOT_COVERED"
    return pit.Calendar(sessions).available_at(filed.isoformat()), "PIT_RULE"


def _source_id(taxonomy: str, concept: str, unit: str, r: dict) -> str:
    sid = f"{taxonomy}:{concept}|{unit}|{r['accn']}|{r['fy']}|{r['fp']}|{r.get('start') or ''}"
    return sid if len(sid) <= 200 else f"{taxonomy}:sha256:{hashlib.sha256(sid.encode('utf-8')).hexdigest()}"


@C.never_raises
def parse(body, *, ticker: str, cik: str, retrieved_at: datetime, concepts: Sequence[Tuple[str, str]] = DEFAULT_CONCEPTS, backfilled: bool = False,
          dataset_id: Optional[str] = None) -> C.Parsed:
    doc, bad = C.load_json(body)
    if bad:
        return C.page_problem(bad)
    if not isinstance(doc, dict) or not isinstance(doc.get("facts"), dict):
        return C.page_problem("UNEXPECTED_SHAPE")
    try:
        if cik10(doc.get("cik")) != cik10(cik):
            return C.page_problem("SYMBOL_MISMATCH", detail="cik")
    except (TypeError, ValueError):
        return C.page_problem("UNEXPECTED_SHAPE", detail="cik")
    tier = tier_for(host="sec.gov", subject_ticker=ticker, ticker=ticker, source_type="SEC_FILING")
    rows = []
    for taxonomy, concept in concepts:
        node = (doc["facts"].get(taxonomy) or {}).get(concept)
        if node is None:
            continue                                                       # not reported: simply absent (no zero, no row)
        if not isinstance(node, dict) or not isinstance(node.get("units"), dict):
            return C.page_problem("UNEXPECTED_SHAPE", detail=f"{taxonomy}:{concept}")
        for unit, values in node["units"].items():
            if not isinstance(values, list):
                return C.page_problem("UNEXPECTED_SHAPE", detail=f"{taxonomy}:{concept}:{unit}")
            rows.extend((taxonomy, concept, str(unit), j, r) for j, r in enumerate(values))
    if len(rows) > MAX_FACTS:
        return C.page_problem("PAGE_TOO_LARGE", facts=len(rows))
    out: List[Observation] = []
    problems: List[dict] = []
    for taxonomy, concept, unit, j, r in rows:
        where = {"concept": f"{taxonomy}:{concept}", "unit": unit, "index": j}
        if not (isinstance(r, dict) and ROW_REQUIRED <= set(r) and C.is_number(r["val"]) and isinstance(r["accn"], str) and ACCESSION.match(r["accn"])
                and isinstance(r["fp"], str) and isinstance(r["form"], str) and (isinstance(r["fy"], int) and not isinstance(r["fy"], bool))):
            problems.append({"problem": "MALFORMED_ROW", **where})
            continue
        end, filed, start = C.iso_date(r["end"]), C.iso_date(r["filed"]), C.iso_date(r["start"]) if r.get("start") is not None else None
        if end is None or filed is None or (r.get("start") is not None and start is None):
            problems.append({"problem": "BAD_TIME", **where})
            continue
        seen_from, visibility = visible_from(filed)
        observed = C.ny_datetime(end)
        session, sdate = C.session_fields(observed)
        try:
            out.append(Observation(kind=KIND, symbol=ticker, provider=PROVIDER, feed="none", adjustment="none", observed_at=observed,
                                   retrieved_at=retrieved_at, available_at=retrieved_at, session=session, session_date=sdate, status="AVAILABLE",
                                   dataset_id=dataset_id, source_url=EDGAR.companyfacts_url(cik10(cik)), source_identifier=_source_id(taxonomy, concept, unit, r),
                                   tier=tier, backfilled=backfilled, quality_flags=("BACKFILLED",) if backfilled else (),
                                   payload={"taxonomy": taxonomy, "concept": concept, "unit": unit, "value": r["val"], "start": start.isoformat() if start else None,
                                            "end": end.isoformat(), "fy": r["fy"], "fp": r["fp"], "form": r["form"], "accn": r["accn"], "filed": filed.isoformat(),
                                            "frame": r.get("frame") if isinstance(r.get("frame"), str) else None, "time_precision": "DATE_ONLY",
                                            "visible_from": seen_from, "visibility": visibility}))
        except (ValueError, TypeError) as exc:
            problems.append({"problem": "REFUSED_BY_ENVELOPE", **where, "detail": str(exc)[:160]})
    return out, None, problems
