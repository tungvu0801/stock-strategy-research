"""value_quality/data.py — the data interface the core reads, built from a validation Dataset's point-in-time auxiliary data
(value_quality/ingest.py). A future live evaluator would provide the same five methods from live data.

    raw_close(symbol, T)               the unadjusted close of session T (price filter, market cap)
    dollar_volumes(symbol, T, n)       raw close x volume of the last n sessions on / before T
    split_ratio(symbol)                {session: raw close / split-adjusted close} (re-bases share counts across splits)
    facts(symbol, field)               the issuer's SEC XBRL rows for a field (net_income, operating_cash_flow, assets, equity, share_count): every
                                       concept of value_quality.CONCEPTS[field] with its priority and name, annotated with available_at (pit.py's rule);
                                       a share count below MIN_SHARE_COUNT (an XBRL scale error) is dropped, never rescaled
    issuer(symbol)                     {cik, name, sic, sic_description} - today's SEC classification (not point in time; documented)"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, List, Optional

from value_quality import CONCEPTS, MIN_SHARE_COUNT, pit


class BundleData:
    def __init__(self, dataset):
        aux = dataset.aux or {}
        series = aux.get("series") or {}
        self._close: Dict[str, Dict[str, float]] = series.get("raw_close") or {}
        self._volume: Dict[str, Dict[str, float]] = series.get("raw_volume") or {}
        self._split: Dict[str, Dict[str, float]] = series.get("split_ratio") or {}
        self._dates = {s: sorted(m) for s, m in self._close.items()}
        self.calendar = pit.Calendar(dataset.sessions)
        self._issuer: Dict[str, dict] = {}
        for cik, info in ((aux.get("static") or {}).get("issuers") or {}).items():
            for sym in info.get("symbols") or []:
                self._issuer[sym] = {"cik": cik, "name": info.get("name"), "sic": info.get("sic"), "sic_description": info.get("sic_description")}
        facts = (aux.get("events") or {}).get("facts") or {}
        self._facts: Dict[str, Dict[str, List[dict]]] = {}
        for cik, tags in facts.items():
            merged = {}
            for field, concepts in CONCEPTS.items():
                rows = []
                for prio, concept in enumerate(concepts):
                    for r in tags.get(concept) or []:
                        if field == "share_count" and float(r["val"]) < MIN_SHARE_COUNT:
                            continue
                        a = self.calendar.available_at(str(r["filed"]))
                        if a is not None:
                            rows.append({**r, "available_at": a, "concept": concept, "priority": prio})
                merged[field] = rows
            self._facts[cik] = merged

    def raw_close(self, symbol: str, T: str) -> Optional[float]:
        return (self._close.get(symbol) or {}).get(T)

    def dollar_volumes(self, symbol: str, T: str, n: int) -> List[float]:
        ds = self._dates.get(symbol) or []
        i = bisect_right(ds, T)
        c, v = self._close.get(symbol) or {}, self._volume.get(symbol) or {}
        return [c[d] * v.get(d, 0.0) for d in ds[max(0, i - n):i]]

    def split_ratio(self, symbol: str) -> Dict[str, float]:
        return self._split.get(symbol) or {}

    def facts(self, symbol: str, field: str) -> List[dict]:
        iss = self._issuer.get(symbol)
        return ((self._facts.get(iss["cik"]) or {}).get(field) or []) if iss else []

    def issuer(self, symbol: str) -> Optional[dict]:
        return self._issuer.get(symbol)
