"""
historical_data/adapters/ms1_evidence.py - Ms1EvidenceProvider: the MS-1 evidence (Alpaca IEX daily bars, raw adjustment) read from its RAW
ARCHIVE ONLY: <raw>/manifest.jsonl plus the gzipped bodies it names (the Stage 7.1 RawArchive format: <day>/<fetch_id>.json.gz, one manifest line each), each body's sha256
verified against its manifest line. Never the SQLite store beside it - no database is opened, not even read-only (P6) - and nothing is
written. Licence UNKNOWN (section 6.2): schema, integrity and validation checks only; no returns or signals (as_of / execution_open refuse).

Mapping (contract section 4.1): raw o / h / l / c / v from each body's bars; the session is the bar's New York date (the stamp must be
midnight New York); known_at = close_utc(session), never the archive's retrieved_at; split_factor and total_return_factor UNAVAILABLE (no
signals unless a run is flagged SPLIT_UNADJUSTED); volume_feed and price_source SINGLE_VENUE:IEX; instrument_id ALPACA:<symbol>:<first
session>, symbol history UNKNOWN; FIXED_CURRENT_UNIVERSE; events NONE; EARNINGS_DATA_UNAVAILABLE. An error envelope body (a non-200 response
the archive kept) is skipped and recorded in adapter_notes; any other unexpected body refuses the dataset (SourceFormatError).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from historical_data import HISTORICAL
from historical_data import calendar as CAL
from historical_data.provider import _TableProvider
from historical_data.schema import (DailyBar, Instrument, MembershipChange, SourceFile, SourceRef, SymbolSpan, bar_problems, licence_unknown)
from historical_data.status import EARNINGS_DATA_UNAVAILABLE, unavailable, unknown

ADAPTER, ADAPTER_VERSION, PROVIDER = "Ms1EvidenceProvider", "ms1_evidence_v1", "ALPACA"
SUPPORTED_CONTRACT_VERSIONS = ("historical_data_contract_v2",)
FEED = "SINGLE_VENUE:IEX"
UNIVERSE_ID = "MS1_EVIDENCE_UNIVERSE"
ENDPOINT_RE = re.compile(r"^/v2/stocks/([A-Z][A-Z0-9.]{0,9})/bars$")
PATH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.json\.gz$")
STAMPS = ("T04:00:00Z", "T05:00:00Z")                                   # midnight New York (EDT / EST)
PAGE_KEYS, PAGE_KEYS_ALLOWED = frozenset(("bars", "symbol", "next_page_token")), frozenset(("bars", "symbol", "next_page_token", "currency"))
BAR_KEYS, BAR_REQUIRED = frozenset(("t", "o", "h", "l", "c", "v", "n", "vw")), ("t", "o", "h", "l", "c", "v")
ENTRY_KEYS = ("fetch_id", "path", "sha256", "bytes", "provider", "endpoint", "retrieved_at")


class SourceIntegrityError(ValueError):
    """A body is missing, unreadable, or its sha256 / size differs from the manifest: no dataset."""


class SourceFormatError(ValueError):
    """The archive is not in the expected format: no dataset."""


def _read(path: Path) -> bytes:
    with open(path, "rb") as f:                                        # read-only, bytes; the only file access of this adapter
        return f.read()


def inside(folder: Path, rel: str) -> Path:
    """`folder / rel` only when `rel` is a plain relative name whose resolved path stays inside `folder` (QA L-3): an absolute path, a drive,
    a `..` part or a link out of the folder is refused before anything is read."""
    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or ":" in rel or ".." in rel.split("/"):
        raise SourceFormatError(f"{rel!r}: not a plain relative archive path")
    base = Path(folder).resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise SourceFormatError(f"{rel!r}: resolves outside the archive folder")
    return p


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ts(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise SourceFormatError(f"retrieved_at {s!r} has no time zone")
    return t


class Ms1EvidenceProvider(_TableProvider):
    def __init__(self, raw_dir):
        raw = Path(raw_dir)
        mbytes = _read(raw / "manifest.jsonl")
        msha = hashlib.sha256(mbytes).hexdigest()
        sources = [SourceFile("raw/manifest.jsonl", msha, msha)]
        rows: Dict[str, List[tuple]] = {}
        notes, retrieved, seen = [], [], set()
        zone = None
        if CAL.tzdata_status()[0]:            # without tzdata only the stamp's form is checked here; construction then fails closed at close_utc
            from zoneinfo import ZoneInfo     # (TzdataUnavailable): no dataset, and so no acceptance record (I-5, accepted fail-closed behaviour)
            zone = ZoneInfo(CAL.ZONE)
        for n, line in enumerate(mbytes.decode("utf-8").splitlines()):
            if not line.strip():
                continue
            e = json.loads(line)
            if not isinstance(e, dict) or any(k not in e for k in ENTRY_KEYS):
                raise SourceFormatError(f"manifest line {n + 1}: not an archive entry")
            m = ENDPOINT_RE.match(str(e["endpoint"]))
            if e["provider"] != PROVIDER or not m or not PATH_RE.match(str(e["path"])) or not re.fullmatch(r"[0-9a-f]{64}", str(e["sha256"])):
                raise SourceFormatError(f"manifest line {n + 1}: not an Alpaca daily-bars archive entry ({e.get('endpoint')!r}, {e.get('path')!r})")
            symbol = m.group(1)
            seen.add(symbol)
            path = inside(raw, e["path"])
            try:
                stored = _read(path)
                body = gzip.decompress(stored)
            except (OSError, EOFError) as exc:
                raise SourceIntegrityError(f"{e['path']}: {type(exc).__name__}") from None
            if hashlib.sha256(body).hexdigest() != e["sha256"] or len(body) != e["bytes"]:
                raise SourceIntegrityError(f"{e['path']}: sha256 / size differs from the manifest")
            file_sha = hashlib.sha256(stored).hexdigest()
            sources.append(SourceFile(f"raw/{e['path']}", file_sha, e["sha256"]))
            retrieved.append(_ts(str(e["retrieved_at"])))
            doc = json.loads(body)
            if isinstance(doc, dict) and ("message" in doc or "code" in doc):
                notes.append({"skipped_response": e["path"], "endpoint": e["endpoint"], "symbol": symbol, "why": "ERROR_ENVELOPE"})
                continue
            if not isinstance(doc, dict) or not PAGE_KEYS <= set(doc) or not set(doc) <= PAGE_KEYS_ALLOWED or doc["symbol"] != symbol \
                    or not isinstance(doc["bars"], (list, type(None))):
                raise SourceFormatError(f"{e['path']}: not a bars page of {symbol}")
            for i, b in enumerate(doc["bars"] or []):
                if not isinstance(b, dict) or not set(b) <= BAR_KEYS or any(k not in b for k in BAR_REQUIRED) or not isinstance(b["t"], str) \
                        or not all(_num(b[k]) for k in ("o", "h", "l", "c", "v")):
                    raise SourceFormatError(f"{e['path']} bar {i}: malformed")
                if b["t"][10:] not in STAMPS:
                    raise SourceFormatError(f"{e['path']} bar {i}: {b['t']} is not a daily-bar stamp (midnight New York)")
                session = b["t"][:10]
                if zone is not None:
                    ny = _ts(b["t"]).astimezone(zone)
                    if (ny.hour, ny.minute, ny.second) != (0, 0, 0) or ny.date().isoformat() != session:
                        raise SourceFormatError(f"{e['path']} bar {i}: {b['t']} is not midnight New York")
                rows.setdefault(symbol, []).append((session, b["o"], b["h"], b["l"], b["c"], b["v"], SourceRef(file_sha, e["sha256"], i)))
        instruments, bars, removed, changes = [], [], [], []
        for symbol in sorted(rows):
            valid = [r for r in rows[symbol] if not bar_problems(*r[1:6])]
            if not valid:
                notes.append({"symbol_without_valid_bar": symbol})
                continue
            first = min(r[0] for r in valid)
            iid = f"ALPACA:{symbol}:{first}"
            instruments.append(Instrument(instrument_id=iid, symbol_history=(SymbolSpan(symbol, first, None),), symbol_history_status="UNKNOWN",
                                          asset_type=unknown("NOT_IN_SOURCE"), listing=unknown("NOT_IN_SOURCE")))
            changes.append(MembershipChange(UNIVERSE_ID, iid, first, "ADD", unknown("FIXED_CURRENT_UNIVERSE")))
            for r in rows[symbol]:
                if bar_problems(*r[1:6]):
                    removed.append((iid, r[0]))
                    continue
                try:
                    known_at = CAL.close_utc(r[0])
                except (CAL.CalendarNotCovered, CAL.NotASession):
                    known_at = datetime.fromisoformat(r[0] + "T00:00:00+00:00")   # not a calendar session: validation BLOCKs it (never a guess)
                bars.append(DailyBar(instrument_id=iid, session=r[0], raw_open=r[1], raw_high=r[2], raw_low=r[3], raw_close=r[4], raw_volume=r[5],
                                     split_factor=unavailable("NOT_IN_SOURCE"), total_return_factor=unavailable("NOT_IN_SOURCE"), volume_feed=FEED,
                                     price_source=FEED, known_at=known_at, source_ref=r[6]))
        if removed:
            notes.append({"removed_invalid": [list(x) for x in sorted(removed)]})
        dropped = sorted(seen - {i.symbol_history[0].symbol for i in instruments})       # only envelopes, or no valid bar (L-5)
        super().__init__(
            manifest_fields=dict(dataset_id="ms1-evidence:" + msha[:16], kind=HISTORICAL, provider=PROVIDER, adapter=ADAPTER,
                                 adapter_version=ADAPTER_VERSION, source_files=tuple(sources), price_views_available=("RAW_CLOSE", "RAW_OHL"),
                                 volume_feed=FEED, price_source=FEED, membership_basis="FIXED_CURRENT_UNIVERSE", events_completeness="NONE",
                                 adjustment_as_of=unavailable("NO_ADJUSTED_FACTORS"), retrieved_at=max(retrieved) if retrieved else unavailable("NO_RESPONSES"),
                                 licence=licence_unknown(), data_exposure=unknown("NO_EXPOSURE_RECORD")),
            instruments=instruments, bars=bars, events=(), membership=changes, scheduled_events=EARNINGS_DATA_UNAVAILABLE, removed=removed, notes=notes,
            dropped_symbols=dropped)
