"""
market_intel/sources/cli.py - Stage 7.2: the OFFLINE command line of the event and reference adapters (DESIGN_72 section 5).

    python -m market_intel.sources.cli describe                               the adapters, their kinds, credentials and approvals
    python -m market_intel.sources.cli request  --source fred --series DGS10 --start 2025-01-01 --end 2025-12-31
    python -m market_intel.sources.cli parse    --source halts --file saved.xml --retrieved-at 2025-12-06T15:00:00Z
    python -m market_intel.sources.cli fetch    ...                           always refused in Stage 7.2 (exit 3)

No command makes a network request, writes a file or a database, or reads a credential's value (tested with network access blocked and
the evidence tripwire armed). `request` prints what a transport WOULD send - never a key - and NOT_CONFIGURED when its credential is
missing. `parse` normalises a response body saved earlier, read-only, so an adapter can be checked against a real payload before any
approved run. Exit codes: 0 done, 1 usage error, 2 the body is unusable (a page problem), 3 fetch refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from market_intel.sources import alpaca_news, alpaca_snapshots, earnings_av, fred, halts, sec_facts, sec_filings
from market_intel.sources.common import PAGE_PROBLEMS
from market_intel.store import natural_key

ADAPTERS = {
    "alpaca_news": {"module": alpaca_news, "kind": "NEWS", "credential": "ALPACA_API_KEY", "approvals": ["REAL_FETCH"]},
    "alpaca_snapshots": {"module": alpaca_snapshots, "kind": "QUOTE_SNAPSHOT", "credential": "ALPACA_API_KEY", "approvals": ["REAL_FETCH", "SIP_FEED for sip"]},
    "sec_filings": {"module": sec_filings, "kind": "FILING", "credential": "SEC_USER_AGENT", "approvals": ["REAL_FETCH"]},
    "sec_facts": {"module": sec_facts, "kind": "FACT", "credential": "SEC_USER_AGENT", "approvals": ["REAL_FETCH"]},
    "fred": {"module": fred, "kind": "MACRO", "credential": "FRED_API_KEY", "approvals": ["REAL_FETCH", "FRED_SERIES_TERMS"]},
    "halts": {"module": halts, "kind": "HALT", "credential": None, "approvals": ["REAL_FETCH", "NASDAQ_RSS_TERMS"]},
    "earnings_av": {"module": earnings_av, "kind": "EARNINGS", "credential": "ALPHAVANTAGE_API_KEY", "approvals": ["REAL_FETCH", "ALPHAVANTAGE_KEY_AND_TERMS"]},
}
EXIT_OK, EXIT_USAGE, EXIT_UNUSABLE, EXIT_FETCH_REFUSED = 0, 1, 2, 3


def _utc(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return d.astimezone(timezone.utc)


def _symbols(s: Optional[str]):
    return [x.strip().upper() for x in (s or "").split(",") if x.strip()]


def describe() -> dict:
    return {"adapters": [{"source": name, "kind": a["kind"], "provider": a["module"].PROVIDER, "host": a["module"].HOST,
                          "credential": a["credential"], "approvals_needed": a["approvals"]} for name, a in ADAPTERS.items()],
            "transport": "none in Stage 7.2 (fetch is refused)", "network": False}


def build_request(a) -> dict:
    s = a.source
    if s == "alpaca_news":
        return alpaca_news.request(_symbols(a.symbols), _utc(a.start), _utc(a.end))
    if s == "alpaca_snapshots":
        return alpaca_snapshots.request(_symbols(a.symbols), a.feed)
    if s == "sec_filings":
        return sec_filings.request(a.cik)
    if s == "sec_facts":
        return sec_facts.request(a.cik)
    if s == "fred":
        return fred.request(a.series, observation_start=date.fromisoformat(a.start), observation_end=date.fromisoformat(a.end))
    if s == "halts":
        return halts.request()
    return earnings_av.request(horizon=a.horizon or "3month", configured=False)     # config declares no Alpha Vantage key: never READY here


def parse_body(a, body: bytes) -> tuple:
    s, t = a.source, _utc(a.retrieved_at)
    if s == "alpaca_news":
        return alpaca_news.parse_page(body, symbols=_symbols(a.symbols), retrieved_at=t)
    if s == "alpaca_snapshots":
        return alpaca_snapshots.parse(body, symbols=_symbols(a.symbols), feed=a.feed, retrieved_at=t)
    if s == "sec_filings":
        return sec_filings.parse(body, ticker=a.ticker, cik=a.cik, retrieved_at=t)
    if s == "sec_facts":
        return sec_facts.parse(body, ticker=a.ticker, cik=a.cik, retrieved_at=t)
    if s == "fred":
        return fred.parse(body, series_id=a.series, retrieved_at=t)
    if s == "halts":
        return halts.parse(body, retrieved_at=t)
    return earnings_av.parse(body, retrieved_at=t, symbols=_symbols(a.symbols) or None)


def main(argv: Optional[Sequence[str]] = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m market_intel.sources.cli", description="Stage 7.2 adapters (offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("describe")
    for name in ("request", "parse", "fetch"):
        sp = sub.add_parser(name)
        sp.add_argument("--source", required=True, choices=sorted(ADAPTERS))
        for opt in ("--symbols", "--start", "--end", "--feed", "--cik", "--ticker", "--series", "--horizon", "--file", "--retrieved-at"):
            sp.add_argument(opt)
        sp.add_argument("--limit", type=int, default=20)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    if a.cmd == "describe":
        print(json.dumps(describe(), indent=1), file=out)
        return EXIT_OK
    if a.cmd == "fetch":
        print(json.dumps({"fetched": False, "reason": "FETCH_NOT_ENABLED", "detail": "Stage 7.2 ships no transport: a real request needs the operator "
                          "approvals listed by `describe` (keys, terms) and a later stage"}), file=out)
        return EXIT_FETCH_REFUSED
    try:
        if a.cmd == "request":
            print(json.dumps(build_request(a), indent=1, default=str), file=out)
            return EXIT_OK
        if not a.file or not a.retrieved_at:
            raise ValueError("parse needs --file and --retrieved-at")
        body = Path(a.file).read_bytes()
        obs, token, problems = parse_body(a, body)
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"error": "USAGE", "detail": str(exc)[:200]}), file=out)
        return EXIT_USAGE
    page = [p for p in problems if str(p.get("problem", "")).split(":")[0] in PAGE_PROBLEMS]
    print(json.dumps({"source": a.source, "observations": len(obs), "next_page_token": token, "problems": problems[: a.limit], "problem_count": len(problems),
                      "rows": [{"natural_key": natural_key(o), "status": o.status, "observed_at": o.observed_at.isoformat() if o.observed_at else None,
                                "available_at": o.available_at.isoformat(), "flags": list(o.quality_flags)} for o in obs[: a.limit]],
                      "network": False, "writes": False}, indent=1), file=out)
    return EXIT_UNUSABLE if page else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
