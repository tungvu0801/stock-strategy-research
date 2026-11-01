"""
Stage 5.4.2 - automatic evidence refresh (DESIGN_542_EVIDENCE_REFRESH.md).

  P01-P04 the pure policy: states and dates by the READY rule's own age formula, the Stage 5.4.1 packet choice, back-off, order, budgets.
  S01-S08 one refresh run: sequential builds, duplicate prevention, leases, gates (mode, session, network), DRY_RUN, failure recovery,
          the real engine + an append-only store end to end, zero model calls and no broker, the thread never starting in tests.
  J01-J04 bounded retries: an attempt that raised before any packet was stored is journaled (durable, survives a restart) and backs off
          like a FAILED packet; consecutive errors or a refusing source open a durable circuit; errors spend the daily budget; an
          unreadable store waits; the journal never touches the evidence tables.
  C01-C04 a manual Build racing the refresh: the check and the insert are one write transaction; a packet stored while the refresh was
          fetching wins (nothing duplicated); a concurrent insert waits and is kept; an identical packet is never inserted twice; the
          guarded insert writes exactly the store's own rows; append-only.
  D01-D02 the production decision path on real stored DUE / STALE / MISSING / FRESH / backing-off packets, through the scheduler thread:
          DRY_RUN names exactly what ON then builds, the read-only preview agrees, ON appends only and converges run by run.
  O01-O02 the outside-session rule governs both ends of a build: no build starts in the session or within PRE_OPEN_MARGIN_S of the
          open; a build that finishes after the open is discarded inside the write transaction (never stored, not a failure).
  R01-R02 GET /api/evidence-refresh is read-only and builds nothing.
  E01-E02 the SEC companyfacts request alone gets EVIDENCE_SEC_FACTS_MAX_BYTES; a large file is used, a larger one is still TOO_LARGE.
  F01-F03 scope: the change set is exactly the declared files; READY thresholds, PPE scoring and the frozen hashes are unchanged; every
          bookkeeping edit is pinned exactly.
"""
from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import evidence_fakes as F
import freeze_ledger as FL
from evidence import engine as EN
from evidence import net
from evidence.store import EvidenceStore
from evidence_refresh import (A_BUILT, A_DISCARDED, A_ERROR, A_FAILED, A_SKIPPED_FRESH, A_SUPERSEDED, A_WOULD_BUILD, BACKOFF, BUILD, DUE,
                              FAILURE_CAP, FRESH, LEASE_NAME, MISSING, PRE_OPEN_MARGIN_S, R_CIRCUIT_OPEN, READ_ERROR, RETRY_WAIT, SESSION_OPEN, STALE,
                              SUPERSEDED_FRESH, SUPERSEDED_IDENTICAL, WAIT)
from evidence_refresh import journal as J
from evidence_refresh import policy as P
from evidence_refresh import scheduler as SCH
from evidence_refresh import service as SV
from evidence_refresh.service import EvidenceRefresher

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 31, 6, 8, 56, tzinfo=timezone.utc)           # the Phase C build time of the live packets
S = P.RefreshSettings()                                               # defaults: refresh at 7 days, stale after 30, retry 6 h, 2 failures / 24 h
PRODUCTION_JOURNAL_PATH = J.default_path                              # kept before the autouse fixture redirects it


@pytest.fixture(autouse=True)
def journal_file(tmp_path, monkeypatch):
    """Every test's default (durable) journal is its own file: nothing leaks between tests or into a real data directory."""
    path = tmp_path / J.JOURNAL_FILE
    monkeypatch.setattr(J, "default_path", lambda: path)
    return path


def row(sym, at, status="COMPLETED", pid=None, code=None):
    return {"ticker": sym, "packet_id": pid or f"{sym.lower()}-{at:%Y%m%d%H%M%S}-{status[0]}", "status": status,
            "created_at": at.isoformat(timespec="seconds"), "failure_code": code, "strength": "WEAK" if status == "COMPLETED" else None}


# ============================================================================================================ policy
def test_P01_states_and_dates_follow_the_ready_rules_own_age_formula():
    from setups import EVIDENCE_MAX_AGE_DAYS
    assert S.max_age_days == EVIDENCE_MAX_AGE_DAYS == 30 and SV.settings().max_age_days == EVIDENCE_MAX_AGE_DAYS
    assert P.symbol_state("NVDA", [], T0, S)["state"] == MISSING and P.symbol_state("NVDA", [], T0, S)["decision"] == BUILD
    r = [row("NVDA", T0)]
    fresh = P.symbol_state("NVDA", r, T0 + timedelta(days=6, hours=23), S)
    assert (fresh["state"], fresh["decision"], fresh["age_days"]) == (FRESH, WAIT, 6)
    assert fresh["due_at"] == (T0 + timedelta(days=7)).isoformat(timespec="seconds")
    assert fresh["stale_at"] == "2026-12-01T06:08:56+00:00"                       # the day READY_BLOCKED_EVIDENCE_STALE starts
    assert P.symbol_state("NVDA", r, T0 + timedelta(days=7), S)["state"] == DUE
    last_ok = P.symbol_state("NVDA", r, T0 + timedelta(days=30, hours=23, minutes=59), S)
    assert (last_ok["state"], last_ok["age_days"], last_ok["days_until_stale"]) == (DUE, 30, 0)
    stale = P.symbol_state("NVDA", r, T0 + timedelta(days=31), S)
    assert (stale["state"], stale["age_days"], stale["decision"]) == (STALE, 31, BUILD)
    # the setup service's own formula agrees: age 30 passes, age 31 is "older than 30 days"
    for days, blocked in ((30.99, False), (31, True)):
        now = T0 + timedelta(days=days)
        assert (max(0, (now - T0).days) > EVIDENCE_MAX_AGE_DAYS) is blocked
        assert (P.symbol_state("NVDA", r, now, S)["state"] == STALE) is blocked


def test_P02_the_judged_packet_is_the_stage_541_gate_packet():
    from trader_view.research import pick
    ok, newer_fail = row("AMD", T0, pid="a1"), row("AMD", T0 + timedelta(hours=2), "FAILED", pid="a2", code="NO_MARKET_DATA")
    st = P.symbol_state("AMD", [newer_fail, ok], T0 + timedelta(hours=3), S)
    assert st["packet_id"] == "a1" == pick([newer_fail, ok], T0 + timedelta(hours=3))["packet_id"]   # a newer FAILED never hides it
    assert st["last_failure"] == {"packet_id": "a2", "created_at": newer_fail["created_at"], "failure_code": "NO_MARKET_DATA"}
    future = row("AMD", T0 + timedelta(days=1), pid="a3")
    assert P.symbol_state("AMD", [future, ok], T0 + timedelta(hours=3), S)["packet_id"] == "a1"         # created after now: not evidence yet
    tie = [row("AMD", T0, pid="b1"), row("AMD", T0, pid="b2")]
    assert P.symbol_state("AMD", tie, T0, S)["packet_id"] == "b2" == pick(tie, T0)["packet_id"]          # ties to the higher packet id
    assert P.symbol_state("AMD", [row("AMD", T0, "FAILED")], T0 + timedelta(days=1), S)["state"] == MISSING
    assert P.symbol_state("AMD", [row("MU", T0)], T0, S)["state"] == MISSING                             # another symbol's packet never counts


def test_P03_a_failed_build_backs_off_and_two_failures_a_day_wait_for_the_window():
    due_now = T0 + timedelta(days=8)
    ok = row("MU", T0, pid="m0")
    f1 = row("MU", due_now - timedelta(hours=1), "FAILED", pid="m1")
    st = P.symbol_state("MU", [f1, ok], due_now, S)
    assert (st["state"], st["decision"], st["reason"]) == (DUE, BACKOFF, RETRY_WAIT)
    assert st["retry_at"] == (due_now + timedelta(hours=5)).isoformat(timespec="seconds")
    assert P.symbol_state("MU", [f1, ok], due_now + timedelta(hours=5), S)["decision"] == BUILD
    f2 = row("MU", due_now + timedelta(hours=6), "FAILED", pid="m2")
    capped = P.symbol_state("MU", [f2, f1, ok], due_now + timedelta(hours=12), S)
    assert (capped["decision"], capped["reason"], capped["failures_24h"]) == (BACKOFF, FAILURE_CAP, 2)
    assert capped["retry_at"] == (datetime.fromisoformat(f1["created_at"]) + timedelta(hours=24)).isoformat(timespec="seconds")
    assert P.symbol_state("MU", [f2, f1, ok], due_now + timedelta(hours=23, minutes=1), S)["decision"] == BUILD
    old_fail = row("MU", T0 - timedelta(hours=1), "FAILED")                                             # older than the valid packet: no back-off
    assert P.symbol_state("MU", [ok, old_fail], due_now, S)["decision"] == BUILD
    assert P.symbol_state("MU", [ok, f1], T0 + timedelta(days=2), S)["decision"] == WAIT                # FRESH: nothing to retry


def test_P04_plan_orders_by_urgency_and_respects_the_run_and_day_budgets():
    now = T0 + timedelta(days=40)
    rows = {"NVDA": [row("NVDA", now - timedelta(days=8))], "AMD": [row("AMD", now - timedelta(days=35))], "MU": [],
            "LLY": [row("LLY", now - timedelta(days=10))], "ORCL": [row("ORCL", now - timedelta(days=1))]}
    p = P.plan(["NVDA", "AMD", "MU", "LLY", "ORCL", "nvda"], rows, now, S)
    assert [i["symbol"] for i in p["items"]] == ["NVDA", "AMD", "MU", "LLY", "ORCL"]                   # de-duplicated, upper-cased, in order
    assert p["to_build"] == ["AMD", "MU", "LLY"] and p["wanted"] == 4 and p["budget"] == 3               # STALE, MISSING, then oldest DUE
    assert p["counts"] == {DUE: 2, STALE: 1, MISSING: 1, FRESH: 1} and p["builds_last_24h"] == 0
    assert p["soonest_stale_at"] == min(i["stale_at"] for i in p["items"] if i["stale_at"])
    busy = {k: v + [row(k, now - timedelta(hours=h), "FAILED", pid=f"{k}{h}") for h in (30,)] for k, v in rows.items()}
    busy["ORCL"] = busy["ORCL"] + [row("ORCL", now - timedelta(hours=h), pid=f"o{h}") for h in range(1, 13)]   # 12 in the trailing 24 h (the 1-day-old one sits on the edge, outside)
    p2 = P.plan(["NVDA", "AMD", "MU", "LLY", "ORCL"], busy, now, S)
    assert p2["builds_last_24h"] == 12 and p2["budget"] == 0 and p2["to_build"] == []
    one = P.plan(["NVDA", "AMD", "MU", "LLY"], rows, now, P.RefreshSettings(max_builds_per_run=1))
    assert one["to_build"] == ["AMD"]


# ============================================================================================================ one run
class FakeLease:
    def __init__(self, holder=None, lose_after=None):
        self.holder, self.lose_after, self.acquires, self.released = holder, lose_after, [], []

    def acquire(self, owner, now=None, ttl_min=2, name="x"):
        self.acquires.append((owner, name, ttl_min))
        if self.lose_after is not None and len(self.acquires) > self.lose_after:
            return False
        if self.holder not in (None, owner):
            return False
        self.holder = owner
        return True

    def release(self, owner, name="x"):
        self.released.append((owner, name))
        if self.holder == owner:
            self.holder = None


class World:
    """An in-memory packet store the fake build appends to, so the re-check sees what the run built."""

    def __init__(self, rows, clock_start):
        self.rows = {k: list(v) for k, v in rows.items()}
        self.now = clock_start
        self.builds, self.sleeps = [], []

    def clock(self):
        return self.now

    def rows_fn(self, s):
        return list(self.rows.get(s, []))

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += timedelta(seconds=s)
        return False

    def build(self, sym, t, status="COMPLETED", code=None, refused=()):
        self.builds.append(sym)
        r = row(sym, t, status, pid=f"new-{sym}-{len(self.builds)}", code=code)
        self.rows.setdefault(sym, []).insert(0, r)
        return {"status": status, "packet_id": r["packet_id"], "failure_code": code, "strength": r["strength"], "refused_hosts": list(refused)}


def refresher(world, *, mode="ON", symbols=("NVDA", "AMD", "MU"), build=None, lease=None, regular=lambda t: False, network=lambda: True,
              journal=None, **kw):
    return EvidenceRefresher(mode_fn=lambda: mode, settings_fn=lambda: S, symbols_fn=lambda: list(symbols), rows_fn=world.rows_fn,
                             build_fn=build or world.build, regular_session_fn=regular, network_fn=network,
                             lease_store_fn=(lambda: lease) if lease is not None else None, clock=world.clock, sleep_fn=world.sleep, spacing_s=20,
                             journal=journal if journal is not None else J.AttemptJournal.memory(), **kw)


def test_S01_a_run_builds_the_due_symbols_one_by_one_with_spacing_and_holds_the_lease():
    now = T0 + timedelta(days=8)
    w = World({"NVDA": [row("NVDA", T0)], "AMD": [row("AMD", now - timedelta(days=1))], "MU": []}, now)
    lease = FakeLease()
    r = refresher(w, lease=lease, owner="me")
    out = r.run_once()
    assert out["result"] == "COMPLETED" and out["stop_reason"] is None
    assert w.builds == ["MU", "NVDA"] and w.sleeps == [20.0]                          # MISSING first, then DUE; AMD is FRESH
    assert [a["outcome"] for a in out["attempts"]] == [A_BUILT, A_BUILT]
    assert lease.acquires[0] == ("me", LEASE_NAME, SV.LEASE_TTL_MIN) and len(lease.acquires) == 3   # once for the run, renewed before each build
    assert lease.released == [("me", LEASE_NAME)] and lease.holder is None
    assert r.totals[A_BUILT] == 2 and r.runs == 1 and r.status()["history"][0]["symbol"] == "NVDA"
    assert r.run_once()["result"] == "NOTHING_DUE" and w.builds == ["MU", "NVDA"]       # idempotent: the next run finds them FRESH


def test_S02_duplicates_are_prevented_by_the_recheck_and_the_run_lock():
    now = T0 + timedelta(days=8)
    w = World({"NVDA": [row("NVDA", T0 - timedelta(days=1))], "AMD": [row("AMD", T0)]}, now)   # NVDA is older: built first
    calls = []

    def build(sym, t):
        calls.append(sym)
        if sym == "NVDA":                                                              # meanwhile a person presses Build for AMD
            w.rows["AMD"].insert(0, row("AMD", t, pid="manual-amd"))
            assert r.run_once()["result"] == "RUN_IN_PROGRESS"                         # a second run while this one holds the lock
        return w.build(sym, t)

    r = refresher(w, symbols=("NVDA", "AMD"), build=build)
    out = r.run_once()
    assert calls == ["NVDA"] and [a["outcome"] for a in out["attempts"]] == [A_BUILT, A_SKIPPED_FRESH]
    assert out["attempts"][1]["packet_id"] == "manual-amd"


def test_S03_a_lease_held_elsewhere_builds_nothing_and_a_lost_lease_stops_the_run():
    now = T0 + timedelta(days=8)
    w = World({"NVDA": [row("NVDA", T0)], "AMD": [row("AMD", T0)]}, now)
    held = FakeLease(holder="other-process")
    assert refresher(w, symbols=("NVDA", "AMD"), lease=held).run_once()["result"] == "LEASE_HELD" and w.builds == [] and held.released == []
    lost = FakeLease(lose_after=2)                                                     # the run's own acquire + the first renewal succeed
    out = refresher(w, symbols=("NVDA", "AMD"), lease=lost).run_once()
    assert out["result"] == "STOPPED" and out["stop_reason"] == "LEASE_LOST" and w.builds == ["AMD"]     # equally due: alphabetical


def test_S04_mode_session_and_network_gates_and_dry_run():
    now = T0 + timedelta(days=8)
    w = World({"NVDA": [row("NVDA", T0)]}, now)
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("must not build"))          # noqa: E731
    assert refresher(w, mode="OFF", build=boom).run_once() == {"result": "OFF"}
    assert refresher(w, build=boom, regular=lambda t: True).run_once()["result"] == "SKIPPED_REGULAR_SESSION"
    assert refresher(w, build=boom, network=lambda: False).run_once()["result"] == "SKIPPED_NETWORK_OFF"
    dry = refresher(w, mode="DRY_RUN", build=boom, symbols=("NVDA",))
    out = dry.run_once()
    assert out == {"result": "DRY_RUN", "mode": "DRY_RUN", "would_build": ["NVDA"]} and dry.totals[A_WOULD_BUILD] == 1
    w2 = World({"NVDA": [row("NVDA", T0)], "AMD": [row("AMD", T0)]}, now)
    opens = refresher(w2, symbols=("NVDA", "AMD"), regular=lambda t: t > now)          # 09:30 ET arrives during the spacing wait
    res = opens.run_once()
    assert res["stop_reason"] == "SESSION_OPENED" and w2.builds == ["AMD"]
    from setups.market_hours import is_regular_session
    assert SV.default_regular_session(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)) is True == is_regular_session(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
    assert SV.default_regular_session(datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)) is False   # a Saturday


def test_S05_failures_back_off_errors_are_contained_and_a_refusing_source_stops_the_run():
    now = T0 + timedelta(days=8)
    w = World({s: [row(s, T0)] for s in ("NVDA", "AMD", "MU")}, now)
    out = refresher(w, build=lambda s, t: w.build(s, t, status="FAILED", code="NO_MARKET_DATA") if s == "NVDA" else w.build(s, t)).run_once()
    assert [(a["symbol"], a["outcome"]) for a in out["attempts"]] == [("AMD", A_BUILT), ("MU", A_BUILT), ("NVDA", A_FAILED)]
    w.now += timedelta(hours=1)
    nxt = refresher(w).plan()
    nv = next(i for i in nxt["items"] if i["symbol"] == "NVDA")
    assert (nv["decision"], nv["reason"], nv["packet_id"]) == (BACKOFF, RETRY_WAIT, w.rows["NVDA"][-1]["packet_id"])   # the old packet stays in use
    w3 = World({s: [row(s, T0)] for s in ("NVDA", "AMD", "MU")}, now)

    def raising(sym, t):
        raise RuntimeError("boom")
    out3 = refresher(w3, build=raising).run_once()
    assert out3["stop_reason"] == "CONSECUTIVE_ERRORS" and [a["outcome"] for a in out3["attempts"]] == [A_ERROR, A_ERROR]
    w4 = World({s: [row(s, T0)] for s in ("NVDA", "AMD", "MU")}, now)
    first_err = {"n": 0}

    def flaky(sym, t):
        first_err["n"] += 1
        if first_err["n"] == 1:
            raise RuntimeError("once")
        return w4.build(sym, t)
    assert [a["outcome"] for a in refresher(w4, build=flaky).run_once()["attempts"]] == [A_ERROR, A_BUILT, A_BUILT]
    w5 = World({s: [row(s, T0)] for s in ("NVDA", "AMD", "MU")}, now)
    out5 = refresher(w5, build=lambda s, t: w5.build(s, t, refused=("data.sec.gov",))).run_once()
    assert out5["stop_reason"] == "SOURCE_REFUSED" and w5.builds == ["AMD"]
    bad_store = refresher(World({}, now), symbols=("NVDA",))
    bad_store.rows_fn = lambda s: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    assert bad_store.plan()["read_errors"] == ["NVDA: OperationalError"]


def _cf_with_padding(tmp_path, pad: int) -> str:
    cf = json.loads((F.FIX / "companyfacts_SNDK.json").read_text(encoding="utf-8"))
    cf["_padding"] = "x" * pad
    p = tmp_path / f"cf_{pad}.json"
    p.write_text(json.dumps(cf), encoding="utf-8")
    return str(p)


@pytest.fixture
def live(monkeypatch):
    wire = F.FakeWire()
    monkeypatch.setattr(config, "EVIDENCE_NETWORK_ENABLED", True)
    monkeypatch.setattr(net, "WIRE", wire)
    monkeypatch.setattr(config, "SEC_USER_AGENT", F.SEC_UA)
    monkeypatch.setattr(config, "EVIDENCE_IR_DOMAINS", F.IR_DOMAINS)
    return wire


def test_S06_the_real_engine_appends_a_packet_and_never_touches_the_existing_one(live, tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    first = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, store=store)["packet"]
    assert first["status"] == "COMPLETED"
    before = {t: [tuple(r) for r in sqlite3.connect(store.path).execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("evidence_packets", "evidence_items")}

    def engine_build(sym, t):
        out = EN.build_packet(F.request(ticker=sym, as_of_date=t.date()), adapters=F.adapters(), now_fn=lambda: t, store=store)["packet"]   # as of the build day, as in production
        return {"status": out["status"], "packet_id": out["packet_id"], "failure_code": out["failure_code"], "strength": out["strength"], "refused_hosts": []}

    later = F.NOW + timedelta(days=8)
    r = EvidenceRefresher(mode_fn=lambda: "ON", settings_fn=lambda: S, symbols_fn=lambda: ["SNDK"], rows_fn=lambda s: store.packets(100, s),
                          build_fn=engine_build, regular_session_fn=lambda t: False, network_fn=lambda: True, lease_store_fn=None,
                          clock=lambda: later, sleep_fn=lambda s: False, spacing_s=0, journal=J.AttemptJournal.memory())
    assert r.plan()["to_build"] == ["SNDK"]
    out = r.run_once()
    assert [a["outcome"] for a in out["attempts"]] == [A_BUILT]
    rows = store.packets(100, "SNDK")
    assert len(rows) == 2 and {x["packet_id"] for x in rows} >= {first["packet_id"]}
    c = sqlite3.connect(store.path)
    for t, old in before.items():
        now_rows = {r_[0]: tuple(r_) for r_ in c.execute(f"SELECT * FROM {t}")}
        assert all(now_rows[o[0]] == o for o in old), t                                # every existing row byte-identical
    from trader_view.research import pick
    assert pick(rows, later)["packet_id"] == out["attempts"][0]["packet_id"] != first["packet_id"]
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE evidence_packets SET strength = 'STRONG' WHERE packet_id = ?", (first["packet_id"],))
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM evidence_packets WHERE packet_id = ?", (first["packet_id"],))
    assert r.plan(later)["items"][0]["state"] == FRESH
    c.close()


def test_S07_zero_model_calls_no_broker_and_only_evidence_hosts(live, tmp_path, monkeypatch):
    from evidence import explain as EX
    monkeypatch.setattr(EX, "explain", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model call")))
    tmp_store = EvidenceStore(tmp_path / "ev.db")
    captured = {}
    real_build = EN.build_packet

    def fake_build_packet(req, *, adapters=None, now_fn=None, store=None, persist=True):
        captured.update(ticker=req.ticker, adapters=adapters, store_type=type(store).__name__, sources=dict(req.sources), persist=persist)
        return real_build(req, adapters=F.adapters(), now_fn=F.now_fn, store=store, persist=persist)   # the production request itself; fixture data

    monkeypatch.setattr(EN, "build_packet", fake_build_packet)
    monkeypatch.setattr(SV, "_default_store", lambda: tmp_store)                       # a temp store instead of the app database
    monkeypatch.setattr(SV, "default_regular_session", lambda t: False)                # the wall clock may be inside the session when CI runs
    out = SV.default_build("SNDK", F.NOW)                                              # the production build function itself
    assert captured == {"ticker": "SNDK", "adapters": None, "store_type": "EvidenceStore", "persist": False,
                        "sources": {"internal": True, "sec": True, "ir": True, "news": True, "manual": True}}   # built in memory, then the guarded insert
    assert out["status"] == "COMPLETED" and out["model_requests"] == 0 and out["refused_hosts"] == []
    assert out["written"] is True and out["packet_id"] == out["built_packet_id"] and [r_["packet_id"] for r_ in tmp_store.packets(10, "SNDK")] == [out["packet_id"]]
    hosts = {net.host_of(c["url"]) for c in live.calls}
    assert hosts == {"www.sec.gov", "data.sec.gov"}                                  # the production request: SEC only (no IR URLs, no manual URLs configured)
    forbidden = ("evidence.explain", "ai_explain", "anthropic", "openai", "llm", "execution", "paper", "broker", "robinhood", "rh_gateway",
                 "unified_portfolio", "alpaca", "proposals", "safety", "autorun")
    for f in sorted((ROOT / "evidence_refresh").glob("*.py")) + [ROOT / "api/routes/evidence_refresh.py"]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module] + \
                [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        bad = [m for m in names if any(m == p or m.startswith(p + ".") or m.split(".")[0] == p for p in forbidden)]
        assert not bad, (f.name, bad)
        src = f.read_text(encoding="utf-8")
        assert "explain(" not in src and "submit_order" not in src and "place_order" not in src, f.name


def test_S08_the_thread_never_starts_in_tests_and_stops_promptly(monkeypatch):
    monkeypatch.setattr(config, "EVIDENCE_REFRESH_MODE", "ON")
    assert SCH.start_if_enabled() is False and SCH._SCHEDULER is None                   # pytest process: never
    monkeypatch.setattr(config, "EVIDENCE_REFRESH_MODE", "OFF")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("STOCK_AGENT_ENV", "")
    assert SCH.start_if_enabled() is False                                             # default OFF: never
    monkeypatch.setattr(config, "EVIDENCE_REFRESH_MODE", "SOMETHING")
    assert SV.mode() == "OFF"                                                          # unknown value: OFF
    assert config.EVIDENCE_REFRESH_AGE_DAYS == 7 and config.EVIDENCE_REFRESH_MAX_BUILDS_PER_RUN == 3 and config.EVIDENCE_SEC_FACTS_MAX_BYTES == 25_000_000
    monkeypatch.setattr(config, "EVIDENCE_REFRESH_AGE_DAYS", 0)
    assert SV.settings().refresh_age_days == 1                                         # never a same-day rebuild (packet_hash is UNIQUE)
    w = World({}, T0)
    runs = []
    r = refresher(w, mode="OFF")
    r.run_once = lambda now=None: runs.append(1) or {"result": "OFF"}
    sch = SCH.EvidenceRefreshScheduler(r, interval_s=3600, startup_delay_s=0)
    assert sch.start() is True and sch.start() is False
    deadline = time.time() + 5
    while not runs and time.time() < deadline:
        time.sleep(0.01)
    t0 = time.time()
    assert sch.stop() is True and time.time() - t0 < 5 and runs == [1]                # the hour-long wait is interrupted
    assert sch.status()["running"] is False


# ============================================================================================================ bounded retries (J)
TABLES_542 = ("evidence_packets", "evidence_items", "evidence_source_fetches")


def snapshot(store):
    c = sqlite3.connect(store.path)
    try:
        return {t: sorted((tuple(r_) for r_ in c.execute(f"SELECT * FROM {t}")), key=repr) for t in TABLES_542}
    finally:
        c.close()


def unchanged(store, before) -> bool:
    """Every row that existed is still there, byte-identical (rows may only have been added)."""
    now = snapshot(store)
    return all(set(before[t]) <= set(now[t]) for t in TABLES_542)


def raising(calls, exc=None):
    def build(sym, t):
        calls.append((sym, t))
        raise exc or RuntimeError("raised before any packet was stored")
    return build


def test_J01_an_attempt_that_raised_before_any_packet_was_stored_backs_off_durably_across_runs_and_restarts(journal_file):
    start = T0 + timedelta(days=8)                                                     # NVDA is DUE
    w = World({"NVDA": [row("NVDA", T0)]}, start)
    calls = []

    def run_at(t, journal_factory):
        w.now = t
        return refresher(w, symbols=("NVDA",), build=raising(calls), journal=journal_factory()).run_once()   # a new refresher each run: a restart

    first = run_at(start, J.AttemptJournal)
    assert [(a["outcome"], a["error"], a["journaled"]) for a in first["attempts"]] == [(A_ERROR, "RuntimeError", True)]
    saved = json.loads(journal_file.read_text(encoding="utf-8"))
    assert saved["version"] == J.JOURNAL_VERSION and [(e["symbol"], e["error"], e["stage"]) for e in saved["errors"]] == [("NVDA", "RuntimeError", "build")]
    nv = refresher(w, symbols=("NVDA",), journal=J.AttemptJournal()).plan(start + timedelta(minutes=15))["items"][0]
    assert (nv["state"], nv["decision"], nv["reason"], nv["errors_24h"], nv["last_failure"]["error"]) == (DUE, BACKOFF, RETRY_WAIT, 1, "RuntimeError")
    for k in range(1, 4 * 48):                                                         # every 15 minutes for 48 hours, a fresh process each time
        run_at(start + timedelta(minutes=15 * k), J.AttemptJournal)
    assert [round((t - start).total_seconds() / 3600, 2) for _, t in calls] == [0.0, 6.0, 24.0, 30.0]   # 6 h apart, 2 per trailing 24 h: 4, not 192
    assert w.rows["NVDA"] == [row("NVDA", T0)]                                        # nothing was stored; the old packet stays in use
    calls.clear()
    for k in range(8):                                                                 # the original behaviour (no durable record): every run tries again
        run_at(start + timedelta(minutes=15 * k), J.AttemptJournal.memory)
    assert len(calls) == 8


def test_J02_consecutive_errors_or_a_refusing_source_open_a_durable_circuit_and_errors_spend_the_daily_budget(journal_file):
    now = T0 + timedelta(days=8)
    w = World({s: [row(s, T0)] for s in ("NVDA", "AMD", "MU")}, now)
    calls = []
    out = refresher(w, build=raising(calls), journal=J.AttemptJournal()).run_once()
    assert out["stop_reason"] == "CONSECUTIVE_ERRORS" and [c[0] for c in calls] == ["AMD", "MU"]           # equally due: alphabetical
    opened = now + timedelta(seconds=20)                                                                    # after the one spacing wait
    assert out["circuit"] == {"until": (opened + timedelta(hours=6)).isoformat(timespec="seconds"), "reason": "CONSECUTIVE_ERRORS"}
    assert json.loads(journal_file.read_text(encoding="utf-8"))["circuit"]["reason"] == "CONSECUTIVE_ERRORS"
    for minutes in (15, 60, 6 * 60):                                                   # a restart each time: the circuit is read from the file
        w.now = now + timedelta(minutes=minutes)
        res = refresher(w, build=raising(calls), journal=J.AttemptJournal()).run_once()
        assert (res["result"], res["reason"]) == (R_CIRCUIT_OPEN, "CONSECUTIVE_ERRORS")
    assert [c[0] for c in calls] == ["AMD", "MU"]                                      # NVDA was never tried while the circuit was open
    w.now = now + timedelta(hours=6, minutes=15)                                       # closed, and AMD / MU waited out their retry
    res = refresher(w, journal=J.AttemptJournal()).run_once()
    assert [(a["symbol"], a["outcome"]) for a in res["attempts"]] == [("AMD", A_BUILT), ("MU", A_BUILT), ("NVDA", A_BUILT)]
    w5 = World({s: [row(s, T0)] for s in ("NVDA", "AMD", "MU")}, now)
    j5 = J.AttemptJournal.memory()
    out5 = refresher(w5, build=lambda s_, t: w5.build(s_, t, status="FAILED", code="NO_MARKET_DATA", refused=("data.sec.gov",)), journal=j5).run_once()
    assert out5["stop_reason"] == "SOURCE_REFUSED" and out5["circuit"]["reason"] == "SOURCE_REFUSED" and w5.builds == ["AMD"]
    w5.now += timedelta(minutes=15)
    assert refresher(w5, journal=j5).run_once()["result"] == R_CIRCUIT_OPEN and w5.builds == ["AMD"]          # SEC is not asked again for 6 h
    errs = {s_: [{"symbol": s_, "at": (now - timedelta(hours=h)).isoformat(), "error": "OperationalError"} for h in range(1, 7)] for s_ in ("AAA", "BBB")}
    p = P.plan(["AAA", "BBB", "CCC"], {}, now, S, errors_by_symbol=errs)
    assert (p["errors_last_24h"], p["builds_last_24h"], p["budget"], p["to_build"]) == (12, 0, 0, [])      # 12 attempts that stored nothing spend the day
    assert P.plan(["CCC"], {}, now, S, circuit={"until": (now + timedelta(minutes=1)).isoformat(), "reason": "X"})["to_build"] == []
    bad = refresher(World({}, now), symbols=("NVDA",))
    bad.rows_fn = lambda s_: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    bp = bad.plan()
    assert bp["read_errors"] == ["NVDA: OperationalError"] and (bp["items"][0]["decision"], bp["items"][0]["reason"]) == (WAIT, READ_ERROR)
    assert bp["to_build"] == []                                                        # an unreadable store is never read as MISSING (which would build)


def test_J03_with_the_real_engine_an_exception_after_the_fetches_or_during_the_insert_stores_nothing_and_is_journaled(live, tmp_path, monkeypatch, journal_file):
    store = EvidenceStore(tmp_path / "ev.db")
    EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, store=store)
    before = snapshot(store)
    later = F.NOW + timedelta(days=8)

    def make(journal):
        return EvidenceRefresher(mode_fn=lambda: "ON", settings_fn=lambda: S, symbols_fn=lambda: ["SNDK"], rows_fn=lambda s_: store.packets(100, s_),
                                 build_fn=lambda s_, t: SV.default_build(s_, t, adapters=F.adapters(), now_fn=lambda: later, store=store, settings_fn=lambda: S),
                                 regular_session_fn=lambda t: False, network_fn=lambda: True, lease_store_fn=None, clock=lambda: later,
                                 sleep_fn=lambda s_: False, spacing_s=0, journal=journal)

    with monkeypatch.context() as m:                                                   # the engine's post-processing raises after every fetch
        m.setattr(EN.R, "strength", lambda ladder: (_ for _ in ()).throw(KeyError("rung")))
        r = make(J.AttemptJournal())
        out = r.run_once()
        assert [(a["outcome"], a["error"], a["journaled"]) for a in out["attempts"]] == [(A_ERROR, "KeyError", True)]
        assert r.run_once()["result"] == "NOTHING_DUE" and len(live.calls) > 0     # the next run backs off (nothing fetched again)
        n_calls = len(live.calls)
        assert make(J.AttemptJournal()).run_once()["result"] == "NOTHING_DUE" and len(live.calls) == n_calls   # nor after a restart
    assert snapshot(store) == before                                                   # nothing stored, nothing changed
    with monkeypatch.context() as m:                                                   # the insert itself fails (a locked database)
        m.setattr(SV, "persist_unless_superseded", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        out2 = make(J.AttemptJournal.memory()).run_once()
        assert [(a["outcome"], a["error"]) for a in out2["attempts"]] == [(A_ERROR, "OperationalError")]
    assert snapshot(store) == before
    assert [e["error"] for e in json.loads(journal_file.read_text(encoding="utf-8"))["errors"]] == ["KeyError"]


def test_J04_the_journal_is_operational_state_reading_never_writes_and_a_bad_file_never_unbounds_a_process(tmp_path, journal_file):
    j = J.AttemptJournal()
    assert j.state(T0) == {"errors": [], "circuit": None} and not journal_file.exists()                  # reading never creates the file
    journal_file.write_text("{not json", encoding="utf-8")
    assert j.state(T0) == {"errors": [], "circuit": None} and j.read_error == "JSONDecodeError"            # unreadable: empty, reported
    assert j.record_error("NVDA", T0, "RuntimeError") is True                                              # the next write replaces it
    assert [e["symbol"] for e in json.loads(journal_file.read_text(encoding="utf-8"))["errors"]] == ["NVDA"]
    assert len(j.state(T0)["errors"]) == 1 and j.read_error is None
    blocked = tmp_path / "a_file"
    blocked.write_text("x", encoding="utf-8")
    jw = J.AttemptJournal(path_fn=lambda: blocked / "journal.json")                    # cannot be written (its parent is a file)
    assert jw.record_error("AMD", T0, "OperationalError") is False and jw.write_error
    st = P.symbol_state("AMD", [row("AMD", T0 - timedelta(days=9))], T0 + timedelta(minutes=15), S, jw.errors_by_symbol(T0)["AMD"])
    assert (st["decision"], st["reason"]) == (BACKOFF, RETRY_WAIT)                    # the in-memory copy still bounds this process
    j2 = J.AttemptJournal(path_fn=lambda: tmp_path / "j2.json")
    j2.record_error("MU", T0, "X")
    j2.record_error("MU", T0 + timedelta(hours=J.KEEP_HOURS + 1), "Y")
    assert [e["error"] for e in json.loads((tmp_path / "j2.json").read_text(encoding="utf-8"))["errors"]] == ["Y"]   # pruned after KEEP_HOURS
    src = (ROOT / "evidence_refresh/journal.py").read_text(encoding="utf-8")
    assert "sqlite3" not in src and "evidence_packets" not in src and "CREATE TABLE" not in src                    # never the evidence tables, no schema
    prod = PRODUCTION_JOURNAL_PATH()
    assert prod == Path(config.PROJECT_ROOT) / "runtime" / J.JOURNAL_FILE                                         # beside account_refresh.fault
    assert subprocess.run(["git", "check-ignore", "-q", "runtime/" + J.JOURNAL_FILE], cwd=ROOT).returncode == 0   # git-ignored: never committed


# ============================================================================================================ a manual Build racing the refresh (C)
def any_bars(symbols, lookback_days):
    base = F.fake_bars(["SNDK", "SPY", "SOXX"], lookback_days)
    return {s_: base.get(s_, base["SNDK"]) for s_ in symbols}


def test_C01_a_manual_build_stored_while_the_refresh_is_fetching_wins_and_nothing_is_duplicated(live, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    from api.routes import evidence as EVR
    from fit import readonly as RO
    from trader_view.research import pick
    db = tmp_path / "stock_agent.db"
    monkeypatch.setattr(RO, "db_path", lambda: db)
    store = EvidenceStore(db)
    old = EN.build_packet(EN.EvidenceRequest(ticker="SNDK"), adapters=F.adapters(), now_fn=lambda: F.NOW, store=store)["packet"]
    before = snapshot(store)
    later = F.NOW + timedelta(days=8)
    clock = {"t": later}
    monkeypatch.setattr(EVR, "NOW_FN", lambda: clock["t"])
    monkeypatch.setitem(EVR.BUILD, "adapters", F.adapters())
    fetching, release = threading.Event(), threading.Event()

    def slow_news(symbol, limit):                                                      # the refresh build is mid-fetch until released
        fetching.set()
        assert release.wait(20)
        return F.fake_news(symbol, limit)

    r = EvidenceRefresher(mode_fn=lambda: "ON", settings_fn=lambda: S, symbols_fn=lambda: ["SNDK"], rows_fn=lambda s_: store.packets(100, s_),
                          build_fn=lambda s_, t: SV.default_build(s_, t, adapters=F.adapters(news=slow_news), now_fn=lambda: clock["t"], store=store,
                                                                  settings_fn=lambda: S),
                          regular_session_fn=lambda t: False, network_fn=lambda: True, lease_store_fn=None, clock=lambda: clock["t"],
                          sleep_fn=lambda s_: False, spacing_s=0, journal=J.AttemptJournal.memory())
    result = {}
    th = threading.Thread(target=lambda: result.update(r.run_once()))
    th.start()
    assert fetching.wait(20)                                                           # past its re-check (SNDK was DUE), now fetching
    clock["t"] = later + timedelta(seconds=30)
    res = TestClient(app).post("/api/evidence/packets", json={"ticker": "SNDK"})       # a person presses Build: the real route
    assert res.status_code == 201
    manual_id = res.json()["packet_id"]
    clock["t"] = later + timedelta(seconds=60)
    release.set()
    th.join(20)
    assert [(a["outcome"], a["reason"], a["packet_id"], a["built_status"]) for a in result["attempts"]] == [(A_SUPERSEDED, SUPERSEDED_FRESH, manual_id, "COMPLETED")]
    rows = store.packets(100, "SNDK")
    assert sorted(x["packet_id"] for x in rows) == sorted([old["packet_id"], manual_id])           # one new packet: the person's
    assert unchanged(store, before) and pick(rows, clock["t"])["packet_id"] == manual_id
    assert r.plan(clock["t"])["items"][0]["state"] == FRESH and r.run_once()["result"] == "NOTHING_DUE"


def test_C02_the_check_and_the_insert_are_one_write_transaction_a_concurrent_insert_waits_and_both_packets_are_kept(live, tmp_path):
    db = tmp_path / "ev.db"
    entered, proceed = threading.Event(), threading.Event()

    class HeldStore(EvidenceStore):                                                    # pauses inside the refresh's write transaction
        @contextmanager
        def write(self):
            with EvidenceStore.write(self) as c:
                entered.set()
                assert proceed.wait(20)
                yield c

    base = EvidenceStore(db)
    old = EN.build_packet(EN.EvidenceRequest(ticker="SNDK"), adapters=F.adapters(), now_fn=lambda: F.NOW, store=base)["packet"]
    before = snapshot(base)
    later = F.NOW + timedelta(days=8)
    out = {}
    th = threading.Thread(target=lambda: out.update(SV.default_build("SNDK", later, adapters=F.adapters(), now_fn=lambda: later, store=HeldStore(db),
                                                                     settings_fn=lambda: S)))
    th.start()
    assert entered.wait(20)                                                            # the refresh holds BEGIN IMMEDIATE
    manual = EN.build_packet(EN.EvidenceRequest(ticker="SNDK"), adapters=F.adapters(news=lambda s_, n: F.fake_news(s_, n)[:2]),
                             now_fn=lambda: later + timedelta(seconds=5), persist=False)    # the Build button's packet (different news: its own hash)
    done = threading.Event()
    mt = threading.Thread(target=lambda: (base.insert_packet(manual["packet"], manual["items"], manual["fetches"]), done.set()))
    mt.start()
    assert not done.wait(0.5)                                                          # the manual insert waits for the refresh's transaction
    proceed.set()
    th.join(20)
    mt.join(20)
    assert done.is_set() and out["written"] is True
    assert {x["packet_id"] for x in base.packets(100, "SNDK")} == {old["packet_id"], out["packet_id"], manual["packet"]["packet_id"]}
    assert unchanged(base, before)                                                     # a person's Build is never refused; nothing lost or changed


def test_C03_an_identical_stored_packet_is_never_inserted_twice_and_the_attempt_is_bounded(live, tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    built = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    twin = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    assert twin["packet"]["packet_hash"] == built["packet"]["packet_hash"] and twin["packet"]["packet_id"] != built["packet"]["packet_id"]
    store.insert_packet(built["packet"], built["items"], built["fetches"])
    before = snapshot(store)
    later = F.NOW + timedelta(days=8)                                                  # the stored twin is DUE: only the hash check stops this one
    assert SV.persist_unless_superseded(store, twin, "SNDK", S, later) == {"written": False, "reason": SUPERSEDED_IDENTICAL,
                                                                            "existing_packet_id": built["packet"]["packet_id"]}
    assert snapshot(store) == before
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_packet(twin["packet"], twin["items"], twin["fetches"])           # what a plain insert would have done (rolled back)
    assert snapshot(store) == before
    w = World({"NVDA": [row("NVDA", T0)]}, T0 + timedelta(days=8))
    j = J.AttemptJournal.memory()
    same = {"status": "COMPLETED", "written": False, "superseded": SUPERSEDED_IDENTICAL, "existing_packet_id": "x", "refused_hosts": []}
    out = refresher(w, symbols=("NVDA",), build=lambda s_, t: same, journal=j).run_once()
    assert [(a["outcome"], a["reason"]) for a in out["attempts"]] == [(A_SUPERSEDED, SUPERSEDED_IDENTICAL)]
    w.now += timedelta(minutes=15)
    assert refresher(w, symbols=("NVDA",), journal=j).plan()["items"][0]["reason"] == RETRY_WAIT   # bounded like an error, not every 15 minutes


def test_C04_the_guarded_insert_writes_exactly_the_stores_own_rows_and_the_tables_stay_append_only(live, tmp_path):
    built = EN.build_packet(F.request(), adapters=F.adapters(), now_fn=F.now_fn, persist=False)
    a, b = EvidenceStore(tmp_path / "a.db"), EvidenceStore(tmp_path / "b.db")
    a.insert_packet(built["packet"], built["items"], built["fetches"])
    assert SV.persist_unless_superseded(b, built, "SNDK", S, F.NOW)["written"] is True
    assert snapshot(a) == snapshot(b) and len(snapshot(b)["evidence_items"]) == built["packet"]["n_items"] > 0
    c = sqlite3.connect(b.path)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE evidence_packets SET strength = 'STRONG' WHERE packet_id = ?", (built["packet"]["packet_id"],))
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM evidence_items WHERE packet_id = ?", (built["packet"]["packet_id"],))
    c.close()
    src = inspect.getsource(SV.persist_unless_superseded)
    assert "UPDATE " not in src and "DELETE " not in src and src.count("INSERT INTO") == 3 and "store.write()" in src


# ============================================================================================================ the production decision path (D)
D_NOW = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)                            # a Saturday, 03:00 New York: outside the session
D_WATCH = ["AAPL", "SNDK", "MU", "LRCX", "NVDA", "AMD"]


@pytest.fixture
def seeded(live, tmp_path, monkeypatch):
    """Real packets from the real engine at backdated times in a temp app database: FRESH, DUE (2), STALE, MISSING, and STALE + a FAILED
    build 2 hours ago. The watchlist, store, settings, mode, session calendar, lease store and network flag are the production defaults."""
    from fit import readonly as RO
    import scanner.watchlist as WL
    from forward.automation import AutomationStore
    db = tmp_path / "stock_agent.db"
    monkeypatch.setattr(RO, "db_path", lambda: db)
    monkeypatch.setattr(WL, "load_watchlist", lambda *a, **k: list(D_WATCH))
    store = EvidenceStore(db)
    ad = F.adapters(bars=any_bars)

    def seed(sym, at, ok=True):
        return EN.build_packet(EN.EvidenceRequest(ticker=sym), adapters=ad if ok else F.adapters(bars=lambda syms, lb: {}), now_fn=lambda: at,
                               store=store)["packet"]

    packets = {"AAPL": seed("AAPL", D_NOW - timedelta(days=2)), "SNDK": seed("SNDK", D_NOW - timedelta(days=10)), "MU": seed("MU", D_NOW - timedelta(days=8)),
               "LRCX": seed("LRCX", D_NOW - timedelta(days=35)), "AMD": seed("AMD", D_NOW - timedelta(days=40))}
    failed = seed("AMD", D_NOW - timedelta(hours=2), ok=False)
    assert all(p_["status"] == "COMPLETED" for p_ in packets.values()) and (failed["status"], failed["failure_code"]) == ("FAILED", "NO_MARKET_DATA")
    live.calls.clear()
    return SimpleNamespace(db=db, store=store, packets=packets, failed=failed, leases=AutomationStore(db), adapters=ad)


def _one_scheduler_run(r):
    sch = SCH.EvidenceRefreshScheduler(r, interval_s=3600, startup_delay_s=0)          # the real thread and loop
    assert sch.start() is True
    deadline = time.time() + 60
    while r.runs < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert sch.stop() is True and r.runs == 1                                          # one run, then the hour-long wait is interrupted
    return r.last_result


def test_D01_the_scheduler_dry_run_on_stored_due_stale_and_missing_packets_names_exactly_what_on_builds(seeded, live, monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    from api.routes import evidence_refresh as ER
    monkeypatch.setattr(config, "EVIDENCE_REFRESH_MODE", "DRY_RUN")
    before = snapshot(seeded.store)
    r = EvidenceRefresher(clock=lambda: D_NOW, lease_store_fn=lambda: seeded.leases, sleep_fn=lambda s_: False,
                          build_fn=lambda *a: (_ for _ in ()).throw(AssertionError("DRY_RUN must not build")))   # every other input: the production default
    assert _one_scheduler_run(r) == {"result": "DRY_RUN", "mode": "DRY_RUN", "would_build": ["LRCX", "NVDA", "SNDK"]}   # STALE, MISSING, oldest DUE
    plan = r.plan(D_NOW)
    assert {i["symbol"]: (i["state"], i["decision"], i["reason"]) for i in plan["items"]} == {
        "AAPL": (FRESH, WAIT, None), "SNDK": (DUE, BUILD, None), "MU": (DUE, BUILD, None), "LRCX": (STALE, BUILD, None), "NVDA": (MISSING, BUILD, None),
        "AMD": (STALE, BACKOFF, RETRY_WAIT)}                                           # AMD's FAILED build 2 h ago: retry at +4 h
    assert (plan["wanted"], plan["budget"], plan["builds_last_24h"], plan["errors_last_24h"]) == (4, 3, 1, 0)   # MU waits for the next run
    by = {i["symbol"]: i for i in plan["items"]}
    assert by["SNDK"]["packet_id"] == seeded.packets["SNDK"]["packet_id"] and by["AMD"]["packet_id"] == seeded.packets["AMD"]["packet_id"]
    assert by["AMD"]["retry_at"] == (D_NOW + timedelta(hours=4)).isoformat(timespec="seconds") and by["LRCX"]["age_days"] == 35
    assert [h["outcome"] for h in r.status()["history"]] == [A_WOULD_BUILD] * 3
    assert snapshot(seeded.store) == before and live.calls == []                       # nothing built, nothing fetched
    assert sqlite3.connect(seeded.db).execute("SELECT COUNT(*) FROM app_leases WHERE name = ?", (LEASE_NAME,)).fetchone()[0] == 0   # held, then released
    # the same decision path through the read-only route, without a running thread, at that instant and a week later
    monkeypatch.setattr(SCH, "_SCHEDULER", None)
    monkeypatch.setattr(ER, "_utcnow", lambda: D_NOW - timedelta(hours=1))
    client = TestClient(app)
    at_now = client.get("/api/evidence-refresh", params={"at": D_NOW.isoformat()}).json()
    assert at_now["preview_at"] == D_NOW.isoformat(timespec="seconds") and at_now["plan"]["to_build"] == ["LRCX", "NVDA", "SNDK"]
    week = client.get("/api/evidence-refresh", params={"at": (D_NOW + timedelta(days=7)).isoformat()}).json()["plan"]
    assert week["to_build"] == ["AMD", "LRCX", "NVDA"] and {i["symbol"]: i["state"] for i in week["items"]}["AAPL"] == DUE
    for bad in ("tomorrow", (D_NOW - timedelta(days=1)).isoformat(), (D_NOW + timedelta(days=ER.PREVIEW_MAX_DAYS + 1)).isoformat()):
        assert client.get("/api/evidence-refresh", params={"at": bad}).status_code == 422
    assert snapshot(seeded.store) == before and live.calls == [] and SCH._SCHEDULER is None


def test_D02_on_through_the_scheduler_builds_exactly_the_planned_packets_appends_only_and_converges(seeded, live, monkeypatch):
    from trader_view.research import pick
    monkeypatch.setattr(config, "EVIDENCE_REFRESH_MODE", "ON")
    before = snapshot(seeded.store)
    clock = {"t": D_NOW}

    def sleep(sec):
        clock["t"] += timedelta(seconds=sec)
        return False

    def make():                                                                        # production store, settings, watchlist, session, network
        return EvidenceRefresher(clock=lambda: clock["t"], lease_store_fn=lambda: seeded.leases, sleep_fn=sleep,
                                 build_fn=lambda s_, t: SV.default_build(s_, t, adapters=seeded.adapters, now_fn=lambda: clock["t"]))

    first = _one_scheduler_run(make())
    assert first["result"] == "COMPLETED" and [(a["symbol"], a["outcome"]) for a in first["attempts"]] == [("LRCX", A_BUILT), ("NVDA", A_BUILT), ("SNDK", A_BUILT)]
    assert clock["t"] == D_NOW + timedelta(seconds=2 * config.EVIDENCE_REFRESH_SPACING_SECONDS)   # sequential, spaced
    for a in first["attempts"]:
        rows = seeded.store.packets(100, a["symbol"])
        assert pick(rows, clock["t"])["packet_id"] == a["packet_id"]                   # the READY gate now reads the new packet
        assert P.symbol_state(a["symbol"], rows, clock["t"], SV.settings())["state"] == FRESH
    assert unchanged(seeded.store, before) and {net.host_of(c["url"]) for c in live.calls} <= {"www.sec.gov", "data.sec.gov"}
    clock["t"] = D_NOW + timedelta(hours=1)                                            # later runs: a fresh process each time (state is the store)
    assert [(a["symbol"], a["outcome"]) for a in make().run_once()["attempts"]] == [("MU", A_BUILT)]     # the budget left MU; AMD still backs off
    clock["t"] = D_NOW + timedelta(hours=4, minutes=1)
    assert [(a["symbol"], a["outcome"]) for a in make().run_once()["attempts"]] == [("AMD", A_BUILT)]    # its retry wait is over
    clock["t"] += timedelta(minutes=15)
    assert make().run_once()["result"] == "NOTHING_DUE"
    assert all(P.symbol_state(s_, seeded.store.packets(100, s_), clock["t"], SV.settings())["state"] == FRESH for s_ in D_WATCH)
    n = {t: len(v) for t, v in snapshot(seeded.store).items()}
    assert n["evidence_packets"] == len(before["evidence_packets"]) + 5 and unchanged(seeded.store, before)   # five appended, none changed


# ============================================================================================================ the session rule (O)
OPEN_UTC = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)                         # Monday 09:30 New York (EST)


def test_O01_no_build_starts_in_the_session_or_within_the_pre_open_margin():
    from evidence import LIMITS
    assert PRE_OPEN_MARGIN_S == LIMITS["build_s"] + 60 == 180
    blocked = SV.default_session_blocked
    assert blocked(OPEN_UTC - timedelta(seconds=180)) is False and blocked(OPEN_UTC - timedelta(seconds=179)) is True
    assert blocked(OPEN_UTC) is True and blocked(OPEN_UTC + timedelta(hours=6, minutes=29)) is True        # 15:59 New York
    assert blocked(OPEN_UTC + timedelta(hours=6, minutes=30)) is False                                    # 16:00: closed, next open tomorrow
    assert blocked(datetime(2026, 10, 31, 14, 28, tzinfo=timezone.utc)) is False                          # Saturday
    assert blocked(datetime(2026, 11, 1, 14, 28, tzinfo=timezone.utc)) is False                           # Sunday: the open is a day away
    assert EvidenceRefresher().regular_session_fn is SV.default_session_blocked                            # the production start gate
    w = World({"NVDA": [row("NVDA", T0)]}, OPEN_UTC - timedelta(seconds=150))
    assert refresher(w, symbols=("NVDA",), regular=blocked).run_once()["result"] == "SKIPPED_REGULAR_SESSION" and w.builds == []
    w2 = World({"NVDA": [row("NVDA", T0 - timedelta(days=8))], "AMD": [row("AMD", T0 - timedelta(days=8))]}, OPEN_UTC - timedelta(seconds=190))
    out = refresher(w2, symbols=("NVDA", "AMD"), regular=blocked).run_once()                               # 190 s before: one build, then
    assert out["stop_reason"] == "SESSION_OPENED" and w2.builds == ["AMD"]                                 # 20 s spacing reaches the margin


def test_O02_a_build_that_finishes_after_the_open_is_discarded_inside_the_write_transaction_and_rebuilt_after_the_close(live, tmp_path, journal_file):
    store = EvidenceStore(tmp_path / "ev.db")
    EN.build_packet(EN.EvidenceRequest(ticker="SNDK"), adapters=F.adapters(), now_fn=lambda: F.NOW, store=store)
    before = snapshot(store)
    clock = {"t": OPEN_UTC - timedelta(seconds=200)}

    def slow_news(symbol, limit):                                                       # this build overruns the margin: it ends at 09:30:05
        clock["t"] = OPEN_UTC + timedelta(seconds=5)
        return F.fake_news(symbol, limit)

    def make(news=None):
        return EvidenceRefresher(mode_fn=lambda: "ON", settings_fn=lambda: S, symbols_fn=lambda: ["SNDK"], rows_fn=lambda s_: store.packets(100, s_),
                                 build_fn=lambda s_, t: SV.default_build(s_, t, adapters=F.adapters(news=news or F.fake_news), now_fn=lambda: clock["t"],
                                                                         store=store, settings_fn=lambda: S),       # the production session check at the insert
                                 regular_session_fn=SV.default_session_blocked, network_fn=lambda: True, lease_store_fn=None,
                                 clock=lambda: clock["t"], sleep_fn=lambda s_: False, spacing_s=0, journal=J.AttemptJournal())

    out = make(slow_news).run_once()                                                    # started 200 s before the open: allowed
    assert out["stop_reason"] == "SESSION_OPENED" and [(a["outcome"], a["reason"], a["built_status"]) for a in out["attempts"]] == [
        (A_DISCARDED, SESSION_OPEN, "COMPLETED")]
    assert snapshot(store) == before                                                    # nothing stored during the session
    assert not journal_file.exists()                                                    # not a failure: no back-off, no budget spent
    clock["t"] = OPEN_UTC + timedelta(minutes=30)
    assert make().run_once()["result"] == "SKIPPED_REGULAR_SESSION"
    clock["t"] = OPEN_UTC + timedelta(hours=6, minutes=35)                              # 16:05 New York
    late = make().run_once()
    assert [a["outcome"] for a in late["attempts"]] == [A_BUILT] and len(snapshot(store)["evidence_packets"]) == len(before["evidence_packets"]) + 1
    built = EN.build_packet(EN.EvidenceRequest(ticker="SNDK"), adapters=F.adapters(), now_fn=lambda: F.NOW + timedelta(days=1), persist=False)
    in_session = OPEN_UTC + timedelta(hours=1)
    assert SV.persist_unless_superseded(store, built, "SNDK", S, in_session) == {"written": False, "reason": SESSION_OPEN, "existing_packet_id": None}
    assert SV.persist_unless_superseded(store, built, "SNDK", S, in_session, in_session=lambda t: True)["reason"] == SESSION_OPEN
    src = inspect.getsource(SV.persist_unless_superseded)
    assert src.index("session_open(now)") < src.index("INSERT INTO") and src.index("with store.write() as c:") < src.index("session_open(now)")


# ============================================================================================================ route
def test_R01_get_evidence_refresh_reports_the_plan_and_builds_nothing(monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    from api.routes import evidence_refresh as ER
    now = datetime.now(timezone.utc)
    w = World({"NVDA": [row("NVDA", now - timedelta(days=9))], "AMD": []}, now)
    r = refresher(w, symbols=("NVDA", "AMD"), build=lambda *a: (_ for _ in ()).throw(AssertionError("GET must not build")))
    r.clock = lambda: now
    monkeypatch.setattr(SCH, "_SCHEDULER", SCH.EvidenceRefreshScheduler(r, interval_s=900, startup_delay_s=120))
    res = TestClient(app).get("/api/evidence-refresh")
    assert res.status_code == 200
    d = res.json()
    assert set(d) == {"version", "read_only", "mode", "checked_at", "preview_at", "scheduler", "plan", "model_requests", "broker_calls", "disclaimer"}
    assert d["read_only"] is True and d["model_requests"] == 0 and d["broker_calls"] == 0 and d["mode"] == "OFF" and d["preview_at"] is None
    assert [i["symbol"] for i in d["plan"]["items"]] == ["NVDA", "AMD"] and d["plan"]["to_build"] == ["AMD", "NVDA"]
    assert d["scheduler"]["running"] is False and d["scheduler"]["lease"] == LEASE_NAME and w.builds == []
    assert TestClient(app).post("/api/evidence-refresh").status_code == 405
    src = (ROOT / "api/routes/evidence_refresh.py").read_text(encoding="utf-8")
    assert src.count("@router.get(") == 1 and "@router.post" not in src and "@router.put" not in src and "@router.delete" not in src
    assert {(rt.path, m) for rt in ER.router.routes for m in rt.methods} == {("/api/evidence-refresh", "GET")}
    assert set(app.openapi()["paths"]["/api/evidence-refresh"]) == {"get"}


def test_R02_without_a_running_thread_the_route_still_answers_from_the_store_and_builds_nothing(monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    monkeypatch.setattr(SCH, "_SCHEDULER", None)
    monkeypatch.setattr(EN, "build_packet", lambda *a, **k: (_ for _ in ()).throw(AssertionError("GET must not build")))
    res = TestClient(app).get("/api/evidence-refresh")
    assert res.status_code == 200 and res.json()["scheduler"] == {"running": False, "version": "evidence_refresh_v1", "mode": "OFF", "lease": LEASE_NAME}
    plan = res.json()["plan"]
    assert [i["symbol"] for i in plan["items"]] == SV.default_symbols() and plan["read_errors"] == []   # the real watchlist, the real store
    assert SCH._SCHEDULER is None                                                      # reading never starts the thread


def test_R03_the_default_symbols_are_the_validated_watchlist(monkeypatch):
    import scanner.watchlist as WL
    monkeypatch.setattr(WL, "load_watchlist", lambda *a, **k: ["nvda", "AMD", "bad symbol", "NVDA", "BRK.B", "123"])
    assert SV.default_symbols() == ["NVDA", "AMD", "BRK.B"]


# ============================================================================================================ SEC companyfacts
def test_E01_only_the_companyfacts_request_gets_the_larger_cap(live):
    res = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90)
    caps = {c["url"]: c["max_bytes"] for c in live.calls}
    cf = [u for u in caps if "/api/xbrl/companyfacts/" in u]
    assert cf and all(caps[u] == config.EVIDENCE_SEC_FACTS_MAX_BYTES == 25_000_000 for u in cf)
    assert all(v == config.EVIDENCE_MAX_BYTES == 2_000_000 for u, v in caps.items() if u not in cf)
    assert res.facts.get("fundamentals") is not None


def test_E02_a_large_companyfacts_file_is_used_and_one_above_the_cap_is_still_too_large(live, tmp_path, monkeypatch):
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json"
    live.urls[url] = _cf_with_padding(tmp_path, 2_500_000)                             # larger than EVIDENCE_MAX_BYTES (2 MB)
    big = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90)
    assert big.facts.get("fundamentals") is not None
    assert [f.status for f in big.fetches if "companyfacts" in f.url] == ["OK"]
    monkeypatch.setattr(config, "EVIDENCE_SEC_FACTS_MAX_BYTES", 1_000_000)
    small = F.adapters()["sec"].run("SNDK", {"suppliers": [], "customers": []}, F.AS_OF, 90)
    assert [f.status for f in small.fetches if "companyfacts" in f.url] == ["TOO_LARGE"] and small.facts.get("fundamentals") is None


# ============================================================================================================ scope (F)
CONFIG_542 = ["EVIDENCE_REFRESH_MODE", "EVIDENCE_REFRESH_AGE_DAYS", "EVIDENCE_REFRESH_CHECK_SECONDS", "EVIDENCE_REFRESH_STARTUP_DELAY_SECONDS",
              "EVIDENCE_REFRESH_MAX_BUILDS_PER_RUN", "EVIDENCE_REFRESH_MAX_BUILDS_PER_DAY", "EVIDENCE_REFRESH_SPACING_SECONDS", "EVIDENCE_REFRESH_RETRY_HOURS",
              "EVIDENCE_REFRESH_MAX_FAILURES_PER_DAY", "EVIDENCE_SEC_FACTS_MAX_BYTES"]
SEC_EDITS_542 = [
    ('    def _get(self, res: AdapterResult, url: str, purpose: str) -> Optional[net.FetchResult]:\n',
     '    def _get(self, res: AdapterResult, url: str, purpose: str, max_bytes: Optional[int] = None) -> Optional[net.FetchResult]:\n'),
    ('        fr = self.fetch_fn(url, purpose=purpose, allowed_hosts=HOSTS, headers={"User-Agent": self.user_agent}, now_fn=self.now_fn)\n',
     '        cap = {} if max_bytes is None else {"max_bytes": int(max_bytes)}      # Evidence refresh, Stage 5.4.2: a per-endpoint cap (companyfacts only)\n'
     '        fr = self.fetch_fn(url, purpose=purpose, allowed_hosts=HOSTS, headers={"User-Agent": self.user_agent}, now_fn=self.now_fn, **cap)\n'),
    ('        fr = self._get(res, companyfacts_url(cik10), f"{ticker} company facts (XBRL)")\n',
     '        fr = self._get(res, companyfacts_url(cik10), f"{ticker} company facts (XBRL)",\n'
     '                       max_bytes=config.EVIDENCE_SEC_FACTS_MAX_BYTES)   # Evidence refresh, Stage 5.4.2: large caps\' companyfacts exceed EVIDENCE_MAX_BYTES\n'),
]


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_542}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_542} (fetch-depth 0 is required)"   # an empty file (__init__.py) is a valid answer
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def post(rel):
    """The Stage 7.0 base (PR #40's head, Stage 5.4.2 complete): what Stage 5.4.2 left, whatever a later stage changed since."""
    run = subprocess.run(["git", "show", f"{FL.BASE_70}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_70} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _strip_blocks(text: str) -> str:
    import re
    return re.sub(r"^[ \t]*# >>> Evidence refresh, Stage 5\.4\.2[^\n]*\n.*?^[ \t]*# <<< Evidence refresh, Stage 5\.4\.2\n", "", text, flags=re.S | re.M)


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_trading_file_changed():
    assert FL.BASE_542 == "ccee3d31f3a291f178b667f46317d58ce3267bec" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_542}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_542).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    stage = set(_git("diff", "--name-only", FL.BASE_542, FL.BASE_70).split())               # Stage 5.4.2 itself, between two fixed commits
    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}
    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71                                                         # Stage 7.0, pinned by its own F tests
    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),
                                                           "missing": sorted((FL.NEW_542 | FL.EDITED_542 | later) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_542).split())
    assert {p for p in stage if p not in in_base} == FL.NEW_542 and {p for p in stage if p in in_base} == FL.EDITED_542
    assert set(FL.SEMANTIC_542) == {"evidence/sources/sec_edgar.py"} and FL.INSTRUMENTATION_542 == {}
    for d in ("setups", "ppe", "evidence", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe", "unified_portfolio", "trader_view",
              "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests"):
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel and rel != "evidence/sources/sec_edgar.py":
                assert post(rel) == at_base(rel), rel                                   # no trading, safety, startup, UI or harness file changed
    cfg_base, cfg_now = at_base("config.py"), post("config.py")
    assert cfg_now.startswith(cfg_base) and cfg_now.count("# >>> Evidence refresh, Stage 5.4.2") == 1
    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_542   # only the new settings, appended
    srv = post("api/server.py")
    assert srv.count("# >>> Evidence refresh, Stage 5.4.2") == 3 and _strip_blocks(srv) == at_base("api/server.py")   # three marked blocks, nothing else
    sec = at_base("evidence/sources/sec_edgar.py")
    for old, new in SEC_EDITS_542:
        assert sec.count(old) == 1, old[:60]
        sec = sec.replace(old, new)
    assert sec == post("evidence/sources/sec_edgar.py")                               # the SEC cap: three exact edits
    for rel in sorted(FL.NEW_542 | FL.EDITED_542):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in post(rel) and m54 not in post(rel), rel                     # no marker another freeze test scans for


def test_F02_ready_thresholds_ppe_scoring_and_the_frozen_hashes_are_unchanged():
    import sys
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import setups as SU
    import ppe as PP
    from evidence import LIMITS
    assert (SU.WATCH_ENTER_STRETCH, SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.READY_DEMAND_COVERAGE, SU.READY_PPE_COVERAGE, SU.EVIDENCE_MAX_AGE_DAYS) == (-40.0, 40, 60, 0.60, 0.60, 30)
    assert (PP.NORMAL_BAND, PP.CONFIDENCE_BASE, PP.CAPS["WEAK"], PP.CAPS["MODERATE"]) == (40, 50, 50, 84)
    assert config.EVIDENCE_MAX_BYTES == 2_000_000 and LIMITS["sec_requests"] == 12 and LIMITS["build_s"] == 120 and config.EVIDENCE_REFRESH_MODE == "OFF"


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    import ast
    for rel, edits in REPLAY_542.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_542) == FL.EDITED_542 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    para = fl[fl.index("\nStage 5.4.2 (automatic evidence refresh"):fl.index('"""', fl.index("\nStage 5.4.2 (automatic evidence refresh"))]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_OPS + NEW_610D + NEW_542 plus this ledger.", " + NEW_OPS + NEW_610D plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_542 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_542: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the declarations, the paragraph, one line and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 5.4.2 left
    assert set(new_led) - set(old) == {"evidence/sources/sec_edgar.py"}                   # one new key: the SEC adapter (read as on main by the earlier freeze tests)
    assert {k for k in old if old[k] != new_led[k]} == FL.EDITED_542 & set(old)           # every re-hash is a declared Stage 5.4.2 edit
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_542 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
        ('    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")\n    cfg_base, cfg_now = at_base("config.py"), now_text("config.py")\n    assert cfg_now.startswith(cfg_base.rstrip("\\n"))\n',
         '    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")\n    cfg_base, cfg_now = at_base("config.py"), post("config.py")\n    assert cfg_now.startswith(cfg_base.rstrip("\\n"))\n'),
        ('    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_610C\n    srv_base, srv_now = at_base("api/server.py").splitlines(), now_text("api/server.py").splitlines()\n    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610C\n',
         '    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_610C\n    srv_base, srv_now = at_base("api/server.py").splitlines(), post("api/server.py").splitlines()\n    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610C\n'),
        ('    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610C\n    life = now_text("api/server.py")\n    life = life[life.index("async def _lifespan"):life.index("app = FastAPI(")]\n',
         '    assert [l for l in srv_base if l not in srv_now] == [] and [l for l in srv_now if l not in srv_base] == SERVER_LINES_610C\n    life = post("api/server.py")\n    life = life[life.index("async def _lifespan"):life.index("app = FastAPI(")]\n'),
        ('    before = {p for p in at_base("api/server.py").splitlines() if "include_router" in p}\n    after = {p for p in now_text("api/server.py").splitlines() if "include_router" in p}\n    assert len(after - before) == 1 and "decision_flow.router" in next(iter(after - before)) and before <= after\n',
         '    before = {p for p in at_base("api/server.py").splitlines() if "include_router" in p}\n    after = {p for p in post("api/server.py").splitlines() if "include_router" in p}\n    assert len(after - before) == 1 and "decision_flow.router" in next(iter(after - before)) and before <= after\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ("\n# every Stage 6.10D edit of a file no ledger entry can hold (an earlier stage's own test file): (text on main, text now), replayed by F03\n",
         '\ndef post(rel):\n    """main after PR #38 (Stage 6.10D merged): what Stage 6.10D left, whatever a later stage changed since."""\n    out = subprocess.run(["git", "show", f"{FL.BASE_542}:{rel}"], cwd=ROOT, capture_output=True).stdout\n    assert out, f"{rel} is not in {FL.BASE_542} (fetch-depth 0 is required)"\n    return out.decode("utf-8").replace("\\r\\n", "\\n")\n\n\n# every Stage 6.10D edit of a file no ledger entry can hold (an earlier stage\'s own test file): (text on main, text now), replayed by F03\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - changed)}\n    assert FL.SEMANTIC_610D == {} and FL.INSTRUMENTATION_610D == {}                                      # observability only\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_610D, FL.BASE_542).split())             # Stage 6.10D itself, between two fixed commits (main before / after PR #38)\n    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n                                                             "missing": sorted((FL.NEW_610D | FL.EDITED_610D | later) - changed)}\n    assert FL.SEMANTIC_610D == {} and FL.INSTRUMENTATION_610D == {}                                      # observability only\n'),
        ('    for rel in ("api/server.py", "config.py", "frontend/index.html", "frontend/product_ui.css", "frontend/trader_view.js", "browser_tests/flows.py", "README.md"):\n        assert (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel), rel\n    src = (ROOT / "api/routes/ops_status.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n")\n',
         '    for rel in ("api/server.py", "config.py", "frontend/index.html", "frontend/product_ui.css", "frontend/trader_view.js", "browser_tests/flows.py", "README.md"):\n        assert post(rel) == at_base(rel), rel                                               # what Stage 6.10D left (later edits: their own F tests)\n    src = (ROOT / "api/routes/ops_status.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n")\n'),
        ('    for rel, edits in REPLAY_610D.items():\n        text = (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n")\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_610D.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_610D) == FL.EDITED_610D - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry\n    fl, base = (ROOT / "tests/freeze_ledger.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n"), at_base("tests/freeze_ledger.py")\n    para = fl[fl.index("\\nStage 6.10D (engine health"):fl.index(\'"""\', fl.index("\\nStage 6.10D (engine health"))]\n',
         '    assert set(REPLAY_610D) == FL.EDITED_610D - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    para = fl[fl.index("\\nStage 6.10D (engine health"):fl.index(\'"""\', fl.index("\\nStage 6.10D (engine health"))]\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) - set(old) == {"api/routes/ops_status.py"}                       # one new key: the extended ops router\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_610D & set(old)         # every re-hash is a declared Stage 6.10D edit\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 6.10D left\n    assert set(new_led) - set(old) == {"api/routes/ops_status.py"}                         # one new key: the extended ops router\n    assert {k for k in old if old[k] != new_led[k]} == FL.EDITED_610D & set(old)           # every re-hash is a declared Stage 6.10D edit\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
