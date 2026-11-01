"""
market_intel/collect_auth.py - Stage 7.3.1: the fail-closed authorization a COLLECT dataset needs (DESIGN_731 sections 2 and 7).

A COLLECT dataset exists only with a collect authorization FILE that an operator writes into the approvals directory
(runtime/market_intel/approvals, git-ignored, the Stage 7.1 location). No code writes one.

  {"version": "mi_collect_authorization_v1", "authorization_id": "COLL-2026-11-02-01", "purpose": "OFFLINE_VERIFICATION",
   "source": "alpaca_news", "scope_key": "symbols=NVDA|feed=none|series=", "provider": "ALPACA", "kind": "NEWS", "feed": "none",
   "symbols": [], "max_requests": 20, "max_rows": 1000, "granted_by": "operator", "granted_at": "...Z", "expires_at": "...Z"}

Fail-closed rules (load, then check again when the dataset is created - by the STORE itself):
* the file lies inside the approvals directory, is a regular .json file, has exactly these fields, a 6-64 character id, positive caps, a
  grantor and a validity window of at most 24 hours that contains `now`; at creation it is READ AGAIN and must hash the same;
* the purpose is ACCEPTED: only OFFLINE_VERIFICATION (fake-transport and fixture data). LIVE is refused here
  (LIVE_COLLECT_NOT_AUTHORIZED), and the schema's claim table cannot record it either: live collection is a later, approved stage;
* the source is a registered Stage 7.3 collection plan that is not blocked (both SEC plans stay blocked: SEC_ACCEPTANCE_TZ_UNVERIFIED);
  provider and kind are that plan's; feed and symbols are the dataset's own; the dataset names this authorization (approval_ref) and
  states the same source, scope and purpose in its request;
* it is used ONCE: the store records the claim in the transaction that creates the dataset (mi_collect_authorization_uses).

What this is NOT: a security boundary against a local process that can write the approvals directory (DESIGN_71 section 8 applies).
A Stage 7.1 BackfillApproval is never a collect authorization, and a collect authorization never creates any other kind of dataset.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from market_intel import approvals as APP

AUTH_VERSION = "mi_collect_authorization_v1"
ACCEPTED_PURPOSES = ("OFFLINE_VERIFICATION",)
KNOWN_PURPOSES = ("OFFLINE_VERIFICATION", "LIVE")
MAX_VALIDITY = timedelta(hours=24)
MAX_REQUESTS = 100_000
FIELDS = ("version", "authorization_id", "purpose", "source", "scope_key", "provider", "kind", "feed", "symbols", "max_requests", "max_rows",
          "granted_by", "granted_at", "expires_at")
_ID = re.compile("^[A-Za-z0-9_-]{6,64}$")


class CollectAuthorizationError(PermissionError):
    """A collect authorization that is missing, malformed, expired, edited, reused, live, or not for this exact dataset: refused."""


@dataclass(frozen=True)
class CollectAuthorization:
    authorization_id: str
    purpose: str
    source: str
    scope_key: str
    provider: str
    kind: str
    feed: str
    symbols: Tuple[str, ...]
    max_requests: int
    max_rows: int
    granted_by: str
    granted_at: datetime
    expires_at: datetime
    path: Path
    file_sha256: str


def _time(v) -> datetime:
    try:
        return APP._time(v)
    except APP.ApprovalError as exc:
        raise CollectAuthorizationError(str(exc)) from exc


def load(path, *, now: datetime, root=None) -> CollectAuthorization:
    root = Path(root) if root is not None else APP.default_root()
    try:
        raw = APP._read(Path(path), root)
    except APP.ApprovalError as exc:
        raise CollectAuthorizationError(str(exc).replace("approval", "collect authorization")) from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectAuthorizationError("a collect authorization is UTF-8 JSON") from exc
    if not isinstance(doc, dict) or set(doc) != set(FIELDS):
        raise CollectAuthorizationError(f"a collect authorization has exactly the fields {FIELDS}")
    if doc["version"] != AUTH_VERSION:
        raise CollectAuthorizationError(f"unknown collect authorization version {doc['version']!r} (a backfill approval is not one)")
    if not isinstance(doc["authorization_id"], str) or not _ID.match(doc["authorization_id"]):
        raise CollectAuthorizationError("authorization_id: 6-64 characters of A-Z a-z 0-9 _ -")
    if doc["purpose"] not in KNOWN_PURPOSES:
        raise CollectAuthorizationError(f"unknown purpose {doc['purpose']!r}")
    if doc["purpose"] not in ACCEPTED_PURPOSES:
        raise CollectAuthorizationError("LIVE_COLLECT_NOT_AUTHORIZED: live collection is not authorized by Stage 7.3.1 (DESIGN_731 section 7)")
    for k in ("source", "scope_key", "provider", "kind", "feed", "granted_by"):
        if not isinstance(doc[k], str) or not doc[k].strip():
            raise CollectAuthorizationError(f"{k}: a non-empty string")
    if not isinstance(doc["symbols"], list) or not all(isinstance(s, str) for s in doc["symbols"]):
        raise CollectAuthorizationError("symbols: a list of tickers")
    for k in ("max_requests", "max_rows"):
        if isinstance(doc[k], bool) or not isinstance(doc[k], int) or doc[k] <= 0:
            raise CollectAuthorizationError(f"{k}: a positive integer")
    if doc["max_requests"] > MAX_REQUESTS:
        raise CollectAuthorizationError(f"max_requests: at most {MAX_REQUESTS}")
    granted, expires = _time(doc["granted_at"]), _time(doc["expires_at"])
    if not granted < expires or expires - granted > MAX_VALIDITY:
        raise CollectAuthorizationError("a collect authorization is valid for at most 24 hours")
    now = now.astimezone(timezone.utc)
    if not granted <= now < expires:
        raise CollectAuthorizationError("the collect authorization is not valid now (not yet valid, or expired)")
    return CollectAuthorization(authorization_id=doc["authorization_id"], purpose=doc["purpose"], source=doc["source"], scope_key=doc["scope_key"],
                                provider=doc["provider"], kind=doc["kind"], feed=doc["feed"], symbols=tuple(doc["symbols"]),
                                max_requests=doc["max_requests"], max_rows=doc["max_rows"], granted_by=doc["granted_by"], granted_at=granted,
                                expires_at=expires, path=Path(path).resolve(), file_sha256=hashlib.sha256(raw).hexdigest())


def check(auth, spec, *, now: datetime, root=None) -> None:
    """Creation-time check by the store: the file again (same hash, still valid), the purpose, the plan, and the dataset spec."""
    from market_intel.collector import plans as PL
    if not isinstance(auth, CollectAuthorization):
        raise CollectAuthorizationError("COLLECT_NEEDS_AUTHORIZATION: a COLLECT dataset needs a collect authorization loaded from its file")
    reloaded = load(auth.path, now=now, root=root)
    if reloaded != auth:
        raise CollectAuthorizationError("the collect authorization file changed after it was loaded")
    plan = PL.PLANS.get(auth.source)
    if plan is None:
        raise CollectAuthorizationError(f"{auth.source!r} is not a registered collection plan")
    if plan.blocked_reason:
        raise CollectAuthorizationError(plan.blocked_reason)
    req = spec.request if isinstance(spec.request, dict) else {}
    mismatches = [k for k, a, b in (("job_kind", spec.job_kind, "COLLECT"), ("kind", spec.kind, plan.kind), ("kind", auth.kind, plan.kind),
                                    ("provider", spec.provider, plan.provider), ("provider", auth.provider, plan.provider), ("feed", spec.feed, auth.feed),
                                    ("symbols", sorted(spec.symbols), sorted(auth.symbols)), ("approval_ref", spec.approval_ref, auth.authorization_id),
                                    ("request.source", req.get("source"), auth.source), ("request.scope", req.get("scope"), auth.scope_key),
                                    ("request.purpose", req.get("purpose"), auth.purpose)) if a != b]
    if mismatches:
        raise CollectAuthorizationError(f"the COLLECT dataset is not the one authorized: {sorted(set(mismatches))}")
