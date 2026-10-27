"""
autorun/store.py - Stage 6.10A auto-run records on the shared database: append-only events and preflights, one upserted row per trading day,
and a read of the single-instance lease row. Reads open read-only connections; a missing table is an empty result.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.autorun_migrations import run_autorun_migrations


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def default_path() -> Path:
    from database.database import get_db
    return Path(get_db().db_path)


class AutoRunStore:
    DAY_COLS = ("first_armed_at", "first_armed_by", "last_disarmed_at", "last_disarmed_by", "last_disarm_code", "scheduled_arm_at", "scheduled_arm_result")

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()

    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_autorun_migrations(conn)
        return conn

    def query(self, sql: str, args=()) -> List[dict]:
        if not self.path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                return []
            raise
        finally:
            conn.close()

    def _write(self, fn) -> Any:
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            out = fn(conn)
            conn.execute("COMMIT")
            return out
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def ensure(self) -> None:
        self._rw().close()

    # ---------------------------------------------------------------- events / preflights --------------------------------------------
    def add_event(self, now: datetime, kind: str, *, code: Optional[str] = None, actor: Optional[str] = None, detail: Optional[dict] = None,
                  trading_date: Optional[str] = None, owner: Optional[str] = None) -> str:
        eid = uuid.uuid4().hex
        self._write(lambda c: c.execute("INSERT INTO autorun_events (event_id, at, trading_date, kind, code, actor, detail_json, owner) VALUES (?,?,?,?,?,?,?,?)",
                                        (eid, _iso(now), trading_date, kind, code, actor, _json(detail or {}), owner)))
        return eid

    def events(self, limit: int = 50, trading_date: Optional[str] = None, kind: Optional[str] = None) -> List[dict]:
        where, args = [], []
        if trading_date:
            where.append("trading_date = ?")
            args.append(trading_date)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        sql = "SELECT * FROM autorun_events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY at DESC, rowid DESC LIMIT ?"
        rows = self.query(sql, (*args, limit))
        for r in rows:
            r["detail"] = _loads(r.pop("detail_json"), {})
        return rows

    def add_preflight(self, now: datetime, trigger: str, result: dict, policy: str, owner: Optional[str] = None) -> str:
        pid = uuid.uuid4().hex
        self._write(lambda c: c.execute("INSERT INTO autorun_preflights (preflight_id, at, trigger, status, checks_json, policy, owner) VALUES (?,?,?,?,?,?,?)",
                                        (pid, _iso(now), trigger, result["status"], _json(result.get("checks") or []), policy, owner)))
        return pid

    def last_preflight(self) -> Optional[dict]:
        rows = self.query("SELECT * FROM autorun_preflights ORDER BY at DESC, rowid DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        r["checks"] = _loads(r.pop("checks_json"), [])
        return r

    def preflights(self, limit: int = 20) -> List[dict]:
        rows = self.query("SELECT * FROM autorun_preflights ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["checks"] = _loads(r.pop("checks_json"), [])
        return rows

    # ---------------------------------------------------------------- the trading-day record ----------------------------------------------
    def day(self, trading_date: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM autorun_days WHERE trading_date = ?", (trading_date,))
        return rows[0] if rows else None

    def update_day(self, trading_date: str, now: datetime, **fields) -> None:
        bad = set(fields) - set(self.DAY_COLS)
        if bad:
            raise ValueError(f"not an autorun day column: {sorted(bad)}")

        def fn(conn):
            conn.execute("INSERT INTO autorun_days (trading_date, updated_at) VALUES (?, ?) ON CONFLICT(trading_date) DO NOTHING", (trading_date, _iso(now)))
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE autorun_days SET {sets}, updated_at = ? WHERE trading_date = ?", (*fields.values(), _iso(now), trading_date))
        self._write(fn)

    # ---------------------------------------------------------------- the single-instance lease (read only; the lease store writes it) -------
    def lease(self, name: str) -> Optional[dict]:
        rows = self.query("SELECT name, owner, acquired_at, expires_at FROM app_leases WHERE name = ?", (name,))
        return rows[0] if rows else None
