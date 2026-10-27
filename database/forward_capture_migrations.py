"""
database/forward_capture_migrations.py - Stage 6.10A FORWARD EVIDENCE CAPTURE (additive). Idempotent; safe on any database state. Only NEW tables;
every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 - 6 migration).

  * forward_capture_cohorts           append-only: one row per forward paper cohort (decision-relevant configuration)
  * forward_capture_config_snapshots  append-only: the allowlisted configuration and its hash at a process start, a trading day or a change
  * forward_capture_candidates        append-only: one row per setup episode (the candidate)
  * forward_capture_decisions         append-only: one row per candidate decision (setup evaluation, executor cycle, intent, position)
  * forward_capture_entry_snapshots   append-only: one decision-time snapshot per entry intent (immutable)
  * forward_capture_outcomes          append-only: one outcome per closed bot position (immutable)
  * forward_capture_context           append-only: point-in-time context of the other accounts (states and freshness only)
  * forward_capture_faults            append-only: every recording fault (an evidence gap is recorded, never hidden)
  * forward_capture_state             upserted, never deleted: watermarks and the capture epoch
  * forward_capture_session_health    one row per trading date, upserted while open and frozen once final; never deleted
Nothing here stores a credential, an account number, an order instruction or a holding of another account.
"""
import sqlite3

TABLES = ("forward_capture_cohorts", "forward_capture_config_snapshots", "forward_capture_candidates", "forward_capture_decisions",
          "forward_capture_entry_snapshots", "forward_capture_outcomes", "forward_capture_context", "forward_capture_faults", "forward_capture_state",
          "forward_capture_session_health")
APPEND_ONLY = (("forward_capture_cohorts", "cohort_id"), ("forward_capture_config_snapshots", "snapshot_id"), ("forward_capture_candidates", "candidate_id"),
               ("forward_capture_decisions", "decision_id"), ("forward_capture_entry_snapshots", "execution_id"), ("forward_capture_outcomes", "position_id"),
               ("forward_capture_context", "context_id"), ("forward_capture_faults", "fault_id"))

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS forward_capture_cohorts (
        cohort_id        TEXT PRIMARY KEY,
        cohort_key       TEXT NOT NULL UNIQUE,
        label            TEXT NOT NULL UNIQUE,
        started_at       TEXT NOT NULL,
        definition_json  TEXT NOT NULL CHECK (json_valid(definition_json))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_capture_config_snapshots (
        snapshot_id   TEXT PRIMARY KEY,
        at            TEXT NOT NULL,
        trigger       TEXT NOT NULL,
        trading_date  TEXT,
        config_hash   TEXT NOT NULL,
        cohort_id     TEXT NOT NULL,
        config_json   TEXT NOT NULL CHECK (json_valid(config_json)),
        owner         TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_forward_capture_config_snapshots_at ON forward_capture_config_snapshots (at);",
    """
    CREATE TABLE IF NOT EXISTS forward_capture_candidates (
        candidate_id      TEXT PRIMARY KEY,
        setup_id          TEXT NOT NULL UNIQUE,
        symbol            TEXT NOT NULL,
        setup_type        TEXT NOT NULL,
        cycle_id          INTEGER,
        strategy_id       TEXT NOT NULL,
        strategy_version  TEXT NOT NULL,
        started_at        TEXT,
        first_seen_at     TEXT NOT NULL,
        first_state       TEXT,
        cohort_id         TEXT,
        legacy            INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_capture_decisions (
        decision_id        TEXT PRIMARY KEY,
        candidate_id       TEXT,
        setup_id           TEXT,
        symbol             TEXT NOT NULL,
        cohort_id          TEXT,
        decided_at         TEXT NOT NULL,
        observed_at        TEXT NOT NULL,
        trading_date       TEXT,
        market_session     TEXT,
        mode               TEXT,
        source             TEXT NOT NULL,
        source_ref         TEXT,
        decision           TEXT NOT NULL,
        reason_codes_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reason_codes_json)),
        setup_state        TEXT,
        evaluation_id      TEXT,
        features_json      TEXT CHECK (features_json IS NULL OR json_valid(features_json)),
        score              TEXT,
        proposal_json      TEXT CHECK (proposal_json IS NULL OR json_valid(proposal_json)),
        account_json       TEXT CHECK (account_json IS NULL OR json_valid(account_json)),
        exposure_json      TEXT CHECK (exposure_json IS NULL OR json_valid(exposure_json)),
        controls_version   TEXT,
        contract_hash      TEXT,
        controls_json      TEXT CHECK (controls_json IS NULL OR json_valid(controls_json)),
        execution_id       TEXT,
        position_id        TEXT,
        run_id             TEXT,
        recovered          INTEGER NOT NULL DEFAULT 0,
        legacy             INTEGER NOT NULL DEFAULT 0,
        context_at         TEXT,
        detail_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
        content_sha256     TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_forward_capture_decisions_date ON forward_capture_decisions (trading_date, decided_at);",
    "CREATE INDEX IF NOT EXISTS ix_forward_capture_decisions_candidate ON forward_capture_decisions (candidate_id);",
    "CREATE INDEX IF NOT EXISTS ix_forward_capture_decisions_execution ON forward_capture_decisions (execution_id);",
    """
    CREATE TABLE IF NOT EXISTS forward_capture_entry_snapshots (
        execution_id     TEXT PRIMARY KEY,
        candidate_id     TEXT,
        decision_id      TEXT,
        cohort_id        TEXT,
        captured_at      TEXT NOT NULL,
        snapshot_json    TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        snapshot_sha256  TEXT NOT NULL,
        recovered        INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_capture_outcomes (
        position_id     TEXT PRIMARY KEY,
        execution_id    TEXT,
        candidate_id    TEXT,
        cohort_id       TEXT,
        closed_at       TEXT,
        outcome_json    TEXT NOT NULL CHECK (json_valid(outcome_json)),
        outcome_sha256  TEXT NOT NULL,
        cross_cohort    INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_capture_context (
        context_id      TEXT PRIMARY KEY,
        at              TEXT NOT NULL,
        unified_state   TEXT,
        context_json    TEXT NOT NULL CHECK (json_valid(context_json)),
        content_sha256  TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_forward_capture_context_at ON forward_capture_context (at);",
    """
    CREATE TABLE IF NOT EXISTS forward_capture_faults (
        fault_id      TEXT PRIMARY KEY,
        at            TEXT NOT NULL,
        trading_date  TEXT,
        component     TEXT NOT NULL,
        code          TEXT NOT NULL,
        detail_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_forward_capture_faults_date ON forward_capture_faults (trading_date);",
    """
    CREATE TABLE IF NOT EXISTS forward_capture_state (
        key         TEXT PRIMARY KEY,
        value       TEXT,
        updated_at  TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS forward_capture_state_no_delete BEFORE DELETE ON forward_capture_state BEGIN SELECT RAISE(ABORT, 'capture state rows are kept'); END;",
    """
    CREATE TABLE IF NOT EXISTS forward_capture_session_health (
        trading_date           TEXT PRIMARY KEY,
        status                 TEXT NOT NULL CHECK (status IN ('HEALTHY', 'DEGRADED', 'INVALID', 'MARKET_CLOSED')),
        final                  INTEGER NOT NULL DEFAULT 0,
        reasons_json           TEXT NOT NULL CHECK (json_valid(reasons_json)),
        metrics_json           TEXT NOT NULL CHECK (json_valid(metrics_json)),
        summary_text           TEXT,
        cohort_ids_json        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cohort_ids_json)),
        source_session_status  TEXT,
        computed_at            TEXT NOT NULL,
        version                TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS forward_capture_session_health_no_delete BEFORE DELETE ON forward_capture_session_health "
    "BEGIN SELECT RAISE(ABORT, 'session health rows are kept'); END;",
    "CREATE TRIGGER IF NOT EXISTS forward_capture_session_health_final BEFORE UPDATE ON forward_capture_session_health WHEN OLD.final = 1 "
    "BEGIN SELECT RAISE(ABORT, 'a final session health row is never changed'); END;",
]
for _t, _key in APPEND_ONLY:
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_key} = NEW.{_key}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_forward_capture_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
