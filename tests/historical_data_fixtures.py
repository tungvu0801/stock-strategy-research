"""tests/historical_data_fixtures.py - SYNTHETIC fixtures for the MD-A historical_data tests. No network, no real file, no database.

  spec() / build(spec)   the look-ahead world: three instruments on the real 2026-02-02..2026-04-30 sessions - AAA with a 2-for-1 split
                         announced 10 sessions before its ex-date, BBB with a cash dividend whose known_at is UNKNOWN and a removed invalid
                         row, CCC listed later - plus POINT_IN_TIME membership changes and scheduled events, each with its known_at
  perturb(spec, t)       every value that becomes known AFTER t changed (prices, events, membership, scheduled events, a new future split)
  cut(spec, t)           the data as it was at t (factors recomputed as of the last session closed by t)
  dump(view) / compare   a view as plain data, compared exactly except the declared snapshot factors (rel 1e-12, contract v2 A6)
  series / provider      small datasets for the section 5 rules
  write_ms1_archive      an archive in the market_intel/archive.py RawArchive format (gzip mtime 0 + manifest.jsonl)
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from historical_data import calendar as CAL  # noqa: E402
from historical_data.adapters import synthetic as SY  # noqa: E402
from historical_data.schema import CorporateEvent, MembershipChange, ScheduledEvent  # noqa: E402
from historical_data.status import unknown  # noqa: E402

US = timedelta(microseconds=1)
S = CAL.sessions("2026-02-02", "2026-04-30")
U = "PIT_UNIVERSE"
FACTORS = ("split_factor", "total_return_factor")


def base_close(j: int, k: int) -> float:
    return 50.0 + 5.0 * j + 0.3 * k + 0.7 * ((k * 7 + j) % 5)


def spec() -> dict:
    first = {"AAA": 0, "BBB": 0, "CCC": 25}
    closes = {}
    for j, (sym, f) in enumerate(first.items()):
        closes[sym] = {S[k]: base_close(j, k) * (0.5 if sym == "AAA" and k >= 30 else 1.0) for k in range(f, len(S))}
    return {
        "first": first,
        "closes": closes,
        "skip": [["AAA", S[10]]],                                            # a NO_BAR gap
        "invalid": [["BBB", S[12]]],                                         # a row that breaks section 3.2 -> REMOVED_INVALID
        "events": [
            {"id": "E-SPLIT-AAA", "sym": "AAA", "type": "SPLIT", "eff": S[30], "known_at": CAL.close_utc(S[20]), "ratio": 2.0},
            {"id": "E-DIV-BBB", "sym": "BBB", "type": "CASH_DIVIDEND", "eff": S[40], "known_at": None, "cash": 0.5},
        ],
        "changes": [
            {"sym": "AAA", "session": S[0], "action": "ADD", "known_at": CAL.open_utc(S[0]) - timedelta(hours=1)},
            {"sym": "BBB", "session": S[0], "action": "ADD", "known_at": CAL.open_utc(S[0]) - timedelta(hours=1)},
            {"sym": "CCC", "session": S[25], "action": "ADD", "known_at": CAL.close_utc(S[24])},
            {"sym": "BBB", "session": S[50], "action": "REMOVE", "known_at": CAL.close_utc(S[45])},
        ],
        "scheduled": [
            {"id": "SCH-AAA", "sym": "AAA", "session": S[35], "timing": "AFTER_CLOSE", "known_at": CAL.close_utc(S[15])},
            {"id": "SCH-BBB", "sym": "BBB", "session": S[55], "timing": "UNKNOWN", "known_at": CAL.close_utc(S[44])},
        ],
    }


def iid(sp: dict, sym: str) -> str:
    return f"SYNTH:{sym}:{S[sp['first'][sym]]}"


def visible_from(e: dict):
    return e["known_at"] if e["known_at"] is not None else CAL.open_utc(e["eff"])


def _factors(sp: dict, sym: str, as_of: str):
    ds = sorted(sp["closes"][sym])
    sf, tr = {}, {}
    evs = [e for e in sp["events"] if e["sym"] == sym and e["eff"] <= as_of]
    for d in ds:
        a = b = 1.0
        for e in evs:
            if d < e["eff"]:
                if e["type"] == "SPLIT":
                    a *= e["ratio"]
                    b *= e["ratio"]
                else:
                    prev = max(x for x in ds if x < e["eff"])
                    c = sp["closes"][sym][prev]
                    b *= c / (c - e["cash"])
        sf[d], tr[d] = a, b
    return sf, tr


def build(sp: dict, *, as_of=None, scale=None, kind="SYNTHETIC", licence=None, price_views=None, dataset_id="synthetic:lookahead"):
    as_of = as_of or max(d for c in sp["closes"].values() for d in c)
    insts = [SY.instrument(sym, S[f]) for sym, f in sp["first"].items()]
    skip, invalid = {tuple(x) for x in sp["skip"]}, {tuple(x) for x in sp["invalid"]}
    rows = []
    for sym in sp["first"]:
        sf, tr = _factors(sp, sym, as_of)
        k = (scale or {}).get(sym, 1.0)
        prev = None
        for d in sorted(sp["closes"][sym]):
            c = sp["closes"][sym][d]
            o = prev if prev is not None else c
            prev = c
            if (sym, d) in skip:
                continue
            lo = min(o, c) * (1.5 if (sym, d) in invalid else 0.99)
            rows.append(dict(instrument_id=iid(sp, sym), session=d, o=o, h=max(o, c) * 1.01, l=lo, c=c, v=1_000_000.0,
                             split_factor=sf[d] * k, total_return_factor=tr[d] * k))
    events = []
    for e in sp["events"]:
        kw = {"ratio": e["ratio"]} if e["type"] == "SPLIT" else {"cash_amount": e["cash"]}
        events.append(CorporateEvent(event_id=e["id"], instrument_id=iid(sp, e["sym"]), type=e["type"], effective_session=e["eff"],
                                     known_at=e["known_at"] if e["known_at"] is not None else unknown("NOT_IN_SOURCE"), **kw))
    changes = [MembershipChange(U, iid(sp, c["sym"]), c["session"], c["action"], c["known_at"]) for c in sp["changes"]]
    sched = [ScheduledEvent(s["id"], iid(sp, s["sym"]), s["session"], s["timing"], s["known_at"]) for s in sp["scheduled"]]
    kw = {} if price_views is None else {"price_views": price_views}
    return SY.SyntheticProvider(instruments=insts, rows=rows, events=events, membership=changes, scheduled_events=sched, events_completeness="COMPLETE",
                                membership_basis="POINT_IN_TIME", kind=kind, licence=licence, dataset_id=dataset_id, adjustment_as_of=as_of, **kw)


def perturb(sp: dict, t) -> dict:
    """Every value that becomes knowable after t, changed."""
    p = copy.deepcopy(sp)
    for sym, closes in p["closes"].items():
        for d in closes:
            if CAL.close_utc(d) > t:
                closes[d] *= 1.0625
    for e in p["events"]:
        if visible_from(e) > t:
            if e["type"] == "SPLIT":
                e["ratio"] *= 2.0
            else:
                e["cash"] *= 1.5
    if CAL.close_utc(S[59]) > t:
        p["events"].append({"id": "E-FUTURE-CCC", "sym": "CCC", "type": "SPLIT", "eff": S[60], "known_at": CAL.close_utc(S[59]), "ratio": 2.0})
    for c in p["changes"]:
        if c["known_at"] > t:
            c["session"] = CAL.next_session(c["session"])
    for s in p["scheduled"]:
        if s["known_at"] > t:
            s["timing"], s["session"] = "BEFORE_OPEN", CAL.next_session(s["session"])
    return p


def cut(sp: dict, t) -> dict:
    """The data as it existed at t."""
    p = copy.deepcopy(sp)
    p["closes"] = {sym: {d: c for d, c in closes.items() if CAL.close_utc(d) <= t} for sym, closes in p["closes"].items()}
    p["events"] = [e for e in p["events"] if visible_from(e) <= t]
    p["changes"] = [c for c in p["changes"] if c["known_at"] <= t]
    p["scheduled"] = [s for s in p["scheduled"] if s["known_at"] <= t]
    return p


def dump(view) -> dict:
    ids = [i.instrument_id for i in view.instruments()]
    sched = {}
    for i in ids:
        x = view.scheduled_events(i)
        sched[i] = x.to_json() if hasattr(x, "status") else [s.to_json() for s in x]
    return {"manifest": view.manifest(), "instruments": [i.to_json() for i in view.instruments()],
            "bars": {i: [b.to_json() for b in view.bars(i)] for i in ids}, "gaps": {i: [g.to_json() for g in view.gaps(i)] for i in ids},
            "events": [e.to_json() for e in view.events()], "membership": {s: view.membership(U, s).to_json() for s in S}, "scheduled": sched}


def leaky_dump(provider) -> dict:
    """A STAND-IN view with no known_at filter (the full history): the control that the comparison detects a leak."""
    ids = [i.instrument_id for i in provider.instruments()]
    return {"manifest": provider.manifest().view_json(), "instruments": [i.to_json() for i in provider.instruments()],
            "bars": {i: [b.to_json() for b in provider.bars(i)] for i in ids}, "gaps": {i: [g.to_json() for g in provider.gaps(i)] for i in ids},
            "events": [e.to_json() for e in provider.events()], "membership": {s: provider.membership(U, s).to_json() for s in S},
            "scheduled": {i: [s.to_json() for s in provider.scheduled_events(i)] for i in ids}}


def differences(a, b, rel: float = 0.0, path: str = "") -> list:
    """Where two dumps differ; the snapshot factors may differ by at most `rel` (A6), everything else must be identical."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(set(a) ^ set(b))[:5]}"]
        return [x for k in sorted(a) for x in differences(a[k], b[k], rel, f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        return [x for i, (u, v) in enumerate(zip(a, b)) for x in differences(u, v, rel, f"{path}[{i}]")]
    if path.rsplit(".", 1)[-1] in FACTORS and isinstance(a, float) and isinstance(b, float):
        return [] if (a == b or abs(a - b) <= rel * max(abs(a), abs(b))) else [f"{path}: {a!r} != {b!r}"]
    return [] if a == b and type(a) is type(b) else [f"{path}: {a!r} != {b!r}"]


# ------------------------------------------------------------------------------------------------ small datasets for section 5
def series(symbol: str, sessions, closes, *, sf=None, trf=None, prefix="SYNTH", inst_kw=None, **bar_kw):
    """(instrument, bars): open = the previous close, high / low 1 % around."""
    inst = SY.instrument(symbol, sessions[0], prefix=prefix, **(inst_kw or {}))
    out, prev = [], None
    for k, (d, c) in enumerate(zip(sessions, closes)):
        o = prev if prev is not None else c
        prev = c
        out.append(SY.bar(inst.instrument_id, d, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1_000_000.0,
                          split_factor=sf[k] if sf is not None else 1.0, total_return_factor=trf[k] if trf is not None else 1.0, **bar_kw))
    return inst, out


def provider(*pairs, **kw):
    return SY.SyntheticProvider(instruments=[p[0] for p in pairs], bars=[b for p in pairs for b in p[1]], **kw)


def smooth(n: int, start: float = 100.0, step: float = 0.002):
    return [start * (1.0 + step) ** k for k in range(n)]


def codes(record, severity=None):
    return sorted({r["code"] for r in record["reasons"] if severity is None or r["severity"] == severity})


# ------------------------------------------------------------------------------------------------ an MS-1-format raw archive
def ms1_bar(session: str, o, h, l, c, v=100_000):
    from datetime import date
    d = date.fromisoformat(session)
    dst = date(d.year, 3, 8) + timedelta(days=(6 - date(d.year, 3, 8).weekday()) % 7) <= d < date(d.year, 11, 1) + timedelta(days=(6 - date(d.year, 11, 1).weekday()) % 7)
    return {"t": f"{session}T0{4 if dst else 5}:00:00Z", "o": o, "h": h, "l": l, "c": c, "v": v, "n": 100, "vw": c}


def write_ms1_archive(root: Path, bodies: dict, *, retrieved_at="2026-11-15T20:42:10.347658+00:00") -> Path:
    """root/raw/manifest.jsonl + root/raw/2026-11-15/<fetch_id>.json.gz, as market_intel.archive.RawArchive.write lays them out.
    `bodies`: {symbol: list of bars | a dict body (e.g. an error envelope)}."""
    raw = root / "raw"
    (raw / "2026-11-15").mkdir(parents=True, exist_ok=True)
    lines = []
    for n, (sym, bars) in enumerate(bodies.items()):
        doc = bars if isinstance(bars, dict) else {"bars": bars, "symbol": sym, "next_page_token": None}
        body = json.dumps(doc).encode("utf-8")
        fid = hashlib.md5(f"{sym}-{n}".encode()).hexdigest()
        rel = f"2026-11-15/{fid}.json.gz"
        (raw / rel).write_bytes(gzip.compress(body, mtime=0))
        lines.append(json.dumps({"bytes": len(body), "endpoint": f"/v2/stocks/{sym}/bars", "fetch_id": fid, "path": rel, "provider": "ALPACA",
                                 "retrieved_at": retrieved_at, "sha256": hashlib.sha256(body).hexdigest()}, sort_keys=True))
    (raw / "manifest.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return raw


DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal")


def tree_snapshot(root: Path, *, hash_under: str = "raw") -> dict:
    """{relative path: (size, mtime_ns, sha256 | None)} of every file under root. Only files under `hash_under` are read (byte reads);
    every other file - a database above all - is only stat()-ed, never opened."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            read = rel.startswith(hash_under + "/") and not rel.lower().endswith(DB_SUFFIXES)
            out[rel] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if read else None)
    return out


def finite(x) -> bool:
    return isinstance(x, float) and math.isfinite(x)
