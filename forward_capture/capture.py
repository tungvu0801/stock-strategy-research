"""
forward_capture/capture.py - ForwardCapture: the Stage 6.10A point-in-time forward dataset (DESIGN_610A_FORWARD_PAPER_AUTORUN.md).

Executor side (ExecutionService._mark, always inside ExecutionService._record): marker(kind, ...) builds a plain, ALLOWLISTED record of what the
executor saw at that moment and appends it to a bounded in-memory sink. No I/O, no database, no executor lock: a recording failure can never
change or delay an order (an overflow is counted and becomes a fault - an evidence gap - never a blocked executor).

  SCAN        the READY episodes a Paper Auto cycle considered           EVALUATED   one candidate's entry-control results (allowlisted)
  SKIPPED     candidates left for the next cycle (entries per cycle)     DUPLICATE   an entry for the proposal already existed
  INTENT      the entry intent as created (immutable columns only)       CYCLE_END   the cycle finished (result, error)

Observer side (ForwardValidationService.observe, the only writer, only while this process holds the instance lease): tick() drains the sink and,
in short transactions, records
  - the config snapshot and cohort (process start, first tick of a trading date, any configuration change)
  - a context row: the other (read-only) accounts' states and freshness, never their holdings
  - every setup evaluation of an open candidate since the watermark (NOT_READY / READY / SKIPPED with PAPER_AUTO_OFF / NOT_EVALUATED)
  - the executor's decisions (BLOCKED / SKIPPED / ENTRY_INTENT + the immutable entry snapshot / NOT_EVALUATED when a cycle ended early)
  - the lifecycle from persisted rows: ENTERED, EXITED + the outcome, NOT_ENTERED; an entry intent whose marker was lost (a crash, a previous
    process) is recovered from the persisted intent and flagged recovered=1 - never presented as decision-time evidence
  - the trading date's operational health (forward_capture/health.py)
Decisions are frozen when recorded: nothing later (bars, scores, evidence, fills) rewrites an observation.
"""
from __future__ import annotations

import bisect
import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from forward_capture import (BLOCKED, CONTEXT_EVERY_S, ENTERED, ENTRY_INTENT, EXITED, NOT_ENTERED, NOT_EVALUATED, NOT_READY, READY, SINK_MAX, SKIPPED, STRATEGY_ID,
                             TICK_MAX_EVALUATIONS, VERSION)
from forward_capture import project as P
from forward_capture.store import ForwardCaptureStore, dumps, fid

log = logging.getLogger(__name__)
UTC = timezone.utc
STALE_CYCLE_S = 900                      # a cycle without CYCLE_END after this long is resolved as INCOMPLETE (a crash, a hang)
LOST_MARKER_AFTER_S = 120                # an own entry intent this old without its INTENT marker: the marker was lost (a failing recorder); recovered
MAX_UNWRITTEN = 5000                     # evidence rows a failed write keeps for the next tick
RESOLVED_NO_FILL = ("NOT_SENT", "REJECTED", "CANCELED", "FAILED")
DECISION_COLS = ("decision_id", "candidate_id", "setup_id", "symbol", "cohort_id", "decided_at", "observed_at", "trading_date", "market_session", "mode", "source",
                 "source_ref", "decision", "reason_codes_json", "setup_state", "evaluation_id", "features_json", "score", "proposal_json", "account_json", "exposure_json",
                 "controls_version", "contract_hash", "controls_json", "execution_id", "position_id", "run_id", "recovered", "legacy", "context_at", "detail_json")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _loads(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def trading_date(ts) -> Optional[str]:
    from setups.market_hours import session_date
    d = _parse(ts)
    if d is None:
        return None
    sd = session_date(d)
    return (sd or d.date()).isoformat()


def market_session(ts, clock: Optional[dict] = None) -> Optional[str]:
    from autorun.session import classify
    d = _parse(ts)
    return classify(d, clock)["session"] if d is not None else None


def candidate_id(setup_id: str) -> str:
    return fid("candidate", setup_id)


def strategy_version() -> str:
    import setups as S
    try:
        from strategy_registry import registry as R
        v = R.get(STRATEGY_ID).version
    except Exception:  # noqa: BLE001
        v = "?"
    return f"{v}/{S.VERSION}"


# ================================================================ the executor-side record ===================================================
def build_marker(kind: str, d: Dict[str, Any], observed: datetime) -> Dict[str, Any]:
    """The marker as plain data, built at the moment of the decision (allowlisted; no object reference is kept)."""
    now = d.get("now")
    rec: Dict[str, Any] = {"kind": kind, "run_id": d.get("run_id"), "at": _iso(now) if isinstance(now, datetime) else now, "observed_at": _iso(observed)}
    if kind in ("SCAN", "SKIPPED"):
        rec["items"] = [(str(s), str(y)) for s, y in (d.get("items") or [])]
    elif kind == "EVALUATED":
        from execution import exposure_gate as G
        from execution.engine import blocking
        ctx, cs = d["ctx"], list(d.get("controls") or [])
        setup = P.setup(ctx.setup)
        gate = getattr(ctx, "exposure_gate", None)
        rec.update(setup_id=d.get("setup_id"), symbol=d.get("symbol"), mode=ctx.mode, setup=setup, proposal=P.proposal(ctx.proposal), account=P.account(ctx.account),
                   account_error=ctx.account_error, environment=P.environment(ctx.environment), safety=P.safety(ctx.safety), clock=P.clock(ctx.broker_clock),
                   controls=P.controls(cs), blocked=[c.control_id for c in blocking(cs)],
                   limits={"broker_positions": ctx.broker_positions_count, "pending_entries": ctx.pending_entries_count, "max_open_positions": ctx.max_open_positions,
                           "qty_cap": ctx.qty_cap, "near_close_minutes": ctx.near_close_minutes},
                   reconciliation={"healthy": bool(ctx.reconciliation_healthy), "reasons": [str(r)[:160] for r in (ctx.reconciliation_reasons or [])][:10]},
                   external=P.external(ctx.unified_portfolio) if getattr(ctx, "unified_portfolio", None) is not None else None,
                   gate=next((P.gate_verdict(c) for c in cs if getattr(c, "control_id", None) == P.GATE_CONTROL), None),
                   gate_enabled=bool(getattr(gate, "enabled", False)), controls_version=G.VERSION if gate is not None else G.PREVIOUS_VERSION,
                   contract_hash=G.contract_hash(gate) if gate is not None else None,
                   mismatch=setup.get("setup_id") not in (None, d.get("setup_id")))
    elif kind == "DUPLICATE":
        rec.update(setup_id=d.get("setup_id"), symbol=d.get("symbol"), execution_id=d.get("execution_id"))
    elif kind == "INTENT":
        from execution.engine import execution_id_for
        it = d["intent"]
        rec.update(setup_id=d.get("setup_id"), intent=P.intent_at_decision(it), mismatch=bool(it.proposal_id) and execution_id_for(it.proposal_id) != it.execution_id)
    elif kind == "CYCLE_END":
        fin = d.get("finished")
        rec.update(finished=_iso(fin) if isinstance(fin, datetime) else fin, mode=d.get("mode"), trigger=d.get("trigger"), result=d.get("result"),
                   error=str(d.get("error"))[:300] if d.get("error") else None)
    return rec


class ForwardCapture:
    def __init__(self, fv):
        self.fv = fv                                                    # the Stage 6.1 observer this capture extends (no I/O here)
        self._sink: deque = deque()
        self._sink_lock = threading.Lock()
        self.dropped = 0
        self._store: Optional[ForwardCaptureStore] = None
        self.process_start = datetime.now(UTC)
        self.cycles: Dict[str, dict] = {}                               # markers of cycles not resolved yet (by run_id)
        self.unwritten: List[Tuple[str, dict]] = []
        self.mem_faults: List[dict] = []
        self.last_cycle_finished: Optional[str] = None                  # the newest drained CYCLE_END: its intents' markers were drained before it
        self.snapshot_owner: Optional[str] = None
        self.snapshot_day: Optional[str] = None
        self.config_hash: Optional[str] = None
        self.cohort: Optional[dict] = None
        self.context_key: Optional[str] = None
        self.context_at: Optional[datetime] = None
        self.ticks = 0
        self.last_tick: Optional[dict] = None
        self._unmarked: Dict[str, datetime] = {}                        # own entry intents seen without their INTENT marker (first sight)

    @property
    def store(self) -> ForwardCaptureStore:
        if self._store is None:
            self._store = ForwardCaptureStore(self.fv.store.path)
        return self._store

    # ---------------------------------------------------------------- executor thread ---------------------------------------------
    def marker(self, kind: str, **data) -> None:
        rec = build_marker(kind, data, self.fv.clock())
        with self._sink_lock:
            if len(self._sink) >= SINK_MAX:
                self.dropped += 1
                return
            self._sink.append(rec)

    def _requeue(self, now: datetime, markers: List[dict]) -> None:
        """Markers drained by a tick that failed before recording them go back to the FRONT of the sink (the next tick records them); any that do
        not fit are an evidence gap, recorded as such (the day is then INVALID)."""
        with self._sink_lock:
            room = max(0, SINK_MAX - len(self._sink))
            keep, lost = markers[:room], len(markers) - min(room, len(markers))
            self._sink.extendleft(reversed(keep))
        if lost:
            self.fault(now, "capture", "EVIDENCE_LOST", {"markers": lost, "reason": "a failed tick's markers did not fit back into the sink"}, key=f"{now.astimezone(UTC):%H%M}|requeue")

    def drain(self) -> Tuple[List[dict], int]:
        with self._sink_lock:
            out = list(self._sink)
            self._sink.clear()
            dropped, self.dropped = self.dropped, 0
        return out, dropped

    def pending(self) -> int:
        with self._sink_lock:
            return len(self._sink)

    # ---------------------------------------------------------------- observer thread -----------------------------------------------
    def fault(self, now: datetime, component: str, code: str, detail: Optional[dict] = None, key: Optional[str] = None, at: Optional[str] = None) -> None:
        td = trading_date(at or _iso(now))
        self.mem_faults.append({"component": component, "code": code, "detail": detail or {}, "key": f"{td}|{key}" if key else None, "trading_date": td, "at": at or _iso(now)})
        del self.mem_faults[:-500]

    def _commit(self, rows: List[Tuple[str, dict]], now: datetime, *, state: Optional[dict] = None, keep: bool = True) -> bool:
        faults, self.mem_faults = self.mem_faults, []
        try:
            res = self.store.put(rows, now, state=state, faults=faults)
            if res["conflicts"]:
                log.warning("forward capture: %d conflicting row(s) kept as stored", len(res["conflicts"]))
            return True
        except Exception as exc:  # noqa: BLE001 - a locked or failing database delays evidence; it never reaches the executor
            log.warning("forward capture write failed (%s)", type(exc).__name__)
            self.mem_faults = faults
            self.fault(now, "store", "WRITE_FAILED", {"error": type(exc).__name__, "rows": len(rows)}, key=f"{now.astimezone(UTC):%H}|{type(exc).__name__}")
            if keep and rows:
                merged = self.unwritten + rows
                if len(merged) > MAX_UNWRITTEN:
                    self.fault(now, "store", "EVIDENCE_LOST", {"rows": len(merged) - MAX_UNWRITTEN, "reason": "unwritten buffer full"}, key=f"{now.astimezone(UTC):%H%M}|lost")
                self.unwritten = merged[-MAX_UNWRITTEN:]
            return False

    def _lease(self, now: datetime) -> Dict[str, Any]:
        """Write only from the process that holds the single-instance lease (no lease row: the supervisor is off or not started - write)."""
        from autorun import INSTANCE_LEASE, instance_id
        rows = self.store.query("SELECT owner, expires_at FROM app_leases WHERE name = ?", (INSTANCE_LEASE,))
        if not rows:
            return {"ok": True, "state": "NO_LEASE"}
        r = rows[0]
        if r["owner"] == instance_id():
            return {"ok": True, "state": "HELD"}
        if str(r["expires_at"] or "") <= _iso(now):
            return {"ok": True, "state": "EXPIRED"}
        return {"ok": False, "state": "OTHER", "owner": r["owner"], "expires_at": r["expires_at"]}

    def tick(self, now: datetime, trigger: str = "SCHEDULED") -> Dict[str, Any]:
        """One capture tick (never raises). Returns counts for the observation record."""
        self.ticks += 1
        out: Dict[str, Any] = {"at": _iso(now), "result": "OK"}
        markers, dropped = self.drain()
        if dropped:
            self.fault(now, "sink", "SINK_OVERFLOW", {"dropped": dropped, "reason": "the executor produced more markers than the sink holds"}, key=f"{now.astimezone(UTC):%H%M}|sink")
        done = False
        try:
            lease = self._lease(now)
            if not lease["ok"]:
                self.fault(now, "capture", "DUPLICATE_INSTANCE", {"owner": lease.get("owner"), "expires_at": lease.get("expires_at"), "dropped_markers": len(markers)},
                           key="duplicate")
                self._commit([], now, keep=False)
                out.update(result="NOT_OWNER", lease=lease["state"])
                self.last_tick = out
                return out
            st = self._epoch(now)
            epoch = st["epoch"]
            td = trading_date(_iso(now))
            if self.unwritten:                                          # a previous tick's rows first (their ids are stable: re-inserting is harmless)
                rows, self.unwritten = self.unwritten, []
                self._commit(rows, now)
            out["snapshot"] = self._snapshots(now, td)
            out["context"] = self._context(now)
            rows, wm, n_ev = self._evaluations(now, st, epoch)
            ok = self._commit(rows, now, state={"eval_watermark": wm} if wm is not None else None, keep=False)
            out["evaluations"] = n_ev if ok else 0
            rows = self._markers(now, markers, epoch)
            done = True                                                 # from here the markers' rows are kept (written, or in `unwritten`)
            self._commit(rows, now)
            out["executor_decisions"] = sum(1 for t, _ in rows if t == "forward_capture_decisions")
            pending_x = {r.get("execution_id") for t, r in self.unwritten if t == "forward_capture_decisions"}
            rows = self._lifecycle(now, epoch, pending_x)
            self._commit(rows, now)
            out["lifecycle"] = sum(1 for t, _ in rows if t == "forward_capture_decisions")
            from forward_capture.health import update as health_update
            out["health"] = health_update(self, now)
            if self.mem_faults:
                self._commit([], now, keep=False)
        except Exception as exc:  # noqa: BLE001 - the capture never raises into the observer
            log.warning("forward capture tick failed (%s)", type(exc).__name__)
            out.update(result="ERROR", error=f"{type(exc).__name__}: {exc}"[:300])
            self.fault(now, "capture", "TICK_FAILED", {"error": out["error"]}, key=f"{now.astimezone(UTC):%H}|tick")
            if not done and markers:
                self._requeue(now, markers)
            try:
                self._commit([], now, keep=False)
            except Exception:  # noqa: BLE001
                pass
        self.last_tick = out
        return out

    # ---------------------------------------------------------------- epoch / snapshots / context ---------------------------------
    def _epoch(self, now: datetime) -> Dict[str, Any]:
        st = self.store.state()
        if "epoch" not in st or "eval_watermark" not in st:
            mx = self.store.query("SELECT MAX(rowid) AS m FROM setup_evaluations")
            self.store.init_state("epoch", _iso(now), now)
            self.store.init_state("eval_watermark", int((mx[0]["m"] if mx else 0) or 0), now)   # evaluations before the epoch are not captured
            st = self.store.state()
        return st

    def _snapshots(self, now: datetime, td: Optional[str]) -> Optional[List[str]]:
        from autorun import instance_id
        from forward_capture.cohort import snapshot
        snap = snapshot(self.fv.stream_fn())
        h = snap["config_hash"]
        triggers = []
        if self.snapshot_owner is None:
            triggers.append("STARTUP")
        if self.snapshot_day != td:
            triggers.append("DAY")
        if self.config_hash is not None and h != self.config_hash:
            triggers.append("CHANGE")
        if not triggers:
            return None
        try:
            cohort = self.store.put_cohort(snap["cohort_id"], snap["cohort_key"], _iso(now), td, snap["cohort_definition"])
        except Exception as exc:  # noqa: BLE001 - a locked database delays the snapshot (retried next tick); the tick goes on
            self.fault(now, "store", "WRITE_FAILED", {"error": type(exc).__name__, "what": "cohort"}, key=f"{now.astimezone(UTC):%H}|cohort|{type(exc).__name__}")
            return triggers + ["DEFERRED"]
        owner = instance_id()
        rows = []
        for t in triggers:
            sid = {"STARTUP": fid("config", "STARTUP", owner, h), "DAY": fid("config", "DAY", td, h), "CHANGE": fid("config", "CHANGE", h, _iso(now))}[t]
            rows.append(("forward_capture_config_snapshots", {"snapshot_id": sid, "at": _iso(now), "trigger": t, "trading_date": td, "config_hash": h,
                                                              "cohort_id": cohort["cohort_id"], "config_json": dumps(snap["config"]), "owner": owner}))
        if self._commit(rows, now):
            self.snapshot_owner, self.snapshot_day, self.config_hash, self.cohort = owner, td, h, cohort
        return triggers

    def _context(self, now: datetime) -> Optional[str]:
        from execution import exposure_gate as G
        fn = getattr(self.fv.stream_fn(), "unified_portfolio", None)
        try:
            ctx = P.external(fn(refresh=False)) if callable(fn) else {"unified_state": "UNAVAILABLE", "sources": [], "error": "NOT_WIRED"}
        except Exception as exc:  # noqa: BLE001 - unreadable = UNAVAILABLE (never 'no holdings')
            ctx = {"unified_state": "UNAVAILABLE", "sources": [], "error": type(exc).__name__}
        gate = G.from_config(config)
        ctx["exposure_gate"] = {"enabled": gate.enabled, "version": G.VERSION, "contract_hash": G.contract_hash(gate)}
        key = P.sha({"u": ctx.get("unified_state"), "s": [(s.get("source"), s.get("state")) for s in ctx.get("sources") or []], "g": ctx["exposure_gate"]})
        if key == self.context_key and self.context_at is not None and (now - self.context_at).total_seconds() < CONTEXT_EVERY_S:
            return None
        h = P.sha(ctx)
        row = {"context_id": fid("context", _iso(now), h), "at": _iso(now), "unified_state": ctx.get("unified_state"), "context_json": dumps(ctx), "content_sha256": h}
        if self._commit([("forward_capture_context", row)], now):
            self.context_key, self.context_at = key, now
        return ctx.get("unified_state")

    # ---------------------------------------------------------------- point-in-time lookups (persisted rows <= the decision time) --------
    def _lookups(self, lo: str, hi: str, setup_ids: List[str]) -> Dict[str, Any]:
        q = self.store.query
        back = _iso(_parse(lo) - timedelta(hours=1)) if _parse(lo) else lo
        modes = q("SELECT at, to_mode FROM execution_mode_events ORDER BY at, rowid")
        samples = q("SELECT at, equity, cash, buying_power, account_status, account_age_s FROM forward_validation_samples WHERE at >= ? AND at <= ? ORDER BY at, rowid", (back, hi))
        safety = q("SELECT evaluated_at AS at, state, allowed_new_entry FROM execution_safety_evaluations WHERE evaluated_at >= ? AND evaluated_at <= ? ORDER BY evaluated_at, rowid",
                   (back, hi))
        safety = q("SELECT evaluated_at AS at, state, allowed_new_entry FROM execution_safety_evaluations WHERE evaluated_at < ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                   (back,))[::-1] + safety
        ctx = q("SELECT at, context_json FROM forward_capture_context WHERE at < ? ORDER BY at DESC, rowid DESC LIMIT 1", (back,))[::-1] \
            + q("SELECT at, context_json FROM forward_capture_context WHERE at >= ? AND at <= ? ORDER BY at, rowid", (back, hi))
        snaps = q("SELECT at, cohort_id, config_json FROM forward_capture_config_snapshots ORDER BY at, rowid")
        marks = ",".join("?" * len(setup_ids))
        eps = {r["setup_id"]: r for r in q(f"SELECT setup_id, symbol, setup_type, cycle_id, started_at FROM setup_episodes WHERE setup_id IN ({marks})", tuple(setup_ids))} if setup_ids else {}
        props: Dict[str, list] = {}
        if setup_ids:
            for r in q(f"SELECT proposal_id, setup_id, status, created_at, expires_at, reference_price, invalidation_price, final_qty FROM trade_proposals WHERE setup_id IN ({marks}) "
                       "ORDER BY created_at, rowid", tuple(setup_ids)):
                props.setdefault(r["setup_id"], []).append(r)
        snap_rows = []
        for s in snaps:
            ec = (_loads(s["config_json"], {}) or {}).get("entry_controls") or {}
            snap_rows.append({"at": s["at"], "cohort_id": s["cohort_id"], "version": ec.get("version"), "contract_hash": ec.get("contract_hash")})
        return {"modes": modes, "samples": samples, "safety": safety, "context": ctx, "snaps": snap_rows, "episodes": eps, "proposals": props}

    @staticmethod
    def _at(rows: List[dict], ts: str) -> Optional[dict]:
        """The last row with at <= ts (rows sorted by at)."""
        i = bisect.bisect_right([r["at"] for r in rows], ts)
        return rows[i - 1] if i else None

    def _cohort_at(self, L: Dict[str, Any], ts: str) -> Dict[str, Any]:
        s = self._at(L["snaps"], ts) or (L["snaps"][0] if L["snaps"] else None)
        return s or {"cohort_id": (self.cohort or {}).get("cohort_id") or "UNSNAPSHOTTED", "version": None, "contract_hash": None}

    # ---------------------------------------------------------------- setup evaluations -> decisions ------------------------------------
    def _evaluations(self, now: datetime, st: Dict[str, Any], epoch: str) -> Tuple[List[Tuple[str, dict]], Optional[int], int]:
        wm = int(st.get("eval_watermark") or 0)
        evs = self.store.query("SELECT rowid AS rid, evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, reason_text, "
                               "inputs_json, trigger FROM setup_evaluations WHERE rowid > ? ORDER BY rowid LIMIT ?", (wm, TICK_MAX_EVALUATIONS))
        if not evs:
            return [], None, 0
        new_wm = int(evs[-1]["rid"])
        evs = [e for e in evs if e.get("setup_id") and e.get("trigger") != "TEST"]   # a symbol without an episode is no candidate; TEST rows are not evidence
        if not evs:
            return [], new_wm, 0
        L = self._lookups(min(e["evaluated_at"] for e in evs), max(e["evaluated_at"] for e in evs), sorted({e["setup_id"] for e in evs}))
        sv = strategy_version()
        rows: List[Tuple[str, dict]] = []
        for e in evs:
            ts = e["evaluated_at"]
            ep = L["episodes"].get(e["setup_id"]) or {}
            legacy = 1 if (ep.get("started_at") or ts) < epoch else 0
            co = self._cohort_at(L, ts)
            rows.append(("forward_capture_candidates", self._candidate(e["setup_id"], e["symbol"], e.get("setup_type"), ep.get("cycle_id"), ep.get("started_at"), ts,
                                                                       e["resulting_state"], co["cohort_id"], legacy, sv)))
            mode = (self._at(L["modes"], ts) or {}).get("to_mode") or "MONITOR_ONLY"
            sess = market_session(ts)
            inputs = P.scrub(_loads(e.get("inputs_json"), {}) or {})
            sample = self._at(L["samples"], ts)
            safety = self._at(L["safety"], ts)
            ctx = self._at(L["context"], ts)
            prop = self._at([dict(p, at=p["created_at"]) for p in L["proposals"].get(e["setup_id"], [])], ts)
            detail: Dict[str, Any] = {"trigger": e.get("trigger"), "prior_state": e.get("prior_state"), "evaluation_decision": e.get("decision"),
                                      "reason_text": str(e.get("reason_text") or "")[:300]}
            if e.get("trigger") == "MANUAL":
                detail["manual"] = True
            if e["decision"] in ("ERROR", "PAUSED"):
                decision, reasons = NOT_EVALUATED, ["EVALUATION_ERROR" if e["decision"] == "ERROR" else "PAUSED", e["reason_code"]]
            elif e["resulting_state"] == "READY" and mode == "PAPER_AUTO":
                decision, reasons = READY, ["AWAITING_EXECUTOR"]
            elif e["resulting_state"] == "READY":
                decision, reasons = SKIPPED, ["PAPER_AUTO_OFF"] + self._knowable(ts, sess, safety, prop)
                detail["not_evaluated_in_monitor_only"] = ("broker-side entry controls (tradability, broker position, buying power, open-positions limit, "
                                                           "reconciliation, unified exposure) are evaluated only in Paper Auto cycles")
            else:
                decision, reasons = NOT_READY, [e["reason_code"]]
            rows.append(("forward_capture_decisions", self._decision(
                decision_id=fid("decision", "EVAL", e["evaluation_id"]), setup_id=e["setup_id"], symbol=e["symbol"], cohort_id=co["cohort_id"], decided_at=ts, observed_at=_iso(now),
                market_session=sess, mode=mode, source="SETUP_EVALUATION", source_ref=e["evaluation_id"], decision=decision, reasons=reasons, setup_state=e["resulting_state"],
                evaluation_id=e["evaluation_id"], features=inputs, score=inputs.get("confidence"), proposal=self._proposal_row(prop), account=self._sample_account(sample),
                exposure=self._context_exposure(ctx), controls_version=co.get("version"), contract_hash=co.get("contract_hash"), controls=None,
                legacy=legacy, context_at=(ctx or {}).get("at"), detail={**detail, "safety": self._safety_row(safety)})))
        return rows, new_wm, len(evs)

    @staticmethod
    def _knowable(ts: str, sess: Optional[str], safety: Optional[dict], prop: Optional[dict]) -> List[str]:
        """Blockers knowable from persisted rows at `ts`, without a broker call (Monitor only never runs the entry controls)."""
        out = []
        if sess != "REGULAR":
            out.append("SESSION_POLICY")
        if safety is None or not int(safety.get("allowed_new_entry") or 0):
            out.append("SAFETY_GATE")
        if prop is None or prop.get("status") != "REVIEWABLE" or str(prop.get("expires_at") or "") <= ts:
            out.append("PROPOSAL_VALIDITY")
        return out

    @staticmethod
    def _proposal_row(p: Optional[dict]) -> Optional[dict]:
        return {k: p.get(k) for k in ("proposal_id", "status", "created_at", "expires_at", "reference_price", "invalidation_price", "final_qty")} if p else None

    @staticmethod
    def _sample_account(s: Optional[dict]) -> Optional[dict]:
        if not s:
            return None
        return {"equity": s.get("equity"), "cash": s.get("cash"), "buying_power": s.get("buying_power"), "status": s.get("account_status"), "age_s": s.get("account_age_s"),
                "sampled_at": s.get("at"), "source": "forward-validation sample (account refresh, PAPER)"}

    @staticmethod
    def _safety_row(s: Optional[dict]) -> Optional[dict]:
        return {"state": s.get("state"), "allowed_new_entry": bool(int(s.get("allowed_new_entry") or 0)), "evaluated_at": s.get("at")} if s else None

    @staticmethod
    def _context_exposure(c: Optional[dict]) -> Optional[dict]:
        if not c:
            return None
        ctx = _loads(c.get("context_json"), {}) or {}
        return {"unified_state": ctx.get("unified_state"), "sources": ctx.get("sources") or [], "exposure_gate": ctx.get("exposure_gate"), "context_at": c.get("at")}

    # ---------------------------------------------------------------- row builders ------------------------------------------------------
    @staticmethod
    def _candidate(setup_id: str, symbol: str, setup_type: Optional[str], cycle: Any, started_at: Optional[str], seen_at: str, state: Optional[str], cohort_id: Optional[str],
                   legacy: int, sv: str) -> dict:
        return {"candidate_id": candidate_id(setup_id), "setup_id": setup_id, "symbol": symbol, "setup_type": setup_type or STRATEGY_ID, "cycle_id": cycle,
                "strategy_id": STRATEGY_ID, "strategy_version": sv, "started_at": started_at, "first_seen_at": seen_at, "first_state": state, "cohort_id": cohort_id,
                "legacy": int(legacy)}

    @staticmethod
    def _decision(*, decision_id: str, setup_id: Optional[str], symbol: str, cohort_id: Optional[str], decided_at: str, observed_at: str, market_session: Optional[str],
                  mode: Optional[str], source: str, source_ref: Optional[str], decision: str, reasons: List[str], setup_state: Optional[str] = None,
                  evaluation_id: Optional[str] = None, features: Optional[dict] = None, score: Any = None, proposal: Optional[dict] = None, account: Optional[dict] = None,
                  exposure: Optional[dict] = None, controls_version: Optional[str] = None, contract_hash: Optional[str] = None, controls: Optional[list] = None,
                  execution_id: Optional[str] = None, position_id: Optional[str] = None, run_id: Optional[str] = None, recovered: int = 0, legacy: int = 0,
                  context_at: Optional[str] = None, detail: Optional[dict] = None) -> dict:
        row = {"decision_id": decision_id, "candidate_id": candidate_id(setup_id) if setup_id else None, "setup_id": setup_id, "symbol": symbol, "cohort_id": cohort_id,
               "decided_at": decided_at, "observed_at": observed_at, "trading_date": trading_date(decided_at), "market_session": market_session, "mode": mode, "source": source,
               "source_ref": source_ref, "decision": decision, "reason_codes_json": dumps([r for r in reasons if r]), "setup_state": setup_state, "evaluation_id": evaluation_id,
               "features_json": dumps(features) if features is not None else None, "score": None if score is None else str(score),
               "proposal_json": dumps(proposal) if proposal is not None else None, "account_json": dumps(account) if account is not None else None,
               "exposure_json": dumps(exposure) if exposure is not None else None, "controls_version": controls_version, "contract_hash": contract_hash,
               "controls_json": dumps(controls) if controls is not None else None, "execution_id": execution_id, "position_id": position_id, "run_id": run_id,
               "recovered": int(recovered), "legacy": int(legacy), "context_at": context_at, "detail_json": dumps(detail or {})}
        row["content_sha256"] = P.sha({k: v for k, v in row.items() if k != "observed_at"})
        return row

    def _exec_decision(self, m: dict, decision: str, reasons: List[str], *, setup_id: Optional[str] = None, symbol: Optional[str] = None,
                       execution_id: Optional[str] = None, detail: Optional[dict] = None, decision_id: Optional[str] = None) -> Tuple[str, dict]:
        sid, sym = setup_id or m.get("setup_id"), symbol or m.get("symbol")
        has_eval = "controls" in m
        setup = m.get("setup") or {}
        cohort_id = (self.cohort or {}).get("cohort_id") or "UNSNAPSHOTTED"
        return ("forward_capture_decisions", self._decision(
            decision_id=decision_id or fid("decision", "EXEC", m.get("run_id"), sid), setup_id=sid, symbol=sym, cohort_id=cohort_id, decided_at=m["at"], observed_at=m["observed_at"],
            market_session=market_session(m["at"], m.get("clock") or None), mode=m.get("mode") or "PAPER_AUTO", source="EXECUTOR_CYCLE", source_ref=m.get("run_id"),
            decision=decision, reasons=reasons, setup_state=setup.get("state"), features=setup.get("inputs") if has_eval else None,
            score=((setup.get("inputs") or {}).get("confidence")) if has_eval else None, proposal=m.get("proposal") if has_eval else None,
            account=(dict(m.get("account") or {}, error=m.get("account_error")) if has_eval else None),
            exposure={"gate_enabled": m.get("gate_enabled"), "gate": m.get("gate"), "external": m.get("external")} if has_eval else None,
            controls_version=m.get("controls_version"), contract_hash=m.get("contract_hash"), controls=m.get("controls") if has_eval else None, execution_id=execution_id,
            run_id=m.get("run_id"), detail={**(detail or {}), **({k: m.get(k) for k in ("limits", "reconciliation", "safety", "environment", "clock")} if has_eval else {})}))

    # ---------------------------------------------------------------- executor markers -> decisions -------------------------------------
    def _markers(self, now: datetime, markers: List[dict], epoch: str) -> List[Tuple[str, dict]]:
        rows: List[Tuple[str, dict]] = []
        sv = strategy_version()
        for m in markers:
            rid = m.get("run_id") or "NO_RUN"
            run = self.cycles.setdefault(rid, {"scan": [], "evaluated": {}, "done": set(), "first_at": m.get("at"), "symbols": {}})
            k = m["kind"]
            if k == "SCAN":
                run["scan"] = list(m.get("items") or [])
                run["symbols"].update({s: y for s, y in run["scan"]})
            elif k == "EVALUATED":
                run["evaluated"][m["setup_id"]] = m
                setup = m.get("setup") or {}
                rows.append(("forward_capture_candidates", self._candidate(m["setup_id"], m["symbol"], STRATEGY_ID, setup.get("cycle_id"), setup.get("started_at"),
                                                                           m["at"], setup.get("state"), (self.cohort or {}).get("cohort_id"),
                                                                           1 if (setup.get("started_at") or m["at"]) < epoch else 0, sv)))
                if m.get("mismatch"):
                    self.fault(now, "executor", "SETUP_MISMATCH", {"setup_id": m["setup_id"], "view_setup_id": setup.get("setup_id"), "symbol": m["symbol"]}, key=m["setup_id"])
                if m.get("blocked"):
                    rows.append(self._exec_decision(m, BLOCKED, list(m["blocked"])))
                    run["done"].add(m["setup_id"])
            elif k == "SKIPPED":
                for sid, sym in m.get("items") or []:
                    base = run["evaluated"].get(sid) or {**m, "setup_id": sid, "symbol": sym}
                    rows.append(self._exec_decision({**base, "run_id": m.get("run_id"), "at": m["at"], "observed_at": m["observed_at"]}, SKIPPED, ["ENTRIES_PER_CYCLE"],
                                                    setup_id=sid, symbol=sym, detail={"text": "the per-cycle entry limit was used; evaluated again next cycle"}))
                    run["done"].add(sid)
            elif k == "DUPLICATE":
                base = run["evaluated"].get(m.get("setup_id")) or m
                rows.append(self._exec_decision({**base, "run_id": m.get("run_id")}, BLOCKED, ["DUPLICATE_ENTRY"], setup_id=m.get("setup_id"), symbol=m.get("symbol"),
                                                execution_id=m.get("execution_id"), detail={"text": "an entry intent for this proposal already exists"}))
                run["done"].add(m.get("setup_id"))
            elif k == "INTENT":
                rows += self._intent_rows(now, m, run["evaluated"].get(m.get("setup_id")), sv)
                run["done"].add(m.get("setup_id"))
            elif k == "CYCLE_END":
                rows += self._close_cycle(rid, run, m)
                self.cycles.pop(rid, None)
                fin = m.get("finished") or m.get("at")
                if fin and (self.last_cycle_finished is None or fin > self.last_cycle_finished):
                    self.last_cycle_finished = fin
        for rid, run in list(self.cycles.items()):                     # a cycle whose end never came (a crash, a hang)
            first = _parse(run.get("first_at"))
            if first is not None and (now - first).total_seconds() > STALE_CYCLE_S:
                rows += self._close_cycle(rid, run, None)
                self.cycles.pop(rid, None)
        while len(self.cycles) > 50:
            rid = next(iter(self.cycles))
            rows += self._close_cycle(rid, self.cycles.pop(rid), None)
        return rows

    def _close_cycle(self, rid: str, run: dict, end: Optional[dict]) -> List[Tuple[str, dict]]:
        """Every candidate the cycle scanned gets a decision: anything without one is NOT_EVALUATED / INCOMPLETE (the cycle error, or its end unseen)."""
        rows = []
        why = (f"the cycle ended in {end.get('result')}: {end.get('error')}" if end and end.get("error") else "the cycle ended before this candidate was decided") if end \
            else "the cycle's end was never observed (process stopped or cycle hung)"
        code = "CYCLE_ERROR" if end and end.get("result") == "ERROR" else "CYCLE_END_NOT_OBSERVED" if end is None else "INCOMPLETE"
        for sid, sym in run.get("scan") or []:
            if sid in run["done"]:
                continue
            base = run["evaluated"].get(sid) or {"run_id": None if rid == "NO_RUN" else rid, "at": run.get("first_at") or (end or {}).get("at"),
                                                 "observed_at": (end or {}).get("observed_at") or run.get("first_at"), "setup_id": sid, "symbol": sym,
                                                 "mode": (end or {}).get("mode")}
            rows.append(self._exec_decision(base, NOT_EVALUATED, ["INCOMPLETE", code], setup_id=sid, symbol=sym, detail={"text": why}))
            run["done"].add(sid)
        return rows

    def _intent_rows(self, now: datetime, m: dict, ev: Optional[dict], sv: str) -> List[Tuple[str, dict]]:
        it = m.get("intent") or {}
        xid = it.get("execution_id")
        if not xid:
            return []
        if m.get("mismatch"):
            self.fault(now, "executor", "INTENT_ID_MISMATCH", {"execution_id": xid, "proposal_id": it.get("proposal_id")}, key=xid)
        ev = ev or {}
        setup = ev.get("setup") or {}
        cohort_id = (self.cohort or {}).get("cohort_id") or "UNSNAPSHOTTED"
        did = fid("decision", "ENTRY_INTENT", xid)
        snap = {"version": VERSION, "execution_id": xid, "candidate_id": candidate_id(it.get("setup_id") or m.get("setup_id")), "decision_id": did, "cohort_id": cohort_id,
                "symbol": it.get("symbol"), "setup_id": it.get("setup_id"), "proposal_id": it.get("proposal_id"),
                "strategy": {"strategy_id": STRATEGY_ID, "strategy_version": sv},
                "signal": {"setup_state": setup.get("state"), "setup_evaluated_at": setup.get("evaluated_at")},
                "planned": {"qty": it.get("qty"), "proposal_qty": it.get("proposal_qty"), "reference_price": it.get("reference_price"),
                            "invalidation_price": it.get("invalidation_price"), "risk_budget": it.get("risk_budget"),
                            "risk_per_share": (it.get("proposal_snapshot") or {}).get("risk_per_share"), "stop_rule": (it.get("proposal_snapshot") or {}).get("stop_rule"),
                            "reward_rule": "rule-based exits (invalidation, demand, stretch, holding time); the strategy defines no fixed reward level"},
                "controls": {"version": ev.get("controls_version"), "contract_hash": ev.get("contract_hash"), "results": ev.get("controls"),
                             "intent_record": it.get("entry_controls")},
                "exposure": {"gate_enabled": ev.get("gate_enabled"), "gate": ev.get("gate"), "external": ev.get("external"),
                             "note": None if ev.get("external") is not None else "not read in this cycle (the exposure gate is off): see the context rows"},
                "market": {"session": market_session(m["at"], ev.get("clock") or None), "broker_clock": ev.get("clock"),
                           "regime_fields": {k: (setup.get("inputs") or {}).get(k) for k in ("stretch", "confidence", "demand", "evidence_strength", "evidence_cause",
                                                                                            "market_data_source", "mode", "session_hint")}},
                "account": ev.get("account"), "environment": it.get("environment") or ev.get("environment"), "safety": ev.get("safety"), "mode": ev.get("mode"),
                "limits": ev.get("limits"), "decided_at": m["at"], "intent_created_at": it.get("created_at"), "recovered": False}
        if not ev:
            snap["note"] = "the cycle's EVALUATED record was not seen: controls / exposure / account are unavailable for this entry"
        s = P.sha(snap)
        cand = self._candidate(it.get("setup_id") or m.get("setup_id"), it.get("symbol"), STRATEGY_ID, None, None, m["at"], setup.get("state") or "READY", cohort_id, 0, sv)
        dec = self._exec_decision({**ev, "run_id": m.get("run_id"), "at": m["at"], "observed_at": m["observed_at"], "setup_id": it.get("setup_id"), "symbol": it.get("symbol")},
                                  ENTRY_INTENT, ["ALL_CONTROLS_PASSED"], execution_id=xid, decision_id=did,
                                  detail={"intent": {k: it.get(k) for k in ("qty", "client_order_id", "created_at")}})
        return [("forward_capture_candidates", cand), dec,
                ("forward_capture_entry_snapshots", {"execution_id": xid, "candidate_id": snap["candidate_id"], "decision_id": did, "cohort_id": cohort_id, "captured_at": m["observed_at"],
                                                     "snapshot_json": dumps(snap), "snapshot_sha256": s, "recovered": 0})]

    # ---------------------------------------------------------------- the lifecycle from persisted rows -----------------------------------
    def _lifecycle(self, now: datetime, epoch: str, pending_x: set) -> List[Tuple[str, dict]]:
        x = self.fv.execution
        q = self.store.query
        rows: List[Tuple[str, dict]] = []
        snaps = {r["execution_id"]: r["cohort_id"] for r in q("SELECT execution_id, cohort_id FROM forward_capture_entry_snapshots")}
        sv = strategy_version()
        current = (self.cohort or {}).get("cohort_id") or "UNSNAPSHOTTED"
        history = q("SELECT at, cohort_id FROM forward_capture_config_snapshots ORDER BY at, rowid")

        def cohort_at(ts) -> str:                                        # the configuration in force at `ts`, not the one when the capture looks
            hit = self._at(history, str(ts or "")) or (history[0] if history else None)
            return hit["cohort_id"] if hit else current
        for r in q("SELECT position_id FROM paper_positions p WHERE (p.opened_at >= ? OR IFNULL(p.closed_at, '') >= ?) AND NOT EXISTS (SELECT 1 FROM forward_capture_decisions d "
                   "WHERE d.position_id = p.position_id AND d.decision = 'ENTERED') ORDER BY opened_at LIMIT 200", (epoch, epoch)):
            p = x.store.position(r["position_id"])
            if p is None:
                continue
            co = snaps.get(p.execution_id) or ("LEGACY" if (p.opened_at or "") < epoch else cohort_at(p.opened_at))
            rows.append(("forward_capture_decisions", self._decision(
                decision_id=fid("decision", ENTERED, p.position_id), setup_id=p.setup_id, symbol=p.symbol, cohort_id=co, decided_at=p.opened_at, observed_at=_iso(now),
                market_session=market_session(p.opened_at), mode=None, source="POSITION", source_ref=p.position_id, decision=ENTERED, reasons=["FILLED"],
                execution_id=p.execution_id, position_id=p.position_id, legacy=1 if (p.opened_at or "") < epoch else 0,
                detail={"first_observed": {"entry_qty": p.entry_qty, "average_entry_price": p.average_entry_price, "state": p.state}, "source": p.source})))
        for r in q("SELECT position_id FROM paper_positions p WHERE p.state = 'CLOSED' AND IFNULL(p.closed_at, '') >= ? AND NOT EXISTS (SELECT 1 FROM forward_capture_decisions d "
                   "WHERE d.position_id = p.position_id AND d.decision = 'EXITED') ORDER BY closed_at LIMIT 200", (epoch,)):
            p = x.store.position(r["position_id"])
            if p is None or p.state != "CLOSED":
                continue
            entry_cohort = snaps.get(p.execution_id)
            co = entry_cohort or ("LEGACY" if (p.opened_at or "") < epoch else cohort_at(p.opened_at))
            outcome = self._outcome(p, co, cohort_at(p.closed_at), sv)
            rows.append(("forward_capture_decisions", self._decision(
                decision_id=fid("decision", EXITED, p.position_id), setup_id=p.setup_id, symbol=p.symbol, cohort_id=co, decided_at=p.closed_at, observed_at=_iso(now),
                market_session=market_session(p.closed_at), mode=None, source="POSITION", source_ref=p.position_id, decision=EXITED, reasons=[p.exit_reason or "UNKNOWN"],
                execution_id=p.execution_id, position_id=p.position_id, legacy=1 if (p.opened_at or "") < epoch else 0,
                detail={"exit_qty": p.exit_qty, "average_exit_price": p.average_exit_price, "realized_pnl": p.realized_pnl})))
            rows.append(("forward_capture_outcomes", {"position_id": p.position_id, "execution_id": p.execution_id, "candidate_id": candidate_id(p.setup_id) if p.setup_id else None,
                                                      "cohort_id": co, "closed_at": p.closed_at, "outcome_json": dumps(outcome), "outcome_sha256": P.sha(outcome),
                                                      "cross_cohort": 1 if outcome.get("cross_cohort_hold") else 0}))
        for r in q("SELECT execution_id FROM execution_intents i WHERE i.intent_type = 'ENTRY' AND i.created_at >= ? AND i.filled_qty = 0 AND i.state IN ('NOT_SENT', 'REJECTED', "
                   "'CANCELED', 'FAILED') AND NOT EXISTS (SELECT 1 FROM forward_capture_decisions d WHERE d.execution_id = i.execution_id AND d.decision = 'NOT_ENTERED') "
                   "ORDER BY created_at LIMIT 200", (epoch,)):
            it = x.store.intent(r["execution_id"])
            if it is None:
                continue
            at = it.resolved_at or it.updated_at or it.created_at
            rows.append(("forward_capture_decisions", self._decision(
                decision_id=fid("decision", NOT_ENTERED, it.execution_id), setup_id=it.setup_id, symbol=it.symbol, cohort_id=snaps.get(it.execution_id) or cohort_at(it.created_at), decided_at=at,
                observed_at=_iso(now), market_session=market_session(at), mode=None, source="INTENT", source_ref=it.execution_id, decision=NOT_ENTERED,
                reasons=[it.failure_code or it.state], execution_id=it.execution_id,
                detail={"state": it.state, "failure_code": it.failure_code, "failure_text": str(it.failure_text or "")[:300], "broker_status": it.broker_status,
                        "outcome_class": it.outcome_class, "attempt_count": it.attempt_count})))
        for r in q("SELECT execution_id FROM execution_intents i WHERE i.intent_type = 'ENTRY' AND i.created_at >= ? AND NOT EXISTS (SELECT 1 FROM forward_capture_decisions d "
                   "WHERE d.execution_id = i.execution_id AND d.decision = 'ENTRY_INTENT') ORDER BY created_at LIMIT 200", (epoch,)):
            xid = r["execution_id"]
            if xid in pending_x:
                continue
            it = x.store.intent(xid)
            if it is None:
                continue
            ours = it.created_by == getattr(x, "owner", None)
            ended = bool(self.last_cycle_finished and str(it.created_at) <= self.last_cycle_finished)
            first = self._unmarked.setdefault(xid, now)                  # timed from when the capture first saw it unmarked (created_at is the cycle start)
            prog = getattr(x, "progress", None) or {}
            in_flight = bool(prog.get("started_at")) and str(it.created_at) >= str(prog.get("started_at"))
            lost = not in_flight and (now - first).total_seconds() >= LOST_MARKER_AFTER_S
            if ours and not (ended or lost):
                continue                                                 # the creating cycle has not ended yet: its INTENT marker may still come
            self._unmarked.pop(xid, None)
            rows += self._recovered_intent(now, it, cohort_at(it.created_at), sv)
        if len(self._unmarked) > 500:
            self._unmarked = dict(list(self._unmarked.items())[-500:])
        return rows

    def _recovered_intent(self, now: datetime, it, cohort_id: str, sv: str) -> List[Tuple[str, dict]]:
        """An entry intent without its decision-time marker (a crash, a previous process, a lost marker): rebuilt ONLY from the persisted intent and
        the persisted samples before it, flagged recovered=1. The per-control results of that cycle are UNAVAILABLE - never reconstructed."""
        q = self.store.query
        sample = q("SELECT at, equity, cash, buying_power, account_status, account_age_s FROM forward_validation_samples WHERE at < ? ORDER BY at DESC, rowid DESC LIMIT 1",
                   (it.created_at,))
        acct = self._sample_account(sample[0] if sample else None)
        d = P.intent_at_decision(it)
        did = fid("decision", "ENTRY_INTENT", it.execution_id)
        snap = {"version": VERSION, "execution_id": it.execution_id, "candidate_id": candidate_id(it.setup_id) if it.setup_id else None, "decision_id": did, "cohort_id": cohort_id,
                "symbol": it.symbol, "setup_id": it.setup_id, "proposal_id": it.proposal_id, "strategy": {"strategy_id": STRATEGY_ID, "strategy_version": sv},
                "planned": {"qty": it.qty, "proposal_qty": it.proposal_qty, "reference_price": it.reference_price, "invalidation_price": it.invalidation_price,
                            "risk_budget": it.risk_budget, "risk_per_share": (d.get("proposal_snapshot") or {}).get("risk_per_share")},
                "controls": {"version": (d.get("entry_controls") or {}).get("version"), "contract_hash": (d.get("entry_controls") or {}).get("contract_hash"),
                             "results": "UNAVAILABLE", "intent_record": d.get("entry_controls")},
                "market": {"session": market_session(it.created_at)}, "account": acct, "environment": d.get("environment"), "decided_at": it.created_at,
                "intent_created_at": it.created_at, "recovered": True,
                "note": "recovered from the persisted intent: the decision-time marker was not captured; account data is the last observer sample before the intent"}
        rows: List[Tuple[str, dict]] = []
        if it.setup_id:
            rows.append(("forward_capture_candidates", self._candidate(it.setup_id, it.symbol, STRATEGY_ID, None, None, it.created_at, "READY", cohort_id, 0, sv)))
        rows.append(("forward_capture_decisions", self._decision(
            decision_id=did, setup_id=it.setup_id, symbol=it.symbol, cohort_id=cohort_id, decided_at=it.created_at, observed_at=_iso(now), market_session=market_session(it.created_at),
            mode=None, source="INTENT", source_ref=it.execution_id, decision=ENTRY_INTENT, reasons=["RECOVERED_FROM_INTENT"], proposal=d.get("proposal_snapshot"), account=acct,
            controls_version=snap["controls"]["version"], contract_hash=snap["controls"]["contract_hash"], execution_id=it.execution_id, recovered=1,
            detail={"intent": {k: d.get(k) for k in ("qty", "client_order_id", "created_at", "created_by")}})))
        rows.append(("forward_capture_entry_snapshots", {"execution_id": it.execution_id, "candidate_id": snap["candidate_id"], "decision_id": did, "cohort_id": cohort_id,
                                                         "captured_at": _iso(now), "snapshot_json": dumps(snap), "snapshot_sha256": P.sha(snap), "recovered": 1}))
        self.fault(now, "capture", "ENTRY_RECOVERED", {"execution_id": it.execution_id, "symbol": it.symbol}, key=it.execution_id, at=it.created_at)
        return rows

    def _outcome(self, p, cohort_id: str, at_close: str, sv: str) -> Dict[str, Any]:
        """The closed position's outcome from the Stage 6.1 trade record (persisted rows only). MFE / MAE are the observer's SAMPLED marks, labelled
        so; with no mark they are None - never estimated from bars that were not recorded."""
        t = self.fv._trade(p)
        opened, closed = _parse(p.opened_at), _parse(p.closed_at)
        exc = t.get("excursions") or {}
        ec = t.get("entry_controls") or {}
        return {"version": VERSION, "position_id": p.position_id, "execution_id": p.execution_id, "setup_id": p.setup_id, "symbol": p.symbol, "cohort_id": cohort_id,
                "cross_cohort_hold": cohort_id not in ("LEGACY", at_close), "strategy_version": sv,
                "controls_version": ec.get("version"), "contract_hash": ec.get("contract_hash"), "entry_at": p.opened_at, "exit_at": p.closed_at,
                "holding_seconds": int((closed - opened).total_seconds()) if opened and closed else None, "holding_sessions": p.sessions_held,
                "entry_qty": p.entry_qty, "exit_qty": p.exit_qty, "average_entry": p.average_entry_price, "average_exit": p.average_exit_price,
                "realized_pnl": p.realized_pnl, "gross_pnl": t.get("gross_pnl"), "return_pct": t.get("return_pct"), "exit_reason": p.exit_reason, "exit_cycles": p.exit_cycle,
                "entry_slippage": t.get("entry_slippage"), "exit_slippage": t.get("exit_slippage"),
                "excursions": {**exc, "basis": "sampled observer marks of the executor's last price (not intraday highs / lows)"} if exc.get("marks") else
                {"mfe_price": None, "mae_price": None, "marks": 0, "basis": "NOT_AVAILABLE: no price mark was sampled while the position was open"},
                "fees_note": t.get("fees_note")}

