"""
database/autorun_migrations.py - Stage 6.10A FORWARD PAPER AUTO-RUN (additive). Idempotent; safe on any database state. Only NEW tables; every
earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 - 6 migration).

  * autorun_events       append-only: STARTUP, STARTUP_DISARM, PREFLIGHT_CHANGED, ARMED, DISARMED, ARM_SKIPPED, ARM_REFUSED, INSTANCE, FAULT
  * autorun_preflights   append-only: each preflight result (READY / DEGRADED / BLOCKED) with every check, when it changed or at startup
  * autorun_days         one row per New York trading date (upserted, never deleted): the first arm / last disarm of the day and whether the
                         scheduled arm already ran - the 'at most once per day' and 'respect a manual disarm' memory survives restarts
Nothing here stores a credential, an order, an account number or a Robinhood holding.
"""
import sqlite3

TABLES = ("autorun_events", "autorun_preflights", "autorun_days")

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS autorun_events (
        event_id      TEXT PRIMARY KEY,
        at            TEXT NOT NULL,
        trading_date  TEXT,
        kind          TEXT NOT NULL,
        code          TEXT,
        actor         TEXT,
        detail_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
        owner         TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_autorun_events_at ON autorun_events (at);",
    """
    CREATE TABLE IF NOT EXISTS autorun_preflights (
        preflight_id  TEXT PRIMARY KEY,
        at            TEXT NOT NULL,
        trigger       TEXT NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('READY', 'DEGRADED', 'BLOCKED')),
        checks_json   TEXT NOT NULL CHECK (json_valid(checks_json)),
        policy        TEXT NOT NULL,
        owner         TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_autorun_preflights_at ON autorun_preflights (at);",
    """
    CREATE TABLE IF NOT EXISTS autorun_days (
        trading_date          TEXT PRIMARY KEY,
        first_armed_at        TEXT,
        first_armed_by        TEXT,
        last_disarmed_at      TEXT,
        last_disarmed_by      TEXT,
        last_disarm_code      TEXT,
        scheduled_arm_at      TEXT,
        scheduled_arm_result  TEXT,
        updated_at            TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS autorun_days_no_delete BEFORE DELETE ON autorun_days BEGIN SELECT RAISE(ABORT, 'autorun day rows are kept'); END;",
]
for _t, _key in (("autorun_events", "event_id"), ("autorun_preflights", "preflight_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_key} = NEW.{_key}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_autorun_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
