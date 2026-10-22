"""
Stage 5.8 — SETUP STATE MACHINE + CONTINUOUS MARKET-HOURS ALERTS (deterministic; READY = READY FOR REVIEW; no order path). Focused tests:
  T01–T15 transition engine · P01–P11 persistence / restart · S01–S07 scheduler (fake clock, never sleeps) · I01–I08 the real service path
  (forced transitions through the stream, the socket and the API) · F01–F09 safety · U01–U04 frontend · B01 base pins
"""
import ast
import inspect
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import setups as S  # noqa: E402
from setups import STATE_LABELS, STATES  # noqa: E402
from setups.engine import alerts_for, evaluate, evidence_contradiction, evidence_gate  # noqa: E402
from setups.market_hours import describe, is_regular_session, next_session_open, seconds_until_open  # noqa: E402
from setups.models import Decision, EpisodeRecord, SetupAlert, SetupInputs, SetupView  # noqa: E402
from setups.scheduler import TEST_ENVS, SetupScheduler, get_scheduler, start_if_enabled  # noqa: E402
from setups.service import SetupService, alert_text  # noqa: E402
from setups.store import SetupStore, setup_id_for  # noqa: E402
from trader_view import MESSAGE_TYPES, SYMBOL_BOUND_TYPES  # noqa: E402
from trader_view.adapters import NullPositionAdapter, NullResearchAdapter  # noqa: E402
from trader_view.hub import accepts_message  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_trader_view_55 import FORBIDDEN_IMPORTS, T0, WL, FakePositions, FakeResearch, burst, hello_of, marker  # noqa: E402

BASE = "8194409"                                                                       # origin/main the stage was branched from (Stage 5.7 merged)
UTC = timezone.utc
IN_SESSION = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)                                # Wednesday 11:00 ET
OUT_SESSION = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)                                # Tuesday 22:00 ET
WEEKEND = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)                                   # Saturday
JS = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
RENDER = (ROOT / "frontend" / "trader_render.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend" / "trader_core.js").read_text(encoding="utf-8")
ALERTS_JS = (ROOT / "frontend" / "setup_alerts.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "trader_view.css").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
SETUP_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "setups").glob("*.py")) + ["api/routes/setups.py", "database/setup_state_migrations.py"]
TRADE_WORDS = re.compile(r"(?i)\b(buy|sell|recommend\w*|enter now|trade now|short it|go long|ready to buy)\b")
AFFIRMATIVE_ENTRY = re.compile(r"(?i)(?<!not an )(?<!not a )entry signal")        # "not an entry signal" is the one allowed (negated) use


def strip_code(src: str) -> str:
    return re.sub(r"#.*|\"\"\"[\s\S]*?\"\"\"", "", src)


def source_of(files):
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in files)


# ---------------------------------------------------------------- builders ------------------------------------------------------

BASE_INPUTS = dict(stretch_status="AVAILABLE", ppe_status="AVAILABLE", ppe_coverage=1.0, ppe_computed_at="2026-10-21T14:59:30+00:00", confidence_status="AVAILABLE",
                   demand_status="AVAILABLE", demand_coverage=0.85, demand_mode="INTRADAY_MODE", demand_computed_at="2026-10-21T14:59:40+00:00", evidence_available=True,
                   evidence_packet_id="p" * 32, evidence_created_at="2026-10-10T00:00:00+00:00", evidence_age_days=11, evidence_cause="MIXED", evidence_strength="WEAK",
                   company_confirm="PARTIAL", quote_ts=int(IN_SESSION.timestamp()) - 5, quote_status="OK", market_data_source="test", mode="test", session_hint="REGULAR")


def fx(symbol="SNDK", **over):
    return SetupInputs(symbol=symbol, evaluated_at=IN_SESSION.isoformat(timespec="seconds"), **{**BASE_INPUTS, **over})


def ep(state="WATCH", **over):
    base = dict(setup_id="a" * 32, symbol="SNDK", state=state, started_at="2026-10-21T14:00:00+00:00", last_transition_at="2026-10-21T14:00:00+00:00",
                last_evaluated_at="2026-10-21T14:59:00+00:00", transition_reason="x", evidence_packet_id_at_start="p" * 32)
    return EpisodeRecord(**{**base, **over})


WATCH_IN = dict(stretch=-67.0, confidence=34, demand=41)
READY_IN = dict(stretch=-54.0, confidence=58, demand=72)


class FakeClock:
    def __init__(self, start=IN_SESSION):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


def make_stream(tmp_path, clock=None, **kw):
    holder = {}
    clock = clock or FakeClock()
    market = kw.pop("market", None) or SimulatedMarket(seed=11, start_time=T0)
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(tmp_path / "setups.db"), clock=clock, cooldown_minutes=15)
    s = TerminalStream(market, kw.pop("positions", None) or FakePositions(), kw.pop("research", None) or FakeResearch(), watchlist_fn=lambda: list(WL),
                       tick_seconds=1.0, allow_manual_tick=True, run_loop=False, now_fn=lambda: T0, setup_service=svc, **kw)
    holder["s"] = s
    return s, svc, clock


# ---------------------------------------------------------------- T transition engine ------------------------------------------

def test_T01_none_to_watch_on_downside_stretch():
    d = evaluate(None, fx(**WATCH_IN))
    assert (d.decision, d.from_state, d.to_state, d.reason_code, d.episode) == ("TRANSITION", "NONE", "WATCH", S.R_STRETCH_DETECTED, "NEW")
    assert "CONFIDENCE_BELOW_40" in d.blockers and "DEMAND_BELOW_60" in d.blockers and d.reason_text == "Downside dislocation detected."
    assert evaluate(None, fx(stretch=-40.0, confidence=34, demand=41)).to_state == "WATCH"                  # boundary: -40 enters


def test_T02_no_watch_when_stretch_is_not_sufficient():
    for s in (-39.9, -20.0, 0.0, 58.7):
        d = evaluate(None, fx(stretch=s, confidence=90, demand=95))
        assert (d.decision, d.to_state, d.episode) == ("HOLD", "NONE", "NONE") and d.reason_code == S.R_NO_SETUP, s


def test_T03_watch_remains_with_weak_demand():
    d = evaluate(ep("WATCH"), fx(stretch=-55.0, confidence=70, demand=41))
    assert (d.decision, d.to_state, d.reason_code) == ("HOLD", "WATCH", "DEMAND_BELOW_60")
    d2 = evaluate(ep("WATCH"), fx(stretch=-55.0, confidence=70, demand=None, demand_status="INSUFFICIENT_DATA"))
    assert (d2.decision, d2.to_state, d2.reason_code) == ("HOLD", "WATCH", "DEMAND_UNAVAILABLE")              # WATCH is early observation


def test_T04_watch_to_ready_with_thresholds_met():
    d = evaluate(ep("WATCH"), fx(**READY_IN))
    assert (d.decision, d.to_state, d.reason_code, d.blockers) == ("TRANSITION", "READY", S.R_CONFIRMED, [])
    assert evaluate(ep("WATCH"), fx(stretch=-40.0, confidence=40, demand=60, demand_coverage=0.60, ppe_coverage=0.60)).to_state == "READY"   # all boundaries inclusive
    assert evaluate(ep("WATCH"), fx(stretch=-40.0, confidence=39, demand=60)).to_state == "WATCH"
    assert evaluate(ep("WATCH"), fx(stretch=-40.0, confidence=40, demand=59)).to_state == "WATCH"
    assert evaluate(ep("WATCH"), fx(stretch=-40.0, confidence=40, demand=60, demand_coverage=0.59)).to_state == "WATCH"


def test_T05_evidence_blocker_prevents_ready_but_watch_remains():
    cases = {"READY_BLOCKED_BY_EVIDENCE": dict(evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="MODERATE"),
             "READY_BLOCKED_BY_EVIDENCE ": dict(evidence_cause="MIXED", guidance_down=1),
             "READY_BLOCKED_EVIDENCE_STALE": dict(evidence_age_days=31),
             "READY_BLOCKED_NO_EVIDENCE": dict(evidence_available=False, evidence_created_at=None, evidence_age_days=None, evidence_cause=None, evidence_strength=None)}
    for code, over in cases.items():
        d = evaluate(ep("WATCH"), fx(**READY_IN, **over))
        assert (d.decision, d.to_state) == ("HOLD", "WATCH") and d.reason_code == code.strip() and code.strip() in d.blockers, code
    assert evidence_gate(fx(evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="WEAK")) is None            # weak company evidence does not block
    assert evidence_gate(fx(evidence_age_days=30)) is None                                                               # 30 days is still fresh enough


def test_T06_ready_to_watch_when_confirmation_weakens_without_invalidation():
    d = evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=44))
    assert (d.decision, d.to_state, d.reason_code) == ("TRANSITION", "READY", S.R_HOLDS) or (d.decision, d.to_state, d.reason_code) == ("TRANSITION", "WATCH", S.R_WEAKENED)
    assert (d.to_state, d.reason_code) == ("WATCH", S.R_WEAKENED)
    assert evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=45)).to_state == "READY"                      # 45 stays (exit needs < 45)
    d2 = evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=None, demand_status="NOT_AVAILABLE"))
    assert (d2.to_state, d2.reason_code) == ("WATCH", S.R_DEMAND_UNAVAILABLE)
    d3 = evaluate(ep("READY"), fx(**READY_IN, evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="MODERATE"))
    assert (d3.to_state, d3.reason_code) == ("WATCH", S.R_BLOCK_EVIDENCE)                                              # a blocker after confirmation: back to WATCH


def test_T07_ready_to_invalidated_on_the_demand_collapse_rule():
    first = evaluate(ep("READY", demand_low_streak=0), fx(stretch=-54.0, confidence=58, demand=30))
    assert (first.decision, first.to_state, first.reason_code, first.demand_low_streak) == ("HOLD", "READY", S.R_DEMAND_COLLAPSE_PENDING, 1)
    second = evaluate(ep("READY", demand_low_streak=1), fx(stretch=-54.0, confidence=58, demand=30))
    assert (second.decision, second.to_state, second.reason_code) == ("TRANSITION", "INVALIDATED", S.R_DEMAND_COLLAPSED)
    recovered = evaluate(ep("READY", demand_low_streak=1), fx(stretch=-54.0, confidence=58, demand=70))
    assert (recovered.decision, recovered.to_state, recovered.demand_low_streak) == ("HOLD", "READY", 0)              # the streak resets when demand recovers
    assert evaluate(ep("READY", demand_low_streak=1), fx(stretch=-54.0, confidence=58, demand=35)).to_state == "WATCH"   # 35 does not count as a collapse


def test_T08_watch_to_invalidated_on_evidence_contradiction():
    new_packet = dict(evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="STRONG", evidence_created_at="2026-10-21T14:30:00+00:00", evidence_packet_id="q" * 32)
    d = evaluate(ep("WATCH"), fx(stretch=-60.0, confidence=30, demand=40, **new_packet))
    assert (d.decision, d.to_state, d.reason_code) == ("TRANSITION", "INVALIDATED", S.R_EVIDENCE_CONTRADICTION)
    assert evaluate(ep("READY"), fx(**READY_IN, **new_packet)).to_state == "INVALIDATED"
    old_packet = {**new_packet, "evidence_created_at": "2026-10-01T00:00:00+00:00"}
    d2 = evaluate(ep("WATCH"), fx(**READY_IN, **old_packet))
    assert (d2.decision, d2.to_state, d2.reason_code) == ("HOLD", "WATCH", S.R_BLOCK_EVIDENCE)                        # an older packet only blocks READY
    assert not evidence_contradiction(None, fx(**new_packet)) and not evidence_contradiction(ep("WATCH"), fx(**{**new_packet, "evidence_strength": "MODERATE"}))


def test_T09_normalised_setup_closes_or_invalidates_per_rule():
    d = evaluate(ep("WATCH"), fx(stretch=-29.9, confidence=10, demand=10))
    assert (d.decision, d.to_state, d.reason_code, d.episode) == ("TRANSITION", "NONE", S.R_STRETCH_FADED, "CLOSE")
    assert evaluate(ep("WATCH"), fx(stretch=-30.0, confidence=10, demand=10)).to_state == "WATCH"                      # -30 is still WATCH
    r = evaluate(ep("READY"), fx(stretch=-19.9, confidence=58, demand=72))
    assert (r.decision, r.to_state, r.reason_code) == ("TRANSITION", "INVALIDATED", S.R_STRETCH_NORMALIZED)
    assert evaluate(ep("READY"), fx(stretch=-20.0, confidence=58, demand=72)).to_state == "READY"                      # -20 is still READY
    c = evaluate(ep("INVALIDATED"), fx(stretch=-29.0, confidence=58, demand=72))
    assert (c.decision, c.to_state, c.reason_code, c.episode) == ("TRANSITION", "NONE", S.R_EPISODE_CLOSED, "CLOSE")
    assert evaluate(ep("INVALIDATED"), fx(stretch=-45.0, confidence=58, demand=90)).to_state == "INVALIDATED"          # never WATCH / READY again inside the same displacement


def test_T10_stale_data_pauses_rather_than_invalidates():
    for state in STATES:
        prev = None if state == "NONE" else ep(state)
        d = evaluate(prev, fx(**READY_IN, ppe_fresh=False, stale_reasons=["PPE_STALE"]))
        assert (d.decision, d.from_state, d.to_state, d.reason_code) == ("PAUSED", state, state, S.R_PAUSED_STALE), state
    d = evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=72, demand_fresh=False, stale_reasons=["DEMAND_STALE"]))
    assert (d.decision, d.to_state, d.reason_code) == ("TRANSITION", "WATCH", S.R_DEMAND_UNAVAILABLE)                 # stale demand: cannot stay confirmed, not invalidated


def test_T11_missing_demand_prevents_ready():
    for over in (dict(demand=None, demand_status="NOT_AVAILABLE"), dict(demand=None, demand_status="INSUFFICIENT_DATA", demand_coverage=0.4),
                 dict(demand=80, demand_status="AVAILABLE", demand_coverage=0.45)):
        d = evaluate(ep("WATCH"), fx(stretch=-54.0, confidence=58, **over))
        assert (d.decision, d.to_state) == ("HOLD", "WATCH") and "DEMAND_UNAVAILABLE" in d.blockers, over


def test_T12_missing_ppe_prevents_evaluation():
    for over in (dict(stretch=None, stretch_status="NOT_AVAILABLE", ppe_status="NOT_AVAILABLE"), dict(stretch=None, stretch_status="INSUFFICIENT_DATA", ppe_status="INSUFFICIENT_DATA"),
                 dict(stretch=-50.0, stretch_status="AVAILABLE", ppe_status="NOT_AVAILABLE")):
        for prev in (None, ep("WATCH"), ep("READY", demand_low_streak=1)):
            d = evaluate(prev, fx(confidence=58, demand=72, **over))
            assert d.decision == "PAUSED" and d.reason_code == S.R_PAUSED_PPE and d.to_state == (prev.state if prev else "NONE")
            assert d.demand_low_streak == (prev.demand_low_streak if prev else 0)                                         # counters frozen while paused


def test_T13_hysteresis_prevents_flapping():
    # WATCH entered at -40 survives a bounce to -35 (exit needs > -30); READY survives demand 50 (exit needs < 45)
    assert evaluate(ep("WATCH"), fx(stretch=-35.0, confidence=10, demand=10)).to_state == "WATCH"
    assert evaluate(ep("WATCH"), fx(stretch=-30.5, confidence=10, demand=10)).to_state == "WATCH"
    assert evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=50)).to_state == "READY"
    assert evaluate(ep("READY"), fx(stretch=-54.0, confidence=10, demand=72)).to_state == "READY"                      # confidence is an entry condition, not an exit one
    # an invalidated episode cannot re-arm inside the same displacement
    assert evaluate(ep("INVALIDATED"), fx(**READY_IN)).to_state == "INVALIDATED"
    seq = [(-41, "WATCH"), (-35, "WATCH"), (-31, "WATCH"), (-29, "NONE")]
    state, prev = "NONE", None
    for s, want in seq:
        d = evaluate(prev, fx(stretch=float(s), confidence=10, demand=10))
        state = d.to_state
        prev = None if state == "NONE" else ep(state)
        assert state == want, (s, state)


def test_T14_exact_boundaries():
    assert evaluate(None, fx(stretch=-40.0, confidence=0, demand=0)).to_state == "WATCH" and evaluate(None, fx(stretch=-39.99, confidence=0, demand=0)).to_state == "NONE"
    assert evaluate(ep("WATCH"), fx(stretch=-30.0, confidence=0, demand=0)).to_state == "WATCH" and evaluate(ep("WATCH"), fx(stretch=-29.99, confidence=0, demand=0)).to_state == "NONE"
    assert evaluate(ep("WATCH"), fx(stretch=-40.0, confidence=40, demand=60)).to_state == "READY"
    assert evaluate(ep("READY"), fx(stretch=-20.0, confidence=58, demand=72)).to_state == "READY" and evaluate(ep("READY"), fx(stretch=-19.99, confidence=58, demand=72)).to_state == "INVALIDATED"
    assert evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=45)).to_state == "READY" and evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=44)).to_state == "WATCH"
    assert evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=35)).to_state == "WATCH" and evaluate(ep("READY"), fx(stretch=-54.0, confidence=58, demand=34)).reason_code == S.R_DEMAND_COLLAPSE_PENDING
    assert evaluate(ep("INVALIDATED"), fx(stretch=-30.0, confidence=58, demand=72)).to_state == "INVALIDATED" and evaluate(ep("INVALIDATED"), fx(stretch=-29.99, confidence=58, demand=72)).to_state == "NONE"
    assert evaluate(ep("WATCH"), fx(**READY_IN, evidence_age_days=30)).to_state == "READY" and evaluate(ep("WATCH"), fx(**READY_IN, evidence_age_days=31)).to_state == "WATCH"


def test_T15_deterministic_repeatability_and_pure_function():
    for prev in (None, ep("WATCH"), ep("READY", demand_low_streak=1), ep("INVALIDATED")):
        for over in (WATCH_IN, READY_IN, dict(stretch=-10.0, confidence=5, demand=5), dict(stretch=-54.0, confidence=58, demand=30)):
            a, b = evaluate(prev, fx(**over)), evaluate(prev, fx(**over))
            assert a == b and a.model_dump() == b.model_dump()
    for from_state, to_state in (("NONE", "WATCH"), ("WATCH", "READY"), ("READY", "WATCH"), ("WATCH", "INVALIDATED"), ("READY", "INVALIDATED")):
        assert alerts_for(from_state, to_state)
    assert not alerts_for("WATCH", "NONE") and not alerts_for("INVALIDATED", "NONE") and not alerts_for("READY", "READY")


# ---------------------------------------------------------------- P persistence / restart -------------------------------------

def test_P01_active_setup_persists_a_restart_with_no_duplicate_alert(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    w = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    assert (w.state, w.decision, w.state_label) == ("WATCH", "TRANSITION", "WATCH") and len(svc.store.alerts()) == 1
    # "restart": a brand-new service + store object on the same database file
    s2, svc2, clock2 = make_stream(tmp_path, clock=FakeClock(IN_SESSION + timedelta(minutes=3)))
    cur = svc2.current("SNDK")
    assert cur.state == "WATCH" and cur.setup_id == w.setup_id and cur.since == w.since and cur.started_at == w.started_at and cur.cycle_id == 1
    again = svc2.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    assert again.decision == "HOLD" and again.setup_id == w.setup_id and len(svc2.store.alerts()) == 1                 # no duplicate WATCH alert
    r = svc2.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    assert r.state == "READY" and r.state_label == "READY FOR REVIEW" and r.setup_id == w.setup_id
    alerts = svc2.store.alerts()
    assert len(alerts) == 2 and [a["to_state"] for a in alerts] == ["READY", "WATCH"] and alerts[0]["title"] == "SNDK — READY FOR REVIEW"   # exactly one READY alert
    s3, svc3, _ = make_stream(tmp_path, clock=FakeClock(IN_SESSION + timedelta(minutes=6)))
    assert svc3.current("SNDK").state == "READY" and svc3.current("SNDK").setup_id == w.setup_id


def test_P02_setup_id_is_stable_through_the_episode_and_new_per_cycle(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    w = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    ids = {w.setup_id}
    for over in (READY_IN, dict(stretch=-54.0, confidence=58, demand=44), READY_IN, dict(stretch=-19.0, confidence=58, demand=72)):
        clock.advance(60)
        ids.add(svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**over)).setup_id)
    assert ids == {w.setup_id} and svc.current("SNDK").state == "INVALIDATED"
    clock.advance(60)
    closed = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-10.0, confidence=58, demand=72))
    assert closed.state == "NONE" and closed.setup_id is None and svc.store.open_episode("SNDK") is None
    clock.advance(60)
    w2 = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    assert w2.state == "WATCH" and w2.cycle_id == 2 and w2.setup_id != w.setup_id and len(svc.store.episodes("SNDK")) == 2
    assert setup_id_for("SNDK", S.SETUP_TYPE, "2026-10-21T15:00:00+00:00", 1) == setup_id_for("SNDK", S.SETUP_TYPE, "2026-10-21T15:00:00+00:00", 1)
    assert setup_id_for("SNDK", S.SETUP_TYPE, "2026-10-21T15:00:00+00:00", 1) != setup_id_for("SNDK", S.SETUP_TYPE, "2026-10-21T15:00:00+00:00", 2)


def test_P03_transitions_and_evaluations_append_and_are_immutable(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    tr = svc.store.transitions("SNDK")
    ev = svc.store.evaluations("SNDK")
    assert [(t["from_state"], t["to_state"]) for t in tr] == [("WATCH", "READY"), ("NONE", "WATCH")] and [e["decision"] for e in ev] == ["TRANSITION", "HOLD", "TRANSITION"]
    assert all(json.loads(t["inputs_json"])["stretch"] is not None and json.loads(t["freshness_json"])["ppe_fresh"] for t in tr)
    conn = sqlite3.connect(str(tmp_path / "setups.db"))
    for sql in ("UPDATE setup_transitions SET to_state = 'READY'", "DELETE FROM setup_transitions", "UPDATE setup_evaluations SET decision = 'HOLD'", "DELETE FROM setup_evaluations",
                "DELETE FROM setup_episodes", "UPDATE setup_episodes SET symbol = 'X'", "DELETE FROM setup_alerts", "UPDATE setup_alerts SET body = 'x'", "DELETE FROM setup_controls"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.close()


def test_P04_alerts_append_only_read_state_changes(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    a = svc.store.alerts()[0]
    assert a["read_at"] is None and svc.store.unread() == 1 and svc.store.mark_read(a["alert_id"]) == 1 and svc.store.unread() == 0
    assert svc.store.mark_read(a["alert_id"]) == 0 and svc.store.alerts()[0]["read_at"] is not None                   # idempotent
    assert svc.store.alert(a["alert_id"])["transition_id"] == svc.store.transitions("SNDK")[0]["transition_id"]


def test_P05_P06_duplicate_evaluation_duplicates_nothing(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    for _ in range(5):
        clock.advance(60)
        v = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
        assert v.decision == "HOLD"
    assert len(svc.store.transitions("SNDK")) == 1 and len(svc.store.alerts()) == 1 and len(svc.store.evaluations("SNDK")) == 6


def test_P07_new_transition_creates_exactly_one_alert_and_bypasses_cooldown(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))                      # 15:00 WATCH
    clock.advance(4 * 60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))                      # 15:04 READY must notify
    assert [a["to_state"] for a in svc.store.alerts()] == ["READY", "WATCH"]
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-54.0, confidence=58, demand=44))   # READY -> WATCH (different reason) notifies
    assert [a["to_state"] for a in svc.store.alerts()] == ["WATCH", "READY", "WATCH"] and svc.store.alerts()[0]["reason_code"] == S.R_WEAKENED


def test_P08_cooldown_suppresses_identical_alerts_inside_one_episode_but_records_the_transition(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))                                # NONE -> WATCH (alert)
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))                                # WATCH -> READY (alert)
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-54.0, confidence=58, demand=44))   # READY -> WATCH (alert: a different key)
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))                                # WATCH -> READY again, same episode, 2 min after the first READY alert: suppressed
    tr = svc.store.transitions("SNDK")
    assert [(t["from_state"], t["to_state"]) for t in tr] == [("WATCH", "READY"), ("READY", "WATCH"), ("WATCH", "READY"), ("NONE", "WATCH")]
    assert tr[0]["alert_id"] is None and tr[0]["alert_suppressed_reason"] == f"COOLDOWN:{tr[2]['alert_id']}" and len(svc.store.alerts()) == 3
    assert svc.current("SNDK").state == "READY" and svc.current("SNDK").setup_id == tr[3]["setup_id"]                  # the state moved anyway; one episode throughout
    clock.advance(16 * 60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-54.0, confidence=58, demand=44))   # READY -> WATCH (alert; its own key is 17 min old)
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))                                # after the window: READY alerts again
    assert [a["to_state"] for a in svc.store.alerts()] == ["READY", "WATCH", "WATCH", "READY", "WATCH"]


def test_P08b_new_episode_inside_the_cooldown_window_is_a_new_transition_and_alerts(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))                                # cycle 1: NONE -> WATCH (alert)
    sid1 = svc.current("SNDK").setup_id
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-25.0, confidence=10, demand=10))   # WATCH -> NONE: episode closed, no alert
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))                                # cycle 2 two minutes later: a new episode is a new transition -> alerts
    v = svc.current("SNDK")
    assert (v.state, v.cycle_id) == ("WATCH", 2) and v.setup_id != sid1
    al = svc.store.alerts()
    assert [a["to_state"] for a in al] == ["WATCH", "WATCH"] and (al[0]["setup_id"], al[1]["setup_id"]) == (v.setup_id, sid1) and al[0]["cooldown_key"] != al[1]["cooldown_key"]
    assert svc.store.transitions("SNDK")[0]["alert_id"] == al[0]["alert_id"] and svc.store.transitions("SNDK")[0]["alert_suppressed_reason"] is None


def test_P09_invalidation_reason_persists_and_timestamps_serialize_stably(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    clock.advance(60)
    inv = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-19.0, confidence=58, demand=72))
    assert inv.state == "INVALIDATED" and inv.invalidation_reason == S.R_STRETCH_NORMALIZED
    s2, svc2, _ = make_stream(tmp_path, clock=FakeClock(IN_SESSION + timedelta(hours=1)))
    cur = svc2.current("SNDK")
    assert cur.state == "INVALIDATED" and cur.invalidation_reason == S.R_STRETCH_NORMALIZED and cur.state_label == "INVALIDATED"
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
    for row in svc2.store.transitions("SNDK") + svc2.store.evaluations("SNDK") + svc2.store.alerts():
        for k in ("evaluated_at", "created_at"):
            if k in row:
                assert iso.match(row[k]), (k, row[k])
    assert SetupView.model_validate_json(cur.model_dump_json()) == cur and json.loads(cur.model_dump_json())["since"] == cur.since


def test_P10_controls_answer_when_did_it_last_fire(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    rows = {c["name"]: c for c in svc.store.controls()}
    ev = rows["setup_state_evaluator"]
    assert ev["evaluation_count"] == 2 and ev["transition_count"] == 1 and ev["last_transition_at"] == IN_SESSION.isoformat(timespec="seconds") and ev["last_result"] == "HOLD"
    assert ev["last_evaluated_at"] == (IN_SESSION + timedelta(seconds=60)).isoformat(timespec="seconds") and ev["error_count"] == 0
    assert rows["alert_persistor"]["run_count"] == 1 and rows["alert_persistor"]["last_result"] == "ALERT_STORED"
    assert set(S.CONTROLS) == {"setup_state_evaluator", "alert_persistor", "market_hours_scheduler"}


def test_P11_a_database_failure_is_reported_not_pretended(tmp_path, monkeypatch):
    s, svc, clock = make_stream(tmp_path)
    events = []
    svc.listeners.append(lambda e: events.append(e["type"]))

    def boom(**kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(svc.store, "record", boom)
    with pytest.raises(sqlite3.OperationalError):
        svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    assert events == [] and svc.current("SNDK").state == "NONE"                                                        # nothing announced, nothing persisted
    assert svc.store.evaluations("SNDK")[0]["decision"] == "ERROR" and "OperationalError" in svc.store.evaluations("SNDK")[0]["reason_text"]
    assert {c["name"]: c for c in svc.store.controls()}["setup_state_evaluator"]["error_count"] == 1


# ---------------------------------------------------------------- S scheduler (fake clock; never sleeps) ------------------------

class FakeService:
    def __init__(self, fail=False):
        self.runs, self.fail = [], fail
        self.store = SetupStoreStub()

    def evaluate_all(self, now, trigger):
        if self.fail:
            raise RuntimeError("down")
        self.runs.append((now, trigger))
        return {"at": now.isoformat(), "results": {"NVDA": "HOLD:NONE"}, "errors": {}}


class SetupStoreStub:
    def __init__(self):
        self.touches = []

    def touch_control(self, name, now, **kw):
        self.touches.append((name, kw.get("result")))


def test_S01_market_hours_evaluation_runs_every_interval():
    svc = FakeService()
    sch = SetupScheduler(lambda: svc, clock=lambda: IN_SESSION, interval_s=60, market_hours_only=True)
    assert sch.step(IN_SESSION) is not None and len(svc.runs) == 1 and svc.runs[0][1] == "SCHEDULED"
    assert sch.step(IN_SESSION + timedelta(seconds=30)) is None and len(svc.runs) == 1                                # not due yet
    assert sch.step(IN_SESSION + timedelta(seconds=60)) is not None and len(svc.runs) == 2
    assert 1.0 <= sch.wait_seconds(IN_SESSION + timedelta(seconds=61)) <= 60.0 and svc.store.touches[-1][0] == "market_hours_scheduler"


def test_S02_outside_hours_nothing_runs_and_the_loop_sleeps_until_the_open():
    svc = FakeService()
    sch = SetupScheduler(lambda: svc, clock=lambda: OUT_SESSION, interval_s=60, market_hours_only=True)
    for t in (OUT_SESSION, OUT_SESSION + timedelta(minutes=5), WEEKEND, datetime(2026, 10, 21, 13, 29, tzinfo=UTC), datetime(2026, 10, 21, 20, 0, tzinfo=UTC)):
        assert sch.step(t) is None, t
    assert svc.runs == [] and sch.skipped_outside_hours == 5
    assert sch.wait_seconds(OUT_SESSION) == 900.0 and sch.wait_seconds(datetime(2026, 10, 21, 13, 29, tzinfo=UTC)) == 60.0     # re-reads the clock at most every 15 min
    assert not is_regular_session(WEEKEND) and is_regular_session(IN_SESSION) and not is_regular_session(datetime(2026, 10, 21, 20, 0, tzinfo=UTC))
    assert next_session_open(WEEKEND) == datetime(2026, 10, 26, 13, 30, tzinfo=UTC) and seconds_until_open(datetime(2026, 10, 21, 13, 29, 30, tzinfo=UTC)) == 30.0
    assert describe(IN_SESSION)["holiday_calendar"].startswith("NONE")
    assert sch.market_hours_only and SetupScheduler(lambda: svc, market_hours_only=False).step(OUT_SESSION) is not None   # the documented opt-out evaluates


def test_S03_one_symbol_failure_does_not_stop_the_others(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    good = s.ppe_result

    def ppe_result(symbol):
        if symbol == "AMD":
            raise RuntimeError("ppe down")
        return good(symbol)
    s.ppe_result = ppe_result
    out = svc.evaluate_all(IN_SESSION, "SCHEDULED", symbols=["NVDA", "AMD", "SNDK"])
    assert set(out["results"]) == {"NVDA", "SNDK"} and out["errors"] == {"AMD": "RuntimeError"}
    assert svc.store.evaluations("AMD")[0]["decision"] == "ERROR" and svc.store.evaluations("NVDA")[0]["decision"] in ("HOLD", "TRANSITION")
    assert {c["name"]: c for c in svc.store.controls()}["setup_state_evaluator"]["last_result"].startswith("RUN:2 ok, 1 errors")


def test_S04_no_duplicate_workers_and_lease_respected():
    svc = FakeService()
    sch = SetupScheduler(lambda: svc, clock=lambda: OUT_SESSION, interval_s=60, startup_delay_s=0.01)
    assert sch.start() is True and sch.start() is False and sch.running()                                               # exactly one thread
    assert sum(1 for t in threading.enumerate() if t.name == "setup-state-evaluator") == 1
    assert sch.stop() is True and not sch.running()
    assert sch.start() is True and sch.stop() is True                                                                    # restartable, still one thread

    class Lease:
        def __init__(self, ok):
            self.ok, self.calls = ok, []

        def acquire(self, owner, now, ttl_min, name):
            self.calls.append(("acquire", name))
            return self.ok

        def release(self, owner, name):
            self.calls.append(("release", name))
    held = Lease(False)
    sch2 = SetupScheduler(lambda: svc, clock=lambda: IN_SESSION, interval_s=60, lease_store_fn=lambda: held)
    assert sch2.step(IN_SESSION) == {"result": "LEASE_HELD"} and svc.runs == [] and held.calls == [("acquire", "setup_evaluator"), ("release", "setup_evaluator")]
    free = Lease(True)
    sch3 = SetupScheduler(lambda: svc, clock=lambda: IN_SESSION, interval_s=60, lease_store_fn=lambda: free)
    assert sch3.step(IN_SESSION) is not None and len(svc.runs) == 1 and free.calls[-1] == ("release", "setup_evaluator")


def test_S05_restart_does_not_create_duplicate_loops_and_tests_never_start_one(monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    assert "test" in TEST_ENVS and start_if_enabled() is False
    monkeypatch.setenv("STOCK_AGENT_ENV", "browser_test")
    assert start_if_enabled() is False
    monkeypatch.setattr(config, "SETUP_EVALUATOR_ENABLED", False)
    monkeypatch.delenv("STOCK_AGENT_ENV", raising=False)
    assert start_if_enabled() is False
    assert get_scheduler() is get_scheduler()                                                                            # one process singleton
    assert not get_scheduler().running()


def test_S06_a_failing_run_is_reported_and_the_scheduler_continues():
    svc = FakeService(fail=True)
    sch = SetupScheduler(lambda: svc, clock=lambda: IN_SESSION, interval_s=60)
    out = sch.step(IN_SESSION)
    assert out["result"] == "ERROR" and sch.last_error == "RuntimeError" and sch.runs == 1 and sch.next_at == IN_SESSION + timedelta(seconds=60)
    svc.fail = False
    assert sch.step(IN_SESSION + timedelta(seconds=60))["errors"] == {} and sch.last_error is None
    st = sch.status(IN_SESSION + timedelta(seconds=61))
    assert st["runs"] == 2 and st["market"]["regular_session"] is True and st["running"] is False


def test_S07_manual_evaluation_stays_available_without_the_scheduler(tmp_path):
    s, svc, clock = make_stream(tmp_path)
    assert not get_scheduler().running()
    v = svc.evaluate_symbol("SNDK", trigger="MANUAL", inputs=fx(**WATCH_IN))
    assert v.state == "WATCH" and svc.store.evaluations("SNDK")[0]["trigger"] == "MANUAL"
    out = svc.evaluate_all(OUT_SESSION, "MANUAL", symbols=["NVDA"])                                                      # manual runs do not depend on the session
    assert "NVDA" in out["results"]


# ---------------------------------------------------------------- I the real service path ------------------------------------

@pytest.fixture
def stack(tmp_path, monkeypatch):
    s, svc, clock = make_stream(tmp_path)
    monkeypatch.setitem(RT.STREAM, "instance", s)
    from api.server import app
    return s, svc, clock, TestClient(app)


FORCED = [("NONE", "WATCH", WATCH_IN, S.R_STRETCH_DETECTED), ("WATCH", "READY", READY_IN, S.R_CONFIRMED), ("READY", "WATCH", dict(stretch=-54.0, confidence=58, demand=44), S.R_WEAKENED),
          ("WATCH", "READY", READY_IN, S.R_CONFIRMED), ("READY", "INVALIDATED", dict(stretch=-19.0, confidence=58, demand=72), S.R_STRETCH_NORMALIZED)]


def test_I01_forced_transitions_through_the_service_persist_alert_and_notify(stack):
    s, svc, clock, api = stack
    events = []
    svc.listeners.append(lambda e: events.append((e["type"], e.get("state") or e.get("to_state"))))
    for from_state, to_state, over, reason in FORCED:
        clock.advance(16 * 60)                                                                                             # past the 15-minute cooldown: every transition alerts
        v = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**over))
        assert (v.decision, v.state, v.reason_code) == ("TRANSITION", to_state, reason), (from_state, to_state)
        t = svc.store.transitions("SNDK")[0]
        assert (t["from_state"], t["to_state"], t["reason_code"]) == (from_state, to_state, reason) and t["alert_id"] is not None
        assert svc.store.alerts()[0]["to_state"] == to_state and svc.store.alerts()[0]["transition_id"] == t["transition_id"]
        assert events[-2:] == [("setup", to_state), ("setup_alert", to_state)]
    assert len(svc.store.alerts()) == 5 and svc.current("SNDK").state == "INVALIDATED"
    assert api.get("/api/setups/SNDK").json()["state"] == "INVALIDATED" and api.get("/api/setups/alerts").json()["unread"] == 5
    assert [(t["from_state"], t["to_state"]) for t in api.get("/api/setups/events?symbol=SNDK").json()["transitions"]][::-1] == [(f, t) for f, t, _, _ in FORCED]


def test_I02_watch_to_invalidated_on_evidence_contradiction_through_the_service(stack):
    s, svc, clock, api = stack
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    v = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-60.0, confidence=30, demand=40, evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="STRONG",
                                                             evidence_created_at=(IN_SESSION + timedelta(seconds=30)).isoformat(timespec="seconds"), evidence_packet_id="q" * 32))
    assert v.state == "INVALIDATED" and v.invalidation_reason == S.R_EVIDENCE_CONTRADICTION and svc.store.alerts()[0]["to_state"] == "INVALIDATED"
    assert "company-specific" in svc.store.alerts()[0]["body"].lower() and not TRADE_WORDS.search(svc.store.alerts()[0]["body"])


def test_I03_ui_receives_symbol_bound_setup_and_broadcast_alert(stack):
    s, svc, clock, api = stack
    with api.websocket_connect("/api/terminal/stream") as ws, api.websocket_connect("/api/terminal/stream") as ws2:
        hello_of(ws)
        hello_of(ws2)
        assert [m["type"] for m in burst(ws, "SNDK")] == ["subscribed", "state", "candles", "quote", "candle", "position", "evidence"]   # the Stage 5.5 burst is unchanged
        burst(ws2, "AAPL")
        ws.send_json({"type": "setup_eval", "symbol": "SNDK", "inputs": {**BASE_INPUTS, **WATCH_IN}})
        got = {m["type"]: m for m in (ws.receive_json(), ws.receive_json())}                                            # the broadcast and the direct reply: order is not guaranteed
        assert set(got) == {"setup", "setup_alert"} and got["setup"]["symbol"] == "SNDK" and got["setup"]["state"] == "WATCH" and got["setup"]["state_label"] == "WATCH"
        assert got["setup_alert"]["to_state"] == "WATCH" and got["setup_alert"]["title"] == "SNDK — WATCH" and "Downside dislocation detected." in got["setup_alert"]["body"]
        other = marker(ws2)
        assert [(m["type"], m.get("symbol")) for m in other] == [("setup_alert", "SNDK")]                                # the alert reaches every tab; the state only its subscriber
        assert not accepts_message({"type": "setup", "symbol": "SNDK"}, "AAPL") and accepts_message({"type": "setup_alert", "symbol": "SNDK"}, "AAPL")
        ws.send_json({"type": "setup_eval", "symbol": "SNDK", "inputs": {**BASE_INPUTS, **WATCH_IN}})               # unchanged inputs: HOLD -> state only, no alert anywhere
        assert [(m["type"], m["decision"]) for m in marker(ws)] == [("setup", "HOLD")] and marker(ws2) == []
        ws.send_json({"type": "refresh"})                                                                               # a refresh re-evaluates the live (simulator) inputs: no stretch -> WATCH closes, never alerted
        got = marker(ws)
        assert [m["type"] for m in got] == ["position", "evidence", "ppe", "demand", "setup"] and got[-1]["state"] == "NONE" and marker(ws2) == []
        ws.send_json({"type": "setup_eval", "symbol": "SNDK", "inputs": {"stretch": "garbage"}})
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
    assert "setup" in SYMBOL_BOUND_TYPES and "setup_alert" in MESSAGE_TYPES and "setup_alert" not in SYMBOL_BOUND_TYPES


def test_I04_forced_evaluation_is_refused_in_production_and_subscribe_evaluates_once(tmp_path, monkeypatch):
    holder = {}
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(tmp_path / "p.db"), clock=FakeClock())
    prod = TerminalStream(SimulatedMarket(seed=1, start_time=T0), NullPositionAdapter(), NullResearchAdapter(), watchlist_fn=lambda: [], run_loop=False, now_fn=lambda: T0, setup_service=svc)
    holder["s"] = prod
    assert prod.allow_manual_tick is False
    monkeypatch.setitem(RT.STREAM, "instance", prod)
    from api.server import app
    api = TestClient(app)
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        ws.send_json({"type": "setup_eval", "symbol": "NVDA", "inputs": {}})
        assert ws.receive_json()["code"] == "NOT_ALLOWED"
        burst(ws, "NVDA")
    ev = svc.store.evaluations("NVDA")
    assert len(ev) == 1 and ev[0]["trigger"] == "SUBSCRIBE" and ev[0]["decision"] in ("HOLD", "TRANSITION")             # opening a ticker evaluates it once
    assert api.get("/api/terminal/state/NVDA?candles=0").json()["setup"]["symbol"] == "NVDA"


def test_I05_restart_preserves_records_and_the_restored_state_is_served(stack, tmp_path):
    s, svc, clock, api = stack
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    sid = svc.current("SNDK").setup_id
    s2, svc2, clock2 = make_stream(tmp_path, clock=FakeClock(IN_SESSION + timedelta(minutes=10)))                      # the restart
    RT.STREAM["instance"] = s2
    from api.server import app
    api2 = TestClient(app)
    j = api2.get("/api/setups/SNDK").json()
    assert (j["state"], j["state_label"], j["setup_id"]) == ("READY", "READY FOR REVIEW", sid)
    assert api2.get("/api/setups").json()["count"] == 1 and api2.get("/api/setups/alerts").json()["unread"] == 2
    assert svc2.startup()["restored"][0]["setup_id"] == sid
    assert {c["name"]: c["last_result"] for c in svc2.store.controls()}["setup_state_evaluator"] == "STARTUP: 1 open episode(s) restored"   # liveness row records the restore
    v = svc2.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    assert v.decision == "HOLD" and api2.get("/api/setups/alerts").json()["unread"] == 2                                 # still exactly one READY alert


def test_I06_evaluate_all_covers_the_watchlist_and_open_episodes(stack):
    s, svc, clock, api = stack
    svc.evaluate_symbol("XOM", trigger="TEST", inputs=fx("XOM", **WATCH_IN))                                             # not on the watchlist, but active
    assert svc.symbols_in_scope() == list(WL) + ["XOM"]
    out = svc.evaluate_all(IN_SESSION, "SCHEDULED")
    assert set(out["results"]) == set(WL) | {"XOM"} and out["errors"] == {}
    assert all(svc.store.evaluations(sym)[0]["trigger"] == "SCHEDULED" for sym in WL)
    assert svc.current("XOM").state in ("WATCH", "NONE", "INVALIDATED")                                                  # evaluated on real simulator inputs now


def test_I07_api_shape_and_read_acknowledgement(stack):
    s, svc, clock, api = stack
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    j = api.get("/api/setups/SNDK").json()
    assert set(j) >= {"version", "symbol", "setup_type", "setup_name", "state", "state_label", "setup_id", "since", "reason_code", "reason_text", "blockers", "inputs", "freshness", "disclaimer"}
    assert j["inputs"]["stretch"] == -67.0 and j["freshness"]["ppe_fresh"] is True and j["disclaimer"] == S.DISCLAIMER
    a = api.get("/api/setups/alerts").json()
    assert a["unread"] == 1 and a["alerts"][0]["to_state_label"] == "WATCH"
    aid = a["alerts"][0]["alert_id"]
    assert api.post(f"/api/setups/alerts/{aid}/read", json={}).json() == {"marked": 1, "unread": 0}
    assert api.post("/api/setups/alerts/zzz/read", json={}).status_code == 404 and api.post("/api/setups/alerts/read-all", json={}).json()["unread"] == 0
    assert api.get("/api/setups/12345").status_code == 400 and api.get("/api/setups/events?symbol=12345").status_code == 400
    c = api.get("/api/setups/controls").json()
    assert {x["name"] for x in c["controls"]} >= {"setup_state_evaluator", "alert_persistor"} and c["scheduler"]["running"] is False
    assert api.get("/api/setups/evaluations?symbol=SNDK").json()["evaluations"][0]["decision"] == "TRANSITION"


def test_I08_stale_inputs_pause_through_the_real_path_in_real_mode(stack):
    s, svc, clock, api = stack
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    clock.advance(60)
    stale = fx(**READY_IN, mode="real", ppe_computed_at=(IN_SESSION - timedelta(hours=1)).isoformat(timespec="seconds"))
    v = svc.evaluate_symbol("SNDK", trigger="TEST", inputs=stale)
    assert (v.decision, v.state, v.reason_code, v.paused) == ("PAUSED", "WATCH", S.R_PAUSED_STALE, True) and "PPE_STALE" in v.freshness["stale_reasons"]
    assert len(svc.store.alerts()) == 1 and svc.current("SNDK").state == "WATCH"                                        # not invalidated, no alert


# ---------------------------------------------------------------- F safety --------------------------------------------------------

def test_F01_no_order_writer_broker_model_or_research_import():
    forbidden = tuple(p for p in FORBIDDEN_IMPORTS if p != "forward") + ("ppe.engine", "ppe.stretch", "demand.engine", "demand.components", "paper", "alpaca", "forward.capture", "forward.journal")
    for rel in SETUP_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == p or name.startswith(p + ".") for p in forbidden), (rel, name)
        for name in names:                                                                                              # the only forward import is the shared lease store
            assert not name.startswith("forward") or name in ("forward.automation", "forward.automation.default_store"), (rel, name)
        assert not re.search(r"TradingClient|TradingStream|messages\.create|get_provider|/v2/orders|alpaca_order_writer|\.submit\(|place_order|cancel_order|requests\.|urlopen|"
                             r"\beval\(|\bexec\(|__import__|os\.system|subprocess|build_packet|get_data_client|anthropic|robin_stocks|rh_gateway", text), rel


def test_F02_no_trading_endpoint_and_read_only_routes(stack):
    s, svc, clock, api = stack
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/setups")}
    assert mine == {"/api/setups": {"get"}, "/api/setups/events": {"get"}, "/api/setups/evaluations": {"get"}, "/api/setups/alerts": {"get"}, "/api/setups/alerts/read-all": {"post"},
                    "/api/setups/alerts/{alert_id}/read": {"post"}, "/api/setups/controls": {"get"}, "/api/setups/{symbol}": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|trade|dismiss-and", p), p
    for path in ("/api/setups", "/api/setups/SNDK", "/api/setups/events", "/api/setups/controls"):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert api.request(method, path, json={"side": "buy", "symbol": "SNDK", "qty": 1}).status_code == 405, (method, path)


def test_F03_no_buy_sell_probability_field_or_wording():
    for model in (SetupInputs, SetupView, SetupAlert, Decision, EpisodeRecord):
        for k in model.model_fields:
            assert not re.search(r"(?i)buy|sell|recommend|probab|expected|target|signal|action|order", k), (model.__name__, k)
    for from_state, to_state, over, reason in FORCED:
        d = Decision(decision="TRANSITION", from_state=from_state, to_state=to_state, reason_code=reason, reason_text=S.REASON_TEXT[reason])
        t = alert_text(d, fx(**over))
        assert not TRADE_WORDS.search(t["title"] + t["body"]) and "probab" not in (t["title"] + t["body"]).lower(), t
    for text in list(S.REASON_TEXT.values()) + list(STATE_LABELS.values()) + [S.DISCLAIMER, S.SETUP_NAME]:
        assert not TRADE_WORDS.search(text), text
    src = strip_code(re.sub(r"DISCLAIMER = \([\s\S]*?\)\n", "", source_of(SETUP_FILES)))                                 # the sanctioned sentence lives in one constant
    assert not TRADE_WORDS.search(src) and not AFFIRMATIVE_ENTRY.search(src)
    assert "probability" not in src.lower() and source_of(SETUP_FILES).count("probability that the stock will rise") == 1


def test_F04_ready_wording_is_ready_for_review_everywhere():
    assert STATE_LABELS["READY"] == "READY FOR REVIEW" and SetupView(symbol="X", state="READY", state_label=STATE_LABELS["READY"]).state_label == "READY FOR REVIEW"
    assert 'READY: "READY FOR REVIEW"' in RENDER and 'READY: "READY FOR REVIEW"' in ALERTS_JS
    for blob in (RENDER, JS, ALERTS_JS, CSS, source_of(SETUP_FILES)):
        assert not re.search(r"(?i)ready to buy", blob)
    for blob in ((ROOT / "README.md").read_text(encoding="utf-8"), (ROOT / "DESIGN_58_SETUP_STATE_ALERTS.md").read_text(encoding="utf-8")):
        for m in re.finditer(r"(?i)ready to buy", blob):                                                                  # documentation may name the phrase only to forbid it
            assert re.search(r"(?i)\b(not|never)\b", blob[max(0, m.start() - 80):m.start()]), blob[max(0, m.start() - 80):m.end()]


def test_F05_prepare_buy_sell_remain_disabled_next_to_the_setup(stack):
    s, svc, clock, api = stack
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))
    st = s.state("SNDK", with_candles=False)
    acts = {a.id: a for a in st.actions}
    assert acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False and st.setup.state in ("WATCH", "READY")
    assert "setup" not in JS.split("function renderActions")[1].split("function renderStatusLine")[0]                  # actions never read the setup state
    assert '{ id: "prepare_buy", label: "Prepare Buy", enabled: false' in JS and 'aria-disabled="${a.enabled ? "false" : "true"}"${a.enabled ? "" : " disabled"}' in JS


def test_F06_alerts_and_transitions_cannot_trigger_execution(stack):
    s, svc, clock, api = stack
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**WATCH_IN))
    a = api.get("/api/setups/alerts").json()["alerts"][0]
    assert set(a) <= {"alert_id", "transition_id", "setup_id", "symbol", "setup_type", "from_state", "to_state", "to_state_label", "reason_code", "title", "body", "cooldown_key", "created_at", "read_at"}
    svc_src = inspect.getsource(SetupService)
    assert "listeners" in svc_src and not re.search(r"(?i)order|broker|execute|preview\(|submit", svc_src)
    assert not re.search(r"(?i)method:\s*[\"'](POST|PUT|PATCH|DELETE)", JS)
    posts = re.findall(r"fetch\(([^,]+), \{ method: \"POST\"", ALERTS_JS)
    assert len(posts) == 1 and "/read" in posts[0] and "read-all" in posts[0]                                            # the only writes are read-acknowledgements
    assert not TRADE_WORDS.search(re.sub(r"//.*", "", ALERTS_JS))


def test_F07_no_real_model_call_and_no_automatic_research(stack, monkeypatch):
    import anthropic
    s, svc, clock, api = stack

    def boom(self, *a, **k):
        raise AssertionError("a model client was constructed by the setup evaluator")
    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    n = len(s.research.calls)
    out = svc.evaluate_all(IN_SESSION, "SCHEDULED", symbols=["NVDA", "SNDK"])
    assert out["errors"] == {} and s.research.calls[n:] == ["NVDA", "SNDK"]                                             # the stored packet is READ once per symbol, never built
    assert api.get("/api/setups").status_code == 200 and api.get("/api/setups/SNDK").status_code == 200
    assert not re.search(r"build_packet|research_flow|run_research|EvidenceStore\(", source_of(SETUP_FILES))


def test_F08_state_evaluation_never_reads_positions_or_pnl(stack):
    s, svc, clock, api = stack
    n = len(s.positions.calls)
    svc.evaluate_all(IN_SESSION, "SCHEDULED", symbols=["NVDA", "SNDK"])
    assert s.positions.calls[n:] == [] and not re.search(r"(?i)position|pnl|p/l|unrealized", strip_code(source_of(["setups/service.py", "setups/engine.py", "setups/models.py"])))


def test_F09_no_24_7_runtime_in_the_scheduler():
    src = inspect.getsource(SetupScheduler)
    assert "is_regular_session" in src and "seconds_until_open" in src and "MAX_SLEEP_S" in src
    assert config.SETUP_MARKET_HOURS_ONLY is True and config.SETUP_EVAL_INTERVAL_SECONDS == 60.0 and config.TRADER_SETUP_REFRESH_SECONDS == 60.0
    sch = SetupScheduler(lambda: FakeService(), clock=lambda: OUT_SESSION, interval_s=60)
    assert sch.step() is None and sch.next_at is None and sch.wait_seconds() >= 60.0


# ---------------------------------------------------------------- U frontend -----------------------------------------------------

def test_U01_node_tests_of_the_setup_markup():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cmd = [node, "--test", "--test-reporter=tap", "--test-reporter-destination=stdout", str(ROOT / "tests" / "frontend" / "trader_setup.test.cjs")]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
    assert r.returncode == 0 and "# fail 0" in r.stdout, r.stdout + r.stderr
    assert int(re.search(r"# pass (\d+)", r.stdout).group(1)) >= 12
    for f in ("trader_core.test.cjs", "trader_render.test.cjs", "trader_demand.test.cjs"):
        rr = subprocess.run(cmd[:-1] + [str(ROOT / "tests" / "frontend" / f)], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120)
        assert rr.returncode == 0 and "# fail 0" in rr.stdout, f


def test_U02_setup_section_wiring_in_the_page():
    assert '"setup"' in CORE.split("SYMBOL_BOUND = new Set(")[1].split(")")[0]
    assert 'case "setup": renderSetup(m); break;' in JS and 'case "setup_alert": onSetupAlert(m); break;' in JS and "renderSetup(d.setup)" in JS and "renderSetup(null)" in JS
    assert JS.index("<h3>PPE</h3>") < JS.index("<h3>Market Demand</h3>") < JS.index("<h3>Setup</h3>") < JS.index("<h3>Evidence</h3>")
    assert '["tv-setup", "SETUP"]' in JS and "seenAlerts.has(a.alert_id)" in JS and "toast(R.setupAlertLine(a))" in JS
    assert "setupHtml" in RENDER and "setupModel" in RENDER and 'data-setup-state="' in RENDER
    assert '<div id="setup-alerts"></div>' in HTML and '<script src="setup_alerts.js"></script>' in HTML and HTML.index("trader_view.js") < HTML.index("setup_alerts.js")
    assert ".tv-set-state.ready" in CSS and ".tv-set-state.watch" in CSS and ".tv-set-state.invalidated" in CSS and ".sa-list" in CSS
    assert min(int(x) for x in re.findall(r"font-size:\s*(\d+)px", CSS)) >= 11


def test_U03_no_buy_wording_in_the_setup_ui():
    section = RENDER.split("Setup state (Stage 5.8")[1].split("status strip")[0]
    assert not TRADE_WORDS.search(re.sub(r"//.*", "", section)) and "READY FOR REVIEW" in section and "not an entry signal" in section
    assert not re.search(r"(?i)probability|expected return|reversal", section)
    alerts = re.sub(r"//.*", "", ALERTS_JS)
    assert not TRADE_WORDS.search(alerts) and "Open in Trader View" in ALERTS_JS and "Mark read" in ALERTS_JS


def test_U04_harness_flow_registered_after_market_demand():
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert names.index("setup_state") == names.index("market_demand") + 1 and names.index("setup_state") < names.index("automation")
    assert names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]
    src = inspect.getsource(F.setup_state)
    assert "READY FOR REVIEW" in src and "evaluate_symbol" in src and 'c.shot("setup_state"' in src


# ---------------------------------------------------------------- B base pins -------------------------------------------------------

def test_B01_ppe_demand_evidence_and_execution_files_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    pinned = [p.relative_to(ROOT).as_posix() for p in (ROOT / "ppe").glob("*.py")] + [p.relative_to(ROOT).as_posix() for p in (ROOT / "demand").glob("*.py")] + \
             ["trader_view/ppe_provider.py", "trader_view/demand_provider.py", "trader_view/adapters.py", "trader_view/hub.py", "trader_view/positions.py", "trader_view/research.py",
              "trader_view/market_real.py", "trader_view/simulated.py", "evidence/engine.py", "evidence/rules.py", "evidence/store.py", "paper/alpaca_order_writer.py",
              "paper/alpaca_orders.py", "paper/alpaca_readonly.py", "paper/execution.py", "portfolio/provider.py", "database/migrations.py", "forward/automation.py",
              "DESIGN_56_PPE_ENGINE.md", "DESIGN_57_MARKET_DEMAND.md", "tests/test_evidence_54.py", "tests/frontend/trader_core.test.cjs", "tests/frontend/trader_demand.test.cjs"]
    for rel in pinned:
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob(rel), f"{rel} differs from {BASE}"
    base_cfg = blob("config.py").decode("utf-8")
    now_cfg = (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert now_cfg.startswith(base_cfg.rstrip("\n")) and "SETUP_EVALUATOR_ENABLED" in now_cfg
