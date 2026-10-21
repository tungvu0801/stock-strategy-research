"""
Read-only position sources for Trader View, shown side by side and NEVER combined:

  ALPACA_PAPER_VIEW     paper.alpaca_view.view()  — the last explicit "Refresh Alpaca Paper" held in memory (0 broker calls here)
  LOCAL_PAPER           paper.portfolio.view()    — the Stage 4.5 local simulator (reads the local database only)
  ROBINHOOD_READ_ONLY   api.routes.portfolio.load_view(provider) — the existing read-only route surface, only when PORTFOLIO_AWARENESS_ENABLED;
                        otherwise DISABLED and nothing is called. Reads only (get_portfolio / get_positions / realized P&L).

A source without a row for the symbol says No position; nothing is inferred and no totals are built across sources. Each source
is isolated: one failing source never hides the others. Results are cached for the position poll interval.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Optional, Tuple

import config
from api.routes.portfolio import PortfolioUnavailable          # the existing Stage 2.7C read-only route surface (also load_view / provider_factory)
from trader_view.models import PositionSource, PositionState

log = logging.getLogger(__name__)
NOTE = "Each source is shown separately and never summed. No position is inferred: a source without a row for this symbol shows No position."
ALPACA_PAPER_VIEW, LOCAL_PAPER, ROBINHOOD_READ_ONLY = "ALPACA_PAPER_VIEW", "LOCAL_PAPER", "ROBINHOOD_READ_ONLY"
LABELS = {ALPACA_PAPER_VIEW: "Alpaca Paper View", LOCAL_PAPER: "Local Paper Portfolio", ROBINHOOD_READ_ONLY: "Robinhood Read Only"}


def _s(v) -> Optional[str]:
    return None if v is None else str(v)


def _dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None and str(v) != "" else None
    except (InvalidOperation, ValueError):
        return None


def _pct(num, den) -> Optional[str]:
    n, d = _dec(num), _dec(den)
    if n is None or d is None or d == 0:
        return None
    return f"{(n / d * 100):.2f}"


def _default_alpaca_status() -> dict:
    from paper import alpaca_view as AV
    return AV.status()


def _default_alpaca_view() -> dict:
    from paper import alpaca_view as AV
    return AV.view()


def _default_local_view() -> dict:
    from paper import portfolio as P
    return P.view()


def _default_robinhood_view():
    import api.routes.portfolio as PR                                  # looked up at call time: tests swap PR.provider_factory
    return PR.load_view(PR.provider_factory(), with_events=False)


class StockAgentPositionAdapter:
    def __init__(self, *, alpaca_status: Callable[[], dict] = _default_alpaca_status, alpaca_view: Callable[[], dict] = _default_alpaca_view,
                 local_view: Callable[[], dict] = _default_local_view, robinhood_view: Callable = _default_robinhood_view,
                 awareness_enabled: Optional[Callable[[], bool]] = None, cache_seconds: Optional[float] = None,
                 now_fn: Callable[[], float] = time.monotonic):
        self._alpaca_status, self._alpaca_view, self._local_view, self._robinhood_view = alpaca_status, alpaca_view, local_view, robinhood_view
        self._awareness_enabled = awareness_enabled or (lambda: bool(config.PORTFOLIO_AWARENESS_ENABLED))
        self.cache_seconds = float(cache_seconds if cache_seconds is not None else config.TRADER_POSITION_POLL_SECONDS)
        self._now = now_fn
        self._cache: Dict[str, Tuple[float, PositionState]] = {}

    # ---------------------------------------------------------------- public --------------------------------------------------
    def position(self, symbol: str) -> PositionState:
        s = symbol.upper()
        now = self._now()
        hit = self._cache.get(s)
        if hit is not None and now - hit[0] < self.cache_seconds:
            return hit[1]
        sources = [self._alpaca(s), self._local(s), self._robinhood(s)]
        state = PositionState(symbol=s, has_position=any(x.status == "POSITION" for x in sources), sources=sources, note=NOTE)
        self._cache[s] = (now, state)
        return state

    def invalidate(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._cache.clear()
        else:
            self._cache.pop(symbol.upper(), None)

    # ---------------------------------------------------------------- sources -------------------------------------------------
    @staticmethod
    def _src(source: str, status: str, **kw) -> PositionSource:
        return PositionSource(source=source, label=LABELS[source], status=status, **kw)

    def _alpaca(self, s: str) -> PositionSource:
        src = ALPACA_PAPER_VIEW
        try:
            st = self._alpaca_status()
            conn = st.get("connection") or {}
            if conn.get("state") == "NOT_CONFIGURED":
                return self._src(src, "NOT_CONFIGURED", message="Alpaca Paper is not configured (names only; nothing was called).")
            if not st.get("refreshed_at"):
                return self._src(src, "NOT_REFRESHED", message="No refresh yet — use Paper Trades › Alpaca Paper › Refresh. Trader View never calls the broker.")
            v = self._alpaca_view()
            as_of = v.get("refreshed_at")
            note = None if (v.get("connection") or {}).get("state") == "CONNECTED" else (v.get("connection") or {}).get("label")
            row = next((p for p in (v.get("positions") or []) if str(p.get("symbol") or "").upper() == s), None)
            if row is None:
                return self._src(src, "NO_POSITION", message=note or "No position", as_of=as_of)
            plpc = _dec(row.get("unrealized_plpc"))
            return self._src(src, "POSITION", message=note, quantity=_s(row.get("qty")), average_cost=_s(row.get("avg_entry_price")),
                             market_value=_s(row.get("market_value")), unrealized_pnl=_s(row.get("unrealized_pl")),
                             unrealized_pnl_pct=f"{plpc * 100:.2f}" if plpc is not None else None, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 - one source never hides the others
            log.warning("Trader View: Alpaca paper view unavailable (%s)", type(exc).__name__)
            return self._src(src, "ERROR", message=f"Unavailable ({type(exc).__name__}).")

    def _local(self, s: str) -> PositionSource:
        src = LOCAL_PAPER
        try:
            v = self._local_view()
            if not v.get("account"):
                return self._src(src, "NOT_SET_UP", message="The local paper portfolio is not set up.")
            as_of = (v.get("summary") or {}).get("mark_session")
            row = next((p for p in (v.get("positions") or []) if str(p.get("symbol") or "").upper() == s and (_dec(p.get("shares")) or 0) != 0), None)
            if row is None:
                return self._src(src, "NO_POSITION", message="No position", as_of=as_of)
            return self._src(src, "POSITION", quantity=_s(row.get("shares")), average_cost=_s(row.get("average_cost")),
                             market_value=_s(row.get("market_value")), unrealized_pnl=_s(row.get("unrealized_pnl")),
                             unrealized_pnl_pct=_pct(row.get("unrealized_pnl"), row.get("cost_basis")), as_of=as_of,
                             message=None if row.get("mark") is not None else "Unmarked (no market data for the last session).")
        except Exception as exc:  # noqa: BLE001
            log.warning("Trader View: local paper view unavailable (%s)", type(exc).__name__)
            return self._src(src, "ERROR", message=f"Unavailable ({type(exc).__name__}).")

    def _robinhood(self, s: str) -> PositionSource:
        src = ROBINHOOD_READ_ONLY
        if not self._awareness_enabled():
            return self._src(src, "DISABLED", message="Portfolio awareness is off (PORTFOLIO_AWARENESS_ENABLED=false); nothing was called.")
        try:
            view = self._robinhood_view()
        except PortfolioUnavailable as exc:
            return self._src(src, "UNAVAILABLE", message=f"{exc.reason}: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Trader View: Robinhood read-only view unavailable (%s)", type(exc).__name__)
            return self._src(src, "ERROR", message=f"Unavailable ({type(exc).__name__}).")
        try:
            as_of = getattr(getattr(view, "snapshot", None), "fetched_at", None)
            pos = next((p for p in getattr(view, "positions", []) if str(getattr(p, "symbol", "")).upper() == s), None)
            if pos is None:
                return self._src(src, "NO_POSITION", message="No position", as_of=as_of)
            return self._src(src, "POSITION", quantity=_s(pos.quantity), average_cost=_s(pos.avg_cost), market_value=_s(pos.market_value),
                             unrealized_pnl=_s(pos.unrealized_pnl), unrealized_pnl_pct=_s(pos.unrealized_pnl_pct), as_of=as_of,
                             message=f"Quote quality {getattr(pos, 'quote_quality', 'UNKNOWN')}; view {getattr(view, 'status', '')}".strip())
        except Exception as exc:  # noqa: BLE001
            return self._src(src, "ERROR", message=f"Unavailable ({type(exc).__name__}).")
