"""
forward_validation/store.py - SQLite persistence of the Stage 6.1 observer (DESIGN_61 section 11) plus READ-ONLY access to the persisted
system events of the earlier stages on the same database (setup transitions, proposals, intents, orders, positions, reconciliations, safety
evaluations, refresh attempts). Raw observer rows are append-only; a session row progresses until it is closed; derived daily summaries are
rebuilt (upserted). Reads open their own read-only connection and tolerate a database without the Stage 6.1 tables.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.forward_validation_migrations import run_forward_validation_migrations
from fit import readonly as RO

SESSION_MUTABLE = ("started_at", "ended_at", "bot_start_time", "bot_stop_time", "market_open_seen", "market_close_seen", "paper_auto_enabled", "account_refresh_healthy",
                   "cash_flow_healthy", "reconciliation_healthy", "session_status", "notes_code", "in_session_samples", "owners_json")
SESSION_BOOLS = ("market_open_seen", "market_close_seen", "paper_auto_enabled", "account_refresh_healthy", "cash_flow_healthy", "reconciliation_healthy")
SAMPLE_COLS = ("sample_id", "at", "trading_date", "in_session", "owner", "mode", "executor_ready", "safety_state", "allowed_new_entry", "safety_codes_json", "reconciliation_healthy",
               "attention_count", "attention_json", "account_status", "account_age_s", "account_failures", "equity", "cash", "buying_power", "net_external_cum", "cash_flow_status",
               "cash_flow_age_s", "cash_flow_failures", "open_positions", "pending_entries", "pending_exits", "positions_json", "controls_json", "last_cycle_at", "last_cycle_result",
               "loop_running")
SAMPLE_JSON = {"safety_codes_json": "safety_codes", "attention_json": "attention", "positions_json": "positions", "controls_json": "controls"}


class ForwardValidationStoreError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def new_id() -> str:
    return uuid.uuid4().hex


class ForwardValidationStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---------------------------------------------------------------- connections -------------------------------------------
    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_forward_validation_migrations(conn)
        return conn

    def _ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def query(self, sql: str, args=()) -> List[dict]:
        """Read-only SQL over ANY table of the shared database; a missing table (an earlier stage never wrote) is an empty result."""
        conn = self._ro()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                return []
            raise
        finally:
            conn.close()

    def _read(self, sql: str, args=()) -> List[dict]:
        return self.query(sql, args)

    def exists(self) -> bool:
        return bool(self.query("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forward_validation_sessions'"))

    def ensure(self) -> None:
        self._rw().close()

    def _write(self, fn) -> None:
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            fn(conn)
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ForwardValidationStoreError(str(exc)) from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- sessions ----------------------------------------------------
    @staticmethod
    def _session(r: dict) -> dict:
        out = dict(r)
        for k in SESSION_BOOLS:
            out[k] = None if out.get(k) is None else bool(out[k])
        out["owners"] = _loads(out.pop("owners_json"), [])
        out["notes"] = [c for c in (out.get("notes_code") or "").split("|") if c]
        return out

    def session(self, trading_date: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM forward_validation_sessions WHERE trading_date = ?", (trading_date,))
        return self._session(rows[0]) if rows else None

    def sessions(self, limit: int = 400, newest_first: bool = True) -> List[dict]:
        order = "DESC" if newest_first else "ASC"
        return [self._session(r) for r in self._read(f"SELECT * FROM forward_validation_sessions ORDER BY trading_date {order} LIMIT ?", (limit,))]

    def open_sessions(self) -> List[dict]:
        return [self._session(r) for r in self._read("SELECT * FROM forward_validation_sessions WHERE session_status = 'OPEN' ORDER BY trading_date")]

    def last_session_date(self) -> Optional[str]:
        rows = self._read("SELECT MAX(trading_date) AS d FROM forward_validation_sessions")
        return rows[0]["d"] if rows and rows[0]["d"] else None

    def first_session_created_at(self) -> Optional[str]:
        rows = self._read("SELECT MIN(created_at) AS c FROM forward_validation_sessions")
        return rows[0]["c"] if rows and rows[0]["c"] else None

    def open_session(self, trading_date: str, now: datetime, owner: str, *, in_session: bool) -> dict:
        stamp = _iso(now)

        def fn(conn):
            conn.execute("INSERT INTO forward_validation_sessions (trading_date, started_at, ended_at, bot_start_time, bot_stop_time, market_open_seen, market_close_seen, paper_auto_enabled, "
                         "account_refresh_healthy, cash_flow_healthy, reconciliation_healthy, session_status, notes_code, in_session_samples, owners_json, created_at, updated_at) "
                         "VALUES (?,?,NULL,?,?,?,0,0,NULL,NULL,NULL,'OPEN','',0,?,?,?)",
                         (trading_date, stamp, stamp, stamp, 1 if in_session else 0, _json([owner]), stamp, stamp))         # the opening tick is counted by the same tick's update
        self._write(fn)
        return self.session(trading_date)

    def insert_not_run(self, trading_date: str, now: datetime, notes_code: str) -> dict:
        stamp = _iso(now)

        def fn(conn):
            conn.execute("INSERT INTO forward_validation_sessions (trading_date, started_at, ended_at, bot_start_time, bot_stop_time, market_open_seen, market_close_seen, paper_auto_enabled, "
                         "account_refresh_healthy, cash_flow_healthy, reconciliation_healthy, session_status, notes_code, in_session_samples, owners_json, created_at, updated_at) "
                         "VALUES (?,NULL,NULL,NULL,NULL,0,0,0,NULL,NULL,NULL,'NOT_RUN',?,0,'[]',?,?)", (trading_date, notes_code, stamp, stamp))
        self._write(fn)
        return self.session(trading_date)

    def update_session(self, trading_date: str, now: datetime, **fields) -> dict:
        bad = set(fields) - set(SESSION_MUTABLE)
        if bad:
            raise ForwardValidationStoreError(f"immutable session columns: {sorted(bad)}")
        vals = dict(fields)
        for k in SESSION_BOOLS:
            if k in vals and vals[k] is not None:
                vals[k] = 1 if vals[k] else 0
        vals["updated_at"] = _iso(now)

        def fn(conn):
            conn.execute(f"UPDATE forward_validation_sessions SET {', '.join(f'{k} = ?' for k in vals)} WHERE trading_date = ?", (*vals.values(), trading_date))
        self._write(fn)
        return self.session(trading_date)

    # ---------------------------------------------------------------- samples / marks / restarts / events -----------------------
    @staticmethod
    def _sample(r: dict) -> dict:
        out = dict(r)
        for col, key in SAMPLE_JSON.items():
            out[key] = _loads(out.pop(col), [] if key != "controls" else {})
        for k in ("in_session", "executor_ready"):
            out[k] = bool(out.get(k))
        for k in ("allowed_new_entry", "reconciliation_healthy", "loop_running"):
            out[k] = None if out.get(k) is None else bool(out[k])
        return out

    def add_sample(self, row: Dict[str, Any]) -> str:
        sid = row.get("sample_id") or new_id()
        d = {k: row.get(k) for k in SAMPLE_COLS}
        d["sample_id"] = sid
        for col, key in SAMPLE_JSON.items():
            d[col] = _json(row.get(key, [] if key != "controls" else {}))
        for k in ("in_session", "executor_ready"):
            d[k] = 1 if row.get(k) else 0
        for k in ("allowed_new_entry", "reconciliation_healthy", "loop_running"):
            d[k] = None if row.get(k) is None else (1 if row.get(k) else 0)
        for k in ("attention_count", "open_positions", "pending_entries", "pending_exits"):
            d[k] = int(row.get(k) or 0)

        def fn(conn):
            conn.execute(f"INSERT INTO forward_validation_samples ({', '.join(SAMPLE_COLS)}) VALUES ({', '.join('?' * len(SAMPLE_COLS))})", [d[c] for c in SAMPLE_COLS])
        self._write(fn)
        return sid

    def samples(self, trading_date: Optional[str] = None, limit: int = 5000, in_session_only: bool = False) -> List[dict]:
        sql, args = "SELECT * FROM forward_validation_samples WHERE 1 = 1", []
        if trading_date:
            sql += " AND trading_date = ?"
            args.append(trading_date)
        if in_session_only:
            sql += " AND in_session = 1"
        sql += " ORDER BY at, rowid LIMIT ?"
        args.append(limit)
        return [self._sample(r) for r in self._read(sql, tuple(args))]

    def last_sample(self) -> Optional[dict]:
        rows = self._read("SELECT * FROM forward_validation_samples ORDER BY at DESC, rowid DESC LIMIT 1")
        return self._sample(rows[0]) if rows else None

    def sample_count(self) -> int:
        rows = self._read("SELECT count(*) AS n FROM forward_validation_samples")
        return int(rows[0]["n"]) if rows else 0

    def add_marks(self, rows: Iterable[Dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0

        def fn(conn):
            for m in rows:
                conn.execute("INSERT INTO forward_validation_marks (mark_id, at, position_id, symbol, state, quantity, last_price, last_price_at) VALUES (?,?,?,?,?,?,?,?)",
                             (new_id(), m["at"], m["position_id"], m["symbol"], m.get("state") or "", int(m.get("quantity") or 0), m.get("last_price"), m.get("last_price_at")))
        self._write(fn)
        return len(rows)

    def marks(self, position_id: str, limit: int = 20000) -> List[dict]:
        return self._read("SELECT * FROM forward_validation_marks WHERE position_id = ? ORDER BY at, rowid LIMIT ?", (position_id, limit))

    def add_restart(self, row: Dict[str, Any]) -> str:
        rid = new_id()

        def fn(conn):
            conn.execute("INSERT INTO forward_validation_restarts (restart_id, at, trading_date, previous_owner, previous_sample_at, owner, state_before_json, state_after_json, "
                         "broker_queried, reconciliation_result, duplicate_orders, position_continuity, detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (rid, row["at"], row["trading_date"], row.get("previous_owner"), row.get("previous_sample_at"), row["owner"], _json(row.get("state_before") or {}),
                          _json(row.get("state_after") or {}), 1 if row.get("broker_queried") else 0, row.get("reconciliation_result") or "NOT_QUERIED",
                          int(row.get("duplicate_orders") or 0), row.get("position_continuity") or "UNKNOWN", _json(row.get("detail") or {})))
        self._write(fn)
        return rid

    def restarts(self, limit: int = 200, trading_date: Optional[str] = None) -> List[dict]:
        if trading_date:
            rows = self._read("SELECT * FROM forward_validation_restarts WHERE trading_date = ? ORDER BY at, rowid LIMIT ?", (trading_date, limit))
        else:
            rows = self._read("SELECT * FROM forward_validation_restarts ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["state_before"] = _loads(r.pop("state_before_json"), {})
            r["state_after"] = _loads(r.pop("state_after_json"), {})
            r["detail"] = _loads(r.pop("detail_json"), {})
            r["broker_queried"] = bool(r["broker_queried"])
        return rows

    def add_event(self, now: datetime, kind: str, code: Optional[str] = None, detail: Optional[dict] = None, trading_date: Optional[str] = None, owner: Optional[str] = None) -> str:
        eid = new_id()

        def fn(conn):
            conn.execute("INSERT INTO forward_validation_events (event_id, at, trading_date, kind, code, detail_json, owner) VALUES (?,?,?,?,?,?,?)",
                         (eid, _iso(now), trading_date, kind, code, _json(detail or {}), owner))
        self._write(fn)
        return eid

    def events(self, limit: int = 200, kind: Optional[str] = None, trading_date: Optional[str] = None, newest_first: bool = True) -> List[dict]:
        sql, args = "SELECT * FROM forward_validation_events WHERE 1 = 1", []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if trading_date:
            sql += " AND trading_date = ?"
            args.append(trading_date)
        sql += f" ORDER BY at {'DESC' if newest_first else 'ASC'}, rowid {'DESC' if newest_first else 'ASC'} LIMIT ?"
        args.append(limit)
        rows = self._read(sql, tuple(args))
        for r in rows:
            r["detail"] = _loads(r.pop("detail_json"), {})
        return rows

    # ---------------------------------------------------------------- derived summaries ------------------------------------------
    def put_daily(self, trading_date: str, now: datetime, version: str, summary: dict) -> None:
        def fn(conn):
            conn.execute("INSERT INTO forward_validation_daily_summary (trading_date, computed_at, version, summary_json) VALUES (?,?,?,?) ON CONFLICT(trading_date) DO UPDATE SET "
                         "computed_at = excluded.computed_at, version = excluded.version, summary_json = excluded.summary_json", (trading_date, _iso(now), version, _json(summary)))
        self._write(fn)

    def daily(self, trading_date: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM forward_validation_daily_summary WHERE trading_date = ?", (trading_date,))
        if not rows:
            return None
        r = rows[0]
        return {"trading_date": r["trading_date"], "computed_at": r["computed_at"], "version": r["version"], **_loads(r["summary_json"], {})}

    def dailies(self, limit: int = 400, newest_first: bool = True) -> List[dict]:
        rows = self._read(f"SELECT * FROM forward_validation_daily_summary ORDER BY trading_date {'DESC' if newest_first else 'ASC'} LIMIT ?", (limit,))
        return [{"trading_date": r["trading_date"], "computed_at": r["computed_at"], "version": r["version"], **_loads(r["summary_json"], {})} for r in rows]

    # ---------------------------------------------------------------- controls -------------------------------------------------------
    def touch_control(self, control_id: str, now: datetime, result: str, reason: str = "", error: Optional[str] = None) -> None:
        stamp = _iso(now)
        fired = result in ("RUN", "BLOCK")

        def fn(conn):
            conn.execute("INSERT INTO forward_validation_controls (control_id, last_evaluated_at, last_fired_at, evaluation_count, fire_count, last_result, last_reason, last_error, updated_at) "
                         "VALUES (?,?,?,1,?,?,?,?,?) ON CONFLICT(control_id) DO UPDATE SET last_evaluated_at = excluded.last_evaluated_at, "
                         "last_fired_at = COALESCE(excluded.last_fired_at, last_fired_at), evaluation_count = evaluation_count + 1, fire_count = fire_count + excluded.fire_count, "
                         "last_result = excluded.last_result, last_reason = excluded.last_reason, last_error = excluded.last_error, updated_at = excluded.updated_at",
                         (control_id, stamp, stamp if fired else None, 1 if fired else 0, result, reason[:300], error, stamp))
        self._write(fn)

    def controls(self) -> List[dict]:
        return self._read("SELECT * FROM forward_validation_controls ORDER BY control_id")

    def counts(self) -> Dict[str, int]:
        out = {}
        for t in ("forward_validation_sessions", "forward_validation_samples", "forward_validation_marks", "forward_validation_restarts", "forward_validation_events",
                  "forward_validation_daily_summary"):
            rows = self._read(f"SELECT count(*) AS n FROM {t}")
            out[t] = int(rows[0]["n"]) if rows else 0
        return out
