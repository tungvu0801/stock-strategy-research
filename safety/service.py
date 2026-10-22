"""
safety/service.py — the execution safety gate (DESIGN_591 §3–§10). `ExecutionSafetyService.evaluate(...)` is the ONE function a future
execution layer calls before any new entry; `evaluate_execution_safety(...)` is its module-level entry. Account equity comes from the
Stage 5.9 read-only account snapshot (never invented); the store is the state; a failure never reports TRADING_ALLOWED.
Nothing here submits, previews, liquidates or calls a broker.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import config
from safety import (CONTROLS, R_DAILY, R_EVALUATOR, R_NEW_DAY, R_PERSISTENCE, R_STARTUP_POLICY, REASON_TEXT, STARTUP_POLICIES, VERSION)
from safety.engine import D, daily_loss, decide, controls as control_rows, icon_label, money, s, trading_date
from safety.models import AccountEquity, Baseline, ControlEvaluation, DailyLoss, Freshness, KillSwitch, Pause, SafetyStateRecord, SafetyView
from safety.store import SafetyStore

log = logging.getLogger(__name__)
NO_CASH_FLOW_SOURCES = ("LOCAL_PAPER", "HYPOTHETICAL_REVIEW")      # these account models have no deposits / withdrawals at all


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _state_of(view: Optional[dict]) -> Optional[str]:
    return view.get("state") if view else None


class ExecutionSafetyService:
    def __init__(self, stream_fn: Optional[Callable[[], object]] = None, store: Optional[SafetyStore] = None, *, account_fn: Optional[Callable[[float], AccountEquity]] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), owner: Optional[str] = None, threshold_pct: Any = None, max_age_s: Optional[float] = None,
                 accept_unadjusted: Optional[bool] = None, heartbeat_s: Optional[float] = None, startup_policy: Optional[str] = None):
        self.stream_fn = stream_fn
        self.store = store or SafetyStore()
        self.account_fn = account_fn
        self.clock = clock
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.threshold_pct = threshold_pct if threshold_pct is not None else config.MAX_DAILY_LOSS_PERCENT
        self.max_age_s = float(max_age_s if max_age_s is not None else config.EXECUTION_SAFETY_ACCOUNT_MAX_AGE_SECONDS)
        self.accept_unadjusted = bool(accept_unadjusted if accept_unadjusted is not None else config.EXECUTION_SAFETY_ACCEPT_UNADJUSTED_PNL)
        self.heartbeat_s = float(heartbeat_s if heartbeat_s is not None else config.EXECUTION_SAFETY_EVALUATION_HEARTBEAT_SECONDS)
        self.startup_policy = (startup_policy or config.EXECUTION_SAFETY_STARTUP_POLICY or "KEEP").upper()
        self.listeners: List[Callable[[dict], None]] = []
        self.overrides: Dict[str, Any] = {}                 # tests / harness only: account overrides (equity, source, as_of, age_s, adjustments)
        self._last: Optional[SafetyView] = None

    # ---------------------------------------------------------------- account equity (read only; never invented) --------------
    def account(self, now_ts: float, overrides: Optional[Dict[str, Any]] = None) -> AccountEquity:
        if self.account_fn is not None:
            acct = self.account_fn(now_ts)
        else:
            mode = "simulated"
            if self.stream_fn is not None:
                try:
                    mode = self.stream_fn().market.mode
                except Exception:  # noqa: BLE001
                    mode = "simulated"
            from proposals.account import AccountAdapter      # the Stage 5.9 read-only snapshot (4.6A view / local paper / hypothetical)
            snap = AccountAdapter(path=self.store.path).snapshot("", mode, now_ts)
            acct = AccountEquity(equity=snap.equity, source=snap.equity_source, as_of=snap.equity_as_of, age_s=snap.equity_age_s, mode=mode,
                                 adjustments="NONE_POSSIBLE" if snap.equity_source in NO_CASH_FLOW_SOURCES else "UNAVAILABLE", notes=list(snap.notes))
        ov = overrides if overrides is not None else self.overrides
        if ov:
            acct = acct.model_copy(update={k: v for k, v in ov.items() if k in AccountEquity.model_fields})
            acct.notes.append("test overrides applied: " + ", ".join(sorted(k for k in ov if k in AccountEquity.model_fields)))
        return acct

    # ---------------------------------------------------------------- the gate -------------------------------------------------
    def evaluate(self, trigger: str = "API", now: Optional[datetime] = None, *, persist: bool = True, overrides: Optional[Dict[str, Any]] = None, actor: str = "system",
                 state: Optional[SafetyStateRecord] = None, action_event: Optional[Dict[str, Any]] = None) -> SafetyView:
        now = now or self.clock()
        today = trading_date(now).isoformat()
        try:
            view = self._evaluate(trigger, now, today, persist, overrides, actor, state, action_event)
        except Exception as exc:  # noqa: BLE001 - a failing evaluator never reports TRADING_ALLOWED
            log.error("execution safety evaluation failed (%s: %s)", type(exc).__name__, exc)
            code = R_PERSISTENCE if isinstance(exc, _PersistError) else R_EVALUATOR
            view = SafetyView(state="UNKNOWN", allowed_new_entry=False, halted=True, reason_codes=[code], reason_text=REASON_TEXT[code], evaluated_at=_iso(now), trigger=trigger,
                              persisted=False, error=f"{type(exc).__name__}: {exc}"[:500], daily_loss=DailyLoss(trading_date=today))
            try:
                self.store.record_error(now, trigger, view.error or "", list(CONTROLS))
            except Exception as exc2:  # noqa: BLE001 - the database itself failed: say so, never pretend
                log.error("execution safety error could not be recorded (%s)", type(exc2).__name__)
        self._last = view
        for fn in list(self.listeners):
            try:
                fn({"type": "safety", "trigger": trigger, **view.model_dump()})
            except Exception as exc:  # noqa: BLE001
                log.warning("safety listener failed (%s)", type(exc).__name__)
        return view

    def _evaluate(self, trigger: str, now: datetime, today: str, persist: bool, overrides: Optional[Dict[str, Any]], actor: str,
                  state: Optional[SafetyStateRecord] = None, action_event: Optional[Dict[str, Any]] = None) -> SafetyView:
        st = state if state is not None else self.store.state()
        prev = self.store.last_evaluation()
        prev_state = _state_of(prev)
        events: List[Dict[str, Any]] = [dict(action_event)] if action_event else []
        if st.daily_halt_active and st.daily_halt_trading_date != today:                           # day rollover: only the daily latch resets
            events.append({"kind": "DAILY_RESET", "actor": "system", "reason": R_NEW_DAY, "trading_date": today, "from_state": prev_state,
                           "input": {"previous_trading_date": st.daily_halt_trading_date, "previous_loss_pct": st.daily_halt_loss_pct}})
            st = st.model_copy(update={"daily_halt_active": False, "daily_halt_trading_date": None, "daily_halt_triggered_at": None, "daily_halt_loss_pct": None, "daily_halt_reason": None})
        acct = self.account(now.timestamp(), overrides)
        baseline = self.store.baseline(today)
        new_baseline: Optional[Baseline] = None
        if baseline is None and persist:                                                                   # only a persisted evaluation may create the day's baseline
            cur = D(acct.equity)
            fresh = acct.source != "ALPACA_PAPER_VIEW" or (acct.age_s is not None and acct.age_s <= self.max_age_s)
            if cur is not None and cur > 0 and acct.source != "NOT_AVAILABLE" and fresh:
                new_baseline = Baseline(trading_date=today, starting_equity=s(money(cur)), baseline_created_at=_iso(now), source=acct.source, adjustments=acct.adjustments,
                                        equity_as_of=acct.as_of, note="first usable equity observed for this trading date")
                baseline = new_baseline
                events.append({"kind": "BASELINE_CREATED", "actor": "system", "reason": acct.source, "trading_date": today, "input": new_baseline.model_dump()})
        daily = daily_loss(baseline, acct, self.threshold_pct, max_age_s=self.max_age_s, accept_unadjusted=self.accept_unadjusted, latched=st.daily_halt_active,
                           latched_trading_date=st.daily_halt_trading_date, triggered_at=st.daily_halt_triggered_at, triggered_loss_pct=st.daily_halt_loss_pct, today=today)
        if daily.status == "TRIGGERED" and not st.daily_halt_active:                               # latch for the day; equity may recover, the halt does not
            st = st.model_copy(update={"daily_halt_active": True, "daily_halt_trading_date": today, "daily_halt_triggered_at": _iso(now), "daily_halt_loss_pct": daily.daily_loss_pct,
                                       "daily_halt_reason": R_DAILY})
            daily = daily.model_copy(update={"latched": True, "triggered_at": _iso(now), "triggered_loss_pct": daily.daily_loss_pct})
            events.append({"kind": "DAILY_HALT_TRIGGERED", "actor": "system", "reason": R_DAILY, "trading_date": today, "from_state": prev_state, "to_state": "HALTED",
                           "input": daily.model_dump()})
        kill = KillSwitch(active=st.kill_active, activated_at=st.kill_activated_at, activated_by=st.kill_activated_by, reason=st.kill_reason)
        pause = Pause(active=st.pause_active, activated_at=st.pause_activated_at, activated_by=st.pause_activated_by, reason=st.pause_reason)
        state, allowed, codes, headline = decide(kill, pause, daily)
        for e in events:
            e["to_state"] = e.get("to_state") or state
        ctl = control_rows(kill, pause, daily, baseline)
        icon, label = icon_label(state)
        last_at = _parse(prev["evaluated_at"]) if prev else None
        within = (now - last_at).total_seconds() if last_at else None
        fresh_acct = None if acct.age_s is None and acct.source == "ALPACA_PAPER_VIEW" else (acct.source != "ALPACA_PAPER_VIEW" or acct.age_s <= self.max_age_s)
        view = SafetyView(state=state, state_label=label, icon=icon, allowed_new_entry=allowed, halted=state in ("HALTED", "UNKNOWN"), reason_codes=codes, reason_text=headline,
                          daily_loss=daily, kill_switch=kill, no_new_entries=pause, freshness=Freshness(account_fresh=fresh_acct, account_age_s=acct.age_s, max_age_s=self.max_age_s,
                          evaluated_within_s=within), controls=ctl, evaluated_at=_iso(now), trigger=trigger, persisted=False)
        if persist:
            changed = bool(events) or prev is None or action_event is not None or prev.get("state") != state or prev.get("allowed_new_entry") != allowed or list(prev.get("reason_codes") or []) != codes
            heartbeat_due = trigger != "API" and (within is None or within >= self.heartbeat_s)       # a plain read never writes a heartbeat row or touches liveness
            if trigger == "API" and not changed:
                return view                                                                           # nothing to persist: reads leave the database alone
            evaluation = None
            if changed or heartbeat_due:
                evaluation = {"trigger": trigger, "state": state, "allowed_new_entry": allowed, "reason_codes": codes, "daily_loss": daily.model_dump(), "kill_switch": kill.model_dump(),
                              "no_new_entries": pause.model_dump(), "freshness": view.freshness.model_dump(),
                              "input": {"account": acct.model_dump(), "threshold_pct": s(D(self.threshold_pct)), "accept_unadjusted": self.accept_unadjusted, "trading_date": today,
                                        "owner": self.owner, "actor": actor, "baseline": baseline.model_dump() if baseline else None}}
            try:
                self.store.commit(now, state=st, events=events, evaluation=evaluation, controls=ctl, new_baseline=new_baseline)
            except Exception as exc:  # noqa: BLE001
                raise _PersistError(f"{type(exc).__name__}: {exc}") from exc
            view = view.model_copy(update={"persisted": True})
        return view

    def current(self, max_age_s: float = 5.0, now: Optional[datetime] = None) -> SafetyView:
        """The latest evaluation when it is a few seconds old, else a fresh one (reads that must not re-evaluate every call)."""
        now = now or self.clock()
        last = self._last
        if last is not None and last.error is None:
            at = _parse(last.evaluated_at)
            if at is not None and 0 <= (now - at).total_seconds() <= max_age_s:
                return last
        return self.evaluate("API", now)

    # ---------------------------------------------------------------- user actions (safety state only) -----------------------
    def _action(self, kind: str, actor: str, reason: Optional[str], now: datetime, update: Dict[str, Any]) -> SafetyView:
        st = self.store.state()
        flag = "kill_active" if kind.startswith("KILL") else "pause_active"
        if bool(getattr(st, flag)) == bool(update[flag]):                                                 # already in that state: no event, just a fresh evaluation
            return self.evaluate("ACTION", now, actor=actor)
        new = st.model_copy(update=update)
        event = {"kind": kind, "actor": actor, "reason": reason, "trading_date": trading_date(now).isoformat(), "from_state": _state_of(self.store.last_evaluation()),
                 "input": {k: update[k] for k in update}}
        return self.evaluate("ACTION", now, actor=actor, state=new, action_event=event)

    def pause(self, reason: Optional[str] = None, actor: str = "user", now: Optional[datetime] = None) -> SafetyView:
        now = now or self.clock()
        return self._action("PAUSE_ON", actor, reason, now, {"pause_active": True, "pause_activated_at": _iso(now), "pause_activated_by": actor, "pause_reason": reason})

    def resume(self, reason: Optional[str] = None, actor: str = "user", now: Optional[datetime] = None) -> SafetyView:
        """Clears the no-new-entries pause ONLY. A kill switch is never cleared here."""
        now = now or self.clock()
        return self._action("PAUSE_OFF", actor, reason, now, {"pause_active": False, "pause_activated_at": None, "pause_activated_by": None, "pause_reason": None})

    def emergency_stop(self, reason: Optional[str] = None, actor: str = "user", now: Optional[datetime] = None) -> SafetyView:
        now = now or self.clock()
        return self._action("KILL_ON", actor, reason, now, {"kill_active": True, "kill_activated_at": _iso(now), "kill_activated_by": actor, "kill_reason": reason})

    def clear_emergency_stop(self, reason: Optional[str] = None, actor: str = "user", now: Optional[datetime] = None) -> SafetyView:
        """A distinct, explicit action: nothing else clears the kill switch."""
        now = now or self.clock()
        return self._action("KILL_OFF", actor, reason, now, {"kill_active": False, "kill_activated_at": None, "kill_activated_by": None, "kill_reason": None})

    # ---------------------------------------------------------------- startup -------------------------------------------------
    def startup(self, now: Optional[datetime] = None) -> SafetyView:
        """Applies the startup policy (KEEP leaves the persisted state alone; HALT_ON_START activates the kill switch once) and evaluates."""
        now = now or self.clock()
        policy = self.startup_policy if self.startup_policy in STARTUP_POLICIES else "KEEP"
        if policy == "HALT_ON_START" and not self.store.state().kill_active:
            return self.emergency_stop(R_STARTUP_POLICY, actor="system", now=now)
        return self.evaluate("STARTUP", now)

    # ---------------------------------------------------------------- reads for the API --------------------------------------
    def overview(self, now: Optional[datetime] = None, *, evaluate: bool = True) -> dict:
        now = now or self.clock()
        view = self.evaluate("API", now) if evaluate else (self._last or self.evaluate("API", now))
        return {"version": VERSION, **view.model_dump(), "events": self.store.events(20), "controls": self.store.controls(), "baselines": self.store.baselines(5),
                "threshold_pct": s(D(self.threshold_pct)), "startup_policy": self.startup_policy}


class _PersistError(RuntimeError):
    pass


# ---------------------------------------------------------------- module-level entry (DESIGN_591 §7) -----------------------------

def default_service() -> ExecutionSafetyService:
    from api.routes.trader_view import stream
    return stream().safety


TEST_ENVS = ("test", "browser_test")


def startup_if_enabled() -> Optional[SafetyView]:
    """Applies the startup policy and records a STARTUP evaluation once per process — never in a test / harness process."""
    if os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS:
        return None
    return default_service().startup()


def evaluate_execution_safety(trigger: str = "API", now: Optional[datetime] = None, **kw) -> SafetyView:
    """The one gate call a future execution layer makes before any new entry. `allowed_new_entry` false = do not enter."""
    return default_service().evaluate(trigger, now, **kw)
