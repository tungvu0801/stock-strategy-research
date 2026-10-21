"""
Deterministic simulated market — the explicit development fallback (TRADER_DATA_MODE=simulated, or auto without market-data
credentials). Any valid symbol works: each gets its own seeded random walk, so the same seed always produces the same candles.
Clearly labelled SIMULATED everywhere; never a market feed.
"""
from __future__ import annotations

import hashlib
import math
import random
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from api.validation import validate_symbol
from trader_view.models import Candle, Quote, Tradability

START_PRICES = {"AAPL": 190.0, "NVDA": 120.0, "SNDK": 1400.0}
SIGMA_PER_SECOND = 0.00035
SOURCE = "simulator (deterministic, seeded) · NOT a market feed"


def start_price(symbol: str) -> float:
    if symbol in START_PRICES:
        return START_PRICES[symbol]
    h = int(hashlib.sha256(symbol.encode("ascii")).hexdigest()[:8], 16)
    return round(15 + (h % 98500) / 100, 2)                                  # 15.00 .. 1000.00, stable per symbol


class _Stream:
    __slots__ = ("symbol", "rng", "price", "prev_close", "history", "live", "last_ts", "volume_base")

    def __init__(self, symbol: str, seed, start_ts: int, backfill_minutes: int):
        self.symbol = symbol
        self.rng = random.Random(f"{seed}:{symbol}")
        self.price = start_price(symbol)
        self.history: deque = deque(maxlen=2000)
        self.live: Optional[dict] = None
        self.last_ts = int(start_ts)
        self.volume_base = 500 + self.rng.randrange(0, 3000)
        self._backfill(self.last_ts, backfill_minutes)
        self.prev_close = self.history[0]["open"] if self.history else self.price   # session reference for % change

    def _move(self, seconds: float) -> None:
        factor = math.exp(self.rng.gauss(0.0, SIGMA_PER_SECOND * math.sqrt(max(seconds, 1.0))))
        self.price = round(max(0.01, self.price * factor), 2)

    def _backfill(self, start_ts: int, minutes: int) -> None:
        first = (start_ts - start_ts % 60) - minutes * 60
        for i in range(minutes):
            t = first + i * 60
            o = self.price
            hi = lo = o
            vol = 0
            for _ in range(6):                                                # six 10-second moves per minute
                self._move(10)
                hi, lo = max(hi, self.price), min(lo, self.price)
                vol += self.volume_base // 6 + self.rng.randrange(0, self.volume_base // 3 + 1)
            self.history.append({"time": t, "open": o, "high": hi, "low": lo, "close": self.price, "volume": float(vol), "final": True})

    def step(self, now_ts: int) -> None:
        if now_ts <= self.last_ts:
            return                                                            # same or earlier second: nothing changes
        self._move(now_ts - self.last_ts)
        minute = now_ts - now_ts % 60
        if self.live is None or self.live["time"] != minute:
            if self.live is not None:
                self.live["final"] = True
                self.history.append(self.live)
            self.live = {"time": minute, "open": self.price, "high": self.price, "low": self.price, "close": self.price, "volume": 0.0, "final": False}
        else:
            self.live["high"] = max(self.live["high"], self.price)
            self.live["low"] = min(self.live["low"], self.price)
            self.live["close"] = self.price
        self.live["volume"] += float(self.volume_base // 60 + self.rng.randrange(0, 50))
        self.last_ts = now_ts

    def candles(self, limit: int) -> List[Candle]:
        rows = list(self.history) + ([self.live] if self.live is not None else [])
        if limit and limit > 0:
            rows = rows[-limit:]
        return [Candle(**r) for r in rows]

    def quote(self) -> Quote:
        p = self.price
        return Quote(symbol=self.symbol, status="OK", price=p, bid=round(p * (1 - 0.0002), 2), ask=round(p * (1 + 0.0002), 2),
                     prev_close=self.prev_close, change_pct=round((p / self.prev_close - 1) * 100, 3) if self.prev_close else None,
                     volume=float(sum(c["volume"] for c in self.history) + (self.live["volume"] if self.live else 0.0)),
                     ts=self.last_ts, source=SOURCE)


class SimulatedMarket:
    mode = "simulated"
    source = SOURCE

    def __init__(self, seed=7, start_time: Optional[int] = None, backfill_minutes: int = 240, symbols: Iterable[str] = ()):
        self.seed = seed
        self.clock = int(start_time if start_time is not None else time.time())
        self.backfill_minutes = int(backfill_minutes)
        self._streams: Dict[str, _Stream] = {}
        for s in symbols:
            self._ensure(validate_symbol(s))

    # ---------------------------------------------------------------- symbols ------------------------------------------------
    def _ensure(self, symbol: str) -> _Stream:
        st = self._streams.get(symbol)
        if st is None:
            st = self._streams[symbol] = _Stream(symbol, self.seed, self.clock, self.backfill_minutes)
        return st

    def resolve(self, symbol: str) -> str:
        s = validate_symbol(symbol)                                           # ValueError for malformed input
        self._ensure(s)
        return s

    def symbols(self) -> List[str]:
        return sorted(self._streams)

    def has(self, symbol: str) -> bool:
        return symbol.upper() in self._streams

    # ---------------------------------------------------------------- data ---------------------------------------------------
    def step(self, now_ts: int) -> None:
        now_ts = int(now_ts)
        if now_ts <= self.clock:
            return
        for st in self._streams.values():
            st.step(now_ts)
        self.clock = now_ts

    def quote(self, symbol: str) -> Quote:
        return self._ensure(validate_symbol(symbol)).quote()

    def quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            try:
                out[validate_symbol(s)] = self.quote(s)
            except ValueError:
                continue
        return out

    def candles(self, symbol: str, limit: int = 240) -> List[Candle]:
        return self._ensure(validate_symbol(symbol)).candles(limit)

    def current_candle(self, symbol: str) -> Optional[Candle]:
        st = self._ensure(validate_symbol(symbol))
        if st.live is not None:
            return Candle(**st.live)
        return Candle(**st.history[-1]) if st.history else None

    def market_state(self) -> dict:
        return {"state": "SIMULATED", "label": "SIMULATED DATA", "source": SOURCE, "seed": self.seed,
                "message": "Deterministic simulated prices for development. Not a market feed.",
                "clock": datetime.fromtimestamp(self.clock, tz=timezone.utc).isoformat(timespec="seconds")}

    def tradability(self, symbol: str) -> Tradability:
        return Tradability(status="SIMULATED", message="Simulated symbol: no tradability metadata exists and none is inferred.")


SimulatedMarketDataAdapter = SimulatedMarket
