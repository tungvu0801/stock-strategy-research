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
import math
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from execution import (ATTENTION_ICON, ATTENTION_LABEL, COID_PREFIX, DISCLAIMER, EXIT_TEXT, INTENT_OPEN, MODE_ICONS, MODE_LABELS, STAGE, VERSION)
from execution import EXIT_REASONS                          # HDR-2 C1c2: the known exit reasons (C1C1-A12)
from execution.broker import AlpacaPaperBroker, BrokerError
from execution import exposure_gate                        # Stage 6.9.1: the UNIFIED_EXTERNAL_EXPOSURE entry control (pure; off by default)
from execution import data_gate as DG                       # HDR-2 r14 3.1: the pure gate (EXEC-C1c1: exits only)
from execution.engine import (D, EntryContext, ExitContext, beginner, blocking, card_key, entry_controls, entry_quantity, entry_coid, execution_id_for, exit_coid, exit_decision,
                              exit_id_for, internal_state, money, pnl, position_id_for, session_policy)
from execution.models import BotStatus, ControlEvaluation, Finding, Intent, Position, SymbolCard
from execution.store import ExecutionStore, ExecutionStoreError
from paper import alpaca_order_rules as RU
from proposals import MINUTE_BAR_MAX_AGE_SECONDS, PRICE_MAX_AGE_SECONDS   # Wave 1: the stop check uses the entry side's own price freshness limits
from proposals.engine import SIMULATED_MODES
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


_LATCH_EVIDENCE = {"STOP_CHECK_BLIND": ("STOP_VERIFIED_NOT_HIT", "STOP_VERIFIED_HIT", "POSITION_CLOSED"),      # HDR-2 r14 3.13.8: what releases a latched key,
                   "PROTECTIVE_EXIT_UNVERIFIED": ("POSITION_CLOSED", "PROTECTIVE_EXIT_ACCEPTED", "PROTECTIVE_FACTS_VERIFIED")}   # per family (never another family's)
ENTRY_RESERVE_READS = 7                                     # HDR-2 r14 3.8.1: entry recheck (3) + DG-final (4), checked before the intent insert
PROBE_RESERVE_READS = 4                                     # HDR-2 r14 3.8.1: a verification / startup probe (P1, P3/P4, P6, P8b)


STOP_PRICE_FUTURE_TOLERANCE_S = 60.0                       # Wave 1: a price stamped further than this after the cycle's clock is not trusted
STOP_PRICE_TEXT = {"PRICE_MISSING": "no price", "PRICE_INVALID": "the price is not a positive finite number", "PRICE_UNDATED": "the price has no time",
                   "PRICE_STALE": "the price is too old", "PRICE_FROM_FUTURE": "the price is stamped in the future"}


def stop_price_problem(price, source: str, price_at: Optional[str], now: datetime, simulated: bool) -> Optional[str]:
    """Wave 1 (audit EX-14): why this price cannot show that a position's stop was NOT hit; None when it can. PRICE_MISSING / PRICE_INVALID:
    no usable value (never a breach either). PRICE_UNDATED / PRICE_STALE / PRICE_FROM_FUTURE: a real value whose time is unknown, older than the
    entry side's freshness limit (proposals.PRICE_MAX_AGE_SECONDS; MINUTE_BAR_MAX_AGE_SECONDS for a minute close) or in the future; it may still
    show a breach. A simulated feed is not aged, as on the entry side (proposals/engine.py)."""
    if price is None:
        return "PRICE_MISSING"
    try:
        value = float(price)
    except (TypeError, ValueError, ArithmeticError):
        return "PRICE_INVALID"
    if not math.isfinite(value) or value <= 0:
        return "PRICE_INVALID"
    if simulated:
        return None
    at = _parse(price_at)
    if at is None:
        return "PRICE_UNDATED"
    age = (now - at).total_seconds()
    if age < -STOP_PRICE_FUTURE_TOLERANCE_S:
        return "PRICE_FROM_FUTURE"
    return "PRICE_STALE" if age > (MINUTE_BAR_MAX_AGE_SECONDS if source == "MINUTE_CLOSE" else PRICE_MAX_AGE_SECONDS) else None


def recompute_pnl(entry_qty: int, entry_avg, exit_qty: int, exits: List[Tuple[int, Any]]) -> Dict[str, Any]:
    """Wave 2 PR A (audit PF-09; design 3.1.2 / 3.1.3): the exit P/L of one position from the broker's CUMULATIVE view (Q_o, A_o) of every exit
    order, N = sum(A_o * Q_o) - never an increment priced at a cumulative average. Pure and idempotent: the same views give the same row whatever
    the observation sequence, so a price-only observation heals it. Valid only with a positive entry price, a price on every filled order and
    sum(Q_o) = exit_qty; otherwise the P/L is NULL (never a guess). Returns the P/L columns and the complete post-write snapshot (Rule E1)."""
    e = D(entry_avg)
    filled = [(int(q), D(a)) for q, a in exits if int(q or 0) > 0]
    valid = e is not None and e > 0 and all(a is not None and a > 0 for _, a in filled) and sum(q for q, _ in filled) == exit_qty
    n = sum((a * q for q, a in filled), Decimal(0)) if valid else None
    avg = money(n / exit_qty) if valid and exit_qty > 0 else None
    realized = money(n - e * exit_qty) if valid and exit_qty > 0 else None
    return {"average_exit_price": avg, "realized_pnl": realized,
            "pnl_snapshot": {"entry_qty": entry_qty, "average_entry_price": entry_avg, "exit_qty": exit_qty, "average_exit_price": avg, "realized_pnl": realized,
                             "valid": valid, "basis": "W2.recompute.v1"}}


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
        self.stop_check_problems: Dict[str, str] = {}                         # Wave 1: symbol -> why its stop check could not run in this cycle
        self.protective_exit_problems: Dict[str, str] = {}                    # HDR-2 r14 3.5.3 item 3: symbol -> its refused protective exit (this cycle; real broker)
        self._unsaved_blocking_alerts: Dict[tuple, dict] = {}                 # HDR-2 r14 3.13.8: the latch, (family, position_id) -> the unsaved alert (in memory only)
        self._verified_positions: set = set()                                 # HDR-2 r14 3.13.7 OD-R6-2(a): V, protective-verified in THIS process
        self._attention_baseline: Optional[Any] = None                        # HDR-2 r14 3.13.7: S_R, this cycle's end-of-RECONCILE states (one cycle only)
        self._cycle_id: Optional[str] = None                                  # the identity of the run_cycle in progress (None outside it)
        self._cycle_reads0: Optional[Any] = None                              # HDR-2 r14 3.8.1: reads0 after the reset that precedes the entry step (C1C1-A11)
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
            elif self.needs_broker() or not self.recon_healthy:                # bot-position recovery (design 3.5, section 6): also with nothing open
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
            if not self.needs_broker() and self.recon_healthy:                 # bot-position recovery (design 3.5, section 6): an unhealthy executor reconciles
                self.last_cycle = {"at": _iso(now), "finished_at": _iso(now), "result": "IDLE", "trigger": trigger, "mode": mode}
                return self.last_cycle                                            # nothing to do: no broker call, no write
            summary: Dict[str, Any] = {"trigger": trigger, "mode": mode}
            error, result = None, "RUN"
            started = time.monotonic()
            self.progress = {"run_id": run_id, "trigger": trigger, "mode": mode, "started_at": _iso(now), "step": None, "step_label": None, "symbol": None, "index": None, "total": None}
            self.broker.reset_budget()
            reads0 = getattr(self.broker, "reads", None)                                # HDR-2 r14 3.8.1: the read-budget snapshot right after the reset
            self._cycle_id, self._cycle_reads0, self._attention_baseline = run_id, reads0, None   # r14 3.13.7: S_R invalidated at the start of each cycle
            try:
                self._step("ORDERS")
                summary["monitored"] = self._monitor_intents(now, trigger)
                self._step("RECONCILE")
                summary["findings"] = self._reconcile(now, trigger, run_id)
                self._step("POSITIONS")
                summary.update(self._evaluate_positions(now, trigger, reads0=reads0, run_id=run_id))
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
            self._cycle_id = None                                                       # a scan outside run_cycle never uses this cycle's S_R (r14 3.13.7)
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
        if order.get("filled_avg_price") is None and fields["filled_qty"] == it.filled_qty:
            fields["filled_avg_price"] = it.filled_avg_price                                # Wave 2 PR A (F-12): a price-less repeat keeps the stored price (accounting / display only)
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
        price_changed = fields["filled_qty"] > 0 and D(order.get("filled_avg_price")) is not None and D(order.get("filled_avg_price")) != D(it.filled_avg_price)   # Wave 2 PR A (R-01): a price-only observation re-runs the accounting
        if new_fill or pos_missing or price_changed:
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
                ev = {"kind": "OPENED", "from_state": None, "to_state": "OPEN", "reason": f"{filled} of {it.qty} filled at {avg}",
                      "detail": {"broker_order_id": order["id"], "status": order.get("status"),
                                 "pnl_snapshot": recompute_pnl(filled, new.average_entry_price, 0, [])["pnl_snapshot"]}}   # Wave 2 PR A (G-03): every fill event carries the snapshot
                self.store.apply_fill(order, fields, it.execution_id, now, new_position=new, position_event=ev)
            else:
                delta = filled - it.filled_qty
                upd = {"position_id": pos.position_id, "quantity": pos.quantity + max(0, delta), "entry_qty": max(pos.entry_qty, filled), "average_entry_price": avg or pos.average_entry_price}
                try:                                                                       # Wave 2 PR A (N-1): a changed entry price also re-prices the exits already filled
                    views = [(x.filled_qty, x.filled_avg_price) for x in self.store.exit_intents_for_position(pos.position_id)] if pos.exit_qty > 0 else []
                    r = recompute_pnl(upd["entry_qty"], upd["average_entry_price"], pos.exit_qty, views)
                except Exception as exc:  # noqa: BLE001 - accounting never blocks the quantity: P/L NULL, the entry price keeps its prior value (G-12c; NOT NULL)
                    log.error("execution: entry accounting of %s failed (%s)", pos.position_id, type(exc).__name__)
                    upd["average_entry_price"] = pos.average_entry_price
                    r = {"average_exit_price": None, "realized_pnl": None, "pnl_snapshot": {"entry_qty": upd["entry_qty"], "average_entry_price": pos.average_entry_price,
                         "exit_qty": pos.exit_qty, "average_exit_price": None, "realized_pnl": None, "valid": False, "basis": "W2.recompute.v1", "error": type(exc).__name__}}
                if pos.exit_qty > 0:
                    upd.update(average_exit_price=r["average_exit_price"], realized_pnl=r["realized_pnl"])
                else:
                    r["pnl_snapshot"].update(average_exit_price=pos.average_exit_price, realized_pnl=pos.realized_pnl)
                ev = {"position_id": pos.position_id, "kind": "FILL_UPDATED", "from_state": pos.state, "to_state": pos.state, "reason": f"{filled} of {it.qty} filled at {avg}",
                      "detail": {"broker_order_id": order["id"], "status": order.get("status"), "pnl_snapshot": r["pnl_snapshot"]}}
                self.store.apply_fill(order, fields, it.execution_id, now, position_update=upd, position_event=ev)
            return
        pos = self.store.position(it.position_id) if it.position_id else None
        if pos is None:
            self.store.record_order(order, it.execution_id, now, "fill", intent_fields=fields)
            return
        delta = max(0, filled - it.filled_qty)
        exit_qty = pos.exit_qty + delta
        remaining = max(0, pos.quantity - delta)
        closed = remaining <= 0
        state = "CLOSED" if closed else pos.state
        try:                                                                               # Wave 2 PR A (PF-09): N = sum(A_o * Q_o) over every exit order of the position
            xs = self.store.exit_intents_for_position(pos.position_id)
            views = [(filled, fields.get("filled_avg_price")) if x.execution_id == it.execution_id else (x.filled_qty, x.filled_avg_price) for x in xs]
            if not any(x.execution_id == it.execution_id for x in xs):
                views.append((filled, fields.get("filled_avg_price")))                   # the order being applied is always one of the views
            r = recompute_pnl(pos.entry_qty, pos.average_entry_price, exit_qty, views)
        except Exception as exc:  # noqa: BLE001 - accounting never blocks the quantity: it is written with NULL P/L and an invalid snapshot
            log.error("execution: exit accounting of %s failed (%s)", pos.position_id, type(exc).__name__)
            r = {"average_exit_price": None, "realized_pnl": None, "pnl_snapshot": {"entry_qty": pos.entry_qty, "average_entry_price": pos.average_entry_price, "exit_qty": exit_qty,
                 "average_exit_price": None, "realized_pnl": None, "valid": False, "basis": "W2.recompute.v1", "error": type(exc).__name__}}
        upd = {"position_id": pos.position_id, "quantity": remaining, "exit_qty": exit_qty, "average_exit_price": r["average_exit_price"], "state": state,
               "realized_pnl": r["realized_pnl"], "closed_at": pos.closed_at if pos.state == "CLOSED" else (_iso(now) if closed else None)}
        ev = {"position_id": pos.position_id, "kind": "CLOSED" if closed and pos.state != "CLOSED" else "EXIT_FILL", "from_state": pos.state, "to_state": state, "reason": pos.exit_reason,
              "detail": {"broker_order_id": order["id"], "status": order.get("status"), "exit_qty": exit_qty, "average_exit_price": r["average_exit_price"],
                         "realized_pnl": r["realized_pnl"], "pnl_snapshot": r["pnl_snapshot"]}}
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

    # HDR-2 r14 3.13.8: the ONLY writer of the safety lifecycle kinds (never _finding). It never raises: the state read, the decision (3.13.4)
    # and the store write are one try; on any Exception it logs and returns False, as _finding does, re-raising only the test-only SimulatedCrash.
    # outcome: FAILURE | EVIDENCE | NEITHER. True: the durable state stands as the outcome requires (a row written; or no row needed: NEITHER,
    # EVIDENCE while NONE or RESOLVED, FAILURE while ACTIVE or RECURRENT, including a lost compare-and-set that found one of those). False: any
    # exception, or any other lost compare-and-set (the next cycle re-decides).
    def _safety_transition(self, now: datetime, trigger: str, run_id: Optional[str], family: str, position_id: str, symbol: str, outcome: str, *,
                           reason_codes: Optional[List[str]] = None, exit_cycle: Optional[int] = None, evaluation: Optional[str] = None,
                           evidence: Optional[str] = None, execution_id: Optional[str] = None, derived: bool = False, deferred: bool = False) -> bool:
        # EXEC-C1c2 (r14 3.13.8): the unsaved-blocking-alert latch lives here. A blocking-family FAILURE whose durable state does not stand (any
        # exception, or a lost compare-and-set that found no ACTIVE / RECURRENT) is latched by a plain dict assignment (setdefault keeps the onset);
        # a durable ACTIVE / RECURRENT row releases it; the family's own evidence releases it first and then makes ONE two-row attempt (the deferred
        # alert row and its resolution), the release standing if that attempt fails. `deferred`: the latch's re-attempt (detail "deferred": true).
        try:
            latch = getattr(self, "_unsaved_blocking_alerts", {})
            key = (family, position_id)
            onset = {"symbol": symbol, "reason_codes": [c for c in (reason_codes or []) if isinstance(c, str) and c], "exit_cycle": exit_cycle}
            latchable = outcome == "FAILURE" and family in DG.SAFETY_KINDS
            try:
                if outcome not in ("FAILURE", "EVIDENCE", "NEITHER"):
                    raise ValueError(f"not a lifecycle outcome: {outcome!r}")
                if outcome == "NEITHER":
                    return True
                held = latch.pop(key, None) if outcome == "EVIDENCE" and evidence in _LATCH_EVIDENCE.get(family, ()) else None
                state = self.store.safety_states([position_id])[(family, position_id)]["state"]
                if outcome == "FAILURE":
                    if state in ("ACTIVE", "RECURRENT"):
                        latch.pop(key, None)
                        return True
                    alert: Dict[str, Any] = {"reason_codes": list(reason_codes or [])}
                    if exit_cycle is not None:
                        alert["exit_cycle"] = exit_cycle
                    if evaluation is not None:
                        alert["evaluation"] = evaluation
                    if deferred:
                        alert["deferred"] = True
                    if self.store.add_safety_transition(family, position_id, symbol, state, now, trigger, run_id, alert=alert):
                        latch.pop(key, None)
                        return True
                    if self.store.safety_states([position_id])[(family, position_id)]["state"] in ("ACTIVE", "RECURRENT"):
                        latch.pop(key, None)
                        return True
                    if latchable:
                        latch.setdefault(key, onset)
                    return False
                res: Dict[str, Any] = {"evidence": evidence}
                if execution_id is not None:
                    res["execution_id"] = execution_id
                if derived:
                    res["derived"] = True
                if state in ("NONE", "RESOLVED"):
                    if held is None:
                        return True
                    episode: Dict[str, Any] = {"reason_codes": list(held.get("reason_codes") or []), "deferred": True}
                    if family == "PROTECTIVE_EXIT_UNVERIFIED" and type(held.get("exit_cycle")) is int:
                        episode["exit_cycle"] = held["exit_cycle"]
                    return self.store.add_safety_transition(family, position_id, symbol, state, now, trigger, run_id, alert=episode, resolution=res)
                return self.store.add_safety_transition(family, position_id, symbol, state, now, trigger, run_id, resolution=res)
            except Exception:
                if latchable:
                    latch.setdefault(key, onset)
                raise
        except Exception as exc:  # noqa: BLE001 - r14 3.13.8: never raises (SimulatedCrash excepted)
            if isinstance(exc, SimulatedCrash):
                raise
            log.warning("safety finding not saved (%s)", type(exc).__name__)
            return False

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
                try:                                                               # Wave 2 PR C (C-64, W2-6): the finding's attribution - detail only; the entry block is unchanged
                    coids = sorted(str(o.get("client_order_id") or "") for o in open_orders if str(o.get("symbol") or "").upper() == sym)
                    detail = {"count": cnt, "client_order_ids": coids, "bot_orders": sum(1 for c in coids if c.startswith(COID_PREFIX)),
                              "bot_exposed": any(p.symbol == sym for p in internal_open) or any(i.symbol == sym for i in open_intents)}
                except Exception:  # noqa: BLE001 - contained: a legacy row without the keys, which is counted (fail-closed)
                    detail = {"count": cnt}
                self._finding(now, trigger, Finding(kind="MULTIPLE_ORDERS", symbol=sym, detail=detail), run_id)
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

    def _feed_simulated(self) -> bool:
        try:
            return str(getattr(getattr(self.stream_fn(), "market", None), "mode", "") or "") in SIMULATED_MODES
        except Exception:  # noqa: BLE001 - unknown is not simulated: the age rule applies
            return False

    def _market_open_for_stop_check(self) -> bool:
        """Wave 1: only a readable broker clock that says CLOSED stands the stop-check escalation down; an unreadable clock counts as open."""
        try:
            self._clock_cache = self.broker.clock()
        except Exception:  # noqa: BLE001 - BrokerError, BrokerRefused (the read budget) or anything else: unknown is treated as open
            self._clock_cache = None
            return True
        return bool(self._clock_cache.get("is_open"))

    def _evaluate_positions(self, now: datetime, trigger: str, reads0: Optional[int] = None, run_id: Optional[str] = None) -> dict:
        self.protective_exit_problems = {}                                                  # HDR-2 r14 3.5.3 item 3: re-derived every cycle, like stop_check_problems
        if not self._simulated_broker():                                                   # HDR-2 r14 3.8.1: a real (or unknown) broker takes the gated two-pass step
            return self._evaluate_positions_gated(now, trigger, reads0, run_id)
        out = {"positions": 0, "exits_decided": 0, "exits_submitted": 0, "exits_deferred": 0, "decisions": []}
        self.stop_check_problems = {}
        stop_market_open: Optional[bool] = None                                             # Wave 1: the broker clock, read at most once per cycle
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
            problem = stop_price_problem(ctx.price, ctx.price_source, ctx.price_at, now, self._feed_simulated())   # Wave 1 (audit EX-14)
            if problem in ("PRICE_MISSING", "PRICE_INVALID"):
                ctx = replace(ctx, price=None)                                               # Wave 1: no usable value is never a breach (a stale one may be)
            dec = exit_decision(pos, ctx)
            fields: Dict[str, Any] = {"last_price": money(ctx.price), "last_price_at": ctx.price_at, "demand_low_streak": dec.demand_low_streak,
                                      "last_demand_eval_at": dec.last_demand_eval_at, "sessions_held": dec.sessions_held}
            if ctx.price is None:                                                            # Wave 1: the last known price stays
                del fields["last_price"], fields["last_price_at"]
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
            elif problem is not None:                                                        # Wave 1: the stop could not be confirmed as NOT hit
                what = f"{STOP_PRICE_TEXT[problem]} ({ctx.price_source or 'no source'}{', ' + ctx.price_at if ctx.price_at else ''})"
                detail = {**dec.detail, "problem": problem, "price_at": ctx.price_at}
                self.store.update_position(pos.position_id, now, None, **fields)            # no state change: neither HEALTHY nor an exit
                if stop_market_open is None:
                    stop_market_open = self._market_open_for_stop_check()
                if stop_market_open:
                    reason = f"{pos.symbol}: stop check could not run - {what}; new entries are blocked until a fresh, valid price arrives"
                    self.stop_check_problems[pos.symbol] = reason
                    self._touch(now, "EXIT_TRIGGER", "UNKNOWN", reason, detail)
                    decide(pos, "ATTENTION", f"The stop check could not run: {what}. Holding; no new entry until a fresh, valid price arrives.")
                else:
                    self._touch(now, "EXIT_TRIGGER", "WARN", f"{pos.symbol}: stop check waits for a fresh price - {what}; market closed", detail)
                    decide(pos, "HOLD", f"Market closed: the stop check waits for a fresh price ({what}).")
            else:
                new_state = "WEAKENING" if dec.weakening else "HEALTHY"
                ev = None
                if pos.state in ("OPEN", "HEALTHY", "WEAKENING") and new_state != pos.state:
                    fields.update(state=new_state, weakening_reason=dec.weakening)
                    ev = {"kind": "STATE", "from_state": pos.state, "to_state": new_state, "reason": dec.weakening or "healthy", "detail": dec.detail}
                self.store.update_position(pos.position_id, now, ev, **fields)
                self._touch(now, "EXIT_TRIGGER", "PASS", f"{pos.symbol}: no exit condition", dec.detail)
                decide(pos, "HOLD", lambda: "No exit condition is met; holding." + (f" Weakening: {dec.weakening}." if dec.weakening else ""))
        if self.stop_check_problems:                                                         # Wave 1: a later PASS never hides a blind stop check
            self._touch(now, "EXIT_TRIGGER", "UNKNOWN", "; ".join(self.stop_check_problems[s] for s in sorted(self.stop_check_problems)),
                        {"symbols": sorted(self.stop_check_problems)})
        return out

    def _safety_sweeps(self, now: datetime, trigger: str, run_id: Optional[str]) -> None:
        """r14 3.13.4 / 3.13.5 at the start of pass 1, on store reads only (no broker read): (1) every ACTIVE / RECURRENT key whose position is
        CLOSED gets POSITION_CLOSED; (2) then every protective or strategy key still ACTIVE / RECURRENT is checked against the derived evidence
        (2') / (1'): an EXIT intent of the position with outcome_class O1, the family's exit_reason and an exit_cycle STRICTLY greater than the
        latest alert row's recorded exit_cycle (none recorded or an unreadable detail: never). Runs for exit-in-flight positions too. A failed read
        writes nothing this cycle (no transition)."""
        try:
            states = self.store.safety_states(None)
        except Exception as exc:  # noqa: BLE001 - r14 3.13.3: unreadable -> no transition this cycle
            log.warning("execution: safety state unreadable (%s)", type(exc).__name__)
            return
        live = sorted((s for s in states.values() if s.get("state") in DG.UNRESOLVED_STATES), key=lambda s: s.get("rowid") or 0)
        still = []
        for s in live:
            try:
                p = self.store.position(s["position_id"])
            except Exception:  # noqa: BLE001
                continue
            if p is None:
                continue
            if p.state == "CLOSED":
                self._safety_transition(now, trigger, run_id, s["family"], p.position_id, p.symbol, "EVIDENCE", evidence="POSITION_CLOSED")
            else:
                still.append((s, p))
        strategy = set(DG.PHASE1_UNVERIFIABLE_REASONS) | {"TIME_EXIT"}
        for s, p in still:
            fam = s["family"]
            if fam == "STOP_CHECK_BLIND" or type(s.get("exit_cycle")) is not int:
                continue
            try:
                xs = self.store.exit_intents_for_position(p.position_id)
            except Exception:  # noqa: BLE001
                continue
            ok = (lambda r: r == "HARD_INVALIDATION") if fam == "PROTECTIVE_EXIT_UNVERIFIED" else (lambda r: r in strategy)
            hit = next((x for x in xs if x.outcome_class == "O1" and ok(x.exit_reason) and type(x.exit_cycle) is int and x.exit_cycle > s["exit_cycle"]), None)
            if hit is not None:
                self._safety_transition(now, trigger, run_id, fam, p.position_id, p.symbol, "EVIDENCE",
                                        evidence="PROTECTIVE_EXIT_ACCEPTED" if fam == "PROTECTIVE_EXIT_UNVERIFIED" else "STRATEGY_EXIT_ACCEPTED",
                                        execution_id=hit.execution_id, derived=True)

    def _exit_context_gated(self, pos: Position, now: datetime, gp: Any) -> ExitContext:
        """_exit_context with the gate's price (r14 2.4.1: the same selection feeds SC and the exit context, so the stored last_price and SC
        agree); _price is not read on this path. The setup reads are today's."""
        gts, gst = DG.parse_ts(getattr(gp, "time", None))
        gval = D(gp.value) if gp is not None and gp.source != "UNAVAILABLE" and gp.value is not None else None
        setup = self.setups.current(pos.symbol)
        rows = self.setups.store.episodes(pos.symbol, 20)
        ep_row = next((r for r in rows if r.get("setup_id") == pos.setup_id), None)
        ep_rec = self.setups.store.open_episode(pos.symbol)
        if ep_rec is not None and ep_rec.setup_id != pos.setup_id:
            ep_rec = None
        return ExitContext(now=now, price=gval, price_source=getattr(gp, "source", "UNAVAILABLE"), price_at=_iso(gts) if gst == DG.TS_OK and gval is not None else None,
                           setup=setup, episode=ep_row, episode_record=ep_rec, inputs=setup.inputs, max_hold_sessions=self.max_hold_sessions, demand_exit=self.demand_exit,
                           demand_exit_evals=self.demand_exit_evals, stretch_exit=self.stretch_exit)

    def _evaluate_positions_gated(self, now: datetime, trigger: str, reads0: Optional[int], run_id: Optional[str]) -> dict:
        """HDR-2 r14 3.8.1 on a real (or unknown) broker. Pass 1: the CLOSED and derived sweeps (store only), ONE broker clock read (3.3.4; once
        more for the remaining positions when that read is older than 60 s on the evaluation clock), then SC for every open position except
        ATTENTION and exit-in-flight (today's handling), computed before and apart from the exit decision, with the 3.3.3 effects and the
        STOP_CHECK_BLIND transitions. Pass 2: HIT positions (the protective path) first, then strategy candidates, each group in store order.
        (The OD-R4-2(a) / OD-R6-2(a) probes between the two groups are EXEC-C1c2.)"""
        out = {"positions": 0, "exits_decided": 0, "exits_submitted": 0, "exits_deferred": 0, "decisions": []}
        self.stop_check_problems = {}
        decide = lambda pos, decision, text: self._record(lambda: out["decisions"].append({"symbol": pos.symbol, "position_id": pos.position_id, "decision": decision,  # noqa: E731
                                                                                         "text": text() if callable(text) else text}))
        self._capture_attention_baseline(run_id)                                            # r14 3.13.7: S_R, before any position is evaluated (its own call site)
        self._safety_sweeps(now, trigger, run_id)
        self._latch_retry(now, trigger, run_id)                                             # r14 3.13.8: after the CLOSED and derived sweeps, before entries
        probes: List[Any] = []                                                              # r14 3.8.1 pass 2: the verified NOT_HIT positions (probe candidates)
        try:
            states = self.store.safety_states(None)
        except Exception:  # noqa: BLE001 - unreadable: no strategy-family evidence this cycle
            states = None
        open_intents = self.store.open_intents()
        clock, read_at, reread = None, None, False
        hits: List[tuple] = []
        held: List[tuple] = []
        for pos in self.store.positions(open_only=True):
            out["positions"] += 1
            if any(i.position_id == pos.position_id and i.intent_type == "EXIT" for i in open_intents):
                decide(pos, "EXIT_IN_PROGRESS", "An exit order is already at the broker; the order monitor follows it.")
                continue                                                                    # an exit is in flight: the monitor owns it
            if pos.state == "ATTENTION":
                self._touch(now, "EXIT_TRIGGER", "UNKNOWN", f"{pos.symbol}: position needs attention ({pos.attention_reason}); no automatic action", {"position_id": pos.position_id})
                decide(pos, "ATTENTION", lambda: f"Needs attention ({(pos.attention_reason or 'manual review').replace('_', ' ').lower()}); no automatic action.")
                continue
            gp = None
            try:                                                                            # Z1: SC of this position (r14 3.1, 3.3)
                eval_clock = self.clock()
                if read_at is None or (not reread and (eval_clock - read_at).total_seconds() > DG.SKEW_MAX_S):
                    reread = read_at is not None
                    try:
                        clock = self.broker.clock()
                    except Exception:  # noqa: BLE001 - unreadable: BLIND CLOCK_UNREADABLE, the market treated as open (Wave 1)
                        clock = DG.UNREADABLE
                    read_at = eval_clock
                age = max(0.0, (eval_clock - read_at).total_seconds())
                gp = self._gate_price(pos.symbol, eval_clock, clock.get("timestamp") if isinstance(clock, dict) else None)
                sc = DG.stop_status(DG.StopCheckFacts(eval_clock, self._market_mode(), gp, pos.invalidation_price, clock, age))
            except SimulatedCrash:
                raise
            except Exception:  # noqa: BLE001 - BLIND GATE_ERROR; the loop continues with the next position
                sc, gp = DG.StopStatus(DG.BLIND, "GATE_ERROR", ("GATE_ERROR",), {}), None
            market_open = self._sc_market_open(clock, read_at)                              # C1C1-A17 (revised): only a VERIFIED clock saying closed stands down
            failed: Dict[str, Any] = {}                                                     # c1d-fix2 M-1: this position's own pass-1 failure, contained
            ctx = self._c1d_contain(failed, lambda: self._exit_context_gated(pos, now, gp))
            dec = self._c1d_contain(failed, lambda: exit_decision(pos, ctx))
            if failed:                                                                       # no exit decision is used; SC's effects below still apply
                ctx, dec = self._c1d_no_decision()
            cands = [c for c in dec.candidates if c != "HARD_INVALIDATION"]                # r14 3.4: the engine's HARD_INVALIDATION is never used on its own
            fields: Dict[str, Any] = {"last_price": money(ctx.price), "last_price_at": ctx.price_at, "demand_low_streak": dec.demand_low_streak,
                                      "last_demand_eval_at": dec.last_demand_eval_at, "sessions_held": dec.sessions_held}
            if ctx.price is None:
                del fields["last_price"], fields["last_price_at"]
            if sc.status == DG.BLIND:                                                       # r14 3.3.3, whatever else happens to the position this cycle
                detail = {**dec.detail, "problem": sc.reason, "codes": list(sc.codes), "price_at": ctx.price_at}
                if market_open:
                    reason = f"{pos.symbol}: stop check could not run - {', '.join(sc.codes)}; new entries are blocked until a fresh, valid price arrives"
                    self.stop_check_problems[pos.symbol] = reason
                    self._touch(now, "EXIT_TRIGGER", "UNKNOWN", reason, detail)
                    self._safety_transition(now, trigger, run_id, "STOP_CHECK_BLIND", pos.position_id, pos.symbol, "FAILURE", reason_codes=list(sc.codes))
                else:
                    self._touch(now, "EXIT_TRIGGER", "WARN", f"{pos.symbol}: stop check waits for a fresh price - {', '.join(sc.codes)}; market closed", detail)
            else:                                                                           # a verified NOT_HIT or HIT on the full allow-list
                self._safety_transition(now, trigger, run_id, "STOP_CHECK_BLIND", pos.position_id, pos.symbol, "EVIDENCE",
                                        evidence="STOP_VERIFIED_HIT" if sc.status == DG.HIT else "STOP_VERIFIED_NOT_HIT")
                if sc.status == DG.NOT_HIT:
                    probes.append(pos)
            entry = self._c1d_contain(failed, lambda: self._c1d_decide(pos, now, trigger, run_id, states, sc, dec, cands, fields, market_open, decide))
            if failed:                                                                       # M-1: fail closed for this position; the loop and pass 2 go on
                entry = self._c1d_pass1_failed(pos, now, sc, failed["exc"], decide)
            if entry is not None:
                out["exits_decided"] += 1
                (hits if sc.status == DG.HIT else held).append(entry)
        for k, (pos, sc, dec, cands) in enumerate(hits + held):                             # pass 2: HIT positions, then the probes, then strategy candidates
            if k == len(hits):
                self._run_probes(now, probes, states, reads0, trigger, run_id)
            protective = sc.status == DG.HIT
            plan = {"order_class": DG.PROTECTIVE_EXIT if protective else DG.STRATEGY_EXIT, "stop": sc, "candidates": ["HARD_INVALIDATION"] if protective else cands,
                    "trigger": trigger, "run_id": run_id, "reads0": reads0, "evaluation": dec.last_demand_eval_at}
            what = "HARD_INVALIDATION" if protective else ", ".join(cands)
            self._record(lambda: self._last_touch.pop("EXIT_SUBMISSION", None))
            if self._maybe_submit_exit(pos, now, trigger, plan):
                out["exits_submitted"] += 1
                decide(pos, "EXIT_SENT", lambda: f"Exit required: {what}. Exit order sent to the broker ({self._last_touch.get('EXIT_SUBMISSION') or 'see its order'}).")
            else:
                out["exits_deferred"] += 1
                decide(pos, "EXIT_DEFERRED", lambda: f"Exit required: {what}. Not sent this cycle: {self._last_touch.get('EXIT_SUBMISSION') or 'deferred'}.")
        if not held:
            self._run_probes(now, probes, states, reads0, trigger, run_id)
        if self.stop_check_problems:                                                         # Wave 1: a later PASS never hides a blind stop check
            self._touch(now, "EXIT_TRIGGER", "UNKNOWN", "; ".join(self.stop_check_problems[s] for s in sorted(self.stop_check_problems)),
                        {"symbols": sorted(self.stop_check_problems)})
        return out

    @staticmethod
    def _c1d_contain(failed: dict, fn: Callable[[], Any]) -> Any:
        """c1d-fix2 M-1: one pass-1 step of ONE position. A raise is recorded in `failed` (that position's failure) and gives None; once a step of
        the position has failed, its later steps are not run. A SimulatedCrash propagates, as everywhere."""
        if failed:
            return None
        try:
            return fn()
        except SimulatedCrash:
            raise
        except Exception as exc:  # noqa: BLE001 - contained: this position only
            failed["exc"] = exc
            return None

    @staticmethod
    def _c1d_no_decision() -> Tuple[Any, Any]:
        """c1d-fix2 M-1: the stand-ins after a failed exit context or exit_decision, so that SC's r14 3.3.3 effects still run (SC is computed apart
        from the exit decision, r14 3.3.1). No price, no candidate, no detail: nothing here can decide an exit or change a stored field."""
        return (SimpleNamespace(price=None, price_at=None),
                SimpleNamespace(candidates=(), detail={}, reason=None, weakening=None, demand_low_streak=None, last_demand_eval_at=None, sessions_held=None))

    def _c1d_decide(self, pos: Position, now: datetime, trigger: str, run_id: Optional[str], states: Any, sc: Any, dec: Any, cands: List[str],
                    fields: Dict[str, Any], market_open: bool, decide: Callable) -> Optional[tuple]:
        """c1d-fix2 M-1: pass 1's exit decision and store write for ONE position, after its SC (r14 3.4; 3.13.5 strategy (3)), unchanged from the
        former loop body. Returns the pass-2 entry (pos, sc, dec, cands) of a decided exit, else None. Called only through _c1d_contain."""
        if not cands and states is not None:                                               # r14 3.13.5 strategy (3): a LATER evaluation and no candidate
            s = states.get(("STRATEGY_EXIT_HELD", pos.position_id)) or {}
            old, ost = DG.parse_ts(s.get("evaluation"))
            new, nst = DG.parse_ts(dec.last_demand_eval_at)
            if s.get("state") in DG.UNRESOLVED_STATES and ost == nst == DG.TS_OK and new > old:
                self._safety_transition(now, trigger, run_id, "STRATEGY_EXIT_HELD", pos.position_id, pos.symbol, "EVIDENCE", evidence="HELD_REASONS_CLEARED")
        decided = "HARD_INVALIDATION" if sc.status == DG.HIT else (cands[0] if cands else None)
        if decided is not None:
            if pos.state != "EXIT_REQUIRED":
                fields.update(state="EXIT_REQUIRED", exit_reason=decided, exit_requested_at=_iso(now), weakening_reason=None)
                pos = self.store.update_position(pos.position_id, now, {"kind": "EXIT_REQUIRED", "from_state": pos.state, "to_state": "EXIT_REQUIRED", "reason": decided, "detail": dec.detail}, **fields)
                self._touch(now, "EXIT_TRIGGER", "RUN", f"{pos.symbol}: {decided}", dec.detail)
            else:
                pos = self.store.update_position(pos.position_id, now, None, **fields)
            return pos, sc, dec, cands
        if sc.status == DG.BLIND:
            self.store.update_position(pos.position_id, now, None, **fields)                # no state change: neither HEALTHY nor an exit
            decide(pos, "ATTENTION" if market_open else "HOLD", f"The stop check could not run ({', '.join(sc.codes)}). Holding; no automatic exit on an unverified price.")
            return None
        new_state = "WEAKENING" if dec.weakening else "HEALTHY"
        ev = None
        if pos.state in ("OPEN", "HEALTHY", "WEAKENING") and new_state != pos.state:
            fields.update(state=new_state, weakening_reason=dec.weakening)
            ev = {"kind": "STATE", "from_state": pos.state, "to_state": new_state, "reason": dec.weakening or "healthy", "detail": dec.detail}
        self.store.update_position(pos.position_id, now, ev, **fields)
        self._touch(now, "EXIT_TRIGGER", "PASS", f"{pos.symbol}: no exit condition", dec.detail)
        decide(pos, "HOLD", lambda: "No exit condition is met; holding." + (f" Weakening: {dec.weakening}." if dec.weakening else ""))
        return None

    def _c1d_pass1_failed(self, pos: Position, now: datetime, sc: Any, exc: Exception, decide: Callable) -> Optional[tuple]:
        """c1d-fix2 M-1 (B2 review): this position's setup / episode read, exit context, exit_decision or pass-1 store write raised. Fail closed for
        this position alone: no exit decision is made or invented for it and nothing more is written to its row; the EXIT_TRIGGER UNKNOWN row
        and its stop_check_problems entry make it visible and block new entries this cycle (as r14 3.3.3 does for an SC failure). The loop and
        pass 2 go on, so no other position loses its protective exit. A verified HIT (SC stands apart from the exit decision, r14 3.3.1) still
        gets its protective-exit attempt: the returned pass-2 entry, so the order follows from the HIT through the exit gate, never from the
        failure. Never raises."""
        name = type(exc).__name__
        log.warning("execution: position evaluation failed (%s); contained to the position", name)
        hit = getattr(sc, "status", None) == DG.HIT
        reason = (f"{pos.symbol}: exit evaluation failed ({name}); no exit decision" + ("; the verified stop hit takes the protective path" if hit else "; no automatic action")
                  + "; new entries are blocked")
        self.stop_check_problems[pos.symbol] = reason
        self._touch(now, "EXIT_TRIGGER", "UNKNOWN", reason, {"position_id": pos.position_id, "exception": name, "stop_status": getattr(sc, "status", None)})
        decide(pos, "ATTENTION", f"The exit evaluation failed ({name}). No automatic action from it.")
        return (pos, sc, self._c1d_no_decision()[1], []) if hit else None

    def _maybe_submit_exit(self, pos: Position, now: datetime, trigger: str, plan: Optional[dict] = None) -> bool:
        gate: Optional[dict] = None
        if self._simulated_broker():                                                        # r14 3.11: a simulated broker behaves exactly as today
            try:
                self._clock_cache = self.broker.clock()
            except BrokerError:
                self._clock_cache = None
            sp = session_policy(now, self._clock_cache, self.near_close_minutes)
            if sp.result != "PASS":
                self._touch(now, "EXIT_SUBMISSION", "WARN", f"{pos.symbol}: exit deferred — {sp.reason}", sp.input)
                return False
        else:                                                                               # HDR-2 r14 3.8 exit gate, steps 1-3: P8a-1, the reservation, P8b, P8a-2
            gate = self._exit_gate_pre(pos, now, trigger, plan)
            if gate is None:
                return False
        if pos.exit_cycle >= self.max_exit_cycles:
            self.store.update_position(pos.position_id, now, {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": "EXIT_ATTEMPTS_EXHAUSTED"},
                                       state="ATTENTION", attention_reason="EXIT_ATTEMPTS_EXHAUSTED")
            self._touch(now, "EXIT_SUBMISSION", "BLOCK", f"{pos.symbol}: {pos.exit_cycle} exit attempts exhausted; needs attention", {"position_id": pos.position_id})
            return False
        if gate is None:
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
        else:                                                                               # r14 3.8 steps 5-7: the collision check, P1-P7, P8c, the intent
            intent = self._exit_gate_intent(pos, now, gate)
            if intent is None:
                return False
        self._hook("after_exit_intent", intent=intent)
        self._submit(intent, now, "EXIT_SUBMISSION", recheck=None, gate=gate)
        return True if gate is None else self._exit_after_post(intent, now, gate)

    # ---------------------------------------------------------------- HDR-2 data gate: exits (EXEC-C1c1; r14 3.1, 3.3-3.8, 3.11, 3.13, 3.14) ----------
    # Real (or unknown) broker only: every function below is reached only when self.broker.simulated is not True (r14 3.11).
    def _simulated_broker(self) -> bool:
        return getattr(self.broker, "simulated", None) is True

    def _broker_class(self, now: datetime) -> Tuple[str, Any]:
        """r14 2.2 / 9.16: the ONE broker-class computation of an evaluation, from `broker.simulated` and a FRESH environment read in this
        evaluation. Reads no environment_cache, _env_at, intent.environment or _clock_cache. Returns (class, the environment read or UNREADABLE)."""
        if self._simulated_broker():
            return DG.SIMULATED, None
        try:
            env = self.broker.environment(now)
        except Exception:  # noqa: BLE001 - a raised read is Unknown (r14 2.2)
            env = DG.UNREADABLE
        return DG.broker_class(getattr(self.broker, "simulated", None), env), env

    def _entry_identity(self, pos: Position) -> Optional[dict]:
        """r14 3.5.1 P2 / 9.16: the ONLY read of an intent's environment in the gate: the entry intent's durable identity, found by pos.execution_id."""
        ent = self.store.intent(pos.execution_id) if pos.execution_id else None
        if ent is None or not isinstance(ent.environment, dict):
            return None
        return {k: ent.environment.get(k) for k in ("account_number_masked", "host", "label")}

    def _market_mode(self) -> Any:
        """r14 2.5: str(stream.market.mode); an injected price_fn counts as `test`; an exception or a missing value is unknown (None)."""
        if self.price_fn is not None:
            return "test"
        try:
            return str(self.stream_fn().market.mode)
        except Exception:  # noqa: BLE001 - unknown is never real
            return None

    def _gate_price(self, symbol: str, eval_clock: datetime, clock_ts: Any) -> "DG.GatePrice":
        """r14 2.4.1 on a real broker: the quote as _price reads it, else the last candle final BY THE CLOCK (end <= the evaluation clock and <= the
        broker clock's timestamp of the same evaluation), else UNAVAILABLE. A simulated broker keeps _price unchanged."""
        if self.price_fn is not None:
            value, src, at = self.price_fn(symbol)
            return DG.GatePrice(src if src in ("QUOTE", "MINUTE_CLOSE") else "UNAVAILABLE", value, at, "OK" if value is not None else "UNAVAILABLE",
                                True if src == "MINUTE_CLOSE" else None)
        st = self.stream_fn()
        try:
            quote = st.market.quote(symbol)
        except Exception:  # noqa: BLE001 - an unreadable quote: the candle fallback, else UNAVAILABLE (BLIND)
            quote = None
        try:
            candles = st.candles(symbol)
        except Exception:  # noqa: BLE001
            candles = None
        return DG.select_gate_price(quote, candles, eval_clock, clock_ts)

    def _gate_calendar(self, eval_clock: datetime) -> Tuple[str, Callable, Callable]:
        """r14 2.6 / 3.14 (OD-DG-4): the calendar fact of the evaluation's session date; an import failure, a raise or a non-bool is UNREADABLE."""
        try:
            from autorun import calendar as _cal
            cov, mday = _cal.covered, _cal.is_market_day
        except Exception:  # noqa: BLE001 - the import failing is UNREADABLE, never UNCOVERED (r5, N-2)
            def cov(_d):
                raise RuntimeError("calendar unreadable")
            mday = cov
        return DG.classify_calendar(session_date(eval_clock), cov, mday), cov, mday

    def _reads_left(self, reads0: Any) -> int:
        return DG.reads_remaining(getattr(self.broker, "budget", None), getattr(self.broker, "reads", None), reads0)

    def _gate_facts(self, order_class: str, stage: str, pos: Position, eval_clock: datetime, *, clock: Any, order_qty: Any, stop: Any = None,
                    reason: Optional[str] = None) -> "DG.ExitFacts":
        """r14 3.1: the exit collector (C1c1: EXIT facts only; entries are C1c2). Called only after P8a-1, P8b and P8a-2 passed (condition (d): no
        fact is read once P8b fails or a P8a step ends the evaluation). Each read is wrapped on its own: a read that raises is UNREADABLE, a failure."""
        _bc, env = self._broker_class(eval_clock)                                              # P1 (fresh environment read)
        try:
            ent = self._entry_identity(pos)                                                      # P2
        except Exception:  # noqa: BLE001
            ent = DG.UNREADABLE
        try:
            b = self.broker.position(pos.symbol)                                                 # P3 / P4
        except Exception:  # noqa: BLE001
            b = DG.UNREADABLE
        try:
            oo = self.broker.orders("open", [pos.symbol])                                        # P6
        except Exception:  # noqa: BLE001
            oo = DG.UNREADABLE
        cal, cov, mday = self._gate_calendar(eval_clock)                                         # P8c / X8
        trig = recount = None
        if order_class == DG.PROTECTIVE_EXIT and stage == DG.DG_FINAL:                          # P9 at DG-final: the trigger's validity (not the level)
            ts = clock.get("timestamp") if isinstance(clock, dict) else None
            trig = DG.StopCheckFacts(eval_clock, self._market_mode(), self._gate_price(pos.symbol, eval_clock, ts), pos.invalidation_price, clock, 0.0)
        if order_class == DG.STRATEGY_EXIT:
            recount = DG.time_exit_recount(pos.opened_at, eval_clock, cov, mday)
        return DG.ExitFacts(order_class=order_class, stage=stage, eval_clock=eval_clock, broker_simulated=getattr(self.broker, "simulated", None), environment=env,
                            entry_identity=ent, broker_position=b, order_qty=order_qty, pos_quantity=pos.quantity, open_orders=oo, broker_clock=clock,
                            near_close_minutes=self.near_close_minutes, calendar=cal, stop=stop, trigger=trig, reason=reason, recount=recount,
                            max_hold_sessions=self.max_hold_sessions)

    def _session_and_clock(self, eval_clock: datetime) -> Tuple[Optional[str], Any, Any, Any]:
        """r14 3.5.1 order: P8a-1 (no broker read), then P8b (one fresh clock read; is_open not part of it), then P8a-2 (only on a verified clock).
        Returns (failed step or None, session_policy result, the clock read, the ClockCheck). Used by the exit gate and by DG-final."""
        sp1 = session_policy(eval_clock, None, self.near_close_minutes)
        if sp1.result != "PASS":
            return "P8a-1", sp1, None, None
        try:
            clock = self.broker.clock()
        except Exception:  # noqa: BLE001 - unreadable: a P8b refusal
            clock = DG.UNREADABLE
        ck = DG.check_clock(clock, eval_clock)
        if not ck.verified:
            return "P8b", sp1, clock, ck
        sp2 = session_policy(eval_clock, {"is_open": ck.is_open}, self.near_close_minutes)
        if sp2.result != "PASS":
            return "P8a-2", sp2, clock, ck
        return None, sp2, clock, ck

    @staticmethod
    def _gate_prov(v: Any) -> dict:
        try:
            return {"code": v.code, "codes": list(v.codes), "failed_facts": list(v.failed_facts), "provenance": dict(v.provenance)}
        except Exception:  # noqa: BLE001 - display only
            return {}

    def _exit_refused(self, pos: Any, now: datetime, g: dict, codes: List[str], inp: Optional[dict] = None) -> None:
        """r14 3.5.3 for a HIT (no post_order; the PROTECTIVE_EXIT_UNVERIFIED FAILURE; an EXIT_SUBMISSION BLOCK row every cycle with the codes and
        provenance; no ATTENTION, no cycle consumed by the refusal itself) or r14 3.4 step 3 for a strategy exit (a hold: the informational
        STRATEGY_EXIT_HELD FAILURE and an EXIT_SUBMISSION WARN row). Never raises (the writer and _touch never raise)."""
        codes = [c for c in dict.fromkeys(codes) if isinstance(c, str) and c] or ["GATE_ERROR"]
        row = {"position_id": getattr(pos, "position_id", None), "codes": codes, **(inp or {})}
        sym = getattr(pos, "symbol", "?")
        if g.get("order_class") == DG.PROTECTIVE_EXIT:
            text = f"{sym}: protective exit refused ({', '.join(codes)}); no automatic order; critical alert"
            self.protective_exit_problems[sym] = text                                          # r14 3.5.3 item 3 (C1C1-A10): set in the same evaluation as the refusal
            self._safety_transition(now, g.get("trigger") or "EXIT_SUBMISSION", g.get("run_id"), "PROTECTIVE_EXIT_UNVERIFIED", getattr(pos, "position_id", None), sym, "FAILURE",
                                    reason_codes=codes, exit_cycle=getattr(pos, "exit_cycle", None))
            self._touch(now, "EXIT_SUBMISSION", "BLOCK", text, row)
            self._touch(now, "PROTECTIVE_EXIT", "BLOCK", text, row)                            # r14 3.11 (C1C1-A9): the PROTECTIVE_EXIT row on a refusal
        else:
            self._safety_transition(now, g.get("trigger") or "EXIT_SUBMISSION", g.get("run_id"), "STRATEGY_EXIT_HELD", getattr(pos, "position_id", None), sym, "FAILURE",
                                    reason_codes=codes, exit_cycle=getattr(pos, "exit_cycle", None), evaluation=g.get("evaluation") or None)
            self._touch(now, "EXIT_SUBMISSION", "WARN", f"{sym}: strategy exit held ({', '.join(codes)}); no order", row)

    def _exit_contained(self, pos: Any, now: datetime, g: dict, exc: Exception) -> None:
        """r14 3.1 Z2: no POST; an intent row with attempt_count 0 and no broker_order_id goes NOT_SENT DG_GATE_ERROR and _after_terminal runs;
        containment NEVER changes exit_cycle and makes no broker read; a HIT is a GATE_ERROR refusal (3.5.3), a strategy exit a hold."""
        log.warning("execution: exit gate error contained (%s)", type(exc).__name__)
        try:
            xid = g.get("intent_id")
            it = self.store.intent(xid) if xid else None
            if it is not None and it.attempt_count == 0 and not it.broker_order_id and it.state in INTENT_OPEN:
                it = self.store.update_intent(xid, now, state="NOT_SENT", failure_code="DG_GATE_ERROR", resolved_at=_iso(now),
                                              failure_text=f"gate error ({type(exc).__name__}); nothing was sent")
                self._after_terminal(it, "NOT_SENT", now)
        except SimulatedCrash:
            raise
        except Exception as exc2:  # noqa: BLE001 - containment itself never aborts the loop
            log.warning("execution: exit gate containment write failed (%s)", type(exc2).__name__)
        self._exit_refused(pos, now, g, ["GATE_ERROR"], {"exception": type(exc).__name__})

    def _exit_gate_pre(self, pos: Position, now: datetime, trigger: str, plan: Optional[dict]) -> Optional[dict]:
        """r14 3.8 exit gate steps 1-3 (before today's exhaustion check, step 4): P8a-1 (a deferral), the read reservation (3.8.1), P8b (a refusal),
        P8a-2 (a deferral under OD-R5-1(a)). Returns the evaluation context, or None when this evaluation ended here."""
        g = dict(plan) if plan else {"order_class": DG.STRATEGY_EXIT, "candidates": [pos.exit_reason or "UNKNOWN"]}   # no plan: fail closed (unknown reason held)
        g.setdefault("trigger", trigger)
        g["pos"] = pos
        g["eval_clock"] = eval_clock = self.clock()                                            # a fresh evaluation clock (r14 2.5)
        if g.get("order_class") == DG.STRATEGY_EXIT and g.get("candidates") is None:
            g["candidates"] = [pos.exit_reason or "UNKNOWN"]
        sp1 = session_policy(eval_clock, None, self.near_close_minutes)
        if sp1.result != "PASS":                                                                 # P8a-1: no broker read, no lifecycle, no cycle
            self._c1d_dg_short(now, DG.EXIT_GATE, g, "P8a-1", "P8a-1", DG.DEFER, {"position_id": pos.position_id, "symbol": pos.symbol}, {"session_reason": sp1.reason})
            self._touch(now, "EXIT_SUBMISSION", "WARN", f"{pos.symbol}: exit deferred — {sp1.reason}", {**sp1.input, "step": "P8a-1"})
            return None
        if self._reads_left(g.get("reads0")) < DG.EXIT_RESERVE_READS:                          # 3.8.1: exit gate + DG-final need 8; no intent, no cycle
            self._exit_refused(pos, now, g, ["BUDGET_RESERVED"], {"reserve": DG.EXIT_RESERVE_READS})
            return None
        step, sp, clock, ck = self._session_and_clock(eval_clock)
        if step == "P8b":
            self._c1d_dg_short(now, DG.EXIT_GATE, g, "P8b", "P8_CLOCK_" + str(ck.problem), None, {"position_id": pos.position_id, "symbol": pos.symbol},
                               {"clock": {"problem": ck.problem, "skew_s": ck.skew_s}})
            self._exit_refused(pos, now, g, ["P8_CLOCK_" + str(ck.problem)], {"step": "P8b", "clock": {"problem": ck.problem, "skew_s": ck.skew_s}})
            return None
        if step == "P8a-2":
            self._c1d_dg_short(now, DG.EXIT_GATE, g, "P8a-2", "P8a-2", DG.DEFER, {"position_id": pos.position_id, "symbol": pos.symbol}, {"session_reason": sp.reason})
            self._touch(now, "EXIT_SUBMISSION", "WARN", f"{pos.symbol}: exit deferred — {sp.reason}", {**sp.input, "step": "P8a-2"})
            return None
        g["clock"] = clock
        return g

    def _exit_gate_intent(self, pos: Position, now: datetime, g: dict) -> Optional[Intent]:
        """r14 3.8 exit gate steps 5-7, inside containment zone Z2: the collision check (3.8.2, OD-R5-4(b): refuse, never advance), P1-P7 then P8c
        (3.4-3.6; the 3.5.2 quantity rule), then on a verified exit the intent insert with the DECIDED reason (3.5.6) and the EXIT_INTENT update."""
        protective = g.get("order_class") == DG.PROTECTIVE_EXIT
        try:
            k1 = pos.exit_cycle + 1
            col = DG.classify_collision(self.store.intent(exit_id_for(pos.position_id, k1)), pos.position_id, k1, INTENT_OPEN)
            if col == DG.COLLISION_OPEN:                                                         # a race: the order monitor owns it; NEITHER
                self._touch(now, "EXIT_SUBMISSION", "UNKNOWN", f"{pos.symbol}: an open exit intent exists for cycle {k1}; the order monitor follows it", {"position_id": pos.position_id})
                return None
            if col != DG.COLLISION_NONE:
                code = "EXIT_CYCLE_COLLISION" if col == DG.COLLISION_NEVER_ATTEMPTED else "EXIT_CYCLE_COLLISION_ATTEMPTED"
                self._exit_refused(pos, now, g, [code], {"exit_cycle": pos.exit_cycle, "colliding": exit_id_for(pos.position_id, k1)})
                return None
            facts = self._gate_facts(g["order_class"], DG.EXIT_GATE, pos, g["eval_clock"], clock=g.get("clock"), order_qty=pos.quantity, stop=g.get("stop"))
            cands = ["HARD_INVALIDATION"] if protective else [c for c in (g.get("candidates") or []) if c != "HARD_INVALIDATION"] or ["UNKNOWN"]
            verdicts = [(c, DG.evaluate(facts if protective else replace(facts, reason=c))) for c in cands]
            for c, v in verdicts:                                                                # r14 3.11 (C1C1-A9): every verdict is a DATA_GATE row
                self._dg_row(now, v, {"stage": DG.EXIT_GATE, "position_id": pos.position_id, "symbol": pos.symbol, "reason": c})
            chosen = next(((c, v) for c, v in verdicts if v.allowed), None)
            if chosen is None:
                if any(v.outcome == DG.QTY_DISAGREEMENT for _, v in verdicts):                # r14 3.5.2: today's QTY handling (+ the lifecycle row FIRST for a HIT)
                    self._exit_qty(pos, now, g, facts.broker_position, verdicts)
                else:
                    self._exit_refused(pos, now, g, [c for _, v in verdicts for c in v.codes], {"verdicts": {c: self._gate_prov(v) for c, v in verdicts}})
                return None
            decided, verdict = chosen
            cycle = k1
            env = dict(facts.environment) if isinstance(facts.environment, dict) else {}
            env["data_gate"] = {"exit_gate": {"order_class": g["order_class"], "reason": decided, **self._gate_prov(verdict)}}
            intent = Intent(execution_id=exit_id_for(pos.position_id, cycle), intent_type="EXIT", symbol=pos.symbol, side="sell", qty=pos.quantity,
                            client_order_id=exit_coid(pos.position_id, cycle), proposal_id=pos.proposal_id, setup_id=pos.setup_id, position_id=pos.position_id, exit_cycle=cycle,
                            exit_reason=decided, reference_price=pos.last_price, invalidation_price=pos.invalidation_price, risk_budget=pos.risk_budget, state="PENDING",
                            created_at=_iso(now), updated_at=_iso(now), created_by=self.owner, environment=env)
            g["intent_id"] = intent.execution_id
            g["decided"] = decided
            intent = self.store.insert_intent(intent)
            self.store.update_position(pos.position_id, now, {"kind": "EXIT_INTENT", "from_state": pos.state, "to_state": "EXIT_SUBMITTED", "reason": decided, "detail": {"exit_cycle": cycle}},
                                       state="EXIT_SUBMITTED", exit_cycle=cycle, exit_reason=decided)
            return intent
        except SimulatedCrash:
            raise
        except Exception as exc:  # noqa: BLE001 - Z2 containment (r14 3.1)
            self._exit_contained(pos, now, g, exc)
            return None

    def _exit_qty(self, pos: Position, now: datetime, g: dict, b: Any, verdicts: list) -> None:
        """r14 3.5.2: only today's exact condition (broker flat, fewer shares, or a negative quantity). No POST; today's QTY_MISMATCH /
        BROKER_FLAT_INTERNAL_OPEN finding and ATTENTION stay; for a HIT the PROTECTIVE_EXIT_UNVERIFIED row is written BEFORE the quantity finding."""
        if g.get("order_class") == DG.PROTECTIVE_EXIT:
            self._safety_transition(now, g.get("trigger") or "EXIT_SUBMISSION", g.get("run_id"), "PROTECTIVE_EXIT_UNVERIFIED", pos.position_id, pos.symbol, "FAILURE",
                                    reason_codes=[c for c in dict.fromkeys(c for _, v in verdicts for c in v.codes)] or ["P3"], exit_cycle=pos.exit_cycle)
        bq = b.get("qty") if isinstance(b, dict) else None
        det = {"internal_qty": pos.quantity, "broker_qty": bq if b else 0}
        self._finding(now, g.get("trigger") or "EXIT_SUBMISSION", Finding(kind="QTY_MISMATCH" if b else "BROKER_FLAT_INTERNAL_OPEN", symbol=pos.symbol, position_id=pos.position_id, detail=det))
        self.store.update_position(pos.position_id, now, {"kind": "ATTENTION", "from_state": pos.state, "to_state": "ATTENTION", "reason": "QTY_MISMATCH", "detail": det},
                                   state="ATTENTION", attention_reason="QTY_MISMATCH")
        self._touch(now, "EXIT_SUBMISSION", "BLOCK", f"{pos.symbol}: broker holds {det['broker_qty']} vs internal {pos.quantity}; no automatic action", det)

    def _dg_final_exit(self, it: Intent, now: datetime, control_id: str, gate: Optional[dict]) -> bool:
        """r14 3.8 DG-final for an EXIT intent: the FIRST statement of _submit, before the attempt write. Re-collects and re-checks P1-P8c in the
        3.5.1 order and the trigger's validity (protective; the level is not re-tested) or the reason's provenance and X8 (strategy). True: proceed
        to the attempt write. False: nothing was sent (a refusal, a DG-final session failure, or a Z2 containment), and the handling is done here.
        A simulated broker: True, recording nothing (r14 3.11)."""
        if self._simulated_broker():
            return True
        g = gate if gate is not None else {"order_class": DG.PROTECTIVE_EXIT if it.exit_reason == "HARD_INVALIDATION" else DG.STRATEGY_EXIT, "trigger": control_id}
        g["intent_id"] = it.execution_id
        pos: Any = SimpleNamespace(position_id=it.position_id, symbol=it.symbol, exit_cycle=it.exit_cycle)
        try:
            p = self.store.position(it.position_id) if it.position_id else None
            if p is None:
                raise LookupError("the exit intent's position is not in the store")
            pos = p
            row = {"position_id": it.position_id, "symbol": it.symbol, "execution_id": it.execution_id}
            if it.exit_reason not in EXIT_REASONS:                                               # C1C1-A12 (HDR condition): an unknown reason is refused, no POST
                self._c1d_dg_short(now, DG.DG_FINAL, g, "REASON", "DG_EXIT_REASON_UNKNOWN", None, row, {"exit_reason": it.exit_reason})
                return self._dg_final_refused(it, pos, now, g, ["DG_EXIT_REASON_UNKNOWN"], {"exit_reason": it.exit_reason})
            eval_clock = self.clock()
            step, sp, clock, ck = self._session_and_clock(eval_clock)
            if step in ("P8a-1", "P8a-2"):
                self._c1d_dg_short(now, DG.DG_FINAL, g, step, "DG_FINAL_SESSION_FAILED", DG.SESSION_FAILURE, row, {"session_reason": getattr(sp, "reason", None)})
                return self._dg_final_session_failure(it, pos, now, g, step, sp)
            if step == "P8b":
                self._c1d_dg_short(now, DG.DG_FINAL, g, "P8b", "P8_CLOCK_" + str(ck.problem), None, row, {"clock": {"problem": ck.problem, "skew_s": ck.skew_s}})
                return self._dg_final_refused(it, pos, now, g, ["P8_CLOCK_" + str(ck.problem)], {"step": "P8b"})
            v = DG.evaluate(self._gate_facts(g["order_class"], DG.DG_FINAL, pos, eval_clock, clock=clock, order_qty=it.qty,
                                             reason=None if g["order_class"] == DG.PROTECTIVE_EXIT else it.exit_reason))
            self._dg_row(now, v, {"stage": DG.DG_FINAL, "position_id": it.position_id, "symbol": it.symbol, "execution_id": it.execution_id})
            if v.allowed:
                g["dg_final_allowed"] = True
                return True
            if v.outcome == DG.SESSION_FAILURE:
                return self._dg_final_session_failure(it, pos, now, g, v.provenance.get("step") or "P8a", SimpleNamespace(reason=v.provenance.get("session_reason")))
            return self._dg_final_refused(it, pos, now, g, list(v.codes), {"dg_final": self._gate_prov(v)})
        except SimulatedCrash:
            raise
        except Exception as exc:  # noqa: BLE001 - Z2: DG-final inside _submit, before the attempt write
            self._exit_contained(pos, now, g, exc)
            return False

    def _c1d_dg_short(self, now: datetime, stage: str, g: dict, fact: str, code: str, outcome: Optional[str], inp: dict, prov: dict) -> None:
        """c1d-fix2 L-3 (r14 3.11, r14:952 'every verdict is a DATA_GATE row'): the DATA_GATE row of a verdict the service reaches before
        DG.evaluate runs (P8a-1, P8b, P8a-2 at the exit gate; P8a, P8b and the unknown exit reason at DG-final), in the pure gate's own form
        (code, codes, failed_facts, provenance with the step and the outcome). Written beside, never instead of, the EXIT_SUBMISSION /
        PROTECTIVE_EXIT rows. Never raises (_dg_row writes through _touch)."""
        oc = g.get("order_class")
        if outcome is None:
            outcome = DG.REFUSE if oc == DG.PROTECTIVE_EXIT else DG.HOLD
        v = DG.GateVerdict(False, oc, code, (code,), (fact,), {"stage": stage, "step": fact, "outcome": outcome, **prov})
        self._dg_row(now, v, {"stage": stage, **inp})

    def _dg_final_refused(self, it: Intent, pos: Any, now: datetime, g: dict, codes: List[str], inp: dict) -> bool:
        """r14 3.8 'A DG-final refusal of an exit': the intent goes NOT_SENT, _after_terminal returns the position to EXIT_REQUIRED, and in the same
        evaluation a HIT gets the 3.5.3 handling (the cycle the intent carries is consumed; exit_cycle is never rolled back)."""
        done = self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="DG_FINAL_REFUSED", resolved_at=_iso(now),
                                        failure_text=("DG-final refused: " + ", ".join(codes))[:500])
        self._after_terminal(done, "NOT_SENT", now)
        self._exit_refused(pos, now, g, codes, {"stage": "DG_FINAL", "execution_id": it.execution_id, **inp})
        return False

    def _dg_final_session_failure(self, it: Intent, pos: Any, now: datetime, g: dict, step: str, sp: Any) -> bool:
        """r14 3.8 'A DG-final session failure' (OD-R6-1(a)): no attempt write, no post_order; the intent NOT_SENT DG_FINAL_SESSION_FAILED naming the
        step and session_policy's reason; _after_terminal; the cycle consumed and never rolled back; a HIT is the protective-family FAILURE with a
        BLOCK row (critical); a strategy exit a WARN row and STRATEGY_EXIT_HELD. On the last cycle the codes add EXIT_ATTEMPTS_EXHAUSTED."""
        reason = getattr(sp, "reason", None)
        done = self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="DG_FINAL_SESSION_FAILED", resolved_at=_iso(now),
                                        failure_text=f"DG-final session failure at {step}: session_policy says {reason}"[:500])
        self._after_terminal(done, "NOT_SENT", now)
        codes = ["DG_FINAL_SESSION_FAILED"] + (["EXIT_ATTEMPTS_EXHAUSTED"] if (it.exit_cycle or 0) >= self.max_exit_cycles else [])
        self._exit_refused(pos, now, g, codes, {"stage": "DG_FINAL", "execution_id": it.execution_id, "step": step, "session_reason": reason})
        return False

    def _exit_after_post(self, intent: Intent, now: datetime, g: dict) -> bool:
        """r14 3.8 'After the POST': read the intent back; only a stored outcome_class O1 after a verified DG-final is the one-shot evidence
        (PROTECTIVE_EXIT_ACCEPTED / STRATEGY_EXIT_ACCEPTED), written through the never-raising writer. True when the attempt was made."""
        try:
            back = self.store.intent(intent.execution_id)
        except Exception:  # noqa: BLE001 - the derived evidence (2') recovers a lost row next cycle
            return True
        if back is None:
            return False
        if back.outcome_class == "O1" and g.get("dg_final_allowed"):
            protective = g.get("order_class") == DG.PROTECTIVE_EXIT
            if protective and back.position_id:
                self._verified_positions.add(back.position_id)                                  # r14 3.13.7 OD-R6-2(a): a protective exit accepted in this process
            self._safety_transition(now, g.get("trigger") or "EXIT_SUBMISSION", g.get("run_id"), "PROTECTIVE_EXIT_UNVERIFIED" if protective else "STRATEGY_EXIT_HELD",
                                    back.position_id, back.symbol, "EVIDENCE", evidence="PROTECTIVE_EXIT_ACCEPTED" if protective else "STRATEGY_EXIT_ACCEPTED",
                                    execution_id=back.execution_id)
        return back.attempt_count >= 1

    # ---------------------------------------------------------------- HDR-2 data gate: entries, probes, latch (EXEC-C1c2; r14 3.2, 3.8, 3.13.6-3.13.8) ---
    def _dg_row(self, now: datetime, v: Any, inp: dict) -> None:
        """r14 3.11 (C1C1-A9): one DATA_GATE control row per verdict on a real broker (PASS or BLOCK, with the codes and provenance)."""
        codes = list(getattr(v, "codes", ()) or ())
        what = f"{inp.get('symbol') or '?'}: {inp.get('stage')} " + ("allowed" if v.allowed else f"refused ({', '.join(codes)})")
        self._touch(now, "DATA_GATE", "PASS" if v.allowed else "BLOCK", what, {**inp, **self._gate_prov(v)})

    @staticmethod
    def _sc_market_open(clock: Any, read_at: Optional[datetime]) -> bool:
        """C1C1-A17 (revised, fail-closed; HDR C1c1 packet): on the real-broker SC path the market counts as CLOSED only when SC's clock read is
        VERIFIED (readable, timestamped, timezone-aware, skew <= 60 s) and says is_open False; otherwise it counts as open (the alert is raised).
        Wave 1's _market_open_for_stop_check and the simulated broker are unchanged."""
        try:
            ck = DG.check_clock(clock, read_at)
            return not (ck.verified and not ck.is_open)
        except Exception:  # noqa: BLE001 - unknown is open
            return True

    def _capture_attention_baseline(self, cycle_id: Optional[str]) -> None:
        """r14 3.13.7: S_R, the ONE dedicated helper and the only caller of the store read that fills it; made at the start of the real-broker
        positions step (after RECONCILE, before any position is evaluated), tagged with the cycle's identity. Store read only: no broker call, no
        store write. A failure marks S_R unavailable in memory and logs (E-I3's allow-list): entries are then refused DG_SAFETY_STATE_UNREADABLE."""
        try:
            self._attention_baseline = DG.AttentionBaseline(cycle_id, {p.position_id: p.state for p in self.store.positions(open_only=True)})
        except Exception as exc:  # noqa: BLE001 - contained: entries only
            self._attention_baseline = None
            log.warning("execution: attention baseline unavailable (%s)", type(exc).__name__)

    def _latch_retry(self, now: datetime, trigger: str, run_id: Optional[str]) -> None:
        """r14 3.13.8 'Retry: every cycle, before entries', at the start of pass 1 after the sweeps. Per latched key: a failed position read or a
        position in ATTENTION writes nothing and keeps the latch (OD-R7-1(a)); a CLOSED position is POSITION_CLOSED evidence (release, then the
        two-row episode); a protective key with a stored O1 HARD_INVALIDATION exit of a later cycle is the derived (2') evidence; otherwise the
        deferred alert row is re-attempted from the latched content (the writer releases on a durable ACTIVE / RECURRENT row)."""
        for (fam, pid), held in list(self._unsaved_blocking_alerts.items()):
            try:
                p = self.store.position(pid)
            except Exception:  # noqa: BLE001 - a failed position read counts as ATTENTION
                continue
            if p is None or p.state == "ATTENTION":
                continue
            sym = p.symbol or held.get("symbol")
            if p.state == "CLOSED":
                self._safety_transition(now, trigger, run_id, fam, pid, sym, "EVIDENCE", evidence="POSITION_CLOSED")
                continue
            ec = held.get("exit_cycle")
            if fam == "PROTECTIVE_EXIT_UNVERIFIED" and type(ec) is int:
                try:
                    xs = self.store.exit_intents_for_position(pid)
                except Exception:  # noqa: BLE001
                    xs = []
                hit = next((x for x in xs if x.outcome_class == "O1" and x.exit_reason == "HARD_INVALIDATION" and type(x.exit_cycle) is int and x.exit_cycle > ec), None)
                if hit is not None:
                    self._safety_transition(now, trigger, run_id, fam, pid, sym, "EVIDENCE", evidence="PROTECTIVE_EXIT_ACCEPTED", execution_id=hit.execution_id, derived=True)
                    continue
            self._safety_transition(now, trigger, run_id, fam, pid, sym, "FAILURE", reason_codes=held.get("reason_codes"),
                                    exit_cycle=ec if fam == "PROTECTIVE_EXIT_UNVERIFIED" else None, deferred=True)

    def _run_probes(self, now: datetime, probes: List[Any], states: Any, reads0: Any, trigger: str, run_id: Optional[str]) -> None:
        """r14 3.8.1 pass 2, the middle group: the OD-R4-2(a) verification probe (a position with an unresolved or latched protective key) and the
        OD-R6-2(a) startup probe (a position not yet in V), for this cycle's verified NOT_HIT positions in store order. ATTENTION is read AT the
        probe (the r9 rule). Only a probe that verifies every one of P1-P8c adds the position to V and is PROTECTIVE_FACTS_VERIFIED evidence."""
        for pos in probes:
            pid = pos.position_id
            key = ("PROTECTIVE_EXIT_UNVERIFIED", pid)
            unresolved = states is None or (states.get(key) or {}).get("state") in DG.UNRESOLVED_STATES or key in self._unsaved_blocking_alerts
            if pid in self._verified_positions and not unresolved:
                continue
            try:
                p = self.store.position(pid)
            except Exception:  # noqa: BLE001 - unreadable: no probe this cycle
                continue
            if p is None or p.state in ("ATTENTION", "CLOSED", "EXIT_SUBMITTED"):
                continue
            if self._protective_probe(p, now, reads0):
                self._verified_positions.add(pid)
                self._safety_transition(now, trigger, run_id, "PROTECTIVE_EXIT_UNVERIFIED", pid, p.symbol, "EVIDENCE", evidence="PROTECTIVE_FACTS_VERIFIED")

    def _protective_probe(self, pos: Position, now: datetime, reads0: Any) -> bool:
        """r14 3.13.5 protective (3) / 3.13.7: the verification-only probe, the exit collector reading P1-P8c fresh (4 reads, reserved), with no
        intent, no POST and no lifecycle row of its own. True only when every fact verified; a short budget, a session step, a raise or any failed
        fact is False (no transition). P9 is not a probe fact: the protective row is evaluated as entered (the status stub), P1-P8c only."""
        if self._reads_left(reads0) < PROBE_RESERVE_READS:
            return False
        try:
            eval_clock = self.clock()
            step, _sp, clock, _ck = self._session_and_clock(eval_clock)
            if step is not None:
                return False
            v = DG.evaluate(self._gate_facts(DG.PROTECTIVE_EXIT, DG.EXIT_GATE, pos, eval_clock, clock=clock, order_qty=pos.quantity, stop=SimpleNamespace(status=DG.HIT)))
            self._dg_row(now, v, {"stage": "PROBE", "position_id": pos.position_id, "symbol": pos.symbol})
            return v.allowed is True
        except SimulatedCrash:
            raise
        except Exception as exc:  # noqa: BLE001 - a probe that could not run verifies nothing
            log.warning("execution: protective probe failed (%s)", type(exc).__name__)
            return False

    def _entry_read2(self) -> Tuple[Any, Any, Any]:
        """r14 3.2 E10(ii)/(iv)/(v): the fresh store reads at an entry evaluation (read 2: the open positions; the lifecycle states of those
        positions). Store reads only: no broker call, no store write. Returns (lifecycle rows or UNREADABLE, {position_id: state} or UNREADABLE,
        V plus every open position with a durable unresolved protective key)."""
        try:
            rows = self.store.positions(open_only=True)
            now_states: Any = {p.position_id: p.state for p in rows}
        except Exception:  # noqa: BLE001
            rows, now_states = None, DG.UNREADABLE
        try:
            if rows is None:
                raise LookupError("open positions unreadable")
            st = self.store.safety_states([p.position_id for p in rows])
            life: Any = [(pid, fam, s.get("state")) for (fam, pid), s in st.items() if s.get("state") != "NONE"]   # NONE: no row for the key
        except Exception:  # noqa: BLE001
            life = DG.UNREADABLE
        verified = set(self._verified_positions)
        if isinstance(life, list):
            verified |= {pid for pid, fam, s in life if fam == "PROTECTIVE_EXIT_UNVERIFIED" and s in DG.UNRESOLVED_STATES}
        return life, now_states, verified

    def _entry_facts(self, stage: str, symbol: str, proposal: Any, expected_id: Any, setup_inputs: Any, env: Any, clock: Any, eval_clock: datetime,
                     live_price: Any = None) -> "DG.EntryFacts":
        """r14 3.1 / 3.2: the entry facts E0-E10. DG-early uses the scan's own reads (no new broker read); DG-final passes fresh ones."""
        life, now_states, verified = self._entry_read2()
        try:
            executable = getattr(proposal, "effective_status", None) == "REVIEWABLE" and (getattr(proposal, "execution_eligibility", None) or {}).get("allowed") is True
        except Exception:  # noqa: BLE001
            executable = False
        return DG.EntryFacts(stage=stage, eval_clock=eval_clock, broker_simulated=getattr(self.broker, "simulated", None), environment=env, market_mode=self._market_mode(),
                             proposal=proposal, expected_proposal_id=expected_id, proposal_executable=executable, setup_inputs=setup_inputs,
                             broker_clock=DG.UNREADABLE if clock is None else clock, calendar=self._gate_calendar(eval_clock)[0], live_price=live_price,
                             stop_check_problems=dict(self.stop_check_problems), protective_exit_problems=dict(self.protective_exit_problems), lifecycle_states=life,
                             latch_keys=list(self._unsaved_blocking_alerts), attention_baseline=self._attention_baseline, cycle_id=self._cycle_id,
                             open_positions_now=now_states, verified_positions=verified)

    def _entry_gate_rows(self, now: datetime, sym: str, pv: Any, setup: Any, env: Any) -> Tuple[List[ControlEvaluation], Any, bool]:
        """r14 3.8 DG-early, per candidate, on a real broker: the PROTECTIVE_EXIT BLOCK row while a protective exit was refused this cycle, and the
        DATA_GATE row (PASS or BLOCK). Returns (rows, verdict, gate_error). Z2: an exception BLOCKs the candidate and stops the scan for this
        cycle. A simulated broker: no row, nothing recorded (E0, r14 3.11)."""
        if self._simulated_broker():
            return [], None, False
        rows: List[ControlEvaluation] = []
        if self.protective_exit_problems:
            rows.append(ControlEvaluation(control_id="PROTECTIVE_EXIT", result="BLOCK", input={"symbols": sorted(self.protective_exit_problems)},
                                          reason="new entries blocked: the protective exit of " + ", ".join(sorted(self.protective_exit_problems)) + " was refused (critical alert)"))
        try:
            v = DG.evaluate(self._entry_facts(DG.DG_EARLY, sym, pv, getattr(pv, "proposal_id", None), getattr(setup, "inputs", DG.UNREADABLE), env, self._clock_cache, self.clock()))
            rows.append(ControlEvaluation(control_id="DATA_GATE", result="PASS" if v.allowed else "BLOCK", input={"stage": DG.DG_EARLY, "symbol": sym, **self._gate_prov(v)},
                                          reason=f"{sym}: data gate " + ("passed" if v.allowed else "refused (" + ", ".join(v.codes) + ")")))
            return rows, v, False
        except SimulatedCrash:
            raise
        except Exception as exc:  # noqa: BLE001 - Z2 for an entry: BLOCKED, and the scan stops for this cycle
            log.warning("execution: entry data gate error contained (%s)", type(exc).__name__)
            rows.append(ControlEvaluation(control_id="DATA_GATE", result="BLOCK", input={"stage": DG.DG_EARLY, "symbol": sym, "codes": ["DG_GATE_ERROR"], "exception": type(exc).__name__},
                                          reason=f"{sym}: data gate error ({type(exc).__name__}); the scan stops for this cycle"))
            return rows, None, True

    def _dg_final_entry(self, it: Intent, now: datetime, control_id: str) -> bool:
        """r14 3.8 entry DG-final: immediately before record_entry_attempt, after the recheck; every fact re-collected (E10(ii) and read 2
        included; S_R is this cycle's, not re-read). A refusal: the intent NOT_SENT DG_FINAL_REFUSED (C1C1-A7), the DATA_GATE BLOCK row,
        _after_terminal. Z2: NOT_SENT DG_GATE_ERROR. True: proceed to the attempt. A simulated broker: True, recording nothing (r14 3.11)."""
        if self._simulated_broker():
            return True
        try:
            eval_clock = self.clock()
            _bc, env = self._broker_class(eval_clock)                                           # E0: a fresh environment read
            try:
                clock = self.broker.clock()                                                      # E4/E5/E8/E9: a fresh clock read
            except Exception:  # noqa: BLE001
                clock = DG.UNREADABLE
            try:
                pv = self.proposals.current(it.symbol, now)                                      # E2-E4: the proposal RE-READ now
            except Exception:  # noqa: BLE001
                pv = DG.UNREADABLE
            try:
                si = self.setups.current(it.symbol).inputs                                       # E6
            except Exception:  # noqa: BLE001
                si = DG.UNREADABLE
            try:
                lp = self._gate_price(it.symbol, eval_clock, clock.get("timestamp") if isinstance(clock, dict) else None)   # E5
            except Exception:  # noqa: BLE001
                lp = DG.UNREADABLE
            v = DG.evaluate(self._entry_facts(DG.DG_FINAL, it.symbol, pv, it.proposal_id, si, env, clock, eval_clock, lp))
            self._dg_row(now, v, {"stage": DG.DG_FINAL, "symbol": it.symbol, "execution_id": it.execution_id})
            if v.allowed:
                return True
            codes = list(v.codes)
            done = self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="DG_FINAL_REFUSED", resolved_at=_iso(now),
                                            failure_text=("DG-final refused: " + ", ".join(codes))[:500])
            self._touch(now, control_id, "BLOCK", f"{it.symbol}: not sent — DG-final refused ({', '.join(codes)})", {"execution_id": it.execution_id, "codes": codes})
            self._after_terminal(done, "NOT_SENT", now)
            return False
        except SimulatedCrash:
            raise
        except Exception as exc:  # noqa: BLE001 - Z2: DG-final inside _submit, before record_entry_attempt
            log.warning("execution: entry DG-final error contained (%s)", type(exc).__name__)
            try:
                cur = self.store.intent(it.execution_id)
                if cur is not None and cur.attempt_count == 0 and not cur.broker_order_id and cur.state in INTENT_OPEN:
                    self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="DG_GATE_ERROR", resolved_at=_iso(now),
                                             failure_text=f"gate error ({type(exc).__name__}); nothing was sent")
            except SimulatedCrash:
                raise
            except Exception as exc2:  # noqa: BLE001
                log.warning("execution: entry gate containment write failed (%s)", type(exc2).__name__)
            self._touch(now, "DATA_GATE", "BLOCK", f"{it.symbol}: DG-final gate error ({type(exc).__name__}); nothing was sent",
                        {"stage": DG.DG_FINAL, "execution_id": it.execution_id, "codes": ["DG_GATE_ERROR"]})
            return False

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
        stop_blind = None                                                                    # Wave 1 (audit EX-14): no new risk while a held stop is unchecked
        if self.stop_check_problems:
            stop_blind = ControlEvaluation(control_id="EXIT_TRIGGER", result="BLOCK", input={"symbols": sorted(self.stop_check_problems)},
                                           reason="new entries blocked: the stop check could not run for " + ", ".join(sorted(self.stop_check_problems)) + " (no fresh, valid price)")
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
            dg_rows, dg_early, dg_error = self._entry_gate_rows(now, sym, pv, setup, env)   # HDR-2 r14 3.8 DG-early: DATA_GATE (+ PROTECTIVE_EXIT) rows, real broker only
            controls = controls + dg_rows
            self.store.touch_controls(controls, now)
            if stop_blind is not None:                                                       # Wave 1: one more blocking reason; every other control still reports
                controls = controls + [stop_blind]
            blocked = blocking(controls)
            self._record(lambda: self._mark("EVALUATED", now=now, setup_id=ep.setup_id, symbol=sym, ctx=ctx, controls=controls))   # Stage 6.10A
            if blocked:
                out["blocked"][sym] = [c.control_id for c in blocked]
                evaluated(sym, "BLOCKED", [{"control": c.control_id, "text": c.reason, **exposure_gate.record(c)} for c in blocked])   # Stage 6.9.1: + the gate's record
                if dg_error:                                                                 # r14 3.1 Z2 for an entry: the scan stops for this cycle
                    break
                continue
            if not self._simulated_broker() and self._reads_left(self._cycle_reads0) < ENTRY_RESERVE_READS:   # r14 3.8.1: recheck + DG-final, before the insert
                out["blocked"][sym] = ["DATA_GATE"]
                self._touch(now, "DATA_GATE", "BLOCK", f"{sym}: data gate refused (DG_BUDGET_RESERVED); no intent", {"stage": DG.DG_EARLY, "codes": ["DG_BUDGET_RESERVED"]})
                evaluated(sym, "BLOCKED", [{"control": "DATA_GATE", "text": f"{sym}: the read budget is short for the recheck and DG-final (DG_BUDGET_RESERVED)"}])
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
            if dg_early is not None:                                                         # HDR-2 r14 3.11: the verdict on the intent (real broker only)
                intent.environment["data_gate"] = {"dg_early": self._gate_prov(dg_early)}
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
        if self.stop_check_problems:                                                         # Wave 1: a held position's stop could not be checked
            problems.append("the stop check of " + ", ".join(sorted(self.stop_check_problems)) + " could not run")
        if self.protective_exit_problems:                                                    # HDR-2 r14 3.5.3 item 3 / 3.13.6: a refused protective exit
            problems.append("the protective exit of " + ", ".join(sorted(self.protective_exit_problems)) + " was refused")
        if self._unsaved_blocking_alerts:                                                    # HDR-2 r14 3.13.8: the unsaved-blocking-alert latch
            problems.append("a blocking safety alert could not be saved")
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
    def _submit(self, it: Intent, now: datetime, control_id: str, recheck: Optional[Callable[[], List[str]]], gate: Optional[dict] = None) -> None:
        if it.intent_type == "EXIT" and not self._dg_final_exit(it, now, control_id, gate):     # HDR-2 r14 3.8: DG-final for an EXIT, before the attempt write
            return
        problems = recheck() if recheck is not None else []
        if problems:
            self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code="PRECHECK_FAILED", failure_text="; ".join(problems)[:500], resolved_at=_iso(now))
            self._touch(now, control_id, "BLOCK", f"{it.symbol}: not sent — " + "; ".join(problems), {"execution_id": it.execution_id})
            self._after_terminal(it, "NOT_SENT", now)
            return
        payload = RU.canonical_payload(it.client_order_id, it.symbol, it.side, it.qty)
        if it.intent_type == "ENTRY":                                                       # C-7 (order gate design 3.5, section 7): the latch IN the attempt's transaction
            if not self._dg_final_entry(it, now, control_id):                               # HDR-2 r14 3.8: entry DG-final, immediately before record_entry_attempt
                return
            refused = self.store.record_entry_attempt(it.execution_id, now)
            if refused:
                self.store.update_intent(it.execution_id, now, state="NOT_SENT", failure_code=refused, resolved_at=_iso(now),
                                         failure_text="Emergency Stop is active" if refused == "EMERGENCY_STOP_ACTIVE" else "the Emergency Stop state could not be read")
                self._touch(now, control_id, "BLOCK", f"{it.symbol}: not sent ({refused})", {"execution_id": it.execution_id, "code": refused})
                self._after_terminal(it, "NOT_SENT", now)
                return
            it = self.store.intent(it.execution_id)
        else:
            it = self.store.update_intent(it.execution_id, now, attempt_count=1, submitted_at=_iso(now))      # exits: byte-identical behaviour, no latch read
        self._hook("after_attempt_recorded", intent=it)
        res = self.broker.post_order(payload, it.client_order_id)
        self._hook("after_post", intent=it, result=res)
        out = RU.classify_post(res, {"client_order_id": it.client_order_id, "symbol": it.symbol, "side": it.side, "qty": it.qty})
        klass, code = out["klass"], out.get("code")
        inp = {"execution_id": it.execution_id, "symbol": it.symbol, "side": it.side, "qty": it.qty, "outcome": klass, "code": code, "http_status": out.get("http_status")}
        # C-7 record: an entry whose attempt was recorded before a stop's commit may reach the broker; if the latch reads ON now, say so. Best effort:
        # a failed read is "not armed", so nothing can come between the broker's answer and recording it (reviews of Task U)
        try:
            armed = it.intent_type == "ENTRY" and klass not in ("O3", "O5", "O6") and self.store.stop_state() == "ON"
        except Exception:  # noqa: BLE001 - the note only; the outcome below is recorded either way
            armed = False
        note = " (armed before the stop)" if armed else ""
        if armed:
            inp["armed_before_stop"] = True
        if klass == "O1":
            self.store.update_intent(it.execution_id, now, outcome_class=klass)
            self._apply_order(it, out["order"], now, "post")
            self._touch(now, control_id, "RUN", f"{it.symbol}: {it.side} {it.qty} submitted ({out['order'].get('status')}){note}", inp)
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
                self._touch(now, control_id, "RUN", f"{it.symbol}: existing broker order adopted{note}", inp)
            else:
                self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", outcome_class=klass, failure_code=code, lookup_count=1, last_lookup_at=_iso(now))
                self._touch(now, control_id, "UNKNOWN", f"{it.symbol}: duplicate id reported but order not found; reconciliation required{note}", inp)
        else:                                                                                 # O2 / O7 / O8: ambiguous — never a second POST
            self.store.update_intent(it.execution_id, now, state="RECONCILIATION_REQUIRED", outcome_class=klass, failure_code=code)
            self._touch(now, control_id, "UNKNOWN", f"{it.symbol}: ambiguous result ({code}); reconciliation required, no retry{note}", inp)

    # ---------------------------------------------------------------- views -----------------------------------------------------------
    def _display_real_broker(self) -> bool:
        """Display only (r14 3.7): the broker class for the alert surfaces WITHOUT touching the broker: no property (never builds a broker), no
        attribute lookup on the broker object (no __getattr__), no call. Reads the client's own `simulated` instance field; anything but exactly
        True is real (unknown is never simulated). No broker built yet: nothing extra is shown (views never build or call the broker)."""
        b = self._broker
        if b is None:
            return False
        try:
            return vars(b).get("simulated") is not True
        except TypeError:  # an object without an instance dict: unknown, so real
            return True

    def _safety_attention(self, held: set) -> List[str]:
        """HDR-2 r14 3.7 / 3.13.7 / 3.13.8, real broker only (display; store reads only): per open position at most one entry per (symbol,
        family): this cycle's protective refusal, else the durable ACTIVE / RECURRENT state (the stop-check family's volatile entry is Wave 1's
        own listing); the latch ("safety alert not saved"); OD-R6-2(a) "protective facts not verified since start". An unreadable lifecycle
        read lists "safety finding state unreadable"."""
        if not self._display_real_broker():
            return []
        try:
            opens = self.store.positions(open_only=True)
        except Exception:  # noqa: BLE001 - display only
            return ["safety finding state unreadable"]
        out: List[str] = []
        try:
            st: Any = self.store.safety_states([p.position_id for p in opens]) if opens else {}
        except Exception:  # noqa: BLE001 - r14 3.13.3
            st = None
            out.append("safety finding state unreadable")
        for p in opens:
            state = lambda fam, p=p: ((st or {}).get((fam, p.position_id)) or {}).get("state")   # noqa: E731
            if p.symbol not in self.stop_check_problems and state("STOP_CHECK_BLIND") in DG.UNRESOLVED_STATES:
                out.append(f"{p.symbol}: stop check unverified (open safety alert)")
            if p.symbol in self.protective_exit_problems:
                out.append(self.protective_exit_problems[p.symbol])
            elif state("PROTECTIVE_EXIT_UNVERIFIED") in DG.UNRESOLVED_STATES:
                out.append(f"{p.symbol}: protective exit unverified (critical alert)")
            for fam, pid in self._unsaved_blocking_alerts:
                if pid == p.position_id:
                    out.append(f"{p.symbol}: safety alert not saved: {fam}")
            if p.state != "ATTENTION" and p.position_id not in self._verified_positions and state("PROTECTIVE_EXIT_UNVERIFIED") not in DG.UNRESOLVED_STATES:
                out.append(f"{p.symbol}: protective facts not verified since start")
        return out

    def _card_safety_reason(self, pos: Optional[Position]) -> Optional[str]:
        """HDR-2 r14 3.7, the position card (display only; real broker only): the reason it shows ATTENTION instead of 'Exiting' / 'In trade'."""
        if pos is None or not self._display_real_broker():
            return None
        if pos.symbol in self.protective_exit_problems:
            return "protective exit refused (critical alert)"
        if pos.symbol in self.stop_check_problems:
            return "stop check could not run"
        try:
            st = self.store.safety_states([pos.position_id])
        except Exception:  # noqa: BLE001 - display only, fail closed
            return "safety finding state unreadable"
        for fam, text in (("PROTECTIVE_EXIT_UNVERIFIED", "protective exit unverified (critical alert)"), ("STOP_CHECK_BLIND", "stop check unverified (open safety alert)")):
            if (st.get((fam, pos.position_id)) or {}).get("state") in DG.UNRESOLVED_STATES:
                return text
        return None

    def _attention(self) -> List[str]:
        reasons: List[str] = []
        for i in self.store.open_intents():
            if i.state == "RECONCILIATION_REQUIRED":
                reasons.append(f"{i.symbol}: order needs reconciliation")
        held = set()
        for p in self.store.positions(open_only=True):
            held.add(p.symbol)
            if p.state == "ATTENTION":
                reasons.append(f"{p.symbol}: {(p.attention_reason or 'position needs attention').lower().replace('_', ' ')}")
        for sym, r in sorted(self.stop_check_problems.items()):                                # Wave 1: a held position whose stop could not be checked
            if sym in held and r not in reasons:
                reasons.append(r)
        for r in self._safety_attention(held):                                                 # HDR-2 r14 3.7: real broker only; nothing on a simulated broker
            if r not in reasons:
                reasons.append(r)
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
        safety_reason = self._card_safety_reason(pos)                                         # HDR-2 r14 3.7: ATTENTION from a blocking safety state (real broker only)
        if safety_reason is not None:
            key = "ATTENTION"
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
            if safety_reason is not None:
                card.line = f"{pos.quantity} shares · {safety_reason}"
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
            card.line = "Risk controls stopped new automatic paper trades"
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
