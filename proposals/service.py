"""
proposals/service.py — the one place a review proposal is generated (DESIGN_59 §3–§14). Inputs come from the Trader View stream
(quote, candles, daily bars, market state), the stored Stage 5.8 setup state and the read-only account snapshot; the pure engine
decides; the store persists; listeners (the socket) are told afterwards. Review only: no order, no broker, no model, no research run.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import config
from proposals import DISCLAIMER, SETUP_TYPE, SIDE_CONTEXT, SIDE_LABEL, STATUS_LABELS, SUBMISSION_STATUS, VERSION
from proposals.account import AccountAdapter
from proposals.engine import build, eligibility, materially_changed
from proposals.models import PriceCandidate, ProposalInputs, ProposalView
from proposals.store import ProposalStore
from setups import MIN_FRESH_WINDOW_SECONDS
from setups.market_hours import is_regular_session, next_session_open
from setups.models import SetupView

log = logging.getLogger(__name__)
NOT_GENERATED = "No proposal generated for this episode yet (generated after each setup evaluation: subscribe, refresh, the in-session cadence and the evaluator)."


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _session_today(now_ts: float):
    from trader_view.ppe_provider import session_today      # the Stage 5.6 New York session date helper
    return session_today(now_ts)


def _seconds_to_close(now: datetime) -> Optional[int]:
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return None
    close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    return max(0, int((close - et).total_seconds()))


class ProposalService:
    def __init__(self, stream_fn: Callable[[], object], store: Optional[ProposalStore] = None, *, account: Optional[AccountAdapter] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), owner: Optional[str] = None, risk_fraction: Optional[float] = None,
                 max_position_pct: Optional[float] = None, ttl_seconds: Optional[int] = None, price_change_pct: Optional[float] = None, safety=None):
        self.stream_fn = stream_fn
        self.safety = safety                                 # Stage 5.9.1: the execution safety gate (safety.service.ExecutionSafetyService); the stream wires it
        self.store = store or ProposalStore()
        self.account = account or AccountAdapter()
        self.clock = clock
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.risk_fraction = str(risk_fraction if risk_fraction is not None else config.TRADE_PROPOSAL_RISK_FRACTION)
        self.max_position_pct = str(max_position_pct if max_position_pct is not None else config.MAX_POSITION_PERCENT)
        self.ttl_seconds = int(ttl_seconds if ttl_seconds is not None else config.TRADE_PROPOSAL_TTL_SECONDS)
        self.price_change_pct = Decimal(str(price_change_pct if price_change_pct is not None else config.TRADE_PROPOSAL_PRICE_CHANGE_PCT))
        self.listeners: List[Callable[[dict], None]] = []
        self.overrides: Dict[str, Dict[str, Any]] = {}      # tests / harness only: per-symbol account overrides (set through the test-only socket command)

    # ---------------------------------------------------------------- helpers ---------------------------------------------------
    def _setups(self):
        return self.stream_fn().setups

    def setup_view(self, symbol: str) -> SetupView:
        return self._setups().current(symbol.upper())

    @staticmethod
    def effective(row: Optional[dict], view: SetupView, now_iso: str):
        """(effective status, reason) for a stored row against the CURRENT setup state. Derived, never written."""
        if row is None:
            return None, None
        if row.get("expires_at") and now_iso >= row["expires_at"]:
            return "EXPIRED", "past expires_at"
        snap = row.get("setup_snapshot") or {}
        if view.setup_id != row.get("setup_id"):
            return "STALE", "the setup episode changed"
        if view.state != "READY":
            return "STALE", f"the setup is no longer READY ({view.state})"
        if snap.get("since") and view.since and snap.get("since") != view.since:
            return "STALE", "the setup transitioned since the proposal was generated"
        return row["status"], None

    def execution_eligibility(self, status: str, now: datetime) -> Dict[str, Any]:
        """Stage 5.9.1: may a future execution layer act on this proposal NOW? Derived from the live gate; never stored and never acted upon here."""
        if self.safety is None:
            return {"allowed": False, "state": "UNKNOWN", "state_label": "Trading stopped", "reason_codes": ["SAFETY_GATE_NOT_CONNECTED"], "reason_text": "Safety gate not connected", "evaluated_at": None}
        sv = self.safety.current(now=now)
        codes = list(sv.reason_codes) + ([] if status == "REVIEWABLE" else [f"PROPOSAL_{status}"])
        return {"allowed": bool(sv.allowed_new_entry and status == "REVIEWABLE"), "state": sv.state, "state_label": sv.state_label, "reason_codes": codes,
                "reason_text": sv.reason_text or ("" if status == "REVIEWABLE" else f"proposal {status.lower()}"), "evaluated_at": sv.evaluated_at}

    def view_of(self, row: Optional[dict], view: SetupView, now: datetime, *, trigger: Optional[str] = None) -> ProposalView:
        s = view.symbol
        now_iso = _iso(now)
        if row is None:
            codes = eligibility(ProposalInputs(symbol=s, now=now_iso, now_ts=now.timestamp(), setup_view=view.model_dump(),
                                               episode=self._setups().store.open_episode(s).model_dump() if self._setups().store.open_episode(s) else None))
            from proposals import TEXT
            if codes:
                return ProposalView(symbol=s, setup_id=view.setup_id, status="NOT_ELIGIBLE", effective_status="NOT_ELIGIBLE", status_label=STATUS_LABELS["NOT_ELIGIBLE"],
                                    blockers=[{"code": c, "text": TEXT[c]} for c in codes], setup_snapshot=view.model_dump(), trigger=trigger, mode=view.mode, source=view.source,
                                    execution_eligibility=self.execution_eligibility("NOT_ELIGIBLE", now))
            return ProposalView(symbol=s, setup_id=view.setup_id, status="NOT_ELIGIBLE", effective_status="STALE", status_label=STATUS_LABELS["STALE"], stale_reason="not generated yet",
                                setup_snapshot=view.model_dump(), trigger=trigger, mode=view.mode, source=view.source, note=NOT_GENERATED,
                                execution_eligibility=self.execution_eligibility("STALE", now))
        eff, reason = self.effective(row, view, now_iso)
        keys = set(ProposalView.model_fields) - {"version", "side_label", "effective_status", "status_label", "stale_reason", "submission_status", "disclaimer", "note", "trigger",
                                                "execution_eligibility"}
        data = {k: row[k] for k in keys if k in row}
        return ProposalView(**data, effective_status=eff, status_label=STATUS_LABELS.get(eff, eff), stale_reason=reason, trigger=trigger or row.get("trigger"),
                            execution_eligibility=self.execution_eligibility(eff, now))

    def current(self, symbol: str, now: Optional[datetime] = None) -> ProposalView:
        """The stored view for a ticker (never generates)."""
        now = now or self.clock()
        s = symbol.upper()
        view = self.setup_view(s)
        row = self.store.latest(s)
        if row is not None and view.setup_id and row.get("setup_id") != view.setup_id:
            row = self.store.latest_for_setup(view.setup_id) or row                     # the newest row of the CURRENT episode when one exists
        return self.view_of(row, view, now)

    def overview(self, now: Optional[datetime] = None) -> dict:
        now = now or self.clock()
        eps = self._setups().store.open_episodes()
        items = [self.current(e.symbol, now).model_dump() for e in eps]
        return {"version": VERSION, "setup_type": SETUP_TYPE, "side_context": SIDE_CONTEXT, "side_label": SIDE_LABEL, "proposals": items, "count": len(items),
                "stored": self.store.count(), "submission_status": SUBMISSION_STATUS, "disclaimer": DISCLAIMER}

    # ---------------------------------------------------------------- inputs (already-held data only) ------------------------
    def inputs_for(self, symbol: str, now: datetime, view: SetupView, prefetched: Optional[dict] = None, overrides: Optional[Dict[str, Any]] = None) -> ProposalInputs:
        st = self.stream_fn()
        s = symbol.upper()
        pre = prefetched or {}
        now_ts = now.timestamp()
        mode = st.market.mode
        q = pre.get("quote") or st.market.quote(s)
        qage = (now_ts - float(q.ts)) if q.ts is not None else None
        cands: List[PriceCandidate] = []
        if q.status == "OK" and q.bid is not None and q.ask is not None and q.bid > 0 and q.ask >= q.bid and (q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100 <= 1.0:
            cands.append(PriceCandidate(source="QUOTE_MID", value=(q.bid + q.ask) / 2, ts=q.ts, age_s=qage))
        else:
            cands.append(PriceCandidate(source="QUOTE_MID", status="UNAVAILABLE", note="no usable bid/ask"))
        cands.append(PriceCandidate(source="LAST_TRADE", value=q.price, ts=q.ts, age_s=qage) if q.status == "OK" and q.price is not None
                     else PriceCandidate(source="LAST_TRADE", status="UNAVAILABLE", note=q.message or q.status))
        candles = []
        try:
            candles = list(st.candles(s))
        except Exception as exc:  # noqa: BLE001
            log.warning("proposal: candles unavailable for %s (%s)", s, type(exc).__name__)
        finals = [c for c in candles if c.final]
        if finals:
            last = finals[-1]
            cands.append(PriceCandidate(source="MINUTE_CLOSE", value=last.close, ts=float(last.time), age_s=now_ts - float(last.time)))
        else:
            cands.append(PriceCandidate(source="MINUTE_CLOSE", status="UNAVAILABLE", note="no completed minute candle"))
        today = _session_today(now_ts)
        bars = []
        try:
            raw = (st.market.daily_bars([s], 60) or {}).get(s) or []
            bars = [{"date": str(b.date), "open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in raw if b.date < today]
        except Exception as exc:  # noqa: BLE001
            log.warning("proposal: daily bars unavailable for %s (%s)", s, type(exc).__name__)
        if bars:
            cands.append(PriceCandidate(source="DAILY_CLOSE", value=bars[-1]["close"], mode="DAILY_REFERENCE", note=f"close of {bars[-1]['date']}"))
        else:
            cands.append(PriceCandidate(source="DAILY_CLOSE", status="UNAVAILABLE", mode="DAILY_REFERENCE", note="no completed daily bar"))
        intraday_lows = [c.low for c in finals if _session_today(float(c.time)) == today]
        pos = pre.get("position")
        safety_view = self.safety.evaluate("PROPOSAL", now) if self.safety is not None else None            # Stage 5.9.1: the persisted gate feeds the halt controls
        acct = self.account.snapshot(s, mode, now_ts, overrides if overrides is not None else self.overrides.get(s), safety=safety_view)
        regular = is_regular_session(now)
        session = {"regular_session": regular, "seconds_to_close": _seconds_to_close(now) if regular else None, "next_open": _iso(next_session_open(now)),
                   "session_hint": (view.inputs.session_hint if view.inputs else "")}
        ppe = pre.get("ppe")
        fallback = False
        try:
            ppe = ppe or st.ppe_result(s)
            fallback = str(getattr(ppe.sources, "benchmark_kind", "") or "").upper() == "MARKET"
            ppe_snap = {"status": ppe.status, "stretch": ppe.stretch.score, "stretch_band": getattr(ppe.stretch, "band", None), "confidence": ppe.confidence.score,
                        "coverage": ppe.stretch.coverage_weight, "computed_at": ppe.sources.computed_at, "benchmark": getattr(ppe.sources, "benchmark", None),
                        "benchmark_kind": getattr(ppe.sources, "benchmark_kind", None)}
        except Exception as exc:  # noqa: BLE001
            ppe_snap = {"status": "UNAVAILABLE", "error": type(exc).__name__}
        si = view.inputs
        demand_snap = {"status": si.demand_status, "score": si.demand, "coverage": si.demand_coverage, "mode": si.demand_mode, "computed_at": si.demand_computed_at} if si else {}
        evidence_snap = {"available": si.evidence_available, "packet_id": si.evidence_packet_id, "created_at": si.evidence_created_at, "age_days": si.evidence_age_days,
                         "cause": si.evidence_cause, "strength": si.evidence_strength, "company_confirm": si.company_confirm, "guidance_down": si.guidance_down} if si else {}
        ep = self._setups().store.open_episode(s)
        return ProposalInputs(symbol=s, now=_iso(now), now_ts=now_ts, mode=mode, market_data_source=st.market.source, setup_view=view.model_dump(),
                              episode=ep.model_dump() if ep else None, price_candidates=cands, daily_bars=bars, intraday_lows=intraday_lows, account=acct, session=session,
                              risk_fraction=self.risk_fraction, max_position_pct=self.max_position_pct, ttl_seconds=self.ttl_seconds,
                              setup_fresh_window_s=max(3 * float(config.TRADER_SETUP_REFRESH_SECONDS), MIN_FRESH_WINDOW_SECONDS), sector_fallback=fallback,
                              ppe_snapshot=ppe_snap, demand_snapshot=demand_snap, evidence_snapshot=evidence_snap)

    # ---------------------------------------------------------------- the real generation path -------------------------------
    def evaluate(self, symbol: str, now: Optional[datetime] = None, trigger: str = "MANUAL", *, setup_view: Optional[SetupView] = None, prefetched: Optional[dict] = None,
                 origin: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None, inputs: Optional[ProposalInputs] = None) -> ProposalView:
        now = now or self.clock()
        s = symbol.upper()
        view = setup_view or self.setup_view(s)
        inp = inputs if inputs is not None else self.inputs_for(s, now, view, prefetched, overrides)
        draft = build(inp)
        row = None
        if draft.status == "NOT_ELIGIBLE":
            row = self.store.latest_for_setup(view.setup_id) if view.setup_id else None      # an existing row reads STALE against the changed setup
        else:
            prev = self.store.latest_for_setup(draft.setup_id)
            reason = materially_changed(prev, draft, self.price_change_pct, _iso(now))
            if reason is None:
                self.store.touch_controls(draft.controls, now, s, prev["proposal_id"])
                row = prev
            else:
                row = self.store.insert(draft, now, trigger, self.owner)
                log.info("proposal %s for %s: %s (%s)", row["proposal_id"], s, draft.status, reason)
        out = self.view_of(row, view, now, trigger=trigger)
        for fn in list(self.listeners):
            try:
                fn({"type": "proposal", "trigger": trigger, "origin": origin, **out.model_dump()})
            except Exception as exc:  # noqa: BLE001 - a listener problem never undoes a persisted proposal
                log.warning("proposal listener failed (%s)", type(exc).__name__)
        return out

    def on_setup_event(self, event: dict) -> None:
        """Setup-service listener: a setup evaluation the stream did not initiate (evaluator, manual, test) refreshes the proposal."""
        if event.get("type") != "setup" or event.get("origin") == "stream":
            return
        try:
            self.evaluate(str(event.get("symbol") or ""), None, "SETUP_EVENT", origin="service")
        except Exception as exc:  # noqa: BLE001 - never breaks the setup path
            log.warning("proposal evaluation after a setup event failed (%s)", type(exc).__name__)

    def set_review(self, proposal_id: str, state: str, now: Optional[datetime] = None) -> Optional[ProposalView]:
        now = now or self.clock()
        row = self.store.set_review(proposal_id, state, now)
        if row is None:
            return None
        return self.view_of(row, self.setup_view(row["symbol"]), now)
