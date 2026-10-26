"""
unified_portfolio/robinhood.py - Robinhood as a READ-ONLY portfolio source.

  RobinhoodPortfolioAdapter   wraps the loopback rh_gateway client (portfolio.provider.RobinhoodGatewayPortfolioProvider) and exposes exactly
                              ONE operation, read(): GET /portfolio + GET /positions. It has no buy / sell / cancel / replace / submit /
                              close method, no generic call, and keeps no credential (the gateway holds the Robinhood OAuth token; this app
                              holds only the gateway's local secret, read by the provider per request and never stored here)
  RobinhoodSource             the cached, normalized snapshot and its state:
                                NOT_CONFIGURED (ROBINHOOD_PORTFOLIO_ENABLED=false, or the gateway is not set up on this machine) |
                                AUTH_REQUIRED (the gateway needs a new Robinhood login, or it rejected this app) | NOT_LOADED (never read) |
                                LOADED | STALE (old, marked STALE by the gateway, or the last good snapshot kept after a failed read) | ERROR
                              `positions` is None unless a snapshot exists: Robinhood being unavailable is never "no holdings".

Every Robinhood row is USER_MANAGED: visible, counted in exposure, never bot-managed, never exited, never resized. Nothing is written to a
database or a log except the reason code of a failed read.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import config
from portfolio.analytics import build_view
from portfolio.provider import PortfolioUnavailable
from unified_portfolio import (ACCOUNT_TYPE, AUTH_REQUIRED, ERROR, LOADED, NOT_CONFIGURED, NOT_LOADED, OWNERSHIP_LABELS, ROBINHOOD, ROBINHOOD_FIXTURE,
                               SOURCE_LABELS, STALE, USER_MANAGED)

log = logging.getLogger(__name__)
_AUTH = {"REAUTH_REQUIRED": "Robinhood needs a new login in the local gateway (python -m rh_gateway login). Nothing was read.",
         "GATEWAY_REJECTED": "The local gateway rejected this app (secret or host check). Nothing was read."}
_NOT_SET_UP = {"GATEWAY_SECRET_UNAVAILABLE": "The local Robinhood gateway is not set up for this app on this machine. Nothing was read.",
               "DISABLED": "Robinhood portfolio reads are turned off."}


class RobinhoodPortfolioAdapter:
    """READ ONLY: one operation, two GETs. The provider is held privately and only its two read methods are ever called."""
    __slots__ = ("_provider",)
    READS = ("get_portfolio", "get_positions")

    def __init__(self, provider: Any):
        self._provider = provider

    def read(self) -> Dict[str, Any]:
        return {"portfolio": self._provider.get_portfolio(), "positions": self._provider.get_positions()}


def _utc(dt: Optional[datetime] = None) -> datetime:
    d = dt or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _s(v) -> Optional[str]:
    return None if v is None else format(v, "f") if hasattr(v, "as_tuple") else str(v)


def default_provider():
    """The configured read path: the synthetic fixture, or the loopback rh_gateway (the only component that talks to Robinhood)."""
    if config.ROBINHOOD_PORTFOLIO_SOURCE == "fixture":
        from unified_portfolio.fixture import FixturePortfolioProvider
        return FixturePortfolioProvider(config.ROBINHOOD_PORTFOLIO_FIXTURE)
    from portfolio.provider import RobinhoodGatewayPortfolioProvider
    return RobinhoodGatewayPortfolioProvider(config.RH_GATEWAY_URL, config.PORTFOLIO_ACCOUNT_ALIAS, timeout=min(float(config.RH_GATEWAY_TIMEOUT_SECONDS), 15.0))


class RobinhoodSource:
    def __init__(self, *, enabled_fn: Callable[[], bool] = lambda: bool(config.ROBINHOOD_PORTFOLIO_ENABLED), provider_fn: Callable[[], Any] = default_provider,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc), ttl_s: Optional[float] = None, stale_after_s: Optional[float] = None,
                 sector_fn: Optional[Callable[[str], Optional[str]]] = None):
        self.enabled_fn, self.provider_fn, self.now_fn = enabled_fn, provider_fn, now_fn
        self.ttl_s = float(ttl_s if ttl_s is not None else config.ROBINHOOD_PORTFOLIO_TTL_SECONDS)
        self.stale_after_s = float(stale_after_s if stale_after_s is not None else config.FRESHNESS_PORTFOLIO_STALE_SECONDS)
        if sector_fn is None:
            from portfolio.sectors import default_resolver
            sector_fn = default_resolver.sector_for
        self.sector_fn = sector_fn
        self._lock = threading.Lock()
        self.lock_wait_s = 2.0                                                              # a request never queues behind a slow read for longer
        self._last_good: Optional[Dict[str, Any]] = None      # the last normalized snapshot (memory only; never written to a database)
        self._last_attempt: Optional[Dict[str, Any]] = None   # {"at", "ok", "reason", "message"}
        self.reads = 0

    def _kind(self, provider: Any) -> str:
        return ROBINHOOD_FIXTURE if getattr(provider, "synthetic", False) else ROBINHOOD

    def _normalize(self, raw: Dict[str, Any], source: str, now: datetime) -> Dict[str, Any]:
        pf, ps = raw["portfolio"], raw["positions"]
        # incomplete data is a FAILED read, never "no holdings": missing payloads, a missing positions list, or a truncated page
        if not isinstance(pf.data, dict) or not isinstance(ps.data, dict) or not isinstance(ps.data.get("positions"), list):
            raise PortfolioUnavailable("DATA_INCOMPLETE", "The gateway returned no complete portfolio / positions data; holdings unknown.")
        if pf.truncated or ps.truncated:
            raise PortfolioUnavailable("DATA_INCOMPLETE", "The gateway returned a truncated positions page; holdings incomplete, not used.")
        view = build_view(pf.data, {"fetched_at": pf.fetched_at}, ps.data, {"fetched_at": ps.fetched_at}, None, now=now,
                          stale_after_s=int(config.PORTFOLIO_QUOTE_STALE_SECONDS), gap_material_pct=float(config.PORTFOLIO_VALUATION_GAP_MATERIAL_PCT),
                          sector_fn=self.sector_fn)
        snap = view.snapshot
        acct = ((pf.data or {}).get("account") or {})
        label = f"{SOURCE_LABELS[source]} · {acct.get('alias') or config.PORTFOLIO_ACCOUNT_ALIAS} {acct.get('masked_id') or ''}".strip()
        refreshed = min((t for t in (pf.fetched_at, ps.fetched_at) if t), default=None) or now.isoformat(timespec="seconds")
        rows: List[Dict[str, Any]] = []
        for p in view.positions:
            rows.append({"symbol": str(p.symbol or "").upper(), "instrument_id": None, "quantity": _s(p.quantity.normalize()) if p.quantity is not None else None, "average_entry_price": _s(p.avg_cost), "current_price": _s(p.last_price),
                         "market_value": _s(p.market_value), "unrealized_pl": _s(p.unrealized_pnl), "unrealized_pl_pct": _s(p.unrealized_pnl_pct),
                         "price_quality": p.quote_quality, "price_at": p.price_timestamp.isoformat() if p.price_timestamp else None, "sector": p.sector,
                         "ownership": USER_MANAGED, "ownership_label": OWNERSHIP_LABELS[USER_MANAGED], "managed": False})
        return {"source": source, "account_label": label, "refreshed_at": refreshed, "gateway_status": "STALE" if "STALE" in (pf.status, ps.status) else "OK",
                "equity": _s(snap.equity_value), "total_value": _s(snap.total_value), "cash": _s(snap.cash), "buying_power": _s(snap.buying_power),
                "currency": snap.currency, "valuation_complete": snap.valuation_complete, "positions": rows}

    def refresh(self, only_if_due: bool = False) -> Dict[str, Any]:
        """One read (two GETs). A failure keeps the last good snapshot and records only a reason code. Never concurrent: a caller that cannot
        get the lock within lock_wait_s gets SKIPPED_IN_FLIGHT and serves the cached view."""
        now = _utc(self.now_fn())
        if not self.enabled_fn():
            return {"result": NOT_CONFIGURED}
        if not self._lock.acquire(timeout=self.lock_wait_s):                               # another read is in flight: the caller serves the cached view
            return {"result": "SKIPPED_IN_FLIGHT"}
        try:
            if only_if_due and not self.due():                                            # checked again under the lock: at most one read per TTL
                return {"result": "SKIPPED_FRESH"}
            now = _utc(self.now_fn())
            try:
                provider = self.provider_fn()
            except ValueError as exc:                                                     # invalid gateway URL / alias: a configuration problem
                self._last_attempt = {"at": now.isoformat(timespec="seconds"), "ok": False, "reason": "CONFIG_INVALID", "message": f"Robinhood configuration invalid: {exc}"}
                return {"result": "FAILED", "reason": "CONFIG_INVALID"}
            except Exception as exc:  # noqa: BLE001 - any construction failure is a recorded failed read (throttled by the TTL), never an HTTP 500
                self._last_attempt = {"at": now.isoformat(timespec="seconds"), "ok": False, "reason": "PROVIDER_ERROR", "message": f"Robinhood source unavailable ({type(exc).__name__})."}
                return {"result": "FAILED", "reason": "PROVIDER_ERROR"}
            try:
                self.reads += 1
                snap = self._normalize(RobinhoodPortfolioAdapter(provider).read(), self._kind(provider), now)
            except PortfolioUnavailable as exc:
                self._last_attempt = {"at": now.isoformat(timespec="seconds"), "ok": False, "reason": exc.reason, "message": exc.message}
                log.warning("robinhood portfolio read failed (%s)", exc.reason)
                return {"result": "FAILED", "reason": exc.reason}
            except Exception as exc:  # noqa: BLE001 - an unexpected payload is a failed read, never a crash
                self._last_attempt = {"at": now.isoformat(timespec="seconds"), "ok": False, "reason": "READ_ERROR", "message": f"Unreadable Robinhood data ({type(exc).__name__})."}
                log.warning("robinhood portfolio read failed (%s)", type(exc).__name__)
                return {"result": "FAILED", "reason": "READ_ERROR"}
            self._last_good = snap
            self._last_attempt = {"at": now.isoformat(timespec="seconds"), "ok": True, "reason": None, "message": None}
            return {"result": "SUCCESS", "refreshed_at": snap["refreshed_at"]}
        finally:
            self._lock.release()

    def due(self) -> bool:
        att = _parse((self._last_attempt or {}).get("at"))
        return att is None or (_utc(self.now_fn()) - att).total_seconds() >= self.ttl_s

    def view(self, refresh: bool = False) -> Dict[str, Any]:
        """The source's state. refresh=True reads first when the last attempt is older than the TTL (the unified routes); False never calls."""
        if refresh and self.enabled_fn() and self.due():
            self.refresh(only_if_due=True)
        now = _utc(self.now_fn())
        base: Dict[str, Any] = {"source": ROBINHOOD, "account_type": ACCOUNT_TYPE[ROBINHOOD], "label": SOURCE_LABELS[ROBINHOOD], "positions": None, "refreshed_at": None, "age_s": None,
                                "last_attempt_at": (self._last_attempt or {}).get("at"), "reads": self.reads, "write_capable": False}
        if not self.enabled_fn():
            return {**base, "state": NOT_CONFIGURED, "message": "Robinhood portfolio reads are off (ROBINHOOD_PORTFOLIO_ENABLED=false): real holdings are not included - unknown, not zero."}
        att, good = self._last_attempt, self._last_good
        if good is None:
            if att is None:
                return {**base, "state": NOT_LOADED, "message": "Not loaded yet: Robinhood holdings are unknown until the first read."}
            reason = att.get("reason") or "ERROR"
            if reason in _AUTH:
                return {**base, "state": AUTH_REQUIRED, "reason": reason, "message": _AUTH[reason]}
            if reason in _NOT_SET_UP or reason == "CONFIG_INVALID":
                return {**base, "state": NOT_CONFIGURED, "reason": reason, "message": _NOT_SET_UP.get(reason) or att.get("message")}
            return {**base, "state": ERROR, "reason": reason, "message": f"Robinhood read failed ({reason}): holdings unknown, not zero. {att.get('message') or ''}".strip()}
        refreshed = _parse(good["refreshed_at"])
        age = round(max(0.0, (now - refreshed).total_seconds()), 1) if refreshed else None
        out = {**base, **{k: v for k, v in good.items() if k != "positions"}, "positions": [dict(r) for r in good["positions"]], "age_s": age,
               "label": SOURCE_LABELS[good["source"]], "account_type": ACCOUNT_TYPE[good["source"]]}
        failed_after = att is not None and not att.get("ok")
        if failed_after or good["gateway_status"] == "STALE" or (age is not None and age > self.stale_after_s):
            why = (f"the last read failed ({att.get('reason')}); showing the last known snapshot" if failed_after
                   else "the gateway marked it stale" if good["gateway_status"] == "STALE" else f"last read {int(age)} s ago")
            return {**out, "state": STALE, "reason": att.get("reason") if failed_after else None, "message": f"Stale: {why}, from {good['refreshed_at']}."}
        return {**out, "state": LOADED, "message": f"{len(good['positions'])} holding(s), read at {good['refreshed_at']}."}


_SOURCE: Optional[RobinhoodSource] = None
_GUARD = threading.Lock()


def get_source() -> RobinhoodSource:
    """The process-wide Robinhood source (tests build their own)."""
    global _SOURCE
    with _GUARD:
        if _SOURCE is None:
            _SOURCE = RobinhoodSource()
        return _SOURCE
