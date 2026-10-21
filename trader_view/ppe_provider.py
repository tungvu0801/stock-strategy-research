"""
Trader View PPE provider (Stage 5.6): builds the PPE inputs from the market adapter's daily bars and latest quote plus the STORED
Stage 5.4 evidence, runs the deterministic engine, and caches the result per symbol for TRADER_PPE_REFRESH_SECONDS. Daily bars
are cached per symbol for PPE_BARS_CACHE_SECONDS (one batched read per symbol group in real mode). Simulated mode uses the
simulator's synthetic daily series and the result is always labelled SIMULATED_DATA. Read only: no order, no research run.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import config
from ppe import WEIGHTS
from ppe.engine import compute_ppe
from ppe.evidence_context import latest_snapshot
from ppe.inputs import Benchmark, DailyBar, MarketInputs
from ppe.models import PPEResult, not_available
from ppe.sectors import basket_bars, basket_live_level, resolve_benchmark, sector_for
from data.sector_map import SECTOR_MAP

log = logging.getLogger(__name__)


def session_today(now_ts: float) -> date:
    """The current US session date (America/New_York); bars dated on or after it are not completed sessions."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now_ts, tz=ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 - no tz database: UTC date (documented fallback)
        return datetime.fromtimestamp(now_ts, tz=timezone.utc).date()


def _iso(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat(timespec="seconds")


class PPEProvider:
    def __init__(self, market, *, evidence_fn: Callable = latest_snapshot, result_cache_s: Optional[float] = None, bars_cache_s: Optional[float] = None,
                 lookback_days: Optional[int] = None, now_fn: Callable[[], float] = time.time):
        self.market = market
        self.evidence_fn = evidence_fn
        self.result_cache_s = float(result_cache_s if result_cache_s is not None else config.TRADER_PPE_REFRESH_SECONDS)
        self.bars_cache_s = float(bars_cache_s if bars_cache_s is not None else config.PPE_BARS_CACHE_SECONDS)
        self.lookback_days = int(lookback_days or config.PPE_BARS_LOOKBACK_DAYS)
        self.now_fn = now_fn
        self._results: Dict[str, Tuple[float, PPEResult]] = {}
        self._bars: Dict[str, Tuple[float, List[DailyBar]]] = {}
        self.computations = 0

    # ---------------------------------------------------------------- public ------------------------------------------------
    def result(self, symbol: str) -> PPEResult:
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
        """Every symbol the benchmark hierarchy might need, so one batched bar read decides availability deterministically."""
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
        stale = [s for s in symbols if s not in self._bars or now - self._bars[s][0] > self.bars_cache_s]
        if stale:
            try:
                fresh = self.market.daily_bars(stale, self.lookback_days)
            except Exception as exc:  # noqa: BLE001 - no bars: PPE is NOT_AVAILABLE, never fabricated
                log.warning("PPE daily bars unavailable (%s)", type(exc).__name__)
                fresh = {}
            for s in stale:
                self._bars[s] = (now, list(fresh.get(s) or []))
        return {s: self._bars[s][1] for s in symbols}

    # ---------------------------------------------------------------- compute -----------------------------------------------
    def _compute(self, symbol: str, now: float) -> PPEResult:
        self.computations += 1
        today = session_today(now)
        md = f"{self.market.source} · daily bars"
        candidates = self._candidates(symbol)
        bars = self._daily_bars([symbol] + candidates, now)
        completed = [b for b in bars.get(symbol) or [] if b.date < today]
        if not completed:
            return not_available(symbol, today.isoformat(), mode=self.market.mode, market_data=md, computed_at=_iso(now), weights=WEIGHTS,
                                 note="no completed daily bars are available for this symbol")
        syms, kind, label = resolve_benchmark(symbol, available=lambda s: bool([b for b in bars.get(s) or [] if b.date < today]))
        bench: Optional[Benchmark] = None
        try:
            if kind == "PEER_BASKET":
                members = {p: [b for b in bars[p] if b.date < today] for p in syms}
                basket = basket_bars(members)
                live = []
                for p, pb in members.items():
                    q = self.market.quote(p)
                    if q.status == "OK" and q.price and pb and pb[-1].close > 0:
                        live.append(q.price / pb[-1].close - 1.0)
                if basket:
                    bench = Benchmark(symbol="PEERS:" + ",".join(syms), kind=kind, bars=basket, current_price=basket_live_level(basket, live), label=label)
            elif kind in ("SECTOR_ETF", "MARKET"):
                q = self.market.quote(syms[0])
                bench = Benchmark(symbol=syms[0], kind=kind, bars=[b for b in bars[syms[0]] if b.date < today],
                                  current_price=q.price if q.status == "OK" else None, label=label)
        except Exception as exc:  # noqa: BLE001 - a benchmark problem only costs component C
            log.warning("PPE benchmark unavailable for %s (%s)", symbol, type(exc).__name__)
            bench = None
        quote = self.market.quote(symbol)
        if quote.status == "OK" and quote.price:
            inputs = MarketInputs(symbol=symbol, bars=completed, as_of=today, current_price=float(quote.price), current_open=quote.open,
                                  price_time=_iso(quote.ts) if quote.ts else None, benchmark=bench, mode=self.market.mode, market_data=md,
                                  price_source=f"{self.market.source} · latest quote")
        else:                                                                   # no live price: evaluate the last completed close instead
            inputs = MarketInputs(symbol=symbol, bars=completed, as_of=completed[-1].date, current_price=None, benchmark=bench, mode=self.market.mode,
                                  market_data=md, price_source="last completed close (live quote unavailable)")
        evidence = None
        try:
            evidence = self.evidence_fn(symbol, datetime.fromtimestamp(now, tz=timezone.utc))
        except Exception as exc:  # noqa: BLE001 - evidence problems never hide the stretch
            log.warning("PPE evidence unavailable for %s (%s)", symbol, type(exc).__name__)
        return compute_ppe(inputs, evidence, now_fn=lambda: now)
