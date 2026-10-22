"""
setups/store.py — persistence for Stage 5.8 (DESIGN_58 §8). One sqlite transaction per evaluation: episode insert / update, transition
row, alert row (cooldown-aware) and evaluation row together — nothing is reported as persisted unless the commit succeeded. Reads open
the database read-only and tolerate a database without the tables (an untouched database means "no setups"). No row is ever deleted.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from database.setup_state_migrations import run_setup_state_migrations
from fit import readonly as RO
from setups import ALERT_COOLDOWN_MINUTES, CONTROLS, SETUP_TYPE, STATE_LABELS
from setups.models import Decision, EpisodeRecord, SetupAlert, SetupInputs

EPISODE_COLS = ("setup_id", "symbol", "setup_type", "cycle_id", "state", "previous_state", "started_at", "last_transition_at", "last_evaluated_at",
                "closed_at", "invalidation_reason", "transition_reason", "demand_low_streak", "evidence_packet_id_at_start")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def new_id() -> str:
    return uuid.uuid4().hex


def setup_id_for(symbol: str, setup_type: str, started_at: str, cycle_id: int) -> str:
    """Deterministic episode identity: the same symbol / type / start time / cycle always maps to the same id."""
    return hashlib.sha256(f"{symbol}|{setup_type}|{started_at}|{int(cycle_id)}".encode("utf-8")).hexdigest()[:32]


class SetupStoreError(RuntimeError):
    pass


class SetupStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---------------------------------------------------------------- connections -------------------------------------------
    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        run_setup_state_migrations(conn)
        return conn

    def _ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'setup_%'")}
        except sqlite3.Error:
            conn.close()
            return None
        if "setup_episodes" not in names:
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
    def _episode(row: dict) -> EpisodeRecord:
        return EpisodeRecord(**{k: row[k] for k in EPISODE_COLS})

    def open_episode(self, symbol: str, setup_type: str = SETUP_TYPE) -> Optional[EpisodeRecord]:
        rows = self._read("SELECT * FROM setup_episodes WHERE symbol = ? AND setup_type = ? AND closed_at IS NULL", (symbol.upper(), setup_type))
        return self._episode(rows[0]) if rows else None

    def open_episodes(self) -> List[EpisodeRecord]:
        return [self._episode(r) for r in self._read("SELECT * FROM setup_episodes WHERE closed_at IS NULL ORDER BY symbol")]

    def episodes(self, symbol: Optional[str] = None, limit: int = 50) -> List[dict]:
        if symbol:
            return self._read("SELECT * FROM setup_episodes WHERE symbol = ? ORDER BY started_at DESC LIMIT ?", (symbol.upper(), limit))
        return self._read("SELECT * FROM setup_episodes ORDER BY last_transition_at DESC LIMIT ?", (limit,))

    def cycle_count(self, symbol: str, setup_type: str = SETUP_TYPE) -> int:
        rows = self._read("SELECT count(*) AS n FROM setup_episodes WHERE symbol = ? AND setup_type = ?", (symbol.upper(), setup_type))
        return int(rows[0]["n"]) if rows else 0

    def last_evaluation(self, symbol: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM setup_evaluations WHERE symbol = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1", (symbol.upper(),))
        return rows[0] if rows else None

    def evaluations(self, symbol: Optional[str] = None, limit: int = 50) -> List[dict]:
        if symbol:
            return self._read("SELECT * FROM setup_evaluations WHERE symbol = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (symbol.upper(), limit))
        return self._read("SELECT * FROM setup_evaluations ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (limit,))

    def transitions(self, symbol: Optional[str] = None, limit: int = 50) -> List[dict]:
        if symbol:
            return self._read("SELECT * FROM setup_transitions WHERE symbol = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (symbol.upper(), limit))
        return self._read("SELECT * FROM setup_transitions ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (limit,))

    def alerts(self, unread_only: bool = False, limit: int = 30) -> List[dict]:
        where = "WHERE read_at IS NULL " if unread_only else ""
        rows = self._read(f"SELECT * FROM setup_alerts {where}ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["to_state_label"] = STATE_LABELS.get(r["to_state"], r["to_state"])
        return rows

    def alert(self, alert_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM setup_alerts WHERE alert_id = ?", (alert_id,))
        return rows[0] if rows else None

    def unread(self) -> int:
        rows = self._read("SELECT count(*) AS n FROM setup_alerts WHERE read_at IS NULL")
        return int(rows[0]["n"]) if rows else 0

    def controls(self) -> List[dict]:
        return self._read("SELECT * FROM setup_controls ORDER BY name")

    def recent_alert(self, cooldown_key: str, now: datetime, minutes: int = ALERT_COOLDOWN_MINUTES) -> Optional[dict]:
        since = _iso(now - timedelta(minutes=minutes))
        rows = self._read("SELECT * FROM setup_alerts WHERE cooldown_key = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1", (cooldown_key, since))
        return rows[0] if rows else None

    # ---------------------------------------------------------------- writes ---------------------------------------------------
    def mark_read(self, alert_id: Optional[str], now: Optional[datetime] = None) -> int:
        conn = self._rw()
        try:
            stamp = _iso(now or datetime.now(timezone.utc))
            conn.execute("BEGIN IMMEDIATE")
            if alert_id:
                cur = conn.execute("UPDATE setup_alerts SET read_at = ? WHERE alert_id = ? AND read_at IS NULL", (stamp, alert_id))
            else:
                cur = conn.execute("UPDATE setup_alerts SET read_at = ? WHERE read_at IS NULL", (stamp,))
            conn.execute("COMMIT")
            return cur.rowcount
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _touch(conn: sqlite3.Connection, name: str, now: str, *, result: Optional[str] = None, error: Optional[str] = None, run: bool = False,
               evaluated: bool = False, transition: bool = False) -> None:
        if name not in CONTROLS:
            raise ValueError(f"unknown control {name!r}")
        conn.execute("INSERT INTO setup_controls (name, updated_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING", (name, now))
        conn.execute("UPDATE setup_controls SET updated_at = ?, last_run_at = CASE WHEN ? THEN ? ELSE last_run_at END, "
                     "last_evaluated_at = CASE WHEN ? THEN ? ELSE last_evaluated_at END, last_transition_at = CASE WHEN ? THEN ? ELSE last_transition_at END, "
                     "run_count = run_count + ?, evaluation_count = evaluation_count + ?, transition_count = transition_count + ?, error_count = error_count + ?, "
                     "last_result = COALESCE(?, last_result), last_error = CASE WHEN ? THEN ? ELSE last_error END WHERE name = ?",
                     (now, run, now, evaluated, now, transition, now, int(run), int(evaluated), int(transition), int(error is not None), result,
                      error is not None or result is not None, error, name))

    def touch_control(self, name: str, now: datetime, **kw) -> None:
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._touch(conn, name, _iso(now), **kw)
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_error(self, symbol: str, now: datetime, trigger: str, owner: str, error: str, prior_state: str = "NONE", setup_id: Optional[str] = None) -> str:
        """An evaluation that raised: recorded as ERROR (state unchanged), the evaluator control's last_error set."""
        conn = self._rw()
        stamp = _iso(now)
        eid = new_id()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO setup_evaluations (evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, "
                         "reason_text, inputs_json, freshness_json, source, mode, trigger, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (eid, stamp, symbol.upper(), SETUP_TYPE, setup_id, prior_state, prior_state, "ERROR", "EVALUATION_ERROR", error[:400], "{}", "{}", "", "", trigger, owner))
            self._touch(conn, "setup_state_evaluator", stamp, result="ERROR", error=error[:200], evaluated=True)
            conn.execute("COMMIT")
            return eid
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record(self, *, inputs: SetupInputs, freshness: dict, decision: Decision, prev: Optional[EpisodeRecord], now: datetime, trigger: str, owner: str,
               alert_text: Callable[[Decision, SetupInputs], Dict[str, str]], cooldown_minutes: int = ALERT_COOLDOWN_MINUTES) -> dict:
        """Persist one evaluation in ONE transaction. Returns {episode, transition_id, alert, alert_suppressed, evaluation_id}."""
        symbol = inputs.symbol.upper()
        stamp = _iso(now)
        inputs_json, fresh_json = _json(inputs.model_dump()), _json(freshness)
        conn = self._rw()
        episode: Optional[EpisodeRecord] = prev
        transition_id: Optional[str] = None
        alert_row: Optional[SetupAlert] = None
        suppressed: Optional[str] = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            if decision.decision == "TRANSITION":
                if decision.episode == "NEW":
                    cycle = self.cycle_count_in(conn, symbol) + 1
                    sid = setup_id_for(symbol, SETUP_TYPE, stamp, cycle)
                    conn.execute("INSERT INTO setup_episodes (setup_id, symbol, setup_type, cycle_id, state, previous_state, started_at, last_transition_at, last_evaluated_at, "
                                 "closed_at, invalidation_reason, transition_reason, demand_low_streak, evidence_packet_id_at_start, last_inputs_json, last_freshness_json, "
                                 "source, mode, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, symbol, SETUP_TYPE, cycle, decision.to_state, decision.from_state, stamp, stamp, stamp, None, None, decision.reason_code,
                                  decision.demand_low_streak, inputs.evidence_packet_id, inputs_json, fresh_json, inputs.market_data_source, inputs.mode, stamp))
                    episode = EpisodeRecord(setup_id=sid, symbol=symbol, cycle_id=cycle, state=decision.to_state, previous_state=decision.from_state, started_at=stamp,
                                            last_transition_at=stamp, last_evaluated_at=stamp, transition_reason=decision.reason_code,
                                            demand_low_streak=decision.demand_low_streak, evidence_packet_id_at_start=inputs.evidence_packet_id)
                else:
                    if prev is None:
                        raise SetupStoreError("a transition from an active state needs an open episode")
                    closed = stamp if decision.episode == "CLOSE" else None
                    inval = decision.reason_code if decision.to_state == "INVALIDATED" else prev.invalidation_reason
                    conn.execute("UPDATE setup_episodes SET state = ?, previous_state = ?, last_transition_at = ?, last_evaluated_at = ?, closed_at = ?, invalidation_reason = ?, "
                                 "transition_reason = ?, demand_low_streak = ?, last_inputs_json = ?, last_freshness_json = ?, source = ?, mode = ?, updated_at = ? WHERE setup_id = ?",
                                 (decision.to_state, decision.from_state, stamp, stamp, closed, inval, decision.reason_code, decision.demand_low_streak, inputs_json, fresh_json,
                                  inputs.market_data_source, inputs.mode, stamp, prev.setup_id))
                    episode = prev.model_copy(update={"state": decision.to_state, "previous_state": decision.from_state, "last_transition_at": stamp, "last_evaluated_at": stamp,
                                                      "closed_at": closed, "invalidation_reason": inval, "transition_reason": decision.reason_code,
                                                      "demand_low_streak": decision.demand_low_streak})
                transition_id = new_id()
                from setups.engine import alerts_for
                aid: Optional[str] = None
                text: Dict[str, str] = {}
                key = f"{episode.setup_id}|{decision.to_state}|{decision.reason_code}"        # cooldown is per episode: a new episode is a new transition and always notifies
                if alerts_for(decision.from_state, decision.to_state):
                    since = _iso(now - timedelta(minutes=cooldown_minutes))
                    hit = conn.execute("SELECT alert_id, created_at FROM setup_alerts WHERE cooldown_key = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                                       (key, since)).fetchone()
                    if hit is not None:
                        suppressed = f"COOLDOWN:{hit['alert_id']}"
                    else:
                        aid, text = new_id(), alert_text(decision, inputs)
                # the transition row first (the alert references it); ids are generated up front because transitions are immutable
                conn.execute("INSERT INTO setup_transitions (transition_id, setup_id, symbol, setup_type, from_state, to_state, reason_code, reason_text, evaluated_at, inputs_json, "
                             "freshness_json, source, mode, trigger, alert_id, alert_suppressed_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (transition_id, episode.setup_id, symbol, SETUP_TYPE, decision.from_state, decision.to_state, decision.reason_code, decision.reason_text, stamp,
                              inputs_json, fresh_json, inputs.market_data_source, inputs.mode, trigger, aid, suppressed, stamp))
                if aid is not None:
                    conn.execute("INSERT INTO setup_alerts (alert_id, transition_id, setup_id, symbol, setup_type, from_state, to_state, reason_code, title, body, cooldown_key, "
                                 "created_at, read_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                                 (aid, transition_id, episode.setup_id, symbol, SETUP_TYPE, decision.from_state, decision.to_state, decision.reason_code, text["title"], text["body"], key, stamp))
                    alert_row = SetupAlert(alert_id=aid, transition_id=transition_id, setup_id=episode.setup_id, symbol=symbol, from_state=decision.from_state,
                                           to_state=decision.to_state, to_state_label=STATE_LABELS[decision.to_state], reason_code=decision.reason_code,
                                           title=text["title"], body=text["body"], created_at=stamp)
                    self._touch(conn, "alert_persistor", stamp, result="ALERT_STORED", run=True)
            elif prev is not None:
                streak = decision.demand_low_streak if decision.decision == "HOLD" else prev.demand_low_streak
                conn.execute("UPDATE setup_episodes SET last_evaluated_at = ?, demand_low_streak = ?, last_inputs_json = ?, last_freshness_json = ?, updated_at = ? WHERE setup_id = ?",
                             (stamp, streak, inputs_json, fresh_json, stamp, prev.setup_id))
                episode = prev.model_copy(update={"last_evaluated_at": stamp, "demand_low_streak": streak})
            eid = new_id()
            conn.execute("INSERT INTO setup_evaluations (evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, reason_text, "
                         "inputs_json, freshness_json, source, mode, trigger, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (eid, stamp, symbol, SETUP_TYPE, episode.setup_id if episode else None, decision.from_state, decision.to_state, decision.decision, decision.reason_code,
                          decision.reason_text, inputs_json, fresh_json, inputs.market_data_source, inputs.mode, trigger, owner))
            self._touch(conn, "setup_state_evaluator", stamp, result=decision.decision, evaluated=True, transition=decision.decision == "TRANSITION")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {"episode": episode, "transition_id": transition_id, "alert": alert_row, "alert_suppressed": suppressed, "evaluation_id": eid}

    @staticmethod
    def cycle_count_in(conn: sqlite3.Connection, symbol: str) -> int:
        return int(conn.execute("SELECT count(*) FROM setup_episodes WHERE symbol = ? AND setup_type = ?", (symbol, SETUP_TYPE)).fetchone()[0])
