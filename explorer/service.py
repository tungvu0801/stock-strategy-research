"""
explorer/service.py - the explorer's read-only market-data reads (Phase A0).

One ExplorerService holds its OWN market-data adapter (trader_view.adapters.build_market_adapter: the simulator, or the existing read-only
Alpaca market-data client). It is a separate instance from the Trader View stream's adapter: the explorer never obtains, calls or imports the
stream, so nothing here can subscribe a ticker, evaluate a setup or a proposal, or reach the executor. No database, no file write, no POST.
Every read checks the feature flag first: while it is not set, ExplorerDisabled is raised and the adapter is not touched.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from api.validation import validate_symbol
from explorer import DEFAULT_CANDLES, MAX_CANDLES, READ_ONLY_NOTE, VERSION, ExplorerDisabled, enabled
from trader_view.adapters import MarketUnavailable, UnknownSymbol, build_market_adapter


class ExplorerService:
    def __init__(self, market=None):
        self._market = market                                   # tests pass an adapter; otherwise built lazily on first use
        self._lock = threading.Lock()

    @property
    def market(self):
        if self._market is None:
            with self._lock:
                if self._market is None:
                    self._market = build_market_adapter()
        return self._market

    def _quote(self, raw: str) -> Tuple[str, Any]:
        """(normalised symbol, quote). ExplorerDisabled = the flag is off; ValueError = malformed; UnknownSymbol = the feed answered without
        data; MarketUnavailable = the feed did not answer (the adapter reports that as a FEED_ERROR quote, never as an exception)."""
        if not enabled():
            raise ExplorerDisabled("The Market Explorer is off.")
        m = self.market
        s = m.resolve(validate_symbol(raw))
        q = m.quote(s)
        if q.status != "OK" and q.code == "FEED_ERROR":
            raise MarketUnavailable(q.message or "The market-data feed did not answer.")
        return s, q

    def resolve(self, raw: str) -> str:
        return self._quote(raw)[0]

    def symbol(self, raw: str, candles: int = DEFAULT_CANDLES) -> Dict[str, Any]:
        """Quote and minute candles of one symbol, for a chart. Market data only."""
        s, q = self._quote(raw)
        n = max(0, min(int(candles), MAX_CANDLES))
        m = self.market
        return {"version": VERSION, "symbol": s, "quote": q.model_dump(), "candles": [c.model_dump() for c in m.candles(s, n)] if n else [],
                "source": m.source, "mode": m.mode, "read_only": True, "note": READ_ONLY_NOTE}

    def search(self, raw: str) -> Dict[str, Any]:
        """Phase A0 search: an exact-ticker lookup against the data feed (no directory yet). An unknown ticker is an empty result, not an error."""
        text = str(raw or "").strip()
        try:
            s, q = self._quote(text)
        except UnknownSymbol:
            return {"version": VERSION, "query": text.upper()[:12], "results": [], "match": "EXACT_TICKER", "read_only": True, "note": READ_ONLY_NOTE}
        return {"version": VERSION, "query": s, "results": [{"symbol": s, "data_available": q.status == "OK"}], "match": "EXACT_TICKER", "read_only": True,
                "note": READ_ONLY_NOTE}


_SERVICE: Dict[str, Optional[ExplorerService]] = {"instance": None}       # tests substitute an instance built on a test adapter


def service() -> ExplorerService:
    if _SERVICE["instance"] is None:
        _SERVICE["instance"] = ExplorerService()
    return _SERVICE["instance"]


__all__ = ["ExplorerService", "MarketUnavailable", "UnknownSymbol", "service"]
