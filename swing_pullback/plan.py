"""swing_pullback/plan.py — the pre-registered experiments, and the only way this package runs the harness.

Exactly two cost runs per version: BASE (5 bps per side) and STRESS_2X (10 bps per side); commission 0 per order, modelled explicitly; no fee
per share (regulatory fees are NOT_MODELLED and flagged). Every run uses the harness defaults otherwise: the chronological 70 / 30 split with
its boundary purge, the research sizing (0.5 % of 100 000, capped at 10 %), the RULES arm. Nothing here persists a run, fetches data or reads a
database: the caller passes the dataset (Stage 7.8: synthetic fixtures only)."""
from __future__ import annotations

from typing import Optional

from swing_pullback import COSTS
from swing_pullback.adapter import SwingTrendPullbackAdapter, SwingTrendPullbackControlAdapter

VERSIONS = {"V1": SwingTrendPullbackAdapter, "C1": SwingTrendPullbackControlAdapter}
RUNS = tuple((v, c) for v in VERSIONS for c in COSTS)                       # (V1, BASE) (V1, STRESS_2X) (C1, BASE) (C1, STRESS_2X)


def friction(cost: str) -> dict:
    if cost not in COSTS:
        raise ValueError(f"cost must be one of the pre-registered runs {', '.join(COSTS)}; got {cost!r}")
    return {"slippage_bps_per_side": COSTS[cost], "commission_per_order": 0.0, "fee_per_share": 0.0}


def adapter(version: str):
    if version not in VERSIONS:
        raise ValueError(f"version must be one of {', '.join(VERSIONS)}; got {version!r}")
    return VERSIONS[version]()


def run(version: str, cost: str, dataset, created_at: Optional[str] = None):
    """One pre-registered run, ephemeral: the ValidationRun is returned, never stored."""
    from strategy_validation import engine as E
    return E.run_validation(adapter(version), dataset, arms=("RULES",), friction=friction(cost), created_at=created_at)
