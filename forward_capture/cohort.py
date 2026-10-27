"""
forward_capture/cohort.py - the configuration snapshot and the forward paper cohort (Stage 6.10A).

  snapshot(stream)   the ALLOWLISTED configuration a forward observation is made under, from code constants and config values only (no
                     environment dump, no credential, no host secret, no account number) and its canonical SHA-256 (config_hash)
  cohort key         the DECISION-RELEVANT subset only: strategy versions, the frozen Stage 6.1 strategy controls hash, the entry-controls
                     contract hash (the Stage 6.9.1 gate on / off and exactly how), the executor's live limits, the broker kind and the market-data
                     mode. A change of any of them starts a NEW cohort - forward evidence of different behaviour is never pooled silently. The
                     universe, cadences and the auto-run policy are recorded in the snapshot (config_hash) but do not split a cohort.
  cohort id / label  cohort_id = 'FP-' + cohort_key[:12]; the label FP_<year>_<nn> is the cohort's sequence number within its starting year.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import config
from forward_capture import STRATEGY_ID
from forward_capture import VERSION as CAPTURE_VERSION
from forward_capture.project import sha

COHORT_PREFIX = "FP-"
EXECUTOR_LIMITS = ("MAX_OPEN_POSITIONS", "EXECUTION_MAX_SHARES_PER_ENTRY", "EXECUTION_MAX_ENTRIES_PER_CYCLE", "EXECUTION_NEAR_CLOSE_MINUTES", "EXECUTION_MAX_HOLD_SESSIONS",
                   "EXECUTION_DEMAND_EXIT", "EXECUTION_DEMAND_EXIT_EVALS", "EXECUTION_STRETCH_EXIT", "EXECUTION_MAX_EXIT_CYCLES", "EXECUTION_LOOKUP_SPAN_SECONDS",
                   "MAX_DAILY_LOSS_PERCENT", "MAX_POSITION_PERCENT", "TRADE_PROPOSAL_RISK_FRACTION")
CADENCES = ("EXECUTION_CYCLE_SECONDS", "EXECUTION_IDLE_CYCLE_SECONDS", "SETUP_EVAL_INTERVAL_SECONDS", "FORWARD_VALIDATION_INTERVAL_SECONDS", "FORWARD_VALIDATION_IDLE_SECONDS",
            "ACCOUNT_REFRESH_SECONDS", "ACCOUNT_REFRESH_IDLE_SECONDS", "ROBINHOOD_PORTFOLIO_TTL_SECONDS")
FLAGS = ("EXECUTION_SCHEDULER_ENABLED", "SETUP_EVALUATOR_ENABLED", "SETUP_MARKET_HOURS_ONLY", "ACCOUNT_REFRESH_ENABLED", "FORWARD_VALIDATION_ENABLED",
         "FORWARD_VALIDATION_EARLY_STOP", "ROBINHOOD_PORTFOLIO_ENABLED", "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED")
COHORT_KEYS = ("strategies", "frozen_controls_hash", "entry_controls", "executor_limits", "broker", "market_data_mode")


def _cfg(names) -> Dict[str, Any]:
    return {n: getattr(config, n, None) for n in names}


def strategies() -> List[Dict[str, Any]]:
    """The registry strategies with their lifecycle declaration; only the DDR setup type can reach the executor (research strategies never trade)."""
    try:
        from strategy_registry import registry as R
        rows = R.entries()
    except Exception:  # noqa: BLE001 - recorded as unreadable, never invented
        return [{"strategy_id": STRATEGY_ID, "version": None, "status": "UNREADABLE", "executes": True}]
    return [{"strategy_id": e.strategy_id, "version": e.version, "status": e.status, "setup_type": e.setup_type, "executes": e.setup_type == STRATEGY_ID}
            for e in sorted(rows, key=lambda e: e.strategy_id)]


def versions() -> Dict[str, Any]:
    import execution as X
    import forward_validation as FV
    import proposals as P
    import safety as SF
    import setups as S
    from autorun import VERSION as AUTORUN_VERSION
    from execution import exposure_gate as G
    return {"setups": S.VERSION, "setup_type": S.SETUP_TYPE, "proposals": P.VERSION, "safety": SF.VERSION, "execution": X.VERSION, "entry_controls": G.VERSION,
            "forward_validation": FV.VERSION, "forward_capture": CAPTURE_VERSION, "autorun": AUTORUN_VERSION}


def snapshot(stream: Any = None, universe: Any = None) -> Dict[str, Any]:
    """{config, config_hash, cohort_key, cohort_definition}. `universe` (the watchlist) is passed in or read from the stream; unreadable = None."""
    import forward_validation as FV
    from autorun import calendar as CAL
    from autorun.policy import settings_from
    from execution import exposure_gate as G
    gate = G.from_config(config)
    if universe is None and stream is not None:
        try:
            universe = list(stream.watch_symbols())
        except Exception:  # noqa: BLE001
            universe = None
    market_mode = None
    try:
        market_mode = getattr(getattr(stream, "market", None), "mode", None)
    except Exception:  # noqa: BLE001
        market_mode = None
    uni = sorted({str(s).upper() for s in universe}) if universe is not None else None
    s = settings_from(config)
    from fit import readonly as RO
    try:
        db = RO.db_path()
        app = {"app_root": db.parent.parent.name, "db_name": db.name}
    except Exception:  # noqa: BLE001
        app = {"app_root": None, "db_name": None}
    cfg = {"strategies": strategies(), "versions": versions(), "frozen_controls": FV.frozen_controls(), "frozen_controls_hash": FV.frozen_hash(),
           "entry_controls": {"version": G.VERSION, "contract_hash": G.contract_hash(gate), "gate_enabled": gate.enabled, "contract": G.contract(gate)},
           "executor_limits": _cfg(EXECUTOR_LIMITS), "cadences": _cfg(CADENCES), "flags": _cfg(FLAGS),
           "broker": str(getattr(config, "EXECUTION_BROKER", "alpaca_paper")).strip().lower(),
           "market_data_mode": {"configured": getattr(config, "TRADER_DATA_MODE", None), "resolved": market_mode},
           "session_policy": "new entries in the US regular session only (SESSION_POLICY + the near-close cutoff); no extended hours",
           "autorun": {k: s[k] for k in ("policy", "effective_policy", "scheduled_arm_enabled", "disarm_at_session_end", "arm_delay_minutes")},
           "universe": {"symbols": uni, "count": len(uni) if uni is not None else None, "sha256": sha(uni) if uni is not None else None},
           "calendar": CAL.CALENDAR_VERSION, "app": app}
    key_src = {k: cfg[k] for k in COHORT_KEYS}
    key_src["entry_controls"] = {"version": G.VERSION, "contract_hash": G.contract_hash(gate)}
    key_src["strategies"] = [x for x in cfg["strategies"] if x.get("executes")] + [{"versions": {k: cfg["versions"][k] for k in ("setups", "setup_type", "proposals", "safety", "execution")}}]
    cohort_key = sha(key_src)
    return {"config": cfg, "config_hash": sha(cfg), "cohort_key": cohort_key, "cohort_id": COHORT_PREFIX + cohort_key[:12], "cohort_definition": key_src}


def cohort_of(trades_cohorts: Dict[str, str], execution_id: str) -> Tuple[str, bool]:
    """(cohort_id, recorded) of an entry: from its immutable entry snapshot when one exists, else LEGACY (entered before the capture)."""
    c = trades_cohorts.get(execution_id)
    return (c, True) if c else ("LEGACY", False)
