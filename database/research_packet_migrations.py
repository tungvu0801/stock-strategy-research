"""
database/research_packet_migrations.py — Stage 5.4 EVIDENCE PACKET persistence (additive). Idempotent; four NEW tables only; no other table is
touched and PRAGMA user_version is left alone. Research records only — nothing here is a configuration anything acts on.
Completed packets are immutable: every table carries no-update / no-delete triggers and a packet is written in one transaction.
"""
import sqlite3

TABLES = ("evidence_packets", "evidence_items", "evidence_source_fetches", "evidence_explanations")
PACKET_STATUSES = ("COMPLETED", "FAILED")
FAILURE_CODES = ("INVALID_TICKER", "NO_MARKET_DATA", "NETWORK_DISABLED", "INTERNAL_ERROR")
CATEGORIES = ("MARKET_WIDE", "COMPANY_SPECIFIC", "INDUSTRY_SUPPLY_CHAIN", "COMPANY_CONFIRMATION", "MARKET_CONFIRMATION")
EPISTEMIC = ("FACT", "INFERENCE", "UNCONFIRMED")
DIRECTIONS = ("UP", "DOWN", "NEUTRAL", "NA")
RUNG_STATUSES = ("SUPPORTED", "PARTIAL", "NOT_SUPPORTED", "NOT_AVAILABLE")
STRENGTHS = ("WEAK", "MODERATE", "STRONG")
SOURCE_TYPES = ("SEC_FILING", "IR_RELEASE", "ALPACA_NEWS", "MANUAL_URL", "MANUAL_TEXT", "MARKET_DATA", "EVENT")
KINDS = ("FILING", "EARNINGS", "GUIDANCE", "HEADLINE", "PAGE", "METRIC", "EVENT", "TEXT")
FETCH_STATUSES = ("OK", "TIMEOUT", "HTTP_ERROR", "ACCESS_DENIED", "HOST_NOT_ALLOWED", "HOST_DENIED", "TOO_LARGE", "PARSE_FAILED", "EMPTY",
                  "REDIRECT_BLOCKED", "DISABLED")
EXPLANATION_STATUSES = ("COMPLETED", "FAILED", "WITHHELD")

_ID = "CHECK (length({c}) = 32 AND {c} NOT GLOB '*[^0-9a-f]*')"
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_JSON = "CHECK (json_valid({c}) AND length({c}) <= {n})"
_INT0 = "CHECK (typeof({c}) = 'integer' AND {c} >= 0)"
_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731


def _opt(check: str, c: str, **kw) -> str:
    inner = check.format(c=c, **kw)[len("CHECK ("):-1]
    return f"CHECK ({c} IS NULL OR ({inner}))"


def _no_update_delete(table: str, prefix: str):
    return [f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_update BEFORE UPDATE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_delete BEFORE DELETE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are never deleted'); END"""]


_REF = "TEXT NOT NULL REFERENCES evidence_packets(packet_id) " + _ID.format(c="packet_id")
_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS evidence_packets (
        packet_id          TEXT PRIMARY KEY {_ID.format(c="packet_id")},
        packet_hash        TEXT NOT NULL UNIQUE {_H64.format(c="packet_hash")},
        ticker             TEXT NOT NULL CHECK (length(ticker) BETWEEN 1 AND 10 AND ticker NOT GLOB '*[^A-Z.]*'),
        as_of_date         TEXT NOT NULL CHECK (length(as_of_date) = 10),
        created_at         TEXT NOT NULL,
        lookback_days      INTEGER NOT NULL CHECK (lookback_days BETWEEN 10 AND 365),
        engine_version     TEXT NOT NULL,
        rules_version      TEXT NOT NULL,
        status             TEXT NOT NULL CHECK (status IN ({_Q(PACKET_STATUSES)})),
        failure_code       TEXT CHECK (failure_code IS NULL OR failure_code IN ({_Q(FAILURE_CODES)})),
        failure_detail     TEXT,
        request_json       TEXT NOT NULL {_JSON.format(c="request_json", n=16384)},
        cause_json         TEXT NOT NULL {_JSON.format(c="cause_json", n=16384)},
        ladder_json        TEXT NOT NULL {_JSON.format(c="ladder_json", n=65536)},
        strength           TEXT CHECK (strength IS NULL OR strength IN ({_Q(STRENGTHS)})),
        strength_points    INTEGER NOT NULL {_INT0.format(c="strength_points")},
        strength_text      TEXT NOT NULL,
        missing_json       TEXT NOT NULL {_JSON.format(c="missing_json", n=16384)},
        next_events_json   TEXT NOT NULL {_JSON.format(c="next_events_json", n=16384)},
        n_items            INTEGER NOT NULL {_INT0.format(c="n_items")},
        n_fetches          INTEGER NOT NULL {_INT0.format(c="n_fetches")},
        n_failed_fetches   INTEGER NOT NULL {_INT0.format(c="n_failed_fetches")},
        sec_requests       INTEGER NOT NULL {_INT0.format(c="sec_requests")},
        runtime_s          TEXT NOT NULL,
        universe_note      TEXT NOT NULL,
        CHECK ((status = 'COMPLETED' AND failure_code IS NULL) OR (status = 'FAILED' AND failure_code IS NOT NULL))
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS evidence_items (
        item_id            TEXT PRIMARY KEY {_ID.format(c="item_id")},
        packet_id          {_REF},
        position           INTEGER NOT NULL {_INT0.format(c="position")},
        ticker             TEXT NOT NULL,
        subject_ticker     TEXT NOT NULL,
        source_type        TEXT NOT NULL CHECK (source_type IN ({_Q(SOURCE_TYPES)})),
        source_name        TEXT NOT NULL,
        source_url         TEXT,
        source_identifier  TEXT,
        published_at       TEXT,
        retrieved_at       TEXT NOT NULL,
        evidence_date      TEXT NOT NULL CHECK (length(evidence_date) = 10),
        category           TEXT NOT NULL CHECK (category IN ({_Q(CATEGORIES)})),
        kind               TEXT NOT NULL CHECK (kind IN ({_Q(KINDS)})),
        text               TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2100),
        value_json         TEXT NOT NULL {_JSON.format(c="value_json", n=4096)},
        epistemic          TEXT NOT NULL CHECK (epistemic IN ({_Q(EPISTEMIC)})),
        direction          TEXT NOT NULL CHECK (direction IN ({_Q(DIRECTIONS)})),
        rule_match         TEXT NOT NULL,
        ladder_rung        INTEGER CHECK (ladder_rung IS NULL OR ladder_rung BETWEEN 1 AND 6),
        ladder_status      TEXT CHECK (ladder_status IS NULL OR ladder_status IN ({_Q(RUNG_STATUSES)})),
        source_tier        INTEGER NOT NULL CHECK (source_tier BETWEEN 1 AND 5),
        contradiction      INTEGER NOT NULL CHECK (contradiction IN (0, 1)),
        contradicts_item_id TEXT {_opt(_ID, "contradicts_item_id")},
        derived_from_json  TEXT NOT NULL {_JSON.format(c="derived_from_json", n=4096)},
        source_hash        TEXT {_opt(_H64, "source_hash")},
        fetch_id           TEXT {_opt(_ID, "fetch_id")},
        role               TEXT NOT NULL,
        date_source        TEXT NOT NULL CHECK (date_source IN ('source', 'retrieval')),
        claim_key          TEXT NOT NULL DEFAULT '',
        rule_offset        INTEGER,
        UNIQUE (packet_id, position)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS evidence_source_fetches (
        fetch_id           TEXT PRIMARY KEY {_ID.format(c="fetch_id")},
        packet_id          {_REF},
        position           INTEGER NOT NULL {_INT0.format(c="position")},
        adapter            TEXT NOT NULL,
        purpose            TEXT NOT NULL,
        url                TEXT NOT NULL,
        host               TEXT NOT NULL,
        status             TEXT NOT NULL CHECK (status IN ({_Q(FETCH_STATUSES)})),
        http_status        INTEGER,
        bytes              INTEGER NOT NULL {_INT0.format(c="bytes")},
        content_hash       TEXT {_opt(_H64, "content_hash")},
        started_at         TEXT NOT NULL,
        duration_ms        INTEGER NOT NULL {_INT0.format(c="duration_ms")},
        error              TEXT NOT NULL,
        UNIQUE (packet_id, position)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS evidence_explanations (
        explanation_id     TEXT PRIMARY KEY {_ID.format(c="explanation_id")},
        packet_id          {_REF},
        packet_hash        TEXT NOT NULL {_H64.format(c="packet_hash")},
        model              TEXT NOT NULL,
        prompt_version     TEXT NOT NULL,
        input_hash         TEXT NOT NULL {_H64.format(c="input_hash")},
        status             TEXT NOT NULL CHECK (status IN ({_Q(EXPLANATION_STATUSES)})),
        text               TEXT NOT NULL,
        guard_failures_json TEXT NOT NULL {_JSON.format(c="guard_failures_json", n=8192)},
        input_tokens       INTEGER {_opt(_INT0, "input_tokens")},
        output_tokens      INTEGER {_opt(_INT0, "output_tokens")},
        estimated_cost_usd TEXT,
        created_at         TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_evidence_packets_ticker ON evidence_packets(ticker, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_items_packet ON evidence_items(packet_id, position)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_fetches_packet ON evidence_source_fetches(packet_id, position)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_explanations_packet ON evidence_explanations(packet_id)",
    *_no_update_delete("evidence_packets", "evp"),
    *_no_update_delete("evidence_items", "evi"),
    *_no_update_delete("evidence_source_fetches", "evf"),
    *_no_update_delete("evidence_explanations", "eve"),
]


# Columns added after the first live packet (validation hardening); ALTER is additive and idempotent, UPDATE/DELETE stay blocked.
_ADDED_COLUMNS = (("evidence_items", "claim_key", "TEXT NOT NULL DEFAULT ''"), ("evidence_items", "rule_offset", "INTEGER"))


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def run_research_packet_migrations(conn: sqlite3.Connection) -> None:
    """Additive and idempotent. Creates only the four Stage 5.4 tables, their indexes and immutability triggers, and adds later columns."""
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    for table, column, ddl in _ADDED_COLUMNS:
        _add_column_if_missing(conn, table, column, ddl)
    conn.commit()
