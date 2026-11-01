"""
market_intel/sources/sec_filings.py - Stage 7.2: SEC EDGAR submissions JSON -> FILING observations. PARSE-ONLY (DESIGN_72 3.3).

The data layer polls only `submissions` METADATA (design C.11); filing documents stay with the evidence adapter
(evidence/sources/sec_edgar.py), whose URL builders are reused here, never duplicated. SEC requires a User-Agent of "Name email": without
config.SEC_USER_AGENT a request is NOT_CONFIGURED.

Mapping:
* one row per filing of `filings.recent` (the column arrays must all have the same length - else UNEXPECTED_SHAPE);
* observed_at = published_at = the acceptance time. ASSUMPTION (unverified, DESIGN_72 section 6): EDGAR's `acceptanceDateTime` carries
  a "Z" but is read as New York wall-clock time - the EDGAR filing index shows the same digits as Eastern time, and the later reading
  is the conservative one for availability. Each row records acceptance_raw, acceptance_tz and acceptance_check (CONSISTENT when the
  filing date is the acceptance's New York date or within the next 4 days, else INCONSISTENT);
* natural key: (FILING, symbol, SEC, acceptance, accession number); tier 1 (an official filing of the company itself, evidence.rules);
* payload: form, filing date, report date, 8-K items, primary document, and earnings_release (an 8-K item 2.02: the official, after-the-
  fact earnings report the earnings calendar is confirmed against).
"""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import List, Optional, Sequence

import config
from evidence.rules import tier_for
from evidence.sources import sec_edgar as EDGAR
from market_intel import validate as VA
from market_intel.models import Observation
from market_intel.sources import common as C

PROVIDER = "SEC"
HOST = "data.sec.gov"
KIND = "FILING"
COLUMNS = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items", "primaryDocument")
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
ACCEPTANCE_TZ = "ASSUMED_AMERICA_NEW_YORK"
FILING_LAG_DAYS = 4                       # a filing accepted after 17:30 ET is dated the next business day (a long weekend: up to 4 days)
_ACCEPT = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?$")


def cik10(cik) -> str:
    return f"{int(str(cik).strip()):010d}"


def request(cik: str, *, user_agent: Optional[str] = None) -> dict:
    ua = config.SEC_USER_AGENT if user_agent is None else user_agent
    url = EDGAR.submissions_url(cik10(cik))
    return C.request(PROVIDER, HOST, url.split(HOST, 1)[1], {}, credential="SEC_USER_AGENT", configured=bool(str(ua or "").strip()),
                     reason="SEC requires a User-Agent of 'Name email' (config.SEC_USER_AGENT)", requires=("REAL_FETCH",))


def acceptance_time(raw) -> Optional[datetime]:
    m = _ACCEPT.match(str(raw or "").strip())
    if not m:
        return None
    d = C.iso_date(m.group(1))
    if d is None:
        return None
    try:
        return C.ny_datetime(d, time(int(m.group(2)), int(m.group(3)), int(m.group(4))))
    except ValueError:
        return None


@C.never_raises
def parse(body, *, ticker: str, cik: str, retrieved_at: datetime, forms: Optional[Sequence[str]] = None, backfilled: bool = False,
          dataset_id: Optional[str] = None) -> C.Parsed:
    doc, bad = C.load_json(body)
    if bad:
        return C.page_problem(bad)
    if not isinstance(doc, dict) or not isinstance(doc.get("filings"), dict) or not isinstance(doc["filings"].get("recent"), dict):
        return C.page_problem("UNEXPECTED_SHAPE")
    try:
        if cik10(doc.get("cik")) != cik10(cik):
            return C.page_problem("SYMBOL_MISMATCH", detail="cik")
    except (TypeError, ValueError):
        return C.page_problem("UNEXPECTED_SHAPE", detail="cik")
    recent = doc["filings"]["recent"]
    if not all(isinstance(recent.get(c), list) for c in COLUMNS) or len({len(recent[c]) for c in COLUMNS}) != 1:
        return C.page_problem("UNEXPECTED_SHAPE", detail="columns")
    want = {f.upper() for f in forms} if forms else None
    tier = tier_for(host="sec.gov", subject_ticker=ticker, ticker=ticker, source_type="SEC_FILING")
    out: List[Observation] = []
    problems: List[dict] = []
    for i in range(len(recent["accessionNumber"])):
        row = {c: recent[c][i] for c in COLUMNS}
        if not all(isinstance(row[c], str) for c in COLUMNS) or not ACCESSION.match(row["accessionNumber"]) or not row["form"].strip():
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        if want is not None and row["form"].upper() not in want:
            continue                                                       # a form the caller does not follow (not a problem)
        accepted, filed = acceptance_time(row["acceptanceDateTime"]), C.iso_date(row["filingDate"])
        if accepted is None or filed is None:
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        lag = (filed - C.ny_date(accepted)).days
        items = [x.strip() for x in row["items"].split(",") if x.strip()]
        session, sdate = C.session_fields(accepted)
        try:
            out.append(Observation(kind=KIND, symbol=ticker, provider=PROVIDER, feed="none", adjustment="none", observed_at=accepted, published_at=accepted,
                                   retrieved_at=retrieved_at, available_at=retrieved_at, session=session, session_date=sdate, status="AVAILABLE",
                                   dataset_id=dataset_id, source_url=EDGAR.index_url(cik10(cik), row["accessionNumber"]),
                                   source_identifier=row["accessionNumber"], tier=tier, backfilled=backfilled,
                                   quality_flags=tuple(sorted(set(VA.time_codes(accepted, retrieved_at, backfilled=backfilled)))),
                                   payload={"form": row["form"].strip(), "filing_date": filed.isoformat(), "report_date": row["reportDate"] or None,
                                            "items": items, "primary_document": row["primaryDocument"] or None, "earnings_release": "2.02" in items,
                                            "acceptance_raw": row["acceptanceDateTime"], "acceptance_tz": ACCEPTANCE_TZ,
                                            "acceptance_check": "CONSISTENT" if 0 <= lag <= FILING_LAG_DAYS else "INCONSISTENT"}))
        except (ValueError, TypeError) as exc:
            problems.append({"problem": "REFUSED_BY_ENVELOPE", "index": i, "detail": str(exc)[:160]})
    return out, None, problems
