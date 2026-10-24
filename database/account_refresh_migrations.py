"""
database/account_refresh_migrations.py - Stage 6.0.1 CONTINUOUS ACCOUNT REFRESH (additive). Idempotent; safe on any database state.
Only NEW tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 / 6 migration).

  * account_refresh_snapshot   ONE row (id = 1): the last SUCCESSFUL, validated read-only snapshot of the paper account (replaced as a
                               whole in one transaction; never partially).
  * account_refresh_attempts   append-only: every refresh attempt (trigger, result, read attempts, error code / text, snapshot id).
  * account_refresh_liveness   ONE row: last attempt / success, counts, consecutive failures, last result / error, last snapshot age.
No credential, auth header or account UUID is stored; the account number is masked.
"""
import sqlite3

TABLES = ("account_refresh_snapshot", "account_refresh_attempts", "account_refresh_liveness")

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS account_refresh_snapshot (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        refresh_id       TEXT NOT NULL,
        refreshed_at     TEXT NOT NULL,
        source           TEXT NOT NULL,
        host             TEXT NOT NULL,
        account_json     TEXT NOT NULL,
        positions_json   TEXT NOT NULL,
        open_orders_json TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS account_refresh_snapshot_no_delete BEFORE DELETE ON account_refresh_snapshot "
    "BEGIN SELECT RAISE(ABORT, 'the account snapshot row is kept'); END;",
    """
    CREATE TABLE IF NOT EXISTS account_refresh_attempts (
        attempt_id      TEXT PRIMARY KEY,
        started_at      TEXT NOT NULL,
        finished_at     TEXT NOT NULL,
        trigger         TEXT NOT NULL,
        result          TEXT NOT NULL CHECK (result IN ('SUCCESS', 'FAILED', 'INVALID')),
        read_attempts   INTEGER NOT NULL DEFAULT 1,
        error_code      TEXT,
        error_text      TEXT,
        refresh_id      TEXT,
        owner           TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_account_refresh_attempts_started ON account_refresh_attempts (started_at);",
    "CREATE TRIGGER IF NOT EXISTS account_refresh_attempts_no_update BEFORE UPDATE ON account_refresh_attempts "
    "BEGIN SELECT RAISE(ABORT, 'account refresh attempts are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS account_refresh_attempts_no_delete BEFORE DELETE ON account_refresh_attempts "
    "BEGIN SELECT RAISE(ABORT, 'account refresh attempts are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS account_refresh_attempts_no_replace BEFORE INSERT ON account_refresh_attempts "
    "WHEN EXISTS (SELECT 1 FROM account_refresh_attempts WHERE attempt_id = NEW.attempt_id) BEGIN SELECT RAISE(ABORT, 'attempts are never replaced'); END;",
    """
    CREATE TABLE IF NOT EXISTS account_refresh_liveness (
        id                    INTEGER PRIMARY KEY CHECK (id = 1),
        last_attempt_at       TEXT,
        last_success_at       TEXT,
        attempt_count         INTEGER NOT NULL DEFAULT 0,
        success_count         INTEGER NOT NULL DEFAULT 0,
        failure_count         INTEGER NOT NULL DEFAULT 0,
        consecutive_failures  INTEGER NOT NULL DEFAULT 0,
        last_result           TEXT,
        last_error            TEXT,
        last_snapshot_age_s   REAL,
        owner                 TEXT,
        updated_at            TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS account_refresh_liveness_no_delete BEFORE DELETE ON account_refresh_liveness "
    "BEGIN SELECT RAISE(ABORT, 'the account refresh liveness row is kept'); END;",
]


def run_account_refresh_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
