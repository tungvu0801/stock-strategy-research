"""breakout/adapter.py — the historical replay adapter of Breakout v1 for the validation harness (validation_v3, CROSS_SECTIONAL_PORTFOLIO).

Rebalance schedule: the FIRST session of every ISO week once the 20-session reference and liquidity histories exist (known without any future
data). At each rebalance the shared core (breakout/core.py) finds the stocks whose adjusted close is above the highest adjusted high of the 20
sessions before, ranks them by breakout strength and decides {enter, hold, exit} for at most 20 of them.

A candidate is one WEEKLY LEG of a holding (the earlier strategies' convention, so the selection-control comparison is like for like): every stock
selected at rebalance R (signal = R's close) is held from the open of the next session to the open of the first session after the NEXT
rebalance. entry_trade = not selected at the previous rebalance (ENTER); exit_trade = not selected at the next one (EXIT); a continuation (HOLD)
is not an order and pays no friction. The last rebalance's legs run to the data end. No stop. Capital: FIXED SLOTS - each selected stock gets
1 / N of the fixed equity and the unused slots stay cash at 0 % (declared to validation_v3 as `strategy_capital`, so a week's strategy return is
the sum of its legs / N and a week without a breakout is an all-cash observation)."""
from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List

from breakout import CASH_RETURN, FLAG_SURVIVORSHIP, LOOKBACK_SESSIONS, MAX_HOLDINGS, RESEARCH_UNIVERSE, STRATEGY_ID, STRATEGY_VERSION
from breakout.core import BreakoutStrategyCore
from breakout.data import PriceData
from strategy_validation.adapters import StrategyAdapter
from strategy_validation.models import RawCandidate


def _r(x, n=10):
    return None if x is None else round(float(x), n)


def eligible_set_sha(symbols) -> str:
    """The identity of a rebalance's eligible set (the selection control's membership), part of every candidate - and so of the candidate set hash."""
    return hashlib.sha256(",".join(sorted(symbols)).encode("utf-8")).hexdigest()[:16]


class BreakoutAdapter(StrategyAdapter):
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION
    source = "INTERNAL"
    historical_replay_supported = True
    entry_policy = "NEXT_BAR_OPEN"
    signal_basis = "SESSION_CLOSE"
    max_hold_bars = 1                                     # unused: every leg carries its own holding limit
    critical_features = ("breakout_strength", "rank", "eligible_count")
    time_horizon = "SWING"
    risk_class = "HIGH"                                   # most breakouts can fail and reverse; exposure varies with the number of breakouts
    entry_condition = ("adjusted close above the highest adjusted high of the 20 sessions before, among the 20 strongest breakouts at a weekly "
                       "rebalance (signal at the close of the week's first session)")
    invalidation_condition = "no price stop: the position is re-examined at every weekly rebalance"
    exit_condition = "no longer among the selected breakouts (or ineligible) at a later rebalance: exit at the open of the following session"
    stop_policy = "NONE"
    time_exit_fill = "OPEN"
    live_implementation = None                            # no separate live implementation exists: replay parity is not applicable
    replay_parity_requirement = "NOT_APPLICABLE"
    provenance_requires = ("corporate_actions", "universe", "point_in_time")
    causality_review = {"status": "PENDING", "reference": ""}
    evaluation_mode = "CROSS_SECTIONAL_PORTFOLIO"
    market_benchmark = "SPY"
    # Stage 6.5.1: the universe declaration - today's list projected backwards; unknown values stay None
    universe_declaration = {"universe_method": "FIXED_CURRENT_UNIVERSE",
                            "universe_source": "breakout.RESEARCH_UNIVERSE = value_quality.RESEARCH_UNIVERSE: the 81 stocks of the Stage 3.2 research store (today's large caps)",
                            "membership_as_of_semantics": "one fixed list, known today, applied to every historical rebalance: no as-of membership",
                            "includes_delisted": False, "includes_acquired": False, "includes_bankrupt": False,
                            "historical_additions_supported": False, "historical_removals_supported": False, "survivorship_bias": "PRESENT",
                            "coverage_start": None, "coverage_end": None,                  # a fixed list has no historical coverage: unknown, not filled in
                            "notes": ("for a breakout ranking the bias is not neutral: today's large caps are partly the stocks whose breakouts kept going, "
                                      "breakouts that later collapsed into delisting or a sale are absent; the eligible-universe control is built from the same list")}

    def __init__(self, params=None):
        super().__init__(params)
        self.max_holdings = int(self.params.get("max_holdings", MAX_HOLDINGS))
        self.lookback = int(self.params.get("lookback_sessions", LOOKBACK_SESSIONS))
        slot_pct = 100.0 / self.max_holdings
        self.sizing = {"mode": "EQUAL_WEIGHT", "position_pct": slot_pct}
        # validation_v3: judged per weekly period against the FULLY INVESTED equal-weight eligible universe; the strategy's own capital is fixed slots
        self.control_definition = {"control_id": "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE", "label": "Equal-weight eligible universe", "role": "SELECTION_CONTROL",
                                   "membership": "every stock eligible at the rebalance (breaking out or not): the same universe, filters, lookback and "
                                                 "data-integrity rule as the selection",
                                   "weighting": "EQUAL_WEIGHT", "rebalance": "the strategy's weekly rebalance sessions",
                                   "execution": "the legs' timing: the open after the rebalance to the open after the next one (the last period: the last close)",
                                   "friction": "RUN_FRICTION_MODEL", "capital": "EQUAL_CAPITAL_BUY_AND_HOLD",
                                   "strategy_capital": {"basis": "FIXED_SLOTS_CASH", "slots": self.max_holdings, "slot_pct": slot_pct, "cash_return": CASH_RETURN},
                                   "membership_size_feature": "eligible_count",
                                   "omits": "the breakout selection (it holds every eligible stock, fully invested; the strategy holds its breakouts and cash)"}
        self.core = BreakoutStrategyCore(self.max_holdings, self.lookback)
        self.rebalance_log: Dict[str, List[dict]] = {}

    def with_params(self, params: dict) -> "BreakoutAdapter":
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
                                                   "selected": list(d["selected"]), "eligible_symbols": list(d["eligible"]),
                                                   "candidates": {x["symbol"]: {"strength": x["strength"], "reference": x["snapshot"]["inputs"]["breakout_reference"]}
                                                                  for x in d["ranked"]},
                                                   "data_exclusions": d["data_exclusions"]} for d in decisions]
        for k, dec in enumerate(decisions):
            nxt = decisions[k + 1] if k + 1 < len(decisions) else None
            prev_sel = set(decisions[k - 1]["selected"]) if k > 0 else set()
            next_sel = set(nxt["selected"]) if nxt else set()
            by = {x["symbol"]: x for x in dec["ranked"]}
            counts = {"eligible": len(dec["eligible"]), "candidates": len(dec["ranked"]), "selected": len(dec["selected"]), "sha": eligible_set_sha(dec["eligible"])}
            for sym in dec["selected"]:
                yield self._raw({"symbol": sym, "signal": dec["rebalance"], "ranked": by[sym], **counts, "next_rebalance": nxt["rebalance"] if nxt else None,
                                 "entry_trade": sym not in prev_sel, "exit_trade": nxt is None or sym not in next_sel}, dataset)

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
        features = {"breakout_score": _r(x["percentile"] * 100, 6), "breakout_strength": _r(x["strength"]), "rank": x["rank"],
                    "breakout_reference": inp["breakout_reference"], "reference_date": inp["reference_date"], "reference_start_date": inp["reference_start_date"],
                    "reference_end_date": inp["reference_end_date"], "close": inp["close"], "lookback_sessions": self.lookback,
                    "eligible_count": sp["eligible"], "breakout_candidate_count": sp["candidates"], "selected_count": sp["selected"], "eligible_set_sha": sp["sha"],
                    "raw_close": inp["raw_close"], "median_dollar_volume": _r(inp["median_dollar_volume"], 2)}
        as_of = {k: R for k in features}
        as_of.update(breakout_reference=inp["reference_end_date"], reference_date=inp["reference_date"], reference_start_date=inp["reference_start_date"],
                     reference_end_date=inp["reference_end_date"])
        bar = dataset.bar(sym, R)
        return RawCandidate(symbol=sym, signal_date=R, entry_reference=bar.close if bar else None, invalidation_reference=None, target_reference=None,
                            score=_r(x["percentile"] * 100, 6), features=features, feature_as_of=as_of, max_hold_bars=max(1, hold),
                            entry_trade=sp["entry_trade"], exit_trade=sp["exit_trade"])

    def audit_extra(self, record: dict, dataset) -> list:
        from breakout import audit as BOA
        return BOA.audit(record, dataset, max_holdings=self.max_holdings, lookback=self.lookback)

    def strategy_diagnostics(self, record: dict, dataset) -> dict:
        from breakout import diagnostics as BOD
        return BOD.diagnostics(record, dataset, self.rebalance_log.get(dataset.dataset_id) or [], max_holdings=self.max_holdings)
