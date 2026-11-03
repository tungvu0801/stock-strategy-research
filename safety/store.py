"""
safety/store.py — SQLite persistence for the execution safety gate (DESIGN_591 §9). The state upsert, the events, the evaluation row,
the new baseline and the control liveness rows of one evaluation are written in ONE transaction; nothing is reported as persisted
unless the commit succeeded. Reads open their own read-only connection and tolerate a database without the tables.

Wave 1 (audit EX-08 / EX-08b): the state row is the latch of the Emergency Stop, the Pause and the day's loss halt, so
  * `state()` reads it strictly: no database file or no table yet is the default state (a first run); any other read failure raises
    SafetyStoreError - an unreadable row is never "no kill switch";
  * `commit(..., expect=...)` checks INSIDE its BEGIN IMMEDIATE transaction that the row (and today's baseline) is still what the caller read;
    if another writer changed it meanwhile it rolls back and raises SafetyStateChanged, so the caller decides again on the new state and a
    concurrent stop, pause or halt is never overwritten by an evaluation that read the state before it;
  * `latch(...)` sets an Emergency Stop or a Pause alone (only its own columns, only while it is off) when the evaluation that should have
    applied it failed, so a stop clicked while the row cannot be read is still saved.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.execution_safety_migrations import run_execution_safety_migrations
from fit import readonly as RO
from safety.models import Baseline, ControlEvaluation, SafetyStateRecord


class SafetyStoreError(RuntimeError):
    pass


class SafetyStateChanged(SafetyStoreError):
    """Wave 1: the state row or today's baseline changed between the caller's read and its commit; nothing was written."""


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


STATE_COLS = ("kill_active", "kill_activated_at", "kill_activated_by", "kill_reason", "pause_active", "pause_activated_at", "pause_activated_by", "pause_reason",
              "daily_halt_active", "daily_halt_trading_date", "daily_halt_triggered_at", "daily_halt_loss_pct", "daily_halt_reason", "updated_at")


class SafetyStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---------------------------------------------------------------- connections -------------------------------------------
    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_execution_safety_migrations(conn)
        return conn

    def _ro(self, strict: bool = False) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'execution_safety_%' OR name = 'daily_risk_baselines'")}
        except sqlite3.Error as exc:
            conn.close()
            if strict:                                                                         # Wave 1: unreadable is not "no table yet"
                raise SafetyStoreError(f"the safety state cannot be read ({type(exc).__name__}: {exc})") from exc
            return None
        if "execution_safety_state" not in names:
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

    # ---------------------------------------------------------------- reads ----------------------------------------------------
    @staticmethod
    def _state_of(r: Optional[dict]) -> SafetyStateRecord:
        if not r:
            return SafetyStateRecord()
        return SafetyStateRecord(**{k: (bool(r[k]) if k.endswith("_active") else r[k]) for k in STATE_COLS})

    def state(self) -> SafetyStateRecord:
        """The latch row, read strictly (Wave 1): no database or no table yet is the default state; an unreadable row raises SafetyStoreError."""
        conn = self._ro(strict=True)
        if conn is None:
            return SafetyStateRecord()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM execution_safety_state WHERE id = 1").fetchall()]
        except sqlite3.Error as exc:
            raise SafetyStoreError(f"the safety state cannot be read ({type(exc).__name__}: {exc})") from exc
        finally:
            conn.close()
        return self._state_of(rows[0] if rows else None)

    def has_state_row(self) -> bool:
        return bool(self._read("SELECT 1 FROM execution_safety_state WHERE id = 1"))

    def baseline(self, trading_date: str) -> Optional[Baseline]:
        rows = self._read("SELECT * FROM daily_risk_baselines WHERE trading_date = ?", (trading_date,))
        return Baseline(**rows[0]) if rows else None

    def baselines(self, limit: int = 30) -> List[dict]:
        return self._read("SELECT * FROM daily_risk_baselines ORDER BY trading_date DESC LIMIT ?", (limit,))

    def events(self, limit: int = 50, kind: Optional[str] = None) -> List[dict]:
        if kind:
            rows = self._read("SELECT * FROM execution_safety_events WHERE kind = ? ORDER BY at DESC, rowid DESC LIMIT ?", (kind, limit))
        else:
            rows = self._read("SELECT * FROM execution_safety_events ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["input"] = _loads(r.pop("input_json"), {})
        return rows

    def event_count(self, kind: Optional[str] = None) -> int:
        rows = self._read("SELECT count(*) AS n FROM execution_safety_events WHERE kind = ?", (kind,)) if kind else self._read("SELECT count(*) AS n FROM execution_safety_events")
        return int(rows[0]["n"]) if rows else 0

    def evaluations(self, limit: int = 50) -> List[dict]:
        rows = self._read("SELECT * FROM execution_safety_evaluations ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (limit,))
        return [self._decode_eval(r) for r in rows]

    def last_evaluation(self) -> Optional[dict]:
        rows = self.evaluations(1)
        return rows[0] if rows else None

    def evaluation_count(self) -> int:
        rows = self._read("SELECT count(*) AS n FROM execution_safety_evaluations")
        return int(rows[0]["n"]) if rows else 0

    @staticmethod
    def _decode_eval(r: dict) -> dict:
        out = dict(r)
        out["allowed_new_entry"] = bool(out["allowed_new_entry"])
        for col, key, default in (("reason_codes_json", "reason_codes", []), ("daily_loss_json", "daily_loss", {}), ("kill_json", "kill_switch", {}), ("pause_json", "no_new_entries", {}),
                                  ("freshness_json", "freshness", {}), ("input_json", "input", {})):
            out[key] = _loads(out.pop(col), default)
        return out

    def controls(self) -> List[dict]:
        rows = self._read("SELECT * FROM execution_safety_controls ORDER BY control_id")
        for r in rows:
            r["last_input"] = _loads(r.pop("last_input_json"), {})
        return rows

    # ---------------------------------------------------------------- the one write --------------------------------------------
    def commit(self, now: datetime, *, state: SafetyStateRecord, events: List[Dict[str, Any]], evaluation: Optional[Dict[str, Any]], controls: List[ControlEvaluation],
               new_baseline: Optional[Baseline] = None, control_error: Optional[str] = None, expect: Optional[SafetyStateRecord] = None) -> None:
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if expect is not None:                                                             # Wave 1: inside the write lock, the row must be what the caller read
                row = conn.execute("SELECT * FROM execution_safety_state WHERE id = 1").fetchone()
                now_state = self._state_of(dict(row) if row else None)
                if any(getattr(now_state, k) != getattr(expect, k) for k in STATE_COLS if k != "updated_at"):
                    raise SafetyStateChanged("the safety state changed since it was read")
                if new_baseline is not None and conn.execute("SELECT 1 FROM daily_risk_baselines WHERE trading_date = ?", (new_baseline.trading_date,)).fetchone():
                    raise SafetyStateChanged(f"the {new_baseline.trading_date} baseline was created since it was read")
            vals = {k: getattr(state, k) for k in STATE_COLS}
            vals["updated_at"] = stamp
            for k in ("kill_active", "pause_active", "daily_halt_active"):
                vals[k] = 1 if vals[k] else 0
            conn.execute("INSERT INTO execution_safety_state (id, " + ", ".join(STATE_COLS) + ") VALUES (1, " + ", ".join("?" * len(STATE_COLS)) + ") "
                         "ON CONFLICT(id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in STATE_COLS), tuple(vals[c] for c in STATE_COLS))
            if new_baseline is not None:
                conn.execute("INSERT INTO daily_risk_baselines (trading_date, starting_equity, baseline_created_at, source, adjustments, status, equity_as_of, note) VALUES (?,?,?,?,?,?,?,?)",
                             (new_baseline.trading_date, new_baseline.starting_equity, new_baseline.baseline_created_at, new_baseline.source, new_baseline.adjustments,
                              new_baseline.status, new_baseline.equity_as_of, new_baseline.note))
            for e in events:
                conn.execute("INSERT INTO execution_safety_events (event_id, at, kind, actor, reason, trading_date, from_state, to_state, input_json) VALUES (?,?,?,?,?,?,?,?,?)",
                             (new_id(), e.get("at") or stamp, e["kind"], e.get("actor") or "system", e.get("reason"), e.get("trading_date"), e.get("from_state"), e.get("to_state"),
                              _json(e.get("input") or {})))
            if evaluation is not None:
                conn.execute("INSERT INTO execution_safety_evaluations (evaluation_id, evaluated_at, trigger, state, allowed_new_entry, reason_codes_json, daily_loss_json, kill_json, "
                             "pause_json, freshness_json, input_json, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (new_id(), stamp, evaluation["trigger"], evaluation["state"], 1 if evaluation["allowed_new_entry"] else 0, _json(evaluation["reason_codes"]),
                              _json(evaluation["daily_loss"]), _json(evaluation["kill_switch"]), _json(evaluation["no_new_entries"]), _json(evaluation["freshness"]),
                              _json(evaluation.get("input") or {}), evaluation.get("error")))
            for c in controls:
                fired = c.result == "BLOCK"
                conn.execute("INSERT INTO execution_safety_controls (control_id, last_evaluated_at, last_fired_at, evaluation_count, fire_count, last_result, last_reason, last_input_json, "
                             "last_error, updated_at) VALUES (?,?,?,1,?,?,?,?,?,?) ON CONFLICT(control_id) DO UPDATE SET last_evaluated_at = excluded.last_evaluated_at, "
                             "last_fired_at = COALESCE(excluded.last_fired_at, last_fired_at), evaluation_count = evaluation_count + 1, fire_count = fire_count + excluded.fire_count, "
                             "last_result = excluded.last_result, last_reason = excluded.last_reason, last_input_json = excluded.last_input_json, last_error = excluded.last_error, "
                             "updated_at = excluded.updated_at",
                             (c.control_id, stamp, stamp if fired else None, 1 if fired else 0, c.result, c.reason, _json(c.input), control_error, stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def latch(self, now: datetime, update: Dict[str, Any], event: Dict[str, Any]) -> bool:
        """Wave 1 (independent review M1): sets ONE latch - the Emergency Stop or the Pause - without the strict read, for the case where the
        evaluation that should have applied it failed. Only that latch's own columns change, and only while it is off, so this can never clear
        or overwrite any other state. True when it was set now (with its event), False when it was already on."""
        flag = next(k for k in update if k.endswith("_active"))
        prefix = flag[:-len("active")]
        if flag not in ("kill_active", "pause_active") or update[flag] is not True or not all(k.startswith(prefix) and k in STATE_COLS for k in update):
            raise ValueError(f"not a stop or pause latch: {sorted(update)}")
        stamp = _iso(now)
        default = SafetyStateRecord()
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO execution_safety_state (id, " + ", ".join(STATE_COLS) + ") VALUES (1, " + ", ".join("?" * len(STATE_COLS)) + ")",
                         tuple(stamp if c == "updated_at" else (1 if getattr(default, c) else 0) if c.endswith("_active") else getattr(default, c) for c in STATE_COLS))
            cols = sorted(update)
            cur = conn.execute("UPDATE execution_safety_state SET " + ", ".join(f"{c} = ?" for c in cols) + f", updated_at = ? WHERE id = 1 AND {flag} = 0",
                               tuple(1 if c == flag else update[c] for c in cols) + (stamp,))
            done = cur.rowcount == 1
            if done:
                conn.execute("INSERT INTO execution_safety_events (event_id, at, kind, actor, reason, trading_date, from_state, to_state, input_json) VALUES (?,?,?,?,?,?,?,?,?)",
                             (new_id(), stamp, event["kind"], event.get("actor") or "system", event.get("reason"), event.get("trading_date"), event.get("from_state"),
                              event.get("to_state"), _json(event.get("input") or {})))
            conn.execute("COMMIT")
            return done
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_error(self, now: datetime, trigger: str, error: str, control_ids: List[str]) -> None:
        """Best effort after an evaluator failure: an EVALUATOR_ERROR event and the error on the control liveness rows."""
        stamp = _iso(now)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO execution_safety_events (event_id, at, kind, actor, reason, trading_date, from_state, to_state, input_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (new_id(), stamp, "EVALUATOR_ERROR", "system", error[:500], None, None, "UNKNOWN", _json({"trigger": trigger})))
            for cid in control_ids:
                conn.execute("INSERT INTO execution_safety_controls (control_id, last_evaluated_at, last_fired_at, evaluation_count, fire_count, last_result, last_reason, last_input_json, "
                             "last_error, updated_at) VALUES (?,?,NULL,1,0,'UNKNOWN',?,?,?,?) ON CONFLICT(control_id) DO UPDATE SET last_evaluated_at = excluded.last_evaluated_at, "
                             "evaluation_count = evaluation_count + 1, last_result = 'UNKNOWN', last_reason = excluded.last_reason, last_error = excluded.last_error, updated_at = excluded.updated_at",
                             (cid, stamp, "evaluator error", _json({"trigger": trigger}), error[:500], stamp))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
