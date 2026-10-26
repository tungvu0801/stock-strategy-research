"""momentum/adapter.py — the historical replay adapter of Momentum v1 for the validation harness (validation_v3, CROSS_SECTIONAL_PORTFOLIO).

Rebalance schedule: the FIRST session of every month once the 252-session lookback exists in the dataset (known without any future data). At each
rebalance the shared core (momentum/core.py) ranks the universe by 12-1 momentum and decides {enter, hold, exit}.

A candidate is one MONTHLY LEG of a holding (the Value + Quality convention, so the selection-control comparison is like for like): every stock
selected at rebalance R (signal = R's close) is held from the open of the next session to the open of the first session after the NEXT rebalance
(a TIME_EXIT filled at the open, where the next leg begins). entry_trade = not selected at the previous rebalance (ENTER); exit_trade = not
selected at the next one (EXIT); a continuation (HOLD) is not an order and pays no friction. The last rebalance's legs run to the data end. No stop
(stop_policy NONE); equal-weight notional sizing (1 / top_n of the fixed equity). The legs of one month are not independent observations: the
selection edge is judged per rebalance period against the selection control (strategy_validation/cross_sectional.py)."""
from __future__ import annotations

from typing import Dict, Iterable, List

from momentum import FLAG_SURVIVORSHIP, LOOKBACK_SESSIONS, RESEARCH_UNIVERSE, SKIP_SESSIONS, STRATEGY_ID, STRATEGY_VERSION, TOP_N
from momentum.core import MomentumStrategyCore
from momentum.data import PriceData
from strategy_validation.adapters import StrategyAdapter
from strategy_validation.models import RawCandidate


def _r(x, n=10):
    return None if x is None else round(float(x), n)


class MomentumAdapter(StrategyAdapter):
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION
    source = "INTERNAL"
    historical_replay_supported = True
    entry_policy = "NEXT_BAR_OPEN"
    signal_basis = "SESSION_CLOSE"
    max_hold_bars = 1                                     # unused: every leg carries its own holding limit
    critical_features = ("momentum_return", "rank", "eligible_count")
    time_horizon = "POSITION"
    risk_class = "HIGH"                                   # momentum portfolios can lose sharply in rebounds (momentum crashes)
    entry_condition = "ranked in the top N by 12-1 momentum at a monthly rebalance (signal at the close of the month's first session)"
    invalidation_condition = "no price stop: the position is re-examined at every monthly rebalance"
    exit_condition = "leaves the top N or becomes ineligible at a later rebalance: exit at the open of the following session"
    stop_policy = "NONE"
    time_exit_fill = "OPEN"
    live_implementation = None                            # no separate live implementation exists: replay parity is not applicable
    replay_parity_requirement = "NOT_APPLICABLE"
    provenance_requires = ("corporate_actions", "universe", "point_in_time")
    causality_review = {"status": "PENDING", "reference": ""}
    # validation_v3: judged per rebalance period against the equal-weight eligible universe
    evaluation_mode = "CROSS_SECTIONAL_PORTFOLIO"
    control_definition = {"control_id": "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", "label": "Equal-weight eligible universe", "role": "SELECTION_CONTROL",
                          "membership": "every stock eligible at the rebalance: the same universe, filters and lookback as the selection",
                          "weighting": "EQUAL_WEIGHT", "rebalance": "the strategy's rebalance sessions",
                          "execution": "the legs' timing: the open after the rebalance to the open after the next one (the last period: the last close)",
                          "friction": "RUN_FRICTION_MODEL", "capital": "EQUAL_CAPITAL_BUY_AND_HOLD",
                          "membership_size_feature": "eligible_count", "omits": "the momentum ranking (it holds every eligible stock)"}
    market_benchmark = "SPY"

    def __init__(self, params=None):
        super().__init__(params)
        self.top_n = int(self.params.get("top_n", TOP_N))
        self.lookback = int(self.params.get("lookback_sessions", LOOKBACK_SESSIONS))
        self.skip = int(self.params.get("skip_sessions", SKIP_SESSIONS))
        self.sizing = {"mode": "EQUAL_WEIGHT", "position_pct": 100.0 / self.top_n}
        self.core = MomentumStrategyCore(self.top_n, self.lookback, self.skip)
        self.rebalance_log: Dict[str, List[dict]] = {}

    def with_params(self, params: dict) -> "MomentumAdapter":
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
                                                   "selected": list(d["selected"]), "eligible_symbols": [x["symbol"] for x in d["ranked"]]} for d in decisions]
        for k, dec in enumerate(decisions):
            nxt = decisions[k + 1] if k + 1 < len(decisions) else None
            prev_sel = set(decisions[k - 1]["selected"]) if k > 0 else set()
            next_sel = set(nxt["selected"]) if nxt else set()
            by = {x["symbol"]: x for x in dec["ranked"]}
            for sym in dec["selected"]:
                yield self._raw({"symbol": sym, "signal": dec["rebalance"], "ranked": by[sym], "eligible": len(dec["ranked"]),
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
        features = {"momentum_score": _r(x["percentile"] * 100, 6), "momentum_return": _r(x["momentum"]), "rank": x["rank"], "eligible_count": sp["eligible"],
                    "lookback_start_date": inp["lookback_start_date"], "lookback_end_date": inp["lookback_end_date"],
                    "lookback_start_close": inp["lookback_start_close"], "lookback_end_close": inp["lookback_end_close"],
                    "lookback_sessions": self.lookback, "skip_sessions": self.skip, "raw_close": inp["raw_close"],
                    "median_dollar_volume": _r(inp["median_dollar_volume"], 2)}
        as_of = {k: R for k in features}
        as_of.update(lookback_start_close=inp["lookback_start_date"], lookback_end_close=inp["lookback_end_date"], momentum_return=inp["lookback_end_date"],
                     lookback_start_date=inp["lookback_start_date"], lookback_end_date=inp["lookback_end_date"])
        bar = dataset.bar(sym, R)
        return RawCandidate(symbol=sym, signal_date=R, entry_reference=bar.close if bar else None, invalidation_reference=None, target_reference=None,
                            score=_r(x["percentile"] * 100, 6), features=features, feature_as_of=as_of, max_hold_bars=max(1, hold),
                            entry_trade=sp["entry_trade"], exit_trade=sp["exit_trade"])

    def audit_extra(self, record: dict, dataset) -> list:
        from momentum import audit as MA
        return MA.audit(record, dataset, top_n=self.top_n, lookback=self.lookback, skip=self.skip)

    def strategy_diagnostics(self, record: dict, dataset) -> dict:
        from momentum import diagnostics as MD
        return MD.diagnostics(record, dataset, self.rebalance_log.get(dataset.dataset_id) or [], top_n=self.top_n)
