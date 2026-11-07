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
    # Wave 2 PR B (session classifier v2, design 3b / B-29 / B-34): an index for the at-range reads and three additive integrity triggers
    "CREATE INDEX IF NOT EXISTS idx_forward_validation_samples_at ON forward_validation_samples (at);",
    "CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_v2_not_run_final BEFORE UPDATE ON forward_validation_sessions "
    "WHEN OLD.session_status = 'NOT_RUN' BEGIN SELECT RAISE(ABORT, 'a NOT_RUN forward validation session is final'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_v2_closed_frozen BEFORE UPDATE ON forward_validation_sessions "
    "WHEN OLD.session_status != 'OPEN' BEGIN SELECT RAISE(ABORT, 'a closed forward validation session is frozen'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_validation_sessions_v2_insert_status BEFORE INSERT ON forward_validation_sessions "
    "WHEN NEW.session_status NOT IN ('OPEN', 'NOT_RUN') BEGIN SELECT RAISE(ABORT, 'a forward validation session is inserted OPEN or NOT_RUN only'); END;",
]
V2_SAMPLE_COLUMNS = (("rr_intents", "INTEGER"), ("attention_positions", "INTEGER"), ("failed_intents", "INTEGER"), ("worker_stale_after_s", "REAL"))   # Wave 2 PR B
V2_SESSION_COLUMNS = (("classifier_version", "TEXT"), ("assessment_json", "TEXT"))                                                        # nullable, no default


def _add_columns(conn: sqlite3.Connection, table: str, columns) -> None:
    """Wave 2 PR B: add the nullable columns a table lacks (idempotent; a concurrent ADD COLUMN by another process is treated as done)."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

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
RUN_STATEMENTS = [                                                                       # Wave 2 PR C (design 4.1): the forward run, additive
    "CREATE TABLE IF NOT EXISTS forward_runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, started_at TEXT NOT NULL, first_session_date TEXT NOT NULL, "
    "frozen_hash TEXT NOT NULL, frozen_controls_json TEXT NOT NULL, cohort_id TEXT NOT NULL, config_hash TEXT NOT NULL, execution_version TEXT NOT NULL, "
    "classifier_version TEXT NOT NULL, min_trades INTEGER NOT NULL, target_sessions INTEGER NOT NULL, target_events INTEGER NOT NULL, minimums_source TEXT NOT NULL, "
    "base_equity TEXT NOT NULL, approved_by TEXT NOT NULL, approval_ref TEXT, evidence_ref TEXT NOT NULL, accepted_blockers_json TEXT NOT NULL, "
    "acknowledged_attention_json TEXT NOT NULL, server_config_json TEXT NOT NULL, prior_invalid_days_json TEXT NOT NULL, ended_at TEXT, end_reason TEXT);",
    "CREATE TABLE IF NOT EXISTS forward_run_events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, at TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK (kind IN ('STARTED', 'SUSPENDED', 'ENDED', 'PRIOR_INVALID_ACKNOWLEDGED')), code TEXT, detail_json TEXT NOT NULL DEFAULT '{}');",
    "CREATE UNIQUE INDEX IF NOT EXISTS forward_runs_one_active ON forward_runs(IFNULL(ended_at, '')) WHERE ended_at IS NULL;",
    "CREATE TRIGGER IF NOT EXISTS forward_runs_immutable BEFORE UPDATE ON forward_runs WHEN OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL "
    "OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at OR NEW.started_at IS NOT OLD.started_at "
    "OR NEW.first_session_date IS NOT OLD.first_session_date OR NEW.frozen_hash IS NOT OLD.frozen_hash OR NEW.frozen_controls_json IS NOT OLD.frozen_controls_json "
    "OR NEW.cohort_id IS NOT OLD.cohort_id OR NEW.config_hash IS NOT OLD.config_hash OR NEW.execution_version IS NOT OLD.execution_version "
    "OR NEW.classifier_version IS NOT OLD.classifier_version OR NEW.min_trades IS NOT OLD.min_trades OR NEW.target_sessions IS NOT OLD.target_sessions "
    "OR NEW.target_events IS NOT OLD.target_events OR NEW.minimums_source IS NOT OLD.minimums_source OR NEW.base_equity IS NOT OLD.base_equity "
    "OR NEW.approved_by IS NOT OLD.approved_by OR NEW.approval_ref IS NOT OLD.approval_ref OR NEW.evidence_ref IS NOT OLD.evidence_ref "
    "OR NEW.accepted_blockers_json IS NOT OLD.accepted_blockers_json OR NEW.acknowledged_attention_json IS NOT OLD.acknowledged_attention_json "
    "OR NEW.server_config_json IS NOT OLD.server_config_json OR NEW.prior_invalid_days_json IS NOT OLD.prior_invalid_days_json "
    "BEGIN SELECT RAISE(ABORT, 'a forward run is immutable; only ended_at / end_reason are set, once'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_runs_start_order BEFORE INSERT ON forward_runs "
    "WHEN EXISTS (SELECT 1 FROM forward_runs WHERE ended_at IS NULL OR ended_at > NEW.started_at) "
    "BEGIN SELECT RAISE(ABORT, 'a forward run starts only after the previous one ended'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_runs_no_replace BEFORE INSERT ON forward_runs WHEN EXISTS (SELECT 1 FROM forward_runs WHERE run_id = NEW.run_id) "
    "BEGIN SELECT RAISE(ABORT, 'already stored: a forward run is never replaced'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_runs_no_delete BEFORE DELETE ON forward_runs BEGIN SELECT RAISE(ABORT, 'a forward run is never deleted'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_run_events_no_update BEFORE UPDATE ON forward_run_events BEGIN SELECT RAISE(ABORT, 'forward_run_events rows are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_run_events_no_delete BEFORE DELETE ON forward_run_events BEGIN SELECT RAISE(ABORT, 'forward_run_events rows are append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_run_events_no_replace BEFORE INSERT ON forward_run_events "
    "WHEN EXISTS (SELECT 1 FROM forward_run_events WHERE event_id = NEW.event_id) BEGIN SELECT RAISE(ABORT, 'already stored: forward_run_events rows are never replaced'); END;",
]
RUN_SAMPLE_COLUMNS = (("frozen_hash", "TEXT"), ("run_id", "TEXT"))                          # Wave 2 PR C (C-14): the sample stamps; NULL = written without PR C


def _add_run_columns(conn: sqlite3.Connection) -> None:
    """Wave 2 PR C: the nullable run stamps of the samples (PRAGMA-guarded, as PR B's); old rows stay NULL."""
    _add_columns(conn, "forward_validation_samples", RUN_SAMPLE_COLUMNS)


def run_forward_validation_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    _add_columns(conn, "forward_validation_samples", V2_SAMPLE_COLUMNS)
    _add_columns(conn, "forward_validation_sessions", V2_SESSION_COLUMNS)
    _add_run_columns(conn)                                                                   # Wave 2 PR C
    for stmt in RUN_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
