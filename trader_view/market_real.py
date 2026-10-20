"""
Real read-only market data for Trader View through the EXISTING Alpaca market-data client (data.market_data.get_data_client — the
historical/snapshot data API; never a trading client).

Polling, not streaming: one batched snapshot request per poll covering every symbol a browser is watching (active tickers plus the
watchlist) gives price, bid/ask, the current minute bar, today's volume and the previous close; minute bars for the active ticker are
fetched on subscribe and cached. Results are cached for the poll interval so HTTP requests and the stream never multiply calls.
Bounded by design: with the default cadences (quote 3 s, watchlist 10 s) this is about 25 requests per minute while the Trader View
tab is open and zero when it is not. Nothing is fabricated: an unreachable feed is reported as UNAVAILABLE / FEED_ERROR and a symbol
the feed has no data for as NO_DATA.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame

import config
from api.validation import validate_symbol
from data.market_data import get_data_client
from trader_view.adapters import UnknownSymbol
from trader_view.models import Candle, Quote, Tradability

log = logging.getLogger(__name__)
HISTORY_LOOKBACK_DAYS = 5                        # enough calendar days to reach the previous session over a weekend


def _epoch(ts) -> Optional[int]:
    return int(ts.timestamp()) if ts is not None else None


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


class RealMarketData:
    mode = "real"

    def __init__(self, *, client_factory: Callable = get_data_client, feed: Optional[str] = None, quote_cache_s: Optional[float] = None,
                 bars_cache_s: float = 60.0, history_minutes: Optional[int] = None, now_fn: Callable[[], float] = time.monotonic):
        self._factory = client_factory
        self._client = None
        self.feed = (feed or config.DATA_FEED or "iex").lower()
        self.source = f"Alpaca market data · {self.feed.upper()} feed · polled, read only"
        self.quote_cache_s = float(quote_cache_s if quote_cache_s is not None else config.TRADER_QUOTE_POLL_SECONDS)
        self.bars_cache_s = float(bars_cache_s)
        self.history_minutes = int(history_minutes or config.TRADER_HISTORY_MINUTES)
        self._now = now_fn
        self._quotes: Dict[str, Tuple[float, Quote, Optional[Candle]]] = {}
        self._bars: Dict[str, Tuple[float, List[Candle]]] = {}
        self.requests = 0
        self.last_error: Optional[str] = None

    # ---------------------------------------------------------------- client -------------------------------------------------
    def _c(self):
        if self._client is None:
            self._client = self._factory()                                    # raises RuntimeError without credentials
        return self._client

    def _feed(self) -> DataFeed:
        try:
            return DataFeed(self.feed)
        except ValueError:
            return DataFeed.IEX

    # ---------------------------------------------------------------- quotes -------------------------------------------------
    def quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        wanted = sorted({validate_symbol(s) for s in symbols})
        now = self._now()
        stale = [s for s in wanted if s not in self._quotes or now - self._quotes[s][0] > self.quote_cache_s]
        if stale:
            try:
                self.requests += 1
                snaps = self._c().get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=stale, feed=self._feed()))
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the feed is optional; the UI shows UNAVAILABLE, never a made-up price
                self.last_error = type(exc).__name__
                log.warning("Trader View market snapshot failed: %s", type(exc).__name__)
                for s in stale:
                    self._quotes[s] = (now, Quote(symbol=s, status="UNAVAILABLE", code="FEED_ERROR", source=self.source,
                                                  message=f"Market data unavailable ({type(exc).__name__})."), None)
            else:
                for s in stale:
                    q, c = self._from_snapshot(s, (snaps or {}).get(s))
                    self._quotes[s] = (now, q, c)
        return {s: self._quotes[s][1] for s in wanted}

    def _from_snapshot(self, s: str, snap) -> Tuple[Quote, Optional[Candle]]:
        trade = getattr(snap, "latest_trade", None) if snap is not None else None
        if trade is None:
            return Quote(symbol=s, status="UNAVAILABLE", code="NO_DATA", source=self.source, message="The feed has no data for this symbol."), None
        lq, mb, db, pdb = snap.latest_quote, snap.minute_bar, snap.daily_bar, snap.previous_daily_bar
        price = _f(trade.price)
        prev = _f(pdb.close) if pdb is not None else None
        chg = round((price / prev - 1) * 100, 3) if price and prev else None
        quote = Quote(symbol=s, status="OK", price=price, bid=_f(lq.bid_price) if lq is not None else None,
                      ask=_f(lq.ask_price) if lq is not None else None, prev_close=prev, change_pct=chg,
                      volume=_f(db.volume) if db is not None else None, ts=_epoch(trade.timestamp), source=self.source)
        candle = None
        if mb is not None and mb.timestamp is not None:
            candle = Candle(time=_epoch(mb.timestamp), open=float(mb.open), high=float(mb.high), low=float(mb.low), close=float(mb.close),
                            volume=float(mb.volume or 0), final=False)
        return quote, candle

    def quote(self, symbol: str) -> Quote:
        s = validate_symbol(symbol)
        return self.quotes([s])[s]

    def current_candle(self, symbol: str) -> Optional[Candle]:
        s = validate_symbol(symbol)
        self.quotes([s])
        return self._quotes[s][2]

    def resolve(self, symbol: str) -> str:
        """Normalised symbol. UnknownSymbol only when the feed answered without data; a feed error keeps the symbol (shown UNAVAILABLE)."""
        s = validate_symbol(symbol)
        q = self.quotes([s])[s]
        if q.status != "OK" and q.code == "NO_DATA":
            raise UnknownSymbol(f"No market data for {s!r}.")
        return s

    # ---------------------------------------------------------------- candles ------------------------------------------------
    def candles(self, symbol: str, limit: Optional[int] = None) -> List[Candle]:
        s = validate_symbol(symbol)
        limit = int(limit or self.history_minutes)
        now = self._now()
        cached = self._bars.get(s)
        if cached is not None and now - cached[0] <= self.bars_cache_s:
            return cached[1][-limit:]
        try:
            self.requests += 1
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=HISTORY_LOOKBACK_DAYS)
            bars = self._c().get_stock_bars(StockBarsRequest(symbol_or_symbols=[s], timeframe=TimeFrame.Minute, start=start, end=end, feed=self._feed()))
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - keep whatever history we had; never invent bars
            self.last_error = type(exc).__name__
            log.warning("Trader View minute bars failed for %s: %s", s, type(exc).__name__)
            return cached[1][-limit:] if cached else []
        rows = (getattr(bars, "data", None) or {}).get(s) or []
        out = [Candle(time=_epoch(b.timestamp), open=float(b.open), high=float(b.high), low=float(b.low), close=float(b.close),
                      volume=float(b.volume or 0), final=True) for b in rows if b.timestamp is not None]
        out.sort(key=lambda c: c.time)
        self._bars[s] = (now, out)
        return out[-limit:]

    # ---------------------------------------------------------------- state --------------------------------------------------
    @staticmethod
    def _session_hint() -> str:
        """Clock-based hint only (no holiday calendar, no exchange call): REGULAR | PRE_MARKET | AFTER_HOURS | CLOSED | WEEKEND | UNKNOWN."""
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001 - no tz database: say so instead of guessing
            return "UNKNOWN"
        if et.weekday() >= 5:
            return "WEEKEND"
        m = et.hour * 60 + et.minute
        if 570 <= m < 960:
            return "REGULAR"
        if 240 <= m < 570:
            return "PRE_MARKET"
        if 960 <= m < 1200:
            return "AFTER_HOURS"
        return "CLOSED"

    def market_state(self) -> dict:
        return {"state": "LIVE_POLL", "label": "LIVE · POLLED", "source": self.source, "session_hint": self._session_hint(),
                "message": f"Read-only market data polled every {self.quote_cache_s:g}s while this tab is open. Session hint is clock-based.",
                "requests": self.requests, "last_error": self.last_error,
                "clock": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def tradability(self, symbol: str) -> Tradability:
        return Tradability(status="UNKNOWN", message="No read-only asset-metadata source exists in Stock Agent; tradability is not inferred.")

    def step(self, now_ts: int) -> None:
        return None
