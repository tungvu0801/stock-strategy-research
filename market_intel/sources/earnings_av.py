"""
market_intel/sources/earnings_av.py - Stage 7.2: the Alpha Vantage EARNINGS_CALENDAR (CSV) -> EARNINGS observations, and an
EarningsEventProvider that reads them back from the store. PARSE-ONLY (DESIGN_72 3.7).

The free baseline (design C.11): one CSV call returns the 3-month calendar. It needs a NEW free key, ALPHAVANTAGE_API_KEY (an approval,
with its terms of use: verify), which is a query parameter the transport would add - never part of a request recorded here. config.py
declares no such key (config changes are Stage 7.3's), so the caller passes `configured`; WITHOUT A KEY THE REQUEST IS NOT_CONFIGURED.

Mapping:
* one fact per (symbol, calendar snapshot day, fiscal period): observed_at = the snapshot's New York date at midnight (the day the
  calendar SAID this), source identifier "expected:<fiscalDateEnding>". Each daily snapshot is kept, so a moved report date is
  point-in-time history (compare the snapshots), never an overwrite. The expected report date is in the payload - it is in the FUTURE,
  so it can never be an observed_at (the store refuses facts observed after their retrieval);
* rows are EARNINGS_EXPECTED: confirmed False, time_precision DATE_ONLY (the calendar's timeOfTheDay, when present, is kept verbatim as
  a hint: pre-market / post-market is not an exact time and none is synthesised - data/events TimePrecision rules);
* quality code: validate.earnings_codes (UNCONFIRMED_PAST for a report date already past without an 8-K item 2.02).
Response validation: Alpha Vantage answers errors and limits with HTTP 200 and a JSON body ({"Information" / "Note" / "Error Message"})
- ERROR_ENVELOPE; the CSV header must be exactly COLUMNS (optionally + timeOfTheDay) - else UNEXPECTED_SHAPE; a row needs a valid
symbol, ISO dates and a numeric or empty estimate - else MALFORMED_ROW / BAD_TIME.

StoreEarningsProvider implements data/events/earnings.EarningsEventProvider over the store (latest snapshot per fiscal period, and
SEC 8-K item 2.02 filings as confirmations). It is NOT wired into get_earnings_provider(): that changes the Stage 2.6 event context and
so the Stage 5.4 packets and the DDR context - a separate, approval-gated stage (design E.2 #7).
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional

from api.validation import validate_symbol
from data.events.earnings import EarningsEventProvider, EarningsQueryResult
from data.events.models import EventCategory, EventImportance, EventStatus, EventType, NormalizedEvent, TimePrecision
from market_intel import validate as VA
from market_intel.models import Observation
from market_intel.sources import common as C

PROVIDER = "ALPHA_VANTAGE"
HOST = "www.alphavantage.co"
ENDPOINT = "/query"
KIND = "EARNINGS"
COLUMNS = ("symbol", "name", "reportDate", "fiscalDateEnding", "estimate", "currency")
OPTIONAL_COLUMN = "timeOfTheDay"
HORIZONS = ("3month", "6month", "12month")
MAX_ROWS = 20_000
ENVELOPE_KEYS = ("Information", "Note", "Error Message")


def request(*, horizon: str = "3month", configured: bool = False) -> dict:
    if horizon not in HORIZONS:
        raise ValueError(f"horizon is one of {HORIZONS}")
    return C.request(PROVIDER, HOST, ENDPOINT, {"function": "EARNINGS_CALENDAR", "horizon": horizon}, credential="ALPHAVANTAGE_API_KEY",
                     configured=configured, reason="ALPHAVANTAGE_API_KEY is not configured (a new free key: an approval, with its terms)",
                     requires=("REAL_FETCH", "ALPHAVANTAGE_KEY_AND_TERMS"))


@C.never_raises
def parse(body, *, retrieved_at: datetime, symbols: Optional[Iterable[str]] = None, dataset_id: Optional[str] = None) -> C.Parsed:
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
    if text.lstrip().startswith(("{", "[")):
        doc, bad = C.load_json(text)
        return C.page_problem("ERROR_ENVELOPE" if isinstance(doc, dict) and any(k in doc for k in ENVELOPE_KEYS) else (bad or "UNEXPECTED_SHAPE"))
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return C.page_problem("INVALID_CSV")
    if not rows:
        return C.page_problem("UNEXPECTED_SHAPE", detail="no header")
    header = tuple(h.strip() for h in rows[0])
    if header not in (COLUMNS, COLUMNS + (OPTIONAL_COLUMN,)):
        return C.page_problem("UNEXPECTED_SHAPE", detail="header")
    if len(rows) - 1 > MAX_ROWS:
        return C.page_problem("PAGE_TOO_LARGE", rows=len(rows) - 1)
    wanted = {s.upper() for s in symbols} if symbols else None
    snapshot = C.ny_date(retrieved_at)
    observed = C.ny_datetime(snapshot)
    session, sdate = C.session_fields(observed)
    out: List[Observation] = []
    problems: List[dict] = []
    for i, r in enumerate(rows[1:]):
        if not r or all(not x.strip() for x in r):
            continue                                                       # a blank trailing line
        if len(r) != len(header):
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        row = dict(zip(header, (x.strip() for x in r)))
        try:
            sym = validate_symbol(row["symbol"])
        except ValueError:
            problems.append({"problem": "MALFORMED_ROW", "index": i})
            continue
        if wanted is not None and sym not in wanted:
            continue
        report, fiscal = C.iso_date(row["reportDate"]), C.iso_date(row["fiscalDateEnding"])
        if report is None or fiscal is None:
            problems.append({"problem": "BAD_TIME", "index": i})
            continue
        estimate = None
        if row["estimate"]:
            try:
                estimate = float(row["estimate"])
            except ValueError:
                problems.append({"problem": "MALFORMED_ROW", "index": i})
                continue
        flags = VA.earnings_codes(report, snapshot, confirmed_by_8k_202=False)
        try:
            out.append(Observation(kind=KIND, symbol=sym, provider=PROVIDER, feed="none", adjustment="none", observed_at=observed, retrieved_at=retrieved_at,
                                   available_at=retrieved_at, session=session, session_date=sdate, status="AVAILABLE", dataset_id=dataset_id,
                                   source_identifier=f"expected:{fiscal.isoformat()}", quality_flags=tuple(flags),
                                   payload={"report_date": report.isoformat(), "fiscal_date_ending": fiscal.isoformat(), "estimate": estimate,
                                            "currency": row["currency"][:8] or None, "name": row["name"][:120], "time_of_day": row.get(OPTIONAL_COLUMN) or None,
                                            "time_precision": TimePrecision.DATE_ONLY.value, "confirmed": False, "snapshot_date": snapshot.isoformat(),
                                            "event": "EARNINGS_EXPECTED"}))
        except (ValueError, TypeError) as exc:
            problems.append({"problem": "REFUSED_BY_ENVELOPE", "index": i, "detail": str(exc)[:160]})
    return out, None, problems


class StoreEarningsProvider(EarningsEventProvider):
    """data/events EarningsEventProvider over the market-intelligence store, point in time at `as_of` (default: now). Upcoming: the
    LATEST calendar snapshot known at as_of, per fiscal period, with a report date in [start, end]. Recent: SEC 8-K item 2.02 filings
    accepted in [start, end] (official, after the fact: confirmed). Nothing known -> available False, never an invented date."""

    def __init__(self, store, *, as_of: Optional[datetime] = None):
        self.store, self.as_of = store, as_of

    def _at(self) -> datetime:
        return self.as_of or datetime.now(timezone.utc)

    def get_upcoming_earnings(self, symbol: str, start_date: date, end_date: date) -> EarningsQueryResult:
        rows = self.store.history(KIND, symbol=symbol, as_of=self._at(), usable_only=True)
        latest = {}
        for r in rows:                                                     # ascending by snapshot: the last one per fiscal period wins
            latest[r["payload"]["fiscal_date_ending"]] = r
        events = []
        for fiscal, r in sorted(latest.items()):
            d = date.fromisoformat(r["payload"]["report_date"])
            if start_date <= d <= end_date:
                events.append(NormalizedEvent(event_id=f"av-{symbol.upper()}-{fiscal}-{r['payload']['snapshot_date']}", event_type=EventType.EARNINGS,
                                              category=EventCategory.COMPANY, symbol=symbol.upper(), title=f"{symbol.upper()} earnings (expected)",
                                              event_date=d, event_time=None, timezone=None, event_datetime_utc=None, time_precision=TimePrecision.DATE_ONLY,
                                              source="Alpha Vantage earnings calendar", source_url=None, source_provider=PROVIDER, status=EventStatus.UPCOMING,
                                              importance=EventImportance.HIGH, confirmed=False, retrieved_at=datetime.fromisoformat(r["retrieved_at"].replace("Z", "+00:00")),
                                              raw_reference_id=r["source_identifier"], metadata={"snapshot_date": r["payload"]["snapshot_date"],
                                                                                                 "time_of_day": r["payload"]["time_of_day"],
                                                                                                 "fiscal_date_ending": fiscal}))
        if not rows:
            return EarningsQueryResult(available=False, events=[], reason="no earnings calendar snapshot is stored")
        return EarningsQueryResult(available=True, events=events)

    def get_recent_earnings(self, symbol: str, start_date: date, end_date: date) -> EarningsQueryResult:
        rows = [r for r in self.store.history("FILING", symbol=symbol, as_of=self._at()) if r["payload"].get("earnings_release")]
        events = []
        for r in rows:
            t = datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00"))
            d = C.ny_date(t)
            if start_date <= d <= end_date:
                events.append(NormalizedEvent(event_id=f"sec-{r['source_identifier']}", event_type=EventType.EARNINGS, category=EventCategory.COMPANY,
                                              symbol=symbol.upper(), title=f"{symbol.upper()} earnings release (8-K item 2.02)", event_date=d, event_time=None,
                                              timezone=None, event_datetime_utc=None, time_precision=TimePrecision.DATE_ONLY, source="SEC EDGAR 8-K item 2.02",
                                              source_url=r["source_url"], source_provider="SEC", status=EventStatus.RELEASED, importance=EventImportance.HIGH,
                                              confirmed=True, retrieved_at=datetime.fromisoformat(r["retrieved_at"].replace("Z", "+00:00")),
                                              raw_reference_id=r["source_identifier"], metadata={"form": r["payload"]["form"]}))
        if not rows:
            return EarningsQueryResult(available=False, events=[], reason="no SEC 8-K item 2.02 filing is stored")
        return EarningsQueryResult(available=True, events=events)
