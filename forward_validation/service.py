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

import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from execution import EXIT_REASONS, EXIT_TEXT, INTENT_OPEN, POSITION_OPEN
from execution import exposure_gate                           # Stage 6.9.1: the versioned entry-controls record of each paper entry
from execution.engine import D as XD, money as xmoney         # Wave 2 PR A: the executor's own money arithmetic, for the P/L re-derivation (pure functions)
from forward_validation import (ATTENTION_CODES, ATTENTION_TEXT, CONTROLS, CRITICAL_CODES, DISCLAIMER, EDGE_LABELS, FUNNEL, INSUFFICIENT_TEXT, MIN_TRADES_FOR_EDGE,
                                PERSISTENT_MISMATCH_SESSIONS, SESSION_LABELS, STAGE, TARGET_EVENTS, TARGET_SESSIONS, VERSION, frozen_hash)
from forward_validation import metrics as M
from forward_validation.attention_signature import HUMAN_ACTORS, signature   # Wave 2 PR A (X2): the incident elements a stop is consumed per
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
BRACKET_ATTEMPTS = 3                                          # Wave 2 PR A: a summary whose P/L inputs moved on every attempt is UNSTABLE (null P/L), never a mixed number
SUMMARY_REBUILDS_PER_TICK = 3                                 # Wave 2 PR A: stored summaries made current per observer tick (bounds work only; reads stay fresh)


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
        self._stopped_for: set = set()                                           # critical codes announced (ATTENTION_RAISED) by this process
        self._stopped: Dict[str, Tuple[frozenset, int]] = {}                      # Wave 2 PR A (X2): code -> (elements at the CONFIRMED stop, mode-event rowid then)
        self._acked: Dict[str, frozenset] = {}                                    # Wave 2 PR A (X2): elements a person's later PAPER_AUTO arm acknowledged (this process)
        self._summary_token: Optional[tuple] = None                               # Wave 2 PR A: the accounting rows' state at the last summary scan (None: scan once)
        self._summary_backlog = False
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
        try:                                                                      # Wave 2 PR A (design 3.12.1): stored summaries made current - after the early stop and capture, under their own guard
            out["summaries"] = self._persist_stale_summaries(now)
        except Exception as exc:  # noqa: BLE001 - never into the observer loop: every read stays fresh, so only persistence is deferred
            out["summaries"] = {"result": "ERROR", "error": type(exc).__name__}
            log.warning("forward validation: summary persistence failed (%s)", type(exc).__name__)
            self._touch(now, "summary_builder", "UNKNOWN", "summary persistence failed", type(exc).__name__)
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
        td = (session_date(now) or now.date()).isoformat()
        try:                                                                  # Wave 2 PR B (B-35): one strict read; any failure is NULL, never 0
            c = self.store.query_strict("SELECT (SELECT count(*) FROM execution_intents WHERE state = 'RECONCILIATION_REQUIRED') AS rr, "
                                        "(SELECT count(*) FROM paper_positions WHERE state = 'ATTENTION') AS att, "
                                        "(SELECT count(*) FROM execution_intents WHERE state = 'FAILED' AND updated_at >= ? AND updated_at <= ?) AS failed",
                                        (ny_window(td)[0], _iso(now)))[0]
            counts = {"rr_intents": int(c["rr"]), "attention_positions": int(c["att"]), "failed_intents": int(c["failed"])}
        except Exception:  # noqa: BLE001
            counts = {"rr_intents": None, "attention_positions": None, "failed_intents": None}
        try:
            from execution import activity as XA
            from execution.scheduler import status as sched_status
            stale = XA.worker(sched_status(), True, now).get("stale_after_s")
            counts["worker_stale_after_s"] = float(stale) if stale is not None else None
        except Exception:  # noqa: BLE001
            counts["worker_stale_after_s"] = None
        return {**counts, "at": _iso(now), "trading_date": td, "in_session": is_regular_session(now), "owner": self.owner, "mode": mode,
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

    # ---------------------------------------------------------------- the session record (Wave 2 PR B: classifier v2, design Rev 3.4.1 sections 4-6) --
    def _record_session(self, now: datetime, sample: dict) -> Optional[str]:
        """B-19 steps 3-5 at a confirmed own sample (the confirmation is computed by _backfill_not_run earlier in the same tick): finalize earlier
        OPEN dates, open today (B-22), finalize today on time (B-20); then v1's display counters of today's OPEN row (B-23). Every date is
        contained on its own (B-44): nothing raises into observe(), so the same tick's early stop always runs."""
        from forward_capture import health as FH
        today = sample["trading_date"]
        conf, self._conf = getattr(self, "_conf", None), None

        def finalize_late(d):                                                          # B-44 rows 3-5: contained; escalated after the bounded retry
            try:
                self._finalize(d, now, trigger=conf, late=True)
            except Exception as exc:  # noqa: BLE001
                if self._evidence_read_failed(now, d, "finalize", exc) >= M.EVIDENCE_READ_ATTEMPTS and today > d:
                    try:
                        self._finalize(d, now, trigger=conf, late=True, escalate=True)
                    except Exception as exc2:  # noqa: BLE001
                        self._evidence_read_failed(now, d, "finalize_escalated", exc2)

        def open_today():                                                              # B-22
            if self.store.session(today) is not None:
                return
            w = FH.window(today)
            if w is None:                                                              # a holiday market_hours reads as a session: one event, no row
                if sample.get("in_session") and not self.store.query_strict("SELECT 1 AS n FROM forward_validation_events WHERE kind = 'CALENDAR_CONFLICT' "
                                                                            "AND trading_date = ? LIMIT 1", (today,)):
                    self.store.add_event(now, "CALENDAR_CONFLICT", "MARKET_HOURS_IN_SESSION", {"note": "market_hours reads an exchange holiday as a session; no row"},
                                         trading_date=today, owner=self.owner)
                return
            in_w = self.store.query_strict("SELECT at FROM forward_validation_samples WHERE at >= ? AND at <= ? ORDER BY at, rowid LIMIT 1", (_iso(w[0]), _iso(w[1])))
            first = in_w[0]["at"] if in_w else None
            if conf["at_dt"] >= w[1] and first is None:                                # after the close: open only an observed day or one with FAILED evidence
                ev, _ = self._session_observations(today, conf, False, False)
                if not ev["S"] and M.classify_session_v2(ev)[0] != "FAILED":           # B-24 (D-2): row-based FAILED evidence, or a FAILED end state in G(d)
                    return                                                             # neither observed nor FAILED: nothing to open (v1's S03 r2)
            self.store.open_session(today, now, self.owner, in_session=bool(sample.get("in_session")), started_at=first or conf["at"])
            self.store.update_session(today, now, assessment_json=json.dumps({"opened_by": {"sample_id": conf["sample_id"], "at": conf["at"], "owner": conf["owner"]}},
                                                                             sort_keys=True, separators=(",", ":")))
            self.store.add_event(now, "SESSION_OPENED", None, {"in_session": sample.get("in_session"), "mode": sample.get("mode")}, trading_date=today, owner=self.owner)
            self._touch(now, "session_recorder", "RUN", f"session {today} opened")
        if conf is not None:
            try:
                open_rows = [r["trading_date"] for r in self.store.open_sessions() if r["trading_date"] < today]
            except Exception as exc:  # noqa: BLE001 - B-44: contained, retried at the next confirmed sample
                self._evidence_read_failed(now, today, "open_sessions", exc)
                open_rows = []
            for d in open_rows:
                finalize_late(d)
            try:
                open_today()
            except Exception as exc:  # noqa: BLE001
                self._evidence_read_failed(now, today, "open", exc)
        try:
            row = self.store.session(today)
            if row is not None and row["session_status"] == "OPEN":                      # B-23: display counters only (never classifier inputs), as v1 before its finalize
                owners = list(row["owners"]) + ([self.owner] if self.owner not in row["owners"] else [])
                self.store.update_session(today, now, bot_stop_time=sample["at"], owners_json=json.dumps(owners),
                                          in_session_samples=_n(row["in_session_samples"]) + (1 if sample["in_session"] else 0),
                                          market_open_seen=bool(row["market_open_seen"]) or sample["in_session"],
                                          paper_auto_enabled=bool(row["paper_auto_enabled"]) or (sample["in_session"] and sample["mode"] == "PAPER_AUTO"))
        except Exception as exc:  # noqa: BLE001
            self._evidence_read_failed(now, today, "counters", exc)
        if conf is not None:
            try:
                row = self.store.session(today)
                w = FH.window(today)
                if row is not None and row["session_status"] == "OPEN" and w is not None:
                    cut = w[1] + timedelta(seconds=M.G)
                    if conf["at_dt"] > cut + timedelta(seconds=M.H) and all(self._settled(cut, conf)):
                        self._finalize(today, now, trigger=conf, late=False)          # B-20 on time
            except Exception as exc:  # noqa: BLE001
                self._evidence_read_failed(now, today, "finalize", exc)
        try:
            row = self.store.session(today)
            return row["session_status"] if row else None
        except Exception as exc:  # noqa: BLE001
            self._evidence_read_failed(now, today, "status", exc)
            return None

    def _finalize(self, trading_date: str, now: datetime, *, trigger: dict, late: bool, escalate: bool = False) -> str:
        """B-20 / B-21: classify the OPEN row of `trading_date` from persisted evidence only and write it in ONE update (a closed row is frozen)."""
        row = self.store.session(trading_date)
        if row is None or row["session_status"] != "OPEN":
            return row["session_status"] if row else "NOT_RUN"
        try:
            status, res, assessment, disp = self._assess(trading_date, row, trigger, late, escalate)
        except Exception as exc:  # noqa: BLE001
            if not escalate:
                raise
            err = f"{type(exc).__name__}: {exc}"[:200]                                # B-44 row 5: even the escalated assessment failed - fail-closed, never OPEN forever
            try:
                n = _n(self.store.query_strict("SELECT count(*) AS n FROM forward_validation_events WHERE kind = 'OBSERVER_ERROR' AND code = 'EVIDENCE_READ' "
                                               "AND trading_date = ?", (trading_date,))[0]["n"])
            except Exception:  # noqa: BLE001
                n = None
            prior = row.get("assessment") or {}
            status, res = "DEGRADED", {"notes": ["EVIDENCE_UNREADABLE"]}
            assessment = {**({"opened_by": prior["opened_by"]} if prior.get("opened_by") else {}), "classifier_version": M.CLASSIFIER_VERSION, "codes": ["EVIDENCE_UNREADABLE"],
                          "failed_codes": [], "degraded_codes": ["EVIDENCE_UNREADABLE"], "unavailable_sources": {"assessment": {"error": err, "attempts": n}},
                          "finalized_by": {"sample_id": trigger.get("sample_id"), "at": trigger.get("at"), "owner": trigger.get("owner")}, "finalized_late": bool(late)}
            disp = {"market_close_seen": False, "account_refresh_healthy": False, "cash_flow_healthy": False, "reconciliation_healthy": False, "in_window_samples": None}
        self.store.update_session(trading_date, now, ended_at=_iso(now), market_close_seen=disp["market_close_seen"], session_status=status, notes_code="|".join(res["notes"]),
                                  account_refresh_healthy=disp["account_refresh_healthy"], cash_flow_healthy=disp["cash_flow_healthy"],
                                  reconciliation_healthy=disp["reconciliation_healthy"], classifier_version=M.CLASSIFIER_VERSION,
                                  assessment_json=json.dumps(assessment, sort_keys=True, separators=(",", ":"), default=str))
        self.store.add_event(now, "SESSION_CLOSED", status, {"notes": "|".join(res["notes"]), "in_session_samples": disp["in_window_samples"],
                                                              "classifier_version": M.CLASSIFIER_VERSION, "finalized_late": bool(late)}, trading_date=trading_date, owner=self.owner)
        self._touch(now, "session_recorder", "RUN", f"session {trading_date} closed as {status}")
        try:
            self.store.put_daily(trading_date, now, VERSION, self.daily_summary(trading_date, now))
            self._touch(now, "summary_builder", "RUN", f"daily summary {trading_date} written")
        except Exception as exc:  # noqa: BLE001 - the session record stands even when the summary failed; it is rebuilt on demand
            log.warning("forward validation: daily summary %s failed (%s)", trading_date, type(exc).__name__)
            self._touch(now, "summary_builder", "UNKNOWN", f"daily summary {trading_date} failed", type(exc).__name__)
        return status

    def _session_observations(self, trading_date: str, trigger: dict, late: bool, escalate: bool) -> Tuple[dict, dict]:
        """The v2 evidence of one date (design 3, 5.2, 5.3, B-25, B-26): windows, S(d), the grace samples, suspects, restarts, the row evidence
        over E(d) and the settle state. Every read is strict (B-36): an absent table or column is an unavailable source; with `escalate`, any
        other failing source is unavailable too (B-44 row 5) - never zero evidence."""
        from autorun import calendar as CAL
        from forward_capture import health as FH
        from forward_validation.store import EvidenceSourceAbsent
        unavailable: Dict[str, str] = {}

        def read(source, fn, default):
            try:
                return fn()
            except EvidenceSourceAbsent as exc:
                unavailable[source] = f"absent: {exc}"[:200]
                return default
            except Exception as exc:  # noqa: BLE001
                if not escalate:
                    try:
                        exc.evidence_source = source                                   # B-44 row 3: the EVIDENCE_READ event names its source
                    except AttributeError:
                        pass
                    raise
                unavailable[source] = f"{type(exc).__name__}: {exc}"[:200]
                return default

        def ny(at):
            dt = _parse(at)
            d = session_date(dt) if dt is not None else None
            return d.isoformat() if d else None
        w = FH.window(trading_date)
        d0, d1 = ny_window(trading_date)
        if w is not None:
            w_open, w_close = w[0], w[1]
            cut = w_close + timedelta(seconds=M.G)
        else:                                                                          # a non-market day: its window is the whole NY day (V-09)
            w_open = w_close = None
            cut = _parse(d1) - timedelta(seconds=1)
        lo, hi = d0, _iso(cut)
        r0 = (read("forward_validation_samples", lambda: self.store.query_strict("SELECT MIN(rowid) AS r FROM forward_validation_samples WHERE at >= ? AND at < ?", (d0, d1)),
                   [{}]) or [{}])[0].get("r")
        rows = read("forward_validation_samples", lambda: self.store.samples_from_rowid(int(r0)), []) if r0 else []
        prev0 = read("forward_validation_samples", lambda: self.store.query_strict("SELECT rowid AS _rowid, sample_id, at, owner FROM forward_validation_samples WHERE rowid < ? "
                                                                                  "ORDER BY rowid DESC LIMIT 1", (int(r0),)), []) if r0 else []
        prev0 = prev0[0] if prev0 else None
        suspects = self._suspects(rows)
        full = read("forward_validation_samples", lambda: self.store.samples_between(_iso(w_open), hi), []) if w is not None else []
        for s in full:
            s["at_iso"], s["at"], s["last_cycle_at_dt"] = s["at"], _parse(s["at"]), _parse(s.get("last_cycle_at"))
        S = [s for s in full if s["sample_id"] not in suspects and s["at"] is not None and w_open <= s["at"] <= w_close]
        grace = [s for s in full if s["sample_id"] not in suspects and s["at"] is not None and w_close < s["at"] <= cut]
        affects = False
        order = [r["_rowid"] for r in rows]
        for i, r in enumerate(rows):                                                   # B-16: the suspect's own date and its anchor's date
            if r["sample_id"] not in suspects:
                continue
            anchor = next((rows[j] for j in range(i - 1, -1, -1) if rows[j]["sample_id"] not in suspects), None) or prev0
            if anchor is None:
                anchor = next((rows[j] for j in range(i + 1, len(order)) if rows[j]["sample_id"] not in suspects), None)
            if trading_date in ({ny(r["at"])} | ({ny(anchor["at"])} if anchor else set())):
                affects = True
                break
        restarts = self._restart_times(rows, prev0, w_open, w_close, trading_date, unavailable, escalate) if w is not None else []
        failed_rows, row_degraded = self._row_evidence(lo, hi, read)
        exec_ok, refresh_ok = read("settle", lambda: self._settled(cut if w is not None else _parse(d1), trigger), (False, False))
        ev = {"market_day": w is not None, "window": (w_open, w_close) if w is not None else None, "covered": CAL.covered(date.fromisoformat(trading_date)), "S": S,
              "end_state": ([S[-1]] if S else []) + grace, "suspect_affects": affects, "clock_inconsistent": affects, "restarts": restarts,
              "failed_rows": failed_rows, "row_degraded": row_degraded, "unavailable": dict(unavailable), "not_settled": bool(late) and not (exec_ok and refresh_ok),
              "min_coverage": FH.MIN_COVERAGE}
        info = {"window": {"open": _iso(w_open) if w_open else None, "close": _iso(w_close) if w_close else None, "cutoff": hi, "minutes": w[2] if w is not None else None},
                "suspects": len([r for r in rows if r["sample_id"] in suspects]), "settled": {"executor": exec_ok, "refresh": refresh_ok}}
        return ev, info

    def _backfill_not_run(self, now: datetime, today: str) -> List[str]:
        """B-19 steps 1-2 (observe calls this first): the clock check (B-27) at every own sample; then, at a confirmed own sample only,
        the session accounting of past days (B-24). Returns the dates back-filled NOT_RUN. Contained (B-44 row 3): nothing escapes into observe()."""
        try:
            self._clock_check(now)
            self._conf = self._confirmed(now)
        except Exception as exc:  # noqa: BLE001 - B-44 row 3: the clock or confirmation read failed - not confirmed, one event, no transition
            self._evidence_read_failed(now, today, "confirm", exc)
            self._conf = None
        if self._conf is None:
            return []
        try:
            return self._account_days(now, today, self._conf)
        except Exception as exc:  # noqa: BLE001 - B-44 row 3: the accounting itself failed (each date is contained inside it)
            self._evidence_read_failed(now, today, "accounting_pass", exc)
            return []

    # ---------------------------------------------------------------- Wave 2 PR B: helpers of the classifier v2 ------------------------------
    def _confirmed(self, now: datetime) -> Optional[dict]:
        """B-13: the latest own sample when it and its two own predecessors are joined by two plausible steps (0 <= step <= G); else None."""
        tail = self.store.own_tail(self.owner, M.K)
        if len(tail) < M.K:
            return None
        ats = [_parse(s["at"]) for s in tail]
        if any(a is None for a in ats) or not all(0 <= (b - a).total_seconds() <= M.G for a, b in zip(ats, ats[1:])):
            return None
        return {**tail[-1], "at_dt": ats[-1]}

    @staticmethod
    def _suspects(rows: List[dict]) -> set:
        """B-14: the sample ids of `rows` (by rowid) that a later-inserted sample of ANY owner proves clock-suspect (t.at < s.at - h)."""
        out, later_min = set(), None
        for r in reversed(rows):
            at = _parse(r["at"])
            if at is None:
                continue
            if later_min is not None and later_min < at - timedelta(seconds=M.H):
                out.add(r["sample_id"])
            later_min = at if later_min is None else min(later_min, at)
        return out

    def _clock_check(self, now: datetime) -> Optional[dict]:
        """B-27: at each own sample t, when t.at < MAX(at of earlier samples) - h, write ONE retroactive CLOCK_INCONSISTENT event naming the
        samples t newly proves suspect (each suspect is named once)."""
        def ny(at):
            dt = _parse(at)
            d = session_date(dt) if dt is not None else None
            return d.isoformat() if d else None
        tail = self.store.own_tail(self.owner, 1)
        if not tail:
            return None
        t = tail[-1]
        ta, ma = _parse(t["at"]), _parse(self.store.max_at_before(int(t["_rowid"])))
        if ta is None or ma is None or ta >= ma - timedelta(seconds=M.H):
            return None
        proven = self.store.query_strict("SELECT rowid AS _rowid, sample_id, at, owner FROM forward_validation_samples WHERE rowid < ? AND at > ? ORDER BY rowid",
                                         (int(t["_rowid"]), _iso(ta + timedelta(seconds=M.H))))
        known = set()
        for e in self.store.query_strict("SELECT detail_json FROM forward_validation_events WHERE kind = 'CLOCK_INCONSISTENT'"):
            try:
                known |= {str(s.get("sample_id")) for s in (json.loads(e["detail_json"] or "{}").get("suspects") or [])}
            except (ValueError, AttributeError):
                continue
        new = [p for p in proven if p["sample_id"] not in known]
        if not new:
            return None
        ids = {p["sample_id"] for p in new} | known                                    # every suspect: never an anchor
        mine = {p["sample_id"] for p in new}
        seq = self.store.query_strict("SELECT rowid AS _rowid, sample_id, at FROM forward_validation_samples WHERE rowid <= ? ORDER BY rowid", (int(t["_rowid"]),))
        prev_ok, last = [], None                                                       # one pass each way, however many suspects (a large clock jump)
        for r in seq:
            prev_ok.append(last)
            if r["sample_id"] not in ids:
                last = r
        next_ok, nxt = [None] * len(seq), None
        for j in range(len(seq) - 1, -1, -1):
            next_ok[j] = nxt
            if seq[j]["sample_id"] not in ids:
                nxt = seq[j]
        pos = {r["sample_id"]: i for i, r in enumerate(seq)}
        affected = set()
        for p in new:                                                                  # B-16: the suspect's date and its anchor's (the latest earlier non-suspect, else the earliest later one)
            affected.add(ny(p["at"]))
            i = pos.get(p["sample_id"])
            anchor = (prev_ok[i] or next_ok[i]) if i is not None else None
            if anchor is not None:
                affected.add(ny(anchor["at"]))
        triggered = []
        for r in self.store.query_strict("SELECT trading_date, assessment_json FROM forward_validation_sessions WHERE assessment_json IS NOT NULL"):
            try:
                a = json.loads(r["assessment_json"] or "{}")
            except ValueError:
                continue
            if any(((a.get(k) or {}).get("sample_id") in mine) for k in ("opened_by", "finalized_by", "backfilled_by")):   # B-27: the rows N triggered
                triggered.append(r["trading_date"])
        detail = {"proven_by": {"sample_id": t["sample_id"], "at": t["at"], "owner": t["owner"]},
                  "suspects": [{"sample_id": p["sample_id"], "at": p["at"], "owner": p["owner"]} for p in new],
                  "affected_dates": sorted(d for d in affected if d), "rows_triggered": sorted(triggered)}
        self.store.add_event(now, "CLOCK_INCONSISTENT", "RETROACTIVE", detail, trading_date=ny(t["at"]), owner=self.owner)
        return detail

    def _restart_times(self, rows: List[dict], prev0: Optional[dict], w_open, w_close, trading_date: str, unavailable: Optional[dict] = None,
                       escalate: bool = False) -> List[str]:
        """B-25: the restart time of every owner change (by rowid, any owner) on the date: the matching autorun STARTUP (detail.pid = the new
        owner's pid, previous owner's last sample <= at <= first sample), else the first sample. Only times in (W.open, W.close]. A restart
        time that stays unknown (no matching STARTUP, or the lookup unreadable) for a change that straddles the close is counted at W.close,
        and an unreadable lookup follows B-44 (R-4): an absent table, or an escalated read, is unavailable at once; any other failure raises
        (contained by the caller and retried). Never HEALTHY on an unknown restart time."""
        from forward_validation.store import EvidenceSourceAbsent
        out, prev = [], prev0
        for r in rows:
            dt = _parse(r["at"])
            if prev is not None and r["owner"] != prev["owner"] and dt is not None and session_date(dt) and session_date(dt).isoformat() == trading_date:
                t, matched, failed = dt, False, None
                pid = str(r["owner"]).split(":")[0]
                try:
                    evs = self.store.query_strict("SELECT at, detail_json FROM autorun_events WHERE kind = 'STARTUP' AND at >= ? AND at <= ? ORDER BY at DESC, rowid DESC",
                                                  (prev["at"], r["at"]))
                except Exception as exc:  # noqa: BLE001 - absent or unreadable: no STARTUP known
                    evs, failed = [], exc
                for e in evs:
                    try:
                        det = json.loads(e["detail_json"] or "{}")
                    except ValueError:
                        det = {}
                    if str(det.get("pid")) == pid and _parse(e["at"]) is not None:
                        t, matched = _parse(e["at"]), True
                        break
                pa = _parse(prev["at"])
                if not matched and pa is not None and pa <= w_close < dt:                 # R-4: the restart may lie before the close - counted, never hidden
                    t = w_close
                    if failed is not None and not (escalate or isinstance(failed, EvidenceSourceAbsent)):
                        try:
                            failed.evidence_source = "autorun_events"                         # B-44 rows 3-4: contained by the caller, retried
                        except AttributeError:
                            pass
                        raise failed
                    if failed is not None and unavailable is not None:
                        unavailable.setdefault("autorun_events", f"{type(failed).__name__}: {failed}"[:200])
                if w_open < t <= w_close:
                    out.append(_iso(t))
            prev = r
        return out

    def _settled(self, cut: datetime, trigger: dict) -> Tuple[bool, bool]:
        """B-17: (S2) the trigger sample's last completed executor cycle started after the cutoff; (S3) an account-refresh attempt started in
        (cutoff, trigger.at]. Strict reads; a failure raises (contained by the caller)."""
        row = self.store.query_strict("SELECT last_cycle_at FROM forward_validation_samples WHERE sample_id = ?", (trigger["sample_id"],))
        lc = _parse(row[0]["last_cycle_at"]) if row else None
        n = self.store.query_strict("SELECT count(*) AS n FROM account_refresh_attempts WHERE started_at > ? AND started_at <= ?", (_iso(cut), trigger["at"]))
        return bool(lc is not None and lc > cut), bool(n and _n(n[0]["n"]) > 0)

    def _duplicate_evidence(self, lo: str, hi: str) -> int:
        """DUPLICATE_BROKER_ORDER, the rule deployed (v1; PR C's W2-6 changes it together with order_reliability, T-S46): client-order-id groups
        with >= 2 members first seen <= hi and one in [lo, hi] (V-12), plus MULTIPLE_ORDERS / UNKNOWN_INTERNAL_ORDER findings in [lo, hi]."""
        groups: Dict[Any, List[str]] = {}
        for r in self.store.query_strict("SELECT client_order_id, first_seen_at FROM execution_orders WHERE first_seen_at <= ?", (hi,)):
            groups.setdefault(r["client_order_id"], []).append(str(r["first_seen_at"] or ""))
        n = sum(1 for v in groups.values() if len(v) >= 2 and any(lo <= x <= hi for x in v))
        rec = self.store.query_strict("SELECT count(*) AS n FROM execution_reconciliations WHERE kind IN ('MULTIPLE_ORDERS', 'UNKNOWN_INTERNAL_ORDER') AND at >= ? AND at <= ?", (lo, hi))
        return n + _n(rec[0]["n"] if rec else 0)

    def _live_host_evidence(self, lo: str, hi: str) -> int:
        """LIVE_HOST_ATTEMPT: the transport's refusal text in the append-only sources with a stamp in [lo, hi], plus a mutable control row whose
        mark is present with updated_at >= lo and NO upper bound (U-11, fail-closed)."""
        like, n = f"%{LIVE_HOST_MARK}%", 0
        for table, col, at in (("account_refresh_attempts", "error_text", "started_at"), ("cash_flow_refresh_attempts", "error_text", "started_at"),
                               ("execution_reconciliations", "detail_json", "at"), ("execution_runs", "error", "started_at")):
            n += _n(self.store.query_strict(f"SELECT count(*) AS n FROM {table} WHERE {col} LIKE ? AND {at} >= ? AND {at} <= ?", (like, lo, hi))[0]["n"])
        n += _n(self.store.query_strict("SELECT count(*) AS n FROM execution_controls WHERE last_error LIKE ? AND updated_at >= ?", (like, lo))[0]["n"])
        return n

    def _row_evidence(self, lo: str, hi: str, read) -> Tuple[Dict[str, int], Dict[str, int]]:
        """The row-based FAILED and DEGRADED evidence over E(d) = [lo, hi] by event time (5.2, 5.3); `read` marks a failing source unavailable."""
        q = self.store.query_strict

        def count(sql, args):
            return _n(q(sql, args)[0]["n"])

        def gate():                                                                    # V-11: strict probes before the pinned tolerant reader
            q("SELECT execution_id, symbol, submitted_at, attempt_count, intent_type FROM execution_intents LIMIT 0")
            q("SELECT state, allowed_new_entry, evaluated_at, reason_codes_json FROM execution_safety_evaluations LIMIT 0")
            return len(self._gate_violations(lo, _iso(_parse(hi) + timedelta(seconds=1))))
        failed = {"DUPLICATE_BROKER_ORDER": read("execution_orders+execution_reconciliations", lambda: self._duplicate_evidence(lo, hi), 0),
                  "LIVE_HOST_ATTEMPT": read("live_host_sources", lambda: self._live_host_evidence(lo, hi), 0),
                  "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE": read("execution_intents+execution_safety_evaluations", gate, 0),
                  "EARLY_STOP": read("forward_validation_events", lambda: count("SELECT count(*) AS n FROM forward_validation_events WHERE kind = 'EARLY_STOP' AND at >= ? AND at <= ?",
                                                                               (lo, hi)), 0)}
        runs_err = read("execution_runs", lambda: count("SELECT count(*) AS n FROM execution_runs WHERE result = 'ERROR' AND started_at >= ? AND started_at <= ?", (lo, hi)), 0)
        acct = read("account_refresh_attempts", lambda: q("SELECT result, error_code FROM account_refresh_attempts WHERE started_at >= ? AND started_at <= ?", (lo, hi)), [])
        cf = read("cash_flow_refresh_attempts", lambda: q("SELECT result, error_code FROM cash_flow_refresh_attempts WHERE started_at >= ? AND started_at <= ?", (lo, hi)), [])
        bu = read("execution_reconciliations", lambda: count("SELECT count(*) AS n FROM execution_reconciliations WHERE kind = 'BROKER_UNAVAILABLE' AND at >= ? AND at <= ?", (lo, hi)), 0)
        acct_bad = [r for r in acct if r.get("result") != "SUCCESS"]
        cf_bad = [r for r in cf if r.get("result") not in ("SUCCESS", "INCOMPLETE")]
        degraded = {"EXECUTOR_CYCLE_ERRORS": runs_err, "REFRESH_FAILURES": len(acct_bad) + len(cf_bad),
                    "BROKER_ERRORS": sum(1 for r in acct_bad + cf_bad if r.get("error_code")) + runs_err + bu}   # the v1 broker_health counting
        return failed, degraded

    def _assess(self, trading_date: str, row: Optional[dict], trigger: dict, late: bool, escalate: bool) -> Tuple[str, dict, dict, dict]:
        """Classify one date (design 5) and build its assessment record (B-37) and v1's display columns."""
        ev, info = self._session_observations(trading_date, trigger, late, escalate)
        status, res = M.classify_session_v2(ev)
        S, end = ev["S"], ev["end_state"]
        last = S[-1] if S else None
        gaps = res["gaps"]
        prior = (row or {}).get("assessment") or {}
        attempts = None
        if ev["unavailable"]:                                                          # B-37: each unavailable source with the date's attempt count
            try:
                attempts = _n(self.store.query_strict("SELECT count(*) AS n FROM forward_validation_events WHERE kind = 'OBSERVER_ERROR' AND code = 'EVIDENCE_READ' "
                                                      "AND trading_date = ?", (trading_date,))[0]["n"])
            except Exception:  # noqa: BLE001
                attempts = None

        def brief(s):
            return {"sample_id": s.get("sample_id"), "at": s.get("at_iso"), "rr_intents": s.get("rr_intents"), "attention_positions": s.get("attention_positions"),
                    "failed_intents": s.get("failed_intents"), "safety_state": s.get("safety_state")}
        assessment = {**({"opened_by": prior["opened_by"]} if prior.get("opened_by") else {}), "classifier_version": M.CLASSIFIER_VERSION,
                      "window": info["window"], "covered": ev["covered"], "measures": res["measures"],
                      "gaps": {"open": gaps[0] if gaps else None, "close": gaps[-1] if gaps else None, "max_interior": max(gaps[1:-1]) if len(gaps) > 2 else None,
                               "over_G": sum(1 for g in gaps if g > M.G)},
                      "samples": {"in_window": len(S), "grace": len(end) - (1 if last else 0), "suspect_excluded": info["suspects"]}, "restarts": ev["restarts"],
                      "codes": res["notes"], "failed_codes": res["failed_codes"], "degraded_codes": res["degraded_codes"],
                      "unavailable_sources": {k: {"error": v, "attempts": attempts} for k, v in ev["unavailable"].items()},
                      "failed_evidence": {k: v for k, v in ev["failed_rows"].items() if v}, "row_degraded_evidence": {k: v for k, v in ev["row_degraded"].items() if v},
                      "end_state": [brief(s) for s in end], "settled": info["settled"],
                      "finalized_by": {"sample_id": trigger.get("sample_id"), "at": trigger.get("at"), "owner": trigger.get("owner")}, "finalized_late": bool(late)}
        disp = {"market_close_seen": bool(S) and bool(gaps) and gaps[-1] <= M.G,
                "account_refresh_healthy": bool(S) and any(s.get("account_status") == "FRESH" for s in S)
                and not any(s.get("account_status") in ("STALE", "UNAVAILABLE", "DISABLED") for s in S),
                "cash_flow_healthy": bool(S) and any(s.get("cash_flow_status") == "FRESH" for s in S)
                and not any(s.get("cash_flow_status") in ("STALE", "UNAVAILABLE", "AMBIGUOUS") for s in S),
                "reconciliation_healthy": bool(S) and not any(s.get("reconciliation_healthy") is False for s in S) and last is not None
                and last.get("rr_intents") == 0 and last.get("attention_positions") == 0, "in_window_samples": len(S)}
        return status, res, assessment, disp

    def _account_days(self, now: datetime, today: str, conf: dict) -> List[str]:
        """B-24: every past market day without a row that is observed or holds FAILED evidence is opened and finalized (late); then the market
        days strictly between L* and today with neither are back-filled NOT_RUN (written at INSERT). A date whose evidence cannot be read is
        retried, then escalated (B-44) - never NOT_RUN. Returns the NOT_RUN dates."""
        from autorun import calendar as CAL
        from forward_capture import health as FH
        try:
            l0 = (self.store.query_strict("SELECT MAX(trading_date) AS d FROM forward_validation_sessions WHERE trading_date < ?", (today,)) or [{}])[0].get("d")
            have = {r["trading_date"] for r in self.store.query_strict("SELECT trading_date FROM forward_validation_sessions")}
            dated = {r["trading_date"] for r in self.store.query_strict("SELECT DISTINCT trading_date FROM forward_validation_samples WHERE trading_date < ?", (today,))}
            pending = {r["trading_date"] for r in self.store.query_strict("SELECT DISTINCT trading_date FROM forward_validation_events WHERE kind = 'OBSERVER_ERROR' "
                                                                           "AND code = 'EVIDENCE_READ' AND trading_date < ?", (today,)) if r["trading_date"]}   # B-44: retried until resolved
        except Exception as exc:  # noqa: BLE001
            self._evidence_read_failed(now, today, "accounting", exc)
            return []
        evidence: Dict[str, Optional[dict]] = {}
        escalated: set = set()

        def market(d):
            try:
                return d not in have and d < today and FH.window(d) is not None
            except Exception as exc:  # noqa: BLE001 - B-44 row 3: a malformed candidate date is skipped (logged, not an event per tick), never aborting the others
                log.warning("forward validation v2: candidate date %r skipped (%s)", d, type(exc).__name__)
                return False

        def evaluate(d):                                                               # None: unreadable now (retried next tick, escalated after the bound)
            if d in evidence:
                return evidence[d]
            try:
                ev = self._session_observations(d, conf, True, False)[0]
                ev["_class"] = M.classify_session_v2(ev)[0]                            # inside the per-date containment (B-44 row 3)
                evidence[d] = ev
            except Exception as exc:  # noqa: BLE001
                evidence[d] = None
                if self._evidence_read_failed(now, d, "accounting", exc) >= M.EVIDENCE_READ_ATTEMPTS:
                    escalated.add(d)                                                   # B-44 row 5: the bound is reached (the date has ended: d < today)
                    try:
                        ev = self._session_observations(d, conf, True, True)[0]
                        evidence[d] = ev                                               # kept even if only the classifier raises below (N-1)
                        ev["_class"] = M.classify_session_v2(ev)[0]
                    except Exception as exc2:  # noqa: BLE001 - even the escalated evaluation failed: unreadable, opened and finalized fail-closed below
                        self._evidence_read_failed(now, d, "accounting_escalated", exc2)
                        if evidence[d] is not None:
                            evidence[d]["_class"] = "DEGRADED"                         # its samples still decide (a): an observed day keeps its row and start
                        else:
                            evidence[d] = {"S": [], "failed_rows": {}, "unavailable": {"accounting": f"{type(exc2).__name__}: {exc2}"[:200]}, "suspect_affects": False,
                                           "_class": "DEGRADED"}
            return evidence[d]

        def open_and_finalize(d, ev):                                                  # B-24 (a): opened with its real start, finalized at once (late)
            try:
                first = ev["S"][0]["at_iso"] if ev["S"] else conf["at"]
                if self.store.session(d) is None:
                    self.store.open_session(d, now, self.owner, in_session=False, started_at=first)
                    self.store.update_session(d, now, assessment_json=json.dumps({"opened_by": {"sample_id": conf["sample_id"], "at": conf["at"], "owner": conf["owner"]}},
                                                                                 sort_keys=True, separators=(",", ":")))
                    self.store.add_event(now, "SESSION_OPENED", None, {"accounted": True, "started_at": first}, trading_date=d, owner=self.owner)
                have.add(d)
                try:
                    self._finalize(d, now, trigger=conf, late=True, escalate=d in escalated)   # a structurally absent source is unavailable at once anyway (row 6)
                except Exception as exc:  # noqa: BLE001 - the row stays OPEN; the late-finalize path of _record_session retries it
                    self._evidence_read_failed(now, d, "finalize", exc)
                return True
            except Exception as exc:  # noqa: BLE001
                self._evidence_read_failed(now, d, "account_open", exc)
                return False
        accounted = []
        for d in sorted(x for x in dated | pending | set(weekdays_between(l0, today) if l0 else []) if market(x)):
            ev = evaluate(d)
            if ev is not None and (ev["S"] or ev["_class"] == "FAILED") and open_and_finalize(d, ev):   # D-2: incl. a FAILED end state in G(d)
                accounted.append(d)
        lstar = l0 or (min(accounted) if accounted else None)
        done: List[str] = []
        if lstar is None:
            return done
        for d in sorted(x for x in set(weekdays_between(lstar, today)) | pending if market(x)):        # a pending date stays a candidate after L0 passes it
            ev = evaluate(d)
            if ev is None:
                continue                                                               # unreadable, not yet escalated: never NOT_RUN
            if ev["_class"] != "NOT_RUN":                                                  # D-2 / B-20: observed, FAILED, unreadable, unknown or clock-suspect: a row
                open_and_finalize(d, ev)
                continue
            try:
                notes = "NO_OBSERVATION" + ("" if CAL.covered(date.fromisoformat(d)) else "|CALENDAR_NOT_COVERED")
                self.store.insert_not_run(d, now, notes, classifier_version=M.CLASSIFIER_VERSION,
                                          assessment={"backfilled_by": {"sample_id": conf["sample_id"], "at": conf["at"], "owner": conf["owner"]},
                                                      "classifier_version": M.CLASSIFIER_VERSION, "codes": notes.split("|")})
                have.add(d)
                self.store.add_event(now, "SESSION_NOT_RUN", "NO_OBSERVATION", {"note": "no observer sample inside this market day's regular session and no FAILED evidence"},
                                     trading_date=d, owner=self.owner)
                done.append(d)
            except Exception as exc:  # noqa: BLE001
                self._evidence_read_failed(now, d, "backfill", exc)
        return done

    def _evidence_read_failed(self, now: datetime, trading_date: Optional[str], phase: str, exc: BaseException) -> int:
        """B-44 row 3: record one OBSERVER_ERROR / EVIDENCE_READ event for the date (guarded) and return the persisted attempt count."""
        log.warning("forward validation v2: %s of %s failed (%s)", phase, trading_date, type(exc).__name__)
        try:
            self.store.add_event(now, "OBSERVER_ERROR", "EVIDENCE_READ", {"phase": phase, "source": getattr(exc, "evidence_source", None), "error": type(exc).__name__,
                                                                          "text": str(exc)[:200]},
                                 trading_date=trading_date, owner=self.owner)
        except Exception:  # noqa: BLE001 - the event itself failed: logged; the date is retried, never silently HEALTHY
            log.warning("forward validation v2: the EVIDENCE_READ event of %s was not recorded", trading_date)
        try:
            return _n(self.store.query_strict("SELECT count(*) AS n FROM forward_validation_events WHERE kind = 'OBSERVER_ERROR' AND code = 'EVIDENCE_READ' "
                                              "AND trading_date = ?", (trading_date,))[0]["n"])
        except Exception:  # noqa: BLE001
            return 0

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
        """The Stage 6.1 early stop, consumed per incident (Wave 2 PR A, X2; design 3.7.2): a critical code is announced once per process, but a
        stop is recorded only when the PERSISTED mode confirms it, and only a person's later PAPER_AUTO arm acknowledges the elements stopped. An
        element not yet acknowledged always stops; an unconfirmed stop is retried on the next tick. Its one action: Paper Auto -> Monitor only."""
        critical = sorted({a["code"] for a in att if a["critical"]})
        if not critical:
            self._touch(now, "attention_monitor", "PASS" if not att else "WARN", f"{len(att)} condition(s)")
            return None
        self._touch(now, "attention_monitor", "BLOCK", "critical: " + ", ".join(critical))
        for c in [c for c in critical if c not in self._stopped_for]:                # announced once per process per code (as before)
            try:
                self.store.add_event(now, "ATTENTION_RAISED", c, {"detail": next((a["detail"] for a in att if a["code"] == c), None)}, trading_date=sample["trading_date"], owner=self.owner)
                self._stopped_for.add(c)                                              # announcement only: nothing is consumed here (PF-12)
            except Exception as exc:  # noqa: BLE001 - L-04: a failed announcement never skips the stop; it is retried on the next tick
                log.warning("forward validation: attention %s not recorded (%s)", c, type(exc).__name__)
        if not self.early_stop_enabled:
            return {"critical": critical, "stopped": False}
        humans = sorted(HUMAN_ACTORS)
        for c, (elements, r_stop) in list(self._stopped.items()):                     # a person's PAPER_AUTO arm after a confirmed stop acknowledges it (any mode)
            try:
                arms = self.store.query(f"SELECT 1 AS n FROM execution_mode_events WHERE rowid > ? AND to_mode = 'PAPER_AUTO' AND actor IN ({', '.join('?' * len(humans))}) LIMIT 1",
                                        (r_stop, *humans))
            except Exception:  # noqa: BLE001 - G-05: an unreadable acknowledgement acknowledges nothing
                arms = []
            if arms:
                self._acked[c] = self._acked.get(c, frozenset()) | elements
        try:
            row = self.store.query("SELECT mode FROM execution_mode WHERE id = 1")
            armed = known = (row[0]["mode"] if row else "MONITOR_ONLY") == "PAPER_AUTO"
        except Exception:  # noqa: BLE001 - an unreadable mode is treated as armed: the stop is attempted, never skipped
            armed, known = True, False                                                # L-03: but never RECORDED - it may have been Monitor only already
        if not armed:
            return {"critical": critical, "stopped": False}
        elements: Dict[str, frozenset] = {}
        for c in critical:
            try:
                el = signature(self.store, c)
            except Exception:  # noqa: BLE001 - G-05: an unreadable signature stops (UNSIGNED), never skips
                el = frozenset()
            elements[c] = el or frozenset({("UNSIGNED", c)})
        new = [c for c in critical if not elements[c] <= self._acked.get(c, frozenset())]
        if not new:
            return {"critical": critical, "stopped": False}
        reason = "Stage 6.1 early stop: " + ", ".join(ATTENTION_TEXT.get(c, c) for c in new)
        try:
            self.execution.disable_auto(actor="forward_validation", reason=reason, now=now)
        except Exception as exc:  # noqa: BLE001 - judged by the persisted mode below; an unconfirmed stop is retried on the next tick
            log.error("forward validation early stop failed (%s)", type(exc).__name__)
        try:
            row = self.store.query("SELECT (SELECT mode FROM execution_mode WHERE id = 1) AS mode, (SELECT MAX(rowid) FROM execution_mode_events) AS r")
            stopped = known and bool(row) and row[0]["mode"] == "MONITOR_ONLY"
            r_stop = int(row[0]["r"] or 0) if stopped else 0
        except Exception:  # noqa: BLE001 - G-05: an unreadable mode is an unconfirmed stop (retried)
            stopped, r_stop = False, 0
        if stopped:
            for c in new:
                self._stopped[c] = (elements[c], r_stop)
        try:
            self.store.add_event(now, "EARLY_STOP", ",".join(new), {"reason": reason, "stopped": stopped, "elements": {c: len(elements[c]) for c in new}},
                                 trading_date=sample["trading_date"], owner=self.owner)
        except Exception as exc:  # noqa: BLE001 - L-04: the stop itself is already done and recorded in memory
            log.warning("forward validation: early stop event not recorded (%s)", type(exc).__name__)
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
        return [self._fail_closed_trade(self._trade(p)) for p in rows[:limit]]       # Wave 2 PR A: Rule F on every forward-validation surface

    def verification(self, p) -> Dict[str, Any]:
        """Wave 2 PR A (design 3.1.4, G-09): whether a closed paper trade's P/L is VERIFIED evidence, from ONE read transaction of the executor's
        rows (ExecutionStore.accounting_view). Every failed clause is named in unverified_reasons. Never raises (6.10A capture calls _trade): a
        read failure is VERIFY_ERROR - unverified, and a summary that sees it is UNSTABLE, never persisted."""
        try:
            v = self.execution.store.accounting_view(p.position_id)
            if v is None:
                return {"verified": False, "unverified_reasons": ["NOT_CLOSED"]}
            pos, ent, exits, snap = v["position"], v["entry"], v["exits"], (v["snapshot"] or {}).get("pnl_snapshot")
            if any(getattr(p, k) != getattr(pos, k) for k in ("state", "quantity", "entry_qty", "exit_qty", "average_entry_price", "average_exit_price", "realized_pnl", "closed_at")):
                return {"verified": False, "unverified_reasons": ["VERIFY_ERROR"]}            # checkpoint 1 K-01: the caller's values were read before a later write - never vouch for them
            intents = ([ent] if ent is not None else []) + list(exits)
            e = XD(pos.average_entry_price)
            filled = [x for x in exits if x.filled_qty > 0]
            r = set()
            if pos.state != "CLOSED" or pos.quantity != 0:
                r.add("NOT_CLOSED")
            if pos.exit_qty != pos.entry_qty:
                r.add("QTY_MISMATCH")
            if pos.realized_pnl is None:
                r.add("PNL_NULL")
            if e is None or e <= 0:
                r.add("ENTRY_PRICE_INVALID")
            if any(i.state in INTENT_OPEN for i in intents):                         # F-03: a working entry or exit can still change the P/L
                r.add("INTENT_OPEN")
            if ent is None or ent.filled_qty != pos.entry_qty or XD(ent.filled_avg_price) != e:
                r.add("ENTRY_MISMATCH")
            if any(XD(x.filled_avg_price) is None or XD(x.filled_avg_price) <= 0 for x in filled):
                r.add("EXIT_PRICE_MISSING")
            if sum(x.filled_qty for x in filled) != pos.exit_qty:                     # G-09
                r.add("EXIT_QTY_SUM_MISMATCH")
            if not r & {"PNL_NULL", "ENTRY_PRICE_INVALID", "EXIT_PRICE_MISSING"}:     # F-06: the row's P/L re-derived from its intents, as the executor computes it
                n = sum((XD(x.filled_avg_price) * x.filled_qty for x in filled), Decimal(0))
                if XD(xmoney(n - e * pos.exit_qty)) != XD(pos.realized_pnl):
                    r.add("REDERIVATION_MISMATCH")
            if not isinstance(snap, dict):                                            # F-04 / E3: the latest snapshot-bearing event must equal the row
                r.add("SNAPSHOT_MISSING")
            elif (snap.get("entry_qty"), snap.get("exit_qty")) != (pos.entry_qty, pos.exit_qty) or any(
                    XD(snap.get(k)) != XD(getattr(pos, k)) for k in ("average_entry_price", "average_exit_price", "realized_pnl")):
                r.add("SNAPSHOT_MISMATCH")
            if any(i.failure_code == "NOT_FOUND_AFTER_LOOKUPS" for i in intents):     # G-09: the broker never confirmed it
                r.add("INTENT_NOT_FOUND_AT_BROKER")
            if any(i.outcome_class in AMBIGUOUS_CLASSES for i in intents):
                r.add("INTENT_AMBIGUOUS")
            return {"verified": not r, "unverified_reasons": sorted(r)}
        except Exception as exc:  # noqa: BLE001 - never into capture or a report; unverified, and the summary that sees it is UNSTABLE
            log.warning("forward validation: verification of %s failed (%s)", getattr(p, "position_id", "?"), type(exc).__name__)
            return {"verified": False, "unverified_reasons": ["VERIFY_ERROR"]}

    @staticmethod
    def _fail_closed_trade(t: dict) -> dict:
        """Wave 2 PR A Rule F (design 3.12.3): an unverified trade's P/L is null - never 0 - on every forward-validation surface; the values as
        recorded stay beside it as labelled audit evidence. Applied by closed_trades() and trace() only: 6.10A capture reads _trade() unchanged."""
        keys = ("realized_pnl", "gross_pnl", "net_pnl", "return_pct")
        out = {**t, "recorded_pnl": {**{k: t.get(k) for k in keys}, "excursions": t.get("excursions"), "entry_slippage": t.get("entry_slippage"),
                                     "basis": "as recorded by the executor; audit evidence, not a verified result"}}
        if not t.get("verified"):
            out.update({k: None for k in keys})
            if not (XD(t.get("entry_price")) or 0) > 0:                                # J-02 / K-07: derived from the "0.00" placeholder, not from a price
                out["excursions"] = {**(t.get("excursions") or {}), **{k: None for k in ("mfe_pnl", "mae_pnl", "mfe_pct", "mae_pct")}}
                out["entry_slippage"] = {**(t.get("entry_slippage") or {}), "slippage_absolute": None, "slippage_bps": None}
        return out

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
                                   "note": "no in-process market trend / volatility source exists; the setup inputs (stretch, confidence, demand, evidence) are the recorded context"},
                **self.verification(p)}                                                      # Wave 2 PR A: verified + unverified_reasons (every other key unchanged)

    def trace(self, position_id: str) -> Optional[dict]:
        """The end-to-end audit trail of one closed (or open) paper trade from persisted rows only; no model, no inference."""
        x = self.execution
        p = self._safe(lambda: x.store.position(position_id))
        if p is None:
            return None
        t = self._fail_closed_trade(self._trade(p))                                      # Wave 2 PR A: Rule F (the raw values in recorded_pnl)
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
                "final_pnl": {"realized_pnl": p.realized_pnl, **M.trade_pnl(p.average_entry_price, p.average_exit_price, p.exit_qty or p.entry_qty),
                              **({} if t["verified"] else {k: None for k in ("realized_pnl", "gross_pnl", "net_pnl", "return_pct")}), "verified": t["verified"]}}

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
        wl = M.win_loss([t["realized_pnl"] for t in trades])                         # Wave 2 PR A: verified trades only (Rule F nulls the others; no gross fallback, W2-N05)
        funnel = self.funnel(trading_date)
        rel = self.order_reliability(trading_date)
        rec = self.reconciliation(trading_date)
        fires = self.safety_fires(trading_date)
        fr = self.freshness(trading_date)
        bh = self.broker_health(trading_date)
        dd = M.drawdown(self.equity_series(trading_date))
        att_events = self.store.events(kind="ATTENTION_RAISED", trading_date=trading_date, limit=100)
        out = {"trading_date": trading_date, "computed_at": _iso(now), "version": VERSION, "session": row, "session_status": row["session_status"] if row else "NOT_RUN",
                "session_label": SESSION_LABELS.get(row["session_status"] if row else "NOT_RUN"), "funnel": funnel["counts"], "rates": funnel["rates"], "setups_ready": funnel["counts"]["READY"],
                "proposals": funnel["counts"]["REVIEWABLE"] + funnel["counts"]["BLOCKED"], "entries": funnel["counts"]["ENTRY_SUBMITTED"], "exits": funnel["counts"]["EXIT_SUBMITTED"], "open_positions_at_end": (self.store.samples(trading_date)[-1]["open_positions"] if self.store.samples(trading_date) else None),
                "rejections": rel["rejects"], "partials": rel["partial_fills"], "duplicates": rel["duplicate_broker_orders"], "reconciliation_issues": rec["needs_attention_cases"],
                "safety_fires": fires["fire_count"], "safety": {k: fires[k] for k in ("fire_count", "recovered", "persistent", "by_reason_code", "daily_loss_halts", "emergency_stops", "pauses", "evaluator_errors", "entry_control_blocks")},
                "order_reliability": rel, "reconciliation": {k: rec[k] for k in rec if k != "detail"}, "freshness": fr, "broker_health": {k: bh[k] for k in bh if k != "not_separately_persisted"},
                "restarts": self.restarts_view(trading_date)["count"], "closed_trades": [{k: t[k] for k in ("position_id", "symbol", "qty", "entry_price", "exit_price", "realized_pnl", "return_pct", "exit_reason", "holding_sessions", "verified")} for t in trades],
                "realized_pnl": wl["gross_pnl"], "win_loss": wl, "unrealized_pnl": self.unrealized_pnl()["unrealized_pnl"] if row and row["session_status"] == "OPEN" else None,
                "drawdown": dd, "attention": [{"code": e["code"], "at": e["at"]} for e in att_events], "exit_reasons": self.exit_reason_distribution(trades), "disclaimer": DISCLAIMER,
                "forward_capture": self._forward_day(trading_date),                # Stage 6.10A: the day's operational evidence health (read only)
                "pnl_status": self._pnl_status(trades), "unverified": sum(1 for t in trades if not t["verified"])}   # Wave 2 PR A (design 3.1.7); never a pnl_watermark here (H-05)
        return self._null_pnl(out) if out["pnl_status"] == "UNSTABLE" else out

    # ---------------------------------------------------------------- Wave 2 PR A: fresh, read-only summaries (design 3.1.7, 3.12) --------
    @staticmethod
    def _pnl_status(trades: List[dict]) -> str:
        """NO_TRADES | OK (every trade verified) | UNVERIFIED_PRESENT (totals over verified trades only, labelled partial) | UNSTABLE (a
        verification read failed: never persisted, every P/L value null)."""
        if not trades:
            return "NO_TRADES"
        if any("VERIFY_ERROR" in (t.get("unverified_reasons") or []) for t in trades):
            return "UNSTABLE"
        return "OK" if all(t.get("verified") for t in trades) else "UNVERIFIED_PRESENT"

    @staticmethod
    def _null_pnl(s: dict) -> dict:
        """An UNSTABLE summary: every P/L total and per-trade value null - never a stale or mixed number - and no watermark (never current)."""
        out = {k: v for k, v in s.items() if k != "pnl_watermark"}
        out.update(pnl_status="UNSTABLE", realized_pnl=None, win_loss=None,
                   closed_trades=[{**t, "realized_pnl": None, "return_pct": None} for t in (s.get("closed_trades") or [])])
        return out

    def pnl_watermark(self, trading_date: str, rows: Optional[List[list]] = None) -> str:
        """W(d): the canonical JSON of the P/L-input watermark rows of the CLOSED positions of one New York date (one read; or `rows` from a wider
        read, filtered to the date). Every write that can change a day's P/L inputs moves it. Raises on a read error (H-04)."""
        start, end = ny_window(trading_date)
        if rows is None:
            rows = self.execution.store.pnl_watermark_rows(start, end)
        return json.dumps([r for r in rows if start <= (r[1] or "") < end], separators=(",", ":"))

    def bracketed_summary(self, trading_date: str, now: Optional[datetime] = None) -> dict:
        """daily_summary() between two equal watermark reads and complete against them (the same CLOSED positions): one consistent P/L snapshot.
        After BRACKET_ATTEMPTS failed attempts it is UNSTABLE with null P/L. Never writes."""
        now = _utc(now or self.clock())
        s = None
        for _ in range(BRACKET_ATTEMPTS):
            try:
                w1 = self.pnl_watermark(trading_date)
            except Exception:  # noqa: BLE001 - an unreadable watermark is a failed attempt, never an empty one
                continue
            s = self.daily_summary(trading_date, now)
            try:
                w2 = self.pnl_watermark(trading_date)
            except Exception:  # noqa: BLE001
                continue
            complete = {r[0] for r in json.loads(w1)} == {t["position_id"] for t in s["closed_trades"]}   # H-04: every input read, not just unchanged
            if w1 == w2 and complete and s["pnl_status"] != "UNSTABLE":
                return {**s, "pnl_watermark": w1}
        return self._null_pnl(s if s is not None else self.daily_summary(trading_date, now))

    def fresh_daily(self, trading_date: str) -> dict:
        """The read sites' daily summary: the stored one when its watermark is current, otherwise a fresh bracketed one (not persisted). Never writes."""
        stored = self.store.daily(trading_date)
        if stored is not None and stored.get("pnl_watermark") is not None:
            try:
                if stored["pnl_watermark"] == self.pnl_watermark(trading_date):
                    return stored
            except Exception:  # noqa: BLE001 - currency unknown: served fresh, never the stored row
                pass
        return self.bracketed_summary(trading_date)

    def fresh_dailies(self, limit: int = 400, newest_first: bool = True) -> List[dict]:
        """fresh_daily() over the stored dates (the stored set and order, as store.dailies()); one watermark read for the currency check."""
        rows = self.store.dailies(limit, newest_first)
        wrows = None
        if rows:
            dates = sorted(r["trading_date"] for r in rows)
            try:
                wrows = self.execution.store.pnl_watermark_rows(ny_window(dates[0])[0], ny_window(dates[-1])[1])
            except Exception:  # noqa: BLE001 - currency unknown: every row served fresh
                wrows = None
        return [r if wrows is not None and r.get("pnl_watermark") is not None and r["pnl_watermark"] == self.pnl_watermark(r["trading_date"], wrows)
                else self.bracketed_summary(r["trading_date"]) for r in rows]

    @staticmethod
    def _pnl_content(s: Optional[dict]) -> str:
        """The P/L content of a summary (H-08): a rebuild writes SUMMARY_REBUILT only when this differs. A legacy row with no status and no
        trades equals NO_TRADES / 0 (J-09)."""
        s = s or {}
        trades = s.get("closed_trades") or []
        status = s.get("pnl_status") if s.get("pnl_status") is not None or trades else "NO_TRADES"
        unverified = s.get("unverified") if s.get("unverified") is not None or trades else 0
        rows = sorted(([str(t.get("position_id")), t.get("verified"), t.get("realized_pnl"), t.get("return_pct")] for t in trades), key=lambda x: x[0])
        return json.dumps([status, unverified, s.get("realized_pnl"), s.get("win_loss"), rows], sort_keys=True, default=str)

    def _persist_stale_summaries(self, now: datetime) -> dict:
        """Design 3.12.1: make the stored daily summaries current, after the early stop and capture (observe()), at most SUMMARY_REBUILDS_PER_TICK
        dates per tick, each date under its own guard. Persists only consistent, complete results; SUMMARY_REBUILT only when the P/L changed.
        Writes only forward_validation_daily_summary, forward_validation_events and the summary_builder control."""
        tok = self.store.query("SELECT (SELECT MAX(rowid) FROM position_events) AS pe, (SELECT MAX(rowid) FROM execution_order_events) AS oe, "
                               "(SELECT MAX(updated_at) FROM execution_intents) AS iu")
        token = (tok[0]["pe"], tok[0]["oe"], tok[0]["iu"]) if tok else ("NO_ROWS",)
        unmarked = self.store.query("SELECT trading_date FROM forward_validation_daily_summary WHERE instr(summary_json, '\"pnl_watermark\"') = 0")
        if token == self._summary_token and not unmarked and not self._summary_backlog:
            return {"result": "OK", "checked": 0, "rebuilt": 0, "refreshed": 0, "failed": 0, "pending": False}
        stored = {d["trading_date"]: d for d in self.store.dailies(limit=100000)}
        dates = sorted(stored)
        wrows = self.execution.store.pnl_watermark_rows(ny_window(dates[0])[0], ny_window(dates[-1])[1]) if dates else []
        stale = [d for d in reversed(dates) if stored[d].get("pnl_watermark") is None or stored[d]["pnl_watermark"] != self.pnl_watermark(d, wrows)]
        backlog, rebuilt, refreshed, failed = len(stale) > SUMMARY_REBUILDS_PER_TICK, 0, 0, 0
        for d in stale[:SUMMARY_REBUILDS_PER_TICK]:
            try:
                s = self.bracketed_summary(d, now)
                if s["pnl_status"] == "UNSTABLE":
                    backlog = True                                                     # never persisted; a later tick retries
                    continue
                before, after = self._pnl_content(stored[d]), self._pnl_content(s)
                self.store.put_daily(d, now, VERSION, s)
                if before != after:
                    self.store.add_event(now, "SUMMARY_REBUILT", None, {"previous": json.loads(before), "current": json.loads(after)}, trading_date=d, owner=self.owner)
                    self._touch(now, "summary_builder", "RUN", f"daily summary {d} rebuilt: its P/L changed")
                    rebuilt += 1
                else:
                    self._touch(now, "summary_builder", "PASS", f"daily summary {d} made current")
                    refreshed += 1
            except Exception as exc:  # noqa: BLE001 - one date never stops the others; every read stays fresh meanwhile
                backlog, failed = True, failed + 1
                log.warning("forward validation: daily summary %s not persisted (%s)", d, type(exc).__name__)
                self._touch(now, "summary_builder", "UNKNOWN", f"daily summary {d} not persisted", type(exc).__name__)
        self._summary_token, self._summary_backlog = token, backlog                  # the token read BEFORE the scan: a write during it re-scans
        return {"result": "OK" if not failed else "PARTIAL", "checked": len(stale), "rebuilt": rebuilt, "refreshed": refreshed, "failed": failed, "pending": backlog}

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
        wl = M.win_loss([t["realized_pnl"] for t in trades])                         # Wave 2 PR A: verified trades only; no gross fallback (W2-N05)
        unverified = sum(1 for t in trades if not t["verified"])
        dd = M.drawdown(self.equity_series())
        funnel = self.funnel(None)
        rel = self.order_reliability(None)
        rec = self.reconciliation(None)
        fires = self.safety_fires(None)
        bh = self.broker_health(None)
        events = funnel["counts"]["READY"] + funnel["counts"]["REVIEWABLE"]
        edge = M.edge_status(wl["closed_trades"], by["HEALTHY"], wl["expectancy"], wl["win_rate"], dd["max_drawdown_pct"], min_trades=self.min_trades, min_sessions=self.target_sessions)
        if unverified and edge["status"] == "EARLY_POSITIVE":                          # Wave 2 PR A (G-12d): never a positive label while a trade's P/L is unverified
            edge = {**edge, "status": "INSUFFICIENT_DATA", "reasons": [f"{unverified} unverified trade(s): no positive label while any closed trade's P/L is unverified"] + edge["reasons"]}
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
                "strategy": {**wl, "unverified": unverified, "expectancy_status": edge, "edge_status": edge["status"], "edge_label": EDGE_LABELS[edge["status"]], "insufficient": edge["status"] == "INSUFFICIENT_DATA",
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
