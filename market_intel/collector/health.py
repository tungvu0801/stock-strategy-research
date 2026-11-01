"""
market_intel/collector/health.py - Stage 7.3: feed health per source and scope (DESIGN_73 section 9). Read-only; never fetches.

status, first match wins:
  BLOCKED_PENDING_APPROVAL  the plan is blocked (both SEC plans: SEC_ACCEPTANCE_TZ_UNVERIFIED)
  NOT_CONFIGURED            its credential is not present (presence only)
  UNAVAILABLE               the store failed its integrity check (STORE_INTEGRITY_FAILED): no run until an operator repairs it
  CIRCUIT_OPEN              the breaker is open, or its one trial run is in flight
  BUDGET_EXHAUSTED          the daily cap is spent (or the budget is disabled)
  UNAVAILABLE               never succeeded; the freshness assessment says UNAVAILABLE; or the provider denied access this cycle
  STALE                     the Stage 7.0 freshness policy says STALE (a stale feed is never shown as current)
  DEGRADED                  the latest ended run was not COMPLETED / EMPTY, or was complete but not verified (PENDING_ANCHOR, UNVERIFIED_SEAL),
                            or it saw clock skew - while freshness is still within policy
  AVAILABLE                 otherwise

The freshness is the plan's Stage 7.0 policy measured from the start of the last successful (COMPLETED / EMPTY and verified) run - the time the data
was asked for, never later. The daily jobs use the 7.0 daily-refresh rule (assess_earnings_calendar's numbers: FRESH on the same New York
day, AGING within 36 h, STALE after). No threshold is new (decision D). Values already stored keep their own point-in-time status
(store.latest_status); this is the health of the FEED.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional

from market_intel import freshness as FR
from market_intel.collector import plans as PL
from market_intel.collector import policy as PO
from market_intel.collector.state import COMPLETE, CollectorState
from market_intel.store import parse_ts

AVAILABLE, DEGRADED, STALE, UNAVAILABLE = "AVAILABLE", "DEGRADED", "STALE", "UNAVAILABLE"
CIRCUIT_OPEN, BUDGET_EXHAUSTED, NOT_CONFIGURED, BLOCKED_PENDING_APPROVAL = "CIRCUIT_OPEN", "BUDGET_EXHAUSTED", "NOT_CONFIGURED", "BLOCKED_PENDING_APPROVAL"
HEALTH_STATUSES = (AVAILABLE, DEGRADED, STALE, UNAVAILABLE, CIRCUIT_OPEN, BUDGET_EXHAUSTED, NOT_CONFIGURED, BLOCKED_PENDING_APPROVAL)
MAX_GAPS_SHOWN = 10


def freshness(plan: PL.CollectionPlan, last_success_at: Optional[datetime], now: datetime) -> dict:
    if plan.health_policy == PL.DAILY_REFRESH:
        return {**FR.assess_earnings_calendar(last_success_at, now), "key": PL.DAILY_REFRESH, "action": None}
    return FR.assess(plan.health_policy, last_success_at, now)


def merge_gaps(rows: List[dict]) -> List[List[str]]:
    out: List[List[str]] = []
    for g in sorted(rows, key=lambda x: (x["gap_start"], x["gap_end"])):
        if out and g["gap_start"] <= out[-1][1]:
            out[-1][1] = max(out[-1][1], g["gap_end"])
        else:
            out.append([g["gap_start"], g["gap_end"]])
    return out


def source_health(plan: PL.CollectionPlan, scope: PL.Scope, *, state: CollectorState, now: datetime, credentials: Callable[[str], bool],
                  budget_refusal: Optional[str] = None, integrity: Optional[str] = None) -> dict:
    key = scope.key()
    last = state.last_run(plan.source, key)
    ended = state.last_ended_run(plan.source, key)
    ok = state.last_success(plan.source, key)
    last_success_at = parse_ts(ok["started_at"]) if ok else None
    b = state.breaker(plan.source)
    fr = freshness(plan, last_success_at, now)
    gaps = merge_gaps(state.gaps(plan.source, key))
    skew = bool(ended and ended["clock_skew"])
    if plan.blocked_reason:
        status, reason = BLOCKED_PENDING_APPROVAL, plan.blocked_reason.split(":")[0]
    elif plan.credential is not None and credentials(plan.credential) is not True:
        status, reason = NOT_CONFIGURED, f"{plan.credential} is not configured"
    elif integrity:
        status, reason = UNAVAILABLE, integrity
    elif PO.gate(b, now) in (PO.CIRCUIT_OPEN, PO.TRIAL_RUNNING):
        status, reason = CIRCUIT_OPEN, PO.gate(b, now)
    elif budget_refusal == "BUDGET_EXHAUSTED":
        status, reason = BUDGET_EXHAUSTED, budget_refusal
    elif budget_refusal == "ACCESS_DENIED_THIS_CYCLE":
        status, reason = UNAVAILABLE, "ACCESS_DENIED"
    elif ok is None:
        status, reason = UNAVAILABLE, "NEVER_SUCCEEDED"
    elif fr["status"] == FR.UNAVAILABLE:
        status, reason = UNAVAILABLE, fr["reason"]
    elif fr["status"] == FR.STALE:
        status, reason = STALE, fr["reason"]
    elif ended is not None and ended["status"] not in COMPLETE:
        status, reason = DEGRADED, f"LAST_RUN_{ended['status']}"
    elif ended is not None and not ended["verified"]:
        status, reason = DEGRADED, ended["last_error"] or "UNVERIFIED"
    elif skew:
        status, reason = DEGRADED, "CLOCK_SKEW"
    else:
        status, reason = AVAILABLE, "OK"
    return {"source": plan.source, "scope": key, "status": status, "reason": reason, "freshness": fr,
            "last_success_at": ok["started_at"] if ok else None, "last_attempt_at": last["started_at"] if last else None,
            "last_status": ended["status"] if ended else None, "running": bool(last and last["status"] is None),
            "consecutive_failures": b.failures, "breaker": {"state": b.state, "opened_at": b.opened_at.isoformat() if b.opened_at else None,
                                                            "cooldown_s": b.cooldown_s},
            "budget": budget_refusal or "OK", "clock_skew": skew, "last_error": ended["last_error"] if ended else "",
            "gaps": gaps[-MAX_GAPS_SHOWN:], "gap_count": len(gaps), "approvals_needed": list(plan.approvals)}
