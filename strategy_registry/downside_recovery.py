"""
strategy_registry/downside_recovery.py — the detail sections of DOWNSIDE_DISLOCATION_RECOVERY, the one implemented strategy.

Every sentence is traceable: thresholds come from forward_validation.frozen_controls() (the Stage 6.1 frozen contract) and the module constants of
setups / proposals / execution; the wording quotes the design documents named in each section's `sources`. Nothing is estimated. What does not exist
(a backtest, an out-of-sample test, a recorded decision) is a NOT_EVALUATED section with the reason; what the process cannot read right now is
UNAVAILABLE. Paper results are LIVE from the forward paper validation summary the API gathered (GET /api/forward-validation).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from strategy_registry import NOT_EVALUATED
from strategy_registry.models import Section

D56, D57, D58 = "DESIGN_56_PPE_ENGINE.md", "DESIGN_57_MARKET_DEMAND.md", "DESIGN_58_SETUP_STATE_ALERTS.md"
D59, D591, D60, D61 = "DESIGN_59_TRADE_PROPOSAL_REVIEW.md", "DESIGN_591_EXECUTION_SAFETY.md", "DESIGN_60_AUTONOMOUS_PAPER.md", "DESIGN_61_FORWARD_PAPER_VALIDATION.md"
V56, V57 = "scratchpad/s56/PPE_VALIDATION_56.md", "scratchpad/s57/DEMAND_VALIDATION_57.md"
LINKS = {"validation": "forward_validation.html", "trade": "#trade", "activity": "#activity",
         "design_docs": " · ".join((D56, D57, D58, D59, D591, D60, D61))}


def _f(x: Any) -> str:
    """A threshold as people read it: -40.0 -> -40, 0.6 -> 0.6, 0.005 -> 0.005."""
    if isinstance(x, float):
        return f"{x:g}"
    return str(x)


def controls() -> Dict[str, Any]:
    from forward_validation import frozen_controls, frozen_hash
    c = dict(frozen_controls())
    c["hash"] = frozen_hash()
    return c


def sections(facts: Optional[Dict[str, Any]] = None) -> List[Section]:
    import execution as X
    import proposals as P
    import setups as S
    c = controls()
    n_exec_controls = len(X.CONTROLS)
    out: List[Section] = []
    out.append(Section(key="overview", title="Overview", state="DOCUMENTED", items=[
        f"Setup type {c['setup_type']}: the one setup every Stage 5.8 (setups), 5.9 (proposals) and 6.0 (execution) module evaluates; {c['side']} only, {c['order_type']} / "
        f"{c['time_in_force']} paper orders, leverage {c['leverage']}, shorts {c['shorts']}.",
        f"Session rule: {c['session_rule']}.",
        "A signal never executes: setup state -> review proposal -> safety gate -> execution controls -> Alpaca PAPER broker. Paper only; no real money.",
        f"Frozen controls hash {c['hash'][:16]}… (GET /api/forward-validation/frozen-controls): the thresholds below are the frozen Stage 6.1 contract.",
    ], facts={"setup_type": c["setup_type"], "side": c["side"], "order_type": c["order_type"], "time_in_force": c["time_in_force"], "leverage": c["leverage"], "shorts": c["shorts"],
              "session_rule": c["session_rule"], "frozen_controls_hash": c["hash"], "component_versions": {"setups": S.VERSION, "proposals": P.VERSION, "execution": X.VERSION}},
       sources=["setups/__init__.py", "proposals/__init__.py", "execution/__init__.py", "forward_validation/__init__.py (frozen_controls)", D60]))
    out.append(Section(key="hypothesis", title="Hypothesis", state="DOCUMENTED", items=[
        'Design wording: "price has become materially downside-stretched and may be showing buyer confirmation. No upside / short setup in V1."',
        'Three deterministic measures: PPE Stretch ("how far is price displaced?"), PPE Confidence ("how credible is the overreaction / dislocation classification?") and '
        'Market Demand ("are buyers actually stepping in now?").',
        "Confidence starts at 50 and moves only with stored Stage 5.4 evidence (market-wide cause up, company-specific cause and confirming company evidence down); "
        "high Stretch alone cannot produce high Confidence.",
        'Status of the idea: "Weights and thresholds are a research hypothesis; nothing is tuned on future returns." Stage 6.0 "proves operational execution correctness, '
        'not trading edge (Stage 6.1 measures that forward)."',
        "No expected return, recovery probability, win rate or edge is claimed anywhere; the design documents forbid those claims.",
    ], sources=[f"{D58} §1", f"{D57} §1", f"{D56} §1, §6", f"{D60} §1"]))
    out.append(Section(key="rules", title="Rules", state="DOCUMENTED", items=[
        f"States: {', '.join(f'{s} ({S.STATE_LABELS[s]})' for s in S.STATES)}. An episode (setup_id) starts at NONE -> WATCH and ends when the state returns to NONE.",
        f"NONE -> WATCH when PPE Stretch <= {_f(c['watch_enter_stretch'])} (a new episode). WATCH -> NONE when Stretch > {_f(c['watch_exit_stretch'])} (hysteresis; the episode closes, no alert).",
        f"READY needs Stretch <= {_f(c['watch_enter_stretch'])}, PPE coverage >= {_f(c['ready_ppe_coverage'])}, PPE Confidence >= {c['ready_confidence']}, Market Demand >= {c['ready_demand']} "
        f"(coverage >= {_f(c['ready_demand_coverage'])}, fresh) and no evidence gate.",
        f"READY -> WATCH when Demand < {c['ready_exit_demand']} (confirmation weakened) or Demand becomes unavailable.",
        f"READY -> INVALIDATED when Stretch > {_f(c['invalidate_stretch'])} (displacement normalised), Demand < {c['invalidate_demand']} on {c['invalidate_demand_evals']} consecutive evaluations "
        f"(demand collapsed) or new strong company-specific evidence contradicts the setup.",
        f"Evidence gate: READY is blocked without a stored COMPLETED Stage 5.4 packet, with a packet older than {c['evidence_max_age_days']} days, or when the packet confirms "
        f"company-specific deterioration. Evidence never adds points.",
        "Stale PPE or Demand pauses the evaluation (state and counters unchanged; nothing is invalidated).",
        "Thresholds were fixed before any outcome analysis (research hypotheses, not tuned); evaluations run every 60 s in the regular session with a 15-minute alert cooldown.",
    ], facts={k: c[k] for k in ("watch_enter_stretch", "watch_exit_stretch", "ready_confidence", "ready_demand", "ready_demand_coverage", "ready_ppe_coverage", "ready_exit_demand",
                                 "invalidate_demand", "invalidate_demand_evals", "invalidate_stretch", "evidence_max_age_days")} | {"states": list(S.STATES), "state_labels": dict(S.STATE_LABELS),
              "reason_texts": dict(S.REASON_TEXT), "eval_interval_seconds": S.EVAL_INTERVAL_SECONDS, "alert_cooldown_minutes": S.ALERT_COOLDOWN_MINUTES},
       sources=["setups/__init__.py", "setups/engine.py", f"{D58} §2-§4"]))
    out.append(Section(key="data_requirements", title="Data requirements", state="DOCUMENTED", items=[
        "PPE: completed daily OHLCV bars (Alpaca adjusted IEX bars in live mode; synthetic bars labelled SIMULATED_DATA in simulated mode), the latest price, and a benchmark "
        "(sector ETF, else a peer basket of >= 3 peers, else SPY). Components need 25-65 sessions of history; coverage below 0.60 is INSUFFICIENT_DATA.",
        "Market Demand: 1-minute candles with volume, the last sessions' bars and quotes for the ticker and its benchmark; INTRADAY_MODE needs >= 30 completed session bars, otherwise DAILY_MODE.",
        f"Evidence: the latest COMPLETED Stage 5.4 packet, built only on explicit request (POST /api/evidence/packets); READY needs one no older than {c['evidence_max_age_days']} days.",
        "Setup inputs carry the computed PPE, Demand, the packet summary, the quote timestamp, source, mode and session hint; no position, P/L, future return or model.",
        "Proposal: a reference price (quote mid -> last trade -> completed 1-minute close -> last daily close), >= 15 completed sessions for ATR(14), the lows of the last 5 sessions, "
        "account equity, buying power, the existing position and pending orders.",
        "Safety gate: a persisted daily equity baseline, an account snapshot no older than 900 s (continuous read-only refresh) and the cash-flow activity feed.",
        "Execution: the Alpaca PAPER environment proof (GET /v2/account), the broker clock, broker positions and open orders.",
    ], sources=[f"{D56} §3-§5", f"{D57} §3", f"{D58} §2", f"{D59} §4-§8", f"{D591} §3", D60]))
    out.append(Section(key="entry_logic", title="Entry logic", state="DOCUMENTED", items=[
        f"A READY setup produces a deterministic long review proposal after every evaluation: reference price, invalidation level (structural low - 0.25 ATR, capped at reference - 1.5 ATR; "
        f"else reference - 1.5 ATR), risk per share, risk budget = equity x {_f(c['risk_fraction'])}, a whole-share quantity capped by the {_f(c['max_position_pct'])} % position cap and buying power; "
        f"an intraday reference expires after {c['proposal_ttl_s']} s.",
        f"{len(P.CONTROLS)} proposal controls ({', '.join(P.CONTROLS)}); any hard blocker makes the proposal BLOCKED, e.g. \"{P.TEXT[P.B_EQUITY_NA]}\" or \"{P.TEXT[P.B_DUPLICATE]}\".",
        f"Paper Auto (mode PAPER_AUTO) enters only when the setup is READY, the proposal is REVIEWABLE, the Stage 5.9.1 gate says TRADING_ALLOWED and every execution control passes "
        f"({n_exec_controls} controls: {', '.join(X.CONTROLS)}); open positions + pending entries must stay below {c['max_open_positions']}.",
        f"Regular session only, no entry in the final {c['near_close_minutes']} minutes; {c['order_type']} / {c['time_in_force']} orders; {c['side']} only; quantity = the proposal's final_qty; "
        f"at most {c['max_entries_per_cycle']} entry per cycle and one entry per setup episode (exactly once: execution_id = sha256('entry|' + proposal_id)).",
        "Monitor only (the default mode) never enters; it keeps monitoring and the exit rules keep running. The shell's READY means setup eligibility; execution eligibility is decided separately by the gate and the controls.",
    ], facts={k: c[k] for k in ("risk_fraction", "max_position_pct", "proposal_ttl_s", "proposal_price_change_pct", "max_open_positions", "near_close_minutes", "max_entries_per_cycle",
                                 "order_type", "time_in_force", "side")} | {"proposal_controls": list(P.CONTROLS), "blocker_codes": list(P.BLOCKER_CODES), "blocker_texts": {k: P.TEXT[k] for k in P.BLOCKER_CODES},
              "execution_controls": list(X.CONTROLS)},
       sources=["proposals/__init__.py", "proposals/engine.py", "execution/engine.py", f"{D59} §4-§9", f"{D60} §3-§5"]))
    out.append(Section(key="invalidation", title="Invalidation", state="DOCUMENTED", items=[
        f"Setup level: READY -> INVALIDATED on {S.REASON_TEXT[S.R_STRETCH_NORMALIZED].rstrip('.')} (Stretch > {_f(c['invalidate_stretch'])}), {S.REASON_TEXT[S.R_DEMAND_COLLAPSED].rstrip('.')} "
        f"(Demand < {c['invalidate_demand']} on {c['invalidate_demand_evals']} consecutive evaluations) or {S.REASON_TEXT[S.R_EVIDENCE_CONTRADICTION].rstrip('.')}; WATCH -> INVALIDATED on the evidence contradiction only.",
        f"Demotions that are not invalidation: READY -> WATCH when Demand < {c['ready_exit_demand']} or Demand is unavailable; WATCH -> NONE when the displacement fades (Stretch > {_f(c['watch_exit_stretch'])}). "
        "A paused evaluation never invalidates.",
        "Position level: an invalidated or closed setup episode exits the position - evidence contradiction -> EVIDENCE_INVALIDATION, demand collapse -> DEMAND_FAILURE, "
        "normalised displacement -> DISLOCATION_NORMALIZED, anything else -> SETUP_INVALIDATED.",
        "Price level: the proposal's invalidation price travels on the intent and the position; a price at or below it is a HARD_INVALIDATION exit, checked once per executor cycle "
        "(there is no resting stop at the broker).",
    ], facts={"invalidate_stretch": c["invalidate_stretch"], "invalidate_demand": c["invalidate_demand"], "invalidate_demand_evals": c["invalidate_demand_evals"],
              "ready_exit_demand": c["ready_exit_demand"], "watch_exit_stretch": c["watch_exit_stretch"]},
       sources=["setups/engine.py", "execution/engine.py", f"{D58} §4", f"{D59} §6", f"{D60} §6"]))
    out.append(Section(key="exit_logic", title="Exit logic", state="DOCUMENTED", items=[
        "Exit precedence (first match wins, every executor cycle): " + "; ".join(f"{i + 1}. {r} - {X.EXIT_TEXT[r]}" for i, r in enumerate(c["exit_reasons"])) + ".",
        f"DEMAND_FAILURE: Market Demand below {c['demand_exit']} on {c['demand_exit_evals']} consecutive post-entry evaluations. TIME_EXIT: after {c['max_hold_sessions']} regular sessions. "
        f"DISLOCATION_NORMALIZED: PPE Stretch above {_f(c['stretch_exit'])}.",
        'No profit target, no predicted price: "No profit target, no predicted price."',
        "Pause, the daily-loss halt and the Emergency Stop block new entries only; exits continue and the stop never liquidates. Monitor only keeps the exit rules running.",
        f"Exit orders are {c['order_type']} / {c['time_in_force']} in the regular session only, not in the final {c['near_close_minutes']} minutes; a rejected or cancelled exit opens the next exit cycle; "
        f"after {c['max_exit_cycles']} cycles the position needs attention (EXIT_ATTEMPTS_EXHAUSTED); a quantity mismatch never sells blindly.",
        "WEAKENING is informational only (Demand < 45 or Stretch > -30).",
    ], facts={"exit_reasons": list(c["exit_reasons"]), "exit_texts": dict(X.EXIT_TEXT), "demand_exit": c["demand_exit"], "demand_exit_evals": c["demand_exit_evals"], "stretch_exit": c["stretch_exit"],
              "max_hold_sessions": c["max_hold_sessions"], "max_exit_cycles": c["max_exit_cycles"], "near_close_minutes": c["near_close_minutes"]},
       sources=["execution/__init__.py", "execution/engine.py", "execution/service.py", f"{D60} §6-§7"]))
    out.append(Section(key="holding_horizon", title="Holding horizon", state="DOCUMENTED", items=[
        f"At most {c['max_hold_sessions']} regular sessions (TIME_EXIT). Sessions are counted as weekdays strictly after the entry date (the entry day counts 0); there is no holiday calendar, "
        "the broker clock is the backstop.",
        "No minimum hold and no target holding time are defined. The 1 / 5 / 20-day horizons of the PPE and Demand replays are diagnostics, not holding rules.",
        "Time horizon: SWING (a few sessions).",
    ], facts={"max_hold_sessions": c["max_hold_sessions"], "time_horizon": "SWING"}, sources=["execution/engine.py (sessions_held)", "setups/market_hours.py", f"{D60} §6", f"{D56} §8"]))
    out.append(Section(key="backtest", title="Backtest", state="NOT_EVALUATED", note=(
        f"{NOT_EVALUATED}. No historical backtest of DOWNSIDE_DISLOCATION_RECOVERY (setup state machine + proposal + exits) exists in this repository. The Stage 4.8-5.2 backtest, "
        "walk-forward, campaign and signal-research modules replay the Stage 4.7 portfolio rotation model, a different strategy family. The PPE and Market Demand replays "
        "(python -m ppe.replay, python -m demand.replay) score single components and are labelled descriptive only, not evidence of predictive power.")))
    out.append(Section(key="out_of_sample", title="Out-of-sample", state="NOT_EVALUATED", note=(
        f"{NOT_EVALUATED}. No out-of-sample test exists for this setup; its thresholds were fixed before any analysis, so there is no in-sample fit to hold out against. The only forward "
        "observation is the Stage 6.1 forward paper validation (Paper results), whose strategy status stays INSUFFICIENT_DATA below 30 closed trades.")))
    out.append(Section(key="risk_notes", title="Risk notes", state="DOCUMENTED", items=[
        f"Paper only (Alpaca PAPER account), {c['side']} only, leverage {c['leverage']}, shorts {c['shorts']}, no live money, no Robinhood write, no model in the decision path.",
        f"No resting stop at the broker: {c['order_type']} / {c['time_in_force']} orders only; the invalidation price is checked once per executor cycle (30 s in session).",
        "No holiday calendar: holidays count as weekdays; the broker clock is the backstop.",
        f"READY depends on a user-built evidence packet no older than {c['evidence_max_age_days']} days; Confidence is penalised for evidence age.",
        "Validation gaps: PPE Confidence was never replayed (no historical evidence); Market Demand's intraday mode was validated on synthetic sessions only; the real Alpaca market-data "
        "path was not live-tested on the development machine; no bid / ask microstructure.",
        "The component replays (2024-09 to 2026-09) ran in a rising market with one sharp correction; 25 of 81 replay symbols fell back to the SPY benchmark.",
        "Stale account or cash-flow data pauses new entries (fail closed).",
        "Forward validation limits: fees are not attributable per paper order; MFE / MAE are sampled at observer cadence; no market trend or volatility proxy; the PC must stay awake.",
        "Survivorship bias is not documented for this setup.",
    ], sources=[D60, D61, D58, f"{D56} / {V56}", f"{D57} / {V57}", "README.md"]))
    out.append(_paper_results(facts))
    out.append(Section(key="decision_log", title="Decision log", state="NOT_EVALUATED", note=(
        f"{NOT_EVALUATED}. No approval or rejection decision has been recorded for this strategy: no decision table exists and proposal review states are VIEWED / DISMISSED only. "
        "The lifecycle status is derived from persisted facts (validation sessions, Paper Auto); APPROVED / REJECTED require a recorded decision.")))
    return out


def _paper_results(facts: Optional[Dict[str, Any]]) -> Section:
    v = (facts or {}).get("validation")
    if not v:
        errs = ", ".join((facts or {}).get("errors") or []) or "no runtime facts in this process"
        return Section(key="paper_results", title="Paper results", state="UNAVAILABLE", note=f"Forward paper validation summary unavailable ({errs}). See forward_validation.html when the app runs.")
    completed, target = int(v.get("sessions_completed") or 0), v.get("sessions_target")
    closed = v.get("closed_trades")
    items = [f"Forward paper validation sessions: {completed} / {target if target is not None else '—'}" + (f" ({v['sessions_label']})" if v.get("sessions_label") else ""),
             f"Closed paper trades: {closed if closed is not None else '—'}" + (" (no closed paper trade yet)" if closed in (0, None) else ""),
             f"Strategy status: {v.get('edge_label') or '—'}" + (f" ({v['edge_status']})" if v.get("edge_status") else "") + (f" - {v['insufficient_text']}" if v.get("insufficient") and v.get("insufficient_text") else ""),
             "Attention: " + (", ".join(str(a) for a in v.get("attention") or []) if v.get("needs_attention") else "none")]
    return Section(key="paper_results", title="Paper results", state="LIVE", items=items, facts=dict(v),
                   sources=["GET /api/forward-validation", "forward_validation.html", f"{D61}"], note="Descriptive paper results from the Alpaca PAPER account; not a forecast and not a recommendation.")
