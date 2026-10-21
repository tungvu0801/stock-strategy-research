"""
Deterministic sector benchmark policy (DESIGN_56 §5). Never chosen by a model, never silent:
  SECTOR_ETF  the curated sector ETF (data.sector_map, plus config.EVIDENCE_SECTOR_EXTENSIONS for tickers the map lacks)
  PEER_BASKET equal-weight daily-return index of the curated peers with available bars (symbol excluded, at least 3 peers)
  MARKET      config.MARKET_PROXY_SYMBOL, explicitly labelled "market fallback"
  NONE        component C is NOT_AVAILABLE
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import config
from data.sector_map import SECTOR_MAP, get_sector_for_symbol
from ppe.inputs import DailyBar

MIN_PEERS = 3


def sector_for(symbol: str) -> Optional[str]:
    """Curated map first, then the explicit extension tables (evidence's, then PPE's). Never guessed."""
    s = symbol.strip().upper()
    return get_sector_for_symbol(s) or config.EVIDENCE_SECTOR_EXTENSIONS.get(s) or getattr(config, "PPE_SECTOR_EXTENSIONS", {}).get(s)


def resolve_benchmark(symbol: str, available: Callable[[str], bool]) -> Tuple[Tuple[str, ...], str, str]:
    """(symbols, kind, label). `available(sym)` says whether bars for sym can be obtained without guessing."""
    s = symbol.strip().upper()
    sector = sector_for(s)
    if sector:
        info = SECTOR_MAP.get(sector)
        if info:
            etf = info["etf"]
            if available(etf):
                return (etf,), "SECTOR_ETF", f"{etf} ({sector} ETF)"
            peers = [p for p in info["peers"] if p != s and available(p)]
            if len(peers) >= MIN_PEERS:
                return tuple(peers), "PEER_BASKET", f"peer basket ({sector}: {', '.join(peers)})"
    spy = config.MARKET_PROXY_SYMBOL
    if available(spy):
        return (spy,), "MARKET", f"{spy} (market fallback — no sector benchmark available)"
    return (), "NONE", "no deterministic benchmark"


def basket_bars(series: Dict[str, List[DailyBar]]) -> List[DailyBar]:
    """Equal-weight index of the members' daily returns on their common sessions (level 100 at the first common session).
    Open/high/low equal the close (no intraday path is invented); volume is None."""
    if not series:
        return []
    by_member = {m: {b.date: b.close for b in bars} for m, bars in series.items()}
    common = sorted(set.intersection(*(set(d) for d in by_member.values())))
    if len(common) < 2:
        return []
    out: List[DailyBar] = []
    level = 100.0
    prev = common[0]
    out.append(DailyBar(date=prev, open=level, high=level, low=level, close=level, volume=None))
    for d in common[1:]:
        rets = [by_member[m][d] / by_member[m][prev] - 1.0 for m in by_member if by_member[m][prev] > 0]
        level = level * (1.0 + sum(rets) / len(rets)) if rets else level
        out.append(DailyBar(date=d, open=level, high=level, low=level, close=level, volume=None))
        prev = d
    return out


def basket_live_level(bars: List[DailyBar], member_returns: List[float]) -> Optional[float]:
    """Live mode: the basket's current level from the members' latest returns since their last close."""
    if not bars or not member_returns:
        return None
    return bars[-1].close * (1.0 + sum(member_returns) / len(member_returns))
