"""
market_intel/ingest.py - Stage 7.1: the backfill PLANNER, its CLI, and the (gated) backfill runner.

    python -m market_intel.ingest plan     --symbols SPY,AAPL --start 2025-01-02 --end 2025-12-31 --timeframe 1Day --feed iex [--adjustment raw]
    python -m market_intel.ingest dry-run  ... (the plan plus the first requests it would send)
    python -m market_intel.ingest calendar --start 2025-01-01 --end 2025-12-31
    python -m market_intel.ingest inspect  --db PATH          (an EXISTING store, read-only; never creates one)
    python -m market_intel.ingest execute  ...                (always refused in Stage 7.1: a real backfill needs operator approval)

Every CLI command is OFFLINE: no network request, no database write, no paid provider, no runtime change (tested with the socket
blocked). Exit codes: 0 a valid plan / report, 2 a refused plan (the refusal codes say why), 3 execute refused, 1 a usage error.

Planning checks: symbols (the API's ticker rule), dates (start <= end, only completed sessions: end before today in New York), timeframe /
feed / adjustment explicit, calendar coverage (any uncovered weekday in the range refuses the plan: CALENDAR_NOT_COVERED), the SIP
15-minute rule (Basic plan: a SIP window must end at least 16 minutes ago) and estimates: sessions, rows, pages, requests, minutes at
the backfill budget, bytes. A SIP plan always lists SIP_FEED among the approvals it needs; every plan lists REAL_BACKFILL.

A plan above MAX_PLAN_ROWS rows is refused (ROWS_EXCEED_GENERIC_STORE_CAP): the generic store is measured for that order of magnitude,
not for a multi-year minute history of a whole universe (DESIGN_71 section 7).

run_backfill() is the library path a FUTURE approved run will use. It takes the BackfillRequest itself - never a plan dict - and
RECOMPUTES the plan at run time; the operator approval (approvals.py: a file in the approvals directory, re-read and re-hashed here) must
name that exact request and plan hash, bound its estimate with its caps, carry sip_approved for SIP, and is claimed once, in the
transaction that creates the dataset. It also needs an injected fetch function, the backfill window (outside 07:00-20:00 New York time
on market days), a budget no larger than the plan's, the raw archive and the store. Every failure on the way (a transport exception, a
malformed Retry-After, an unparsable page, a store refusal) is recorded and the dataset is ALWAYS sealed (COMPLETED / PARTIAL / FAILED /
EMPTY), its manifest hash anchored in the raw archive. Stage 7.1 ships no fetch function and the CLI never builds an approval, so no
real request can be made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from market_intel import approvals as APP
from market_intel import calendar as CAL
from market_intel.budget import ProviderBudget
from market_intel.models import FetchRecord
from market_intel.settings import settings as mi_settings
from market_intel.sources import alpaca_bars as AB

PLAN_VERSION = "mi_backfill_plan_v1"
MAX_SYMBOLS = 100
MAX_RANGE_DAYS = 3660
SIP_DELAY_MINUTES = 16                    # Alpaca Basic: SIP history is available from 15 minutes back (design C.8; verify on the plan)
WINDOW_BLOCK = (7 * 60, 20 * 60)          # no backfill 07:00-20:00 New York time on a market day (it shares the 200/min plan)
EXTENDED_MINUTES = 16 * 60                # 04:00-20:00
BYTES_PER_ROW_ESTIMATE = 1500             # one value row + its membership row incl. indexes: measured 1,440-1,600 (test_C05, DESIGN_71 section 7)
MAX_RETRIES = 2
RETRY_SLEEP_S = 5.0                       # after a transport exception or a 5xx (a 429 waits for the budget's back-off instead)
MAX_PLAN_ROWS = 2_500_000                 # the generic store's protective cap per plan (B10: measured, DESIGN_71 section 7)
BUDGET_NAME = "ALPACA_BACKFILL"
EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_EXECUTE_REFUSED = 0, 1, 2, 3


def _et(d: date, minute: int) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.combine(d, time(minute // 60, minute % 60), tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class BackfillRequest:
    symbols: Tuple[str, ...]
    start: date
    end: date
    timeframe: str
    feed: str
    adjustment: str = "raw"
    extended_hours: bool = False

    def as_dict(self) -> dict:
        return {"symbols": list(self.symbols), "start": self.start.isoformat(), "end": self.end.isoformat(), "timeframe": self.timeframe,
                "feed": self.feed, "adjustment": self.adjustment, "extended_hours": self.extended_hours}


def coverage(start: date, end: date) -> dict:
    sessions, uncovered, versions = 0, [], set()
    x = start
    while x <= end:
        st = CAL.market_day_status(x)
        if st == CAL.MARKET_DAY:
            sessions += 1
            versions.add(CAL.calendar_version(x))
        elif st == CAL.NOT_COVERED:
            uncovered.append(x.isoformat())
        x += timedelta(days=1)
    return {"status": "COVERED" if not uncovered else "NOT_COVERED", "sessions": sessions, "uncovered_count": len(uncovered),
            "uncovered_first": uncovered[:10], "uncovered_last": uncovered[-3:], "calendars": sorted(versions)}


def _rows_per_symbol(req: BackfillRequest) -> int:
    n, x = 0, req.start
    while x <= req.end:
        mins = CAL.regular_minutes(x) if CAL.market_day_status(x) != CAL.NOT_COVERED else 0
        if mins:
            if req.timeframe == "1Day":
                n += 1
            elif req.timeframe == "1Hour":
                n += 16 if req.extended_hours else math.ceil((mins + 30) / 60)       # hour-aligned bars from 09:00 (09:30-10:00) on
            else:
                n += EXTENDED_MINUTES if req.extended_hours else mins
        x += timedelta(days=1)
    return n


def backfill_window_open(now: datetime) -> bool:
    """Outside 07:00-20:00 New York time on a market day; any time on a covered weekend / holiday; never on an uncovered weekday."""
    s = CAL.session(now)
    st = s["day_status"]
    if st == CAL.NOT_COVERED:
        return False
    if st != CAL.MARKET_DAY:
        return True
    return not (WINDOW_BLOCK[0] <= (s["et_minute"] or 0) < WINDOW_BLOCK[1])


def validate(req: BackfillRequest, now: datetime) -> List[dict]:
    from api.validation import validate_symbol
    out: List[dict] = []
    if not req.symbols:
        out.append({"code": "NO_SYMBOLS", "detail": "at least one symbol"})
    if len(req.symbols) > MAX_SYMBOLS:
        out.append({"code": "TOO_MANY_SYMBOLS", "detail": f"at most {MAX_SYMBOLS}"})
    for s in req.symbols:
        try:
            ok = validate_symbol(s) == s
        except ValueError:
            ok = False
        if not ok:
            out.append({"code": "INVALID_SYMBOL", "detail": str(s)[:12] + " (an upper-case ticker)"})
    if len(set(req.symbols)) != len(req.symbols):
        out.append({"code": "DUPLICATE_SYMBOL", "detail": "each symbol once"})
    if req.timeframe not in AB.TIMEFRAMES:
        out.append({"code": "INVALID_TIMEFRAME", "detail": f"one of {sorted(AB.TIMEFRAMES)}"})
    if req.feed not in AB.FEEDS:
        out.append({"code": "FEED_REQUIRED", "detail": "an explicit feed: iex or sip (feeds are never mixed)"})
    if req.adjustment not in AB.ADJUSTMENTS:
        out.append({"code": "INVALID_ADJUSTMENT", "detail": f"one of {AB.ADJUSTMENTS}"})
    if req.extended_hours and req.timeframe == "1Day":
        out.append({"code": "EXTENDED_HOURS_INTRADAY_ONLY", "detail": "extended hours apply to 1Min / 1Hour"})
    if req.start > req.end:
        out.append({"code": "START_AFTER_END", "detail": f"{req.start} > {req.end}"})
    elif (req.end - req.start).days > MAX_RANGE_DAYS:
        out.append({"code": "RANGE_TOO_LONG", "detail": f"at most {MAX_RANGE_DAYS} days per plan"})
    today = CAL.session(now)["session_date"]
    if today is not None and req.end >= today:
        out.append({"code": "END_NOT_COMPLETED", "detail": f"only completed sessions: end must be before {today} (New York)"})
    if req.feed == "sip" and req.start <= req.end:
        window_end = _et(req.end + timedelta(days=1), 0)                             # the request covers up to the end date's midnight
        if window_end > now - timedelta(minutes=SIP_DELAY_MINUTES):
            out.append({"code": "SIP_WITHIN_15_MIN", "detail": f"Basic serves SIP history from {SIP_DELAY_MINUTES} minutes back only"})
    return out


def plan(req: BackfillRequest, now: datetime, *, store=None) -> dict:
    """The offline plan: validation, coverage, estimates, budget. No network, no write (a store, when given, is only read)."""
    refusals = validate(req, now)
    cov = coverage(req.start, req.end) if req.start <= req.end else {"status": "INVALID", "sessions": 0, "uncovered_count": 0}
    if cov["status"] == "NOT_COVERED":
        refusals.append({"code": "CALENDAR_NOT_COVERED", "detail": f"{cov['uncovered_count']} weekday(s) outside the verified calendar, first "
                                                                   f"{cov['uncovered_first'][:3]}: refused, never assumed to be sessions"})
    b = mi_settings()["budgets"]
    per_min = b["MI_ALPACA_BACKFILL_MAX_PER_MIN"]
    rows = _rows_per_symbol(req) if req.start <= req.end and req.timeframe in AB.TIMEFRAMES else 0
    pages = max(1, math.ceil(rows / AB.PAGE_LIMIT)) if rows else 0
    n = len(req.symbols)
    est = {"rows_per_symbol": rows, "rows_total": rows * n, "pages_per_symbol": pages, "requests": pages * n, "page_limit": AB.PAGE_LIMIT,
           "minutes_at_budget": round(pages * n / per_min, 2) if per_min else None, "bytes_estimate": rows * n * BYTES_PER_ROW_ESTIMATE,
           "bytes_per_row_estimate": BYTES_PER_ROW_ESTIMATE}
    if est["rows_total"] > MAX_PLAN_ROWS:
        refusals.append({"code": "ROWS_EXCEED_GENERIC_STORE_CAP", "detail": f"{est['rows_total']} rows > {MAX_PLAN_ROWS} per plan: split the request "
                                                                            "(the generic store's measured limit, DESIGN_71 section 7)"})
    used = None
    if store is not None and store.exists():
        used = store.budget_used(BUDGET_NAME, CAL.session(now)["session_date"])
    policy = {"provider": BUDGET_NAME, "per_minute": per_min, "max_retries": MAX_RETRIES, "window_block": list(WINDOW_BLOCK),
              "max_plan_rows": MAX_PLAN_ROWS, "parser": AB.ENDPOINT, "page_limit": AB.PAGE_LIMIT}
    # the hash binds the request, its coverage (calendar versions included), the estimate and the run policy (budget, retries, window)
    body = {"plan_version": PLAN_VERSION, "request": req.as_dict(), "coverage": cov, "estimate": est, "policy": policy}
    sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    return {**body, "valid": not refusals, "refusals": refusals, "network": False, "writes": False, "paid": False,
            "requires_approval": ["REAL_BACKFILL"] + (["SIP_FEED"] if req.feed == "sip" else []),
            "budget": {"provider": BUDGET_NAME, "per_minute": per_min, "window_rule": "outside 07:00-20:00 New York time on market days",
                       "window_open_now": backfill_window_open(now), "used_today": used},
            "plan_sha256": sha, "evaluated_at": _iso(now)}


def preview(p: dict, n: int = 10) -> List[dict]:
    """The first request of each symbol (up to n) - page tokens are only known online, so later pages are counted, not listed."""
    r = p["request"]
    s, e = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
    start, end = _et(s, 0), _et(e + timedelta(days=1), 0)
    return [{"endpoint": AB.ENDPOINT.format(symbol=sym.upper()), "params": AB.request_params(sym, r["timeframe"], start, end, r["feed"], r["adjustment"]),
             "pages_expected": p["estimate"]["pages_per_symbol"]} for sym in r["symbols"][:n]]


# ------------------------------------------------------------------------------------------------------------ the gated runner
def retry_after_s(headers, retrieved: datetime) -> Optional[float]:
    """Retry-After in seconds: a non-negative finite number, or an HTTP date (RFC 9110) relative to `retrieved`. Anything else - absent,
    negative, NaN, garbage, a header map that is not a map - is None, and the budget applies its own default back-off."""
    if not isinstance(headers, Mapping):
        return None
    raw = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
    if raw is None:
        return None
    text = str(raw).strip()[:64]
    try:
        v = float(text)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            d = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if d is None or d.tzinfo is None:
            return None
        return max(0.0, (d - retrieved).total_seconds())
    return v if math.isfinite(v) and v >= 0 else None


def _http_status(status) -> str:
    if status == 200:
        return "OK"
    if status == 429:
        return "RATE_LIMITED"
    if status in (401, 403):
        return "ACCESS_DENIED"
    return "HTTP_ERROR"


def _final_status(stats: dict, stop: Optional[str]) -> str:
    if stats["ok_fetches"] == 0:
        return "FAILED"
    if stop or stats["unrecovered"] or stats["rejected"] or stats["problems"]:
        return "PARTIAL"
    if stats["inserted"] + stats["revised"] + stats["duplicates"] == 0:
        return "EMPTY"
    return "COMPLETED"


def run_backfill(req: BackfillRequest, *, approval, fetch_fn: Callable[[dict], Tuple[int, dict, bytes]], store, archive,
                 clock: Callable[[], datetime], sleep_fn: Callable[[float], None], budget: Optional[ProviderBudget] = None,
                 approvals_root: Optional[Path] = None) -> dict:
    """Fetch, archive and ingest an APPROVED request (see the module docstring). Refusals before the dataset exists raise PermissionError
    (ApprovalError) or StoreError and send nothing. Once the dataset exists it is always sealed; an Exception during the run becomes the
    stop reason (returned), and a KeyboardInterrupt / SystemExit is re-raised after the seal."""
    if not isinstance(req, BackfillRequest):
        raise PermissionError("run_backfill takes the BackfillRequest itself: the plan is recomputed here, never accepted from a caller")
    now = clock()
    fresh = plan(req, now)                                               # recomputed now: a plan cannot be edited after approval
    APP.check(approval, fresh, now=now, root=approvals_root)
    if not backfill_window_open(now):
        raise PermissionError("outside the backfill window (no backfill 07:00-20:00 New York time on a market day)")
    per_min = fresh["budget"]["per_minute"]
    if budget is None:
        budget = ProviderBudget(BUDGET_NAME, per_min, clock)
    elif budget.name != BUDGET_NAME or budget.bucket.per_minute > per_min or budget.bucket.capacity > per_min:
        raise PermissionError(f"the budget may not exceed the plan's ({BUDGET_NAME}, {per_min}/min)")
    from market_intel.store import DatasetSpec
    r = fresh["request"]
    first, last = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
    kind = AB.TIMEFRAMES[r["timeframe"]]
    did = store.create_dataset(DatasetSpec(job_kind="BACKFILL", kind=kind, provider=AB.PROVIDER, feed=r["feed"], adjustment=r["adjustment"],
                                           symbols=r["symbols"], timeframe=r["timeframe"], start_date=first, end_date=last, request=r,
                                           plan_sha256=fresh["plan_sha256"], approval_ref=approval.approval_id), approval=approval)
    stats = {"requests": 0, "ok_fetches": 0, "failed_fetches": 0, "unrecovered": 0, "inserted": 0, "duplicates": 0, "revised": 0,
             "absences": 0, "rejected": 0, "problems": 0, "rows_delivered": 0}
    stop, interrupt = None, None
    try:
        stop = _run(r, did, first, last, fetch_fn=fetch_fn, store=store, archive=archive, approval=approval, clock=clock, sleep_fn=sleep_fn,
                    budget=budget, stats=stats)
    except Exception as exc:                                             # noqa: BLE001 - recorded as the stop reason, never left RUNNING
        stop = f"ERROR:{type(exc).__name__}"
    except BaseException as exc:                                         # KeyboardInterrupt / SystemExit: sealed first, then re-raised
        stop, interrupt = f"INTERRUPTED:{type(exc).__name__}", exc
    final = _final_status(stats, stop)
    detail = json.dumps({"stop": stop, **{k: stats[k] for k in ("requests", "unrecovered", "rejected", "problems")}})
    from market_intel.store import StoreError
    try:
        sealed = store.finish_dataset(did, final, detail=detail)
    except StoreError:                                                   # the store's own rule disagrees: never COMPLETED / EMPTY by default
        final = "PARTIAL" if stats["ok_fetches"] else "FAILED"
        sealed = store.finish_dataset(did, final, detail=detail)
    anchor = archive.anchor(did, sealed["content_sha256"], records_sha256=sealed["records_sha256"], status=final, sealed_at=clock())
    if interrupt is not None:
        raise interrupt
    return {"dataset_id": did, "status": final, "stop": stop, **stats, "content_sha256": sealed["content_sha256"], "anchor": anchor,
            "plan_sha256": fresh["plan_sha256"], "approval_id": approval.approval_id}


def _run(r: dict, did: str, first: date, last: date, *, fetch_fn, store, archive, approval, clock, sleep_fn, budget, stats) -> Optional[str]:
    """The request loop. Returns the stop reason (None: every symbol was attempted). Each response is archived, recorded as a fetch row
    and only then parsed and ingested; nothing a provider returns is trusted before it is validated."""
    start, end = _et(first, 0), _et(last + timedelta(days=1), 0)
    for sym in r["symbols"]:
        endpoint = AB.ENDPOINT.format(symbol=sym)
        token, retries, seen = None, 0, set()
        while True:
            if not backfill_window_open(clock()):
                return "WINDOW_CLOSED"
            if stats["requests"] >= approval.max_requests:
                return "APPROVAL_REQUEST_CAP"
            ok, why = budget.allow()
            store.record_budget_event(budget.name, "ALLOW" if ok else "REFUSE", reason=why, per_minute=budget.bucket.per_minute,
                                      used_today=budget.day_count, dataset_id=did)
            if not ok:
                wait = budget.bucket.wait_s() if why == "RATE_LIMITED" else None
                if wait is not None and retries <= MAX_RETRIES:
                    retries += 1
                    sleep_fn(wait)
                    continue
                return f"BUDGET_{why}"
            stats["requests"] += 1
            params = AB.request_params(sym, r["timeframe"], start, end, r["feed"], r["adjustment"], token)
            started = clock()
            try:
                status, headers, body = fetch_fn(params)
            except Exception as exc:                                     # noqa: BLE001 - a transport failure is a fetch row, not a crash
                fstatus = "TIMEOUT" if isinstance(exc, TimeoutError) else "ERROR"
                store.record_fetch(FetchRecord(provider=AB.PROVIDER, endpoint=endpoint, started_at=started, status=fstatus,
                                               error=f"TRANSPORT:{type(exc).__name__}"[:300]), dataset_id=did, params=params)
                store.record_budget_event(budget.name, "RESPONSE", reason=fstatus, dataset_id=did)
                stats["failed_fetches"] += 1
                if retries < MAX_RETRIES:
                    retries += 1
                    sleep_fn(RETRY_SLEEP_S)
                    continue
                stats["unrecovered"] += 1
                break
            retrieved = clock()
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599 or not isinstance(body, (bytes, bytearray)) \
                    or not (headers is None or isinstance(headers, Mapping)):
                store.record_fetch(FetchRecord(provider=AB.PROVIDER, endpoint=endpoint, started_at=started, retrieved_at=retrieved,
                                               status="INVALID_RESPONSE", error="TRANSPORT_CONTRACT"), dataset_id=did, params=params)
                stats["failed_fetches"] += 1
                stats["unrecovered"] += 1
                break
            body = bytes(body)
            wait_s = retry_after_s(headers, retrieved)
            budget.record(status, retry_after_s=wait_s, access_denied=status in (401, 403))
            store.record_budget_event(budget.name, "RESPONSE", http_status=status, retry_after_s=wait_s, dataset_id=did)
            fid = uuid.uuid4().hex
            try:
                entry = archive.write(fid, body, provider=AB.PROVIDER, endpoint=endpoint, retrieved_at=retrieved)
            except OSError as exc:                                       # no raw evidence: nothing from this response is stored
                store.record_fetch(FetchRecord(provider=AB.PROVIDER, endpoint=endpoint, started_at=started, retrieved_at=retrieved, status="ERROR",
                                               http_status=status, error=f"ARCHIVE:{type(exc).__name__}"), dataset_id=did, params=params, fetch_id=fid)
                stats["failed_fetches"] += 1
                stats["unrecovered"] += 1
                return "ARCHIVE_ERROR"
            fstatus, obs, nxt, problems, page_problem = _http_status(status), [], None, [], None
            if fstatus == "OK":
                try:
                    obs, nxt, problems = AB.parse_page(body, symbol=sym, timeframe=r["timeframe"], feed=r["feed"], adjustment=r["adjustment"],
                                                       retrieved_at=retrieved, dataset_id=did, start_date=first, end_date=last)
                except Exception as exc:                                 # noqa: BLE001 - a parser crash makes the page invalid, not the run
                    obs, nxt, problems = [], None, [{"problem": f"PARSER_ERROR:{type(exc).__name__}"}]
                page_problem = next((x["problem"] for x in problems if x["problem"] in AB.PAGE_PROBLEMS or x["problem"].startswith("PARSER_ERROR")), None)
                if page_problem:
                    fstatus = "INVALID_RESPONSE"
            store.record_fetch(FetchRecord(provider=AB.PROVIDER, endpoint=endpoint, started_at=started, retrieved_at=retrieved, status=fstatus,
                                           http_status=status, latency_ms=max(0, int((retrieved - started).total_seconds() * 1000)), bytes=len(body),
                                           content_hash=entry["sha256"], retry_after_s=wait_s, error=page_problem or ""),
                               dataset_id=did, params=params, raw_path=entry["path"], fetch_id=fid)
            if fstatus == "ACCESS_DENIED":
                stats["failed_fetches"] += 1
                stats["unrecovered"] += 1
                return "ACCESS_DENIED"
            if fstatus in ("RATE_LIMITED", "HTTP_ERROR"):
                stats["failed_fetches"] += 1
                if (fstatus == "RATE_LIMITED" or status >= 500) and retries < MAX_RETRIES:
                    retries += 1                                         # the same request again (recovered only if it then succeeds)
                    until = budget.blocked_until
                    sleep_fn(max(0.0, (until - clock()).total_seconds()) if (fstatus == "RATE_LIMITED" and until) else RETRY_SLEEP_S)
                    continue
                stats["unrecovered"] += 1
                break                                                    # this symbol stays incomplete: the dataset can only be PARTIAL
            if fstatus == "INVALID_RESPONSE":
                stats["failed_fetches"] += 1
                stats["unrecovered"] += 1
                stats["problems"] += store.record_problems(did, problems, fetch_id=fid)
                break
            stats["ok_fetches"] += 1
            retries = 0
            if stats["rows_delivered"] + len(obs) + len(problems) > approval.max_rows:
                return "APPROVAL_ROW_CAP"                                # the page is archived and recorded, but not ingested
            stats["rows_delivered"] += len(obs) + len(problems)
            stats["problems"] += store.record_problems(did, problems, fetch_id=fid)
            res = store.ingest(did, obs, fetch_id=fid, on_reject="quarantine")
            for k in ("inserted", "duplicates", "revised", "absences", "rejected"):
                stats[k] += getattr(res, k)
            if not nxt:
                break
            if nxt in seen:
                return "PAGINATION_LOOP"
            seen.add(nxt)
            token = nxt
    return None


# ------------------------------------------------------------------------------------------------------------ CLI (offline)
def _parse_now(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("--now must be timezone-aware")
    return d.astimezone(timezone.utc)


def _request(a) -> BackfillRequest:
    syms = tuple(x.strip().upper() for x in (a.symbols or "").split(",") if x.strip())
    return BackfillRequest(symbols=syms, start=date.fromisoformat(a.start), end=date.fromisoformat(a.end), timeframe=a.timeframe, feed=a.feed,
                           adjustment=a.adjustment, extended_hours=a.extended_hours)


def main(argv: Optional[Sequence[str]] = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m market_intel.ingest", description="Stage 7.1 market-intelligence backfill planner (offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "dry-run", "execute"):
        sp = sub.add_parser(name)
        sp.add_argument("--symbols", required=True)
        sp.add_argument("--start", required=True)
        sp.add_argument("--end", required=True)
        sp.add_argument("--timeframe", required=True, choices=sorted(AB.TIMEFRAMES))
        sp.add_argument("--feed", required=True, help="iex or sip - always explicit")
        sp.add_argument("--adjustment", default="raw")
        sp.add_argument("--extended-hours", action="store_true")
        sp.add_argument("--now", help=argparse.SUPPRESS)
    sc = sub.add_parser("calendar")
    sc.add_argument("--start", required=True)
    sc.add_argument("--end", required=True)
    si = sub.add_parser("inspect")
    si.add_argument("--db", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        if a.cmd == "calendar":
            print(json.dumps(coverage(date.fromisoformat(a.start), date.fromisoformat(a.end)), indent=1), file=out)
            return EXIT_OK
        if a.cmd == "inspect":
            from market_intel.store import MarketIntelStore
            path = Path(a.db)
            if not path.is_file():
                print(json.dumps({"store": "NOT_FOUND", "path": str(path), "created": False}), file=out)
                return EXIT_REFUSED
            print(json.dumps(MarketIntelStore(path).status_counts(), indent=1), file=out)
            return EXIT_OK
        req = _request(a)
        now = _parse_now(getattr(a, "now", None))
    except ValueError as exc:
        print(json.dumps({"error": "USAGE", "detail": str(exc)[:200]}), file=out)
        return EXIT_USAGE
    p = plan(req, now)
    if a.cmd == "execute":
        print(json.dumps({"executed": False, "reason": "EXECUTE_NOT_ENABLED", "detail": "Stage 7.1 is code-only: a real backfill needs a separate "
                          "operator approval (and a SIP request its own); no request was sent and nothing was written", "plan": p}, indent=1), file=out)
        return EXIT_EXECUTE_REFUSED
    doc = dict(p)
    if a.cmd == "dry-run":
        doc["preview"] = preview(p) if p["valid"] else []
        doc["would_write"] = {"store": "data/market_intel.db (not written by a dry run)", "rows": p["estimate"]["rows_total"]}
    print(json.dumps(doc, indent=1), file=out)
    return EXIT_OK if p["valid"] else EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
