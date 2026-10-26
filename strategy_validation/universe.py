"""strategy_validation/universe.py — Stage 6.5.1 UNIVERSE INTEGRITY (DESIGN_651_UNIVERSE_INTEGRITY.md, policy universe_integrity_v1).

A cross-sectional result is selection-edge evidence only if the universe it ranked was knowable at each rebalance. A fixed list of today's
survivors projected backwards leaks the future (the strategy and its same-universe control can only hold stocks that survived); a fair control
does not remove that. Every CROSS_SECTIONAL_PORTFOLIO adapter therefore DECLARES its universe, and the selection edge is capped by its integrity.

  validate_declaration(d)        the declaration's problems ([] = valid); unknown values are None, never filled in
  PointInTimeMembership          the point-in-time contract: members_as_of(T) from dated intervals (effective_from <= T < effective_to), removal
                                 reasons, one identity across ticker changes; a future addition never appears early, a later removal never early
  SnapshotMembership             dated historical constituent snapshots: the latest on / before T and its staleness
  membership_check(...)          every eligible / selected symbol at a rebalance is a member as of it (a supplied history the ranking ignores
                                 is still survivorship leakage)
  classify(...)                  TRUSTED / LIMITED / UNTRUSTED (or NOT_APPLICABLE, or MISSING when nothing is declared) and the edge cap
  cap_edge(raw, integrity)       the FINAL selection edge: the raw v3 edge lowered to the cap (raw kept; reason UNIVERSE_INTEGRITY_CAP)
  assess_stored_run(...)         an append-only universe-integrity assessment of a stored run (the run is never rewritten)

Pure: no clock (except an assessment's timestamp), no network, no broker."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from strategy_validation import (EDGE_LABELS, EDGE_LADDER, SNAPSHOT_MAX_STALENESS_DAYS, UNIVERSE_DECLARATION_FIELDS, UNIVERSE_EDGE_CAP, UNIVERSE_METHODS,
                                 UNIVERSE_POLICY_VERSION)

REMOVAL_REASONS = ("DELISTED", "ACQUIRED", "BANKRUPT", "INDEX_REMOVAL", "OTHER")
COMPLETENESS = ("includes_delisted", "includes_acquired", "includes_bankrupt", "historical_additions_supported", "historical_removals_supported")
BIAS_VALUES = ("PRESENT", "ABSENT", "UNKNOWN")
CONSEQUENCE = {"TRUSTED": "The v3 selection-edge policy applies in full.",
               "LIMITED": "The selection edge can at most be WEAK: the historical membership has gaps.",
               "UNTRUSTED": "The selection edge cannot be established: the universe is not point in time (cannot establish alpha).",
               "NOT_APPLICABLE": "An event-driven strategy: no universe ranking.",
               "MISSING": "No universe declaration: the selection edge cannot be assessed and the run cannot be persisted."}


class UniverseError(ValueError):
    """An invalid declaration or membership history."""


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso(x) -> bool:
    """A canonical YYYY-MM-DD date only: membership compares the strings, so 20220601 or 2022-W22-3 (which fromisoformat accepts) would misorder."""
    if not isinstance(x, str) or not _ISO.match(x):
        return False
    try:
        date.fromisoformat(x)
        return True
    except ValueError:
        return False


def _date_arg(T) -> str:
    if not _iso(T):
        raise UniverseError(f"{T!r} is not a YYYY-MM-DD date")
    return T


# ---------------------------------------------------------------- the declaration ------------------------------------------------------------------
def validate_declaration(d) -> List[str]:
    if not isinstance(d, dict):
        return ["no universe declaration"]
    p = [f"missing field {f}" for f in UNIVERSE_DECLARATION_FIELDS if f not in d]
    if d.get("universe_method") not in UNIVERSE_METHODS:
        p.append(f"universe_method must be one of {', '.join(UNIVERSE_METHODS)}")
    for f in COMPLETENESS:
        if d.get(f) not in (True, False, None):
            p.append(f"{f} must be true, false or null (unknown)")
    if d.get("survivorship_bias") not in BIAS_VALUES:
        p.append(f"survivorship_bias must be one of {', '.join(BIAS_VALUES)}")
    for f in ("coverage_start", "coverage_end"):
        if d.get(f) is not None and not _iso(d[f]):
            p.append(f"{f} must be an ISO date or null")
    if not str(d.get("universe_source") or "").strip() or not str(d.get("membership_as_of_semantics") or "").strip():
        p.append("universe_source and membership_as_of_semantics must be stated")
    if d.get("universe_method") == "FIXED_CURRENT_UNIVERSE" and d.get("survivorship_bias") == "ABSENT":
        p.append("a fixed current universe cannot declare survivorship bias ABSENT")
    return p


# ---------------------------------------------------------------- the point-in-time contract ---------------------------------------------------------
class PointInTimeMembership:
    """intervals: [{security_id, effective_from, effective_to (None = still a member), removal_reason (when effective_to is set),
    symbols: [{symbol, from, to (None = current)}] (the ticker history; optional)}]."""

    def __init__(self, intervals: List[dict]):
        by: Dict[str, List[dict]] = {}
        for iv in intervals:
            sid, a, b = iv.get("security_id"), iv.get("effective_from"), iv.get("effective_to")
            if not sid or not _iso(a) or (b is not None and not _iso(b)):
                raise UniverseError(f"interval of {sid}: security_id and ISO effective_from / effective_to are required")
            if b is not None and b <= a:
                raise UniverseError(f"{sid}: effective_to {b} must be after effective_from {a}")
            if (b is None) != (iv.get("removal_reason") is None) or (b is not None and iv["removal_reason"] not in REMOVAL_REASONS):
                raise UniverseError(f"{sid}: a removal (effective_to) needs a reason among {', '.join(REMOVAL_REASONS)}, and only a removal has one")
            by.setdefault(sid, []).append(iv)
        for sid, ivs in by.items():
            ivs.sort(key=lambda x: x["effective_from"])
            for x, y in zip(ivs, ivs[1:]):
                if x["effective_to"] is None or x["effective_to"] > y["effective_from"]:
                    raise UniverseError(f"{sid}: overlapping membership intervals")
        self.intervals = [iv for ivs in by.values() for iv in ivs]
        held: Dict[str, List[tuple]] = {}
        for iv in self.intervals:
            syms = sorted(iv.get("symbols") or [], key=lambda s: str(s.get("from")))
            for s in syms:
                if not s.get("symbol") or not _iso(s.get("from")) or (s.get("to") is not None and (not _iso(s["to"]) or s["to"] <= s["from"])):
                    raise UniverseError(f"{iv['security_id']}: a symbol entry needs a symbol and YYYY-MM-DD from / to (to after from)")
            for x, y in zip(syms, syms[1:]):
                if x.get("to") is None or x["to"] > y["from"]:
                    raise UniverseError(f"{iv['security_id']}: overlapping symbol history")
            for s in syms:
                held.setdefault(s["symbol"], []).append((s["from"], s.get("to"), iv["security_id"]))
        for sym, spans in held.items():
            spans.sort(key=lambda x: x[0])
            for x, y in zip(spans, spans[1:]):
                if x[2] != y[2] and (x[1] is None or x[1] > y[0]):
                    raise UniverseError(f"symbol {sym} belongs to {x[2]} and {y[2]} at the same time")

    def members_as_of(self, T: str) -> Dict[str, str]:
        """{security_id: its symbol at T} for every security that is a member at T - known from information dated on / before T."""
        _date_arg(T)
        out = {}
        for iv in self.intervals:
            if iv["effective_from"] <= T and (iv["effective_to"] is None or T < iv["effective_to"]):
                sym = next((s["symbol"] for s in iv.get("symbols") or [] if s["from"] <= T and (s.get("to") is None or T < s["to"])), iv["security_id"])
                out[iv["security_id"]] = sym
        return out

    def summary(self) -> dict:
        blob = json.dumps(sorted(self.intervals, key=lambda x: (x["security_id"], x["effective_from"])), sort_keys=True)
        return {"kind": "POINT_IN_TIME", "securities": len({iv["security_id"] for iv in self.intervals}), "intervals": len(self.intervals),
                "removals": sum(1 for iv in self.intervals if iv["effective_to"] is not None),
                "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]}


class SnapshotMembership:
    """snapshots: {snapshot_date: [symbols]} - dated historical constituent lists."""

    def __init__(self, snapshots: Dict[str, List[str]]):
        if not snapshots or not all(_iso(d) for d in snapshots):
            raise UniverseError("snapshots need ISO dates")
        self.snapshots = {d: sorted(set(v)) for d, v in snapshots.items()}
        self.dates = sorted(self.snapshots)

    def as_of(self, T: str):
        _date_arg(T)
        prior = [d for d in self.dates if d <= T]
        return (prior[-1], self.snapshots[prior[-1]]) if prior else (None, [])

    def staleness(self, T: str) -> Optional[int]:
        d, _ = self.as_of(T)
        return None if d is None else (date.fromisoformat(T) - date.fromisoformat(d)).days

    def summary(self) -> dict:
        blob = json.dumps(self.snapshots, sort_keys=True)
        return {"kind": "HISTORICAL_SNAPSHOT", "snapshots": len(self.dates), "first": self.dates[0], "last": self.dates[-1],
                "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]}


# ---------------------------------------------------------------- classification + the cap ------------------------------------------------------------
def _block(status: Optional[str], method: Optional[str], reasons: List[str], declaration, membership_summary=None, membership_check=None) -> dict:
    return {"policy_version": UNIVERSE_POLICY_VERSION, "method": method, "status": status, "cap": UNIVERSE_EDGE_CAP.get(status) if status else None,
            "point_in_time": status == "TRUSTED", "survivorship_bias": (declaration or {}).get("survivorship_bias") if isinstance(declaration, dict) else None,
            "includes_delisted": (declaration or {}).get("includes_delisted") if isinstance(declaration, dict) else None,
            "reasons": reasons, "consequence": CONSEQUENCE.get(status or "MISSING"), "declaration": declaration if isinstance(declaration, dict) else None,
            "membership": membership_summary, "membership_check": membership_check}


def membership_check(membership, selections: Optional[List[dict]]) -> dict:
    """Did the strategy RANK the supplied membership? Both directions, at every rebalance: every eligible / selected symbol is a member as of it
    (a valid history beside a ranking of today's list is survivorship leakage), and every member as of it is eligible or excluded with a stated
    reason (`excluded`: {symbol: reason}) - a member that silently never ranks (a later failure without bars) is the same leakage. A member
    excluded as NO_DATA is counted: its outcome is missing from the replay."""
    outside, unaccounted, no_data = [], [], 0
    for s in selections or []:
        T = _date_arg(s.get("rebalance"))
        allowed = set(membership.members_as_of(T).values()) if isinstance(membership, PointInTimeMembership) else set(membership.as_of(T)[1])
        eligible, excluded = set(s.get("eligible") or []), dict(s.get("excluded") or {})
        bad = sorted((eligible | set(s.get("selected") or [])) - allowed)
        if bad:
            outside.append({"rebalance": T, "symbols": bad[:5], "count": len(bad)})
        missing = sorted(allowed - eligible - set(excluded))
        if missing:
            unaccounted.append({"rebalance": T, "symbols": missing[:5], "count": len(missing)})
        no_data += sum(1 for k, r in excluded.items() if k in allowed and r == "NO_DATA")
    return {"rebalances": sorted(s["rebalance"] for s in selections or []), "rebalances_checked": len(selections or []),
            "rebalances_outside": len(outside), "first_outside": outside[0] if outside else None,
            "rebalances_unaccounted": len(unaccounted), "first_unaccounted": unaccounted[0] if unaccounted else None, "members_without_data": no_data}


def not_assessed() -> dict:
    """A cross-sectional record no universe_integrity_v1 assessment covers: its universe is UNKNOWN, so UNTRUSTED - never read uncapped."""
    return {**_block("UNTRUSTED", "UNKNOWN", ["no universe-integrity assessment covers this record: its universe is treated as UNKNOWN"], None),
            "not_assessed": True}


def not_applicable() -> dict:
    return _block("NOT_APPLICABLE", None, ["an event-driven strategy ranks no universe"], None)


def classify(declaration, membership=None, rebalances: Optional[List[str]] = None, start: Optional[str] = None, end: Optional[str] = None,
             selections: Optional[List[dict]] = None) -> dict:
    """The universe integrity of a declaration (and, for point-in-time methods, the SUPPLIED membership and the strategy's per-rebalance
    eligible / selected symbols) - policy universe_integrity_v1."""
    if declaration is None:
        return {**_block(None, None, ["no universe declaration"], None), "missing": True}
    problems = validate_declaration(declaration)
    if problems:
        return {**_block(None, declaration.get("universe_method") if isinstance(declaration, dict) else None, problems, declaration), "invalid": True}
    m = declaration["universe_method"]
    complete = all(declaration.get(f) is True for f in COMPLETENESS)
    msum = membership.summary() if membership is not None else None
    if m == "FIXED_CURRENT_UNIVERSE":
        return _block("UNTRUSTED", m, ["today's list projected backwards: the universe knows which stocks survived"], declaration, msum)
    if m == "UNKNOWN":
        return _block("UNTRUSTED", m, ["the universe method is not known"], declaration, msum)
    if m in ("POINT_IN_TIME", "HISTORICAL_SNAPSHOT") and isinstance(membership, (PointInTimeMembership, SnapshotMembership)):
        if selections is None:
            return _block("UNTRUSTED", m, ["a membership is supplied, but the ranked symbols per rebalance are not: the membership cannot be checked against them"],
                          declaration, msum)
        chk = membership_check(membership, selections)
        if not chk["rebalances_checked"]:
            return _block("UNTRUSTED", m, ["no rebalance to check the membership against"], declaration, msum, chk)
        if chk["rebalances_outside"]:
            f = chk["first_outside"]
            return _block("UNTRUSTED", m, [f"{chk['rebalances_outside']} rebalance(s) rank securities outside the as-of membership (first {f['rebalance']}: "
                                           f"{', '.join(f['symbols'])}): survivorship leakage"], declaration, msum, chk)
        if chk["rebalances_unaccounted"]:
            f = chk["first_unaccounted"]
            return _block("UNTRUSTED", m, [f"{chk['rebalances_unaccounted']} rebalance(s) leave members neither eligible nor excluded with a reason (first "
                                           f"{f['rebalance']}: {', '.join(f['symbols'])}): survivorship leakage"], declaration, msum, chk)
    else:
        chk = None
    caveats = ([] if declaration.get("survivorship_bias") == "ABSENT" else [f"survivorship bias is declared {declaration.get('survivorship_bias')}, not ABSENT"]) + \
              ([f"{chk['members_without_data']} member observation(s) excluded for missing price data: their outcomes are not in the replay"]
               if chk and chk["members_without_data"] else [])
    if m == "POINT_IN_TIME":
        if not isinstance(membership, PointInTimeMembership):
            return _block("UNTRUSTED", m, ["point in time is claimed, but no membership history is supplied: the claim cannot be checked"], declaration)
        cov = (declaration.get("coverage_start") is not None and declaration.get("coverage_end") is not None and start is not None and end is not None
               and declaration["coverage_start"] <= start and declaration["coverage_end"] >= end)
        reasons = ([] if complete else ["the declaration does not cover delisted, acquired and bankrupt securities and additions and removals"]) + \
                  ([] if cov else ["the membership history does not cover the dataset"]) + caveats + \
                  (["the membership records no removal at all: the delisted / acquired / bankrupt claims cannot be corroborated"]
                   if msum["removals"] == 0 and any(declaration.get(f) is True for f in ("includes_delisted", "includes_acquired", "includes_bankrupt")) else [])
        return _block("TRUSTED" if not reasons else "LIMITED", m, reasons or ["membership known at every rebalance"], declaration, msum, chk)
    # HISTORICAL_SNAPSHOT
    if not isinstance(membership, SnapshotMembership):
        return _block("UNTRUSTED", m, ["historical snapshots are claimed, but none are supplied: the claim cannot be checked"], declaration, msum)
    gaps = []
    for T in sorted(set(rebalances or []) | {s["rebalance"] for s in selections or []}):
        st = membership.staleness(T)
        if st is None or st > SNAPSHOT_MAX_STALENESS_DAYS:
            gaps.append(T)
    reasons = ([f"{len(gaps)} rebalance(s) without a snapshot within {SNAPSHOT_MAX_STALENESS_DAYS} days (first {gaps[0]})"] if gaps else []) + \
              ([] if complete else ["the snapshots do not cover delisted securities, additions and removals"]) + caveats
    return _block("TRUSTED" if not reasons else "LIMITED", m, reasons or ["a snapshot within the staleness limit at every rebalance"], declaration, msum, chk)


def reclassify(recorded: dict, start: Optional[str], end: Optional[str], periods: Optional[List[str]] = None) -> Optional[tuple]:
    """The integrity statuses the policy allows for a RECORDED block (the gate never trusts the recorded status): None = no valid declaration.
    FIXED / UNKNOWN and the point-in-time coverage are re-derived exactly; the snapshot staleness needs the snapshots, so a recorded snapshot
    universe may be TRUSTED only with complete coverage flags and a recorded snapshot summary."""
    d = (recorded or {}).get("declaration")
    if validate_declaration(d):
        return None
    m, msum = d["universe_method"], (recorded or {}).get("membership") or {}
    complete = all(d.get(f) is True for f in COMPLETENESS)
    if m in ("FIXED_CURRENT_UNIVERSE", "UNKNOWN"):
        return ("UNTRUSTED",)
    chk = (recorded or {}).get("membership_check") or {}
    if (not chk.get("rebalances_checked") or chk.get("rebalances_outside") != 0 or chk.get("rebalances_unaccounted") != 0
            or chk.get("rebalances_checked") != len(chk.get("rebalances") or []) or not set(periods or []) <= set(chk.get("rebalances") or [])):
        return ("UNTRUSTED",)                                                   # no clean check of the ranking against the membership at every period
    limited = d.get("survivorship_bias") != "ABSENT" or bool(chk.get("members_without_data"))
    if m == "POINT_IN_TIME":
        if msum.get("kind") != "POINT_IN_TIME" or any(msum.get(k) is None for k in ("securities", "intervals", "removals", "sha256")):
            return ("UNTRUSTED",)
        cov = (d.get("coverage_start") is not None and d.get("coverage_end") is not None and start is not None and end is not None
               and d["coverage_start"] <= start and d["coverage_end"] >= end)
        no_removal = msum["removals"] == 0 and any(d.get(f) is True for f in ("includes_delisted", "includes_acquired", "includes_bankrupt"))
        return ("TRUSTED",) if complete and cov and not limited and not no_removal else ("LIMITED",)
    if msum.get("kind") != "HISTORICAL_SNAPSHOT" or any(msum.get(k) is None for k in ("snapshots", "first", "last", "sha256")):
        return ("UNTRUSTED",)
    return ("TRUSTED", "LIMITED") if complete and not limited else ("LIMITED",)


def cap_edge(raw: dict, integrity: dict) -> dict:
    """The FINAL selection edge: the raw v3 edge lowered to the integrity's cap (never raised); the raw edge is kept beside it."""
    raw = dict(raw or {})
    st, status = raw.get("status"), (integrity or {}).get("status")
    base = {"raw_status": st, "universe_integrity": status, "capped": False}
    if status is None:                                                          # missing / invalid declaration: no assessment at all
        return {**raw, **base, "status": "INVALID", "label": EDGE_LABELS["INVALID"],
                "reasons": [{"code": "UNIVERSE_NOT_DECLARED", "text": "The universe is not (validly) declared: the selection edge cannot be assessed."}]}
    cap = (integrity or {}).get("cap")
    if cap is None or st not in EDGE_LADDER or EDGE_LADDER.index(st) <= EDGE_LADDER.index(cap):
        return {**raw, **base}
    text = (f"The raw selection edge is {st}; the universe is {status} ({integrity.get('method')}), so the final edge is at most {cap}. "
            + CONSEQUENCE[status])
    return {**raw, **base, "status": cap, "label": EDGE_LABELS[cap], "capped": True,
            "reasons": [{"code": "UNIVERSE_INTEGRITY_CAP", "text": text}] + list(raw.get("reasons") or [])}


# ---------------------------------------------------------------- stored runs: an append-only assessment ---------------------------------------------
def assess_stored_run(run: dict, declaration: dict, evaluation: Optional[dict] = None, created_at: Optional[str] = None, git_sha: Optional[str] = None) -> dict:
    """A universe-integrity assessment of a stored cross-sectional run: the raw edge is the RECORDED one (the run's, or its methodology evaluation's),
    the integrity is classified from the strategy's declaration, and the final edge / overall verdict follow the policy. Nothing is recomputed."""
    from strategy_validation import cross_sectional as CS
    if run.get("universe_integrity"):
        raise UniverseError(f"run {run.get('run_id')} already carries its universe integrity")
    src = evaluation or run
    if src.get("evaluation_mode") != CS.CROSS_SECTIONAL:
        raise UniverseError(f"run {run.get('run_id')} has no cross-sectional selection edge to assess")
    if evaluation is not None and evaluation.get("run_id") != run.get("run_id"):
        raise UniverseError("the methodology evaluation does not reference this run")
    raw = dict(src.get("selection_edge") or {})
    if not raw.get("status"):
        raise UniverseError("the recorded selection edge is missing")
    integrity = classify(declaration, None, None, run.get("dataset_start"), run.get("dataset_end"))
    final = cap_edge(raw, integrity)
    perf = dict(src.get("performance_verdict") or run.get("verdict") or {})
    return {"assessment_id": uuid.uuid4().hex, "run_id": run["run_id"], "evaluation_id": (evaluation or {}).get("evaluation_id"),
            "strategy_id": run["strategy_id"], "strategy_version": run["strategy_version"], "dataset_id": run["dataset_id"],
            "policy_version": UNIVERSE_POLICY_VERSION, "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_commit_sha": git_sha,
            "universe_integrity": integrity, "raw_selection_edge": raw, "selection_edge": final, "performance_verdict": perf,
            "verdict": CS.overall_verdict(perf, final["status"]),
            "note": (f"A {UNIVERSE_POLICY_VERSION} assessment of run {run['run_id']}" + (f" (raw edge from methodology evaluation {evaluation['evaluation_id']})"
                     if evaluation else "") + ". The run and its evaluation are unchanged.")}
