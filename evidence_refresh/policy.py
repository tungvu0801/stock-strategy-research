"""
evidence_refresh/policy.py - pure decisions for the Stage 5.4.2 evidence refresh (no I/O, no clock, no network).

The packet a symbol is judged by is the packet the setup's evidence check reads: trader_view.research.pick (Stage 5.4.1 - the newest
COMPLETED packet created at or before now). Its age uses the setup service's own formula (whole days since created_at), so "STALE"
here is exactly the day READY_BLOCKED_EVIDENCE_STALE would start.

A failed attempt is either a stored FAILED packet or a journal error (journal.py: an attempt that raised before any packet was stored).
Both count the same way: the retry wait, the per-symbol daily cap and the daily build budget. An open circuit blocks every build.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from evidence_refresh import (BACKOFF, BUILD, DUE, FAILURE_CAP, FRESH, MISSING, READ_ERROR, RETRY_WAIT, STALE, WAIT)

_URGENCY = {STALE: 0, MISSING: 1, DUE: 2, FRESH: 3}


@dataclass(frozen=True)
class RefreshSettings:
    refresh_age_days: int = 7            # rebuild a COMPLETED packet once it is this many whole days old
    max_age_days: int = 30               # setups.EVIDENCE_MAX_AGE_DAYS: older than this blocks READY
    retry_hours: float = 6.0             # after a FAILED build, wait this long before the next attempt for that symbol
    max_failures_per_day: int = 2        # FAILED packets for one symbol in the trailing 24 h before it waits for the window to clear
    max_builds_per_run: int = 3
    max_builds_per_day: int = 12         # all packets (any status, any origin) for the in-scope symbols in the trailing 24 h

    def as_dict(self) -> dict:
        return asdict(self)


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def created_of(row: dict) -> Optional[datetime]:
    try:
        return _utc(datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00")))
    except ValueError:
        return None


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def gate_packet(rows: List[dict], now: datetime) -> Optional[dict]:
    """The packet the READY evidence check uses for these rows, or None when it would read "no evidence"."""
    from trader_view.research import pick                        # the Stage 5.4.1 rule itself, not a copy of it
    chosen = pick(list(rows), now)
    if not chosen or chosen.get("status") != "COMPLETED":
        return None
    c = created_of(chosen)
    return chosen if c is not None and c <= now else None


def _error_at(e: dict) -> Optional[datetime]:
    try:
        return _utc(datetime.fromisoformat(str(e.get("at") or "").replace("Z", "+00:00")))
    except ValueError:
        return None


def symbol_state(symbol: str, rows: Iterable[dict], now: datetime, s: RefreshSettings, errors: Iterable[dict] = ()) -> dict:
    """One symbol's refresh state from its packets (any order, any status) and its journal errors (attempts that stored no packet)."""
    now = _utc(now)
    rows = [r for r in rows if (r.get("ticker") or symbol).upper() == symbol.upper()]
    valid = gate_packet(rows, now)
    vc = created_of(valid) if valid else None
    failed = [(created_of(r), r.get("packet_id") or "", {"packet_id": r.get("packet_id"), "failure_code": r.get("failure_code")})
              for r in rows if r.get("status") == "FAILED" and created_of(r) is not None and created_of(r) <= now]
    errs = [(t, "~" + str(e.get("error")), {"packet_id": None, "failure_code": None, "error": e.get("error")})
            for e in errors if (e.get("symbol") or symbol).upper() == symbol.upper() for t in [_error_at(e)] if t is not None and t <= now]
    failed = sorted(failed + errs, key=lambda f: (f[0], f[1]))
    window = [f for f in failed if f[0] > now - timedelta(hours=24)]
    last_fail = failed[-1] if failed else None
    out = {"symbol": symbol.upper(), "packet_id": valid.get("packet_id") if valid else None, "created_at": _iso(vc),
           "strength": valid.get("strength") if valid else None, "age_days": None, "due_at": None, "stale_at": None, "days_until_stale": None,
           "failures_24h": len(window), "errors_24h": sum(1 for f in window if f[1].startswith("~")), "last_failure": None, "retry_at": None,
           "reason": None}
    if last_fail:
        out["last_failure"] = {**last_fail[2], "created_at": _iso(last_fail[0])}
    if vc is None:
        state = MISSING
    else:
        age = max(0, (now - vc).days)                            # setups/service.py: max(0, (now - created).days)
        stale_at = vc + timedelta(days=s.max_age_days + 1)       # first moment age > max_age_days
        due_at = vc + timedelta(days=s.refresh_age_days)
        out.update(age_days=age, due_at=_iso(due_at), stale_at=_iso(stale_at), days_until_stale=max(0, (stale_at - now).days))
        state = STALE if now >= stale_at else (DUE if now >= due_at else FRESH)
    out["state"] = state
    if state == FRESH:
        out["decision"] = WAIT
        return out
    if len(window) >= s.max_failures_per_day:
        out.update(decision=BACKOFF, reason=FAILURE_CAP, retry_at=_iso(window[0][0] + timedelta(hours=24)))
        return out
    if last_fail and (vc is None or last_fail[0] > vc) and now < last_fail[0] + timedelta(hours=s.retry_hours):
        out.update(decision=BACKOFF, reason=RETRY_WAIT, retry_at=_iso(last_fail[0] + timedelta(hours=s.retry_hours)))
        return out
    out["decision"] = BUILD
    return out


def builds_last_24h(rows_by_symbol: Dict[str, List[dict]], now: datetime) -> int:
    now = _utc(now)
    return sum(1 for rows in rows_by_symbol.values() for r in rows
               if created_of(r) is not None and now - timedelta(hours=24) < created_of(r) <= now)


def errors_last_24h(errors_by_symbol: Dict[str, List[dict]], now: datetime) -> int:
    now = _utc(now)
    return sum(1 for errs in errors_by_symbol.values() for e in errs for t in [_error_at(e)] if t is not None and now - timedelta(hours=24) < t <= now)


def circuit_open(circuit: Optional[dict], now: datetime) -> bool:
    if not circuit:
        return False
    until = _error_at({"at": circuit.get("until")})
    return until is not None and _utc(now) < until


def plan(symbols: Iterable[str], rows_by_symbol: Dict[str, List[dict]], now: datetime, s: RefreshSettings,
         errors_by_symbol: Optional[Dict[str, List[dict]]] = None, circuit: Optional[dict] = None, unreadable: Iterable[str] = ()) -> dict:
    """Every in-scope symbol's state, and the ordered symbols to build now (most urgent first, within the run and daily budgets).
    A journal error spends the daily budget like a stored packet (it made the same requests). A symbol whose packets could not be read
    waits (it is not MISSING: we cannot tell). An open circuit builds nothing."""
    now = _utc(now)
    errors_by_symbol = {str(k).upper(): list(v) for k, v in (errors_by_symbol or {}).items()}
    unreadable = {str(u).upper() for u in unreadable}
    seen: List[str] = []
    for sym in symbols:
        u = str(sym).upper()
        if u not in seen:
            seen.append(u)
    items = []
    for sym in seen:
        it = symbol_state(sym, rows_by_symbol.get(sym, []), now, s, errors_by_symbol.get(sym, ()))
        if sym in unreadable:
            it.update(decision=WAIT, reason=READ_ERROR)
        items.append(it)
    used = builds_last_24h({k: v for k, v in rows_by_symbol.items() if k in seen}, now)
    errored = errors_last_24h({k: v for k, v in errors_by_symbol.items() if k in seen}, now)
    is_open = circuit_open(circuit, now)
    budget = 0 if is_open else max(0, min(s.max_builds_per_run, s.max_builds_per_day - used - errored))
    wanted = sorted((i for i in items if i["decision"] == BUILD), key=lambda i: (_URGENCY[i["state"]], i["created_at"] or "", i["symbol"]))
    counts: Dict[str, int] = {}
    for i in items:
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    soonest = min((i["stale_at"] for i in items if i["stale_at"]), default=None)
    return {"generated_at": _iso(now), "settings": s.as_dict(), "items": items, "to_build": [i["symbol"] for i in wanted[:budget]],
            "wanted": len(wanted), "budget": budget, "builds_last_24h": used, "errors_last_24h": errored, "counts": counts,
            "soonest_stale_at": soonest, "circuit": {**circuit, "open": is_open} if circuit else None}
