"""
database/setup_state_migrations.py — Stage 5.8 SETUP STATE MACHINE + ALERTS (additive). Idempotent; safe on any database state. Only NEW
tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 migration).

  * setup_episodes      one row per setup episode (symbol × setup type × cycle). Its state fields progress (state, previous_state,
                        last_transition_at, last_evaluated_at, closed_at, invalidation_reason, …); identity fields never change and no
                        row is ever deleted. At most one OPEN episode per symbol + setup type (partial unique index).
  * setup_transitions   append-only, immutable: every state change with the inputs and freshness it was decided on.
  * setup_evaluations   append-only, immutable: every evaluation (TRANSITION / HOLD / PAUSED / ERROR) — "when did this last fire?"
                        is answerable from data, not from console logs.
  * setup_alerts        append-only; only read_at ever changes (the Stage 4.1 alert pattern). One alert per transition at most.
  * setup_controls      small liveness rows per control (upserted, never deleted).
Nothing here stores an order, a recommendation or a probability.
"""
import sqlite3

STATES = ("NONE", "WATCH", "READY", "INVALIDATED")
DECISIONS = ("TRANSITION", "HOLD", "PAUSED", "ERROR")
TRIGGERS = ("SCHEDULED", "SUBSCRIBE", "REFRESH", "MANUAL", "STARTUP", "TEST")
TABLES = ("setup_episodes", "setup_transitions", "setup_evaluations", "setup_alerts", "setup_controls")
_q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
_ID = "CHECK (length({c}) = 32)"

_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS setup_episodes (
        setup_id                     TEXT PRIMARY KEY {_ID.format(c="setup_id")},
        symbol                       TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 12),
        setup_type                   TEXT NOT NULL,
        cycle_id                     INTEGER NOT NULL CHECK (cycle_id >= 1),
        state                        TEXT NOT NULL CHECK (state IN ({_q(STATES)})),
        previous_state               TEXT CHECK (previous_state IS NULL OR previous_state IN ({_q(STATES)})),
        started_at                   TEXT NOT NULL,
        last_transition_at           TEXT NOT NULL,
        last_evaluated_at            TEXT NOT NULL,
        closed_at                    TEXT,
        invalidation_reason          TEXT,
        transition_reason            TEXT NOT NULL,
        demand_low_streak            INTEGER NOT NULL DEFAULT 0 CHECK (demand_low_streak >= 0),
        evidence_packet_id_at_start  TEXT,
        last_inputs_json             TEXT NOT NULL CHECK (json_valid(last_inputs_json)),
        last_freshness_json          TEXT NOT NULL CHECK (json_valid(last_freshness_json)),
        source                       TEXT NOT NULL DEFAULT '',
        mode                         TEXT NOT NULL DEFAULT '',
        updated_at                   TEXT NOT NULL,
        UNIQUE (symbol, setup_type, cycle_id)
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_setup_episodes_open ON setup_episodes (symbol, setup_type) WHERE closed_at IS NULL;",
    """
    CREATE TRIGGER IF NOT EXISTS setup_episodes_identity_fixed BEFORE UPDATE OF setup_id, symbol, setup_type, cycle_id, started_at ON setup_episodes
    BEGIN SELECT RAISE(ABORT, 'setup episode identity is immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS setup_episodes_no_delete BEFORE DELETE ON setup_episodes
    BEGIN SELECT RAISE(ABORT, 'setup episodes are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS setup_transitions (
        transition_id                TEXT PRIMARY KEY {_ID.format(c="transition_id")},
        setup_id                     TEXT NOT NULL REFERENCES setup_episodes(setup_id) ON DELETE RESTRICT,
        symbol                       TEXT NOT NULL,
        setup_type                   TEXT NOT NULL,
        from_state                   TEXT NOT NULL CHECK (from_state IN ({_q(STATES)})),
        to_state                     TEXT NOT NULL CHECK (to_state IN ({_q(STATES)})),
        reason_code                  TEXT NOT NULL,
        reason_text                  TEXT NOT NULL,
        evaluated_at                 TEXT NOT NULL,
        inputs_json                  TEXT NOT NULL CHECK (json_valid(inputs_json)),
        freshness_json               TEXT NOT NULL CHECK (json_valid(freshness_json)),
        source                       TEXT NOT NULL DEFAULT '',
        mode                         TEXT NOT NULL DEFAULT '',
        trigger                      TEXT NOT NULL CHECK (trigger IN ({_q(TRIGGERS)})),
        alert_id                     TEXT,
        alert_suppressed_reason      TEXT,
        created_at                   TEXT NOT NULL,
        CHECK (from_state <> to_state)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_setup_transitions_symbol ON setup_transitions (symbol, evaluated_at DESC);",
    "CREATE INDEX IF NOT EXISTS ix_setup_transitions_time ON setup_transitions (evaluated_at DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS setup_transitions_no_update BEFORE UPDATE ON setup_transitions
    BEGIN SELECT RAISE(ABORT, 'setup transitions are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS setup_transitions_no_delete BEFORE DELETE ON setup_transitions
    BEGIN SELECT RAISE(ABORT, 'setup transitions are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS setup_evaluations (
        evaluation_id                TEXT PRIMARY KEY {_ID.format(c="evaluation_id")},
        evaluated_at                 TEXT NOT NULL,
        symbol                       TEXT NOT NULL,
        setup_type                   TEXT NOT NULL,
        setup_id                     TEXT,
        prior_state                  TEXT NOT NULL CHECK (prior_state IN ({_q(STATES)})),
        resulting_state              TEXT NOT NULL CHECK (resulting_state IN ({_q(STATES)})),
        decision                     TEXT NOT NULL CHECK (decision IN ({_q(DECISIONS)})),
        reason_code                  TEXT NOT NULL,
        reason_text                  TEXT NOT NULL,
        inputs_json                  TEXT NOT NULL CHECK (json_valid(inputs_json)),
        freshness_json               TEXT NOT NULL CHECK (json_valid(freshness_json)),
        source                       TEXT NOT NULL DEFAULT '',
        mode                         TEXT NOT NULL DEFAULT '',
        trigger                      TEXT NOT NULL CHECK (trigger IN ({_q(TRIGGERS)})),
        owner                        TEXT NOT NULL DEFAULT ''
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_setup_evaluations_symbol ON setup_evaluations (symbol, evaluated_at DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS setup_evaluations_no_update BEFORE UPDATE ON setup_evaluations
    BEGIN SELECT RAISE(ABORT, 'setup evaluations are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS setup_evaluations_no_delete BEFORE DELETE ON setup_evaluations
    BEGIN SELECT RAISE(ABORT, 'setup evaluations are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS setup_alerts (
        alert_id                     TEXT PRIMARY KEY {_ID.format(c="alert_id")},
        transition_id                TEXT NOT NULL UNIQUE REFERENCES setup_transitions(transition_id) ON DELETE RESTRICT,
        setup_id                     TEXT NOT NULL,
        symbol                       TEXT NOT NULL,
        setup_type                   TEXT NOT NULL,
        from_state                   TEXT NOT NULL CHECK (from_state IN ({_q(STATES)})),
        to_state                     TEXT NOT NULL CHECK (to_state IN ({_q(STATES)})),
        reason_code                  TEXT NOT NULL,
        title                        TEXT NOT NULL,
        body                         TEXT NOT NULL,
        cooldown_key                 TEXT NOT NULL,
        created_at                   TEXT NOT NULL,
        read_at                      TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_setup_alerts_new ON setup_alerts (read_at, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS ix_setup_alerts_cooldown ON setup_alerts (cooldown_key, created_at DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS setup_alerts_content_fixed BEFORE UPDATE OF alert_id, transition_id, setup_id, symbol, setup_type, from_state, to_state,
                     reason_code, title, body, cooldown_key, created_at ON setup_alerts
    BEGIN SELECT RAISE(ABORT, 'setup alerts are immutable (only the read state changes)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS setup_alerts_no_delete BEFORE DELETE ON setup_alerts
    BEGIN SELECT RAISE(ABORT, 'setup alerts are kept'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS setup_controls (
        name                         TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 64),
        last_run_at                  TEXT,
        last_evaluated_at            TEXT,
        last_transition_at           TEXT,
        run_count                    INTEGER NOT NULL DEFAULT 0,
        evaluation_count             INTEGER NOT NULL DEFAULT 0,
        transition_count             INTEGER NOT NULL DEFAULT 0,
        error_count                  INTEGER NOT NULL DEFAULT 0,
        last_result                  TEXT,
        last_error                   TEXT,
        updated_at                   TEXT NOT NULL
    ) WITHOUT ROWID;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS setup_controls_no_delete BEFORE DELETE ON setup_controls
    BEGIN SELECT RAISE(ABORT, 'setup controls are kept'); END;
    """,
]

# an INSERT OR REPLACE would delete the existing row without firing the delete triggers: refuse any second row instead
for _t, _where in (("setup_episodes", "setup_id = NEW.setup_id OR (symbol = NEW.symbol AND setup_type = NEW.setup_type AND cycle_id = NEW.cycle_id)"),
                   ("setup_transitions", "transition_id = NEW.transition_id"), ("setup_evaluations", "evaluation_id = NEW.evaluation_id"),
                   ("setup_alerts", "alert_id = NEW.alert_id OR transition_id = NEW.transition_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_where}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_setup_state_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
