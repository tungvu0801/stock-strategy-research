"""
Risk controls panel (RISK_CONTROLS_PANEL_DESIGN.md 3.1, section 4, section 6 "Python"): the one approved behaviour change in safety/service.py and
the text-only server edits.
  P01     the key race: a refresh cycle in flight, Emergency Stop pressed, the latch visible on a second connection in under 1 s, while a plain
          evaluation still waits for the cycle (the gate's wait is unchanged)
  P02-P05 latch first: a failed account read, a failed commit, a latch that raises, a Clear committed between the latch and the evaluation
  P06-P08 the latch's own event; Pause, Resume and Clear are never latched first; the first run with no state row
  P09-P10 the labels: per flag through the real gate, and every consumer of STATE_LABELS
  P11     a failed evaluation still reports a saved stop (B-3, no new route); only active=True is information
  P12-P14 the text-only edits, no overclaim word, a repeated stop writes one event
"""
import ast
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import safety as SF  # noqa: E402
from safety import engine as E  # noqa: E402
from safety.models import AccountEquity, SafetyView  # noqa: E402
from safety.service import ExecutionSafetyService  # noqa: E402
from safety.store import SafetyStore  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
OVERCLAIM = re.compile(r"(?i)\bimmediately\b|remains? protected|stay protected|all new entries|exits continue|decides its exit|trading stopped|trading halted")


def acct(equity="100000.00", source="HYPOTHETICAL_REVIEW", age_s=None):
    return AccountEquity(equity=equity, source=source, as_of=None, age_s=age_s, mode="simulated",
                         adjustments="NONE_POSSIBLE" if source in ("LOCAL_PAPER", "HYPOTHETICAL_REVIEW") else "UNAVAILABLE")


def svc(tmp_path, account_fn=None, name="panel.db", threshold=5):
    return ExecutionSafetyService(store=SafetyStore(tmp_path / name), account_fn=account_fn or (lambda ts: acct()), clock=lambda: NOW, startup_policy="KEEP",
                                  threshold_pct=threshold, max_age_s=900, accept_unadjusted=False)


def kills(s):
    return [e for e in s.store.events(100) if e["kind"] == "KILL_ON"]


def boom(ts):
    raise RuntimeError("account read failed")


# ---------------------------------------------------------------- P01: the key race on the stream's default wiring ---------------------------
def test_P01_the_stop_is_durable_in_under_1_s_while_a_plain_evaluation_still_waits_for_the_cash_flow_cycle(tmp_path, monkeypatch):
    import test_cash_flow_602 as CF6
    r = CF6.make_rig(tmp_path, monkeypatch, clock=CF6.FakeClock(CF6.OPEN))
    sched = CF6.AccountRefreshScheduler(lambda: r.ref, clock=lambda: r.clock.now, interval_s=60, idle_interval_s=300, cash_flow_fn=lambda: r.cf)
    sched.step(r.clock.now)
    assert CF6.gate(r).state == "TRADING_ALLOWED" and r.cf.cycle_active() is False
    entered, release = threading.Event(), threading.Event()
    real = r.cf.refresh

    def slow(now=None, trigger="SCHEDULED"):
        entered.set()
        release.wait(30)
        return real(now, trigger)
    r.cf.refresh = slow
    r.clock.advance(60)
    cycle = threading.Thread(target=sched.step, args=(r.clock.now,))
    cycle.start()
    out, plain = {}, {}
    try:
        assert entered.wait(10) and r.cf.cycle_active()                                     # a cycle is in flight: the gate's account read waits for it
        stop = threading.Thread(target=lambda: out.setdefault("v", r.s.safety.emergency_stop("panel P01", actor="test", now=r.clock.now)))
        ev = threading.Thread(target=lambda: plain.setdefault("v", r.s.safety.evaluate("TEST", r.clock.now)))
        t0 = time.monotonic()
        stop.start()
        ev.start()
        other = SafetyStore(r.s.safety.store.path)                                          # a second connection
        while not other.state().kill_active and time.monotonic() - t0 < 1.0:
            time.sleep(0.01)
        assert other.state().kill_active is True and time.monotonic() - t0 < 1.0, "the Emergency Stop latch is durable in under 1 s"
        assert stop.is_alive() and ev.is_alive() and "v" not in plain                         # both still wait for the same cycle: the wait is unchanged
    finally:
        release.set()
        cycle.join(30)
    stop.join(30)
    ev.join(30)
    assert out["v"].kill_switch.active is True and out["v"].state == "HALTED"
    assert len([e for e in r.s.safety.store.events(100) if e["kind"] == "KILL_ON"]) == 1


# ---------------------------------------------------------------- P02-P05: latch first --------------------------------------------------------
def test_P02_the_latch_is_saved_before_the_account_read_so_a_failed_read_still_leaves_one_saved_stop(tmp_path):
    s = svc(tmp_path, account_fn=boom)
    v = s.emergency_stop("x", actor="test", now=NOW)
    assert (v.state, v.kill_switch.active, v.persisted, v.state_label) == ("UNKNOWN", True, False, "Risk status unknown")
    assert s.store.state().kill_active is True
    ev = kills(s)
    assert len(ev) == 1 and ev[0]["input"]["saved_before_evaluation"] is True and ev[0]["to_state"] == "HALTED" and ev[0]["actor"] == "test"


def test_P03_a_failed_commit_after_the_latch_leaves_one_saved_stop_and_reports_it(tmp_path, monkeypatch):
    s = svc(tmp_path)

    def fail_commit(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(s.store, "commit", fail_commit)
    v = s.emergency_stop("x", actor="test", now=NOW)
    assert (v.state, v.kill_switch.active) == ("UNKNOWN", True)
    assert s.store.state().kill_active is True and len(kills(s)) == 1


def test_P04_a_latch_that_raises_leaves_the_action_to_the_evaluation_then_the_fallback(tmp_path, monkeypatch):
    s = svc(tmp_path)
    real = s.store.latch
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(s.store, "latch", flaky)
    v = s.emergency_stop("x", actor="test", now=NOW)
    assert (v.state, v.kill_switch.active) == ("HALTED", True) and len(calls) == 1               # the evaluation applied it; no fallback was needed
    ev = kills(s)
    assert len(ev) == 1 and "saved_before_evaluation" not in ev[0]["input"]
    # everything fails: the latch, the commit and the fallback; the view is UNKNOWN and says nothing about a stop that was never saved
    s2 = svc(tmp_path, name="panel2.db")

    def always(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(s2.store, "latch", always)
    monkeypatch.setattr(s2.store, "commit", always)
    v2 = s2.emergency_stop("x", actor="test", now=NOW)
    assert (v2.state, v2.kill_switch.active) == ("UNKNOWN", False) and s2.store.state().kill_active is False and kills(s2) == []


def test_P05_a_clear_committed_between_the_latch_and_the_evaluation_stands_and_no_second_event_is_written(tmp_path):
    s = svc(tmp_path)
    other = svc(tmp_path)                                                                    # another writer on the same database
    calls = []

    def read_then_clear(ts):
        if not calls:
            calls.append(1)
            assert SafetyStore(s.store.path).state().kill_active is True                     # the latch is already durable here
            other.clear_emergency_stop("cleared meanwhile", actor="test", now=NOW)
        return acct()
    s.account_fn = read_then_clear
    v = s.emergency_stop("stop", actor="test", now=NOW)
    assert v.kill_switch.active is False and s.store.state().kill_active is False             # never re-applied: the later Clear stands
    assert len(kills(s)) == 1 and len([e for e in s.store.events(100) if e["kind"] == "KILL_OFF"]) == 1


# ---------------------------------------------------------------- P06-P08 -----------------------------------------------------------------
def test_P06_the_latch_first_event_records_to_state_halted_and_the_last_state_in_memory(tmp_path):
    s = svc(tmp_path)
    s.emergency_stop("first", actor="test", now=NOW)
    assert kills(s)[0]["from_state"] is None and kills(s)[0]["to_state"] == "HALTED"            # a fresh process has no last view
    s2 = svc(tmp_path, name="panel2.db")
    assert s2.evaluate("TEST", NOW).state == "TRADING_ALLOWED"
    s2.emergency_stop("second", actor="test", now=NOW)
    assert kills(s2)[0]["from_state"] == "TRADING_ALLOWED"
    s3 = svc(tmp_path, name="panel3.db", account_fn=boom)
    assert s3.evaluate("TEST", NOW).state == "UNKNOWN"                                         # the last view failed: from_state says so
    s3.emergency_stop("third", actor="test", now=NOW)
    assert kills(s3)[0]["from_state"] == "UNKNOWN"


def test_P07_pause_resume_and_clear_keep_todays_order_and_are_never_latched_first(tmp_path, monkeypatch):
    s = svc(tmp_path)
    real = s.store.latch
    seen = []
    monkeypatch.setattr(s.store, "latch", lambda now, update, event: seen.append(sorted(update)) or real(now, update, event))
    assert s.pause("p", actor="test", now=NOW).state == "NO_NEW_ENTRIES" and seen == []
    assert s.resume("r", actor="test", now=NOW).state == "TRADING_ALLOWED" and seen == []
    assert s.emergency_stop("k", actor="test", now=NOW).state == "HALTED" and len(seen) == 1 and seen[0][0].startswith("kill_")
    assert s.clear_emergency_stop("c", actor="test", now=NOW).state == "TRADING_ALLOWED" and len(seen) == 1
    s.account_fn = boom                                                                      # a Pause whose evaluation fails still uses today's fallback
    s.pause("p2", actor="test", now=NOW)
    assert len(seen) == 2 and seen[1][0].startswith("pause_") and s.store.state().pause_active is True


def test_P08_the_first_run_with_no_state_row_saves_the_stop(tmp_path):
    s = svc(tmp_path)
    assert not (tmp_path / "panel.db").exists()
    v = s.emergency_stop("first run", actor="test", now=NOW)
    assert v.kill_switch.active is True and v.state == "HALTED" and s.store.state().kill_active is True and len(kills(s)) == 1


# ---------------------------------------------------------------- P09-P10: labels -------------------------------------------------------------
def test_P09_icon_label_follows_section_4_per_flag_and_the_real_gate_reports_it(tmp_path):
    assert E.icon_label("TRADING_ALLOWED") == ("🟢", "Trading allowed")
    assert E.icon_label("NO_NEW_ENTRIES", pause_active=True) == ("🟡", "New automatic entries paused")
    assert E.icon_label("NO_NEW_ENTRIES", pause_active=False) == ("🟡", "New automatic entries blocked: daily-loss data unavailable")
    assert E.icon_label("HALTED", kill_active=False) == ("🔴", "New automatic entries stopped")
    assert E.icon_label("HALTED", kill_active=True) == ("🔴", "New entries stopped")              # the order-gate PR split the kill (panel design section 4)
    assert E.icon_label("UNKNOWN") == ("🔴", "Risk status unknown") and E.icon_label("SOMETHING_ELSE")[1] == "Risk status unknown"
    s = svc(tmp_path)
    assert s.evaluate("TEST", NOW).state_label == "Trading allowed"
    p = s.pause("p", actor="test", now=NOW)
    assert (p.state_label, p.reason_text) == ("New automatic entries paused", "New automatic entries paused by you")
    s.resume("r", actor="test", now=NOW)
    s.overrides = {"equity": "94000.00"}                                                     # a 6 % loss on the persisted 100000 baseline
    d = s.evaluate("TEST", NOW)
    assert (d.state, d.state_label, d.kill_switch.active, d.reason_codes[0]) == ("HALTED", "New automatic entries stopped", False, SF.R_DAILY)
    s2 = svc(tmp_path, name="panel2.db", account_fn=lambda ts: acct(source="ALPACA_PAPER_VIEW", age_s=99999))
    n = s2.evaluate("TEST", NOW)
    assert (n.state, n.state_label, n.no_new_entries.active) == ("NO_NEW_ENTRIES", "New automatic entries blocked: daily-loss data unavailable", False)
    k = svc(tmp_path, name="panel3.db").emergency_stop("k", actor="test", now=NOW)
    assert (k.state_label, k.reason_codes[0]) == ("New entries stopped", SF.R_KILL)            # the kill: the order gate also refuses manual paper BUYs
    assert svc(tmp_path, name="panel4.db", account_fn=boom).evaluate("TEST", NOW).state_label == "Risk status unknown"


def test_P10_every_consumer_of_the_labels_reads_the_new_words(tmp_path):
    assert SF.STATE_LABELS == {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New automatic entries paused", "HALTED": "New automatic entries stopped",
                               "UNKNOWN": "Risk status unknown"}
    assert SafetyView().state_label == "Risk status unknown"
    from api.routes import execution_safety as RX
    import decision_flow as DF
    assert RX.STATE_LABELS is SF.STATE_LABELS and DF.SAFETY_STATE_LABELS is SF.STATE_LABELS
    from proposals.account import AccountAdapter
    from proposals.service import ProposalService
    from proposals.store import ProposalStore
    db = tmp_path / "proposals.db"
    ps = ProposalService(stream_fn=lambda: None, store=ProposalStore(db), account=AccountAdapter(path=db), clock=lambda: NOW, safety=None)
    assert ps.execution_eligibility("REVIEWABLE", NOW)["state_label"] == "Risk status unknown"
    src = (ROOT / "trader_view" / "stream.py").read_text(encoding="utf-8")
    assert '"NO_NEW_ENTRIES": "New automatic entries paused"' in src and '"HALTED": "New automatic entries stopped"' in src and "Trading stopped" not in src


# ---------------------------------------------------------------- P11: a failed evaluation still reports a saved stop --------------------------
def test_P11_a_failed_evaluation_reports_the_saved_kill_switch_and_an_unreadable_one_as_no_information(tmp_path, monkeypatch):
    s = svc(tmp_path)
    s.emergency_stop("k", actor="test", now=NOW)
    s.account_fn = boom
    v = s.evaluate("API", NOW)
    assert (v.state, v.kill_switch.active, v.kill_switch.activated_by, v.kill_switch.reason) == ("UNKNOWN", True, "test", "k")

    def unreadable():
        raise sqlite3.OperationalError("unreadable")
    monkeypatch.setattr(s.store, "state", unreadable)
    v2 = s.evaluate("API", NOW)
    assert (v2.state, v2.kill_switch.active) == ("UNKNOWN", False)                           # the default: the panel never reads it as "not active"


# ---------------------------------------------------------------- P12-P14: the text-only edits ------------------------------------------------
def test_P12_the_text_only_edits_say_exactly_what_section_4_says():
    from execution.account_positions import MANAGEMENT
    assert MANAGEMENT["BOT"] == ("Bot-managed", "Opened by the bot from a broker-confirmed fill; the bot runs its exit rules while it is monitored; "
                                                "a position marked Needs attention gets no automatic exit.")
    assert SF.REASON_TEXT[SF.R_PAUSE] == "New automatic entries paused by you"
    src = (ROOT / "execution" / "service.py").read_text(encoding="utf-8")
    assert 'card.line = "Risk controls stopped new automatic paper trades"' in src and "Risk controls stopped new paper trades" not in src
    dr = (ROOT / "strategy_registry" / "downside_recovery.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert ("block new automatic entries only; exit rules keep running while a position is monitored, a position marked Needs attention gets no automatic exit, "
            "and the stop never liquidates") in dr.replace('"\n        "', "")
    assert "exits continue" not in dr
    assert (ROOT / "proposals" / "service.py").read_text(encoding="utf-8").count('"state_label": "Risk status unknown"') == 1


def test_P13_no_overclaim_word_in_the_server_texts_the_dashboard_shows():
    from execution.account_positions import MANAGEMENT
    texts = list(SF.STATE_LABELS.values()) + list(SF.REASON_TEXT.values()) + [SF.STATE_LABEL_DAILY_UNAVAILABLE] + [t for pair in MANAGEMENT.values() for t in pair]
    for t in texts:
        assert not OVERCLAIM.search(t), t


def test_P14_a_repeated_stop_writes_one_event_and_reports_it_active(tmp_path):
    s = svc(tmp_path)
    s.emergency_stop("a", actor="test", now=NOW)
    v = s.emergency_stop("b", actor="test", now=NOW)
    assert v.kill_switch.active is True and len(kills(s)) == 1
