"""
database/cash_flow_migrations.py - Stage 6.0.2 CASH-FLOW-ADJUSTED DAILY P&L (additive). Idempotent; safe on any database state. Only NEW
tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 / 6 migration).

  * cash_flow_events             append-only ledger of normalized non-trade account activities: one row per broker activity id and
                                 revision (a broker-side change is a NEW revision row; history is never updated, deleted or replaced).
  * cash_flow_refresh_attempts   append-only: every activity read (trigger, result, pages, rows, new / revised events, completeness, error).
  * cash_flow_liveness           ONE row: last attempt / success, counts, consecutive failures, event count, last result / error / event.
No credential, auth header or account identifier is stored; broker free text is redacted.
"""
import sqlite3

TABLES = ("cash_flow_events", "cash_flow_refresh_attempts", "cash_flow_liveness")

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS cash_flow_events (
        event_id           TEXT PRIMARY KEY,
        broker_event_id    TEXT NOT NULL,
        revision           INTEGER NOT NULL DEFAULT 1,
        activity_type      TEXT NOT NULL,
        activity_sub_type  TEXT,
        category           TEXT NOT NULL CHECK (category IN ('DEPOSIT', 'WITHDRAWAL', 'JOURNAL_IN', 'JOURNAL_OUT', 'FEE', 'DIVIDEND', 'INTEREST', 'OTHER', 'UNCLASSIFIED')),
        external           INTEGER NOT NULL CHECK (external IN (0, 1)),
        direction          TEXT NOT NULL CHECK (direction IN ('IN', 'OUT', 'NONE')),
        amount             TEXT,
        net_amount         TEXT,
        currency           TEXT,
        broker_status      TEXT,
        business_date      TEXT,
        effective_at       TEXT,
        trading_date       TEXT,
        source             TEXT NOT NULL,
        note               TEXT,
        raw_reference      TEXT NOT NULL,
        fingerprint        TEXT NOT NULL,
        refresh_id         TEXT,
        created_at         TEXT NOT NULL,
        UNIQUE (broker_event_id, revision)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_cash_flow_events_effective ON cash_flow_events (effective_at);",
    "CREATE INDEX IF NOT EXISTS idx_cash_flow_events_trading_date ON cash_flow_events (trading_date);",
    "CREATE TRIGGER IF NOT EXISTS cash_flow_events_no_update BEFORE UPDATE ON cash_flow_events "
    "BEGIN SELECT RAISE(ABORT, 'cash flow events are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS cash_flow_events_no_delete BEFORE DELETE ON cash_flow_events "
    "BEGIN SELECT RAISE(ABORT, 'cash flow events are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS cash_flow_events_no_replace BEFORE INSERT ON cash_flow_events "
    "WHEN EXISTS (SELECT 1 FROM cash_flow_events WHERE event_id = NEW.event_id OR (broker_event_id = NEW.broker_event_id AND revision = NEW.revision)) "
    "BEGIN SELECT RAISE(ABORT, 'cash flow events are never replaced'); END;",
    """
    CREATE TABLE IF NOT EXISTS cash_flow_refresh_attempts (
        attempt_id          TEXT PRIMARY KEY,
        refresh_id          TEXT,
        started_at          TEXT NOT NULL,
        finished_at         TEXT NOT NULL,
        trigger             TEXT NOT NULL,
        result              TEXT NOT NULL CHECK (result IN ('SUCCESS', 'INCOMPLETE', 'FAILED', 'INVALID')),
        read_attempts       INTEGER NOT NULL DEFAULT 1,
        pages               INTEGER NOT NULL DEFAULT 0,
        rows_read           INTEGER NOT NULL DEFAULT 0,
        new_events          INTEGER NOT NULL DEFAULT 0,
        revised_events      INTEGER NOT NULL DEFAULT 0,
        skipped_trade_rows  INTEGER NOT NULL DEFAULT 0,
        complete            INTEGER NOT NULL DEFAULT 0,
        lookback_after      TEXT,
        error_code          TEXT,
        error_text          TEXT,
        owner               TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_cash_flow_attempts_started ON cash_flow_refresh_attempts (started_at);",
    "CREATE TRIGGER IF NOT EXISTS cash_flow_attempts_no_update BEFORE UPDATE ON cash_flow_refresh_attempts "
    "BEGIN SELECT RAISE(ABORT, 'cash flow refresh attempts are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS cash_flow_attempts_no_delete BEFORE DELETE ON cash_flow_refresh_attempts "
    "BEGIN SELECT RAISE(ABORT, 'cash flow refresh attempts are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS cash_flow_attempts_no_replace BEFORE INSERT ON cash_flow_refresh_attempts "
    "WHEN EXISTS (SELECT 1 FROM cash_flow_refresh_attempts WHERE attempt_id = NEW.attempt_id) BEGIN SELECT RAISE(ABORT, 'attempts are never replaced'); END;",
    """
    CREATE TABLE IF NOT EXISTS cash_flow_liveness (
        id                    INTEGER PRIMARY KEY CHECK (id = 1),
        last_attempt_at       TEXT,
        last_success_at       TEXT,
        attempt_count         INTEGER NOT NULL DEFAULT 0,
        success_count         INTEGER NOT NULL DEFAULT 0,
        failure_count         INTEGER NOT NULL DEFAULT 0,
        consecutive_failures  INTEGER NOT NULL DEFAULT 0,
        event_count           INTEGER NOT NULL DEFAULT 0,
        last_result           TEXT,
        last_error            TEXT,
        last_event_at         TEXT,
        last_complete         INTEGER,
        last_pages            INTEGER,
        owner                 TEXT,
        updated_at            TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS cash_flow_liveness_no_delete BEFORE DELETE ON cash_flow_liveness "
    "BEGIN SELECT RAISE(ABORT, 'the cash flow liveness row is kept'); END;",
]


def run_cash_flow_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
