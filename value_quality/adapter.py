"""value_quality/adapter.py — the historical replay adapter of Value + Quality for the Stage 6.2 / 6.3.1 harness (designed for validation from day 1).

Rebalance schedule: the FIRST session of every month (a session is the first of its month when the previous session is in another month - known
without any future data), from the 21st session of the dataset on. At each rebalance the shared core (value_quality/core.py) ranks the universe
from point-in-time data and decides {enter, hold, exit}.

A candidate is one MONTHLY LEG of a holding: every stock selected at rebalance R (signal = R's close) is held from the open of the next session
to the open of the first session after the NEXT rebalance (a TIME_EXIT filled at the open: the moment the next leg would begin - strictly after
the next decision). Consecutive legs of a stock that stays selected chain without a gap, and only real transactions pay friction:
entry_trade = the stock was not selected at the previous rebalance (ENTER), exit_trade = it is not selected at the next one (EXIT); a leg that
continues a position on either side (HOLD) is not an order there. The last legs run to the data end (DATA_END). No stop (stop_policy NONE);
equal-weight notional sizing (1 / top_n of the fixed equity); the harness R of a leg is its return on the position notional.

Why legs and not whole holding spells: with ~7 % monthly turnover a spell lasts about a year, so the Stage 6.3.1 PURGE would remove every
holding open at the TRAIN end (the persistent ones - a selection on the result) and the OOS window would start almost empty. With monthly legs
only the legs of the boundary month are purged and the OOS window holds the full portfolio from its first rebalance. The legs within one month
are not independent observations: the per-leg t-statistic is evidence only; the portfolio diagnostics show the monthly view."""
from __future__ import annotations

from typing import Dict, Iterable, List

from strategy_validation.adapters import StrategyAdapter
from strategy_validation.models import RawCandidate
from value_quality import (DOLLAR_VOLUME_SESSIONS, FLAG_SURVIVORSHIP, RESEARCH_UNIVERSE, STRATEGY_ID, STRATEGY_VERSION, TOP_N, VALUE_FACTORS, QUALITY_FACTORS,
                           VALUE_WEIGHT)
from value_quality.core import ValueQualityStrategyCore
from value_quality.data import BundleData


def rebalance_sessions(sessions: List[str]) -> List[str]:
    return [s for i, s in enumerate(sessions) if i >= DOLLAR_VOLUME_SESSIONS and s[:7] != sessions[i - 1][:7]]


def _r(x, n=10):
    return None if x is None else round(float(x), n)


class ValueQualityAdapter(StrategyAdapter):
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION
    source = "INTERNAL"
    historical_replay_supported = True
    entry_policy = "NEXT_BAR_OPEN"
    signal_basis = "SESSION_CLOSE"
    max_hold_bars = 1                                     # unused: every spell carries its own holding limit
    critical_features = ("composite_score", "value_score", "quality_score", "market_cap")
    time_horizon = "POSITION"
    risk_class = "MODERATE"
    entry_condition = "ranked in the top N of the Value + Quality composite at a monthly rebalance (signal at the close of the month's first session)"
    invalidation_condition = "no price stop: the position is re-examined at every monthly rebalance"
    exit_condition = "leaves the top N or becomes ineligible at a later rebalance: exit at the close of the following session"
    stop_policy = "NONE"
    time_exit_fill = "OPEN"                               # a leg ends at the open after the next rebalance (where the next leg begins)
    live_implementation = None                            # no separate live implementation exists: replay parity is not applicable
    replay_parity_requirement = "NOT_APPLICABLE"
    provenance_requires = ("corporate_actions", "universe", "point_in_time")
    causality_review = {"status": "PENDING", "reference": ""}

    def __init__(self, params=None):
        super().__init__(params)
        self.top_n = int(self.params.get("top_n", TOP_N))
        self.value_weight = float(self.params.get("value_weight", VALUE_WEIGHT))
        self.sizing = {"mode": "EQUAL_WEIGHT", "position_pct": 100.0 / self.top_n}
        self.core = ValueQualityStrategyCore(self.top_n, self.value_weight)
        self.rebalance_log: Dict[str, List[dict]] = {}

    def with_params(self, params: dict) -> "ValueQualityAdapter":
        return type(self)(params={**self.params, **(params or {})})

    def research_flags(self, dataset) -> list:
        return [FLAG_SURVIVORSHIP]                        # a fixed universe of today's large caps: never suppressed

    def candidates(self, dataset) -> Iterable[RawCandidate]:
        data = BundleData(dataset)
        universe = [s for s in RESEARCH_UNIVERSE if s in dataset.bars]
        decisions: List[dict] = []
        prev: set = set()
        for R in rebalance_sessions(dataset.sessions):
            dec = self.core.evaluate(R, universe, data, data.calendar, prev=prev)   # selection is stateless; prev only labels enter / hold / exit
            decisions.append(dec)
            prev = set(dec["selected"])
        self.rebalance_log[dataset.dataset_id] = [{**d["summary"], "rebalance": d["rebalance"], "enter": d["enter"], "hold": d["hold"], "exit": d["exit"]}
                                                  for d in decisions]
        for k, dec in enumerate(decisions):
            nxt = decisions[k + 1] if k + 1 < len(decisions) else None
            prev_sel = set(decisions[k - 1]["selected"]) if k > 0 else set()
            next_sel = set(nxt["selected"]) if nxt else set()
            by = {x["symbol"]: x for x in dec["ranked"]}
            for sym in dec["selected"]:
                yield self._raw({"symbol": sym, "signal": dec["rebalance"], "ranked": by[sym], "eligible": len(dec["ranked"]),
                                 "next_rebalance": nxt["rebalance"] if nxt else None, "entry_trade": sym not in prev_sel,
                                 "exit_trade": nxt is None or sym not in next_sel}, dataset)

    def _raw(self, sp: dict, dataset) -> RawCandidate:
        sym, R, x = sp["symbol"], sp["signal"], sp["ranked"]
        snap = x["snapshot"]
        inp, fac = snap["inputs"], snap["factors"]
        after = dataset.bars_after(sym, R)
        if sp["next_rebalance"] is None:
            hold = len(after) + 1                                                   # the last legs run to the data end: DATA_END
        else:
            idx = next((i for i, b in enumerate(after) if b.date > sp["next_rebalance"]), None)
            hold = idx + 1 if idx is not None else len(after) + 1
        fund = {k: inp[k] for k in ("assets", "net_income_ttm", "operating_cash_flow_ttm", "equity") if inp.get(k)}
        prov = {k: {"value": _r(v["value"], 4), "period_end": v["period_end"], "filed": v["filed"], "available_at": v["available_at"], "accn": v.get("accn"),
                    "concept": v.get("concept")} for k, v in fund.items()}
        sh = inp["diluted_share_count"]
        features = {"composite_score": _r(x["composite"] * 100, 6), "value_score": _r(x["value_score"]), "quality_score": _r(x["quality_score"]), "rank": x["rank"],
                    "eligible_count": sp["eligible"], **{f: _r(fac[f]) for f in VALUE_FACTORS + QUALITY_FACTORS},
                    "market_cap": _r(inp["market_cap"], 2), "raw_close": inp["raw_close"], "median_dollar_volume": _r(inp["median_dollar_volume"], 2),
                    "diluted_share_count": {"reported": sh["reported"], "split_factor": sh["split_factor"], "period_end": sh["period_end"], "filed": sh["filed"],
                                            "available_at": sh["available_at"], "concept": sh.get("concept")},
                    "fundamentals": prov}
        as_of = {k: R for k in features}
        as_of["diluted_share_count"] = sh["available_at"]
        as_of["fundamentals"] = max(v["available_at"] for v in prov.values())
        bar = dataset.bar(sym, R)
        return RawCandidate(symbol=sym, signal_date=R, entry_reference=bar.close if bar else None, invalidation_reference=None, target_reference=None,
                            score=_r(x["composite"] * 100, 6), features=features, feature_as_of=as_of, max_hold_bars=max(1, hold),
                            entry_trade=sp["entry_trade"], exit_trade=sp["exit_trade"])

    def audit_extra(self, record: dict, dataset) -> list:
        from value_quality import audit as VA
        return VA.audit(record, dataset, top_n=self.top_n, value_weight=self.value_weight)

    def strategy_diagnostics(self, record: dict, dataset) -> dict:
        from value_quality import diagnostics as VD
        return VD.diagnostics(record, dataset, self.rebalance_log.get(dataset.dataset_id) or [], top_n=self.top_n)
