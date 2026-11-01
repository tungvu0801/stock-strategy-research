"""
Stage 7.2 - the market-intelligence event and reference adapters, parse-only (DESIGN_72_MARKET_INTEL_SOURCES.md). Synthetic fixtures
only; no network (the conftest socket block and evidence tripwire, plus explicit guards); temporary databases; nothing written outside
tmp_path.

  A01-A07 one per adapter (news, snapshots, SEC filings, SEC facts, FRED, halts, earnings): the request (never a credential;
          NOT_CONFIGURED without one), the mapping (explicit times, availability = our retrieval, natural keys), pagination, malformed
          rows, error envelopes and strict page shapes, absences, quality codes.
  N01-N03 news: attribution (tiers equal to evidence.rules, deny hosts metadata only, snippets), dedup (same story / different outlets,
          revisions, near duplicates, different stories from one template, determinism), entities (methods, confidences, common-word
          tickers, roles, catalyst eligibility).
  P01-P02 the Stage 7.1 store, reused unchanged: every adapter's rows ingest into FIXTURE datasets; point in time, revisions (news
          edits, halt resumptions), absences, feed isolation, earnings date moves through the store-backed provider, integrity
          VERIFIED; value_quality.pit visibility reused.
  E01     the earnings provider stays unwired (get_earnings_provider unchanged) and NOT_CONFIGURED without a key.
  X01-X04 invariants: no adapter ever raises (fuzzed bodies); every row has explicit times and available_at == retrieved_at; no request
          carries a credential; the evidence tripwire is never hit and no socket is opened.
  C01     the CLI: offline, read-only; fetch always refused.
  S01     isolation: a closed registry of the 7.2 modules and their imports; nothing outside market_intel imports them.
  F01-F03 scope (freeze bookkeeping).
"""
from __future__ import annotations

import ast
import io
import json
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import freeze_ledger as FL
import value_quality as VQ
from evidence.rules import tier_for
from evidence.sources import sec_edgar as EDGAR
from market_intel import calendar as CAL
from market_intel.models import Observation
from market_intel.news import attribution as AT
from market_intel.news import dedup as DD
from market_intel.news import entities as EN
from market_intel.sources import alpaca_news as NEWS
from market_intel.sources import alpaca_snapshots as SNAP
from market_intel.sources import cli as CLI
from market_intel.sources import common as C
from market_intel.sources import earnings_av as EARN
from market_intel.sources import fred as FRED
from market_intel.sources import halts as HALTS
from market_intel.sources import sec_facts as FACTS
from market_intel.sources import sec_filings as FILINGS
from market_intel.store import DatasetSpec, FeedRequired, MarketIntelStore, content_hash, natural_key, ts
from value_quality import pit

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
RET = datetime(2025, 12, 6, 15, 0, tzinfo=UTC)                 # Saturday 10:00 New York: the retrieval time of every fixture
START, END = datetime(2025, 12, 1, tzinfo=UTC), datetime(2025, 12, 6, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def codes(parsed):
    return [p["problem"] for p in parsed[2]]


# ------------------------------------------------------------------------------------------------------------ fixtures
def art(id_, headline="Nvidia raises full-year guidance after record quarter", *, created="2025-12-01T13:30:00Z", updated=None, symbols=("NVDA",),
        source="Example Wire", url="https://www.benzinga.com/news/25/12/nvda-guidance/?utm_source=feed&ref=x", summary="Nvidia said revenue rose.",
        author="A. Writer", **extra):
    a = {"id": id_, "headline": headline, "author": author, "created_at": created, "updated_at": updated or created, "summary": summary, "url": url,
         "symbols": list(symbols), "source": source, "images": []}
    a.update(extra)
    return a


def news_page(arts, token=None):
    return json.dumps({"news": arts, "next_page_token": token}).encode()


def snap(bp=100.0, ap=100.04, qt="2025-12-05T20:59:59.123456789Z", tt="2025-12-05T20:59:58Z"):
    bar = {"t": "2025-12-05T20:59:00Z", "o": 100.0, "h": 100.1, "l": 99.9, "c": 100.02, "v": 1200, "n": 12, "vw": 100.01}
    return {"latestQuote": {"t": qt, "bp": bp, "bs": 3, "ap": ap, "as": 2, "bx": "V", "ax": "V", "c": ["R"], "z": "C"},
            "latestTrade": {"t": tt, "p": 100.02, "s": 100, "x": "V", "c": ["@"], "i": 1, "z": "C"},
            "minuteBar": bar, "dailyBar": {**bar, "t": "2025-12-05T05:00:00Z"}, "prevDailyBar": {**bar, "t": "2025-12-04T05:00:00Z"}}


def frow(acc="0000320193-25-000123", filed="2025-10-30", accepted="2025-10-30T16:30:12.000Z", form="8-K", items="2.02,9.01", report="2025-10-30",
         primary="a8-k.htm"):
    return {"accessionNumber": acc, "filingDate": filed, "reportDate": report, "acceptanceDateTime": accepted, "form": form, "items": items,
            "primaryDocument": primary}


def submissions(rows, cik="320193"):
    cols = {c: [r[c] for r in rows] for c in FILINGS.COLUMNS}
    return json.dumps({"cik": cik, "name": "Apple Inc.", "tickers": ["AAPL"], "filings": {"recent": cols, "files": []}}).encode()


CONCEPT = VQ.ALL_CONCEPTS[0]


def fact(val=1000, end="2025-09-27", filed="2025-10-31", accn="0000320193-25-000079", fy=2025, fp="FY", form="10-K", start="2024-09-29"):
    r = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:
        r["start"] = start
    return r


def companyfacts(rows, cik=320193, extra=None):
    doc = {"cik": cik, "entityName": "Apple Inc.", "facts": {"us-gaap": {CONCEPT: {"label": CONCEPT, "description": "", "units": {"USD": rows}},
                                                                        "SomethingUndeclared": {"label": "x", "units": {"USD": [fact(val=7)]}}},
                                                            "dei": {"EntityCommonStockSharesOutstanding": {"label": "shares", "units": {"shares": [
                                                                fact(val=15_000_000_000, end="2025-10-17", start=None, fp="FY")]}}}}}
    doc.update(extra or {})
    return json.dumps(doc).encode()


def fobs(d, v, rs, re_="9999-12-31"):
    return {"realtime_start": rs, "realtime_end": re_, "date": d, "value": v}


def fred_page(obs, *, count=None, offset=0, limit=100000):
    return json.dumps({"realtime_start": "1776-07-04", "realtime_end": "9999-12-31", "observation_start": "2025-12-01", "observation_end": "2025-12-05",
                       "units": "Percent", "output_type": 1, "file_type": "json", "order_by": "observation_date", "sort_order": "asc",
                       "count": len(obs) if count is None else count, "offset": offset, "limit": limit, "observations": obs}).encode()


def hitem(sym="ABCD", hd="12/01/2025", ht="10:15:30", reason="LUDP", rd="", rq="", rt="", name="Abcd Corp", market="NASDAQ", price=""):
    f = {"HaltDate": hd, "HaltTime": ht, "IssueSymbol": sym, "IssueName": name, "Market": market, "ReasonCode": reason, "PauseThresholdPrice": price,
         "ResumptionDate": rd, "ResumptionQuoteTime": rq, "ResumptionTradeTime": rt}
    return f"<item><title>{sym}</title>" + "".join(f"<ndaq:{k}>{v}</ndaq:{k}>" for k, v in f.items()) + f"<guid>{sym}-{hd}-{ht}</guid></item>"


def rss(*items):
    return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><title>Trade Halts</title>'
            + "".join(items) + "</channel></rss>").encode()


AV_CSV = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"
          "AAPL,Apple Inc,2026-01-29,2025-12-31,2.35,USD\r\nMSFT,Microsoft Corp,2026-01-28,2025-12-31,,USD\r\n")


# ============================================================================================================ adapters (A)
def test_A01_alpaca_news_maps_articles_with_our_availability_attribution_and_strict_pages():
    r = NEWS.request(["nvda", "AMD"], START, END)
    assert r["params"] == {"symbols": "AMD,NVDA", "start": "2025-12-01T00:00:00Z", "end": "2025-12-06T00:00:00Z", "sort": "asc", "limit": 50,
                           "include_content": "false"} and (r["credential"], r["status"], r["network"], r["endpoint"]) == ("ALPACA_API_KEY", "READY", False, "/v1beta1/news")
    with pytest.raises(ValueError):
        NEWS.request([f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(51)], START, END)
    obs, token, problems = NEWS.parse_page(news_page([art(101), art(102, source="Reuters", url="https://www.reuters.com/x", content="FULL TEXT")], "abc123"),
                                           symbols=["NVDA"], retrieved_at=RET)
    assert token == "abc123" and problems == [] and len(obs) == 2
    a, b = obs
    created = datetime(2025, 12, 1, 13, 30, tzinfo=UTC)
    assert (a.observed_at, a.published_at, a.retrieved_at, a.available_at) == (created, created, RET, RET)           # known when WE saw it
    assert natural_key(a) == "NEWS|||ALPACA|none|none|2025-12-01T13:30:00.000000Z|101" and a.symbol is None and a.session == "PRE"
    assert (a.tier, a.source_url, a.payload["symbols"], a.payload["host"]) == (4, "https://benzinga.com/news/25/12/nvda-guidance", ("NVDA",), "benzinga.com")
    assert (b.tier, b.payload["deny_host"]) == (3, True) and "DENY_HOST_LINK" in b.quality_flags and "FULL TEXT" not in json.dumps(b.payload)   # metadata only
    pages = {"ERROR_ENVELOPE": {"message": "forbidden"}, "UNEXPECTED_SHAPE": {"news": []}, "BAD_PAGE_TOKEN": {"news": [], "next_page_token": ""},
             "PAGE_TOO_LARGE": {"news": [art(i) for i in range(51)], "next_page_token": None}}
    for code, doc in pages.items():
        assert codes(NEWS.parse_page(json.dumps(doc).encode(), symbols=["NVDA"], retrieved_at=RET))[0] == code
    assert codes(NEWS.parse_page(b"<html>", symbols=["NVDA"], retrieved_at=RET)) == ["INVALID_JSON"]
    rows = [art("x"), art(2, created="2025-12-01T13:30:00"), art(3, created="2025-11-20T13:30:00Z"), art(4, symbols=("AAPL",)), art(5, foo=1)]
    assert codes(NEWS.parse_page(news_page(rows), symbols=["NVDA"], retrieved_at=RET, start=START, end=END)) == [
        "MALFORMED_ROW", "BAD_TIME", "OUT_OF_RANGE", "OUT_OF_SCOPE", "MALFORMED_ROW"]
    fut = NEWS.parse_page(news_page([art(6, created=(RET + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"), headline=" ")]), symbols=["NVDA"],
                          retrieved_at=RET)[0][0]
    assert {"FUTURE_DATED", "FUTURE_OBSERVED", "EMPTY_HEADLINE"} <= set(fut.quality_flags)                           # flagged, and blocking
    assert fut.available_at == fut.retrieved_at == RET                                                            # still OUR time, never the article's
    bf = NEWS.parse_page(news_page([art(7)]), symbols=["NVDA"], retrieved_at=RET, backfilled=True)[0][0]
    assert bf.backfilled is True and "BACKFILLED" in bf.quality_flags and bf.available_at == bf.retrieved_at == RET


def test_A02_alpaca_snapshots_keep_feeds_apart_derive_sessions_and_record_missing_symbols_as_absences():
    with pytest.raises(ValueError, match="feed"):
        SNAP.request(["AAPL"], "none")
    r = SNAP.request(["msft", "AAPL"], "sip")
    assert r["params"] == {"symbols": "AAPL,MSFT", "feed": "sip"} and "SIP_FEED" in r["requires_approval"]
    obs, token, problems = SNAP.parse(json.dumps({"AAPL": snap()}).encode(), symbols=["AAPL", "MSFT"], feed="iex", retrieved_at=RET)
    assert token is None and problems == []
    aapl, msft = obs
    assert (aapl.status, aapl.observed_at, aapl.session, aapl.session_date, aapl.feed) == (
        "AVAILABLE", datetime(2025, 12, 5, 20, 59, 59, 123456, tzinfo=UTC), "REG", date(2025, 12, 5), "iex")       # nanoseconds truncated
    assert (aapl.payload["bid"], aapl.payload["ask"], aapl.payload["last_price"], aapl.payload["daily_bar"]["close"]) == (100.0, 100.04, 100.02, 100.02)
    assert aapl.quality_flags == () and aapl.available_at == RET
    assert (msft.status, msft.payload, msft.observed_at, msft.available_at) == ("UNAVAILABLE", {}, RET, RET)        # we looked: nothing - never a zero
    crossed = SNAP.parse(json.dumps({"AAPL": snap(bp=101.0, ap=100.0)}).encode(), symbols=["AAPL"], feed="iex", retrieved_at=RET)[0][0]
    future = SNAP.parse(json.dumps({"AAPL": snap(qt="2025-12-06T15:00:30Z")}).encode(), symbols=["AAPL"], feed="iex", retrieved_at=RET)[0][0]
    assert "CROSSED" in crossed.quality_flags and "FUTURE_OBSERVED" in future.quality_flags
    assert codes(SNAP.parse(json.dumps({"AAPL": snap(), "TSLA": snap()}).encode(), symbols=["AAPL"], feed="iex", retrieved_at=RET)) == ["SYMBOL_MISMATCH"]
    assert codes(SNAP.parse(json.dumps({"code": 40110000, "message": "x"}).encode(), symbols=["AAPL"], feed="iex", retrieved_at=RET)) == ["ERROR_ENVELOPE"]
    bad = snap()
    del bad["latestQuote"]["ap"]
    assert codes(SNAP.parse(json.dumps({"AAPL": bad, "MSFT": {**snap(), "extra": {}}}).encode(), symbols=["AAPL", "MSFT"], feed="iex", retrieved_at=RET)) == [
        "MALFORMED_ROW", "MALFORMED_ROW"]
    none = SNAP.parse(json.dumps({"AAPL": None}).encode(), symbols=["AAPL"], feed="iex", retrieved_at=RET)[0][0]
    assert none.status == "UNAVAILABLE"
    iex = SNAP.parse(json.dumps({"AAPL": snap()}).encode(), symbols=["AAPL"], feed="iex", retrieved_at=RET)[0][0]
    sip = SNAP.parse(json.dumps({"AAPL": snap()}).encode(), symbols=["AAPL"], feed="sip", retrieved_at=RET)[0][0]
    assert natural_key(iex) != natural_key(sip) and (iex.feed, sip.feed) == ("iex", "sip")                       # feeds never share a fact


def test_A03_sec_filings_reuse_the_evidence_urls_and_record_the_acceptance_time_assumption():
    assert FILINGS.request("320193", user_agent="")["status"] == "NOT_CONFIGURED"                                 # SEC requires a User-Agent
    r = FILINGS.request("320193", user_agent="Jane Doe jane@example.com")
    assert (r["status"], r["host"], r["endpoint"], r["params"]) == ("READY", "data.sec.gov", "/submissions/CIK0000320193.json", {})
    assert EDGAR.submissions_url("0000320193") == "https://data.sec.gov" + r["endpoint"]                            # the evidence adapter's URL, reused
    rows = [frow(), frow(acc="0000320193-25-000124", filed="2025-11-24", accepted="2025-11-21T18:05:00.000Z", form="10-Q", items="", report="2025-09-27"),
            frow(acc="0000320193-25-000125", filed="2025-11-20", accepted="2025-11-21T09:00:00.000Z", form="8-K", items="8.01")]
    obs, _, problems = FILINGS.parse(submissions(rows), ticker="AAPL", cik="320193", retrieved_at=RET)
    assert problems == [] and [o.source_identifier for o in obs] == ["0000320193-25-000123", "0000320193-25-000124", "0000320193-25-000125"]
    k8, q10, odd = obs
    assert k8.observed_at == datetime(2025, 10, 30, 20, 30, 12, tzinfo=UTC) == k8.published_at                  # 16:30:12 New York (EDT)
    assert (k8.payload["earnings_release"], k8.payload["items"], k8.payload["acceptance_tz"], k8.tier) == (True, ("2.02", "9.01"), "ASSUMED_AMERICA_NEW_YORK", 1)
    assert k8.source_url == EDGAR.index_url("0000320193", "0000320193-25-000123") and k8.available_at == RET
    assert (q10.payload["acceptance_check"], odd.payload["acceptance_check"]) == ("CONSISTENT", "INCONSISTENT")   # after 17:30 -> next business day
    assert [o.payload["form"] for o in FILINGS.parse(submissions(rows), ticker="AAPL", cik="320193", retrieved_at=RET, forms=["10-Q"])[0]] == ["10-Q"]
    assert codes(FILINGS.parse(submissions(rows, cik="789019"), ticker="AAPL", cik="320193", retrieved_at=RET)) == ["SYMBOL_MISMATCH"]
    uneven = json.loads(submissions(rows))
    uneven["filings"]["recent"]["form"].append("8-K")
    assert codes(FILINGS.parse(json.dumps(uneven).encode(), ticker="AAPL", cik="320193", retrieved_at=RET)) == ["UNEXPECTED_SHAPE"]
    assert codes(FILINGS.parse(submissions([frow(acc="123"), frow(accepted="yesterday")]), ticker="AAPL", cik="320193", retrieved_at=RET)) == [
        "MALFORMED_ROW", "BAD_TIME"]


def test_A04_sec_facts_read_declared_concepts_keep_every_vintage_and_reuse_the_pit_visibility_rule():
    rows = [fact(), fact(val=1100, accn="0000320193-26-000010", filed="2026-01-30", fp="FY", form="10-K/A"),     # a restatement: its own fact
            fact(val=900, end="2024-09-28", filed="2024-11-01", accn="0000320193-24-000123", fy=2024, start="2023-10-01"),
            fact(val="1000"), fact(end="2025-13-01")]
    obs, _, problems = FACTS.parse(companyfacts(rows), ticker="AAPL", cik="320193", retrieved_at=RET)
    assert [p["problem"] for p in problems] == ["MALFORMED_ROW", "BAD_TIME"]
    assert {o.payload["concept"] for o in obs} == {CONCEPT, "EntityCommonStockSharesOutstanding"}               # undeclared concepts are not read
    main, restated, old = [o for o in obs if o.payload["concept"] == CONCEPT]
    assert main.observed_at == datetime(2025, 9, 27, 4, 0, tzinfo=UTC) and main.payload["time_precision"] == "DATE_ONLY" and main.available_at == RET
    assert natural_key(main) != natural_key(restated) and (main.payload["value"], restated.payload["value"]) == (1000, 1100)
    assert (main.payload["visible_from"], main.payload["visibility"]) == ("2025-11-03", "PIT_RULE")               # filed Fri 10-31: Monday's session
    assert (old.payload["visible_from"], old.payload["visibility"]) == (None, "CALENDAR_NOT_COVERED")             # 2024: never guessed
    assert main.tier == 1 and main.symbol == "AAPL" and main.payload["unit"] == "USD"
    assert codes(FACTS.parse(companyfacts(rows, cik=789019), ticker="AAPL", cik="320193", retrieved_at=RET)) == ["SYMBOL_MISMATCH"]
    assert codes(FACTS.parse(json.dumps({"cik": 320193, "facts": {"us-gaap": {CONCEPT: {"units": []}}}}).encode(), ticker="AAPL", cik="320193",
                             retrieved_at=RET)) == ["UNEXPECTED_SHAPE"]


def test_A05_fred_keeps_every_alfred_vintage_pages_by_offset_and_reads_a_dot_as_an_absence():
    assert FRED.request("DGS10", observation_start=date(2025, 12, 1), observation_end=date(2025, 12, 5), configured=False)["status"] == "NOT_CONFIGURED"
    r = FRED.request("DGS10", observation_start=date(2025, 12, 1), observation_end=date(2025, 12, 5), configured=True)
    assert r["status"] == "READY" and r["credential"] == "FRED_API_KEY" and not [k for k in r["params"] if "key" in k]
    with pytest.raises(ValueError):
        FRED.request("dgs10 ", observation_start=date(2025, 12, 1), observation_end=date(2025, 12, 5), configured=True)
    page = fred_page([fobs("2025-12-01", "4.10", "2025-12-02", "2025-12-04"), fobs("2025-12-01", "4.12", "2025-12-05"), fobs("2025-12-02", ".", "2025-12-03")],
                     count=5, limit=3)
    obs, token, problems = FRED.parse(page, series_id="DGS10", retrieved_at=RET)
    assert token == "3" and problems == []
    v1, v2, dot = obs
    assert (v1.series_id, v1.status, v1.payload["value"], v2.payload["value"]) == ("DGS10", "AVAILABLE", 4.10, 4.12)
    assert natural_key(v1) != natural_key(v2) and (v1.source_identifier, v2.source_identifier) == ("vintage:2025-12-02", "vintage:2025-12-05")
    assert (dot.status, dot.payload) == ("UNAVAILABLE", {}) and v1.observed_at == datetime(2025, 12, 1, 5, 0, tzinfo=UTC)
    assert FRED.parse(fred_page([fobs("2025-12-03", "4.0", "2025-12-04")], count=4, offset=3, limit=3), series_id="DGS10", retrieved_at=RET)[1] is None
    assert codes(FRED.parse(json.dumps({"error_code": 400, "error_message": "Bad Request."}).encode(), series_id="DGS10", retrieved_at=RET)) == ["ERROR_ENVELOPE"]
    assert codes(FRED.parse(json.dumps({"observations": []}).encode(), series_id="DGS10", retrieved_at=RET)) == ["UNEXPECTED_SHAPE"]
    assert codes(FRED.parse(fred_page([fobs("2025-12-01", "1", "2025-12-02")] * 2, limit=1), series_id="DGS10", retrieved_at=RET)) == ["PAGE_TOO_LARGE"]
    assert codes(FRED.parse(fred_page([fobs("2025-12-01", "abc", "2025-12-02"), fobs("2025-12-01", "1", "2025-12-05", "2025-12-02"),
                                       {**fobs("2025-12-01", "1", "2025-12-02"), "x": 1}]), series_id="DGS10", retrieved_at=RET)) == [
        "MALFORMED_ROW", "BAD_TIME", "MALFORMED_ROW"]


def test_A06_halts_parse_safely_in_new_york_time_and_a_resumption_is_a_revision_of_the_same_halt():
    assert HALTS.request()["params"] == {"feed": "tradehalts"} and "NASDAQ_RSS_TERMS" in HALTS.request()["requires_approval"]
    body = rss(hitem(), hitem(sym="WXYZ", hd="12/02/2025", ht="09:45:00", reason="T1"))
    obs, token, problems = HALTS.parse(body, retrieved_at=RET)
    assert token is None and problems == [] and [o.symbol for o in obs] == ["ABCD", "WXYZ"]
    a, w = obs
    assert a.observed_at == datetime(2025, 12, 1, 15, 15, 30, tzinfo=UTC) and a.payload["reason_code_known"] is True and a.tier == 1
    assert "HALTED_OPEN" in a.quality_flags and "HALTED_OPEN" in w.quality_flags
    resumed = HALTS.parse(rss(hitem(rd="12/01/2025", rq="10:20:00", rt="10:20:30")), retrieved_at=RET + timedelta(hours=1))[0][0]
    assert resumed.payload["resumed_at"] == "2025-12-01T15:20:30+00:00" and "HALTED_OPEN" not in resumed.quality_flags
    assert natural_key(resumed) == natural_key(a) and content_hash(resumed) != content_hash(a)                   # the same halt, a new revision
    assert codes(HALTS.parse(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "x">]><rss/>', retrieved_at=RET)) == ["UNSAFE_XML"]
    assert codes(HALTS.parse(b"<feed/>", retrieved_at=RET)) == ["UNEXPECTED_SHAPE"]
    assert codes(HALTS.parse(b"<rss><channel>", retrieved_at=RET)) == ["INVALID_XML"]
    assert codes(HALTS.parse(b" " * (HALTS.MAX_BYTES + 1), retrieved_at=RET)) == ["PAGE_TOO_LARGE"]
    assert codes(HALTS.parse(rss(hitem(sym="a1"), hitem(ht="25:00:00"), hitem(rd="12/01/2025", rt="10:00:00")), retrieved_at=RET)) == [
        "MALFORMED_ROW", "BAD_TIME", "BAD_TIME"]                                                                   # a resumption before its halt
    assert HALTS.parse(rss(hitem(reason="ZZ9")), retrieved_at=RET)[0][0].payload["reason_code_known"] is False


def test_A07_the_earnings_calendar_is_not_configured_without_a_key_and_keeps_each_snapshot():
    r = EARN.request()
    assert (r["status"], r["credential"], r["params"]) == ("NOT_CONFIGURED", "ALPHAVANTAGE_API_KEY", {"function": "EARNINGS_CALENDAR", "horizon": "3month"})
    assert EARN.request(configured=True)["status"] == "READY"
    with pytest.raises(ValueError):
        EARN.request(horizon="1month")
    obs, _, problems = EARN.parse(AV_CSV.encode(), retrieved_at=RET)
    assert problems == [] and [o.symbol for o in obs] == ["AAPL", "MSFT"]
    aapl, msft = obs
    assert aapl.observed_at == datetime(2025, 12, 6, 5, 0, tzinfo=UTC) and aapl.source_identifier == "expected:2025-12-31"   # the snapshot day
    assert (aapl.payload["report_date"], aapl.payload["estimate"], aapl.payload["confirmed"], aapl.payload["time_precision"]) == ("2026-01-29", 2.35, False, "DATE_ONLY")
    assert msft.payload["estimate"] is None and aapl.available_at == RET
    timed = EARN.parse((AV_CSV.splitlines()[0] + ",timeOfTheDay\r\nAAPL,Apple Inc,2026-01-29,2025-12-31,2.35,USD,post-market\r\n").encode(), retrieved_at=RET)[0][0]
    assert timed.payload["time_of_day"] == "post-market" and timed.payload["time_precision"] == "DATE_ONLY"        # a hint, never a synthesised time
    past = EARN.parse(b"symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAAPL,Apple,2025-12-01,2025-09-30,1,USD\n", retrieved_at=RET)[0][0]
    assert "UNCONFIRMED_PAST" in past.quality_flags
    assert codes(EARN.parse(json.dumps({"Information": "rate limit"}).encode(), retrieved_at=RET)) == ["ERROR_ENVELOPE"]
    assert codes(EARN.parse(b'{"x": 1}', retrieved_at=RET)) == ["UNEXPECTED_SHAPE"]
    assert codes(EARN.parse(b"symbol,name,date\nAAPL,Apple,2026-01-01\n", retrieved_at=RET)) == ["UNEXPECTED_SHAPE"]
    assert codes(EARN.parse(b"symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAAPL,A,2026-13-01,2025-12-31,1,USD\nMSFT,M,2026-01-01,2025-12-31,n/a,USD\n",
                            retrieved_at=RET)) == ["BAD_TIME", "MALFORMED_ROW"]
    assert [o.symbol for o in EARN.parse(AV_CSV.encode(), retrieved_at=RET, symbols=["msft"])[0]] == ["MSFT"]


# ============================================================================================================ news (N)
def test_N01_attribution_uses_the_evidence_tiers_and_keeps_deny_listed_links_as_metadata_only():
    assert AT.canonical_url("https://WWW.Benzinga.com/a/b/?utm_source=x&b=2&a=1&fbclid=z&ref=q#frag") == "https://benzinga.com/a/b?a=1&b=2"
    assert [AT.canonical_url(x) for x in ("ftp://x.com/a", "", None, "not a url")] == [None, None, None, None]
    for source, url in (("Reuters", "https://www.reuters.com/a"), ("Benzinga", "https://www.benzinga.com/a"), ("Benzinga", "https://x.com/someone/1"),
                        ("Some Blog", "https://blog.example.org/p")):
        a = AT.attribute(provider="ALPACA", source=source, author="x", url=url, published_at=START, first_seen_at=RET)
        assert a["tier"] == tier_for(host=AT.host_of(url), subject_ticker="", ticker="", source_type="ALPACA_NEWS", press_source=source)   # never a 2nd table
    tiers = [AT.attribute(provider="ALPACA", source=s, author="", url=u, published_at=START, first_seen_at=RET)["tier"]
             for s, u in (("Reuters", "https://reuters.com/a"), ("Example Wire", "https://benzinga.com/a"), ("Benzinga", "https://x.com/a"))]
    assert tiers == [3, 4, 5]
    deny = AT.attribute(provider="ALPACA", source="Reuters", author="", url=f"https://www.{config.EVIDENCE_DENY_HOSTS[0]}/x", published_at=START, first_seen_at=RET)
    assert deny["deny_host"] is True
    long = "word " * 200
    assert len(AT.snippet(long)) <= AT.SNIPPET_MAX and AT.snippet(long).endswith("…") and AT.snippet("  a   b ") == "a b"


def _item(key, headline, *, source="Benzinga", url=None, symbols=("NVDA",), pub=START + timedelta(hours=13), seen=RET, tier=4, pid=None):
    return {"key": key, "provider": "ALPACA", "provider_id": pid or key, "headline": headline, "source": source, "url": url or f"https://example.com/{key}",
            "symbols": list(symbols), "published_at": pub, "first_seen_at": seen, "tier": tier}


def test_N02_dedup_clusters_revisions_urls_headlines_and_near_duplicates_but_not_template_lookalikes():
    h = "Nvidia raises full-year guidance after record quarter"
    items = [_item("a1", h, pid="1", seen=RET),
             _item("a1r", "Nvidia raises full-year guidance after a record quarter", pid="1", seen=RET + timedelta(hours=1)),      # a revision
             _item("b", h, source="Bloomberg", url="https://bloomberg.example/n", tier=3, seen=RET + timedelta(minutes=5)),        # another outlet
             _item("c", "Different words entirely", url="https://example.com/a1?utm_source=x", seen=RET + timedelta(minutes=9)),   # same URL as a1
             _item("d", "Apple shares rise 2% in premarket trading", symbols=("AAPL",)),
             _item("e", "Tesla shares rise 2% in premarket trading", symbols=("TSLA",)),                                       # same template
             _item("g", "Apple shares rise 2% in premarket trading", symbols=("AAPL",), url="https://other.example/g", pid="g2"),   # same headline+source+day
             _item("f", h, source="Fortune", url="https://fortune.example/f", pub=START + timedelta(hours=13 + 40))]            # 40 h later
    out = {r["key"]: r for r in DD.cluster(items)}
    same = lambda *ks: len({out[k]["cluster_id"] for k in ks}) == 1                                                   # noqa: E731
    assert same("a1", "a1r", "b", "c") and same("d", "g") and not same("d", "e") and not same("a1", "f")
    assert (out["a1r"]["rule"], out["c"]["rule"], out["g"]["rule"], out["b"]["rule"]) == ("PROVIDER_ID", "CANONICAL_URL", "HEADLINE_HASH", "NEAR_DUPLICATE")
    assert out["a1"]["representative"] and not out["b"]["representative"] and out["e"]["rule"] is None              # first seen represents
    tie = DD.cluster([_item("x1", h, tier=4, seen=RET), _item("x2", h, source="Reuters", tier=3, seen=RET, url="https://r.example/x")])
    assert [r["representative"] for r in tie] == [False, True]                                                        # a tie goes to the lower tier
    assert DD.cluster(list(reversed(items))) == list(reversed(DD.cluster(items)))                                     # deterministic, order-free
    assert DD.jaccard(DD.tokens("Apple shares rise 2% in premarket trading"), DD.tokens("Tesla shares rise 2% in premarket trading")) < DD.JACCARD_MIN
    tpl = "beats quarterly estimates raises annual outlook shares climb in late trading session"
    twins = {r["key"]: r["cluster_id"] for r in DD.cluster([_item("t1", f"Apple {tpl}", symbols=("AAPL",)), _item("t2", f"Tesla {tpl}", symbols=("TSLA",))])}
    assert DD.jaccard(DD.tokens(f"Apple {tpl}"), DD.tokens(f"Tesla {tpl}")) >= DD.JACCARD_MIN and twins["t1"] != twins["t2"]   # only a shared symbol joins
    assert (DD.JACCARD_MIN, DD.WINDOW_HOURS) == (0.8, 36)                                                             # pre-registered
    with pytest.raises(ValueError):
        DD.cluster([_item("k", h), _item("k", h)])


SEC_NAMES = {"NVDA": "NVIDIA CORP", "AMD": "ADVANCED MICRO DEVICES INC", "ON": "ON SEMICONDUCTOR CORP", "AAPL": "Apple Inc."}


def test_N03_entities_record_method_confidence_and_role_and_never_read_a_bare_word_as_a_ticker():
    m = {x["symbol"]: x for x in EN.mentions(headline="Nvidia (NASDAQ: NVDA) raises guidance; chips rally on demand",
                                             summary="Advanced Micro Devices also gained.", provider_symbols=["NVDA", "AMD"], sec_names=SEC_NAMES)}
    assert (m["NVDA"]["method"], m["NVDA"]["confidence"], m["NVDA"]["role"]) == ("EXCHANGE_TAG", 0.95, "PRIMARY")
    assert (m["AMD"]["method"], m["AMD"]["role"]) == ("PROVIDER_TAG", "MENTION") and "NAME_MATCH" in m["AMD"]["methods"]
    assert "ON" not in m                                                                                               # "on" is a word, not a ticker
    alone = {x["symbol"]: x for x in EN.mentions(headline="Chip stocks move", summary="", provider_symbols=["ON"], sec_names=SEC_NAMES)}
    assert (alone["ON"]["method"], alone["ON"]["confidence"]) == ("UNVERIFIED", 0.3)                                   # a common word needs more
    cash = {x["symbol"]: x for x in EN.mentions(headline="$ON jumps on guidance", summary="", provider_symbols=["ON"], sec_names=SEC_NAMES)}
    assert (cash["ON"]["method"], cash["ON"]["role"]) == ("CASHTAG", "PRIMARY")
    unknown = EN.mentions(headline="Small cap news", summary="", provider_symbols=["ZZZQ"], sec_names=SEC_NAMES)
    assert [(x["symbol"], x["method"]) for x in unknown] == [("ZZZQ", "UNVERIFIED")]
    named = {x["symbol"]: x for x in EN.mentions(headline="Apple unveils a new phone", summary="", provider_symbols=[], sec_names=SEC_NAMES, universe=["AAPL"])}
    assert (named["AAPL"]["method"], named["AAPL"]["confidence"], named["AAPL"]["role"]) == ("NAME_MATCH", 0.7, "PRIMARY")
    roundup = EN.mentions(headline="Movers: NVDA, AMD, AAPL", summary="", provider_symbols=["NVDA", "AMD", "AAPL", "MSFT", "TSLA", "META"], sec_names=SEC_NAMES)
    assert {x["role"] for x in roundup} == {"ROUNDUP"}
    assert EN.catalyst_eligible(m["NVDA"], 3) and not EN.catalyst_eligible(m["NVDA"], 4) and not EN.catalyst_eligible(m["AMD"], 3)
    assert not EN.catalyst_eligible(alone["ON"], 1) and not EN.catalyst_eligible(roundup[0], 1)
    assert (EN.normalize_name("Apple Inc."), EN.normalize_name("ON Semiconductor Corp"), EN.normalize_name("The Walt Disney Company")) == (
        "apple", "on semiconductor", "walt disney")


# ============================================================================================================ the store (P)
PROVIDER_OF = {"NEWS": "ALPACA", "QUOTE_SNAPSHOT": "ALPACA", "FILING": "SEC", "FACT": "SEC", "MACRO": "FRED", "HALT": "NASDAQ_TRADER",
               "EARNINGS": "ALPHA_VANTAGE"}


def fixture_dataset(store, kind, feed="none", symbols=()):
    return store.create_dataset(DatasetSpec(job_kind="FIXTURE", kind=kind, provider=PROVIDER_OF[kind], feed=feed, adjustment="none", symbols=list(symbols)))


def all_fixture_observations(t=RET):
    return {"NEWS": NEWS.parse_page(news_page([art(101), art(102, headline="AMD wins a contract", symbols=("AMD",), url="https://benzinga.com/b")]),
                                    symbols=["NVDA", "AMD"], retrieved_at=t)[0],
            "QUOTE_SNAPSHOT": SNAP.parse(json.dumps({"AAPL": snap()}).encode(), symbols=["AAPL", "MSFT"], feed="iex", retrieved_at=t)[0],
            "FILING": FILINGS.parse(submissions([frow()]), ticker="AAPL", cik="320193", retrieved_at=t)[0],
            "FACT": FACTS.parse(companyfacts([fact()]), ticker="AAPL", cik="320193", retrieved_at=t)[0],
            "MACRO": FRED.parse(fred_page([fobs("2025-12-01", "4.10", "2025-12-02"), fobs("2025-12-02", ".", "2025-12-03")]), series_id="DGS10", retrieved_at=t)[0],
            "HALT": HALTS.parse(rss(hitem()), retrieved_at=t)[0],
            "EARNINGS": EARN.parse(AV_CSV.encode(), retrieved_at=t)[0]}


def test_P01_every_adapter_s_rows_go_through_the_71_store_point_in_time_with_revisions_absences_and_feeds_apart(tmp_path):
    clk = Clock(RET)
    s = MarketIntelStore(tmp_path / "mi.db", clock=clk, approvals_root=tmp_path / "approvals")
    seals = {}
    for kind, obs in all_fixture_observations().items():
        did = fixture_dataset(s, kind, feed="iex" if kind == "QUOTE_SNAPSHOT" else "none")
        res = s.ingest(did, obs)
        assert res.rejected == 0 and res.inserted + res.absences == len(obs), kind
        seals[did] = s.finish_dataset(did, "COMPLETED")
    anchors = {d: {k: v[k] for k in ("content_sha256", "records_sha256", "metadata_sha256", "status")} for d, v in seals.items()}
    assert s.verify_integrity(anchors=anchors)["state"] == "VERIFIED"
    assert len(s.history("NEWS", as_of=RET)) == 2 and s.history("NEWS", as_of=RET - timedelta(microseconds=1)) == []   # never known before we saw it
    assert s.latest_usable("QUOTE_SNAPSHOT", at=RET, symbol="AAPL", feed="iex")["payload"]["bid"] == 100.0
    assert s.latest_status("QUOTE_SNAPSHOT", at=RET, symbol="MSFT", feed="iex")["latest_record"]["record_type"] == "ABSENCE"
    with pytest.raises(FeedRequired):
        s.ingest(fixture_dataset(s, "QUOTE_SNAPSHOT", feed="iex"), SNAP.parse(json.dumps({"AAPL": snap()}).encode(), symbols=["AAPL"], feed="sip",
                                                                                   retrieved_at=RET)[0])               # an iex dataset refuses a sip row
    macro = s.history("MACRO", series_id="DGS10", as_of=RET)
    dot_key = natural_key(all_fixture_observations()["MACRO"][1])
    assert [r["payload"]["value"] for r in macro] == [4.10] and len(s.absences(dot_key)) == 1                        # "." stays an absence
    clk.t = RET + timedelta(hours=1)
    edited = NEWS.parse_page(news_page([art(101, headline="Nvidia raises full-year guidance (updated)", updated="2025-12-06T15:30:00Z")]), symbols=["NVDA"],
                             retrieved_at=clk())[0]
    s.ingest(fixture_dataset(s, "NEWS"), edited)
    key = natural_key(edited[0])
    assert len(s.revisions(key)) == 2
    assert [r["payload"]["headline"] for r in s.history("NEWS", as_of=RET) if r["natural_key"] == key] == ["Nvidia raises full-year guidance after record quarter"]
    assert [r["payload"]["headline"] for r in s.history("NEWS", as_of=clk()) if r["natural_key"] == key] == ["Nvidia raises full-year guidance (updated)"]
    s.ingest(fixture_dataset(s, "HALT"), HALTS.parse(rss(hitem(rd="12/01/2025", rq="10:20:00", rt="10:20:30")), retrieved_at=clk())[0])
    halt = s.history("HALT", symbol="ABCD", as_of=clk())[0]
    assert halt["revision"] == 2 and "HALTED_OPEN" not in halt["quality_flags"]
    assert "HALTED_OPEN" in s.history("HALT", symbol="ABCD", as_of=RET)[0]["quality_flags"]                          # each state as known when seen
    clk.t = RET + timedelta(days=1)
    moved = EARN.parse(AV_CSV.replace("2026-01-29", "2026-01-30").encode(), retrieved_at=clk())[0]
    s.ingest(fixture_dataset(s, "EARNINGS"), moved)
    provider = EARN.StoreEarningsProvider(s, as_of=RET + timedelta(hours=2))
    assert [e.event_date for e in provider.get_upcoming_earnings("AAPL", date(2026, 1, 1), date(2026, 2, 28)).events] == [date(2026, 1, 29)]
    later = EARN.StoreEarningsProvider(s, as_of=clk() + timedelta(hours=1)).get_upcoming_earnings("AAPL", date(2026, 1, 1), date(2026, 2, 28))
    assert [e.event_date for e in later.events] == [date(2026, 1, 30)] and later.events[0].confirmed is False      # the latest snapshot wins
    recent = EARN.StoreEarningsProvider(s, as_of=clk()).get_recent_earnings("AAPL", date(2025, 10, 1), date(2025, 10, 31))
    assert recent.available and [(e.event_date, e.confirmed, e.source_provider) for e in recent.events] == [(date(2025, 10, 30), True, "SEC")]
    nothing = EARN.StoreEarningsProvider(MarketIntelStore(tmp_path / "none.db", clock=clk)).get_upcoming_earnings("AAPL", date(2026, 1, 1), date(2026, 2, 28))
    assert (nothing.available, nothing.events) == (False, [])                                                        # nothing stored: never invented


def test_P02_sec_fact_visibility_is_value_quality_pit_s_rule_on_the_verified_calendar():
    for filed, want in ((date(2025, 10, 31), "2025-11-03"), (date(2025, 11, 26), "2025-11-28"), (date(2025, 12, 31), "2026-01-02"), (date(2024, 12, 31), None)):
        got, how = FACTS.visible_from(filed)
        sessions = C.market_sessions(filed, filed + timedelta(days=15))
        assert got == want and (got is None or got == pit.Calendar(sessions).available_at(filed.isoformat())), filed
        assert how == ("PIT_RULE" if want else "CALENDAR_NOT_COVERED")
    assert pit.Calendar(["2025-11-03"]).available_at("2025-10-31") == "2025-11-03"                                  # the reused rule itself


# ============================================================================================================ earnings wiring (E)
def test_E01_the_earnings_provider_is_not_wired_and_never_configured_without_a_key():
    from data.events import earnings as ER
    assert isinstance(ER.get_earnings_provider(), ER.UnavailableEarningsProvider)                                    # Stage 2.6 context unchanged
    assert "market_intel" not in (ROOT / "data" / "events" / "earnings.py").read_text(encoding="utf-8")
    assert EARN.request()["status"] == "NOT_CONFIGURED" and not [n for n in dir(config) if "ALPHAVANTAGE" in n or "ALPHA_VANTAGE" in n]
    assert CLI.build_request(type("A", (), {"source": "earnings_av", "horizon": None})())["status"] == "NOT_CONFIGURED"


# ============================================================================================================ invariants (X)
FUZZ = [b"", b"\x00\xff\xfe", b"null", b"[]", b"{}", b"0", b'"x"', b'{"news": 5, "next_page_token": null}', b'{"AAPL": [1, 2]}',
        b'{"cik": "x", "filings": {"recent": {}}}', b'{"cik": 1, "facts": {"us-gaap": 5}}', b'{"count": "1", "offset": 0, "limit": 1, "observations": []}',
        b"<rss><channel><item>" + b"<x>" * 50 + b"</channel></rss>", b"symbol,name\n" + b"," * 1000, ("[" * 5000 + "]" * 5000).encode(),
        json.dumps({"news": [{"id": 1}] * 3, "next_page_token": None}).encode(), b"\xef\xbb\xbf{}"]
PARSERS = {"news": lambda b: NEWS.parse_page(b, symbols=["NVDA"], retrieved_at=RET),
           "snapshots": lambda b: SNAP.parse(b, symbols=["AAPL"], feed="iex", retrieved_at=RET),
           "filings": lambda b: FILINGS.parse(b, ticker="AAPL", cik="320193", retrieved_at=RET),
           "facts": lambda b: FACTS.parse(b, ticker="AAPL", cik="320193", retrieved_at=RET),
           "fred": lambda b: FRED.parse(b, series_id="DGS10", retrieved_at=RET),
           "halts": lambda b: HALTS.parse(b, retrieved_at=RET),
           "earnings": lambda b: EARN.parse(b, retrieved_at=RET)}


def test_X01_no_adapter_ever_raises_and_a_parser_bug_is_a_page_problem(monkeypatch):
    for name, parse in PARSERS.items():
        for body in FUZZ:
            obs, token, problems = parse(body)                                                                       # never raises
            assert isinstance(obs, list) and (token is None or isinstance(token, str)) and isinstance(problems, list), (name, body[:30])
            assert all(isinstance(o, Observation) for o in obs), name
    monkeypatch.setattr(C, "session_fields", lambda t: 1 / 0)
    assert codes(NEWS.parse_page(news_page([art(1)]), symbols=["NVDA"], retrieved_at=RET)) == ["PARSER_ERROR:ZeroDivisionError"]


def test_X02_every_row_has_explicit_times_and_is_available_exactly_when_we_retrieved_it():
    for kind, obs in all_fixture_observations().items():
        assert obs, kind
        for o in obs:
            assert o.kind == kind and o.retrieved_at == o.available_at == RET, kind                                  # never the provider's clock
            assert o.observed_at is not None and o.observed_at.tzinfo is not None and o.observed_at <= RET, kind
            assert o.session_date is not None and o.provider == PROVIDER_OF[kind], kind
            if o.status == "AVAILABLE":
                assert o.payload, kind
            else:
                assert o.payload == {}, kind                                                                         # a missing value carries nothing


def test_X03_no_request_ever_carries_a_credential():
    reqs = [NEWS.request(["NVDA"], START, END, page_token="tok123"), SNAP.request(["AAPL"], "iex"), FILINGS.request("320193", user_agent="Jane j@x.org"),
            FACTS.request("320193", user_agent="Jane j@x.org"), FRED.request("DGS10", observation_start=date(2025, 1, 1), observation_end=date(2025, 2, 1), configured=True),
            HALTS.request(), EARN.request(configured=True)]
    for r in reqs:
        blob = json.dumps({k: v for k, v in r.items() if k != "credential"}).lower()          # the credential is NAMED, never carried
        assert not [w for w in ("apikey", "api_key", "secret", "password", "apca-api") if w in blob], r
        assert r["network"] is False and r["status"] == "READY" and set(r["params"]) <= {"symbols", "start", "end", "sort", "limit", "include_content", "page_token",
                                                                                         "feed", "series_id", "file_type", "observation_start", "observation_end",
                                                                                         "realtime_start", "realtime_end", "sort_order", "offset", "function", "horizon"}
    for bad in ({"api_key": "x"}, {"apikey": "x"}, {"token": "x"}, {"q": "apikey=abc"}):
        with pytest.raises(ValueError):
            C.request("FRED", "api.stlouisfed.org", "/x", bad)


def test_X04_the_evidence_tripwire_is_never_hit_and_no_socket_is_opened(monkeypatch, tmp_path):
    from evidence import net
    calls = []
    monkeypatch.setattr(net, "WIRE", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("evidence network")))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("socket")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("socket")))
    all_fixture_observations()
    for parse in PARSERS.values():
        for body in FUZZ:
            parse(body)
    for args in (["describe"], ["request", "--source", "halts"], ["fetch", "--source", "fred"]):
        CLI.main(args, out=io.StringIO())
    assert calls == []
    for f in sorted((ROOT / "market_intel").rglob("*.py")):
        rel = f.relative_to(ROOT / "market_intel").as_posix()
        if rel.startswith(("sources/", "news/")) and rel not in ("sources/__init__.py", "sources/alpaca_bars.py"):
            src = f.read_text(encoding="utf-8")
            for word in ("urlopen", "urllib.request", "http.client", "requests.", "import requests", "socket", "alpaca.data", "alpaca.trading", "net.fetch(", "WIRE"):
                assert word not in src, (rel, word)


# ============================================================================================================ the CLI (C)
def test_C01_the_cli_is_offline_and_read_only_and_fetch_is_always_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = tmp_path / "halts.xml"
    saved.write_bytes(rss(hitem()))
    bad = tmp_path / "bad.json"
    bad.write_bytes(json.dumps({"message": "x"}).encode())
    before = sorted(p.name for p in tmp_path.iterdir())

    def run(*args):
        buf = io.StringIO()
        return CLI.main(list(args), out=buf), buf.getvalue()
    code, text = run("describe")
    assert code == 0 and {a["source"] for a in json.loads(text)["adapters"]} == set(CLI.ADAPTERS) == {"alpaca_news", "alpaca_snapshots", "sec_filings",
                                                                                                         "sec_facts", "fred", "halts", "earnings_av"}
    code, text = run("request", "--source", "fred", "--series", "DGS10", "--start", "2025-01-01", "--end", "2025-12-31")
    assert code == 0 and json.loads(text)["status"] == ("READY" if config.has_fred_credentials() else "NOT_CONFIGURED")
    code, text = run("parse", "--source", "halts", "--file", str(saved), "--retrieved-at", "2025-12-06T15:00:00Z")
    doc = json.loads(text)
    assert code == 0 and doc["observations"] == 1 and doc["rows"][0]["natural_key"].startswith("HALT|ABCD|") and (doc["network"], doc["writes"]) == (False, False)
    assert run("parse", "--source", "alpaca_news", "--file", str(bad), "--retrieved-at", "2025-12-06T15:00:00Z", "--symbols", "NVDA")[0] == 2
    assert run("parse", "--source", "halts", "--file", str(saved), "--retrieved-at", "2025-12-06T15:00:00")[0] == 1       # a naive time
    code, text = run("fetch", "--source", "alpaca_news")
    assert (code, json.loads(text)["fetched"]) == (3, False)
    assert run("request", "--source", "nope")[0] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before and not (ROOT / "data" / "market_intel.db").exists()


# ============================================================================================================ isolation (S)
NEW_MODULES_72 = ("sources/common.py", "sources/alpaca_news.py", "sources/alpaca_snapshots.py", "sources/sec_filings.py", "sources/sec_facts.py",
                  "sources/fred.py", "sources/halts.py", "sources/earnings_av.py", "sources/cli.py", "news/__init__.py", "news/attribution.py",
                  "news/dedup.py", "news/entities.py")
ALLOWED_72 = {"__future__", "argparse", "csv", "functools", "hashlib", "io", "json", "math", "re", "sys", "unicodedata", "datetime", "pathlib", "typing",
              "zoneinfo", "urllib.parse", "xml.etree.ElementTree", "config", "api.validation", "value_quality", "evidence.net", "evidence.rules",
              "evidence.sources", "data.events.earnings", "data.events.models", "market_intel", "market_intel.calendar", "market_intel.validate",
              "market_intel.models", "market_intel.store", "market_intel.news", "market_intel.news.attribution", "market_intel.sources", "market_intel.sources.common",
              "market_intel.sources.sec_filings"}


def test_S01_the_72_modules_are_a_closed_registry_with_pure_imports_and_nothing_outside_imports_them():
    for rel in NEW_MODULES_72:
        tree = ast.parse((ROOT / "market_intel" / rel).read_text(encoding="utf-8"))
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                                    for a in n.names}
        assert names <= ALLOWED_72, (rel, sorted(names - ALLOWED_72))
        calls = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, (ast.Name, ast.Attribute))}
        assert not calls & {"urlopen", "create_connection", "fetch", "connect", "write_text", "write_bytes", "mkdir", "unlink", "Popen", "system"}, rel
        assert "os.environ" not in (ROOT / "market_intel" / rel).read_text(encoding="utf-8"), rel
    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}
    assert set(NEW_MODULES_72) <= present and {f"market_intel/{m}" for m in NEW_MODULES_72} <= FL.NEW_72                # declared, one by one
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",
             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]
    assert users == []                                                                                                 # no live path reads the adapters



# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_72}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_72} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


APP_EDITS_72 = {".github/ci/pytest_groups.json"}
FROZEN_DIRS_72 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                  "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                  "database", "data", "value_quality", "market_intel")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_72 == "05358cb0f617697cc760de4e53cda107841a352b" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_72}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_72).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_72).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_72 and {p for p in changed if p in in_base} == FL.EDITED_72
    assert FL.NEW_72 == {"DESIGN_72_MARKET_INTEL_SOURCES.md", "tests/test_market_intel_sources_72.py"} | {f"market_intel/{m}" for m in NEW_MODULES_72}
    assert {p for p in FL.EDITED_72 if not p.startswith("tests/")} == APP_EDITS_72
    assert FL.SEMANTIC_72 == {} and FL.INSTRUMENTATION_72 == {}
    for d in FROZEN_DIRS_72:                                                                   # every existing module, the 7.0 / 7.1 market_intel included
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert now_text(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore"):
        assert now_text(rel) == at_base(rel), rel
    for rel in sorted(FL.NEW_72 | FL.EDITED_72):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_market_intel():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    from market_intel.settings import settings as mi_settings
    assert mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_", "ALPHAVANTAGE"))]
    import setups as SU
    assert (SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.EVIDENCE_MAX_AGE_DAYS) == (40, 60, 30)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    import ast
    for rel, edits in REPLAY_72.items():
        text = now_text(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_72) == FL.EDITED_72 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.2 (market intelligence event")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_70 + NEW_71 + NEW_72 plus this ledger.", " + NEW_70 + NEW_71 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_72 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_72: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key
    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_72 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_72 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71                                                         # Stage 7.1, pinned by its own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                             # Stages 7.1, 7.2: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    assert present == set(PURE_70) | set(NEW_MODULES), sorted(present ^ (set(PURE_70) | set(NEW_MODULES)))   # a new module must be declared here\n    for rel in sorted(present):\n        tree = ast.parse((ROOT / "market_intel" / rel).read_text(encoding="utf-8"))\n',
         '    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 if p.startswith("market_intel/") and p.endswith(".py")}   # Stage 7.2: its own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n    for rel in sorted(present - later):\n        tree = ast.parse((ROOT / "market_intel" / rel).read_text(encoding="utf-8"))\n'),
        ('\nAPP_EDITS_71 = {".github/ci/pytest_groups.json", ".gitignore", "DESIGN_70_MARKET_INTEL_CONTRACTS.md", "market_intel/__init__.py", "market_intel/calendar.py"}\n',
         '\ndef post(rel):\n    """main after PR #42 (Stage 7.1 merged; the Stage 7.2 base): what Stage 7.1 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_72}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_72} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_71 = {".github/ci/pytest_groups.json", ".gitignore", "DESIGN_70_MARKET_INTEL_CONTRACTS.md", "market_intel/__init__.py", "market_intel/calendar.py"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_71).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_71, FL.BASE_72).split())                # Stage 7.1 itself, between two fixed commits\n    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72                                                         # Stage 7.2, pinned by its own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n                                                         "missing": sorted((FL.NEW_71 | FL.EDITED_71 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_71).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_71).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_71 and {p for p in changed if p in in_base} == FL.EDITED_71\n    assert FL.NEW_71 == {"DESIGN_71_MARKET_INTEL_STORE.md", "tests/test_market_intel_store_71.py"} | {f"market_intel/{n}" for n in (\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_71).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_71 and {p for p in stage if p in in_base} == FL.EDITED_71\n    assert FL.NEW_71 == {"DESIGN_71_MARKET_INTEL_STORE.md", "tests/test_market_intel_store_71.py"} | {f"market_intel/{n}" for n in (\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_71.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_71.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_71) == FL.EDITED_71 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.1 (market intelligence store")\n',
         '    assert set(REPLAY_71) == FL.EDITED_71 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.1 (market intelligence store")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) - set(old) == {".gitignore"}                                     # one new key: the store\'s ignore lines (read as on main earlier)\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_71 & set(old)\n    gi_base, gi_now = at_base(".gitignore"), now_text(".gitignore")\n    assert gi_now.startswith(gi_base) and gi_now[len(gi_base):] == "data/market_intel.db\\ndata/market_intel.db-wal\\ndata/market_intel.db-shm\\n"\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.1 left\n    assert set(new_led) - set(old) == {".gitignore"}                                       # one new key: the store\'s ignore lines (read as on main earlier)\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_71 & set(old)\n    gi_base, gi_now = at_base(".gitignore"), post(".gitignore")\n    assert gi_now.startswith(gi_base) and gi_now[len(gi_base):] == "data/market_intel.db\\ndata/market_intel.db-wal\\ndata/market_intel.db-shm\\n"\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
