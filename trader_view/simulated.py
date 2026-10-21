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
from datetime import datetime, timedelta, timezone
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
        return Quote(symbol=self.symbol, status="OK", price=p, open=self.prev_close, bid=round(p * (1 - 0.0002), 2), ask=round(p * (1 + 0.0002), 2),
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

    # ---------------------------------------------------------------- synthetic daily history (Stage 5.6 PPE; SIMULATED) ---------
    def daily_bars(self, symbols: Iterable[str], lookback_days: int = 420) -> Dict[str, list]:
        """Deterministic synthetic completed daily sessions per symbol: business days ending the day before the simulator's clock date,
        closing at the symbol's start price so the live intraday path continues from them. Every consumer labels this SIMULATED."""
        from ppe.inputs import DailyBar
        out: Dict[str, list] = {}
        today = datetime.fromtimestamp(self.clock, tz=timezone.utc).date()
        n = max(60, min(600, int(lookback_days * 252 / 365)))
        for raw in symbols:
            try:
                s = validate_symbol(raw)
            except ValueError:
                continue
            rng = random.Random(f"{self.seed}:{s}:daily")
            days, d = [], today - timedelta(days=1)
            while len(days) < n:
                if d.weekday() < 5:
                    days.append(d)
                d -= timedelta(days=1)
            days.reverse()
            vol = 0.012 + rng.random() * 0.02
            drift = rng.gauss(0.0003, 0.0004)
            base_vol = 400_000 + rng.randrange(0, 3_000_000)
            closes, px = [], 100.0
            for _ in days:
                px *= math.exp(rng.gauss(drift, vol))
                closes.append(px)
            scale = start_price(s) / closes[-1]
            bars, prev = [], closes[0] * scale / math.exp(rng.gauss(0.0, vol))
            for day, c in zip(days, closes):
                c *= scale
                o = prev * math.exp(rng.gauss(0.0, vol / 3))
                h = max(o, c) * (1 + abs(rng.gauss(0.0, vol / 2)))
                lo = min(o, c) * (1 - abs(rng.gauss(0.0, vol / 2)))
                bars.append(DailyBar(date=day, open=round(o, 4), high=round(h, 4), low=round(lo, 4), close=round(c, 4),
                                     volume=float(int(base_vol * (1 + abs(rng.gauss(0.0, 0.5)))))))
                prev = c
            out[s] = bars
        return out

    # ---------------------------------------------------------------- synthetic sessions (Stage 5.7 Market Demand; SIMULATED) ------------
    def session_candles(self, symbol: str, sessions: int = 5) -> Dict[str, List[Candle]]:
        """The current synthetic session (this stream's bars) plus `sessions - 1` deterministic prior sessions of 390 one-minute bars with a
        U-shaped volume profile, keyed by ISO date (prior business days before the simulator's clock date). SIMULATED, never a market feed."""
        st = self._ensure(validate_symbol(symbol))
        today = datetime.fromtimestamp(self.clock, tz=timezone.utc).date()
        days, d = [], today - timedelta(days=1)
        while len(days) < max(0, int(sessions) - 1):
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        out: Dict[str, List[Candle]] = {}
        for day in reversed(days):
            rng = random.Random(f"{self.seed}:{st.symbol}:{day.isoformat()}:session")
            start = int(datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc).timestamp())
            px = round(start_price(st.symbol) * math.exp(rng.gauss(0.0, 0.01)), 2)
            bars = []
            for i in range(390):
                o = px
                hi = lo = o
                for _ in range(6):
                    px = round(max(0.01, px * math.exp(rng.gauss(0.0, SIGMA_PER_SECOND * math.sqrt(10.0)))), 2)
                    hi, lo = max(hi, px), min(lo, px)
                u = 1.0 + 6.0 * (i / 389.0 - 0.5) ** 2                            # U-shape: 2.5x at the open and close, 1x midday
                vol = float(int(st.volume_base * u * (0.7 + 0.6 * rng.random())))
                bars.append(Candle(time=start + 60 * i, open=o, high=hi, low=lo, close=px, volume=vol, final=True))
            out[day.isoformat()] = bars
        out[today.isoformat()] = st.candles(0)
        return out

    def market_state(self) -> dict:
        return {"state": "SIMULATED", "label": "SIMULATED DATA", "source": SOURCE, "seed": self.seed,
                "message": "Deterministic simulated prices for development. Not a market feed.",
                "clock": datetime.fromtimestamp(self.clock, tz=timezone.utc).isoformat(timespec="seconds")}

    def tradability(self, symbol: str) -> Tradability:
        return Tradability(status="SIMULATED", message="Simulated symbol: no tradability metadata exists and none is inferred.")


SimulatedMarketDataAdapter = SimulatedMarket
