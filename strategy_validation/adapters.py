"""strategy_validation/adapters.py — how a strategy is replayed historically for validation. An adapter declares its identity (strategy id + version),
its execution assumptions (entry policy, holding limit, signal basis), its critical features and the Strategy Standard v1 condition texts, and turns a
dataset into raw candidates. A strategy WITHOUT an honest historical replay declares historical_replay_supported = False with the reason: the harness
then reports NOT_RUN and never manufactures a backtest.

Registered strategies:
  DOWNSIDE_DISLOCATION_RECOVERY  no honest historical replay (below) -> NOT_RUN
  VALUE_QUALITY / MOMENTUM / MEAN_REVERSION / BREAKOUT  placeholders: not implemented -> NOT_RUN
Test fixtures live in tests/strategy_validation_fixtures.py and are never registered here."""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

from strategy_validation.models import RawCandidate


class StrategyAdapter:
    strategy_id = ""
    strategy_version = ""
    source = "INTERNAL"
    historical_replay_supported = True
    not_supported_reason = ""
    entry_policy = "NEXT_BAR_OPEN"            # NEXT_BAR_OPEN | SIGNAL_CLOSE_MOC
    moc_declared = False                      # SIGNAL_CLOSE_MOC requires an explicit market-on-close assumption
    signal_basis = "SESSION_CLOSE"
    max_hold_bars = 5
    critical_features: Tuple[str, ...] = ()
    time_horizon = "SWING"
    risk_class = "UNCLASSIFIED"
    entry_condition = ""
    invalidation_condition = ""
    exit_condition = ""
    # Stage 6.3.1 declarations read by the persistence gate (strategy_validation/integrity.py). None = UNDECLARED, which blocks persistence:
    # a decision that does not apply must still be made explicitly.
    live_implementation: Optional[str] = None          # the separate live implementation this adapter claims to reproduce (a module path), or None
    replay_parity_requirement: Optional[str] = None    # EXACT_REQUIRED (a live implementation exists) | NOT_APPLICABLE (research-only strategy)
    replay_parity_status: Optional[str] = None         # ESTABLISHED (parity tests pass) | NOT_ESTABLISHED
    replay_parity_reference: Optional[str] = None      # ESTABLISHED must cite its evidence (e.g. the parity test module)
    provenance_requires: Optional[Tuple[str, ...]] = None   # which of corporate_actions / universe / point_in_time the dataset must declare
    causality_review: Optional[dict] = None             # {"status": "PENDING" | "COMPLETED", "reference": "..."}: the MANUAL review, declared

    def __init__(self, params: Optional[dict] = None):
        self.params = dict(params or {})

    def candidates(self, dataset) -> Iterable[RawCandidate]:
        raise NotImplementedError

    def with_params(self, params: dict) -> "StrategyAdapter":
        """A neighbouring configuration for the sensitivity diagnostic (a new instance; this one is never changed)."""
        return type(self)(params=params)

    def describe(self) -> dict:
        return {"strategy_id": self.strategy_id, "strategy_version": self.strategy_version, "source": self.source, "entry_policy": self.entry_policy,
                "moc_declared": self.moc_declared, "signal_basis": self.signal_basis, "max_hold_bars": self.max_hold_bars, "critical_features": list(self.critical_features),
                "params": dict(self.params), "historical_replay_supported": self.historical_replay_supported,
                "declarations": {"live_implementation": self.live_implementation, "replay_parity_requirement": self.replay_parity_requirement,
                                 "replay_parity_status": self.replay_parity_status, "replay_parity_reference": self.replay_parity_reference,
                                 "provenance_requires": None if self.provenance_requires is None else list(self.provenance_requires),
                                 "causality_review": None if self.causality_review is None else dict(self.causality_review)}}


DDR_REASON = ("No honest historical replay exists for Downside Dislocation Recovery: READY needs a point-in-time Stage 5.4 evidence packet, and no "
              "packet exists for past sessions (the Stage 5.6 PPE replay could not compute Confidence: EVIDENCE_NOT_AVAILABLE for every observation); "
              "intraday Market Demand was validated on synthetic sessions only. A backtest would have to invent that evidence, so the strategy is not "
              "validated here. Its forward evidence is the Stage 6.1 forward paper validation. Replay readiness, the missing inputs and the "
              "forward-data plan: DESIGN_63_DDR_REPLAY_READINESS.md (faithful replay not established).")


class DownsideRecoveryAdapter(StrategyAdapter):
    strategy_id = "DOWNSIDE_DISLOCATION_RECOVERY"
    strategy_version = "1.0"
    historical_replay_supported = False
    not_supported_reason = DDR_REASON
    max_hold_bars = 5
    live_implementation = "setups/engine.py"            # the live setup state machine: any replay must reproduce it EXACTLY (DESIGN_63 §11)
    replay_parity_requirement = "EXACT_REQUIRED"
    replay_parity_status = "NOT_ESTABLISHED"            # DESIGN_63: faithful replay not established - the engine refuses before the gate is reached
    provenance_requires = ("corporate_actions", "universe", "point_in_time")
    causality_review = {"status": "PENDING", "reference": ""}

    def candidates(self, dataset) -> Iterable[RawCandidate]:
        raise NotImplementedError(DDR_REASON)


ADAPTERS: Dict[str, type] = {"DOWNSIDE_DISLOCATION_RECOVERY": DownsideRecoveryAdapter}
PLACEHOLDER_REASON = "Not implemented: a registry placeholder has no rules, so no candidate can be generated."


def adapter_for(strategy_id: str, registry_entry=None) -> Tuple[Optional[StrategyAdapter], str]:
    sid = str(strategy_id or "").upper()
    if registry_entry is not None and not registry_entry.implemented:
        return None, PLACEHOLDER_REASON
    cls = ADAPTERS.get(sid)
    if cls is None:
        return None, f"No historical validation adapter exists for {sid}."
    a = cls()
    if not a.historical_replay_supported:
        return None, a.not_supported_reason
    return a, ""


def not_run_reason(strategy_id: str, registry_entry=None) -> str:
    _, reason = adapter_for(strategy_id, registry_entry)
    return reason
