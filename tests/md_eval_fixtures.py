"""Synthetic, contract-shaped fixtures for the MD-B tests (md_eval). No file, no network, no database: every value is built here.

v1_double()      260 weekday sessions; AAPL (a 2-for-1 split at index 150, known 10 sessions before), MSFT, SPY; a 12-session cycle (nine
                 rises of 1.2 %, three falls of 0.6 %) with a tiny deterministic wobble (no exact ties), so V1's three-lower-closes trigger
                 fires once a cycle after the 200-session warm-up and C1 (no pullback rule) fires more often
simple_double()  a few instruments with explicit (open, close) per session index, for the ledger and book tests
ScriptedSource   signals and exits given per session index (the ledger, book and look-ahead control tests)"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from md_eval import CONTRACT_V2, CONTRACT_V2_SHA256
from md_eval.contract import CorporateEvent, DailyBar, DatasetManifest, Instrument
from md_eval.costs import ZERO_COST_BUCKETS, CostModel
from md_eval.double import ContractDouble, weekday_sessions
from md_eval.engine import RunConfig
from md_eval.signals import Signal, SignalSource

START = "2024-01-01"
N_V1 = 260
SPLIT_INDEX = 150
AAPL, MSFT, SPY = "SIM:AAPL:2024-01-01", "SIM:MSFT:2024-01-01", "SIM:SPY:2024-01-01"
UNIVERSE = "U-FIXED"


def manifest(sessions, n_instruments, *, kind="SYNTHETIC", licence=None, completeness="COMPLETE", basis="FIXED_CURRENT_UNIVERSE",
             views=("RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN"), volume_feed="CONSOLIDATED", version=CONTRACT_V2, sha=CONTRACT_V2_SHA256):
    return DatasetManifest(dataset_id="md-b-synthetic", contract_version=version, contract_sha256=sha, kind=kind, provider="md_eval.double",
                           adapter="ContractDouble", adapter_version="1", start_session=sessions[0], end_session=sessions[-1],
                           instrument_count=n_instruments, content_sha256="c" * 64, price_views_available=tuple(views), volume_feed=volume_feed,
                           price_source="CONSOLIDATED", membership_basis=basis, events_completeness=completeness,
                           licence=dict(licence or {"licence_ref": "synthetic", "personal_research_use": "UNKNOWN", "redistribution": "NONE"}))


def instrument(iid, symbol, first, sector="TECH", issuer=None, asset_type="COMMON_STOCK", history_status="KNOWN"):
    kw = {"sector": sector} if sector is not None else {}
    if issuer is not None:
        kw["issuer_id"] = issuer
    entry = {"symbol": symbol, "valid_from_session": first, "valid_to_session": None} if history_status == "KNOWN" else {"symbol": symbol}
    return Instrument(instrument_id=iid, symbol_history=(entry,), symbol_history_status=history_status, asset_type=asset_type, **kw)


def _cycle_closes(n: int, phase: int, base: float, wobble: float) -> List[float]:
    out, c = [], base
    for k in range(n):
        c *= 1.012 if (k + phase) % 12 < 9 else 0.994
        out.append(c * (1.0 + 1e-4 * math.sin(1.7 * k + wobble)))
    return out


def _bars_from_adjusted(iid, sessions, adj, split_index=None, volume=1_000_000.0):
    bars = []
    for k, s in enumerate(sessions):
        sf = 2.0 if (split_index is not None and k < split_index) else 1.0
        o_adj = adj[k - 1] if k else adj[k]
        c_adj = adj[k]
        o, c = o_adj * sf, c_adj * sf
        bars.append(DailyBar(instrument_id=iid, session=s, raw_open=o, raw_high=max(o, c) * 1.005, raw_low=min(o, c) * 0.995, raw_close=c,
                             raw_volume=volume, split_factor=sf, total_return_factor=sf))
    return bars


def v1_double(unknown_history=(), **manifest_kw) -> ContractDouble:
    sessions = weekday_sessions(START, N_V1)
    bars = (_bars_from_adjusted(AAPL, sessions, _cycle_closes(N_V1, 0, 100.0, 0.3), split_index=SPLIT_INDEX)
            + _bars_from_adjusted(MSFT, sessions, _cycle_closes(N_V1, 5, 80.0, 1.1))
            + _bars_from_adjusted(SPY, sessions, _cycle_closes(N_V1, 3, 300.0, 2.2), volume=50_000_000.0))
    st = lambda iid: "UNKNOWN" if iid in unknown_history else "KNOWN"  # noqa: E731
    ins = [instrument(AAPL, "AAPL", sessions[0], "TECH", "SEC:0000320193", history_status=st(AAPL)),
           instrument(MSFT, "MSFT", sessions[0], "TECH", "SEC:0000789019", history_status=st(MSFT)),
           instrument(SPY, "SPY", sessions[0], None, None, "ETF", history_status=st(SPY))]
    split = CorporateEvent(event_id="E-AAPL-SPLIT", instrument_id=AAPL, type="SPLIT", effective_session=sessions[SPLIT_INDEX],
                           known_at=sessions[SPLIT_INDEX - 10] + "T21:00:00Z", ratio=2.0)
    return ContractDouble(sessions=sessions, instruments=ins, bars=bars, events=[split], manifest=manifest(sessions, 3, **manifest_kw),
                          membership={UNIVERSE: {"basis": "FIXED_CURRENT_UNIVERSE", "members": (AAPL, MSFT)}})


def v1_config(d: ContractDouble, **kw) -> RunConfig:
    s = d.sessions("0000-00-00", "9999-99-99")
    return RunConfig(start_session=s[0], end_session=s[-1], **kw)


def zero_costs() -> CostModel:
    return CostModel(buckets=ZERO_COST_BUCKETS, fees=False, label="ZERO_COST")


def simple_double(prices: Dict[str, List[Optional[tuple]]], *, n: int, events=(), sectors=None, completeness="COMPLETE", kind="SYNTHETIC",
                  licence=None, factors: Dict[str, List[tuple]] = None, membership=None, basis="FIXED_CURRENT_UNIVERSE", volume=1_000_000.0,
                  views=("RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN")):
    """prices[iid][k] = (open, close) or None (no bar). factors[iid][k] = (split_factor, total_return_factor)."""
    sessions = weekday_sessions(START, n)
    bars = []
    for iid, rows in prices.items():
        for k, oc in enumerate(rows):
            if oc is None:
                continue
            o, c = oc
            sf, tr = (factors or {}).get(iid, [(1.0, 1.0)] * n)[k]
            bars.append(DailyBar(instrument_id=iid, session=sessions[k], raw_open=o, raw_high=max(o, c), raw_low=min(o, c), raw_close=c,
                                 raw_volume=volume, split_factor=sf, total_return_factor=tr))
    ins = [instrument(iid, iid.split(":")[-1], sessions[0], (sectors or {}).get(iid, "SECTOR_" + iid.split(":")[-1]), "SEC:" + iid.split(":")[-1])
           for iid in prices]
    mem = membership or {UNIVERSE: {"basis": "FIXED_CURRENT_UNIVERSE", "members": tuple(prices)}}
    return ContractDouble(sessions=sessions, instruments=ins, bars=bars, events=list(events),
                          manifest=manifest(sessions, len(prices), kind=kind, licence=licence, completeness=completeness, basis=basis, views=views),
                          membership=mem)


def flat(n: int, price: float = 100.0, missing=()):
    return [None if k in missing else (price, price) for k in range(n)]


class ScriptedSource(SignalSource):
    def __init__(self, sessions, entries=None, exits=None, role="BENCHMARK", name="SCRIPTED", flags=(), identity_extra=None):
        self.idx = {s: i for i, s in enumerate(sessions)}
        self.entries_by, self.exits_by, self.role, self.name = dict(entries or {}), dict(exits or {}), role, name
        self.contexts, self._flags, self._extra = [], list(flags), dict(identity_extra or {})

    def identity(self) -> dict:
        return {"name": self.name, **self._extra}

    def flags(self) -> list:
        return list(self._flags)

    def entries(self, ctx) -> list:
        self.contexts.append(ctx)
        return list(self.entries_by.get(self.idx[ctx.session], []))

    def exits(self, ctx) -> list:
        return list(self.exits_by.get(self.idx[ctx.session], []))


def ev(event_id, iid, typ, sessions, k, known_k=0, **kw) -> CorporateEvent:
    known = kw.pop("known_at", sessions[known_k] + "T21:00:00Z")
    return CorporateEvent(event_id=event_id, instrument_id=iid, type=typ, effective_session=sessions[k], known_at=known, **kw)


def sig(iid, **kw) -> Signal:
    return Signal(instrument_id=iid, **kw)


def decisions_until(result, session):
    return [d for d in result["decisions"] if d["session"] <= session]
