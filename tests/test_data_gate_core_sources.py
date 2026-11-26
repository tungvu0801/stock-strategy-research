"""EXEC-C1a: r14 §9 test 2 (the actual source identifiers) on the pure gate. Accepted: LAST_TRADE and MINUTE_CLOSE with INTRADAY;
QUOTE_MID per OD-R4-5(a) (UNVERIFIED before U-11). Refused: DAILY_CLOSE / DAILY_REFERENCE -> DG_DAILY_FALLBACK; QUOTE, '' and unknown ->
DG_UNVERIFIED. A minute close is aged from its stamped start under OD-R5-3(a) (900 s accepted, 901 s stale) and its finality comes
from start + 60 s. "Accepted" on ENTRY means E3/E4 pass: E7 still refuses every real-broker entry in Phase 1 (OD-R2-2(a)).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from execution import data_gate as dg

UTC = timezone.utc
T = datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)
MASK = "••••"


def iso(dt):
    return dt.isoformat()


def ago(s):
    return T - timedelta(seconds=s)


ENV = {"paper": True, "host": dg.PAPER_HOST, "label": "alpaca-paper", "error": None, "blocked": False, "status": "ACTIVE",
       "account_number_masked": MASK + "1234"}
SETUP = {"mode": "real", "ppe_status": "AVAILABLE", "demand_status": "AVAILABLE", "stretch_status": "AVAILABLE", "quote_status": "OK",
         "ppe_fresh": True, "demand_fresh": True, "quote_fresh": True, "stale_reasons": [], "evaluated_at": iso(ago(60))}


def entry(price_mode="INTRADAY", price_source="LAST_TRADE", price_timestamp=None, broker_clock_ts=T, **kw):
    proposal = {"proposal_id": "P-1", "mode": "real", "warnings": [], "price_mode": price_mode, "price_source": price_source,
                "price_timestamp": iso(ago(100)) if price_timestamp is None else price_timestamp}
    base = dict(stage=dg.DG_EARLY, eval_clock=T, broker_simulated=False, environment=ENV, market_mode="real", proposal=proposal,
                expected_proposal_id="P-1", proposal_executable=True, setup_inputs=SETUP,
                broker_clock={"timestamp": iso(broker_clock_ts), "is_open": True}, calendar=dg.COVERED_SESSION,
                stop_check_problems={}, protective_exit_problems={}, lifecycle_states=[], latch_keys=set(),
                attention_baseline=dg.AttentionBaseline("c1", {}), cycle_id="c1", open_positions_now={}, verified_positions=set())
    base.update(kw)
    return dg.evaluate(dg.EntryFacts(**base))


def e34(v):
    """The E3/E4 failures of a verdict, as {fact: codes}."""
    return [f for f in v.failed_facts if f in ("E3", "E4")]


@pytest.mark.parametrize("source,ts_age", [("LAST_TRADE", 100), ("MINUTE_CLOSE", 120)])
def test_accepted_identifiers_with_intraday(source, ts_age):
    v = entry(price_source=source, price_timestamp=iso(ago(ts_age)))
    assert e34(v) == [] and v.failed_facts == ("E7",)


def test_quote_mid_is_unverified_before_u11_od_r4_5_a():
    v = entry(price_source="QUOTE_MID")
    assert "E3" in v.failed_facts and "DG_UNVERIFIED" in v.codes


@pytest.mark.parametrize("mode,source", [("DAILY_REFERENCE", "DAILY_CLOSE"), ("DAILY_REFERENCE", "LAST_TRADE"), ("INTRADAY", "DAILY_CLOSE")])
def test_daily_fallback(mode, source):
    v = entry(price_mode=mode, price_source=source)
    assert "E3" in v.failed_facts and "DG_DAILY_FALLBACK" in v.codes
    assert v.code == "DG_DAILY_FALLBACK"                     # E3 precedes E7


@pytest.mark.parametrize("source", ["QUOTE", "", None, "BOGUS", "last_trade"])
def test_other_identifiers_unverified(source):
    v = entry(price_source=source)
    assert "E3" in v.failed_facts and "E4" in v.failed_facts
    assert "DG_UNVERIFIED" in v.codes and "DG_DAILY_FALLBACK" not in v.codes


@pytest.mark.parametrize("mode", [None, "", "intraday", "REALTIME"])
def test_price_mode_must_be_intraday(mode):
    assert "E3" in entry(price_mode=mode).failed_facts


@pytest.mark.parametrize("age,stale", [(300, False), (301, True), (600, True)])
def test_last_trade_limit_300(age, stale):
    v = entry(price_source="LAST_TRADE", price_timestamp=iso(ago(age)))
    assert ("DG_STALE" in v.codes) is stale


@pytest.mark.parametrize("age,stale", [(600, False), (899, False), (900, False), (901, True), (959, True), (960, True)])
def test_minute_close_aged_from_stamped_start(age, stale):
    """901-960 s from the start is stale even though the bar's END is only 841-900 s old (the end-basis mutant)."""
    v = entry(price_source="MINUTE_CLOSE", price_timestamp=iso(ago(age)))
    assert ("DG_STALE" in v.codes) is stale
    assert ("E4" in v.failed_facts) is stale


@pytest.mark.parametrize("start_age,final", [(60, True), (59, False), (30, False), (0, False)])
def test_minute_close_finality_from_start_plus_60(start_age, final):
    v = entry(price_source="MINUTE_CLOSE", price_timestamp=iso(ago(start_age)))
    assert ("E4" not in v.failed_facts) is final


def test_minute_close_end_must_not_be_after_the_broker_clock():
    assert "E4" in entry(price_source="MINUTE_CLOSE", price_timestamp=iso(ago(60)), broker_clock_ts=ago(10)).failed_facts
    assert "E4" not in entry(price_source="MINUTE_CLOSE", price_timestamp=iso(ago(70)), broker_clock_ts=ago(10)).failed_facts


@pytest.mark.parametrize("ts,ok", [("2026-11-23T14:58:20Z", True), ("2026-11-23T14:58:20+00:00", True), ("2026-11-23T14:58:20", False),
                                   (None, False), ("", False), ("not-a-time", False)])
def test_proposal_timestamp_forms(ts, ok):
    v = entry(price_timestamp=ts if ts is not None else "")
    assert ("E4" not in v.failed_facts) is ok


@pytest.mark.parametrize("source,age,stale", [("QUOTE", 300, False), ("QUOTE", 301, True), ("MINUTE_CLOSE", 900, False), ("MINUTE_CLOSE", 901, True)])
def test_executor_live_price_sources_e5(source, age, stale):
    lp = dg.GatePrice(source, "10.00", iso(ago(age)), "OK", True if source == "MINUTE_CLOSE" else None)
    v = entry(stage=dg.DG_FINAL, live_price=lp)
    assert ("E5" in v.failed_facts) is stale


@pytest.mark.parametrize("source", ["UNAVAILABLE", "LAST_TRADE", "QUOTE_MID", "", None])
def test_executor_live_price_other_sources_refused(source):
    v = entry(stage=dg.DG_FINAL, live_price=dg.GatePrice(source, "10.00", iso(ago(10)), "OK"))
    assert "E5" in v.failed_facts and "DG_UNVERIFIED" in v.codes


@pytest.mark.parametrize("source,age,status", [("QUOTE", 300, dg.NOT_HIT), ("QUOTE", 301, dg.BLIND), ("MINUTE_CLOSE", 900, dg.NOT_HIT),
                                               ("MINUTE_CLOSE", 901, dg.BLIND), ("MINUTE_CLOSE", 950, dg.BLIND)])
def test_sc_uses_the_same_source_rules(source, age, status):
    p = dg.GatePrice(source, "10.00", iso(ago(age)), "OK", True if source == "MINUTE_CLOSE" else None)
    r = dg.stop_status(dg.StopCheckFacts(eval_clock=T, market_mode="real", price=p, level="9.00", clock={"timestamp": iso(T), "is_open": True}))
    assert r.status == status


@pytest.mark.parametrize("source", ["LAST_TRADE", "QUOTE_MID", "DAILY_CLOSE", "UNAVAILABLE", ""])
def test_sc_refuses_non_executor_sources(source):
    p = dg.GatePrice(source, "10.00", iso(ago(10)), "OK")
    r = dg.stop_status(dg.StopCheckFacts(eval_clock=T, market_mode="real", price=p, level="9.00", clock={"timestamp": iso(T), "is_open": True}))
    assert r.status == dg.BLIND and r.reason == "PRICE_MISSING"


def test_age_basis_and_source_time_helpers():
    s = ago(100)
    assert dg.age_basis("MINUTE_CLOSE", s) == s
    assert dg.source_time("MINUTE_CLOSE", s) == s + timedelta(seconds=60)
    assert dg.source_time("QUOTE", s) == s
    assert (dg.age_limit("MINUTE_CLOSE"), dg.age_limit("QUOTE"), dg.age_limit("LAST_TRADE")) == (900, 300, 300)
