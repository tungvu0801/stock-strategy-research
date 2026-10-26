"""
database/strategy_validation_migrations.py - Stage 6.2 STRATEGY VALIDATION HARNESS (additive). Idempotent; safe on any database state. Only NEW
tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 / 6 migration).
  * strategy_validation_runs         one row per completed validation run: identity, strategy version, dataset, split, verdict and the full
                                     reproducible run record (config snapshot, friction model, execution assumptions, integrity, causality,
                                     metrics, diagnostics, audit) as JSON
  * strategy_validation_candidates   the immutable candidate set of the run (one row per candidate, with its identity hash)
  * strategy_validation_trades       the simulated (hypothetical) trades per arm
  * strategy_validation_evaluations  Stage 6.4.1: METHODOLOGY EVALUATIONS (validation_v3) of a stored run - a separate record that references
                                     the run; the run itself is never rewritten or reinterpreted
Rows are append-only: never updated, never deleted (triggers). A run is written once, complete. Nothing here stores a credential, an order or a
recommendation; every trade row is a research simulation.
"""
import sqlite3

TABLES = ("strategy_validation_runs", "strategy_validation_candidates", "strategy_validation_trades", "strategy_validation_evaluations")

DDL = (
    """
    CREATE TABLE IF NOT EXISTS strategy_validation_runs (
        run_id              TEXT PRIMARY KEY CHECK (length(run_id) = 32),
        strategy_id         TEXT NOT NULL,
        strategy_version    TEXT NOT NULL,
        dataset_id          TEXT NOT NULL,
        dataset_kind        TEXT NOT NULL CHECK (dataset_kind IN ('HISTORICAL', 'SYNTHETIC')),
        verdict             TEXT NOT NULL CHECK (verdict IN ('INSUFFICIENT_DATA', 'FAIL', 'MIXED', 'PASS', 'INVALID')),
        validation_version  TEXT NOT NULL,
        candidate_count     INTEGER NOT NULL,
        created_at          TEXT NOT NULL,
        run_json            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS strategy_validation_runs_by_strategy ON strategy_validation_runs (strategy_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS strategy_validation_candidates (
        run_id          TEXT NOT NULL,
        candidate_id    TEXT NOT NULL,
        signal_date     TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        candidate_json  TEXT NOT NULL,
        PRIMARY KEY (run_id, candidate_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_validation_trades (
        run_id          TEXT NOT NULL,
        trade_id        TEXT NOT NULL,
        arm             TEXT NOT NULL CHECK (arm IN ('RULES', 'GATED', 'MODEL')),
        candidate_id    TEXT NOT NULL,
        segment         TEXT NOT NULL CHECK (segment IN ('TRAIN', 'OOS')),
        trade_json      TEXT NOT NULL,
        PRIMARY KEY (run_id, trade_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_validation_evaluations (
        evaluation_id          TEXT PRIMARY KEY CHECK (length(evaluation_id) = 32),
        run_id                 TEXT NOT NULL,
        strategy_id            TEXT NOT NULL,
        strategy_version       TEXT NOT NULL,
        methodology_version    TEXT NOT NULL,
        selection_edge_status  TEXT NOT NULL CHECK (selection_edge_status IN ('NOT_APPLICABLE', 'INSUFFICIENT_DATA', 'NOT_ESTABLISHED', 'WEAK', 'ESTABLISHED', 'INVALID')),
        verdict                TEXT NOT NULL CHECK (verdict IN ('INSUFFICIENT_DATA', 'FAIL', 'MIXED', 'PASS', 'INVALID')),
        created_at             TEXT NOT NULL,
        evaluation_json        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS strategy_validation_evaluations_by_run ON strategy_validation_evaluations (run_id, created_at)",
)
for _t in TABLES:
    DDL += (f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are immutable'); END",
            f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are never deleted'); END")


def run_strategy_validation_migrations(conn: sqlite3.Connection) -> None:
    for stmt in DDL:
        conn.execute(stmt)
