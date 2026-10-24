"""
cash_flow/service.py - the ONE cash-flow service (DESIGN_602 sections 4-8).

  refresh()      one bounded, read-only activity read (retried a bounded number of times; 429 Retry-After honoured up to a cap) ->
                 normalize -> dedupe by broker activity id -> append new / revised events, the attempt and liveness in ONE transaction.
                 A failure keeps every stored event and is recorded.
  freshness()    the cash-flow DATA: FRESH | STALE | UNAVAILABLE | AMBIGUOUS (an incomplete read), from THIS process's reads only.
  adjustment()   today's adjustment against the Stage 5.9.1 baseline and the current account snapshot: ADJUSTED (+ net external cash
                 flow) | PENDING | AMBIGUOUS | UNCERTAIN | STALE | UNAVAILABLE. Only ADJUSTED lets the gate evaluate adjusted P&L.
  make_cash_flow_account_fn()   wraps the gate's account source (Stage 6.0.1) with the adjustment for the Alpaca paper account.

Nothing here places, cancels or changes an order or a position, changes the execution mode or touches strategy state.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config
from cash_flow import DISCLAIMER, LABELS, MAX_RETRY_AFTER_S, RETRYABLE_CODES, VERSION
from cash_flow import rules as R
from cash_flow.reader import ActivityReader, ReadFailure
from cash_flow.store import CashFlowStore

log = logging.getLogger(__name__)
UTC = timezone.utc


def _utc(dt: Optional[datetime] = None) -> datetime:
    d = dt or datetime.now(UTC)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _default_broker():
    from execution.service import default_broker
    return default_broker()


def _floats(raw: str, default: List[float]) -> List[float]:
    out = []
    for part in str(raw).split(","):
        try:
            out.append(max(0.0, float(part)))
        except ValueError:
            pass
    return out or default


class CashFlowService:
    def __init__(self, store: Optional[CashFlowStore] = None, *, broker=None, broker_fn: Callable[[], object] = _default_broker,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), sleep: Callable[[float], None] = time.sleep, owner: Optional[str] = None,
                 snapshot_at_fn: Optional[Callable[[], Optional[str]]] = None, baseline_fn: Optional[Callable[[str], object]] = None,
                 stale_after_s: Optional[float] = None, max_failures: Optional[int] = None, read_attempts: Optional[int] = None, backoff: Optional[List[float]] = None,
                 margin_s: Optional[float] = None, lookback_days: Optional[int] = None, bootstrap_days: Optional[int] = None, max_pages: Optional[int] = None,
                 page_size: Optional[int] = None, fault_file: Optional[Path] = None, cycle_wait_s: Optional[float] = None):
        self.store = store or CashFlowStore()
        self._broker = broker
        self.broker_fn = broker_fn
        self.clock = clock
        self.sleep = sleep
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.snapshot_at_fn = snapshot_at_fn
        self.baseline_fn = baseline_fn
        self._cfg = {"stale_after_s": stale_after_s, "max_failures": max_failures, "read_attempts": read_attempts, "backoff": backoff, "margin_s": margin_s,
                     "lookback_days": lookback_days, "bootstrap_days": bootstrap_days, "max_pages": max_pages, "page_size": page_size,
                     "cycle_wait_s": cycle_wait_s}
        self._fault_file = fault_file
        self._lock = threading.Lock()
        self._cycle_cv = threading.Condition()                                 # one refresh cycle = snapshot + this ledger, seen atomically
        self._cycle_active = False
        self.process_success_at: Optional[str] = None          # the START of the last successful read by THIS process (its coverage)
        self.process_complete: Optional[bool] = None
        self.last_outcome: Optional[Dict[str, Any]] = None
        self.reader: Optional[ActivityReader] = None

    # ---------------------------------------------------------------- configuration ------------------------------------------
    def _c(self, key: str, conf: str, cast):
        v = self._cfg.get(key)
        return cast(v if v is not None else getattr(config, conf))

    @property
    def stale_after_s(self) -> float:
        return self._c("stale_after_s", "CASH_FLOW_STALE_SECONDS", float)

    @property
    def max_failures(self) -> int:
        return self._c("max_failures", "CASH_FLOW_MAX_FAILURES", int)

    @property
    def read_attempts(self) -> int:
        return max(1, self._c("read_attempts", "CASH_FLOW_READ_ATTEMPTS", int))

    @property
    def backoff(self) -> List[float]:
        b = self._cfg.get("backoff")
        return list(b) if b is not None else _floats(getattr(config, "CASH_FLOW_BACKOFF_SECONDS", "2,5"), [2.0, 5.0])

    @property
    def margin_s(self) -> float:
        return self._c("margin_s", "CASH_FLOW_MARGIN_SECONDS", float)

    @property
    def lookback_days(self) -> int:
        return max(1, self._c("lookback_days", "CASH_FLOW_LOOKBACK_DAYS", int))

    @property
    def bootstrap_days(self) -> int:
        return max(self.lookback_days, self._c("bootstrap_days", "CASH_FLOW_BOOTSTRAP_DAYS", int))

    @property
    def max_pages(self) -> int:
        return max(1, self._c("max_pages", "CASH_FLOW_MAX_PAGES", int))

    @property
    def page_size(self) -> int:
        return max(1, min(100, self._c("page_size", "CASH_FLOW_PAGE_SIZE", int)))

    @property
    def cycle_wait_s(self) -> float:
        return max(0.0, self._c("cycle_wait_s", "CASH_FLOW_CYCLE_WAIT_SECONDS", float))

    @property
    def broker(self):
        if self._broker is None:
            self._broker = self.broker_fn()
        return self._broker

    @property
    def fault_file(self) -> Optional[Path]:
        if self._fault_file is not None:
            return self._fault_file
        if not getattr(config, "ACCOUNT_REFRESH_TEST_FAULTS", False):              # the same test-mode switch as the Stage 6.0.1 fault file
            return None
        raw = str(getattr(config, "CASH_FLOW_FAULT_FILE", "") or "")
        return Path(raw) if raw else Path(config.PROJECT_ROOT) / "runtime" / "cash_flow.fault"

    def enabled(self) -> bool:
        if not getattr(config, "CASH_FLOW_ENABLED", True):
            return False
        try:
            return bool(self.broker.configured())
        except Exception:  # noqa: BLE001
            return False

    def source(self) -> str:
        return "SIMULATED_PAPER" if getattr(self.broker, "simulated", False) else "ALPACA_PAPER"

    def _wire_fn(self):
        b = self.broker
        if getattr(b, "simulated", False):                                          # the simulated paper broker: its activity feed sits below the reader
            from cash_flow.simulated import ActivityWire, feed_for

            def wire():
                inner = b.wire()
                return ActivityWire(inner, feed_for(inner))
            return wire
        return b.wire

    # ---------------------------------------------------------------- the refresh cycle (marked by the account refresh loop) ----
    def cycle_started(self) -> None:
        with self._cycle_cv:
            self._cycle_active = True

    def cycle_finished(self) -> None:
        with self._cycle_cv:
            self._cycle_active = False
            self._cycle_cv.notify_all()

    def cycle_active(self) -> bool:
        return self._cycle_active

    def await_cycle(self, timeout: float) -> bool:
        with self._cycle_cv:
            return self._cycle_cv.wait_for(lambda: not self._cycle_active, timeout=timeout)

    # ---------------------------------------------------------------- the read -------------------------------------------------
    def _read_once(self, after: datetime) -> Dict[str, Any]:
        f = self.fault_file
        if f is not None and f.exists():
            raise ReadFailure("TEST_FAULT", f"test-mode fault file {f.name} present (no broker call)")
        self.reader = ActivityReader(self.broker, wire_fn=self._wire_fn(), page_size=self.page_size, max_pages=self.max_pages)
        return self.reader.read(R.iso(after))

    def refresh(self, now: Optional[datetime] = None, trigger: str = "SCHEDULED") -> Dict[str, Any]:
        """One complete read-only activity read. Never overlaps itself; never writes when no paper broker is configured."""
        if not self._lock.acquire(blocking=False):
            return {"result": "SKIPPED_RUNNING", "trigger": trigger}
        try:
            if not self.enabled():
                out = {"result": "NOT_CONFIGURED", "trigger": trigger}
                self.last_outcome = out
                return out
            started = _utc(now or self.clock())
            after = started - timedelta(days=self.bootstrap_days if self.process_success_at is None else self.lookback_days)
            attempts, failure, res = 0, None, None
            delays = self.backoff
            for i in range(self.read_attempts):
                attempts = i + 1
                try:
                    res = self._read_once(after)
                    failure = None
                    break
                except ReadFailure as exc:
                    failure = exc
                    retry = exc.result == "FAILED" and (exc.code in RETRYABLE_CODES or str(exc.code).startswith("HTTP_5"))
                    if not retry or attempts >= self.read_attempts:
                        break
                    delay = delays[min(i, len(delays) - 1)]
                    if exc.code == "RATE_LIMITED" and exc.retry_after is not None:
                        delay = max(delay, min(float(exc.retry_after), MAX_RETRY_AFTER_S))     # respect 429, within a cap
                    self.sleep(delay)
                except Exception as exc:  # noqa: BLE001 - an unexpected read failure is a failed refresh, never a crash
                    failure = ReadFailure("READ_ERROR", type(exc).__name__)
                    break
            finished = _utc(self.clock()) if now is None else started
            rid = uuid.uuid4().hex
            base = {"refresh_id": rid, "started_at": R.iso(started), "finished_at": R.iso(finished), "trigger": trigger, "read_attempts": attempts,
                    "lookback_after": R.iso(after), "owner": self.owner}
            if failure is None and res is not None:
                events = [e for e in (R.normalize(a) for a in res["rows"]) if e is not None]
                result = "SUCCESS" if res["complete"] else "INCOMPLETE"
                counts = self.store.commit_success(events, {**base, "result": result, "pages": res["pages"], "rows": len(res["rows"]), "complete": res["complete"],
                                                            "skipped_trade_rows": res["skipped_trade_rows"]}, finished, source=self.source())
                self.process_success_at = R.iso(started)
                self.process_complete = bool(res["complete"])
                if not res["complete"]:
                    log.warning("cash-flow read reached the page limit (%d pages): activity history incomplete", res["pages"])
                out = {"result": result, "trigger": trigger, "refresh_id": rid, "read_at": R.iso(started), "read_attempts": attempts, "pages": res["pages"],
                       "rows": len(res["rows"]), "new_events": counts["new"], "revised_events": counts["revised"], "skipped_trade_rows": res["skipped_trade_rows"]}
            else:
                self.store.commit_failure({**base, "result": failure.result, "error_code": failure.code, "error_text": failure.detail}, finished)
                log.warning("cash-flow read %s after %d attempt(s): %s", failure.result, attempts, failure.code)
                out = {"result": failure.result, "trigger": trigger, "error_code": failure.code, "error_text": failure.detail, "read_attempts": attempts}
            self.last_outcome = out
            return out
        finally:
            self._lock.release()

    # ---------------------------------------------------------------- freshness ------------------------------------------------
    def freshness(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _utc(now or self.clock())
        live = self.store.liveness()
        cf = int(live.get("consecutive_failures") or 0)
        d = R.parse_ts(self.process_success_at)
        age = round(max(0.0, (now - d).total_seconds()), 1) if d else None
        if not self.enabled():
            status, reason = "UNAVAILABLE", "no paper broker configured" if getattr(config, "CASH_FLOW_ENABLED", True) else "cash-flow ledger disabled"
        elif d is None:
            status, reason = "UNAVAILABLE", (f"{cf} failed activity read(s); no successful read yet" if cf else "waiting for the first activity read of this process")
        elif cf >= self.max_failures:
            status, reason = "STALE", f"{cf} consecutive failed activity reads"
        elif age is not None and age > self.stale_after_s:
            status, reason = "STALE", f"last successful activity read {int(age)} s ago (limit {int(self.stale_after_s)} s)"
        elif self.process_complete is False:
            status, reason = "AMBIGUOUS", "the activity history was incomplete (page limit reached)"
        else:
            status, reason = "FRESH", ""
        return {"version": VERSION, "status": status, "reason": reason, "age_seconds": age, "read_at": self.process_success_at,
                "last_attempt_at": live.get("last_attempt_at"), "last_success_at": live.get("last_success_at"), "last_result": live.get("last_result"),
                "last_error": live.get("last_error"), "consecutive_failures": cf, "attempt_count": int(live.get("attempt_count") or 0),
                "success_count": int(live.get("success_count") or 0), "failure_count": int(live.get("failure_count") or 0), "event_count": int(live.get("event_count") or 0),
                "last_event_at": live.get("last_event_at"), "stale_after_s": self.stale_after_s, "max_failures": self.max_failures, "source": self.source() if self.enabled() else None}

    # ---------------------------------------------------------------- today's adjustment ---------------------------------------
    def adjustment(self, now: Optional[datetime] = None, *, baseline=None, snapshot_at: Optional[str] = None, use_default_inputs: bool = False,
                   wait: bool = True) -> Dict[str, Any]:
        """Today's adjustment. `baseline` = the Stage 5.9.1 baseline row of today (None: the snapshot itself becomes the baseline);
        `snapshot_at` = when the current account equity was read. When that snapshot belongs to a refresh cycle whose activity read is still
        in flight, wait for it (at most `cycle_wait_s`): the gate sees one cycle atomically; past the bound it fails closed (STALE)."""
        now = _utc(now or self.clock())
        today = R.ny_date(now).isoformat()
        if use_default_inputs:
            snapshot_at = self.snapshot_at_fn() if self.snapshot_at_fn else None
            baseline = self.baseline_fn(today) if self.baseline_fn else None
        snap, read = R.parse_ts(snapshot_at), R.parse_ts(self.process_success_at)
        if wait and snap is not None and (read is None or read < snap) and self._cycle_active:
            self.await_cycle(self.cycle_wait_s)
        fr = self.freshness(now)
        out: Dict[str, Any] = {"trading_date": today, "status": "UNAVAILABLE", "reason": "", "net_external_cash_flow": None, "deposits": None, "withdrawals": None,
                               "journal_in": None, "journal_out": None, "counts": {}, "events": [], "cutoff": None, "upper": snapshot_at, "data": fr,
                               "baseline_trading_date": getattr(baseline, "trading_date", None)}
        if fr["status"] in ("UNAVAILABLE", "STALE"):
            return {**out, "status": fr["status"], "reason": fr["reason"]}
        upper = R.parse_ts(snapshot_at)
        if upper is None:
            return {**out, "status": "UNAVAILABLE", "reason": "no account snapshot to adjust"}
        covered = R.parse_ts(self.process_success_at)
        if covered is None or covered < upper:
            return {**out, "status": "STALE", "reason": "the activity read is older than the account snapshot"}
        cutoff = None
        if baseline is not None:
            cutoff = R.parse_ts(getattr(baseline, "equity_as_of", None)) or R.parse_ts(getattr(baseline, "baseline_created_at", None))
        cutoff = cutoff or upper                                                    # no baseline yet: this snapshot becomes it
        since = R.iso(cutoff - timedelta(seconds=self.margin_s) - timedelta(days=1))
        res = R.adjust(self.store.current_events(since), cutoff=cutoff, upper=upper, margin_s=self.margin_s)
        res = {**out, **res, "data": fr}
        if fr["status"] == "AMBIGUOUS" and res["status"] in ("ADJUSTED", "PENDING"):
            res.update(status="UNCERTAIN", reason=fr["reason"])
        return res

    @staticmethod
    def overall(adj: Dict[str, Any]) -> str:
        """The one cash-flow status a beginner sees: FRESH | STALE | UNAVAILABLE | AMBIGUOUS."""
        st = adj.get("status")
        if st == "ADJUSTED":
            return "FRESH"
        if st in ("STALE", "UNAVAILABLE"):
            return st
        return "AMBIGUOUS"

    def summary(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Display-only status for the Paper Bot panel and the health check."""
        now = _utc(now or self.clock())
        adj = self.adjustment(now, use_default_inputs=True)
        st = self.overall(adj)
        fr = adj["data"]
        age = fr.get("age_seconds")
        label = {"FRESH": f"Cash-flow data: Fresh ({int(age or 0)}s ago)", "STALE": "Cash-flow data: Stale", "UNAVAILABLE": "Cash-flow data unavailable",
                 "AMBIGUOUS": "Cash-flow data: Unclear"}[st]
        today = adj["trading_date"]
        todays = [e for e in self.store.current_events(R.iso(now - timedelta(days=2))) if e.get("trading_date") == today]
        return {"status": st, "label": label, "short_label": LABELS[st], "reason": adj.get("reason") or fr.get("reason") or "", "adjustment_status": adj["status"],
                "age_seconds": age, "last_cash_flow_refresh_at": fr.get("last_success_at"), "read_at": fr.get("read_at"), "last_attempt_at": fr.get("last_attempt_at"),
                "last_result": fr.get("last_result"), "last_error": fr.get("last_error"), "consecutive_failures": fr.get("consecutive_failures"),
                "attempt_count": fr.get("attempt_count"), "failure_count": fr.get("failure_count"), "event_count": fr.get("event_count"),
                "event_count_today": len(todays), "net_external_cash_flow": adj.get("net_external_cash_flow"), "deposits": adj.get("deposits"),
                "withdrawals": adj.get("withdrawals"), "journal_in": adj.get("journal_in"), "journal_out": adj.get("journal_out"), "trading_date": today}

    def overview(self, safety_view=None, now: Optional[datetime] = None) -> Dict[str, Any]:
        """GET /api/cash-flow-status: status, last refresh, today's events (no broker ids, no free text) and the gate's adjusted numbers."""
        now = _utc(now or self.clock())
        s = self.summary(now)
        adj = self.adjustment(now, use_default_inputs=True)
        dl = getattr(safety_view, "daily_loss", None)
        adjusted = None
        if dl is not None:
            adjusted = {"starting_equity": dl.starting_equity, "current_equity": dl.current_equity, "net_external_cash_flow": dl.net_external_cash_flow,
                        "adjusted_starting_equity": dl.adjusted_starting_equity, "adjusted_daily_pnl": dl.daily_pnl if dl.adjustments == "CASH_FLOW_ADJUSTED" else None,
                        "adjusted_daily_loss_pct": dl.daily_loss_pct if dl.adjustments == "CASH_FLOW_ADJUSTED" else None, "unadjusted_daily_pnl": dl.unadjusted_daily_pnl,
                        "threshold_pct": dl.threshold_pct, "daily_loss_status": dl.status, "daily_loss_code": dl.code, "adjustments": dl.adjustments,
                        "equity_source": dl.equity_source, "safety_state": getattr(safety_view, "state", None), "safety_label": getattr(safety_view, "state_label", None)}
        events = [{"category": e["category"], "activity_type": e["activity_type"], "direction": e["direction"], "amount": e["amount"], "currency": e.get("currency"),
                   "effective_at": e.get("effective_at"), "trading_date": e.get("trading_date"), "broker_status": e.get("broker_status"), "effect": e.get("effect")}
                  for e in adj.get("events") or []]
        try:
            from account_refresh.scheduler import status as loop_status
            loop = loop_status()
        except Exception:  # noqa: BLE001
            loop = {}
        return {"version": VERSION, "cash_flow_status": s["status"], **s, "last_refresh": s["last_cash_flow_refresh_at"], "cash_flow_age_seconds": s["age_seconds"],
                "adjustment": {k: adj.get(k) for k in ("status", "reason", "trading_date", "cutoff", "upper", "margin_s", "counts", "net_external_cash_flow",
                                                       "deposits", "withdrawals", "journal_in", "journal_out")},
                "events_since_baseline": events, "adjusted": adjusted, "adjusted_starting_equity": (adjusted or {}).get("adjusted_starting_equity"),
                "adjusted_daily_pnl": (adjusted or {}).get("adjusted_daily_pnl"), "adjusted_daily_loss_pct": (adjusted or {}).get("adjusted_daily_loss_pct"),
                "data": adj.get("data"), "loop": {"running": loop.get("running"), "lease": loop.get("lease"), "cash_flow": loop.get("cash_flow")},
                "disclaimer": DISCLAIMER}

    def events_view(self, limit: int = 100) -> Dict[str, Any]:
        rows = self.store.all_rows(limit)
        return {"version": VERSION, "total_rows": self.store.event_row_count(), "events": [
            {"category": e["category"], "activity_type": e["activity_type"], "activity_sub_type": e.get("activity_sub_type"), "direction": e["direction"],
             "amount": e["amount"], "currency": e.get("currency"), "external": e["external"], "effective_at": e.get("effective_at"), "trading_date": e.get("trading_date"),
             "business_date": e.get("business_date"), "broker_status": e.get("broker_status"), "revision": e["revision"], "note": e.get("note"), "recorded_at": e["created_at"]}
            for e in rows]}


# ---------------------------------------------------------------- the gate's account input (wired by trader_view/stream.py) ---------
def make_cash_flow_account_fn(inner: Callable[[float], object], cash_flow: CashFlowService, path: Path) -> Callable[[float], object]:
    """Wraps the Stage 6.0.1 safety account source: for the Alpaca paper account it adds today's cash-flow status and net external cash
    flow (the safety engine then evaluates ADJUSTED daily P&L, or pauses new entries with a precise reason). Every other source, and an
    unconfigured broker, passes through unchanged (the Stage 5.9.1 / 6.0.1 behaviour)."""
    def account(now_ts: float):
        acct = inner(now_ts)
        if getattr(acct, "source", None) != "ALPACA_PAPER_VIEW" or not cash_flow.enabled():
            return acct
        from safety.store import SafetyStore
        now = datetime.fromtimestamp(now_ts, tz=UTC)
        try:
            baseline = SafetyStore(path).baseline(R.ny_date(now).isoformat())
            adj = cash_flow.adjustment(now, baseline=baseline, snapshot_at=acct.as_of)
        except Exception as exc:  # noqa: BLE001 - never healthy on an error
            adj = {"status": "UNAVAILABLE", "reason": f"cash-flow evaluation failed ({type(exc).__name__})", "net_external_cash_flow": None}
        note = f"cash flow {adj['status'].lower()}" + (f": {adj.get('reason')}" if adj.get("reason") else f": net external {adj.get('net_external_cash_flow')}")
        return acct.model_copy(update={"cash_flow_status": adj["status"], "net_external_cash_flow": adj.get("net_external_cash_flow") if adj["status"] == "ADJUSTED" else None,
                                       "notes": list(acct.notes) + [note]})
    return account
