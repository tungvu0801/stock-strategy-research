"""
cash_flow/rules.py - the pure part (DESIGN_602 sections 2, 3, 5): normalization and classification of one broker activity, the redaction
of its free text, and the placement of each event relative to today's baseline and the current account snapshot. No I/O, no clock reads;
Decimal money; deterministic for identical inputs.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cash_flow import EXECUTED, EXTERNAL, EXTERNAL_TYPES, PERFORMANCE_TYPES, TRADE_TYPES, UNCLASSIFIED, UNVALUED_TYPES

UTC = timezone.utc
_ACCOUNT_LIKE = re.compile(r"\b[A-Z0-9]*\d[A-Z0-9]{5,}\b")                       # account numbers / long ids in free text
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def parse_ts(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def ny_date(dt: datetime) -> date:
    """The America/New_York calendar date (the Stage 5.9.1 trading-date convention); the UTC date without a tz database."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001
        return dt.astimezone(UTC).date()


def dec(v: Any) -> Optional[Decimal]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def redact(text: Any, limit: int = 160) -> Optional[str]:
    """Broker free text with account numbers and ids replaced (the paper JNLC / FEE descriptions embed the full account number)."""
    if text is None:
        return None
    s = _UUID.sub("****", str(text))
    s = _ACCOUNT_LIKE.sub("****", s)
    return s[:limit]


def classify(activity_type: str, net_amount: Optional[Decimal], currency: Optional[str]) -> Tuple[str, str, bool, str]:
    """(category, direction IN | OUT | NONE, external, note). Unknown or unvalued types are UNCLASSIFIED - never guessed into funding."""
    t = (activity_type or "").strip().upper()
    sign = net_amount if net_amount is not None else Decimal(0)
    direction = "IN" if sign > 0 else ("OUT" if sign < 0 else "NONE")
    if t in EXTERNAL_TYPES:
        if net_amount is None:
            return UNCLASSIFIED, "NONE", False, f"{t} without a usable amount"
        if currency and str(currency).upper() != "USD":
            return UNCLASSIFIED, direction, False, f"{t} in {currency} (only USD is valued)"
        pos, neg = EXTERNAL_TYPES[t]
        if net_amount > 0 and pos:
            return pos, "IN", True, ""
        if net_amount < 0 and neg:
            return neg, "OUT", True, ""
        if net_amount == 0:
            return pos or neg, "NONE", True, "zero amount"
        return UNCLASSIFIED, direction, False, f"{t} with an unexpected sign"
    if t in PERFORMANCE_TYPES:
        return PERFORMANCE_TYPES[t], direction, False, ""
    if t in UNVALUED_TYPES:
        return UNCLASSIFIED, direction, False, f"{t} is not valued as cash here"
    return UNCLASSIFIED, direction, False, f"unknown activity type {t or '(blank)'}"


def fingerprint(ev: Dict[str, Any]) -> str:
    keys = ("activity_type", "activity_sub_type", "net_amount", "currency", "broker_status", "business_date", "effective_at")
    return hashlib.sha256(json.dumps({k: ev.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()[:32]


def normalize(a: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One broker activity -> one ledger event (None for a trade row). Never raises for an odd row: what cannot be valued is UNCLASSIFIED."""
    t = str(a.get("activity_type") or "").strip().upper()
    if t in TRADE_TYPES:
        return None
    net = dec(a.get("net_amount"))
    currency = a.get("currency")
    category, direction, external, note = classify(t, net, currency)
    eff = parse_ts(a.get("created_at"))
    bdate = str(a.get("date") or "")[:10] or None
    ev = {"broker_event_id": str(a.get("id")), "activity_type": t or "(blank)", "activity_sub_type": a.get("activity_sub_type"), "category": category,
          "external": bool(external), "direction": direction, "amount": format(abs(net), "f") if net is not None else None,
          "net_amount": format(net, "f") if net is not None else None, "currency": currency,
          "broker_status": str(a.get("status")) if a.get("status") is not None else None, "business_date": bdate,
          "effective_at": iso(eff) if eff else None, "trading_date": ny_date(eff).isoformat() if eff else bdate, "note": note or None,
          "raw_reference": {"type": t, "sub_type": a.get("activity_sub_type"), "date": bdate, "created_at": a.get("created_at"), "status": a.get("status"),
                            "net_amount": a.get("net_amount"), "currency": currency, "symbol": a.get("symbol"), "qty": a.get("qty"),
                            "per_share_amount": a.get("per_share_amount"), "description": redact(a.get("description"))}}
    ev["fingerprint"] = fingerprint(ev)
    return ev


# ---------------------------------------------------------------- the window (DESIGN_602 section 5) ---------------------------------------

def place(ev: Dict[str, Any], *, cutoff: datetime, upper: datetime, margin_s: float) -> str:
    """Where one event stands for today's adjustment. `cutoff` = when the baseline equity was read; `upper` = the current account snapshot."""
    relevant = bool(ev.get("external")) or ev.get("category") == UNCLASSIFIED
    if not relevant:
        return "PERFORMANCE"                                                       # fees, dividends, interest, corporate actions: trading P&L
    m = timedelta(seconds=float(margin_s))
    eff = parse_ts(ev.get("effective_at"))
    if eff is None:                                                                # date only: placeable only when it is an earlier date
        bd = ev.get("business_date")
        if bd and bd < ny_date(cutoff).isoformat():
            return "BEFORE_BASELINE"
        return "UNCERTAIN" if ev.get("category") == UNCLASSIFIED else "AMBIGUOUS"
    if eff <= cutoff - m:
        return "BEFORE_BASELINE"                                                   # already inside the starting equity
    if ev.get("category") == UNCLASSIFIED:
        return "UNCERTAIN"                                                         # never guessed: blocks new entries
    if eff <= cutoff + m:
        return "AMBIGUOUS"                                                         # too close to the baseline read to know its side
    if eff > upper - m:
        return "PENDING"                                                           # not yet attributable to the equity reading
    if str(ev.get("broker_status") or "").strip().lower() != EXECUTED:
        return "AMBIGUOUS"                                                         # corrected / cancelled funding: effect on equity unknown
    return "COUNTED"


def adjust(events: Iterable[Dict[str, Any]], *, cutoff: datetime, upper: datetime, margin_s: float) -> Dict[str, Any]:
    """Today's net external cash flow from the events inside the window, and the adjustment status (worst event wins:
    UNCERTAIN > AMBIGUOUS > PENDING > ADJUSTED)."""
    sums = {k: Decimal("0") for k in EXTERNAL}
    counts: Dict[str, int] = {}
    placed: List[Dict[str, Any]] = []
    for ev in events:
        eff = place(ev, cutoff=cutoff, upper=upper, margin_s=margin_s)
        counts[eff] = counts.get(eff, 0) + 1
        if eff == "COUNTED":
            sums[ev["category"]] += dec(ev.get("amount")) or Decimal("0")
        if eff != "BEFORE_BASELINE":
            placed.append({**ev, "effect": eff})
    net = (sums["DEPOSIT"] + sums["JOURNAL_IN"]) - (sums["WITHDRAWAL"] + sums["JOURNAL_OUT"])
    n_unc, n_amb, n_pen = counts.get("UNCERTAIN", 0), counts.get("AMBIGUOUS", 0), counts.get("PENDING", 0)
    if n_unc:
        status, reason = "UNCERTAIN", f"{n_unc} account activity record(s) since the baseline could not be classified"
    elif n_amb:
        status, reason = "AMBIGUOUS", f"{n_amb} deposit / withdrawal record(s) cannot be placed before or after the baseline"
    elif n_pen:
        status, reason = "PENDING", f"{n_pen} new deposit / withdrawal record(s) not yet reflected in the account snapshot"
    else:
        status, reason = "ADJUSTED", ""
    return {"status": status, "reason": reason, "net_external_cash_flow": format(net, "f"), "deposits": format(sums["DEPOSIT"], "f"),
            "withdrawals": format(sums["WITHDRAWAL"], "f"), "journal_in": format(sums["JOURNAL_IN"], "f"), "journal_out": format(sums["JOURNAL_OUT"], "f"),
            "counts": counts, "events": placed, "cutoff": iso(cutoff), "upper": iso(upper), "margin_s": float(margin_s)}


def adjusted(start: Decimal, net_external: Decimal, current: Decimal) -> Dict[str, Optional[Decimal]]:
    """The DESIGN_602 section 3 formula, unrounded (the gate rounds money to cents and percentages to 0.01)."""
    adj = start + net_external
    if adj <= 0:
        return {"adjusted_starting_equity": adj, "adjusted_daily_pnl": None, "adjusted_daily_loss_pct": None}
    return {"adjusted_starting_equity": adj, "adjusted_daily_pnl": current - adj, "adjusted_daily_loss_pct": max(Decimal(0), (adj - current) / adj * 100)}
