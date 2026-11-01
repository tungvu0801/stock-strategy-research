"""
market_scan/flags.py - Stage 7.5: the shared scanner's flags, PURE (DESIGN_75_MARKET_SCAN.md section 5; master design D.1, C.5, C.11).

Each flag is a value with the reason it holds; a flag that cannot be established says so and is never read as its favourable value:
* halt_flag           TRUE (an open halt today, or a resumption less than 10 minutes ago) / FALSE (the halt feed is current - the 7.0
                      HALT_FEED policy - and shows neither) / UNKNOWN (the feed is not current: fail closed for the day horizon);
* catalyst_flag       TRUE (a catalyst-eligible PRIMARY news mention - confidence >= 0.8, tier <= 3, the 7.2 rule - of an article
                      published in the last 18 h, from a mention run known at T) / NOT_ESTABLISHED. Never FALSE: the 8-K and earnings
                      release parts read SEC acceptance times, which stay BLOCKED (SEC_ACCEPTANCE_TZ_UNVERIFIED), so "no catalyst" is
                      never established;
* earnings_proximity  the market sessions to the next expected earnings date of the latest calendar snapshot known at T (the 7.2
                      StoreEarningsProvider), when the 7.0 EARNINGS_CALENDAR policy says the calendar is current; else UNKNOWN
                      (EARNINGS_UNKNOWN). Expected dates only (SEC confirmations are blocked);
* regime              rotation_walkforward.regimes (SPY close vs SMA200; 20-session realised volatility vs 0.20), unchanged, at the last
                      completed session, plus the macro flags: curve inversion (T10Y2Y < 0) for display only - the 7.0 MACRO policy needs a
                      release schedule no source records, so it is never usable - and the VIX percentile, which is not pre-registered.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from market_intel import AVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE
from market_intel import calendar as CAL
from market_intel import freshness as FR
from market_scan import (CATALYST_WINDOW_S, FALSE, HALT_RESUMPTION_S, NOT_ESTABLISHED, REGIME_TREND_SESSIONS, REGIME_VOL_SESSIONS, SEC_BLOCKED,
                         TRUE, UNKNOWN)
from market_scan.features import DIGITS, at, num


def flag(value: str, codes: Sequence[str] = (), **extra) -> Dict[str, Any]:
    return {"value": value, "codes": sorted(set(codes)), **extra}


def halt_flag(halts: Sequence[dict], feed: Dict[str, Any], *, today: Optional[date], now: datetime) -> Dict[str, Any]:
    """`halts`: the symbol's halt facts known at `now` (latest known revision each, usable); `feed`: the 7.0 HALT_FEED assessment."""
    hits: List[dict] = []
    for h in halts:
        p = h.get("payload") or {}
        halted, resumed = at(p.get("halted_at")), at(p.get("resumed_at"))
        if halted is None or CAL.session(halted)["session_date"] != today:
            continue
        if resumed is None or resumed > now:
            hits.append({"halted_at": halted.isoformat(), "resumed_at": None if resumed is None else resumed.isoformat(), "state": "HALT_OPEN",
                         "obs_id": h.get("obs_id")})
        elif (now - resumed).total_seconds() < HALT_RESUMPTION_S:
            hits.append({"halted_at": halted.isoformat(), "resumed_at": resumed.isoformat(), "state": "RESUMED_WITHIN_10_MIN", "obs_id": h.get("obs_id")})
    feed_brief = {"status": feed["status"], "reason": feed["reason"], "age_s": feed.get("age_s")}
    if hits:
        return flag(TRUE, [x["state"] for x in hits], halts=hits, feed=feed_brief)
    if feed["usable"]:
        return flag(FALSE, feed=feed_brief)
    return flag(UNKNOWN, [f"HALT_FEED_{feed['reason']}"], feed=feed_brief)


def catalyst_flag(mentions: Optional[Sequence[dict]], *, news_poll: Dict[str, Any], runs_known: int) -> Dict[str, Any]:
    """`mentions`: catalyst-eligible mentions of the symbol in articles published within 18 h before T, from mention runs known at T; None
    when the store has no news-mention tables (schema v1)."""
    poll = {"status": news_poll["status"], "reason": news_poll["reason"], "age_s": news_poll.get("age_s")}
    if mentions is None:
        return flag(NOT_ESTABLISHED, ["NEWS_MENTIONS_NOT_IN_SCHEMA_V1", SEC_BLOCKED], news_poll=poll, window_s=CATALYST_WINDOW_S)
    if mentions:
        return flag(TRUE, ["PRIMARY_NEWS_MENTION"], articles=list(mentions), news_poll=poll, window_s=CATALYST_WINDOW_S)
    codes = ["NO_CATALYST_MENTION_KNOWN", SEC_BLOCKED] + ([] if runs_known else ["NO_MENTION_RUN_KNOWN"])
    return flag(NOT_ESTABLISHED, codes, news_poll=poll, window_s=CATALYST_WINDOW_S)


def earnings_proximity(result: Any, calendar: Dict[str, Any], *, today: Optional[date]) -> Dict[str, Any]:
    """`result`: the StoreEarningsProvider's get_upcoming_earnings answer (or None); `calendar`: the 7.0 EARNINGS_CALENDAR assessment."""
    cal = {"status": calendar["status"], "reason": calendar["reason"], "age_s": calendar.get("age_s")}
    if not calendar["usable"]:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", f"EARNINGS_CALENDAR_{calendar['reason']}"], sessions=None, calendar=cal)
    if result is None or not getattr(result, "available", False):
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "NO_CALENDAR_ROW"], sessions=None, calendar=cal)
    days = sorted(e.event_date for e in result.events if today is not None and e.event_date >= today)
    if not days:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "NO_UPCOMING_DATE_IN_CALENDAR"], sessions=None, calendar=cal)
    target, n, x = days[0], 0, today
    while x < target:                                              # the market days after `today` up to the report date
        x = CAL.next_market_day(x)
        if x is None:
            return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "CALENDAR_NOT_COVERED"], sessions=None, calendar=cal, report_date=target.isoformat())
        n += x <= target
    return flag("KNOWN", ["EXPECTED_NOT_CONFIRMED", SEC_BLOCKED], sessions=n, report_date=target.isoformat(), time_precision="DATE_ONLY", calendar=cal)


def regime(spy: Sequence[dict], daily: Dict[str, Any], *, last_session: Optional[date], macro: Optional[dict], now: datetime) -> Dict[str, Any]:
    """`spy`: SPY's raw daily bars, oldest first, the contiguous run of market days ending at `last_session`; `daily`: SPY's 7.0 daily-bar
    assessment; `macro`: the latest usable T10Y2Y value known at `now`, or None."""
    from backtest.replay import BarSeries
    from rotation_walkforward import regimes as RG
    trend = vol = RG.UNKNOWN
    codes: List[str] = []
    if not daily["usable"]:
        codes.append(f"SPY_DAILY_{daily['reason']}")
    elif not spy or spy[-1]["session_date"] != last_session:
        codes.append("SPY_LAST_SESSION_MISSING")
    else:
        rows = [(b["session_date"].isoformat(), b["start"].isoformat(), b["open"], b["high"], b["low"], b["close"], b["volume"])
                for b in spy[-REGIME_TREND_SESSIONS:]]
        lab = RG.labels(BarSeries("SPY", rows))[last_session.isoformat()]         # UNKNOWN before 200 / 21 closes: the module's own rule
        trend, vol = lab["trend"], lab["vol"]
        codes += [] if len(spy) >= REGIME_TREND_SESSIONS else ["FEWER_THAN_200_SESSIONS"]
        codes += [] if len(spy) >= REGIME_VOL_SESSIONS else ["FEWER_THAN_21_SESSIONS"]
    m = FR.assess_macro(None, None, now)                     # no source records a release schedule: the 7.0 policy never makes it usable
    curve = {"series": "T10Y2Y", "value": None, "inverted": None, "status": m["status"], "reason": m["reason"], "display_only": True}
    v = num((macro or {}).get("payload", {}).get("value"))
    if v is not None:
        curve.update(value=round(v, DIGITS), inverted=v < 0, observed_at=macro.get("observed_at"), obs_id=macro.get("obs_id"))
    return {"spy_trend": trend, "spy_vol": vol, "codes": sorted(set(codes)), "sessions": len(spy), "as_of_session": None if last_session is None else
            last_session.isoformat(), "macro": {"curve_inversion": curve, "vix_percentile": {"status": NOT_APPLICABLE, "reason": "NOT_PRE_REGISTERED"}},
            "status": AVAILABLE if trend != RG.UNKNOWN or vol != RG.UNKNOWN else INSUFFICIENT_DATA}
