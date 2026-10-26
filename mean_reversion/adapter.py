"""mean_reversion/adapter.py — the historical replay adapter of Mean Reversion v1 for the validation harness (validation_v3, CROSS_SECTIONAL_PORTFOLIO).

Rebalance schedule: the FIRST session of every ISO week once the 20-session liquidity history exists in the dataset (known without any future
data). At each rebalance the shared core (mean_reversion/core.py) ranks the universe by the 5-session adjusted return, MOST NEGATIVE FIRST, and
decides {enter, hold, exit}.

A candidate is one WEEKLY LEG of a holding (the Value + Quality / Momentum convention, so the selection-control comparison is like for like): every
stock selected at rebalance R (signal = R's close) is held from the open of the next session to the open of the first session after the NEXT
rebalance (a TIME_EXIT filled at the open, where the next leg begins). entry_trade = not selected at the previous rebalance (ENTER); exit_trade =
not selected at the next one (EXIT); a continuation (HOLD) is not an order and pays no friction. The last rebalance's legs run to the data end. No
stop (stop_policy NONE); equal-weight notional sizing (1 / top_n of the fixed equity). The legs of one week are not independent observations: the
selection edge is judged per weekly period against the selection control (strategy_validation/cross_sectional.py)."""
from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List

from mean_reversion import FLAG_SURVIVORSHIP, LOOKBACK_SESSIONS, RESEARCH_UNIVERSE, STRATEGY_ID, STRATEGY_VERSION, TOP_N
from mean_reversion.core import MeanReversionStrategyCore
from mean_reversion.data import PriceData
from strategy_validation.adapters import StrategyAdapter
from strategy_validation.models import RawCandidate


def _r(x, n=10):
    return None if x is None else round(float(x), n)


def eligible_set_sha(symbols) -> str:
    """The identity of a rebalance's eligible set (the selection control's membership), part of every candidate - and so of the candidate set hash."""
    return hashlib.sha256(",".join(sorted(symbols)).encode("utf-8")).hexdigest()[:16]


class MeanReversionAdapter(StrategyAdapter):
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION
    source = "INTERNAL"
    historical_replay_supported = True
    entry_policy = "NEXT_BAR_OPEN"
    signal_basis = "SESSION_CLOSE"
    max_hold_bars = 1                                     # unused: every leg carries its own holding limit
    critical_features = ("recent_return", "rank", "eligible_count")
    time_horizon = "SWING"
    risk_class = "HIGH"                                   # it buys recent losers on purpose: a loser can keep falling (a falling knife)
    entry_condition = "ranked among the N most negative 5-session adjusted returns at a weekly rebalance (signal at the close of the week's first session)"
    invalidation_condition = "no price stop: the position is re-examined at every weekly rebalance"
    exit_condition = "leaves the N most negative or becomes ineligible at a later rebalance: exit at the open of the following session"
    stop_policy = "NONE"
    time_exit_fill = "OPEN"
    live_implementation = None                            # no separate live implementation exists: replay parity is not applicable
    replay_parity_requirement = "NOT_APPLICABLE"
    provenance_requires = ("corporate_actions", "universe", "point_in_time")
    causality_review = {"status": "PENDING", "reference": ""}
    # validation_v3: judged per weekly rebalance period against the equal-weight eligible universe
    evaluation_mode = "CROSS_SECTIONAL_PORTFOLIO"
    control_definition = {"control_id": "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", "label": "Equal-weight eligible universe", "role": "SELECTION_CONTROL",
                          "membership": "every stock eligible at the rebalance: the same universe, filters, lookback and data-integrity rule as the selection",
                          "weighting": "EQUAL_WEIGHT", "rebalance": "the strategy's weekly rebalance sessions",
                          "execution": "the legs' timing: the open after the rebalance to the open after the next one (the last period: the last close)",
                          "friction": "RUN_FRICTION_MODEL", "capital": "EQUAL_CAPITAL_BUY_AND_HOLD",
                          "membership_size_feature": "eligible_count", "omits": "the reversal ranking (it holds every eligible stock)"}
    market_benchmark = "SPY"
    # Stage 6.5.1: the universe declaration - today's list projected backwards; unknown values stay None
    universe_declaration = {"universe_method": "FIXED_CURRENT_UNIVERSE",
                            "universe_source": "mean_reversion.RESEARCH_UNIVERSE = value_quality.RESEARCH_UNIVERSE: the 81 stocks of the Stage 3.2 research store (today's large caps)",
                            "membership_as_of_semantics": "one fixed list, known today, applied to every historical rebalance: no as-of membership",
                            "includes_delisted": False, "includes_acquired": False, "includes_bankrupt": False,
                            "historical_additions_supported": False, "historical_removals_supported": False, "survivorship_bias": "PRESENT",
                            "coverage_start": None, "coverage_end": None,                  # a fixed list has no historical coverage: unknown, not filled in
                            "notes": ("for a loser ranking the bias is not neutral: the losers that later failed, were delisted or were bought cheaply are "
                                      "absent, so every loser in this universe is one that survived; the eligible-universe control is built from the same list")}

    def __init__(self, params=None):
        super().__init__(params)
        self.top_n = int(self.params.get("top_n", TOP_N))
        self.lookback = int(self.params.get("lookback_sessions", LOOKBACK_SESSIONS))
        self.sizing = {"mode": "EQUAL_WEIGHT", "position_pct": 100.0 / self.top_n}
        self.core = MeanReversionStrategyCore(self.top_n, self.lookback)
        self.rebalance_log: Dict[str, List[dict]] = {}

    def with_params(self, params: dict) -> "MeanReversionAdapter":
        return type(self)(params={**self.params, **(params or {})})

    def research_flags(self, dataset) -> list:
        return [FLAG_SURVIVORSHIP]                        # a fixed universe of today's large caps: never suppressed

    def decisions(self, dataset) -> List[dict]:
        data = PriceData(dataset)
        universe = [s for s in RESEARCH_UNIVERSE if s in dataset.bars]
        out, prev = [], set()
        for R in self.core.schedule(dataset.sessions):
            dec = self.core.evaluate(R, universe, data, prev=prev)              # selection is stateless; prev only labels enter / hold / exit
            out.append(dec)
            prev = set(dec["selected"])
        return out

    def candidates(self, dataset) -> Iterable[RawCandidate]:
        decisions = self.decisions(dataset)
        self.rebalance_log[dataset.dataset_id] = [{**d["summary"], "rebalance": d["rebalance"], "enter": d["enter"], "hold": d["hold"], "exit": d["exit"],
                                                   "selected": list(d["selected"]), "eligible_symbols": [x["symbol"] for x in d["ranked"]],
                                                   "recent_returns": {x["symbol"]: x["recent_return"] for x in d["ranked"]},
                                                   "data_exclusions": d["data_exclusions"]} for d in decisions]
        for k, dec in enumerate(decisions):
            nxt = decisions[k + 1] if k + 1 < len(decisions) else None
            prev_sel = set(decisions[k - 1]["selected"]) if k > 0 else set()
            next_sel = set(nxt["selected"]) if nxt else set()
            by = {x["symbol"]: x for x in dec["ranked"]}
            sha = eligible_set_sha(by)
            for sym in dec["selected"]:
                yield self._raw({"symbol": sym, "signal": dec["rebalance"], "ranked": by[sym], "eligible": len(dec["ranked"]), "eligible_sha": sha,
                                 "next_rebalance": nxt["rebalance"] if nxt else None, "entry_trade": sym not in prev_sel,
                                 "exit_trade": nxt is None or sym not in next_sel}, dataset)

    def selection_snapshots(self, dataset) -> list:
        if dataset.dataset_id not in self.rebalance_log:
            for _ in self.candidates(dataset):
                pass
        return [{"rebalance": r["rebalance"], "eligible": list(r["eligible_symbols"]), "selected": list(r["selected"])}
                for r in self.rebalance_log[dataset.dataset_id]]

    def _raw(self, sp: dict, dataset) -> RawCandidate:
        sym, R, x = sp["symbol"], sp["signal"], sp["ranked"]
        inp = x["snapshot"]["inputs"]
        after = dataset.bars_after(sym, R)
        if sp["next_rebalance"] is None:
            hold = len(after) + 1                                                   # the last legs run to the data end: DATA_END
        else:
            idx = next((i for i, b in enumerate(after) if b.date > sp["next_rebalance"]), None)
            hold = idx + 1 if idx is not None else len(after) + 1
        features = {"reversal_score": _r(x["percentile"] * 100, 6), "recent_return": _r(x["recent_return"]), "rank": x["rank"], "eligible_count": sp["eligible"],
                    "eligible_set_sha": sp["eligible_sha"], "lookback_start_date": inp["lookback_start_date"], "lookback_end_date": inp["lookback_end_date"],
                    "lookback_start_close": inp["lookback_start_close"], "lookback_end_close": inp["lookback_end_close"], "lookback_sessions": self.lookback,
                    "raw_close": inp["raw_close"], "median_dollar_volume": _r(inp["median_dollar_volume"], 2)}
        as_of = {k: R for k in features}
        as_of.update(lookback_start_close=inp["lookback_start_date"], lookback_start_date=inp["lookback_start_date"])
        bar = dataset.bar(sym, R)
        return RawCandidate(symbol=sym, signal_date=R, entry_reference=bar.close if bar else None, invalidation_reference=None, target_reference=None,
                            score=_r(x["percentile"] * 100, 6), features=features, feature_as_of=as_of, max_hold_bars=max(1, hold),
                            entry_trade=sp["entry_trade"], exit_trade=sp["exit_trade"])

    def audit_extra(self, record: dict, dataset) -> list:
        from mean_reversion import audit as MRA
        return MRA.audit(record, dataset, top_n=self.top_n, lookback=self.lookback)

    def strategy_diagnostics(self, record: dict, dataset) -> dict:
        from mean_reversion import diagnostics as MRD
        return MRD.diagnostics(record, dataset, self.rebalance_log.get(dataset.dataset_id) or [], top_n=self.top_n)
