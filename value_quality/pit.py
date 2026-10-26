"""value_quality/pit.py — POINT-IN-TIME fundamentals from SEC XBRL facts (each row: start?, end, val, filed, form, fy, fp, accn).

  available_at   a fact is usable from the AVAILABILITY_LAG_SESSIONS-th session AFTER its filing date (filing times are not used: conservative)
  restatements   per period (start, end) the highest-priority CONCEPT visible at T wins (value_quality.CONCEPTS), and within it the LATEST
                 filing visible at T; a restated value is invisible until the restating filing is available, after which it replaces the original
  instant        balance-sheet items: the latest period end visible at T
  quarters       fiscal quarters (12-17 weeks) visible at T: a direct duration, else the difference of two year-to-date durations of the same
                 fiscal year (Q4 = FY - 9M; cash-flow items are reported year-to-date only); available when BOTH filings are available
  ttm            the sum of the latest four CONTIGUOUS quarters spanning one fiscal year (else missing - never padded)
  shares         weighted-average diluted shares of the latest period visible at T, re-based by the split ratio from its FILING date (the basis
                 the filing used) to T; a filing before the split-ratio history starts cannot be re-based and is unusable

Every returned value carries its period_end, filed, available_at and accession(s). Nothing here reads a fact filed after T."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

from value_quality import AVAILABILITY_LAG_SESSIONS, QUARTER_DAYS, TTM_SPAN_DAYS

Q_DAYS = QUARTER_DAYS
FY_DAYS = (350, 380)


def days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


class Calendar:
    """The trading sessions of the dataset (ascending ISO dates)."""

    def __init__(self, sessions: List[str]):
        self.s = sorted(sessions)

    def available_at(self, filed: str) -> Optional[str]:
        i = bisect_right(self.s, filed) + AVAILABILITY_LAG_SESSIONS - 1             # the lag-th session strictly after the filing date
        return self.s[i] if 0 <= i < len(self.s) else None

    def on_or_before(self, d: str) -> Optional[str]:
        i = bisect_right(self.s, d) - 1
        return self.s[i] if i >= 0 else None


def visible(rows: List[dict], T: str, cal: Calendar) -> List[dict]:
    out = []
    for r in rows or []:
        av = r.get("available_at") or cal.available_at(str(r["filed"]))          # pre-annotated by value_quality/data.py (same rule)
        if av is not None and av <= T:
            out.append({**r, "available_at": av})
    return out


def latest_per_period(rows: List[dict]) -> Dict[tuple, dict]:
    """Per period: the highest-priority concept (lowest `priority`), then the latest filing - among the rows given (already point in time)."""
    best: Dict[tuple, dict] = {}
    for r in rows:
        k = (r.get("start"), r["end"])
        b = best.get(k)
        if b is None or (-int(r.get("priority") or 0), r["filed"], r.get("accn") or "") > (-int(b.get("priority") or 0), b["filed"], b.get("accn") or ""):
            best[k] = r
    return best


def instant(rows: List[dict], T: str, cal: Calendar) -> Optional[dict]:
    vis = latest_per_period(visible(rows, T, cal))
    if not vis:
        return None
    k = max(vis, key=lambda k: (k[1], vis[k]["filed"]))
    r = vis[k]
    return {"value": float(r["val"]), "period_end": r["end"], "filed": r["filed"], "available_at": r["available_at"], "accn": [r.get("accn")],
            "concept": r.get("concept")}


def quarters(rows: List[dict], T: str, cal: Calendar) -> Dict[str, dict]:
    vis = latest_per_period(visible(rows, T, cal))
    q: Dict[str, dict] = {}
    ytd: Dict[str, list] = defaultdict(list)
    for (s, e), r in vis.items():
        if not s:
            continue
        n = days(s, e)
        rec = {"value": float(r["val"]), "filed": r["filed"], "available_at": r["available_at"], "accn": [r.get("accn")], "concept": [r.get("concept")]}
        if Q_DAYS[0] <= n <= Q_DAYS[1]:
            q[e] = {**rec, "start": s, "end": e, "derived": False}
        if Q_DAYS[0] <= n <= FY_DAYS[1]:
            ytd[s].append((e, rec))
    for s, seq in ytd.items():                                                     # year-to-date differences within one fiscal year
        seq.sort(key=lambda x: x[0])
        for (pe, pr), (e, r) in zip(seq, seq[1:]):
            if e in q or not (Q_DAYS[0] <= days(pe, e) <= Q_DAYS[1]):
                continue
            q[e] = {"value": r["value"] - pr["value"], "start": pe, "end": e, "derived": True, "filed": max(r["filed"], pr["filed"]),
                    "available_at": max(r["available_at"], pr["available_at"]), "accn": pr["accn"] + r["accn"], "concept": pr["concept"] + r["concept"]}
    return q


def ttm(rows: List[dict], T: str, cal: Calendar) -> Optional[dict]:
    qs = sorted(quarters(rows, T, cal).values(), key=lambda x: x["end"])
    if len(qs) < 4:
        return None
    last = qs[-4:]
    if not all(Q_DAYS[0] <= days(a["end"], b["end"]) <= Q_DAYS[1] for a, b in zip(last, last[1:])):
        return None                                                                 # a gap: four contiguous quarters are not visible
    if not TTM_SPAN_DAYS[0] <= days(last[0]["start"], last[-1]["end"]) <= TTM_SPAN_DAYS[1]:
        return None                                                                 # four quarters that do not make one fiscal year
    return {"value": sum(x["value"] for x in last), "period_end": last[-1]["end"], "filed": max(x["filed"] for x in last),
            "available_at": max(x["available_at"] for x in last), "quarters": [x["end"] for x in last], "derived_quarters": sum(1 for x in last if x["derived"]),
            "accn": sorted({a for x in last for a in x["accn"] if a}), "concept": sorted({c for x in last for c in x["concept"] if c})}


def shares(rows: List[dict], T: str, cal: Calendar, split_ratio: Dict[str, float]) -> Optional[dict]:
    vis = latest_per_period(visible(rows, T, cal))
    vis = {k: r for k, r in vis.items() if k[0]}
    if not vis or not split_ratio:
        return None
    k = max(vis, key=lambda k: (k[1], -days(k[0], k[1])))                         # the latest period; the shortest duration ending there
    r = vis[k]
    first = min(split_ratio)
    if r["filed"] < first:
        return None                                                                 # its split basis cannot be established
    at_filed = split_ratio.get(cal.on_or_before(r["filed"]) or "")
    at_t = split_ratio.get(T)
    if not at_filed or not at_t:
        return None
    factor = at_filed / at_t
    return {"value": float(r["val"]) * factor, "reported": float(r["val"]), "split_factor": round(factor, 6), "period_end": r["end"], "filed": r["filed"],
            "available_at": r["available_at"], "accn": [r.get("accn")], "concept": r.get("concept")}
