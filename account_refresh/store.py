"""
account_refresh/store.py - SQLite persistence of the read-only account snapshot (DESIGN_601 section 12). A success replaces the snapshot row,
appends the attempt and updates liveness in ONE transaction; a failure appends the attempt and updates liveness only (the last good
snapshot stays as it was). Reads open their own read-only connection and tolerate a database without the Stage 6.0.1 tables.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.account_refresh_migrations import run_account_refresh_migrations
from fit import readonly as RO


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


class AccountRefreshStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_account_refresh_migrations(conn)
        return conn

    def _read(self, sql: str, args=()) -> List[dict]:
        if not self.path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_refresh_liveness'").fetchone():
                return []
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads ---------------------------------------------------
    def snapshot(self) -> Optional[Dict[str, Any]]:
        rows = self._read("SELECT * FROM account_refresh_snapshot WHERE id = 1")
        if not rows:
            return None
        r = rows[0]
        return {"refresh_id": r["refresh_id"], "refreshed_at": r["refreshed_at"], "source": r["source"], "host": r["host"], "account": _loads(r["account_json"], {}),
                "positions": _loads(r["positions_json"], []), "open_orders": _loads(r["open_orders_json"], []), "updated_at": r["updated_at"]}

    def liveness(self) -> Dict[str, Any]:
        rows = self._read("SELECT * FROM account_refresh_liveness WHERE id = 1")
        if not rows:
            return {"last_attempt_at": None, "last_success_at": None, "attempt_count": 0, "success_count": 0, "failure_count": 0, "consecutive_failures": 0,
                    "last_result": None, "last_error": None, "last_snapshot_age_s": None, "owner": None, "updated_at": None}
        r = dict(rows[0])
        r.pop("id", None)
        return r

    def attempts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._read("SELECT * FROM account_refresh_attempts ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))

    def attempt_count(self) -> int:
        rows = self._read("SELECT count(*) AS n FROM account_refresh_attempts")
        return int(rows[0]["n"]) if rows else 0

    # ---------------------------------------------------------------- the two writes ------------------------------------------
    def _attempt(self, conn, attempt: Dict[str, Any]) -> None:
        conn.execute("INSERT INTO account_refresh_attempts (attempt_id, started_at, finished_at, trigger, result, read_attempts, error_code, error_text, refresh_id, owner) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, attempt["started_at"], attempt["finished_at"], attempt["trigger"], attempt["result"],
                                                      int(attempt.get("read_attempts") or 1), attempt.get("error_code"), (attempt.get("error_text") or None) and str(attempt["error_text"])[:500],
                                                      attempt.get("refresh_id"), attempt.get("owner")))

    def commit_success(self, snap: Dict[str, Any], attempt: Dict[str, Any], now: datetime) -> None:
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO account_refresh_snapshot (id, refresh_id, refreshed_at, source, host, account_json, positions_json, open_orders_json, updated_at) "
                         "VALUES (1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET refresh_id = excluded.refresh_id, refreshed_at = excluded.refreshed_at, "
                         "source = excluded.source, host = excluded.host, account_json = excluded.account_json, positions_json = excluded.positions_json, "
                         "open_orders_json = excluded.open_orders_json, updated_at = excluded.updated_at",
                         (snap["refresh_id"], snap["refreshed_at"], snap["source"], snap["host"], _json(snap["account"]), _json(snap["positions"]), _json(snap["open_orders"]), stamp))
            self._attempt(conn, attempt)
            conn.execute("INSERT INTO account_refresh_liveness (id, last_attempt_at, last_success_at, attempt_count, success_count, failure_count, consecutive_failures, "
                         "last_result, last_error, last_snapshot_age_s, owner, updated_at) VALUES (1,?,?,1,1,0,0,'SUCCESS',NULL,0,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "last_attempt_at = excluded.last_attempt_at, last_success_at = excluded.last_success_at, attempt_count = attempt_count + 1, "
                         "success_count = success_count + 1, consecutive_failures = 0, last_result = 'SUCCESS', last_error = NULL, last_snapshot_age_s = 0, "
                         "owner = excluded.owner, updated_at = excluded.updated_at", (attempt["started_at"], snap["refreshed_at"], attempt.get("owner"), stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def commit_failure(self, attempt: Dict[str, Any], now: datetime, snapshot_age_s: Optional[float]) -> None:
        stamp = _iso(now)
        err = f"{attempt.get('error_code')}: {attempt.get('error_text') or ''}".strip(": ")[:500]
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._attempt(conn, attempt)
            conn.execute("INSERT INTO account_refresh_liveness (id, last_attempt_at, last_success_at, attempt_count, success_count, failure_count, consecutive_failures, "
                         "last_result, last_error, last_snapshot_age_s, owner, updated_at) VALUES (1,?,NULL,1,0,1,1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "last_attempt_at = excluded.last_attempt_at, attempt_count = attempt_count + 1, failure_count = failure_count + 1, "
                         "consecutive_failures = consecutive_failures + 1, last_result = excluded.last_result, last_error = excluded.last_error, "
                         "last_snapshot_age_s = excluded.last_snapshot_age_s, owner = excluded.owner, updated_at = excluded.updated_at",
                         (attempt["started_at"], attempt["result"], err, snapshot_age_s, attempt.get("owner"), stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
