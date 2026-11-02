"""swing_pullback/adapter.py — the EVENT_DRIVEN validation-harness adapters of V1 (SWING_TREND_PULLBACK 1.0, a modified D.3 research baseline)
and of its control C1 (SWING_TREND_PULLBACK_CONTROL 1.0: V1 without the pullback rule).

One candidate per signal of swing_pullback/core.py: entry at the next session's open (NEXT_BAR_OPEN), stop and target fixed at the signal close,
a time exit at the close of the 10th session in the trade. The holding limit is the ADAPTER's (max_hold_bars), never a candidate's: no candidate
carries its own, so no exit can be set from a bar after the signal. Neither adapter is registered in strategy_validation/adapters.py or in the
strategy registry (Stage 7.8: synthetic data only); swing_pullback/plan.py runs them."""
from __future__ import annotations

from typing import Dict, Iterable

from swing_pullback import (CONTROL_ID, CONTROL_VERSION, DEVIATIONS, FLAG_CONTROL, FLAG_EARNINGS, FLAG_FEES, FLAG_IEX, FLAG_LEVELS, FLAG_MODIFIED,
                            FLAG_NO_CLOSE_EXIT, FLAG_PORTFOLIO, FLAG_SURVIVORSHIP, MAX_HOLD_BARS, RSI_MAX, SCORE, STOP_ATR, STRATEGY_ID, STRATEGY_VERSION,
                            TARGET_ATR)
from swing_pullback.core import SwingPullbackCore
from strategy_validation.adapters import StrategyAdapter
from strategy_validation.models import RawCandidate

PARAMS = ("rsi_max", "stop_atr", "target_atr", "max_hold_bars")            # the sensitivity neighbours' keys: nothing else is a parameter


def _r(x, n=10):
    return None if x is None else round(float(x), n)


class SwingTrendPullbackAdapter(StrategyAdapter):
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION
    source = "INTERNAL"
    historical_replay_supported = True
    entry_policy = "NEXT_BAR_OPEN"
    signal_basis = "SESSION_CLOSE"
    max_hold_bars = MAX_HOLD_BARS
    critical_features = ("close", "sma_slow", "sma_fast", "sma_fast_prior", "rsi", "atr", "regime_close", "regime_sma_slow", "raw_close")
    time_horizon = "SWING"
    risk_class = "UNCLASSIFIED"
    entry_condition = ("SPY close above its SMA200; close above SMA200 and SMA50 above its value 20 sessions earlier; Wilder RSI(2) at or below 10 or "
                       "three successive lower closes, with close at or above 0.97 x SMA50; raw close at least $5; 20-session median raw dollar volume at "
                       "least $20M (SIP volume; not applied on IEX volume); none for 9 sessions after the symbol's previous candidate; entry at the next "
                       "session's open")
    invalidation_condition = "the signal close minus 2 x ATR(14) (Wilder), fixed at the signal"
    exit_condition = ("the signal close plus 3 x ATR(14), or the close of the 10th session in the trade; D.3's close-based exit is not modelled "
                      "(deviation D1)")
    stop_policy = "INVALIDATION"
    time_exit_fill = "CLOSE"
    evaluation_mode = "EVENT_DRIVEN"
    live_implementation = None                            # no live implementation exists: replay parity is not applicable
    replay_parity_requirement = "NOT_APPLICABLE"
    provenance_requires = ("corporate_actions", "universe", "point_in_time")
    causality_review = {"status": "PENDING", "reference": ""}
    use_pullback_rule = True

    def __init__(self, params=None):
        super().__init__(params)
        unknown = sorted(set(self.params) - set(PARAMS))
        if unknown:
            raise ValueError(f"unknown parameter(s): {', '.join(unknown)} (only the pre-registered sensitivity keys {', '.join(PARAMS)})")
        hold = self.params.get("max_hold_bars", MAX_HOLD_BARS)
        if not isinstance(hold, int) or isinstance(hold, bool) or hold < 1:
            raise ValueError("max_hold_bars must be a positive integer")
        self.max_hold_bars = hold                                                   # one holding limit for the whole configuration
        self.core = SwingPullbackCore(self.use_pullback_rule, rsi_max=self.params.get("rsi_max", RSI_MAX),
                                      stop_atr=self.params.get("stop_atr", STOP_ATR), target_atr=self.params.get("target_atr", TARGET_ATR))
        self.funnel: Dict[str, dict] = {}

    def with_params(self, params: dict) -> "SwingTrendPullbackAdapter":
        return type(self)(params={**self.params, **(params or {})})

    def research_flags(self, dataset) -> list:
        flags = [FLAG_MODIFIED, FLAG_SURVIVORSHIP, FLAG_NO_CLOSE_EXIT, FLAG_EARNINGS, FLAG_LEVELS, FLAG_PORTFOLIO, FLAG_FEES]
        if (dataset.declared or {}).get("volume_feed") == "IEX":
            flags.append(FLAG_IEX)
        return sorted(flags)

    def deviations(self) -> dict:
        return dict(DEVIATIONS)

    def candidates(self, dataset) -> Iterable[RawCandidate]:
        ev = self.core.evaluate(dataset)
        self.funnel[dataset.dataset_id] = {"funnel": dict(ev["funnel"]), "volume_feed": ev["volume_feed"], "second_listings": list(ev["second_listings"]),
                                           "symbols": len(ev["symbols"])}
        for sg in ev["signals"]:
            yield self._raw(sg)

    def _raw(self, sg: dict) -> RawCandidate:
        d = sg["signal_date"]
        features = {"close": _r(sg["close"]), "sma_slow": _r(sg["sma_slow"]), "sma_fast": _r(sg["sma_fast"]), "sma_fast_prior": _r(sg["sma_fast_prior"]),
                    "rsi": _r(sg["rsi"]), "lower_closes": bool(sg["lower_closes"]), "depth_ratio": _r(sg["depth_ratio"]), "atr": _r(sg["atr"]),
                    "regime_close": _r(sg["regime_close"]), "regime_sma_slow": _r(sg["regime_sma_slow"]), "raw_close": _r(sg["raw_close"]),
                    "median_dollar_volume": _r(sg["median_dollar_volume"], 2), "liquidity_rule": "APPLIED" if sg["median_dollar_volume"] is not None
                    else "NOT_APPLIED_IEX_VOLUME", "stop_reference": _r(sg["stop"]), "target_reference": _r(sg["target"]),
                    "window_start": sg["window_start"], "pullback_rule": "APPLIED" if self.use_pullback_rule else "OMITTED"}
        as_of = {k: d for k in features}
        as_of["sma_fast_prior"] = sg["sma_fast_prior_date"]
        as_of["window_start"] = sg["window_start"]
        return RawCandidate(symbol=sg["symbol"], signal_date=d, entry_reference=sg["close"], invalidation_reference=sg["stop"], target_reference=sg["target"],
                            score=SCORE, features=features, feature_as_of=as_of)

    def strategy_diagnostics(self, record: dict, dataset) -> dict:
        from swing_pullback import diagnostics as SPD
        return SPD.diagnostics(record, dataset, self)


class SwingTrendPullbackControlAdapter(SwingTrendPullbackAdapter):
    """C1: V1 with the pullback rule omitted (the RSI(2) / lower-close trigger and the 0.97 x SMA50 depth guard); every other rule, value, exit and
    the cooldown are V1's. A control, not a strategy: it runs no sensitivity (the eight checks are V1's)."""
    strategy_id = CONTROL_ID
    strategy_version = CONTROL_VERSION
    critical_features = ("close", "sma_slow", "sma_fast", "sma_fast_prior", "atr", "regime_close", "regime_sma_slow", "raw_close")
    entry_condition = ("SPY close above its SMA200; close above SMA200 and SMA50 above its value 20 sessions earlier (no pullback rule: the control); "
                       "raw close at least $5; 20-session median raw dollar volume at least $20M (SIP volume; not applied on IEX volume); none for 9 sessions "
                       "after the symbol's previous candidate; entry at the next session's open")
    use_pullback_rule = False

    def research_flags(self, dataset) -> list:
        return sorted(super().research_flags(dataset) + [FLAG_CONTROL])
