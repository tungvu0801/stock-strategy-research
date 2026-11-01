"""
market_scan/flags.py - Stage 7.5: the shared scanner's flags, PURE (DESIGN_75_MARKET_SCAN.md section 5; master design D.1, C.5, C.11).

Each flag is a value with the reason it holds; a flag that cannot be established says so and is never read as its favourable value:
* halt_flag           TRUE (a halt open at T - whenever it began: an unresolved halt from an earlier session is open today - or a
                      resumption less than 10 minutes ago; a resumption time that cannot be read keeps it open) / FALSE (the halt feed is
                      current - the 7.0 HALT_FEED policy - and shows neither) / UNKNOWN (the feed is not current: fail closed for the day
                      horizon). The halt time is the row's validated observed_at, never the payload's;
* catalyst_flag       TRUE (a catalyst-eligible PRIMARY news mention - confidence >= 0.8, tier <= 3, the 7.2 rule - of an article
                      published in the last 18 h, from the mention run that read the article's latest revision known at T) /
                      NOT_ESTABLISHED / UNAVAILABLE (the news poll is not current, C.5: never "no news"). Never FALSE: the 8-K and
                      earnings-release parts read SEC acceptance times, which stay BLOCKED (SEC_ACCEPTANCE_TZ_UNVERIFIED);
* earnings_proximity  the market sessions to the next expected earnings date in the LATEST calendar snapshot known at T (a symbol the latest
                      snapshot no longer lists is unknown, whatever an older snapshot said), when the 7.0 EARNINGS_CALENDAR policy says
                      the calendar is current; else UNKNOWN (EARNINGS_UNKNOWN). Expected dates only (SEC confirmations are blocked);
* regime              rotation_walkforward.regimes (SPY close vs SMA200; 20-session realised volatility vs 0.20), unchanged, at the last
                      completed session, plus the macro flags: curve inversion (T10Y2Y < 0) for display only - the 7.0 MACRO policy needs a
                      release schedule no source records, so it is never usable - and the VIX percentile, which is not pre-registered.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from market_intel import AVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE, UNAVAILABLE
from market_intel import calendar as CAL
from market_intel import freshness as FR
from market_scan import (CATALYST_WINDOW_S, FALSE, HALT_RESUMPTION_S, NOT_ESTABLISHED, REGIME_TREND_SESSIONS, REGIME_VOL_SESSIONS, SEC_BLOCKED,
                         TRUE, UNKNOWN)
from market_scan.features import DIGITS, at, num


def flag(value: str, codes: Sequence[str] = (), **extra) -> Dict[str, Any]:
    return {"value": value, "codes": sorted(set(codes)), **extra}


def halt_flag(halts: Sequence[dict], feed: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    """`halts`: every halt fact of the symbol known at `now` (latest known revision each, usable; observed_at is the store-validated halt
    time); `feed`: the 7.0 HALT_FEED assessment."""
    hits: List[dict] = []
    for h in halts:
        p = h.get("payload") or {}
        raw = p.get("resumed_at")
        resumed = at(raw)
        state = None
        if raw is not None and resumed is None:
            state = "HALT_RESUMPTION_UNREADABLE"                       # never read as resumed
        elif resumed is None or resumed > now:
            state = "HALT_OPEN"
        elif (now - resumed).total_seconds() < HALT_RESUMPTION_S:
            state = "RESUMED_WITHIN_10_MIN"
        if state:
            hits.append({"halted_at": h.get("observed_at"), "resumed_at": None if resumed is None else resumed.isoformat(), "state": state,
                         "obs_id": h.get("obs_id")})
    feed_brief = {"status": feed["status"], "reason": feed["reason"], "age_s": feed.get("age_s")}
    if hits:
        return flag(TRUE, [x["state"] for x in hits], halts=hits, feed=feed_brief)
    if feed["usable"]:
        return flag(FALSE, feed=feed_brief)
    return flag(UNKNOWN, [f"HALT_FEED_{feed['reason']}"], feed=feed_brief)


def catalyst_flag(mentions: Optional[Sequence[dict]], *, news_poll: Dict[str, Any], runs_known: int) -> Dict[str, Any]:
    """`mentions`: catalyst-eligible mentions of the symbol in articles published within 18 h before T, each from the mention run that read
    the article's latest revision known at T; None when the store has no news-mention tables (schema v1)."""
    poll = {"status": news_poll["status"], "reason": news_poll["reason"], "age_s": news_poll.get("age_s")}
    if mentions:
        return flag(TRUE, ["PRIMARY_NEWS_MENTION"], articles=list(mentions), news_poll=poll, window_s=CATALYST_WINDOW_S)
    codes = [SEC_BLOCKED] + (["NEWS_MENTIONS_NOT_IN_SCHEMA_V1"] if mentions is None else ["NO_CATALYST_MENTION_KNOWN"])
    codes += [] if runs_known or mentions is None else ["NO_MENTION_RUN_KNOWN"]
    if not news_poll["usable"]:                                        # C.5: the flag reads UNAVAILABLE, never "no news"
        return flag(UNAVAILABLE, codes + [f"NEWS_POLL_{news_poll['reason']}"], news_poll=poll, window_s=CATALYST_WINDOW_S)
    return flag(NOT_ESTABLISHED, codes, news_poll=poll, window_s=CATALYST_WINDOW_S)


def earnings_proximity(rows: Optional[Sequence[dict]], calendar: Dict[str, Any], *, today: Optional[date], snapshot: Optional[str],
                       evidence=lambda r: None) -> Dict[str, Any]:
    """`rows`: the symbol's usable EARNINGS rows in the latest calendar snapshot known at T (`snapshot`, its New York date; None when no
    snapshot is known); `calendar`: the 7.0 EARNINGS_CALENDAR assessment; `today`: the reference date (None: not covered)."""
    cal = {"status": calendar["status"], "reason": calendar["reason"], "age_s": calendar.get("age_s"), "snapshot_date": snapshot}
    if not calendar["usable"]:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", f"EARNINGS_CALENDAR_{calendar['reason']}"], sessions=None, calendar=cal)
    if today is None:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "CALENDAR_NOT_COVERED"], sessions=None, calendar=cal)
    if snapshot is None:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "NO_CALENDAR_SNAPSHOT"], sessions=None, calendar=cal)
    if not rows:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "NOT_IN_LATEST_CALENDAR"], sessions=None, calendar=cal)
    up = sorted((d, r) for r in rows for d in [_day(r)] if d is not None and d >= today)
    if not up:
        return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "NO_UPCOMING_DATE_IN_CALENDAR"], sessions=None, calendar=cal)
    target, row = up[0][0], up[0][1]
    n, x = 0, today
    while x < target:                                              # the market days after `today` up to the report date
        x = CAL.next_market_day(x)
        if x is None:
            return flag(UNKNOWN, ["EARNINGS_UNKNOWN", "CALENDAR_NOT_COVERED"], sessions=None, calendar=cal, report_date=target.isoformat())
        n += x <= target
    return flag("KNOWN", ["EXPECTED_NOT_CONFIRMED", SEC_BLOCKED], sessions=n, report_date=target.isoformat(), time_precision="DATE_ONLY", calendar=cal,
                obs_id=row.get("obs_id"), evidence=evidence(row))


def _day(r: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str((r.get("payload") or {}).get("report_date")))
    except ValueError:
        return None


def regime(spy: Sequence[dict], daily: Dict[str, Any], *, last_session: Optional[date], macro: Optional[dict], now: datetime) -> Dict[str, Any]:
    """`spy`: SPY's raw daily bars, oldest first, the contiguous run of market days ending at `last_session`; `daily`: SPY's 7.0 daily-bar
    assessment; `macro`: the latest usable T10Y2Y value known at `now`, or None."""
    from backtest.replay import BarSeries
    from rotation_walkforward import regimes as RG
    trend = vol = RG.UNKNOWN
    codes: List[str] = []
    if not daily["usable"]:                                  # current means the last completed session's bar is present, so the run ends there
        codes.append(f"SPY_DAILY_{daily['reason']}")
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
