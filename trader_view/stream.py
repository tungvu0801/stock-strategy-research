"""
The terminal stream: one WebSocket per browser tab, one detailed subscription per connection, bounded polling.

Server -> client: hello · subscribed · unsubscribed · state · candles · quote · candle · watchlist · position · evidence · ppe · demand · setup · setup_alert · proposal · heartbeat · error
Client -> server: subscribe {symbol} · unsubscribe {symbol} · ping · refresh            (tick / setup_eval / proposal_eval: tests only, refused otherwise)

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
import os
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
from demand import DISCLAIMER as DEMAND_DISCLAIMER
from demand import VERSION as DEMAND_VERSION
from demand import WEIGHTS as DEMAND_WEIGHTS
from demand.models import DemandResult
from demand.models import not_available as demand_not_available
from trader_view.models import (Candle, DataStatus, EvidenceSummary, PositionState, Quote, TerminalState, default_actions)
from trader_view.ppe_provider import PPEProvider
from trader_view.demand_provider import DemandProvider
from setups.market_hours import is_regular_session
from setups.models import SetupInputs, SetupView
from setups.service import SetupService
from proposals.models import ProposalView
from proposals.service import ProposalService
from safety.service import ExecutionSafetyService
from execution.service import ExecutionService      # Stage 6.0: the autonomous PAPER executor reads setups / proposals / safety through this stream
from execution.store import ExecutionStore
from safety.store import SafetyStore
from proposals.account import AccountAdapter
from account_refresh.service import AccountRefreshService, make_alpaca_view, make_safety_account_fn   # Stage 6.0.1: continuous read-only paper account refresh
from account_refresh.store import AccountRefreshStore
from cash_flow.service import CashFlowService, make_cash_flow_account_fn   # Stage 6.0.2: cash-flow-adjusted daily P&L for the Stage 5.9.1 gate
from cash_flow.store import CashFlowStore
from forward_validation.service import ForwardValidationService   # Stage 6.1: the forward paper validation observer (reads this stream's state; never orders)
from forward_validation.store import ForwardValidationStore

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
                 ppe_poll_seconds: Optional[float] = None, ppe_provider=None, demand_poll_seconds: Optional[float] = None, demand_provider=None,
                 setup_service=None, setup_poll_seconds: Optional[float] = None, now_fn: Callable[[], float] = time.time, proposal_service=None, safety_service=None,
                 execution_service=None, account_refresh_service=None, cash_flow_service=None, forward_validation_service=None):
        self.market, self.positions, self.research = market, positions, research
        self.watchlist_fn = watchlist_fn
        self.demo_symbols = tuple(demo_symbols or config.TRADER_DEMO_SYMBOLS)
        self.tick_seconds = float(tick_seconds or config.TRADER_TICK_SECONDS)
        every = lambda s: max(1, round(float(s) / self.tick_seconds))  # noqa: E731 - ticks per cadence
        self.quote_every = 1 if market.mode == "simulated" else every(quote_poll_seconds or config.TRADER_QUOTE_POLL_SECONDS)
        self.watchlist_every = every(watchlist_poll_seconds or config.TRADER_WATCHLIST_POLL_SECONDS)
        self.position_every = every(position_poll_seconds or config.TRADER_POSITION_POLL_SECONDS)
        self.ppe_every = every(ppe_poll_seconds or config.TRADER_PPE_REFRESH_SECONDS)          # PPE: bounded cadence, never per quote tick
        self.demand_every = every(demand_poll_seconds or config.TRADER_DEMAND_REFRESH_SECONDS)   # Market Demand: bounded cadence, never per quote tick
        self.history_minutes = int(history_minutes or config.TRADER_HISTORY_MINUTES)
        self.allow_manual_tick = bool(allow_manual_tick)
        self.run_loop = bool(run_loop)
        self.now_fn = now_fn
        self.ppe = ppe_provider or PPEProvider(market, now_fn=now_fn)
        self.demand = demand_provider or DemandProvider(market, now_fn=now_fn)
        self.setup_every = every(setup_poll_seconds or config.TRADER_SETUP_REFRESH_SECONDS)       # Stage 5.8: in-session cadence for the active ticker
        self.setups = setup_service or SetupService(stream_fn=lambda: self)
        self.setups.listeners.append(self._setup_event)
        # Stage 6.0.1: ONE read-only account refresher per stream; the default proposal account adapter and the default safety gate read its snapshot
        # through their existing seams (identical to the old defaults while no paper broker is configured)
        self.account_refresh = account_refresh_service or AccountRefreshService(store=AccountRefreshStore(proposal_service.store.path if proposal_service is not None else None))
        # Stage 6.0.2: ONE read-only cash-flow ledger on the refresher's paper client and database; the default gate evaluates ADJUSTED daily P&L
        self.cash_flow = cash_flow_service or CashFlowService(store=CashFlowStore(self.account_refresh.store.path), broker_fn=lambda: self.account_refresh.broker,
                                                              snapshot_at_fn=lambda: (self.account_refresh.store.snapshot() or {}).get("refreshed_at"),
                                                              baseline_fn=lambda d: SafetyStore(self.proposals.store.path).baseline(d))
        self.proposals = proposal_service or ProposalService(stream_fn=lambda: self, account=AccountAdapter(alpaca_view=make_alpaca_view(self.account_refresh)))   # Stage 5.9: review only, never an order
        # Stage 5.9.1: the persisted execution safety gate shares the proposal store's database; the proposal controls consume it
        self.safety = safety_service or ExecutionSafetyService(stream_fn=lambda: self, store=SafetyStore(self.proposals.store.path),
                                                               account_fn=make_cash_flow_account_fn(make_safety_account_fn(lambda: self, self.account_refresh, self.proposals.store.path),
                                                                                                    self.cash_flow, self.proposals.store.path))
        if getattr(self.proposals, "safety", None) is None:
            self.proposals.safety = self.safety
        self.setups.listeners.append(self.proposals.on_setup_event)
        self.proposals.listeners.append(self._proposal_event)
        # Stage 6.0: the executor shares the database; it registers NO listener (setup / proposal events never fire an order) and the socket carries no order message
        self.execution = execution_service or ExecutionService(stream_fn=lambda: self, store=ExecutionStore(self.proposals.store.path))
        # Stage 6.1: the forward paper validation observer reads this stream's setups / proposals / safety / execution / account / cash-flow state on the
        # Stage 6.1: same database; it registers NO listener, evaluates nothing and never orders (its only action is the early stop: Paper Auto -> Monitor only)
        self.forward_validation = forward_validation_service or ForwardValidationService(stream_fn=lambda: self, store=ForwardValidationStore(self.proposals.store.path))
        self._loop_ref: Optional[asyncio.AbstractEventLoop] = None
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

    def _demand(self, s: str) -> DemandResult:
        try:
            return self.demand.result(s)
        except Exception as exc:  # noqa: BLE001 - a demand problem never hides the rest of the state; nothing is fabricated
            log.warning("Trader View: Market Demand failed for %s (%s)", s, type(exc).__name__)
            return demand_not_available(s, _iso(), mode=self.market.mode, market_data=self.market.source, computed_at=_iso(), weights=DEMAND_WEIGHTS,
                                        note=f"Market Demand unavailable ({type(exc).__name__}).")

    def demand_result(self, symbol: str) -> DemandResult:
        return self._demand(symbol.upper())

    # ---------------------------------------------------------------- Stage 5.8 setup state (stored view; evaluation only on subscribe / refresh / in-session cadence) ----
    def _setup_view(self, s: str) -> SetupView:
        try:
            return self.setups.current(s)
        except Exception as exc:  # noqa: BLE001 - the stored view never hides the rest of the state
            log.warning("Trader View: setup view failed for %s (%s)", s, type(exc).__name__)
            return SetupView(symbol=s, note=f"Setup state unavailable ({type(exc).__name__}).")

    def _evaluate_setup(self, s: str, trigger: str, inputs: Optional[SetupInputs] = None, prefetched: Optional[dict] = None) -> Optional[SetupView]:
        """Runs in a worker thread. Returns the view after the evaluation, or None when the evaluation itself failed (recorded as ERROR)."""
        try:
            return self.setups.evaluate_symbol(s, None, trigger, inputs=inputs, prefetched=prefetched, origin="stream")
        except Exception as exc:  # noqa: BLE001
            log.warning("Trader View: setup evaluation failed for %s (%s)", s, type(exc).__name__)
            return None

    def _setup_event(self, event: dict) -> None:
        """Service listener (any thread): fan out persisted alerts to every connection and scheduler-driven state views to subscribers."""
        loop = self._loop_ref
        if loop is None or not self.connections or loop.is_closed():
            return
        if event.get("type") == "setup" and event.get("origin") == "stream":
            return                                                            # the stream sends its own evaluations in order
        try:
            asyncio.run_coroutine_threadsafe(self._fanout(dict(event)), loop)
        except RuntimeError:
            pass

    def _proposal_event(self, event: dict) -> None:
        """Stage 5.9 listener (any thread): a proposal generated outside the stream's own evaluations reaches the symbol's subscribers."""
        loop = self._loop_ref
        if loop is None or not self.connections or loop.is_closed() or event.get("origin") == "stream":
            return
        try:
            asyncio.run_coroutine_threadsafe(self._fanout(dict(event)), loop)
        except RuntimeError:
            pass

    async def _fanout(self, event: dict) -> None:
        event.pop("origin", None)
        if event.get("type") == "setup_alert":
            for conn_id in list(self.connections):
                await self._send(conn_id, event)
        elif event.get("type") in ("setup", "proposal"):
            for conn_id, symbol in self.hub.items():
                if symbol == event.get("symbol"):
                    await self._send(conn_id, event)

    # ---------------------------------------------------------------- Stage 5.9 review proposal (stored view; generated after each setup evaluation) ----
    def _proposal_view(self, s: str) -> ProposalView:
        try:
            return self.proposals.current(s)
        except Exception as exc:  # noqa: BLE001 - the stored view never hides the rest of the state
            log.warning("Trader View: proposal view failed for %s (%s)", s, type(exc).__name__)
            return ProposalView(symbol=s, note=f"Proposal view unavailable ({type(exc).__name__}).")

    def _evaluate_proposal(self, s: str, trigger: str, view: Optional[SetupView], prefetched: Optional[dict] = None) -> Optional[ProposalView]:
        """Runs in a worker thread after a setup evaluation the stream initiated. Review only. None when the evaluation itself failed."""
        try:
            return self.proposals.evaluate(s, None, trigger, setup_view=view, prefetched=prefetched, origin="stream")
        except Exception as exc:  # noqa: BLE001
            log.warning("Trader View: proposal evaluation failed for %s (%s)", s, type(exc).__name__)
            return None

    def setup_in_session(self, now: float) -> bool:
        return (not config.SETUP_MARKET_HOURS_ONLY) or is_regular_session(datetime.fromtimestamp(now, tz=timezone.utc))

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
            label=LABEL, symbol=s, quote=quote, position=pos, evidence=ev, ppe=self._ppe(s), demand=self._demand(s), setup=self._setup_view(s), proposal=self._proposal_view(s),
            tradability=self.market.tradability(s),
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
                "demand_s": t * self.demand_every, "setup_s": t * self.setup_every, "evidence": "on subscribe / refresh only", "idle": "nothing is polled while no browser is subscribed"}

    def config_payload(self) -> dict:
        return {"stage": STAGE, "label": LABEL, "mode": self.market.mode, "source": self.market.source, "cadence": self.cadence(),
                "history_minutes": self.history_minutes, "demo_symbols": list(self.demo_symbols), "market_state": self.market.market_state(),
                "actions": [a.model_dump() for a in default_actions()], "ppe": {"version": PPE_VERSION, "weights": PPE_WEIGHTS, "refresh_s": self.tick_seconds * self.ppe_every, "disclaimer": PPE_DISCLAIMER,
                        "cadence": "on subscribe, on refresh and every refresh_s for the active ticker — never per quote tick"},
                "demand": {"version": DEMAND_VERSION, "weights": DEMAND_WEIGHTS, "refresh_s": self.tick_seconds * self.demand_every, "disclaimer": DEMAND_DISCLAIMER,
                           "cadence": "on subscribe, on refresh and every refresh_s for the active ticker — never per quote tick", "separate_from_ppe": True},
                "setup": {"version": "setups_v1", "states": ["NONE", "WATCH", "READY", "INVALIDATED"], "ready_label": "READY FOR REVIEW", "refresh_s": self.tick_seconds * self.setup_every,
                          "cadence": "on subscribe, on refresh and every refresh_s for the active ticker inside the US regular session; the server-side evaluator covers the watchlist",
                          "market_hours_only": bool(config.SETUP_MARKET_HOURS_ONLY)},
                "proposal": {"version": "proposals_v1", "statuses": ["NOT_ELIGIBLE", "BLOCKED", "REVIEWABLE", "STALE", "EXPIRED"], "reviewable_label": "REVIEWABLE PROPOSAL",
                             "submission_status": "NOT SUBMITTED", "cadence": "generated after each setup evaluation of the active ticker; review only, never an order"},
                "safety": {"version": "safety_v1", "states": ["TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN"], "labels": {"TRADING_ALLOWED": "Trading allowed", "NO_NEW_ENTRIES": "New trades paused",
                           "HALTED": "Trading stopped"}, "cadence": "on every proposal evaluation, on the in-session cadence, on each API read and user action; persisted"},
                "chart": dict(CHART_ASSET),
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
        if force_all or n % self.demand_every == 0:                                 # Market Demand on its own bounded cadence (TRADER_DEMAND_REFRESH_SECONDS)
            for conn_id, symbol in self.hub.items():
                dem = await asyncio.to_thread(self._demand, symbol)
                if self.hub.symbol_of(conn_id) == symbol:
                    await self._send(conn_id, {"type": "demand", **dem.model_dump()})
        if (force_all or n % self.setup_every == 0) and self.setup_in_session(now):          # Stage 5.8: in-session cadence only, never per quote tick
            try:
                await asyncio.to_thread(self.safety.evaluate, "SCHEDULED")                        # Stage 5.9.1: the gate is evaluated once per cadence (persisted)
            except Exception as exc:  # noqa: BLE001 - the gate itself records its failure as UNKNOWN; the stream goes on
                log.warning("Trader View: safety evaluation failed (%s)", type(exc).__name__)
            for conn_id, symbol in self.hub.items():
                view = await asyncio.to_thread(self._evaluate_setup, symbol, "SCHEDULED")
                if view is not None and self.hub.symbol_of(conn_id) == symbol:
                    await self._send(conn_id, {"type": "setup", **view.model_dump()})
                    pv = await asyncio.to_thread(self._evaluate_proposal, symbol, "SCHEDULED", view)        # Stage 5.9: the review proposal follows the setup
                    if pv is not None and pv.effective_status != "NOT_ELIGIBLE" and self.hub.symbol_of(conn_id) == symbol:
                        await self._send(conn_id, {"type": "proposal", **pv.model_dump()})
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
        self._loop_ref = asyncio.get_running_loop()
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
                elif kind == "setup_eval":                                     # tests / harness only: force an evaluation through the REAL service path
                    if not self.allow_manual_tick:
                        await self._error(conn_id, "NOT_ALLOWED", "forced setup evaluations are only enabled in tests")
                        continue
                    await self._setup_eval(conn_id, msg)
                elif kind == "safety_eval":                                    # tests / harness only: force a safety evaluation (optional account overrides)
                    if not self.allow_manual_tick:
                        await self._error(conn_id, "NOT_ALLOWED", "forced safety evaluations are only enabled in tests")
                        continue
                    await self._safety_eval(conn_id, msg)
                elif kind == "proposal_eval":                                  # tests / harness only: force a proposal evaluation (optional account overrides)
                    if not self.allow_manual_tick:
                        await self._error(conn_id, "NOT_ALLOWED", "forced proposal evaluations are only enabled in tests")
                        continue
                    await self._proposal_eval(conn_id, msg)
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
        # Stage 5.8: opening a ticker is an explicit user action — evaluate it once (already-computed inputs; no extra research read)
        view = await asyncio.to_thread(self._evaluate_setup, s, "SUBSCRIBE", None, {"ppe": state.ppe, "demand": state.demand, "evidence": state.evidence, "quote": state.quote})
        if view is not None and view.decision == "TRANSITION" and self.hub.symbol_of(conn_id) == s:
            await self._send(conn_id, {"type": "setup", **view.model_dump()})
        # Stage 5.9: the review proposal follows the setup evaluation; sent only when it changed (the Stage 5.5 burst stays unchanged)
        pv = await asyncio.to_thread(self._evaluate_proposal, s, "SUBSCRIBE", view, {"quote": state.quote, "ppe": state.ppe, "position": state.position}) if view is not None else None
        if pv is not None and self.hub.symbol_of(conn_id) == s and pv.effective_status != "NOT_ELIGIBLE" and \
                (view.decision == "TRANSITION" or pv.proposal_id != state.proposal.proposal_id or pv.effective_status != state.proposal.effective_status):
            await self._send(conn_id, {"type": "proposal", **pv.model_dump()})

    async def _safety_eval(self, conn_id: int, msg: dict) -> None:
        account = msg.get("account")
        if account is not None and not isinstance(account, dict):
            await self._error(conn_id, "BAD_MESSAGE", "safety_eval account overrides must be an object")
            return
        if msg.get("clear"):
            self.safety.overrides = {}
        elif account is not None:
            self.safety.overrides = dict(account)
        view = await asyncio.to_thread(self.safety.evaluate, "TEST")
        await self._send(conn_id, {"type": "safety", **view.model_dump()})

    async def _proposal_eval(self, conn_id: int, msg: dict) -> None:
        raw = str(msg.get("symbol") or "")
        try:
            s = validate_symbol(raw)
            account = msg.get("account")
            if account is not None and not isinstance(account, dict):
                raise ValueError("account overrides must be an object")
        except Exception as exc:  # noqa: BLE001 - malformed fixture
            await self._error(conn_id, "BAD_MESSAGE", f"proposal_eval needs a symbol and optional account overrides ({type(exc).__name__})")
            return
        if msg.get("clear"):
            self.proposals.overrides.pop(s, None)
        elif account is not None:
            self.proposals.overrides[s] = dict(account)
        view = await asyncio.to_thread(self._evaluate_proposal, s, "TEST", None, None)
        if view is None:
            await self._error(conn_id, "EVALUATION_FAILED", "the forced proposal evaluation raised", symbol=s)
            return
        sent = set()
        for cid, symbol in self.hub.items():
            if symbol == s:
                await self._send(cid, {"type": "proposal", **view.model_dump()})
                sent.add(cid)
        if conn_id not in sent:
            await self._send(conn_id, {"type": "proposal", **view.model_dump()})

    async def _setup_eval(self, conn_id: int, msg: dict) -> None:
        raw = str(msg.get("symbol") or "")
        try:
            s = validate_symbol(raw)
            inputs = SetupInputs(**{"symbol": s, "evaluated_at": _iso(self.now_fn()), **dict(msg.get("inputs") or {})})
        except Exception as exc:  # noqa: BLE001 - malformed fixture
            await self._error(conn_id, "BAD_MESSAGE", f"setup_eval needs a symbol and valid inputs ({type(exc).__name__})")
            return
        view = await asyncio.to_thread(self._evaluate_setup, s, "TEST", inputs, None)
        if view is None:
            await self._error(conn_id, "EVALUATION_FAILED", "the forced evaluation raised; see setup_evaluations", symbol=s)
            return
        sent = set()
        for cid, symbol in self.hub.items():
            if symbol == s:
                await self._send(cid, {"type": "setup", **view.model_dump()})
                sent.add(cid)
        if conn_id not in sent:
            await self._send(conn_id, {"type": "setup", **view.model_dump()})

    async def _refresh(self, conn_id: int) -> None:
        s = self.hub.symbol_of(conn_id)
        if s is None:
            await self._error(conn_id, "NOT_SUBSCRIBED", "subscribe to a symbol first")
            return
        for adapter in (self.positions, self.ppe, self.demand):
            try:
                adapter.invalidate(s)
            except Exception:  # noqa: BLE001 - adapters without a cache
                pass
        pos = await asyncio.to_thread(self._position, s)
        ev = await asyncio.to_thread(self._evidence, s)
        res = await asyncio.to_thread(self._ppe, s)
        dem = await asyncio.to_thread(self._demand, s)
        if self.hub.symbol_of(conn_id) != s:
            return
        await self._send(conn_id, {"type": "position", **pos.model_dump()})
        await self._send(conn_id, {"type": "evidence", **ev.model_dump()})
        await self._send(conn_id, {"type": "ppe", **res.model_dump()})
        await self._send(conn_id, {"type": "demand", **dem.model_dump()})
        view = await asyncio.to_thread(self._evaluate_setup, s, "REFRESH", None, {"ppe": res, "demand": dem, "evidence": ev, "quote": None})   # Stage 5.8: explicit refresh evaluates
        if view is not None and self.hub.symbol_of(conn_id) == s:
            await self._send(conn_id, {"type": "setup", **view.model_dump()})
            pv = await asyncio.to_thread(self._evaluate_proposal, s, "REFRESH", view, {"ppe": res, "position": pos})    # Stage 5.9: the review proposal follows the setup
            if pv is not None and self.hub.symbol_of(conn_id) == s:
                await self._send(conn_id, {"type": "proposal", **pv.model_dump()})


def build_stream(mode: Optional[str] = None) -> TerminalStream:
    """Production wiring: the configured market adapter, the Stock Agent position sources and the stored-evidence reader."""
    from trader_view.positions import StockAgentPositionAdapter
    from trader_view.research import EvidenceResearchAdapter
    from execution.account_positions import view as account_view
    holder: Dict[str, TerminalStream] = {}
    from unified_portfolio.robinhood import get_source as robinhood_source
    s = TerminalStream(build_market_adapter(mode), StockAgentPositionAdapter(account_view=lambda: account_view(holder["s"].account_refresh, holder["s"].execution.store),
                                                                             robinhood_snapshot=lambda: robinhood_source().view(refresh=False)),   # Stage 6.9: cached, read only
                       EvidenceResearchAdapter(), allow_manual_tick=os.environ.get("STOCK_AGENT_ENV", "") == "browser_test")     # test-only socket commands (tick, setup_eval, proposal_eval, safety_eval) in the harness process only
    holder["s"] = s                                                                          # the account positions come from THIS stream's refresher and executor
    return s
