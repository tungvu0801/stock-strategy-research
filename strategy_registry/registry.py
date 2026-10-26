"""
strategy_registry/registry.py — the registry entries (code-defined, read only; no table, no mutation route).

ONE implemented strategy: DOWNSIDE_DISLOCATION_RECOVERY, the setup type every Stage 5.8-6.1 module evaluates (setups.SETUP_TYPE == proposals.SETUP_TYPE
== execution.SETUP_TYPE). Its dates are the repository's: the setup state machine merged on 2026-10-21 (PR #10), the forward paper validation on
2026-10-24 (PR #17). The four placeholders are registry rows only: implemented = False, status RESEARCH, no setup type, no rules, no data, no signal.
No QuantConnect strategy is registered: the source exists in the data model, the adapter is documented, nothing is wired.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from strategy_registry.models import StrategyEntry

DDR_ID = "DOWNSIDE_DISLOCATION_RECOVERY"
PLACEHOLDER_IDS = ("BREAKOUT",)                                   # Stage 6.4: VALUE_QUALITY, 6.5: MOMENTUM, 6.6: MEAN_REVERSION are implemented (research only)
PLACEHOLDER_NOTE = "Registry placeholder only: not implemented. No rules, no data requirements, no backtest, no out-of-sample result, no paper result and no signal exist."
REGISTRY_DATE = "2026-10-25"                                       # the day the registry (Stage 6.1.2) was created: the placeholders' created_at / updated_at

ENTRIES: List[StrategyEntry] = [
    StrategyEntry(strategy_id=DDR_ID, display_name="Downside Dislocation Recovery", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",
                  status="PAPER_CANDIDATE", version="1.0",
                  description=("Long recovery after a downside dislocation that buyers confirm. The Stage 5.6 PPE stretch opens a WATCH, PPE confidence and Stage 5.7 Market "
                               "Demand with fresh stored evidence make the setup READY (Stage 5.8); the Stage 5.9 review proposal, the Stage 5.9.1 safety gate and the Stage 6.0 "
                               "execution controls decide every Alpaca PAPER entry; exits are deterministic. Observed by the Stage 6.1 forward paper validation."),
                  created_at="2026-10-21", updated_at="2026-10-24", implemented=True, setup_type=DDR_ID),
    StrategyEntry(strategy_id="VALUE_QUALITY", display_name="Value + Quality", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="POSITION",
                  status="RESEARCH", version="1.0",
                  description=("Long-only monthly rebalance of the 20 highest-ranked stocks by an equal-weight composite of value (earnings, book and operating "
                               "cash-flow yield) and quality (return on assets, cash flow on assets, equity ratio), from point-in-time SEC fundamentals "
                               "(Stage 6.4, value_quality/). Research only: it is validated historically and never produces a trading signal."),
                  created_at=REGISTRY_DATE, updated_at="2026-10-25", implemented=True),
    StrategyEntry(strategy_id="MOMENTUM", display_name="Momentum", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="POSITION",
                  status="RESEARCH", version="1.0",
                  description=("Long-only monthly rebalance of the 20 stocks with the strongest 12-1 price momentum (the adjusted return from about 12 months "
                               "to about 1 month before the rebalance), equal weight, from daily bars (Stage 6.5, momentum/). Research only: it is "
                               "validated historically against its eligible universe and never produces a trading signal."),
                  created_at=REGISTRY_DATE, updated_at="2026-10-26", implemented=True),
    StrategyEntry(strategy_id="MEAN_REVERSION", display_name="Mean Reversion", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",
                  status="RESEARCH", version="1.0",
                  description=("Long-only weekly rebalance of the 20 stocks with the most negative 5-session adjusted return (short-term cross-sectional "
                               "reversal), equal weight, from daily bars (Stage 6.6, mean_reversion/). Research only: it is validated historically "
                               "against its eligible universe and never produces a trading signal."),
                  created_at=REGISTRY_DATE, updated_at="2026-10-26", implemented=True),
    StrategyEntry(strategy_id="BREAKOUT", display_name="Breakout", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",
                  status="RESEARCH", version="0.0", description="Future internal strategy (range breakout). " + PLACEHOLDER_NOTE,
                  created_at=REGISTRY_DATE, updated_at=REGISTRY_DATE, implemented=False, placeholder_note=PLACEHOLDER_NOTE),
]


def entries() -> List[StrategyEntry]:
    return [e.model_copy(deep=True) for e in ENTRIES]


def by_id() -> Dict[str, StrategyEntry]:
    return {e.strategy_id: e.model_copy(deep=True) for e in ENTRIES}


def get(strategy_id: str) -> Optional[StrategyEntry]:
    return by_id().get(str(strategy_id or "").upper())


def implemented() -> List[StrategyEntry]:
    return [e for e in entries() if e.implemented]
