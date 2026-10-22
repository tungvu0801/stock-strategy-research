"""
Stage 5.9 — DETERMINISTIC TRADE PROPOSAL / REVIEW LAYER (review only; READY is READY FOR REVIEW; a proposal is never an order). Focused tests:
  E01–E10 eligibility · Z01–Z13 sizing (pure, Decimal) · C01–C07 risk controls through the REAL service path with persisted control evidence
  P01–P10 persistence / restart / dedup / review state · X01–X09 no execution · U01–U05 frontend · B01 base pins
"""
import ast
import inspect
import json
import math
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import proposals as PR  # noqa: E402
from proposals import engine as E  # noqa: E402
from proposals.account import AccountAdapter  # noqa: E402
from proposals.models import AccountSnapshot, ControlEvaluation, Draft, PriceCandidate, ProposalInputs, ProposalView  # noqa: E402
from proposals.service import ProposalService  # noqa: E402
from proposals.store import ProposalStore, ProposalStoreError  # noqa: E402
from database.proposal_migrations import TABLES, run_proposal_migrations  # noqa: E402
from setups.service import SetupService  # noqa: E402
from setups.store import SetupStore  # noqa: E402
from trader_view import MESSAGE_TYPES, SYMBOL_BOUND_TYPES  # noqa: E402
from trader_view.hub import accepts_message  # noqa: E402
from trader_view.simulated import SimulatedMarket  # noqa: E402
from trader_view.stream import TerminalStream  # noqa: E402
from api.routes import trader_view as RT  # noqa: E402
from test_trader_view_55 import FORBIDDEN_IMPORTS, T0, WL, FakePositions, FakeResearch, burst, hello_of, marker  # noqa: E402
from test_setups_58 import IN_SESSION, READY_IN, WATCH_IN, FakeClock, fx, strip_code  # noqa: E402

BASE = "e714dc7"                                                                       # origin/main the stage was branched from (Stage 5.8 merged)
UTC = timezone.utc
JS = (ROOT / "frontend" / "trader_view.js").read_text(encoding="utf-8")
RENDER = (ROOT / "frontend" / "trader_render.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend" / "trader_core.js").read_text(encoding="utf-8")
REVIEW_JS = (ROOT / "frontend" / "trade_review.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "trader_view.css").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
PROPOSAL_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "proposals").glob("*.py")) + ["api/routes/proposals.py", "database/proposal_migrations.py"]
TRADE_WORDS = re.compile(r"(?i)\b(buy|sell|buy now|sell now|enter now|trade now|short it|go long|ready to buy|you should|approved|execute)\b")
FORECAST = re.compile(r"(?i)target price|price target|take.profit|expected (return|upside|value)|probabilit|win rate|reward/risk|predicted")
ALL_CONTROLS = list(PR.CONTROLS)


def source_of(files):
    return "".join((ROOT / f).read_text(encoding="utf-8") for f in files)


# ---------------------------------------------------------------- builders (pure engine) --------------------------------------------

def bars(n=30, start=100.0, step=0.1, rng=2.0):
    return [{"date": (datetime(2026, 8, 1) + timedelta(days=i)).date().isoformat(), "open": start + i * step, "high": start + i * step + rng / 2, "low": start + i * step - rng / 2,
             "close": start + i * step + 0.3} for i in range(n)]


def setup_inputs(**over):
    return fx(**{**READY_IN, **over}).model_copy(update={"ppe_fresh": True, "demand_fresh": True, "quote_fresh": True})


def ready_view(si=None, **over):
    si = si or setup_inputs()
    base = {"version": "setups_v1", "symbol": "SNDK", "setup_type": PR.SETUP_TYPE, "setup_name": "Downside Dislocation Recovery", "state": "READY", "state_label": "READY FOR REVIEW",
            "setup_id": "a" * 32, "cycle_id": 1, "previous_state": "WATCH", "since": "2026-10-21T15:00:00+00:00", "started_at": "2026-10-21T14:00:00+00:00",
            "last_evaluated_at": "2026-10-21T15:00:30+00:00", "decision": "HOLD", "reason_code": "CONFIRMATION_HOLDS", "reason_text": "Confirmation holds.", "blockers": [],
            "invalidation_reason": None, "paused": False, "inputs": si.model_dump(), "freshness": {}, "source": "test", "mode": "test", "disclaimer": "", "note": None}
    return {**base, **over}


def episode(**over):
    base = {"setup_id": "a" * 32, "symbol": "SNDK", "setup_type": PR.SETUP_TYPE, "cycle_id": 1, "state": "READY", "previous_state": "WATCH", "started_at": "2026-10-21T14:00:00+00:00",
            "last_transition_at": "2026-10-21T15:00:00+00:00", "last_evaluated_at": "2026-10-21T15:00:30+00:00", "closed_at": None, "invalidation_reason": None,
            "transition_reason": "CONFIRMATION_STRENGTHENED", "demand_low_streak": 0, "evidence_packet_id_at_start": "p" * 32}
    return {**base, **over}


def account(**over):
    base = dict(equity="50000.00", equity_source="HYPOTHETICAL_REVIEW", buying_power="50000.00", buying_power_source="HYPOTHETICAL_REVIEW", tradable=None)
    return AccountSnapshot(**{**base, **over})


def candidates(mid=102.5, last=102.4, minute=102.3, daily=101.9, age=1.0):
    return [PriceCandidate(source="QUOTE_MID", value=mid, ts=1.0, age_s=age), PriceCandidate(source="LAST_TRADE", value=last, ts=1.0, age_s=age),
            PriceCandidate(source="MINUTE_CLOSE", value=minute, ts=1.0, age_s=age), PriceCandidate(source="DAILY_CLOSE", value=daily, mode="DAILY_REFERENCE")]


def inputs(**over):
    base = dict(symbol="SNDK", now="2026-10-21T15:01:00+00:00", now_ts=IN_SESSION.timestamp() + 60, mode="test", market_data_source="test", setup_view=ready_view(), episode=episode(),
                price_candidates=candidates(), daily_bars=bars(), intraday_lows=[101.2] * 40, account=account(),
                session={"regular_session": True, "seconds_to_close": 3600, "next_open": "2026-10-22T13:30:00+00:00"}, risk_fraction="0.005", max_position_pct="10", ttl_seconds=300)
    return ProposalInputs(**{**base, **over})


def codes(draft):
    return [b["code"] for b in draft.blockers]


def control(draft, cid):
    return next(c for c in draft.controls if c.control_id == cid)


# ---------------------------------------------------------------- stack (the real path) ----------------------------------------------

def make_stack(tmp_path, clock=None, **kw):
    holder = {}
    clock = clock or FakeClock()
    db = tmp_path / "stage59.db"
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(db), clock=clock, cooldown_minutes=15)
    psvc = ProposalService(stream_fn=lambda: holder["s"], store=ProposalStore(db), account=AccountAdapter(path=db), clock=clock)
    s = TerminalStream(kw.pop("market", None) or SimulatedMarket(seed=11, start_time=T0), kw.pop("positions", None) or FakePositions(), kw.pop("research", None) or FakeResearch(),
                       watchlist_fn=lambda: list(WL), tick_seconds=1.0, allow_manual_tick=True, run_loop=False, now_fn=lambda: T0, setup_service=svc, proposal_service=psvc, **kw)
    holder["s"] = s
    return s, svc, psvc, clock


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRADE_PROPOSAL_REVIEW_EQUITY", 100000.0)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", False)
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", False)
    s, svc, psvc, clock = make_stack(tmp_path)
    monkeypatch.setitem(RT.STREAM, "instance", s)
    from api.server import app
    return s, svc, psvc, clock, TestClient(app)


def go_ready(svc, clock, symbol="SNDK"):
    svc.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **WATCH_IN))
    clock.advance(60)
    return svc.evaluate_symbol(symbol, trigger="TEST", inputs=fx(symbol, **READY_IN))


# ================================================================ E eligibility ===================================================================

def test_E01_ready_setup_is_eligible_and_reviewable():
    d = E.build(inputs())
    assert d.status == "REVIEWABLE" and codes(d) == [] and d.setup_id == "a" * 32 and d.side_context if hasattr(d, "side_context") else True
    assert [c.control_id for c in d.controls] == ALL_CONTROLS and all(c.result in ("PASS", "WARN") for c in d.controls)
    assert d.final_qty and d.final_qty >= 1 and d.reference_price == "102.5000" and d.price_source == "QUOTE_MID" and d.price_mode == "INTRADAY"
    assert E.eligibility(inputs()) == []


def test_E02_watch_is_not_eligible():
    d = E.build(inputs(setup_view=ready_view(state="WATCH", state_label="WATCH")))
    assert (d.status, codes(d), d.controls, d.final_qty, d.reference_price) == ("NOT_ELIGIBLE", [PR.NE_NOT_READY], [], None, None)


def test_E03_none_is_not_eligible():
    d = E.build(inputs(setup_view=ready_view(state="NONE", state_label="NO ACTIVE SETUP", setup_id=None), episode=None))
    assert d.status == "NOT_ELIGIBLE" and codes(d) == [PR.NE_NOT_FOUND]
    d2 = E.build(inputs(setup_view=ready_view(setup_type="SOMETHING_ELSE")))
    assert codes(d2) == [PR.NE_TYPE]


def test_E04_invalidated_is_not_eligible():
    d = E.build(inputs(setup_view=ready_view(state="INVALIDATED", state_label="INVALIDATED", invalidation_reason="DEMAND_COLLAPSED"), episode=episode(state="INVALIDATED")))
    assert d.status == "NOT_ELIGIBLE" and codes(d) == [PR.NE_INVALIDATED]


def test_E05_stale_setup_evaluation_blocks():
    d = E.build(inputs(mode="real", setup_view=ready_view(last_evaluated_at="2026-10-21T14:00:00+00:00")))
    assert d.status == "BLOCKED" and PR.B_SETUP_STALE in codes(d) and control(d, "SETUP_FRESHNESS").result == "BLOCK" and control(d, "SETUP_FRESHNESS").input["age_s"] > 300
    ok = E.build(inputs(mode="real"))
    assert PR.B_SETUP_STALE not in codes(ok) and control(ok, "SETUP_FRESHNESS").result == "PASS"


def test_E06_stale_or_missing_ppe_blocks():
    stale = E.build(inputs(mode="real", setup_view=ready_view(setup_inputs().model_copy(update={"ppe_fresh": False, "stale_reasons": ["PPE_STALE"]}))))
    assert stale.status == "BLOCKED" and PR.B_PPE_STALE in codes(stale) and control(stale, "PPE_FRESHNESS").result == "BLOCK"
    gone = E.build(inputs(setup_view=ready_view(setup_inputs(ppe_status="NOT_AVAILABLE", stretch=None, stretch_status="NOT_AVAILABLE"))))
    assert PR.B_PPE_NA in codes(gone)


def test_E07_stale_or_missing_demand_blocks():
    stale = E.build(inputs(mode="real", setup_view=ready_view(setup_inputs().model_copy(update={"demand_fresh": False, "stale_reasons": ["DEMAND_STALE"]}))))
    assert stale.status == "BLOCKED" and PR.B_DEMAND_STALE in codes(stale) and control(stale, "DEMAND_FRESHNESS").result == "BLOCK"
    gone = E.build(inputs(setup_view=ready_view(setup_inputs(demand_status="INSUFFICIENT_DATA", demand=None))))
    assert PR.B_DEMAND_NA in codes(gone)


def test_E08_evidence_blocker_and_contradiction_block():
    gated = E.build(inputs(setup_view=ready_view(setup_inputs(evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="MODERATE"))))
    assert gated.status == "BLOCKED" and f"{PR.B_EVIDENCE_BLOCKED}:READY_BLOCKED_BY_EVIDENCE" in codes(gated) and control(gated, "EVIDENCE_GATE").input["gate"] == "READY_BLOCKED_BY_EVIDENCE"
    contra = E.build(inputs(setup_view=ready_view(setup_inputs(evidence_cause="COMPANY_SPECIFIC_DOMINANT", evidence_strength="STRONG", evidence_packet_id="q" * 32,
                                                               evidence_created_at="2026-10-21T14:30:00+00:00"))))
    assert PR.B_EVIDENCE_CONTRA in codes(contra)
    none = E.build(inputs(setup_view=ready_view(setup_inputs(evidence_available=False, evidence_packet_id=None, evidence_created_at=None, evidence_age_days=None, evidence_cause=None,
                                                             evidence_strength=None))))
    assert f"{PR.B_EVIDENCE_BLOCKED}:READY_BLOCKED_NO_EVIDENCE" in codes(none)


def test_E09_unavailable_or_stale_price_blocks():
    none = E.build(inputs(price_candidates=[PriceCandidate(source=s, status="UNAVAILABLE", mode="DAILY_REFERENCE" if s == "DAILY_CLOSE" else "INTRADAY") for s in PR.PRICE_SOURCES]))
    assert none.status == "BLOCKED" and PR.B_PRICE_NA in codes(none) and none.reference_price is None
    assert control(none, "INVALIDATION_LEVEL").result == "UNKNOWN" and control(none, "QUANTITY").result == "UNKNOWN"          # dependents are recorded, not skipped
    stale = E.build(inputs(mode="real", price_candidates=[PriceCandidate(source="QUOTE_MID", value=102.5, ts=1.0, age_s=900.0), PriceCandidate(source="LAST_TRADE", value=102.4, ts=1.0, age_s=900.0),
                                                           PriceCandidate(source="MINUTE_CLOSE", value=102.3, ts=1.0, age_s=3000.0), PriceCandidate(source="DAILY_CLOSE", status="UNAVAILABLE", mode="DAILY_REFERENCE")]))
    assert PR.B_PRICE_STALE in codes(stale)
    daily = E.build(inputs(mode="real", price_candidates=[PriceCandidate(source="QUOTE_MID", value=102.5, ts=1.0, age_s=900.0), PriceCandidate(source="LAST_TRADE", status="UNAVAILABLE"),
                                                           PriceCandidate(source="MINUTE_CLOSE", status="UNAVAILABLE"), PriceCandidate(source="DAILY_CLOSE", value=101.9, mode="DAILY_REFERENCE")]))
    assert daily.status == "REVIEWABLE" and daily.price_mode == "DAILY_REFERENCE" and daily.price_source == "DAILY_CLOSE" and PR.W_DAILY_REF in [w["code"] for w in daily.warnings]
    assert daily.expires_at == "2026-10-22T13:30:00+00:00"                                                                   # the next session open


def test_E10_invalid_price_blocks_and_never_falls_through():
    for bad in (0.0, -3.0, float("nan"), float("inf")):
        d = E.build(inputs(price_candidates=[PriceCandidate(source="QUOTE_MID", value=bad, ts=1.0, age_s=1.0)] + candidates()[1:]))
        assert d.status == "BLOCKED" and PR.B_PRICE_INVALID in codes(d) and d.reference_price is None, bad
        assert control(d, "PRICE_REFERENCE").input["candidates"][0]["outcome"] == "invalid" and len(control(d, "PRICE_REFERENCE").input["candidates"]) == 1


# ================================================================ Z sizing ======================================================================

def test_Z01_risk_per_share_is_reference_minus_invalidation():
    d = E.build(inputs())
    assert Decimal(d.risk_per_share) == Decimal(d.reference_price) - Decimal(d.invalidation_price) and Decimal(d.risk_per_share) > 0
    assert Decimal(d.risk_pct) == (Decimal(d.risk_per_share) / Decimal(d.reference_price) * 100).quantize(Decimal("0.01"))
    assert d.stop_rule == "STRUCTURAL" and Decimal(d.invalidation_price) == (Decimal(d.structural_low) - Decimal("0.25") * Decimal(d.atr)).quantize(Decimal("0.01"))


def test_Z02_risk_budget_is_equity_times_fraction():
    d = E.build(inputs(account=account(equity="12345.67"), risk_fraction="0.005"))
    assert d.risk_budget == "61.73" and d.equity == "12345.67" and d.risk_fraction == "0.005"
    d2 = E.build(inputs(account=account(equity="12345.67"), risk_fraction="0.01"))
    assert d2.risk_budget == "123.46"


def test_Z03_risk_based_quantity_is_floor_of_budget_over_risk_per_share():
    d = E.build(inputs(account=account(equity="1000000.00", buying_power="1000000.00")))
    assert d.base_qty == math.floor(Decimal(d.risk_budget) / Decimal(d.risk_per_share)) and d.caps["risk_based"] == d.base_qty


def test_Z04_position_cap_reduces_quantity():
    d = E.build(inputs(account=account(equity="50000.00", buying_power="50000.00"), max_position_pct="1"))
    assert d.position_cap_qty == math.floor(Decimal("500.00") / Decimal(d.reference_price)) and d.final_qty == d.position_cap_qty < d.base_qty
    assert d.caps["binding"] == "position_cap" and PR.W_CAP_REDUCED in [w["code"] for w in d.warnings] and d.max_notional == "500.00" and d.cap_source == "config.MAX_POSITION_PERCENT"
    big = E.build(inputs(account=account(equity="5000000.00", buying_power="5000000.00")))
    assert big.max_notional == "100000.00" and big.cap_source == "4.6B MAX_NOTIONAL"                                           # the frozen 4.6B limit bounds the cap


def test_Z05_buying_power_reduces_quantity():
    d = E.build(inputs(account=account(equity="100000.00", buying_power="300.00")))
    assert d.buying_power_qty == math.floor(Decimal("300.00") / (Decimal(d.reference_price) * Decimal("1.05"))) == 2 and d.final_qty == 2 and d.caps["binding"] == "buying_power"
    assert PR.W_BP_REDUCED in [w["code"] for w in d.warnings]
    unknown = E.build(inputs(account=account(buying_power=None)))
    assert unknown.buying_power_qty is None and unknown.status == "REVIEWABLE" and PR.W_BP_UNKNOWN in [w["code"] for w in unknown.warnings]


def test_Z06_existing_position_reduces_the_available_cap():
    d = E.build(inputs(account=account(equity="50000.00", buying_power="50000.00", existing_qty="20", existing_avg_cost="100.00", existing_notional="2050.00"), max_position_pct="10"))
    assert d.existing_position_qty == "20" and d.existing_position_notional == "2050.00" and d.position_cap_qty == math.floor(Decimal("2950.00") / Decimal(d.reference_price))
    assert PR.W_EXISTING in [w["code"] for w in d.warnings] and control(d, "POSITION_CAP").input["available_notional"] == "2950.00"


def test_Z07_existing_position_over_cap_blocks():
    d = E.build(inputs(account=account(equity="50000.00", existing_qty="60", existing_notional="6150.00"), max_position_pct="10"))
    assert d.status == "BLOCKED" and PR.B_CAP_EXCEEDED in codes(d) and d.final_qty in (None, 0) and control(d, "POSITION_CAP").result == "BLOCK"


def test_Z08_zero_risk_budget_blocks():
    d = E.build(inputs(risk_fraction="0"))
    assert PR.B_FRACTION in codes(d) and d.risk_budget is None
    too_big = E.build(inputs(risk_fraction="0.02"))
    assert PR.B_FRACTION in codes(too_big)                                                                                   # never more than 1 %
    tiny = E.build(inputs(account=account(equity="0.40", buying_power="0.40")))
    assert PR.B_BUDGET_ZERO in codes(tiny) and tiny.risk_budget is None


def test_Z09_invalid_stop_blocks():
    flat = [{**b, "open": 102.0, "high": 102.0015, "low": 101.9985, "close": 102.0} for b in bars()]                           # ATR = 0.003: the stop rounds onto the reference
    d = E.build(inputs(daily_bars=flat, intraday_lows=[]))
    assert d.status == "BLOCKED" and PR.B_INVALID_STOP in codes(d) and d.invalidation_price is None and control(d, "RISK_PER_SHARE").result == "UNKNOWN"
    no_atr = E.build(inputs(daily_bars=bars(10)))
    assert PR.B_ATR_NA in codes(no_atr) and no_atr.atr is None


def test_Z10_quantity_is_never_negative():
    for acc in (account(buying_power="0.00"), account(existing_qty="10000", existing_notional="99999999.00"), account(equity="1.00", buying_power="1.00")):
        d = E.build(inputs(account=acc))
        for q in (d.base_qty, d.position_cap_qty, d.buying_power_qty, d.final_qty):
            assert q is None or q >= 0
        assert d.status == "BLOCKED"
    assert PR.B_BUYING_POWER in codes(E.build(inputs(account=account(buying_power="0.00"))))


def test_Z11_quantity_is_deterministic():
    a, b = E.build(inputs()), E.build(inputs())
    assert a.model_dump() == b.model_dump() and a.fingerprint == b.fingerprint
    assert E.proposal_id_for("a" * 32, "2026-10-21T15:01:00+00:00", a.reference_price, a.invalidation_price, a.risk_budget) == E.proposal_id_for("a" * 32, "2026-10-21T15:01:00+00:00", a.reference_price, a.invalidation_price, a.risk_budget)


def test_Z12_no_nan_or_infinity_anywhere():
    assert E.D(float("nan")) is None and E.D(float("inf")) is None and E.D("Infinity") is None and E.D("NaN") is None and E.D("12.5") == Decimal("12.5")
    d = E.build(inputs(account=account(equity="NaN", buying_power="inf")))
    assert PR.B_EQUITY_NA in codes(d) and d.equity is None
    for v in json.loads(json.dumps(E.build(inputs()).model_dump())).values():
        assert v != "NaN" and v != "Infinity"
    for key in ("reference_price", "invalidation_price", "risk_per_share", "risk_budget", "proposed_notional", "atr"):
        assert Decimal(getattr(E.build(inputs()), key)).is_finite()


def test_Z13_whole_share_rounding_is_deterministic_and_matches_the_existing_sizing_rule():
    from analysis.position_sizing import compute_position_sizing
    d = E.build(inputs(account=account(equity="5000.00", buying_power="5000.00"), risk_fraction="0.005", max_position_pct="10"))
    ref, inval = float(d.reference_price), float(d.invalidation_price)
    ps = compute_position_sizing(5000.0, ref, inval, max_risk_pct=0.5, max_position_pct=10.0)
    assert d.base_qty == ps.max_risk_based_shares and d.position_cap_qty == ps.max_allocation_based_shares and d.final_qty == min(ps.max_risk_based_shares, ps.max_allocation_based_shares)
    assert E.floor_div(Decimal("25.00"), Decimal("6.30")) == 3 and E.floor_div(Decimal("24.99"), Decimal("6.30")) == 3 and E.floor_div(Decimal("25.00"), Decimal("8.34")) == 2
    assert E.floor_div(Decimal("0.10"), Decimal("6.30")) == 0 and isinstance(d.final_qty, int)


# ================================================================ C risk controls (the real service path) ==========================================

def _blocked_through_service(stack, overrides, code, control_id):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    clock.advance(1)
    v = psvc.evaluate("SNDK", trigger="TEST", overrides=overrides)
    assert v.effective_status == "BLOCKED" and code in [b["code"] for b in v.blockers] and v.proposal_id and v.submission_status == "NOT SUBMITTED"
    rows = psvc.store.controls_for(v.proposal_id)
    hit = next(r for r in rows if r["control_id"] == control_id)
    assert hit["result"] == "BLOCK" and hit["evaluated_at"] == v.created_at and hit["input"]                              # what the control saw is on disk
    status = {c["control_id"]: c for c in psvc.store.control_status()}
    assert status[control_id]["last_result"] == "BLOCK" and status[control_id]["block_count"] >= 1 and status[control_id]["last_blocked_at"] == v.created_at and status[control_id]["last_proposal_id"] == v.proposal_id
    j = api.get("/api/proposals/controls").json()
    assert any(c["control_id"] == control_id and c["last_result"] == "BLOCK" for c in j["controls"]) and any(e["control_id"] == control_id and e["result"] == "BLOCK" for e in j["evaluations"])
    assert api.get("/api/proposals/SNDK").json()["effective_status"] == "BLOCKED"
    return v, hit


def test_C01_kill_switch_blocks_and_is_evidenced(stack, monkeypatch):
    v, hit = _blocked_through_service(stack, {"halt": True, "halt_source": "forced halt"}, PR.B_KILL, "KILL_SWITCH")
    assert hit["input"] == {"halt": True, "source": "forced halt"}
    s, svc, psvc, clock, api = stack
    monkeypatch.setattr(config, "TRADE_PROPOSAL_HALT", True)                                                                # the review-only config halt, no overrides
    clock.advance(1)
    v2 = psvc.evaluate("SNDK", trigger="TEST")
    assert PR.B_KILL in [b["code"] for b in v2.blockers] and v2.proposal_id == v.proposal_id                                  # same material inputs: the stored row is reused
    live = {c["control_id"]: c for c in psvc.store.control_status()}["KILL_SWITCH"]
    assert live["last_input"]["source"] == "config.TRADE_PROPOSAL_HALT" and live["block_count"] == 2                        # liveness still records this run


def test_C02_daily_loss_halt_blocks_and_is_evidenced(stack, monkeypatch):
    _blocked_through_service(stack, {"daily_loss_halt": True, "daily_loss_source": "forced"}, PR.B_DAILY_LOSS, "DAILY_LOSS_HALT")
    s, svc, psvc, clock, api = stack
    monkeypatch.setattr(config, "TRADE_PROPOSAL_DAILY_LOSS_HALT", True)
    clock.advance(1)
    assert PR.B_DAILY_LOSS in [b["code"] for b in psvc.evaluate("SNDK", trigger="TEST").blockers]


def test_C03_symbol_not_tradable_blocks_unknown_only_warns(stack):
    v, hit = _blocked_through_service(stack, {"tradable": False, "tradability_source": "forced"}, PR.B_NOT_TRADABLE, "TRADABILITY")
    s, svc, psvc, clock, api = stack
    clock.advance(1)
    u = psvc.evaluate("SNDK", trigger="TEST", overrides={"tradable": None})
    assert u.effective_status == "REVIEWABLE" and PR.W_TRADABILITY in [w["code"] for w in u.warnings] and next(c for c in u.controls if c["control_id"] == "TRADABILITY")["result"] == "WARN"


def test_C04_duplicate_pending_order_blocks(stack):
    v, hit = _blocked_through_service(stack, {"pending_orders": 1, "pending_detail": ["4.6B intent 7 SUBMITTED"], "pending_source": "forced"}, PR.B_DUPLICATE, "DUPLICATE_ORDER")
    assert hit["input"]["detail"] == ["4.6B intent 7 SUBMITTED"] and hit["reason"] == "4.6B intent 7 SUBMITTED"


def test_C05_buying_power_too_low_blocks(stack):
    v, hit = _blocked_through_service(stack, {"buying_power": "10.00", "buying_power_source": "forced"}, PR.B_BUYING_POWER, "BUYING_POWER")
    assert hit["input"]["buying_power_qty"] == 0 and hit["input"]["buffer"] == "1.05" and v.final_qty in (None, 0)


def test_C06_max_position_exceeded_blocks(stack):
    v, hit = _blocked_through_service(stack, {"existing_qty": "100", "existing_notional": "150000.00", "existing_avg_cost": "1500.00"}, PR.B_CAP_EXCEEDED, "POSITION_CAP")
    assert v.existing_position_qty == "100" and Decimal(hit["input"]["available_notional"]) <= 0


def test_C07_normal_controls_are_reviewable_with_full_evidence_and_the_socket_can_force_a_blocker(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")                                                                                                # generated by the READY transition itself (listener)
    assert v.effective_status == "REVIEWABLE" and v.trigger == "SETUP_EVENT" and v.final_qty >= 1 and v.equity_source == "HYPOTHETICAL_REVIEW" and v.blockers == []
    rows = psvc.store.controls_for(v.proposal_id)
    assert [r["control_id"] for r in rows] == ALL_CONTROLS and {r["result"] for r in rows} <= {"PASS", "WARN"}
    status = {c["control_id"]: c for c in psvc.store.control_status()}
    assert set(status) == set(ALL_CONTROLS) and all(c["evaluation_count"] >= 1 and c["block_count"] == 0 for c in status.values())
    clock.advance(1)
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        ws.send_json({"type": "proposal_eval", "symbol": "SNDK", "account": {"halt": True, "halt_source": "socket"}})
        m = ws.receive_json()
        assert m["type"] == "proposal" and m["symbol"] == "SNDK" and m["effective_status"] == "BLOCKED" and PR.B_KILL in [b["code"] for b in m["blockers"]]
        clock.advance(1)
        ws.send_json({"type": "proposal_eval", "symbol": "SNDK", "clear": True})
        m2 = ws.receive_json()
        assert m2["effective_status"] == "REVIEWABLE" and m2["proposal_id"] != m["proposal_id"]
        ws.send_json({"type": "proposal_eval", "symbol": "SNDK", "account": "nope"})
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
    assert psvc.store.count() == 3                                                                                           # production refusal is pinned in X04


# ================================================================ P persistence =================================================================

def test_P01_migrations_are_idempotent_and_the_tables_are_append_only(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    run_proposal_migrations(conn)
    run_proposal_migrations(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(TABLES) <= names
    st = ProposalStore(db)
    d = E.build(inputs())
    row = st.insert(d, IN_SESSION, "TEST", "t")
    pid = row["proposal_id"]
    rw = sqlite3.connect(db)
    for sql in (f"UPDATE trade_proposals SET final_qty = 99 WHERE proposal_id = '{pid}'", f"UPDATE trade_proposals SET status = 'BLOCKED' WHERE proposal_id = '{pid}'",
                f"DELETE FROM trade_proposals WHERE proposal_id = '{pid}'", "DELETE FROM proposal_controls", "UPDATE proposal_controls SET result = 'PASS'", "DELETE FROM proposal_control_status",
                f"INSERT OR REPLACE INTO trade_proposals (proposal_id, setup_id, symbol, setup_type, side_context, status, created_at, expires_at, equity_source, blockers_json, warnings_json, inputs_json, controls_json, ppe_snapshot_json, demand_snapshot_json, evidence_snapshot_json, setup_snapshot_json, trigger, fingerprint) VALUES ('{pid}', '{'a' * 32}', 'SNDK', 'x', 'LONG_REVIEW', 'REVIEWABLE', 'x', 'x', 'x', '[]', '[]', '{{}}', '[]', '{{}}', '{{}}', '{{}}', '{{}}', 'TEST', 'f')"):
        with pytest.raises(sqlite3.IntegrityError):
            rw.execute(sql)
    rw.execute(f"UPDATE trade_proposals SET review_state = 'VIEWED', reviewed_at = 'x' WHERE proposal_id = '{pid}'")      # the only mutable fields
    rw.commit()
    assert st.get(pid)["review_state"] == "VIEWED" and len(st.controls_for(pid)) == len(ALL_CONTROLS) and len(st.control_status()) == len(ALL_CONTROLS)
    assert ProposalStore(tmp_path / "missing.db").latest("SNDK") is None and ProposalStore(tmp_path / "missing.db").count() == 0


def test_P02_proposal_persists_a_restart(stack, tmp_path):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    before = psvc.current("SNDK")
    s2, svc2, psvc2, _ = make_stack(tmp_path, clock=FakeClock(clock.now))                                                   # the restart: same database, new services
    RT.STREAM["instance"] = s2
    after = psvc2.current("SNDK")
    assert (after.proposal_id, after.effective_status, after.final_qty, after.reference_price, after.invalidation_price) == (before.proposal_id, "REVIEWABLE", before.final_qty, before.reference_price, before.invalidation_price)
    assert psvc2.store.controls_for(before.proposal_id) == psvc.store.controls_for(before.proposal_id)
    assert api.get("/api/proposals/SNDK").json()["proposal_id"] == before.proposal_id
    clock.advance(1)
    again = psvc2.evaluate("SNDK", trigger="MANUAL")
    assert again.proposal_id == before.proposal_id and psvc2.store.count() == 1                                              # unchanged inputs after the restart: no duplicate


def test_P03_proposal_id_is_stable_and_follows_the_formula(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    assert v.proposal_id == E.proposal_id_for(v.setup_id, v.created_at, v.reference_price, v.invalidation_price, v.risk_budget) and len(v.proposal_id) == 32
    assert psvc.current("SNDK").proposal_id == v.proposal_id == api.get("/api/proposals/SNDK").json()["proposal_id"]


def test_P04_unchanged_inputs_do_not_generate_a_duplicate(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    for _ in range(3):
        clock.advance(1)
        assert psvc.evaluate("SNDK", trigger="MANUAL").proposal_id == v.proposal_id
    assert psvc.store.count() == 1 and len(psvc.store.controls_for(v.proposal_id)) == len(ALL_CONTROLS)
    assert {c["control_id"]: c["evaluation_count"] for c in psvc.store.control_status()}["QUANTITY"] == 4                  # liveness still records every run


def test_P05_materially_changed_inputs_create_a_new_proposal_and_keep_the_old_one(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    clock.advance(1)
    v2 = psvc.evaluate("SNDK", trigger="TEST", overrides={"equity": "200000.00", "buying_power": "200000.00"})
    assert v2.proposal_id != v.proposal_id and v2.equity == "200000.00" and psvc.store.count() == 2
    hist = api.get("/api/proposals/history?symbol=SNDK").json()["proposals"]
    assert [h["proposal_id"] for h in hist] == [v2.proposal_id, v.proposal_id] and psvc.store.get(v.proposal_id)["equity"] == v.equity
    assert E.materially_changed(psvc.store.get(v2.proposal_id), E.build(psvc.inputs_for("SNDK", clock(), svc.current("SNDK"), overrides={"equity": "200000.00", "buying_power": "200000.00"})),
                                Decimal("0.5"), E._iso(clock())) is None                                                      # price unchanged -> reuse


def test_P06_a_stale_proposal_stays_in_history_and_is_never_reused(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    clock.advance(1)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(stretch=-54.0, confidence=58, demand=44))                       # READY -> WATCH
    cur = psvc.current("SNDK")
    assert cur.effective_status == "STALE" and cur.proposal_id == v.proposal_id and "no longer READY" in cur.stale_reason and cur.status == "REVIEWABLE"
    assert api.get("/api/proposals/SNDK").json()["effective_status"] == "STALE" and psvc.store.get(v.proposal_id)["status"] == "REVIEWABLE"
    clock.advance(1)
    svc.evaluate_symbol("SNDK", trigger="TEST", inputs=fx(**READY_IN))                                                      # back to READY: a NEW proposal, the old row untouched
    v2 = psvc.current("SNDK")
    assert v2.effective_status == "REVIEWABLE" and v2.proposal_id != v.proposal_id and psvc.store.count() == 2
    clock.advance(301)
    assert psvc.current("SNDK").effective_status == "EXPIRED"
    clock.advance(1)
    assert psvc.evaluate("SNDK", trigger="MANUAL").proposal_id not in (v.proposal_id, v2.proposal_id) and psvc.store.count() == 3


def test_P07_dismissal_persists_and_review_state_is_monotonic(stack, tmp_path):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    r = api.post(f"/api/proposals/{v.proposal_id}/view").json()
    assert r["review_state"] == "VIEWED" and r["reviewed_at"]
    r2 = api.post(f"/api/proposals/{v.proposal_id}/dismiss").json()
    assert r2["review_state"] == "DISMISSED" and r2["proposal"]["submission_status"] == "NOT SUBMITTED"
    assert api.post(f"/api/proposals/{v.proposal_id}/view").json()["review_state"] == "DISMISSED"                           # VIEWED only from UNREVIEWED
    assert api.post(f"/api/proposals/{'0' * 32}/dismiss").status_code == 404 and api.post("/api/proposals/not-an-id/view").status_code == 404
    s2, svc2, psvc2, _ = make_stack(tmp_path, clock=FakeClock(clock.now))
    assert psvc2.current("SNDK").review_state == "DISMISSED" and psvc2.store.get(v.proposal_id)["reviewed_at"] == r2["reviewed_at"]
    with pytest.raises(ProposalStoreError):
        psvc2.store.set_review(v.proposal_id, "APPROVED", clock())


def test_P08_blockers_and_warnings_persist(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    clock.advance(1)
    v = psvc.evaluate("SNDK", trigger="TEST", overrides={"halt": True, "halt_source": "forced", "buying_power": "5.00"})
    row = psvc.store.get(v.proposal_id)
    assert [b["code"] for b in row["blockers"]] == [PR.B_KILL, PR.B_BUYING_POWER, PR.B_QTY_ZERO] and row["blockers"] == v.blockers
    assert [w["code"] for w in row["warnings"]] == [w["code"] for w in v.warnings] and PR.W_SIMULATED in [w["code"] for w in row["warnings"]]
    j = api.get(f"/api/proposals/id/{v.proposal_id}").json()
    assert j["blockers"] == v.blockers and j["warnings"] == v.warnings and len(j["control_rows"]) == len(ALL_CONTROLS)


def test_P09_control_evidence_persists_per_proposal(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    rows = psvc.store.controls_for(v.proposal_id)
    assert len(rows) == len(ALL_CONTROLS) and all(r["proposal_id"] == v.proposal_id and r["symbol"] == "SNDK" and r["evaluated_at"] == v.created_at for r in rows)
    q = next(r for r in rows if r["control_id"] == "QUANTITY")
    assert q["input"]["final"] == v.final_qty and q["input"]["binding"] in ("risk_based", "position_cap", "buying_power", "hard_limit")
    assert {c["control_id"] for c in api.get("/api/proposals/controls?control=QUANTITY").json()["evaluations"]} == {"QUANTITY"}
    assert api.get("/api/proposals/controls").json()["known_controls"] == ALL_CONTROLS


def test_P10_overview_lists_open_setups_and_proposals_write_in_one_transaction(stack, monkeypatch):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    j = api.get("/api/proposals").json()
    assert j["count"] == 1 and j["proposals"][0]["symbol"] == "SNDK" and j["stored"] == 1 and j["submission_status"] == "NOT SUBMITTED" and j["disclaimer"] == PR.DISCLAIMER
    clock.advance(1)
    boom = ProposalStore(psvc.store.path)
    import sqlite3 as _sq
    real_connect = _sq.connect

    class Broken(Exception):
        pass

    class FailingConn(_sq.Connection):
        def execute(self, sql, *a):
            if str(sql).startswith("INSERT INTO proposal_controls"):
                raise Broken("disk gone")
            return super().execute(sql, *a)

    def failing_rw(self):
        conn = real_connect(str(self.path), timeout=10.0, isolation_level=None, factory=FailingConn)
        conn.row_factory = _sq.Row
        return conn
    monkeypatch.setattr(ProposalStore, "_rw", failing_rw)
    with pytest.raises(Broken):
        boom.insert(E.build(psvc.inputs_for("SNDK", clock(), svc.current("SNDK"), overrides={"equity": "77777.00", "buying_power": "77777.00"})), clock(), "TEST", "t")
    monkeypatch.undo()
    assert psvc.store.count() == 1                                                                                           # the proposal row was rolled back with its controls


# ================================================================ X no execution ==================================================================

ALLOWED_PAPER = ("paper.alpaca_order_rules", "paper.alpaca_order_store", "paper.alpaca_view", "paper.portfolio")


def test_X01_no_order_writer_broker_client_model_or_research_import():
    forbidden = tuple(p for p in FORBIDDEN_IMPORTS if p not in ("forward", "paper.alpaca_order")) + ("paper.alpaca_orders", "paper.alpaca_order_writer", "paper.alpaca_order_reads",
                                                                                                       "paper.execution", "paper.alpaca_readonly", "ppe.engine", "ppe.stretch", "demand.engine", "demand.components",
                                                                                                       "evidence.engine", "research_flow", "forward.capture", "forward.journal", "rotation")
    for rel in PROPOSAL_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names += [n.module or ""] + [f"{n.module}.{a.name}" if n.module else a.name for a in n.names]
        for name in names:
            assert not any(name == p or name.startswith(p + ".") for p in forbidden), (rel, name)
            if name.startswith("paper") and name != "paper":
                assert any(name == p or name.startswith(p + ".") for p in ALLOWED_PAPER), (rel, name)                      # read-only views / constants / store only
        assert not re.search(r"TradingClient|TradingStream|messages\.create|get_provider|/v2/orders|alpaca_order_writer|\.submit\(|place_order|cancel_order|_post_once|confirm\(|preview\(|"
                             r"requests\.|urlopen|\beval\(|\bexec\(|__import__|os\.system|subprocess|build_packet|get_data_client|anthropic|robin_stocks|rh_gateway|create_order", text), rel


def test_X02_no_alpaca_post_is_possible_from_the_proposal_path(stack, monkeypatch):
    import paper.alpaca_order_writer as W
    import paper.alpaca_orders as AO

    def boom(*a, **k):
        raise AssertionError("the broker writer was reached from the proposal layer")
    monkeypatch.setattr(W, "submit", boom)
    monkeypatch.setattr(AO, "preview", boom)
    monkeypatch.setattr(AO, "confirm", boom)
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    assert v.effective_status == "REVIEWABLE"
    api.post(f"/api/proposals/{v.proposal_id}/view")
    api.post(f"/api/proposals/{v.proposal_id}/dismiss")
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        ws.send_json({"type": "proposal_eval", "symbol": "SNDK"})
        assert ws.receive_json()["type"] == "proposal"
    from paper import alpaca_order_store as AOS
    assert not AOS.OrderStore(psvc.store.path).exists()                                                                      # no 4.6B tables were created, let alone written


def test_X03_no_robinhood_write_and_no_robinhood_read(stack, monkeypatch):
    import api.routes.portfolio as PRT
    calls = []
    monkeypatch.setattr(PRT, "provider_factory", lambda *a, **k: calls.append("factory") or (_ for _ in ()).throw(RuntimeError("robinhood touched")))
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    assert psvc.current("SNDK").effective_status == "REVIEWABLE" and calls == []
    assert not re.search(r"(?i)robinhood|robin_stocks|rh_gateway", source_of(PROPOSAL_FILES))


def test_X04_no_order_endpoint_and_review_state_is_the_only_write(stack):
    s, svc, psvc, clock, api = stack
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/proposals")}
    assert mine == {"/api/proposals": {"get"}, "/api/proposals/controls": {"get"}, "/api/proposals/history": {"get"}, "/api/proposals/id/{proposal_id}": {"get"},
                    "/api/proposals/{symbol}": {"get"}, "/api/proposals/{proposal_id}/view": {"post"}, "/api/proposals/{proposal_id}/dismiss": {"post"}}
    for p in mine:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|trade|confirm|accept", p), p
    for path in ("/api/proposals", "/api/proposals/SNDK", "/api/proposals/controls", "/api/proposals/history"):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert api.request(method, path, json={"side": "buy", "symbol": "SNDK", "qty": 1}).status_code == 405, (method, path)
    go_ready(svc, clock)
    v = psvc.current("SNDK")
    before = psvc.store.get(v.proposal_id)
    api.post(f"/api/proposals/{v.proposal_id}/view", json={"final_qty": 999, "status": "APPROVED"})
    after = psvc.store.get(v.proposal_id)
    assert {k: after[k] for k in after if k not in ("review_state", "reviewed_at")} == {k: before[k] for k in before if k not in ("review_state", "reviewed_at")}
    prod = TerminalStream(SimulatedMarket(seed=1, start_time=T0), FakePositions(), FakeResearch(), watchlist_fn=lambda: [], run_loop=False, now_fn=lambda: T0,
                          setup_service=svc, proposal_service=psvc)
    RT.STREAM["instance"] = prod
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        ws.send_json({"type": "proposal_eval", "symbol": "SNDK", "account": {"halt": False}})
        assert ws.receive_json()["code"] == "NOT_ALLOWED"                                                                   # forced evaluations exist only in test processes
        for kind in ("prepare_buy", "accept", "preview", "confirm", "order", "execute"):
            ws.send_json({"type": kind, "symbol": "SNDK", "proposal_id": v.proposal_id})
            assert ws.receive_json()["code"] == "BAD_MESSAGE", kind


def test_X05_trader_view_still_imports_no_execution_module():
    import trader_view.stream as stream_mod
    import trader_view.models as models_mod
    for mod in (stream_mod, models_mod):
        for value in vars(mod).values():
            if inspect.ismodule(value):
                assert not any(value.__name__ == p or value.__name__.startswith(p + ".") for p in FORBIDDEN_IMPORTS), (mod.__name__, value.__name__)
    text = (ROOT / "trader_view" / "stream.py").read_text(encoding="utf-8")
    assert "from proposals.service import ProposalService" in text and not re.search(r"(?i)preview\(|submit\(|/v2/orders|alpaca_order_writer|place_order", text)


def test_X06_alerts_and_setup_transitions_trigger_nothing_but_a_review_artifact(stack):
    s, svc, psvc, clock, api = stack
    assert [fn.__name__ for fn in svc.listeners] == ["_setup_event", "on_setup_event"] and [fn.__name__ for fn in psvc.listeners] == ["_proposal_event"]
    src = inspect.getsource(ProposalService)
    assert not re.search(r"(?i)\border\b|broker|execute|preview\(|submit|confirm\(|accept", src)
    go_ready(svc, clock)                                                                                                     # a READY alert fired; the only consequence:
    assert psvc.store.count() == 1 and psvc.current("SNDK").submission_status == "NOT SUBMITTED"
    from paper import alpaca_order_store as AOS
    from paper.store import PaperStore
    assert not AOS.OrderStore(psvc.store.path).exists() and PaperStore(psvc.store.path).account() is None


def test_X07_no_automatic_research_and_no_model_call(stack, monkeypatch):
    import anthropic
    s, svc, psvc, clock, api = stack

    def boom(self, *a, **k):
        raise AssertionError("a model client was constructed by the proposal layer")
    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    n = len(s.research.calls)
    go_ready(svc, clock)
    clock.advance(1)
    psvc.evaluate("SNDK", trigger="MANUAL")
    assert psvc.current("SNDK").effective_status == "REVIEWABLE" and s.research.calls[n:] == []                            # evidence comes from the stored setup inputs
    assert not re.search(r"build_packet|research_flow|run_research|EvidenceStore\(|anthropic|messages\.create", source_of(PROPOSAL_FILES))


def test_X08_no_buy_sell_recommendation_target_or_probability_field_anywhere():
    for model in (ProposalInputs, ProposalView, Draft, AccountSnapshot, ControlEvaluation, PriceCandidate):
        for k in model.model_fields:
            assert not re.search(r"(?i)\b(buy|sell|recommend\w*|probab\w*|expected|target|signal|execute|accept\w*)\b", k.replace("_", " ")), (model.__name__, k)
    assert set(PR.STATUSES) == {"NOT_ELIGIBLE", "BLOCKED", "REVIEWABLE", "STALE", "EXPIRED"} and not {"APPROVED", "BUY", "EXECUTE", "GO"} & set(PR.STATUSES)
    assert PR.STATUS_LABELS["REVIEWABLE"] == "REVIEWABLE PROPOSAL" and PR.SUBMISSION_STATUS == "NOT SUBMITTED" and PR.SIDE_LABEL == "Long review proposal"
    for text in list(PR.TEXT.values()) + list(PR.STATUS_LABELS.values()) + [PR.SIDE_LABEL]:
        assert not TRADE_WORDS.search(text) and not FORECAST.search(text), text
    assert PR.DISCLAIMER == "This is a deterministic review proposal, not a recommendation or submitted order."
    src = strip_code(source_of(PROPOSAL_FILES)).replace(PR.DISCLAIMER, "").replace(".execute(", ".sql(")                   # sqlite3 calls are not trade words
    assert not TRADE_WORDS.search(src) and not FORECAST.search(src)
    assert "recommend" not in src.lower().replace("not a recommendation", "")


def test_X09_prepare_buy_sell_remain_disabled_and_no_short_proposal(stack):
    s, svc, psvc, clock, api = stack
    go_ready(svc, clock)
    st = s.state("SNDK", with_candles=False)
    acts = {a.id: a for a in st.actions}
    assert st.proposal.effective_status == "REVIEWABLE" and acts["prepare_buy"].enabled is False and acts["prepare_sell"].enabled is False
    assert acts["prepare_buy"].status == acts["prepare_sell"].status == "PREVIEW_NOT_CONNECTED" and s.config_payload()["execution_preview"] == "NOT_CONNECTED"
    assert "proposal" not in JS.split("function renderActions")[1].split("function renderStatusLine")[0]
    assert st.proposal.side_context == "LONG_REVIEW" and PR.SIDE_CONTEXT == "LONG_REVIEW" and not re.search(r"(?i)short_review|SHORT_REVIEW|short proposal", source_of(PROPOSAL_FILES))


# ================================================================ U frontend =========================================================================

def test_U01_node_tests_of_the_proposal_markup():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "trader_proposal.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 14\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]
    assert re.search(r"fail 0\b", r.stdout)


def test_U02_trade_review_section_wiring_in_the_page():
    assert '"proposal"' in CORE.split("SYMBOL_BOUND = new Set(")[1].split(")")[0]
    assert 'case "proposal": renderProposal(m); break;' in JS and "renderProposal(d.proposal)" in JS and "renderProposal(null)" in JS and '["tv-proposal", "TRADE REVIEW"]' in JS
    assert JS.index("<h3>Setup</h3>") < JS.index("<h3>Trade Review</h3>") < JS.index("<h3>Evidence</h3>") and "st.pending.proposal = true;" in JS
    assert "proposalHtml" in RENDER and "proposalModel" in RENDER and "proposalDetailsHtml" in RENDER and 'data-proposal-status="' in RENDER and "data-proposal-submission" in RENDER
    assert '<script src="trade_review.js"></script>' in HTML and HTML.index("trader_view.js") < HTML.index("setup_alerts.js") < HTML.index("trade_review.js")
    assert ".tv-prop-state.reviewable" in CSS and ".tv-prop-state.blocked" in CSS and ".tv-prop-state.stale" in CSS and ".tv-prop-grid" in CSS and ".tv-prop-details" in CSS
    assert min(int(x) for x in re.findall(r"font-size:\s*(\d+)px", CSS)) >= 11
    assert "window.TradeReview = { bind, state };" in REVIEW_JS and "applyProposal" in JS


def test_U03_no_buy_wording_target_or_probability_in_the_trade_review_ui():
    section = RENDER.split("Trade review proposal (Stage 5.9")[1].split("status strip")[0]
    code = re.sub(r"//.*", "", section)
    assert not TRADE_WORDS.search(code) and not FORECAST.search(code) and "recommend" not in code.lower()
    assert "NOT SUBMITTED" in section and "REVIEWABLE PROPOSAL" in section and "Long review proposal" in section and "It is not an entry signal" in section
    review = re.sub(r"//.*", "", REVIEW_JS)
    assert not TRADE_WORDS.search(review) and not FORECAST.search(review) and "View Details" in RENDER and "Dismiss" in RENDER
    assert re.findall(r"method: \"(POST|PUT|PATCH|DELETE)\"", REVIEW_JS) == ["POST"] and REVIEW_JS.count("/api/proposals/") == 2
    assert not re.search(r"(?i)method:\s*[\"'](POST|PUT|PATCH|DELETE)", JS)                                                 # the page itself never writes


def test_U04_harness_flow_registered_after_setup_state():
    import browser_tests.flows as F
    names = [f.__name__ for f in F.FLOWS]
    assert "trade_review" in names and names.index("trade_review") == names.index("setup_state") + 1 and names.index("trade_review") < names.index("automation")
    assert names[-8:] == ["automation", "saved_scans", "daily_brief", "brief_delivery", "notification_click", "paper_portfolio", "alpaca_paper", "alpaca_orders"]


def test_U05_proposal_messages_are_symbol_bound_and_follow_refresh(stack):
    assert "proposal" in MESSAGE_TYPES and "proposal" in SYMBOL_BOUND_TYPES
    assert not accepts_message({"type": "proposal", "symbol": "SNDK"}, "AAPL") and accepts_message({"type": "proposal", "symbol": "AAPL"}, "AAPL")
    s, svc, psvc, clock, api = stack
    with api.websocket_connect("/api/terminal/stream") as ws:
        hello_of(ws)
        assert [m["type"] for m in burst(ws, "AAPL")] == ["subscribed", "state", "candles", "quote", "candle", "position", "evidence"]   # the Stage 5.5 burst is unchanged
        assert marker(ws) == []                                                                                                         # NOT ELIGIBLE: no extra message on subscribe
        ws.send_json({"type": "refresh"})
        got = marker(ws)
        assert [m["type"] for m in got] == ["position", "evidence", "ppe", "demand", "setup", "proposal"] and got[-1]["symbol"] == "AAPL" and got[-1]["effective_status"] == "NOT_ELIGIBLE"
        assert got[-1]["submission_status"] == "NOT SUBMITTED" and got[-1]["disclaimer"] == PR.DISCLAIMER


# ================================================================ B base pins =========================================================================

def test_B01_setups_ppe_demand_evidence_and_execution_files_identical_to_base():
    def blob(rel):
        out = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert out, rel
        return out.replace(b"\r\n", b"\n")
    pinned = [p.relative_to(ROOT).as_posix() for p in (ROOT / "setups").glob("*.py")] + [p.relative_to(ROOT).as_posix() for p in (ROOT / "ppe").glob("*.py")] + \
             [p.relative_to(ROOT).as_posix() for p in (ROOT / "demand").glob("*.py")] + \
             ["trader_view/ppe_provider.py", "trader_view/demand_provider.py", "trader_view/adapters.py", "trader_view/hub.py", "trader_view/positions.py", "trader_view/research.py",
              "trader_view/market_real.py", "trader_view/simulated.py", "evidence/engine.py", "evidence/rules.py", "evidence/store.py", "paper/alpaca_order_writer.py",
              "paper/alpaca_orders.py", "paper/alpaca_order_rules.py", "paper/alpaca_order_store.py", "paper/alpaca_order_reads.py", "paper/alpaca_readonly.py", "paper/alpaca_view.py",
              "paper/execution.py", "paper/portfolio.py", "portfolio/provider.py", "database/migrations.py", "database/setup_state_migrations.py", "api/routes/setups.py",
              "forward/automation.py", "analysis/position_sizing.py", "DESIGN_56_PPE_ENGINE.md", "DESIGN_57_MARKET_DEMAND.md", "DESIGN_58_SETUP_STATE_ALERTS.md",
              "tests/test_evidence_54.py", "tests/frontend/trader_core.test.cjs", "tests/frontend/trader_demand.test.cjs", "tests/frontend/trader_render.test.cjs", "tests/frontend/trader_setup.test.cjs"]
    for rel in pinned:
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob(rel), f"{rel} differs from {BASE}"
    base_cfg = blob("config.py").decode("utf-8")
    now_cfg = (ROOT / "config.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert now_cfg.startswith(base_cfg.rstrip("\n")) and "TRADE_PROPOSAL_RISK_FRACTION" in now_cfg and config.TRADE_PROPOSAL_RISK_FRACTION == 0.005
    assert config.TRADE_PROPOSAL_TTL_SECONDS == 300 and config.MAX_POSITION_PERCENT == 10.0 and PR.ATR_PERIOD == config.ATR_PERIOD == 14
