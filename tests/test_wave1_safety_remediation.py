"""
Wave 1 safety remediation (VALIDATION_REMEDIATION_PLAN.md, Wave 1; independent validation audit of main 4c01519, findings EX-08 / EX-07 / EX-08b,
EX-14 and EP-13), extended after independent review rounds 1 and 2. The S / P / E tests run the REAL code path. On main 4c01519 all but four
fail (the before / after logs list each one and why); S09, P03, P05 and E04 are regression guards that pass on main and must keep passing.
  S  the safety state: an Emergency Stop, a Pause or the day's loss halt is never lost to a concurrent evaluation or action, in the account-read
     window (S01-S05) or the read-to-commit window only the compare-and-swap protects (S10-S16); an unreadable state row is UNKNOWN, never "no
     kill switch" (S06, S17); the cached view never hides a latch written elsewhere (S07); a stop or pause is saved even when its evaluation fails
  P  the stop-loss check fails closed: a missing, invalid, undated or stale price never reports "no exit condition" (the real _price path, no
     price_fn, except where noted); a stale but valid price may still show a breach; new entries stop while a stop check cannot run
  E  test processes, and the processes they start, load no .env of their own checkout and hold no credential; a test never puts a credential
     value into an assertion; an explicit synthetic env file still loads
  F  the change set, the frozen hashes and the bookkeeping, pinned like every earlier stage
"""
import ast
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import freeze_ledger as FL  # noqa: E402
from execution import broker as BK  # noqa: E402
from execution.broker import BrokerError  # noqa: E402
from safety.models import AccountEquity  # noqa: E402
from safety.service import ExecutionSafetyService  # noqa: E402
from safety.store import SafetyStore  # noqa: E402
from trader_view.models import Candle, Quote  # noqa: E402
from test_execution_60 import ctl, cyc, entered, go_ready, make_stack  # noqa: E402

UTC = timezone.utc
CREDENTIAL_SETTINGS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ROBINHOOD_USERNAME", "ROBINHOOD_PASSWORD", "ANTHROPIC_API_KEY", "FRED_API_KEY", "SEC_USER_AGENT")


@pytest.fixture(autouse=True)
def no_execution_wire(monkeypatch):
    """As in tests/test_execution_60.py: the execution layer's real network adapter is a tripwire; only the simulated broker is reached."""
    hits = []

    def tripwire():
        hits.append("wire")
        raise AssertionError("the real Alpaca paper network adapter was requested by the execution layer during a test")
    monkeypatch.setattr(BK, "WIRE", tripwire)
    yield
    assert not hits


@pytest.fixture(autouse=True)
def execution_config(monkeypatch):
    """The Stage 6.0 test configuration (tests/test_execution_60.py::execution_config), so its stack helpers behave as there."""
    for k, v in (("TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0), ("MAX_DAILY_LOSS_PERCENT", 5.0), ("TRADE_PROPOSAL_HALT", False), ("TRADE_PROPOSAL_DAILY_LOSS_HALT", False),
                 ("EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL", False), ("EXECUTION_SAFETY_STARTUP_POLICY", "KEEP"), ("MAX_OPEN_POSITIONS", 10), ("EXECUTION_MAX_SHARES_PER_ENTRY", 0)):
        monkeypatch.setattr(config, k, v)


# ================================================================ S: the safety state ==================================================================

class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)                                   # Monday 10:00 ET

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)
        return self.now


def account(equity="100000.00"):
    return AccountEquity(equity=equity, source="LOCAL_PAPER", age_s=0.0, mode="simulated", adjustments="NONE_POSSIBLE")


def safety_service(db, clock, equity=None, during=None):
    """A real ExecutionSafetyService on a real SQLite store. `during` (a list) holds callables run INSIDE the next account read: the window
    between this evaluation's read of the state and its write, where the audit found the lost update."""
    eq = equity if equity is not None else ["100000.00"]

    def account_fn(ts):
        if during:
            during.pop(0)()
        return account(eq[0])
    return ExecutionSafetyService(store=SafetyStore(db), account_fn=account_fn, clock=clock, threshold_pct=3, startup_policy="KEEP", heartbeat_s=0)


def flags(store):
    st = store.state()
    return st.kill_active, st.pause_active, st.daily_halt_active


def after_read(store, name, action):
    """Runs `action` once, right AFTER this store's first `name()` read returns: inside the evaluation's read-to-commit window, which only
    the compare-and-swap commit protects (the account is read before the state). Another service on the same database is the other writer."""
    real, fired = getattr(store, name), []

    def hooked(*a, **k):
        out = real(*a, **k)
        if not fired:
            fired.append(action())
        return out
    setattr(store, name, hooked)
    return fired


def failing_reads(monkeypatch, marker="sqlite_master"):
    """Every read-only connection fails on SQL containing `marker` (writes still work): an unreadable state row. Returns the undo."""
    real_connect = sqlite3.connect

    class Failing:
        def __init__(self, conn):
            object.__setattr__(self, "_c", conn)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __setattr__(self, name, value):
            setattr(self._c, name, value)

        def execute(self, sql, *a):
            if marker in sql:
                raise sqlite3.OperationalError("disk I/O error (test)")
            return self._c.execute(sql, *a)

    def connect(target, *a, **k):
        conn = real_connect(target, *a, **k)
        return Failing(conn) if "mode=ro" in str(target) else conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    return lambda: monkeypatch.setattr(sqlite3, "connect", real_connect)


def test_S01_an_emergency_stop_survives_a_concurrent_background_evaluation(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    during = []
    bg, ui = safety_service(db, clock, during=during), safety_service(db, clock)
    assert bg.evaluate("SCHEDULED").state == "TRADING_ALLOWED"
    clock.advance()
    during.append(lambda: ui.emergency_stop("operator", actor="user"))                       # the click lands while the background evaluation runs
    v = bg.evaluate("SCHEDULED")
    assert flags(bg.store) == (True, False, False)                                           # main 4c01519: (False, ...) - the stop was overwritten
    assert v.state == "HALTED" and not v.allowed_new_entry                                   # the evaluation that raced the stop reports it
    assert bg.store.event_count("KILL_ON") == 1 and bg.evaluate("API").state == "HALTED"


def test_S02_an_emergency_stop_survives_real_threads_at_the_same_window(tmp_path):
    lost = 0
    for trial in range(15):
        clock, db = Clock(), tmp_path / f"t{trial}.db"
        inside, release = threading.Event(), threading.Event()

        def slow_account(ts):
            inside.set()
            release.wait(5)                                                                 # holds the evaluation between its read and its write
            return account()
        bg = ExecutionSafetyService(store=SafetyStore(db), account_fn=slow_account, clock=clock, threshold_pct=3, startup_policy="KEEP", heartbeat_s=0)
        ui = safety_service(db, clock)
        ui.evaluate("SCHEDULED")
        t = threading.Thread(target=lambda: bg.evaluate("SCHEDULED"))
        t.start()
        assert inside.wait(5)
        ui.emergency_stop("operator", actor="user")                                          # a real second thread commits the stop meanwhile
        release.set()
        t.join(10)
        assert not t.is_alive()
        lost += 0 if bg.store.state().kill_active else 1
    assert lost == 0, f"the Emergency Stop was lost in {lost} of 15 trials"


def test_S03_a_resume_racing_an_emergency_stop_never_clears_the_stop(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    during = []
    a, b = safety_service(db, clock, during=during), safety_service(db, clock)
    a.evaluate("SCHEDULED")
    a.pause("p", actor="user")
    clock.advance()
    during.append(lambda: b.emergency_stop("operator", actor="user"))
    v = a.resume("r", actor="user")                                                          # Resume clears the pause ONLY
    assert flags(a.store) == (True, False, False)                                            # main 4c01519: the stop is cleared with the pause
    assert v.state == "HALTED" and {e["kind"] for e in a.store.events(10)} >= {"PAUSE_ON", "PAUSE_OFF", "KILL_ON"}


def test_S04_clearing_the_stop_racing_a_pause_keeps_the_pause(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    during = []
    a, b = safety_service(db, clock, during=during), safety_service(db, clock)
    a.evaluate("SCHEDULED")
    a.emergency_stop("x", actor="user")
    clock.advance()
    during.append(lambda: b.pause("p", actor="user"))
    v = a.clear_emergency_stop("ok", actor="user")
    assert flags(a.store) == (False, True, False)                                            # main 4c01519: (False, False, ...) - the pause was dropped
    assert v.state == "NO_NEW_ENTRIES"


def test_S05_the_days_loss_halt_survives_a_stale_evaluation_with_recovered_equity(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    during, eq_b = [], ["100000.00"]
    a = safety_service(db, clock)
    b = safety_service(db, clock, equity=eq_b, during=during)
    a.evaluate("SCHEDULED")                                                                  # the day's baseline: 100,000
    clock.advance()
    a_eq = ["96000.00"]
    a2 = safety_service(db, clock, equity=a_eq)
    during.append(lambda: a2.evaluate("SCHEDULED"))                                          # -4 %: the halt latches for the day while b runs
    b.evaluate("SCHEDULED")                                                                  # b read the state before the latch, equity recovered
    assert flags(b.store)[2] is True                                                         # main 4c01519: the latch is overwritten (False)
    assert b.evaluate("API").state == "HALTED"                                               # "equity may recover, the halt does not"


@pytest.mark.parametrize("trigger", ["API", "SCHEDULED"])
def test_S06_an_unreadable_state_row_is_unknown_and_never_clears_the_stop(tmp_path, monkeypatch, trigger):
    clock, db = Clock(), tmp_path / "s.db"
    svc = safety_service(db, clock, equity=[None])                                           # no usable equity: no baseline row exists to collide with
    svc.emergency_stop("x", actor="user")
    assert svc.store.state().kill_active is True
    restore = failing_reads(monkeypatch)
    clock.advance()
    v = svc.evaluate(trigger)
    restore()
    assert v.state in ("UNKNOWN", "HALTED") and not v.allowed_new_entry                      # never "no kill switch" (main 4c01519: NO_NEW_ENTRIES, kill off)
    assert v.state == "UNKNOWN" or trigger != "API"                                          # a read-only evaluation cannot know the state: UNKNOWN
    assert svc.store.state().kill_active is True                                             # main 4c01519: kill_active False was persisted


@pytest.mark.parametrize("latch, expected", [("emergency_stop", "HALTED"), ("pause", "NO_NEW_ENTRIES")])
def test_S07_the_cached_view_never_hides_a_stop_or_pause_written_by_another_process(tmp_path, latch, expected):
    clock, db = Clock(), tmp_path / "s.db"
    a, other = safety_service(db, clock), safety_service(db, clock)
    a.evaluate("SCHEDULED")
    assert a.current().state == "TRADING_ALLOWED"                                            # cached for up to 5 s
    getattr(other, latch)("from another process", actor="user")
    assert a.current().state == expected                                                     # main 4c01519: the cached TRADING_ALLOWED view


def test_S08_contention_that_never_settles_fails_closed(tmp_path, monkeypatch):
    from safety import store as SS
    clock, db = Clock(), tmp_path / "s.db"
    svc = safety_service(db, clock)
    svc.evaluate("SCHEDULED")
    calls = []

    def always_changed(self, now, **kw):
        calls.append(1)
        raise SS.SafetyStateChanged("changed (test)")
    monkeypatch.setattr(SafetyStore, "commit", always_changed)
    clock.advance()
    v = svc.evaluate("SCHEDULED")
    assert v.state == "UNKNOWN" and not v.allowed_new_entry and 1 < len(calls) <= 5           # bounded re-reads, then UNKNOWN


def test_S09_sequential_behaviour_is_unchanged(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    svc = safety_service(db, clock)
    assert svc.evaluate("SCHEDULED").state == "TRADING_ALLOWED"
    n = svc.store.evaluation_count()
    svc.evaluate("API")
    assert svc.store.evaluation_count() == n                                                 # a plain read that changes nothing writes nothing
    clock.advance()
    assert svc.emergency_stop("x", actor="user").state == "HALTED"
    clock.advance()
    assert svc.resume("r", actor="user").state == "HALTED" and svc.store.state().kill_active  # Resume never clears the stop
    clock.advance()
    assert svc.pause("p", actor="user").state == "HALTED"                                    # kill > pause
    clock.advance()
    assert svc.clear_emergency_stop("ok", actor="user").state == "NO_NEW_ENTRIES"
    clock.advance()
    assert svc.resume("r", actor="user").state == "TRADING_ALLOWED"
    assert [e["kind"] for e in reversed(svc.store.events(20)) if e["kind"] in ("KILL_ON", "KILL_OFF", "PAUSE_ON", "PAUSE_OFF")] == \
        ["KILL_ON", "PAUSE_ON", "KILL_OFF", "PAUSE_OFF"]                                     # the no-op Resume under the stop recorded nothing
    clock.advance()
    assert svc.emergency_stop("x", actor="user").state == "HALTED" and svc.emergency_stop("again", actor="user").state == "HALTED"
    assert svc.store.event_count("KILL_ON") == 2                                             # already active: no second event


# S10-S21 (independent review rounds 1 and 2, H1 / M1 / N2): the read-to-commit window, which only the compare-and-swap commit protects
# (S01-S05 inject their write in the account read, which Wave 1 moved before the state read); the strict read at the store; a stop or pause
# saved when its evaluation fails, and nothing else. S10-S16 fail on main 4c01519 and without the compare-and-swap. S18 and S20 keep main's
# "the stop is saved" and fail on main only because main reported the unreadable state as a normal view; S19 fails on main because main
# applied a blind Resume / Clear, S21 because main rewrote an active stop.

def test_S10_a_stop_committed_inside_an_evaluations_read_to_commit_window_is_kept(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    bg, ui = safety_service(db, clock), safety_service(db, clock)
    bg.evaluate("SCHEDULED")
    clock.advance()
    fired = after_read(bg.store, "state", lambda: ui.emergency_stop("operator", actor="user").state)
    v = bg.evaluate("SCHEDULED")
    assert fired == ["HALTED"] and flags(bg.store) == (True, False, False)                    # without the compare-and-swap: (False, ...)
    assert v.state == "HALTED" and bg.store.event_count("KILL_ON") == 1


def test_S11_a_resume_whose_window_sees_a_stop_never_clears_it(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    a, b = safety_service(db, clock), safety_service(db, clock)
    a.evaluate("SCHEDULED")
    a.pause("p", actor="user")
    clock.advance()
    after_read(a.store, "state", lambda: b.emergency_stop("operator", actor="user"))
    v = a.resume("r", actor="user")
    assert flags(a.store) == (True, False, False) and v.state == "HALTED"


def test_S12_a_clear_whose_window_sees_a_pause_keeps_the_pause(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    a, b = safety_service(db, clock), safety_service(db, clock)
    a.evaluate("SCHEDULED")
    a.emergency_stop("x", actor="user")
    clock.advance()
    after_read(a.store, "state", lambda: b.pause("p", actor="user"))
    v = a.clear_emergency_stop("ok", actor="user")
    assert flags(a.store) == (False, True, False) and v.state == "NO_NEW_ENTRIES"            # a compare of the kill column alone would drop the pause


def test_S13_the_days_loss_halt_latched_inside_the_window_is_kept(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    a = safety_service(db, clock)
    a.evaluate("SCHEDULED")                                                                  # the day's baseline: 100,000
    clock.advance()
    loser = safety_service(db, clock, equity=["96000.00"])
    after_read(a.store, "state", lambda: loser.evaluate("SCHEDULED").state)                  # -4 %: the halt latches inside a's window
    a.evaluate("SCHEDULED")                                                                  # a's equity has recovered
    assert flags(a.store)[2] is True and a.evaluate("API").state == "HALTED"


def test_S14_a_baseline_created_inside_the_window_is_decided_again_not_a_failure(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    a, b = safety_service(db, clock), safety_service(db, clock)
    fired = after_read(a.store, "baseline", lambda: b.evaluate("SCHEDULED").state)            # the day's first evaluation elsewhere creates the baseline
    v = a.evaluate("SCHEDULED")
    assert fired == ["TRADING_ALLOWED"] and (v.state, v.persisted, v.error) == ("TRADING_ALLOWED", True, None)   # without the baseline check: UNKNOWN
    assert a.store.baseline(v.daily_loss.trading_date) is not None


def test_S15_a_stop_from_another_process_inside_the_window_is_kept(tmp_path):
    clock, db = Clock(), tmp_path / "s.db"
    bg = safety_service(db, clock)
    bg.evaluate("SCHEDULED")
    clock.advance()
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from datetime import datetime, timezone;"
            "from safety.service import ExecutionSafetyService; from safety.store import SafetyStore; from safety.models import AccountEquity;"
            "acct = lambda ts: AccountEquity(equity='100000.00', source='LOCAL_PAPER', age_s=0.0, mode='simulated', adjustments='NONE_POSSIBLE');"
            "now = datetime(2026, 11, 2, 15, 0, 1, tzinfo=timezone.utc);"
            "svc = ExecutionSafetyService(store=SafetyStore(sys.argv[2]), account_fn=acct, clock=lambda: now, threshold_pct=3, startup_policy='KEEP', heartbeat_s=0);"
            "print(svc.emergency_stop('other process', actor='user').state)")
    fired = after_read(bg.store, "state", lambda: subprocess.run([sys.executable, "-B", "-c", code, str(ROOT), str(db)], cwd=str(tmp_path),
                                                                 capture_output=True, text=True, timeout=120))
    v = bg.evaluate("SCHEDULED")
    assert fired[0].returncode == 0 and fired[0].stdout.strip() == "HALTED", fired[0].stderr[-800:]
    assert flags(bg.store) == (True, False, False) and v.state == "HALTED"


def test_S16_real_threads_lose_no_stop_and_report_no_unknown(tmp_path):
    lost, unknown, evaluations = 0, 0, 0
    for trial in range(60):
        clock, db = Clock(), tmp_path / f"r{trial}.db"
        bg, ui = safety_service(db, clock), safety_service(db, clock)
        bg.evaluate("SCHEDULED")
        states = []

        def loop():
            for _ in range(15):
                states.append(bg.evaluate("SCHEDULED").state)
        t = threading.Thread(target=loop)
        t.start()
        time.sleep((trial % 7) * 0.008)                                                      # the stop lands at a different point of the loop each trial
        uv = ui.emergency_stop("operator", actor="user")
        t.join(60)
        assert not t.is_alive()
        evaluations += len(states)
        lost += 0 if bg.store.state().kill_active else 1
        unknown += states.count("UNKNOWN") + (uv.state == "UNKNOWN")
    assert (lost, unknown) == (0, 0), f"{lost} stop(s) lost and {unknown} UNKNOWN in 60 trials / {evaluations} evaluations"   # without the CAS: about half lost


def test_S17_the_store_reads_the_latch_row_strictly(tmp_path, monkeypatch):
    from safety.models import SafetyStateRecord
    from safety.store import SafetyStoreError
    assert SafetyStore(tmp_path / "absent.db").state() == SafetyStateRecord()                 # a first run: no database yet
    sqlite3.connect(str(tmp_path / "empty.db")).close()
    assert SafetyStore(tmp_path / "empty.db").state() == SafetyStateRecord()                 # ... or no table yet
    clock, db = Clock(), tmp_path / "s.db"
    safety_service(db, clock).emergency_stop("x", actor="user")
    for marker in ("sqlite_master", "FROM execution_safety_state"):                          # the table check, then the row itself
        restore = failing_reads(monkeypatch, marker)
        with pytest.raises(SafetyStoreError):
            SafetyStore(db).state()                                                          # main 4c01519: the default record, kill off
        restore()
    assert SafetyStore(db).state().kill_active is True


@pytest.mark.parametrize("latch, flag, kind", [("emergency_stop", 0, "KILL_ON"), ("pause", 1, "PAUSE_ON")])
def test_S18_a_stop_or_pause_clicked_while_the_row_is_unreadable_is_still_saved(tmp_path, monkeypatch, latch, flag, kind):
    clock, db = Clock(), tmp_path / "s.db"
    svc = safety_service(db, clock, equity=[None])
    svc.evaluate("SCHEDULED")
    restore = failing_reads(monkeypatch)
    clock.advance()
    v = getattr(svc, latch)("operator", actor="user")
    restore()
    assert v.state == "UNKNOWN" and not v.allowed_new_entry                                  # the evaluation itself could not read the state
    assert flags(svc.store)[flag] is True and svc.store.event_count(kind) == 1               # review M1: saved anyway, with its event (as on main)


def test_S19_the_saved_latch_changes_nothing_else_and_a_blind_resume_or_clear_is_never_applied(tmp_path, monkeypatch):
    clock, db = Clock(), tmp_path / "s.db"
    svc = safety_service(db, clock, equity=[None])
    svc.pause("p", actor="user")
    restore = failing_reads(monkeypatch)
    clock.advance()
    svc.emergency_stop("x", actor="user")                                                    # saved on its own: the pause is kept
    clock.advance()
    svc.resume("r", actor="user")
    svc.clear_emergency_stop("ok", actor="user")                                             # neither is applied without a successful read
    restore()
    assert flags(svc.store) == (True, True, False)
    assert (svc.store.event_count("KILL_ON"), svc.store.event_count("PAUSE_OFF"), svc.store.event_count("KILL_OFF")) == (1, 0, 0)


def test_S20_halt_on_start_and_a_stop_under_endless_contention_are_still_saved(tmp_path, monkeypatch):
    from safety import store as SS
    clock, db = Clock(), tmp_path / "s.db"
    safety_service(db, clock, equity=[None]).evaluate("SCHEDULED")
    restore = failing_reads(monkeypatch)
    starting = ExecutionSafetyService(store=SafetyStore(db), account_fn=lambda ts: account(None), clock=clock, threshold_pct=3, startup_policy="HALT_ON_START",
                                      heartbeat_s=0)
    assert starting.startup().state == "UNKNOWN"
    restore()
    assert flags(starting.store)[0] is True                                                  # HALT_ON_START halted although the row was unreadable
    clock, db2 = Clock(), tmp_path / "s2.db"
    svc = safety_service(db2, clock)
    svc.evaluate("SCHEDULED")
    real_commit = SafetyStore.commit
    monkeypatch.setattr(SafetyStore, "commit", lambda self, now, **kw: (_ for _ in ()).throw(SS.SafetyStateChanged("changed (test)")))
    clock.advance()
    assert svc.emergency_stop("operator", actor="user").state == "UNKNOWN"
    monkeypatch.setattr(SafetyStore, "commit", real_commit)
    assert flags(svc.store)[0] is True and svc.store.event_count("KILL_ON") == 1             # the retries ran out; the stop was saved on its own


def test_S21_the_saved_latch_never_rewrites_an_active_latch_and_a_blind_resume_or_clear_sets_nothing(tmp_path, monkeypatch):
    clock, db = Clock(), tmp_path / "s.db"
    svc = safety_service(db, clock, equity=[None])
    svc.emergency_stop("first", actor="user")
    first = svc.store.state()
    restore = failing_reads(monkeypatch)
    clock.advance(60)
    svc.emergency_stop("second", actor="user")                                               # already on: the fallback changes nothing
    restore()
    again = svc.store.state()
    assert (again.kill_activated_at, again.kill_reason, svc.store.event_count("KILL_ON")) == (first.kill_activated_at, "first", 1)   # review round 2, N2
    clock2, db2 = Clock(), tmp_path / "s2.db"
    off = safety_service(db2, clock2, equity=[None])
    off.evaluate("SCHEDULED")                                                                # both latches off
    restore = failing_reads(monkeypatch)
    clock2.advance()
    off.clear_emergency_stop("ok", actor="user")
    off.resume("r", actor="user")
    restore()
    assert flags(off.store) == (False, False, False)                                         # a blind Clear or Resume never sets a latch ...
    assert [off.store.event_count(k) for k in ("KILL_ON", "KILL_OFF", "PAUSE_ON", "PAUSE_OFF")] == [0, 0, 0, 0]   # ... nor records one


# ================================================================ P: the stop check fails closed ========================================================

class FakeRealMarket:
    """A non-simulated market data source (the live feed's mode) whose quotes and minute bars a test sets; nothing is fetched."""
    mode = "real"

    def __init__(self):
        self.quotes, self.bars = {}, {}

    def quote(self, symbol):
        return self.quotes.get(symbol) or Quote(symbol=symbol, status="UNAVAILABLE", code="FEED_ERROR", source="test", message="feed down (test)")

    def candles(self, symbol, limit=None):
        return list(self.bars.get(symbol, []))


class PriceView:
    """The executor's stream with only its market data replaced: every other attribute (setups, proposals, safety, ...) is the real stream's."""
    def __init__(self, real, market):
        self._real, self.market = real, market

    def __getattr__(self, name):
        return getattr(self._real, name)

    def candles(self, symbol, limit=None):
        return self.market.candles(symbol, limit)


def real_feed(st):
    market = FakeRealMarket()
    st.x.price_fn = None
    st.x.stream_fn = lambda: PriceView(st.holder["s"], market)
    return market


def epoch(dt):
    return int(dt.timestamp())


def decisions(result):
    return [(d["symbol"], d["decision"]) for d in (result.get("summary") or {}).get("decisions", [])]


def exit_intents(st):
    return [i for i in st.x.store.intents() if i.intent_type == "EXIT"]


def stop_blind(st, result, symbol="SNDK"):
    """The fail-closed outcome: the control is UNKNOWN (never PASS), the decision is ATTENTION, the bot shows attention, nothing is sold."""
    c = ctl(st, "EXIT_TRIGGER")
    status = st.x.status(st.clock.now)
    return (c["last_result"] == "UNKNOWN" and "stop check could not run" in c["last_reason"] and (symbol, "ATTENTION") in decisions(result)
            and status.attention and any("stop check" in r for r in status.attention_reasons) and not exit_intents(st))


def test_P01_no_price_at_all_is_not_no_exit_condition(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    real_feed(st)                                                                            # quote UNAVAILABLE, no minute bars
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))                        # main 4c01519: PASS "no exit condition", HOLD


def test_P02_a_stale_quote_above_the_stop_cannot_confirm_the_stop_was_not_hit(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    above = float(Decimal(pos.invalidation_price) + Decimal("5"))
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=above, ts=epoch(datetime(2023, 3, 1, 15, 0, tzinfo=UTC)), source="test")
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))                        # main 4c01519: a 2023 trade read as the current price


def test_P03_a_stale_minute_bar_below_the_stop_still_exits(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    below = float(Decimal(pos.invalidation_price) - Decimal("1"))
    t = epoch(st.clock.now - timedelta(hours=3))
    market.bars["SNDK"] = [Candle(time=t, open=below, high=below, low=below, close=below, final=True)]
    cyc(st)
    assert [i.exit_reason for i in exit_intents(st)] == ["HARD_INVALIDATION"]                # a stale price can only ADD an exit (as on main)


@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf")])
def test_P04_an_invalid_price_is_never_a_breach_and_never_a_pass(tmp_path, bad):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=bad, ts=epoch(st.clock.now), source="test")
    r = cyc(st)
    assert r["result"] == "RUN" and stop_blind(st, r), (r.get("error"), ctl(st, "EXIT_TRIGGER"), decisions(r))   # main: 0 / -5 sold, NaN crashed the cycle


def test_P05_a_fresh_valid_price_behaves_as_before(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    inv = Decimal(pos.invalidation_price)
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=float(inv + Decimal("5")), ts=epoch(st.clock.now) - 20, source="test")
    r = cyc(st)
    assert ctl(st, "EXIT_TRIGGER")["last_result"] == "PASS" and ("SNDK", "HOLD") in decisions(r) and not exit_intents(st)
    assert not st.x.status(st.clock.now).attention
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=float(inv - Decimal("1")), ts=epoch(st.clock.now) + 30, source="test")
    cyc(st)
    assert [i.exit_reason for i in exit_intents(st)] == ["HARD_INVALIDATION"]


def test_P06_a_stale_price_while_the_market_is_closed_is_recorded_not_escalated(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    real_feed(st)
    monkeypatch.setattr(st.x.broker, "clock", lambda: {"timestamp": None, "is_open": False, "next_open": None, "next_close": None})
    r = cyc(st)
    c = ctl(st, "EXIT_TRIGGER")
    assert c["last_result"] == "WARN" and "market closed" in c["last_reason"] and ("SNDK", "HOLD") in decisions(r)
    assert not st.x.status(st.clock.now).attention and not exit_intents(st)


def test_P07_an_unreadable_broker_clock_is_treated_as_open(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    real_feed(st)

    def unreadable():
        raise BrokerError("UNREADABLE", 200)
    monkeypatch.setattr(st.x.broker, "clock", unreadable)
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))


def test_P08_new_entries_stop_while_a_stop_check_cannot_run(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)                                                                    # Paper Auto armed, SNDK held
    go_ready(st, "MU")                                                                       # a second READY setup with a reviewable proposal
    st.x.price_fn = lambda sym: (None, "UNAVAILABLE", None) if sym == "SNDK" else (Decimal("1"), "TEST", st.clock.now.isoformat())
    r = cyc(st)
    entries = (r.get("summary") or {}).get("entries") or {}
    assert not [i for i in st.x.store.intents() if i.intent_type == "ENTRY" and i.symbol == "MU"], entries   # main 4c01519: MU entered
    assert entries.get("blocked", {}).get("MU") == ["EXIT_TRIGGER"]


def test_P09_a_price_stamped_in_the_future_is_stale(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    above = float(Decimal(pos.invalidation_price) + Decimal("5"))
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=above, ts=epoch(st.clock.now + timedelta(hours=1)), source="test")
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))


def test_P10_a_missing_price_never_overwrites_the_last_known_price(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    inv = Decimal(pos.invalidation_price)
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=float(inv + Decimal("5")), ts=epoch(st.clock.now), source="test")
    cyc(st)
    seen = st.x.store.open_position("SNDK")
    assert seen.last_price is not None and seen.last_price_at is not None
    market.quotes.clear()
    cyc(st)
    after = st.x.store.open_position("SNDK")
    assert (after.last_price, after.last_price_at) == (seen.last_price, seen.last_price_at)   # main 4c01519: last_price None


def test_P11_several_blind_positions_read_the_broker_clock_once_per_cycle(tmp_path, monkeypatch):
    st = make_stack(tmp_path)
    entered(st)
    entered(st, "MU")                                                                        # two held positions
    real_feed(st)                                                                            # neither has a price
    reads, clock = [], st.x.broker.clock
    monkeypatch.setattr(st.x.broker, "clock", lambda: reads.append(1) or clock())
    st.clock.advance(30)
    st.x._evaluate_positions(st.clock.now, "TEST")
    assert sorted(st.x.stop_check_problems) == ["MU", "SNDK"] and len(reads) == 1             # the broker read budget is not spent per position


# P12-P18 (independent review round 1, L1 / L2 / L7)

def test_P12_an_undated_price_cannot_confirm_the_stop_was_not_hit(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    above = float(Decimal(pos.invalidation_price) + Decimal("5"))
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=above, ts=0, source="test")   # a real value with no time
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))                        # main 4c01519: PASS "no exit condition"


def test_P13_the_entry_block_clears_by_itself_when_a_fresh_price_returns(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    go_ready(st, "MU")
    blind = {"on": True}
    above = Decimal(pos.invalidation_price) + Decimal("5")
    st.x.price_fn = lambda sym: (None, "UNAVAILABLE", None) if (sym == "SNDK" and blind["on"]) else (above, "TEST", st.clock.now.isoformat())
    r1 = cyc(st)
    assert ((r1.get("summary") or {}).get("entries") or {}).get("blocked", {}).get("MU") == ["EXIT_TRIGGER"]
    blind["on"] = False
    r2 = cyc(st)
    assert not st.x.stop_check_problems and not any("stop check" in r for r in st.x.status(st.clock.now).attention_reasons)
    assert [i for i in st.x.store.intents() if i.intent_type == "ENTRY" and i.symbol == "MU"], (r2.get("summary") or {}).get("entries")


def test_P14_a_quote_has_the_300_s_limit_and_a_minute_bar_the_900_s_limit(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    market = real_feed(st)
    above = float(Decimal(pos.invalidation_price) + Decimal("5"))
    ten_min_old = lambda: epoch(st.clock.now + timedelta(seconds=30)) - 600                  # noqa: E731 - 600 s old when the next cycle runs
    market.quotes["SNDK"] = Quote(symbol="SNDK", status="OK", price=above, ts=ten_min_old(), source="test")
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))                        # a 10-minute-old quote is stale
    market.quotes.clear()
    market.bars["SNDK"] = [Candle(time=ten_min_old(), open=above, high=above, low=above, close=above, final=True)]
    r = cyc(st)
    assert ctl(st, "EXIT_TRIGGER")["last_result"] == "PASS" and ("SNDK", "HOLD") in decisions(r)   # a 10-minute-old minute close is not
    market.bars["SNDK"] = [Candle(time=ten_min_old() - 400, open=above, high=above, low=above, close=above, final=True)]
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))                        # ... and a 1000 s old one is


def test_P15_a_non_finite_price_is_invalid_through_price_fn_too(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    for bad in (Decimal("NaN"), Decimal("Infinity"), float("nan")):
        st.x.price_fn = lambda sym, bad=bad: (bad, "TEST", st.clock.now.isoformat())
        r = cyc(st)
        assert r["result"] == "RUN" and ("SNDK", "ATTENTION") in decisions(r) and not exit_intents(st), (bad, r.get("error"), decisions(r))


def test_P16_an_unreadable_stream_is_not_a_simulated_feed(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    above = Decimal(pos.invalidation_price) + Decimal("5")
    st.x.price_fn = lambda sym: (above, "TEST", "2023-03-01T15:00:00+00:00")
    real_stream = st.x.stream_fn

    class Broken:
        def __getattr__(self, name):
            if name == "market":
                raise RuntimeError("stream down (test)")
            return getattr(real_stream(), name)
    st.x.stream_fn = lambda: Broken()
    r = cyc(st)
    assert stop_blind(st, r), (ctl(st, "EXIT_TRIGGER"), decisions(r))                        # unknown is never "simulated": the age rule applies


NOW_P17 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize("price, source, age, simulated, expected", [
    (None, "QUOTE", 0, False, "PRICE_MISSING"), (None, "QUOTE", 0, True, "PRICE_MISSING"),
    (Decimal("0"), "QUOTE", 0, False, "PRICE_INVALID"), (Decimal("-1"), "QUOTE", 0, True, "PRICE_INVALID"), (Decimal("NaN"), "QUOTE", 0, False, "PRICE_INVALID"),
    (float("inf"), "QUOTE", 0, False, "PRICE_INVALID"), ("not a number", "QUOTE", 0, False, "PRICE_INVALID"),
    (Decimal("10"), "QUOTE", None, False, "PRICE_UNDATED"), (Decimal("10"), "QUOTE", None, True, None),
    (Decimal("10"), "QUOTE", 300, False, None), (Decimal("10"), "QUOTE", 301, False, "PRICE_STALE"),
    (Decimal("10"), "MINUTE_CLOSE", 900, False, None), (Decimal("10"), "MINUTE_CLOSE", 901, False, "PRICE_STALE"),
    (Decimal("10"), "QUOTE", -60, False, None), (Decimal("10"), "QUOTE", -61, False, "PRICE_FROM_FUTURE"),
    (Decimal("10"), "QUOTE", 99999, True, None),
])
def test_P17_the_stop_price_rules_one_by_one(price, source, age, simulated, expected):
    from execution.service import stop_price_problem
    at = None if age is None else (NOW_P17 - timedelta(seconds=age)).isoformat()
    assert stop_price_problem(price, source, at, NOW_P17, simulated) == expected


def test_P18_a_later_healthy_position_never_hides_a_blind_stop_check_on_the_control_row(tmp_path):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    entered(st, "MU")                                                                        # positions are evaluated newest first: MU, then SNDK
    fine = Decimal(pos.invalidation_price) + Decimal("5")
    st.x.price_fn = lambda sym: (None, "UNAVAILABLE", None) if sym == "MU" else (fine, "TEST", st.clock.now.isoformat())
    r = cyc(st)
    c = ctl(st, "EXIT_TRIGGER")
    assert [d for d in decisions(r) if d[0] in ("MU", "SNDK")] == [("MU", "ATTENTION"), ("SNDK", "HOLD")]   # the blind one first, then a PASS
    assert c["last_result"] == "UNKNOWN" and "MU" in c["last_reason"], c                     # not SNDK's later PASS


# ================================================================ E: credentials in test environments ===================================================

def test_E01_this_test_process_loaded_no_env_file_and_holds_no_credential():
    assert os.environ.get("STOCK_AGENT_SKIP_DOTENV") == "1"                                 # set by tests/conftest.py before any project import
    assert "PYTHON_DOTENV_DISABLED" not in os.environ                                       # python-dotenv itself stays on: explicit synthetic files load (E05)
    present = [n for n in os.environ if re.match(r"(?i)^(ALPACA|APCA|ANTHROPIC|ROBINHOOD|FRED_API_KEY|SEC_USER_AGENT|OPENAI)", n)]
    assert not present, f"credential variables present in the test process: {present}"     # names only, never values
    set_ = [n for n in CREDENTIAL_SETTINGS if getattr(config, n, "")]
    assert not set_, f"credential settings set in the test process: {set_}"


def test_E02_a_checkout_nested_inside_another_never_loads_the_outer_env(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "nested_checkout"
    inner.mkdir(parents=True)
    (outer / ".env").write_text("ALPACA_API_KEY=FAKE-NESTED-SENTINEL-NOT-A-KEY\n", encoding="utf-8")   # synthetic; the real .env is never read
    env = {k: v for k, v in os.environ.items() if k not in ("STOCK_AGENT_SKIP_DOTENV", "PYTHON_DOTENV_DISABLED")
           and not re.match(r"(?i)^(ALPACA|APCA|ANTHROPIC|ROBINHOOD|FRED_API_KEY|SEC_USER_AGENT)", k)}   # as outside tests

    def loads(config_text):
        (inner / "config.py").write_text(config_text, encoding="utf-8")
        out = subprocess.run([sys.executable, "-B", "-c", "import config; print('LOADED' if config.ALPACA_API_KEY else 'EMPTY')"], cwd=inner, env=env,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-800:]
        return out.stdout.strip()
    base = subprocess.run(["git", "show", "4c01519566a0bb66ebcff9cc51f5aa822194850b:config.py"], cwd=ROOT, capture_output=True, text=True).stdout
    assert base and loads(base) == "LOADED"                                                  # the audit finding, reproduced on main's config.py
    assert loads((ROOT / "config.py").read_text(encoding="utf-8")) == "EMPTY"                # this checkout's config.py reads only its own .env


SECRET_NAMES = CREDENTIAL_SETTINGS + ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")   # + the paper broker's
SECRET_NAME_REFS = ("KEY_ENV", "SECRET_ENV")                                                   # execution/broker.py's names of the paper key variables


def _is_secret_name(e):
    return isinstance(e, ast.Constant) and e.value in SECRET_NAMES or isinstance(e, ast.Attribute) and e.attr in SECRET_NAME_REFS


def _reads_secret(expr):
    """True when `expr` reads a credential VALUE: config.<SETTING>, os.environ[<NAME>], .get / os.getenv(<NAME>) or getattr(x, <NAME>), a
    NAME being a credential variable name or execution/broker.py's KEY_ENV / SECRET_ENV."""
    for a in ast.walk(expr):
        if isinstance(a, ast.Attribute) and a.attr in SECRET_NAMES:
            return True
        if isinstance(a, ast.Subscript) and _is_secret_name(a.slice):
            return True
        if isinstance(a, ast.Call):
            fname = a.func.attr if isinstance(a.func, ast.Attribute) else a.func.id if isinstance(a.func, ast.Name) else ""
            if fname in ("get", "getenv") and a.args and _is_secret_name(a.args[0]):
                return True
            if fname == "getattr" and len(a.args) >= 2 and _is_secret_name(a.args[1]):
                return True
    return False


def _scope_secret_uses(scope, nodes):
    """The sinks in `nodes` (one function, or the module's own statements) that print a credential value: an assert (test or message),
    fail(...) / pytest.fail(...) or raise ...Error(...), reading one directly or through a variable of this scope assigned (also annotated,
    unpacked or copied) from one, or bound by a loop over one."""
    tainted = set()
    targets = lambda t: {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}  # noqa: E731
    uses = lambda e: _reads_secret(e) or bool(targets(e) & tainted)  # noqa: E731

    def copy_of_secret(e):                                                                    # a read, or a plain copy / unpacking of a tainted value
        if _reads_secret(e):
            return True
        if isinstance(e, (ast.Tuple, ast.List)):
            return any(copy_of_secret(x) for x in e.elts)
        return isinstance(e, (ast.Name, ast.Attribute, ast.Subscript, ast.Starred)) and bool(targets(e) & tainted)
    for _ in range(5):                                                                        # taint through copies, to a fixed point
        before = set(tainted)
        for node in nodes:
            if isinstance(node, ast.Assign) and copy_of_secret(node.value):
                tainted |= {n for t in node.targets for n in targets(t)}
            if isinstance(node, ast.AnnAssign) and node.value is not None and copy_of_secret(node.value):
                tainted |= targets(node.target)
            if isinstance(node, ast.For) and (uses(node.iter) or any(isinstance(a, ast.Constant) and a.value in CREDENTIAL_SETTINGS for a in ast.walk(node.iter))):
                tainted |= targets(node.target)
        if tainted == before:
            break
    hits = []
    for node in nodes:
        if isinstance(node, ast.Assert):
            exprs = [node.test, node.msg]
        elif isinstance(node, ast.Call) and (isinstance(node.func, ast.Attribute) and node.func.attr == "fail" or isinstance(node.func, ast.Name) and node.func.id == "fail"):
            exprs = list(node.args)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name) and node.exc.func.id.endswith("Error"):
            exprs = list(node.exc.args)
        else:
            continue
        if any(e is not None and uses(e) for e in exprs):
            hits.append(node.lineno)
    return hits


def _assert_secret_uses(tree):
    """Every sink of the file that prints a credential value (_scope_secret_uses), each function analysed as its own scope."""
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    inner = {id(x) for f in funcs for x in ast.walk(f)}
    hits = [h for f in funcs for h in _scope_secret_uses(f, list(ast.walk(f)))]
    hits += _scope_secret_uses(tree, [n for n in ast.walk(tree) if id(n) not in inner])
    return sorted(set(hits))


def test_E03_no_test_puts_a_credential_value_into_an_assertion():
    found = {}
    for p in sorted((ROOT / "tests").glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        lines = _assert_secret_uses(ast.parse(p.read_text(encoding="utf-8")))
        if lines:
            found[p.name] = lines
    assert not found, found                                                                  # main 4c01519: test_automation_37, test_history_budget_39


def test_E04_the_secret_scan_itself_catches_every_form():
    src = ("import config, os, pytest\n"
           "def t1(text):\n    for s in (config.ALPACA_API_KEY, 'x'):\n        assert s not in text\n"
           "def t2(text):\n    assert config.ANTHROPIC_API_KEY not in text\n"
           "def t3(text):\n    leaked = [n for n in ('ALPACA_API_KEY',) if n]\n    assert not leaked\n"
           "def t4(text):\n    k = config.ALPACA_SECRET_KEY\n    assert k not in text\n"
           "def t5(text):\n    assert os.environ['ROBINHOOD_PASSWORD'] not in text\n"
           "def t6(text):\n    assert os.getenv('FRED_API_KEY') not in text and os.environ.get('SEC_USER_AGENT') != text\n"
           "def t7(text):\n    pytest.fail(f'leaked {config.ALPACA_API_KEY}')\n"
           "def t8(text):\n    assert 'x' in text, config.ROBINHOOD_USERNAME\n"
           "def t9(text):\n    assert os.environ['ALPACA_PAPER_SECRET_KEY'] not in text\n"
           "def t10(text):\n    assert getattr(config, 'ALPACA_SECRET_KEY') not in text\n"
           "def t11(text):\n    assert os.environ[XB.SECRET_ENV] not in text\n"
           "def t12(text):\n    key, secret = os.environ['APCA_API_KEY_ID'], os.environ.get(XB.SECRET_ENV)\n    copy = secret\n    assert copy not in text\n"
           "def t13(text):\n    k: str = config.ANTHROPIC_API_KEY\n    raise AssertionError(k)\n"
           "def t14(text):\n    fail(f'{config.FRED_API_KEY}')\n"
           "def t15(text):\n    assert k not in text\n")                                    # k is t4's / t13's: another scope
    assert _assert_secret_uses(ast.parse(src)) == [4, 6, 12, 14, 16, 18, 20, 22, 24, 26, 30, 33, 35]   # every form; t3 (names) and t15 are fine


CREDENTIAL_ENV_W1 = r"(?i)^(ALPACA|APCA|ANTHROPIC|ROBINHOOD|FRED_API_KEY|SEC_USER_AGENT|OPENAI|POLYGON|FINNHUB|ALPHA_?VANTAGE)"


def test_E05_a_test_child_skips_this_checkouts_env_but_still_loads_an_explicit_synthetic_file(tmp_path):
    co = tmp_path / "checkout"
    co.mkdir()
    (co / ".env").write_text("ALPACA_API_KEY=FAKE-OWN-ENV-SENTINEL-NOT-A-KEY\n", encoding="utf-8")       # synthetic; no real .env is ever read
    (co / "config.py").write_text((ROOT / "config.py").read_text(encoding="utf-8"), encoding="utf-8")
    synthetic = tmp_path / "synthetic.env"
    synthetic.write_text("ALPACA_API_KEY=FAKE-EXPLICIT-SENTINEL-NOT-A-KEY\n", encoding="utf-8")
    test_env = {k: v for k, v in os.environ.items() if not re.match(CREDENTIAL_ENV_W1, k)}   # what a test's child inherits (the switch included)

    def child(code, env):
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=co, env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-800:]
        return out.stdout.strip()
    probe = "import config; print('LOADED' if config.ALPACA_API_KEY else 'EMPTY')"
    assert child(probe, test_env) == "EMPTY"                                                 # a test's child: this checkout's .env is skipped
    assert child(probe, {k: v for k, v in test_env.items() if k != "STOCK_AGENT_SKIP_DOTENV"}) == "LOADED"   # outside tests: its own .env, as before
    explicit = ("import os; from dotenv import load_dotenv; load_dotenv(os.environ['W1_SYNTHETIC_ENV']); "
                "print('LOADED' if os.environ.get('ALPACA_API_KEY') else 'EMPTY')")
    assert child(explicit, {**test_env, "W1_SYNTHETIC_ENV": str(synthetic)}) == "LOADED"      # review M2: an explicit synthetic file still loads


def test_E06_conftest_removes_every_credential_variable_and_keeps_the_rest(tmp_path):
    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    block = src[src.index("# Wave 1 (audit EP-13)"):src.index("ROOT = Path(__file__)")]         # exactly the lines conftest runs first
    fakes = {"ALPACA_API_KEY": "FAKE-1", "APCA_API_SECRET_KEY": "FAKE-2", "ROBINHOOD_PASSWORD": "FAKE-3", "anthropic_api_key": "FAKE-4", "FRED_API_KEY": "FAKE-5",
             "SEC_USER_AGENT": "FAKE-6", "OPENAI_API_KEY": "FAKE-7", "ALPHAVANTAGE_KEY": "FAKE-8", "W1_UNRELATED_SETTING": "kept"}
    code = ("import os, re\n" + block + "print(sorted(n for n in os.environ if n.upper().startswith(('ALPACA', 'APCA', 'ROBINHOOD', 'ANTHROPIC', 'FRED', 'SEC_', "
            "'OPENAI', 'ALPHA'))), os.environ.get('W1_UNRELATED_SETTING'), os.environ.get('STOCK_AGENT_SKIP_DOTENV'))")
    out = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(tmp_path), env={**os.environ, **fakes}, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.split() == ["[]", "kept", "1"], (out.stdout, out.stderr[-600:])   # names only are printed



# ================================================================ F: scope and bookkeeping ==============================================================

def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_W1}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W1} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #50 (Wave 1 merged; the Wave 2 base): what Wave 1 left, whatever a later change made since."""
    run = subprocess.run(["git", "show", f"{FL.BASE_W2A}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W2A} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


APP_EDITS_W1 ={".github/ci/pytest_groups.json", "config.py", "execution/service.py", "safety/service.py", "safety/store.py"}   # the CI entry and the four fixed files
NEW_LEDGER_W1 = {"safety/service.py", "safety/store.py", "tests/conftest.py", "tests/test_automation_37.py", "tests/test_history_budget_39.py"}   # main's files Wave 1 registers
CHANGED_METHODS_W1 = {"__init__", "_evaluate_positions", "_scan_entries", "_recheck_entry", "_attention"}   # the ExecutionService methods Wave 1 extends
NEW_METHODS_W1 = {"_feed_simulated", "_market_open_for_stop_check"}


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_market_data_broker_or_research_file_changed():
    assert FL.BASE_W1 == "4c01519566a0bb66ebcff9cc51f5aa822194850b" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W1}^{{commit}}"], cwd=ROOT).returncode == 0
    stage = set(_git("diff", "--name-only", FL.BASE_W1, FL.BASE_W2A).split())                # Wave 1 itself, between two fixed commits (main before / after PR #50)
    changed = set(_git("diff", "--name-only", FL.BASE_W1).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    later = FL.NEW_W2A | FL.EDITED_W2A | FL.NEW_W2B | FL.EDITED_W2B | FL.NEW_W2C | FL.EDITED_W2C | FL.NEW_EX06 | FL.EDITED_EX06 | FL.NEW_XSS01 | FL.EDITED_XSS01                                                         # Wave 2 PR A, pinned by its own F tests
    assert stage == FL.NEW_W1 | FL.EDITED_W1, {"unexpected": sorted(stage - FL.NEW_W1 - FL.EDITED_W1), "missing": sorted((FL.NEW_W1 | FL.EDITED_W1) - stage)}
    assert changed == FL.NEW_W1 | FL.EDITED_W1 | later, {"unexpected": sorted(changed - FL.NEW_W1 - FL.EDITED_W1 - later), "missing": sorted((FL.NEW_W1 | FL.EDITED_W1 | later) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_W1).split())
    assert {p for p in stage if p not in in_base} == FL.NEW_W1 and {p for p in stage if p in in_base} == FL.EDITED_W1   # so every other file of main is untouched
    assert {p for p in FL.EDITED_W1 if not p.startswith("tests/")} == APP_EDITS_W1             # no strategy, setup, proposal, market-data, broker, SIP or UI file
    assert set(FL.SEMANTIC_W1) == APP_EDITS_W1 - {".github/ci/pytest_groups.json"} | {"tests/conftest.py"} and FL.INSTRUMENTATION_W1 == {}
    assert {"execution/engine.py", "execution/broker.py", "safety/engine.py", "proposals/engine.py", "proposals/__init__.py"}.isdisjoint(changed)   # the rules and limits
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")                                       # a marked block an older freeze test would strip unseen
    for rel in sorted(FL.NEW_W1 | APP_EDITS_W1 | {"tests/conftest.py"}):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in post(rel).splitlines() if l not in base and marker.match(l)], rel


def test_F02_the_safety_semantics_changed_exactly_as_declared_and_no_threshold_or_frozen_control_moved():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)                              # a clean process: the real configuration
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    svc, svc_base = FL.pre_w2a("execution/service.py", now_text("execution/service.py")), at_base("execution/service.py")   # Wave 2 PR A's accounting change: its own tests
    assert [l for l in svc_base.splitlines() if l not in set(svc.splitlines())] == []         # additive: every line of main's executor is kept
    added = "\n".join(l for l in svc.splitlines() if l not in set(svc_base.splitlines()))
    assert not re.search(r"post_order|insert_intent|update_intent|enable_auto|disable_auto|set_mode|submit|cancel|liquidat|flatten", added)   # never an order or a mode change

    def methods(text):
        cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "ExecutionService")
        return {n.name: ast.dump(n) for n in cls.body if isinstance(n, ast.FunctionDef)}
    m_now, m_base = methods(svc), methods(svc_base)
    assert set(m_now) - set(m_base) == NEW_METHODS_W1 and set(m_base) <= set(m_now)
    assert {k for k in m_base if m_base[k] != m_now[k]} == CHANGED_METHODS_W1                  # exit_decision, entry_controls, sizing: untouched (execution/engine.py)
    top = lambda t: {n.name for n in ast.parse(t).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {  # noqa: E731
        x.id for n in ast.parse(t).body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    assert top(svc) - top(svc_base) == {"stop_price_problem", "STOP_PRICE_TEXT", "STOP_PRICE_FUTURE_TOLERANCE_S"}
    import execution.service as XS
    import proposals as PR
    assert (XS.PRICE_MAX_AGE_SECONDS, XS.MINUTE_BAR_MAX_AGE_SECONDS) == (PR.PRICE_MAX_AGE_SECONDS, PR.MINUTE_BAR_MAX_AGE_SECONDS) == (300, 900)   # the entry side's limits, reused
    assert XS.STOP_PRICE_FUTURE_TOLERANCE_S == 60.0
    cfg = now_text("config.py")
    assert FL.undo_w1("config.py", cfg) == at_base("config.py")                                 # the one declared replacement, nothing else
    assert [l for l in at_base("config.py").splitlines() if l not in set(cfg.splitlines())] == list(FL.REMOVED_W1["config.py"])
    stored = lambda t: {n.id for n in ast.walk(ast.parse(t)) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}  # noqa: E731
    assert stored(cfg) == stored(at_base("config.py"))                                         # no new setting
    assert FL.undo_w1("tests/conftest.py", FL.undo_ex06("tests/conftest.py", now_text("tests/conftest.py"))) == at_base("tests/conftest.py")
    defs = lambda t: sorted(n.name for n in ast.parse(t).body if isinstance(n, (ast.ClassDef, ast.FunctionDef)))  # noqa: E731
    assert defs(now_text("safety/store.py")) == sorted(defs(at_base("safety/store.py")) + ["SafetyStateChanged"])
    assert defs(now_text("safety/service.py")) == defs(at_base("safety/service.py"))
    import safety.service as SSV
    import safety.store as SST
    assert issubclass(SST.SafetyStateChanged, SST.SafetyStoreError) and SSV.MAX_STATE_ATTEMPTS == 3


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_W1.items():
        text = post(rel)                                                                       # Wave 1's edits as main left them (Wave 2 PR A extends some lines: its own F03)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")             # the ledger as Wave 1 left it (Wave 2 PR A's additions: its own F03)
    main_ledger = ast.literal_eval(fl[fl.index("LEDGER = {"):fl.index("}\n", fl.index("LEDGER = {")) + 2][len("LEDGER = "):])
    assert set(REPLAY_W1) == FL.EDITED_W1 - set(main_ledger) - {"tests/freeze_ledger.py"}      # every other edited file carries a ledger entry
    start = fl.index("\nWave 1 safety remediation (the independent")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_78B1 + NEW_W1 plus this ledger.", " + NEW_78B1 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_W1 = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    helper = rebuilt[rebuilt.index("\n\ndef undo_w1("):rebuilt.index("\n\ndef unchanged(")]
    rebuilt = rebuilt.replace(helper, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                    # the declarations, the paragraph, one line, the helper and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(main_ledger) - set(old) == NEW_LEDGER_W1 and set(old) <= set(main_ledger)
    assert {k for k in old if old[k] != main_ledger[k]} == FL.EDITED_W1 & set(old)            # every re-hash is a declared Wave 1 edit
    for rel in sorted(NEW_LEDGER_W1):
        assert FL.base_text(rel) == at_base(rel), rel                                        # unchanged from BASE_68 to main: the ledger view IS main's text
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_W1 = {                                                                                # every edit of a file no ledger entry can hold: (main, Wave 1)
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1\n',
         '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541, rel               # since then: only Stage 5.4.1's declared correction\n",
         "            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n"),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('from dynamic_universe import signals as SIG  # noqa: E402\n\n',
         'from dynamic_universe import signals as SIG  # noqa: E402\nfrom freeze_ledger import SEMANTIC_W1  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n\n'),
        ('        assert base and (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base, rel\n',
         '        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1), rel   # Wave 1: its declared stop-check change, no universe wiring\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                      # Stage 5.4.2, pinned by its own F tests\n',
         '    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                      # Stage 5.4.2, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel), rel\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n',
         '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel), rel      # since then: Stage 6.10D touches none of these\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                         # Stage 7.0, pinned by its own F tests\n',
         '    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                         # Stage 7.0, pinned by its own F tests\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ('    assert not changed & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",\n',
         '    assert not (changed - set(FL.SEMANTIC_W1)) & {"frontend/index.html", "tests/conftest.py", ".github/workflows/tests.yml", "DESIGN_60_AUTONOMOUS_PAPER.md", "forward_validation/__init__.py",   # Wave 1: its declared conftest change\n'),
        ('        assert not [l for l in post(rel).splitlines() if l not in set(now_text(rel).splitlines())], rel   # a later stage keeps every line Stage 6.10A left\n',
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, now_text(rel)).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_market_intel_70.py': [
        ('    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # Stages 7.1-7.3: pinned by their own F tests\n',
         '    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # Stages 7.1-7.3: pinned by their own F tests\n'),
        ('    assert now_text("config.py") == at_base("config.py") and now_text("api/server.py") == at_base("api/server.py")\n',
         '    assert FL.undo_w1("config.py", now_text("config.py")) == at_base("config.py") and now_text("api/server.py") == at_base("api/server.py")   # Wave 1\'s declared config.py edit undone first\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                           # Stages 7.3.1, 7.4: pinned by their own F tests\n',
         '    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                           # Stages 7.3.1, 7.4: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                         # Stage 7.5, pinned by its own F tests\n',
         '    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                         # Stage 7.5, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                         # Stage 7.4, pinned by its own F tests\n',
         '    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                         # Stage 7.4, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                           # Stages 7.3, 7.3.1: pinned by their own F tests\n',
         '    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                           # Stages 7.3, 7.3.1: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                             # Stages 7.2, 7.3: pinned by their own F tests\n',
         '    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                             # Stages 7.2, 7.3: pinned by their own F tests\n'),
        ('                assert now_text(rel) == at_base(rel), rel                               # no trading, safety, startup, API, DB, UI or harness file changed\n',
         '                assert post(rel) == at_base(rel), rel                                   # no trading, safety, startup, API, DB, UI or harness file changed\n'),
        ('        assert now_text(rel) == at_base(rel), rel\n',
         '        assert post(rel) == at_base(rel), rel\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n',
         '    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
        ('    assert (ROOT / "config.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n").startswith(base_cfg.rstrip("\\n"))   # config.py: append-only\n',
         '    assert FL.undo_w1("config.py", (ROOT / "config.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n")).startswith(base_cfg.rstrip("\\n"))   # config.py: append-only (Wave 1\'s declared edit undone first)\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1\n',
         '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1\n'),
    ],
    'tests/test_sip_eval_78b1.py': [
        ('\nNEW_FILES_78B1 = {"DESIGN_78B1_SIP_EVALUATION.md", "tests/test_sip_eval_78b1.py", "tests/sip_eval_fixtures.py"} | {\n',
         '\ndef post(rel):\n    """main after PR #49 (Stage 7.8B-1 merged; the Wave 1 base): what Stage 7.8B-1 left, whatever a later change made since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_W1}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W1} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nNEW_FILES_78B1 = {"DESIGN_78B1_SIP_EVALUATION.md", "tests/test_sip_eval_78b1.py", "tests/sip_eval_fixtures.py"} | {\n'),
        ('    assert changed == FL.NEW_78B1 | FL.EDITED_78B1, {"unexpected": sorted(changed - FL.NEW_78B1 - FL.EDITED_78B1),\n                                                     "missing": sorted((FL.NEW_78B1 | FL.EDITED_78B1) - changed)}\n',
         '    stage = set(_git("diff", "--name-only", FL.BASE_78B1, FL.BASE_W1).split())               # Stage 7.8B-1 itself, between two fixed commits\n    assert stage == FL.NEW_78B1 | FL.EDITED_78B1, {"unexpected": sorted(stage - FL.NEW_78B1 - FL.EDITED_78B1),\n                                                   "missing": sorted((FL.NEW_78B1 | FL.EDITED_78B1) - stage)}\n    later = FL.NEW_W1 | FL.EDITED_W1                                                           # Wave 1, pinned by its own F tests\n    assert changed == FL.NEW_78B1 | FL.EDITED_78B1 | later, {"unexpected": sorted(changed - FL.NEW_78B1 - FL.EDITED_78B1 - later),\n                                                             "missing": sorted((FL.NEW_78B1 | FL.EDITED_78B1 | later) - changed)}\n'),
        ('    assert {p for p in changed if p not in in_base} == FL.NEW_78B1 and {p for p in changed if p in in_base} == FL.EDITED_78B1\n',
         '    assert {p for p in stage if p not in in_base} == FL.NEW_78B1 and {p for p in stage if p in in_base} == FL.EDITED_78B1\n'),
        ('                assert now_text(rel) == at_base(rel), rel\n',
         '                assert post(rel) == at_base(rel), rel\n'),
        ('        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook, dependency or workflow edit\n',
         '        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook, dependency or workflow edit\n'),
        ('            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n',
         '            assert m61 not in post(rel) and m54 not in post(rel), rel\n'),
        ('        text = now_text(rel)\n',
         '        text = post(rel)\n'),
        ('    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n',
         '    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n'),
        ('    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_78B1 & set(old)\n',
         '    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.8B-1 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_78B1 & set(old)\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('    later = FL.NEW_78B1 | FL.EDITED_78B1                                                     # Stage 7.8B-1, pinned by its own F tests\n',
         '    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                     # Stage 7.8B-1, pinned by its own F tests\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ('        assert not [l for l in post(rel).splitlines() if l not in set(_now(rel).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left\n',
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, _now(rel)).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n"),
    ],
}
