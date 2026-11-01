"""
market_intel/lifecycle.py - Stage 7.3.1: the data lifecycle - retention classes and SAFE, OFFLINE primitives (DESIGN_731 sections 4 and 8).

Nothing here deletes, truncates, compacts or edits anything, and no retention period has a default: every period and threshold is an
operator decision passed in explicitly.

  RETENTION_CLASSES          what each kind of record is, and the policy proposed for it (audit evidence and facts: never deleted)
  inventory(...)             rows and bytes per class - read only
  archive_candidates(...)    a DRY RUN: the raw bodies that would be eligible for cold storage after `older_than_days`, each other body with
                             the reason it is not (UNSEALED, UNANCHORED, UNVERIFIED, BODY_MISMATCH, MISSING, TOO_RECENT)
  export_raw(...)            byte-identical COPIES of eligible bodies into a cold-archive directory, each re-verified, with an append-only
                             export manifest; never inside the hot archive or the project's data/; the sources are never touched; idempotent
  disk_status(...)           free space against an explicit threshold: OK or LOW (a later wiring stage decides what LOW stops)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

AUDIT, FACT, RAW, DERIVED, STATE = "AUDIT_EVIDENCE", "FACT", "RAW_PAYLOAD", "DERIVED", "COLLECTOR_STATE"
RETENTION_CLASSES: Dict[str, dict] = {
    AUDIT: {"tables": ("mi_schema_migrations", "mi_datasets", "mi_dataset_events", "mi_dataset_members", "mi_approval_uses",
                       "mi_collect_authorization_uses", "mi_fetches", "mi_budget_events", "mi_quality_audits"),
            "files": ("anchors.jsonl", "manifest.jsonl"), "policy": "NEVER_DELETED", "archivable": False},
    FACT: {"tables": ("mi_observations", "mi_absences"), "files": (), "policy": "NEVER_DELETED", "archivable": False},
    RAW: {"tables": (), "files": ("YYYY-MM-DD/<fetch_id>.json.gz",), "policy": "ARCHIVABLE_AFTER_VERIFIED_SEAL", "archivable": True},
    DERIVED: {"tables": ("mi_news_entity_maps", "mi_news_runs", "mi_news_run_inputs", "mi_news_clusters", "mi_news_cluster_members", "mi_news_mentions"),
              "files": (), "policy": "KEPT_RECOMPUTABLE", "archivable": False},
    STATE: {"tables": ("cs_runs", "cs_run_ends", "cs_watermarks", "cs_leases", "cs_lease_releases", "cs_breaker_events"), "files": (),
            "policy": "KEPT_APPEND_ONLY", "archivable": False},
}
EXCLUSIONS = ("UNSEALED", "UNANCHORED", "UNVERIFIED", "BODY_MISMATCH", "MISSING", "TOO_RECENT", "NO_DATASET")


def _size(p: Path) -> int:
    return sum(q.stat().st_size for q in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")) if q.is_file())


def _counts(path: Path, tables) -> Dict[str, int]:
    import sqlite3
    if not path.is_file():
        return {}
    c = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if t in have}
    finally:
        c.close()


def inventory(*, store_path, archive_root, state_path=None) -> dict:
    """Rows per table and bytes per class (store file, raw archive by day, collector state). Read only: creates nothing."""
    store_path, archive_root = Path(store_path), Path(archive_root)
    tables = {t for k in (AUDIT, FACT, DERIVED) for t in RETENTION_CLASSES[k]["tables"]}
    raw_days: Dict[str, dict] = {}
    if archive_root.is_dir():
        for day in sorted(p for p in archive_root.iterdir() if p.is_dir()):
            files = list(day.glob("*.json.gz"))
            raw_days[day.name] = {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}
    out = {"store": {"bytes": _size(store_path), "rows": _counts(store_path, sorted(tables))},
           "raw": {"days": raw_days, "files": sum(d["files"] for d in raw_days.values()), "bytes": sum(d["bytes"] for d in raw_days.values()),
                   "manifest_bytes": (archive_root / "manifest.jsonl").stat().st_size if (archive_root / "manifest.jsonl").is_file() else 0,
                   "anchors_bytes": (archive_root / "anchors.jsonl").stat().st_size if (archive_root / "anchors.jsonl").is_file() else 0}}
    if state_path is not None:
        out["collector_state"] = {"bytes": _size(Path(state_path)), "rows": _counts(Path(state_path), RETENTION_CLASSES[STATE]["tables"])}
    return out


def archive_candidates(store, archive, *, older_than_days: int, now: datetime) -> dict:
    """DRY RUN. A raw body is eligible when its fetch's dataset is sealed, anchored with the seal the store holds, the store verifies
    without a CORRUPT finding, the body re-hashes to its manifest entry, and its retrieval day is older than `older_than_days` (required)."""
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < 0:
        raise ValueError("older_than_days is an explicit, non-negative number of days (an operator decision: no default)")
    v = store.verify_integrity(anchors=archive.anchors())
    if v["state"] == "CORRUPT":
        return {"eligible": [], "excluded": [], "refused": "STORE_CORRUPT"}
    sealed = {d["dataset_id"]: d for d in store.dataset_states()["sealed"]}
    anchors = archive.anchors()
    dataset_of = _fetch_datasets(store)
    cutoff = (now.astimezone(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
    eligible, excluded = [], []
    for e in archive.manifest():
        did = dataset_of.get(e["fetch_id"])
        why = None
        if did is None:
            why = "NO_DATASET"
        elif did not in sealed:
            why = "UNSEALED"
        elif did not in anchors or any(anchors[did].get(k) != sealed[did][k] for k in ("content_sha256", "records_sha256", "metadata_sha256")):
            why = "UNANCHORED"
        elif e["path"].split("/")[0] >= cutoff:
            why = "TOO_RECENT"
        else:
            p = archive.root / e["path"]
            if not p.is_file():
                why = "MISSING"
            else:
                try:
                    ok = hashlib.sha256(gzip.decompress(p.read_bytes())).hexdigest() == e["sha256"]
                except (OSError, EOFError):
                    ok = False
                why = None if ok else "BODY_MISMATCH"
        (excluded if why else eligible).append({**e, "dataset_id": did, **({"reason": why} if why else {})})
    if v["state"] != "VERIFIED":
        for e in eligible:
            excluded.append({**e, "reason": "UNVERIFIED"})
        eligible = []
    return {"eligible": eligible, "excluded": excluded, "refused": None, "older_than_days": older_than_days, "cutoff_day": cutoff}


def _fetch_datasets(store) -> Dict[str, str]:
    with store._ro() as c:
        return {} if c is None else {r["fetch_id"]: r["dataset_id"] for r in c.execute("SELECT fetch_id, dataset_id FROM mi_fetches")}


def export_raw(candidates: List[dict], archive, destination, *, now: datetime) -> dict:
    """COPY eligible bodies (byte-identical .json.gz) to `destination`, re-verified after the copy, with destination/export_manifest.jsonl.
    Never deletes or edits a source. Refuses a destination inside the hot archive or the project's data/. A body already exported
    identically is skipped; a different file already at its place is refused (never overwritten)."""
    import config
    dest = Path(destination).resolve()
    hot, data = archive.root.resolve(), (Path(config.PROJECT_ROOT) / "data").resolve()
    for forbidden in (hot, data):
        if dest == forbidden or forbidden in dest.parents:
            raise PermissionError(f"an export destination is outside {forbidden}")
    copied, skipped = [], []
    for e in candidates:
        if e.get("reason"):
            raise ValueError("only eligible candidates are exported")
        src = archive.root / e["path"]
        target = dest / e["path"]
        raw = src.read_bytes()
        if hashlib.sha256(gzip.decompress(raw)).hexdigest() != e["sha256"]:
            raise ValueError(f"{e['fetch_id']}: the source body does not match its manifest")
        if target.exists():
            if target.read_bytes() == raw:
                skipped.append(e["fetch_id"])
                continue
            raise FileExistsError(f"{target} exists with other content: never overwritten")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        if target.read_bytes() != raw or hashlib.sha256(gzip.decompress(target.read_bytes())).hexdigest() != e["sha256"]:
            raise OSError(f"{target}: the copy does not verify")
        with (dest / "export_manifest.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"fetch_id": e["fetch_id"], "path": e["path"], "sha256": e["sha256"], "bytes": len(raw), "dataset_id": e.get("dataset_id"),
                                "exported_at": now.astimezone(timezone.utc).isoformat(timespec="microseconds")}, sort_keys=True) + "\n")
        copied.append(e["fetch_id"])
    return {"copied": copied, "skipped": skipped, "destination": str(dest)}


def disk_status(path, *, min_free_bytes: int) -> dict:
    if isinstance(min_free_bytes, bool) or not isinstance(min_free_bytes, int) or min_free_bytes < 0:
        raise ValueError("min_free_bytes is an explicit threshold (an operator decision: no default)")
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    u = shutil.disk_usage(p)
    return {"path": str(p), "free_bytes": u.free, "total_bytes": u.total, "min_free_bytes": min_free_bytes, "state": "OK" if u.free >= min_free_bytes else "LOW"}
