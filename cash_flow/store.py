"""
cash_flow/store.py - SQLite persistence of the cash-flow ledger (DESIGN_602 sections 4, 8). A read writes its new / revised events, its
attempt row and the liveness row in ONE transaction; a failed read writes its attempt and liveness only (every stored event stays). Events
are deduplicated by the broker activity id: an identical record is skipped, a changed one becomes a new revision. Reads open their own
read-only connection and tolerate a database without the Stage 6.0.2 tables.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.cash_flow_migrations import run_cash_flow_migrations
from fit import readonly as RO

_EVENT_COLS = ("event_id", "broker_event_id", "revision", "activity_type", "activity_sub_type", "category", "external", "direction", "amount", "net_amount",
               "currency", "broker_status", "business_date", "effective_at", "trading_date", "source", "note", "raw_reference", "fingerprint", "refresh_id", "created_at")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _event(r: dict) -> Dict[str, Any]:
    d = dict(r)
    d["external"] = bool(d.get("external"))
    d["raw_reference"] = _loads(d.get("raw_reference"), {})
    return d


class CashFlowStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_cash_flow_migrations(conn)
        return conn

    def _read(self, sql: str, args=()) -> List[dict]:
        if not self.path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cash_flow_liveness'").fetchone():
                return []
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads ---------------------------------------------------
    def current_events(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """The latest revision of every broker activity (optionally: effective at / after `since`, plus every date-only record)."""
        sql = ("SELECT e.* FROM cash_flow_events e JOIN (SELECT broker_event_id, MAX(revision) AS rv FROM cash_flow_events GROUP BY broker_event_id) m "
               "ON e.broker_event_id = m.broker_event_id AND e.revision = m.rv")
        args: tuple = ()
        if since:
            sql += " WHERE e.effective_at IS NULL OR e.effective_at >= ?"
            args = (since,)
        sql += " ORDER BY COALESCE(e.effective_at, e.business_date) DESC, e.broker_event_id DESC"
        return [_event(r) for r in self._read(sql, args)]

    def all_rows(self, limit: int = 500) -> List[Dict[str, Any]]:
        return [_event(r) for r in self._read("SELECT * FROM cash_flow_events ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    def event_row_count(self) -> int:
        rows = self._read("SELECT count(*) AS n FROM cash_flow_events")
        return int(rows[0]["n"]) if rows else 0

    def liveness(self) -> Dict[str, Any]:
        rows = self._read("SELECT * FROM cash_flow_liveness WHERE id = 1")
        if not rows:
            return {"last_attempt_at": None, "last_success_at": None, "attempt_count": 0, "success_count": 0, "failure_count": 0, "consecutive_failures": 0,
                    "event_count": 0, "last_result": None, "last_error": None, "last_event_at": None, "last_complete": None, "last_pages": None, "owner": None,
                    "updated_at": None}
        r = dict(rows[0])
        r.pop("id", None)
        return r

    def attempts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._read("SELECT * FROM cash_flow_refresh_attempts ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))

    def attempt_count(self) -> int:
        rows = self._read("SELECT count(*) AS n FROM cash_flow_refresh_attempts")
        return int(rows[0]["n"]) if rows else 0

    # ---------------------------------------------------------------- the two writes ------------------------------------------
    @staticmethod
    def _attempt(conn, a: Dict[str, Any]) -> None:
        conn.execute("INSERT INTO cash_flow_refresh_attempts (attempt_id, refresh_id, started_at, finished_at, trigger, result, read_attempts, pages, rows_read, new_events, "
                     "revised_events, skipped_trade_rows, complete, lookback_after, error_code, error_text, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (uuid.uuid4().hex, a.get("refresh_id"), a["started_at"], a["finished_at"], a["trigger"], a["result"], int(a.get("read_attempts") or 1),
                      int(a.get("pages") or 0), int(a.get("rows") or 0), int(a.get("new_events") or 0), int(a.get("revised_events") or 0),
                      int(a.get("skipped_trade_rows") or 0), 1 if a.get("complete") else 0, a.get("lookback_after"), a.get("error_code"),
                      (a.get("error_text") or None) and str(a["error_text"])[:500], a.get("owner")))

    def commit_success(self, events: List[Dict[str, Any]], attempt: Dict[str, Any], now: datetime, *, source: str) -> Dict[str, int]:
        """Dedupe + append + attempt + liveness, atomically. Returns {"new": n, "revised": n, "unchanged": n}."""
        stamp = _iso(now)
        conn = self._rw()
        out = {"new": 0, "revised": 0, "unchanged": 0}
        try:
            conn.execute("BEGIN IMMEDIATE")
            for ev in events:
                prev = conn.execute("SELECT revision, fingerprint FROM cash_flow_events WHERE broker_event_id = ? ORDER BY revision DESC LIMIT 1",
                                    (ev["broker_event_id"],)).fetchone()
                if prev is not None and prev["fingerprint"] == ev["fingerprint"]:
                    out["unchanged"] += 1
                    continue
                rev = 1 if prev is None else int(prev["revision"]) + 1
                out["new" if prev is None else "revised"] += 1
                row = {**ev, "event_id": uuid.uuid4().hex, "revision": rev, "external": 1 if ev.get("external") else 0, "source": source,
                       "raw_reference": _json(ev.get("raw_reference") or {}), "refresh_id": attempt.get("refresh_id"), "created_at": stamp}
                conn.execute(f"INSERT INTO cash_flow_events ({', '.join(_EVENT_COLS)}) VALUES ({', '.join('?' for _ in _EVENT_COLS)})", tuple(row.get(c) for c in _EVENT_COLS))
            attempt = {**attempt, "new_events": out["new"], "revised_events": out["revised"]}
            self._attempt(conn, attempt)
            n = conn.execute("SELECT count(DISTINCT broker_event_id) FROM cash_flow_events").fetchone()[0]
            last_ev = conn.execute("SELECT MAX(effective_at) FROM cash_flow_events").fetchone()[0]
            ok = attempt["result"] == "SUCCESS"
            conn.execute("INSERT INTO cash_flow_liveness (id, last_attempt_at, last_success_at, attempt_count, success_count, failure_count, consecutive_failures, event_count, "
                         "last_result, last_error, last_event_at, last_complete, last_pages, owner, updated_at) VALUES (1,?,?,1,1,0,0,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "last_attempt_at = excluded.last_attempt_at, last_success_at = excluded.last_success_at, attempt_count = attempt_count + 1, "
                         "success_count = success_count + 1, consecutive_failures = 0, event_count = excluded.event_count, last_result = excluded.last_result, "
                         "last_error = excluded.last_error, last_event_at = excluded.last_event_at, last_complete = excluded.last_complete, last_pages = excluded.last_pages, "
                         "owner = excluded.owner, updated_at = excluded.updated_at",
                         (attempt["started_at"], attempt["started_at"], int(n), attempt["result"], None if ok else "page limit reached: activity history incomplete",
                          last_ev, 1 if attempt.get("complete") else 0, int(attempt.get("pages") or 0), attempt.get("owner"), stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return out

    def commit_failure(self, attempt: Dict[str, Any], now: datetime) -> None:
        stamp = _iso(now)
        err = f"{attempt.get('error_code')}: {attempt.get('error_text') or ''}".strip(": ")[:500]
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._attempt(conn, attempt)
            n = conn.execute("SELECT count(DISTINCT broker_event_id) FROM cash_flow_events").fetchone()[0]
            conn.execute("INSERT INTO cash_flow_liveness (id, last_attempt_at, last_success_at, attempt_count, success_count, failure_count, consecutive_failures, event_count, "
                         "last_result, last_error, owner, updated_at) VALUES (1,?,NULL,1,0,1,1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "last_attempt_at = excluded.last_attempt_at, attempt_count = attempt_count + 1, failure_count = failure_count + 1, "
                         "consecutive_failures = consecutive_failures + 1, event_count = excluded.event_count, last_result = excluded.last_result, "
                         "last_error = excluded.last_error, owner = excluded.owner, updated_at = excluded.updated_at",
                         (attempt["started_at"], int(n), attempt["result"], err, attempt.get("owner"), stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
