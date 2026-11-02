"""swing_pullback/data.py — what the swing core reads from a validation Dataset, and the dataset declarations it refuses to guess.

  sessions / position(d)       the dataset's sessions (ascending) and a session's index among them
  bars(symbol)                 the symbol's all-adjusted daily bars (the simulator's prices), oldest first
  raw_close(symbol, d)         aux series "raw_close": the unadjusted close of that session, or None (never filled)
  raw_volume(symbol, d)        aux series "raw_volume": the unadjusted volume of that session, or None
  issuer(symbol)               aux static "issuers" (an issuer id -> {"symbols": [...]}): the symbol's issuer
  volume_feed                  the dataset's DECLARED volume feed (provenance "volume_feed"): SIP or IEX

A dataset that does not declare its volume feed as SIP or IEX, or whose issuer map does not cover every universe symbol it holds, is refused
(SwingDataError): the liquidity rule and the one-listing-per-issuer rule are never decided by a default."""
from __future__ import annotations

from typing import Dict, List, Optional

from swing_pullback import RESEARCH_UNIVERSE, VOLUME_FEEDS


class SwingDataError(ValueError):
    pass


class SwingData:
    def __init__(self, dataset):
        self.sessions: List[str] = list(dataset.sessions)
        self._pos = {d: i for i, d in enumerate(self.sessions)}
        self._bars = dataset.bars
        series = (dataset.aux or {}).get("series") or {}
        self._raw: Dict[str, Dict[str, float]] = series.get("raw_close") or {}
        self._vol: Dict[str, Dict[str, float]] = series.get("raw_volume") or {}
        feed = (dataset.declared or {}).get("volume_feed")
        if feed not in VOLUME_FEEDS:
            raise SwingDataError(f"the dataset must declare its volume feed (provenance volume_feed: {' or '.join(VOLUME_FEEDS)}); got {feed!r}")
        self.volume_feed: str = feed
        issuers = (((dataset.aux or {}).get("static") or {}).get("issuers")) or {}
        self._issuer = {sym: str(iid) for iid, info in issuers.items() for sym in (info or {}).get("symbols") or []}
        missing = sorted(s for s in RESEARCH_UNIVERSE if s in self._bars and s not in self._issuer)
        if missing:
            raise SwingDataError(f"the issuer map (aux static issuers) does not cover {', '.join(missing)}: one listing per issuer cannot be applied")

    def position(self, d: str) -> Optional[int]:
        return self._pos.get(d)

    def bars(self, symbol: str) -> list:
        return self._bars.get(symbol) or []

    def raw_close(self, symbol: str, d: str) -> Optional[float]:
        return (self._raw.get(symbol) or {}).get(d)

    def raw_volume(self, symbol: str, d: str) -> Optional[float]:
        return (self._vol.get(symbol) or {}).get(d)

    def issuer(self, symbol: str) -> str:
        return self._issuer[symbol]
