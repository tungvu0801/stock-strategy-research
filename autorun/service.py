"""
autorun/service.py - Stage 6.10A AutoRunService: WHEN Paper Auto may be armed (DESIGN_610A_FORWARD_PAPER_AUTORUN.md). Its only actions are the
existing ExecutionService.disable_auto (new entries stop; monitoring and exits of existing bot positions continue) and - only under an
explicitly enabled SCHEDULED_ARM - the existing ExecutionService.enable_auto with all of its own refusals. No order, no broker write, no
strategy or risk value is touched here.

  startup_step(...)   the server lifespan, BEFORE the executor and the observer threads exist: take the single-instance lease, then demote a
                      persisted PAPER_AUTO to MONITOR_ONLY (MANUAL_ARM: a process start never keeps Paper Auto armed). Fails CLOSED: when the
                      lease is held by another instance or the demotion cannot be written, it raises and the lifespan starts neither the
                      executor nor the observer; the supervisor retries and starts them only once this process owns the lease and the demotion
                      is written.
  tick(...)           the supervisor (autorun/scheduler.py): renew the lease FIRST, gather read-only facts, the preflight (persisted on change and
                      hourly), the automatic disarms while Paper Auto is on, the opt-in scheduled arm (one attempt per trading date).
A process that does not own the lease is PASSIVE: no executor, no observer, no disarm and no arm of its own - it reports the duplicate.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import config
from autorun import (ACTOR_POLICY, ACTOR_SCHEDULED_ARM, INSTANCE_LEASE, INSTANCE_LEASE_TTL_MIN, LOCK_WAIT_S, READY, SCHEDULED_ARM, STARTUP_DEMOTION_ATTEMPTS,
                     STARTUP_DEMOTION_BACKOFF_S, STARTUP_DISARM, STARTUP_REASON, instance_id)
from autorun import policy as POL
from autorun.store import AutoRunStore

log = logging.getLogger(__name__)
UTC = timezone.utc
TEST_ENVS = ("test", "browser_test")
PREFLIGHT_PERSIST_S = 3600


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _utc(dt: Optional[datetime]) -> datetime:
    d = dt or datetime.now(UTC)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class StartupRefused(RuntimeError):
    def __init__(self, code: str, text: str):
        super().__init__(f"{code}: {text}")
        self.code, self.text = code, text


# the startup step's outcome, shared with the supervisor of this process
STARTUP: Dict[str, Any] = {"pending": False, "code": None, "text": None, "attempts": 0, "demoted": None, "at": None, "components_started": False, "passive": False,
                           "lease_taken": False, "owned_since": None}


def _execution():
    from api.routes.trader_view import stream
    return stream().execution


def _lease_store():
    from forward.automation import default_store
    return default_store()


def _day_of(now: datetime) -> Optional[str]:
    from forward_capture.capture import trading_date
    return trading_date(_iso(now))


def demote(x, now: datetime, reason: str, *, attempts: int = STARTUP_DEMOTION_ATTEMPTS, backoff_s: float = STARTUP_DEMOTION_BACKOFF_S,
           sleep: Callable[[float], None] = time.sleep) -> bool:
    """PAPER_AUTO -> MONITOR_ONLY through the executor's own disable_auto; success is judged by the PERSISTED mode (a failing status view after the
    write is still a written demotion)."""
    for n in range(max(1, attempts)):
        try:
            x.disable_auto(actor=ACTOR_POLICY, reason=reason[:500], now=now)
        except Exception as exc:  # noqa: BLE001 - judged by the persisted mode below
            log.warning("auto-run: Paper Auto demotion attempt %d failed (%s)", n + 1, type(exc).__name__)
        try:
            if x.store.mode()["mode"] != "PAPER_AUTO":
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-run: mode unreadable after the demotion attempt (%s)", type(exc).__name__)
        if n + 1 < attempts:
            sleep(backoff_s)
    return False


def startup_step(x, lease_store, store: AutoRunStore, now: Optional[datetime] = None, *, sleep: Callable[[float], None] = time.sleep,
                 take_lease: bool = True) -> Dict[str, Any]:
    """§5 steps 1 and 9 before the executor starts: the single-instance guard, then MONITOR_ONLY. Raises StartupRefused (fail closed).
    take_lease=False (AUTORUN_SUPERVISOR_ENABLED=false): no long-held lease - nothing would renew it, so it could only block this process's own
    restart; the demotion still runs."""
    now = _utc(now)
    STARTUP.update(at=_iso(now), attempts=int(STARTUP.get("attempts") or 0) + 1)
    me = instance_id()
    if take_lease:
        try:
            held = bool(lease_store.acquire(me, now, ttl_min=INSTANCE_LEASE_TTL_MIN, name=INSTANCE_LEASE))
        except Exception as exc:  # noqa: BLE001
            STARTUP.update(pending=True, passive=True, code="LEASE_UNREADABLE", text=type(exc).__name__)
            log.warning("auto-run: single-instance lease unreadable (%s): the executor and the observer are not started", type(exc).__name__)
            raise StartupRefused("LEASE_UNREADABLE", type(exc).__name__) from None
        if not held:
            row = store.lease(INSTANCE_LEASE) or {}
            STARTUP.update(pending=True, passive=True, code="LEASE_HELD", text=f"instance lease held by {row.get('owner')} until {row.get('expires_at')}")
            log.warning("auto-run: another Stock Agent instance holds this database (%s until %s): this process stays passive (no executor, no observer)",
                        row.get("owner"), row.get("expires_at"))
            raise StartupRefused("LEASE_HELD", STARTUP["text"])
        STARTUP.update(lease_taken=True, owned_since=_iso(now))                # released at shutdown, after the executor stopped
    before = x.store.mode()
    demoted = False
    if before.get("mode") == "PAPER_AUTO":
        reason = f"{STARTUP_REASON} (pid {os.getpid()}, instance {me}): Paper Auto is never kept armed across a restart; a person arms it again"
        if not demote(x, now, reason, sleep=sleep):
            STARTUP.update(pending=True, passive=False, code="DEMOTION_FAILED", text="Paper Auto could not be set to Monitor only")
            log.warning("auto-run: Paper Auto could not be demoted at startup: the executor and the observer are not started (fail closed)")
            raise StartupRefused("DEMOTION_FAILED", STARTUP["text"])
        demoted = True
        log.warning("auto-run: Paper Auto was armed before this process started; it is now Monitor only (MANUAL_ARM)")
    STARTUP.update(pending=False, passive=False, code=None, text=None, demoted=demoted)
    try:
        td = _day_of(now)
        store.add_event(now, "STARTUP", code=STARTUP_DISARM if demoted else "MONITOR_ONLY", actor=ACTOR_POLICY,
                        detail={"mode_before": before.get("mode"), "mode_changed_at": before.get("changed_at"), "demoted": demoted, "pid": os.getpid(), "instance": me},
                        trading_date=td, owner=me)
        if demoted:
            store.update_day(td, now, last_disarmed_at=_iso(now), last_disarmed_by="startup", last_disarm_code=STARTUP_DISARM)
    except Exception as exc:  # noqa: BLE001 - the record is evidence, not a safety step: the demotion above is already persisted
        log.warning("auto-run: startup record not saved (%s)", type(exc).__name__)
    return {"result": "OK", "demoted": demoted, "mode_before": before.get("mode"), "instance": me}


def in_test_process() -> bool:
    """A test / harness process: STOCK_AGENT_ENV test or browser_test, or pytest imported IN this process. Deliberately not PYTEST_CURRENT_TEST:
    that variable leaks into the real server processes the Windows runner tests start, which must behave exactly like production."""
    return os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS or "pytest" in sys.modules


def startup_if_enabled() -> Dict[str, Any]:
    """The lifespan hook. Gated ONLY on a test process (never on a configuration flag): MANUAL_ARM cannot be configured away."""
    if in_test_process():
        return {"result": "SKIPPED_TEST_PROCESS"}
    return startup_step(_execution(), _lease_store(), AutoRunStore(), take_lease=POL.settings_from(config)["supervisor_enabled"])


def executor_running() -> bool:
    """This process runs the executor loop - or cannot run one (EXECUTION_SCHEDULER_ENABLED=false, a test process): nothing to (re)start then."""
    if not getattr(config, "EXECUTION_SCHEDULER_ENABLED", True) or in_test_process():
        return True
    try:
        from execution.scheduler import status
        return bool(status().get("running"))
    except Exception:  # noqa: BLE001
        return False


def mark_started() -> None:
    """The lifespan started the executor and the observer itself (a normal start): the supervisor's first tick is not a takeover."""
    STARTUP["components_started"] = executor_running()


def start_components() -> Dict[str, bool]:
    """The executor and observer threads (their own enable flags and test gates apply) - only after this process owns the lease."""
    out = {}
    try:
        from execution.scheduler import start_if_enabled as exec_start
        out["executor"] = bool(exec_start())
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-run: executor could not start (%s)", type(exc).__name__)
        out["executor"] = False
    try:
        from forward_validation.scheduler import start_if_enabled as fv_start
        out["observer"] = bool(fv_start())
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-run: forward-validation observer could not start (%s)", type(exc).__name__)
        out["observer"] = False
    STARTUP["components_started"] = executor_running()                         # a failed start is retried at the next tick
    return out


def stop_components() -> None:
    """A process that lost the lease to a live instance stops its executor and observer (the lease owner runs them)."""
    for mod in ("forward_validation.scheduler", "execution.scheduler"):
        try:
            __import__(mod, fromlist=["shutdown"]).shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-run: %s did not stop (%s)", mod, type(exc).__name__)
    STARTUP["components_started"] = False


class AutoRunService:
    def __init__(self, stream_fn: Callable[[], object], store: Optional[AutoRunStore] = None, *, lease_store_fn: Callable[[], object] = _lease_store,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), process_start: Optional[datetime] = None,
                 start_fn: Callable[[], Dict[str, bool]] = start_components, stop_fn: Callable[[], None] = stop_components):
        self.stream_fn, self.clock = stream_fn, clock
        self.store = store or AutoRunStore()
        self.lease_store_fn = lease_store_fn
        self.process_start = process_start or datetime.now(UTC)
        self.start_fn, self.stop_fn = start_fn, stop_fn
        self.mem: Dict[str, Any] = {}
        self.lease_seen: Optional[Dict[str, Any]] = None
        self.last_preflight: Optional[Dict[str, Any]] = None
        self._pf_sig: Optional[str] = None
        self._pf_at: Optional[datetime] = None
        self.last_facts: Optional[Dict[str, Any]] = None
        self.last_tick: Optional[Dict[str, Any]] = None
        self.ticks = 0
        self.robinhood_primed = False
        self._yield_noted = False

    # ---------------------------------------------------------------- the single-instance lease ---------------------------------------
    def renew(self, now: datetime) -> Dict[str, Any]:
        me = instance_id()
        if STARTUP["passive"] and not getattr(config, "EXECUTION_SCHEDULER_ENABLED", True):
            return self._observe_lease(now)                                    # a process that cannot run an executor never takes a database over
        try:
            held = bool(self.lease_store_fn().acquire(me, now, ttl_min=INSTANCE_LEASE_TTL_MIN, name=INSTANCE_LEASE))
        except Exception as exc:  # noqa: BLE001 - unreadable: not held this tick (no action depends on holding it but arming)
            if self.mem.get("last_mode") == "PAPER_AUTO":
                self.mem.setdefault("db_failure", {"since": _iso(now), "error": type(exc).__name__})
            return {"held_by_self": False, "owner": None, "expires_at": None, "other_renewing": False, "other_static": False, "error": type(exc).__name__}
        if held:
            self.lease_seen = None
            STARTUP["lease_taken"] = True
            return {"held_by_self": True, "owner": me, "expires_at": _iso(now + timedelta(minutes=INSTANCE_LEASE_TTL_MIN)), "other_renewing": False, "other_static": False}
        return self._observe_lease(now)

    def _observe_lease(self, now: datetime) -> Dict[str, Any]:
        """Another owner's lease row: renewing (expires_at advanced since the last read: alive) or static (expiring)."""
        row = self.store.lease(INSTANCE_LEASE) or {}
        prev = self.lease_seen
        renewing = bool(prev and prev.get("owner") == row.get("owner") and str(row.get("expires_at") or "") > str(prev.get("expires_at") or ""))
        self.lease_seen = {"owner": row.get("owner"), "expires_at": row.get("expires_at"), "first_seen_at": (prev or {}).get("first_seen_at") or _iso(now)} \
            if not prev or prev.get("owner") != row.get("owner") else {**prev, "expires_at": row.get("expires_at")}
        return {"held_by_self": False, "owner": row.get("owner"), "expires_at": row.get("expires_at"), "other_renewing": renewing, "other_static": not renewing}

    def release(self) -> None:
        try:
            self.lease_store_fn().release(instance_id(), name=INSTANCE_LEASE)
        except Exception as exc:  # noqa: BLE001 - the lease expires by itself
            log.warning("auto-run: lease not released (%s)", type(exc).__name__)

    # ---------------------------------------------------------------- one supervisor tick ----------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _utc(now or self.clock())
        self.ticks += 1
        out: Dict[str, Any] = {"at": _iso(now), "result": "OK"}
        lease = self.renew(now)                                          # FIRST: before anything that could wait
        out["lease"] = "HELD" if lease["held_by_self"] else ("DUPLICATE" if lease.get("other_renewing") else "WAITING" if lease.get("owner") else "UNREADABLE")
        x = self.stream_fn().execution
        if lease["held_by_self"] and (STARTUP["pending"] or not STARTUP["components_started"]):
            out["startup"] = self._takeover(x, now)
        if lease["held_by_self"]:
            self._yield_noted = False
        f = self.facts(x, now, lease)
        if not lease["held_by_self"] and lease.get("other_renewing") and STARTUP["components_started"] and not STARTUP["passive"]:
            out["lost_lease"] = self._yield(x, now, lease, f)
            f["passive"] = bool(STARTUP["passive"])
        self.last_facts = f
        pf = POL.preflight(f)
        self._persist_preflight(now, pf, f)
        out["preflight"] = pf["status"]
        if not STARTUP["passive"] and lease["held_by_self"]:
            if not self.robinhood_primed:
                self._prime_context(now)
            out["disarm"] = self._disarm_step(x, now, f)
            out["scheduled_arm"] = self._arm_step(x, now, f, pf)
        self.last_tick = out
        return out

    def facts(self, x, now: datetime, lease: Dict[str, Any]) -> Dict[str, Any]:
        from autorun.facts import gather
        owned = POL._parse(STARTUP.get("owned_since"))
        f = gather(x, self.stream_fn(), self.store, self.mem, now, lease=lease, process_start=self.process_start, owned_since=owned)
        f["passive"] = bool(STARTUP["passive"])
        return f

    def _takeover(self, x, now: datetime) -> Dict[str, Any]:
        """This process owns the lease now (a startup that was refused, or a predecessor's lease that expired): demote, then start the threads."""
        if x.store.mode().get("mode") == "PAPER_AUTO":
            reason = f"{STARTUP_REASON} (pid {os.getpid()}, instance {instance_id()}, took the instance lease over): a person arms it again"
            if not demote(x, now, reason):
                STARTUP.update(pending=True, passive=False, code="DEMOTION_FAILED", text="Paper Auto could not be set to Monitor only")
                return {"result": "DEMOTION_FAILED"}
            try:
                self.store.update_day(_day_of(now), now, last_disarmed_at=_iso(now), last_disarmed_by="startup", last_disarm_code=STARTUP_DISARM)
            except Exception as exc:  # noqa: BLE001
                log.warning("auto-run: takeover record not saved (%s)", type(exc).__name__)
        STARTUP.update(pending=False, passive=False, code=None, text=None, owned_since=_iso(now))
        started = self.start_fn()
        STARTUP["components_started"] = bool((started or {}).get("executor")) or executor_running()
        self._event(now, "TAKEOVER", "COMPONENTS_STARTED", ACTOR_POLICY, {"started": started})
        return {"result": "STARTED", "started": started}

    def _yield(self, x, now: datetime, lease: Dict[str, Any], f: Dict[str, Any]) -> Dict[str, Any]:
        """Another live instance renews the lease while this one runs an executor. Paper Auto is disarmed at once (DUPLICATE_INSTANCE). This
        process's executor and observer stop only once the other instance demonstrably runs an executor (its cycles on this database): until
        then exits keep running here - a lost lease never strands them."""
        disarmed = False
        if (f.get("mode") or {}).get("mode") == "PAPER_AUTO":
            disarmed = self._disarm(x, now, [("DUPLICATE_INSTANCE", f"another live instance ({lease.get('owner')}) owns this database")])["result"] == "DISARMED"
        other_runs = bool(f.get("foreign_executor"))
        if other_runs:
            self.stop_fn()
            STARTUP.update(pending=True, passive=True, code="LEASE_LOST", text=f"instance lease renewed by {lease.get('owner')}", components_started=False)
        if other_runs or not self._yield_noted:
            self._event(now, "LEASE_LOST", "DUPLICATE_INSTANCE", ACTOR_POLICY, {"owner": lease.get("owner"), "disarmed": disarmed, "stopped": other_runs})
            self._yield_noted = True
        return {"result": "PASSIVE" if other_runs else "KEPT_RUNNING", "disarmed": disarmed}

    def _prime_context(self, now: datetime) -> None:
        """§5 step 7: one read-only Robinhood read at startup when ROBINHOOD_PORTFOLIO_ENABLED (the source's own TTL applies). Context only."""
        self.robinhood_primed = True
        if not getattr(config, "ROBINHOOD_PORTFOLIO_ENABLED", False):
            return
        try:
            fn = getattr(self.stream_fn(), "unified_portfolio", None)
            u = fn(refresh=True) if callable(fn) else None
            self._event(now, "STARTUP_CONTEXT", (u or {}).get("state") or "UNAVAILABLE", ACTOR_POLICY,
                        {"sources": [{"source": s.get("source"), "state": s.get("state")} for s in ((u or {}).get("sources") or [])]})
        except Exception as exc:  # noqa: BLE001 - context only
            self._event(now, "STARTUP_CONTEXT", "UNAVAILABLE", ACTOR_POLICY, {"error": type(exc).__name__})

    def _persist_preflight(self, now: datetime, pf: Dict[str, Any], f: Dict[str, Any]) -> None:
        self.last_preflight = {**pf, "at": _iso(now), "policy": f["settings"]["effective_policy"]}
        sig = pf["status"] + "|" + ",".join(f"{c['id']}:{c['status']}" for c in pf["checks"] if c["status"] in ("FAIL", "WARN"))
        if sig == self._pf_sig and self._pf_at is not None and (now - self._pf_at).total_seconds() < PREFLIGHT_PERSIST_S:
            return
        try:
            self.store.add_preflight(now, "STARTUP" if self._pf_sig is None else "SUPERVISOR", pf, f["settings"]["effective_policy"], owner=instance_id())
            self._pf_sig, self._pf_at = sig, now
        except Exception as exc:  # noqa: BLE001 - kept in memory; retried at the next tick
            log.warning("auto-run: preflight not saved (%s)", type(exc).__name__)

    def _event(self, now: datetime, kind: str, code: Optional[str], actor: str, detail: Dict[str, Any]) -> None:
        try:
            self.store.add_event(now, kind, code=code, actor=actor, detail=detail, trading_date=_day_of(now), owner=instance_id())
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-run: event %s not saved (%s)", kind, type(exc).__name__)

    # ---------------------------------------------------------------- automatic disarm -------------------------------------------------
    def _disarm_step(self, x, now: datetime, f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if (f.get("mode") or {}).get("mode") != "PAPER_AUTO":
            return None
        conds = POL.disarm_conditions(f, self.mem, now)
        if not conds:
            return None
        res = self._disarm(x, now, conds, regather=lambda: self.facts(x, now, f.get("lease") or {}))
        if res["result"] in ("DISARMED", "ALREADY_OFF"):
            self.mem.pop("db_failure", None)
        return res

    def _disarm(self, x, now: datetime, conds: List[tuple], regather: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
        """New entries stop at once (the executor re-reads the mode before every POST); exits keep running in Monitor only (needs_broker).
        With `regather`, the conditions are checked again UNDER the executor lock: one that cleared while a cycle ran never disarms."""
        if not x._lock.acquire(timeout=LOCK_WAIT_S):                     # the executor's own lock: never longer than LOCK_WAIT_S (retried next tick)
            self._event(now, "AUTO_DISARM_DEFERRED", conds[0][0], ACTOR_POLICY, {"codes": [c for c, _ in conds], "why": "a cycle holds the executor lock"})
            return {"result": "DEFERRED", "codes": [c for c, _ in conds]}
        try:
            if x.store.mode().get("mode") != "PAPER_AUTO":
                return {"result": "ALREADY_OFF"}
            if regather is not None:
                conds = POL.disarm_conditions(regather(), self.mem, now)
                if not conds:
                    return {"result": "CLEARED"}
            reason = "auto-disarm: " + "; ".join(f"{c}: {t}" for c, t in conds)
            ok = demote(x, now, reason, attempts=1)
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.warning("auto-run: disarm failed (%s)", type(exc).__name__)
        finally:
            x._lock.release()
        codes = [c for c, _ in conds]
        if ok:
            log.warning("auto-run: Paper Auto disarmed (%s)", ", ".join(codes))
            self._event(now, "AUTO_DISARM", codes[0], ACTOR_POLICY, {"codes": codes, "texts": [t for _, t in conds]})
            try:
                self.store.update_day(_day_of(now), now, last_disarmed_at=_iso(now), last_disarmed_by=ACTOR_POLICY, last_disarm_code=codes[0])
            except Exception as exc:  # noqa: BLE001
                log.warning("auto-run: disarm day record not saved (%s)", type(exc).__name__)
            return {"result": "DISARMED", "codes": codes}
        self._event(now, "AUTO_DISARM_FAILED", codes[0], ACTOR_POLICY, {"codes": codes})
        return {"result": "FAILED", "codes": codes}

    # ---------------------------------------------------------------- the opt-in scheduled arm -----------------------------------------
    def _arm_step(self, x, now: datetime, f: Dict[str, Any], pf: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if f["settings"]["effective_policy"] != SCHEDULED_ARM:
            return None
        td = f.get("trading_date")
        day = self.store.day(td) if td else None
        chk = POL.scheduled_arm_check(f, day, now)
        if pf["status"] != READY:
            chk = {"ok": False, "reasons": chk["reasons"] + [f"preflight {pf['status']} ({', '.join(pf['blocking'] + pf['degrading'])})"]}
        if not chk["ok"]:
            return {"result": "NOT_ELIGIBLE", "reasons": chk["reasons"]}
        if not x._lock.acquire(timeout=LOCK_WAIT_S):
            return {"result": "LOCK_BUSY"}                               # nothing written: the attempt is not consumed
        try:
            again = POL.scheduled_arm_check(self.facts(x, now, f.get("lease") or {}), day, now)   # the facts again, under the lock (a kill, an order ...)
            if not again["ok"]:
                return {"result": "NOT_ELIGIBLE_UNDER_LOCK", "reasons": again["reasons"]}
            seen = (f.get("mode") or {}).get("event_id")
            latest = (x.store.mode_events(1) or [{}])[0].get("event_id")
            self.store.update_day(td, now, scheduled_arm_at=_iso(now), scheduled_arm_result="ATTEMPTING")   # write-ahead: ONE attempt per trading date
            if latest != seen or x.store.mode().get("mode") == "PAPER_AUTO":
                result = "MODE_CHANGED"
            else:
                try:
                    x.enable_auto(actor=ACTOR_SCHEDULED_ARM, reason=f"SCHEDULED_ARM: all conditions met at {_iso(now)} (preflight READY)")
                    result = "ARMED"
                except Exception as exc:  # noqa: BLE001 - enable_auto's own refusals (paper proof, account, safety, reconciliation) are final for the day
                    result = f"REFUSED:{getattr(exc, 'code', type(exc).__name__)}"
        finally:
            x._lock.release()
        fields = {"scheduled_arm_result": result}
        if result == "ARMED" and not (day or {}).get("first_armed_at"):
            fields.update(first_armed_at=_iso(now), first_armed_by=ACTOR_SCHEDULED_ARM)
        try:
            self.store.update_day(td, now, **fields)
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-run: scheduled-arm result not saved (%s)", type(exc).__name__)
        self._event(now, "SCHEDULED_ARM", result, ACTOR_SCHEDULED_ARM, {"reasons_checked": "all", "preflight": pf["status"]})
        return {"result": result}


def view_state() -> Dict[str, Any]:
    """The startup outcome of this process (display only)."""
    return {k: STARTUP.get(k) for k in ("pending", "passive", "code", "text", "demoted", "at", "components_started", "owned_since")}

