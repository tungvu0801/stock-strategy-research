"""
Trader View Market Demand provider (Stage 5.7): the current session's minute bars and a few prior sessions from the market adapter
(`session_candles`, served from the adapter's existing bar cache — no extra request), completed daily bars (cached like PPE's), the
batched quotes for the ticker and its benchmark, then the deterministic engine; results cached per symbol for
TRADER_DEMAND_REFRESH_SECONDS (never per quote tick). Simulated mode is labelled SIMULATED_DATA. Read only: no order, no research run,
nothing shared with PPE except the benchmark hierarchy.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import config
from data.sector_map import SECTOR_MAP
from demand import WEIGHTS
from demand.engine import compute_demand
from demand.inputs import BenchmarkInputs, DemandInputs, from_candles
from demand.models import DemandResult, not_available
from ppe.inputs import DailyBar
from ppe.sectors import basket_bars, resolve_benchmark, sector_for
from trader_view.ppe_provider import session_today

log = logging.getLogger(__name__)
CLOSED_HINTS = ("AFTER_HOURS", "CLOSED", "WEEKEND")


def _iso(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat(timespec="seconds")


class DemandProvider:
    def __init__(self, market, *, result_cache_s: Optional[float] = None, bars_cache_s: Optional[float] = None, lookback_days: Optional[int] = None,
                 prior_sessions: Optional[int] = None, now_fn: Callable[[], float] = time.time):
        self.market = market
        self.result_cache_s = float(result_cache_s if result_cache_s is not None else config.TRADER_DEMAND_REFRESH_SECONDS)
        self.bars_cache_s = float(bars_cache_s if bars_cache_s is not None else config.PPE_BARS_CACHE_SECONDS)
        self.lookback_days = int(lookback_days or config.DEMAND_DAILY_LOOKBACK_DAYS)
        self.prior_sessions = int(prior_sessions or config.DEMAND_PRIOR_SESSIONS)
        self.now_fn = now_fn
        self._results: Dict[str, Tuple[float, DemandResult]] = {}
        self._daily: Dict[str, Tuple[float, List[DailyBar]]] = {}
        self.computations = 0

    # ---------------------------------------------------------------- public ------------------------------------------------
    def result(self, symbol: str) -> DemandResult:
        s = symbol.upper()
        now = self.now_fn()
        hit = self._results.get(s)
        if hit is not None and now - hit[0] < self.result_cache_s:
            return hit[1]
        res = self._compute(s, now)
        self._results[s] = (now, res)
        return res

    def invalidate(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._results.clear()
        else:
            self._results.pop(symbol.upper(), None)

    # ---------------------------------------------------------------- data --------------------------------------------------
    def _candidates(self, symbol: str) -> List[str]:
        out: List[str] = []
        sector = sector_for(symbol)
        if sector and sector in SECTOR_MAP:
            out.append(SECTOR_MAP[sector]["etf"])
            out += [p for p in SECTOR_MAP[sector]["peers"] if p != symbol]
        out.append(config.MARKET_PROXY_SYMBOL)
        seen, uniq = set(), []
        for s in out:
            if s not in seen and s != symbol:
                seen.add(s)
                uniq.append(s)
        return uniq

    def _daily_bars(self, symbols: List[str], now: float) -> Dict[str, List[DailyBar]]:
        stale = [s for s in symbols if s not in self._daily or now - self._daily[s][0] > self.bars_cache_s]
        if stale:
            try:
                fresh = self.market.daily_bars(stale, self.lookback_days)
            except Exception as exc:  # noqa: BLE001 - no bars: the daily components are NOT_AVAILABLE, nothing is fabricated
                log.warning("Market Demand daily bars unavailable (%s)", type(exc).__name__)
                fresh = {}
            for s in stale:
                self._daily[s] = (now, list(fresh.get(s) or []))
        return {s: self._daily[s][1] for s in symbols}

    @staticmethod
    def _session_return(q) -> Optional[float]:
        if q is not None and q.status == "OK" and q.price and q.prev_close:
            return q.price / q.prev_close - 1.0
        return None

    # ---------------------------------------------------------------- compute -----------------------------------------------
    def _compute(self, symbol: str, now: float) -> DemandResult:
        self.computations += 1
        today = session_today(now)
        md = f"{self.market.source} · minute + daily bars"
        try:
            sessions = self.market.session_candles(symbol, self.prior_sessions + 1) or {}
        except Exception as exc:  # noqa: BLE001 - no minute bars: DAILY_MODE
            log.warning("Market Demand session bars unavailable for %s (%s)", symbol, type(exc).__name__)
            sessions = {}
        keys = sorted(sessions)
        current_key = keys[-1] if keys else None
        if current_key and (self.market.mode != "real" or current_key == today.isoformat()):
            session_bars = from_candles(sessions[current_key])
            prior = {k: from_candles(sessions[k]) for k in keys[:-1]}
        else:                                                                   # real mode before the first bar of today: everything is a prior session
            session_bars = []
            prior = {k: from_candles(sessions[k]) for k in keys}
        quote = self.market.quote(symbol)
        candidates = self._candidates(symbol)
        bars = self._daily_bars([symbol] + candidates, now)
        completed = [b for b in bars.get(symbol) or [] if b.date < today]
        if not completed and not session_bars:
            return not_available(symbol, _iso(now), mode=self.market.mode, market_data=md, computed_at=_iso(now), weights=WEIGHTS,
                                 note="no completed daily bars and no session bars are available for this symbol")
        syms, kind, label = resolve_benchmark(symbol, available=lambda s: bool([b for b in bars.get(s) or [] if b.date < today]))
        bench: Optional[BenchmarkInputs] = None
        try:
            if kind == "PEER_BASKET":
                members = {p: [b for b in bars[p] if b.date < today] for p in syms}
                basket = basket_bars(members)
                quotes = self.market.quotes(list(syms))
                live = [r for r in (self._session_return(quotes.get(p)) for p in syms) if r is not None]
                if basket:
                    bench = BenchmarkInputs(symbol="PEERS:" + ",".join(syms), kind=kind, label=label, daily=basket,
                                            session_return=statistics.fmean(live) if live else None)
            elif kind in ("SECTOR_ETF", "MARKET"):
                q = self.market.quote(syms[0])
                bench = BenchmarkInputs(symbol=syms[0], kind=kind, label=label, daily=[b for b in bars[syms[0]] if b.date < today], session_return=self._session_return(q))
        except Exception as exc:  # noqa: BLE001 - a benchmark problem only costs component E
            log.warning("Market Demand benchmark unavailable for %s (%s)", symbol, type(exc).__name__)
            bench = None
        hint = ""
        try:
            hint = str((self.market.market_state() or {}).get("session_hint") or "")
        except Exception:  # noqa: BLE001 - no hint: the session is treated as open
            hint = ""
        ok = quote.status == "OK" and bool(quote.price)
        inp = DemandInputs(symbol=symbol, now=int(now), session_date=today, session_bars=session_bars, prior_sessions=prior, daily_bars=completed,
                           current_price=float(quote.price) if ok else None, prev_close=quote.prev_close if ok else None, session_open=quote.open if ok else None,
                           benchmark=bench, mode=self.market.mode, market_data=md,
                           price_source=f"{self.market.source} · latest quote" if ok else "last completed bar (live quote unavailable)",
                           session_complete=(self.market.mode == "real" and hint in CLOSED_HINTS))
        return compute_demand(inp, now_fn=lambda: now)
