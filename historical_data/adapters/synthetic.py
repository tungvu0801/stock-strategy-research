"""
historical_data/adapters/synthetic.py - SyntheticProvider: a dataset from rows given in code (fixtures). kind SYNTHETIC by default (exempt from
the licence rule, section 6.2); a fixture may declare kind HISTORICAL only with the UNKNOWN licence every adapter emits (A10, QA L-1); known_at of every bar is close_utc(session) unless a fixture states another one on purpose (a negative test).
A row that breaks section 3.2 is removed and becomes a REMOVED_INVALID gap, as in every adapter.
"""
from __future__ import annotations

from typing import Iterable, Optional

from historical_data import calendar as CAL
from historical_data.provider import _TableProvider
from historical_data.schema import (BarInvalid, DailyBar, Instrument, MembershipChange, SymbolSpan, licence_unknown)
from historical_data.status import EARNINGS_DATA_UNAVAILABLE, unavailable, unknown

ADAPTER, ADAPTER_VERSION, PROVIDER = "SyntheticProvider", "synthetic_v1", "SYNTHETIC"
SUPPORTED_CONTRACT_VERSIONS = ("historical_data_contract_v2",)


def instrument(symbol: str, first_session: str, *, prefix: str = "SYNTH", history=None, status: str = "UNKNOWN", asset_type="COMMON_STOCK",
               listing=None, sector=None, issuer_id=None) -> Instrument:
    return Instrument(instrument_id=f"{prefix}:{symbol}:{first_session}",
                      symbol_history=tuple(history) if history else (SymbolSpan(symbol, first_session, None),), symbol_history_status=status,
                      asset_type=asset_type, listing=listing if listing is not None else unknown("NOT_IN_SOURCE"),
                      sector=sector if sector is not None else unknown("NOT_IN_SOURCE"),
                      issuer_id=issuer_id if issuer_id is not None else unknown("NOT_IN_SOURCE"))


def bar(instrument_id: str, session: str, o, h, l, c, v, *, split_factor=1.0, total_return_factor=1.0, volume_feed="CONSOLIDATED",
        price_source="CONSOLIDATED", known_at=None, source_ref=None) -> DailyBar:
    return DailyBar(instrument_id=instrument_id, session=session, raw_open=o, raw_high=h, raw_low=l, raw_close=c, raw_volume=v,
                    split_factor=split_factor, total_return_factor=total_return_factor, volume_feed=volume_feed, price_source=price_source,
                    known_at=known_at if known_at is not None else CAL.close_utc(session),
                    source_ref=source_ref if source_ref is not None else unavailable("SYNTHETIC_ROW"))


class SyntheticProvider(_TableProvider):
    def __init__(self, *, instruments: Iterable[Instrument], bars: Iterable = (), rows: Iterable[dict] = (), events=(), membership=None,
                 scheduled_events=EARNINGS_DATA_UNAVAILABLE, events_completeness: str = "NONE", membership_basis: str = "FIXED_CURRENT_UNIVERSE",
                 price_views=("RAW_CLOSE", "RAW_OHL", "SPLIT_ADJUSTED", "TOTAL_RETURN"), volume_feed: str = "CONSOLIDATED",
                 price_source: str = "CONSOLIDATED", kind: str = "SYNTHETIC", licence=None, dataset_id: str = "synthetic:fixture",
                 universe_id: str = "SYNTHETIC_UNIVERSE", adjustment_as_of=None, data_exposure=None):
        use = licence.personal_research_use if licence is not None else "UNKNOWN"
        if use != "UNKNOWN" and (kind == "HISTORICAL" or use != "NOT_PERMITTED"):     # i.e. any confirmed licence, and any non-UNKNOWN HISTORICAL one
            raise ValueError("contract v2 has no licence-file loader (A10): no dataset can be CONFIRMED, and a HISTORICAL dataset's licence is "
                             "UNKNOWN (QA L-1)")
        built, removed = list(bars), []
        for r in rows:                                                      # dict rows: invalid ones are removed (REMOVED_INVALID), never repaired
            try:
                built.append(bar(**r))
            except BarInvalid:
                removed.append((r["instrument_id"], r["session"]))
        insts = list(instruments)
        if membership is None:
            first = {}
            for b in built:
                first[b.instrument_id] = min(first.get(b.instrument_id, b.session), b.session)
            membership = [MembershipChange(universe_id, i, s, "ADD", unknown("FIXED_CURRENT_UNIVERSE")) for i, s in sorted(first.items())]
        sessions = sorted({b.session for b in built})
        super().__init__(
            manifest_fields=dict(dataset_id=dataset_id, kind=kind, provider=PROVIDER, adapter=ADAPTER, adapter_version=ADAPTER_VERSION, source_files=(),
                                 price_views_available=tuple(price_views), volume_feed=volume_feed, price_source=price_source,
                                 membership_basis=membership_basis, events_completeness=events_completeness,
                                 adjustment_as_of=adjustment_as_of if adjustment_as_of is not None else (sessions[-1] if sessions else unavailable("NO_BARS")),
                                 retrieved_at=unavailable("SYNTHETIC"), licence=licence if licence is not None else licence_unknown(),
                                 data_exposure=data_exposure if data_exposure is not None else unknown("NO_EXPOSURE_RECORD")),
            instruments=insts, bars=built, events=events, membership=membership, scheduled_events=scheduled_events, removed=removed,
            notes=[{"removed_invalid": [list(x) for x in removed]}] if removed else ())
