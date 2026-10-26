"""momentum/data.py — the price interface the Momentum core reads, built from a validation Dataset (the bars are all-adjusted daily bars; `aux`
carries the raw series and today's issuer map). A future live evaluator would provide the same methods from live data. Momentum needs no
fundamentals.

    sessions                          the dataset's trading sessions (ascending ISO dates); position(T) = T's index among them
    adj_close(symbol, date)           the all-adjusted close of that session, or None when the symbol has no bar on it (never filled)
    first_date(symbol)                the symbol's first bar date, or None
    raw_close(symbol, T)              the unadjusted close of session T (the price filter)
    dollar_volumes(symbol, T, n)      raw close x raw volume of the symbol's last n sessions on / before T
    issuer(symbol)                    the issuer id (SEC CIK) of the symbol, or the symbol itself when no issuer is known"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, List, Optional


class PriceData:
    def __init__(self, dataset):
        self.sessions: List[str] = list(dataset.sessions)
        self._pos = {d: i for i, d in enumerate(self.sessions)}
        self._adj: Dict[str, Dict[str, float]] = {s: {b.date: b.close for b in rows} for s, rows in dataset.bars.items()}
        series = (dataset.aux or {}).get("series") or {}
        self._raw: Dict[str, Dict[str, float]] = series.get("raw_close") or {}
        self._vol: Dict[str, Dict[str, float]] = series.get("raw_volume") or {}
        self._raw_dates = {s: sorted(m) for s, m in self._raw.items()}
        self._issuer = {sym: cik for cik, info in (((dataset.aux or {}).get("static") or {}).get("issuers") or {}).items() for sym in info.get("symbols") or []}

    def position(self, T: str) -> int:
        return self._pos[T]

    def adj_close(self, symbol: str, d: str) -> Optional[float]:
        return (self._adj.get(symbol) or {}).get(d)

    def first_date(self, symbol: str) -> Optional[str]:
        m = self._adj.get(symbol) or {}
        return min(m) if m else None

    def raw_close(self, symbol: str, T: str) -> Optional[float]:
        return (self._raw.get(symbol) or {}).get(T)

    def dollar_volumes(self, symbol: str, T: str, n: int) -> List[float]:
        ds = self._raw_dates.get(symbol) or []
        i = bisect_right(ds, T)
        c, v = self._raw.get(symbol) or {}, self._vol.get(symbol) or {}
        return [c[d] * v.get(d, 0.0) for d in ds[max(0, i - n):i]]

    def issuer(self, symbol: str) -> str:
        return self._issuer.get(symbol) or symbol
