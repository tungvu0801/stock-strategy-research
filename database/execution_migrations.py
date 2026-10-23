"""
database/execution_migrations.py — Stage 6.0 AUTONOMOUS ALPACA PAPER TRADING (additive). Idempotent; safe on any database state.
Only NEW tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 migration).

  * execution_mode             ONE row (id = 1): MONITOR_ONLY | PAPER_AUTO, who changed it, when, the paper-environment proof.
  * execution_mode_events      append-only mode changes.
  * execution_runs             append-only: one row per executor cycle that did work (trigger, result, summary, error).
  * execution_intents          one row per entry / exit intent, written BEFORE the broker is contacted; identity columns immutable,
                               attempt_count never decreases and is at most 1 (exactly one POST per intent), a broker order id never changes.
  * execution_orders           the current broker view of each order we own (upserted); execution_order_events is its append-only history.
  * paper_positions            internal paper positions created ONLY from broker-confirmed fills; identity immutable; CLOSED is final.
  * position_events            append-only position lifecycle.
  * execution_reconciliations  append-only findings of every reconciliation pass (nothing is ever silently fixed).
  * execution_controls         liveness rows per control (upserted, never deleted).
Nothing here stores a recommendation; the broker is the Alpaca PAPER account only.
"""
import sqlite3

MODES = ("MONITOR_ONLY", "PAPER_AUTO")
INTENT_STATES = ("PENDING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "NOT_SENT", "RECONCILIATION_REQUIRED", "FAILED")
POSITION_STATES = ("OPEN", "HEALTHY", "WEAKENING", "EXIT_REQUIRED", "EXIT_SUBMITTED", "CLOSED", "ATTENTION")
TABLES = ("execution_mode", "execution_mode_events", "execution_runs", "execution_intents", "execution_orders", "execution_order_events", "paper_positions", "position_events",
          "execution_reconciliations", "execution_controls")
_q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731

_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS execution_mode (
        id                INTEGER PRIMARY KEY CHECK (id = 1),
        mode              TEXT NOT NULL CHECK (mode IN ({_q(MODES)})),
        changed_at        TEXT NOT NULL,
        changed_by        TEXT NOT NULL,
        reason            TEXT,
        environment_json  TEXT NOT NULL DEFAULT '{{}}'
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS execution_mode_no_delete BEFORE DELETE ON execution_mode BEGIN SELECT RAISE(ABORT, 'the execution mode row is kept'); END;",
    """
    CREATE TABLE IF NOT EXISTS execution_mode_events (
        event_id          TEXT PRIMARY KEY,
        at                TEXT NOT NULL,
        from_mode         TEXT,
        to_mode           TEXT NOT NULL,
        actor             TEXT NOT NULL,
        reason            TEXT,
        environment_json  TEXT NOT NULL DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_runs (
        run_id        TEXT PRIMARY KEY,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        trigger       TEXT NOT NULL,
        mode          TEXT NOT NULL,
        result        TEXT NOT NULL,
        summary_json  TEXT NOT NULL DEFAULT '{}',
        error         TEXT,
        owner         TEXT
    );
    """,
    f"""
    CREATE TABLE IF NOT EXISTS execution_intents (
        execution_id        TEXT PRIMARY KEY,
        intent_type         TEXT NOT NULL CHECK (intent_type IN ('ENTRY', 'EXIT')),
        symbol              TEXT NOT NULL,
        side                TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        qty                 INTEGER NOT NULL CHECK (qty >= 1),
        client_order_id     TEXT NOT NULL UNIQUE,
        proposal_id         TEXT,
        setup_id            TEXT,
        position_id         TEXT,
        exit_cycle          INTEGER NOT NULL DEFAULT 0,
        exit_reason         TEXT,
        reference_price     TEXT,
        invalidation_price  TEXT,
        risk_budget         TEXT,
        proposal_qty        INTEGER,
        state               TEXT NOT NULL CHECK (state IN ({_q(INTENT_STATES)})),
        broker_order_id     TEXT,
        broker_status       TEXT,
        filled_qty          INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
        filled_avg_price    TEXT,
        attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 1),
        lookup_count        INTEGER NOT NULL DEFAULT 0,
        last_lookup_at      TEXT,
        failure_code        TEXT,
        failure_text        TEXT,
        outcome_class       TEXT,
        created_at          TEXT NOT NULL,
        submitted_at        TEXT,
        resolved_at         TEXT,
        updated_at          TEXT NOT NULL,
        created_by          TEXT NOT NULL DEFAULT 'system',
        proposal_json       TEXT NOT NULL DEFAULT '{{}}',
        environment_json    TEXT NOT NULL DEFAULT '{{}}'
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_execution_intents_symbol_state ON execution_intents (symbol, state);",
    "CREATE TRIGGER IF NOT EXISTS execution_intents_no_delete BEFORE DELETE ON execution_intents BEGIN SELECT RAISE(ABORT, 'execution intents are never deleted'); END;",
    """
    CREATE TRIGGER IF NOT EXISTS execution_intents_identity_immutable BEFORE UPDATE ON execution_intents
    WHEN NEW.execution_id != OLD.execution_id OR NEW.intent_type != OLD.intent_type OR NEW.symbol != OLD.symbol OR NEW.side != OLD.side OR NEW.qty != OLD.qty
         OR NEW.client_order_id != OLD.client_order_id OR IFNULL(NEW.proposal_id, '') != IFNULL(OLD.proposal_id, '') OR IFNULL(NEW.setup_id, '') != IFNULL(OLD.setup_id, '')
         OR IFNULL(NEW.position_id, '') != IFNULL(OLD.position_id, '') OR NEW.exit_cycle != OLD.exit_cycle OR NEW.created_at != OLD.created_at
         OR NEW.attempt_count < OLD.attempt_count OR (OLD.broker_order_id IS NOT NULL AND IFNULL(NEW.broker_order_id, '') != OLD.broker_order_id)
    BEGIN SELECT RAISE(ABORT, 'execution intent identity, attempt count and broker order id are immutable'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_orders (
        broker_order_id    TEXT PRIMARY KEY,
        client_order_id    TEXT NOT NULL,
        execution_id       TEXT,
        symbol             TEXT NOT NULL,
        side               TEXT NOT NULL,
        qty                TEXT,
        status             TEXT,
        filled_qty         INTEGER NOT NULL DEFAULT 0,
        filled_avg_price   TEXT,
        submitted_at       TEXT,
        broker_updated_at  TEXT,
        first_seen_at      TEXT NOT NULL,
        last_seen_at       TEXT NOT NULL,
        raw_json           TEXT NOT NULL DEFAULT '{}'
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS execution_orders_no_delete BEFORE DELETE ON execution_orders BEGIN SELECT RAISE(ABORT, 'broker order views are never deleted'); END;",
    """
    CREATE TABLE IF NOT EXISTS execution_order_events (
        event_id          TEXT PRIMARY KEY,
        broker_order_id   TEXT NOT NULL,
        execution_id      TEXT,
        at                TEXT NOT NULL,
        status            TEXT,
        filled_qty        INTEGER NOT NULL DEFAULT 0,
        filled_avg_price  TEXT,
        source            TEXT NOT NULL,
        raw_json          TEXT NOT NULL DEFAULT '{}'
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_execution_order_events_order ON execution_order_events (broker_order_id, at);",
    f"""
    CREATE TABLE IF NOT EXISTS paper_positions (
        position_id          TEXT PRIMARY KEY,
        execution_id         TEXT NOT NULL UNIQUE,
        proposal_id          TEXT,
        setup_id             TEXT,
        symbol               TEXT NOT NULL,
        side                 TEXT NOT NULL DEFAULT 'LONG',
        quantity             INTEGER NOT NULL CHECK (quantity >= 0),
        entry_qty            INTEGER NOT NULL CHECK (entry_qty >= 1),
        average_entry_price  TEXT NOT NULL,
        opened_at            TEXT NOT NULL,
        invalidation_price   TEXT,
        risk_budget          TEXT,
        source               TEXT NOT NULL DEFAULT 'BROKER_FILLS',
        broker               TEXT NOT NULL DEFAULT '',
        state                TEXT NOT NULL CHECK (state IN ({_q(POSITION_STATES)})),
        demand_low_streak    INTEGER NOT NULL DEFAULT 0,
        last_demand_eval_at  TEXT,
        sessions_held        INTEGER NOT NULL DEFAULT 0,
        weakening_reason     TEXT,
        exit_reason          TEXT,
        exit_cycle           INTEGER NOT NULL DEFAULT 0,
        exit_requested_at    TEXT,
        exit_qty             INTEGER NOT NULL DEFAULT 0,
        average_exit_price   TEXT,
        realized_pnl         TEXT,
        last_price           TEXT,
        last_price_at        TEXT,
        attention_reason     TEXT,
        closed_at            TEXT,
        updated_at           TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_symbol_state ON paper_positions (symbol, state);",
    "CREATE TRIGGER IF NOT EXISTS paper_positions_no_delete BEFORE DELETE ON paper_positions BEGIN SELECT RAISE(ABORT, 'paper positions are never deleted'); END;",
    """
    CREATE TRIGGER IF NOT EXISTS paper_positions_identity_immutable BEFORE UPDATE ON paper_positions
    WHEN NEW.position_id != OLD.position_id OR NEW.execution_id != OLD.execution_id OR IFNULL(NEW.proposal_id, '') != IFNULL(OLD.proposal_id, '')
         OR IFNULL(NEW.setup_id, '') != IFNULL(OLD.setup_id, '') OR NEW.symbol != OLD.symbol OR NEW.side != OLD.side OR NEW.opened_at != OLD.opened_at
         OR NEW.entry_qty < OLD.entry_qty OR (OLD.state = 'CLOSED' AND NEW.state != 'CLOSED')
    BEGIN SELECT RAISE(ABORT, 'paper position identity is immutable and CLOSED is final'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS position_events (
        event_id     TEXT PRIMARY KEY,
        position_id  TEXT NOT NULL,
        at           TEXT NOT NULL,
        kind         TEXT NOT NULL,
        from_state   TEXT,
        to_state     TEXT,
        reason       TEXT,
        detail_json  TEXT NOT NULL DEFAULT '{}'
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_position_events_position ON position_events (position_id, at);",
    """
    CREATE TABLE IF NOT EXISTS execution_reconciliations (
        reconciliation_id  TEXT PRIMARY KEY,
        run_id             TEXT,
        at                 TEXT NOT NULL,
        trigger            TEXT NOT NULL,
        kind               TEXT NOT NULL,
        symbol             TEXT,
        blocking           INTEGER NOT NULL DEFAULT 1 CHECK (blocking IN (0, 1)),
        execution_id       TEXT,
        position_id        TEXT,
        broker_order_id    TEXT,
        detail_json        TEXT NOT NULL DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_controls (
        control_id         TEXT PRIMARY KEY,
        last_evaluated_at  TEXT NOT NULL,
        last_fired_at      TEXT,
        evaluation_count   INTEGER NOT NULL DEFAULT 0,
        fire_count         INTEGER NOT NULL DEFAULT 0,
        last_result        TEXT NOT NULL,
        last_reason        TEXT,
        last_input_json    TEXT NOT NULL DEFAULT '{}',
        last_error         TEXT,
        updated_at         TEXT NOT NULL
    );
    """,
    "CREATE TRIGGER IF NOT EXISTS execution_controls_no_delete BEFORE DELETE ON execution_controls BEGIN SELECT RAISE(ABORT, 'execution control rows are kept'); END;",
]

# append-only tables: no UPDATE, no DELETE, and no INSERT OR REPLACE of an existing key
for _t, _key in (("execution_mode_events", "event_id"), ("execution_runs", "run_id"), ("execution_order_events", "event_id"), ("position_events", "event_id"),
                 ("execution_reconciliations", "reconciliation_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} BEGIN SELECT RAISE(ABORT, '{_t} rows are append-only'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_key} = NEW.{_key}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")
for _t, _key in (("execution_intents", "execution_id"), ("paper_positions", "position_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_key} = NEW.{_key}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_execution_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
