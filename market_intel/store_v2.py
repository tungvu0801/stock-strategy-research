"""
market_intel/store_v2.py - Stage 7.3.1: the market-intelligence store on schema v2 (DESIGN_731_MARKET_INTEL_SCHEMA_V2.md).

MarketIntelStoreV2 is the Stage 7.1 MarketIntelStore - unchanged and reused - on a v2 file:
* EXPLICIT files only: a path is required (there is no default, so nothing is ever created at data/market_intel.db); a fresh file is
  created directly at v2; a v1 file is refused (SchemaUpgradeRequired: migrate a copy, schema_v2.migrate_copy); above v2 SchemaTooNew;
* FIXTURE, IMPORT and BACKFILL datasets behave exactly as in v1 (the 7.1 code paths, the 7.1 backfill approval);
* COLLECT datasets need a collect authorization the store re-reads and checks itself (collect_auth.check), claimed once in the
  transaction that creates the dataset; only the OFFLINE_VERIFICATION purpose exists (the schema refuses any other). Their rows carry
  the provenance of a successful fetch of their own dataset, are never backfilled, and are available exactly when retrieved; their caps
  come from the claim; their seal also covers the claim (a non-COLLECT seal is exactly the 7.1 seal, so migrated seals still verify);
* news intelligence: point-in-time inputs, closed and idempotent runs, as-of reads that never see a run computed later;
* verify_integrity = the 7.1 verdict plus the COLLECT claims and every news run recomputed (inputs, record hashes, digests, outputs).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from market_intel import calendar as CAL
from market_intel import collect_auth as CA
from market_intel import schema_v2 as SV
from market_intel import store as ST
from market_intel.models import Observation
from market_intel.news import persist as NP

COLLECT = SV.COLLECT


class PointInTimeError(ST.StoreError):
    """A news run or read that would use something not known at its as_of (LOOK_AHEAD, FUTURE_AS_OF)."""


def _news_input(r: sqlite3.Row, first_seen_at: str) -> dict:
    return {"natural_key": r["natural_key"], "obs_id": r["obs_id"], "revision": int(r["revision"]), "record_sha256": ST.record_sha256(r),
            "first_seen_at": first_seen_at, "provider": r["provider"], "source_identifier": r["source_identifier"], "published_at": r["published_at"],
            "source_url": r["source_url"], "tier": r["tier"], "payload": json.loads(r["payload_json"]), "observed_at": r["observed_at"]}


def _known_inputs(c: sqlite3.Connection, as_of: str, window_start: str, window_end: str) -> List[dict]:
    """Every NEWS article in the window, as known at as_of: its latest revision then known (kept only when usable), first seen = the
    earliest then-known availability. A revision ingested or available after as_of does not exist yet."""
    rows = c.execute("SELECT * FROM mi_observations WHERE kind = 'NEWS' AND observed_at >= ? AND observed_at < ? AND available_at <= ? AND "
                     "ingested_at <= ? ORDER BY natural_key, revision", (window_start, window_end, as_of, as_of)).fetchall()
    latest: Dict[str, sqlite3.Row] = {}
    first: Dict[str, str] = {}
    for r in rows:
        k = r["natural_key"]
        latest[k] = r
        first[k] = min(first.get(k, r["available_at"]), r["available_at"])
    return [_news_input(r, first[k]) for k, r in sorted(latest.items()) if r["usable"]]


class MarketIntelStoreV2(ST.MarketIntelStore):
    def __init__(self, path, *, clock=None, busy_timeout_ms: int = ST.BUSY_TIMEOUT_MS, approvals_root=None):
        if path is None:
            raise ValueError("a v2 store names its file explicitly (Stage 7.3.1 has no default v2 path)")
        super().__init__(Path(path), clock=clock, busy_timeout_ms=busy_timeout_ms, approvals_root=approvals_root)

    # ------------------------------------------------------------------------------------------------ connections (v2 only)
    @contextmanager
    def _ro(self) -> Iterator[Optional[sqlite3.Connection]]:
        if not self.exists():
            yield None
            return
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            yield None if SV.verify(conn) == 0 else conn
        finally:
            conn.close()

    @contextmanager
    def _rw(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            self._wal(conn)
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if SV.verify(conn) == 0:                                # a fresh, empty file: created directly at v2 (never an upgrade)
                    SV.create(conn, self._now())
                    SV.verify(conn)
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _wal(self, conn: sqlite3.Connection) -> None:
        """WAL once, when the file is not in WAL yet. Switching takes an exclusive lock that SQLite does not wait for, so writers racing to
        create a new file retry within the busy timeout instead of failing (the 7.1 store sets it on every open: DESIGN_731 section 5)."""
        import time
        deadline = time.monotonic() + self.busy_timeout_ms / 1000.0
        while conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    # ------------------------------------------------------------------------------------------------ COLLECT
    def create_dataset(self, spec: ST.DatasetSpec, *, approval=None) -> str:
        if spec.job_kind != COLLECT:
            if isinstance(approval, CA.CollectAuthorization):
                raise ST.ScopeError("only a COLLECT dataset carries a collect authorization")
            return super().create_dataset(spec, approval=approval)      # FIXTURE / IMPORT / BACKFILL: the Stage 7.1 path, unchanged
        if not isinstance(approval, CA.CollectAuthorization):
            raise CA.CollectAuthorizationError("COLLECT_NEEDS_AUTHORIZATION: a COLLECT dataset needs a collect authorization "
                                               "(a backfill approval, or none, is refused)")
        problems = ST.validate_spec(replace(spec, job_kind="FIXTURE"))  # every field rule of v1; job_kind is checked here
        if problems:
            if any("feed" in p for p in problems) and spec.kind in ST.FEED_KINDS:
                raise ST.FeedRequired("; ".join(problems))
            raise ST.ScopeError("invalid COLLECT dataset spec: " + "; ".join(problems))
        if spec.kind in ST.SESSION_KINDS and spec.start_date and spec.end_date:
            x = spec.start_date
            while x <= spec.end_date:
                if CAL.market_day_status(x) == CAL.NOT_COVERED:
                    raise ST.CoverageError(f"{x} is outside the calendar coverage: a dataset range must be covered")
                x += timedelta(days=1)
        now = self._now()
        CA.check(approval, spec, now=now, root=self.approvals_root)
        did, at = uuid.uuid4().hex, ST.ts(now)
        with self._rw() as c:
            try:
                c.execute("INSERT INTO mi_collect_authorization_uses (authorization_id, dataset_id, purpose, source, scope_key, authorization_file_sha256, "
                          "used_at, expires_at, max_requests, max_rows) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (approval.authorization_id, did, approval.purpose, approval.source, approval.scope_key, approval.file_sha256, at,
                           ST.ts(approval.expires_at), int(approval.max_requests), int(approval.max_rows)))
            except sqlite3.IntegrityError as exc:
                raise ST.StoreError(f"collect authorization {approval.authorization_id} was already used: an authorization runs once") from exc
            c.execute("""INSERT INTO mi_datasets (dataset_id, job_kind, kind, provider, feed, adjustment, timeframe, symbols_json, start_date,
                         end_date, request_json, plan_sha256, approval_ref, created_at, code_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (did, COLLECT, spec.kind, spec.provider, spec.feed, spec.adjustment, spec.timeframe, ST.canonical(sorted(spec.symbols)),
                       spec.start_date.isoformat() if spec.start_date else None, spec.end_date.isoformat() if spec.end_date else None,
                       ST.canonical(spec.request), spec.plan_sha256, spec.approval_ref, at, f"{ST.VERSION}/schema{SV.SCHEMA_VERSION}"))
            c.execute("INSERT INTO mi_dataset_events (dataset_id, status, at) VALUES (?, 'RUNNING', ?)", (did, at))
        return did

    def _job_kind(self, dataset_id: str) -> Optional[str]:
        with self._ro() as c:
            r = None if c is None else c.execute("SELECT job_kind FROM mi_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return None if r is None else r["job_kind"]

    def _collect_live(self, dataset_id: Optional[str]) -> None:
        """A COLLECT dataset takes rows only while its authorization is valid (the schema refuses them too); it can always be sealed."""
        if dataset_id is None:
            return
        with self._ro() as c:
            u = None if c is None else c.execute("SELECT expires_at FROM mi_collect_authorization_uses WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if u is not None and ST.ts(self._now()) >= u["expires_at"]:
            raise CA.CollectAuthorizationError("COLLECT_AUTHORIZATION_EXPIRED: this COLLECT dataset's authorization expired; seal it")

    def record_fetch(self, fetch, *, dataset_id: Optional[str], params: Optional[dict] = None, raw_path: Optional[str] = None,
                     fetch_id: Optional[str] = None) -> str:
        self._collect_live(dataset_id)
        return super().record_fetch(fetch, dataset_id=dataset_id, params=params, raw_path=raw_path, fetch_id=fetch_id)

    def record_problems(self, dataset_id: str, problems, *, fetch_id: str) -> int:
        if problems:
            self._collect_live(dataset_id)
        return super().record_problems(dataset_id, problems, fetch_id=fetch_id)

    def ingest(self, dataset_id: str, observations, *, fetch_id: Optional[str] = None, on_reject: str = "raise") -> ST.IngestResult:
        if fetch_id is None and self._job_kind(dataset_id) == COLLECT:
            raise ST.ScopeError("a COLLECT row needs the fetch that delivered it (provenance): ingest(..., fetch_id=...)")
        self._collect_live(dataset_id)
        return super().ingest(dataset_id, observations, fetch_id=fetch_id, on_reject=on_reject)

    @staticmethod
    def _scope_reason(o: Observation, ds: sqlite3.Row, dataset_id: str):
        bad = ST.MarketIntelStore._scope_reason(o, ds, dataset_id)
        if bad is None and ds["job_kind"] == COLLECT:
            if o.backfilled:
                return ST.StoreError, "COLLECT_BACKFILLED"
            if o.available_at != o.retrieved_at:
                return ST.StoreError, "COLLECT_AVAILABILITY"
        return bad

    @staticmethod
    def _caps(c: sqlite3.Connection, dataset_id: str):
        use = c.execute("SELECT max_requests, max_rows FROM mi_collect_authorization_uses WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if use is not None:
            return int(use["max_requests"]), min(int(use["max_rows"]), ST.MAX_DATASET_ROWS)
        return ST.MarketIntelStore._caps(c, dataset_id)

    @staticmethod
    def _metadata_sha256(c: sqlite3.Connection, dataset_id: str, status: str, counts: Dict[str, int]) -> str:
        base = ST.MarketIntelStore._metadata_sha256(c, dataset_id, status, counts)
        use = c.execute("SELECT * FROM mi_collect_authorization_uses WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if use is None:
            return base                                                 # exactly the Stage 7.1 seal: migrated datasets verify unchanged
        return hashlib.sha256(ST.canonical({"v1": base, "collect_authorization": ST.record_sha256(use)}).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------------------------------------ news intelligence
    def record_entity_map(self, entity_map: dict) -> str:
        """Store an entity map (idempotent) and return its SHA-256. Its recorded_at bounds which runs may use it (point in time)."""
        m = NP.normalize_map(entity_map)
        sha = NP.sha256_of(m)
        with self._rw() as c:
            if c.execute("SELECT 1 FROM mi_news_entity_maps WHERE map_sha256 = ?", (sha,)).fetchone() is None:
                c.execute("INSERT INTO mi_news_entity_maps (map_sha256, map_json, recorded_at) VALUES (?,?,?)", (sha, NP.canonical(m), ST.ts(self._now())))
        return sha

    def entity_map(self, map_sha256: str, *, as_of: Optional[datetime] = None) -> dict:
        with self._ro() as c:
            r = None if c is None else c.execute("SELECT * FROM mi_news_entity_maps WHERE map_sha256 = ?", (map_sha256,)).fetchone()
        if r is None:
            raise ST.StoreError(f"unknown entity map {map_sha256}")
        if as_of is not None and r["recorded_at"] > ST.ts(as_of):
            raise PointInTimeError("LOOK_AHEAD: the entity map was recorded after as_of")
        return json.loads(r["map_json"])

    def news_inputs(self, *, as_of: datetime, window_start: datetime, window_end: datetime) -> List[dict]:
        with self._ro() as c:
            return [] if c is None else _known_inputs(c, ST.ts(as_of), ST.ts(window_start), ST.ts(window_end))

    def record_news_run(self, *, run_kind: str, algorithm: str, map_sha256: str, as_of: datetime, window_start: datetime, window_end: datetime,
                        inputs: Sequence[dict], clusters: Sequence[dict], members: Sequence[dict], mentions: Sequence[dict]) -> dict:
        """One CLOSED run: its inputs and outputs, then the run row, in one transaction (the schema refuses anything inconsistent or not
        known at as_of). Idempotent: the same kind, algorithm, map, window and inputs return the run already recorded."""
        now = self._now()
        if as_of > now:
            raise PointInTimeError("FUTURE_AS_OF: a run is never computed as of a time after now")
        if not window_start < window_end:
            raise ST.StoreError("a news run window has start < end")
        a, s, e = ST.ts(as_of), ST.ts(window_start), ST.ts(window_end)
        in_sha, out_sha = NP.input_sha256(inputs), NP.output_sha256(clusters, members, mentions)
        want_algo = {"CLUSTERS": NP.CLUSTER_ALGORITHM, "MENTIONS": NP.MENTION_ALGORITHM}.get(run_kind)
        if algorithm != want_algo:
            raise ST.StoreError(f"a {run_kind} run is computed by {want_algo}, not {algorithm!r}")
        with self._rw() as c:
            known = _known_inputs(c, a, s, e)                            # re-derived here: a stale, partial or future input set is refused
            key = lambda xs: [(x["natural_key"], x["obs_id"], int(x["revision"]), x["record_sha256"], x["first_seen_at"]) for x in xs]  # noqa: E731
            if sorted(key(inputs)) != sorted(key(known)):
                raise PointInTimeError("NEWS_INPUTS_NOT_KNOWN_SET: a run reads exactly the articles known at its as_of (latest known revision each)")
            if run_kind == "CLUSTERS":
                want = NP.output_sha256(*NP.compute_clusters(known), [])
            else:
                m = c.execute("SELECT * FROM mi_news_entity_maps WHERE map_sha256 = ?", (map_sha256,)).fetchone()
                if m is None or m["recorded_at"] > a:
                    raise PointInTimeError("LOOK_AHEAD: a mention run's entity map is recorded at or before its as_of")
                want = NP.output_sha256([], [], NP.compute_mentions(known, json.loads(m["map_json"])))
            if out_sha != want:
                raise ST.StoreError("NEWS_OUTPUT_MISMATCH: the stated clusters / mentions are not what the inputs give")
            old = c.execute("SELECT run_id FROM mi_news_runs WHERE run_kind = ? AND algorithm = ? AND map_sha256 = ? AND window_start = ? AND "
                            "window_end = ? AND input_sha256 = ?", (run_kind, algorithm, map_sha256, s, e, in_sha)).fetchone()
            if old is not None:
                return {"run_id": old["run_id"], "status": "DUPLICATE", "input_sha256": in_sha}
            rid = uuid.uuid4().hex
            for i in inputs:
                c.execute("INSERT INTO mi_news_run_inputs (run_id, natural_key, obs_id, revision, record_sha256, first_seen_at) VALUES (?,?,?,?,?,?)",
                          (rid, i["natural_key"], i["obs_id"], int(i["revision"]), i["record_sha256"], i["first_seen_at"]))
            for k in clusters:
                c.execute("INSERT INTO mi_news_clusters (run_id, cluster_id, representative_key, member_count, first_seen_at, first_published_at) "
                          "VALUES (?,?,?,?,?,?)", (rid, k["cluster_id"], k["representative_key"], int(k["member_count"]), k["first_seen_at"],
                                                   k["first_published_at"]))
            for m in members:
                c.execute("INSERT INTO mi_news_cluster_members (run_id, natural_key, cluster_id, representative, rule) VALUES (?,?,?,?,?)",
                          (rid, m["natural_key"], m["cluster_id"], int(m["representative"]), m["rule"]))
            for m in mentions:
                c.execute("INSERT INTO mi_news_mentions (run_id, natural_key, symbol, method, confidence, role, in_headline, catalyst_eligible) "
                          "VALUES (?,?,?,?,?,?,?,?)", (rid, m["natural_key"], m["symbol"], m["method"], float(m["confidence"]), m["role"],
                                                       int(m["in_headline"]), int(m["catalyst_eligible"])))
            c.execute("INSERT INTO mi_news_runs (run_id, run_kind, algorithm, map_sha256, as_of, computed_at, window_start, window_end, input_count, "
                      "input_sha256, output_count, output_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, run_kind, algorithm, map_sha256, a, ST.ts(now), s, e, len(inputs), in_sha,
                       len(clusters) if run_kind == "CLUSTERS" else len(mentions), out_sha))
        return {"run_id": rid, "status": "INSERTED", "input_sha256": in_sha}

    def news_run(self, run_id: str) -> Optional[dict]:
        with self._ro() as c:
            if c is None:
                return None
            r = c.execute("SELECT * FROM mi_news_runs WHERE run_id = ?", (run_id,)).fetchone()
            if r is None:
                return None
            q = lambda t, order: [dict(x) for x in c.execute(f"SELECT * FROM {t} WHERE run_id = ? ORDER BY {order}", (run_id,))]  # noqa: E731
            return {**dict(r), "inputs": q("mi_news_run_inputs", "natural_key"), "clusters": q("mi_news_clusters", "cluster_id"),
                    "members": q("mi_news_cluster_members", "natural_key"), "mentions": q("mi_news_mentions", "natural_key, symbol")}

    def news_run_as_of(self, run_kind: str, at: datetime, *, window_start: datetime, window_end: datetime, map_sha256: str = "",
                       algorithm: Optional[str] = None) -> Optional[dict]:
        """The run a reader at `at` could have used: as_of <= at AND computed_at <= at (never a run computed later), the latest such."""
        algorithm = algorithm or (NP.CLUSTER_ALGORITHM if run_kind == "CLUSTERS" else NP.MENTION_ALGORITHM)
        t = ST.ts(at)
        with self._ro() as c:
            r = None if c is None else c.execute(
                "SELECT run_id FROM mi_news_runs WHERE run_kind = ? AND algorithm = ? AND map_sha256 = ? AND window_start = ? AND window_end = ? AND "
                "as_of <= ? AND computed_at <= ? ORDER BY as_of DESC, computed_at DESC, run_id LIMIT 1",
                (run_kind, algorithm, map_sha256, ST.ts(window_start), ST.ts(window_end), t, t)).fetchone()
        return None if r is None else self.news_run(r["run_id"])

    # ------------------------------------------------------------------------------------------------ integrity
    def verify_integrity(self, *, anchors: Optional[Dict[str, dict]] = None) -> dict:
        out = super().verify_integrity(anchors=anchors)
        if out.get("store") == "SCHEMA_ERROR" and str(out.get("schema_error", "")).startswith("SchemaUpgradeRequired"):
            return {**out, "store": "UPGRADE_REQUIRED", "state": ST.UNVERIFIED, "ok": False}
        extra = {"collect_mismatches": [], "news_mismatches": []}
        if out.get("store") == "OK":
            with self._ro() as c:
                if c is not None:
                    self._verify_collect(c, extra)
                    self._verify_news(c, extra)
        out.update(extra)
        if any(extra.values()):
            out["state"], out["ok"] = ST.CORRUPT, False
        return out

    @staticmethod
    def _verify_collect(c: sqlite3.Connection, out: dict) -> None:
        for d in c.execute("SELECT d.dataset_id, d.job_kind, d.approval_ref, (SELECT COUNT(*) FROM mi_collect_authorization_uses u WHERE "
                           "u.dataset_id = d.dataset_id AND u.authorization_id = d.approval_ref) AS own, (SELECT COUNT(*) FROM "
                           "mi_collect_authorization_uses u WHERE u.dataset_id = d.dataset_id) AS any_claim FROM mi_datasets d"):
            if d["job_kind"] == COLLECT and (d["own"] != 1 or d["any_claim"] != 1):
                out["collect_mismatches"].append({"dataset_id": d["dataset_id"], "problem": "CLAIM"})
            if d["job_kind"] != COLLECT and d["any_claim"]:
                out["collect_mismatches"].append({"dataset_id": d["dataset_id"], "problem": "CLAIM_ON_OTHER_KIND"})
        for u in c.execute("SELECT u.dataset_id FROM mi_collect_authorization_uses u WHERE NOT EXISTS (SELECT 1 FROM mi_datasets d WHERE "
                           "d.dataset_id = u.dataset_id)"):
            out["collect_mismatches"].append({"dataset_id": u["dataset_id"], "problem": "ORPHAN_CLAIM"})
        for r in c.execute("SELECT m.dataset_id FROM mi_dataset_members m JOIN mi_datasets d ON d.dataset_id = m.dataset_id WHERE d.job_kind = 'COLLECT' "
                           "AND (m.fetch_id IS NULL OR NOT EXISTS (SELECT 1 FROM mi_fetches f WHERE f.fetch_id = m.fetch_id AND f.dataset_id = m.dataset_id))"):
            out["collect_mismatches"].append({"dataset_id": r["dataset_id"], "problem": "PROVENANCE:mi_dataset_members"})
        for t in ("mi_observations", "mi_absences"):
            for r in c.execute(f"SELECT t.dataset_id, t.natural_key FROM {t} t JOIN mi_datasets d ON d.dataset_id = t.dataset_id WHERE "
                               f"d.job_kind = 'COLLECT' AND (t.fetch_id IS NULL OR t.backfilled != 0 OR t.available_at != t.retrieved_at OR NOT EXISTS "
                               f"(SELECT 1 FROM mi_fetches f WHERE f.fetch_id = t.fetch_id AND f.dataset_id = t.dataset_id AND f.status IN ('OK', 'NOT_MODIFIED')))"):
                out["collect_mismatches"].append({"dataset_id": r["dataset_id"], "problem": f"PROVENANCE:{t}"})

    @staticmethod
    def _verify_news(c: sqlite3.Connection, out: dict) -> None:
        bad = out["news_mismatches"]
        for t in ("mi_news_run_inputs", "mi_news_clusters", "mi_news_cluster_members", "mi_news_mentions"):
            n = c.execute(f"SELECT COUNT(*) FROM {t} x WHERE NOT EXISTS (SELECT 1 FROM mi_news_runs r WHERE r.run_id = x.run_id)").fetchone()[0]
            if n:
                bad.append({"table": t, "problem": "ORPHAN_ROWS", "rows": n})
        for m in c.execute("SELECT * FROM mi_news_entity_maps"):
            if NP.sha256_of(json.loads(m["map_json"])) != m["map_sha256"]:
                bad.append({"map_sha256": m["map_sha256"], "problem": "MAP_HASH"})
        for run in c.execute("SELECT * FROM mi_news_runs ORDER BY computed_at, run_id").fetchall():
            rid = run["run_id"]
            stored = [dict(x) for x in c.execute("SELECT * FROM mi_news_run_inputs WHERE run_id = ? ORDER BY natural_key", (rid,))]
            known = {i["natural_key"]: i for i in _known_inputs(c, run["as_of"], run["window_start"], run["window_end"])}
            problems = []
            for s in stored:
                k = known.get(s["natural_key"])
                if k is None or (k["obs_id"], k["revision"], k["record_sha256"], k["first_seen_at"]) != (s["obs_id"], s["revision"], s["record_sha256"],
                                                                                                         s["first_seen_at"]):
                    problems.append(f"INPUT:{s['natural_key']}")
            if len(stored) != len(known) and not problems:
                problems.append("INPUT_SET")
            inputs = [known[s["natural_key"]] for s in stored if s["natural_key"] in known]
            if run["input_count"] != len(stored) or NP.input_sha256(stored) != run["input_sha256"]:
                problems.append("INPUT_DIGEST")
            clusters = [{k: v for k, v in dict(x).items() if k != "run_id"} for x in c.execute("SELECT * FROM mi_news_clusters WHERE run_id = ?", (rid,))]
            members = [{k: v for k, v in dict(x).items() if k != "run_id"} for x in c.execute("SELECT * FROM mi_news_cluster_members WHERE run_id = ?", (rid,))]
            mentions = [{k: v for k, v in dict(x).items() if k != "run_id"} for x in c.execute("SELECT * FROM mi_news_mentions WHERE run_id = ?", (rid,))]
            if NP.output_sha256(clusters, members, mentions) != run["output_sha256"]:
                problems.append("OUTPUT_DIGEST")
            if not problems:
                if run["run_kind"] == "CLUSTERS":
                    want_c, want_m = NP.compute_clusters(inputs)
                    if NP.output_sha256(want_c, want_m, []) != run["output_sha256"]:
                        problems.append("CLUSTERS_RECOMPUTE")
                else:
                    m = c.execute("SELECT * FROM mi_news_entity_maps WHERE map_sha256 = ?", (run["map_sha256"],)).fetchone()
                    if m is None or m["recorded_at"] > run["as_of"]:
                        problems.append("MAP_NOT_KNOWN_AT_AS_OF")
                    elif NP.output_sha256([], [], NP.compute_mentions(inputs, json.loads(m["map_json"]))) != run["output_sha256"]:
                        problems.append("MENTIONS_RECOMPUTE")
            if run["as_of"] > run["computed_at"]:
                problems.append("AS_OF_AFTER_COMPUTED")
            for p in problems:
                bad.append({"run_id": rid, "problem": p})
