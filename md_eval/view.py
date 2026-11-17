"""md_eval/view.py - DecisionContext: the ONLY object a signal source receives. It carries the as_of(close_utc(d)) view, the session d and an
immutable snapshot of the book. It exposes no provider, no manifest beyond the view's A11 allow-list, no full-history query and no
execution_open (contract §4, A2, A11; test (d))."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class PositionSnapshot:
    instrument_id: str
    shares: int
    sector: str
    held_sessions: int
    stop_raw: object
    target_raw: object
    max_hold: object


@dataclass(frozen=True)
class BookSnapshot:
    session: str
    cash: float
    equity: float
    stale_equity: bool
    positions: Tuple[PositionSnapshot, ...]


class DecisionContext:
    __slots__ = ("view", "session", "book")

    def __init__(self, view, session: str, book: BookSnapshot):
        object.__setattr__(self, "view", view)
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "book", book)

    def __setattr__(self, k, v):
        raise AttributeError("a DecisionContext is read-only")
