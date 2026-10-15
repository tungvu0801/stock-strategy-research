"""
evidence/sources/internal.py — the project's own market, sector and event data as evidence (DESIGN_54 §3, §5 "computed facts").

Bars for the ticker, SPY and the sector ETF (existing data.market_data client), the Stage 2.6 events bundle (FOMC / macro /
corporate / earnings availability) and the curated sector map. Everything computed here is a tier-1 FACT with its inputs stored
in value_json, plus a `facts` dict the rules read directly: the decline window (peak → trough), window returns for the three
series, 20-session returns, EMA20 / EMA50 positions, relative strength, relative volume, next earnings and next FOMC dates.
No model, no broker, no Robinhood. Bars and events are injectable so tests use fixtures only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from evidence import MARKET_CONFIRMATION, MARKET_WIDE
from evidence.net import FetchResult
from evidence.sources.base import AdapterResult, SourceItem, iso_day

SPY = "SPY"
Series = List[Tuple[str, float, float]]          # (YYYY-MM-DD, close, volume), ascending


def _default_bars(symbols: List[str], lookback_days: int) -> Dict[str, object]:
    from data.market_data import fetch_daily_bars, get_data_client     # local import: only touched when actually used
    return fetch_daily_bars(get_data_client(), symbols, lookback_days)


def _default_events(symbol: str):
    from services.event_context import build_event_context
    return build_event_context(symbol)


def _default_sector(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    from data.sector_map import SECTOR_MAP, get_sector_for_symbol
    name = get_sector_for_symbol(symbol)
    return (name, SECTOR_MAP[name]["etf"]) if name and name in SECTOR_MAP else (None, None)


def to_series(frame) -> Series:
    """Accept the project's bar DataFrame (timestamp / close / volume columns) or a list of {date, close, volume} dicts."""
    out: Series = []
    if frame is None:
        return out
    if hasattr(frame, "to_dict"):
        rows = frame.to_dict("records")
        for r in rows:
            ts = r.get("timestamp") or r.get("date")
            out.append((iso_day(ts), float(r["close"]), float(r.get("volume") or 0)))
    else:
        for r in frame:
            out.append((iso_day(r.get("date") or r.get("timestamp")), float(r["close"]), float(r.get("volume") or 0)))
    out = [x for x in out if x[0]]
    out.sort(key=lambda x: x[0])
    return out


def ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def ret_n(closes: List[float], n: int) -> Optional[float]:
    return (closes[-1] / closes[-1 - n] - 1.0) if len(closes) > n and closes[-1 - n] else None


def decline_window(s: Series, start_day: str) -> Optional[dict]:
    """Peak close on/after start_day to the lowest close after it. None when fewer than 2 sessions."""
    win = [x for x in s if x[0] >= start_day]
    if len(win) < 2:
        return None
    pi = max(range(len(win)), key=lambda i: (win[i][1], -i))
    after = win[pi:]
    ti = max(range(len(after)), key=lambda i: (-after[i][1], -i))
    peak, trough = win[pi], after[ti]
    return {"window_start": peak[0], "window_end": trough[0], "peak_close": peak[1], "trough_close": trough[1], "stock_ret": trough[1] / peak[1] - 1.0}


def window_return(s: Series, start_day: str, end_day: str) -> Optional[float]:
    a = next((x for x in s if x[0] >= start_day), None)
    b = next((x for x in reversed(s) if x[0] <= end_day), None)
    return (b[1] / a[1] - 1.0) if a and b and a[1] and a[0] <= b[0] else None


class InternalAdapter:
    name = "internal"

    def __init__(self, *, bars_fn: Optional[Callable] = None, events_fn: Optional[Callable] = None, sector_fn: Optional[Callable] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.bars_fn, self.events_fn, self.sector_fn = bars_fn or _default_bars, events_fn or _default_events, sector_fn or _default_sector
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def run(self, ticker: str, as_of: date, lookback_days: int) -> AdapterResult:
        res = AdapterResult(adapter=self.name)
        now = self.now_fn()
        retrieved = now.isoformat(timespec="seconds")
        facts: dict = {"missing": []}
        sector_name, etf = (None, None)
        try:
            sector_name, etf = self.sector_fn(ticker)
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"sector lookup failed: {type(exc).__name__}")
        facts["sector_name"], facts["sector_etf"] = sector_name, etf
        symbols = [ticker, SPY] + ([etf] if etf and etf not in (ticker, SPY) else [])
        try:
            raw = self.bars_fn(symbols, lookback_days + 120) or {}
        except Exception as exc:  # noqa: BLE001 - a data failure is a recorded result
            raw = {}
            res.notes.append(f"bars unavailable: {type(exc).__name__}: {exc}"[:200])
        series: Dict[str, Series] = {}
        for sym in symbols:
            s = [x for x in to_series(raw.get(sym)) if x[0] <= as_of.isoformat()]
            fr = FetchResult(status="OK" if s else "EMPTY", url=f"alpaca://bars/{sym}", final_url=f"alpaca://bars/{sym}", host="alpaca", purpose=f"{sym} daily bars",
                             started_at=retrieved, error="" if s else "no bars returned")
            res.add_fetch(fr)
            if s:
                series[sym] = s
        start_day = (as_of - timedelta(days=lookback_days)).isoformat()
        t, spy, sec = series.get(ticker), series.get(SPY), series.get(etf) if etf else None
        if not t:
            facts["missing"].append("ticker bars")
        if not spy:
            facts["missing"].append("SPY bars")
        if etf and not sec:
            facts["missing"].append("sector ETF bars")

        def closes(s: Optional[Series]) -> List[float]:
            return [x[1] for x in (s or [])]

        # decline window and window returns
        dw = decline_window(t, start_day) if t else None
        if dw:
            facts.update(dw)
            facts["spy_ret"] = window_return(spy, dw["window_start"], dw["window_end"]) if spy else None
            facts["sector_ret"] = window_return(sec, dw["window_start"], dw["window_end"]) if sec else None
        else:
            facts.update({"stock_ret": None, "spy_ret": None, "sector_ret": None, "window_start": None, "window_end": None})
        # 20-session returns, EMAs, relative strength, relative volume
        tc, sc, ec = closes(t), closes(spy), closes(sec)
        facts["stock_20"], facts["spy_20"], facts["etf_20"] = ret_n(tc, 20), ret_n(sc, 20), ret_n(ec, 20)
        spy50 = ema(sc, 50)
        facts["spy_above_ema50"] = (sc[-1] > spy50) if (sc and spy50 is not None) else None
        e20, e50 = ema(tc, 20), ema(tc, 50)
        facts["price_above_ema20"] = (tc[-1] > e20) if (tc and e20 is not None) else None
        facts["price_above_ema50"] = (tc[-1] > e50) if (tc and e50 is not None) else None
        facts["rel_strength_20"] = (facts["stock_20"] - facts["etf_20"]) if (facts["stock_20"] is not None and facts["etf_20"] is not None) else None
        vols = [x[2] for x in (t or [])]
        facts["relative_volume"] = (vols[-1] / (sum(vols[-21:-1]) / 20)) if len(vols) > 21 and sum(vols[-21:-1]) else None
        facts["last_close"] = tc[-1] if tc else None
        facts["last_session"] = t[-1][0] if t else None

        def metric(category: str, subject: str, ident: str, text: str, value: dict, signed: Optional[float], day: str) -> None:
            res.items.append(SourceItem(subject_ticker=subject, source_type="MARKET_DATA", source_name="daily bars (existing market-data client)", source_url=None,
                                        source_identifier=ident, published_at=None, retrieved_at=retrieved, evidence_date=day, kind="METRIC", text=text,
                                        role="market", category=category, value={**value, "signed": signed}, source_hash=None, fetch_index=None))

        day = facts["last_session"] or as_of.isoformat()
        if facts["spy_20"] is not None:
            metric(MARKET_WIDE, SPY, "SPY 20-session return", f"SPY 20-session return {facts['spy_20']:+.2%} as of {spy[-1][0]}.", {"spy_20": facts["spy_20"]}, facts["spy_20"], spy[-1][0])
        if facts["spy_above_ema50"] is not None:
            metric(MARKET_WIDE, SPY, "SPY vs 50-day EMA", f"SPY close {sc[-1]:.2f} is {'above' if facts['spy_above_ema50'] else 'below'} its 50-day EMA {spy50:.2f}.",
                   {"close": sc[-1], "ema50": spy50}, 1.0 if facts["spy_above_ema50"] else -1.0, spy[-1][0])
        if facts["etf_20"] is not None:
            metric(MARKET_WIDE, etf, f"{etf} 20-session return", f"Sector ETF {etf} ({sector_name}) 20-session return {facts['etf_20']:+.2%}.", {"etf_20": facts["etf_20"]}, facts["etf_20"], sec[-1][0])
        if dw:
            metric(MARKET_WIDE, ticker, "decline window", f"{ticker} fell {dw['stock_ret']:+.2%} from the {dw['window_start']} peak close {dw['peak_close']:.2f} to the "
                   f"{dw['window_end']} trough {dw['trough_close']:.2f}; SPY {_pct(facts['spy_ret'])}, sector ETF {_pct(facts['sector_ret'])} over the same window.",
                   {k: facts.get(k) for k in ("window_start", "window_end", "peak_close", "trough_close", "stock_ret", "spy_ret", "sector_ret")}, dw["stock_ret"], dw["window_end"])
        if facts["price_above_ema20"] is not None:
            metric(MARKET_CONFIRMATION, ticker, "price vs EMA20", f"{ticker} close {tc[-1]:.2f} is {'above' if facts['price_above_ema20'] else 'below'} its 20-day EMA {e20:.2f}.",
                   {"close": tc[-1], "ema20": e20, "ema50": e50}, 1.0 if facts["price_above_ema20"] else -1.0, day)
        if facts["rel_strength_20"] is not None:
            metric(MARKET_CONFIRMATION, ticker, "relative strength 20", f"{ticker} 20-session return {facts['stock_20']:+.2%} vs sector ETF {facts['etf_20']:+.2%}: "
                   f"relative strength {facts['rel_strength_20']:+.2%}.", {"stock_20": facts["stock_20"], "etf_20": facts["etf_20"], "rel_strength_20": facts["rel_strength_20"]},
                   facts["rel_strength_20"], day)
        if facts["relative_volume"] is not None:
            metric(MARKET_CONFIRMATION, ticker, "relative volume", f"{ticker} last-session volume is {facts['relative_volume']:.2f}x its 20-session average.",
                   {"relative_volume": facts["relative_volume"]}, None, day)

        # events
        facts["next_earnings"], facts["next_fomc"], facts["earnings_reason"] = None, None, None
        try:
            bundle = self.events_fn(ticker)
        except Exception as exc:  # noqa: BLE001
            bundle = None
            res.notes.append(f"events unavailable: {type(exc).__name__}")
        if bundle is not None:
            facts["earnings_reason"] = getattr(bundle, "earnings_reason", None)
            for ev in list(getattr(bundle, "macro_events", []) or []) + list(getattr(bundle, "company_events", []) or []):
                d = iso_day(getattr(ev, "event_date", None))
                if not d:
                    continue
                etype = str(getattr(getattr(ev, "event_type", ""), "value", getattr(ev, "event_type", "")))
                is_macro = getattr(ev, "symbol", None) in (None, "")
                title = str(getattr(ev, "title", etype))
                if d >= as_of.isoformat():
                    if "FOMC" in (etype + title).upper() and facts["next_fomc"] is None:
                        facts["next_fomc"] = {"date": d, "title": title, "source": getattr(ev, "source", "")}
                    if "EARN" in (etype + title).upper() and not is_macro and facts["next_earnings"] is None:
                        facts["next_earnings"] = {"date": d, "title": title, "source": getattr(ev, "source", "")}
                if start_day <= d <= (as_of + timedelta(days=45)).isoformat():
                    res.items.append(SourceItem(subject_ticker=SPY if is_macro else ticker, source_type="EVENT", source_name=str(getattr(ev, "source", "events")),
                                                source_url=getattr(ev, "source_url", None), source_identifier=str(getattr(ev, "event_id", "")), published_at=None,
                                                retrieved_at=retrieved, evidence_date=d, kind="EVENT", role="event", category=MARKET_WIDE if is_macro else None,
                                                text=f"{title} on {d} ({etype}; {'confirmed' if getattr(ev, 'confirmed', False) else 'not confirmed'}).",
                                                value={"event_type": etype, "status": str(getattr(getattr(ev, 'status', ''), 'value', ''))}, source_hash=None))
        res.facts = facts
        return res


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:+.2%}"
