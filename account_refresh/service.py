"""
account_refresh/service.py - the ONE read-only refresh service (DESIGN_601 sections 2-11).

  refresh()          3 GETs (account, positions, open orders) through the Stage 6.0 paper client -> validate the complete candidate ->
                     commit atomically; bounded read retries; a failure keeps the last good snapshot and is recorded
  freshness()        FRESH | STARTING | STALE | UNAVAILABLE | DISABLED, derived from the snapshot's own refreshed_at, the consecutive
                     failures and whether THIS process has refreshed successfully (never from the application start time)
  view_payload()     the Stage 4.6A view shape (account / positions / orders / refreshed_at / connection) for the proposal account adapter
  make_safety_account_fn()   the Stage 5.9.1 gate's account source: fresh -> the refreshed equity; not fresh -> fail closed

Nothing here places, cancels or changes an order or a position, changes the execution mode or touches strategy state.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config
from account_refresh import DISCLAIMER, LABELS, REQUIRED_ACCOUNT_FIELDS, RETRYABLE_CODES, VERSION
from account_refresh.store import AccountRefreshStore

log = logging.getLogger(__name__)


class RefreshFailure(Exception):
    def __init__(self, code: str, detail: str = "", result: str = "FAILED"):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.result = code, detail, result


def _utc(dt: Optional[datetime] = None) -> datetime:
    d = dt or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _num(v, name: str, *, required: bool, minimum: Optional[Decimal] = None) -> Optional[str]:
    if v is None or v == "":
        if required:
            raise RefreshFailure("INVALID_PAYLOAD", f"{name} missing", "INVALID")
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise RefreshFailure("INVALID_PAYLOAD", f"{name} is not a number", "INVALID") from None
    if not d.is_finite() or (minimum is not None and d < minimum):
        raise RefreshFailure("INVALID_PAYLOAD", f"{name} out of range", "INVALID")
    return format(d, "f")


def _mask(number) -> Optional[str]:
    s = str(number or "")
    return ("****" + s[-4:]) if len(s) > 4 else ("****" if s else None)


def _default_broker():
    from execution.service import default_broker
    return default_broker()


def _backoff() -> List[float]:
    raw = str(getattr(config, "ACCOUNT_REFRESH_BACKOFF_SECONDS", "2,5"))
    out = []
    for part in raw.split(","):
        try:
            out.append(max(0.0, float(part)))
        except ValueError:
            pass
    return out or [2.0, 5.0]


class AccountRefreshService:
    def __init__(self, store: Optional[AccountRefreshStore] = None, *, broker=None, broker_fn: Callable[[], object] = _default_broker,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), sleep: Callable[[float], None] = time.sleep, owner: Optional[str] = None,
                 stale_after_s: Optional[float] = None, max_failures: Optional[int] = None, read_attempts: Optional[int] = None, backoff: Optional[List[float]] = None,
                 fault_file: Optional[Path] = None):
        self.store = store or AccountRefreshStore()
        self._broker = broker
        self.broker_fn = broker_fn
        self.clock = clock
        self.sleep = sleep
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stale_after_s = stale_after_s
        self._max_failures = max_failures
        self._read_attempts = read_attempts
        self._backoff = backoff
        self._fault_file = fault_file
        self._lock = threading.Lock()
        self.process_success_at: Optional[str] = None                   # a success by THIS process (startup rule)
        self.last_outcome: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------------- configuration ------------------------------------------
    @property
    def broker(self):
        if self._broker is None:
            self._broker = self.broker_fn()
        return self._broker

    @property
    def stale_after_s(self) -> float:
        return float(self._stale_after_s if self._stale_after_s is not None else config.ACCOUNT_REFRESH_STALE_SECONDS)

    @property
    def max_failures(self) -> int:
        return int(self._max_failures if self._max_failures is not None else config.ACCOUNT_REFRESH_MAX_FAILURES)

    @property
    def read_attempts(self) -> int:
        return max(1, int(self._read_attempts if self._read_attempts is not None else config.ACCOUNT_REFRESH_READ_ATTEMPTS))

    @property
    def backoff(self) -> List[float]:
        return list(self._backoff) if self._backoff is not None else _backoff()

    @property
    def fault_file(self) -> Optional[Path]:
        if self._fault_file is not None:
            return self._fault_file
        if not getattr(config, "ACCOUNT_REFRESH_TEST_FAULTS", False):
            return None
        raw = str(getattr(config, "ACCOUNT_REFRESH_FAULT_FILE", "") or "")
        return Path(raw) if raw else Path(config.PROJECT_ROOT) / "runtime" / "account_refresh.fault"

    def enabled(self) -> bool:
        """A paper broker is configured (paper credentials, or the deterministic simulated paper broker)."""
        try:
            return bool(self.broker.configured())
        except Exception:  # noqa: BLE001
            return False

    def source(self) -> str:
        return "SIMULATED_PAPER" if getattr(self.broker, "simulated", False) else "ALPACA_PAPER"

    # ---------------------------------------------------------------- the read -------------------------------------------------
    def _read_once(self) -> Dict[str, Any]:
        from execution import broker as BK
        f = self.fault_file
        if f is not None and f.exists():
            raise RefreshFailure("TEST_FAULT", f"test-mode fault file {f.name} present (no broker call)")
        b = self.broker
        b.reset_budget()
        try:
            acct = b._get("/v2/account")
            positions = b._get("/v2/positions")
            orders = b._get("/v2/orders", params={"status": "open", "limit": 500, "direction": "desc"})
        except BK.BrokerError as exc:
            raise RefreshFailure(exc.code, exc.detail) from None
        return self._validate(acct, positions, orders, BK.PAPER_HOST)

    def _validate(self, acct, positions, orders, host: str) -> Dict[str, Any]:
        if not isinstance(acct, dict):
            raise RefreshFailure("INVALID_PAYLOAD", "account is not an object", "INVALID")
        missing = [k for k in REQUIRED_ACCOUNT_FIELDS if k not in acct or acct.get(k) is None]
        if missing:
            raise RefreshFailure("INVALID_PAYLOAD", f"account fields missing: {', '.join(missing)}", "INVALID")
        if not isinstance(acct.get("status"), str) or not acct["status"].strip():
            raise RefreshFailure("INVALID_PAYLOAD", "account status invalid", "INVALID")
        for flag in ("trading_blocked", "account_blocked"):
            if not isinstance(acct.get(flag), bool):
                raise RefreshFailure("INVALID_PAYLOAD", f"{flag} is not a boolean", "INVALID")
        zero = Decimal("0")
        account = {"status": acct["status"].strip().upper(), "currency": acct.get("currency"), "equity": _num(acct.get("equity"), "equity", required=True, minimum=zero),
                   "last_equity": _num(acct.get("last_equity"), "last_equity", required=False), "cash": _num(acct.get("cash"), "cash", required=True),
                   "buying_power": _num(acct.get("buying_power"), "buying_power", required=True, minimum=zero),
                   "portfolio_value": _num(acct.get("portfolio_value"), "portfolio_value", required=False),
                   "long_market_value": _num(acct.get("long_market_value"), "long_market_value", required=False),
                   "short_market_value": _num(acct.get("short_market_value"), "short_market_value", required=False),
                   "trading_blocked": bool(acct["trading_blocked"]), "account_blocked": bool(acct["account_blocked"]),
                   "transfers_blocked": bool(acct.get("transfers_blocked")) if acct.get("transfers_blocked") is not None else None,
                   "trade_suspended_by_user": bool(acct.get("trade_suspended_by_user")) if acct.get("trade_suspended_by_user") is not None else None,
                   "pattern_day_trader": bool(acct.get("pattern_day_trader")) if acct.get("pattern_day_trader") is not None else None,
                   "created_at": acct.get("created_at"), "account_number_masked": _mask(acct.get("account_number"))}
        if not isinstance(positions, list):
            raise RefreshFailure("INVALID_PAYLOAD", "positions is not a list", "INVALID")
        pos = []
        for p in positions:
            if not isinstance(p, dict) or not p.get("symbol"):
                raise RefreshFailure("INVALID_PAYLOAD", "a position has no symbol", "INVALID")
            qty = _num(p.get("qty"), f"{p.get('symbol')} qty", required=True)
            side = str(p.get("side") or "long").lower()
            q = abs(Decimal(qty))
            pos.append({"symbol": str(p["symbol"]).upper(), "qty": format(-q if side == "short" else q, "f"), "side": side,
                        "avg_entry_price": _num(p.get("avg_entry_price"), "avg_entry_price", required=False), "cost_basis": _num(p.get("cost_basis"), "cost_basis", required=False),
                        "market_value": _num(p.get("market_value"), "market_value", required=False), "current_price": _num(p.get("current_price"), "current_price", required=False),
                        "unrealized_pl": _num(p.get("unrealized_pl"), "unrealized_pl", required=False), "unrealized_plpc": _num(p.get("unrealized_plpc"), "unrealized_plpc", required=False)})
        if not isinstance(orders, list):
            raise RefreshFailure("INVALID_PAYLOAD", "open orders is not a list", "INVALID")
        oo = []
        for o in orders:
            if not isinstance(o, dict) or not o.get("id") or not o.get("symbol"):
                raise RefreshFailure("INVALID_PAYLOAD", "an open order has no id or symbol", "INVALID")
            oo.append({"id_short": str(o["id"])[:8], "client_order_id": o.get("client_order_id"), "symbol": str(o["symbol"]).upper(), "side": o.get("side"),
                       "qty": _num(o.get("qty"), "order qty", required=False), "notional": _num(o.get("notional"), "notional", required=False),
                       "type": o.get("type") or o.get("order_type"), "time_in_force": o.get("time_in_force"), "status": o.get("status"),
                       "submitted_at": o.get("submitted_at"), "filled_at": o.get("filled_at"), "filled_qty": _num(o.get("filled_qty"), "filled_qty", required=False),
                       "filled_avg_price": _num(o.get("filled_avg_price"), "filled_avg_price", required=False)})
        return {"account": account, "positions": pos, "open_orders": oo, "host": host}

    def refresh(self, now: Optional[datetime] = None, trigger: str = "SCHEDULED") -> Dict[str, Any]:
        """One complete read-only refresh. Never overlaps itself; never writes when no paper broker is configured."""
        if not self._lock.acquire(blocking=False):
            return {"result": "SKIPPED_RUNNING", "trigger": trigger}
        try:
            if not self.enabled():
                out = {"result": "NOT_CONFIGURED", "trigger": trigger}
                self.last_outcome = out
                return out
            started = _utc(now or self.clock())
            attempts, failure, cand = 0, None, None
            delays = self.backoff
            for i in range(self.read_attempts):
                attempts = i + 1
                try:
                    cand = self._read_once()
                    failure = None
                    break
                except RefreshFailure as exc:
                    failure = exc
                    retry = exc.result == "FAILED" and (exc.code in RETRYABLE_CODES or str(exc.code).startswith("HTTP_5"))
                    if not retry or attempts >= self.read_attempts:
                        break
                    self.sleep(delays[min(i, len(delays) - 1)])
                except Exception as exc:  # noqa: BLE001 - an unexpected read failure is a failed refresh, never a crash
                    failure = RefreshFailure("READ_ERROR", type(exc).__name__)
                    break
            finished = _utc(self.clock()) if now is None else started
            if failure is None and cand is not None:
                rid = uuid.uuid4().hex
                snap = {"refresh_id": rid, "refreshed_at": _iso(finished), "source": self.source(), **cand}
                self.store.commit_success(snap, {"started_at": _iso(started), "finished_at": _iso(finished), "trigger": trigger, "result": "SUCCESS",
                                                 "read_attempts": attempts, "refresh_id": rid, "owner": self.owner}, finished)
                self.process_success_at = snap["refreshed_at"]
                out = {"result": "SUCCESS", "trigger": trigger, "refresh_id": rid, "refreshed_at": snap["refreshed_at"], "read_attempts": attempts}
            else:
                prev = self.store.snapshot()
                d = _parse(prev["refreshed_at"]) if prev else None
                age = max(0.0, (finished - d).total_seconds()) if d else None
                self.store.commit_failure({"started_at": _iso(started), "finished_at": _iso(finished), "trigger": trigger, "result": failure.result, "read_attempts": attempts,
                                           "error_code": failure.code, "error_text": failure.detail, "owner": self.owner}, finished, age)
                log.warning("account refresh %s after %d read attempt(s): %s", failure.result, attempts, failure.code)
                out = {"result": failure.result, "trigger": trigger, "error_code": failure.code, "error_text": failure.detail, "read_attempts": attempts}
            self.last_outcome = out
            return out
        finally:
            self._lock.release()

    # ---------------------------------------------------------------- freshness / views ------------------------------------
    def freshness(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _utc(now or self.clock())
        enabled = self.enabled()
        snap = self.store.snapshot()
        live = self.store.liveness()
        d = _parse(snap["refreshed_at"]) if snap else None
        age = round(max(0.0, (now - d).total_seconds()), 1) if d else None
        cf = int(live.get("consecutive_failures") or 0)
        if not enabled:
            status, reason = "DISABLED", "no paper broker configured"
        elif snap is None:
            status, reason = "UNAVAILABLE", "no successful refresh yet"
        elif self.process_success_at is None:
            status, reason = "STARTING", "waiting for the first refresh of this process"
        elif cf >= self.max_failures:
            status, reason = "STALE", f"{cf} consecutive failed refreshes"
        elif age is not None and age > self.stale_after_s:
            status, reason = "STALE", f"last successful refresh {int(age)} s ago (limit {int(self.stale_after_s)} s)"
        else:
            status, reason = "FRESH", ""
        label = {"FRESH": f"Account data: Fresh ({int(age or 0)}s ago)", "STARTING": "Account data: Checking", "STALE": "Account data: Stale",
                 "UNAVAILABLE": "Account data unavailable", "DISABLED": "Account data unavailable"}[status]
        return {"version": VERSION, "status": status, "fresh": status == "FRESH", "label": label, "short_label": LABELS[status], "reason": reason,
                "age_seconds": age, "refreshed_at": snap["refreshed_at"] if snap else None, "source": snap["source"] if snap else (self.source() if enabled else None),
                "last_success_at": live.get("last_success_at"), "last_attempt_at": live.get("last_attempt_at"), "last_result": live.get("last_result"),
                "last_error": live.get("last_error"), "consecutive_failures": cf, "attempt_count": int(live.get("attempt_count") or 0),
                "success_count": int(live.get("success_count") or 0), "failure_count": int(live.get("failure_count") or 0), "stale_after_s": self.stale_after_s,
                "max_failures": self.max_failures, "process_refreshed": self.process_success_at is not None}

    def view_payload(self) -> Dict[str, Any]:
        """The Stage 4.6A view shape (the keys the proposal account adapter reads). A stale snapshot keeps its true refreshed_at."""
        fr = self.freshness()
        snap = self.store.snapshot()
        if snap is None:
            return {"connection": {"state": fr["status"]}, "refreshed_at": None, "account": None, "positions": [], "orders": [], "source": "account_refresh", "freshness": fr}
        return {"connection": {"state": "CONNECTED" if fr["fresh"] else fr["status"]}, "refreshed_at": snap["refreshed_at"], "account": snap["account"],
                "positions": snap["positions"], "orders": snap["open_orders"], "source": "account_refresh", "freshness": fr}

    def overview(self) -> Dict[str, Any]:
        fr = self.freshness()
        snap = self.store.snapshot()
        acct = (snap or {}).get("account") or {}
        summary = None
        if snap:
            summary = {k: acct.get(k) for k in ("status", "trading_blocked", "account_blocked", "transfers_blocked", "trade_suspended_by_user", "currency", "equity",
                                                "cash", "buying_power", "account_number_masked")}
            summary.update(positions=len(snap["positions"]), open_orders=len(snap["open_orders"]), source=snap["source"], host=snap["host"])
        try:
            from account_refresh.scheduler import status as loop_status
            loop = loop_status()
        except Exception:  # noqa: BLE001
            loop = {}
        return {**fr, "snapshot": summary, "loop": loop, "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------- consumers (wired by trader_view/stream.py) ------------------------
def make_alpaca_view(refresher: AccountRefreshService) -> Callable[[], dict]:
    """For the Stage 5.9 proposal account adapter: the refreshed snapshot when a paper broker is configured, else the Stage 4.6A view."""
    def view() -> dict:
        if refresher.enabled():
            return refresher.view_payload()
        from proposals import account as PA
        return PA._default_alpaca_view()
    return view


def make_safety_account_fn(stream_fn: Callable[[], object], refresher: AccountRefreshService, path: Path) -> Callable[[float], object]:
    """For the Stage 5.9.1 gate (its `account_fn` seam): identical to the gate's own default source, except that a configured refresher which
    is not FRESH withholds the snapshot age (Alpaca source -> ACCOUNT_SNAPSHOT_STALE) or the equity (other sources -> EQUITY_NOT_AVAILABLE).
    Either way: New trades paused. A failed refresh can never read as Trading allowed."""
    def account(now_ts: float):
        from proposals.account import AccountAdapter
        from safety.models import AccountEquity
        from safety.service import NO_CASH_FLOW_SOURCES
        try:
            mode = stream_fn().market.mode
        except Exception:  # noqa: BLE001
            mode = "simulated"
        snap = AccountAdapter(path=path, alpaca_view=make_alpaca_view(refresher)).snapshot("", mode, now_ts)
        acct = AccountEquity(equity=snap.equity, source=snap.equity_source, as_of=snap.equity_as_of, age_s=snap.equity_age_s, mode=mode,
                             adjustments="NONE_POSSIBLE" if snap.equity_source in NO_CASH_FLOW_SOURCES else "UNAVAILABLE", notes=list(snap.notes))
        if refresher.enabled():
            fr = refresher.freshness()
            if not fr["fresh"]:
                note = f"account data {fr['status'].lower()}: {fr['reason']}"
                if acct.source == "ALPACA_PAPER_VIEW":
                    acct = acct.model_copy(update={"age_s": None, "notes": acct.notes + [note]})
                else:
                    acct = acct.model_copy(update={"equity": None, "notes": acct.notes + [note]})
        return acct
    return account
