"""
proposals/engine.py — the pure proposal builder (DESIGN_59 §3–§10). build(inputs) -> Draft. No I/O, no clock, no randomness.
Everything that can block is a named control whose exact inputs are recorded; money is Decimal; quantities are whole shares.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from paper import alpaca_order_rules as RU            # pure constants of the frozen Stage 4.6B rules module (MAX_QTY, MAX_NOTIONAL, BUY_BUFFER)
from ppe.features import atr_last
from ppe.inputs import DailyBar
from proposals import (ACCOUNT_SNAPSHOT_WARN_AGE_SECONDS, ATR_PERIOD, B_ATR_NA, B_BUDGET_ZERO, B_BUYING_POWER, B_CAP_EXCEEDED, B_DAILY_LOSS, B_DEMAND_NA,
                       B_DEMAND_STALE, B_DUPLICATE, B_EQUITY_NA, B_EVIDENCE_BLOCKED, B_EVIDENCE_CONTRA, B_FRACTION, B_INVALID_STOP, B_KILL, B_NOT_TRADABLE,
                       B_PPE_NA, B_PPE_STALE, B_PRICE_INVALID, B_PRICE_NA, B_PRICE_STALE, B_QTY_ZERO, B_RPS_INVALID, B_SETUP_STALE, CONFIDENCE_MODERATE_BELOW,
                       DAILY_REFERENCE_MAX_TTL_SECONDS, DEMAND_MARGINAL_BELOW, FALLBACK_STOP_ATR, INTRADAY_MIN_BARS, MINUTE_BAR_MAX_AGE_SECONDS, NE_INVALIDATED,
                       NE_NOT_FOUND, NE_NOT_READY, NE_TYPE, NEAR_CLOSE_SECONDS, PRICE_MAX_AGE_SECONDS, RISK_FRACTION_MAX, SETUP_TYPE, STRUCTURAL_BUFFER_ATR,
                       STRUCTURAL_LOOKBACK_SESSIONS, TEXT, W_BP_REDUCED, W_BP_UNKNOWN, W_CAP_REDUCED, W_CLOSED, W_CONFIDENCE, W_DAILY_REF, W_DEMAND,
                       W_EVIDENCE_WEAK, W_EXISTING, W_HARD_REDUCED, W_HYPOTHETICAL, W_NEAR_CLOSE, W_NO_NEW_ENTRIES, W_SECTOR_FALLBACK, W_SIMULATED, W_SNAPSHOT_STALE,
                       W_TRADABILITY)
from proposals.models import ControlEvaluation, Draft, ProposalInputs
from setups.engine import evidence_contradiction, evidence_gate
from setups.models import EpisodeRecord, SetupInputs

MONEY, PRICE4 = Decimal("0.01"), Decimal("0.0001")
SIMULATED_MODES = ("simulated", "test", "replay")


# ---------------------------------------------------------------- deterministic arithmetic -----------------------------------------

def D(x: Any) -> Optional[Decimal]:
    """A finite Decimal from a number or canonical string; None for anything else (never NaN, never infinity)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        if isinstance(x, float):
            if not math.isfinite(x):
                return None
            d = Decimal(repr(x))
        else:
            d = Decimal(str(x).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(d: Decimal) -> Decimal:
    return d.quantize(MONEY, rounding=ROUND_HALF_UP)


def p4(d: Decimal) -> Decimal:
    return d.quantize(PRICE4, rounding=ROUND_HALF_UP)


def floor_div(a: Decimal, b: Decimal) -> int:
    return max(0, int((a / b).to_integral_value(rounding=ROUND_FLOOR)))


def s(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else format(d, "f")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _words(code: str) -> str:
    return code.replace("_", " ").lower()


# ---------------------------------------------------------------- control bookkeeping -------------------------------------------------

class _Controls:
    def __init__(self) -> None:
        self.rows: List[ControlEvaluation] = []
        self.blockers: List[Dict[str, str]] = []
        self.warnings: List[Dict[str, str]] = []

    def add(self, control_id: str, result: str, reason: str = "", **inp: Any) -> None:
        self.rows.append(ControlEvaluation(control_id=control_id, result=result, reason=reason, input=dict(inp)))

    def block(self, control_id: str, code: str, reason: Optional[str] = None, **inp: Any) -> None:
        head = code.split(":")[0]
        text = TEXT.get(head, head)
        if ":" in code:
            text = f"{text} ({_words(code.split(':', 1)[1])})"
        self.blockers.append({"code": code, "text": text})
        self.add(control_id, "BLOCK", reason or code, **inp)

    def warn(self, code: str) -> None:
        if all(w["code"] != code for w in self.warnings):
            self.warnings.append({"code": code, "text": TEXT.get(code, code)})

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


# ---------------------------------------------------------------- eligibility (NOT_ELIGIBLE is computed, never stored) ----------

def eligibility(inp: ProposalInputs) -> List[str]:
    v = inp.setup_view or {}
    if not inp.episode or not v.get("setup_id"):
        return [NE_NOT_FOUND]
    if v.get("setup_type", SETUP_TYPE) != SETUP_TYPE:
        return [NE_TYPE]
    if v.get("state") == "INVALIDATED":
        return [NE_INVALIDATED]
    if v.get("state") != "READY":
        return [NE_NOT_READY]
    return []


# ---------------------------------------------------------------- the builder ------------------------------------------------------------

def build(inp: ProposalInputs) -> Draft:
    v = inp.setup_view or {}
    base: Dict[str, Any] = dict(symbol=inp.symbol.upper(), setup_id=v.get("setup_id"), setup_type=v.get("setup_type") or SETUP_TYPE, mode=inp.mode,
                                source=inp.market_data_source, setup_snapshot=dict(v), ppe_snapshot=dict(inp.ppe_snapshot), demand_snapshot=dict(inp.demand_snapshot),
                                evidence_snapshot=dict(inp.evidence_snapshot))
    ne = eligibility(inp)
    if ne:
        return Draft(status="NOT_ELIGIBLE", blockers=[{"code": c, "text": TEXT[c]} for c in ne], inputs={"now": inp.now, "setup_state": v.get("state"), "mode": inp.mode}, **base)

    now = _parse(inp.now) or datetime.fromtimestamp(inp.now_ts, tz=timezone.utc)
    simulated = inp.mode in SIMULATED_MODES
    c = _Controls()
    si = SetupInputs(**v["inputs"]) if isinstance(v.get("inputs"), dict) and v.get("inputs") else None
    ep = EpisodeRecord(**inp.episode) if inp.episode else None

    # 1. setup freshness
    last = _parse(v.get("last_evaluated_at"))
    age = (now - last).total_seconds() if last else None
    if simulated or (age is not None and age <= float(inp.setup_fresh_window_s)):
        c.add("SETUP_FRESHNESS", "PASS", "fresh", last_evaluated_at=v.get("last_evaluated_at"), age_s=age, window_s=inp.setup_fresh_window_s, mode=inp.mode)
    else:
        c.block("SETUP_FRESHNESS", B_SETUP_STALE, last_evaluated_at=v.get("last_evaluated_at"), age_s=age, window_s=inp.setup_fresh_window_s)

    # 2. PPE / 3. Demand freshness (from the stored setup inputs — the same values the setup was decided on)
    if si is None:
        c.block("PPE_FRESHNESS", B_PPE_NA, "no stored setup inputs")
        c.block("DEMAND_FRESHNESS", B_DEMAND_NA, "no stored setup inputs")
    else:
        ppe_ok = si.ppe_status in ("AVAILABLE", "SIMULATED_DATA") and si.stretch is not None
        if not ppe_ok:
            c.block("PPE_FRESHNESS", B_PPE_NA, ppe_status=si.ppe_status, stretch=si.stretch)
        elif not (si.ppe_fresh or simulated):
            c.block("PPE_FRESHNESS", B_PPE_STALE, ppe_computed_at=si.ppe_computed_at, stale_reasons=list(si.stale_reasons))
        else:
            c.add("PPE_FRESHNESS", "PASS", "fresh", ppe_status=si.ppe_status, ppe_computed_at=si.ppe_computed_at, stretch=si.stretch, confidence=si.confidence)
        dem_ok = si.demand_status in ("AVAILABLE", "SIMULATED_DATA") and si.demand is not None
        if not dem_ok:
            c.block("DEMAND_FRESHNESS", B_DEMAND_NA, demand_status=si.demand_status, demand=si.demand)
        elif not (si.demand_fresh or simulated):
            c.block("DEMAND_FRESHNESS", B_DEMAND_STALE, demand_computed_at=si.demand_computed_at, stale_reasons=list(si.stale_reasons))
        else:
            c.add("DEMAND_FRESHNESS", "PASS", "fresh", demand_status=si.demand_status, demand_computed_at=si.demand_computed_at, demand=si.demand, coverage=si.demand_coverage)

    # 4. evidence gate / contradiction (the Stage 5.8 rules, re-applied to the stored inputs)
    if si is None:
        c.add("EVIDENCE_GATE", "UNKNOWN", "not evaluated: no stored setup inputs")
    else:
        gate = evidence_gate(si)
        contra = evidence_contradiction(ep, si) if ep else False
        einp = dict(gate=gate, contradiction=contra, cause=si.evidence_cause, strength=si.evidence_strength, packet_id=si.evidence_packet_id, age_days=si.evidence_age_days)
        if contra:
            c.block("EVIDENCE_GATE", B_EVIDENCE_CONTRA, **einp)
        elif gate:
            c.block("EVIDENCE_GATE", f"{B_EVIDENCE_BLOCKED}:{gate}", gate, **einp)
        else:
            c.add("EVIDENCE_GATE", "PASS", "no gate", **einp)
        if si.evidence_strength == "WEAK":
            c.warn(W_EVIDENCE_WEAK)
        if si.confidence is not None and si.confidence < CONFIDENCE_MODERATE_BELOW:
            c.warn(W_CONFIDENCE)
        if si.demand is not None and si.demand < DEMAND_MARGINAL_BELOW:
            c.warn(W_DEMAND)

    # 5. reference price (DESIGN_59 §4 hierarchy; the first usable candidate wins; invalid values never fall through)
    reference: Optional[Decimal] = None
    chosen = None
    tried: List[Dict[str, Any]] = []
    invalid = False
    stale_seen = False
    for cand in inp.price_candidates:
        if cand.status != "OK" or cand.value is None:
            tried.append({"source": cand.source, "outcome": "unavailable"})
            continue
        if not (isinstance(cand.value, (int, float)) and math.isfinite(float(cand.value)) and float(cand.value) > 0):
            tried.append({"source": cand.source, "outcome": "invalid", "value": str(cand.value)})
            invalid = True
            break
        limit = MINUTE_BAR_MAX_AGE_SECONDS if cand.source == "MINUTE_CLOSE" else PRICE_MAX_AGE_SECONDS
        if cand.mode == "INTRADAY" and not simulated and cand.age_s is not None and cand.age_s > limit:
            tried.append({"source": cand.source, "outcome": "stale", "age_s": cand.age_s})
            stale_seen = True
            continue
        chosen = cand
        tried.append({"source": cand.source, "outcome": "chosen", "value": cand.value, "age_s": cand.age_s})
        break
    price_mode = chosen.mode if chosen else None
    if chosen is not None:
        reference = p4(D(chosen.value))
        c.add("PRICE_REFERENCE", "WARN" if price_mode == "DAILY_REFERENCE" else "PASS", chosen.source, candidates=tried, reference_price=s(reference), mode=price_mode)
        if price_mode == "DAILY_REFERENCE":
            c.warn(W_DAILY_REF)
    elif invalid:
        c.block("PRICE_REFERENCE", B_PRICE_INVALID, candidates=tried)
    elif stale_seen:
        c.block("PRICE_REFERENCE", B_PRICE_STALE, candidates=tried)
    else:
        c.block("PRICE_REFERENCE", B_PRICE_NA, candidates=tried)

    # 6. invalidation level (DESIGN_59 §5) — ATR(14) on completed sessions, structural low of the last 5 sessions (+ the session low)
    bars = [DailyBar(date=b.get("date"), open=float(b["open"]), high=float(b["high"]), low=float(b["low"]), close=float(b["close"]))
            for b in inp.daily_bars if all(k in b for k in ("open", "high", "low", "close"))]
    atr_f = atr_last(bars, ATR_PERIOD) if len(bars) >= ATR_PERIOD + 1 else None
    atr = D(atr_f) if atr_f is not None and atr_f > 0 else None
    recent = [b.low for b in bars[-STRUCTURAL_LOOKBACK_SESSIONS:]] if len(bars) >= STRUCTURAL_LOOKBACK_SESSIONS else []
    session_low = min(inp.intraday_lows) if len(inp.intraday_lows) >= INTRADAY_MIN_BARS else None
    lows = [x for x in recent if x and x > 0] + ([session_low] if session_low and session_low > 0 else [])
    structural_low = D(min(lows)) if len(recent) == STRUCTURAL_LOOKBACK_SESSIONS and lows else None
    invalidation: Optional[Decimal] = None
    stop_rule: Optional[str] = None
    structural_stop = atr_stop = None
    if reference is None:
        c.add("INVALIDATION_LEVEL", "UNKNOWN", "not evaluated: no reference price", atr=s(atr), structural_low=s(structural_low), bars=len(bars))
    elif atr is None:
        c.block("INVALIDATION_LEVEL", B_ATR_NA, bars=len(bars), needed=ATR_PERIOD + 1, structural_low=s(structural_low))
    else:
        structural_stop = money(structural_low - Decimal(STRUCTURAL_BUFFER_ATR) * atr) if structural_low is not None and structural_low < reference else None
        atr_stop = money(reference - Decimal(FALLBACK_STOP_ATR) * atr)
        if structural_stop is not None:
            if structural_stop >= atr_stop:
                invalidation, stop_rule = structural_stop, "STRUCTURAL"
            else:
                invalidation, stop_rule = atr_stop, "ATR_CAP"
        else:
            invalidation, stop_rule = atr_stop, "ATR_FALLBACK"
        linp = dict(reference_price=s(reference), atr=s(atr), atr_period=ATR_PERIOD, structural_low=s(structural_low), session_low=session_low, structural_stop=s(structural_stop),
                    atr_stop=s(atr_stop), rule=stop_rule, bars=len(bars), intraday_bars=len(inp.intraday_lows))
        if invalidation >= reference:
            c.block("INVALIDATION_LEVEL", B_INVALID_STOP, invalidation_price=s(invalidation), **linp)
            invalidation = None
        else:
            c.add("INVALIDATION_LEVEL", "PASS", stop_rule, invalidation_price=s(invalidation), **linp)

    # 7. risk per share
    rps: Optional[Decimal] = None
    risk_pct: Optional[Decimal] = None
    if reference is None or invalidation is None:
        c.add("RISK_PER_SHARE", "UNKNOWN", "not evaluated: no reference price or invalidation level")
    else:
        rps = p4(reference - invalidation)
        if rps <= 0:
            c.block("RISK_PER_SHARE", B_RPS_INVALID, reference_price=s(reference), invalidation_price=s(invalidation), risk_per_share=s(rps))
            rps = None
        else:
            risk_pct = money(rps / reference * 100)
            c.add("RISK_PER_SHARE", "PASS", "positive", reference_price=s(reference), invalidation_price=s(invalidation), risk_per_share=s(rps), risk_pct=s(risk_pct))

    # 8. account equity
    acct = inp.account
    equity = D(acct.equity) if acct.equity_source != "NOT_AVAILABLE" else None
    equity = equity if equity is not None and equity > 0 else None
    ainp = dict(equity=acct.equity, source=acct.equity_source, as_of=acct.equity_as_of, age_s=acct.equity_age_s, notes=list(acct.notes))
    if equity is None:
        c.block("ACCOUNT_EQUITY", B_EQUITY_NA, **ainp)
    else:
        c.add("ACCOUNT_EQUITY", "PASS", acct.equity_source, **ainp)
        if acct.equity_source == "HYPOTHETICAL_REVIEW":
            c.warn(W_HYPOTHETICAL)
        if acct.equity_source == "ALPACA_PAPER_VIEW" and acct.equity_age_s is not None and acct.equity_age_s > ACCOUNT_SNAPSHOT_WARN_AGE_SECONDS:
            c.warn(W_SNAPSHOT_STALE)

    # 9. risk budget
    fraction = D(inp.risk_fraction)
    budget: Optional[Decimal] = None
    if fraction is None or fraction <= 0 or fraction > Decimal(RISK_FRACTION_MAX):
        c.block("RISK_BUDGET", B_FRACTION, risk_fraction=inp.risk_fraction, max_fraction=RISK_FRACTION_MAX)
        fraction = None
    elif equity is None:
        c.add("RISK_BUDGET", "UNKNOWN", "not evaluated: equity not available", risk_fraction=s(fraction))
    else:
        budget = money(equity * fraction)
        if budget <= 0:
            c.block("RISK_BUDGET", B_BUDGET_ZERO, equity=s(equity), risk_fraction=s(fraction), risk_budget=s(budget))
            budget = None
        else:
            c.add("RISK_BUDGET", "PASS", "equity × fraction", equity=s(equity), risk_fraction=s(fraction), risk_budget=s(budget))

    # 10. halts (observed, never bypassed), 11. tradability, 12. duplicate / pending orders
    if acct.halt:
        c.block("KILL_SWITCH", B_KILL, acct.halt_source or "halt", halt=True, source=acct.halt_source)
    else:
        c.add("KILL_SWITCH", "PASS", "not active", halt=False, source=acct.halt_source)
    if acct.daily_loss_halt:
        c.block("DAILY_LOSS_HALT", B_DAILY_LOSS, acct.daily_loss_source or "halt", halt=True, source=acct.daily_loss_source)
    else:
        c.add("DAILY_LOSS_HALT", "PASS", "not active", halt=False, source=acct.daily_loss_source)
    if acct.tradable is False:
        c.block("TRADABILITY", B_NOT_TRADABLE, tradable=False, source=acct.tradability_source)
    elif acct.tradable is True:
        c.add("TRADABILITY", "PASS", "tradable", tradable=True, source=acct.tradability_source)
    else:
        c.add("TRADABILITY", "WARN", "unknown", tradable=None, source=acct.tradability_source)
        c.warn(W_TRADABILITY)
    if acct.pending_orders > 0:
        c.block("DUPLICATE_ORDER", B_DUPLICATE, "; ".join(acct.pending_detail) or "pending", pending=acct.pending_orders, detail=list(acct.pending_detail), source=acct.pending_source)
    else:
        c.add("DUPLICATE_ORDER", "PASS", "none", pending=0, source=acct.pending_source)

    # 13. buying power (the 4.6B 5 % buffer)
    bp = D(acct.buying_power)
    bp_qty: Optional[int] = None
    if bp is None:
        c.add("BUYING_POWER", "WARN", "unknown", buying_power=None, source=acct.buying_power_source)
        c.warn(W_BP_UNKNOWN)
    elif reference is None:
        c.add("BUYING_POWER", "UNKNOWN", "not evaluated: no reference price", buying_power=s(bp), source=acct.buying_power_source)
    else:
        bp_qty = floor_div(bp, reference * RU.BUY_BUFFER)
        binp = dict(buying_power=s(bp), source=acct.buying_power_source, buffer=s(RU.BUY_BUFFER), reference_price=s(reference), buying_power_qty=bp_qty)
        if bp_qty < 1:
            c.block("BUYING_POWER", B_BUYING_POWER, **binp)
        else:
            c.add("BUYING_POWER", "PASS", "covers at least one share", **binp)

    # 14. position cap (existing rule config.MAX_POSITION_PERCENT, bounded by the 4.6B notional limit) + existing position
    pct = D(inp.max_position_pct) or Decimal(0)
    existing_qty = D(acct.existing_qty) or Decimal(0)
    existing_notional = D(acct.existing_notional)
    if existing_notional is None and existing_qty > 0 and reference is not None:
        existing_notional = money(existing_qty * reference)
    existing_notional = existing_notional if existing_notional is not None else Decimal(0)
    cap_notional: Optional[Decimal] = None
    cap_qty: Optional[int] = None
    cap_source = None
    if existing_qty > 0:
        c.warn(W_EXISTING)
    if equity is None or pct <= 0:
        c.add("POSITION_CAP", "UNKNOWN", "not evaluated: equity not available" if equity is None else "no position cap configured", max_position_pct=s(pct))
    else:
        by_pct = money(equity * pct / Decimal(100))
        cap_notional, cap_source = (by_pct, "config.MAX_POSITION_PERCENT") if by_pct <= RU.MAX_NOTIONAL else (RU.MAX_NOTIONAL, "4.6B MAX_NOTIONAL")
        available = cap_notional - existing_notional
        pinp = dict(equity=s(equity), max_position_pct=s(pct), max_notional=s(cap_notional), cap_source=cap_source, existing_qty=s(existing_qty), existing_notional=s(existing_notional),
                    available_notional=s(available), position_source=acct.position_source)
        if available <= 0:
            cap_qty = 0                                                       # nothing may be added: the quantity control sees a zero cap
            c.block("POSITION_CAP", B_CAP_EXCEEDED, **pinp)
        elif reference is None:
            c.add("POSITION_CAP", "UNKNOWN", "not evaluated: no reference price", **pinp)
        else:
            cap_qty = min(floor_div(available, reference), max(0, RU.MAX_QTY - int(existing_qty)))
            c.add("POSITION_CAP", "PASS", "within cap", position_cap_qty=cap_qty, **pinp)

    # 15. quantity (DESIGN_59 §8): every cap contribution is reported; the smallest wins
    base_qty: Optional[int] = None
    final_qty: Optional[int] = None
    caps: Dict[str, Any] = {}
    proposed_notional: Optional[Decimal] = None
    if budget is None or rps is None or reference is None:
        c.add("QUANTITY", "UNKNOWN", "not evaluated: budget, risk per share or reference price missing", risk_budget=s(budget), risk_per_share=s(rps))
    else:
        base_qty = floor_div(budget, rps)
        caps = {"risk_based": base_qty, "position_cap": cap_qty, "buying_power": bp_qty, "hard_limit": RU.MAX_QTY}
        order = [("risk_based", base_qty), ("position_cap", cap_qty), ("buying_power", bp_qty), ("hard_limit", RU.MAX_QTY)]
        known = [(n, q) for n, q in order if q is not None]
        binding, final_qty = min(known, key=lambda t: (t[1], order.index(t)))
        caps.update(final=final_qty, binding=binding)
        if final_qty < base_qty:
            c.warn({"position_cap": W_CAP_REDUCED, "buying_power": W_BP_REDUCED, "hard_limit": W_HARD_REDUCED}.get(binding, W_HARD_REDUCED))
        if final_qty < 1:
            c.block("QUANTITY", B_QTY_ZERO, f"binding cap: {binding}", **caps)
            final_qty = 0
        else:
            proposed_notional = money(Decimal(final_qty) * reference)
            c.add("QUANTITY", "PASS", f"binding cap: {binding}", proposed_notional=s(proposed_notional), **caps)

    # 16. no-new-entries (Stage 5.9.1): a persisted pause never hides a reviewable proposal; it blocks execution eligibility
    if acct.no_new_entries:
        c.add("NO_NEW_ENTRIES", "WARN", acct.no_new_entries_source or "paused", active=True, source=acct.no_new_entries_source, safety_state=acct.safety_state, reason_codes=list(acct.safety_reason_codes))
        c.warn(W_NO_NEW_ENTRIES)
    else:
        c.add("NO_NEW_ENTRIES", "PASS", "not active", active=False, source=acct.no_new_entries_source, safety_state=acct.safety_state, reason_codes=list(acct.safety_reason_codes))

    # session / data warnings
    sess = inp.session or {}
    if sess.get("regular_session") is False:
        c.warn(W_CLOSED)
    elif sess.get("seconds_to_close") is not None and float(sess["seconds_to_close"]) <= NEAR_CLOSE_SECONDS:
        c.warn(W_NEAR_CLOSE)
    if inp.sector_fallback:
        c.warn(W_SECTOR_FALLBACK)
    if simulated:
        c.warn(W_SIMULATED)

    # expiry (DESIGN_59 §12)
    if price_mode == "DAILY_REFERENCE":
        nxt = _parse(sess.get("next_open"))
        cap_dt = now + timedelta(seconds=DAILY_REFERENCE_MAX_TTL_SECONDS)
        expires = min(nxt, cap_dt) if nxt and nxt > now else cap_dt
    else:
        expires = now + timedelta(seconds=int(inp.ttl_seconds))

    status = "BLOCKED" if c.blocked else "REVIEWABLE"
    evidence_pid = si.evidence_packet_id if si else None
    fp_src = "|".join([str(v.get("setup_id")), status, ",".join(sorted(b["code"] for b in c.blockers)), s(invalidation) or "", str(final_qty), acct.equity_source, s(money(equity)) if equity else "",
                       evidence_pid or "", acct.safety_state or ""])                     # Stage 5.9.1: a change of the risk-controls state is material
    summary = {"now": inp.now, "mode": inp.mode, "setup": {k: v.get(k) for k in ("setup_id", "state", "since", "last_evaluated_at", "cycle_id")},
               "price": {"source": chosen.source if chosen else None, "mode": price_mode, "candidates": tried}, "atr": s(atr), "atr_period": ATR_PERIOD,
               "structural_low": s(structural_low), "structural_stop": s(structural_stop), "atr_stop": s(atr_stop), "daily_bars": len(bars), "intraday_bars": len(inp.intraday_lows),
               "account": acct.model_dump(), "session": dict(sess), "risk_fraction": inp.risk_fraction, "max_position_pct": inp.max_position_pct, "ttl_seconds": inp.ttl_seconds,
               "caps": dict(caps)}
    return Draft(status=status, blockers=list(c.blockers), warnings=list(c.warnings), controls=list(c.rows), price_mode=price_mode, reference_price=s(reference),
                 price_source=chosen.source if chosen else None, price_timestamp=_iso(datetime.fromtimestamp(chosen.ts, tz=timezone.utc)) if chosen and chosen.ts else None,
                 invalidation_price=s(invalidation), stop_rule=stop_rule, structural_low=s(structural_low), atr=s(atr), atr_period=ATR_PERIOD if atr is not None else 0,
                 risk_per_share=s(rps), risk_pct=s(risk_pct), equity=s(money(equity)) if equity is not None else None, equity_source=acct.equity_source, risk_fraction=s(fraction),
                 risk_budget=s(budget), base_qty=base_qty, position_cap_qty=cap_qty, buying_power_qty=bp_qty, final_qty=final_qty, max_notional=s(cap_notional),
                 max_position_pct=s(pct) if pct > 0 else None, cap_source=cap_source, existing_position_qty=s(existing_qty) if existing_qty > 0 else None,
                 existing_position_notional=s(existing_notional) if existing_qty > 0 else None, existing_avg_cost=acct.existing_avg_cost if existing_qty > 0 else None,
                 proposed_notional=s(proposed_notional), caps=caps, expires_at=_iso(expires), inputs=summary, fingerprint=hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:32], **base)


def proposal_id_for(setup_id: str, created_at: str, reference_price: Optional[str], invalidation_price: Optional[str], risk_budget: Optional[str]) -> str:
    """DESIGN_59 §11: sha256(setup_id | created_at | reference_price | invalidation_price | risk_budget)[:32]."""
    return hashlib.sha256(f"{setup_id}|{created_at}|{reference_price or ''}|{invalidation_price or ''}|{risk_budget or ''}".encode("utf-8")).hexdigest()[:32]


def materially_changed(prev: Optional[dict], draft: Draft, price_change_pct: Decimal, now_iso: str) -> Optional[str]:
    """Why the latest stored row of the same setup cannot be reused (None = reuse it). DESIGN_59 §12."""
    if prev is None:
        return "no stored proposal"
    if prev.get("setup_id") != draft.setup_id:
        return "different episode"
    if prev.get("expires_at") and prev["expires_at"] <= now_iso:
        return "previous proposal expired"
    if prev.get("fingerprint") != draft.fingerprint:
        return "material inputs changed"
    if (prev.get("setup_snapshot") or {}).get("since") != (draft.setup_snapshot or {}).get("since"):
        return "the setup transitioned since the previous proposal"
    a, b = D(prev.get("reference_price")), D(draft.reference_price)
    if a is None or b is None:
        return None if a is None and b is None else "reference price availability changed"
    if a > 0 and abs(b - a) / a * 100 > price_change_pct:
        return f"reference price moved more than {price_change_pct}%"
    return None
