"""md_eval/isolation.py - offline(): while an evaluation runs every socket and SQLite connection is refused (restored afterwards), and each attempt
is recorded so the run is refused even if a caller swallowed the error. The same pattern as the Stage 7.8B-1 offline evaluator, written here so
this package imports no evaluator module (contract P6: no network, no database opened, not even read-only)."""
from __future__ import annotations

import _socket
import contextlib
import socket
import sqlite3
from typing import List

try:
    import _sqlite3
except ImportError:                                  # pragma: no cover - CPython always ships it
    _sqlite3 = None


class IsolationViolation(RuntimeError):              # not an OSError: an `except OSError` never swallows it
    pass


@contextlib.contextmanager
def offline():
    attempts: List[str] = []

    def refuse(*a, **k):
        attempts.append("network or database access")
        raise IsolationViolation("network or database access during an offline evaluation")
    targets = [(socket, "socket"), (socket, "SocketType"), (socket, "create_connection"), (socket, "getaddrinfo"), (_socket, "socket"),
               (sqlite3, "connect"), (sqlite3, "Connection"), (sqlite3.dbapi2, "connect"), (sqlite3.dbapi2, "Connection")] + \
        ([(_sqlite3, "connect"), (_sqlite3, "Connection")] if _sqlite3 is not None else [])
    saved = [(mod, name, getattr(mod, name)) for mod, name in targets]
    for mod, name, _ in saved:
        setattr(mod, name, refuse)
    try:
        yield attempts
    finally:
        for mod, name, orig in saved:
            setattr(mod, name, orig)
