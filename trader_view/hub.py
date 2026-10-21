"""
Subscription hub and navigation history (the Python side of the rules the browser also applies in frontend/trader_core.js).

Hub: exactly ONE detailed subscription per connection — subscribe replaces the previous symbol, so a ticker switch can never leave
two streams open and a reconnect (a new connection) can never stack a second one. accepts_message mirrors the browser's stale-ticker
guard: symbol-bound messages are only accepted for the active ticker.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from trader_view import SYMBOL_BOUND_TYPES


class Hub:
    def __init__(self):
        self._by_conn: Dict[int, str] = {}

    def subscribe(self, conn_id: int, symbol: str) -> Optional[str]:
        """Returns the symbol this subscription replaced (None when new or unchanged)."""
        symbol = symbol.upper()
        previous = self._by_conn.get(conn_id)
        self._by_conn[conn_id] = symbol
        return previous if previous is not None and previous != symbol else None

    def unsubscribe(self, conn_id: int, symbol: Optional[str] = None) -> bool:
        current = self._by_conn.get(conn_id)
        if current is None or (symbol is not None and current != symbol.upper()):
            return False
        del self._by_conn[conn_id]
        return True

    def drop(self, conn_id: int) -> None:
        self._by_conn.pop(conn_id, None)

    def symbol_of(self, conn_id: int) -> Optional[str]:
        return self._by_conn.get(conn_id)

    def subscribers(self, symbol: str) -> List[int]:
        symbol = symbol.upper()
        return sorted(c for c, s in self._by_conn.items() if s == symbol)

    def items(self) -> List[Tuple[int, str]]:
        return sorted(self._by_conn.items())

    def values(self) -> List[str]:
        return sorted(set(self._by_conn.values()))

    def __len__(self) -> int:
        return len(self._by_conn)


def accepts_message(msg, active: Optional[str]) -> bool:
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        return False
    if msg["type"] not in SYMBOL_BOUND_TYPES:
        return True
    return isinstance(msg.get("symbol"), str) and msg["symbol"] == active


class NavigationHistory:
    """Back / Forward over visited tickers: pushing from the middle truncates the forward stack; pushing the current one is a no-op."""

    def __init__(self, items: Iterable[str] = ()):
        self.items: List[str] = list(items)
        self.index = len(self.items) - 1

    @property
    def current(self) -> Optional[str]:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def push(self, symbol: str) -> None:
        if symbol == self.current:
            return
        del self.items[self.index + 1:]
        self.items.append(symbol)
        self.index = len(self.items) - 1

    def can_back(self) -> bool:
        return self.index > 0

    def can_forward(self) -> bool:
        return self.index < len(self.items) - 1

    def back(self) -> Optional[str]:
        if not self.can_back():
            return None
        self.index -= 1
        return self.current

    def forward(self) -> Optional[str]:
        if not self.can_forward():
            return None
        self.index += 1
        return self.current
