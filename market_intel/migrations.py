"""
market_intel/migrations.py - Stage 7.1: the versioned schema of the market-intelligence store (its OWN SQLite file, data/market_intel.db).

* Explicit versions. Each migration is (version, name, statements). migrate() applies the pending ones in order inside one transaction,
  records each in mi_schema_migrations with the SHA-256 of its DDL, and sets PRAGMA user_version. A database whose user_version is newer
  than this code refuses to open for writing (SchemaTooNew); a recorded DDL hash that differs from this code refuses too (SchemaMismatch).
* Append-only. Every data table carries no-UPDATE / no-DELETE triggers (the evidence/store.py convention): a revision or a correction is
  a new row; history is never rewritten. A dataset's status changes are rows in mi_dataset_events, never an UPDATE.
* No instruction field. There is no order, quantity, side, signal, target or position column anywhere (tested over the schema).
* Nothing here runs at import. The file is created only by MarketIntelStore on an explicit write (store.py).
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import List, Sequence, Tuple

SCHEMA_VERSION = 1
APPEND_ONLY_TABLES = ("mi_datasets", "mi_dataset_events", "mi_fetches", "mi_observations", "mi_budget_events", "mi_quality_audits",
                      "mi_schema_migrations")


class SchemaTooNew(RuntimeError):
    """The database was written by a newer schema than this code knows."""


class SchemaMismatch(RuntimeError):
    """A recorded migration's DDL hash differs from this code's: the schema was altered outside these migrations."""


def _immutable(table: str) -> List[str]:
    return [f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{table} rows are never deleted'); END"]


_TS = "CHECK ({c} IS NULL OR ({c} GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-6][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))"


def _ts(*cols: str) -> str:
    return ", ".join(_TS.format(c=c) for c in cols)


V1: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS mi_schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, ddl_sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)""",
    # one explicit download, import or (later) daemon window = one dataset; its status history is mi_dataset_events
    f"""CREATE TABLE IF NOT EXISTS mi_datasets (
        dataset_id TEXT PRIMARY KEY, job_kind TEXT NOT NULL CHECK (job_kind IN ('BACKFILL', 'IMPORT', 'FIXTURE')),
        kind TEXT NOT NULL, provider TEXT NOT NULL, feed TEXT NOT NULL, adjustment TEXT NOT NULL, timeframe TEXT,
        symbols_json TEXT NOT NULL, start_date TEXT, end_date TEXT, request_json TEXT NOT NULL, plan_sha256 TEXT,
        approval_ref TEXT, created_at TEXT NOT NULL, code_version TEXT NOT NULL, {_ts('created_at')})""",
    f"""CREATE TABLE IF NOT EXISTS mi_dataset_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id TEXT NOT NULL REFERENCES mi_datasets(dataset_id),
        status TEXT NOT NULL CHECK (status IN ('PLANNED', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'EMPTY')),
        at TEXT NOT NULL, rows_inserted INTEGER NOT NULL DEFAULT 0, rows_duplicate INTEGER NOT NULL DEFAULT 0,
        rows_revised INTEGER NOT NULL DEFAULT 0, content_sha256 TEXT, detail TEXT NOT NULL DEFAULT '', {_ts('at')})""",
    # every provider request; the raw response body lives gzipped in the raw archive (runtime/market_intel/raw), named here
    f"""CREATE TABLE IF NOT EXISTS mi_fetches (
        fetch_id TEXT PRIMARY KEY, dataset_id TEXT REFERENCES mi_datasets(dataset_id), provider TEXT NOT NULL, endpoint TEXT NOT NULL,
        params_json TEXT NOT NULL, started_at TEXT NOT NULL, retrieved_at TEXT, status TEXT NOT NULL, http_status INTEGER,
        latency_ms INTEGER, bytes INTEGER NOT NULL DEFAULT 0, content_hash TEXT, retry_after_s REAL, error TEXT NOT NULL DEFAULT '',
        raw_path TEXT, ingested_at TEXT NOT NULL, {_ts('started_at', 'retrieved_at', 'ingested_at')})""",
    # the Stage 7.0 Observation envelope, one row per revision of one fact
    f"""CREATE TABLE IF NOT EXISTS mi_observations (
        obs_id TEXT PRIMARY KEY, natural_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1),
        supersedes_obs_id TEXT REFERENCES mi_observations(obs_id), content_hash TEXT NOT NULL,
        dataset_id TEXT NOT NULL REFERENCES mi_datasets(dataset_id), fetch_id TEXT REFERENCES mi_fetches(fetch_id),
        kind TEXT NOT NULL, symbol TEXT, series_id TEXT, provider TEXT NOT NULL, feed TEXT NOT NULL, adjustment TEXT NOT NULL,
        observed_at TEXT, published_at TEXT, retrieved_at TEXT NOT NULL, available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
        session TEXT NOT NULL, session_date TEXT, status TEXT NOT NULL, freshness TEXT, age_s REAL, source_url TEXT,
        source_identifier TEXT, tier INTEGER, quality_flags_json TEXT NOT NULL, backfilled INTEGER NOT NULL CHECK (backfilled IN (0, 1)),
        payload_json TEXT NOT NULL, envelope_version TEXT NOT NULL,
        UNIQUE (natural_key, revision),
        CHECK (available_at >= retrieved_at), CHECK (backfilled = 0 OR available_at = retrieved_at),
        {_ts('observed_at', 'published_at', 'retrieved_at', 'available_at', 'ingested_at')})""",
    f"""CREATE TABLE IF NOT EXISTS mi_budget_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, provider TEXT NOT NULL,
        event TEXT NOT NULL CHECK (event IN ('ALLOW', 'REFUSE', 'RESPONSE', 'BACKOFF', 'ACCESS_DENIED', 'PRESSURE', 'RESET')),
        reason TEXT NOT NULL DEFAULT '', http_status INTEGER, retry_after_s REAL, per_minute REAL, used_today INTEGER,
        dataset_id TEXT REFERENCES mi_datasets(dataset_id), {_ts('at')})""",
    f"""CREATE TABLE IF NOT EXISTS mi_quality_audits (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, dataset_id TEXT REFERENCES mi_datasets(dataset_id),
        scope_json TEXT NOT NULL, check_name TEXT NOT NULL, result TEXT NOT NULL CHECK (result IN ('PASS', 'WARN', 'BLOCKED')),
        codes_json TEXT NOT NULL, detail_json TEXT NOT NULL, {_ts('at')})""",
    "CREATE INDEX IF NOT EXISTS mi_obs_symbol_kind_time ON mi_observations(symbol, kind, feed, observed_at)",
    "CREATE INDEX IF NOT EXISTS mi_obs_series_time ON mi_observations(series_id, kind, observed_at)",
    "CREATE INDEX IF NOT EXISTS mi_obs_available ON mi_observations(available_at)",
    "CREATE INDEX IF NOT EXISTS mi_obs_provider_kind ON mi_observations(provider, kind)",
    "CREATE INDEX IF NOT EXISTS mi_obs_kind_time ON mi_observations(kind, observed_at)",
    "CREATE INDEX IF NOT EXISTS mi_obs_dataset ON mi_observations(dataset_id)",
    "CREATE INDEX IF NOT EXISTS mi_dataset_events_ds ON mi_dataset_events(dataset_id, event_id)",
    "CREATE INDEX IF NOT EXISTS mi_fetches_dataset ON mi_fetches(dataset_id)",
    "CREATE INDEX IF NOT EXISTS mi_budget_provider_at ON mi_budget_events(provider, at)",
    *[t for table in APPEND_ONLY_TABLES for t in _immutable(table)],
)
MIGRATIONS: Sequence[Tuple[int, str, Tuple[str, ...]]] = ((1, "initial market-intelligence store", V1),)


def ddl_sha256(statements: Sequence[str]) -> str:
    return hashlib.sha256("\n;\n".join(" ".join(s.split()) for s in statements).encode("utf-8")).hexdigest()


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def verify(conn: sqlite3.Connection) -> int:
    """Read-only check of a database's schema against this code: its version, and every recorded DDL hash. Returns the version."""
    v = current_version(conn)
    if v > SCHEMA_VERSION:
        raise SchemaTooNew(f"market_intel.db is schema version {v}; this code knows up to {SCHEMA_VERSION}")
    if v:
        recorded = {r[0]: r[1] for r in conn.execute("SELECT version, ddl_sha256 FROM mi_schema_migrations")}
        for version, _name, stmts in MIGRATIONS:
            if version <= v and recorded.get(version) != ddl_sha256(stmts):
                raise SchemaMismatch(f"migration {version} on disk does not match this code")
    return v


def migrate(conn: sqlite3.Connection, now: datetime) -> int:
    """Apply every pending migration in ONE transaction (the caller's connection must not be inside another). Returns the version."""
    v = verify(conn)
    pending = [m for m in MIGRATIONS if m[0] > v]
    if not pending:
        return v
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for version, name, stmts in pending:
            for st in stmts:
                conn.execute(st)
            conn.execute("INSERT INTO mi_schema_migrations (version, name, ddl_sha256, applied_at) VALUES (?, ?, ?, ?)",
                         (version, name, ddl_sha256(stmts), stamp))
            conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return pending[-1][0]
