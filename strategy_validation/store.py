"""strategy_validation/store.py — persistence of validation runs on the app's shared database (the store convention of the earlier stages: one file,
additive migrations, append-only rows). A run is written once, complete, in one transaction: the run record, its candidate set and its simulated trades.

SYNTHETIC runs (test fixtures) are refused unless the process is a test process (STOCK_AGENT_ENV test / browser_test) and are invisible to every read
outside such a process: a normal preview or app never shows a fixture result. A strategy_version is validated with ONE adapter configuration
(parameters, entry policy, holding limit, critical features): another configuration under the same version is refused - it is a new version.

Stage 6.3.1: the store re-evaluates the PERSISTENCE INTEGRITY GATE (strategy_validation/integrity.py) from the run record itself and writes only an
ELIGIBLE run - a BLOCKED run raises PersistenceBlocked with its reasons (it may still be used ephemerally). The gate's result is stored with the
run. Stage 6.2 records (validation_v1) stay readable as they are: nothing is back-filled or rewritten."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from database.strategy_validation_migrations import run_strategy_validation_migrations
from strategy_validation import integrity as IG
from strategy_validation.models import ValidationRun

TEST_ENVS = ("test", "browser_test")


class StoreError(ValueError):
    pass


class PersistenceBlocked(StoreError):
    """The integrity gate blocked the run: it is not written. `eligibility` carries every check and reason."""

    def __init__(self, eligibility: "IG.PersistenceEligibility"):
        self.eligibility = eligibility
        super().__init__("persistence BLOCKED by the integrity gate: " + "; ".join(f"{r['code']}: {r['text']}" for r in eligibility.reasons))


STRATEGY_KEYS = ("strategy_id", "strategy_version", "source", "entry_policy", "moc_declared", "signal_basis", "max_hold_bars", "critical_features", "params")


def _strategy_config(rec: dict) -> str:
    """The strategy-defining part of the adapter description (the gate declarations are not strategy rules)."""
    a = (rec.get("config_snapshot") or {}).get("adapter") or {}
    return json.dumps({k: a.get(k) for k in STRATEGY_KEYS}, sort_keys=True)


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
        eligibility = IG.evaluate(rec)                                          # recomputed here: a run's own `eligibility` field is never trusted
        if not eligibility.eligible:
            raise PersistenceBlocked(eligibility)
        rec["eligibility"] = eligibility.model_dump()
        cands = rec.pop("candidates")
        trades = []
        for a in rec["arms"].values():                                       # the trades get their own rows; the run keeps the count per arm
            ts = a.pop("trades")
            a["trade_count"] = len(ts)
            trades += ts
        adapter = _strategy_config(rec)
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for (prior,) in conn.execute("SELECT run_json FROM strategy_validation_runs WHERE strategy_id = ? AND strategy_version = ?", (run.strategy_id, run.strategy_version)):
                prior_rec = json.loads(prior)
                if _strategy_config(prior_rec) != adapter:
                    raise StoreError(f"{run.strategy_id} v{run.strategy_version} was validated with other parameters / execution assumptions: a change after a "
                                     "validation is a new strategy_version (no OOS contamination)")
                if prior_rec.get("evaluation_mode") == "CROSS_SECTIONAL_PORTFOLIO" and run.evaluation_mode != "CROSS_SECTIONAL_PORTFOLIO":   # Stage 6.4.1
                    raise StoreError(f"{run.strategy_id} v{run.strategy_version} was validated as a cross-sectional portfolio: its evaluation mode cannot change")
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

    # ---------------------------------------------------------------- methodology evaluations (Stage 6.4.1) ----------------------------------
    def insert_evaluation(self, ev: dict) -> str:
        """Append a validation_v3 methodology evaluation of a stored run. The run is never touched; the evaluation is re-checked here."""
        from strategy_validation import VERSION
        from strategy_validation import cross_sectional as CS
        run = self.run(ev.get("run_id") or "")
        problems = []
        if run is None:
            problems.append("no visible stored run with this run_id")
        else:
            for k in ("strategy_id", "strategy_version", "dataset_id", "candidate_set_hash"):
                if ev.get(k) != run.get(k):
                    problems.append(f"{k} differs from the run's")
            if run.get("validation_version") == VERSION or ev.get("evaluated_run_validation_version") != run.get("validation_version"):
                problems.append(f"a {VERSION} run carries its own assessment; an evaluation is only for a run of an older methodology")
            if ev.get("performance_verdict") != run.get("verdict"):
                problems.append("the performance verdict is not the run's recorded verdict")
            ids = sorted(c["candidate_id"] for c in self.candidates(run["run_id"]))
            if hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:32] != run.get("candidate_set_hash"):
                problems.append("the run's stored candidates do not match its candidate set hash")
        if ev.get("methodology_version") != VERSION or ev.get("evaluation_mode") != CS.CROSS_SECTIONAL or not ev.get("candidate_set_reproduced"):
            problems.append("not a validation_v3 cross-sectional evaluation with a reproduced candidate set")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not isinstance(ev.get("created_at"), str) or ev["created_at"] > now:
            problems.append("created_at is missing or in the future")
        if run is not None and not problems:                                  # nothing recorded is trusted: re-derived from the run's own trades / candidates
            problems += CS.verify(ev.get("cross_sectional") or {}, ev.get("selection_edge") or {}, ev.get("performance_verdict") or {}, ev.get("verdict") or {},
                                  trades=self.trades(run["run_id"], "RULES"), candidates=self.candidates(run["run_id"]))
        if problems:
            raise StoreError("methodology evaluation refused: " + "; ".join(problems))
        conn = self._rw()
        try:
            conn.execute("INSERT INTO strategy_validation_evaluations (evaluation_id, run_id, strategy_id, strategy_version, methodology_version, selection_edge_status, "
                         "verdict, created_at, evaluation_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (ev["evaluation_id"], ev["run_id"], ev["strategy_id"], ev["strategy_version"], ev["methodology_version"], ev["selection_edge"]["status"],
                          ev["verdict"]["verdict"], ev["created_at"], json.dumps(ev, sort_keys=True)))
        finally:
            conn.close()
        return ev["evaluation_id"]

    def evaluations(self, run_id: str) -> List[dict]:
        if self.run(run_id) is None:
            return []
        return [json.loads(r["evaluation_json"]) for r in self._read("SELECT evaluation_json FROM strategy_validation_evaluations WHERE run_id = ? "
                                                                      "ORDER BY rowid DESC", (run_id,))]   # insertion order: never a caller's timestamp

    def latest_evaluation(self, run_id: str) -> Optional[dict]:
        evs = self.evaluations(run_id)
        return evs[0] if evs else None

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
        """The latest stored run as a READ VIEW: its record as stored, plus `methodology_evaluation` - its latest validation_v3 methodology
        evaluation (Stage 6.4.1) or None. The stored run JSON itself is never modified."""
        rows = self.runs(strategy_id, limit=20)
        if not rows:
            return None
        run = self.run(rows[0]["run_id"])
        return None if run is None else {**run, "methodology_evaluation": self.latest_evaluation(run["run_id"])}

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
