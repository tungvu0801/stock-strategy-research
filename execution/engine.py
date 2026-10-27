"""
execution/engine.py — the PURE rules of the execution layer (DESIGN_60 §4, §7): deterministic ids, the session policy, the entry
preconditions as control evaluations, the exit decision with its precedence, holding-time counting and the beginner mapping.
No I/O, no clock reads, no broker: the service feeds already-fetched facts in and persists what comes out.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from execution import BEGINNER, ENTRY_PREFIX, EXIT_PREFIX, EXIT_REASONS, SETUP_TYPE
from execution import exposure_gate                                    # Stage 6.9.1: the UNIFIED_EXTERNAL_EXPOSURE entry control (pure)
from execution.models import ControlEvaluation, Intent, Position
from setups.market_hours import CLOSE_MINUTE, is_regular_session, session_date, to_et

MONEY = Decimal("0.01")
BUYING_POWER_BUFFER = Decimal("1.05")
WEAKENING_DEMAND = 45
WEAKENING_STRETCH = -30.0


# ---------------------------------------------------------------- deterministic identifiers ----------------------------------------
def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def execution_id_for(proposal_id: str) -> str:
    return _h("entry|" + proposal_id)


def position_id_for(execution_id: str) -> str:
    return _h("position|" + execution_id)


def exit_id_for(position_id: str, cycle: int) -> str:
    return _h(f"exit|{position_id}|{int(cycle)}")


def entry_coid(execution_id: str) -> str:
    return ENTRY_PREFIX + execution_id


def exit_coid(position_id: str, cycle: int) -> str:
    return f"{EXIT_PREFIX}{position_id}-{int(cycle)}"


# ---------------------------------------------------------------- numbers --------------------------------------------------------------
def D(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else str(d.quantize(MONEY))


def pnl(entry_avg, price, qty: int):
    e, p = D(entry_avg), D(price)
    if e is None or p is None or qty <= 0:
        return None, None
    diff = (p - e) * qty
    pct = (p - e) / e * 100 if e > 0 else None
    return money(diff), (str(pct.quantize(MONEY)) if pct is not None else None)


# ---------------------------------------------------------------- session policy --------------------------------------------------------
def minutes_to_close(now: datetime) -> Optional[float]:
    et = to_et(now)
    if et is None:
        return None
    return CLOSE_MINUTE - (et.hour * 60 + et.minute + et.second / 60.0)


def session_policy(now: datetime, broker_clock: Optional[dict], near_close_minutes: int) -> ControlEvaluation:
    """Market orders only in the regular session (weekdays 09:30–16:00 ET, no holiday calendar), never in the final minutes, and only
    when the broker's own clock (when readable) agrees that the market is open."""
    regular = is_regular_session(now)
    mtc = minutes_to_close(now)
    inp = {"regular_session": regular, "minutes_to_close": round(mtc, 1) if mtc is not None else None, "near_close_minutes": near_close_minutes,
           "broker_clock_open": broker_clock.get("is_open") if broker_clock else None}
    if not regular:
        return ControlEvaluation(control_id="SESSION_POLICY", result="BLOCK", reason="outside the US regular session", input=inp)
    if mtc is not None and mtc <= near_close_minutes:
        return ControlEvaluation(control_id="SESSION_POLICY", result="BLOCK", reason=f"within the final {near_close_minutes} minutes of the session", input=inp)
    if broker_clock is not None and not broker_clock.get("is_open"):
        return ControlEvaluation(control_id="SESSION_POLICY", result="BLOCK", reason="the broker clock says the market is closed (holiday or early close)", input=inp)
    return ControlEvaluation(control_id="SESSION_POLICY", result="PASS", reason="regular session", input=inp)


def sessions_held(opened_at_iso: str, now: datetime) -> int:
    """Weekday sessions strictly after the entry's session date up to and including today's session date (the entry day counts 0)."""
    try:
        opened = datetime.fromisoformat(opened_at_iso)
    except (TypeError, ValueError):
        return 0
    d0, d1 = session_date(opened), session_date(now)
    if d0 is None or d1 is None or d1 <= d0:
        return 0
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ---------------------------------------------------------------- entry preconditions -----------------------------------------------------
@dataclass
class EntryContext:
    now: datetime
    mode: str
    environment: Dict[str, Any]
    safety: Any                                           # SafetyView or None
    proposal: Any                                         # ProposalView
    setup: Any                                            # SetupView
    account: Optional[dict] = None
    account_error: Optional[str] = None
    asset: Optional[dict] = None
    asset_error: Optional[str] = None
    broker_position: Optional[dict] = None
    broker_open_orders: List[dict] = field(default_factory=list)
    internal_open_intents: List[Intent] = field(default_factory=list)
    prior_entries_for_setup: int = 0
    internal_open_position: Optional[Position] = None
    legacy_intents: List[str] = field(default_factory=list)       # unresolved Stage 4.6B manual intents in the symbol
    broker_positions_count: int = 0
    pending_entries_count: int = 0
    max_open_positions: int = 10
    reconciliation_healthy: bool = True
    reconciliation_reasons: List[str] = field(default_factory=list)
    broker_clock: Optional[dict] = None
    near_close_minutes: int = 15
    qty_cap: int = 0
    exposure_gate: Any = None                             # Stage 6.9.1: exposure_gate.GateSettings; None or not enabled = the entry_controls_v1 set
    unified_portfolio: Optional[dict] = None              # Stage 6.9.1: the UnifiedPortfolioState read once for this scan (None = not read)


def entry_quantity(proposal_qty: Optional[int], qty_cap: int) -> int:
    q = int(proposal_qty or 0)
    return min(q, int(qty_cap)) if qty_cap and qty_cap > 0 else q


def entry_controls(c: EntryContext) -> List[ControlEvaluation]:
    """Every precondition as a control row. The order is the evaluation order; ALL must PASS before an intent is created."""
    out: List[ControlEvaluation] = []
    p, s = c.proposal, c.setup
    sym = p.symbol
    out.append(ControlEvaluation(control_id="EXECUTION_MODE", result="PASS" if c.mode == "PAPER_AUTO" else "BLOCK", reason=f"mode {c.mode}", input={"mode": c.mode}))
    env = c.environment or {}
    out.append(ControlEvaluation(control_id="PAPER_ENVIRONMENT", result="PASS" if env.get("paper") else "BLOCK",
                                 reason="paper environment confirmed" if env.get("paper") else f"paper environment not confirmed ({env.get('error') or 'no proof'})",
                                 input={"host": env.get("host"), "label": env.get("label"), "simulated": env.get("simulated"), "error": env.get("error")}))
    sv = c.safety
    if sv is None:
        out.append(ControlEvaluation(control_id="SAFETY_GATE", result="BLOCK", reason="safety gate not evaluated", input={}))
    else:
        ok = bool(sv.allowed_new_entry and sv.state == "TRADING_ALLOWED")
        out.append(ControlEvaluation(control_id="SAFETY_GATE", result="PASS" if ok else "BLOCK", reason=f"{sv.state}" + (f": {sv.reason_text}" if sv.reason_text else ""),
                                     input={"state": sv.state, "allowed_new_entry": sv.allowed_new_entry, "reason_codes": list(sv.reason_codes)}))
    # proposal validity
    problems = []
    if p.effective_status != "REVIEWABLE":
        problems.append(f"proposal {p.effective_status}")
    if not (p.execution_eligibility or {}).get("allowed"):
        problems.append("proposal not eligible by the risk-controls gate")
    if s.state != "READY" or not s.setup_id or s.setup_id != p.setup_id:
        problems.append(f"setup {s.state}" if s.state != "READY" else "proposal belongs to another setup episode")
    if p.setup_type != SETUP_TYPE or s.setup_type != SETUP_TYPE:
        problems.append("not the Downside Dislocation Recovery setup")
    if p.side_context != "LONG_REVIEW":
        problems.append("only LONG proposals are executed")
    qty = entry_quantity(p.final_qty, c.qty_cap)
    if qty < 1:
        problems.append("quantity below one share")
    ref, inv = D(p.reference_price), D(p.invalidation_price)
    if ref is None or inv is None or inv >= ref:
        problems.append("reference / invalidation prices missing or inconsistent")
    now_iso = c.now.isoformat(timespec="seconds")
    if p.expires_at and now_iso >= p.expires_at:
        problems.append("proposal expired")
    if p.blockers:
        problems.append("proposal has blockers")
    out.append(ControlEvaluation(control_id="PROPOSAL_VALIDITY", result="BLOCK" if problems else "PASS", reason="; ".join(problems) if problems else "reviewable, eligible, READY, priced",
                                 input={"proposal_id": p.proposal_id, "effective_status": p.effective_status, "final_qty": p.final_qty, "qty": qty, "qty_cap": c.qty_cap,
                                        "reference_price": p.reference_price, "invalidation_price": p.invalidation_price, "expires_at": p.expires_at, "setup_id": s.setup_id}))
    out.append(session_policy(c.now, c.broker_clock, c.near_close_minutes))
    # broker tradability
    if c.asset_error:
        out.append(ControlEvaluation(control_id="BROKER_TRADABILITY", result="BLOCK", reason=f"asset not readable ({c.asset_error})", input={"symbol": sym}))
    elif c.asset is None:
        out.append(ControlEvaluation(control_id="BROKER_TRADABILITY", result="BLOCK", reason="asset unknown to the broker", input={"symbol": sym}))
    else:
        ok = bool(c.asset.get("tradable")) and str(c.asset.get("status") or "").lower() == "active" and str(c.asset.get("asset_class") or "us_equity") == "us_equity"
        out.append(ControlEvaluation(control_id="BROKER_TRADABILITY", result="PASS" if ok else "BLOCK", reason="tradable active US equity" if ok else "asset not tradable",
                                     input={"symbol": sym, "tradable": c.asset.get("tradable"), "status": c.asset.get("status"), "asset_class": c.asset.get("asset_class")}))
    # duplicates: internal intents, prior entry of this episode, internal position, broker open orders, legacy 4.6B intents
    dups = []
    if c.internal_open_intents:
        dups.append(f"{len(c.internal_open_intents)} open internal intent(s)")
    if c.prior_entries_for_setup:
        dups.append("this setup episode already had an entry")
    if c.internal_open_position is not None:
        dups.append("an internal paper position is open")
    if c.broker_open_orders:
        dups.append(f"{len(c.broker_open_orders)} open broker order(s) in {sym}")
    if c.legacy_intents:
        dups.append(f"{len(c.legacy_intents)} unresolved manual paper intent(s)")
    out.append(ControlEvaluation(control_id="DUPLICATE_ENTRY", result="BLOCK" if dups else "PASS", reason="; ".join(dups) if dups else "no duplicate intent, order or position",
                                 input={"symbol": sym, "open_intents": [i.execution_id for i in c.internal_open_intents], "broker_open_orders": [o.get("id") for o in c.broker_open_orders],
                                        "legacy": list(c.legacy_intents), "prior_entries_for_setup": c.prior_entries_for_setup}))
    out.append(ControlEvaluation(control_id="BROKER_POSITION", result="BLOCK" if c.broker_position else "PASS",
                                 reason=f"broker already holds {c.broker_position['qty']} {sym}" if c.broker_position else "no broker position in the symbol",
                                 input={"symbol": sym, "broker_qty": c.broker_position["qty"] if c.broker_position else 0}))
    # account
    if c.account_error or c.account is None:
        out.append(ControlEvaluation(control_id="BROKER_ACCOUNT", result="BLOCK", reason=f"account not readable ({c.account_error or 'no data'})", input={}))
    else:
        a = c.account
        bp, need = D(a.get("buying_power")), (ref * qty * BUYING_POWER_BUFFER) if (ref is not None and qty > 0) else None
        reasons = []
        if str(a.get("status") or "").upper() != "ACTIVE":
            reasons.append(f"account status {a.get('status')}")
        if a.get("blocked"):
            reasons.append("account or trading blocked")
        if bp is None or need is None:
            reasons.append("buying power unreadable")
        elif bp < need:
            reasons.append(f"buying power {money(bp)} below {money(need)} (price × qty × 1.05)")
        out.append(ControlEvaluation(control_id="BROKER_ACCOUNT", result="BLOCK" if reasons else "PASS", reason="; ".join(reasons) if reasons else "account active with buying power",
                                     input={"status": a.get("status"), "blocked": a.get("blocked"), "buying_power": a.get("buying_power"), "required": money(need) if need is not None else None,
                                            "account": a.get("account_number_masked")}))
    total = c.broker_positions_count + c.pending_entries_count
    out.append(ControlEvaluation(control_id="MAX_OPEN_POSITIONS", result="PASS" if total < c.max_open_positions else "BLOCK",
                                 reason=f"{c.broker_positions_count} broker position(s) + {c.pending_entries_count} pending entr(y/ies) vs cap {c.max_open_positions}",
                                 input={"broker_positions": c.broker_positions_count, "pending_entries": c.pending_entries_count, "cap": c.max_open_positions}))
    out.append(ControlEvaluation(control_id="BROKER_RECONCILIATION", result="PASS" if c.reconciliation_healthy else "BLOCK",
                                 reason="reconciliation healthy" if c.reconciliation_healthy else "; ".join(c.reconciliation_reasons) or "reconciliation required",
                                 input={"healthy": c.reconciliation_healthy, "reasons": list(c.reconciliation_reasons)}))
    # Stage 6.9.1 (entry_controls_v2): UNIFIED_EXTERNAL_EXPOSURE, only when UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED=true. The planned notional is the
    # quantity and reference price above; the other accounts are a risk input only. PASS / WARN allow, BLOCK blocks (blocking() below) - never a resize.
    if c.exposure_gate is not None and c.exposure_gate.enabled:
        out.append(exposure_gate.evaluate(sym, qty, ref, c.unified_portfolio, c.exposure_gate))
    return out


def blocking(controls: List[ControlEvaluation]) -> List[ControlEvaluation]:
    return [c for c in controls if c.result == "BLOCK"]


# ---------------------------------------------------------------- exits ------------------------------------------------------------------
@dataclass
class ExitContext:
    now: datetime
    price: Optional[Decimal]
    price_source: str = ""
    price_at: Optional[str] = None
    setup: Any = None                                       # SetupView for the symbol (may belong to a later episode)
    episode: Optional[dict] = None                          # the setup_episodes row of the position's own episode
    episode_record: Any = None                              # EpisodeRecord (open episode) when still open
    inputs: Any = None                                      # SetupInputs of the latest evaluation
    max_hold_sessions: int = 5
    demand_exit: int = 35
    demand_exit_evals: int = 2
    stretch_exit: float = -20.0


@dataclass
class ExitDecision:
    reason: Optional[str]
    candidates: List[str]
    demand_low_streak: int
    last_demand_eval_at: Optional[str]
    sessions_held: int
    weakening: Optional[str]
    detail: Dict[str, Any]


def _demand_available(i) -> bool:
    return i is not None and i.demand is not None and i.demand_status in ("AVAILABLE", "SIMULATED_DATA")


def _stretch_available(i) -> bool:
    return i is not None and i.stretch is not None and i.stretch_status == "AVAILABLE"


def exit_decision(pos: Position, c: ExitContext) -> ExitDecision:
    """The deterministic exit reasons in precedence order (HARD_INVALIDATION > EVIDENCE_INVALIDATION > SETUP_INVALIDATED > DEMAND_FAILURE > TIME_EXIT >
    DISLOCATION_NORMALIZED). Nothing here aims at a gain and nothing forecasts a price."""
    cands: List[str] = []
    detail: Dict[str, Any] = {"price": money(c.price) if c.price is not None else None, "price_source": c.price_source, "invalidation_price": pos.invalidation_price}
    inv = D(pos.invalidation_price)
    if c.price is not None and inv is not None and c.price <= inv:
        cands.append("HARD_INVALIDATION")
    ep = c.episode or {}
    ep_state, ep_closed, inv_reason = ep.get("state"), bool(ep.get("closed_at")), ep.get("invalidation_reason")
    detail.update({"episode_state": ep_state, "episode_closed": ep_closed, "invalidation_reason": inv_reason})
    if c.episode is None or ep_closed or ep_state in ("INVALIDATED", "NONE"):
        if inv_reason == "EVIDENCE_CONTRADICTION":
            cands.append("EVIDENCE_INVALIDATION")
        elif inv_reason == "DEMAND_COLLAPSED":
            cands.append("DEMAND_FAILURE")
        elif inv_reason == "STRETCH_NORMALIZED":
            cands.append("DISLOCATION_NORMALIZED")
        else:
            cands.append("SETUP_INVALIDATED")
    elif c.episode_record is not None and c.inputs is not None:
        try:
            from setups.engine import evidence_contradiction
            if evidence_contradiction(c.episode_record, c.inputs):
                cands.append("EVIDENCE_INVALIDATION")
        except Exception:  # noqa: BLE001 - a malformed input never decides an exit
            pass
    streak, eval_at = pos.demand_low_streak, pos.last_demand_eval_at
    i = c.inputs
    if i is not None and i.evaluated_at and i.evaluated_at != eval_at and i.evaluated_at >= pos.opened_at:
        eval_at = i.evaluated_at
        if _demand_available(i):
            streak = streak + 1 if i.demand < c.demand_exit else 0
        if _stretch_available(i) and float(i.stretch) > c.stretch_exit:
            cands.append("DISLOCATION_NORMALIZED")
    detail.update({"demand": getattr(i, "demand", None), "stretch": getattr(i, "stretch", None), "demand_low_streak": streak, "evaluated_at": eval_at})
    if streak >= c.demand_exit_evals:
        cands.append("DEMAND_FAILURE")
    held = sessions_held(pos.opened_at, c.now)
    detail["sessions_held"] = held
    if held >= c.max_hold_sessions:
        cands.append("TIME_EXIT")
    reason = next((r for r in EXIT_REASONS if r in cands), None)
    weakening = None
    if reason is None and i is not None:
        if _demand_available(i) and i.demand < WEAKENING_DEMAND:
            weakening = "demand weakening"
        elif _stretch_available(i) and float(i.stretch) > WEAKENING_STRETCH:
            weakening = "dislocation fading"
    return ExitDecision(reason=reason, candidates=[r for r in EXIT_REASONS if r in cands], demand_low_streak=streak, last_demand_eval_at=eval_at, sessions_held=held,
                        weakening=weakening, detail=detail)


# ---------------------------------------------------------------- beginner mapping -----------------------------------------------------------
def card_key(*, position: Optional[Position], open_intent: Optional[Intent], last_intent: Optional[Intent], last_closed: Optional[Position], safety_state: Optional[str],
             setup_state: str, proposal_status: Optional[str], today: Optional[str]) -> str:
    if position is not None:
        if position.state == "ATTENTION":
            return "ATTENTION"
        if position.state in ("EXIT_REQUIRED", "EXIT_SUBMITTED") or (open_intent is not None and open_intent.intent_type == "EXIT"):
            return "EXITING"
        return "IN_TRADE"
    if open_intent is not None:
        return "ATTENTION" if open_intent.state == "RECONCILIATION_REQUIRED" else "ENTERING"
    if last_intent is not None and last_intent.state in ("FAILED", "NOT_SENT", "REJECTED", "CANCELED") and last_intent.filled_qty == 0 and today and (last_intent.updated_at or "")[:10] == today:
        return "ATTENTION"
    if last_closed is not None and today and (last_closed.closed_at or "")[:10] == today:
        return "CLOSED"
    if safety_state in ("HALTED", "UNKNOWN"):
        return "STOPPED"
    if setup_state == "READY":
        return "READY"
    if setup_state == "WATCH":
        return "WATCHING"
    return "NONE"


def internal_state(*, position: Optional[Position], open_intent: Optional[Intent], last_intent: Optional[Intent], last_closed: Optional[Position]) -> str:
    if open_intent is not None and open_intent.intent_type == "ENTRY":
        return {"PENDING": "ENTRY_PENDING", "SUBMITTED": "ENTRY_SUBMITTED", "PARTIALLY_FILLED": "ENTRY_PARTIAL", "RECONCILIATION_REQUIRED": "RECONCILIATION_REQUIRED"}.get(open_intent.state, "ENTRY_PENDING")
    if position is not None:
        if open_intent is not None:
            return {"PENDING": "EXIT_PENDING", "SUBMITTED": "EXIT_SUBMITTED", "PARTIALLY_FILLED": "EXIT_PARTIAL", "RECONCILIATION_REQUIRED": "RECONCILIATION_REQUIRED"}.get(open_intent.state, "EXIT_PENDING")
        if position.state == "ATTENTION":
            return "RECONCILIATION_REQUIRED"
        return "EXIT_REQUIRED" if position.state in ("EXIT_REQUIRED", "EXIT_SUBMITTED") else "POSITION_OPEN"
    if last_intent is not None and last_intent.state in ("FAILED", "NOT_SENT", "REJECTED", "CANCELED") and last_intent.filled_qty == 0 and (last_closed is None or last_intent.updated_at > (last_closed.closed_at or "")):
        return "FAILED"
    if last_closed is not None:
        return "CLOSED"
    return "IDLE"


def beginner(key: str):
    return BEGINNER.get(key, BEGINNER["NONE"])
