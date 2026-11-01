"""
market_intel/approvals.py - Stage 7.1: the fail-closed, operator-controlled backfill approval (DESIGN_71 section 4).

A real backfill (a FUTURE stage: Stage 7.1 ships no transport) needs an approval FILE that an operator writes into the approvals
directory (runtime/market_intel/approvals, git-ignored). No code in market_intel writes one; the CLI never builds one.

  {"version": "mi_backfill_approval_v1", "approval_id": "OPS-2026-11-07-01", "plan_sha256": "<the plan's hash>",
   "request": {<the exact BackfillRequest>}, "sip_approved": false, "max_requests": 4, "max_rows": 1000,
   "granted_by": "operator name", "granted_at": "2026-11-07T23:00:00Z", "expires_at": "2026-11-08T05:00:00Z"}

Fail-closed rules (load, then check again at run time):
* the file lies inside the approvals directory (no path outside it, no link escaping it) and is a regular .json file;
* the document is exactly this schema: an id of 6-64 characters [A-Za-z0-9_-], a 64-hex plan hash, the full request, booleans and
  positive integer caps, a non-empty grantor, timezone-aware times, a validity window of at most 24 hours that contains `now`;
* at run time the file is READ AGAIN and must hash exactly as when it was loaded (an edited file is refused);
* the request in the file equals the request being run, the plan recomputed AT RUN TIME from that request is valid and hashes to
  plan_sha256 (a plan cannot be mutated after approval), a SIP request needs sip_approved, and the caps bound the plan's estimate;
* an approval is used ONCE: the run records it in mi_approval_uses in the transaction that creates its dataset.

What this is NOT: a security boundary against a local process that can write the approvals directory (such a process could write a
file). It is an integrity control against mistakes, scope drift, stale or reused approvals and mutated plans. A transport stage should
add an operator signature with a key kept off this machine (DESIGN_71 section 8).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

APPROVAL_VERSION = "mi_backfill_approval_v1"
MAX_VALIDITY = timedelta(hours=24)
FIELDS = ("version", "approval_id", "plan_sha256", "request", "sip_approved", "max_requests", "max_rows", "granted_by", "granted_at", "expires_at")
_ID = re.compile("^[A-Za-z0-9_-]{6,64}$")
_HEX = re.compile("^[0-9a-f]{64}$")


class ApprovalError(PermissionError):
    """An approval that is missing, malformed, expired, edited, reused or not for this exact plan: the run is refused."""


def default_root() -> Path:
    import config
    return Path(config.PROJECT_ROOT) / "runtime" / "market_intel" / "approvals"


@dataclass(frozen=True)
class BackfillApproval:
    approval_id: str
    plan_sha256: str
    request_json: str                    # canonical JSON of the approved request (compared, never parsed back into a plan)
    sip_approved: bool
    max_requests: int
    max_rows: int
    granted_by: str
    granted_at: datetime
    expires_at: datetime
    path: Path
    file_sha256: str


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _time(v) -> datetime:
    if not isinstance(v, str):
        raise ApprovalError("approval times are ISO strings")
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError(f"approval time {v!r} is not ISO-8601") from exc
    if d.tzinfo is None:
        raise ApprovalError("approval times must be timezone-aware")
    return d.astimezone(timezone.utc)


def _read(path: Path, root: Path) -> bytes:
    root_r = root.resolve()
    if path.is_symlink():
        raise ApprovalError("an approval file may not be a link")
    p = path.resolve()
    if root_r not in p.parents or p.suffix != ".json" or not p.is_file():
        raise ApprovalError("an approval must be a .json file inside the approvals directory")
    return p.read_bytes()


def load(path: Path, *, now: datetime, root: Optional[Path] = None) -> BackfillApproval:
    """Parse and validate one approval file. Raises ApprovalError on anything short of a valid, current approval."""
    root = Path(root) if root is not None else default_root()
    raw = _read(Path(path), root)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApprovalError("an approval file is UTF-8 JSON") from exc
    if not isinstance(doc, dict) or set(doc) != set(FIELDS):
        raise ApprovalError(f"an approval has exactly the fields {FIELDS}")
    if doc["version"] != APPROVAL_VERSION:
        raise ApprovalError(f"unknown approval version {doc['version']!r}")
    if not isinstance(doc["approval_id"], str) or not _ID.match(doc["approval_id"]):
        raise ApprovalError("approval_id: 6-64 characters of A-Z a-z 0-9 _ -")
    if not isinstance(doc["plan_sha256"], str) or not _HEX.match(doc["plan_sha256"]):
        raise ApprovalError("plan_sha256: a lowercase sha256 hex digest")
    if not isinstance(doc["request"], dict):
        raise ApprovalError("request: the full approved request")
    if not isinstance(doc["sip_approved"], bool):
        raise ApprovalError("sip_approved: true or false")
    for k in ("max_requests", "max_rows"):
        if isinstance(doc[k], bool) or not isinstance(doc[k], int) or doc[k] <= 0:
            raise ApprovalError(f"{k}: a positive integer")
    if not isinstance(doc["granted_by"], str) or not doc["granted_by"].strip():
        raise ApprovalError("granted_by: who approved it")
    granted, expires = _time(doc["granted_at"]), _time(doc["expires_at"])
    if not granted < expires or expires - granted > MAX_VALIDITY:
        raise ApprovalError("an approval is valid for at most 24 hours")
    now = now.astimezone(timezone.utc)
    if not granted <= now < expires:
        raise ApprovalError("the approval is not valid now (not yet valid, or expired)")
    return BackfillApproval(approval_id=doc["approval_id"], plan_sha256=doc["plan_sha256"], request_json=canonical(doc["request"]),
                            sip_approved=doc["sip_approved"], max_requests=doc["max_requests"], max_rows=doc["max_rows"], granted_by=doc["granted_by"],
                            granted_at=granted, expires_at=expires, path=Path(path).resolve(), file_sha256=hashlib.sha256(raw).hexdigest())


def check(approval, fresh_plan: dict, *, now: datetime, root: Optional[Path] = None) -> None:
    """Run-time check of an approval against the plan RECOMPUTED from the request being run. Re-reads the file."""
    if not isinstance(approval, BackfillApproval):
        raise ApprovalError("a backfill needs an approval loaded from an approval file")
    root = Path(root) if root is not None else default_root()
    reloaded = load(approval.path, now=now, root=root)                  # validity again, now
    if reloaded.file_sha256 != approval.file_sha256 or reloaded != approval:
        raise ApprovalError("the approval file changed after it was loaded")
    if not fresh_plan.get("valid"):
        raise ApprovalError("the plan recomputed now is refused: " + ", ".join(r["code"] for r in fresh_plan.get("refusals", [])))
    if canonical(fresh_plan["request"]) != approval.request_json:
        raise ApprovalError("the request being run is not the approved request")
    if fresh_plan["plan_sha256"] != approval.plan_sha256:
        raise ApprovalError("the plan recomputed now does not hash to the approved plan")
    if fresh_plan["request"]["feed"] == "sip" and not approval.sip_approved:
        raise ApprovalError("a SIP request needs its own approval (sip_approved)")
    est = fresh_plan["estimate"]
    if approval.max_requests < est["requests"] or approval.max_rows < est["rows_total"]:
        raise ApprovalError("the approval's caps are below the plan's own estimate")
