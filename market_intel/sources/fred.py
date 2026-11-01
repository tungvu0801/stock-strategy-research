"""
market_intel/sources/fred.py - Stage 7.2: FRED / ALFRED series observations -> MACRO observations. PARSE-ONLY (DESIGN_72 3.5).

`GET /fred/series/observations` with realtime_start / realtime_end returns every VINTAGE of every observation (ALFRED): the value an
observation date had from realtime_start until realtime_end. The existing release-dates module (data/events/macro.py) is a different
endpoint and stays unchanged. The API key is a query parameter the transport adds (credential FRED_API_KEY): it is never part of a
request recorded here. Without it the request is NOT_CONFIGURED. Series availability and licensing per series: verify (DESIGN_72 6).

Mapping:
* one fact per (series, observation date, vintage): observed_at = the observation date at New York midnight (DATE_ONLY); the vintage
  is the source identifier ("vintage:<realtime_start>") and its dates are in the payload. A later vintage is its own fact - every
  vintage is kept, so a research reader (Stage 7.6) can ask what was known when;
* FRED's "." (no value) is an ABSENCE: status UNAVAILABLE, no payload - never a zero;
* paging: offset + limit < count -> the next page's offset is the token.
Response validation: an object with integer count / offset / limit and an `observations` list; FRED's error envelope (error_code /
error_message) is ERROR_ENVELOPE. The paging metadata must be CONSISTENT, or the page is UNEXPECTED_SHAPE (detail pagination) - never a
legitimate empty result: count >= 0, offset >= 0, 1 <= limit <= PAGE_LIMIT, offset <= count, offset + rows <= count, rows <= limit (else
PAGE_TOO_LARGE), a page short of `limit` must end the series (offset + rows == count), and an empty page is legitimate only at the end
(offset == count); when the caller passes the offset it asked for, the response's offset must equal it; an observation is exactly {realtime_start, realtime_end, date, value} with ISO dates and a numeric
string value or ".", else MALFORMED_ROW / BAD_TIME.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import List, Optional

import config
from market_intel.models import Observation
from market_intel.sources import common as C

PROVIDER = "FRED"
HOST = "api.stlouisfed.org"
ENDPOINT = "/fred/series/observations"
KIND = "MACRO"
SERIES = ("DGS10", "DGS2", "T10Y2Y", "VIXCLS", "UNRATE", "CPIAUCSL", "FEDFUNDS")   # the design's fixed list (C.11)
PAGE_LIMIT = 100_000                     # FRED's documented maximum `limit`
ROW_KEYS = frozenset(("realtime_start", "realtime_end", "date", "value"))
_SERIES = re.compile("^[A-Z0-9_.:-]{1,40}$")
ALL_VINTAGES = ("1776-07-04", "9999-12-31")


def request(series_id: str, *, observation_start: date, observation_end: date, realtime_start: str = ALL_VINTAGES[0],
            realtime_end: str = ALL_VINTAGES[1], offset: int = 0, limit: int = PAGE_LIMIT, configured: Optional[bool] = None) -> dict:
    if not _SERIES.match(series_id) or not 1 <= int(limit) <= PAGE_LIMIT or int(offset) < 0 or observation_start > observation_end:
        raise ValueError("a series id, 0 <= offset, 1 <= limit <= 100000 and start <= end")
    ok = config.has_fred_credentials() if configured is None else configured
    params = {"series_id": series_id, "file_type": "json", "observation_start": observation_start.isoformat(), "observation_end": observation_end.isoformat(),
              "realtime_start": realtime_start, "realtime_end": realtime_end, "sort_order": "asc", "offset": int(offset), "limit": int(limit)}
    return C.request(PROVIDER, HOST, ENDPOINT, params, credential="FRED_API_KEY", configured=ok, requires=("REAL_FETCH", "FRED_SERIES_TERMS"))


@C.never_raises
def parse(body, *, series_id: str, retrieved_at: datetime, backfilled: bool = False, dataset_id: Optional[str] = None,
          expected_offset: Optional[int] = None) -> C.Parsed:
    doc, bad = C.load_json(body)
    if bad:
        return C.page_problem(bad)
    if isinstance(doc, dict) and ("error_code" in doc or "error_message" in doc):
        return C.page_problem("ERROR_ENVELOPE")
    if not (isinstance(doc, dict) and isinstance(doc.get("observations"), list)
            and all(isinstance(doc.get(k), int) and not isinstance(doc.get(k), bool) for k in ("count", "offset", "limit"))):
        return C.page_problem("UNEXPECTED_SHAPE")
    obs, count, offset, limit = doc["observations"], doc["count"], doc["offset"], doc["limit"]
    if count < 0 or offset < 0 or not 1 <= limit <= PAGE_LIMIT or offset > count or (expected_offset is not None and offset != expected_offset):
        return C.page_problem("UNEXPECTED_SHAPE", detail="pagination")
    if len(obs) > limit:
        return C.page_problem("PAGE_TOO_LARGE", observations=len(obs))
    nxt = offset + len(obs)
    if nxt > count or (len(obs) < limit and nxt != count):                 # more rows than exist, or a short page that does not end the series
        return C.page_problem("UNEXPECTED_SHAPE", detail="pagination")
    token = str(nxt) if nxt < count else None
    out: List[Observation] = []
    problems: List[dict] = []
    for i, r in enumerate(obs):
        if not (isinstance(r, dict) and set(r) == ROW_KEYS and all(isinstance(r[k], str) for k in ROW_KEYS)):
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        d, rs = C.iso_date(r["date"]), C.iso_date(r["realtime_start"])
        re_ = C.iso_date(r["realtime_end"]) if r["realtime_end"] != ALL_VINTAGES[1] else date(9999, 12, 31)
        if d is None or rs is None or re_ is None or rs > re_:
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        value = None
        if r["value"].strip() != ".":
            try:
                value = float(r["value"])
            except ValueError:
                problems.append({"problem": "MALFORMED_ROW", "index": i})
                continue
            if not math.isfinite(value):
                problems.append({"problem": "MALFORMED_ROW", "index": i})
                continue
        observed = C.ny_datetime(d)
        session, sdate = C.session_fields(observed)
        common = dict(kind=KIND, series_id=series_id, provider=PROVIDER, feed="none", adjustment="none", observed_at=observed, retrieved_at=retrieved_at,
                      available_at=retrieved_at, session=session, session_date=sdate, dataset_id=dataset_id, source_identifier=f"vintage:{rs.isoformat()}",
                      backfilled=backfilled, quality_flags=("BACKFILLED",) if backfilled else ())
        if value is None:
            out.append(Observation(**common, status="UNAVAILABLE", payload={}))       # "." - no value: an absence, never a zero
        else:
            out.append(Observation(**common, status="AVAILABLE", payload={"value": value, "date": d.isoformat(), "realtime_start": rs.isoformat(),
                                                                           "realtime_end": r["realtime_end"], "units": str(doc.get("units") or "")[:60],
                                                                           "time_precision": "DATE_ONLY"}))
    return out, token, problems
