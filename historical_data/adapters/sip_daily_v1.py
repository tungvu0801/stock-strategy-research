"""
historical_data/adapters/sip_daily_v1.py - SipDailyV1Provider: the sip_daily_v1 bundle (<out>/sip_bundle.json, built by the Stage 7.8B-1
builder's offline build step) plus its archived raw responses (<out>/manifest.json + <out>/raw/*.json.gz, each sha256 verified) for the
raw open / high / low. Read only; nothing is written; no network. Fixture-tested only: no real sip_daily_v1 dataset exists.

Mapping (contract section 4.1): raw_close / raw_volume from aux.series; raw open / high / low from the archived "raw" adjustment responses
(when none is archived the dataset is integrity-only: RAW_OHL is not offered and as_of / execution_open refuse); split_factor = aux.series
split_ratio (raw / split-adjusted close, 6 decimals); total_return_factor = raw_close / the all-adjusted close on dates present in both (a
removed all-adjusted row leaves it UNAVAILABLE); volume_feed and price_source CONSOLIDATED; instrument_id ALPACA:<symbol>:<first session>,
symbol history UNKNOWN; FIXED_CURRENT_UNIVERSE; events NONE (a split seen in split_ratio is an INFERRED_SPLIT warning, never booked);
EARNINGS_DATA_UNAVAILABLE; licence UNKNOWN; kind as the bundle declares (SYNTHETIC only for a fixture, which the builder enforces).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

from historical_data import calendar as CAL
from historical_data.adapters.ms1_evidence import STAMPS, SourceFormatError, SourceIntegrityError, _read, inside
from historical_data.provider import _TableProvider
from historical_data.schema import (DailyBar, Instrument, MembershipChange, SourceFile, SourceRef, SymbolSpan, bar_problems, licence_unknown)
from historical_data.status import EARNINGS_DATA_UNAVAILABLE, unavailable, unknown

ADAPTER, ADAPTER_VERSION, PROVIDER = "SipDailyV1Provider", "sip_daily_v1_reader_v1", "ALPACA"
SUPPORTED_CONTRACT_VERSIONS = ("historical_data_contract_v2",)
BUILDER = "sip_daily_v1"
UNIVERSE_ID = "SIP_DAILY_V1_UNIVERSE"
RAW_FILE_RE = re.compile(r"^(sec_company_tickers|bars_(all|raw|split)_\d{2})_p\d{3}\.json\.gz$")   # the builder's names: f"{name}_p{page:03d}.json.gz"


def _canon_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()   # = sip_bundle.sha256_of


def _ts(s: str) -> datetime:
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise SourceFormatError(f"{s!r} has no time zone")
    return t


class SipDailyV1Provider(_TableProvider):
    def __init__(self, out_dir):
        out = Path(out_dir)
        bundle_bytes, man_bytes = _read(out / "sip_bundle.json"), _read(out / "manifest.json")
        bundle, man = json.loads(bundle_bytes), json.loads(man_bytes)
        prov = bundle.get("provenance") or {}
        if prov.get("builder") != BUILDER or man.get("builder") != BUILDER:
            raise SourceFormatError("not a sip_daily_v1 bundle")
        if prov.get("volume_feed") != "SIP":
            raise SourceFormatError(f"volume_feed {prov.get('volume_feed')!r}: a sip_daily_v1 bundle declares SIP")
        if bundle.get("kind") not in ("HISTORICAL", "SYNTHETIC"):
            raise SourceFormatError("the bundle's kind")
        if prov.get("raw_manifest_sha256") != hashlib.sha256(man_bytes).hexdigest():
            raise SourceIntegrityError("manifest.json differs from the one the bundle was built from")
        if man.get("plan_sha256") != _canon_sha(man.get("plan")):
            raise SourceIntegrityError("the archived plan does not match its sha256")
        if man.get("complete") is not True:                                      # QA L-2: the builder builds only a complete fetch
            raise SourceIntegrityError(f"the raw fetch is not complete ({man.get('stopped')!r}): no dataset is read from it")
        if bundle["kind"] == "SYNTHETIC" and man.get("network") is not False:     # QA L-2: a fetch over the network is HISTORICAL, never SYNTHETIC
            raise SourceIntegrityError("the bundle declares SYNTHETIC but its raw fetch was not recorded as offline (network must be false)")
        bsha, msha = hashlib.sha256(bundle_bytes).hexdigest(), hashlib.sha256(man_bytes).hexdigest()
        sources = [SourceFile("sip_bundle.json", bsha, bsha), SourceFile("manifest.json", msha, msha)]
        series = bundle["aux"]["series"]

        # raw open / high / low from the archived "raw" responses (all of them, or none: integrity-only)
        raw_names = {r["name"] for r in man["plan"]["requests"] if r.get("kind") == "BARS" and r.get("adjustment") == "raw"}
        raw_resp = sorted((r for r in man.get("responses", []) if r["name"] in raw_names), key=lambda r: (r["name"], r["page"]))
        for r in man.get("responses", []):                                       # QA L-3: every manifest file name, before any file access
            if not RAW_FILE_RE.match(str(r.get("file"))):
                raise SourceFormatError(f"{r.get('file')!r}: not a sip_daily_v1 raw response file name")
            inside(out / "raw", r["file"])
        present = [r for r in raw_resp if inside(out / "raw", r["file"]).is_file()]
        ohl: Dict[Tuple[str, str], tuple] = {}
        ohl_available = bool(raw_resp) and len(present) == len(raw_resp)
        if present and not ohl_available:
            raise SourceIntegrityError(f"{len(raw_resp) - len(present)} of {len(raw_resp)} archived raw responses are missing")
        for r in present if ohl_available else []:
            stored = _read(inside(out / "raw", r["file"]))
            body = gzip.decompress(stored)
            if hashlib.sha256(body).hexdigest() != r["sha256"]:
                raise SourceIntegrityError(f"{r['file']}: sha256 differs from the manifest")
            fsha = hashlib.sha256(stored).hexdigest()
            sources.append(SourceFile(f"raw/{r['file']}", fsha, r["sha256"]))
            idx = 0
            for sym, rows in (json.loads(body).get("bars") or {}).items():
                for b in rows:
                    t = str(b.get("t") or "")
                    if t[10:] not in STAMPS:
                        raise SourceFormatError(f"{r['file']}: {sym} {t} is not a daily-bar stamp")
                    key = (sym, t[:10])
                    if key in ohl:
                        raise SourceFormatError(f"{sym} {t[:10]}: two archived raw bars")
                    ohl[key] = (b["o"], b["h"], b["l"], b["c"], b["v"], SourceRef(fsha, r["sha256"], idx))
                    idx += 1
        adj_close = {s: {b["date"]: b["close"] for b in rows} for s, rows in (bundle.get("bars") or {}).items()}
        bundle_sha = bsha
        instruments, bars, removed, changes = [], [], [], []
        for sym in sorted(series.get("raw_close", {})):
            closes = series["raw_close"][sym]
            if not closes:
                continue
            vols, ratios = series.get("raw_volume", {}).get(sym, {}), series.get("split_ratio", {}).get(sym, {})
            dates = sorted(closes)
            candidates = []
            for k, d in enumerate(dates):
                c, v = closes[d], vols.get(d)
                if ohl_available:
                    x = ohl.get((sym, d))
                    if x is None or float(x[3]) != float(c):
                        raise SourceIntegrityError(f"{sym} {d}: the raw close is not the archived raw bar's close")
                    o, h, lo, ref = x[0], x[1], x[2], x[5]
                else:
                    o = h = lo = unavailable("RAW_OHL_NOT_ARCHIVED")
                    ref = SourceRef(bundle_sha, bundle_sha, k)
                candidates.append((d, o, h, lo, c, v if v is not None else -1.0, ref))
            valid = [r for r in candidates if not bar_problems(*r[1:6])]
            if not valid:
                continue
            first = valid[0][0]
            iid = f"ALPACA:{sym}:{first}"
            instruments.append(Instrument(instrument_id=iid, symbol_history=(SymbolSpan(sym, first, None),), symbol_history_status="UNKNOWN",
                                          asset_type=unknown("NOT_IN_SOURCE"), listing=unknown("NOT_IN_SOURCE")))
            changes.append(MembershipChange(UNIVERSE_ID, iid, first, "ADD", unknown("FIXED_CURRENT_UNIVERSE")))
            for d, o, h, lo, c, v, ref in candidates:
                if bar_problems(o, h, lo, c, v):
                    removed.append((iid, d))
                    continue
                sf = ratios.get(d)
                ac = adj_close.get(sym, {}).get(d)
                try:
                    known_at = CAL.close_utc(d)
                except (CAL.CalendarNotCovered, CAL.NotASession):
                    known_at = datetime.fromisoformat(d + "T00:00:00+00:00")       # not a calendar session: validation BLOCKs it
                bars.append(DailyBar(instrument_id=iid, session=d, raw_open=o, raw_high=h, raw_low=lo, raw_close=c, raw_volume=v,
                                     split_factor=float(sf) if sf else unavailable("SPLIT_RATIO_MISSING"),
                                     total_return_factor=(c / ac) if ac else unavailable("ALL_ADJUSTED_BAR_ABSENT"),
                                     volume_feed="CONSOLIDATED", price_source="CONSOLIDATED", known_at=known_at, source_ref=ref))
        retrieved = man.get("retrieved_at")
        retrieved_at = _ts(retrieved) if retrieved else unavailable("NOT_RECORDED")
        as_of = unavailable("NOT_RECORDED")
        if retrieved:
            from zoneinfo import ZoneInfo
            nyd = retrieved_at.astimezone(ZoneInfo(CAL.ZONE)).date()
            s = CAL.session_of_date(nyd)
            as_of = s if (s is not None and nyd.isoformat() <= CAL.COVERAGE[1]) else unknown("CALENDAR_NOT_COVERED")
        views = ("RAW_CLOSE",) + (("RAW_OHL",) if ohl_available else ()) + ("SPLIT_ADJUSTED", "TOTAL_RETURN")
        notes = [{"removed_invalid": [list(x) for x in sorted(removed)]}] if removed else []
        if not ohl_available:
            notes.append({"raw_ohl": "the archived raw responses are absent: integrity-only"})
        planned = set(man["plan"].get("universe") or []) | set(series.get("raw_close", {}))
        dropped = sorted(planned - {i.symbol_history[0].symbol for i in instruments})   # planned, but no valid bar (L-5)
        super().__init__(
            manifest_fields=dict(dataset_id="sip-daily-v1:" + bundle_sha[:16], kind=bundle["kind"], provider=PROVIDER, adapter=ADAPTER,
                                 adapter_version=ADAPTER_VERSION, source_files=tuple(sources), price_views_available=views,
                                 volume_feed="CONSOLIDATED", price_source="CONSOLIDATED", membership_basis="FIXED_CURRENT_UNIVERSE",
                                 events_completeness="NONE", adjustment_as_of=as_of, retrieved_at=retrieved_at, licence=licence_unknown(),
                                 data_exposure=unknown("NO_EXPOSURE_RECORD")),
            instruments=instruments, bars=bars, events=(), membership=changes, scheduled_events=EARNINGS_DATA_UNAVAILABLE, removed=removed, notes=notes,
            dropped_symbols=dropped)
