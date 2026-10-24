"""
database/forward_validation_migrations.py - Stage 6.1 FORWARD PAPER VALIDATION (additive). Idempotent; safe on any database state. Only NEW
tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 / 6 migration).

  * forward_validation_sessions        one row per New York trading session the observer saw (or back-filled as NOT_RUN); identity
                                       (trading_date, created_at) immutable; the lifecycle columns progress until the session is closed;
                                       never deleted.
  * forward_validation_samples         append-only observer ticks: what the bot, the gate, the account data, the cash-flow data and the
                                       reconciliation looked like at that moment (the raw evidence every summary is rebuilt from).
  * forward_validation_marks           append-only price marks of open paper positions (the executor's own last price per cycle, sampled
                                       at the observer cadence) - the source of the sampled MFE / MAE.
  * forward_validation_restarts        append-only: every process restart the observer detected, with the state before and after.
  * forward_validation_events          append-only: session opened / closed, NOT_RUN back-fill, restarts, attention raised / cleared,
                                       the early stop, summary rebuilds, observer errors.
  * forward_validation_daily_summary   one DERIVED row per trading date (rebuildable: upserted, never deleted).
  * forward_validation_controls        liveness rows per control (upserted, never deleted).
Raw rows are never updated or deleted; derived summaries may be rebuilt. Nothing here stores a credential, an order or a recommendation.
"""
import sqlite3

SESSION_STATUSES = ("OPEN", "HEALTHY", "DEGRADED", "FAILED", "NOT_RUN")
TABLES = ("forward_validation_sessions", "forward_validation_samples", "forward_validation_marks", "forward_validation_restarts", "forward_validation_events",
          "forward_validation_daily_summary", "forward_validation_controls")
_q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731

_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS forward_validation_sessions (
        trading_date               TEXT PRIMARY KEY,
        started_at                 TEXT,
        ended_at                   TEXT,
        bot_start_time             TEXT,
        bot_stop_time              TEXT,
        market_open_seen           INTEGER NOT NULL DEFAULT 0 CHECK (market_open_seen IN (0, 1)),
        market_close_seen          INTEGER NOT NULL DEFAULT 0 CHECK (market_close_seen IN (0, 1)),
        paper_auto_enabled         INTEGER NOT NULL DEFAULT 0 CHECK (paper_auto_enabled IN (0, 1)),
        account_refresh_healthy    INTEGER CHECK (account_refresh_healthy IS NULL OR account_refresh_healthy IN (0, 1)),
        cash_flow_healthy          INTEGER CHECK (cash_flow_healthy IS NULL OR cash_flow_healthy IN (0, 1)),
        reconciliation_healthy     INTEGER CHECK (reconciliation_healthy IS NULL OR reconciliation_healthy IN (0, 1)),
        session_status             TEXT NOT NULL CHECK (session_status IN ({_q(SESSION_STATUSES)})),
        notes_code                 TEXT NOT NULL DEFAULT '',
        in_session_samples         INTEGER NOT NULL DEFAULT 0,
        owners_json                TEXT NOT NULL DEFAULT '[]',
        created_at                 TEXT NOT NULL,
        updated_at                 TEXT NOT NULL
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_identity_fixed BEFORE UPDATE OF trading_date, created_at ON forward_validation_sessions
    BEGIN SELECT RAISE(ABORT, 'forward validation session identity is immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_final BEFORE UPDATE OF session_status ON forward_validation_sessions
    WHEN OLD.session_status IN ('HEALTHY', 'DEGRADED', 'FAILED') AND NEW.session_status != OLD.session_status
    BEGIN SELECT RAISE(ABORT, 'a closed forward validation session is never reclassified'); END;
    """,
    "CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_no_delete BEFORE DELETE ON forward_validation_sessions "
    "BEGIN SELECT RAISE(ABORT, 'forward validation sessions are kept'); END;",
    """
    CREATE TABLE IF NOT EXISTS forward_validation_samples (
        sample_id              TEXT PRIMARY KEY,
        at                     TEXT NOT NULL,
        trading_date           TEXT NOT NULL,
        in_session             INTEGER NOT NULL CHECK (in_session IN (0, 1)),
        owner                  TEXT NOT NULL,
        mode                   TEXT NOT NULL,
        executor_ready         INTEGER NOT NULL DEFAULT 0,
        safety_state           TEXT,
        allowed_new_entry      INTEGER,
        safety_codes_json      TEXT NOT NULL DEFAULT '[]',
        reconciliation_healthy INTEGER,
        attention_count        INTEGER NOT NULL DEFAULT 0,
        attention_json         TEXT NOT NULL DEFAULT '[]',
        account_status         TEXT,
        account_age_s          REAL,
        account_failures       INTEGER,
        equity                 TEXT,
        cash                   TEXT,
        buying_power           TEXT,
        net_external_cum       TEXT,
        cash_flow_status       TEXT,
        cash_flow_age_s        REAL,
        cash_flow_failures     INTEGER,
        open_positions         INTEGER NOT NULL DEFAULT 0,
        pending_entries        INTEGER NOT NULL DEFAULT 0,
        pending_exits          INTEGER NOT NULL DEFAULT 0,
        positions_json         TEXT NOT NULL DEFAULT '[]',
        controls_json          TEXT NOT NULL DEFAULT '{}',
        last_cycle_at          TEXT,
        last_cycle_result      TEXT,
        loop_running           INTEGER
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_forward_validation_samples_date ON forward_validation_samples (trading_date, at);",
    """
    CREATE TABLE IF NOT EXISTS forward_validation_marks (
        mark_id        TEXT PRIMARY KEY,
        at             TEXT NOT NULL,
        position_id    TEXT NOT NULL,
        symbol         TEXT NOT NULL,
        state          TEXT NOT NULL,
        quantity       INTEGER NOT NULL,
        last_price     TEXT,
        last_price_at  TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_forward_validation_marks_position ON forward_validation_marks (position_id, at);",
    """
    CREATE TABLE IF NOT EXISTS forward_validation_restarts (
        restart_id            TEXT PRIMARY KEY,
        at                    TEXT NOT NULL,
        trading_date          TEXT NOT NULL,
        previous_owner        TEXT,
        previous_sample_at    TEXT,
        owner                 TEXT NOT NULL,
        state_before_json     TEXT NOT NULL DEFAULT '{}',
        state_after_json      TEXT NOT NULL DEFAULT '{}',
        broker_queried        INTEGER NOT NULL DEFAULT 0,
        reconciliation_result TEXT NOT NULL,
        duplicate_orders      INTEGER NOT NULL DEFAULT 0,
        position_continuity   TEXT NOT NULL,
        detail_json           TEXT NOT NULL DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_validation_events (
        event_id      TEXT PRIMARY KEY,
        at            TEXT NOT NULL,
        trading_date  TEXT,
        kind          TEXT NOT NULL,
        code          TEXT,
        detail_json   TEXT NOT NULL DEFAULT '{}',
        owner         TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_forward_validation_events_at ON forward_validation_events (at);",
    """
    CREATE TABLE IF NOT EXISTS forward_validation_daily_summary (
        trading_date   TEXT PRIMARY KEY,
        computed_at    TEXT NOT NULL,
        version        TEXT NOT NULL,
        summary_json   TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS forward_validation_daily_summary_no_delete BEFORE DELETE ON forward_validation_daily_summary "
    "BEGIN SELECT RAISE(ABORT, 'daily summaries are rebuilt, never deleted'); END;",
    """
    CREATE TABLE IF NOT EXISTS forward_validation_controls (
        control_id         TEXT PRIMARY KEY,
        last_evaluated_at  TEXT NOT NULL,
        last_fired_at      TEXT,
        evaluation_count   INTEGER NOT NULL DEFAULT 0,
        fire_count         INTEGER NOT NULL DEFAULT 0,
        last_result        TEXT NOT NULL,
        last_reason        TEXT,
        last_error         TEXT,
        updated_at         TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS forward_validation_controls_no_delete BEFORE DELETE ON forward_validation_controls "
    "BEGIN SELECT RAISE(ABORT, 'forward validation control rows are kept'); END;",
]

# append-only raw evidence: no UPDATE, no DELETE, no INSERT OR REPLACE of an existing key
for _t, _key in (("forward_validation_samples", "sample_id"), ("forward_validation_marks", "mark_id"), ("forward_validation_restarts", "restart_id"),
                 ("forward_validation_events", "event_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_key} = NEW.{_key}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")
_STATEMENTS.append("CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_no_replace BEFORE INSERT ON forward_validation_sessions "
                   "WHEN EXISTS (SELECT 1 FROM forward_validation_sessions WHERE trading_date = NEW.trading_date) "
                   "BEGIN SELECT RAISE(ABORT, 'already stored: a trading session row is never replaced'); END;")


def run_forward_validation_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
