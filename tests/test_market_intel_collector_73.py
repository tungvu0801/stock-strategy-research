"""
Stage 7.3 - the market-intelligence collector and scheduler, OFFLINE (DESIGN_73_MARKET_INTEL_COLLECTOR.md). A fake transport and a fake
clock only; no network (the conftest socket block, the evidence tripwire and explicit guards); temporary stores and state files; nothing
written outside tmp_path.

  P01-P02 plans: registered, frozen and read-only; Stage 7.0 budgets and freshness policies only; the C.8 cadences; daily row and run bounds.
  T01-T02 the transport: every outcome's error class and retry rule; the default refuses the network; the fake keeps the size contract.
  R01-R02 retry backoff with deterministic jitter; the circuit breaker's transitions.
  E01-E06 failures: timeouts and connection errors, HTTP 429 / 5xx / 4xx / 401 / 403, malformed bodies and misleading HTTP 200 envelopes,
          bodies above the cap, transport contract violations.
  G01-G02 pagination: tokens and offsets, loops, duplicate pages, the page cap, the row cap.
  I01-I02 idempotent ingestion: re-deliveries, late arrivals and late revisions inside the overlap, duplicates and conflicts in one page.
  W01-W04 watermarks: only after a complete run (the schema enforces it), partial ingestion failure, a crash between seal and watermark,
          outages longer than the lookback (gaps) and historical windows (refused).
  K01-K04 clock skew (decision A): future rows rejected, never rewritten; the HTTP Date check; our clock behind the watermark; future news.
  Z01-Z05 interruption and restart: KeyboardInterrupt, a hard kill and recovery, request_stop; a run that cannot create its dataset, seal
          it or archive a body; the run time limit.
  L01-L03 concurrency: leases (pair and provider), expired leases, two schedulers on one state file, real threads; a breaker opened
          after the read, a watermark advanced by another worker.
  B01-B03 budgets: the persisted Alpha Vantage daily cap across a restart and across two collectors, per-minute limits, waits and
          back-offs inside a run.
  C01-C02 the circuit breaker end to end, persisted across a restart; a trial without a request never closes it.
  H01     feed health: blocked, not configured, never succeeded, fresh / aging / stale, degraded, budget exhausted, daily refresh.
  D01-D03 the scheduler: a deterministic simulated session, repeated ticks, daily jobs, weekends, a new cycle each New York day.
  X01-X04 invariants: approval bypass attempts fail; no network is ever reached; no credential value is read or recorded; no incomplete
          run ever has a watermark and no malformed response is ever EMPTY or COMPLETED.
  S01-S03 isolation and laziness; the state file's schema verification; the Stage 7.1 store unchanged and VERIFIED.
  M01     bounded resources.
  V01-V06 final review: an unanchored or unconfirmed seal never moves a watermark; a CORRUPT store takes no run; an interrupted seal; every
          request has a persisted ALLOW and retries never pass a daily cap; FRED's exact 400-calendar-day window, enforced by the store;
          an incomplete daily job is never complete.
  F01-F03 scope (freeze bookkeeping).
"""
from __future__ import annotations

import ast
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import freeze_ledger as FL
from market_intel import freshness as FR
from market_intel import store as ST
from market_intel import validate as VA
from market_intel.archive import RawArchive
from market_intel.budget import default_budgets
from market_intel.collector import health as HE
from market_intel.collector import plans as PL
from market_intel.collector import policy as PO
from market_intel.collector import runner as RU
from market_intel.collector import state as SS
from market_intel.collector import transport as TR
from market_intel.collector.runner import Collector
from market_intel.collector.scheduler import Scheduler
from market_intel.collector.state import CollectorState, StateMismatch
from market_intel.collector.transport import FakeTransport, Response
from market_intel.store import MarketIntelStore

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
TUE = datetime(2025, 12, 2, 15, 0, tzinfo=UTC)                 # Tuesday 10:00 New York (regular session)
SAT = datetime(2025, 12, 6, 15, 0, tzinfo=UTC)                 # Saturday 10:00 New York
NEWS, SNAP, HALTS, FRED, EARN = (PL.PLANS[s] for s in ("alpaca_news", "alpaca_snapshots", "halts", "fred", "earnings_av"))
FILINGS, FACTS = PL.PLANS["sec_filings"], PL.PLANS["sec_facts"]
NV, AAPL_IEX, NONE, DGS10, AAPL = PL.Scope(("NVDA",)), PL.Scope(("AAPL",), feed="iex"), PL.Scope(), PL.Scope(series="DGS10"), PL.Scope(("AAPL",))
ALL = lambda name: True  # noqa: E731 - every credential present (presence only)


class Clock:
    def __init__(self, t):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += timedelta(seconds=s)

    def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.tick(s)


def env(tmp_path, t=TUE, *, credentials=ALL, worker="w1", outcomes=(), responder=None, clock=None, state_path=None, store_path=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    clk = clock or Clock(t)
    store = MarketIntelStore(store_path or tmp_path / "mi.db", clock=clk, approvals_root=tmp_path / "approvals")
    archive = RawArchive(tmp_path / "raw")
    state = CollectorState(state_path or tmp_path / "collector.db")
    ft = FakeTransport(outcomes, responder=responder, clock=clk)
    col = Collector(store=store, archive=archive, state=state, transport=ft, clock=clk, sleep=clk.sleep, credentials=credentials, worker=worker)
    return SimpleNamespace(clk=clk, store=store, archive=archive, state=state, ft=ft, col=col, tmp=tmp_path)


def z(t):
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def art(id_, created, *, headline=None, updated=None, symbols=("NVDA",)):
    return {"id": id_, "headline": headline or f"Nvidia story {id_}", "author": "A. Writer", "created_at": created if isinstance(created, str) else z(created),
            "updated_at": (updated if isinstance(updated, str) else z(updated)) if updated else (created if isinstance(created, str) else z(created)),
            "summary": "Nvidia said revenue rose.", "url": f"https://example.com/n/{id_}", "symbols": list(symbols), "source": "Example Wire", "images": []}


def news(arts, token=None, **headers):
    return Response(200, headers, json.dumps({"news": arts, "next_page_token": token}).encode())


def quote(t):
    return {"latestQuote": {"t": z(t), "bp": 100.0, "bs": 3, "ap": 100.04, "as": 2, "bx": "V", "ax": "V", "c": ["R"], "z": "C"}}


def snaps(**by_symbol):
    return Response(200, {}, json.dumps({s: quote(t) for s, t in by_symbol.items()}).encode())


def hitem(sym="ABCD", hd="12/02/2025", ht="09:45:30", reason="LUDP"):
    f = {"HaltDate": hd, "HaltTime": ht, "IssueSymbol": sym, "IssueName": f"{sym} Corp", "Market": "NASDAQ", "ReasonCode": reason, "PauseThresholdPrice": "",
         "ResumptionDate": "", "ResumptionQuoteTime": "", "ResumptionTradeTime": ""}
    return f"<item><title>{sym}</title>" + "".join(f"<ndaq:{k}>{v}</ndaq:{k}>" for k, v in f.items()) + f"<guid>{sym}-{hd}-{ht}</guid></item>"


def rss(*items):
    return Response(200, {}, ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>'
                              '<title>Trade Halts</title>' + "".join(items) + "</channel></rss>").encode())


def fobs(d, v, rs, re_="9999-12-31"):
    return {"realtime_start": rs, "realtime_end": re_, "date": d, "value": v}


def fred_page(obs, *, count=None, offset=0, limit=RU.FRED_PAGE_ROWS):
    return Response(200, {}, json.dumps({"realtime_start": "1776-07-04", "realtime_end": "9999-12-31", "units": "Percent", "count": len(obs) if count is None
                                         else count, "offset": offset, "limit": limit, "observations": obs}).encode())


AV_CSV = b"symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\nAAPL,Apple Inc,2026-01-29,2025-12-31,2.35,USD\r\nMSFT,Microsoft,2026-01-28,2025-12-31,,USD\r\n"


def fetches(e, did):
    c = sqlite3.connect(f"file:{e.store.path.as_posix()}?mode=ro", uri=True)
    try:
        return [r[0] for r in c.execute("SELECT status FROM mi_fetches WHERE dataset_id = ? ORDER BY seq", (did,))]
    finally:
        c.close()


def rejected(e, did):
    return [(m["disposition"], m["reason"]) for m in e.store.members(did) if m["disposition"] == "REJECTED"]


def key_of(sent):
    return ST.canonical({"endpoint": sent["endpoint"], "params": sent["params"]})


# ============================================================================================================ plans (P)
def test_P01_plans_are_registered_frozen_read_only_and_use_only_the_stage_70_budgets_and_policies():
    assert set(PL.PLANS) == {"alpaca_news", "alpaca_snapshots", "halts", "sec_filings", "sec_facts", "fred", "earnings_av"}
    assert all(v == [] for v in PL.check_plans().values()), PL.check_plans()
    with pytest.raises(TypeError):
        PL.PLANS["sec_filings"] = NEWS                                                          # the registry is read-only
    with pytest.raises(Exception):
        NEWS.max_pages = 1000                                                                    # a plan is frozen
    import dataclasses
    assert PL.registered(NEWS) and not PL.registered(dataclasses.replace(NEWS)) and not PL.registered(dataclasses.replace(FILINGS, blocked_reason=None))
    table = {p.source: (p.budget_setting, p.health_policy, [(c.window, c.interval_s, c.at_minute) for c in p.cadences], p.max_pages, p.max_bytes,
                        p.max_rows_per_run, p.credential, bool(p.blocked_reason)) for p in PL.PLANS.values()}
    MB = 1024 * 1024
    assert table == {
        "alpaca_news": ("MI_ALPACA_MAX_PER_MIN", "NEWS_POLL", [("EXTENDED", 120, None)], 20, 2 * MB, 1000, "ALPACA_API_KEY", False),
        "alpaca_snapshots": ("MI_ALPACA_MAX_PER_MIN", "QUOTE_SWING", [("REG", 60, None), ("PRE_FROM_0700", 300, None)], 1, 2 * MB, 100, "ALPACA_API_KEY", False),
        "halts": ("MI_HALTS_MAX_PER_MIN", "HALT_FEED", [("MARKET_0400_2000", 60, None)], 1, 2 * MB, 500, None, False),
        "sec_filings": ("MI_SEC_MAX_PER_MIN", "SEC_POLL", [("SEC_06_22", 600, None)], 1, 5 * MB, 1000, "SEC_USER_AGENT", True),
        "sec_facts": ("MI_SEC_MAX_PER_MIN", "DAILY_REFRESH", [("DAILY", 86400, 210)], 1, 20 * MB, 50000, "SEC_USER_AGENT", True),
        "fred": ("MI_FRED_MAX_PER_MIN", "DAILY_REFRESH", [("DAILY", 86400, 420)], 5, 5 * MB, 50000, "FRED_API_KEY", False),
        "earnings_av": ("MI_ALPHA_VANTAGE_MAX_PER_DAY", "EARNINGS_CALENDAR", [("DAILY", 86400, 390)], 1, 5 * MB, 20000, "ALPHAVANTAGE_API_KEY", False)}
    assert FILINGS.blocked_reason.startswith("SEC_ACCEPTANCE_TZ_UNVERIFIED") and FACTS.blocked_reason == FILINGS.blocked_reason
    assert [p.source for p in PL.PLANS.values() if p.windowed] == ["alpaca_news"] and (NEWS.lookback_max_s, NEWS.overlap_s) == (36 * 3600, 600)
    assert FRED.observation_days == 400 and not FRED.windowed
    pol = {k: (p.fresh_s, p.aging_s, p.window) for k, p in FR.POLICIES.items()}                 # the 7.0 numbers the plans grade against
    assert (pol["NEWS_POLL"], pol["QUOTE_SWING"], pol["HALT_FEED"], pol["SEC_POLL"]) == ((180, 600, "EXTENDED"), (60, 300, "REG"), (120, 180, "REG"),
                                                                                          (900, 1800, "SEC_06_22"))
    assert {p.budget_name for p in PL.PLANS.values()} <= set(default_budgets(lambda: TUE))
    assert {p.source: PL.daily_row_bound(p) for p in PL.PLANS.values()} == {"alpaca_news": 480_000, "alpaca_snapshots": 42_000, "halts": 480_000,
                                                                              "sec_filings": 96_000, "sec_facts": 50_000, "fred": 50_000, "earnings_av": 20_000}


def test_P02_cadence_windows_and_a_full_simulated_day_never_exceed_the_runs_per_day_bound():
    ny = lambda h, m, d=2: datetime(2025, 12, d, h, m, tzinfo=UTC) + timedelta(hours=5)       # noqa: E731 - New York (EST) wall clock
    assert PL.active_cadence(NEWS, ny(3, 59)) is None and PL.active_cadence(NEWS, ny(4, 0)).interval_s == 120 and PL.active_cadence(NEWS, ny(20, 0)) is None
    assert PL.active_cadence(SNAP, ny(6, 59)) is None and PL.active_cadence(SNAP, ny(7, 0)).interval_s == 300 and PL.active_cadence(SNAP, ny(9, 30)).interval_s == 60
    assert PL.active_cadence(SNAP, ny(16, 0)) is None and PL.active_cadence(HALTS, ny(4, 0)) and PL.active_cadence(HALTS, ny(20, 0)) is None
    for p in (NEWS, SNAP, HALTS, FILINGS):
        assert PL.active_cadence(p, ny(10, 0, d=6)) is None, p.source                             # Saturday: no intraday poll
    assert PL.active_cadence(FRED, ny(10, 0, d=6)) is not None                                    # a daily job runs every day
    runs = {}
    for p in (NEWS, SNAP, HALTS):
        last, n, t = None, 0, ny(0, 0)
        while t < ny(0, 0, d=3):
            c = PL.active_cadence(p, t)
            if c is not None and (last is None or (t - last).total_seconds() >= c.interval_s):
                last, n = t, n + 1
            t += timedelta(minutes=1)
        runs[p.source] = n
    assert runs == {"alpaca_news": 480, "alpaca_snapshots": 420, "halts": 960} == {p.source: PL.runs_per_day_bound(p) for p in (NEWS, SNAP, HALTS)}


# ============================================================================================================ transport (T)
def test_T01_every_outcome_has_one_error_class_and_retry_rule():
    mb = 100
    cases = [(TR.TransportTimeout("t"), "TIMEOUT", True), (TimeoutError(), "TIMEOUT", True), (TR.TransportConnectionError("c"), "ERROR", True),
             (OSError("x"), "ERROR", True), (Response(200, {}, b"{}"), "OK", False), (Response(429), "RATE_LIMITED", True), (Response(500), "HTTP_ERROR", True),
             (Response(503), "HTTP_ERROR", True), (Response(401), "ACCESS_DENIED", False), (Response(403), "ACCESS_DENIED", False),
             (Response(404), "HTTP_ERROR", False), (Response(400), "HTTP_ERROR", False), (Response(304), "HTTP_ERROR", False),
             (Response(200, {}, b"x" * 101), "TOO_LARGE", False), (Response(200, {}, b"x", truncated=True), "TOO_LARGE", False),
             (Response(True, {}, b""), "INVALID_RESPONSE", False), (Response(99, {}, b""), "INVALID_RESPONSE", False), (Response(600, {}, b""), "INVALID_RESPONSE", False),
             (Response(200, {}, "text"), "INVALID_RESPONSE", False), (Response(200, [], b""), "INVALID_RESPONSE", False), ("garbage", "INVALID_RESPONSE", False),
             (None, "INVALID_RESPONSE", False)]
    for outcome, status, retry in cases:
        got = TR.classify(outcome, max_bytes=mb)
        assert (got[0], got[2]) == (status, retry), (outcome, got)
    assert set(TR.CLASSES) >= {"OK", "TIMEOUT", "ERROR", "RATE_LIMITED", "HTTP_ERROR", "ACCESS_DENIED", "TOO_LARGE", "INVALID_RESPONSE"}


def test_T02_the_default_transport_refuses_and_the_fake_keeps_the_size_contract():
    req = {"host": "h", "endpoint": "/e", "params": {}, "credential": None}
    with pytest.raises(PermissionError, match="REAL_NETWORK_NOT_APPROVED"):
        TR.NoTransport().send(req, timeout_s=1, max_bytes=10)
    got = FakeTransport([Response(200, {}, b"y" * 100)]).send(req, timeout_s=1, max_bytes=10)
    assert (len(got.body), got.truncated) == (11, True)                                           # read to max_bytes + 1, then stopped

    class Sub(FakeTransport):
        pass
    assert TR.is_fake(FakeTransport()) and not any(TR.is_fake(x) for x in (Sub(), TR.NoTransport(), object(), SimpleNamespace(send=None)))
    with pytest.raises(TR.TransportConnectionError, match="FAKE_TRANSPORT_EXHAUSTED"):
        FakeTransport().send(req, timeout_s=1, max_bytes=1)                                       # an unscripted request fails, never succeeds


# ============================================================================================================ retry and breaker (R)
def test_R01_backoff_is_exponential_capped_and_deterministically_jittered():
    for n in range(1, 10):
        base = min(PO.BACKOFF_CAP_S, PO.BACKOFF_BASE_S * 2 ** (n - 1))
        b = PO.backoff_s(n, "req")
        assert base * (1 - PO.JITTER) <= b <= base * (1 + PO.JITTER) and b == PO.backoff_s(n, "req"), (n, b)
    assert len({PO.backoff_s(1, f"req{i}") for i in range(20)}) > 10                              # different requests spread out
    assert max(PO.backoff_s(n, "x") for n in range(1, 20)) <= PO.BACKOFF_CAP_S * (1 + PO.JITTER)
    assert (PO.MAX_ATTEMPTS, PO.BACKOFF_BASE_S, PO.BACKOFF_CAP_S, PO.JITTER) == (3, 2.0, 60.0, 0.2)
    with pytest.raises(ValueError):
        PO.backoff_s(0, "x")


def test_R02_the_breaker_opens_after_three_failed_runs_and_half_opens_after_a_doubling_cooldown():
    t = TUE
    b = PO.after_run(PO.after_run(PO.Breaker(), True, t), True, t)
    assert (b.state, b.failures) == (PO.CLOSED, 2) and PO.gate(b, t) == PO.ALLOW
    assert PO.after_run(b, False, t) == PO.Breaker()                                              # a success resets the count
    b = PO.after_run(b, True, t)
    assert (b.state, b.failures, b.cooldown_s, b.opened_at) == (PO.OPEN, 3, 300.0, t)
    assert PO.gate(b, t + timedelta(seconds=299)) == PO.CIRCUIT_OPEN and PO.gate(b, t + timedelta(seconds=300)) == PO.TRIAL
    cooldowns, now = [], t
    for _ in range(6):
        now = b.opened_at + timedelta(seconds=b.cooldown_s)
        tr = PO.start_trial(b, now)
        assert tr.state == PO.HALF_OPEN and PO.gate(tr, now + timedelta(seconds=1)) == PO.TRIAL_RUNNING
        assert PO.gate(tr, now + timedelta(seconds=PO.TRIAL_STALE_S)) == PO.TRIAL                 # a trial that died with its worker
        b = PO.after_run(tr, True, now)
        cooldowns.append(b.cooldown_s)
    assert cooldowns == [600.0, 1200.0, 2400.0, 3600.0, 3600.0, 3600.0]
    assert PO.after_run(PO.start_trial(b, now), False, now) == PO.Breaker()


# ============================================================================================================ failures (E)
def test_E01_timeouts_and_connection_errors_are_retried_with_backoff_and_count_against_the_breaker(tmp_path):
    e = env(tmp_path, outcomes=[TR.TransportTimeout("slow"), news([art(1, TUE - timedelta(hours=1))])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], r["failed_fetches"], r["unrecovered"], r["inserted"]) == ("COMPLETED", 2, 1, 0, 1)
    assert e.ft.sent[0]["params"] == e.ft.sent[1]["params"] and e.clk.sleeps == [PO.backoff_s(1, key_of(e.ft.sent[0]))]
    assert fetches(e, r["dataset_id"]) == ["TIMEOUT", "OK"] and e.store.dataset(r["dataset_id"])["status"] == "COMPLETED"   # recovered by the identical request
    wm = e.state.watermark("alpaca_news", NV.key())
    e.clk.tick(120)
    e.ft.push(*[TR.TransportConnectionError("reset")] * 3)
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], r["unrecovered"], r["stop"], r["last_error"]) == ("FAILED", 3, 1, "RETRIES_EXHAUSTED", "TRANSPORT:TransportConnectionError")
    assert e.state.watermark("alpaca_news", NV.key()) == wm and e.state.breaker("alpaca_news").failures == 1


def test_E02_http_429_waits_for_retry_after_within_the_cap_and_repeated_429s_raise_plan_pressure(tmp_path):
    e = env(tmp_path, outcomes=[Response(429, {"Retry-After": "7"}), news([])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], e.clk.sleeps) == ("EMPTY", 2, [7.0])                      # max(backoff ~2 s, Retry-After 7 s)
    e.clk.tick(120)
    e.ft.push(Response(429, {"Retry-After": "120"}), news([]))
    assert e.col.collect(NEWS, NV)["status"] == "EMPTY" and e.clk.sleeps[-1] == 30.0               # Retry-After capped at 30 s (Stage 7.0)
    b = e.col.budgets["ALPACA"]
    assert b.pressure and b.bucket.per_minute == 20.0                                             # two 429s in 10 minutes: half the rate
    e.clk.tick(120)
    e.ft.push(Response(429), news([]))
    assert e.col.collect(NEWS, NV)["status"] == "EMPTY" and e.clk.sleeps[-1] == 60.0               # no Retry-After: the budget's own 60 s
    e.clk.tick(120)
    e.ft.push(*[Response(429, {"Retry-After": "1"})] * 3)
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], r["stop"]) == ("FAILED", 3, "RETRIES_EXHAUSTED")
    n = len(e.ft.sent)
    assert e.col.collect(NEWS, NV)["refused"] == "RATE_LIMITED" and len(e.ft.sent) == n            # still backing off: nothing sent


def test_E03_5xx_is_retried_4xx_is_not_and_access_denied_blocks_the_provider_for_the_cycle(tmp_path):
    e = env(tmp_path, outcomes=[Response(500), Response(502), news([])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], r["failed_fetches"]) == ("EMPTY", 3, 2)
    for code, stop in ((404, "HTTP_ERROR"), (400, "HTTP_ERROR")):
        e.clk.tick(120)
        e.ft.push(Response(code))
        r = e.col.collect(NEWS, NV)
        assert (r["status"], r["requests"], r["stop"], r["last_error"]) == ("FAILED", 1, stop, f"HTTP_{code}")
    e = env(tmp_path / "denied", outcomes=[Response(403)])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], r["stop"]) == ("FAILED", 1, "ACCESS_DENIED")
    n = len(e.ft.sent)
    e.clk.tick(120)
    assert e.col.collect(NEWS, NV)["refused"] == "ACCESS_DENIED_THIS_CYCLE" and e.col.collect(SNAP, AAPL_IEX)["refused"] == "ACCESS_DENIED_THIS_CYCLE"
    assert len(e.ft.sent) == n and e.col.health(NEWS, NV)["status"] == HE.UNAVAILABLE                # the provider, every source of it
    e.col.new_cycle()
    e.ft.push(news([]))
    assert e.col.collect(NEWS, NV)["status"] == "EMPTY"


@pytest.mark.parametrize("plan,scope,body,problem", [
    (NEWS, NV, b'{"message": "forbidden"}', "ERROR_ENVELOPE"),
    (NEWS, NV, b"<html>maintenance</html>", "INVALID_JSON"),
    (NEWS, NV, b'{"news": []}', "UNEXPECTED_SHAPE"),
    (SNAP, AAPL_IEX, b'{"code": 40110000, "message": "request is not authorized"}', "ERROR_ENVELOPE"),
    (FRED, DGS10, b'{"error_code": 400, "error_message": "Bad Request.  The series does not exist."}', "ERROR_ENVELOPE"),
    (EARN, AAPL, b'{"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is ..."}', "ERROR_ENVELOPE"),
    (EARN, AAPL, b'{"Information": "The **demo** API key is for demo purposes only."}', "ERROR_ENVELOPE"),
    (HALTS, NONE, b"<html><body>Service unavailable</body></html>", "UNEXPECTED_SHAPE"),
    (HALTS, NONE, b"not xml at all", "INVALID_XML"),
])
def test_E04_malformed_bodies_and_misleading_http_200_envelopes_are_invalid_never_empty(tmp_path, plan, scope, body, problem):
    e = env(tmp_path, outcomes=[Response(200, {}, body)])
    r = e.col.collect(plan, scope)
    assert (r["status"], r["requests"], r["stop"], r["watermark"]) == ("FAILED", 1, "INVALID_RESPONSE", None)   # not retried, not EMPTY
    assert fetches(e, r["dataset_id"]) == ["INVALID_RESPONSE"] and rejected(e, r["dataset_id"]) == [("REJECTED", problem)]
    assert [m["sha256"] for m in e.archive.manifest()] == [hashlib.sha256(body).hexdigest()]     # the raw body is kept as evidence
    assert e.state.watermark(plan.source, scope.key()) is None and e.col.health(plan, scope)["status"] == HE.UNAVAILABLE


def test_E05_a_body_above_the_cap_is_too_large_never_archived_and_never_retried(tmp_path):
    e = env(tmp_path, outcomes=[Response(200, {}, b"x" * (NEWS.max_bytes + 10))])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], r["stop"], r["last_error"]) == ("FAILED", 1, "TOO_LARGE", f"BODY_OVER_{NEWS.max_bytes}_BYTES")
    assert fetches(e, r["dataset_id"]) == ["TOO_LARGE"] and e.archive.manifest() == [] and e.ft.sent[0]["max_bytes"] == NEWS.max_bytes


def test_E06_a_response_breaking_the_transport_contract_is_invalid_and_not_retried(tmp_path):
    for i, bad in enumerate((Response(200, {}, "text, not bytes"), "garbage", Response(True, {}, b"{}"), Response(200, None, b"{}"))):
        e = env(tmp_path / str(i), outcomes=[bad])
        r = e.col.collect(NEWS, NV)
        assert (r["status"], r["requests"], r["stop"], r["last_error"]) == ("FAILED", 1, "INVALID_RESPONSE", "TRANSPORT_CONTRACT"), bad
        assert e.archive.manifest() == []


# ============================================================================================================ pagination (G)
def test_G01_pages_follow_tokens_and_offsets_and_stop_on_loops_duplicate_pages_and_the_page_cap(tmp_path, monkeypatch):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path / "a", outcomes=[news([art(1, t0)], "t1"), news([art(2, t0)], "t2"), news([art(3, t0)])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["pages"], r["inserted"]) == ("COMPLETED", 3, 3) and [s["params"].get("page_token") for s in e.ft.sent] == [None, "t1", "t2"]
    e = env(tmp_path / "b", outcomes=[news([art(1, t0)], "t1"), news([art(2, t0)], "t1")])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["pages"], r["inserted"], r["watermark"]) == ("PARTIAL", "PAGINATION_LOOP", 2, 2, None)
    e = env(tmp_path / "c", outcomes=[news([art(1, t0)], "t1"), news([art(1, t0)], "t1")])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["pages"], r["inserted"], r["duplicates"]) == ("PARTIAL", "DUPLICATE_PAGE", 2, 1, 0)      # not ingested twice
    e = env(tmp_path / "d", responder=lambda req: news([art(len(e.ft.sent), t0)], f"t{len(e.ft.sent)}"))
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["requests"], r["inserted"]) == ("PARTIAL", "PAGE_LIMIT", NEWS.max_pages, NEWS.max_pages)
    monkeypatch.setattr(RU, "FRED_PAGE_ROWS", 2)
    rows = [fobs(f"2025-11-{d}", "4.1", "2025-12-01") for d in (24, 25, 26)]
    e = env(tmp_path / "e", outcomes=[fred_page(rows[:2], count=3, offset=0, limit=2), fred_page(rows[2:], count=3, offset=2, limit=2)])
    r = e.col.collect(FRED, DGS10)
    assert (r["status"], r["pages"], r["inserted"]) == ("COMPLETED", 2, 3) and [s["params"]["offset"] for s in e.ft.sent] == [0, 2]
    assert {(s["params"]["observation_start"], s["params"]["observation_end"], s["params"]["realtime_start"], s["params"]["realtime_end"]) for s in e.ft.sent} == {
        ((TUE.date() - timedelta(days=FRED.observation_days)).isoformat(), TUE.date().isoformat(), "1776-07-04", "9999-12-31")}   # 400 days, every vintage
    e = env(tmp_path / "f", outcomes=[fred_page(rows[:2], count=3, offset=0, limit=2), fred_page(rows[:2], count=3, offset=0, limit=2)])
    r = e.col.collect(FRED, DGS10)
    assert (r["status"], r["stop"], r["inserted"]) == ("PARTIAL", "INVALID_RESPONSE", 2) and fetches(e, r["dataset_id"]) == ["OK", "INVALID_RESPONSE"]


def test_G02_a_page_above_the_row_cap_is_recorded_but_never_ingested(tmp_path):
    items = [hitem(f"Z{chr(65 + i // 676 % 26)}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}") for i in range(HALTS.max_rows_per_run + 1)]
    e = env(tmp_path, outcomes=[rss(*items)])
    r = e.col.collect(HALTS, NONE)
    assert (r["status"], r["stop"], r["rows_delivered"], r["inserted"]) == ("PARTIAL", "ROW_LIMIT", 0, 0)
    assert e.store.members(r["dataset_id"]) == [] and fetches(e, r["dataset_id"]) == ["OK"] and len(e.archive.manifest()) == 1
    e.clk.tick(60)
    e.ft.push(rss(*items[:HALTS.max_rows_per_run]))
    assert e.col.collect(HALTS, NONE)["inserted"] == HALTS.max_rows_per_run                        # exactly the cap is fine


# ============================================================================================================ idempotency (I)
def test_I01_re_deliveries_are_duplicates_and_late_arrivals_and_revisions_inside_the_overlap_are_kept_once(tmp_path):
    a3 = TUE - timedelta(seconds=300)
    e = env(tmp_path, outcomes=[news([art(1, TUE - timedelta(hours=1)), art(3, a3, headline="v1")])])
    r1 = e.col.collect(NEWS, NV)
    assert (r1["status"], r1["inserted"], r1["watermark"]) == ("COMPLETED", 2, ST.ts(TUE))
    e.clk.tick(120)
    late = TUE - timedelta(seconds=120)                                                            # created before run 1's end, delivered only now
    e.ft.push(news([art(3, a3, headline="v2", updated=TUE + timedelta(seconds=60)), art(4, late), art(5, TUE + timedelta(seconds=60))]))
    r2 = e.col.collect(NEWS, NV)
    assert r2["window"] == [ST.ts(TUE - timedelta(seconds=NEWS.overlap_s)), ST.ts(TUE + timedelta(seconds=120))]    # the overlap re-reads 10 minutes
    assert (r2["status"], r2["revised"], r2["inserted"], r2["duplicates"]) == ("COMPLETED", 1, 2, 0)
    k3 = next(m["natural_key"] for m in e.store.members(r2["dataset_id"]) if m["natural_key"].endswith("|3"))
    assert [x["payload"]["headline"] for x in e.store.revisions(k3)] == ["v1", "v2"]
    e.clk.tick(120)
    e.ft.push(news([art(3, a3, headline="v2", updated=TUE + timedelta(seconds=60)), art(5, TUE + timedelta(seconds=60))]))
    r3 = e.col.collect(NEWS, NV)
    assert (r3["status"], r3["inserted"], r3["revised"], r3["duplicates"]) == ("COMPLETED", 0, 0, 2) and len(e.store.revisions(k3)) == 2
    f = env(tmp_path / "fred", outcomes=[fred_page([fobs("2025-11-28", "4.1", "2025-12-01")])])
    assert f.col.collect(FRED, DGS10)["inserted"] == 1
    f.clk.tick(86400)                                                                              # the next day: a new vintage, the old one closed
    f.ft.push(fred_page([fobs("2025-11-28", "4.1", "2025-12-01", "2025-12-02"), fobs("2025-11-28", "4.2", "2025-12-03")]))
    r = f.col.collect(FRED, DGS10)
    assert (r["status"], r["revised"], r["inserted"]) == ("COMPLETED", 1, 1)


def test_I02_a_repeated_row_in_one_page_is_a_duplicate_and_a_conflicting_one_is_rejected(tmp_path):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path / "a", outcomes=[news([art(1, t0), art(1, t0)])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["inserted"], r["duplicates"]) == ("COMPLETED", 1, 1)
    e = env(tmp_path / "b", outcomes=[news([art(1, t0), art(1, t0, headline="changed")])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["inserted"], r["rejected"], r["watermark"]) == ("PARTIAL", 1, 1, None)
    assert rejected(e, r["dataset_id"]) == [("REJECTED", "CONFLICT_IN_DATASET")]


# ============================================================================================================ watermarks (W)
def test_W01_the_schema_refuses_a_watermark_for_an_incomplete_run_a_future_or_backwards_one_and_any_edit(tmp_path):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path, outcomes=[news([art(1, t0)], "t1"), news([art(2, t0)], "t1")])
    partial = e.col.collect(NEWS, NV)
    assert partial["status"] == "PARTIAL" and e.state.watermarks("alpaca_news", NV.key()) == []
    ins = "INSERT INTO cs_watermarks (source, scope_key, run_id, watermark, at) VALUES (?,?,?,?,?)"
    c = sqlite3.connect(str(e.state.path))
    try:                                                                                           # raw SQL, a watermark at the run's own end
        with pytest.raises(sqlite3.DatabaseError, match="WATERMARK_NEEDS_COMPLETE_RUN"):
            c.execute(ins, ("alpaca_news", NV.key(), partial["run_id"], ST.ts(e.clk()), ST.ts(e.clk())))
    finally:
        c.close()
    e.clk.tick(120)
    e.ft.push(news([art(3, t0)]))
    done = e.col.collect(NEWS, NV)
    assert done["status"] == "COMPLETED" and [w["run_id"] for w in e.state.watermarks("alpaca_news", NV.key())] == [done["run_id"]]
    lease, _, _ = e.state.begin(source="alpaca_news", scope_key=NV.key(), provider="ALPACA", worker="w9", now=e.clk(), ttl_s=60, concurrency=1)
    e.state.start_run(run_id="manual", source="alpaca_news", scope_key=NV.key(), scope={}, worker="w9", lease_id=lease, plan_sha256="x", started_at=e.clk(),
                      window=None, gap=None, dataset_id="d", trial=False)
    with pytest.raises(sqlite3.DatabaseError, match="WATERMARK_NEEDS_COMPLETE_RUN"):
        e.state.end_run("manual", status="PARTIAL", now=e.clk(), stop=None, failed=False, clock_skew=False, last_error="", content_sha256=None, counts={},
                        watermark=e.clk())
    assert e.state.last_run("alpaca_news", NV.key())["status"] is None                               # all or nothing: no end was written either
    e.state.end_run("manual", status="COMPLETED", now=e.clk(), stop=None, failed=False, clock_skew=False, last_error="", content_sha256=None, counts={},
                    verified=True, breaker_failed=True)
    assert e.state.breaker("alpaca_news").failures == 1
    e.state.start_run(run_id="unverified", source="alpaca_news", scope_key=NV.key(), scope={}, worker="w9", lease_id=lease, plan_sha256="x",
                      started_at=e.clk(), window=None, gap=None, dataset_id="d2", trial=False)
    with pytest.raises(sqlite3.DatabaseError, match="WATERMARK_NEEDS_COMPLETE_RUN"):                 # COMPLETED in the store, not verified
        e.state.end_run("unverified", status="COMPLETED", now=e.clk(), stop=None, failed=False, clock_skew=False, last_error="", content_sha256=None,
                        counts={}, watermark=e.clk())
    e.state.end_run("unverified", status="COMPLETED", now=e.clk(), stop=None, failed=False, clock_skew=False, last_error="PENDING_ANCHOR:OSError",
                    content_sha256=None, counts={})
    e.state.start_run(run_id="mislabelled", source="alpaca_news", scope_key=NV.key(), scope={}, worker="w9", lease_id=lease, plan_sha256="x",
                      started_at=e.clk(), window=None, gap=None, dataset_id="d3", trial=False)
    with pytest.raises(sqlite3.DatabaseError, match="WATERMARK_NEEDS_COMPLETE_RUN"):                 # PARTIAL is never complete, whatever its flag
        e.state.end_run("mislabelled", status="PARTIAL", now=e.clk(), stop=None, failed=False, clock_skew=False, last_error="", content_sha256=None,
                        counts={}, watermark=e.clk(), verified=True)
    c = sqlite3.connect(str(e.state.path))
    try:
        for args, msg in (((("halts", NV.key(), "manual", ST.ts(e.clk()), ST.ts(e.clk())),), "WATERMARK_NEEDS_COMPLETE_RUN"),
                          ((("alpaca_news", NV.key(), "unverified", ST.ts(e.clk()), ST.ts(e.clk())),), "WATERMARK_NEEDS_COMPLETE_RUN"),
                          ((("alpaca_news", NV.key(), "manual", ST.ts(e.clk() + timedelta(seconds=1)), ST.ts(e.clk())),), "WATERMARK_IN_FUTURE"),
                          ((("alpaca_news", NV.key(), "manual", ST.ts(TUE - timedelta(days=1)), ST.ts(e.clk())),), "WATERMARK_BACKWARDS"),
                          ((("alpaca_news", NV.key(), done["run_id"], ST.ts(e.clk()), ST.ts(e.clk())),), "UNIQUE")):
            with pytest.raises(sqlite3.DatabaseError, match=msg):
                c.execute(ins, args[0])
        for t in SS.TABLES:
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                c.execute(f"UPDATE {t} SET rowid = rowid")
            with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
                c.execute(f"DELETE FROM {t}")
    finally:
        c.close()


def test_W02_a_partial_ingestion_failure_keeps_the_watermark_and_the_next_run_completes_idempotently(tmp_path, monkeypatch):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path, outcomes=[news([art(1, t0)], "t1"), news([art(2, t0)])])
    real, calls = e.store.ingest, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*a, **k)
    monkeypatch.setattr(e.store, "ingest", flaky)
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["inserted"], r["watermark"]) == ("PARTIAL", "ERROR:OperationalError", 1, None)
    assert e.state.breaker("alpaca_news").failures == 0                                            # not a provider failure
    monkeypatch.setattr(e.store, "ingest", real)
    e.clk.tick(120)
    e.ft.push(news([art(1, t0)], "t1"), news([art(2, t0)]))
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["duplicates"], r["inserted"]) == ("COMPLETED", 1, 1) and r["watermark"] == ST.ts(e.clk())
    assert all(len(e.store.revisions(m["natural_key"])) == 1 for m in e.store.members(r["dataset_id"]))


def test_W03_a_crash_between_the_seal_and_the_watermark_only_recollects_the_window(tmp_path, monkeypatch):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path, outcomes=[news([art(1, t0), art(2, t0)])])
    real = e.state.end_run
    monkeypatch.setattr(e.state, "end_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError, match="power cut"):
        e.col.collect(NEWS, NV)
    monkeypatch.setattr(e.state, "end_run", real)
    run = e.state.last_run("alpaca_news", NV.key())
    assert run["status"] is None and e.store.dataset(run["dataset_id"])["status"] == "COMPLETED" and e.state.watermark("alpaca_news", NV.key()) is None
    assert e.col.recover()["interrupted"] == [run["run_id"]] and e.state.last_run("alpaca_news", NV.key())["status"] == "INTERRUPTED"
    e.clk.tick(120)
    e.ft.push(news([art(1, t0), art(2, t0)]))
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["duplicates"], r["inserted"]) == ("COMPLETED", 2, 0) and e.state.watermark("alpaca_news", NV.key()) == e.clk()
    assert e.store.verify_integrity(anchors=e.archive.anchors())["state"] == "VERIFIED"


def test_W04_an_outage_beyond_the_lookback_records_a_gap_and_an_earlier_window_needs_a_historical_approval(tmp_path):
    e = env(tmp_path, outcomes=[news([])])
    assert e.col.collect(NEWS, NV)["watermark"] == ST.ts(TUE)
    e.clk.t = TUE + timedelta(hours=48)                                                            # Thursday: 48 h without a run
    e.ft.push(news([]))
    r = e.col.collect(NEWS, NV)
    floor = e.clk() - timedelta(seconds=NEWS.lookback_max_s)
    assert r["window"] == [ST.ts(floor), ST.ts(e.clk())] and r["gap"] == [ST.ts(TUE), ST.ts(floor)]
    h = e.col.health(NEWS, NV)
    assert (h["gap_count"], h["gaps"]) == (1, [[ST.ts(TUE), ST.ts(floor)]])
    n = len(e.ft.sent)
    early = e.col.collect(NEWS, NV, window_start=e.clk() - timedelta(seconds=NEWS.lookback_max_s + 1))
    assert (early["refused"], len(e.ft.sent)) == ("HISTORICAL_WINDOW_NEEDS_APPROVAL", n)
    assert e.col.collect(SNAP, AAPL_IEX, window_start=e.clk())["refused"] == "SCOPE_INVALID"        # a current-state poll has no window
    e.clk.tick(120)
    e.ft.push(news([]))
    ok = e.col.collect(NEWS, NV, window_start=e.clk() - timedelta(hours=1))
    assert ok["status"] == "EMPTY" and ok["window"][0] == ST.ts(e.clk() - timedelta(hours=1))


# ============================================================================================================ clock skew (K)
def test_K01_a_row_from_the_future_is_rejected_never_rewritten_and_holds_the_watermark(tmp_path):
    assert (FR.CLOCK_SKEW_S, VA.CLOCK_SKEW_S, ST.SKEW) == (2.0, 2.0, timedelta(seconds=2))         # the 7.1 tolerance, unchanged
    e = env(tmp_path, responder=lambda req: snaps(AAPL=e.clk() + timedelta(seconds=10)))
    r = e.col.collect(SNAP, AAPL_IEX)
    assert (r["status"], r["rejected"], r["clock_skew"], r["watermark"]) == ("PARTIAL", 1, True, None)
    assert rejected(e, r["dataset_id"]) == [("REJECTED", "OBSERVED_AFTER_RETRIEVAL")]
    e.clk.tick(60)
    e.ft._responder = lambda req: snaps(AAPL=e.clk() + timedelta(seconds=1.5))                      # within the tolerance: stored as given
    r = e.col.collect(SNAP, AAPL_IEX)
    assert (r["status"], r["inserted"], r["clock_skew"]) == ("COMPLETED", 1, False)
    row = e.store.latest_usable("QUOTE_SNAPSHOT", at=e.clk() + timedelta(seconds=5), symbol="AAPL", feed="iex")
    assert row["observed_at"] == ST.ts(e.clk() + timedelta(seconds=1.5))                            # the provider's time, never rewritten
    e.clk.tick(60)
    e.ft._responder = lambda req: snaps(AAPL=e.clk() + timedelta(seconds=10))
    e.col.collect(SNAP, AAPL_IEX)
    h = e.col.health(SNAP, AAPL_IEX)
    assert (h["status"], h["reason"], h["clock_skew"]) == (HE.DEGRADED, "LAST_RUN_PARTIAL", True)


def test_K02_the_http_date_header_marks_clock_skew_beyond_two_seconds(tmp_path):
    e = env(tmp_path, responder=lambda req: news([], **{"Date": format_datetime(e.clk() + timedelta(seconds=5), usegmt=True)}))
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["clock_skew"]) == ("EMPTY", True)
    assert (e.col.health(NEWS, NV)["status"], e.col.health(NEWS, NV)["reason"]) == (HE.DEGRADED, "CLOCK_SKEW")
    e.clk.tick(120)
    e.ft._responder = lambda req: news([], **{"date": format_datetime(e.clk() + timedelta(seconds=1), usegmt=True)})
    assert e.col.collect(NEWS, NV)["clock_skew"] is False and e.col.health(NEWS, NV)["status"] == HE.AVAILABLE
    e.clk.tick(120)
    e.ft._responder = lambda req: news([], **{"Date": "not a date"})
    assert e.col.collect(NEWS, NV)["clock_skew"] is False


def test_K03_a_clock_behind_the_watermark_never_runs(tmp_path):
    e = env(tmp_path, outcomes=[news([]), rss()])
    e.col.collect(NEWS, NV)
    e.col.collect(HALTS, NONE)
    e.clk.t = TUE - timedelta(minutes=5)
    n = len(e.ft.sent)
    assert [e.col.collect(p, s)["refused"] for p, s in ((NEWS, NV), (HALTS, NONE))] == ["CLOCK_BEHIND_WATERMARK"] * 2 and len(e.ft.sent) == n
    assert Scheduler(e.col, [(NEWS, NV), (HALTS, NONE)]).due() == []


def test_K04_an_article_from_the_future_is_out_of_range_and_never_stored(tmp_path):
    e = env(tmp_path, outcomes=[news([art(1, TUE - timedelta(minutes=5)), art(2, TUE + timedelta(seconds=30))])])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["inserted"], r["problems"], r["watermark"]) == ("PARTIAL", 1, 1, None)
    assert rejected(e, r["dataset_id"]) == [("REJECTED", "OUT_OF_RANGE")]


# ============================================================================================================ interruption (Z)
def test_Z01_a_keyboard_interrupt_seals_the_run_releases_the_lease_and_is_re_raised(tmp_path):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path, outcomes=[news([art(1, t0)], "t1"), KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        e.col.collect(NEWS, NV)
    run = e.state.last_run("alpaca_news", NV.key())
    assert (run["status"], run["stop"]) == ("PARTIAL", "INTERRUPTED:KeyboardInterrupt") and e.store.dataset(run["dataset_id"])["status"] == "PARTIAL"
    assert e.state.watermark("alpaca_news", NV.key()) is None and not e.state.lease_active(run["lease_id"], e.clk())
    again = env(tmp_path, clock=e.clk, outcomes=[news([art(1, t0)], "t1"), news([art(2, t0)])])     # a restart over the same files
    r = again.col.collect(NEWS, NV)
    assert (r["status"], r["duplicates"], r["inserted"]) == ("COMPLETED", 1, 1)


def test_Z02_after_a_hard_kill_recovery_waits_for_the_lease_then_interrupts_the_run_and_seals_its_dataset(tmp_path):
    e = env(tmp_path)
    now = e.clk()
    lease, _, _ = e.state.begin(source="alpaca_news", scope_key=NV.key(), provider="ALPACA", worker="dead", now=now, ttl_s=RU.LEASE_TTL_S, concurrency=1)
    did = e.store.create_dataset(Collector.dataset_spec(NEWS, NV, "killed", (now - timedelta(hours=1), now)))
    e.state.start_run(run_id="killed", source="alpaca_news", scope_key=NV.key(), scope={}, worker="dead", lease_id=lease, plan_sha256="x",
                      started_at=now, window=(now - timedelta(hours=1), now), gap=None, dataset_id=did, trial=False)
    obs = RU.alpaca_news.parse_page(news([art(1, now - timedelta(minutes=30))]).body, symbols=["NVDA"], retrieved_at=now, dataset_id=did)[0]
    e.store.ingest(did, obs)                                                                         # ... and then the process died
    fresh = env(tmp_path, clock=e.clk, outcomes=[news([art(1, now - timedelta(minutes=30))])])
    out = fresh.col.recover()
    assert (out["interrupted"], out["left_running"], out["store"]["left_running"]) == ([], ["killed"], [did])     # its lease is still live
    assert fresh.col.collect(NEWS, NV)["refused"] == "LEASE_HELD" and fresh.ft.sent == []
    fresh.clk.tick(RU.LEASE_TTL_S + 1)
    out = fresh.col.recover()
    assert out["interrupted"] == ["killed"] and out["store"]["sealed"] == [{"dataset_id": did, "status": "PARTIAL"}] and out["store"]["anchored"] == [did]
    assert fresh.store.verify_integrity(anchors=fresh.archive.anchors())["state"] == "VERIFIED"
    r = fresh.col.collect(NEWS, NV)
    assert (r["status"], r["duplicates"]) == ("COMPLETED", 1) and fresh.col.recover() == {"interrupted": [], "left_running": [],
                                                                                          "store": {"sealed": [], "anchored": [], "left_running": [], "refused": None},
                                                                                          "integrity": "OK"}


def test_Z03_request_stop_ends_the_run_at_the_next_page_boundary_and_no_further_pair_starts(tmp_path):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path / "a", responder=lambda req: (e.col.request_stop(), news([art(1, t0)], "t1"))[1])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["pages"], len(e.ft.sent), r["watermark"]) == ("PARTIAL", "SHUTDOWN", 1, 1, None)
    assert e.col.collect(HALTS, NONE)["refused"] == "SHUTDOWN" and len(e.ft.sent) == 1
    e = env(tmp_path / "b", responder=lambda req: (e.col.request_stop(), Response(500))[1])
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["requests"], r["unrecovered"]) == ("FAILED", "SHUTDOWN", 1, 1)                 # no retry after a stop
    e = env(tmp_path / "c", responder=lambda req: (sch.request_stop(), news([]))[1])
    sch = Scheduler(e.col, [(NEWS, NV), (HALTS, NONE)])
    res = sch.tick()
    assert [x["source"] for x in res] == ["alpaca_news"] and len(e.ft.sent) == 1


# ============================================================================================================ concurrency (L)
def test_L01_one_lease_per_pair_and_one_run_per_provider_and_an_expired_lease_never_blocks(tmp_path):
    seen = {}

    def responder(req):
        if not seen:
            seen["pair"] = b.col.collect(NEWS, NV)
            seen["provider"] = b.col.collect(SNAP, AAPL_IEX)
            b.ft.push(rss())
            seen["other"] = b.col.collect(HALTS, NONE)
        return news([])
    a = env(tmp_path, responder=responder, worker="A")
    b = env(tmp_path, clock=a.clk, worker="B")
    assert a.col.collect(NEWS, NV)["status"] == "EMPTY"
    assert (seen["pair"]["refused"], seen["provider"]["refused"], seen["other"]["status"]) == ("LEASE_HELD", "PROVIDER_CONCURRENCY", "EMPTY")
    assert [s["host"] for s in b.ft.sent] == ["www.nasdaqtrader.com"]                                # the refused worker sent nothing
    stale, _, _ = a.state.begin(source="halts", scope_key=NONE.key(), provider="NASDAQ_TRADER", worker="gone", now=a.clk(), ttl_s=10, concurrency=1)
    assert stale and a.col.collect(HALTS, NONE)["refused"] == "LEASE_HELD"
    a.clk.tick(61)
    a.ft._responder = lambda req: rss()
    assert a.col.collect(HALTS, NONE)["status"] == "EMPTY"                                          # the lease expired after its TTL


def test_L02_two_schedulers_on_one_state_file_and_two_threads_never_run_a_pair_twice(tmp_path):
    a = env(tmp_path, responder=lambda req: news([]), worker="A")
    b = env(tmp_path, clock=a.clk, responder=lambda req: news([]), worker="B")
    sa, sb = Scheduler(a.col, [(NEWS, NV)]), Scheduler(b.col, [(NEWS, NV)])
    assert [len(sa.tick()), len(sb.tick()), len(sa.tick())] == [1, 0, 0]
    a.clk.tick(60)
    assert [len(sb.tick()), len(sa.tick())] == [0, 0]                                               # the 120 s interval, whoever ticks
    a.clk.tick(60)
    assert [len(sb.tick()), len(sa.tick())] == [1, 0] and len(a.state.runs()) == 2
    inside, done = threading.Event(), threading.Event()

    def slow(req):
        inside.set()
        assert done.wait(10)
        return news([])
    a.ft._responder = slow
    a.clk.tick(120)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("a", a.col.collect(NEWS, NV)))
    th.start()
    assert inside.wait(10)
    out["b"] = b.col.collect(NEWS, NV)
    done.set()
    th.join(10)
    assert (out["a"]["status"], out["b"]["refused"]) == ("EMPTY", "LEASE_HELD") and len(a.state.runs()) == 3


# ============================================================================================================ budgets (B)
def test_B01_the_alpha_vantage_daily_cap_is_persisted_and_survives_a_restart(tmp_path):
    e = env(tmp_path, responder=lambda req: Response(200, {}, AV_CSV))
    for i in range(6):
        assert e.col.collect(EARN, AAPL)["status"] == "COMPLETED", i
        e.clk.tick(61)                                                                              # 1 request a minute
    n = len(e.ft.sent)
    assert e.col.collect(EARN, AAPL)["refused"] == "BUDGET_EXHAUSTED" and len(e.ft.sent) == n == 6
    assert e.store.budget_used("ALPHA_VANTAGE", TUE.date()) == 6
    restarted = env(tmp_path, clock=e.clk, responder=lambda req: Response(200, {}, AV_CSV))          # fresh in-memory budgets
    assert restarted.col.collect(EARN, AAPL)["refused"] == "BUDGET_EXHAUSTED" and restarted.ft.sent == []
    assert restarted.col.health(EARN, AAPL)["status"] == HE.BUDGET_EXHAUSTED
    restarted.clk.t = TUE + timedelta(days=1)
    assert restarted.col.collect(EARN, AAPL)["status"] == "COMPLETED"                                # a new New York day


def test_B02_per_minute_budgets_refuse_before_sending_and_a_run_waits_only_within_its_cap(tmp_path):
    e = env(tmp_path, responder=lambda req: rss())
    assert e.col.collect(HALTS, NONE)["status"] == "EMPTY"
    assert e.col.collect(HALTS, NONE)["refused"] == "RATE_LIMITED" and len(e.ft.sent) == 1            # Nasdaq: once a minute
    e.clk.tick(60)
    assert e.col.collect(HALTS, NONE)["status"] == "EMPTY"
    t0 = TUE - timedelta(hours=1)
    n = env(tmp_path / "n", outcomes=[news([art(1, t0)], "t1"), news([art(2, t0)], "t2"), news([art(3, t0)])])
    b = n.col.budgets["ALPACA"]
    for _ in range(39):
        assert b.allow()[0]                                                                         # one token left of 40
    r = n.col.collect(NEWS, NV)
    assert (r["status"], r["requests"], n.clk.sleeps) == ("COMPLETED", 3, [1.5, 1.5])               # 40/min: a token every 1.5 s
    assert n.store.budget_used("ALPACA", TUE.date()) == 3
    m = env(tmp_path / "m", responder=lambda req: (setattr(m.col.budgets["ALPACA"], "blocked_until", m.clk() + timedelta(seconds=120)),
                                                   news([art(len(m.ft.sent), t0)], f"t{len(m.ft.sent)}"))[1])
    r = m.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["requests"]) == ("PARTIAL", "BUDGET_BLOCKED_BACKOFF", 1)      # a 120 s back-off is longer than a run waits


# ============================================================================================================ breaker (C)
def test_C01_three_failed_runs_open_the_circuit_a_trial_follows_the_cooldown_and_the_state_survives_a_restart(tmp_path):
    e = env(tmp_path, outcomes=[TR.TransportConnectionError("x")] * 9)
    for _ in range(3):
        assert e.col.collect(NEWS, NV)["status"] == "FAILED"
        e.clk.tick(120)
    n = len(e.ft.sent)
    assert e.col.collect(NEWS, NV)["refused"] == "CIRCUIT_OPEN" and len(e.ft.sent) == n == 9
    assert e.col.health(NEWS, NV)["status"] == HE.CIRCUIT_OPEN and e.col.health(NEWS, NV)["consecutive_failures"] == 3
    opened = e.state.breaker("alpaca_news").opened_at
    e = env(tmp_path, clock=e.clk)                                                                  # a restart: the breaker is persisted
    assert e.col.collect(NEWS, NV)["refused"] == "CIRCUIT_OPEN" and e.ft.sent == []
    e.ft.push(rss())
    assert e.col.collect(HALTS, NONE)["status"] == "EMPTY"                                          # one source's breaker, not the others'
    e.ft.push(*[TR.TransportConnectionError("x")] * 3)
    e.clk.t = opened + timedelta(seconds=300)
    trial = e.col.collect(NEWS, NV)
    assert (trial["trial"], trial["status"]) == (True, "FAILED")
    b = e.state.breaker("alpaca_news")
    assert (b.state, b.cooldown_s) == (PO.OPEN, 600.0)
    e.clk.tick(599)
    assert e.col.collect(NEWS, NV)["refused"] == "CIRCUIT_OPEN"
    e.clk.tick(1)
    e.ft._responder = lambda req: (seen.append(e.col.collect(NEWS, PL.Scope(("AMD",)))), news([]))[1]
    seen = []
    ok = e.col.collect(NEWS, NV)
    assert (ok["trial"], ok["status"], seen[0]["refused"]) == (True, "EMPTY", "CIRCUIT_TRIAL_RUNNING")
    assert e.state.breaker("alpaca_news") == PO.Breaker()
    assert [x["state"] for x in e.state.breaker_events("alpaca_news")] == ["CLOSED", "CLOSED", "OPEN", "HALF_OPEN", "OPEN", "HALF_OPEN", "CLOSED"]


# ============================================================================================================ health (H)
def test_H01_health_reports_blocked_unconfigured_unavailable_fresh_aging_stale_degraded_and_daily_refresh_states(tmp_path):
    e = env(tmp_path, credentials=lambda n: n != "ALPHAVANTAGE_API_KEY", outcomes=[news([])])
    hs = {p.source: e.col.health(p, s) for p, s in ((FILINGS, AAPL), (FACTS, AAPL), (EARN, AAPL), (NEWS, NV))}
    assert [(hs[k]["status"], hs[k]["reason"]) for k in ("sec_filings", "sec_facts")] == [(HE.BLOCKED_PENDING_APPROVAL, "SEC_ACCEPTANCE_TZ_UNVERIFIED")] * 2
    assert hs["earnings_av"]["status"] == HE.NOT_CONFIGURED and e.col.collect(EARN, AAPL)["refused"] == "NOT_CONFIGURED"
    assert (hs["alpaca_news"]["status"], hs["alpaca_news"]["reason"], hs["alpaca_news"]["freshness"]["reason"]) == (HE.UNAVAILABLE, "NEVER_SUCCEEDED", "NEVER_OBSERVED")
    e.col.collect(NEWS, NV)
    seen = []
    for dt in (60, 300, 601):
        e.clk.t = TUE + timedelta(seconds=dt)
        h = e.col.health(NEWS, NV)
        seen.append((h["status"], h["freshness"]["freshness"]))
    assert seen == [(HE.AVAILABLE, "FRESH"), (HE.AVAILABLE, "AGING"), (HE.STALE, "STALE")]          # NEWS_POLL: 180 s / 600 s
    e.clk.t = SAT
    assert e.col.health(NEWS, NV)["freshness"]["status"] == "NOT_APPLICABLE"                        # outside the window: never graded current
    f = env(tmp_path / "f", outcomes=[fred_page([fobs("2025-11-28", "4.1", "2025-12-01")]), TR.TransportConnectionError("x")] + [TR.TransportConnectionError("x")] * 2)
    f.col.collect(FRED, DGS10)
    states = []
    for dt in (3600, 23 * 3600, 37 * 3600):
        f.clk.t = TUE + timedelta(seconds=dt)
        h = f.col.health(FRED, DGS10)
        states.append((h["status"], h["freshness"]["freshness"], h["freshness"]["key"]))
    assert states == [(HE.AVAILABLE, "FRESH", "DAILY_REFRESH"), (HE.AVAILABLE, "AGING", "DAILY_REFRESH"), (HE.STALE, "STALE", "DAILY_REFRESH")]
    f.clk.t = TUE + timedelta(hours=2)
    f.col.collect(FRED, DGS10)                                                                      # failed after a success, still fresh
    h = f.col.health(FRED, DGS10)
    assert (h["status"], h["reason"], h["last_status"], h["last_error"]) == (HE.DEGRADED, "LAST_RUN_FAILED", "FAILED", "TRANSPORT:TransportConnectionError")
    assert set(HE.HEALTH_STATUSES) == {"AVAILABLE", "DEGRADED", "STALE", "UNAVAILABLE", "CIRCUIT_OPEN", "BUDGET_EXHAUSTED", "NOT_CONFIGURED",
                                       "BLOCKED_PENDING_APPROVAL"}
    s = env(tmp_path / "slow", outcomes=[Response(429), news([])])                                  # a run that waited 60 s for its back-off
    assert s.col.collect(NEWS, NV)["status"] == "EMPTY" and s.clk() == TUE + timedelta(seconds=60)
    s.clk.t = TUE + timedelta(seconds=200)
    h = s.col.health(NEWS, NV)
    assert (h["last_success_at"], h["freshness"]["freshness"]) == (ST.ts(TUE), "AGING")             # measured from when the data was asked for


# ============================================================================================================ scheduler (D)
def _session(tmp_path, minutes=21):
    def responder(req):
        if req["endpoint"] == RU.alpaca_news.ENDPOINT:
            return news([])
        if req["endpoint"] == RU.alpaca_snapshots.ENDPOINT:
            return snaps(AAPL=e.clk() - timedelta(seconds=1))
        return rss()
    e = env(tmp_path, t=datetime(2025, 12, 2, 14, 20, tzinfo=UTC), responder=responder)              # 09:20 New York
    sch = Scheduler(e.col, [(HALTS, NONE), (SNAP, AAPL_IEX), (NEWS, NV)])
    log = []
    for _ in range(minutes):                                                                         # 09:20 .. 09:40, a tick a minute
        log += [(x["source"], x["status"], x.get("refused")) for x in sch.tick()]
        assert sch.tick() == []                                                                     # a repeated tick runs nothing
        e.clk.tick(60)
    return e, [(r["source"], r["started_at"], r["status"]) for r in e.state.runs()], log


def test_D01_a_simulated_session_is_deterministic_and_runs_each_pair_exactly_on_its_cadence(tmp_path):
    e, runs, log = _session(tmp_path / "one")
    _, again, _ = _session(tmp_path / "two")
    assert runs == again                                                                             # same clock, same decisions
    count = {s: sum(1 for r in runs if r[0] == s) for s in ("alpaca_news", "alpaca_snapshots", "halts")}
    assert count == {"alpaca_news": 11, "alpaca_snapshots": 2 + 11, "halts": 21}                     # 120 s; 300 s before 09:30 then 60 s; 60 s
    assert all(x[2] is None for x in log) and {x[1] for x in log} <= {"EMPTY", "COMPLETED"}
    first = [r[0] for r in runs[:3]]
    assert first == ["alpaca_news", "alpaca_snapshots", "halts"]                                     # a fixed order inside a tick
    assert e.store.budget_used("NASDAQ_TRADER", datetime(2025, 12, 2).date()) == 21                  # 1 a minute, never more


def test_D02_daily_jobs_run_once_a_new_york_day_from_their_time_and_weekends_have_no_intraday_poll(tmp_path):
    ny = lambda d, h, m: datetime(2025, 12, d, h, m, tzinfo=UTC) + timedelta(hours=5)               # noqa: E731
    e = env(tmp_path, t=ny(6, 6, 0), responder=lambda req: fred_page([]) if req["endpoint"] == RU.fred.ENDPOINT else Response(200, {}, AV_CSV))
    sch = Scheduler(e.col, [(FRED, DGS10), (EARN, AAPL), (NEWS, NV), (HALTS, NONE), (SNAP, AAPL_IEX)])
    ran = []
    for d, h, m in ((6, 6, 0), (6, 6, 29), (6, 6, 30), (6, 6, 31), (6, 7, 0), (6, 12, 0), (6, 23, 59), (7, 6, 30), (7, 7, 0)):
        e.clk.t = ny(d, h, m)
        ran += [(f"{d} {h:02d}:{m:02d}", x["source"], x["status"]) for x in sch.tick()]
    assert ran == [("6 06:30", "earnings_av", "COMPLETED"), ("6 07:00", "fred", "EMPTY"), ("7 06:30", "earnings_av", "COMPLETED"), ("7 07:00", "fred", "EMPTY")]
    with pytest.raises(ValueError):
        Scheduler(e.col, [(NEWS, NV), (NEWS, NV)])


# ============================================================================================================ invariants (X)
def test_X01_every_approval_bypass_attempt_is_refused_before_anything_is_sent_created_or_claimed(tmp_path):
    import dataclasses
    e = env(tmp_path)

    class Sub(FakeTransport):
        pass
    attempts = [
        (lambda: e.col.collect(dataclasses.replace(FILINGS, blocked_reason=None), AAPL), "PLAN_NOT_REGISTERED"),
        (lambda: e.col.collect(dataclasses.replace(NEWS, max_pages=1000), NV), "PLAN_NOT_REGISTERED"),
        (lambda: e.col.collect("alpaca_news", NV), "PLAN_NOT_REGISTERED"),
        (lambda: e.col.collect(SimpleNamespace(**{f.name: getattr(NEWS, f.name) for f in dataclasses.fields(NEWS)}), NV), "PLAN_NOT_REGISTERED"),
        (lambda: e.col.collect(FILINGS, AAPL), "BLOCKED_PENDING_APPROVAL"),
        (lambda: e.col.collect(FACTS, AAPL), "BLOCKED_PENDING_APPROVAL"),
        (lambda: e.col.collect(NEWS, NV, sink_job_kind="BACKFILL"), "LIVE_INGESTION_NOT_APPROVED"),
        (lambda: e.col.collect(NEWS, NV, sink_job_kind="IMPORT"), "LIVE_INGESTION_NOT_APPROVED"),
        (lambda: e.col.collect(NEWS, NV, window_start=TUE - timedelta(days=30)), "HISTORICAL_WINDOW_NEEDS_APPROVAL"),
        (lambda: e.col.collect(NEWS, PL.Scope(("nvda",))), "SCOPE_INVALID"),
        (lambda: e.col.collect(NEWS, PL.Scope()), "SCOPE_INVALID"),
        (lambda: e.col.collect(SNAP, PL.Scope(("AAPL",), feed="none")), "SCOPE_INVALID"),
        (lambda: e.col.collect(FRED, PL.Scope(series="NOT_A_SERIES")), "SCOPE_INVALID"),
        (lambda: e.col.collect(NEWS, ("NVDA",)), "SCOPE_INVALID"),
    ]
    for make, code in attempts:
        r = make()
        assert (r["status"], r["refused"]) == ("REFUSED", code), (code, r)
    for transport in (Sub([news([])]), TR.NoTransport(), SimpleNamespace(send=lambda *a, **k: news([]))):
        c = Collector(store=e.store, archive=e.archive, state=e.state, transport=transport, clock=e.clk, sleep=e.clk.sleep, credentials=ALL)
        assert c.collect(NEWS, NV)["refused"] == "REAL_NETWORK_NOT_APPROVED"
    for creds in (lambda n: "yes", lambda n: 1, lambda n: None, RU.nothing_configured):
        c = Collector(store=e.store, archive=e.archive, state=e.state, transport=e.ft, clock=e.clk, sleep=e.clk.sleep, credentials=creds)
        assert c.collect(NEWS, NV)["refused"] == "NOT_CONFIGURED"                                     # presence is exactly True
    halts = Collector(store=e.store, archive=e.archive, state=e.state, transport=TR.NoTransport(), clock=e.clk, sleep=e.clk.sleep).collect(HALTS, NONE)
    assert halts["refused"] == "REAL_NETWORK_NOT_APPROVED"                                          # no credential needed: past that gate, still refused
    with pytest.raises(TypeError):
        e.col.collect(NEWS, NV, approval=object())                                                   # no parameter takes a 7.1 BackfillApproval
    assert e.ft.sent == [] and not e.store.exists() and e.state.runs() == []


def test_X02_no_network_is_ever_reached_and_no_collector_module_can_reach_one(tmp_path, monkeypatch):
    from evidence import net
    calls = []
    trip = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network"))      # noqa: E731
    monkeypatch.setattr(net, "WIRE", trip)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, trip)
    monkeypatch.setattr(socket.socket, "connect", trip)
    _session(tmp_path / "s", minutes=6)
    e = env(tmp_path / "n")
    Collector(store=e.store, archive=e.archive, state=e.state, transport=TR.NoTransport(), clock=e.clk, sleep=e.clk.sleep, credentials=ALL).collect(NEWS, NV)
    assert calls == []
    for f in sorted((ROOT / "market_intel" / "collector").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for word in ("urlopen", "urllib", "http.client", "import requests", "socket", "alpaca.data", "alpaca.trading", "net.fetch", "WIRE", "threading",
                     "Thread(", "Timer(", "asyncio", "subprocess", "os.environ", "getenv", "sched.", "time.sleep", "dotenv", ".env"):
            assert word not in src, (f.name, word)


def test_X03_no_credential_value_is_ever_read_sent_or_recorded(tmp_path):
    asked = []
    e = env(tmp_path, credentials=lambda n: asked.append(n) or True,
            responder=lambda req: {RU.alpaca_news.ENDPOINT: news([]), RU.fred.ENDPOINT: fred_page([]), RU.earnings_av.ENDPOINT: Response(200, {}, AV_CSV)}[req["endpoint"]])
    for p, s in ((NEWS, NV), (FRED, DGS10), (EARN, AAPL), (FILINGS, AAPL)):
        e.col.collect(p, s)
    assert asked == ["ALPACA_API_KEY", "FRED_API_KEY", "ALPHAVANTAGE_API_KEY"]                         # names only; a blocked plan never asks
    assert [s["credential"] for s in e.ft.sent] == asked
    blobs = [json.dumps([s["params"] for s in e.ft.sent]), (e.archive.root / "manifest.jsonl").read_text(encoding="utf-8")]
    for path in (e.store.path, e.state.path):
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                blobs.append(json.dumps([list(r) for r in c.execute(f"SELECT * FROM {t}")], default=str))
        finally:
            c.close()
    for b in blobs:
        assert not [w for w in ("apikey", "api_key=", "secret", "password", "apca-api", "bearer") if w in b.lower()]


def test_X04_no_incomplete_run_ever_has_a_watermark_and_no_invalid_response_is_ever_empty_or_completed(tmp_path):
    t0 = TUE - timedelta(hours=1)
    outcomes = [news([art(1, t0)]), Response(200, {}, b"{}"), news([art(2, t0)], "t1"), news([art(3, t0)], "t1"), TR.TransportTimeout("t"),
                Response(500), Response(503), news([]), Response(200, {}, b"<html>"), news([art(5, t0), art(5, t0, headline="other")])]
    e = env(tmp_path, outcomes=outcomes)
    for _ in range(7):
        e.col.collect(NEWS, NV)
        e.clk.tick(120)
    runs = e.state.runs("alpaca_news", NV.key())
    marks = {w["run_id"] for w in e.state.watermarks("alpaca_news", NV.key())}
    assert len(runs) == 7 and {r["run_id"] for r in runs if r["status"] in ("COMPLETED", "EMPTY")} == marks
    assert [r["status"] for r in runs] == ["COMPLETED", "FAILED", "PARTIAL", "FAILED", "EMPTY", "FAILED", "PARTIAL"]
    for r in runs:
        if "INVALID_RESPONSE" in fetches(e, r["dataset_id"]):
            assert r["status"] not in ("COMPLETED", "EMPTY") and e.store.dataset(r["dataset_id"])["status"] not in ("COMPLETED", "EMPTY")


def test_Z04_a_run_that_cannot_start_or_seal_or_archive_fails_closed_and_recovers(tmp_path, monkeypatch):
    e = env(tmp_path / "a", outcomes=[TR.TransportConnectionError("x")] * 9)
    for _ in range(3):
        e.col.collect(NEWS, NV)
        e.clk.tick(120)
    e.clk.t = e.state.breaker("alpaca_news").opened_at + timedelta(seconds=300)
    real = e.store.create_dataset
    monkeypatch.setattr(e.store, "create_dataset", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    with pytest.raises(sqlite3.OperationalError):
        e.col.collect(NEWS, NV)
    assert e.state.breaker("alpaca_news").state == PO.OPEN and e.state.runs("alpaca_news", NV.key())[-1]["status"] == "FAILED"   # the trial claim handed back
    monkeypatch.setattr(e.store, "create_dataset", real)
    e.ft.push(news([]))
    r = e.col.collect(NEWS, NV)
    assert (r["trial"], r["status"]) == (True, "EMPTY")                                             # the lease was released, the trial still allowed
    f = env(tmp_path / "b", outcomes=[news([art(1, TUE - timedelta(hours=1))])])
    monkeypatch.setattr(f.store, "finish_dataset", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    with pytest.raises(RU.IG.FinalizationError):
        f.col.collect(NEWS, NV)
    monkeypatch.undo()
    run = f.state.last_run("alpaca_news", NV.key())
    assert run["status"] == "PENDING_FINALIZATION" and f.store.dataset(run["dataset_id"])["status"] == "RUNNING" and f.state.watermark("alpaca_news", NV.key()) is None
    f.clk.tick(RU.LEASE_TTL_S + 1)
    assert f.col.recover()["store"]["sealed"] == [{"dataset_id": run["dataset_id"], "status": "PARTIAL"}]
    g = env(tmp_path / "c", outcomes=[news([art(1, TUE - timedelta(hours=1))])])
    monkeypatch.setattr(g.archive, "write", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only disk")))
    r = g.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["unrecovered"], r["inserted"]) == ("FAILED", "ARCHIVE_ERROR", 1, 0) and fetches(g, r["dataset_id"]) == ["ERROR"]


def test_D03_a_new_new_york_day_starts_a_new_cycle_for_a_provider_that_denied_access(tmp_path):
    e = env(tmp_path, outcomes=[Response(403), news([])])
    sch = Scheduler(e.col, [(NEWS, NV)])
    assert [(x["status"], x["stop"]) for x in sch.tick()] == [("FAILED", "ACCESS_DENIED")]
    e.clk.tick(120)
    assert [x["refused"] for x in sch.tick()] == ["ACCESS_DENIED_THIS_CYCLE"] and len(e.ft.sent) == 1
    e.clk.t = TUE + timedelta(days=1)
    assert [x["status"] for x in sch.tick()] == ["EMPTY"]


def test_B03_two_collectors_sharing_the_files_never_spend_past_a_daily_cap_together(tmp_path, monkeypatch):
    a = env(tmp_path, responder=lambda req: Response(200, {}, AV_CSV), worker="A")
    b = env(tmp_path, clock=a.clk, responder=lambda req: Response(200, {}, AV_CSV), worker="B")
    assert b.col.collect(EARN, AAPL)["status"] == "COMPLETED"                                       # B has spent 1 (and knows it)
    for _ in range(5):
        a.clk.tick(61)
        assert a.col.collect(EARN, AAPL)["status"] == "COMPLETED"                                   # A spends the other 5
    a.clk.tick(61)
    assert b.col.collect(EARN, AAPL)["refused"] == "BUDGET_EXHAUSTED" and len(b.ft.sent) == 1        # re-read before the run, not once a day
    assert a.store.budget_used("ALPHA_VANTAGE", TUE.date()) == 6
    c = env(tmp_path / "race", responder=lambda req: Response(200, {}, AV_CSV), worker="C")
    d = env(tmp_path / "race", clock=c.clk, responder=lambda req: Response(200, {}, AV_CSV), worker="D")
    for _ in range(5):
        assert c.col.collect(EARN, AAPL)["status"] == "COMPLETED"
        c.clk.tick(61)
    real = d.state.begin

    def c_spends_first(**k):                                                                     # between D's budget check and D's claim
        assert c.col.collect(EARN, AAPL)["status"] == "COMPLETED"
        return real(**k)
    monkeypatch.setattr(d.state, "begin", c_spends_first)
    assert d.col.collect(EARN, AAPL)["refused"] == "BUDGET_EXHAUSTED" and d.ft.sent == []           # re-read under the provider lease
    assert c.store.budget_used("ALPHA_VANTAGE", TUE.date()) == 6 and d.state.open_runs() == []


def test_C02_a_trial_that_made_no_request_never_closes_the_breaker(tmp_path, monkeypatch):
    e = env(tmp_path, outcomes=[TR.TransportConnectionError("x")] * 9)
    for _ in range(3):
        e.col.collect(NEWS, NV)
        e.clk.tick(120)
    opened = e.state.breaker("alpaca_news")
    e.clk.t = opened.opened_at + timedelta(seconds=300)
    real = e.store.create_dataset
    monkeypatch.setattr(e.store, "create_dataset", lambda *a, **k: (e.col.request_stop(), real(*a, **k))[1])    # stopped before its first page
    r = e.col.collect(NEWS, NV)
    assert (r["trial"], r["stop"], r["requests"]) == (True, "SHUTDOWN", 0)
    b = e.state.breaker("alpaca_news")
    assert (b.state, b.failures, b.opened_at, b.cooldown_s) == (PO.OPEN, opened.failures, opened.opened_at, opened.cooldown_s)   # handed back, not closed


def test_L03_races_a_breaker_opened_after_the_read_and_a_watermark_advanced_by_another_worker_are_both_honoured(tmp_path, monkeypatch):
    e = env(tmp_path / "a", outcomes=[TR.TransportConnectionError("x")] * 9)
    for _ in range(3):
        e.col.collect(NEWS, NV)
        e.clk.tick(120)
    monkeypatch.setattr(e.state, "breaker", lambda source: PO.Breaker())                          # a stale read: another process just opened it
    n = len(e.ft.sent)
    assert e.col.collect(NEWS, NV)["refused"] == "CIRCUIT_OPEN" and len(e.ft.sent) == n               # the claim re-checks it, atomically
    monkeypatch.undo()
    w = env(tmp_path / "b")

    def other_worker(req):                                                                       # this run's lease expired; another worker ran
        now = w.clk()
        lease, _, _ = w.state.begin(source="alpaca_news", scope_key=NV.key(), provider="ALPACA", worker="B", now=now + timedelta(hours=1),
                                    ttl_s=60, concurrency=9)
        w.state.start_run(run_id="b-run", source="alpaca_news", scope_key=NV.key(), scope={}, worker="B", lease_id=lease, plan_sha256="x",
                          started_at=now + timedelta(seconds=100), window=None, gap=None, dataset_id="d", trial=False)
        w.state.end_run("b-run", status="COMPLETED", now=now + timedelta(seconds=100), stop=None, failed=False, clock_skew=False, last_error="",
                        content_sha256=None, counts={}, watermark=now + timedelta(seconds=100), verified=True)
        return news([])
    w.ft._responder = other_worker
    r = w.col.collect(NEWS, NV)
    assert (r["status"], r["watermark"], r["last_error"]) == ("EMPTY", None, "WATERMARK_BEHIND_CURRENT")    # never moved back, and the run ended
    assert w.state.watermark("alpaca_news", NV.key()) == TUE + timedelta(seconds=100) and w.state.open_runs() == []


def test_Z05_a_run_stops_at_the_next_page_boundary_after_its_time_limit(tmp_path):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path, responder=lambda req: news([art(len(e.ft.sent), t0)], f"t{len(e.ft.sent)}"))
    e.ft.latency_s = 100.0                                                                       # a slow provider: 100 s a page
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["stop"], r["pages"]) == ("PARTIAL", "RUN_TIME_LIMIT", 10) and RU.MAX_RUN_S == 900.0
    assert (e.clk() - TUE).total_seconds() < RU.LEASE_TTL_S and r["watermark"] is None


# ============================================================================================================ final review (V)
def test_V01_an_unanchored_or_unconfirmed_seal_is_never_a_verified_run_and_never_moves_the_watermark(tmp_path, monkeypatch):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path / "a", outcomes=[news([art(1, t0)])])
    monkeypatch.setattr(e.archive, "anchor", lambda *a, **k: (_ for _ in ()).throw(OSError("anchors file read-only")))
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["finalization"], r["verified"], r["watermark"], r["last_error"]) == ("COMPLETED", "PENDING_ANCHOR", False, None,
                                                                                                 "PENDING_ANCHOR:OSError")
    assert e.state.watermark("alpaca_news", NV.key()) is None and e.store.verify_integrity(anchors=e.archive.anchors())["state"] == "UNVERIFIED"
    h = e.col.health(NEWS, NV)
    assert (h["status"], h["reason"], h["last_success_at"]) == (HE.UNAVAILABLE, "NEVER_SUCCEEDED", None)          # unverified: not a success
    monkeypatch.undo()
    assert e.col.recover()["store"]["anchored"] == [r["dataset_id"]]                                               # anchored afterwards ...
    assert e.state.watermark("alpaca_news", NV.key()) is None                                                      # ... but never retroactively
    e.clk.tick(120)
    e.ft.push(news([art(1, t0)]))
    again = e.col.collect(NEWS, NV)                                                                                # the window is read again
    assert (again["status"], again["verified"], again["duplicates"], again["watermark"]) == ("COMPLETED", True, 1, ST.ts(e.clk()))
    monkeypatch.setattr(e.archive, "anchor", lambda *a, **k: (_ for _ in ()).throw(OSError("anchors file read-only")))
    e.clk.tick(120)
    e.ft.push(news([]))
    assert e.col.collect(NEWS, NV)["watermark"] is None and e.state.watermark("alpaca_news", NV.key()) == ST.parse_ts(again["watermark"])
    h = e.col.health(NEWS, NV)
    assert (h["status"], h["reason"], h["last_success_at"]) == (HE.DEGRADED, "PENDING_ANCHOR:OSError", again["window"][1])   # the last VERIFIED run
    monkeypatch.undo()
    f = env(tmp_path / "b", outcomes=[news([art(1, t0)])])
    real = f.store.dataset
    monkeypatch.setattr(f.store, "dataset", lambda did: {**real(did), "status": "PARTIAL"})                       # the read-back disagrees
    r = f.col.collect(NEWS, NV)
    assert (r["status"], r["verified"], r["watermark"], r["last_error"]) == ("COMPLETED", False, None, "UNVERIFIED_SEAL")
    for k in ("content_sha256", "records_sha256", "metadata_sha256"):                                              # same status, another seal
        monkeypatch.setattr(f.store, "dataset", lambda did, k=k: (lambda d: {**d, "events": d["events"][:-1] + [{**d["events"][-1], k: "0" * 64}]})(real(did)))
        f.clk.tick(120)
        f.ft.push(news([]))
        r = f.col.collect(NEWS, NV)
        assert (r["status"], r["verified"], r["watermark"], r["last_error"]) == ("EMPTY", False, None, "UNVERIFIED_SEAL"), k
    monkeypatch.setattr(f.store, "dataset", lambda did: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    f.clk.tick(120)
    f.ft.push(news([art(2, f.clk() - timedelta(seconds=30))]))
    r = f.col.collect(NEWS, NV)
    assert (r["verified"], r["watermark"], r["last_error"]) == (False, None, "UNVERIFIED_SEAL:OperationalError")
    monkeypatch.undo()
    h = f.col.health(NEWS, NV)
    assert (h["status"], h["reason"]) == (HE.UNAVAILABLE, "NEVER_SUCCEEDED") and f.state.watermarks("alpaca_news", NV.key()) == []


def test_V02_a_store_that_fails_its_integrity_check_takes_no_run_until_repaired(tmp_path):
    t0 = TUE - timedelta(minutes=30)
    e = env(tmp_path, outcomes=[news([art(1, t0)])])
    assert e.col.collect(NEWS, NV)["verified"] is True
    p = e.archive.root / "anchors.jsonl"
    entry = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    p.write_bytes((json.dumps({**entry, "content_sha256": "0" * 64}) + "\n").encode("utf-8"))                     # an anchor that disagrees
    out = e.col.recover()
    assert (out["store"]["refused"], out["integrity"], e.col.integrity) == ("CORRUPT", "STORE_INTEGRITY_FAILED", "STORE_INTEGRITY_FAILED")
    e.clk.tick(120)
    n, wm = len(e.ft.sent), e.state.watermark("alpaca_news", NV.key())
    assert [e.col.collect(pl, s)["refused"] for pl, s in ((NEWS, NV), (HALTS, NONE), (FRED, DGS10))] == ["STORE_INTEGRITY_FAILED"] * 3
    assert len(e.ft.sent) == n and e.state.watermark("alpaca_news", NV.key()) == wm
    assert (e.col.health(NEWS, NV)["status"], e.col.health(NEWS, NV)["reason"]) == (HE.UNAVAILABLE, "STORE_INTEGRITY_FAILED")
    fresh = env(tmp_path, clock=e.clk, outcomes=[news([])])                                                         # a restart ...
    sch = Scheduler(fresh.col, [(NEWS, NV)])
    assert [x["refused"] for x in sch.tick()] == ["STORE_INTEGRITY_FAILED"] and fresh.ft.sent == []                # ... recovers before its first run
    assert sch.recovery["integrity"] == "STORE_INTEGRITY_FAILED"


def test_V03_an_interrupted_seal_leaves_no_end_and_no_watermark_until_recovery_and_the_next_run(tmp_path, monkeypatch):
    t0 = TUE - timedelta(hours=1)
    e = env(tmp_path, outcomes=[news([art(1, t0)])])
    monkeypatch.setattr(e.store, "finish_dataset", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        e.col.collect(NEWS, NV)
    monkeypatch.undo()
    run = e.state.last_run("alpaca_news", NV.key())
    assert run["status"] is None and e.store.dataset(run["dataset_id"])["status"] == "RUNNING" and e.state.watermark("alpaca_news", NV.key()) is None
    assert not e.state.lease_active(run["lease_id"], e.clk())                                                      # released on the way out
    out = e.col.recover()
    assert (out["interrupted"], out["store"]["left_running"]) == ([run["run_id"]], [run["dataset_id"]])            # recent: the store waits
    e.clk.tick(RU.LEASE_TTL_S + 1)
    assert e.col.recover()["store"]["sealed"] == [{"dataset_id": run["dataset_id"], "status": "PARTIAL"}]
    e.ft.push(news([art(1, t0)]))
    r = e.col.collect(NEWS, NV)
    assert (r["status"], r["verified"], r["duplicates"]) == ("COMPLETED", True, 1) and e.state.watermark("alpaca_news", NV.key()) == e.clk()
    assert e.store.verify_integrity(anchors=e.archive.anchors())["state"] == "VERIFIED"


def test_V04_every_request_sent_has_a_persisted_allow_and_retries_never_pass_a_daily_cap(tmp_path):
    t0 = TUE - timedelta(hours=1)
    a = env(tmp_path, outcomes=[TR.TransportTimeout("t"), Response(429, {"Retry-After": "3"}), news([art(1, t0)], "t1"), Response(500), news([art(2, t0)])],
            worker="A")
    b = env(tmp_path, clock=a.clk, outcomes=[Response(503), news([])], worker="B")
    runs = [a.col.collect(NEWS, NV)]
    a.clk.tick(120)
    runs.append(b.col.collect(NEWS, NV))
    c = sqlite3.connect(f"file:{a.store.path.as_posix()}?mode=ro", uri=True)
    try:
        per_ds = dict(c.execute("SELECT dataset_id, COUNT(*) FROM mi_budget_events WHERE event = 'ALLOW' GROUP BY dataset_id").fetchall())
    finally:
        c.close()
    assert [per_ds[r["dataset_id"]] for r in runs] == [r["requests"] for r in runs] == [5, 2]                        # retries included
    assert sum(per_ds.values()) == len(a.ft.sent) + len(b.ft.sent) == a.store.budget_used("ALPACA", TUE.date())
    v = env(tmp_path / "av", responder=lambda req: Response(200, {}, AV_CSV))
    for _ in range(5):
        assert v.col.collect(EARN, AAPL)["status"] == "COMPLETED"
        v.clk.tick(61)
    v.ft._responder = lambda req: Response(500)
    r = v.col.collect(EARN, AAPL)                                                                                   # the 6th request fails ...
    assert (r["status"], r["stop"], r["requests"], r["unrecovered"]) == ("FAILED", "BUDGET_DAILY_CAP", 1, 1)       # ... and is not retried past 6
    assert len(v.ft.sent) == 6 == v.store.budget_used("ALPHA_VANTAGE", TUE.date())


def test_V05_the_fred_window_is_400_calendar_days_of_observation_dates_inclusive_on_the_new_york_date_and_enforced_by_the_store(tmp_path):
    assert RU.fred_dates(FRED, datetime(2025, 12, 3, 4, 30, tzinfo=UTC)) == (datetime(2024, 10, 28).date(), datetime(2025, 12, 2).date())   # 23:30 NY Dec 2
    assert RU.fred_dates(FRED, datetime(2025, 12, 3, 5, 0, tzinfo=UTC)) == (datetime(2024, 10, 29).date(), datetime(2025, 12, 3).date())    # 00:00 NY Dec 3
    assert RU.fred_dates(FRED, datetime(2025, 7, 1, 3, 59, tzinfo=UTC))[1] == datetime(2025, 6, 30).date()                                  # EDT
    first, last = RU.fred_dates(FRED, TUE)
    assert ((last - first).days, first.isoformat(), last.isoformat()) == (400, "2024-10-28", "2025-12-02")         # 401 calendar dates, both inclusive
    e = env(tmp_path / "edges", outcomes=[fred_page([fobs("2024-10-28", "4.0", "2024-10-29"), fobs("2025-12-02", "4.1", "2025-12-02")])])
    r = e.col.collect(FRED, DGS10)
    assert (r["status"], r["inserted"], r["verified"]) == ("COMPLETED", 2, True)                                   # both edges are inside
    ds = e.store.dataset(r["dataset_id"])
    assert (ds["start_date"], ds["end_date"]) == ("2024-10-28", "2025-12-02")                                      # the dataset declares the window
    for i, d in enumerate(("2024-10-27", "2023-01-01")):
        f = env(tmp_path / f"out{i}", outcomes=[fred_page([fobs(d, "3.9", "2024-10-27"), fobs("2025-11-28", "4.1", "2025-12-01")])])
        r = f.col.collect(FRED, DGS10)
        assert (r["status"], r["inserted"], r["rejected"], r["watermark"]) == ("PARTIAL", 1, 1, None), d           # outside: never complete
        assert rejected(f, r["dataset_id"]) == [("REJECTED", f"SCOPE_DATE:{d}")]
    g = env(tmp_path / "future", outcomes=[fred_page([fobs("2025-12-03", "4.2", "2025-12-02")])])
    r = g.col.collect(FRED, DGS10)
    assert (r["status"], r["rejected"], r["watermark"]) == ("PARTIAL", 1, None) and rejected(g, r["dataset_id"]) == [("REJECTED", "SCOPE_DATE:2025-12-03")]


def test_V06_an_incomplete_daily_job_is_never_complete_and_runs_again_the_next_new_york_day(tmp_path):
    ny7 = datetime(2025, 12, 2, 12, 0, tzinfo=UTC)                                                                  # 07:00 New York
    e = env(tmp_path, t=ny7, outcomes=[KeyboardInterrupt()])
    sch = Scheduler(e.col, [(FRED, DGS10)])
    with pytest.raises(KeyboardInterrupt):
        sch.tick()
    e.clk.tick(3600)
    assert sch.due() == [] and e.state.watermark("fred", DGS10.key()) is None                                       # once a New York day (decision pending)
    h = e.col.health(FRED, DGS10)
    assert h["status"] in (HE.UNAVAILABLE, HE.DEGRADED) and h["last_success_at"] is None                            # never shown as complete
    e.clk.t = ny7 + timedelta(days=1)
    e.ft.push(fred_page([fobs("2025-11-28", "4.1", "2025-12-01")]))
    assert [(x["status"], x["verified"]) for x in sch.tick()] == [("COMPLETED", True)]


# ============================================================================================================ isolation (S)
NEW_MODULES_73 = ("collector/__init__.py", "collector/plans.py", "collector/transport.py", "collector/policy.py", "collector/state.py",
                  "collector/runner.py", "collector/scheduler.py", "collector/health.py")
ALLOWED_73 = {"__future__", "collections", "collections.abc", "contextlib", "dataclasses", "datetime", "email.utils", "hashlib", "json", "pathlib",
              "sqlite3", "types", "typing", "uuid", "zoneinfo", "config", "api.validation", "market_intel", "market_intel.calendar", "market_intel.freshness",
              "market_intel.ingest", "market_intel.store", "market_intel.validate", "market_intel.budget", "market_intel.settings", "market_intel.models",
              "market_intel.sources", "market_intel.sources.common", "market_intel.sources.fred", "market_intel.collector", "market_intel.collector.state"}


def test_S01_the_collector_is_a_closed_lazy_registry_that_nothing_outside_imports(tmp_path):
    for rel in NEW_MODULES_73:
        tree = ast.parse((ROOT / "market_intel" / rel).read_text(encoding="utf-8"))
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                                    for a in n.names}
        assert names <= ALLOWED_73, (rel, sorted(names - ALLOWED_73))
    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel" / "collector").rglob("*.py")}
    assert present == set(NEW_MODULES_73) and {f"market_intel/{m}" for m in NEW_MODULES_73} <= FL.NEW_73
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "market_scan/", "tests/", ".venv/",
             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]
    assert users == []
    files = lambda: sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))          # noqa: E731
    clk = Clock(TUE)
    state, store = CollectorState(tmp_path / "c" / "collector.db"), MarketIntelStore(tmp_path / "s" / "mi.db", clock=clk, approvals_root=tmp_path / "a")
    col = Collector(store=store, archive=RawArchive(tmp_path / "r"), state=state, transport=FakeTransport(), clock=clk, sleep=clk.sleep)
    sch = Scheduler(col, [(NEWS, NV), (FRED, DGS10)])
    col.health(NEWS, NV), sch.due(), state.runs(), state.watermark("alpaca_news", NV.key()), state.breaker("x"), col.recover()
    assert files() == []                                                                             # constructing, reading and recovering create nothing
    assert CollectorState().path == ROOT / "runtime" / "market_intel" / "collector.db"
    assert subprocess.run(["git", "check-ignore", "-q", "runtime/market_intel/collector.db"], cwd=ROOT).returncode == 0
    assert not (ROOT / "runtime" / "market_intel" / "collector.db").exists() and not (ROOT / "data" / "market_intel.db").exists()


def test_S02_the_state_file_verifies_its_schema_and_refuses_a_reset_or_foreign_one(tmp_path):
    e = env(tmp_path, outcomes=[news([])])
    e.col.collect(NEWS, NV)
    assert SS.expected_objects() and len(e.state.runs()) == 1
    for sql, path in (("CREATE TABLE extra (x)", "extra.db"), ("PRAGMA user_version = 0", "reset.db"), ("PRAGMA user_version = 2", "newer.db"),
                      ("DROP TRIGGER cs_watermarks_need_complete_run", "trigger.db")):
        p = tmp_path / path
        p.write_bytes(e.state.path.read_bytes())
        c = sqlite3.connect(str(p))
        c.execute(sql)
        c.commit()
        c.close()
        bad = CollectorState(p)
        with pytest.raises(StateMismatch):
            bad.runs()
        with pytest.raises(StateMismatch):
            bad.begin(source="halts", scope_key="x", provider="NASDAQ_TRADER", worker="w", now=TUE, ttl_s=60, concurrency=1)


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    return _session(tmp_path_factory.mktemp("session73"))


def test_S03_the_stage_71_store_is_used_unchanged_fixture_datasets_only_and_verified(session):
    from market_intel import migrations as MG
    e, _, _ = session
    assert MG.SCHEMA_VERSION == 1 and e.store.schema_version() == 1 and RU.SINK_JOB_KIND == "FIXTURE"
    c = sqlite3.connect(f"file:{e.store.path.as_posix()}?mode=ro", uri=True)
    try:
        kinds = {r[0] for r in c.execute("SELECT job_kind FROM mi_datasets")}
        reqs = [json.loads(r[0]) for r in c.execute("SELECT request_json FROM mi_datasets")]
    finally:
        c.close()
    assert kinds == {"FIXTURE"} and {q["collector"] for q in reqs} == {RU.COLLECTOR_VERSION} and len(reqs) == len(e.state.runs())
    v = e.store.verify_integrity(anchors=e.archive.anchors())
    assert (v["state"], v["unsealed_datasets"], v["unanchored_datasets"]) == ("VERIFIED", [], []) and e.archive.verify()["ok"]


# ============================================================================================================ bounds (M)
def test_M01_requests_pages_rows_bytes_and_state_rows_per_run_are_bounded(session):
    e, runs, _ = session
    assert all(s["max_bytes"] == PL.PLANS[{RU.alpaca_news.ENDPOINT: "alpaca_news", RU.alpaca_snapshots.ENDPOINT: "alpaca_snapshots"}.get(s["endpoint"], "halts")].max_bytes
               for s in e.ft.sent)
    c = sqlite3.connect(f"file:{e.state.path.as_posix()}?mode=ro", uri=True)
    try:
        n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in SS.TABLES}
    finally:
        c.close()
    k = len(runs)
    assert n == {"cs_runs": k, "cs_run_ends": k, "cs_watermarks": k, "cs_leases": k, "cs_lease_releases": k, "cs_breaker_events": 0}
    assert all(p.max_pages <= 20 and p.max_rows_per_run <= ST.MAX_DATASET_ROWS and p.max_bytes <= 20 * 1024 * 1024 for p in PL.PLANS.values())
    assert RU.MAX_RUN_S < RU.LEASE_TTL_S == PO.TRIAL_STALE_S and RU.MAX_BUDGET_WAIT_S <= 60



# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_73}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_73} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #44 (Stage 7.3 merged; the Stage 7.3.1 base): what Stage 7.3 left, whatever a later stage changed since."""
    run = subprocess.run(["git", "show", f"{FL.BASE_731}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_731} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


APP_EDITS_73 = {".github/ci/pytest_groups.json"}
FROZEN_DIRS_73 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                  "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                  "database", "data", "value_quality", "market_intel")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_73 == "7376568260bcd8e18282593fbc82b4927248644a" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_73}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_73).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    stage = set(_git("diff", "--name-only", FL.BASE_73, FL.BASE_731).split())               # Stage 7.3 itself, between two fixed commits
    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}
    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75                           # Stages 7.3.1, 7.4: pinned by their own F tests
    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),
                                                         "missing": sorted((FL.NEW_73 | FL.EDITED_73 | later) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_73).split())
    assert {p for p in stage if p not in in_base} == FL.NEW_73 and {p for p in stage if p in in_base} == FL.EDITED_73
    assert FL.NEW_73 == {"DESIGN_73_MARKET_INTEL_COLLECTOR.md", "tests/test_market_intel_collector_73.py"} | {f"market_intel/{m}" for m in NEW_MODULES_73}
    assert {p for p in FL.EDITED_73 if not p.startswith("tests/")} == APP_EDITS_73
    assert FL.SEMANTIC_73 == {} and FL.INSTRUMENTATION_73 == {}
    for d in FROZEN_DIRS_73:                                                                   # every existing module, the 7.0-7.2 market_intel included
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert post(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):
        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook or dependency
    for rel in sorted(FL.NEW_73 | FL.EDITED_73):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in post(rel) and m54 not in post(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_market_intel():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    from market_intel.settings import settings as mi_settings
    assert mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_", "ALPHAVANTAGE"))]          # no new key (config.py itself: F01)
    import setups as SU
    assert (SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.EVIDENCE_MAX_AGE_DAYS) == (40, 60, 30)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_73.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_73) == FL.EDITED_73 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.3 (market intelligence collector")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_70 + NEW_71 + NEW_72 + NEW_73 plus this ledger.", " + NEW_70 + NEW_71 + NEW_72 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_73 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_73: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.3 left
    assert set(new_led) == set(old)                                                         # no new ledger key
    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_73 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_73 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72                             # Stages 7.1, 7.2: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('\nAPP_EDITS_72 = {".github/ci/pytest_groups.json"}\n',
         '\ndef post(rel):\n    """main after PR #43 (Stage 7.2 merged; the Stage 7.3 base): what Stage 7.2 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_73}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_73} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_72 = {".github/ci/pytest_groups.json"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_72).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_72, FL.BASE_73).split())                # Stage 7.2 itself, between two fixed commits\n    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73                                                         # Stage 7.3, pinned by its own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n                                                         "missing": sorted((FL.NEW_72 | FL.EDITED_72 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_72).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_72).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_72 and {p for p in changed if p in in_base} == FL.EDITED_72\n    assert FL.NEW_72 == {"DESIGN_72_MARKET_INTEL_SOURCES.md", "tests/test_market_intel_sources_72.py"} | {f"market_intel/{m}" for m in NEW_MODULES_72}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_72).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_72 and {p for p in stage if p in in_base} == FL.EDITED_72\n    assert FL.NEW_72 == {"DESIGN_72_MARKET_INTEL_SOURCES.md", "tests/test_market_intel_sources_72.py"} | {f"market_intel/{m}" for m in NEW_MODULES_72}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert now_text(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore"):\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert post(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore"):\n'),
        ('    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore"):\n        assert now_text(rel) == at_base(rel), rel\n    for rel in sorted(FL.NEW_72 | FL.EDITED_72):\n',
         '    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore"):\n        assert post(rel) == at_base(rel), rel\n    for rel in sorted(FL.NEW_72 | FL.EDITED_72):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_72.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_72.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_72) == FL.EDITED_72 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.2 (market intelligence event")\n',
         '    assert set(REPLAY_72) == FL.EDITED_72 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.2 (market intelligence event")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_72 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.2 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_72 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 if p.startswith("market_intel/") and p.endswith(".py")}   # Stage 7.2: its own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n',
         '    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 | FL.NEW_73 if p.startswith("market_intel/") and p.endswith(".py")}   # Stages 7.2, 7.3: their own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n'),
        ('    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72                                                         # Stage 7.2, pinned by its own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n',
         '    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
