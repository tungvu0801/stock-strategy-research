"""
safety/engine.py — the pure part of the safety gate (DESIGN_591 §2, §5, §9): the daily-loss computation from a PERSISTED baseline,
the precedence decision and the per-control evidence. No I/O, no clock reads, no randomness; Decimal money; never NaN.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, List, Optional, Tuple

from safety import (R_ADJUSTMENT_NA, R_BASELINE_MISSING, R_DAILY, R_EQUITY_INVALID, R_EQUITY_NA, R_KILL, R_PAUSE, R_SNAPSHOT_STALE, R_THRESHOLD_INVALID,
                    REASON_TEXT, STATE_ICONS, STATE_LABELS)
from safety.models import AccountEquity, Baseline, ControlEvaluation, DailyLoss, KillSwitch, Pause

MONEY = Decimal("0.01")
PCT = Decimal("0.01")


def D(x: Any) -> Optional[Decimal]:
    """A finite Decimal or None (never NaN, never infinity)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        if isinstance(x, float):
            if not math.isfinite(x):
                return None
            d = Decimal(repr(x))
        else:
            d = Decimal(str(x).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(d: Decimal) -> Decimal:
    return d.quantize(MONEY, rounding=ROUND_HALF_UP)


def s(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else format(d, "f")


def trading_date(now: datetime) -> date:
    """The America/New_York calendar date of the evaluation clock (the Stage 5.6 / 5.8 convention)."""
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 - no tz database: UTC date (documented fallback)
        return now.astimezone(timezone.utc).date()


# ---------------------------------------------------------------- daily loss ---------------------------------------------------------

def daily_loss(baseline: Optional[Baseline], account: AccountEquity, threshold_pct: Any, *, max_age_s: float, accept_unadjusted: bool,
               latched: bool, latched_trading_date: Optional[str], triggered_at: Optional[str], triggered_loss_pct: Optional[str], today: str) -> DailyLoss:
    """The daily-loss control for `today`. A missing baseline is UNAVAILABLE — starting equity is never derived from current equity here."""
    out = DailyLoss(trading_date=today, equity_source=account.source, equity_as_of=account.as_of, equity_age_s=account.age_s, current_equity=account.equity,
                    latched=bool(latched and latched_trading_date == today), triggered_at=triggered_at if latched and latched_trading_date == today else None,
                    triggered_loss_pct=triggered_loss_pct if latched and latched_trading_date == today else None)
    thr = D(threshold_pct)
    out.threshold_pct = s(thr) if thr is not None else None
    if baseline is not None:
        out.starting_equity, out.baseline_created_at, out.adjustments = baseline.starting_equity, baseline.baseline_created_at, baseline.adjustments
    if out.latched:                                                                   # the latch is a persisted fact for the day, whatever equity does later
        out.status, out.code = "TRIGGERED", R_DAILY
    if thr is None or thr <= 0:
        return _unavailable(out, R_THRESHOLD_INVALID)
    cur = D(account.equity)
    if account.equity is None:
        return _unavailable(out, R_EQUITY_NA)
    if cur is None or cur <= 0:
        return _unavailable(out, R_EQUITY_INVALID)
    out.current_equity = s(money(cur))
    if account.source == "ALPACA_PAPER_VIEW" and (account.age_s is None or account.age_s > max_age_s):
        return _unavailable(out, R_SNAPSHOT_STALE)
    if baseline is None or baseline.trading_date != today:
        return _unavailable(out, R_BASELINE_MISSING)
    start = D(baseline.starting_equity)
    if start is None or start <= 0:
        return _unavailable(out, R_EQUITY_INVALID)
    pnl = money(cur - start)
    loss_pct = max(Decimal(0), (start - cur) / start * 100).quantize(PCT, rounding=ROUND_HALF_UP)
    out.daily_pnl, out.daily_loss_pct = s(pnl), s(loss_pct)
    adjustments = baseline.adjustments
    if adjustments == "UNAVAILABLE" and accept_unadjusted:
        adjustments = "ACCEPTED_UNADJUSTED"
    out.adjustments = adjustments
    if loss_pct >= thr:
        out.status, out.code = "TRIGGERED", R_DAILY                                   # a withdrawal that looks like a loss halts: the conservative error
        return out
    if out.latched:
        return out
    if adjustments == "UNAVAILABLE":
        return _unavailable(out, R_ADJUSTMENT_NA)
    out.status, out.code = "OK", None
    return out


def _unavailable(out: DailyLoss, code: str) -> DailyLoss:
    if out.status == "TRIGGERED":                                                     # a latched day stays TRIGGERED; the note keeps the data problem visible
        out.note = REASON_TEXT.get(code, code)
        return out
    out.status, out.code = "UNAVAILABLE", code
    return out


# ---------------------------------------------------------------- precedence ---------------------------------------------------------

def decide(kill: KillSwitch, pause: Pause, daily: DailyLoss) -> Tuple[str, bool, List[str], str]:
    """(state, allowed_new_entry, reason_codes, headline). Precedence: kill switch > daily loss > no-new-entries (manual or a degraded
    daily-loss control) > trading allowed. A degraded daily-loss control is never reported as healthy."""
    codes: List[str] = []
    if kill.active:
        codes.append(R_KILL)
    if daily.status == "TRIGGERED":
        codes.append(R_DAILY)
    if pause.active:
        codes.append(R_PAUSE)
    if daily.status == "UNAVAILABLE" and daily.code:
        codes.append(daily.code)
    if kill.active or daily.status == "TRIGGERED":
        state = "HALTED"
    elif pause.active or daily.status == "UNAVAILABLE":
        state = "NO_NEW_ENTRIES"
    else:
        state = "TRADING_ALLOWED"
    headline = REASON_TEXT.get(codes[0], codes[0]) if codes else ""
    return state, state == "TRADING_ALLOWED", codes, headline


def controls(kill: KillSwitch, pause: Pause, daily: DailyLoss, baseline: Optional[Baseline]) -> List[ControlEvaluation]:
    out = [ControlEvaluation(control_id="GLOBAL_KILL_SWITCH", result="BLOCK" if kill.active else "PASS", reason=(kill.reason or "active") if kill.active else "not active",
                             input=kill.model_dump())]
    if daily.status == "TRIGGERED":
        out.append(ControlEvaluation(control_id="DAILY_LOSS_HALT", result="BLOCK", reason=f"daily loss {daily.daily_loss_pct or daily.triggered_loss_pct or '?'}% >= {daily.threshold_pct}%",
                                     input=daily.model_dump()))
    elif daily.status == "OK":
        out.append(ControlEvaluation(control_id="DAILY_LOSS_HALT", result="PASS", reason=f"daily loss {daily.daily_loss_pct}% < {daily.threshold_pct}%", input=daily.model_dump()))
    else:
        out.append(ControlEvaluation(control_id="DAILY_LOSS_HALT", result="UNKNOWN", reason=daily.code or "unavailable", input=daily.model_dump()))
    out.append(ControlEvaluation(control_id="NO_NEW_ENTRIES", result="BLOCK" if pause.active else "PASS", reason=(pause.reason or "paused") if pause.active else "not active",
                                 input=pause.model_dump()))
    out.append(ControlEvaluation(control_id="DAILY_BASELINE", result="PASS" if baseline is not None else "BLOCK",
                                 reason=f"baseline {baseline.trading_date} from {baseline.source}" if baseline is not None else "no baseline for today",
                                 input=baseline.model_dump() if baseline is not None else {"trading_date": daily.trading_date, "equity_source": daily.equity_source}))
    return out


def icon_label(state: str) -> Tuple[str, str]:
    return STATE_ICONS.get(state, STATE_ICONS["UNKNOWN"]), STATE_LABELS.get(state, STATE_LABELS["UNKNOWN"])
