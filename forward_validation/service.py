"""
forward_validation/service.py - the Stage 6.1 observer and the descriptive statistics (DESIGN_61 sections 3-10).

  observe()              one observer tick: a sample of what the bot, the gate, the account data, the cash-flow data and the reconciliation look
                         like; price marks of open paper positions; the session record of the New York trading date (opened on the first
                         observation, closed after the close); NOT_RUN back-fill; restart detection; the attention monitor and the early stop
  funnel() / rates()     the forward lifecycle counts of a trading date from the PERSISTED events of Stages 5.8 - 6.0 (never inferred)
  order_reliability(), reconciliation(), safety_fires(), freshness(), broker_health(), restarts_view()
  closed_trades()        every closed paper position with slippage, P/L, sampled MFE / MAE, exit reason and setup attribution
  trace()                the end-to-end audit trail of one closed trade
  daily_summary()        the deterministic, rebuildable summary of one trading date; cumulative() the validation-wide one; overview() the API root
Everything is read from persisted rows; the only write outside the Stage 6.1 tables is the early stop (Paper Auto -> Monitor only).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from execution import EXIT_REASONS, EXIT_TEXT, INTENT_OPEN, POSITION_OPEN
from execution import exposure_gate                           # Stage 6.9.1: the versioned entry-controls record of each paper entry
from forward_validation import (ATTENTION_CODES, ATTENTION_TEXT, CONTROLS, CRITICAL_CODES, DISCLAIMER, EDGE_LABELS, FUNNEL, INSUFFICIENT_TEXT, MIN_TRADES_FOR_EDGE,
                                PERSISTENT_MISMATCH_SESSIONS, SESSION_LABELS, STAGE, TARGET_EVENTS, TARGET_SESSIONS, VERSION, frozen_hash)
from forward_validation import metrics as M
from forward_validation.store import ForwardValidationStore
from forward_capture.capture import ForwardCapture            # Stage 6.10A: the point-in-time forward dataset this observer records (instrumentation only)
from setups.market_hours import CLOSE_MINUTE, is_regular_session, session_date, to_et

log = logging.getLogger(__name__)
UTC = timezone.utc
LIVE_HOST_MARK = "PAPER host is allowed"                      # the Stage 6.0 transport's refusal text: any occurrence = a non-paper request was attempted
AMBIGUOUS_CLASSES = ("O2", "O7", "O8")
BLOCKING_FINDINGS = ("ORDER_MISSING_AT_BROKER", "UNKNOWN_INTERNAL_ORDER", "QTY_MISMATCH", "BROKER_FLAT_INTERNAL_OPEN", "INTERNAL_FLAT_BROKER_POSITION", "MULTIPLE_ORDERS",
                     "UNKNOWN_STATUS", "BROKER_UNAVAILABLE")
RESTART_GRACE_TICKS = 3


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def entry_controls_of(entry) -> Dict[str, Any]:
    """Stage 6.9.1: the entry-controls record of a paper entry. Intents created from Stage 6.9.1 on carry it (environment.entry_controls); an older
    intent has none and was decided, by construction, under entry_controls_v1 (no unified external exposure gate) - labelled so, never rewritten."""
    if entry is None:
        return {"version": None, "recorded": False, "unified_external_exposure": None, "note": "entry intent not found"}
    rec = (entry.environment or {}).get("entry_controls")
    if isinstance(rec, dict) and rec.get("version"):
        return {**rec, "recorded": True}
    return {"version": exposure_gate.PREVIOUS_VERSION, "recorded": False, "unified_external_exposure": {"enabled": False, "result": "NOT_PRESENT"},
            "note": "entered before Stage 6.9.1: no unified external exposure gate existed (no record was written then; none is added now)"}


def entry_controls_mix(trades: List[dict]) -> Dict[str, int]:
    """Closed trades per entry-control regime: entry_controls_v1 | entry_controls_v2 gate off | entry_controls_v2 gate on (never pooled silently)."""
    out: Dict[str, int] = {}
    for t in trades:
        ec = t.get("entry_controls") or {}
        g = ec.get("unified_external_exposure") or {}
        key = str(ec.get("version") or "unknown") + ("" if ec.get("version") != exposure_gate.VERSION else " gate on" if g.get("enabled") else " gate off")
        out[key] = out.get(key, 0) + 1
    return out


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _utc(dt: Optional[datetime]) -> datetime:
    d = dt or datetime.now(UTC)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def ny_window(trading_date: str) -> Tuple[str, str]:
    """[start, end) of a New York calendar day as UTC ISO strings (the persisted stamps are UTC ISO with +00:00; string order == time order)."""
    d = date.fromisoformat(trading_date)
    et = to_et(datetime(d.year, d.month, d.day, 12, tzinfo=UTC))
    tz = et.tzinfo if et is not None else UTC
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return _iso(start), _iso(end)


def weekdays_between(after: str, before: str) -> List[str]:
    """Weekdays strictly between two ISO dates (no exchange holiday calendar exists: holidays appear here too, documented)."""
    a, b = date.fromisoformat(after), date.fromisoformat(before)
    out, d = [], a + timedelta(days=1)
    while d < b:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _n(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


class ForwardValidationService:
    def __init__(self, stream_fn: Callable[[], object], store: Optional[ForwardValidationStore] = None, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 owner: Optional[str] = None, target_sessions: Optional[int] = None, target_events: Optional[int] = None, min_trades: Optional[int] = None,
                 early_stop_enabled: Optional[bool] = None):
        self.stream_fn = stream_fn
        self.store = store or ForwardValidationStore()
        self.clock = clock
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.target_sessions = int(target_sessions if target_sessions is not None else getattr(config, "FORWARD_VALIDATION_TARGET_SESSIONS", TARGET_SESSIONS))
        self.target_events = int(target_events if target_events is not None else getattr(config, "FORWARD_VALIDATION_TARGET_EVENTS", TARGET_EVENTS))
        self.min_trades = int(min_trades if min_trades is not None else getattr(config, "FORWARD_VALIDATION_MIN_TRADES", MIN_TRADES_FOR_EDGE))
        self.early_stop_enabled = bool(early_stop_enabled if early_stop_enabled is not None else getattr(config, "FORWARD_VALIDATION_EARLY_STOP", True))
        self.ticks = 0
        self.last_observation: Optional[dict] = None
        self._restart_pending: Optional[dict] = None
        self._restart_checked = False
        self._stopped_for: set = set()
        self.capture = ForwardCapture(self)                                       # Stage 6.10A: no I/O here; the executor hands it markers, observe() records them

    # ---------------------------------------------------------------- collaborators (through the Trader View stream) ------------------
    @property
    def execution(self):
        return self.stream_fn().execution

    def _safe(self, fn, default=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - an unreadable collaborator is recorded, never a crash of the observer
            log.warning("forward validation: %s unavailable (%s)", getattr(fn, "__name__", "read"), type(exc).__name__)
            return default

    def _touch(self, now: datetime, control: str, result: str, reason: str = "", error: Optional[str] = None) -> None:
        try:
            self.store.touch_control(control, now, result, reason, error)
        except Exception as exc:  # noqa: BLE001 - liveness bookkeeping never breaks the observer
            log.warning("forward validation control %s not saved (%s)", control, type(exc).__name__)

    # ================================================================ the observer tick =========================================
    def observe(self, now: Optional[datetime] = None, trigger: str = "SCHEDULED") -> dict:
        now = _utc(now or self.clock())
        self.ticks += 1
        out: Dict[str, Any] = {"at": _iso(now), "trigger": trigger, "result": "OK"}
        try:
            self.store.ensure()
            sample = self._snapshot(now)
            out["trading_date"] = sample["trading_date"]
            self.store.add_sample(sample)
            self.store.add_marks(self._marks(now, sample))
            out["backfilled"] = self._backfill_not_run(now, sample["trading_date"])        # weekdays between the last record and today: NOT_RUN
            out["session_status"] = self._record_session(now, sample)
            out["restart"] = self._detect_restart(now, sample)
            att = self.attention(now)
            out["attention"] = [a["code"] for a in att]
            out["early_stop"] = self._early_stop(now, att, sample)
            self._touch(now, "observer", "PASS", f"tick {self.ticks}; {len(att)} attention condition(s)")
        except Exception as exc:  # noqa: BLE001 - the observer never raises into its loop; the failure is persisted
            out.update(result="ERROR", error=f"{type(exc).__name__}: {exc}"[:300])
            log.warning("forward validation observation failed: %s", type(exc).__name__)
            try:
                self.store.add_event(now, "OBSERVER_ERROR", type(exc).__name__, {"error": out["error"]}, owner=self.owner)
                self._touch(now, "observer", "UNKNOWN", "observation failed", out["error"])
            except Exception:  # noqa: BLE001
                pass
        try:                                                                      # Stage 6.10A: the forward evidence capture, under its own guard
            out["capture"] = self.capture.tick(now, trigger)
        except Exception as exc:  # noqa: BLE001 - never into the observer loop; the day's evidence health records the gap
            out["capture"] = {"result": "ERROR", "error": type(exc).__name__}
        self.last_observation = out
        return out

    def _snapshot(self, now: datetime) -> dict:
        st = self.stream_fn()
        x = st.execution
        mode = self._safe(lambda: x.mode()["mode"], "MONITOR_ONLY")
        sv = self._safe(lambda: st.safety.current(now=now))
        fr = self._safe(lambda: st.account_refresh.freshness(now), {}) or {}
        snap = self._safe(lambda: st.account_refresh.store.snapshot(), None)
        acct = (snap or {}).get("account") or {}
        cf = self._safe(lambda: st.cash_flow.freshness(now), {}) or {}
        adj = self._safe(lambda: st.cash_flow.adjustment(now, use_default_inputs=True, wait=False), {}) or {}
        from cash_flow.service import CashFlowService
        cf_status = CashFlowService.overall(adj) if adj else (cf.get("status") or None)
        open_pos = self._safe(lambda: x.store.positions(open_only=True), []) or []
        opens = self._safe(lambda: x.store.open_intents(), []) or []
        attention = self._execution_attention(x, opens, open_pos)
        controls = {c["control_id"]: _n(c.get("fire_count")) for c in (self._safe(lambda: x.store.controls(), []) or [])}
        try:
            from execution.scheduler import status as sched_status
            loop_running = bool(sched_status().get("running"))
        except Exception:  # noqa: BLE001
            loop_running = None
        last = x.last_cycle or {}
        return {"at": _iso(now), "trading_date": (session_date(now) or now.date()).isoformat(), "in_session": is_regular_session(now), "owner": self.owner, "mode": mode,
                "executor_ready": bool(getattr(x, "ready", False)), "safety_state": getattr(sv, "state", None), "allowed_new_entry": getattr(sv, "allowed_new_entry", None),
                "safety_codes": list(getattr(sv, "reason_codes", []) or []), "reconciliation_healthy": bool(getattr(x, "recon_healthy", True)), "attention_count": len(attention),
                "attention": attention, "account_status": fr.get("status"), "account_age_s": fr.get("age_seconds"), "account_failures": fr.get("consecutive_failures"),
                "equity": acct.get("equity"), "cash": acct.get("cash"), "buying_power": acct.get("buying_power"), "net_external_cum": self._net_external_cum(now),
                "cash_flow_status": cf_status, "cash_flow_age_s": cf.get("age_seconds"), "cash_flow_failures": cf.get("consecutive_failures"), "open_positions": len(open_pos),
                "pending_entries": sum(1 for i in opens if i.intent_type == "ENTRY"), "pending_exits": sum(1 for i in opens if i.intent_type == "EXIT"),
                "positions": [{"position_id": p.position_id, "symbol": p.symbol, "quantity": p.quantity, "state": p.state, "last_price": p.last_price, "last_price_at": p.last_price_at,
                               "average_entry_price": p.average_entry_price} for p in open_pos],
                "controls": controls, "last_cycle_at": last.get("at"), "last_cycle_result": last.get("result"), "loop_running": loop_running}

    @staticmethod
    def _execution_attention(x, opens, open_pos) -> List[str]:
        reasons: List[str] = []
        for i in opens:
            if i.state == "RECONCILIATION_REQUIRED":
                reasons.append(f"{i.symbol}: order needs reconciliation")
        for p in open_pos:
            if p.state == "ATTENTION":
                reasons.append(f"{p.symbol}: {(p.attention_reason or 'position needs attention').lower().replace('_', ' ')}")
        for r in list(getattr(x, "recon_reasons", []) or []):
            if r not in reasons:
                reasons.append(r)
        return reasons

    def _net_external_cum(self, now: datetime) -> Optional[str]:
        """Cumulative external cash flow (deposits + journals in - withdrawals - journals out) since the validation started: the adjustment
        that keeps deposits and withdrawals out of the equity curve (the Stage 6.0.2 ledger, latest revision per broker activity)."""
        start = self.store.first_session_created_at()
        if start is None:
            return None
        rows = self.store.query("SELECT e.direction, e.amount, e.net_amount FROM cash_flow_events e JOIN (SELECT broker_event_id, MAX(revision) AS rv FROM cash_flow_events "
                                "GROUP BY broker_event_id) m ON e.broker_event_id = m.broker_event_id AND e.revision = m.rv WHERE e.external = 1 AND e.effective_at IS NOT NULL "
                                "AND e.effective_at >= ? AND e.effective_at <= ? AND (e.broker_status IS NULL OR lower(e.broker_status) = 'executed')", (start, _iso(now)))
        total = Decimal(0)
        for r in rows:
            net = M.D(r.get("net_amount"))
            if net is None:
                amt = M.D(r.get("amount")) or Decimal(0)
                net = -amt.copy_abs() if r.get("direction") == "OUT" else amt.copy_abs()
            total += net
        return M.money(total)

    def _marks(self, now: datetime, sample: dict) -> List[dict]:
        return [{"at": sample["at"], "position_id": p["position_id"], "symbol": p["symbol"], "state": p["state"], "quantity": p["quantity"], "last_price": p.get("last_price"),
                 "last_price_at": p.get("last_price_at")} for p in sample["positions"] if p.get("last_price")]

    # ---------------------------------------------------------------- the session record ---------------------------------------
    def _record_session(self, now: datetime, sample: dict) -> Optional[str]:
        today = sample["trading_date"]
        et = to_et(now)
        for row in self.store.open_sessions():                                            # an earlier date still OPEN: the process was not there to close it
            if row["trading_date"] < today:
                self._finalize(row["trading_date"], now, close_seen=False, reason="CLOSE_NOT_OBSERVED")
        if et is None or et.weekday() >= 5:
            return None
        after_close = (et.hour * 60 + et.minute) >= CLOSE_MINUTE
        row = self.store.session(today)
        if row is None:
            if after_close and not sample["in_session"]:
                return None                                                              # first seen after the close: nothing of the session was observed
            row = self.store.open_session(today, now, self.owner, in_session=sample["in_session"])
            self.store.add_event(now, "SESSION_OPENED", None, {"in_session": sample["in_session"], "mode": sample["mode"]}, trading_date=today, owner=self.owner)
            self._touch(now, "session_recorder", "RUN", f"session {today} opened")
        if row["session_status"] != "OPEN":
            return row["session_status"]
        owners = list(row["owners"]) + ([self.owner] if self.owner not in row["owners"] else [])
        fields: Dict[str, Any] = {"bot_stop_time": sample["at"], "owners_json": __import__("json").dumps(owners),
                                  "in_session_samples": _n(row["in_session_samples"]) + (1 if sample["in_session"] else 0),
                                  "market_open_seen": bool(row["market_open_seen"]) or sample["in_session"],
                                  "paper_auto_enabled": bool(row["paper_auto_enabled"]) or (sample["in_session"] and sample["mode"] == "PAPER_AUTO")}
        self.store.update_session(today, now, **fields)
        if after_close:
            return self._finalize(today, now, close_seen=True, reason="")
        return "OPEN"

    def _finalize(self, trading_date: str, now: datetime, *, close_seen: bool, reason: str) -> str:
        row = self.store.session(trading_date)
        if row is None or row["session_status"] != "OPEN":
            return row["session_status"] if row else "NOT_RUN"
        obs = self._session_observations(trading_date, row, close_seen)
        status, notes = M.classify_session(obs)
        if reason and reason not in notes:
            notes = "|".join([c for c in notes.split("|") if c] + [reason])
        self.store.update_session(trading_date, now, ended_at=_iso(now), market_close_seen=close_seen, session_status=status, notes_code=notes,
                                  account_refresh_healthy=obs["account_healthy"], cash_flow_healthy=obs["cash_flow_healthy"], reconciliation_healthy=obs["reconciliation_healthy"])
        self.store.add_event(now, "SESSION_CLOSED", status, {"notes": notes, "in_session_samples": obs["in_session_samples"]}, trading_date=trading_date, owner=self.owner)
        self._touch(now, "session_recorder", "RUN", f"session {trading_date} closed as {status}")
        try:
            self.store.put_daily(trading_date, now, VERSION, self.daily_summary(trading_date, now))
            self._touch(now, "summary_builder", "RUN", f"daily summary {trading_date} written")
        except Exception as exc:  # noqa: BLE001 - the session record stands even when the summary failed; it is rebuilt on demand
            log.warning("forward validation: daily summary %s failed (%s)", trading_date, type(exc).__name__)
            self._touch(now, "summary_builder", "UNKNOWN", f"daily summary {trading_date} failed", type(exc).__name__)
        return status

    def _session_observations(self, trading_date: str, row: dict, close_seen: bool) -> dict:
        samples = self.store.samples(trading_date, in_session_only=True)
        start, end = ny_window(trading_date)
        x = self.execution
        fresh_seen = any(s.get("account_status") == "FRESH" for s in samples)
        cf_fresh_seen = any(s.get("cash_flow_status") == "FRESH" for s in samples)
        acct_stale = sum(1 for s in samples if s.get("account_status") in ("STALE", "UNAVAILABLE", "DISABLED"))
        cf_stale = sum(1 for s in samples if s.get("cash_flow_status") in ("STALE", "UNAVAILABLE", "AMBIGUOUS"))
        recon_bad = sum(1 for s in samples if s.get("reconciliation_healthy") is False)
        unknown = sum(1 for s in samples if s.get("safety_state") == "UNKNOWN")
        broker_unavail = sum(1 for s in samples if any("broker unavailable" in a for a in (s.get("attention") or [])))
        rel = self.order_reliability(trading_date)
        gate = self._gate_violations(start, end)
        live = self._live_host_attempts(start, end)
        runs = self.store.query("SELECT result FROM execution_runs WHERE started_at >= ? AND started_at < ?", (start, end))
        refresh_fail = _n(self.freshness(trading_date)["account"]["refresh_failures"]) + _n(self.freshness(trading_date)["cash_flow"]["refresh_failures"])
        broker_errors = _n(self.broker_health(trading_date)["total_errors"])
        early = bool(self.store.events(kind="EARLY_STOP", trading_date=trading_date))
        open_now = self._safe(lambda: x.store.open_intents(), []) or []
        pos_now = self._safe(lambda: x.store.positions(open_only=True), []) or []
        unresolved_recon = any(i.state == "RECONCILIATION_REQUIRED" for i in open_now) or any(p.state == "ATTENTION" for p in pos_now)
        failed_intents = self.store.query("SELECT count(*) AS n FROM execution_intents WHERE state = 'FAILED' AND updated_at >= ? AND updated_at < ?", (start, end))
        last = samples[-1] if samples else None
        n_in = len(samples)
        return {"in_session_samples": n_in, "market_open_seen": bool(row["market_open_seen"]) or n_in > 0, "market_close_seen": close_seen,
                "duplicate_orders": _n(rel["duplicate_broker_orders"]), "live_host_attempts": live, "gate_violations": len(gate), "early_stop": early,
                "unresolved_reconciliation_at_end": unresolved_recon, "unresolved_execution_at_end": _n(failed_intents[0]["n"]) if failed_intents else 0,
                "safety_unknown_at_end": bool(last and last.get("safety_state") == "UNKNOWN"), "broker_unavailable_persistent": n_in > 0 and broker_unavail * 2 > n_in,
                "account_stale_samples": acct_stale, "cash_flow_stale_samples": cf_stale, "reconciliation_unhealthy_samples": recon_bad, "safety_unknown_samples": unknown,
                "cycle_errors": sum(1 for r in runs if r.get("result") == "ERROR"), "refresh_failures": refresh_fail, "broker_errors": broker_errors,
                "restarts": len(self.store.restarts(trading_date=trading_date)), "account_fresh_seen": fresh_seen, "cash_flow_fresh_seen": cf_fresh_seen,
                "account_healthy": n_in > 0 and fresh_seen and acct_stale == 0, "cash_flow_healthy": n_in > 0 and cf_fresh_seen and cf_stale == 0,
                "reconciliation_healthy": n_in > 0 and recon_bad == 0 and not unresolved_recon}

    def _backfill_not_run(self, now: datetime, today: str) -> List[str]:
        last = self.store.last_session_date()
        if last is None or last >= today:
            return []
        done = []
        for d in weekdays_between(last, today):
            if self.store.session(d) is None:
                self.store.insert_not_run(d, now, "NO_OBSERVATION|HOLIDAYS_NOT_MODELLED")
                self.store.add_event(now, "SESSION_NOT_RUN", "NO_OBSERVATION", {"note": "no observer tick on this weekday (an exchange holiday reads the same: no calendar exists)"},
                                     trading_date=d, owner=self.owner)
                done.append(d)
        return done

    # ---------------------------------------------------------------- restart detection ----------------------------------------
    def _detect_restart(self, now: datetime, sample: dict) -> Optional[dict]:
        if not self._restart_checked:
            self._restart_checked = True
            prev = self._previous_sample()
            if prev is not None and prev.get("owner") != self.owner:
                self._restart_pending = {"previous": prev, "ticks": 0}
        pend = self._restart_pending
        if pend is None:
            return None
        pend["ticks"] += 1
        x = self.execution
        if not sample["executor_ready"] and pend["ticks"] < RESTART_GRACE_TICKS:
            return {"pending": True}
        prev = pend["previous"]
        before = {p["position_id"]: p for p in (prev.get("positions") or [])}
        after = {p["position_id"]: p for p in (sample.get("positions") or [])}
        closed_since = {r["position_id"] for r in self.store.query("SELECT position_id FROM position_events WHERE kind = 'CLOSED' AND at >= ?", (prev["at"],))}
        problems = []
        for pid, p in before.items():
            a = after.get(pid)
            if a is None:
                if pid not in closed_since:
                    problems.append(f"{p['symbol']}: open before the restart, absent after it without a CLOSED event")
            elif _n(a["quantity"]) != _n(p["quantity"]) and a["state"] not in ("EXIT_SUBMITTED", "CLOSED"):
                problems.append(f"{p['symbol']}: quantity {p['quantity']} -> {a['quantity']}")
        continuity = "CONTINUOUS" if not problems else "CHANGED: " + "; ".join(problems)
        needs = self._safe(lambda: x.needs_broker(), False)
        recon = "NOT_QUERIED" if not sample["executor_ready"] or not needs else ("HEALTHY" if getattr(x, "recon_healthy", True) else "BLOCKED: " + "; ".join(getattr(x, "recon_reasons", [])))
        rel = self.order_reliability(None)
        row = {"at": sample["at"], "trading_date": sample["trading_date"], "previous_owner": prev.get("owner"), "previous_sample_at": prev.get("at"), "owner": self.owner,
               "state_before": {k: prev.get(k) for k in ("mode", "open_positions", "pending_entries", "pending_exits", "attention", "reconciliation_healthy", "safety_state", "positions")},
               "state_after": {k: sample.get(k) for k in ("mode", "open_positions", "pending_entries", "pending_exits", "attention", "reconciliation_healthy", "safety_state", "positions")},
               "broker_queried": bool(sample["executor_ready"] and needs), "reconciliation_result": recon, "duplicate_orders": _n(rel["duplicate_broker_orders"]),
               "position_continuity": continuity, "detail": {"startup_findings": self.store.query("SELECT kind, symbol, blocking FROM execution_reconciliations WHERE trigger = 'STARTUP' AND at >= ?",
                                                                                                    (prev["at"],)), "grace_ticks": pend["ticks"]}}
        self.store.add_restart(row)
        self.store.add_event(now, "RESTART_DETECTED", continuity.split(":")[0], {"previous_owner": prev.get("owner"), "reconciliation": recon}, trading_date=sample["trading_date"], owner=self.owner)
        self._restart_pending = None
        return {"recorded": True, "continuity": continuity, "reconciliation": recon}

    def _previous_sample(self) -> Optional[dict]:
        rows = self.store.query("SELECT * FROM forward_validation_samples WHERE owner != ? ORDER BY at DESC, rowid DESC LIMIT 1", (self.owner,))
        return ForwardValidationStore._sample(rows[0]) if rows else None

    # ================================================================ attention + early stop ====================================
    def _live_host_attempts(self, start: Optional[str] = None, end: Optional[str] = None) -> int:
        """Rows anywhere in the persisted broker-facing evidence carrying the Stage 6.0 transport's refusal text (a non-paper host was asked for)."""
        n = 0
        like = f"%{LIVE_HOST_MARK}%"
        for table, col, at in (("account_refresh_attempts", "error_text", "started_at"), ("cash_flow_refresh_attempts", "error_text", "started_at"), ("execution_controls", "last_error", "updated_at"),
                               ("execution_reconciliations", "detail_json", "at"), ("execution_runs", "error", "started_at")):
            sql, args = f"SELECT count(*) AS n FROM {table} WHERE {col} LIKE ?", [like]
            if start:
                sql += f" AND {at} >= ? AND {at} < ?"
                args += [start, end]
            n += self._count(sql, tuple(args))
        return n

    def _gate_violations(self, start: Optional[str] = None, end: Optional[str] = None) -> List[dict]:
        """Entry intents whose attempt was recorded while the latest PERSISTED safety evaluation did not allow new entries."""
        sql, args = "SELECT execution_id, symbol, submitted_at FROM execution_intents WHERE intent_type = 'ENTRY' AND attempt_count >= 1 AND submitted_at IS NOT NULL", []
        if start:
            sql += " AND submitted_at >= ?"
            args.append(start)
        if end:
            sql += " AND submitted_at < ?"
            args.append(end)
        out = []
        for it in self.store.query(sql, tuple(args)):
            ev = self.store.query("SELECT state, allowed_new_entry, evaluated_at, reason_codes_json FROM execution_safety_evaluations WHERE evaluated_at <= ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                                  (it["submitted_at"],))
            if ev and not _n(ev[0]["allowed_new_entry"]):
                out.append({"execution_id": it["execution_id"], "symbol": it["symbol"], "submitted_at": it["submitted_at"], "gate_state": ev[0]["state"], "gate_evaluated_at": ev[0]["evaluated_at"]})
        return out

    def attention(self, now: Optional[datetime] = None) -> List[dict]:
        now = _utc(now or self.clock())
        st = self.stream_fn()
        x = st.execution
        out: List[dict] = []

        def add(code, detail, count=1):
            out.append({"code": code, "text": ATTENTION_TEXT.get(code, code), "critical": code in CRITICAL_CODES, "detail": detail, "count": count})
        rel = self.order_reliability(None)
        if _n(rel["duplicate_broker_orders"]):
            add("DUPLICATE_BROKER_ORDER", rel["duplicate_detail"], _n(rel["duplicate_broker_orders"]))
        live = self._live_host_attempts()
        if live:
            add("LIVE_HOST_ATTEMPT", "a request to a non-paper host was refused by the transport", live)
        gate = self._gate_violations()
        if gate:
            add("SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", gate, len(gate))
        opens = self._safe(lambda: x.store.open_intents(), []) or []
        rr = [i for i in opens if i.state == "RECONCILIATION_REQUIRED"]
        if rr:
            add("RECONCILIATION_REQUIRED", [f"{i.symbol} {i.execution_id[:8]} ({i.failure_code or 'ambiguous'})" for i in rr], len(rr))
        pos = self._safe(lambda: x.store.positions(open_only=True), []) or []
        att = [p for p in pos if p.state == "ATTENTION"]
        if att:
            add("UNRESOLVED_QTY_MISMATCH", [f"{p.symbol}: {p.attention_reason}" for p in att], len(att))
            closed = [s for s in self.store.sessions(limit=PERSISTENT_MISMATCH_SESSIONS + 1) if s["session_status"] != "OPEN"][:PERSISTENT_MISMATCH_SESSIONS]
            oldest = min((p.updated_at or _iso(now)) for p in att)
            for p in att:
                ev = self.store.query("SELECT MIN(at) AS a FROM position_events WHERE position_id = ? AND to_state = 'ATTENTION'", (p.position_id,))
                if ev and ev[0]["a"]:
                    oldest = min(oldest, ev[0]["a"])
            if len(closed) >= PERSISTENT_MISMATCH_SESSIONS and all((s.get("started_at") or s["trading_date"]) > oldest for s in closed):
                add("PERSISTENT_RECONCILIATION_MISMATCH", f"attention since {oldest} across {len(closed)} closed session(s)")
        failed = self.store.query("SELECT count(*) AS n FROM execution_intents WHERE state = 'FAILED'")
        if failed and _n(failed[0]["n"]):
            add("FAILED_EXECUTION", f"{failed[0]['n']} failed execution intent(s)", _n(failed[0]["n"]))
        fr = self._safe(lambda: st.account_refresh.freshness(now), {}) or {}
        if fr.get("status") == "STALE" and _n(fr.get("consecutive_failures")) >= _n(fr.get("max_failures") or 3):
            add("PERSISTENT_REFRESH_FAILURE", fr.get("reason"), _n(fr.get("consecutive_failures")))
        cf = self._safe(lambda: st.cash_flow.freshness(now), {}) or {}
        if cf.get("status") == "STALE" and _n(cf.get("consecutive_failures")) >= _n(cf.get("max_failures") or 3):
            add("PERSISTENT_CASH_FLOW_FAILURE", cf.get("reason"), _n(cf.get("consecutive_failures")))
        return out

    def _early_stop(self, now: datetime, att: List[dict], sample: dict) -> Optional[dict]:
        critical = sorted({a["code"] for a in att if a["critical"]})
        if not critical:
            self._touch(now, "attention_monitor", "PASS" if not att else "WARN", f"{len(att)} condition(s)")
            return None
        self._touch(now, "attention_monitor", "BLOCK", "critical: " + ", ".join(critical))
        new = [c for c in critical if c not in self._stopped_for]
        for c in new:
            self.store.add_event(now, "ATTENTION_RAISED", c, {"detail": next((a["detail"] for a in att if a["code"] == c), None)}, trading_date=sample["trading_date"], owner=self.owner)
        self._stopped_for.update(critical)
        if not self.early_stop_enabled or sample["mode"] != "PAPER_AUTO" or not new:
            return {"critical": critical, "stopped": False}
        reason = "Stage 6.1 early stop: " + ", ".join(ATTENTION_TEXT.get(c, c) for c in new)
        try:
            self.execution.disable_auto(actor="forward_validation", reason=reason, now=now)
            stopped = True
        except Exception as exc:  # noqa: BLE001 - recorded; the next tick tries again
            stopped = False
            log.error("forward validation early stop failed (%s)", type(exc).__name__)
        self.store.add_event(now, "EARLY_STOP", ",".join(new), {"reason": reason, "stopped": stopped}, trading_date=sample["trading_date"], owner=self.owner)
        self._touch(now, "early_stop", "RUN" if stopped else "UNKNOWN", reason)
        return {"critical": critical, "stopped": stopped}

    # ================================================================ descriptive statistics from persisted events ===============
    def _window(self, trading_date: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        return ny_window(trading_date) if trading_date else (None, None)

    @staticmethod
    def _where(col: str, start: Optional[str], end: Optional[str]) -> Tuple[str, list]:
        if not start:
            return "", []
        return f" AND {col} >= ? AND {col} < ?", [start, end]

    def _count(self, sql: str, args=()) -> int:
        rows = self.store.query(sql, args)
        return _n(rows[0]["n"]) if rows else 0

    def funnel(self, trading_date: Optional[str] = None) -> dict:
        start, end = self._window(trading_date)
        c: Dict[str, int] = {}
        w, a = self._where("evaluated_at", start, end)
        c["universe_scanned"] = self._count(f"SELECT count(DISTINCT symbol) AS n FROM setup_evaluations WHERE 1 = 1{w}", a)
        for state in ("WATCH", "READY", "INVALIDATED"):
            c[state] = self._count(f"SELECT count(*) AS n FROM setup_transitions WHERE to_state = ?{w}", [state] + a)
        w, a = self._where("created_at", start, end)
        for status in ("REVIEWABLE", "BLOCKED"):
            c[status] = self._count(f"SELECT count(*) AS n FROM trade_proposals WHERE status = ?{w}", [status] + a)
        w, a = self._where("submitted_at", start, end)
        c["ENTRY_SUBMITTED"] = self._count(f"SELECT count(*) AS n FROM execution_intents WHERE intent_type = 'ENTRY' AND attempt_count >= 1{w}", a)
        c["EXIT_SUBMITTED"] = self._count(f"SELECT count(*) AS n FROM execution_intents WHERE intent_type = 'EXIT' AND attempt_count >= 1{w}", a)
        w, a = self._where("resolved_at", start, end)
        c["ENTRY_FILLED"] = self._count(f"SELECT count(*) AS n FROM execution_intents WHERE intent_type = 'ENTRY' AND state = 'FILLED'{w}", a)
        c["EXIT_FILLED"] = self._count(f"SELECT count(*) AS n FROM execution_intents WHERE intent_type = 'EXIT' AND state = 'FILLED'{w}", a)
        w, a = self._where("e.at", start, end)
        c["ENTRY_PARTIAL"] = self._count(f"SELECT count(DISTINCT e.execution_id) AS n FROM execution_order_events e JOIN execution_intents i ON i.execution_id = e.execution_id "
                                         f"WHERE i.intent_type = 'ENTRY' AND lower(e.status) = 'partially_filled'{w}", a)
        w, a = self._where("opened_at", start, end)
        c["POSITION_OPEN"] = self._count(f"SELECT count(*) AS n FROM paper_positions WHERE 1 = 1{w}", a)
        w, a = self._where("closed_at", start, end)
        c["CLOSED"] = self._count(f"SELECT count(*) AS n FROM paper_positions WHERE state = 'CLOSED'{w}", a)
        w, a = self._where("at", start, end)
        c["EXIT_REQUIRED"] = self._count(f"SELECT count(*) AS n FROM position_events WHERE kind = 'EXIT_REQUIRED'{w}", a)
        c["ATTENTION"] = self._count(f"SELECT count(*) AS n FROM position_events WHERE to_state = 'ATTENTION' AND kind = 'ATTENTION'{w}", a)
        counts = {k: c.get(k, 0) for k in FUNNEL}
        counts["INVALIDATED"] = c["INVALIDATED"]
        return {"trading_date": trading_date, "counts": counts, "rates": M.rates(counts), "source": "persisted setup transitions, proposals, intents, order events and position events"}

    def order_reliability(self, trading_date: Optional[str] = None) -> dict:
        start, end = self._window(trading_date)
        w, a = self._where("created_at", start, end)
        base = f"SELECT count(*) AS n FROM execution_intents WHERE 1 = 1{w}"
        out = {"trading_date": trading_date, "entry_intents": self._count(base + " AND intent_type = 'ENTRY'", a), "exit_intents": self._count(base + " AND intent_type = 'EXIT'", a),
               "broker_submissions": self._count(base + " AND attempt_count >= 1", a), "fills": self._count(base + " AND state = 'FILLED'", a),
               "rejects": self._count(base + " AND state = 'REJECTED'", a), "cancels": self._count(base + " AND state = 'CANCELED' AND lower(IFNULL(broker_status, '')) != 'expired'", a),
               "expired_orders": self._count(base + " AND lower(IFNULL(broker_status, '')) = 'expired'", a), "not_sent": self._count(base + " AND state = 'NOT_SENT'", a),
               "failed": self._count(base + " AND state = 'FAILED'", a),
               "ambiguous_submissions": self._count(base + f" AND (outcome_class IN ({', '.join('?' * len(AMBIGUOUS_CLASSES))}) OR state = 'RECONCILIATION_REQUIRED')", a + list(AMBIGUOUS_CLASSES)),
               "duplicate_attempts": self._count(base + " AND attempt_count > 1", a)}
        w2, a2 = self._where("e.at", start, end)
        out["partial_fills"] = self._count(f"SELECT count(DISTINCT e.execution_id) AS n FROM execution_order_events e WHERE lower(e.status) = 'partially_filled'{w2}", a2)
        dup_rows = self.store.query("SELECT client_order_id, count(*) AS n FROM execution_orders GROUP BY client_order_id HAVING count(*) > 1")
        if start:
            dup_rows = [r for r in dup_rows if self.store.query("SELECT 1 FROM execution_orders WHERE client_order_id = ? AND first_seen_at >= ? AND first_seen_at < ?", (r["client_order_id"], start, end))]
        w3, a3 = self._where("at", start, end)
        multi = self.store.query(f"SELECT kind, symbol, detail_json FROM execution_reconciliations WHERE kind IN ('MULTIPLE_ORDERS', 'UNKNOWN_INTERNAL_ORDER'){w3}", a3)
        out["duplicate_broker_orders"] = len(dup_rows) + len(multi)
        out["duplicate_detail"] = [f"{r['client_order_id']}: {r['n']} broker orders" for r in dup_rows] + [f"{m['kind']} {m.get('symbol') or ''}".strip() for m in multi]
        out["duplicate_entry_blocks"] = _n(next((c.get("fire_count") for c in self.store.query("SELECT fire_count FROM execution_controls WHERE control_id = 'DUPLICATE_ENTRY'")), 0))
        out["target"] = {"duplicate_broker_orders": 0, "met": out["duplicate_broker_orders"] == 0}
        return out

    def reconciliation(self, trading_date: Optional[str] = None) -> dict:
        start, end = self._window(trading_date)
        w, a = self._where("at", start, end)
        rows = self.store.query(f"SELECT * FROM execution_reconciliations WHERE 1 = 1{w} ORDER BY at", a)
        by_kind: Dict[str, int] = {}
        resolved = 0
        detail = []
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            res = self._resolution(r)
            resolved += 1 if res == "RESOLVED" else 0
            if r["kind"] in BLOCKING_FINDINGS:
                detail.append({"at": r["at"], "kind": r["kind"], "symbol": r.get("symbol"), "blocking": bool(r.get("blocking")), "resolution": res, "trigger": r.get("trigger")})
        runs = self.store.query(f"SELECT summary_json FROM execution_runs WHERE 1 = 1{self._where('started_at', start, end)[0]}", a)
        healthy_runs = 0
        for r in runs:
            try:
                healthy_runs += 1 if _n(__import__("json").loads(r["summary_json"] or "{}").get("findings")) == 0 else 0
            except ValueError:
                pass
        return {"trading_date": trading_date, "findings": len(rows), "by_kind": by_kind, "blocking": sum(1 for r in rows if r.get("blocking")), "resolved": resolved,
                "open": sum(1 for d in detail if d["resolution"] == "OPEN"), "startup_reconciliations": sum(1 for r in rows if r.get("trigger") == "STARTUP") + len(self.store.restarts(trading_date=trading_date) if trading_date else self.store.restarts(limit=100000)),
                "healthy_reconciliations": healthy_runs, "needs_attention_cases": by_kind.get("QTY_MISMATCH", 0) + by_kind.get("BROKER_FLAT_INTERNAL_OPEN", 0) + by_kind.get("INTERNAL_FLAT_BROKER_POSITION", 0) + by_kind.get("UNKNOWN_INTERNAL_ORDER", 0) + by_kind.get("UNKNOWN_STATUS", 0) + by_kind.get("ORDER_MISSING_AT_BROKER", 0),
                "quantity_mismatches": by_kind.get("QTY_MISMATCH", 0), "unknown_broker_orders": by_kind.get("UNKNOWN_INTERNAL_ORDER", 0), "internal_open_broker_flat": by_kind.get("BROKER_FLAT_INTERNAL_OPEN", 0),
                "internal_flat_broker_open": by_kind.get("INTERNAL_FLAT_BROKER_POSITION", 0), "unknown_status": by_kind.get("UNKNOWN_STATUS", 0), "broker_unavailable": by_kind.get("BROKER_UNAVAILABLE", 0),
                "external_positions": by_kind.get("EXTERNAL_POSITION", 0), "detail": detail[-50:]}

    def _resolution(self, finding: dict) -> str:
        if finding["kind"] not in BLOCKING_FINDINGS:
            return "INFORMATIONAL"
        if finding.get("position_id"):
            later = self.store.query("SELECT 1 FROM position_events WHERE position_id = ? AND at > ? AND (kind IN ('ATTENTION_CLEARED', 'CLOSED') OR to_state = 'CLOSED') LIMIT 1", (finding["position_id"], finding["at"]))
            return "RESOLVED" if later else "OPEN"
        if finding.get("execution_id"):
            it = self.store.query("SELECT state FROM execution_intents WHERE execution_id = ?", (finding["execution_id"],))
            return "RESOLVED" if it and it[0]["state"] not in INTENT_OPEN else "OPEN"
        if finding["kind"] == "BROKER_UNAVAILABLE":
            later = self.store.query("SELECT 1 FROM execution_runs WHERE started_at > ? AND result = 'RUN' AND IFNULL(error, '') = '' LIMIT 1", (finding["at"],))
            return "RESOLVED" if later else "OPEN"
        return "OPEN"

    def safety_fires(self, trading_date: Optional[str] = None) -> dict:
        start, end = self._window(trading_date)
        w, a = self._where("evaluated_at", start, end)
        evs = self.store.query(f"SELECT evaluated_at, trigger, state, allowed_new_entry, reason_codes_json FROM execution_safety_evaluations WHERE 1 = 1{w} ORDER BY evaluated_at, rowid", a)
        for e in evs:
            try:
                e["reason_codes"] = __import__("json").loads(e.pop("reason_codes_json") or "[]")
            except ValueError:
                e["reason_codes"] = []
        fires = M.classify_safety_fires(evs)
        w2, a2 = self._where("at", start, end)
        events = self.store.query(f"SELECT at, kind, actor, reason, from_state, to_state FROM execution_safety_events WHERE 1 = 1{w2} ORDER BY at", a2)
        by_kind: Dict[str, int] = {}
        for e in events:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        by_code: Dict[str, int] = {}
        for f in fires:
            for code in f["reason_codes"] or [f["state"]]:
                by_code[code] = by_code.get(code, 0) + 1
        controls = self._control_fires(trading_date)
        return {"trading_date": trading_date, "evaluations": len(evs), "fires": fires, "fire_count": len(fires), "by_reason_code": by_code,
                "recovered": sum(1 for f in fires if f["outcome"] == "RECOVERED"), "persistent": sum(1 for f in fires if f["outcome"] == "PERSISTENT"),
                "expected_events": {k: v for k, v in by_kind.items() if k in ("BASELINE_CREATED", "DAILY_RESET", "STARTUP")},
                "events_by_kind": by_kind, "daily_loss_halts": by_kind.get("DAILY_HALT_TRIGGERED", 0), "emergency_stops": by_kind.get("KILL_ON", 0), "pauses": by_kind.get("PAUSE_ON", 0),
                "evaluator_errors": by_kind.get("EVALUATOR_ERROR", 0), "entry_control_blocks": controls}

    def _control_fires(self, trading_date: Optional[str]) -> dict:
        """Entry-control blocks (near-close, position cap, buying power, proposal validity, paper proof, reconciliation ...) as the fire-count
        deltas between the first and the last observer sample of the window (the Stage 6.0 control rows keep running totals only)."""
        samples = self.store.samples(trading_date) if trading_date else self.store.samples()
        first = next((s for s in samples if s.get("controls")), None)
        last = next((s for s in reversed(samples) if s.get("controls")), None)
        if first is None or last is None:
            return {}
        return {k: max(0, _n(last["controls"].get(k)) - _n(first["controls"].get(k))) for k in sorted(set(first["controls"]) | set(last["controls"]))}

    def freshness(self, trading_date: str) -> dict:
        start, end = ny_window(trading_date)
        samples = self.store.samples(trading_date, in_session_only=True)
        ages = [s["account_age_s"] for s in samples if s.get("account_age_s") is not None]
        cfa = [s["cash_flow_age_s"] for s in samples if s.get("cash_flow_age_s") is not None]
        acc = self.store.query("SELECT result, error_code FROM account_refresh_attempts WHERE started_at >= ? AND started_at < ?", (start, end))
        cfr = self.store.query("SELECT result, error_code FROM cash_flow_refresh_attempts WHERE started_at >= ? AND started_at < ?", (start, end))
        last_ok_a = self.store.query("SELECT MAX(finished_at) AS t FROM account_refresh_attempts WHERE result = 'SUCCESS' AND started_at < ?", (end,))
        last_ok_c = self.store.query("SELECT MAX(finished_at) AS t FROM cash_flow_refresh_attempts WHERE result = 'SUCCESS' AND started_at < ?", (end,))
        return {"trading_date": trading_date,
                "account": {"max_age_s": max(ages) if ages else None, "attempts": len(acc), "refresh_failures": sum(1 for r in acc if r["result"] != "SUCCESS"),
                            "max_consecutive_failures": max([_n(s.get("account_failures")) for s in samples] or [0]), "last_successful_refresh": (last_ok_a[0]["t"] if last_ok_a else None),
                            "statuses": self._status_counts(samples, "account_status")},
                "cash_flow": {"max_age_s": max(cfa) if cfa else None, "attempts": len(cfr), "refresh_failures": sum(1 for r in cfr if r["result"] not in ("SUCCESS", "INCOMPLETE")),
                              "incomplete_reads": sum(1 for r in cfr if r["result"] == "INCOMPLETE"), "max_consecutive_failures": max([_n(s.get("cash_flow_failures")) for s in samples] or [0]),
                              "last_successful_read": (last_ok_c[0]["t"] if last_ok_c else None), "statuses": self._status_counts(samples, "cash_flow_status")}}

    @staticmethod
    def _status_counts(samples: List[dict], key: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for s in samples:
            k = s.get(key) or "NONE"
            out[k] = out.get(k, 0) + 1
        return out

    def broker_health(self, trading_date: Optional[str] = None) -> dict:
        start, end = self._window(trading_date)
        codes: Dict[str, int] = {}

        def bump(code):
            if code:
                codes[str(code)] = codes.get(str(code), 0) + 1
        w, a = self._where("started_at", start, end)
        for r in self.store.query(f"SELECT error_code FROM account_refresh_attempts WHERE result != 'SUCCESS'{w}", a):
            bump(r["error_code"])
        for r in self.store.query(f"SELECT error_code FROM cash_flow_refresh_attempts WHERE result NOT IN ('SUCCESS', 'INCOMPLETE'){w}", a):
            bump(r["error_code"])
        for r in self.store.query(f"SELECT error FROM execution_runs WHERE result = 'ERROR'{w}", a):
            bump((r["error"] or "ERROR").split(":")[0][:40])
        w2, a2 = self._where("at", start, end)
        for r in self.store.query(f"SELECT detail_json FROM execution_reconciliations WHERE kind = 'BROKER_UNAVAILABLE'{w2}", a2):
            try:
                bump("BROKER_UNAVAILABLE:" + str(__import__("json").loads(r["detail_json"] or "{}").get("code")))
            except ValueError:
                bump("BROKER_UNAVAILABLE")
        groups = {"timeouts": sum(v for k, v in codes.items() if "TIMEOUT" in k), "connection_resets": sum(v for k, v in codes.items() if "CONNECTION" in k),
                  "rate_limited_429": sum(v for k, v in codes.items() if "RATE_LIMITED" in k or k.endswith("429")), "http_5xx": sum(v for k, v in codes.items() if "HTTP_5" in k),
                  "unreadable": sum(v for k, v in codes.items() if "UNREADABLE" in k), "invalid_payload": sum(v for k, v in codes.items() if "INVALID" in k),
                  "account_read_failures": sum(1 for _ in self.store.query(f"SELECT 1 FROM account_refresh_attempts WHERE result != 'SUCCESS'{w}", a)),
                  "broker_unavailable_findings": sum(v for k, v in codes.items() if k.startswith("BROKER_UNAVAILABLE"))}
        return {"trading_date": trading_date, "total_errors": sum(codes.values()), "by_code": codes, **groups,
                "not_separately_persisted": ["broker clock failures (the executor records them only in the SESSION_POLICY control input)", "position / order read failures inside an executor cycle (recorded as reconciliation findings when they block)"]}

    def restarts_view(self, trading_date: Optional[str] = None) -> dict:
        rows = self.store.restarts(trading_date=trading_date) if trading_date else self.store.restarts(limit=200)
        return {"trading_date": trading_date, "count": len(rows), "continuous": sum(1 for r in rows if r["position_continuity"] == "CONTINUOUS"),
                "duplicate_orders_after_restart": sum(_n(r["duplicate_orders"]) for r in rows), "restarts": rows}

    # ---------------------------------------------------------------- closed trades ------------------------------------------------
    def closed_trades(self, limit: int = 500, trading_date: Optional[str] = None) -> List[dict]:
        x = self.execution
        rows = [p for p in (self._safe(lambda: x.store.positions(open_only=False, limit=limit * 3), []) or []) if p.state == "CLOSED"]
        if trading_date:
            start, end = ny_window(trading_date)
            rows = [p for p in rows if p.closed_at and start <= p.closed_at < end]
        return [self._trade(p) for p in rows[:limit]]

    def _trade(self, p) -> dict:
        x = self.execution
        entry = self._safe(lambda: x.store.intent(p.execution_id))
        exits = [i for i in (self._safe(lambda: x.store.intents(p.symbol, intent_type="EXIT", limit=50), []) or []) if i.position_id == p.position_id]
        exits.sort(key=lambda i: i.exit_cycle)
        filled_exit = next((i for i in exits if i.filled_qty > 0), None)
        ref_entry = entry.reference_price if entry else None
        ref_exit = filled_exit.reference_price if filled_exit else None
        pnl = M.trade_pnl(p.average_entry_price, p.average_exit_price, p.exit_qty or p.entry_qty)
        marks = self.store.marks(p.position_id)
        exc = M.excursions(p.average_entry_price, [m["last_price"] for m in marks], p.exit_qty or p.entry_qty)
        prop = None
        if p.proposal_id:
            try:
                from proposals.store import ProposalStore
                prop = ProposalStore(self.store.path).get(p.proposal_id)
            except Exception:  # noqa: BLE001
                prop = None
        ep = self.store.query("SELECT * FROM setup_episodes WHERE setup_id = ?", (p.setup_id,)) if p.setup_id else []
        ready = self.store.query("SELECT evaluated_at, reason_code, inputs_json FROM setup_transitions WHERE setup_id = ? AND to_state = 'READY' ORDER BY evaluated_at LIMIT 1", (p.setup_id,)) if p.setup_id else []
        inputs = {}
        if ready:
            try:
                inputs = __import__("json").loads(ready[0]["inputs_json"] or "{}")
            except ValueError:
                inputs = {}
        ppe, dem, evd = (prop or {}).get("ppe_snapshot") or {}, (prop or {}).get("demand_snapshot") or {}, (prop or {}).get("evidence_snapshot") or {}
        return {"position_id": p.position_id, "execution_id": p.execution_id, "proposal_id": p.proposal_id, "setup_id": p.setup_id, "symbol": p.symbol, "side": p.side,
                "entry_controls": entry_controls_of(entry),                                     # Stage 6.9.1: which entry controls allowed this entry
                "trading_date": (session_date(_parse(p.closed_at)) or _parse(p.closed_at).date()).isoformat() if _parse(p.closed_at) else None,
                "opened_at": p.opened_at, "closed_at": p.closed_at, "qty": p.exit_qty or p.entry_qty, "entry_qty": p.entry_qty, "entry_price": p.average_entry_price,
                "exit_price": p.average_exit_price, "realized_pnl": p.realized_pnl, **pnl, "fees_note": "not attributable per paper order (Alpaca posts fees as account activities; see the cash-flow ledger)",
                "holding_sessions": p.sessions_held, "exit_reason": p.exit_reason, "exit_reason_text": EXIT_TEXT.get(p.exit_reason or "", p.exit_reason), "exit_cycles": p.exit_cycle,
                "entry_slippage": M.slippage("buy", ref_entry, p.average_entry_price), "exit_slippage": M.slippage("sell", ref_exit, p.average_exit_price),
                "excursions": exc, "entry_broker_order_id": entry.broker_order_id if entry else None, "exit_broker_order_ids": [i.broker_order_id for i in exits if i.broker_order_id],
                "entry_submitted_at": entry.submitted_at if entry else None, "entry_filled_at": entry.resolved_at if entry else None,
                "attribution": {"setup_type": (prop or {}).get("setup_type") or (ep[0]["setup_type"] if ep else None), "cycle_id": ep[0]["cycle_id"] if ep else None,
                                "episode_started_at": ep[0]["started_at"] if ep else None, "ready_at": ready[0]["evaluated_at"] if ready else None,
                                "ready_reason": ready[0]["reason_code"] if ready else None, "stretch": inputs.get("stretch"), "confidence": inputs.get("confidence"), "demand": inputs.get("demand"),
                                "evidence_strength": inputs.get("evidence_strength"), "evidence_cause": inputs.get("evidence_cause"), "ppe_snapshot": ppe, "demand_snapshot": dem, "evidence_snapshot": evd,
                                "reference_price": (prop or {}).get("reference_price") or ref_entry, "invalidation_price": p.invalidation_price, "risk_per_share": (prop or {}).get("risk_per_share"),
                                "risk_budget": p.risk_budget, "proposal_qty": entry.proposal_qty if entry else (prop or {}).get("final_qty"), "equity_at_proposal": (prop or {}).get("equity"),
                                "price_mode": (prop or {}).get("price_mode"), "mode": (prop or {}).get("mode") or inputs.get("mode"), "market_data_source": inputs.get("market_data_source")},
                "market_context": {"session_date": (session_date(_parse(p.opened_at)) or _parse(p.opened_at).date()).isoformat() if _parse(p.opened_at) else None,
                                   "market_trend_proxy": None, "volatility_proxy": None, "market_event_flags": [],
                                   "note": "no in-process market trend / volatility source exists; the setup inputs (stretch, confidence, demand, evidence) are the recorded context"}}

    def trace(self, position_id: str) -> Optional[dict]:
        """The end-to-end audit trail of one closed (or open) paper trade from persisted rows only; no model, no inference."""
        x = self.execution
        p = self._safe(lambda: x.store.position(position_id))
        if p is None:
            return None
        t = self._trade(p)
        transitions = self.store.query("SELECT evaluated_at, from_state, to_state, reason_code, reason_text, inputs_json, trigger FROM setup_transitions WHERE setup_id = ? ORDER BY evaluated_at", (p.setup_id,)) if p.setup_id else []
        for tr in transitions:
            try:
                tr["inputs"] = __import__("json").loads(tr.pop("inputs_json") or "{}")
            except ValueError:
                tr["inputs"] = {}
        prop = None
        controls: List[dict] = []
        if p.proposal_id:
            try:
                from proposals.store import ProposalStore
                ps = ProposalStore(self.store.path)
                prop = ps.get(p.proposal_id)
                controls = ps.controls_for(p.proposal_id)
            except Exception:  # noqa: BLE001
                prop = None
        entry = self._safe(lambda: x.store.intent(p.execution_id))
        gate = self.store.query("SELECT evaluated_at, state, allowed_new_entry, reason_codes_json, trigger FROM execution_safety_evaluations WHERE evaluated_at <= ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                                (entry.submitted_at or entry.created_at,)) if entry else []
        exits = sorted([i for i in (self._safe(lambda: x.store.intents(p.symbol, intent_type="EXIT", limit=50), []) or []) if i.position_id == p.position_id], key=lambda i: i.exit_cycle)
        events = self._safe(lambda: x.store.position_events(p.position_id, 200), []) or []
        exit_req = next((e for e in events if e.get("kind") == "EXIT_REQUIRED"), None)
        orders = {}
        for it in ([entry] if entry else []) + exits:
            if it.broker_order_id:
                orders[it.execution_id] = self._safe(lambda it=it: x.store.order_events(it.broker_order_id, 50), []) or []
        return {**t, "why_considered": [tr for tr in transitions if tr["to_state"] == "WATCH"], "why_ready": [tr for tr in transitions if tr["to_state"] == "READY"],
                "why_quantity": {k: (prop or {}).get(k) for k in ("equity", "equity_source", "risk_fraction", "risk_budget", "risk_per_share", "base_qty", "position_cap_qty", "buying_power_qty",
                                                                 "final_qty", "cap_source", "reference_price", "invalidation_price", "stop_rule")} if prop else {"note": "proposal row not found"},
                "why_entry_allowed": {"proposal_controls": [{k: c.get(k) for k in ("control_id", "result", "reason")} for c in controls], "safety_gate_before_submission": gate[0] if gate else None,
                                      "entry_environment": entry.environment if entry else None, "proposal_snapshot": entry.proposal_snapshot if entry else None},
                "broker_order_sent": {k: getattr(entry, k) for k in ("client_order_id", "broker_order_id", "side", "qty", "state", "broker_status", "outcome_class", "submitted_at")} if entry else None,
                "fill": {"filled_qty": entry.filled_qty, "filled_avg_price": entry.filled_avg_price, "resolved_at": entry.resolved_at, "order_events": orders.get(entry.execution_id, [])} if entry else None,
                "why_exit_required": exit_req, "exit_orders": [{k: getattr(i, k) for k in ("execution_id", "exit_cycle", "client_order_id", "broker_order_id", "state", "filled_qty", "filled_avg_price",
                                                                                       "reference_price", "submitted_at", "resolved_at", "failure_code")} for i in exits],
                "exit_order_events": {k: v for k, v in orders.items() if entry is None or k != entry.execution_id}, "position_events": events,
                "final_pnl": {"realized_pnl": p.realized_pnl, **M.trade_pnl(p.average_entry_price, p.average_exit_price, p.exit_qty or p.entry_qty)}}

    # ---------------------------------------------------------------- equity, drawdown, open P/L -------------------------------------
    def equity_series(self, trading_date: Optional[str] = None) -> List[Tuple[str, Optional[str]]]:
        samples = self.store.samples(trading_date) if trading_date else self.store.samples(limit=100000)
        out = []
        for s in samples:
            e = M.D(s.get("equity"))
            if e is None or s.get("account_status") not in ("FRESH",):
                continue
            adj = e - (M.D(s.get("net_external_cum")) or Decimal(0))
            out.append((s["at"], M.money(adj)))
        return out

    def unrealized_pnl(self) -> dict:
        x = self.execution
        total, rows = Decimal(0), []
        for p in (self._safe(lambda: x.store.positions(open_only=True), []) or []):
            px, e = M.D(p.last_price), M.D(p.average_entry_price)
            u = (px - e) * p.quantity if px is not None and e is not None else None
            if u is not None:
                total += u
            rows.append({"symbol": p.symbol, "quantity": p.quantity, "entry": p.average_entry_price, "last_price": p.last_price, "unrealized_pnl": M.money(u), "state": p.state})
        return {"open_positions": len(rows), "unrealized_pnl": M.money(total) if rows else None, "positions": rows, "basis": "the executor's last price per position (not a broker mark)"}

    # ================================================================ summaries ====================================================
    def exit_reason_distribution(self, trades: List[dict]) -> Dict[str, int]:
        out = {r: 0 for r in EXIT_REASONS}
        for t in trades:
            out[t.get("exit_reason") or "UNKNOWN"] = out.get(t.get("exit_reason") or "UNKNOWN", 0) + 1
        return out

    def daily_summary(self, trading_date: str, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        row = self.store.session(trading_date)
        trades = self.closed_trades(trading_date=trading_date)
        wl = M.win_loss([t["realized_pnl"] or t["gross_pnl"] for t in trades])
        funnel = self.funnel(trading_date)
        rel = self.order_reliability(trading_date)
        rec = self.reconciliation(trading_date)
        fires = self.safety_fires(trading_date)
        fr = self.freshness(trading_date)
        bh = self.broker_health(trading_date)
        dd = M.drawdown(self.equity_series(trading_date))
        att_events = self.store.events(kind="ATTENTION_RAISED", trading_date=trading_date, limit=100)
        return {"trading_date": trading_date, "computed_at": _iso(now), "version": VERSION, "session": row, "session_status": row["session_status"] if row else "NOT_RUN",
                "session_label": SESSION_LABELS.get(row["session_status"] if row else "NOT_RUN"), "funnel": funnel["counts"], "rates": funnel["rates"], "setups_ready": funnel["counts"]["READY"],
                "proposals": funnel["counts"]["REVIEWABLE"] + funnel["counts"]["BLOCKED"], "entries": funnel["counts"]["ENTRY_SUBMITTED"], "exits": funnel["counts"]["EXIT_SUBMITTED"], "open_positions_at_end": (self.store.samples(trading_date)[-1]["open_positions"] if self.store.samples(trading_date) else None),
                "rejections": rel["rejects"], "partials": rel["partial_fills"], "duplicates": rel["duplicate_broker_orders"], "reconciliation_issues": rec["needs_attention_cases"],
                "safety_fires": fires["fire_count"], "safety": {k: fires[k] for k in ("fire_count", "recovered", "persistent", "by_reason_code", "daily_loss_halts", "emergency_stops", "pauses", "evaluator_errors", "entry_control_blocks")},
                "order_reliability": rel, "reconciliation": {k: rec[k] for k in rec if k != "detail"}, "freshness": fr, "broker_health": {k: bh[k] for k in bh if k != "not_separately_persisted"},
                "restarts": self.restarts_view(trading_date)["count"], "closed_trades": [{k: t[k] for k in ("position_id", "symbol", "qty", "entry_price", "exit_price", "realized_pnl", "return_pct", "exit_reason", "holding_sessions")} for t in trades],
                "realized_pnl": wl["gross_pnl"], "win_loss": wl, "unrealized_pnl": self.unrealized_pnl()["unrealized_pnl"] if row and row["session_status"] == "OPEN" else None,
                "drawdown": dd, "attention": [{"code": e["code"], "at": e["at"]} for e in att_events], "exit_reasons": self.exit_reason_distribution(trades), "disclaimer": DISCLAIMER,
                "forward_capture": self._forward_day(trading_date)}                # Stage 6.10A: the day's operational evidence health (read only)

    def _forward_day(self, trading_date: str) -> Optional[dict]:
        try:
            from forward_capture.view import daily
            return daily(self.capture.store, trading_date)
        except Exception as exc:  # noqa: BLE001 - reporting only
            return {"error": type(exc).__name__}

    def _forward_evidence(self, now: datetime) -> dict:
        """Stage 6.10A: the forward evidence per cohort (healthy capture sessions, candidate events, closed trades, the Stage 6.1 status label)."""
        try:
            from forward_capture.view import evidence
            return evidence(self.capture.store, self, now)
        except Exception as exc:  # noqa: BLE001 - reporting only
            return {"error": type(exc).__name__}

    def cumulative(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        sessions = self.store.sessions(limit=100000)
        by = {k: sum(1 for s in sessions if s["session_status"] == k) for k in ("HEALTHY", "DEGRADED", "FAILED", "NOT_RUN", "OPEN")}
        completed = by["HEALTHY"] + by["DEGRADED"] + by["FAILED"]
        trades = self.closed_trades(limit=5000)
        wl = M.win_loss([t["realized_pnl"] or t["gross_pnl"] for t in trades])
        dd = M.drawdown(self.equity_series())
        funnel = self.funnel(None)
        rel = self.order_reliability(None)
        rec = self.reconciliation(None)
        fires = self.safety_fires(None)
        bh = self.broker_health(None)
        events = funnel["counts"]["READY"] + funnel["counts"]["REVIEWABLE"]
        edge = M.edge_status(wl["closed_trades"], by["HEALTHY"], wl["expectancy"], wl["win_rate"], dd["max_drawdown_pct"], min_trades=self.min_trades, min_sessions=self.target_sessions)
        att = self.attention(now)
        refresh_fail = self._count("SELECT count(*) AS n FROM account_refresh_attempts WHERE result != 'SUCCESS'") + self._count("SELECT count(*) AS n FROM cash_flow_refresh_attempts WHERE result NOT IN ('SUCCESS', 'INCOMPLETE')")
        operational_ok = rel["duplicate_broker_orders"] == 0 and self._live_host_attempts() == 0 and rec["open"] == 0 and not any(a["critical"] for a in att) and by["FAILED"] == 0
        return {"version": VERSION, "stage": STAGE, "as_of": _iso(now), "targets": {"sessions": self.target_sessions, "events": self.target_events, "min_trades_for_edge": self.min_trades},
                "sessions": {"completed": completed, "healthy": by["HEALTHY"], "degraded": by["DEGRADED"], "failed": by["FAILED"], "not_run": by["NOT_RUN"], "in_progress": by["OPEN"],
                             "target": self.target_sessions, "target_met": completed >= self.target_sessions, "label": f"{completed} / {self.target_sessions}"},
                "events": {"ready_transitions": funnel["counts"]["READY"], "reviewable_proposals": funnel["counts"]["REVIEWABLE"], "total": events, "target": self.target_events, "target_met": events >= self.target_events},
                "operational": {"duplicate_broker_orders": rel["duplicate_broker_orders"], "reconciliation_attention": rec["needs_attention_cases"], "reconciliation_open": rec["open"],
                                "broker_rejections": rel["rejects"], "partial_fills": rel["partial_fills"], "ambiguous_submissions": rel["ambiguous_submissions"], "not_sent": rel["not_sent"],
                                "failed_intents": rel["failed"], "broker_errors": bh["total_errors"], "refresh_failures": refresh_fail, "safety_fires": fires["fire_count"],
                                "daily_loss_halts": fires["daily_loss_halts"], "emergency_stops": fires["emergency_stops"], "restarts": self.restarts_view()["count"],
                                "live_host_attempts": self._live_host_attempts(), "early_stops": len(self.store.events(kind="EARLY_STOP", limit=1000)), "status": "READY" if operational_ok else "NOT READY"},
                "strategy": {**wl, "expectancy_status": edge, "edge_status": edge["status"], "edge_label": EDGE_LABELS[edge["status"]], "insufficient": edge["status"] == "INSUFFICIENT_DATA",
                             "insufficient_text": INSUFFICIENT_TEXT if edge["status"] == "INSUFFICIENT_DATA" else None, "drawdown": dd, "exit_reasons": self.exit_reason_distribution(trades),
                             "funnel": funnel["counts"], "conversion": funnel["rates"], "unrealized": self.unrealized_pnl(), "note": "descriptive only; nothing is tuned from these numbers"},
                "attention": att, "needs_attention": bool(att), "frozen_controls_hash": frozen_hash(), "disclaimer": DISCLAIMER,
                "entry_controls": {**exposure_gate.summary(exposure_gate.from_config(config)), "closed_trades_by_entry_controls": entry_controls_mix(trades),   # Stage 6.9.1
                                   "note": "frozen_controls_hash is the unchanged Stage 6.1 strategy contract; entry_controls is the separately versioned entry-control set. "
                                           "Trades decided under different entry controls are counted apart, never mixed without this record."}}

    def overview(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        cum = self.cumulative(now)
        try:
            from forward_validation.scheduler import status as loop_status
            loop = loop_status()
        except Exception:  # noqa: BLE001
            loop = {}
        today = (session_date(now) or now.date()).isoformat()
        return {**cum, "today": today, "today_session": self.store.session(today), "recent_sessions": self.store.sessions(limit=10), "loop": loop, "controls": self.store.controls(),
                "counts": self.store.counts(), "last_observation": self.last_observation, "forward_evidence": self._forward_evidence(now), "policy": {"paper_auto": "ON during intended validation sessions (regular hours, existing near-close cutoff)",
                "early_stop": "Paper Auto -> Monitor only on a critical defect (duplicate broker order, non-paper host attempt, entry under a blocked gate, persistent reconciliation mismatch); never a liquidation",
                "no_tuning": "no threshold, rule, size or exit changes during Stage 6.1", "attention_codes": list(ATTENTION_CODES), "controls": list(CONTROLS)}}
