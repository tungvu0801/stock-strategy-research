"""
decision_flow/store.py - READ-ONLY access for the Stage 6.10C decision flow (DESIGN_610C_DECISION_FLOW.md section 6).

  connections  short-lived sqlite connections opened mode=ro with a 5 s busy timeout, one per request: no migration, no DDL, nothing is created
               or written by a read; a missing database, table or column reads as empty
  bisection    the append-only tables have no usable time index (setup_evaluations is indexed by (symbol, evaluated_at) only, execution_runs by
               its key only), but rows are appended in roughly time order. A binary search on rowid finds the boundary of a time, and a short
               ADAPTIVE margin scan picks up the few rows that interleave around it (a setup batch is written one symbol at a time while the
               Trader View stream and the other writers append). No request reads a growing table from its start.

execution_mode_events.environment_json (the paper host and a masked account number) is never selected: callers name their columns.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

BUSY_TIMEOUT_S = 5.0
MARGIN = 64                      # rows read per margin step around a bisection boundary
MAX_MARGIN_STEPS = 64            # bound on one adaptive margin (64 steps x 64 rows)
# the tables searched by time and their (TEXT, UTC ISO) time column; string order is time order
TIME_COLUMNS = {"setup_evaluations": "evaluated_at", "execution_runs": "started_at", "execution_safety_evaluations": "evaluated_at",
                "autorun_preflights": "at", "forward_validation_samples": "at", "execution_mode_events": "at"}


def default_path() -> Path:
    """The app's database file (the same file every other stage uses): the open singleton's path, else the configured path. Never get_db(),
    which would create the directory, open the file read-write and migrate it in a process that has none."""
    import config
    from database import database as dbm
    return Path(dbm._db_instance.db_path) if dbm._db_instance is not None else Path(config.RESEARCH_DB_PATH)


class Reader:
    """One read-only connection (one request). Every method reads; a missing table or column is an empty result, never an error."""

    def __init__(self, conn: Optional[sqlite3.Connection]):
        self.conn = conn

    # ---------------------------------------------------------------- plain reads ---------------------------------------------------
    def query(self, sql: str, args=()) -> List[Dict[str, Any]]:
        if self.conn is None:
            return []
        try:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                return []
            raise

    def one(self, sql: str, args=()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def _scalar(self, sql: str, args=()) -> Any:
        row = self.one(sql, args)
        return None if row is None else next(iter(row.values()))

    # ---------------------------------------------------------------- rowid bisection ----------------------------------------------
    @staticmethod
    def _col(table: str) -> str:
        if table not in TIME_COLUMNS:
            raise ValueError(f"not a time-searched table: {table}")
        return TIME_COLUMNS[table]

    def first_rowid(self, table: str) -> Optional[int]:
        self._col(table)
        return self._scalar(f"SELECT min(rowid) AS m FROM {table}")          # min / max alone use the b-tree ends (no scan)

    def last_rowid(self, table: str) -> Optional[int]:
        self._col(table)
        return self._scalar(f"SELECT max(rowid) AS m FROM {table}")

    def chunk(self, table: str, *, after: Optional[int] = None, before: Optional[int] = None, cols: str = "", limit: int = MARGIN) -> List[Dict[str, Any]]:
        """Up to `limit` rows next to a rowid: ascending after it, or descending before it. Always rowid AS rid and the time AS t."""
        col = self._col(table)
        extra = f", {cols}" if cols else ""
        if after is not None:
            return self.query(f"SELECT rowid AS rid, {col} AS t{extra} FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?", (after, limit))
        return self.query(f"SELECT rowid AS rid, {col} AS t{extra} FROM {table} WHERE rowid < ? ORDER BY rowid DESC LIMIT ?", (before, limit))

    def bisect(self, table: str, t: str, *, strict: bool = False) -> Optional[int]:
        """The (approximate) largest rowid whose time is <= t (< t when strict), by binary search on rowid; None when the first row is past t."""
        col = self._col(table)
        lo, hi = self.first_rowid(table), self.last_rowid(table)
        if lo is None or hi is None:
            return None
        best: Optional[int] = None
        while lo <= hi:
            mid = (lo + hi) // 2
            row = self.one(f"SELECT rowid AS rid, {col} AS t FROM {table} WHERE rowid >= ? ORDER BY rowid LIMIT 1", (mid,))
            if row is None or row["rid"] > hi:                               # a rowid gap: nothing between mid and hi
                hi = mid - 1
                continue
            v = row["t"]
            if v is not None and (v < t if strict else v <= t):
                best, lo = row["rid"], row["rid"] + 1
            else:
                hi = mid - 1
        return best

    def window(self, table: str, lo: str, hi: str, cols: str, *, hi_inclusive: bool = False, where: str = "", args=()) -> List[Dict[str, Any]]:
        """Rows with lo <= time < hi (<= hi when hi_inclusive), rowid order, as {rid, <cols>}. Bisection finds both boundaries; each margin
        grows while its next chunk still holds a row of the window. `where` / `args` filter the final read only."""
        col = self._col(table)
        first = self.first_rowid(table)
        if first is None:
            return []
        inside = (lambda v: v is not None and lo <= v <= hi) if hi_inclusive else (lambda v: v is not None and lo <= v < hi)  # noqa: E731
        a = self.bisect(table, lo, strict=True)                                  # the last row before the window
        b = self.bisect(table, hi, strict=not hi_inclusive)                      # the last row inside the window (or before it)
        start = a + 1 if a is not None else first
        end = b if b is not None else first - 1
        for _ in range(MAX_MARGIN_STEPS):
            rows = self.chunk(table, before=start)
            if not rows or not any(inside(r["t"]) for r in rows):
                break
            start = rows[-1]["rid"]
        for _ in range(MAX_MARGIN_STEPS):
            rows = self.chunk(table, after=end)
            if not rows or not any(inside(r["t"]) for r in rows):
                break
            end = rows[-1]["rid"]
        start, end = min(start, end), max(start, end)
        op = "<=" if hi_inclusive else "<"
        sql = (f"SELECT rowid AS rid, {cols} FROM {table} WHERE rowid BETWEEN ? AND ? AND {col} >= ? AND {col} {op} ?" + (f" AND ({where})" if where else "")
               + " ORDER BY rowid")
        return self.query(sql, (start, end, lo, hi, *args))

    def last_before(self, table: str, t: str, cols: str, *, strict: bool = False) -> Optional[Dict[str, Any]]:
        """The row with the latest time <= t (< t when strict); ties go to the higher rowid. None when there is none."""
        col = self._col(table)
        first = self.first_rowid(table)
        if first is None:
            return None
        ok = (lambda v: v is not None and v < t) if strict else (lambda v: v is not None and v <= t)  # noqa: E731
        b = self.bisect(table, t, strict=strict)
        end = b if b is not None else first - 1
        for _ in range(MAX_MARGIN_STEPS):                                        # rows written late that still belong before t
            rows = self.chunk(table, after=end)
            if not rows or not any(ok(r["t"]) for r in rows):
                break
            end = rows[-1]["rid"]
        back = self.chunk(table, before=(b if b is not None else first) + 1, limit=MARGIN)   # the MARGIN rows up to b by rowid, gaps or not
        start = back[-1]["rid"] if back else first
        op = "<" if strict else "<="
        return self.one(f"SELECT rowid AS rid, {cols} FROM {table} WHERE rowid BETWEEN ? AND ? AND {col} {op} ? ORDER BY {col} DESC, rowid DESC LIMIT 1",
                        (start, end, t))

    def marks(self) -> tuple:
        """The newest rowid of every table a payload reads: a cache key that changes whenever any of them gains a row."""
        out = [self._scalar(f"SELECT max(rowid) AS m FROM {t}") for t in TIME_COLUMNS]
        out.append(self._scalar("SELECT max(rowid) AS m FROM forward_capture_decisions"))
        out.append(self._scalar("SELECT last_run_at FROM setup_controls WHERE name = 'setup_state_evaluator'"))
        return tuple(out)


class DecisionFlowStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()

    @contextmanager
    def read(self) -> Iterator[Reader]:
        """One short-lived read-only connection; a database file that does not exist reads as empty (it is never created)."""
        if not self.path.exists():
            yield Reader(None)
            return
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        try:
            yield Reader(conn)
        finally:
            conn.close()
