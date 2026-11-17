"""md_eval/ledger.py - the cash ledger (contract P3, P3a; amendment A3, A5). It books actual cash flows from RAW prices and explicit events only:
  - fills: whole shares; cash debited / credited at raw_open x shares plus costs; an exit closes the whole position; cost basis includes costs;
  - split / reverse split / stock dividend: shares x ratio, the TOTAL basis kept, cash unchanged; a fraction paid at cash_in_lieu_per_share,
    else valued at 0 and FRACTION_UNPAID; carried price levels (stop, target, the valuation mark) divided by the ratio (A3);
  - cash dividend: entitled = the shares held at the corporate-action step of the ex-date (= at the previous close); credited once on
    pay_session; UNKNOWN pay_session -> credited on the ex-date, PAY_DATE_UNKNOWN;
  - spin-off: child shares = shares x child_ratio (+ cash in lieu); basis split by cost_basis_allocation, else by raw closing values on the
    effective session (or the child's first raw close at or after it), else CORPORATE_ACTION_UNACCOUNTED;
  - merger: stock_ratio acquirer shares and / or cash_amount per share;
  - delisting: last_raw_price x (1 + delisting_return); UNKNOWN return -> -100 % (primary) or 0 % (the labelled LAST_PRICE sensitivity).
No total-return factor is ever applied to a ledger position (no double counting). Cash never goes below 0: a violation raises."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from md_eval import (CORPORATE_ACTION_UNACCOUNTED, DELISTING_LAST_PRICE_SENSITIVITY, DELISTING_RETURN_UNKNOWN, FRACTION_UNPAID,
                     PAY_DATE_UNKNOWN)
from md_eval.contract import is_missing

SNAP = 1e-9                                       # a share count within 1e-9 of an integer is that integer (binary ratio products)


class LedgerInvariantError(AssertionError):
    pass


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _whole(x: float):
    """(whole shares, fractional remainder) of a share count, snapping binary noise."""
    r = round(x)
    if abs(x - r) < SNAP:
        return int(r), 0.0
    w = math.floor(x)
    return int(w), x - w


@dataclass
class Position:
    instrument_id: str
    shares: int
    cost_basis: float
    sector: str
    entry_session: str
    stop_raw: Optional[float] = None
    target_raw: Optional[float] = None
    max_hold: Optional[int] = None
    held_sessions: int = 0
    mark: float = 0.0
    mark_session: Optional[str] = None
    received: bool = False                       # child / acquirer shares received from an event: sold at the next open (decision D5)
    role: str = "STRATEGY"


@dataclass
class Ledger:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    receivables: List[dict] = field(default_factory=list)
    log: List[dict] = field(default_factory=list)
    flags: set = field(default_factory=set)
    traded_notional: float = 0.0
    total_costs: float = 0.0
    dividends_credited: float = 0.0

    def _check(self, where: str):
        if not (self.cash >= 0.0):
            raise LedgerInvariantError(f"cash below 0 after {where}: {self.cash!r} (no margin)")

    def _flag(self, code: str, session: str, instrument_id: str, **kw):
        self.flags.add(code)
        self.log.append({"session": session, "kind": "FLAG", "flag": code, "instrument_id": instrument_id, **kw})

    # ---- fills ------------------------------------------------------------------------------------------------------------------------
    def buy(self, instrument_id: str, shares: int, price: float, cost: float, session: str, **meta) -> Position:
        if not (isinstance(shares, int) and shares >= 1):
            raise LedgerInvariantError("whole shares only, at least one")
        if instrument_id in self.positions:
            raise LedgerInvariantError(f"{instrument_id}: already held")
        debit = shares * price + cost
        if debit > self.cash:
            raise LedgerInvariantError(f"{instrument_id}: {debit} exceeds cash {self.cash} (the book checks affordability first)")
        self.cash -= debit
        pos = Position(instrument_id=instrument_id, shares=shares, cost_basis=debit, entry_session=session, mark=price, mark_session=session, **meta)
        self.positions[instrument_id] = pos
        self.traded_notional += shares * price
        self.total_costs += cost
        self.log.append({"session": session, "kind": "BUY", "instrument_id": instrument_id, "shares": shares, "price": price, "cost": cost})
        self._check("a buy")
        return pos

    def sell(self, instrument_id: str, price: float, cost: float, session: str, reason: str = "") -> float:
        pos = self.positions.pop(instrument_id)
        proceeds = pos.shares * price - cost
        self.cash += proceeds
        self.traded_notional += pos.shares * price
        self.total_costs += cost
        self.log.append({"session": session, "kind": "SELL", "instrument_id": instrument_id, "shares": pos.shares, "price": price, "cost": cost,
                         "reason": reason, "realised": proceeds - pos.cost_basis})
        self._check("a sell")
        return proceeds

    # ---- the corporate-action step ------------------------------------------------------------------------------------------------------
    def split(self, ev, session: str):
        pos = self.positions.get(ev.instrument_id)
        if pos is None:
            return
        if not _num(ev.ratio) or ev.ratio <= 0:
            self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="RATIO_UNKNOWN")
            return
        whole, frac = _whole(pos.shares * ev.ratio)
        lieu = 0.0
        if frac > 0:
            if _num(ev.cash_in_lieu_per_share):
                lieu = frac * ev.cash_in_lieu_per_share
            else:
                self._flag(FRACTION_UNPAID, session, ev.instrument_id, event_id=ev.event_id, fraction=frac)
        old = pos.shares
        pos.shares = whole
        self.cash += lieu
        for k in ("stop_raw", "target_raw"):                       # A3: carried price levels divided by the ratio at the booking
            v = getattr(pos, k)
            if v is not None:
                setattr(pos, k, v / ev.ratio)
        pos.mark = pos.mark / ev.ratio
        self.log.append({"session": session, "kind": ev.type, "instrument_id": ev.instrument_id, "event_id": ev.event_id, "shares_before": old,
                         "shares_after": whole, "cash_in_lieu": lieu})
        if whole == 0:
            self.positions.pop(ev.instrument_id)
        self._check("a split")

    def dividend_entitle(self, ev, session: str):
        pos = self.positions.get(ev.instrument_id)
        if pos is None:
            return
        if not _num(ev.cash_amount):
            self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="CASH_AMOUNT_UNKNOWN")
            return
        amount = pos.shares * ev.cash_amount
        if is_missing(ev.pay_session):
            self._flag(PAY_DATE_UNKNOWN, session, ev.instrument_id, event_id=ev.event_id)
            self._credit({"event_id": ev.event_id, "instrument_id": ev.instrument_id, "amount": amount, "pay_session": session,
                          "entitled_shares": pos.shares}, session)
        else:
            self.receivables.append({"event_id": ev.event_id, "instrument_id": ev.instrument_id, "amount": amount, "pay_session": ev.pay_session,
                                     "entitled_shares": pos.shares})
            self.log.append({"session": session, "kind": "DIVIDEND_ENTITLED", "instrument_id": ev.instrument_id, "event_id": ev.event_id,
                             "shares": pos.shares, "amount": amount, "pay_session": ev.pay_session})

    def credit_due(self, session: str):
        due = [r for r in self.receivables if r["pay_session"] <= session]
        self.receivables = [r for r in self.receivables if r["pay_session"] > session]
        for r in sorted(due, key=lambda r: (r["pay_session"], r["event_id"])):
            self._credit(r, session)

    def _credit(self, r: dict, session: str):
        self.cash += r["amount"]
        self.dividends_credited += r["amount"]
        self.log.append({"session": session, "kind": "DIVIDEND_CREDITED", "instrument_id": r["instrument_id"], "event_id": r["event_id"],
                         "amount": r["amount"], "entitled_shares": r["entitled_shares"]})
        self._check("a dividend")

    def spin_off(self, ev, session: str, parent_close, child_close, child_close_rule: str, sector_of, child_mark=None):
        """child_close (possibly the child's first close AFTER the session) only allocates the basis; the valuation mark is the child's raw close
        ON the session (child_mark) or 0 / STALE_MARK, never a later price."""
        pos = self.positions.get(ev.instrument_id)
        if pos is None:
            return
        if not (_num(ev.child_ratio) and ev.child_instrument_id):
            self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="CHILD_UNKNOWN")
            return
        whole, frac = _whole(pos.shares * ev.child_ratio)
        lieu = 0.0
        if frac > 0:
            if _num(ev.cash_in_lieu_per_share):
                lieu = frac * ev.cash_in_lieu_per_share
            else:
                self._flag(FRACTION_UNPAID, session, ev.instrument_id, event_id=ev.event_id, fraction=frac)
        rule, share = None, None
        if _num(ev.cost_basis_allocation):
            rule, share = "COST_BASIS_ALLOCATION", float(ev.cost_basis_allocation)
        elif _num(parent_close) and _num(child_close):
            pv, cv = pos.shares * parent_close, (whole + frac) * child_close
            if pv + cv > 0:
                rule, share = child_close_rule, cv / (pv + cv)
        if share is None:
            self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="SPIN_OFF_BASIS_UNALLOCATED")
            share = 0.0
            rule = "UNACCOUNTED_CHILD_BASIS_ZERO"
        child_basis = pos.cost_basis * share
        pos.cost_basis -= child_basis
        self.cash += lieu
        if whole > 0:
            child = self.positions.get(ev.child_instrument_id)
            if child is None:
                self.positions[ev.child_instrument_id] = Position(instrument_id=ev.child_instrument_id, shares=whole, cost_basis=child_basis,
                                                                  sector=sector_of(ev.child_instrument_id), entry_session=session,
                                                                  mark=child_mark if _num(child_mark) else 0.0,
                                                                  mark_session=session if _num(child_mark) else None, received=True, role=pos.role)
            else:
                child.shares += whole
                child.cost_basis += child_basis
        self.log.append({"session": session, "kind": "SPIN_OFF", "instrument_id": ev.instrument_id, "event_id": ev.event_id,
                         "child_instrument_id": ev.child_instrument_id, "child_shares": whole, "cash_in_lieu": lieu, "basis_rule": rule,
                         "child_basis_share": share})
        self._check("a spin-off")

    def merger(self, ev, session: str, acquirer_price, price_rule, sector_of, acquirer_mark=None):
        """LOW-7: the cost basis is allocated between the cash received and the acquirer shares PRO RATA BY VALUE: the acquirer's raw close on
        the effective session, or its first raw close at or after it (the rule is recorded); the cash part's basis is realised against the cash.
        Acquirer shares with no such close: CORPORATE_ACTION_UNACCOUNTED, and the whole basis stays with the shares. The valuation mark is the
        acquirer's close ON the session (acquirer_mark) or 0 / STALE_MARK, never a later price."""
        pos = self.positions.get(ev.instrument_id)
        if pos is None:
            return
        stock = ev.stock_ratio if _num(ev.stock_ratio) else 0.0
        cash_ps = ev.cash_amount if _num(ev.cash_amount) else 0.0
        if not (_num(ev.stock_ratio) or _num(ev.cash_amount)) or (stock > 0 and not ev.acquirer_instrument_id):
            self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="MERGER_TERMS_UNKNOWN")
            return
        self.positions.pop(ev.instrument_id)
        whole, frac = _whole(pos.shares * stock)
        lieu = 0.0
        if frac > 0:
            if _num(ev.cash_in_lieu_per_share):
                lieu = frac * ev.cash_in_lieu_per_share
            else:
                self._flag(FRACTION_UNPAID, session, ev.instrument_id, event_id=ev.event_id, fraction=frac)
        cash = pos.shares * cash_ps + lieu
        self.cash += cash
        rule, acq_basis = "ALL_CASH", 0.0
        if whole > 0:
            if _num(acquirer_price) and acquirer_price > 0:
                value = whole * acquirer_price
                acq_basis = pos.cost_basis * value / (value + cash)
                rule = price_rule
            else:
                self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="ACQUIRER_HAS_NO_CLOSE_AT_OR_AFTER")
                acq_basis, rule = pos.cost_basis, "UNACCOUNTED_BASIS_TO_ACQUIRER"
            acq = self.positions.get(ev.acquirer_instrument_id)
            if acq is None:
                self.positions[ev.acquirer_instrument_id] = Position(instrument_id=ev.acquirer_instrument_id, shares=whole, cost_basis=acq_basis,
                                                                     sector=sector_of(ev.acquirer_instrument_id), entry_session=session,
                                                                     mark=acquirer_mark if _num(acquirer_mark) else 0.0,
                                                                     mark_session=session if _num(acquirer_mark) else None, received=True, role=pos.role)
            else:
                acq.shares += whole
                acq.cost_basis += acq_basis
        self.log.append({"session": session, "kind": "MERGER", "instrument_id": ev.instrument_id, "event_id": ev.event_id,
                         "acquirer_instrument_id": ev.acquirer_instrument_id, "acquirer_shares": whole, "cash": cash, "basis_rule": rule,
                         "acquirer_basis": acq_basis, "realised_on_cash": cash - (pos.cost_basis - acq_basis)})
        self._check("a merger")

    def delist(self, ev, session: str, mode: str, fallback_price=None):
        pos = self.positions.get(ev.instrument_id)
        if pos is None:
            return
        price = ev.last_raw_price if _num(ev.last_raw_price) else None
        if price is None:
            self._flag(CORPORATE_ACTION_UNACCOUNTED, session, ev.instrument_id, event_id=ev.event_id, why="LAST_RAW_PRICE_UNKNOWN")
            price = 0.0
        if _num(ev.delisting_return):
            ret = float(ev.delisting_return)
        else:
            self._flag(DELISTING_RETURN_UNKNOWN, session, ev.instrument_id, event_id=ev.event_id)
            if mode == "LAST_PRICE":
                self.flags.add(DELISTING_LAST_PRICE_SENSITIVITY)
                ret = 0.0
            else:
                ret = -1.0                                           # primary: -100 %
        self.positions.pop(ev.instrument_id)
        proceeds = max(0.0, pos.shares * price * (1.0 + ret))
        self.cash += proceeds
        self.log.append({"session": session, "kind": "DELISTING", "instrument_id": ev.instrument_id, "event_id": ev.event_id, "shares": pos.shares,
                         "last_raw_price": price, "delisting_return": ret, "proceeds": proceeds, "mode": mode})
        self._check("a delisting")

    # ---- valuation ------------------------------------------------------------------------------------------------------------------------
    def market_value(self) -> float:
        return math.fsum(p.shares * p.mark for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + self.market_value()
