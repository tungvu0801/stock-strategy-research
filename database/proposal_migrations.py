"""
database/proposal_migrations.py — Stage 5.9 TRADE PROPOSAL / REVIEW LAYER (additive). Idempotent; safe on any database state. Only NEW
tables; every earlier table is untouched and PRAGMA user_version is left alone (like every other Stage 3 / 4 / 5 migration).

  * trade_proposals          one row per generated review proposal (BLOCKED or REVIEWABLE). Append-only: only review_state /
                             reviewed_at ever change; no delete; no replace. NOT_ELIGIBLE is computed and never stored.
  * proposal_controls        append-only, immutable: one row per control per generated proposal — what the control saw and returned.
  * proposal_control_status  one upserted row per control: last evaluation, counts, last block — liveness from data, not logs.
Nothing here stores an order, a recommendation, a target price, an expected return or a probability.
"""
import sqlite3

STATUSES = ("BLOCKED", "REVIEWABLE")
REVIEW_STATES = ("UNREVIEWED", "VIEWED", "DISMISSED")
RESULTS = ("PASS", "BLOCK", "WARN", "UNKNOWN")
TABLES = ("trade_proposals", "proposal_controls", "proposal_control_status")
_q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731

_FIXED = ("proposal_id", "setup_id", "symbol", "setup_type", "side_context", "status", "created_at", "expires_at", "price_mode", "reference_price", "price_source",
          "price_timestamp", "invalidation_price", "stop_rule", "structural_low", "atr", "atr_period", "risk_per_share", "risk_pct", "equity", "equity_source",
          "risk_fraction", "risk_budget", "base_qty", "position_cap_qty", "buying_power_qty", "final_qty", "max_notional", "max_position_pct", "cap_source",
          "existing_position_qty", "existing_position_notional", "existing_avg_cost", "proposed_notional", "blockers_json", "warnings_json", "inputs_json",
          "controls_json", "ppe_snapshot_json", "demand_snapshot_json", "evidence_snapshot_json", "setup_snapshot_json", "trigger", "owner", "source", "mode", "fingerprint")

_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS trade_proposals (
        proposal_id                 TEXT PRIMARY KEY CHECK (length(proposal_id) = 32),
        setup_id                    TEXT NOT NULL CHECK (length(setup_id) = 32),
        symbol                      TEXT NOT NULL,
        setup_type                  TEXT NOT NULL,
        side_context                TEXT NOT NULL CHECK (side_context = 'LONG_REVIEW'),
        status                      TEXT NOT NULL CHECK (status IN ({_q(STATUSES)})),
        created_at                  TEXT NOT NULL,
        expires_at                  TEXT NOT NULL,
        price_mode                  TEXT,
        reference_price             TEXT,
        price_source                TEXT,
        price_timestamp             TEXT,
        invalidation_price          TEXT,
        stop_rule                   TEXT,
        structural_low              TEXT,
        atr                         TEXT,
        atr_period                  INTEGER NOT NULL DEFAULT 0,
        risk_per_share              TEXT,
        risk_pct                    TEXT,
        equity                      TEXT,
        equity_source               TEXT NOT NULL,
        risk_fraction               TEXT,
        risk_budget                 TEXT,
        base_qty                    INTEGER CHECK (base_qty IS NULL OR base_qty >= 0),
        position_cap_qty            INTEGER CHECK (position_cap_qty IS NULL OR position_cap_qty >= 0),
        buying_power_qty            INTEGER CHECK (buying_power_qty IS NULL OR buying_power_qty >= 0),
        final_qty                   INTEGER CHECK (final_qty IS NULL OR final_qty >= 0),
        max_notional                TEXT,
        max_position_pct            TEXT,
        cap_source                  TEXT,
        existing_position_qty       TEXT,
        existing_position_notional  TEXT,
        existing_avg_cost           TEXT,
        proposed_notional           TEXT,
        blockers_json               TEXT NOT NULL,
        warnings_json               TEXT NOT NULL,
        inputs_json                 TEXT NOT NULL,
        controls_json               TEXT NOT NULL,
        ppe_snapshot_json           TEXT NOT NULL,
        demand_snapshot_json        TEXT NOT NULL,
        evidence_snapshot_json      TEXT NOT NULL,
        setup_snapshot_json         TEXT NOT NULL,
        review_state                TEXT NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state IN ({_q(REVIEW_STATES)})),
        reviewed_at                 TEXT,
        trigger                     TEXT NOT NULL,
        owner                       TEXT,
        source                      TEXT,
        mode                        TEXT,
        fingerprint                 TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_trade_proposals_symbol ON trade_proposals (symbol, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS ix_trade_proposals_setup ON trade_proposals (setup_id, created_at DESC);",
    f"""
    CREATE TRIGGER IF NOT EXISTS trade_proposals_content_fixed BEFORE UPDATE OF {", ".join(_FIXED)} ON trade_proposals
    BEGIN SELECT RAISE(ABORT, 'trade proposals are immutable (only the review state changes)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trade_proposals_no_delete BEFORE DELETE ON trade_proposals
    BEGIN SELECT RAISE(ABORT, 'trade proposals are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS proposal_controls (
        control_eval_id  TEXT PRIMARY KEY CHECK (length(control_eval_id) = 32),
        proposal_id      TEXT NOT NULL REFERENCES trade_proposals(proposal_id),
        control_id       TEXT NOT NULL,
        symbol           TEXT NOT NULL,
        evaluated_at     TEXT NOT NULL,
        result           TEXT NOT NULL CHECK (result IN ({_q(RESULTS)})),
        reason           TEXT NOT NULL,
        input_json       TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_proposal_controls_control ON proposal_controls (control_id, evaluated_at DESC);",
    "CREATE INDEX IF NOT EXISTS ix_proposal_controls_proposal ON proposal_controls (proposal_id);",
    """
    CREATE TRIGGER IF NOT EXISTS proposal_controls_no_update BEFORE UPDATE ON proposal_controls
    BEGIN SELECT RAISE(ABORT, 'proposal control evaluations are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS proposal_controls_no_delete BEFORE DELETE ON proposal_controls
    BEGIN SELECT RAISE(ABORT, 'proposal control evaluations are kept'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS proposal_control_status (
        control_id        TEXT PRIMARY KEY,
        last_evaluated_at TEXT NOT NULL,
        last_result       TEXT NOT NULL CHECK (last_result IN ({_q(RESULTS)})),
        last_reason       TEXT,
        last_symbol       TEXT,
        last_proposal_id  TEXT,
        evaluation_count  INTEGER NOT NULL DEFAULT 0,
        block_count       INTEGER NOT NULL DEFAULT 0,
        last_blocked_at   TEXT,
        last_input_json   TEXT,
        updated_at        TEXT NOT NULL
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS proposal_control_status_no_delete BEFORE DELETE ON proposal_control_status
    BEGIN SELECT RAISE(ABORT, 'proposal control status rows are kept'); END;
    """,
]

# an INSERT OR REPLACE would delete the existing row without firing the delete triggers: refuse any second row instead
for _t, _where in (("trade_proposals", "proposal_id = NEW.proposal_id"), ("proposal_controls", "control_eval_id = NEW.control_eval_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} WHERE {_where}) "
                       f"BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_proposal_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
