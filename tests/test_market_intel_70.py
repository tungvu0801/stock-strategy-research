"""
Stage 7.0 - market intelligence CONTRACTS (DESIGN_70_MARKET_INTEL_CONTRACTS.md). Pure modules: no network, thread, file, database,
route or config change.

  M01-M03 the observation envelope and the fetch record: UTC times, available_at >= retrieved_at, backfill honesty, symbols, a missing
          value is never a zero, status / freshness agreement, closed quality codes, no instruction field anywhere in the schema, no
          instruction key at any depth of the payload, a read-only payload.
  T01     settings: OFF by default (config declares nothing yet), SHADOW accepted, ACTIVE / junk read as OFF and are reported, budgets
          clamped to their ranges (the Alpaca live budget can never exceed 20 % of the Basic plan).
  Q01-Q04 the pre-registered freshness policy: every state of every kind at its exact boundaries, windows, fail-closed scopes, NO_PRINTS,
          the date-based kinds (daily bar, earnings calendar, macro, fundamentals), numbers pinned and equal to their source constants,
          `usable`, and a scheduled macro release that has not been observed is never usable (boundaries).
  V01-V05 validation codes on fixtures (bars, series, quotes, feeds, news, events); every code is declared and accepted by the envelope;
          uncovered 2024 / 2028 dates and bars on known holidays are flagged and blocked, never treated as verified sessions.
  K01-K03 the calendar: the Stage 6.10A classifier is the one source of truth; holidays, early closes, PRE / REG / POST, DST change days,
          an uncovered year; the holiday rules reproduce the static 2026-2027 table; uncovered weekdays are NOT_COVERED, never guessed.
  B01-B02 request budgets on an injected clock: token bucket, daily cap at New York midnight, back-off, ACCESS_DENIED per cycle, plan
          pressure, the defaults.
  I01     isolation: the package imports only pure modules, opens no file / socket / thread, and nothing outside it imports it.
  F01-F03 scope: exactly the declared files changed; frozen hashes unchanged; every bookkeeping edit pinned exactly.
"""
from __future__ import annotations

import ast
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import freeze_ledger as FL
import market_intel as MI
from market_intel import budget as BU
from market_intel import calendar as CAL
from market_intel import freshness as FR
from market_intel import settings as ST
from market_intel import validate as VA
from market_intel.models import FORBIDDEN_WORDS, FetchRecord, Observation, forbidden_key

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
REG_T = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)          # Monday 10:00 New York (EST, the day after the DST change): REG
SAT_T = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)         # a Saturday


def obs(**over):
    base = dict(kind="BAR_1MIN", symbol="nvda", provider="ALPACA", feed="iex", adjustment="raw", observed_at=REG_T - timedelta(minutes=1),
                retrieved_at=REG_T, available_at=REG_T, session="REG", session_date=date(2026, 11, 2), status="AVAILABLE", freshness="FRESH",
                age_s=60.0, payload={"open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100})
    base.update(over)
    return Observation(**base)


# ============================================================================================================ models (M)
def test_M01_the_envelope_enforces_point_in_time_honesty_and_never_turns_a_missing_value_into_a_zero():
    o = obs()
    assert o.symbol == "NVDA" and o.retrieved_at.tzinfo == UTC and o.usable_at(REG_T) and not o.usable_at(REG_T - timedelta(seconds=1))
    assert obs(retrieved_at=datetime(2026, 11, 2, 10, 0, tzinfo=timezone(timedelta(hours=-5))), available_at=REG_T).retrieved_at == REG_T
    bad = [
        dict(retrieved_at=datetime(2026, 11, 2, 15, 0)),                                         # naive time
        dict(available_at=REG_T - timedelta(seconds=1)),                                          # usable before we had it
        dict(backfilled=True, available_at=REG_T + timedelta(days=1)),                            # history presented as known live... later
        dict(symbol="BAD SYMBOL"), dict(symbol=None),                                             # a symbol kind needs a valid symbol
        dict(kind="MACRO", symbol=None, series_id=None),                                          # a series kind needs a series id
        dict(status="UNAVAILABLE", freshness=None),                                               # ... with a payload: never a zero
        dict(status="STALE", freshness="AGING"), dict(status="AVAILABLE", freshness="STALE"),     # status and freshness agree
        dict(quality_flags=("LOOKS_FINE",)),                                                      # closed codes
        dict(payload={"order_qty": 5}), dict(payload={"buySide": True}), dict(payload={"limit_price": 1.0}), dict(payload={"x": float("nan")}),
        dict(kind="TICK"), dict(provider="ROBINHOOD"), dict(feed="otc"), dict(session="LUNCH"), dict(content_hash="ABC"), dict(tier=9),
        dict(unknown_field=1),                                                                    # extra=forbid
    ]
    for over in bad:
        with pytest.raises(ValidationError):
            obs(**over)
    assert obs(backfilled=True, available_at=REG_T, quality_flags=("BACKFILLED",)).backfilled
    assert obs(status="UNAVAILABLE", freshness=None, payload={}).payload == {}
    assert obs(kind="MACRO", symbol=None, series_id="CPIAUCSL", provider="FRED", feed="none", payload={"value": 3.1}).series_id == "CPIAUCSL"
    assert obs(payload={"bid_size": 3, "inside_bar": True, "volume": 0}).payload["volume"] == 0       # a real zero volume is a value
    with pytest.raises(ValidationError):
        o.status = "STALE"                                                                     # frozen


def test_M02_no_field_anywhere_in_the_schema_names_an_instruction_and_fetch_records_hold_no_credential():
    for model in (Observation, FetchRecord):
        names = list(model.model_fields)
        assert not [n for n in names if forbidden_key(n)], model.__name__
        assert model.model_config.get("extra") == "forbid" and model.model_config.get("frozen") is True
    assert {"order", "qty", "side", "buy", "sell", "signal", "target"} <= set(FORBIDDEN_WORDS)
    assert not forbidden_key("inside") and not forbidden_key("bid_size") and forbidden_key("orderQty") and forbidden_key("stop_loss_pct")
    ok = FetchRecord(provider="SEC", endpoint="/submissions/CIK0000320193.json", started_at=REG_T, retrieved_at=REG_T + timedelta(seconds=1),
                     status="OK", http_status=200, latency_ms=812, bytes=1024)
    assert ok.latency_ms == 812
    for over in (dict(endpoint="/query?apikey=abc"), dict(retrieved_at=REG_T - timedelta(seconds=1)), dict(retrieved_at=None),
                 dict(status="FINE"), dict(provider="X"), dict(http_status=700)):
        with pytest.raises(ValidationError):
            FetchRecord(**{**ok.model_dump(), **over})


def test_M03_instruction_keys_are_refused_at_any_depth_and_the_stored_payload_is_read_only():
    nested = [{"meta": {"order_qty": 5}}, {"legs": [{"side": "BUY"}]}, {"a": {"b": {"c": {"stopPrice": 1.0}}}}, {"x": [[{"limit_price": 2}]]},
              {"m": {"take_profit": 1}}, {"m": {"buySide": True}}, {"bars": [{"close": float("nan")}]}, {"m": {1: "not a string key"}},
              {"t": REG_T}, {"s": {1, 2}}]
    for payload in nested:
        with pytest.raises(ValidationError):
            obs(payload=payload)
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}                                              # depth 6: accepted
    assert obs(payload=deep).payload["a"]["b"]["c"]["d"]["e"]["f"] == 1
    with pytest.raises(ValidationError):
        obs(payload={"a": deep})                                                                    # depth 7: refused
    o = obs(payload={"bars": [{"open": 1.0, "volume": 0, "inside_bar": True}], "source": {"name": "IEX", "bid_size": 3}})
    for mutate in (lambda: o.payload.__setitem__("order_qty", 5), lambda: o.payload["source"].update(side="SELL"),
                   lambda: o.payload.pop("bars"), lambda: o.payload["source"].setdefault("qty", 1), lambda: o.payload.clear()):
        with pytest.raises(TypeError):
            mutate()
    with pytest.raises((TypeError, AttributeError)):
        o.payload["bars"].append({"order": 1})                                                     # lists are stored as tuples
    original = {"close": 1.0}
    o2 = obs(payload={"bar": original})
    original["order"] = 1                                                                           # the caller's dict is not the stored one
    assert "order" not in o2.payload["bar"]
    assert o.model_dump(mode="json")["payload"] == {"bars": [{"open": 1.0, "volume": 0, "inside_bar": True}], "source": {"name": "IEX", "bid_size": 3}}
    assert Observation.model_validate_json(o.model_dump_json()).payload == o.payload                 # round trip, re-validated
    assert o.model_copy(deep=True).payload == o.payload


# ============================================================================================================ settings (T)
def test_T01_off_by_default_shadow_only_and_budgets_clamped():
    s = ST.settings()
    assert s == {"mode": "OFF", "configured_mode": "OFF", "budgets": {k: v[0] for k, v in ST.BUDGETS.items()}, "declared": [], "problems": []}
    assert ST.settings(SimpleNamespace(MARKET_INTEL_MODE="shadow"))["mode"] == "SHADOW"
    for raw in ("ACTIVE", "on", "LIVE"):
        out = ST.settings(SimpleNamespace(MARKET_INTEL_MODE=raw))
        assert out["mode"] == "OFF" and len(out["problems"]) == 1
    assert "does not exist" in ST.settings(SimpleNamespace(MARKET_INTEL_MODE="ACTIVE"))["problems"][0]
    assert MI.MODES == ("OFF", "SHADOW") and "ACTIVE" not in MI.MODES
    out = ST.settings(SimpleNamespace(MI_ALPACA_MAX_PER_MIN=41, MI_SEC_MAX_PER_MIN="30", MI_HALTS_MAX_PER_MIN=2, MI_FRED_MAX_PER_MIN=True))
    assert out["budgets"]["MI_ALPACA_MAX_PER_MIN"] == 40 and out["budgets"]["MI_SEC_MAX_PER_MIN"] == 30 and out["budgets"]["MI_HALTS_MAX_PER_MIN"] == 1
    assert out["budgets"]["MI_FRED_MAX_PER_MIN"] == 30 and len(out["problems"]) == 3
    assert ST.BUDGETS["MI_ALPACA_MAX_PER_MIN"][2] <= 0.20 * 200                                   # <= 20 % of Alpaca Basic, shared with the evaluator


# ============================================================================================================ freshness (Q)
PINNED = {"QUOTE_DAY": (5, 15, "REG", "SYMBOL"), "QUOTE_SWING": (60, 300, "REG", "SYMBOL"), "BAR_1MIN": (120, 300, "REG", "SYMBOL"),
          "BAR_1HOUR": (4200, 7800, "REG", "SYMBOL"), "NEWS_POLL": (180, 600, "EXTENDED", "SYMBOL"), "HALT_FEED": (120, 180, "REG", "ALL"),
          "SEC_POLL": (900, 1800, "SEC_06_22", "SYMBOL"), "EARNINGS_CALENDAR": (0, 129600, "ALWAYS", "SYMBOL")}


def test_Q01_every_age_based_kind_has_every_state_at_its_exact_boundaries():
    for key, p in FR.POLICIES.items():
        if key == "EARNINGS_CALENDAR":
            continue
        at = lambda age: FR.assess(key, REG_T - timedelta(seconds=age), REG_T)            # noqa: E731
        assert (at(p.fresh_s)["freshness"], at(p.fresh_s)["status"]) == ("FRESH", "AVAILABLE"), key
        assert (at(p.fresh_s + 1)["freshness"], at(p.fresh_s + 1)["status"]) == ("AGING", "AVAILABLE"), key
        assert (at(p.aging_s)["freshness"], at(p.aging_s + 1)["freshness"], at(p.aging_s + 1)["status"]) == ("AGING", "STALE", "STALE"), key
        assert at(p.aging_s + 1)["action"] == p.action and at(p.fresh_s)["action"] is None
        never = FR.assess(key, None, REG_T)
        assert (never["status"], never["reason"], never["action"]) == ("UNAVAILABLE", "NEVER_OBSERVED", p.action), key
        fut = FR.assess(key, REG_T + timedelta(seconds=3), REG_T)
        assert (fut["status"], fut["reason"]) == ("UNAVAILABLE", "FUTURE_TIMESTAMP"), key
        assert FR.assess(key, REG_T + timedelta(seconds=2), REG_T)["status"] == "AVAILABLE"     # within the 2 s skew
        off = FR.assess(key, SAT_T - timedelta(days=1), SAT_T)
        assert (off["status"], off["reason"], off["action"]) == ("NOT_APPLICABLE", "OUTSIDE_WINDOW", None), key
    assert FR.assess("HALT_FEED", REG_T - timedelta(seconds=181), REG_T)["scope"] == "ALL"          # fail closed: every new day candidate
    assert FR.assess("NEWS_POLL", None, REG_T)["action"].startswith("the catalyst flag reads UNAVAILABLE, never 'no news'")
    gap = FR.assess("BAR_1MIN", REG_T - timedelta(seconds=400), REG_T, prints_expected=False)
    assert (gap["status"], gap["reason"], gap["freshness"]) == ("INSUFFICIENT_DATA", "NO_PRINTS", None)   # IEX: no prints is not a stale feed
    pre = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)                                                 # 07:00 New York: PRE
    assert FR.assess("NEWS_POLL", pre - timedelta(seconds=10), pre)["status"] == "AVAILABLE" and FR.assess("QUOTE_DAY", pre, pre)["status"] == "NOT_APPLICABLE"
    late = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)                                                 # Monday 23:00 New York: SEC window closed
    assert FR.assess("SEC_POLL", late - timedelta(hours=2), late)["status"] == "NOT_APPLICABLE"


def test_Q02_the_numbers_are_pre_registered_and_equal_their_source_constants():
    assert {k: (p.fresh_s, p.aging_s, p.window, p.fail_closed_scope) for k, p in FR.POLICIES.items()} == PINNED
    assert FR.POLICY_VERSION == "mi_freshness_v2" and FR.CLOCK_SKEW_S == 2.0 == VA.CLOCK_SKEW_S and FR.MACRO_PENDING_ESCALATION_S == 86400.0
    import mean_reversion
    import setups
    import value_quality
    assert FR.POLICIES["QUOTE_SWING"].aging_s == setups.QUOTE_MAX_AGE_SECONDS == 300
    assert FR.FUNDAMENTALS_MAX_AGE_DAYS == value_quality.MAX_FUNDAMENTAL_AGE_DAYS == 200
    assert VA.DAY_JUMP == mean_reversion.NORMAL_DAY == 0.25
    assert (VA.FEED_DIVERGENCE_BPS, VA.WIDE_SPREAD_BPS, VA.STALE_PRINT_S, VA.NEWS_SKEW_S, VA.SESSION_HOLE_DAYS) == (10.0, 50.0, 60.0, 300.0, 2)
    assert (BU.RETRY_AFTER_CAP_S, BU.DEFAULT_BACKOFF_S, BU.PRESSURE_WINDOW_S, BU.PRESSURE_429S) == (30.0, 60.0, 600.0, 2)


def ny(y, m, d, hh, mm):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def test_Q03_the_date_based_kinds_daily_bar_earnings_macro_fundamentals():
    mon_am, mon_pm = ny(2026, 11, 2, 10, 0), ny(2026, 11, 2, 18, 0)
    assert FR.assess_daily_bar(date(2026, 10, 30), mon_am)["status"] == "AVAILABLE"                  # Monday morning: Friday's bar is the last expected
    assert FR.assess_daily_bar(date(2026, 10, 30), mon_pm)["reason"] == "SESSION_MISSING"            # from 18:00: Monday's own bar
    assert FR.assess_daily_bar(date(2026, 11, 2), mon_pm)["freshness"] == "FRESH"
    fri = ny(2026, 11, 27, 9, 0)                                                                     # the Friday after Thanksgiving (an early close)
    assert FR.assess_daily_bar(date(2026, 11, 25), fri)["status"] == "AVAILABLE"                    # Thursday was a holiday
    assert FR.assess_daily_bar(date(2026, 11, 25), ny(2026, 11, 27, 18, 0))["status"] == "STALE"
    assert FR.assess_daily_bar(None, mon_am)["reason"] == "NEVER_OBSERVED" and FR.assess_daily_bar(date(2026, 11, 3), mon_am)["reason"] == "FUTURE_TIMESTAMP"
    uncovered = FR.assess_daily_bar(date(2028, 3, 1), ny(2028, 3, 2, 10, 0))
    assert (uncovered["status"], uncovered["reason"]) == ("UNAVAILABLE", "CALENDAR_NOT_COVERED")       # fail closed until the table is extended
    e = FR.assess_earnings_calendar
    assert e(ny(2026, 11, 2, 6, 30), mon_pm)["freshness"] == "FRESH"
    assert e(ny(2026, 11, 1, 23, 30), ny(2026, 11, 2, 0, 30))["freshness"] == "AGING"               # yesterday's New York date, 1 h ago
    assert e(ny(2026, 11, 1, 6, 30), ny(2026, 11, 2, 18, 30))["freshness"] == "AGING"               # 36 h exactly
    stale = e(ny(2026, 11, 1, 6, 30), ny(2026, 11, 2, 18, 31))
    assert (stale["status"], stale["action"]) == ("STALE", FR.POLICIES["EARNINGS_CALENDAR"].action) and e(None, mon_pm)["status"] == "UNAVAILABLE"
    assert FR.assess("EARNINGS_CALENDAR", ny(2026, 11, 2, 6, 30), mon_pm)["freshness"] == "FRESH"
    rel = ny(2026, 11, 13, 8, 30)                                                                    # a scheduled 08:30 release
    assert FR.assess_macro(rel, rel, rel + timedelta(minutes=5))["freshness"] == "FRESH"
    assert FR.assess_macro(rel, rel - timedelta(days=30), rel + timedelta(hours=23))["reason"] == "RELEASE_PENDING"
    miss = FR.assess_macro(rel, None, rel + timedelta(days=1, seconds=1))
    assert (miss["status"], miss["reason"], miss["action"]) == ("STALE", "RELEASE_MISSING", FR.MACRO_ACTION)
    assert FR.assess_macro(None, None, rel)["reason"] == "NO_RELEASE_SCHEDULE" and FR.assess_macro(rel + timedelta(hours=1), None, rel)["reason"] == "FUTURE_TIMESTAMP"
    f = FR.assess_fundamentals
    assert f(date(2026, 4, 16), ny(2026, 11, 2, 10, 0))["status"] == "AVAILABLE" and f(date(2026, 4, 15), ny(2026, 11, 2, 10, 0))["status"] == "STALE"
    assert f(date(2026, 4, 16), ny(2026, 11, 2, 10, 0))["age_days"] == 200 and f(None, mon_am)["status"] == "UNAVAILABLE"
    assert f(date(2026, 12, 31), mon_am)["reason"] == "FUTURE_TIMESTAMP"


def test_Q04_an_unobserved_scheduled_macro_release_is_never_usable_and_every_assessment_says_usable_explicitly():
    rel = ny(2026, 11, 13, 8, 30)
    m = FR.assess_macro
    ok = m(rel, rel, rel)
    assert (ok["status"], ok["freshness"], ok["usable"]) == ("AVAILABLE", "FRESH", True)                 # observed at the release time exactly
    assert m(rel, rel + timedelta(seconds=1), rel + timedelta(seconds=2))["usable"] is True              # a later vintage of the release
    for now, prev in ((rel, rel - timedelta(days=30)), (rel, None), (rel + timedelta(seconds=1), rel - timedelta(seconds=1)),
                      (rel + timedelta(days=1), rel - timedelta(days=30))):
        pend = m(rel, prev, now)
        assert (pend["status"], pend["reason"], pend["usable"], pend["freshness"]) == ("UNAVAILABLE", "RELEASE_PENDING", False, None), now
        assert pend["action"] == FR.MACRO_ACTION
    miss = m(rel, rel - timedelta(days=30), rel + timedelta(days=1, seconds=1))
    assert (miss["status"], miss["reason"], miss["usable"], miss["age_s"]) == ("STALE", "RELEASE_MISSING", False, 86401.0)
    assert m(rel + timedelta(seconds=2), None, rel)["reason"] == "RELEASE_PENDING"                        # within the 2 s skew: due
    assert m(rel + timedelta(seconds=3), None, rel)["reason"] == "FUTURE_TIMESTAMP" and m(None, rel, rel)["usable"] is False
    assert not [r for r in (m(rel, x, rel + timedelta(hours=h)) for x in (None, rel - timedelta(days=1)) for h in (0, 1, 23, 24, 25))
                if r["usable"]]                                                                          # no usable AGING state for MACRO
    samples = [FR.assess(k, REG_T - timedelta(seconds=a), REG_T) for k in FR.POLICIES for a in (0, 10, 100, 10_000)]
    samples += [FR.assess_daily_bar(date(2026, 10, 30), ny(2026, 11, 2, 10, 0)), FR.assess_fundamentals(date(2026, 1, 1), REG_T), ok, miss]
    assert all(set(x) >= {"status", "usable"} and x["usable"] == (x["status"] == "AVAILABLE") for x in samples)


# ============================================================================================================ validation (V)
def test_V01_bar_and_series_codes():
    good = {"open": 10, "high": 11, "low": 9.5, "close": 10.5, "volume": 0}
    assert VA.bar_codes(good) == []
    assert VA.bar_codes({**good, "high": 10.2}) == ["HIGH_BELOW_BODY"] and VA.bar_codes({**good, "low": 10.1}) == ["LOW_ABOVE_BODY"]
    assert VA.bar_codes({**good, "volume": -1}) == ["NEGATIVE_VOLUME"] and VA.bar_codes({**good, "close": float("inf")}) == ["NON_FINITE"]
    assert "NON_POSITIVE_PRICE" in VA.bar_codes({**good, "low": 0})
    t0 = ny(2026, 11, 2, 9, 30)
    mins = [{**good, "ts": t0 + timedelta(minutes=k)} for k in (0, 1, 2, 5, 5, 4)]
    out = VA.minute_series_codes(mins)
    assert out["codes"] == ["DUPLICATE_TS", "GAP_MINUTES", "NOT_INCREASING"] and out["gap_minutes"] == 2
    assert out["per_bar"][4] == ["DUPLICATE_TS"] and out["per_bar"][5] == ["NOT_INCREASING"]
    pre = VA.minute_series_codes([{**good, "ts": ny(2026, 11, 2, 8, 0)}])
    assert pre["codes"] == ["OUTSIDE_SESSION"] and VA.minute_series_codes([{**good, "ts": ny(2026, 11, 2, 8, 0)}], regular_only=False)["codes"] == []
    assert VA.minute_series_codes([{**good, "ts": ny(2026, 11, 26, 10, 0)}], regular_only=False)["codes"] == ["OUTSIDE_SESSION"]   # Thanksgiving
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 9), date(2026, 11, 10)]
    closes = [100, 101, 102, 103, 140]
    d = VA.daily_series_codes([{**good, "open": c, "high": c, "low": c, "close": c, "day": x} for x, c in zip(days, closes)])
    assert d["codes"] == ["DAY_JUMP", "SESSION_HOLE"] and d["holes"] == [{"after": "2026-11-04", "before": "2026-11-09", "market_days": 2}]
    assert d["jumps"] == ["2026-11-10"] and d["blocked"] is True                                   # a hole blocks; a jump is only classified later
    hol = [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]                             # over Thanksgiving: no hole
    assert VA.daily_series_codes([{**good, "day": x} for x in hol]) == {"codes": [], "holes": [], "jumps": [], "non_session": [], "uncovered": [],
                                                                        "blocked": False}


def test_V02_quote_feed_and_clock_codes():
    q = {"bid": 100.0, "ask": 100.1, "quote_at": REG_T, "last_trade_at": REG_T - timedelta(seconds=10)}
    assert VA.quote_codes(q) == []
    assert VA.quote_codes({**q, "ask": 99.9}) == ["CROSSED"] and VA.quote_codes({**q, "ask": 100.0}) == ["LOCKED"]
    assert VA.quote_codes({**q, "ask": 100.6}) == ["WIDE_SPREAD"] and VA.quote_codes({**q, "bid": 0}) == ["NON_POSITIVE_QUOTE"]
    assert VA.quote_codes({**q, "last_trade_at": REG_T - timedelta(seconds=61)}) == ["STALE_PRINT"]
    assert VA.feed_divergence(100.11, 100.0) == ["FEED_DIVERGENCE"] and VA.feed_divergence(100.09, 100.0) == []
    assert VA.clock_skew(REG_T + timedelta(seconds=3), REG_T) == ["CLOCK_SKEW"] and VA.clock_skew(REG_T + timedelta(seconds=2), REG_T) == []
    assert VA.time_codes(REG_T + timedelta(seconds=5), REG_T, backfilled=True) == ["FUTURE_OBSERVED", "BACKFILLED"]


def test_V03_news_and_event_codes():
    import config
    item = {"headline": "Company reports results", "published_at": REG_T - timedelta(minutes=1), "retrieved_at": REG_T, "url": "https://www.sec.gov/x"}
    assert VA.news_codes(item, config.EVIDENCE_DENY_HOSTS) == []
    assert VA.news_codes({**item, "url": "https://www.reuters.com/a"}, config.EVIDENCE_DENY_HOSTS) == ["DENY_HOST_LINK"]   # subdomain of a denied host
    assert VA.news_codes({**item, "url": "https://notreuters.com/a"}, ("reuters.com",)) == []
    assert VA.news_codes({**item, "headline": "  "}, ()) == ["EMPTY_HEADLINE"] and VA.news_codes({**item, "published_at": None}, ()) == ["MISSING_PUBLISHED"]
    assert VA.news_codes({**item, "published_at": REG_T + timedelta(seconds=301)}, ()) == ["FUTURE_DATED"]
    assert VA.earnings_codes(date(2026, 10, 30), date(2026, 11, 2), confirmed_by_8k_202=False) == ["UNCONFIRMED_PAST"]
    assert VA.earnings_codes(date(2026, 10, 30), date(2026, 11, 2), confirmed_by_8k_202=True) == []
    assert VA.halt_codes({"halted_at": REG_T}) == ["HALTED_OPEN"] and VA.halt_codes({"halted_at": REG_T, "resumed_at": REG_T}) == []


def test_V04_every_code_is_declared_blocking_codes_are_a_subset_and_the_envelope_accepts_them():
    produced = {"HIGH_BELOW_BODY", "LOW_ABOVE_BODY", "NEGATIVE_VOLUME", "NON_FINITE", "NON_POSITIVE_PRICE", "DUPLICATE_TS", "GAP_MINUTES",
                "NOT_INCREASING", "OUTSIDE_SESSION", "DAY_JUMP", "SESSION_HOLE", "CROSSED", "LOCKED", "WIDE_SPREAD", "NON_POSITIVE_QUOTE",
                "STALE_PRINT", "FEED_DIVERGENCE", "CLOCK_SKEW", "FUTURE_OBSERVED", "BACKFILLED", "DENY_HOST_LINK", "EMPTY_HEADLINE",
                "MISSING_PUBLISHED", "FUTURE_DATED", "UNCONFIRMED_PAST", "HALTED_OPEN", "NON_SESSION_DAY", "CALENDAR_NOT_COVERED"}
    assert produced == set(VA.QUALITY_CODES) and VA.BLOCKING_CODES <= VA.QUALITY_CODES
    assert obs(quality_flags=tuple(sorted(VA.QUALITY_CODES))).quality_flags
    assert VA.blocking(["DAY_JUMP", "WIDE_SPREAD"]) is False and VA.blocking(["CROSSED"]) is True


def test_V05_uncovered_2024_and_2028_dates_and_bars_on_known_holidays_are_flagged_and_blocked():
    good = {"open": 10, "high": 11, "low": 9.5, "close": 10.5, "volume": 1}
    series = lambda days: VA.daily_series_codes([{**good, "day": d} for d in days])                     # noqa: E731
    y24 = series([date(2024, 6, 28), date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 5)])   # over 2024-07-04
    assert y24["codes"] == ["CALENDAR_NOT_COVERED"] and y24["blocked"] is True and y24["holes"] == []          # never "verified complete"
    assert "2024-07-03..2024-07-05" in y24["uncovered"] and "2024-06-28" in y24["uncovered"]
    edge = series([date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2)])                                # into the covered history
    assert edge["codes"] == ["CALENDAR_NOT_COVERED"] and edge["blocked"] is True
    assert series([date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)])["blocked"] is False                  # covered: verified clean
    y28 = series([date(2027, 12, 30), date(2027, 12, 31), date(2028, 1, 3)])
    assert y28["codes"] == ["CALENDAR_NOT_COVERED"] and y28["uncovered"] == ["2028-01-03"] and y28["blocked"] is True
    hol = series([date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 27), date(2026, 11, 28)])           # Thanksgiving, a Saturday
    assert hol["codes"] == ["NON_SESSION_DAY"] and hol["non_session"] == ["2026-11-26", "2026-11-28"] and hol["blocked"] is True
    assert series([date(2026, 4, 2), date(2026, 4, 6)])["codes"] == []                                       # Good Friday + weekend: no gap
    assert series([date(2027, 6, 17), date(2027, 6, 21)])["codes"] == []                                     # Juneteenth observed 2027-06-18
    two = series([date(2026, 11, 23), date(2026, 11, 27)])                                                   # 24th and 25th missing; 26th a holiday
    assert two["holes"] == [{"after": "2026-11-23", "before": "2026-11-27", "market_days": 2}] and two["blocked"] is True
    assert series([date(2026, 11, 24), date(2026, 11, 27)])["codes"] == []                                   # one missing day: below the threshold
    for t in (ny(2024, 7, 3, 10, 0), ny(2028, 3, 1, 10, 0)):                                                  # the 6.10A classifier would say REG
        m = VA.minute_series_codes([{**good, "ts": t}], regular_only=False)
        assert m["codes"] == ["CALENDAR_NOT_COVERED"] and VA.blocking(m["codes"]), t
    early = VA.minute_series_codes([{**good, "ts": ny(2026, 11, 27, 13, 30)}])                               # after the 13:00 early close
    assert early["codes"] == ["OUTSIDE_SESSION"]
    q = FR.assess("QUOTE_DAY", ny(2028, 3, 1, 10, 0), ny(2028, 3, 1, 10, 0))
    assert (q["status"], q["reason"], q["usable"]) == ("UNAVAILABLE", "CALENDAR_NOT_COVERED", False)
    assert FR.assess("NEWS_POLL", None, ny(2024, 7, 3, 7, 0))["reason"] == "CALENDAR_NOT_COVERED"
    assert FR.assess("QUOTE_DAY", ny(2028, 3, 4, 10, 0), ny(2028, 3, 4, 10, 0))["status"] == "NOT_APPLICABLE"   # a 2028 Saturday is still a weekend
    assert FR.assess_daily_bar(date(2024, 7, 3), ny(2024, 7, 8, 10, 0))["reason"] == "CALENDAR_NOT_COVERED"
    assert FR.assess_daily_bar(date(2024, 12, 31), ny(2025, 1, 2, 10, 0))["reason"] == "CALENDAR_NOT_COVERED"   # the expected session is in 2024


# ============================================================================================================ calendar (K)
def test_K01_sessions_holidays_early_closes_and_dst_days_come_from_the_stage_610a_classifier():
    from autorun import session as SES
    cases = [(ny(2026, 11, 2, 3, 59), "CLOSED"), (ny(2026, 11, 2, 4, 0), "PRE"), (ny(2026, 11, 2, 9, 29), "PRE"), (ny(2026, 11, 2, 9, 30), "REG"),
             (ny(2026, 11, 2, 15, 59), "REG"), (ny(2026, 11, 2, 16, 0), "POST"), (ny(2026, 11, 2, 19, 59), "POST"), (ny(2026, 11, 2, 20, 0), "CLOSED"),
             (ny(2026, 11, 26, 11, 0), "CLOSED"),                                                        # Thanksgiving
             (ny(2026, 11, 27, 12, 59), "REG"), (ny(2026, 11, 27, 13, 0), "POST"),                       # early close 13:00
             (ny(2026, 10, 31, 11, 0), "CLOSED"), (ny(2026, 11, 1, 11, 0), "CLOSED"),                    # weekend; the DST-change Sunday
             (datetime(2026, 3, 9, 13, 30, tzinfo=UTC), "REG"),                                           # 09:30 EDT, the Monday after DST starts
             (datetime(2026, 11, 2, 14, 30, tzinfo=UTC), "REG"), (datetime(2026, 11, 2, 13, 30, tzinfo=UTC), "PRE")]   # 09:30 / 08:30 EST
    for t, want in cases:
        assert CAL.session_of(t) == want, t
    mapping = {"PREMARKET": "PRE", "REGULAR": "REG", "AFTER_HOURS": "POST", "CLOSED": "CLOSED"}
    t = ny(2026, 11, 27, 0, 0)
    for _ in range(0, 24 * 60, 7):                                                                   # one source of truth, minute by minute -
        want = mapping[SES.session_of(t)]                                                            # except the ONE declared Stage 7.1 exception:
        if want == "POST" and t >= ny(2026, 11, 27, 17, 0):                                          # late trading ends at 17:00 on an NYSE early close
            want = "CLOSED"                                                                          # (DESIGN_71 section 5; 6.10A itself is unchanged)
        assert CAL.session_of(t) == want
        t += timedelta(minutes=7)
    s = CAL.session(ny(2026, 11, 27, 10, 0))
    assert (s["session_date"], s["close_minute"], s["covered"], s["calendar"]) == (date(2026, 11, 27), 13 * 60, True, CAL.CALENDAR_VERSION)
    assert (CAL.regular_minutes(date(2026, 11, 2)), CAL.regular_minutes(date(2026, 11, 27)), CAL.regular_minutes(date(2026, 11, 26))) == (390, 210, 0)
    assert CAL.previous_market_day(date(2026, 11, 27)) == date(2026, 11, 25) and CAL.next_market_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert CAL.last_completed_session(ny(2026, 11, 2, 17, 59)) == date(2026, 10, 30) and CAL.last_completed_session(ny(2026, 11, 2, 18, 0)) == date(2026, 11, 2)
    assert CAL.session(ny(2028, 1, 4, 10, 0))["covered"] is False                                     # an uncovered year is reported


def test_K02_the_holiday_rules_reproduce_the_static_2026_2027_table():
    from autorun import calendar as AC
    for y in AC.COVERED_YEARS:
        assert CAL.rule_holidays(y) == {d for d in AC.HOLIDAYS if d.year == y}, y
    assert date(2027, 12, 31) not in CAL.rule_holidays(2027) and date(2028, 1, 1) not in CAL.rule_holidays(2028)   # a Saturday New Year is not moved


def test_K03_uncovered_weekdays_are_not_covered_never_guessed_and_known_holidays_are_holidays():
    st = CAL.market_day_status
    assert [st(d) for d in (date(2024, 7, 4), date(2024, 1, 9), date(2028, 1, 3), date(2024, 7, 6), date(2028, 1, 1))] == [
        "NOT_COVERED", "NOT_COVERED", "NOT_COVERED", "WEEKEND", "WEEKEND"]                       # 2024: outside every verified table
    assert [st(d) for d in (date(2026, 11, 26), date(2026, 4, 3), date(2027, 6, 18), date(2026, 7, 3), date(2027, 12, 24))] == ["HOLIDAY"] * 5
    assert st(date(2026, 11, 27)) == "MARKET_DAY" and st(date(2026, 11, 2)) == "MARKET_DAY" and st(None) == "NOT_COVERED"
    with pytest.raises(CAL.CalendarNotCovered):
        CAL.is_market_day(date(2024, 7, 3))
    with pytest.raises(CAL.CalendarNotCovered):
        CAL.is_market_day(date(2028, 1, 3))
    assert CAL.is_market_day(date(2028, 1, 1)) is False and CAL.is_market_day(date(2026, 11, 26)) is False
    assert CAL.previous_market_day(date(2025, 1, 2)) is None                                       # 2025-01-01 holiday, then 2024: unknown
    assert CAL.next_market_day(date(2027, 12, 31)) is None and CAL.next_market_day(date(2027, 12, 23)) == date(2027, 12, 27)
    assert CAL.regular_minutes(date(2024, 7, 3)) is None and CAL.regular_minutes(date(2024, 7, 6)) == 0
    assert CAL.last_completed_session(ny(2025, 1, 2, 10, 0)) is None and CAL.last_completed_session(ny(2028, 3, 1, 19, 0)) is None
    s24 = CAL.session(ny(2024, 7, 3, 10, 0))
    assert (s24["session"], s24["day_status"], s24["market_day"], s24["covered"]) == ("REG", "NOT_COVERED", None, False)   # never claimed


# ============================================================================================================ budgets (B)
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += timedelta(seconds=s)


def test_B01_token_bucket_on_an_injected_clock():
    c = Clock(REG_T)
    b = BU.TokenBucket(60, c)
    assert all(b.try_take() for _ in range(60)) and not b.try_take()                                # a burst never exceeds one minute
    assert b.wait_s() == pytest.approx(1.0)
    c.tick(0.5)
    assert not b.try_take()
    c.tick(0.5)
    assert b.try_take()
    c.t = c.t - timedelta(hours=1)                                                                    # a clock stepping back mints nothing
    assert not b.try_take()
    c.tick(3600 + 600)
    assert b.tokens <= b.capacity and b.wait_s(61) is None and BU.TokenBucket(0, c).wait_s() is None


def test_B02_provider_budgets_daily_cap_back_off_cycle_block_and_plan_pressure():
    c = Clock(ny(2026, 11, 2, 23, 58))
    av = BU.ProviderBudget("ALPHA_VANTAGE", 60, c, daily_cap=2)
    assert av.allow() == (True, "OK") and av.allow() == (True, "OK") and av.allow() == (False, "DAILY_CAP")
    c.tick(180)                                                                                       # New York midnight: a new day
    assert av.allow() == (True, "OK") and av.status()["used_today"] == 1
    p = BU.ProviderBudget("SEC", 60, c)
    p.record(429, retry_after_s=120)
    assert p.allow() == (False, "BLOCKED_BACKOFF")
    c.tick(30)
    assert p.allow() == (True, "OK")                                                                  # Retry-After capped at 30 s
    p.record(403)
    c.tick(59)
    assert p.allow()[1] == "BLOCKED_BACKOFF"
    c.tick(1)
    assert p.allow()[0] is True
    p.record(access_denied=True)
    c.tick(3600)
    assert p.allow() == (False, "BLOCKED_CYCLE")
    p.new_cycle()
    assert p.allow()[0] is True
    q = BU.ProviderBudget("ALPACA", 40, c)
    q.record(429, retry_after_s=1)
    c.tick(599)
    q.record(429, retry_after_s=1)
    st = q.status()
    assert st["flags"] == ["PLAN_LIMIT_PRESSURE"] and st["per_minute"] == 20.0                       # two 429s in 10 minutes halve the rate
    q.clear_pressure()
    assert q.status()["per_minute"] == 40.0 and q.status()["flags"] == []
    r = BU.ProviderBudget("FRED", 30, c)
    r.record(429)
    c.tick(601)
    r.record(429)
    assert r.status()["flags"] == []                                                                   # 10 minutes apart: no pressure
    assert BU.ProviderBudget("X", 0, c).allow() == (False, "DISABLED")
    d = BU.default_budgets(c)
    assert {k: (v.base_per_minute, v.daily_cap) for k, v in d.items()} == {"ALPACA": (40.0, None), "ALPACA_BACKFILL": (60.0, None), "SEC": (60.0, None),
                                                                           "FRED": (30.0, None), "NASDAQ_TRADER": (1.0, None), "ALPHA_VANTAGE": (1.0, 6)}


# ============================================================================================================ isolation (I)
ALLOWED_IMPORTS = {"__future__", "math", "re", "datetime", "typing", "dataclasses", "collections", "collections.abc", "urllib.parse", "pydantic", "market_intel",
                   "market_intel.calendar", "market_intel.settings", "market_intel.validate", "autorun", "autorun.calendar", "autorun.session",
                   "api.validation", "config", "zoneinfo", "market_intel.calendar_history", "setups.market_hours"}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
        elif isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
    return out


def test_I01_pure_contracts_only_pure_imports_no_io_and_nothing_imports_them():
    files = [ROOT / "market_intel" / n for n in ("__init__.py", "budget.py", "calendar.py", "freshness.py", "models.py", "settings.py", "validate.py",
                                                 "calendar_history.py")]            # the pure contracts; the Stage 7.1 store: its own tests
    assert all(f.is_file() for f in files)
    for f in files:
        assert _imports(f) <= ALLOWED_IMPORTS, (f.name, sorted(_imports(f) - ALLOWED_IMPORTS))
        src = f.read_text(encoding="utf-8")
        calls = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
                 and isinstance(n.func, (ast.Name, ast.Attribute))}
        assert not calls & {"open", "connect", "urlopen", "Thread", "start", "mkdir", "write_text", "write_bytes", "unlink", "system", "Popen"}, f.name
        for word in ("sqlite3", "subprocess", "anthropic", "submit_order", "os.environ"):            # modules themselves: the import scan
            assert word not in src, (f.name, word)
    users = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("market_intel/", "tests/", ".venv/", "scratchpad/")) or "/site-packages/" in rel:
            continue
        if "market_intel" in p.read_text(encoding="utf-8", errors="replace"):
            users.append(rel)
    assert users == []                                                                                # display-only: no live path reads it
    assert not (ROOT / "data" / "market_intel.db").exists()


# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_70}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_70} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #41 (Stage 7.0 merged; the Stage 7.1 base): what Stage 7.0 left, whatever a later stage changed since."""
    run = subprocess.run(["git", "show", f"{FL.BASE_71}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_71} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def test_F01_the_change_set_is_exactly_the_declared_files_and_only_tests_and_the_ci_manifest_are_edited():
    assert FL.BASE_70 == "13c1635ca77bcd028d3e0aa9bf821376e4860905" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_70}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_70).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    stage = set(_git("diff", "--name-only", FL.BASE_70, FL.BASE_71).split())                # Stage 7.0 itself, between two fixed commits
    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}
    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stages 7.1-7.3: pinned by their own F tests
    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),
                                                         "missing": sorted((FL.NEW_70 | FL.EDITED_70 | later) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_70).split())
    assert {p for p in stage if p not in in_base} == FL.NEW_70 and {p for p in stage if p in in_base} == FL.EDITED_70
    assert FL.NEW_70 == {"DESIGN_70_MARKET_INTEL_CONTRACTS.md", "tests/test_market_intel_70.py"} | {f"market_intel/{n}" for n in (
        "__init__.py", "budget.py", "calendar.py", "freshness.py", "models.py", "settings.py", "validate.py")}
    assert {p for p in FL.EDITED_70 if not p.startswith("tests/")} == {".github/ci/pytest_groups.json"}     # no application file is edited
    assert FL.SEMANTIC_70 == {} and FL.INSTRUMENTATION_70 == {}
    for rel in sorted(FL.NEW_70 | FL.EDITED_70):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in post(rel) and m54 not in post(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_config_declares_no_market_intel_setting():
    import sys
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import config
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_"))] and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert now_text("config.py") == at_base("config.py") and now_text("api/server.py") == at_base("api/server.py")


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_70.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_70) == FL.EDITED_70 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.0 (market intelligence contracts")
    para = fl[start:fl.index('"""', start)]                                                 # the paragraph and the blank line before it
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_542 + NEW_70 plus this ledger.", " + NEW_542 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_70 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_70: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.0 left
    assert set(new_led) == set(old)                                                         # no new ledger key
    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_70 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_70 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('\ndef now_text(rel):\n',
         '\ndef post(rel):\n    """The Stage 7.0 base (PR #40\'s head, Stage 5.4.2 complete): what Stage 5.4.2 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_70}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_70} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\ndef now_text(rel):\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542), "missing": sorted(FL.NEW_542 | FL.EDITED_542 - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_542).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_542, FL.BASE_70).split())               # Stage 5.4.2 itself, between two fixed commits\n    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n                                                           "missing": sorted((FL.NEW_542 | FL.EDITED_542 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_542).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_542).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_542 and {p for p in changed if p in in_base} == FL.EDITED_542\n    assert set(FL.SEMANTIC_542) == {"evidence/sources/sec_edgar.py"} and FL.INSTRUMENTATION_542 == {}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_542).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_542 and {p for p in stage if p in in_base} == FL.EDITED_542\n    assert set(FL.SEMANTIC_542) == {"evidence/sources/sec_edgar.py"} and FL.INSTRUMENTATION_542 == {}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel and rel != "evidence/sources/sec_edgar.py":\n                assert now_text(rel) == at_base(rel), rel                               # no trading, safety, startup, UI or harness file changed\n    cfg_base, cfg_now = at_base("config.py"), now_text("config.py")\n    assert cfg_now.startswith(cfg_base) and cfg_now.count("# >>> Evidence refresh, Stage 5.4.2") == 1\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel and rel != "evidence/sources/sec_edgar.py":\n                assert post(rel) == at_base(rel), rel                                   # no trading, safety, startup, UI or harness file changed\n    cfg_base, cfg_now = at_base("config.py"), post("config.py")\n    assert cfg_now.startswith(cfg_base) and cfg_now.count("# >>> Evidence refresh, Stage 5.4.2") == 1\n'),
        ('    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_542   # only the new settings, appended\n    srv = now_text("api/server.py")\n    assert srv.count("# >>> Evidence refresh, Stage 5.4.2") == 3 and _strip_blocks(srv) == at_base("api/server.py")   # three marked blocks, nothing else\n',
         '    assert [l.split(":")[0] for l in cfg_now[len(cfg_base):].splitlines() if l and not l.startswith("#")] == CONFIG_542   # only the new settings, appended\n    srv = post("api/server.py")\n    assert srv.count("# >>> Evidence refresh, Stage 5.4.2") == 3 and _strip_blocks(srv) == at_base("api/server.py")   # three marked blocks, nothing else\n'),
        ('        sec = sec.replace(old, new)\n    assert sec == now_text("evidence/sources/sec_edgar.py")                               # the SEC cap: three exact edits\n    for rel in sorted(FL.NEW_542 | FL.EDITED_542):\n',
         '        sec = sec.replace(old, new)\n    assert sec == post("evidence/sources/sec_edgar.py")                               # the SEC cap: three exact edits\n    for rel in sorted(FL.NEW_542 | FL.EDITED_542):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel             # no marker another freeze test scans for\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel                     # no marker another freeze test scans for\n\n'),
        ('    for rel, edits in REPLAY_542.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_542.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_542) == FL.EDITED_542 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    para = fl[fl.index("\\nStage 5.4.2 (automatic evidence refresh"):fl.index(\'"""\', fl.index("\\nStage 5.4.2 (automatic evidence refresh"))]\n',
         '    assert set(REPLAY_542) == FL.EDITED_542 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    para = fl[fl.index("\\nStage 5.4.2 (automatic evidence refresh"):fl.index(\'"""\', fl.index("\\nStage 5.4.2 (automatic evidence refresh"))]\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) - set(old) == {"evidence/sources/sec_edgar.py"}                 # one new key: the SEC adapter (read as on main by the earlier freeze tests)\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_542 & set(old)         # every re-hash is a declared Stage 5.4.2 edit\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 5.4.2 left\n    assert set(new_led) - set(old) == {"evidence/sources/sec_edgar.py"}                   # one new key: the SEC adapter (read as on main by the earlier freeze tests)\n    assert {k for k in old if old[k] != new_led[k]} == FL.EDITED_542 & set(old)           # every re-hash is a declared Stage 5.4.2 edit\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
