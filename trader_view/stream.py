"""
The terminal stream: one WebSocket per browser tab, one detailed subscription per connection, bounded polling.

Server -> client: hello · subscribed · unsubscribed · state · candles · quote · candle · watchlist · position · evidence · heartbeat · error
Client -> server: subscribe {symbol} · unsubscribe {symbol} · ping · refresh            (tick: tests only, refused otherwise)

Cadence (ticks of TRADER_TICK_SECONDS; nothing at all is polled while no browser is connected):
  quote + current candle   every tick (simulated) / every TRADER_QUOTE_POLL_SECONDS (real: one batched snapshot for all active symbols)
  watchlist                every TRADER_WATCHLIST_POLL_SECONDS (lightweight price / % change)
  position                 every TRADER_POSITION_POLL_SECONDS (read-only sources, cached)
  evidence                 on subscribe and on refresh only — never recomputed per tick, never built here
Every symbol-bound message carries its symbol; a message for a symbol the connection no longer holds is never sent.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from itertools import count
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect

import config
from api.validation import validate_symbol
from evidence import STRENGTH_DISCLAIMER
from scanner.watchlist import load_watchlist
from trader_view import CLIENT_COMMANDS, LABEL, STAGE
from trader_view.adapters import MarketUnavailable, UnknownSymbol, build_market_adapter
from trader_view.hub import Hub
from ppe import DISCLAIMER as PPE_DISCLAIMER
from ppe import VERSION as PPE_VERSION
from ppe import WEIGHTS as PPE_WEIGHTS
from ppe.models import PPEResult
from ppe.models import not_available as ppe_not_available
from trader_view.models import (Candle, DataStatus, EvidenceSummary, PositionState, Quote, TerminalState, default_actions)
from trader_view.ppe_provider import PPEProvider

log = logging.getLogger(__name__)
HEARTBEAT_TICKS = 5
CHART_ASSET = {"library": "TradingView Lightweight Charts", "version": "4.2.3", "license": "Apache-2.0",
               "served_from": "/vendor/lightweight-charts/lightweight-charts.standalone.production.js"}


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat(timespec="seconds")


class TerminalStream:
    def __init__(self, market, positions, research, *, watchlist_fn: Callable[[], List[str]] = load_watchlist,
                 demo_symbols: Optional[List[str]] = None, tick_seconds: Optional[float] = None, quote_poll_seconds: Optional[float] = None,
                 watchlist_poll_seconds: Optional[float] = None, position_poll_seconds: Optional[float] = None,
                 history_minutes: Optional[int] = None, allow_manual_tick: bool = False, run_loop: bool = True,
                 ppe_poll_seconds: Optional[float] = None, ppe_provider=None, now_fn: Callable[[], float] = time.time):
        self.market, self.positions, self.research = market, positions, research
        self.watchlist_fn = watchlist_fn
        self.demo_symbols = tuple(demo_symbols or config.TRADER_DEMO_SYMBOLS)
        self.tick_seconds = float(tick_seconds or config.TRADER_TICK_SECONDS)
        every = lambda s: max(1, round(float(s) / self.tick_seconds))  # noqa: E731 - ticks per cadence
        self.quote_every = 1 if market.mode == "simulated" else every(quote_poll_seconds or config.TRADER_QUOTE_POLL_SECONDS)
        self.watchlist_every = every(watchlist_poll_seconds or config.TRADER_WATCHLIST_POLL_SECONDS)
        self.position_every = every(position_poll_seconds or config.TRADER_POSITION_POLL_SECONDS)
        self.ppe_every = every(ppe_poll_seconds or config.TRADER_PPE_REFRESH_SECONDS)          # PPE: bounded cadence, never per quote tick
        self.history_minutes = int(history_minutes or config.TRADER_HISTORY_MINUTES)
        self.allow_manual_tick = bool(allow_manual_tick)
        self.run_loop = bool(run_loop)
        self.now_fn = now_fn
        self.ppe = ppe_provider or PPEProvider(market, now_fn=now_fn)
        self.hub = Hub()
        self.connections: Dict[int, WebSocket] = {}
        self._ids = count(1)
        self._task: Optional[asyncio.Task] = None
        self.ticks = 0

    # ---------------------------------------------------------------- read-only state (sync; HTTP routes + socket via a thread) -----
    def watch_symbols(self) -> List[str]:
        out: List[str] = []
        try:
            raw = list(self.watchlist_fn())
        except Exception as exc:  # noqa: BLE001 - an unreadable watchlist is an empty one, never a crash
            log.warning("Trader View: watchlist unavailable (%s)", type(exc).__name__)
            raw = []
        for s in raw:
            try:
                v = validate_symbol(str(s))
            except ValueError:
                continue
            if v not in out:
                out.append(v)
        return out

    def symbols(self) -> dict:
        wl = self.watch_symbols()
        return {"watchlist": wl, "demo": list(self.demo_symbols), "default": (wl or list(self.demo_symbols))[0],
                "mode": self.market.mode, "source": self.market.source, "label": LABEL, "watchlist_source": "watchlist.txt (existing, read only)"}

    def resolve(self, symbol: str) -> str:
        return self.market.resolve(symbol)

    def candles(self, symbol: str, limit: Optional[int] = None) -> List[Candle]:
        return self.market.candles(symbol, limit or self.history_minutes)

    def _position(self, s: str) -> PositionState:
        try:
            return self.positions.position(s)
        except Exception as exc:  # noqa: BLE001
            log.warning("Trader View: position sources failed for %s (%s)", s, type(exc).__name__)
            return PositionState(symbol=s, has_position=False, sources=[], note=f"Position sources unavailable ({type(exc).__name__}).")

    def _evidence(self, s: str) -> EvidenceSummary:
        try:
            return self.research.latest(s)
        except Exception as exc:  # noqa: BLE001
            return EvidenceSummary(symbol=s, status="FAILED", data_status="FAILED", message=f"Evidence unavailable ({type(exc).__name__}).",
                                   disclaimer=STRENGTH_DISCLAIMER)

    def _ppe(self, s: str) -> PPEResult:
        try:
            return self.ppe.result(s)
        except Exception as exc:  # noqa: BLE001 - a PPE problem never hides the rest of the state; nothing is fabricated
            log.warning("Trader View: PPE failed for %s (%s)", s, type(exc).__name__)
            return ppe_not_available(s, _iso()[:10], mode=self.market.mode, market_data=self.market.source, computed_at=_iso(), weights=PPE_WEIGHTS,
                                     note=f"PPE unavailable ({type(exc).__name__}).")

    def ppe_result(self, symbol: str) -> PPEResult:
        return self._ppe(symbol.upper())

    @staticmethod
    def _position_status(pos: PositionState) -> str:
        if not pos.sources:
            return "UNAVAILABLE"
        bad = [x for x in pos.sources if x.status in ("ERROR", "UNAVAILABLE")]
        return "OK" if not bad else ("PARTIAL" if len(bad) < len(pos.sources) else "UNAVAILABLE")

    def state(self, symbol: str, *, with_candles: bool = True) -> TerminalState:
        s = symbol.upper()
        quote = self.market.quote(s)
        pos = self._position(s)
        ev = self._evidence(s)
        return TerminalState(
            label=LABEL, symbol=s, quote=quote, position=pos, evidence=ev, ppe=self._ppe(s), tradability=self.market.tradability(s),
            data_status=DataStatus(mode=self.market.mode, market="SIMULATED" if self.market.mode == "simulated" else quote.status,
                                   market_source=self.market.source, position=self._position_status(pos), evidence=ev.status, server_time=_iso()),
            actions=default_actions(), market_state=self.market.market_state(), candles=self.candles(s) if with_candles else [])

    def watchlist_payload(self) -> dict:
        wl = self.watch_symbols()
        quotes = self.market.quotes(wl) if wl else {}
        items = []
        for s in wl:
            q = quotes.get(s) or Quote(symbol=s, status="UNAVAILABLE", source=self.market.source)
            items.append({"symbol": s, "price": q.price, "change_pct": q.change_pct, "status": q.status})
        return {"type": "watchlist", "items": items, "source": self.market.source, "clock": _iso(self.now_fn())}

    def cadence(self) -> dict:
        t = self.tick_seconds
        return {"tick_s": t, "quote_s": t * self.quote_every, "watchlist_s": t * self.watchlist_every, "position_s": t * self.position_every, "ppe_s": t * self.ppe_every,
                "evidence": "on subscribe / refresh only", "idle": "nothing is polled while no browser is subscribed"}

    def config_payload(self) -> dict:
        return {"stage": STAGE, "label": LABEL, "mode": self.market.mode, "source": self.market.source, "cadence": self.cadence(),
                "history_minutes": self.history_minutes, "demo_symbols": list(self.demo_symbols), "market_state": self.market.market_state(),
                "actions": [a.model_dump() for a in default_actions()], "ppe": {"version": PPE_VERSION, "weights": PPE_WEIGHTS, "refresh_s": self.tick_seconds * self.ppe_every, "disclaimer": PPE_DISCLAIMER,
                        "cadence": "on subscribe, on refresh and every refresh_s for the active ticker — never per quote tick"}, "chart": dict(CHART_ASSET),
                "client_commands": list(CLIENT_COMMANDS), "execution_preview": "NOT_CONNECTED"}

    # ---------------------------------------------------------------- loop ---------------------------------------------------
    def ensure_running(self) -> None:
        if not self.run_loop:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.tick_seconds)
                if not self.connections:
                    continue                                                  # idle: no browser, no polling
                try:
                    await self.tick(int(self.now_fn()))
                except Exception as exc:  # noqa: BLE001 - one bad tick never stops the stream
                    log.warning("Trader View stream tick failed: %s", type(exc).__name__)
        except asyncio.CancelledError:
            pass

    async def tick(self, now: int, *, force_all: bool = False) -> None:
        self.ticks += 1
        n = self.ticks
        self.market.step(now)
        active = self.hub.values()
        if not active:                                                        # connected but nothing subscribed (tab left): heartbeat only
            if self.connections and (force_all or n % HEARTBEAT_TICKS == 0):
                hb = {"type": "heartbeat", "server_time": _iso(now), "market_state": self.market.market_state(), "subscriptions": 0}
                for conn_id in list(self.connections):
                    await self._send(conn_id, hb)
            return
        if force_all or n % self.quote_every == 0:
            quotes = await asyncio.to_thread(self.market.quotes, active)
            for conn_id, symbol in self.hub.items():
                q = quotes.get(symbol)
                c = self.market.current_candle(symbol)
                if q is not None:
                    await self._send(conn_id, {"type": "quote", **q.model_dump()})
                if c is not None:
                    await self._send(conn_id, {"type": "candle", "symbol": symbol, **c.model_dump()})
        if force_all or n % self.watchlist_every == 0:
            payload = await asyncio.to_thread(self.watchlist_payload)
            for conn_id, _symbol in self.hub.items():
                await self._send(conn_id, payload)
        if force_all or n % self.position_every == 0:
            for conn_id, symbol in self.hub.items():
                pos = await asyncio.to_thread(self._position, symbol)
                if self.hub.symbol_of(conn_id) == symbol:
                    await self._send(conn_id, {"type": "position", **pos.model_dump()})
        if force_all or n % self.ppe_every == 0:                                    # PPE on its own bounded cadence (TRADER_PPE_REFRESH_SECONDS)
            for conn_id, symbol in self.hub.items():
                res = await asyncio.to_thread(self._ppe, symbol)
                if self.hub.symbol_of(conn_id) == symbol:
                    await self._send(conn_id, {"type": "ppe", **res.model_dump()})
        if self.connections and (force_all or n % HEARTBEAT_TICKS == 0):
            hb = {"type": "heartbeat", "server_time": _iso(now), "market_state": self.market.market_state(), "subscriptions": len(self.hub)}
            for conn_id in list(self.connections):
                await self._send(conn_id, hb)

    # ---------------------------------------------------------------- socket -------------------------------------------------
    async def _send(self, conn_id: int, msg: Dict[str, Any]) -> bool:
        ws = self.connections.get(conn_id)
        if ws is None:
            return False
        try:
            await ws.send_json(msg)
            return True
        except Exception:  # noqa: BLE001 - a dead socket is dropped; the browser reconnects and re-subscribes exactly once
            self._drop(conn_id)
            return False

    def _drop(self, conn_id: int) -> None:
        self.connections.pop(conn_id, None)
        self.hub.drop(conn_id)

    async def _error(self, conn_id: int, code: str, message: str, **extra) -> None:
        await self._send(conn_id, {"type": "error", "code": code, "message": message, **extra})

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        conn_id = next(self._ids)
        self.connections[conn_id] = websocket
        self.ensure_running()
        try:
            await self._send(conn_id, {"type": "hello", "connection": conn_id, "stage": STAGE, "label": LABEL, "mode": self.market.mode,
                                       "source": self.market.source, "symbols": self.symbols(), "market_state": self.market.market_state(),
                                       "cadence": self.cadence(), "commands": list(CLIENT_COMMANDS)})
            await self._send(conn_id, await asyncio.to_thread(self.watchlist_payload))      # the watchlist is populated at once, then every N ticks
            while True:
                try:
                    msg = await websocket.receive_json()
                except ValueError:
                    await self._error(conn_id, "BAD_MESSAGE", "expected a JSON object")
                    continue
                kind = msg.get("type") if isinstance(msg, dict) else None
                if kind == "subscribe":
                    await self._subscribe(conn_id, msg)
                elif kind == "unsubscribe":
                    s = str(msg.get("symbol") or "").upper() or None
                    ok = self.hub.unsubscribe(conn_id, s)
                    await self._send(conn_id, {"type": "unsubscribed", "symbol": s, "ok": ok})
                elif kind == "ping":
                    await self._send(conn_id, {"type": "heartbeat", "server_time": _iso(self.now_fn()), "market_state": self.market.market_state(),
                                               "subscriptions": len(self.hub)})
                elif kind == "refresh":
                    await self._refresh(conn_id)
                elif kind == "tick":
                    if not self.allow_manual_tick:
                        await self._error(conn_id, "NOT_ALLOWED", "manual ticks are only enabled in tests")
                        continue
                    await self.tick(int(msg.get("now") or self.now_fn()), force_all=bool(msg.get("all")))
                else:
                    await self._error(conn_id, "BAD_MESSAGE", "expected subscribe / unsubscribe / ping / refresh")
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self._drop(conn_id)

    async def _subscribe(self, conn_id: int, msg: dict) -> None:
        raw = str(msg.get("symbol") or "")
        try:
            s = validate_symbol(raw)
        except ValueError as exc:
            await self._error(conn_id, "INVALID_SYMBOL", str(exc), symbol=raw.strip().upper()[:12])
            return
        try:
            s = await asyncio.to_thread(self.market.resolve, s)
        except UnknownSymbol as exc:
            await self._error(conn_id, "UNKNOWN_SYMBOL", str(exc), symbol=s)
            return
        except MarketUnavailable as exc:
            await self._error(conn_id, "MARKET_UNAVAILABLE", str(exc), symbol=s)
        replaced = self.hub.subscribe(conn_id, s)
        await self._send(conn_id, {"type": "subscribed", "symbol": s, "replaced": replaced})
        state = await asyncio.to_thread(self.state, s, with_candles=True)
        if self.hub.symbol_of(conn_id) != s:
            return                                                            # switched again while loading: the old symbol is never delivered
        d = state.model_dump()
        candles = d.pop("candles")
        await self._send(conn_id, {"type": "state", **d})
        await self._send(conn_id, {"type": "candles", "symbol": s, "candles": candles})
        await self._send(conn_id, {"type": "quote", **d["quote"]})
        if candles:
            await self._send(conn_id, {"type": "candle", "symbol": s, **candles[-1]})
        await self._send(conn_id, {"type": "position", **d["position"]})
        await self._send(conn_id, {"type": "evidence", **d["evidence"]})

    async def _refresh(self, conn_id: int) -> None:
        s = self.hub.symbol_of(conn_id)
        if s is None:
            await self._error(conn_id, "NOT_SUBSCRIBED", "subscribe to a symbol first")
            return
        for adapter in (self.positions, self.ppe):
            try:
                adapter.invalidate(s)
            except Exception:  # noqa: BLE001 - adapters without a cache
                pass
        pos = await asyncio.to_thread(self._position, s)
        ev = await asyncio.to_thread(self._evidence, s)
        res = await asyncio.to_thread(self._ppe, s)
        if self.hub.symbol_of(conn_id) != s:
            return
        await self._send(conn_id, {"type": "position", **pos.model_dump()})
        await self._send(conn_id, {"type": "evidence", **ev.model_dump()})
        await self._send(conn_id, {"type": "ppe", **res.model_dump()})


def build_stream(mode: Optional[str] = None) -> TerminalStream:
    """Production wiring: the configured market adapter, the Stock Agent position sources and the stored-evidence reader."""
    from trader_view.positions import StockAgentPositionAdapter
    from trader_view.research import EvidenceResearchAdapter
    return TerminalStream(build_market_adapter(mode), StockAgentPositionAdapter(), EvidenceResearchAdapter())
