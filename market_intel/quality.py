"""
market_intel/quality.py - Stage 7.4: the market-intelligence DATA QUALITY AND LATENCY REPORT (DESIGN_74_MARKET_INTEL_QUALITY.md).

Offline, read-only and display-only. It reads a store (v1 or v2) and, when given, the Stage 7.3 collector state and the raw archive, and
reports as of an explicit evaluation time T:

  freshness        per stream, every timestamp of its latest known record kept apart (event, event end, published, retrieved, available,
                   ingested, last delivered) with its age at T. The Stage 7.0 policy grades it: bars by the 7.1 latest_status, quotes by
                   QUOTE_DAY and QUOTE_SWING. Poll-based kinds are graded by their feed (the 7.3 health rule); SEC kinds are not graded
  coverage         expected versus observed only where an expectation is explicit: a snapshot dataset's requested symbols, the calendar's
                   sessions for daily bars (the 7.1 quality_report), and a collection plan's 7.0 policy over its windows
  latency          fetch latency (reported, and started -> retrieved), delivery latency (retrieved - provider time, live rows only), and
                   publication, availability and ingestion delays: n, p50, p95, min, max (nearest rank). Measured, never judged without a target
  timestamps       malformed values, and rows contradicting the store's own time invariants (negative, missing where required, inconsistent)
  integrity        the 7.1 / 7.3.1 verify_integrity, SQLite's integrity and foreign-key checks, and the raw archive's manifest
  provider health  fetch outcomes and errors, plan pressure (the 7.0 rule), budget use, collector run outcomes, breakers and gaps
  feed divergence  IEX vs SIP bars at the same time: the 7.0 FEED_DIVERGENCE rule
  evidence         every figure is labelled by where its data came from: FIXTURE, OFFLINE_VERIFICATION (COLLECT) or OPERATOR (BACKFILL / IMPORT)

Read-only by construction. SQLite never opens an input in place: a read-only open of a WAL file creates -shm and -wal beside it. Each
database is copied byte for byte into a private temporary directory, and the copy is refused when a writer may be active (a non-empty
-wal or -journal, or a file that changes during the copy). Every read - this module's and the 7.1 / 7.3.1 interfaces it reuses - runs on
that copy, which is deleted afterwards. Nothing is written beside the inputs, no row changes, and no network is used.

Point in time. A row counts at T only when the store's own rule says it was known at T (observed, complete, available and ingested by T).
Datasets, fetches, budget events and collector runs count when they were recorded by T. Two sections describe the FILE, not the moment:
integrity, and malformed timestamps (a value that cannot be parsed cannot be placed in time).

    python -m market_intel.quality --store <db> [--state <collector.db>] [--archive-root <raw dir>] [--as-of <ISO time>] [--since <ISO time>]
                                   [--min-coverage 1.0] [--max-fetch-p95-ms N] [--max-feed-p95-s N] [--format text|json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from market_intel import freshness as FR
from market_intel import schema_v2 as SV
from market_intel import store as ST
from market_intel import validate as VA
from market_intel.archive import RawArchive
from market_intel.budget import PRESSURE_429S, PRESSURE_WINDOW_S
from market_intel.collector import health as HE
from market_intel.collector import plans as PL
from market_intel.collector.state import COMPLETE, CollectorState, StateError
from market_intel.migrations import SchemaMismatch, SchemaTooNew
from market_intel.settings import BUDGETS
from market_intel.store_v2 import MarketIntelStoreV2

REPORT_VERSION = "mi_quality_report_v1"
PASS, WARN, BLOCK, INSUFFICIENT = "PASS", "WARN", "BLOCK", "INSUFFICIENT_DATA"
RESULTS = (PASS, WARN, BLOCK, INSUFFICIENT)
EXIT_CODES = {PASS: 0, WARN: 1, BLOCK: 2, INSUFFICIENT: 3}
USAGE_ERROR = 4

FIXTURE, OFFLINE, OPERATOR, UNATTRIBUTED = "FIXTURE", "OFFLINE_VERIFICATION", "OPERATOR", "UNATTRIBUTED"
EVIDENCE = {"FIXTURE": FIXTURE, "COLLECT": OFFLINE, "BACKFILL": OPERATOR, "IMPORT": OPERATOR}
SEC_KINDS = ("FILING", "FACT")
SEC_BLOCKED = PL.SEC_TZ_BLOCK.split(":")[0]                 # SEC_ACCEPTANCE_TZ_UNVERIFIED: no SEC time is graded or measured
BARS = tuple(ST.BAR_KINDS)
INTRADAY = ("BAR_1MIN", "BAR_1HOUR")
EVENT_KINDS = ("NEWS", "HALT", "FILING", "EARNINGS")         # no event is not missing data: no expected count exists
POLL_POLICY = {"NEWS": "NEWS_POLL", "HALT": "HALT_FEED", "EARNINGS": "EARNINGS_CALENDAR", "MACRO": PL.DAILY_REFRESH}
SAMPLE_S = 60                                                # schedule coverage resolution (a presentation step, not a threshold)
MAX_LISTED = 20
NY = ZoneInfo("America/New_York")

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_TS_COL = re.compile(r"(\w+) GLOB '\[0-9\]\[0-9\]\[0-9\]\[0-9\]-")      # the schema's own timestamp CHECK names every time column
# the store's KNOWN rule (store.MarketIntelStore._KNOWN): observed, complete, available and ingested by T
_KNOWN = "{a}.observed_at <= :T AND {a}.event_end <= :T AND {a}.available_at <= :T AND {a}.ingested_at <= :T"
_LATEST = ("{a}.revision = (SELECT MAX(r.revision) FROM mi_observations r WHERE r.natural_key = {a}.natural_key "
           "AND r.available_at <= :T AND r.ingested_at <= :T)")


@dataclass(frozen=True)
class Thresholds:
    """The research thresholds the report applies. The pinned ones (pinned()) come from Stage 7.0 / 7.1, so they are reported, not
    configurable here. Each configurable one has a documented default or none: a check that needs a missing one is not judged."""
    min_coverage: float = 1.0                 # the expectation itself: every expected item present. A lower bar is the operator's call
    max_fetch_p95_ms: Optional[float] = None  # no latency target or SLA exists, so none is assumed
    max_feed_p95_s: Optional[float] = None

    def __post_init__(self):
        if isinstance(self.min_coverage, bool) or not isinstance(self.min_coverage, (int, float)) or not 0 < self.min_coverage <= 1:
            raise ValueError("min_coverage must be a number in (0, 1]")
        for name in ("max_fetch_p95_ms", "max_feed_p95_s"):
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
                raise ValueError(f"{name} must be a positive finite number, or None")

    def as_dict(self) -> dict:
        return {"configurable": {"min_coverage": self.min_coverage, "max_fetch_p95_ms": self.max_fetch_p95_ms, "max_feed_p95_s": self.max_feed_p95_s},
                "pinned": pinned()}


def pinned() -> dict:
    """Every pre-registered number the report uses, with where it is defined."""
    return {"clock_skew_s": VA.CLOCK_SKEW_S, "news_skew_s": VA.NEWS_SKEW_S, "freshness_policy_version": FR.POLICY_VERSION,
            "freshness_policies": {k: {"reference": p.reference, "fresh_s": p.fresh_s, "aging_s": p.aging_s, "window": p.window}
                                   for k, p in sorted(FR.POLICIES.items())},
            "daily_refresh": "FRESH on the same New York day, AGING within 36 h, STALE after (freshness.assess_earnings_calendar)",
            "plan_pressure": {"window_s": PRESSURE_WINDOW_S, "responses_429": PRESSURE_429S},
            "feed_divergence_bps": VA.FEED_DIVERGENCE_BPS, "session_hole_days": VA.SESSION_HOLE_DAYS,
            "default_budgets": {k: v[0] for k, v in sorted(BUDGETS.items())}, "schedule_sample_s": SAMPLE_S}


# ------------------------------------------------------------------------------------------------ times and numbers
def _aware(t: datetime, name: str) -> datetime:
    if not isinstance(t, datetime) or t.tzinfo is None or t.tzinfo.utcoffset(t) is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return t.astimezone(timezone.utc)


def _t(s) -> Optional[datetime]:
    """A stored time, or None when it is absent or malformed (the timestamp scan counts malformed values; they are never used)."""
    if not isinstance(s, str) or not _TS_RE.match(s):
        return None
    try:
        return ST.parse_ts(s)
    except ValueError:
        return None


def _malformed(s) -> bool:
    return s is not None and _t(s) is None


def _sec(a: datetime, b: datetime) -> float:
    return round((a - b).total_seconds(), 6)


def _dist(xs: List[float]) -> dict:
    """n, p50, p95, min and max by nearest rank: every figure is a sample, never an interpolation."""
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    n = len(s)
    return {"n": n, "p50": s[max(0, math.ceil(0.5 * n) - 1)], "p95": s[max(0, math.ceil(0.95 * n) - 1)], "min": s[0], "max": s[-1]}


def _key(*parts) -> Tuple[str, ...]:
    return tuple("" if p is None else str(p) for p in parts)


def _top(counter: Counter) -> List[dict]:
    return [{"value": k, "count": n} for k, n in sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:MAX_LISTED]]


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _check(name: str, result: str, codes: Iterable[str] = (), *, required: bool = False, detail: str = "") -> dict:
    assert result in RESULTS
    return {"check": name, "result": result, "codes": sorted(set(codes)), "required": required, "detail": detail}


# ------------------------------------------------------------------------------------------------ the read-only snapshot
def _side_sizes(p: Path) -> Dict[str, int]:
    return {s: Path(str(p) + s).stat().st_size for s in ("-wal", "-journal") if Path(str(p) + s).is_file()}


def _snapshot(src: Path, dst: Path) -> dict:
    """Copies `src` byte for byte to `dst`. Refused (nothing copied) when a writer may be active or the file changes meanwhile."""
    out = {"path": str(src), "present": src.exists()}
    if not out["present"]:
        return out
    if not src.is_file():
        return {**out, "refused": "NOT_A_FILE"}
    before, side = src.stat(), _side_sizes(src)
    if any(side.values()):
        return {**out, "refused": "WRITER_MAY_BE_ACTIVE",
                "detail": f"non-empty {', '.join(sorted(side))}: a writer is active or a checkpoint is pending; stop it or let it finish, then run again"}
    h = hashlib.sha256()
    with open(src, "rb") as f, open(dst, "xb") as g:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            g.write(chunk)
    after, side = src.stat(), _side_sizes(src)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or any(side.values()):
        dst.unlink()
        return {**out, "refused": "CHANGED_DURING_SNAPSHOT", "detail": "the file or its journal changed while it was copied: run again when it is quiet"}
    return {**out, "bytes": after.st_size, "sha256": h.hexdigest(), "_copy": dst}


def _open_store(copy: Path, now: datetime) -> Tuple[Optional[object], dict]:
    """The 7.1 store (v1) or the 7.3.1 store (v2) on the copy, after each one's own schema verification."""
    try:
        s = SV.status(copy)
    except sqlite3.DatabaseError as exc:
        return None, {"state": "NOT_A_DATABASE", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
    clock = lambda: now  # noqa: E731 - the store's own "now" is the evaluation time
    if s["state"] == "V2_VERIFIED":
        return MarketIntelStoreV2(copy, clock=clock), {"state": "OK", "schema_version": 2}
    if s["state"] == "V1_UPGRADE_REQUIRED":
        store = ST.MarketIntelStore(copy, clock=clock)
        try:
            store.schema_version()                                          # the 7.1 schema verification, on the copy
        except (SchemaMismatch, SchemaTooNew, sqlite3.DatabaseError) as exc:
            return None, {"state": "SCHEMA_MISMATCH", "schema_version": 1, "detail": str(exc)[:200]}
        return store, {"state": "OK", "schema_version": 1}
    if s["state"] == "EMPTY":
        return None, {"state": "EMPTY", "schema_version": 0}
    return None, {"state": "SCHEMA_TOO_NEW" if s["state"] == "TOO_NEW" else "SCHEMA_MISMATCH", "schema_version": s.get("user_version"),
                  "detail": s["state"][:200]}


def _ro(copy: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ------------------------------------------------------------------------------------------------ the context
@dataclass
class _Ctx:
    T: datetime
    S: Optional[datetime]
    th: Thresholds
    conn: sqlite3.Connection
    store: object
    ds: Dict[str, dict]

    @property
    def at(self) -> str:
        return ST.ts(self.T)

    def evidence(self, dataset_id: Optional[str]) -> str:
        d = self.ds.get(dataset_id) if dataset_id else None
        return d["evidence"] if d else UNATTRIBUTED

    def in_window(self, t: Optional[datetime]) -> bool:
        return t is not None and t <= self.T and (self.S is None or t >= self.S)


def _json_list(s) -> list:
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def _datasets(c: sqlite3.Connection, T: datetime) -> Dict[str, dict]:
    ds: Dict[str, dict] = {}
    for r in c.execute("SELECT * FROM mi_datasets ORDER BY dataset_id"):
        created = _t(r["created_at"])
        ds[r["dataset_id"]] = {"dataset_id": r["dataset_id"], "job_kind": r["job_kind"], "evidence": EVIDENCE.get(r["job_kind"], UNATTRIBUTED),
                               "kind": r["kind"], "provider": r["provider"], "feed": r["feed"], "adjustment": r["adjustment"], "timeframe": r["timeframe"],
                               "symbols": _json_list(r["symbols_json"]), "start_date": r["start_date"], "end_date": r["end_date"],
                               "created_at": r["created_at"], "known": created is not None and created <= T, "events": []}
    for r in c.execute("SELECT * FROM mi_dataset_events ORDER BY event_id"):
        at = _t(r["at"])
        if r["dataset_id"] in ds and at is not None and at <= T:
            ds[r["dataset_id"]]["events"].append({k: r[k] for k in ("status", "at", "rows_inserted", "rows_duplicate", "rows_revised", "rows_absent",
                                                                   "rows_rejected", "rows_excluded")})
    for d in ds.values():
        d["status"] = d["events"][-1]["status"] if d["events"] else None
    return ds


# ------------------------------------------------------------------------------------------------ evidence
def _evidence(ctx: _Ctx) -> dict:
    known = [d for d in ctx.ds.values() if d["known"]]
    obs = Counter()
    for r in ctx.conn.execute(f"SELECT o.dataset_id, COUNT(*) n FROM mi_observations o WHERE {_KNOWN.format(a='o')} GROUP BY o.dataset_id", {"T": ctx.at}):
        obs[ctx.evidence(r["dataset_id"])] += r["n"]
    classes = {d["evidence"] for d in known}
    if not classes:
        scope = "NONE"
    elif OPERATOR in classes:
        scope = "INCLUDES_OPERATOR_DATA"
    elif classes == {FIXTURE}:
        scope = "FIXTURE_ONLY"
    elif classes == {OFFLINE}:
        scope = "OFFLINE_VERIFICATION_ONLY"
    else:
        scope = "FIXTURE_AND_OFFLINE_VERIFICATION"
    return {"scope": scope, "datasets_by_job_kind": dict(sorted(Counter(d["job_kind"] for d in known).items())),
            "datasets_by_class": dict(sorted(Counter(d["evidence"] for d in known).items())), "observations_by_class": dict(sorted(obs.items())),
            "production_observations_possible": OPERATOR in classes,
            "note": ("FIXTURE and OFFLINE_VERIFICATION data are never production observations. OPERATOR data (an approved BACKFILL or an "
                     "IMPORT) is the only class that can hold them.")}


# ------------------------------------------------------------------------------------------------ freshness
_TIMES = ("observed_at", "event_end", "published_at", "retrieved_at", "available_at", "ingested_at", "last_delivered_at")


def _grade(kind: str, ls: dict, T: datetime) -> dict:
    if kind in SEC_KINDS:
        return {"assessed_by": "NOT_ASSESSED", "grades": {}, "reason": SEC_BLOCKED}
    if kind in BARS:
        f = ls["freshness"] or {}
        return {"assessed_by": "STORE_LATEST_STATUS", "reason": ls["reason"],
                "grades": {kind: {"status": ls["status"], "freshness": f.get("freshness"), "reason": ls["reason"], "age_s": f.get("age_s")}}}
    if kind == "QUOTE_SNAPSHOT":
        if ls["usable_is_latest"] and ls["latest_usable"]:
            seen = _t(ls["latest_usable"]["observed_at"])
            grades = {k: {x: a[x] for x in ("status", "freshness", "reason", "age_s")} for k, a in ((k, FR.assess(k, seen, T)) for k in ("QUOTE_DAY", "QUOTE_SWING"))}
        else:
            grades = {k: {"status": FR.UNAVAILABLE, "freshness": None, "reason": ls["reason"], "age_s": None} for k in ("QUOTE_DAY", "QUOTE_SWING")}
        return {"assessed_by": "POLICY_7_0", "grades": grades, "reason": ls["reason"]}
    return {"assessed_by": "FEED", "grades": {}, "reason": "POLL_BASED", "feed_policy": POLL_POLICY.get(kind)}


def _streams(ctx: _Ctx) -> List[dict]:
    groups: Dict[tuple, dict] = {}

    def g(r):
        k = (r["kind"], r["symbol"], r["series_id"], r["feed"], r["adjustment"])
        return groups.setdefault(k, {"values": 0, "facts": 0, "absences": 0, "absences_without_event_time": 0, "providers": set(), "evidence": set()})
    for r in ctx.conn.execute(f"""SELECT o.kind, o.symbol, o.series_id, o.feed, o.adjustment, COUNT(*) n, COUNT(DISTINCT o.natural_key) facts
                                  FROM mi_observations o WHERE {_KNOWN.format(a='o')} GROUP BY 1, 2, 3, 4, 5""", {"T": ctx.at}):
        x = g(r)
        x["values"], x["facts"] = r["n"], r["facts"]
    for r in ctx.conn.execute(f"""SELECT DISTINCT o.kind, o.symbol, o.series_id, o.feed, o.adjustment, o.provider, m.dataset_id FROM mi_dataset_members m
                                  JOIN mi_observations o ON o.obs_id = m.record_id WHERE m.disposition IN ('INSERTED', 'REVISED', 'DUPLICATE')
                                  AND m.at <= :T AND m.available_at <= :T AND {_KNOWN.format(a='o')}""", {"T": ctx.at}):
        x = g(r)                                                                 # every dataset that DELIVERED a fact, a re-delivery included
        x["providers"].add(r["provider"])
        x["evidence"].add(ctx.evidence(r["dataset_id"]))
    for r in ctx.conn.execute("""SELECT b.kind, b.symbol, b.series_id, b.feed, b.adjustment, b.provider, b.dataset_id, COUNT(*) n,
                                 SUM(b.observed_at IS NULL) untimed FROM mi_absences b WHERE b.available_at <= :T AND b.ingested_at <= :T
                                 GROUP BY 1, 2, 3, 4, 5, 6, 7""", {"T": ctx.at}):
        x = g(r)
        x["absences"] += r["n"]
        x["absences_without_event_time"] += r["untimed"] or 0
        x["providers"].add(r["provider"])
        x["evidence"].add(ctx.evidence(r["dataset_id"]))
    out = []
    for k in sorted(groups, key=lambda k: _key(*k)):
        kind, symbol, series, feed, adj = k
        x = groups[k]
        try:
            ls = ctx.store.latest_status(kind, at=ctx.T, symbol=symbol, series_id=series, feed=feed, adjustment=adj)
        except ST.StoreError as exc:                                             # FeedRequired: a feed kind without an explicit feed
            ls = {"latest_record": None, "latest_usable": None, "usable_is_latest": False, "freshness": None, "status": FR.UNAVAILABLE,
                  "reason": type(exc).__name__.upper()}
        rec = ls["latest_record"] or {}
        times = {n: rec.get(n) for n in _TIMES if n in rec}
        out.append({"kind": kind, "symbol": symbol, "series_id": series, "feed": feed, "adjustment": adj, "providers": sorted(x["providers"]),
                    "evidence": sorted(x["evidence"]), "mixed_evidence": len(x["evidence"]) > 1, "values": x["values"], "facts": x["facts"],
                    "absences": x["absences"], "absences_without_event_time": x["absences_without_event_time"],
                    "latest_record": {"type": rec.get("record_type"), "times": times,
                                      "ages_s": {n: _sec(ctx.T, _t(v)) for n, v in times.items() if _t(v) is not None},
                                      "missing": sorted(n for n, v in times.items() if v is None)} if rec else None,
                    "latest_usable_observed_at": (ls["latest_usable"] or {}).get("observed_at"), "usable_is_latest": ls["usable_is_latest"],
                    "store_status": ls["status"], "store_reason": ls["reason"], **_grade(kind, ls, ctx.T)})
    return out


def _runs_at(state_runs: List[dict], T: datetime) -> List[dict]:
    """Collector runs started by T; an end recorded after T is not known at T (the run was still open then)."""
    out = []
    for r in state_runs:
        started, ended = _t(r["started_at"]), _t(r.get("ended_at"))
        if started is None or started > T:
            continue
        known_end = ended is not None and ended <= T
        out.append({**r, "_started": started, "_ended": ended if known_end else None, "status": r["status"] if known_end else None})
    return out


def _successes(runs: List[dict]) -> List[Tuple[datetime, datetime]]:
    """(ended, started) of every verified COMPLETED / EMPTY run: a feed is current from a success's START, once that success has ENDED."""
    return sorted((r["_ended"], r["_started"]) for r in runs if r["_ended"] is not None and r["status"] in COMPLETE and r.get("verified"))


def _feeds(ctx: _Ctx, runs: List[dict]) -> List[dict]:
    out = []
    for (source, scope), rs in sorted(_pairs(runs).items()):
        plan = PL.PLANS.get(source)
        if plan is None:
            out.append({"source": source, "scope": scope, "status": "UNKNOWN_SOURCE", "reason": "not a registered collection plan"})
            continue
        if plan.blocked_reason:
            out.append({"source": source, "scope": scope, "kind": plan.kind, "policy": plan.health_policy, "status": HE.BLOCKED_PENDING_APPROVAL,
                        "reason": plan.blocked_reason.split(":")[0]})
            continue
        ok = _successes(rs)
        ref = max((s for _, s in ok), default=None)
        a = HE.freshness(plan, ref, ctx.T)
        out.append({"source": source, "scope": scope, "kind": plan.kind, "policy": plan.health_policy, "status": a["status"], "freshness": a["freshness"],
                    "reason": a["reason"], "age_s": a["age_s"], "last_success_started_at": ST.ts(ref) if ref else None,
                    "evidence": sorted({ctx.evidence(r["dataset_id"]) for r in rs})})
    return out


def _pairs(runs: List[dict]) -> Dict[Tuple[str, str], List[dict]]:
    out: Dict[Tuple[str, str], List[dict]] = {}
    for r in runs:
        out.setdefault((r["source"], r["scope_key"]), []).append(r)
    return out


def _freshness(ctx: _Ctx, runs: Optional[List[dict]]) -> Tuple[dict, dict]:
    streams = _streams(ctx)
    feeds = _feeds(ctx, runs) if runs is not None else None
    hist: Dict[str, dict] = {}
    ages: Dict[str, List[float]] = {}
    for s in streams:
        for key, g in s["grades"].items():
            h = hist.setdefault(key, {"status": Counter(), "freshness": Counter()})
            h["status"][g["status"]] += 1
            h["freshness"][g["freshness"] or "NONE"] += 1
        age = ((s["latest_record"] or {}).get("ages_s") or {}).get("observed_at")
        if age is not None and s["kind"] not in SEC_KINDS:
            ages.setdefault(s["kind"], []).append(age)
    for f in feeds or []:
        h = hist.setdefault(f"FEED:{f.get('policy')}", {"status": Counter(), "freshness": Counter()})
        h["status"][f["status"]] += 1
        h["freshness"][f.get("freshness") or "NONE"] += 1
    graded = [g for s in streams for g in s["grades"].values()] + [f for f in feeds or [] if f["status"] in (FR.AVAILABLE, FR.STALE, FR.UNAVAILABLE,
                                                                                                           FR.NOT_APPLICABLE)]
    codes = set()
    if any(g["status"] == FR.STALE for s in streams for g in s["grades"].values()):
        codes.add("STALE_STREAMS")
    if any(g["status"] == FR.UNAVAILABLE for s in streams for g in s["grades"].values()):
        codes.add("UNAVAILABLE_STREAMS")
    if any(s["mixed_evidence"] for s in streams):
        codes.add("MIXED_EVIDENCE")
    for f in feeds or []:
        if f["status"] == FR.STALE:
            codes.add("STALE_FEEDS")
        elif f["status"] in (FR.UNAVAILABLE, "UNKNOWN_SOURCE"):
            codes.add("UNAVAILABLE_FEEDS" if f["status"] == FR.UNAVAILABLE else "UNKNOWN_SOURCE")
    if any(s["kind"] in SEC_KINDS for s in streams):
        codes.add(SEC_BLOCKED)
    warn = codes - {SEC_BLOCKED}
    result = WARN if warn else (PASS if graded else INSUFFICIENT)
    detail = "" if graded else ("no stream or feed could be graded: poll-based kinds need the collector state" if streams else "no stream known at as_of")
    section = {"streams": streams, "feeds": feeds, "histogram": {k: {"status": dict(sorted(v["status"].items())), "freshness": dict(sorted(v["freshness"].items()))}
                                                                  for k, v in sorted(hist.items())},
               "event_age_s": {k: _dist(v) for k, v in sorted(ages.items())}}
    return section, _check("freshness", result, codes, detail=detail)


# ------------------------------------------------------------------------------------------------ coverage
def _delivered(ctx: _Ctx) -> Dict[str, Dict[str, dict]]:
    """Per dataset and symbol, what it DELIVERED by T (its members: a re-delivered, unchanged value counts for the dataset that re-delivered
    it): values (usable or not) and absences."""
    out: Dict[str, Dict[str, dict]] = {}
    for r in ctx.conn.execute(f"""SELECT m.dataset_id, o.symbol, COUNT(*) n, SUM(o.usable) u FROM mi_dataset_members m JOIN mi_observations o
                                  ON o.obs_id = m.record_id WHERE m.disposition IN ('INSERTED', 'REVISED', 'DUPLICATE') AND m.at <= :T
                                  AND m.available_at <= :T AND {_KNOWN.format(a='o')} GROUP BY 1, 2""", {"T": ctx.at}):
        x = out.setdefault(r["dataset_id"], {}).setdefault(r["symbol"] or "", {"values": 0, "usable": 0, "absences": 0})
        x["values"] += r["n"]
        x["usable"] += r["u"] or 0
    for r in ctx.conn.execute("""SELECT m.dataset_id, b.symbol, COUNT(*) n FROM mi_dataset_members m JOIN mi_absences b ON b.absence_id = m.record_id
                                 WHERE m.disposition = 'ABSENCE' AND m.at <= :T AND b.available_at <= :T AND b.ingested_at <= :T GROUP BY 1, 2""",
                              {"T": ctx.at}):
        out.setdefault(r["dataset_id"], {}).setdefault(r["symbol"] or "", {"values": 0, "usable": 0, "absences": 0})["absences"] += r["n"]
    return out


def _dataset_coverage(ctx: _Ctx) -> Tuple[List[dict], Dict[tuple, dict], set, set]:
    delivered = _delivered(ctx)
    rows, agg, codes, blocks = [], {}, set(), set()
    for d in sorted((d for d in ctx.ds.values() if d["known"]), key=lambda d: (d["created_at"], d["dataset_id"])):
        got = delivered.get(d["dataset_id"], {})
        item = {"dataset_id": d["dataset_id"], "job_kind": d["job_kind"], "evidence": d["evidence"], "kind": d["kind"], "provider": d["provider"],
                "feed": d["feed"], "window": {"start_date": d["start_date"], "end_date": d["end_date"], "created_at": d["created_at"]},
                "status_at_as_of": d["status"] or "OPEN", "symbols_requested": d["symbols"], "codes": []}
        if d["status"] in ("FAILED", "PARTIAL"):
            item["codes"].append(f"DATASET_{d['status']}")
        elif d["status"] not in ST.FINAL_STATUSES:
            item["codes"].append("DATASET_NOT_FINAL")
        try:
            span = (date.fromisoformat(d["start_date"]), date.fromisoformat(d["end_date"])) if d["start_date"] and d["end_date"] else None
        except (TypeError, ValueError):
            span = None
            item["codes"].append("DATASET_DATES_UNREADABLE")
        if d["kind"] == "BAR_1DAY" and d["symbols"] and span and d["feed"] in ("iex", "sip"):
            per = {}
            for sym in d["symbols"]:
                q = ctx.store.quality_report("BAR_1DAY", symbol=sym, feed=d["feed"], start=span[0], end=span[1], adjustment=d["adjustment"], as_of=ctx.T)
                per[sym] = {k: q[k] for k in ("result", "codes", "expected_sessions", "present_sessions", "missing_sessions", "invalid_sessions", "holes")}
                per[sym]["missing_sessions"] = per[sym]["missing_sessions"][:MAX_LISTED]
            exp = sum(p["expected_sessions"] for p in per.values())
            have = sum(p["present_sessions"] for p in per.values())
            item.update({"expectation": "CALENDAR_SESSIONS (store.quality_report)", "expected": exp, "observed": have,
                         "ratio": round(have / exp, 6) if exp else None, "per_symbol": per})
            for p in per.values():
                if p["result"] == "BLOCKED":
                    item["codes"].append("DAILY_BAR_BLOCKED")
                    blocks.add("DAILY_BAR_BLOCKED")
                elif p["result"] == "WARN":
                    item["codes"].append("DAILY_BAR_WARN")
        elif d["kind"] == "QUOTE_SNAPSHOT" and d["symbols"]:
            per = {}
            for sym in d["symbols"]:
                x = got.get(sym, {"values": 0, "usable": 0, "absences": 0})
                per[sym] = "VALUE" if x["usable"] else ("INVALID" if x["values"] else ("ABSENCE" if x["absences"] else "MISSING"))
            exp, have = len(per), sum(1 for v in per.values() if v == "VALUE")
            item.update({"expectation": "REQUESTED_SYMBOLS", "expected": exp, "observed": have, "ratio": round(have / exp, 6), "per_symbol": per})
        else:
            reason = ("NO_PRINTS_MAKE_INTRADAY_COUNTS_UNKNOWN" if d["kind"] in INTRADAY else
                      "NO_EVENT_IS_NOT_MISSING_DATA" if d["kind"] in EVENT_KINDS else "NO_EXPECTED_UNIVERSE")
            item.update({"expectation": INSUFFICIENT, "reason": reason, "expected": None,
                         "observed": sum(x["values"] for x in got.values()), "absences": sum(x["absences"] for x in got.values())})
        if item.get("ratio") is not None and item["ratio"] < ctx.th.min_coverage:
            item["codes"].append("BELOW_MIN_COVERAGE")
        item["codes"] = sorted(set(item["codes"]))
        codes.update(item["codes"])
        rows.append(item)
        if item["expected"] is not None:
            a = agg.setdefault((d["provider"], d["kind"], d["evidence"]), {"provider": d["provider"], "kind": d["kind"], "evidence": d["evidence"],
                                                                           "datasets": 0, "expected": 0, "observed": 0})
            a["datasets"] += 1
            a["expected"] += item["expected"]
            a["observed"] += item["observed"]
    return rows, agg, codes, blocks


def _schedule(ctx: _Ctx, runs: List[dict]) -> Tuple[List[dict], set, bool]:
    """Per collection pair: the share of its plan's open-window minutes in which the plan's 7.0 policy said the feed was current."""
    out, codes, any_expected = [], set(), False
    for (source, scope), rs in sorted(_pairs(runs).items()):
        plan = PL.PLANS.get(source)
        item = {"source": source, "scope": scope, "gaps_recorded": sorted({(r["gap_start"], r["gap_end"]) for r in rs if r.get("gap_start")})}
        item["gaps_recorded"] = [list(g) for g in item["gaps_recorded"]][:MAX_LISTED]
        if item["gaps_recorded"]:
            codes.add("COLLECTOR_GAPS")
        if plan is None or plan.blocked_reason:
            out.append({**item, "status": "UNKNOWN_SOURCE" if plan is None else HE.BLOCKED_PENDING_APPROVAL})
            continue
        first = min(r["_started"] for r in rs)
        start = max(first, ctx.S) if ctx.S else first
        t = start.replace(second=0, microsecond=0) + (timedelta(minutes=1) if (start.second or start.microsecond) else timedelta(0))
        ok, i, ref = _successes(rs), 0, None
        counts = Counter()
        stale: List[List[datetime]] = []
        while t <= ctx.T:
            while i < len(ok) and ok[i][0] <= t:
                ref = ok[i][1] if ref is None else max(ref, ok[i][1])
                i += 1
            a = HE.freshness(plan, ref, t)
            if a["status"] == FR.NOT_APPLICABLE:
                counts["outside_window"] += 1
            elif a["reason"] == "CALENDAR_NOT_COVERED":
                counts["calendar_not_covered"] += 1
            elif a["status"] == FR.AVAILABLE:
                counts["current"] += 1
            else:
                counts["not_current"] += 1
                if stale and stale[-1][1] == t:
                    stale[-1][1] = t + timedelta(seconds=SAMPLE_S)
                else:
                    stale.append([t, t + timedelta(seconds=SAMPLE_S)])
            t += timedelta(seconds=SAMPLE_S)
        expected = counts["current"] + counts["not_current"]
        any_expected = any_expected or expected > 0
        ratio = round(counts["current"] / expected, 6) if expected else None
        item.update({"status": "EVALUATED", "policy": plan.health_policy, "from": ST.ts(start), "minutes": dict(sorted(counts.items())),
                     "expected_minutes": expected, "current_minutes": counts["current"], "ratio": ratio,
                     "not_current_intervals": [[ST.ts(a), ST.ts(b)] for a, b in stale[:MAX_LISTED]], "not_current_interval_count": len(stale),
                     "evidence": sorted({ctx.evidence(r["dataset_id"]) for r in rs})})
        if ratio is not None and ratio < ctx.th.min_coverage:
            codes.add("SCHEDULE_BELOW_MIN_COVERAGE")
        if counts["calendar_not_covered"]:
            codes.add("CALENDAR_NOT_COVERED")
        out.append(item)
    return out, codes, any_expected


def _coverage(ctx: _Ctx, runs: Optional[List[dict]]) -> Tuple[dict, dict]:
    rows, agg, codes, blocks = _dataset_coverage(ctx)
    sched, scodes, sched_expected = _schedule(ctx, runs) if runs is not None else (None, set(), False)
    codes |= scodes
    judged = any(r["expected"] is not None for r in rows) or sched_expected
    warn = codes & {"DATASET_FAILED", "DATASET_PARTIAL", "DATASET_NOT_FINAL", "DAILY_BAR_WARN", "BELOW_MIN_COVERAGE", "SCHEDULE_BELOW_MIN_COVERAGE",
                    "COLLECTOR_GAPS"}
    result = BLOCK if blocks else (WARN if warn else (PASS if judged else INSUFFICIENT))
    by_provider = sorted(agg.values(), key=lambda a: (a["provider"], a["kind"], a["evidence"]))
    for a in by_provider:
        a["ratio"] = round(a["observed"] / a["expected"], 6) if a["expected"] else None
    section = {"datasets": rows, "by_provider_kind_evidence": by_provider, "schedules": sched, "min_coverage": ctx.th.min_coverage}
    return section, _check("coverage", result, codes, detail="" if judged else "no explicit expectation (requested symbols, calendar or schedule) exists")


# ------------------------------------------------------------------------------------------------ latency
def _latency(ctx: _Ctx) -> Tuple[dict, dict]:
    skew = VA.CLOCK_SKEW_S
    groups: Dict[tuple, dict] = {}
    excluded = Counter()
    excluded["backfilled_rows"] = ctx.conn.execute(f"SELECT COUNT(*) FROM mi_observations o WHERE o.backfilled = 1 AND {_KNOWN.format(a='o')}",
                                                   {"T": ctx.at}).fetchone()[0]
    for r in ctx.conn.execute(f"""SELECT o.kind, o.provider, o.feed, o.dataset_id, o.observed_at, o.event_end, o.published_at, o.retrieved_at,
                                  o.available_at, o.ingested_at FROM mi_observations o WHERE o.backfilled = 0 AND {_KNOWN.format(a='o')}""", {"T": ctx.at}):
        ret = _t(r["retrieved_at"])
        if ret is None or (ctx.S is not None and ret < ctx.S):
            excluded["outside_window_or_unparsed"] += 1
            continue
        if r["kind"] in SEC_KINDS:
            excluded[SEC_BLOCKED] += 1
            continue
        k = (r["provider"], r["kind"], r["feed"], ctx.evidence(r["dataset_id"]))
        g = groups.setdefault(k, {"delivery": [], "published_to_retrieved": [], "availability": [], "ingestion": [], "negative_within_skew": 0,
                                  "missing_reference": 0})
        ref = _t(r["event_end"]) if r["kind"] in BARS else _t(r["observed_at"])
        if ref is None:
            g["missing_reference"] += 1
        else:
            d = _sec(ret, ref)
            if d >= -skew:                                   # beyond the skew it contradicts the store's own rule: a timestamp violation
                g["delivery"].append(d)
                g["negative_within_skew"] += d < 0
        pub, av, ing = _t(r["published_at"]), _t(r["available_at"]), _t(r["ingested_at"])
        if pub is not None and r["kind"] == "NEWS":
            g["published_to_retrieved"].append(_sec(ret, pub))
        if av is not None:
            g["availability"].append(_sec(av, ret))
        if ing is not None:
            g["ingestion"].append(_sec(ing, ret))
    feed = [{"provider": k[0], "kind": k[1], "feed": k[2], "evidence": k[3], "reference": "event_end" if k[1] in BARS else "observed_at",
             "delivery_s": _dist(g["delivery"]), "published_to_retrieved_s": _dist(g["published_to_retrieved"]), "availability_s": _dist(g["availability"]),
             "ingestion_s": _dist(g["ingestion"]), "negative_within_skew": g["negative_within_skew"], "missing_reference": g["missing_reference"]}
            for k, g in sorted(groups.items())]
    fetch_groups: Dict[tuple, dict] = {}
    for r in ctx.conn.execute("SELECT * FROM mi_fetches ORDER BY seq"):
        ing, start, ret = _t(r["ingested_at"]), _t(r["started_at"]), _t(r["retrieved_at"])
        if ing is None or ing > ctx.T or not ctx.in_window(start):
            continue
        g = fetch_groups.setdefault((r["provider"], r["endpoint"], ctx.evidence(r["dataset_id"])), {"latency_ms": [], "duration_ms": [], "missing_latency": 0,
                                                                                                   "n": 0})
        if r["status"] not in ("OK", "NOT_MODIFIED"):
            continue
        g["n"] += 1
        if r["latency_ms"] is None:
            g["missing_latency"] += 1
        elif r["latency_ms"] >= 0:
            g["latency_ms"].append(float(r["latency_ms"]))
        if ret is not None and ret >= start:
            g["duration_ms"].append(round((ret - start).total_seconds() * 1000, 3))
    fetch = [{"provider": k[0], "endpoint": k[1], "evidence": k[2], "successful_fetches": g["n"], "latency_ms": _dist(g["latency_ms"]),
              "duration_ms": _dist(g["duration_ms"]), "missing_latency": g["missing_latency"]} for k, g in sorted(fetch_groups.items())]
    codes, judged = set(), []
    if ctx.th.max_fetch_p95_ms is not None:
        judged.append("fetch")
        if any(f["latency_ms"].get("p95", 0) > ctx.th.max_fetch_p95_ms for f in fetch):
            codes.add("FETCH_P95_ABOVE_TARGET")
    if ctx.th.max_feed_p95_s is not None:
        judged.append("delivery")
        if any(f["delivery_s"].get("p95", -math.inf) > ctx.th.max_feed_p95_s for f in feed):
            codes.add("DELIVERY_P95_ABOVE_TARGET")
    samples = any(f["latency_ms"]["n"] or f["duration_ms"]["n"] for f in fetch) or any(f["delivery_s"]["n"] for f in feed)
    if not samples:
        result, codes, detail = INSUFFICIENT, codes | {"NO_LATENCY_SAMPLES"}, "no live (non-backfilled) row or successful fetch in the window"
    elif not judged:
        result, codes, detail = INSUFFICIENT, codes | {"NO_LATENCY_TARGET"}, "measured, not judged: no latency target or SLA is set"
    else:
        result, detail = (WARN if codes else PASS), f"judged against: {', '.join(judged)}"
    section = {"fetch": fetch, "feed": feed, "excluded": dict(sorted(excluded.items())), "percentiles": "nearest rank",
               "note": "backfilled rows are history, not latency; SEC times are not measured (" + SEC_BLOCKED + ")"}
    return section, _check("latency", result, codes, detail=detail)


# ------------------------------------------------------------------------------------------------ timestamps
def _ts_columns(c: sqlite3.Connection) -> Dict[str, List[str]]:
    out = {}
    for name, sql in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL ORDER BY name"):
        cols = sorted(set(_TS_COL.findall(sql)))
        if cols:
            out[name] = cols
    return out


class _Tally:
    def __init__(self):
        self.counts, self.examples = Counter(), []

    def add(self, code: str, **where) -> None:
        self.counts[code] += 1
        if len(self.examples) < MAX_LISTED:
            self.examples.append({"code": code, **{k: (str(v)[:60] if v is not None else None) for k, v in where.items()}})

    def as_dict(self) -> dict:
        return {"counts": dict(sorted(self.counts.items())), "examples": self.examples}


def _timestamps(ctx: _Ctx, state_conn: Optional[sqlite3.Connection], runs: Optional[List[dict]]) -> Tuple[dict, dict]:
    T, skew, news_skew = ctx.T, ST.SKEW, ST.NEWS_SKEW
    malformed, bad, missing = _Tally(), _Tally(), Counter()
    for conn, label in ((ctx.conn, "store"), (state_conn, "state")):
        if conn is None:
            continue
        for table, cols in _ts_columns(conn).items():
            for row in conn.execute(f'SELECT {", ".join(cols)} FROM "{table}"'):
                for c in cols:
                    if _malformed(row[c]):
                        malformed.add("MALFORMED_TIMESTAMP", column=f"{label}:{table}.{c}", value=row[c])
    for r in ctx.conn.execute("SELECT obs_id, kind, observed_at, event_end, published_at, retrieved_at, available_at, ingested_at, backfilled FROM mi_observations"):
        ing = _t(r["ingested_at"])
        if ing is None or ing > T:
            continue
        obs, end, pub, ret, av = (_t(r[k]) for k in ("observed_at", "event_end", "published_at", "retrieved_at", "available_at"))
        w = {"table": "mi_observations", "id": r["obs_id"]}
        if r["observed_at"] is None:
            if r["kind"] in BARS:
                bad.add("BAR_WITHOUT_OBSERVED_AT", **w)
            else:
                missing["observations_without_observed_at"] += 1
        if r["published_at"] is None and r["kind"] == "NEWS":
            missing["news_without_published_at"] += 1
        if ret is not None and av is not None and av < ret:
            bad.add("AVAILABLE_BEFORE_RETRIEVAL", **w)
        if r["backfilled"] and ret is not None and av is not None and av != ret:
            bad.add("BACKFILL_AVAILABLE_AFTER_RETRIEVAL", **w)
        if obs is not None and end is not None and end < obs:
            bad.add("EVENT_END_BEFORE_OBSERVED", **w)
        if obs is not None and ret is not None and obs > ret + skew:
            bad.add("OBSERVED_AFTER_RETRIEVAL", **w)                         # a negative delivery latency beyond the clock skew
        if r["kind"] in BARS and end is not None and ret is not None and end > ret + skew:
            bad.add("BAR_NOT_COMPLETE_AT_RETRIEVAL", **w)
        if pub is not None and ret is not None and pub > ret + news_skew:
            bad.add("PUBLISHED_AFTER_RETRIEVAL", **w)
        if ret is not None and ing < ret:
            bad.add("INGESTED_BEFORE_RETRIEVAL", **w)
    for r in ctx.conn.execute("SELECT absence_id, observed_at, retrieved_at, available_at, ingested_at FROM mi_absences"):
        ing = _t(r["ingested_at"])
        if ing is None or ing > T:
            continue
        ret, av = _t(r["retrieved_at"]), _t(r["available_at"])
        w = {"table": "mi_absences", "id": r["absence_id"]}
        if r["observed_at"] is None:
            missing["absences_without_observed_at"] += 1
        if ret is not None and av is not None and av < ret:
            bad.add("AVAILABLE_BEFORE_RETRIEVAL", **w)
        if ret is not None and ing < ret:
            bad.add("INGESTED_BEFORE_RETRIEVAL", **w)
    for r in ctx.conn.execute("SELECT fetch_id, status, started_at, retrieved_at, latency_ms, ingested_at FROM mi_fetches"):
        ing = _t(r["ingested_at"])
        if ing is None or ing > T:
            continue
        start, ret = _t(r["started_at"]), _t(r["retrieved_at"])
        w = {"table": "mi_fetches", "id": r["fetch_id"]}
        if start is not None and ret is not None and ret < start:
            bad.add("NEGATIVE_FETCH_DURATION", **w)
        if r["latency_ms"] is not None and r["latency_ms"] < 0:
            bad.add("NEGATIVE_LATENCY_MS", **w)
        if r["status"] in ("OK", "NOT_MODIFIED") and r["retrieved_at"] is None:
            bad.add("SUCCESS_WITHOUT_RETRIEVED_AT", **w)
        if r["status"] in ("OK", "NOT_MODIFIED") and r["latency_ms"] is None:
            missing["successful_fetches_without_latency_ms"] += 1
        if start is not None and ing < start:
            bad.add("INGESTED_BEFORE_START", **w)
    for r in ctx.conn.execute("SELECT member_id, retrieved_at, available_at, at FROM mi_dataset_members"):
        at = _t(r["at"])
        if at is None or at > T:
            continue
        ret, av = _t(r["retrieved_at"]), _t(r["available_at"])
        if ret is not None and av is not None and av < ret:
            bad.add("AVAILABLE_BEFORE_RETRIEVAL", table="mi_dataset_members", id=r["member_id"])
    for d in ctx.ds.values():
        created = _t(d["created_at"])
        for e in d["events"]:
            at = _t(e["at"])
            if created is not None and at is not None and at < created:
                bad.add("EVENT_BEFORE_DATASET", table="mi_dataset_events", id=d["dataset_id"])
    for r in runs or []:
        if r["_ended"] is not None and r["_ended"] < r["_started"]:
            bad.add("RUN_ENDED_BEFORE_START", table="cs_runs", id=r["run_id"])
        ws, we = _t(r.get("window_start")), _t(r.get("window_end"))
        if ws is not None and we is not None and we < ws:
            bad.add("WINDOW_END_BEFORE_START", table="cs_runs", id=r["run_id"])
    codes = set(malformed.counts) | set(bad.counts)
    section = {"malformed": malformed.as_dict(), "violations": bad.as_dict(), "missing": dict(sorted(missing.items())),
               "scope": "malformed values: the whole file (a value that cannot be parsed cannot be placed in time); violations: rows recorded by as_of"}
    return section, _check("timestamps", BLOCK if codes else PASS, codes, required=True,
                           detail="a stored time contradicts the store's own invariants" if codes else "")


# ------------------------------------------------------------------------------------------------ integrity
def _integrity(ctx: _Ctx, archive_root: Optional[Path]) -> Tuple[dict, dict]:
    codes, block, anchors, archive = set(), False, None, None
    if archive_root is not None:
        if not archive_root.is_dir():
            archive = {"path": str(archive_root), "present": False}
            codes.add("ARCHIVE_NOT_FOUND")
        else:
            ra = RawArchive(archive_root)
            try:
                anchors = ra.anchors()
            except (ValueError, KeyError, TypeError) as exc:
                codes.add("ANCHORS_UNREADABLE")
                block = True
                archive = {"path": str(archive_root), "present": True, "anchors_error": str(exc)[:120]}
            if anchors is not None:
                v = ra.verify()
                archive = {"path": str(archive_root), "present": True, "anchors": len(anchors), "manifest_entries": v["entries"],
                           "manifest_problems": v["problems"][:MAX_LISTED], "manifest_problem_count": len(v["problems"])}
                if v["problems"]:
                    codes.add("RAW_ARCHIVE_MISMATCH")
                    block = True
    v = ctx.store.verify_integrity(anchors=anchors)
    findings = {k: {"count": len(x), "first": sorted(map(str, x))[:MAX_LISTED]} for k, x in sorted(v.items()) if isinstance(x, list) and x}
    sqlite_check = [r[0] for r in ctx.conn.execute("PRAGMA integrity_check")]
    fk = [list(r) for r in ctx.conn.execute("PRAGMA foreign_key_check")]
    if v["state"] == ST.CORRUPT:
        codes.add("INTEGRITY_CORRUPT" if v.get("store") != "SCHEMA_ERROR" else "INTEGRITY_SCHEMA_ERROR")
        block = True
    if sqlite_check != ["ok"]:
        codes.add("SQLITE_INTEGRITY_CHECK")
        block = True
    if fk:
        codes.add("FOREIGN_KEY_VIOLATIONS")
        block = True
    if block:
        result = BLOCK
    elif "ARCHIVE_NOT_FOUND" in codes or not v["anchors_checked"]:
        codes.add("NO_ANCHORS")
        result = INSUFFICIENT
    elif v["state"] == ST.UNVERIFIED:
        codes |= {"PENDING_ANCHOR"} if v["unanchored_datasets"] else set()
        codes |= {"PENDING_FINALIZATION"} if v["unsealed_datasets"] else set()
        result = WARN
    else:
        result = PASS
    section = {"state": v["state"], "store": v.get("store"), "rows_checked": v.get("rows_checked"), "anchors_checked": v["anchors_checked"],
               "findings": findings, "sqlite_integrity_check": sqlite_check[:MAX_LISTED], "foreign_key_violations": len(fk), "archive": archive,
               "scope": "the file as read (not point in time): integrity is a property of the stored bytes"}
    return section, _check("integrity", result, codes, required=True,
                           detail={BLOCK: "stored data cannot be trusted", INSUFFICIENT: "seals cannot be confirmed without the archive's anchors",
                                   WARN: "nothing contradicts, but something is pending", PASS: ""}[result])


# ------------------------------------------------------------------------------------------------ provider health
def _pressure(times: List[datetime]) -> List[List[str]]:
    """Episodes of the 7.0 plan-pressure rule (budget.ProviderBudget.record): at least PRESSURE_429S 429 responses within PRESSURE_WINDOW_S.
    Overlapping episodes are merged; each is [first 429, last 429]."""
    xs, out, j = sorted(times), [], 0
    for i, t in enumerate(xs):
        while (t - xs[j]).total_seconds() > PRESSURE_WINDOW_S:
            j += 1
        if i - j + 1 >= PRESSURE_429S:
            if out and xs[j] <= out[-1][1]:
                out[-1][1] = t
            else:
                out.append([xs[j], t])
    return [[ST.ts(a), ST.ts(b)] for a, b in out]


def _health(ctx: _Ctx, state_info: Optional[dict], runs: Optional[List[dict]], state: Optional[CollectorState]) -> Tuple[dict, dict]:
    codes, fetch_groups, r429 = set(), {}, {}
    for r in ctx.conn.execute("SELECT * FROM mi_fetches ORDER BY seq"):
        ing, start = _t(r["ingested_at"]), _t(r["started_at"])
        if ing is None or ing > ctx.T or not ctx.in_window(start):
            continue
        g = fetch_groups.setdefault((r["provider"], r["endpoint"], ctx.evidence(r["dataset_id"])),
                                    {"status": Counter(), "http": Counter(), "errors": Counter(), "last_ok": None, "last_error": None})
        g["status"][r["status"]] += 1
        if r["http_status"] is not None:
            g["http"][str(r["http_status"])] += 1
        if r["status"] in ("OK", "NOT_MODIFIED"):
            g["last_ok"] = max(g["last_ok"] or r["started_at"], r["started_at"])
        else:
            g["errors"][(r["error"] or r["status"])[:120]] += 1
            g["last_error"] = max(g["last_error"] or r["started_at"], r["started_at"])
        if r["http_status"] == 429:
            r429.setdefault(r["provider"], []).append(start)
    fetches = [{"provider": k[0], "endpoint": k[1], "evidence": k[2], "by_status": dict(sorted(g["status"].items())),
                "by_http_status": dict(sorted(g["http"].items())), "errors": _top(g["errors"]), "last_ok_started_at": g["last_ok"],
                "last_error_started_at": g["last_error"]} for k, g in sorted(fetch_groups.items())]
    if any(g["errors"] for g in fetch_groups.values()):
        codes.add("FETCH_ERRORS_RECORDED")
    budget: Dict[str, dict] = {}
    allows: Dict[str, List[datetime]] = {}
    for r in ctx.conn.execute("SELECT provider, event, reason, at FROM mi_budget_events ORDER BY event_id"):
        at = _t(r["at"])
        if not ctx.in_window(at):
            continue
        b = budget.setdefault(r["provider"], {"events": Counter(), "refusals": Counter(), "allows_by_new_york_day": Counter()})
        b["events"][r["event"]] += 1
        if r["event"] == "REFUSE":
            b["refusals"][r["reason"] or ""] += 1
        if r["event"] == "ALLOW":
            b["allows_by_new_york_day"][at.astimezone(NY).date().isoformat()] += 1
            allows.setdefault(r["provider"], []).append(at)
        if r["event"] == "PRESSURE":
            codes.add("PLAN_LIMIT_PRESSURE")
    caps = {"ALPHA_VANTAGE": BUDGETS["MI_ALPHA_VANTAGE_MAX_PER_DAY"][0]}
    budget_out = {}
    for p, b in sorted(budget.items()):
        xs = sorted(allows.get(p, []))
        j, peak = 0, 0
        for i in range(len(xs)):
            while (xs[i] - xs[j]).total_seconds() >= 60:
                j += 1
            peak = max(peak, i - j + 1)
        over = sorted(d for d, n in b["allows_by_new_york_day"].items() if p in caps and n > caps[p])
        if over:
            codes.add("DAILY_CAP_EXCEEDED")
        budget_out[p] = {"events": dict(sorted(b["events"].items())), "refusals": dict(sorted(b["refusals"].items())),
                         "allows_by_new_york_day": dict(sorted(b["allows_by_new_york_day"].items())), "peak_allows_in_60_s": peak,
                         "daily_cap": caps.get(p), "days_over_daily_cap": over}
    pressure = {p: _pressure(ts_) for p, ts_ in sorted(r429.items())}
    pressure = {p: e for p, e in pressure.items() if e}
    if pressure:
        codes.add("PLAN_LIMIT_PRESSURE")
    collector, block = None, False
    if state_info is not None and state_info.get("state") != "OK":
        codes.add(f"STATE_{state_info.get('state')}")
        block = True
    elif runs is not None:
        collector = []
        for (source, scope), rs in sorted(_pairs(runs).items()):
            win = [r for r in rs if ctx.S is None or r["_started"] >= ctx.S]
            ended = [r for r in win if r["_ended"] is not None]
            latest = max((r for r in rs if r["_ended"] is not None), key=lambda r: (r["_ended"], r["run_id"]), default=None)
            events = [e for e in state.breaker_events(source) if _t(e["at"]) is not None and _t(e["at"]) <= ctx.T]
            breaker = events[-1]["state"] if events else "CLOSED"
            plan = PL.PLANS.get(source)
            item = {"source": source, "scope": scope, "runs": len(win), "by_status": dict(sorted(Counter(r["status"] for r in ended).items())),
                    "open_at_as_of": sorted(r["run_id"] for r in rs if r["_ended"] is None), "clock_skew_runs": sum(1 for r in ended if r["clock_skew"]),
                    "errors": _top(Counter(r["last_error"][:120] for r in ended if r["last_error"])), "breaker": breaker,
                    "latest": {k: latest[k] for k in ("run_id", "status", "ended_at", "verified", "last_error")} if latest else None,
                    "blocked": plan.blocked_reason.split(":")[0] if plan and plan.blocked_reason else None,
                    "evidence": sorted({ctx.evidence(r["dataset_id"]) for r in rs})}
            if latest is not None and (latest["status"] not in COMPLETE or not latest["verified"]):
                codes.add("LATEST_RUN_NOT_COMPLETE")
            if breaker != "CLOSED":
                codes.add(f"CIRCUIT_{breaker}")
            if item["clock_skew_runs"]:
                codes.add("CLOCK_SKEW")
            if item["errors"]:
                codes.add("RUN_ERRORS_RECORDED")
            collector.append(item)
    seen = bool(fetches or budget_out or collector)
    result = BLOCK if block else (WARN if codes else (PASS if seen else INSUFFICIENT))
    section = {"fetches": fetches, "plan_pressure": pressure, "budget": budget_out, "collector": collector,
               "budget_note": "pre-registered default budgets; config overrides are not read. A token bucket allows a burst plus its refill, "
                              "so a 60 s peak above the per-minute budget is not a violation"}
    return section, _check("provider_health", result, codes, detail="" if seen else "no fetch, budget event or collector run in the window")


# ------------------------------------------------------------------------------------------------ feed divergence
def _divergence(ctx: _Ctx) -> Tuple[dict, dict]:
    groups: Dict[tuple, dict] = {}
    q = f"""SELECT a.kind, a.symbol, a.adjustment, a.observed_at, a.payload_json pa, b.payload_json pb, a.dataset_id da, b.dataset_id db
            FROM mi_observations a JOIN mi_observations b ON b.kind = a.kind AND b.symbol = a.symbol AND b.adjustment = a.adjustment
            AND b.observed_at = a.observed_at AND b.feed = 'sip'
            WHERE a.feed = 'iex' AND a.kind IN ('BAR_1MIN', 'BAR_1HOUR', 'BAR_1DAY') AND a.usable = 1 AND b.usable = 1
            AND {_KNOWN.format(a='a')} AND {_LATEST.format(a='a')} AND {_KNOWN.format(a='b')} AND {_LATEST.format(a='b')}
            ORDER BY a.kind, a.symbol, a.adjustment, a.observed_at"""
    for r in ctx.conn.execute(q, {"T": ctx.at}):
        g = groups.setdefault((r["kind"], r["symbol"], r["adjustment"]), {"bps": [], "divergent": 0, "not_computable": 0, "evidence": set()})
        g["evidence"] |= {ctx.evidence(r["da"]), ctx.evidence(r["db"])}
        try:
            a, b = float(json.loads(r["pa"]).get("close")), float(json.loads(r["pb"]).get("close"))
        except (TypeError, ValueError, AttributeError):
            g["not_computable"] += 1
            continue
        codes = VA.feed_divergence(a, b)
        if "NON_FINITE" in codes:
            g["not_computable"] += 1
            continue
        g["bps"].append(round(abs(a - b) / b * 10_000, 6))
        g["divergent"] += "FEED_DIVERGENCE" in codes
    pairs = [{"kind": k[0], "symbol": k[1], "adjustment": k[2], "pairs": len(g["bps"]) + g["not_computable"], "divergent": g["divergent"],
              "not_computable": g["not_computable"], "bps": _dist(g["bps"]), "evidence": sorted(g["evidence"])} for k, g in sorted(groups.items())]
    codes = ({"FEED_DIVERGENCE"} if any(p["divergent"] for p in pairs) else set()) | ({"DIVERGENCE_NOT_COMPUTABLE"} if any(p["not_computable"] for p in pairs) else set())
    result = INSUFFICIENT if not pairs else (WARN if codes else PASS)
    codes |= set() if pairs else {"NO_FEED_OVERLAP"}
    section = {"pairs": pairs, "threshold_bps": VA.FEED_DIVERGENCE_BPS, "rule": "validate.feed_divergence: |IEX close - SIP close| / SIP close; a report, never a correction"}
    return section, _check("feed_divergence", result, codes, detail="" if pairs else "no IEX and SIP bar of one symbol at one time")


# ------------------------------------------------------------------------------------------------ the report
def _verdict(checks: List[dict], evidence_scope: str) -> dict:
    blocking = [c for c in checks if c["result"] == BLOCK]
    required_missing = [c for c in checks if c["result"] == INSUFFICIENT and c["required"]]
    warnings = [c for c in checks if c["result"] == WARN]
    result = BLOCK if blocking else (INSUFFICIENT if required_missing else (WARN if warnings else PASS))
    brief = lambda cs: [{"check": c["check"], "codes": c["codes"]} for c in cs]  # noqa: E731
    return {"result": result, "blocking": brief(blocking), "insufficient": brief(required_missing), "warnings": brief(warnings),
            "not_established": brief([c for c in checks if c["result"] == INSUFFICIENT and not c["required"]]), "evidence_scope": evidence_scope,
            "rule": ("BLOCK if any check blocks; else INSUFFICIENT_DATA if a required check cannot be established; else WARN if any check warns; "
                     "else PASS. Checks are never averaged or scored, so nothing outweighs a BLOCK.")}


SECTIONS = ("evidence", "freshness", "coverage", "latency", "timestamps", "integrity", "provider_health", "feed_divergence")


def build_report(store, *, as_of: datetime, state=None, archive_root=None, since: Optional[datetime] = None,
                 thresholds: Optional[Thresholds] = None, tmp_root=None) -> dict:
    """The Stage 7.4 report as of `as_of` (see the module docstring). Reads only copies of `store` and `state`; returns a JSON-ready dict."""
    T = _aware(as_of, "as_of")
    S = _aware(since, "since") if since is not None else None
    if S is not None and S > T:
        raise ValueError("since must not be after as_of")
    th = thresholds or Thresholds()
    store_path, state_path = Path(store), (Path(state) if state is not None else None)
    archive = Path(archive_root) if archive_root is not None else None
    with tempfile.TemporaryDirectory(prefix="mi_quality_", dir=str(tmp_root) if tmp_root else None) as tmp:
        snap = _snapshot(store_path, Path(tmp) / "store.db")
        st_snap = _snapshot(state_path, Path(tmp) / "collector.db") if state_path is not None else None
        report = _build(T, S, th, snap, st_snap, archive)
    report["report_sha256"] = hashlib.sha256(_canonical(report)).hexdigest()
    return report


def _build(T: datetime, S: Optional[datetime], th: Thresholds, snap: dict, st_snap: Optional[dict], archive: Optional[Path]) -> dict:
    inputs = {"store": {k: v for k, v in snap.items() if not k.startswith("_")},
              "collector_state": {k: v for k, v in st_snap.items() if not k.startswith("_")} if st_snap is not None else None,
              "archive_root": str(archive) if archive is not None else None}
    head = {"report": REPORT_VERSION, "stage": "7.4", "as_of": ST.ts(T), "since": ST.ts(S) if S else None, "read_only": True, "inputs": inputs,
            "thresholds": th.as_dict()}
    store, info = (None, {"state": "NOT_READ"})
    if snap.get("refused"):
        info = {"state": snap["refused"]}
    elif not snap["present"]:
        info = {"state": "NOT_CREATED"}
    else:
        store, info = _open_store(snap["_copy"], T)
    inputs["store"].update({"schema": info})
    state, runs, state_conn, state_info = None, None, None, None
    if st_snap is not None:
        if st_snap.get("refused"):
            state_info = {"state": st_snap["refused"]}
        elif not st_snap["present"]:
            state_info = {"state": "NOT_CREATED"}
        else:
            state = CollectorState(st_snap["_copy"])
            try:
                runs = _runs_at(state.runs(), T)
                state_conn = _ro(st_snap["_copy"])
                state_info = {"state": "OK"}
            except (StateError, sqlite3.DatabaseError) as exc:
                state, runs, state_info = None, None, {"state": "SCHEMA_MISMATCH", "detail": str(exc)[:200]}
        inputs["collector_state"].update({"schema": state_info})
        if state_info["state"] == "NOT_CREATED":
            state_info = None                                          # nothing collected yet: health falls back to the store alone
    if store is None:
        code = {"NOT_CREATED": "STORE_NOT_CREATED", "EMPTY": "STORE_EMPTY"}.get(info["state"], f"STORE_{info['state']}")
        result = INSUFFICIENT if info["state"] in ("NOT_CREATED", "EMPTY") else BLOCK
        checks = [_check("inputs", result, [code], required=True, detail=info.get("detail", ""))]
        checks += [_check(n, INSUFFICIENT, ["INPUT_NOT_READ"], required=n in ("integrity", "timestamps")) for n in SECTIONS[1:]]
        if state_conn is not None:
            state_conn.close()
        return {**head, **{n: None for n in SECTIONS}, "checks": checks, "verdict": _verdict(checks, "NONE")}
    conn = _ro(snap["_copy"])
    try:
        ctx = _Ctx(T=T, S=S, th=th, conn=conn, store=store, ds=_datasets(conn, T))
        evidence = _evidence(ctx)
        rows = ctx.conn.execute(f"SELECT (SELECT COUNT(*) FROM mi_observations o WHERE {_KNOWN.format(a='o')}) + (SELECT COUNT(*) FROM mi_absences b "
                                "WHERE b.available_at <= :T AND b.ingested_at <= :T)", {"T": ctx.at}).fetchone()[0]
        empty = "STORE_EMPTY_AT_AS_OF" if not any(d["known"] for d in ctx.ds.values()) else ("NO_ROWS_AT_AS_OF" if not rows else None)
        checks = [_check("inputs", INSUFFICIENT if empty else PASS, [empty] if empty else [], required=True,
                         detail="nothing was delivered by as_of: data quality cannot be established from no data" if empty else "")]
        sections = {"evidence": evidence}
        for name, fn in (("integrity", lambda: _integrity(ctx, archive)), ("timestamps", lambda: _timestamps(ctx, state_conn, runs)),
                         ("freshness", lambda: _freshness(ctx, runs)), ("coverage", lambda: _coverage(ctx, runs)), ("latency", lambda: _latency(ctx)),
                         ("provider_health", lambda: _health(ctx, state_info, runs, state)), ("feed_divergence", lambda: _divergence(ctx))):
            sections[name], c = fn()
            checks.append(c)
    finally:
        conn.close()
        if state_conn is not None:
            state_conn.close()
    return {**head, **sections, "checks": checks, "verdict": _verdict(checks, evidence["scope"])}


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


SCOPE_TEXT = {"NONE": "no dataset known at as_of", "FIXTURE_ONLY": "fixture data only - NOT production observations",
              "OFFLINE_VERIFICATION_ONLY": "offline-verification (COLLECT) data only - NOT production observations",
              "FIXTURE_AND_OFFLINE_VERIFICATION": "fixture and offline-verification data only - NOT production observations",
              "INCLUDES_OPERATOR_DATA": "includes operator data (BACKFILL / IMPORT), the only class that can hold production observations"}


def render_summary(report: dict) -> str:
    v = report["verdict"]
    lines = ["Market intelligence data quality report (Stage 7.4; offline, read-only)",
             f"As of {report['as_of']}" + (f"   since {report['since']}" if report["since"] else ""), f"VERDICT: {v['result']}",
             f"EVIDENCE: {v['evidence_scope']} - {SCOPE_TEXT.get(v['evidence_scope'], '')}"]
    s = report["inputs"]["store"]
    lines.append(f"Store: {s['path']} - {s.get('schema', {}).get('state')}" + (f", schema v{s['schema'].get('schema_version')}, {s.get('bytes')} bytes" if s.get("bytes") else ""))
    lines.append("Checks:")
    for c in report["checks"]:
        lines.append(f"  {c['check']:<16} {c['result']:<18} {' '.join(c['codes'])}{'  (required)' if c['required'] else ''}")
    f = report.get("freshness")
    if f:
        lines.append(f"Freshness: {len(f['streams'])} stream(s)" + (f", {len(f['feeds'])} collector feed(s)" if f["feeds"] is not None else ""))
        for k, h in f["histogram"].items():
            lines.append(f"  {k:<22} " + "  ".join(f"{a}={n}" for a, n in h["status"].items()))
    lat = report.get("latency")
    if lat:
        for x in lat["fetch"]:
            d = x["latency_ms"]
            lines.append(f"  fetch {x['provider']} {x['endpoint']} [{x['evidence']}]: n={d['n']}" + (f" p50={d['p50']} ms p95={d['p95']} ms" if d["n"] else ""))
        for x in lat["feed"]:
            d = x["delivery_s"]
            lines.append(f"  delivery {x['provider']} {x['kind']} {x['feed']} [{x['evidence']}]: n={d['n']}" + (f" p50={d['p50']} s p95={d['p95']} s" if d["n"] else ""))
    cov = report.get("coverage")
    if cov:
        for a in cov["by_provider_kind_evidence"]:
            lines.append(f"Coverage {a['provider']} {a['kind']} [{a['evidence']}]: {a['observed']}/{a['expected']} expected")
        for x in cov["schedules"] or []:
            if x.get("status") == "EVALUATED":
                lines.append(f"Schedule {x['source']} {x['scope']}: {x['current_minutes']}/{x['expected_minutes']} expected minutes current")
    lines.append(f"report_sha256: {report['report_sha256']}")
    return "\n".join(lines) + "\n"


def _iso(s: str) -> datetime:
    d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError(f"{s!r} has no timezone: give one (e.g. Z or -05:00)")
    return d


def main(argv: Optional[List[str]] = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m market_intel.quality", description="Stage 7.4 data quality and latency report (offline, read-only)")
    ap.add_argument("--store", required=True)
    ap.add_argument("--state")
    ap.add_argument("--archive-root")
    ap.add_argument("--as-of")
    ap.add_argument("--since")
    ap.add_argument("--min-coverage", type=float, default=1.0)
    ap.add_argument("--max-fetch-p95-ms", type=float)
    ap.add_argument("--max-feed-p95-s", type=float)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    try:
        a = ap.parse_args(argv)
        as_of = _iso(a.as_of) if a.as_of else datetime.now(timezone.utc)
        rep = build_report(a.store, as_of=as_of, state=a.state, archive_root=a.archive_root, since=_iso(a.since) if a.since else None,
                           thresholds=Thresholds(a.min_coverage, a.max_fetch_p95_ms, a.max_feed_p95_s))
    except SystemExit:
        return USAGE_ERROR
    except ValueError as exc:
        out.write(f"refused: {exc}\n")
        return USAGE_ERROR
    out.write(dumps(rep) if a.format == "json" else render_summary(rep))
    return EXIT_CODES[rep["verdict"]["result"]]


if __name__ == "__main__":
    raise SystemExit(main())
