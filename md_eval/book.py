"""md_eval/book.py - the FROZEN book (owner: an evaluation assumption, never an optimised parameter) and the benchmark book.

STRATEGY book, at the decision close of session d (index i of the run's sessions), candidates ranked by (score descending, instrument_id):
  ALREADY_HELD          held, or already accepted this session
  EMBARGO               i < the segment's embargo sessions
  PURGE_BOUNDARY        the maximum holding window (the fill at i+1, the time exit decided at i+max_hold, filled at i+max_hold+1) ends after
                        the segment's last session
  MAX_NEW_PER_SESSION   2 new entries already accepted
  MAX_POSITIONS         4 positions after the pending exits and the accepted entries
  MAX_PER_SECTOR        2 in the sector (an UNKNOWN sector is one shared, conservative bucket)
  NO_DECISION_PRICE / INVALID_STOP   no raw close at d, or a stop at / above it
  notional = min(0.5 % x equity(d) x raw_close / (raw_close - stop), 10 % x equity(d))
At the fill (the next open): the slot and sector limits again (an exit may not have filled), then shares = floor(notional / raw_open)
(< 1 -> SIZE_BELOW_ONE_SHARE), then cash (shares x open + costs > cash -> CASH). Margin is never used.
BENCHMARK book (buy-and-hold weights): notional = weight x equity(d) / (1 + the bucket's friction rate), or an explicit notional; no limits."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from md_eval import BOOK


def check_frozen(overrides: Optional[dict]) -> None:
    if overrides:
        raise ValueError(f"the book is FROZEN (an evaluation assumption, never an optimised parameter); refused overrides: {sorted(overrides)}")


def rank(signals) -> list:
    return sorted(signals, key=lambda s: (-float(s.score), s.instrument_id))


def strategy_notional(raw_close, stop_raw, equity: float) -> Tuple[Optional[float], Optional[str]]:
    if not (isinstance(raw_close, (int, float)) and raw_close > 0 and math.isfinite(raw_close)):
        return None, "NO_DECISION_PRICE"
    if not (isinstance(stop_raw, (int, float)) and math.isfinite(stop_raw) and stop_raw < raw_close):
        return None, "INVALID_STOP"
    risk = BOOK["risk_fraction_of_current_equity"] * equity * raw_close / (raw_close - stop_raw)
    cap = BOOK["max_position_pct_of_current_equity"] / 100.0 * equity
    return min(risk, cap), None


def purge_hits(i: int, max_hold: Optional[int], last_index: int) -> bool:
    return max_hold is not None and i + max_hold + 1 > last_index


def decide_strategy_entries(ranked, *, i: int, last_index: int, embargo: int, held: Dict[str, str], exiting: set, equity: float,
                            sector_of) -> Tuple[List[tuple], List[tuple]]:
    """([(signal, notional, sector)], [(signal, reason)]) for the STRATEGY book."""
    accepted, refused = [], []
    remaining = {iid: sec for iid, sec in held.items() if iid not in exiting}
    sector_counts: Dict[str, int] = {}
    for sec in remaining.values():
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    taken = set()
    for s in ranked:
        sec = sector_of(s.instrument_id)
        if s.instrument_id in held or s.instrument_id in taken:
            refused.append((s, "ALREADY_HELD"))
        elif i < embargo:
            refused.append((s, "EMBARGO"))
        elif purge_hits(i, s.max_hold, last_index):
            refused.append((s, "PURGE_BOUNDARY"))
        elif len(accepted) >= BOOK["max_new_per_session"]:
            refused.append((s, "MAX_NEW_PER_SESSION"))
        elif len(remaining) + len(accepted) >= BOOK["max_positions"]:
            refused.append((s, "MAX_POSITIONS"))
        elif sector_counts.get(sec, 0) >= BOOK["max_per_sector"]:
            refused.append((s, "MAX_PER_SECTOR"))
        else:
            notional, why = strategy_notional(s.raw_close, s.stop_raw, equity)
            if why:
                refused.append((s, why))
                continue
            accepted.append((s, notional, sec))
            taken.add(s.instrument_id)
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    return accepted, refused


def fill_limits_ok(held: Dict[str, str], sector: str) -> Optional[str]:
    """At the fill: the slot and sector limits against what is actually held (after this open's exits)."""
    if len(held) >= BOOK["max_positions"]:
        return "MAX_POSITIONS"
    if sum(1 for s in held.values() if s == sector) >= BOOK["max_per_sector"]:
        return "MAX_PER_SECTOR"
    return None


def benchmark_notional(signal, equity: float, friction_rate: float) -> float:
    if signal.notional is not None:
        return float(signal.notional)
    return float(signal.weight) * equity / (1.0 + friction_rate)
