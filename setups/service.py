"""
setups/service.py — the one place state changes happen (DESIGN_58 §3, §6–§9). Inputs come from the Trader View stream's providers
(PPE, Market Demand, the stored Stage 5.4 packet, the quote, the market mode); the pure engine decides; the store persists in one
transaction; listeners (the socket) are told afterwards. Read-only toward every other subsystem: no order, no broker, no model, no
research run, no position read.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import config
from setups import (DISCLAIMER, MIN_FRESH_WINDOW_SECONDS, QUOTE_MAX_AGE_SECONDS, R_PAUSED_PPE, R_PAUSED_STALE, SETUP_TYPE, STATE_LABELS)
from setups.engine import evaluate
from setups.models import Decision, EpisodeRecord, SetupInputs, SetupView
from setups.store import SetupStore

log = logging.getLogger(__name__)


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


def alert_text(decision: Decision, i: SetupInputs) -> Dict[str, str]:
    """Alert title / body (DESIGN_58 §7). No buy / sell wording — the state label and the deterministic reason only."""
    label = STATE_LABELS[decision.to_state]
    title = f"{i.symbol} — {label}"
    metrics = f"PPE Stretch: {i.stretch:+.0f}" if i.stretch is not None else "PPE Stretch: —"
    metrics += f" · Confidence: {i.confidence if i.confidence is not None else '—'} · Demand: {i.demand if i.demand is not None else '—'}"
    if decision.to_state == "INVALIDATED":
        body = f"Recovery setup invalidated.\nReason: {decision.reason_text.rstrip('.').lower()}.\n{metrics}"
    elif decision.to_state == "READY":
        body = f"Buyer confirmation strengthened.\n{metrics}"
    elif decision.to_state == "WATCH" and decision.from_state == "READY":
        body = f"Confirmation weakened; back to WATCH.\nReason: {decision.reason_text.rstrip('.').lower()}.\n{metrics}"
    else:
        body = f"Downside dislocation detected.\n{metrics}"
    return {"title": title, "body": body}


class SetupService:
    def __init__(self, stream_fn: Callable[[], object], store: Optional[SetupStore] = None, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 owner: Optional[str] = None, cooldown_minutes: Optional[int] = None):
        self.stream_fn = stream_fn
        self.store = store or SetupStore()
        self.clock = clock
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.cooldown_minutes = int(cooldown_minutes if cooldown_minutes is not None else config.SETUP_ALERT_COOLDOWN_MINUTES)
        self.listeners: List[Callable[[dict], None]] = []

    # ---------------------------------------------------------------- views (never evaluate) ---------------------------------
    def current(self, symbol: str) -> SetupView:
        s = symbol.upper()
        ep = self.store.open_episode(s)
        last = self.store.last_evaluation(s)
        inputs = None
        freshness: dict = {}
        if last:
            try:
                import json
                inputs = SetupInputs(**json.loads(last["inputs_json"])) if last["inputs_json"] not in ("{}", None) else None
                freshness = json.loads(last["freshness_json"] or "{}")
            except Exception:  # noqa: BLE001 - a malformed historical row never breaks the view
                inputs, freshness = None, {}
        state = ep.state if ep else "NONE"
        return SetupView(symbol=s, state=state, state_label=STATE_LABELS[state], setup_id=ep.setup_id if ep else None, cycle_id=ep.cycle_id if ep else None,
                         previous_state=ep.previous_state if ep else None, since=ep.last_transition_at if ep else None, started_at=ep.started_at if ep else None,
                         last_evaluated_at=last["evaluated_at"] if last else None, decision=last["decision"] if last else None,
                         reason_code=last["reason_code"] if last else None, reason_text=last["reason_text"] if last else None,
                         blockers=[b for b in (freshness.get("blockers") or [])], invalidation_reason=ep.invalidation_reason if ep else None,
                         paused=bool(last and last["decision"] == "PAUSED"), inputs=inputs, freshness=freshness,
                         source=(last or {}).get("source") or "", mode=(last or {}).get("mode") or "", disclaimer=DISCLAIMER,
                         note=None if last else "Not evaluated yet (evaluated on subscribe, on refresh and every minute during the regular session).")

    def overview(self) -> dict:
        eps = self.store.open_episodes()
        return {"version": "setups_v1", "setup_type": SETUP_TYPE, "open": [self.current(e.symbol).model_dump() for e in eps], "count": len(eps),
                "controls": self.store.controls(), "disclaimer": DISCLAIMER}

    # ---------------------------------------------------------------- inputs from already-computed research --------------------
    def inputs_for(self, symbol: str, now: datetime, prefetched: Optional[dict] = None) -> SetupInputs:
        """Build the inputs from the stream's already-computed results; `prefetched` (ppe / demand / evidence / quote) avoids a second read."""
        st = self.stream_fn()
        s = symbol.upper()
        pre = prefetched or {}
        ppe = pre.get("ppe") or st.ppe_result(s)
        dem = pre.get("demand") or st.demand_result(s)
        ev = pre.get("evidence") or st.research.latest(s)
        q = pre.get("quote") or st.market.quote(s)
        ms = {}
        try:
            ms = st.market.market_state() or {}
        except Exception:  # noqa: BLE001
            ms = {}
        snap = ppe.confidence.evidence
        age = None
        created = ev.created_at if ev.status == "LOADED" and ev.packet_status == "COMPLETED" else None
        if created:
            d = _parse(created)
            age = max(0, (now - d).days) if d else None
        return SetupInputs(symbol=s, evaluated_at=_iso(now), stretch=ppe.stretch.score, stretch_status=ppe.stretch.status, ppe_status=ppe.status,
                           ppe_coverage=float(ppe.stretch.coverage_weight or 0.0), ppe_computed_at=ppe.sources.computed_at, confidence=ppe.confidence.score,
                           confidence_status=ppe.confidence.status, demand=dem.score, demand_status=dem.status, demand_coverage=float(dem.coverage_weight or 0.0),
                           demand_mode=dem.mode, demand_computed_at=dem.sources.computed_at, evidence_available=created is not None,
                           evidence_packet_id=ev.packet_id if created else None, evidence_created_at=created, evidence_age_days=age,
                           evidence_cause=ev.cause if created else None, evidence_strength=ev.strength if created else None,
                           company_confirm=ev.company_confirm if created else None, guidance_down=int(snap.guidance_down) if snap else 0,
                           company_down=int(snap.company_down) if snap else 0, quote_ts=q.ts, quote_status=q.status, market_data_source=st.market.source,
                           mode=st.market.mode, session_hint=str(ms.get("session_hint") or ms.get("state") or ""))

    def with_freshness(self, i: SetupInputs, now: datetime) -> SetupInputs:
        if i.mode in ("simulated", "test", "replay"):
            return i.model_copy(update={"ppe_fresh": True, "demand_fresh": True, "quote_fresh": True, "stale_reasons": []})
        ppe_win = max(3 * float(config.TRADER_PPE_REFRESH_SECONDS), MIN_FRESH_WINDOW_SECONDS)
        dem_win = max(3 * float(config.TRADER_DEMAND_REFRESH_SECONDS), MIN_FRESH_WINDOW_SECONDS)
        reasons = []
        pc, dc = _parse(i.ppe_computed_at), _parse(i.demand_computed_at)
        ppe_fresh = pc is not None and (now - pc).total_seconds() <= ppe_win
        dem_fresh = dc is not None and (now - dc).total_seconds() <= dem_win
        quote_fresh = i.quote_ts is not None and i.quote_status == "OK" and (now.timestamp() - i.quote_ts) <= QUOTE_MAX_AGE_SECONDS
        if not ppe_fresh:
            reasons.append("PPE_STALE")
        if not dem_fresh:
            reasons.append("DEMAND_STALE")
        if not quote_fresh:
            reasons.append("QUOTE_STALE")
        return i.model_copy(update={"ppe_fresh": ppe_fresh, "demand_fresh": dem_fresh, "quote_fresh": quote_fresh, "stale_reasons": reasons})

    @staticmethod
    def freshness_record(i: SetupInputs, decision: Decision) -> dict:
        return {"ppe_fresh": i.ppe_fresh, "demand_fresh": i.demand_fresh, "quote_fresh": i.quote_fresh, "stale_reasons": list(i.stale_reasons),
                "ppe_computed_at": i.ppe_computed_at, "demand_computed_at": i.demand_computed_at, "evidence_created_at": i.evidence_created_at,
                "evidence_age_days": i.evidence_age_days, "quote_ts": i.quote_ts, "blockers": list(decision.blockers), "paused": decision.decision == "PAUSED"}

    # ---------------------------------------------------------------- the real evaluation path ----------------------------------
    def evaluate_symbol(self, symbol: str, now: Optional[datetime] = None, trigger: str = "MANUAL", inputs: Optional[SetupInputs] = None,
                        prefetched: Optional[dict] = None, origin: Optional[str] = None) -> SetupView:
        now = now or self.clock()
        s = symbol.upper()
        prev = self.store.open_episode(s)
        try:
            i = inputs if inputs is not None else self.inputs_for(s, now, prefetched)
            i = i.model_copy(update={"symbol": s, "evaluated_at": _iso(now)})
            i = self.with_freshness(i, now)
            decision = evaluate(prev, i)
            out = self.store.record(inputs=i, freshness=self.freshness_record(i, decision), decision=decision, prev=prev, now=now, trigger=trigger, owner=self.owner,
                                    alert_text=alert_text, cooldown_minutes=self.cooldown_minutes)
        except Exception as exc:  # noqa: BLE001 - recorded as ERROR for this symbol; the caller continues with the next one
            log.warning("setup evaluation failed for %s (%s)", s, type(exc).__name__)
            try:
                self.store.record_error(s, now, trigger, self.owner, f"{type(exc).__name__}: {exc}", prior_state=prev.state if prev else "NONE", setup_id=prev.setup_id if prev else None)
            except Exception as exc2:  # noqa: BLE001 - the database itself failed: say so, never pretend
                log.error("setup evaluation could not even record its error for %s (%s)", s, type(exc2).__name__)
            raise
        view = self.current(s)
        for fn in list(self.listeners):
            try:
                fn({"type": "setup", "trigger": trigger, "origin": origin, **view.model_dump()})
                if out["alert"] is not None:
                    fn({"type": "setup_alert", **out["alert"].model_dump()})
            except Exception as exc:  # noqa: BLE001 - a listener problem never undoes a persisted transition
                log.warning("setup listener failed (%s)", type(exc).__name__)
        return view

    def symbols_in_scope(self) -> List[str]:
        st = self.stream_fn()
        out: List[str] = []
        try:
            out += list(st.watch_symbols())
        except Exception as exc:  # noqa: BLE001
            log.warning("watchlist unavailable for setup evaluation (%s)", type(exc).__name__)
        for ep in self.store.open_episodes():
            if ep.symbol not in out:
                out.append(ep.symbol)
        return out

    def evaluate_all(self, now: Optional[datetime] = None, trigger: str = "SCHEDULED", symbols: Optional[List[str]] = None) -> dict:
        now = now or self.clock()
        syms = list(symbols) if symbols is not None else self.symbols_in_scope()
        results: Dict[str, str] = {}
        errors: Dict[str, str] = {}
        for s in syms:
            try:
                v = self.evaluate_symbol(s, now, trigger)
                results[s] = f"{v.decision}:{v.state}"
            except Exception as exc:  # noqa: BLE001 - one symbol never stops the others
                errors[s] = type(exc).__name__
        try:
            self.store.touch_control("setup_state_evaluator", now, run=True, result=f"RUN:{len(results)} ok, {len(errors)} errors")
        except Exception as exc:  # noqa: BLE001
            log.warning("setup control status not saved (%s)", type(exc).__name__)
        return {"at": _iso(now), "trigger": trigger, "symbols": syms, "results": results, "errors": errors}

    def startup(self, now: Optional[datetime] = None) -> dict:
        now = now or self.clock()
        eps = self.store.open_episodes()
        try:
            self.store.touch_control("setup_state_evaluator", now, result=f"STARTUP: {len(eps)} open episode(s) restored")
        except Exception as exc:  # noqa: BLE001
            log.warning("setup startup status not saved (%s)", type(exc).__name__)
        return {"restored": [e.model_dump() for e in eps]}
