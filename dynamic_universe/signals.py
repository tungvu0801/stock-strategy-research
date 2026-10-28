"""
dynamic_universe/signals.py - the cheap scanner's signal INTERFACES (Stage 6.10C): what a later stage could rank the core universe by, the
inputs each signal would need and its expected market-data request cost.

Declarations only. No signal is implemented (implemented() is empty), compute() refuses, no signal value is computed anywhere, and the scanner
reads no news, earnings or catalyst source. None of these is a strategy signal: a scanner could only choose which symbols the existing setup
evaluator looks at - READY, risk and execution stay with the existing engines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class SignalSpec:
    id: str
    label: str
    description: str
    inputs: Tuple[str, ...]
    request_cost: str
    implemented: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description, "inputs": list(self.inputs),
                "request_cost": self.request_cost, "implemented": self.implemented}


SNAPSHOT = "the batched market-data snapshot of the scanned symbols (one request covers every symbol)"
SIGNALS: Tuple[SignalSpec, ...] = (
    SignalSpec("relative_volume", "Relative volume",
               "Today's volume so far against the average daily volume of prior sessions.",
               ("today's volume so far: " + SNAPSHOT, "average daily volume: batched daily bars of prior sessions"),
               "1 batched snapshot request per scan, plus 1 batched daily-bar request refreshed at most every 15 minutes"),
    SignalSpec("gap_pct", "Gap %",
               "Today's open against the previous close, in percent.",
               ("today's open and the previous close: " + SNAPSHOT,),
               "no extra request: the same batched snapshot"),
    SignalSpec("intraday_move", "Intraday move",
               "The last trade against today's open, in percent.",
               ("last trade and today's open: " + SNAPSHOT,),
               "no extra request: the same batched snapshot"),
    SignalSpec("sector_strength", "Sector strength",
               "The move of the symbol's sector ETF against SPY, using the core universe's sector tags (Unmapped symbols have none).",
               ("the core universe's sector tags", "sector ETF and SPY moves: one batched snapshot of the data/sector_map.py ETFs and SPY"),
               "1 batched snapshot request per scan (12 sector ETFs + SPY)"),
    SignalSpec("catalyst_flag", "Earnings / catalyst flag",
               "Whether a scheduled earnings date or another company catalyst is near. No source is connected to the scanner.",
               ("none connected: the scanner reads no news, earnings or catalyst source",),
               "not estimated: no source is connected"),
    SignalSpec("unusual_volatility", "Unusual volatility",
               "Today's range so far against the symbol's average true range.",
               ("today's high and low so far: " + SNAPSHOT, "average true range: the batched daily bars shared with relative volume"),
               "no extra request beyond the batched snapshot and the 15-minute daily-bar request"),
)
SIGNAL_IDS: Tuple[str, ...] = tuple(s.id for s in SIGNALS)


def implemented() -> Tuple[SignalSpec, ...]:
    """The signals a scanner could compute today: none in Stage 6.10C."""
    return tuple(s for s in SIGNALS if s.implemented)


def compute(*_args: Any, **_kwargs: Any) -> Any:
    """Refuses: no signal value is computed in Stage 6.10C."""
    raise NotImplementedError("declared only in Stage 6.10C")


def describe() -> List[Dict[str, Any]]:
    return [s.as_dict() for s in SIGNALS]
