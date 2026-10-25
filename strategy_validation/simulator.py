"""strategy_validation/simulator.py — one long candidate -> one hypothetical trade (research simulation only; nothing here sizes or sends a real order).

Execution causality: a session-close signal is known at the close, so the default entry is the NEXT bar's open (NEXT_BAR_OPEN); an entry at the
signal close needs the strategy's explicit market-on-close assumption (SIGNAL_CLOSE_MOC) and then exits are scanned from the next bar only. With a
next-bar-open entry the entry bar's own high / low happen after the fill and are scanned.

Exits, per bar, in order: a gap through the stop fills at the (worse) open (STOP_GAP); a gap through the target fills at the target (conservative,
never the better open); stop and target both inside one bar with unknown intrabar ordering -> STOP_FIRST (counted as an ambiguous bar, with the
target-first alternative recorded for the dependence diagnostic only); stop (STOP); target (TARGET); after max_hold_bars the close (TIME_EXIT); the
dataset ending first exits at the last close (DATA_END). An entry bar opening at / below the stop is skipped explicitly (ENTRY_GAP_THROUGH_STOP; the
Stage 5.9 proposal would block an invalid stop too), and so is one opening at / above the target (ENTRY_GAP_THROUGH_TARGET: never a TARGET exit below
the fill).

Friction (the Stage 3.2 convention, backtest/engine.py): buy fills at price x (1 + slippage_bps_per_side / 10 000), sell fills at price x (1 - ...);
commission_per_order per order plus fee_per_share per share and order. Gross, slippage, fees and net are all kept.

Risk-based sizing (the Stage 5.9 conventions, research only): qty = floor(equity x risk_fraction / risk per share), capped by max_position_pct of
equity; a quantity below one share is simulated as ONE share (MIN_ONE_UNIT, achieved risk above the configured risk - reported, never dropped); a cap
below the risk quantity is CAPPED (achieved below configured - reported)."""
from __future__ import annotations

import hashlib
import math
from typing import Optional, Tuple

from strategy_validation.models import SimulatedTrade, SkippedCandidate, StrategyCandidate


def _r(x: float) -> float:
    return round(float(x), 6)


def size(entry_fill: float, stop: float, sizing: dict) -> Tuple[int, str, float, float]:
    rps = entry_fill - stop
    budget = float(sizing["equity"]) * float(sizing["risk_fraction"])
    q_risk = int(math.floor(budget / rps)) if rps > 0 else 0
    q_cap = int(math.floor(float(sizing["equity"]) * float(sizing["max_position_pct"]) / 100.0 / entry_fill))
    qty = min(q_risk, q_cap)
    status = "RISK_SIZED" if qty == q_risk else "CAPPED"
    if qty < 1:
        qty, status = 1, "MIN_ONE_UNIT"
    return qty, status, budget, qty * rps


def simulate(c: StrategyCandidate, dataset, *, arm: str, segment: str, entry_policy: str, friction: dict, sizing: dict) -> Tuple[Optional[SimulatedTrade], Optional[SkippedCandidate]]:
    base = {"symbol": c.symbol, "signal_date": c.signal_date, "candidate_id": c.candidate_id}
    stop, target = c.invalidation_reference, c.target_reference
    if entry_policy == "SIGNAL_CLOSE_MOC":
        sig = dataset.bar(c.symbol, c.signal_date)
        window = dataset.bars_after(c.symbol, c.signal_date)
        if sig is None or not window:
            return None, SkippedCandidate(**base, reason="NO_ENTRY_BAR", detail="no bar after the signal session to exit on")
        entry_date, entry_raw, scan, first_bar_is_entry = sig.date, sig.close, window, False
    else:
        window = dataset.bars_after(c.symbol, c.signal_date)
        if not window:
            return None, SkippedCandidate(**base, reason="NO_ENTRY_BAR", detail="no bar after the signal session (dataset end)")
        entry_date, entry_raw, scan, first_bar_is_entry = window[0].date, window[0].open, window, True
    if entry_raw <= stop:
        return None, SkippedCandidate(**base, reason="ENTRY_GAP_THROUGH_STOP", detail=f"entry {entry_raw} at or below the invalidation {stop}")
    if target is not None and entry_raw >= target:
        return None, SkippedCandidate(**base, reason="ENTRY_GAP_THROUGH_TARGET", detail=f"entry {entry_raw} at or above the target {target}: the planned move is gone")
    slip = float(friction["slippage_bps_per_side"]) / 10000.0
    entry_fill = entry_raw * (1 + slip)
    qty, status, configured, achieved = size(entry_fill, stop, sizing)
    scan = scan[:c.max_hold_bars]
    exit_bar, exit_raw, reason, ambiguous, alt_raw, held = None, None, None, False, None, 0
    for k, bar in enumerate(scan, start=1):
        held = k
        if not (first_bar_is_entry and k == 1):
            if bar.open <= stop:
                exit_bar, exit_raw, reason = bar, bar.open, "STOP_GAP"
                break
            if target is not None and bar.open >= target:
                exit_bar, exit_raw, reason = bar, target, "TARGET"
                break
        stop_hit = bar.low <= stop
        tgt_hit = target is not None and bar.high >= target
        if stop_hit and tgt_hit:
            exit_bar, exit_raw, reason, ambiguous, alt_raw = bar, stop, "STOP", True, target
            break
        if stop_hit:
            exit_bar, exit_raw, reason = bar, stop, "STOP"
            break
        if tgt_hit:
            exit_bar, exit_raw, reason = bar, target, "TARGET"
            break
        if k == c.max_hold_bars:
            exit_bar, exit_raw, reason = bar, bar.close, "TIME_EXIT"
            break
    if exit_bar is None:                                                         # the dataset ended before the holding limit
        exit_bar = scan[-1]
        exit_raw, reason = exit_bar.close, "DATA_END"

    def money(xr: float) -> Tuple[float, float, float, float]:
        xf = xr * (1 - slip)
        gross = (xr - entry_raw) * qty
        slippage = ((entry_fill - entry_raw) + (xr - xf)) * qty
        fees = 2 * float(friction["commission_per_order"]) + 2 * float(friction["fee_per_share"]) * qty
        return xf, gross, slippage, fees

    exit_fill, gross, slippage, fees = money(exit_raw)
    net = gross - slippage - fees
    alt_net = None
    if ambiguous:
        _, g2, s2, f2 = money(alt_raw)
        alt_net = _r(g2 - s2 - f2)
    tid = hashlib.sha256(f"{arm}|{c.candidate_id}".encode("utf-8")).hexdigest()[:24]
    return SimulatedTrade(trade_id=tid, arm=arm, candidate_id=c.candidate_id, strategy_id=c.strategy_id, strategy_version=c.strategy_version, symbol=c.symbol, direction=c.direction,
                          segment=segment, signal_date=c.signal_date, entry_date=entry_date, entry_policy=entry_policy, entry_raw=_r(entry_raw), entry_fill=_r(entry_fill), stop=_r(stop),
                          target=None if target is None else _r(target), exit_date=exit_bar.date, exit_raw=_r(exit_raw), exit_fill=_r(exit_fill), exit_reason=reason, holding_bars=held,
                          max_hold_bars=c.max_hold_bars, qty=qty, size_status=status, configured_risk=_r(configured), achieved_risk=_r(achieved), gross_pnl=_r(gross),
                          slippage_cost=_r(slippage), fees=_r(fees), net_pnl=_r(net), r_gross=_r(gross / achieved), r_net=_r(net / achieved), trade_return=_r(net / (entry_fill * qty)),
                          ambiguous_bar=ambiguous, alt_target_first_net=alt_net), None
