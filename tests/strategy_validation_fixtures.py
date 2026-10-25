"""tests/strategy_validation_fixtures.py — SYNTHETIC, deterministic strategy fixtures for testing the Stage 6.2 validation harness itself.

Test-only: never registered in the Strategy Registry, never written to a real database (the store refuses SYNTHETIC runs outside a test process).
Every candidate gets its own symbol and a 7-session window (signal bar d0, entry bar d1, ...), and its outcome is designed bar by bar:

  W  target 110 hit on d2 (+~2R)          L  stop 95 hit on d2 (-1R)          A  stop AND target inside d1 (ambiguous: STOP_FIRST)
  F  a 5-bar time exit at 100.08 (gross +0.08 / share, below the round-trip slippage: friction-killed)

Fixtures: GOOD (PASS), OVERFIT (TRAIN strong, OOS negative: FAIL), TOO_SMALL (10 trades: INSUFFICIENT_DATA), FRICTION_KILLED (gross > 0, net < 0:
FAIL), LOOKAHEAD (features dated after the signal + a negative shift in the source: INVALID), CANDIDATE_MISMATCH (an arm hook drops a candidate:
INVALID), AMBIGUOUS (many ambiguous bars: stop-first dependence)."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_validation.adapters import StrategyAdapter  # noqa: E402
from strategy_validation.dataset import Dataset  # noqa: E402
from strategy_validation.models import RawCandidate  # noqa: E402

START = date(2019, 1, 2)
WINDOW = 7


def business_days(start: date, n: int) -> List[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bar(d, o, h, low, c):
    return {"date": d, "open": o, "high": h, "low": low, "close": c, "volume": 1_000_000.0}


def window(days: List[str], outcome: str) -> List[dict]:
    d0, d1, d2, d3, d4, d5, d6 = days
    sig = _bar(d0, 99.0, 101.0, 98.5, 100.0)
    if outcome == "W":
        rest = [_bar(d1, 100.0, 103.0, 98.0, 102.0), _bar(d2, 102.0, 111.0, 101.0, 108.0), _bar(d3, 108.0, 109.0, 106.0, 107.0), _bar(d4, 107.0, 108.0, 105.0, 106.0),
                _bar(d5, 106.0, 107.0, 104.0, 105.0), _bar(d6, 105.0, 106.0, 103.0, 104.0)]
    elif outcome == "L":
        rest = [_bar(d1, 100.0, 101.0, 97.0, 98.0), _bar(d2, 98.0, 98.5, 94.0, 95.5), _bar(d3, 95.5, 96.0, 93.0, 94.0), _bar(d4, 94.0, 95.0, 92.0, 93.0),
                _bar(d5, 93.0, 94.0, 91.0, 92.0), _bar(d6, 92.0, 93.0, 90.0, 91.0)]
    elif outcome == "A":
        rest = [_bar(d1, 100.0, 111.0, 94.0, 100.0), _bar(d2, 100.0, 101.0, 99.0, 100.0), _bar(d3, 100.0, 101.0, 99.0, 100.0), _bar(d4, 100.0, 101.0, 99.0, 100.0),
                _bar(d5, 100.0, 101.0, 99.0, 100.0), _bar(d6, 100.0, 101.0, 99.0, 100.0)]
    elif outcome == "F":
        rest = [_bar(d1, 100.0, 100.5, 99.5, 100.05), _bar(d2, 100.05, 100.5, 99.6, 100.0), _bar(d3, 100.0, 100.4, 99.7, 100.1), _bar(d4, 100.1, 100.4, 99.8, 100.02),
                _bar(d5, 100.02, 100.3, 99.8, 100.08), _bar(d6, 100.08, 100.3, 99.9, 100.1)]
    else:
        raise ValueError(outcome)
    return [sig] + rest


class FixtureAdapter(StrategyAdapter):
    """A test strategy whose candidates are the planned windows (symbol, signal day)."""
    strategy_id = "FIXTURE_GOOD"
    strategy_version = "1.0"
    critical_features = ("stretch", "demand")
    entry_condition = "fixture signal at the close"
    invalidation_condition = "price at or below 95"
    exit_condition = "target 110, stop 95 or 5 sessions"
    risk_class = "MODERATE"

    def __init__(self, plan: List[Tuple[str, str]], params=None, future_features: bool = False, strategy_id: str = None, missing: Tuple[int, ...] = ()):
        super().__init__(params)
        self.plan, self.future_features, self.missing = plan, future_features, set(missing)
        if strategy_id:
            self.strategy_id = strategy_id

    def with_params(self, params):
        return type(self)(self.plan, params=params, future_features=self.future_features, strategy_id=self.strategy_id, missing=tuple(self.missing))

    def candidates(self, dataset):
        keep = self.params.get("keep")                                          # sensitivity fixtures: an explicit neighbouring selection rule
        if isinstance(keep, str):                                               # a JSON-friendly rule for the CLI: "mod:5:0,1,2" keeps i % 5 in {0, 1, 2}
            _, m, rest = keep.split(":")
            allowed = {int(x) for x in rest.split(",")}
            keep = (lambda i, m=int(m), a=allowed: i % m in a)
        for i, (sym, d0) in enumerate(self.plan):
            if keep and not keep(i):
                continue
            nxt = dataset.next_bar(sym, d0)
            as_of = nxt.date if (self.future_features and nxt) else d0
            feats = {"stretch": -50.0, "demand": None if i in self.missing else 70.0}
            yield RawCandidate(symbol=sym, signal_date=d0, entry_reference=100.0, invalidation_reference=95.0, target_reference=110.0, score=60.0,
                               features=feats, feature_as_of={"stretch": as_of, "demand": as_of})


class PeekingAdapter(FixtureAdapter):
    """Selection on the future without a dated feature: a candidate is emitted only when its NEXT bar closes above the signal close (the features
    stay dated at the signal). Only the point-in-time truncation check can see this."""
    strategy_id = "FIXTURE_PEEKING"

    def candidates(self, dataset):
        for raw in super().candidates(dataset):
            nxt = dataset.next_bar(raw.symbol, raw.signal_date)
            if nxt is not None and nxt.close > dataset.bar(raw.symbol, raw.signal_date).close:
                yield raw


class LookaheadAdapter(FixtureAdapter):
    strategy_id = "FIXTURE_LOOKAHEAD"

    def _future_close(self, closes):                                            # the known future dependency the static scan must flag
        return closes.shift(-1)


def build(outcomes: List[str], start: date = START, kind: str = "SYNTHETIC") -> Tuple[Dataset, List[Tuple[str, str]]]:
    days = business_days(start, WINDOW * len(outcomes))
    bars: Dict[str, List[dict]] = {}
    plan = []
    for i, o in enumerate(outcomes):
        sym = f"S{i:03d}"
        w = days[WINDOW * i: WINDOW * (i + 1)]
        bars[sym] = window(w, o)
        plan.append((sym, w[0]))
    return Dataset(bars, kind=kind, source="synthetic fixture"), plan


def pattern(n: int, train: str, oos: str = None, train_ratio: float = 0.7) -> List[str]:
    """Outcome letters for n candidates: `train` cycles over the first ~70 %, `oos` (default: the same) over the rest."""
    cut = int(n * train_ratio)
    oos = oos or train
    return [train[i % len(train)] for i in range(cut)] + [oos[i % len(oos)] for i in range(n - cut)]


FIXTURES: Dict[str, Callable] = {
    "GOOD": lambda: build(pattern(100, "WWWLL")),
    "OVERFIT": lambda: build(pattern(100, "WWWWL", "WLLLL")),
    "TOO_SMALL": lambda: build(pattern(10, "WWWLL")),
    "FRICTION_KILLED": lambda: build(["F"] * 100),
    "AMBIGUOUS": lambda: build(pattern(100, "WWALL")),
}


def fixture(name: str, **kw):
    dataset, plan = FIXTURES["GOOD" if name in ("LOOKAHEAD", "CANDIDATE_MISMATCH") else name]()
    cls = LookaheadAdapter if name == "LOOKAHEAD" else FixtureAdapter
    sid = {"GOOD": "FIXTURE_GOOD", "OVERFIT": "FIXTURE_OVERFIT", "TOO_SMALL": "FIXTURE_TOO_SMALL", "FRICTION_KILLED": "FIXTURE_FRICTION_KILLED",
           "LOOKAHEAD": "FIXTURE_LOOKAHEAD", "CANDIDATE_MISMATCH": "FIXTURE_MISMATCH", "AMBIGUOUS": "FIXTURE_AMBIGUOUS"}[name]
    return dataset, cls(plan, future_features=(name == "LOOKAHEAD"), strategy_id=sid, **kw)


def drop_first(arm):
    """CANDIDATE_MISMATCH: an arm that silently lost one candidate (what the same-candidate-set guarantee must catch)."""
    return arm.model_copy(update={"input_candidate_ids": arm.input_candidate_ids[1:], "decisions": arm.decisions[1:]})


class SymbolGate:
    """A deterministic test gate (vetoes symbols ending in 5); NOT a real strategy gate."""
    name = "test_symbol_gate"

    def decide(self, c):
        return ("VETO", "symbol ends in 5") if c.symbol.endswith("5") else ("ALLOW", "")


class DemandModel:
    """A deterministic fake model decider: STAND_ASIDE every third candidate. No provider, no network."""
    name = "fake_model"

    def __init__(self):
        self.n = 0

    def decide(self, c):
        self.n += 1
        return {"decision": "STAND_ASIDE" if self.n % 3 == 0 else "TAKE", "reason": "fake rule", "score": 0.5}


class GreedyModel:
    """A model that tries to change the trade (a stop) - must be refused."""
    name = "greedy_model"

    def decide(self, c):
        return {"decision": "TAKE", "stop": 90.0}


class MutatingModel:
    """A model that mutates the candidate copy it receives - the generated candidate must stay untouched."""
    name = "mutating_model"

    def decide(self, c):
        c.features["demand"] = 999.0
        return {"decision": "TAKE", "reason": "mutated its copy"}
