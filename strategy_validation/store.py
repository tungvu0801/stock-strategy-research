"""strategy_validation/store.py — persistence of validation runs on the app's shared database (the store convention of the earlier stages: one file,
additive migrations, append-only rows). A run is written once, complete, in one transaction: the run record, its candidate set and its simulated trades.

SYNTHETIC runs (test fixtures) are refused unless the process is a test process (STOCK_AGENT_ENV test / browser_test) and are invisible to every read
outside such a process: a normal preview or app never shows a fixture result. A strategy_version is validated with ONE adapter configuration
(parameters, entry policy, holding limit, critical features): another configuration under the same version is refused - it is a new version."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from database.strategy_validation_migrations import run_strategy_validation_migrations
from strategy_validation.models import ValidationRun

TEST_ENVS = ("test", "browser_test")


class StoreError(ValueError):
    pass


def synthetic_allowed() -> bool:
    return os.environ.get("STOCK_AGENT_ENV", "") in TEST_ENVS


def default_path() -> Path:
    from database.database import get_db
    return Path(get_db().db_path)


class StrategyValidationStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()

    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        run_strategy_validation_migrations(conn)
        return conn

    def _ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _read(self, sql: str, args=()) -> List[dict]:
        conn = self._ro()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.OperationalError as exc:                         # the tables do not exist yet: nothing was ever validated
            if "no such table" in str(exc):
                return []
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- write (once, complete) ---------------------------------------------------
    def insert(self, run: ValidationRun) -> str:
        if run.dataset_kind == "SYNTHETIC" and not synthetic_allowed():
            raise StoreError("SYNTHETIC validation runs are test fixtures: refused outside a test process")
        if not run.verdict.get("verdict"):
            raise StoreError("only a complete run (with a verdict) is persisted")
        rec = json.loads(run.model_dump_json())
        cands = rec.pop("candidates")
        trades = []
        for a in rec["arms"].values():                                       # the trades get their own rows; the run keeps the count per arm
            ts = a.pop("trades")
            a["trade_count"] = len(ts)
            trades += ts
        adapter = json.dumps((rec.get("config_snapshot") or {}).get("adapter") or {}, sort_keys=True)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for (prior,) in conn.execute("SELECT run_json FROM strategy_validation_runs WHERE strategy_id = ? AND strategy_version = ?", (run.strategy_id, run.strategy_version)):
                if json.dumps((json.loads(prior).get("config_snapshot") or {}).get("adapter") or {}, sort_keys=True) != adapter:
                    raise StoreError(f"{run.strategy_id} v{run.strategy_version} was validated with other parameters / execution assumptions: a change after a "
                                     "validation is a new strategy_version (no OOS contamination)")
            conn.execute("INSERT INTO strategy_validation_runs (run_id, strategy_id, strategy_version, dataset_id, dataset_kind, verdict, validation_version, candidate_count, "
                         "created_at, run_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (run.run_id, run.strategy_id, run.strategy_version, run.dataset_id, run.dataset_kind, run.verdict["verdict"], run.validation_version, run.candidate_count,
                          run.created_at, json.dumps(rec, sort_keys=True)))
            conn.executemany("INSERT INTO strategy_validation_candidates (run_id, candidate_id, signal_date, symbol, candidate_json) VALUES (?, ?, ?, ?, ?)",
                             [(run.run_id, c["candidate_id"], c["signal_date"], c["symbol"], json.dumps(c, sort_keys=True)) for c in cands])
            conn.executemany("INSERT INTO strategy_validation_trades (run_id, trade_id, arm, candidate_id, segment, trade_json) VALUES (?, ?, ?, ?, ?, ?)",
                             [(run.run_id, t["trade_id"], t["arm"], t["candidate_id"], t["segment"], json.dumps(t, sort_keys=True)) for t in trades])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return run.run_id

    # ---------------------------------------------------------------- reads -----------------------------------------------------------------
    def _visible(self, kind: str) -> bool:
        return kind != "SYNTHETIC" or synthetic_allowed()

    def run(self, run_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM strategy_validation_runs WHERE run_id = ?", (run_id,))
        if not rows or not self._visible(rows[0]["dataset_kind"]):
            return None
        return json.loads(rows[0]["run_json"])

    def runs(self, strategy_id: Optional[str] = None, limit: int = 50) -> List[dict]:
        sql, args = "SELECT run_id, strategy_id, strategy_version, dataset_id, dataset_kind, verdict, validation_version, candidate_count, created_at FROM strategy_validation_runs", []
        if strategy_id:
            sql, args = sql + " WHERE strategy_id = ?", [strategy_id]
        rows = self._read(sql + " ORDER BY created_at DESC, run_id DESC LIMIT ?", (*args, int(limit)))
        return [r for r in rows if self._visible(r["dataset_kind"])]

    def latest(self, strategy_id: str) -> Optional[dict]:
        rows = self.runs(strategy_id, limit=20)
        return self.run(rows[0]["run_id"]) if rows else None

    def candidates(self, run_id: str) -> List[dict]:
        if self.run(run_id) is None:
            return []
        return [json.loads(r["candidate_json"]) for r in self._read("SELECT candidate_json FROM strategy_validation_candidates WHERE run_id = ? ORDER BY signal_date, symbol, candidate_id",
                                                                     (run_id,))]

    def trades(self, run_id: str, arm: Optional[str] = None) -> List[dict]:
        if self.run(run_id) is None:
            return []
        sql, args = "SELECT trade_json FROM strategy_validation_trades WHERE run_id = ?", [run_id]
        if arm:
            sql, args = sql + " AND arm = ?", [run_id, arm]
        return [json.loads(r["trade_json"]) for r in self._read(sql + " ORDER BY arm, trade_id", tuple(args))]
