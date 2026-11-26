"""EXEC-C1a: r14 §9 test 18, PURE-EVALUATION PORTION ONLY (PARTIAL; test 18 is not complete here). Covered: the real-broker selection of
§2.4.1 (`select_gate_price`) feeding SC - a quote outage with the newest bar still forming selects the previous bar, final by the clock,
giving NOT_HIT or HIT (never BAR_NOT_FINAL), aged from its stamped start. NOT covered (service part, stage C1c1): the collector wiring
of `_gate_price` and "on a simulated broker `_price` is unchanged".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from execution import data_gate as dg

UTC = timezone.utc
T = datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)


def ago(s):
    return T - timedelta(seconds=s)


def candle(start, close, final=True, epoch=False):
    return {"time": start.timestamp() if epoch else start.isoformat(), "close": close, "final": final}


OUTAGE = {"status": "UNAVAILABLE", "price": None, "time": None}


def bars(epoch=False):
    # every fetched bar is flagged final (trader_view/market_real.py:147-148); the newest is still forming (end T+30)
    return [candle(ago(150), "10.10", epoch=epoch), candle(ago(90), "10.20", epoch=epoch), candle(ago(30), "10.30", epoch=epoch)]


def status_of(price, level):
    return dg.stop_status(dg.StopCheckFacts(eval_clock=T, market_mode="real", price=price, level=level, clock={"timestamp": T.isoformat(), "is_open": True}))


@pytest.mark.parametrize("epoch", [False, True])
def test_outage_with_forming_bar_selects_the_previous_final_bar(epoch):
    p = dg.select_gate_price(OUTAGE, bars(epoch), T, T.isoformat())
    assert p.source == "MINUTE_CLOSE" and p.value == "10.20" and p.final is True
    assert dg.parse_ts(p.time)[0] == ago(90)                # the stamped start, not finals[-1]


@pytest.mark.parametrize("level,status", [("9.00", dg.NOT_HIT), ("10.50", dg.HIT)])
def test_selected_bar_gives_not_hit_or_hit_never_bar_not_final(level, status):
    r = status_of(dg.select_gate_price(OUTAGE, bars(), T, T.isoformat()), level)
    assert r.status == status and "BAR_NOT_FINAL" not in r.codes


@pytest.mark.parametrize("start_age,status", [(900, dg.NOT_HIT), (901, dg.BLIND), (950, dg.BLIND)])
def test_selected_bar_is_aged_from_its_start(start_age, status):
    rows = [candle(ago(start_age), "10.20"), candle(ago(30), "10.30")]
    p = dg.select_gate_price(OUTAGE, rows, T, T.isoformat())
    assert dg.parse_ts(p.time)[0] == ago(start_age)
    r = status_of(p, "9.00")
    assert r.status == status
    if status == dg.BLIND:
        assert r.codes == ("PRICE_STALE",)


def test_quote_first_when_it_has_status_ok_a_price_and_a_time():
    q = {"status": "OK", "price": "10.50", "time": ago(5).isoformat()}
    p = dg.select_gate_price(q, bars(), T, T.isoformat())
    assert (p.source, p.value) == ("QUOTE", "10.50")


@pytest.mark.parametrize("q", [{"status": "STALE", "price": "10.5", "time": "2026-11-23T14:59:55+00:00"},
                               {"status": "OK", "price": None, "time": "2026-11-23T14:59:55+00:00"},
                               {"status": "OK", "price": "10.5", "time": None},
                               None, dg.UNREADABLE])
def test_unusable_quote_falls_through_to_the_bars(q):
    assert dg.select_gate_price(q, bars(), T, T.isoformat()).source == "MINUTE_CLOSE"


def test_bar_end_must_not_be_after_the_broker_clock():
    p = dg.select_gate_price(OUTAGE, bars(), T, ago(40).isoformat())    # T-90's end (T-30) is after the broker clock
    assert p.value == "10.10" and dg.parse_ts(p.time)[0] == ago(150)


def test_a_bar_not_flagged_final_is_skipped():
    rows = [candle(ago(150), "10.10"), candle(ago(90), "10.20", final=False), candle(ago(30), "10.30")]
    assert dg.select_gate_price(OUTAGE, rows, T, T.isoformat()).value == "10.10"


@pytest.mark.parametrize("broker_ts", [None, "", "2026-11-23T15:00:00", dg.UNREADABLE])
def test_no_verified_broker_timestamp_means_no_bar_is_final_by_the_clock(broker_ts):
    p = dg.select_gate_price(OUTAGE, bars(), T, broker_ts)
    assert p.source == "UNAVAILABLE"
    assert status_of(p, "9.00").reason == "PRICE_MISSING"


@pytest.mark.parametrize("rows", [[], None, dg.UNREADABLE, [candle(ago(30), "10.30")]])
def test_nothing_final_is_unavailable(rows):
    assert dg.select_gate_price(OUTAGE, rows, T, T.isoformat()).source == "UNAVAILABLE"
