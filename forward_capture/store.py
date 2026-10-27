"""
forward_capture/store.py - the Stage 6.10A forward-evidence tables (database/forward_capture_migrations.py) on the shared database.

  writes   only the forward-validation observer thread writes (forward_capture/capture.py). Every evidence row is 'insert if absent'
           (INSERT ... SELECT ... WHERE NOT EXISTS - never INSERT OR IGNORE, which the append-only triggers turn into an abort). The same id
           stored again with a DIFFERENT content hash is a CONFLICT fault, never an overwrite. Short transactions and a 1 s busy timeout: a
           locked database delays evidence, never an order.
  reads    read-only connections (mode=ro): no migration, no DDL, nothing is created by a read; a missing table is an empty result.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database.forward_capture_migrations import run_forward_capture_migrations

BUSY_TIMEOUT_S = 1.0
INSERTED, EXISTS, CONFLICT = "INSERTED", "EXISTS", "CONFLICT"
# table -> (id column, content-hash column or None: an id derived from the content needs no hash)
TABLE_KEYS = {"forward_capture_cohorts": ("cohort_id", None), "forward_capture_config_snapshots": ("snapshot_id", None),
              "forward_capture_candidates": ("candidate_id", None), "forward_capture_decisions": ("decision_id", "content_sha256"),
              "forward_capture_entry_snapshots": ("execution_id", "snapshot_sha256"), "forward_capture_outcomes": ("position_id", "outcome_sha256"),
              "forward_capture_context": ("context_id", "content_sha256"), "forward_capture_faults": ("fault_id", None)}
JSON_COLS = ("reason_codes_json", "features_json", "proposal_json", "account_json", "exposure_json", "controls_json", "detail_json", "snapshot_json",
             "outcome_json", "context_json", "config_json", "definition_json", "reasons_json", "metrics_json", "cohort_ids_json")
HEALTH_COLS = ("trading_date", "status", "final", "reasons_json", "metrics_json", "summary_text", "cohort_ids_json", "source_session_status", "computed_at", "version")

_MIGRATED: set = set()
_MIGRATE_LOCK = threading.Lock()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def fid(*parts: Any) -> str:
    """A stable 32-hex id from its parts (the same logical row always gets the same id)."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def default_path() -> Path:
    from fit import readonly as RO
    return RO.db_path()


def decode(row: dict) -> dict:
    """A stored row with its *_json columns parsed (key without the _json suffix)."""
    out = dict(row)
    for c in JSON_COLS:
        if c in out:
            out[c[:-5]] = _loads(out.pop(c), None)
    return out


class ForwardCaptureStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()

    def _key(self) -> str:
        return str(self.path.resolve())

    # ---------------------------------------------------------------- connections -------------------------------------------
    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        key = self._key()
        if key not in _MIGRATED:                                  # migrations once per process and database file (the observer writes often)
            with _MIGRATE_LOCK:
                if key not in _MIGRATED:
                    run_forward_capture_migrations(conn)
                    _MIGRATED.add(key)
        return conn

    def query(self, sql: str, args=()) -> List[dict]:
        """Read-only SQL over the shared database; a missing table or column (nothing captured yet) is an empty result."""
        if not self.path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S)
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
        for attempt in (0, 1):
            conn = self._rw()
            try:
                conn.execute("BEGIN IMMEDIATE")
                out = fn(conn)
                conn.execute("COMMIT")
                return out
            except sqlite3.OperationalError as exc:
                _rollback(conn)
                if attempt == 0 and "no such table" in str(exc):      # the file was replaced since this process migrated it
                    _MIGRATED.discard(self._key())
                    continue
                raise
            except Exception:
                _rollback(conn)
                raise
            finally:
                conn.close()

    def ensure(self) -> None:
        self._rw().close()

    def exists(self) -> bool:
        return bool(self.query("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forward_capture_decisions'"))

    # ---------------------------------------------------------------- writes (observer thread only) --------------------------
    def put(self, rows: Iterable[Tuple[str, dict]], now: datetime, *, state: Optional[Dict[str, Any]] = None, faults: Iterable[dict] = ()) -> Dict[str, Any]:
        """ONE transaction: every (table, row) inserted if absent, the given state keys set, and the given faults added. A same-id row with a
        different content hash is reported (and recorded) as a CONFLICT fault; the stored row is never changed."""
        rows, faults = list(rows), list(faults)

        def fn(conn):
            res = {"inserted": 0, "existing": 0, "conflicts": []}
            for table, row in rows:
                r = _insert(conn, table, row)
                if r == INSERTED:
                    res["inserted"] += 1
                elif r == EXISTS:
                    res["existing"] += 1
                else:
                    key = TABLE_KEYS[table][0]
                    res["conflicts"].append({"table": table, "id": row[key]})
            for c in res["conflicts"]:
                _fault(conn, now, {"component": "store", "code": "CONFLICT", "key": f"{c['table']}|{c['id']}", "detail": c})
            for f in faults:
                _fault(conn, now, f)
            for k, v in (state or {}).items():
                _set_state(conn, k, v, now)
            return res
        return self._write(fn)

    def put_cohort(self, cohort_id: str, cohort_key: str, started_at: str, trading_date: Optional[str], definition: dict) -> dict:
        """The cohort row (insert if absent). Its label FP_<year>_<nn> is assigned inside the same transaction: the cohort's sequence in its year."""
        def fn(conn):
            r = conn.execute("SELECT cohort_id, cohort_key, label, started_at FROM forward_capture_cohorts WHERE cohort_id = ?", (cohort_id,)).fetchone()
            if r is not None:
                return dict(r)
            year = str(trading_date or started_at)[:4]
            n = conn.execute("SELECT count(*) FROM forward_capture_cohorts WHERE label LIKE ?", (f"FP_{year}_%",)).fetchone()[0]
            row = {"cohort_id": cohort_id, "cohort_key": cohort_key, "label": f"FP_{year}_{int(n) + 1:02d}", "started_at": started_at, "definition_json": dumps(definition)}
            _insert(conn, "forward_capture_cohorts", row)
            return {k: row[k] for k in ("cohort_id", "cohort_key", "label", "started_at")}
        return self._write(fn)

    def add_faults(self, now: datetime, faults: Iterable[dict]) -> None:
        faults = list(faults)
        if faults:
            self._write(lambda conn: [_fault(conn, now, f) for f in faults])

    def init_state(self, key: str, value: Any, now: datetime) -> Any:
        """Set `key` once (the capture epoch); returns the value stored first."""
        def fn(conn):
            conn.execute("INSERT INTO forward_capture_state (key, value, updated_at) SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM forward_capture_state WHERE key = ?)",
                         (key, dumps(value), _iso(now), key))
            r = conn.execute("SELECT value FROM forward_capture_state WHERE key = ?", (key,)).fetchone()
            return _loads(r["value"], None) if r else None
        return self._write(fn)

    def put_health(self, row: dict) -> bool:
        """Upsert the trading date's health record while it is open; a FINAL record is never changed (returns False then)."""
        vals = {c: row.get(c) for c in HEALTH_COLS}
        for c in ("reasons_json", "metrics_json", "cohort_ids_json"):
            if not isinstance(vals[c], str):
                vals[c] = dumps(vals[c] if vals[c] is not None else ([] if c != "metrics_json" else {}))
        vals["final"] = 1 if vals.get("final") else 0
        sets = ", ".join(f"{c} = excluded.{c}" for c in HEALTH_COLS if c != "trading_date")

        def fn(conn):
            cur = conn.execute(f"INSERT INTO forward_capture_session_health ({', '.join(HEALTH_COLS)}) VALUES ({', '.join('?' * len(HEALTH_COLS))}) "
                               f"ON CONFLICT(trading_date) DO UPDATE SET {sets} WHERE forward_capture_session_health.final = 0", [vals[c] for c in HEALTH_COLS])
            return cur.rowcount == 1
        return bool(self._write(fn))

    # ---------------------------------------------------------------- reads (read only) ---------------------------------------
    def state(self) -> Dict[str, Any]:
        return {r["key"]: _loads(r["value"], None) for r in self.query("SELECT key, value FROM forward_capture_state")}

    def health(self, trading_date: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM forward_capture_session_health WHERE trading_date = ?", (trading_date,))
        return decode(rows[0]) if rows else None

    def healths(self, limit: int = 60) -> List[dict]:
        return [decode(r) for r in self.query("SELECT * FROM forward_capture_session_health ORDER BY trading_date DESC LIMIT ?", (limit,))]

    def cohorts(self) -> List[dict]:
        return [decode(r) for r in self.query("SELECT * FROM forward_capture_cohorts ORDER BY started_at, cohort_id")]

    def cohort(self, cohort_id: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM forward_capture_cohorts WHERE cohort_id = ?", (cohort_id,))
        return decode(rows[0]) if rows else None

    def config_snapshots(self, limit: int = 20, trading_date: Optional[str] = None) -> List[dict]:
        if trading_date:
            rows = self.query("SELECT * FROM forward_capture_config_snapshots WHERE trading_date = ? ORDER BY at DESC, rowid DESC LIMIT ?", (trading_date, limit))
        else:
            rows = self.query("SELECT * FROM forward_capture_config_snapshots ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        return [decode(r) for r in rows]

    def decisions(self, trading_date: Optional[str] = None, limit: int = 500, candidate_id: Optional[str] = None) -> List[dict]:
        where, args = [], []
        if trading_date:
            where.append("trading_date = ?")
            args.append(trading_date)
        if candidate_id:
            where.append("candidate_id = ?")
            args.append(candidate_id)
        sql = "SELECT * FROM forward_capture_decisions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY decided_at, decision_id LIMIT ?"
        return [decode(r) for r in self.query(sql, (*args, limit))]

    def decision_counts(self, trading_date: str) -> Dict[str, int]:
        rows = self.query("SELECT decision, count(*) AS n FROM forward_capture_decisions WHERE trading_date = ? GROUP BY decision", (trading_date,))
        return {r["decision"]: int(r["n"]) for r in rows}

    def candidates(self, limit: int = 500, since: Optional[str] = None) -> List[dict]:
        if since:
            return self.query("SELECT * FROM forward_capture_candidates WHERE first_seen_at >= ? ORDER BY first_seen_at, candidate_id LIMIT ?", (since, limit))
        return self.query("SELECT * FROM forward_capture_candidates ORDER BY first_seen_at, candidate_id LIMIT ?", (limit,))

    def entry_snapshot(self, execution_id: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM forward_capture_entry_snapshots WHERE execution_id = ?", (execution_id,))
        return decode(rows[0]) if rows else None

    def outcome(self, position_id: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM forward_capture_outcomes WHERE position_id = ?", (position_id,))
        return decode(rows[0]) if rows else None

    def faults(self, trading_date: Optional[str] = None, limit: int = 200) -> List[dict]:
        if trading_date:
            rows = self.query("SELECT * FROM forward_capture_faults WHERE trading_date = ? ORDER BY at, fault_id LIMIT ?", (trading_date, limit))
        else:
            rows = self.query("SELECT * FROM forward_capture_faults ORDER BY at DESC, fault_id LIMIT ?", (limit,))
        return [decode(r) for r in rows]

    def last_context(self) -> Optional[dict]:
        rows = self.query("SELECT * FROM forward_capture_context ORDER BY at DESC, rowid DESC LIMIT 1")
        return decode(rows[0]) if rows else None

    def context_before(self, at: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM forward_capture_context WHERE at <= ? ORDER BY at DESC, rowid DESC LIMIT 1", (at,))
        return decode(rows[0]) if rows else None

    def counts(self) -> Dict[str, int]:
        from database.forward_capture_migrations import TABLES
        out = {}
        for t in TABLES:
            rows = self.query(f"SELECT count(*) AS n FROM {t}")
            out[t] = int(rows[0]["n"]) if rows else 0
        return out


# ---------------------------------------------------------------- statement helpers (inside one write transaction) -------------------
def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> str:
    key, sha_col = TABLE_KEYS[table]
    cols = list(row)
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join('?' * len(cols))} WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {key} = ?)",
                       [*(row[c] for c in cols), row[key]])
    if cur.rowcount == 1:
        return INSERTED
    if sha_col is None:
        return EXISTS
    old = conn.execute(f"SELECT {sha_col} FROM {table} WHERE {key} = ?", (row[key],)).fetchone()
    return EXISTS if old is not None and old[0] == row.get(sha_col) else CONFLICT


def _fault(conn: sqlite3.Connection, now: datetime, f: dict) -> None:
    """A recording fault: an evidence gap is a row, never hidden. A keyed fault is recorded once (the same gap seen on every tick)."""
    key = f.get("key")
    row = {"fault_id": fid("fault", f.get("component"), f.get("code"), key) if key else uuid.uuid4().hex, "at": f.get("at") or _iso(now),
           "trading_date": f.get("trading_date"), "component": str(f.get("component") or "capture"), "code": str(f.get("code") or "FAULT"),
           "detail_json": dumps(f.get("detail") or {})}
    _insert(conn, "forward_capture_faults", row)


def _set_state(conn: sqlite3.Connection, key: str, value: Any, now: datetime) -> None:
    conn.execute("INSERT INTO forward_capture_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                 "updated_at = excluded.updated_at", (key, dumps(value), _iso(now)))
