"""
execution/service.py — the Stage 6.0 executor (DESIGN_60 §4–§11): one cycle = monitor open intents -> reconcile with the broker ->
evaluate open positions (exits first) -> evaluate the safety gate -> (PAPER_AUTO only) scan READY setups with reviewable proposals and
submit at most one new entry. The database is the state; every intent is persisted before the broker is contacted; an ambiguous POST
is never retried blindly; the safety gate blocks NEW ENTRIES only.

Everything the rules need is read through the Trader View stream (setups, proposals, safety, market quotes) and the paper broker client.
No model call, no Robinhood, no Stage 4.6B writer, no live endpoint.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from execution import (ATTENTION_ICON, ATTENTION_LABEL, COID_PREFIX, DISCLAIMER, EXIT_TEXT, INTENT_OPEN, MODE_ICONS, MODE_LABELS, STAGE, VERSION)
from execution.broker import AlpacaPaperBroker, BrokerError
from execution import exposure_gate                        # Stage 6.9.1: the UNIFIED_EXTERNAL_EXPOSURE entry control (pure; off by default)
from execution.engine import (D, EntryContext, ExitContext, beginner, blocking, card_key, entry_controls, entry_quantity, entry_coid, execution_id_for, exit_coid, exit_decision,
                              exit_id_for, internal_state, money, pnl, position_id_for, session_policy)
from execution.models import BotStatus, ControlEvaluation, Finding, Intent, Position, SymbolCard
from execution.store import ExecutionStore, ExecutionStoreError
from paper import alpaca_order_rules as RU
from setups.market_hours import session_date

log = logging.getLogger(__name__)
ENV_CACHE_SECONDS = 300.0
RUN_ROW_HEARTBEAT_S = 300.0                                 # quiet cycles write a run row at most this often; active cycles always do
_STATE_OF = {RU.SUBMITTED: "SUBMITTED", RU.BROKER_ACCEPTED: "SUBMITTED", RU.PARTIALLY_FILLED: "PARTIALLY_FILLED", RU.FILLED: "FILLED", RU.CANCELED: "CANCELED",
             RU.BROKER_REJECTED: "REJECTED", RU.RR: "RECONCILIATION_REQUIRED"}
_TERMINAL = ("FILLED", "CANCELED", "REJECTED", "NOT_SENT", "FAILED")
CYCLE_STEPS = {"ORDERS": "Checking open orders with the broker", "RECONCILE": "Reconciling broker positions", "POSITIONS": "Monitoring bot positions and exits",
               "SAFETY": "Running the risk checks", "ENTRIES": "Evaluating READY tickers for entry"}   # display only (ExecutionService.progress)


class ExecutionRefused(RuntimeError):
    def __init__(self, code: str, text: str):
        super().__init__(f"{code}: {text}")
        self.code, self.text = code, text


class SimulatedCrash(RuntimeError):
    """Raised by test hooks to simulate a process death at a precise point; nothing after the hook runs."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


_SIMULATED = {"broker": None}


def shared_simulated():
    """The process-wide simulated broker (EXECUTION_BROKER=simulated, the browser harness)."""
    if _SIMULATED["broker"] is None:
        from execution.simulated import SimulatedPaperBroker
        _SIMULATED["broker"] = SimulatedPaperBroker()
    return _SIMULATED["broker"]


def default_broker() -> AlpacaPaperBroker:
    if str(getattr(config, "EXECUTION_BROKER", "alpaca_paper")).strip().lower() == "simulated":
        return AlpacaPaperBroker(simulated=True, wire=shared_simulated)
    return AlpacaPaperBroker()


class ExecutionService:
    def __init__(self, stream_fn: Callable[[], object], store: Optional[ExecutionStore] = None, *, broker: Optional[AlpacaPaperBroker] = None,
                 broker_fn: Callable[[], AlpacaPaperBroker] = default_broker, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), owner: Optional[str] = None,
                 price_fn: Optional[Callable[[str], Tuple[Optional[Decimal], str, Optional[str]]]] = None):
        self.stream_fn = stream_fn
        self.store = store or ExecutionStore()
        self._broker = broker
        self.broker_fn = broker_fn
        self.clock = clock
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.price_fn = price_fn
        self.hooks: Dict[str, Callable[[dict], None]] = {}
        self.ready = False
        self.environment_cache: Dict[str, Any] = {}
        self._env_at: Optional[datetime] = None
        self.recon_healthy = True
        self.recon_reasons: List[str] = []
        self._broker_positions: List[dict] = []
        self._broker_positions_at: Optional[str] = None                      # when _reconcile last read the broker positions (display only)
        self.progress: Optional[Dict[str, Any]] = None                       # the cycle in progress: step and ticker (display only; never persisted)
        self._last_touch: Dict[str, str] = {}                                 # the last reason written per control (display only)
        self._open_orders: List[dict] = []
        self._clock_cache: Optional[dict] = None
        self._finding_sigs: set = set()
        self.last_cycle: Optional[dict] = None
        self.last_safety = None
        self._last_run_row_at: Optional[datetime] = None
        self._lock = threading.RLock()
        self.max_open_positions = int(config.MAX_OPEN_POSITIONS)
        self.qty_cap = int(getattr(config, "EXECUTION_MAX_SHARES_PER_ENTRY", 0) or 0)
        self.near_close_minutes = int(getattr(config, "EXECUTION_NEAR_CLOSE_MINUTES", 15))
        self.max_hold_sessions = int(getattr(config, "EXECUTION_MAX_HOLD_SESSIONS", 5))
        self.demand_exit = int(getattr(config, "EXECUTION_DEMAND_EXIT", 35))
        self.demand_exit_evals = int(getattr(config, "EXECUTION_DEMAND_EXIT_EVALS", 2))
        self.stretch_exit = float(getattr(config, "EXECUTION_STRETCH_EXIT", -20.0))
        self.max_exit_cycles = int(getattr(config, "EXECUTION_MAX_EXIT_CYCLES", 3))
        self.lookup_span_s = float(getattr(config, "EXECUTION_LOOKUP_SPAN_SECONDS", 30))
        self.max_entries_per_cycle = int(getattr(config, "EXECUTION_MAX_ENTRIES_PER_CYCLE", 1))

    # ---------------------------------------------------------------- collaborators -------------------------------------------
    @property
    def broker(self) -> AlpacaPaperBroker:
        if self._broker is None:
            self._broker = self.broker_fn()
        return self._broker

    @broker.setter
    def broker(self, value: AlpacaPaperBroker) -> None:
        self._broker = value
        self.environment_cache, self._env_at = {}, None

    @property
    def setups(self):
        return self.stream_fn().setups

    @property
    def proposals(self):
        return self.stream_fn().proposals

    @property
    def safety(self):
        return self.stream_fn().safety

    def _hook(self, name: str, **ctx) -> None:
        fn = self.hooks.get(name)
        if fn is not None:
            fn(ctx)

    def _record(self, fn: Callable[[], Any]) -> None:
        """Display-only bookkeeping (cycle progress, per-ticker evaluations, per-position decisions, the last control text): any failure is logged
        and swallowed here, so recording can never change or interrupt order handling. No decision reads what is recorded."""
        try:
            self._hook("before_record")                                             # tests inject a recording failure at every call site
            fn()
        except Exception as exc:  # noqa: BLE001 - display only
            log.warning("execution activity record skipped (%s)", type(exc).__name__)

    def _mark(self, kind: str, **data) -> None:
        """Stage 6.10A forward evidence (instrumentation only): hands a decision-time record to the forward-validation capture, an in-memory sink
        (no I/O, not this executor's lock). Called only through _record, so any failure is logged and swallowed; no decision reads it back."""
        cap = getattr(getattr(self.stream_fn(), "forward_validation", None), "capture", None)
        if cap is not None:
            data.setdefault("run_id", (self.progress or {}).get("run_id"))
            cap.marker(kind, **data)

    def _touch(self, now: datetime, control_id: str, result: str, reason: str, inp: Optional[dict] = None, error: Optional[str] = None) -> None:
        self._record(lambda: self._last_touch.__setitem__(control_id, reason[:300]))   # display only: the per-position decision text of this cycle
        try:
            self.store.touch_controls([ControlEvaluation(control_id=control_id, result=result, reason=reason[:300], input=inp or {})], now, error)
        except Exception as exc:  # noqa: BLE001 - liveness bookkeeping never breaks the loop
            log.warning("execution control %s not saved (%s)", control_id, type(exc).__name__)

    # ---------------------------------------------------------------- mode -------------------------------------------------------
    def mode(self) -> dict:
        return self.store.mode()

    def _environment(self, now: datetime, force: bool = False) -> dict:
        if force or self._env_at is None or (now - self._env_at).total_seconds() > ENV_CACHE_SECONDS or not self.environment_cache:
            try:
                self.environment_cache = self.broker.environment(now)
            except Exception as exc:  # noqa: BLE001
                self.environment_cache = {"paper": False, "host": "", "label": getattr(self.broker, "label", ""), "simulated": False, "error": type(exc).__name__, "checked_at": _iso(now)}
            self._env_at = now
            self._touch(now, "PAPER_ENVIRONMENT", "PASS" if self.environment_cache.get("paper") else "BLOCK",
                        "paper environment confirmed" if self.environment_cache.get("paper") else f"not confirmed ({self.environment_cache.get('error') or 'no proof'})",
                        {k: self.environment_cache.get(k) for k in ("host", "label", "simulated", "status", "error")})
        return self.environment_cache

    def _safety_view(self, now: datetime, evaluate: bool = False):
        try:
            self.last_safety = self.safety.evaluate("SCHEDULED", now) if evaluate else self.safety.current(now=now)
        except Exception as exc:  # noqa: BLE001 - the gate records its own failure as UNKNOWN; the executor treats None as blocked
            log.warning("execution: safety evaluation failed (%s)", type(exc).__name__)
            self.last_safety = None
        return self.last_safety

    def enable_auto(self, actor: str = "user", reason: Optional[str] = None, now: Optional[datetime] = None) -> dict:
        """PAPER_AUTO is an explicit, persisted choice: refused unless the PAPER environment is proven, the account is active, the gate is readable and reconciliation is healthy."""
        with self._lock:
            now = now or self.clock()
            env = self._environment(now, force=True)
            if not env.get("paper"):
                raise ExecutionRefused("PAPER_ENVIRONMENT_UNPROVEN", f"Paper environment not confirmed ({env.get('error') or 'no proof'})")
            if env.get("blocked") or str(env.get("status") or "").upper() != "ACTIVE":
                raise ExecutionRefused("BROKER_ACCOUNT_NOT_ACTIVE", f"Paper account not active ({env.get('status')})")
            sv = self._safety_view(now)
            if sv is None or sv.state == "UNKNOWN":
                raise ExecutionRefused("SAFETY_GATE_UNKNOWN", "Safety status unavailable")
            if not self.ready:
                self.startup(now)
            elif self.needs_broker():
                self.broker.reset_budget()
                self._reconcile(now, "ENABLE", None)
            if not self.recon_healthy:
                raise ExecutionRefused("RECONCILIATION_REQUIRED", "; ".join(self.recon_reasons) or "reconciliation required")
            if self.mode()["mode"] != "PAPER_AUTO":
                self.store.set_mode("PAPER_AUTO", now, actor, reason, env)
                self._touch(now, "EXECUTION_MODE", "RUN", f"PAPER_AUTO enabled by {actor}", {"actor": actor, "reason": reason})
            return self.status(now).model_dump()

    def disable_auto(self, actor: str = "user", reason: Optional[str] = None, now: Optional[datetime] = None) -> dict:
        """Back to MONITOR_ONLY: no new entries. Monitoring, reconciliation and deterministic exits of existing paper positions continue."""
        with self._lock:
            now = now or self.clock()
            if self.mode()["mode"] != "MONITOR_ONLY" or not self.mode()["persisted"]:
                self.store.set_mode("MONITOR_ONLY", now, actor, reason, self.environment_cache)
                self._touch(now, "EXECUTION_MODE", "RUN", f"MONITOR_ONLY set by {actor}", {"actor": actor, "reason": reason})
            return self.status(now).model_dump()

    # ---------------------------------------------------------------- startup / cycle -----------------------------------------
    def needs_broker(self) -> bool:
        return self.mode()["mode"] == "PAPER_AUTO" or bool(self.store.open_intents()) or bool(self.store.positions(open_only=True))

    def startup(self, now: Optional[datetime] = None) -> dict:
        """Startup reconciliation BEFORE any write: restore the mode, load open intents / positions, query the broker, reconcile. Nothing is submitted here."""
        with self._lock:
            now = now or self.clock()
            self.store.ensure()
            summary = {"mode": self.mode()["mode"], "open_intents": len(self.store.open_intents()), "open_positions": len(self.store.positions(open_only=True)), "broker_queried": False}
            if self.needs_broker():
                self.broker.reset_budget()
                summary["broker_queried"] = True
                summary["monitored"] = self._monitor_intents(now, "STARTUP")
                summary["findings"] = self._reconcile(now, "STARTUP", None)
                self._touch(now, "BROKER_RECONCILIATION", "PASS" if self.recon_healthy else "BLOCK", "startup reconciliation: " + ("healthy" if self.recon_healthy else "; ".join(self.recon_reasons)), summary)
            self.ready = True
            return summary

    def run_cycle(self, now: Optional[datetime] = None, trigger: str = "SCHEDULED") -> dict:
        with self._lock:
            now = now or self.clock()
            run_id = uuid.uuid4().hex
            if not self.ready:
                self.startup(now)
            mode = self.mode()["mode"]
            if not self.needs_broker():
                self.last_cycle = {"at": _iso(now), "finished_at": _iso(now), "result": "IDLE", "trigger": trigger, "mode": mode}
                return self.last_cycle                                            # nothing to do: no broker call, no write
            summary: Dict[str, Any] = {"trigger": trigger, "mode": mode}
            error, result = None, "RUN"
            started = time.monotonic()
            self.progress = {"run_id": run_id, "trigger": trigger, "mode": mode, "started_at": _iso(now), "step": None, "step_label": None, "symbol": None, "index": None, "total": None}
            self.broker.reset_budget()
            try:
                self._step("ORDERS")
                summary["monitored"] = self._monitor_intents(now, trigger)
                self._step("RECONCILE")
                summary["findings"] = self._reconcile(now, trigger, run_id)
                self._step("POSITIONS")
                summary.update(self._evaluate_positions(now, trigger))
                self._step("SAFETY")
                sv = self._safety_view(now, evaluate=True)
                summary["safety"] = sv.state if sv is not None else None
                self._touch(now, "EXECUTION_MODE", "PASS" if mode == "PAPER_AUTO" else "BLOCK", f"mode {mode}", {"mode": mode})
                self._touch(now, "SAFETY_GATE", "PASS" if (sv is not None and sv.allowed_new_entry) else "BLOCK",
                            f"{getattr(sv, 'state', 'UNKNOWN')}" + (f": {sv.reason_text}" if sv is not None and sv.reason_text else ""), {"state": getattr(sv, "state", None)})
                if mode == "PAPER_AUTO":
                    self._step("ENTRIES")
                    summary["entries"] = self._scan_entries(now, trigger, sv)
            except SimulatedCrash:
                self.progress = None
                raise
            except Exception as exc:  # noqa: BLE001 - never crash the loop; the run row says what failed
                error, result = f"{type(exc).__name__}: {exc}"[:500], "ERROR"
                log.warning("execution cycle failed: %s", type(exc).__name__)
            finished = now + timedelta(seconds=time.monotonic() - started)              # the measured completion time (start + elapsed), not a second clock
            summary["duration_ms"] = int((finished - now).total_seconds() * 1000)
            active = bool(error or summary.get("monitored") or summary.get("findings") or summary.get("positions") or (summary.get("entries") or {}).get("candidates"))
            if active or self._last_run_row_at is None or (now - self._last_run_row_at).total_seconds() >= RUN_ROW_HEARTBEAT_S:
                try:
                    self.store.add_run(run_id, now, finished, trigger, mode, result, summary, error, self.owner)
                    self._last_run_row_at = now
                except Exception as exc:  # noqa: BLE001
                    log.warning("execution run not saved (%s)", type(exc).__name__)
            self._record(lambda: self._mark("CYCLE_END", now=now, run_id=run_id, finished=finished, mode=mode, trigger=trigger, result=result, error=error))   # Stage 6.10A
            self.progress = None
            self.last_cycle = {"at": _iso(now), "finished_at": _iso(finished), "result": result, "trigger": trigger, "mode": mode, "run_id": run_id, "summary": summary, "error": error}
            return self.last_cycle

    def _step(self, step: str) -> None:
        """Display only: the step of the cycle in progress (a dict assignment; it cannot fail and no decision reads it)."""
        if self.progress is not None:
            self._record(lambda: self.progress.update(step=step, step_label=CYCLE_STEPS[step], symbol=None, index=None, total=None))

    # ---------------------------------------------------------------- intents: monitoring, lookups, fills -----------------------
    def _monitor_intents(self, now: datetime, trigger: str) -> int:
        n = 0
        for it in self.store.open_intents():
            n += 1
            try:
                self._monitor_intent(it, now, trigger)
            except BrokerError as exc:
                self._touch(now, "POSITION_RECONCILIATION" if it.intent_type == "EXIT" else "BROKER_RECONCILIATION", "UNKNOWN", f"{it.symbol}: broker unreadable ({exc.code})", {"execution_id": it.execution_id}, exc.code)
        return n

    def _monitor_intent(self, it: Intent, now: datetime, trigger: str) -> None:
        if it.broker_order_id:
            order = self.broker.order_by_id(it.broker_order_id) or self.broker.order_by_client_id(it.client_order_id)
            if order is None:
                self._finding(now, trigger, Finding(kind="ORDER_MISSING_AT_BROKER", symbol=it.symbol, execution_id=it.execution_id, broker_order_id=it.broker_order_id,
                                                    detail={"state": it.state}))
                if it.state != "RECONCILIATION_REQUIRED":
                    self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", failure_code="ORDER_MISSING_AT_BROKER")
                return
            self._apply_order(it, order, now, "monitor")
            return
        if it.state == "PENDING" and it.attempt_count == 0:
            found = self.broker.order_by_client_id(it.client_order_id)
            if found is not None:
                self._finding(now, trigger, Finding(kind="ORDER_ADOPTED", symbol=it.symbol, blocking=False, execution_id=it.execution_id, broker_order_id=found["id"], detail={"status": found["status"]}))
                self._apply_order(it, found, now, "lookup")
            else:                                                                       # never attempted by a previous run: NOT submitted blindly
                self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="STALE_PENDING", resolved_at=_iso(now),
                                         failure_text="intent created by a previous run was never attempted; it is not submitted blindly")
                self._after_terminal(it, "NOT_SENT", now)
            return
        last = _parse(it.last_lookup_at)
        if last is not None and (now - last).total_seconds() < self.lookup_span_s:
            return                                                                      # at least lookup_span_s between lookups
        found = self.broker.order_by_client_id(it.client_order_id)
        if found is not None:
            self._finding(now, trigger, Finding(kind="ORDER_ADOPTED", symbol=it.symbol, blocking=False, execution_id=it.execution_id, broker_order_id=found["id"], detail={"status": found["status"]}))
            self._apply_order(it, found, now, "lookup")
            return
        count = it.lookup_count + 1
        if count >= 2:
            self.store.update_intent(it.execution_id, now, state="NOT_SENT", lookup_count=count, last_lookup_at=_iso(now), failure_code="NOT_FOUND_AFTER_LOOKUPS", resolved_at=_iso(now),
                                     failure_text=f"not found at the broker in {count} lookups at least {int(self.lookup_span_s)} s apart")
            self._finding(now, trigger, Finding(kind="ORDER_NOT_FOUND_FINAL", symbol=it.symbol, blocking=False, execution_id=it.execution_id, detail={"lookups": count}))
            self._after_terminal(it, "NOT_SENT", now)
        else:
            self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", lookup_count=count, last_lookup_at=_iso(now))

    def _apply_order(self, it: Intent, order: dict, now: datetime, source: str) -> None:
        """The broker's view of OUR order: identity must match exactly; the status maps through the frozen Stage 4.6B table; fills move positions."""
        if order.get("client_order_id") != it.client_order_id or str(order.get("symbol") or "").upper() != it.symbol or order.get("side") != it.side or order.get("qty") != str(it.qty):
            self._finding(now, source, Finding(kind="UNKNOWN_STATUS", symbol=it.symbol, execution_id=it.execution_id, broker_order_id=order.get("id"),
                                               detail={"problem": "order identity differs from the intent", "order": {k: order.get(k) for k in ("client_order_id", "symbol", "side", "qty")}}))
            if it.state != "RECONCILIATION_REQUIRED":
                self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", failure_code="INCONSISTENT_ORDER")
            return
        st, code = RU.map_status(order.get("status"))
        state = _STATE_OF[st]
        if state == "FILLED" and int(order.get("filled_qty") or 0) < it.qty:
            state, code = "RECONCILIATION_REQUIRED", "FILLED_WITHOUT_QUANTITY"             # a filled status without the fill is inconsistent: never a silent loss
        fields: Dict[str, Any] = {"broker_status": order.get("status"), "state": state, "filled_qty": int(order.get("filled_qty") or 0), "filled_avg_price": order.get("filled_avg_price")}
        if not it.broker_order_id:
            fields["broker_order_id"] = order["id"]
        if state == "RECONCILIATION_REQUIRED":
            fields["failure_code"] = code
            self._finding(now, source, Finding(kind="UNKNOWN_STATUS", symbol=it.symbol, execution_id=it.execution_id, broker_order_id=order["id"], detail={"status": order.get("status"), "code": code}))
        elif state == "REJECTED":
            fields.update(failure_code="BROKER_REJECTED", failure_text="rejected by the broker; no automatic resubmission", resolved_at=_iso(now))
        elif state == "CANCELED":
            fields.update(failure_code="CANCELED_OR_EXPIRED", failure_text="canceled or expired at the broker; no automatic replacement", resolved_at=_iso(now))
        elif state == "FILLED":
            fields["resolved_at"] = _iso(now)
        new_fill = int(order.get("filled_qty") or 0) > it.filled_qty
        pos_missing = it.intent_type == "ENTRY" and int(order.get("filled_qty") or 0) > 0 and self.store.position_for_execution(it.execution_id) is None
        if new_fill or pos_missing:
            self._hook("before_fill_persisted", intent=it, order=order)
            self._apply_fill(it, order, fields, now)
        else:
            self.store.record_order(order, it.execution_id, now, source, intent_fields=fields)
        if state in _TERMINAL:
            self._after_terminal(it.model_copy(update=fields), state, now)

    def _apply_fill(self, it: Intent, order: dict, fields: dict, now: datetime) -> None:
        filled, avg = int(order.get("filled_qty") or 0), order.get("filled_avg_price")
        if it.intent_type == "ENTRY":
            pos = self.store.position_for_execution(it.execution_id)
            if pos is None:
                new = Position(position_id=position_id_for(it.execution_id), execution_id=it.execution_id, proposal_id=it.proposal_id, setup_id=it.setup_id, symbol=it.symbol, side="LONG",
                               quantity=filled, entry_qty=filled, average_entry_price=avg or "0.00", opened_at=_iso(now), invalidation_price=it.invalidation_price, risk_budget=it.risk_budget,
                               source="BROKER_FILLS", broker=self.broker.label, state="OPEN", updated_at=_iso(now))
                ev = {"kind": "OPENED", "from_state": None, "to_state": "OPEN", "reason": f"{filled} of {it.qty} filled at {avg}", "detail": {"broker_order_id": order["id"], "status": order.get("status")}}
                self.store.apply_fill(order, fields, it.execution_id, now, new_position=new, position_event=ev)
            else:
                delta = filled - it.filled_qty
                upd = {"position_id": pos.position_id, "quantity": pos.quantity + max(0, delta), "entry_qty": max(pos.entry_qty, filled), "average_entry_price": avg or pos.average_entry_price}
                ev = {"position_id": pos.position_id, "kind": "FILL_UPDATED", "from_state": pos.state, "to_state": pos.state, "reason": f"{filled} of {it.qty} filled at {avg}",
                      "detail": {"broker_order_id": order["id"], "status": order.get("status")}}
                self.store.apply_fill(order, fields, it.execution_id, now, position_update=upd, position_event=ev)
            return
        pos = self.store.position(it.position_id) if it.position_id else None
        if pos is None:
            self.store.record_order(order, it.execution_id, now, "fill", intent_fields=fields)
            return
        delta = max(0, filled - it.filled_qty)
        exit_qty = pos.exit_qty + delta
        prev_avg, fill_px, entry_avg = D(pos.average_exit_price) or Decimal("0"), D(avg) or Decimal("0"), D(pos.average_entry_price) or Decimal("0")
        new_avg = ((prev_avg * pos.exit_qty) + fill_px * delta) / exit_qty if exit_qty > 0 else None
        remaining = max(0, pos.quantity - delta)
        closed = remaining <= 0
        state = "CLOSED" if closed else pos.state
        upd = {"position_id": pos.position_id, "quantity": remaining, "exit_qty": exit_qty, "average_exit_price": money(new_avg), "state": state,
               "realized_pnl": money((new_avg - entry_avg) * exit_qty) if new_avg is not None else None, "closed_at": _iso(now) if closed else None}
        ev = {"position_id": pos.position_id, "kind": "CLOSED" if closed else "EXIT_FILL", "from_state": pos.state, "to_state": state, "reason": pos.exit_reason,
              "detail": {"broker_order_id": order["id"], "status": order.get("status"), "exit_qty": exit_qty, "average_exit_price": money(new_avg), "realized_pnl": upd["realized_pnl"]}}
        self.store.apply_fill(order, fields, it.execution_id, now, position_update=upd, position_event=ev)

    def _after_terminal(self, it: Intent, state: str, now: datetime) -> None:
        """An exit that did not fully fill hands the position back to EXIT_REQUIRED (the next cycle may open the next exit cycle, bounded)."""
        if it.intent_type != "EXIT" or not it.position_id:
            return
        pos = self.store.position(it.position_id)
        if pos is None or pos.state == "CLOSED" or pos.quantity <= 0:
            return
        if state in ("CANCELED", "REJECTED", "NOT_SENT", "FAILED") and pos.state != "EXIT_REQUIRED":
            self.store.update_position(pos.position_id, now, {"kind": "EXIT_NOT_DONE", "from_state": pos.state, "to_state": "EXIT_REQUIRED", "reason": f"exit intent {state}",
                                                              "detail": {"execution_id": it.execution_id, "exit_cycle": it.exit_cycle, "failure_code": it.failure_code}}, state="EXIT_REQUIRED")

    # ---------------------------------------------------------------- reconciliation ------------------------------------------
    def _finding(self, now: datetime, trigger: str, f: Finding, run_id: Optional[str] = None) -> None:
        sig = (f.kind, f.symbol, f.execution_id, f.position_id, f.broker_order_id, str(sorted(f.detail.items())))
        if sig in self._finding_sigs:
            return
        self._finding_sigs.add(sig)
        try:
            self.store.add_findings([f], now, trigger, run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("reconciliation finding not saved (%s)", type(exc).__name__)

    def _set_recon(self, healthy: bool, reasons: List[str]) -> None:
        self.recon_healthy, self.recon_reasons = bool(healthy), list(reasons)

    def _reconcile(self, now: datetime, trigger: str, run_id: Optional[str]) -> int:
        reasons: List[str] = []
        try:
            bpos = self.broker.positions()
            open_orders = self.broker.orders("open")
        except BrokerError as exc:
            self._finding(now, trigger, Finding(kind="BROKER_UNAVAILABLE", detail={"code": exc.code}), run_id)
            self._set_recon(False, [f"broker unavailable ({exc.code})"])
            self._touch(now, "BROKER_RECONCILIATION", "UNKNOWN", f"broker unavailable ({exc.code})", {}, exc.code)
            return 1
        self._broker_positions, self._open_orders = bpos, open_orders
        self._broker_positions_at = _iso(now)
        n = 0
        internal_open = self.store.positions(open_only=True)
        open_intents = self.store.open_intents()
        closed_syms = {p.symbol for p in self.store.positions(open_only=False, limit=500) if p.state == "CLOSED"}
        by_sym = {p["symbol"]: p for p in bpos}
        for pos in internal_open:
            b = by_sym.get(pos.symbol)
            exiting = pos.state == "EXIT_SUBMITTED" or any(i.position_id == pos.position_id and i.intent_type == "EXIT" for i in open_intents)
            problem = None
            if b is None and pos.quantity > 0 and not exiting:
                problem = ("BROKER_FLAT_INTERNAL_OPEN", {"internal_qty": pos.quantity})
            elif b is not None and b["qty"] != pos.quantity and not exiting:
                problem = ("QTY_MISMATCH", {"internal_qty": pos.quantity, "broker_qty": b["qty"]})
            if problem is not None:
                kind, det = problem
                n += 1
                reasons.append(f"{pos.symbol}: {kind.lower().replace('_', ' ')}")
                self._finding(now, trigger, Finding(kind=kind, symbol=pos.symbol, position_id=pos.position_id, detail=det), run_id)
                if pos.state != "ATTENTION":
                    self.store.update_position(pos.position_id, now, {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": kind, "detail": det}, state="ATTENTION", attention_reason=kind)
            elif pos.state == "ATTENTION" and pos.attention_reason in ("QTY_MISMATCH", "BROKER_FLAT_INTERNAL_OPEN"):
                back = "EXIT_REQUIRED" if pos.exit_reason else "OPEN"
                self.store.update_position(pos.position_id, now, {"kind": "ATTENTION_CLEARED", "from_state": "ATTENTION", "to_state": back, "reason": "broker and internal quantities agree again"},
                                           state=back, attention_reason=None)
            elif pos.state == "ATTENTION":
                reasons.append(f"{pos.symbol}: {(pos.attention_reason or 'attention').lower().replace('_', ' ')}")
        internal_syms = {p.symbol for p in internal_open} | {i.symbol for i in open_intents if i.intent_type == "ENTRY"}
        for b in bpos:
            if b["symbol"] in internal_syms:
                continue
            kind = "INTERNAL_FLAT_BROKER_POSITION" if b["symbol"] in closed_syms else "EXTERNAL_POSITION"
            n += 1
            if kind != "EXTERNAL_POSITION":
                reasons.append(f"{b['symbol']}: broker position without an internal position")
            self._finding(now, trigger, Finding(kind=kind, symbol=b["symbol"], blocking=kind != "EXTERNAL_POSITION", detail={"broker_qty": b["qty"], "avg_entry_price": b.get("avg_entry_price")}), run_id)
        per_symbol: Counter = Counter()
        for o in open_orders:
            per_symbol[str(o.get("symbol") or "").upper()] += 1
            coid = o.get("client_order_id") or ""
            if coid.startswith(COID_PREFIX) and self.store.intent_by_coid(coid) is None:
                n += 1
                reasons.append(f"{o.get('symbol')}: broker order {coid} has no internal intent")
                self._finding(now, trigger, Finding(kind="UNKNOWN_INTERNAL_ORDER", symbol=o.get("symbol"), broker_order_id=o.get("id"), detail={"client_order_id": coid, "status": o.get("status")}), run_id)
        for sym, cnt in per_symbol.items():
            if cnt > 1:
                n += 1
                reasons.append(f"{sym}: {cnt} open broker orders")
                self._finding(now, trigger, Finding(kind="MULTIPLE_ORDERS", symbol=sym, detail={"count": cnt}), run_id)
        for i in open_intents:
            if i.state == "RECONCILIATION_REQUIRED":
                reasons.append(f"{i.symbol}: intent {i.execution_id[:8]} needs reconciliation ({i.failure_code or 'ambiguous'})")
        self._set_recon(not reasons, reasons)
        self._touch(now, "BROKER_RECONCILIATION", "PASS" if not reasons else "BLOCK", "healthy" if not reasons else "; ".join(reasons), {"findings": n, "broker_positions": len(bpos), "open_orders": len(open_orders)})
        self._touch(now, "POSITION_RECONCILIATION", "PASS" if not any("mismatch" in r or "flat" in r for r in reasons) else "BLOCK",
                    f"{len(internal_open)} internal open position(s) checked", {"internal_open": len(internal_open), "broker_positions": [p['symbol'] for p in bpos]})
        return n

    # ---------------------------------------------------------------- positions and exits -------------------------------------
    def _price(self, symbol: str, now: datetime) -> Tuple[Optional[Decimal], str, Optional[str]]:
        if self.price_fn is not None:
            return self.price_fn(symbol)
        st = self.stream_fn()
        try:
            q = st.market.quote(symbol)
            if q.status == "OK" and q.price is not None:
                return D(q.price), "QUOTE", _iso(datetime.fromtimestamp(float(q.ts), tz=timezone.utc)) if q.ts else None
        except Exception as exc:  # noqa: BLE001
            log.warning("execution: quote unavailable for %s (%s)", symbol, type(exc).__name__)
        try:
            finals = [c for c in st.candles(symbol) if c.final]
            if finals:
                return D(finals[-1].close), "MINUTE_CLOSE", _iso(datetime.fromtimestamp(float(finals[-1].time), tz=timezone.utc))
        except Exception as exc:  # noqa: BLE001
            log.warning("execution: candles unavailable for %s (%s)", symbol, type(exc).__name__)
        return None, "UNAVAILABLE", None

    def _exit_context(self, pos: Position, now: datetime) -> ExitContext:
        price, src, at = self._price(pos.symbol, now)
        setup = self.setups.current(pos.symbol)
        rows = self.setups.store.episodes(pos.symbol, 20)
        ep_row = next((r for r in rows if r.get("setup_id") == pos.setup_id), None)
        ep_rec = self.setups.store.open_episode(pos.symbol)
        if ep_rec is not None and ep_rec.setup_id != pos.setup_id:
            ep_rec = None
        return ExitContext(now=now, price=price, price_source=src, price_at=at, setup=setup, episode=ep_row, episode_record=ep_rec, inputs=setup.inputs,
                           max_hold_sessions=self.max_hold_sessions, demand_exit=self.demand_exit, demand_exit_evals=self.demand_exit_evals, stretch_exit=self.stretch_exit)

    def _evaluate_positions(self, now: datetime, trigger: str) -> dict:
        out = {"positions": 0, "exits_decided": 0, "exits_submitted": 0, "exits_deferred": 0, "decisions": []}
        open_intents = self.store.open_intents()
        decide = lambda pos, decision, text: self._record(lambda: out["decisions"].append({"symbol": pos.symbol, "position_id": pos.position_id, "decision": decision,  # noqa: E731
                                                                                         "text": text() if callable(text) else text}))
        for pos in self.store.positions(open_only=True):
            out["positions"] += 1
            if any(i.position_id == pos.position_id and i.intent_type == "EXIT" for i in open_intents):
                decide(pos, "EXIT_IN_PROGRESS", "An exit order is already at the broker; the order monitor follows it.")
                continue                                                                    # an exit is in flight: the monitor owns it
            if pos.state == "ATTENTION":
                self._touch(now, "EXIT_TRIGGER", "UNKNOWN", f"{pos.symbol}: position needs attention ({pos.attention_reason}); no automatic action", {"position_id": pos.position_id})
                decide(pos, "ATTENTION", lambda: f"Needs attention ({(pos.attention_reason or 'manual review').replace('_', ' ').lower()}); no automatic action.")
                continue
            ctx = self._exit_context(pos, now)
            dec = exit_decision(pos, ctx)
            fields: Dict[str, Any] = {"last_price": money(ctx.price), "last_price_at": ctx.price_at, "demand_low_streak": dec.demand_low_streak,
                                      "last_demand_eval_at": dec.last_demand_eval_at, "sessions_held": dec.sessions_held}
            if dec.reason:
                out["exits_decided"] += 1
                if pos.state != "EXIT_REQUIRED":
                    fields.update(state="EXIT_REQUIRED", exit_reason=dec.reason, exit_requested_at=_iso(now), weakening_reason=None)
                    pos = self.store.update_position(pos.position_id, now, {"kind": "EXIT_REQUIRED", "from_state": pos.state, "to_state": "EXIT_REQUIRED", "reason": dec.reason, "detail": dec.detail}, **fields)
                    self._touch(now, "EXIT_TRIGGER", "RUN", f"{pos.symbol}: {dec.reason}", dec.detail)
                else:
                    pos = self.store.update_position(pos.position_id, now, None, **fields)
                self._record(lambda: self._last_touch.pop("EXIT_SUBMISSION", None))
                if self._maybe_submit_exit(pos, now, trigger):
                    out["exits_submitted"] += 1
                    decide(pos, "EXIT_SENT", lambda: f"Exit required: {EXIT_TEXT.get(dec.reason, dec.reason)}. Exit order sent to the broker ({self._last_touch.get('EXIT_SUBMISSION') or 'see its order'}).")
                else:
                    out["exits_deferred"] += 1
                    decide(pos, "EXIT_DEFERRED", lambda: f"Exit required: {EXIT_TEXT.get(dec.reason, dec.reason)}. Not sent this cycle: {self._last_touch.get('EXIT_SUBMISSION') or 'deferred'}.")
            else:
                new_state = "WEAKENING" if dec.weakening else "HEALTHY"
                ev = None
                if pos.state in ("OPEN", "HEALTHY", "WEAKENING") and new_state != pos.state:
                    fields.update(state=new_state, weakening_reason=dec.weakening)
                    ev = {"kind": "STATE", "from_state": pos.state, "to_state": new_state, "reason": dec.weakening or "healthy", "detail": dec.detail}
                self.store.update_position(pos.position_id, now, ev, **fields)
                self._touch(now, "EXIT_TRIGGER", "PASS", f"{pos.symbol}: no exit condition", dec.detail)
                decide(pos, "HOLD", lambda: "No exit condition is met; holding." + (f" Weakening: {dec.weakening}." if dec.weakening else ""))
        return out

    def _maybe_submit_exit(self, pos: Position, now: datetime, trigger: str) -> bool:
        try:
            self._clock_cache = self.broker.clock()
        except BrokerError:
            self._clock_cache = None
        sp = session_policy(now, self._clock_cache, self.near_close_minutes)
        if sp.result != "PASS":
            self._touch(now, "EXIT_SUBMISSION", "WARN", f"{pos.symbol}: exit deferred — {sp.reason}", sp.input)
            return False
        if pos.exit_cycle >= self.max_exit_cycles:
            self.store.update_position(pos.position_id, now, {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": "EXIT_ATTEMPTS_EXHAUSTED"},
                                       state="ATTENTION", attention_reason="EXIT_ATTEMPTS_EXHAUSTED")
            self._touch(now, "EXIT_SUBMISSION", "BLOCK", f"{pos.symbol}: {pos.exit_cycle} exit attempts exhausted; needs attention", {"position_id": pos.position_id})
            return False
        try:
            b = self.broker.position(pos.symbol)
        except BrokerError as exc:
            self._touch(now, "EXIT_SUBMISSION", "UNKNOWN", f"{pos.symbol}: broker position unreadable ({exc.code})", {}, exc.code)
            return False
        if b is None or int(b["qty"]) < pos.quantity:
            det = {"internal_qty": pos.quantity, "broker_qty": b["qty"] if b else 0}
            self._finding(now, trigger, Finding(kind="QTY_MISMATCH" if b else "BROKER_FLAT_INTERNAL_OPEN", symbol=pos.symbol, position_id=pos.position_id, detail=det))
            self.store.update_position(pos.position_id, now, {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": "QTY_MISMATCH", "detail": det},
                                       state="ATTENTION", attention_reason="QTY_MISMATCH")
            self._touch(now, "EXIT_SUBMISSION", "BLOCK", f"{pos.symbol}: broker holds {det['broker_qty']} vs internal {pos.quantity}; no automatic action", det)
            return False
        cycle = pos.exit_cycle + 1
        intent = Intent(execution_id=exit_id_for(pos.position_id, cycle), intent_type="EXIT", symbol=pos.symbol, side="sell", qty=pos.quantity, client_order_id=exit_coid(pos.position_id, cycle),
                        proposal_id=pos.proposal_id, setup_id=pos.setup_id, position_id=pos.position_id, exit_cycle=cycle, exit_reason=pos.exit_reason, reference_price=pos.last_price,
                        invalidation_price=pos.invalidation_price, risk_budget=pos.risk_budget, state="PENDING", created_at=_iso(now), updated_at=_iso(now), created_by=self.owner,
                        environment=dict(self.environment_cache))
        try:
            intent = self.store.insert_intent(intent)
        except ExecutionStoreError:
            existing = self.store.intent(intent.execution_id)
            if existing is None or existing.state not in INTENT_OPEN:
                return False
            intent = existing
        self.store.update_position(pos.position_id, now, {"kind": "EXIT_INTENT", "from_state": pos.state, "to_state": "EXIT_SUBMITTED", "reason": pos.exit_reason, "detail": {"exit_cycle": cycle}},
                                   state="EXIT_SUBMITTED", exit_cycle=cycle)
        self._hook("after_exit_intent", intent=intent)
        self._submit(intent, now, "EXIT_SUBMISSION", recheck=None)
        return True

    # ---------------------------------------------------------------- entries -------------------------------------------------------
    def _legacy_intents(self, symbol: str) -> List[str]:
        try:
            from proposals.account import _default_intents
            return _default_intents(self.store.path, symbol)
        except Exception as exc:  # noqa: BLE001
            return [f"4.6B intent store unreadable ({type(exc).__name__})"]

    def _unified_portfolio(self) -> dict:
        """Stage 6.9.1: the stream's read-only UnifiedPortfolioState, the UNIFIED_EXTERNAL_EXPOSURE input (the Alpaca PAPER account-refresh snapshot plus
        the Robinhood read-only source, which reads at most once per ROBINHOOD_PORTFOLIO_TTL_SECONDS). Any failure is UNAVAILABLE: never an empty
        portfolio. Data only - nothing here or in the gate can place, change or close anything in any account."""
        fn = getattr(self.stream_fn(), "unified_portfolio", None)
        if not callable(fn):
            return {"state": "UNAVAILABLE", "error": "NOT_WIRED", "sources": [], "rows": []}
        try:
            u = fn(refresh=True)
        except Exception as exc:  # noqa: BLE001 - unreadable data is unknown data: the gate's policy decides, never "no holdings"
            log.warning("execution: unified portfolio unavailable (%s)", type(exc).__name__)
            return {"state": "UNAVAILABLE", "error": type(exc).__name__, "sources": [], "rows": []}
        return u if isinstance(u, dict) else {"state": "UNAVAILABLE", "error": "UNREADABLE", "sources": [], "rows": []}

    def exposure_precheck(self, symbols: List[str], unified: Optional[dict], now: Optional[datetime] = None) -> Dict[str, dict]:
        """Stage 6.9.1, display only (GET /api/unified-portfolio): what UNIFIED_EXTERNAL_EXPOSURE decides NOW for each symbol, from `unified` and the
        current proposal's planned entry (the same pure rule and planned quantity as entry_controls). No broker call, no write, never an order: in
        Monitor only this is where the decision is visible, since the executor evaluates entries in Paper Auto cycles only."""
        now = now or self.clock()
        gate = exposure_gate.from_config(config)
        out: Dict[str, dict] = {}
        for sym in symbols:
            try:
                pv = self.proposals.current(sym, now)
                ok = pv.effective_status == "REVIEWABLE"
                ev = exposure_gate.evaluate(sym, entry_quantity(pv.final_qty, self.qty_cap) if ok else 0, D(pv.reference_price) if ok else None, unified, gate,
                                            planned_required=False)
                out[sym] = exposure_gate.precheck_view(ev, gate, pv.effective_status)
            except Exception as exc:  # noqa: BLE001 - display only
                out[sym] = {"control_id": exposure_gate.CONTROL_ID, "symbol": sym, "result": "UNKNOWN", "label": "UNAVAILABLE", "enforced": gate.enabled, "would_block": None,
                            "text": f"pre-check unavailable ({type(exc).__name__})"}
        return out

    def _scan_entries(self, now: datetime, trigger: str, sv) -> dict:
        out: Dict[str, Any] = {"candidates": [], "submitted": [], "blocked": {}, "evaluations": []}
        episodes = [e for e in self.setups.store.open_episodes() if e.state == "READY"]
        if not episodes:
            return out
        self._record(lambda: self._mark("SCAN", now=now, items=[(e.setup_id, e.symbol) for e in episodes]))   # Stage 6.10A: forward evidence (instrumentation only)
        evaluated = lambda sym, result, reasons, **kw: self._record(lambda: out["evaluations"].append({"symbol": sym, "result": result, "reasons": reasons, **kw}))  # noqa: E731
        env = self._environment(now)
        try:
            self._clock_cache = self.broker.clock()
        except BrokerError:
            self._clock_cache = None
        try:
            account, account_error = self.broker.account(), None
        except BrokerError as exc:
            account, account_error = None, exc.code
        open_intents = self.store.open_intents()
        pending_entries = [i for i in open_intents if i.intent_type == "ENTRY" and self.store.position_for_execution(i.execution_id) is None]
        by_sym = {p["symbol"]: p for p in self._broker_positions}
        gate = exposure_gate.from_config(config)                                         # Stage 6.9.1: read at every scan (off by default)
        unified = self._unified_portfolio() if gate.enabled else None                    # Stage 6.9.1: ONE read-only snapshot per scan, only when the gate is on
        budget = self.max_entries_per_cycle
        for n, ep in enumerate(episodes, 1):
            if budget <= 0:
                for rest in episodes[n - 1:]:
                    evaluated(rest.symbol, "SKIPPED", [{"control": "ENTRIES_PER_CYCLE", "text": f"the limit of {self.max_entries_per_cycle} new entr(y/ies) per cycle was used; evaluated again next cycle"}])
                self._record(lambda: self._mark("SKIPPED", now=now, items=[(r.setup_id, r.symbol) for r in episodes[n - 1:]]))   # Stage 6.10A
                break
            sym = ep.symbol
            if self.progress is not None:
                self._record(lambda: self.progress.update(symbol=sym, index=n, total=len(episodes)))
            out["candidates"].append(sym)
            pv = self.proposals.current(sym, now)
            setup = self.setups.current(sym)
            asset, asset_error = None, None
            if pv.effective_status == "REVIEWABLE":
                try:
                    asset = self.broker.asset(sym)
                except BrokerError as exc:
                    asset_error = exc.code
            ctx = EntryContext(now=now, mode=self.mode()["mode"], environment=env, safety=sv, proposal=pv, setup=setup, account=account, account_error=account_error, asset=asset,
                               asset_error=asset_error, broker_position=by_sym.get(sym), broker_open_orders=[o for o in self._open_orders if str(o.get("symbol") or "").upper() == sym],
                               internal_open_intents=[i for i in open_intents if i.symbol == sym], prior_entries_for_setup=len(self.store.intents_for_setup(pv.setup_id)) if pv.setup_id else 0,
                               internal_open_position=self.store.open_position(sym), legacy_intents=self._legacy_intents(sym), broker_positions_count=len(self._broker_positions),
                               pending_entries_count=len(pending_entries), max_open_positions=self.max_open_positions, reconciliation_healthy=self.recon_healthy,
                               reconciliation_reasons=self.recon_reasons, broker_clock=self._clock_cache, near_close_minutes=self.near_close_minutes, qty_cap=self.qty_cap)
            ctx.exposure_gate, ctx.unified_portfolio = gate, unified                    # Stage 6.9.1: the UNIFIED_EXTERNAL_EXPOSURE inputs (off = the v1 controls)
            controls = entry_controls(ctx)
            self.store.touch_controls(controls, now)
            blocked = blocking(controls)
            self._record(lambda: self._mark("EVALUATED", now=now, setup_id=ep.setup_id, symbol=sym, ctx=ctx, controls=controls))   # Stage 6.10A
            if blocked:
                out["blocked"][sym] = [c.control_id for c in blocked]
                evaluated(sym, "BLOCKED", [{"control": c.control_id, "text": c.reason, **exposure_gate.record(c)} for c in blocked])   # Stage 6.9.1: + the gate's record
                continue
            xid = execution_id_for(pv.proposal_id)
            if self.store.intent(xid) is not None:
                out["blocked"][sym] = ["DUPLICATE_ENTRY"]
                evaluated(sym, "BLOCKED", [{"control": "DUPLICATE_ENTRY", "text": "an entry for this proposal already exists"}])
                self._record(lambda: self._mark("DUPLICATE", now=now, setup_id=ep.setup_id, symbol=sym, execution_id=xid))   # Stage 6.10A
                continue
            qty = entry_quantity(pv.final_qty, self.qty_cap)
            snap = {k: getattr(pv, k) for k in ("proposal_id", "setup_id", "status", "effective_status", "reference_price", "price_source", "invalidation_price", "stop_rule", "risk_per_share",
                                                 "risk_budget", "equity", "equity_source", "final_qty", "created_at", "expires_at")}
            intent = Intent(execution_id=xid, intent_type="ENTRY", symbol=sym, side="buy", qty=qty, client_order_id=entry_coid(xid), proposal_id=pv.proposal_id, setup_id=pv.setup_id,
                            reference_price=pv.reference_price, invalidation_price=pv.invalidation_price, risk_budget=pv.risk_budget, proposal_qty=pv.final_qty, state="PENDING",
                            created_at=_iso(now), updated_at=_iso(now), created_by=self.owner, proposal_snapshot=snap, environment=dict(env))
            intent.environment["entry_controls"] = exposure_gate.intent_record(gate, next((c for c in controls if c.control_id == exposure_gate.CONTROL_ID), None))   # Stage 6.9.1
            intent = self.store.insert_intent(intent)
            self._hook("after_intent", intent=intent)
            self._record(lambda: self._mark("INTENT", now=now, setup_id=ep.setup_id, intent=intent))   # Stage 6.10A
            self._record(lambda: self._last_touch.pop("ENTRY_SUBMISSION", None))
            self._submit(intent, now, "ENTRY_SUBMISSION", recheck=lambda i=intent: self._recheck_entry(i, now))
            out["submitted"].append(sym)

            def sent_row(sym=sym, xid=xid) -> None:                                         # the persisted intent: its state comes from the broker's answer only
                sent = self.store.intent(xid)
                out["evaluations"].append({"symbol": sym, "result": "ENTRY_INTENT", "execution_id": xid, "intent_state": sent.state if sent else None,
                                           "broker_status": sent.broker_status if sent else None,
                                           "reasons": [{"control": "ENTRY_SUBMISSION", "text": self._last_touch.get("ENTRY_SUBMISSION") or "entry intent recorded"}]})
            self._record(sent_row)
            pending_entries.append(intent)
            budget -= 1
        return out

    def _recheck_entry(self, it: Intent, now: datetime) -> List[str]:
        """The last look before the wire: mode, paper proof, safety, the proposal (re-read), the account, broker duplicates."""
        problems: List[str] = []
        if self.mode()["mode"] != "PAPER_AUTO":
            problems.append("mode is no longer PAPER_AUTO")
        if not self.environment_cache.get("paper"):
            problems.append("paper environment not confirmed")
        sv = self._safety_view(now)
        if sv is None or not (sv.allowed_new_entry and sv.state == "TRADING_ALLOWED"):
            problems.append(f"safety gate {getattr(sv, 'state', 'UNKNOWN')}")
        pv = self.proposals.current(it.symbol, now)
        if pv.proposal_id != it.proposal_id or pv.effective_status != "REVIEWABLE" or not (pv.execution_eligibility or {}).get("allowed"):
            problems.append(f"proposal changed ({pv.effective_status})")
        try:
            a = self.broker.account()
            if str(a.get("status") or "").upper() != "ACTIVE" or a.get("blocked"):
                problems.append("account not active")
            if self.broker.orders("open", [it.symbol]):
                problems.append("an open broker order exists in the symbol")
            if self.broker.position(it.symbol) is not None:
                problems.append("a broker position exists in the symbol")
        except BrokerError as exc:
            problems.append(f"broker unreadable ({exc.code})")
        return problems

    # ---------------------------------------------------------------- the single POST ----------------------------------------------
    def _submit(self, it: Intent, now: datetime, control_id: str, recheck: Optional[Callable[[], List[str]]]) -> None:
        problems = recheck() if recheck is not None else []
        if problems:
            self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="PRECHECK_FAILED", failure_text="; ".join(problems)[:500], resolved_at=_iso(now))
            self._touch(now, control_id, "BLOCK", f"{it.symbol}: not sent — " + "; ".join(problems), {"execution_id": it.execution_id})
            self._after_terminal(it, "NOT_SENT", now)
            return
        payload = RU.canonical_payload(it.client_order_id, it.symbol, it.side, it.qty)
        it = self.store.update_intent(it.execution_id, now, attempt_count=1, submitted_at=_iso(now))          # the attempt is recorded BEFORE the wire
        self._hook("after_attempt_recorded", intent=it)
        res = self.broker.post_order(payload, it.client_order_id)
        self._hook("after_post", intent=it, result=res)
        out = RU.classify_post(res, {"client_order_id": it.client_order_id, "symbol": it.symbol, "side": it.side, "qty": it.qty})
        klass, code = out["klass"], out.get("code")
        inp = {"execution_id": it.execution_id, "symbol": it.symbol, "side": it.side, "qty": it.qty, "outcome": klass, "code": code, "http_status": out.get("http_status")}
        if klass == "O1":
            self.store.update_intent(it.execution_id, now, outcome_class=klass)
            self._apply_order(it, out["order"], now, "post")
            self._touch(now, control_id, "RUN", f"{it.symbol}: {it.side} {it.qty} submitted ({out['order'].get('status')})", inp)
        elif klass == "O3":
            self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code=code, outcome_class=klass, resolved_at=_iso(now), failure_text="nothing left the machine")
            self._touch(now, control_id, "BLOCK", f"{it.symbol}: not sent ({code})", inp)
            self._after_terminal(it, "NOT_SENT", now)
        elif klass in ("O5", "O6"):
            self.store.update_intent(it.execution_id, now, state="REJECTED", failure_code=code, outcome_class=klass, resolved_at=_iso(now), failure_text=RU.CATEGORY_TEXT.get(code, "rejected by the broker"))
            self._touch(now, control_id, "BLOCK", f"{it.symbol}: rejected by the broker ({code})", inp)
            self._after_terminal(it, "REJECTED", now)
        elif klass == "O4":                                                                   # the broker already has this client_order_id: look it up now
            try:
                found = self.broker.order_by_client_id(it.client_order_id)
            except BrokerError:
                found = None
            if found is not None:
                self.store.update_intent(it.execution_id, now, outcome_class=klass, lookup_count=1, last_lookup_at=_iso(now))
                self._apply_order(it, found, now, "lookup")
                self._touch(now, control_id, "RUN", f"{it.symbol}: existing broker order adopted", inp)
            else:
                self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", outcome_class=klass, failure_code=code, lookup_count=1, last_lookup_at=_iso(now))
                self._touch(now, control_id, "UNKNOWN", f"{it.symbol}: duplicate id reported but order not found; reconciliation required", inp)
        else:                                                                                 # O2 / O7 / O8: ambiguous — never a second POST
            self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", outcome_class=klass, failure_code=code)
            self._touch(now, control_id, "UNKNOWN", f"{it.symbol}: ambiguous result ({code}); reconciliation required, no retry", inp)

    # ---------------------------------------------------------------- views -----------------------------------------------------------
    def _attention(self) -> List[str]:
        reasons: List[str] = []
        for i in self.store.open_intents():
            if i.state == "RECONCILIATION_REQUIRED":
                reasons.append(f"{i.symbol}: order needs reconciliation")
        for p in self.store.positions(open_only=True):
            if p.state == "ATTENTION":
                reasons.append(f"{p.symbol}: {(p.attention_reason or 'position needs attention').lower().replace('_', ' ')}")
        for r in self.recon_reasons:
            if r not in reasons:
                reasons.append(r)
        return reasons

    def card(self, symbol: str, now: Optional[datetime] = None, safety_state: Optional[str] = None) -> SymbolCard:
        now = now or self.clock()
        s = symbol.upper()
        pos = self.store.open_position(s)
        opens = self.store.open_intents(s)
        open_intent = opens[0] if opens else None
        all_intents = self.store.intents(s, limit=5)
        last_intent = all_intents[0] if all_intents else None
        closed = [p for p in self.store.positions(s, open_only=False, limit=5) if p.state == "CLOSED"]
        last_closed = closed[0] if closed else None
        try:
            setup = self.setups.current(s)
            setup_state = setup.state
        except Exception:  # noqa: BLE001
            setup_state = "NONE"
        if safety_state is None:
            sv = self._safety_view(now)
            safety_state = sv.state if sv is not None else "UNKNOWN"
        today = session_date(now).isoformat() if session_date(now) else None
        key = card_key(position=pos, open_intent=open_intent, last_intent=last_intent, last_closed=last_closed, safety_state=safety_state, setup_state=setup_state, proposal_status=None, today=today)
        icon, label = beginner(key)
        st = internal_state(position=pos, open_intent=open_intent, last_intent=last_intent, last_closed=last_closed)
        card = SymbolCard(symbol=s, key=key, icon=icon, label=label, state=st, setup_state=setup_state)
        if pos is not None:
            price = D(pos.last_price)
            p, pct = pnl(pos.average_entry_price, price, pos.quantity)
            card.position_id, card.execution_id, card.shares, card.entry, card.current, card.pnl, card.pnl_pct = pos.position_id, pos.execution_id, pos.quantity, pos.average_entry_price, pos.last_price, p, pct
            card.since = pos.opened_at
            card.reason = EXIT_TEXT.get(pos.exit_reason or "", pos.exit_reason) if pos.exit_reason else (pos.weakening_reason or None)
            if key == "ATTENTION":
                card.line = f"{pos.quantity} shares · {(pos.attention_reason or 'needs a look').lower().replace('_', ' ')}"
            elif key == "EXITING":
                card.line = f"{pos.quantity} shares · {card.reason or 'exit in progress'}"
            else:
                card.line = f"{pos.quantity} shares · entry {pos.average_entry_price}" + (f" · now {pos.last_price}" if pos.last_price else "") + (f" · {p}" if p else "")
            card.details = {"position_state": pos.state, "exit_cycle": pos.exit_cycle, "sessions_held": pos.sessions_held, "invalidation_price": pos.invalidation_price,
                            "open_intent": open_intent.model_dump() if open_intent else None, "events": self.store.position_events(pos.position_id, 20)}
        elif open_intent is not None:
            card.execution_id, card.shares, card.since = open_intent.execution_id, open_intent.qty, open_intent.created_at
            card.line = ("Waiting for the paper order to fill" if key == "ENTERING" else "Order status needs reconciliation") + f" · {open_intent.qty} shares"
            card.details = {"intent": open_intent.model_dump()}
        elif key == "CLOSED" and last_closed is not None:
            card.position_id, card.execution_id, card.shares, card.entry, card.current = last_closed.position_id, last_closed.execution_id, last_closed.entry_qty, last_closed.average_entry_price, last_closed.average_exit_price
            card.pnl, card.reason, card.since = last_closed.realized_pnl, EXIT_TEXT.get(last_closed.exit_reason or "", last_closed.exit_reason), last_closed.closed_at
            card.line = f"Trade completed · {last_closed.realized_pnl or '—'} · {card.reason or ''}".rstrip(" ·")
            card.details = {"position": last_closed.model_dump(), "events": self.store.position_events(last_closed.position_id, 20)}
        elif key == "ATTENTION" and last_intent is not None:
            card.execution_id = last_intent.execution_id
            card.line = f"Last paper order {last_intent.state.lower().replace('_', ' ')}" + (f" ({last_intent.failure_code.lower().replace('_', ' ')})" if last_intent.failure_code else "")
            card.details = {"intent": last_intent.model_dump()}
        elif key == "STOPPED":
            card.line = "Risk controls stopped new paper trades"
        elif key == "READY":
            card.line = "Setup ready; waiting for all entry checks"
        elif key == "WATCHING":
            card.line = "Setup on watch"
        else:
            card.line = "No active setup"
        return card

    def cards(self, now: Optional[datetime] = None) -> List[SymbolCard]:
        now = now or self.clock()
        sv = self._safety_view(now)
        state = sv.state if sv is not None else "UNKNOWN"
        syms: List[str] = []
        for p in self.store.positions(open_only=True):
            syms.append(p.symbol)
        for i in self.store.open_intents():
            syms.append(i.symbol)
        try:
            for e in self.setups.store.open_episodes():
                syms.append(e.symbol)
        except Exception:  # noqa: BLE001
            pass
        today = session_date(now).isoformat() if session_date(now) else ""
        for p in self.store.positions(open_only=False, limit=50):
            if p.state == "CLOSED" and (p.closed_at or "")[:10] == today:
                syms.append(p.symbol)
        for i in self.store.intents(limit=50):
            if i.state in ("FAILED", "NOT_SENT", "REJECTED") and (i.updated_at or "")[:10] == today:
                syms.append(i.symbol)
        seen, out = set(), []
        for s in syms:
            if s not in seen:
                seen.add(s)
                out.append(self.card(s, now, state))
        return out

    def history(self, limit: int = 20) -> List[dict]:
        rows: List[dict] = []
        for p in self.store.positions(open_only=False, limit=limit * 2):
            if p.state == "CLOSED":
                rows.append({"kind": "TRADE", "symbol": p.symbol, "at": p.closed_at, "opened_at": p.opened_at, "shares": p.entry_qty, "entry": p.average_entry_price, "exit": p.average_exit_price,
                             "pnl": p.realized_pnl, "reason": EXIT_TEXT.get(p.exit_reason or "", p.exit_reason), "reason_code": p.exit_reason, "position_id": p.position_id,
                             "sessions_held": p.sessions_held, "label": "Closed"})
        for i in self.store.intents(limit=limit * 2):
            if i.intent_type == "ENTRY" and i.state in ("REJECTED", "NOT_SENT", "FAILED", "CANCELED") and i.filled_qty == 0:
                rows.append({"kind": "NO_TRADE", "symbol": i.symbol, "at": i.resolved_at or i.updated_at, "shares": i.qty, "state": i.state, "code": i.failure_code,
                             "reason": (i.failure_text or i.failure_code or i.state).replace("_", " ").lower(), "execution_id": i.execution_id, "label": "Not filled"})
        rows.sort(key=lambda r: r.get("at") or "", reverse=True)
        return rows[:limit]

    def status(self, now: Optional[datetime] = None) -> BotStatus:
        now = now or self.clock()
        m = self.mode()
        sv = self._safety_view(now)
        attention = self._attention()
        open_pos = self.store.positions(open_only=True)
        opens = self.store.open_intents()
        pending_entries = [i for i in opens if i.intent_type == "ENTRY"]
        pending_exits = [i for i in opens if i.intent_type == "EXIT"]
        external = [p for p in self._broker_positions if p["symbol"] not in {x.symbol for x in open_pos} | {i.symbol for i in pending_entries}]
        ext_source, ext_at = ("BROKER_RECONCILIATION", self._broker_positions_at) if self._broker_positions_at else (None, None)
        try:                                                                       # display only: the account refresh's newer read of the same account (0 broker calls)
            refresher = getattr(self.stream_fn(), "account_refresh", None)
            snap = refresher.store.snapshot() if refresher is not None and refresher.enabled() else None
            if snap is not None and (ext_at is None or str(snap["refreshed_at"]) > ext_at):
                from execution.account_positions import classify
                external = [r for r in classify(snap["positions"], self.store) if not r["managed"]]
                ext_source, ext_at = "ACCOUNT_REFRESH", snap["refreshed_at"]
        except Exception:  # noqa: BLE001 - display only
            pass
        icon, label = (ATTENTION_ICON, ATTENTION_LABEL) if attention else (MODE_ICONS[m["mode"]], MODE_LABELS[m["mode"]])
        try:
            from execution.scheduler import status as sched_status
            sched = sched_status()
        except Exception:  # noqa: BLE001
            sched = {}
        last = self.last_cycle or {}
        acct: Dict[str, Any] = {}
        try:                                                                       # Stage 6.0.1: read-only account refresher freshness (display only)
            refresher = getattr(self.stream_fn(), "account_refresh", None)
            if refresher is not None:
                fr = refresher.freshness()
                acct = {k: fr.get(k) for k in ("status", "fresh", "label", "short_label", "reason", "age_seconds", "refreshed_at", "last_success_at", "last_attempt_at",
                                               "last_result", "last_error", "consecutive_failures", "attempt_count", "failure_count")}
        except Exception:  # noqa: BLE001 - display only
            acct = {}
        try:                                                                       # Stage 6.0.2: cash-flow data + the gate's adjusted daily P&L (display only)
            cash = getattr(self.stream_fn(), "cash_flow", None)
            if cash is not None and acct:
                cf = cash.summary()
                dl = sv.daily_loss if sv is not None else None
                if dl is not None:
                    cf.update(daily_pnl=dl.daily_pnl, daily_loss_pct=dl.daily_loss_pct, threshold_pct=dl.threshold_pct, adjustments=dl.adjustments,
                              gate_net_external_cash_flow=dl.net_external_cash_flow, daily_loss_status=dl.status)
                acct["cash_flow"] = cf
        except Exception:  # noqa: BLE001 - display only
            acct.pop("cash_flow", None)
        return BotStatus(mode=m["mode"], mode_label=MODE_LABELS[m["mode"]], icon=icon, headline=label, attention=bool(attention), attention_reasons=attention, ready=self.ready,
                         environment=dict(self.environment_cache), safety_state=sv.state if sv is not None else None, safety_label=sv.state_label if sv is not None else None,
                         open_positions=len(open_pos), pending_entries=len(pending_entries), pending_exits=len(pending_exits), external_positions=len(external),
                         external_positions_source=ext_source, external_positions_as_of=ext_at, external_symbols=sorted(str(p["symbol"]) for p in external),
                         max_open_positions=self.max_open_positions, reconciliation_healthy=self.recon_healthy, last_cycle_at=last.get("at"), last_cycle_result=last.get("result"),
                         scheduler=sched, cards=self.cards(now), recent=self.history(8), mode_changed_at=m.get("changed_at"), mode_changed_by=m.get("changed_by"),
                         account_data=acct)

    def overview(self, now: Optional[datetime] = None) -> dict:
        now = now or self.clock()
        st = self.status(now)
        return {"version": VERSION, "stage": STAGE, **st.model_dump(), "mode_events": self.store.mode_events(10), "controls": self.store.controls(), "findings": self.store.findings(30),
                "runs": self.store.runs(10), "intents": [i.model_dump() for i in self.store.intents(limit=30)], "positions": [p.model_dump() for p in self.store.positions(open_only=False, limit=30)],
                "counts": self.store.counts(), "limits": {"max_open_positions": self.max_open_positions, "max_shares_per_entry": self.qty_cap, "max_hold_sessions": self.max_hold_sessions,
                                                           "near_close_minutes": self.near_close_minutes, "max_exit_cycles": self.max_exit_cycles, "demand_exit": self.demand_exit,
                                                           "demand_exit_evals": self.demand_exit_evals, "stretch_exit": self.stretch_exit}, "disclaimer": DISCLAIMER}
