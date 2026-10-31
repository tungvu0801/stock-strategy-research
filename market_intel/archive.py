"""
market_intel/archive.py - Stage 7.1: the raw-response archive (the value_quality/ingest.py pattern; design C.9 "Retention").

Every provider response body is kept byte-for-byte, gzipped (mtime 0, so the archive is reproducible), under
<root>/YYYY-MM-DD/<fetch_id>.json.gz with one manifest line {fetch_id, sha256, bytes, provider, endpoint, retrieved_at}. The default root
is runtime/market_intel/raw (git-ignored). verify() re-hashes every archived body against the manifest. Nothing here touches the network.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


def default_root() -> Path:
    import config
    return Path(config.PROJECT_ROOT) / "runtime" / "market_intel" / "raw"


class RawArchive:
    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_root()

    def write(self, fetch_id: str, body: bytes, *, provider: str, endpoint: str, retrieved_at: datetime) -> dict:
        day = retrieved_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        rel = f"{day}/{fetch_id}.json.gz"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(gzip.compress(body, mtime=0))
        tmp.replace(path)                                        # the body appears whole or not at all
        entry = {"fetch_id": fetch_id, "path": rel, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "provider": provider,
                 "endpoint": endpoint, "retrieved_at": retrieved_at.astimezone(timezone.utc).isoformat(timespec="microseconds")}
        with (self.root / "manifest.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def read(self, rel: str) -> bytes:
        return gzip.decompress((self.root / rel).read_bytes())

    def manifest(self) -> List[dict]:
        p = self.root / "manifest.jsonl"
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []

    def verify(self) -> dict:
        """Every manifest entry's body exists and hashes to its recorded SHA-256."""
        bad, n = [], 0
        for e in self.manifest():
            n += 1
            try:
                if hashlib.sha256(self.read(e["path"])).hexdigest() != e["sha256"]:
                    bad.append({"fetch_id": e["fetch_id"], "problem": "SHA256_MISMATCH"})
            except FileNotFoundError:
                bad.append({"fetch_id": e["fetch_id"], "problem": "MISSING"})
            except (OSError, EOFError, gzip.BadGzipFile):              # truncated or overwritten: reported, never trusted
                bad.append({"fetch_id": e["fetch_id"], "problem": "UNREADABLE"})
        return {"entries": n, "ok": not bad, "problems": bad}
