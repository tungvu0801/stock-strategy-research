"""
database/execution_safety_migrations.py — Stage 5.9.1 REAL EXECUTION SAFETY CONTROLS (additive). Idempotent; safe on any database state.
Only NEW tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 migration).

  * execution_safety_state        ONE row (id = 1), upserted: the manual kill switch, the no-new-entries pause and the daily-loss latch.
  * daily_risk_baselines          one row per trading date: the persisted start-of-day equity the daily loss is measured against.
                                  Immutable; never derived on the fly.
  * execution_safety_events       append-only: user actions, baseline creation, daily-loss trigger, day reset, evaluator errors.
  * execution_safety_evaluations  append-only: every persisted gate evaluation with its inputs.
  * execution_safety_controls     liveness rows per control (upserted, never deleted): evaluated / fired counts, last result, last error.
Nothing here stores an order, a recommendation or a side.
"""
import sqlite3

STATES = ("TRADING_ALLOWED", "NO_NEW_ENTRIES", "HALTED", "UNKNOWN")
RESULTS = ("PASS", "BLOCK", "WARN", "UNKNOWN")
EVENT_KINDS = ("PAUSE_ON", "PAUSE_OFF", "KILL_ON", "KILL_OFF", "BASELINE_CREATED", "DAILY_HALT_TRIGGERED", "DAILY_RESET", "EVALUATOR_ERROR", "STARTUP")
TABLES = ("execution_safety_state", "daily_risk_baselines", "execution_safety_events", "execution_safety_evaluations", "execution_safety_controls")
_q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS execution_safety_state (
        id                        INTEGER PRIMARY KEY CHECK (id = 1),
        kill_active               INTEGER NOT NULL DEFAULT 0 CHECK (kill_active IN (0, 1)),
        kill_activated_at         TEXT,
        kill_activated_by         TEXT,
        kill_reason               TEXT,
        pause_active              INTEGER NOT NULL DEFAULT 0 CHECK (pause_active IN (0, 1)),
        pause_activated_at        TEXT,
        pause_activated_by        TEXT,
        pause_reason              TEXT,
        daily_halt_active         INTEGER NOT NULL DEFAULT 0 CHECK (daily_halt_active IN (0, 1)),
        daily_halt_trading_date   TEXT,
        daily_halt_triggered_at   TEXT,
        daily_halt_loss_pct       TEXT,
        daily_halt_reason         TEXT,
        updated_at                TEXT NOT NULL
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS execution_safety_state_no_delete BEFORE DELETE ON execution_safety_state
    BEGIN SELECT RAISE(ABORT, 'the execution safety state row is kept'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_risk_baselines (
        trading_date         TEXT PRIMARY KEY,
        starting_equity      TEXT NOT NULL,
        baseline_created_at  TEXT NOT NULL,
        source               TEXT NOT NULL,
        adjustments          TEXT NOT NULL,
        status               TEXT NOT NULL DEFAULT 'ACTIVE',
        equity_as_of         TEXT,
        note                 TEXT
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS daily_risk_baselines_no_update BEFORE UPDATE ON daily_risk_baselines
    BEGIN SELECT RAISE(ABORT, 'daily risk baselines are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS daily_risk_baselines_no_delete BEFORE DELETE ON daily_risk_baselines
    BEGIN SELECT RAISE(ABORT, 'daily risk baselines are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS execution_safety_events (
        event_id      TEXT PRIMARY KEY CHECK (length(event_id) = 32),
        at            TEXT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ({_q(EVENT_KINDS)})),
        actor         TEXT NOT NULL,
        reason        TEXT,
        trading_date  TEXT,
        from_state    TEXT,
        to_state      TEXT,
        input_json    TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_execution_safety_events_at ON execution_safety_events (at DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS execution_safety_events_no_update BEFORE UPDATE ON execution_safety_events
    BEGIN SELECT RAISE(ABORT, 'execution safety events are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS execution_safety_events_no_delete BEFORE DELETE ON execution_safety_events
    BEGIN SELECT RAISE(ABORT, 'execution safety events are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS execution_safety_evaluations (
        evaluation_id       TEXT PRIMARY KEY CHECK (length(evaluation_id) = 32),
        evaluated_at        TEXT NOT NULL,
        trigger             TEXT NOT NULL,
        state               TEXT NOT NULL CHECK (state IN ({_q(STATES)})),
        allowed_new_entry   INTEGER NOT NULL CHECK (allowed_new_entry IN (0, 1)),
        reason_codes_json   TEXT NOT NULL,
        daily_loss_json     TEXT NOT NULL,
        kill_json           TEXT NOT NULL,
        pause_json          TEXT NOT NULL,
        freshness_json      TEXT NOT NULL,
        input_json          TEXT NOT NULL,
        error               TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_execution_safety_evaluations_at ON execution_safety_evaluations (evaluated_at DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS execution_safety_evaluations_no_update BEFORE UPDATE ON execution_safety_evaluations
    BEGIN SELECT RAISE(ABORT, 'execution safety evaluations are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS execution_safety_evaluations_no_delete BEFORE DELETE ON execution_safety_evaluations
    BEGIN SELECT RAISE(ABORT, 'execution safety evaluations are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS execution_safety_controls (
        control_id        TEXT PRIMARY KEY,
        last_evaluated_at TEXT NOT NULL,
        last_fired_at     TEXT,
        evaluation_count  INTEGER NOT NULL DEFAULT 0,
        fire_count        INTEGER NOT NULL DEFAULT 0,
        last_result       TEXT NOT NULL CHECK (last_result IN ({_q(RESULTS)})),
        last_reason       TEXT,
        last_input_json   TEXT,
        last_error        TEXT,
        updated_at        TEXT NOT NULL
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS execution_safety_controls_no_delete BEFORE DELETE ON execution_safety_controls
    BEGIN SELECT RAISE(ABORT, 'execution safety control rows are kept'); END;
    """,
]

# an INSERT OR REPLACE would delete the existing row without firing the delete triggers: refuse any second row instead
for _t, _where in (("daily_risk_baselines", "trading_date = NEW.trading_date"), ("execution_safety_events", "event_id = NEW.event_id"),
                   ("execution_safety_evaluations", "evaluation_id = NEW.evaluation_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_where}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_execution_safety_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
