"""
market_intel/settings.py - the market-intelligence settings, normalised (the dynamic_universe/plan.py settings pattern).

Stage 7.0 adds NO setting to config.py (that is the Stage 7.3 wiring stage). Every value here is read with getattr and a default, so
today every setting is its default: MARKET_INTEL_MODE is OFF. An unknown mode, or ACTIVE (which does not exist), reads as OFF and is
reported. A request budget outside its range reads as its default and is reported: the Alpaca live budget can never exceed 20 % of the
Basic plan's 200 requests/min, because the evaluator and Trader View share that plan (DESIGN_610C section 9).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from market_intel import MODES, OFF

# name -> (default, minimum, maximum). Requests per minute unless the name says otherwise.
BUDGETS: Dict[str, Tuple[int, int, int]] = {
    "MI_ALPACA_MAX_PER_MIN": (40, 1, 40),                 # <= 20 % of Alpaca Basic (200/min), shared with the evaluator and Trader View
    "MI_ALPACA_BACKFILL_MAX_PER_MIN": (60, 1, 60),        # offline backfill, outside 07:00-20:00 New York time only (Stage 7.1)
    "MI_SEC_MAX_PER_MIN": (60, 1, 60),                    # <= 1 request/s for this layer (SEC allows 10/s per user)
    "MI_FRED_MAX_PER_MIN": (30, 1, 30),
    "MI_HALTS_MAX_PER_MIN": (1, 1, 1),                    # Nasdaq Trader: "do not query the data more than once a minute"
    "MI_ALPHA_VANTAGE_MAX_PER_DAY": (6, 0, 6),            # the free key allows 25/day; 1 calendar + 5 re-checks
}
MODE_SETTING = "MARKET_INTEL_MODE"


def _text(v: Any, n: int = 40) -> str:
    return str(v).strip()[:n]


def _int(v: Any):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdecimal() and len(v.strip()) <= 6:
        return int(v.strip())
    return None


def settings(cfg: Any = None) -> Dict[str, Any]:
    """{mode, configured_mode, budgets, declared, problems}. Pure over `cfg` (config by default); never raises."""
    if cfg is None:
        import config
        cfg = config
    raw = _text(getattr(cfg, MODE_SETTING, OFF) or "").upper()
    mode = raw if raw in MODES else OFF
    problems: List[str] = []
    if raw == "ACTIVE":
        problems.append(f"{MODE_SETTING}=ACTIVE does not exist (the data layer is read-only and display-only): OFF applies")
    elif raw and raw not in MODES:
        problems.append(f"{MODE_SETTING}={raw!r} is not one of {', '.join(MODES)}: OFF applies")
    budgets: Dict[str, int] = {}
    for name, (default, lo, hi) in BUDGETS.items():
        v = _int(getattr(cfg, name, default))
        if v is None or not lo <= v <= hi:
            problems.append(f"{name}={_text(getattr(cfg, name, default), 20)!r} is not an integer from {lo} to {hi}: {default} applies")
            v = default
        budgets[name] = v
    declared = sorted(n for n in (MODE_SETTING, *BUDGETS) if hasattr(cfg, n))
    return {"mode": mode, "configured_mode": raw or OFF, "budgets": budgets, "declared": declared, "problems": problems}
