"""
Stage 7.3.1 - market intelligence schema v2 and data lifecycle, OFFLINE (DESIGN_731_MARKET_INTEL_SCHEMA_V2.md). Temporary files only:
every store, archive, copy and export lives under tmp_path; the production paths are never read, written or created.

  M01-M05 migration: v1 data, hashes, seals and anchors preserved and the source never touched; every refusal (production paths, existing
          target, leftover partial, corrupt / unverified / unknown sources); interruption and recovery; cross-version opens; concurrency.
  C01-C04 COLLECT: a distinct, authorized kind (caps, seal over the claim, v1 seals unchanged); every unauthorized creation refused, by the
          store and by the schema; provenance and chronology of COLLECT rows; the Stage 7.3 collector still writes FIXTURE only.
  N01-N04 news: duplicates, revisions, late corrections and idempotent runs; point in time (no look-ahead, future as_of, late maps);
          ticker associations recomputed (tampered ones caught); closed runs and dangling references refused by the schema.
  L01-L02 lifecycle: retention classes cover every table; inventory, archive candidates and copy-only export; disk status with an explicit
          threshold; nothing is ever deleted.
  V01-V05 self-review: a COLLECT dataset outliving its authorization; recovery on v2; concurrent migrations to one target. The operator's
          review: promotion never overwrites (another process creating the target or report just before it; no hard links, no fallback;
          a swapped or changed file), concurrent migration processes, a crash at every promotion step and recovery.
  X01-X02 isolation: no network, broker, trading or model import; nothing outside market_intel imports the new modules; existing gates.
  F01-F03 scope (freeze bookkeeping).
"""
from __future__ import annotations

import ast
import errno
import gzip
import os
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import freeze_ledger as FL
from market_intel import collect_auth as CA
from market_intel import lifecycle as LC
from market_intel import migrations as MG
from market_intel import schema_v2 as SV
from market_intel import store as ST
from market_intel.collector import plans as PL
from market_intel.collector import transport as TR
from market_intel.collector.runner import Collector
from market_intel.models import FetchRecord, Observation
from market_intel.news import persist as NP
from market_intel.sources import alpaca_news
from market_intel.store_v2 import MarketIntelStoreV2, PointInTimeError
from test_market_intel_collector_73 import DGS10, FRED, HALTS, NEWS, NONE, NV, SNAP, TUE, Clock, art, env, fobs, fred_page, news, rss, snaps

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
W0, W1 = TUE - timedelta(hours=36), TUE + timedelta(hours=12)                 # a news window covering every fixture
AAPL_MSFT = PL.Scope(("AAPL", "MSFT"), feed="iex")


def sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def v1_world(tmp):
    """A v1 store written by the Stage 7.3 collector on the fake transport: news (with a revision), a snapshot with an absence, an empty
    halt poll and a FRED page - every dataset sealed and anchored (VERIFIED)."""
    t0 = TUE - timedelta(seconds=300)
    e = env(tmp, outcomes=[news([art(1, TUE - timedelta(hours=1)), art(3, t0, headline="v1")]), snaps(AAPL=TUE - timedelta(seconds=1)), rss(),
                           fred_page([fobs("2025-11-28", "4.1", "2025-12-01")])])
    assert [e.col.collect(p, s)["status"] for p, s in ((NEWS, NV), (SNAP, AAPL_MSFT), (HALTS, NONE), (FRED, DGS10))] == ["COMPLETED", "COMPLETED", "EMPTY",
                                                                                                                       "COMPLETED"]
    e.clk.tick(120)
    e.ft.push(news([art(3, t0, headline="v2", updated=TUE + timedelta(seconds=60))]))
    assert e.col.collect(NEWS, NV)["revised"] == 1
    assert e.store.verify_integrity(anchors=e.archive.anchors())["state"] == "VERIFIED"
    return e


def auth_file(tmp, *, name="c1", **over):
    doc = {"version": CA.AUTH_VERSION, "authorization_id": "COLL-TEST-01", "purpose": "OFFLINE_VERIFICATION", "source": "alpaca_news",
           "scope_key": NV.key(), "provider": "ALPACA", "kind": "NEWS", "feed": "none", "symbols": [], "max_requests": 5, "max_rows": 100,
           "granted_by": "test operator", "granted_at": "2025-12-02T14:00:00Z", "expires_at": "2025-12-02T20:00:00Z"}
    doc.update(over)
    root = tmp / "approvals"
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.json"
    p.write_bytes(json.dumps(doc).encode("utf-8"))
    return p


def collect_spec(**over):
    base = dict(job_kind="COLLECT", kind="NEWS", provider="ALPACA", feed="none", adjustment="none", symbols=[],
                request={"collector": "test", "source": "alpaca_news", "scope": NV.key(), "purpose": "OFFLINE_VERIFICATION"}, approval_ref="COLL-TEST-01")
    base.update(over)
    return ST.DatasetSpec(**base)


def v2(tmp, clk=None, name="v2.db"):
    clk = clk or Clock(TUE)
    return MarketIntelStoreV2(tmp / name, clock=clk, approvals_root=tmp / "approvals"), clk


def load(tmp, p, clk):
    return CA.load(p, now=clk(), root=tmp / "approvals")


def fixture_spec(kind="NEWS", provider="ALPACA", feed="none", symbols=()):
    return ST.DatasetSpec(job_kind="FIXTURE", kind=kind, provider=provider, feed=feed, adjustment="none", symbols=list(symbols))


def news_obs(arts, t, did):
    return alpaca_news.parse_page(news(arts).body, symbols=[], retrieved_at=t, dataset_id=did)[0]


def put_news(s, clk, arts, t, *, ingest_at=None):
    clk.t = t
    did = s.create_dataset(fixture_spec())
    obs = news_obs(arts, t, did)
    if ingest_at is not None:
        clk.t = ingest_at
    res = s.ingest(did, obs)
    s.finish_dataset(did, "COMPLETED")
    return res


def ok_fetch(s, did, clk, params=None):
    return s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v1beta1/news", started_at=clk(), retrieved_at=clk(), status="OK"), dataset_id=did,
                          params=params or {"page": 1})


def tamper(path, table, sql, args=()):
    """Edit a row the way an attacker with file access could: drop the append-only trigger, update, put the identical trigger back."""
    c = sqlite3.connect(str(path), isolation_level=None)
    try:
        trig = c.execute("SELECT sql FROM sqlite_master WHERE name = ?", (f"{table}_no_update",)).fetchone()[0]
        c.execute(f"DROP TRIGGER {table}_no_update")
        c.execute(sql, args)
        c.execute(trig)
    finally:
        c.close()


def raw(path):
    c = sqlite3.connect(str(path), isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    return c


# ============================================================================================================ migration (M)
def test_M01_migration_preserves_every_row_hash_seal_and_anchor_and_never_touches_the_source(tmp_path):
    e = v1_world(tmp_path / "w")
    src, anchors = e.store.path, e.archive.anchors()
    before, at = sha_file(src), TUE + timedelta(days=1)
    reads = lambda s: (s.history("NEWS", as_of=at), s.history("QUOTE_SNAPSHOT", symbol="AAPL", feed="iex", as_of=at), s.history("MACRO", series_id="DGS10",   # noqa: E731
                       as_of=at), s.latest_status("QUOTE_SNAPSHOT", at=at, symbol="MSFT", feed="iex"), s.dataset_states(),
                       [s.revisions(r["natural_key"]) for r in s.history("NEWS", as_of=at)])
    v1_reads = reads(e.store)
    rep = SV.migrate_copy(src, tmp_path / "m" / "v2.db", anchors=anchors, clock=e.clk)
    assert rep["source_unchanged"] and sha_file(src) == before and not (tmp_path / "m" / "v2.db.partial").exists()
    assert (rep["target"], rep["target_sha256"], rep["promotion"]) == (str(tmp_path / "m" / "v2.db"), sha_file(tmp_path / "m" / "v2.db"), SV.PROMOTION)
    assert sorted(x.name for x in (tmp_path / "m").iterdir()) == ["v2.db", "v2.db.migration.json"]                     # one file: no WAL left behind
    assert {t for t, v in rep["tables"].items() if v["before"] != v["after"]} == {"mi_schema_migrations"}               # every other row, byte for byte
    assert rep["tables"]["mi_schema_migrations"]["after"]["rows"] == 2 and rep["tables"]["mi_observations"]["before"]["rows"] >= 5
    assert json.loads((tmp_path / "m" / "v2.db.migration.json").read_text(encoding="utf-8")) == rep
    s2 = MarketIntelStoreV2(tmp_path / "m" / "v2.db", clock=e.clk, approvals_root=tmp_path / "approvals")
    v = s2.verify_integrity(anchors=anchors)
    assert (v["state"], v["unanchored_datasets"], v["collect_mismatches"], v["news_mismatches"]) == ("VERIFIED", [], [], [])   # every seal recomputes
    assert reads(s2) == v1_reads and s2.schema_version() == 2 and SV.status(tmp_path / "m" / "v2.db")["state"] == "V2_VERIFIED"
    fresh, _ = v2(tmp_path / "f")
    fresh.create_dataset(fixture_spec())
    for p in (fresh.path, s2.path):
        c = sqlite3.connect(str(p))
        assert SV._norm(MG._objects(c)) == SV.expected_objects(2), p                                                    # fresh and migrated: one schema
        c.close()
    again = SV.migrate_copy(src, tmp_path / "m2" / "v2.db", anchors=anchors, clock=e.clk)
    assert {t: v["after"] for t, v in again["tables"].items()} == {t: v["after"] for t, v in rep["tables"].items()}      # deterministic
    did = s2.create_dataset(fixture_spec())                                                                              # the v1 kinds work on v2
    assert s2.ingest(did, news_obs([art(1, TUE - timedelta(hours=1))], e.clk(), did)).duplicates == 1


def test_M02_every_unsafe_or_unverified_source_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    e = v1_world(tmp_path / "w")
    src, anchors = e.store.path, e.archive.anchors()
    before = sha_file(src)

    def copy(name):
        p = tmp_path / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, p)
        return p

    def refused(source, *, code, target=None, a=anchors):
        target = target or tmp_path / "out" / f"{code if isinstance(code, str) else 'any'}.db"
        existed = target.exists()
        with pytest.raises(SV.MigrationRefused) as ei:
            SV.migrate_copy(source, target, anchors=a, clock=e.clk)
        assert ei.value.code == code or (isinstance(code, set) and ei.value.code in code), (code, ei.value.code, ei.value.detail)
        assert target.exists() == existed and not Path(str(target) + ".partial").exists(), code
        return ei.value
    assert SV.production_paths() == (ST.default_path().resolve(), (ROOT / "data").resolve())                           # the real ones, read only
    prod = tmp_path / "prod" / "data"                                                                                    # a stand-in: even a broken
    prod.mkdir(parents=True)                                                                                             # guard writes here, never
    shutil.copyfile(src, prod / "market_intel.db")                                                                       # into the repository's data/
    monkeypatch.setattr(SV, "production_paths", lambda: ((prod / "market_intel.db").resolve(), prod.resolve()))
    refused(prod / "market_intel.db", code="SOURCE_IS_PRODUCTION")
    refused(src, code="TARGET_IS_PRODUCTION", target=prod / "mi_v2.db")
    refused(src, code="TARGET_IS_PRODUCTION", target=prod / "deeper" / "mi_v2.db")
    monkeypatch.undo()
    assert sorted(x.name for x in prod.iterdir()) == ["market_intel.db"] and not ST.default_path().exists()
    refused(src, code="NO_ANCHORS", a=None)
    refused(tmp_path / "nope.db", code="SOURCE_MISSING")
    refused(src, code="SAME_FILE", target=src)
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "out" / "taken.db").write_bytes(b"x")
    with pytest.raises(SV.MigrationRefused, match="TARGET_EXISTS"):
        SV.migrate_copy(src, tmp_path / "out" / "taken.db", anchors=anchors, clock=e.clk)
    assert (tmp_path / "out" / "taken.db").read_bytes() == b"x" and not (tmp_path / "out" / "taken.db.partial").exists()   # refused before any copy
    empty = tmp_path / "src" / "empty.db"
    empty.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(empty))
    c.execute("CREATE TABLE t (x)")
    c.execute("DROP TABLE t")
    c.commit()
    c.close()
    refused(empty, code="SOURCE_NOT_A_STORE")
    newer = copy("newer.db")
    c = sqlite3.connect(str(newer))
    c.execute("PRAGMA user_version = 7")
    c.commit()
    c.close()
    refused(newer, code="SOURCE_UNKNOWN_VERSION")
    extra = copy("extra.db")
    c = sqlite3.connect(str(extra))
    c.execute("CREATE TABLE extra (x)")
    c.commit()
    c.close()
    refused(extra, code="SOURCE_SCHEMA_MISMATCH")
    bad_row = copy("tampered.db")
    tamper(bad_row, "mi_observations", "UPDATE mi_observations SET payload_json = '{\"headline\":\"forged\"}' WHERE rowid = 1")
    assert "row_hash_mismatches" in refused(bad_row, code="SOURCE_NOT_VERIFIED").detail                                  # a hash that does not recompute
    one = dict(anchors)
    one.pop(sorted(one)[0])
    refused(src, code="SOURCE_NOT_VERIFIED", a=one)                                                                      # an unanchored seal
    forged = {k: dict(v) for k, v in anchors.items()}
    forged[sorted(forged)[0]]["content_sha256"] = "0" * 64
    refused(src, code="SOURCE_NOT_VERIFIED", a=forged)                                                                   # an anchor that disagrees
    dangling = copy("dangling.db")
    c = sqlite3.connect(str(dangling))
    c.execute("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES ('no-such-dataset', 'RUNNING', '2025-12-02T15:00:00.000000Z')")
    c.commit()
    c.close()
    refused(dangling, code="SOURCE_FOREIGN_KEYS")
    open_ds = copy("unsealed.db")
    s1 = ST.MarketIntelStore(open_ds, clock=e.clk, approvals_root=tmp_path / "approvals")
    s1.create_dataset(fixture_spec())                                                                                    # a run in flight: not quiescent
    refused(open_ds, code="SOURCE_NOT_VERIFIED")
    broken = copy("broken.db")
    b = bytearray(broken.read_bytes())
    b[4096:8192] = b"\xff" * 4096
    broken.write_bytes(bytes(b))
    refused(broken, code={"SOURCE_UNREADABLE", "SOURCE_INTEGRITY_CHECK", "SOURCE_SCHEMA_MISMATCH", "SOURCE_NOT_VERIFIED"})
    rep = SV.migrate_copy(src, tmp_path / "ok" / "v2.db", anchors=anchors, clock=e.clk)
    refused(tmp_path / "ok" / "v2.db", code="SOURCE_ALREADY_V2")
    assert sha_file(src) == before and rep["source_unchanged"]


def test_M03_an_interrupted_migration_leaves_no_target_blocks_a_rerun_and_recovers_after_the_operator(tmp_path, monkeypatch):
    e = v1_world(tmp_path / "w")
    src, anchors, target = e.store.path, e.archive.anchors(), tmp_path / "m" / "v2.db"
    partial = Path(str(target) + ".partial")
    real = SV.apply_v2

    def crash(conn, now):
        for st in SV.V2[:4]:                                                                                             # mid-rebuild: the table is gone
            conn.execute(st)
        raise KeyboardInterrupt()
    monkeypatch.setattr(SV, "apply_v2", crash)
    with pytest.raises(KeyboardInterrupt):
        SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)
    assert not target.exists() and partial.exists()
    c = sqlite3.connect(str(partial))
    assert MG.current_version(c) == 1 and MG.verify(c) == 1                                                              # rolled back whole: still v1
    c.close()
    monkeypatch.setattr(SV, "apply_v2", real)
    with pytest.raises(SV.MigrationRefused, match="INTERRUPTED_MIGRATION_PRESENT"):
        SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)
    partial.unlink()                                                                                                     # the operator's decision
    assert SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)["post_integrity"]["state"] == "VERIFIED"

    def writes_a_row(conn, now):
        real(conn, now)
        conn.execute("INSERT INTO mi_quality_audits (at, scope_json, check_name, result, codes_json, detail_json) VALUES "
                     "('2025-12-02T15:00:00.000000Z', '{}', 'x', 'PASS', '[]', '{}')")
    monkeypatch.setattr(SV, "apply_v2", writes_a_row)
    with pytest.raises(SV.MigrationRefused, match="POST_ROWS_CHANGED"):                                                  # a migration that changed data
        SV.migrate_copy(src, tmp_path / "m3" / "v2.db", anchors=anchors, clock=e.clk)
    assert not (tmp_path / "m3" / "v2.db").exists() and not (tmp_path / "m3" / "v2.db.partial").exists()

    def bad_v2_row(conn, now):
        real(conn, now)
        conn.execute("INSERT INTO mi_news_entity_maps (map_sha256, map_json, recorded_at) VALUES (?, '{}', '2025-12-02T15:00:00.000000Z')", ("0" * 64,))
    monkeypatch.setattr(SV, "apply_v2", bad_v2_row)
    with pytest.raises(SV.MigrationRefused, match="POST_NOT_VERIFIED"):                                                  # caught by the v2 verification
        SV.migrate_copy(src, tmp_path / "m4" / "v2.db", anchors=anchors, clock=e.clk)
    assert not (tmp_path / "m4" / "v2.db").exists()


def test_M04_each_store_opens_only_its_own_version_and_unknown_versions_fail_closed(tmp_path):
    e = v1_world(tmp_path / "w")
    v2p = tmp_path / "m" / "v2.db"
    SV.migrate_copy(e.store.path, v2p, anchors=e.archive.anchors(), clock=e.clk)
    with pytest.raises(MG.SchemaTooNew):
        ST.MarketIntelStore(v2p, clock=e.clk).create_dataset(fixture_spec())                                            # the 7.1 store never reads v2
    with pytest.raises(MG.SchemaTooNew):
        ST.MarketIntelStore(v2p).history("NEWS")
    assert (ST.MarketIntelStore(v2p).verify_integrity()["store"], ST.MarketIntelStore(v2p).verify_integrity()["ok"]) == ("SCHEMA_ERROR", False)
    s_on_v1 = MarketIntelStoreV2(e.store.path, clock=e.clk)
    with pytest.raises(SV.SchemaUpgradeRequired):
        s_on_v1.history("NEWS")
    with pytest.raises(SV.SchemaUpgradeRequired):
        s_on_v1.create_dataset(fixture_spec())                                                                           # never an implicit upgrade
    assert s_on_v1.verify_integrity()["store"] == "UPGRADE_REQUIRED" and SV.status(e.store.path)["state"] == "V1_UPGRADE_REQUIRED"
    c = sqlite3.connect(str(e.store.path))
    assert MG.current_version(c) == 1
    c.close()
    for name, sql in (("v3.db", "PRAGMA user_version = 3"), ("trigger.db", "DROP TRIGGER mi_news_runs_point_in_time"),
                      ("records.db", "DELETE FROM mi_schema_migrations WHERE version = 2"), ("reset.db", "PRAGMA user_version = 0")):
        p = tmp_path / name
        shutil.copyfile(v2p, p)
        c = sqlite3.connect(str(p))
        if "DELETE" in sql:
            trig = c.execute("SELECT sql FROM sqlite_master WHERE name = 'mi_schema_migrations_no_delete'").fetchone()[0]
            c.execute("DROP TRIGGER mi_schema_migrations_no_delete")
            c.execute(sql)
            c.execute(trig)                                                                                      # the objects are intact: only the records differ
        else:
            c.execute(sql)
        c.commit()
        c.close()
        s = MarketIntelStoreV2(p, clock=e.clk)
        with pytest.raises(MG.SchemaTooNew if name == "v3.db" else MG.SchemaMismatch):
            s.history("NEWS")
        with pytest.raises((MG.SchemaTooNew, MG.SchemaMismatch)):
            s.create_dataset(fixture_spec())
        assert SV.status(p)["state"] == "TOO_NEW" if name == "v3.db" else SV.status(p)["state"].startswith("MISMATCH"), name
    with pytest.raises(ValueError):
        MarketIntelStoreV2(None)                                                                                         # no default v2 path
    assert not ST.default_path().exists()


def test_M05_concurrent_writers_never_corrupt_a_v2_store_and_a_migration_takes_a_consistent_snapshot(tmp_path):
    s, clk = v2(tmp_path)
    errors = []

    def writer(i):
        try:
            for j in range(4):
                did = s.create_dataset(fixture_spec())
                s.ingest(did, news_obs([art(100 * i + j, TUE - timedelta(minutes=j + 1))], clk(), did))
                s.finish_dataset(did, "COMPLETED")
        except Exception as exc:                                                                                         # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    v = s.verify_integrity()
    assert errors == [] and len(s.dataset_states()["sealed"]) == 12 and not [k for k in ST.MarketIntelStore._CORRUPT_KEYS if v[k]]
    e = v1_world(tmp_path / "w")
    anchors = e.archive.anchors()
    results = []
    stop = threading.Event()

    def source_writer():
        s1 = ST.MarketIntelStore(e.store.path, clock=e.clk, approvals_root=tmp_path / "approvals")
        while not stop.is_set():
            did = s1.create_dataset(fixture_spec())
            s1.finish_dataset(did, "FAILED")
    th = threading.Thread(target=source_writer)
    th.start()
    try:
        for i in range(3):
            try:
                results.append(SV.migrate_copy(e.store.path, tmp_path / f"c{i}" / "v2.db", anchors=anchors, clock=e.clk)["post_integrity"]["state"])
            except SV.MigrationRefused as exc:
                results.append(exc.code)
    finally:
        stop.set()
        th.join(30)
    assert set(results) <= {"VERIFIED", "SOURCE_NOT_VERIFIED"} and results                                              # a snapshot: consistent or refused
    for i, r in enumerate(results):
        if r == "VERIFIED":
            assert MarketIntelStoreV2(tmp_path / f"c{i}" / "v2.db").verify_integrity(anchors=anchors)["state"] == "VERIFIED"
    assert not [k for k in ST.MarketIntelStore._CORRUPT_KEYS if e.store.verify_integrity()[k]]                           # the live source is intact


# ============================================================================================================ COLLECT (C)
def test_C01_a_collect_dataset_is_distinct_authorized_capped_and_sealed_over_its_claim(tmp_path):
    s, clk = v2(tmp_path)
    auth = load(tmp_path, auth_file(tmp_path, max_requests=2, max_rows=3), clk)
    did = s.create_dataset(collect_spec(), approval=auth)
    ds = s.dataset(did)
    assert (ds["job_kind"], ds["approval_ref"], json.loads(ds["request_json"])["purpose"]) == ("COLLECT", "COLL-TEST-01", "OFFLINE_VERIFICATION")
    f1, f2 = ok_fetch(s, did, clk, {"p": 1}), ok_fetch(s, did, clk, {"p": 2})
    with pytest.raises(ST.RowCapError, match="APPROVAL_REQUEST_CAP"):
        ok_fetch(s, did, clk, {"p": 3})                                                                                  # the claim's request cap
    arts = [art(i, TUE - timedelta(minutes=i)) for i in range(1, 5)]
    with pytest.raises(ST.RowCapError, match="DELIVERED_ROW_CAP"):
        s.ingest(did, news_obs(arts, clk(), did), fetch_id=f1)                                                           # the claim's row cap
    assert s.ingest(did, news_obs(arts[:3], clk(), did), fetch_id=f1).inserted == 3
    sealed = s.finish_dataset(did, "COMPLETED")
    fx = s.create_dataset(fixture_spec())
    s.ingest(fx, news_obs([art(9, TUE - timedelta(minutes=9))], clk(), fx))
    fsealed = s.finish_dataset(fx, "COMPLETED")
    def v1_digest(d):
        ev = s.dataset(d)["events"][-1]
        with s._ro() as c:
            return ST.MarketIntelStore._metadata_sha256(c, d, ev["status"], {k: ev[col] for k, col in ST.MarketIntelStore._COUNT_COLUMNS.items()})
    assert v1_digest(did) != sealed["metadata_sha256"]                                                                   # the claim is in the seal
    assert v1_digest(fx) == fsealed["metadata_sha256"]                                                                   # a FIXTURE seal is the 7.1 seal
    anchors = {d: {"content_sha256": x["content_sha256"], "records_sha256": x["records_sha256"], "metadata_sha256": x["metadata_sha256"],
                   "status": x["status"]} for d, x in ((did, sealed), (fx, fsealed))}
    assert s.verify_integrity(anchors=anchors)["state"] == "VERIFIED"
    tamper(s.path, "mi_collect_authorization_uses", "UPDATE mi_collect_authorization_uses SET max_rows = 99")
    v = s.verify_integrity(anchors=anchors)
    assert v["state"] == "CORRUPT" and v["metadata_mismatches"] == [did] and f2                                          # a raised cap breaks the seal


def test_C02_no_path_but_a_valid_offline_authorization_creates_a_collect_dataset(tmp_path):
    s, clk = v2(tmp_path)
    good = load(tmp_path, auth_file(tmp_path), clk)
    with pytest.raises(ST.ScopeError):
        ST.MarketIntelStore(tmp_path / "v1.db", clock=clk).create_dataset(collect_spec(), approval=good)                # the 7.1 store: no COLLECT
    for approval in (None, object(), "COLL-TEST-01"):
        with pytest.raises(CA.CollectAuthorizationError, match="COLLECT_NEEDS_AUTHORIZATION"):
            s.create_dataset(collect_spec(), approval=approval)
    bad_files = {"live": dict(purpose="LIVE"), "unknown": dict(purpose="SHADOW"), "backfill": dict(version="mi_backfill_approval_v1"),
                 "expired": dict(expires_at="2025-12-02T14:30:00Z"), "future": dict(granted_at="2025-12-02T16:00:00Z", expires_at="2025-12-02T18:00:00Z"),
                 "long": dict(granted_at="2025-12-01T14:00:00Z", expires_at="2025-12-02T20:00:00Z"), "zero": dict(max_rows=0),
                 "id": dict(authorization_id="x")}
    for name, over in bad_files.items():
        with pytest.raises(CA.CollectAuthorizationError):
            load(tmp_path, auth_file(tmp_path, name=name, **over), clk)
    with pytest.raises(CA.CollectAuthorizationError, match="LIVE_COLLECT_NOT_AUTHORIZED"):
        load(tmp_path, auth_file(tmp_path, name="live2", purpose="LIVE"), clk)
    outside = tmp_path / "elsewhere.json"
    outside.write_bytes(auth_file(tmp_path, name="o").read_bytes())
    with pytest.raises(CA.CollectAuthorizationError):
        CA.load(outside, now=clk(), root=tmp_path / "approvals")
    wrong = {"source": collect_spec(request={"collector": "t", "source": "halts", "scope": NV.key(), "purpose": "OFFLINE_VERIFICATION"}),
             "scope": collect_spec(request={"collector": "t", "source": "alpaca_news", "scope": "symbols=AMD|feed=none|series=", "purpose": "OFFLINE_VERIFICATION"}),
             "purpose": collect_spec(request={"collector": "t", "source": "alpaca_news", "scope": NV.key(), "purpose": "LIVE"}),
             "kind": collect_spec(kind="HALT", provider="NASDAQ_TRADER"), "provider": collect_spec(provider="SEC"), "ref": collect_spec(approval_ref="COLL-OTHER-1"),
             "symbols": collect_spec(symbols=["NVDA"])}
    for name, spec in wrong.items():
        with pytest.raises((CA.CollectAuthorizationError, ST.StoreError)):
            s.create_dataset(spec, approval=good)
    sec = load(tmp_path, auth_file(tmp_path, name="sec", source="sec_filings", provider="SEC", kind="FILING", authorization_id="COLL-SEC-01"), clk)
    with pytest.raises(CA.CollectAuthorizationError, match="SEC_ACCEPTANCE_TZ_UNVERIFIED"):
        s.create_dataset(collect_spec(kind="FILING", provider="SEC", approval_ref="COLL-SEC-01",
                                      request={"collector": "t", "source": "sec_filings", "scope": NV.key(), "purpose": "OFFLINE_VERIFICATION"}), approval=sec)
    edited = auth_file(tmp_path, name="edited")
    e_auth = load(tmp_path, edited, clk)
    edited.write_bytes(edited.read_bytes().replace(b'"max_rows": 100', b'"max_rows": 101'))
    with pytest.raises(CA.CollectAuthorizationError, match="changed"):
        s.create_dataset(collect_spec(), approval=e_auth)
    for job in ("FIXTURE", "IMPORT", "BACKFILL"):
        with pytest.raises(ST.StoreError):
            s.create_dataset(fixture_spec() if job == "FIXTURE" else ST.DatasetSpec(job_kind=job, kind="NEWS", provider="ALPACA", feed="none",
                                                                                      adjustment="none", symbols=[]), approval=good)
    assert s.dataset_states() == {"unsealed": [], "sealed": []}                                                          # nothing was created
    did = s.create_dataset(collect_spec(), approval=good)
    with pytest.raises(ST.StoreError, match="already used"):
        s.create_dataset(collect_spec(), approval=good)                                                                  # used once
    c = raw(s.path)
    try:
        ins = ("INSERT INTO mi_datasets (dataset_id, job_kind, kind, provider, feed, adjustment, symbols_json, request_json, approval_ref, created_at, "
               "code_version) VALUES (?, ?, 'NEWS', 'ALPACA', 'none', 'none', '[]', '{}', ?, '2025-12-02T15:00:00.000000Z', 'x')")
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_NEEDS_AUTHORIZATION"):
            c.execute(ins, ("raw1", "COLLECT", "COLL-TEST-01"))                                                          # raw SQL: no claim of its own
        claim = ("INSERT INTO mi_collect_authorization_uses (authorization_id, dataset_id, purpose, source, scope_key, authorization_file_sha256, used_at, "
                 "expires_at, max_requests, max_rows) VALUES (?, ?, ?, 'alpaca_news', 'x', ?, '2025-12-02T15:00:00.000000Z', '2025-12-02T16:00:00.000000Z', 1, 1)")
        with pytest.raises(sqlite3.DatabaseError, match="CHECK"):
            c.execute(claim, ("RAW-LIVE-1", "raw2", "LIVE", "0" * 64))                                                    # the schema has no live claim
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_AUTHORIZATION_AFTER_THE_FACT"):
            c.execute(claim, ("RAW-LATE-1", did, "OFFLINE_VERIFICATION", "0" * 64))
        c.execute("BEGIN")
        c.execute(claim, ("RAW-FIX-1", "raw3", "OFFLINE_VERIFICATION", "0" * 64))
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_AUTHORIZATION_ON_OTHER_KIND"):
            c.execute(ins, ("raw3", "FIXTURE", "RAW-FIX-1"))
        c.execute("ROLLBACK")
    finally:
        c.close()
    assert [d["dataset_id"] for d in s.dataset_states()["unsealed"]] == [did]
    c = sqlite3.connect(str(s.path), isolation_level=None)
    try:
        trig = c.execute("SELECT sql FROM sqlite_master WHERE name = 'mi_datasets_collect_needs_claim'").fetchone()[0]
        c.execute("DROP TRIGGER mi_datasets_collect_needs_claim")
        c.execute(ins, ("forced", "COLLECT", "NO-CLAIM-1"))
        c.execute("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES ('forced', 'RUNNING', '2025-12-02T15:00:00.000000Z')")
        c.execute(trig)
    finally:
        c.close()
    v = s.verify_integrity()
    assert v["state"] == "CORRUPT" and {"dataset_id": "forced", "problem": "CLAIM"} in v["collect_mismatches"]


def test_C03_collect_rows_carry_live_provenance_and_the_71_chronology_and_revision_rules(tmp_path):
    s, clk = v2(tmp_path)
    did = s.create_dataset(collect_spec(), approval=load(tmp_path, auth_file(tmp_path), clk))
    other = s.create_dataset(fixture_spec())
    f_other = ok_fetch(s, other, clk)
    bad_fetch = s.record_fetch(FetchRecord(provider="ALPACA", endpoint="/v1beta1/news", started_at=clk(), status="TIMEOUT"), dataset_id=did, params={"p": 9})
    good = ok_fetch(s, did, clk)
    obs = news_obs([art(1, TUE - timedelta(minutes=5))], clk(), did)
    with pytest.raises(ST.ScopeError, match="COLLECT row needs the fetch"):
        s.ingest(did, obs)
    for fid in (f_other, bad_fetch):
        with pytest.raises(ST.ScopeError):
            s.ingest(did, obs, fetch_id=fid)
    late = obs[0].model_copy(update={"available_at": clk() + timedelta(seconds=30)})
    back = news_obs([art(2, TUE - timedelta(minutes=6))], clk(), did)[0].model_copy(update={"backfilled": True, "quality_flags": ("BACKFILLED",)})
    future = news_obs([art(3, TUE + timedelta(minutes=10))], clk(), did)[0]
    r = s.ingest(did, [obs[0], late, back, future], fetch_id=good, on_reject="quarantine")
    assert (r.inserted, sorted(x["reason"] for x in r.rejections)) == (1, ["COLLECT_AVAILABILITY", "COLLECT_BACKFILLED", "OBSERVED_AFTER_RETRIEVAL"])
    c = raw(s.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_PROVENANCE"):
            c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, disposition, at) VALUES (?, 'k', 'REJECTED', '2025-12-02T15:00:00.000000Z')",
                      (did,))
        cols = [r[1] for r in c.execute("PRAGMA table_info(mi_observations)")]
        sel = ", ".join({"obs_id": "'forged'", "revision": "9", "fetch_id": "NULL", "supersedes_obs_id": "NULL"}.get(x, x) for x in cols)
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_PROVENANCE"):
            c.execute(f"INSERT INTO mi_observations ({', '.join(cols)}) SELECT {sel} FROM mi_observations WHERE dataset_id = ?", (did,))
        sel2 = ", ".join({"obs_id": "'forged2'", "revision": "8", "supersedes_obs_id": "NULL", "backfilled": "1"}.get(x, x) for x in cols)
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_PROVENANCE"):                                           # a valid fetch, but backfilled
            c.execute(f"INSERT INTO mi_observations ({', '.join(cols)}) SELECT {sel2} FROM mi_observations WHERE dataset_id = ?", (did,))
    finally:
        c.close()
    tamper(s.path, "mi_observations", "UPDATE mi_observations SET fetch_id = NULL WHERE dataset_id = ?", (did,))
    assert {"dataset_id": did, "problem": "PROVENANCE:mi_observations"} in s.verify_integrity()["collect_mismatches"]
    s.finish_dataset(did, "PARTIAL")
    clk.tick(60)
    d2 = s.create_dataset(collect_spec(approval_ref="COLL-TEST-02"), approval=load(tmp_path, auth_file(tmp_path, name="c2", authorization_id="COLL-TEST-02"), clk))
    f2 = ok_fetch(s, d2, clk)
    again = news_obs([art(1, TUE - timedelta(minutes=5)), art(1, TUE - timedelta(minutes=5), headline="edited")][1:], clk(), d2)
    assert s.ingest(d2, again, fetch_id=f2).revised == 1                                                                # a revision across COLLECT datasets


def test_C04_the_73_collector_still_writes_fixture_only_whatever_store_it_is_given(tmp_path):
    s, clk = v2(tmp_path)
    from market_intel.archive import RawArchive
    from market_intel.collector.state import CollectorState
    ft = TR.FakeTransport([news([art(1, TUE - timedelta(hours=1))])], clock=clk)
    col = Collector(store=s, archive=RawArchive(tmp_path / "raw"), state=CollectorState(tmp_path / "cs.db"), transport=ft, clock=clk, sleep=clk.sleep,
                    credentials=lambda n: True)
    r = col.collect(NEWS, NV)
    assert r["status"] == "COMPLETED" and s.dataset(r["dataset_id"])["job_kind"] == "FIXTURE"
    assert col.collect(NEWS, NV, sink_job_kind="COLLECT")["refused"] == "LIVE_INGESTION_NOT_APPROVED"
    assert col.collect(PL.PLANS["sec_filings"], PL.Scope(("AAPL",)))["refused"] == "BLOCKED_PENDING_APPROVAL"
    real = Collector(store=s, archive=RawArchive(tmp_path / "raw"), state=CollectorState(tmp_path / "cs.db"), transport=TR.NoTransport(), clock=clk,
                     sleep=clk.sleep, credentials=lambda n: True)
    assert real.collect(NEWS, NV)["refused"] == "REAL_NETWORK_NOT_APPROVED"
    assert {d["job_kind"] for d in (s.dataset(x["dataset_id"]) for x in s.dataset_states()["sealed"])} == {"FIXTURE"}


# ============================================================================================================ news (N)
def test_N01_runs_keep_duplicates_once_follow_revisions_and_late_corrections_and_are_idempotent(tmp_path):
    s, clk = v2(tmp_path)
    t1 = TUE - timedelta(hours=2)
    put_news(s, clk, [art(1, t1, headline="Nvidia raises full-year guidance after record quarter"), art(2, t1 + timedelta(minutes=1),
             headline="Nvidia raises full year guidance after record quarter")], t1 + timedelta(minutes=5))
    put_news(s, clk, [art(1, t1, headline="Nvidia raises full-year guidance after record quarter")], t1 + timedelta(minutes=10))   # a re-delivery
    put_news(s, clk, [art(3, t1, headline="AMD wins a data-center contract", symbols=("AMD",))], t1 + timedelta(minutes=15))
    clk.t = t1 + timedelta(minutes=20)
    r1 = NP.cluster_run(s, as_of=clk(), window_start=W0, window_end=W1)
    run1 = s.news_run(r1["run_id"])
    assert (r1["status"], run1["input_count"], run1["output_count"]) == ("INSERTED", 3, 2)                               # the re-delivery is one input
    by = {m["natural_key"].rsplit("|", 1)[1]: m for m in run1["members"]}
    key = {m["natural_key"].rsplit("|", 1)[1]: m["natural_key"] for m in run1["members"]}
    k3 = key["3"]
    assert by["1"]["cluster_id"] == by["2"]["cluster_id"] != by["3"]["cluster_id"]                                       # same story, two ids
    assert {i["obs_id"] for i in run1["inputs"]} <= {r["obs_id"] for r in s.history("NEWS", as_of=clk())}                # traceable to stored facts
    assert NP.cluster_run(s, as_of=clk(), window_start=W0, window_end=W1)["status"] == "DUPLICATE"                        # idempotent
    t2 = t1 + timedelta(minutes=40)
    put_news(s, clk, [art(3, t1, headline="AMD wins a data-center contract worth $2 billion", updated=t2, symbols=("AMD",))], t2)   # a late correction
    clk.t = t2 + timedelta(minutes=5)
    r2 = NP.cluster_run(s, as_of=clk(), window_start=W0, window_end=W1)
    run2 = s.news_run(r2["run_id"])
    assert r2["status"] == "INSERTED" and run2["input_sha256"] != run1["input_sha256"]
    assert [i["revision"] for i in run2["inputs"] if i["natural_key"] == k3] == [2] and [i["revision"] for i in run1["inputs"] if i["natural_key"] == k3] == [1]
    at = lambda t: (s.news_run_as_of("CLUSTERS", t, window_start=W0, window_end=W1) or {}).get("run_id")  # noqa: E731
    assert (at(t1), at(t1 + timedelta(minutes=30)), at(clk())) == (None, r1["run_id"], r2["run_id"])                     # what a reader could see then
    late = NP.cluster_run(s, as_of=t1 + timedelta(minutes=20), window_start=W0, window_end=W1)                           # recomputed later, as of earlier
    assert late["status"] == "DUPLICATE" and at(t1 + timedelta(minutes=30)) == r1["run_id"]
    other = NP.cluster_run(s, as_of=t1 + timedelta(minutes=20), window_start=W0, window_end=W1 + timedelta(hours=1))         # a new window, computed now
    assert other["status"] == "INSERTED" and s.news_run_as_of("CLUSTERS", t1 + timedelta(minutes=30), window_start=W0,
                                                              window_end=W1 + timedelta(hours=1)) is None                # not visible before it existed
    first = {i["natural_key"]: i["first_seen_at"] for i in run1["inputs"]}
    assert [i["first_seen_at"] for i in run2["inputs"] if i["natural_key"] == k3] == [first[k3]]                         # first seen: the earliest delivery
    assert s.verify_integrity()["news_mismatches"] == []


def test_N02_a_run_never_sees_anything_not_known_at_its_as_of(tmp_path):
    s, clk = v2(tmp_path)
    t1 = TUE - timedelta(hours=2)
    put_news(s, clk, [art(1, t1)], t1 + timedelta(minutes=1))
    put_news(s, clk, [art(2, t1 + timedelta(minutes=2))], t1 + timedelta(minutes=3), ingest_at=t1 + timedelta(minutes=30))   # retrieved early, stored late
    put_news(s, clk, [art(3, t1 + timedelta(minutes=4))], t1 + timedelta(minutes=50))                                          # available late
    clk.t = t1 + timedelta(hours=1)
    keys = lambda t: [i["natural_key"].rsplit("|", 1)[1] for i in s.news_inputs(as_of=t, window_start=W0, window_end=W1)]  # noqa: E731
    assert (keys(t1 + timedelta(minutes=10)), keys(t1 + timedelta(minutes=40)), keys(t1 + timedelta(minutes=55))) == (["1"], ["1", "2"], ["1", "2", "3"])
    with pytest.raises(PointInTimeError, match="FUTURE_AS_OF"):
        NP.cluster_run(s, as_of=clk() + timedelta(seconds=1), window_start=W0, window_end=W1)
    inputs = s.news_inputs(as_of=t1 + timedelta(minutes=40), window_start=W0, window_end=W1)
    with pytest.raises(PointInTimeError, match="NEWS_INPUTS_NOT_KNOWN_SET"):                                             # a partial or stale input set
        s.record_news_run(run_kind="CLUSTERS", algorithm=NP.CLUSTER_ALGORITHM, map_sha256="", as_of=t1 + timedelta(minutes=40), window_start=W0,
                          window_end=W1, inputs=inputs[:1], clusters=NP.compute_clusters(inputs[:1])[0], members=NP.compute_clusters(inputs[:1])[1], mentions=[])
    with pytest.raises(PointInTimeError, match="NEWS_INPUTS_NOT_KNOWN_SET"):                                             # inputs from a later as_of
        s.record_news_run(run_kind="CLUSTERS", algorithm=NP.CLUSTER_ALGORITHM, map_sha256="", as_of=t1 + timedelta(minutes=10), window_start=W0,
                          window_end=W1, inputs=inputs, clusters=NP.compute_clusters(inputs)[0], members=NP.compute_clusters(inputs)[1], mentions=[])
    msha = s.record_entity_map({"sec_names": {"NVDA": "NVIDIA CORP"}, "universe": ["NVDA"], "aliases": {}})
    with pytest.raises(PointInTimeError, match="LOOK_AHEAD"):
        NP.mention_run(s, as_of=t1 + timedelta(minutes=40), window_start=W0, window_end=W1, map_sha256=msha)              # the map came later
    with pytest.raises(PointInTimeError, match="LOOK_AHEAD"):                                                            # the store checks it itself
        s.record_news_run(run_kind="MENTIONS", algorithm=NP.MENTION_ALGORITHM, map_sha256=msha, as_of=t1 + timedelta(minutes=40), window_start=W0,
                          window_end=W1, inputs=inputs, clusters=[], members=[],
                          mentions=NP.compute_mentions(inputs, {"sec_names": {"NVDA": "NVIDIA CORP"}, "universe": ["NVDA"], "aliases": {}}))
    clk.t = t1 + timedelta(hours=1, minutes=10)
    did = s.create_dataset(fixture_spec())
    flagged = news_obs([art(1, t1, headline="Nvidia story 1, corrected")], clk(), did)[0].model_copy(update={"quality_flags": ("FUTURE_DATED",)})
    assert s.ingest(did, [flagged]).revised == 1 and s.revisions(ST.natural_key(flagged))[-1]["usable"] is False
    assert keys(clk()) == ["2", "3"]                                                                                     # its latest known revision is unusable
    c = raw(s.path)
    try:
        o = c.execute("SELECT obs_id, natural_key, available_at FROM mi_observations WHERE natural_key LIKE '%|3'").fetchone()
        c.execute("BEGIN")
        c.execute("INSERT INTO mi_news_run_inputs (run_id, natural_key, obs_id, revision, record_sha256, first_seen_at) VALUES ('r', ?, ?, 1, ?, ?)",
                  (o[1], o[0], "0" * 64, o[2]))
        with pytest.raises(sqlite3.DatabaseError, match="LOOK_AHEAD"):                                                   # raw SQL: the schema refuses it
            c.execute("INSERT INTO mi_news_runs (run_id, run_kind, algorithm, map_sha256, as_of, computed_at, window_start, window_end, input_count, "
                      "input_sha256, output_count, output_sha256) VALUES ('r', 'MENTIONS', 'a', ?, '2025-12-02T13:10:00.000000Z', "
                      "'2025-12-02T14:00:00.000000Z', '2025-12-01T00:00:00.000000Z', '2025-12-03T00:00:00.000000Z', 1, 'x', 0, 'y')", (msha,))
        c.execute("ROLLBACK")
        c.execute("BEGIN")
        c.execute("INSERT INTO mi_news_run_inputs (run_id, natural_key, obs_id, revision, record_sha256, first_seen_at) VALUES ('rc', ?, ?, 1, ?, ?)",
                  (o[1], o[0], "0" * 64, "2025-12-02T13:05:00.000000Z"))
        c.execute("INSERT INTO mi_news_clusters (run_id, cluster_id, representative_key, member_count, first_seen_at) VALUES ('rc', 'nc', ?, 1, "
                  "'2025-12-02T13:05:00.000000Z')", (o[1],))
        c.execute("INSERT INTO mi_news_cluster_members (run_id, natural_key, cluster_id, representative, rule) VALUES ('rc', ?, 'nc', 1, NULL)", (o[1],))
        with pytest.raises(sqlite3.DatabaseError, match="LOOK_AHEAD"):                                                   # the input was available at 13:50
            c.execute("INSERT INTO mi_news_runs (run_id, run_kind, algorithm, map_sha256, as_of, computed_at, window_start, window_end, input_count, "
                      "input_sha256, output_count, output_sha256) VALUES ('rc', 'CLUSTERS', 'a', '', '2025-12-02T13:10:00.000000Z', "
                      "'2025-12-02T14:00:00.000000Z', '2025-12-01T00:00:00.000000Z', '2025-12-03T00:00:00.000000Z', 1, 'x', 1, 'y')")
        c.execute("ROLLBACK")
    finally:
        c.close()


def test_N03_mentions_are_recomputed_from_their_inputs_and_map_and_a_wrong_ticker_is_caught(tmp_path):
    s, clk = v2(tmp_path)
    t1 = TUE - timedelta(hours=2)
    put_news(s, clk, [dict(art(1, t1, headline="Nvidia (NASDAQ: NVDA) beats estimates; $AMD slips", symbols=("NVDA",)), source="Reuters",
                           url="https://www.reuters.com/markets/nvda-beats"),
                      art(2, t1, headline="All you can eat: CAT and DAY tagged", symbols=("CAT", "DAY")),
                      art(3, t1, headline="Market wrap", symbols=("A", "B", "C", "D", "E", "F"))], t1 + timedelta(minutes=1))
    clk.t = t1 + timedelta(minutes=5)
    msha = s.record_entity_map({"sec_names": {"NVDA": "NVIDIA CORP", "AMD": "ADVANCED MICRO DEVICES INC", "CAT": "CATERPILLAR INC"}, "universe": ["NVDA", "AMD"],
                                "aliases": {}})
    clk.tick(60)
    r = NP.mention_run(s, as_of=clk(), window_start=W0, window_end=W1, map_sha256=msha)
    got = {(m["natural_key"].rsplit("|", 1)[1], m["symbol"]): (m["method"], m["role"]) for m in s.news_run(r["run_id"])["mentions"]}
    assert got[("1", "NVDA")] == ("EXCHANGE_TAG", "PRIMARY") and got[("1", "AMD")] == ("CASHTAG", "PRIMARY")
    assert got[("2", "CAT")][0] == "UNVERIFIED" and got[("2", "DAY")][0] == "UNVERIFIED"                                 # common words never confirmed by a tag
    assert {v[1] for k, v in got.items() if k[0] == "3"} == {"ROUNDUP"}
    elig = {(m["natural_key"].rsplit("|", 1)[1], m["symbol"]): m["catalyst_eligible"] for m in s.news_run(r["run_id"])["mentions"]}
    assert elig[("1", "NVDA")] == 1 and sum(elig.values()) == 2                                                          # tier 3, primary, >= 0.8 only
    inputs = s.news_inputs(as_of=clk(), window_start=W0, window_end=W1)
    forged = [dict(m, symbol="TSLA") if m["symbol"] == "AMD" else m for m in NP.compute_mentions(inputs, s.entity_map(msha))]
    with pytest.raises(ST.StoreError, match="NEWS_OUTPUT_MISMATCH"):
        s.record_news_run(run_kind="MENTIONS", algorithm=NP.MENTION_ALGORITHM, map_sha256=msha, as_of=clk(), window_start=W0, window_end=TUE + timedelta(hours=13),
                          inputs=inputs, clusters=[], members=[], mentions=forged)
    assert s.verify_integrity()["news_mismatches"] == []
    tamper(s.path, "mi_news_mentions", "UPDATE mi_news_mentions SET symbol = 'TSLA' WHERE symbol = 'AMD'")
    assert {p["problem"] for p in s.verify_integrity()["news_mismatches"]} == {"OUTPUT_DIGEST"}
    run = s.news_run(r["run_id"])
    rows = [{k: v for k, v in m.items() if k != "run_id"} for m in run["mentions"]]
    tamper(s.path, "mi_news_runs", "UPDATE mi_news_runs SET output_sha256 = ? WHERE run_id = ?", (NP.output_sha256([], [], rows), r["run_id"]))
    v = s.verify_integrity()
    assert v["state"] == "CORRUPT" and {p["problem"] for p in v["news_mismatches"]} == {"MENTIONS_RECOMPUTE"}           # even with a matching digest


def test_N04_the_schema_refuses_dangling_mismatched_late_or_inconsistent_news_rows(tmp_path):
    s, clk = v2(tmp_path)
    t1 = TUE - timedelta(hours=2)
    put_news(s, clk, [art(1, t1), art(2, t1 + timedelta(minutes=1))], t1 + timedelta(minutes=1))
    clk.t = t1 + timedelta(minutes=10)
    r = NP.cluster_run(s, as_of=clk(), window_start=W0, window_end=W1)
    msha = s.record_entity_map({"sec_names": {}, "universe": [], "aliases": {}})
    c = raw(s.path)
    try:
        o = c.execute("SELECT obs_id, natural_key, revision, available_at FROM mi_observations ORDER BY natural_key LIMIT 1").fetchone()
        ins = "INSERT INTO mi_news_run_inputs (run_id, natural_key, obs_id, revision, record_sha256, first_seen_at) VALUES (?,?,?,?,?,?)"
        with pytest.raises(sqlite3.DatabaseError, match="NEWS_RUN_CLOSED"):
            c.execute(ins, (r["run_id"], o[1], o[0], 1, "0" * 64, o[3]))                                                 # a run takes nothing later
        c.execute("BEGIN")
        for args in (("r1", o[1], "missing-obs", 1, "0" * 64, o[3]), ("r1", o[1], o[0], 2, "0" * 64, o[3]), ("r1", "wrong-key", o[0], 1, "0" * 64, o[3])):
            with pytest.raises(sqlite3.DatabaseError, match="NEWS_INPUT_MISMATCH"):
                c.execute(ins, args)                                                                                     # a cluster input is a stored record
        c.execute("ROLLBACK")
        c.execute("BEGIN")
        c.execute(ins, ("r2", o[1], o[0], 1, "0" * 64, o[3]))
        with pytest.raises(sqlite3.DatabaseError, match="NEWS_RUN_INCONSISTENT"):
            c.execute("INSERT INTO mi_news_runs (run_id, run_kind, algorithm, map_sha256, as_of, computed_at, window_start, window_end, input_count, "
                      "input_sha256, output_count, output_sha256) VALUES ('r2', 'CLUSTERS', 'a', '', ?, ?, '2025-12-01T00:00:00.000000Z', "
                      "'2025-12-03T00:00:00.000000Z', 2, 'x', 0, 'y')", (ST.ts(clk()), ST.ts(clk())))
        c.execute("ROLLBACK")
        c.execute("BEGIN")
        c.execute(ins, ("r4", o[1], o[0], 1, "0" * 64, o[3]))
        with pytest.raises(sqlite3.DatabaseError, match="NEWS_RUN_INCONSISTENT"):
            c.execute("INSERT INTO mi_news_runs (run_id, run_kind, algorithm, map_sha256, as_of, computed_at, window_start, window_end, input_count, "
                      "input_sha256, output_count, output_sha256) VALUES ('r4', 'MENTIONS', 'a', ?, ?, ?, '2025-12-01T00:00:00.000000Z', "
                      "'2025-12-03T00:00:00.000000Z', 2, 'x', 0, 'y')", (msha, ST.ts(clk()), ST.ts(clk())))
        c.execute("ROLLBACK")
        with pytest.raises(sqlite3.DatabaseError, match="FOREIGN KEY"):                                                  # members reference stored inputs
            c.execute("INSERT INTO mi_news_cluster_members (run_id, natural_key, cluster_id, representative, rule) VALUES ('r3', 'k', 'nc', 1, NULL)")
    finally:
        c.close()
    run = s.news_run(r["run_id"])
    tamper(s.path, "mi_news_cluster_members", "UPDATE mi_news_cluster_members SET representative = 1 - representative WHERE run_id = ?", (r["run_id"],))
    rows = [{k: v for k, v in m.items() if k != "run_id"} for m in s.news_run(r["run_id"])["members"]]
    clusters = [{k: v for k, v in x.items() if k != "run_id"} for x in run["clusters"]]
    tamper(s.path, "mi_news_runs", "UPDATE mi_news_runs SET output_sha256 = ? WHERE run_id = ?", (NP.output_sha256(clusters, rows, []), r["run_id"]))
    assert {p["problem"] for p in s.verify_integrity()["news_mismatches"]} == {"CLUSTERS_RECOMPUTE"}                    # even with a matching digest
    tamper(s.path, "mi_observations", "UPDATE mi_observations SET payload_json = replace(payload_json, 'Nvidia', 'Forged') WHERE obs_id = ?", (o[0],))
    v = s.verify_integrity()
    assert v["state"] == "CORRUPT" and v["row_hash_mismatches"] and f"INPUT:{o[1]}" in {p["problem"] for p in v["news_mismatches"]}


# ============================================================================================================ lifecycle (L)
def test_L01_retention_classes_cover_every_table_and_the_primitives_only_read_or_copy(tmp_path, monkeypatch):
    tables = {k[1] for k in SV.expected_objects(2) if k[0] == "table"}
    from market_intel.collector import state as CS
    classed = [t for c in LC.RETENTION_CLASSES.values() for t in c["tables"]]
    assert sorted(classed) == sorted(set(classed)) and set(classed) == tables | set(CS.TABLES)                           # each table in exactly one class
    assert {k: v["policy"] for k, v in LC.RETENTION_CLASSES.items()}[LC.AUDIT] == "NEVER_DELETED" == LC.RETENTION_CLASSES[LC.FACT]["policy"]
    e = v1_world(tmp_path / "w")
    hot = sorted(p for p in e.archive.root.rglob("*") if p.is_file())
    sums = {p: sha_file(p) for p in hot}
    inv = LC.inventory(store_path=e.store.path, archive_root=e.archive.root, state_path=e.state.path)
    assert inv["store"]["rows"]["mi_datasets"] == len(e.store.dataset_states()["sealed"]) and inv["raw"]["files"] == len(e.archive.manifest())
    assert LC.inventory(store_path=tmp_path / "none.db", archive_root=tmp_path / "none")["store"]["rows"] == {} and not (tmp_path / "none.db").exists()
    with pytest.raises(TypeError):
        LC.archive_candidates(e.store, e.archive, now=e.clk())                                                           # no default period
    for bad in (-1, True, 1.5, None):
        with pytest.raises(ValueError):
            LC.archive_candidates(e.store, e.archive, older_than_days=bad, now=e.clk())
    recent = LC.archive_candidates(e.store, e.archive, older_than_days=30, now=e.clk())
    assert recent["eligible"] == [] and {x["reason"] for x in recent["excluded"]} == {"TOO_RECENT"}
    later = LC.archive_candidates(e.store, e.archive, older_than_days=1, now=e.clk() + timedelta(days=3))
    assert len(later["eligible"]) == len(e.archive.manifest()) and later["excluded"] == []
    victim = e.archive.root / later["eligible"][0]["path"]
    keep = victim.read_bytes()
    victim.write_bytes(gzip.compress(b"forged", mtime=0))
    assert {x["reason"] for x in LC.archive_candidates(e.store, e.archive, older_than_days=1, now=e.clk() + timedelta(days=3))["excluded"]} == {"BODY_MISMATCH"}
    victim.write_bytes(keep)
    cold = tmp_path / "cold"
    out = LC.export_raw(later["eligible"], e.archive, cold, now=e.clk())
    assert len(out["copied"]) == len(later["eligible"]) and LC.export_raw(later["eligible"], e.archive, cold, now=e.clk())["skipped"] == out["copied"]
    assert all((cold / x["path"]).read_bytes() == (e.archive.root / x["path"]).read_bytes() for x in later["eligible"])
    assert len((cold / "export_manifest.jsonl").read_text(encoding="utf-8").splitlines()) == len(out["copied"])
    (cold / later["eligible"][0]["path"]).write_bytes(b"different")
    with pytest.raises(FileExistsError):
        LC.export_raw(later["eligible"], e.archive, cold, now=e.clk())                                                   # never overwritten
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "proj")                                                      # a stand-in project root
    for dest in (e.archive.root / "cold", tmp_path / "proj" / "data" / "cold"):
        with pytest.raises(PermissionError):
            LC.export_raw(later["eligible"], e.archive, dest, now=e.clk())
    monkeypatch.undo()
    assert not (tmp_path / "proj").exists() and not (e.archive.root / "cold").exists()
    assert sorted(p for p in e.archive.root.rglob("*") if p.is_file()) == hot and all(sha_file(p) == sums[p] for p in hot)   # nothing deleted or edited
    s1 = ST.MarketIntelStore(e.store.path, clock=e.clk, approvals_root=tmp_path / "approvals")
    did = s1.create_dataset(fixture_spec())
    fid = s1.record_fetch(FetchRecord(provider="ALPACA", endpoint="/x", started_at=e.clk(), retrieved_at=e.clk(), status="OK"), dataset_id=did, params={"q": 1})
    e.archive.write(fid, b"{}", provider="ALPACA", endpoint="/x", retrieved_at=e.clk())
    pend = LC.archive_candidates(e.store, e.archive, older_than_days=1, now=e.clk() + timedelta(days=3))
    assert pend["eligible"] == [] and {x["reason"] for x in pend["excluded"]} == {"UNSEALED", "UNVERIFIED"}              # an unsealed run: nothing moves
    s1.finish_dataset(did, "FAILED")
    pend = LC.archive_candidates(e.store, e.archive, older_than_days=1, now=e.clk() + timedelta(days=3))
    assert {x["reason"] for x in pend["excluded"]} == {"UNANCHORED", "UNVERIFIED"}                                       # sealed, never anchored
    tamper(e.store.path, "mi_observations", "UPDATE mi_observations SET payload_json = '{}' WHERE rowid = 1")
    assert LC.archive_candidates(e.store, e.archive, older_than_days=1, now=e.clk() + timedelta(days=3))["refused"] == "STORE_CORRUPT"


def test_L02_disk_status_needs_an_explicit_threshold(tmp_path, monkeypatch):
    with pytest.raises(TypeError):
        LC.disk_status(tmp_path)
    for bad in (-1, True, 1.0):
        with pytest.raises(ValueError):
            LC.disk_status(tmp_path, min_free_bytes=bad)
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(LC.shutil, "disk_usage", lambda p: U(100, 90, 10))
    assert (LC.disk_status(tmp_path / "x" / "y", min_free_bytes=10)["state"], LC.disk_status(tmp_path, min_free_bytes=11)["state"]) == ("OK", "LOW")


# ============================================================================================================ self-review (V)
def test_V01_a_collect_dataset_takes_no_row_after_its_authorization_expires_and_recovery_seals_it_on_v2(tmp_path):
    from market_intel import ingest as IG
    from market_intel.archive import RawArchive
    s, clk = v2(tmp_path)
    did = s.create_dataset(collect_spec(), approval=load(tmp_path, auth_file(tmp_path), clk))                           # valid until 20:00
    f1 = ok_fetch(s, did, clk)
    assert s.ingest(did, news_obs([art(1, TUE - timedelta(minutes=5))], clk(), did), fetch_id=f1).inserted == 1
    clk.t = datetime(2025, 12, 2, 20, 0, tzinfo=UTC)
    with pytest.raises(CA.CollectAuthorizationError, match="COLLECT_AUTHORIZATION_EXPIRED"):
        ok_fetch(s, did, clk, {"p": 2})
    with pytest.raises(CA.CollectAuthorizationError, match="COLLECT_AUTHORIZATION_EXPIRED"):
        s.ingest(did, news_obs([art(2, TUE - timedelta(minutes=6))], clk(), did), fetch_id=f1)
    c = raw(s.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="COLLECT_AUTHORIZATION_EXPIRED"):                               # raw SQL: the schema refuses it
            c.execute("INSERT INTO mi_dataset_members (dataset_id, natural_key, fetch_id, disposition, at) VALUES (?, 'k', ?, 'REJECTED', ?)",
                      (did, f1, ST.ts(clk())))
    finally:
        c.close()
    clk.tick(2 * 3600)                                                                                                   # abandoned: nobody sealed it
    archive = RawArchive(tmp_path / "raw")
    out = IG.recover_pending(s, archive, clock=clk)
    assert out["sealed"] == [{"dataset_id": did, "status": "PARTIAL"}] and out["anchored"] == [did] and out["refused"] is None
    assert s.verify_integrity(anchors=archive.anchors())["state"] == "VERIFIED"                                        # the 7.1 recovery, unchanged, on v2


def test_V02_two_migrations_to_one_target_never_share_a_copy_and_a_target_is_never_overwritten(tmp_path, monkeypatch):
    e = v1_world(tmp_path / "w")
    src, anchors, target = e.store.path, e.archive.anchors(), tmp_path / "m" / "v2.db"
    results, gate = [], threading.Barrier(2)

    def run():
        gate.wait(10)
        try:
            results.append(SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)["post_integrity"]["state"])
        except SV.MigrationRefused as exc:
            results.append(exc.code)
    th = [threading.Thread(target=run) for _ in range(2)]
    for x in th:
        x.start()
    for x in th:
        x.join(60)
    assert results.count("VERIFIED") == 1 and set(results) - {"VERIFIED"} <= {"INTERRUPTED_MIGRATION_PRESENT", "TARGET_EXISTS"}, results
    assert MarketIntelStoreV2(target).verify_integrity(anchors=anchors)["state"] == "VERIFIED"
    race = tmp_path / "m5" / "v2.db"
    real_sha = SV._file_sha256

    def a_rival_claims_it(p):
        race.parent.mkdir(parents=True, exist_ok=True)
        Path(str(race) + ".partial").write_bytes(b"")                                                                    # a rival run, just after our check
        return real_sha(p)
    monkeypatch.setattr(SV, "_file_sha256", a_rival_claims_it)
    with pytest.raises(SV.MigrationRefused, match="another migration"):
        SV.migrate_copy(src, race, anchors=anchors, clock=e.clk)
    monkeypatch.setattr(SV, "_file_sha256", real_sha)
    assert not race.exists()
    real = SV._migrate_partial
    other = tmp_path / "m2" / "v2.db"

    def someone_else_writes_the_target(partial, a, now):
        rep = real(partial, a, now)
        other.write_bytes(b"not ours")
        return rep
    monkeypatch.setattr(SV, "_migrate_partial", someone_else_writes_the_target)
    with pytest.raises(SV.MigrationRefused, match="TARGET_EXISTS"):
        SV.migrate_copy(src, other, anchors=anchors, clock=e.clk)
    assert other.read_bytes() == b"not ours" and Path(str(other) + ".partial").exists()                                 # never overwritten; the copy kept


def names(d: Path) -> list:
    return sorted(x.name for x in d.iterdir())


def by_another_process(path: Path, data: str) -> None:
    """A separate process creates `path` (exclusively: it must be a new file) holding `data`."""
    subprocess.run([sys.executable, "-I", "-c", "import sys; open(sys.argv[1], 'xb').write(sys.argv[2].encode())", str(path), data], check=True, timeout=60)


def test_V03_promotion_never_overwrites_what_another_process_creates_just_before_it_and_never_falls_back(tmp_path, monkeypatch):
    """Operator review of 75b3051: the target was checked and then os.replace'd, so a file created in between was overwritten. Here
    another process creates the name immediately before the promotion call, whichever call that is (link, replace or rename)."""
    e = v1_world(tmp_path / "w")
    src, anchors = e.store.path, e.archive.anchors()
    real = {n: getattr(SV.os, n) for n in ("link", "replace", "rename")}
    calls = []

    def racing(at, data, after=False):
        def wrap(n):
            def call(a, b, *k, **kw):
                calls.append((n, Path(b).name))
                if not after and Path(b) == at:
                    by_another_process(at, data)                                                                         # just before the call
                out = real[n](a, b, *k, **kw)
                if after and Path(b).name == "v2.db":
                    by_another_process(at, data)                                                                         # just after the target's
                return out
            return call
        for n in real:
            monkeypatch.setattr(SV.os, n, wrap(n))
    t1 = tmp_path / "a" / "v2.db"
    racing(t1, "foreign target")
    with pytest.raises(SV.MigrationRefused, match="TARGET_EXISTS"):
        SV.migrate_copy(src, t1, anchors=anchors, clock=e.clk)
    monkeypatch.undo()
    assert t1.read_bytes() == b"foreign target" and calls == [("link", "v2.db")]                                       # never overwritten, one try
    assert names(t1.parent) == ["v2.db", "v2.db.partial"] and SV.status(Path(str(t1) + ".partial"))["state"] == "V2_VERIFIED"   # the copy kept
    with pytest.raises(SV.MigrationRefused, match="INTERRUPTED_MIGRATION_PRESENT"):
        SV.migrate_copy(src, t1, anchors=anchors, clock=e.clk)
    assert t1.read_bytes() == b"foreign target"
    t2 = tmp_path / "b" / "v2.db"
    r2 = Path(str(t2) + ".migration.json")
    racing(r2, "foreign report")
    with pytest.raises(SV.MigrationRefused, match="REPORT_EXISTS"):
        SV.migrate_copy(src, t2, anchors=anchors, clock=e.clk)
    monkeypatch.undo()
    assert r2.read_bytes() == b"foreign report" and SV.status(t2)["state"] == "V2_VERIFIED"                            # the report is never overwritten
    assert names(t2.parent) == ["v2.db", "v2.db.migration.json", "v2.db.migration.json.partial"]                     # its own report kept aside
    t3 = tmp_path / "c" / "v2.db"
    r3 = Path(str(t3) + ".migration.json.partial")
    racing(r3, "a rival report", after=True)
    with pytest.raises(SV.MigrationRefused, match="INTERRUPTED_MIGRATION_PRESENT"):
        SV.migrate_copy(src, t3, anchors=anchors, clock=e.clk)
    monkeypatch.undo()
    assert r3.read_bytes() == b"a rival report" and names(t3.parent) == ["v2.db", "v2.db.migration.json.partial"]    # no report: not completed

    def never(n):
        def call(*a, **kw):
            raise AssertionError(f"{n} must never be tried")
        return call
    for i, err in enumerate((errno.EPERM, errno.EXDEV, errno.EINVAL, errno.EACCES, errno.EOPNOTSUPP)):               # a file system without hard links
        t4 = tmp_path / f"d{i}" / "v2.db"
        def no_links(*a, err=err, **kw):
            raise OSError(err, os.strerror(err))
        monkeypatch.setattr(SV.os, "link", no_links)
        for mod, n in ((SV.os, "replace"), (SV.os, "rename"), (shutil, "copyfile"), (shutil, "copy"), (shutil, "copy2"), (shutil, "move")):
            monkeypatch.setattr(mod, n, never(n))
        with pytest.raises(SV.MigrationRefused, match="ATOMIC_PROMOTION_UNSUPPORTED"):
            SV.migrate_copy(src, t4, anchors=anchors, clock=e.clk)
        monkeypatch.undo()
        assert names(t4.parent) == ["v2.db.partial"], err                                                                # fail closed: no target at all

    def swapped(a, b, *k, **kw):                                                                                         # an identical copy, not our file
        real["link"](a, b, *k, **kw)
        if Path(b).name == "v2.db":
            shutil.copyfile(a, Path(b).with_name("theirs"))
            real["replace"](Path(b).with_name("theirs"), b)

    def changed(a, b, *k, **kw):                                                                                         # our file, other bytes
        real["link"](a, b, *k, **kw)
        if Path(b).name == "v2.db":
            with open(b, "ab") as f:
                f.write(b"x")
    for i, fake in enumerate((swapped, changed)):
        t5 = tmp_path / f"e{i}" / "v2.db"
        monkeypatch.setattr(SV.os, "link", fake)
        with pytest.raises(SV.MigrationRefused, match="PROMOTION_NOT_VERIFIED"):
            SV.migrate_copy(src, t5, anchors=anchors, clock=e.clk)
        monkeypatch.undo()
        assert names(t5.parent) == ["v2.db", "v2.db.partial"], fake.__name__                                             # no report: not completed
    text = (ROOT / "market_intel" / "schema_v2.py").read_text(encoding="utf-8")
    assert [w for w in ("os.replace", "os.rename", ".rename(", "shutil", ".write_bytes(") if w in text] == []


def test_V04_concurrent_migration_processes_promote_one_target_once_and_never_overwrite_it(tmp_path):
    e = v1_world(tmp_path / "w")
    src, target, data = e.store.path, tmp_path / "m" / "v2.db", names(ROOT / "data")
    cmd = [sys.executable, "-m", "market_intel.schema_v2", "migrate", "--source", str(src), "--target", str(target), "--archive-root", str(e.archive.root)]
    procs = [subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    outs = [x.communicate(timeout=300) for x in procs]
    codes = sorted(x.returncode for x in procs)
    assert codes == [0, 2, 2], (codes, outs)
    refused = {json.loads(o)["refused"] for (o, _), x in zip(outs, procs) if x.returncode == 2}
    assert refused <= {"INTERRUPTED_MIGRATION_PRESENT", "TARGET_EXISTS"}, outs
    rep = json.loads(Path(str(target) + ".migration.json").read_text(encoding="utf-8"))
    assert (rep["target"], rep["target_sha256"], rep["promotion"], rep["source_unchanged"]) == (str(target), sha_file(target), SV.PROMOTION, True)
    assert MarketIntelStoreV2(target).verify_integrity(anchors=e.archive.anchors())["state"] == "VERIFIED"
    left = names(target.parent)
    assert left in (["v2.db", "v2.db.migration.json"], ["v2.db", "v2.db.migration.json", "v2.db.partial"]), left        # a late loser keeps its copy
    if "v2.db.partial" in left:
        assert not os.path.samefile(target, Path(str(target) + ".partial"))
    assert names(ROOT / "data") == data and not ST.default_path().exists()


CRASH = r"""
import os, sys
from pathlib import Path
step, target = sys.argv[1], Path(sys.argv[2])
report, partial = Path(str(target) + ".migration.json"), Path(str(target) + ".partial")
real_link, real_unlink = os.link, os.unlink
def link(a, b, *k, **kw):
    if (step, Path(b)) in (("before_link", target), ("before_report_link", report)):
        os._exit(9)
    real_link(a, b, *k, **kw)
    if (step, Path(b)) in (("after_link", target), ("after_report_link", report)):
        os._exit(9)
def unlink(p, *k, **kw):
    real_unlink(p, *k, **kw)
    if (step, Path(p)) == ("after_unlink", partial):
        os._exit(9)
os.link, os.unlink = link, unlink
from market_intel import schema_v2 as SV
raise SystemExit(SV.main(["migrate", "--source", sys.argv[3], "--target", str(target), "--archive-root", sys.argv[4]]))
"""


@pytest.mark.parametrize("step, left, code", [
    ("before_link", ["v2.db.partial"], "INTERRUPTED_MIGRATION_PRESENT"),
    ("after_link", ["v2.db", "v2.db.partial"], "INTERRUPTED_MIGRATION_PRESENT"),
    ("after_unlink", ["v2.db"], "TARGET_EXISTS"),
    ("before_report_link", ["v2.db", "v2.db.migration.json.partial"], "INTERRUPTED_MIGRATION_PRESENT"),
    ("after_report_link", ["v2.db", "v2.db.migration.json", "v2.db.migration.json.partial"], "INTERRUPTED_MIGRATION_PRESENT"),
])
def test_V05_a_crash_at_every_promotion_step_fails_closed_and_recovers(tmp_path, step, left, code):
    e = v1_world(tmp_path / "w")
    src, anchors, target = e.store.path, e.archive.anchors(), tmp_path / "m" / "v2.db"
    before = sha_file(src)
    run = subprocess.run([sys.executable, "-c", CRASH, step, str(target), str(src), str(e.archive.root)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert run.returncode == 9 and names(target.parent) == left, (run.returncode, run.stdout, run.stderr, names(target.parent))   # killed, no cleanup
    if step == "after_link":
        assert os.path.samefile(target, Path(str(target) + ".partial"))                                                   # one file, two names
    report = Path(str(target) + ".migration.json")
    if report.exists():                                                                                                   # published last: complete
        rep = json.loads(report.read_text(encoding="utf-8"))
        assert step == "after_report_link" and rep["target_sha256"] == sha_file(target) and SV.status(target)["state"] == "V2_VERIFIED"
    snap = {x.name: sha_file(x) for x in target.parent.iterdir()}
    with pytest.raises(SV.MigrationRefused) as ei:
        SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)
    assert ei.value.code == code and {x.name: sha_file(x) for x in target.parent.iterdir()} == snap                       # refused, nothing touched
    if step == "after_report_link":
        Path(str(report) + ".partial").unlink()                                                                           # the operator: complete already
    else:
        for x in target.parent.iterdir():                                                                                 # the operator: an unreported
            x.unlink()                                                                                                    # target is not a migration
        rep = SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)
        assert json.loads(report.read_text(encoding="utf-8")) == rep and rep["post_integrity"]["state"] == "VERIFIED"
    assert names(target.parent) == ["v2.db", "v2.db.migration.json"] and sha_file(src) == before
    assert json.loads(report.read_text(encoding="utf-8"))["target_sha256"] == sha_file(target)


def test_V05b_a_stale_report_or_report_copy_is_refused_before_anything_is_copied(tmp_path):
    e = v1_world(tmp_path / "w")
    src, anchors = e.store.path, e.archive.anchors()
    for i, (leftover, code) in enumerate(((".migration.json", "REPORT_EXISTS"), (".migration.json.partial", "INTERRUPTED_MIGRATION_PRESENT"))):
        target = tmp_path / f"m{i}" / "v2.db"
        target.parent.mkdir(parents=True)
        Path(str(target) + leftover).write_bytes(b"stale")
        with pytest.raises(SV.MigrationRefused) as ei:
            SV.migrate_copy(src, target, anchors=anchors, clock=e.clk)
        assert ei.value.code == code and names(target.parent) == ["v2.db" + leftover] and Path(str(target) + leftover).read_bytes() == b"stale"


# ============================================================================================================ isolation (X)
NEW_MODULES_731 = ("schema_v2.py", "store_v2.py", "collect_auth.py", "lifecycle.py", "news/persist.py")
ALLOWED_731 = {"__future__", "argparse", "contextlib", "dataclasses", "datetime", "gzip", "hashlib", "json", "os", "re", "shutil", "sqlite3", "sys", "time",
               "typing", "uuid", "pathlib", "config", "api.validation", "market_intel", "market_intel.migrations", "market_intel.store", "market_intel.store_v2",
               "market_intel.models", "market_intel.archive", "market_intel.news", "market_intel.collector", "market_intel.news.persist"}


def test_X01_the_new_modules_reach_no_network_broker_trading_or_model_and_nothing_outside_imports_them():
    for rel in NEW_MODULES_731:
        src = (ROOT / "market_intel" / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                                    for a in n.names}
        assert names <= ALLOWED_731, (rel, sorted(names - ALLOWED_731))
        for word in ("urlopen", "urllib", "http.client", "import requests", "socket", "alpaca.data", "alpaca.trading", "net.fetch", "broker", "execution",
                     "proposals", "anthropic", "openai", "threading", "subprocess", "os.environ", "getenv", "submit_order", ".unlink(" if rel != "schema_v2.py" else "@@",
                     "rmtree", "os.remove"):
            assert word not in src, (rel, word)
    sv = (ROOT / "market_intel" / "schema_v2.py").read_text(encoding="utf-8")
    assert (sv.count(".unlink()"), sv.count("partial.unlink()"), sv.count("tmp.unlink()")) == (3, 2, 1)                  # its own .partial; a second name
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if not p.relative_to(ROOT).as_posix().startswith(("market_intel/", "tests/", ".venv/",
             "scratchpad/")) and "market_intel" in p.read_text(encoding="utf-8", errors="replace")]
    assert users == []
    assert not ST.default_path().exists() and not (ROOT / "runtime" / "market_intel" / "approvals").exists()


def test_X02_existing_gates_hold_unchanged_on_v2(tmp_path):
    s, clk = v2(tmp_path)
    with pytest.raises(PermissionError):
        s.create_dataset(ST.DatasetSpec(job_kind="BACKFILL", kind="BAR_1DAY", provider="ALPACA", feed="iex", adjustment="raw", symbols=["SPY"], timeframe="1Day",
                                        start_date=TUE.date() - timedelta(days=10), end_date=TUE.date() - timedelta(days=3)))   # the 7.1 approval, unchanged
    assert MG.SCHEMA_VERSION == 1 and SV.SCHEMA_VERSION == 2 and SV.ACCEPTED_PURPOSES == CA.ACCEPTED_PURPOSES == ("OFFLINE_VERIFICATION",)
    assert PL.PLANS["sec_filings"].blocked_reason.startswith("SEC_ACCEPTANCE_TZ_UNVERIFIED") and PL.PLANS["sec_facts"].blocked_reason
    from market_intel.collector import runner as RU
    assert RU.SINK_JOB_KIND == "FIXTURE" and TR.is_fake(TR.FakeTransport()) and not TR.is_fake(TR.NoTransport())
    did = s.create_dataset(ST.DatasetSpec(job_kind="IMPORT", kind="NEWS", provider="ALPACA", feed="none", adjustment="none", symbols=[]))
    with pytest.raises(ST.StoreError, match="BACKFILL_FLAG"):
        s.ingest(did, news_obs([art(1, TUE - timedelta(hours=1))], clk(), did))                                         # IMPORT keeps its 7.1 rule



# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_731}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_731} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def post(rel):
    """main after PR #45 (Stage 7.3.1 merged; the Stage 7.4 base): what Stage 7.3.1 left, whatever a later stage changed since."""
    run = subprocess.run(["git", "show", f"{FL.BASE_74}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_74} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


APP_EDITS_731 = {".github/ci/pytest_groups.json"}
FROZEN_DIRS_731 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                   "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                   "database", "data", "value_quality", "market_intel")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_731 == "ad60a0211a1e76de86e8abd5d8734921d9034a9a" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_731}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_731).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    stage = set(_git("diff", "--name-only", FL.BASE_731, FL.BASE_74).split())               # Stage 7.3.1 itself, between two fixed commits
    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}
    later = FL.NEW_74 | FL.EDITED_74                                                         # Stage 7.4, pinned by its own F tests
    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),
                                                           "missing": sorted((FL.NEW_731 | FL.EDITED_731 | later) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_731).split())
    assert {p for p in stage if p not in in_base} == FL.NEW_731 and {p for p in stage if p in in_base} == FL.EDITED_731
    assert FL.NEW_731 == {"DESIGN_731_MARKET_INTEL_SCHEMA_V2.md", "tests/test_market_intel_schema_v2_731.py"} | {f"market_intel/{m}" for m in NEW_MODULES_731}
    assert {p for p in FL.EDITED_731 if not p.startswith("tests/")} == APP_EDITS_731
    assert FL.SEMANTIC_731 == {} and FL.INSTRUMENTATION_731 == {}
    for d in FROZEN_DIRS_731:                                                                  # every existing module: the 7.1 store and migrations included
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert post(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):
        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook or dependency
    for rel in sorted(FL.NEW_731 | FL.EDITED_731):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in post(rel) and m54 not in post(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_market_intel():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    from market_intel.settings import settings as mi_settings
    assert mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert not [n for n in dir(config) if n.startswith(("MARKET_INTEL", "MI_", "ALPHAVANTAGE"))]          # no new key (config.py itself: F01)
    import setups as SU
    assert (SU.READY_CONFIDENCE, SU.READY_DEMAND, SU.EVIDENCE_MAX_AGE_DAYS) == (40, 60, 30)


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_731.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_731) == FL.EDITED_731 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.3.1 (market intelligence schema v2")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_72 + NEW_73 + NEW_731 plus this ledger.", " + NEW_72 + NEW_73 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_731 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_731: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.3.1 left
    assert set(new_led) == set(old)                                                         # no new ledger key
    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_731 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_731 = {
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('\nAPP_EDITS_73 = {".github/ci/pytest_groups.json"}\n',
         '\ndef post(rel):\n    """main after PR #44 (Stage 7.3 merged; the Stage 7.3.1 base): what Stage 7.3 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_731}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_731} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_73 = {".github/ci/pytest_groups.json"}\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_73).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_73, FL.BASE_731).split())               # Stage 7.3 itself, between two fixed commits\n    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731                                                       # Stage 7.3.1, pinned by its own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n                                                         "missing": sorted((FL.NEW_73 | FL.EDITED_73 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_73).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_73).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_73 and {p for p in changed if p in in_base} == FL.EDITED_73\n    assert FL.NEW_73 == {"DESIGN_73_MARKET_INTEL_COLLECTOR.md", "tests/test_market_intel_collector_73.py"} | {f"market_intel/{m}" for m in NEW_MODULES_73}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_73).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_73 and {p for p in stage if p in in_base} == FL.EDITED_73\n    assert FL.NEW_73 == {"DESIGN_73_MARKET_INTEL_COLLECTOR.md", "tests/test_market_intel_collector_73.py"} | {f"market_intel/{m}" for m in NEW_MODULES_73}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert now_text(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert post(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n'),
        ('    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook or dependency\n    for rel in sorted(FL.NEW_73 | FL.EDITED_73):\n',
         '    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt"):\n        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook or dependency\n    for rel in sorted(FL.NEW_73 | FL.EDITED_73):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_73.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_73.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_73) == FL.EDITED_73 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.3 (market intelligence collector")\n',
         '    assert set(REPLAY_73) == FL.EDITED_73 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.3 (market intelligence collector")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_73 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.3 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_73 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73                                                         # Stage 7.3, pinned by its own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n',
         '    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 | FL.NEW_73 if p.startswith("market_intel/") and p.endswith(".py")}   # Stages 7.2, 7.3: their own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n',
         '    present = {q.relative_to(ROOT / "market_intel").as_posix() for q in (ROOT / "market_intel").rglob("*.py")}\n    later = {p[len("market_intel/"):] for p in FL.NEW_72 | FL.NEW_73 | FL.NEW_731 if p.startswith("market_intel/") and p.endswith(".py")}   # 7.2, 7.3, 7.3.1: own S01\n    assert present == set(PURE_70) | set(NEW_MODULES) | later, sorted(present ^ (set(PURE_70) | set(NEW_MODULES) | later))   # declared, or it fails\n'),
        ('    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n',
         '    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
