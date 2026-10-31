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

run_backfill() is the library path a FUTURE approved run will use: it requires a BackfillApproval bound to the plan's hash (and
sip_approved for SIP), an injected fetch function, the backfill window (outside 07:00-20:00 New York time on market days), the budget,
the raw archive and the store. Stage 7.1 ships no fetch function and the CLI never builds an approval, so no real request can be made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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
BYTES_PER_ROW_ESTIMATE = 950              # one envelope row incl. indexes: measured 915-950 on daily-bar fixtures (test_C05)
MAX_RETRIES = 2
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


@dataclass(frozen=True)
class BackfillApproval:
    """What a future approved run must present. Stage 7.1 never constructs one outside tests."""
    ref: str
    plan_sha256: str
    sip_approved: bool = False
    granted_by: str = "operator"


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
            validate_symbol(s)
        except ValueError:
            out.append({"code": "INVALID_SYMBOL", "detail": str(s)[:12]})
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
    used = None
    if store is not None and store.exists():
        used = store.budget_used("ALPACA_BACKFILL", CAL.session(now)["session_date"])
    body = {"plan_version": PLAN_VERSION, "request": req.as_dict(), "coverage": cov, "estimate": est}
    sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    return {**body, "valid": not refusals, "refusals": refusals, "network": False, "writes": False, "paid": False,
            "requires_approval": ["REAL_BACKFILL"] + (["SIP_FEED"] if req.feed == "sip" else []),
            "budget": {"provider": "ALPACA_BACKFILL", "per_minute": per_min, "window_rule": "outside 07:00-20:00 New York time on market days",
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
def run_backfill(p: dict, *, fetch_fn: Callable[[dict], Tuple[int, dict, bytes]], store, archive, approval: BackfillApproval,
                 clock: Callable[[], datetime], sleep_fn: Callable[[float], None], budget: Optional[ProviderBudget] = None) -> dict:
    """Fetch, archive and ingest a VALID, APPROVED plan. fetch_fn(params) -> (http_status, headers, body) is injected; Stage 7.1 has none."""
    if not p.get("valid"):
        raise PermissionError("refused plan: " + ", ".join(r["code"] for r in p.get("refusals", [])))
    if not isinstance(approval, BackfillApproval) or not approval.ref or approval.plan_sha256 != p["plan_sha256"]:
        raise PermissionError("a real backfill needs an operator approval bound to this exact plan")
    r = p["request"]
    if r["feed"] == "sip" and not approval.sip_approved:
        raise PermissionError("a SIP request needs its own approval")
    if not backfill_window_open(clock()):
        raise PermissionError("outside the backfill window (no backfill 07:00-20:00 New York time on a market day)")
    from market_intel.store import DatasetSpec
    budget = budget or ProviderBudget("ALPACA_BACKFILL", p["budget"]["per_minute"], clock)
    kind = AB.TIMEFRAMES[r["timeframe"]]
    did = store.create_dataset(DatasetSpec(job_kind="BACKFILL", kind=kind, provider=AB.PROVIDER, feed=r["feed"], adjustment=r["adjustment"],
                                           symbols=r["symbols"], timeframe=r["timeframe"], start_date=date.fromisoformat(r["start"]),
                                           end_date=date.fromisoformat(r["end"]), request=r, plan_sha256=p["plan_sha256"], approval_ref=approval.ref))
    start, end = _et(date.fromisoformat(r["start"]), 0), _et(date.fromisoformat(r["end"]) + timedelta(days=1), 0)
    stats = {"ok_fetches": 0, "failed_fetches": 0, "unrecovered": 0, "inserted": 0, "duplicates": 0, "revised": 0, "problems": []}
    stop = None
    for sym in r["symbols"]:
        token, retries = None, 0
        while stop is None:
            if not backfill_window_open(clock()):
                stop = "WINDOW_CLOSED"
                break
            ok, why = budget.allow()
            store.record_budget_event(budget.name, "ALLOW" if ok else "REFUSE", reason=why, per_minute=budget.bucket.per_minute,
                                      used_today=budget.day_count, dataset_id=did)
            if not ok:
                wait = budget.bucket.wait_s() if why == "RATE_LIMITED" else None
                if wait is not None and retries <= MAX_RETRIES:
                    retries += 1
                    sleep_fn(wait)
                    continue
                stop = f"BUDGET_{why}"
                break
            params = AB.request_params(sym, r["timeframe"], start, end, r["feed"], r["adjustment"], token)
            started = clock()
            status, headers, body = fetch_fn(params)
            retrieved = clock()
            retry_after = (headers or {}).get("retry-after")
            budget.record(status, retry_after_s=float(retry_after) if retry_after else None, access_denied=status in (401, 403))
            store.record_budget_event(budget.name, "RESPONSE", http_status=status, retry_after_s=float(retry_after) if retry_after else None,
                                      dataset_id=did)
            fid = uuid.uuid4().hex
            entry = archive.write(fid, body or b"", provider=AB.PROVIDER, endpoint=AB.ENDPOINT.format(symbol=sym.upper()), retrieved_at=retrieved)
            fstatus = "OK" if status == 200 else ("RATE_LIMITED" if status == 429 else ("ACCESS_DENIED" if status in (401, 403) else "HTTP_ERROR"))
            store.record_fetch(FetchRecord(provider=AB.PROVIDER, endpoint=AB.ENDPOINT.format(symbol=sym.upper()), started_at=started, retrieved_at=retrieved,
                                           status=fstatus, http_status=status, latency_ms=int((retrieved - started).total_seconds() * 1000),
                                           bytes=len(body or b""), content_hash=entry["sha256"]),
                               dataset_id=did, params=params, raw_path=entry["path"], fetch_id=fid)
            if fstatus == "ACCESS_DENIED":
                stats["failed_fetches"] += 1
                stats["unrecovered"] += 1
                stop = "ACCESS_DENIED"
                break
            if fstatus != "OK":
                stats["failed_fetches"] += 1
                if fstatus == "RATE_LIMITED" and retries < MAX_RETRIES:
                    retries += 1                                                 # the same request again after the back-off (recovered if it succeeds)
                    until = budget.blocked_until
                    sleep_fn(max(0.0, (until - clock()).total_seconds()) if until else 1.0)
                    continue
                stats["unrecovered"] += 1
                break                                                            # this symbol stays incomplete: the dataset can only be PARTIAL
            stats["ok_fetches"] += 1
            retries = 0
            obs, token, problems = AB.parse_page(body, symbol=sym, timeframe=r["timeframe"], feed=r["feed"], adjustment=r["adjustment"],
                                                 retrieved_at=retrieved, dataset_id=did)
            stats["problems"] += [{**x, "symbol": sym} for x in problems]
            res = store.ingest(did, obs, fetch_id=fid)
            stats["inserted"] += res.inserted
            stats["duplicates"] += res.duplicates
            stats["revised"] += res.revised
            if not token:
                break
        if stop:
            break
    rows = stats["inserted"] + stats["revised"]
    if stats["ok_fetches"] == 0:
        final = "FAILED"
    elif stop or stats["unrecovered"] or stats["problems"]:
        final = "PARTIAL"
    elif rows == 0 and stats["duplicates"] == 0:
        final = "EMPTY"
    else:
        final = "COMPLETED"
    sealed = store.finish_dataset(did, final, detail=json.dumps({"stop": stop, "problems": len(stats["problems"])}), duplicates=stats["duplicates"])
    return {"dataset_id": did, "status": final, "stop": stop, **stats, "content_sha256": sealed["content_sha256"]}


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
