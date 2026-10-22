"""
proposals/account.py — the account snapshot a proposal is sized against (DESIGN_59 §1, §7, §10). Built ONLY from existing read-only
views, with ZERO network calls: the Stage 4.6A Alpaca Paper snapshot (`paper.alpaca_view.view()`, refreshed only by the user), the
Stage 4.5 local paper view (`paper.portfolio.view()`), the frozen Stage 4.6B intent store (read only) and the review-only halt flags.
Nothing here places, previews, submits or cancels anything, and nothing is faked: a source that is missing says so.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config
from proposals.models import AccountSnapshot

log = logging.getLogger(__name__)
OPEN_BROKER_STATUSES = ("new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "held", "pending_replace", "pending_cancel")
SIMULATED_MODES = ("simulated", "test", "replay")


def _dec(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        d = Decimal(str(x).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _s(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else format(d, "f")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _default_alpaca_view() -> dict:
    from paper import alpaca_view as AV                       # Stage 4.6A: the in-memory snapshot, 0 broker calls
    return AV.view()


def _default_local_view() -> dict:
    from paper import portfolio as P                          # Stage 4.5: reads the local database only (the Trader View position adapter does the same)
    return P.view()


def _default_intents(path: Optional[Path], symbol: str) -> List[str]:
    """Unresolved or live Stage 4.6B intents in the symbol (the rotation/engine.py::default_conflicts pattern; read only)."""
    from paper import alpaca_order_rules as RU                # pure state constants of the frozen module
    from paper import alpaca_order_store as AOS               # read-only store of the frozen module (never creates tables)
    st = AOS.OrderStore(path) if path else AOS.OrderStore()
    if not st.exists():
        return []
    return [f"4.6B intent {r['intent_id']} {r['state']}" for r in st.in_states(RU.UNRESOLVED | RU.LIVE) if str(r.get("symbol", "")).upper() == symbol]


class AccountAdapter:
    def __init__(self, path: Optional[Path] = None, *, alpaca_view: Callable[[], dict] = _default_alpaca_view, local_view: Callable[[], dict] = _default_local_view,
                 intents: Callable[[Optional[Path], str], List[str]] = _default_intents):
        self.path = Path(path) if path else None
        self.alpaca_view, self.local_view, self.intents = alpaca_view, local_view, intents

    # ---------------------------------------------------------------- the snapshot ------------------------------------------
    def snapshot(self, symbol: str, mode: str, now_ts: float, overrides: Optional[Dict[str, Any]] = None) -> AccountSnapshot:
        s = symbol.upper()
        snap = AccountSnapshot(halt=bool(config.TRADE_PROPOSAL_HALT), halt_source="config.TRADE_PROPOSAL_HALT" if config.TRADE_PROPOSAL_HALT else "config.TRADE_PROPOSAL_HALT (off)",
                               daily_loss_halt=bool(config.TRADE_PROPOSAL_DAILY_LOSS_HALT),
                               daily_loss_source="config.TRADE_PROPOSAL_DAILY_LOSS_HALT" if config.TRADE_PROPOSAL_DAILY_LOSS_HALT else "config.TRADE_PROPOSAL_DAILY_LOSS_HALT (off; no automatic daily P&L exists)",
                               tradable=None, tradability_source="no zero-call broker asset metadata (the 4.6B form checks tradability at preview time)")
        if mode in SIMULATED_MODES:
            snap = self._hypothetical(snap)
        else:
            snap = self._alpaca(snap, s, now_ts)
            if snap.equity_source == "NOT_AVAILABLE":
                snap = self._local(snap, s)
            snap = self._intents(snap, s)
        if overrides:
            snap = snap.model_copy(update={k: v for k, v in overrides.items() if k in AccountSnapshot.model_fields})
            snap.notes.append("test overrides applied: " + ", ".join(sorted(k for k in overrides if k in AccountSnapshot.model_fields)))
        return snap

    # ---------------------------------------------------------------- sources -------------------------------------------------
    @staticmethod
    def _hypothetical(snap: AccountSnapshot) -> AccountSnapshot:
        eq = _dec(config.TRADE_PROPOSAL_REVIEW_EQUITY)
        if eq is not None and eq > 0:
            money = eq.quantize(Decimal("0.01"))
            return snap.model_copy(update={"equity": _s(money), "equity_source": "HYPOTHETICAL_REVIEW", "buying_power": _s(money), "cash": _s(money),
                                           "buying_power_source": "HYPOTHETICAL_REVIEW (cash account assumption: buying power = equity)",
                                           "pending_source": "none (hypothetical review account has no orders)", "position_source": "none (hypothetical review account has no positions)",
                                           "notes": snap.notes + ["simulated mode: equity is config.TRADE_PROPOSAL_REVIEW_EQUITY"]})
        return snap.model_copy(update={"equity_source": "NOT_AVAILABLE", "notes": snap.notes + ["simulated mode: set TRADE_PROPOSAL_REVIEW_EQUITY to size review proposals"]})

    def _alpaca(self, snap: AccountSnapshot, s: str, now_ts: float) -> AccountSnapshot:
        try:
            v = self.alpaca_view() or {}
        except Exception as exc:  # noqa: BLE001 - a failing view is reported, never guessed
            return snap.model_copy(update={"notes": snap.notes + [f"Alpaca Paper view unavailable ({type(exc).__name__})"]})
        acct = v.get("account") or {}
        eq = _dec(acct.get("equity"))
        if eq is None or eq <= 0:
            state = ((v.get("connection") or {}).get("state")) or "UNKNOWN"
            return snap.model_copy(update={"notes": snap.notes + [f"Alpaca Paper view has no account snapshot (connection {state}; refresh it in the Alpaca Paper tab)"]})
        as_of = v.get("refreshed_at")
        d = _parse(as_of)
        age = max(0.0, now_ts - d.timestamp()) if d else None
        upd: Dict[str, Any] = {"equity": _s(eq.quantize(Decimal("0.01"))), "equity_source": "ALPACA_PAPER_VIEW", "equity_as_of": as_of, "equity_age_s": age,
                               "buying_power": _s(_dec(acct.get("buying_power"))), "buying_power_source": "ALPACA_PAPER_VIEW buying_power", "cash": _s(_dec(acct.get("cash"))),
                               "position_source": "ALPACA_PAPER_VIEW", "pending_source": "ALPACA_PAPER_VIEW open orders + 4.6B intents"}
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            upd.update(halt=True, halt_source="Alpaca account blocked (trading_blocked / account_blocked)")
        for p in v.get("positions") or []:
            if str(p.get("symbol", "")).upper() == s:
                qty = _dec(p.get("qty"))
                if qty is not None and qty > 0:
                    upd.update(existing_qty=_s(qty), existing_avg_cost=_s(_dec(p.get("avg_entry_price"))), existing_notional=_s(_dec(p.get("market_value"))))
                elif qty is not None and qty < 0:
                    upd["notes"] = snap.notes + ["a SHORT position exists in the Alpaca paper account; long review proposals do not size against it"]
        pending = [f"Alpaca order {o.get('id_short') or ''} {o.get('status')}" for o in (v.get("orders") or [])
                   if str(o.get("symbol", "")).upper() == s and str(o.get("status", "")).lower() in OPEN_BROKER_STATUSES]
        upd["pending_detail"] = list(snap.pending_detail) + pending
        upd["pending_orders"] = snap.pending_orders + len(pending)
        return snap.model_copy(update=upd)

    def _local(self, snap: AccountSnapshot, s: str) -> AccountSnapshot:
        try:
            v = self.local_view() or {}
        except Exception as exc:  # noqa: BLE001
            return snap.model_copy(update={"notes": snap.notes + [f"local paper portfolio unavailable ({type(exc).__name__})"]})
        if not v.get("account"):
            return snap.model_copy(update={"notes": snap.notes + ["no local paper portfolio is set up"]})
        summ = v.get("summary") or {}
        eq = _dec(summ.get("equity"))
        if eq is None or eq <= 0:
            return snap.model_copy(update={"notes": snap.notes + ["local paper equity unavailable (a holding is unmarked)"]})
        upd: Dict[str, Any] = {"equity": _s(eq.quantize(Decimal("0.01"))), "equity_source": "LOCAL_PAPER", "equity_as_of": summ.get("mark_session"), "cash": _s(_dec(summ.get("cash"))),
                               "buying_power": _s(_dec(summ.get("cash"))), "buying_power_source": "LOCAL_PAPER cash (no margin)", "position_source": "LOCAL_PAPER",
                               "pending_source": "LOCAL_PAPER pending orders + 4.6B intents"}
        for p in v.get("positions") or []:
            if str(p.get("symbol", "")).upper() == s and _dec(p.get("shares")) and _dec(p.get("shares")) > 0:
                upd.update(existing_qty=_s(_dec(p.get("shares"))), existing_avg_cost=_s(_dec(p.get("average_cost"))), existing_notional=_s(_dec(p.get("market_value"))))
        pending = [f"local paper order {o.get('order_id') or ''} PENDING" for o in (v.get("pending_orders") or []) if str(o.get("symbol", "")).upper() == s]
        upd["pending_detail"] = list(snap.pending_detail) + pending
        upd["pending_orders"] = snap.pending_orders + len(pending)
        return snap.model_copy(update=upd)

    def _intents(self, snap: AccountSnapshot, s: str) -> AccountSnapshot:
        try:
            rows = self.intents(self.path, s)
        except Exception as exc:  # noqa: BLE001
            return snap.model_copy(update={"notes": snap.notes + [f"4.6B intent store unreadable ({type(exc).__name__})"]})
        if not rows:
            return snap
        return snap.model_copy(update={"pending_detail": list(snap.pending_detail) + rows, "pending_orders": snap.pending_orders + len(rows)})
