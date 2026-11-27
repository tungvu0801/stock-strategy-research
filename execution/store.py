"""
execution/store.py — SQLite persistence of the execution layer (DESIGN_60 §10). The database is the state: every intent is written
before the broker is contacted, fills and position changes are written in ONE transaction, history tables are append-only. Reads open
their own read-only connection and tolerate a database without the Stage 6.0 tables.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.execution_migrations import run_execution_migrations
from execution import INTENT_OPEN, POSITION_OPEN
from execution.models import ControlEvaluation, Finding, Intent, Position
from fit import readonly as RO


class ExecutionStoreError(RuntimeError):
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


INTENT_COLS = ("execution_id", "intent_type", "symbol", "side", "qty", "client_order_id", "proposal_id", "setup_id", "position_id", "exit_cycle", "exit_reason", "reference_price",
               "invalidation_price", "risk_budget", "proposal_qty", "state", "broker_order_id", "broker_status", "filled_qty", "filled_avg_price", "attempt_count", "lookup_count",
               "last_lookup_at", "failure_code", "failure_text", "outcome_class", "created_at", "submitted_at", "resolved_at", "updated_at", "created_by")
INTENT_MUTABLE = ("state", "broker_order_id", "broker_status", "filled_qty", "filled_avg_price", "attempt_count", "lookup_count", "last_lookup_at", "failure_code", "failure_text",
                  "outcome_class", "submitted_at", "resolved_at", "exit_reason")
POSITION_COLS = ("position_id", "execution_id", "proposal_id", "setup_id", "symbol", "side", "quantity", "entry_qty", "average_entry_price", "opened_at", "invalidation_price", "risk_budget",
                 "source", "broker", "state", "demand_low_streak", "last_demand_eval_at", "sessions_held", "weakening_reason", "exit_reason", "exit_cycle", "exit_requested_at", "exit_qty",
                 "average_exit_price", "realized_pnl", "last_price", "last_price_at", "attention_reason", "closed_at", "updated_at")
POSITION_MUTABLE = ("quantity", "entry_qty", "average_entry_price", "state", "demand_low_streak", "last_demand_eval_at", "sessions_held", "weakening_reason", "exit_reason", "exit_cycle",
                    "exit_requested_at", "exit_qty", "average_exit_price", "realized_pnl", "last_price", "last_price_at", "attention_reason", "closed_at", "broker")


class ExecutionStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---------------------------------------------------------------- connections -------------------------------------------
    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_execution_migrations(conn)
        return conn

    def _ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'execution_intents'")}
        except sqlite3.Error:
            conn.close()
            return None
        if "execution_intents" not in names:
            conn.close()
            return None
        return conn

    def _read(self, sql: str, args=()) -> List[dict]:
        conn = self._ro()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def exists(self) -> bool:
        conn = self._ro()
        if conn is None:
            return False
        conn.close()
        return True

    def ensure(self) -> None:
        self._rw().close()

    # ---------------------------------------------------------------- mode ------------------------------------------------------
    def mode(self) -> dict:
        rows = self._read("SELECT * FROM execution_mode WHERE id = 1")
        if not rows:
            return {"mode": "MONITOR_ONLY", "changed_at": None, "changed_by": None, "reason": None, "environment": {}, "persisted": False}
        r = rows[0]
        return {"mode": r["mode"], "changed_at": r["changed_at"], "changed_by": r["changed_by"], "reason": r["reason"], "environment": _loads(r["environment_json"], {}), "persisted": True}

    def set_mode(self, mode: str, now: datetime, actor: str, reason: Optional[str], environment: dict) -> dict:
        stamp = _iso(now)
        prev = self.mode()
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO execution_mode (id, mode, changed_at, changed_by, reason, environment_json) VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                         "mode = excluded.mode, changed_at = excluded.changed_at, changed_by = excluded.changed_by, reason = excluded.reason, environment_json = excluded.environment_json",
                         (mode, stamp, actor, reason, _json(environment or {})))
            conn.execute("INSERT INTO execution_mode_events (event_id, at, from_mode, to_mode, actor, reason, environment_json) VALUES (?,?,?,?,?,?,?)",
                         (new_id(), stamp, prev["mode"] if prev["persisted"] else None, mode, actor, reason, _json(environment or {})))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.mode()

    def mode_events(self, limit: int = 20) -> List[dict]:
        rows = self._read("SELECT * FROM execution_mode_events ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["environment"] = _loads(r.pop("environment_json"), {})
        return rows

    # ---------------------------------------------------------------- intents ----------------------------------------------------
    @staticmethod
    def _intent(r: dict) -> Intent:
        d = {k: r[k] for k in INTENT_COLS}
        d["proposal_snapshot"] = _loads(r.get("proposal_json"), {})
        d["environment"] = _loads(r.get("environment_json"), {})
        return Intent(**d)

    def intent(self, execution_id: str) -> Optional[Intent]:
        rows = self._read("SELECT * FROM execution_intents WHERE execution_id = ?", (execution_id,))
        return self._intent(rows[0]) if rows else None

    def intent_by_coid(self, client_order_id: str) -> Optional[Intent]:
        rows = self._read("SELECT * FROM execution_intents WHERE client_order_id = ?", (client_order_id,))
        return self._intent(rows[0]) if rows else None

    def intents(self, symbol: Optional[str] = None, states: Optional[Iterable[str]] = None, intent_type: Optional[str] = None, limit: int = 200) -> List[Intent]:
        sql, args = "SELECT * FROM execution_intents WHERE 1 = 1", []
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol.upper())
        if states:
            st = list(states)
            sql += f" AND state IN ({', '.join('?' * len(st))})"
            args += st
        if intent_type:
            sql += " AND intent_type = ?"
            args.append(intent_type)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [self._intent(r) for r in self._read(sql, tuple(args))]

    def open_intents(self, symbol: Optional[str] = None) -> List[Intent]:
        return self.intents(symbol, INTENT_OPEN)

    def intents_for_setup(self, setup_id: str) -> List[Intent]:
        return [self._intent(r) for r in self._read("SELECT * FROM execution_intents WHERE setup_id = ? AND intent_type = 'ENTRY' ORDER BY created_at", (setup_id,))]

    def insert_intent(self, intent: Intent) -> Intent:
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            d = intent.model_dump()
            vals = [d[c] for c in INTENT_COLS] + [_json(d["proposal_snapshot"]), _json(d["environment"])]
            conn.execute(f"INSERT INTO execution_intents ({', '.join(INTENT_COLS)}, proposal_json, environment_json) VALUES ({', '.join('?' * len(vals))})", vals)
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ExecutionStoreError(f"intent not inserted: {exc}") from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.intent(intent.execution_id)

    def update_intent(self, execution_id: str, now: datetime, **fields) -> Intent:
        bad = set(fields) - set(INTENT_MUTABLE)
        if bad:
            raise ExecutionStoreError(f"immutable intent columns: {sorted(bad)}")
        fields["updated_at"] = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"UPDATE execution_intents SET {', '.join(f'{k} = ?' for k in fields)} WHERE execution_id = ?", (*fields.values(), execution_id))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ExecutionStoreError(f"intent not updated: {exc}") from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.intent(execution_id)

    # ---------------------------------------------------------------- Emergency Stop order gate, C-7 (design revision 3.5, section 7) --
    STOP_REFUSALS = {"ON": "EMERGENCY_STOP_ACTIVE", "UNKNOWN": "EMERGENCY_STOP_STATE_UNKNOWN"}

    @staticmethod
    def _latch(conn: sqlite3.Connection) -> str:
        """The Emergency Stop latch row, by raw SQL (no import from safety/): "OFF" only for exactly 0, "ON" for exactly 1, anything else
        (no table, no row, a read error, another value) "UNKNOWN". A failed SELECT leaves the caller's transaction usable."""
        try:
            rows = conn.execute("SELECT kill_active FROM execution_safety_state WHERE id = 1").fetchall()
        except sqlite3.Error:
            return "UNKNOWN"
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] not in (0, 1):
            return "UNKNOWN"
        return "OFF" if rows[0][0] == 0 else "ON"

    def record_entry_attempt(self, execution_id: str, now: datetime) -> Optional[str]:
        """ENTRY only. ONE BEGIN IMMEDIATE: read the latch; anything but exactly 0 returns a refusal code and writes nothing. Otherwise record
        the attempt (attempt_count = 1, submitted_at) in the same transaction, BEFORE the wire. The latch and this commit form one total order
        with a stop's commit (both BEGIN IMMEDIATE on one file): if KILL_ON commits first, the entry is refused. A locked database raises,
        as update_intent does, so nothing is posted."""
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = self._latch(conn)
            if state != "OFF":
                conn.execute("ROLLBACK")
                return self.STOP_REFUSALS[state]
            stamp = _iso(now)
            conn.execute("UPDATE execution_intents SET attempt_count = ?, submitted_at = ?, updated_at = ? WHERE execution_id = ?",
                         (1, stamp, stamp, execution_id))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ExecutionStoreError(f"intent not updated: {exc}") from None
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return None

    def stop_state(self) -> str:
        """The latch, read-only (the note after an entry's POST). "UNKNOWN" when it cannot be read."""
        if not self.path.exists():
            return "UNKNOWN"
        try:
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        except sqlite3.Error:
            return "UNKNOWN"
        try:
            return self._latch(conn)
        finally:
            conn.close()

    # ---------------------------------------------------------------- broker orders (current view + append-only events) -------
    def _upsert_order(self, conn: sqlite3.Connection, order: dict, execution_id: Optional[str], stamp: str, source: str) -> bool:
        """Returns True when the status / fill changed (an event row was appended)."""
        prev = conn.execute("SELECT status, filled_qty, filled_avg_price FROM execution_orders WHERE broker_order_id = ?", (order["id"],)).fetchone()
        changed = prev is None or prev["status"] != order["status"] or int(prev["filled_qty"]) != int(order["filled_qty"]) or prev["filled_avg_price"] != order["filled_avg_price"]
        conn.execute("INSERT INTO execution_orders (broker_order_id, client_order_id, execution_id, symbol, side, qty, status, filled_qty, filled_avg_price, submitted_at, broker_updated_at, "
                     "first_seen_at, last_seen_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(broker_order_id) DO UPDATE SET status = excluded.status, "
                     "filled_qty = excluded.filled_qty, filled_avg_price = excluded.filled_avg_price, broker_updated_at = excluded.broker_updated_at, last_seen_at = excluded.last_seen_at, "
                     "raw_json = excluded.raw_json, execution_id = COALESCE(execution_orders.execution_id, excluded.execution_id)",
                     (order["id"], order.get("client_order_id") or "", execution_id, order.get("symbol") or "", order.get("side") or "", order.get("qty"), order.get("status"),
                      int(order.get("filled_qty") or 0), order.get("filled_avg_price"), order.get("submitted_at"), order.get("updated_at"), stamp, stamp, _json(order.get("raw") or {})))
        if changed:
            conn.execute("INSERT INTO execution_order_events (event_id, broker_order_id, execution_id, at, status, filled_qty, filled_avg_price, source, raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (new_id(), order["id"], execution_id, stamp, order.get("status"), int(order.get("filled_qty") or 0), order.get("filled_avg_price"), source, _json(order.get("raw") or {})))
        return changed

    def record_order(self, order: dict, execution_id: Optional[str], now: datetime, source: str, intent_fields: Optional[dict] = None) -> bool:
        """The broker's current view of an order + (optionally) the owning intent's lifecycle columns, in ONE transaction."""
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._upsert_order(conn, order, execution_id, stamp, source)
            if intent_fields and execution_id:
                bad = set(intent_fields) - set(INTENT_MUTABLE)
                if bad:
                    raise ExecutionStoreError(f"immutable intent columns: {sorted(bad)}")
                f = dict(intent_fields, updated_at=stamp)
                conn.execute(f"UPDATE execution_intents SET {', '.join(f'{k} = ?' for k in f)} WHERE execution_id = ?", (*f.values(), execution_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return changed

    def orders(self, limit: int = 50, execution_id: Optional[str] = None) -> List[dict]:
        if execution_id:
            rows = self._read("SELECT * FROM execution_orders WHERE execution_id = ? ORDER BY first_seen_at DESC LIMIT ?", (execution_id, limit))
        else:
            rows = self._read("SELECT * FROM execution_orders ORDER BY first_seen_at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["raw"] = _loads(r.pop("raw_json"), {})
        return rows

    def order_events(self, broker_order_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        if broker_order_id:
            rows = self._read("SELECT * FROM execution_order_events WHERE broker_order_id = ? ORDER BY at, rowid LIMIT ?", (broker_order_id, limit))
        else:
            rows = self._read("SELECT * FROM execution_order_events ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["raw"] = _loads(r.pop("raw_json"), {})
        return rows

    # ---------------------------------------------------------------- positions --------------------------------------------------
    @staticmethod
    def _position(r: dict) -> Position:
        return Position(**{k: r[k] for k in POSITION_COLS})

    def position(self, position_id: str) -> Optional[Position]:
        rows = self._read("SELECT * FROM paper_positions WHERE position_id = ?", (position_id,))
        return self._position(rows[0]) if rows else None

    def position_for_execution(self, execution_id: str) -> Optional[Position]:
        rows = self._read("SELECT * FROM paper_positions WHERE execution_id = ?", (execution_id,))
        return self._position(rows[0]) if rows else None

    def positions(self, symbol: Optional[str] = None, open_only: bool = True, limit: int = 200) -> List[Position]:
        sql, args = "SELECT * FROM paper_positions WHERE 1 = 1", []
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol.upper())
        if open_only:
            sql += f" AND state IN ({', '.join('?' * len(POSITION_OPEN))})"
            args += list(POSITION_OPEN)
        sql += " ORDER BY opened_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [self._position(r) for r in self._read(sql, tuple(args))]

    def open_position(self, symbol: str) -> Optional[Position]:
        rows = self.positions(symbol, open_only=True, limit=1)
        return rows[0] if rows else None

    def apply_fill(self, order: dict, intent_fields: dict, execution_id: str, now: datetime, *, new_position: Optional[Position] = None,
                   position_update: Optional[dict] = None, position_event: Optional[dict] = None) -> None:
        """A broker-confirmed fill: the order view, the intent, and the position creation / update + its event, in ONE transaction."""
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_order(conn, order, execution_id, stamp, "fill")
            f = dict(intent_fields, updated_at=stamp)
            conn.execute(f"UPDATE execution_intents SET {', '.join(f'{k} = ?' for k in f)} WHERE execution_id = ?", (*f.values(), execution_id))
            if new_position is not None:
                d = new_position.model_dump()
                conn.execute(f"INSERT INTO paper_positions ({', '.join(POSITION_COLS)}) VALUES ({', '.join('?' * len(POSITION_COLS))})", [d[c] for c in POSITION_COLS])
                pid = new_position.position_id
            else:
                pid = (position_update or {}).pop("position_id", None) or (position_event or {}).get("position_id")
                if position_update:
                    bad = set(position_update) - set(POSITION_MUTABLE)
                    if bad:
                        raise ExecutionStoreError(f"immutable position columns: {sorted(bad)}")
                    pu = dict(position_update, updated_at=stamp)
                    conn.execute(f"UPDATE paper_positions SET {', '.join(f'{k} = ?' for k in pu)} WHERE position_id = ?", (*pu.values(), pid))
            if position_event is not None:
                e = dict(position_event)
                conn.execute("INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                             (new_id(), e.get("position_id") or pid, stamp, e["kind"], e.get("from_state"), e.get("to_state"), e.get("reason"), _json(e.get("detail") or {})))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def update_position(self, position_id: str, now: datetime, event: Optional[dict] = None, **fields) -> Position:
        bad = set(fields) - set(POSITION_MUTABLE)
        if bad:
            raise ExecutionStoreError(f"immutable position columns: {sorted(bad)}")
        stamp = _iso(now)
        fields["updated_at"] = stamp
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"UPDATE paper_positions SET {', '.join(f'{k} = ?' for k in fields)} WHERE position_id = ?", (*fields.values(), position_id))
            if event is not None:
                conn.execute("INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                             (new_id(), position_id, stamp, event["kind"], event.get("from_state"), event.get("to_state"), event.get("reason"), _json(event.get("detail") or {})))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ExecutionStoreError(f"position not updated: {exc}") from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.position(position_id)

    def position_events(self, position_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        if position_id:
            rows = self._read("SELECT * FROM position_events WHERE position_id = ? ORDER BY at, rowid LIMIT ?", (position_id, limit))
        else:
            rows = self._read("SELECT * FROM position_events ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = _loads(r.pop("detail_json"), {})
        return rows

    # ---------------------------------------------------------------- Wave 2 PR A: accounting reads (read only) -------------------
    def exit_intents_for_position(self, position_id: str) -> List[Intent]:
        """Every EXIT intent of one position (no LIMIT), in exit-cycle order: the per-order cumulative (Q, A) the exit P/L is recomputed from."""
        return [self._intent(r) for r in self._read("SELECT * FROM execution_intents WHERE position_id = ? AND intent_type = 'EXIT' ORDER BY exit_cycle, rowid",
                                                    (position_id,))]

    def accounting_view(self, position_id: str) -> Optional[dict]:
        """One read transaction: the position, its entry intent, all its exit intents and its latest P/L-snapshot event (None: no such position).
        Raises on an unreadable database or a missing table - never an empty view (Wave 2 design 3.1.4, J-08)."""
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)   # a missing file is an error here, never an empty result
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            p = conn.execute("SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)).fetchone()
            if p is None:
                return None
            pos = self._position(dict(p))
            ent = conn.execute("SELECT * FROM execution_intents WHERE execution_id = ?", (pos.execution_id,)).fetchone()
            exits = conn.execute("SELECT * FROM execution_intents WHERE position_id = ? AND intent_type = 'EXIT' ORDER BY exit_cycle, rowid", (position_id,)).fetchall()
            ev = conn.execute("SELECT rowid AS event_rowid, kind, detail_json FROM position_events WHERE position_id = ? AND instr(detail_json, '\"pnl_snapshot\"') > 0 "
                              "ORDER BY rowid DESC LIMIT 1", (position_id,)).fetchone()
            conn.execute("COMMIT")
        finally:
            conn.close()
        snap = None
        if ev is not None:
            snap = {"event_rowid": ev["event_rowid"], "kind": ev["kind"], "pnl_snapshot": (_loads(ev["detail_json"], {}) or {}).get("pnl_snapshot")}
        return {"position": pos, "entry": self._intent(dict(ent)) if ent is not None else None, "exits": [self._intent(dict(r)) for r in exits], "snapshot": snap}

    def pnl_watermark_rows(self, start: str, end: str) -> List[list]:
        """W(d) inputs (Wave 2 design 3.1.7): per CLOSED position with closed_at in [start, end), the latest position-event rowid, the latest order-event
        rowid of its entry and exit intents and their latest updated_at - one statement, one read transaction. Raises on any read error (H-04)."""
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)   # a missing file is an error here, never an empty result
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT p.position_id, p.closed_at, "
                "(SELECT MAX(e.rowid) FROM position_events e WHERE e.position_id = p.position_id) AS pe, "
                "(SELECT MAX(o.rowid) FROM execution_order_events o JOIN execution_intents i ON i.execution_id = o.execution_id "
                " WHERE i.execution_id = p.execution_id OR i.position_id = p.position_id) AS oe, "
                "(SELECT MAX(i.updated_at) FROM execution_intents i WHERE i.execution_id = p.execution_id OR i.position_id = p.position_id) AS iu "
                "FROM paper_positions p WHERE p.state = 'CLOSED' AND p.closed_at >= ? AND p.closed_at < ? ORDER BY p.position_id", (start, end)).fetchall()
            conn.execute("COMMIT")
        finally:
            conn.close()
        return [[r["position_id"], r["closed_at"], r["pe"], r["oe"], r["iu"]] for r in rows]

    # ---------------------------------------------------------------- reconciliation findings / runs / controls ---------------
    def add_findings(self, findings: List[Finding], now: datetime, trigger: str, run_id: Optional[str]) -> None:
        if not findings:
            return
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for f in findings:
                conn.execute("INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, execution_id, position_id, broker_order_id, detail_json) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (new_id(), run_id, stamp, trigger, f.kind, f.symbol, 1 if f.blocking else 0, f.execution_id, f.position_id, f.broker_order_id, _json(f.detail)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def findings(self, limit: int = 50, blocking_only: bool = False, run_id: Optional[str] = None) -> List[dict]:
        sql, args = "SELECT * FROM execution_reconciliations WHERE 1 = 1", []
        if blocking_only:
            sql += " AND blocking = 1"
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        sql += " ORDER BY at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        rows = self._read(sql, tuple(args))
        for r in rows:
            r["blocking"] = bool(r["blocking"])
            r["detail"] = _loads(r.pop("detail_json"), {})
        return rows

    # ---------------------------------------------------------------- HDR-2 r14 3.13.3: the safety-finding lifecycle rows (additive) ---
    def safety_states(self, position_ids: Optional[Iterable[str]] = None, *, conn: Optional[sqlite3.Connection] = None) -> Dict[tuple, dict]:
        """r14 3.13.3: per key (family, position_id), the state of the LATEST row by rowid among the family's alert kind and its resolution
        kind (rowid is insertion order: the table never deletes or replaces; `at` is never used for ordering). No row: NONE. The alert kind with
        detail lifecycle ACTIVE or RECURRENT: that state; with an unreadable or unexpected detail: ACTIVE (fail closed), and then no exit_cycle
        or evaluation is reported (derived evidence never applies to it). The resolution kind: RESOLVED. STRICT: a missing database file or
        table, or any SQLite error, raises to the caller (never an empty result). `position_ids` given: every family of every id is reported,
        NONE included. `conn`: read inside the caller's transaction (add_safety_transition's compare-and-set). Reads only."""
        families = {"STOP_CHECK_BLIND": "STOP_CHECK_BLIND_RESOLVED", "PROTECTIVE_EXIT_UNVERIFIED": "PROTECTIVE_EXIT_UNVERIFIED_RESOLVED",
                    "STRATEGY_EXIT_HELD": "STRATEGY_EXIT_HELD_RESOLVED"}
        ids = None if position_ids is None else [str(p) for p in position_ids]
        out: Dict[tuple, dict] = {}

        def blank(fam: str, pid: str) -> dict:
            return {"family": fam, "position_id": pid, "state": "NONE", "symbol": None, "rowid": None, "kind": None, "alerts": 0, "detail_readable": None,
                    "exit_cycle": None, "evaluation": None}
        for pid in ids or ():
            for fam in families:
                out[(fam, pid)] = blank(fam, pid)
        if ids is not None and not ids:
            return out
        kinds = list(families) + list(families.values())
        sql = f"SELECT rowid AS rid, kind, position_id, symbol, detail_json FROM execution_reconciliations WHERE kind IN ({', '.join('?' * len(kinds))})"
        args: list = list(kinds)
        if ids is not None:
            sql += f" AND position_id IN ({', '.join('?' * len(ids))})"
            args += ids
        sql += " ORDER BY rowid"
        own = conn is None
        if own:
            if not self.path.exists():
                raise ExecutionStoreError("safety state unreadable: no database file")
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            if own:
                conn.close()
        resolution_of = {v: k for k, v in families.items()}
        for r in rows:
            pid, kind = r[2], r[1]
            if not isinstance(pid, str) or not pid:
                continue                                                         # never a lifecycle row: position_id is always set (3.13.2)
            fam = kind if kind in families else resolution_of[kind]
            e = out.setdefault((fam, pid), blank(fam, pid))
            e.update(rowid=r[0], kind=kind, symbol=r[3])
            if kind == fam:
                e["alerts"] += 1
                det = _loads(r[4], None)
                life = det.get("lifecycle") if isinstance(det, dict) else None
                ok = isinstance(life, str) and life in ("ACTIVE", "RECURRENT")
                ec = det.get("exit_cycle") if ok else None
                ev = det.get("evaluation") if ok else None
                e.update(state=life if ok else "ACTIVE", detail_readable=ok, exit_cycle=ec if type(ec) is int else None, evaluation=ev if isinstance(ev, str) else None)
            else:
                e.update(state="RESOLVED", detail_readable=None, exit_cycle=None, evaluation=None)
        return out

    def add_safety_transition(self, family: str, position_id: str, symbol: str, expected_state: str, now: datetime, trigger: str, run_id: Optional[str], *,
                              alert: Optional[dict] = None, resolution: Optional[dict] = None) -> bool:
        """r14 3.13.3, 3.13.8 and 7: inside ONE BEGIN IMMEDIATE, re-derive the key's state and insert only if it is still `expected_state`, the
        state the caller decided from (compare-and-set). Returns whether it wrote. Forms: an alert row from NONE (lifecycle ACTIVE) or RESOLVED
        (RECURRENT), episode = 1 + the key's earlier alert rows counted in this transaction; a resolution row from ACTIVE or RECURRENT; or the
        two-row form (a deferred alert row and its resolution row) from NONE or RESOLVED, both or neither. Columns: kind, symbol, position_id,
        blocking (the two blocking families' alert rows only), trigger, run_id; execution_id and broker_order_id stay empty. Detail: stable
        values only. Evidence is checked per family (families are independent, 3.13.2). An invalid call raises; a database error raises."""
        evidence_codes = {"STOP_CHECK_BLIND": ("STOP_VERIFIED_NOT_HIT", "STOP_VERIFIED_HIT", "POSITION_CLOSED"),
                          "PROTECTIVE_EXIT_UNVERIFIED": ("POSITION_CLOSED", "PROTECTIVE_EXIT_ACCEPTED", "PROTECTIVE_FACTS_VERIFIED"),
                          "STRATEGY_EXIT_HELD": ("STRATEGY_EXIT_ACCEPTED", "POSITION_CLOSED", "HELD_REASONS_CLEARED")}
        if family not in evidence_codes:
            raise ExecutionStoreError(f"not a safety family: {family!r}")
        if not isinstance(position_id, str) or not position_id or not isinstance(symbol, str) or not symbol:
            raise ExecutionStoreError("a safety row needs position_id and symbol")
        if alert is None and resolution is None:
            raise ExecutionStoreError("nothing to write")
        if alert is not None:
            if expected_state not in ("NONE", "RESOLVED"):
                raise ExecutionStoreError(f"an alert row is written only from NONE or RESOLVED, not {expected_state!r}")
            allowed = {"reason_codes", "deferred"} | ({"exit_cycle"} if family != "STOP_CHECK_BLIND" else set()) | ({"evaluation"} if family == "STRATEGY_EXIT_HELD" else set())
            if set(alert) - allowed:
                raise ExecutionStoreError(f"alert detail fields not allowed: {sorted(set(alert) - allowed)}")
            codes = alert.get("reason_codes")
            if not isinstance(codes, (list, tuple)) or not all(isinstance(c, str) and c for c in codes):
                raise ExecutionStoreError("reason_codes must be a list of codes")
            if "exit_cycle" in alert and type(alert["exit_cycle"]) is not int:
                raise ExecutionStoreError("exit_cycle must be an integer")
            if "evaluation" in alert and (not isinstance(alert["evaluation"], str) or not alert["evaluation"]):
                raise ExecutionStoreError("evaluation must be the setup evaluation's identity")
            if "deferred" in alert and alert["deferred"] is not True:
                raise ExecutionStoreError("deferred is only ever true")
            if resolution is not None and alert.get("deferred") is not True:
                raise ExecutionStoreError("the two-row form is the deferred episode")
        elif expected_state not in ("ACTIVE", "RECURRENT"):
            raise ExecutionStoreError(f"a resolution row is written only from ACTIVE or RECURRENT, not {expected_state!r}")
        if resolution is not None:
            if set(resolution) - {"evidence", "execution_id", "derived"}:
                raise ExecutionStoreError(f"resolution detail fields not allowed: {sorted(set(resolution) - {'evidence', 'execution_id', 'derived'})}")
            if resolution.get("evidence") not in evidence_codes[family]:
                raise ExecutionStoreError(f"{resolution.get('evidence')!r} is not evidence for {family}")
            if "execution_id" in resolution and (not isinstance(resolution["execution_id"], str) or not resolution["execution_id"]):
                raise ExecutionStoreError("execution_id must name the evidencing intent")
            if "derived" in resolution and resolution["derived"] is not True:
                raise ExecutionStoreError("derived is only ever true")
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = self.safety_states([position_id], conn=conn)[(family, position_id)]
            if cur["state"] != expected_state:
                conn.execute("ROLLBACK")
                return False
            episode = int(cur["alerts"])
            rows = []
            if alert is not None:
                episode += 1
                det = dict(alert, reason_codes=list(alert["reason_codes"]), lifecycle="ACTIVE" if expected_state == "NONE" else "RECURRENT", episode=episode)
                rows.append((family, 0 if family == "STRATEGY_EXIT_HELD" else 1, det))
            if resolution is not None:
                rows.append((family + "_RESOLVED", 0, dict(resolution, episode=episode)))
            for kind, blk, det in rows:
                conn.execute("INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, execution_id, position_id, broker_order_id, "
                             "detail_json) VALUES (?,?,?,?,?,?,?,NULL,?,NULL,?)", (new_id(), run_id, stamp, trigger, kind, symbol, blk, position_id, _json(det)))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return True

    def add_run(self, run_id: str, started_at: datetime, finished_at: datetime, trigger: str, mode: str, result: str, summary: dict, error: Optional[str], owner: str) -> None:
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO execution_runs (run_id, started_at, finished_at, trigger, mode, result, summary_json, error, owner) VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id, _iso(started_at), _iso(finished_at), trigger, mode, result, _json(summary), error, owner))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def runs(self, limit: int = 20) -> List[dict]:
        rows = self._read("SELECT * FROM execution_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = _loads(r.pop("summary_json"), {})
        return rows

    def last_run(self) -> Optional[dict]:
        rows = self.runs(1)
        return rows[0] if rows else None

    def touch_controls(self, controls: List[ControlEvaluation], now: datetime, error: Optional[str] = None) -> None:
        if not controls:
            return
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for c in controls:
                fired = c.result in ("BLOCK", "RUN")
                conn.execute("INSERT INTO execution_controls (control_id, last_evaluated_at, last_fired_at, evaluation_count, fire_count, last_result, last_reason, last_input_json, last_error, "
                             "updated_at) VALUES (?,?,?,1,?,?,?,?,?,?) ON CONFLICT(control_id) DO UPDATE SET last_evaluated_at = excluded.last_evaluated_at, "
                             "last_fired_at = COALESCE(excluded.last_fired_at, last_fired_at), evaluation_count = evaluation_count + 1, fire_count = fire_count + excluded.fire_count, "
                             "last_result = excluded.last_result, last_reason = excluded.last_reason, last_input_json = excluded.last_input_json, last_error = excluded.last_error, "
                             "updated_at = excluded.updated_at", (c.control_id, stamp, stamp if fired else None, 1 if fired else 0, c.result, c.reason, _json(c.input), error, stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def controls(self) -> List[dict]:
        rows = self._read("SELECT * FROM execution_controls ORDER BY control_id")
        for r in rows:
            r["last_input"] = _loads(r.pop("last_input_json"), {})
        return rows

    def counts(self) -> Dict[str, int]:
        out = {}
        for t in ("execution_intents", "execution_orders", "execution_order_events", "paper_positions", "position_events", "execution_reconciliations", "execution_runs", "execution_controls"):
            rows = self._read(f"SELECT count(*) AS n FROM {t}")
            out[t] = int(rows[0]["n"]) if rows else 0
        return out
