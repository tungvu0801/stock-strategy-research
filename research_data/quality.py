"""research_data/quality.py — the read-only PRE-RUN data-quality report of a daily research dataset (Stage 7.8B-1, decision lock R8 / R22).

    python -m research_data.quality --dataset FILE --out REPORT.json [--provenance OVERRIDE.json] [--approval "OPERATOR-APPROVAL:<ref>"]

Data checks only: no strategy rule, indicator, price statistic or result is computed. Each check has a severity:
  BLOCK    the dataset cannot be evaluated (the report's status is BLOCKED)
  FINDING  the operator must accept it before any run (status REVIEW_REQUIRED)
  INFO     recorded

  Q01 identity       the file's sha256, kind, symbols, sessions, first / last session
  Q02 provenance     the harness's required and conditional fields, and a volume_feed of SIP or IEX (BLOCK if missing)
  Q03 coverage       the benchmark present (BLOCK); every universe symbol present (FINDING lists the absent ones); late starters (INFO)
  Q04 calendar       every date is a benchmark session (BLOCK otherwise); the benchmark's sessions equal the static NYSE calendar over its span:
                     an extra date, a missing session or a weekend BLOCKS; a span outside 2016-2026 BLOCKS; when the builder declares its
                     requested window, the benchmark has exactly the window's sessions (a session missing at an edge BLOCKS)
  Q05 missing bars   per symbol, sessions inside its own span without a bar (FINDING), and the share of symbol-sessions whose 200 sessions are
                     complete (INFO; the data side of the swing baseline's warm-up rule)
  Q06 raw series     raw_close for every universe symbol (BLOCK if absent: final-review M-4); raw_volume for every universe symbol when the volume
                     feed is SIP (BLOCK if absent); bar dates without a raw close, or on SIP without a raw volume (FINDING)
  Q07 issuers        every universe symbol mapped, none under two issuers (BLOCK); per issuer with several listings, each listing's first / last
                     bar and the kept listing (alphabetically first held); a kept listing that starts later than another (FINDING: M-7)
  Q08 corporate      split-ratio changes (INFO) and raw / split / adjusted inconsistencies found by the Stage 6.7 scanner (FINDING each)
  Q09 invalid bars   bars the builder removed (INFO); bars with zero volume (FINDING)

The report carries its own sha256 (canonical JSON without that field) and the approval reference it was made under. A report on a HISTORICAL
dataset needs an approval reference."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from research_data import BENCHMARK, RESEARCH_UNIVERSE, approved, output_problem
from research_data import nyse_calendar as CAL

REPORT_VERSION = "pre_run_report_v1"
WINDOW = 200                                     # the swing baseline's 200-session completeness window (data side only)
SPLIT_EVENT = 0.01                               # a raw / split ratio move above 1 % is listed as a split-ratio event


class ReportRefused(PermissionError):
    pass


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def report_sha256(rep: dict) -> str:
    return hashlib.sha256(_canon({k: v for k, v in rep.items() if k != "report_sha256"})).hexdigest()


def report(path: str, *, provenance: Optional[dict] = None, approval: Optional[str] = None) -> dict:
    from strategy_validation import PROVENANCE_CONDITIONAL, PROVENANCE_REQUIRED
    from strategy_validation.dataset import load
    raw = Path(path).read_bytes()
    ds = load(str(path), provenance=provenance)
    if ds.kind == "HISTORICAL" and not approved(approval):
        raise ReportRefused("a report on a HISTORICAL dataset needs an operator approval reference (OPERATOR-APPROVAL:<reference>)")
    checks: List[dict] = []

    def add(code: str, severity: str, title: str, **detail) -> None:
        checks.append({"code": code, "severity": severity, "title": title, **detail})

    prov = dict(ds.declared)
    universe = [s for s in RESEARCH_UNIVERSE]
    present = [s for s in universe if s in ds.bars]
    add("Q01", "INFO", "identity", sha256=hashlib.sha256(raw).hexdigest(), kind=ds.kind, symbols=len(ds.bars), sessions=len(ds.sessions),
        first=ds.start, last=ds.end)
    # Q02 provenance
    req = list(PROVENANCE_REQUIRED) + list(PROVENANCE_CONDITIONAL)
    absent = [k for k in req if prov.get(k) in (None, "", [])]
    feed = prov.get("volume_feed")
    if absent:
        add("Q02", "BLOCK", "provenance incomplete", missing=absent)
    if feed not in ("SIP", "IEX"):
        add("Q02", "BLOCK", "no volume_feed declared (SIP or IEX)", volume_feed=feed)
    if not absent and feed in ("SIP", "IEX"):
        add("Q02", "INFO", "provenance complete", volume_feed=feed, fields=sorted(prov))
    # Q03 coverage
    spy = [b.date for b in ds.bars.get(BENCHMARK, [])]
    if not spy:
        add("Q03", "BLOCK", "the benchmark has no bars", benchmark=BENCHMARK)
    absent_syms = [s for s in universe if s not in ds.bars]
    if absent_syms:
        add("Q03", "FINDING", "universe symbols without bars", symbols=absent_syms)
    late = {s: ds.bars[s][0].date for s in present if spy and ds.bars[s][0].date > spy[0]}
    if late:
        add("Q03", "INFO", "symbols starting after the benchmark's first session", starts=late)
    # Q04 calendar
    if spy:
        stray = sorted(set(ds.sessions) - set(spy))
        if stray:
            add("Q04", "BLOCK", "dates that are not benchmark sessions", dates=stray[:50], count=len(stray))
        cmp = CAL.compare(spy)
        if cmp["status"] != "MATCH" or cmp.get("weekend"):
            add("Q04", "BLOCK", "the benchmark calendar differs from the NYSE calendar", comparison=cmp)
        else:
            add("Q04", "INFO", "the benchmark calendar equals the NYSE calendar", sessions=cmp["sessions"], first=cmp["start"], last=cmp["end"],
                source=CAL.SOURCE)
        win = prov.get("window")
        if isinstance(win, dict) and win.get("start") and win.get("end"):              # the builder's requested window [start, end)
            try:
                last = (date.fromisoformat(str(win["end"])[:10]) - timedelta(days=1)).isoformat()
                want = CAL.sessions(str(win["start"])[:10], last)
            except (CAL.CalendarError, ValueError) as exc:
                add("Q04", "BLOCK", "the requested window cannot be checked against the NYSE table", window=win, detail=str(exc))
            else:
                missing, outside = sorted(set(want) - set(spy)), sorted(set(spy) - set(want))
                if missing or outside:
                    add("Q04", "BLOCK", "the benchmark does not hold exactly the requested window's sessions", window=win, missing=missing[:50],
                        outside=outside[:50], count=len(missing) + len(outside))
    # Q05 missing bars inside each symbol's span, and the data side of the 200-session window
    if spy:
        holes: Dict[str, List[str]] = {}
        possible = complete = 0
        for s in present:
            have = {b.date for b in ds.bars[s]}
            first = ds.bars[s][0].date
            h = [d for d in spy if d >= first and d not in have]
            if h:
                holes[s] = h
            flags = [1 if d in have else 0 for d in spy]
            pref = [0]
            for x in flags:
                pref.append(pref[-1] + x)
            for i in range(WINDOW - 1, len(spy)):
                if flags[i]:
                    possible += 1
                    complete += pref[i + 1] - pref[i + 1 - WINDOW] == WINDOW
        if holes:
            by_date: Dict[str, int] = {}
            for h in holes.values():
                for d in h:
                    by_date[d] = by_date.get(d, 0) + 1
            add("Q05", "FINDING", "sessions without a bar inside a symbol's own span (never filled)", symbols=len(holes), holes=sum(by_date.values()),
                by_date=dict(sorted(by_date.items())), by_symbol={s: v for s, v in sorted(holes.items())})
        add("Q05", "INFO", "symbol-sessions with a complete 200-session window (data only)", possible=possible, complete=complete,
            share=round(complete / possible, 4) if possible else None)
    # Q06 raw series
    series = (ds.aux or {}).get("series") or {}
    rc, rv = series.get("raw_close") or {}, series.get("raw_volume") or {}
    no_rc = [s for s in present if not rc.get(s)]
    if no_rc:
        add("Q06", "BLOCK", "symbols without a raw_close series", symbols=no_rc)
    no_rv = [s for s in present if not rv.get(s)]
    if no_rv and feed == "SIP":
        add("Q06", "BLOCK", "symbols without a raw_volume series (SIP: the liquidity rule reads it)", symbols=no_rv)
    elif no_rv:
        add("Q06", "INFO", "symbols without a raw_volume series (IEX: the liquidity rule is not applied)", symbols=no_rv)
    gaps = {s: [b.date for b in ds.bars[s] if b.date not in (rc.get(s) or {})] for s in present if rc.get(s)}
    gaps = {s: v for s, v in gaps.items() if v}
    if gaps:
        add("Q06", "FINDING", "bar dates without a raw close", by_symbol={s: v[:20] for s, v in gaps.items()}, count=sum(len(v) for v in gaps.values()))
    if feed == "SIP":
        vgaps = {s: [b.date for b in ds.bars[s] if b.date not in (rv.get(s) or {})] for s in present if rv.get(s)}
        vgaps = {s: v for s, v in vgaps.items() if v}
        if vgaps:
            add("Q06", "FINDING", "bar dates without a raw volume (SIP)", by_symbol={s: v[:20] for s, v in vgaps.items()},
                count=sum(len(v) for v in vgaps.values()))
    # Q07 issuers
    iss = (((ds.aux or {}).get("static") or {}).get("issuers")) or {}
    of: Dict[str, List[str]] = {}
    for iid, info in iss.items():
        for s in (info or {}).get("symbols") or []:
            of.setdefault(s, []).append(str(iid))
    unmapped = [s for s in present if s not in of]
    doubled = {s: v for s, v in of.items() if len(v) > 1}
    if unmapped:
        add("Q07", "BLOCK", "universe symbols without an issuer", symbols=unmapped)
    if doubled:
        add("Q07", "BLOCK", "symbols listed under two issuers", symbols=doubled)
    for iid, info in sorted(iss.items()):
        held = sorted(s for s in (info or {}).get("symbols") or [] if s in ds.bars)
        if len(held) > 1:
            span = {s: [ds.bars[s][0].date, ds.bars[s][-1].date] for s in held}
            kept = held[0]
            later = [s for s in held if span[s][0] < span[kept][0]]
            add("Q07", "FINDING" if later else "INFO", "issuer with several listings: the alphabetically first is kept", issuer=iid, kept=kept,
                listings=span, earlier_listings_than_kept=later)
    # Q08 corporate actions
    ratio = series.get("split_ratio") or {}
    events = []
    for s in sorted(ratio):
        ds_ = sorted(ratio[s])
        for a, b in zip(ds_, ds_[1:]):
            if ratio[s][a] and ratio[s][b] and abs(ratio[s][b] / ratio[s][a] - 1.0) > SPLIT_EVENT:
                events.append({"symbol": s, "date": b, "from": ratio[s][a], "to": ratio[s][b]})
    add("Q08", "INFO", "split-ratio changes", events=events, count=len(events))
    if ratio and rc:
        from breakout.audit import inconsistent_pairs                       # the Stage 6.7 raw / split / adjusted consistency scanner
        for ev in inconsistent_pairs(ds, present):
            add("Q08", "FINDING", "raw / split / adjusted inconsistency (Stage 6.7 scanner)", symbol=ev["symbol"], first=ev["from"], to=ev["to"],
                pair_code=ev["code"], step=ev["step"])
    # Q09 invalid and zero-volume bars
    if prov.get("invalid_bars_removed"):
        add("Q09", "INFO", "bars removed by the builder (gaps, never repaired)", bars=prov["invalid_bars_removed"])
    zero = {s: [b.date for b in ds.bars[s] if not b.volume] for s in present}
    zero = {s: v for s, v in zero.items() if v}
    if zero:
        add("Q09", "FINDING", "bars with zero volume", by_symbol={s: v[:20] for s, v in zero.items()}, count=sum(len(v) for v in zero.values()))
    sev = {c["severity"] for c in checks}
    status = "BLOCKED" if "BLOCK" in sev else ("REVIEW_REQUIRED" if "FINDING" in sev else "CLEAN")
    rep = {"report_version": REPORT_VERSION, "dataset": {"path_name": Path(path).name, "sha256": hashlib.sha256(raw).hexdigest(), "kind": ds.kind,
                                                         "dataset_id": ds.dataset_id, "volume_feed": feed, "first": ds.start, "last": ds.end,
                                                         "sessions": len(ds.sessions), "symbols": len(ds.bars)},
           "provenance_override": dict(provenance or {}), "approval": approval, "status": status, "checks": checks,
           "counts": {k: sum(1 for c in checks if c["severity"] == k) for k in ("BLOCK", "FINDING", "INFO")}}
    rep["report_sha256"] = report_sha256(rep)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m research_data.quality", description="the read-only pre-run data-quality report")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--provenance", default=None, help="a JSON object merged over the file's provenance (e.g. {\"volume_feed\": \"IEX\"})")
    ap.add_argument("--approval", default=None)
    a = ap.parse_args(argv)
    prov = json.loads(Path(a.provenance).read_text(encoding="utf-8")) if a.provenance else None
    try:
        rep = report(a.dataset, provenance=prov, approval=a.approval)
    except ReportRefused as exc:
        print(f"REFUSED: {exc}")
        return 2
    out = Path(a.out)
    problem = output_problem(out)
    if problem or out.exists():
        print(f"REFUSED: {problem or f'{out} exists (a report is never overwritten)'}")
        return 2
    out.write_text(json.dumps(rep, indent=1, sort_keys=True), encoding="utf-8")
    print(f"{rep['status']}: {rep['counts']} - report sha256 {rep['report_sha256']}")
    return 0 if rep["status"] != "BLOCKED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
