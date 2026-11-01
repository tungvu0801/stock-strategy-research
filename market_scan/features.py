"""
market_scan/features.py - Stage 7.5: the shared scanner's feature maths, PURE (DESIGN_75_MARKET_SCAN.md section 4; master design D.1).

Every function takes values the run has already read point in time and returns a Feature: {"value", "status", "codes", "unit", ...}. Nothing
here reads a store, a clock, a file or the network. A value that cannot be computed exactly as pre-registered is never estimated: the
status says why (INSUFFICIENT_DATA / NOT_APPLICABLE) and the value is None. A number that is not finite (NaN, an infinity, text, a bool)
is never a value. A feature computed from graded inputs carries the worst of their states (graded()): an old quote gives a STALE gap,
shown but never used.

Inputs, as the store returns them:
* a snapshot is a QUOTE_SNAPSHOT payload (Stage 7.2): bid, ask, quote_at, last_price, last_trade_at and the embedded minute_bar,
  daily_bar and prev_daily_bar ({start, open, high, low, close, volume, ...}), all from ONE provider response, so one feed;
* a bar is {"start": aware datetime, "session_date": date, "open", "high", "low", "close", "volume"} from a BAR_1MIN / BAR_1DAY row.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from market_intel import AVAILABLE, INSUFFICIENT_DATA, NOT_APPLICABLE, POST, PRE, REG, STALE, UNAVAILABLE
from market_intel import calendar as CAL
from market_intel import validate as VA
from market_scan import ATR_SESSIONS, DOLLAR_VOLUME_SESSIONS, RVOL_PRIOR_SESSIONS, SPREAD_SNAPSHOTS

# the order in which a derived value's state follows its inputs: the worst one wins
PRECEDENCE = (INSUFFICIENT_DATA, UNAVAILABLE, STALE, NOT_APPLICABLE, AVAILABLE)
DIGITS = 6                                            # output rounding (presentation and determinism, not a threshold)


def num(x: Any) -> Optional[float]:
    """A finite number, or None (bools, text, NaN and infinities are never values)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def feature(value: Optional[float] = None, status: str = INSUFFICIENT_DATA, codes: Iterable[str] = (), unit: str = "", **extra) -> Dict[str, Any]:
    v = None if value is None else round(value, DIGITS)
    if v is None and status == AVAILABLE:
        status = INSUFFICIENT_DATA                    # nothing to show is never AVAILABLE
    return {"value": v, "status": status, "usable": status == AVAILABLE and v is not None, "codes": sorted(set(codes)), "unit": unit, **extra}


def worst(statuses: Iterable[str]) -> str:
    got = list(statuses)
    return next((s for s in PRECEDENCE if s in got), AVAILABLE)


def graded(f: Dict[str, Any], *inputs: Dict[str, Any]) -> Dict[str, Any]:
    """`f` with the worst state of the graded inputs it was computed from (each a Stage 7.0 assessment or a Feature)."""
    if f["value"] is None:
        return f
    s = worst([f["status"], *(i["status"] for i in inputs)])
    codes = set(f["codes"]) | {f"INPUT_{i['status']}" for i in inputs if i["status"] != AVAILABLE}
    return {**f, "status": s, "usable": s == AVAILABLE, "codes": sorted(codes)}


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def at(s: Any) -> Optional[datetime]:
    """An ISO time with an offset, as the snapshot payload stores it, or None."""
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def ny_day(s: Any) -> Optional[date]:
    """The New York date a provider time falls on (a daily bar starts at New York midnight)."""
    d = at(s)
    return None if d is None else CAL.session(d)["session_date"]


def _prev_close(snap: dict, previous: Optional[date], codes: List[str]) -> Optional[float]:
    pb = snap.get("prev_daily_bar")
    if not isinstance(pb, dict) or previous is None or ny_day(pb.get("start")) != previous:
        codes.append("PREV_CLOSE_NOT_PREVIOUS_SESSION")
        return None
    if not _bar_ok(pb):                                                # an embedded bar is checked like a stored one
        codes.append("EMBEDDED_BAR_INVALID")
        return None
    return float(pb["close"])


def _today_bar(snap: dict, today: Optional[date], codes: List[str]) -> Optional[dict]:
    """The snapshot's daily bar, only when it is dated `today` and consistent (positive prices, high / low around open and close)."""
    db = snap.get("daily_bar")
    if not isinstance(db, dict) or ny_day(db.get("start")) != today:
        codes.append("DAILY_BAR_NOT_TODAY")
        return None
    if not _bar_ok(db):
        codes.append("EMBEDDED_BAR_INVALID")
        return None
    return db


def last_trade(snap: Optional[dict], *, today: Optional[date], sessions: Sequence[str] = (PRE, REG, POST)) -> Dict[str, Any]:
    """The snapshot's last trade price, only when that trade happened on `today` in one of `sessions`."""
    if not isinstance(snap, dict):
        return feature(codes=["NO_SNAPSHOT"], unit="USD")
    t = at(snap.get("last_trade_at"))
    p = num(snap.get("last_price"))
    if t is None or p is None:
        return feature(codes=["NO_LAST_TRADE"], unit="USD")
    s = CAL.session(t)
    if s["session_date"] != today or s["session"] not in sessions:
        return feature(codes=["LAST_TRADE_NOT_TODAY" if s["session_date"] != today else f"LAST_TRADE_IN_{s['session']}"], unit="USD")
    if p <= 0:
        return feature(codes=["NON_POSITIVE_PRICE"], unit="USD")
    return feature(p, AVAILABLE, unit="USD", trade_at=t.isoformat())


def gap_pct(snap: Optional[dict], *, session: str, today: Optional[date], previous: Optional[date]) -> Dict[str, Any]:
    """PRE: the last premarket trade vs the previous close; REG / POST: today's open vs the previous close. Both sides from one snapshot,
    so one feed (D.1 "same feed both sides"). 100 x (price / previous close - 1)."""
    if session not in (PRE, REG, POST) or today is None:
        return feature(status=NOT_APPLICABLE, codes=["OUTSIDE_SESSION"], unit="%")
    if not isinstance(snap, dict):
        return feature(codes=["NO_SNAPSHOT"], unit="%")
    codes: List[str] = []
    pc = _prev_close(snap, previous, codes)
    if session == PRE:
        lt = last_trade(snap, today=today, sessions=(PRE,))
        p, basis = lt["value"], "PRE_LAST_TRADE"
        codes += [] if p is not None else ["NO_PREMARKET_TRADE"]
    else:
        db = _today_bar(snap, today, codes)
        p, basis = (None if db is None else float(db["open"])), "TODAY_OPEN"
    if pc is None or p is None:
        return feature(codes=codes, unit="%", basis=basis)
    return feature(100.0 * (p / pc - 1.0), AVAILABLE, codes, unit="%", basis=basis, price=round(p, DIGITS), previous_close=round(pc, DIGITS))


def day_change(snap: Optional[dict], *, today: Optional[date], previous: Optional[date]) -> Dict[str, Any]:
    """The move since the previous close: 100 x (today's last trade / previous close - 1), from one snapshot (sector strength's 'move')."""
    if not isinstance(snap, dict) or today is None:
        return feature(codes=["NO_SNAPSHOT"], unit="%")
    codes: List[str] = []
    pc = _prev_close(snap, previous, codes)
    lt = last_trade(snap, today=today)
    if lt["value"] is None:
        codes += lt["codes"]
    if pc is None or lt["value"] is None:
        return feature(codes=codes, unit="%")
    return feature(100.0 * (lt["value"] / pc - 1.0), AVAILABLE, unit="%")


def _bar_ok(b: dict) -> bool:
    o, h, lo, c, v = (num(b.get(k)) for k in ("open", "high", "low", "close", "volume"))
    return None not in (o, h, lo, c, v) and min(o, h, lo, c) > 0 and v >= 0 and h >= max(o, c) and lo <= min(o, c)


def premarket(bars: Sequence[dict], *, feed: str, minutes_elapsed: int = 0) -> Dict[str, Dict[str, Any]]:
    """PRE volume and the PRE high / low of `today`'s DELIVERED premarket minute bars (D.1: flagged THIN on IEX). A sum of the delivered
    bars, with its coverage stated (minutes covered of the minutes elapsed): premarket prints are sparse, so a missing minute is not read as
    zero volume here - it is not read at all, and the coverage says so."""
    thin = ["THIN_ON_IEX"] if feed == "iex" else []
    cov = {"minutes_covered": len({b["start"] for b in bars}), "minutes_elapsed": minutes_elapsed}
    if not bars:
        return {"premarket_volume": feature(codes=["NO_PREMARKET_BARS"], unit="shares", **cov),
                "premarket_range": feature(codes=["NO_PREMARKET_BARS"], unit="%", **cov)}
    if not all(_bar_ok(b) for b in bars):
        return {"premarket_volume": feature(codes=["INVALID_BAR"], unit="shares"), "premarket_range": feature(codes=["INVALID_BAR"], unit="%")}
    hi, lo = max(float(b["high"]) for b in bars), min(float(b["low"]) for b in bars)
    return {"premarket_volume": feature(sum(float(b["volume"]) for b in bars), AVAILABLE, thin + ["SUM_OF_DELIVERED_BARS"], unit="shares", **cov),
            "premarket_range": feature(100.0 * (hi / lo - 1.0), AVAILABLE, thin, unit="%", high=round(hi, DIGITS), low=round(lo, DIGITS), **cov)}


def cumulative_volume(bars: Sequence[dict], *, open_at: datetime, minutes: int) -> Optional[float]:
    """The volume of the minute bars of [open_at, open_at + minutes), only when EVERY minute of it has its bar: a missing minute (on IEX
    possibly a minute without prints) is never a zero. None with a gap, without any minute, or with a volume that is not a finite,
    non-negative number."""
    if minutes < 1:
        return None
    cut = open_at + timedelta(minutes=minutes)
    by = {b["start"]: b for b in bars if open_at <= b["start"] < cut}
    if set(by) != {open_at + timedelta(minutes=k) for k in range(minutes)}:
        return None
    vols = [num(b.get("volume")) for b in by.values()]
    if any(v is None or v < 0 for v in vols):
        return None
    return sum(vols)


def relative_volume_tod(today: Optional[float], prior: Sequence[Optional[float]], *, minutes: int, missing: Sequence[str] = (),
                        today_code: str = "NO_BARS_TODAY") -> Dict[str, Any]:
    """Cumulative volume to t / the median of the prior 10 sessions' cumulative volume to the same minute (one feed both sides), t = the end
    of today's latest delivered regular-session minute bar (run.py)."""
    extra = {"minutes": minutes, "prior_sessions": len(prior)}
    if minutes < 1:
        return feature(codes=["NO_COMPLETED_MINUTE"], unit="x", **extra)
    if today is None:
        return feature(codes=[today_code], unit="x", **extra)
    if len(prior) < RVOL_PRIOR_SESSIONS or any(v is None for v in prior):
        return feature(codes=["PRIOR_SESSIONS_MISSING"], unit="x", missing_sessions=list(missing), **extra)
    base = median([float(v) for v in prior])
    if base <= 0:
        return feature(codes=["ZERO_BASELINE"], unit="x", **extra)
    return feature(today / base, AVAILABLE, unit="x", today_volume=today, baseline_volume=round(base, DIGITS), **extra)


def atr(bars: Sequence[dict], *, missing: Sequence[str] = ()) -> Dict[str, Any]:
    """ATR(14): the plain mean of the last 14 true ranges over the 15 most recent completed sessions, oldest first. Raw bars; a day jump
    beyond the 7.0 limit (25 %: a split or an extreme move) leaves it undefined rather than mixing two price scales."""
    need = ATR_SESSIONS + 1
    if len(bars) < need:                                                # the run passes only the present bars of the window
        return feature(codes=["SESSIONS_MISSING"], unit="USD", missing_sessions=list(missing))
    w = list(bars)[-need:]
    if not all(_bar_ok(b) for b in w):
        return feature(codes=["INVALID_BAR"], unit="USD")
    if any(abs(float(b["close"]) / float(a["close"]) - 1.0) > VA.DAY_JUMP for a, b in zip(w, w[1:])):
        return feature(codes=["DAY_JUMP_IN_WINDOW"], unit="USD")
    trs = [max(float(b["high"]), float(a["close"])) - min(float(b["low"]), float(a["close"])) for a, b in zip(w, w[1:])]
    return feature(sum(trs) / len(trs), AVAILABLE, unit="USD", sessions=[b["session_date"].isoformat() for b in w])


def intraday_move(last: Dict[str, Any], snap: Optional[dict], atr_f: Dict[str, Any], *, today: Optional[date]) -> Dict[str, Any]:
    """The last trade vs today's open: 100 x (last / open - 1), and (last - open) / ATR(14) when the ATR is usable."""
    if last["value"] is None:
        return feature(codes=last["codes"], unit="%")
    codes: List[str] = []
    db = _today_bar(snap, today, codes) if isinstance(snap, dict) else None
    if db is None:
        return feature(codes=codes or ["NO_SNAPSHOT"], unit="%")
    o = float(db["open"])
    units = (last["value"] - o) / atr_f["value"] if atr_f["usable"] and atr_f["value"] > 0 else None
    return feature(100.0 * (last["value"] / o - 1.0), AVAILABLE, [] if units is not None else ["ATR_UNAVAILABLE"], unit="%",
                   atr_units=None if units is None else round(units, DIGITS))


def unusual_volatility(snap: Optional[dict], atr_f: Dict[str, Any], *, today: Optional[date]) -> Dict[str, Any]:
    """Today's range so far (the snapshot's daily bar high - low) / ATR(14)."""
    codes: List[str] = []
    db = _today_bar(snap, today, codes) if isinstance(snap, dict) else None
    if db is None:
        return feature(codes=codes or ["NO_SNAPSHOT"], unit="x")
    h, lo = float(db["high"]), float(db["low"])
    if not atr_f["usable"] or atr_f["value"] <= 0:
        return feature(codes=["ATR_UNAVAILABLE"], unit="x")
    return feature((h - lo) / atr_f["value"], AVAILABLE, unit="x")


def sector_strength(sector: Optional[str], etf: Optional[str], etf_move: Optional[Dict[str, Any]], spy_move: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The symbol's sector ETF move - SPY's move, in percentage points (both since the previous close). Unmapped: none."""
    if not sector or not etf:
        return feature(status=NOT_APPLICABLE, codes=["NO_SECTOR"], unit="pp")
    if etf_move is None or spy_move is None or etf_move["value"] is None or spy_move["value"] is None:
        return feature(codes=["SECTOR_OR_MARKET_MOVE_UNAVAILABLE"], unit="pp", sector=sector, etf=etf)
    f = feature(etf_move["value"] - spy_move["value"], AVAILABLE, unit="pp", sector=sector, etf=etf)
    return graded(f, etf_move, spy_move)


def dollar_volume(bars: Sequence[dict], *, label: Sequence[str] = (), missing: Sequence[str] = ()) -> Dict[str, Any]:
    """dollar_volume_20d: the median of close x volume over the 20 most recent completed sessions (raw bars)."""
    if len(bars) < DOLLAR_VOLUME_SESSIONS:
        return feature(codes=["SESSIONS_MISSING", *label], unit="USD", missing_sessions=list(missing))
    w = list(bars)[-DOLLAR_VOLUME_SESSIONS:]
    if not all(_bar_ok(b) for b in w):
        return feature(codes=["INVALID_BAR", *label], unit="USD")
    return feature(median([float(b["close"]) * float(b["volume"]) for b in w]), AVAILABLE, label, unit="USD")


def spread_bps(snaps: Sequence[dict], *, feed: str) -> Dict[str, Any]:
    """The median of 10 000 x (ask - bid) / mid over the last 5 snapshots (oldest first). IEX spreads are labelled non-representative."""
    label = ["NON_REPRESENTATIVE_IEX"] if feed == "iex" else []
    if len(snaps) < SPREAD_SNAPSHOTS:
        return feature(codes=["FEWER_THAN_5_SNAPSHOTS", *label], unit="bps", snapshots=len(snaps))
    w = list(snaps)[-SPREAD_SNAPSHOTS:]
    out = []
    for s in w:
        bid, ask = num(s.get("bid")), num(s.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return feature(codes=["INVALID_QUOTE", *label], unit="bps")
        out.append(10_000.0 * (ask - bid) / ((ask + bid) / 2.0))
    return feature(median(out), AVAILABLE, label, unit="bps", snapshots=len(w))
