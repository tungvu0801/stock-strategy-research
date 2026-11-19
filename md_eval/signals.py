"""md_eval/signals.py - signal sources. A source sees only a DecisionContext (the as_of view of the decision close, the session, a book snapshot)
and returns Signals; the engine sizes, limits and fills them.

SwingBridge runs the frozen V1 / C1 rules UNCHANGED: swing_pullback.core.SwingPullbackCore.evaluate on a strategy_validation Dataset built,
at every decision session d, from the view only:
  - bars = raw / split_factor with the view's REBASED factors (A6: the last visible bar's factor is exactly 1.0), so the absolute factor
    level never reaches the rules (P1 exception i) - the signal basis is split-adjusted (SIGNAL_BASIS_SPLIT_ADJUSTED, decision D2), not the
    all-adjusted series of the frozen evaluation;
  - aux series raw_close / raw_volume = the raw values; the declared volume feed SIP (CONSOLIDATED) or IEX (SINGLE_VENUE:IEX);
  - the issuer map from issuer_id, or from a static map passed as a run parameter (A4: STATIC_CLASSIFICATION_MAP, caps the verdict);
  - the universe: membership(universe_id, d) of the view, matched to the frozen RESEARCH_UNIVERSE by the symbol at d (A8).
Only the signals dated d are taken. Stops and targets come back in split-adjusted units and are converted to raw with the bar's rebased factor.
Exits follow the frozen levels and holding limit but fill at the next open (EXITS_NEXT_OPEN, A3): not comparable with the frozen execution.
The V1 universe is a fixed list of today's large caps, so every V1 / C1 run is FIXED_CURRENT_UNIVERSE whatever the provider declares."""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional

import swing_pullback as SP
from swing_pullback.core import SwingPullbackCore
from strategy_validation.dataset import Dataset
from strategy_validation.models import Bar

from md_eval import (EXITS_NEXT_OPEN, FIXED_CURRENT_UNIVERSE, NO_SIGNALS_SPLIT_VIEW_UNAVAILABLE, SIGNAL_BASIS_SPLIT_ADJUSTED, SPLIT_UNADJUSTED,
                     STATIC_CLASSIFICATION_MAP, SURVIVORSHIP_BIAS_PRESENT, SYMBOL_HISTORY_UNKNOWN)
from md_eval.contract import is_missing, licence_gate, symbol_at
import research_gate as RGATE


@dataclass(frozen=True)
class Signal:
    instrument_id: str
    score: float = 0.0
    raw_close: Optional[float] = None           # the decision session's raw close (sizing)
    stop_raw: Optional[float] = None
    target_raw: Optional[float] = None
    max_hold: Optional[int] = None
    weight: Optional[float] = None              # BENCHMARK sources: the fraction of equity
    notional: Optional[float] = None            # BENCHMARK sources: an explicit order notional


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def bar_at(view, instrument_id: str, session: str):
    rows = view.bars(instrument_id, session, session)
    return rows[0] if rows else None


class SignalSource:
    name = "SOURCE"
    role = "STRATEGY"

    def identity(self) -> dict:
        return {"name": self.name}

    def flags(self) -> list:
        return []

    def entries(self, ctx) -> List[Signal]:
        return []

    def exits(self, ctx) -> List[str]:
        return []


class SwingBridge(SignalSource):
    role = "STRATEGY"

    def __init__(self, use_pullback_rule: bool, universe_id: str, static_map: Optional[Dict[str, dict]] = None, split_unadjusted: bool = False):
        self.use_pullback_rule = bool(use_pullback_rule)
        self.name = SP.STRATEGY_ID if self.use_pullback_rule else SP.CONTROL_ID
        self.version = SP.STRATEGY_VERSION if self.use_pullback_rule else SP.CONTROL_VERSION
        self.universe_id = universe_id
        self.static_map = {k: dict(v) for k, v in (static_map or {}).items()}
        self.split_unadjusted = bool(split_unadjusted)
        self.core = SwingPullbackCore(self.use_pullback_rule)
        self.run_flags = {EXITS_NEXT_OPEN, FIXED_CURRENT_UNIVERSE, SURVIVORSHIP_BIAS_PRESENT, SP.FLAG_MODIFIED, SP.FLAG_NO_CLOSE_EXIT, SP.FLAG_EARNINGS,
                          SP.FLAG_LEVELS}
        if not self.use_pullback_rule:
            self.run_flags.add(SP.FLAG_CONTROL)
        self.run_flags.add(SPLIT_UNADJUSTED if self.split_unadjusted else SIGNAL_BASIS_SPLIT_ADJUSTED)
        self.emitted: List[tuple] = []

    def identity(self) -> dict:
        return {"name": self.name, "version": self.version, "core": SP.CORE_VERSION, "preregistration_hash": SP.preregistration_hash(),
                "universe_id": self.universe_id, "max_hold_bars": SP.MAX_HOLD_BARS,
                "static_map_sha256": _sha(self.static_map) if self.static_map else None}

    def flags(self) -> list:
        return sorted(self.run_flags)

    def _issuer(self, ins) -> Optional[str]:
        if not is_missing(ins.issuer_id) and ins.issuer_id:
            return str(ins.issuer_id)
        m = self.static_map.get(ins.instrument_id, {})
        if m.get("issuer_id"):
            self.run_flags.add(STATIC_CLASSIFICATION_MAP)
            return str(m["issuer_id"])
        return None

    def dataset(self, view, d: str):
        """(the strategy_validation Dataset of the view at d, {symbol: instrument_id}, {symbol: {session: rebased split factor}}) or None."""
        man = view.manifest()
        licence_gate(man)                                                    # MED-2: HISTORICAL only when CONFIRMED, even outside the engine
        RGATE.require_admitted_view(view)                                    # I-1B-1 (E7): only a view the admission gate issued
        views = tuple(man.get("price_views_available") or ())
        if "SPLIT_ADJUSTED" not in views and not self.split_unadjusted:
            self.run_flags.add(NO_SIGNALS_SPLIT_VIEW_UNAVAILABLE)
            return None
        feed = {"CONSOLIDATED": "SIP", "SINGLE_VENUE:IEX": "IEX"}.get(man.get("volume_feed"))
        if feed is None:
            raise ValueError(f"the V1 rules need a SIP (CONSOLIDATED) or IEX volume feed; got {man.get('volume_feed')!r}")
        members = set(view.membership(self.universe_id, d).instrument_ids)
        chosen: Dict[str, object] = {}
        for ins in sorted(view.instruments(), key=lambda i: i.instrument_id):
            sym, a8 = symbol_at(ins, d)
            if sym is None:
                continue
            if sym == SP.REGIME_SYMBOL or (sym in SP.RESEARCH_UNIVERSE and ins.instrument_id in members):
                if a8:
                    self.run_flags.add(SYMBOL_HISTORY_UNKNOWN)
                if sym in chosen:
                    raise ValueError(f"two instruments carry {sym} at {d}")
                chosen[sym] = ins
        if SP.REGIME_SYMBOL not in chosen:
            return None
        start = man["start_session"]
        bars, raw_close, raw_volume, factors, iid_of = {}, {}, {}, {}, {}
        issuers: Dict[str, dict] = {}
        for sym, ins in chosen.items():
            rows = view.bars(ins.instrument_id, start, d)
            out, fac = [], {}
            for b in rows:
                prices = (b.raw_open, b.raw_high, b.raw_low, b.raw_close)
                if not all(_num(x) for x in prices):
                    out = None                                       # integrity-only: no signals for this instrument
                    break
                sf = 1.0 if self.split_unadjusted else b.split_factor
                if not _num(sf):
                    out = None
                    break
                vol = b.raw_volume if _num(b.raw_volume) else 0.0
                out.append(Bar(date=b.session, open=b.raw_open / sf, high=b.raw_high / sf, low=b.raw_low / sf, close=b.raw_close / sf, volume=vol))
                raw_close.setdefault(sym, {})[b.session] = b.raw_close
                if _num(b.raw_volume):
                    raw_volume.setdefault(sym, {})[b.session] = b.raw_volume
                fac[b.session] = sf
            if not out:
                raw_close.pop(sym, None)
                raw_volume.pop(sym, None)
                continue
            bars[sym], factors[sym], iid_of[sym] = out, fac, ins.instrument_id
            if sym != SP.REGIME_SYMBOL:
                iss = self._issuer(ins)
                if iss is None:
                    raise ValueError(f"{ins.instrument_id}: issuer_id UNKNOWN and no static map entry: one listing per issuer cannot be applied")
                issuers.setdefault(iss, {"symbols": []})["symbols"].append(sym)
        if SP.REGIME_SYMBOL not in bars or len({b.date for rows in bars.values() for b in rows}) < SP.WARMUP_SESSIONS:
            return None
        ds = Dataset(bars, kind=man["kind"], source="md_eval.view", provenance={"volume_feed": feed},
                     aux={"series": {"raw_close": raw_close, "raw_volume": raw_volume}, "static": {"issuers": issuers}})
        return ds, iid_of, factors

    def entries(self, ctx) -> List[Signal]:
        licence_gate(ctx.view.manifest())                                    # MED-2: the shared gate, on the view's licence (A11 allow-list)
        RGATE.require_admitted_view(ctx.view)                                # I-1B-1 (E7): only a view the admission gate issued
        built = self.dataset(ctx.view, ctx.session)
        if built is None:
            return []
        ds, iid_of, factors = built
        out = []
        for sg in self.core.evaluate(ds)["signals"]:
            if sg["signal_date"] != ctx.session:
                continue
            f = factors[sg["symbol"]][ctx.session]
            out.append(Signal(instrument_id=iid_of[sg["symbol"]], score=SP.SCORE, raw_close=sg["raw_close"], stop_raw=sg["stop"] * f,
                              target_raw=sg["target"] * f, max_hold=SP.MAX_HOLD_BARS))
        out.sort(key=lambda s: s.instrument_id)
        self.emitted.extend((ctx.session, s) for s in out)                     # (decision session, signal): a record, never read back
        return out


class RandomEntry(SignalSource):
    """The random-entry control (v4 B3): at each session, k = V1's signal count there, drawn without replacement from C1's signals there
    (V1's filters without its pullback rule), with V1's exits from those signals' levels; the same book and costs. The seed is recorded; the
    draw for a session depends only on (seed, session). Report only: nothing here feeds back into V1 (decision D6)."""
    role = "STRATEGY"

    def __init__(self, v1: SwingBridge, c1: SwingBridge, seed: int):
        if not v1.use_pullback_rule or c1.use_pullback_rule:
            raise ValueError("RandomEntry needs V1 (the count) and C1 (the pool)")
        self.v1, self.c1, self.seed = v1, c1, int(seed)
        self.name = f"RANDOM_ENTRY_CONTROL:{self.seed}"

    def identity(self) -> dict:
        return {"name": "RANDOM_ENTRY_CONTROL", "seed": self.seed, "count_from": self.v1.identity(), "pool_from": self.c1.identity()}

    def flags(self) -> list:
        return sorted(set(self.v1.flags()) | set(self.c1.flags()) | {"RANDOM_ENTRY_CONTROL_REPORT_ONLY"})

    def entries(self, ctx) -> List[Signal]:
        licence_gate(ctx.view.manifest())                                    # I-1B-1 (E7): the licence gate first, then the admitted view
        RGATE.require_admitted_view(ctx.view)
        k = len(self.v1.entries(ctx))
        pool = sorted(self.c1.entries(ctx), key=lambda s: s.instrument_id)
        if not k or not pool:
            return []
        rng = random.Random(int.from_bytes(hashlib.sha256(f"{self.seed}|{ctx.session}".encode("utf-8")).digest()[:8], "big"))
        picks = rng.sample(pool, min(k, len(pool)))
        return [replace(s, score=float(len(picks) - j)) for j, s in enumerate(picks)]


class BuyAndHold(SignalSource):
    """A BENCHMARK source: the whole equity into one instrument at the first session it has a bar, held to the end."""
    role = "BENCHMARK"

    def __init__(self, instrument_id: str):
        self.instrument_id, self.done = instrument_id, False
        self.name = f"BUY_AND_HOLD:{instrument_id}"

    def identity(self) -> dict:
        return {"name": "BUY_AND_HOLD", "instrument_id": self.instrument_id}

    def entries(self, ctx) -> List[Signal]:
        licence_gate(ctx.view.manifest())
        RGATE.require_admitted_view(ctx.view)                                # I-1B-1 (E7)
        if self.done:
            return []
        b = bar_at(ctx.view, self.instrument_id, ctx.session)
        if b is None or not _num(b.raw_close):
            return []
        self.done = True
        return [Signal(self.instrument_id, raw_close=b.raw_close, weight=1.0)]


class EqualWeight(SignalSource):
    """B4, a BENCHMARK source: equal notional across the universe members with a bar at the first session, held to the end (no rebalancing)."""
    role = "BENCHMARK"

    def __init__(self, universe_id: str):
        self.universe_id, self.done = universe_id, False
        self.name = f"EQUAL_WEIGHT:{universe_id}"

    def identity(self) -> dict:
        return {"name": "EQUAL_WEIGHT", "universe_id": self.universe_id}

    def flags(self) -> list:
        return []

    def entries(self, ctx) -> List[Signal]:
        licence_gate(ctx.view.manifest())
        RGATE.require_admitted_view(ctx.view)                                # I-1B-1 (E7)
        if self.done:
            return []
        ids = []
        for iid in ctx.view.membership(self.universe_id, ctx.session).instrument_ids:
            b = bar_at(ctx.view, iid, ctx.session)
            if b is not None and _num(b.raw_close):
                ids.append((iid, b.raw_close))
        if not ids:
            return []
        self.done = True
        return [Signal(iid, raw_close=c, weight=1.0 / len(ids)) for iid, c in sorted(ids)]


def _sha(obj) -> str:
    import json
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
