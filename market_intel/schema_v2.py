"""
market_intel/schema_v2.py - Stage 7.3.1: schema v2 of the market-intelligence store, its verification and the EXPLICIT copy migration
(DESIGN_731_MARKET_INTEL_SCHEMA_V2.md sections 1, 2, 3 and 5). The Stage 7.1 migrations module is unchanged: it still knows only v1, so
the 7.1 store refuses a v2 file (SchemaTooNew) and never upgrades anything.

v2 = migration 1 (Stage 7.1, unchanged) + migration 2:
* mi_datasets rebuilt so job_kind also admits COLLECT. The rows (with their rowids) are carried through a temporary table and copied back
  into a table created from the code's own DDL: identical columns and values, so every record hash and every seal digest is unchanged;
* mi_collect_authorization_uses - the one claim a COLLECT dataset needs; purpose CHECK admits OFFLINE_VERIFICATION only (no live claim
  can be recorded, by any code or raw SQL); triggers: a COLLECT dataset needs its own claim, a claim never names another kind, a claim
  never comes after its dataset; COLLECT values, absences and members carry the provenance of a fetch of their own dataset;
* news intelligence: entity maps, runs (CLUSTERS / MENTIONS), run inputs, clusters, cluster members and mentions. A run is CLOSED: its rows
  precede it in one transaction (deferred keys) and nothing joins it later; the run row is refused when its counts disagree, when an input
  was not known at as_of (LOOK_AHEAD), or when a mention run's map was recorded after as_of.

migrate_copy(source, target, anchors=...) never writes the source: backup to <target>.partial, verify the copy as v1 (schema, integrity,
foreign keys, the 7.1 verify_integrity VERIFIED against the anchors), migrate it in one transaction, verify it as v2 (schema, integrity,
foreign keys, every v1 table's row digest unchanged, the v2 verify_integrity VERIFIED), then give it the target's name with a hard link
(a new name or nothing: no existing file is ever replaced) and publish the report the same way, last. It refuses the production paths
(the store's default file and anything under the project's data/), an existing target or report, a leftover .partial (an interrupted
earlier run), a file system without hard links and a source that is not a quiescent, verified v1 store.

    python -m market_intel.schema_v2 status  --file <db>
    python -m market_intel.schema_v2 migrate --source <v1.db> --target <new.db> --archive-root <raw archive root>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from market_intel import migrations as MG
from market_intel.migrations import SchemaMismatch, SchemaTooNew

SCHEMA_VERSION = 2
MIGRATION_2_NAME = "schema v2: COLLECT job kind, collect authorizations, news clusters and mentions"
COLLECT = "COLLECT"
ACCEPTED_PURPOSES = ("OFFLINE_VERIFICATION",)                        # the schema admits no other claim purpose (no LIVE)


class SchemaUpgradeRequired(SchemaMismatch):
    """A v1 file opened by v2 code: migrate a COPY explicitly (python -m market_intel.schema_v2 migrate ...); nothing upgrades implicitly."""


class MigrationRefused(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# ------------------------------------------------------------------------------------------------ the v2 DDL
_V1_HEAD = "CREATE TABLE IF NOT EXISTS mi_datasets ("
_OLD_CHECK = "CHECK (job_kind IN ('BACKFILL', 'IMPORT', 'FIXTURE'))"
_NEW_CHECK = "CHECK (job_kind IN ('BACKFILL', 'IMPORT', 'FIXTURE', 'COLLECT'))"
_V1_DATASETS = next(s for s in MG.V1 if s.lstrip().startswith(_V1_HEAD))
DATASETS_V2_DDL = _V1_DATASETS.strip().replace(_V1_HEAD, "CREATE TABLE mi_datasets (", 1).replace(_OLD_CHECK, _NEW_CHECK, 1)
if DATASETS_V2_DDL.count(_NEW_CHECK) != 1 or _OLD_CHECK in DATASETS_V2_DDL:
    raise RuntimeError("the Stage 7.1 mi_datasets DDL is not the one schema v2 rebuilds")
DATASET_COLUMNS = ("dataset_id", "job_kind", "kind", "provider", "feed", "adjustment", "timeframe", "symbols_json", "start_date", "end_date",
                   "request_json", "plan_sha256", "approval_ref", "created_at", "code_version")
_COLS = ", ".join(DATASET_COLUMNS)
_TS = MG._ts
_FINAL = MG.FINAL
NEWS_TABLES = ("mi_news_entity_maps", "mi_news_runs", "mi_news_run_inputs", "mi_news_clusters", "mi_news_cluster_members", "mi_news_mentions")
V2_TABLES = ("mi_collect_authorization_uses",) + NEWS_TABLES
METHODS = ("EXCHANGE_TAG", "PROVIDER_TAG", "CASHTAG", "NAME_MATCH", "UNVERIFIED")


def _collect_provenance(table: str, *, needs_ok: bool) -> str:
    ok = "AND f.status IN ('OK', 'NOT_MODIFIED')" if needs_ok else ""
    extra = "OR NEW.backfilled != 0 OR NEW.available_at != NEW.retrieved_at" if needs_ok else ""
    return (f"CREATE TRIGGER {table}_collect_provenance BEFORE INSERT ON {table} WHEN (SELECT d.job_kind FROM mi_datasets d WHERE "
            f"d.dataset_id = NEW.dataset_id) = 'COLLECT' AND (NEW.fetch_id IS NULL {extra} OR NOT EXISTS (SELECT 1 FROM mi_fetches f WHERE "
            f"f.fetch_id = NEW.fetch_id AND f.dataset_id = NEW.dataset_id {ok})) BEGIN SELECT RAISE(ABORT, 'COLLECT_PROVENANCE: a COLLECT row names "
            f"a fetch of its own dataset, live (not backfilled) and available when retrieved'); END")


def _collect_expiry(table: str, at_col: str) -> str:
    return (f"CREATE TRIGGER {table}_collect_expiry BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM mi_collect_authorization_uses u WHERE "
            f"u.dataset_id = NEW.dataset_id AND NEW.{at_col} >= u.expires_at) BEGIN SELECT RAISE(ABORT, 'COLLECT_AUTHORIZATION_EXPIRED: a COLLECT "
            f"dataset takes no row once its authorization expired (it can still be sealed)'); END")


def _closed(table: str) -> str:
    return (f"CREATE TRIGGER {table}_run_closed BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM mi_news_runs r WHERE r.run_id = NEW.run_id) "
            f"BEGIN SELECT RAISE(ABORT, 'NEWS_RUN_CLOSED: a news run takes no row after it was recorded'); END")


_RUN_FK = "REFERENCES mi_news_runs(run_id) DEFERRABLE INITIALLY DEFERRED"
V2: Tuple[str, ...] = (
    # --- mi_datasets: COLLECT admitted; same columns, rows and rowids
    "CREATE TEMP TABLE mi_datasets_carry AS SELECT rowid AS carry_rowid, * FROM main.mi_datasets",
    "DROP TABLE main.mi_datasets",
    DATASETS_V2_DDL,
    f"INSERT INTO main.mi_datasets (rowid, {_COLS}) SELECT carry_rowid, {_COLS} FROM temp.mi_datasets_carry ORDER BY carry_rowid",
    "DROP TABLE temp.mi_datasets_carry",
    *MG._immutable("mi_datasets"),
    # --- the COLLECT claim
    f"""CREATE TABLE mi_collect_authorization_uses (
        authorization_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL UNIQUE REFERENCES mi_datasets(dataset_id) DEFERRABLE INITIALLY DEFERRED,
        purpose TEXT NOT NULL CHECK (purpose IN ('OFFLINE_VERIFICATION')), source TEXT NOT NULL, scope_key TEXT NOT NULL,
        authorization_file_sha256 TEXT NOT NULL CHECK (length(authorization_file_sha256) = 64), used_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        max_requests INTEGER NOT NULL CHECK (max_requests > 0), max_rows INTEGER NOT NULL CHECK (max_rows > 0), {_TS('used_at', 'expires_at')})""",
    """CREATE TRIGGER mi_collect_authorization_uses_first BEFORE INSERT ON mi_collect_authorization_uses WHEN EXISTS (SELECT 1 FROM mi_datasets d
        WHERE d.dataset_id = NEW.dataset_id) BEGIN SELECT RAISE(ABORT, 'COLLECT_AUTHORIZATION_AFTER_THE_FACT: a claim is made with its dataset'); END""",
    """CREATE TRIGGER mi_datasets_collect_needs_claim BEFORE INSERT ON mi_datasets WHEN NEW.job_kind = 'COLLECT' AND NOT EXISTS (SELECT 1
        FROM mi_collect_authorization_uses u WHERE u.dataset_id = NEW.dataset_id AND u.authorization_id = NEW.approval_ref)
        BEGIN SELECT RAISE(ABORT, 'COLLECT_NEEDS_AUTHORIZATION: a COLLECT dataset needs its own collect authorization claim'); END""",
    """CREATE TRIGGER mi_datasets_claim_only_collect BEFORE INSERT ON mi_datasets WHEN NEW.job_kind != 'COLLECT' AND EXISTS (SELECT 1
        FROM mi_collect_authorization_uses u WHERE u.dataset_id = NEW.dataset_id)
        BEGIN SELECT RAISE(ABORT, 'COLLECT_AUTHORIZATION_ON_OTHER_KIND: only a COLLECT dataset carries a collect authorization'); END""",
    _collect_provenance("mi_observations", needs_ok=True),
    _collect_provenance("mi_absences", needs_ok=True),
    _collect_provenance("mi_dataset_members", needs_ok=False),
    *(_collect_expiry(t, col) for t, col in (("mi_fetches", "ingested_at"), ("mi_observations", "ingested_at"), ("mi_absences", "ingested_at"),
                                             ("mi_dataset_members", "at"))),
    # --- news intelligence (append-only, point in time, closed runs)
    f"""CREATE TABLE mi_news_entity_maps (
        map_sha256 TEXT PRIMARY KEY CHECK (length(map_sha256) = 64), map_json TEXT NOT NULL, recorded_at TEXT NOT NULL, {_TS('recorded_at')})""",
    f"""CREATE TABLE mi_news_runs (
        run_id TEXT PRIMARY KEY, run_kind TEXT NOT NULL CHECK (run_kind IN ('CLUSTERS', 'MENTIONS')), algorithm TEXT NOT NULL,
        map_sha256 TEXT NOT NULL, as_of TEXT NOT NULL, computed_at TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
        input_count INTEGER NOT NULL CHECK (input_count >= 0), input_sha256 TEXT NOT NULL, output_count INTEGER NOT NULL CHECK (output_count >= 0),
        output_sha256 TEXT NOT NULL, UNIQUE (run_kind, algorithm, map_sha256, window_start, window_end, input_sha256),
        CHECK (as_of <= computed_at), CHECK (window_start < window_end),
        CHECK ((run_kind = 'CLUSTERS' AND map_sha256 = '') OR (run_kind = 'MENTIONS' AND length(map_sha256) = 64)),
        {_TS('as_of', 'computed_at', 'window_start', 'window_end')})""",
    f"""CREATE TABLE mi_news_run_inputs (
        run_id TEXT NOT NULL {_RUN_FK}, natural_key TEXT NOT NULL, obs_id TEXT NOT NULL REFERENCES mi_observations(obs_id),
        revision INTEGER NOT NULL CHECK (revision >= 1), record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64), first_seen_at TEXT NOT NULL,
        PRIMARY KEY (run_id, natural_key), {_TS('first_seen_at')})""",
    f"""CREATE TABLE mi_news_clusters (
        run_id TEXT NOT NULL {_RUN_FK}, cluster_id TEXT NOT NULL, representative_key TEXT NOT NULL, member_count INTEGER NOT NULL CHECK (member_count >= 1),
        first_seen_at TEXT NOT NULL, first_published_at TEXT, PRIMARY KEY (run_id, cluster_id),
        FOREIGN KEY (run_id, representative_key) REFERENCES mi_news_run_inputs(run_id, natural_key), {_TS('first_seen_at', 'first_published_at')})""",
    """CREATE TABLE mi_news_cluster_members (
        run_id TEXT NOT NULL, natural_key TEXT NOT NULL, cluster_id TEXT NOT NULL, representative INTEGER NOT NULL CHECK (representative IN (0, 1)),
        rule TEXT CHECK (rule IS NULL OR rule IN ('PROVIDER_ID', 'CANONICAL_URL', 'HEADLINE_HASH', 'NEAR_DUPLICATE')), PRIMARY KEY (run_id, natural_key),
        FOREIGN KEY (run_id, natural_key) REFERENCES mi_news_run_inputs(run_id, natural_key),
        FOREIGN KEY (run_id, cluster_id) REFERENCES mi_news_clusters(run_id, cluster_id) DEFERRABLE INITIALLY DEFERRED)""",
    f"""CREATE TABLE mi_news_mentions (
        run_id TEXT NOT NULL, natural_key TEXT NOT NULL, symbol TEXT NOT NULL,
        method TEXT NOT NULL CHECK (method IN ({', '.join(repr(m) for m in METHODS)})), confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
        role TEXT NOT NULL CHECK (role IN ('PRIMARY', 'MENTION', 'ROUNDUP')), in_headline INTEGER NOT NULL CHECK (in_headline IN (0, 1)),
        catalyst_eligible INTEGER NOT NULL CHECK (catalyst_eligible IN (0, 1)), PRIMARY KEY (run_id, natural_key, symbol),
        FOREIGN KEY (run_id, natural_key) REFERENCES mi_news_run_inputs(run_id, natural_key))""",
    """CREATE TRIGGER mi_news_run_inputs_match BEFORE INSERT ON mi_news_run_inputs WHEN NOT EXISTS (SELECT 1 FROM mi_observations o
        WHERE o.obs_id = NEW.obs_id AND o.natural_key = NEW.natural_key AND o.revision = NEW.revision AND o.kind = 'NEWS')
        BEGIN SELECT RAISE(ABORT, 'NEWS_INPUT_MISMATCH: a run input is a stored NEWS observation, named by its own key and revision'); END""",
    *(_closed(t) for t in ("mi_news_run_inputs", "mi_news_clusters", "mi_news_cluster_members", "mi_news_mentions")),
    """CREATE TRIGGER mi_news_runs_counts BEFORE INSERT ON mi_news_runs WHEN NEW.input_count != (SELECT COUNT(*) FROM mi_news_run_inputs i
        WHERE i.run_id = NEW.run_id) OR NEW.output_count != (CASE NEW.run_kind WHEN 'CLUSTERS' THEN (SELECT COUNT(*) FROM mi_news_clusters c
        WHERE c.run_id = NEW.run_id) ELSE (SELECT COUNT(*) FROM mi_news_mentions m WHERE m.run_id = NEW.run_id) END)
        OR (NEW.run_kind = 'CLUSTERS' AND (EXISTS (SELECT 1 FROM mi_news_mentions m WHERE m.run_id = NEW.run_id)
            OR (SELECT COUNT(*) FROM mi_news_cluster_members b WHERE b.run_id = NEW.run_id) != NEW.input_count))
        OR (NEW.run_kind = 'MENTIONS' AND EXISTS (SELECT 1 FROM mi_news_cluster_members b WHERE b.run_id = NEW.run_id))
        BEGIN SELECT RAISE(ABORT, 'NEWS_RUN_INCONSISTENT: a run row states exactly the rows recorded with it'); END""",
    """CREATE TRIGGER mi_news_runs_point_in_time BEFORE INSERT ON mi_news_runs WHEN EXISTS (SELECT 1 FROM mi_news_run_inputs i JOIN mi_observations o
        ON o.obs_id = i.obs_id WHERE i.run_id = NEW.run_id AND (o.available_at > NEW.as_of OR o.ingested_at > NEW.as_of OR i.first_seen_at > NEW.as_of))
        OR (NEW.run_kind = 'MENTIONS' AND NOT EXISTS (SELECT 1 FROM mi_news_entity_maps m WHERE m.map_sha256 = NEW.map_sha256
            AND m.recorded_at <= NEW.as_of))
        BEGIN SELECT RAISE(ABORT, 'LOOK_AHEAD: every input and the entity map are known at the run as_of'); END""",
    "CREATE INDEX mi_news_runs_lookup ON mi_news_runs (run_kind, algorithm, map_sha256, window_start, window_end, as_of, computed_at)",
    "CREATE INDEX mi_news_run_inputs_obs ON mi_news_run_inputs (obs_id)",
    "CREATE INDEX mi_news_mentions_symbol ON mi_news_mentions (symbol, run_id)",
    *[t for table in V2_TABLES for t in MG._immutable(table)],
)
MIGRATIONS: Sequence[Tuple[int, str, Tuple[str, ...]]] = tuple(MG.MIGRATIONS) + ((2, MIGRATION_2_NAME, V2),)
_EXPECTED: Dict[int, Dict[Tuple[str, str], str]] = {}


def _norm(objects: Dict[Tuple[str, str], str]) -> Dict[Tuple[str, str], str]:
    return {k: v.replace('"', "") for k, v in objects.items()}


def expected_objects(version: int) -> Dict[Tuple[str, str], str]:
    """The objects of `version` (1 or 2), built once in memory by running the same statements a real file runs."""
    if version not in _EXPECTED:
        mem = sqlite3.connect(":memory:", isolation_level=None)
        try:
            for v, _name, stmts in MIGRATIONS:
                if v <= version:
                    for st in stmts:
                        mem.execute(st)
            _EXPECTED[version] = _norm(MG._objects(mem))
        finally:
            mem.close()
    return _EXPECTED[version]


def verify(conn: sqlite3.Connection) -> int:
    """Read-only: 2 for a verified v2 file, 0 for an empty file. A v1 file is SchemaUpgradeRequired; above 2 SchemaTooNew; anything else that
    differs from this code (recorded migrations, DDL hashes, live objects, a reset user_version) SchemaMismatch."""
    v = MG.current_version(conn)
    live = _norm(MG._objects(conn))
    if v == 0:
        if live:
            raise SchemaMismatch("a populated file with user_version 0: refused, never read as empty")
        return 0
    if v > SCHEMA_VERSION:
        raise SchemaTooNew(f"schema version {v}; this code knows up to {SCHEMA_VERSION}")
    recorded = {r[0]: r[1] for r in conn.execute("SELECT version, ddl_sha256 FROM mi_schema_migrations")}
    want = {ver: MG.ddl_sha256(stmts) for ver, _n, stmts in MIGRATIONS if ver <= v}
    if recorded != want:
        raise SchemaMismatch(f"recorded migrations {sorted(recorded)} do not match this code's for version {v}")
    if live != expected_objects(v):
        want_o = expected_objects(v)
        raise SchemaMismatch(f"schema objects differ: missing {sorted(set(want_o) - set(live))[:4]}, extra {sorted(set(live) - set(want_o))[:4]}, "
                             f"altered {sorted(k for k in want_o if k in live and live[k] != want_o[k])[:4]}")
    if v == 1:
        raise SchemaUpgradeRequired("a v1 store: migrate a copy explicitly (python -m market_intel.schema_v2 migrate --source ... --target ...)")
    return v


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def apply_v2(conn: sqlite3.Connection, now: datetime) -> None:
    """Migration 2 on a connection already inside a transaction (the caller commits or rolls back)."""
    for st in V2:
        conn.execute(st)
    conn.execute("INSERT INTO mi_schema_migrations (version, name, ddl_sha256, applied_at) VALUES (2, ?, ?, ?)",
                 (MIGRATION_2_NAME, MG.ddl_sha256(V2), _stamp(now)))
    conn.execute("PRAGMA user_version = 2")


def create(conn: sqlite3.Connection, now: datetime) -> None:
    """A FRESH v2 file (empty), inside the caller's transaction: migration 1 then migration 2, exactly as a migrated file gets them."""
    for st in MG.V1:
        conn.execute(st)
    conn.execute("INSERT INTO mi_schema_migrations (version, name, ddl_sha256, applied_at) VALUES (1, ?, ?, ?)",
                 (MG.MIGRATIONS[0][1], MG.ddl_sha256(MG.V1), _stamp(now)))
    conn.execute("PRAGMA user_version = 1")
    apply_v2(conn, now)


# ------------------------------------------------------------------------------------------------ the explicit copy migration
V1_TABLES = ("mi_schema_migrations", "mi_datasets", "mi_dataset_events", "mi_dataset_members", "mi_approval_uses", "mi_fetches", "mi_observations",
             "mi_absences", "mi_budget_events", "mi_quality_audits")


def table_digests(conn: sqlite3.Connection, tables: Sequence[str] = V1_TABLES) -> Dict[str, dict]:
    """{table: {rows, sha256}} - every row, every column, in rowid order (the same rows hash the same, whatever wrote the file)."""
    from market_intel.store import canonical
    out = {}
    for t in tables:
        conn.row_factory = sqlite3.Row
        h, n = hashlib.sha256(), 0
        for r in conn.execute(f"SELECT rowid AS _rowid_, * FROM {t} ORDER BY rowid"):
            h.update(canonical({k: r[k] for k in r.keys()}).encode("utf-8") + b"\n")
            n += 1
        out[t] = {"rows": n, "sha256": h.hexdigest()}
    return out


def _file_sha256(p: Path) -> Optional[str]:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def production_paths() -> Tuple[Path, Path]:
    import config
    from market_intel.store import default_path
    return default_path().resolve(), (Path(config.PROJECT_ROOT) / "data").resolve()


def _is_production(p: Path) -> bool:
    db, data = production_paths()
    rp = p.resolve()
    return rp == db or data in rp.parents or rp == data


def _checks(conn: sqlite3.Connection, prefix: str) -> None:
    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        raise MigrationRefused(f"{prefix}_INTEGRITY_CHECK", str(ic)[:200])
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise MigrationRefused(f"{prefix}_FOREIGN_KEYS", f"{len(fk)} violation(s)")


def _verdict(v: dict) -> dict:
    keys = [k for k in v if isinstance(v[k], list) and v[k] and k not in ("unanchored_datasets", "unsealed_datasets")]
    return {"state": v.get("state"), "store": v.get("store"), "findings": keys, "unsealed": v.get("unsealed_datasets", []),
            "unanchored": v.get("unanchored_datasets", [])}


PROMOTION = "os.link: a new name or nothing, never a replaced file"


def migrate_copy(source, target, *, anchors: Optional[Dict[str, dict]], clock: Callable[[], datetime] = None) -> dict:
    """The ONLY way a v1 store becomes v2 (see the module docstring). Returns the report, also published as <target>.migration.json as
    the very last step: a target without its report is not a completed migration (DESIGN_731 section 1). Raises MigrationRefused with
    a code; a refusal before promotion discards this run's own .partial copy, while a refusal during promotion, a crash or an
    interrupt leaves every file in place (fail closed: the next run refuses until an operator removes them)."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    source, target = Path(source), Path(target)
    partial = target.with_name(target.name + ".partial")
    report_path = target.with_name(target.name + ".migration.json")
    report_partial = report_path.with_name(report_path.name + ".partial")
    if anchors is None:
        raise MigrationRefused("NO_ANCHORS", "the raw archive's anchors are required (every seal is verified against them)")
    if _is_production(source):
        raise MigrationRefused("SOURCE_IS_PRODUCTION", "Stage 7.3.1 never reads a production store for a migration")
    if _is_production(target) or _is_production(partial):
        raise MigrationRefused("TARGET_IS_PRODUCTION", "a migration target is a disposable copy outside the project's data/")
    if not source.is_file():
        raise MigrationRefused("SOURCE_MISSING", str(source))
    if source.resolve() == target.resolve():
        raise MigrationRefused("SAME_FILE")
    left = [str(x) for x in (partial, report_partial) if x.exists()]
    if left:
        raise MigrationRefused("INTERRUPTED_MIGRATION_PRESENT", f"{', '.join(left)} left from an interrupted run: inspect and remove first")
    if target.exists():
        raise MigrationRefused("TARGET_EXISTS", str(target))
    if report_path.exists():
        raise MigrationRefused("REPORT_EXISTS", str(report_path))
    started = clock()
    source_before = _file_sha256(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(str(partial), os.O_CREAT | os.O_EXCL | os.O_WRONLY))   # claimed exclusively: two runs never share a .partial
    except FileExistsError as exc:
        raise MigrationRefused("INTERRUPTED_MIGRATION_PRESENT", f"{partial} appeared: another migration to this target is running") from exc
    try:
        src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(partial))
            try:
                src.backup(dst)                                          # a consistent snapshot; the source is only read
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.DatabaseError as exc:
        if partial.exists():
            partial.unlink()                                             # this run's own copy, never the source
        raise MigrationRefused("SOURCE_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:160]}") from exc
    try:
        report = _migrate_partial(partial, anchors, started)
    except MigrationRefused:
        partial.unlink()                                                 # this run's own copy, never the source
        raise
    side = [str(partial) + s for s in ("-wal", "-shm", "-journal") if Path(str(partial) + s).exists()]
    if side:                                                             # a promoted main file must hold every committed page itself
        raise MigrationRefused("POST_SIDE_FILES", ", ".join(side))
    verified = _file_sha256(partial)
    _publish(partial, target, verified, "TARGET_EXISTS")                 # a target created meanwhile is never overwritten
    report.update({"source": str(source), "target": str(target), "target_sha256": verified, "promotion": PROMOTION, "started_at": _stamp(started),
                   "finished_at": _stamp(clock()), "source_sha256_before": source_before, "source_sha256_after": _file_sha256(source)})
    report["source_unchanged"] = report["source_sha256_before"] == report["source_sha256_after"]
    body = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(str(report_partial), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except FileExistsError as exc:
        raise MigrationRefused("INTERRUPTED_MIGRATION_PRESENT", f"{report_partial} appeared: another migration to this target is running") from exc
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    _publish(report_partial, report_path, hashlib.sha256(body).hexdigest(), "REPORT_EXISTS")   # last: the report marks a completed migration
    return report


def _publish(tmp: Path, final: Path, sha256: str, exists_code: str) -> None:
    """Gives the finished file `tmp` the name `final` without ever replacing a file, then drops the name `tmp`.

    os.link creates the new name or fails when it exists: neither POSIX link(2) nor Windows CreateHardLinkW has a replacing mode, so no
    file system that accepts the call can overwrite, and one without hard links (FAT, exFAT, some network shares) fails it. Nothing
    else is ever tried (no rename, replace or copy): the migration is refused instead. After the link, `final` must be this very file
    holding these very bytes. Every refusal here keeps `tmp`, so the next run refuses until an operator looks."""
    fd = os.open(str(tmp), os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)                                                     # the bytes are on disk before any new name points at them
    finally:
        os.close(fd)
    try:
        os.link(tmp, final)
    except FileExistsError as exc:
        raise MigrationRefused(exists_code, f"{final} appeared during the migration: never overwritten ({tmp.name} kept)") from exc
    except OSError as exc:
        raise MigrationRefused("ATOMIC_PROMOTION_UNSUPPORTED", f"no hard link in {final.parent} ({type(exc).__name__}: {str(exc)[:120]}); "
                               f"nothing else is tried ({tmp.name} kept)") from exc
    try:
        ours = os.path.samefile(tmp, final) and _file_sha256(final) == sha256
    except OSError:
        ours = False
    if not ours:
        raise MigrationRefused("PROMOTION_NOT_VERIFIED", f"{final} is not the verified file ({tmp.name} kept)")
    _sync_dir(final.parent)                                              # the new name is durable before the old one goes
    tmp.unlink()                                                         # a second name of the promoted file, never another file


def _sync_dir(d: Path) -> None:
    """POSIX: flush the directory, so a new name survives a power loss before the next step. Windows: os cannot open a directory for
    fsync; NTFS records the name in its metadata log."""
    if os.name != "posix":
        return
    fd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _migrate_partial(partial: Path, anchors: Dict[str, dict], now: datetime) -> dict:
    from market_intel.store import MarketIntelStore
    from market_intel.store_v2 import MarketIntelStoreV2
    conn = sqlite3.connect(str(partial), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")                    # the backup carries the source's WAL mode: the copy must be one file
        v = MG.current_version(conn)
        if v != 1:
            raise MigrationRefused({0: "SOURCE_NOT_A_STORE", 2: "SOURCE_ALREADY_V2"}.get(v, "SOURCE_UNKNOWN_VERSION"), f"user_version {v}")
        try:
            MG.verify(conn)
        except (SchemaMismatch, SchemaTooNew) as exc:
            raise MigrationRefused("SOURCE_SCHEMA_MISMATCH", str(exc)[:200]) from exc
        _checks(conn, "SOURCE")
        before = table_digests(conn)
    except sqlite3.DatabaseError as exc:
        raise MigrationRefused("SOURCE_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:160]}") from exc
    finally:
        conn.close()
    pre = MarketIntelStore(partial).verify_integrity(anchors=anchors)
    if pre["state"] != "VERIFIED":
        raise MigrationRefused("SOURCE_NOT_VERIFIED", json.dumps(_verdict(pre), sort_keys=True)[:300])
    conn = sqlite3.connect(str(partial), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")                          # the rebuild only: every key is checked again below
        conn.execute("BEGIN IMMEDIATE")
        try:
            apply_v2(conn, now)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            verify(conn)
        except (SchemaMismatch, SchemaTooNew) as exc:
            raise MigrationRefused("POST_SCHEMA", str(exc)[:200]) from exc
        _checks(conn, "POST")
        after = table_digests(conn)
    finally:
        conn.close()
    for t in V1_TABLES:
        if t == "mi_schema_migrations":
            if after[t]["rows"] != before[t]["rows"] + 1:
                raise MigrationRefused("POST_ROWS_CHANGED", t)
            continue
        if after[t] != before[t]:
            raise MigrationRefused("POST_ROWS_CHANGED", t)
    c = sqlite3.connect(str(partial))
    try:
        if [r[0] for r in c.execute("SELECT version FROM mi_schema_migrations ORDER BY version")] != [1, 2]:
            raise MigrationRefused("POST_MIGRATION_RECORDS")
    finally:
        c.close()
    post = MarketIntelStoreV2(partial).verify_integrity(anchors=anchors)
    if post["state"] != "VERIFIED":
        raise MigrationRefused("POST_NOT_VERIFIED", json.dumps(_verdict(post), sort_keys=True)[:300])
    return {"report": "mi_schema_v2_migration_v1", "from_version": 1, "to_version": 2, "tables": {t: {"before": before[t], "after": after[t]} for t in V1_TABLES},
            "pre_integrity": _verdict(pre), "post_integrity": _verdict(post), "datasets_verified": len(anchors)}


def status(path) -> dict:
    """Read-only: what a file is (never creates or changes it)."""
    p = Path(path)
    if not p.is_file():
        return {"file": str(p), "exists": False}
    conn = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    try:
        v = MG.current_version(conn)
        try:
            verify(conn)
            state = "V2_VERIFIED" if v == 2 else "EMPTY"
        except SchemaUpgradeRequired:
            state = "V1_UPGRADE_REQUIRED"
        except SchemaTooNew:
            state = "TOO_NEW"
        except SchemaMismatch as exc:
            state = f"MISMATCH: {str(exc)[:120]}"
    finally:
        conn.close()
    return {"file": str(p), "exists": True, "user_version": v, "state": state}


def main(argv: Optional[Sequence[str]] = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m market_intel.schema_v2", description="Stage 7.3.1 schema v2: status and the explicit copy migration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--file", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--source", required=True)
    m.add_argument("--target", required=True)
    m.add_argument("--archive-root", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 1
    if a.cmd == "status":
        out.write(json.dumps(status(a.file), indent=2) + "\n")
        return 0
    from market_intel.archive import RawArchive
    try:
        rep = migrate_copy(a.source, a.target, anchors=RawArchive(Path(a.archive_root)).anchors())
    except MigrationRefused as exc:
        out.write(json.dumps({"migrated": False, "refused": exc.code, "detail": exc.detail}, indent=2) + "\n")
        return 2
    out.write(json.dumps({"migrated": True, "target": rep["target"], "datasets_verified": rep["datasets_verified"]}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
